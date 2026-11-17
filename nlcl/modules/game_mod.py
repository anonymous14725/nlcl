"""Game domain: Unity (C#), Godot 4 (GDScript) and a self-contained HTML5 preview.

Every entity becomes one script per engine.  The scene is built in code (Unity:
a RuntimeInitializeOnLoadMethod bootstrap, Godot: main.gd), so nothing has to
be wired up by hand: drop the Scripts folder into Assets/ and press Play, or
open godot/project.godot and press F5.  preview.html simulates the same
entities and behaviours in a browser (2D side view, AABB physics).

Shared runtime code lives in _assets/game/: NlclEntity.cs, NlclHub.cs,
NlclInput.cs (Unity), nlcl_world.gd (Godot), preview_runtime.js (preview).
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..ir import Entity, describe_action, expr_tokens, ident, pascal, validate_expr
from .base import BuildContext, DomainModule, RunResult

ASSETS = Path(__file__).parent / "_assets" / "game"

# ---------------------------------------------------------------- kinds and looks
KINDS: dict[str, dict] = {
    "object": {},
    "player": {"health": 100, "speed": 5, "movable": True, "collision": True, "gravity": True,
               "jump_force": 8, "score": 0},
    "character": {"health": 100, "speed": 5, "movable": True, "collision": True},
    "npc": {"speed": 2, "collision": True},
    "enemy": {"health": 30, "speed": 2, "damage": 10, "collision": True},
    "coin": {"value": 1, "collision": True},
    "pickup": {"value": 10, "collision": True},
    "bullet": {"speed": 12, "damage": 10, "collision": True},
    "projectile": {"speed": 12, "damage": 10, "collision": True},
    "platform": {"collision": True},
    "wall": {"collision": True},
    "obstacle": {"damage": 10, "collision": True},
    "spawner": {"spawn": "", "spawn_interval_ms": 2000, "max_spawn": 10, "collision": False},
    "camera": {"follow": ""},
    "level": {},
    "cube": {},
    "sphere": {},
    "capsule": {},
}
# kind -> (shape, size (w, h, d), colour)
LOOK = {
    "object": ("cube", (1, 1, 1), "#94a3b8"),
    "player": ("capsule", (0.8, 1.6, 0.8), "#3b82f6"),
    "character": ("capsule", (0.8, 1.6, 0.8), "#6366f1"),
    "npc": ("capsule", (0.8, 1.6, 0.8), "#a855f7"),
    "enemy": ("cube", (1, 1, 1), "#ef4444"),
    "coin": ("sphere", (0.6, 0.6, 0.6), "#facc15"),
    "pickup": ("sphere", (0.7, 0.7, 0.7), "#22c55e"),
    "bullet": ("sphere", (0.3, 0.3, 0.3), "#f97316"),
    "projectile": ("sphere", (0.3, 0.3, 0.3), "#f97316"),
    "platform": ("cube", (6, 0.5, 2), "#64748b"),
    "wall": ("cube", (0.5, 4, 2), "#475569"),
    "obstacle": ("cube", (1, 1, 1), "#b45309"),
    "spawner": ("cube", (0.8, 0.8, 0.8), "#14b8a6"),
    "camera": ("none", (0, 0, 0), ""),
    "level": ("none", (0, 0, 0), ""),
    "cube": ("cube", (1, 1, 1), "#94a3b8"),
    "sphere": ("sphere", (1, 1, 1), "#94a3b8"),
    "capsule": ("capsule", (1, 2, 1), "#94a3b8"),
}
SHAPES = {"cube": "cube", "box": "cube", "square": "cube", "rectangle": "cube", "sphere": "sphere",
          "ball": "sphere", "circle": "sphere", "capsule": "capsule", "pill": "capsule",
          "cylinder": "cylinder", "none": "none", "empty": "none", "invisible": "none"}
AI_KINDS = {"enemy", "npc", "bullet", "projectile", "coin", "pickup", "platform", "wall", "obstacle",
            "spawner", "camera", "level"}
SOLID_KINDS = {"platform", "wall", "obstacle"}
LAYOUT_PROPS = {"position", "pos", "x", "y", "z", "size", "width", "height", "depth", "radius",
                "shape", "rotation", "scale"}
REF_PROPS = ("spawn", "target", "follow")      # string props that name another entity

COLORS = {
    "red": "#ef4444", "green": "#22c55e", "blue": "#3b82f6", "yellow": "#facc15", "orange": "#f97316",
    "purple": "#a855f7", "violet": "#8b5cf6", "pink": "#ec4899", "cyan": "#06b6d4", "teal": "#14b8a6",
    "white": "#f8fafc", "black": "#111827", "gray": "#9ca3af", "grey": "#9ca3af", "brown": "#92400e",
    "gold": "#eab308", "silver": "#cbd5e1", "lime": "#84cc16", "magenta": "#d946ef", "navy": "#1e3a8a",
    "maroon": "#7f1d1d", "olive": "#65a30d", "indigo": "#6366f1", "beige": "#e7d8b0",
    "dark blue": "#1e40af", "light blue": "#93c5fd", "dark green": "#166534", "light green": "#86efac",
    "dark red": "#991b1b", "darkblue": "#1e40af", "lightblue": "#93c5fd", "darkgreen": "#166534",
}

# ---------------------------------------------------------------- reserved names
CS_KEYWORDS = set("""abstract as base bool break byte case catch char checked class const continue decimal
default delegate do double else enum event explicit extern false finally fixed float for foreach goto if
implicit in int interface internal is lock long namespace new null object operator out override params
private protected public readonly ref return sbyte sealed short sizeof stackalloc static string struct
switch this throw true try typeof uint ulong unchecked unsafe ushort using virtual void volatile while
var dynamic value now fire field other key""".split())
CS_MEMBERS = set("""name tag transform gameObject enabled isActiveAndEnabled useGUILayout hideFlags rigidbody
rigidbody2D collider collider2D renderer camera light animation audio constantForce hingeJoint networkView
particleSystem runInEditMode destroyCancellationToken didStart didAwake""".split())
CS_TYPES = set("""Vector2 Vector3 Quaternion Time Mathf Debug KeyCode GameObject Color Rigidbody Collider
Camera Light PrimitiveType Object Renderer Shader Material Physics Transform MonoBehaviour Component
Behaviour BoxCollider SphereCollider CapsuleCollider ColorUtility RigidbodyConstraints LightType String
Input Random Application Screen Mesh Ray RaycastHit Collision Coroutine Resources Texture Sprite
RuntimeInitializeOnLoadMethod RuntimeInitializeLoadType RuntimeInitializeOnLoadMethodAttribute Math
Action Array Exception Type Convert Console Enum Nlcl NlclGame NlclEntity NlclWorld NlclHub NlclInput
NlclJson NlclSceneBuilder Ch Hit Log Jump Spawn Start Update Awake LateUpdate SetX SetY SetZ""".split())
GD_RESERVED = set("""if elif else for while match break continue pass return class class_name extends is in
as self signal func static const enum var breakpoint preload await yield super tool void and or not when
true false null PI TAU INF NAN int float bool String Array Dictionary Vector2 Vector3 Color Object Node
print str range len min max abs sign load
name owner process_mode process_priority editor_description unique_name_in_owner scene_file_path
multiplayer transform global_transform position rotation rotation_degrees quaternion basis scale
rotation_edit_mode rotation_order top_level global_position global_basis global_rotation visible
visibility_parent disable_mode collision_layer collision_mask collision_priority input_ray_pickable
mass inertia center_of_mass physics_material_override gravity_scale custom_integrator continuous_cd
contact_monitor max_contacts_reported sleeping can_sleep lock_rotation freeze freeze_mode
linear_velocity linear_damp angular_velocity angular_damp constant_force constant_torque
constant_linear_velocity constant_angular_velocity fov near far current size projection environment
attributes keep_aspect cull_mask h_offset v_offset doppler_tracking frustum_offset compositor
script delta event value field key other now fire""".split())

UNITY_KEYS = {"space": "Space", "enter": "Return", "up": "UpArrow", "down": "DownArrow", "left": "LeftArrow",
              "right": "RightArrow", "escape": "Escape", "shift": "LeftShift", "ctrl": "LeftControl",
              "alt": "LeftAlt", "tab": "Tab", "backspace": "Backspace"}
KEY_ALIASES = {"return": "enter", "esc": "escape", "spacebar": "space", "control": "ctrl",
               "click": "mouse", "left click": "mouse", "mouse click": "mouse", "left mouse": "mouse",
               "tap": "mouse", "touch": "mouse"}
NUM_RE = re.compile(r"-?\d+(\.\d+)?")


def _key(k: str) -> str:
    k = re.sub(r"\b(key|arrow|button|bar|pressed)\b", " ", str(k).lower().replace("_", " "))
    k = re.sub(r"\s+", " ", k).strip()
    return KEY_ALIASES.get(k, k) or "space"


def _ptype(v: Any) -> str:
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, (list, tuple)):
        return "nlist" if all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v) else "slist"
    return "string"


def _num(v: Any, d: float) -> float:
    try:
        f = float(v)
        return f if f == f and abs(f) < 1e6 else d
    except (TypeError, ValueError):
        return d


def _vec(v: Any) -> list[float] | None:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return [float(v)] * 3
    if isinstance(v, str):
        v = [x for x in re.split(r"[\s,;x]+", v.strip("()[] ")) if x]
    if isinstance(v, (list, tuple)) and v:
        out = [_num(x, 0.0) for x in list(v)[:3]]
        return out + [0.0] * (3 - len(out))
    return None


def _f(x: float) -> str:
    """Compact float for generated code."""
    s = f"{round(float(x), 3):g}"
    return s if s not in ("-0",) else "0"


_BINOPS = {"&&", "||", "<", ">", "<=", ">=", "==", "!=", "+", "-", "*", "/", "%"}


def _check_expr(expr: Any) -> str:
    """validate_expr plus a grammar check (operands around every operator)."""
    text = validate_expr(expr)
    toks = expr_tokens(text)
    pos = 0

    def primary():
        nonlocal pos
        if pos >= len(toks):
            raise ValueError("missing operand")
        k, v = toks[pos]
        if v in ("!", "-", "+") and k == "op":
            pos += 1
            return primary()
        if v == "(":
            pos += 1
            binary()
            if pos >= len(toks) or toks[pos][1] != ")":
                raise ValueError("missing )")
            pos += 1
            return
        if k == "op":
            raise ValueError(f"unexpected {v!r}")
        pos += 1

    def binary():
        nonlocal pos
        primary()
        while pos < len(toks) and toks[pos][0] == "op" and toks[pos][1] in _BINOPS:
            pos += 1
            primary()

    binary()
    if pos != len(toks):
        raise ValueError(f"unexpected {toks[pos][1]!r}")
    return text


@dataclass
class _Info:
    e: Entity
    id: str
    cls: str
    gd: str
    kind: str
    shape: str
    size: tuple
    color: str
    controls: bool
    auto_move: bool
    gravity: bool
    solid: bool
    props: dict                      # name -> value (fields of the generated class)
    behaviors: list
    parent: str = ""                 # local parent id
    pos: list = field(default_factory=lambda: [0.0, 0.0, 0.0])   # absolute
    rel: list = field(default_factory=lambda: [0.0, 0.0, 0.0])   # relative to parent
    explicit_pos: bool = False

    @property
    def visual(self) -> bool:
        return self.shape != "none"


class GameModule(DomainModule):
    name = "game"
    title = "Game (Unity C#, Godot 4, HTML5 preview)"
    aliases = ("unity", "godot", "بازی")
    keywords = ("game", "player", "enemy", "enemies", "coin", "coins", "platformer", "npc", "bullet",
                "projectile", "pickup", "powerup", "spawner", "sprite", "boss", "arena", "unity", "godot",
                "level", "hero", "monster", "zombie", "gem", "شخصیت", "دشمن", "سکه")
    kinds = KINDS
    suggested_props = ("health", "speed", "score", "damage", "color", "movable", "collision", "gravity",
                       "jump_force", "position", "size", "target", "spawn", "spawn_interval_ms")
    protocols = ("http", "websocket")
    toolchain = """Three free targets are generated in `game/`:

