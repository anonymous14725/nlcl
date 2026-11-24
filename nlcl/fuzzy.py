"""Fuzzy meanings of vague words: "fast", "very bright", "a bit slower", "when health is low".

A vague adjective names a fuzzy set on a property's range; the range depends on what is described
(a fast hero is not a fast car), and both live in the Prolog KB (vague/3, range/4, hedge/2,
comparative/3, amount/2).  Values are obtained by centroid defuzzification; hedges ("very",
"somewhat") are concentration / dilation (membership ** exponent); comparatives are a relative change
of the range ("a bit faster" = +10% of the range); conditions use the 0.5 alpha-cut of the set.
"""
from __future__ import annotations

import re

KB = None          # set by the runtime (a Knowledge instance)

# term -> trapezoid (a, b, c, d) on the normalised range [0, 1]
TERMS = {"low": (0.0, 0.0, 0.2, 0.4), "medium": (0.3, 0.45, 0.55, 0.7), "high": (0.6, 0.8, 1.0, 1.0)}
HEDGES_DEFAULT = {"very": 2.0, "really": 2.0, "super": 3.0, "extremely": 3.0, "quite": 1.5, "pretty": 1.5,
                  "fairly": 0.75, "somewhat": 0.5, "rather": 0.75, "slightly": 0.5, "a_bit": 0.5, "a_little": 0.5}


def _kb():
    global KB
    if KB is None:
        from . import modules as modreg
        from .knowledge import Knowledge
        KB = Knowledge(None, modreg.load_modules())
    return KB


def membership(term: str, x: float) -> float:
    a, b, c, d = TERMS[term]
    if x < a or x > d:
        return 0.0
    if b <= x <= c:
        return 1.0
    if x < b:
        return (x - a) / (b - a) if b > a else 1.0
    return (d - x) / (d - c) if d > c else 1.0


def centroid(term: str, exponent: float = 1.0, steps: int = 400) -> float:
    num = den = 0.0
    for i in range(steps + 1):
        x = i / steps
        m = membership(term, x) ** exponent
        num += m * x
        den += m
    return num / den if den else 0.5


def alpha_cut(term: str, exponent: float = 1.0, alpha: float = 0.5) -> tuple[float, float]:
    """Interval where membership ** exponent >= alpha (used for conditions)."""
    xs = [i / 400 for i in range(401) if membership(term, i / 400) ** exponent >= alpha]
    return (xs[0], xs[-1]) if xs else (0.0, 1.0)


# ---------------------------------------------------------------------- KB lookups
def adjective(word: str) -> tuple[str, str] | None:
    """'fast' -> ('speed', 'high'); 'low' -> ('any', 'low')."""
    r = _kb().query(f"vague({_kb().to_term(word.lower())}, P, T)", limit=1)
    return (r[0]["P"], r[0]["T"]) if r else None


def comparative(word: str) -> tuple[str, str] | None:
    r = _kb().query(f"comparative({_kb().to_term(word.lower())}, P, D)", limit=1)
    return (r[0]["P"], r[0]["D"]) if r else None


def hedge(phrase: str | None) -> float:
    if not phrase:
        return 1.0
    key = re.sub(r"\s+", "_", phrase.strip().lower())
    r = _kb().query(f"hedge({_kb().to_term(key)}, E)", limit=1)
    return float(r[0]["E"]) if r else HEDGES_DEFAULT.get(key, 1.0)


def amount(phrase: str | None) -> float:
    key = re.sub(r"\s+", "_", (phrase or "default").strip().lower())
    r = _kb().query(f"amount({_kb().to_term(key)}, A)", limit=1)
    return float(r[0]["A"]) if r else 0.2


def value_range(prop: str, kind: str | None = None, domain: str | None = None, current=None) -> tuple[float, float]:
    k = _kb()
    r = k.query(f"range_for({k.to_term(kind or 'none')}, {k.to_term(domain or 'none')}, {k.to_term(prop)}, L, H)", limit=1)
    if r:
        return float(r[0]["L"]), float(r[0]["H"])
    cur = float(current) if isinstance(current, (int, float)) and not isinstance(current, bool) else 0.0
    return 0.0, max(1.0, 2 * cur) if cur > 0 else 100.0


