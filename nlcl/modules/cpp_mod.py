"""C++ domain: native C++17 programs (simulations, controllers, services, agents).

Output (relative to <build>/cpp/):
    include/nlcl_core.hpp       runtime core: Value, flat JSON, Entity, World loop
    include/<entity>.hpp        one class per entity (props as typed fields)
    src/<entity>.cpp            behaviours as methods (onStart/onUpdate/timers/...)
    src/main.cpp                builds the World, wires channels, CLI (--seconds N ...)
    src/nlcl_channels.hpp/.cpp  transports: http hub, udp, serial-style JSON lines, mqtt
    single/<project>.cpp        everything in one file (for online compilers)
    CMakeLists.txt, build.sh, build.bat, README.md

Only the C++ standard library and the OS socket API are used (Winsock2 on
Windows, BSD sockets elsewhere), so any free C++17 compiler builds it.
"""
from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path

from ..ir import Entity, describe_action, expr_names, expr_tokens, ident, validate_expr
from .base import BuildContext, DomainModule, RunResult

ASSETS = Path(__file__).parent / "_assets" / "cpp"

_CPP_KEYWORDS = set("""
alignas alignof and and_eq asm auto bitand bitor bool break case catch char char8_t char16_t char32_t class
compl concept const consteval constexpr constinit const_cast continue co_await co_return co_yield decltype
default delete do double dynamic_cast else enum explicit export extern false float for friend goto if inline
int long mutable namespace new noexcept not not_eq nullptr operator or or_eq private protected public register
reinterpret_cast requires return short signed sizeof static static_assert static_cast struct switch template
this thread_local throw true try typedef typeid typename union unsigned using virtual void volatile wchar_t
while xor xor_eq final override import module
""".split())
# members of nlcl::Entity, hook parameter names and common lowercase macros
_BASE_MEMBERS = {"name", "kind", "parent", "set_parent", "alive", "world", "props", "get", "set", "has",
                 "state", "state_json", "every", "destroy", "log", "send", "move", "timer", "nlcl", "std",
                 "dt", "prop", "sender", "msg", "other", "key", "args", "v", "main"}
_MACROS = {"near", "far", "interface", "small", "hyper", "pascal", "cdecl", "major", "minor", "unix", "linux",
           "errno", "assert", "stdin", "stdout", "stderr", "offsetof", "setjmp", "va_arg", "min", "max",
           "environ", "i386", "noreturn"}
_RESERVED = _CPP_KEYWORDS | _BASE_MEMBERS | _MACROS
_RESERVED_FILES = {"main", "nlcl_channels", "nlcl_core"}

_CTYPE = {"int": "int", "double": "double", "bool": "bool", "string": "std::string",
          "vec": "std::vector<double>"}


# ---------------------------------------------------------------- literals
def _fnum(x) -> str:
    try:
        f = float(x)
    except (TypeError, ValueError):
        return "0.0"
    if not math.isfinite(f):
        return "0.0"
    r = repr(f)
    return r if any(c in r for c in ".e") else r + ".0"


def _cstr(s) -> str:
    """C++ string literal (UTF-8 kept as-is, controls as 3-digit octal escapes)."""
    out = ['"']
    for ch in str(s):
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append("\\%03o" % ord(ch))
        elif ch == "?" and out[-1] == "?":
            out.append("\\?")  # never form a trigraph (pre-C++17 / -trigraphs compilers)
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _cmt(s) -> str:
    """Text safe inside a // comment."""
    return re.sub(r"[\r\n\\]+", " ", str(s)).strip()


def _typ(v) -> str:
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int" if -2 ** 31 < v < 2 ** 31 else "double"
    if isinstance(v, float) or v is None:
        return "double"
    if isinstance(v, (list, tuple)):
        return "vec" if all(isinstance(x, (int, float)) for x in v) else "string"
    return "string"


def _lit(v, typ: str) -> str:
    if typ == "bool":
        return "true" if (v if isinstance(v, bool) else bool(v)) else "false"
    if typ == "int":
        try:
            return str(int(v))
        except (TypeError, ValueError):
            return "0"
    if typ == "double":
        return _fnum(v if isinstance(v, (int, float)) and not isinstance(v, bool) else
                     (float(v) if isinstance(v, bool) else 0.0))
    if typ == "vec":
        if not isinstance(v, (list, tuple)):
            return "{}"
        return "{" + ", ".join(_fnum(x) for x in v) + "}"
    if isinstance(v, (list, tuple)):
        v = ", ".join(str(x) for x in v)
    elif isinstance(v, dict):
        v = json.dumps(v, ensure_ascii=False)
    elif v is None:
        v = ""
    return _cstr(v)


def _value_lit(v) -> str:
    """nlcl::Value(...) expression for a python value."""
    t = _typ(v)
    if t == "vec":
        return f"nlcl::Value(std::vector<double>{_lit(v, t)})"
    return f"nlcl::Value({_lit(v, t)})"


# ---------------------------------------------------------------- per-entity model
class _Model:
    """Resolved C++ view of one entity: typed fields, triggers, method names."""

    def __init__(self, e: Entity, stem: str):
        self.e = e
        self.stem = stem
        self.cls = e.class_name
        self.props: dict[str, str] = {}       # prop -> type key
        self.init: dict[str, object] = {}     # prop -> initial python value
        self.fields: dict[str, str] = {}      # prop -> C++ member name
        self._used: set[str] = set()
        self.calls: dict[str, str] = {}       # call name -> C++ method name
        self.edges: list[str] = []            # private bool members for edge-triggered rules
        for k, v in (e.props or {}).items():
            k = str(k)
            if k.startswith("_") or not ident(k):
                continue
            self.add(k, _typ(v), v)

    def add(self, prop: str, typ: str, value=None) -> None:
        prop = str(prop)
        if prop in self.props:
            return
        base = re.sub(r"_+", "_", ident(prop)) or "prop"
        f = base + "_" if base in _RESERVED else base
        n = 2
        while f in self._used:
            f = f"{base}_{n}"
            n += 1
        self._used.add(f)
        self.props[prop] = typ
        self.fields[prop] = f
        if value is None:
            value = {"int": 0, "double": 0.0, "bool": False, "string": "", "vec": [0.0, 0.0, 0.0]}[typ]
        self.init[prop] = value

    def call_name(self, name: str) -> str:
        name = str(name)
        if name not in self.calls:
            base = re.sub(r"_+", "_", ident(name)) or "action"
            m = base if base not in _RESERVED and base not in self._used else "do_" + base
            while m in self._used:
                m += "_"
            self._used.add(m)
            self.calls[name] = m
        return self.calls[name]


