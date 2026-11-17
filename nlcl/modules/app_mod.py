"""Application domain: GUI programs for every operating system.

From the same IR this module emits
  * app.py + nlcl_app.py   a desktop app (Python + Tkinter, standard library only) that runs on
                           Windows, macOS and Linux, with scripts that package it as a native
                           executable for each OS (PyInstaller, free);
  * web/                   the same app as an installable PWA (HTML + JS, no build step) for
                           Android, iOS, ChromeOS and any browser.
Widgets are entities (button, label, input, checkbox, slider, list ...), a window/app entity is
the root, behaviours become event handlers, links to other modules go through the NLCL hub.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from ..ir import Entity, describe_action
from .base import BuildContext, DomainModule, RunResult

ASSETS = Path(__file__).parent / "_assets" / "app"
ROOT_KINDS = ("app", "application", "window", "dialog", "screen", "page")
KEYS = {"space", "enter", "return", "escape", "esc", "up", "down", "left", "right", "tab"}


class AppModule(DomainModule):
    name = "app"
    title = "Applications (desktop GUI for Windows/macOS/Linux + installable web app for Android/iOS)"
    aliases = ("application", "desktop", "gui", "ui", "pwa", "mobile")
    keywords = ("app", "application", "window", "dialog", "form", "menu", "widget", "tkinter", "windows",
                "macos", "linux", "android", "ios", "notepad", "calculator", "todo", "checkbox", "textbox",
                "textarea", "dropdown", "slider", "listbox", "screen", "page", "toolbar", "label")
    kinds = {
        "object": {},
        "app": {"title": "", "size": [480, 360]},
        "application": {"title": "", "size": [480, 360]},
        "window": {"title": "", "size": [480, 360]},
        "dialog": {"title": ""},
        "screen": {"title": ""},
        "page": {"title": ""},
        "form": {},
        "panel": {},
        "frame": {},
        "group": {},
        "tab": {},
        "button": {},
        "label": {},
        "slider": {"value": 0, "min": 0, "max": 100},
        "timer": {},
        "title": {},
        "heading": {},
        "input": {"value": ""},
        "textbox": {"value": ""},
        "entry": {"value": ""},
        "field": {"value": ""},
        "password": {"value": ""},
        "textarea": {"value": ""},
        "checkbox": {"checked": False},
        "switch": {"checked": False},
        "dropdown": {"items": [], "value": ""},
        "select": {"items": [], "value": ""},
        "listbox": {"items": []},
        "list": {"items": []},
        "progress": {"value": 0, "max": 100},
        "canvas": {},
        "image": {},
        "store": {},
        "model": {},
    }
    suggested_props = ("title", "text", "value", "size", "items", "checked", "label", "placeholder")
    protocols = ("http",)
    toolchain = """**Desktop (Windows / macOS / Linux)** - free CPython 3.10+ from python.org (Tkinter is included;
on Debian/Ubuntu: `sudo apt install python3-tk`):

    python app.py                 # run the app
    python app.py --selftest      # build the UI, fire every event once, print the state, exit

Native executable for the OS you are on (free PyInstaller: `pip install pyinstaller`):

    build_windows.bat             # -> dist\\<name>.exe
    ./build_macos.sh              # -> dist/<name>.app
    ./build_linux.sh              # -> dist/<name>  (+ <name>.desktop launcher)

