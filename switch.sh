#!/bin/bash

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
gpio_pin="17"
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
  toggle_btn='<button type="submit" name="state" class="btn btn-success btn-lg" value="1">turn on</button>'
else
  status="on"
  toggle_btn='<button type="submit" name="state" class="btn btn-danger btn-lg" value="0">turn off</button>'
fi

# Read temperature from DS18B20 1-wire probe
w1_device=$(echo /sys/bus/w1/devices/28-*/w1_slave)
if [[ ! -f "$w1_device" ]]; then
  temp_display="Temperature probe not found"
elif ! head -1 "$w1_device" | grep -q "YES$"; then
  temp_display="Temperature read error"
else
  temp_display=$(awk -F 't=' '/t=/{c=$2/1000; f=(c*1.8)+32; printf "%.1f &deg;C | %.1f &deg;F", c, f}' "$w1_device")
fi

echo -e "Content-type: text/html\r\n\r\n"
cat << EOF
<html>
<head>
<title>Airplane Hanger Heater Control</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.8/dist/css/bootstrap.min.css" rel="stylesheet" integrity="sha384-sRIl4kxILFvY47J16cr9ZwB07vP4J8+LH7qKQnuqkuIAvNWLzeN8tE5YBujZqJLB" crossorigin="anonymous">
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.8/dist/js/bootstrap.bundle.min.js" integrity="sha384-FKyoEForCGlyvwx9Hj09JcYn3nv7wiPVlz7YYwJrWVcXK/BmnVDxM+D2scQbITxI" crossorigin="anonymous"></script>
</head>
<body>
<div class="card">
<div class="card-header">
  <h4>Airplane Hanger Heater Control</h4>
</div>
<div class="card-body">
<h5 class="card-title">Heater Status: <b>$status</b></h5>
<form action="switch.sh" method="GET">
<p>$toggle_btn</p>
</form>
</div>
<div class="card-footer text-body-secondary">
  Current temp: $temp_display<br>
  $(date)
</div>
</div>
</body>
</html>
EOF
