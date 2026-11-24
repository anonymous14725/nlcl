"""Benchmark corpus: intents with a canonical sentence, training paraphrases and HELD-OUT test paraphrases,
plus out-of-scope / nonsense sentences for which the right behaviour is to NOT execute (ask instead).

The paraphrases were written as different people would phrase the request; they were not filtered by
whether the parser already understands them.  Test paraphrases are never used for training.
"""
from __future__ import annotations

HERO = ["create a game player called hero with health 100"]
STORE = ["create a python service called store with stock 10 and price 5"]
BOARD = ["add an esp32 called board", "add a led called lamp to board on pin 2",
         "add a temperature sensor called temp to board with temperature 20"]
FSM = ["create a hardware state machine called door with states closed, open and locked"]
APP = ['create an app called tally with title "Tally"', "add a label called display to tally with value 0",
       'add a button called plus to tally with text "+1"']
TWIN = ["create a 3d cube called box with color red"]

INTENTS = [
    # ------------------------------------------------------------------ creation
    dict(id="mk_player", context=[], gold=HERO,
         train=["make a game player named hero with 100 health",
                "I want a game player called hero that has 100 health",
                "spawn a game player called hero, health 100"],
         test=["please create a player for the game called hero with a health of 100",
               "new game player hero with health 100",
               "build a game player called hero and give it 100 health"]),
    dict(id="mk_enemies", context=[], gold=["create 3 game enemies with health 30"],
         train=["make three game enemies with 30 health",
                "spawn 3 game enemies having health 30",
                "add 3 enemies to the game with health 30"],
         test=["create three game enemies, each with health 30",
               "I need 3 game enemies with 30 health",
               "generate 3 game foes with health 30"]),
    dict(id="mk_service", context=[], gold=STORE,
         train=["make a python service named store with 10 stock and price 5",
                "create a store service in python with stock 10 and price 5",
                "new python service called store, stock 10, price 5"],
         test=["I'd like a python service called store with a stock of 10 and a price of 5",
               "set up a python service called store with stock 10 and price 5",
               "please make a python service store with stock 10 and price 5"]),
    dict(id="mk_board", context=[], gold=["add an esp32 called board"],
         train=["create an esp32 named board", "I need an esp32 called board", "make an esp32 board called board"],
         test=["add a new esp32 called board", "put an esp32 called board in the project", "get an esp32 named board"]),
    dict(id="mk_led", context=["add an esp32 called board"], gold=["add a led called lamp to board on pin 2"],
         train=["connect a led called lamp to board on pin 2", "create a led named lamp on board at pin 2",
                "add an led called lamp to the board on pin 2"],
         test=["put a led called lamp on the board using pin 2", "attach a led named lamp to board on pin 2",
               "the board needs a led called lamp on pin 2"]),
    dict(id="mk_fsm", context=[], gold=FSM,
         train=["make a hardware state machine named door with states closed, open and locked",
                "create a state machine in hardware called door with the states closed, open, locked",
                "new hardware state machine door with states closed, open and locked"],
         test=["build a hardware state machine called door having states closed, open and locked",
               "create a hardware fsm called door with states closed, open and locked",
               "define a hardware state machine door whose states are closed, open and locked"]),
    dict(id="mk_cube", context=[], gold=TWIN,
         train=["make a red 3d cube called box", "create a 3d cube named box with red color",
                "add a 3d cube called box, color red"],
         test=["create a red cube in 3d called box", "new 3d cube box colored red",
               "I want a 3d cube called box that is red"]),
    dict(id="mk_enclosure", context=[], gold=["create a cad enclosure called case with wall 2"],
         train=["make a cad enclosure named case with wall 2", "create an enclosure in cad called case, wall 2",
                "new cad enclosure case with a wall of 2"],
         test=["design a cad enclosure called case with wall 2", "create a cad enclosure called case with 2 mm walls",
               "I need a cad enclosure named case whose wall is 2"]),
    dict(id="mk_app", context=[], gold=['create an app called tally with title "Tally"'],
         train=['make an app named tally with title "Tally"', 'create a new app called tally titled "Tally"',
                'I want an app called tally with the title "Tally"'],
         test=['build an application called tally with title "Tally"', 'create an app tally with title "Tally"',
               'please create an app named tally whose title is "Tally"']),
    # ------------------------------------------------------------------ properties
    dict(id="set_speed", context=HERO, gold=["set speed of hero to 20"],
         train=["give hero 20 speed", "hero's speed should be 20", "change the speed of hero to 20"],
         test=["make hero's speed 20", "the speed of hero is 20", "set hero speed to 20"]),
    dict(id="set_health", context=HERO, gold=["set health of hero to 50"],
         train=["set hero's hp to 50", "change hero's health to 50", "give hero 50 health"],
         test=["hero's health should be 50", "update the health of hero to 50", "set the hit points of hero to 50"]),
    dict(id="set_color", context=TWIN, gold=["set color of box to blue"],
         train=["make box blue", "change the color of box to blue", "box's color should be blue"],
         test=["paint box blue", "set the colour of box to blue", "turn box blue"]),
    dict(id="set_movable", context=HERO, gold=["make hero movable"],
         train=["hero should be movable", "make the hero movable", "let hero move"],
         test=["allow hero to move", "hero must be movable", "set hero movable"]),
    dict(id="remove_prop", context=HERO + ["set speed of hero to 20"], gold=["remove speed from hero"],
         train=["delete the speed of hero", "remove hero's speed", "take the speed off hero"],
         test=["get rid of hero's speed", "drop speed from hero", "erase the speed of hero"]),
    dict(id="rename", context=HERO, gold=["rename hero to knight"],
         train=["call hero knight", "change hero's name to knight", "rename the hero to knight"],
         test=["hero should be called knight", "name hero knight", "change the name of hero to knight"]),
    dict(id="delete_entity", context=HERO, gold=["delete hero"],
         train=["remove hero", "destroy the hero", "get rid of hero"],
         test=["erase hero", "remove the hero from the game", "throw away hero"]),
    # ------------------------------------------------------------------ behaviours
    dict(id="beh_die", context=HERO, gold=["when health of hero is below 1, destroy hero"],
         train=["when hero's health drops below 1, destroy it", "destroy hero when its health is below 1",
                "if hero's health is less than 1, destroy hero"],
         test=["when the health of hero goes below 1 destroy hero", "hero dies when its health is under 1",
               "kill hero once its health falls below 1"]),
    dict(id="beh_timer", context=STORE, gold=["every 2 seconds, decrease stock of store by 1"],
         train=["every two seconds, reduce store's stock by 1", "decrease the stock of store by 1 every 2 seconds",
                "every 2 seconds lower stock of store by 1"],
         test=["each 2 seconds, decrease store's stock by 1", "every 2000 ms, decrease stock of store by 1",
               "take 1 from the stock of store every 2 seconds"]),
    dict(id="beh_key", context=HERO, gold=["when space is pressed, hero jumps"],
         train=["when the player presses space, hero jumps", "hero jumps when space is pressed",
                "if space is pressed, hero jumps"],
         test=["when I press space, hero jumps", "pressing space makes hero jump", "on space, hero jumps"]),
    dict(id="beh_button", context=APP, gold=["when plus is pressed, increase value of display by 1"],
         train=["when plus is clicked, increase value of display by 1", "if plus is pressed, add 1 to value of display",
                "increase display's value by 1 when plus is pressed"],
         test=["clicking plus adds 1 to the value of display", "when plus is pressed, raise value of display by 1",
               "when the plus button is pressed, increment value of display by 1"]),
    dict(id="beh_sensor", context=BOARD, gold=["when temperature of temp is above 30, turn on lamp"],
         train=["when temp's temperature is above 30, turn on lamp", "turn on lamp when temperature of temp is above 30",
                "if the temperature of temp is greater than 30, turn on lamp"],
         test=["when temp reads more than 30 degrees, switch on lamp", "when temperature of temp exceeds 30, turn lamp on",
               "lamp should turn on when temp's temperature is over 30"]),
    dict(id="beh_fsm", context=FSM, gold=["every 3 seconds, if state of door is closed, set state of door to open"],
         train=["every 3 seconds, if door's state is closed, set door's state to open",
                "every three seconds, when state of door is closed, set state of door to open",
                "every 3 s, if state of door is closed, set door state to open"],
         test=["each 3 seconds, if the door is closed, open it", "every 3 seconds, change door's state from closed to open",
               "every 3 seconds if state of door equals closed set state of door to open"]),
    # ------------------------------------------------------------------ links
    dict(id="connect", context=BOARD + ["create a python dashboard called monitor"],
         gold=["connect temp to monitor via http sending temperature every 2 seconds"],
         train=["link temp to monitor via http sending temperature every 2 seconds",
                "connect temp to monitor over http, sending temperature every 2 seconds",
                "send temperature from temp to monitor via http every 2 seconds"],
         test=["wire temp to monitor via http sending temperature every 2 seconds",
               "hook up temp to monitor over http sending temperature every 2 seconds",
               "connect the temp sensor to monitor via http, sending temperature every 2 seconds"]),
    dict(id="parent", context=["create a cpp object called robot", "create a cpp object called wheel"],
         gold=["put wheel inside robot"],
         train=["make wheel a child of robot", "wheel is part of robot", "place wheel in robot"],
         test=["attach wheel to robot", "wheel belongs inside robot", "move wheel into robot"]),
    # ------------------------------------------------------------------ terminal
    dict(id="t_list_py", context=[], gold=["list all python files in src"],
         train=["show me the python files in src", "list the .py files in src", "what python files are in src"],
         test=["display all python files in the folder src", "ls the python files in src", "which python files are in src"]),
    dict(id="t_count_logs", context=[], gold=["count the log files in logs"],
         train=["how many log files are in logs", "how many .log files are there in logs", "number of log files in logs"],
         test=["count all .log files in logs", "how many log files does logs have", "tell me how many log files are in logs"]),
    dict(id="t_mkdir", context=[], gold=["create a folder called backup"],
         train=["make a new folder named backup", "make a directory called backup", "mkdir backup"],
         test=["create a new directory named backup", "add a folder called backup", "I need a folder called backup"]),
    dict(id="t_delete_tmp", context=[], gold=["delete all .tmp files recursively"],
         train=["remove all temporary files recursively", "delete every .tmp file including subfolders",
                "get rid of all tmp files recursively"],
         test=["erase all .tmp files in all subfolders", "clean up the temporary files everywhere",
               "wipe all .tmp files recursively"]),
    dict(id="t_copy", context=[], gold=["copy notes.txt to backup.txt"],
         train=["duplicate notes.txt to backup.txt", "make a copy of notes.txt called backup.txt", "cp notes.txt to backup.txt"],
         test=["copy the file notes.txt to backup.txt", "copy notes.txt into backup.txt", "clone notes.txt to backup.txt"]),
    dict(id="t_rename", context=[], gold=["rename data.csv to people.csv"],
         train=["rename the file data.csv to people.csv", "move data.csv to people.csv", "change the name of data.csv to people.csv"],
         test=["mv data.csv to people.csv", "rename data.csv as people.csv", "data.csv should be renamed to people.csv"]),
    dict(id="t_head", context=[], gold=["show the first 2 lines of notes.txt"],
         train=["print the first 2 lines of notes.txt", "display the top 2 lines of notes.txt", "first 2 lines of notes.txt"],
         test=["show me the first 2 lines in notes.txt", "head 2 lines of notes.txt", "read the first 2 lines of notes.txt"]),
    dict(id="t_grep", context=[], gold=["search for 'TODO' in all python files"],
         train=["find 'TODO' in all python files", "which python files contain 'TODO'", "look for 'TODO' in the python files"],
         test=["grep 'TODO' in python files", "search all python files for 'TODO'", "where does 'TODO' appear in python files"]),
    dict(id="t_lines", context=[], gold=["count the lines in data.csv"],
         train=["how many lines are in data.csv", "number of lines in data.csv", "count lines of data.csv"],
         test=["how many lines does data.csv have", "line count of data.csv", "tell me the number of lines in data.csv"]),
    dict(id="t_replace", context=[], gold=["replace 'apple' with 'pear' in notes.txt"],
         train=["replace all 'apple' with 'pear' in notes.txt", "replace the word 'apple' with 'pear' in notes.txt",
                "in notes.txt replace 'apple' with 'pear'"],
         test=["change 'apple' to 'pear' in notes.txt", "swap 'apple' for 'pear' in notes.txt",
               "replace every 'apple' by 'pear' in the file notes.txt"]),
    dict(id="t_zip", context=[], gold=["zip the folder src into src.zip"],
         train=["compress src into src.zip", "archive the folder src as src.zip", "zip src to src.zip"],
         test=["make a zip of src called src.zip", "pack src into src.zip", "compress the folder src to src.zip"]),
]