**Android / iOS / ChromeOS / any browser** - `web/` is an installable PWA, no build step:

    python -m http.server 8080 --directory web     # then open http://localhost:8080
    (on the phone: open the page -> browser menu -> "Add to Home screen" / "Install app")
    node web/app.js --selftest                      # logic self-test without a browser"""

    # ------------------------------------------------------------------ spec
    def _spec(self, ctx: BuildContext) -> dict:
        ids = {e.id: e.ident for e in ctx.entities}
        root = next((e for e in ctx.entities if e.kind in ROOT_KINDS and e.parent not in ids), None)
        title = str((root.props.get("title") if root else "") or (root.name if root else ctx.system.name)).replace("_", " ")
        size = (root.props.get("size") if root else None) or [480, 360]
        if not isinstance(size, list):
            size = [size, 360]
        widgets = []
        for e in ctx.entities:
            props = {k: v for k, v in e.props.items() if not k.startswith("_")}
            parent = ids.get(e.parent)
            if parent is None and root is not None and e.id != root.id and e.kind not in ROOT_KINDS:
                parent = root.ident
            widgets.append({"id": e.ident, "kind": e.kind, "parent": parent, "props": props})
        channels = []
        for c in ctx.channels:
            url = c.config.get("hub_url", "http://localhost:8765") + c.config.get("path", f"/channel/{c.id}")
            if c.src.domain == self.name and c.dst.domain == self.name:
                continue        # both ends in this app: state is shared already
            channels.append({"id": c.id, "dir": "out" if c.src.domain == self.name else "in",
                             "src": c.src.ident, "dst": c.dst.ident, "fields": list(c.fields), "url": url,
                             "interval_ms": int(c.config.get("interval_ms", 1000) or 1000)})
        return {"title": title.strip() or "NLCL app", "size": size, "widgets": widgets, "channels": channels}

    # ------------------------------------------------------------------ handlers
    def _handlers(self, ctx: BuildContext, lang: str) -> tuple[dict, set[str], list[str]]:
        """-> ({group: entries}, call names, notes). Entries are source snippets."""
        local = {e.ident for e in ctx.entities}
        H: dict = {"start": [], "update": [], "timers": [], "click": {}, "key": {}, "change": {}, "message": {}}
        calls: set[str] = set()
        notes: list[str] = []
        n = 0
        for e in ctx.entities:
            for b in e.behaviors or []:
                if not isinstance(b, dict) or not b.get("do"):
                    continue
                n += 1
                on = str(b.get("on") or "update")
                kind, _, arg = on.partition(":")
                name = self._namer(e, lang)
                cond = self.expr(b["if"], "python" if lang == "py" else "js", name) if b.get("if") else None
                acts = []
                for a in b["do"]:
                    acts += self._action(e, a, lang, name, calls)
                edge = None
                if kind == "update" and cond:
                    edge = f"{e.ident}_{n}"     # "when x is above 5, ..." fires once when it becomes true
                fn = self._fn(e, cond, acts, lang, edge, on)
                if kind == "start":
                    H["start"].append((e.ident, fn))
                elif kind == "update":
                    H["update"].append((e.ident, fn))
                elif kind == "timer":
                    try:
                        ms = max(10, int(float(arg)))
                    except ValueError:
                        ms = 1000
                    H["timers"].append((e.ident, ms, fn))
                elif kind == "input":
                    key = arg.strip().lower().replace(" ", "_")
                    if key in ("click", "mouse", "tap", "touch", "clicked", "it", "self"):
                        H["click"].setdefault(e.ident, []).append((e.ident, fn))
                    elif key in local and key not in KEYS:
                        H["click"].setdefault(key, []).append((e.ident, fn))
                    else:
                        H["key"].setdefault({"return": "enter", "esc": "escape"}.get(key, key), []).append((e.ident, fn))
                elif kind == "change":
                    ent, _, prop = arg.rpartition(".")
                    H["change"].setdefault((ent or e.ident, prop), []).append((e.ident, fn))
                elif kind == "message":
                    H["message"].setdefault(arg or "any", []).append((e.ident, fn))
                else:
                    notes.append(f"{e.name}: '{on}' has no meaning in a GUI app and was skipped")
        return H, calls, notes

    @staticmethod
    def _namer(e: Entity, lang: str):
        def f(n: str) -> str:
            if "." in n:
                ent, prop = n.split(".", 1)
                return f"app.get({ent!r}, {prop!r})" if lang == "py" else f"app.get({json.dumps(ent)}, {json.dumps(prop)})"
            return f"app.get({e.ident!r}, {n!r})" if lang == "py" else f"app.get({json.dumps(e.ident)}, {json.dumps(n)})"
        return f

    def _action(self, e: Entity, a: dict, lang: str, name, calls: set[str]) -> list[str]:
        q = (lambda s: repr(s)) if lang == "py" else (lambda s: json.dumps(s, ensure_ascii=False))
        ex = lambda v: self.expr(v, "python" if lang == "py" else "js", name)  # noqa: E731
        end = "" if lang == "py" else ";"
        o, op = q(e.ident), a.get("op")
        try:
            if op == "set":
                return [f"app.set({o}, {q(a['prop'])}, {ex(a['value'])}){end}"]
            if op == "add":
                return [f"app.add({o}, {q(a['prop'])}, {ex(a['value'])}){end}"]
            if op == "toggle":
                return [f"app.toggle({o}, {q(a['prop'])}){end}"]
            if op == "destroy":
                return [f"app.hide({o}){end}"]
            if op == "log":
                return [f"app.log({o}, {q(a.get('text', ''))}){end}"]
            if op == "send":
                none = "None" if lang == "py" else "null"
                return [f"app.send({o}, {q(a['to']) if a.get('to') else none}, {q(list(a.get('fields') or []))}){end}"]
            if op == "call":
                calls.add(str(a["name"]))
                args = "".join(", " + ex(x) for x in a.get("args", []))
                return [f"app.call({o}, {q(a['name'])}{args}){end}"]
        except Exception:
            pass
        text = describe_action(a) if isinstance(a, dict) and a.get("op") in ("move",) else str(a.get("op"))
        return [("pass  # " if lang == "py" else "/* ") + f"'{text}' is not meaningful in a GUI app" + ("" if lang == "py" else " */")]

    @staticmethod
    def _fn(e: Entity, cond: str | None, acts: list[str], lang: str, edge: str | None, on: str) -> list[str]:
        """Body lines of one handler (no def line)."""
        body = []
        if cond and edge:
            body.append(f"if not app.edge({edge!r}, {cond}): return" if lang == "py"
                        else f"if (!app.edge({json.dumps(edge)}, {cond})) return;")
        elif cond:
            body.append(f"if not ({cond}): return" if lang == "py" else f"if (!({cond})) return;")
        return body + (acts or (["pass"] if lang == "py" else []))

    # ------------------------------------------------------------------ python
    def _py(self, ctx: BuildContext, spec: dict) -> str:
        H, calls, notes = self._handlers(ctx, "py")
        L = ["#!/usr/bin/env python3",
             f'"""{spec["title"]} - desktop application (Windows / macOS / Linux). Generated by NLCL; edit freely."""',
             "from nlcl_app import main", "",
             "SPEC = " + _pyrepr(spec), ""]
        L += [f"# note: {n}" for n in notes]
        defs: list[str] = []
        counter = [0]

        def reg(owner, body, comment):
            counter[0] += 1
            fname = f"h{counter[0]}_{owner}"
            defs.extend([f"def {fname}(app):  # {comment}"] + ["    " + ln for ln in body] + ["", ""])
            return f"({owner!r}, {fname})"
        groups = {"start": [reg(o, b, "on start") for o, b in H["start"]],
                  "update": [reg(o, b, "every tick") for o, b in H["update"]]}
        timers = []
        for o, ms, b in H["timers"]:
            counter[0] += 1
            fname = f"h{counter[0]}_{o}"
            defs.extend([f"def {fname}(app):  # every {ms} ms"] + ["    " + ln for ln in b] + ["", ""])
            timers.append(f"({o!r}, {ms}, {fname})")
        maps = {}
        for g in ("click", "key", "message"):
            maps[g] = {k: [reg(o, b, f"{g} {k}") for o, b in v] for k, v in H[g].items()}
        change = {k: [reg(o, b, f"when {k[0]}.{k[1]} changes") for o, b in v] for k, v in H["change"].items()}
        L += defs
        L += ["HANDLERS = {",
              f"    'start': [{', '.join(groups['start'])}],",
              f"    'update': [{', '.join(groups['update'])}],",
              f"    'timers': [{', '.join(timers)}],"]
        for g in ("click", "key", "message"):
            L.append(f"    {g!r}: {{" + ", ".join(f"{k!r}: [{', '.join(v)}]" for k, v in maps[g].items()) + "},")
        L.append("    'change': {" + ", ".join(f"{k!r}: [{', '.join(v)}]" for k, v in change.items()) + "},")
        L += ["}", "", "# Your own functions for 'call <name>' actions: name -> function(app, owner, *args)",
              "CALLS = {"]
        for c in sorted(calls):
            L.append(f"    # {c!r}: lambda app, owner, *args: app.log(owner, {c + ' done'!r}),")
        L += ["}", "", "", "if __name__ == '__main__':", "    main(SPEC, HANDLERS, CALLS)", ""]
        return "\n".join(L)

    # ------------------------------------------------------------------ javascript
    def _js(self, ctx: BuildContext, spec: dict) -> str:
        H, calls, notes = self._handlers(ctx, "js")

        def fn(body):
            return "(app) => { " + " ".join(body) + " }"

        def pairs(lst):
            return "[" + ", ".join(f"[{json.dumps(o)}, {fn(b)}]" for o, b in lst) + "]"
        L = [(ASSETS / "app_runtime.js").read_text(encoding="utf-8").rstrip(), "",
             "// ---------------------------------------------------------------- generated by NLCL",
             "const SPEC = " + json.dumps(spec, ensure_ascii=False, indent=1) + ";"]
        L += [f"// note: {n}" for n in notes]
        L += ["const HANDLERS = {",
              f"  start: {pairs(H['start'])},",
              f"  update: {pairs(H['update'])},",
              "  timers: [" + ", ".join(f"[{json.dumps(o)}, {ms}, {fn(b)}]" for o, ms, b in H["timers"]) + "],"]
        for g in ("click", "key", "message"):
            L.append(f"  {g}: {{" + ", ".join(f"{json.dumps(k)}: {pairs(v)}" for k, v in H[g].items()) + "},")
        L.append("  change: {" + ", ".join(f"{json.dumps(k[0] + '.' + k[1])}: {pairs(v)}" for k, v in H["change"].items()) + "},")
        L += ["};", "// Your own functions for 'call <name>' actions: name -> (app, owner, ...args) => {}",
              "const CALLS = {" + "".join(f"\n  // {json.dumps(c)}: (app, owner, ...args) => app.log(owner, {json.dumps(c + ' done')})," for c in sorted(calls)) + "\n};",
              "if (typeof document !== 'undefined') {",
              "  const app = new NlclApp(SPEC, HANDLERS, CALLS); window.app = app; app.mainloop();",
              "  if ('serviceWorker' in navigator && location.protocol.startsWith('http')) navigator.serviceWorker.register('sw.js').catch(() => {});",
              "} else if (typeof process !== 'undefined' && process.argv.includes('--selftest')) {",
              "  new NlclApp(SPEC, HANDLERS, CALLS, null).selftest();",
              "}", ""]
        return "\n".join(L)

    # ------------------------------------------------------------------ build
    def generate(self, ctx: BuildContext) -> dict[str, str]:
        spec = self._spec(ctx)
        slug = self.ident(spec["title"]) or "app"
        title = spec["title"]
        files = {
            "nlcl_app.py": (ASSETS / "nlcl_app.py").read_text(encoding="utf-8"),
            "app.py": self._py(ctx, spec),
            "web/app.js": self._js(ctx, spec),
            "web/index.html": _INDEX.replace("{title}", _html(title)),
            "web/manifest.webmanifest": json.dumps({
                "name": title, "short_name": title[:12], "start_url": ".", "display": "standalone",
                "background_color": "#ffffff", "theme_color": "#2563eb",
                "icons": [{"src": "icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any maskable"}]}, indent=1),
            "web/sw.js": _SW,
            "web/icon.svg": _ICON.replace("{letter}", _html((title[:1] or "N").upper())),
            "build_windows.bat": f"@echo off\r\nrem Native Windows executable (free): pip install pyinstaller\r\n"
                                 f"python -m PyInstaller --noconfirm --onefile --windowed --name {slug} app.py\r\n"
                                 f"echo Done: dist\\{slug}.exe\r\n",
            "build_macos.sh": f"#!/bin/sh\n# Native macOS app bundle (free): pip3 install pyinstaller\nset -e\n"
                              f"python3 -m PyInstaller --noconfirm --windowed --name {slug} app.py\n"
                              f"echo \"Done: dist/{slug}.app\"\n",
            "build_linux.sh": f"#!/bin/sh\n# Native Linux executable (free): pip3 install pyinstaller ; needs python3-tk\nset -e\n"
                              f"python3 -m PyInstaller --noconfirm --onefile --name {slug} app.py\n"
                              f"cat > {slug}.desktop <<EOF\n[Desktop Entry]\nType=Application\nName={title}\n"
                              f"Exec=$(pwd)/dist/{slug}\nTerminal=false\nCategories=Utility;\nEOF\n"
                              f"echo \"Done: dist/{slug} and {slug}.desktop\"\n",
            "README.md": f"# {title}\n\nGenerated by NLCL - one description, every operating system.\n\n"
                         + self.toolchain + "\n\n## Widgets\n\n"
                         + "\n".join(f"- `{w['id']}` ({w['kind']})" + (f" in `{w['parent']}`" if w["parent"] else "")
                                     for w in spec["widgets"]) + "\n",
        }
        return files

    def validate(self, entity: Entity) -> list[str]:
        out = []
        if entity.kind in ("dropdown", "select", "list", "listbox") and not isinstance(entity.props.get("items", []), list):
            out.append("items should be a list, e.g. 'with items red, green and blue'")
        return out

    def prompt_for(self, entity: Entity) -> str | None:
        if entity.kind in ROOT_KINDS:
            return f"What should {entity.name} contain? (e.g. 'add a button called ok with text \"OK\" to {entity.name}')"
        return None

    def run(self, ctx: BuildContext) -> RunResult | None:
        out, ok = [], True
        r = self.sh([sys.executable, "-u", "app.py", "--selftest"], ctx.outdir, timeout=30)
        ok &= r.ok and "SELFTEST OK" in r.output
        out.append("desktop (python app.py --selftest): " + ("OK" if r.ok and "SELFTEST OK" in r.output else "FAILED"))
        out += ["  " + ln[:300] for ln in r.output.splitlines()[-8:]]
        node = self.which("node")
        if node:
            r2 = self.sh([node, "web/app.js", "--selftest"], ctx.outdir, timeout=30)
            good = r2.ok and "SELFTEST OK" in r2.output
            ok &= good
            out.append("web/PWA (node web/app.js --selftest): " + ("OK" if good else "FAILED"))
            if not good:
                out += ["  " + ln[:300] for ln in r2.output.splitlines()[-8:]]
        out.append(f"start it: python {ctx.outdir / 'app.py'}")
        return RunResult(ok, "\n".join(out), "app self-test")


