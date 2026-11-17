# The interaction from the proposal, then a small playable game.
:name arena
create an object
a game object called hero
give it 100 health, make it movable, and add collision detection
set speed to 5 and score 0
color blue
when health drops below 1, destroy it
when space is pressed, jump
every 2 seconds, print 'hero health {health}, score {score}'
create 3 enemies with health 30 and color red
when an enemy touches hero, decrease hero health by 10
create a coin called gold with value 10
when hero touches gold, increase hero score by 10 and destroy gold
build and run it
