"""Self-structuring: what the interpreter does when a word or a whole module does not exist yet.

1. Unknown verb / word  -> close forms in the KB vocabulary (typos, inflections) are tried; a candidate
                           is accepted only if it makes the sentence interpretable with a high weight.
2. Online teacher       -> if an LLM backend exists, its interpretation of the sentence is used as gold
                           and *distilled* into a rewrite rule in the Prolog KB, so the next time the
                           same kind of sentence is understood offline, deterministically, for free.
3. Unknown module       -> a language / platform the KB knows (language/3) but no module implements is
                           grown into a new domain module at run time, registered, and remembered
                           (learned_module/3). Its code is written by the online agent and checked by the
                           bug-fix diagnostics; offline it produces a precise spec instead.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from . import modules as modreg
from .ir import Entity
from .modules.base import BuildContext, DomainModule, RunResult

VERB_WORDS_CACHE: list[str] | None = None


def _dl(a: str, b: str) -> int:
    """Damerau-Levenshtein distance (optimal string alignment)."""
    d = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        d[i][0] = i
    for j in range(len(b) + 1):
        d[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            c = 0 if a[i - 1] == b[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + c)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[-1][-1]


def _vocabulary(kb) -> list[str]:
    words = {r["W"] for r in kb.query("atomic(W, _)", limit=5000)}
    words |= {r["K"] for r in kb.query("kind_of(K, _, _)", limit=5000)}
    return sorted(w for w in words if isinstance(w, str) and len(w) >= 3)


def word_candidates(word: str, kb) -> list[str]:
    """Known words this unknown word may stand for: inflections first, then typos."""
    w = word.lower()
    vocab = _vocabulary(kb)
    out = []
    for suf, rep in (("ing", ""), ("ing", "e"), ("ed", ""), ("ed", "e"), ("es", ""), ("s", ""), ("ies", "y")):
        if w.endswith(suf) and len(w) > len(suf) + 2:
            base = w[: -len(suf)] + rep
            if base in vocab and base not in out:
                out.append(base)
    if len(w) >= 4:
        limit = 1 if len(w) < 7 else 2
        near = sorted((_dl(w, v), v) for v in vocab if abs(len(v) - len(w)) <= limit and v != w)
        out += [v for dist, v in near if dist <= limit and v not in out][:3]
    return out


def repair_sentence(session, text: str, min_weight: float | None = None) -> tuple[str, str, str, float] | None:
    """Try replacing one unknown word by a close known word. -> (new text, old, new, weight) or None."""
    min_weight = session.execute_at if min_weight is None else min_weight
    base = session.interpret(text)[0].score
    known = set(_vocabulary(session.kb))
    best = None
    for m in re.finditer(r"[A-Za-z][A-Za-z\-]{2,}", text):
        w = m.group(0)
        if w.lower() in known or session.vocab.atomic(w.lower()):
            continue
        for cand in word_candidates(w, session.kb):
            new = text[:m.start()] + cand + text[m.end():]
            sc = session.interpret(new)[0].score
            if sc >= min_weight and sc > base + 0.15 and (best is None or sc > best[3]):
                best = (new, w.lower(), cand, sc)
    return best


def distill(session, text: str, gold_ops: list, max_trials: int = 2500) -> tuple[list[str], list[str]] | None:
    """Find a rewrite rule (phrase -> known word or nothing) that makes the offline interpreter produce
    the same state as the teacher's interpretation. Stores nothing; the caller decides."""
    from .learn import signature
    from .interpreter import Interpreter
    clone = session.state.clone()
    Interpreter(clone).apply_all(list(gold_ops))
    shell = [op.props for op in gold_ops if op.action == "SHELL"]
    gold_sig = signature(clone, shell)
    words = re.findall(r"[A-Za-z][\w'\-]*", text.lower())
    verbs = sorted({r["W"] for r in session.kb.query("atomic(W, T)", limit=5000)
                    if r["T"] in ("CREATE", "DELETE", "SET", "CONNECT", "MOVE", "RENAME", "SHOW")})
    targets = [""] + verbs
    grams = []
    for n in (1, 2, 3):
        for i in range(len(words) - n + 1):
            grams.append(words[i:i + n])
    base = list(session.rules.rewrites)
    tried = 0
    for g in grams:
        if all(session.vocab.atomic(w) for w in g):
            continue
        for t in targets:
            tried += 1
            if tried > max_trials:
                return None
            session.rules.rewrites = sorted(base + [(g, [t] if t else [], 1)], key=lambda r: -len(r[0]))
            try:
                c = session.interpret(text)[0]
                if c.ops:
                    st = session.state.clone()
                    sh = []
                    if c.source == "shell":
                        sh = [c.ops[0].props]
                    else:
                        Interpreter(st).apply_all(c.ops)
                    if signature(st, sh) == gold_sig:
                        return g, ([t] if t else [])
            finally:
                session.rules.rewrites = base
    return None


# ======================================================================= module discovery
GENERIC_KINDS = {"object": {}, "program": {}, "app": {}, "service": {}, "library": {}, "script": {}, "cli": {},
                 "tool": {}, "class": {}, "function": {}, "module": {}, "server": {"port": 8080}, "game": {},
                 "calculator": {}, "counter": {"count": 0}}