# ---------------------------------------------------------------- expressions
class _ExprRenderer:
    """Tiny recursive-descent translator from the NLCL expression language to C++.

    Same precedence as C/C++.  Every sub-expression carries a static kind
    (num, bool, str, lit = string literal, vec) so that mixed operands are
    converted explicitly: `/` is always true division, `%` is std::fmod,
    `+` with a string concatenates (nlcl::cat), comparisons of different
    kinds go through nlcl::Value and strings used as conditions through
    nlcl::truthy.  The generated code therefore always compiles.
    """

    def __init__(self, tokens, name_fn, kind_fn=None):
        self.t = tokens
        self.i = 0
        self.name = name_fn
        self.kind = kind_fn or (lambda n: "num")

    def peek(self):
        return self.t[self.i] if self.i < len(self.t) else (None, None)

    def take(self):
        tok = self.peek()
        self.i += 1
        return tok

    def parse(self) -> tuple[str, str]:
        out = self.binary(0)
        if self.i != len(self.t):
            raise ValueError("trailing tokens")
        return out

    LEVELS = [("||",), ("&&",), ("==", "!="), ("<", ">", "<=", ">="), ("+", "-"), ("*", "/", "%")]

    @staticmethod
    def num(x):
        c, k = x
        return c if k in ("num", "bool") else f"nlcl::num({c})"

    @staticmethod
    def cond(x):
        c, k = x
        return c if k in ("num", "bool") else f"nlcl::truthy({c})"

    @staticmethod
    def sobj(x):
        c, k = x
        return f"std::string({c})" if k == "lit" else c

    def combine(self, left, op, right):
        lk, rk = left[1], right[1]
        strs = ("str", "lit")
        if op in ("||", "&&"):
            return f"{self.cond(left)} {op} {self.cond(right)}", "bool"
        if op in ("==", "!="):
            if lk in strs and rk in strs:
                return f"{self.sobj(left)} {op} {right[0]}", "bool"
            if (lk in ("num", "bool") and rk in ("num", "bool")) or (lk == rk == "vec"):
                return f"{left[0]} {op} {right[0]}", "bool"
            return f"nlcl::Value({left[0]}) {op} nlcl::Value({right[0]})", "bool"
        if op in ("<", ">", "<=", ">="):
            if lk in strs and rk in strs:
                return f"{self.sobj(left)} {op} {right[0]}", "bool"
            return f"{self.num(left)} {op} {self.num(right)}", "bool"
        if op == "+" and (lk in strs or rk in strs):
            return f"nlcl::cat({left[0]}, {right[0]})", "str"
        if op == "%":
            return f"std::fmod(static_cast<double>({self.num(left)}), static_cast<double>({self.num(right)}))", "num"
        if op == "/":  # NLCL division is true division, also for int operands
            return f"{self.num(left)} / static_cast<double>({self.num(right)})", "num"
        return f"{self.num(left)} {op} {self.num(right)}", "num"

    def binary(self, lvl: int):
        if lvl == len(self.LEVELS):
            return self.unary()
        left = self.binary(lvl + 1)
        while True:
            k, v = self.peek()
            if k != "op" or v not in self.LEVELS[lvl]:
                return left
            self.take()
            right = self.binary(lvl + 1)
            left = self.combine(left, v, right)

    def unary(self):
        k, v = self.peek()
        if k == "op" and v in ("!", "-", "+"):
            self.take()
            inner = self.unary()
            code = self.cond(inner) if v == "!" else self.num(inner)
            code = f"{v}({code})" if code[:1] in "-+!" else f"{v}{code}"
            return code, ("bool" if v == "!" else "num")
        return self.primary()

    def primary(self):
        k, v = self.take()
        if k == "num":
            if "." in v:
                return v, "num"
            n = int(v)
            return (str(n) if n < 2 ** 31 else f"{n}.0"), "num"
        if k == "str":
            return _cstr(v[1:-1]), "lit"
        if k == "id":
            if v in ("true", "false"):
                return v, "bool"
            return self.name(v), self.kind(v)
        if k == "op" and v == "(":
            inner = self.binary(0)
            k2, v2 = self.take()
            if v2 != ")":
                raise ValueError("missing )")
            return f"({inner[0]})", inner[1]
        raise ValueError(f"unexpected token {v!r}")


# rotation props (degrees): angle = about z, angle_x / angle_y = about x / y
_ANGLES = ("angle", "angle_x", "angle_y", "angle_z")

_KIND = {"int":"num", "double": "num", "bool": "bool", "string": "str", "vec": "vec"}


def _trigger(b: dict) -> tuple[str, str]:
    """('timer', '500') from {'on': 'timer:500'}; tolerant of junk."""
    on = str(b.get("on") or "update").strip()
    key, _, arg = on.partition(":")
    return key.strip().lower() or "update", arg.strip()


def _names(expr) -> list[str]:
    try:
        return expr_names(validate_expr(expr))
    except ValueError:
        return []


