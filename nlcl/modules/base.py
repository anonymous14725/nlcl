"""Domain module interface.

A domain module turns the universal IR (entities, behaviours, channels) into
artefacts for one execution environment: a game engine, a micro-controller,
an HDL, a CAD tool ...  Modules never see natural language.

To add a new domain, drop a file in nlcl/modules/ that defines a subclass of
DomainModule and exposes an instance as ``MODULE``.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..ir import Entity, SystemState, ident, pascal, render_expr


@dataclass
class Channel:
    """A resolved cross-entity communication path (from a Link)."""
    id: str
    protocol: str            # serial | http | mqtt | gpio | spi | i2c | udp | websocket
    src: Entity
    dst: Entity
    fields: list[str]        # data fields carried src -> dst
    config: dict = field(default_factory=dict)   # baud, port, url, topic, broker, pins ...

    def direction(self, entity_or_domain: Any) -> str | None:
        """'out' if the given entity/domain is the sender, 'in' if receiver."""
        if isinstance(entity_or_domain, Entity):
            if entity_or_domain.id == self.src.id:
                return "out"
            if entity_or_domain.id == self.dst.id:
                return "in"
            return None
        if self.src.domain == entity_or_domain:
            return "out"
        if self.dst.domain == entity_or_domain:
            return "in"
        return None

    def peer(self, entity: Entity) -> Entity:
        return self.dst if entity.id == self.src.id else self.src

    @property
    def topic(self) -> str:
        return self.config.get("topic", f"nlcl/{self.id}")

    @property
    def hub_url(self) -> str:
        return self.config.get("hub_url", "http://localhost:8765")

    def to_dict(self) -> dict:
        return {"id": self.id, "protocol": self.protocol, "src": self.src.name,
                "src_domain": self.src.domain, "dst": self.dst.name,
                "dst_domain": self.dst.domain, "fields": self.fields, "config": self.config}


@dataclass
class BuildContext:
    system: SystemState
    domain: str
    entities: list[Entity]          # entities of this domain (creation order)
    channels: list[Channel]         # every channel touching this domain
    outdir: Path                    # where this module writes (already created)
    project: str = "project"
    llm: Any = None                 # optional LLM backend (may be None)

    def channels_for(self, entity: Entity) -> list[Channel]:
        return [c for c in self.channels if entity.id in (c.src.id, c.dst.id)]

    def children(self, entity: Entity) -> list[Entity]:
        return [e for e in self.entities if e.parent == entity.id]

    def roots(self) -> list[Entity]:
        ids = {e.id for e in self.entities}
        return [e for e in self.entities if e.parent not in ids]


@dataclass
class RunResult:
    ok: bool
    output: str
    command: str = ""


class DomainModule:
    # identity
    name: str = "base"                 # canonical domain id, e.g. "game"
    title: str = "Base"
    aliases: tuple[str, ...] = ()      # other words that name this domain directly
    keywords: tuple[str, ...] = ()     # words that *suggest* this domain (kinds, tech names)
    # kind -> default props. "object" should always be present.
    kinds: dict[str, dict] = {"object": {}}
    # property names the module understands well (used for "What properties?" prompts)
    suggested_props: tuple[str, ...] = ()
    # protocols this domain can speak natively
    protocols: tuple[str, ...] = ("http",)
    # free toolchain advice shown in SYSTEM.md
    toolchain: str = ""

    # ---------------------------------------------------------------- props
    def default_props(self, kind: str) -> dict:
        return dict(self.kinds.get(kind, self.kinds.get("object", {})))

    def validate(self, entity: Entity) -> list[str]:
        """Return human-readable warnings (never raise)."""
        return []

    def prompt_for(self, entity: Entity) -> str | None:
        """Optional follow-up question after an entity is created."""
        if self.suggested_props:
            return (f"What properties should {entity.name} have? "
                    f"(e.g. {', '.join(self.suggested_props[:5])})")
        return None

    # ---------------------------------------------------------------- build
    def generate(self, ctx: BuildContext) -> dict[str, str]:
        """Return {relative_path: file_content}."""
        raise NotImplementedError

    def run(self, ctx: BuildContext) -> RunResult | None:
        """Execute/simulate generated artefacts if a free local tool exists."""
        return None

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def expr(e: Any, lang: str, name=lambda n: n) -> str:
        return render_expr(e, lang, name)

    @staticmethod
    def ident(s: str) -> str:
        return ident(s)

    @staticmethod
    def pascal(s: str) -> str:
        return pascal(s)

    @staticmethod
    def which(*names: str) -> str | None:
        for n in names:
            p = shutil.which(n)
            if p:
                return p
        return None

    @staticmethod
    def sh(cmd: list[str], cwd: Path, timeout: int = 30) -> RunResult:
        try:
            p = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                               timeout=timeout, encoding="utf-8", errors="replace")
            return RunResult(p.returncode == 0, (p.stdout + p.stderr).strip(), " ".join(cmd))
        except subprocess.TimeoutExpired as e:
            out = (e.stdout or "") if isinstance(e.stdout, str) else ""
            return RunResult(True, out + f"\n[stopped after {timeout}s]", " ".join(cmd))
        except OSError as e:
            return RunResult(False, str(e), " ".join(cmd))

    @staticmethod
    def literal(v: Any, lang: str) -> str:
        """Render a property value as a literal in the target language."""
        if isinstance(v, bool):
            return {"python": "True" if v else "False",
                    "verilog": "1'b1" if v else "1'b0"}.get(lang, "true" if v else "false")
        if isinstance(v, (int, float)):
            if lang in ("csharp",) and isinstance(v, float):
                return f"{v}f"
            return repr(v)
        if isinstance(v, (list, tuple)):
            inner = ", ".join(DomainModule.literal(x, lang) for x in v)
            return {"python": f"[{inner}]", "js": f"[{inner}]", "gdscript": f"[{inner}]",
                    "openscad": f"[{inner}]"}.get(lang, "{" + inner + "}")
        s = str(v).replace("\\", "\\\\").replace('"', '\\"')
        return f'"{s}"'

    @staticmethod
    def ctype(v: Any, lang: str) -> str:
        """Best-effort static type for a value."""
        if isinstance(v, bool):
            return {"csharp": "bool", "cpp": "bool", "c": "bool", "gdscript": "bool"}.get(lang, "bool")
        if isinstance(v, int):
            return {"csharp": "int", "cpp": "int", "c": "int", "gdscript": "int"}.get(lang, "int")
        if isinstance(v, float):
            return {"csharp": "float", "cpp": "float", "c": "float", "gdscript": "float"}.get(lang, "float")
        if isinstance(v, (list, tuple)):
            return {"csharp": "float[]", "cpp": "std::vector<float>", "gdscript": "Array"}.get(lang, "float*")
        return {"csharp": "string", "cpp": "std::string", "c": "const char*", "gdscript": "String"}.get(lang, "String")
