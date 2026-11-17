# Hardware description in natural language (Verilog + testbench)
:name blinky
use hardware
create a counter called tick with width 8
create a register called leds with width 4
every 500 ms, increase leds by 1
when leds is 15, set leds to 0
connect tick to leds via wire
build and run it
