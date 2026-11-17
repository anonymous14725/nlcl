"""NLCL benchmark:  python -m nlcl.bench  [--out DIR] [--backend NAME|auto|off] [--quick]

Suites (all on the same corpus, held-out sentences only for scoring):
  fidelity     interpretation accuracy, silent errors, nonsense refusal, calibration, consistency
  learning     the same metrics after training on 0 / 1 / 2 / 3 paraphrases per intent
  terminal     effect-equivalence of terminal sentences + verification of every generated bash / PowerShell
               command in a sandbox + unconfirmed destructive actions
  determinism  identical interpretations across repeated runs
  cost         latency and tokens
Systems: nlcl-untrained, nlcl-trained, and (with an API key) llm-direct and nlcl+teacher.
Writes report.md and results.json.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

from .. import modules as modreg
from .. import weights as wmod
from ..interpreter import Interpreter
from ..knowledge import Knowledge
from ..learn import Evaluator, Example, consistency, load_corpus, metrics, signature, train
from ..shell import crosscheck, execute, snapshot, ShellError

HERE = Path(__file__).resolve().parent


def _fixture() -> Path:
    from .fixture import build
    return build(Path(tempfile.mkdtemp(prefix="nlcl_bench_fs_")) / "proj")


def _fresh_kb() -> Knowledge:
    return Knowledge(tempfile.mkdtemp(prefix="nlcl_bench_kb_"), modreg.load_modules())


def _subset(examples: list[Example], k: int) -> list[Example]:
    """Training set with only the first k training paraphrases of every intent (invalid: first 4k)."""
    out = []
    count: dict[str, int] = {}
    for e in examples:
        if e.split != "train":
            out.append(e)
            continue
        lim = 4 * k if e.intent == "invalid" else k
        count[e.intent] = count.get(e.intent, 0) + 1
        if count[e.intent] <= lim:
            out.append(e)
    return out


# ---------------------------------------------------------------------- LLM baselines
class LLMDirect:
    """The model reads the sentence and writes the IR itself (no weighting, no abstention except questions)."""

    def __init__(self, backend):
        from ..llm import LLMParser
        self.backend = backend
        self.p = LLMParser(backend)
        self.tokens = 0

    def run(self, ev: Evaluator, ex: Example) -> dict:
        from ..llm import LLMError
        st = ev.context_state(ex.context)
        t0 = time.perf_counter()
        try:
            ops, question = self.p.parse(ex.text, st, list(modreg.load_modules()))
        except LLMError as e:
            from ..llm import QuotaExceeded
            if isinstance(e, QuotaExceeded):
                raise
            ops, question = [], f"error: {e}"
        dt = time.perf_counter() - t0
        self.tokens += (len(ex.text) + 4000) // 4          # prompt + sentence, rough
        executed = bool(ops)
        shell = []
        try:
            Interpreter(st).apply_all([o for o in ops if o.action != "SHELL"])
        except Exception:
            pass
        shell = [o.props for o in ops if o.action == "SHELL"]
        right = signature(st, shell) == ev.gold_signature(ex) and executed == ex.valid
        return {"id": ex.id, "text": ex.text, "valid": ex.valid, "right": right, "weight": 1.0 if executed else 0.0,
                "decision": "execute" if executed else "ask", "executed": executed, "features": {},
                "source": "llm", "summary": " ; ".join(map(str, ops)) or (question or ""), "seconds": dt,
                "frames": [], "intent": None}


def _teacher_run(kb, backend, ev: Evaluator, ex: Example) -> dict:
    """NLCL with the model as a teacher below the execution weight (and distillation into the KB)."""
    from ..runtime import Session
    from .. import discover
    s = Session("bench", backend=backend, kb=kb)
    s.interactive, s.auto_yes = False, True
    s.state = ev.context_state(ex.context)
    s.interp = Interpreter(s.state)
    t0 = time.perf_counter()
    best = s.interpret(ex.text)[0]
    source = "nlcl"
    ops = best.ops
    if best.score < s.execute_at:
        from ..llm import LLMError
        try:
            ops2, q = s.llm.parse(ex.text, s.state, list(modreg.load_modules()))
        except LLMError as e:
            from ..llm import QuotaExceeded
            if isinstance(e, QuotaExceeded):
                raise
            ops2, q = [], None
        if ops2:
            dry = wmod.llm_features(ops2, s.state)
            if wmod.score(dry, kb.params()) >= s.ask_below:
                rule = discover.distill(s, ex.text, ops2)
                if rule:
                    kb.add_rewrite(rule[0], rule[1], 1)
                ops, source = ops2, "teacher"
            else:
                ops = []
        else:
            ops = []
    executed = bool(ops) and (source == "teacher" or wmod.decision(best.score) != "ask")
    shell = []
    if executed:
        Interpreter(s.state).apply_all([o for o in ops if o.action != "SHELL"])
        shell = [o.props for o in ops if o.action == "SHELL"]
    right = signature(s.state, shell) == ev.gold_signature(ex) and executed == ex.valid
    return {"id": ex.id, "text": ex.text, "valid": ex.valid, "right": right, "weight": best.score,
            "decision": "execute" if executed else "ask", "executed": executed, "features": best.features,
            "source": source, "summary": " ; ".join(map(str, ops)), "seconds": time.perf_counter() - t0,
            "frames": [], "intent": None}


# ---------------------------------------------------------------------- terminal suite
def terminal_suite(kb, fs: Path, examples: list[Example]) -> dict:
    ev = Evaluator(kb, fs)
    from ..runtime import Session
    term = [e for e in examples if e.split == "test" and e.intent.startswith("t_")]
    rows, verified, checked, unconfirmed = [], 0, 0, 0
    for ex in term:
        s = Session("bench", backend="off", kb=kb)
        s.cwd = fs
        s.interactive, s.auto_yes = True, False
        best = s.interpret(ex.text)[0]
        gold = s.interpret(ex.gold[-1])[0]
        ok = False
        if best.source == "shell" and best.ops and gold.source == "shell":
            ok = _same_effect(best.ops[0], gold.ops[0], fs)
            for sh in ("bash", "powershell"):
                try:
                    good, _why = crosscheck(best.ops[0], fs, sh)
                except Exception:
                    good = False
                checked += 1
                verified += int(good)
            # would the runtime change files without confirmation?
            out = s._shell(best)
            if any(line.strip() == "done" or line.startswith("  done") for line in out):
                if kb.needs_confirmation(best.ops[0].props["intent"]):
                    unconfirmed += 1
            s.confirm = None
        rows.append({"text": ex.text, "right": ok, "weight": round(best.score, 3), "source": best.source,
                     "executed": wmod.decision(best.score) != "ask" and best.source == "shell"})
    n = len(rows)
    return {"sentences": n,
            "effect_accuracy": round(sum(r["right"] for r in rows) / max(1, n), 4),
            "silent_error_rate": round(sum(1 for r in rows if r["executed"] and not r["right"]) / max(1, n), 4),
            "commands_verified": f"{verified}/{checked}",
            "unconfirmed_destructive_actions": unconfirmed,
            "rows": rows}


def _same_effect(a, b, fs: Path) -> bool:
    with tempfile.TemporaryDirectory() as td:
        import shutil
        pa, pb = Path(td) / "a", Path(td) / "b"
        shutil.copytree(fs, pa)
        shutil.copytree(fs, pb)
        try:
            oa = execute(a, pa)
        except ShellError:
            return False
        try:
            ob = execute(b, pb)
        except ShellError:
            return False
        return snapshot(pa) == snapshot(pb) and oa.replace(str(pa), "") == ob.replace(str(pb), "")


def llm_terminal_suite(backend, fs: Path, examples: list[Example], limit: int = 0) -> dict:
    """The model writes a bash command directly; scored by the same sandbox effect check."""
    import shutil
    import subprocess
    from ..llm import LLMError
    from ..runtime import Session
    term = [e for e in examples if e.split == "test" and e.intent.startswith("t_")]
    if limit and limit < len(term):
        term = [term[int(i * len(term) / limit)] for i in range(limit)]
    right = executed = 0
    kb = _fresh_kb()
    for ex in term:
        s = Session("bench", backend="off", kb=kb)
        gold = s.interpret(ex.gold[-1])[0]
        try:
            cmd = backend.chat([{"role": "system", "content": "Reply with ONLY one bash command, no explanation, "
                                                             "no code fences. Working directory is the project folder."},
                                {"role": "user", "content": ex.text}], json_mode=False).strip().strip("`")
        except LLMError as e:
            from ..llm import QuotaExceeded
            if isinstance(e, QuotaExceeded):
                raise
            continue
        executed += 1
        with tempfile.TemporaryDirectory() as td:
            pa, pb = Path(td) / "a", Path(td) / "b"
            shutil.copytree(fs, pa)
            shutil.copytree(fs, pb)
            try:
                ref = execute(gold.ops[0], pa)
                p = subprocess.run(["bash", "-c", cmd], cwd=str(pb), capture_output=True, text=True, timeout=60)
                same_fs = snapshot(pa) == snapshot(pb)
                same_out = sorted(x.strip().lstrip("./") for x in ref.splitlines() if x.strip()) == \
                    sorted(x.strip().lstrip("./") for x in p.stdout.splitlines() if x.strip())
                right += int(same_fs and (same_out or ref == ""))
            except Exception:
                pass
    n = len(term)
    return {"sentences": n, "effect_accuracy": round(right / max(1, n), 4), "executed": executed}


# ---------------------------------------------------------------------- main
def run_bench(out: Path, backend_name: str | None = "off", quick: bool = False, log=print, sample_n: int = 0) -> dict:
    from ..llm import autodetect
    out.mkdir(parents=True, exist_ok=True)
    examples = load_corpus()
    fs = _fixture()
    results: dict = {"date": time.strftime("%Y-%m-%d %H:%M"), "systems": {}, "learning_curve": [], "notes": []}
    test = [e for e in examples if e.split == "test"]

    log("== fidelity: untrained")
    kb0 = _fresh_kb()
    ev0 = Evaluator(kb0, fs)
    r0 = [ev0.run(e) for e in test]
    m0 = metrics(r0)
    m0["paraphrase_consistency"] = consistency(r0, test)
    results["systems"]["nlcl-untrained"] = m0

    log("== learning curve")
    for k in ([0, 3] if quick else [0, 1, 2, 3]):
        kb = _fresh_kb()
        if k:
            train(kb, _subset(examples, k), log=lambda *a: None, cwd=fs)
        ev = Evaluator(kb, fs)
        res = [ev.run(e, kb.rewrites(), kb.params() or None) for e in test]
        m = metrics(res)
        m["paraphrases_per_intent"] = k
        m["rules_learned"] = kb.stats()["rewrites"]
        results["learning_curve"].append(m)
        log(f"   k={k}: accuracy {m['interpretation_accuracy']}  silent {m['silent_error_rate']}  "
            f"nonsense {m['nonsense_caught']}  ece {m['calibration_ece']}")
    kb_t = _fresh_kb()
    tr = train(kb_t, examples, log=lambda *a: None, cwd=fs)
    ev_t = Evaluator(kb_t, fs)
    rt = [ev_t.run(e, kb_t.rewrites(), kb_t.params()) for e in test]
    mt = metrics(rt)
    mt["paraphrase_consistency"] = consistency(rt, test)
    mt["rules_learned"] = tr["rules_learned"]
    results["systems"]["nlcl-trained"] = mt
    results["learned_kb"] = kb_t.learned_text()
    # risk / coverage: what share it executes at each weight threshold, and how often that is right
    sel = []
    for th in (0.0, 0.35, 0.5, 0.6, 0.7, 0.8, 0.9):
        ex_ = [r for r in rt if r["weight"] >= th and r["summary"] != "(nothing)"]
        sel.append({"threshold": th, "coverage": round(len(ex_) / len(rt), 3),
                    "precision": round(sum(r["right"] for r in ex_) / max(1, len(ex_)), 3),
                    "wrong_executions": sum(1 for r in ex_ if not r["right"])})
    results["selective"] = sel

    log("== determinism")
    runs = []
    for _ in range(2 if quick else 5):
        evd = Evaluator(kb_t, fs)
        runs.append([evd.run(e, kb_t.rewrites(), kb_t.params())["summary"] for e in test])
    same = sum(1 for i in range(len(test)) if len({r[i] for r in runs}) == 1)
    results["determinism"] = {"nlcl": f"{same}/{len(test)}", "runs": len(runs)}

    log("== terminal")
    results["terminal"] = {"nlcl-trained": terminal_suite(kb_t, fs, examples)}

    backend = None
    if backend_name not in ("off", "none"):
        backend = autodetect(None if backend_name in (None, "auto") else backend_name)
    if backend is None:
        results["notes"].append("llm-direct and nlcl+teacher were NOT run: no language-model backend "
                                "(set a free API key, see ':backend status', then run again).")
    else:
        from ..llm import QuotaExceeded
        # the same sample of held-out sentences for every system (free tiers allow few requests a day)
        def spread(items, k):
            return [items[int(i * len(items) / k)] for i in range(k)] if k < len(items) else list(items)
        if not sample_n or sample_n >= len(test):
            sample = test
        else:   # keep the share of nonsense sentences, spread over all intents
            bad = [e for e in test if not e.valid]
            good = [e for e in test if e.valid]
            k_bad = max(1, round(sample_n * len(bad) / len(test)))
            sample = spread(good, sample_n - k_bad) + spread(bad, k_bad)
        ids = {e.id for e in sample}
        if len(sample) < len(test):
            results["notes"].append(f"language-model systems were scored on a sample of {len(sample)} of {len(test)} "
                                    "held-out sentences; the NLCL columns in the comparison table use the same sample.")
            for name, rows in (("nlcl-untrained", r0), ("nlcl-trained", rt)):
                sub = [r for r in rows if r["id"] in ids]
                ms = metrics(sub)
                ms["paraphrase_consistency"] = consistency(sub, sample)
                results["systems"][name + " (sample)"] = ms
        n_term = 33 if len(sample) == len(test) else max(3, len(sample) // 3)
        n_rep = 10 if len(sample) == len(test) else 4
        log(f"== llm-direct ({backend}) on {len(sample)} sentences; at most about "
            f"{len(sample) + n_rep + len(sample) // 2 + n_term} requests in total")
        try:
            lp = LLMDirect(backend)
            ev_l = Evaluator(_fresh_kb(), fs)
            rl = [lp.run(ev_l, e) for e in sample]
            ml = metrics(rl)
            ml["paraphrase_consistency"] = consistency(rl, sample)
            ml["approx_tokens"] = lp.tokens
            for k in ("calibration_ece", "calibration_brier", "calibration_auroc"):
                ml[k] = "n/a"
            results["systems"]["llm-direct"] = ml
            rep_n = min(10 if len(sample) == len(test) else 4, len(sample))
            rl2 = [lp.run(ev_l, e)["summary"] for e in sample[:rep_n]]
            same_l = sum(1 for a, b in zip([r["summary"] for r in rl], rl2) if a == b)
            results["determinism"]["llm-direct"] = f"{same_l}/{rep_n}"
            log("== nlcl+teacher")
            kb_h = _fresh_kb()
            train(kb_h, examples, log=lambda *a: None, cwd=fs)
            ev_h = Evaluator(kb_h, fs)
            rh = [_teacher_run(kb_h, backend, ev_h, e) for e in sample]
            mh = metrics(rh)
            mh["paraphrase_consistency"] = consistency(rh, sample)
            mh["rules_distilled"] = kb_h.stats()["rewrites"] - tr["rules_learned"]
            results["systems"]["nlcl+teacher"] = mh
            log("== llm terminal")
            results["terminal"]["llm-direct"] = llm_terminal_suite(
                backend, fs, examples, limit=0 if len(sample) == len(test) else max(3, len(sample) // 3))
        except QuotaExceeded as e:
            results["notes"].append(f"stopped early: {e}. Systems not listed were not completed.")
            log(f"   stopped: {e}")

    results["cost"] = {"nlcl_median_ms": mt["median_ms"], "nlcl_tokens": 0}
    (out / "results.json").write_text(json.dumps(results, indent=1, default=str), encoding="utf-8")
    (out / "report.md").write_text(report(results), encoding="utf-8")
    return results


def report(r: dict) -> str:
    systems = r["systems"]
    cols = list(systems)
    rows = [("interpretation accuracy (valid sentences)", "interpretation_accuracy"),
            ("executed something not meant (silent error rate)", "silent_error_rate"),
            ("precision when it executes", "precision_when_executed"),
            ("nonsense / out-of-scope refused", "nonsense_caught"),
            ("asked instead of executing", "asked"),
            ("all paraphrases of an intent right", "paraphrase_consistency"),
            ("calibration error of the weight (ECE, lower is better)", "calibration_ece"),
            ("weight separates right/wrong (AUROC)", "calibration_auroc"),
            ("median latency per sentence (ms)", "median_ms")]
    L = [f"# NLCL benchmark report ({r['date']})", "",
         "Held-out sentences only (never used for training). Correct = same resulting system state as the canonical "
         "sentence. Pre-registered hypotheses: `bench/HYPOTHESES.md`.", ""]
    for n in r.get("notes", []):
        L.append(f"> **Note:** {n}")
    L += ["", "## Fidelity", "", "| metric | " + " | ".join(cols) + " |", "|---|" + "---|" * len(cols)]
    for label, key in rows:
        L.append(f"| {label} | " + " | ".join(str(systems[c].get(key, "")) for c in cols) + " |")
    if r.get("selective"):
        L += ["", "## Risk / coverage of the trained weight", "",
              "Executing only sentences whose weight reaches the threshold (the rest are asked about):", "",
              "| weight threshold | share executed | precision of what is executed | wrong executions |",
              "|---|---|---|---|"]
        for s in r["selective"]:
            L.append(f"| {s['threshold']} | {s['coverage']} | {s['precision']} | {s['wrong_executions']} |")
    L += ["", "## Learning curve (no model retraining: rules, evidence and weights in Prolog)", "",
          "| training paraphrases per intent | rules learned | accuracy | silent errors | nonsense refused | ECE |",
          "|---|---|---|---|---|---|"]
    for m in r["learning_curve"]:
        L.append(f"| {m['paraphrases_per_intent']} | {m['rules_learned']} | {m['interpretation_accuracy']} | "
                 f"{m['silent_error_rate']} | {m['nonsense_caught']} | {m['calibration_ece']} |")
    t = r["terminal"]
    L += ["", "## Terminal", "", "| metric | " + " | ".join(t) + " |", "|---|" + "---|" * len(t)]
    for key, label in (("effect_accuracy", "same file-system effect as the meant command"),
                       ("silent_error_rate", "executed with the wrong effect"),
                       ("commands_verified", "generated bash + PowerShell verified in a sandbox"),
                       ("unconfirmed_destructive_actions", "files changed/deleted without showing a diff first")):
        L.append(f"| {label} | " + " | ".join(str(t[s].get(key, "n/a")) for s in t) + " |")
    L += ["", "## Determinism", "", f"Identical interpretation on every repeated run: " +
          ", ".join(f"{k}: {v}" for k, v in r["determinism"].items() if k != "runs"), "",
          "## Cost", "", f"NLCL offline: {r['cost']['nlcl_tokens']} tokens, median {r['cost']['nlcl_median_ms']} ms per sentence.",
          "", "## What the trained interpreter learned (Prolog facts)", "", "```prolog", r.get("learned_kb", "").strip(), "```", ""]
    return "\n".join(L)


def main(argv=None) -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="nlcl.bench")
    ap.add_argument("--out", default=str(Path.cwd() / "bench" / "results"))
    ap.add_argument("--backend", default="off", help="off | auto | groq | gemini | ... (for llm-direct and nlcl+teacher)")
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--sample", type=int, default=0, help="score language-model systems on N held-out sentences")
    a = ap.parse_args(argv)
    res = run_bench(Path(a.out), a.backend, a.quick, sample_n=a.sample)
    print(f"\nreport: {Path(a.out) / 'report.md'}")
    for name, m in res["systems"].items():
        print(f"  {name:16} accuracy {m['interpretation_accuracy']}  silent errors {m['silent_error_rate']}  "
              f"nonsense refused {m['nonsense_caught']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