def _num(v: float):
    v = round(v, 2)
    return int(v) if float(v).is_integer() else v


# ---------------------------------------------------------------------- meanings
HEDGE_WORDS = r"(?:very|really|super|extremely|incredibly|quite|pretty|fairly|somewhat|rather|slightly|kind\s+of|a\s+bit|a\s+little)"
AMOUNT_WORDS = r"(?:a\s+bit|a\s+little|slightly|somewhat|much|a\s+lot|way|far)"


def set_value(entity, adj: str, hedge_phrase: str | None = None, prop: str | None = None):
    """-> (prop, value, explanation) for 'make <entity> <hedge> <adj>'."""
    a = adjective(adj)
    if not a:
        return None
    p, term = a
    if p == "any":
        if not prop:
            return None
        p = prop
    lo, hi = value_range(p, getattr(entity, "kind", None), getattr(entity, "domain", None),
                         getattr(entity, "props", {}).get(p))
    c = centroid(term, hedge(hedge_phrase))
    v = _num(lo + c * (hi - lo))
    what = getattr(entity, "kind", "it")
    return p, v, f"{(hedge_phrase + ' ') if hedge_phrase else ''}{adj} for a {what}: {_num(lo)}..{_num(hi)}"


def change_value(entity, word: str, amount_phrase: str | None = None, factor: float | None = None, prop: str | None = None):
    """-> (prop, new_value, explanation) for 'make <entity> <amount> <comparative>' / 'twice as fast'."""
    c = comparative(word)
    if not c:
        a = adjective(word)                              # "twice as fast": factor on the property of 'fast'
        if not a or factor is None:
            return None
        c = (a[0], "up")
    p, direction = c
    if p == "any":
        if not prop:
            return None
        p = prop
    props = getattr(entity, "props", {})
    lo, hi = value_range(p, getattr(entity, "kind", None), getattr(entity, "domain", None), props.get(p))
    cur = props.get(p)
    if not isinstance(cur, (int, float)) or isinstance(cur, bool):
        cur = lo + 0.5 * (hi - lo)
    if factor is not None:
        new = cur * factor
        how = f"x{_num(factor)}"
    else:
        share = amount(amount_phrase)
        delta = share * (hi - lo) * (1 if direction == "up" else -1)
        new = cur + delta
        how = f"{'+' if delta >= 0 else ''}{_num(delta)} ({int(share * 100)}% of {_num(lo)}..{_num(hi)})"
    new = max(lo, min(hi, new))
    return p, _num(new), how


def delta_for(entity, word: str, amount_phrase: str | None = None) -> tuple[str, float] | None:
    """Relative change as a fixed delta (for behaviours: 'when it rains, make hero faster')."""
    c = comparative(word)
    if not c or c[0] == "any":
        return None
    p, direction = c
    lo, hi = value_range(p, getattr(entity, "kind", None), getattr(entity, "domain", None),
                         getattr(entity, "props", {}).get(p))
    return p, _num(amount(amount_phrase) * (hi - lo) * (1 if direction == "up" else -1))


def condition(subject_expr: str, adj: str, hedge_phrase: str | None, entity) -> str | None:
    """'health is low' -> 'health <= 30'; 'hero is fast' -> 'speed >= 14' (alpha-cut 0.5)."""
    a = adjective(adj)
    if not a:
        return None
    p, term = a
    if p == "any":
        expr = subject_expr
        p = subject_expr.split(".")[-1]
    else:
        expr = (subject_expr + "." + p) if subject_expr and subject_expr not in ("it", "self") else p
    lo, hi = value_range(p, getattr(entity, "kind", None), getattr(entity, "domain", None),
                         getattr(entity, "props", {}).get(p) if entity is not None else None)
    a0, a1 = alpha_cut(term, hedge(hedge_phrase))
    x0, x1 = _num(lo + a0 * (hi - lo)), _num(lo + a1 * (hi - lo))
    if term == "low":
        return f"{expr} <= {x1}"
    if term == "high":
        return f"{expr} >= {x0}"
    return f"({expr} >= {x0} && {expr} <= {x1})"
