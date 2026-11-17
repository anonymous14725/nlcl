"""Multi-module system composer.

Groups entities by domain, resolves every Link into a Channel with a concrete
protocol + configuration that BOTH sides agree on, then lets each domain
module generate its part.  Writes system.json (IR), SYSTEM.md (architecture,
wiring and how to run everything with free tools).
"""
from __future__ import annotations

import json
from pathlib import Path

from . import modules as modreg
from .ir import SystemState, ident
from .modules.base import BuildContext, Channel, RunResult

# default protocol by (domain, domain) pair (order-insensitive)
DEFAULT_PROTOCOL = {
    frozenset({"embedded", "python"}): "serial",
    frozenset({"embedded"}): "serial",
    frozenset({"embedded", "hardware"}): "spi",
    frozenset({"hardware"}): "wire",
    frozenset({"hardware", "python"}): "serial",
    frozenset({"python", "3d"}): "http",
    frozenset({"python", "game"}): "http",
    frozenset({"python"}): "http",
    frozenset({"python", "cpp"}): "http",
    frozenset({"cpp", "embedded"}): "serial",
    frozenset({"game", "3d"}): "http",
    frozenset({"embedded", "3d"}): "http",
    frozenset({"embedded", "game"}): "http",
}
NON_DATA_PROPS = {"pin", "pins", "position", "rotation", "scale", "size", "color", "subtype", "baud", "port",
                  "address", "width", "height", "depth", "radius", "length", "thickness", "board", "mass",
                  "model", "wifi_ssid", "wifi_password", "interval_ms", "clock_hz", "on_pin", "movable",
                  "collision", "gravity", "visible", "shape", "material", "texture", "units", "divide_by",
                  "stable_ms", "frequency_hz", "active_low", "for_entity", "states", "wall", "lid", "vents"}


def data_fields(state: SystemState, src) -> list[str]:
    """Default fields of a link: real data props of the source (or of its child sensors when the
    source is a board), never config, flags named after other things, or flattened references."""
    names = {"esp32", "esp8266", "arduino", "uno", "nano", "mega", "pico", "pico_w", "stm32", "raspberry_pi",
             "arduino_uno", "arduino_nano", "arduino_mega"}
    for e in state.entities.values():
        if e.id != src.id:
            names |= {e.ident, ident(e.kind)}

    def ok(k, v):
        if k in NON_DATA_PROPS or k.startswith("_") or not isinstance(v, (int, float, bool)):
            return False
        if k in names or any(k.startswith(n + "_") for n in names if n):
            return False                   # 'esp32': True, 'alu1_result', 'bob_lives'
        return True
    fields = [k for k, v in src.props.items() if ok(k, v)]
    if not fields:
        for c in state.children(src.id):
            fields += [k for k, v in c.props.items() if ok(k, v) and k not in fields]
    return fields


def resolve_channels(state: SystemState) -> list[Channel]:
    chans = []
    for l in state.links.values():
        src, dst = state.entities.get(l.src), state.entities.get(l.dst)
        if not src or not dst:
            continue
        proto = (l.via or DEFAULT_PROTOCOL.get(frozenset({src.domain, dst.domain})) or "http").lower()
        if proto == "uart":
            proto = "serial"
        requested = None
        if proto == "websocket":
            # every module implements this as HTTP polling on the hub: say so in the contract
            proto, requested = "http", "websocket"
        fields = list(l.props.get("fields") or [])
        if not fields:
            fields = data_fields(state, src)
        if not fields:
            fields = ["value"]
        cfg = {"interval_ms": int(l.props.get("interval_ms", 1000))}
        if requested:
            cfg["requested_protocol"] = requested
        # field types + enum tables so both sides (and simulators) agree on the encoding
        types, enums = {}, {}
        for f in fields:
            holder = src if f in src.props else next((c for c in state.children(src.id) if f in c.props), None)
            v = holder.props.get(f) if holder else None
            types[f] = ("bool" if isinstance(v, bool) else "int" if isinstance(v, int) else
                        "string" if isinstance(v, str) else "float")
            if holder and f == "state" and isinstance(holder.props.get("states"), list):
                enums[f] = [str(s) for s in holder.props["states"]]
                types[f] = "enum"
        cfg["types"] = types
        if enums:
            cfg["enums"] = enums
        if proto == "serial":
            cfg["baud"] = int(l.props.get("baud", 115200))
            cfg["port"] = l.props.get("port_name", "auto")
        elif proto in ("http", "websocket"):
            cfg["hub_url"] = f"http://localhost:{int(l.props.get('port', 8765))}"
            cfg["path"] = f"/channel/{l.id}"
        elif proto == "mqtt":
            cfg["broker"] = l.props.get("broker", "broker.hivemq.com")
            cfg["broker_port"] = int(l.props.get("broker_port", 1883))
            cfg["topic"] = l.props.get("topic", f"nlcl/{ident(state.name)}/{l.id}")
        elif proto == "udp":
            cfg["port"] = int(l.props.get("port", 9876))
        elif proto in ("spi", "i2c", "gpio", "wire"):
            cfg["bus"] = proto
            if proto == "i2c":
                cfg["address"] = l.props.get("address", "0x42")
        chans.append(Channel(l.id, proto, src, dst, fields, cfg))
    return chans


