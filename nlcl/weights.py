"""Sentence weighting: how sure is the interpreter that it understood?

Every interpretation of a sentence (rule parse, shell parse, LLM parse) gets features that the
interpreter can measure *without* knowing the answer, and a logistic model turns them into a
weight (probability of being right).  The parameters live in the Prolog KB (param/2) and are fitted
by the trainer on labelled sentences, so the weight is calibrated: a 0.8 sentence is right ~80% of
the time.  The runtime uses the weight to decide: execute / show and execute / ask first.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from .ir import Op, SystemState

FEATURES = ("coverage", "unknown", "errors", "notes", "reliability", "frame_rel", "dry_errors", "question",
            "warnings", "inferred", "fuzzy", "shell", "missing_paths", "llm", "n_ops", "novel", "verbless")

DEFAULT_PARAMS = {
    "bias": -0.6, "coverage": 2.6, "unknown": -0.9, "errors": -2.2, "notes": -0.7, "reliability": 1.2,
    "frame_rel": 0.8, "dry_errors": -2.4, "question": -0.6, "warnings": -0.25, "inferred": -0.35,
    "fuzzy": -0.9, "shell": 0.9, "missing_paths": -1.8, "llm": 0.4, "n_ops": 0.05, "novel": -1.1,
    "verbless": -1.0,
}

# property names that are meaningful without context (everything else must come from a module or the state)
COMMON_PROPS = {"color", "size", "position", "rotation", "scale", "speed", "health", "name", "text", "value", "width",
                "height", "depth", "length", "radius", "diameter", "count", "score", "label", "title", "visible",
                "movable", "collision", "gravity", "pin", "on", "state", "states", "items", "max", "min", "mass",
                "temperature", "humidity", "level", "angle", "duty", "threshold", "interval_ms", "brightness", "power",
                "lives", "damage", "energy", "balance", "rate", "step", "checked", "target", "board", "wall", "lid",
                "mode", "type", "subtype", "material", "shape", "price", "stock", "volume", "distance", "time", "delay",
                "frequency", "voltage", "current", "address", "port", "baud", "topic", "url", "file", "path", "data",
                "points", "jump_force", "velocity", "opacity", "intensity", "label", "placeholder", "password", "enabled",
                "active", "alive", "running", "teeth", "holes", "hole_count", "hole_diameter", "thickness", "fillet"}
EXECUTE_AT = 0.60      # >= : execute
ASK_BELOW = 0.35       # <  : ask before doing anything (between: execute and say how it was understood)

_STOP = {"it", "this", "that", "a", "an", "the", "and", "with", "to", "of", "in", "on", "by", "is", "be", "for",
         "please", "then", "also", "its", "at", "from", "into", "as", "me", "all", "some", "my", "your"}


@dataclass
class Candidate:
    source: str                      # rules | shell | llm
    ops: list[Op]
    features: dict = field(default_factory=dict)
    score: float = 0.0
    text: str = ""
    detail: object = None            # ParseResult or shell features

    def summary(self) -> str:
        return " ; ".join(str(o) for o in self.ops) or "(nothing)"


def sigmoid(x: float) -> float:
    if x < -40:
        return 0.0
    if x > 40:
        return 1.0
    return 1.0 / (1.0 + math.exp(-x))


def score(features: dict, params: dict | None = None) -> float:
    p = dict(DEFAULT_PARAMS)
    p.update(params or {})
    z = p.get("bias", 0.0) + sum(p.get(k, 0.0) * float(features.get(k, 0.0)) for k in FEATURES)
    return sigmoid(z)


def explain(features: dict, params: dict | None = None, top: int = 3) -> str:
    p = dict(DEFAULT_PARAMS)
    p.update(params or {})
    contrib = sorted(((p.get(k, 0.0) * float(features.get(k, 0.0)), k) for k in FEATURES if features.get(k)),
                     key=lambda x: x[0])
    neg = [f"{k}" for v, k in contrib if v < -0.05][:top]
    pos = [f"{k}" for v, k in reversed(contrib) if v > 0.05][:top]
    return (("+ " + ", ".join(pos)) if pos else "") + (("  - " + ", ".join(neg)) if neg else "")


# ---------------------------------------------------------------------- feature extraction
def _content_words(text: str) -> list[str]:
    words = re.findall(r"[A-Za-z_][\w.\-]*|\d+(?:\.\d+)?|\"[^\"]*\"|'[^']*'", text)
    return [w for w in words if w.lower() not in _STOP]


def dry_run(ops: list[Op], state: SystemState) -> tuple[int, int, int, int]:
    """Apply the interpretation to a copy of the state: (errors, warnings, question, inferred)."""
    from .interpreter import Interpreter
    clone = state.clone()
    it = Interpreter(clone)
    it.quiet_followups = True
    try:
        res = it.apply_all([op for op in ops if op.action not in ("SHELL",)])
    except Exception:
        return 1, 0, 0, 0
    errors = sum(1 for m in res.messages if m.startswith("error"))
    warnings = sum(1 for m in res.messages if "warning" in m)
    inferred = 0
    for op in ops:
        if op.action == "CREATE" and not op.domain:
            inferred += 1
    return errors, warnings, int(res.question is not None), inferred


def rule_features(pr, text: str, state: SystemState, kb=None, vocab=None) -> dict:
    words = _content_words(text)
    n = max(1, len(words))
    unknown = len(set(w.lower() for w in pr.unknown))
    f = {"coverage": max(0.0, 1.0 - unknown / n), "unknown": min(3, unknown),
         "errors": min(3, len(pr.errors)), "notes": min(2, len(pr.notes)), "n_ops": min(6, len(pr.ops))}
    if not pr.ops:
        f["coverage"] = 0.0
    rel, frel = [], []
    for _clause, frame, toks in pr.clauses:
        if kb is not None and frame:
            frel.append(kb.reliability(f"frame({frame.lower()})"))
        for t in toks:
            if t.tag not in ("WORD", "NUMBER", "QUOTED", "DOMAIN", "PROTOCOL", "ARTICLE"):
                key = f"token({t.tag.lower()})"
                rel.append(kb.reliability(key) if kb is not None else 0.9)
    f["reliability"] = sum(rel) / len(rel) if rel else 0.75
    f["frame_rel"] = sum(frel) / len(frel) if frel else 0.75
    if pr.ops:
        e, w, q, inf = dry_run(pr.ops, state)
        f.update({"dry_errors": min(3, e), "warnings": min(4, w), "question": q, "inferred": min(3, inf)})
        f["fuzzy"] = _fuzzy_refs(pr.ops, state)
        f["novel"] = min(3, _novel_props(pr.ops, state))
    verbs = ("CREATE", "DELETE", "SET", "CONNECT", "DISCONNECT", "RENAME", "MOVE", "SHOW", "BUILD", "RUN", "UNDO",
             "DEFINE", "USE", "WHEN", "MACRO")
    if pr.clauses and not any(t.tag in verbs for _c, _f, toks in pr.clauses for t in toks) \
            and not any(fr in ("BEHAVIOR", "SPECIAL", "DEFINE_WORD", "FIX") for _c, fr, _t in pr.clauses):
        f["verbless"] = 1
    return f


_KNOWN_PROPS: set[str] | None = None


def known_props() -> set[str]:
    global _KNOWN_PROPS
    if _KNOWN_PROPS is None:
        from . import modules as modreg
        k = set(COMMON_PROPS)
        for m in modreg.load_modules().values():
            k |= set(m.suggested_props)
            for d in m.kinds.values():
                k |= set(d)
        _KNOWN_PROPS = k
    return _KNOWN_PROPS


def _novel_props(ops: list[Op], state: SystemState) -> int:
    """Property names nobody defined (no module, no entity has them) introduced as bare flags."""
    have = known_props() | {k for e in state.entities.values() for k in e.props}
    n = 0
    for op in ops:
        if op.action in ("SET", "CREATE"):
            for k, v in op.props.items():
                if k.startswith("__") or k == "subtype":
                    continue
                if k not in have and (v is True or "_" in k):
                    n += 1
    return n


def _fuzzy_refs(ops: list[Op], state: SystemState) -> int:
    """References that only resolve by partial matching ("the temperature sensor" -> temp)."""
    from .ir import ident
    n = 0
    names = {e.name.lower() for e in state.entities.values()} | {e.ident for e in state.entities.values()} | \
            {e.kind.lower() for e in state.entities.values()} | {e.id for e in state.entities.values()}
    for op in ops:
        for ref in (op.target, op.dst):
            if not ref or str(ref).lower() in ("it", "this", "that", "them") or str(ref).startswith("*"):
                continue
            r = str(ref).lower().replace("the ", "")
            if r not in names and ident(r) not in names and state.resolve(ref) is not None:
                n += 1
    return min(2, n)


def shell_features(op: Op, info: dict, root, kb=None) -> dict:
    from pathlib import Path
    s = op.props
    missing = 0
    root = Path(root)
    for key in ("path", "src", "dir"):
        v = s.get(key)
        if not v or v in (".",):
            continue
        must_exist = not (s["intent"] in ("make_dir", "make_file") and key == "path")
        if must_exist and not (root / v).exists():
            missing += 1
    rel = kb.reliability(f"intent({s['intent']})") if kb is not None else 0.85
    return {"coverage": 1.0, "shell": 1, "reliability": rel, "frame_rel": rel,
            "missing_paths": min(2, missing), "n_ops": 1}


def llm_features(ops: list[Op], state: SystemState) -> dict:
    e, w, q, inf = dry_run(ops, state)
    return {"coverage": 1.0 if ops else 0.0, "llm": 1, "reliability": 0.75, "frame_rel": 0.75,
            "dry_errors": min(3, e), "warnings": min(4, w), "question": q, "n_ops": min(6, len(ops))}


# ---------------------------------------------------------------------- learning the weights
def fit(samples: list[tuple[dict, int]], params: dict | None = None, epochs: int = 400,
        lr: float = 0.15, l2: float = 0.02) -> dict:
    """Logistic regression by gradient descent (stdlib only). samples: (features, 1 if right else 0)."""
    p = dict(DEFAULT_PARAMS)
    p.update(params or {})
    keys = ["bias"] + list(FEATURES)
    if not samples:
        return p
    for _ in range(epochs):
        grad = {k: 0.0 for k in keys}
        for feats, y in samples:
            pr = score(feats, p)
            err = pr - y
            grad["bias"] += err
            for k in FEATURES:
                grad[k] += err * float(feats.get(k, 0.0))
        n = len(samples)
        for k in keys:
            reg = l2 * (p[k] - DEFAULT_PARAMS.get(k, 0.0)) if k != "bias" else 0.0
            p[k] -= lr * (grad[k] / n + reg)
    return p


def calibration(scores: list[float], labels: list[int], bins: int = 5) -> dict:
    """Expected calibration error, Brier score and AUROC of the weights."""
    n = len(scores)
    if not n:
        return {"ece": None, "brier": None, "auroc": None}
    ece = 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        idx = [i for i, s in enumerate(scores) if (lo <= s < hi) or (b == bins - 1 and s == 1.0)]
        if idx:
            conf = sum(scores[i] for i in idx) / len(idx)
            acc = sum(labels[i] for i in idx) / len(idx)
            ece += len(idx) / n * abs(conf - acc)
    brier = sum((s - y) ** 2 for s, y in zip(scores, labels)) / n
    pos = [s for s, y in zip(scores, labels) if y]
    neg = [s for s, y in zip(scores, labels) if not y]
    if pos and neg:
        wins = sum(1.0 if p > q else 0.5 if p == q else 0.0 for p in pos for q in neg)
        auroc = wins / (len(pos) * len(neg))
    else:
        auroc = None
    return {"ece": round(ece, 4), "brier": round(brier, 4), "auroc": round(auroc, 4) if auroc is not None else None}


def decision(weight: float, execute_at: float = EXECUTE_AT, ask_below: float = ASK_BELOW) -> str:
    if weight >= execute_at:
        return "execute"
    if weight >= ask_below:
        return "execute_shown"
    return "ask"