* **HTML5 preview** - open `game/preview.html` in any browser (arrow keys/WASD move, space jumps,
  click entities). Headless check: `node --check` on its script; the simulation also runs under
  Node (`node preview.js 5` after extracting the `<script>`).
* **Unity** (free Personal edition, 2021.3 LTS or newer): create a 3D project, copy
  `game/unity/Scripts/` into `Assets/NLCL/`, open any scene and press Play - `NlclSceneBuilder`
  builds the whole scene at runtime (primitives, colours, Rigidbody/Collider, scripts, hub channels).
* **Godot 4** (free, godotengine.org): `godot --path game/godot` or open `game/godot/project.godot`
  and press F5. Headless: `godot --headless --path game/godot --quit-after 300`.

Channels to other modules go through the NLCL HTTP hub (`python python/main.py`, port 8765)."""

    # ------------------------------------------------------------------ IR helpers
    def validate(self, entity: Entity) -> list[str]:
        out = []
        try:
            p = entity.props or {}
            if entity.kind == "spawner" and not p.get("spawn") and not any(
                    re.match(r"(spawns?|to|of)_", str(k)) and v is True for k, v in p.items()):
                out.append("spawner has no 'spawn' property (name of the entity to spawn)")
            if p.get("gravity") and str(p.get("shape", "")).lower() in ("none", "empty"):
                out.append("gravity on an invisible entity has no effect")
            for b in entity.behaviors or []:
                if isinstance(b, dict) and b.get("if") is not None:
                    try:
                        _check_expr(b["if"])
                    except Exception:
                        out.append(f"condition {b['if']!r} is not an expression; that behaviour will never fire "
                                   "(say e.g. 'when lives < 1, ...')")
        except Exception:
            pass
        return out

    # ------------------------------------------------------------------ analysis
    def _analyse(self, ctx: BuildContext) -> list[_Info]:
        infos: list[_Info] = []
        used_ids: set[str] = set()
        self._idmap: dict[str, str] = {}      # entity.ident / name / id -> our id
        for n, e in enumerate(ctx.entities):
            i = e.ident or ident(e.id) or f"entity_{n + 1}"
            base = i
            k = 2
            while i in used_ids:
                i = f"{base}_{k}"
                k += 1
            used_ids.add(i)
            for key in (e.ident, ident(e.name), e.id, ident(e.id)):
                if key:
                    self._idmap.setdefault(key, i)
        self._remote: dict[str, Entity] = {}
        for e in getattr(ctx.system, "entities", {}).values():
            if e.domain != ctx.domain and e.ident:
                self._remote.setdefault(e.ident, e)
        has_player = any(e.kind in ("player", "character") for e in ctx.entities)
        controls_given = False
        used_cls: set[str] = set()
        for e in ctx.entities:
            eid = self._idmap.get(e.ident) or self._idmap.get(e.id) or ident(e.id)
            props_in = dict(e.props or {})
            kind = e.kind if e.kind in LOOK else "object"
            shape, size, color = LOOK[kind]
            sp = str(props_in.get("shape", "")).lower().strip()
            shape = SHAPES.get(sp, shape)
            if shape == "none" and kind not in ("camera", "level") and sp not in SHAPES:
                shape = "cube"
            size = list(size) if shape != "none" else [1.0, 1.0, 1.0]
            sv = _vec(props_in.get("size")) if "size" in props_in else None
            if sv:
                size = [max(0.05, abs(x)) if x else size[j] for j, x in enumerate(sv)]
            for j, k in enumerate(("width", "height", "depth")):
                if k in props_in:
                    size[j] = max(0.05, abs(_num(props_in[k], size[j])))
            if "radius" in props_in:
                r = max(0.05, abs(_num(props_in["radius"], 0.5))) * 2
                size = [r, r if shape != "capsule" else max(r, size[1]), r]
            col = props_in.get("color", props_in.get("colour"))
            # parser output for "make it dark green": {"dark_green": True}
            for k in list(props_in):
                if props_in[k] is True and str(k).lower().replace("_", " ") in COLORS:
                    if not (isinstance(col, str) and col.strip()):
                        col = str(k)
                    props_in.pop(k)
            color = self._color(col) if isinstance(col, str) and col.strip() else color
            movable = bool(props_in.get("movable", False))
            if isinstance(props_in.get("controls"), bool):
                controls = props_in["controls"]
            else:
                controls = kind in ("player", "character") or (
                    movable and not has_player and kind not in AI_KINDS and not controls_given)
            controls_given = controls_given or controls
            auto = props_in.get("auto_move")
            auto_move = bool(auto) if isinstance(auto, bool) else kind in ("bullet", "projectile")
            g = props_in.get("gravity")
            gravity = (bool(g) if isinstance(g, bool) else kind in ("player",)) and shape != "none"
            s = props_in.get("solid")
            solid = (bool(s) if isinstance(s, bool) else kind in SOLID_KINDS) and shape != "none"
            if gravity and solid:
                solid = False
            # ---- fields of the generated class
            props: dict[str, Any] = {}
            for k, v in props_in.items():
                k2 = ident(str(k))
                if not k2 or str(k).startswith("_") or k2 in LAYOUT_PROPS or k2 == "colour":
                    continue
                if isinstance(v, dict) or v is None:
                    v = json.dumps(v) if isinstance(v, dict) else ""
                if isinstance(v, tuple):
                    v = list(v)
                if isinstance(v, float) and (v != v or abs(v) == float("inf")):
                    v = 0.0
                props[k2] = v
            # parser output such as "target_hero: True" means "target: hero"
            for k in list(props):
                for ref in REF_PROPS:
                    tail = k[len(ref) + 1:]
                    if k.startswith(ref + "_") and props[k] is True and tail in self._idmap:
                        props[ref] = self._idmap[tail]
                        del props[k]
                        break
            # "ghost follows bob" / "enemy chases hero" -> chase target (camera: follow)
            for k in list(props):
                m = re.fullmatch(r"(?:follows?|chases?|chasing|following|hunts?|targets?|attacks?)_(\w+)", k)
                if m and props[k] is True and m.group(1) in self._idmap:
                    ref = "follow" if kind == "camera" else "target"
                    props[ref] = self._idmap[m.group(1)]
                    del props[k]
            if kind == "spawner" and not props.get("spawn"):     # "set factory spawn to drone" -> to_drone
                for k in list(props):
                    m = re.fullmatch(r"(?:to|of|spawns?)_(\w+)", k)
                    if m and props[k] is True and m.group(1) in self._idmap:
                        props["spawn"] = self._idmap[m.group(1)]
                        del props[k]
                        break
            if kind != "camera" and isinstance(props.get("follow"), str) and props["follow"] \
                    and not props.get("target"):
                props["target"] = props.pop("follow")
            if props.get("visible") is False and any(
                    isinstance(b, dict) and any(isinstance(a, dict) and a.get("op") == "toggle" and
                                                a.get("prop") == "visible" for a in b.get("do") or [])
                    for b in e.behaviors or []):
                props["visible"] = True     # toggled flag defaulted to false by the parser
            props["color"] = color if shape != "none" else props.get("color", "")
            if not props["color"]:
                del props["color"]
            for k in REF_PROPS:
                if isinstance(props.get(k), str) and props[k]:
                    props[k] = self._idmap.get(ident(props[k]), ident(props[k]))
            cls = pascal(eid)
            if cls in CS_TYPES or cls.startswith("Nlcl") or cls in used_cls:
                cls = cls + "Entity"
            while cls in used_cls:
                cls += "X"
            used_cls.add(cls)
            gd = eid if eid not in ("main", "nlcl_world", "project", "icon") else eid + "_entity"
            info = _Info(e, eid, cls, gd, kind, shape, tuple(size), color if shape != "none" else "",
                         controls, auto_move, gravity, solid, props, [])
            parent = ctx.system.entities.get(e.parent) if e.parent and hasattr(ctx.system, "entities") else None
            if parent is not None and parent.domain == e.domain:
                info.parent = self._idmap.get(parent.ident) or self._idmap.get(parent.id, "")
            pv = _vec(props_in.get("position", props_in.get("pos"))) if (
                "position" in props_in or "pos" in props_in) else None
            xyz = [props_in[k] for k in ("x", "y", "z") if k in props_in]
            # x/y/z that are all 0 were created by the parser for "change x" etc., not a placement
            if xyz and (pv or any(_num(v, 0.0) != 0 for v in xyz)):
                pv = pv or [0.0, 0.0, 0.0]
                for j, k in enumerate(("x", "y", "z")):
                    if k in props_in:
                        pv[j] = _num(props_in[k], 0.0)
            if pv:
                info.rel = pv
                info.explicit_pos = True
            infos.append(info)
        by_id = {i.id: i for i in infos}
        for i in infos:
            if i.parent == i.id or i.parent not in by_id:
                i.parent = ""
        # channel fields become props of the receiving entity (if missing)
        for c in ctx.channels:
            dst = by_id.get(self._idmap.get(c.dst.ident) or self._idmap.get(c.dst.id, ""))
            if dst and c.dst.domain == ctx.domain:
                for f in c.fields:
                    f2 = ident(f)
                    if f2 and f2 not in dst.props and f2 not in ("x", "y", "z"):
                        sv = c.src.props.get(f, 0)
                        dst.props[f2] = sv if isinstance(sv, (int, float, bool, str)) else 0
        for i in infos:
            i.behaviors = self._norm_behaviors(i, by_id)
        self._by_id = by_id
        # entities used as spawn templates; bullets/projectiles exist only as templates
        self._templates: dict[str, bool] = {}
        for i in infos:
            refs = [i.props.get("spawn")] if isinstance(i.props.get("spawn"), str) else []
            refs += [str(a["args"][0]).strip("'\"") for b in i.behaviors for a in b["do"]
                     if a["op"] == "call" and a["name"] in ("spawn", "fire", "shoot") and a.get("args")]
            for r in refs:
                if r in by_id:
                    self._templates[r] = by_id[r].kind in ("bullet", "projectile") and r != i.id
        self._layout(infos)
        return infos

    def _color(self, c: str) -> str:
        c = str(c).strip().lower()
        if re.fullmatch(r"#?[0-9a-f]{6}", c):
            return c if c.startswith("#") else "#" + c
        if re.fullmatch(r"#[0-9a-f]{3}", c):
            return "#" + "".join(ch * 2 for ch in c[1:])
        return COLORS.get(c, COLORS.get(c.replace("_", " "), "#94a3b8"))

    def _names(self, expr: Any) -> list[str]:
        try:
            return [v for k, v in expr_tokens(validate_expr(expr)) if k == "id" and v not in ("true", "false")]
        except Exception:
            return []

    @staticmethod
    def _boolify(info: _Info, expr: Any) -> None:
        """A numeric 0/1 prop used as a truth value ("when label", "x == true", "!x", "x && y")
        becomes a bool prop, so the C# / GDScript comparison type-checks."""
        try:
            toks = expr_tokens(validate_expr(expr))
        except Exception:
            return
        boolish = ("&&", "||", "!")
        for i, (k, v) in enumerate(toks):
            if k != "id" or v in ("true", "false") or v not in info.props:
                continue
            cur = info.props[v]
            if isinstance(cur, bool) or not isinstance(cur, (int, float)) or cur not in (0, 1):
                continue
            prev = toks[i - 1] if i > 0 else ("", "")
            nxt = toks[i + 1] if i + 1 < len(toks) else ("", "")
            nxt2 = toks[i + 2] if i + 2 < len(toks) else ("", "")
            prev2 = toks[i - 2] if i > 1 else ("", "")
            cmp_bool = (nxt[1] in ("==", "!=") and nxt2[1] in ("true", "false")) or (
                prev[1] in ("==", "!=") and prev2[1] in ("true", "false"))
            operand = prev[1] in boolish + ("", "(") and nxt[1] in ("&&", "||", "", ")")
            if cmp_bool or operand:
                info.props[v] = bool(cur)

    def _norm_behaviors(self, info: _Info, by_id: dict) -> list[dict]:
        out = []
        ents = set(by_id) | set(self._remote)
        for b in info.e.behaviors or []:
            if not isinstance(b, dict) or not isinstance(b.get("on"), str):
                continue
            nb = {"on": b["on"].strip(), "if": b.get("if"), "do": []}
            if b.get("with"):
                nb["with"] = b["with"]
            m = re.fullmatch(r"message_from_(\w+)", str(nb["if"] or "").strip())
            if nb["on"] == "update" and m:           # parser output for "on message from X"
                nb["on"], nb["if"] = "message:" + m.group(1), None
                if info.props.get(m.group(0)) == 0:
                    info.props.pop(m.group(0), None)
            if nb["if"] is not None:
                try:
                    nb["if"] = _check_expr(nb["if"])
                except Exception:
                    nb["if"] = "false"
                self._boolify(info, nb["if"])
            for a in b.get("do") or []:
                if not isinstance(a, dict) or "op" not in a:
                    continue
                a = dict(a)
                op = a["op"]
                if op in ("set", "add", "toggle"):
                    p = str(a.get("prop") or "")
                    p = ".".join(ident(x) for x in p.split(".", 1)) if "." in p else ident(p)
                    if not p:
                        continue
                    if "." not in p and p not in ("x", "y", "z") and (
                            p not in info.props or info.props[p] in (0, False, "")):
                        # "hero_score" (parser flattening of hero.score) -> hero.score
                        for oid in sorted(by_id, key=len, reverse=True):
                            rest = p[len(oid) + 1:]
                            if oid != info.id and p.startswith(oid + "_") and (
                                    rest in by_id[oid].props or rest in ("x", "y", "z")):
                                info.props.pop(p, None)
                                p = oid + "." + rest
                                break
                    a["prop"] = p
                    if p not in info.props and "." not in p and p not in ("x", "y", "z"):
                        if op == "toggle":
                            info.props[p] = False
                        else:
                            v = str(a.get("value", "0")).strip()
                            info.props[p] = (float(v) * 0 if "." in v else 0) if NUM_RE.fullmatch(v) else (
                                "" if v[:1] in "'\"" else False if v in ("true", "false") else 0)
                    # bare word assigned to a text property: it is a literal (set color to green)
                    v = a.get("value")
                    is_word = isinstance(v, str) and re.fullmatch(r"[A-Za-z_]\w*", v.strip()) is not None
                    if op == "set" and is_word and v.strip() not in ("true", "false", "x", "y", "z") and (
                            (isinstance(info.props.get(p), str) and v not in info.props and v not in ents) or
                            (p == "color" and v.strip().lower() in COLORS)):
                        if p == "color" and info.props.get(v.strip()) == 0 and v.strip() in (info.e.props or {}):
                            info.props.pop(v.strip(), None)       # parser side effect: "red: 0"
                        a["value"] = "'" + (self._color(v) if p == "color" else v.strip()) + "'"
                    if op != "toggle":
                        try:
                            a["value"] = _check_expr(a.get("value", 0))
                        except Exception:
                            a["value"] = "0"
                elif op == "move":
                    for k in ("x", "y", "z"):
                        try:
                            a[k] = _check_expr(a.get(k, 0))
                        except Exception:
                            a[k] = "0"
                elif op == "call":
                    args = []
                    for x in a.get("args") or []:
                        sx = str(x).strip()
                        if re.fullmatch(r"[A-Za-z_]\w*", sx) and ident(sx) in ents and sx not in info.props:
                            sx = "'" + self._idmap.get(ident(sx), ident(sx)) + "'"
                        try:
                            args.append(_check_expr(sx))
                        except Exception:
                            args.append("'" + re.sub(r"['\"\\\n]", "", sx) + "'")
                    a["name"] = ident(str(a.get("name") or "action")) or "action"
                    if a["name"] in ("spawn", "fire", "shoot") and not args and not info.props.get("spawn") \
                            and info.kind not in ("bullet", "projectile"):
                        ammo = [o for o in by_id if by_id[o].kind in ("bullet", "projectile") and o != info.id]
                        if len(ammo) == 1:
                            args = ["'" + ammo[0] + "'"]      # "fire" / "spawn shell" with one bullet kind
                    if (a["name"] in ("spawn", "fire", "shoot") and not args and not info.props.get("spawn")
                            and info.kind != "spawner" and not nb["on"].startswith(("timer:", "update"))):
                        args = ["'" + info.id + "'"]      # "spawn ball" said on ball: a copy of itself
                    a["args"] = args
                elif op == "send":
                    to = a.get("to")
                    a["to"] = self._idmap.get(ident(str(to)), ident(str(to))) if to else ""
                    a["fields"] = [ident(str(f)) for f in (a.get("fields") or []) if ident(str(f))]
                elif op == "log":
                    a["text"] = str(a.get("text", ""))
                elif op != "destroy":
                    a = {"op": "noop", "text": describe_action(a) if op in ("set",) else str(op)}
                nb["do"].append(a)
            trig = nb["on"]
            if trig.startswith("timer:"):     # 0 ms (or junk) would fire forever: once per frame
                nb["on"] = f"timer:{max(16, int(_num(trig.split(':', 1)[1], 1000)))}"
            if trig.startswith("change:"):
                p = ident(trig.split(":", 1)[1])
                if p and p not in info.props and p not in ("x", "y", "z"):
                    info.props[p] = 0
                nb["on"] = "change:" + (p or "x")
            out.append(nb)
        # names used in expressions but never defined become int fields (0)
        for b in out:
            exprs = [b["if"]] if b["if"] else []
            for a in b["do"]:
                exprs += [a.get(k) for k in ("value", "x", "y", "z") if a.get(k) is not None]
                exprs += a.get("args", []) if a["op"] == "call" else []
            for ex in exprs:
                for n in self._names(ex):
                    if "." not in n and n not in info.props and n not in ("x", "y", "z"):
                        info.props[n] = 0
        return out

    def _layout(self, infos: list[_Info]) -> None:
        roots = [i for i in infos if not i.parent]
        actor_x, plat_k, wall_k = 0.0, 0, 0
        for i in roots:
            if i.explicit_pos:
                i.pos = list(i.rel)
                continue
            w, h, _ = i.size
            if i.kind in ("camera", "level") or not i.visual:
                i.pos = [0.0, 0.0, 0.0]
            elif i.kind == "platform":
                i.pos = [4.0 + plat_k * 8.0, 2.75 + plat_k * 1.5, 0.0]
                plat_k += 1
            elif i.kind == "wall":
                i.pos = [None, h / 2, 0.0]   # placed after the actors are known
                wall_k += 1
            else:
                i.pos = [actor_x + w / 2, h / 2 + 0.05, 0.0]
                actor_x += max(w, 1.0) + 1.5
            i.rel = list(i.pos)
        walls = [i for i in roots if i.pos and i.pos[0] is None]
        for n, i in enumerate(walls):
            i.pos[0] = -3.0 if n % 2 == 0 else actor_x + 2.0 + n * 0.5
            i.rel = list(i.pos)
        by_id = {i.id: i for i in infos}
        done = {i.id for i in roots}
        for _ in range(len(infos)):          # children (relative to parent), any depth
            for i in infos:
                if i.id in done or i.parent not in done:
                    continue
                par = by_id[i.parent]
                if not i.explicit_pos:
                    sibs = [s for s in infos if s.parent == i.parent and not s.explicit_pos]
                    k = sibs.index(i)
                    i.rel = [(k - (len(sibs) - 1) / 2) * 1.2, par.size[1] / 2 + i.size[1] / 2 + 0.05, 0.0]
                i.pos = [par.pos[j] + i.rel[j] for j in range(3)]
                done.add(i.id)
        for i in infos:
            if i.id not in done:
                i.parent = ""
                i.pos = list(i.rel)
        # camera / bounds
        vis = [i for i in infos if i.visual]
        if vis:
            l = min(i.pos[0] - i.size[0] / 2 for i in vis)
            r = max(i.pos[0] + i.size[0] / 2 for i in vis)
            b = min(min(i.pos[1] - i.size[1] / 2 for i in vis), 0.0)
            t = max(i.pos[1] + i.size[1] / 2 for i in vis)
        else:
            l, r, b, t = -5.0, 5.0, 0.0, 5.0
        self._bounds = (l, r, b, t)
        self._ground = None
        if any(i.gravity for i in infos):
            self._ground = ((l + r) / 2, -0.25, (r - l) + 12, 0.5)
        for i in infos:
            if i.kind == "camera" and not i.explicit_pos:
                i.pos = [(l + r) / 2, (b + t) / 2 + 1, 0.0]
                i.rel = list(i.pos)

    def _cam_dist(self) -> float:
        l, r, b, t = self._bounds
        return round(max(10.0, (r - l) * 0.9, (t - b) * 1.6) + 2, 1)

    # ------------------------------------------------------------------ expressions
    def _split(self, info: _Info, n: str):
        """Classify a name: ('local', prop) | ('pos', axis) | ('ent', id, prop) | ('remote', src, prop)."""
        if "." in n:
            obj, prop = n.split(".", 1)
            prop = ident(prop.split(".")[0]) or "value"
            if obj in ("self", "this", "it", info.id):
                return self._split(info, prop)
            oid = self._idmap.get(obj)
            if oid and oid in self._by_id:
                return ("ent", oid, prop)
            return ("remote", obj, prop)
        if n in ("x", "y", "z"):
            return ("pos", n)
        return ("local", n)

    def _ent_ptype(self, oid: str, prop: str) -> str:
        if prop in ("x", "y", "z"):
            return "float"
        o = self._by_id.get(oid)
        return _ptype(o.props.get(prop, 0)) if o else "float"

    def _remote_ptype(self, src: str, prop: str) -> str:
        e = self._remote.get(src)
        v = e.props.get(prop, 0) if e else 0
        t = _ptype(v)
        return "float" if t in ("int", "float", "nlist", "slist") else t

    def _infer(self, info: _Info, expr: Any, lang: str) -> str:
        """Static type of an expression in the target language (int/float/double/bool/string/?)."""
        try:
            toks = expr_tokens(validate_expr(expr))
        except Exception:
            return "?"
        if any(v in ("<", ">", "<=", ">=", "==", "!=", "&&", "||", "!") for k, v in toks if k == "op"):
            return "bool"
        types = []
        for k, v in toks:
            if k == "num":
                types.append("double" if "." in v else "int")
            elif k == "str":
                types.append("string")
            elif k == "id":
                if v in ("true", "false"):
                    types.append("bool")
                    continue
                c = self._split(info, v)
                if c[0] == "local":
                    types.append(_ptype(info.props.get(c[1], 0)))
                elif c[0] == "pos":
                    types.append("float")
                elif lang == "gd":
                    types.append("?")
                elif c[0] == "ent":
                    t = self._ent_ptype(c[1], c[2])
                    types.append("float" if t in ("int", "float") else t)
                else:
                    types.append(self._remote_ptype(c[1], c[2]))
        if not types or "?" in types or "nlist" in types or "slist" in types:
            return "?"
        if "string" in types:
            return "string" if len(types) == 1 or all(t == "string" for t in types) or lang != "gd" else "?"
        if "bool" in types:
            return "bool" if len(types) == 1 else "?"
        if "double" in types:
            return "double" if lang == "cs" else "float"
        if "float" in types:
            return "float"
        if any(v == "/" for k, v in toks if k == "op") and lang == "js":
            return "float"
        return "int"

    def _cs_name(self, info: _Info):
        def f(n: str) -> str:
            c = self._split(info, n)
            if c[0] == "local":
                return self._cs_field(c[1])
            if c[0] == "pos":
                return f"transform.position.{c[1]}"
            if c[0] == "ent":
                t = self._ent_ptype(c[1], c[2])
                conv = {"bool": "ToBool", "string": "ToStr"}.get(t, "ToFloat")
                return f'{conv}(global::NlclWorld.Prop("{c[1]}", "{c[2]}"))'
            t = self._remote_ptype(c[1], c[2])
            if t == "bool":
                return f'global::NlclHub.GetBool("{c[1]}", "{c[2]}", false)'
            if t == "string":
                return f'global::NlclHub.GetString("{c[1]}", "{c[2]}", "")'
            return f'global::NlclHub.GetFloat("{c[1]}", "{c[2]}", 0f)'
        return f

    def _gd_name(self, info: _Info):
        def f(n: str) -> str:
            c = self._split(info, n)
            if c[0] == "local":
                return self._gd_field(c[1])
            if c[0] == "pos":
                return f"position.{c[1]}"
            if c[0] == "ent":
                d = self._gd_default(self._ent_ptype(c[1], c[2]))
                return f'nlcl_world.get_prop("{c[1]}", "{c[2]}", {d})'
            d = self._gd_default(self._remote_ptype(c[1], c[2]))
            return f'nlcl_world.get_remote("{c[1]}", "{c[2]}", {d})'
        return f

    def _js_name(self, info: _Info):
        def f(n: str) -> str:
            c = self._split(info, n)
            if c[0] == "local":
                return f"o.p[{json.dumps(c[1])}]"
            if c[0] == "pos":
                return f"o.{c[1]}"
            if c[0] == "ent":
                return f"w.get({json.dumps(c[1])}, {json.dumps(c[2])})"
            t = self._remote_ptype(c[1], c[2])
            d = {"bool": "false", "string": '""'}.get(t, "0")
            return f"w.remote({json.dumps(c[1])}, {json.dumps(c[2])}, {d})"
        return f

    def _x(self, info: _Info, expr: Any, lang: str) -> str:
        fn = {"cs": self._cs_name, "gd": self._gd_name, "js": self._js_name}[lang](info)
        target = {"cs": "csharp", "gd": "gdscript", "js": "js"}[lang]
        try:
            return self.expr(expr, target, fn)
        except Exception:
            return {"cs": "0", "gd": "0", "js": "0"}[lang]

    @staticmethod
    def _gd_default(t: str) -> str:
        return {"bool": "false", "string": '""', "float": "0.0", "int": "0"}.get(t, "0")

    @staticmethod
    def _cs_field(p: str) -> str:
        return p + "_" if p in CS_KEYWORDS or p in CS_MEMBERS or p.startswith("nlcl") else p

    @staticmethod
    def _gd_field(p: str) -> str:
        return p + "_" if (p in GD_RESERVED or p.startswith("nlcl_") or p.startswith("_")
                           or re.fullmatch(r"(now|fire|_?edge|_?timer)\d+", p)) else p

    # coercions to a field type ----------------------------------------------
    def _cs_coerce(self, info: _Info, expr: Any, target: str) -> str:
        code = self._x(info, expr, "cs")
        s = str(expr).strip()
        t = self._infer(info, expr, "cs")
        if target == "float":
            if NUM_RE.fullmatch(s):
                return s + "f"
            if t in ("int", "float"):
                return code
            if t == "double":
                return f"(float)({code})"
            return f"ToFloat({code})"
        if target == "int":
            if t == "int":
                return code
            if t in ("float", "double"):
                return f"(int)({code})"
            return f"(int)ToFloat({code})"
        if target == "bool":
            return code if t == "bool" else f"ToBool({code})"
        if target == "string":
            return code if t == "string" else f"ToStr({code})"
        return code

    def _gd_coerce(self, info: _Info, expr: Any, target: str) -> str:
        code = self._x(info, expr, "gd")
        t = self._infer(info, expr, "gd")
        if target == "float":
            return code if t in ("int", "float") else f"float({code})"
        if target == "int":
            return code if t == "int" else f"int({code})"
        if target == "bool":
            return code if t == "bool" else f"nlcl_world.to_bool({code})"
        if target == "string":
            return code if t == "string" else f"str({code})"
        return code

    def _cs_cond(self, info: _Info, expr: Any) -> str:
        return self._cs_coerce(info, expr, "bool")

    def _gd_cond(self, info: _Info, expr: Any) -> str:
        return self._gd_coerce(info, expr, "bool")

    def _log_parts(self, info: _Info, text: str, lang: str) -> str:
        """Log text with {prop} placeholders as a target-language string expression."""
        parts: list[str] = []
        pos = 0
        q = {"cs": self._cs_str, "gd": self._cs_str, "js": json.dumps}[lang]
        for m in re.finditer(r"\{([A-Za-z_][\w.]*)\}", text):
            n = m.group(1)
            c = self._split(info, n)
            if c[0] == "local" and c[1] not in info.props:
                continue           # unknown placeholder: keep the text as written
            if m.start() > pos:
                parts.append(q(text[pos:m.start()]))
            code = self._x(info, n, lang)
            parts.append({"cs": f"ToStr({code})", "gd": f"str({code})", "js": f"({code})"}[lang])
            pos = m.end()
        if pos < len(text) or not parts:
            parts.append(q(text[pos:]))
        if lang == "js" and not parts[0].startswith('"'):
            parts.insert(0, '""')
        return " + ".join(parts)

    @staticmethod
    def _cs_str(s: str) -> str:
        return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace(
            "\r", "").replace("\t", "\\t") + '"'

    # ------------------------------------------------------------------ input triggers
    def _input_kind(self, trig: str):
        raw = trig.split(":", 1)[1] if ":" in trig else trig
        rid = ident(raw)
        if rid in self._idmap and self._idmap[rid] in self._by_id:
            return ("click", self._idmap[rid])
        if rid in self._remote:
            return ("remote", rid)
        k = _key(raw)
        if k == "mouse":
            return ("click", "mouse")
        return ("key", k)

    def _is_jump(self, info: _Info, a: dict) -> bool:
        return (info.gravity and str(a.get("x")) in ("0", "0.0") and str(a.get("z")) in ("0", "0.0")
                and NUM_RE.fullmatch(str(a.get("y"))) is not None and _num(a.get("y"), 0) > 0)

    # ================================================================== Unity C#
    def _cs_type(self, v: Any) -> str:
        return {"bool": "bool", "int": "int", "float": "float", "nlist": "float[]",
                "slist": "string[]", "string": "string"}[_ptype(v)]

    def _cs_lit(self, v: Any) -> str:
        t = _ptype(v)
        if t == "nlist":
            return "new float[] {" + ", ".join(_f(x) + "f" for x in v) + "}"
        if t == "slist":
            return "new string[] {" + ", ".join(self._cs_str(str(x)) for x in v) + "}"
        if t == "float":
            return _f(v) + "f"
        if t == "string":
            return self._cs_str(v)
        return self.literal(v, "csharp")

    def _cs_actions(self, info: _Info, acts: list[dict], ind: str) -> list[str]:
        L = []
        for a in acts:
            op = a["op"]
            try:
                L += [ind + x for x in self._cs_action(info, a)]
            except Exception as ex:  # never break generation
                L.append(f"{ind}// could not translate {op}: {ex}")
        return L

    def _cs_action(self, info: _Info, a: dict) -> list[str]:
        op = a["op"]
        if op in ("set", "add", "toggle"):
            p = a["prop"]
            c = self._split(info, p)
            if c[0] == "pos":
                ax = c[1].upper()
                if op == "toggle":
                    return [f"// toggle {p}: not meaningful for a position"]
                v = self._cs_coerce(info, a["value"], "float")
                return [f"Set{ax}({v});" if op == "set" else f"Set{ax}(transform.position.{c[1]} + {v});"]
            if c[0] == "remote":
                return [f"// {describe_action(a)}: '{c[1]}' lives in another module - use a channel (send)"]
            if c[0] == "ent":
                o = self._by_id[c[1]]
                if c[2] in ("x", "y", "z"):
                    v = self._cs_coerce(info, a.get("value", 0), "float")
                    return [f'{{ var t = global::NlclWorld.Get<global::NlclEntity>("{c[1]}"); '
                            f'if (t != null) {{ var q = t.transform.position; q.{c[2]} = '
                            f'{"q." + c[2] + " + " if op == "add" else ""}{v}; t.transform.position = q; }} }}']
                if c[2] not in o.props:
                    return [f"// {describe_action(a)}: {o.id} has no property '{c[2]}'"]
                tgt = f"t.{self._cs_field(c[2])}"
                body = self._cs_assign(info, tgt, o.props[c[2]], a)
                return [f'{{ var t = global::NlclWorld.Get<{o.cls}>("{o.id}"); if (t != null) {{ {body} }} }}']
            return [self._cs_assign(info, self._cs_field(p), info.props.get(p, 0), a)]
        if op == "destroy":
            return ["Destroy(gameObject);", "return;"]
        if op == "log":
            return [f"Log({self._log_parts(info, a['text'], 'cs')});"]
        if op == "move":
            if self._is_jump(info, a):
                return ["Jump();"]
            xs = [self._cs_coerce(info, a.get(k, 0), "float") for k in ("x", "y", "z")]
            return [f"transform.position += new Vector3({', '.join(xs)});"]
        if op == "send":
            f = "new string[] {" + ", ".join(self._cs_str(x) for x in a["fields"]) + "}" if a["fields"] else "null"
            return [f"global::NlclHub.Send(this, {self._cs_str(a['to'])}, {f});"]
        if op == "call":
            n = a["name"]
            args = a.get("args", [])
            if n == "jump":
                return ["Jump();"]
            if n in ("spawn", "fire", "shoot"):
                arg = self._cs_coerce(info, args[0], "string") if args else (
                    self._cs_field("spawn") if "spawn" in info.props else '""')
                return [f"Spawn({arg});"]
            if n in ("reset", "respawn", "restart"):
                return ["ResetEntity();"]
            return [f"Do{pascal(n)}({', '.join(self._x(info, x, 'cs') for x in args)});"]
        return [f"// {a.get('text', op)}"]

    def _cs_assign(self, info: _Info, tgt: str, cur: Any, a: dict) -> str:
        t = {"int": "int", "float": "float", "bool": "bool", "string": "string"}.get(_ptype(cur))
        op = a["op"]
        if op == "toggle":
            if t == "bool":
                return f"{tgt} = !{tgt};"
            if t == "int":
                return f"{tgt} = {tgt} == 0 ? 1 : 0;"
            if t == "float":
                return f"{tgt} = {tgt} == 0f ? 1f : 0f;"
            return f"// toggle {a['prop']}: not a number or flag"
        if t is None:
            return f"// {describe_action(a)}: list properties are set in the inspector"
        v = self._cs_coerce(info, a["value"], t)
        if op == "add":
            if t == "bool":
                return f"{tgt} = {v};"
            return f"{tgt} += {v};"
        return f"{tgt} = {v};"

    def _cs_entity(self, ctx: BuildContext, info: _Info) -> str:
        e = info.e
        L = ["// " + f"{e.name} ({info.kind}) - generated by NLCL from {ctx.project}; edit freely.",
             "using UnityEngine;", "", "namespace NlclGame", "{",
             f"public class {info.cls} : global::NlclEntity", "{"]
        for k, v in info.props.items():
            L.append(f"    public {self._cs_type(v)} {self._cs_field(k)} = {self._cs_lit(v)};")
        groups: dict[str, list] = {}
        for b in info.behaviors:
            groups.setdefault(b["on"].split(":")[0], []).append(b)
        timers = groups.get("timer", [])
        upd = groups.get("update", [])
        changes = groups.get("change", [])
        inputs = groups.get("input", [])
        spawner = bool(info.props.get("spawn")) and _num(info.props.get("spawn_interval_ms"), 0) > 0
        priv = []
        for n, _ in enumerate(timers):
            priv.append(f"    float _timer{n};")
        for n, b in enumerate(upd):
            if b["if"]:
                priv.append(f"    bool _edge{n};")
        for b in changes:
            p = b["on"].split(":", 1)[1]
            t = "float" if p in ("x", "y", "z") else self._cs_type(info.props.get(p, 0))
            priv.append(f"    {t} _prev_{p};")
        if spawner:
            priv += ["    float _spawnTimer;", "    int _spawnCount;"]
        if priv:
            L += [""] + sorted(set(priv), key=priv.index)
        # Start
        L += ["", "    void Start()", "    {"]
        for b in changes:
            p = b["on"].split(":", 1)[1]
            src = f"transform.position.{p}" if p in ("x", "y", "z") else self._cs_field(p)
            L.append(f"        _prev_{p} = {src};")
        for b in groups.get("start", []):
            L += self._cs_block(info, b, "        ")
        L += ["    }", ""]
        # Update
        L += ["    void Update()", "    {"]
        for b in inputs:
            k = self._input_kind(b["on"])
            if k[0] == "key":
                code = UNITY_KEYS.get(k[1])
                if not code and re.fullmatch(r"[a-z]", k[1]):
                    code = k[1].upper()
                elif not code and re.fullmatch(r"[0-9]", k[1]):
                    code = "Alpha" + k[1]
                elif not code and re.fullmatch(r"f([1-9]|1[0-5])", k[1]):
                    code = k[1].upper()
                test = (f"global::NlclInput.Down(KeyCode.{code})" if code
                        else f"global::NlclInput.DownNamed({self._cs_str(k[1])})")
                L.append(f"        if ({test})  // {k[1]} key")
            elif k[0] == "click":
                test = ("global::NlclInput.Down(KeyCode.Mouse0)" if k[1] == "mouse"
                        else f'global::NlclInput.Clicked() == "{k[1]}"')
                L.append(f"        if ({test})  // mouse click")
            else:
                continue          # remote inputs: NlclRemoteInput
            L += ["        {"] + self._cs_block(info, b, "            ") + ["        }"]
        for n, b in enumerate(timers):
            ms = b["on"].split(":", 1)[1]
            L += [f"        _timer{n} += Time.deltaTime * 1000f;",
                  f"        if (_timer{n} >= {ms}f)  // every {ms} ms", "        {",
                  f"            _timer{n} -= {ms}f;"]
            L += self._cs_block(info, b, "            ") + ["        }"]
        for n, b in enumerate(upd):
            if b["if"]:
                L += ["        {", f"            bool now = {self._cs_cond(info, b['if'])};",
                      f"            bool fire = now && !_edge{n};  // fires when the condition becomes true",
                      f"            _edge{n} = now;", "            if (fire)", "            {"]
                L += self._cs_actions(info, b["do"], "                ") + ["            }", "        }"]
            else:
                L += self._cs_actions(info, b["do"], "        ")
        if spawner:
            L += ["        _spawnTimer += Time.deltaTime * 1000f;",
                  f"        if (_spawnTimer >= {self._cs_field('spawn_interval_ms')} && _spawnCount < "
                  f"{self._cs_field('max_spawn') if 'max_spawn' in info.props else '10'})",
                  "        {", "            _spawnTimer = 0f;", "            _spawnCount++;",
                  f"            Spawn({self._cs_field('spawn')});", "        }"]
        for b in changes:
            p = b["on"].split(":", 1)[1]
            src = f"transform.position.{p}" if p in ("x", "y", "z") else self._cs_field(p)
            t = "float" if p in ("x", "y", "z") else self._cs_type(info.props.get(p, 0))
            diff = (f"!Mathf.Approximately({src}, _prev_{p})" if t == "float" else
                    f"!ReferenceEquals({src}, _prev_{p})" if t.endswith("[]") else f"{src} != _prev_{p}")
            L += [f"        if ({diff})  // {p} changed", "        {", f"            _prev_{p} = {src};"]
            L += self._cs_block(info, b, "            ") + ["        }"]
        L += ["    }"]
        for name in ("Update", "Start"):      # drop empty Unity callbacks
            k = L.index(f"    void {name}()")
            if L[k + 2] == "    }":
                del L[k:k + 4]
        # collision
        if "collision" in groups:
            L += ["", "    public override void NlclCollision(GameObject other)", "    {"]
            L += ["        NlclEntity nlclOther = other != null ? other.GetComponentInParent<NlclEntity>() : null;"]
            for b in groups["collision"]:
                w = self._with_key(b)
                if w:   # "when hero touches gold": only that entity (id) or kind counts
                    L += [f'        if (nlclOther != null && (nlclOther.nlclId == "{w}" || nlclOther.nlclKind == "{w}"))',
                          "        {"]
                    L += self._cs_block(info, b, "            ") + ["        }"]
                else:
                    L += self._cs_block(info, b, "        ")
            L += ["    }"]
        # messages
        if "message" in groups:
            L += ["", "    public override void NlclMessage(string src, bool isDestination)", "    {"]
            for b in groups["message"]:
                s = ident(b["on"].split(":", 1)[1])
                s = self._idmap.get(s, s)
                L.append("        if (isDestination)" if s in ("any", "") else f'        if (src == "{s}")')
                L += ["        {"] + self._cs_block(info, b, "            ") + ["        }"]
            L += ["    }"]
        remotes = [b for b in inputs if self._input_kind(b["on"])[0] == "remote"]
        if remotes:
            L += ["", "    public override void NlclRemoteInput(string src)", "    {"]
            for b in remotes:
                L += [f'        if (src == "{self._input_kind(b["on"])[1]}")  // remote input', "        {"]
                L += self._cs_block(info, b, "            ") + ["        }"]
            L += ["    }"]
        # custom calls
        calls = sorted({a["name"] for b in info.behaviors for a in b["do"] if a["op"] == "call"} -
                       {"jump", "spawn", "fire", "shoot", "reset", "respawn", "restart"})
        for c in calls:
            L += ["", f"    void Do{pascal(c)}(params object[] args)", "    {",
                  f'        Log("{c} called");  // TODO: implement', "    }"]
        # property access for the runtime (channels, built-in movement, remote reads)
        L += ["", "    public override object NlclGet(string field)", "    {", "        switch (field)", "        {"]
        for k in info.props:
            L.append(f'            case "{k}": return {self._cs_field(k)};')
        L += ['            case "x": return transform.position.x;',
              '            case "y": return transform.position.y;',
              '            case "z": return transform.position.z;',
              "        }", "        return null;", "    }",
              "", "    public override void NlclSet(string field, object value)", "    {",
              "        switch (field)", "        {"]
        for k, v in info.props.items():
            t = _ptype(v)
            f = self._cs_field(k)
            conv = {"int": "(int)ToFloat(value)", "float": "ToFloat(value)", "bool": "ToBool(value)",
                    "string": "ToStr(value)"}.get(t)
            if conv:
                L.append(f'            case "{k}": {f} = {conv}; break;')
            else:
                L.append(f'            case "{k}": if (value is {self._cs_type(v)}) {f} = ({self._cs_type(v)})value; break;')
        L += ['            case "x": SetX(ToFloat(value)); break;',
              '            case "y": SetY(ToFloat(value)); break;',
              '            case "z": SetZ(ToFloat(value)); break;',
              "        }", "    }", "}", "}", ""]
        return "\n".join(L)

    def _with_key(self, b: dict) -> str | None:
        """Collision partner of a rule ("with": entity ident or kind), mapped to the runtime id."""
        w = b.get("with")
        if not w:
            return None
        w = ident(str(w))
        return self._idmap.get(w, w)

    def _cs_block(self, info: _Info, b: dict, ind: str) -> list[str]:
        if b.get("if") and not b["on"].startswith("update"):
            return [f"{ind}if ({self._cs_cond(info, b['if'])})", ind + "{"] + \
                self._cs_actions(info, b["do"], ind + "    ") + [ind + "}"]
        return self._cs_actions(info, b["do"], ind)

    def _cs_builder(self, ctx: BuildContext, infos: list[_Info]) -> str:
        l, r, b, t = self._bounds
        dist = self._cam_dist()
        L = [f"// Scene bootstrap for {ctx.project} - generated by NLCL; edit freely.",
             "// Runs automatically when you press Play in ANY scene (no manual setup needed).",
             "using UnityEngine;", "", "namespace NlclGame", "{", "public static class NlclSceneBuilder", "{",
             "    public const string HubUrl = \"http://localhost:8765\";", "",
             "    [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.AfterSceneLoad)]",
             "    static void Boot()", "    {",
             '        if (GameObject.Find("NLCL World") != null) return;',
             '        GameObject world = new GameObject("NLCL World");',
             "        global::NlclHub hub = world.AddComponent<global::NlclHub>();",
             "        Lighting();"]
        if self._ground:
            gx, gy, gw, gh = self._ground
            L.append(f'        Make("ground", 1, new Vector3({_f(gx)}f, {_f(gy)}f, 0f), new Vector3({_f(gw)}f, '
                     f'{_f(gh)}f, 4f), "#3f4654", null, 1);')
        cam = next((i for i in infos if i.kind == "camera"), None)
        for i in infos:
            shape = {"none": 0, "cube": 1, "sphere": 2, "capsule": 3, "cylinder": 4}[i.shape]
            body = 3 if i.kind == "camera" else 2 if i.gravity else 1 if i.solid else 0
            pos = list(i.pos)
            if i.kind == "camera":
                pos[2] = pos[2] - dist if not i.explicit_pos else pos[2]
            v = lambda xs: "new Vector3(" + ", ".join(_f(x) + "f" for x in xs) + ")"
            par = f"go_{i.parent}" if i.parent else "null"
            L += [f"        // {i.e.name}: {i.kind}",
                  f'        GameObject go_{i.id} = Make("{i.id}", {shape}, {v(pos)}, {v(i.size)}, '
                  f'"{i.color}", {par}, {body});',
                  f"        {'global::NlclWorld.KeepTemplate(' if i.id in self._templates else ''}"
                  f"Setup(go_{i.id}.AddComponent<{i.cls}>(), \"{i.id}\", \"{i.kind}\", "
                  f"{'true' if i.controls else 'false'}, {'true' if i.auto_move else 'false'})"
                  + (f", {str(self._templates[i.id]).lower()});  // spawn template"
                     if i.id in self._templates else ";")]
        if not cam:
            L += ["        Camera cam = Camera.main;",
                  '        if (cam == null) { GameObject c = new GameObject("Main Camera"); c.tag = "MainCamera"; '
                  "cam = c.AddComponent<Camera>(); }",
                  f"        cam.transform.position = new Vector3({_f((l + r) / 2)}f, {_f((b + t) / 2 + 1)}f, "
                  f"-{_f(dist)}f);",
                  "        cam.transform.rotation = Quaternion.identity;"]
        L.append("        cam_bg();")
        for c in ctx.channels:
            if c.protocol in ("spi", "i2c", "gpio", "wire"):
                continue
            sl = c.src.domain == ctx.domain and c.src.id in [i.e.id for i in infos]
            dl = c.dst.domain == ctx.domain and c.dst.id in [i.e.id for i in infos]
            src = self._idmap.get(c.src.ident, c.src.ident) if sl else c.src.ident
            dst = self._idmap.get(c.dst.ident, c.dst.ident) if dl else c.dst.ident
            path = c.config.get("path", f"/channel/{c.id}")
            hub = c.config.get("hub_url", "http://localhost:8765")
            fields = "new string[] {" + ", ".join(self._cs_str(f) for f in c.fields) + "}"
            note = "" if c.protocol in ("http", "websocket") else f"  // {c.protocol}: bridged to the hub by python/main.py"
            L.append(f'        hub.Add("{c.id}", "{hub}{path}", "{src}", "{dst}", {fields}, '
                     f'{int(c.config.get("interval_ms", 1000))}, {str(sl).lower()}, {str(dl).lower()});{note}')
        L += ["    }", "",
              "    static global::NlclEntity Setup(global::NlclEntity e, string id, string kind, bool controls, "
              "bool autoMove)",
              "    {",
              "        e.nlclId = id;", "        e.nlclKind = kind;", "        e.nlclControls = controls;",
              "        e.nlclAutoMove = autoMove;", "        global::NlclWorld.Register(e);", "        return e;",
              "    }", "",
              "    // shape: 0 none, 1 cube, 2 sphere, 3 capsule, 4 cylinder",
              "    // body: 0 trigger (kinematic), 1 solid static, 2 physics with gravity, 3 camera",
              "    static GameObject Make(string id, int shape, Vector3 pos, Vector3 size, string color, "
              "GameObject parent, int body)", "    {", "        GameObject go;", "        switch (shape)", "        {",
              "            case 1: go = GameObject.CreatePrimitive(PrimitiveType.Cube); break;",
              "            case 2: go = GameObject.CreatePrimitive(PrimitiveType.Sphere); break;",
              "            case 3: go = GameObject.CreatePrimitive(PrimitiveType.Capsule); break;",
              "            case 4: go = GameObject.CreatePrimitive(PrimitiveType.Cylinder); break;",
              "            default: go = new GameObject(); break;", "        }",
              "        go.name = id;", "        go.transform.position = pos;",
              "        if (shape == 3 || shape == 4) go.transform.localScale = new Vector3(size.x, size.y / 2f, size.z);",
              "        else if (shape != 0) go.transform.localScale = size;",
              "        if (parent != null) go.transform.SetParent(parent.transform, true);",
              "        Color c;",
              "        Renderer rend = go.GetComponent<Renderer>();",
              "        if (rend != null && ColorUtility.TryParseHtmlString(color, out c)) rend.material.color = c;",
              "        Collider col = go.GetComponent<Collider>();",
              "        if (body == 3)", "        {",
              "            Camera cam = Camera.main;",
              "            if (cam != null && cam.gameObject != go) Object.Destroy(cam.gameObject);",
              "            go.AddComponent<Camera>();", '            go.tag = "MainCamera";', "        }",
              "        else if (body == 2)", "        {",
              "            Rigidbody rb = go.AddComponent<Rigidbody>();",
              "            rb.constraints = RigidbodyConstraints.FreezeRotation | RigidbodyConstraints.FreezePositionZ;",
              "            rb.interpolation = RigidbodyInterpolation.Interpolate;", "        }",
              "        else if (body == 0 && col != null)", "        {",
              "            col.isTrigger = true;   // overlaps fire NlclCollision without pushing",
              "            Rigidbody rb = go.AddComponent<Rigidbody>();",
              "            rb.isKinematic = true;", "            rb.useGravity = false;", "        }",
              "        return go;", "    }", "",
              "    static void Lighting()", "    {",
              "#if UNITY_2023_1_OR_NEWER",
              "        Light existing = Object.FindFirstObjectByType<Light>();", "#else",
              "        Light existing = Object.FindObjectOfType<Light>();", "#endif",
              "        if (existing != null) return;",
              '        GameObject l = new GameObject("Sun");',
              "        Light light = l.AddComponent<Light>();", "        light.type = LightType.Directional;",
              "        l.transform.rotation = Quaternion.Euler(50f, -30f, 0f);", "    }", "",
              "    static void cam_bg()", "    {",
              "        if (Camera.main == null) return;",
              "        Camera.main.clearFlags = CameraClearFlags.SolidColor;",
              "        Camera.main.backgroundColor = new Color(0.09f, 0.1f, 0.13f);", "    }", "}", "}", ""]
        return "\n".join(L)

    # ================================================================== Godot
    def _gd_type(self, v: Any) -> str:
        return {"bool": "bool", "int": "int", "float": "float", "nlist": "Array", "slist": "Array",
                "string": "String"}[_ptype(v)]

    def _gd_lit(self, v: Any) -> str:
        t = _ptype(v)
        if t in ("nlist", "slist"):
            return "[" + ", ".join(self._gd_lit(x) for x in v) + "]"
        if t == "float":
            s = _f(v)
            return s if "." in s or "e" in s else s + ".0"
        if t == "string":
            return self._cs_str(v)
        return self.literal(v, "gdscript")

    def _gd_actions(self, info: _Info, acts: list[dict], ind: str) -> list[str]:
        L = []
        for a in acts:
            try:
                L += [ind + x for x in self._gd_action(info, a)]
            except Exception as ex:
                L.append(f"{ind}pass  # could not translate {a.get('op')}: {ex}")
        return L or [ind + "pass"]

    def _gd_action(self, info: _Info, a: dict) -> list[str]:
        op = a["op"]
        if op in ("set", "add", "toggle"):
            p = a["prop"]
            c = self._split(info, p)
            if c[0] == "pos":
                if op == "toggle":
                    return [f"pass  # toggle {p}: not meaningful for a position"]
                v = self._gd_coerce(info, a["value"], "float")
                return [f"position.{c[1]} {'=' if op == 'set' else '+='} {v}"]
            if c[0] == "remote":
                return [f"pass  # {describe_action(a)}: '{c[1]}' lives in another module - use a channel"]
            if c[0] == "ent":
                o = self._by_id[c[1]]
                if c[2] in ("x", "y", "z"):
                    v = self._gd_coerce(info, a.get("value", 0), "float")
                    return [f'if nlcl_world.entities.has("{o.id}") and is_instance_valid(nlcl_world.entities["{o.id}"]):',
                            f'\tnlcl_world.entities["{o.id}"].position.{c[2]} {"+=" if op == "add" else "="} {v}']
                if c[2] not in o.props:
                    return [f"pass  # {describe_action(a)}: {o.id} has no property '{c[2]}'"]
                return [f'if nlcl_world.entities.has("{o.id}") and is_instance_valid(nlcl_world.entities["{o.id}"]):',
                        "\t" + self._gd_assign(info, f'nlcl_world.entities["{o.id}"].{self._gd_field(c[2])}',
                                               o.props[c[2]], a)]
            return [self._gd_assign(info, self._gd_field(p), info.props.get(p, 0), a)]
        if op == "destroy":
            return ["nlcl_world.destroy(self)", "return"]
        if op == "log":
            return [f"nlcl_world.log_line(nlcl_id, {self._log_parts(info, a['text'], 'gd')})"]
        if op == "move":
            if self._is_jump(info, a):
                return ["nlcl_world.jump(self)"]
            xs = [self._gd_coerce(info, a.get(k, 0), "float") for k in ("x", "y", "z")]
            return [f"position += Vector3({', '.join(xs)})"]
        if op == "send":
            return [f"nlcl_world.send(self, {self._cs_str(a['to'])}, {json.dumps(a['fields'])})"]
        if op == "call":
            n = a["name"]
            args = a.get("args", [])
            if n == "jump":
                return ["nlcl_world.jump(self)"]
            if n in ("spawn", "fire", "shoot"):
                arg = self._gd_coerce(info, args[0], "string") if args else (
                    self._gd_field("spawn") if "spawn" in info.props else '""')
                return [f"nlcl_world.spawn({arg}, position + Vector3(0.8, 0, 0))"]
            if n in ("reset", "respawn", "restart"):
                return ["nlcl_world.reset_entity(self)"]
            return [f"do_{n}([{', '.join(self._x(info, x, 'gd') for x in args)}])"]
        return [f"pass  # {a.get('text', op)}"]

    def _gd_assign(self, info: _Info, tgt: str, cur: Any, a: dict) -> str:
        t = {"int": "int", "float": "float", "bool": "bool", "string": "string"}.get(_ptype(cur))
        op = a["op"]
        if op == "toggle":
            if t == "bool":
                return f"{tgt} = not {tgt}"
            if t in ("int", "float"):
                return f"{tgt} = 1 if {tgt} == 0 else 0"
            return f"pass  # toggle {a['prop']}: not a number or flag"
        if t is None:
            return f"pass  # {describe_action(a)}: list property"
        v = self._gd_coerce(info, a["value"], t)
        if op == "add" and t != "bool":
            return f"{tgt} += {v}"
        return f"{tgt} = {v}"

    def _gd_block(self, info: _Info, b: dict, ind: str) -> list[str]:
        if b.get("if") and not b["on"].startswith("update"):
            return [f"{ind}if {self._gd_cond(info, b['if'])}:"] + self._gd_actions(info, b["do"], ind + "\t")
        return self._gd_actions(info, b["do"], ind)

    def _gd_base(self, info: _Info) -> str:
        if info.kind == "camera":
            return "Camera3D"
        if info.gravity:
            return "RigidBody3D"
        if info.solid:
            return "StaticBody3D"
        return "Node3D"

    def _gd_entity(self, ctx: BuildContext, info: _Info) -> str:
        e = info.e
        L = [f"extends {self._gd_base(info)}",
             f"## {e.name} ({info.kind}) - generated by NLCL from {ctx.project}; edit freely.", "",
             "var nlcl_world  # the world (main.gd)", 'var nlcl_id := ""', 'var nlcl_kind := ""',
             "var nlcl_controls := false", "var nlcl_auto_move := false", "var nlcl_start := Vector3.ZERO", ""]
        for k, v in info.props.items():
            L.append(f"var {self._gd_field(k)}: {self._gd_type(v)} = {self._gd_lit(v)}")
        groups: dict[str, list] = {}
        for b in info.behaviors:
            groups.setdefault(b["on"].split(":")[0], []).append(b)
        timers, upd, changes = groups.get("timer", []), groups.get("update", []), groups.get("change", [])
        inputs = groups.get("input", [])
        spawner = bool(info.props.get("spawn")) and _num(info.props.get("spawn_interval_ms"), 0) > 0
        priv = [f"var _timer{n} := 0.0" for n in range(len(timers))]
        priv += [f"var _edge{n} := false" for n, b in enumerate(upd) if b["if"]]
        for b in changes:
            priv.append(f"var _prev_{b['on'].split(':', 1)[1]}")
        if spawner:
            priv += ["var _spawn_timer := 0.0", "var _spawn_count := 0"]
        L += [""] + list(dict.fromkeys(priv)) if priv else []

        def src(p):
            return f"position.{p}" if p in ("x", "y", "z") else self._gd_field(p)
        L += ["", "", "func _ready() -> void:", "\tnlcl_start = position"]
        for b in changes:
            p = b["on"].split(":", 1)[1]
            L.append(f"\t_prev_{p} = {src(p)}")
        for b in groups.get("start", []):
            L += self._gd_block(info, b, "\t")
        uses_delta = bool(timers) or spawner
        L += ["", "", f"func _process({'delta' if uses_delta else '_delta'}: float) -> void:"]
        body = []
        for n, b in enumerate(timers):
            ms = b["on"].split(":", 1)[1]
            body += [f"\t_timer{n} += delta * 1000.0", f"\tif _timer{n} >= {ms}.0:  # every {ms} ms",
                     f"\t\t_timer{n} -= {ms}.0"] + self._gd_block(info, b, "\t\t")
        for n, b in enumerate(upd):
            if b["if"]:
                body += [f"\tvar now{n}: bool = {self._gd_cond(info, b['if'])}",
                         f"\tvar fire{n} = now{n} and not _edge{n}  # fires when the condition becomes true",
                         f"\t_edge{n} = now{n}", f"\tif fire{n}:"] + self._gd_actions(info, b["do"], "\t\t")
            else:
                body += self._gd_actions(info, b["do"], "\t")
        if spawner:
            mx = self._gd_field("max_spawn") if "max_spawn" in info.props else "10"
            body += ["\t_spawn_timer += delta * 1000.0",
                     f"\tif _spawn_timer >= {self._gd_field('spawn_interval_ms')} and _spawn_count < {mx}:",
                     "\t\t_spawn_timer = 0.0", "\t\t_spawn_count += 1",
                     f"\t\tnlcl_world.spawn({self._gd_field('spawn')}, position + Vector3(0.8, 0, 0))"]
        for b in changes:
            p = b["on"].split(":", 1)[1]
            body += [f"\tif {src(p)} != _prev_{p}:  # {p} changed", f"\t\t_prev_{p} = {src(p)}"]
            body += self._gd_block(info, b, "\t\t")
        L += body or ["\tpass"]
        keys = [(self._input_kind(b["on"]), b) for b in inputs]
        if any(k[0] in ("key", "click") for k, _ in keys):
            L += ["", "", "func _unhandled_input(event: InputEvent) -> void:",
                  "\tif event is InputEventKey and event.pressed and not event.echo:",
                  "\t\tvar key: String = nlcl_world.key_name(event)"]
            kb = [(k, b) for k, b in keys if k[0] == "key"]
            for k, b in kb:
                L += [f'\t\tif key == "{k[1]}":'] + self._gd_block(info, b, "\t\t\t")
            if not kb:
                L.append("\t\tpass")
            cb = [(k, b) for k, b in keys if k[0] == "click"]
            if cb:
                L += ["\telif event is InputEventMouseButton and event.pressed and event.button_index == MOUSE_BUTTON_LEFT:",
                      "\t\tvar hit: String = nlcl_world.entity_at(event.position)"]
                for k, b in cb:
                    L += ["\t\tif true:  # any click" if k[1] == "mouse" else f'\t\tif hit == "{k[1]}":']
                    L += self._gd_block(info, b, "\t\t\t")
        if "collision" in groups:
            L += ["", "", "func nlcl_collision(_other) -> void:"]
            for b in groups["collision"]:
                w = self._with_key(b)
                if w:   # only collisions with that entity (id) or kind
                    L += [f'\tif _other != null and "nlcl_id" in _other and (_other.nlcl_id == "{w}" or _other.nlcl_kind == "{w}"):']
                    L += self._gd_block(info, b, "\t\t") or ["\t\tpass"]
                else:
                    L += self._gd_block(info, b, "\t")
        else:
            L += ["", "", "func nlcl_collision(_other) -> void:", "\tpass"]
        msg = []
        for b in groups.get("message", []):
            s = ident(b["on"].split(":", 1)[1])
            s = self._idmap.get(s, s)
            msg += ["\tif is_destination:" if s in ("any", "") else f'\tif src == "{s}":']
            msg += self._gd_block(info, b, "\t\t")
        uses_src = any(ln.startswith("\tif src ==") for ln in msg)
        uses_dst = "\tif is_destination:" in msg
        L += ["", "", f"func nlcl_message({'' if uses_src else '_'}src: String, "
                      f"{'' if uses_dst else '_'}is_destination: bool) -> void:"]
        L += msg or ["\tpass"]
        rem = []
        for k, b in keys:
            if k[0] == "remote":
                rem += [f'\tif src == "{k[1]}":'] + self._gd_block(info, b, "\t\t")
        L += ["", "", "func nlcl_remote_input(src: String) -> void:" if rem else
              "func nlcl_remote_input(_src: String) -> void:"]
        L += rem or ["\tpass"]
        calls = sorted({a["name"] for b in info.behaviors for a in b["do"] if a["op"] == "call"} -
                       {"jump", "spawn", "fire", "shoot", "reset", "respawn", "restart"})
        for c in calls:
            L += ["", "", f"func do_{c}(_args := []) -> void:",
                  f'\tnlcl_world.log_line(nlcl_id, "{c} called")  # TODO: implement']
        L += ["", "", "func nlcl_get(field: String):", "\tmatch field:"]
        for k in info.props:
            L += [f'\t\t"{k}":', f"\t\t\treturn {self._gd_field(k)}"]
        L += ['\t\t"x":', "\t\t\treturn position.x", '\t\t"y":', "\t\t\treturn position.y",
              '\t\t"z":', "\t\t\treturn position.z", "\treturn null", "", "",
              "func nlcl_set(field: String, value) -> void:", "\tmatch field:"]
        for k, v in info.props.items():
            t = _ptype(v)
            f = self._gd_field(k)
            conv = {"int": "int(value)", "float": "float(value)", "bool": "nlcl_world.to_bool(value)",
                    "string": "str(value)"}.get(t, f"value if value is Array else {f}")
            L += [f'\t\t"{k}":', f"\t\t\t{f} = {conv}"]
        L += ['\t\t"x":', "\t\t\tposition.x = float(value)", '\t\t"y":', "\t\t\tposition.y = float(value)",
              '\t\t"z":', "\t\t\tposition.z = float(value)", ""]
        return "\n".join(L)

    def _gd_main(self, ctx: BuildContext, infos: list[_Info]) -> str:
        l, r, b, t = self._bounds
        dist = self._cam_dist()
        L = ['extends "res://nlcl_world.gd"', f"## {ctx.project}: builds the scene in code. Generated by NLCL.", "",
             "", "func _build() -> void:"]
        if self._ground:
            gx, gy, gw, gh = self._ground
            L.append(f"\tadd_ground(Vector3({_f(gx)}, {_f(gy)}, 0), Vector3({_f(gw)}, {_f(gh)}, 4))")
        cam = None
        for i in infos:
            v = lambda xs: "Vector3(" + ", ".join(_f(x) for x in xs) + ")"
            pos = list(i.rel if i.parent else i.pos)
            if i.kind == "camera" and not i.explicit_pos:
                pos[2] = dist
            flags = json.dumps({"controls": i.controls, "auto_move": i.auto_move}).replace("true", "true")
            var = "var cam = " if i.kind == "camera" and cam is None else ""
            if var:
                cam = i
            L.append(f"\t# {i.e.name}: {i.kind}")
            if self._templates.get(i.id):
                L.append(f'\tadd_template(preload("res://{i.gd}.gd"), "{i.id}", "{i.kind}", "{i.shape}", '
                         f'{v(i.size)}, "{i.color or "#ffffff"}", {flags})  # spawned by a spawner')
                continue
            L.append(f'\t{var}make(preload("res://{i.gd}.gd"), "{i.id}", "{i.kind}", "{i.shape}", {v(pos)}, '
                     f'{v(i.size)}, "{i.color or "#ffffff"}", "{i.parent}", {flags})')
        if cam:
            L += ["\tcam.current = true"]
            if cam.props.get("follow"):
                L.append(f'\tcam.set_meta("follow", "{cam.props["follow"]}")')
        else:
            L.append(f'\tadd_camera(Vector3({_f((l + r) / 2)}, {_f((b + t) / 2 + 1)}, {_f(dist)}), "")')
        for c in ctx.channels:
            if c.protocol in ("spi", "i2c", "gpio", "wire"):
                continue
            ids = [i.e.id for i in infos]
            sl, dl = c.src.id in ids, c.dst.id in ids
            src = self._idmap.get(c.src.ident, c.src.ident) if sl else c.src.ident
            dst = self._idmap.get(c.dst.ident, c.dst.ident) if dl else c.dst.ident
            url = c.config.get("hub_url", "http://localhost:8765") + c.config.get("path", f"/channel/{c.id}")
            L.append(f'\tadd_channel("{c.id}", "{url}", "{src}", "{dst}", {json.dumps(c.fields)}, '
                     f'{int(c.config.get("interval_ms", 1000))}, {str(sl).lower()}, {str(dl).lower()})'
                     + ("" if c.protocol in ("http", "websocket") else f"  # {c.protocol} bridged by python/main.py"))
        L.append("")
        return "\n".join(L)

    def _godot(self, ctx: BuildContext, infos: list[_Info]) -> dict[str, str]:
        files = {
            "godot/project.godot": "\n".join([
                "; Engine configuration file. Generated by NLCL - open with Godot 4.2+.", "",
                "config_version=5", "", "[application]", "",
                f'config/name="{self._cs_str(ctx.project)[1:-1]}"', 'run/main_scene="res://main.tscn"',
                'config/features=PackedStringArray("4.2")', "", "[display]", "",
                "window/size/viewport_width=1280", "window/size/viewport_height=720", ""]),
            "godot/main.tscn": "\n".join([
                '[gd_scene load_steps=2 format=3]', "",
                '[ext_resource type="Script" path="res://main.gd" id="1_main"]', "",
                '[node name="Main" type="Node3D"]', 'script = ExtResource("1_main")', ""]),
            "godot/main.gd": self._gd_main(ctx, infos),
            "godot/nlcl_world.gd": (ASSETS / "nlcl_world.gd").read_text(encoding="utf-8"),
        }
        for i in infos:
            files[f"godot/{i.gd}.gd"] = self._gd_entity(ctx, i)
        return files

    # ================================================================== HTML5 preview
    def _js_actions(self, info: _Info, acts: list[dict]) -> str:
        out = []
        for a in acts:
            try:
                out.append(self._js_action(info, a))
            except Exception as ex:
                out.append(f"/* could not translate {a.get('op')}: {ex} */")
        return " ".join(out)

    def _js_action(self, info: _Info, a: dict) -> str:
        op = a["op"]
        j = json.dumps
        if op in ("set", "add", "toggle"):
            p = a["prop"]
            c = self._split(info, p)
            if c[0] == "pos":
                tgt = f"o.{c[1]}"
            elif c[0] == "ent":
                tgt = (f"W.byId[{j(c[1])}].{c[2]}" if c[2] in ("x", "y", "z") else f"W.byId[{j(c[1])}].p[{j(c[2])}]")
                inner = self._js_assign(info, tgt, a)
                return f"if (W.byId[{j(c[1])}]) {{ {inner} }}"
            elif c[0] == "remote":
                return f"/* {describe_action(a)}: remote property */"
            else:
                tgt = f"o.p[{j(c[1])}]"
            return self._js_assign(info, tgt, a)
        if op == "destroy":
            return "w.destroy(o); return;"
        if op == "log":
            return f"w.log(o, {self._log_parts(info, a['text'], 'js')});"
        if op == "move":
            if self._is_jump(info, a):
                return 'w.call(o, "jump", []);'
            return f"w.move(o, {', '.join(self._x(info, a.get(k, 0), 'js') for k in ('x', 'y', 'z'))});"
        if op == "send":
            return f"w.send(o, {j(a['to'])}, {j(a['fields'])});"
        if op == "call":
            return f"w.call(o, {j(a['name'])}, [{', '.join(self._x(info, x, 'js') for x in a.get('args', []))}]);"
        return f"/* {a.get('text', op)} */"

    def _js_assign(self, info: _Info, tgt: str, a: dict) -> str:
        if a["op"] == "toggle":
            return f"{tgt} = w.tog({tgt});"
        v = self._x(info, a["value"], "js")
        return f"{tgt} {'+=' if a['op'] == 'add' else '='} {v};"

    def _js_beh(self, info: _Info) -> dict[str, list[str]]:
        B: dict[str, list[str]] = {}

        def item(b, extra=""):
            c = f"c: (o, w) => ({self._x(info, b['if'], 'js')}), " if b.get("if") else ""
            return "{ " + extra + c + "f: (o, w, dt) => { " + self._js_actions(info, b["do"]) + " } }"
        for b in info.behaviors:
            trig = b["on"]
            kind = trig.split(":")[0]
            if kind == "start":
                B.setdefault("start", []).append(item(b))
            elif kind == "update":
                B.setdefault("update", []).append(item(b))
            elif kind == "collision":
                w = self._with_key(b)
                B.setdefault("collision", []).append(item(b, f"w: {json.dumps(w)}, " if w else ""))
            elif kind == "timer":
                B.setdefault("timers", []).append(item(b, f"ms: {int(_num(trig.split(':', 1)[1], 1000)) or 1000}, "))
            elif kind == "change":
                B.setdefault("change", []).append(item(b, f"k: {json.dumps(trig.split(':', 1)[1])}, "))
            elif kind == "message":
                s = ident(trig.split(":", 1)[1])
                s = self._idmap.get(s, s)
                src = "null" if s in ("any", "") else json.dumps([s])
                B.setdefault("message", []).append(item(b, f"src: {src}, "))
            elif kind == "input":
                k = self._input_kind(trig)
                if k[0] == "key":
                    B.setdefault("input", []).append(item(b, f"key: {json.dumps(k[1])}, "))
                elif k[0] == "click":
                    B.setdefault("click", []).append(item(b, f"id: {json.dumps(k[1])}, "))
                else:
                    B.setdefault("remote", []).append(item(b, f"src: {json.dumps([k[1]])}, "))
        return B

    def _preview(self, ctx: BuildContext, infos: list[_Info]) -> str:
        ents = []
        if self._ground:
            gx, gy, gw, gh = self._ground
            ents.append({"id": "ground", "name": "ground", "kind": "ground", "x": gx, "y": gy, "z": 0, "w": gw,
                         "h": gh, "shape": "cube", "color": "#3f4654", "controls": False, "autoMove": False,
                         "gravity": False, "solid": True, "visual": True, "noHit": True, "p": {}})
        for i in infos:
            p = {k: v for k, v in i.props.items()}
            ents.append({"id": i.id, "name": i.e.name, "kind": i.kind, "x": round(i.pos[0], 3),
                         "y": round(i.pos[1], 3), "z": round(i.pos[2], 3), "w": i.size[0], "h": i.size[1],
                         "shape": i.shape, "color": i.color or "#94a3b8", "controls": i.controls,
                         "autoMove": i.auto_move, "gravity": i.gravity, "solid": i.solid, "visual": i.visual,
                         "template": bool(self._templates.get(i.id)), "parent": i.parent, "p": p})
        if any(e["id"] == "ground" for e in ents[1:]):
            ents[0]["id"] = "_ground"
        chans = []
        ids = {i.e.id for i in infos}
        for c in ctx.channels:
            if c.protocol in ("spi", "i2c", "gpio", "wire"):
                continue
            sl, dl = c.src.id in ids, c.dst.id in ids
            chans.append({"id": c.id, "url": c.config.get("hub_url", "http://localhost:8765") +
                          c.config.get("path", f"/channel/{c.id}"),
                          "src": self._idmap.get(c.src.ident, c.src.ident) if sl else c.src.ident,
                          "dst": self._idmap.get(c.dst.ident, c.dst.ident) if dl else c.dst.ident,
                          "fields": c.fields, "interval": int(c.config.get("interval_ms", 1000)),
                          "srcLocal": sl, "dstLocal": dl})
        data = json.dumps({"project": ctx.project, "entities": ents, "channels": chans}, ensure_ascii=False)
        beh_lines = ["const BEH = {"]
        for i in infos:
            B = self._js_beh(i)
            if not B:
                continue
            beh_lines.append(f"  {json.dumps(i.id)}: {{")
            for k, items in B.items():
                beh_lines.append(f"    {k}: [")
                beh_lines += [f"      {it}," for it in items]
                beh_lines.append("    ],")
            beh_lines.append("  },")
        beh_lines.append("};")
        script = ("// " + f"{ctx.project} - generated by NLCL\nconst DATA = {data};\n" + "\n".join(beh_lines) + "\n" +
                  (ASSETS / "preview_runtime.js").read_text(encoding="utf-8"))
        script = script.replace("</script", "<\\/script").replace("</SCRIPT", "<\\/SCRIPT")
        keys = sorted({self._input_kind(b["on"])[1] for i in infos for b in i.behaviors
                       if b["on"].startswith("input:") and self._input_kind(b["on"])[0] == "key"})
        hints = []
        if any(i.controls for i in infos):
            hints.append("arrows / WASD move" + (", space jumps" if any(i.controls and i.gravity for i in infos) else ""))
        if keys:
            hints.append("keys: " + ", ".join(keys))
        if any(b["on"].startswith("input:") and self._input_kind(b["on"])[0] == "click"
               for i in infos for b in i.behaviors):
            hints.append("click entities")
        title = self._html(ctx.project)
        return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} preview</title>
