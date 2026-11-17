"""Hardware domain: synthesizable Verilog-2005 RTL, testbench and Yosys script.

Every hardware entity becomes one module (``rtl/<ident>.v``) with a single
clocked ``always @(posedge clk)`` block (synchronous active-high reset):

    reset values -> free-running timers / synchronisers -> kind core logic
    -> values received from channels -> user behaviours (last NBA wins)

``rtl/top.v`` instantiates every entity and wires the links:

* hardware <-> hardware (any protocol)  : parallel wires, same-named fields
* embedded -> hardware via spi / i2c    : ``nlcl_spi_slave`` / ``nlcl_i2c_slave``
  (frame 0xA5, N, N x int16 big-endian, field order -- same as the embedded module)
* gpio / wire with another domain       : one pin per field (value != 0)
* serial / http / mqtt / udp / websocket: a UART carrying the NLCL flat JSON
  line ``{"channel":..,"from":..,<field>:<int>}\\n`` (``host_bridge.py`` relays
  it to the hub / broker when the peer is not on the serial line itself)

``sim/tb_top.v`` (Icarus / Verilator), ``synth.ys`` (Yosys) and a README are
generated alongside.  Nothing here ever raises on odd input: unknown kinds
become a register bank and unknown names become (documented) registers.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

from ..ir import Entity, describe_action, ident
from .base import BuildContext, Channel, DomainModule, RunResult

ASSETS = Path(__file__).parent / "_assets" / "hardware"

VKEYWORDS = set("""always and assign automatic begin buf bufif0 bufif1 case casex casez cell cmos config deassign
default defparam design disable edge else end endcase endconfig endfunction endgenerate endmodule endprimitive
endspecify endtable endtask event for force forever fork function generate genvar highz0 highz1 if ifnone incdir
include initial inout input instance integer join large liblist library localparam macromodule medium module nand
negedge nmos nor noshowcancelled not notif0 notif1 or output parameter pmos posedge primitive pull0 pull1 pulldown
pullup pulsestyle_onevent pulsestyle_ondetect rcmos real realtime reg release repeat rnmos rpmos rtran rtranif0
rtranif1 scalared showcancelled signed small specify specparam strong0 strong1 supply0 supply1 table task time tran
tranif0 tranif1 tri tri0 tri1 triand trior trireg unsigned use uwire vectored wait wand weak0 weak1 while wire wor
xnor xor logic bit byte int""".split()) | set("""shortint longint string class interface package program property
sequence final type var void return break continue struct union enum typedef import export extends const static virtual
local protected rand randc priority unique chandle bind clocking constraint covergroup do foreach inside new null super
this with wildcard assert assume cover expect alias before context dist solve tagged iff intersect throughout within
modport ref restrict soft let checker global implies nettype interconnect untyped shortreal""".split())
RESERVED = {"clk", "rst", "top", "tb_top"}
PARAM_PROPS = {"width", "clock_hz", "baud", "interval_ms", "depth", "stable_ms", "divide_by", "frequency_hz",
               "states", "init", "active_low", "pin", "pins", "board", "address", "subtype", "for_entity",
               "position", "rotation", "scale", "size", "color", "label", "model", "units", "shape"}
BUS_TO_WIRE = {"wire", "gpio"}
SIM_CLOCK_HZ = 1_000_000

# kind synonyms -> canonical kind
KIND_ALIAS = {
    "multiplexer": "mux", "state_machine": "fsm", "statemachine": "fsm", "led_driver": "blinker",
    "memory": "ram", "bram": "ram", "dff": "flipflop", "flip_flop": "flipflop", "shifter": "shift_register",
    "shiftregister": "shift_register", "clockdivider": "clock_divider", "prescaler": "clock_divider",
    "uart": "uart_tx", "uart_transmitter": "uart_tx", "uart_receiver": "uart_rx", "seven_seg": "seven_segment",
    "sevenseg": "seven_segment", "seg7": "seven_segment", "pwm_generator": "pwm", "debounce": "debouncer",
    "register_bank": "object", "regfile": "object", "machine": "fsm", "finite_state_machine": "fsm",
    "blink": "blinker", "divider": "clock_divider",
}

# kind -> spec.  regs: name -> (width | "W", init) ; pins_in: 1-bit input pins ; pins_out: regs exposed as pins
SPECS = {
    "object": dict(regs={}, pins_in=[], pins_out=[], primary=[]),
    "counter": dict(regs={"count": ("W", 0)}, pins_in=[], pins_out=[], primary=["count"]),
    "register": dict(regs={"value": ("W", 0)}, pins_in=[], pins_out=[], primary=["value"]),
    "adder": dict(regs={"a": ("W", 0), "b": ("W", 0), "sum": ("W", 0), "carry": (1, 0)},
                  pins_in=[], pins_out=[], primary=["sum", "carry"]),
    "alu": dict(regs={"a": ("W", 0), "b": ("W", 0), "op": (3, 0), "result": ("W", 0), "zero": (1, 0)},
                pins_in=[], pins_out=[], primary=["result", "zero"]),
    "mux": dict(regs={"a": ("W", 0), "b": ("W", 0), "sel": (1, 0), "y": ("W", 0)},
                pins_in=[], pins_out=[], primary=["y"]),
    "comparator": dict(regs={"a": ("W", 0), "b": ("W", 0), "eq": (1, 0), "lt": (1, 0), "gt": (1, 0)},
                       pins_in=[], pins_out=[], primary=["eq", "lt", "gt"]),
    "shift_register": dict(regs={"value": ("W", 0), "serial_in": (1, 0), "serial_out": (1, 0)},
                           pins_in=[], pins_out=["serial_out"], primary=["value"]),
    "clock_divider": dict(regs={"clk_out": (1, 0), "tick": (1, 0)}, pins_in=[], pins_out=["clk_out"],
                          primary=["clk_out"]),
    "pwm": dict(regs={"duty": ("W", 0), "out": (1, 0)}, pins_in=[], pins_out=["out"], primary=["out"]),
    "uart_tx": dict(regs={"data": (8, 0), "start": (1, 0), "busy": (1, 0), "tx": (1, 1)},
                    pins_in=[], pins_out=["tx"], primary=["busy"]),
    "uart_rx": dict(regs={"data": (8, 0), "valid": (1, 0)}, pins_in=["rx"], pins_out=[], primary=["data", "valid"]),
    "fsm": dict(regs={}, pins_in=[], pins_out=[], primary=["state"]),
    "blinker": dict(regs={"led": (1, 0)}, pins_in=[], pins_out=["led"], primary=["led"]),
    "debouncer": dict(regs={"out": (1, 0), "pressed": (1, 0)}, pins_in=["btn"], pins_out=[], primary=["out"]),
    "ram": dict(regs={"addr": ("A", 0), "data_in": ("W", 0), "we": (1, 0), "data_out": ("W", 0)},
                pins_in=[], pins_out=[], primary=["data_out"]),
    "flipflop": dict(regs={"d": (1, 0), "q": (1, 0)}, pins_in=[], pins_out=[], primary=["q"]),
    "seven_segment": dict(regs={"value": ("W", 0), "seg": (7, 0)}, pins_in=[], pins_out=["seg"], primary=[]),
}
DEFAULT_WIDTH = {"flipflop": 1, "seven_segment": 4}


def vname(s) -> str:
    n = ident(str(s)) or "x"
    if n in VKEYWORDS or n in RESERVED or n.startswith("nl_"):
        n = n + "_v"
    return n


def clog2(n: int) -> int:
    return max(1, math.ceil(math.log2(max(2, n))))


def to_int(v, default=0) -> int:
    try:
        if isinstance(v, bool):
            return int(v)
        if isinstance(v, (int, float)):
            return int(round(v))
        return int(float(str(v)))
    except (TypeError, ValueError, OverflowError):
        return default


def need_bits(v: int) -> int:
    return max(1, abs(v).bit_length() + (1 if v < 0 else 0))


def vstr(text: str) -> str:
    """ASCII-safe body of a Verilog string literal."""
    out = []
    for ch in str(text):
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif 32 <= ord(ch) < 127:
            out.append(ch)
        else:
            out.append("?")
    return "".join(out)


def vcom(text) -> str:
    """ASCII-safe text for a // comment (no string escaping, single line)."""
    return "".join(ch if 32 <= ord(ch) < 127 else (" " if ord(ch) in (9, 10, 13) else "?") for ch in str(text))


def lit(v: int, w: int) -> str:
    v = to_int(v)
    if v < 0:
        return f"-{w}'sd{min(-v, (1 << w) - 1)}"
    return f"{w}'d{v & ((1 << w) - 1)}"


class Reg:
    __slots__ = ("name", "width", "signed", "init", "note")

    def __init__(self, name, width, init=0, signed=False, note=""):
        self.name, self.width, self.init, self.signed, self.note = name, width, init, signed, note

    def decl(self) -> str:
        rng = f"[{self.width - 1}:0] " if self.width > 1 else ""
        return f"reg {'signed ' if self.signed else ''}{rng}{self.name};"


class Plan:
    """Everything needed to emit one entity module and its instance in top."""

    def __init__(self, e: Entity):
        self.e = e
        self.mod = ""
        self.kind = "object"
        self.W = 8
        self.clock_hz = 50_000_000
        self.regs: dict[str, Reg] = {}
        self.pins_in: list[str] = []
        self.pins_out: list[str] = []
        self.primary: list[str] = []
        self.states: list[str] = []
        self.xrefs: dict[tuple[str, str], str] = {}      # (other ident, prop) -> port name
        self.xstates: dict[str, "Plan"] = {}              # port reading another fsm's state -> that plan
        self.triggers: dict[str, str] = {}               # trigger key -> port name in_<x>
        self.timers: list[int] = []
        self.changes: list[str] = []
        self.in_chans: list[Channel] = []
        self.out_chans: list[Channel] = []
        self.uses_send = False
        self.uses_start = False
        self.comments: list[str] = []
        self.body: list[str] = []                         # rendered behaviour lines
        self.local_params: list[str] = []
        self.skip: set = set()                            # behaviour indexes re-homed to another entity
        self.extra: list[dict] = []                       # behaviours re-homed here from another entity
        self.moved: list[str] = []                        # notes about re-homed behaviours


