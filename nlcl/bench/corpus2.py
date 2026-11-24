"""100 further corpus sentences (added 2026-11-22).

* 10 intents with vague words (fuzzy meanings): 60 sentences. Four of these intents are HELD OUT
  entirely ("unseen intents"): none of their sentences is used for training, so the score on them
  measures generalisation to new meanings, not to new wordings of trained meanings.
* 30 additional paraphrases for 10 existing intents (10 training, 20 test).
* 10 additional out-of-scope / risky sentences (5 training, 5 test) that must NOT be executed.
Written as people phrase things; not filtered by whether the parser already understands them.
"""
from __future__ import annotations

HERO = ["create a game player called hero with health 100"]
HERO10 = HERO + ["set speed of hero to 10"]
BOARD = ["add an esp32 called board", "add a led called lamp to board on pin 2"]
TAXI = ["create a game car called taxi"]

FUZZY_INTENTS = [
    dict(id="fz_fast", context=HERO, gold=["make hero fast"],
         train=["hero should be fast", "make the hero fast", "I want hero to be fast"],
         test=["set hero to fast", "hero needs to be fast", "make hero speedy"]),
    dict(id="fz_very_fast", context=HERO, gold=["make hero very fast"],
         train=["hero should be very fast", "make the hero really fast", "I want hero to be very fast"],
         test=["make hero really quick", "hero must be very fast", "make the hero very speedy"]),
    dict(id="fz_slow", context=HERO, gold=["make hero slow"],
         train=["hero should be slow", "make the hero slow", "I want the hero to be slow"],
         test=["hero needs to be slow", "make hero sluggish", "set hero to slow"]),
    dict(id="fz_bit_slower", context=HERO10, gold=["make hero a bit slower"],
         train=["make hero slightly slower", "make the hero a little slower", "hero should be a bit slower"],
         test=["slow hero down a bit", "make hero a tad slower", "make hero just a little slower"]),
    dict(id="fz_much_faster", context=HERO10, gold=["make hero much faster"],
         train=["make hero a lot faster", "hero should be much faster", "make the hero a lot faster"],
         test=["make hero much quicker", "hero needs to be a lot faster", "speed hero up a lot"]),
    dict(id="fz_cond_low", context=HERO, gold=["when health of hero is low, print 'help'"],
         train=["when hero's health is low, print 'help'", "print 'help' when the health of hero is low",
                "if health of hero gets low, print 'help'"],
         test=["when the hero's health becomes low, print 'help'", "if hero's health is low print 'help'",
               "print 'help' whenever hero's health is low"]),
    # ---- held out entirely: never trained on
    dict(id="fz_taxi_fast", context=TAXI, gold=["make taxi fast"], holdout=True, train=[],
         test=["taxi should be fast", "make the taxi fast", "I want taxi to be fast",
               "set taxi to fast", "taxi needs to be fast", "make taxi quick"]),
    dict(id="fz_lamp_bright", context=BOARD, gold=["make lamp bright"], holdout=True, train=[],
         test=["lamp should be bright", "make the lamp bright", "I want lamp to be bright",
               "set lamp to bright", "the lamp needs to be bright", "make lamp very bright"]),
    dict(id="fz_lamp_dimmer", context=BOARD + ["set brightness of lamp to 200"], gold=["make lamp a bit dimmer"],
         holdout=True, train=[],
         test=["make lamp slightly dimmer", "make the lamp a little dimmer", "lamp should be a bit dimmer",
               "make lamp a bit darker", "dim the lamp a bit", "make lamp just a little dimmer"]),
    dict(id="fz_twice", context=HERO + ["set speed of hero to 5"], gold=["make hero twice as fast"], holdout=True,
         train=[],
         test=["hero should be twice as fast", "make the hero twice as fast", "make hero two times as fast",
               "double hero's speed", "hero needs to be twice as fast", "make hero twice as fast as before"]),
]
# "make lamp very bright" in fz_lamp_bright is deliberately a DIFFERENT meaning (should not match): it tests
# that the interpreter does not confuse 'very bright' with 'bright'.

# extra paraphrases for existing intents (intent id -> (train, test))
EXTRA = {
    "mk_player": (["add a player to the game called hero with 100 health"],
                  ["create a game player hero, give it 100 health", "make me a game player named hero with health 100"]),
    "mk_enemies": (["create 3 enemies for the game with health 30"],
                   ["spawn three game monsters with health 30", "add 3 game opponents with 30 health"]),
    "set_speed": (["set the speed of hero to 20"],
                  ["hero should have speed 20", "change hero speed to 20"]),
    "set_color": (["set the color of box to blue"],
                  ["box should be blue", "make the box blue"]),
    "rename": (["change the name of hero to knight"],
               ["rename hero as knight", "hero is now called knight"]),
    "delete_entity": (["delete the hero"],
                      ["remove hero completely", "eliminate hero"]),
    "beh_timer": (["each 2 seconds, decrease stock of store by 1"],
                  ["every 2 secs, decrease stock of store by 1", "reduce the stock of store by 1 every two seconds"]),
    "beh_sensor": (["when the temperature of temp is above 30, switch on lamp"],
                   ["if temp's temperature exceeds 30, turn on lamp", "turn on the lamp whenever temperature of temp is above 30"]),
    "t_mkdir": (["create a directory called backup"],
                ["new folder backup", "make the folder backup"]),
    "t_delete_tmp": (["delete all tmp files recursively"],
                     ["remove every .tmp file recursively", "delete the temporary files in all subfolders"]),
}

# out-of-scope / risky: must not be executed
INVALID_TRAIN2 = ["make everything better", "delete the universe", "hero go brrr", "compute the meaning of life",
                  "make the code nicer somehow"]
INVALID_TEST2 = ["please read my mind", "launch the missiles", "make it pop", "hero dance dance", "be creative"]


def extend(intents: list, invalid_train: list, invalid_test: list) -> None:
    """Merge into the base corpus (in place)."""
    by_id = {it["id"]: it for it in intents}
    for iid, (tr, te) in EXTRA.items():
        it = by_id[iid]
        it["train"] = list(it["train"]) + tr
        it["test"] = list(it["test"]) + te
    intents.extend(FUZZY_INTENTS)
    invalid_train.extend(INVALID_TRAIN2)
    invalid_test.extend(INVALID_TEST2)


def count() -> int:
    n = sum(len(it["train"]) + len(it["test"]) for it in FUZZY_INTENTS)
    n += sum(len(a) + len(b) for a, b in EXTRA.values())
    return n + len(INVALID_TRAIN2) + len(INVALID_TEST2)
