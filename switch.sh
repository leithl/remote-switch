#!/bin/bash

# Configuration
gpio_pin="17"
enable_temp="yes"  # set to "no" to disable all temperature features

# Safely extract known parameters from the query string.
# The previous approach used eval on raw input, which allows remote code execution.
IFS='&' read -ra params <<< "$QUERY_STRING"
for p in "${params[@]}"; do
  IFS='=' read -r key val <<< "$p"
  if [[ "$key" == "state" ]]; then
    state="$val"
  elif [[ "$key" == "range" ]]; then
    range="$val"
  elif [[ "$key" == "sched_dt" ]]; then
    sched_dt="$val"
  elif [[ "$key" == "sched_action" ]]; then
    sched_action="$val"
  elif [[ "$key" == "cancel_id" ]]; then
    cancel_id="$val"
  elif [[ "$key" == "manifest" ]]; then
    manifest="$val"
  elif [[ "$key" == "icon" ]]; then
    icon="$val"
  fi
done

# Serve web app manifest (for Android home screen install)
if [[ "$manifest" == "1" ]]; then
  echo -e "Content-type: application/manifest+json\r\n\r"
  cat << 'MANIFEST'
{"name":"Heater Control","short_name":"Heater","start_url":"switch.sh","display":"standalone","background_color":"#212529","theme_color":"#212529","icons":[{"src":"switch.sh?icon=192","sizes":"any","type":"image/svg+xml"}]}
MANIFEST
  exit 0
fi

# Serve home screen icon
if [[ "$icon" == "192" || "$icon" == "512" ]]; then
  echo -e "Content-type: image/svg+xml\r\n\r"
  cat << 'ICON'
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><rect width="512" height="512" rx="80" fill="#212529"/><path d="M256 60c-50 0-90 70-90 150 0 60 30 110 60 130v32c0 17 13 30 30 30s30-13 30-30v-32c30-20 60-70 60-130 0-80-40-150-90-150zm0 60c25 0 50 45 50 90 0 35-15 65-35 80h-30c-20-15-35-45-35-80 0-45 25-90 50-90z" fill="#ff6420"/><path d="M256 140c-15 0-30 30-30 60 0 25 10 45 20 55h20c10-10 20-30 20-55 0-30-15-60-30-60z" fill="#ffc832"/></svg>
ICON
  exit 0
fi

# Specify the GPIO file path
gpio_file="/sys/class/gpio/gpio$gpio_pin"
gpio_value="$gpio_file/value"

# Bail out early if the GPIO pin hasn't been exported
if [[ ! -f "$gpio_value" ]]; then
  echo -e "Content-type: text/plain\r\n\r\n"
  echo "Error: GPIO pin $gpio_pin is not configured ($gpio_value not found)"
  exit 1
fi

# Only write to GPIO if state is exactly "0" or "1", then redirect
if [[ "$state" == "0" || "$state" == "1" ]]; then
  echo "$state" > "$gpio_value"
  echo -e "Status: 303 See Other\r\nLocation: switch.sh\r\n\r"
  exit 0
fi

# Read the value from the GPIO file
value=$(< "$gpio_value")

# Determine status text and toggle button
if [[ $value -eq 0 ]]; then
  status="off"
  header_class="text-bg-secondary"
  toggle_btn='<button type="submit" name="state" class="btn btn-success btn-lg" value="1">turn on</button>'
else
  status="on"
  header_class="text-bg-success"
  toggle_btn='<button type="submit" name="state" class="btn btn-danger btn-lg" value="0">turn off</button>'
fi