# ---------------------------------------------------------------- module
class CppModule(DomainModule):
    name = "cpp"
    title = "C++ (native C++17 programs: simulations, controllers, services, agents)"
    aliases = ("c++", "native", "cplusplus", "cxx")
    keywords = ("cmake", "g++", "clang", "msvc", "mingw", "compiled", "compiler", "executable", "stl",
                "simulation", "simulator", "solver", "integrator", "kalman", "robot", "robotics", "vehicle",
                "autopilot", "engine", "multithreaded", "algorithm", "numerical", "fft", "orderbook",
                "matcher", "scheduler", "planner")
    kinds = {
        "object": {},
        "class": {},
        "service": {"running": True, "requests": 0},
        "controller": {"setpoint": 0.0, "output": 0.0, "gain": 1.0},
        "simulation": {"elapsed": 0.0, "steps": 0},
        "engine": {"running": False, "rpm": 0.0},
        "processor": {"processed": 0},
        "filter": {"input": 0.0, "output": 0.0, "alpha": 0.5},
        "calculator": {"result": 0.0},
        "counter": {"count": 0},
        "timer": {"elapsed": 0.0},
        "robot": {"position": [0.0, 0.0, 0.0], "speed": 1.0, "battery": 100.0},
        "vehicle": {"position": [0.0, 0.0, 0.0], "speed": 0.0, "fuel": 100.0},
        "agent": {"position": [0.0, 0.0, 0.0], "energy": 100.0},
    }
    suggested_props = ("speed", "count", "threshold", "gain", "rate", "label")
    protocols = ("http", "udp", "serial", "mqtt", "websocket")
    toolchain = """Free C++17 toolchains (no third-party libraries needed):

    cmake -S . -B build && cmake --build build          # any compiler, CMake 3.10+
    ./build/<project>_app --simulate --seconds 5          # Windows: build\\Debug\\<project>_app.exe

    g++ -std=c++17 -O2 -Iinclude -Isrc src/*.cpp -o app -pthread          # Linux/macOS (GCC or clang++)
    g++ -std=c++17 -O2 -Iinclude -Isrc src/*.cpp -o app.exe -lws2_32      # Windows, MinGW-w64 (MSYS2/WinLibs)
    cl /std:c++17 /EHsc /utf-8 /Iinclude /Isrc src\\*.cpp /Fe:app.exe ws2_32.lib   # MSVC Build Tools

Flags: --simulate (fake incoming data/key presses), --seconds N, --dt S, --fast, --quiet.
`single/<project>.cpp` is the whole program in one file for online compilers
(Compiler Explorer godbolt.org, Wandbox, OnlineGDB) - run it with --simulate --seconds 3."""

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _stem(e: Entity, used: set[str]) -> str:
        s = re.sub(r"_+", "_", e.ident) or "entity"
        if s in _RESERVED_FILES:
            s += "_entity"
        base, n = s, 2
        while s in used:
            s = f"{base}_{n}"
            n += 1
        used.add(s)
        return s

    @staticmethod
    def _find(ctx: BuildContext, ref: str | None) -> Entity | None:
        if not ref:
            return None
        r = ident(ref)
        for e in ctx.system.entities.values():
            if e.ident == r or e.name.lower() == str(ref).lower():
                return e
        return None

    def _watch(self, ctx: BuildContext, e: Entity, arg: str) -> tuple[str | None, str]:
        """'change:<arg>' -> (other entity ident or None for own, prop)."""
        arg = str(arg).strip()
        if "." not in arg:
            return None, arg
        head, _, prop = arg.partition(".")
        head, prop = head.strip(), prop.strip()
        if head in ("self", "this", "it") or ident(head) == e.ident:
            return None, prop
        return self._ref_name(ctx, head), prop

    def _with_cond(self, ctx: BuildContext, b: dict) -> str | None:
        """C++ test for a collision rule's "with" filter (entity ident or kind); None = any."""
        w = b.get("with")
        if w is None or not str(w).strip() or str(w).strip().lower() in ("any", "*", "anything"):
            return None
        w = str(w).strip()
        names = {self._ref_name(ctx, w), ident(w) or w}
        kinds = {w.lower(), (ident(w) or w).lower()}
        parts = [f"other.name() == {_cstr(n)}" for n in sorted(names)]
        parts += [f"other.kind() == {_cstr(k)}" for k in sorted(kinds)]
        return " || ".join(parts)

    def _ref_name(self, ctx: BuildContext, ref: str) -> str:
        e = self._find(ctx, ref)
        return e.ident if e else (ident(ref) or str(ref))

    def _models(self, ctx: BuildContext) -> list[_Model]:
        used: set[str] = set()
        classes: set[str] = set()
        models = []
        for e in ctx.entities:
            m = _Model(e, self._stem(e, used))
            base = m.cls if re.match(r"[A-Za-z]\w*$", m.cls or "") else "Obj"
            cls, n = base, 2
            while cls in classes:  # "my robot" and "my_robot" -> MyRobot, MyRobot2
                cls = f"{base}{n}"
                n += 1
            classes.add(cls)
            m.cls = cls
            self._augment(ctx, m)
            models.append(m)
        return models

    def _augment(self, ctx: BuildContext, m: _Model) -> None:
        """Add fields that behaviours and channels need but the IR does not declare."""
        e = m.e

        def need_names(expr):
            for n in _names(expr):
                if "." not in n:
                    m.add(n, "double")
                else:
                    head = n.split(".", 1)[0]
                    if head in ("self", "this", e.ident) and n.count(".") >= 1:
                        prop = n.split(".")[1]
                        m.add(prop, "double")

        for b in e.behaviors or []:
            if not isinstance(b, dict):
                continue
            key, arg = _trigger(b)
            if key == "change" and arg:
                ent, prop = self._watch(ctx, e, arg)
                if ent is None and prop and ident(prop):
                    m.add(prop, "double")
            if b.get("if"):
                need_names(b["if"])
            for a in b.get("do") or []:
                if not isinstance(a, dict):
                    continue
                op = a.get("op")
                if op in ("set", "add", "toggle") and not (a.get("prop") and ident(str(a.get("prop")))):
                    continue
                if op in ("set", "add", "toggle") and self._redirect(ctx, e, b, a.get("prop")):
                    need_names(a.get("value", 0)) if op != "toggle" else None
                    continue
                if op == "move" and self._xy_move(e):
                    for ax in ("x", "y", "z"):
                        need_names(a.get(ax, 0))
                    continue
                if op == "set":
                    v = a.get("value")
                    toks = []
                    try:
                        toks = expr_tokens(validate_expr(v))
                    except ValueError:
                        pass
                    if len(toks) == 1 and toks[0][0] == "str":
                        m.add(a["prop"], "string", toks[0][1][1:-1])
                    elif len(toks) == 1 and toks[0][1] in ("true", "false"):
                        m.add(a["prop"], "bool", False)
                    else:
                        m.add(a["prop"], "double")
                    need_names(v)
                elif op == "add":
                    m.add(a["prop"], "double")
                    need_names(a.get("value", 1))
                elif op == "toggle":
                    m.add(a["prop"], "bool", False)
                elif op == "move":
                    m.add("position", "vec", [0.0, 0.0, 0.0])
                    for ax in ("x", "y", "z"):
                        need_names(a.get(ax, 0))
                elif op == "call":
                    for x in a.get("args") or []:
                        need_names(x)
                    m.call_name(a.get("name") or "action")
        # The interpreter registers props it first sees in a behaviour as int 0.
        # If such a prop is only ever assigned booleans (or strings), type it so.
        assigned: dict[str, set[str]] = {}
        for b in e.behaviors or []:
            for a in (b.get("do") or []) if isinstance(b, dict) else []:
                if not isinstance(a, dict):
                    continue
                op, prop = a.get("op"), a.get("prop")
                if not prop:
                    continue
                prop = str(prop)
                kind = "num"
                if op == "toggle":
                    kind = "bool"
                elif op in ("set", "add"):
                    try:
                        toks = expr_tokens(validate_expr(a.get("value", 1)))
                    except ValueError:
                        toks = []
                    if op == "set" and len(toks) == 1 and toks[0][0] == "str":
                        kind = "string"
                    elif op == "set" and len(toks) == 1 and toks[0][1] in ("true", "false"):
                        kind = "bool"
                    elif not all(k == "num" and "." not in v or k == "op" and v in "+-*()" for k, v in toks):
                        kind = "real"  # fractions, names, division...: keep it a double
                assigned.setdefault(prop, set()).add(kind)
        for prop, kinds in assigned.items():
            orig = (e.props or {}).get(prop)
            if prop in m.props and type(orig) is int and orig == 0:
                if kinds == {"bool"}:
                    m.props[prop], m.init[prop] = "bool", False
                elif kinds == {"string"}:
                    m.props[prop], m.init[prop] = "string", ""
                elif ("real" in kinds or prop in _ANGLES) and m.props[prop] == "int":
                    m.props[prop], m.init[prop] = "double", 0.0
        for prop in _ANGLES:  # rotation in degrees: always a double, kept in [0, 360)
            if m.props.get(prop) == "int":
                m.props[prop], m.init[prop] = "double", float(m.init.get(prop) or 0)
        for c in ctx.channels_for(e):
            for f in c.fields or []:
                f = str(f)
                if not ident(f):
                    continue
                if c.dst.id == e.id:
                    src_v = (c.src.props or {}).get(f)
                    t = _typ(src_v) if src_v is not None else "double"
                    if t == "int":  # received data (sensor readings...) may carry fractions
                        t = "double"
                    m.add(f, t, src_v)
                else:
                    m.add(f, "double")

    def _redirect(self, ctx: BuildContext, e: Entity, b: dict, prop) -> str | None:
        """'when balance of account < 0, set balance to 0' attached to another entity: the
        property belongs to the (single, local) entity the condition is about, not to e."""
        prop = str(prop or "")
        if not prop or not b.get("if"):
            return None
        names = [n for n in _names(b["if"]) if "." in n]
        own = (e.props or {}).get(prop, None) if prop in (e.props or {}) else None
        if prop in (e.props or {}):
            # the interpreter registers a behaviour's prop on the focused entity as int 0;
            # only treat it as misplaced when the condition reads <other>.<same prop>
            if not (type(own) is int and own == 0):
                return None
            heads = {n.split(".", 1)[0] for n in names if n.split(".", 1)[1] == prop}
        else:
            heads = {n.split(".", 1)[0] for n in names}
        heads -= {"self", "this", "other", e.ident}
        hits = []
        for h in heads:
            o = self._find(ctx, h)
            if o is not None and o.id != e.id and prop in (o.props or {})                     and any(x.id == o.id for x in ctx.entities):
                hits.append(o.ident)
        return hits[0] if len(hits) == 1 else None

    @staticmethod
    def _xy_move(e: Entity) -> bool:
        """Entity keeps its location in separate numeric x / y (/ z) properties."""
        p = e.props or {}
        num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
        return "position" not in p and num(p.get("x")) and num(p.get("y"))

    def _namer(self, ctx: BuildContext, m: _Model, collision: bool = False):
        local_idents = {x.ident for x in ctx.entities}

        def comp(prop_parts: list[str]):
            return {"x": 0, "y": 1, "z": 2, "r": 0, "g": 1, "b": 2}.get(prop_parts[0]) if prop_parts else None

        kinds: dict[str, str] = {}

        def g(n: str) -> tuple[str, str]:
            parts = [p for p in n.split(".") if p]
            if not parts:
                return "0", "num"
            if len(parts) == 1:
                n = parts[0]
                if n not in m.fields:
                    m.add(n, "double")  # defensive: should already exist
                return m.fields[n], _KIND[m.props[n]]
            head, rest = parts[0], parts[1:]
            # own property component: position.x
            if head in m.fields and m.props[head] == "vec" and comp(rest) is not None:
                return f"nlcl::at({m.fields[head]}, {comp(rest)})", "num"
            if head in ("self", "this") or (head == m.e.ident and head not in m.fields):
                return g(".".join(rest))
            prop = rest[0]
            idx = comp(rest[1:])
            if collision and head == "other":
                base = f"other.get({_cstr(prop)})"
                return (f"nlcl::at({base}.asList(), {idx})", "num") if idx is not None else (f"{base}.asNum()", "num")
            ent = self._find(ctx, head)
            ename = ent.ident if ent else (ident(head) or head)
            base = f"world().get({_cstr(ename)}, {_cstr(prop)})"
            if idx is not None:
                return f"nlcl::at({base}.asList(), {idx})", "num"
            typ = _typ((ent.props or {}).get(prop)) if ent is not None and prop in (ent.props or {}) else "double"
            if ent is not None and ent.ident in local_idents and prop not in (ent.props or {}):
                typ = "double"
            lm = (getattr(self, "_local_models", None) or {}).get(ename)
            if lm is not None and prop in lm.props:  # same program: use the generated C++ type
                typ = lm.props[prop]
            acc = {"string": "asStr()", "bool": "asBool()", "vec": "asList()"}.get(typ, "asNum()")
            return f"{base}.{acc}", _KIND.get(typ, "num")

        def f(n: str) -> str:
            code, k = g(n)
            kinds[n] = k
            return code

        f.kind = lambda n: kinds.get(n) or g(n)[1]
        return f

    def _texpr(self, expr, namer) -> tuple[str, str]:
        """(C++ code, kind) for an NLCL expression; never raises."""
        try:
            toks = expr_tokens(validate_expr(expr))
            return _ExprRenderer(toks, namer, getattr(namer, "kind", None)).parse()
        except Exception:
            return "0", "num"

    def _cexpr(self, expr, namer) -> str:
        return self._texpr(expr, namer)[0]

    def _cond(self, expr, namer) -> str:
        return _ExprRenderer.cond(self._texpr(expr, namer))

    def _cnum(self, expr, namer) -> str:
        return _ExprRenderer.num(self._texpr(expr, namer))

    # ------------------------------------------------------------------ actions
    def _action(self, ctx: BuildContext, m: _Model, a: dict, namer) -> list[str]:
        op = a.get("op")
        if op in ("set", "add", "toggle"):
            prop = a.get("prop")
            if not prop or not ident(str(prop)):
                return [f"// {op}: no property given"]
            prop = str(prop)
            if prop not in m.fields:
                m.add(prop, "bool" if op == "toggle" else "double")
            fld, t = m.fields[prop], m.props[prop]
            ctype = _CTYPE[t]
            numeric = ("num", "bool")
            if op == "set":
                v = a.get("value", 0)
                ex, k = self._texpr(v, namer)
                if t in ("int", "double", "bool") and k in numeric:
                    return [f"{fld} = static_cast<{ctype}>({ex});"]
                if t == "string" and k in ("str", "lit"):
                    return [f"{fld} = {ex};"]
                return [f"nlcl::assign({fld}, nlcl::Value({ex}));"]
            if op == "add":
                v = a.get("value", 1)
                x = self._texpr(v, namer)
                if prop in _ANGLES and t == "double":  # rotate: wrap degrees into [0, 360)
                    return [f"{fld} = std::fmod({fld} + ({_ExprRenderer.num(x)}), 360.0);",
                            f"if ({fld} < 0) {fld} += 360.0;"]
                if t in ("int", "double", "bool"):
                    return [f"{fld} = static_cast<{ctype}>({fld} + ({_ExprRenderer.num(x)}));"]
                if t == "string":
                    return [f"{fld} += nlcl::Value({x[0]}).asStr();"]
                return [f"nlcl::addTo({fld}, static_cast<double>({_ExprRenderer.num(x)}));"]
            # toggle
            if t == "bool":
                return [f"{fld} = !{fld};"]
            if t in ("int", "double"):
                return [f"{fld} = static_cast<{ctype}>({fld} == 0 ? 1 : 0);"]
            if t == "string":
                return [f'{fld} = {fld}.empty() ? "on" : "";']
            return [f"// toggle {prop}: not applicable to a vector"]
        if op == "destroy":
            return ["destroy();", "return;"]
        if op == "log":
            text = str(a.get("text", ""))
            t = text.strip()
            if t in m.props or ("." in t and self._find(ctx, t.split(".", 1)[0]) is not None
                                and re.match(r"^[A-Za-z_]\w*\.[A-Za-z_]\w*$", t)):
                text = f"{t} = {{{t}}}"  # "log temperature" -> print its value
            return [f"log({_cstr(text)});"]
        if op == "move":
            xs = [self._cnum(a.get(ax, 0), namer) for ax in ("x", "y", "z")]
            if self._xy_move(m.e):
                out = []
                for ax, v in zip(("x", "y", "z"), xs):
                    if ax in m.props and m.props[ax] in ("int", "double") and v not in ("0", "0.0"):
                        f = m.fields[ax]
                        out.append(f"{f} = static_cast<{_CTYPE[m.props[ax]]}>({f} + ({v}));")
                return out or ["// move: no displacement"]
            return [f"move({xs[0]}, {xs[1]}, {xs[2]});"]
        if op == "send":
            to = a.get("to")
            to_s = _cstr(self._ref_name(ctx, to)) if to else '""'
            fields = [str(x) for x in (a.get("fields") or [])]
            if fields:
                return [f"send({to_s}, {{{', '.join(_cstr(x) for x in fields)}}});"]
            return [f"send({to_s});"]
        if op == "call":
            meth = m.call_name(a.get("name") or "action")
            args = ", ".join(f"nlcl::Value({self._cexpr(x, namer)})" for x in a.get("args") or [])
            return [f"{meth}({{{args}}});"]
        try:
            return [f"// {_cmt(describe_action(a))}"]
        except Exception:
            return ["// (unsupported action)"]

    def _remote_action(self, ent: str, a: dict, namer) -> list[str]:
        op, prop = a.get("op"), str(a.get("prop"))
        head = f"if (nlcl::Entity* t = world().find({_cstr(ent)})) "
        if op == "set":
            return [head + f"t->set({_cstr(prop)}, nlcl::Value({self._cexpr(a.get('value', 0), namer)}));"]
        if op == "add":
            return [head + f"t->set({_cstr(prop)}, nlcl::Value(t->get({_cstr(prop)}).asNum() + "
                           f"({self._cnum(a.get('value', 1), namer)})));"]
        return [head + f"t->set({_cstr(prop)}, nlcl::Value(!t->get({_cstr(prop)}).asBool()));"]

    def _body(self, ctx, m, b, namer, indent="    ", edge: bool = False) -> list[str]:
        out = [f"{indent}// {_cmt(self._describe(b))}"]
        ind = indent
        acts = [a for a in b.get("do") or [] if isinstance(a, dict)]
        if b.get("if"):
            cond = self._cond(b['if'], namer)
            if edge and any(a.get("op") in ("log", "send", "call", "toggle", "add") for a in acts):
                # fire once each time the condition becomes true (no flood at the loop rate)
                flag = f"nlclWas{len(m.edges)}_"
                m.edges.append(flag)
                out += [f"{indent}const bool {flag}now = {cond};",
                        f"{indent}if ({flag}now && !{flag}) {{"]
                tail = [f"{indent}}}", f"{indent}{flag} = {flag}now;"]
            else:
                out.append(f"{indent}if ({cond}) {{")
                tail = [indent + "}"]
            ind = indent + "    "
        for a in acts:
            if a.get("op") in ("set", "add", "toggle"):
                ent = self._redirect(ctx, m.e, b, a.get("prop"))
                if ent:
                    out += [ind + ln for ln in self._remote_action(ent, a, namer)]
                    continue
            out += [ind + ln for ln in self._action(ctx, m, a, namer)]
        if b.get("if"):
            out += tail
        return out

    @staticmethod
    def _describe(b: dict) -> str:
        try:
            acts = "; ".join(describe_action(a) for a in b.get("do") or [])
        except Exception:
            acts = "..."
        return f"on {b.get('on')}" + (f" if {b['if']}" if b.get("if") else "") + f": {acts}"

    # ------------------------------------------------------------------ classes
    def _class_files(self, ctx: BuildContext, m: _Model) -> tuple[str, str]:
        e = m.e
        groups: dict[str, list[dict]] = {}
        timers: list[dict] = []
        for b in e.behaviors or []:
            if not isinstance(b, dict) or not isinstance(b.get("do"), list) or not b.get("do"):
                continue
            key, arg = _trigger(b)
            if key not in ("start", "update", "timer", "change", "message", "collision", "input") \
                    or (key in ("change", "input") and not arg):
                continue  # unknown/incomplete trigger: nothing to hook it to
            if key == "timer":
                timers.append(b)
            groups.setdefault(key, []).append(b)

        namer = self._namer(ctx, m)
        cnamer = self._namer(ctx, m, collision=True)
        # ---- method bodies (generated first: they may register extra fields/calls)
        bodies: list[tuple[str, str, list[str]]] = []   # (decl, definition head, lines)
        cls = m.cls

        if "start" in groups:
            L = []
            for b in groups["start"]:
                L += self._body(ctx, m, b, namer)
            bodies.append(("void onStart() override;", f"void {cls}::onStart() {{", L))

        intrinsic = []
        num = ("int", "double")
        if e.kind in ("timer", "simulation") and m.props.get("elapsed") in num:
            intrinsic.append(f"    {m.fields['elapsed']} += static_cast<{_CTYPE[m.props['elapsed']]}>(dt);  "
                             f"// {e.kind}: world time")
        if e.kind == "simulation" and m.props.get("steps") in num:
            intrinsic.append(f"    {m.fields['steps']} += 1;  // simulation: step counter")
        if e.kind == "filter" and all(m.props.get(p) in num for p in ("input", "output", "alpha")):
            fi, fo, fa = m.fields["input"], m.fields["output"], m.fields["alpha"]
            intrinsic.append(f"    {fo} = static_cast<{_CTYPE[m.props['output']]}>({fo} + {fa} * ({fi} - {fo}));"
                             f"  // filter: exponential smoothing")
        if "update" in groups or intrinsic:
            L = ["    (void)dt;"] + intrinsic
            for b in groups.get("update", []):
                L += self._body(ctx, m, b, namer, edge=True)
            bodies.append(("void onUpdate(double dt) override;", f"void {cls}::onUpdate(double dt) {{", L))

        timer_decls = []
        for i, b in enumerate(timers):
            try:
                ms = max(1, int(round(float(_trigger(b)[1]))))
            except (TypeError, ValueError, OverflowError):
                ms = 1000
            ms = min(ms, 2 ** 31 - 1)
            L = self._body(ctx, m, b, namer)
            bodies.append((f"void onTimer{i}();  // every {ms} ms", f"void {cls}::onTimer{i}() {{", L))
            timer_decls.append((i, ms))

        if "change" in groups:
            L, W = [], ["    (void)who;", "    (void)prop;"]
            for b in groups["change"]:
                ent, prop = self._watch(ctx, e, _trigger(b)[1])
                if ent is None:
                    L.append(f"    if (prop == {_cstr(prop)}) {{")
                    L += self._body(ctx, m, b, namer, "        ")
                    L.append("    }")
                else:  # another entity's property: change:<entity>.<prop>
                    W.append(f"    if (who == {_cstr(ent)} && prop == {_cstr(prop)}) {{")
                    W += self._body(ctx, m, b, namer, "        ")
                    W.append("    }")
            if L:
                bodies.append(("void onChange(const std::string& prop) override;",
                               f"void {cls}::onChange(const std::string& prop) {{", L))
            if len(W) > 2:
                bodies.append(("void onWatch(const std::string& who, const std::string& prop) override;",
                               f"void {cls}::onWatch(const std::string& who, const std::string& prop) {{", W))

        if "message" in groups:
            L = ["    (void)sender;", "    (void)msg;"]
            for b in groups["message"]:
                src = _trigger(b)[1]
                if src in ("any", "", "*"):
                    L += self._body(ctx, m, b, namer)
                else:
                    L.append(f"    if (sender == {_cstr(self._ref_name(ctx, src) if self._find(ctx, src) else src)}) {{")
                    L += self._body(ctx, m, b, namer, "        ")
                    L.append("    }")
            bodies.append(("void onMessage(const std::string& sender, const nlcl::Fields& msg) override;",
                           f"void {cls}::onMessage(const std::string& sender, const nlcl::Fields& msg) {{", L))

        if "collision" in groups:
            L = ["    (void)other;"]
            for b in groups["collision"]:
                wc = self._with_cond(ctx, b)
                if wc is None:
                    L += self._body(ctx, m, b, cnamer)
                else:  # "with": only collisions with that entity / kind
                    L.append(f"    if ({wc}) {{")
                    L += self._body(ctx, m, b, cnamer, "        ")
                    L.append("    }")
            bodies.append(("void onCollision(nlcl::Entity& other) override;",
                           f"void {cls}::onCollision(nlcl::Entity& other) {{", L))

        if "input" in groups:
            L = []
            for b in groups["input"]:
                k = _trigger(b)[1]
                k = self._ref_name(ctx, k) if self._find(ctx, k) else k
                L.append(f"    if (key == {_cstr(k)}) {{")
                L += self._body(ctx, m, b, namer, "        ")
                L.append("    }")
            bodies.append(("void onInput(const std::string& key) override;",
                           f"void {cls}::onInput(const std::string& key) {{", L))

        for cname, meth in list(m.calls.items()):
            impl = None
            if ctx.llm is not None:
                try:
                    impl = ctx.llm.write_body(
                        "C++17 member function body (fields are members; args is const std::vector<nlcl::Value>&)",
                        cname.replace("_", " "),
                        f"class {cls} with fields {', '.join(f'{_CTYPE[t]} {m.fields[p]}' for p, t in m.props.items())}")
                except Exception:
                    impl = None
            if impl and isinstance(impl, str):
                L = ["    " + ln for ln in impl.splitlines()]
            else:
                L = ["    (void)args;  // TODO: implement",
                     f"    log({_cstr(cname + ' called')});"]
            bodies.append((f"void {meth}(const std::vector<nlcl::Value>& args = {{}});",
                           f"void {cls}::{meth}(const std::vector<nlcl::Value>& args) {{", L))

        # ---- header
        title = f'{e.kind} "{e.name}"'
        H = [f"// {m.stem}.hpp - C++ class for {_cmt(title)} (generated by NLCL; edit freely)",
             "#pragma once", "", '#include "nlcl_core.hpp"', "",
             f"class {cls} : public nlcl::Entity {{", "public:",
             f"    explicit {cls}(nlcl::World& world, const std::string& name = {_cstr(e.ident)});", ""]
        H.append("    // properties")
        if not m.props:
            H.append("    // (none yet)")
        for p, t in m.props.items():
            note = "" if m.fields[p] == p else f"  // property \"{_cmt(p)}\""
            H.append(f"    {_CTYPE[t]} {m.fields[p]} = {_lit(m.init[p], t)};{note}")
        if bodies:
            H += ["", "    // behaviours"]
            for decl, _, _ in bodies:
                H.append("    " + decl)
        if m.edges:
            H += ["", "private:", "    // edge detection for 'when <condition>' rules", 
                  *[f"    bool {f} = false;" for f in m.edges], "", "public:"]
        H += ["", "    // reflection used by the runtime (channels, change detection, log)",
              "    std::vector<std::string> props() const override;",
              "    nlcl::Value get(const std::string& prop) const override;",
              "    bool set(const std::string& prop, const nlcl::Value& v) override;",
              "};", ""]

        # ---- source
        S = [f"// {m.stem}.cpp - behaviours of {_cmt(title)} (generated by NLCL; edit freely)",
             f'#include "{m.stem}.hpp"', "",
             f"{cls}::{cls}(nlcl::World& world, const std::string& name)",
             f"    : nlcl::Entity(world, name, {_cstr(e.kind)}) {{"]
        for i, ms in timer_decls:
            S.append(f"    every({ms}, [this] {{ onTimer{i}(); }});")
        S += ["}", ""]
        for _, head, L in bodies:
            S.append(head)
            S += L
            S += ["}", ""]
        S.append(f"std::vector<std::string> {cls}::props() const {{")
        S.append("    return {" + ", ".join(_cstr(p) for p in m.props) + "};")
        S += ["}", "", f"nlcl::Value {cls}::get(const std::string& prop) const {{"]
        if not m.props:
            S.append("    (void)prop;")
        for p in m.props:
            S.append(f"    if (prop == {_cstr(p)}) return nlcl::Value({m.fields[p]});")
        S += ["    return nlcl::Value();", "}", "",
              f"bool {cls}::set(const std::string& prop, const nlcl::Value& v) {{"]
        if not m.props:
            S += ["    (void)prop;", "    (void)v;"]
        for p in m.props:
            S.append(f"    if (prop == {_cstr(p)}) {{ nlcl::assign({m.fields[p]}, v); return true; }}")
        S += ["    return false;", "}", ""]
        return "\n".join(H), "\n".join(S)

    # ------------------------------------------------------------------ main
    def _main(self, ctx: BuildContext, models: list[_Model]) -> str:
        proj = ctx.system.name or ctx.project
        local = {e.id for e in ctx.entities}
        L = [f"// main.cpp - {_cmt(proj)}: C++ module generated by NLCL (edit freely).",
             "// Builds the world, wires the channels and runs the fixed-step loop.",
             "#include <csignal>", "#include <cstdlib>", "#include <iostream>", "#include <memory>",
             "#include <string>", "", '#include "nlcl_core.hpp"']
        for m in models:
            L.append(f'#include "{m.stem}.hpp"')
        L += ["", "namespace {", "",
              "void onSigint(int) { nlcl::g_stop = 1; }", "",
              "void usage() {",
              '    std::cout << "usage: app [--seconds N] [--simulate] [--dt S] [--fast] [--quiet]\\n"',
              '                 "  --seconds N  stop after N seconds of world time (default: run until Ctrl+C)\\n"',
              '                 "  --simulate   no network/hardware: invent incoming channel data and key presses\\n"',
              '                 "  --dt S       fixed time step in seconds (default 0.1)\\n"',
              '                 "  --fast       do not wait for real time between steps\\n"',
              '                 "  --quiet      only print behaviour logs and data lines\\n";',
              "}", "", "}  // namespace", "",
              "int main(int argc, char** argv) {",
              "    double seconds = 0.0, dt = 0.1;",
              "    bool simulate = false, quiet = false, fast = false;",
              "    for (int i = 1; i < argc; ++i) {",
              "        std::string a = argv[i];",
              '        if (a == "--seconds" && i + 1 < argc) seconds = std::atof(argv[++i]);',
              '        else if (a == "--dt" && i + 1 < argc) dt = std::atof(argv[++i]);',
              '        else if (a == "--simulate") simulate = true;',
              '        else if (a == "--quiet") quiet = true;',
              '        else if (a == "--fast") fast = true;',
              '        else if (a == "--help" || a == "-h") { usage(); return 0; }',
              '        else { std::cerr << "unknown argument: " << a << "\\n"; usage(); return 2; }',
              "    }",
              "    if (dt <= 0.0) dt = 0.1;",
              "    std::signal(SIGINT, onSigint);",
              "",
              "    nlcl::World world;",
              "    world.simulate = simulate;",
              "    world.verbose = !quiet;", ""]
        if models:
            L.append("    // entities")
        for m in models:
            L.append(f"    world.add(std::make_unique<{m.cls}>(world));  // {_cmt(m.e.kind)} {_cmt(m.e.name)}")
        ids = {e.id: e for e in ctx.entities}
        parents = [(m.e, ids[m.e.parent]) for m in models if m.e.parent in ids]
        if parents:
            L += ["", "    // hierarchy (\"X in Y\")"]
            for child, par in parents:
                L.append(f"    if (nlcl::Entity* e = world.find({_cstr(child.ident)})) e->setParent({_cstr(par.ident)});")
        if ctx.channels:
            L += ["", "    // channels: flat JSON {\"channel\", \"from\", field...}"]
        for c in ctx.channels:
            ls, ld = c.src.id in local, c.dst.id in local
            L.append(f"    {{  // {_cmt(c.src.name)} ({c.src.domain}) -> {_cmt(c.dst.name)} ({c.dst.domain}) "
                     f"via {c.protocol}: {_cmt(', '.join(map(str, c.fields or [])))}")
            cfg = dict(c.config or {})
            cfg.setdefault("interval_ms", 1000)
            cfg_txt = ", ".join(f"{{{_cstr(k)}, {_value_lit(v)}}}" for k, v in cfg.items()
                                if isinstance(v, (int, float, str, bool, list)))
            fields = ", ".join(_cstr(f) for f in c.fields or [])
            L.append(f"        auto ch = nlcl::makeChannel({_cstr(c.protocol)}, {_cstr(c.id)}, {_cstr(c.src.ident)}, "
                     f"{_cstr(c.dst.ident)}, {{{fields}}},")
            L.append(f"                                  nlcl::Fields{{{cfg_txt}}}, {str(ls).lower()}, {str(ld).lower()});")
            if not ls:
                seed = [(f, (c.src.props or {}).get(f)) for f in c.fields or []]
                seed = [(f, v) for f, v in seed if isinstance(v, (int, float, str, bool, list))
                        and (not isinstance(v, list) or _typ(v) == "vec")]
                if seed:
                    L.append("        ch->seed = nlcl::Fields{" + ", ".join(
                        f"{{{_cstr(f)}, {_value_lit(v)}}}" for f, v in seed) + "};  // simulate mode starts here")
            if c.protocol in ("spi", "i2c", "gpio", "wire"):
                L.append(f"        // note: {c.protocol} is an electrical bus; here it is carried as JSON lines on stdio")
            L.append("        world.addChannel(std::move(ch));")
            L.append("    }")
        keys = []
        for m in models:
            for b in m.e.behaviors or []:
                if not isinstance(b, dict):
                    continue
                key, k = _trigger(b)
                if key == "input" and k and b.get("do"):
                    k = self._ref_name(ctx, k) if self._find(ctx, k) else k
                    if k not in keys:
                        keys.append(k)
        if keys:
            L += ["", "    // keys used by \"input\" behaviours: type them + Enter (simulated with --simulate)",
                  "    world.inputKeys = {" + ", ".join(_cstr(k) for k in keys) + "};"]
        L += ["",
              f"    std::cout << \"[\" << {_cstr(proj)} << \"] running \" << world.entities.size() << \" entities, \"",
              "              << world.channels.size() << \" channels\" << (simulate ? \" (simulated)\" : \"\")",
              "              << (seconds > 0 ? \"\" : \" - Ctrl+C to stop\") << std::endl;",
              "    world.run(seconds, dt, !fast);",
              "    world.printState();",
              "    return 0;",
              "}", ""]
        return "\n".join(L)

    # ------------------------------------------------------------------ project files
    def _cmake(self, proj: str, models: list[_Model]) -> str:
        srcs = " ".join(["src/main.cpp", "src/nlcl_channels.cpp"] + [f"src/{m.stem}.cpp" for m in models])
        t = f"{proj}_app"
        return f"""# CMake build for the NLCL C++ module (any C++17 compiler).
#   cmake -S . -B build && cmake --build build
cmake_minimum_required(VERSION 3.10)
project({proj} LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
find_package(Threads REQUIRED)

add_executable({t} {srcs})
target_include_directories({t} PRIVATE include src)
target_link_libraries({t} PRIVATE Threads::Threads)
if(WIN32)
  target_link_libraries({t} PRIVATE ws2_32)
endif()
if(MSVC)
  target_compile_options({t} PRIVATE /utf-8 /W3 /EHsc)
else()
  target_compile_options({t} PRIVATE -Wall)
endif()
"""

    def _scripts(self, models: list[_Model]) -> tuple[str, str]:
        srcs = " ".join(["src/main.cpp", "src/nlcl_channels.cpp"] + [f"src/{m.stem}.cpp" for m in models])
        wsrcs = srcs.replace("/", "\\")
        sh = f"""#!/bin/sh
# Build without CMake (GCC or Clang). Usage: ./build.sh && ./app --simulate --seconds 5
set -e
cd "$(dirname "$0")"
CXX="${{CXX:-$(command -v g++ || command -v clang++)}}"
EXTRA="-pthread"
case "$(uname -s 2>/dev/null)" in MINGW*|MSYS*|CYGWIN*) EXTRA="-lws2_32";; esac
"$CXX" -std=c++17 -O2 -Wall -Iinclude -Isrc {srcs} -o app $EXTRA
echo "built ./app"
"""
        bat = f"""@echo off
rem Build without CMake on Windows: MinGW-w64 g++ or clang++ if on PATH, else MSVC cl
rem (run cl from a "Developer Command Prompt for VS"). Then: app.exe --simulate --seconds 5
cd /d "%~dp0"
where g++ >nul 2>nul
if not errorlevel 1 (
  g++ -std=c++17 -O2 -Iinclude -Isrc {wsrcs} -o app.exe -lws2_32 || exit /b 1
  goto done
)
where clang++ >nul 2>nul
if not errorlevel 1 (
  clang++ -std=c++17 -O2 -Iinclude -Isrc {wsrcs} -o app.exe -lws2_32 || exit /b 1
  goto done
)
where cl >nul 2>nul
if not errorlevel 1 (
  cl /nologo /std:c++17 /EHsc /utf-8 /O2 /Iinclude /Isrc {wsrcs} /Feapp.exe ws2_32.lib || exit /b 1
  goto done
)
echo No C++ compiler found. Install MinGW-w64 (MSYS2 or WinLibs), LLVM clang or the free MSVC Build Tools.
exit /b 1
:done
echo built app.exe
"""
        return sh, bat

    @staticmethod
    def _amalgamate(parts: list[tuple[str, str]]) -> str:
        """Concatenate files into one translation unit (local includes and pragma once removed)."""
        out = ["// Single-file build of the NLCL C++ module - paste into an online compiler",
               "// (Compiler Explorer / Wandbox / OnlineGDB) with -std=c++17 and run it with",
               "// arguments: --simulate --seconds 3    (Windows: link ws2_32)", ""]
        for name, text in parts:
            out.append(f"// ======================================================== {name}")
            for ln in text.splitlines():
                if re.match(r'\s*#\s*include\s+"[^"]+"', ln) or re.match(r"\s*#\s*pragma\s+once", ln):
                    continue
                out.append(ln)
            out.append("")
        return "\n".join(out) + "\n"

    def _readme(self, ctx: BuildContext, proj: str, models: list[_Model]) -> str:
        local = {e.id for e in ctx.entities}
        L = [f"# {ctx.system.name} - C++ module", "",
             "Native C++17 program generated by NLCL. It uses only the C++ standard library",
             "and the operating system's socket API (Winsock2 on Windows, BSD sockets elsewhere).", "",
             "## Layout", "",
             "| file | contents |", "|---|---|",
             "| `include/nlcl_core.hpp` | runtime: `nlcl::Value`, flat JSON, `Entity` base, `World` loop |",
             "| `src/nlcl_channels.hpp` / `.cpp` | transports: HTTP hub client, UDP, serial-style JSON lines, MQTT |",
             "| `src/main.cpp` | creates the entities, wires the channels, command line |"]
        for m in models:
            L.append(f"| `include/{m.stem}.hpp`, `src/{m.stem}.cpp` | class `{m.cls}` ({_cmt(m.e.kind)} "
                     f"\"{_cmt(m.e.name)}\") |")
        L += ["| `single/" + proj + ".cpp` | the whole program in one file (online compilers) |",
              "| `CMakeLists.txt`, `build.sh`, `build.bat` | build scripts |", ""]
        if models:
            L += ["## Entities", ""]
            for m in models:
                props = ", ".join(f"`{p}` ({_CTYPE[t]})" for p, t in m.props.items()) or "none"
                L.append(f"- **{m.e.name}** - {m.e.kind}; properties: {props}")
                for b in m.e.behaviors or []:
                    if isinstance(b, dict):
                        L.append(f"  - {self._describe(b)}")
            L.append("")
        if ctx.channels:
            L += ["## Channels", "",
                  "| id | protocol | from | to | fields | here |", "|---|---|---|---|---|---|"]
            for c in ctx.channels:
                role = ("local (in memory)" if c.src.id in local and c.dst.id in local
                        else "sends" if c.src.id in local else "receives")
                L.append(f"| {c.id} | {c.protocol} | {c.src.name} ({c.src.domain}) | {c.dst.name} ({c.dst.domain}) "
                         f"| {', '.join(map(str, c.fields or []))} | {role} |")
            L += ["", "Messages are flat JSON objects: `{\"channel\": id, \"from\": sender, field: value, ...}`.",
                  "Senders publish their fields every `interval_ms`; receivers copy fields onto same-named",
                  "properties and fire `message` behaviours.", "",
                  "- **http / websocket** - POST/GET `http://localhost:<port>/channel/<id>` on the NLCL hub.",
                  "  Start the hub first: `python ../python/main.py` (generated next to this folder).",
                  "- **udp** - one JSON datagram per message to 127.0.0.1:<port>.",
                  "- **serial** (and spi/i2c/gpio/wire) - data lines are printed on stdout; incoming JSON lines",
                  "  are read from stdin. Connect a real port with a bridge, e.g. on Linux",
                  "  `stty -F /dev/ttyUSB0 115200 raw && ./app < /dev/ttyUSB0 > /dev/ttyUSB0`, or pipe",
                  "  two programs: `python ../python/main.py | ./app`.",
                  "- **mqtt** - built-in minimal MQTT 3.1.1 client (QoS 0) to the configured broker.",
                  "- `--simulate` invents plausible incoming data instead of using the network.", ""]
        L += ["## Build and run (free tools)", "",
              "CMake (any compiler):", "", "```sh", "cmake -S . -B build", "cmake --build build",
              f"./build/{proj}_app --simulate --seconds 5        # Windows/MSVC: build\\Debug\\{proj}_app.exe",
              "```", "",
              "Without CMake:", "", "```sh",
              "# Linux / macOS (GCC or clang)",
              "g++ -std=c++17 -O2 -Iinclude -Isrc src/*.cpp -o app -pthread",
              "# Windows, MinGW-w64 g++ (MSYS2 `pacman -S mingw-w64-ucrt-x86_64-gcc` or WinLibs)",
              "g++ -std=c++17 -O2 -Iinclude -Isrc src/*.cpp -o app.exe -lws2_32",
              "# Windows, LLVM clang++",
              "clang++ -std=c++17 -O2 -Iinclude -Isrc src/*.cpp -o app.exe -lws2_32",
              "# Windows, free MSVC Build Tools (Developer Command Prompt)",
              "cl /std:c++17 /EHsc /utf-8 /Iinclude /Isrc src\\*.cpp /Fe:app.exe ws2_32.lib",
              "```", "",
              "Or just `./build.sh` (Linux/macOS/MSYS2) / `build.bat` (Windows).", "",
              "Online, no install: open https://godbolt.org (Compiler Explorer), paste",
              f"`single/{proj}.cpp`, choose x86-64 gcc, add `-std=c++17`, enable \"Execute the code\"",
              "and set the arguments to `--simulate --seconds 3`. Wandbox and OnlineGDB work the same way.", "",
              "### Command line", "",
              "| flag | meaning |", "|---|---|",
              "| `--seconds N` | stop after N seconds of world time (default: until Ctrl+C) |",
              "| `--simulate` | no network/hardware: fake incoming data and key presses |",
              "| `--dt S` | fixed step in seconds (default 0.1) |",
              "| `--fast` | do not wait for real time between steps |",
              "| `--quiet` | print only behaviour logs and data lines |", "",
              "Keyboard input: type a key name used by an `input` behaviour (e.g. `space`) and press Enter.",
              "At the end the program prints the final state of every entity.", "",
              "Notes: MinGW needs a POSIX-threads build (MSYS2 and WinLibs are) for `std::thread`.",
              "Properties whose names are C++ keywords get a trailing underscore (e.g. `class_`).", ""]
        return "\n".join(L)

    # ------------------------------------------------------------------ build
    def generate(self, ctx: BuildContext) -> dict[str, str]:
        proj = re.sub(r"_+", "_", ident(ctx.project or ctx.system.name or "project")) or "project"
        self._local_models = {}
        models = self._models(ctx)
        self._local_models = {m.e.ident: m for m in models}
        files: dict[str, str] = {}
        core = (ASSETS / "nlcl_core.hpp").read_text(encoding="utf-8")
        chan_h = (ASSETS / "nlcl_channels.hpp").read_text(encoding="utf-8")
        chan_c = (ASSETS / "nlcl_channels.cpp").read_text(encoding="utf-8")
        files["include/nlcl_core.hpp"] = core
        files["src/nlcl_channels.hpp"] = chan_h
        files["src/nlcl_channels.cpp"] = chan_c
        heads, srcs = [], []
        for m in models:
            h, s = self._class_files(ctx, m)
            files[f"include/{m.stem}.hpp"] = h
            files[f"src/{m.stem}.cpp"] = s
            heads.append((f"include/{m.stem}.hpp", h))
            srcs.append((f"src/{m.stem}.cpp", s))
        main = self._main(ctx, models)
        files["src/main.cpp"] = main
        files["CMakeLists.txt"] = self._cmake(proj, models)
        sh, bat = self._scripts(models)
        files["build.sh"] = sh
        files["build.bat"] = bat.replace("\n", "\r\n")
        # socket headers come last so their macros never touch entity code
        files[f"single/{proj}.cpp"] = self._amalgamate(
            [("include/nlcl_core.hpp", core)] + heads + srcs + [("src/main.cpp", main),
                                                                 ("src/nlcl_channels.hpp", chan_h),
                                                                 ("src/nlcl_channels.cpp", chan_c)])
        files["README.md"] = self._readme(ctx, proj, models)
        return files

    # ------------------------------------------------------------------ run
    def run(self, ctx: BuildContext) -> RunResult | None:
        out = ctx.outdir
        if not (out / "src" / "main.cpp").exists():
            return RunResult(False, "build first")
        win = os.name == "nt"
        srcs = [str(Path("src") / p.name) for p in sorted((out / "src").glob("*.cpp"))]
        bdir = out / "build"
        bdir.mkdir(exist_ok=True)
        exe = bdir / ("app.exe" if win else "app")
        gxx = self.which("g++", "clang++")
        zig = None if gxx else self.which("zig")
        cl = None if gxx or zig else self.which("cl")
        flags = ["-std=c++17", "-O1", "-Iinclude", "-Isrc", *srcs, "-o", str(exe)]
        flags += ["-lws2_32"] if win else ["-pthread"]
        if gxx:
            cmd = [gxx, *flags]
        elif zig:
            cmd = [zig, "c++", *flags]
        elif cl:
            cmd = [cl, "/nologo", "/std:c++17", "/EHsc", "/utf-8", "/Iinclude", "/Isrc", *srcs,
                   f"/Fe{exe}", f"/Fo{bdir}{os.sep}", "ws2_32.lib"]
        else:
            return None
        comp = self.sh(cmd, out, timeout=300)
        if not comp.ok or not exe.exists():
            return RunResult(False, "compile failed:\n" + comp.output, comp.command)
        r = self.sh([str(exe), "--simulate", "--seconds", "2"], out, timeout=20)
        return RunResult(r.ok, (comp.output + "\n" if comp.output else "") + r.output, comp.command + " && " + r.command)


MODULE = CppModule()
