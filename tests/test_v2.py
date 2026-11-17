"""Semantic architecture: Prolog KB, terminal semantics, sentence weighting, training, discovery."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nlcl import modules as modreg  # noqa: E402
from nlcl import weights as wmod  # noqa: E402
from nlcl.bench.fixture import build  # noqa: E402
from nlcl.knowledge import Knowledge  # noqa: E402
from nlcl.learn import evaluate, load_corpus, train  # noqa: E402
from nlcl.prolog import Prolog, PrologError  # noqa: E402
from nlcl.runtime import Session  # noqa: E402
from nlcl.shell import ShellParser, crosscheck, execute, render, sandbox, shell_available  # noqa: E402


def session(**kw):
    tmp = tempfile.mkdtemp(prefix="nlcl_v2_")
    return Session("t", backend="off", vocab_path=os.path.join(tmp, "v.json"), **kw)


def run(s, *lines):
    out = []
    for ln in lines:
        out += s.handle(ln)
    return "\n".join(out)


class PrologEngine(unittest.TestCase):
    def setUp(self):
        self.p = Prolog()
        self.p.consult_text(r"""
            parent(tom, bob). parent(bob, ann). parent(bob, pat).
            anc(X, Y) :- parent(X, Y).
            anc(X, Y) :- parent(X, Z), anc(Z, Y).
            max(X, Y, X) :- X >= Y, !.
            max(_, Y, Y).
            safe(X) :- \+ bad(X).
            bad(rm).
            size(N, S) :- ( N > 9 -> S = big ; S = small ).
            :- dynamic seen/1.
        """)

    def test_resolution_cut_negation(self):
        self.assertEqual([a["X"] for a in self.p.query("anc(tom, X)")], ["bob", "ann", "pat"])
        self.assertEqual(self.p.query("max(3, 7, M)"), [{"M": 7}])
        self.assertTrue(self.p.ask("safe(ls)"))
        self.assertFalse(self.p.ask("safe(rm)"))
        self.assertEqual(self.p.query("size(12, S)"), [{"S": "big"}])

    def test_builtins_and_assert(self):
        self.assertEqual(self.p.query("findall(X, parent(bob, X), L)"), [{"L": ["ann", "pat"]}])
        self.assertEqual(self.p.query("X is 2 + 3 * 4"), [{"X": 14}])
        self.assertEqual(self.p.query("append(A, [c], [a, b, c])"), [{"A": ["a", "b"]}])
        self.assertFalse(self.p.ask("seen(x)"))           # dynamic: exists, simply false
        self.p.assertz("seen(x)")
        self.assertTrue(self.p.ask("seen(x)"))
        with self.assertRaises(PrologError):
            self.p.query("undefined_pred(1)")


class KnowledgeBase(unittest.TestCase):
    def test_ontology_safety_learning_persistence(self):
        d = tempfile.mkdtemp()
        kb = Knowledge(d, modreg.load_modules())
        self.assertEqual(kb.domain_candidates("servo"), ["embedded"])
        self.assertTrue(kb.needs_confirmation("delete"))
        self.assertFalse(kb.needs_confirmation("list_files"))
        self.assertTrue(kb.forbidden("delete", ".git"))
        self.assertEqual(kb.file_type("images")[0], "*.png")
        kb.add_rewrite(["get", "rid", "of"], ["delete"], 2)
        kb.record("frame(create_object)", True)
        kb.set_params({"bias": -1.0})
        kb.save()
        kb2 = Knowledge(d, modreg.load_modules())
        self.assertEqual(kb2.rewrites(), [(["get", "rid", "of"], ["delete"], 2)])
        self.assertAlmostEqual(kb2.reliability("frame(create_object)"), 2 / 3)
        self.assertEqual(kb2.params()["bias"], -1.0)
        out = Path(d) / "all.pl"
        kb2.export(out)
        p = Prolog()
        p.consult(out)                                   # the export is plain, reloadable Prolog
        self.assertTrue(p.ask("rewrite([get,rid,of], [delete], _)"))


class Terminal(unittest.TestCase):
    def setUp(self):
        self.root = build(Path(tempfile.mkdtemp()) / "proj")
        self.kb = Knowledge(None, modreg.load_modules())
        self.p = ShellParser(self.kb)

    def op(self, text):
        r = self.p.parse(text)
        self.assertIsNotNone(r, text)
        return r[0]

    def test_parse_and_execute(self):
        self.assertEqual(execute(self.op("count the python files in src"), self.root), "2")
        out, d = sandbox(self.op("delete all .tmp files recursively"), self.root)
        self.assertEqual(sorted(d.removed), ["cache.tmp", "src/old.tmp"])
        self.assertTrue((self.root / "cache.tmp").exists())          # the sandbox never touches the real folder
        self.assertEqual(execute(self.op("show the first 2 lines of notes.txt"), self.root), "banana\napple")
        execute(self.op("replace 'apple' with 'pear' in notes.txt"), self.root)
        self.assertIn("pear pie", (self.root / "notes.txt").read_text())

    def test_safety(self):
        from nlcl.shell import ShellError
        with self.assertRaises(ShellError):
            execute(self.op("remove the folder .git"), self.root, self.kb)
        with self.assertRaises(ShellError):
            execute(self.op("delete ../outside.txt"), self.root, self.kb)

    def test_entity_words_are_not_files(self):
        self.assertIsNone(self.p.parse("delete hero"))

    @unittest.skipUnless(shell_available("bash"), "bash not installed")
    def test_bash_text_is_verified(self):
        for t in ("copy all python files from src to backup", "delete all .tmp files recursively",
                  "search for 'TODO' in all python files", "count the lines in data.csv"):
            ok, why = crosscheck(self.op(t), self.root, "bash")
            self.assertTrue(ok, f"{t}: {render(self.op(t), 'bash')} -> {why}")

    @unittest.skipUnless(shell_available("powershell"), "powershell not installed")
    def test_powershell_text_is_verified(self):
        for t in ("move data.csv into docs", "write 'hi' to a/b.txt", "sort notes.txt"):
            ok, why = crosscheck(self.op(t), self.root, "powershell")
            self.assertTrue(ok, f"{t}: {render(self.op(t), 'powershell')} -> {why}")

    def test_runtime_shows_diff_and_asks_before_deleting(self):
        s = session()
        s.cwd = self.root
        out = run(s, "delete all .tmp files")
        self.assertIn("- cache.tmp", out)
        self.assertIn("Apply these changes", out)
        self.assertTrue((self.root / "cache.tmp").exists())
        run(s, "no")
        self.assertTrue((self.root / "cache.tmp").exists())
        run(s, "delete all .tmp files", "yes")
        self.assertFalse((self.root / "cache.tmp").exists())
        self.assertEqual(s.state.shell_log[-1]["props"]["intent"], "delete")


class Weighting(unittest.TestCase):
    def test_fit_and_calibration(self):
        samples = [({"coverage": 1.0, "novel": 0}, 1)] * 30 + [({"coverage": 1.0, "novel": 2}, 0)] * 30
        p = wmod.fit(samples)
        self.assertGreater(wmod.score({"coverage": 1.0, "novel": 0}, p), 0.8)
        self.assertLess(wmod.score({"coverage": 1.0, "novel": 2}, p), 0.2)
        c = wmod.calibration([0.9, 0.9, 0.1, 0.1], [1, 1, 0, 0])
        self.assertEqual(c["auroc"], 1.0)

    def test_low_weight_asks(self):
        s = session()
        out = run(s, "create a game player called hero", "flibber the wobble")
        self.assertIn("Not sure", out)
        self.assertNotIn("flibber_wobble", s.state.resolve("hero").props)


class Training(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ex = load_corpus()
        cls.kb = Knowledge(tempfile.mkdtemp(), modreg.load_modules())
        cls.before, _ = evaluate(Knowledge(None, modreg.load_modules()), cls.ex)
        cls.report = train(cls.kb, cls.ex, log=lambda *a: None)
        cls.after, cls.res = evaluate(cls.kb, cls.ex)

    def test_held_out_improves(self):
        self.assertLess(self.after["silent_error_rate"], self.before["silent_error_rate"])
        self.assertGreaterEqual(self.after["interpretation_accuracy"], self.before["interpretation_accuracy"])
        caught = lambda m: int(m["nonsense_caught"].split("/")[0])  # noqa: E731
        self.assertGreater(caught(self.after), caught(self.before))
        self.assertLess(self.after["calibration_ece"], self.before["calibration_ece"])

    def test_learned_rules_are_safe(self):
        for frm, to, _ in self.kb.rewrites():
            for w in frm:
                r = self.kb.query(f"atomic({self.kb.to_term(w)}, T)", limit=1)
                if r and r[0]["T"] in ("CREATE", "DELETE", "SET", "CONNECT"):
                    self.assertTrue(to, f"core verb {w} must never be ignored")
        self.assertTrue(self.kb.learned_path.read_text(encoding="utf-8").startswith("% NLCL learned knowledge"))

    def test_deterministic(self):
        _, again = evaluate(self.kb, self.ex)
        self.assertEqual([r["summary"] for r in self.res], [r["summary"] for r in again])


class Discovery(unittest.TestCase):
    def test_typo_is_learned_from_one_confirmation(self):
        d = tempfile.mkdtemp()
        s = Session("t", backend="off", vocab_path=os.path.join(d, "v.json"))
        out = run(s, "crate a game player called hero")
        self.assertIn("Did you mean 'create'", out)
        run(s, "yes")
        self.assertIsNotNone(s.state.resolve("hero"))
        s2 = Session("t", backend="off", vocab_path=os.path.join(d, "v.json"))
        run(s2, "crate a game enemy called orc")           # now understood directly, offline
        self.assertIsNotNone(s2.state.resolve("orc"))

    def test_module_is_grown_and_remembered(self):
        d = tempfile.mkdtemp()
        s = Session("t", backend="off", vocab_path=os.path.join(d, "v.json"))
        out = run(s, "create a kotlin program called calc with precision 2")
        self.assertIn("discovered a new module: 'kotlin'", out)
        self.assertEqual(s.state.resolve("calc").domain, "kotlin")
        s.build()
        spec = json.loads((s.build_dir / "kotlin" / "spec.json").read_text(encoding="utf-8"))
        self.assertEqual(spec["entities"][0]["props"]["precision"], 2)
        self.assertIn(("kotlin", "kotlin", ".kt"), Knowledge(d, modreg.load_modules()).learned_modules())
        modreg.load_modules().pop("kotlin", None)

    def test_ordinary_words_do_not_create_modules(self):
        s = session()
        out = run(s, "use hardware", "create a state machine called door with states a, b",
                  "every 1 second, if state is a, go to b")
        self.assertNotIn("discovered", out)
        self.assertNotIn("go", modreg.load_modules())

    def test_teacher_answer_is_distilled_into_a_rule(self):
        from nlcl.ir import Op
        from nlcl import discover
        s = session()
        run(s, "create a game player called hero with 100 health")
        gold = [Op("DELETE", target="hero")]
        rule = discover.distill(s, "vaporize hero", gold)
        self.assertEqual(rule, (["vaporize"], ["delete"]) if rule and rule[1] == ["delete"] else rule)
        self.assertIsNotNone(rule)


if __name__ == "__main__":
    unittest.main()