class GeneratedModule(DomainModule):
    """A domain the system grew by itself for a language it has no hand-written module for."""

    def __init__(self, language: str, ext: str, run_hint: str, home: Path | None = None):
        self.name = language
        self.language = language
        self.ext = ext
        self.run_hint = run_hint
        self.home = home
        self.title = f"{language} (discovered module: code written by the online agent, checked by diagnostics)"
        self.aliases = (language,)
        self.keywords = (language,)
        self.kinds = dict(GENERIC_KINDS)
        self.suggested_props = ("purpose", "inputs", "outputs")
        self.protocols = ("http",)
        self.toolchain = f"Run with: `{run_hint}`"

    def spec(self, ctx: BuildContext) -> dict:
        return {"language": self.language, "project": ctx.project,
                "entities": [{"name": e.name, "kind": e.kind, "props": e.props, "behaviors": e.behaviors,
                              "parent": next((x.name for x in ctx.entities if x.id == e.parent), None)}
                             for e in ctx.entities],
                "channels": [c.to_dict() for c in ctx.channels]}

    def generate(self, ctx: BuildContext) -> dict[str, str]:
        spec = self.spec(ctx)
        blob = json.dumps(spec, sort_keys=True, ensure_ascii=False)
        key = hashlib.sha1(blob.encode()).hexdigest()[:16]
        files = {"spec.json": json.dumps(spec, indent=2, ensure_ascii=False)}
        cache = (self.home / "modules" / self.name / f"{key}.json") if self.home else None
        if cache and cache.exists():                     # deterministic: same IR -> same code
            files.update(json.loads(cache.read_text(encoding="utf-8")))
            return files
        llm = ctx.llm
        if llm is None or not hasattr(llm, "backend"):
            files["README.md"] = (f"# {ctx.project} ({self.language})\n\nNLCL discovered the `{self.language}` module, "
                                  "but writing code in it needs the online agent (set a free API key, see "
                                  "`:backend status`) and then `build it` again.\n\nThe exact specification "
                                  "the agent will implement is in `spec.json`.\n")
            return files
        from .llm import extract_json
        prompt = [{"role": "system", "content":
                   f"You write complete, runnable {self.language} programs from a JSON specification produced by "
                   "a natural-language programming system. Every entity is a part of the program; its props are "
                   "its data; its behaviors are event rules (on: start|update|timer:<ms>|input:<key>|change:<prop>"
                   "|message:<src>|collision, if: condition, do: actions). Reply with ONLY JSON: "
                   '{"files": {"<relative path>": "<full file content>"}}. Include a README.md with the run '
                   f"command ({self.run_hint}). No placeholders, no TODOs."},
                  {"role": "user", "content": blob}]
        try:
            data = extract_json(llm.backend.chat(prompt))
            gen = {str(k): str(v) for k, v in (data.get("files") or {}).items()}
        except Exception as e:
            files["README.md"] = f"# {ctx.project}\n\nThe online agent failed to write the code: {e}\n"
            return files
        files.update(gen)
        if cache:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(gen, ensure_ascii=False), encoding="utf-8")
        return files

    def run(self, ctx: BuildContext) -> RunResult | None:
        from .fixer import diagnose
        diags = diagnose(ctx.outdir)
        return RunResult(not diags, "diagnostics: " + ("no problems" if not diags else "; ".join(map(str, diags[:5]))),
                         "nlcl check")


def register(kb, home: Path | None, name: str) -> DomainModule | None:
    """Grow a module for a language listed in the KB and remember it."""
    lang = kb.language(name)
    if not lang:
        return None
    reg = modreg.load_modules()
    if name in reg:
        return reg[name]
    ext, hint = lang
    m = GeneratedModule(name, ext, hint, home)
    reg[name] = m
    kb.add_learned_module(name, name, ext)
    kb.load_modules(reg)
    kb.save()
    return m


def restore(kb, home: Path | None) -> list[str]:
    """Re-register modules discovered in earlier sessions."""
    out = []
    reg = modreg.load_modules()
    for name, lang, ext in kb.learned_modules():
        if name not in reg:
            hint = (kb.language(lang) or (ext, ""))[1]
            reg[name] = GeneratedModule(name, ext, hint, home)
            out.append(name)
    if out:
        kb.load_modules(reg)
    return out


PROGRAM_NOUNS = r"(?:program|app|application|service|script|cli|tool|library|code|module|project|server|class|function|game|calculator|counter|object)"


def languages_in(text: str, kb) -> list[str]:
    """Languages the sentence clearly asks for ("a rust program", "in kotlin", "use dart") with no module yet."""
    known = set(kb.languages())
    reg = modreg.load_modules()
    low = text.lower()
    out = []
    for lang in sorted(known, key=len, reverse=True):
        if lang in reg or modreg.get(lang):
            continue
        l = re.escape(lang)
        if re.search(rf"\b(?:in|use|using|with|written\s+in)\s+{l}\b", low) or \
                re.search(rf"\b{l}\s+{PROGRAM_NOUNS}s?\b", low):
            out.append(lang)
    return out