# Handle scheduling
now=$(date +%s)
sched_csv="/run/heater-schedule.csv"
sched_msg=""
if [[ -n "$sched_dt" && ( "$sched_action" == "0" || "$sched_action" == "1" ) ]]; then
  # URL-decode: %3A -> :, %2F -> /, + -> space
  sched_dt_decoded=$(echo "$sched_dt" | sed 's/%3A/:/g; s/%2F/\//g; s/+/ /g')
  sched_epoch=$(date -d "${sched_dt_decoded/T/ }" +%s 2>/dev/null)
  if [[ -z "$sched_epoch" ]]; then
    sched_msg='<div class="alert alert-danger alert-sm py-1 mb-2">Invalid date/time.</div>'
  elif (( sched_epoch <= now )); then
    sched_msg='<div class="alert alert-warning alert-sm py-1 mb-2">Cannot schedule in the past.</div>'
  else
    (
      flock -x 200
      echo "$now,$sched_epoch,$sched_action" >> "$sched_csv"
    ) 200>/tmp/heater-schedule.lock 2>/dev/null
    if grep -q "^${now},${sched_epoch},${sched_action}$" "$sched_csv" 2>/dev/null; then
      # Redirect to clean URL to prevent duplicate on refresh
      echo -e "Status: 303 See Other\r\nLocation: switch.sh\r\n\r"
      exit 0
    else
      sched_msg='<div class="alert alert-danger alert-sm py-1 mb-2">Failed to save schedule. Run <code>sudo log_temp.sh</code> once to initialize.</div>'
    fi
  fi
fi

# Handle schedule cancellation
if [[ -n "$cancel_id" && "$cancel_id" =~ ^[0-9]+$ && -f "$sched_csv" ]]; then
  (
    flock -x 200
    grep -v "^${cancel_id}," "$sched_csv" > "${sched_csv}.tmp"
    cat "${sched_csv}.tmp" > "$sched_csv"
    rm -f "${sched_csv}.tmp"
  ) 200>/tmp/heater-schedule.lock 2>/dev/null
  # Redirect to clean URL to prevent duplicate on refresh
  echo -e "Status: 303 See Other\r\nLocation: switch.sh\r\n\r"
  exit 0
fi

# Read pending schedules for display
pending_schedules=""
if [[ -f "$sched_csv" && -s "$sched_csv" ]]; then
  while IFS=, read -r sid sepoch saction; do
    [[ -z "$sid" || -z "$sepoch" || -z "$saction" ]] && continue
    action_label=$( [[ "$saction" == "1" ]] && echo "Turn ON" || echo "Turn OFF" )
    sched_time=$(date -d @"$sepoch" '+%b %d, %Y %I:%M %p' 2>/dev/null)
    [[ -z "$sched_time" ]] && continue
    pending_schedules="${pending_schedules}<tr><td>$sched_time</td><td>$action_label</td><td><a href=\"switch.sh?cancel_id=$sid\" class=\"btn btn-sm btn-outline-danger py-0\">Cancel</a></td></tr>"
  done < "$sched_csv"
fi

# Shared time variables
ram_csv="/run/heater-temp.csv"
disk_csv="/var/lib/heater-temp.csv"
chart_dir="/var/lib/heater-chart"
cms=$(date +%Y-%m-01)
cur_month=$(date +%Y-%m)

# Default range
[[ -z "$range" ]] && range="7d"

# Validate range: must be 7d, 30d, or YYYY-MM format
if [[ "$range" != "7d" && "$range" != "30d" && ! "$range" =~ ^[0-9]{4}-[0-9]{2}$ ]]; then
  range="7d"
fi

# Build month keys, labels, and starts for the last 13 months
month_keys=""
month_labels=""
month_starts=""
for i in $(seq 12 -1 0); do
  mk=$(date -d "$cms -$i month" +%Y-%m)
  ml=$(date -d "$cms -$i month" +"%b %Y")
  ms=$(date -d "$cms -$i month" +%s)
  month_keys="${month_keys:+$month_keys|}$mk"
  month_labels="${month_labels:+$month_labels|}$ml"
  month_starts="${month_starts:+$month_starts|}$ms"
done
next_ms=$(date -d "$cms +1 month" +%s)
month_starts="$month_starts|$next_ms"

# Determine chart cutoff and end based on range, and chart title
chart_title=""
if [[ "$range" == "30d" ]]; then
  chart_cutoff=$((now - 30 * 86400))
  chart_end="$now"
  chart_title="Temperature - Last 30 Days"
