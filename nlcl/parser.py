"""Rule-based semantic parser: natural language -> semantic tokens -> IR ops.

Works fully offline.  It handles the common shapes of instructions in
English (and basic Persian through a small lexicon).  Anything it is not
confident about is flagged so the hybrid runtime can ask a free LLM instead.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .ir import Op, PROTOCOLS, SystemState, ident, norm_name, parse_value, singular, validate_behavior, validate_expr
from .tokens import Token, Vocabulary

# Non-English input (Persian, ...) is NOT parsed offline: the runtime sends it to the online LLM agent,
# which rewrites it as English commands for this parser.
_FA_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

COLORS = {"red", "green", "blue", "yellow", "white", "black", "orange", "purple", "pink", "cyan",
          "magenta", "gray", "grey", "brown", "gold", "silver"}
STOP = {"it", "this", "that", "a", "an", "the", "and", "with", "has", "have", "having", "make", "give",
        "set", "add", "is", "be", "should", "its", "also", "then", "please", "them", "some", "can",
        "will", "so", "enable", "enabled", "support", "property", "properties", "for", "let",
        "let's", "lets", "me", "us", "we", "i", "want", "would", "like", "you", "now", "just", "too"}
FLAG_SUFFIX = {"detection", "support", "enabled", "capability", "ability", "mode", "on"}
UNITS = {"%", "ms", "s", "sec", "secs", "second", "seconds", "hz", "khz", "mhz", "cm", "mm", "m",
         "px", "unit", "units", "bit", "bits", "v", "volt", "volts", "deg", "degrees", "c", "kg", "g"}
BUILD_WORDS = {"it", "project", "system", "everything", "all", "code", "program", "the", "whole",
               "files", "output", "software"}
BUILD_VERBS = {"build", "generate", "compile", "export", "emit", "codegen"}
CREATE_ARTICLES = {"a", "an", "new", "another", "some", "یک", "یه"}
IMMEDIATE_VERBS = {"increase", "decrease", "increment", "decrement", "raise", "reduce", "toggle", "turn",
                   "switch", "blink", "flip", "subtract", "print", "log", "say"}


@dataclass
class ParseResult:
    ops: list[Op] = field(default_factory=list)
    clauses: list[tuple[str, str | None, list[Token]]] = field(default_factory=list)  # text, frame, tokens
    unknown: list[str] = field(default_factory=list)
    confident: bool = True
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class RuleParser:
    def __init__(self, vocab: Vocabulary, modules: dict):
        self.vocab = vocab
        self.modules = modules
        self.known_kinds: set[str] = set()
        self.kind_names: set[str] = set()          # real kinds only (not keywords)
        self.synonyms: list[tuple[list[str], list[str], int]] = []    # reviewed: [fsm] -> [state, machine]
        self._learned: list[tuple[list[str], list[str], int]] = []    # learned: [get,rid,of] -> [delete]
        self.table = RewriteTable()
        for m in modules.values():
            self.known_kinds |= {k.lower() for k in m.kinds}
            self.kind_names |= {k.lower() for k in m.kinds}
            self.known_kinds |= {k.lower() for k in m.keywords}
        self.known_kinds |= {"object", "entity", "thing", "item", "component"}

    @property
    def rewrites(self):
        return self._learned

    @rewrites.setter
    def rewrites(self, rules):
        self._learned = list(rules or [])
        self.table = RewriteTable(self._learned + self.synonyms)

    def set_synonyms(self, syn) -> None:
        self.synonyms = list(syn or [])
        self.table = RewriteTable(self._learned + self.synonyms)

    def refresh_modules(self, modules: dict) -> None:
        """A module was discovered at run time: its kinds join the vocabulary."""
        self.modules = modules
        self.vocab.register_domains(modules)
        for m in modules.values():
            self.known_kinds |= {k.lower() for k in m.kinds} | {k.lower() for k in m.keywords}
            self.kind_names |= {k.lower() for k in m.kinds}

    # ================================================================ entry
    def parse(self, text: str, state: SystemState, _depth: int = 0) -> ParseResult:
        self._state = state
        res = ParseResult()
        text = self.normalize(text)
        if not text:
            return res

        # --- bug fixing / checking is whole-sentence: "fix the bug in main.py: it crashes on start"
        m = re.match(r"^(fix|debug|repair|troubleshoot|check|diagnose|lint|verify)\b\s*(.*)$", text, re.I) \
            or re.match(r"^(?:there(?:'s| is) (?:a |an )?(?:bug|error|problem)|(?:a |the )?(?:bug|error|crash) )\s*(?:in|with)?\s*()(.*)$", text, re.I)
        if m and not re.match(r"^check\s+(?:if|whether|that)\b", text, re.I):
            verb = (m.group(1) or "fix").lower()
            mode = "check" if verb in ("check", "diagnose", "lint", "verify") else "fix"
            res.ops.append(Op("FIX", text=m.group(2).strip(), props={"mode": mode}))
            res.clauses.append((text, "FIX", self.vocab.tokenize(text)))
            return res

        # --- definitions (adaptive vocabulary) are whole-sentence
        d = self._parse_define(text)
        if d:
            res.ops.append(d)
            res.clauses.append((text, "DEFINE_WORD", self.vocab.tokenize(text)))
            return res

        # --- behaviours are whole-sentence ("when ..., do ..." / "do ... when ...")
        b = self._split_behavior(text, state)
        if b:
            cond, action = b
            try:
                op = self._parse_behavior(cond, action, state)
                res.ops.append(op)
                res.clauses.append((text, "BEHAVIOR", self.vocab.tokenize(text)))
            except ValueError as e:
                res.errors.append(f"could not understand behaviour: {e}")
                res.confident = False
            return res

        # relative clauses after a creation ("an enemy that moves left and right") are not
        # understood offline: keep the creation, report the rest instead of mis-parsing it
        m = re.match(r"^((?:create|make|add|build|spawn|new|place)\b.+?)\s+(?:that|which|who)\s+(.+)$", text, re.I)
        if m and not re.search(r"\b(?:that|which)\s+is\s+(?:on|off|true|false)\b", text, re.I):
            text = m.group(1)
            res.notes.append(f"ignored '{m.group(2)}' - describe behaviour separately, e.g. 'every 2 seconds, ...' or 'when ..., ...'")

        prev_frame = None
        for clause in self._split_clauses(text):
            toks = self.vocab.tokenize(clause)
            if not toks:
                continue
            special = self._special(clause, toks, state)
            if special is not None:
                res.clauses.append((clause, "SPECIAL", toks))
                res.ops.extend(special)
                prev_frame = "SET_PROPERTY" if special and special[-1].action == "SET" else prev_frame
                continue
            if prev_frame == "DELETE_OBJECT" and self._exact_entity(_strip_articles(clause), state):
                res.clauses.append((clause, "DELETE_OBJECT", toks))
                res.ops.append(Op("DELETE", target=_strip_articles(clause)))
                continue
            try:
                frame = self._frame(clause, toks, prev_frame)
            except ValueError as e:
                res.clauses.append((clause, None, toks))
                res.errors.append(str(e))
                res.confident = False
                continue
            res.clauses.append((clause, frame, toks))
            try:
                if frame == "SET_PROPERTY" and prev_frame == "CREATE_OBJECT" and self.vocab.compose(toks) is None \
                        and res.ops and res.ops[-1].action == "CREATE":
                    # "... with value 1 and label 'Step'": the tail belongs to the entity being created
                    more = self.extract_props(_join(toks))
                    if more:
                        res.ops[-1].props.update(more)
                        continue
                ops, unknown = self._clause_ops(frame, clause, toks, state, _depth)
            except ValueError as e:
                res.errors.append(str(e))
                res.confident = False
                continue
            res.ops.extend(ops)
            res.unknown.extend(unknown)
            if frame:
                prev_frame = frame
        if res.unknown or res.errors or not res.ops:
            res.confident = False
        return res

    # ================================================================ helpers
    def normalize(self, text: str) -> str:
        text = text.translate(_FA_DIGITS).replace("‌", "‌").strip()
        text = text.replace("’", "'").replace("“", '"').replace("”", '"').replace("،", ",").replace("؛", ";")
        text = text.lstrip("﻿")
        text = self.table.apply(text)       # reviewed synonyms + meanings learned into the Prolog KB
        text = re.sub(r"\beach\s+(?=\d|second|minute|frame|tick|hour)", "every ", text, flags=re.I)
        text = self._english(text)
        # number lists: "position 1, 2, 3" -> "position 1 2 3" (commas would split clauses)
        text = re.sub(r"(?<=\d)\s*,\s*(?=-?\d)", " ", text)
        # word lists after list-like keys: "states idle, heat and cool" -> "states idle|heat|cool"
        text = re.sub(r"\b(states|options|modes|items|levels|fields|sending|carrying|send|publish|report)\s+"
                      r"([A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)+(?:\s*,?\s*and\s+[A-Za-z_]\w*(?!\s+-?\d))?"
                      r"|[A-Za-z_]\w*\s+and\s+[A-Za-z_]\w*(?!\s+-?\d))",
                      lambda m: m.group(1) + " " + "|".join(w for w in re.split(r"\s*,\s*|\s*,?\s*\band\b\s*", m.group(2)) if w),
                      text)
        return text.rstrip(".!?؟ ")

    _NUMWORDS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
                 "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
                 "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
                 "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
                 "hundred": 100, "thousand": 1000}

    def _english(self, text: str) -> str:
        # polite / desire prefixes: "please", "could you", "I want", "I'd like", "let's"
        prev = None
        while prev != text:
            prev = text
            text = re.sub(r"^(?:please|kindly|ok(?:ay)?|now|so|and)\s*,?\s+|^(?:could|can|would|will)\s+you\s+(?:please\s+)?"
                          r"|^i(?:\s+would|'d)\s+like\s+(?:you\s+to\s+)?|^i\s+(?:want|need)\s+(?:you\s+to\s+)?"
                          r"|^let'?s\s+", "", text, flags=re.I)
        text = re.sub(r",?\s+please$", "", text, flags=re.I)
        if re.match(r"^(?:a|an|\d+)\s+\w", text, re.I) and not re.match(r"^\d+\s*(?:ms|s|sec|seconds?)\b", text, re.I):
            text = "create " + text          # "I want a game object called ball"
        # number words (outside quotes): "twenty five" -> 25, "half a second" -> 0.5 second
        text = re.sub(r"\bhalf\s+(?:a|an)\s+(second|minute)\b", r"0.5 \1", text, flags=re.I)
        text = re.sub(r"\ba\s+(second|minute)\b(?=\s*,|\s*$|\s+\w)", r"1 \1", text, flags=re.I) \
            if re.search(r"\bevery\s+a\s+(second|minute)", text, re.I) else text
        nw = "|".join(self._NUMWORDS)

        def numrep(m):
            parts = re.split(r"[\s\-]+", m.group(0).lower())
            total, cur = 0, 0
            for p in parts:
                v = self._NUMWORDS[p]
                if v == 100:
                    cur = max(cur, 1) * 100
                elif v == 1000:
                    total += max(cur, 1) * 1000
                    cur = 0
                else:
                    cur += v
            return str(total + cur)
        if not re.search(r"""["']""", text):
            text = re.sub(rf"\b(?:{nw})(?:[\s\-]+(?:{nw}))*\b(?!\s+of\b)", numrep, text, flags=re.I)
        # dimensions: "12 by 3 by 10" / "12x3x10" -> "12 3 10"
        text = re.sub(r"(?<![\w.])(\d+(?:\.\d+)?)\s*(?:mm|cm)?\s*(?:by|x|×)\s*(?=-?\d)", r"\1 ", text)
        return text

    def _split_clauses(self, text: str) -> list[str]:
        parts: list[str] = []
        # protect quoted strings
        quoted: list[str] = []

        def keep(m):
            quoted.append(m.group(0))
            return f"\x00{len(quoted) - 1}\x00"
        t = re.sub(r'"[^"]*"|\'[^\']*\'', keep, text)
        for sent in re.split(r";|\.(?=\s|$)|\bthen\b|,", t):
            sent = sent.strip()
            if not sent:
                continue
            low = sent.lower()
            if re.match(r"^(connect|link|wire|disconnect|unlink|send)\b", low):
                parts.append(sent)
                continue
            for p in re.split(r"\band\b|\s+و\s+", sent):
                if p.strip():
                    parts.append(p.strip())
        out = []
        for p in parts:
            p = re.sub(r"\x00(\d+)\x00", lambda m: quoted[int(m.group(1))], p)
            if out and re.match(r"^(?:sending|carrying|every|via|over|using|at\s+\d+\s*baud)\b", p, re.I) and \
                    re.match(r"^(?:connect|link|wire|hook)\b", out[-1], re.I):
                out[-1] = out[-1] + " " + p
                continue
            out.append(p)
        return out

    def _ent(self, phrase: str, state: SystemState) -> str | None:
        """Return the phrase (article-stripped) if it names an existing entity (or 'it')."""
        p = _strip_articles(phrase).lower()
        if p in ("it", "this", "that", "them"):
            return "it"
        return p if self._exact_entity(p, state) else None

    def _special(self, clause: str, toks: list[Token], state: SystemState) -> list[Op] | None:
        """Sentence shapes that the token frames get wrong.  None = not special."""
        low = clause.lower().strip()
        tgt = lambda e: None if e == "it" else e  # noqa: E731
        # "send temperature from temp to monitor via http every 2 seconds" -> a link
        m = re.fullmatch(r"(?:send|stream|publish|forward)\s+(.+?)\s+from\s+(?:the\s+)?(\w+)\s+to\s+(?:the\s+)?(\w+)(.*)", low)
        if m and self._ent(m.group(2), state) and self._ent(m.group(3), state):
            return [self._connect(f"connect {m.group(2)} to {m.group(3)} sending {m.group(1)}{m.group(4)}",
                                  self.vocab.tokenize(f"connect {m.group(2)} to {m.group(3)}"), state, "CONNECT")]
        # "connect / attach a led called lamp to board on pin 2" -> create it inside board
        m = re.fullmatch(r"(?:connect|attach|hook\s+up|wire)\s+(an?\s+.+?\s+(?:called|named)\s+\S+)\s+to\s+(.+)", low)
        if m:
            tail = m.group(2)
            ref = re.match(r"(?:the\s+)?(\w+)(.*)", tail)
            if ref and self._ent(ref.group(1), state) and not self._ent(re.sub(r".*(?:called|named)\s+", "", m.group(1)), state):
                c = f"add {m.group(1)} to {ref.group(1)}{ref.group(2)}"
                ops, _ = self._create(c, self.vocab.tokenize(c), state)
                return ops
        # "speed hero up" / "slow it down"
        m = re.fullmatch(r"(speed|slow)\s+(?:the\s+)?(\w+)\s+(up|down)(?:\s+(a\s+bit|a\s+little|slightly|a\s+lot|much))?", low)
        if m and (m.group(2) in ("it", "them") or self._ent(m.group(2), state)):
            word = "faster" if m.group(1) == "speed" else "slower"
            who = self._ent(m.group(2), state)
            return [Op("SET", target=None if who in (None, "it") else who,
                       props={"__relative__": [[word, m.group(4), None, None]]})]
        # focus / select
        m = re.fullmatch(r"(?:select|focus(?:\s+on)?|choose|pick|switch\s+to|go\s+to|work\s+on)\s+(.+)", low)
        if m and self._ent(m.group(1), state) not in (None, "it"):
            return [Op("FOCUS", target=self._ent(m.group(1), state))]
        # undo N
        m = re.fullmatch(r"(?:undo|revert|go\s+back|step\s+back)(?:\s+(?:the\s+)?(?:last\s+)?(\d+)?\s*(?:changes?|steps?|times?))?(?:\s+please)?", low)
        if m:
            return [Op("UNDO", count=int(m.group(1) or 1))]
        # parent relations
        pats = [(r"(?:put|place|attach|mount|move|insert|stick)\s+(.+?)\s+(?:on\s+top\s+of|onto|on|inside|into|in|to|under|below|above)\s+(.+)", False),
                (r"make\s+(.+?)\s+(?:a\s+)?(?:child|part)\s+of\s+(.+)", False),
                (r"(.+?)\s+(?:is|should\s+be)\s+(?:a\s+)?(?:child|part)\s+of\s+(.+)", False),
                (r"(.+?)\s+(?:is|should\s+be)\s+(?:inside|in|on)\s+(.+)", False),
                (r"make\s+(.+?)\s+(?:the\s+)?parent\s+of\s+(.+)", True)]
        for pat, swap in pats:
            m = re.fullmatch(pat, low)
            if m:
                a, b = self._ent(m.group(1), state), self._ent(m.group(2), state)
                if a and b and a != b:
                    if swap:
                        a, b = b, a
                    return [Op("SET", target=tgt(a), parent=b)]
        # "X has a Y [called Z]" -> create Y inside X
        m = re.fullmatch(r"(?:the\s+)?(\w+)\s+(?:has|have|contains|gets)\s+((?:a|an|\d+)\s+.+)", low)
        if m and self._ent(m.group(1), state) not in (None, "it"):
            c = f"create {m.group(2)} in {m.group(1)}"
            ops, _ = self._create(c, self.vocab.tokenize(c), state)
            return ops
        # "X follows/chases Y"
        m = re.fullmatch(r"(?:the\s+)?(\w+)\s+(?:follows?|chases?|hunts?|tracks?|targets?|pursues?)\s+(?:the\s+)?(\w+)", low)
        if m and self._ent(m.group(1), state) and self._ent(m.group(2), state):
            return [Op("SET", target=tgt(self._ent(m.group(1), state)), props={"target": m.group(2)})]
        # "change its name to X" / "name it X"
        m = re.fullmatch(r"(?:change|set|update)\s+(?:its|the|(\w+)'s)\s+name\s+to\s+[\"']?([\w؀-ۿ\-]+)[\"']?", clause.strip(), re.I) \
            or re.fullmatch(r"(?:change|set|update)\s+the\s+name\s+of\s+(?:the\s+)?(\w+)\s+to\s+[\"']?([\w\-]+)[\"']?", clause.strip(), re.I)
        if m:
            who = self._ent(m.group(1), state) if m.group(1) else "it"
            return [Op("RENAME", target=tgt(who), name=m.group(2))]
        # possessives: "hero's speed should be 9", "the speed of hero is 4"
        m = re.fullmatch(r"(?:(?:set|make|change)\s+)?(?:the\s+)?(\w+)'s\s+(.+?)\s+(?:should\s+be|is|=|becomes|to)\s+(.+)", clause.strip(), re.I) \
            or re.fullmatch(r"(?:(?:set|change|update|make)\s+)?(?:the\s+)?(.+?)\s+of\s+(?:the\s+)?(\w+)\s+(?:should\s+be|is|=|becomes|to)\s+(.+)", clause.strip(), re.I)
        if m:
            if "'s" in clause:
                who, key, val = m.group(1), m.group(2), m.group(3)
            else:
                key, who, val = m.group(1), m.group(2), m.group(3)
            if self._ent(who, state) not in (None, "it"):
                props = self.extract_props(f"{key} {val}") or {norm_name(key): self._value(val)}
                return [Op("SET", target=self._ent(who, state), props=props)]
        # "make it a counter [with width 8]" -> change kind
        m = re.fullmatch(r"(?:make|turn|convert|change)\s+(it|\w+)\s+(?:into\s+|to\s+)?(?:a|an)\s+(\w+(?:\s\w+)?)(?:\s+with\s+(.+))?", low)
        if m and self._ent(m.group(1), state):
            kind = self._compound_kind(m.group(2).split()) or singular(m.group(2).split()[-1])
            if self._is_kind(kind) and kind not in self._domain_aliases() and m.group(2) not in self._domain_aliases():
                props = {"__kind__": kind}
                if m.group(3):
                    props.update(self.extract_props(m.group(3)))
                return [Op("SET", target=tgt(self._ent(m.group(1), state)), props=props)]
        # immediate arithmetic: "add 5 to health", "subtract 3 from hero's health"
        if re.fullmatch(r"(?:add|subtract)\s+-?[\d.]+\s+(?:to|from)\s+.+", low):
            a, who = self._action(clause, state)
            return [Op("SET", target=who, props={"__act__": a})]
        # "delete all enemies" / "remove every coin"
        m = re.fullmatch(r"(?:delete|remove|destroy|erase|kill)\s+(?:all|every|each)\s+(?:the\s+|of\s+the\s+)?(\w+)", low)
        if m:
            k = singular(m.group(1))
            hits = [e.name for e in state.entities.values() if e.kind == k or singular(e.name).rstrip("0123456789") == k]
            if hits:
                return [Op("DELETE", target=h) for h in hits]
        # "connect the led to pin 13" -> pin property
        m = re.fullmatch(r"(?:connect|wire|attach|link)\s+(.+?)\s+to\s+(?:the\s+)?(?:pin|gpio)\s*(\d+)", low)
        if m and self._ent(m.group(1), state):
            return [Op("SET", target=tgt(self._ent(m.group(1), state)), props={"pin": int(m.group(2))})]
        return None

    def _compound_kind(self, words: list[str]) -> str | None:
        """Longest run of words that forms a known multi-word kind ("state machine", "clock divider")."""
        ws = [w.lower() for w in words]
        for n in range(min(4, len(ws)), 1, -1):
            for i in range(len(ws) - n, -1, -1):
                cand = "_".join(ws[i:i + n])
                cands = {cand, "_".join(ws[i:i + n - 1] + [singular(ws[i + n - 1])])}
                for c in cands:
                    if c in self.known_kinds:
                        return c
        return None

    def _frame(self, clause: str, toks: list[Token], prev: str | None) -> str | None:
        words = [t.text.lower() for t in toks]
        if words and words[0] in BUILD_VERBS and all(w in BUILD_WORDS or t.tag == "DOMAIN"
                                                      for w, t in zip(words[1:], toks[1:])):
            return "BUILD_SYSTEM"
        if words and words[0] in ("run", "simulate", "execute", "test") and all(
                w in BUILD_WORDS or t.tag == "DOMAIN" for w, t in zip(words[1:], toks[1:])):
            return "RUN_SYSTEM"
        if words and words[0] in IMMEDIATE_VERBS and not (words[0] in ("turn", "switch") and len(words) > 1
                                                           and words[1] not in ("on", "off") and "on" not in words and "off" not in words):
            return "ACTION_NOW"
        frame = self.vocab.compose(toks)
        # a SHOW / BUILD / RUN word in the middle of a sentence ("clicking plus adds 1 to the value of display")
        # is not a command; treating it as one was the most confident wrong reading
        if frame in ("SHOW_STATE", "BUILD_SYSTEM", "RUN_SYSTEM") and toks[0].tag not in ("SHOW", "BUILD", "RUN"):
            raise ValueError(f"no command recognised in '{clause}'")
        # "make hero a bit slower" / "make it a lot faster": an amount, not a new object
        if frame in ("CREATE_OBJECT", "CREATE_OR_SET") and words and words[0] in ("make", "get", "turn") and \
                re.search(r"\ba\s+(?:bit|little|lot)\b", clause, re.I):
            return "SET_PROPERTY"
        # "make the wall 3" / "make the arm 50 mm long": 'the' refers to something that exists
        if frame == "CREATE_OBJECT" and len(toks) > 1 and words[0] in ("make", "add", "give", "turn") \
                and toks[1].tag == "ARTICLE" and words[1] == "the":
            frame = "SET_PROPERTY"
        if frame == "CREATE_OR_SET":
            frame = "CREATE_OBJECT" if self._looks_like_creation(toks) else "SET_PROPERTY"
        if frame is None:
            tags = [t.tag for t in toks]
            if prev in ("CREATE_OBJECT",) and (tags[0] == "ARTICLE" or (tags[0] == "NUMBER" and len(toks) > 1
                                               and self._is_kind(toks[1].text))):
                return "CREATE_OBJECT"
            if prev not in ("CONNECT_TO", "DISCONNECT_FROM", "DELETE_OBJECT", "RENAME_OBJECT") and toks:
                return "SET_PROPERTY"
        return frame

    def _is_kind(self, w: str) -> bool:
        w = w.lower()
        if w in UNITS or w in ("long", "wide", "tall", "high", "thick", "deep"):
            return False
        return w in self.known_kinds or singular(w) in self.known_kinds or w in self.vocab.domain_words

    def _looks_like_creation(self, toks: list[Token]) -> bool:
        verb_i = next((i for i, t in enumerate(toks) if t.tag == "CREATE"), 0)
        rest = toks[:verb_i] + toks[verb_i + 1:]
        st = getattr(self, "_state", None)
        for i, t in enumerate(rest):
            if t.tag == "IT":
                return False
            # "make box blue": a bare word naming something that already exists refers to it
            if t.tag in ("WORD", "DOMAIN") and st is not None and \
                    any(t.text.lower() == e.name.lower() for e in st.entities.values()):
                return False
            if t.tag == "OBJECT" or (t.tag == "DOMAIN" and self._is_kind(t.text)):
                return True
            if t.tag == "ARTICLE":
                # "a bit / a little / a lot" is an amount, not an article ("make hero a bit slower")
                if i + 1 < len(rest) and rest[i + 1].text.lower() in ("bit", "little", "lot"):
                    return False
                # "make the wall 3" / "make the led blink" refer to something that exists
                return t.text.lower() in CREATE_ARTICLES
            if t.tag == "NUMBER":
                return i + 1 < len(rest) and (self._is_kind(rest[i + 1].text)
                                              or (rest[i + 1].text.lower().endswith("s") and rest[i + 1].tag == "DOMAIN"))
            if t.tag in ("WORD", "DOMAIN"):
                w = t.text.lower()
                # bare plural without article ("add vents") is a feature/property, not a new entity
                if w.endswith("s") and singular(w) != w and t.tag == "WORD":
                    return False
                return self._is_kind(w) and w not in COLORS
        return False

    # ================================================================ clauses
    def _clause_ops(self, frame, clause, toks, state, depth) -> tuple[list[Op], list[str]]:
        if frame == "MACRO":
            return self._expand_macro(toks, state, depth), []
        if frame == "ACTION_NOW":
            a, who = self._action(clause, state)
            return [Op("SET", target=who, props={"__act__": a})], []
        if frame == "CREATE_OBJECT":
            return self._create(clause, toks, state)
        if frame == "SET_PROPERTY":
            return self._set(clause, toks, state)
        if frame == "CONNECT_TO":
            return [self._connect(clause, toks, state, "CONNECT")], []
        if frame == "DISCONNECT_FROM":
            return [self._connect(clause, toks, state, "DISCONNECT")], []
        if frame == "DELETE_OBJECT":
            return [self._delete(clause, toks, state)], []
        if frame == "RENAME_OBJECT":
            return [self._rename(clause, toks, state)], []
        if frame == "MOVE_OBJECT":
            return [self._move(clause, toks, state)], []
        if frame == "USE_DOMAIN":
            verb = toks[0].text.lower()
            dom = next((t.value for t in toks if t.tag == "DOMAIN"
                        and (verb != "target" or t.text.lower() in self._domain_aliases())), None)
            if not dom:
                if len(toks) > 1:            # "target hero" is a property, not a domain switch
                    return self._set(clause, toks, state)
                raise ValueError(f"which domain? ({', '.join(self.modules)})")
            return [Op("DOMAIN", domain=dom)], []
        if frame == "SHOW_STATE":
            rest = " ".join(t.text for t in toks if t.tag not in ("SHOW", "ARTICLE"))
            return [Op("SHOW", text=rest or None)], []
        if frame == "BUILD_SYSTEM":
            return [Op("BUILD")], []
        if frame == "RUN_SYSTEM":
            dom = next((t.value for t in toks if t.tag == "DOMAIN"), None)
            return [Op("RUN", domain=dom)], []
        if frame == "UNDO":
            return [Op("UNDO")], []
        raise ValueError(f"I don't know what to do with: '{clause}'")

    # ---------------------------------------------------------------- CREATE
    def _create(self, clause, toks, state):
        unknown: list[str] = []
        verb_i = next((i for i, t in enumerate(toks) if t.tag == "CREATE"), None)
        body = [t for i, t in enumerate(toks) if i != verb_i]
        # split noun phrase / props at WITH
        wi = next((i for i, t in enumerate(body) if t.tag == "WITH"), None)
        np_toks, prop_toks = (body[:wi], body[wi + 1:]) if wi is not None else (body, [])
        # name: "called X" / "named X"
        name = None
        ni = next((i for i, t in enumerate(np_toks) if t.tag == "NAMED"), None)
        if ni is not None:
            # "called my robot" -> my_robot ; stop at to/in/on/for/at
            j = ni + 1
            parts = []
            while j < len(np_toks) and (np_toks[j].tag in ("WORD", "DOMAIN", "QUOTED", "OBJECT", "NUMBER")
                                        or (j == ni + 1 and np_toks[j].tag != "TO")) \
                    and np_toks[j].text.lower() not in ("on", "in", "at", "for", "inside", "onto"):
                parts.append(np_toks[j].text)
                j += 1
                if np_toks[j - 1].tag == "QUOTED":
                    break
            if parts:
                name = "_".join(parts)
            np_toks = np_toks[:ni] + np_toks[max(j, ni + 1):]
        ni = next((i for i, t in enumerate(prop_toks) if t.tag == "NAMED"), None)
        if ni is not None and name is None and ni + 1 < len(prop_toks):
            name = prop_toks[ni + 1].text
            prop_toks = prop_toks[:ni] + prop_toks[ni + 2:]
        # parent: "... to/in/inside X"
        parent = None
        tail_props: list[Token] = []
        dom_phrase = []
        for ti, t in enumerate(np_toks):
            if t.text.lower() in ("in", "using", "with") and ti + 1 < len(np_toks) and \
                    np_toks[ti + 1].text.lower() in self._domain_aliases():
                dom_phrase = [ti, ti + 1]
                break
        if dom_phrase:
            explicit_dom = np_toks[dom_phrase[1]].value
            np_toks = np_toks[:dom_phrase[0]] + np_toks[dom_phrase[1] + 1:]
        else:
            explicit_dom = None
        for ti, t in enumerate(np_toks):
            if not (t.tag == "TO" or t.text.lower() in ("in", "inside", "on", "onto", "for", "at")):
                continue
            after = [x for x in np_toks[ti + 1:] if x.tag != "ARTICLE"]
            if not after:
                continue
            # longest prefix that names an existing entity is the parent; the rest are props
            hit = 0
            for k in range(min(4, len(after)), 0, -1):
                if self._exact_entity(" ".join(x.text for x in after[:k]), state):
                    hit = k
                    break
            if hit:
                parent = " ".join(x.text for x in after[:hit])
                tail_props = after[hit:]
            else:
                tail_props = np_toks[ti + 1:]
            np_toks = np_toks[:ti]
            break
        tail_props = [x for x in tail_props if x.text.lower() not in ("on", "at", "in")]
        # count / domain / kind / modifiers
        count = 1
        domains: list[str] = []
        hints: list[str] = []
        props: dict = {}
        nouns: list[str] = []
        skip = set()
        for i, t in enumerate(np_toks):
            if i in skip:
                continue
            nxt = np_toks[i + 1].text.lower() if i + 1 < len(np_toks) else ""
            if t.tag == "NUMBER" and nxt in ("bit", "bits", "bitwise"):
                props["width"] = t.value          # "an 8 bit counter"
                skip.add(i + 1)
            elif t.tag == "NUMBER" and nxt in ("segment", "segments", "channel", "channels", "way", "axis", "pin", "pins") \
                    and isinstance(t.value, int):
                nouns.append({7: "seven", 4: "four", 8: "eight", 2: "two", 3: "three", 16: "sixteen"}.get(t.value, str(t.value)))
            elif t.tag == "NUMBER" and isinstance(t.value, int) and not nouns:
                count = t.value
            elif t.tag == "DOMAIN":
                if t.text.lower() in self._domain_aliases():
                    domains.append(t.value)        # explicit: "a game object", "a unity cube"
                else:
                    hints.append(t.value)          # keyword only: decided by the interpreter
                if self._is_kind(t.text) and t.text.lower() not in self.vocab.domain_words.keys() - self.known_kinds:
                    nouns.append(t.text.lower())
            elif t.tag == "OBJECT":
                nouns.append("object")
            elif t.tag == "WORD" or (t.tag in ("SHOW", "RUN", "BUILD", "USE", "MOVE", "SET", "DELETE", "CONNECT",
                                                "UNDO", "RENAME", "WHEN", "PROTOCOL") and nouns is not None and i > 0):
                w = t.text.lower()
                if w in COLORS:
                    props["color"] = w
                elif w in STOP:
                    continue
                else:
                    nouns.append(w)
            elif t.tag == "QUOTED" and name is None:
                name = t.text
        # kind: a known multi-word kind ("state machine"), else the LAST known kind noun
        # ("temperature sensor" and Persian-order "sensor temperature" both -> sensor)
        compound = self._compound_kind(nouns)
        if compound:
            parts = compound.split("_")
            kind = compound
            last = max(i for i, n in enumerate(nouns) if n in parts or singular(n) in parts)
            trailing = [n for n in nouns[last + 1:] if not self._is_kind(n)]
            if trailing and name is None and len(trailing) <= 2:
                name = "_".join(trailing)
            mods = [n for n in nouns[:last + 1] if n not in parts and singular(n) not in parts]
        else:
            known = [n for n in nouns if self._is_kind(n) and n != "object" and not
                     (self._is_domain_name(n) and singular(n) not in self.kind_names)]
            head = known[-1] if known else (nouns[-1] if nouns else "object")
            kind = singular(head)
            mods = [n for n in nouns if n != head]
        if kind in ("entity", "thing", "item", "element"):
            kind = "object"
        mods = [n for n in mods if n != "object" and not self._is_domain_name(n)]
        if mods:
            props["subtype"] = "_".join(mods)
        if not self._is_kind(kind) and kind != "object":
            unknown.append(kind)  # fine (new kind), but tell hybrid mode
        # pure domain names are not kinds ("a python object" -> kind object), unless the
        # domain also defines that kind ("a 3d scene")
        if self._is_domain_name(kind) and kind not in self.kind_names:
            kind = "object"
        ptxt = " , ".join(s for s in (_join(prop_toks), _join(tail_props)) if s)
        props.update(self.extract_props(ptxt, unknown))
        if name is None and isinstance(props.get("name"), str):
            name = props.pop("name")
        if explicit_dom:
            domains.append(explicit_dom)
        domain = self._pick_domain(domains)
        op = Op("CREATE", domain=domain, kind=kind, name=name or ("_".join(mods + [kind]) if mods else None),
                props=props, parent=parent, count=count)
        if len(set(domains)) > 1 and not domain:
            op.props["__domain_candidates__"] = sorted(set(domains))
        elif not domain and hints:
            op.props["__domain_hints__"] = sorted(set(hints))
        return [op], unknown

    def _is_domain_name(self, w: str) -> bool:
        m = self.modules.get(w)
        return bool(m) or any(w in mm.aliases for mm in self.modules.values())

    def _pick_domain(self, domains: list[str]) -> str | None:
        uniq = list(dict.fromkeys(domains))
        if len(uniq) == 1:
            return uniq[0]
        return None

    # ---------------------------------------------------------------- SET
    def _set(self, clause, toks, state):
        unknown: list[str] = []
        target, used = self._find_ref(toks, state)
        # drop the leading verb only: "set factory spawn to drone" keeps 'spawn' as the key
        rest = [t for i, t in enumerate(toks) if i not in used and t.tag not in ("IT", "WITH")
                and not (i == 0 and t.tag in ("SET", "CREATE"))
                and not (t.tag == "SET" and t.text.lower() in ("کن", "بده"))]
        parent = None
        dom = None
        txt = " ".join(t.text if t.tag != "QUOTED" else f'"{t.text}"' for t in rest)
        # "make it a game object" -> domain hint
        doms = [t.value for t in rest if t.tag == "DOMAIN" and t.text.lower() in self._domain_aliases()]
        if doms and len(rest) <= 3:
            dom = doms[0]
            txt = ""
        props = self.extract_props(txt, unknown) if txt else {}
        if not props and not dom:
            raise ValueError(f"no properties found in '{clause}'")
        op = Op("SET", target=target, props=props, parent=parent)
        if dom:
            op.domain = dom
        return [op], unknown

    def _domain_aliases(self):
        out = set()
        for m in self.modules.values():
            out |= {m.name, *m.aliases}
        return out

    def _find_ref(self, toks: list[Token], state: SystemState, skip_tags=("SET", "CREATE", "DELETE", "CONNECT", "DISCONNECT", "RENAME", "MOVE", "ARTICLE", "TO", "VIA", "WITH")):
        """Longest run of words that names an existing entity (exact name/kind/id)."""
        best = (None, set())
        n = len(toks)
        for i in range(n):
            if toks[i].tag in skip_tags or toks[i].tag in ("NUMBER", "QUOTED"):
                continue
            for j in range(min(n, i + 4), i, -1):
                span = toks[i:j]
                if any(t.tag in skip_tags or t.tag == "NUMBER" for t in span):
                    continue
                phrase = " ".join(t.text for t in span).lower().replace("'s", "")
                if self._exact_entity(phrase, state):
                    if len(span) > len(best[1]):
                        idx = set(range(i, j))
                        if i > 0 and toks[i - 1].tag == "ARTICLE":
                            idx.add(i - 1)
                        best = (phrase, idx)
                    break
        if best[0] is None:
            for i, t in enumerate(toks):
                if t.tag == "IT":
                    return None, {i}
        return best

    @staticmethod
    def _exact_entity(phrase: str, state: SystemState) -> bool:
        from .ir import ident
        p = phrase.strip().lower()
        for e in state.entities.values():
            if p in (e.id, e.name.lower(), e.kind.lower()) or ident(p) == e.name.lower() \
                    or singular(p) == e.kind.lower():
                return True
        return False

    # ---------------------------------------------------------------- props
    def extract_props(self, text: str, unknown: list[str] | None = None) -> dict:
        """'100 health, speed 5, movable, no gravity, color red, position 1 2 3' -> dict."""
        props: dict = {}
        if unknown is None:
            unknown = []
        # "temperature 20 pin 4" -> "temperature 20, pin 4"
        text = re.sub(r"([A-Za-z_]\w*\s+-?\d+(?:\.\d+)?)\s+(?=[A-Za-z_]\w*\s+-?\d)", r"\1, ", text)
        quoted: list[str] = []

        def keep(m):
            quoted.append(m.group(0))
            return f"\x00{len(quoted) - 1}\x00"
        text = re.sub(r'"[^"]*"|\'[^\']*\'', keep, text)
        for seg in re.split(r",|;|\band\b|\bwith\b", text):
            seg = re.sub(r"\x00(\d+)\x00", lambda m: quoted[int(m.group(1))], seg)
            seg = seg.strip().strip(".")
            words = seg.split()
            while words and words[0].lower() in STOP:
                words.pop(0)
            while words and words[-1].lower() in ("too", "also", "please"):
                words.pop()
            seg = " ".join(words)
            if not seg:
                continue
            low = seg.lower()
            # negation
            m = re.fullmatch(r"(?:no|not|non|without|disable|disabled|never|unable to)[\s\-]+(.+)", low)
            if m:
                props[self._key(m.group(1))] = False
                continue
            # fuzzy: "very fast", "a bit faster", "twice as fast", "brightness to high"
            fz = self._fuzzy_seg(low)
            if fz:
                key, item = fz
                props.setdefault(key, []).append(item)
                continue
            # quoted string with key: label "hello", text 'hi {score}', a state of "red", an owner named 'Sara'
            m = re.fullmatch(r"([A-Za-z_][\w ]*?)\s*(?:=|:|\bto\b|\bis\b|\bas\b|\bof\b|\bnamed\b|\bcalled\b)?\s*(\"[^\"]*\"|'[^']*')", seg)
            if m:
                props[self._key(m.group(1))] = parse_value(m.group(2))
                continue
            # "a heater that is off", "a door which is open"
            m = re.fullmatch(r"(?:an?\s+)?(\w+)\s+(?:that|which)\s+is\s+(on|off|true|false|open|closed|enabled|disabled)", low)
            if m:
                props[self._key(m.group(1))] = m.group(2) in ("on", "true", "open", "enabled")
                continue
            # "4 holes of 3.2 mm", "6 holes"
            m = re.fullmatch(r"(\d+)\s+holes?(?:\s+(?:of|with\s+diameter)\s+(\d+(?:\.\d+)?)\s*(mm|cm)?(?:\s+diameter)?)?", low)
            if m:
                props["hole_count"] = int(m.group(1))
                if m.group(2):
                    props["hole_diameter"] = _length(float(m.group(2)), m.group(3))
                continue
            # "50 mm long", "3 cm thick", "20 wide"
            m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(mm|cm|m|in|inch|inches)?\s+(long|wide|tall|high|thick|deep)", low)
            if m:
                key = {"long": "length", "wide": "width", "tall": "height", "high": "height",
                       "thick": "thickness", "deep": "depth"}[m.group(3)]
                props[key] = _length(float(m.group(1)), m.group(2))
                continue
            # "width 25mm", "diameter 2 cm" (number glued to or followed by a length unit)
            m = re.fullmatch(r"([A-Za-z_][\w ]*?)\s*(?:=|:|\bof\b|\bto\b|\bis\b)?\s*(\d+(?:\.\d+)?)\s*(mm|cm|m|in|inch|inches)", low)
            if m and m.group(3) != "m" or (m and self._key(m.group(1)) in LENGTH_KEYS):
                props[self._key(m.group(1))] = _length(float(m.group(2)), m.group(3))
                continue
            # "dark green", "light blue"
            m = re.fullmatch(r"(?:colou?r(?:ed)?\s+)?((?:dark|light|bright|pale|deep|neon)\s+(?:%s))(?:\s+colou?r)?" % "|".join(COLORS), low)
            if m:
                props["color"] = m.group(1)
                continue
            # boolean value: "lid false", "vents true", "movable yes"
            m = re.fullmatch(r"(.+?)\s+(?:(?:=|is|to)\s+)?(true|false|yes|no|on|off|enabled|disabled)", low)
            if m and self._key(m.group(1)):
                props[self._key(m.group(1))] = m.group(2) in ("true", "yes", "on", "enabled")
                continue
            # word list (from normalize): "states idle|heat|cool"
            m = re.fullmatch(r"([A-Za-z_][\w ]*?)\s+(\w+(?:\|\w+)+)", seg)
            if m:
                props[self._key(m.group(1))] = [parse_value(w) for w in m.group(2).split("|")]
                continue
            # key = value / key to value / key is value / key of value
            m = re.fullmatch(r"(.+?)\s*(?:=|:|\bto\b|\bis\b|\bequals?\b|\bof\b|\bat\b|\bas\b)\s*(.+)", seg, re.I)
            if m and not re.fullmatch(r"-?\d+(\.\d+)?", m.group(1).strip()):
                props[self._key(m.group(1))] = self._value(m.group(2))
                continue
            # 100 health / 8 bit width / 5 meters speed
            m = re.fullmatch(r"(-?\d+(?:\.\d+)?)\s*(%|[a-zA-Z]+)?\s+(?:of\s+)?([A-Za-z_][\w ]*)", seg)
            if m:
                unit, key = m.group(2), m.group(3)
                if unit and unit.lower() not in UNITS:
                    key = f"{unit} {key}"
                props[self._key(key)] = self._value(m.group(1))
                continue
            m = re.fullmatch(r"(-?\d+(?:\.\d+)?)\s*([a-zA-Z]+)", seg)  # "5 lives", "3s"
            if m and m.group(2).lower() not in UNITS:
                props[self._key(m.group(2))] = self._value(m.group(1))
                continue
            # speed 5 / pin 13 / position 1 2 3 / size 2x2x2 / interval 500 ms
            m = re.fullmatch(r"([A-Za-z_][\w ]*?)\s+(-?\d+(?:\.\d+)?(?:\s*[ ,x×]\s*-?\d+(?:\.\d+)?)*)\s*([a-zA-Z%]+)?", seg)
            if m:
                props[self._key(m.group(1))] = self._value(m.group(2))
                continue
            # quoted string with key: label "hello"
            m = re.fullmatch(r"([A-Za-z_][\w ]*?)\s+(\"[^\"]*\"|'[^']*')", seg)
            if m:
                props[self._key(m.group(1))] = parse_value(m.group(2))
                continue
            # colour alone / "color red" / "red color"
            cw = [w for w in low.split() if w in COLORS]
            if cw and len(low.split()) <= 2 and set(low.split()) - set(cw) <= {"color", "colour", "colored", "coloured"}:
                props["color"] = cw[0]
                continue
            # string-valued keys: "shape cylinder", "board esp32", "label hello"
            m = re.fullmatch(r"titled\s+(\"[^\"]*\"|'[^']*'|\S+)", seg, re.I)
            if m:
                props["title"] = parse_value(m.group(1))
                continue
            m = re.fullmatch(r"(color|colour|shape|material|mode|label|text|title|board|subtype|font|direction|"
                             r"state|unit|units|protocol|model|type|ssid|host|topic|key|side|target|url|file|"
                             r"path|follow|owner|sound|image|texture|icon|language|format|variant|style|role)"
                             r"\s+(\S+)", seg, re.I)
            if m:
                props[self._key(m.group(1))] = parse_value(m.group(2))
                continue
            # flag(s): movable / collision detection / physics
            ws = [w for w in re.findall(r"[A-Za-z_][\w\-]*", low) if w not in STOP]
            while len(ws) > 1 and ws[-1] in FLAG_SUFFIX:
                ws.pop()
            if 1 <= len(ws) <= 3:
                key = self._key(" ".join(ws))
                if key:
                    props[key] = True
                    continue
            unknown.append(seg)
        return props

    def _fuzzy_seg(self, low: str):
        """A vague-word segment -> ('__fuzzy__', [adj, hedge, prop]) or ('__relative__', [word, amount, factor, prop])."""
        from . import fuzzy
        H, A = fuzzy.HEDGE_WORDS, fuzzy.AMOUNT_WORDS
        low = re.sub(r"^(bit|little|lot)\b", r"a \1", low)          # the article was stripped as a stop word
        m = re.fullmatch(rf"(?:({A})\s+)?([a-z]+er)(?:\s+than\s+(?:before|now))?", low)
        if m and fuzzy.comparative(m.group(2)) and fuzzy.comparative(m.group(2))[0] != "any":
            return "__relative__", [m.group(2), m.group(1), None, None]
        m = re.fullmatch(r"(twice|two\s+times|three\s+times|half)\s+as\s+([a-z]+)(?:\s+as\s+(?:before|now))?", low)
        if m and fuzzy.adjective(m.group(2)):
            f = {"twice": 2.0, "half": 0.5}.get(m.group(1).split()[0], 3.0 if m.group(1).startswith("three") else 2.0)
            return "__relative__", [m.group(2), None, f, None]
        m = re.fullmatch(rf"(?:({H})\s+)?([a-z]+)", low)
        if m and fuzzy.adjective(m.group(2)) and fuzzy.adjective(m.group(2))[0] != "any":
            return "__fuzzy__", [m.group(2), m.group(1), None]
        m = re.fullmatch(rf"([a-z_][\w ]*?)\s+(?:to\s+|is\s+|=\s*|at\s+)?(?:({H})\s+)?(low|high|medium|normal|average)", low)
        if m and self._key(m.group(1)):
            return "__fuzzy__", [m.group(3), m.group(2), self._key(m.group(1))]
        m = re.fullmatch(rf"([a-z_][\w ]*?)\s+(?:({A})\s+)?(higher|lower)", low)
        if m and self._key(m.group(1)):
            return "__relative__", [m.group(3), m.group(2), None, self._key(m.group(1))]
        return None

    @staticmethod
    def _key(s: str) -> str:
        words = [w for w in re.findall(r"[A-Za-z_][\w]*", s.lower()) if w not in STOP]
        return norm_name(" ".join(words))

    @staticmethod
    def _value(s: str):
        s = s.strip()
        nums = re.findall(r"-?\d+(?:\.\d+)?", s)
        if len(nums) > 1 and re.fullmatch(r"[\s\-\d.,x×()\[\]]+", s):
            return [parse_value(n) for n in nums]
        m = re.fullmatch(r"(-?\d+(?:\.\d+)?)\s*([a-zA-Z%]+)?", s)
        if m and (not m.group(2) or m.group(2).lower() in UNITS):
            return parse_value(m.group(1))
        return parse_value(s.strip())

    # ---------------------------------------------------------------- CONNECT
    def _connect(self, clause, toks, state, action):
        low = clause.lower()
        via = next((t.value for t in toks if t.tag == "PROTOCOL"), None)
        props: dict = {}
        m = re.search(r"(\d+)\s*baud|baud(?:\s*rate)?\s*(\d+)", low)
        if m:
            props["baud"] = int(m.group(1) or m.group(2))
        m = re.search(r"\bport\s*(\d+)", low)
        if m:
            props["port"] = int(m.group(1))
        m = re.search(r"\btopic\s+([\w/#+\-]+)", low)
        if m:
            props["topic"] = m.group(1)
        m = re.search(r"\bevery\s+(\d+(?:\.\d+)?)?\s*(milliseconds?|ms|seconds?|secs?|minutes?|s)\b", low)
        if m:
            props["interval_ms"] = _to_ms(m.group(1), m.group(2))
        m = re.search(r"\b(?:sending|send|carrying|with fields?|fields?)\s+(.+?)(?:\b(?:via|over|using|every|at|port|topic)\b|$)", low)
        if m:
            fields = [norm_name(f) for f in re.split(r",|\band\b|\|", m.group(1)) if norm_name(f) and norm_name(f) not in ("it",)]
            if fields:
                props["fields"] = fields
        # endpoints: words between verb and TO/AND, and after
        body = re.sub(r"\b(via|over|using|through)\b.*$", "", low)
        body = re.sub(r"\b(sending|send|carrying|with fields?|fields?|every|at)\b.*$", "", body)
        body = re.sub(r"^\s*(connect|link|wire|attach|bind|hook|disconnect|unlink|detach|unwire)\s+(up\s+)?", "", body)
        parts = re.split(r"\b(?:to|with|and|from|into)\b|\s+به\s+", body, maxsplit=1)
        a = _strip_articles(parts[0]) if parts else ""
        b = _strip_articles(parts[1]) if len(parts) > 1 else ""
        if not b:
            raise ValueError(f"connect what to what? ('{clause}')")
        if via and via not in PROTOCOLS:
            via = None
        return Op(action, target=a or None, dst=b, via=via, props=props)

    # ---------------------------------------------------------------- DELETE / RENAME / MOVE
    def _delete(self, clause, toks, state):
        low = clause.lower()
        m = re.match(r"^\s*\w+\s+(?:the\s+)?(\w+)\s+of\s+(?:the\s+)?(\w+)\s*$", low) \
            or re.match(r"^\s*\w+\s+(?:the\s+)?(\w+)'s\s+(\w+)\s*$", low)
        if m:
            a, b = m.group(1), m.group(2)
            prop, who = (a, b) if " of " in low else (b, a)
            e = state.resolve(who) if self._exact_entity(who, state) else None
            if e is not None and norm_name(prop) in e.props:
                return Op("UNSET", target=who, props={norm_name(prop): None})
        m = re.match(r"^\s*\w+\s+(.+?)\s+from\s+(.+)$", low)
        if m:
            what, frm = _strip_articles(m.group(1)), _strip_articles(m.group(2))
            if not self._exact_entity(what, state):
                return Op("UNSET", target=frm if frm not in ("it",) else None, props={norm_name(what): None})
        target, used = self._find_ref(toks, state)
        rest = [t for i, t in enumerate(toks) if i not in used and t.tag not in ("DELETE", "ARTICLE", "IT")]
        if target is None and not any(t.tag == "IT" for t in toks) and rest:
            words = " ".join(t.text for t in rest)
            focus = state.resolve(None)
            key = norm_name(words)
            if focus and key in focus.props:
                return Op("UNSET", target=None, props={key: None})
            target = words
        return Op("DELETE", target=target)

    def _rename(self, clause, toks, state):
        low = clause.strip()
        m = re.match(r"^\s*(?:rename|call|name)\s+(.+?)\s+(?:to|as)\s+[\"']?([\w؀-ۿ\-]+)[\"']?\s*$", low, re.I) \
            or re.match(r"^\s*(?:rename|call|name)\s+(it|this|that|the\s+\w+|\w+)\s+[\"']?([\w؀-ۿ\-]+)[\"']?\s*$", low, re.I)
        if not m:
            raise ValueError(f"rename what to what? ('{clause}')")
        tgt = _strip_articles(m.group(1))
        return Op("RENAME", target=None if tgt.lower() in ("it", "this", "that") else tgt, name=m.group(2))

    def _move(self, clause, toks, state):
        low = clause.lower()
        target, used = self._find_ref(toks, state)
        nums = [t.value for t in toks if t.tag == "NUMBER"]
        dirs = {"up": (0, 1, 0), "down": (0, -1, 0), "left": (-1, 0, 0), "right": (1, 0, 0),
                "forward": (0, 0, 1), "forwards": (0, 0, 1), "back": (0, 0, -1), "backward": (0, 0, -1),
                "backwards": (0, 0, -1)}
        for w, vec in dirs.items():
            if re.search(rf"\b{w}\b", low):
                k = nums[0] if nums else 1
                return Op("SET", target=target, props={"__move__": [vec[0] * k, vec[1] * k, vec[2] * k]})
        if re.search(r"\bby\b", low) and nums:
            v = (nums + [0, 0, 0])[:3]
            return Op("SET", target=target, props={"__move__": v})
        if nums:
            v = (nums + [0, 0, 0])[:3]
            return Op("SET", target=target, props={"position": v})
        m = re.search(r"\b(?:to|into|inside|in|onto)\s+(.+)$", low)
        if m:
            return Op("SET", target=target, parent=_strip_articles(m.group(1)))
        raise ValueError(f"move where? ('{clause}')")

    # ---------------------------------------------------------------- DEFINE (adaptive)
    def _parse_define(self, text: str) -> Op | None:
        m = re.match(r"^(?:define|teach(?:\s+(?:that|you))?|learn(?:\s+that)?)\s+[\"']?([\w؀-ۿ\-]+)[\"']?"
                     r"(?:\s*\(([^)]*)\))?\s+(?:as|=|means|to mean|is)\s+(.+)$", text, re.I) \
            or re.match(r"^[\"']?([\w؀-ۿ\-]+)[\"']?(?:\s*\(([^)]*)\))?\s+means\s+(.+)$", text, re.I)
        if not m:
            return None
        params = [p.strip().lower() for p in (m.group(2) or "").split(",") if p.strip()]
        return Op("DEFINE", name=m.group(1).lower(), text=m.group(3).strip(), props={"params": params})

    def _expand_macro(self, toks, state, depth) -> list[Op]:
        if depth > 4:
            raise ValueError("macro nesting too deep")
        name = toks[0].value
        macro = self.vocab.macros[name]
        args = [t for t in toks[1:] if t.tag in ("NUMBER", "QUOTED", "WORD", "DOMAIN")]
        ops: list[Op] = []
        target, used = self._find_ref(toks[1:], state)
        if target:
            ops.append(Op("FOCUS", target=target))
            args = [t for i, t in enumerate(toks[1:]) if i not in used and t.tag in ("NUMBER", "QUOTED", "WORD")]
        values = {p: (args[i].text if i < len(args) else "0") for i, p in enumerate(macro["params"])}
        for stmt in macro["body"]:
            s = stmt
            for p, v in values.items():
                s = re.sub(rf"(?<![\w-])-{re.escape(p)}\b", lambda _m, v=v: str(-parse_value(v)) if isinstance(parse_value(v), (int, float)) else "-" + v, s)
                s = re.sub(rf"\b{re.escape(p)}\b", v, s)
            r = self.parse(s, state, depth + 1)
            if r.errors:
                raise ValueError(f"macro '{name}': " + "; ".join(r.errors))
            ops.extend(r.ops)
        return ops

    # ---------------------------------------------------------------- BEHAVIOUR
    _WHEN = r"(?:when(?:ever)?|if|every|once|on)"

    _ACT_VERBS = (r"destroy|delete|remove|set|increase|decrease|increment|decrement|reduce|raise|toggle|turn|"
                  r"switch|print|log|say|move|jump|send|publish|blink|add|subtract|play|call|stop|open|close|beep|"
                  r"flash|reset|spawn|fire|shoot|rotate|spin|change|show|display|go|heal|kill|die|disappear|"
                  r"report|transmit|flip|invoke|trigger")

    def _act_verbs(self) -> str:
        extra = [re.escape(w) for w in list(self.vocab.macros) + list(self.vocab.learned)]
        return self._ACT_VERBS + ("|" + "|".join(extra) if extra else "")

    def _split_behavior(self, text: str, state: SystemState | None = None) -> tuple[str, str] | None:
        low = text.lower()
        verbs = self._act_verbs()
        # "make the led blink every 500 ms" -> "every 500 ms, blink the led"
        m = re.match(r"^(?:make|let)\s+(?:the\s+)?(\w+)\s+(blink|flash|toggle|flicker|spin|rotate|beep|move)\s+"
                     r"(every\s+.+)$", text, re.I)
        if m:
            verb = {"flicker": "blink"}.get(m.group(2).lower(), m.group(2))
            return m.group(3), f"{verb} {m.group(1)}"
        m = re.match(rf"^({self._WHEN}\b.+?)(?:,|\bthen\b|:)\s*(.+)$", text, re.I)
        if m and not re.match(r"^on\s+(pin|the\s+board|board)\b", low):
            cond, act = m.group(1), m.group(2)
            if low.startswith("on ") and not re.match(r"^on\s+(collision|start|startup|hit|contact|touch|click|press|message|receive|data|update|tick|each|every)", low):
                return None
            return cond, act
        # action first: "destroy it when ...", "add 1 to score every 3 seconds", "hero jumps when ..."
        if not re.match(r"^(create|build|connect|link|define|use)\b|^(add|make)\s+(a|an|new|\d+\s+(?!to\b|from\b)\w)", low):
            for m in re.finditer(r"\s+(?=(?:when(?:ever)?|if|every|once)\b)", text, re.I):
                head, tail = text[:m.start()], text[m.end():]
                if self._starts_with_action(head.split(), state):
                    return tail, head
        # "every 2 seconds blink the led" without comma
        m = re.match(r"^(every\s+(?:\d+(?:\.\d+)?\s*)?(?:milliseconds?|ms|seconds?|secs?|minutes?|mins?|s|frame|tick))\b\s+(.+)$", text, re.I)
        if m:
            return m.group(1), m.group(2)
        # no comma: "when health is below 1 destroy it", "if space is pressed set speed to 10"
        if re.match(r"^(?:when(?:ever)?|if|once)\s", low):
            words = text.split()
            # the LONGEST condition wins: "when it hits the wall destroy it" -> cond "when it hits the wall"
            for k in range(len(words) - 1, 1, -1):
                cond, act = words[:k], words[k:]
                if re.fullmatch(r"(?:is|are|was|gets?|the|a|an|of|to|than|and|or|by|then|with)", cond[-1], re.I):
                    continue
                if self._starts_with_action(act, state):
                    return " ".join(cond), " ".join(act)
        return None

    def _starts_with_action(self, words: list[str], state: SystemState | None) -> bool:
        """'destroy it', 'hero jumps', 'the tank fires a bullet', 'smash it' (taught word)."""
        if not words:
            return False
        vre = re.compile(rf"(?:{self._act_verbs()})(?:s|es|ed)?", re.I)
        w = [x.lower().strip(",") for x in words]
        if vre.fullmatch(w[0]):
            return True
        if state is not None:
            for n in (2, 1):            # subject: "the tank fires", "hero jumps"
                if len(w) > n and self._ent(" ".join(w[:n]), state) and vre.fullmatch(w[n]):
                    return True
        return False

    def _parse_behavior(self, cond_text: str, action_text: str, state: SystemState) -> Op:
        trigger, cond = self._trigger(cond_text.strip(), state)
        partner = None
        if isinstance(trigger, tuple):            # ("collision", partner, subject)
            trigger, partner, subject = trigger
        else:
            subject = None
        # "every 3 s, if state is red, set state to green" -> extra condition
        m = re.match(r"^(?:if|when|while|but\s+only\s+if|only\s+if)\s+(.+?)(?:,|\bthen\b)\s*(.+)$", action_text.strip(), re.I) \
            or re.match(rf"^(?:if|when|while|only\s+if)\s+(.+?)\s+((?:{self._act_verbs()})\w*\b.*)$", action_text.strip(), re.I)
        if m:
            extra = self._cond(m.group(1))
            cond = f"({cond}) && ({extra})" if cond else extra
            action_text = m.group(2)
        owner = None
        actions = []
        quoted: list[str] = []

        def keep(mm):
            quoted.append(mm.group(0))
            return f"\x00{len(quoted) - 1}\x00"
        protected = re.sub(r'"[^"]*"|\'[^\']*\'', keep, action_text)
        for part in re.split(r",|\band\b|\bthen\b|;", protected):
            part = re.sub(r"\x00(\d+)\x00", lambda mm: quoted[int(mm.group(1))], part)
            part = part.strip()
            if not part:
                continue
            for a, who in self._actions(part, state):
                if who and owner is not None and who != owner:
                    # an action on another entity ("destroy gold", "set door angle"): the
                    # interpreter attaches it to that entity
                    a["__who"] = who
                actions.append(a)
                if who and owner is None:
                    owner = who
        if not actions:
            raise ValueError("no actions")
        bd = {"on": trigger, "if": cond, "do": actions}
        if trigger == "collision":
            # the rule lives on whoever is acted upon; the other party is the collision partner
            if owner is None or owner == subject:
                owner, other = subject, partner
            elif owner == partner or (partner and partner.lstrip("*") == owner.lstrip("*")):
                owner, other = partner, subject
            else:
                other = partner
            if other:
                bd["with"] = other.lstrip("*")
        b = validate_behavior(bd)
        return Op("BEHAVIOR", target=owner, behavior=b)

    def _actions(self, part: str, state) -> list[tuple[dict, str | None]]:
        """One action phrase -> actions (macros and taught words expand to several)."""
        words = part.split()
        if words:
            w0 = words[0].lower()
            if w0 in self.vocab.macros:
                mac = self.vocab.macros[w0]
                args = [x for x in words[1:] if x.lower() not in ("it", "the", "by", "with", "to")]
                out = []
                for stmt in mac["body"]:
                    s = stmt
                    for i, p in enumerate(mac["params"]):
                        s = re.sub(rf"\b{re.escape(p)}\b", args[i] if i < len(args) else "0", s)
                    out.append(self._action(s, state))
                return out
            tok = self.vocab.learned.get(w0)
            if tok:
                canon = {"DELETE": "destroy", "CREATE": "spawn", "SET": "set", "MOVE": "move", "SHOW": "print",
                         "RUN": "call", "CONNECT": "send"}.get(tok)
                if canon:
                    part = " ".join([canon] + words[1:])
        return [self._action(part, state)]

    def _trigger(self, t: str, state) -> tuple[str, str | None]:
        low = re.sub(r"^(?:when(?:ever)?|if|once|on)\s+", "", t.lower()).strip()
        m = re.match(r"^every\s+(\d+(?:\.\d+)?)?\s*(milliseconds?|ms|seconds?|secs?|minutes?|mins?|s)?\b\s*(?:,|$)?(.*)$", t.lower())
        if t.lower().startswith("every"):
            if m and (m.group(2) or m.group(1)):
                rest = (m.group(3) or "").strip()
                rest = re.sub(r"^(?:if|when)\s+", "", rest)
                return f"timer:{_to_ms(m.group(1), m.group(2) or 's')}", (self._cond(rest) if rest else None)
            if re.match(r"^every\s+(frame|tick|update)", t.lower()):
                return "update", None
        # key / button presses (checked before 'start' so an entity named "start" works)
        m = re.match(r"^(.+?)\s+and\s+(?:the\s+)?([\w ]+?)\s+(?:is|gets|was)?\s*(?:pressed|clicked|tapped|pushed)$", low)
        if m and not re.search(r"\b(pressed|clicked)\b", m.group(1)):
            key = ident(re.sub(r"\bkey\b|\bbutton\b", "", m.group(2)).strip()) or "any"
            return f"input:{key}", self._cond(m.group(1))
        m = re.match(r"^(?:the\s+)?([\w ]+?)\s+(?:key\s+|button\s+)?(?:is|gets|was)?\s*(pressed|clicked|tapped|pushed|released)$", low) \
            or re.match(r"^(?:pressing|clicking)\s+(?:the\s+)?([\w ]+)$", low) \
            or re.match(r"^(?:the\s+)?(?:player|user|i|you|someone|we)\s+(?:presses|press|hits?|pushes|push|clicks?|taps?|"
                        r"types?|enters?|sends?)\s+(?:the\s+)?(?:key\s+)?[\"']?([\w ]+?)[\"']?(?:\s+key|\s+button)?$", low) \
            or re.match(r"^(?:key\s+)([\w]+)\s+(?:is\s+)?(?:pressed|hit|down)$", low)
        if m:
            key = ident(re.sub(r"\bkey\b|\bbutton\b|\bthe\b", "", m.group(1)).strip()) or "any"
            return f"input:{key}", None
        if re.match(r"^(?:the\s+\w+\s+|it\s+|we\s+)?(?:is\s+|gets\s+)?(?:start|starts|started|starting|startup|"
                    r"begins?|began|boots?|booted|setup|launch(?:es|ed)?|loads?|loaded|powers?\s+on|powered\s+on)$", low) \
                or low in ("the program starts", "the game starts", "start", "startup"):
            return "start", None
        # collisions, with an optional partner: "ghost touches bob", "it collides with a wall", "a coin is touched"
        m = re.match(r"^(.+?)\s+(?:collides?|collided|hits?|touch(?:es)?|touched|bumps?\s+into|runs?\s+into|meets?|"
                     r"overlaps?|reaches?)\s+(?:with\s+)?(.+)$", low)
        if m and (self._ent(m.group(2), state) or self._kind_ref(m.group(2), state)):
            return ("collision", self._who(m.group(2), state), self._who(m.group(1), state)), None
        m = re.match(r"^(.+?)\s+(?:is|gets|was)\s+(?:touched|hit|collected|picked\s+up|bumped|shot)(?:\s+by\s+(.+))?$", low)
        if m:
            return ("collision", self._who(m.group(2), state) if m.group(2) else None, self._who(m.group(1), state)), None
        if re.search(r"\b(collides?|collision|hits?\s+something|touch(?:es)?|contact)\b", low) or low in ("hit", "touched"):
            subj = re.match(r"^(.+?)\s+(?:collides?|hits?|touch(?:es)?)\b", low)
            return ("collision", None, self._who(subj.group(1), state) if subj else None), None
        m = re.match(r"^(?:the\s+)?(?:(\w+)'s\s+)?([\w ]+?)(?:\s+of\s+(?:the\s+)?(\w+))?\s+(?:changes|changed|is\s+changed)$", low)
        if m:
            owner = m.group(1) or m.group(3)
            prop = norm_name(m.group(2))
            if owner and self._exact_entity(owner, state):
                return f"change:{ident(owner)}.{prop}", None
            return f"change:{prop}", None
        m = re.match(r"^(?:the\s+)?(?:\w+\s+)?(?:receives?|gets?)\s+(?:a\s+|any\s+|new\s+)?(?:message|data|value)s?"
                     r"(?:\s+from\s+(?:the\s+)?(\w+))?$", low) \
            or re.match(r"^(?:a\s+|new\s+)?(?:message|data)(?:\s+from\s+(?:the\s+)?(\w+))?(?:\s+(?:arrives|is\s+received|comes(?:\s+in)?))?"
                        r"(?:\s+from\s+(?:the\s+)?(\w+))?$", low) \
            or re.match(r"^(?:the\s+)?(\w+)\s+(?:sends?|publishes|reports?)\s+(?:a\s+|any\s+|new\s+)?(?:message|data|values?|something)$", low)
        if m:
            src = next((g for g in m.groups() if g), None)
            return f"message:{ident(src) if src else 'any'}", None
        return "update", self._cond(low)

    def _kind_ref(self, phrase: str, state) -> str | None:
        """'a cone' / 'any enemy' / 'enemies' -> 'cone' when entities of that kind exist."""
        p = re.sub(r"^(?:a|an|any|every|each|the|some)\s+", "", phrase.strip().lower())
        k = singular(p)
        return k if any(e.kind == k for e in state.entities.values()) else None

    def _who(self, phrase: str | None, state) -> str | None:
        """Entity name, '*kind' for a whole kind ('a coin'), or None for it/unknown."""
        if not phrase:
            return None
        p = phrase.strip().lower()
        if re.match(r"^(?:a|an|any|every|each|all)\s+", p) or (p.endswith("s") and self._kind_ref(p, state)
                                                                and not self._exact_entity(_strip_articles(p), state)):
            k = self._kind_ref(p, state)
            if k:
                return f"*{k}"
        e = self._ent(p, state)
        if e and e != "it":
            return e
        k = self._kind_ref(p, state)
        return f"*{k}" if k and sum(1 for x in state.entities.values() if x.kind == k) > 1 else (k or None)

    def _cond(self, s: str) -> str:
        s = " " + s.lower().strip() + " "
        s = re.sub(r"(\w+)'s\s+(\w+)", r"\1.\2", s)
        s = re.sub(r"\b(\w+)\s+of\s+(?:the\s+)?(\w+)\b", r"\2.\1", s)
        s = self._fuzzy_cond(s)
        # "x is between 20 and 80"
        s = re.sub(r"([\w.]+)\s+(?:is\s+)?between\s+(-?[\d.]+)\s+and\s+(-?[\d.]+)",
                   r" (\1 >= \2 && \1 <= \3) ", s)
        # sensors: "eye detects motion", "motion is detected", "motion stops"
        s = re.sub(r"\b(\w+)\s+(?:detects|senses|sees|notices)\s+(?:a\s+|an\s+|any\s+)?(\w+)\b", r" \1.\2 == true ", s)
        s = re.sub(r"\b(\w+)\s+(?:is\s+)?(?:detected|present|active)\b", r" \1 == true ", s)
        s = re.sub(r"\b(\w+)\s+(?:stops|ends|is\s+gone|disappears|is\s+inactive)\b", r" \1 == false ", s)
        s = re.sub(r"\b(?:reach|reaches|reached|hit|hits)\b", " is ", s)
        reps = [
            (r"\bis\s+(?:greater|more|bigger|higher)\s+than\s+or\s+equal\s+to\b", ">="),
            (r"\bis\s+(?:less|smaller|lower)\s+than\s+or\s+equal\s+to\b", "<="),
            (r"\b(?:is\s+)?at\s+least\b", ">="), (r"\b(?:is\s+)?at\s+most\b", "<="),
            (r"\b(?:is\s+)?(?:greater|more|bigger|higher)\s+than\b|\b(?:is\s+)?(?:above|over)\b|\bexceeds\b|\brises\s+above\b|\bgoes\s+above\b", ">"),
            (r"\b(?:is\s+)?(?:less|smaller|lower)\s+than\b|\b(?:is\s+)?(?:below|under)\b|\b(?:drops|falls|goes)\s+below\b", "<"),
            (r"\bis\s+not\b|\bisn't\b|\bdoes\s+not\s+equal\b|\bnot\s+equal\s+to\b", "!="),
            (r"\bis\s+equal\s+to\b|\bequals\b|\breaches\b|\bbecomes\b|\bhits\b|\bis\b", "=="),
            (r"\bzero\b", "0"), (r"\bthe\b|\bits\b|\bit\b", " "),
        ]
        for a, b in reps:
            s = re.sub(a, f" {b} ", s)
        s = re.sub(r"\s+", " ", s).strip()
        # "is on/off" -> true/false ; colour words / quoted words stay strings
        s = re.sub(r"(==|!=)\s*(?:on|yes)\b", r"\1 true", s)
        s = re.sub(r"(==|!=)\s*(?:off|no)\b", r"\1 false", s)
        s = re.sub(r"(==|!=)\s*([a-z]+)\b(?![.\w])",
                   lambda m: f'{m.group(1)} "{m.group(2)}"' if m.group(2) in COLORS else m.group(0), s)
        if s.endswith(("true", "false")) is False and re.fullmatch(r"[\w.]+", s):
            s = f"{s} == true"   # "if alive" -> alive == true
        # snake-case multiword identifiers: "max speed > 3" -> max_speed > 3
        s = re.sub(r"([A-Za-z_][\w.]*)(?:\s+([A-Za-z_][\w.]*))+(?=\s*(?:[<>=!]|$))",
                   lambda m: "_".join(m.group(0).split()) if not re.search(r"\b(and|or|not)\b", m.group(0)) else m.group(0), s)
        s = re.sub(r"\band\b", "&&", s)
        s = re.sub(r"\bor\b", "||", s)
        return validate_expr(s)

    def _fuzzy_cond(self, s: str) -> str:
        from . import fuzzy
        st = getattr(self, "_state", None)
        H = fuzzy.HEDGE_WORDS

        def rep(m):
            subj, hed, adj = m.group(1), m.group(2), m.group(3)
            if not fuzzy.adjective(adj):
                return m.group(0)
            ent = None
            if st is not None:
                head = subj.split(".")[0]
                if self._exact_entity(head, st):
                    ent = st.resolve(head)
                else:
                    prop = subj.split(".")[-1]
                    focus = st.resolve(None)
                    ent = focus if focus is not None and prop in focus.props else \
                        next((e for e in st.entities.values() if prop in e.props), focus)
            a = fuzzy.adjective(adj)
            if a[0] != "any" and "." not in subj and ent is not None and subj == ent.name.lower():
                subj = ident(ent.name)
            c = fuzzy.condition(subj, adj, hed, ent)
            return f" {c} " if c else m.group(0)
        return re.sub(rf"\b([a-z_][\w.]*)\s+(?:is|gets|becomes|feels|seems|looks)\s+(?:({H})\s+)?([a-z]+)\b(?!\s*(?:than|to|and|or)\b)",
                      rep, s)

    def _action(self, p: str, state) -> tuple[dict, str | None]:
        low = p.lower().strip()
        low = re.sub(r"^(please\s+|then\s+|it\s+should\s+|should\s+)", "", low)

        def owner_of(phrase: str) -> str | None:
            ph = _strip_articles(phrase)
            if ph in ("it", "this", "that", "", "itself"):
                return None
            if self._exact_entity(ph, state):
                return ph
            w = self._who(phrase, state)
            return w if w and w.startswith("*") else None

        # explicit subject + 3rd-person verb: "hero jumps", "the tank fires a bullet", "heater turns on"
        words = low.split()
        vre = re.compile(rf"(?:{self._act_verbs()})", re.I)
        for n in (2, 1):
            if len(words) > n and self._ent(" ".join(words[:n]), state) not in (None, "it"):
                v = words[n]
                base = next((c for c in (v, v[:-1], v[:-2], v[:-3] + "y" if v.endswith("ies") else v)
                             if c and vre.fullmatch(c)), None)
                if base and base != v or (base and n and v == base):
                    subj = self._ent(" ".join(words[:n]), state)
                    rest = " ".join([base] + words[n + 1:])
                    a, who = self._action(rest if re.search(r"\b(it|itself)\b", rest) or len(words) > n + 1
                                          else rest + " it", state)
                    return a, who or subj
        m = re.fullmatch(r"(?:show|display|print|write)\s+(.+?)\s+on\s+(?:the\s+)?(\w+)", p.strip(), re.I)
        if m and owner_of(m.group(2)):
            return {"op": "set", "prop": "text", "value": self._expr_value(m.group(1))}, owner_of(m.group(2))
        m = re.fullmatch(r"(?:rotate|spin|turn)\s+(?:(it|itself|.+?)\s+)?(?:by\s+)?(-?[\d.]+)\s*(?:degrees?|deg|°)?(?:\s+(?:around|on|about)\s+(?:the\s+)?([xyz])(?:\s*axis)?)?", low)
        if m:
            o = owner_of(m.group(1)) if m.group(1) else None
            return {"op": "add", "prop": f"angle{'_' + m.group(3) if m.group(3) and m.group(3) != 'z' else ''}",
                    "value": m.group(2)}, o
        m = re.fullmatch(r"change\s+(?:the\s+|its\s+)?(?:(\w+)'s\s+)?(.+?)\s+to\s+(.+)", p.strip(), re.I)
        if m:
            low = f"set {(m.group(1) + chr(39) + 's ') if m.group(1) else ''}{m.group(2)} to {m.group(3)}".lower()
            p = f"set {(m.group(1) + chr(39) + 's ') if m.group(1) else ''}{m.group(2)} to {m.group(3)}"
        m = re.fullmatch(r"(?:go|switch|move|transition|change)\s+(?:state\s+)?to\s+(?:state\s+)?([a-z_]\w*)", low)
        if m and not self._ent(m.group(1), state):
            return {"op": "set", "prop": "state", "value": f'"{m.group(1)}"'}, None
        m = re.fullmatch(r"(.+?)\s+(on|off)", low)
        if m and owner_of(m.group(1)) and not low.startswith(("turn", "switch")):
            return {"op": "set", "prop": "on", "value": "true" if m.group(2) == "on" else "false"}, owner_of(m.group(1))
        m = re.fullmatch(r"(?:move|go)\s+(?!it\b|itself\b|by\b|up\b|down\b|left\b|right\b|forward|back)(.+?)\s+"
                         r"(up|down|left|right|forward|forwards|back|backward|backwards|by)\b(.*)", low)
        if m and owner_of(m.group(1)):
            a, _ = self._action(f"move it {m.group(2)}{m.group(3)}", state)
            return a, owner_of(m.group(1))
        from . import fuzzy
        m = re.fullmatch(rf"(?:make|get|turn|set)\s+(?:the\s+)?(\w+)\s+(?:({fuzzy.AMOUNT_WORDS})\s+)?([a-z]+er)", low)
        if m and fuzzy.comparative(m.group(3)):
            who = owner_of(m.group(1))
            ent = state.resolve(who) if who else state.resolve(None)
            d = fuzzy.delta_for(ent, m.group(3), m.group(2))
            if d:
                return {"op": "add", "prop": d[0], "value": str(d[1])}, who
        m = re.fullmatch(rf"(?:make|get|turn|set)\s+(?:the\s+)?(\w+)\s+(?:({fuzzy.HEDGE_WORDS})\s+)?([a-z]+)", low)
        if m and fuzzy.adjective(m.group(3)) and fuzzy.adjective(m.group(3))[0] != "any":
            who = owner_of(m.group(1))
            ent = state.resolve(who) if who else state.resolve(None)
            r = fuzzy.set_value(ent, m.group(3), m.group(2))
            if r:
                return {"op": "set", "prop": r[0], "value": str(r[1])}, who
        m = re.fullmatch(r"(?:log|print|say)\s+([a-z_][\w.]*)", low)
        if m and m.group(1) not in ("hello", "hi", "done", "ok", "tick", "ping", "pong"):
            name = m.group(1)
            known = any(name in e.props or name.split(".")[-1] in e.props for e in state.entities.values())
            if known:
                return {"op": "log", "text": f"{name} = {{{name.split('.')[-1]}}}"}, None
        if re.fullmatch(r"(?:destroy|delete|remove|kill)(?:\s+(?:it|itself|self|this))?|die|disappear", low):
            return {"op": "destroy"}, None
        m = re.fullmatch(r"(?:destroy|delete|remove|kill)\s+(.+)", low)
        if m:
            return {"op": "destroy"}, owner_of(m.group(1))
        m = re.fullmatch(r"(?:turn|switch)\s+(on|off)\s+(.+)|(?:turn|switch)\s+(.+?)\s+(on|off)", low)
        if m:
            state_word = m.group(1) or m.group(4)
            who = m.group(2) or m.group(3)
            return {"op": "set", "prop": "on", "value": "true" if state_word == "on" else "false"}, owner_of(who)
        def prop_owner(phrase: str, possessor: str | None = None) -> tuple[str, str | None]:
            """'led1.on' / 'brightness of the led' / "the led's brightness" -> (prop, owner)."""
            ph = _strip_articles(phrase)
            if possessor and owner_of(possessor):
                return norm_name(ph), owner_of(possessor)
            mm = re.fullmatch(r"([\w]+)\.([\w]+)", ph)
            if mm and owner_of(mm.group(1)):
                return norm_name(mm.group(2)), owner_of(mm.group(1))
            mm = re.fullmatch(r"(.+?)\s+of\s+(?:the\s+)?(.+)", ph)
            if mm and owner_of(mm.group(2)):
                return norm_name(mm.group(1)), owner_of(mm.group(2))
            mm = re.fullmatch(r"(\w+)\s+(.+)", ph)            # "bob lives", "door angle"
            if mm and owner_of(mm.group(1)) and not owner_of(mm.group(1)).startswith("*"):
                return norm_name(mm.group(2)), owner_of(mm.group(1))
            mm = re.fullmatch(r"(.+?)\s+(\w+)", ph)           # Persian order: "health player"
            if mm and owner_of(mm.group(2)) and not owner_of(mm.group(2)).startswith("*"):
                return norm_name(mm.group(1)), owner_of(mm.group(2))
            return norm_name(ph), None

        m = re.fullmatch(r"(?:toggle|blink|flash|flip)(?:\s+(.+))?", low)
        if m:
            who = m.group(1) or "it"
            o = owner_of(who)
            if o or who in ("it", "this"):
                return {"op": "toggle", "prop": "on"}, o
            prop, o = prop_owner(who)
            return {"op": "toggle", "prop": prop}, o
        m = re.fullmatch(r"set\s+(?:the\s+)?(?:(\w+)'s\s+)?(.+?)\s+(?:to|=)\s+(.+)", p.strip(), re.I)
        if m:
            prop, o = prop_owner(m.group(2).lower(), m.group(1).lower() if m.group(1) else None)
            return {"op": "set", "prop": prop, "value": self._expr_value(m.group(3))}, o
        m = re.fullmatch(r"(increase|raise|increment|decrease|reduce|lower|decrement)\s+(?:the\s+)?(?:(\w+)'s\s+)?(.+?)(?:\s+by\s+(.+))?", low)
        if m:
            sign = -1 if m.group(1) in ("decrease", "reduce", "lower", "decrement") else 1
            amount = self._expr_value(m.group(4) or "1")
            val = amount if sign > 0 else (f"-{amount}" if re.fullmatch(r"[\d.]+", amount) else f"-({amount})")
            prop, o = prop_owner(m.group(3), m.group(2))
            return {"op": "add", "prop": prop, "value": val}, o
        m = re.fullmatch(r"(add|subtract)\s+(.+?)\s+(?:to|from)\s+(?:the\s+)?(?:(\w+)'s\s+)?(.+)", low)
        if m:
            amount = self._expr_value(m.group(2))
            if m.group(1) == "subtract":
                amount = f"-{amount}" if re.fullmatch(r"[\d.]+", amount) else f"-({amount})"
            prop, o = prop_owner(m.group(4), m.group(3))
            return {"op": "add", "prop": prop, "value": amount}, o
        m = re.fullmatch(r"(?:print|log|say|display|show)\s+(.+)", p.strip(), re.I)
        if m:
            txt = m.group(1).strip()
            if len(txt) >= 2 and txt[0] == txt[-1] and txt[0] in "\"'":
                txt = txt[1:-1].replace(txt[0] * 2, txt[0])
            return {"op": "log", "text": txt}, None
        m = re.fullmatch(r"(?:send|publish|report|transmit)(?:\s+(?:it|data|everything|values|state))?\s+to\s+(.+)", low)
        if m:
            return {"op": "send", "to": _strip_articles(m.group(1)), "fields": []}, None
        m = re.fullmatch(r"(?:send|publish|report|transmit)\s+(.+?)(?:\s+to\s+(.+))?", low)
        if m:
            fields = [norm_name(f) for f in re.split(r",|\band\b|\|", m.group(1))
                      if norm_name(f) and norm_name(f) not in ("it", "data", "state", "everything", "values")]
            to = _strip_articles(m.group(2)) if m.group(2) else None
            return {"op": "send", "to": to, "fields": fields}, None
        m = re.fullmatch(r"(?:move|go)(?:\s+(?:it|itself))?\s+(up|down|left|right|forward|forwards|back|backward|backwards)(?:\s+(?:by\s+)?(.+))?", low)
        if m:
            k = self._expr_value(m.group(2) or "1")
            vec = {"up": ("0", k, "0"), "down": ("0", f"-{k}", "0"), "left": (f"-{k}", "0", "0"),
                   "right": (k, "0", "0"), "forward": ("0", "0", k), "forwards": ("0", "0", k),
                   "back": ("0", "0", f"-{k}"), "backward": ("0", "0", f"-{k}"), "backwards": ("0", "0", f"-{k}")}[m.group(1)]
            return {"op": "move", "x": vec[0], "y": vec[1], "z": vec[2]}, None
        m = re.fullmatch(r"(?:move|go)(?:\s+(?:it|itself))?\s+by\s+(-?[\d.]+)[\s,]+(-?[\d.]+)(?:[\s,]+(-?[\d.]+))?", low)
        if m:
            return {"op": "move", "x": m.group(1), "y": m.group(2), "z": m.group(3) or "0"}, None
        if re.fullmatch(r"jump(?:\s+(?:it|up))?", low):
            return {"op": "move", "x": "0", "y": "1", "z": "0"}, None
        m = re.fullmatch(r"(call|invoke|trigger|play|fire|shoot|spawn|beep|reset|stop|open|close)"
                         r"(?:\s+(.+?))?(?:\s+with\s+(.+))?", low)
        if m:
            verb = m.group(1)
            name = verb if verb not in ("call", "invoke", "trigger") else (m.group(2) or "action")
            if verb == "play" and m.group(2):
                name = "play_" + _strip_articles(m.group(2))
            args = []
            if verb in ("spawn", "fire", "shoot") and m.group(2):
                # "spawn shell", "fire a bullet", "spawn 3 enemies" -> name the template
                what = re.sub(r"^(?:a|an|the|\d+)\s+", "", m.group(2).strip())
                args.append(f'"{singular(ident(what))}"')
            if m.group(3):
                args = [self._expr_value(a) for a in re.split(r",|\band\b|\s+(?=-?\d)", m.group(3)) if a.strip()]
            return {"op": "call", "name": norm_name(name), "args": args}, None
        raise ValueError(f"unknown action '{p}'")

    def _expr_value(self, s: str) -> str:
        s = s.strip()
        # a single quoted literal: 'big', "it's", 'it''s'
        if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'" and s[0] not in s[1:-1].replace(s[0] * 2, ""):
            inner = s[1:-1].replace(s[0] * 2, s[0])
            return '"' + inner.replace('"', "'") + '"' if '"' not in inner else "'" + inner + "'"
        if s.lower() in COLORS:
            return f'"{s.lower()}"'
        s = re.sub(r"\b(the|its|it's)\b", "", s).strip()
        s = re.sub(r"(\w+)'s\s+(\w+)", r"\1.\2", s)
        s = re.sub(r"\b([A-Za-z_]\w*)\s+of\s+([A-Za-z_]\w*)\b", r"\2.\1", s)     # "value of step" -> step.value
        if re.fullmatch(r"[A-Za-z_][\w ]*", s) and " " in s:
            s = "_".join(s.split())
        if s in ("on", "true", "yes"):
            return "true"
        if s in ("off", "false", "no"):
            return "false"
        try:
            return validate_expr(s)
        except ValueError:
            return validate_expr(f'"{s}"')


