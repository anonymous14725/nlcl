"""Domain module registry (auto-discovers nlcl/modules/*.py exposing MODULE)."""
from __future__ import annotations

import importlib
import pkgutil
import sys

from .base import BuildContext, Channel, DomainModule, RunResult

_REGISTRY: dict[str, DomainModule] = {}


def load_modules() -> dict[str, DomainModule]:
    if _REGISTRY:
        return _REGISTRY
    for info in pkgutil.iter_modules(__path__):
        if info.name.startswith("_") or info.name == "base":
            continue
        try:
            mod = importlib.import_module(f"{__name__}.{info.name}")
        except Exception as e:  # a broken plugin must not kill the runtime
            print(f"[nlcl] warning: module {info.name} failed to load: {e}", file=sys.stderr)
            continue
        m = getattr(mod, "MODULE", None)
        if isinstance(m, DomainModule):
            _REGISTRY[m.name] = m
    return _REGISTRY


def get(name: str | None) -> DomainModule | None:
    if not name:
        return None
    reg = load_modules()
    n = name.lower()
    if n in reg:
        return reg[n]
    for m in reg.values():
        if n in m.aliases:
            return m
    return None


__all__ = ["BuildContext", "Channel", "DomainModule", "RunResult", "load_modules", "get"]