elif [[ "$range" =~ ^[0-9]{4}-[0-9]{2}$ ]]; then
  chart_cutoff=$(date -d "${range}-01" +%s 2>/dev/null)
  chart_end=$(date -d "${range}-01 +1 month" +%s 2>/dev/null)
  chart_title="Temperature - $(date -d "${range}-01" +"%b %Y" 2>/dev/null)"
  # Fall back to 7d if date parsing failed
  if [[ -z "$chart_cutoff" || -z "$chart_end" ]]; then
    range="7d"
  fi
fi
if [[ "$range" == "7d" ]]; then
  chart_cutoff=$((now - 7 * 86400))
  chart_end="$now"
  chart_title="Temperature - Last 7 Days"
fi

# Collect pre-computed stats from .dat files for past months
pre_temp_rows=""
pre_runtime_rows=""
IFS='|' read -ra mk_arr <<< "$month_keys"
for mk in "${mk_arr[@]}"; do
  dat="$chart_dir/$mk.dat"
  if [[ "$mk" != "$cur_month" && -f "$dat" ]]; then
    row=$(grep '^temp|' "$dat" | cut -d'|' -f2-)
    [[ -n "$row" ]] && pre_temp_rows="${pre_temp_rows}${row}
"
    row=$(grep '^runtime|' "$dat" | cut -d'|' -f2-)
    [[ -n "$row" ]] && pre_runtime_rows="${pre_runtime_rows}${row}
"
  fi
done

# Build list of months that still need live computation (no .dat file)
live_months=""
for mk in "${mk_arr[@]}"; do
  dat="$chart_dir/$mk.dat"
  if [[ "$mk" == "$cur_month" || ! -f "$dat" ]]; then
    live_months="${live_months:+$live_months|}$mk"
  fi
done

# Check if chart data comes from a pre-computed .dat file
chart_from_dat=""
if [[ "$range" =~ ^[0-9]{4}-[0-9]{2}$ && "$range" != "$cur_month" ]]; then
  dat="$chart_dir/$range.dat"
  if [[ -f "$dat" ]]; then
    chart_data=$(grep '^chart|' "$dat" | cut -d'|' -f2-)
    heater_data=$(grep '^heater|' "$dat" | cut -d'|' -f2-)
    cold_data=$(grep '^cold|' "$dat" | cut -d'|' -f2-)
    chart_from_dat="yes"
  fi
fi

