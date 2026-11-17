"""Training and testing of the interpreter's semantic knowledge.

Correctness is *semantic*: an interpretation is right when applying it produces the same system state
as the canonical (gold) sentence - not when the text or the IR string matches.

train()  1. induces rewrite rules (new meanings for words / phrases) from failed examples; a rule is kept
            only if it fixes examples and breaks none of the training examples that were already right
         2. records evidence (reliability counts) for every frame / intent used
         3. fits the sentence-weighting model on the training examples
         -> everything is written to the Prolog knowledge base (learned.pl)
evaluate() the same measurements on held-out sentences (never seen during training)
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import weights as wmod
from .interpreter import Interpreter
from .ir import SystemState


@dataclass
class Example:
    id: str
    text: str
    split: str                       # train | test
    context: list[str] = field(default_factory=list)
    gold: list[str] = field(default_factory=list)   # canonical sentences; [] + valid=False -> must not execute
    valid: bool = True
    intent: str = ""


def load_corpus(path: Path | str | None = None) -> list[Example]:
    from .bench.corpus import build_examples
    if path is None:
        return build_examples()
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [Example(**d) for d in data]


# ---------------------------------------------------------------------- semantic signatures
def signature(state: SystemState, shell_ops: list | None = None) -> str:
    ents = []
    for e in state.entities.values():
        par = state.entities[e.parent].name if e.parent in state.entities else None
        ents.append((e.name, e.domain, e.kind, json.dumps(e.props, sort_keys=True, default=str), par,
                     json.dumps(e.behaviors, sort_keys=True)))
    links = sorted((state.entities[l.src].name, state.entities[l.dst].name, l.via or "",
                    json.dumps(l.props, sort_keys=True)) for l in state.links.values()
                   if l.src in state.entities and l.dst in state.entities)
    shell = [json.dumps(_norm_shell(p), sort_keys=True) for p in (shell_ops or [])]
    return json.dumps({"e": sorted(ents), "l": links, "s": shell, "d": state.default_domain}, sort_keys=True)


def _norm_shell(props: dict) -> dict:
    p = {k: v for k, v in props.items() if v not in (None, False, [], "")}
    if p.get("dir") == ".":
        p.pop("dir")
    if p.get("kind") == "any" and p.get("path"):
        p.pop("kind")
    return p


class Evaluator:
    """Runs sentences through a session that shares one knowledge base (fast, no disk writes)."""

    def __init__(self, kb, cwd: Path | None = None):
        from .runtime import Session
        self.kb = kb
        self._Session = Session
        self.cwd = cwd
        self._ctx_cache: dict[tuple, SystemState] = {}
        self._gold_cache: dict[tuple, str] = {}

    def session(self, rewrites=None):
        s = self._Session("eval", backend="off", kb=self.kb)
        s.interactive = False
        s.auto_yes = True
        if rewrites is not None:
            s.rules.rewrites = rewrites
        if self.cwd:
            s.cwd = Path(self.cwd)
        return s

    def context_state(self, ctx: list[str]) -> SystemState:
        key = tuple(ctx)
        if key not in self._ctx_cache:
            s = self.session(rewrites=[])
            for ln in ctx:
                s.handle(ln)
            self._ctx_cache[key] = s.state.clone()
        return self._ctx_cache[key].clone()

    def gold_signature(self, ex: Example) -> str:
        key = (tuple(ex.context), tuple(ex.gold), ex.valid)
        if key not in self._gold_cache:
            s = self.session(rewrites=[])
            s.state = self.context_state(ex.context)
            s.interp = Interpreter(s.state)
            shell = []
            if ex.valid:
                for ln in ex.gold:
                    c = s.interpret(ln)[0]
                    if c.source == "shell":
                        shell.append(c.ops[0].props)
                    else:
                        s.interp.apply_all(c.ops)
            self._gold_cache[key] = signature(s.state, shell)
        return self._gold_cache[key]

    def run(self, ex: Example, rewrites=None, params=None) -> dict:
        """Interpret one sentence: weight, decision, and whether the interpretation is semantically right."""
        s = self.session(rewrites)
        s.state = self.context_state(ex.context)
        s.interp = Interpreter(s.state)
        t0 = time.perf_counter()
        cands = s.interpret(ex.text)
        best = cands[0]
        if params is not None:
            best.score = wmod.score(best.features, params)
        elapsed = time.perf_counter() - t0
        shell = []
        if best.ops:
            if best.source == "shell":
                shell.append(best.ops[0].props)
            else:
                try:
                    s.interp.apply_all(best.ops)
                except Exception:
                    pass
        right = signature(s.state, shell) == self.gold_signature(ex) and bool(best.ops) == ex.valid
        decision = wmod.decision(best.score)
        return {"id": ex.id, "text": ex.text, "valid": ex.valid, "right": right, "weight": round(best.score, 4),
                "decision": decision, "executed": decision != "ask", "features": best.features,
                "source": best.source, "summary": best.summary(), "seconds": elapsed,
                "frames": [f for _c, f, _t in (best.detail.clauses if best.source == "rules" and best.detail else []) if f],
                "intent": best.ops[0].props.get("intent") if best.source == "shell" and best.ops else None}


# ---------------------------------------------------------------------- metrics
def metrics(results: list[dict]) -> dict:
    n = len(results)
    if not n:
        return {}
    valid = [r for r in results if r["valid"]]
    executed = [r for r in results if r["executed"]]
    silent = [r for r in executed if not r["right"]]
    m = {
        "sentences": n,
        "interpretation_accuracy": round(sum(r["right"] for r in valid) / max(1, len(valid)), 4),
        "executed": len(executed),
        "precision_when_executed": round(sum(r["right"] for r in executed) / max(1, len(executed)), 4),
        "silent_error_rate": round(len(silent) / n, 4),
        "asked": n - len(executed),
        "nonsense_caught": f"{sum(1 for r in results if not r['valid'] and not r['executed'])}/"
                           f"{sum(1 for r in results if not r['valid'])}",
        "median_ms": round(sorted(r["seconds"] for r in results)[n // 2] * 1000, 2),
    }
    m.update({f"calibration_{k}": v for k, v in
              wmod.calibration([r["weight"] for r in results], [int(r["right"]) for r in results]).items()})
    return m


def consistency(results: list[dict], examples: list[Example]) -> float:
    """Share of intents whose paraphrases all end in the same (right) state."""
    by: dict[str, list[bool]] = {}
    ids = {e.id: e for e in examples}
    for r in results:
        ex = ids.get(r["id"])
        if ex is not None and ex.valid:
            by.setdefault(ex.intent, []).append(r["right"])
    if not by:
        return 0.0
    return round(sum(all(v) for v in by.values()) / len(by), 4)


# ---------------------------------------------------------------------- training
def _words(text: str) -> list[str]:
    import re
    return re.findall(r"[A-Za-z][\w'\-]*", text.lower())


VERB_TOKENS = ("CREATE", "DELETE", "SET", "CONNECT", "DISCONNECT", "RENAME", "MOVE", "SHOW", "BUILD", "RUN",
               "UNDO", "DEFINE", "USE", "WHEN")


_CONTENT: set[str] = set()


def _safe_rule(kb, frm: tuple, to: str) -> bool:
    """The KB refuses rules that would erase or change the meaning of a core verb, and rules that
    would make the interpreter ignore a content word (an entity name, a property or a kind)."""
    if not to and any(w.replace("'s", "") in _CONTENT for w in frm):
        return False
    for w in frm:
        r = kb.query(f"atomic({kb.to_term(w)}, T)", limit=1)
        if r and r[0]["T"] in VERB_TOKENS:
            if not to:
                return False
            r2 = kb.query(f"atomic({kb.to_term(to)}, T2)", limit=1)
            if not r2 or r2[0]["T2"] != r[0]["T"]:
                return False
    return True


def induce_rewrites(ev: Evaluator, train: list[Example], base: list, log=print, max_rules: int = 60,
                    keep: list[Example] | None = None) -> list:
    """Learn new meanings: phrase in the sentence -> word of the canonical sentence (or nothing).
    `keep` are sentences that must stay right (the canonical ones): a rule that breaks any is rejected."""
    rules = list(base)
    keep = list(keep or [])
    guard = train + keep
    # content words of this corpus: names of entities, properties and kinds
    from .weights import known_props
    _CONTENT.clear()
    _CONTENT.update(known_props())
    for r in ev.kb.query("kind_of(K, _, 2)", limit=5000):
        _CONTENT.add(str(r["K"]))
    for ex in guard:
        st = ev.context_state(ex.context)
        for e in st.entities.values():
            _CONTENT.update({e.name.lower(), e.kind.lower(), *e.props})
    current = {ex.id: ev.run(ex, rules)["right"] for ex in guard}
    failing = [ex for ex in train if ex.valid and not current[ex.id]]
    proposals: dict[tuple, set] = {}
    for ex in failing:
        tw = _words(ex.text)
        gw = set(w for g in ex.gold for w in _words(g))
        targets = sorted(w for w in gw if w not in tw) + [""]
        grams = set()
        for n in (1, 2, 3):
            for i in range(len(tw) - n + 1):
                g = tuple(tw[i:i + n])
                if any(w not in gw for w in g):
                    grams.add(g)
        for g in grams:
            for t in targets:
                if not _safe_rule(ev.kb, g, t):
                    continue
                rule = (list(g), [t] if t else [], 1)
                trial = rules + [rule]
                trial.sort(key=lambda r: -len(r[0]))
                if ev.run(ex, trial)["right"]:
                    proposals.setdefault((g, t), set()).add(ex.id)
    accepted = []
    for (g, t), fixed in sorted(proposals.items(), key=lambda kv: (-len(kv[1]), len(kv[0][0]), kv[0])):
        if len(accepted) >= max_rules:
            break
        if all(current.get(i) for i in fixed):
            continue                                   # already fixed by an earlier rule
        trial = sorted(rules + [(list(g), [t] if t else [], len(fixed))], key=lambda r: -len(r[0]))
        new = {ex.id: ev.run(ex, trial)["right"] for ex in guard}
        broken = [i for i, ok in current.items() if ok and not new[i]]
        gained = [i for i, ok in new.items() if ok and not current[i]]
        if broken or not gained:
            continue
        rules, current = trial, new
        accepted.append((list(g), [t] if t else [], len(gained)))
        log(f"  learned: '{' '.join(g)}' -> '{t or '(ignore)'}'  fixes {len(gained)}, breaks 0")
    return accepted


def corpus_folder() -> Path:
    """The small project folder the corpus' terminal sentences refer to (built fresh each time)."""
    import tempfile
    from .bench.fixture import build
    return build(Path(tempfile.mkdtemp(prefix="nlcl_corpus_fs_")) / "proj")