def needs_hub(chans: list[Channel]) -> bool:
    return any(c.protocol in ("http", "websocket") for c in chans) or \
        any(c.protocol == "serial" and "python" not in (c.src.domain, c.dst.domain) for c in chans)


class Composer:
    def __init__(self, state: SystemState, outdir: Path, llm=None):
        self.state = state
        self.outdir = Path(outdir)
        self.llm = llm
        self.modules = modreg.load_modules()

    def contexts(self) -> list[BuildContext]:
        chans = resolve_channels(self.state)
        ctxs = []
        for dom in self.state.domains():
            ents = self.state.in_domain(dom)
            mine = [c for c in chans if dom in (c.src.domain, c.dst.domain)]
            ctxs.append(BuildContext(self.state, dom, ents, mine, self.outdir / dom,
                                     project=ident(self.state.name) or "project", llm=self.llm))
        # cross-domain http links need the python hub even if there are no python entities
        if needs_hub(chans) and "python" not in self.state.domains():
            ctxs.append(BuildContext(self.state, "python", [], [c for c in chans if c.protocol in ("http", "websocket", "serial")],
                                     self.outdir / "python", project=ident(self.state.name) or "project", llm=self.llm))
        return ctxs

    def build(self) -> tuple[dict[str, list[str]], list[str]]:
        """Generate everything. Returns ({domain: [files]}, warnings)."""
        written: dict[str, list[str]] = {}
        warnings: list[str] = []
        self.outdir.mkdir(parents=True, exist_ok=True)
        ctxs = self.contexts()
        for ctx in ctxs:
            mod = modreg.get(ctx.domain)
            if not mod:
                warnings.append(f"no module for domain '{ctx.domain}'")
                continue
            ctx.outdir.mkdir(parents=True, exist_ok=True)
            try:
                files = mod.generate(ctx)
            except Exception as e:  # report, keep building other domains
                import traceback
                warnings.append(f"{ctx.domain}: generator failed: {e}\n{traceback.format_exc(limit=3)}")
                continue
            for rel, content in files.items():
                p = ctx.outdir / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content, encoding="utf-8", newline="\n")
                written.setdefault(ctx.domain, []).append(str(p.relative_to(self.outdir)))
            for e in ctx.entities:
                warnings += [f"{e.name}: {w}" for w in mod.validate(e)]
        self._cleanup(written, warnings)
        warnings += self._reapply_patches()
        chans = resolve_channels(self.state)
        (self.outdir / "system.json").write_text(json.dumps({
            "ir": self.state.to_dict(),
            "channels": [c.to_dict() for c in chans],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        (self.outdir / "SYSTEM.md").write_text(self.report(chans, written), encoding="utf-8")
        return written, warnings

    def _reapply_patches(self) -> list[str]:
        """Bug fixes made to generated files (state.patches) survive a rebuild when they still apply."""
        notes, kept = [], 0
        for pt in getattr(self.state, "patches", []):
            p = (self.outdir / pt["file"]).resolve()
            if self.outdir.resolve() not in p.parents or not p.is_file():
                continue
            txt = p.read_text(encoding="utf-8", errors="replace")
            if pt["replace"] in txt and pt["search"] not in txt:
                kept += 1
            elif pt["search"] in txt:
                p.write_text(txt.replace(pt["search"], pt["replace"], 1), encoding="utf-8", newline="")
                kept += 1
            else:
                notes.append(f"an earlier fix in {pt['file']} no longer applies to the regenerated code")
        if kept:
            notes.append(f"re-applied {kept} earlier fix(es) to the generated code")
        return notes

    MANIFEST = ".nlcl_manifest.json"

    def _cleanup(self, written: dict[str, list[str]], warnings: list[str]) -> None:
        """Delete files that a previous build generated but this build no longer does.
        Only files listed in the previous manifest are touched (never user files)."""
        mpath = self.outdir / self.MANIFEST
        now = sorted(f.replace("\\", "/") for fs in written.values() for f in fs)
        try:
            before = json.loads(mpath.read_text(encoding="utf-8")).get("files", [])
        except (OSError, ValueError):
            before = []
        removed = 0
        for f in set(before) - set(now):
            p = (self.outdir / f).resolve()
            if self.outdir.resolve() in p.parents and p.is_file():
                p.unlink()
                removed += 1
                try:
                    p.parent.rmdir()            # only if now empty
                except OSError:
                    pass
        if removed:
            warnings.append(f"removed {removed} stale file(s) from the previous build")
        mpath.write_text(json.dumps({"files": now}, indent=1), encoding="utf-8")

    def run(self, domain: str | None = None) -> list[tuple[str, RunResult]]:
        results = []
        for ctx in self.contexts():
            if domain and ctx.domain != domain:
                continue
            mod = modreg.get(ctx.domain)
            if not mod:
                continue
            r = mod.run(ctx)
            if r is not None:
                results.append((ctx.domain, r))
        return results

    # -------------------------------------------------------------- report
    def report(self, chans: list[Channel], written: dict[str, list[str]]) -> str:
        st = self.state
        L = [f"# System: {st.name}", "",
             "Generated by NLCL (Natural Language Computational Language).", "",
             "## Architecture", "", "```mermaid", "flowchart LR"]
        for dom in st.domains():
            L.append(f"  subgraph {ident(dom)}[{dom}]")
            for e in st.in_domain(dom):
                L.append(f"    {e.ident}[\"{e.name}<br/><i>{e.kind}</i>\"]")
            L.append("  end")
        for e in st.entities.values():
            if e.parent in st.entities:
                L.append(f"  {st.entities[e.parent].ident} -.contains.- {e.ident}")
        for c in chans:
            L.append(f"  {c.src.ident} -->|{c.protocol}: {', '.join(c.fields)}| {c.dst.ident}")
        L += ["```", "", "## Entities", ""]
        L.append("```")
        L.append(st.summary())
        L.append("```")
        if chans:
            L += ["", "## Channels (cross-module contracts)", "",
                  "| id | protocol | from | to | fields | config |", "|---|---|---|---|---|---|"]
            for c in chans:
                L.append(f"| {c.id} | {c.protocol} | {c.src.name} ({c.src.domain}) | {c.dst.name} ({c.dst.domain}) "
                         f"| {', '.join(c.fields)} | {json.dumps(c.config)} |")
            L += ["", "Every message is a flat JSON object: `{\"channel\": id, \"from\": src, <field>: value, ...}` "
                  "terminated by a newline on stream transports (serial/udp)."]
        L += ["", "## Generated files and free toolchains", ""]
        for dom, files in written.items():
            mod = modreg.get(dom)
            L.append(f"### {dom} — {mod.title if mod else ''}")
            for f in files:
                L.append(f"- `{f}`")
            if mod and mod.toolchain:
                L += ["", mod.toolchain.strip(), ""]
        return "\n".join(L) + "\n"
