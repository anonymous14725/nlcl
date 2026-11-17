// ---------------------------------------------------------------- NLCL 2D preview runtime
// World units: x to the right, y up.  Entities are axis-aligned boxes (AABB).
// Runs in a browser (canvas) or headless under Node: `node preview.js [seconds]`.
const W = {
  t: 0, ents: [], byId: {}, remoteVals: {}, logs: [], held: new Set(), pressed: [], clicks: [],
  pairs: new Set(), chans: [], hub: "not used", lastBool: {}, spawned: 0, paused: false, headless: false,
};
const GRAVITY = 20;

function log(o, s) {
  const line = "[" + W.t.toFixed(1) + "s] " + (o ? "[" + o.id + "] " : "") + s;
  W.logs.push(line);
  if (W.logs.length > 300) W.logs.shift();
  console.log(line);
}
function tog(v) { return typeof v === "number" ? (v ? 0 : 1) : !v; }
function num(v, d) { const n = Number(v); return Number.isFinite(n) ? n : d; }
function get(id, k) {
  const e = W.byId[id];
  if (!e || e.dead) return 0;
  if (k in e.p) return e.p[k];
  if (k === "x" || k === "y" || k === "z") return e[k];
  return 0;
}
function remote(id, k, d) { const v = W.remoteVals[id + "." + k]; return v === undefined ? d : v; }

function makeEntity(d, id) {
  const o = JSON.parse(JSON.stringify(d));
  o.id = id || d.id;
  o.tmpl = d.id;
  o.vx = 0; o.vy = 0; o.grounded = false; o.dead = false;
  o.start = { x: o.x, y: o.y, p: JSON.parse(JSON.stringify(o.p)) };
  o.maxHealth = typeof o.p.health === "number" && o.p.health > 0 ? o.p.health : null;
  o.beh = BEH[d.id] || {};
  o.timers = (o.beh.timers || []).map(() => 0);
  o.edges = (o.beh.update || []).map(() => false);
  o.prev = {};
  o.spawnAcc = 0; o.spawnN = 0;
  return o;
}
function addEntity(o) { W.ents.push(o); W.byId[o.id] = o; snapshot(o); return o; }
function snapshot(o) { for (const c of (o.beh.change || [])) o.prev[c.k] = val(o, c.k); }
function val(o, k) { return k in o.p ? o.p[k] : (k === "x" || k === "y" || k === "z" ? o[k] : undefined); }

const w = {   // API used by generated behaviour code
  log, get, remote, tog,
  destroy(o) { if (!o.dead) { o.dead = true; log(o, "destroyed"); } },
  move(o, dx, dy, dz) { o.x += num(dx, 0); o.y += num(dy, 0); o.z += num(dz, 0); },
  send(o, to, fields) {
    let sent = false;
    for (const ch of W.chans) {
      if (ch.src !== o.id) continue;
      if (to && to !== ch.dst && to !== ch.id) continue;
      emit(ch, fields && fields.length ? fields : ch.fields);
      sent = true;
    }
    // no channel declared, but the receiver lives in this game: deliver directly
    if (!sent && to && W.byId[to] && !W.byId[to].dead) {
      const payload = {};
      for (const f of fields || []) payload[f] = get(o.id, f);
      deliver({ id: o.id + "_to_" + to, src: o.id, dst: to, fields: fields || [], dstLocal: true, srcLocal: true }, payload);
    }
  },
  call(o, name, args) {
    if (name === "jump") {
      if (o.gravity) { if (o.grounded) { o.vy = num(o.p.jump_force, 8); o.grounded = false; } }
      else o.y += 1;
    } else if (name === "spawn" || name === "fire" || name === "shoot") {
      const t = args && args.length ? String(args[0]) : (o.p.spawn || o.spawnDefault || "");
      if (t) spawn(t, o.x + o.w / 2 + 0.5, o.y, o);
      else if (!o.warnedSpawn) { o.warnedSpawn = true; log(o, "spawn: nothing to spawn (set the 'spawn' property)"); }
    } else if (name === "reset" || name === "respawn" || name === "restart") {
      o.x = o.start.x; o.y = o.start.y; o.vx = o.vy = 0;
    } else if (o.beh.calls && o.beh.calls[name]) {
      o.beh.calls[name](o, w, args || []);
    } else log(o, name + "(" + (args || []).join(", ") + ") called");
  },
};