def train(kb, examples: list[Example], log=print, induce: bool = True, cwd: Path | None = None) -> dict:
    cwd = cwd or corpus_folder()
    ev = Evaluator(kb, cwd)
    tr = [e for e in examples if e.split == "train"]
    base = kb.rewrites()
    t0 = time.time()
    gold = [e for e in examples if e.split == "gold"]
    learned = induce_rewrites(ev, tr, base, log, keep=gold) if induce else []
    for frm, to, sup in learned:
        kb.add_rewrite(frm, to, sup)
    rewrites = kb.rewrites()
    results = [ev.run(e, rewrites) for e in tr]
    for r in results:
        for f in r["frames"]:
            kb.record(f"frame({f.lower()})", r["right"])
        if r["intent"]:
            kb.record(f"intent({r['intent']})", r["right"])
    # refit features after evidence changed, then fit the weighting model
    results = [ev.run(e, rewrites) for e in tr]
    samples = [(r["features"], int(r["right"])) for r in results]
    params = wmod.fit(samples, kb.params() or None)
    kb.set_params(params)
    kb.save()
    final = [ev.run(e, rewrites, params) for e in tr]
    return {"train_sentences": len(tr), "rules_learned": len(learned), "seconds": round(time.time() - t0, 1),
            "train_metrics": metrics(final)}


def evaluate(kb, examples: list[Example], split: str = "test", cwd: Path | None = None) -> tuple[dict, list[dict]]:
    ev = Evaluator(kb, cwd or corpus_folder())
    ex = [e for e in examples if e.split == split]
    params = kb.params() or None
    res = [ev.run(e, kb.rewrites(), params) for e in ex]
    m = metrics(res)
    m["paraphrase_consistency"] = consistency(res, ex)
    return m, res
