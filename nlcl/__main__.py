"""python -m nlcl [script.nl] [options]"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .runtime import HELP, Session


def main(argv=None) -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="nlcl", description="Natural Language Computational Language runtime")
    ap.add_argument("script", nargs="?", help="file with one natural-language instruction per line")
    ap.add_argument("-p", "--project", default="project", help="system/project name")
    ap.add_argument("-o", "--out", help="build directory (default ./build/<project>)")
    ap.add_argument("-b", "--backend", help="LLM backend: auto|off|ollama|groq|gemini|openrouter|cerebras|mistral|huggingface|github")
    ap.add_argument("-m", "--mode", default="hybrid", choices=["rules", "hybrid", "llm"])
    ap.add_argument("--vocab", default=str(Path.home() / ".nlcl" / "vocab.json"), help="adaptive vocabulary file")
    ap.add_argument("--trace", action="store_true", help="show tokens, composite tokens and IR for each line")
    ap.add_argument("--yes", action="store_true", help="auto-accept vocabulary proposals")
    ap.add_argument("--build", action="store_true", help="build after running the script")
    ap.add_argument("--run", action="store_true", help="build and run after the script")
    ap.add_argument("-c", "--command", action="append", help="instruction to execute (repeatable)")
    a = ap.parse_args(argv)

    s = Session(a.project, backend=a.backend, mode=a.mode, outdir=a.out, vocab_path=a.vocab,
                trace=a.trace, auto_yes=a.yes)

    def emit(lines):
        for ln in lines:
            print(ln, flush=True)

    lines = []
    if a.script:
        lines = Path(a.script).read_text(encoding="utf-8-sig").splitlines()
    if a.command:
        lines += a.command
    if lines:
        for ln in lines:
            if ln.strip() and not ln.strip().startswith("#"):
                print(f"> {ln.strip()}")
            try:
                emit(s.handle(ln))
            except EOFError:
                return 0
        if s.pending:
            print(f"(unanswered question: {s.pending.text})")
        if a.run:
            emit(s.run())
        elif a.build and s.dirty:           # a script ending in "build it" is not built twice
            emit(s.build())
        return 0

    print(f"NLCL {__version__} - natural language computational language. ':help' for help, ':quit' to exit.")
    print(f"backend: {s.backend or 'offline rules'}   domains: {', '.join(s.modules)}")
    while True:
        try:
            prompt = "? " if s.pending or s.llm_question else "> "
            line = input(prompt)
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        try:
            emit(s.handle(line))
        except EOFError:
            return 0
        except Exception as e:  # never crash the REPL
            print(f"! internal error: {type(e).__name__}: {e}")


if __name__ == "__main__":
    sys.exit(main())