if [[ "$enable_temp" == "yes" ]]; then
  # Read temperature from DS18B20 1-wire probe
  w1_device=$(echo /sys/bus/w1/devices/28-*/w1_slave)
  if [[ ! -f "$w1_device" ]]; then
    temp_display="Temperature probe not found"
  elif ! head -1 "$w1_device" | grep -q "YES$"; then
    temp_display="Temperature read error"
  else
    temp_display=$(awk -F 't=' '/t=/{c=$2/1000; f=(c*1.8)+32; printf "%.1f &deg;C | %.1f &deg;F", c, f}' "$w1_device")
  fi

  # Compute chart data + stats for months without .dat files
  awk_output=$(cat "$disk_csv" "$ram_csv" 2>/dev/null | awk -F, \
    -v cutoff="$chart_cutoff" \
    -v chart_end="$chart_end" \
    -v now="$now" \
    -v m_starts="$month_starts" \
    -v m_labels="$month_labels" \
    -v m_keys="$month_keys" \
    -v live="$live_months" \
    -v need_chart="$( [[ -z "$chart_from_dat" ]] && echo 1 || echo 0 )" \
  'BEGIN {
    nm = split(m_starts, starts, "|")
    split(m_labels, labels, "|")
    split(m_keys, keys, "|")
    nl = split(live, live_arr, "|")
    for (j = 1; j <= nl; j++) live_set[live_arr[j]] = 1
    sep = ""
  }
  {
    epoch = $1 + 0
    if (epoch == 0) next

    # Temperature chart + monthly stats
    if ($2 != "") {
      temp = $2 + 0

      # Chart: 15-min buckets within the selected range
      if (need_chart + 0 == 1 && epoch >= cutoff + 0 && epoch < chart_end + 0) {
        b = int(epoch / 900) * 900
        if (b != prev_b && prev_b != "") {
          f = (bsum / bcnt) * 1.8 + 32
          chart = chart sep sprintf("{x:%d000,y:%.1f}", prev_b, f)
          sep = ","
        }
        if (b != prev_b) { bsum = 0; bcnt = 0; prev_b = b }
        bsum += temp; bcnt++
      }

      # Monthly stats (only for months that need live computation)
      for (i = 1; i < nm; i++) {
        if (epoch >= starts[i] + 0 && epoch < starts[i+1] + 0) {
          if (keys[i] in live_set) {
            msum[i] += temp; mcnt[i]++
            if (!(i in mmin) || temp < mmin[i]) mmin[i] = temp
            if (!(i in mmax) || temp > mmax[i]) mmax[i] = temp
            if (temp <= 8.89) cmins[i]++
          }
          break
        }
      }

      # Cold range tracking for chart (temp <= 48°F / 8.89°C)
      if (need_chart + 0 == 1 && epoch >= cutoff + 0 && epoch < chart_end + 0) {
        if (temp <= 8.89) {
          if (!in_cold) { cold_start = epoch; in_cold = 1 }
          cold_last = epoch
        } else if (in_cold) {
          cranges = cranges csep sprintf("{xMin:%d000,xMax:%d000}", cold_start, cold_last + 60)
          csep = ","
          in_cold = 0
        }
      }
    }

    # Heater on/off ranges for chart annotation
    if (need_chart + 0 == 1 && epoch >= cutoff + 0 && epoch < chart_end + 0 && $3 != "") {
      if ($3 + 0 == 1) {
        if (!in_h) { h_start = epoch; in_h = 1 }
        h_last = epoch
      } else if (in_h) {
        hranges = hranges hsep sprintf("{xMin:%d000,xMax:%d000}", h_start, h_last + 60)
        hsep = ","
        in_h = 0
      }
    }
  }
  END {
    # Flush last chart bucket
    if (need_chart + 0 == 1 && bcnt > 0) {
      f = (bsum / bcnt) * 1.8 + 32
      chart = chart sep sprintf("{x:%d000,y:%.1f}", prev_b, f)
    }
    print chart

    # Stats rows for live months only
    for (i = 1; i < nm; i++) {
      if (mcnt[i] + 0 == 0) continue
      if (i == nm - 1)
        possible = int((now + 0 - starts[i] + 0) / 60)
      else
        possible = int((starts[i+1] + 0 - starts[i] + 0) / 60)
      pct = (possible > 0) ? (mcnt[i] / possible) * 100 : 0
      lbl = (pct >= 100) ? labels[i] : sprintf("%s (%.1f%%)", labels[i], pct)
      avg_c = msum[i] / mcnt[i]; avg_f = avg_c * 1.8 + 32
      min_f = mmin[i] * 1.8 + 32; max_f = mmax[i] * 1.8 + 32
      cold_hrs = (cmins[i] + 0) / 60
      printf "temp:%s|<tr onclick=\"location='"'"'switch.sh?range=%s'"'"'\" style=\"cursor:pointer\"><td>%s</td><td>%.1f / %.1f</td><td>%.1f / %.1f</td><td>%.1f / %.1f</td><td>%.1f</td></tr>\n", \
        keys[i], keys[i], lbl, avg_f, avg_c, min_f, mmin[i], max_f, mmax[i], cold_hrs
    }

    # Heater on/off ranges
    if (in_h) {
      hranges = hranges hsep sprintf("{xMin:%d000,xMax:%d000}", h_start, h_last + 60)
    }
    printf "heater|%s\n", hranges

    # Cold ranges (temp <= 48°F)
    if (in_cold) {
      cranges = cranges csep sprintf("{xMin:%d000,xMax:%d000}", cold_start, cold_last + 60)
    }
    printf "cold|%s\n", cranges
  }')

  if [[ -z "$chart_from_dat" ]]; then
    chart_data=$(echo "$awk_output" | head -1)
    heater_data=$(echo "$awk_output" | grep '^heater|' | cut -d'|' -f2-)
    cold_data=$(echo "$awk_output" | grep '^cold|' | cut -d'|' -f2-)
  fi
  live_temp_rows=$(echo "$awk_output" | grep '^temp:' | sed 's/^temp:[^|]*|//')
