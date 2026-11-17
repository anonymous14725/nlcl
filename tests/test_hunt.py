"""Regression tests for bugs reported by the tester agents (exact sentences they used)."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nlcl.composer import resolve_channels  # noqa: E402
from nlcl.runtime import Session  # noqa: E402


def session():
    tmp = tempfile.mkdtemp(prefix="nlcl_hunt_")
    return Session("t", backend="off", vocab_path=os.path.join(tmp, "v.json"), outdir=os.path.join(tmp, "b"))


def run(s, *lines):
    out = []
    for ln in lines:
        out += s.handle(ln)
    return "\n".join(out)


def E(s, name):
    e = s.state.resolve(name)
    assert e is not None, f"no entity {name}: {s.state.summary()}"
    return e


def rules(s):
    return [(e.name, b) for e in s.state.entities.values() for b in e.behaviors]


class Creation(unittest.TestCase):
    def test_called_at_position(self):
        s = session()
        run(s, "use game", "create a platform called floor at position 0, 0", "create 3 coins called star")
        f = E(s, "floor")
        self.assertEqual((f.kind, f.props.get("position")), ("platform", [0, 0]))
        self.assertEqual(len([e for e in s.state.entities.values() if e.kind == "coin"]), 3)

    def test_group_props_apply_to_all(self):
        s = session()
        run(s, "create 3 game enemies with health 30 and color red")
        es = [e for e in s.state.entities.values() if e.kind == "enemy"]
        self.assertEqual([e.props.get("color") for e in es], ["red"] * 3)
        run(s, "make them blue")
        self.assertEqual([e.props.get("color") for e in es], ["blue"] * 3)

    def test_sequential_names(self):
        s = session()
        run(s, "create 3 game enemies", "create 3 game enemies")
        self.assertEqual(sorted(e.name for e in s.state.entities.values()),
                         ["enemy1", "enemy2", "enemy3", "enemy4", "enemy5", "enemy6"])

    def test_relative_clause_keeps_kind(self):
        s = session()
        out = run(s, "use game", "create an enemy called goomba that moves left and right")
        self.assertEqual(E(s, "goomba").kind, "enemy")
        self.assertIn("ignored", out)

    def test_compound_kinds_and_bits(self):
        s = session()
        run(s, "use hardware", "create a state machine called traffic with states red, green, yellow",
            "create a clock divider called slow", "create an 8 bit counter called c8")
        self.assertIn(E(s, "traffic").kind, ("state_machine", "fsm"))
        self.assertEqual(E(s, "traffic").props["states"], ["red", "green", "yellow"])
        self.assertEqual(E(s, "slow").kind, "clock_divider")
        self.assertEqual((E(s, "c8").kind, E(s, "c8").props["width"]), ("counter", 8))
        self.assertEqual(len(s.state.entities), 3)

    def test_seven_segment(self):
        s = session()
        run(s, "use hardware", "create a seven segment display called d")
        self.assertEqual(len(s.state.entities), 1)

    def test_parent_after_with(self):
        s = session()
        run(s, "use hardware", "create a register called r1", "create a register called acc with width 8 inside r1")
        acc = E(s, "acc")
        self.assertEqual(acc.props.get("width"), 8)

    def test_domain_from_context(self):
        s = session()
        out = run(s, "create a 3d sphere called ball", "add a red cube called box1", "add a blue sphere called ball2")
        self.assertNotIn("What kind", out)
        self.assertEqual({e.domain for e in s.state.entities.values()}, {"3d"})

    def test_3d_scene_kind(self):
        s = session()
        run(s, "create a 3d scene called demo")
        self.assertEqual(E(s, "demo").kind, "scene")

    def test_no_property_question_when_props_given(self):
        s = session()
        out = run(s, "create a 3d light called sun with intensity 2")
        self.assertNotIn("What properties", out)

    def test_dimensions_and_units(self):
        s = session()
        run(s, "create a 3d room called lab with size 12 by 3 by 10")
        self.assertEqual(E(s, "lab").props["size"], [12, 3, 10])
        run(s, "use cad", "create a box called block with width 25mm", "create a tube called p with diameter 2 cm")
        self.assertEqual(E(s, "block").props["width"], 25)
        self.assertEqual(E(s, "p").props["diameter"], 20)

    def test_cad_phrases(self):
        s = session()
        run(s, "use cad", "make a plate called base with 4 holes of 3.2 mm", "create a bracket called arm",
            "make the arm 50 mm long")
        self.assertEqual((E(s, "base").props["hole_count"], E(s, "base").props["hole_diameter"]), (4, 3.2))
        self.assertEqual(E(s, "arm").props["length"], 50)
        run(s, "create a box called b2", "make the wall 3")
        self.assertEqual(E(s, "b2").props["wall"], 3)
        self.assertNotIn("game", s.state.domains())

    def test_name_prop_on_create(self):
        s = session()
        run(s, "use python", "create a service called x with state 1 and name 'bob'")
        self.assertIsNotNone(s.state.resolve("bob") or E(s, "x"))


class Pending(unittest.TestCase):
    def test_command_cancels_question(self):
        s = session()
        out = run(s, "create an object", "create a python service called a")
        self.assertIn("skipped", out)
        self.assertEqual(E(s, "a").domain, "python")

class Phrases(unittest.TestCase):
    def test_polite_prefixes(self):
        s = session()
        run(s, "I want a game object called ball", "I'd like a python object named bank", "please create 2 game enemies")
        self.assertEqual(len(s.state.entities), 4)

    def test_number_words(self):
        s = session()
        run(s, "create a game object named player", "set its speed to five", "give it twenty health",
            "every two seconds, log y", "when health is below five, log low")
        p = E(s, "player")
        self.assertEqual((p.props["speed"], p.props["health"]), (5, 20))
        ons = [b["on"] for _, b in rules(s)]
        self.assertIn("timer:2000", ons)
        self.assertIn("health < 5", [b["if"] for _, b in rules(s)])

    def test_possessive_and_of(self):
        s = session()
        run(s, "create a game object called hero", "hero's speed should be 9")
        self.assertEqual(E(s, "hero").props["speed"], 9)
        run(s, "the speed of hero is 4")
        self.assertEqual(E(s, "hero").props["speed"], 4)

    def test_select_and_rename_and_undo(self):
        s = session()
        run(s, "create a game object called hero", "create a game object called other", "select hero")
        self.assertEqual(s.state.focus, E(s, "hero").id)
        self.assertNotIn("select", E(s, "hero").props)
        run(s, "change its name to knight")
        self.assertIsNotNone(s.state.resolve("knight"))
        run(s, "set speed to 1", "set speed to 2", "undo the last 2 changes")
        self.assertNotIn("speed", E(s, "knight").props)

    def test_parent_phrases(self):
        s = session()
        run(s, "use cpp", "create a cpp object called robot", "create a cpp object called wheel",
            "make wheel a child of robot")
        self.assertEqual(E(s, "wheel").parent, E(s, "robot").id)
        run(s, "create a cpp object called arm", "put arm inside robot")
        self.assertEqual(E(s, "arm").parent, E(s, "robot").id)

    def test_has_a(self):
        s = session()
        run(s, "use hardware", "create a register called r1", "r1 has a pwm called fan")
        self.assertEqual(E(s, "fan").parent, E(s, "r1").id)

    def test_follows(self):
        s = session()
        run(s, "use game", "create a game enemy called ghost", "create a game player called bob", "ghost follows bob")
        self.assertEqual(E(s, "ghost").props["target"], "bob")

    def test_dark_color_and_spawn_key(self):
        s = session()
        run(s, "use game", "create a game object called turret", "make turret dark green",
            "create a spawner called factory", "set factory spawn to drone")
        self.assertEqual(E(s, "turret").props["color"], "dark green")
        self.assertEqual(E(s, "factory").props["spawn"], "drone")

    def test_add_to_immediate(self):
        s = session()
        run(s, "create a game object called hero with 50 health", "add 5 to health")
        self.assertEqual(E(s, "hero").props["health"], 55)
        self.assertNotIn("to_health", E(s, "hero").props)

    def test_make_it_a_kind(self):
        s = session()
        run(s, "use hardware", "create a register called r1", "make it a counter with width 8")
        self.assertEqual((E(s, "r1").kind, E(s, "r1").props["width"], len(s.state.entities)), ("counter", 8, 1))

    def test_delete_all_and_list(self):
        s = session()
        run(s, "create 3 game enemies", "delete enemy1 and enemy2")
        self.assertEqual([e.name for e in s.state.entities.values()], ["enemy3"])
        run(s, "create 2 game enemies", "remove all enemies")
        self.assertEqual(len(s.state.entities), 0)

    def test_remove_prop(self):
        s = session()
        run(s, "create a game object called hero", "set the color to red", "remove the color")
        self.assertNotIn("color", E(s, "hero").props)

    def test_heater_that_is_off(self):
        s = session()
        run(s, "use cpp", "create a cpp object called thermostat", "give it 20 temperature, 22 target and a heater that is off")
        p = E(s, "thermostat").props
        self.assertEqual((p["temperature"], p["target"], p["heater"]), (20, 22, False))

    def test_state_of_quoted(self):
        s = session()
        run(s, "use cpp", "create a cpp object called light", "give it a state of \"red\" and an owner named 'Sara'")
        p = E(s, "light").props
        self.assertEqual((p["state"], p["owner"]), ("red", "Sara"))

    def test_connect_to_pin(self):
        s = session()
        run(s, "add an esp32 called b", "add a led called led to b", "connect led to pin 13")
        self.assertEqual(E(s, "led").props["pin"], 13)


class Behaviours(unittest.TestCase):
    def test_owner_is_prop_holder(self):
        s = session()
        run(s, "create a python service called store with stock 10 and price 5", "create a python logger called audit",
            "every 1 second, decrease stock by 2")
        self.assertEqual([n for n, _ in rules(s)], ["store"])
        self.assertNotIn("stock", E(s, "audit").props)

    def test_cross_entity_actions_split(self):
        s = session()
        run(s, "add an arduino uno called ctl", "add a servo called door to ctl", "add a button called bell to ctl",
            "create a python counter called visits", "when bell is pressed, increase visits count by 1 and set door angle to 90")
        owners = sorted(n for n, _ in rules(s))
        self.assertEqual(owners, ["door", "visits"])

    def test_flattened_condition_names(self):
        s = session()
        run(s, "use game", "create a player called bob with lives 3", "when bob lives reach 0, print 'game over' and destroy bob")
        (_, b), = rules(s)
        self.assertEqual(b["if"], "lives == 0")

    def test_collision_partner(self):
        s = session()
        run(s, "use game", "create a game enemy called ghost", "create a game player called bob with lives 3",
            "when ghost touches bob, decrease bob lives by 1")
        (name, b), = rules(s)
        self.assertEqual((name, b["on"], b.get("with"), b["do"][0]["prop"]), ("bob", "collision", "ghost", "lives"))
        self.assertNotIn("bob_lives", E(s, "ghost").props)

    def test_passive_touched(self):
        s = session()
        run(s, "use game", "create 2 game coins", "when a coin is touched, destroy it")
        self.assertEqual(sorted(n for n, b in rules(s) if b["on"] == "collision"), ["coin1", "coin2"])

    def test_no_comma_and_trailing(self):
        s = session()
        run(s, "create a game object called hero", "when health is below 1 destroy it", "add 1 to score every 3 seconds",
            "when space is pressed set speed to 10")
        ons = [(b["on"], b["if"]) for _, b in rules(s)]
        self.assertIn(("update", "health < 1"), ons)
        self.assertIn(("timer:3000", None), ons)
        self.assertIn(("input:space", None), ons)

    def test_key_press_phrasings(self):
        s = session()
        run(s, "create a game object called hero", "when the player presses space, jump",
            "when I press the left key, move left by 2", "when the user types reset, set speed to 0")
        self.assertEqual([b["on"] for _, b in rules(s)], ["input:space", "input:left", "input:reset"])

    def test_timer_with_if(self):
        s = session()
        run(s, "use hardware", "create a state machine called light with states red, green",
            "every 3000 ms, if state is red, set state to green")
        (_, b), = rules(s)
        self.assertEqual((b["on"], b["if"], b["do"][0]["value"]), ("timer:3000", 'state == "red"', '"green"'))

    def test_subject_verbs(self):
        s = session()
        run(s, "use game", "create a game player called hero", "create a game object called tank",
            "create a game object called pad", "when a message from pad arrives, hero jumps",
            "when space is pressed, tank fires a bullet")
        names = [n for n, _ in rules(s)]
        self.assertEqual(names, ["hero", "tank"])
        self.assertEqual(rules(s)[1][1]["do"][0]["args"], ['"bullet"'])

    def test_sensor_phrases(self):
        s = session()
        run(s, "add an esp32 called n", "add a pir sensor called eye to n on pin 5 with motion false",
            "add a led called led to n on pin 2", "when eye detects motion, turn on the led",
            "when motion is detected, turn on the led", "when motion stops, turn off the led")
        conds = [b["if"] for _, b in rules(s)]
        self.assertEqual(conds, ["eye.motion == true"] * 2 + ["eye.motion == false"])
        self.assertNotIn("detected", E(s, "led").props)

    def test_between(self):
        s = session()
        run(s, "use cpp", "create a cpp object called tank with level 50", "when level of tank is between 20 and 80, log ok")
        (_, b), = rules(s)
        self.assertEqual(b["if"], "(level >= 20 && level <= 80)")

    def test_rotate_change_show(self):
        s = session()
        run(s, "create a 3d cube called spinner", "every frame, rotate it by 90 degrees",
            "when it is clicked, change its color to green")
        bs = [b for _, b in rules(s)]
        self.assertEqual(bs[0]["do"][0]["op"], "add")
        self.assertEqual(bs[1]["do"][0], {"op": "set", "prop": "color", "value": '"green"'})

    def test_quoted_commas_in_log(self):
        s = session()
        run(s, "create a 3d box called b", "every 2 seconds, print 'at {x}, score {score}'")
        (_, b), = rules(s)
        self.assertEqual(b["do"][0]["text"], "at {x}, score {score}")

    def test_macro_in_behaviour(self):
        s = session()
        run(s, "create a game object called hero with 50 health", "define heal(n) as increase health by n",
            "when space is pressed, heal 10", ":teach smash DELETE", "when it collides with hero, smash it")
        acts = [b["do"][0]["op"] for _, b in rules(s)]
        self.assertEqual(acts, ["add", "destroy"])

    def test_send_owner_is_link_source(self):
        s = session()
        run(s, "create a cpp agent called ant with energy 5", "create a python service called monitor",
            "connect ant to monitor via mqtt", "every 2 seconds send energy to monitor")
        self.assertEqual([n for n, _ in rules(s)], ["ant"])

    def test_go_to_state(self):
        s = session()
        run(s, "use hardware", "create a state machine called conveyor with states idle, running and stopped",
            "when state is idle and start is pressed, go to running")
        (_, b), = rules(s)
        self.assertEqual((b["on"], b["if"], b["do"][0]["value"]), ("input:start", 'state == "idle"', '"running"'))

    def test_rename_updates_references(self):
        s = session()
        run(s, "add an esp32 board called station", "add a temperature sensor called temp to station with temperature 20",
            "add a fan called fan to station", "when the temperature of temp goes above 30, turn on fan",
            "rename temp to probe")
        (_, b), = rules(s)
        self.assertEqual(b["if"], "probe.temperature > 30")

    def test_decrease_display(self):
        s = session()
        run(s, "create a game object called hero with health 5", "every 1 second, decrease health by 10")
        self.assertIn("health -= 10", s.state.summary())


class Channels(unittest.TestCase):
    def test_fields_exclude_junk_and_board_children(self):
        s = session()
        run(s, "add an esp32 called node", "add a temperature sensor called temp to node with temperature 20",
            "create a python server called srv", "connect node to srv via mqtt")
        ch, = resolve_channels(s.state)
        self.assertEqual(ch.fields, ["temperature"])
        self.assertEqual(ch.config["types"], {"temperature": "int"})

    def test_websocket_is_http(self):
        s = session()
        run(s, "create a python server called hub with temperature 1", "create a game object called hud",
            "connect hub to hud via websocket sending temperature")
        ch, = resolve_channels(s.state)
        self.assertEqual((ch.protocol, ch.config["requested_protocol"]), ("http", "websocket"))

    def test_enum_contract(self):
        s = session()
        run(s, "use hardware", "create a state machine called conveyor with states idle, running and stopped",
            "create a python logger called log", "connect conveyor to log via uart sending state")
        ch, = resolve_channels(s.state)
        self.assertEqual(ch.config["enums"]["state"], ["idle", "running", "stopped"])

    def test_stale_files_removed(self):
        s = session()
        run(s, "use python", "create a service called a")
        s.build()
        run(s, "use hardware", "create a register called acc")
        s.build()
        rtl = s.build_dir / "hardware"
        self.assertTrue(rtl.exists())
        run(s, "delete acc")
        s.build()
        self.assertFalse(any(p.suffix == ".v" for p in s.build_dir.rglob("*")))


if __name__ == "__main__":
    unittest.main()