function spawn(tmpl, x, y, by) {
  const d = DATA.entities.find((e) => e.id === tmpl);
  if (!d) { log(by, "cannot spawn '" + tmpl + "': unknown entity"); return null; }
  if (W.ents.filter((e) => !e.dead).length >= 400) return null;   // runaway spawning guard
  W.spawned++;
  const o = makeEntity(d, tmpl + "_" + W.spawned);
  o.x = x; o.y = y; o.controls = false;
  addEntity(o);
  fire(o, o.beh.start);
  return o;
}

function fire(o, list, dt) {
  if (!list) return;
  for (const b of list) {
    if (o.dead) return;
    try { if (!b.c || b.c(o, w)) b.f(o, w, dt || 0); }
    catch (err) { log(o, "behaviour error: " + err.message); }
  }
}

// ---------------------------------------------------------------- physics
function box(o) { return { l: o.x - o.w / 2, r: o.x + o.w / 2, b: o.y - o.h / 2, t: o.y + o.h / 2 }; }
function overlap(a, b) {
  const A = box(a), B = box(b);
  return A.l < B.r && A.r > B.l && A.b < B.t && A.t > B.b;
}
function solids(o) { return W.ents.filter((s) => s !== o && !s.dead && s.solid && s.visual); }
function moveAxis(o, dx, dy) {
  o.x += dx;
  for (const s of solids(o)) if (overlap(o, s)) { o.x -= dx; o.vx = 0; break; }
  o.y += dy;
  for (const s of solids(o)) {
    if (!overlap(o, s)) continue;
    const S = box(s);
    if (dy < 0) { o.y = S.t + o.h / 2; o.grounded = true; } else if (dy > 0) o.y = S.b - o.h / 2;
    else o.y -= dy;
    o.vy = 0;
    break;
  }
}

