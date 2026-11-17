"""NLCL application runtime (Python standard library only: Tkinter).

Runs on Windows, macOS and Linux.  The generated app.py supplies SPEC (windows + widgets),
HANDLERS (behaviours) and optional CALLS (your own functions); this file turns them into a
running GUI.  ``--selftest`` builds the UI, fires every event once and exits (used by NLCL's
run step and handy in CI); ``--headless`` runs the logic without a display.
"""
from __future__ import annotations

import argparse
import json
import queue
import re
import sys
import threading
import time
import urllib.request

try:
    import tkinter as tk
    from tkinter import ttk
except Exception:      # no Tk on this Python build: logic still works with --headless
    tk = None
    ttk = None

CONTAINERS = {"app", "window", "dialog", "form", "frame", "panel", "group", "page", "tab", "screen"}
TEXT_PROP = {"button": "text", "label": "text", "checkbox": "text", "switch": "text", "title": "text",
             "heading": "text", "link": "text"}
VALUE_PROP = {"input": "value", "textbox": "value", "entry": "value", "field": "value", "textarea": "value",
              "slider": "value", "progress": "value", "dropdown": "value", "select": "value",
              "checkbox": "checked", "switch": "checked", "spinner": "value", "number": "value",
              "password": "value", "search": "value"}


