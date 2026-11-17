"""CAD domain: parametric, 3D-printable parts as OpenSCAD (+ CadQuery) source.

Every root entity becomes ``cad/<ident>.scad`` (parameters as top-level
variables named ``<ident>_<prop>`` so they can be overridden with
``openscad -D``), children of a part become features: holes / cutouts /
ports are subtracted, other parts are unioned.  ``cad/assembly.scad`` places
every root part at its position/rotation.  ``cad/<ident>_cadquery.py`` is a
free CadQuery alternative and ``cad/cad_bridge.py`` connects parts to NLCL
channels (live parameters in, part parameters out).

Geometry is millimetres.  Every part is centred on X/Y and sits on Z=0 (the
print bed) unless stated otherwise.
"""
from __future__ import annotations

import json
import math
import pprint
import re

from ..ir import Entity, describe_action, ident
from .base import BuildContext, Channel, DomainModule, RunResult

# ----------------------------------------------------------------------------
# reference data
# ----------------------------------------------------------------------------
# Common boards: pcb length (x) / width (y), tallest component above the pcb,
# mounting holes (from the lower-left pcb corner), mounting hole diameter and
# connector cut-outs: (label, side, offset along the side from the pcb corner,
# width, height).  Dimensions are approximate - verify against your board.
BOARDS = {
    "uno": dict(title="Arduino Uno", l=68.6, w=53.4, comp=12.0, hole_d=3.2,
                holes=[(14.0, 2.5), (15.3, 50.7), (66.1, 7.6), (66.1, 35.5)],
                ports=[("usb-b", "left", 38.1, 12.5, 11.5), ("dc jack", "left", 7.6, 9.5, 11.5)]),
    "mega": dict(title="Arduino Mega 2560", l=101.6, w=53.3, comp=12.0, hole_d=3.2,
                 holes=[(14.0, 2.5), (15.3, 50.7), (66.1, 7.6), (66.1, 35.5), (90.2, 50.7), (96.5, 2.5)],
                 ports=[("usb-b", "left", 38.1, 12.5, 11.5), ("dc jack", "left", 7.6, 9.5, 11.5)]),
    "nano": dict(title="Arduino Nano", l=45.0, w=18.0, comp=8.0, hole_d=0, holes=[],
                 ports=[("usb", "left", 9.0, 9.0, 5.0)]),
    "esp32": dict(title="ESP32 DevKit", l=55.0, w=28.0, comp=8.0, hole_d=0, holes=[],
                  ports=[("usb", "left", 14.0, 10.0, 5.0)]),
    "esp8266": dict(title="ESP8266 NodeMCU", l=49.0, w=26.0, comp=8.0, hole_d=3.0,
                    holes=[(2.5, 2.5), (46.5, 2.5), (2.5, 23.5), (46.5, 23.5)],
                    ports=[("usb", "left", 13.0, 10.0, 5.0)]),
    "pico": dict(title="Raspberry Pi Pico", l=51.0, w=21.0, comp=4.0, hole_d=2.1,
                 holes=[(2.0, 4.8), (2.0, 16.2), (49.0, 4.8), (49.0, 16.2)],
                 ports=[("usb", "left", 10.5, 9.0, 4.5)]),
    "rpi4": dict(title="Raspberry Pi 4", l=85.0, w=56.0, comp=16.0, hole_d=2.75,
                 holes=[(3.5, 3.5), (61.5, 3.5), (3.5, 52.5), (61.5, 52.5)],
                 ports=[("usb-c power", "front", 11.2, 10.0, 4.5), ("micro hdmi 0", "front", 26.0, 8.0, 4.0),
                        ("micro hdmi 1", "front", 39.5, 8.0, 4.0), ("usb a 1", "right", 9.0, 14.5, 16.0),
                        ("usb a 2", "right", 27.0, 14.5, 16.0), ("ethernet", "right", 45.75, 16.5, 14.0)]),
    "pizero": dict(title="Raspberry Pi Zero", l=65.0, w=30.0, comp=5.0, hole_d=2.75,
                   holes=[(3.5, 3.5), (61.5, 3.5), (3.5, 26.5), (61.5, 26.5)],
                   ports=[("mini hdmi", "front", 12.4, 12.0, 4.5), ("micro usb", "front", 41.4, 9.0, 4.0),
                          ("micro usb power", "front", 54.0, 9.0, 4.0)]),
}
# normalised text fragment -> board key (checked in order)
BOARD_MATCH = [("pi4", "rpi4"), ("raspberrypi4", "rpi4"), ("rpi4", "rpi4"), ("pizero", "pizero"),
               ("rpizero", "pizero"), ("pico", "pico"), ("mega", "mega"), ("nano", "nano"),
               ("uno", "uno"), ("esp8266", "esp8266"), ("nodemcu", "esp8266"), ("wemos", "esp8266"),
               ("esp32", "esp32"), ("esp", "esp32"), ("raspberrypi", "rpi4"), ("rpi", "rpi4"),
               ("arduino", "uno")]

# child features that are *subtracted* from their parent.
# shape: round (d) | rect (w x h) | slots ; side: default face
FEATURES = {
    "usb_c": dict(shape="rect", w=10.0, h=4.5, side="left"),
    "usb": dict(shape="rect", w=12.0, h=7.0, side="left"),
    "hdmi": dict(shape="rect", w=16.0, h=7.0, side="back"),
    "ethernet": dict(shape="rect", w=17.0, h=14.0, side="back"),
    "button": dict(shape="round", d=12.0, side="top"),
    "switch": dict(shape="rect", w=13.0, h=9.0, side="front"),
    "led": dict(shape="round", d=5.2, side="top"),
    "lcd": dict(shape="rect", w=72.0, h=25.0, side="top"),
    "oled": dict(shape="rect", w=27.0, h=16.0, side="top"),
    "display": dict(shape="rect", w=27.0, h=16.0, side="top"),
    "screen": dict(shape="rect", w=27.0, h=16.0, side="top"),
    "jack": dict(shape="round", d=8.0, side="back"),
    "barrel": dict(shape="round", d=8.0, side="back"),
    "power": dict(shape="round", d=8.0, side="back"),
    "cable": dict(shape="round", d=6.0, side="back"),
    "wire": dict(shape="round", d=6.0, side="back"),
    "antenna": dict(shape="round", d=6.5, side="back"),
    "potentiometer": dict(shape="round", d=7.0, side="front"),
    "pot": dict(shape="round", d=7.0, side="front"),
    "encoder": dict(shape="round", d=7.0, side="top"),
    "buzzer": dict(shape="round", d=12.0, side="top"),
    "speaker": dict(shape="round", d=20.0, side="top"),
    "sensor": dict(shape="round", d=10.0, side="top"),
    "fan": dict(shape="round", d=28.0, side="top"),
    "vent": dict(shape="slots", side="front"),
    "grille": dict(shape="slots", side="front"),
    "slot": dict(shape="rect", w=20.0, h=2.0, side="top"),
    "window": dict(shape="rect", w=20.0, h=10.0, side="front"),
    "opening": dict(shape="rect", w=10.0, h=6.0, side="front"),
    "port": dict(shape="rect", w=10.0, h=6.0, side="front"),
    "cutout": dict(shape="rect", w=10.0, h=6.0, side="front"),
    "hole": dict(shape="round", d=3.2, side="top"),
    "bore": dict(shape="round", d=5.0, side="top"),
}

# words that map onto an implemented geometry ("canonical kind")
KIND_MAP = {
    "object": "box", "part": "box", "box": "box", "block": "box", "cuboid": "box", "brick": "box",
    "cube": "cube",
    "cylinder": "cylinder", "rod": "cylinder", "disc": "cylinder", "disk": "cylinder", "puck": "cylinder",
    "peg": "cylinder", "shaft": "cylinder", "axle": "cylinder",
    "sphere": "sphere", "ball": "sphere",
    "cone": "cone",
    "plate": "plate", "panel": "plate", "baseplate": "plate", "sheet": "plate", "faceplate": "plate",
    "enclosure": "enclosure", "case": "enclosure", "housing": "enclosure", "casing": "enclosure",
    "shell": "enclosure",
    "bracket": "bracket", "angle": "bracket",
    "standoff": "standoff", "boss": "standoff",
    "spacer": "spacer", "bushing": "spacer",
    "washer": "washer",
    "gear": "gear", "cog": "gear", "sprocket": "gear", "pinion": "gear",
    "tube": "tube", "pipe": "tube", "ring": "tube", "sleeve": "tube",
    "knob": "knob", "dial": "knob",
    "hinge": "hinge",
    "mount": "mount", "holder": "mount", "clip": "mount", "clamp": "mount", "cradle": "mount",
}
GEOMETRY_KINDS = {"box", "cube", "cylinder", "sphere", "cone", "plate", "enclosure", "bracket", "standoff",
                  "spacer", "washer", "gear", "tube", "knob", "hinge", "mount"}
PALETTE = ["SteelBlue", "Orange", "SeaGreen", "Tomato", "SlateGray", "Gold", "Orchid", "Peru"]
GENERIC_FEATURES = {"hole", "cutout", "opening", "port", "slot", "window", "bore"}
PROP_ALIASES = {"holes": "hole_count", "number_of_holes": "hole_count", "hole_number": "hole_count",
                "num_holes": "hole_count", "outer_diameter": "diameter", "outside_diameter": "diameter",
                "inner_diameter": "hole_diameter", "inside_diameter": "hole_diameter",
                "number_of_teeth": "teeth", "tooth_count": "teeth", "teeth_count": "teeth",
                "thick": "thickness", "tall": "height", "long": "length", "wide": "width"}

SIDE_WORDS = {"left": "left", "right": "right", "front": "front", "back": "back", "rear": "back",
              "top": "top", "lid": "top", "bottom": "bottom", "floor": "bottom", "base": "bottom",
              "-x": "left", "+x": "right", "-y": "front", "+y": "back", "+z": "top", "-z": "bottom"}


# ----------------------------------------------------------------------------
# small value helpers (never raise)
# ----------------------------------------------------------------------------
def _num(v, default=None):
    """Coerce a prop value to float; None/garbage -> default."""
    if isinstance(v, bool) or v is None:
        return default
    if isinstance(v, (int, float)):
        return float(v) if math.isfinite(v) else default
    if isinstance(v, (list, tuple)):
        return _num(v[0], default) if v else default
    m = re.search(r"-?\d+(?:\.\d+)?", str(v))
    return float(m.group(0)) if m else default