function step(dt) {
  if (W.paused) return;
  W.t += dt;
  const live = W.ents.filter((o) => !o.dead);
  for (const o of live) { o.px = o.x; o.py = o.y; }
  for (const o of live) {
    const speed = num(o.p.speed, 5);
    let dx = 0, dy = 0;
    if (o.controls) {
      const h = (W.held.has("right") || W.held.has("d") ? 1 : 0) - (W.held.has("left") || W.held.has("a") ? 1 : 0);
      const v = (W.held.has("up") || W.held.has("w") ? 1 : 0) - (W.held.has("down") || W.held.has("s") ? 1 : 0);
      dx += h * speed * dt;
      if (o.gravity) {
        if (o.grounded && (W.pressed.includes("space") || W.pressed.includes("up") || W.pressed.includes("w"))) {
          o.vy = num(o.p.jump_force, 8); o.grounded = false;
        }
      } else dy += v * speed * dt;
    }
    if (o.autoMove) {
      let dir = [1, 0];
      if (Array.isArray(o.p.direction) && o.p.direction.length >= 2) dir = o.p.direction;
      const len = Math.hypot(dir[0], dir[1]) || 1;
      dx += dir[0] / len * speed * dt; dy += dir[1] / len * speed * dt;
      if (Math.abs(o.x) > 500 || Math.abs(o.y) > 500) o.dead = true;
    }
    const tgt = typeof o.p.target === "string" ? W.byId[o.p.target] : null;
    if (tgt && !tgt.dead && tgt !== o) {
      const ex = tgt.x - o.x, ey = tgt.y - o.y, d = Math.hypot(ex, ey);
      if (d > 0.05) { dx += ex / d * Math.min(d, speed * dt); if (!o.gravity) dy += ey / d * Math.min(d, speed * dt); }
    }
    if (o.gravity) { o.vy -= GRAVITY * dt; dy += o.vy * dt; o.grounded = false; }
    if (dx || dy) { if (o.solid && !o.gravity) { o.x += dx; o.y += dy; } else moveAxis(o, dx, dy); }
    if (o.y < -100) { o.dead = true; log(o, "fell out of the world"); }
  }
  // children are carried along by their parent (parents first, any depth)
  const depth = (o) => { let d = 0, q = o; while (q && q.parent && d < 50) { q = W.byId[q.parent]; d++; } return d; };
  for (const o of live.filter((c) => c.parent && W.byId[c.parent]).sort((a, b) => depth(a) - depth(b))) {
    const par = W.byId[o.parent];
    if (par.dead) { if (!o.dead) { o.dead = true; } continue; }
    o.x += par.x - par.px; o.y += par.y - par.py;
  }
  // keyboard / mouse
  for (const k of W.pressed) for (const o of W.ents.slice()) {
    if (o.dead) continue;
    fire(o, (o.beh.input || []).filter((b) => b.key === k || b.key === "any"), dt);
  }
  for (const id of W.clicks) for (const o of W.ents.slice()) {
    if (o.dead) continue;
    fire(o, (o.beh.click || []).filter((b) => b.id === id || b.id === "mouse"), dt);
  }
  // timers
  for (const o of W.ents.slice()) {
    if (o.dead) continue;
    (o.beh.timers || []).forEach((b, i) => {
      o.timers[i] += dt * 1000;
      if (o.timers[i] >= b.ms) { o.timers[i] -= b.ms; fire(o, [b], dt); }
    });
  }
  // update behaviours: unconditional every frame; conditional ones fire when the condition becomes true
  for (const o of W.ents.slice()) {
    if (o.dead) continue;
    (o.beh.update || []).forEach((b, i) => {
      if (o.dead) return;
      if (!b.c) { fire(o, [b], dt); return; }
      let ok = false;
      try { ok = !!b.c(o, w); } catch (err) { ok = false; }
      if (ok && !o.edges[i]) { o.edges[i] = true; fire(o, [{ f: b.f }], dt); }
      else if (!ok) o.edges[i] = false;
    });
  }
  // collisions (AABB, fired once when two entities start touching)
  const now = new Set();
  const vis = W.ents.filter((o) => !o.dead && o.visual && !o.noHit && o.p.collision !== false);
  for (let i = 0; i < vis.length; i++) for (let j = i + 1; j < vis.length; j++) {
    const a = vis[i], b = vis[j];
    if (a.parent === b.id || b.parent === a.id) continue;   // a part does not hit its own body
    if (!overlap(a, b) && !touching(a, b)) continue;
    const key = a.id + "|" + b.id;
    now.add(key);
    if (!W.pairs.has(key)) {
      // rules with a partner ("w": entity id or kind) only fire for that partner
      const hit = (o, other) => (o.beh.collision || []).filter((r) => !r.w || r.w === other.id || r.w === other.kind);
      W.other = b; fire(a, hit(a, b), dt);
      W.other = a; fire(b, hit(b, a), dt);
    }
  }
  W.pairs = now;
  // spawners
  for (const o of W.ents.slice()) {
    if (o.dead || !o.p.spawn || !(num(o.p.spawn_interval_ms, 0) > 0)) continue;
    o.spawnAcc += dt * 1000;
    if (o.spawnAcc >= o.p.spawn_interval_ms && o.spawnN < num(o.p.max_spawn, 10)) {
      o.spawnAcc = 0; o.spawnN++;
      spawn(String(o.p.spawn), o.x, o.y, o);
    }
  }
  // change detection
  for (const o of W.ents.slice()) {
    if (o.dead) continue;
    for (const c of (o.beh.change || [])) {
      const v = val(o, c.k);
      if (JSON.stringify(v) !== JSON.stringify(o.prev[c.k])) { o.prev[c.k] = v; fire(o, [c], dt); }
    }
  }
  tickChannels(dt);
  W.pressed = []; W.clicks = [];
  for (const o of W.ents.slice()) if (o.dead) delete W.byId[o.id];
  W.ents = W.ents.filter((o) => !o.dead);
}
function touching(a, b) {   // resting contact (e.g. standing on a platform) counts as touching
  const A = box(a), B = box(b), eps = 0.02;
  return A.l < B.r && A.r > B.l && Math.abs(A.b - B.t) < eps || A.l < B.r && A.r > B.l && Math.abs(A.t - B.b) < eps;
}

