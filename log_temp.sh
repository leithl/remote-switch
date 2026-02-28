#!/bin/bash

# Configuration
gpio_pin="17"
notify_email=""  # set to an email address for monthly summary emails (requires msmtp)

ram_csv="/run/heater-temp.csv"
disk_csv="/var/lib/heater-temp.csv"
chart_dir="/var/lib/heater-chart"
sched_csv="/run/heater-schedule.csv"

# Flush mode: persist RAM data to disk and clear RAM
if [[ "$1" == "flush" ]]; then
  if [[ -s "$ram_csv" ]]; then
    cat "$ram_csv" >> "$disk_csv"
    : > "$ram_csv"
  fi
  exit 0
fi

# Rollup mode: pre-compute previous month's chart data and stats
if [[ "$1" == "rollup" ]]; then
  prev_month=$(date -d "$(date +%Y-%m-01) -1 month" +%Y-%m)
  prev_start=$(date -d "${prev_month}-01" +%s)
  prev_end=$(date -d "$(date +%Y-%m-01)" +%s)
  prev_label=$(date -d "${prev_month}-01" +"%b %Y")
  possible=$(( (prev_end - prev_start) / 60 ))

  mkdir -p "$chart_dir"

  # Single-pass awk: compute chart data, temp stats, and runtime stats
  awk_output=$(cat "$disk_csv" "$ram_csv" 2>/dev/null | awk -F, \
    -v m_start="$prev_start" \
    -v m_end="$prev_end" \
    -v m_label="$prev_label" \
    -v possible="$possible" \
  'BEGIN { sep = "" }
  {
    epoch = $1 + 0
    if (epoch == 0 || epoch < m_start + 0 || epoch >= m_end + 0) next

    # Temperature stats (skip rows with no temp)
    if ($2 != "") {
      temp = $2 + 0
      tsum += temp; tcnt++
      if (tcnt == 1 || temp < tmin) tmin = temp
      if (tcnt == 1 || temp > tmax) tmax = temp

      # Chart: 15-min buckets
      b = int(epoch / 900) * 900
      if (b != prev_b && prev_b != "") {
        f = (bsum / bcnt) * 1.8 + 32
        chart = chart sep sprintf("{x:%d000,y:%.1f}", prev_b, f)
        sep = ","
      }
      if (b != prev_b) { bsum = 0; bcnt = 0; prev_b = b }
      bsum += temp; bcnt++

      # Cold tracking (temp <= 48°F / 8.89°C)
      if (temp <= 8.89) {
        cold_mins++
        if (!in_cold) { cold_start = epoch; in_cold = 1 }
        cold_last = epoch
      } else if (in_cold) {
        cranges = cranges csep sprintf("{xMin:%d000,xMax:%d000}", cold_start, cold_last + 60)
        csep = ","
        in_cold = 0
      }
    }

    # Runtime stats + heater on/off ranges
    if ($3 != "") {
      rcnt++
      if ($3 + 0 == 1) {
        on_mins += 1
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
    if (bcnt > 0) {
      f = (bsum / bcnt) * 1.8 + 32
      chart = chart sep sprintf("{x:%d000,y:%.1f}", prev_b, f)
    }
    print "chart|" chart

    # Temp stats row
    if (tcnt > 0) {
      pct = (possible > 0) ? (tcnt / possible) * 100 : 0
      lbl = (pct >= 100) ? m_label : sprintf("%s (%.1f%%)", m_label, pct)
      avg_c = tsum / tcnt; avg_f = avg_c * 1.8 + 32
      min_f = tmin * 1.8 + 32; max_f = tmax * 1.8 + 32
      cold_hrs = cold_mins / 60
      printf "temp|<tr><td>%s</td><td>%.1f / %.1f</td><td>%.1f / %.1f</td><td>%.1f / %.1f</td><td>%.1f</td></tr>\n", \
        lbl, avg_f, avg_c, min_f, tmin, max_f, tmax, cold_hrs
    }

    # Runtime stats row
    if (rcnt > 0) {
      pct = (possible > 0) ? (rcnt / possible) * 100 : 0
      lbl = (pct >= 100) ? m_label : sprintf("%s (%.1f%%)", m_label, pct)
      hours = on_mins / 60
      days = (m_end + 0 - m_start + 0) / 86400
      avg = (days > 0) ? hours / days : 0
      printf "runtime|<tr><td>%s</td><td>%.1f</td><td>%.1f</td></tr>\n", lbl, hours, avg
    }

    # Email-friendly plain text stats
    if (tcnt > 0) {
      printf "email_temp|Avg: %.1f°F / %.1f°C, Min: %.1f°F / %.1f°C, Max: %.1f°F / %.1f°C\n", \
        avg_f, avg_c, min_f, tmin, max_f, tmax
      printf "email_cold|%.1f hours at or below 48°F\n", cold_hrs
    }
    if (rcnt > 0) {
      printf "email_runtime|Total: %.1f hours, Avg: %.1f hours/day\n", hours, avg
    }
    if (tcnt > 0 || rcnt > 0) {
      t_pct = (tcnt > 0 && possible > 0) ? (tcnt / possible) * 100 : 0
      r_pct = (rcnt > 0 && possible > 0) ? (rcnt / possible) * 100 : 0
      printf "email_coverage|Temp: %.1f%%, Heater: %.1f%%\n", t_pct, r_pct
    }

    # Heater on/off ranges for chart annotation
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

  # Write .dat file (chart, temp, runtime lines only)
  echo "$awk_output" | grep -E '^(chart|temp|runtime|heater|cold)\|' > "$chart_dir/$prev_month.dat"

  # Send email summary if configured
  if [[ -n "$notify_email" ]] && command -v msmtp &>/dev/null; then
    email_temp=$(echo "$awk_output" | grep '^email_temp|' | cut -d'|' -f2)
    email_cold=$(echo "$awk_output" | grep '^email_cold|' | cut -d'|' -f2)
    email_runtime=$(echo "$awk_output" | grep '^email_runtime|' | cut -d'|' -f2)
    email_coverage=$(echo "$awk_output" | grep '^email_coverage|' | cut -d'|' -f2)

    msmtp "$notify_email" << MAIL
Subject: Heater Monthly Summary - $prev_label
To: $notify_email

Heater Monthly Summary: $prev_label
$(printf '=%.0s' {1..40})

Temperature:
  ${email_temp:-No data}
  ${email_cold:+Cold: $email_cold}

Heater Runtime:
  ${email_runtime:-No data}

Data Coverage:
  ${email_coverage:-No data}
MAIL
  fi

  exit 0
fi

# Read heater state from GPIO
gpio_value="/sys/class/gpio/gpio$gpio_pin/value"
heater_state=0
[[ -f "$gpio_value" ]] && heater_state=$(< "$gpio_value")

# Ensure schedule file exists with correct permissions
if [[ ! -f "$sched_csv" ]]; then
  touch "$sched_csv"
  chown www-data:gpio "$sched_csv" 2>/dev/null
  chmod 0664 "$sched_csv"
fi

# Execute due schedules
if [[ -s "$sched_csv" ]]; then
  now_epoch=$(date +%s)
  sched_tmp="${sched_csv}.tmp"
  (
    flock -x 200
    while IFS=, read -r sid sepoch saction; do
      [[ -z "$sid" || -z "$sepoch" || -z "$saction" ]] && continue
      if (( sepoch <= now_epoch )); then
        if [[ "$saction" == "0" || "$saction" == "1" ]] && [[ -f "$gpio_value" ]]; then
          echo "$saction" > "$gpio_value"
          heater_state="$saction"
        fi
      else
        echo "$sid,$sepoch,$saction" >> "$sched_tmp"
      fi
    done < "$sched_csv"
    if [[ -f "$sched_tmp" ]]; then
      cat "$sched_tmp" > "$sched_csv"
      rm -f "$sched_tmp"
    else
      : > "$sched_csv"
    fi
  ) 200>"${sched_csv}.lock"
fi

# Read temperature from DS18B20 1-wire probe (optional)
temp_c=""
w1_device=$(echo /sys/bus/w1/devices/28-*/w1_slave)
if [[ -f "$w1_device" ]] && head -1 "$w1_device" | grep -q "YES$"; then
  temp_c=$(awk -F 't=' '/t=/{printf "%.1f", $2/1000}' "$w1_device")
fi
echo "$(date +%s),$temp_c,$heater_state" >> "$ram_csv"