class App:
    def __init__(self, spec, handlers, calls=None, headless=False):
        self.spec = spec
        self.h = handlers
        self.calls = calls or {}
        self.S = {w["id"]: dict(w.get("props", {})) for w in spec["widgets"]}
        self.kind = {w["id"]: w["kind"] for w in spec["widgets"]}
        self.remote_values = {}
        self.widgets, self.vars = {}, {}
        self.logs = []
        self.headless = headless or tk is None
        self.root = None
        self._q = queue.Queue()
        self._busy = set()
        self._edges = {}
        if not self.headless:
            try:
                self.root = tk.Tk()
            except Exception as e:       # no display (server / CI)
                print(f"[app] no display ({e}); running headless", flush=True)
                self.headless = True
        if self.root is not None:
            self._build()

    # ------------------------------------------------------------------ state
    def get(self, ident, prop, default=0):
        if ident in self.S:
            self._pull(ident)
            return self.S[ident].get(prop, default)
        return self.remote_values.get((ident, prop), default)

    def set(self, ident, prop, value):
        if ident not in self.S:
            self.remote_values[(ident, prop)] = value
            return
        old = self.S[ident].get(prop)
        self.S[ident][prop] = value
        self._push(ident, prop)
        if old != value:
            self._fire_change(ident, prop)

    def toggle(self, ident, prop):
        self.set(ident, prop, not bool(self.get(ident, prop, False)))

    def add(self, ident, prop, amount):
        cur = self.get(ident, prop, 0)
        if isinstance(cur, str) or isinstance(amount, str):
            try:
                cur = float(cur) if "." in str(cur) else int(cur or 0)
            except ValueError:
                self.set(ident, prop, f"{cur}{amount}")
                return
        self.set(ident, prop, cur + amount)

    def log(self, ident, text):
        def sub(m):
            key = m.group(1)
            if "." in key:
                e, p = key.split(".", 1)
                return str(self.get(e, p, m.group(0)))
            return str(self.get(ident, key, m.group(0)))
        line = f"[{ident}] " + re.sub(r"\{([\w.]+)\}", sub, str(text))
        self.logs.append(line)
        print(line, flush=True)
        if self.root is not None and getattr(self, "status", None) is not None:
            self.status.set(line)

    def hide(self, ident):
        self.S.get(ident, {})["visible"] = False
        w = self.widgets.get(ident)
        if w is not None:
            try:
                w.pack_forget()
            except Exception:
                pass

    def call(self, ident, name, *args):
        fn = self.calls.get(name)
        if fn:
            return fn(self, ident, *args)
        self.log(ident, f"{name}({', '.join(map(str, args))}) called - add it to CALLS in app.py")

    def send(self, ident, to=None, fields=None):
        for ch in self.spec.get("channels", []):
            if ch["dir"] == "out" and ch["src"] == ident and (not to or to in (ch["dst"], ch["id"])):
                self._post(ch, fields or ch["fields"])

    # ------------------------------------------------------------------ events
    def _run(self, fns):
        for owner, fn in fns:
            try:
                fn(self)
            except Exception as e:
                self.log(owner, f"error in behaviour: {type(e).__name__}: {e}")

    def click(self, ident):
        self._run(self.h.get("click", {}).get(ident, []))

    def key(self, name):
        self._run(self.h.get("key", {}).get(name, []))

    def edge(self, key, cond):
        """True only when a condition becomes true (for 'when x is above 5, ...' rules)."""
        was = self._edges.get(key, False)
        self._edges[key] = bool(cond)
        return bool(cond) and not was

    def _fire_change(self, ident, prop):
        k = (ident, prop)
        if k in self._busy:
            return
        self._busy.add(k)
        try:
            self._run(self.h.get("change", {}).get(k, []))
        finally:
            self._busy.discard(k)

    def message(self, src, payload):
        for k, v in payload.items():
            if k not in ("channel", "from", "_t") and v is not None:
                self.remote_values[(src, k)] = v
        self._run(self.h.get("message", {}).get(src, []) + self.h.get("message", {}).get("any", []))

    def tick(self):
        self._run(self.h.get("update", []))

    # ------------------------------------------------------------------ GUI
    def _build(self):
        spec, root = self.spec, self.root
        root.title(spec.get("title", "NLCL app"))
        w, h = (list(spec.get("size", [480, 360])) + [360])[:2]
        root.geometry(f"{int(w)}x{int(h)}")
        try:
            ttk.Style().theme_use("clam" if sys.platform.startswith("linux") else ttk.Style().theme_use())
        except Exception:
            pass
        body = ttk.Frame(root, padding=12)
        body.pack(fill="both", expand=True)
        self.status = tk.StringVar(value="")
        ttk.Label(root, textvariable=self.status, anchor="w", relief="sunken", padding=(6, 2)).pack(fill="x", side="bottom")
        frames = {None: body}
        for wd in spec["widgets"]:
            parent = frames.get(wd.get("parent"), body)
            kind, ident, p = wd["kind"], wd["id"], self.S[wd["id"]]
            if kind in ("app", "window", "dialog", "screen", "page") and wd.get("parent") is None:
                frames[ident] = body
                continue
            if kind in CONTAINERS:
                f = ttk.LabelFrame(parent, text=str(p.get("title", p.get("text", ident))), padding=8)
                f.pack(fill="x", pady=4)
                frames[ident] = f
                self.widgets[ident] = f
                continue
            widget = self._make(parent, kind, ident, p)
            if widget is not None:
                widget.pack(fill="x", pady=3)
                self.widgets[ident] = widget
                if p.get("visible") is False:
                    widget.pack_forget()
        for name in self.h.get("key", {}):
            seq = {"space": "<space>", "enter": "<Return>", "return": "<Return>", "escape": "<Escape>", "esc": "<Escape>",
                   "up": "<Up>", "down": "<Down>", "left": "<Left>", "right": "<Right>", "tab": "<Tab>"}.get(name)
            if not seq:
                seq = f"<Key-{name}>" if len(name) == 1 else None
            if seq:
                root.bind(seq, lambda e, n=name: self.key(n))

    def _make(self, parent, kind, ident, p):
        text = str(p.get("text", p.get("label", ident.replace("_", " "))))
        if kind in ("label", "title", "heading") and "text" not in p and "value" in p:
            text = str(p["value"])          # a label that shows a value
        if kind == "button":
            v = tk.StringVar(value=text)
            self.vars[(ident, "text")] = v
            return ttk.Button(parent, textvariable=v, command=lambda: self.click(ident))
        if kind in ("label", "title", "heading", "link"):
            v = tk.StringVar(value=text)
            self.vars[(ident, "text")] = v
            font = ("TkDefaultFont", 14, "bold") if kind in ("title", "heading") else None
            lab = ttk.Label(parent, textvariable=v, font=font) if font else ttk.Label(parent, textvariable=v)
            if self.h.get("click", {}).get(ident):
                lab.bind("<Button-1>", lambda e: self.click(ident))
            return lab
        if kind in ("input", "textbox", "entry", "field", "password", "search", "number"):
            row = ttk.Frame(parent)
            if p.get("label") or p.get("placeholder"):
                ttk.Label(row, text=str(p.get("label", p.get("placeholder"))) + ":").pack(side="left", padx=(0, 6))
            v = tk.StringVar(value=str(p.get("value", "")))
            self.vars[(ident, "value")] = v
            e = ttk.Entry(row, textvariable=v, show="*" if kind == "password" else "")
            e.pack(side="left", fill="x", expand=True)
            v.trace_add("write", lambda *a: self._from_widget(ident, "value", self._num(v.get(), p.get("value"))))
            e.bind("<Return>", lambda ev: self.click(ident))
            return row
        if kind == "textarea":
            t = tk.Text(parent, height=int(p.get("rows", 5)), wrap="word")
            t.insert("1.0", str(p.get("value", "")))
            t.bind("<KeyRelease>", lambda ev: self._from_widget(ident, "value", t.get("1.0", "end-1c")))
            self.vars[(ident, "value")] = t
            return t
        if kind in ("checkbox", "switch"):
            v = tk.BooleanVar(value=bool(p.get("checked", False)))
            self.vars[(ident, "checked")] = v
            tv = tk.StringVar(value=text)
            self.vars[(ident, "text")] = tv
            return ttk.Checkbutton(parent, textvariable=tv, variable=v,
                                   command=lambda: (self._from_widget(ident, "checked", v.get()), self.click(ident)))
        if kind in ("slider", "spinner"):
            v = tk.DoubleVar(value=float(p.get("value", 0) or 0))
            self.vars[(ident, "value")] = v
            row = ttk.Frame(parent)
            ttk.Label(row, text=text + ":").pack(side="left", padx=(0, 6))
            s = ttk.Scale(row, from_=float(p.get("min", 0)), to=float(p.get("max", 100)), variable=v,
                          command=lambda val: self._from_widget(ident, "value", self._num(val, p.get("value"))))
            s.pack(side="left", fill="x", expand=True)
            return row
        if kind == "progress":
            v = tk.DoubleVar(value=float(p.get("value", 0) or 0))
            self.vars[(ident, "value")] = v
            return ttk.Progressbar(parent, maximum=float(p.get("max", 100)), variable=v)
        if kind in ("dropdown", "select"):
            items = [str(x) for x in (p.get("items") or [])]
            v = tk.StringVar(value=str(p.get("value", items[0] if items else "")))
            self.vars[(ident, "value")] = v
            c = ttk.Combobox(parent, values=items, textvariable=v, state="readonly")
            c.bind("<<ComboboxSelected>>", lambda ev: (self._from_widget(ident, "value", v.get()), self.click(ident)))
            return c
        if kind in ("list", "listbox", "table"):
            lb = tk.Listbox(parent, height=int(p.get("rows", 6)))
            for it in (p.get("items") or []):
                lb.insert("end", str(it))
            lb.bind("<<ListboxSelect>>", lambda ev: (self._from_widget(
                ident, "value", lb.get(lb.curselection()[0]) if lb.curselection() else ""), self.click(ident)))
            self.vars[(ident, "items")] = lb
            return lb
        if kind in ("image", "canvas", "chart"):
            c = tk.Canvas(parent, height=int(p.get("height", 120)), background=str(p.get("color", "#f3f4f6")))
            c.bind("<Button-1>", lambda ev: self.click(ident))
            return c
        if kind in ("timer", "object", "service", "store", "model", "counter", "data"):
            return None            # state only, no visible widget
        v = tk.StringVar(value=text)
        self.vars[(ident, "text")] = v
        return ttk.Label(parent, textvariable=v)

    @staticmethod
    def _num(raw, like):
        if isinstance(like, bool) or not isinstance(like, (int, float)):
            return raw
        try:
            f = float(raw)
            return int(f) if isinstance(like, int) and f.is_integer() else f
        except (TypeError, ValueError):
            return raw

    def _from_widget(self, ident, prop, value):
        old = self.S[ident].get(prop)
        self.S[ident][prop] = value
        if old != value:
            self._fire_change(ident, prop)

    def _pull(self, ident):
        pass      # widget -> state is pushed by traces; nothing to pull

    def _push(self, ident, prop):
        v = self.vars.get((ident, prop))
        val = self.S[ident].get(prop)
        if v is None and prop in ("text", "value", "label"):
            v = self.vars.get((ident, "text")) or self.vars.get((ident, "value"))
        if v is None:
            if prop == "visible" and ident in self.widgets:
                (self.widgets[ident].pack if val else self.widgets[ident].pack_forget)()
            return
        try:
            if tk is not None and isinstance(v, tk.Text):
                v.delete("1.0", "end")
                v.insert("1.0", str(val))
            elif tk is not None and isinstance(v, tk.Listbox):
                v.delete(0, "end")
                for it in (val or []):
                    v.insert("end", str(it))
            elif str(v.get()) != str(val):
                v.set(val)
        except Exception:
            pass

    # ------------------------------------------------------------------ channels (HTTP hub)
    def _post(self, ch, fields):
        payload = {"channel": ch["id"], "from": ch["src"]}
        payload.update({f: self.get(ch["src"], f, None) for f in fields})

        def work():
            try:
                req = urllib.request.Request(ch["url"], json.dumps(payload).encode(), {"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=2).read()
            except OSError:
                pass
        threading.Thread(target=work, daemon=True).start()

    def _poll(self, ch):
        seen = None
        while True:
            try:
                msg = json.loads(urllib.request.urlopen(ch["url"], timeout=2).read() or b"{}")
                if msg and msg.get("_t") != seen:
                    seen = msg.get("_t")
                    self._q.put((ch, msg))
            except (OSError, ValueError):
                pass
            time.sleep(max(0.3, ch.get("interval_ms", 1000) / 2000.0))

    def _deliver(self, ch, msg):
        for k, v in msg.items():
            if k in ("channel", "from", "_t") or v is None:
                continue
            if k in self.S.get(ch["dst"], {}) or self.kind.get(ch["dst"]):
                self.set(ch["dst"], k, v)
        self.message(ch["src"], msg)

    # ------------------------------------------------------------------ main loop
    def start(self, net=True):
        self._run(self.h.get("start", []))
        self._timers = [[ms, time.monotonic() + ms / 1000.0, owner, fn] for owner, ms, fn in self.h.get("timers", [])]
        if net:
            for ch in self.spec.get("channels", []):
                if ch["dir"] == "in":
                    threading.Thread(target=self._poll, args=(ch,), daemon=True).start()
                else:
                    self._timers.append([ch.get("interval_ms", 1000), time.monotonic(), ch["src"],
                                         lambda app, c=ch: app._post(c, c["fields"])])

    def step(self):
        now = time.monotonic()
        for t in self._timers:
            if now >= t[1]:
                t[1] = now + t[0] / 1000.0
                self._run([(t[2], t[3])])
        self.tick()
        while not self._q.empty():
            self._deliver(*self._q.get())

    def mainloop(self):
        self.start()
        if self.root is None:
            try:
                while True:
                    self.step()
                    time.sleep(0.05)
            except KeyboardInterrupt:
                return

        def loop():
            self.step()
            self.root.after(50, loop)
        self.root.after(50, loop)
        self.root.mainloop()

    def selftest(self, seconds=0.6):
        """Build everything, fire each event once, run briefly, print the final state."""
        self.start(net=False)
        for ident in list(self.h.get("click", {})):
            self.click(ident)
        for name in list(self.h.get("key", {})):
            self.key(name)
        for src in list(self.h.get("message", {})):
            self.message(src, {})
        end = time.monotonic() + seconds
        for t in self._timers:
            t[1] = 0          # every timer fires at least once
        while time.monotonic() < end:
            self.step()
            if self.root is not None:
                self.root.update()
            time.sleep(0.02)
        print("SELFTEST OK " + json.dumps({k: v for k, v in self.S.items()}, default=str), flush=True)
        if self.root is not None:
            self.root.destroy()


def main(spec, handlers, calls=None, argv=None):
    ap = argparse.ArgumentParser(description=spec.get("title", "NLCL app"))
    ap.add_argument("--selftest", action="store_true", help="build the UI, fire every event once, exit")
    ap.add_argument("--headless", action="store_true", help="run the logic without a window")
    a = ap.parse_args(argv)
    app = App(spec, handlers, calls, headless=a.headless)
    if a.selftest:
        app.selftest()
    else:
        app.mainloop()
    return app
