"""Terminal semantics: natural language -> shell intent (+ slots) -> exact effect on the file system.

* parse_shell()   sentence -> SHELL op {intent, slots}  (rule frames; file-type words come from Prolog)
* execute()       the reference implementation of every intent in Python (identical on every OS)
* render()        the same intent as a bash command and as a PowerShell command
* sandbox()       run in a copy of the folder and report the exact file-system diff before committing
* crosscheck()    run the rendered bash / PowerShell command in a sandbox and compare its effect with the
                  reference implementation: the shell text is *verified* to mean what the sentence means
"""
from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .ir import Op

PATH = r"""(?:"[^"]+"|'[^']+'|[\w.\-~/\\:*]+)"""
QUOTED = r"""(?:"[^"]*"|'[^']*')"""
REC = r"(?P<rec>\s+(?:recursively|including\s+(?:all\s+)?sub-?folders|in\s+all\s+sub-?folders|and\s+(?:all\s+)?(?:its\s+)?sub-?folders|everywhere|in\s+every\s+folder))?"
DIRP = rf"(?:\s+(?:in|inside|under|within|from|of)\s+(?:the\s+)?(?:folder\s+|directory\s+|dir\s+)?(?P<dir>{PATH}))?"
MAX_SANDBOX_FILES = 3000
MAX_SANDBOX_BYTES = 80 * 1024 * 1024


class ShellError(Exception):
    pass


@dataclass
class Selector:
    patterns: list[str] = field(default_factory=list)   # glob patterns ("*" = everything)
    path: str | None = None                              # one explicit path
    kind: str = "file"                                   # file | dir | any


def _unq(s: str | None) -> str | None:
    if s is None:
        return None
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    return s.rstrip("?.,;")


def _looks_like_path(s: str) -> bool:
    s = _unq(s) or ""
    return bool(re.search(r"[./\\]|^~", s)) or s in ("*",)