def _vec(v, n=3):
    """Coerce size-like values ([80,50,30], 80, '80x50x30') to a list of floats (may be shorter than n)."""
    if v is None or isinstance(v, bool):
        return []
    if isinstance(v, (int, float)):
        return [float(v)]
    if isinstance(v, (list, tuple)):
        out = [_num(x) for x in v]
        return [x for x in out if x is not None][:n]
    return [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", str(v))][:n]


def _truthy(v, default=False):
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    return str(v).strip().lower() in ("true", "yes", "on", "1", "y", "enabled")


def _n(x) -> str:
    """Number literal for OpenSCAD / Python."""
    try:
        x = float(x)
    except (TypeError, ValueError):
        return "0"
    if not math.isfinite(x):
        return "0"
    x = round(x, 4)
    if x == int(x):
        return str(int(x))
    return repr(x)


def _pos(e: Entity, default=(0.0, 0.0, 0.0)) -> list[float]:
    p = _vec(e.props.get("position"))
    if not p:
        return list(default)
    return (p + list(default)[len(p):])[:3]


def _rot(e: Entity) -> list[float]:
    r = None
    for k in ("rotation", "rotate", "angle", "rotate_by", "rotated_by", "rotated", "rotate_to", "turn", "turn_by"):
        if e.props.get(k) is not None and not isinstance(e.props.get(k), bool):
            r = e.props.get(k)
            break
    v = _vec(r)
    if not v:
        return [0.0, 0.0, 0.0]
    if len(v) == 1:
        return [0.0, 0.0, v[0]]
    return (v + [0.0, 0.0])[:3]


def _color(v, fallback: str | None) -> str | None:
    if v is None or isinstance(v, bool):
        return fallback
    if isinstance(v, (list, tuple)):
        nums = [_num(x, 0.0) for x in v][:3]
        if len(nums) == 3:
            if max(nums) > 1:
                nums = [c / 255.0 for c in nums]
            return "#%02x%02x%02x" % tuple(int(max(0.0, min(1.0, c)) * 255) for c in nums)
        return fallback
    s = re.sub(r"[^A-Za-z0-9#]", "", str(v))
    if re.fullmatch(r"#?[0-9a-fA-F]{6}", s):
        return "#" + s.lstrip("#")
    return s or fallback


def _scad_str(s: str) -> str:
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _is_start(b: dict) -> bool:
    """A start-up behaviour (also the 'update if started == true' form some parsers produce)."""
    cond = re.sub(r"\s+", " ", str(b.get("if") or "")).strip().lower()
    if b.get("on") == "start":
        return not cond
    return b.get("on") == "update" and cond in ("started == true", "started", "started == 1")


def _comment(s: str) -> str:
    return re.sub(r"[\r\n]+", " ", str(s))


# ----------------------------------------------------------------------------
# the module
# ----------------------------------------------------------------------------
class CadModule(DomainModule):
    name = "cad"
    title = "CAD (parametric 3D-printable parts: OpenSCAD + CadQuery)"
    aliases = ("cad", "openscad", "mechanical", "3d-print", "printable", "قطعه")
    keywords = ("cadquery", "freecad", "scad", "stl", "printable", "printer", "3d-printer",
                "printing", "filament", "pla", "petg", "slicer", "cura", "prusaslicer", "prusa",
                "mm", "millimeter", "millimeters", "millimetre", "enclosure", "housing", "casing", "case",
                "lid", "bracket", "standoff", "standoffs", "spacer", "washer", "bushing", "gear", "gears",
                "cog", "sprocket", "pinion", "tube", "pipe", "knob", "dial", "hinge", "mount", "holder",
                "clip", "clamp", "cradle", "plate", "baseplate", "faceplate", "fillet", "chamfer", "gusset",
                "boss", "flange", "extrusion", "cutout", "bore", "countersink", "screw", "bolt", "nut",
                "m3", "m2.5", "infill", "mechanical", "cuboid", "cylinder", "cone")
    kinds = {
        "object": {},
        "part": {"size": [20, 20, 20]},
        "box": {"size": [20, 20, 20]},
        "cube": {"size": 20},
        "cylinder": {"diameter": 20, "height": 20},
        "sphere": {"diameter": 20},
        "cone": {"diameter": 20, "top_diameter": 0, "height": 20},
        "plate": {"size": [60, 40, 3], "hole_count": 4, "hole_diameter": 3.2, "corner_radius": 3},
        "enclosure": {"wall": 2},
        "case": {"wall": 2},
        "housing": {"wall": 2},
        "bracket": {"width": 20, "length": 40, "height": 30, "thickness": 3, "hole_diameter": 4.2},
        "standoff": {"height": 10, "diameter": 6, "hole_diameter": 2.7},
        "spacer": {"height": 5, "diameter": 7, "hole_diameter": 3.4},
        "washer": {"diameter": 10, "hole_diameter": 5.3, "thickness": 1},
        "gear": {"teeth": 20, "module": 1, "thickness": 5, "bore": 5},
        "tube": {"diameter": 20, "wall": 2, "length": 50},
        "pipe": {"diameter": 20, "wall": 2, "length": 50},
        "knob": {"diameter": 20, "height": 15, "shaft_diameter": 6},
        "hinge": {"width": 40, "leaf": 20, "thickness": 3, "knuckles": 5, "pin_diameter": 3},
        "mount": {"diameter": 20, "width": 15, "thickness": 3, "hole_diameter": 4.2},
        "hole": {},
        "cutout": {},
    }
    suggested_props = ("size", "wall", "hole_diameter", "fillet", "board", "vents", "color", "position",
                       "rotation", "quality")
    protocols = ("http", "mqtt", "serial", "udp", "websocket")
    toolchain = """Free tools for the CAD output (all millimetres):

* **OpenSCAD** (openscad.org): open `cad/assembly.scad` or any `cad/<part>.scad`, F6 to render, F7 to export STL.
  Command line: `openscad -o assembly.stl cad/assembly.scad` ; override a parameter:
  `openscad -D "case_wall=2.4" -o case.stl cad/case.scad`.
* **CadQuery** (`pip install cadquery`): `python cad/<part>_cadquery.py` writes STL + STEP (or open it in CQ-editor).
* **FreeCAD** can import the STEP/STL files; **Cura** / **PrusaSlicer** / **OrcaSlicer** slice the STL for printing.
* `python cad/cad_bridge.py` (stdlib) links parts to NLCL channels (live parameters / published dimensions)."""

    # ------------------------------------------------------------------ props
    def default_props(self, kind: str) -> dict:
        k = (kind or "object").lower()
        if k in self.kinds:
            return json.loads(json.dumps(self.kinds[k]))
        return {}

    def prompt_for(self, entity: Entity) -> str | None:
        if entity.parent and self._is_cut(entity):
            return (f"Where and how big is the {entity.name} cut-out? "
                    f"(e.g. side front, diameter 6 / width 12 height 7, position 10 0 8)")
        k = self._kind(entity)
        hints = {
            "enclosure": "size 80 50 30, wall 2, board esp32, vents true, fillet 3",
            "plate": "size 60 40 3, hole_count 4, hole_diameter 3.2, corner_radius 3",
            "gear": "teeth 24, module 1.5, thickness 6, bore 5",
            "bracket": "width 20, length 40, height 30, thickness 3",
            "cylinder": "diameter 20, height 30",
        }.get(k)
        if hints:
            return f"What dimensions should {entity.name} have? (e.g. {hints})"
        return super().prompt_for(entity)

    def validate(self, entity: Entity) -> list[str]:
        out = []
        try:
            k = self._kind(entity)
            p = entity.props
            wall = _num(p.get("wall", p.get("thickness")))
            if wall is not None and 0 < wall < 0.8 and k in ("enclosure", "tube", "plate", "bracket"):
                out.append(f"wall/thickness {wall} mm is thinner than two 0.4 mm nozzle lines (min ~0.8 mm)")
            if k == "gear":
                t = _num(p.get("teeth"), 20)
                if t < 6:
                    out.append("gears need at least 6 teeth (using 6)")
            used = self.default_props(entity.kind) or self.default_props(k)
            for key in ("size", "width", "depth", "height", "diameter", "radius", "length"):
                for x in _vec(p.get(key)):
                    if x < 0 or (x == 0 and (key in used or key in ("size", "width", "depth"))):
                        out.append(f"{key} must be positive (got {x})")
                    elif x > 1000:
                        out.append(f"{key} {x} mm is larger than most printers (values are millimetres)")
            if k == "enclosure":
                b = self._board_key(entity, None)
                size = _vec(p.get("size"))
                if b and len(size) >= 2:
                    bd = BOARDS[b]
                    if size[0] < bd["l"] + 2 or size[1] < bd["w"] + 2:
                        out.append(f"size {size} is too small for {bd['title']} "
                                   f"({bd['l']}x{bd['w']} mm); enlarged automatically")
        except Exception:  # validation must never raise
            pass
        return out

    # ------------------------------------------------------------------ kind helpers
    def _kind(self, e: Entity) -> str:
        k = ident(e.kind or "object")
        if k in ("object", "part") and e.props.get("subtype"):
            sub = ident(str(e.props.get("subtype")))
            for w in [sub] + sub.split("_"):
                if w in KIND_MAP and KIND_MAP[w] != "box":
                    return KIND_MAP[w]
        if k in KIND_MAP:
            ck = KIND_MAP[k]
        else:
            ck = None
            for w in reversed(k.split("_")):
                if w in KIND_MAP:
                    ck = KIND_MAP[w]
                    break
            if ck is None:
                fk = self._feature_key(e)
                ck = ("cylinder" if FEATURES[fk]["shape"] == "round" else "box") if fk else "box"
        if ck == "box" and ("lid" in e.props or _truthy(e.props.get("hollow"))):
            ck = "enclosure"
        return ck

    def _feature_key(self, e: Entity) -> str | None:
        """Most specific feature-table key named by the kind, subtype or name (usb hole -> usb)."""
        words = [ident(e.kind)] + ident(e.kind).split("_")
        if e.props.get("subtype"):
            s = ident(str(e.props["subtype"]))
            words += [s] + s.split("_")
        words += [e.ident] + e.ident.split("_")
        found = [w for w in words if w in FEATURES]
        for key in FEATURES:  # substring match ("usbport", "holes")
            if any(key in w for w in words if len(w) > 3) and key not in found:
                found.append(key)
        specific = [w for w in found if w not in GENERIC_FEATURES]
        return (specific or found or [None])[0]

    def _foreign_features(self, ctx: BuildContext | None, e: Entity) -> list[Entity]:
        """Entities of other domains placed "on/in" this part (core sets for_entity), e.g. an embedded
        led or button added to a CAD enclosure: they become cut-outs (a hole for the led, ...)."""
        out: list[Entity] = []
        try:
            system = ctx.system if ctx else None
            if system is None:
                return out
            mine = {str(e.name).lower(), e.ident}
            for o in system.entities.values():
                if o.domain == e.domain or o.parent:
                    continue
                fe = (o.props or {}).get("for_entity")
                if fe is None or str(fe).lower() not in mine and ident(str(fe)) not in mine:
                    continue
                if self._feature_key(o) is None:
                    continue
                props = {k: v for k, v in (o.props or {}).items() if k not in ("color", "colour")}
                for k, val in list(props.items()):  # 'side_front': True -> side 'front'
                    m = re.fullmatch(r"(side|face|location)_(\w+)", str(k))
                    if m and val is True and m.group(1) not in props:
                        props[m.group(1)] = m.group(2)
                props["cut"] = True
                out.append(Entity(id=o.id, name=o.name, domain=e.domain, kind=o.kind, props=props,
                                  behaviors=[], parent=e.id))
        except Exception:
            return []
        return out

    def _is_cut(self, e: Entity) -> bool:
        p = e.props
        if _truthy(p.get("cut")) or _truthy(p.get("subtract")) or _truthy(p.get("negative")):
            return True
        if _truthy(p.get("solid")) or _truthy(p.get("add")):
            return False
        k = ident(e.kind)
        if k in KIND_MAP and KIND_MAP[k] in GEOMETRY_KINDS and k not in ("object", "part"):
            return False
        return self._feature_key(e) is not None

    # ------------------------------------------------------------------ board lookup
    def _board_key(self, e: Entity, ctx: BuildContext | None) -> str | None:
        texts = []
        for key in ("board", "for", "fits", "contains", "mcu", "controller", "model", "subtype"):
            v = e.props.get(key)
            if v is not None and not isinstance(v, bool):
                texts.append(str(v))
        system = ctx.system if ctx else None
        # the part's own name only counts word-by-word ("respirator case" must not match "esp")
        for w in re.split(r"[^a-z0-9]+", str(e.name).lower()):
            if any(w.startswith(frag) for frag, _k in BOARD_MATCH):
                texts.append(w)
        # a prop naming another entity ("board esp" where esp is an embedded entity)
        if system is not None:
            for t in list(texts):
                other = None
                for o in system.entities.values():
                    if o.id != e.id and (o.name.lower() == str(t).lower() or o.ident == ident(str(t))):
                        other = o
                        break
                if other is not None:
                    texts += [str(other.props.get("board", "")), str(other.props.get("model", "")),
                              other.kind, other.name]
            # peers over channels (e.g. the esp32 that lives inside this case)
            if ctx is not None:
                for c in ctx.channels_for(e):
                    peer = c.peer(e)
                    if peer.domain in ("embedded", "hardware"):
                        texts += [str(peer.props.get("board", "")), peer.kind, peer.name]
        for t in texts:
            norm = re.sub(r"[^a-z0-9]", "", str(t).lower())
            if not norm:
                continue
            for frag, key in BOARD_MATCH:
                if frag in norm:
                    return key
        return None

    # ==================================================================
    # parameters per kind
    # ==================================================================
    def _params(self, ctx: BuildContext | None, e: Entity) -> tuple[dict, list[float], dict]:
        """Return (params, bbox [x,y,z], extra).  params are scalar numbers/bools/strings."""
        k = self._kind(e)
        p = e.props
        fk = self._feature_key(e) if ident(e.kind) not in KIND_MAP else None
        if fk:  # a feature (hole, usb, ...) without a parent: show it as a solid stand-in
            spec = FEATURES[fk]
            stand_in = ({"diameter": spec.get("d", 3.2)} if spec["shape"] == "round"
                        else {"width": spec.get("w", 10.0), "depth": spec.get("h", 6.0)})
            stand_in["height"] = 10.0
            stand_in.update(p)
            p = stand_in
        g = lambda *names, d=None: next((x for x in (_num(p.get(n)) for n in names) if x is not None), d)
        size = _vec(p.get("size"))
        extra: dict = {}
        P: dict = {}
        if k in ("box", "cube"):
            dflt = 20.0
            if len(size) == 1:
                size = size * 3
            size = (size + [dflt] * 3)[:3] if size else [dflt] * 3
            P["width"] = g("width", "length", d=size[0])
            P["depth"] = g("depth", d=size[1])
            P["height"] = g("height", d=size[2])
            P["fillet"] = g("fillet", "rounding", "corner_radius", d=0.0)
            if g("hole_diameter", d=0) > 0:
                P["hole_diameter"] = g("hole_diameter")
            bbox = [P["width"], P["depth"], P["height"]]
        elif k == "cylinder":
            d = g("diameter", d=None)
            if d is None:
                r = g("radius")
                d = 2 * r if r else (size[0] if size else 20.0)
            P["diameter"] = d
            P["height"] = g("height", "length", d=size[2] if len(size) > 2 else (size[1] if len(size) == 2 else 20.0))
            P["hole_diameter"] = g("hole_diameter", "bore", d=0.0)
            bbox = [d, d, P["height"]]
        elif k == "sphere":
            d = g("diameter", d=None)
            if d is None:
                r = g("radius")
                d = 2 * r if r else (size[0] if size else 20.0)
            P["diameter"] = d
            bbox = [d, d, d]
        elif k == "cone":
            d = g("diameter", "bottom_diameter", d=None)
            if d is None:
                r = g("radius")
                d = 2 * r if r else 20.0
            P["diameter"] = d
            P["top_diameter"] = g("top_diameter", d=0.0)
            P["height"] = g("height", "length", d=20.0)
            bbox = [d, d, P["height"]]
        elif k == "plate":
            size = (size + [60.0, 40.0, 3.0][len(size):])[:3]
            P["width"] = g("width", "length", d=size[0])
            P["depth"] = g("depth", d=size[1])
            P["thickness"] = g("thickness", "height", d=size[2])
            P["corner_radius"] = g("corner_radius", "fillet", "rounding", "radius", d=3.0)
            P["hole_count"] = int(max(0, min(200, g("hole_count", "holes", d=4))))
            P["hole_diameter"] = g("hole_diameter", d=3.2)
            P["hole_inset"] = g("hole_inset", d=max(P["hole_diameter"] * 1.5, 5.0))
            P["countersink"] = _truthy(p.get("countersink"))
            bbox = [P["width"], P["depth"], P["thickness"]]
        elif k == "enclosure":
            P, bbox, extra = self._enclosure_params(ctx, e, size, g)
        elif k == "bracket":
            P["width"] = g("width", d=size[1] if len(size) > 1 else 20.0)
            P["length"] = g("length", d=size[0] if size else 40.0)
            P["height"] = g("height", d=size[2] if len(size) > 2 else 30.0)
            P["thickness"] = g("thickness", "wall", d=3.0)
            P["hole_diameter"] = g("hole_diameter", d=4.2)
            P["holes_per_leg"] = int(max(0, min(20, g("holes_per_leg", d=max(1.0, g("hole_count", d=4) / 2)))))
            P["gusset"] = _truthy(p.get("gusset"), True)
            bbox = [P["length"], P["width"], P["height"]]
        elif k in ("standoff", "spacer", "washer"):
            defaults = {"standoff": (10.0, 6.0, 2.7), "spacer": (5.0, 7.0, 3.4), "washer": (1.0, 10.0, 5.3)}[k]
            P["height"] = g("height", "length", "thickness", d=defaults[0])
            d = g("diameter", d=None)
            if d is None:
                r = g("radius")
                d = 2 * r if r else defaults[1]
            P["diameter"] = d
            P["hole_diameter"] = min(g("hole_diameter", "bore", d=defaults[2]), d - 0.8)
            P["hex"] = _truthy(p.get("hex"), False) or str(p.get("shape", "")).lower() in ("hex", "hexagon")
            bbox = [d, d, P["height"]]
        elif k == "gear":
            P["teeth"] = int(max(6, min(400, g("teeth", "tooth_count", d=20))))
            P["module"] = max(0.2, g("module", "gear_module", d=None) or
                              (g("pitch_diameter", "diameter", d=0) / P["teeth"]) or 1.0)
            P["thickness"] = g("thickness", "height", "width", d=5.0)
            P["bore"] = max(0.0, g("bore", "hole_diameter", "shaft_diameter", d=5.0))
            od = P["module"] * (P["teeth"] + 2)
            bbox = [od, od, P["thickness"]]
        elif k == "tube":
            d = g("diameter", "outer_diameter", d=None)
            if d is None:
                r = g("radius")
                d = 2 * r if r else 20.0
            P["diameter"] = d
            inner = g("inner_diameter", d=None)
            P["wall"] = (d - inner) / 2 if inner else g("wall", "thickness", d=2.0)
            P["wall"] = max(0.4, min(P["wall"], d / 2 - 0.2))
            P["length"] = g("length", "height", d=50.0)
            bbox = [d, d, P["length"]]
        elif k == "knob":
            P["diameter"] = g("diameter", d=(2 * g("radius", d=10.0)))
            P["height"] = g("height", d=15.0)
            P["shaft_diameter"] = g("shaft_diameter", "hole_diameter", "bore", d=6.0)
            P["d_shaft"] = _truthy(p.get("d_shaft"), True)
            P["ridges"] = int(max(0, min(120, g("ridges", "grip", d=18))))
            bbox = [P["diameter"], P["diameter"], P["height"]]
        elif k == "hinge":
            P["width"] = g("width", "length", d=40.0)
            P["leaf"] = g("leaf", "leaf_length", "depth", d=20.0)
            P["thickness"] = g("thickness", d=3.0)
            P["knuckles"] = int(max(2, min(15, g("knuckles", d=5))))
            P["pin_diameter"] = g("pin_diameter", d=3.0)
            P["clearance"] = g("clearance", d=0.4)
            P["hole_diameter"] = g("hole_diameter", d=3.2)
            kd = P["pin_diameter"] + 2 * P["clearance"] + 3.2
            bbox = [P["width"], 2 * P["leaf"] + kd, max(kd, P["thickness"])]
        elif k == "mount":
            P["diameter"] = g("diameter", "item_diameter", d=(2 * g("radius", d=10.0)))
            P["width"] = g("width", d=15.0)
            P["thickness"] = g("thickness", "wall", d=3.0)
            P["hole_diameter"] = g("hole_diameter", d=4.2)
            L = P["diameter"] + 2 * P["thickness"] + 6 * P["hole_diameter"]
            bbox = [P["width"], L, P["diameter"] + 2 * P["thickness"]]
        else:  # unreachable fallback: generic box
            P = {"width": 20.0, "depth": 20.0, "height": 20.0, "fillet": 0.0}
            bbox = [20.0, 20.0, 20.0]
        # common: dimensions must be positive and finite
        for key, dflt in (("width", 20.0), ("depth", 20.0), ("height", 20.0), ("diameter", 20.0), ("length", 20.0),
                          ("thickness", 2.0), ("wall", 2.0), ("module", 1.0), ("leaf", 20.0), ("pin_diameter", 3.0)):
            val = P.get(key)
            if isinstance(val, (int, float)) and not isinstance(val, bool) and (not math.isfinite(val) or val <= 0):
                P[key] = dflt
                bbox = [dflt if (not isinstance(b, (int, float)) or b <= 0) else b for b in bbox]
        for key in ("hole_diameter", "fillet", "corner_radius", "bore", "top_diameter", "hole_inset"):
            val = P.get(key)
            if isinstance(val, (int, float)) and not isinstance(val, bool) and (not math.isfinite(val) or val < 0):
                P[key] = 0.0
        q = p.get("quality", p.get("fn", p.get("resolution")))
        if isinstance(q, str) and q.strip().lower() in ("low", "draft", "fast"):
            fn = 24
        elif isinstance(q, str) and q.strip().lower() in ("high", "fine", "smooth"):
            fn = 128
        else:
            fn = int(max(8, min(360, _num(q, 64.0))))
        P["fn"] = fn
        for key, v in list(P.items()):
            if isinstance(v, float) and not math.isfinite(v):
                P[key] = 0.0
        return P, [max(0.01, float(x)) for x in bbox], extra

    def _enclosure_params(self, ctx, e, size, g):
        p = e.props
        extra: dict = {}
        wall = max(0.8, g("wall", "wall_thickness", "thickness", d=2.0))
        board = self._board_key(e, ctx)
        clearance = g("clearance", d=1.0)
        standoff_h = g("standoff_height", d=5.0)
        dims = (size + [80.0, 50.0, 30.0][len(size):])[:3] if size else None
        W = g("width", "length", d=dims[0] if dims else None)
        D = g("depth", d=dims[1] if dims else None)
        H = g("height", d=dims[2] if dims else None)
        if board:
            bd = BOARDS[board]
            need = [bd["l"] + 2 * clearance + 2 * wall,
                    bd["w"] + 2 * clearance + 2 * wall,
                    standoff_h + 1.6 + bd["comp"] + 2.0 + 2 * wall]
            W = max(W or 0, need[0])
            D = max(D or 0, need[1])
            H = max(H or 0, need[2])
            extra["board"] = board
        W, D, H = W or 80.0, D or 50.0, H or 30.0
        H = max(H, 2 * wall + 4)
        P = {"width": W, "depth": D, "height": H, "wall": wall,
             "fillet": g("fillet", "rounding", "corner_radius", d=3.0),
             "lip_height": g("lip_height", d=3.0), "lip_thickness": g("lip_thickness", d=1.2),
             "fit": g("fit", "tolerance", d=0.2), "explode": g("explode", d=0.0),
             "standoff_height": standoff_h, "screw_diameter": g("screw_diameter", d=2.5)}
        vents = p.get("vents", p.get("ventilation", p.get("vent")))
        if vents is not None and _truthy(vents if not isinstance(vents, (int, float)) or isinstance(vents, bool) else vents != 0):
            n = int(_num(vents, 0) or 0) if not isinstance(vents, bool) else 0
            extra["vents"] = int(max(1, min(40, n if n > 1 else max(3, (W - 20) / 6))))
        bosses = p.get("screw_bosses", p.get("bosses"))
        extra["bosses"] = _truthy(bosses, bool(board and BOARDS[board]["holes"]))
        extra["lid"] = _truthy(p.get("lid"), True)
        return P, [W, D, H], extra

    # ==================================================================
    # features (holes, cut-outs, bosses, vents) as primitive lists
    # ==================================================================
    # feature dict: op cut|add, target base|lid|body, shape box|cyl, axis x|y|z,
    #               size [x,y,z] (box) or d,h (cyl), c numeric centre, cs scad expressions
    def _face(self, v, k, P, bbox, side, u, w_along, zc, depth_override=None):
        """Compute centre + oriented box size for a feature on a face.

        u: offset along the face's horizontal tangent (x for front/back/top, y for left/right),
        zc: height of the centre (side faces) or y offset (top/bottom faces).
        Returns (target, axis, centre numbers, centre scad expressions, normal depth).
        """
        bx, by, bz = bbox
        enc = k == "enclosure"
        wall = P.get("wall", 2.0)
        if side in ("left", "right"):
            s = -1 if side == "left" else 1
            if enc:
                cx, cxs = s * (bx / 2 - wall / 2), f"{'-' if s < 0 else ''}({v}_width/2 - {v}_wall/2)"
                depth = wall + 2
            else:
                cx, cxs = 0.0, "0"
                depth = bx + 2
            return "base" if enc else "body", "x", [cx, u, zc], [cxs, _n(u), _n(zc)], depth
        if side in ("front", "back"):
            s = -1 if side == "front" else 1
            if enc:
                cy, cys = s * (by / 2 - wall / 2), f"{'-' if s < 0 else ''}({v}_depth/2 - {v}_wall/2)"
                depth = wall + 2
            else:
                cy, cys = 0.0, "0"
                depth = by + 2
            return "base" if enc else "body", "y", [u, cy, zc], [_n(u), cys, _n(zc)], depth
        # top / bottom
        if enc:
            if side == "top":
                return "lid", "z", [u, zc, bz - wall / 2], [_n(u), _n(zc), f"{v}_height - {v}_wall/2"], wall + 2
            return "base", "z", [u, zc, wall / 2], [_n(u), _n(zc), f"{v}_wall/2"], wall + 2
        return "body", "z", [u, zc, bz / 2], [_n(u), _n(zc), _n(bz / 2)], bz + 2

    def _features(self, ctx: BuildContext, e: Entity, k: str, P: dict, bbox: list, extra: dict) -> tuple[list, list]:
        """Return (features, union_children)."""
        v = self._vid(e)
        feats: list[dict] = []
        unions: list[Entity] = []
        bx, by, bz = bbox
        enc = k == "enclosure"
        wall = P.get("wall", 2.0)
        base_h = (bz - wall if extra.get("lid", True) else bz) if enc else bz
        mid_z = (wall + base_h) / 2 if enc else bz / 2

        def add_round(target, axis, c, cs, d, depth, label, op="cut"):
            feats.append(dict(op=op, target=target, shape="cyl", axis=axis, d=d, h=depth, c=c, cs=cs, label=label))

        def add_rect(target, axis, c, cs, w, h, depth, label, op="cut"):
            if axis == "x":
                size = [depth, w, h]
            elif axis == "y":
                size = [w, depth, h]
            else:
                size = [w, h, depth]
            feats.append(dict(op=op, target=target, shape="box", axis=axis, size=size, c=c, cs=cs, label=label))

        # ---- board: bosses + connector cut-outs
        board = extra.get("board")
        if enc and board:
            bd = BOARDS[board]
            ox, oy = -bd["l"] / 2, -bd["w"] / 2
            pcb_top = wall + P["standoff_height"] + 1.6
            if extra.get("bosses"):
                for (hx, hy) in bd["holes"]:
                    x, y = ox + hx, oy + hy
                    h = P["standoff_height"]
                    od = max(5.0, bd["hole_d"] * 2.2)
                    add_round("base", "z", [x, y, wall + h / 2 - 0.01], [_n(x), _n(y), f"{v}_wall + {_n(h / 2 - 0.01)}"],
                              od, h + 0.02, f"{bd['title']} mounting boss", op="add")
                    add_round("base", "z", [x, y, wall + h / 2 + 0.5], [_n(x), _n(y), f"{v}_wall + {_n(h / 2 + 0.5)}"],
                              P["screw_diameter"], h + 1.02, "boss pilot hole")
            child_usb = any(self._is_cut(ch) and self._feature_key(ch) in ("usb", "usb_c")
                            and "position" not in ch.props
                            for ch in ctx.children(e) + self._foreign_features(ctx, e))
            for pi, (label, side, off, w, h) in enumerate(bd["ports"]):
                if pi == 0 and child_usb:
                    continue  # the usb child feature below replaces the board's own usb cut-out
                u = (oy + off) if side in ("left", "right") else (ox + off)
                zc = pcb_top + h / 2 - 0.5
                tgt, axis, c, cs, depth = self._face(v, k, P, bbox, side, u, w, zc)
                add_rect(tgt, axis, c, cs, w + 1.0, h + 1.0, depth, f"{bd['title']} {label}")
        elif enc and extra.get("bosses"):
            # generic pcb bosses in the corners
            inset = wall + 4.0
            for sx in (-1, 1):
                for sy in (-1, 1):
                    x, y = sx * (bx / 2 - inset), sy * (by / 2 - inset)
                    h = P["standoff_height"]
                    add_round("base", "z", [x, y, wall + h / 2 - 0.01], [_n(x), _n(y), f"{v}_wall + {_n(h / 2 - 0.01)}"],
                              6.0, h + 0.02, "screw boss", op="add")
                    add_round("base", "z", [x, y, wall + h / 2 + 0.5], [_n(x), _n(y), f"{v}_wall + {_n(h / 2 + 0.5)}"],
                              P["screw_diameter"], h + 1.02, "boss pilot hole")
        # ---- ventilation slots on the two long side walls
        if enc and extra.get("vents"):
            n = int(extra["vents"])
            long_x = bx >= by
            span = (bx if long_x else by) * 0.6
            hh = max(3.0, (base_h - wall) * 0.4)
            zc = wall + (base_h - wall) * 0.6
            for side in (("front", "back") if long_x else ("left", "right")):
                for i in range(n):
                    u = -span / 2 + (i + 0.5) * span / n
                    tgt, axis, c, cs, depth = self._face(v, k, P, bbox, side, u, 2.0, zc)
                    add_rect(tgt, axis, c, cs, 2.0, hh, depth, f"vent slot {side}")
        # ---- children
        cuts: list[tuple[Entity, dict, str]] = []
        for ch in ctx.children(e) + self._foreign_features(ctx, e):
            if self._is_cut(ch):
                key = self._feature_key(ch) or "hole"
                spec = dict(FEATURES.get(key, FEATURES["hole"]))
                side = None
                for sk in ("side", "face", "wall", "location"):
                    sv = ch.props.get(sk)
                    if isinstance(sv, str) and sv.strip().lower() in SIDE_WORDS:
                        side = SIDE_WORDS[sv.strip().lower()]
                if side is None:
                    side = spec["side"] if enc else "top"
                    if enc and key == "usb" and board:
                        side = BOARDS[board]["ports"][0][1]
                cuts.append((ch, spec, side))
            else:
                unions.append(ch)
        # distribute features without explicit position along their face
        by_side: dict[str, list] = {}
        for item in cuts:
            if "position" not in item[0].props:
                by_side.setdefault(item[2], []).append(item)
        auto_u: dict[str, float] = {}
        for side, items in by_side.items():
            length = (by if side in ("left", "right") else bx) * 0.6
            for i, (ch, _s, _sd) in enumerate(items):
                auto_u[ch.id] = -length / 2 + (i + 0.5) * length / len(items)
        for ch, spec, side in cuts:
            cp = ch.props
            pos = _vec(cp.get("position"))
            d = _num(cp.get("diameter"))
            if d is None and _num(cp.get("radius")) is not None:
                d = 2 * _num(cp.get("radius"))
            if d is None:
                d = _num(cp.get("hole_diameter"))
            sz = _vec(cp.get("size"))
            w = _num(cp.get("width"), sz[0] if sz else None)
            h = _num(cp.get("height"), sz[1] if len(sz) > 1 else None)
            shape = spec["shape"]
            if d is not None and w is None:
                shape = "round"
            elif w is not None or h is not None:
                shape = "rect" if shape != "slots" else "slots"
            if side in ("left", "right"):
                u = pos[1] if len(pos) > 1 else auto_u.get(ch.id, 0.0)
                zc = pos[2] if len(pos) > 2 else mid_z
            elif side in ("front", "back"):
                u = pos[0] if pos else auto_u.get(ch.id, 0.0)
                zc = pos[2] if len(pos) > 2 else mid_z
            else:
                u = pos[0] if pos else auto_u.get(ch.id, 0.0)
                zc = pos[1] if len(pos) > 1 else 0.0
            # usb without explicit height on a board enclosure: align with the board connector
            if enc and board and self._feature_key(ch) in ("usb", "usb_c") and len(pos) < 3 and side in ("left", "right", "front", "back"):
                bp = BOARDS[board]["ports"][0]
                zc = wall + P["standoff_height"] + 1.6 + bp[4] / 2 - 0.5
                if not pos:
                    u = (-BOARDS[board]["w"] / 2 + bp[2]) if side in ("left", "right") else (-BOARDS[board]["l"] / 2 + bp[2])
            tgt, axis, c, cs, depth = self._face(v, k, P, bbox, side, u, w or 0, zc)
            depth = _num(cp.get("depth"), depth)
            label = f"{ch.name} ({ch.kind}, {side})"
            if shape == "round":
                add_round(tgt, axis, c, cs, d if d is not None else spec.get("d", 3.2), depth, label)
            elif shape == "slots":
                n = int(_num(cp.get("count", cp.get("slots")), 5))
                n = max(1, min(60, n))
                sw = w or 2.0
                sh = h or max(3.0, (base_h - wall) * 0.4 if enc else bz * 0.5)
                pitch = sw * 2.5
                for i in range(n):
                    du = (i - (n - 1) / 2) * pitch
                    if axis == "x":
                        cc = [c[0], c[1] + du, c[2]]
                        ccs = [cs[0], _n(cc[1]), cs[2]]
                    else:
                        cc = [c[0] + du, c[1], c[2]]
                        ccs = [_n(cc[0]), cs[1], cs[2]]
                    add_rect(tgt, axis, cc, ccs, sw, sh, depth, f"{label} slot {i + 1}")
            else:
                add_rect(tgt, axis, c, cs, w or spec.get("w", 10.0), h or spec.get("h", 6.0), depth, label)
        return feats, unions

    # ==================================================================
    # OpenSCAD generation
    # ==================================================================
    def _vid(self, e: Entity) -> str:
        v = e.ident or ident(e.id) or "part"
        # names that would clash with generated files (assembly.scad, the helper library, the bridge)
        if v in ("assembly", "nlcl_cad_lib", "cad_bridge", "readme", "live_values"):
            v += "_part"
        return v

    def _scad_vars(self, v: str, P: dict) -> list[str]:
        L = []
        for key, val in P.items():
            if isinstance(val, bool):
                L.append(f"{v}_{key} = {'true' if val else 'false'};")
            elif isinstance(val, (int, float)):
                L.append(f"{v}_{key} = {_n(val)};")
            else:
                L.append(f"{v}_{key} = {_scad_str(val)};")
        return L

    def _scad_prim(self, f: dict) -> str:
        c = ", ".join(f["cs"])
        if f["shape"] == "box":
            return f"translate([{c}]) cube([{', '.join(_n(x) for x in f['size'])}], center=true);  // {_comment(f['label'])}"
        rot = {"x": "rotate([0, 90, 0]) ", "y": "rotate([90, 0, 0]) ", "z": ""}[f["axis"]]
        return (f"translate([{c}]) {rot}cylinder(d={_n(f['d'])}, h={_n(f['h'])}, center=true);"
                f"  // {_comment(f['label'])}")

    def _scad_body(self, v: str, k: str, P: dict) -> list[str]:
        """Plain body of a non-enclosure part (no features)."""
        if k in ("box", "cube"):
            L = [f"nl_rbox([{v}_width, {v}_depth, {v}_height], {v}_fillet);"]
            if "hole_diameter" in P:
                L = ["difference() {", "    " + L[0],
                     f"    translate([0, 0, -1]) cylinder(d={v}_hole_diameter, h={v}_height + 2);", "}"]
            return L
        if k == "cylinder":
            return ["difference() {", f"    cylinder(d={v}_diameter, h={v}_height);",
                    f"    if ({v}_hole_diameter > 0) translate([0, 0, -1]) cylinder(d={v}_hole_diameter, h={v}_height + 2);",
                    "}"]
        if k == "sphere":
            return [f"translate([0, 0, {v}_diameter / 2]) sphere(d={v}_diameter);"]
        if k == "cone":
            return [f"cylinder(d1={v}_diameter, d2={v}_top_diameter, h={v}_height);"]
        if k == "plate":
            return [f"nl_plate({v}_width, {v}_depth, {v}_thickness, {v}_corner_radius, {v}_hole_count, "
                    f"{v}_hole_diameter, {v}_hole_inset, {v}_countersink);"]
        if k == "bracket":
            return [f"nl_bracket({v}_length, {v}_width, {v}_height, {v}_thickness, {v}_hole_diameter, "
                    f"{v}_holes_per_leg, {v}_gusset);"]
        if k in ("standoff", "spacer", "washer"):
            return [f"nl_standoff({v}_diameter, {v}_height, {v}_hole_diameter, {v}_hex);"]
        if k == "gear":
            return [f"nl_gear({v}_teeth, {v}_module, {v}_thickness, {v}_bore);"]
        if k == "tube":
            return [f"nl_tube({v}_diameter, {v}_diameter - 2 * {v}_wall, {v}_length);"]
        if k == "knob":
            return [f"nl_knob({v}_diameter, {v}_height, {v}_shaft_diameter, {v}_d_shaft, {v}_ridges);"]
        if k == "hinge":
            return [f"nl_hinge({v}_width, {v}_leaf, {v}_thickness, {v}_knuckles, {v}_pin_diameter, "
                    f"{v}_clearance, {v}_hole_diameter);"]
        if k == "mount":
            return [f"nl_mount({v}_diameter, {v}_thickness, {v}_width, {v}_hole_diameter);"]
        return [f"nl_rbox([{v}_width, {v}_depth, {v}_height], {v}_fillet);"]

    def _notes(self, ctx: BuildContext, e: Entity) -> list[str]:
        L = []
        for b in e.behaviors or []:
            try:
                acts = "; ".join(describe_action(a) for a in b.get("do", []))
                cond = f" if {b['if']}" if b.get("if") else ""
                L.append(f"// note: behaviour on {b.get('on')}{cond} -> {_comment(acts)} "
                         f"(CAD geometry is static; behaviours are not simulated)")
            except Exception:
                L.append(f"// note: behaviour {_comment(b)} (ignored)")
        for c in ctx.channels_for(e) if ctx else []:
            d = c.direction(e)
            L.append(f"// note: channel {c.id} ({c.protocol}) {'sends' if d == 'out' else 'receives'} "
                     f"{', '.join(c.fields)} {'to' if d == 'out' else 'from'} {c.peer(e).name} "
                     f"({c.peer(e).domain}) - see cad_bridge.py")
        return L

    def _start_offset(self, e: Entity) -> list[float]:
        """Numeric 'move' actions in start behaviours shift the placement."""
        off = [0.0, 0.0, 0.0]
        for b in e.behaviors or []:
            if not _is_start(b):
                continue
            for a in b.get("do", []):
                if a.get("op") == "move":
                    vals = [_num(a.get(ax)) for ax in ("x", "y", "z")]
                    if all(x is not None and re.fullmatch(r"-?\d+(\.\d+)?", str(a.get(ax)).strip())
                           for x, ax in zip(vals, ("x", "y", "z"))):
                        off = [o + x for o, x in zip(off, vals)]
        return off

    def _placement(self, e: Entity, default=(0.0, 0.0, 0.0)) -> tuple[list[float], list[float]]:
        pos = _pos(e, default)
        off = self._start_offset(e)
        return [a + b for a, b in zip(pos, off)], _rot(e)

    def _scad_entity(self, ctx: BuildContext, e: Entity, color_fallback: str | None, info: dict, depth=0) -> list[str]:
        """Parameters + modules for e and (recursively) its children."""
        v = self._vid(e)
        k = self._kind(e)
        P, bbox, extra = self._params(ctx, e)
        feats, unions = self._features(ctx, e, k, P, bbox, extra)
        info[e.id] = dict(kind=k, P=P, bbox=bbox, extra=extra, feats=feats, unions=unions)
        L = [f"// ---- {_comment(e.name)}: {k}" + (f" (from kind '{_comment(e.kind)}')" if ident(e.kind) != k else "")
             + f"  bbox {' x '.join(_n(x) for x in bbox)} mm"]
        if ident(e.kind) not in KIND_MAP and not self._feature_key(e):
            L.append(f"// note: kind '{_comment(e.kind)}' has no dedicated CAD template; using a parametric box")
        if extra.get("board"):
            bd = BOARDS[extra["board"]]
            L.append(f"// sized for {bd['title']} ({_n(bd['l'])} x {_n(bd['w'])} mm pcb) + clearance; "
                     f"verify connector positions against your board")
        L += self._notes(ctx, e)
        L += self._scad_vars(v, P)
        if k == "enclosure":
            if extra.get("lid", True):
                L.append(f"{v}_base_h = {v}_height - {v}_wall;  // height of the base (lid sits on top)")
            else:
                L.append(f"{v}_base_h = {v}_height;  // open-top box (no lid)")
        col = _color(e.props.get("color", e.props.get("colour")), color_fallback)
        L.append(f"{v}_color = {_scad_str(col) if col else 'undef'};")
        L.append("")
        # children modules first
        child_lines = []
        for ch in unions:
            child_lines += self._scad_entity(ctx, ch, None, info, depth + 1)
        L += child_lines

        def child_calls(target_floor):
            out = []
            for ch in unions:
                cpos, crot = self._placement(ch, (0.0, 0.0, target_floor))
                out.append(f"translate([{', '.join(_n(x) for x in cpos)}]) rotate([{', '.join(_n(x) for x in crot)}]) "
                           f"{self._vid(ch)}_part();  // child {_comment(ch.name)}")
            return out

        def wrap(target, body):
            adds = [self._scad_prim(f) for f in feats if f["target"] == target and f["op"] == "add"]
            cuts = [self._scad_prim(f) for f in feats if f["target"] == target and f["op"] == "cut"]
            extra_adds = child_calls(P.get("wall", 0) if k == "enclosure" else bbox[2]) if target in ("base", "body") else []
            if not adds and not cuts and not extra_adds:
                return body
            out = ["difference() {", "    union() {"]
            out += ["        " + x for x in body + adds + extra_adds]
            out += ["    }"]
            out += ["    " + x for x in cuts]
            out += ["}"]
            return out

        if k == "enclosure":
            shell = [
                "difference() {",
                f"    nl_rbox([{v}_width, {v}_depth, {v}_base_h], {v}_fillet);",
                f"    translate([0, 0, {v}_wall]) nl_rbox([{v}_width - 2 * {v}_wall, {v}_depth - 2 * {v}_wall, {v}_base_h], "
                f"max({v}_fillet - {v}_wall, 0));",
                "}",
            ]
            lid = [
                f"translate([0, 0, {v}_base_h]) nl_rbox([{v}_width, {v}_depth, {v}_wall], {v}_fillet);",
                "// lip that keys into the base (friction fit)",
                f"translate([0, 0, {v}_base_h - {v}_lip_height]) difference() {{",
                f"    nl_rbox([{v}_width - 2 * ({v}_wall + {v}_fit), {v}_depth - 2 * ({v}_wall + {v}_fit), {v}_lip_height + 0.01], "
                f"max({v}_fillet - {v}_wall, 0));",
                f"    translate([0, 0, -1]) nl_rbox([{v}_width - 2 * ({v}_wall + {v}_fit + {v}_lip_thickness), "
                f"{v}_depth - 2 * ({v}_wall + {v}_fit + {v}_lip_thickness), {v}_lip_height + 2], 0);",
                "}",
            ]
            L.append(f"// base: open box, floor = wall")
            L.append(f"module {v}_base() {{")
            L.append(f"    $fn = {v}_fn;")
            L += ["    " + x for x in wrap("base", shell)]
            L.append("}")
            L.append("")
            L.append(f"// lid (in assembled position)")
            L.append(f"module {v}_lid() {{")
            L.append(f"    $fn = {v}_fn;")
            L += ["    " + x for x in wrap("lid", lid)]
            L.append("}")
            L.append("")
            L.append(f"// assembled part (lid raised by {v}_explode for an exploded view)")
            L.append(f"module {v}_part() {{")
            L.append(f"    nl_color({v}_color) {{")
            L.append(f"        {v}_base();")
            if extra.get("lid", True):
                L.append(f"        translate([0, 0, {v}_explode]) {v}_lid();")
            L.append("    }")
            if extra.get("board"):
                bd = BOARDS[extra["board"]]
                L.append(f"    // ghost of the {bd['title']} pcb (% = preview only, not exported)")
                L.append(f"    %translate([{_n(-bd['l'] / 2)}, {_n(-bd['w'] / 2)}, {v}_wall + {v}_standoff_height]) "
                         f"cube([{_n(bd['l'])}, {_n(bd['w'])}, 1.6]);")
            L.append("}")
            L.append("")
            L.append("// print layout: base + lid flipped upside down next to it")
            L.append(f"module {v}_print() {{")
            L.append(f"    {v}_base();")
            if extra.get("lid", True):
                L.append(f"    translate([{v}_width + 10, 0, {v}_height]) rotate([180, 0, 0]) {v}_lid();")
            L.append("}")
        else:
            body = self._scad_body(v, k, P)
            L.append(f"module {v}_part() {{")
            L.append(f"    $fn = {v}_fn;")
            L.append(f"    nl_color({v}_color) {{")
            L += ["        " + x for x in wrap("body", body)]
            L.append("    }")
            L.append("}")
            L.append("")
            L.append(f"module {v}_print() {{ {v}_part(); }}")
        L.append("")
        return L

    # ------------------------------------------------------------------ library
    SCAD_LIB = r"""// NLCL CAD helper library (OpenSCAD 2019.05+). Units: mm.
// All parts are centred on X/Y and sit on Z = 0.

// colour wrapper that accepts undef (= default colour)
module nl_color(c) { if (is_undef(c)) children(); else color(c) children(); }

// box centred on X/Y, from z = 0 up, vertical edges rounded with radius r
module nl_rbox(size, r = 0) {
    rr = min(r, size[0] / 2 - 0.01, size[1] / 2 - 0.01);
    if (rr <= 0.01) translate([-size[0] / 2, -size[1] / 2, 0]) cube(size);
    else hull() for (x = [-1, 1], y = [-1, 1])
        translate([x * (size[0] / 2 - rr), y * (size[1] / 2 - rr), 0]) cylinder(r = rr, h = size[2]);
}

// point on the perimeter of an a x b rectangle at arc length s (starts at the -x,-y corner)
function nl_perim_at(a, b, s) =
    s < a ? [-a / 2 + s, -b / 2] :
    s < a + b ? [a / 2, -b / 2 + (s - a)] :
    s < 2 * a + b ? [a / 2 - (s - a - b), b / 2] :
    [-a / 2, b / 2 - (s - 2 * a - b)];

// n hole centres for a w x d plate, inset from the edges
function nl_hole_pts(w, d, n, inset) =
    let(a = max(w - 2 * inset, 0), b = max(d - 2 * inset, 0))
    n <= 0 ? [] :
    n == 1 ? [[0, 0]] :
    n == 2 ? [[-a / 2, 0], [a / 2, 0]] :
    n == 3 ? [[-a / 2, -b / 2], [a / 2, -b / 2], [0, b / 2]] :
    n == 4 ? [for (x = [-1, 1], y = [-1, 1]) [x * a / 2, y * b / 2]] :
    [for (i = [0 : n - 1]) nl_perim_at(a, b, i * 2 * (a + b) / n)];

// flat plate with rounded corners and a hole pattern
module nl_plate(w, d, t, r, n, hd, inset, countersink = false) {
    difference() {
        nl_rbox([w, d, t], r);
        for (p = nl_hole_pts(w, d, n, inset)) translate([p[0], p[1], 0]) {
            translate([0, 0, -1]) cylinder(d = hd, h = t + 2);
            if (countersink) translate([0, 0, t - hd / 2]) cylinder(d1 = hd, d2 = 2 * hd, h = hd / 2 + 0.01);
        }
    }
}

// L-bracket: horizontal leg along +x (length a), vertical leg up +z (height b), width w along y
module nl_bracket(a, w, b, t, hd, n, gusset = true) {
    difference() {
        union() {
            translate([0, -w / 2, 0]) cube([a, w, t]);
            translate([0, -w / 2, 0]) cube([t, w, b]);
            if (gusset) for (s = [-1, 1])
                translate([t, s * (w / 2 - t / 2), t]) rotate([90, 0, 0])
                    linear_extrude(height = t, center = true)
                        polygon([[0, 0], [max(min(a, b) * 0.5 - t, 1), 0], [0, max(min(a, b) * 0.5 - t, 1)]]);
        }
        if (n > 0) for (i = [0 : n - 1]) {
            translate([t + (a - t) * (i + 0.5) / n, 0, -1]) cylinder(d = hd, h = t + 2);
            translate([-1, 0, t + (b - t) * (i + 0.5) / n]) rotate([0, 90, 0]) cylinder(d = hd, h = t + 2);
        }
    }
}

// round or hexagonal standoff / spacer / washer with a centre hole
module nl_standoff(od, h, hd, hex = false) {
    difference() {
        if (hex) cylinder(d = od, h = h, $fn = 6); else cylinder(d = od, h = h);
        if (hd > 0) translate([0, 0, -1]) cylinder(d = hd, h = h + 2);
    }
}

module nl_tube(od, id, h) {
    difference() {
        cylinder(d = od, h = h);
        if (id > 0) translate([0, 0, -1]) cylinder(d = id, h = h + 2);
    }
}

// spur gear, trapezoid approximation of an involute tooth (6 points per tooth)
// mod = module (pitch diameter / teeth); pressure angle ~20 deg
module nl_gear(teeth, mod, thickness, bore) {
    z = max(6, round(teeth));
    rp = mod * z / 2;                                 // pitch radius
    ra = rp + mod;                                    // addendum (tip) radius
    rf = max(rp - 1.25 * mod, bore / 2 + 0.5 * mod);  // dedendum (root) radius
    h = 90 / z;                                       // quarter of the angular pitch (deg)
    prof = [[rf, -1.25], [rp, -1.0], [ra, -0.55], [ra, 0.55], [rp, 1.0], [rf, 1.25]];
    pts = [for (i = [0 : z - 1], j = [0 : 5])
              let(a = i * 360 / z + prof[j][1] * h) [prof[j][0] * cos(a), prof[j][0] * sin(a)]];
    difference() {
        linear_extrude(height = thickness) polygon(pts);
        if (bore > 0) translate([0, 0, -1]) cylinder(d = bore, h = thickness + 2);
    }
}

// knurled knob with a (D-)shaft hole from below and a pointer groove on top
module nl_knob(d, h, sd, dshaft = true, ridges = 18) {
    difference() {
        cylinder(d = d, h = h);
        if (ridges > 0) for (i = [0 : ridges - 1])
            rotate([0, 0, i * 360 / ridges]) translate([d / 2, 0, -1]) cylinder(d = d * 0.08, h = h + 2, $fn = 12);
        translate([0, 0, -1]) intersection() {
            cylinder(d = sd + 0.2, h = h - 2 + 1);
            if (dshaft) translate([-sd, -sd, -1]) cube([2 * sd, sd + sd * 0.25, h + 2]);
            else translate([-sd, -sd, -1]) cube([2 * sd, 2 * sd, h + 2]);
        }
        translate([0, d / 4, h]) cube([1.2, d / 2 - 1, 1.6], center = true);
    }
}

// print-in-place hinge (two leaves, alternating knuckles, pin attached to leaf A)
module nl_hinge(w, leaf, t, n, pin, clr, hd) {
    kd = pin + 2 * clr + 3.2;                // knuckle diameter
    seg = (w - (n - 1) * clr) / n;           // knuckle length
    // leaf A (-y side) + its knuckles + pin
    difference() {
        union() {
            translate([-w / 2, -(kd / 2 + leaf), 0]) cube([w, leaf - clr, t]);
            for (i = [0 : 2 : n - 1]) translate([-w / 2 + i * (seg + clr), 0, 0]) {
                translate([0, -(kd / 2 + clr), 0]) cube([seg, kd / 2 + clr, t]);
                translate([0, 0, kd / 2]) rotate([0, 90, 0]) cylinder(d = kd, h = seg);
            }
            translate([-w / 2, 0, kd / 2]) rotate([0, 90, 0]) cylinder(d = pin, h = w);
        }
        for (s = [-1, 1]) translate([s * w / 4, -(kd / 2 + leaf / 2), -1]) cylinder(d = hd, h = t + 2);
    }
    // leaf B (+y side) + its knuckles, bored for the pin
    difference() {
        union() {
            translate([-w / 2, kd / 2 + clr, 0]) cube([w, leaf - clr, t]);
            if (n > 1) for (i = [1 : 2 : n - 1]) translate([-w / 2 + i * (seg + clr), 0, 0]) {
                cube([seg, kd / 2 + clr + 0.01, t]);
                translate([0, 0, kd / 2]) rotate([0, 90, 0]) cylinder(d = kd, h = seg);
            }
        }
        translate([-w / 2 - 1, 0, kd / 2]) rotate([0, 90, 0]) cylinder(d = pin + 2 * clr, h = w + 2);
        for (s = [-1, 1]) translate([s * w / 4, kd / 2 + leaf / 2, -1]) cylinder(d = hd, h = t + 2);
    }
}

// snap-in cradle / pipe clip for a round item of diameter D, screwed down with two holes
module nl_mount(D, t, w, hd) {
    L = D + 2 * t + 6 * hd;
    difference() {
        union() {
            translate([-w / 2, -L / 2, 0]) cube([w, L, t]);
            translate([0, 0, t + D / 2]) rotate([0, 90, 0]) cylinder(d = D + 2 * t, h = w, center = true);
        }
        translate([0, 0, t + D / 2]) rotate([0, 90, 0]) cylinder(d = D, h = w + 2, center = true);
        translate([0, 0, t + 0.8 * D + D / 2]) cube([w + 2, 0.8 * D, D], center = true);  // snap opening
        for (s = [-1, 1]) translate([0, s * (L / 2 - 1.5 * hd), -1]) cylinder(d = hd, h = t + 2);
    }
}
"""

    def _scad_file(self, ctx: BuildContext, root: Entity, color_fallback: str | None, info: dict) -> str:
        v = self._vid(root)
        L = [f"// {_comment(root.name)} - generated by NLCL (cad module). Units: mm. Edit the parameters below.",
             f"// Render: F6 in OpenSCAD, export STL with F7, or: openscad -o {v}.stl {v}.scad",
             f"// Override: openscad -D \"{v}_<param>=<value>\" -o {v}.stl {v}.scad",
             "include <nlcl_cad_lib.scad>", ""]
        L += self._scad_entity(ctx, root, color_fallback, info)
        L += ["// what is rendered when this file is opened directly (print layout)", f"{v}_print();", ""]
        return "\n".join(L)

    def _assembly(self, ctx: BuildContext, roots: list[Entity], info: dict) -> str:
        L = [f"// {ctx.system.name} - CAD assembly generated by NLCL. Units: mm.",
             "// Each part file is 'use'd (modules only) and placed at its position / rotation.",
             "// Export everything as one STL: openscad -o assembly.stl assembly.scad", ""]
        for r in roots:
            L.append(f"use <{self._vid(r)}.scad>")
        L.append("")
        if not roots:
            L.append("// (no CAD parts yet)")
            L.append("cube(0.01);")
        # the first part without an explicit position stays at the origin (others are usually placed
        # relative to it); further unplaced parts are laid out in a row along +X so they do not overlap
        auto = {}
        unplaced = [r for r in roots if "position" not in r.props]
        extents = [self._placement(r)[0][0] + info.get(r.id, {}).get("bbox", [0])[0] / 2
                   for r in roots if "position" in r.props]
        if unplaced:
            extents.append(info.get(unplaced[0].id, {}).get("bbox", [20.0])[0] / 2)
        cursor = (max(extents) if extents else 0.0) + 10
        for r in unplaced[1:]:
            w = info.get(r.id, {}).get("bbox", [20.0])[0]
            auto[r.id] = cursor + w / 2
            cursor += w + 10
        placed: dict = {}
        stacked = {r.id for r in roots if r.props.get("on") and "position" not in r.props}
        for r in roots:
            if r.id in stacked:
                auto.pop(r.id, None)
        order = [r for r in roots if r.id not in stacked] + [r for r in roots if r.id in stacked]
        for r in order:
            pos, rot = self._placement(r)
            if r.id in auto:
                pos[0] += auto[r.id]
            tgt = r.props.get("on") if r.id in stacked else None
            if tgt in placed and tgt != r.id:
                tpos = placed[tgt]
                pos = [tpos[0] + pos[0], tpos[1] + pos[1],
                       tpos[2] + info.get(tgt, {}).get("bbox", [0, 0, 0])[2] + pos[2]]
            placed[r.id] = pos
            i = info.get(r.id, {})
            L.append(f"// {_comment(r.name)}: {i.get('kind', r.kind)}, bbox {' x '.join(_n(x) for x in i.get('bbox', []))} mm")
            L.append(f"translate([{', '.join(_n(x) for x in pos)}]) rotate([{', '.join(_n(x) for x in rot)}]) "
                     f"{self._vid(r)}_part();")
        L.append("")
        return "\n".join(L)

    # ==================================================================
    # CadQuery generation
    # ==================================================================
    CQ_HEAD = '''"""{title} - CadQuery model generated by NLCL. Units: mm.

Free alternative to the OpenSCAD file.  Requires CadQuery:  pip install cadquery
    python {fname}           -> writes STL + STEP next to this script
Open it in CQ-editor to view it interactively (show_object is used there).
"""
import math
import os

import cadquery as cq

HERE = os.path.dirname(os.path.abspath(__file__))


def rbox(w, d, h, r=0.0):
    """Box centred on X/Y from z=0 up, vertical edges rounded."""
    b = cq.Workplane("XY").box(w, d, h, centered=(True, True, False))
    r = min(r, w / 2 - 0.01, d / 2 - 0.01)
    if r > 0.05:
        b = b.edges("|Z").fillet(r)
    return b


def cyl(d, h, center=(0, 0, 0), axis="z", base=False):
    """Cylinder of diameter d, length h along axis, centred on center (or starting at z=0 if base)."""
    plane = {"x": "YZ", "y": "XZ", "z": "XY"}[axis]
    if base:
        return cq.Workplane(plane).circle(d / 2).extrude(h).translate(center)
    return cq.Workplane(plane).circle(d / 2).extrude(h / 2, both=True).translate(center)


def boxc(size, center):
    return cq.Workplane("XY").box(*size).translate(center)


def tube(od, id_, h):
    t = cyl(od, h, base=True)
    if id_ > 0:
        t = t.cut(cyl(id_, h + 2, center=(0, 0, -1), base=True))
    return t


def perim_at(a, b, s):
    if s < a:
        return (-a / 2 + s, -b / 2)
    if s < a + b:
        return (a / 2, -b / 2 + (s - a))
    if s < 2 * a + b:
        return (a / 2 - (s - a - b), b / 2)
    return (-a / 2, b / 2 - (s - 2 * a - b))


def hole_pts(w, d, n, inset):
    a, b = max(w - 2 * inset, 0), max(d - 2 * inset, 0)
    if n <= 0:
        return []
    if n == 1:
        return [(0, 0)]
    if n == 2:
        return [(-a / 2, 0), (a / 2, 0)]
    if n == 3:
        return [(-a / 2, -b / 2), (a / 2, -b / 2), (0, b / 2)]
    if n == 4:
        return [(x * a / 2, y * b / 2) for x in (-1, 1) for y in (-1, 1)]
    return [perim_at(a, b, i * 2 * (a + b) / n) for i in range(n)]


def gear(teeth, mod, thickness, bore):
    z = max(6, int(round(teeth)))
    rp = mod * z / 2
    ra = rp + mod
    rf = max(rp - 1.25 * mod, bore / 2 + 0.5 * mod)
    h = math.radians(90 / z)
    prof = [(rf, -1.25), (rp, -1.0), (ra, -0.55), (ra, 0.55), (rp, 1.0), (rf, 1.25)]
    pts = []
    for i in range(z):
        for r, k in prof:
            a = i * 2 * math.pi / z + k * h
            pts.append((r * math.cos(a), r * math.sin(a)))
    g = cq.Workplane("XY").polyline(pts).close().extrude(thickness)
    if bore > 0:
        g = g.cut(cyl(bore, thickness + 2, center=(0, 0, -1), base=True))
    return g


def place(wp, pos=(0, 0, 0), rot=(0, 0, 0)):
    """Rotate (x, y, z order, degrees) then translate, like OpenSCAD translate() rotate()."""
    for axis, ang in zip(((1, 0, 0), (0, 1, 0), (0, 0, 1)), rot):
        if ang:
            wp = wp.rotate((0, 0, 0), axis, ang)
    return wp.translate(tuple(pos))

'''

    def _cq_feat(self, f: dict) -> str:
        c = "(" + ", ".join(_n(x) for x in f["c"]) + ")"
        if f["shape"] == "box":
            return f"boxc(({', '.join(_n(x) for x in f['size'])}), {c})"
        return f"cyl({_n(f['d'])}, {_n(f['h'])}, center={c}, axis={f['axis']!r})"

    def _cq_entity(self, ctx: BuildContext, e: Entity, info: dict) -> list[str]:
        """def build_<v>() -> dict[name, Workplane]; children first."""
        v = self._vid(e)
        i = info.get(e.id)
        if i is None:
            return []
        k, P, bbox, extra, feats, unions = i["kind"], i["P"], i["bbox"], i["extra"], i["feats"], i["unions"]
        L = []
        for ch in unions:
            L += self._cq_entity(ctx, ch, info)
        L.append(f"P_{v} = {P!r}")
        L.append("")
        L.append("")
        L.append(f"def build_{v}():")
        L.append(f'    """{_comment(e.name).replace(chr(92), "/").replace(chr(34), chr(39))}: {k}. Returns {{name: solid}}."""')
        L.append(f"    P = P_{v}")
        approx = False
        if k in ("box", "cube"):
            L.append('    body = rbox(P["width"], P["depth"], P["height"], P["fillet"])')
            if "hole_diameter" in P:
                L.append('    body = body.cut(cyl(P["hole_diameter"], P["height"] + 2, center=(0, 0, -1), base=True))')
        elif k == "cylinder":
            L.append('    body = cyl(P["diameter"], P["height"], base=True)')
            L.append('    if P["hole_diameter"] > 0:')
            L.append('        body = body.cut(cyl(P["hole_diameter"], P["height"] + 2, center=(0, 0, -1), base=True))')
        elif k == "sphere":
            L.append('    body = cq.Workplane("XY").sphere(P["diameter"] / 2).translate((0, 0, P["diameter"] / 2))')
        elif k == "cone":
            L.append('    body = cq.Workplane("XY").add(cq.Solid.makeCone(P["diameter"] / 2, max(P["top_diameter"], 0) / 2, P["height"]))')
        elif k == "plate":
            L += ['    body = rbox(P["width"], P["depth"], P["thickness"], P["corner_radius"])',
                  '    for (x, y) in hole_pts(P["width"], P["depth"], P["hole_count"], P["hole_inset"]):',
                  '        body = body.cut(cyl(P["hole_diameter"], P["thickness"] + 2, center=(x, y, -1), base=True))']
        elif k in ("standoff", "spacer", "washer"):
            L += ['    if P["hex"]:',
                  '        body = cq.Workplane("XY").polygon(6, P["diameter"]).extrude(P["height"])',
                  '    else:',
                  '        body = cyl(P["diameter"], P["height"], base=True)',
                  '    if P["hole_diameter"] > 0:',
                  '        body = body.cut(cyl(P["hole_diameter"], P["height"] + 2, center=(0, 0, -1), base=True))']
        elif k == "tube":
            L.append('    body = tube(P["diameter"], P["diameter"] - 2 * P["wall"], P["length"])')
        elif k == "gear":
            L.append('    body = gear(P["teeth"], P["module"], P["thickness"], P["bore"])')
        elif k == "bracket":
            L += ['    a, w, b, t, n = P["length"], P["width"], P["height"], P["thickness"], P["holes_per_leg"]',
                  '    body = boxc((a, w, t), (a / 2, 0, t / 2)).union(boxc((t, w, b), (t / 2, 0, b / 2)))',
                  '    for i in range(n):',
                  '        body = body.cut(cyl(P["hole_diameter"], t + 2, center=(t + (a - t) * (i + 0.5) / n, 0, t / 2)))',
                  '        body = body.cut(cyl(P["hole_diameter"], t + 2, center=(t / 2, 0, t + (b - t) * (i + 0.5) / n), axis="x"))']
        elif k == "enclosure":
            L += ['    W, D, H, wall, r = P["width"], P["depth"], P["height"], P["wall"], P["fillet"]',
                  '    base_h = H - wall' + ('' if extra.get("lid", True) else ' * 0  # open-top box (no lid)'),
                  '    base = rbox(W, D, base_h, r).cut(rbox(W - 2 * wall, D - 2 * wall, base_h, max(r - wall, 0)).translate((0, 0, wall)))',
                  '    lw, ld = W - 2 * (wall + P["fit"]), D - 2 * (wall + P["fit"])',
                  '    lip = rbox(lw, ld, P["lip_height"], max(r - wall, 0)).cut(',
                  '        rbox(lw - 2 * P["lip_thickness"], ld - 2 * P["lip_thickness"], P["lip_height"] + 2).translate((0, 0, -1)))',
                  '    lid = rbox(W, D, wall, r).translate((0, 0, base_h)).union(lip.translate((0, 0, base_h - P["lip_height"])))']
        else:
            approx = True
            bx, by, bz = bbox
            L.append(f"    # NOTE: '{k}' has no CadQuery template yet - bounding-box stand-in; use {v}.scad for the real shape")
            L.append(f"    body = rbox({_n(bx)}, {_n(by)}, {_n(bz)})")
        # features
        for target in (("base", "lid") if k == "enclosure" else ("body",)):
            var = target
            for f in feats:
                if f["target"] != target:
                    continue
                L.append(f"    {var} = {var}.{'union' if f['op'] == 'add' else 'cut'}({self._cq_feat(f)})  # {_comment(f['label'])}")
        # union children
        floor = P.get("wall", 0) if k == "enclosure" else bbox[2]
        tgt = "base" if k == "enclosure" else "body"
        for ch in unions:
            cpos, crot = self._placement(ch, (0.0, 0.0, floor))
            L.append(f"    for _s in build_{self._vid(ch)}().values():  # child {_comment(ch.name)}")
            L.append(f"        {tgt} = {tgt}.union(place(_s, ({', '.join(_n(x) for x in cpos)}), "
                     f"({', '.join(_n(x) for x in crot)})))")
        if k == "enclosure":
            if extra.get("lid", True):
                L.append(f"    return {{{(v + '_base')!r}: base, {(v + '_lid')!r}: lid}}")
            else:
                L.append(f"    return {{{(v + '_base')!r}: base}}")
        else:
            L.append(f"    return {{{v!r}: body}}")
        L.append("")
        L.append("")
        if approx:
            info[e.id]["cq_approx"] = True
        return L

    def _cq_file(self, ctx: BuildContext, root: Entity, info: dict) -> str:
        v = self._vid(root)
        L = [self.CQ_HEAD.replace("{title}", _comment(root.name)).replace("{fname}", f"{v}_cadquery.py")]
        L += self._cq_entity(ctx, root, info)
        L += [
            f"result = build_{v}()",
            "",
            'if "show_object" in globals():  # CQ-editor',
            "    for _name, _shape in result.items():",
            "        show_object(_shape, name=_name)  # noqa: F821",
            'elif __name__ == "__main__":',
            "    for _name, _shape in result.items():",
            '        cq.exporters.export(_shape, os.path.join(HERE, _name + ".stl"))',
            '        cq.exporters.export(_shape, os.path.join(HERE, _name + ".step"))',
            '        print("wrote", _name + ".stl", "and", _name + ".step")',
            "",
        ]
        return "\n".join(L)

    # ==================================================================
    # channel bridge
    # ==================================================================
    @staticmethod
    def _bridge_rules(ent: Entity, peer: Entity, fields: list) -> list[dict]:
        """'when target changes / when dial receives a message, set height to target' on a CAD part:
        rules the bridge applies to incoming messages (field -> geometry parameter)."""
        rules = []
        pnames = {str(peer.name).lower(), peer.ident, str(peer.id).lower()}
        for b in ent.behaviors or []:
            if not isinstance(b, dict):
                continue
            on = str(b.get("on") or "")
            kind, _, arg = on.partition(":")
            arg = arg.lower()
            if kind == "message" and arg not in ("", "any", "*") and arg not in pnames and ident(arg) not in pnames:
                continue
            if kind == "change" and arg not in fields:
                continue
            if kind not in ("message", "change"):
                continue
            for a in b.get("do") or []:
                if isinstance(a, dict) and a.get("op") == "set" and a.get("prop") and a.get("value") is not None:
                    expr = str(a["value"])
                    for pn in pnames:  # 'ctl.target' -> 'target'
                        expr = re.sub(rf"\b{re.escape(pn)}\.", "", expr, flags=re.I)
                    rules.append({"on": on, "if": b.get("if"), "param": str(a["prop"]), "expr": expr})
        return rules

    def _bridge(self, ctx: BuildContext, info: dict) -> str | None:
        local = {e.id: e for e in ctx.entities}
        chans = []
        for c in ctx.channels:
            if c.protocol in ("spi", "i2c", "gpio", "wire"):
                continue
            for ent, direction in ((c.src, "out"), (c.dst, "in")):
                if ent.id not in local:
                    continue
                root = ent
                seen = set()
                while root.parent in local and root.id not in seen:
                    seen.add(root.id)
                    root = local[root.parent]
                cfg = dict(c.config)
                ch = {"id": c.id, "protocol": c.protocol, "direction": direction,
                      "entity": self._vid(ent), "file": self._vid(root), "from": c.src.name,
                      "peer": c.peer(ent).name, "peer_domain": c.peer(ent).domain,
                      "fields": list(c.fields), "config": cfg}
                if direction == "in":
                    ch["rules"] = self._bridge_rules(local.get(ent.id, ent), c.peer(ent), list(c.fields))
                    pp = c.peer(ent).props or {}
                    ch["samples"] = {f: pp[f] for f in c.fields
                                     if isinstance(pp.get(f), (int, float, bool))}  # peer's values for --simulate
                chans.append(ch)
        if not chans:
            return None
        params = {}
        for e in ctx.entities:
            i = info.get(e.id)
            if i:
                params[self._vid(e)] = {k: v for k, v in i["P"].items()}
                for k, val in e.props.items():
                    if isinstance(val, (int, float)) and not isinstance(val, bool) and k not in params[self._vid(e)]:
                        params[self._vid(e)].setdefault("_" + k, val)
        # Python literals (not JSON: booleans must be True/False in the generated script)
        return BRIDGE_TEMPLATE.replace("__CHANNELS__", pprint.pformat(chans, indent=1, width=110, sort_dicts=False)) \
            .replace("__PARAMS__", pprint.pformat(params, indent=1, width=110, sort_dicts=False)) \
            .replace("__SYSTEM__", repr(ctx.system.name))

    # ==================================================================
    # README
    # ==================================================================
    def _readme(self, ctx: BuildContext, roots: list[Entity], info: dict, has_bridge: bool) -> str:
        L = [f"# {ctx.system.name} - CAD parts", "",
             "Generated by NLCL. Units are **millimetres**; every part is centred on X/Y and sits on Z = 0.", "",
             "## Parts", "", "| part | type | bounding box (mm) | files |", "|---|---|---|---|"]
        for r in roots:
            i = info.get(r.id, {})
            v = self._vid(r)
            files = f"`{v}.scad`, `{v}_cadquery.py`"
            L.append(f"| {r.name} | {i.get('kind', r.kind)} | {' x '.join(_n(x) for x in i.get('bbox', []))} | {files} |")
        L += [""]
        for r in roots:
            i = info.get(r.id, {})
            ex = i.get("extra", {})
            details = []
            if ex.get("board"):
                bd = BOARDS[ex["board"]]
                details.append(f"sized for the {bd['title']} ({_n(bd['l'])} x {_n(bd['w'])} mm) with "
                               f"{'mounting bosses and ' if ex.get('bosses') and bd['holes'] else ''}"
                               f"connector cut-outs ({', '.join(p[0] for p in bd['ports'])}) - check them against "
                               "your exact board revision before printing")
            if ex.get("vents"):
                details.append(f"{ex['vents']} ventilation slots per long side")
            feats = [f["label"] for f in i.get("feats", []) if f["op"] == "cut" and "slot" not in f["label"]
                     and "pilot" not in f["label"]]
            if feats:
                details.append("cut-outs: " + "; ".join(feats))
            if i.get("unions"):
                details.append("attached: " + ", ".join(c.name for c in i["unions"]))
            if i.get("cq_approx"):
                details.append("CadQuery file is a bounding-box stand-in for this type (use the .scad file)")
            if details:
                L.append(f"- **{r.name}**: " + "; ".join(details))
        L += ["", "## Open / render with free tools", "",
              "1. Install **OpenSCAD** (https://openscad.org, free).",
              "2. Open `assembly.scad` to see everything placed together, or a single `<part>.scad` for its print layout",
              "   (enclosures show base + flipped lid side by side). F5 preview, F6 render, F7 export STL.",
              "3. Command line:", "",
              "```sh",
              "openscad -o assembly.stl assembly.scad"]
        for r in roots:
            v = self._vid(r)
            L.append(f"openscad -o {v}.stl {v}.scad")
        if roots:
            v = self._vid(roots[0])
            L.append(f'openscad -D "{v}_fn=128" -o {v}_fine.stl {v}.scad   # override any <part>_<param>')
        L += ["```", "",
              "Every parameter is a top-level variable `<part>_<name>` at the top of the part file "
              "(e.g. `case_wall`, `case_fillet`). Edit it, or use the OpenSCAD Customizer.", "",
              "### CadQuery (Python, free)", "", "```sh", "pip install cadquery"]
        for r in roots:
            L.append(f"python {self._vid(r)}_cadquery.py   # writes STL + STEP")
        L += ["```", "",
              "STEP files open in **FreeCAD** (free) for further editing / drawings.", "",
              "## Printing", "",
              "Slice the STL with **PrusaSlicer**, **Cura** or **OrcaSlicer** (all free). Typical settings: PLA or PETG, "
              "0.2 mm layers, 3 perimeters, 20 % infill. Print enclosure lids top-face down (the print layout already "
              "flips them). Holes are modelled at nominal size + clearance (M3 clearance 3.2-3.4 mm, M3 tap 2.5-2.7 mm).", ""]
        if has_bridge:
            L += ["## Live link to other modules", "",
                  "`cad_bridge.py` (Python stdlib) connects these parts to the NLCL channels:", "",
                  "- **incoming** channels write `<part>_live.scad` (includes the part and overrides the matching "
                  "`<part>_<field>` parameters) and `live_values.json`; with `--export` and OpenSCAD on PATH it "
                  "re-exports `<part>_live.stl` after every change;",
                  "- **outgoing** channels publish the part's parameters (e.g. dimensions) every `interval_ms`.", "",
                  "```sh",
                  "python ../python/main.py          # starts the NLCL hub on :8765 (HTTP channels)",
                  "python cad_bridge.py              # real transports",
                  "python cad_bridge.py --simulate --seconds 5   # offline demo",
                  "```", "",
                  "| channel | protocol | direction | part | fields |", "|---|---|---|---|---|"]
            for c in ctx.channels:
                for ent in (c.src, c.dst):
                    if ent.id in {x.id for x in ctx.entities}:
                        L.append(f"| {c.id} | {c.protocol} | {'out' if ent.id == c.src.id else 'in'} "
                                 f"({c.peer(ent).name}, {c.peer(ent).domain}) | {ent.name} | {', '.join(c.fields)} |")
            L.append("")
        notes = []
        for e in ctx.entities:
            for b in e.behaviors or []:
                notes.append(f"- {e.name}: on `{b.get('on')}` -> " + "; ".join(describe_action(a) for a in b.get("do", [])))
        if notes:
            L += ["## Behaviours (not simulated)", "",
                  "CAD output is static geometry. These behaviours are kept as `// note:` comments in the .scad files "
                  "(numeric `move` actions on start shift the placement):", ""] + notes + [""]
        return "\n".join(L)

    # ==================================================================
    # entry points
    # ==================================================================
    @staticmethod
    def _normalized(ctx: BuildContext) -> BuildContext:
        """Copy of ctx whose entities have tidy props ('vents_true': True -> 'vents': True)."""
        ents = []
        for e in ctx.entities:
            props = {}
            for k, val in dict(e.props or {}).items():
                k = str(k)
                m = re.fullmatch(r"(\w+?)_(true|yes|on|false|no|off)", k)
                m2 = re.fullmatch(r"(side|face|location|shape|color|colour|board|material|quality)_(\w+)", k)
                m3 = re.fullmatch(r"(?:on_|at_)?(?:the_)?(\w+?)(?:_(?:side|face|wall|panel|end))?", k)
                if m2 and val is True and m2.group(1) not in e.props:
                    props[m2.group(1)] = m2.group(2)   # 'side_front': True -> side 'front'
                elif (m3 and val is True and m3.group(1) in SIDE_WORDS and m3.group(1) not in ("lid", "base")
                      and not any(s in e.props for s in ("side", "face", "location"))):
                    props["side"] = SIDE_WORDS[m3.group(1)]   # 'front_side' / 'on_top': True -> side
                    props[k] = val
                elif m and isinstance(val, bool) and m.group(1) not in e.props:
                    props[m.group(1)] = m.group(2) in ("true", "yes", "on")
                else:
                    props[k] = val
            # aliases the user may type ("6 holes", "outer diameter 12"): an explicit alias wins over the
            # kind's default that the core filled in at creation
            dflt = CadModule.kinds.get(str(e.kind or "object").lower(), {})
            for alias, canon in PROP_ALIASES.items():
                av = props.get(alias)
                if not isinstance(av, (int, float)) or isinstance(av, bool) or av <= 0:
                    continue
                if canon in ("hole_count", "teeth") and float(av) != int(av):
                    continue  # "4 holes of 3.2 mm" may leave holes: 3.2 - a size, not a count
                if canon == "hole_diameter" and ident(e.kind) in ("tube", "pipe"):
                    continue  # tubes use inner_diameter directly
                if canon not in props or (canon in dflt and props[canon] == dflt[canon]):
                    props[canon] = int(av) if canon in ("hole_count", "teeth", "knuckles") else av
            # board named by a flag key ('arduino_uno': True from "for an arduino uno")
            if "board" not in props:
                for k, val in (e.props or {}).items():
                    nk = re.sub(r"[^a-z0-9]", "", str(k).lower())
                    if val is True and any(frag in nk for frag, _bk in BOARD_MATCH):
                        props["board"] = str(k)
                        break
            # "put peg on the base" (core: 'put_on_base': True) -> stack on that part in the assembly
            if "on" not in props:
                names = {}
                for o in ctx.entities:
                    if o.id != e.id:
                        names[ident(str(o.name))] = o.id
                        names[o.ident] = o.id
                for k, val in (e.props or {}).items():
                    m4 = re.fullmatch(r"(?:put_|place_|sit_|sits_|stack_|stacked_|mount_|mounted_|set_)?"
                                      r"(?:on|onto|atop|above)(?:_top_of)?_(?:the_|a_)?(\w+)", str(k))
                    if m4 and val is True and m4.group(1) in names:
                        props["on"] = names[m4.group(1)]
                        break
            # "when started, set wall to 3": literal start-up values are the initial geometry
            for b in e.behaviors or []:
                if not isinstance(b, dict) or not _is_start(b):
                    continue
                for a in b.get("do") or []:
                    if not isinstance(a, dict) or a.get("op") != "set" or not a.get("prop"):
                        continue
                    val = a.get("value")
                    if isinstance(val, str) and re.fullmatch(r"\s*-?\d+(\.\d+)?\s*", val):
                        val = float(val) if "." in val else int(val)
                    if isinstance(val, (bool, int, float, list)) or (
                            isinstance(val, str) and str(a["prop"]) in ("color", "colour", "board", "side")):
                        props[str(a["prop"])] = val
            ents.append(Entity(id=e.id, name=e.name or e.id, domain=e.domain, kind=e.kind or "object",
                               props=props, behaviors=list(e.behaviors or []), parent=e.parent))
        return BuildContext(ctx.system, ctx.domain, ents, ctx.channels, ctx.outdir, ctx.project, ctx.llm)

    def generate(self, ctx: BuildContext) -> dict[str, str]:
        ctx = self._normalized(ctx)
        files: dict[str, str] = {"nlcl_cad_lib.scad": self.SCAD_LIB}
        info: dict = {}
        roots = ctx.roots()
        for idx, r in enumerate(roots):
            fallback = PALETTE[idx % len(PALETTE)]
            v = self._vid(r)
            try:
                files[f"{v}.scad"] = self._scad_file(ctx, r, fallback, info)
            except Exception as ex:  # never fail the whole build because of one odd part
                files[f"{v}.scad"] = (f"// {_comment(r.name)}: generation failed ({_comment(ex)}); placeholder cube\n"
                                      f"module {v}_part() {{ cube(10); }}\nmodule {v}_print() {{ {v}_part(); }}\n{v}_print();\n")
                info[r.id] = dict(kind="box", P={}, bbox=[10, 10, 10], extra={}, feats=[], unions=[])
            try:
                files[f"{v}_cadquery.py"] = self._cq_file(ctx, r, info)
            except Exception as ex:
                files[f"{v}_cadquery.py"] = f"# {r.name}: CadQuery generation failed ({_comment(ex)})\n"
        files["assembly.scad"] = self._assembly(ctx, roots, info)
        bridge = None
        try:
            bridge = self._bridge(ctx, info)
        except Exception:
            bridge = None
        if bridge:
            files["cad_bridge.py"] = bridge
        files["README.md"] = self._readme(ctx, roots, info, bool(bridge))
        return files

    def run(self, ctx: BuildContext) -> RunResult | None:
        if not (ctx.outdir / "assembly.scad").exists():
            return RunResult(False, "build first (assembly.scad missing)")
        exe = self.which("openscad", "openscad.com", "openscad.exe")
        if exe:
            return self.sh([exe, "-o", "assembly.stl", "assembly.scad"], ctx.outdir, timeout=180)
        # no OpenSCAD: static check of the generated .scad files (+ a short offline bridge run)
        import sys
        problems, parts = [], 0
        for f in sorted(ctx.outdir.glob("*.scad")):
            if f.name.endswith("_live.scad"):
                continue
            text = re.sub(r'"(?:\\.|[^"\\])*"', '""', f.read_text(encoding="utf-8"))
            text = re.sub(r"//[^\n]*", "", text)
            for o, c in ("()", "[]", "{}"):
                if text.count(o) != text.count(c):
                    problems.append(f"{f.name}: unbalanced {o}{c} ({text.count(o)} vs {text.count(c)})")
            parts += 1
        lib = (ctx.outdir / "nlcl_cad_lib.scad")
        defined = set(re.findall(r"\bmodule\s+(\w+)", lib.read_text(encoding="utf-8"))) if lib.exists() else set()
        for f in ctx.outdir.glob("*.scad"):
            defined |= set(re.findall(r"\bmodule\s+(\w+)", f.read_text(encoding="utf-8")))
        for f in ctx.outdir.glob("*.scad"):
            body = re.sub(r"//[^\n]*", "", f.read_text(encoding="utf-8"))
            for m in set(re.findall(r"\b(nl_\w+|\w+_part|\w+_print)\s*\(", body)):
                if m not in defined and not re.search(rf"\bfunction\s+{m}\b", body + (lib.read_text(encoding='utf-8') if lib.exists() else "")):
                    problems.append(f"{f.name}: module {m}() is not defined")
        out = [f"OpenSCAD not found - static check of {parts} .scad file(s): "
               + ("OK" if not problems else f"{len(problems)} problem(s)")] + problems
        if (ctx.outdir / "cad_bridge.py").exists():
            r = self.sh([sys.executable, "cad_bridge.py", "--simulate", "--seconds", "3"], ctx.outdir, timeout=20)
            out += ["", "$ python cad_bridge.py --simulate --seconds 3", r.output]
            if not r.ok:
                problems.append("bridge failed")
        out.append("Install OpenSCAD (openscad.org) to render STL: openscad -o assembly.stl assembly.scad")
        return RunResult(not problems, "\n".join(out), "static check")


# ----------------------------------------------------------------------------
# cad_bridge.py template (stdlib only; pyserial / paho-mqtt if installed)
# ----------------------------------------------------------------------------
BRIDGE_TEMPLATE = r'''#!/usr/bin/env python3
"""CAD <-> NLCL channel bridge for system __SYSTEM__ (generated by NLCL).

Incoming channels: received fields override part parameters -> <part>_live.scad
                   (+ live_values.json, and <part>_live.stl with --export when OpenSCAD is on PATH).
Outgoing channels: the part's parameters are published every interval_ms.

Messages are flat JSON {"channel": id, "from": name, field: value, ...}.
HTTP/websocket channels use the NLCL hub (python/main.py): POST/GET {hub_url}/channel/<id>.

    python cad_bridge.py                       # real transports
    python cad_bridge.py --simulate            # no network: fake incoming values
    python cad_bridge.py --seconds 10 --export # stop after 10 s, re-export STL on change
"""
import argparse
import json
import os
import random
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CHANNELS = __CHANNELS__
# current geometry parameters per part (names match <part>_<param> in the .scad files)
PARAMS = __PARAMS__

values = {}            # part -> {param: value} received over channels
lock = threading.Lock()


def scad_literal(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(scad_literal(x) for x in v) + "]"
    return json.dumps(str(v))


def write_live(ch, export):
    part, fname = ch["entity"], ch["file"]
    with lock:
        vals = dict(values.get(part, {}))
    lines = ["// live parameters received over NLCL channels - generated by cad_bridge.py",
             "// " + time.strftime("%Y-%m-%d %H:%M:%S"),
             "include <%s.scad>" % fname]
    for k, v in vals.items():
        if k in PARAMS.get(part, {}):
            lines.append("%s_%s = %s;  // from %s" % (part, k, scad_literal(v), ch["peer"]))
        else:
            lines.append("// %s = %r (not a geometry parameter of %s)" % (k, v, part))
    path = os.path.join(HERE, fname + "_live.scad")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    with open(os.path.join(HERE, "live_values.json"), "w", encoding="utf-8") as f:
        json.dump(values, f, indent=2)
    print("[%s] %s <- %s" % (ch["id"], part, vals), flush=True)
    exe = shutil.which("openscad")
    if export and exe:
        try:
            subprocess.run([exe, "-o", fname + "_live.stl", fname + "_live.scad"], cwd=HERE,
                           timeout=180, capture_output=True)
            print("    exported %s_live.stl" % fname, flush=True)
        except (OSError, subprocess.TimeoutExpired) as e:
            print("    openscad failed: %s" % e, flush=True)


def receive(ch, msg, export):
    """Copy fields of an incoming message onto the part parameters."""
    if not isinstance(msg, dict):
        return
    got = {k: v for k, v in msg.items() if k in ch["fields"] and not k.startswith("_")}
    if not got:
        return
    # behaviours of the part ("when target changes, set height to target") map fields -> parameters
    with lock:
        env = dict(PARAMS.get(ch["entity"], {}))
        env.update(values.get(ch["entity"], {}))
    env.update(got)
    for rule in ch.get("rules", []):
        trig = rule.get("on", "")
        if trig.startswith("change:") and trig[7:] not in got:
            continue
        try:
            if rule.get("if") and not evaluate(rule["if"], env):
                continue
            got[rule["param"]] = env[rule["param"]] = evaluate(rule["expr"], env)
        except Exception as e:  # an expression the bridge cannot evaluate: report, keep going
            print("    rule %s = %s skipped (%s)" % (rule["param"], rule["expr"], e), flush=True)
    with lock:
        values.setdefault(ch["entity"], {}).update(got)
    write_live(ch, export)


def evaluate(expr, env):
    """Tiny safe evaluator for the C-like behaviour expressions (numbers, fields, + - * / %, compares)."""
    import ast
    import re as _re
    s = str(expr).strip()
    s = s.replace("&&", " and ").replace("||", " or ")
    s = _re.sub(r"!(?!=)", " not ", s)
    s = _re.sub(r"\btrue\b", "True", _re.sub(r"\bfalse\b", "False", s))
    tree = ast.parse(s, mode="eval")
    ops = {ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b, ast.Mult: lambda a, b: a * b,
           ast.Div: lambda a, b: a / b, ast.Mod: lambda a, b: a % b, ast.Pow: lambda a, b: a ** b,
           ast.Lt: lambda a, b: a < b, ast.LtE: lambda a, b: a <= b, ast.Gt: lambda a, b: a > b,
           ast.GtE: lambda a, b: a >= b, ast.Eq: lambda a, b: a == b, ast.NotEq: lambda a, b: a != b}

    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float, bool, str)):
            return n.value
        if isinstance(n, ast.Name):
            if n.id in env:
                return env[n.id]
            raise NameError(n.id)
        if isinstance(n, ast.Attribute):  # peer.field -> field
            return ev(ast.Name(id=n.attr))
        if isinstance(n, ast.BinOp) and type(n.op) in ops:
            return ops[type(n.op)](float(ev(n.left)), float(ev(n.right)))
        if isinstance(n, ast.UnaryOp):
            v = ev(n.operand)
            return (not v) if isinstance(n.op, ast.Not) else (-float(v) if isinstance(n.op, ast.USub) else v)
        if isinstance(n, ast.BoolOp):
            vals = [ev(x) for x in n.values]
            return all(vals) if isinstance(n.op, ast.And) else any(vals)
        if isinstance(n, ast.Compare):
            left = ev(n.left)
            for op, c in zip(n.ops, n.comparators):
                right = ev(c)
                if type(op) not in ops or not ops[type(op)](float(left), float(right)):
                    return False
                left = right
            return True
        raise ValueError("unsupported expression")
    v = ev(tree)
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return v


def payload(ch):
    p = dict(PARAMS.get(ch["entity"], {}))
    with lock:
        p.update(values.get(ch["entity"], {}))  # parameters received over channels win
    out = {"channel": ch["id"], "from": ch["from"]}
    for f in ch["fields"]:
        out[f] = p.get(f, p.get("_" + f, 0))
    return out


# ------------------------------------------------------------------ transports
def http_loop(ch, args, stop):
    url = ch["config"].get("hub_url", "http://localhost:8765").rstrip("/") + ch["config"].get("path", "/channel/" + ch["id"])
    url = url.replace("//localhost:", "//127.0.0.1:")  # avoids a ~2 s IPv6 fallback per request on Windows
    period = max(0.2, ch["config"].get("interval_ms", 1000) / 1000.0)
    seen = None
    while not stop.is_set():
        try:
            if ch["direction"] == "out":
                data = json.dumps(payload(ch)).encode()
                urllib.request.urlopen(urllib.request.Request(url, data, {"Content-Type": "application/json"}), timeout=3).read()
                print("[%s] -> hub %s" % (ch["id"], payload(ch)), flush=True)
            else:
                msg = json.loads(urllib.request.urlopen(url, timeout=3).read() or b"{}")
                if msg and msg.get("_t") != seen:
                    seen = msg.get("_t")
                    receive(ch, msg, args.export)
        except (OSError, ValueError) as e:
            print("[%s] hub %s unreachable (%s) - start python/main.py" % (ch["id"], url, e), flush=True)
        stop.wait(period)


def mqtt_loop(ch, args, stop):
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        print("[%s] paho-mqtt not installed (pip install paho-mqtt)" % ch["id"], flush=True)
        return
    cfg = ch["config"]
    topic = cfg.get("topic", "nlcl/" + ch["id"])
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2) if hasattr(mqtt, "CallbackAPIVersion") else mqtt.Client()
        client.on_message = lambda c, u, m: receive(ch, json.loads(m.payload.decode() or "{}"), args.export)
        client.connect(cfg.get("broker", "broker.hivemq.com"), int(cfg.get("broker_port", 1883)), 30)
        if ch["direction"] == "in":
            client.subscribe(topic)
        client.loop_start()
    except (OSError, ValueError) as e:
        print("[%s] mqtt broker unreachable: %s" % (ch["id"], e), flush=True)
        return
    period = max(0.2, cfg.get("interval_ms", 1000) / 1000.0)
    while not stop.is_set():
        if ch["direction"] == "out":
            client.publish(topic, json.dumps(payload(ch)))
        stop.wait(period)
    client.loop_stop()


def serial_loop(ch, args, stop):
    try:
        import serial
        import serial.tools.list_ports
    except ImportError:
        print("[%s] pyserial not installed (pip install pyserial)" % ch["id"], flush=True)
        return
    cfg = ch["config"]
    port = os.environ.get("NLCL_SERIAL_PORT") or (None if cfg.get("port", "auto") == "auto" else cfg.get("port"))
    if not port:
        ports = [p.device for p in serial.tools.list_ports.comports()]
        port = ports[0] if ports else None
    if not port:
        print("[%s] no serial port found" % ch["id"], flush=True)
        return
    ser = serial.Serial(port, int(cfg.get("baud", 115200)), timeout=0.2)
    period = max(0.2, cfg.get("interval_ms", 1000) / 1000.0)
    nxt = 0.0
    while not stop.is_set():
        if ch["direction"] == "out" and time.monotonic() >= nxt:
            nxt = time.monotonic() + period
            ser.write((json.dumps(payload(ch)) + "\n").encode())
        line = ser.readline().decode(errors="replace").strip()
        if line.startswith("{") and ch["direction"] == "in":
            try:
                receive(ch, json.loads(line), args.export)
            except ValueError:
                pass


def udp_loop(ch, args, stop):
    port = int(ch["config"].get("port", 9876))
    period = max(0.2, ch["config"].get("interval_ms", 1000) / 1000.0)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(0.5)
    if ch["direction"] == "in":
        s.bind(("0.0.0.0", port))
    while not stop.is_set():
        if ch["direction"] == "out":
            s.sendto((json.dumps(payload(ch)) + "\n").encode(), ("127.0.0.1", port))
            stop.wait(period)
        else:
            try:
                data, _ = s.recvfrom(65536)
                receive(ch, json.loads(data.decode() or "{}"), args.export)
            except (socket.timeout, ValueError):
                pass


def simulate_loop(ch, args, stop):
    """Offline demo: fake incoming values around the current parameters."""
    period = max(0.2, ch["config"].get("interval_ms", 1000) / 1000.0)
    p = PARAMS.get(ch["entity"], {})
    while not stop.is_set():
        if ch["direction"] == "in":
            msg = {"channel": ch["id"], "from": ch["peer"]}
            for f in ch["fields"]:
                base = ch.get("samples", {}).get(f, p.get(f, p.get("_" + f, 10.0)))
                if isinstance(base, bool):
                    msg[f] = random.random() < 0.5
                elif isinstance(base, (int, float)):
                    base = base or 20.0  # a zero start value would never change
                    val = base * random.uniform(0.9, 1.1)
                    msg[f] = round(val, 2) if isinstance(base, float) or abs(base) < 20 else int(round(val))
                else:
                    msg[f] = base
            receive(ch, msg, args.export)
        else:
            print("[%s] (simulated) -> %s" % (ch["id"], payload(ch)), flush=True)
        stop.wait(period)


LOOPS = {"http": http_loop, "websocket": http_loop, "mqtt": mqtt_loop, "serial": serial_loop, "uart": serial_loop,
         "udp": udp_loop}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--simulate", action="store_true", help="no network/hardware: fake incoming values")
    ap.add_argument("--seconds", type=float, default=0, help="stop after N seconds (0 = until Ctrl+C)")
    ap.add_argument("--export", action="store_true", help="re-export <part>_live.stl with OpenSCAD on change")
    args = ap.parse_args()
    stop = threading.Event()
    threads = []
    for ch in CHANNELS:
        fn = simulate_loop if args.simulate else LOOPS.get(ch["protocol"])
        if fn is None:
            print("[%s] protocol %s is not handled by the CAD bridge" % (ch["id"], ch["protocol"]), flush=True)
            continue
        t = threading.Thread(target=fn, args=(ch, args, stop), daemon=True)
        t.start()
        threads.append(t)
    print("cad bridge: %d channel(s) (Ctrl+C to stop)" % len(threads), flush=True)
    t0 = time.monotonic()
    try:
        while threads and (args.seconds <= 0 or time.monotonic() - t0 < args.seconds):
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    stop.set()
    print("cad bridge stopped", flush=True)


if __name__ == "__main__":
    main()
'''

MODULE = CadModule()
