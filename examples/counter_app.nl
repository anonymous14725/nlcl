# A desktop + mobile application described in natural language.
# Output: app.py (Windows/macOS/Linux, Tkinter) and web/ (installable PWA for Android/iOS).
:name tally
create an app called tally with title "Tally Counter" and size 360 by 320
add a label called display to tally with value 0
add a button called plus to tally with text "+1"
add a button called reset to tally with text "Reset"
add an input called step to tally with value 1 and label "Step"
add a checkbox called auto to tally with text "Count automatically"
add a progress called bar to tally with max 20
when plus is pressed, increase value of display by value of step
when reset is pressed, set value of display to 0
every 1 second, if checked of auto is on, increase value of display by 1
when value of display changes, set value of bar to value of display
when value of display is above 19, print 'limit reached: {value}'
build it and run it
