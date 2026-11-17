# A multi-module system described in natural language:
# ESP32 firmware + FPGA fan PWM + Python monitor + 3D digital twin + 3D-printed enclosure.
:name greenhouse
add an esp32 called board
add a temperature sensor called temp to the board on pin 4 with temperature 20
add a fan called fan to the board on pin 5
add a button called btn to the board on pin 0
when temperature is above 30, turn on the fan
when temperature is below 25, turn off the fan
when the btn is pressed, toggle the fan

create a python dashboard called monitor with threshold 30
connect temp to monitor via http sending temperature every 2 seconds
when temperature is above threshold, print 'ALERT: greenhouse at {temperature} C'

create a 3d tank called twin with color green
connect monitor to twin via http sending temperature

create a hardware pwm called fan_pwm with duty 0 and width 8
connect board to fan_pwm via spi sending duty

create an enclosure called case for the board with wall 2
build and run it