fi

# Compute runtime stats for live months from raw CSV
live_runtime_rows=$(cat "$disk_csv" "$ram_csv" 2>/dev/null | awk -F, \
  -v now="$now" \
  -v m_starts="$month_starts" \
  -v m_labels="$month_labels" \
  -v m_keys="$month_keys" \
  -v live="$live_months" \
'BEGIN {
  nm = split(m_starts, starts, "|")
  split(m_labels, labels, "|")
  split(m_keys, keys, "|")
  nl = split(live, live_arr, "|")
  for (j = 1; j <= nl; j++) live_set[live_arr[j]] = 1
}
{
  epoch = $1 + 0; st = $3 + 0
  if (epoch == 0 || $3 == "") next
  for (i = 1; i < nm; i++) {
    if (epoch >= starts[i] + 0 && epoch < starts[i+1] + 0) {
      if (keys[i] in live_set) {
        rcnt[i]++
        if (st == 1) secs[i] += 60
      }
      break
    }
  }
}
END {
  for (i = 1; i < nm; i++) {
    if (rcnt[i] + 0 == 0) continue
    hours = secs[i] + 0 > 0 ? secs[i] / 3600 : 0
    if (i == nm - 1)
      possible = int((now + 0 - starts[i] + 0) / 60)
    else
      possible = int((starts[i+1] + 0 - starts[i] + 0) / 60)
    pct = (possible > 0) ? (rcnt[i] / possible) * 100 : 0
    lbl = (pct >= 100) ? labels[i] : sprintf("%s (%.1f%%)", labels[i], pct)
    days = (i == nm - 1) ? (now + 0 - starts[i] + 0) / 86400 : (starts[i+1] + 0 - starts[i] + 0) / 86400
    avg = (days > 0) ? hours / days : 0
    printf "<tr><td>%s</td><td>%.1f</td><td>%.1f</td></tr>\n", lbl, hours, avg
  }
}')

# Merge pre-computed + live stats rows (pre-computed are older months, live is current)
stats_rows="${pre_temp_rows}${live_temp_rows}"
runtime_rows="${pre_runtime_rows}${live_runtime_rows}"

# Add onclick to pre-computed temp rows (they come from .dat without onclick)
# We need to add clickability to pre-computed rows too
stats_rows_final=""
IFS='|' read -ra mk_arr <<< "$month_keys"
for mk in "${mk_arr[@]}"; do
  dat="$chart_dir/$mk.dat"
  if [[ "$mk" != "$cur_month" && -f "$dat" ]]; then
    row=$(grep '^temp|' "$dat" | cut -d'|' -f2-)
    if [[ -n "$row" ]]; then
      # Add onclick to existing <tr>
      row="${row/<tr>/<tr onclick=\"location=\'switch.sh?range=$mk\'\" style=\"cursor:pointer\">}"
      stats_rows_final="${stats_rows_final}${row}
"
    fi
  else
    # Live-computed row (already has onclick from awk)
    row=$(echo "$awk_output" 2>/dev/null | grep "^temp:$mk|" | sed 's/^temp:[^|]*|//')
    [[ -n "$row" ]] && stats_rows_final="${stats_rows_final}${row}
"
  fi
done
[[ -n "$stats_rows_final" ]] && stats_rows="$stats_rows_final"

# === HTML Output ===
echo -e "Content-type: text/html\r\n\r\n"

cat << EOF
<html>
<head>
<title>Airplane Hanger Heater Control</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="mobile-web-app-capable" content="yes">
<meta name="theme-color" content="#212529">
<link rel="manifest" href="switch.sh?manifest=1">
<link rel="icon" href="switch.sh?icon=192" type="image/svg+xml">
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.8/dist/css/bootstrap.min.css" rel="stylesheet" integrity="sha384-sRIl4kxILFvY47J16cr9ZwB07vP4J8+LH7qKQnuqkuIAvNWLzeN8tE5YBujZqJLB" crossorigin="anonymous">
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.8/dist/js/bootstrap.bundle.min.js" integrity="sha384-FKyoEForCGlyvwx9Hj09JcYn3nv7wiPVlz7YYwJrWVcXK/BmnVDxM+D2scQbITxI" crossorigin="anonymous"></script>
EOF

