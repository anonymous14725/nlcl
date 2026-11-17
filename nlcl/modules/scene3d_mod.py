"""3D domain: three.js scenes / digital twins in the browser + a Blender script.

Output (in <build>/3d/):
  index.html        self-contained page (scene code inlined) - double-click to open
  scene.js          the same scene code as an ES module (edit + use dev.html)
  dev.html          loads scene.js; needs a local static server (serve.py)
  serve.py          tiny static server (python serve.py -> http://localhost:8000/dev.html)
  blender_scene.py  recreates the static scene in Blender (free) via bpy
  README.md         how to run it

three.js is loaded from unpkg (three@0.160.0) through an importmap, so the page
needs internet access the first time (the browser caches it afterwards).
"""
from __future__ import annotations

import json
import math
import pprint
import re
import sys
import tempfile
from pathlib import Path

from ..ir import Entity, describe_action
from .base import BuildContext, Channel, DomainModule, RunResult

ASSET_DIR = Path(__file__).parent / "_assets" / "3d"
THREE_VERSION = "0.160.0"
THREE_URL = f"https://unpkg.com/three@{THREE_VERSION}/build/three.module.js"
ADDONS_URL = f"https://unpkg.com/three@{THREE_VERSION}/examples/jsm/"

# entity kind -> rendering shape understood by the JS runtime / Blender script
SHAPE_OF_KIND = {
    "object": "box", "cube": "box", "box": "box", "crate": "box", "block": "box",
    "sphere": "sphere", "ball": "sphere", "orb": "sphere",
    "cylinder": "cylinder", "column": "cylinder", "pillar": "cylinder",
    "cone": "cone", "torus": "torus", "toru": "torus", "tori": "torus", "ring": "torus", "donut": "torus",
    "plane": "plane", "floor": "plane", "ground": "ground", "terrain": "ground",
    "light": "light", "lamp": "light", "sun": "light",
    "camera": "camera", "label": "label", "text": "label", "sign": "label",
    "model": "model", "mesh": "model", "twin": "twin",
    "building": "building", "house": "building", "tank": "tank", "silo": "tank",
    "pipe": "pipe", "tube": "pipe", "arm": "arm",
    "scene": "group", "group": "group", "room": "room", "assembly": "group",
}
SHAPES = set(SHAPE_OF_KIND.values())
PIPEABLE = ("http", "websocket", "serial")   # protocols a browser reaches through the NLCL hub

# common colour names -> hex (CSS names are also understood by three.js at runtime)
COLORS = {
    "red": "#e53935", "green": "#43a047", "blue": "#1e88e5", "yellow": "#fdd835", "orange": "#fb8c00",
    "purple": "#8e24aa", "violet": "#8e24aa", "pink": "#ec407a", "white": "#ffffff", "black": "#111111",
    "gray": "#808080", "grey": "#808080", "brown": "#795548", "cyan": "#00bcd4", "magenta": "#d81b60",
    "silver": "#c0c0c0", "gold": "#ffc107", "lime": "#cddc39", "navy": "#1a237e", "teal": "#00897b",
    "maroon": "#800000", "olive": "#808000", "beige": "#f5f5dc", "turquoise": "#40e0d0",
    "lightblue": "#90caf9", "darkblue": "#0d47a1", "lightgreen": "#a5d6a7", "darkgreen": "#1b5e20",
    "lightgray": "#d3d3d3", "lightgrey": "#d3d3d3", "darkgray": "#505050", "darkgrey": "#505050",
    "darkred": "#8b0000", "skyblue": "#87ceeb", "steel": "#71797e", "steelblue": "#4682b4",
    "concrete": "#9e9e9e", "wood": "#a0703c", "glass": "#bfe3f2", "copper": "#b87333", "water": "#2f7fd8",
}
DEFAULT_HEX = {
    "box": "#8fa3bf", "sphere": "#d9a441", "cylinder": "#6fb08f", "cone": "#c46b6b", "torus": "#a57fd1",
    "plane": "#7d8794", "ground": "#4f5b4a", "building": "#b7bec9", "tank": "#9aa7b5", "pipe": "#7f8c99",
    "arm": "#f0a020", "twin": "#5aa7d6", "model": "#9aa0a6", "room": "#5c6470", "light": "#ffffff",
}


def color_hex(v, fallback: str = "#8fa3bf") -> str:
    """Best-effort conversion of a colour prop to #rrggbb (never raises)."""
    try:
        if isinstance(v, bool) or v is None or v == "":
            return fallback
        if isinstance(v, (int, float)):
            return "#%06x" % (int(v) & 0xFFFFFF)
        if isinstance(v, (list, tuple)) and len(v) >= 3:
            vals = [float(x) for x in v[:3]]
            if all(x <= 1 for x in vals):
                vals = [x * 255 for x in vals]
            return "#" + "".join("%02x" % max(0, min(255, int(x))) for x in vals)
        s = str(v).strip().lower()
        if s.startswith("0x"):
            s = "#" + s[2:]
        if re.fullmatch(r"[0-9a-f]{6}", s):
            s = "#" + s
        if re.fullmatch(r"#[0-9a-f]{6}", s):
            return s
        if re.fullmatch(r"#[0-9a-f]{3}", s):
            return "#" + "".join(c * 2 for c in s[1:])
        key = re.sub(r"[\s_\-]+", "", s)
        return COLORS.get(key) or COLORS.get(s.split()[-1] if s.split() else "", fallback)
    except Exception:
        return fallback


# "angle" props are rotations in degrees: angle (and angle_z) about z, angle_x / angle_y about x / y
ROT_PROPS = {"angle": 2, "angle_z": 2, "angle_x": 0, "angle_y": 1}


def _num(v, d: float = 0.0) -> float:
    try:
        if isinstance(v, bool):
            return float(v)
        f = float(v)
        return f if math.isfinite(f) else d
    except (TypeError, ValueError):
        return d


def _vec(v, d, fill_last: bool = False) -> list[float]:
    if isinstance(v, (list, tuple)) and v:
        a = [_num(x, 0.0 if not fill_last else 1.0) for x in v[:3]]
        while len(a) < 3:
            a.append(a[-1] if fill_last else 0.0)
        return a
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return [float(v)] * 3
    if isinstance(v, str):
        try:
            f = float(v)
            return [f] * 3
        except ValueError:
            pass
    return list(d)


def _js(v) -> str:
    """JSON literal that is safe to inline in an HTML <script>."""
    return json.dumps(v, ensure_ascii=False, default=str).replace("</", "<\\/")


