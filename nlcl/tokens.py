"""Semantic tokenisation: atomic tokens, composite tokens, adaptive vocabulary.

  A. Atomic semantic tokens   word -> CREATE, OBJECT, CONNECT, ...
  B. Composite semantic tokens CREATE + OBJECT -> CREATE_OBJECT (rule table,
     extendable at runtime)
  C. Adaptive vocabulary       unknown words can be learned (by the user or
     proposed by an LLM) and new macros defined from natural language; every
     extension is validated before it is accepted.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------- A. atomic
ATOMIC: dict[str, list[str]] = {
    "CREATE": ["create", "make", "build", "add", "new", "spawn", "generate", "construct", "instantiate", "place", "insert"],
    "DELETE": ["delete", "remove", "destroy", "drop", "erase", "kill"],
    "CONNECT": ["connect", "link", "wire", "attach", "bind", "hook"],
    "DISCONNECT": ["disconnect", "unlink", "detach", "unwire"],
    "SET": ["set", "give", "change", "assign", "update", "configure", "enable", "disable"],
    "MOVE": ["move", "position", "jump"],
    "RENAME": ["rename", "call", "name"],
    "SHOW": ["show", "list", "display", "describe", "inspect", "state"],
    "BUILD": ["compile", "export", "emit", "codegen"],
    "RUN": ["run", "execute", "simulate", "evaluate", "test"],
    "UNDO": ["undo", "revert"],
    "DEFINE": ["define", "teach", "learn", "means", "mean"],
    "USE": ["use", "target", "switch", "work"],
    "OBJECT": ["object", "thing", "entity", "item", "component", "element"],
    "WHEN": ["when", "whenever", "if", "every", "once"],
    "TO": ["to", "into", "onto"],
    "WITH": ["with", "having", "has", "have"],
    "VIA": ["via", "over", "using", "through"],
    "IT": ["it", "this", "that", "them"],
    "NOT": ["no", "not", "without", "non", "never"],
    "AND": ["and", "then", "also"],
    "ARTICLE": ["a", "an", "the", "some"],
    "NAMED": ["named", "called"],
}

NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
                "eight": 8, "nine": 9, "ten": 10, "twelve": 12, "twenty": 20}

# ---------------------------------------------------------------- B. composite
# Pattern = tuple of atomic tokens that must appear in order (gaps allowed).
COMPOSITE: list[tuple[tuple[str, ...], str]] = [
    (("DEFINE",), "DEFINE_WORD"),
    (("UNDO",), "UNDO"),
    (("WHEN",), "BEHAVIOR"),
    (("DISCONNECT",), "DISCONNECT_FROM"),
    (("CONNECT", "TO"), "CONNECT_TO"),
    (("CONNECT",), "CONNECT_TO"),
    (("DELETE",), "DELETE_OBJECT"),
    (("RENAME",), "RENAME_OBJECT"),
    (("MOVE",), "MOVE_OBJECT"),
    (("USE",), "USE_DOMAIN"),
    (("SHOW",), "SHOW_STATE"),
    (("BUILD",), "BUILD_SYSTEM"),
    (("RUN",), "RUN_SYSTEM"),
    (("CREATE", "ARTICLE", "OBJECT"), "CREATE_OBJECT"),
    (("CREATE", "OBJECT"), "CREATE_OBJECT"),
    (("CREATE", "ARTICLE"), "CREATE_OBJECT"),
    (("CREATE", "IT"), "SET_PROPERTY"),
    (("SET",), "SET_PROPERTY"),
    (("CREATE",), "CREATE_OR_SET"),   # resolved by the parser (add a led vs add collision)
    (("WITH",), "SET_PROPERTY"),
]

TOKENS = set(ATOMIC) | {"NUMBER", "WORD", "DOMAIN", "PROTOCOL", "QUOTED"}


@dataclass
class Token:
    text: str
    tag: str          # atomic token or NUMBER / WORD / DOMAIN / PROTOCOL / QUOTED
    value: object = None

    def __repr__(self):
        return f"{self.tag}({self.text})" if self.tag not in ("WORD",) else f"'{self.text}'"


@dataclass
class Vocabulary:
    path: Path | None = None
    learned: dict[str, str] = field(default_factory=dict)        # word -> atomic token
    composites: list[tuple[tuple[str, ...], str]] = field(default_factory=list)
    macros: dict[str, dict] = field(default_factory=dict)        # name -> {"params": [...], "body": [...]}
    domain_words: dict[str, str] = field(default_factory=dict)   # word -> domain (from modules)

    def __post_init__(self):
        self._index: dict[str, str] = {}
        for tok, words in ATOMIC.items():
            for w in words:
                self._index.setdefault(w.lower(), tok)
        if self.path and Path(self.path).exists():
            self.load()

    # -------------------------------------------------------------- lookups
    def atomic(self, word: str) -> str | None:
        w = word.lower()
        return self.learned.get(w) or self._index.get(w)

    def all_composites(self):
        return self.composites + COMPOSITE

    def register_domains(self, modules: dict) -> None:
        """Pull domain names / aliases / keywords from loaded modules."""
        for m in modules.values():
            for w in (m.name, *m.aliases):
                self.domain_words[w.lower()] = m.name
        for m in modules.values():
            for w in m.keywords:
                self.domain_words.setdefault(w.lower(), m.name)

    # -------------------------------------------------------------- tokenize
    WORD_RE = re.compile(r'"[^"]*"|\'[^\']*\'|(?<![\w.])-?\d+(?:\.\d+)?(?![A-Za-z_\d.])|[\w؀-ۿ‌+#.|\-]+|[,;:=<>!]+')

    def tokenize(self, text: str) -> list[Token]:
        toks: list[Token] = []
        for m in self.WORD_RE.finditer(text):
            raw = m.group(0)
            low = raw.lower().rstrip(".")
            if raw[0] in "\"'":
                toks.append(Token(raw[1:-1], "QUOTED", raw[1:-1]))
            elif re.fullmatch(r"-?\d+(?:\.\d+)?", raw):
                toks.append(Token(raw, "NUMBER", float(raw) if "." in raw else int(raw)))
            elif low in NUMBER_WORDS:
                toks.append(Token(raw, "NUMBER", NUMBER_WORDS[low]))
            elif low in self.macros:
                toks.append(Token(raw, "MACRO", low))
            elif self.atomic(low):
                toks.append(Token(raw, self.atomic(low)))
            elif low in self.domain_words:
                toks.append(Token(raw, "DOMAIN", self.domain_words[low]))
            elif low in _protocols():
                toks.append(Token(raw, "PROTOCOL", low))
            else:
                toks.append(Token(raw, "WORD"))
        return toks

    def compose(self, toks: list[Token]) -> str | None:
        """Find the composite token (verb frame) for a clause."""
        tags = [t.tag for t in toks]
        if tags and tags[0] == "MACRO":
            return "MACRO"
        best = None
        for pattern, name in self.all_composites():
            if _subseq(pattern, tags):
                first = tags.index(pattern[0])
                # earliest anchor wins, then longer pattern
                key = (first, -len(pattern))
                if best is None or key < best[0]:
                    best = (key, name)
        return best[1] if best else None

    # -------------------------------------------------------------- C. adaptive
    def learn_word(self, word: str, token: str) -> str:
        """Teach that ``word`` means atomic ``token``.  Validated."""
        word = word.lower().strip()
        token = token.upper().strip()
        if not re.fullmatch(r"[\w؀-ۿ‌\-]+", word):
            raise ValueError(f"'{word}' is not a single word")
        if token not in ATOMIC:
            # allow mapping to another known word: learn spawn = create
            t2 = self.atomic(token.lower())
            if not t2:
                raise ValueError(f"unknown token '{token}'. Known: {', '.join(sorted(ATOMIC))}")
            token = t2
        core = self._index.get(word)
        if core and core != token:
            raise ValueError(f"'{word}' is a core word for {core}; refusing to redefine it")
        if word in self.macros:
            raise ValueError(f"'{word}' is already a macro")
        self.learned[word] = token
        self.save()
        return token

    def learn_composite(self, pattern: list[str], name: str) -> None:
        pat = tuple(p.upper() for p in pattern)
        if not pat or any(p not in TOKENS for p in pat):
            raise ValueError(f"composite pattern must use known tokens: {pat}")
        if not re.fullmatch(r"[A-Z][A-Z_]+", name):
            raise ValueError("composite name must be UPPER_SNAKE")
        known_frames = {n for _, n in COMPOSITE}
        if name not in known_frames:
            raise ValueError(f"composite must resolve to an executable frame: {sorted(known_frames)}")
        self.composites.insert(0, (pat, name))
        self.save()

    def learn_macro(self, name: str, params: list[str], body: list[str], validator) -> None:
        """Define a new composite *command* from natural-language statements.

        ``validator(body_statements, params) -> list[str] errors`` performs a
        dry run on a cloned state; the macro is accepted only if it is empty.
        """
        name = name.lower().strip()
        if not re.fullmatch(r"[\w؀-ۿ‌\-]+", name):
            raise ValueError("macro name must be one word")
        if self._index.get(name) or name in self.learned:
            raise ValueError(f"'{name}' is already a vocabulary word")
        if any(name in re.findall(r"[\w؀-ۿ‌\-]+", s.lower()) for s in body):
            raise ValueError("recursive macro definitions are not allowed")
        errors = validator(body, params)
        if errors:
            raise ValueError("macro rejected: " + "; ".join(errors))
        self.macros[name] = {"params": params, "body": body}
        self.save()

    def forget(self, word: str) -> bool:
        w = word.lower()
        hit = self.learned.pop(w, None) or self.macros.pop(w, None)
        self.save()
        return bool(hit)

    def unknown_words(self, toks: list[Token], known_extra: set[str] = frozenset()) -> list[str]:
        return [t.text for t in toks if t.tag == "WORD" and t.text.lower() not in known_extra]

    # -------------------------------------------------------------- persistence
    def save(self) -> None:
        if not self.path:
            return
        p = Path(self.path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "learned": self.learned,
            "composites": [[list(a), b] for a, b in self.composites],
            "macros": self.macros,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    def load(self) -> None:
        try:
            d = json.loads(Path(self.path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self.learned = {k.lower(): v for k, v in d.get("learned", {}).items() if v in ATOMIC}
        self.composites = [(tuple(a), b) for a, b in d.get("composites", [])]
        self.macros = d.get("macros", {})

    def describe(self) -> str:
        lines = ["Atomic tokens:"]
        for tok in sorted(ATOMIC):
            extra = [w for w, t in self.learned.items() if t == tok]
            lines.append(f"  {tok:<10} {', '.join(ATOMIC[tok][:6])}"
                         + (f"  + learned: {', '.join(extra)}" if extra else ""))
        lines.append("Composite tokens:")
        for pat, name in self.all_composites():
            lines.append(f"  {' + '.join(pat):<28} -> {name}")
        if self.macros:
            lines.append("Macros (learned commands):")
            for n, m in self.macros.items():
                ps = f"({', '.join(m['params'])})" if m["params"] else ""
                lines.append(f"  {n}{ps}: " + " ; ".join(m["body"]))
        doms: dict[str, list[str]] = {}
        for w, d in self.domain_words.items():
            doms.setdefault(d, []).append(w)
        lines.append("Domain words:")
        for d, ws in doms.items():
            lines.append(f"  {d:<10} {', '.join(ws[:10])}")
        return "\n".join(lines)


def _subseq(pattern, tags) -> bool:
    it = iter(tags)
    return all(any(p == t for t in it) for p in pattern)


def _protocols():
    from .ir import PROTOCOLS
    return PROTOCOLS