if [[ "$enable_temp" == "yes" ]]; then
cat << EOF
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-zoom@2"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-annotation@3"></script>
EOF
fi

cat << EOF
</head>
<body>
<div class="container mt-3">

<div class="card mb-3">
<div class="card-header $header_class">
  <h4>Airplane Hanger Heater Control</h4>
</div>
<div class="card-body">
<h5 class="card-title">Heater Status: <b>$status</b></h5>
<form action="switch.sh" method="GET">
<p>$toggle_btn</p>
</form>
</div>
<div class="card-footer text-body-secondary">
  $(date)
</div>
</div>
EOF

if [[ -n "$runtime_rows" ]]; then
cat << EOF

<div class="card mb-3">
<div class="card-header"><h5 class="mb-0">Heater Runtime</h5></div>
<div class="card-body">
<div class="table-responsive">
<table class="table table-sm table-striped mb-0">
<thead><tr><th>Month</th><th>Total Hours</th><th>Avg Hours/Day</th></tr></thead>
<tbody>
$runtime_rows
</tbody>
</table>
</div>
</div>
</div>
EOF
fi

if [[ "$enable_temp" == "yes" ]]; then
# Range button styles
btn_7d="btn-outline-primary"
btn_30d="btn-outline-primary"
if [[ "$range" == "7d" ]]; then btn_7d="btn-primary"; fi
if [[ "$range" == "30d" ]]; then btn_30d="btn-primary"; fi

cat << EOF

<div class="card mb-3">
<div class="card-header"><h5 class="mb-0">$chart_title</h5></div>
<div class="card-body">
  <p class="mb-2"><strong>Current:</strong> $temp_display</p>
  <div class="mb-2">
    <a href="switch.sh?range=7d" class="btn btn-sm $btn_7d">7 Days</a>
    <a href="switch.sh?range=30d" class="btn btn-sm $btn_30d">30 Days</a>
    <span class="ms-3 small text-muted"><span style="display:inline-block;width:12px;height:12px;background:rgba(220,53,69,0.3);vertical-align:middle"></span> Heater on</span>
    <span class="ms-2 small text-muted"><span style="display:inline-block;width:12px;height:12px;background:rgba(255,152,0,0.3);vertical-align:middle"></span> &le; 48&deg;F</span>
  </div>
  <canvas id="tempChart"></canvas>
  <button id="resetZoom" style="display:none" class="btn btn-sm btn-outline-secondary mt-2" onclick="tempChart.resetZoom()">Reset zoom</button>
  <p id="noChartData" style="display:none" class="text-muted mb-0">No temperature history yet. Data will appear after the logging cron job runs.</p>
</div>
</div>

<div class="card mb-3">
<div class="card-header"><h5 class="mb-0">Monthly Statistics</h5></div>
<div class="card-body">
<div class="table-responsive">
<table class="table table-sm table-striped table-hover mb-0">
<thead><tr><th>Month</th><th>Avg (&deg;F / &deg;C)</th><th>Min (&deg;F / &deg;C)</th><th>Max (&deg;F / &deg;C)</th><th>Hours &le; 48&deg;F</th></tr></thead>
<tbody>
$stats_rows
</tbody>
</table>
</div>
</div>
</div>
EOF
fi

cat << EOF

<div class="card mb-3">
<div class="card-header"><h5 class="mb-0">Schedule</h5></div>
<div class="card-body">
$sched_msg
<form action="switch.sh" method="GET" class="row g-2 align-items-end">
  <div class="col-auto">
    <label for="sched_dt" class="form-label mb-0 small">Date &amp; Time</label>
    <input type="datetime-local" id="sched_dt" name="sched_dt" class="form-control form-control-sm" required min="$(date '+%Y-%m-%dT%H:%M')">
  </div>
  <div class="col-auto">
    <label for="sched_action" class="form-label mb-0 small">Action</label>
    <select id="sched_action" name="sched_action" class="form-select form-select-sm">
      <option value="1">Turn ON</option>
      <option value="0">Turn OFF</option>
    </select>
  </div>
  <div class="col-auto">
    <button type="submit" class="btn btn-primary btn-sm">Schedule</button>
  </div>