class RewriteTable:
    """Phrase rewrites (synonyms + learned meanings) applied in ONE pass over the words.
    A dictionary keyed by the first word replaces one regex per rule, so thousands of rules stay fast."""

    def __init__(self, rules=()):
        self.rules = [r for r in rules if r and r[0]]
        self.index: dict[str, list[tuple[list[str], list[str]]]] = {}
        for frm, to, *_ in sorted(self.rules, key=lambda r: -len(r[0])):
            self.index.setdefault(frm[0].lower(), []).append(([w.lower() for w in frm], list(to)))

    def __len__(self):
        return len(self.rules)

    def apply(self, text: str) -> str:
        if not self.index:
            return text
        parts = re.split(r"""("[^"]*"|'[^']*')""", text)
        for i in range(0, len(parts), 2):
            toks = re.findall(r"[A-Za-z0-9_'\-]+|\s+|[^\sA-Za-z0-9_'\-]+", parts[i])
            words = [(j, t) for j, t in enumerate(toks) if not t.isspace()]
            k, changed = 0, False
            pos_out: list[str] = list(toks)
            drop: set[int] = set()
            while k < len(words):
                j, t = words[k]
                cands = self.index.get(t.lower())
                hit = None
                if cands:
                    for frm, to in cands:
                        n = len(frm)
                        if k + n <= len(words) and all(words[k + q][1].lower() == frm[q] for q in range(n)):
                            hit = (n, to)
                            break
                if hit:
                    n, to = hit
                    first = words[k][0]
                    last = words[k + n - 1][0]
                    pos_out[first] = " ".join(to)
                    for q in range(first + 1, last + 1):
                        drop.add(q)
                    changed = True
                    k += n
                else:
                    k += 1
            if changed:
                seg = "".join(t for q, t in enumerate(pos_out) if q not in drop)
                parts[i] = re.sub(r"\s{2,}", " ", seg)
        return "".join(parts).strip()