// ---------------------------------------------------------------- channels (NLCL hub)
function emit(ch, fields) {
  const src = W.byId[ch.src];
  if (!src) return;
  const payload = {};
  for (const f of fields) payload[f] = get(ch.src, f);
  if (ch.dstLocal) { deliver(ch, payload); return; }
  if (W.headless) { log(src, "-> " + ch.id + " " + JSON.stringify(payload)); return; }
  fetch(ch.url, { method: "POST", headers: { "Content-Type": "text/plain" },
    body: JSON.stringify(Object.assign({ channel: ch.id, from: ch.src }, payload)) })
    .then(() => { W.hub = "online"; }).catch(() => { W.hub = "offline (run python/main.py)"; });
}
function deliver(ch, msg) {
  let anyBool = false, rising = false;
  for (const [k, v] of Object.entries(msg)) {
    if (k === "channel" || k === "from" || k.startsWith("_")) continue;
    W.remoteVals[ch.src + "." + k] = v;
    if (typeof v === "boolean") {
      anyBool = true;
      const key = ch.src + "." + k;
      if (v && !W.lastBool[key]) rising = true;
      W.lastBool[key] = v;
    }
  }
  const dst = W.byId[ch.dst];
  if (dst) for (const [k, v] of Object.entries(msg)) if (k in dst.p) dst.p[k] = v;
  for (const o of W.ents.slice()) {
    if (o.dead) continue;
    fire(o, (o.beh.message || []).filter((b) => b.src ? b.src.includes(ch.src) : o === dst));
    if (rising || !anyBool) fire(o, (o.beh.remote || []).filter((b) => b.src.includes(ch.src)));
  }
}
function tickChannels(dt) {
  for (const ch of W.chans) {
    ch.acc = (ch.acc || 0) + dt * 1000;
    if (ch.srcLocal) {
      if (ch.acc >= ch.interval) { ch.acc = 0; emit(ch, ch.fields); }
    } else if (ch.dstLocal && !W.headless && ch.acc >= Math.max(200, ch.interval / 2) && !ch.busy) {
      ch.acc = 0; ch.busy = true;
      fetch(ch.url).then((r) => r.json()).then((m) => {
        W.hub = "online";
        if (m && m._t !== undefined && m._t !== ch.seen) { ch.seen = m._t; deliver(ch, m); }
      }).catch(() => { W.hub = "offline (run python/main.py)"; }).finally(() => { ch.busy = false; });
    }
  }
}

function init() {
  W.ents = []; W.byId = {}; W.pairs = new Set(); W.t = 0;
  for (const d of DATA.entities) if (!d.template) addEntity(makeEntity(d));   // templates only spawn
  W.chans = DATA.channels.map((c) => Object.assign({}, c));
  if (W.chans.some((c) => !(c.srcLocal && c.dstLocal))) W.hub = "connecting...";
  for (const o of W.ents.slice()) fire(o, o.beh.start);
}

