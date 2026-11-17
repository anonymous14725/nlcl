"""Smart bug fixing for any module and any existing code.

    diagnose  -> real tools per file type (offline):  Python compile, node --check (JS / HTML scripts),
                 JSON parse, C/C++ (g++/clang++), Verilog (iverilog/verilator), Arduino (arduino-cli),
                 OpenSCAD, GDScript (godot), C# (dotnet), plus bracket balance for every C-like file.
    fix       -> online agent loop: code + diagnostics + the user's description go to the LLM, which
                 returns search/replace edits; edits are applied with a backup, the code is re-checked,
                 and a round that makes things worse is rolled back.  Works on files the user wrote
                 earlier as well as on generated output.

Without an LLM backend only ``diagnose`` is available (problems are located and reported, not fixed).
"""
from __future__ import annotations

import difflib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

SOURCE_EXT = {".py", ".js", ".mjs", ".ts", ".html", ".htm", ".css", ".json", ".c", ".h", ".cpp", ".hpp", ".cc",
              ".ino", ".cs", ".gd", ".v", ".sv", ".scad", ".java", ".kt", ".swift", ".go", ".rs", ".sh", ".ps1",
              ".bat", ".toml", ".ini", ".yml", ".yaml", ".md", ".txt", ".nl", ".tscn", ".godot", ".cmake"}
CODE_EXT = SOURCE_EXT - {".md", ".txt"}
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".nlcl_backup", ".venv", "venv", "dist", "build_out", ".pio"}
MAX_FILE = 60_000          # bytes sent per file
MAX_TOTAL = 160_000        # bytes sent per round


@dataclass
class Diag:
    file: str
    line: int | None
    message: str
    tool: str
    severity: str = "error"

    def __str__(self):
        loc = f"{self.file}:{self.line}" if self.line else self.file
        return f"{loc}: {self.severity}: {self.message} [{self.tool}]"


@dataclass
class FixReport:
    ok: bool
    summary: str
    before: list[Diag] = field(default_factory=list)
    after: list[Diag] = field(default_factory=list)
    changed: dict[str, str] = field(default_factory=dict)       # rel path -> unified diff
    edits: list[dict] = field(default_factory=list)             # applied {file, search, replace}
    backup: str | None = None
    notes: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = [f"  {self.summary}"]
        out += [f"  note: {n}" for n in self.notes]
        if self.before:
            out.append(f"  problems found: {len(self.before)}")
            out += [f"    {d}" for d in self.before[:12]]
        for f, d in self.changed.items():
            out.append(f"  --- fixed {f}")
            out += ["    " + ln for ln in d.splitlines()[2:42]]
        if self.changed:
            out.append(f"  remaining problems: {len(self.after)}")
            out += [f"    {d}" for d in self.after[:12]]
        if self.backup:
            out.append(f"  originals saved in {self.backup}  (':fix undo' restores them)")
        return out


# --------------------------------------------------------------------------
# diagnostics
# --------------------------------------------------------------------------

