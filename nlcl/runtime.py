"""Interactive natural-language runtime (the 'REPL' of the language).

Pipeline for every line:
  text -> semantic tokens (atomic) -> composite tokens (frames)
       -> IR ops (rule parser, or a free LLM in hybrid/llm mode)
       -> interpreter (persistent state, clarification questions)
       -> composer + domain modules (code generation) -> execution
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from . import llm as llmmod
from . import modules as modreg
from .composer import Composer
from .interpreter import Interpreter, Outcome, Question
from .ir import Entity, Op, SystemState, ident
from . import discover
from . import shell as shellmod
from . import weights as wmod
from .knowledge import Knowledge
from .parser import RuleParser, apply_rewrites
from .shell import ShellParser
from .weights import Candidate
from .tokens import ATOMIC, Vocabulary

# letters of non-Latin scripts (Arabic/Persian, Cyrillic, CJK, ...): such input goes to the online agent
NON_LATIN = re.compile(r"[Ͱ-ϿЀ-ԯ֐-ࣿऀ-෿฀-࿿぀-ヿ㐀-鿿가-힯]")

def is_foreign(text: str) -> bool:
    """True when the instruction itself (ignoring quoted text) is written in a non-Latin script."""
    return bool(NON_LATIN.search(re.sub(r'"[^"]*"|\'[^\']*\'', "", text)))


HELP = """\
Talk to the system in natural language, e.g.
  create an object                      (it will ask which domain)
  create a game object called hero with 100 health
  give it speed 5, make it movable and add collision detection
  when health drops below 1, destroy it
  add an esp32 called board
  add a temperature sensor to the board on pin 4
  create a python dashboard
  connect the sensor to the dashboard via serial every 2 seconds
  create a 3d scene called twin  /  connect the dashboard to the twin
  define patrol(d) as move it by d 0 0 then move it by -d 0 0
  build it   /   run it   /   undo   /   show
  fix main.py: it crashes when the list is empty      (any existing file or folder)
  fix the python module: the alert never prints        (generated code of any module)
  check the game code