def _js_list(items: list) -> str:
    """JS array literal with one compact JSON object per line."""
    if not items:
        return "[]"
    return "[\n  " + ",\n  ".join(_js(x) for x in items) + ",\n]"


class Scene3DModule(DomainModule):
    name = "3d"
    title = "3D scenes & digital twins (three.js in the browser, Blender script)"
    aliases = ("3d", "threejs", "three.js", "webgl", "scene", "twin", "digital-twin", "digital_twin",
               "سه‌بعدی", "سه_بعدی")
    keywords = ("threejs", "three.js", "webgl", "render", "renderer", "rendering", "visualization", "visualisation",
                "visualize", "visualise", "gltf", "glb", "blender", "viewer", "factory", "warehouse", "silo",
                "pipeline", "floor", "wall", "hologram", "lamp", "skyline", "city", "office", "3d", "orbit", "mesh",
                "spotlight", "sunlight")
    kinds = {
        "object": {},
        "scene": {},
        "group": {},
        "room": {"size": [10, 3, 8]},
        "cube": {"size": [1, 1, 1]},
        "box": {"size": [1, 1, 1]},
        "sphere": {"radius": 0.5},
        "cylinder": {"radius": 0.5, "height": 1},
        "cone": {"radius": 0.5, "height": 1},
        "torus": {"radius": 0.5, "tube": 0.15},
        "plane": {"size": [4, 0, 4]},
        "ground": {"size": [20, 0, 20]},
        "light": {"subtype": "point", "intensity": 1, "position": [3, 5, 3]},
        "camera": {"position": [6, 5, 8], "fov": 50},
        "label": {"text": "", "position": [0, 2, 0]},
        "model": {"url": "", "size": [1, 1, 1]},
        "twin": {"size": [1, 1, 1]},
        "building": {"size": [10, 6, 8]},
        "tank": {"radius": 1, "height": 3, "level": 50},
        "pipe": {"radius": 0.15, "length": 4},
        "arm": {"base_angle": 0, "shoulder": 30, "elbow": 45},
    }
    suggested_props = ("position", "size", "color", "rotation", "opacity", "level", "temperature", "text")
    protocols = ("http", "websocket")
    toolchain = """Open `3d/index.html` in a modern browser (Chrome/Edge/Firefox; three.js r160 comes from
unpkg via an importmap, so internet access is needed once). For live data start the hub first:

    python python/main.py          # NLCL hub on http://localhost:8765 (+ python objects)
    python 3d/serve.py             # optional: http://localhost:8000/ (use if file:// fetches are blocked)

Without the hub the page shows "hub offline - simulated" and invents plausible values.
Blender (free, blender.org) can rebuild the static scene:

    blender --background --python 3d/blender_scene.py -- --save scene.blend --gltf scene.glb"""

    # ------------------------------------------------------------------ helpers
    def shape_of(self, e: Entity) -> str:
        s = str(e.props.get("shape") or "").strip().lower()
        if s in SHAPE_OF_KIND:
            return SHAPE_OF_KIND[s]
        k = str(e.kind or "object").lower()
        if k in SHAPE_OF_KIND:
            return SHAPE_OF_KIND[k]
        sub = str(e.props.get("subtype") or "").strip().lower()   # "a 3d camera called cam at ..." -> kind 'at'
        if sub in SHAPE_OF_KIND and sub != "object":
            return SHAPE_OF_KIND[sub]
        for word, shape in SHAPE_OF_KIND.items():   # "water_tank" -> tank, "robot_arm" -> arm
            if word in k.split("_") and word != "object":
                return shape
        return "box"

    def validate(self, entity: Entity) -> list[str]:
        out = []
        try:
            c = entity.props.get("color")
            if isinstance(c, str) and c and color_hex(c, "") == "" and not c.startswith(("rgb", "hsl")):
                out.append(f"colour {c!r} is not a known name; three.js may fall back to grey")
            if self.shape_of(entity) == "model" and not entity.props.get("url"):
                out.append("model has no 'url' (.gltf/.glb); a placeholder box is shown")
        except Exception:
            pass
        return out

    @staticmethod
    def _idents(ctx: BuildContext) -> dict[str, str]:
        """entity id -> unique JS-safe identifier."""
        out, used = {}, set()
        for e in ctx.entities:
            base = e.ident or "obj"
            name = base
            i = 2
            while name in used:
                name = f"{base}{i}"
                i += 1
            used.add(name)
            out[e.id] = name
        return out

    def _resolver(self, ctx: BuildContext, ids: dict[str, str]):
        """Build a function mapping identifiers in expressions to JS expressions."""
        local: dict[str, str] = {}
        for e in ctx.entities:
            for k in (ids[e.id], e.ident, e.name.lower(), str(e.kind).lower()):
                local.setdefault(k, ids[e.id])
        remote: dict[str, str] = {}
        for e in ctx.system.entities.values():
            if e.domain != ctx.domain:
                for k in (e.ident, e.name.lower(), str(e.kind).lower()):
                    remote.setdefault(k, e.ident)

        def name(n: str) -> str:
            if "." in n:
                ent, prop = n.split(".", 1)
                prop = prop.replace(".", "_")
                if ent in ("self", "this", "it", "its"):
                    return f"self.get({_js(prop)})"
                if ent in local:
                    return f"obj({_js(local[ent])}).get({_js(prop)})"
                return f"remote({_js(remote.get(ent, ent))}, {_js(prop)})"
            if n == "dt":
                return "dt"
            if n.lower() in COLORS:   # "set color to red": the parser hands over a bare colour word
                return _js(COLORS[n.lower()])
            return f"self.get({_js(n)})"
        return name, local, remote

    def _expr(self, e, name) -> str:
        try:
            return self.expr(e, "js", name)
        except Exception:
            return "0 /* unparsable expression: " + str(e).replace("*/", "") + " */"

    def _action(self, a: dict, name, per_second: bool) -> str:
        op = a.get("op")
        try:
            if op == "set":
                return f"self.set({_js(a['prop'])}, {self._expr(a['value'], name)});"
            if op == "add":
                v = self._expr(a['value'], name)
                if per_second and str(a.get("prop")).lower() in ROT_PROPS:
                    v = f"({v}) * dt"   # "every frame, rotate it by 90 degrees" = an angular speed (deg/s)
                return f"self.add({_js(a['prop'])}, {v});"
            if op == "toggle":
                return f"self.set({_js(a['prop'])}, !self.get({_js(a['prop'])}));"
            if op == "destroy":
                return "self.destroy(); return;"
            if op == "log":
                return f"self.log({_js(a.get('text', ''))});"
            if op == "move":
                xs = ", ".join(self._expr(a.get(ax, 0), name) for ax in ("x", "y", "z"))
                # in per-frame behaviours a move is a velocity (units per second)
                return f"self.move({xs}{', dt' if per_second else ''});"
            if op == "send":
                to, fields = a.get("to"), [str(f) for f in (a.get("fields") or [])]
                if not to:   # "send to sensor" can arrive as fields=["to_sensor"]
                    tos = [f[3:] for f in fields if f.startswith("to_") and len(f) > 3]
                    if tos:
                        to, fields = tos[0], [f for f in fields if not f.startswith("to_")]
                return f"self.send({_js(to)}, {_js(fields)});"
            if op == "call":
                args = ", ".join(self._expr(x, name) for x in a.get("args", []))
                return f"self.call({_js(a.get('name', 'action'))}, [{args}]);"
        except Exception:
            pass
        return f"/* unsupported action: {describe_action(a) if isinstance(a, dict) and 'op' in a else a} */"

    def _fn(self, b: dict, name, per_second: bool, edge_key: str | None = None) -> str:
        lines = []
        if b.get("if") and edge_key:
            # "when <cond>, ..." checked every frame: act once when the condition becomes true
            lines.append(f"if (!self.edge({_js(edge_key)}, {self._expr(b['if'], name)})) return;")
        elif b.get("if"):
            lines.append(f"if (!({self._expr(b['if'], name)})) return;")
        for a in b.get("do", []):
            lines.append(self._action(a, name, per_second))
        body = "\n        ".join(lines) or "/* nothing */"
        return f"(self, dt, other, msg) => {{\n        {body}\n      }}"

    # ------------------------------------------------------------------ JS
    def _behaviours_js(self, ctx: BuildContext, ids: dict[str, str]) -> tuple[str, set[str]]:
        name, local, remote = self._resolver(ctx, ids)
        calls: set[str] = set()
        out = ["const BEHAVIOURS = {"]
        for e in ctx.entities:
            groups: dict[str, list[str]] = {}
            for b in e.behaviors or []:
                if not isinstance(b, dict):
                    continue
                on = str(b.get("on") or "update")
                kind, _, arg = on.partition(":")
                for a in b.get("do", []):
                    if isinstance(a, dict) and a.get("op") == "call":
                        calls.add(str(a.get("name") or "action"))
                cond = f"// on {on}" + (f" if {b['if']}" if b.get("if") else "")
                if kind == "collision" and b.get("with"):
                    # "when robot touches a cone": only contacts with that entity (ident) or kind count
                    w = str(b["with"]).strip().lower()
                    keys = [w] + [ids[x.id] for x in ctx.entities if (x.ident or "").lower() == w and ids[x.id] != w]
                    groups.setdefault(kind, []).append(f"{cond} with {w}\n      [{_js(keys)}, {self._fn(b, name, False)}]")
                elif kind in ("start", "update", "collision"):
                    # conditional per-frame rules without movement are edge-triggered (a "when" event);
                    # rules that move keep running every frame while the condition holds
                    moves = any(isinstance(a, dict) and a.get("op") == "move" for a in b.get("do", []))
                    edge = f"w{len(groups.get(kind, []))}" if kind == "update" and b.get("if") and not moves else None
                    fn = self._fn(b, name, kind == "update", edge)
                    groups.setdefault(kind, []).append(f"{cond}\n      {fn}")
                elif kind == "timer":
                    ms = max(1, int(_num(arg, 1000)))
                    groups.setdefault("timer", []).append(f"{cond}\n      [{ms}, {self._fn(b, name, False)}]")
                elif kind == "change":
                    if "." in arg:   # change:<entity>.<prop> watches another entity's property
                        ent, _, prop = arg.partition(".")
                        ent = ent.strip().lower()
                        if ent in ("self", "this", "it", "its", ids[e.id], (e.ident or "").lower()):
                            arg = prop
                        else:
                            ent = next((ids[x.id] for x in ctx.entities if (x.ident or "").lower() == ent), ent)
                            arg = f"{ent}.{prop}"
                    groups.setdefault("change", []).append(f"{cond}\n      [{_js(arg)}, {self._fn(b, name, False)}]")
                elif kind == "message":
                    key = arg.lower()
                    key = remote.get(key, local.get(key, key)) if key != "any" else "any"
                    groups.setdefault("message", []).append(f"{cond}\n      [{_js(key)}, {self._fn(b, name, False)}]")
                elif kind == "input":
                    key = arg.strip().lower()
                    k_id = key.replace(" ", "_")
                    if k_id in ("click", "mouse", "tap", "touch", "self"):
                        typ, key = "click", ids[e.id]
                    elif k_id in local and len(k_id) > 1:
                        typ, key = "click", local[k_id]
                    elif k_id in remote and len(k_id) > 1:
                        typ, key = "remote", remote[k_id]
                    else:
                        typ = "key"
                    groups.setdefault("input", []).append(f"{cond}\n      [{_js(typ)}, {_js(key)}, {self._fn(b, name, False)}]")
            if not groups:
                continue
            out.append(f"  {_js(ids[e.id])}: {{")
            for kind, items in groups.items():
                out.append(f"    {kind}: [")
                for it in items:
                    out.append("      " + it + ",")
                out.append("    ],")
            out.append("  },")
        out.append("};")
        return "\n".join(out), calls

    def _channels(self, ctx: BuildContext, ids: dict[str, str]) -> list[dict]:
        local = {e.id for e in ctx.entities}
        out = []
        for c in ctx.channels:
            try:
                s_local, d_local = c.src.id in local, c.dst.id in local
                if not (s_local or d_local):
                    continue
                direction = "local" if (s_local and d_local) else ("out" if s_local else "in")
                hub = str(c.config.get("hub_url") or "http://localhost:8765").rstrip("/")
                path = str(c.config.get("path") or f"/channel/{c.id}")
                out.append({
                    "id": c.id, "protocol": c.protocol, "dir": direction,
                    "src": ids[c.src.id] if s_local else c.src.ident, "src_name": c.src.name,
                    "src_kind": str(c.src.kind), "src_domain": c.src.domain,
                    "dst": ids[c.dst.id] if d_local else c.dst.ident, "dst_name": c.dst.name,
                    "dst_kind": str(c.dst.kind), "dst_domain": c.dst.domain,
                    "fields": list(c.fields or ["value"]),
                    "interval_ms": int(_num(c.config.get("interval_ms", 1000), 1000)),
                    "url": hub + path,
                    # serial devices reach the browser through the python serial->hub bridge
                    "pollable": c.protocol in PIPEABLE,
                })
            except Exception:
                continue
        return out

    def _defs(self, ctx: BuildContext, ids: dict[str, str]) -> list[dict]:
        out, auto = [], []
        n_auto = 0
        for e in ctx.entities:
            shape = self.shape_of(e)
            props = {}
            for k, v in (e.props or {}).items():
                if isinstance(v, (str, int, float, bool)) or v is None:
                    props[k] = v
                elif isinstance(v, (list, tuple)):
                    props[k] = [x if isinstance(x, (str, int, float, bool)) else str(x) for x in v]
                else:
                    props[k] = str(v)
            # placeholders the interpreter adds for names used in behaviours ("set color to red"
            # creates color=0 and red=0; "toggle visible" creates visible=False) are not real values
            acts = [a for b in (e.behaviors or []) if isinstance(b, dict) for a in (b.get("do") or [])
                    if isinstance(a, dict)]
            for k in [k for k, v in props.items() if k.lower() in COLORS and v in (0, False)]:
                del props[k]
            if props.get("color") in (0, False, None, ""):
                props.pop("color", None)
            if props.get("visible") in (0, False) and not isinstance(props.get("visible"), str) and any(
                    a.get("prop") == "visible" and a.get("op") in ("toggle", "set") for a in acts):
                props["visible"] = True
            # bare x / y / z props mean the position ("set its y to 0" creates a placeholder y=0)
            axes = {a: props.pop(a) for a in ("x", "y", "z") if a in props}
            real = {a: v for a, v in axes.items() if isinstance(v, (int, float)) and not isinstance(v, bool) and v != 0}
            if real:
                q = _vec(props.get("position"), [0, 0, 0])
                for a, v in real.items():
                    q["xyz".index(a)] = float(v)
                props["position"] = q
            rots = {k: props.pop(k) for k in list(props) if k.lower() in ROT_PROPS}
            if rots:   # fold angle props into the rotation vector (degrees)
                r = _vec(props.get("rotation"), [0, 0, 0])
                for k, v in rots.items():
                    if _num(v, 0):   # a 0 is usually the placeholder "rotate it by ..." creates
                        r[ROT_PROPS[k.lower()]] = _num(v, 0)
                props["rotation"] = r
            if "color" in props:
                props["color"] = color_hex(props["color"], DEFAULT_HEX.get(shape, "#8fa3bf"))
            # objects without a position would all overlap at the origin: lay them out in a row
            if "position" not in props and not e.parent and shape not in (
                    "light", "camera", "label", "group", "ground", "plane", "room"):
                props["position"] = [n_auto * 2.5, 0, 0]
                n_auto += 1
            out.append({"id": e.id, "ident": ids[e.id], "name": e.name, "kind": str(e.kind),
                        "shape": shape, "parent": ids.get(e.parent) if e.parent else None,
                        "props": props})
            if "position" not in (e.props or {}) and "position" in props:
                auto.append(out[-1])
        # a child without a position rests on top of its parent ("a sphere on the desk")
        by_ident = {d["ident"]: d for d in out}
        for e, d in zip(ctx.entities, out):
            par = by_ident.get(d["parent"]) if d["parent"] else None
            if par is not None and "position" not in d["props"] and d["shape"] not in ("light", "camera", "group"):
                d["props"]["position"] = [0, round(self._top(par) + self._rest_height(d), 3), 0]
        # lay the row out by width with a 1.5 m gap, centred on the origin (one object stays at 0)
        widths = [self._width(d) for d in auto]
        total = sum(widths) + 1.5 * max(0, len(auto) - 1)
        x = -total / 2
        for d, w in zip(auto, widths):
            d["props"]["position"] = [round(x + w / 2, 3), self._rest_height(d), 0]
            x += w + 1.5
        return out

    @staticmethod
    def _width(d: dict) -> float:
        """approximate x extent of an object (for the automatic layout)."""
        p, s = d["props"], d["shape"]
        try:
            if s in ("sphere", "cylinder", "cone", "tank"):
                return 2 * _num(p.get("radius"), 1.0 if s == "tank" else 0.5)
            if s == "torus":
                return 2 * (_num(p.get("radius"), 0.5) + _num(p.get("tube"), 0.15))
            if s == "pipe":
                return _num(p.get("length"), 4.0)
            if s == "arm":
                return 2.5
            dflt = [10, 6, 8] if s == "building" else [1, 1, 1]
            return max(0.1, _vec(p.get("size"), dflt, True)[0])
        except Exception:
            return 1.0

    @staticmethod
    def _top(d: dict) -> float:
        """height of a parent's top surface above its own origin."""
        p, s = d["props"], d["shape"]
        try:
            if s in ("box", "twin", "model"):
                return _vec(p.get("size"), [1, 1, 1], True)[1] / 2
            if s == "sphere":
                return _num(p.get("radius"), 0.5)
            if s in ("cylinder", "cone"):
                return _num(p.get("height"), 1.0) / 2
            if s == "tank":
                return _num(p.get("height"), 3.0)
            if s == "building":
                return _vec(p.get("size"), [10, 6, 8], True)[1]
            if s == "torus":
                return _num(p.get("tube"), 0.15)
        except Exception:
            pass
        return 0.0

    @staticmethod
    def _rest_height(d: dict) -> float:
        """y that puts a centred primitive on the ground grid (tank/building/arm are bottom-anchored)."""
        p, s = d["props"], d["shape"]
        try:
            if s in ("box", "twin", "model"):
                return round(_vec(p.get("size"), [1, 1, 1], True)[1] / 2, 3)
            if s == "sphere":
                return round(_num(p.get("radius"), 0.5), 3)
            if s in ("cylinder", "cone"):
                return round(_num(p.get("height"), 1.0) / 2, 3)
            if s == "torus":
                return round(_num(p.get("radius"), 0.5) + _num(p.get("tube"), 0.15), 3)
        except Exception:
            pass
        return 0

    def _user_functions(self, ctx: BuildContext, calls: set[str]) -> str:
        L = ["// called by 'call' actions; edit freely. self.builtin() knows reset/stop/open/close/jump/spawn.",
             "const USER_FUNCTIONS = {"]
        for c in sorted(calls):
            fn = re.sub(r"\W", "_", c) or "action"
            body = None
            if ctx.llm is not None:
                try:
                    body = ctx.llm.write_body(
                        "JavaScript function body (self is a scene object with get(prop), set(prop, value), "
                        "move(dx,dy,dz), log(text); args is an array)", c.replace("_", " "), "three.js scene object")
                except Exception:
                    body = None
            L.append(f"  {_js(fn)}: (self, ...args) => {{")
            if body and "</" not in body:
                L += ["    " + ln for ln in str(body).splitlines()]
            else:
                L.append(f"    return self.builtin({_js(fn)}, args);")
            L.append("  },")
        L.append("};")
        return "\n".join(L)

    def _scene_js(self, ctx: BuildContext) -> str:
        ids = self._idents(ctx)
        defs = self._defs(ctx, ids)
        chans = self._channels(ctx, ids)
        beh, calls = self._behaviours_js(ctx, ids)
        need_gltf = any(d["shape"] == "model" and d["props"].get("url") for d in defs)
        runtime = (ASSET_DIR / "runtime.js").read_text(encoding="utf-8")
        L = [f"// {ctx.system.name} - 3D scene generated by NLCL (three.js r{THREE_VERSION.split('.')[1]}). Edit freely.",
             "// Coordinates: Y is up, units are metres, rotations are in degrees.",
             "import * as THREE from 'three';",
             "import { OrbitControls } from 'three/addons/controls/OrbitControls.js';"]
        if need_gltf:
            L.append("import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';")
        L += ["",
              f"const PROJECT = {_js(ctx.system.name)};",
              "const HUB_POLL_MS = 500;  // how often incoming hub channels are polled",
              "",
              "// one entry per entity: shape decides the geometry, props drive the visuals",
              "const DEFS = " + _js_list(defs) + ";",
              "",
              "// cross-module channels (dir: in = we receive, out = we send, local = both ends here)",
              "const CHANNELS = " + _js_list(chans) + ";",
              "",
              "// behaviours: (self, dt, other, msg) => {...}; self.get/set/move/log/send/destroy/call",
              beh, "",
              self._user_functions(ctx, calls), "",
              runtime]
        return "\n".join(L)

    # ------------------------------------------------------------------ HTML
    def _html(self, ctx: BuildContext, body_script: str, src: str | None = None) -> str:
        importmap = json.dumps({"imports": {"three": THREE_URL, "three/addons/": ADDONS_URL}}, indent=2)
        title = f"{ctx.system.name} - 3D".replace("<", "&lt;").replace(">", "&gt;")
        # "</script" anywhere in the inlined code (e.g. inside a string) would end the element early
        body_script = re.sub(r"</(script)", r"<\\/\1", body_script, flags=re.IGNORECASE)
        script = (f'<script type="module" src="{src}"></script>' if src
                  else f'<script type="module">\n{body_script}\n</script>')
        return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  html, body {{ margin: 0; height: 100%; overflow: hidden; background: #1f242d; font: 13px system-ui, sans-serif; color: #e8edf3; }}
  canvas {{ display: block; }}
  #hud {{ position: fixed; top: 10px; left: 10px; max-height: 60vh; overflow: auto; min-width: 180px;
         background: rgba(18,22,30,.78); border: 1px solid #3a4352; border-radius: 8px; padding: 8px 10px; }}
  #hud .ent {{ margin-top: 6px; }} #hud .nm {{ font-weight: 600; color: #fff; }} #hud .kd {{ color: #8fa0b5; font-size: 11px; }}
  #hud .pr {{ padding-left: 10px; color: #c5cfdb; }} #hud .live {{ color: #7ee2a8; }}
  #log {{ position: fixed; left: 10px; bottom: 10px; max-width: 55vw; font: 12px ui-monospace, monospace;
         background: rgba(18,22,30,.7); border-radius: 8px; padding: 6px 10px; color: #cfd8e3; }}
  #log:empty {{ display: none; }}
  #badge {{ position: fixed; top: 10px; right: 10px; padding: 5px 10px; border-radius: 12px; font-weight: 600; }}
  #badge.on {{ background: #1f6f43; }} #badge.off {{ background: #8a2d2d; }} #badge.wait {{ background: #806016; }}
  #help {{ position: fixed; right: 10px; bottom: 10px; color: #8fa0b5; font-size: 11px; }}
</style>
<script type="importmap">
{importmap}
</script>
</head>
<body>
<div id="hud"></div>
<div id="log"></div>
<div id="badge"></div>
<div id="help">drag: orbit &middot; right-drag: pan &middot; wheel: zoom &middot; click objects / keys trigger behaviours</div>
<noscript>This page needs JavaScript (three.js).</noscript>
{script}
</body>
</html>
"""

    # ------------------------------------------------------------------ Blender
    def _blender(self, ctx: BuildContext) -> str:
        ids = self._idents(ctx)
        items = []
        for d in self._defs(ctx, ids):
            p = d["props"]
            shape = d["shape"]
            rad = _num(p.get("radius"), _num(p.get("size"), 1.0) / 2 if isinstance(p.get("size"), (int, float)) else 0.5)
            item = {
                "name": d["name"], "ident": d["ident"], "shape": shape, "parent": d["parent"],
                "position": _vec(p.get("position"), [0, 0, 0]),
                "rotation": _vec(p.get("rotation"), [0, 0, 0]),
                "scale": _vec(p.get("scale"), [1, 1, 1], True),
                "color": color_hex(p.get("color"), DEFAULT_HEX.get(shape, "#8fa3bf")),
                "opacity": max(0.0, min(1.0, _num(p.get("opacity"), 1.0))),
                "visible": p.get("visible") not in (False, 0, "false"),
                "radius": rad,
                "height": _num(p.get("height"), _vec(p.get("size"), [1, 1, 1], True)[1] if isinstance(p.get("size"), list) else 1.0),
                "tube": _num(p.get("tube"), rad * 0.3),
                "length": _num(p.get("length"), 4.0),
                "text": str(p.get("text") or d["name"]),
                "subtype": str(p.get("subtype") or "point").lower(),
                "intensity": _num(p.get("intensity"), 1.0),
                "fov": _num(p.get("fov"), 50.0),
                "target": _vec(p.get("target"), [0, 0, 0]),
                "url": str(p.get("url") or ""),
                "level": max(0.0, min(100.0, _num(p.get("level"), 50.0))),
                "joints": [_num(p.get("base_angle"), 0), _num(p.get("shoulder"), 30), _num(p.get("elbow"), 45)],
            }
            defaults = {"ground": [20, 0, 20], "plane": [4, 0, 4], "room": [10, 3, 8], "building": [10, 6, 8]}
            item["size"] = _vec(p.get("size"), defaults.get(shape, [1, 1, 1]), True)
            if shape in ("plane", "ground", "room") and isinstance(p.get("size"), list) and len(p["size"]) == 2:
                item["size"] = [_num(p["size"][0], 1), 0.0, _num(p["size"][1], 1)]
            if shape == "tank":
                item["radius"] = _num(p.get("radius"), 1.0)
                item["height"] = _num(p.get("height"), 3.0)
            if shape == "pipe":
                item["radius"] = _num(p.get("radius"), 0.15)
            items.append(item)
        # a Python literal (json.dumps would emit true/false/null, which are NameErrors in Python)
        data = pprint.pformat(items, width=110, sort_dicts=False)
        return BLENDER_TEMPLATE.replace("@@PROJECT@@", repr(ctx.system.name)).replace("@@ITEMS@@", data)

    # ------------------------------------------------------------------ README
    def _readme(self, ctx: BuildContext, defs: list[dict], chans: list[dict]) -> str:
        L = [f"# {ctx.system.name} - 3D scene (three.js + Blender)", "",
             "Generated by NLCL. Everything here runs with free tools.", "",
             "## View it", "",
             "1. **Double-click `index.html`** (Chrome, Edge or Firefox). The scene code is inlined, and three.js "
             f"r{THREE_VERSION.split('.')[1]} is loaded from unpkg through an importmap, so the first load needs internet.",
             "2. Or serve the folder (recommended when talking to the hub, and required for `dev.html`, which loads "
             "`scene.js` as a separate ES module - browsers block module imports from `file://`):", "",
             "   ```", "   python serve.py            # http://localhost:8000/  (dev.html uses scene.js)", "   ```", "",
             "`scene.js` and the script inside `index.html` are the same code: edit `scene.js` and open `dev.html` "
             "through `serve.py`, or rebuild with NLCL to refresh `index.html`.", "",
             "Controls: left-drag orbit, right-drag pan, wheel zoom. Objects with `movable: true` follow WASD/arrow keys "
             "at `speed` units/s. Clicking an object fires `input:<object>` behaviours.", "",
             "## Objects", "", "| name | kind | shape | parent | props |", "|---|---|---|---|---|"]
        names = {d["ident"]: d["name"] for d in defs}
        for d in defs:
            props = ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in d["props"].items())
            L.append(f"| {d['name']} | {d['kind']} | {d['shape']} | {names.get(d['parent'], '') if d['parent'] else ''} | {props} |")
        beh = [(e, b) for e in ctx.entities for b in (e.behaviors or []) if isinstance(b, dict)]
        if beh:
            L += ["", "## Behaviours", ""]
            for e, b in beh:
                acts = "; ".join(describe_action(a) for a in b.get("do", []) if isinstance(a, dict) and "op" in a)
                L.append(f"- **{e.name}** on `{b.get('on')}`" + (f" if `{b['if']}`" if b.get("if") else "") + f": {acts}")
            L += ["", "`update` runs every frame (moves are per second; a conditional rule without a move acts once each "
                  "time its condition becomes true); `timer:<ms>` uses real-time accumulators; "
                  "`change:<prop>` fires when a prop actually changes; `collision` uses bounding-box (Box3) overlap "
                  "and fires once per contact; `destroy` removes the mesh."]
        L += ["", "## Live data", ""]
        if chans:
            L += ["| channel | from | to | protocol | direction | fields | URL |", "|---|---|---|---|---|---|---|"]
            for c in chans:
                L.append(f"| {c['id']} | {c['src_name']} ({c['src_domain']}) | {c['dst_name']} ({c['dst_domain']}) | "
                         f"{c['protocol']} | {c['dir']} | {', '.join(c['fields'])} | {c['url'] if c['pollable'] else '-'} |")
            L += ["",
                  "Incoming channels are polled from the NLCL hub every 500 ms (`GET <hub>/channel/<id>`); outgoing ones "
                  "are POSTed there every `interval_ms`. Start the hub with `python ../python/main.py` (it is generated "
                  "next to this folder). Serial devices reach the browser through that hub's serial bridge.",
                  "",
                  "When the hub is unreachable (or a sender is silent) the badge shows **hub offline - simulated** and "
                  "plausible values are generated, so the scene always moves.",
                  "",
                  "Received fields are copied onto the destination object's props and drive the visuals:",
                  "- a received `temperature` maps blue (10 C) to red (40 C) and overrides the static `color`;",
                  "- `on` (or the first boolean field received) makes the object glow; a light with `on: false` switches off;",
                  "- received `level` / `value` / `distance` stretch the object vertically (`level` 0-100 fills a tank)."]
            if any(not c["pollable"] for c in chans):
                L += ["", "Note: MQTT/UDP/bus channels cannot be reached from a browser directly; they are simulated here. "
                      "Route them through the python module (http) for live data."]
        else:
            L.append("No channels. Connect an object to another module (e.g. `connect the sensor to the tank via http`).")
        L += ["", "## Blender (optional, free)", "",
              "`blender_scene.py` rebuilds the static scene (primitives, transforms, colours as materials). "
              "three.js is Y-up, Blender Z-up: the script converts (x, y, z) -> (x, -z, y).", "",
              "```",
              "blender --python blender_scene.py                                   # open interactively",
              "blender --background --python blender_scene.py -- --save scene.blend --gltf scene.glb",
              "```", "",
              "`scene.glb` produced this way can be loaded back into three.js with a `model` object (`url`)."]
        return "\n".join(L) + "\n"

    # ------------------------------------------------------------------ build
    def generate(self, ctx: BuildContext) -> dict[str, str]:
        js = self._scene_js(ctx)
        ids = self._idents(ctx)
        files = {
            "scene.js": js,
            "index.html": self._html(ctx, js),
            "dev.html": self._html(ctx, "", src="./scene.js"),
            "serve.py": SERVE_PY,
            "blender_scene.py": self._blender(ctx),
            "README.md": self._readme(ctx, self._defs(ctx, ids), self._channels(ctx, ids)),
        }
        return files

    # ------------------------------------------------------------------ run
    def run(self, ctx: BuildContext) -> RunResult | None:
        scene = ctx.outdir / "scene.js"
        index = ctx.outdir / "index.html"
        if not scene.exists():
            return RunResult(False, "build first (3d/scene.js missing)")
        out, ok, cmds = [], True, []
        node = self.which("node")
        if node:
            with tempfile.TemporaryDirectory() as td:
                mjs = Path(td) / "scene.mjs"   # .mjs: node parses it as an ES module
                mjs.write_text(scene.read_text(encoding="utf-8"), encoding="utf-8")
                r = self.sh([node, "--check", str(mjs)], ctx.outdir, timeout=30)
                cmds.append("node --check scene.mjs")
                ok &= r.ok
                out.append("scene.js syntax: " + ("OK" if r.ok else "ERROR\n" + r.output.replace(str(mjs), "scene.js")))
        else:
            out.append("node not found: skipped the JavaScript syntax check")
        for f in ("blender_scene.py", "serve.py"):
            r = self.sh([sys.executable, "-c", "import sys; compile(open(sys.argv[1], encoding='utf-8').read(), sys.argv[1], 'exec')", f],
                        ctx.outdir, timeout=30)
            ok &= r.ok
            out.append(f"{f} syntax: " + ("OK" if r.ok else "ERROR\n" + r.output))
        blender = self.which("blender")
        if blender:
            r = self.sh([blender, "--background", "--factory-startup", "--python", "blender_scene.py"], ctx.outdir, timeout=120)
            ok &= r.ok
            out.append("blender: " + ("scene built OK" if r.ok else "ERROR\n" + r.output[-1500:]))
        out.append(f"open in a browser: {index.resolve().as_uri()}")
        out.append(f"or: python {ctx.outdir / 'serve.py'}  ->  http://localhost:8000/")
        return RunResult(ok, "\n".join(out), " && ".join(cmds))


SERVE_PY = '''#!/usr/bin/env python3
"""Serve this folder on http://localhost:8000 (stdlib only) and open the scene.

Usage: python serve.py [port]
"""
import functools
import http.server
import socketserver
import sys
import threading
import webbrowser
from pathlib import Path

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
HERE = Path(__file__).resolve().parent


class Handler(http.server.SimpleHTTPRequestHandler):
    extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map,
                      ".js": "text/javascript", ".mjs": "text/javascript", ".glb": "model/gltf-binary",
                      ".gltf": "model/gltf+json"}

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


if __name__ == "__main__":
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("127.0.0.1", PORT), functools.partial(Handler, directory=str(HERE))) as httpd:
        url = f"http://localhost:{PORT}/dev.html"
        print(f"serving {HERE} at {url}  (index.html also works)  Ctrl+C to stop", flush=True)
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
'''

BLENDER_TEMPLATE = '''"""Recreate the NLCL 3D scene @@PROJECT@@ in Blender (free: https://www.blender.org).

  blender --python blender_scene.py
  blender --background --python blender_scene.py -- --save scene.blend --gltf scene.glb

Objects created by a previous run (custom property "nlcl") are replaced.
three.js is Y-up, Blender is Z-up: (x, y, z) -> (x, -z, y). Rotations are in degrees.
"""
import math
import sys

import bpy

ITEMS = @@ITEMS@@


def to_blender(v):
    """three.js (x, y, z) Y-up -> Blender (x, -z, y) Z-up."""
    return (v[0], -v[2], v[1])


def material(name, hex_color, alpha=1.0, emission=0.0):
    h = hex_color.lstrip("#")
    rgb = [int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]  # sRGB -> linear
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    if bsdf:
        bsdf.inputs["Base Color"].default_value = (*lin, 1.0)
        bsdf.inputs["Alpha"].default_value = alpha
        if emission and "Emission Strength" in bsdf.inputs:
            bsdf.inputs["Emission Strength"].default_value = emission
    mat.diffuse_color = (*lin, alpha)
    if alpha < 1.0:
        try:
            mat.blend_method = "BLEND"
        except (AttributeError, TypeError):
            pass
    return mat


def tag(obj, item, mat=None):
    obj["nlcl"] = item["ident"]
    if mat is not None and getattr(obj, "data", None) is not None and hasattr(obj.data, "materials"):
        obj.data.materials.clear()
        obj.data.materials.append(mat)
    return obj


def empty(name):
    obj = bpy.data.objects.new(name, None)
    bpy.context.collection.objects.link(obj)
    return obj


def child(obj, parent, loc=(0, 0, 0)):
    obj.parent = parent
    obj.location = loc
    return obj


def wrap(name, mesh, item, mat):
    """Put a scaled/rotated primitive under an unscaled empty, like three.js (geometry carries the size,
    the node only the user's transform), so child objects are not stretched by the parent's size."""
    root = empty(name)
    tag(mesh, item, mat)
    mesh.name = name + "_mesh"
    child(mesh, root)
    return root


def build(item):
    """Create the Blender object(s) for one item; returns the root object."""
    s, name = item["shape"], item["name"]
    sx, sy, sz = item["size"]                      # three.js size: width, height, depth
    mat = material(name + "_mat", item["color"], item["opacity"])
    ops = bpy.ops
    if s == "box":
        ops.mesh.primitive_cube_add(size=1)
        bpy.context.active_object.scale = (sx, sz, sy)
        root = wrap(name, bpy.context.active_object, item, mat)
    elif s == "sphere":
        ops.mesh.primitive_uv_sphere_add(radius=item["radius"], segments=32, ring_count=16)
        root = bpy.context.active_object
    elif s == "cylinder":
        ops.mesh.primitive_cylinder_add(radius=item["radius"], depth=item["height"], vertices=32)
        root = bpy.context.active_object
    elif s == "cone":
        ops.mesh.primitive_cone_add(radius1=item["radius"], radius2=0, depth=item["height"], vertices=32)
        root = bpy.context.active_object
    elif s == "torus":
        ops.mesh.primitive_torus_add(major_radius=item["radius"], minor_radius=item["tube"])
        bpy.context.active_object.rotation_euler = (math.pi / 2, 0, 0)       # three.js torus faces +Z
        root = wrap(name, bpy.context.active_object, item, mat)
    elif s in ("plane", "ground"):
        ops.mesh.primitive_plane_add(size=1)
        bpy.context.active_object.scale = (sx, sz, 1)
        root = wrap(name, bpy.context.active_object, item, mat)
    elif s == "room":
        root = empty(name)
        ops.mesh.primitive_plane_add(size=1)
        floor = tag(bpy.context.active_object, item, mat)
        floor.name = name + "_floor"
        floor.scale = (sx, sz, 1)
        child(floor, root)
        return root
    elif s == "building":
        root = empty(name)
        ops.mesh.primitive_cube_add(size=1)
        body = tag(bpy.context.active_object, item, mat)
        body.name = name + "_body"
        body.scale = (sx, sz, sy)
        child(body, root, (0, 0, sy / 2))
        return root
    elif s == "tank":
        r, h = item["radius"], item["height"]
        root = empty(name)
        ops.mesh.primitive_cylinder_add(radius=r, depth=h, vertices=40)
        shell = tag(bpy.context.active_object, item, material(name + "_shell", "#9aa7b5", 0.3))
        shell.name = name + "_shell"
        child(shell, root, (0, 0, h / 2))
        lh = max(0.01, h * item["level"] / 100.0)
        ops.mesh.primitive_cylinder_add(radius=r * 0.92, depth=lh, vertices=40)
        liquid = tag(bpy.context.active_object, item, material(name + "_liquid", item["color"] if item["color"] != "#9aa7b5" else "#2f7fd8"))
        liquid.name = name + "_liquid"
        child(liquid, root, (0, 0, lh / 2))
        return root
    elif s == "pipe":
        ops.mesh.primitive_cylinder_add(radius=item["radius"], depth=item["length"], vertices=20)
        bpy.context.active_object.rotation_euler = (0, math.pi / 2, 0)        # along the X axis
        root = wrap(name, bpy.context.active_object, item, mat)
    elif s == "arm":
        yaw, shoulder, elbow = (math.radians(a) for a in item["joints"])
        root = empty(name)
        ops.mesh.primitive_cylinder_add(radius=0.5, depth=0.3, vertices=32)
        child(tag(bpy.context.active_object, item, material(name + "_base", "#3b4048")), root, (0, 0, 0.15))
        j0 = child(empty(name + "_yaw"), root, (0, 0, 0.3))
        j0.rotation_euler = (0, 0, yaw)
        j1 = child(empty(name + "_shoulder"), j0, (0, 0, 0.3))
        j1.rotation_euler = (0, shoulder, 0)
        ops.mesh.primitive_cube_add(size=1)
        upper = child(tag(bpy.context.active_object, item, mat), j1, (0, 0, 0.6))
        upper.scale = (0.22, 0.22, 1.2)
        j2 = child(empty(name + "_elbow"), j1, (0, 0, 1.2))
        j2.rotation_euler = (0, elbow, 0)
        ops.mesh.primitive_cube_add(size=1)
        lower = child(tag(bpy.context.active_object, item, mat), j2, (0, 0, 0.5))
        lower.scale = (0.18, 0.18, 1.0)
        for o in (j0, j1, j2):
            o["nlcl"] = item["ident"]
        return root
    elif s == "light":
        sub = item["subtype"]
        kind = "SUN" if ("sun" in sub or "direction" in sub) else "SPOT" if "spot" in sub else "POINT"
        if "ambient" in sub:
            world = bpy.context.scene.world or bpy.data.worlds.new("World")
            bpy.context.scene.world = world
            world.color = (0.05, 0.05, 0.06)
            return None
        data = bpy.data.lights.new(name, kind)
        data.energy = item["intensity"] * (3.0 if kind == "SUN" else 800.0)
        h = item["color"].lstrip("#")
        data.color = tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
        root = bpy.data.objects.new(name, data)
        bpy.context.collection.objects.link(root)
    elif s == "camera":
        data = bpy.data.cameras.new(name)
        data.angle = math.radians(item["fov"])
        root = bpy.data.objects.new(name, data)
        bpy.context.collection.objects.link(root)
        target = empty(name + "_target")
        target.location = to_blender(item["target"])
        target["nlcl"] = item["ident"]
        c = root.constraints.new("TRACK_TO")
        c.target = target
        c.track_axis = "TRACK_NEGATIVE_Z"
        c.up_axis = "UP_Y"
        bpy.context.scene.camera = root
    elif s == "label":
        ops.object.text_add()
        root = bpy.context.active_object
        root.data.body = item["text"]
        root.data.align_x = "CENTER"
        bpy.context.active_object.rotation_euler = (math.pi / 2, 0, 0)        # stand upright facing -Y (three.js +Z)
        root = wrap(name, bpy.context.active_object, item, mat)
    elif s == "model" and item["url"].lower().endswith((".gltf", ".glb")):
        try:
            before = set(bpy.data.objects)
            ops.import_scene.gltf(filepath=item["url"])
            new = [o for o in bpy.data.objects if o not in before]
            root = empty(name)
            for o in new:
                if o.parent is None:
                    o.parent = root
                o["nlcl"] = item["ident"]
        except Exception as e:  # missing file: placeholder cube
            print(f"[nlcl] could not import {item['url']}: {e}")
            ops.mesh.primitive_cube_add(size=1)
            bpy.context.active_object.scale = (sx, sz, sy)
            root = wrap(name, bpy.context.active_object, item, mat)
    elif s == "group":
        root = empty(name)
    else:  # object / twin / model without url / anything unknown: a box
        ops.mesh.primitive_cube_add(size=1)
        bpy.context.active_object.scale = (sx, sz, sy)
        root = wrap(name, bpy.context.active_object, item, mat)
    root.name = name
    return tag(root, item, mat)


def main():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    if bpy.context.object and bpy.context.object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    # remove objects from a previous run (and Blender's default cube/light/camera in a fresh file)
    for o in list(bpy.data.objects):
        if "nlcl" in o or o.name in ("Cube", "Light", "Camera"):
            bpy.data.objects.remove(o, do_unlink=True)
    made = {}
    for item in ITEMS:
        obj = build(item)
        if obj is None:
            continue
        made[item["ident"]] = (obj, item)
    for ident, (obj, item) in made.items():
        if item["parent"] in made:
            obj.parent = made[item["parent"]][0]      # location stays relative to the parent
        obj.location = to_blender(item["position"])
        rx, ry, rz = (math.radians(a) for a in item["rotation"])
        base = obj.rotation_euler
        obj.rotation_euler = (base[0] + rx, base[1] - rz, base[2] + ry)
        k = item["scale"]
        obj.scale = (obj.scale[0] * k[0], obj.scale[1] * k[2], obj.scale[2] * k[1])
        if not item["visible"]:   # hide the parts built for this item too (wrapped meshes, tank shell ...)
            stack = [obj]
            while stack:
                o = stack.pop()
                if o.get("nlcl") == item["ident"] or o is obj:
                    o.hide_viewport = o.hide_render = True
                    stack.extend(o.children)
    if not any(i["shape"] == "light" for i in ITEMS):
        data = bpy.data.lights.new("nlcl_sun", "SUN")
        data.energy = 3.0
        sun = bpy.data.objects.new("nlcl_sun", data)
        sun["nlcl"] = "sun"
        sun.rotation_euler = (math.radians(45), 0, math.radians(30))
        bpy.context.collection.objects.link(sun)
    print(f"[nlcl] built {len(made)} objects for " + @@PROJECT@@)
    if "--save" in argv:
        path = argv[argv.index("--save") + 1]
        bpy.ops.wm.save_as_mainfile(filepath=bpy.path.abspath(path) if path.startswith("//") else path)
        print("[nlcl] saved", path)
    if "--gltf" in argv:
        path = argv[argv.index("--gltf") + 1]
        bpy.ops.export_scene.gltf(filepath=path)
        print("[nlcl] exported", path)


main()
'''


MODULE = Scene3DModule()
