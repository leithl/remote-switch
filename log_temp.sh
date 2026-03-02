#!/bin/bash

gpio_pin="17"
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

# Read heater state from GPIO
gpio_value="/sys/class/gpio/gpio$gpio_pin/value"
heater_state=0
[[ -f "$gpio_value" ]] && heater_state=$(< "$gpio_value")

# Read temperature from DS18B20 1-wire probe (optional)
temp_c=""
w1_device=$(echo /sys/bus/w1/devices/28-*/w1_slave)
if [[ -f "$w1_device" ]] && head -1 "$w1_device" | grep -q "YES$"; then
  temp_c=$(awk -F 't=' '/t=/{printf "%.1f", $2/1000}' "$w1_device")
fi
echo "$(date +%s),$temp_c,$heater_state" >> "$ram_csv"
