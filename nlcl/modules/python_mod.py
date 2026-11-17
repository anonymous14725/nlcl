"""Python domain: objects, services, dashboards, the HTTP hub and bridges.

This is also the *glue* module of multi-module systems: whenever two other
modules talk over HTTP (or a serial device must reach a browser), the
composer asks this module to emit the hub/bridge even if the user never
created a Python object.
"""
from __future__ import annotations

import keyword
import sys
from pathlib import Path

from ..ir import Entity, describe_action
from .base import BuildContext, DomainModule, RunResult

ASSET = Path(__file__).parent / "_assets" / "nlcl_runtime.py"

# attributes/methods of nlcl_runtime.Entity: a user property with one of these names
# is stored under "<name>_" (the wire/state name stays the same, see Entity.aliases)
RESERVED_ATTRS = {"name", "world", "alive", "props", "kind", "aliases", "every", "state", "log",
                  "destroy", "move", "send", "get", "on_start", "on_update", "on_change",
                  "on_message", "on_collision", "on_input", "rotation"}
# names main.py itself defines at module level
RESERVED_CLASSES = {"Entity", "World", "Hub", "make_channel", "main_args", "build", "_Relay"}


def method(call: str) -> str:
    """Python method name for a user-named action ("call shutdown")."""
    if call in RESERVED_ATTRS or keyword.iskeyword(call) or call.startswith("_"):
        return "do_" + call.lstrip("_")
    return call


def attr(prop: str) -> str:
    """Python attribute name used for a property."""
    if prop in RESERVED_ATTRS or keyword.iskeyword(prop) or prop.startswith("_"):
        return prop + "_"
    return prop