# ======================================================================= parsing
class ShellParser:
    def __init__(self, kb=None):
        self.kb = kb

    def selector(self, what: str) -> Selector | None:
        w = what.strip()
        low = w.lower()
        low = re.sub(r"^(?:all|the|every|any)\s+", "", low)
        low = re.sub(r"^(?:the\s+)", "", low)
        if re.fullmatch(r"(?:files|file|everything|contents?|items|stuff)(?:\s+and\s+folders)?", low):
            return Selector(["*"], kind="file" if "folders" not in low else "any")
        if re.fullmatch(r"(?:folders|directories|dirs|sub-?folders|sub-?directories)", low):
            return Selector(["*"], kind="dir")
        m = re.fullmatch(r"(?:files\s+(?:ending\s+(?:in|with)|with\s+(?:the\s+)?extension)\s+)\.?(\w+)", low)
        if m:
            return Selector([f"*.{m.group(1)}"])
        m = re.fullmatch(r"\*?\.(\w+)\s+files?", low) or re.fullmatch(r"(\*\.\w+)", low)
        if m:
            g = m.group(1)
            return Selector([g if g.startswith("*") else f"*.{g}"])
        m = re.fullmatch(r"([\w\-]+)\s+(?:files?|documents?|scripts?|sources?|source\s+files)", low) \
            or re.fullmatch(r"(images|pictures|photos|logs|backups)", low)
        if m:
            pats = self.kb.file_type(m.group(1)) if self.kb else None
            if pats is None and m.group(1) in ("log", "logs"):
                pats = ["*.log"]
            if pats:
                return Selector(pats)
            return None
        m = re.fullmatch(r"(?:files|file)\s+(?:named|called)\s+(\S+)", w, re.I)
        if m:
            return Selector([_unq(m.group(1))])
        m = re.fullmatch(r"(?:the\s+)?(folder|directory|dir|file)\s+(?:called\s+|named\s+)?(\S+)", w, re.I)
        if m:
            return Selector(path=_unq(m.group(2)), kind="dir" if m.group(1).lower() != "file" else "file")
        if _looks_like_path(w) or re.fullmatch(r"[\w\-]+", w):
            p = _unq(w)
            return Selector(path=p, kind="any") if "*" not in p else Selector([p])
        return None

    def parse(self, text: str) -> tuple[Op, dict] | None:
        """-> (SHELL op, features) or None when the sentence is not a terminal instruction."""
        t = text.strip().rstrip(".!")
        t = re.sub(r"^(?:please|can you|could you|now|ok)\s+", "", t, flags=re.I)
        for pid, fn in self.FRAMES:
            r = fn(self, t)
            if r:
                intent, slots = r
                slots = {k: v for k, v in slots.items() if v is not None}
                op = Op("SHELL", props={"intent": intent, **slots})
                return op, {"frame": pid, "slots": len(slots)}
        return None

    # ---- frames ---------------------------------------------------------------
    def _current(self, t):
        if re.fullmatch(r"(?:where\s+am\s+i|pwd|(?:show|print|what\s+is|what's)(?:\s+the)?\s+(?:current|working)\s+(?:folder|directory|dir|path))\??", t, re.I):
            return "current_dir", {}

    def _count_files(self, t):
        m = re.fullmatch(rf"(?:how\s+many|count(?:\s+the)?|number\s+of)\s+(?P<what>.*?(?:files?|folders|directories|images|pictures|photos|logs))(?:\s+are)?(?:\s+there)?{DIRP}{REC}\??", t, re.I)
        if m:
            sel = self.selector(m.group("what"))
            if sel and sel.path is None:
                return "count_files", {"patterns": sel.patterns, "kind": sel.kind, "dir": _unq(m.group("dir")) or ".",
                                       "recursive": bool(m.group("rec"))}

    def _list(self, t):
        m = re.fullmatch(rf"(?:list|show|display|ls|print|get)(?:\s+me)?\s+(?P<what>(?:all\s+)?(?:the\s+)?[\w.*\-\s]*?(?:files?|folders|directories|dirs|contents?|everything|images|pictures|photos|logs))(?:\s+files)?{DIRP}{REC}", t, re.I) \
            or re.fullmatch(rf"what\s+(?P<what>files|folders|\w+\s+files)\s+are\s+(?:there\s+)?(?:in|inside)\s+(?:the\s+)?(?:folder\s+)?(?P<dir>{PATH}){REC}\??", t, re.I)
        if m:
            sel = self.selector(m.group("what"))
            if sel and sel.path is None:
                return "list_files", {"patterns": sel.patterns, "kind": sel.kind, "dir": _unq(m.group("dir")) or ".",
                                      "recursive": bool(m.group("rec"))}
        m = re.fullmatch(rf"what(?:'s|\s+is)\s+(?:in|inside)\s+(?:the\s+)?(?:folder\s+|directory\s+)?(?P<dir>{PATH})\??", t, re.I)
        if m:
            return "list_files", {"patterns": ["*"], "kind": "any", "dir": _unq(m.group("dir")), "recursive": False}
        m = re.fullmatch(rf"(?:find|locate|search\s+for)(?:\s+all)?(?:\s+the)?\s+files?\s+(?:named|called)\s+(?P<name>{PATH}){DIRP}", t, re.I)
        if m:
            return "list_files", {"patterns": [_unq(m.group("name"))], "kind": "file", "dir": _unq(m.group("dir")) or ".",
                                  "recursive": True}

    def _make_dir(self, t):
        m = re.fullmatch(rf"(?:create|make|add|new|mkdir)(?:\s+a)?(?:\s+new)?(?:\s+empty)?\s+(?:folder|directory|dir)(?:\s+(?:called|named))?\s+(?P<path>{PATH}){DIRP}", t, re.I) \
            or re.fullmatch(rf"mkdir(?:\s+-p)?\s+(?P<path>{PATH}){DIRP}", t, re.I)
        if m:
            return "make_dir", {"path": _join(m.group("dir"), m.group("path"))}

    def _make_file(self, t):
        m = re.fullmatch(rf"(?:create|make|add|new|touch)(?:\s+an?)?(?:\s+new)?(?:\s+empty)?(?:\s+text)?\s+file(?:\s+(?:called|named))?\s+(?P<path>{PATH}){DIRP}(?:\s+(?:with|containing|that\s+says)(?:\s+the)?(?:\s+(?:text|content|line|words?))?\s+(?P<text>{QUOTED}))?", t, re.I)
        if m:
            return "make_file", {"path": _join(m.group("dir"), m.group("path")), "text": _unq(m.group("text"))}
        m = re.fullmatch(rf"(?:write|put|save)(?:\s+the)?(?:\s+(?:text|line|words?))?\s+(?P<text>{QUOTED})\s+(?:to|into|in)(?:\s+(?:the|a))?(?:\s+(?:new\s+)?file)?\s+(?P<path>{PATH})", t, re.I)
        if m:
            return "make_file", {"path": _unq(m.group("path")), "text": _unq(m.group("text"))}

    def _append(self, t):
        m = re.fullmatch(rf"(?:append|add)(?:\s+the)?(?:\s+(?:new\s+)?(?:line|text))?\s+(?P<text>{QUOTED})\s+(?:to|at\s+the\s+end\s+of)(?:\s+the)?(?:\s+file)?\s+(?P<path>{PATH})", t, re.I)
        if m:
            return "append_text", {"path": _unq(m.group("path")), "text": _unq(m.group("text"))}

    def _copy_move(self, t):
        m = re.fullmatch(rf"(?P<verb>copy|duplicate|cp|move|mv|transfer)(?:\s+all)?(?:\s+the)?\s+(?P<what>.+?){DIRP}\s+(?:to|into)(?:\s+the)?(?:\s+(?:folder|directory))?\s+(?P<dst>{PATH})", t, re.I)
        if m:
            sel = self.selector(m.group("what"))
            if not sel:
                return None
            intent = "copy" if m.group("verb").lower() in ("copy", "duplicate", "cp") else "move"
            return intent, {"patterns": sel.patterns or None, "kind": sel.kind,
                            "src": _join(m.group("dir"), sel.path) if sel.path else None,
                            "dir": _unq(m.group("dir")) or "." if not sel.path else None, "dst": _unq(m.group("dst"))}
        m = re.fullmatch(rf"rename(?:\s+the)?(?:\s+(?:file|folder|directory))?\s+(?P<src>{PATH})\s+(?:to|as)\s+(?P<dst>{PATH})", t, re.I) \
            or re.fullmatch(rf"change\s+the\s+name\s+of(?:\s+the)?(?:\s+(?:file|folder))?\s+(?P<src>{PATH})\s+to\s+(?P<dst>{PATH})", t, re.I)
        if m and not (_looks_like_path(m.group("src")) or _looks_like_path(m.group("dst"))):
            m = None
        if m:
            return "move", {"src": _unq(m.group("src")), "dst": _unq(m.group("dst")), "kind": "any"}
        m = re.fullmatch(rf"make\s+a\s+copy\s+of(?:\s+the)?(?:\s+file)?\s+(?P<src>{PATH})\s+(?:called|named|as)\s+(?P<dst>{PATH})", t, re.I)
        if m:
            return "copy", {"src": _unq(m.group("src")), "dst": _unq(m.group("dst")), "kind": "any"}
        m = re.fullmatch(rf"(?:back\s*up|make\s+a\s+backup\s+(?:copy\s+)?of)(?:\s+the)?(?:\s+file)?\s+(?P<src>{PATH})", t, re.I)
        if m:
            src = _unq(m.group("src"))
            return "copy", {"src": src, "dst": src + ".bak", "kind": "any"}

    def _delete(self, t):
        m = re.fullmatch(rf"(?:delete|remove|erase|rm|clean\s+up|get\s+rid\s+of|wipe)(?:\s+all)?(?:\s+the)?\s+(?P<what>.+?){DIRP}{REC}", t, re.I)
        if m:
            sel = self.selector(m.group("what"))
            if not sel:
                return None
            if sel.path is not None and not _looks_like_path(sel.path) and not re.search(r"\b(?:folder|file|directory)\b", t, re.I):
                return None      # "delete hero" is an entity, not a file
            if sel.path is not None:
                return "delete", {"path": _join(m.group("dir"), sel.path), "kind": "any", "recursive": True}
            return "delete", {"patterns": sel.patterns, "kind": sel.kind, "dir": _unq(m.group("dir")) or ".",
                              "recursive": bool(m.group("rec"))}

    def _show(self, t):
        m = re.fullmatch(rf"(?:show|print|display|cat|read|open|view|output)(?:\s+me)?(?:\s+the)?(?:\s+(?P<which>first|last|top|bottom)\s+(?P<n>\d+)\s+lines?(?:\s+(?:of|from|in)))?(?:\s+the)?(?:\s+file)?\s+(?P<path>{PATH})", t, re.I) \
            or re.fullmatch(rf"(?:show|print|display)?\s*(?:the\s+)?(?P<which>first|last|top|bottom)\s+(?P<n>\d+)\s+lines?\s+(?:of|from|in)(?:\s+the)?(?:\s+file)?\s+(?P<path>{PATH})", t, re.I)
        if m and _looks_like_path(m.group("path")):
            which = (m.group("which") or "").lower()
            mode = "head" if which in ("first", "top") else "tail" if which in ("last", "bottom") else "all"
            return "show_file", {"path": _unq(m.group("path")), "mode": mode,
                                 "n": int(m.group("n")) if m.group("n") else None}

    def _count_lines(self, t):
        m = re.fullmatch(rf"(?:count(?:\s+the)?\s+lines\s+(?:in|of)|how\s+many\s+lines\s+(?:are\s+)?(?:in|does|has)|number\s+of\s+lines\s+(?:in|of))(?:\s+the)?(?:\s+file)?\s+(?P<path>{PATH})(?:\s+(?:have|has))?\??", t, re.I)
        if m:
            return "count_lines", {"path": _unq(m.group("path"))}

    def _search(self, t):
        m = re.fullmatch(rf"(?:search|look|find|grep)(?:\s+for)?(?:\s+the\s+(?:text|word))?\s+(?P<text>{QUOTED}|\w+)\s+(?:in|inside|within)(?:\s+all)?(?:\s+the)?\s+(?P<where>.+?){REC}", t, re.I) \
            or re.fullmatch(rf"which\s+(?P<where>(?:[\w.*]+\s+)?files)\s+(?:contain|mention|have|include)\s+(?P<text>{QUOTED}|\w+)(?:\s+(?:in|under)\s+(?P<dir>{PATH}))?{REC}\??", t, re.I)
        if m:
            text = _unq(m.group("text"))
            where = m.group("where").strip()
            sel = self.selector(where)
            if not sel:
                return None
            d = _unq(m.groupdict().get("dir")) if m.groupdict().get("dir") else "."
            if sel.path is not None and not _looks_like_path(sel.path) and not Path(sel.path).suffix:
                return "search_text", {"text": text, "patterns": ["*"], "dir": sel.path, "recursive": True}
            if sel.path is not None:
                return "search_text", {"text": text, "path": sel.path}
            return "search_text", {"text": text, "patterns": sel.patterns, "dir": d, "recursive": True}

    def _replace(self, t):
        m = re.fullmatch(rf"replace(?:\s+(?:all|every))?(?:\s+the\s+(?:text|word))?\s+(?P<old>{QUOTED}|\S+)\s+(?:with|by)\s+(?P<new>{QUOTED}|\S+)\s+in(?:\s+the)?(?:\s+file)?\s+(?P<path>{PATH})", t, re.I) \
            or re.fullmatch(rf"in(?:\s+the)?(?:\s+file)?\s+(?P<path>{PATH}),?\s+replace(?:\s+(?:all|every))?\s+(?P<old>{QUOTED}|\S+)\s+(?:with|by)\s+(?P<new>{QUOTED}|\S+)", t, re.I)
        if m:
            return "replace_text", {"path": _unq(m.group("path")), "old": _unq(m.group("old")), "new": _unq(m.group("new"))}

    def _large(self, t):
        m = re.fullmatch(rf"(?:find|list|show)(?:\s+all)?(?:\s+the)?\s+files\s+(?:that\s+are\s+)?(?:larger|bigger|greater|over|above|more)(?:\s+than)?\s+(?P<num>[\d.]+)\s*(?P<unit>kb|mb|gb|b|bytes|k|m|g|kilobytes|megabytes)?{DIRP}", t, re.I)
        if m:
            unit = (m.group("unit") or "kb").lower()
            mult = 1 if unit in ("b", "bytes") else 1024 ** 2 if unit in ("mb", "m", "megabytes") else 1024 ** 3 if unit in ("gb", "g") else 1024
            return "find_large", {"bytes": int(float(m.group("num")) * mult), "dir": _unq(m.group("dir")) or "."}

    def _du(self, t):
        m = re.fullmatch(rf"(?:how\s+big\s+is|what\s+is\s+the\s+size\s+of|size\s+of|(?:show|get|print)(?:\s+the)?\s+(?:disk\s+usage|size)\s+of)(?:\s+the)?(?:\s+(?:folder|directory))?\s+(?P<dir>{PATH})\??", t, re.I)
        if m:
            return "disk_usage", {"dir": _unq(m.group("dir"))}

    def _archive(self, t):
        m = re.fullmatch(rf"(?:zip|compress|archive|pack)(?:\s+up)?(?:\s+the)?(?:\s+(?:folder|directory|file))?\s+(?P<src>{PATH})(?:\s+(?:into|to|as)(?:\s+a)?(?:\s+zip)?(?:\s+(?:file|archive))?(?:\s+(?:called|named))?\s+(?P<dst>{PATH}))?", t, re.I)
        if m:
            src = _unq(m.group("src")).rstrip("/\\")
            dst = _unq(m.group("dst")) or (src + ".zip")
            if not dst.lower().endswith(".zip"):
                dst += ".zip"
            return "archive", {"src": src, "dst": dst}

    def _sort(self, t):
        m = re.fullmatch(rf"sort(?:\s+the)?(?:\s+lines\s+(?:of|in))?(?:\s+the)?(?:\s+file)?\s+(?P<path>{PATH})", t, re.I)
        if m and _looks_like_path(m.group("path")):
            return "sort_lines", {"path": _unq(m.group("path"))}

    FRAMES = [("current_dir", _current), ("count_files", _count_files), ("count_lines", _count_lines),
              ("find_large", _large), ("disk_usage", _du), ("make_dir", _make_dir), ("make_file", _make_file),
              ("append_text", _append), ("replace_text", _replace), ("search_text", _search),
              ("copy_move", _copy_move), ("archive", _archive), ("sort_lines", _sort), ("delete", _delete),
              ("show_file", _show), ("list_files", _list)]


