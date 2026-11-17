"""Online features, tested with a stand-in LLM backend (no network, no API key):
non-English input routed to the online agent, and the bug-fix agent."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nlcl.fixer import Fixer, diagnose  # noqa: E402
from nlcl.runtime import Session  # noqa: E402


class FakeBackend:
    """Replies with queued JSON objects (or callables taking the messages)."""
    name = "fake"
    model = "fake-1"

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def chat(self, messages, temperature=0.0, json_mode=True):
        self.calls.append(messages)
        r = self.replies.pop(0) if self.replies else {"edits": [], "done": True, "lines": []}
        if callable(r):
            r = r(messages)
        return r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)

    def __str__(self):
        return "fake (fake-1)"


def session(backend="off"):
    tmp = tempfile.mkdtemp(prefix="nlcl_online_")
    return Session("t", backend=backend, vocab_path=os.path.join(tmp, "v.json"), outdir=os.path.join(tmp, "b"))


def run(s, *lines):
    out = []
    for ln in lines:
        out += s.handle(ln)
    return "\n".join(out)


class NonEnglish(unittest.TestCase):
    def test_persian_needs_online_agent(self):
        s = session()
        out = run(s, "یک بازیکن بساز")
        self.assertIn("online agent", out)
        self.assertEqual(len(s.state.entities), 0)

    def test_persian_goes_through_agent(self):
        fb = FakeBackend({"lines": ["create a game player called ali with health 100",
                                    "when health of ali is below 1, destroy ali"]})
        s = session(fb)
        s.mode = "rules"            # the translated English must be handled by the tested offline pipeline
        out = run(s, "یه بازیکن به اسم علی بساز با ۱۰۰ تا جون، وقتی جونش صفر شد حذفش کن")
        e = s.state.resolve("ali")
        self.assertIsNotNone(e, out)
        self.assertEqual((e.domain, e.props["health"], e.behaviors[0]["if"]), ("game", 100, "health < 1"))
        self.assertEqual(len(fb.calls), 1)
        self.assertIn("یه بازیکن", fb.calls[0][-1]["content"])

    def test_agent_question_is_shown(self):
        fb = FakeBackend({"lines": [], "question": "کدام شیء؟"})
        s = session(fb)
        self.assertIn("کدام شیء؟", run(s, "اون رو قرمز کن"))

    def test_persian_inside_quotes_stays_offline(self):
        s = session()
        run(s, "use python", "create a service called a", "when started, print 'سلام'")
        self.assertEqual(s.state.resolve("a").behaviors[0]["do"][0]["text"], "سلام")

    def test_bad_translation_does_not_loop(self):
        fb = FakeBackend({"lines": ["یک بازیکن بساز"]})
        s = session(fb)
        self.assertIn("skipped", run(s, "یک بازیکن بساز"))
        self.assertEqual(len(fb.calls), 1)


BROKEN = "def total(items):\n    s = 0\n    for x in items\n        s += x\n    return s\n\nprint(total([1, 2, 3]))\n"


class BugFix(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="nlcl_fix_"))

    def test_diagnose_offline(self):
        (self.dir / "a.py").write_text(BROKEN, encoding="utf-8")
        (self.dir / "b.json").write_text("{bad", encoding="utf-8")
        (self.dir / "c.js").write_text("function f( { return 1 }", encoding="utf-8")
        (self.dir / "d.v").write_text("module m(input a);\n  always @(*) begin\n  end\n", encoding="utf-8")
        (self.dir / "ok.py").write_text("x = 1\n", encoding="utf-8")
        d = {x.file: x for x in diagnose(self.dir)}
        self.assertEqual(d["a.py"].line, 3)
        self.assertIn("b.json", d)
        self.assertIn("c.js", d)
        self.assertIn("d.v", d)
        self.assertNotIn("ok.py", d)

    def test_offline_reports_but_does_not_edit(self):
        f = self.dir / "a.py"
        f.write_text(BROKEN, encoding="utf-8")
        rep = Fixer(None).fix(f)
        self.assertFalse(rep.ok)
        self.assertEqual(f.read_text(encoding="utf-8"), BROKEN)
        self.assertEqual(len(rep.before), 1)

    def test_fix_syntax_error_in_existing_file(self):
        f = self.dir / "a.py"
        f.write_text(BROKEN, encoding="utf-8")
        fb = FakeBackend({"analysis": "missing colon", "done": True,
                          "edits": [{"file": "a.py", "search": "    for x in items\n", "replace": "    for x in items:\n"}]})
        rep = Fixer(fb).fix(f, "it does not run")
        self.assertTrue(rep.ok, rep.lines())
        self.assertIn("for x in items:", f.read_text(encoding="utf-8"))
        self.assertIn("a.py", rep.changed)
        self.assertTrue(Path(rep.backup, "a.py").read_text(encoding="utf-8") == BROKEN)
        self.assertIn("it does not run", fb.calls[0][-1]["content"])
        self.assertIn("a.py:3", fb.calls[0][-1]["content"])

    def test_bad_round_is_rolled_back_then_fixed(self):
        f = self.dir / "a.py"
        good = "def half(x):\n    return x / 2\n"
        f.write_text(good.replace("x / 2", "x * 2"), encoding="utf-8")
        fb = FakeBackend(
            {"analysis": "x", "done": True, "edits": [{"file": "a.py", "search": "return x * 2", "replace": "return x / 2 ("}]},
            {"analysis": "wrong operator", "done": True,
             "edits": [{"file": "a.py", "search": "return x * 2", "replace": "return x / 2"}]})
        rep = Fixer(fb).fix(f, "half() doubles the number")
        self.assertEqual(f.read_text(encoding="utf-8"), good)
        self.assertTrue(any("rolled back" in n for n in rep.notes))
        self.assertIn("ROLLED BACK", fb.calls[1][-1]["content"])

    def test_inexact_search_and_runtime_error(self):
        f = self.dir / "run.py"
        f.write_text("data = {'a': 1}\nif True:\n    print(data['b'])\n", encoding="utf-8")
        fb = FakeBackend({"analysis": "missing key", "done": True,
                          "edits": [{"file": "run.py", "search": "print(data['b'])", "replace": "print(data.get('b', 0))"}]})
        rep = Fixer(fb).fix(f, "KeyError when I run it", run_cmd=[sys.executable, "-u", "run.py"])
        self.assertTrue(rep.ok, rep.lines())
        self.assertEqual(rep.before[0].tool, "run")
        self.assertIn("    print(data.get('b', 0))", f.read_text(encoding="utf-8"))

    def test_unmatched_edit_is_reported_back(self):
        f = self.dir / "a.py"
        f.write_text("x = 1\n", encoding="utf-8")
        fb = FakeBackend({"analysis": "?", "done": True, "edits": [{"file": "a.py", "search": "y = 2", "replace": "y = 3"}]},
                         {"edits": [], "done": True})
        rep = Fixer(fb).fix(f, "something")
        self.assertEqual(rep.changed, {})
        self.assertIn("did not apply", fb.calls[1][-1]["content"])

    def test_undo(self):
        f = self.dir / "a.py"
        f.write_text(BROKEN, encoding="utf-8")
        fb = FakeBackend({"analysis": "colon", "done": True,
                          "edits": [{"file": "a.py", "search": "for x in items", "replace": "for x in items:"}]})
        Fixer(fb).fix(f)
        Fixer.undo(self.dir)
        self.assertEqual(f.read_text(encoding="utf-8"), BROKEN)


class FixThroughLanguage(unittest.TestCase):
    def test_fix_existing_file_by_sentence(self):
        d = Path(tempfile.mkdtemp(prefix="nlcl_fixnl_"))
        f = d / "calc.py"
        f.write_text(BROKEN, encoding="utf-8")
        fb = FakeBackend({"analysis": "missing colon", "done": True,
                          "edits": [{"file": "calc.py", "search": "for x in items", "replace": "for x in items:"}]})
        s = session(fb)
        out = run(s, f'fix "{f}": it has a syntax error')
        self.assertIn("fixed 1 file", out)
        self.assertIn("for x in items:", f.read_text(encoding="utf-8"))
        self.assertIn("syntax error", fb.calls[0][-1]["content"])

    def test_check_generated_module_offline(self):
        s = session()
        run(s, "use python", "create a counter called c", "every 1 second, increase count by 1")
        out = run(s, "check the python module")
        self.assertIn("no problems", out)

    def test_fix_generated_code_survives_rebuild(self):
        def reply(messages):
            self.assertIn("main.py", messages[-1]["content"])
            return {"analysis": "wrong step", "done": True,
                    "edits": [{"file": "main.py", "search": "self.count += 1", "replace": "self.count += 2"}]}
        s = session(FakeBackend(reply))
        s.mode = "rules"
        run(s, "use python", "create a counter called c", "every 1 second, increase count by 1")
        out = run(s, "fix the python module: the counter should go up by 2")
        main = s.build_dir / "python" / "main.py"
        self.assertIn("self.count += 2", main.read_text(encoding="utf-8"), out)
        self.assertEqual(len(s.state.patches), 1)
        run(s, "set label to 'x'")           # change the system -> rebuild regenerates main.py
        out = "\n".join(s.build())
        self.assertIn("self.count += 2", main.read_text(encoding="utf-8"))
        self.assertIn("re-applied", out)

    def test_fix_needs_target_when_empty(self):
        s = session()
        self.assertIn("nothing to fix", run(s, "fix it"))

    def test_fix_every_module_kind_is_diagnosable(self):
        s = session()
        run(s, "create a game player called hero with health 5", "add an esp32 called b", "add a led called led to b",
            "create a hardware counter called hc", "create a cpp object called co", "create a 3d cube called cu",
            "create a cad box called bx", "create a python service called sv")
        out = run(s, "check it")
        self.assertIn("no problems", out, out)


if __name__ == "__main__":
    unittest.main()