</form>
EOF

if [[ -n "$pending_schedules" ]]; then
cat << EOF
<hr>
<h6>Pending</h6>
<div class="table-responsive">
<table class="table table-sm table-striped mb-0">
<thead><tr><th>Time</th><th>Action</th><th></th></tr></thead>
<tbody>
$pending_schedules
</tbody>
</table>
</div>
EOF
fi

cat << EOF
<p class="text-muted small mb-0 mt-2">Schedules do not survive reboot.</p>
</div>
</div>

</div>
EOF

if [[ "$enable_temp" == "yes" ]]; then
cat << EOF
<script>
var data = [$chart_data];
var heaterRanges = [$heater_data];
var coldRanges = [$cold_data];
var heaterData = data.map(function(d) {
  var bucketEnd = d.x + 900000;
  for (var j = 0; j < heaterRanges.length; j++) {
    if (d.x < heaterRanges[j].xMax && bucketEnd > heaterRanges[j].xMin) return {x: d.x, y: d.y};
  }
  return {x: d.x, y: null};
});
for (var i = 0; i < heaterData.length; i++) {
  if (heaterData[i].y !== null) {
    var hasPrev = i > 0 && heaterData[i-1].y !== null;
    var hasNext = i < heaterData.length - 1 && heaterData[i+1].y !== null;
    if (!hasPrev && !hasNext) {
      if (i < heaterData.length - 1) heaterData[i+1] = {x: data[i+1].x, y: data[i+1].y};
      else if (i > 0) heaterData[i-1] = {x: data[i-1].x, y: data[i-1].y};
    }
  }
}
var annotations = {};
coldRanges.forEach(function(r, i) {
  annotations['c' + i] = {
    type: 'box',
    xMin: r.xMin,
    xMax: r.xMax,
    backgroundColor: 'rgba(255, 152, 0, 0.15)',
    borderWidth: 0
  };
});
if (data.length > 0) {
  var tempsF = data.map(function(d) { return d.y; });
  var minF = Math.min.apply(null, tempsF);
  var maxF = Math.max.apply(null, tempsF);
  var padF = Math.max((maxF - minF) * 0.1, 1);
  minF = Math.floor(minF - padF);
  maxF = Math.ceil(maxF + padF);
  var minC = Math.floor((minF - 32) / 1.8);
  var maxC = Math.ceil((maxF - 32) / 1.8);
  var tempChart = new Chart(document.getElementById('tempChart'), {
    type: 'line',
    data: {
      datasets: [{
        data: data,
        borderColor: 'rgb(75, 192, 192)',
        backgroundColor: 'rgba(75, 192, 192, 0.2)',
        fill: true,
        pointRadius: 0,
        tension: 0.3
      }, {
        data: heaterData,
        borderColor: 'transparent',
        backgroundColor: 'rgba(220, 53, 69, 0.25)',
        fill: 'origin',
        pointRadius: 0,
        tension: 0.3,
        spanGaps: false
      }]
    },
    options: {
      responsive: true,
      plugins: {
        legend: { display: false },
        annotation: { drawTime: 'beforeDatasetsDraw', annotations: annotations },
        zoom: {
          zoom: { wheel: { enabled: true }, pinch: { enabled: true }, mode: 'x' },
          pan: { enabled: true, mode: 'x' }
        }
      },
      scales: {
        x: {
          type: 'time',
          time: { unit: 'day', displayFormats: { day: 'MMM d' } }
        },
        y: {
          position: 'left',
          min: minF, max: maxF,
          title: { display: true, text: '\u00B0F' }
        },
        y1: {
          position: 'right',
          min: minC, max: maxC,
          grid: { drawOnChartArea: false },
          title: { display: true, text: '\u00B0C' }
        }
      }
    }
  });
  document.getElementById('resetZoom').style.display = '';
} else {
  document.getElementById('tempChart').style.display = 'none';
  document.getElementById('noChartData').style.display = 'block';
}
</script>
EOF
fi

cat << EOF
</body>
</html>
EOF