class PythonModule(DomainModule):
    name = "python"
    title = "Python (software objects, services, hub, bridges)"
    aliases = ("py", "software", "پایتون")
    keywords = ("python", "script", "class", "service", "server", "dashboard", "api", "backend",
                "logger", "database", "monitor", "controller", "bot", "agent", "counter", "timer",
                "inventory", "bank", "account", "calculator", "queue", "stack")
    kinds = {
        "object": {},
        "class": {},
        "service": {},
        "dashboard": {},
        "server": {"port": 8000},
        "logger": {},
        "monitor": {},
        "counter": {"count": 0},
        "timer": {"elapsed": 0},
        "account": {"balance": 0},
    }
    suggested_props = ("count", "balance", "threshold", "interval_ms", "label")
    protocols = ("http", "serial", "mqtt", "udp", "websocket")
    toolchain = """Run with free CPython 3.10+ (python.org):

    python main.py               # real transports (hub on :8765, serial/mqtt if available)
    python main.py --simulate    # no hardware/network needed: remote data is simulated
    python main.py --seconds 10  # stop automatically

Optional free packages: `pip install pyserial paho-mqtt`.
Open http://localhost:8765 for the live channel dashboard."""

    # ------------------------------------------------------------------ build
    def generate(self, ctx: BuildContext) -> dict[str, str]:
        files = {"nlcl_runtime.py": ASSET.read_text(encoding="utf-8")}
        files["main.py"] = self._main(ctx)
        return files

    def _name(self, ctx: BuildContext, owner: Entity):
        def f(n: str) -> str:
            if "." in n:
                ent, prop = n.split(".", 1)
                if ent in (owner.ident, owner.name):
                    return f"self.{attr(prop)}"
                return f"self.world.get_remote({ent!r}, {prop!r})"
            return f"self.{attr(n)}"
        return f

    def _class_names(self, ctx: BuildContext) -> dict[str, str]:
        out, used = {}, set(RESERVED_CLASSES)
        for e in ctx.entities:
            cn = e.class_name
            if not cn.isidentifier() or keyword.iskeyword(cn):
                cn = "E" + "".join(ch for ch in cn if ch.isalnum() or ch == "_")
            base, i = cn, 2
            if cn in used:
                cn = base = base + "Entity"
            while cn in used:
                cn, i = f"{base}{i}", i + 1
            used.add(cn)
            out[e.id] = cn
        return out

    def _class(self, ctx: BuildContext, e: Entity) -> list[str]:
        cname = self._cls[e.id]
        L = [f"class {cname}(Entity):", f"    kind = {e.kind!r}"]
        props = {k: v for k, v in e.props.items() if not k.startswith("_")}
        for b in e.behaviors:   # "rotate it by 90 degrees" needs the angle attribute to exist
            for a in b["do"]:
                p = str(a.get("prop", ""))
                if p.split(".")[-1] in ("angle", "angle_x", "angle_y") and (
                        "." not in p or p.split(".", 1)[0] in (e.ident, e.name)):
                    props.setdefault(p.split(".")[-1], 0)
        L.append(f"    props = {tuple(props)!r}")
        aliases = {k: attr(k) for k in props if attr(k) != k}
        if aliases:
            L.append(f"    aliases = {aliases!r}")
        L += ["", "    def __init__(self, world, name=" + repr(e.ident) + "):",
              "        super().__init__(world, name)"]
        for k, v in props.items():
            L.append(f"        self.{attr(k)} = {self.literal(v, 'python')}")
        timers = [b for b in e.behaviors if b["on"].startswith("timer:")]
        for i, b in enumerate(timers):
            L.append(f"        self.every({b['on'].split(':')[1]}, self._timer_{i})")
        L.append("")
        name = self._name(ctx, e)
        groups: dict[str, list[dict]] = {}
        foreign_changes = []   # "when door.open changes" on another entity: polled in on_update
        for b in e.behaviors:
            key = b["on"].split(":")[0]
            if key == "change" and "." in b["on"]:
                ent, prop = b["on"].split(":", 1)[1].split(".", 1)
                if ent not in (e.ident, e.name):
                    foreign_changes.append((ent, prop, b))
                    continue
                b = dict(b, on=f"change:{prop}")
            groups.setdefault(key, []).append(b)

        def body(b, indent="        ", edge=None):
            out = []
            ind = indent
            if b.get("if"):
                cond = self.expr(b['if'], 'python', name)
                if edge is not None:
                    # "when X ..." fires once each time X becomes true, not on every tick
                    out.append(f"{indent}if self.rising({edge}, {cond}):")
                else:
                    out.append(f"{indent}if {cond}:")
                ind = indent + "    "
            for a in b["do"]:
                out += [ind + line for line in self._action(a, name)]
            return out

        if "start" in groups:
            L.append("    def on_start(self):")
            for b in groups["start"]:
                L += body(b)
            L.append("")
        auto_elapsed = (e.kind == "timer" and "elapsed" in props and not any(
            a.get("prop") == "elapsed" for b in e.behaviors for a in b["do"]))
        if "update" in groups or auto_elapsed or foreign_changes:
            L.append("    def on_update(self, dt):")
            if auto_elapsed:
                L.append("        self.elapsed += int(round(dt * 1000))  # a timer counts milliseconds")
            for ent, prop, b in foreign_changes:
                L.append(f"        if self.changed({ent + '.' + prop!r}, self.world.get_remote({ent!r}, {prop!r}, None)):")
                L += body(b, "            ")
            for i, b in enumerate(groups.get("update", [])):
                L += body(b, edge=i)
            L.append("")
        for i, b in enumerate(timers):
            L.append(f"    def _timer_{i}(self):  # every {b['on'].split(':')[1]} ms")
            L += body(b)
            L.append("")
        if "change" in groups:
            L.append("    def on_change(self, prop):")
            for b in groups["change"]:
                prop = b["on"].split(":", 1)[1]
                L.append(f"        if prop == {prop!r}:")
                L += body(b, "            ")
            L.append("")
        if "message" in groups:
            L.append("    def on_message(self, sender, msg):")
            for b in groups["message"]:
                src = b["on"].split(":", 1)[1]
                if src != "any":
                    L.append(f"        if sender == {src!r}:")
                    L += body(b, "            ")
                else:
                    L += body(b)
            L.append("")
        if "collision" in groups:
            L.append("    def on_collision(self, other):")
            for b in groups["collision"]:
                w = b.get("with")
                if w:
                    # "when hero touches gold": only a partner whose name or kind matches
                    L.append(f"        if other.name == {w!r} or getattr(other, 'kind', None) == {w!r}:")
                    L += body(b, "            ")
                else:
                    L += body(b)
            L.append("")
        if "input" in groups:
            L.append("    def on_input(self, key):")
            for b in groups["input"]:
                L.append(f"        if key == {b['on'].split(':', 1)[1]!r}:")
                L += body(b, "            ")
            L.append("")
        calls = {a["name"] for b in e.behaviors for a in b["do"]
                 if a["op"] == "call" and str(a["name"]).isidentifier()}
        for c in sorted(calls):
            L.append(f"    def {method(c)}(self, *args):")
            impl = None
            if ctx.llm is not None:
                try:
                    impl = ctx.llm.write_body("Python method (self is the object)", c.replace("_", " "),
                                              f"class {e.class_name} with attributes {props}")
                except Exception:
                    impl = None
            if impl:
                L += ["        " + ln for ln in impl.splitlines()]
            else:
                L.append(f'        self.log("{c} called")  # TODO: implement')
            L.append("")
        if len(L) and L[-1] != "":
            L.append("")
        return L

    def _action(self, a: dict, name) -> list[str]:
        op = a["op"]
        if op in ("set", "add", "toggle"):
            tgt = name(a["prop"])
            if not tgt.startswith("self.world."):
                if op == "set":
                    return [f"{tgt} = {self.expr(a['value'], 'python', name)}"]
                if op == "add":
                    return [f"{tgt} += {self.expr(a['value'], 'python', name)}"]
                return [f"{tgt} = not {tgt}"]
            # property of another entity (e.g. "set door.open to true")
            ent, prop = a["prop"].split(".", 1)
            if op == "set":
                val = self.expr(a["value"], "python", name)
            elif op == "add":
                val = f"{tgt} + ({self.expr(a['value'], 'python', name)})"
            else:
                val = f"not {tgt}"
            return [f"self.world.set_remote({ent!r}, {prop!r}, {val})"]
        if op == "destroy":
            return ["self.destroy()", "return"]
        if op == "log":
            return [f"self.log({a['text']!r})"]
        if op == "move":
            return [f"self.move({self.expr(a['x'], 'python', name)}, {self.expr(a['y'], 'python', name)}, "
                    f"{self.expr(a['z'], 'python', name)})"]
        if op == "send":
            return [f"self.send({a.get('to')!r}, {a.get('fields') or None!r})"]
        if op == "call":
            args = ", ".join(self.expr(x, "python", name) for x in a.get("args", []))
            m = a["name"]
            return [f"self.{method(m)}({args})" if m.isidentifier() else f"pass  # call {m}({args})"]
        return [f"pass  # {describe_action(a)}"]

    def _main(self, ctx: BuildContext) -> str:
        sysname = ctx.system.name
        local = {e.id for e in ctx.entities}
        self._cls = self._class_names(ctx)
        L = ['#!/usr/bin/env python3',
             f'"""{sysname} - python module. Generated by NLCL; edit freely."""',
             "from nlcl_runtime import Entity, World, Hub, make_channel, main_args", "", ""]
        for e in ctx.entities:
            L += self._class(ctx, e)
            L.append("")
        L += ["def build(world):"]
        if not ctx.entities:
            L.append("    pass  # this process only relays/bridges data between other modules")
        for e in ctx.entities:
            L.append(f"    world.add({self._cls[e.id]}(world))")
        hub_ports = set()
        for c in ctx.channels:
            kw = {"interval_ms": c.config.get("interval_ms", 1000),
                  "local_src": c.src.id in local, "local_dst": c.dst.id in local}
            for k in ("baud", "port", "hub_url", "path", "broker", "broker_port", "topic", "types", "enums"):
                if k in c.config:
                    kw[k] = c.config[k]
            if c.protocol in ("http", "websocket"):
                hub_ports.add(int(c.config.get("hub_url", "http://localhost:8765").rsplit(":", 1)[1]))
            if c.protocol in ("spi", "i2c", "gpio", "wire"):
                continue  # electrical buses are handled by embedded/hardware modules
            if c.src.id not in local and c.dst.id not in local and c.protocol not in ("serial",):
                continue  # other modules talk to the hub directly; nothing to do here
            kwtxt = ", ".join(f"{k}={v!r}" for k, v in kw.items())
            L.append(f"    # {c.src.name} ({c.src.domain}) -> {c.dst.name} ({c.dst.domain}) via {c.protocol}")
            L.append(f"    world.channel(make_channel({c.protocol!r}, {c.id!r}, {c.src.ident!r}, {c.dst.ident!r}, "
                     f"{c.fields!r}, {kwtxt}))")
            if c.protocol == "serial" and c.src.id not in local and c.dst.id not in local:
                # bridge serial device -> hub so browsers/game engines can read it
                hub_ports.add(8765)
                L.append(f"    world.channel(make_channel('http', {c.id!r}, {c.src.ident!r}, '__hub__', "
                         f"{c.fields!r}, interval_ms={kw['interval_ms']}, local_src=True))  # serial->hub bridge")
                L.append(f"    world.add(_Relay(world, {c.src.ident!r}, {c.fields!r}))")
        L.append("")
        if any(c.protocol == "serial" and c.src.id not in local and c.dst.id not in local for c in ctx.channels):
            L = L[:5] + [
                "class _Relay(Entity):",
                '    """Mirror of a remote device so its data can be re-published to the hub."""',
                "    def __init__(self, world, name, fields):",
                "        super().__init__(world, name)",
                "        self.props = tuple(fields)",
                "        for f in fields:",
                "            setattr(self, f, 0)",
                "",
                "    def on_update(self, dt):",
                "        for f in self.props:",
                "            setattr(self, f, self.world.remote.get((self.name, f), getattr(self, f)))",
                "", ""] + L[5:]
        L += ["", "if __name__ == '__main__':",
              "    args = main_args()",
              "    world = World(simulate=args.simulate, verbose=not args.quiet)"]
        if hub_ports:
            for p in sorted(hub_ports):
                L.append(f"    hub = Hub({p}) if not args.no_hub else None")
                L.append("    if hub: hub.start()")
        L += ["    build(world)",
              "    print('[{0}] running {1} entities, {2} channels (Ctrl+C to stop)'.format("
              f"{sysname!r}, len(world.entities), len(world.channels)), flush=True)",
              "    world.run(seconds=args.seconds, ticks=args.ticks)",
              "    for e in world.entities.values():",
              "        print(f'  {e.name}: {e.state()}', flush=True)", ""]
        return "\n".join(L)

    # ------------------------------------------------------------------ run
    def run(self, ctx: BuildContext) -> RunResult | None:
        main = ctx.outdir / "main.py"
        if not main.exists():
            return RunResult(False, "build first")
        return self.sh([sys.executable, "-u", "main.py", "--simulate", "--seconds", "3", "--no-hub"],
                       ctx.outdir, timeout=20)


MODULE = PythonModule()