class HardwareModule(DomainModule):
    name = "hardware"
    title = "Hardware (synthesizable Verilog-2005 RTL, testbench, Yosys)"
    aliases = ("hardware", "verilog", "fpga", "rtl", "hdl", "سخت‌افزار", "سخت_افزار", "سخت‌افزاری")
    keywords = ("fpga", "verilog", "rtl", "hdl", "asic", "cpld", "alu", "flipflop", "flip-flop", "dff", "mux",
                "multiplexer", "demux", "register", "shift_register", "adder", "comparator", "debouncer",
                "clock_divider", "yosys", "iverilog", "icarus", "gtkwave", "verilator", "testbench", "lut",
                "bitstream", "ice40", "ecp5", "synthesis", "netlist", "uart_tx", "uart_rx", "bram",
                "state_machine", "fsm", "seven_segment", "led_driver", "blinker")
    kinds = {
        "object": {"width": 8},
        "counter": {"count": 0, "width": 8},
        "register": {"value": 0, "width": 8},
        "adder": {"a": 0, "b": 0, "sum": 0, "carry": False, "width": 8},
        "alu": {"a": 0, "b": 0, "op": 0, "result": 0, "zero": False, "width": 8},
        "mux": {"a": 0, "b": 0, "sel": False, "y": 0, "width": 8},
        "multiplexer": {"a": 0, "b": 0, "sel": False, "y": 0, "width": 8},
        "comparator": {"a": 0, "b": 0, "eq": False, "lt": False, "gt": False, "width": 8},
        "shift_register": {"value": 0, "serial_in": False, "serial_out": False, "width": 8},
        "clock_divider": {"divide_by": 2, "clk_out": False, "tick": False},
        "pwm": {"duty": 0, "out": False, "width": 8},
        "uart_tx": {"baud": 115200, "data": 0, "start": False, "busy": False, "tx": True},
        "uart_rx": {"baud": 115200, "data": 0, "valid": False},
        "fsm": {"states": ["idle", "run", "done"], "state": "idle"},
        "state_machine": {"states": ["idle", "run", "done"], "state": "idle"},
        "blinker": {"interval_ms": 500, "led": False},
        "led_driver": {"interval_ms": 500, "led": False},
        "debouncer": {"stable_ms": 10, "out": False, "pressed": False},
        "ram": {"depth": 16, "addr": 0, "data_in": 0, "we": False, "data_out": 0, "width": 8},
        "memory": {"depth": 16, "addr": 0, "data_in": 0, "we": False, "data_out": 0, "width": 8},
        "flipflop": {"d": False, "q": False},
        "dff": {"d": False, "q": False},
        "seven_segment": {"value": 0, "seg": 0, "width": 4},
    }
    suggested_props = ("width", "clock_hz", "duty", "interval_ms", "baud", "states", "depth")
    protocols = ("wire", "gpio", "spi", "i2c", "serial", "http", "mqtt", "udp", "websocket")
    toolchain = """All tools are free / open source:

- **Icarus Verilog + GTKWave** (simulate, view waves):
  `iverilog -g2005 -DSIMULATION -s tb_top -o build/sim.vvp sim/tb_top.v -c files.f`  (files.f lists rtl/*.v; works in PowerShell too)
  then `vvp build/sim.vvp` and `gtkwave tb_top.vcd`
  (Windows installer: bleyer.org/icarus ; Linux: `apt install iverilog gtkwave`)
- **Verilator** (lint / fast C++ sim): `verilator --lint-only -Wall -Wno-fatal -DSIMULATION --top-module top -f files.f`
- **Yosys** (synthesis, OSS CAD Suite): `yosys -s synth.ys`  (generic gates + stats; edit for `synth_ice40` / `synth_ecp5`)
- **No install**: paste `rtl/*.v` + `sim/tb_top.v` into https://www.edaplayground.com (Icarus, tick "Open EPWave")
- `host_bridge.py` (stdlib; `pip install pyserial`) relays the FPGA UART JSON lines to the NLCL hub / MQTT / UDP."""

    # ------------------------------------------------------------------ props
    def default_props(self, kind: str) -> dict:
        k = self._canon(kind)
        base = self.kinds.get(kind) or self.kinds.get(k) or self.kinds["object"]
        return json.loads(json.dumps(base))

    def validate(self, e: Entity) -> list[str]:
        out = []
        try:
            w = to_int(e.props.get("width", 8), 8)
            if w < 1 or w > 32:
                out.append(f"width {w} clamped to 1..32 bits")
            kind = self._kind(e)
            if kind == "object" and e.kind not in ("object", "register_bank"):
                out.append(f"hardware kind '{e.kind}' is not built in; generated a register bank "
                           f"(known: {', '.join(k for k in SPECS if k != 'object')})")
            if kind == "pwm" and to_int(e.props.get("duty", 0)) > (1 << max(1, min(32, w))) - 1:
                out.append("duty is larger than 2^width-1 (output will be always high)")
            if kind == "fsm" and not isinstance(e.props.get("states"), (list, tuple)):
                out.append("fsm 'states' should be a list, e.g. states idle, run, done")
            hz = to_int(e.props.get("clock_hz", 50_000_000), 0)
            if hz <= 0:
                out.append("clock_hz must be positive; using 50000000")
        except Exception:  # never raise
            pass
        return out

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _canon(kind: str) -> str:
        k = ident(kind or "object")
        return KIND_ALIAS.get(k, k)

    def _kind(self, e: Entity) -> str:
        k = self._canon(e.kind)
        if k in SPECS:
            return k
        sub = e.props.get("subtype")
        if isinstance(sub, str) and self._canon(sub) in SPECS:
            return self._canon(sub)
        if k == "object" or k not in SPECS:
            st = e.props.get("states")
            if isinstance(st, (list, tuple)) and st:
                return "fsm"   # "a state machine" may arrive as kind=machine/object with states
        # "a shift register" may arrive as kind=register name=shift_register
        name = ident(e.name)
        cands = sorted(set(SPECS) | set(KIND_ALIAS), key=len, reverse=True)
        for c in cands:
            if c != "object" and len(c) > 2 and c in name:
                kk = KIND_ALIAS.get(c, c)
                if kk in SPECS and kk != "object":
                    return kk
        return "object"

    @staticmethod
    def _cid(c: Channel) -> str:
        return vname("ch_" + ident(c.id))

    @staticmethod
    def _wire_like(c: Channel) -> bool:
        return c.src.domain == c.dst.domain == "hardware"

    # ------------------------------------------------------------------ planning
    def _plan(self, ctx: BuildContext, e: Entity, used_mods: set) -> Plan:
        p = Plan(e)
        p.kind = self._kind(e)
        spec = SPECS[p.kind]
        p.W = max(1, min(32, to_int(e.props.get("width", DEFAULT_WIDTH.get(p.kind, 8)), 8)))
        hz = to_int(e.props.get("clock_hz", 50_000_000), 50_000_000)
        p.clock_hz = hz if hz > 0 else 50_000_000
        mod = vname(e.ident)
        if mod in used_mods or mod.startswith("nlcl_"):
            mod = mod + "_m"
        used_mods.add(mod)
        p.mod = mod
        p.pins_in = list(spec["pins_in"])
        p.pins_out = list(spec["pins_out"])
        p.primary = list(spec["primary"])
        depth = max(2, min(65536, to_int(e.props.get("depth", 16), 16)))
        for n, (w, init) in spec["regs"].items():
            width = p.W if w == "W" else (clog2(depth) if w == "A" else w)
            pv = e.props.get(n, init)
            iv = to_int(pv, init) if isinstance(pv, (int, float, bool)) else init
            if w == "W" and iv and need_bits(iv) > width:
                width = min(32, need_bits(iv))
            p.regs[n] = Reg(n, width, iv, iv < 0)
        if p.kind == "fsm":
            st = e.props.get("states")
            if isinstance(st, str):
                st = [s for s in re.split(r"[,\s]+", st) if s]
            if not isinstance(st, (list, tuple)) or not st:
                st = ["idle", "run", "done"]
            seen = []
            for s in st:
                n = ident(str(s)) or f"s{len(seen)}"
                if n not in seen:
                    seen.append(n)
            # "set state to closed": a bare / quoted name that is not a property becomes a new state
            for b in e.behaviors or []:
                for a in b.get("do") or []:
                    if isinstance(a, dict) and a.get("op") == "set" and a.get("prop") == "state":
                        v = str(a.get("value", "")).strip().strip("'\"")
                        # (the interpreter may have auto-added the name as a 0 property)
                        if re.fullmatch(r"[A-Za-z_]\w*", v) and e.props.get(v, 0) in (0, None) \
                                and v not in ("true", "false") and ident(v) not in seen:
                            seen.append(ident(v))
            p.states = seen
            init = e.props.get("state", 0)
            idx = seen.index(ident(str(init))) if isinstance(init, str) and ident(init) in seen else \
                max(0, min(len(seen) - 1, to_int(init, 0)))
            p.regs["state"] = Reg("state", clog2(len(seen)), idx, note="one of S_*")
        # free-form numeric / bool props
        idents = sorted({x.ident for x in ctx.entities}, key=len, reverse=True)
        for k, v in e.props.items():
            if k.startswith("_") or k in PARAM_PROPS:
                continue
            n = vname(k)
            if n in p.regs or n in p.pins_in:
                continue
            if any(ident(k).startswith(o + "_") and len(ident(k)) > len(o) + 1 for o in idents) or \
                    (ident(k) in idents and ident(k) != e.ident):
                continue  # "traffic state" flattened to traffic_state: resolved as traffic.state by the namer
            if ident(k) == e.ident and p.primary and p.primary[0] in p.regs:
                continue  # "<entity name>" used as a property means its main value
            if p.kind == "fsm" and (ident(k) == e.ident or ident(k) in p.states):
                continue
            if isinstance(v, bool):
                p.regs[n] = Reg(n, 1, int(v))
            elif isinstance(v, (int, float)):
                iv = to_int(v)
                note = f"float {v} stored as integer" if isinstance(v, float) and v != iv else ""
                p.regs[n] = Reg(n, min(32, max(p.W, need_bits(iv))), iv, iv < 0, note)
            else:
                txt = json.dumps(v, ensure_ascii=True) if not isinstance(v, str) else vstr(v)
                p.comments.append(f"{k} = {txt} (not synthesizable data, kept as a note)")
        # channel fields exist on both ends
        for c in ctx.channels_for(e):
            if c.dst.id == e.id:
                p.in_chans.append(c)
            if c.src.id == e.id:
                p.out_chans.append(c)
            for f in c.fields:
                n = vname(f)
                if n not in p.regs and n not in p.pins_in:
                    if c.dst.id == e.id and not self._wire_like(c) and c.protocol not in BUS_TO_WIRE:
                        # other domains send int16 words: keep sign and range (e.g. -5 or 300 degrees)
                        p.regs[n] = Reg(n, max(p.W, 16), 0, True, note=f"channel field ({c.id}), int16")
                    else:
                        p.regs[n] = Reg(n, p.W, 0, note=f"channel field ({c.id})")
        if p.kind == "object":
            p.primary = list(p.regs)[:8]
        return p

    # ------------------------------------------------------------------ expressions
    def _namer(self, ctx: BuildContext, p: Plan, plans: dict[str, Plan]):
        e = p.e

        def local(n: str) -> str:
            v = vname(n)
            if p.states and ident(n) in p.states:
                return f"S_{ident(n).upper()}"
            if v in p.pins_in:
                return v
            if v not in p.regs and ident(n) == e.ident:
                # "increase leds by 1" on the register named leds -> its main value
                main = [x for x in p.primary if x in p.regs] or list(p.regs)
                if main:
                    return main[0]
            if v not in p.regs:
                # "temperature" while a sensor called temp delivers its 'value' over a link
                idn = ident(n)
                for c in p.in_chans:
                    s_id, s_kind = c.src.ident, ident(c.src.kind)
                    if len(idn) >= 3 and (idn.startswith(s_id) or (len(s_id) >= 3 and s_id.startswith(idn))
                                          or idn == s_kind):
                        fl = [vname(x) for x in c.fields if vname(x) in p.regs]
                        num = [x for x in fl if p.regs[x].width > 1]
                        pick = "value" if "value" in fl else (num[0] if len(num) == 1 else None)
                        if pick:
                            return pick
                p.regs[v] = Reg(v, p.W, 0, note="implicit: first used in a behaviour")
            return v

        def f(n: str) -> str:
            if "." not in n and vname(n) not in p.regs and vname(n) not in p.pins_in:
                # "traffic state" may arrive flattened as traffic_state -> traffic.state
                idn = ident(n)
                for other in sorted(plans, key=len, reverse=True):
                    if idn.startswith(other + "_") and len(idn) > len(other) + 1:
                        n = f"{other}.{idn[len(other) + 1:]}"
                        break
            if "." not in n:
                if n in plans and n != e.ident:          # bare entity name -> its primary value
                    other = plans[n]
                    pr = (other.primary or list(other.regs) or ["value"])[0]
                    return f(f"{n}.{pr}")
                return local(n)
            ent, prop = n.split(".", 1)
            prop = prop.replace(".", "_")
            if ent in (e.ident, "self", "this"):
                return local(prop)
            if ent in plans:                              # same domain: wire from the other module
                other = plans[ent]
                pv = vname(prop)
                if pv not in other.regs and pv not in other.pins_in:
                    other.regs[pv] = Reg(pv, other.W, 0, note=f"implicit: read by {e.ident}")
                port = vname(f"x_{ent}_{prop}")
                p.xrefs[(ent, pv)] = port
                if pv == "state" and other.states:
                    p.xstates[port] = other
                return port
            # other domain: last value received via a channel
            for c in p.in_chans:
                if c.src.ident == ent and prop in [ident(x) for x in c.fields]:
                    return local(prop)
            v = vname(f"x_{ent}_{prop}")
            if v not in p.regs:
                p.regs[v] = Reg(v, p.W, 0, note=f"{ent}.{prop}: no channel delivers it (connect {ent} to {e.ident})")
            return v
        return f

    def _vexpr(self, ex, name) -> str:
        try:
            s = self.expr(ex, "verilog", name)
        except Exception:
            return "0 /* invalid expression */"
        return re.sub(r"(?<![\w'.])(\d+)\.(\d+)", lambda m: str(int(round(float(m.group(0))))), s)

    def _fix_states(self, p: Plan, s: str) -> str:
        # another fsm's state compared with a state name -> that fsm's encoding
        for port, other in p.xstates.items():
            def enc(m, other=other):
                st = ident(m.group("lit"))
                if st not in other.states:
                    return m.group(0)
                w = other.regs["state"].width if "state" in other.regs else 8
                return m.group(0).replace(f'"{m.group("lit")}"', f"{w}'d{other.states.index(st)}")
            s = re.sub(rf'\b{port}\s*(==|!=)\s*"(?P<lit>[^"]*)"', enc, s)
            s = re.sub(rf'"(?P<lit>[^"]*)"\s*(==|!=)\s*{port}\b', enc, s)
        if not p.states:
            return s
        return re.sub(r'"([^"]*)"', lambda m: f"S_{ident(m.group(1)).upper()}" if ident(m.group(1)) in p.states
                      else m.group(0), s)

    # ------------------------------------------------------------------ behaviours
    def _render_behaviours(self, ctx: BuildContext, p: Plan, plans: dict[str, Plan]) -> None:
        name = self._namer(ctx, p, plans)
        L = []
        own = [b for i, b in enumerate(p.e.behaviors or []) if i not in p.skip]
        for i, b in enumerate(own + p.extra):
            try:
                L += self._behaviour(ctx, p, b, name, i)
            except Exception as ex:  # never raise
                L.append(f"// behaviour {i} skipped: {vcom(ex)}")
        p.body = L
        # a register compared with / set to a constant wider than itself ("timer > 3000" on an
        # implicit 8-bit register) would be silently truncated: widen it to fit the constant
        try:
            code = "\n".join(x for x in L if not x.lstrip().startswith("//"))
            for r in p.regs.values():
                if r.name == "state" and p.states:
                    continue
                pat = rf"\b{re.escape(r.name)}\s*(?:==|!=|<=|>=|<|>|\+|-)\s*(\d+)\b(?!')"
                vals = [int(m) for m in re.findall(pat, code)]
                vals += [int(m) for m in re.findall(rf"(?<!')\b(\d+)\s*(?:==|!=|<=|>=|<|>)\s*{re.escape(r.name)}\b", code)]
                if vals and need_bits(max(vals)) > r.width and r.width > 1:
                    r.width = min(32, need_bits(max(vals)))
        except Exception:
            pass

    def _behaviour(self, ctx, p: Plan, b: dict, name, i: int) -> list[str]:
        on = str(b.get("on") or "update")
        trig, label = None, on
        if on == "start":
            trig = "!nl_started"
            p.uses_start = True
        elif on == "update":
            trig = None
        elif on.startswith("timer:"):
            ms = min(10**12, max(1, to_int(on.split(":", 1)[1], 1000)))
            if ms not in p.timers:
                p.timers.append(ms)
            trig = f"nl_tick_{ms}"
        elif on.startswith("input:") and p.kind == "debouncer" and \
                ident(on.split(":", 1)[1]) in (p.e.ident, "button", "btn", "press", "pressed", "key"):
            trig = "pressed"   # the debounced one-clock press pulse
        elif on.startswith("input:") or on == "collision":
            key = vname(on.split(":", 1)[1] if ":" in on else "collision")
            port = vname(f"in_{key}")
            p.triggers[key] = port
            trig = f"nl_{port}_rise"
        elif on.startswith("change:"):
            prop = name(on.split(":", 1)[1])
            if prop.startswith("S_"):
                return [f"// on {vcom(label)}: a constant never changes"]
            if prop not in p.changes:
                p.changes.append(prop)
            trig = f"({prop} != nl_prev_{prop})"
        elif on.startswith("message:"):
            src = on.split(":", 1)[1]
            conds = []
            for c in p.in_chans:
                if src in ("any", "*") or c.src.ident == ident(src) or c.src.name == src or c.src.id == src:
                    cid = self._cid(c)
                    if self._wire_like(c) or c.protocol in BUS_TO_WIRE:
                        diffs = " || ".join(f"({self._conv_in(p, cid, vname(fl))} != {vname(fl)})" for fl in c.fields
                                            if vname(fl) in p.regs) or "1'b0"
                        conds.append(f"(i_{cid}_we && ({diffs}))")
                    else:
                        conds.append(f"i_{cid}_we")
            trig = " || ".join(conds) if conds else "1'b0 /* no channel from " + vstr(src) + " */"
            if len(conds) > 1:
                trig = f"({trig})"
        else:
            trig = "1'b0 /* unsupported trigger */"
        cond = None
        if b.get("if"):
            cond = self._fix_states(p, self._vexpr(b["if"], name))
        parts = []
        if trig:
            parts.append(trig if re.fullmatch(r"!?\w+|\(.*\)", trig) else f"({trig})")
        if cond:
            parts.append(f"({cond})")
        test = " && ".join(parts)
        acts = []
        for a in b.get("do") or []:
            acts += self._action(p, a, name)
        head = f"// on {vcom(label)}" + (f" if {vcom(b.get('if'))}" if b.get("if") else "")
        if not test:
            return [head] + acts
        return [head, f"if ({test}) begin"] + ["    " + x for x in acts] + ["end"]

    @staticmethod
    def _target_of(prop: str, src: str, plans: dict) -> str | None:
        n = ident(str(prop).split(".", 1)[0]) if "." in str(prop) else ident(prop)
        if "." in str(prop):
            return n if n in plans else None
        if n in plans:
            return n
        for o in sorted(plans, key=len, reverse=True):
            if n.startswith(o + "_") and len(n) > len(o) + 1:
                return o
        return None

    def _rehome(self, plans: dict[str, Plan]) -> None:
        """A register lives in exactly one module, so a behaviour whose actions all write another
        hardware entity ("when button is pressed, increase pulses by 1" stored on button) is moved
        into that entity; its trigger / condition then read the source through a cross-module port."""
        for src, p in list(plans.items()):
            for i, b in enumerate(p.e.behaviors or []):
                if not isinstance(b, dict):
                    continue
                on = str(b.get("on") or "update")
                if on.startswith("message:"):
                    continue
                writes = [a for a in b.get("do") or [] if isinstance(a, dict) and a.get("op") in ("set", "add", "toggle")]
                others = [a for a in b.get("do") or [] if isinstance(a, dict) and a.get("op") not in ("set", "add", "toggle", "log")]
                if not writes or others:
                    continue
                tg = {self._target_of(a.get("prop", ""), src, plans) for a in writes}
                if len(tg) != 1:
                    continue
                t = tg.pop()
                srcp = set(p.regs) | set(p.pins_in)
                if t is None:
                    # "set duty to 200" while a clock divider is in focus: the one other entity whose
                    # kind owns every written register gets the behaviour
                    props = {vname(a.get("prop", "")) for a in writes}
                    own = set(SPECS[p.kind]["regs"]) | set(p.pins_in) | ({"state"} if p.states else set())
                    if props & own:
                        continue
                    cands = [k for k, o in plans.items() if k != src and props <= (
                        set(SPECS[o.kind]["regs"]) | ({"state"} if o.states else set()))]
                    if len(cands) != 1:
                        continue
                    t = cands[0]
                    srcp = own  # names the source merely picked up stay bare (resolved in the target)
                if t == src:
                    continue
                q = plans[t]

                def qual(ex, src=src, srcp=srcp):
                    if ex is None:
                        return None
                    parts = re.split(r'("[^"]*")', str(ex))
                    for j in range(0, len(parts), 2):
                        parts[j] = re.sub(r'(?<![\w.])([A-Za-z_]\w*)(?![\w.(])',
                                          lambda m: f"{src}.{m.group(1)}" if vname(m.group(1)) in srcp
                                          and m.group(1) not in plans else m.group(1), parts[j])
                    return "".join(parts)
                nb = dict(b)
                cond = qual(b.get("if"))
                if on.startswith("input:") and p.kind == "debouncer":
                    nb["on"] = "update"
                    cond = f"{src}.pressed" + (f" && ({cond})" if cond else "")
                elif on.startswith("change:"):
                    pr = on.split(":", 1)[1]
                    nb["on"] = "change:" + (pr if "." in pr else f"{src}.{pr}")
                nb["if"] = cond
                nb["do"] = [dict(a, value=qual(a.get("value"))) if "value" in a else dict(a)
                            for a in b.get("do") or []]
                p.skip.add(i)
                p.moved.append(f"behaviour 'on {vcom(on)}' writes {t}: generated inside {t}")
                q.extra.append(nb)
                q.moved.append(f"behaviour 'on {vcom(on)}' comes from {src}")

    def _conv_in(self, p: Plan, cid: str, f: str) -> str:
        r = p.regs.get(f)
        w = r.width if r else p.W
        port = f"i_{cid}_{f}"
        if w == 1:
            return f"({port} != 32'd0)"
        return f"{port}[{w - 1}:0]"

    def _action(self, p: Plan, a: dict, name) -> list[str]:
        op = a.get("op")
        if op in ("set", "add", "toggle"):
            prop = name(a["prop"])
            if prop.startswith("S_") or prop in p.pins_in:
                return [f"// cannot assign {vcom(a['prop'])} (constant or input pin)"]
            if prop.startswith("x_") and prop not in p.regs:
                return [f"// {vcom(a['prop'])} belongs to another module; send it through a link instead"]
            r = p.regs.get(prop)
            w = r.width if r else p.W
            if op == "toggle":
                return [f"{prop} <= !{prop};" if w > 1 else f"{prop} <= ~{prop};"]
            val = self._fix_states(p, self._vexpr(a.get("value", 0), name))
            if op == "add":
                m = re.fullmatch(r"-\s*(.+)", val.strip())
                if m:
                    # "decrease health by 10": a free-form register may go below zero, so it becomes
                    # signed (one extra bit keeps its range) and "health < 1" still fires after 5 - 10
                    if r is not None and not r.signed and r.width > 1 and r.name not in SPECS[p.kind]["regs"] \
                            and r.name != "state":
                        r.signed = True
                        r.width = min(32, r.width + 1)
                    inner = m.group(1)
                    if not re.fullmatch(r"[\w']+|\(.*\)", inner):
                        inner = f"({inner})"
                    return [f"{prop} <= {prop} - {inner};"]
                return [f"{prop} <= {prop} + {val};"]
            if w == 1 and val not in ("1'b0", "1'b1", "0", "1"):
                val = f"(({val}) != 0)"
            return [f"{prop} <= {val};"]
        if op == "destroy":
            return ["nl_alive <= 1'b0;  // destroy: module stops updating"]
        if op == "log":
            text = str(a.get("text", ""))
            args = []

            def sub(m):
                try:
                    args.append(name(ident(m.group(1)) if "." not in m.group(1) else m.group(1)))
                except Exception:
                    return "?"
                return "@@NLARG@@"
            t = re.sub(r"\{([^{}]+)\}", sub, text.replace("@@NLARG@@", ""))
            t = vstr(t).replace("%", "%%").replace("@@NLARG@@", "%0d")
            argtxt = "".join(f", {x}" for x in args)
            return ["`ifdef SIMULATION",
                    f'$display("[%0t] {p.e.ident}: {t}", $time{argtxt});',
                    "`endif"]
        if op == "move":
            out = []
            for ax in ("x", "y", "z"):
                v = str(a.get(ax, "0"))
                if v not in ("0", "0.0"):
                    reg = name(f"pos_{ax}")
                    out.append(f"{reg} <= {reg} + {self._vexpr(v, name)};")
            return out or ["// move by zero"]
        if op == "send":
            p.uses_send = True
            return ["nl_send <= 1'b1;  // publish linked fields now"]
        if op == "call":
            args = ", ".join(self._vexpr(x, name) for x in a.get("args", []))
            fn = vstr(a.get("name", "fn"))
            return [f"// TODO: call {fn}({args}) - implement as logic here",
                    "`ifdef SIMULATION", f'$display("[%0t] {p.e.ident}: call {fn}", $time);', "`endif"]
        return [f"// {vcom(describe_action(a)) if isinstance(a, dict) and 'op' in a else 'unknown action'}"]

    # ------------------------------------------------------------------ kind core logic
    def _core(self, p: Plan) -> tuple[list[str], list[str], list[str], list[str]]:
        """-> (declarations, reset lines, free-running lines, gated core lines)."""
        k, W, R, e = p.kind, p.W, p.regs, p.e
        D, RS, FR, C = [], [], [], []
        has = lambda n: n in R  # noqa: E731

        def ms_param(name, ms):
            D.append(f"localparam [39:0] {name}_RAW = (40'd0 + CLOCK_HZ / 1000) * 40'd{ms};")
            D.append(f"localparam [39:0] {name} = ({name}_RAW == 40'd0) ? 40'd1 : {name}_RAW;")

        driven = any(re.search(r"^\s*count <=", x) for x in p.body)
        if k == "counter" and driven and not has("enable") and not has("step"):
            C.append("// count is written by the behaviours below (no free-running increment)")
        elif k == "counter":
            step = "step" if has("step") else f"{R['count'].width}'d1"
            inner = []
            if has("max"):
                inner = [f"if (max != 0 && count >= max) count <= {R['count'].width}'d0;",
                         f"else count <= count + {step};"]
            else:
                inner = [f"count <= count + {step};"]
            if has("enable"):
                C += ["if (enable) begin"] + ["    " + x for x in inner] + ["end"]
            else:
                C += inner
        elif k == "register":
            if has("load") and has("d"):
                C.append("if (load) value <= d;")
            else:
                C.append("// holds 'value'; written by behaviours / channels (add props d + load for a load port)")
        elif k == "adder":
            cin = " + cin" if has("cin") else ""
            C.append(f"{{carry, sum}} <= a + b{cin};")
        elif k == "alu":
            rw = R["result"].width
            D += [f"// op: 0 ADD, 1 SUB, 2 AND, 3 OR, 4 XOR, 5 SHL1, 6 SHR1, 7 SLT",
                  f"wire [{rw - 1}:0] nl_alu_y = (op == 3'd0) ? a + b :",
                  "                         (op == 3'd1) ? a - b :",
                  "                         (op == 3'd2) ? a & b :",
                  "                         (op == 3'd3) ? a | b :",
                  "                         (op == 3'd4) ? a ^ b :",
                  "                         (op == 3'd5) ? a << 1 :",
                  "                         (op == 3'd6) ? a >> 1 :",
                  f"                         ((a < b) ? {rw}'d1 : {rw}'d0);"]
            C += ["result <= nl_alu_y;", f"zero <= (nl_alu_y == {rw}'d0);"]
        elif k == "mux":
            C.append("y <= sel ? b : a;")
        elif k == "comparator":
            C += ["eq <= (a == b);", "lt <= (a < b);", "gt <= (a > b);"]
        elif k == "shift_register":
            vw = R["value"].width
            sh = f"value <= {{value[{vw - 2}:0], serial_in}};" if vw > 1 else "value <= serial_in;"
            C += (["if (shift) begin", "    " + sh, "end"] if has("shift") else [sh])
            C.append(f"serial_out <= value[{vw - 1}];" if vw > 1 else "serial_out <= value;")
        elif k == "clock_divider":
            fq = to_int(e.props.get("frequency_hz", 0), 0)
            if fq > 0:
                D.append(f"localparam [39:0] NL_HALF_RAW = (40'd0 + CLOCK_HZ) / (40'd2 * 40'd{fq});")
            else:
                dv = max(2, to_int(e.props.get("divide_by", 2), 2))
                D.append(f"localparam [39:0] NL_HALF_RAW = 40'd{dv // 2};")
            D += ["localparam [39:0] NL_HALF = (NL_HALF_RAW == 40'd0) ? 40'd1 : NL_HALF_RAW;",
                  "reg [39:0] nl_div;"]
            RS.append("nl_div <= 40'd0;")
            C += ["tick <= 1'b0;",
                  "if (nl_div >= NL_HALF - 40'd1) begin",
                  "    nl_div  <= 40'd0;",
                  "    clk_out <= ~clk_out;",
                  "    tick    <= ~clk_out;   // one-clk pulse on every rising edge of clk_out",
                  "end else begin",
                  "    nl_div <= nl_div + 40'd1;",
                  "end"]
        elif k == "pwm":
            dw = R["duty"].width
            fq = to_int(e.props.get("frequency_hz", 0), 0)
            if fq > 0:
                D.append(f"localparam [39:0] NL_PRE_RAW = (40'd0 + CLOCK_HZ) / (40'd{fq} * 40'd{1 << dw});")
            else:
                D.append("localparam [39:0] NL_PRE_RAW = 40'd1;  // set frequency_hz to slow the PWM down")
            D += ["localparam [39:0] NL_PRE = (NL_PRE_RAW == 40'd0) ? 40'd1 : NL_PRE_RAW;",
                  f"reg [{dw - 1}:0] nl_pwm_cnt;", "reg [39:0] nl_pre;"]
            RS += [f"nl_pwm_cnt <= {dw}'d0;", "nl_pre <= 40'd0;"]
            C += ["if (nl_pre >= NL_PRE - 40'd1) begin",
                  "    nl_pre     <= 40'd0;",
                  f"    nl_pwm_cnt <= nl_pwm_cnt + {dw}'d1;",
                  "end else begin",
                  "    nl_pre <= nl_pre + 40'd1;",
                  "end",
                  ("out <= enable && (nl_pwm_cnt < duty);" if has("enable") else "out <= (nl_pwm_cnt < duty);")]
        elif k == "uart_tx":
            baud = max(1, to_int(e.props.get("baud", 115200), 115200))
            D += [f"localparam [31:0] NL_BAUD_RAW = CLOCK_HZ / {baud};",
                  "localparam [31:0] NL_BAUD_DIV = (NL_BAUD_RAW == 0) ? 1 : NL_BAUD_RAW;",
                  "reg [31:0] nl_tx_div;", "reg [3:0] nl_tx_bit;", "reg [9:0] nl_tx_sh;"]
            RS += ["nl_tx_div <= 32'd0;", "nl_tx_bit <= 4'd0;", "nl_tx_sh <= 10'h3FF;"]
            dsl = "data[7:0]" if R["data"].width >= 8 else f"{{{8 - R['data'].width}'d0, data}}"
            C += ["// 8N1: write 'data' then pulse 'start'; 'busy' is high while shifting",
                  "if (!busy) begin",
                  "    tx <= 1'b1;",
                  "    if (start) begin",
                  f"        nl_tx_sh  <= {{1'b1, {dsl}, 1'b0}};",
                  "        nl_tx_bit <= 4'd0;",
                  "        nl_tx_div <= 32'd0;",
                  "        busy      <= 1'b1;",
                  "        start     <= 1'b0;",
                  "    end",
                  "end else begin",
                  "    tx <= nl_tx_sh[0];",
                  "    if (nl_tx_div == NL_BAUD_DIV - 1) begin",
                  "        nl_tx_div <= 32'd0;",
                  "        nl_tx_sh  <= {1'b1, nl_tx_sh[9:1]};",
                  "        if (nl_tx_bit == 4'd9) busy <= 1'b0;",
                  "        else nl_tx_bit <= nl_tx_bit + 4'd1;",
                  "    end else begin",
                  "        nl_tx_div <= nl_tx_div + 32'd1;",
                  "    end",
                  "end"]
        elif k == "uart_rx":
            baud = max(1, to_int(e.props.get("baud", 115200), 115200))
            D += [f"localparam [31:0] NL_BAUD_RAW = CLOCK_HZ / {baud};",
                  "localparam [31:0] NL_BAUD_DIV = (NL_BAUD_RAW == 0) ? 1 : NL_BAUD_RAW;",
                  "reg [2:0] nl_rx_s;", "reg nl_rx_busy;", "reg [31:0] nl_rx_div;", "reg [3:0] nl_rx_bit;",
                  "reg [7:0] nl_rx_sh;"]
            RS += ["nl_rx_s <= 3'b111;", "nl_rx_busy <= 1'b0;", "nl_rx_div <= 32'd0;", "nl_rx_bit <= 4'd0;",
                   "nl_rx_sh <= 8'd0;"]
            FR.append("nl_rx_s <= {nl_rx_s[1:0], rx};")
            dw = R["data"].width
            dval = "nl_rx_sh" if dw == 8 else (f"nl_rx_sh[{dw - 1}:0]" if dw < 8 else f"{{{dw - 8}'d0, nl_rx_sh}}")
            C += ["// 8N1 receiver: 'data' + one-clk 'valid' pulse per byte",
                  "valid <= 1'b0;",
                  "if (!nl_rx_busy) begin",
                  "    if (!nl_rx_s[2]) begin",
                  "        nl_rx_busy <= 1'b1;",
                  "        nl_rx_div  <= NL_BAUD_DIV / 2;",
                  "        nl_rx_bit  <= 4'd0;",
                  "    end",
                  "end else if (nl_rx_div >= NL_BAUD_DIV - 1) begin",
                  "    nl_rx_div <= 32'd0;",
                  "    if (nl_rx_bit == 4'd0) begin",
                  "        if (nl_rx_s[2]) nl_rx_busy <= 1'b0;   // glitch, not a start bit",
                  "        else nl_rx_bit <= 4'd1;",
                  "    end else if (nl_rx_bit <= 4'd8) begin",
                  "        nl_rx_sh  <= {nl_rx_s[2], nl_rx_sh[7:1]};",
                  "        nl_rx_bit <= nl_rx_bit + 4'd1;",
                  "    end else begin",
                  "        nl_rx_busy <= 1'b0;",
                  "        if (nl_rx_s[2]) begin",
                  f"            data  <= {dval};",
                  "            valid <= 1'b1;",
                  "        end",
                  "    end",
                  "end else begin",
                  "    nl_rx_div <= nl_rx_div + 32'd1;",
                  "end"]
        elif k == "fsm":
            for i, s in enumerate(p.states):
                D.append(f"localparam [{R['state'].width - 1}:0] S_{s.upper()} = {R['state'].width}'d{i};")
            writes_state = any(a.get("prop") == "state" for b in (e.behaviors or []) for a in b.get("do", [])
                               if isinstance(a, dict))
            if not writes_state:
                last = f"S_{p.states[-1].upper()}"
                step = f"    state <= (state == {last}) ? S_{p.states[0].upper()} : state + 1'b1;"
                if "advance" in R:      # a property / link drives it
                    D.append("reg nl_adv_q;")
                    RS.append("nl_adv_q <= 1'b0;")
                    C += ["// default sequencing: each rising edge of 'advance' moves to the next state",
                          "nl_adv_q <= advance;", "if (advance && !nl_adv_q)", step]
                else:                   # no driver: expose 'advance' as a (synchronised) input pin
                    if "advance" not in p.pins_in:
                        p.pins_in.append("advance")
                    D.append("reg [2:0] nl_adv_s;")
                    RS.append("nl_adv_s <= 3'b000;")
                    FR.append("nl_adv_s <= {nl_adv_s[1:0], advance};")
                    C += ["// default sequencing: each rising edge of the 'advance' pin moves to the next state",
                          "if (nl_adv_s[2:1] == 2'b01)", step]
            else:
                C.append("// transitions are written by the behaviours below")
        elif k == "blinker":
            ms = max(1, to_int(e.props.get("interval_ms", 500), 500))
            ms_param("NL_HALF", ms)
            D.append("reg [39:0] nl_blink;")
            RS.append("nl_blink <= 40'd0;")
            body = ["if (nl_blink >= NL_HALF - 40'd1) begin", "    nl_blink <= 40'd0;", "    led <= ~led;",
                    "end else begin", "    nl_blink <= nl_blink + 40'd1;", "end"]
            C += (["if (enable) begin"] + ["    " + x for x in body] + ["end"]) if has("enable") else body
        elif k == "debouncer":
            ms = max(1, to_int(e.props.get("stable_ms", 10), 10))
            ms_param("NL_STABLE", ms)
            D += ["reg [2:0] nl_btn_s;", "reg [39:0] nl_db_cnt;"]
            RS += ["nl_btn_s <= 3'b000;", "nl_db_cnt <= 40'd0;"]
            inv = "~" if e.props.get("active_low") else ""
            FR.append(f"nl_btn_s <= {{nl_btn_s[1:0], {inv}btn}};")
            C += ["pressed <= 1'b0;",
                  "if (nl_btn_s[2] == out) begin",
                  "    nl_db_cnt <= 40'd0;",
                  "end else if (nl_db_cnt >= NL_STABLE - 40'd1) begin",
                  "    nl_db_cnt <= 40'd0;",
                  "    out       <= nl_btn_s[2];",
                  "    pressed   <= nl_btn_s[2];   // one-clk pulse on a clean press",
                  "end else begin",
                  "    nl_db_cnt <= nl_db_cnt + 40'd1;",
                  "end"]
        elif k == "ram":
            depth = max(2, min(65536, to_int(e.props.get("depth", 16), 16)))
            dw = R["data_out"].width
            D += [f"localparam DEPTH = {depth};", f"reg [{dw - 1}:0] nl_mem [0:DEPTH-1];"]
            init = e.props.get("init")
            if isinstance(init, (list, tuple)) and init:
                D.append("initial begin")
                for i, v in enumerate(list(init)[:depth]):
                    D.append(f"    nl_mem[{i}] = {lit(to_int(v), dw)};")
                D.append("end")
            C += ["if (we) nl_mem[addr] <= data_in;", "data_out <= nl_mem[addr];   // synchronous read (BRAM)"]
        elif k == "flipflop":
            C.append("if (en) q <= d;" if has("en") else "q <= d;")
        elif k == "seven_segment":
            D += ["// segments {g,f,e,d,c,b,a}, active high (set active_low true for common-anode)",
                  "function [6:0] nl_seg7;", "    input [3:0] v;", "    begin", "        case (v)"]
            codes = [0x3F, 0x06, 0x5B, 0x4F, 0x66, 0x6D, 0x7D, 0x07, 0x7F, 0x6F, 0x77, 0x7C, 0x39, 0x5E, 0x79, 0x71]
            for i, c in enumerate(codes):
                D.append(f"            4'h{i:X}: nl_seg7 = 7'h{c:02X};")
            D += ["            default: nl_seg7 = 7'h00;", "        endcase", "    end", "endfunction"]
            # "connect counter to display": show the first linked field when no link carries 'value'
            src = "value"
            linked = [vname(f) for c in p.in_chans for f in c.fields if vname(f) in R]
            writes_value = any(isinstance(a, dict) and a.get("prop") == "value"
                               for b in (e.behaviors or []) for a in (b.get("do") or []))
            if linked and "value" not in linked and not writes_value:
                src = linked[0]
                C.append(f"// shows linked field '{src}' (no link or behaviour writes 'value')")
            vw = R[src].width
            vv = f"{src}[3:0]" if vw >= 4 else (f"{{{4 - vw}'d0, {src}}}" if vw < 4 else src)
            C.append(f"seg <= {'~' if e.props.get('active_low') else ''}nl_seg7({vv});")
        else:
            C.append("// register bank: one register per numeric/bool property")
        return D, RS, FR, C

    # ------------------------------------------------------------------ emit module
    def _module(self, ctx: BuildContext, p: Plan) -> str:
        D, RS, FR, C = self._core(p)
        e = p.e
        R = p.regs
        L = ["`timescale 1ns / 1ps", "`default_nettype none",
             f"// {e.name}: hardware {e.kind}" + (f" (built as {p.kind})" if p.kind != e.kind else "")
             + " - generated by NLCL, edit freely.",
             "// Synchronous active-high reset; every register updates on posedge clk."]
        for c in p.comments:
            L.append(f"// note: {c}")
        for c in p.moved:
            L.append(f"// note: {c}")
        for b in e.behaviors or []:
            try:
                L.append("// rule: on " + vcom(b.get("on")) + (f" if {vcom(b.get('if'))}" if b.get("if") else "")
                         + " -> " + vcom("; ".join(describe_action(a) for a in b.get("do", []))))
            except Exception:
                pass
        ports = ["input  wire clk", "input  wire rst"]
        for pin in p.pins_in:
            ports.append(f"input  wire {pin}")
        for key, port in p.triggers.items():
            ports.append(f"input  wire {port}")
        for (ent, prop), port in p.xrefs.items():
            w = self._other_width(ctx, ent, prop)
            ports.append(f"input  wire {self._rng(w)}{port}")
        for c in p.in_chans:
            cid = self._cid(c)
            for f in c.fields:
                ports.append(f"input  wire [31:0] i_{cid}_{vname(f)}")
            ports.append(f"input  wire i_{cid}_we")
        for n, r in R.items():
            ports.append(f"output wire {'signed ' if r.signed else ''}{self._rng(r.width)}o_{n}")
        if p.out_chans:
            ports.append("output wire o_send")
        L.append(f"module {p.mod} #(")
        L.append(f"    parameter CLOCK_HZ = {p.clock_hz}")
        L.append(") (")
        L.append(",\n".join("    " + x for x in ports))
        L.append(");")
        L.append(f"    localparam WIDTH = {p.W};")
        # declarations
        for n, r in R.items():
            L.append(f"    {r.decl()}" + (f"  // {vcom(r.note)}" if r.note else ""))
        L.append("    reg nl_alive;     // cleared by 'destroy'")
        if p.uses_start:
            L.append("    reg nl_started;")
        if p.out_chans or p.uses_send:
            L.append("    reg nl_send;      // one-clk pulse from a 'send' action")
        def tw(ms):   # timer width: 40 bits, more for very long periods (e.g. hours at 100 MHz)
            return min(64, max(40, (ms * max(p.clock_hz, 100_000_000) // 1000).bit_length() + 1))
        for ms in p.timers:
            t = tw(ms)
            L.append(f"    localparam [{t - 1}:0] NL_T{ms}_RAW = ({t}'d0 + CLOCK_HZ / 1000) * {t}'d{ms};")
            L.append(f"    localparam [{t - 1}:0] NL_T{ms} = (NL_T{ms}_RAW == {t}'d0) ? {t}'d1 : NL_T{ms}_RAW;")
            L.append(f"    reg [{t - 1}:0] nl_tmr_{ms};")
            L.append(f"    reg nl_tick_{ms};   // one-clk pulse every {ms} ms")
        for key, port in p.triggers.items():
            L.append(f"    reg [2:0] nl_{port}_s;")
            L.append(f"    wire nl_{port}_rise = (nl_{port}_s[2:1] == 2'b01);")
        for prop in p.changes:
            r = R.get(prop)
            L.append(f"    reg {self._rng(r.width if r else p.W)}nl_prev_{prop};")
        for d in D:
            L.append("    " + d)
        for n in R:
            L.append(f"    assign o_{n} = {n};")
        if p.out_chans:
            L.append("    assign o_send = " + ("nl_send;" if p.uses_send else "1'b0;"))
        L.append("")
        L.append("    always @(posedge clk) begin")
        L.append("        if (rst) begin")
        rs = [f"{n} <= {lit(r.init, r.width)};" for n, r in R.items()]
        rs.append("nl_alive <= 1'b1;")
        if p.uses_start:
            rs.append("nl_started <= 1'b0;")
        if p.out_chans or p.uses_send:
            rs.append("nl_send <= 1'b0;")
        for ms in p.timers:
            rs += [f"nl_tmr_{ms} <= {tw(ms)}'d0;", f"nl_tick_{ms} <= 1'b0;"]
        for key, port in p.triggers.items():
            rs.append(f"nl_{port}_s <= 3'b000;")
        for prop in p.changes:
            r = R.get(prop)
            rs.append(f"nl_prev_{prop} <= {lit(r.init if r else 0, r.width if r else p.W)};")
        rs += RS
        L += ["            " + x for x in rs]
        L.append("        end else begin")
        fr = []
        if p.out_chans or p.uses_send:
            fr.append("nl_send <= 1'b0;")
        for ms in p.timers:
            t = tw(ms)
            fr += [f"if (nl_tmr_{ms} >= NL_T{ms} - {t}'d1) begin",
                   f"    nl_tmr_{ms} <= {t}'d0;", f"    nl_tick_{ms} <= 1'b1;",
                   "end else begin", f"    nl_tmr_{ms} <= nl_tmr_{ms} + {t}'d1;", f"    nl_tick_{ms} <= 1'b0;", "end"]
        for key, port in p.triggers.items():
            fr.append(f"nl_{port}_s <= {{nl_{port}_s[1:0], {port}}};")
        fr += FR
        L += ["            " + x for x in fr]
        L.append("            if (nl_alive) begin")
        g = ["// ---- " + p.kind + " core ----"] + C
        if p.in_chans:
            g.append("// ---- values received from links ----")
            for c in p.in_chans:
                cid = self._cid(c)
                g.append(f"if (i_{cid}_we) begin   // {vcom(c.src.name)} ({c.src.domain}) via {c.protocol}")
                for f in c.fields:
                    fv = vname(f)
                    if fv in R:
                        g.append(f"    {fv} <= {self._conv_in(p, cid, fv)};")
                g.append("end")
        if p.body:
            g.append("// ---- behaviours ----")
            g += p.body
        if p.uses_start:
            g.append("nl_started <= 1'b1;")
        L += ["                " + x for x in g]
        L.append("            end")
        for prop in p.changes:
            L.append(f"            nl_prev_{prop} <= {prop};")
        L.append("        end")
        L.append("    end")
        L.append("endmodule")
        L.append("`default_nettype wire")
        return "\n".join(L) + "\n"

    @staticmethod
    def _rng(w: int) -> str:
        return f"[{w - 1}:0] " if w > 1 else ""

    def _other_width(self, ctx, ent: str, prop: str) -> int:
        p = self._plans.get(ent)
        if p and prop in p.regs:
            return p.regs[prop].width
        return 1 if p and prop in p.pins_in else (p.W if p else 8)

    # ------------------------------------------------------------------ json uart bridges
    def _json_tx(self, c: Channel, cid: str) -> str:
        n = len(c.fields)
        items: list[tuple[int, int, str]] = []

        def text(s):
            for ch in s.encode("ascii", "replace"):
                items.append((0, ch, chr(ch) if 32 <= ch < 127 and ch not in (92,) else f"0x{ch:02X}"))
        text('{"channel":' + json.dumps(str(c.id), ensure_ascii=True) + ',"from":'
             + json.dumps(str(c.src.ident), ensure_ascii=True))
        for i, f in enumerate(c.fields):
            text(',' + json.dumps(str(f), ensure_ascii=True) + ':')
            items.append((1, i, f"field {f}"))
        text("}\n")
        items.append((2, 0, "end"))
        L = ["`timescale 1ns / 1ps", "`default_nettype none",
             f"// UART JSON transmitter for channel {vcom(c.id)}: {vcom(c.src.name)} -> {vcom(c.dst.name)} "
             f"({c.dst.domain}, {c.protocol})",
             f"// Sends one line every INTERVAL_MS (or on 'send'):  {vcom(''.join(chr(x[1]) if x[0] == 0 else '<' + x[2][6:] + '>' for x in items[:-1]).strip())}",
             "// Field values are int16 (two's complement) printed in decimal.",
             f"module nlcl_json_tx_{cid} #(",
             "    parameter CLOCK_HZ = 50000000,",
             f"    parameter BAUD = {int(c.config.get('baud', 115200))},",
             f"    parameter INTERVAL_MS = {max(1, to_int(c.config.get('interval_ms', 1000), 1000))}",
             ") (",
             "    input  wire clk,", "    input  wire rst,", "    input  wire send,",
             f"    input  wire [{16 * n - 1}:0] words,", "    output reg  tx", ");",
             "    localparam [31:0] BAUD_RAW = CLOCK_HZ / BAUD;",
             "    localparam [31:0] BAUD_DIV = (BAUD_RAW == 0) ? 1 : BAUD_RAW;",
             "    localparam [39:0] TICKS_RAW = (40'd0 + CLOCK_HZ / 1000) * INTERVAL_MS;",
             "    localparam [39:0] TICKS = (TICKS_RAW == 40'd0) ? 40'd1 : TICKS_RAW;",
             "    localparam S_IDLE = 3'd0, S_FETCH = 3'd1, S_DIG = 3'd2, S_EMIT = 3'd3;",
             "    // template ROM: {kind[1:0], data[7:0]}  kind 0 = literal byte, 1 = field index, 2 = end",
             "    function [9:0] rom;", "        input [9:0] a;", "        begin", "            case (a)"]
        for i, (kd, d, cm) in enumerate(items):
            L.append(f"                10'd{i}: rom = {{2'd{kd}, 8'h{d:02X}}};   // {vcom(cm).replace('*/', '')}")
        L += ["                default: rom = {2'd2, 8'h00};", "            endcase", "        end", "    endfunction",
              "    function [16:0] pw10;", "        input [2:0] pl;", "        begin", "            case (pl)",
              "                3'd0: pw10 = 17'd10000;", "                3'd1: pw10 = 17'd1000;",
              "                3'd2: pw10 = 17'd100;", "                3'd3: pw10 = 17'd10;",
              "                default: pw10 = 17'd1;", "            endcase", "        end", "    endfunction",
              "    reg [39:0] tmr;", "    reg [9:0] idx;", "    reg [2:0] st;", "    reg [2:0] ret;",
              "    reg [2:0] place;", "    reg [3:0] digit;", "    reg [16:0] val;", "    reg started;",
              f"    reg [{16 * n - 1}:0] snap;", "    reg [7:0] ch;",
              "    reg [9:0] sh;", "    reg [3:0] bitn;", "    reg [31:0] div;", "    reg ubusy;",
              "    wire [9:0] r = rom(idx);",
              f"    wire [15:0] cur = snap[{16 * n - 1} - 16*r[7:0] -: 16];",
              "",
              "    always @(posedge clk) begin",
              "        if (rst) begin",
              "            tmr <= 40'd0; idx <= 10'd0; st <= S_IDLE; ret <= S_IDLE; place <= 3'd0;",
              "            digit <= 4'd0; val <= 17'd0; started <= 1'b0; snap <= 0; ch <= 8'd0;",
              "            sh <= 10'h3FF; bitn <= 4'd0; div <= 32'd0; ubusy <= 1'b0; tx <= 1'b1;",
              "        end else begin",
              "            tmr <= (tmr >= TICKS - 40'd1) ? 40'd0 : tmr + 40'd1;",
              "            // ---- 8N1 shifter ----",
              "            tx <= ubusy ? sh[0] : 1'b1;",
              "            if (ubusy) begin",
              "                if (div == BAUD_DIV - 1) begin",
              "                    div <= 32'd0;",
              "                    sh  <= {1'b1, sh[9:1]};",
              "                    if (bitn == 4'd9) ubusy <= 1'b0;",
              "                    else bitn <= bitn + 4'd1;",
              "                end else begin",
              "                    div <= div + 32'd1;",
              "                end",
              "            end",
              "            // ---- line formatter ----",
              "            case (st)",
              "                S_IDLE: if (send || tmr >= TICKS - 40'd1) begin",
              "                    snap <= words;",
              "                    idx  <= 10'd0;",
              "                    st   <= S_FETCH;",
              "                end",
              "                S_FETCH: begin",
              "                    case (r[9:8])",
              "                        2'd0: begin ch <= r[7:0]; idx <= idx + 10'd1; ret <= S_FETCH; st <= S_EMIT; end",
              "                        2'd1: begin",
              "                            place <= 3'd0; digit <= 4'd0; started <= 1'b0;",
              "                            if (cur[15]) begin",
              "                                val <= {1'b0, ~cur + 16'd1};",
              "                                ch  <= 8'h2D;   // '-'",
              "                                ret <= S_DIG;",
              "                                st  <= S_EMIT;",
              "                            end else begin",
              "                                val <= {1'b0, cur};",
              "                                st  <= S_DIG;",
              "                            end",
              "                        end",
              "                        default: st <= S_IDLE;",
              "                    endcase",
              "                end",
              "                S_DIG: begin",
              "                    if (val >= pw10(place)) begin",
              "                        val   <= val - pw10(place);",
              "                        digit <= digit + 4'd1;",
              "                    end else begin",
              "                        digit <= 4'd0;",
              "                        place <= place + 3'd1;",
              "                        if (place == 3'd4) idx <= idx + 10'd1;",
              "                        if (digit != 4'd0 || started || place == 3'd4) begin",
              "                            ch      <= 8'h30 + {4'd0, digit};",
              "                            started <= 1'b1;",
              "                            ret     <= (place == 3'd4) ? S_FETCH : S_DIG;",
              "                            st      <= S_EMIT;",
              "                        end",
              "                    end",
              "                end",
              "                S_EMIT: if (!ubusy) begin",
              "                    sh    <= {1'b1, ch, 1'b0};",
              "                    bitn  <= 4'd0;",
              "                    div   <= 32'd0;",
              "                    ubusy <= 1'b1;",
              "                    st    <= ret;",
              "                end",
              "                default: st <= S_IDLE;",
              "            endcase",
              "        end",
              "    end",
              "endmodule", "`default_nettype wire"]
        return "\n".join(L) + "\n"

    def _json_rx(self, c: Channel, cid: str) -> str:
        n = len(c.fields)
        pats = ['"' + str(f) + '":' for f in c.fields]
        chan_seen = '"channel":'
        chan_id = '"channel":' + json.dumps(str(c.id), ensure_ascii=True)
        K = max([len(x.encode("ascii", "replace")) for x in pats + [chan_seen, chan_id]] + [2])

        def vl(s):
            return '"' + vstr(s) + '"'
        L = ["`timescale 1ns / 1ps", "`default_nettype none",
             f"// UART JSON receiver for channel {vcom(c.id)}: {vcom(c.src.name)} ({c.src.domain}, {c.protocol}) "
             f"-> {vcom(c.dst.name)}",
             "// Parses newline-terminated flat JSON lines such as",
             "//   {" + (chan_id + ', "from": "x", ' + ", ".join(f"{p} 12" for p in pats)).encode("ascii", "replace").decode() + "}",
             "// Spaces are ignored, integers / true / false accepted, fractions truncated.",
             "// 'words' (int16 per field, field order) updates and 'valid' pulses at the end of each line",
             "// that belongs to this channel (or carries no \"channel\" key).",
             f"module nlcl_json_rx_{cid} #(",
             "    parameter CLOCK_HZ = 50000000,",
             f"    parameter BAUD = {int(c.config.get('baud', 115200))}",
             ") (",
             "    input  wire clk,", "    input  wire rst,", "    input  wire rx,",
             f"    output reg  [{16 * n - 1}:0] words,", "    output reg  valid", ");",
             "    localparam [31:0] BAUD_RAW = CLOCK_HZ / BAUD;",
             "    localparam [31:0] BAUD_DIV = (BAUD_RAW == 0) ? 1 : BAUD_RAW;",
             "    localparam P_SCAN = 2'd0, P_PRE = 2'd1, P_NUM = 2'd2, P_SKIP = 2'd3;",
             "    // ---- 8N1 receiver ----",
             "    reg [2:0] rs;", "    reg rbusy;", "    reg [31:0] rdiv;", "    reg [3:0] rbit;", "    reg [7:0] rsh;",
             "    reg [7:0] rb;", "    reg rbv;",
             "    // ---- parser ----",
             f"    reg [{8 * K - 1}:0] win;", f"    wire [{8 * K - 1}:0] win_n = {{win[{8 * K - 9}:0], rb}};",
             f"    reg [{16 * n - 1}:0] buf_w;", "    reg [1:0] ps;", "    reg [7:0] fld;", "    reg neg;",
             "    reg [19:0] acc;", "    reg got;", "    reg chan_seen;", "    reg chan_ok;",
             "    wire is_digit = (rb >= 8'h30) && (rb <= 8'h39);",
             "    wire [3:0] dval = rb[3:0];",
             f"    wire m_chan = (win_n[{8 * len(chan_seen) - 1}:0] == {vl(chan_seen)});",
             f"    wire m_chan_id = (win_n[{8 * len(chan_id.encode('ascii', 'replace')) - 1}:0] == {vl(chan_id)});"]
        for i, pt in enumerate(pats):
            L.append(f"    wire m_{i} = (win_n[{8 * len(pt.encode('ascii', 'replace')) - 1}:0] == {vl(pt)});")
        L += ["    wire [15:0] acc16 = acc[15:0];",
              "    wire [15:0] num = neg ? (~acc16 + 16'd1) : acc16;",
              "",
              "    always @(posedge clk) begin",
              "        if (rst) begin",
              "            rs <= 3'b111; rbusy <= 1'b0; rdiv <= 32'd0; rbit <= 4'd0; rsh <= 8'd0;",
              "            rb <= 8'd0; rbv <= 1'b0; win <= 0; buf_w <= 0; words <= 0; valid <= 1'b0;",
              "            ps <= P_SCAN; fld <= 8'd0; neg <= 1'b0; acc <= 20'd0; got <= 1'b0;",
              "            chan_seen <= 1'b0; chan_ok <= 1'b0;",
              "        end else begin",
              "            rs    <= {rs[1:0], rx};",
              "            rbv   <= 1'b0;",
              "            valid <= 1'b0;",
              "            if (!rbusy) begin",
              "                if (!rs[2]) begin rbusy <= 1'b1; rdiv <= BAUD_DIV / 2; rbit <= 4'd0; end",
              "            end else if (rdiv >= BAUD_DIV - 1) begin",
              "                rdiv <= 32'd0;",
              "                if (rbit == 4'd0) begin",
              "                    if (rs[2]) rbusy <= 1'b0; else rbit <= 4'd1;",
              "                end else if (rbit <= 4'd8) begin",
              "                    rsh  <= {rs[2], rsh[7:1]};",
              "                    rbit <= rbit + 4'd1;",
              "                end else begin",
              "                    rbusy <= 1'b0;",
              "                    if (rs[2]) begin rb <= rsh; rbv <= 1'b1; end",
              "                end",
              "            end else begin",
              "                rdiv <= rdiv + 32'd1;",
              "            end",
              "            if (rbv) begin",
              "                if (rb == 8'h0A) begin",
              "                    if (got && (chan_ok || !chan_seen)) begin",
              "                        words <= buf_w;",
              "                        valid <= 1'b1;",
              "                    end",
              "                    got <= 1'b0; chan_seen <= 1'b0; chan_ok <= 1'b0; ps <= P_SCAN; win <= 0;",
              "                end else if (rb == 8'h20 || rb == 8'h0D || rb == 8'h09) begin",
              "                    if (ps == P_NUM) begin",
              f"                        buf_w[{16 * n - 1} - 16*fld -: 16] <= num;",
              "                        got <= 1'b1;",
              "                        ps  <= P_SCAN;",
              "                    end",
              "                end else begin",
              "                    win <= win_n;",
              "                    case (ps)",
              "                        P_SCAN: begin",
              "                            if (m_chan) chan_seen <= 1'b1;",
              "                            if (m_chan_id) chan_ok <= 1'b1;"]
        for i in range(n):
            L.append(f"                            if (m_{i}) begin fld <= 8'd{i}; ps <= P_PRE; end")
        L += ["                        end",
              "                        P_PRE: begin",
              "                            neg <= 1'b0;",
              "                            acc <= 20'd0;",
              "                            if (rb == 8'h2D) begin neg <= 1'b1; ps <= P_NUM; end",
              "                            else if (is_digit) begin acc <= {16'd0, dval}; ps <= P_NUM; end",
              "                            else if (rb == 8'h74) begin   // true",
              f"                                buf_w[{16 * n - 1} - 16*fld -: 16] <= 16'd1; got <= 1'b1; ps <= P_SCAN;",
              "                            end else if (rb == 8'h66) begin   // false",
              f"                                buf_w[{16 * n - 1} - 16*fld -: 16] <= 16'd0; got <= 1'b1; ps <= P_SCAN;",
              "                            end else ps <= P_SCAN;",
              "                        end",
              "                        P_NUM: begin",
              "                            if (is_digit) begin",
              "                                if (acc < 20'd100000) acc <= acc * 20'd10 + {16'd0, dval};",
              "                            end else begin",
              f"                                buf_w[{16 * n - 1} - 16*fld -: 16] <= num;",
              "                                got <= 1'b1;",
              "                                ps  <= (rb == 8'h2E) ? P_SKIP : P_SCAN;   // '.' -> skip fraction",
              "                            end",
              "                        end",
              "                        default: if (!is_digit) ps <= P_SCAN;",
              "                    endcase",
              "                end",
              "            end",
              "        end",
              "    end",
              "endmodule", "`default_nettype wire"]
        return "\n".join(L) + "\n"

    # ------------------------------------------------------------------ top
    def _to16(self, expr: str, w: int, signed: bool) -> str:
        if w == 16:
            return expr
        if w > 16:
            return f"{expr}[15:0]"
        if w == 1:
            return f"{{15'd0, {expr}}}"
        if signed:
            return f"{{{{{16 - w}{{{expr}[{w - 1}]}}}}, {expr}}}"
        return f"{{{16 - w}'d0, {expr}}}"

    def _to32(self, expr: str, w: int, signed: bool) -> str:
        if w >= 32:
            return expr
        if signed and w > 1:
            return f"{{{{{32 - w}{{{expr}[{w - 1}]}}}}, {expr}}}"
        return f"{{{32 - w}'d0, {expr}}}"

    def _src_value(self, p: Plan, f: str, tports: dict) -> tuple[str, int, bool]:
        fv = vname(f)
        if fv in p.regs:
            r = p.regs[fv]
            return f"w_{p.e.ident}__{fv}", r.width, r.signed
        if fv in p.pins_in:
            return f"{p.e.ident}_{fv}", 1, False
        return "1'b0", 1, False

    def _top(self, ctx: BuildContext, plans: dict[str, Plan]) -> tuple[str, list[dict], dict[str, str]]:
        """-> (top.v text, top port descriptors, extra rtl files)."""
        ports: list[dict] = []
        extra: dict[str, str] = {}
        body: list[str] = []
        hw_ids = {p.e.id for p in plans.values()}
        conn: dict[str, dict[str, str]] = {k: {} for k in plans}

        def port(name, d, w=1, **role):
            ports.append(dict(name=name, dir=d, width=w, **role))

        for key, p in plans.items():
            for pin in p.pins_in:
                port(f"{key}_{pin}", "input", 1, role="uart_rx" if p.kind == "uart_rx" else
                     ("button" if p.kind == "debouncer" else "pin"), ent=key,
                     baud=to_int(p.e.props.get("baud", 115200), 115200),
                     hold_ms=to_int(p.e.props.get("stable_ms", 10), 10) * 3)
                conn[key][pin] = f"{key}_{pin}"
            for trig, tport in p.triggers.items():
                if trig in plans and trig != key:
                    other = plans[trig]
                    pr = (other.primary or list(other.regs) or [None])[0]
                    if pr and pr in other.regs:
                        conn[key][tport] = f"|w_{trig}__{pr}"
                        continue
                port(f"{key}_{tport}", "input", 1, role="trigger", ent=key)
                conn[key][tport] = f"{key}_{tport}"
            for (ent, prop), xport in p.xrefs.items():
                other = plans.get(ent)
                if other and prop in other.regs:
                    conn[key][xport] = f"w_{ent}__{prop}"
                elif other and prop in other.pins_in:
                    conn[key][xport] = f"{ent}_{prop}"
                else:
                    conn[key][xport] = "0"
            for n, r in p.regs.items():
                body.append(f"    wire {'signed ' if r.signed else ''}{self._rng(r.width)}w_{key}__{n};")
                conn[key][f"o_{n}"] = f"w_{key}__{n}"
            if p.out_chans:
                body.append(f"    wire w_{key}__send;")
                conn[key]["o_send"] = f"w_{key}__send"
        body.append("")
        # ---- channels
        seen = set()
        for c in ctx.channels:
            if c.id in seen:
                continue
            seen.add(c.id)
            cid = self._cid(c)
            sp = plans.get(c.src.ident) if c.src.id in hw_ids else None
            dp = plans.get(c.dst.ident) if c.dst.id in hw_ids else None
            n = len(c.fields)
            body.append(f"    // ---- {c.id}: {vcom(c.src.name)} ({c.src.domain}) -> {vcom(c.dst.name)} "
                        f"({c.dst.domain}) via {c.protocol}: {', '.join(vstr(f) for f in c.fields)}")
            if sp and dp:
                body.append("    // both ends are in the FPGA: parallel wires, receiver copies every clock")
                for f in c.fields:
                    ex, w, sg = self._src_value(sp, f, conn)
                    conn[dp.e.ident][f"i_{cid}_{vname(f)}"] = self._to32(ex, w, sg)
                conn[dp.e.ident][f"i_{cid}_we"] = "1'b1"
                continue
            if not sp and not dp:
                continue
            words = []
            if sp:
                for f in c.fields:
                    ex, w, sg = self._src_value(sp, f, conn)
                    words.append(self._to16(ex, w, sg))
            proto = c.protocol
            if proto in ("spi", "i2c"):
                txw = "{" + ", ".join(words) + "}" if sp else f"{{{16 * n}{{1'b0}}}}"
                body.append(f"    wire [{16 * n - 1}:0] w_{cid}_rx;")
                body.append(f"    wire w_{cid}_valid;")
                if proto == "spi":
                    for nm_, d in (("sclk", "input"), ("mosi", "input"), ("cs_n", "input"), ("miso", "output")):
                        port(f"{cid}_{nm_}", d, 1, role="spi", cid=cid, sig=nm_, n=n, send=bool(dp))
                    body += [f"    wire w_{cid}_miso;",
                             f"    assign {cid}_miso = {cid}_cs_n ? 1'bz : w_{cid}_miso;   // tri-state when not selected",
                             f"    nlcl_spi_slave #(.N({n})) u_{cid} (",
                             f"        .clk(clk), .rst(rst), .sclk({cid}_sclk), .mosi({cid}_mosi), .cs_n({cid}_cs_n),",
                             f"        .miso(w_{cid}_miso), .rx_words(w_{cid}_rx), .rx_valid(w_{cid}_valid),",
                             f"        .tx_words({txw}));"]
                else:
                    addr = self._i2c_addr(c.config.get("address", "0x42"))
                    port(f"{cid}_scl", "input", 1, role="i2c", cid=cid, sig="scl", n=n, addr=addr, send=bool(dp))
                    port(f"{cid}_sda", "inout", 1, role="i2c", cid=cid, sig="sda", n=n, addr=addr, send=bool(dp))
                    body += [f"    wire w_{cid}_sda_oe;",
                             f"    assign {cid}_sda = w_{cid}_sda_oe ? 1'b0 : 1'bz;   // open drain, add a pull-up",
                             f"    nlcl_i2c_slave #(.ADDR(7'h{addr:02X}), .N({n})) u_{cid} (",
                             f"        .clk(clk), .rst(rst), .scl({cid}_scl), .sda_i({cid}_sda), .sda_oe(w_{cid}_sda_oe),",
                             f"        .rx_words(w_{cid}_rx), .rx_valid(w_{cid}_valid), .tx_words({txw}));"]
                if dp:
                    for i, f in enumerate(c.fields):
                        hi = 16 * (n - i) - 1
                        conn[dp.e.ident][f"i_{cid}_{vname(f)}"] = \
                            f"{{{{16{{w_{cid}_rx[{hi}]}}}}, w_{cid}_rx[{hi}:{hi - 15}]}}"
                    conn[dp.e.ident][f"i_{cid}_we"] = f"w_{cid}_valid"
            elif proto in BUS_TO_WIRE:
                if sp:
                    for f in c.fields:
                        ex, w, sg = self._src_value(sp, f, conn)
                        pn = f"{cid}_{vname(f)}"
                        port(pn, "output", 1, role="gpio_out")
                        body.append(f"    assign {pn} = |{ex};   // HIGH when {vcom(f)} != 0")
                else:
                    for f in c.fields:
                        pn = f"{cid}_{vname(f)}"
                        port(pn, "input", 1, role="pin", ent=dp.e.ident)
                        body += [f"    reg [1:0] s_{pn};",
                                 f"    always @(posedge clk) s_{pn} <= rst ? 2'b00 : {{s_{pn}[0], {pn}}};"]
                        conn[dp.e.ident][f"i_{cid}_{vname(f)}"] = f"{{31'd0, s_{pn}[1]}}"
                    conn[dp.e.ident][f"i_{cid}_we"] = "1'b1"
            else:
                # serial / http / mqtt / udp / websocket -> UART JSON line
                if sp:
                    port(f"{cid}_tx", "output", 1, role="uart_tx")
                    extra[f"rtl/nlcl_json_tx_{cid}.v"] = self._json_tx(c, cid)
                    send = f"w_{sp.e.ident}__send" if sp.out_chans else "1'b0"
                    body += [f"    nlcl_json_tx_{cid} #(.CLOCK_HZ(CLOCK_HZ)) u_{cid} (",
                             f"        .clk(clk), .rst(rst), .send({send}),",
                             f"        .words({{{', '.join(words)}}}), .tx({cid}_tx));"]
                else:
                    port(f"{cid}_rx", "input", 1, role="json_rx", cid=cid, chan=c.id,
                         fields=list(c.fields), baud=int(c.config.get("baud", 115200)))
                    extra[f"rtl/nlcl_json_rx_{cid}.v"] = self._json_rx(c, cid)
                    body += [f"    wire [{16 * n - 1}:0] w_{cid}_rx;", f"    wire w_{cid}_valid;",
                             f"    nlcl_json_rx_{cid} #(.CLOCK_HZ(CLOCK_HZ)) u_{cid} (",
                             f"        .clk(clk), .rst(rst), .rx({cid}_rx), .words(w_{cid}_rx), .valid(w_{cid}_valid));"]
                    for i, f in enumerate(c.fields):
                        hi = 16 * (n - i) - 1
                        conn[dp.e.ident][f"i_{cid}_{vname(f)}"] = \
                            f"{{{{16{{w_{cid}_rx[{hi}]}}}}, w_{cid}_rx[{hi}:{hi - 15}]}}"
                    conn[dp.e.ident][f"i_{cid}_we"] = f"w_{cid}_valid"
        # ---- observable outputs
        obs = []
        for key, p in plans.items():
            for n in list(dict.fromkeys(p.pins_out + p.primary)):
                if n in p.regs:
                    r = p.regs[n]
                    port(f"{key}_{n}", "output", r.width, role="out")
                    obs.append(f"    assign {key}_{n} = w_{key}__{n};")
        # ---- instances
        inst = []
        for key, p in plans.items():
            c = conn[key]
            pl = [".clk(clk)", ".rst(rst)"]
            for pin in p.pins_in:
                pl.append(f".{pin}({c.get(pin, 1)})")
            for trig, tport in p.triggers.items():
                pl.append(f".{tport}({c.get(tport, '1' + chr(39) + 'b0')})")
            for (ent, prop), xport in p.xrefs.items():
                pl.append(f".{xport}({c.get(xport, '0')})")
            for ch in p.in_chans:
                cid = self._cid(ch)
                for f in ch.fields:
                    pl.append(f".i_{cid}_{vname(f)}({c.get(f'i_{cid}_{vname(f)}', '32' + chr(39) + 'd0')})")
                pl.append(f".i_{cid}_we({c.get(f'i_{cid}_we', '1' + chr(39) + 'b0')})")
            for n in p.regs:
                pl.append(f".o_{n}(w_{key}__{n})")
            if p.out_chans:
                pl.append(f".o_send(w_{key}__send)")
            par = ctx.system.entities.get(p.e.parent) if p.e.parent else None
            if par is not None:
                inst.append(f"    // {key} is part of {vcom(par.name)}"
                            + (f" ({par.domain})" if par.domain != "hardware" else ""))
            inst.append(f"    {p.mod} #(.CLOCK_HZ(CLOCK_HZ)) u_{key} (")
            inst.append(",\n".join("        " + x for x in pl))
            inst.append("    );")
        L = ["`timescale 1ns / 1ps", "`default_nettype none",
             f"// top.v - {vcom(ctx.system.name)}: every hardware entity instantiated and wired. Generated by NLCL.",
             "// Ports: clk, rst (sync, active high), entity pins <entity>_<pin>, link pins <channel>_<signal>.",
             "module top #(",
             f"    parameter CLOCK_HZ = {self._sys_clock(plans)}",
             ") ("]
        pdecl = ["    input  wire clk", "    input  wire rst"]
        for pd in ports:
            kw = {"input": "input  wire", "output": "output wire", "inout": "inout  wire"}[pd["dir"]]
            pdecl.append(f"    {kw} {self._rng(pd['width'])}{pd['name']}")
        L.append(",\n".join(pdecl))
        L.append(");")
        L += body
        L += obs
        L.append("")
        L += inst
        L += ["endmodule", "`default_nettype wire"]
        return "\n".join(L) + "\n", ports, extra

    @staticmethod
    def _sys_clock(plans) -> int:
        for p in plans.values():
            if "clock_hz" in p.e.props:
                return p.clock_hz
        return 50_000_000

    @staticmethod
    def _i2c_addr(a) -> int:
        try:
            if isinstance(a, int):
                v = a
            else:
                s = str(a).strip().lower()
                v = int(s, 16) if s.startswith("0x") else int(s)
            return v & 0x7F
        except (TypeError, ValueError):
            return 0x42

    # ------------------------------------------------------------------ testbench
    def _tb(self, ctx: BuildContext, plans: dict[str, Plan], ports: list[dict]) -> str:
        timers = [ms for p in plans.values() for ms in p.timers]
        for p in plans.values():
            if p.kind == "blinker":
                timers.append(to_int(p.e.props.get("interval_ms", 500), 500) * 2)
        for c in ctx.channels:
            if c.protocol not in ("spi", "i2c", "wire", "gpio"):
                timers.append(to_int(c.config.get("interval_ms", 1000), 1000))
        ms = max(timers + [20])
        cycles = int(min(3_000_000, max(50_000, ms * (SIM_CLOCK_HZ // 1000) * 3)))
        L = ["`timescale 1ns / 1ps", "`default_nettype none",
             "// tb_top.v - self-running testbench for top.v (generated by NLCL).",
             f"// Simulated clock: {SIM_CLOCK_HZ} Hz (1 ms = {SIM_CLOCK_HZ // 1000} cycles); runs {cycles} cycles.",
             "//   iverilog -g2005 -DSIMULATION -s tb_top -o build/sim.vvp -c files.f sim/tb_top.v && vvp build/sim.vvp",
             "//   gtkwave tb_top.vcd",
             "module tb_top;",
             f"    localparam integer SIM_CLOCK_HZ = {SIM_CLOCK_HZ};",
             f"    localparam integer CYCLES = {cycles};",
             "    localparam integer CLK_NS = 1000000000 / SIM_CLOCK_HZ;",
             "    reg clk = 1'b0;", "    reg rst = 1'b1;",
             "    always #(CLK_NS / 2) clk = ~clk;"]
        conns = [".clk(clk)", ".rst(rst)"]
        stim: list[str] = []
        outs = []
        i2c_done, spi_done = set(), set()
        for pd in ports:
            nm, w = pd["name"], pd["width"]
            role = pd.get("role")
            if pd["dir"] == "output":
                L.append(f"    wire {self._rng(w)}{nm};")
                conns.append(f".{nm}({nm})")
                outs.append(nm)
                continue
            if pd["dir"] == "inout":
                L += [f"    wire {nm};", f"    reg {nm}_low = 1'b0;", f"    assign {nm} = {nm}_low ? 1'b0 : 1'bz;",
                      f"    pullup({nm});"]
                conns.append(f".{nm}({nm})")
                continue
            idle = 1 if role in ("uart_rx", "json_rx") or (role == "spi" and pd.get("sig") == "cs_n") or \
                (role == "i2c") else 0
            L.append(f"    reg {self._rng(w)}{nm} = {w}'d{idle};")
            conns.append(f".{nm}({nm})")
            if role in ("trigger", "pin"):
                stim.append(("pulse", nm, 200))
            elif role == "button":
                stim.append(("pulse", nm, max(1, pd.get("hold_ms", 30)) * (SIM_CLOCK_HZ // 1000)))
            elif role == "uart_rx":
                stim.append(("uart", nm, pd.get("baud", 115200), '"A"', 1))
            elif role == "json_rx":
                line = json.dumps(dict([("channel", pd["chan"]), ("from", "tb")] +
                                       [(f, 10 * (i + 1) + 2) for i, f in enumerate(pd["fields"])]),
                                  separators=(", ", ": ")) + "\n"
                stim.append(("uart", nm, pd.get("baud", 115200), '"' + vstr(line) + '"',
                             len(line.encode("ascii", "replace"))))
        for pd in ports:
            if pd.get("role") == "spi" and pd["cid"] not in spi_done:
                spi_done.add(pd["cid"])
                stim.append(("spi", pd["cid"], pd["n"], pd.get("send")))
            if pd.get("role") == "i2c" and pd["cid"] not in i2c_done:
                i2c_done.add(pd["cid"])
                stim.append(("i2c", pd["cid"], pd["n"], pd.get("addr", 0x42), pd.get("send")))
        L.append("")
        L.append("    top #(.CLOCK_HZ(SIM_CLOCK_HZ)) dut (")
        L.append(",\n".join("        " + x for x in conns))
        L.append("    );")
        L.append("")
        # tasks
        L += ["    task wait_cycles(input integer n);", "        begin", "            repeat (n) @(posedge clk);",
              "        end", "    endtask"]
        for s in stim:
            if s[0] == "uart":
                _, nm, baud, text, ln = s
                bit = max(1, SIM_CLOCK_HZ // max(1, baud))
                L += [f"    task uart_{nm};",
                      f"        reg [{8 * ln - 1}:0] msg;", "        integer i, b;", "        begin",
                      f"            msg = {text};",
                      f"            for (i = {ln - 1}; i >= 0; i = i - 1) begin",
                      f"                {nm} = 1'b0; wait_cycles({bit});",
                      "                for (b = 0; b < 8; b = b + 1) begin",
                      f"                    {nm} = msg[8*i + b]; wait_cycles({bit});",
                      "                end",
                      f"                {nm} = 1'b1; wait_cycles({bit * 2});",
                      "            end", "        end", "    endtask"]
            elif s[0] == "spi":
                _, cid, n, send = s
                L += [f"    reg [7:0] spi_in_{cid};",
                      f"    task spi_byte_{cid}(input [7:0] v);", "        integer b;", "        begin",
                      "            for (b = 7; b >= 0; b = b - 1) begin",
                      f"                {cid}_mosi = v[b]; wait_cycles(8);",
                      f"                {cid}_sclk = 1'b1; spi_in_{cid} = {{spi_in_{cid}[6:0], {cid}_miso}}; wait_cycles(8);",
                      f"                {cid}_sclk = 1'b0;",
                      "            end", "            wait_cycles(8);", "        end", "    endtask",
                      f"    task spi_read_{cid};   // 0x5A, then 1 + 2N bytes: 0xA5 + int16 words",
                      "        integer k;", "        begin",
                      f"            {cid}_cs_n = 1'b0; wait_cycles(16);",
                      f"            spi_byte_{cid}(8'h5A);",
                      f"            spi_byte_{cid}(8'h00);",
                      f'            $display("[%0t ns] {cid}: sync byte %h (expect a5)", $time, spi_in_{cid});',
                      f"            for (k = 0; k < {2 * n}; k = k + 1) begin",
                      f"                spi_byte_{cid}(8'h00);",
                      f'                $display("[%0t ns] {cid}: data byte %0d = %h", $time, k, spi_in_{cid});',
                      "            end",
                      f"            wait_cycles(16); {cid}_cs_n = 1'b1;", "        end", "    endtask",
                      f"    task spi_frame_{cid};", "        integer k;", "        begin",
                      f"            {cid}_cs_n = 1'b0; wait_cycles(16);",
                      f"            spi_byte_{cid}(8'hA5); spi_byte_{cid}(8'd{n});",
                      f"            for (k = 0; k < {n}; k = k + 1) begin",
                      f"                spi_byte_{cid}(8'h00); spi_byte_{cid}(8'd20 + k[7:0] * 8'd10);",
                      "            end",
                      f"            wait_cycles(16); {cid}_cs_n = 1'b1;", "        end", "    endtask"]
            elif s[0] == "i2c":
                _, cid, n, addr, send = s
                sda, scl = f"{cid}_sda_low", f"{cid}_scl"
                L += [f"    task i2c_byte_{cid}(input [7:0] v);", "        integer b;", "        begin",
                      "            for (b = 7; b >= 0; b = b - 1) begin",
                      f"                {sda} = ~v[b]; wait_cycles(16);",
                      f"                {scl} = 1'b1; wait_cycles(16);",
                      f"                {scl} = 1'b0; wait_cycles(4);",
                      "            end",
                      f"            {sda} = 1'b0; wait_cycles(16);   // release for ACK",
                      f"            {scl} = 1'b1; wait_cycles(16);",
                      f"            {scl} = 1'b0; wait_cycles(16);", "        end", "    endtask",
                      f"    task i2c_frame_{cid};", "        integer k;", "        begin",
                      f"            {sda} = 1'b1; wait_cycles(16);   // START: SDA low while SCL high",
                      f"            {scl} = 1'b0; wait_cycles(16);",
                      f"            i2c_byte_{cid}(8'h{(addr << 1) & 0xFE:02X});",
                      f"            i2c_byte_{cid}(8'hA5); i2c_byte_{cid}(8'd{n});",
                      f"            for (k = 0; k < {n}; k = k + 1) begin",
                      f"                i2c_byte_{cid}(8'h00); i2c_byte_{cid}(8'd20 + k[7:0] * 8'd10);",
                      "            end",
                      f"            {sda} = 1'b1; wait_cycles(16);",
                      f"            {scl} = 1'b1; wait_cycles(16);",
                      f"            {sda} = 1'b0; wait_cycles(16);   // STOP: SDA high while SCL high",
                      "        end", "    endtask"]
        L += ["", "    integer cyc;", "    initial begin",
              '        $dumpfile("tb_top.vcd");', "        // depth 2 = testbench + every entity register in top (0 = all signals: huge VCD)",
              "        $dumpvars(2, tb_top);",
              "        wait_cycles(5);", "        rst = 1'b0;", "    end", ""]
        # stimulus process
        L += ["    initial begin", "        wait_cycles(20);"]
        if not stim:
            L.append("        // no external inputs: the design runs on its own")
        for s in stim:
            if s[0] == "pulse":
                _, nm, hold = s
                L += [f"        repeat (3) begin", f"            {s[1]} = 1'b1; wait_cycles({hold});",
                      f"            {s[1]} = 1'b0; wait_cycles({hold});", "        end"]
            elif s[0] == "uart":
                L.append(f"        uart_{s[1]};")
            elif s[0] == "spi":
                L.append(f"        spi_frame_{s[1]};" if s[3] else f"        wait_cycles(CYCLES / 2); spi_read_{s[1]};")
            elif s[0] == "i2c":
                L.append(f"        i2c_frame_{s[1]};" if s[4] else f"        // {s[1]}: FPGA is the sender; the master reads it")
        L += ["    end", ""]
        fmt = " ".join(f"{o}=%0d" for o in outs[:16])
        args = "".join(f", {o}" for o in outs[:16])
        L += ["    initial begin", "        wait_cycles(10);",
              "        for (cyc = 0; cyc < 10; cyc = cyc + 1) begin",
              "            wait_cycles(CYCLES / 10);",
              f'            $display("[%0t ns] {fmt}", $time{args});' if outs else
              '            $display("[%0t ns] running", $time);',
              "        end",
              '        $display("tb_top: done after %0d cycles", CYCLES);',
              "        $finish;", "    end", "endmodule", "`default_nettype wire"]
        return "\n".join(L) + "\n"

    # ------------------------------------------------------------------ host bridge
    def _bridge(self, ctx: BuildContext) -> str | None:
        hw = {e.id for e in ctx.entities}
        chans = []
        for c in ctx.channels:
            if c.protocol in ("http", "websocket", "mqtt", "udp") and (c.src.id in hw) != (c.dst.id in hw):
                chans.append(dict(id=c.id, dir="out" if c.src.id in hw else "in", protocol=c.protocol,
                                  src=c.src.ident, fields=list(c.fields), config=c.config,
                                  pin=f"{self._cid(c)}_{'tx' if c.src.id in hw else 'rx'}"))
        if not chans:
            return None
        return BRIDGE.replace("__CHANNELS__", json.dumps(chans, indent=4, ensure_ascii=True))

    # ------------------------------------------------------------------ generate
    @staticmethod
    def _unique_view(ctx: BuildContext) -> BuildContext:
        """Hardware entities whose identifier is empty (e.g. a Persian name) or clashes with
        another one get a private ASCII name for this build, so no module is silently dropped."""
        import copy
        import dataclasses
        seen, ren = set(), {}
        for e in ctx.entities:
            k = e.ident
            if not k or k in seen:
                base = k or (ident(e.kind) or "obj")
                k = f"{base}_{ident(e.id) or len(seen)}"
                while k in seen:
                    k += "_2"
                ren[e.id] = dataclasses.replace(e, name=k)
            seen.add(k)
        if not ren:
            return ctx
        v = copy.copy(ctx)
        v.entities = [ren.get(e.id, e) for e in ctx.entities]
        v.channels = [dataclasses.replace(c, src=ren.get(c.src.id, c.src), dst=ren.get(c.dst.id, c.dst))
                      for c in ctx.channels]
        return v

    def _wire_view(self, ctx: BuildContext) -> BuildContext:
        """FPGA-internal links (both ends hardware) are ours alone, so their default field list
        (every numeric prop of the source, including names like 'alu1_result' that only refer to
        other entities) is narrowed to the source's real outputs unless the user named fields."""
        import copy
        import dataclasses
        links = getattr(ctx.system, "links", {}) or {}
        idents = sorted({e.ident for e in ctx.entities}, key=len, reverse=True)

        def bogus(f):
            n = ident(f)
            return n.startswith("_") or n in idents or any(n.startswith(o + "_") and len(n) > len(o) + 1
                                                            for o in idents)
        out, changed = [], False
        for c in ctx.channels:
            lk = links.get(c.id)
            explicit = bool(lk is not None and (getattr(lk, "props", {}) or {}).get("fields"))
            if not self._wire_like(c) or explicit:
                out.append(c)
                continue
            kind = self._kind(c.src)
            fields = [f for f in c.fields if not bogus(f)]
            prim = [f for f in SPECS[kind]["primary"] if f in fields or f in SPECS[kind]["regs"] or f == "state"]
            if kind != "object" and prim:
                fields = prim
            fields = fields or list(c.fields)
            if fields != list(c.fields):
                changed = True
                c = dataclasses.replace(c, fields=fields)
            out.append(c)
        if not changed:
            return ctx
        v = copy.copy(ctx)
        v.channels = out
        return v

    def generate(self, ctx: BuildContext) -> dict[str, str]:
        try:
            ctx = self._unique_view(ctx)
        except Exception:
            pass
        try:
            ctx = self._wire_view(ctx)
        except Exception:
            pass
        files: dict[str, str] = {}
        plans: dict[str, Plan] = {}
        used: set = set()
        self._plans = plans
        for e in ctx.entities:
            try:
                plans[e.ident] = self._plan(ctx, e, used)
            except Exception as ex:  # never raise: fall back to an empty register bank
                p = Plan(e)
                p.mod = vname(e.ident) + "_m"
                p.comments.append(f"planning failed: {vstr(ex)}")
                plans[e.ident] = p
        try:   # a field received over an FPGA-internal link keeps the sender's width
            for p in plans.values():
                for c in p.in_chans:
                    q = plans.get(c.src.ident)
                    if not self._wire_like(c) or q is None:
                        continue
                    for f in c.fields:
                        r, sr = p.regs.get(vname(f)), q.regs.get(vname(f))
                        if r is not None and sr is not None and r.note.startswith("channel field"):
                            r.width = sr.width
        except Exception:
            pass
        try:
            self._rehome(plans)
        except Exception:
            pass
        for p in plans.values():
            self._render_behaviours(ctx, p, plans)
        for p in plans.values():
            # keep module names distinct from the signals inside (e.g. a mux called "sel")
            if p.mod in p.regs or p.mod in p.pins_in:
                base = p.mod + "_m"
                while base in used or base in p.regs:
                    base += "_m"
                used.add(base)
                p.mod = base
        for key, p in plans.items():
            try:
                files[f"rtl/{p.mod}.v"] = self._module(ctx, p)
            except Exception as ex:
                files[f"rtl/{p.mod}.v"] = (f"// generation failed for {vcom(p.e.name)}: {vcom(ex)}\n"
                                         f"module {p.mod} (input wire clk, input wire rst);\nendmodule\n")
        top, ports, extra = self._top(ctx, plans)
        files["rtl/top.v"] = top
        files.update(extra)
        protos = {c.protocol for c in ctx.channels}
        hw_ids = {p.e.id for p in plans.values()}
        needs = {c.protocol for c in ctx.channels if (c.src.id in hw_ids) != (c.dst.id in hw_ids)}
        if "spi" in needs:
            files["rtl/nlcl_spi_slave.v"] = (ASSETS / "nlcl_spi_slave.v").read_text(encoding="utf-8")
        if "i2c" in needs:
            files["rtl/nlcl_i2c_slave.v"] = (ASSETS / "nlcl_i2c_slave.v").read_text(encoding="utf-8")
        files["sim/tb_top.v"] = self._tb(ctx, plans, ports)
        rtl = [k for k in files if k.startswith("rtl/")]
        rtl = [k for k in rtl if k != "rtl/top.v"] + ["rtl/top.v"]
        self._rtl_files = rtl
        files["synth.ys"] = self._synth(rtl)
        files["files.f"] = "\n".join(rtl) + "\n"
        br = self._bridge(ctx)
        if br:
            files["host_bridge.py"] = br
        files["README.md"] = self._readme(ctx, plans, ports, rtl, bool(br), protos)
        return files

    def _synth(self, rtl: list[str]) -> str:
        return "\n".join([
            "# Yosys synthesis script (generated by NLCL):  yosys -s synth.ys",
            "# generic gate-level synthesis + statistics; swap the 'synth' line for an FPGA flow:",
            "#   synth_ice40 -top top -json build/top_ice40.json   (then nextpnr-ice40)",
            "#   synth_ecp5  -top top -json build/top_ecp5.json    (then nextpnr-ecp5)",
            "#   synth_gowin -top top -json build/top_gowin.json",
            *[f"read_verilog -defer {f}" for f in rtl],
            "hierarchy -check -top top",
            "proc",
            "tribuf",
            "synth -top top",
            "check",
            "stat",
            "write_json build/top.json",
            "write_verilog -noattr build/top_synth.v",
            ""])

    def _readme(self, ctx, plans, ports, rtl, bridge, protos) -> str:
        L = [f"# {ctx.system.name} - hardware (Verilog-2005)", "",
             "Generated by NLCL. Synthesizable RTL, a self-running testbench (stimulus + periodic $display + VCD) and a Yosys script.", "",
             "## Files", "",
             "| file | contents |", "|---|---|"]
        for key, p in plans.items():
            par = ctx.system.entities.get(p.e.parent) if p.e.parent else None
            L.append(f"| `rtl/{p.mod}.v` | module `{p.mod}` (entity {key}) - {p.kind} ({p.W}-bit)"
                     + (f", part of {vcom(par.name)}" if par is not None else "") + " |")
        for f in rtl:
            if "nlcl_" in f:
                L.append(f"| `{f}` | link adapter |")
        L += ["| `rtl/top.v` | instantiates everything, wires the links |",
              "| `sim/tb_top.v` | testbench: clock, reset, stimulus, VCD dump |",
              "| `synth.ys` | Yosys script |", "| `files.f` | file list for tools |"]
        if bridge:
            L.append("| `host_bridge.py` | relays FPGA UART JSON lines to the hub / MQTT / UDP |")
        L += ["", "## Top-level ports", "", "| port | dir | width | meaning |", "|---|---|---|---|",
              "| clk | in | 1 | system clock (CLOCK_HZ parameter) |", "| rst | in | 1 | synchronous reset, active high |"]
        for pd in ports:
            L.append(f"| {pd['name']} | {pd['dir']} | {pd['width']} | {pd.get('role', '')} |")
        L += ["", "## Simulate (free)", "", "```sh", "mkdir build",
              "iverilog -g2005 -DSIMULATION -s tb_top -o build/sim.vvp sim/tb_top.v " + " ".join(rtl),
              "vvp build/sim.vvp", "gtkwave tb_top.vcd", "```", "",
              "- Icarus Verilog for Windows: https://bleyer.org/icarus/ (includes GTKWave); Linux `apt install iverilog gtkwave`.",
              "- Verilator lint: `verilator --lint-only -Wall -Wno-fatal -DSIMULATION --top-module top " + " ".join(rtl) + "`",
              "- No install: https://www.edaplayground.com - choose *Icarus Verilog 12*, paste `sim/tb_top.v` as testbench,",
              "  the `rtl/*.v` files as design, add `-DSIMULATION` to compile options, tick *Open EPWave after run*.",
              "", "## Synthesize (free)", "",
              "```sh", "mkdir build", "yosys -s synth.ys", "```",
              "Yosys is part of the free OSS CAD Suite (github.com/YosysHQ/oss-cad-suite-build). For a real board",
              "replace `synth` with `synth_ice40` / `synth_ecp5` and run nextpnr + the vendor-free packer.",
              "", "## Behaviour mapping", "",
              "- `update` -> every clock; `timer:<ms>` -> divider tick derived from `CLOCK_HZ`;",
              "  `input:<x>` -> input port `<entity>_in_<x>` with 2-FF synchroniser + rising-edge detect;",
              "  `change:<p>` -> compare with a delayed copy; `message:<src>` -> link valid strobe.",
              "- `set/add/toggle` -> non-blocking assignments; `log` -> `$display` under `ifdef SIMULATION`;",
              "  `destroy` -> clears the module's `nl_alive` register (it stops updating).",
              "- Numbers are unsigned integers of the entity `width` (floats are rounded).",
              "", "## Link byte protocols", "",
              "- **wire / gpio between FPGA entities**: parallel buses, the receiver copies same-named fields every clock.",
              "- **SPI (mode 0, MSB first) / I2C slave** (from a micro-controller): write frame `0xA5, N, N x int16 big-endian`",
              "  in field order; read: SPI master sends `0x5A` then clocks `1+2N` bytes (`0xA5` + words),",
              "  I2C read returns `0xA5, N, words`. SCLK <= clk/8, SCL <= clk/16.",
              "- **serial / http / mqtt / udp**: 8N1 UART carrying one flat JSON line per message,",
              "  `{\"channel\":\"<id>\",\"from\":\"<src>\",\"<field>\":<int>}\\n` (int16 values).",
              "  Serial peers read it directly; for http/mqtt/udp run `python host_bridge.py --port COM5` on the PC",
              "  wired to the FPGA UART (USB-UART adapter) to relay it to the NLCL hub / broker.", ""]
        return "\n".join(L)

    # ------------------------------------------------------------------ run
    def run(self, ctx: BuildContext) -> RunResult | None:
        top = ctx.outdir / "rtl" / "top.v"
        if not top.exists():
            return RunResult(False, "build first")
        rtl = sorted(str(p.relative_to(ctx.outdir)).replace("\\", "/") for p in (ctx.outdir / "rtl").glob("*.v"))
        iv, vvp = self.which("iverilog"), self.which("vvp")
        if iv and vvp:
            (ctx.outdir / "build").mkdir(exist_ok=True)
            r = self.sh([iv, "-g2005", "-DSIMULATION", "-s", "tb_top", "-o", "build/sim.vvp", "sim/tb_top.v"] + rtl,
                        ctx.outdir, timeout=60)
            if not r.ok:
                return r
            r2 = self.sh([vvp, "-n", "build/sim.vvp"], ctx.outdir, timeout=120)
            return RunResult(r2.ok, (r.output + "\n" + r2.output).strip(), r.command + " && " + r2.command)
        vl = self.which("verilator", "verilator_bin")
        if vl:
            return self.sh([vl, "--lint-only", "-Wall", "-Wno-fatal", "-DSIMULATION", "--top-module", "top"] + rtl,
                           ctx.outdir, timeout=60)
        return None


BRIDGE = r'''#!/usr/bin/env python3
"""Host bridge: FPGA UART JSON lines <-> NLCL hub (HTTP) / MQTT / UDP.  Generated by NLCL.

The FPGA prints one flat JSON line per message on its <channel>_tx pin and parses
lines arriving on <channel>_rx.  Wire every UART pin to one USB-UART adapter
(or edit top.v to share one TX/RX pair), then:

    pip install pyserial            # paho-mqtt too if a channel uses mqtt
    python host_bridge.py --port COM5 --baud 115200
    python host_bridge.py --demo    # no FPGA: prints what would be relayed
"""
import argparse
import json
import socket
import sys
import threading
import time
import urllib.request

CHANNELS = __CHANNELS__


def http_url(ch):
    cfg = ch["config"]
    return cfg.get("hub_url", "http://localhost:8765").rstrip("/") + cfg.get("path", "/channel/" + ch["id"])


class Bridge:
    def __init__(self, ser, demo=False):
        self.ser, self.demo, self.lock = ser, demo, threading.Lock()
        self.mqtt = None
        self.seen = {}
        if any(c["protocol"] == "mqtt" for c in CHANNELS):
            try:
                import paho.mqtt.client as mqtt
                cfg = next(c["config"] for c in CHANNELS if c["protocol"] == "mqtt")
                try:  # paho-mqtt >= 2 needs an explicit callback API version
                    self.mqtt = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
                except AttributeError:
                    self.mqtt = mqtt.Client()
                self.mqtt.on_message = self.on_mqtt
                self.mqtt.connect(cfg.get("broker", "broker.hivemq.com"), int(cfg.get("broker_port", 1883)))
                for c in CHANNELS:
                    if c["protocol"] == "mqtt" and c["dir"] == "in":
                        self.mqtt.subscribe(c["config"].get("topic", "nlcl/" + c["id"]))
                self.mqtt.loop_start()
            except Exception as e:  # noqa: BLE001
                print("[bridge] mqtt unavailable:", e)

    def to_fpga(self, ch, msg):
        line = json.dumps(dict({"channel": ch["id"], "from": ch["src"]},
                               **{f: int(round(float(msg.get(f, 0)))) for f in ch["fields"]}),
                          separators=(",", ":")) + "\n"
        with self.lock:
            if self.ser:
                self.ser.write(line.encode())
        print("[bridge] -> fpga", line.strip())

    def from_fpga(self, msg):
        ch = next((c for c in CHANNELS if c["id"] == msg.get("channel") and c["dir"] == "out"), None)
        if not ch:
            return
        data = json.dumps(msg).encode()
        try:
            if ch["protocol"] in ("http", "websocket"):
                urllib.request.urlopen(urllib.request.Request(http_url(ch), data,
                                                              {"Content-Type": "application/json"}), timeout=2).read()
            elif ch["protocol"] == "mqtt" and self.mqtt:
                self.mqtt.publish(ch["config"].get("topic", "nlcl/" + ch["id"]), data)
            elif ch["protocol"] == "udp":
                socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(data, ("127.0.0.1", int(ch["config"].get("port", 9876))))
            print("[bridge] fpga ->", ch["protocol"], msg)
        except OSError as e:
            print("[bridge] relay failed:", e)

    def on_mqtt(self, client, userdata, m):
        for c in CHANNELS:
            if c["dir"] == "in" and c["protocol"] == "mqtt" and m.topic == c["config"].get("topic", "nlcl/" + c["id"]):
                try:
                    self.to_fpga(c, json.loads(m.payload or b"{}"))
                except ValueError:
                    pass

    def poll(self):
        udp = {}
        for c in CHANNELS:
            if c["dir"] == "in" and c["protocol"] == "udp":
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.bind(("0.0.0.0", int(c["config"].get("port", 9876))))
                s.settimeout(0.05)
                udp[c["id"]] = s
        while True:
            for c in CHANNELS:
                if c["dir"] != "in":
                    continue
                if c["protocol"] in ("http", "websocket"):
                    try:
                        msg = json.loads(urllib.request.urlopen(http_url(c), timeout=2).read() or b"{}")
                    except (OSError, ValueError):
                        continue
                    if msg and msg.get("_t") != self.seen.get(c["id"]):
                        self.seen[c["id"]] = msg.get("_t")
                        self.to_fpga(c, msg)
                elif c["protocol"] == "udp":
                    try:
                        self.to_fpga(c, json.loads(udp[c["id"]].recv(4096)))
                    except (OSError, ValueError):
                        pass
            time.sleep(0.2)

    def read_serial(self):
        buf = b""
        while self.ser:
            buf += self.ser.read(256)
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    self.from_fpga(json.loads(line.decode("ascii", "replace")))
                except ValueError:
                    pass


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", help="serial port of the FPGA UART (COM5, /dev/ttyUSB0)")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--demo", action="store_true", help="no FPGA: just show the channel table")
    a = ap.parse_args()
    for c in CHANNELS:
        print(f"[bridge] {c['id']}: {c['dir']} via {c['protocol']} pin {c['pin']} fields {c['fields']}")
    if a.demo or not a.port:
        if not a.demo:
            print("[bridge] give --port to connect to the FPGA")
        return 0
    try:
        import serial
    except ImportError:
        print("pip install pyserial")
        return 1
    ser = serial.Serial(a.port, a.baud, timeout=0.1)
    b = Bridge(ser)
    threading.Thread(target=b.read_serial, daemon=True).start()
    try:
        b.poll()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

MODULE = HardwareModule()
