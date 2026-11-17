"""Regression tests for parser/core bugs found by the module agents."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nlcl.ir import ident, singular  # noqa: E402
from nlcl.runtime import Session  # noqa: E402


def session():
    tmp = tempfile.mkdtemp(prefix="nlcl_fix_")
    return Session("t", backend="off", vocab_path=os.path.join(tmp, "v.json"), outdir=os.path.join(tmp, "b"))


def run(s, *lines):
    out = []
    for ln in lines:
        out += s.handle(ln)
    return "\n".join(out)


def beh(s, name, i=-1):
    return s.state.resolve(name).behaviors[i]


class Triggers(unittest.TestCase):
    def test_when_started(self):
        s = session()
        run(s, "use python", "create a service called svc", "when started, print 'hi'", "when it boots, print 'b'")
        self.assertEqual(beh(s, "svc", 0)["on"], "start")
        self.assertEqual(beh(s, "svc", 1)["on"], "start")

    def test_message_from(self):
        s = session()
        run(s, "use python", "create a service called brain", "create a service called temp",
            "when brain receives a message from temp, print 'got'")
        self.assertEqual(beh(s, "temp")["on"], "message:temp")

    def test_change_dotted(self):
        s = session()
        run(s, "use python", "create a service called temp with temperature 1", "create a dashboard called d",
            "when temp's temperature changes, set alerts to 1")
        b = [x for e in s.state.entities.values() for x in e.behaviors][0]
        self.assertEqual(b["on"], "change:temp.temperature")

    def test_is_on(self):
        s = session()
        run(s, "use python", "create a service called x with status 0", "when status is on, print 'on'")
        self.assertEqual(beh(s, "x")["if"], "status == true")


class Actions(unittest.TestCase):
    def test_send_to(self):
        s = session()
        run(s, "use python", "create a service called a", "create a dashboard called sensor",
            "focus a", "every 1 second, send to sensor")
        b = [x for e in s.state.entities.values() for x in e.behaviors][0]
        self.assertEqual(b["do"][0], {"op": "send", "to": "sensor", "fields": []})

    def test_send_fields_list(self):
        s = session()
        run(s, "use python", "create a service called a", "create a dashboard called d",
            "every 1 second, send temperature, humidity and light to d")
        b = [x for e in s.state.entities.values() for x in e.behaviors][0]
        self.assertEqual(b["do"][0]["fields"], ["temperature", "humidity", "light"])

    def test_quoted_set_value(self):
        s = session()
        run(s, "use python", "create a service called x", "when started, set label to 'big'",
            "when started, set mood to 'it''s ok'")
        self.assertEqual(beh(s, "x", 0)["do"][0]["value"], '"big"')
        self.assertEqual(beh(s, "x", 1)["do"][0]["value"], "\"it's ok\"")
        self.assertEqual(s.state.resolve("x").props["label"], "")
        self.assertNotIn("it", s.state.resolve("x").props)

    def test_color_word_value(self):
        s = session()
        run(s, "use python", "create a service called x", "when started, set color to red")
        self.assertEqual(beh(s, "x")["do"][0]["value"], '"red"')
        self.assertNotIn("red", s.state.resolve("x").props)

    def test_dotted_and_of_targets(self):
        s = session()
        run(s, "use python", "create a service called led1 with on 0", "create a service called ctrl",
            "every 1 second, set led1.on to true", "every 2 seconds, increase brightness of led1 by 5")
        led = s.state.resolve("led1")
        self.assertEqual(len(led.behaviors), 2)
        self.assertEqual(led.behaviors[0]["do"][0]["prop"], "on")
        self.assertEqual(led.behaviors[1]["do"][0]["prop"], "brightness")

    def test_move_by_commas_and_call_args(self):
        s = session()
        run(s, "use python", "create a service called x", "every 1 second, move it by 1, 0, 0",
            "when started, call reset with 1, 2")
        self.assertEqual(beh(s, "x", 0)["do"][0]["op"], "move")
        self.assertEqual(beh(s, "x", 1)["do"][0]["args"], ["1", "2"])

    def test_visible_toggle_default(self):
        s = session()
        run(s, "use python", "create a service called x", "every 1 second, toggle visible")
        self.assertIs(s.state.resolve("x").props["visible"], True)

    def test_bare_condition_is_bool(self):
        s = session()
        run(s, "use python", "create a service called x", "when armed, print 'a'")
        self.assertIs(s.state.resolve("x").props["armed"], False)


class Props(unittest.TestCase):
    def test_position_commas(self):
        s = session()
        run(s, "use python", "create a service called x", "position 0, 1, 0")
        self.assertEqual(s.state.resolve("x").props["position"], [0, 1, 0])

    def test_move_to_commas(self):
        s = session()
        run(s, "use python", "create a service called x", "move it to 1, 2, 3")
        self.assertEqual(s.state.resolve("x").props["position"], [1, 2, 3])

    def test_list_values(self):
        s = session()
        run(s, "use python", "create a service called door with states idle, open and closed")
        self.assertEqual(s.state.resolve("door").props["states"], ["idle", "open", "closed"])
        self.assertEqual(len(s.state.entities), 1)

    def test_bool_and_string_keys(self):
        s = session()
        run(s, "use python", "create a service called x with lid false and side front",
            "give it movable true", "target hero", "url gear.glb", "with value 7")
        p = s.state.resolve("x").props
        self.assertIs(p["lid"], False)
        self.assertEqual(p["side"], "front")
        self.assertIs(p["movable"], True)
        self.assertEqual(p["target"], "hero")
        self.assertEqual(p["url"], "gear.glb")
        self.assertEqual(p["value"], 7)

    def test_quoted_text_prop(self):
        s = session()
        run(s, "use python", "create a service called x with text 'hi {score}, ok'")
        self.assertEqual(s.state.resolve("x").props["text"], "hi {score}, ok")

    def test_connect_field_list(self):
        s = session()
        run(s, "use python", "create a service called a", "create a dashboard called b",
            "connect a to b via http sending x, y and z")
        self.assertEqual(next(iter(s.state.links.values())).props["fields"], ["x", "y", "z"])


class Names(unittest.TestCase):
    def test_multiword_name(self):
        s = session()
        run(s, "use python", "create a service called my robot")
        self.assertIsNotNone(s.state.resolve("my_robot"))

    def test_singular(self):
        for w in ("torus", "status", "axis", "chassis", "physics", "gas"):
            self.assertEqual(singular(w), w)
        self.assertEqual(singular("enemies"), "enemy")
        self.assertEqual(singular("boxes"), "box")


class Domains(unittest.TestCase):
    def test_default_domain_wins(self):
        s = session()
        run(s, "use hardware", "create a counter called c")
        self.assertEqual(s.state.resolve("c").domain, "hardware")
        run(s, "create a dashboard called d")
        self.assertEqual(s.state.resolve("d").domain, "python")

    def test_explicit_domain_word(self):
        s = session()
        run(s, "create a hardware pwm called p", "create a 3d cone called k", "create a game enemy called e")
        self.assertEqual(s.state.resolve("p").domain, "hardware")
        self.assertEqual(s.state.resolve("k").domain, "3d")
        self.assertEqual(s.state.resolve("e").domain, "game")

    def test_bom_script(self):
        tmp = Path(tempfile.mkdtemp()) / "s.nl"
        tmp.write_text(":name bom\nuse python\ncreate a service called a\n", encoding="utf-8-sig")
        from nlcl.__main__ import main
        out = Path(tempfile.mkdtemp())
        self.assertEqual(main([str(tmp), "-b", "off", "-o", str(out), "--vocab", str(out / "v.json"), "--build"]), 0)
        self.assertTrue((out / "python" / "main.py").exists())


if __name__ == "__main__":
    unittest.main()
