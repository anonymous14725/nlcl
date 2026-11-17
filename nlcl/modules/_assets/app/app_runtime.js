// NLCL application runtime for the browser (installable PWA: Android, iOS, ChromeOS, any desktop).
// SPEC / HANDLERS / CALLS are generated; this file renders the widgets and runs the behaviours.
"use strict";
const CONTAINERS = new Set(["app", "window", "dialog", "form", "frame", "panel", "group", "page", "tab", "screen"]);

class NlclApp {
  constructor(spec, handlers, calls, mount) {
    this.spec = spec; this.h = handlers; this.calls = calls || {};
    this.S = {}; this.kind = {}; this.el = {}; this.remote = {}; this.logs = []; this.busy = new Set(); this.edges = {};
    for (const w of spec.widgets) { this.S[w.id] = Object.assign({}, w.props || {}); this.kind[w.id] = w.kind; }
    this.mount = mount || (typeof document !== "undefined" ? document.getElementById("app") : null);
    if (this.mount) this.build();
  }
  // ---- state
  get(id, prop, dflt = 0) {
    if (id in this.S) return prop in this.S[id] ? this.S[id][prop] : dflt;
    const k = id + "." + prop; return k in this.remote ? this.remote[k] : dflt;
  }
  set(id, prop, value) {
    if (!(id in this.S)) { this.remote[id + "." + prop] = value; return; }
    const old = this.S[id][prop]; this.S[id][prop] = value; this.push(id, prop);
    if (old !== value) this.fireChange(id, prop);
  }
  toggle(id, prop) { this.set(id, prop, !this.get(id, prop, false)); }
  add(id, prop, amount) {
    const cur = this.get(id, prop, 0);
    if (typeof cur === "string" && isNaN(Number(cur))) this.set(id, prop, cur + String(amount));
    else this.set(id, prop, Number(cur) + Number(amount));
  }
  log(id, text) {
    const line = "[" + id + "] " + String(text).replace(/\{([\w.]+)\}/g, (m, k) => {
      const i = k.indexOf("."); return String(i < 0 ? this.get(id, k, m) : this.get(k.slice(0, i), k.slice(i + 1), m));
    });
    this.logs.push(line); console.log(line);
    const s = typeof document !== "undefined" ? document.getElementById("status") : null; if (s) s.textContent = line;
  }
  hide(id) { if (this.S[id]) this.S[id].visible = false; if (this.el[id]) this.el[id].style.display = "none"; }
  call(id, name, ...args) {
    if (this.calls[name]) return this.calls[name](this, id, ...args);
    this.log(id, name + "(" + args.join(", ") + ") called - add it to CALLS in app.js");
  }
  send(id, to, fields) {
    for (const ch of this.spec.channels || [])
      if (ch.dir === "out" && ch.src === id && (!to || to === ch.dst || to === ch.id)) this.post(ch, fields && fields.length ? fields : ch.fields);
  }
  // ---- events
  run(list) { for (const [owner, fn] of list || []) { try { fn(this); } catch (e) { this.log(owner, "error in behaviour: " + e.message); } } }
  click(id) { this.run((this.h.click || {})[id]); }
  key(name) { this.run((this.h.key || {})[name]); }
  edge(key, cond) { const was = !!this.edges[key]; this.edges[key] = !!cond; return !!cond && !was; }
  fireChange(id, prop) {
    const k = id + "." + prop; if (this.busy.has(k)) return;
    this.busy.add(k); try { this.run((this.h.change || {})[k]); } finally { this.busy.delete(k); }
  }
  message(src, payload) {
    for (const [k, v] of Object.entries(payload || {})) if (!["channel", "from", "_t"].includes(k) && v !== null) this.remote[src + "." + k] = v;
    this.run(((this.h.message || {})[src] || []).concat((this.h.message || {}).any || []));
  }
  // ---- DOM
  build() {
    document.title = this.spec.title || "NLCL app";
    const frames = { "": this.mount };
    for (const w of this.spec.widgets) {
      const parent = frames[w.parent || ""] || this.mount, p = this.S[w.id];
      if (["app", "window", "dialog", "screen", "page"].includes(w.kind) && !w.parent) { frames[w.id] = this.mount; continue; }
      if (CONTAINERS.has(w.kind)) {
        const f = document.createElement("fieldset"); const lg = document.createElement("legend");
        lg.textContent = String(p.title || p.text || w.id); f.appendChild(lg); parent.appendChild(f); frames[w.id] = f; this.el[w.id] = f; continue;
      }
      const e = this.make(w.kind, w.id, p); if (!e) continue;
      e.classList.add("w"); parent.appendChild(e); this.el[w.id] = e; if (p.visible === false) e.style.display = "none";
    }
    document.addEventListener("keydown", (ev) => {
      if (/^(INPUT|TEXTAREA|SELECT)$/.test(ev.target.tagName)) return;
      const n = { " ": "space", Enter: "enter", Escape: "escape", ArrowUp: "up", ArrowDown: "down", ArrowLeft: "left", ArrowRight: "right" }[ev.key] || ev.key.toLowerCase();
      this.key(n);
    });
  }
  make(kind, id, p) {
    let text = String(p.text !== undefined ? p.text : (p.label !== undefined ? p.label : id.replace(/_/g, " ")));
    if (["label", "title", "heading"].includes(kind) && p.text === undefined && p.value !== undefined) text = String(p.value);
    const mk = (tag, cls) => { const e = document.createElement(tag); if (cls) e.className = cls; e.dataset.id = id; return e; };
    const fromWidget = (prop, value) => { const old = this.S[id][prop]; this.S[id][prop] = value; if (old !== value) this.fireChange(id, prop); };
    const num = (raw, like) => (typeof like === "number" && raw !== "" && !isNaN(Number(raw)) ? Number(raw) : raw);
    if (kind === "button") { const b = mk("button"); b.textContent = text; b.onclick = () => this.click(id); return b; }
    if (["label", "title", "heading", "link"].includes(kind)) {
      const l = mk(kind === "title" || kind === "heading" ? "h2" : "div", "label"); l.textContent = text; l.onclick = () => this.click(id); return l;
    }
    if (["input", "textbox", "entry", "field", "password", "search", "number"].includes(kind)) {
      const row = mk("label", "row"); const cap = p.label || p.placeholder; if (cap) row.append(String(cap) + ": ");
      const i = document.createElement("input"); i.type = kind === "password" ? "password" : (typeof p.value === "number" ? "number" : "text");
      i.value = p.value !== undefined ? p.value : ""; i.oninput = () => fromWidget("value", num(i.value, p.value));
      i.onkeydown = (ev) => { if (ev.key === "Enter") this.click(id); }; row.appendChild(i); return row;
    }
    if (kind === "textarea") { const t = mk("textarea"); t.rows = p.rows || 5; t.value = p.value || ""; t.oninput = () => fromWidget("value", t.value); return t; }
    if (kind === "checkbox" || kind === "switch") {
      const row = mk("label", "row"); const c = document.createElement("input"); c.type = "checkbox"; c.checked = !!p.checked;
      c.onchange = () => { fromWidget("checked", c.checked); this.click(id); }; const s = document.createElement("span"); s.textContent = " " + text;
      row.append(c, s); return row;
    }
    if (kind === "slider" || kind === "spinner") {
      const row = mk("label", "row"); row.append(text + ": "); const r = document.createElement("input"); r.type = "range";
      r.min = p.min !== undefined ? p.min : 0; r.max = p.max !== undefined ? p.max : 100; r.value = p.value || 0;
      r.oninput = () => fromWidget("value", Number(r.value)); row.appendChild(r); return row;
    }
    if (kind === "progress") { const g = mk("progress"); g.max = p.max || 100; g.value = p.value || 0; return g; }
    if (kind === "dropdown" || kind === "select") {
      const s = mk("select"); for (const it of p.items || []) { const o = document.createElement("option"); o.textContent = String(it); s.appendChild(o); }
      if (p.value !== undefined) s.value = p.value; s.onchange = () => { fromWidget("value", s.value); this.click(id); }; return s;
    }
    if (["list", "listbox", "table"].includes(kind)) {
      const u = mk("ul", "list"); this.fillList(u, id, p.items || []); return u;
    }
    if (["image", "canvas", "chart"].includes(kind)) { const c = mk("canvas"); c.height = p.height || 120; c.onclick = () => this.click(id); return c; }
    if (["timer", "object", "service", "store", "model", "counter", "data"].includes(kind)) return null;
    const d = mk("div", "label"); d.textContent = text; return d;
  }
  fillList(u, id, items) {
    u.innerHTML = "";
    for (const it of items) { const li = document.createElement("li"); li.textContent = String(it);
      li.onclick = () => { const old = this.S[id].value; this.S[id].value = it; if (old !== it) this.fireChange(id, "value"); this.click(id); }; u.appendChild(li); }
  }
  push(id, prop) {
    const e = this.el[id]; if (!e) return; const v = this.S[id][prop];
    if (prop === "visible") { e.style.display = v ? "" : "none"; return; }
    const inp = e.matches("input,textarea,select,progress") ? e : e.querySelector("input,textarea,select,progress");
    if (prop === "items" && e.tagName === "UL") return this.fillList(e, id, v || []);
    if (prop === "checked" && inp) { inp.checked = !!v; return; }
    if (prop === "value" && inp) { if (String(inp.value) !== String(v)) inp.value = v; return; }
    if (prop === "value" && (e.classList.contains("label") || e.tagName === "H2") && this.S[id].text === undefined) { e.textContent = String(v); return; }
    if (prop === "text" || prop === "label") {
      if (e.tagName === "BUTTON" || e.classList.contains("label") || e.tagName === "H2") e.textContent = String(v);
      else { const s = e.querySelector("span"); if (s) s.textContent = " " + String(v); }
    }
  }
  // ---- channels (HTTP hub)
  post(ch, fields) {
    const payload = { channel: ch.id, from: ch.src }; for (const f of fields) payload[f] = this.get(ch.src, f, null);
    if (typeof fetch === "undefined") return;
    fetch(ch.url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }).catch(() => {});
  }
  deliver(ch, msg) {
    for (const [k, v] of Object.entries(msg)) if (!["channel", "from", "_t"].includes(k) && v !== null) this.set(ch.dst, k, v);
    this.message(ch.src, msg);
  }
  // ---- main loop
  start(net = true) {
    this.run(this.h.start);
    this.timers = (this.h.timers || []).map(([owner, ms, fn]) => ({ owner, ms, fn, due: Date.now() + ms }));
    if (net && typeof fetch !== "undefined") for (const ch of this.spec.channels || []) {
      if (ch.dir === "out") this.timers.push({ owner: ch.src, ms: ch.interval_ms || 1000, fn: (a) => a.post(ch, ch.fields), due: Date.now() });
      else { let seen = null; setInterval(() => fetch(ch.url).then((r) => r.json()).then((m) => {
        if (m && m._t !== seen && Object.keys(m).length) { seen = m._t; this.deliver(ch, m); } }).catch(() => {}), Math.max(300, (ch.interval_ms || 1000) / 2)); }
    }
  }
  step() {
    const now = Date.now();
    for (const t of this.timers) if (now >= t.due) { t.due = now + t.ms; this.run([[t.owner, t.fn]]); }
    this.run(this.h.update);
  }
  mainloop() { this.start(); setInterval(() => this.step(), 50); }
  selftest() {      // used by `node app.js --selftest` (no DOM): fire every event once
    this.start(false);
    for (const id of Object.keys(this.h.click || {})) this.click(id);
    for (const k of Object.keys(this.h.key || {})) this.key(k);
    for (const s of Object.keys(this.h.message || {})) this.message(s, {});
    for (const t of this.timers) t.due = 0;
    this.step(); this.step();
    console.log("SELFTEST OK " + JSON.stringify(this.S));
  }
}
if (typeof module !== "undefined") module.exports = { NlclApp };