def apply_rewrites(text: str, rewrites) -> str:
    """Apply phrase rewrites (whole words, case-insensitive, never inside quotes)."""
    if not rewrites:
        return text
    table = rewrites if isinstance(rewrites, RewriteTable) else RewriteTable(rewrites)
    return table.apply(text)


LENGTH_KEYS = {"width", "height", "depth", "length", "diameter", "radius", "thickness", "wall", "size",
               "hole_diameter", "fillet", "rounding", "clearance", "gap", "distance"}


def _length(v: float, unit: str | None):
    """Convert to millimetres (the CAD unit)."""
    f = {"cm": 10, "m": 1000, "in": 25.4, "inch": 25.4, "inches": 25.4}.get((unit or "mm").lower(), 1)
    v = v * f
    return int(v) if float(v).is_integer() else round(v, 3)


def _join(toks: list[Token]) -> str:
    """Rebuild text from tokens, keeping quotes around quoted strings."""
    return " ".join(f'"{t.text}"' if t.tag == "QUOTED" and '"' not in t.text else
                    (f"'{t.text}'" if t.tag == "QUOTED" else t.text) for t in toks)


def _strip_articles(s: str) -> str:
    s = s.strip().strip(".,")
    s = re.sub(r"^(?:the|a|an|my|our|its)\s+", "", s, flags=re.I)
    s = re.sub(r"'s$", "", s)
    return s.strip()


def _to_ms(num: str | None, unit: str | None) -> int:
    n = float(num) if num else 1.0
    u = (unit or "s").lower()
    if u.startswith("ms") or u.startswith("milli"):
        return int(n)
    if u.startswith("min"):
        return int(n * 60000)
    return int(n * 1000)