# Out-of-scope or meaningless: the right behaviour is to NOT execute (ask).
INVALID_TRAIN = ["flibber the wobble", "frobnicate hero quickly", "the weather is nice today", "tell me a joke",
                 "blorp", "make it better", "do the thing", "sing a song about the board", "why is the sky blue",
                 "hero sparkle mode engage", "zap zap", "optimize everything magically"]
INVALID_TEST = ["glorp the snizzle", "what time is it", "make it awesome", "please do something cool",
                "hero feels sad", "the quick brown fox", "fix my life", "teleport hero to mars instantly",
                "wobble wobble", "do what I mean", "turn the frobnitz", "be smarter"]


def build_examples(extended: bool = True):
    import copy
    from ..learn import Example
    from .corpus2 import extend
    intents, inv_tr, inv_te = copy.deepcopy(INTENTS), list(INVALID_TRAIN), list(INVALID_TEST)
    if extended:
        extend(intents, inv_tr, inv_te)
    out = []
    for it in intents:
        for split in ("train", "test"):
            for i, text in enumerate(it[split]):
                out.append(Example(id=f"{it['id']}:{split}{i}", text=text, split=split, context=list(it["context"]),
                                   gold=list(it["gold"]), valid=True, intent=it["id"], holdout=bool(it.get("holdout"))))
        out.append(Example(id=f"{it['id']}:gold", text=it["gold"][-1], split="gold", context=list(it["context"]) + it["gold"][:-1],
                           gold=list(it["gold"]), valid=True, intent=it["id"]))
    ctx = HERO
    for i, t in enumerate(inv_tr):
        out.append(Example(id=f"invalid:train{i}", text=t, split="train", context=ctx, gold=[], valid=False, intent="invalid"))
    for i, t in enumerate(inv_te):
        out.append(Example(id=f"invalid:test{i}", text=t, split="test", context=ctx, gold=[], valid=False, intent="invalid"))
    return out