def _join(d: str | None, p: str | None) -> str | None:
    p, d = _unq(p), _unq(d)
    if p is None:
        return None
    if d and d not in (".", "./"):
        return f"{d.rstrip('/')}/{p}"
    return p


# ======================================================================= execution (reference)
def _inside(root: Path, p: Path) -> bool:
    try:
        p.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _match(root: Path, d: str, patterns: list[str], kind: str, recursive: bool) -> list[Path]:
    base = (root / d)
    if not base.is_dir():
        raise ShellError(f"no such folder: {d}")
    it = base.rglob("*") if recursive else base.iterdir()
    out = []
    for p in it:
        if ".git" in p.relative_to(root).parts:
            continue
        if kind == "file" and not p.is_file():
            continue
        if kind == "dir" and not p.is_dir():
            continue
        if any(fnmatch.fnmatch(p.name, pat) for pat in patterns):
            out.append(p)
    return sorted(out)


def _rel(root: Path, p: Path) -> str:
    return str(p.relative_to(root)).replace("\\", "/")


def execute(op: Op, root: Path, kb=None) -> str:
    """Perform the intent inside `root`. Returns what a shell would print."""
    root = Path(root)
    s = op.props
    intent = s["intent"]
    if kb is not None:
        # the folder a pattern is searched in ("delete *.tmp in .") is not itself removed
        for key in ("path", "src"):
            if s.get(key) and kb.forbidden(intent, s[key]):
                raise ShellError(f"refused: '{intent}' on protected path '{s[key]}'")
    for key in ("path", "src", "dst", "dir"):
        if s.get(key) and not _inside(root, root / s[key]):
            raise ShellError(f"refused: '{s[key]}' is outside the working folder")

    if intent == "current_dir":
        return str(root.resolve())
    if intent in ("list_files", "count_files"):
        hits = _match(root, s.get("dir", "."), s.get("patterns") or ["*"], s.get("kind", "file"), s.get("recursive", False))
        if intent == "count_files":
            return str(len(hits))
        return "\n".join(_rel(root, p) + ("/" if p.is_dir() else "") for p in hits)
    if intent == "make_dir":
        (root / s["path"]).mkdir(parents=True, exist_ok=True)
        return ""
    if intent == "make_file":
        p = root / s["path"]
        p.parent.mkdir(parents=True, exist_ok=True)
        if s.get("text") is not None:
            p.write_text(s["text"] + "\n", encoding="utf-8", newline="\n")
        elif not p.exists():
            p.write_text("", encoding="utf-8")
        return ""
    if intent == "append_text":
        p = root / s["path"]
        with open(p, "a", encoding="utf-8", newline="\n") as f:
            f.write(s["text"] + "\n")
        return ""
    if intent in ("copy", "move"):
        dst = root / s["dst"]
        if s.get("src"):
            src = root / s["src"]
            if not src.exists():
                raise ShellError(f"no such file or folder: {s['src']}")
            target = dst / src.name if dst.is_dir() else dst
            if intent == "copy":
                (shutil.copytree(src, target, dirs_exist_ok=True) if src.is_dir() else shutil.copy2(src, target))
            else:
                shutil.move(str(src), str(target))
            return ""
        hits = _match(root, s.get("dir", "."), s.get("patterns") or ["*"], s.get("kind", "file"), False)
        dst.mkdir(parents=True, exist_ok=True)
        for p in hits:
            if p.resolve() == dst.resolve():
                continue
            if intent == "copy":
                (shutil.copytree(p, dst / p.name, dirs_exist_ok=True) if p.is_dir() else shutil.copy2(p, dst / p.name))
            else:
                shutil.move(str(p), str(dst / p.name))
        return ""
    if intent == "delete":
        if s.get("path"):
            p = root / s["path"]
            if p.is_dir():
                shutil.rmtree(p)
            elif p.exists():
                p.unlink()
            return ""
        for p in reversed(_match(root, s.get("dir", "."), s.get("patterns") or ["*"], s.get("kind", "file"), s.get("recursive", False))):
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            elif p.exists():
                p.unlink()
        return ""
    if intent == "show_file":
        lines = _read_lines(root / s["path"])
        n = s.get("n") or 10
        if s.get("mode") == "head":
            lines = lines[:n]
        elif s.get("mode") == "tail":
            lines = lines[-n:]
        return "\n".join(lines)
    if intent == "count_lines":
        return str(len(_read_lines(root / s["path"])))
    if intent == "sort_lines":
        return "\n".join(sorted(_read_lines(root / s["path"])))
    if intent == "search_text":
        files = [root / s["path"]] if s.get("path") else _match(root, s.get("dir", "."), s.get("patterns") or ["*"], "file", s.get("recursive", True))
        out = []
        for f in files:
            for i, ln in enumerate(_read_lines(f), 1):
                if s["text"] in ln:
                    out.append(f"{_rel(root, f)}:{i}:{ln}" if not s.get("path") else f"{i}:{ln}")
        return "\n".join(out)
    if intent == "replace_text":
        p = root / s["path"]
        raw = p.read_bytes().decode("utf-8", "replace")
        p.write_bytes(raw.replace(s["old"], s["new"]).encode("utf-8"))
        return ""
    if intent == "find_large":
        hits = [p for p in _match(root, s.get("dir", "."), ["*"], "file", True) if p.stat().st_size > s["bytes"]]
        return "\n".join(_rel(root, p) for p in hits)
    if intent == "disk_usage":
        d = root / s["dir"]
        total = sum(p.stat().st_size for p in d.rglob("*") if p.is_file()) if d.is_dir() else d.stat().st_size
        return str(total)
    if intent == "archive":
        src, dst = root / s["src"], root / s["dst"]
        with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
            if src.is_dir():
                for p in sorted(src.rglob("*")):
                    if p.is_file():
                        z.write(p, str(p.relative_to(src.parent)).replace("\\", "/"))
            else:
                z.write(src, src.name)
        return ""
    raise ShellError(f"unknown intent {intent}")