def _pyrepr(obj) -> str:
    """JSON-like Python literal (True/False/None) with indentation."""
    return json.dumps(obj, ensure_ascii=False, indent=1).replace(": true", ": True").replace(": false", ": False") \
        .replace(": null", ": None").replace("[true", "[True").replace("[false", "[False") \
        .replace(", true", ", True").replace(", false", ", False")


def _html(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


_INDEX = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<link rel="manifest" href="manifest.webmanifest">
<link rel="icon" href="icon.svg">
<meta name="theme-color" content="#2563eb">
<style>
  :root { color-scheme: light dark; --bg:#f8fafc; --fg:#0f172a; --card:#ffffff; --line:#cbd5e1; --accent:#2563eb; }
  @media (prefers-color-scheme: dark) { :root { --bg:#0f172a; --fg:#e2e8f0; --card:#1e293b; --line:#334155; --accent:#60a5fa; } }
  body { margin:0; font:16px/1.4 system-ui, sans-serif; background:var(--bg); color:var(--fg); }
  header { padding:14px 16px; font-weight:600; background:var(--accent); color:#fff; }
  #app { max-width:560px; margin:0 auto; padding:16px; display:flex; flex-direction:column; gap:10px; }
  fieldset { border:1px solid var(--line); border-radius:10px; background:var(--card); display:flex; flex-direction:column; gap:10px; }
  button { padding:10px 14px; border:0; border-radius:8px; background:var(--accent); color:#fff; font:inherit; cursor:pointer; }
  button:active { opacity:.8 }
  input:not([type=checkbox]):not([type=range]), textarea, select { width:100%; box-sizing:border-box; padding:9px; border:1px solid var(--line);
    border-radius:8px; background:var(--card); color:var(--fg); font:inherit; }
  .row { display:flex; align-items:center; gap:8px; } .row input[type=range] { flex:1; }
  progress { width:100%; height:14px; } canvas { width:100%; background:var(--card); border:1px solid var(--line); border-radius:8px; }
  ul.list { list-style:none; margin:0; padding:0; border:1px solid var(--line); border-radius:8px; background:var(--card); }
  ul.list li { padding:9px 12px; border-bottom:1px solid var(--line); cursor:pointer; } ul.list li:last-child { border:0; }
  h2 { margin:4px 0; font-size:20px; }
  #status { position:sticky; bottom:0; padding:8px 16px; font-size:13px; border-top:1px solid var(--line); background:var(--card); min-height:18px; }
</style>
</head>
<body>
<header>{title}</header>
<main id="app"></main>
<div id="status"></div>
<script src="app.js"></script>
</body>
</html>
"""

_SW = """// Offline cache for the installed app.
const CACHE = "nlcl-app-v1";
const FILES = ["./", "index.html", "app.js", "manifest.webmanifest", "icon.svg"];
self.addEventListener("install", (e) => e.waitUntil(caches.open(CACHE).then((c) => c.addAll(FILES))));
self.addEventListener("fetch", (e) => {
  if (e.request.method !== "GET" || !e.request.url.startsWith(self.location.origin)) return;
  e.respondWith(fetch(e.request).then((r) => { const copy = r.clone(); caches.open(CACHE).then((c) => c.put(e.request, copy)); return r; })
    .catch(() => caches.match(e.request)));
});
"""

_ICON = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128"><rect width="128" height="128" rx="24" fill="#2563eb"/>
<text x="64" y="86" font-family="system-ui, sans-serif" font-size="72" font-weight="700" text-anchor="middle" fill="#fff">{letter}</text></svg>
"""

MODULE = AppModule()
