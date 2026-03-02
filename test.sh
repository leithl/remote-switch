#!/bin/bash

# https://gist.github.com/pablochacin/8016575?permalink_comment_id=4450245
# the following one liner creates a shell variable from every parameter in a
# the query string in the variable QUERY, of the form p1=v1&p2=v2,... and sets it to
# the corresponding value so that parameters can be accessed by its name $p1, $p2, ...
for p in ${QUERY_STRING//&/ };do kvp=( ${p/=/ } ); k=${kvp[0]};v=${kvp[1]};eval $k=$v;done

# Specify the GPIO file path
gpio_file="/sys/class/gpio/gpio17/"
gpio_value="$gpio_file/value"

# Check for the "state" variable
if [[ -v state ]]; then
  # Set the GPIO value to the "state" variable
  echo "$state" > "$gpio_value"
fi

# Read the value from the GPIO file
value=$(cat "$gpio_value")

echo -e "Content-type: text/html\r\n\r\n"
echo "<html>"
echo "<head>"
echo "<title>Airplane Hanger Heater Control</title>"
echo "</head>"
echo "<body>"
echo "<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet" integrity="sha384-T3c6CoIi6uLrA9TneNEoa7RxnatzjcDSCmG1MXxSR1GAsXEV/Dwwykc2MPK8M2HN" crossorigin="anonymous">
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js" integrity="sha384-C6RzsynM9kWDrMNeT87bh95OGNyZPhcTNXj1NW7RuBCsyN/o0jlpcV8Qyq46cDfL" crossorigin="anonymous"></script>"
echo "<div class="card">"
echo "<div class="card-header">"
echo "<h4>Airplane Hanger Heater Control</h4>"
echo "</div>"

#echo "<p>state: $state</p>"
#echo "<p>value: $value</p>"
#echo "<p>gpio: $gpio_file</p>"

echo "<div class="card-body">"
echo "<h5 class="card-title">Heater Status: "
# Check the value and print the state
  if [[ $value -eq 0 ]]; then
    echo "<b>off</b>"
  else
    echo "<b>on</b>"
  fi
echo "</h5>"

echo "<p>
<form action="test.sh" method="GET">
<button type="submit" name="state" class=\"btn btn-success btn-lg\" value="1">turn on</button>
</p><p><button name="state" class=\"btn btn-danger btn-lg\" value="0">turn off</button>
</form></p>
"
echo "</div>"
echo "<p>"
echo `date`
echo "</p>"
echo "</body>"
echo "</html>"
