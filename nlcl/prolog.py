"""A small, dependency-free Prolog engine (ISO subset) for NLCL's semantic knowledge base.

Supported: facts and rules, unification, backtracking (SLD resolution), cut (!), conjunction,
disjunction, if-then-else (->), negation as failure (\\+), arithmetic (is, < > =< >= =:= =\\=),
lists [H|T], quoted atoms, strings, % comments, assert/retract at run time, and builtins:
true fail = \\= == \\== var nonvar atom number atomic is_list member memberchk append length
nth0 nth1 findall bagof(simple) setof(simple) msort sort sum_list max_list min_list between
atom_concat atom_length atom_chars atom_number sub_atom(simple) atom_string upcase_atom
downcase_atom number_codes(simple) format(simple) writeln write nl succ_or_zero assertz asserta
retract once forall call/N.

The knowledge base files are plain Prolog, so SWI-Prolog (free) can load them too.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterator


# ------------------------------------------------------------------ terms
class Var:
    __slots__ = ("name", "id")
    _n = 0

    def __init__(self, name: str = "_"):
        Var._n += 1
        self.name, self.id = name, Var._n

    def __repr__(self):
        return f"_{self.name}{self.id}"


@dataclass(frozen=True)
class Atom:
    name: str

    def __repr__(self):
        return fmt(self)


@dataclass(frozen=True)
class Struct:
    name: str
    args: tuple

    def __repr__(self):
        return fmt(self)


NIL = Atom("[]")
TRUE = Atom("true")


def mk_list(items, tail=NIL):
    out = tail
    for x in reversed(list(items)):
        out = Struct(".", (x, out))
    return out


class PrologError(Exception):
    pass


class _Cut(Exception):
    """Raised to cut back to the clause that owns the barrier."""
    def __init__(self, barrier):
        self.barrier = barrier


# ------------------------------------------------------------------ bindings
def deref(t, env):
    while isinstance(t, Var) and t in env:
        t = env[t]
    return t


def resolve(t, env):
    t = deref(t, env)
    if isinstance(t, Struct):
        return Struct(t.name, tuple(resolve(a, env) for a in t.args))
    return t


def unify(a, b, env) -> dict | None:
    stack = [(a, b)]
    env = dict(env)
    while stack:
        x, y = stack.pop()
        x, y = deref(x, env), deref(y, env)
        if x is y:
            continue
        if isinstance(x, Var):
            env[x] = y
        elif isinstance(y, Var):
            env[y] = x
        elif isinstance(x, Struct) and isinstance(y, Struct):
            if x.name != y.name or len(x.args) != len(y.args):
                return None
            stack.extend(zip(x.args, y.args))
        elif isinstance(x, (int, float)) and isinstance(y, (int, float)) and not isinstance(x, bool):
            if type(x) is not type(y) or x != y:
                return None
        elif x != y:
            return None
    return env


def rename(t, mapping):
    if isinstance(t, Var):
        if t not in mapping:
            mapping[t] = Var(t.name)
        return mapping[t]
    if isinstance(t, Struct):
        return Struct(t.name, tuple(rename(a, mapping) for a in t.args))
    return t


# ------------------------------------------------------------------ printing / conversion
_PLAIN = re.compile(r"^[a-z][A-Za-z0-9_]*$")


def fmt(t, env=None) -> str:
    t = deref(t, env or {})
    if isinstance(t, Var):
        return f"_G{t.id}"
    if isinstance(t, bool):
        return "true" if t else "false"
    if isinstance(t, (int, float)):
        return repr(t)
    if isinstance(t, Atom):
        if t.name == "[]" or _PLAIN.match(t.name) or re.fullmatch(r"[+\-*/\\^<>=~:.?@#&$]+", t.name) or t.name in ("!", ";", ","):
            return t.name
        return "'" + t.name.replace("\\", "\\\\").replace("'", "\\'") + "'"
    if isinstance(t, Struct):
        if t.name == "." and len(t.args) == 2:
            items, cur = [], t
            while isinstance(cur, Struct) and cur.name == "." and len(cur.args) == 2:
                items.append(fmt(cur.args[0], env))
                cur = deref(cur.args[1], env or {})
            tail = "" if cur == NIL else "|" + fmt(cur, env)
            return "[" + ",".join(items) + tail + "]"
        if len(t.args) == 2 and t.name in _INFIX:
            return f"{fmt(t.args[0], env)}{_spaced(t.name)}{fmt(t.args[1], env)}"
        return fmt(Atom(t.name)) + "(" + ",".join(fmt(a, env) for a in t.args) + ")"
    return str(t)


def _spaced(op):
    return op if op in (",",) else f" {op} "


def to_python(t, env=None):
    """Prolog term -> Python value (atoms -> str, lists -> list, compound -> (name, *args))."""
    t = deref(t, env or {})
    if isinstance(t, Var):
        return None
    if isinstance(t, Atom):
        return [] if t == NIL else t.name
    if isinstance(t, Struct):
        if t.name == "." and len(t.args) == 2:
            out, cur = [], t
            while isinstance(cur, Struct) and cur.name == "." and len(cur.args) == 2:
                out.append(to_python(cur.args[0], env))
                cur = deref(cur.args[1], env or {})
            return out
        return (t.name,) + tuple(to_python(a, env) for a in t.args)
    return t


def from_python(v):
    if isinstance(v, (Atom, Struct, Var)):
        return v
    if isinstance(v, bool):
        return Atom("true" if v else "false")
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, (list, tuple)) and not (isinstance(v, tuple) and v and isinstance(v[0], str) and False):
        return mk_list([from_python(x) for x in v])
    if v is None:
        return Atom("none")
    return Atom(str(v))


# ------------------------------------------------------------------ parser
_OPS = {  # name: (priority, type)
    ":-": (1200, "xfx"), "-->": (1200, "xfx"), "?-": (1200, "fx"),
    ";": (1100, "xfy"), "|": (1100, "xfy"), "->": (1050, "xfy"), ",": (1000, "xfy"),
    "\\+": (900, "fy"),
    "=": (700, "xfx"), "\\=": (700, "xfx"), "==": (700, "xfx"), "\\==": (700, "xfx"), "is": (700, "xfx"),
    "<": (700, "xfx"), ">": (700, "xfx"), "=<": (700, "xfx"), ">=": (700, "xfx"), "=:=": (700, "xfx"),
    "=\\=": (700, "xfx"), "=..": (700, "xfx"), "@<": (700, "xfx"), "@>": (700, "xfx"),
    "+": (500, "yfx"), "-": (500, "yfx"), "*": (400, "yfx"), "/": (400, "yfx"), "//": (400, "yfx"),
    "mod": (400, "yfx"), "**": (200, "xfx"), "^": (200, "xfy"),
}
_PREFIX = {"-": (200, "fy"), "\\+": (900, "fy"), "?-": (1200, "fx"), ":-": (1200, "fx"),
           "dynamic": (1150, "fx"), "discontiguous": (1150, "fx")}
_INFIX = {k for k, (p, t) in _OPS.items() if t in ("xfx", "xfy", "yfx")}

_TOKEN = re.compile(r"""
    (?P<ws>\s+|%[^\n]*) |
    (?P<num>\d+\.\d+(?:[eE][+-]?\d+)?|\d+) |
    (?P<var>[A-Z_][A-Za-z0-9_]*) |
    (?P<atom>[a-z][A-Za-z0-9_]*) |
    (?P<qatom>'(?:[^'\\]|\\.|'')*') |
    (?P<str>"(?:[^"\\]|\\.)*") |
    (?P<punct>[()\[\],|!;]) |
    (?P<sym>[+\-*/\\^<>=~:.?@#&$]+)
""", re.X | re.S)


def _unescape(s: str) -> str:
    return re.sub(r"\\(.)|''", lambda m: {"n": "\n", "t": "\t", "\\": "\\", "'": "'", '"': '"'}.get(m.group(1), m.group(1)) if m.group(1) else "'", s)


def tokenize(text: str):
    toks, pos = [], 0
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m:
            raise PrologError(f"syntax error near {text[pos:pos + 30]!r}")
        pos = m.end()
        kind = m.lastgroup
        val = m.group(kind)
        if kind == "ws":
            continue
        if kind == "qatom":
            val, kind = _unescape(val[1:-1]), "atom"
        elif kind == "str":
            val, kind = _unescape(val[1:-1]), "atom"
        elif kind == "sym" and val.endswith(".") and (pos >= len(text) or text[pos].isspace() or text[pos] == "%"):
            if len(val) > 1:
                toks.append(("sym", val[:-1]))
            val, kind = ".", "end"
        elif kind == "num":
            val = float(val) if "." in val else int(val)
        toks.append((kind, val))
    return toks


class _Parser:
    def __init__(self, toks):
        self.toks, self.i, self.vars = toks, 0, {}

    def peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else ("eof", None)

    def take(self):
        t = self.peek()
        self.i += 1
        return t

    def expect(self, val):
        t = self.take()
        if t[1] != val:
            raise PrologError(f"expected {val!r}, got {t[1]!r}")

    def clause(self):
        self.vars = {}
        t = self.parse(1200)
        if self.take()[0] != "end":
            raise PrologError("expected '.' at end of clause")
        return t

    def parse(self, maxp):
        left, lp = self.primary(maxp)
        while True:
            kind, val = self.peek()
            name = val if kind in ("sym", "atom") or (kind == "punct" and val in (",", "|", ";")) else None
            if name not in _OPS or kind == "end":
                break
            p, typ = _OPS[name]
            if typ not in ("xfx", "xfy", "yfx") or p > maxp:
                break
            la = p - 1 if typ in ("xfx", "xfy") else p
            if lp > la:
                break
            self.take()
            ra = p if typ == "xfy" else p - 1
            right = self.parse(ra)
            if name == "|":
                name = ";"
            left, lp = Struct(name, (left, right)), p
        return left

    def primary(self, maxp):
        kind, val = self.take()
        if kind == "num":
            return val, 0
        if kind == "var":
            if val == "_":
                return Var("_"), 0
            if val not in self.vars:
                self.vars[val] = Var(val)
            return self.vars[val], 0
        if kind == "punct" and val == "(":
            t = self.parse(1200)
            self.expect(")")
            return t, 0
        if kind == "punct" and val == "[":
            if self.peek()[1] == "]":
                self.take()
                return self._after_atom("[]", maxp)
            items = [self.parse(999)]
            while self.peek()[1] == ",":
                self.take()
                items.append(self.parse(999))
            tail = NIL
            if self.peek()[1] == "|":
                self.take()
                tail = self.parse(999)
            self.expect("]")
            return mk_list(items, tail), 0
        if kind == "punct" and val in ("!", ";"):
            return self._after_atom(val, maxp) if val == ";" else (Atom("!"), 0)
        if kind in ("atom", "sym") or (kind == "punct" and val == ","):
            return self._after_atom(val, maxp)
        raise PrologError(f"unexpected {val!r}")

    def _after_atom(self, name, maxp):
        if self.peek() == ("punct", "("):
            self.take()
            args = [self.parse(999)]
            while self.peek()[1] == ",":
                self.take()
                args.append(self.parse(999))
            self.expect(")")
            return Struct(name, tuple(args)), 0
        if name == "-" and isinstance(self.peek()[1], (int, float)) and self.peek()[0] == "num":
            return -self.take()[1], 0
        if name in _PREFIX:
            p, typ = _PREFIX[name]
            nk, nv = self.peek()
            starts = nk in ("num", "var", "atom") or (nk == "punct" and nv in ("(", "[")) or (nk == "sym" and nv not in _OPS)
            if starts and p <= maxp:
                arg = self.parse(p if typ == "fy" else p - 1)
                return Struct(name, (arg,)), p
        p = _OPS.get(name, (0,))[0]
        return Atom(name), (p if p <= maxp else 0)


def parse_program(text: str) -> list:
    p = _Parser(tokenize(text))
    out = []
    while p.peek()[0] != "eof":
        out.append(p.clause())
    return out


def parse_term(text: str):
    text = text.strip()
    if not text.endswith("."):
        text += " ."
    p = _Parser(tokenize(text))
    t = p.clause()
    return t, p.vars


# ------------------------------------------------------------------ engine
def _arith(t, env):
    t = deref(t, env)
    if isinstance(t, (int, float)) and not isinstance(t, bool):
        return t
    if isinstance(t, Var):
        raise PrologError("arguments are not sufficiently instantiated")
    if isinstance(t, Atom):
        consts = {"pi": 3.141592653589793, "e": 2.718281828459045, "inf": float("inf")}
        if t.name in consts:
            return consts[t.name]
        raise PrologError(f"type_error(evaluable, {t.name}/0)")
    if isinstance(t, Struct):
        a = [_arith(x, env) for x in t.args]
        n, k = t.name, len(a)
        if k == 2:
            x, y = a
            if n == "+": return x + y
            if n == "-": return x - y
            if n == "*": return x * y
            if n == "/":
                if y == 0:
                    raise PrologError("evaluation_error(zero_divisor)")
                r = x / y
                return int(r) if isinstance(x, int) and isinstance(y, int) and x % y == 0 else r
            if n == "//": return int(x // y) if (x >= 0) == (y >= 0) else -int(abs(x) // abs(y))
            if n == "mod": return x % y
            if n in ("**", "^"): return x ** y
            if n == "min": return min(x, y)
            if n == "max": return max(x, y)
        if k == 1:
            x = a[0]
            if n == "-": return -x
            if n == "abs": return abs(x)
            if n == "round": return int(round(x))
            if n == "truncate": return int(x)
            if n == "float": return float(x)
            if n == "integer": return int(round(x))
            if n == "sqrt": return x ** 0.5
            if n == "log":
                import math
                return math.log(x)
    raise PrologError(f"type_error(evaluable, {fmt(t)})")


def _text(t, env) -> str:
    t = deref(t, env)
    if isinstance(t, Atom):
        return t.name
    if isinstance(t, (int, float)):
        return repr(t)
    if isinstance(t, Struct) and t.name == ".":
        return "".join(str(x) for x in to_python(t, env))
    raise PrologError("atom expected")


class Prolog:
    def __init__(self, max_depth: int = 4000):
        self.db: dict[tuple, list] = {}
        self.max_depth = max_depth
        self.output: list[str] = []

    # ---- loading
    def consult_text(self, text: str) -> int:
        n = 0
        for c in parse_program(text):
            if isinstance(c, Struct) and c.name == ":-" and len(c.args) == 1:
                for _ in self.solve(c.args[0], {}):     # directive
                    break
                continue
            self.add_clause(c)
            n += 1
        return n

    def consult(self, path) -> int:
        from pathlib import Path
        return self.consult_text(Path(path).read_text(encoding="utf-8"))

    def add_clause(self, c, front: bool = False):
        head = c.args[0] if isinstance(c, Struct) and c.name == ":-" and len(c.args) == 2 else c
        key = self._key(head)
        if key in _BUILTIN_KEYS:
            raise PrologError(f"permission_error: cannot redefine builtin {key[0]}/{key[1]}")
        lst = self.db.setdefault(key, [])
        lst.insert(0, c) if front else lst.append(c)

    @staticmethod
    def _key(t):
        if isinstance(t, Atom):
            return (t.name, 0)
        if isinstance(t, Struct):
            return (t.name, len(t.args))
        raise PrologError(f"callable expected, got {fmt(t)}")

    # ---- querying
    def query(self, text: str, limit: int | None = None) -> list[dict]:
        """Run a query given as text; return a list of {VarName: python value} answers."""
        goal, names = parse_term(text)
        out = []
        for env in self.solve(goal, {}):
            out.append({n: to_python(v, env) for n, v in names.items()
                        if not n.startswith("_") and not isinstance(deref(v, env), Var)})
            if limit and len(out) >= limit:
                break
        return out

    def ask(self, text: str) -> bool:
        return bool(self.query(text, limit=1))

    def facts(self, name: str, arity: int) -> list[tuple]:
        return [tuple(to_python(a) for a in c.args) if isinstance(c, Struct) else ()
                for c in self.db.get((name, arity), []) if not (isinstance(c, Struct) and c.name == ":-")]

    def assertz(self, fact_text_or_term):
        t = parse_term(fact_text_or_term)[0] if isinstance(fact_text_or_term, str) else fact_text_or_term
        self.add_clause(t)

    def retract_all(self, name: str, arity: int, pred=None):
        lst = self.db.get((name, arity), [])
        keep = [c for c in lst if pred is not None and not pred(c)]
        self.db[(name, arity)] = keep

    def solve(self, goal, env, depth: int = 0) -> Iterator[dict]:
        barrier = object()
        try:
            yield from self._solve(goal, env, depth, barrier)
        except _Cut as c:
            if c.barrier is not barrier:
                raise

    def _solve(self, goal, env, depth, barrier) -> Iterator[dict]:
        if depth > self.max_depth:
            raise PrologError("depth limit exceeded (infinite recursion?)")
        goal = deref(goal, env)
        if isinstance(goal, Var):
            raise PrologError("arguments are not sufficiently instantiated")
        if isinstance(goal, (int, float)):
            raise PrologError(f"type_error(callable, {goal})")
        name = goal.name
        args = goal.args if isinstance(goal, Struct) else ()
        # ---- control constructs
        if name == "true" and not args:
            yield env
            return
        if name in ("fail", "false") and not args:
            return
        if name == "!" and not args:
            yield env
            raise _Cut(barrier)
        if name == "," and len(args) == 2:
            for e1 in self._solve(args[0], env, depth + 1, barrier):
                yield from self._solve(args[1], e1, depth + 1, barrier)
            return
        if name == ";" and len(args) == 2:
            left = deref(args[0], env)
            if isinstance(left, Struct) and left.name == "->" and len(left.args) == 2:
                for e1 in self.solve(left.args[0], env, depth + 1):
                    yield from self._solve(left.args[1], e1, depth + 1, barrier)
                    return
                yield from self._solve(args[1], env, depth + 1, barrier)
                return
            yield from self._solve(args[0], env, depth + 1, barrier)
            yield from self._solve(args[1], env, depth + 1, barrier)
            return
        if name == "->" and len(args) == 2:
            for e1 in self.solve(args[0], env, depth + 1):
                yield from self._solve(args[1], e1, depth + 1, barrier)
                return
            return
        if name == "\\+" and len(args) == 1:
            for _ in self.solve(args[0], env, depth + 1):
                return
            yield env
            return
        if name in ("call", "once", "ignore", "not") and args:
            g = deref(args[0], env)
            if len(args) > 1:
                g = Struct(g.name if isinstance(g, (Atom, Struct)) else str(g),
                           (g.args if isinstance(g, Struct) else ()) + tuple(args[1:]))
            if name == "call":
                yield from self.solve(g, env, depth + 1)
            elif name == "once":
                for e1 in self.solve(g, env, depth + 1):
                    yield e1
                    return
            elif name == "not":
                for _ in self.solve(g, env, depth + 1):
                    return
                yield env
            else:   # ignore
                for e1 in self.solve(g, env, depth + 1):
                    yield e1
                    return
                yield env
            return
        if name == "forall" and len(args) == 2:
            for e1 in self.solve(args[0], env, depth + 1):
                if not any(True for _ in self.solve(args[1], e1, depth + 1)):
                    return
            yield env
            return
        # ---- builtins
        b = _BUILTINS.get((name, len(args)))
        if b is not None:
            yield from b(self, args, env, depth)
            return
        # ---- user clauses
        key = (name, len(args))
        clauses = self.db.get(key)
        if clauses is None:
            raise PrologError(f"existence_error: unknown procedure {name}/{len(args)}")
        inner = object()
        try:
            for c in list(clauses):
                c = rename(c, {})
                if isinstance(c, Struct) and c.name == ":-" and len(c.args) == 2:
                    head, body = c.args
                else:
                    head, body = c, TRUE
                e1 = unify(head, goal, env)
                if e1 is None:
                    continue
                yield from self._solve(body, e1, depth + 1, inner)
        except _Cut as cut:
            if cut.barrier is not inner:
                raise


# ------------------------------------------------------------------ builtins
def _b(name, arity):
    def deco(fn):
        _BUILTINS[(name, arity)] = fn
        return fn
    return deco


_BUILTINS: dict = {}


def _det(fn):
    """Builtin that succeeds at most once: fn returns env or None."""
    def g(pl, args, env, depth):
        r = fn(pl, args, env)
        if r is not None:
            yield r
    return g


def _unify_b(pl, a, env):
    r = unify(a[0], a[1], env)
    return r


_BUILTINS[("=", 2)] = _det(_unify_b)
_BUILTINS[("\\=", 2)] = _det(lambda pl, a, env: env if unify(a[0], a[1], env) is None else None)
_BUILTINS[("==", 2)] = _det(lambda pl, a, env: env if resolve(a[0], env) == resolve(a[1], env) and fmt(a[0], env) == fmt(a[1], env) else None)
_BUILTINS[("\\==", 2)] = _det(lambda pl, a, env: None if fmt(a[0], env) == fmt(a[1], env) else env)
_BUILTINS[("var", 1)] = _det(lambda pl, a, env: env if isinstance(deref(a[0], env), Var) else None)
_BUILTINS[("nonvar", 1)] = _det(lambda pl, a, env: None if isinstance(deref(a[0], env), Var) else env)
_BUILTINS[("atom", 1)] = _det(lambda pl, a, env: env if isinstance(deref(a[0], env), Atom) else None)
_BUILTINS[("number", 1)] = _det(lambda pl, a, env: env if isinstance(deref(a[0], env), (int, float)) else None)
_BUILTINS[("integer", 1)] = _det(lambda pl, a, env: env if isinstance(deref(a[0], env), int) else None)
_BUILTINS[("atomic", 1)] = _det(lambda pl, a, env: env if isinstance(deref(a[0], env), (Atom, int, float)) else None)
_BUILTINS[("is_list", 1)] = _det(lambda pl, a, env: env if isinstance(to_python(a[0], env), list) else None)
_BUILTINS[("is", 2)] = _det(lambda pl, a, env: unify(a[0], _arith(a[1], env), env))


def _cmp(op):
    return _det(lambda pl, a, env: env if op(_arith(a[0], env), _arith(a[1], env)) else None)


for _n, _f in (("<", lambda x, y: x < y), (">", lambda x, y: x > y), ("=<", lambda x, y: x <= y),
               (">=", lambda x, y: x >= y), ("=:=", lambda x, y: x == y), ("=\\=", lambda x, y: x != y)):
    _BUILTINS[(_n, 2)] = _cmp(_f)
_BUILTINS[("@<", 2)] = _det(lambda pl, a, env: env if fmt(a[0], env) < fmt(a[1], env) else None)
_BUILTINS[("@>", 2)] = _det(lambda pl, a, env: env if fmt(a[0], env) > fmt(a[1], env) else None)


@_b("member", 2)
def _member(pl, a, env, depth):
    lst = deref(a[1], env)
    while isinstance(lst, Struct) and lst.name == "." and len(lst.args) == 2:
        e1 = unify(a[0], lst.args[0], env)
        if e1 is not None:
            yield e1
        lst = deref(lst.args[1], env)


@_b("memberchk", 2)
def _memberchk(pl, a, env, depth):
    for e1 in _member(pl, a, env, depth):
        yield e1
        return


@_b("append", 3)
def _append(pl, a, env, depth):
    x = deref(a[0], env)
    if isinstance(x, Var):
        items = to_python(a[2], env)
        if not isinstance(items, list):
            return
        full = resolve(a[2], env)
        elems = []
        cur = full
        while isinstance(cur, Struct) and cur.name == ".":
            elems.append(cur.args[0])
            cur = cur.args[1]
        for i in range(len(elems) + 1):
            e1 = unify(a[0], mk_list(elems[:i]), env)
            if e1 is not None:
                e2 = unify(a[1], mk_list(elems[i:]), e1)
                if e2 is not None:
                    yield e2
        return
    elems, cur = [], x
    while isinstance(cur, Struct) and cur.name == ".":
        elems.append(cur.args[0])
        cur = deref(cur.args[1], env)
    if cur != NIL:
        return
    e1 = unify(a[2], mk_list(elems, a[1]), env)
    if e1 is not None:
        yield e1


@_b("length", 2)
def _length(pl, a, env, depth):
    v = to_python(a[0], env)
    if isinstance(v, list):
        e1 = unify(a[1], len(v), env)
        if e1 is not None:
            yield e1
        return
    n = deref(a[1], env)
    if isinstance(n, int):
        e1 = unify(a[0], mk_list([Var() for _ in range(n)]), env)
        if e1 is not None:
            yield e1


def _nth(base):
    def fn(pl, a, env, depth):
        lst = to_python(a[1], env)
        full = resolve(a[1], env)
        elems, cur = [], full
        while isinstance(cur, Struct) and cur.name == ".":
            elems.append(cur.args[0])
            cur = cur.args[1]
        i = deref(a[0], env)
        rng = [i - base] if isinstance(i, int) else range(len(elems))
        for k in rng:
            if 0 <= k < len(elems):
                e1 = unify(a[0], k + base, env)
                if e1 is not None:
                    e2 = unify(a[2], elems[k], e1)
                    if e2 is not None:
                        yield e2
        _ = lst
    return fn


_BUILTINS[("nth0", 3)] = _nth(0)
_BUILTINS[("nth1", 3)] = _nth(1)


@_b("findall", 3)
def _findall(pl, a, env, depth):
    res = [resolve(a[0], e) for e in pl.solve(a[1], env, depth + 1)]
    e1 = unify(a[2], mk_list(res), env)
    if e1 is not None:
        yield e1


def _collect_sorted(pl, a, env, depth, dedupe):
    res = [resolve(a[0], e) for e in pl.solve(a[1], env, depth + 1)]
    if not res:
        return
    res = sorted(res, key=lambda t: _sort_key(t))
    if dedupe:
        uniq, seen = [], set()
        for t in res:
            k = fmt(t)
            if k not in seen:
                seen.add(k)
                uniq.append(t)
        res = uniq
    e1 = unify(a[2], mk_list(res), env)
    if e1 is not None:
        yield e1


_BUILTINS[("bagof", 3)] = lambda pl, a, env, depth: _collect_sorted(pl, a, env, depth, False) \
    if False else _bagof(pl, a, env, depth)
_BUILTINS[("setof", 3)] = lambda pl, a, env, depth: _collect_sorted(pl, a, env, depth, True)


def _bagof(pl, a, env, depth):
    res = [resolve(a[0], e) for e in pl.solve(a[1], env, depth + 1)]
    if not res:
        return
    e1 = unify(a[2], mk_list(res), env)
    if e1 is not None:
        yield e1


def _sort_key(t):
    if isinstance(t, (int, float)):
        return (0, t, "")
    return (1, 0, fmt(t))


@_b("msort", 2)
def _msort(pl, a, env, depth):
    items = _elems(a[0], env)
    e1 = unify(a[1], mk_list(sorted(items, key=_sort_key)), env)
    if e1 is not None:
        yield e1


@_b("sort", 2)
def _sort(pl, a, env, depth):
    items, seen, out = sorted(_elems(a[0], env), key=_sort_key), set(), []
    for t in items:
        if fmt(t) not in seen:
            seen.add(fmt(t))
            out.append(t)
    e1 = unify(a[1], mk_list(out), env)
    if e1 is not None:
        yield e1


def _elems(t, env):
    out, cur = [], resolve(t, env)
    while isinstance(cur, Struct) and cur.name == ".":
        out.append(cur.args[0])
        cur = cur.args[1]
    return out


def _num_list(t, env):
    return [_arith(x, env) for x in _elems(t, env)]


_BUILTINS[("sum_list", 2)] = _det(lambda pl, a, env: unify(a[1], sum(_num_list(a[0], env)), env))
_BUILTINS[("sumlist", 2)] = _BUILTINS[("sum_list", 2)]
_BUILTINS[("max_list", 2)] = _det(lambda pl, a, env: unify(a[1], max(_num_list(a[0], env)), env) if _elems(a[0], env) else None)
_BUILTINS[("min_list", 2)] = _det(lambda pl, a, env: unify(a[1], min(_num_list(a[0], env)), env) if _elems(a[0], env) else None)


@_b("between", 3)
def _between(pl, a, env, depth):
    lo, hi = _arith(a[0], env), deref(a[1], env)
    hi = float("inf") if isinstance(hi, Atom) and hi.name in ("inf", "infinite") else _arith(hi, env)
    x = deref(a[2], env)
    if isinstance(x, int):
        if lo <= x <= hi:
            yield env
        return
    i = lo
    while i <= hi:
        e1 = unify(a[2], i, env)
        if e1 is not None:
            yield e1
        i += 1


@_b("atom_concat", 3)
def _atom_concat(pl, a, env, depth):
    x, y = deref(a[0], env), deref(a[1], env)
    if not isinstance(x, Var) and not isinstance(y, Var):
        e1 = unify(a[2], Atom(_text(x, env) + _text(y, env)), env)
        if e1 is not None:
            yield e1
        return
    whole = _text(a[2], env)
    for i in range(len(whole) + 1):
        e1 = unify(a[0], Atom(whole[:i]), env)
        if e1 is not None:
            e2 = unify(a[1], Atom(whole[i:]), e1)
            if e2 is not None:
                yield e2


_BUILTINS[("atom_length", 2)] = _det(lambda pl, a, env: unify(a[1], len(_text(a[0], env)), env))
_BUILTINS[("atom_chars", 2)] = _det(lambda pl, a, env: unify(a[1], mk_list([Atom(c) for c in _text(a[0], env)]), env)
                                     if not isinstance(deref(a[0], env), Var) else unify(a[0], Atom(_text(a[1], env)), env))
_BUILTINS[("upcase_atom", 2)] = _det(lambda pl, a, env: unify(a[1], Atom(_text(a[0], env).upper()), env))
_BUILTINS[("downcase_atom", 2)] = _det(lambda pl, a, env: unify(a[1], Atom(_text(a[0], env).lower()), env))
_BUILTINS[("atom_string", 2)] = _det(lambda pl, a, env: unify(a[1], Atom(_text(a[0], env)), env))
_BUILTINS[("term_to_atom", 2)] = _det(lambda pl, a, env: unify(a[1], Atom(fmt(a[0], env)), env))


@_b("atom_number", 2)
def _atom_number(pl, a, env, depth):
    x = deref(a[0], env)
    if isinstance(x, Atom):
        try:
            n = float(x.name) if "." in x.name else int(x.name)
        except ValueError:
            return
        e1 = unify(a[1], n, env)
    else:
        e1 = unify(a[0], Atom(repr(_arith(a[1], env))), env)
    if e1 is not None:
        yield e1


@_b("sub_atom", 5)
def _sub_atom(pl, a, env, depth):
    """sub_atom(+Atom, ?Before, ?Length, ?After, ?Sub) - enumerates substrings."""
    s = _text(a[0], env)
    sub = deref(a[4], env)
    if isinstance(sub, Atom):
        t = sub.name
        start = s.find(t)
        while start >= 0:
            e1 = unify(a[1], start, env)
            e1 = e1 and unify(a[2], len(t), e1)
            e1 = e1 and unify(a[3], len(s) - start - len(t), e1)
            if e1 is not None:
                yield e1
            start = s.find(t, start + 1)
        return
    for b in range(len(s) + 1):
        for ln in range(len(s) - b + 1):
            e1 = unify(a[1], b, env)
            e1 = e1 and unify(a[2], ln, e1)
            e1 = e1 and unify(a[3], len(s) - b - ln, e1)
            e1 = e1 and unify(a[4], Atom(s[b:b + ln]), e1)
            if e1 is not None:
                yield e1


@_b("=..", 2)
def _univ(pl, a, env, depth):
    t = deref(a[0], env)
    if isinstance(t, Struct):
        e1 = unify(a[1], mk_list([Atom(t.name)] + list(t.args)), env)
    elif not isinstance(t, Var):
        e1 = unify(a[1], mk_list([t]), env)
    else:
        items = _elems(a[1], env)
        if not items:
            return
        head = deref(items[0], env)
        e1 = unify(a[0], Struct(head.name, tuple(items[1:])) if len(items) > 1 else head, env)
    if e1 is not None:
        yield e1


def _assert(front):
    def fn(pl, a, env, depth):
        pl.add_clause(resolve(a[0], env), front=front)
        yield env
    return fn


_BUILTINS[("assertz", 1)] = _assert(False)
_BUILTINS[("assert", 1)] = _assert(False)
_BUILTINS[("asserta", 1)] = _assert(True)


@_b("retract", 1)
def _retract(pl, a, env, depth):
    t = deref(a[0], env)
    head = t.args[0] if isinstance(t, Struct) and t.name == ":-" and len(t.args) == 2 else t
    key = pl._key(deref(head, env))
    for c in list(pl.db.get(key, [])):
        e1 = unify(t if isinstance(t, Struct) and t.name == ":-" else head,
                   c if isinstance(t, Struct) and t.name == ":-" else
                   (c.args[0] if isinstance(c, Struct) and c.name == ":-" and len(c.args) == 2 else c), env)
        if e1 is not None:
            pl.db[key].remove(c)
            yield e1
            return


@_b("retractall", 1)
def _retractall(pl, a, env, depth):
    head = deref(a[0], env)
    key = pl._key(head)
    keep = []
    for c in pl.db.get(key, []):
        h = c.args[0] if isinstance(c, Struct) and c.name == ":-" and len(c.args) == 2 else c
        if unify(head, rename(h, {}), env) is None:
            keep.append(c)
    pl.db[key] = keep
    yield env


@_b("write", 1)
def _write(pl, a, env, depth):
    x = deref(a[0], env)
    pl.output.append(x.name if isinstance(x, Atom) else fmt(x, env))
    yield env


_BUILTINS[("print", 1)] = _write


@_b("nl", 0)
def _nl(pl, a, env, depth):
    pl.output.append("\n")
    yield env


@_b("writeln", 1)
def _writeln(pl, a, env, depth):
    x = deref(a[0], env)
    pl.output.append((x.name if isinstance(x, Atom) else fmt(x, env)) + "\n")
    yield env


@_b("format", 2)
def _format(pl, a, env, depth):
    f = _text(a[0], env)
    args = _elems(a[1], env) if isinstance(deref(a[1], env), Struct) else [deref(a[1], env)]
    it = iter(args)

    def rep(m):
        d = m.group(1)
        if d == "n":
            return "\n"
        if d == "~":
            return "~"
        x = deref(next(it, Atom("")), env)
        return x.name if isinstance(x, Atom) else fmt(x, env)
    pl.output.append(re.sub(r"~([awdpqn~])", rep, f))
    yield env


_BUILTINS[("format", 1)] = lambda pl, a, env, depth: _format(pl, (a[0], NIL), env, depth)


@_b("dynamic", 1)
def _dynamic(pl, a, env, depth):
    """:- dynamic foo/2, bar/1.  -> the predicates exist (and simply fail) even with no clauses."""
    spec = deref(a[0], env)
    todo = [spec]
    while todo:
        t = deref(todo.pop(), env)
        if isinstance(t, Struct) and t.name == "," and len(t.args) == 2:
            todo += list(t.args)
        elif isinstance(t, Struct) and t.name == "/" and len(t.args) == 2:
            pl.db.setdefault((deref(t.args[0], env).name, deref(t.args[1], env)), [])
    yield env


_BUILTINS[("discontiguous", 1)] = lambda pl, a, env, depth: iter([env])
_BUILTINS[("halt", 0)] = lambda pl, a, env, depth: iter(())

_BUILTIN_KEYS = set(_BUILTINS) | {("call", n) for n in range(1, 8)} | {
    (",", 2), (";", 2), ("->", 2), ("\\+", 1), ("!", 0), ("true", 0), ("fail", 0), ("false", 0),
    ("once", 1), ("ignore", 1), ("not", 1), ("forall", 2)}
