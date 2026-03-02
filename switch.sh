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
  fi
done

# Specify the GPIO file path
gpio_file="/sys/class/gpio/gpio$gpio_pin"
gpio_value="$gpio_file/value"

# Bail out early if the GPIO pin hasn't been exported
if [[ ! -f "$gpio_value" ]]; then
  echo -e "Content-type: text/plain\r\n\r\n"
  echo "Error: GPIO pin $gpio_pin is not configured ($gpio_value not found)"
  exit 1
fi

# Only write to GPIO if state is exactly "0" or "1"
if [[ "$state" == "0" || "$state" == "1" ]]; then
  echo "$state" > "$gpio_value"
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

# Shared time variables for stats processing
now=$(date +%s)
month_starts=""
month_labels=""
cms=$(date +%Y-%m-01)
for i in $(seq 12 -1 0); do
  ms=$(date -d "$cms -$i month" +%s)
  ml=$(date -d "$cms -$i month" +"%b %Y")
  month_starts="${month_starts:+$month_starts|}$ms"
  month_labels="${month_labels:+$month_labels|}$ml"
done
next_ms=$(date -d "$cms +1 month" +%s)
month_starts="$month_starts|$next_ms"

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

  # Process temperature history for chart and stats
  ram_csv="/run/heater-temp.csv"
  disk_csv="/var/lib/heater-temp.csv"
  seven_days_ago=$((now - 7 * 86400))

  # Single-pass awk: line 1 = chart JS data, lines 2+ = stats HTML rows
  awk_output=$(cat "$disk_csv" "$ram_csv" 2>/dev/null | awk -F, \
    -v cutoff="$seven_days_ago" \
    -v now="$now" \
    -v m_starts="$month_starts" \
    -v m_labels="$month_labels" \
  'BEGIN {
    nm = split(m_starts, starts, "|")
    split(m_labels, labels, "|")
    sep = ""
  }
  {
    epoch = $1 + 0
    if (epoch == 0 || $2 == "") next
    temp = $2 + 0

    # Chart: 15-min buckets for last 7 days
    if (epoch >= cutoff + 0) {
      b = int(epoch / 900) * 900
      if (b != prev_b && prev_b != "") {
        f = (bsum / bcnt) * 1.8 + 32
        chart = chart sep sprintf("{x:%d000,y:%.1f}", prev_b, f)
        sep = ","
      }
      if (b != prev_b) { bsum = 0; bcnt = 0; prev_b = b }
      bsum += temp; bcnt++
    }

    # Monthly stats
    for (i = 1; i < nm; i++) {
      if (epoch >= starts[i] + 0 && epoch < starts[i+1] + 0) {
        msum[i] += temp; mcnt[i]++
        if (!(i in mmin) || temp < mmin[i]) mmin[i] = temp
        if (!(i in mmax) || temp > mmax[i]) mmax[i] = temp
        break
      }
    }
  }
  END {
    # Flush last chart bucket
    if (bcnt > 0) {
      f = (bsum / bcnt) * 1.8 + 32
      chart = chart sep sprintf("{x:%d000,y:%.1f}", prev_b, f)
    }
    print chart

    # Stats rows (oldest first)
    for (i = 1; i < nm; i++) {
      if (mcnt[i] + 0 == 0) continue

      # Calculate data coverage: actual readings vs possible (1 per minute)
      if (i == nm - 1)
        possible = int((now + 0 - starts[i] + 0) / 60)
      else
        possible = int((starts[i+1] + 0 - starts[i] + 0) / 60)
      pct = (possible > 0) ? (mcnt[i] / possible) * 100 : 0
      lbl = (pct >= 100) ? labels[i] : sprintf("%s (%.1f%%)", labels[i], pct)

      avg_c = msum[i] / mcnt[i]; avg_f = avg_c * 1.8 + 32
      min_f = mmin[i] * 1.8 + 32; max_f = mmax[i] * 1.8 + 32
      printf "<tr><td>%s</td><td>%.1f / %.1f</td><td>%.1f / %.1f</td><td>%.1f / %.1f</td></tr>\n", \
        lbl, avg_f, avg_c, min_f, mmin[i], max_f, mmax[i]
    }
  }')

  chart_data=$(echo "$awk_output" | head -1)
  stats_rows=$(echo "$awk_output" | tail -n +2)
fi

# Process heater runtime stats from column 3 of temp CSV
runtime_rows=$(cat /var/lib/heater-temp.csv /run/heater-temp.csv 2>/dev/null | awk -F, \
  -v now="$now" \
  -v m_starts="$month_starts" \
  -v m_labels="$month_labels" \
'BEGIN {
  nm = split(m_starts, starts, "|")
  split(m_labels, labels, "|")
}
{
  epoch = $1 + 0; st = $3 + 0
  if (epoch == 0 || $3 == "") next

  for (i = 1; i < nm; i++) {
    if (epoch >= starts[i] + 0 && epoch < starts[i+1] + 0) {
      rcnt[i]++
      if (st == 1) secs[i] += 60
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

# === HTML Output ===
echo -e "Content-type: text/html\r\n\r\n"

cat << EOF
<html>
<head>
<title>Airplane Hanger Heater Control</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:,">
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.8/dist/css/bootstrap.min.css" rel="stylesheet" integrity="sha384-sRIl4kxILFvY47J16cr9ZwB07vP4J8+LH7qKQnuqkuIAvNWLzeN8tE5YBujZqJLB" crossorigin="anonymous">
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.8/dist/js/bootstrap.bundle.min.js" integrity="sha384-FKyoEForCGlyvwx9Hj09JcYn3nv7wiPVlz7YYwJrWVcXK/BmnVDxM+D2scQbITxI" crossorigin="anonymous"></script>
EOF

if [[ "$enable_temp" == "yes" ]]; then
cat << EOF
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-zoom@2"></script>
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
cat << EOF

<div class="card mb-3">
<div class="card-header"><h5 class="mb-0">Temperature - Last 7 Days</h5></div>
<div class="card-body">
  <p class="mb-2"><strong>Current:</strong> $temp_display</p>
  <canvas id="tempChart"></canvas>
  <button id="resetZoom" style="display:none" class="btn btn-sm btn-outline-secondary mt-2" onclick="tempChart.resetZoom()">Reset zoom</button>
  <p id="noChartData" style="display:none" class="text-muted mb-0">No temperature history yet. Data will appear after the logging cron job runs.</p>
</div>
</div>

<div class="card mb-3">
<div class="card-header"><h5 class="mb-0">Monthly Statistics</h5></div>
<div class="card-body">
<div class="table-responsive">
<table class="table table-sm table-striped mb-0">
<thead><tr><th>Month</th><th>Avg (&deg;F / &deg;C)</th><th>Min (&deg;F / &deg;C)</th><th>Max (&deg;F / &deg;C)</th></tr></thead>
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

</div>
EOF

if [[ "$enable_temp" == "yes" ]]; then
cat << EOF
<script>
var data = [$chart_data];
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
      }]
    },
    options: {
      responsive: true,
      plugins: {
        legend: { display: false },
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