def _read_lines(p: Path) -> list[str]:
    if not p.is_file():
        raise ShellError(f"no such file: {p.name}")
    return p.read_bytes().decode("utf-8", "replace").lstrip("﻿").replace("\r\n", "\n").split("\n")[:-1] \
        if p.read_bytes().endswith(b"\n") else p.read_bytes().decode("utf-8", "replace").lstrip("﻿").replace("\r\n", "\n").split("\n")


# ======================================================================= rendering
def q_sh(s: str) -> str:
    return "'" + str(s).replace("'", "'\\''") + "'"


def q_ps(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def _sed_escape(s: str) -> str:
    return re.sub(r"([\\/&.*\[\]^$])", r"\\\1", s)


def render(op: Op, shell: str = "bash") -> str:
    """The command a person would type, for bash (Linux/macOS/Git Bash) or PowerShell (Windows)."""
    s, intent = op.props, op.props["intent"]
    sh = shell == "bash"
    q = q_sh if sh else q_ps
    d = s.get("dir", ".")
    pats = s.get("patterns") or ["*"]
    rec = s.get("recursive", False)

    def find(extra: str = "", kind=None) -> str:
        kind = kind or s.get("kind", "file")
        t = {"file": " -type f", "dir": " -type d", "any": ""}[kind]
        names = " -o ".join(f"-name {q(p)}" for p in pats)
        names = f"\\( {names} \\)" if len(pats) > 1 else names
        depth = "" if rec else " -mindepth 1 -maxdepth 1"
        return f"find {q(d)}{depth}{t} {names}{extra}"

    def gci(extra: str = "", kind=None) -> str:
        kind = kind or s.get("kind", "file")
        t = {"file": " -File", "dir": " -Directory", "any": ""}[kind]
        r = " -Recurse" if rec else ""
        inc = ",".join(q(p) for p in pats)
        flt = f" -Filter {inc}" if len(pats) == 1 else f" -Include {inc}"
        path = q(d) if len(pats) == 1 or rec else q(str(Path(d) / "*").replace("\\", "/"))
        return f"Get-ChildItem {path}{t}{r}{flt}{extra}"

    if intent == "current_dir":
        return "pwd" if sh else "Get-Location"
    if intent == "list_files":
        return find(" | sort") if sh else gci(" | Select-Object -ExpandProperty FullName")
    if intent == "count_files":
        return find(" | wc -l") if sh else f"@({gci()}).Count"
    if intent == "make_dir":
        return f"mkdir -p {q(s['path'])}" if sh else f"New-Item -ItemType Directory -Force -Path {q(s['path'])} | Out-Null"
    if intent in ("make_file", "append_text"):
        p = q(s["path"])
        norm = s["path"].replace("\\", "/")
        pre = ""
        if "/" in norm:          # like the reference: create missing parent folders first
            parent = q(norm.rsplit("/", 1)[0])
            pre = f"mkdir -p {parent} && " if sh else f"New-Item -ItemType Directory -Force -Path {parent} | Out-Null; "
        if intent == "append_text":
            body = f"printf '%s\\n' {q(s['text'])} >> {p}" if sh else \
                f"[IO.File]::AppendAllText((Join-Path $PWD {p}), {q(s['text'])} + \"`n\")"
        elif s.get("text") is not None:
            body = f"printf '%s\\n' {q(s['text'])} > {p}" if sh else \
                f"[IO.File]::WriteAllText((Join-Path $PWD {p}), {q(s['text'])} + \"`n\")"
        else:
            body = f"touch {p}" if sh else f"if (-not (Test-Path {p})) {{ New-Item -ItemType File -Path {p} | Out-Null }}"
        return pre + body
    if intent in ("copy", "move"):
        if s.get("src"):
            if sh:
                return f"{'cp -r' if intent == 'copy' else 'mv'} {q(s['src'])} {q(s['dst'])}"
            return f"{'Copy-Item -Recurse' if intent == 'copy' else 'Move-Item'} {q(s['src'])} {q(s['dst'])}"
        if sh:
            act = "cp -r" if intent == "copy" else "mv"
            dq = q(s["dst"])
            return f"mkdir -p {dq} && " + find(f" -exec {act} {{}} {dq} \\;")
        act = "Copy-Item -Recurse" if intent == "copy" else "Move-Item"
        return f"New-Item -ItemType Directory -Force -Path {q(s['dst'])} | Out-Null; {gci()} | {act} -Destination {q(s['dst'])}"
    if intent == "delete":
        if s.get("path"):
            return f"rm -rf {q(s['path'])}" if sh else f"Remove-Item -Recurse -Force {q(s['path'])}"
        return find(" -exec rm -rf {} +") if sh else f"{gci()} | Remove-Item -Recurse -Force"
    if intent == "show_file":
        n = s.get("n") or 10
        if s.get("mode") == "head":
            return f"head -n {n} {q(s['path'])}" if sh else f"Get-Content {q(s['path'])} -TotalCount {n}"
        if s.get("mode") == "tail":
            return f"tail -n {n} {q(s['path'])}" if sh else f"Get-Content {q(s['path'])} -Tail {n}"
        return f"cat {q(s['path'])}" if sh else f"Get-Content {q(s['path'])}"
    if intent == "count_lines":
        return f"wc -l < {q(s['path'])}" if sh else f"@(Get-Content {q(s['path'])}).Count"
    if intent == "sort_lines":
        return f"LC_ALL=C sort {q(s['path'])}" if sh else \
            f"[string[]]$l = @(Get-Content {q(s['path'])}); [Array]::Sort($l, [StringComparer]::Ordinal); $l"
    if intent == "search_text":
        if s.get("path"):
            return f"grep -n -F -- {q(s['text'])} {q(s['path'])}" if sh else \
                (f"Select-String -Path {q(s['path'])} -Pattern {q(s['text'])} -SimpleMatch -CaseSensitive"
                 ' | ForEach-Object { "$($_.LineNumber):$($_.Line)" }')
        inc = " ".join(f"--include={q(p)}" for p in pats)
        return f"grep -rn -F {inc} -- {q(s['text'])} {q(d)}" if sh else \
            (f"{gci()} | Select-String -Pattern {q(s['text'])} -SimpleMatch -CaseSensitive"
             ' | ForEach-Object { "$($_.Path):$($_.LineNumber):$($_.Line)" }')
    if intent == "replace_text":
        if sh:
            return f"sed -i 's/{_sed_escape(s['old'])}/{_sed_escape(s['new'])}/g' {q(s['path'])}"
        return (f"$f = Join-Path $PWD {q(s['path'])}; [IO.File]::WriteAllText($f, "
                f"[IO.File]::ReadAllText($f).Replace({q(s['old'])}, {q(s['new'])}))")
    if intent == "find_large":
        kb = (s["bytes"] // 1024) + (1 if s["bytes"] % 1024 else 0)
        return f"find {q(d)} -type f -size +{s['bytes']}c" if sh else \
            f"Get-ChildItem {q(d)} -File -Recurse | Where-Object Length -gt {s['bytes']} | Select-Object -ExpandProperty FullName"
        _ = kb
    if intent == "disk_usage":
        return f"du -sb {q(s['dir'])}" if sh else \
            f"(Get-ChildItem {q(s['dir'])} -Recurse -File | Measure-Object Length -Sum).Sum"
    if intent == "archive":
        return f"zip -r {q(s['dst'])} {q(s['src'])}" if sh else \
            f"Compress-Archive -Path {q(s['src'])} -DestinationPath {q(s['dst'])} -Force"
    raise ShellError(f"unknown intent {intent}")


# ======================================================================= sandbox + diff
def snapshot(root: Path) -> dict[str, str]:
    """relative path -> content fingerprint (text normalised: BOM and CRLF ignored; zips by member list)."""
    out = {}
    root = Path(root)
    for p in root.rglob("*"):
        rel = _rel(root, p)
        if rel.split("/")[0] in (".git", ".nlcl_sandbox"):
            continue
        if p.is_dir():
            out[rel + "/"] = "dir"
            continue
        b = p.read_bytes()
        if p.suffix.lower() == ".zip":
            try:
                with zipfile.ZipFile(p) as z:
                    out[rel] = "zip:" + ",".join(sorted(n.rstrip("/") for n in z.namelist() if not n.endswith("/")))
                continue
            except zipfile.BadZipFile:
                pass
        b = b.lstrip(b"\xef\xbb\xbf").replace(b"\r\n", b"\n")
        out[rel] = hashlib.sha1(b).hexdigest()
    return out


@dataclass
class Diff:
    added: list[str]
    removed: list[str]
    modified: list[str]

    @property
    def empty(self) -> bool:
        return not (self.added or self.removed or self.modified)

    def lines(self) -> list[str]:
        out = [f"    + {p}" for p in self.added] + [f"    - {p}" for p in self.removed] + [f"    ~ {p}" for p in self.modified]
        return out or ["    (no file changes)"]


def diff(before: dict, after: dict) -> Diff:
    return Diff(sorted(set(after) - set(before)), sorted(set(before) - set(after)),
                sorted(k for k in before if k in after and before[k] != after[k]))


def _copy_tree(src: Path, dst: Path) -> None:
    n, size = 0, 0
    for p in Path(src).rglob("*"):
        if ".git" in p.relative_to(src).parts or ".nlcl_sandbox" in p.relative_to(src).parts:
            continue
        n += 1
        if p.is_file():
            size += p.stat().st_size
        if n > MAX_SANDBOX_FILES or size > MAX_SANDBOX_BYTES:
            raise ShellError("folder too large to simulate safely; run it in a smaller folder")
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".git", ".nlcl_sandbox"), dirs_exist_ok=True)


