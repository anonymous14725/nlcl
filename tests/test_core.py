"""Core tests (offline, stdlib unittest):  python -m unittest discover -s tests"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nlcl.ir import Op, SystemState, eval_expr, render_expr, validate_behavior, validate_expr  # noqa: E402
from nlcl.runtime import Session  # noqa: E402


def session(**kw):
    tmp = tempfile.mkdtemp(prefix="nlcl_test_")
    return Session("t", backend="off", vocab_path=os.path.join(tmp, "vocab.json"), outdir=os.path.join(tmp, "build"), **kw)


def run(s, *lines):
    out = []
    for ln in lines:
        out += s.handle(ln)
    return "\n".join(out)


class IRTests(unittest.TestCase):
    def test_expr_roundtrip(self):
        self.assertEqual(validate_expr("health <= 0 and not alive"), "health <= 0 && ! alive")
        self.assertEqual(render_expr("health <= 0 && !alive", "python", lambda n: f"self.{n}"),
                         "self.health <= 0 and not self.alive")
        self.assertEqual(render_expr("a == true", "verilog"), "a == 1'b1")

    def test_bad_expr(self):
        with self.assertRaises(ValueError):
            validate_expr("__import__('os')[0]")
        with self.assertRaises(ValueError):
            validate_expr("(a + b")

    def test_eval(self):
        self.assertEqual(eval_expr("x * 2 + y", {"x": 3, "y": 1}), 7)

    def test_behavior_validation(self):
        b = validate_behavior({"on": "timer:500", "do": [{"op": "add", "prop": "HP", "value": "1"}]})
        self.assertEqual(b["do"][0]["prop"], "health")  # synonym normalisation
        with self.assertRaises(ValueError):
            validate_behavior({"on": "sometimes", "do": [{"op": "destroy"}]})

    def test_op_from_dict(self):
        op = Op.from_dict({"action": "create", "domain": "Python", "props": {"Max Speed": 3}})
        self.assertEqual(op.action, "CREATE")
        self.assertEqual(op.props, {"max_speed": 3})

    def test_state_json_undo(self):
        s = SystemState("x")
        s.snapshot()
        s.name = "y"
        self.assertTrue(s.undo())
        self.assertEqual(s.name, "x")
        self.assertEqual(SystemState.from_json(s.to_json()).name, "x")


class ParserTests(unittest.TestCase):
    def test_clarification_flow(self):
        s = session()
        out = run(s, "create an object")
        self.assertIn("What kind of object", out)
        out = run(s, "a python object called bank with balance 100")
        e = s.state.resolve("bank")
        self.assertIsNotNone(e)
        self.assertEqual(e.domain, "python")
        self.assertEqual(e.props["balance"], 100)

    def test_properties(self):
        s = session()
        run(s, "use python", "create an object called hero")
        run(s, "give it 100 health, make it movable, and add collision detection")
        run(s, "set speed to 2.5", "color red", "position 1 2 3", "no gravity")
        p = s.state.resolve("hero").props
        self.assertEqual(p["health"], 100)
        self.assertIs(p["movable"], True)
        self.assertIs(p["collision"], True)
        self.assertEqual(p["speed"], 2.5)
        self.assertEqual(p["color"], "red")
        self.assertEqual(p["position"], [1, 2, 3])
        self.assertIs(p["gravity"], False)

    def test_behaviours(self):
        s = session()
        run(s, "use python", "create a counter called c")
        run(s, "every 2 seconds, increase count by 1")
        run(s, "when count is at least 10, print 'done {count}' and set count to 0")
        run(s, "destroy it when count is above 100")
        bs = s.state.resolve("c").behaviors
        self.assertEqual(bs[0]["on"], "timer:2000")
        self.assertEqual(bs[1]["if"], "count >= 10")
        self.assertEqual([a["op"] for a in bs[1]["do"]], ["log", "set"])
        self.assertEqual(bs[2]["do"][0]["op"], "destroy")

    def test_immediate_and_macro(self):
        s = session()
        run(s, "use python", "create an account called bank with balance 10")
        run(s, "increase balance by 5")
        self.assertEqual(s.state.resolve("bank").props["balance"], 15)
        out = run(s, "define deposit(x) as increase balance by x")
        self.assertIn("learned command", out)
        run(s, "deposit 20")
        self.assertEqual(s.state.resolve("bank").props["balance"], 35)

    def test_teach_word(self):
        s = session()
        run(s, ":teach conjure create", "use python")
        run(s, "conjure a service called api")
        self.assertIsNotNone(s.state.resolve("api"))
        self.assertIn("refusing", run(s, ":teach create delete"))

    def test_connect_delete_undo(self):
        s = session()
        run(s, "use python", "create a service called a", "create a dashboard called b")
        run(s, "connect a to b via http sending load every 500 ms")
        link = next(iter(s.state.links.values()))
        self.assertEqual(link.via, "http")
        self.assertEqual(link.props["fields"], ["load"])
        self.assertEqual(link.props["interval_ms"], 500)
        run(s, "delete b")
        self.assertEqual(len(s.state.links), 0)
        run(s, "undo")
        self.assertIsNotNone(s.state.resolve("b"))

    def test_count_and_rename(self):
        s = session()
        run(s, "use python", "create 3 services")
        self.assertEqual(len(s.state.entities), 3)
        run(s, "rename service2 to worker")
        self.assertIsNotNone(s.state.resolve("worker"))

class BuildTests(unittest.TestCase):
    def test_python_build_and_run(self):
        s = session()
        run(s, "use python", "create a counter called c with count 0", "every 100 ms, increase count by 1")
        out = "\n".join(s.run("python"))
        self.assertIn("[ok]", out)
        files = json.loads((s.build_dir / "system.json").read_text(encoding="utf-8"))
        self.assertEqual(files["ir"]["entities"][0]["name"], "c")


if __name__ == "__main__":
    unittest.main()
