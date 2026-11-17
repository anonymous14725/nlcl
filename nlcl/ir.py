"""Universal Computational Representation (IR).

Everything the user says is reduced to a small set of domain-independent
operations (Op) that mutate a persistent SystemState made of Entities and
Links.  Domain modules (game, python, embedded, hardware, ...) only ever see
this representation, never the original natural language.
"""
from __future__ import annotations

import copy
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

# --------------------------------------------------------------------------
# Operations
# --------------------------------------------------------------------------

STATE_ACTIONS = {
    "CREATE",      # create entity (domain, kind, name, props, count, parent)
    "SET",         # set properties on target
    "UNSET",       # remove properties from target
    "DELETE",      # delete target
    "CONNECT",     # link target -> dst (via protocol)
    "DISCONNECT",  # remove link(s) between target and dst
    "RENAME",      # rename target to name
    "BEHAVIOR",    # attach behaviour rule to target
    "DOMAIN",      # set default domain for subsequent CREATEs
    "FOCUS",       # make target the current "it"
}
META_ACTIONS = {"SHOW", "BUILD", "RUN", "UNDO", "DEFINE", "HELP", "FIX", "SHELL"}
ACTIONS = STATE_ACTIONS | META_ACTIONS

PROTOCOLS = {"serial", "uart", "http", "mqtt", "gpio", "spi", "i2c", "udp", "websocket", "wire"}


@dataclass
class Op:
    action: str
    target: str | None = None
    domain: str | None = None
    kind: str | None = None
    name: str | None = None
    props: dict = field(default_factory=dict)
    dst: str | None = None
    via: str | None = None
    behavior: dict | None = None
    parent: str | None = None
    count: int = 1
    text: str | None = None  # free argument (DEFINE body, SHOW what, BUILD dir ...)

    def to_dict(self) -> dict:
        d = asdict(self)
        return {k: v for k, v in d.items()
                if v not in (None, {}, []) and not (k == "count" and v == 1)}

    @classmethod
    def from_dict(cls, d: dict) -> "Op":
        if not isinstance(d, dict):
            raise ValueError(f"op must be an object, got {type(d).__name__}")
        action = str(d.get("action", "")).upper()
        if action not in ACTIONS:
            raise ValueError(f"unknown action {action!r}")
        known = {f for f in cls.__dataclass_fields__}
        kw = {k: v for k, v in d.items() if k in known}
        kw["action"] = action
        if kw.get("props") is None:
            kw["props"] = {}
        if not isinstance(kw["props"], dict):
            raise ValueError("props must be an object")
        kw["props"] = {norm_name(k): v for k, v in kw["props"].items()}
        if kw.get("domain"):
            kw["domain"] = str(kw["domain"]).lower()
        if kw.get("via"):
            kw["via"] = str(kw["via"]).lower()
        try:
            kw["count"] = max(1, min(100, int(kw.get("count") or 1)))
        except (TypeError, ValueError):
            kw["count"] = 1
        op = cls(**kw)
        if op.behavior is not None:
            op.behavior = validate_behavior(op.behavior)
        return op

    def __str__(self) -> str:
        parts = []
        for k, v in self.to_dict().items():
            if k == "action":
                continue
            if k == "domain":
                v = str(v).upper()
            parts.append(f"{'type' if k == 'kind' else ('properties' if k == 'props' else k)}="
                         f"{json.dumps(v, ensure_ascii=False) if not isinstance(v, str) or k in ('name', 'text') else v}")
        return f"{self.action}(" + ", ".join(parts) + ")"


# --------------------------------------------------------------------------
# Entities / links / state
# --------------------------------------------------------------------------

@dataclass
class Entity:
    id: str
    name: str
    domain: str
    kind: str
    props: dict = field(default_factory=dict)
    behaviors: list = field(default_factory=list)
    parent: str | None = None

    @property
    def ident(self) -> str:
        """Identifier-safe name (snake_case)."""
        return ident(self.name)

    @property
    def class_name(self) -> str:
        return pascal(self.name)