Commands:
  :help :show :ir :trace on|off :build [dir] :run [domain] :files
  :save [file] :load file :new [name] :undo :domains :vocab
  :fix <file|folder|module|entity>[: what is wrong]   :fix undo   :check <target>
  :teach <word> <TOKEN>   :forget <word>   :accept (accept proposed vocabulary)
  :why (weights of the last sentence)   :threshold <execute-at> [<ask-below>]   :cd <folder>
  :kb [stats | <prolog query> | export file.pl | forget <words>]   :train   :test
  :backend [status|auto|off|<name>]   :mode rules|hybrid|llm   :quit"""


class Session:
    def __init__(self, project: str = "project", backend: str | None = None, mode: str = "hybrid",
                 outdir: str | Path | None = None, vocab_path: str | Path | None = None,
                 trace: bool = False, auto_yes: bool = False, kb: Knowledge | None = None):
        self.modules = modreg.load_modules()
        self._shared_kb = kb
        self.state = SystemState(project)
        self.vocab = Vocabulary(Path(vocab_path) if vocab_path else None)
        self.vocab.register_domains(self.modules)
        self.rules = RuleParser(self.vocab, self.modules)
        self.interp = Interpreter(self.state)
        self.mode = mode
        self.trace = trace
        self.auto_yes = auto_yes
        self.outdir = Path(outdir) if outdir else None
        self.pending: Question | None = None
        self.llm_question: str | None = None
        self.llm_history: list[dict] = []
        self.proposals: list[tuple[str, str]] = []
        self.dirty = True
        # semantic knowledge base (Prolog): ontology, learned meanings, evidence, weighting parameters
        self.home = Path(vocab_path).parent if vocab_path else None
        self.kb = self._shared_kb if self._shared_kb is not None else Knowledge(self.home, self.modules, pretrained=True)
        self.rules.rewrites = self.kb.rewrites()
        self.shellp = ShellParser(self.kb)
        self.cwd = Path.cwd()
        self.confirm: tuple | None = None          # (Candidate, text) waiting for yes / no
        self.last: list[Candidate] = []            # candidates of the last sentence (for :why)
        self.interactive = True
        self.execute_at, self.ask_below = wmod.EXECUTE_AT, wmod.ASK_BELOW
        if discover.restore(self.kb, self.home):           # modules this system grew in earlier sessions
            self.rules.refresh_modules(self.modules)
        self.backend = None
        self.llm = None
        self.set_backend(backend if backend is not None else ("off" if mode == "rules" else None))

    # ------------------------------------------------------------------ setup
    def set_backend(self, name: str | None) -> str:
        if name is not None and not isinstance(name, str) and hasattr(name, "chat"):
            self.backend, self.llm = name, llmmod.LLMParser(name)     # injected backend object
            return f"LLM backend: {name}"
        if name in ("off", "none", "rules"):
            self.backend, self.llm = None, None
            return "LLM disabled (offline rule parser only)"
        try:
            b = llmmod.autodetect(None if name in (None, "auto") else name)
        except llmmod.LLMError as e:
            return str(e)
        self.backend = b
        self.llm = llmmod.LLMParser(b) if b else None
        if b:
            return f"LLM backend: {b}"
        return "no free LLM backend found (offline rule parser only). Try ':backend status'."

    @property
    def build_dir(self) -> Path:
        return self.outdir or (Path.cwd() / "build" / (ident(self.state.name) or "project"))

    def _set_state(self, st: SystemState):
        self.state = st
        self.interp = Interpreter(st)

    # ------------------------------------------------------------------ input
    def handle(self, line: str) -> list[str]:
        line = line.strip()
        if not line or line.startswith("#"):
            return []
        if line.startswith(":"):
            return self.command(line)
        if is_foreign(line):
            return self._foreign(line)
        if self.confirm:
            return self._answer_confirm(line)
        if self.pending:
            q, self.pending = self.pending, None
            norm = self.rules.normalize(line)
            # a full new command (it starts with its own verb) cancels the question;
            # an answer ("a game object", "game", "3d") has no verb
            if q.field == "domain":
                raw_toks = self.vocab.tokenize(line.strip())
                toks = self.vocab.tokenize(norm)
                verbish = {"CREATE", "DELETE", "SET", "CONNECT", "DISCONNECT", "RENAME", "MOVE", "SHOW", "BUILD",
                           "RUN", "UNDO", "DEFINE", "USE", "WHEN", "MACRO"}
                is_cmd = (raw_toks and raw_toks[0].tag in verbish) or self.rules._split_behavior(norm, self.state) \
                    or (toks and toks[0].tag in verbish and not self.interp._domain_from_text(norm.lower(), q.options))
                if is_cmd:
                    kind = q.op.kind or "object"
                    return [f"  (skipped creating the {kind}: no domain chosen)"] + self.handle(line)
            line = norm
            # an answer like "A game object with 100 health" has no verb: read it as a creation
            toks = self.vocab.tokenize(line)
            text = line if any(t.tag == "CREATE" for t in toks[:1]) else f"create {line}"
            fake = [o for o in self.rules.parse(text, self.state).ops if o.action in ("CREATE", "SET")]
            if not fake:
                # a bare domain word ("game"): treat as "a <word> object"
                fake = [o for o in self.rules.parse(f"create a {line} object", self.state).ops if o.action == "CREATE"]
            out = self.interp.answer(q, line, fake)
            self.state.history.append(line)
            return self._outcome(out)
        if self.llm_question and self.llm:
            q, self.llm_question = self.llm_question, None
            return self._via_llm(line, followup_of=q)
        return self.process(line)

    def _foreign(self, line: str) -> list[str]:
        """Non-English input (Persian, Arabic, ...) is handled ONLINE only: the LLM agent rewrites it
        as English NLCL commands, which then run through the normal pipeline."""
        if not self.llm:
            return ["? This language needs the online agent (there is no offline parser for it).",
                    "  Set one free API key and restart, e.g.  GROQ_API_KEY / GEMINI_API_KEY / OPENROUTER_API_KEY,",
                    "  or run Ollama locally. Check with ':backend status'. English works offline."]
        try:
            lines, question = self.llm.to_english(line, self.state, list(self.modules))
        except (llmmod.LLMError, ValueError) as e:
            return [f"? online agent failed: {str(e)[:200]}"]
        out: list[str] = []
        if not lines:
            return [f"System: {question}"] if question else ["? the online agent could not turn that into commands"]
        for en in lines:
            if is_foreign(en):                # never loop on a bad translation
                out.append(f"? agent returned a non-English command, skipped: {en}")
                continue
            out.append(f"  [{self.backend.name}] {en}")
            out += self.handle(en)
        return out

    # ------------------------------------------------------------------ interpretation + weighting
    def interpret(self, text: str) -> list[Candidate]:
        """All interpretations of a sentence, each with its weight (best first). Executes nothing."""
        params = self.kb.params()
        cands: list[Candidate] = []
        pr = self.rules.parse(text, self.state)
        if pr.ops:
            f = wmod.rule_features(pr, text, self.state, self.kb, self.vocab)
            cands.append(Candidate("rules", pr.ops, f, wmod.score(f, params), text, pr))
        sh = self.shellp.parse(apply_rewrites(text, self.rules.rewrites))
        if sh:
            op, info = sh
            f = wmod.shell_features(op, info, self.cwd, self.kb)
            cands.append(Candidate("shell", [op], f, wmod.score(f, params), text, info))
        cands.sort(key=lambda c: -c.score)
        if not cands:
            cands.append(Candidate("rules", [], {"coverage": 0.0}, 0.0, text, pr))
        return cands

    def process(self, text: str) -> list[str]:
        out: list[str] = []
        for lang in discover.languages_in(text, self.kb):
            m = discover.register(self.kb, self.home, lang)
            if m:
                self.rules.refresh_modules(self.modules)
                out.append(f"  discovered a new module: '{lang}' (no hand-written module existed; registered in the KB)")
        cands = self.interpret(text)
        self.last = cands
        best = cands[0]
        pr = best.detail if best.source == "rules" else next((c.detail for c in cands if c.source == "rules"), None)
        if pr is None:
            pr = self.rules.parse(text, self.state)
        if self.trace:
            out += self._trace_tokens(pr)
            for c in cands:
                out.append(f"  weight {c.score:.2f} [{c.source}] {c.summary()}   ({wmod.explain(c.features, self.kb.params())})")
        use_llm = self.llm is not None and (self.mode == "llm" or (self.mode == "hybrid" and best.score < self.execute_at))
        if use_llm:
            res = self._via_llm(text, rule_result=pr)
            if res is not None:
                return out + res
        if best.source == "shell" and best.ops:
            return out + self._shell(best)
        if not pr.ops:
            fix = discover.repair_sentence(self, text)
            if fix:
                new_text, old, new, w = fix
                cand = self.interpret(new_text)[0]
                if self.auto_yes:
                    self._learn_word(old, new)
                    return out + [f"  read '{old}' as '{new}' (weight {w:.2f}) and learned it"] + self.process(new_text)
                if self.interactive:
                    self.confirm = (cand, new_text, ("learn", old, new))
                    return out + [f"? Did you mean '{new}' for '{old}'?  ->  {cand.summary()}  (weight {w:.2f})",
                                  "  yes = do it and remember the word, no = cancel"]
            msg = "; ".join(pr.errors) if pr.errors else "I could not understand that."
            out.append(f"? {msg}")
            if pr.unknown:
                out.append(f"  unknown words: {', '.join(dict.fromkeys(pr.unknown))}")
            if not self.llm:
                out.append("  tip: teach a word with ':teach <word> <TOKEN>', or enable a free LLM (':backend status').")
            return out
        if pr.errors:
            out += [f"? {e}" for e in pr.errors]
        out += [f"  note: {n}" for n in pr.notes]
        if pr.unknown and not self.llm:
            out.append(f"  (treated as new names: {', '.join(dict.fromkeys(pr.unknown))})")
        d = wmod.decision(best.score, self.execute_at, self.ask_below)
        if d == "ask":
            fix = discover.repair_sentence(self, text)
            if fix:
                new_text, old, new, w = fix
                cand = self.interpret(new_text)[0]
                if self.auto_yes:
                    self._learn_word(old, new)
                    return out + [f"  read '{old}' as '{new}' (weight {w:.2f}) and learned it"] + self.process(new_text)
                if self.interactive:
                    self.confirm = (cand, new_text, ("learn", old, new))
                    return out + [f"? Did you mean '{new}' for '{old}'?  ->  {cand.summary()}  (weight {w:.2f})",
                                  "  yes = do it and remember the word, no = cancel"]
        if d == "ask" and not self.auto_yes:
            if not self.interactive:
                return out + [f"  skipped (weight {best.score:.2f} is too low): understood as {best.summary()}",
                              "  (rewrite the sentence, or run the script with --yes to accept)"]
            self.confirm = (best, text)
            return out + [f"? Not sure (weight {best.score:.2f}; {wmod.explain(best.features, self.kb.params())}).",
                          f"  I understood: {best.summary()}",
                          "  Do it? (yes / no - or just type the sentence differently)"]
        if d == "execute_shown":
            out.append(f"  (weight {best.score:.2f}) understood as: {best.summary()}")
        return out + self._execute(pr.ops, text)

    # ------------------------------------------------------------------ confirmation and learning
    def _learn_word(self, old: str, new: str) -> None:
        self.kb.add_rewrite([old], [new], 1)
        self.kb.save()
        self.rules.rewrites = self.kb.rewrites()

    def _answer_confirm(self, line: str) -> list[str]:
        cand, text, *extra = self.confirm
        self.confirm = None
        low = line.strip().lower().rstrip(".!")
        yes = re.fullmatch(r"y|yes|yeah|yep|ok|okay|sure|do it|go|go ahead|apply|run it|confirm|correct|right", low)
        no = re.fullmatch(r"n|no|nope|cancel|stop|abort|don't|dont|wrong|never mind|nevermind", low)
        if not yes and not no:
            self._feedback(cand, False)
            return ["  (cancelled the previous instruction)"] + self.handle(line)
        self._feedback(cand, bool(yes))
        if no:
            return ["  cancelled - nothing was changed. Rephrase it and I will try again."]
        if extra and extra[0] and extra[0][0] == "learn":
            _, old, new = extra[0]
            self._learn_word(old, new)
            return [f"  learned: '{old}' means '{new}' (saved in the knowledge base)"] + self._execute(cand.ops, text) \
                if cand.source != "shell" else self._shell_commit(cand)
        if cand.source == "shell":
            return self._shell_commit(cand)
        return self._execute(cand.ops, text)

    def _feedback(self, cand: Candidate, ok: bool) -> None:
        """A yes / no from the user is evidence: it updates the reliability counts in the KB."""
        if cand.source == "shell" and cand.ops:
            self.kb.record(f"intent({cand.ops[0].props['intent']})", ok)
        elif cand.source == "rules" and cand.detail is not None:
            for _c, frame, _t in cand.detail.clauses:
                if frame:
                    self.kb.record(f"frame({frame.lower()})", ok)
        self.kb.save()
        if self.home:
            try:
                with open(self.home / "examples.jsonl", "a", encoding="utf-8") as f:
                    f.write(json.dumps({"text": cand.text, "source": cand.source, "ok": ok,
                                        "ops": [o.to_dict() for o in cand.ops]}, ensure_ascii=False) + "\n")
            except OSError:
                pass

    # ------------------------------------------------------------------ terminal
    def _shell(self, cand: Candidate) -> list[str]:
        op = cand.ops[0]
        intent = op.props["intent"]
        out = [f"  [{intent}] weight {cand.score:.2f}",
               f"    bash:       {shellmod.render(op, 'bash')}",
               f"    powershell: {shellmod.render(op, 'powershell')}"]
        safety = (self.kb.shell_intent(intent) or ("read", ""))[0]
        try:
            if safety == "read":
                text = shellmod.execute(op, self.cwd, self.kb)
                self._log_shell(cand)
                body = text.splitlines()
                return out + ["    " + ln for ln in body[:60]] + (["    ..."] if len(body) > 60 else []) \
                    + ([] if body else ["    (nothing)"])
            text, d = shellmod.sandbox(op, self.cwd, self.kb)
        except shellmod.ShellError as e:
            self.kb.record(f"intent({intent})", False)
            return out + [f"? {e}"]
        out.append("  simulated in a sandbox copy; changes:")
        out += d.lines()
        if d.empty:
            return out + ["  nothing would change - not applied"]
        if self.kb.needs_confirmation(intent) and not self.auto_yes:
            if not self.interactive:
                return out + ["  not applied: this changes or deletes files (run the script with --yes to allow)"]
            self.confirm = (cand, cand.text)
            return out + ["? Apply these changes to the real folder? (yes / no)"]
        return out + self._shell_commit(cand)

    def _shell_commit(self, cand: Candidate) -> list[str]:
        try:
            text = shellmod.execute(cand.ops[0], self.cwd, self.kb)
        except shellmod.ShellError as e:
            return [f"? {e}"]
        self._log_shell(cand)
        self.dirty = True
        return ["  done" + (f": {text}" if text else "")]

    def _log_shell(self, cand: Candidate) -> None:
        op = cand.ops[0]
        self.state.shell_log.append({"text": cand.text, "props": dict(op.props),
                                     "bash": shellmod.render(op, "bash"),
                                     "powershell": shellmod.render(op, "powershell")})
        self.state.history.append(cand.text)

    def _via_llm(self, text: str, rule_result=None, followup_of: str | None = None) -> list[str] | None:
        out: list[str] = []
        msgs = list(self.llm_history)
        try:
            ops, question = self.llm.parse(text, self.state, list(self.modules), self._vocab_brief(), msgs)
        except llmmod.LLMError as e:
            out.append(f"  (LLM unavailable: {str(e)[:160]}; using offline parser)")
            if rule_result and rule_result.ops:
                return out + self._execute(rule_result.ops, text)
            return out + [f"? {'; '.join(rule_result.errors) if rule_result and rule_result.errors else 'I could not understand that.'}"]
        self.llm_history += [{"role": "user", "content": text},
                             {"role": "assistant", "content": json.dumps({"ops": [o.to_dict() for o in ops], "question": question}, ensure_ascii=False)}]
        self.llm_history = self.llm_history[-8:]
        if self.trace:
            out.append(f"  [llm:{self.backend.name}] " + "; ".join(str(o) for o in ops))
        if question and not ops:
            self.llm_question = question
            return out + [f"System: {question}"]
        # teacher -> student: turn what the online agent understood into an offline rule in the KB
        if ops and rule_result is not None and not rule_result.confident:
            try:
                rule = discover.distill(self, text, ops)
            except Exception:
                rule = None
            if rule:
                frm, to = rule
                self.kb.add_rewrite(frm, to, 1)
                self.kb.save()
                self.rules.rewrites = self.kb.rewrites()
                out.append(f"  learned offline: '{' '.join(frm)}' -> '{' '.join(to) or '(ignore)'}' "
                           "(next time this works without the online agent)")
        # adaptive vocabulary: words the rule parser did not know
        if rule_result is not None:
            out += self._propose_vocab(rule_result, text)
        return out + self._execute(ops, text)

    def _propose_vocab(self, pr, text) -> list[str]:
        out = []
        cands = [w for w in dict.fromkeys(pr.unknown) if re.fullmatch(r"[A-Za-z؀-ۿ\-]{3,}", w)]
        verbs = [t.text for _, frame, toks in pr.clauses if frame is None for t in toks[:1] if t.tag == "WORD"]
        for w in (verbs + cands)[:2]:
            if w.lower() in self.vocab.learned or any(p[0] == w.lower() for p in self.proposals):
                continue
            prop = self.llm.propose_meaning(w, text, sorted(ATOMIC))
            tok = (prop or {}).get("token")
            if tok and str(tok).upper() in ATOMIC and not self.vocab.atomic(w):
                self.proposals.append((w.lower(), str(tok).upper()))
                out.append(f"  (vocabulary: '{w}' looks like {str(tok).upper()} - type :accept to learn it)")
        if self.auto_yes and self.proposals:
            out += self.command(":accept")
        return out

    def _vocab_brief(self) -> str:
        parts = [f"{w}={t}" for w, t in self.vocab.learned.items()]
        parts += [f"{n}({', '.join(m['params'])}): {' ; '.join(m['body'])}" for n, m in self.vocab.macros.items()]
        return ", ".join(parts)

    def _execute(self, ops: list[Op], text: str) -> list[str]:
        out: list[str] = []
        if self.trace:
            out.append("  IR: " + " ; ".join(str(o) for o in ops))
        res = self.interp.apply_all(ops)
        if res.changed:
            self.state.history.append(text)
            self.dirty = True
        return out + self._outcome(res)

    def _outcome(self, res: Outcome) -> list[str]:
        out = [f"  {m}" for m in res.messages]
        for m in res.meta:
            out += self._meta(m)
        if res.question:
            self.pending = res.question
            out.append(f"System: {res.question.text}")
        elif res.followup:
            out.append(f"System: {res.followup}")
        return out

    # ------------------------------------------------------------------ meta
    def _meta(self, op: Op) -> list[str]:
        a = op.action
        if a == "SHOW":
            what = (op.text or "").lower()
            if "ir" in what.split() or "json" in what:
                return [self.state.to_json(indent=2)]
            if "vocab" in what or "word" in what:
                return [self.vocab.describe()]
            return [self.state.summary()]
        if a == "UNDO":
            n = sum(1 for _ in range(max(1, op.count)) if self.state.undo())
            return [f"  undone {n} step(s)" if n else "  nothing to undo"]
        if a == "BUILD":
            return self.build()
        if a == "RUN":
            return self.run(op.domain)
        if a == "DEFINE":
            return self.define(op)
        if a == "FIX":
            return self.fix(op.text or "", check_only=op.props.get("mode") == "check")
        if a == "HELP":
            return [HELP]
        return []

    def define(self, op: Op) -> list[str]:
        word, body = op.name, (op.text or "").strip()
        params = op.props.get("params", [])
        single = re.fullmatch(r"[\"']?([\w؀-ۿ\-]+)[\"']?", body)
        try:
            if single and not params and (single.group(1).upper() in ATOMIC or self.vocab.atomic(single.group(1))):
                tok = self.vocab.learn_word(word, single.group(1))
                return [f"  learned: '{word}' = {tok}"]
            stmts = [s.strip() for s in re.split(r"\bthen\b|;", body) if s.strip()]
            self.vocab.learn_macro(word, params, stmts, self._validate_macro)
            return [f"  learned command '{word}'" + (f"({', '.join(params)})" if params else "")
                    + f": {len(stmts)} step(s), validated by dry run"]
        except ValueError as e:
            return [f"? {e}"]

    def _validate_macro(self, stmts: list[str], params: list[str]) -> list[str]:
        clone = self.state.clone()
        if not clone.focus:
            e = Entity("probe_0", "probe", "python", "object", {})
            clone.entities[e.id] = e
            clone.focus = e.id
        interp = Interpreter(clone)
        errors = []
        for s in stmts:
            for p in params:
                s = re.sub(rf"\b{re.escape(p)}\b", "1", s)
            pr = self.rules.parse(s, clone)
            if not pr.ops or pr.errors:
                errors.append(f"cannot understand step '{s}'" + (f" ({'; '.join(pr.errors)})" if pr.errors else ""))
                continue
            res = interp.apply_all(pr.ops)
            errors += [m for m in res.messages if m.startswith("error")]
            if res.question:
                errors.append(f"step '{s}' is ambiguous: {res.question.text}")
        return errors

    # ------------------------------------------------------------------ bug fixing
    def _fix_target(self, text: str) -> tuple[Path | None, str, list[str], list[str] | None, list[str]]:
        """Resolve what to fix from free text -> (path, description, focus names, run command, notes)."""
        notes: list[str] = []
        desc = text
        m = re.search(r"(?::\s+|\s-\s|\sbecause\s|\s-{2}\s)(.+)$", text)     # ':' needs a space (not C:\path)
        head = text[:m.start()] if m else text
        if m:
            desc = m.group(1).strip()
        head = re.sub(r"^(?:the\s+)?(?:bugs?|errors?|problems?|issues?|crash(?:es)?)\s*(?:in|with|of)?\s*", "", head.strip(), flags=re.I)
        head = re.sub(r"^(?:the\s+|my\s+|this\s+)", "", head, flags=re.I)
        want_run = bool(re.search(r"\b(run|runs|running|crash|crashes|traceback|exception|runtime)\b", text, re.I))
        # 1. an existing file or folder (code written earlier, anywhere on disk)
        cands = re.findall(r'"([^"]+)"|\'([^\']+)\'|(\S+)', head)
        for q1, q2, w in cands:
            tok = (q1 or q2 or w).rstrip(",.;:")
            if not tok or tok.lower() in ("it", "code", "everything", "all", "project", "system", "program", "app"):
                continue
            for base in (Path.cwd(), self.build_dir):
                p = (base / tok) if not Path(tok).is_absolute() else Path(tok)
                if p.exists() and (p.is_dir() or p.suffix):
                    run = None
                    if want_run and p.is_file() and p.suffix == ".py":
                        run = [sys.executable, "-u", p.name]
                    return p, desc if m else re.sub(re.escape(tok), "", desc).strip(" :,-"), [], run, notes
        # 2. a module (domain) or an entity of the current system -> its generated code
        low = head.lower()
        dom, focus = None, []
        for w in re.findall(r"[\w+#.\-]+", low):
            mod = modreg.get(w)
            if mod and mod.name in self.state.domains():
                dom = mod.name
                break
        if not dom:
            for w in re.findall(r"[\w]+", low):
                e = self.state.resolve(w) if self.rules._exact_entity(w, self.state) else None
                if e:
                    dom, focus = e.domain, [e.ident, e.class_name]
                    break
        if not self.state.entities:
            return None, desc, [], None, ["nothing to fix: give a file or folder (e.g. fix main.py: it crashes), or create something first"]
        if self.dirty or not self.build_dir.exists():
            notes += [ln.strip() for ln in self.build()[:1]]
        path = self.build_dir / dom if dom else self.build_dir
        run = None
        if dom == "python" and (path / "main.py").exists():
            run = [sys.executable, "-u", "main.py", "--simulate", "--seconds", "2", "--no-hub"]
        return path, desc if (m or not dom) else "", focus, run, notes

    def fix(self, text: str, check_only: bool = False) -> list[str]:
        from .fixer import Fixer, diagnose
        if text.strip().lower() in ("undo", "revert"):
            root = getattr(self, "_last_fix_root", None)
            if not root:
                return ["  no fix to undo in this session"]
            self.state.patches = [p for p in self.state.patches if p.get("run") != getattr(self, "_last_fix_id", None)]
            return Fixer.undo(root)
        path, desc, focus, run, notes = self._fix_target(text)
        out = [f"  note: {n}" for n in notes]
        if path is None:
            return out
        if check_only:
            diags = diagnose(path, run)
            out.append(f"  checked {path}: " + (f"{len(diags)} problem(s)" if diags else "no problems found by the available tools"))
            return out + [f"    {d}" for d in diags[:25]]
        rep = Fixer(self.backend).fix(path, desc, run, focus)
        out += rep.lines()
        if rep.changed:
            root = path if path.is_dir() else path.parent
            self._last_fix_root = root
            self._last_fix_id = len(self.state.patches) + 1
            # fixes to generated code are remembered so a rebuild does not lose them
            try:
                base = root.resolve().relative_to(self.build_dir.resolve())
                for ed in rep.edits:
                    self.state.patches.append({"file": str(base / ed["file"]).replace("\\", "/"),
                                               "search": ed["search"], "replace": ed["replace"],
                                               "run": self._last_fix_id})
            except ValueError:
                pass
        return out

    def build(self, outdir: Path | None = None) -> list[str]:
        if not self.state.entities:
            return ["  nothing to build yet"]
        out_path = Path(outdir) if outdir else self.build_dir
        comp = Composer(self.state, out_path, self.llm)
        written, warnings = comp.build()
        self.dirty = False
        out = [f"  built {sum(len(v) for v in written.values())} file(s) into {out_path}"]
        for dom, files in written.items():
            out.append(f"    {dom}: {', '.join(files)}")
        out.append("    + system.json (IR), SYSTEM.md (architecture & how to run)")
        out += [f"  warning: {w}" for w in warnings]
        return out

    def run(self, domain: str | None = None) -> list[str]:
        out = self.build()
        comp = Composer(self.state, self.build_dir, self.llm)
        results = comp.run(domain)
        if not results:
            out.append("  nothing runnable locally (see SYSTEM.md for the free tools for each module)")
        for dom, r in results:
            out.append(f"  --- run {dom}: {r.command}  [{'ok' if r.ok else 'FAILED'}]")
            out += ["    " + ln for ln in r.output.splitlines()[-40:]]
        return out

    # ------------------------------------------------------------------ commands
    def command(self, line: str) -> list[str]:
        parts = line[1:].split(maxsplit=2)
        if not parts:
            return [HELP]
        cmd, args = parts[0].lower(), parts[1:]
        if cmd in ("h", "help", "?"):
            return [HELP]
        if cmd in ("q", "quit", "exit"):
            raise EOFError
        if cmd in ("show", "state", "s"):
            return [self.state.summary()]
        if cmd == "ir":
            return [self.state.to_json(indent=2)]
        if cmd == "trace":
            self.trace = not args or args[0].lower() in ("on", "1", "true")
            return [f"  trace {'on' if self.trace else 'off'}"]
        if cmd == "build":
            return self.build(Path(args[0]) if args else None)
        if cmd == "run":
            return self.run(args[0] if args else None)
        if cmd in ("fix", "check"):
            return self.fix(line[1 + len(cmd):].strip(), check_only=(cmd == "check"))
        if cmd == "files":
            d = self.build_dir
            if not d.exists():
                return ["  not built yet"]
            return [f"  {p.relative_to(d)}" for p in sorted(d.rglob("*")) if p.is_file()]
        if cmd == "save":
            p = Path(args[0] if args else f"{ident(self.state.name) or 'project'}.nlcl.json")
            p.write_text(self.state.to_json(indent=2), encoding="utf-8")
            return [f"  saved to {p}"]
        if cmd == "load":
            if not args:
                return ["  usage: :load file"]
            self._set_state(SystemState.from_json(Path(args[0]).read_text(encoding="utf-8")))
            return [f"  loaded {self.state.name}: {len(self.state.entities)} entities"]
        if cmd == "new":
            self._set_state(SystemState(args[0] if args else "project"))
            self.pending = None
            return [f"  new system '{self.state.name}'"]
        if cmd == "name":
            if args:
                self.state.name = " ".join(args)
            return [f"  system name: {self.state.name}"]
        if cmd == "undo":
            return ["  undone" if self.state.undo() else "  nothing to undo"]
        if cmd == "domains":
            out = []
            for m in self.modules.values():
                out.append(f"  {m.name:<10} {m.title}")
                out.append(f"             kinds: {', '.join(list(m.kinds)[:12])}")
            return out
        if cmd == "vocab":
            return [self.vocab.describe()]
        if cmd == "teach":
            if len(args) < 2:
                return ["  usage: :teach <word> <TOKEN or known word>"]
            try:
                return [f"  learned: '{args[0]}' = {self.vocab.learn_word(args[0], args[1])}"]
            except ValueError as e:
                return [f"? {e}"]
        if cmd == "forget":
            return [f"  forgot {args[0]}" if args and self.vocab.forget(args[0]) else "  not found"]
        if cmd == "accept":
            out = []
            for w, t in self.proposals:
                try:
                    self.vocab.learn_word(w, t)
                    out.append(f"  learned: '{w}' = {t}")
                except ValueError as e:
                    out.append(f"? {e}")
            self.proposals = []
            return out or ["  nothing proposed"]
        if cmd == "backend":
            if not args or args[0] == "status":
                cur = f"current: {self.backend}" if self.backend else "current: none (offline rules)"
                return [f"  {cur}", llmmod.status()]
            return ["  " + self.set_backend(args[0])]
        if cmd == "why":
            if not self.last:
                return ["  nothing interpreted yet"]
            out = []
            for c in self.last:
                out.append(f"  {c.score:.2f} [{c.source}] {c.summary()}")
                out.append("       " + ", ".join(f"{k}={v:.2f}" if isinstance(v, float) else f"{k}={v}"
                                              for k, v in c.features.items()))
            return out
        if cmd == "kb":
            rest = line[1 + len(cmd):].strip()
            if not rest or rest == "stats":
                return [f"  {k}: {v}" for k, v in self.kb.stats().items()] + \
                    [f"  learned file: {self.kb.learned_path or '(memory only)'}"]
            if rest.startswith("export"):
                p = Path(rest[6:].strip() or "nlcl_kb.pl")
                self.kb.export(p)
                return [f"  exported the whole knowledge base to {p} (load it with: swipl {p})"]
            if rest.startswith("forget"):
                words = rest[6:].split()
                return ["  removed" if words and self.kb.remove_rewrite(words) else "  not found"] + \
                    ([] if not self.kb.save() else [])
            try:
                ans = self.kb.query(rest.rstrip("."), limit=40)
            except Exception as e:
                return [f"? {e}"]
            if not ans:
                return ["  false."]
            return ["  " + (", ".join(f"{k} = {v}" for k, v in a.items()) or "true.") for a in ans]
        if cmd in ("train", "test"):
            from .learn import evaluate, load_corpus, train as do_train
            ex = load_corpus(args[0] if args else None)
            if cmd == "train":
                r = do_train(self.kb, ex, log=lambda s: None)
                self.rules.rewrites = self.kb.rewrites()
                return [f"  trained on {r['train_sentences']} sentences in {r['seconds']} s; "
                        f"{r['rules_learned']} new rule(s); saved to {self.kb.learned_path or 'memory'}"] + \
                    [f"    {k}: {v}" for k, v in r["train_metrics"].items()]
            m, _ = evaluate(self.kb, ex, "test")
            return ["  held-out sentences:"] + [f"    {k}: {v}" for k, v in m.items()]
        if cmd == "cd":
            p = (self.cwd / args[0]).resolve() if args else Path.cwd()
            if not p.is_dir():
                return [f"? no such folder: {p}"]
            self.cwd = p
            return [f"  terminal folder: {p}"]
        if cmd == "threshold":
            if args:
                try:
                    self.execute_at = float(args[0])
                    if len(args) > 1:
                        self.ask_below = float(args[1])
                except ValueError:
                    return ["  usage: :threshold <execute-at> [<ask-below>]"]
            return [f"  execute at weight >= {self.execute_at}, ask below {self.ask_below}"]
        if cmd == "mode":
            if args and args[0] in ("rules", "hybrid", "llm"):
                self.mode = args[0]
            return [f"  mode: {self.mode}"]
        return [f"? unknown command :{cmd} (see :help)"]

    # ------------------------------------------------------------------ trace
    def _trace_tokens(self, pr) -> list[str]:
        out = []
        for text, frame, toks in pr.clauses:
            out.append(f"  tokens: {' '.join(repr(t) for t in toks)}")
            out.append(f"  composite: {frame}")
        return out