def _run(cmd: list[str], cwd: Path | None = None, timeout: int = 40) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", errors="replace")
        return p.returncode, (p.stdout + "\n" + p.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"
    except OSError as e:
        return 127, str(e)


def _tool(*names: str) -> str | None:
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return None


def _line_of(text: str, pat: str = r"(?:line |:)(\d+)") -> int | None:
    m = re.search(pat, text)
    return int(m.group(1)) if m else None


def check_python(path: Path, rel: str) -> list[Diag]:
    src = path.read_text(encoding="utf-8", errors="replace")
    try:
        compile(src, str(path), "exec")
    except SyntaxError as e:
        return [Diag(rel, e.lineno, f"{e.msg}: {(e.text or '').strip()}", "python")]
    except ValueError as e:
        return [Diag(rel, None, str(e), "python")]
    return []


def _node_check(code: str, rel: str, module: bool, offset: int = 0, tool: str = "node") -> list[Diag]:
    node = _tool("node")
    if not node:
        return []
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / ("x.mjs" if module else "x.cjs")
        f.write_text(code, encoding="utf-8")
        rc, out = _run([node, "--check", str(f)])
    if rc == 0:
        return []
    m = re.search(r":(\d+)\s*\n", out)
    line = int(m.group(1)) + offset if m else None
    msg = next((ln for ln in out.splitlines() if "Error" in ln), out.splitlines()[-1] if out else "syntax error")
    return [Diag(rel, line, msg.strip(), tool)]


def check_js(path: Path, rel: str) -> list[Diag]:
    src = path.read_text(encoding="utf-8", errors="replace")
    module = path.suffix == ".mjs" or bool(re.search(r"^\s*(import|export)\s", src, re.M))
    return _node_check(src, rel, module)


def check_html(path: Path, rel: str) -> list[Diag]:
    src = path.read_text(encoding="utf-8", errors="replace")
    out: list[Diag] = []
    for m in re.finditer(r"<script\b([^>]*)>(.*?)</script>", src, re.S | re.I):
        attrs, code = m.group(1), m.group(2)
        if "src=" in attrs or re.search(r"type\s*=\s*[\"'](?:application/json|importmap|text/template)", attrs, re.I):
            continue
        if not code.strip():
            continue
        offset = src[:m.start(2)].count("\n")
        out += _node_check(code, rel, "module" in attrs, offset, "node(html)")
    if src.lower().count("<script") != src.lower().count("</script>"):
        out.append(Diag(rel, None, "unbalanced <script> tags", "html"))
    return out


def check_json(path: Path, rel: str) -> list[Diag]:
    try:
        json.loads(path.read_text(encoding="utf-8-sig", errors="replace"))
    except ValueError as e:
        return [Diag(rel, getattr(e, "lineno", None), str(e), "json")]
    return []


def check_c(path: Path, rel: str) -> list[Diag]:
    cc = _tool("g++", "clang++")
    if not cc or path.suffix == ".ino":
        return check_brackets(path, rel)
    inc = [f"-I{p}" for p in {path.parent, path.parent.parent / "include", path.parent / "include"} if p.exists()]
    rc, out = _run([cc, "-std=c++17", "-fsyntax-only", *inc, str(path)])
    diags = []
    for ln in out.splitlines():
        m = re.match(r"(.+?):(\d+):(?:\d+:)?\s*(error|warning):\s*(.+)", ln)
        if m and m.group(3) == "error":
            diags.append(Diag(rel, int(m.group(2)), m.group(4), Path(cc).stem))
    return diags or ([Diag(rel, None, out[:300], Path(cc).stem)] if rc not in (0,) and out else [])


def check_verilog(path: Path, rel: str) -> list[Diag]:
    iv, vl = _tool("iverilog"), _tool("verilator")
    out_d: list[Diag] = []
    if iv:
        rc, out = _run([iv, "-g2005", "-t", "null", "-y", str(path.parent), str(path)])
        for ln in out.splitlines():
            m = re.match(r"(.+?):(\d+):\s*(.*error.*|syntax error.*)", ln)
            if m:
                out_d.append(Diag(rel, int(m.group(2)), m.group(3), "iverilog"))
    elif vl:
        rc, out = _run([vl, "--lint-only", "-Wno-fatal", f"-y", str(path.parent), str(path)])
        for ln in out.splitlines():
            m = re.match(r"%Error[^:]*:\s*(.+?):(\d+):(?:\d+:)?\s*(.+)", ln)
            if m:
                out_d.append(Diag(rel, int(m.group(2)), m.group(3), "verilator"))
    else:
        out_d += check_brackets(path, rel)
        src = _strip_comments(path.read_text(encoding="utf-8", errors="replace"), "//")
        for a, b in (("module", "endmodule"), ("begin", "end"), ("case", "endcase"), ("function", "endfunction")):
            na = len(re.findall(rf"\b{a}\b", src)) if a != "case" else len(re.findall(r"\bcase[xz]?\b", src))
            nb = len(re.findall(rf"\b{b}\b", src))
            if a == "begin":
                nb = len(re.findall(r"\bend\b", src))
            if na != nb:
                out_d.append(Diag(rel, None, f"{na} '{a}' but {nb} '{b}'", "structure"))
    return out_d


def check_scad(path: Path, rel: str) -> list[Diag]:
    tool = _tool("openscad")
    if tool:
        with tempfile.TemporaryDirectory() as td:
            rc, out = _run([tool, "-o", str(Path(td) / "o.echo"), str(path)], timeout=60)
        if rc != 0:
            return [Diag(rel, _line_of(out), out.splitlines()[0][:300] if out else "error", "openscad")]
        return []
    return check_brackets(path, rel)


def check_gd(path: Path, rel: str) -> list[Diag]:
    tool = _tool("godot", "godot4")
    if not tool:
        return []
    rc, out = _run([tool, "--headless", "--check-only", "--script", str(path)], timeout=60)
    return [Diag(rel, _line_of(out), ln.strip(), "godot") for ln in out.splitlines() if "ERROR" in ln][:5]


def _strip_comments(src: str, line_comment: str) -> str:
    # one left-to-right pass so that '"' (a char literal) or "//" inside a string cannot confuse it
    pat = re.compile(r"/\*.*?\*/|" + re.escape(line_comment) + r"[^\n]*|\"(?:\\.|[^\"\\\n])*\"|'(?:\\.|[^'\\\n])*'|`(?:\\.|[^`\\])*`", re.S)
    return pat.sub(lambda m: "\n" * m.group(0).count("\n"), src)


def check_brackets(path: Path, rel: str) -> list[Diag]:
    """Language-independent structural check for C-like sources."""
    src = _strip_comments(path.read_text(encoding="utf-8", errors="replace"), "//")
    stack: list[tuple[str, int]] = []
    pairs = {")": "(", "]": "[", "}": "{"}
    line = 1
    for ch in src:
        if ch == "\n":
            line += 1
        elif ch in "([{":
            stack.append((ch, line))
        elif ch in ")]}":
            if not stack or stack[-1][0] != pairs[ch]:
                return [Diag(rel, line, f"unexpected '{ch}'", "brackets")]
            stack.pop()
    if stack:
        return [Diag(rel, stack[-1][1], f"'{stack[-1][0]}' is never closed", "brackets")]
    return []


CHECKERS = {
    ".py": check_python, ".js": check_js, ".mjs": check_js, ".html": check_html, ".htm": check_html,
    ".json": check_json, ".c": check_c, ".h": check_c, ".cpp": check_c, ".hpp": check_c, ".cc": check_c,
    ".ino": check_brackets, ".cs": check_brackets, ".java": check_brackets, ".v": check_verilog,
    ".sv": check_verilog, ".scad": check_scad, ".gd": check_gd, ".go": check_brackets, ".rs": check_brackets,
    ".kt": check_brackets, ".swift": check_brackets, ".ts": check_brackets, ".css": check_brackets,
}


def collect(target: Path) -> tuple[Path, list[Path]]:
    """(root, source files) for a file or directory target."""
    target = target.resolve()
    if target.is_file():
        return target.parent, [target]
    files = []
    for p in sorted(target.rglob("*")):
        if p.is_file() and p.suffix.lower() in SOURCE_EXT and not (set(p.relative_to(target).parts) & SKIP_DIRS) \
                and not p.name.startswith(".nlcl_"):
            files.append(p)
    return target, files


def diagnose(target: Path, run_cmd: list[str] | None = None) -> list[Diag]:
    root, files = collect(Path(target))
    diags: list[Diag] = []
    for f in files:
        chk = CHECKERS.get(f.suffix.lower())
        if not chk:
            continue
        rel = str(f.relative_to(root)).replace("\\", "/")
        try:
            diags += chk(f, rel)
        except Exception as e:  # a checker must never stop the fixer
            diags.append(Diag(rel, None, f"checker failed: {e}", "nlcl", "warning"))
    if run_cmd:
        rc, out = _run(run_cmd, cwd=root, timeout=30)
        if rc not in (0, 124):
            tail = "\n".join(out.splitlines()[-25:])
            m = re.findall(r'File "([^"]+)", line (\d+)', out)
            f, ln = (m[-1] if m else ("(run)", None))
            try:
                f = str(Path(f).resolve().relative_to(root)).replace("\\", "/")
            except (ValueError, OSError):
                pass
            diags.append(Diag(f, int(ln) if ln else None, "runtime failure:\n" + tail, "run"))
    return diags


# --------------------------------------------------------------------------
# online fix loop
# --------------------------------------------------------------------------

FIX_PROMPT = """You are the debugging agent of NLCL. You fix bugs in source code of any language
(Python, JavaScript/HTML, C/C++, Arduino, C#, GDScript, Verilog, OpenSCAD, ...).
You receive: the user's description of the bug (may be empty), tool diagnostics, and the files.
Reply with ONLY a JSON object:
{"analysis": "<one or two sentences: the root cause>",
 "edits": [{"file": "<relative path exactly as given>", "search": "<exact text copied from the file>", "replace": "<new text>"}],
 "done": true|false}
Rules:
- "search" must be copied EXACTLY from the current file content (whitespace included), long enough to be unique
  (a few full lines), and as small as possible. Never rewrite a whole file.
- Fix the root cause, keep the code style, do not remove features, do not add unrelated changes.
- Only edit the files you were given. To report that nothing is wrong, return "edits": [] and "done": true.
- If earlier edits failed to apply or made things worse you will be told; correct them.
"""


class Fixer:
    def __init__(self, backend=None, max_rounds: int = 3):
        self.backend = backend            # object with .chat(messages, json_mode=True) or None
        self.max_rounds = max_rounds

    # ---- public ---------------------------------------------------------------
    def fix(self, target: Path, description: str = "", run_cmd: list[str] | None = None,
            focus: list[str] | None = None) -> FixReport:
        target = Path(target)
        if not target.exists():
            return FixReport(False, f"'{target}' does not exist")
        root, files = collect(target)
        if not files:
            return FixReport(False, f"no source files found in {target}")
        before = diagnose(target, run_cmd)
        if self.backend is None:
            if before:
                return FixReport(False, "offline: problems located but not fixed (fixing needs the online agent - "
                                        "set a free API key, see ':backend status')", before=before, after=before)
            return FixReport(True, f"offline check of {len(files)} file(s): no problems found by the available tools. "
                                   "Logic bugs need the online agent (':backend status').")
        originals: dict[Path, str] = {}
        applied: list[dict] = []
        notes: list[str] = []
        diags = before
        feedback = ""
        analysis = ""
        for rnd in range(1, self.max_rounds + 1):
            payload = self._payload(root, files, diags, description, focus)
            msgs = [{"role": "system", "content": FIX_PROMPT},
                    {"role": "user", "content": payload + (f"\n\nFEEDBACK FROM THE PREVIOUS ROUND:\n{feedback}" if feedback else "")}]
            try:
                from .llm import extract_json
                data = extract_json(self.backend.chat(msgs))
            except Exception as e:
                notes.append(f"round {rnd}: agent error: {str(e)[:160]}")
                break
            analysis = str(data.get("analysis") or analysis)
            edits = data.get("edits") or []
            if not edits:
                if data.get("done", True):
                    break
                feedback = "You returned no edits but said you are not done. Return edits or done=true."
                continue
            snapshot = {p: p.read_text(encoding="utf-8", errors="replace") for p in files}
            ok_edits, failed = [], []
            for ed in edits:
                r = self._apply(root, files, ed, originals)
                (ok_edits if r is None else failed).append(ed if r is None else f"{ed.get('file')}: {r}")
            new = diagnose(target, run_cmd)
            if _errors(new) > _errors(diags):
                for p, txt in snapshot.items():            # roll the round back
                    p.write_text(txt, encoding="utf-8", newline="")
                feedback = ("Your edits were ROLLED BACK because they introduced new problems:\n"
                            + "\n".join(str(d) for d in new[:8]))
                notes.append(f"round {rnd}: rolled back (edits made it worse)")
                continue
            applied += ok_edits
            diags = new
            feedback = ""
            if failed:
                feedback = "These edits did not apply (search text not found exactly):\n" + "\n".join(failed[:6])
                notes.append(f"round {rnd}: {len(failed)} edit(s) did not match the file")
            if not _errors(diags) and not failed and data.get("done", True):
                break
            if not _errors(diags) and not failed:
                feedback = "Tools report no problems now. Return done=true if the bug described by the user is fixed."
        changed, backup = {}, None
        for p, old in originals.items():
            new_txt = p.read_text(encoding="utf-8", errors="replace")
            if new_txt != old:
                rel = str(p.relative_to(root)).replace("\\", "/")
                changed[rel] = "".join(difflib.unified_diff(old.splitlines(True), new_txt.splitlines(True),
                                                            "before/" + rel, "after/" + rel, n=2))
        if changed:
            backup = str(self._backup(root, originals))
        ok = not _errors(diags)
        if changed:
            summary = f"fixed {len(changed)} file(s)" + (f": {analysis}" if analysis else "")
        elif analysis:
            summary = f"no change made: {analysis}"
        else:
            summary = "no change made"
        return FixReport(ok, summary, before, diags, changed, applied, backup, notes)

    @staticmethod
    def undo(root: Path) -> list[str]:
        """Restore the most recent backup made by fix()."""
        bdir = Path(root) / ".nlcl_backup"
        runs = sorted(p for p in bdir.iterdir() if p.is_dir()) if bdir.exists() else []
        if not runs:
            return ["  no fix backup found"]
        last, out = runs[-1], []
        for f in last.rglob("*"):
            if f.is_file():
                dst = Path(root) / f.relative_to(last)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dst)
                out.append(f"  restored {f.relative_to(last)}")
        shutil.rmtree(last, ignore_errors=True)
        return out or ["  backup was empty"]

    # ---- internals ------------------------------------------------------------
    def _payload(self, root: Path, files: list[Path], diags: list[Diag], description: str,
                 focus: list[str] | None) -> str:
        flagged = {d.file for d in diags}
        words = set(re.findall(r"[\w.\-/]+", (description or "").lower())) | {f.lower() for f in (focus or [])}

        def rank(p: Path) -> tuple:
            rel = str(p.relative_to(root)).replace("\\", "/")
            named = rel.lower() in words or p.name.lower() in words or p.stem.lower() in words
            return (rel not in flagged, not named, p.suffix.lower() not in CODE_EXT, p.stat().st_size)
        parts, total = [], 0
        skipped = []
        for p in sorted(files, key=rank):
            rel = str(p.relative_to(root)).replace("\\", "/")
            txt = p.read_text(encoding="utf-8", errors="replace")
            if len(txt) > MAX_FILE or total + len(txt) > MAX_TOTAL:
                skipped.append(rel)
                continue
            total += len(txt)
            parts.append(f"=== FILE {rel} ===\n{txt}\n=== END {rel} ===")
        head = [f"BUG DESCRIPTION FROM THE USER: {description.strip() or '(none - find and fix what the tools report, then anything clearly wrong)'}",
                "", "TOOL DIAGNOSTICS:" if diags else "TOOL DIAGNOSTICS: none (the tools found no problem; look for the logic bug described)"]
        head += [f"- {d}" for d in diags[:30]]
        if skipped:
            head.append(f"(not shown, too large: {', '.join(skipped[:12])})")
        return "\n".join(head) + "\n\n" + "\n\n".join(parts)

    @staticmethod
    def _apply(root: Path, files: list[Path], ed: dict, originals: dict[Path, str]) -> str | None:
        """Apply one edit. Returns None on success or the reason it failed."""
        if not isinstance(ed, dict) or "file" not in ed or "search" not in ed or "replace" not in ed:
            return "malformed edit"
        rel = str(ed["file"]).replace("\\", "/").lstrip("./")
        p = next((f for f in files if str(f.relative_to(root)).replace("\\", "/") == rel), None)
        if p is None:
            p = next((f for f in files if f.name == Path(rel).name), None)
        if p is None:
            return "not one of the given files"
        txt = p.read_text(encoding="utf-8", errors="replace")
        search, repl = str(ed["search"]), str(ed["replace"])
        if not search:
            return "empty search"
        if search not in txt:
            # tolerate differences in line endings / trailing spaces / indentation width
            norm = lambda s: re.sub(r"[ \t]+", " ", s.replace("\r\n", "\n")).strip()  # noqa: E731
            lines = txt.replace("\r\n", "\n").split("\n")
            want = [norm(x) for x in search.replace("\r\n", "\n").strip("\n").split("\n")]
            hit = None
            for i in range(len(lines) - len(want) + 1):
                if [norm(x) for x in lines[i:i + len(want)]] == want:
                    hit = i
                    break
            if hit is None:
                return "search text not found"
            indent = re.match(r"[ \t]*", lines[hit]).group(0)
            new_lines = repl.replace("\r\n", "\n").strip("\n").split("\n")
            base = re.match(r"[ \t]*", new_lines[0]).group(0) if new_lines else ""
            if base != indent:
                new_lines = [indent + ln[len(base):] if ln.startswith(base) else ln for ln in new_lines]
            lines[hit:hit + len(want)] = new_lines
            new_txt = "\n".join(lines)
        else:
            new_txt = txt.replace(search, repl, 1)
        if new_txt == txt:
            return "edit changes nothing"
        originals.setdefault(p, txt)
        p.write_text(new_txt, encoding="utf-8", newline="")
        return None

    @staticmethod
    def _backup(root: Path, originals: dict[Path, str]) -> Path:
        d = root / ".nlcl_backup" / time.strftime("%Y%m%d-%H%M%S")
        n = 1
        while d.exists():
            n += 1
            d = root / ".nlcl_backup" / (time.strftime("%Y%m%d-%H%M%S") + f"-{n}")
        for p, old in originals.items():
            dst = d / p.relative_to(root)
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(old, encoding="utf-8", newline="")
        return d


def _errors(diags: list[Diag]) -> int:
    return sum(1 for d in diags if d.severity == "error")


def python_run_cmd(path: Path) -> list[str] | None:
    """A safe default way to execute a Python target when the user asks to run it while fixing."""
    if path.is_file() and path.suffix == ".py":
        return [sys.executable, "-u", path.name]
    return None