def sandbox(op: Op, root: Path, kb=None) -> tuple[str, Diff]:
    """Run the intent in a throw-away copy of `root`; return (output, file-system diff)."""
    root = Path(root)
    with tempfile.TemporaryDirectory(prefix="nlcl_sbx_") as td:
        work = Path(td) / "w"
        _copy_tree(root, work)
        before = snapshot(work)
        out = execute(op, work, kb)
        return out.replace(str(work.resolve()), str(root.resolve())), diff(before, snapshot(work))


def shell_available(shell: str) -> str | None:
    if shell == "bash":
        return shutil.which("bash")
    return shutil.which("powershell") or shutil.which("pwsh")


def run_rendered(op: Op, root: Path, shell: str, timeout: int = 60) -> tuple[int, str]:
    cmd = render(op, shell)
    exe = shell_available(shell)
    if not exe:
        raise ShellError(f"{shell} is not installed")
    if shell == "bash":
        args = [exe, "-c", cmd]
    else:
        args = [exe, "-NoProfile", "-NonInteractive", "-Command",
                "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; " + cmd]
    env = dict(os.environ, LC_ALL="C")
    p = subprocess.run(args, cwd=str(root), capture_output=True, text=True, timeout=timeout, env=env,
                       encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def _norm_out(intent: str, out: str, root: Path) -> list[str]:
    """Shell output -> comparable lines (paths made relative, sizes / counts as numbers)."""
    root_s = [str(root.resolve()), str(root.resolve()).replace("\\", "/"), str(root)]
    lines = []
    for ln in out.replace("\r\n", "\n").split("\n"):
        ln = ln.rstrip()
        if not ln:
            continue
        for r in root_s:
            ln = ln.replace(r + "\\", "").replace(r + "/", "")
        ln = ln.replace("\\", "/")
        if ln.startswith("./"):
            ln = ln[2:]
        if intent == "disk_usage":
            ln = ln.split()[0]
        lines.append(ln.strip())
    if intent in ("list_files", "find_large", "search_text"):
        lines = sorted(x.rstrip("/") for x in lines)
    return lines


def crosscheck(op: Op, root: Path, shell: str) -> tuple[bool, str]:
    """Verify that the rendered shell command has exactly the effect of the reference implementation."""
    intent = op.props["intent"]
    if intent == "archive" and shell == "bash" and not shutil.which("zip"):
        return True, "skipped (zip not installed)"
    if intent == "current_dir":
        return True, "skipped (prints a location)"
    with tempfile.TemporaryDirectory(prefix="nlcl_xc_") as td:
        a, b = Path(td) / "ref", Path(td) / "sh"
        _copy_tree(root, a)
        _copy_tree(root, b)
        ref_out = execute(op, a)
        code, sh_out = run_rendered(op, b, shell)
        if code != 0 and not (intent == "search_text" and code == 1 and not ref_out):
            return False, f"{shell} exit {code}: {sh_out.strip()[:300]}"
        d = diff(snapshot(a), snapshot(b))
        if not d.empty:
            return False, "file system differs: " + " ".join(x.strip() for x in d.lines())
        ro, so = _norm_out(intent, ref_out, a), _norm_out(intent, sh_out, b)
        if intent == "disk_usage" and shell == "powershell" and not so:
            so = ["0"]
        if ro != so:
            return False, f"output differs:\n  reference: {ro[:6]}\n  {shell}: {so[:6]}"
        return True, "identical effect"
