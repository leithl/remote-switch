#!/bin/bash

ram_csv="/run/heater-temp.csv"
disk_csv="/var/lib/heater-temp.csv"

# Flush mode: persist RAM data to disk and clear RAM
if [[ "$1" == "flush" ]]; then
  if [[ -s "$ram_csv" ]]; then
    cat "$ram_csv" >> "$disk_csv"
    : > "$ram_csv"
  fi
  exit 0
fi

# Read temperature from DS18B20 1-wire probe
w1_device=$(echo /sys/bus/w1/devices/28-*/w1_slave)
[[ ! -f "$w1_device" ]] && exit 0
head -1 "$w1_device" | grep -q "YES$" || exit 0
temp_c=$(awk -F 't=' '/t=/{printf "%.1f", $2/1000}' "$w1_device")
echo "$(date +%s),$temp_c" >> "$ram_csv"
