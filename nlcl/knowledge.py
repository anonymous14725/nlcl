"""The semantic knowledge base: a Prolog database the interpreter reasons with and learns into.

core.pl            rules (domain inference, reliability, terminal meanings and safety, languages)
start-up facts     the core lexicon and the module ontology, asserted from Python
learned.pl         what the interpreter has learned (rewrites, evidence counts, weighting parameters,
                   discovered modules) - plain Prolog facts, human-readable, loadable by SWI-Prolog
"""
from __future__ import annotations

import time
from pathlib import Path

from .prolog import Prolog, PrologError, fmt, from_python, mk_list, Atom, Struct

CORE = Path(__file__).parent / "kb" / "core.pl"
PRETRAINED = Path(__file__).parent / "kb" / "pretrained.pl"   # learned from the corpus' TRAINING split only
LEARNED_PREDICATES = (("rewrite", 3), ("reliability", 3), ("param", 2), ("learned_module", 3))


def _atom(s) -> str:
    return fmt(Atom(str(s)))


class Knowledge:
    def __init__(self, home: Path | str | None = None, modules: dict | None = None, atomic: dict | None = None,
                 pretrained: bool = False):
        self.home = Path(home) if home else None
        self.pl = Prolog()
        self.pl.consult(CORE)
        if atomic is None:
            from .tokens import ATOMIC
            atomic = ATOMIC
        for tok, words in atomic.items():
            for w in words:
                self.pl.add_clause(Struct("atomic", (Atom(w), Atom(tok))))
        self.load_modules(modules or {})
        if pretrained and PRETRAINED.exists() and not (self.learned_path and self.learned_path.exists()):
            self.pl.consult(PRETRAINED)          # a fresh install starts from what was learned in training
        if self.learned_path and self.learned_path.exists():
            try:
                self.pl.consult(self.learned_path)
            except PrologError as e:  # a damaged learned file must not stop the system
                print(f"[nlcl] warning: could not load {self.learned_path}: {e}")

    @property
    def learned_path(self) -> Path | None:
        return self.home / "learned.pl" if self.home else None

    def load_modules(self, modules: dict) -> None:
        self.pl.retract_all("kind_of", 3)
        self.pl.retract_all("alias", 2)
        for m in modules.values():
            for k in m.kinds:
                self.pl.add_clause(Struct("kind_of", (Atom(k.lower()), Atom(m.name), 2)))
            for k in m.keywords:
                self.pl.add_clause(Struct("kind_of", (Atom(k.lower()), Atom(m.name), 1)))
            for a in (m.name, *m.aliases):
                self.pl.add_clause(Struct("alias", (Atom(a.lower()), Atom(m.name))))

    # ------------------------------------------------------------------ queries
    def query(self, text: str, limit: int = 50) -> list[dict]:
        return self.pl.query(text, limit=limit)

    def domain_candidates(self, kind: str) -> list[str]:
        r = self.pl.query(f"domain_candidates({_atom(kind.lower())}, Ds)", limit=1)
        return r[0]["Ds"] if r else []

    def reliability(self, key: str) -> float:
        r = self.pl.query(f"reliable({key}, P)", limit=1)
        return float(r[0]["P"]) if r else 0.75

    def shell_intent(self, intent: str) -> tuple[str, str] | None:
        r = self.pl.query(f"shell_intent({_atom(intent)}, S, D)", limit=1)
        return (r[0]["S"], r[0]["D"]) if r else None

    def needs_confirmation(self, intent: str) -> bool:
        return self.pl.ask(f"needs_confirmation({_atom(intent)})")

    def forbidden(self, intent: str, path: str) -> bool:
        p = str(path).replace("\\", "/").rstrip("/") or "/"
        return self.pl.ask(f"forbidden({_atom(intent)}, {_atom(p)})") or \
            self.pl.ask(f"forbidden({_atom(intent)}, {_atom(p + '/')})")

    def file_type(self, word: str) -> list[str] | None:
        w = word.lower().rstrip("s") if word.lower() not in ("js", "ts", "css") else word.lower()
        for cand in (word.lower(), w):
            r = self.pl.query(f"file_type({_atom(cand)}, P)", limit=1)
            if r:
                return list(r[0]["P"])
        return None

    def language(self, name: str) -> tuple[str, str] | None:
        r = self.pl.query(f"language({_atom(name.lower())}, E, H)", limit=1)
        return (r[0]["E"], r[0]["H"]) if r else None

    def languages(self) -> list[str]:
        return [r["N"] for r in self.pl.query("language(N, _, _)", limit=200)]

    def learned_modules(self) -> list[tuple[str, str, str]]:
        return [(r["N"], r["L"], r["E"]) for r in self.pl.query("learned_module(N, L, E)", limit=200)]

    # ------------------------------------------------------------------ learned knowledge
    def rewrites(self) -> list[tuple[list[str], list[str], int]]:
        out = []
        for r in self.pl.query("rewrite(F, T, S)", limit=10000):
            out.append((list(r["F"]), list(r["T"]), int(r["S"])))
        # longest phrases first so "get rid of" wins over "get"
        return sorted(out, key=lambda x: -len(x[0]))

    def add_rewrite(self, frm: list[str], to: list[str], support: int = 1) -> None:
        f, t = mk_list([Atom(w) for w in frm]), mk_list([Atom(w) for w in to])
        self.pl.retract_all("rewrite", 3, lambda c: fmt(c.args[0]) == fmt(f))
        self.pl.add_clause(Struct("rewrite", (f, t, int(support))))

    def remove_rewrite(self, frm: list[str]) -> bool:
        f = fmt(mk_list([Atom(w) for w in frm]))
        before = len(self.pl.db.get(("rewrite", 3), []))
        self.pl.retract_all("rewrite", 3, lambda c: fmt(c.args[0]) == f)
        return len(self.pl.db.get(("rewrite", 3), [])) < before

    def record(self, key: str, ok: bool, n: int = 1) -> None:
        """Add evidence for a token / frame / intent / rewrite key (term text, e.g. frame(create_object))."""
        from .prolog import parse_term
        k = parse_term(key)[0]
        lst = self.pl.db.setdefault(("reliability", 3), [])
        for i, c in enumerate(lst):
            if fmt(c.args[0]) == fmt(k):
                s, f = c.args[1], c.args[2]
                lst[i] = Struct("reliability", (k, s + (n if ok else 0), f + (0 if ok else n)))
                return
        lst.append(Struct("reliability", (k, n if ok else 0, 0 if ok else n)))

    def params(self) -> dict[str, float]:
        return {r["N"]: float(r["V"]) for r in self.pl.query("param(N, V)", limit=1000)}

    def set_params(self, params: dict[str, float]) -> None:
        self.pl.retract_all("param", 2)
        for k, v in params.items():
            self.pl.add_clause(Struct("param", (Atom(k), float(round(v, 6)))))

    def add_learned_module(self, name: str, language: str, ext: str) -> None:
        self.pl.retract_all("learned_module", 3, lambda c: fmt(c.args[0]) == _atom(name))
        self.pl.add_clause(Struct("learned_module", (Atom(name), Atom(language), Atom(ext))))

    def reset_learned(self) -> None:
        for name, ar in LEARNED_PREDICATES:
            self.pl.retract_all(name, ar)

    # ------------------------------------------------------------------ persistence
    def learned_text(self) -> str:
        lines = [f"% NLCL learned knowledge - written {time.strftime('%Y-%m-%d %H:%M:%S')}",
                 "% (plain Prolog facts: edit, delete or load them in SWI-Prolog)", ""]
        for name, ar in LEARNED_PREDICATES:
            clauses = self.pl.db.get((name, ar), [])
            if clauses:
                lines.append(f"% {name}/{ar}")
                lines += sorted(fmt(c) + "." for c in clauses)
                lines.append("")
        return "\n".join(lines) + "\n"

    def save(self) -> Path | None:
        if not self.learned_path:
            return None
        self.learned_path.parent.mkdir(parents=True, exist_ok=True)
        self.learned_path.write_text(self.learned_text(), encoding="utf-8")
        return self.learned_path

    def export(self, path: Path | str) -> Path:
        """Everything (core rules + start-up facts + learned facts) as one SWI-Prolog file."""
        path = Path(path)
        facts = []
        for (name, ar), clauses in sorted(self.pl.db.items()):
            if name in ("atomic", "kind_of", "alias"):
                facts += [fmt(c) + "." for c in clauses]
        path.write_text(CORE.read_text(encoding="utf-8") + "\n% ---- facts from the lexicon and the modules\n"
                        + "\n".join(facts) + "\n\n" + self.learned_text(), encoding="utf-8")
        return path

    def stats(self) -> dict:
        return {"rewrites": len(self.pl.db.get(("rewrite", 3), [])),
                "evidence": len(self.pl.db.get(("reliability", 3), [])),
                "params": len(self.pl.db.get(("param", 2), [])),
                "learned_modules": len(self.pl.db.get(("learned_module", 3), [])),
                "lexicon": len(self.pl.db.get(("atomic", 2), [])),
                "ontology": len(self.pl.db.get(("kind_of", 3), []))}

    def to_term(self, v) -> str:
        return fmt(from_python(v))