// ---------------------------------------------------------------- headless (Node) mode
if (typeof window === "undefined") {
  W.headless = true;
  const secs = Number((typeof process !== "undefined" && process.argv[2]) || 5);
  init();
  const keys = [...new Set(Object.values(BEH).flatMap((b) => (b.input || []).map((x) => x.key)))].filter((k) => k !== "any");
  const clicks = [...new Set(Object.values(BEH).flatMap((b) => (b.click || []).map((x) => x.id)))];
  const dt = 1 / 60;
  let next = 0.5;
  const script = keys.map((k) => ["key", k]).concat(clicks.map((c) => ["click", c]));
  for (let i = 0; i < secs / dt; i++) {
    if (script.length && W.t >= next) {   // exercise every bound key / click once, 0.5 s apart
      const [type, k] = script.shift();
      console.log("[" + W.t.toFixed(1) + "s] (test input: " + type + " " + k + ")");
      if (type === "key") W.pressed.push(k); else W.clicks.push(k);
      next += 0.5;
    }
    step(dt);
  }
  console.log("--- state after " + secs + " s ---");
  for (const o of W.ents.slice()) console.log(o.id + " (" + o.kind + ") x=" + o.x.toFixed(2) + " y=" + o.y.toFixed(2) + " " + JSON.stringify(o.p));
  const gone = DATA.entities.filter((d) => !d.template && !W.byId[d.id]).map((d) => d.id);
  if (gone.length) console.log("destroyed: " + gone.join(", "));
}