<style>
  :root {{ --bg: #0f1115; --panel: #171a21; --line: #262b36; --text: #d7dbe4; --muted: #8a92a3; --accent: #60a5fa; }}
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; height: 100%; background: var(--bg); color: var(--text);
    font: 13px/1.4 system-ui, -apple-system, Segoe UI, Roboto, sans-serif; }}
  .app {{ display: grid; grid-template-columns: 1fr 380px; height: 100vh; }}
  .stage {{ position: relative; min-height: 300px; }}
  #view {{ width: 100%; height: 100%; display: block; outline: none; }}
  .hud {{ position: absolute; left: 12px; top: 10px; right: 12px; display: flex; gap: 8px; align-items: center;
    flex-wrap: wrap; pointer-events: none; }}
  .hud > * {{ pointer-events: auto; }}
  .title {{ font-weight: 600; margin-right: 6px; }}
  .chip {{ background: #00000066; border: 1px solid var(--line); border-radius: 999px; padding: 2px 10px; color: var(--muted); }}
  button {{ background: var(--panel); color: var(--text); border: 1px solid var(--line); border-radius: 6px;
    padding: 4px 10px; cursor: pointer; font: inherit; }}
  button:hover {{ border-color: var(--accent); }}
  aside {{ background: var(--panel); border-left: 1px solid var(--line); display: flex; flex-direction: column; min-height: 0; }}
  aside h2 {{ font-size: 11px; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); margin: 12px 12px 6px; }}
  .tbl {{ overflow: auto; max-height: 45%; padding: 0 12px; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 12px; }}
  th, td {{ text-align: left; padding: 3px 6px; border-bottom: 1px solid var(--line); vertical-align: top; }}
  th {{ color: var(--muted); font-weight: 500; }}
  td:last-child {{ word-break: break-word; color: #b9c0cf; }}
  #log {{ flex: 1; overflow: auto; margin: 0 12px 12px; padding: 8px; background: #0b0d11; border-radius: 6px;
    font: 12px/1.45 ui-monospace, SFMono-Regular, Consolas, monospace; white-space: pre-wrap; min-height: 120px; }}
  @media (max-width: 800px) {{
    .app {{ grid-template-columns: 1fr; grid-template-rows: 60vh auto; height: auto; }}
    aside {{ border-left: 0; border-top: 1px solid var(--line); }}
  }}
</style>
</head>
<body>
<div class="app">
  <div class="stage">
    <canvas id="view" tabindex="0"></canvas>
    <div class="hud">
      <span class="title">{title}</span>
      <button id="pause">Pause</button><button id="restart">Restart</button>
      <span class="chip" id="time">0.0 s</span>
      <span class="chip">{self._html(" | ".join(hints) or "no controls bound")}</span>
      <span class="chip" id="hub"></span>
    </div>
  </div>
  <aside>
    <h2>Entities</h2>
    <div class="tbl"><table id="ents"></table></div>
    <h2>Log</h2>
    <pre id="log"></pre>
  </aside>
</div>
<script>
{script}
</script>
</body>
</html>
"""

    @staticmethod
    def _html(s: str) -> str:
        return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

    # ================================================================== build
    def generate(self, ctx: BuildContext) -> dict[str, str]:
        infos = self._analyse(ctx)
        files: dict[str, str] = {}
        for a in ("NlclEntity.cs", "NlclHub.cs", "NlclInput.cs"):
            files[f"unity/Scripts/{a}"] = (ASSETS / a).read_text(encoding="utf-8")
        files["unity/Scripts/NlclSceneBuilder.cs"] = self._cs_builder(ctx, infos)
        for i in infos:
            files[f"unity/Scripts/{i.cls}.cs"] = self._cs_entity(ctx, i)
        files.update(self._godot(ctx, infos))
        files["preview.html"] = self._preview(ctx, infos)
        files["README.md"] = self._readme(ctx, infos)
        return files

    def _readme(self, ctx: BuildContext, infos: list[_Info]) -> str:
        rows = "\n".join(f"| {i.e.name} | {i.kind} | `{i.cls}.cs` / `{i.gd}.gd` | {i.shape} | "
                         f"{'keys' if i.controls else ''}{' auto' if i.auto_move else ''}"
                         f"{' gravity' if i.gravity else ''}{' solid' if i.solid else ''} | "
                         f"{len(i.behaviors)} |" for i in infos)
        ch = "\n".join(f"- `{c.id}`: {c.src.name} ({c.src.domain}) -> {c.dst.name} ({c.dst.domain}) via "
                       f"{c.protocol}, fields {', '.join(c.fields)}" for c in ctx.channels) or "- none"
        return f"""# {ctx.project} - game module (generated by NLCL)

| entity | kind | scripts | shape | flags | behaviours |
|---|---|---|---|---|---|
{rows}

Channels (through the NLCL hub, start `python ../python/main.py` first):
{ch}

{self.toolchain}

Conventions: x to the right, y up, z depth. Unity camera looks along +z, Godot along -z.
Keyboard names: space, enter, up/down/left/right, a-z, 0-9, f1-f12, escape, shift, ctrl, alt, tab.
"""

    # ================================================================== run
    def run(self, ctx: BuildContext) -> RunResult | None:
        html = ctx.outdir / "preview.html"
        if not html.exists():
            return RunResult(False, "build first")
        out = []
        ok = True
        try:
            text = html.read_text(encoding="utf-8")
            m = re.search(r"<script>\n?(.*)</script>", text, re.S)
            script = m.group(1) if m else ""
        except OSError as e:
            return RunResult(False, f"cannot read preview.html: {e}")
        node = self.which("node", "nodejs")
        cmd = ""
        if node and script:
            fd, tmp = tempfile.mkstemp(suffix=".js", prefix="nlcl_preview_")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(script)
                chk = self.sh([node, "--check", tmp], ctx.outdir, timeout=20)
                cmd = chk.command
                ok = chk.ok
                out.append("node --check preview script: " + ("OK" if chk.ok else "FAILED\n" + chk.output))
                if chk.ok:
                    sim = self.sh([node, tmp, "3"], ctx.outdir, timeout=20)
                    lines = sim.output.splitlines()
                    out.append("headless simulation (3 s):")
                    out += ["  " + ln for ln in (lines[:25] + (["  ..."] if len(lines) > 40 else []) + lines[-15:]
                                                  if len(lines) > 40 else lines)]
                    ok = ok and sim.ok
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        else:
            out.append("node not found: skipped the JavaScript check")
        cs = self._csharp_syntax(ctx)
        if cs:
            out.append(cs)
        out.append(f"open {html} in a browser to play the preview")
        if self.which("godot", "godot4"):
            out.append(f"Godot: godot --path \"{ctx.outdir / 'godot'}\"")
        return RunResult(ok, "\n".join(out), cmd)

    def _csharp_syntax(self, ctx: BuildContext) -> str:
        """Parse-only check of the Unity scripts with Roslyn from the .NET SDK (if installed)."""
        dotnet = self.which("dotnet")
        if not dotnet:
            return ""
        try:
            sdks = self.sh([dotnet, "--list-sdks"], ctx.outdir, timeout=15).output.splitlines()
            csc = None
            for line in reversed(sdks):
                m = re.match(r"(\S+)\s+\[(.+)\]", line.strip())
                if m:
                    p = Path(m.group(2)) / m.group(1) / "Roslyn" / "bincore" / "csc.dll"
                    if p.exists():
                        csc = p
                        break
            if not csc:
                return ""
            files = sorted(str(p) for p in (ctx.outdir / "unity" / "Scripts").glob("*.cs"))
            tmpdir = tempfile.mkdtemp(prefix="nlcl_cs_")
            r = self.sh([dotnet, str(csc), "-nologo", "-t:library", "-nostdlib-", f"-out:{tmpdir}/x.dll"] + files,
                        ctx.outdir, timeout=60)
            syntax = [ln for ln in r.output.splitlines() if re.search(r"error CS1\d\d\d", ln)]
            return ("C# syntax (Roslyn parse, Unity references not loaded): " +
                    ("OK" if not syntax else "ERRORS\n" + "\n".join(syntax[:20])))
        except Exception as e:
            return f"C# syntax check skipped: {e}"


MODULE = GameModule()