@dataclass
class Link:
    id: str
    src: str
    dst: str
    via: str | None = None
    props: dict = field(default_factory=dict)


class SystemState:
    """Persistent computational state built incrementally by instructions."""

    def __init__(self, name: str = "project"):
        self.name = name
        self.entities: dict[str, Entity] = {}
        self.links: dict[str, Link] = {}
        self.focus: str | None = None
        self.focus_group: list[str] = []      # entities made by the last "create 3 ..." (for "them")
        self.default_domain: str | None = None
        self.patches: list[dict] = []
        self.shell_log: list[dict] = []        # terminal actions taken (exported as bash + PowerShell scripts)         # fixes applied to generated files, re-applied on rebuild
        self._counter = 0
        self._undo: list[str] = []
        self.history: list[str] = []

    # ---- ids / snapshots -------------------------------------------------
    def new_id(self, kind: str) -> str:
        self._counter += 1
        return f"{ident(kind) or 'obj'}_{self._counter}"

    def snapshot(self) -> None:
        self._undo.append(self.to_json())
        if len(self._undo) > 200:
            self._undo.pop(0)

    def undo(self) -> bool:
        if not self._undo:
            return False
        prev = SystemState.from_json(self._undo.pop())
        undo_stack = self._undo
        self.__dict__.update(prev.__dict__)
        self._undo = undo_stack
        return True

    def clone(self) -> "SystemState":
        c = SystemState.from_json(self.to_json())
        return c

    # ---- lookup ------------------------------------------------------------
    def resolve(self, ref: str | None) -> Entity | None:
        if ref is None or str(ref).lower() in {"it", "this", "that", "them", "آن", "این", "اون", "اینو", "اونو"}:
            return self.entities.get(self.focus) if self.focus else None
        r = str(ref).strip().lower()
        r = re.sub(r"^(the|a|an)\s+", "", r)
        if r in self.entities:
            return self.entities[r]
        by_name = [e for e in self.entities.values() if e.name.lower() == r or e.ident == ident(r)]
        if by_name:
            return by_name[-1]
        by_kind = [e for e in self.entities.values() if e.kind.lower() == r or e.kind.lower() == singular(r)]
        if by_kind:
            return by_kind[-1]
        # partial: "temperature sensor" -> entity named temp_sensor or kind sensor
        words = [singular(w) for w in r.split()]
        for e in reversed(list(self.entities.values())):
            if e.kind.lower() in words or any(w and w in e.name.lower() for w in words if len(w) > 2):
                return e
        return None

    def unique_name(self, base: str) -> str:
        base = ident(base) or "obj"
        names = {e.name for e in self.entities.values()}
        if base not in names:
            return base
        i = 2
        while f"{base}{i}" in names:
            i += 1
        return f"{base}{i}"

    def children(self, eid: str) -> list[Entity]:
        return [e for e in self.entities.values() if e.parent == eid]

    def links_of(self, eid: str) -> list[Link]:
        return [l for l in self.links.values() if eid in (l.src, l.dst)]

    def domains(self) -> list[str]:
        out: list[str] = []
        for e in self.entities.values():
            if e.domain not in out:
                out.append(e.domain)
        return out

    def in_domain(self, domain: str) -> list[Entity]:
        return [e for e in self.entities.values() if e.domain == domain]

    # ---- serialisation -----------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "focus": self.focus,
            "focus_group": self.focus_group,
            "default_domain": self.default_domain,
            "counter": self._counter,
            "entities": [asdict(e) for e in self.entities.values()],
            "links": [asdict(l) for l in self.links.values()],
            "history": self.history,
            "patches": self.patches,
            "shell_log": self.shell_log,
        }

    def to_json(self, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    @classmethod
    def from_dict(cls, d: dict) -> "SystemState":
        s = cls(d.get("name", "project"))
        s.focus = d.get("focus")
        s.focus_group = list(d.get("focus_group", []))
        s.default_domain = d.get("default_domain")
        s._counter = d.get("counter", 0)
        for e in d.get("entities", []):
            s.entities[e["id"]] = Entity(**e)
        for l in d.get("links", []):
            s.links[l["id"]] = Link(**l)
        s.history = list(d.get("history", []))
        s.patches = list(d.get("patches", []))
        s.shell_log = list(d.get("shell_log", []))
        return s

    @classmethod
    def from_json(cls, text: str) -> "SystemState":
        return cls.from_dict(json.loads(text))

    def summary(self) -> str:
        if not self.entities:
            return "(empty system)"
        lines = []
        for e in self.entities.values():
            mark = "*" if e.id == self.focus else " "
            props = ", ".join(f"{k}={v!r}" for k, v in e.props.items())
            par = f" in {self.entities[e.parent].name}" if e.parent in self.entities else ""
            lines.append(f"{mark} [{e.domain}] {e.kind} '{e.name}' ({e.id}){par} {{{props}}}")
            for b in e.behaviors:
                lines.append(f"      on {b['on']}" + (f" with {b['with']}" if b.get('with') else "")
                             + (f" if {b['if']}" if b.get('if') else "")
                             + " -> " + "; ".join(describe_action(a) for a in b["do"]))
        for l in self.links.values():
            s, d = self.entities.get(l.src), self.entities.get(l.dst)
            if s and d:
                extra = f" {l.props}" if l.props else ""
                lines.append(f"  link {s.name} -> {d.name} via {l.via or 'auto'}{extra}")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Behaviours and expressions
# --------------------------------------------------------------------------
# A behaviour is {"on": trigger, "if": expr|None, "do": [action, ...]}
#   triggers : start | update | collision | input:<key> | timer:<ms> |
#              change:<prop> | message:<link-or-entity>
#   actions  : {"op":"set","prop":p,"value":expr} {"op":"add","prop":p,"value":expr}
#              {"op":"toggle","prop":p} {"op":"destroy"} {"op":"log","text":s}
#              {"op":"move","x":expr,"y":expr,"z":expr} {"op":"send","to":ref,"fields":[..]}
#              {"op":"call","name":fn,"args":[expr..]}
# Expressions use a tiny C-like language: identifiers (entity properties),
# numbers, strings, true/false, + - * / % < > <= >= == != && || ! ( )

TRIGGER_RE = re.compile(r"^(start|update|collision|input:[\w ]+|timer:\d+|change:[\w.]+|message:[\w.\-]+)$")
ACTION_OPS = {"set", "add", "toggle", "destroy", "log", "move", "send", "call"}

_TOKEN_RE = re.compile(r"""\s*(?:
    (?P<num>\d+(?:\.\d+)?) |
    (?P<str>"[^"\n]*"|'[^'\n]*') |
    (?P<op>&&|\|\||<=|>=|==|!=|[-+*/%<>!()]) |
    (?P<id>[A-Za-z_][A-Za-z0-9_.]*)
)""", re.X)

_WORD_OPS = {"and": "&&", "or": "||", "not": "!", "True": "true", "False": "false"}


def expr_tokens(expr: str) -> list[tuple[str, str]]:
    expr = str(expr)
    pos, out = 0, []
    while pos < len(expr):
        if expr[pos:].strip() == "":
            break
        m = _TOKEN_RE.match(expr, pos)
        if not m or m.end() == pos:
            raise ValueError(f"bad expression near {expr[pos:]!r}")
        pos = m.end()
        kind = m.lastgroup
        val = m.group(kind)
        if kind == "id" and val in _WORD_OPS:
            val = _WORD_OPS[val]
            kind = "op" if val not in ("true", "false") else "id"
        out.append((kind, val))
    return out


def validate_expr(expr: Any) -> str:
    """Return normalised expression text or raise ValueError."""
    if isinstance(expr, bool):
        return "true" if expr else "false"
    if isinstance(expr, (int, float)):
        return repr(expr)
    toks = expr_tokens(str(expr))
    if not toks:
        raise ValueError("empty expression")
    depth = 0
    for k, v in toks:
        if v == "(":
            depth += 1
        elif v == ")":
            depth -= 1
            if depth < 0:
                raise ValueError("unbalanced parentheses")
    if depth:
        raise ValueError("unbalanced parentheses")
    # unary minus glued to its number: "- 10" -> "-10"
    out: list[str] = []
    for i, (k, v) in enumerate(toks):
        if k == "num" and out and out[-1] == "-" and (len(out) == 1 or out[-2] in
                                                       ("(", "+", "-", "*", "/", "%", "<", ">", "<=", ">=", "==", "!=", "&&", "||", "!")):
            out[-1] = "-" + v
        else:
            out.append(v)
    s = " ".join(out)
    return re.sub(r"\(\s+", "(", re.sub(r"\s+\)", ")", s))


def expr_names(expr: str) -> list[str]:
    return [v for k, v in expr_tokens(expr) if k == "id" and v not in ("true", "false")]


def render_expr(expr: Any, lang: str, name=lambda n: n) -> str:
    """Render a normalised expression for a target language.

    lang: python | c | cpp | csharp | js | gdscript | verilog | openscad
    name: callback mapping a property identifier to target syntax
          (e.g. lambda n: f"self.{n}").
    """
    toks = expr_tokens(validate_expr(expr))
    out = []
    for k, v in toks:
        if k == "id":
            if v in ("true", "false"):
                if lang == "python":
                    v = v.capitalize()
                elif lang == "verilog":
                    v = "1'b1" if v == "true" else "1'b0"
            else:
                v = name(v)
        elif k == "op" and lang in ("python", "gdscript"):
            v = {"&&": "and", "||": "or", "!": "not "}.get(v, v)
        elif k == "str" and lang in ("c", "cpp", "csharp", "verilog") and v.startswith("'"):
            v = '"' + v[1:-1].replace('"', '\\"') + '"'
        out.append(v)
    s = " ".join(out)
    s = re.sub(r"\(\s+", "(", s)
    s = re.sub(r"\s+\)", ")", s)
    s = s.replace("not  ", "not ").replace("! ", "!")
    return s


def eval_expr(expr: Any, env: dict) -> Any:
    """Evaluate a validated expression against a dict of names (design-time)."""
    code = render_expr(expr, "python", name=lambda n: f"_env.get({n!r}, 0)")
    try:
        return eval(code, {"__builtins__": {}}, {"_env": env})  # tokens are whitelisted by expr_tokens
    except Exception as e:
        raise ValueError(f"cannot evaluate {expr!r}: {e}") from None


def validate_behavior(b: Any) -> dict:
    if not isinstance(b, dict):
        raise ValueError("behavior must be an object")
    on = str(b.get("on") or "update").strip().lower()
    if not TRIGGER_RE.match(on):
        raise ValueError(f"bad trigger {on!r}")
    cond = b.get("if")
    cond = validate_expr(cond) if cond not in (None, "", True) else None
    acts = b.get("do")
    if isinstance(acts, dict):
        acts = [acts]
    if not acts or not isinstance(acts, list):
        raise ValueError("behavior needs at least one action in 'do'")
    clean = []
    for a in acts:
        if not isinstance(a, dict) or str(a.get("op", "")).lower() not in ACTION_OPS:
            raise ValueError(f"bad action {a!r}")
        a = dict(a)
        a["op"] = a["op"].lower()
        if a["op"] in ("set", "add", "toggle"):
            if not a.get("prop"):
                raise ValueError(f"action {a['op']} needs 'prop'")
            a["prop"] = norm_name(a["prop"])
        if a["op"] in ("set", "add"):
            a["value"] = validate_expr(a.get("value", 1 if a["op"] == "add" else 0))
        if a["op"] == "move":
            for ax in ("x", "y", "z"):
                a[ax] = validate_expr(a.get(ax, 0))
        if a["op"] == "log":
            a["text"] = str(a.get("text", ""))
        if a["op"] == "send":
            a["fields"] = [norm_name(f) for f in a.get("fields", [])]
        if a["op"] == "call":
            a["name"] = ident(a.get("name", "fn"))
            a["args"] = [validate_expr(x) for x in a.get("args", [])]
        clean.append(a)
    out = {"on": on, "if": cond, "do": clean}
    if b.get("with") and on == "collision":
        out["with"] = ident(str(b["with"]))      # collision partner (entity name or kind)
    return out


def describe_action(a: dict) -> str:
    op = a["op"]
    if op == "set":
        return f"{a['prop']} = {a['value']}"
    if op == "add":
        v = str(a["value"])
        if re.fullmatch(r"-[\d.]+", v):
            return f"{a['prop']} -= {v[1:]}"
        if v.startswith("-(") and v.endswith(")"):
            return f"{a['prop']} -= {v[2:-1]}"
        return f"{a['prop']} += {v}"
    if op == "toggle":
        return f"toggle {a['prop']}"
    if op == "move":
        return f"move ({a['x']}, {a['y']}, {a['z']})"
    if op == "log":
        return f"log {a['text']!r}"
    if op == "send":
        return f"send {a.get('fields') or 'state'} to {a.get('to') or 'links'}"
    if op == "call":
        return f"{a['name']}({', '.join(a['args'])})"
    return op


# --------------------------------------------------------------------------
# Naming helpers (shared with modules)
# --------------------------------------------------------------------------

_SYN = {
    "hp": "health", "life": "health", "lives": "lives", "moveable": "movable",
    "collision_detection": "collision", "collisions": "collision", "collider": "collision",
    "colour": "color", "temp": "temperature", "velocity": "speed",
}


def norm_name(s: Any) -> str:
    n = ident(str(s))
    return _SYN.get(n, n)


_FA_LATIN = {
    "ا": "a", "آ": "a", "أ": "a", "إ": "e", "ب": "b", "پ": "p", "ت": "t", "ث": "s", "ج": "j", "چ": "ch",
    "ح": "h", "خ": "kh", "د": "d", "ذ": "z", "ر": "r", "ز": "z", "ژ": "zh", "س": "s", "ش": "sh", "ص": "s",
    "ض": "z", "ط": "t", "ظ": "z", "ع": "a", "غ": "gh", "ف": "f", "ق": "gh", "ک": "k", "ك": "k", "گ": "g",
    "ل": "l", "م": "m", "ن": "n", "و": "v", "ه": "h", "ة": "h", "ی": "i", "ي": "i", "ئ": "y", "ؤ": "o",
    "ء": "", "‌": "_", "ـ": "",
}


def transliterate(s: str) -> str:
    """Persian/Arabic letters -> ASCII so every target language gets a valid identifier."""
    return "".join(_FA_LATIN.get(c, c) for c in s)


def ident(s: str) -> str:
    raw = str(s).strip()
    s = re.sub(r"[^0-9A-Za-z_]+", "_", transliterate(raw)).strip("_").lower()
    if not s and raw:
        import zlib
        s = f"e_{zlib.crc32(raw.encode('utf-8')):08x}"
    if s and s[0].isdigit():
        s = "n" + s
    return s


def pascal(s: str) -> str:
    return "".join(p[:1].upper() + p[1:] for p in ident(s).split("_") if p) or "Obj"


def camel(s: str) -> str:
    p = pascal(s)
    return p[:1].lower() + p[1:]


def singular(w: str) -> str:
    w = w.lower()
    if w.endswith(("us", "is", "ss", "os", "ics")) or w in ("gas", "lens", "alias", "atlas", "canvas", "series"):
        return w  # torus, status, axis, chassis, physics ...
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 3 and w.endswith("es") and w[-3] in "sxz":
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def parse_value(v: str) -> Any:
    if isinstance(v, (int, float, bool)):
        return v
    s = str(v).strip()
    low = s.lower()
    if low in ("true", "yes", "on", "enabled", "روشن"):
        return True
    if low in ("false", "no", "off", "disabled", "خاموش"):
        return False
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    if re.fullmatch(r"-?\d+\.\d*|-?\.\d+", s):
        return float(s)
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    return s


def deep_copy(x):
    return copy.deepcopy(x)