// ---------------------------------------------------------------- browser mode
if (typeof window !== "undefined") {
  const cv = document.getElementById("view"), g = cv.getContext("2d");
  let view = { cx: 0, cy: 0, scale: 40 };
  function fit() {
    cv.width = cv.clientWidth * devicePixelRatio; cv.height = cv.clientHeight * devicePixelRatio;
    const vis = W.ents.filter((o) => o.visual);
    if (!vis.length) return;
    let l = Infinity, r = -Infinity, b = Infinity, t = -Infinity;
    for (const o of vis) { const B = box(o); l = Math.min(l, B.l); r = Math.max(r, B.r); b = Math.min(b, B.b); t = Math.max(t, B.t); }
    l -= 3; r += 3; b -= 3; t += 3;
    view.cx = (l + r) / 2; view.cy = (b + t) / 2;
    view.scale = Math.min(cv.width / (r - l), cv.height / (t - b), 80 * devicePixelRatio);
  }
  const sx = (x) => cv.width / 2 + (x - view.cx) * view.scale;
  const sy = (y) => cv.height / 2 - (y - view.cy) * view.scale;
  const KEYMAP = { " ": "space", Enter: "enter", ArrowUp: "up", ArrowDown: "down", ArrowLeft: "left", ArrowRight: "right",
    Escape: "escape", Shift: "shift", Control: "ctrl", Alt: "alt", Tab: "tab", Backspace: "backspace" };
  const keyName = (e) => KEYMAP[e.key] || e.key.toLowerCase();
  addEventListener("keydown", (e) => {
    const k = keyName(e);
    if (["space", "up", "down", "left", "right"].includes(k)) e.preventDefault();
    if (!W.held.has(k)) W.pressed.push(k);
    W.held.add(k);
  });
  addEventListener("keyup", (e) => W.held.delete(keyName(e)));
  addEventListener("blur", () => W.held.clear());
  cv.addEventListener("mousedown", (e) => {
    const r = cv.getBoundingClientRect();
    const x = view.cx + ((e.clientX - r.left) * devicePixelRatio - cv.width / 2) / view.scale;
    const y = view.cy - ((e.clientY - r.top) * devicePixelRatio - cv.height / 2) / view.scale;
    W.clicks.push("mouse");
    for (const o of W.ents.slice()) { const B = box(o); if (o.visual && x >= B.l && x <= B.r && y >= B.b && y <= B.t) W.clicks.push(o.id); }
  });
  document.getElementById("pause").onclick = (e) => { W.paused = !W.paused; e.target.textContent = W.paused ? "Resume" : "Pause"; };
  document.getElementById("restart").onclick = () => { W.logs = []; init(); fit(); };
  addEventListener("resize", fit);

  function draw() {
    g.fillStyle = "#14161c"; g.fillRect(0, 0, cv.width, cv.height);
    const cam = W.ents.find((o) => o.kind === "camera" && typeof o.p.follow === "string" && W.byId[o.p.follow]);
    if (cam) { view.cx = W.byId[cam.p.follow].x; view.cy = W.byId[cam.p.follow].y; }
    g.strokeStyle = "#1f232c"; g.lineWidth = 1;
    const step1 = view.scale;
    for (let x = sx(Math.floor(view.cx - cv.width / step1)); x < cv.width; x += step1) { g.beginPath(); g.moveTo(x, 0); g.lineTo(x, cv.height); g.stroke(); }
    for (let y = sy(Math.ceil(view.cy + cv.height / step1)); y < cv.height; y += step1) { g.beginPath(); g.moveTo(0, y); g.lineTo(cv.width, y); g.stroke(); }
    const font = Math.max(10, 12 * devicePixelRatio);
    g.font = font + "px system-ui, sans-serif"; g.textAlign = "center";
    for (const o of W.ents.slice()) {
      if (!o.visual || o.p.visible === false) continue;
      const B = box(o), x = sx(B.l), y = sy(B.t), wd = o.w * view.scale, ht = o.h * view.scale;
      g.fillStyle = typeof o.p.color === "string" && o.p.color ? o.p.color : o.color;
      g.beginPath();
      if (o.shape === "sphere") g.ellipse(x + wd / 2, y + ht / 2, wd / 2, ht / 2, 0, 0, Math.PI * 2);
      else if (o.shape === "capsule" && g.roundRect) g.roundRect(x, y, wd, ht, Math.min(wd, ht) / 2);
      else g.rect(x, y, wd, ht);
      g.fill();
      if (o.controls) { g.strokeStyle = "#ffffffaa"; g.lineWidth = 2; g.stroke(); }
      if (!o.noHit) { g.fillStyle = "#cfd3dc"; g.fillText(o.id, x + wd / 2, y - 6 * devicePixelRatio); }
      if (o.maxHealth && typeof o.p.health === "number") {
        const f = Math.max(0, Math.min(1, o.p.health / o.maxHealth));
        const bw = Math.max(wd, 40 * devicePixelRatio), bx = x + wd / 2 - bw / 2, by = y - 6 * devicePixelRatio - font - 6 * devicePixelRatio;
        g.fillStyle = "#3a3f4b"; g.fillRect(bx, by, bw, 5 * devicePixelRatio);
        g.fillStyle = f > 0.5 ? "#22c55e" : f > 0.25 ? "#eab308" : "#ef4444"; g.fillRect(bx, by, bw * f, 5 * devicePixelRatio);
      }
    }
  }
  const esc = (t) => String(t).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  let lastPanel = 0;
  function panel() {
    const rows = W.ents.filter((o) => !o.noHit).map((o) => "<tr><td>" + o.id + "</td><td>" + o.kind + "</td><td>" + o.x.toFixed(1) + ", " + o.y.toFixed(1) +
      "</td><td>" + esc(Object.entries(o.p).map(([k, v]) => k + "=" + (typeof v === "number" ? +v.toFixed(2) : JSON.stringify(v))).join(" ")) + "</td></tr>");
    document.getElementById("ents").innerHTML = "<tr><th>entity</th><th>kind</th><th>x, y</th><th>properties</th></tr>" + rows.join("");
    document.getElementById("log").textContent = W.logs.slice(-60).join("\n");
    document.getElementById("time").textContent = W.t.toFixed(1) + " s";
    document.getElementById("hub").textContent = W.chans.length ? "hub: " + W.hub : "";
  }
  let last = performance.now();
  function frame(now) {
    const dt = Math.min(0.05, (now - last) / 1000); last = now;
    step(dt); draw();
    if (now - lastPanel > 250) { lastPanel = now; panel(); }
    requestAnimationFrame(frame);
  }
  init(); fit(); requestAnimationFrame(frame);
}
