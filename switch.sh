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
gpio_file="/sys/class/gpio/gpio$gpio_pin/"
gpio_value="$gpio_file/value"

# Only write to GPIO if state is exactly "0" or "1"
if [[ "$state" == "0" || "$state" == "1" ]]; then
  echo "$state" > "$gpio_value"
fi

# Read the value from the GPIO file
value=$(cat "$gpio_value")

echo -e "Content-type: text/html\r\n\r\n"

# Determine status text
if [[ $value -eq 0 ]]; then
  status="off"
else
  status="on"
fi

cat << EOF
<html>
<head>
<title>Airplane Hanger Heater Control</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet" integrity="sha384-T3c6CoIi6uLrA9TneNEoa7RxnatzjcDSCmG1MXxSR1GAsXEV/Dwwykc2MPK8M2HN" crossorigin="anonymous">
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js" integrity="sha384-C6RzsynM9kWDrMNeT87bh95OGNyZPhcTNXj1NW7RuBCsyN/o0jlpcV8Qyq46cDfL" crossorigin="anonymous"></script>
</head>
<body>
<div class="card">
<div class="card-header">
  <h4>Airplane Hanger Heater Control</h4>
</div>
<div class="card-body">
<h5 class="card-title">Heater Status: <b>$status</b></h5>
<p>
<form action="test.sh" method="GET">
<button type="submit" name="state" class="btn btn-success btn-lg" value="1">turn on</button>
</p><p><button name="state" class="btn btn-danger btn-lg" value="0">turn off</button>
</form></p>
</div>
<p>$(date)</p>
</body>
</html>
EOF
