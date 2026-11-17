// ===========================================================================
// NLCL three.js runtime (static part, shared by every generated 3D scene).
// Expects these constants to be declared above it in the same module:
//   PROJECT, DEFS, CHANNELS, BEHAVIOURS, USER_FUNCTIONS, HUB_POLL_MS
// ===========================================================================

const DEG = Math.PI / 180;
const GEOMETRY_KEYS = new Set(["size", "radius", "height", "tube", "length", "subtype"]);
const AXES = { x: 0, y: 1, z: 2 };
const ROT_AXES = { angle: 2, angle_z: 2, angle_x: 0, angle_y: 1 };   // "angle" props are rotation components (degrees)   // bare x/y/z in behaviours address the position
// props that describe geometry/appearance rather than live data (hidden in the HUD)
const NON_DATA = new Set(["position", "rotation", "scale", "size", "color", "opacity", "wireframe",
  "visible", "shape", "radius", "height", "width", "depth", "length", "tube", "text", "intensity",
  "fov", "subtype", "url", "target", "segments", "material", "texture", "units", "movable",
  "collision", "gravity", "emissive", "metalness", "roughness", "font_size", "model"]);
const DEFAULT_COLORS = {
  box: "#8fa3bf", sphere: "#d9a441", cylinder: "#6fb08f", cone: "#c46b6b", torus: "#a57fd1",
  plane: "#7d8794", ground: "#4f5b4a", building: "#b7bec9", tank: "#9aa7b5", pipe: "#7f8c99",
  arm: "#f0a020", twin: "#5aa7d6", model: "#9aa0a6", room: "#5c6470", light: "#ffffff",
};

// ---------------------------------------------------------------- helpers
function num(v, d = 0) { const n = Number(v); return Number.isFinite(n) ? n : d; }
// position-like vector: missing components are 0
function pos3(v, d = [0, 0, 0]) {
  if (Array.isArray(v)) { const a = v.slice(0, 3).map(x => num(x, 0)); while (a.length < 3) a.push(0); return a; }
  if (v !== undefined && v !== null && v !== "" && Number.isFinite(Number(v))) { const n = Number(v); return [n, n, n]; }
  return d.slice();
}
// size-like vector: a single number means a cube, missing components repeat the last one
function size3(v, d = [1, 1, 1]) {
  if (Array.isArray(v) && v.length) { const a = v.slice(0, 3).map(x => Math.max(0.001, num(x, 1))); while (a.length < 3) a.push(a[a.length - 1]); return a; }
  if (v !== undefined && v !== null && v !== "" && Number.isFinite(Number(v))) { const n = Math.max(0.001, Number(v)); return [n, n, n]; }
  return d.slice();
}
function clamp(v, a, b) { return Math.min(b, Math.max(a, v)); }
function toColor(v, fallback = "#8fa3bf") {
  const c = new THREE.Color(fallback);
  if (v === undefined || v === null || v === "" || typeof v === "boolean") return c;
  if (typeof v === "number") return c.setHex(Math.max(0, Math.floor(v)) & 0xffffff);
  let s = String(v).trim().toLowerCase();
  if (s.startsWith("0x")) s = "#" + s.slice(2);
  if (/^[0-9a-f]{6}$/.test(s)) s = "#" + s;
  if (!s.startsWith("#") && !s.startsWith("rgb") && !s.startsWith("hsl")) s = s.replace(/[\s_-]+/g, "").replace("grey", "gray");
  if (s.startsWith("#") || s.startsWith("rgb") || s.startsWith("hsl") || THREE.Color.NAMES[s] !== undefined) {
    try { c.setStyle(s); } catch (e) { /* keep fallback */ }
  }
  return c;
}
// 10 degC -> blue, 40 degC -> red
function temperatureColor(t) { const k = clamp((num(t) - 10) / 30, 0, 1); return new THREE.Color().setHSL(0.66 * (1 - k), 0.85, 0.5); }
function esc(s) { return String(s).replace(/[&<>"]/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[ch])); }
function fmt(v) {
  if (typeof v === "number") return Number.isInteger(v) ? String(v) : v.toFixed(2);
  if (Array.isArray(v)) return "[" + v.map(fmt).join(", ") + "]";
  if (v === null || v === undefined) return "-";
  return String(v);
}

// ---------------------------------------------------------------- renderer / scene
const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
renderer.setSize(window.innerWidth, window.innerHeight);
renderer.shadowMap.enabled = true;
document.body.appendChild(renderer.domElement);
const scene = new THREE.Scene();
scene.background = new THREE.Color(0x1f242d);
const camera = new THREE.PerspectiveCamera(50, window.innerWidth / window.innerHeight, 0.05, 5000);
camera.position.set(6, 5, 8);
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
const hasLights = DEFS.some(d => d.shape === "light");
scene.add(new THREE.HemisphereLight(0xffffff, 0x3a4250, hasLights ? 0.6 : 0.9));
if (!hasLights) {
  const sun = new THREE.DirectionalLight(0xffffff, 1.6);
  sun.position.set(6, 12, 8);
  sun.castShadow = true;
  scene.add(sun);
}
const grid = new THREE.GridHelper(40, 40, 0x55606e, 0x323946);
scene.add(grid);
window.addEventListener("resize", () => {
  camera.aspect = window.innerWidth / window.innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(window.innerWidth, window.innerHeight);
});

// ---------------------------------------------------------------- HUD / log
const hudEl = document.getElementById("hud");
const logEl = document.getElementById("log");
const badgeEl = document.getElementById("badge");
const LOG = [];
let lastLine = "", lastLineAt = 0;
function hudLog(line) {
  // per-frame behaviours may log the same line 60x per second: show it at most once per second
  const now = performance.now();
  if (line === lastLine && now - lastLineAt < 1000) return;
  lastLine = line; lastLineAt = now;
  console.log(line);
  LOG.push(line);
  if (LOG.length > 12) LOG.shift();
  if (logEl) logEl.innerHTML = LOG.map(esc).join("<br>");
}

// ---------------------------------------------------------------- remote data
const REMOTE = {};        // sender ident -> {field: latest value}
const REMOTE_FIELD = {};  // field -> latest value from any sender
function remote(ent, prop) {
  const r = REMOTE[ent];
  if (r && r[prop] !== undefined) return r[prop];
  const o = OBJS.get(ent);
  return o ? o.get(prop) : 0;
}

// ---------------------------------------------------------------- objects
const OBJS = new Map();   // ident -> NObj
const VIEW = { cam: null }; // camera entity that drives the view (set in setupView)
const NULL_OBJ = { get: () => 0, num: () => 0, alive: false };
function obj(ident) { return OBJS.get(ident) || NULL_OBJ; }

function std(color, extra = {}) {
  return new THREE.MeshStandardMaterial(Object.assign({ color: new THREE.Color(color), roughness: 0.55, metalness: 0.1 }, extra));
}

function labelTexture(text, color = "#ffffff") {
  const canvas = document.createElement("canvas");
  const ctx2 = canvas.getContext("2d");
  const font = "bold 48px system-ui, sans-serif";
  ctx2.font = font;
  const w = Math.max(64, Math.ceil(ctx2.measureText(text).width) + 32);
  canvas.width = w; canvas.height = 72;
  ctx2.font = font;
  ctx2.fillStyle = "rgba(20,24,32,0.75)";
  ctx2.fillRect(0, 0, w, 72);
  ctx2.fillStyle = color;
  ctx2.textBaseline = "middle";
  ctx2.fillText(text, 16, 38);
  const tex = new THREE.CanvasTexture(canvas);
  tex.colorSpace = THREE.SRGBColorSpace;
  return { tex, aspect: w / 72 };
}

class NObj {
  constructor(def) {
    this.id = def.id; this.ident = def.ident; this.name = def.name; this.kind = def.kind; this.shape = def.shape;
    this.p = JSON.parse(JSON.stringify(def.props || {}));
    this.initial = JSON.parse(JSON.stringify(this.p));
    this.alive = true;
    this.recv = new Set();        // fields received over channels
    this.colorMats = [];          // materials tinted by color / temperature
    this.dataMesh = null;         // mesh stretched by level/value/distance
    this.dataHeight = 1;
    this.light = null;
    this.label = null;
    this.timers = [];
    this._changing = new Set();
    this.node = new THREE.Group();
    this.node.name = this.ident;
    this.node.userData.nobj = this;
    this.buildOwn();
    this.apply();
  }
  // build() and remember what it added, so a later size/radius change can rebuild the geometry
  // without touching child objects attached to this node
  buildOwn() {
    const before = new Set(this.node.children);
    this.build();
    this.own = this.node.children.filter(c => !before.has(c));
  }
  rebuild() {
    for (const c of this.own || []) {
      this.node.remove(c);
      c.traverse(o => { if (o.geometry) o.geometry.dispose(); });
    }
    if (this.light && this.light.target && this.light.target.parent) this.light.target.parent.remove(this.light.target);
    this.colorMats = []; this.dataMesh = null; this.dataHeight = 1; this.light = null; this.label = null;
    this.bulb = null; this.yaw = this.shoulderJ = this.elbowJ = null; this._labelText = null;
    this.buildOwn();
  }

  // ---- geometry per shape
  mesh(geo, mat, parent = this.node) {
    const m = new THREE.Mesh(geo, mat);
    m.castShadow = true; m.receiveShadow = true;
    m.userData.nobj = this;
    parent.add(m);
    return m;
  }
  build() {
    const p = this.p, s = this.shape;
    const col = DEFAULT_COLORS[s] || DEFAULT_COLORS.box;
    const rad = num(p.radius, typeof p.size === "number" ? p.size / 2 : 0.5);
    const h = num(p.height, Array.isArray(p.size) ? size3(p.size)[1] : 1);
    if (s === "box") {
      const sz = size3(p.size);
      const mat = std(col); this.colorMats.push(mat);
      this.dataMesh = this.mesh(new THREE.BoxGeometry(sz[0], sz[1], sz[2]), mat); this.dataHeight = sz[1];
    } else if (s === "sphere") {
      const mat = std(col); this.colorMats.push(mat);
      this.dataMesh = this.mesh(new THREE.SphereGeometry(rad, 32, 16), mat); this.dataHeight = rad * 2;
    } else if (s === "cylinder") {
      const mat = std(col); this.colorMats.push(mat);
      this.dataMesh = this.mesh(new THREE.CylinderGeometry(rad, rad, h, 32), mat); this.dataHeight = h;
    } else if (s === "cone") {
      const mat = std(col); this.colorMats.push(mat);
      this.dataMesh = this.mesh(new THREE.ConeGeometry(rad, h, 32), mat); this.dataHeight = h;
    } else if (s === "torus") {
      const mat = std(col); this.colorMats.push(mat);
      this.dataMesh = this.mesh(new THREE.TorusGeometry(rad, num(p.tube, rad * 0.3), 16, 64), mat); this.dataHeight = rad * 2;
    } else if (s === "plane" || s === "ground") {
      const sz = size3(p.size, s === "ground" ? [20, 0, 20] : [4, 0, 4]);
      const geo = new THREE.PlaneGeometry(sz[0], sz[2]); geo.rotateX(-Math.PI / 2);
      const mat = std(col, { side: THREE.DoubleSide, roughness: 0.9 }); this.colorMats.push(mat);
      const m = this.mesh(geo, mat); m.castShadow = false;
    } else if (s === "room") {
      const sz = size3(p.size, [10, 3, 8]);
      const geo = new THREE.PlaneGeometry(sz[0], sz[2]); geo.rotateX(-Math.PI / 2);
      const mat = std(col, { side: THREE.DoubleSide, roughness: 0.9 }); this.colorMats.push(mat);
      const floor = this.mesh(geo, mat); floor.castShadow = false;
      const edges = new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.BoxGeometry(sz[0], sz[1], sz[2])),
        new THREE.LineBasicMaterial({ color: 0x9fb3c8 }));
      edges.position.y = sz[1] / 2;
      this.node.add(edges);
    } else if (s === "building") {
      const sz = size3(p.size, [10, 6, 8]);
      const mat = std(col); this.colorMats.push(mat);
      const m = this.mesh(new THREE.BoxGeometry(sz[0], sz[1], sz[2]), mat);
      m.position.y = sz[1] / 2;
      m.add(new THREE.LineSegments(new THREE.EdgesGeometry(m.geometry), new THREE.LineBasicMaterial({ color: 0x30343b })));
    } else if (s === "tank") {
      const r = num(p.radius, 1), th = num(p.height, 3);
      const shell = this.mesh(new THREE.CylinderGeometry(r, r, th, 40, 1, true),
        std(col, { transparent: true, opacity: 0.3, side: THREE.DoubleSide, depthWrite: false }));
      shell.position.y = th / 2;
      const lmat = std("#2f7fd8", { roughness: 0.2 }); this.colorMats.push(lmat);
      this.dataMesh = this.mesh(new THREE.CylinderGeometry(r * 0.92, r * 0.92, th, 40), lmat);
      this.dataMesh.userData.anchorBottom = true;
      this.dataHeight = th;
      this.mesh(new THREE.CylinderGeometry(r * 1.02, r * 1.02, 0.08, 40), std("#59636e")).position.y = 0.04;
    } else if (s === "pipe") {
      const r = num(p.radius, 0.15), len = num(p.length, 4);
      const geo = new THREE.CylinderGeometry(r, r, len, 20); geo.rotateZ(Math.PI / 2);
      const mat = std(col, { metalness: 0.6, roughness: 0.35 }); this.colorMats.push(mat);
      this.mesh(geo, mat);
    } else if (s === "arm") {
      const mat = std(col); this.colorMats.push(mat);
      const dark = std("#3b4048");
      this.mesh(new THREE.CylinderGeometry(0.45, 0.55, 0.3, 32), dark).position.y = 0.15;
      this.yaw = new THREE.Group(); this.yaw.position.y = 0.3; this.node.add(this.yaw);
      this.mesh(new THREE.CylinderGeometry(0.25, 0.25, 0.3, 24), mat, this.yaw).position.y = 0.15;
      this.shoulderJ = new THREE.Group(); this.shoulderJ.position.y = 0.3; this.yaw.add(this.shoulderJ);
      this.mesh(new THREE.BoxGeometry(0.22, 1.2, 0.22), mat, this.shoulderJ).position.y = 0.6;
      this.elbowJ = new THREE.Group(); this.elbowJ.position.y = 1.2; this.shoulderJ.add(this.elbowJ);
      this.mesh(new THREE.SphereGeometry(0.16, 20, 12), dark, this.elbowJ);
      this.mesh(new THREE.BoxGeometry(0.18, 1.0, 0.18), mat, this.elbowJ).position.y = 0.5;
      this.mesh(new THREE.BoxGeometry(0.3, 0.12, 0.12), dark, this.elbowJ).position.y = 1.05;
    } else if (s === "light") {
      const c = toColor(p.color, "#ffffff"), inten = num(p.intensity, 1);
      const sub = String(p.subtype || "point").toLowerCase();
      if (sub.includes("ambient")) this.light = new THREE.AmbientLight(c, inten);
      else if (sub.includes("direction") || sub.includes("sun")) { this.light = new THREE.DirectionalLight(c, inten * 2); this.light.castShadow = true; }
      else if (sub.includes("spot")) { this.light = new THREE.SpotLight(c, inten * 4, 0, Math.PI / 5, 0.3, 0); this.light.castShadow = true; }
      else { this.light = new THREE.PointLight(c, inten * 4, 0, 0); this.light.castShadow = true; }
      this.node.add(this.light);
      if (this.light.target) { scene.add(this.light.target); }
      if (!sub.includes("ambient")) {
        const bulb = new THREE.Mesh(new THREE.SphereGeometry(0.12, 16, 8), new THREE.MeshBasicMaterial({ color: c }));
        bulb.userData.nobj = this; this.bulb = bulb; this.node.add(bulb);
      }
    } else if (s === "camera") {
      // the first camera entity drives the view (see setupView)
    } else if (s === "label") {
      this.label = new THREE.Sprite(new THREE.SpriteMaterial({ transparent: true, depthTest: false }));
      this.label.renderOrder = 10;
      this.node.add(this.label);
      this._labelText = null;
    } else if (s === "model") {
      const sz = size3(p.size);
      const ph = this.mesh(new THREE.BoxGeometry(sz[0], sz[1], sz[2]), std(col, { wireframe: true }));
      const url = String(p.url || "");
      if (url && typeof GLTFLoader !== "undefined") {
        new GLTFLoader().load(url, g => { this.node.remove(ph); this.node.add(g.scene); hudLog(`[${this.name}] model loaded`); },
          undefined, e => hudLog(`[${this.name}] could not load ${url}: ${e && e.message ? e.message : e}`));
      }
    } else if (s === "twin") {
      const sz = size3(p.size);
      const mat = std(col, { transparent: true, opacity: 0.85 }); this.colorMats.push(mat);
      this.dataMesh = this.mesh(new THREE.BoxGeometry(sz[0], sz[1], sz[2]), mat); this.dataHeight = sz[1];
      this.dataMesh.add(new THREE.LineSegments(new THREE.EdgesGeometry(this.dataMesh.geometry), new THREE.LineBasicMaterial({ color: 0xe8f4ff })));
    }
    // "group" (scene/group/...) has no geometry of its own
    if (s === "twin" || (s !== "label" && s !== "group" && s !== "camera" && s !== "light" && this.p.show_name)) {
      const t = labelTexture(this.name);
      const sp = new THREE.Sprite(new THREE.SpriteMaterial({ map: t.tex, depthTest: false, transparent: true }));
      sp.scale.set(0.35 * t.aspect, 0.35, 1);
      sp.position.y = size3(p.size)[1] / 2 + 0.4;
      sp.renderOrder = 10;
      this.node.add(sp);
    }
  }

  // ---- props -> visuals
  apply() {
    const p = this.p, n = this.node;
    n.position.set(...pos3(p.position));
    const r = pos3(p.rotation);
    n.rotation.set(r[0] * DEG, r[1] * DEG, r[2] * DEG);
    if (p.scale !== undefined) n.scale.set(...size3(p.scale)); else n.scale.set(1, 1, 1);
    n.visible = p.visible !== false && p.visible !== 0 && p.visible !== "false";
    let col;
    // live temperature (received over a channel) drives the colour; otherwise an explicit color wins
    // ...unless a behaviour set the colour explicitly ("when temperature > 80, set color to red")
    if (typeof p.temperature === "number" && this.recv.has("temperature") && !this.userColor) col = temperatureColor(p.temperature);
    else if (p.color !== undefined && p.color !== "") col = toColor(p.color, DEFAULT_COLORS[this.shape] || DEFAULT_COLORS.box);
    else if (typeof p.temperature === "number") col = temperatureColor(p.temperature);
    else col = new THREE.Color(this.shape === "tank" ? "#2f7fd8" : (DEFAULT_COLORS[this.shape] || DEFAULT_COLORS.box));
    // glow: a prop named "on", otherwise the first boolean field received over a channel
    let glowKey = typeof p.on === "boolean" ? "on" : null;
    if (!glowKey) for (const k of this.recv) if (typeof p[k] === "boolean") { glowKey = k; break; }
    const glow = glowKey ? !!p[glowKey] : false;
    const opacity = p.opacity === undefined ? null : clamp(num(p.opacity, 1), 0, 1);
    for (const m of this.colorMats) {
      m.color.copy(col);
      m.wireframe = !!p.wireframe;
      if (opacity !== null) { m.transparent = opacity < 1; m.opacity = opacity; m.depthWrite = opacity >= 1; }
      m.emissive.copy(col).multiplyScalar(glow ? 0.9 : 0);
    }
    // level/value/distance stretch the data mesh vertically (bottom stays in place); tanks always
    // show their level, other shapes only data received over a channel (a "value 10" prop of a
    // game object must not shrink it)
    if (this.dataMesh) {
      const key = ["level", "value", "distance"].find(k => typeof p[k] === "number" && (this.recv.has(k) || (this.shape === "tank" && k === "level")));
      let f = 1;
      if (key) { const v = p[key]; f = key === "value" && v <= 1 ? v : v / 100; f = clamp(f, 0.02, this.shape === "tank" ? 1 : 5); }
      this.dataMesh.scale.y = f;
      this.dataMesh.position.y = this.dataMesh.userData.anchorBottom ? this.dataHeight * f / 2 : (f - 1) * this.dataHeight / 2;
    }
    if (this.light) {
      const c = p.color !== undefined ? toColor(p.color, "#ffffff") : new THREE.Color("#ffffff");
      this.light.color.copy(c);
      const base = num(p.intensity, 1) * (this.light.isPointLight || this.light.isSpotLight ? 4 : this.light.isDirectionalLight ? 2 : 1);
      this.light.intensity = p.on === false ? 0 : base;
      if (this.bulb) this.bulb.material.color.copy(p.on === false ? new THREE.Color("#333333") : c);
      if (this.light.target) this.light.target.position.set(...pos3(p.target));
    }
    if (this.yaw) {
      this.yaw.rotation.y = num(p.base_angle, 0) * DEG;
      this.shoulderJ.rotation.z = -num(p.shoulder, 30) * DEG;
      this.elbowJ.rotation.z = -num(p.elbow, 45) * DEG;
    }
    if (this.shape === "camera" && this === VIEW.cam) {
      camera.position.set(...pos3(p.position, [6, 5, 8]));
      camera.fov = num(p.fov, 50); camera.updateProjectionMatrix();
      controls.target.set(...pos3(p.target));
    }
    this.updateLabel();
  }
  updateLabel() {
    if (!this.label) return;
    const text = this.format(this.p.text !== undefined && this.p.text !== "" ? String(this.p.text) : this.name);
    if (text === this._labelText) return;
    this._labelText = text;
    const t = labelTexture(text, this.p.color !== undefined ? "#" + toColor(this.p.color, "#ffffff").getHexString() : "#ffffff");
    if (this.label.material.map) this.label.material.map.dispose();
    this.label.material.map = t.tex; this.label.material.needsUpdate = true;
    const hgt = num(this.p.font_size, typeof this.p.size === "number" ? this.p.size : 0.5);
    this.label.scale.set(hgt * t.aspect, hgt, 1);
  }

  // ---- API used by generated behaviours
  get(k) {
    const ax = AXES[k];   // "its y" / "x" mean the position components
    if (ax !== undefined && this.p[k] === undefined) return pos3(this.p.position)[ax];
    if (ROT_AXES[k] !== undefined) return pos3(this.p.rotation)[ROT_AXES[k]];
    if (this.p[k] !== undefined) return this.p[k];
    if (REMOTE_FIELD[k] !== undefined) return REMOTE_FIELD[k];
    return 0;
  }
  num(k) { return num(this.get(k), 0); }
  // "increase rotation by 5" / "increase size by 1": vectors get the amount on every component,
  // except rotation, which spins around the vertical (Y) axis
  add(k, v) {
    const cur = this.get(k), d = num(v, 0);
    if (Array.isArray(cur) || k === "rotation" || k === "position" || k === "scale" || k === "size") {
      const q = k === "scale" || k === "size" ? size3(cur) : pos3(cur);
      if (k === "rotation" || k === "position") q[1] += d; else for (let i = 0; i < 3; i++) q[i] += d;
      return this.set(k, q);
    }
    this.set(k, num(cur, 0) + d);
  }
  // true only when a "when <condition>" becomes true (rising edge), used by conditional update behaviours
  edge(key, cond) { this._edges = this._edges || {}; const was = this._edges[key]; this._edges[key] = !!cond; return !!cond && !was; }
  set(k, v) {
    if (!this.alive) return;
    const ax = AXES[k];
    if (ax !== undefined && this.p[k] === undefined) {
      const q = pos3(this.p.position); q[ax] = num(v, q[ax]); return this.set("position", q);
    }
    if (ROT_AXES[k] !== undefined) {
      const r = pos3(this.p.rotation), i = ROT_AXES[k], was = r[i];
      r[i] = num(v, r[i]); this.set("rotation", r);
      if (r[i] !== was && this.alive && !this._changing.has(k)) {
        this._changing.add(k);
        try { fire(this, "change", k); fireWatchers(this, k); } finally { this._changing.delete(k); }
      }
      return;
    }
    if (k === "color") this.userColor = true;
    const old = this.p[k];
    this.p[k] = v;
    if (JSON.stringify(old) === JSON.stringify(v)) return;
    if (GEOMETRY_KEYS.has(k)) this.rebuild();
    this.apply();
    if (!this._changing.has(k)) {   // guard against change -> set -> change loops
      this._changing.add(k);
      try { fire(this, "change", k); fireWatchers(this, k); } finally { this._changing.delete(k); }
    }
  }
  move(dx, dy, dz, scale = 1) {
    const q = pos3(this.p.position);
    this.set("position", [q[0] + num(dx) * scale, q[1] + num(dy) * scale, q[2] + num(dz) * scale]);
  }
  format(text) {
    return String(text).replace(/\{([\w.]+)\}/g, (m, key) => {
      const parts = key.split(".");
      const v = parts.length > 1 ? remote(parts[0], parts.slice(1).join("_")) : (this.p[key] !== undefined ? this.p[key] : AXES[key] !== undefined ? +pos3(this.p.position)[AXES[key]].toFixed(2) : REMOTE_FIELD[key]);
      return v === undefined ? m : fmt(v);
    });
  }
  log(text) { hudLog(`[${this.name}] ${this.format(text)}`); }
  destroy() {
    if (!this.alive) return;
    this.alive = false;
    if (this.node.parent) this.node.parent.remove(this.node);
    this.node.traverse(o => { if (o.geometry) o.geometry.dispose(); });
    hudLog(`[${this.name}] destroyed`);
  }
  send(to, fields) {
    let sent = 0;
    for (const ch of CHANNELS) {
      if (ch.src !== this.ident) continue;
      if (to && ![ch.dst, ch.dst_name, ch.dst_kind, ch.id].includes(to)) continue;
      sendChannel(ch, fields && fields.length ? fields : ch.fields);
      sent++;
    }
    if (!sent) hudLog(`[${this.name}] send: no channel from ${this.name}${to ? " to " + to : ""} (connect them first)`);
  }
  call(name, args) {
    const f = USER_FUNCTIONS[name];
    if (f) return f(this, ...(args || []));
    return this.builtin(name, args);
  }
  builtin(name, args) {
    if (name === "reset") { for (const k of Object.keys(this.initial)) this.set(k, JSON.parse(JSON.stringify(this.initial[k]))); }
    else if (name === "stop") this.set("speed", 0);
    else if (name === "open") this.set("open", true);
    else if (name === "close") this.set("open", false);
    else if (name === "jump") this.move(0, 1, 0);
    else if (name === "spawn") {
      const c = this.node.clone(); c.position.x += 1; (this.node.parent || scene).add(c);
    }
    this.log(`${name}(${(args || []).map(fmt).join(", ")})`);
  }
}

// ---------------------------------------------------------------- behaviours
const ERRORS = new Set();
function run(o, fn, dt = 0, other = null, msg = null) {
  if (!o.alive) return;
  try { fn(o, dt, other, msg); }
  catch (e) { const k = o.ident + ":" + e; if (!ERRORS.has(k)) { ERRORS.add(k); hudLog(`[${o.name}] behaviour error: ${e.message || e}`); } }
}
// fire behaviours of one kind; for keyed kinds (change/input/message) only matching keys run
function fire(o, kind, key = null, extra = {}) {
  const b = BEHAVIOURS[o.ident];
  if (!b || !b[kind]) return;
  for (const item of b[kind]) {
    if (kind === "change" && item[0] !== key) continue;
    // collision rules with "with": only contacts with that entity ident or kind fire
    if (kind === "collision" && Array.isArray(item) && !collidesWith(extra.other, item[0])) continue;
    if (kind === "message" && item[0] !== "any" && !(key || []).includes(item[0])) continue;
    run(o, Array.isArray(item) ? item[item.length - 1] : item, extra.dt || 0, extra.other || null, extra.msg || null);
  }
}

function collidesWith(other, keys) {
  if (!other) return false;
  const names = [other.ident, String(other.kind || "").toLowerCase(), String(other.name || "").toLowerCase()];
  return keys.some(k => names.includes(k));
}
// "change:<entity>.<prop>" rules on other objects watching src's prop
function fireWatchers(src, k) {
  const names = new Set([src.ident, String(src.kind || "").toLowerCase(), String(src.name || "").toLowerCase()]);
  for (const o of OBJS.values()) {
    if (o === src || !o.alive) continue;
    const b = BEHAVIOURS[o.ident];
    if (!b || !b.change) continue;
    for (const item of b.change) {
      const key = item[0], dot = key.indexOf(".");
      if (dot > 0 && key.slice(dot + 1) === k && names.has(key.slice(0, dot))) run(o, item[item.length - 1], 0, src);
    }
  }
}

// ---------------------------------------------------------------- channels
const HUB = { lastOk: 0, everOk: false };
function hubOnline() { return performance.now() - HUB.lastOk < 3000; }
function payloadOf(o, fields) {
  const out = {};
  for (const f of fields) out[f] = o ? o.get(f) : 0;
  return out;
}
async function postHub(ch, payload) {
  // hub offline: retry at most every 3 s (each failed fetch is a console error)
  const now = performance.now();
  if (!hubOnline() && now - (ch.lastPost || -1e9) < 3000) return;
  ch.lastPost = now;
  try {
    const r = await fetch(ch.url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    if (r.ok) { HUB.lastOk = performance.now(); HUB.everOk = true; }
  } catch (e) { /* hub offline: ignore */ }
}
function sendChannel(ch, fields) {
  const o = OBJS.get(ch.src);
  if (!o || !o.alive) return;
  const payload = Object.assign({ channel: ch.id, from: ch.src }, payloadOf(o, fields));
  if (ch.dir === "local") deliver(ch, payload);
  else if (ch.pollable) postHub(ch, payload);
  else hudLog(`[${ch.id}] ${ch.protocol} is not reachable from a browser; would send ${JSON.stringify(payloadOf(o, fields))}`);
}
// a message arrived for channel ch: remember it, copy fields onto the destination, fire behaviours
function deliver(ch, msg) {
  const fields = {};
  // null/undefined = the sender has no value yet: keep what we have
  for (const [k, v] of Object.entries(msg)) if (k !== "channel" && k !== "from" && !k.startsWith("_") && v !== null && v !== undefined) fields[k] = v;
  REMOTE[ch.src] = Object.assign(REMOTE[ch.src] || {}, fields);
  Object.assign(REMOTE_FIELD, fields);
  ch.last = fields;
  const dst = OBJS.get(ch.dst);
  if (!dst || !dst.alive) return;
  let rising = false;
  const hasBool = Object.values(fields).some(v => typeof v === "boolean");
  let newKey = false;
  for (const [k, v] of Object.entries(fields)) {
    if (typeof v === "boolean" && v && dst.p[k] !== true) rising = true;
    if (!dst.recv.has(k)) { dst.recv.add(k); newKey = true; }
    dst.set(k, v);
  }
  if (newKey && dst.alive) dst.apply();   // a field that is now live may change how the object is drawn
  const names = [ch.src, ch.src_name, ch.src_kind, ch.id];
  fire(dst, "message", names, { msg: fields });
  // input:<remote entity> fires on a rising boolean edge (or on every message without booleans)
  const b = BEHAVIOURS[dst.ident];
  if (b && b.input) for (const [type, key, fn] of b.input) {
    if (type === "remote" && names.includes(key) && (rising || !hasBool)) run(dst, fn, 0, null, fields);
  }
}
// plausible fake values for a sender we cannot reach
function fake(ch, f) {
  const prev = (REMOTE[ch.src] || {})[f];
  const srcObj = OBJS.get(ch.src);
  const dstObj = OBJS.get(ch.dst);
  const seed = srcObj ? srcObj.get(f) : (dstObj ? dstObj.p[f] : undefined);  // start near the current value
  if (typeof prev === "boolean" || typeof seed === "boolean" || /^(on|off|pressed|active|enabled|alarm|open|running|state|motion|detected)$/.test(f)) {
    const cur = typeof prev === "boolean" ? prev : false;
    return Math.random() < 0.15 ? !cur : cur;
  }
  const base = { temperature: 24, humidity: 45, distance: 100, light: 300, pressure: 1013, voltage: 3.3, level: 50, value: 0.5, speed: 1 }[f];
  const start = typeof prev === "number" ? prev : (typeof seed === "number" && seed !== 0 ? seed : (base !== undefined ? base : 10));
  const step = f === "level" ? 3 : f === "value" ? 0.05 : f === "temperature" ? 0.8 : 1;
  let v = start + (Math.random() * 2 - 1) * step;
  if (f === "level") v = clamp(v, 0, 100);
  if (f === "value") v = clamp(v, 0, 1);
  return Math.round(v * 100) / 100;
}
function startChannels() {
  for (const ch of CHANNELS) {
    ch.seen = null; ch.liveAt = 0; ch.last = null;
    const every = Math.max(100, ch.interval_ms || 1000);
    if (ch.dir === "out" || ch.dir === "local") {
      setInterval(() => sendChannel(ch, ch.fields), every);   // sender auto-publishes its fields
    }
    if (ch.dir === "in") {
      if (ch.pollable) {
        ch.lastTry = -1e9;
        setInterval(async () => {
          // while the hub is offline retry every 3 s instead of every poll (less console noise)
          const now = performance.now();
          if (!hubOnline() && now - ch.lastTry < 3000) return;
          ch.lastTry = now;
          try {
            const r = await fetch(ch.url, { cache: "no-store" });
            const msg = await r.json();
            HUB.lastOk = performance.now(); HUB.everOk = true;
            if (msg && msg._t !== undefined && msg._t !== ch.seen) { ch.seen = msg._t; ch.liveAt = performance.now(); deliver(ch, msg); }
          } catch (e) { /* hub offline */ }
        }, HUB_POLL_MS);
      }
      // simulate the sender whenever no fresh real data arrives
      setInterval(() => {
        if (performance.now() - ch.liveAt < Math.max(5000, 3 * every)) return;
        const msg = { channel: ch.id, from: ch.src };
        for (const f of ch.fields) msg[f] = fake(ch, f);
        ch.simulated = true;
        deliver(ch, msg);
      }, every);
    }
  }
}

// ---------------------------------------------------------------- input
const KEY_ALIASES = { space: " ", spacebar: " ", enter: "enter", return: "enter", up: "arrowup", down: "arrowdown",
  left: "arrowleft", right: "arrowright", esc: "escape", escape: "escape", ctrl: "control", control: "control",
  shift: "shift", alt: "alt", tab: "tab", backspace: "backspace", delete: "delete" };
function keyMatches(name, ev) {
  const n = String(name).toLowerCase().trim();
  const k = (ev.key || "").toLowerCase(), c = (ev.code || "").toLowerCase();
  const want = KEY_ALIASES[n] || n.replace(/^key_?/, "").replace(/_/g, " ");
  return k === want || c === n || c === "key" + want || c === "digit" + want || (want.startsWith("arrow") && c === want);
}
const HELD = new Set();
window.addEventListener("keydown", ev => {
  HELD.add((ev.key || "").toLowerCase());
  if (ev.repeat) return;
  for (const o of OBJS.values()) {
    const b = BEHAVIOURS[o.ident];
    if (!b || !b.input) continue;
    for (const [type, key, fn] of b.input) if (type === "key" && keyMatches(key, ev)) run(o, fn);
  }
});
window.addEventListener("keyup", ev => HELD.delete((ev.key || "").toLowerCase()));
window.addEventListener("blur", () => HELD.clear());
// clicking an object fires input:<that object's name> behaviours (on any object listening to it)
const raycaster = new THREE.Raycaster();
let downAt = null;
renderer.domElement.addEventListener("pointerdown", ev => { downAt = [ev.clientX, ev.clientY]; });
renderer.domElement.addEventListener("pointerup", ev => {
  if (!downAt || Math.hypot(ev.clientX - downAt[0], ev.clientY - downAt[1]) > 5) return;  // was a drag (orbit)
  const ndc = new THREE.Vector2(ev.clientX / window.innerWidth * 2 - 1, -(ev.clientY / window.innerHeight) * 2 + 1);
  raycaster.setFromCamera(ndc, camera);
  const hit = raycaster.intersectObjects(scene.children, true).find(h => h.object.userData.nobj || findOwner(h.object));
  if (!hit) return;
  const target = hit.object.userData.nobj || findOwner(hit.object);
  for (const o of OBJS.values()) {
    const b = BEHAVIOURS[o.ident];
    if (!b || !b.input) continue;
    for (const [type, key, fn] of b.input) {
      if (type !== "click") continue;
      if ([target.ident, target.name, target.kind].includes(key) || isAncestor(OBJS.get(key), target)) run(o, fn, 0, target);
    }
  }
});
function findOwner(o3) { while (o3) { if (o3.userData && o3.userData.nobj) return o3.userData.nobj; o3 = o3.parent; } return null; }
function isAncestor(a, b) { if (!a) return false; let n = b.node.parent; while (n) { if (n === a.node) return true; n = n.parent; } return false; }

// ---------------------------------------------------------------- collisions
const TOUCHING = new Set();
let collisionsPrimed = false;
const SOLID = new Set(["box", "sphere", "cylinder", "cone", "torus", "plane", "ground", "building", "tank", "pipe", "arm", "model", "twin"]);
function related(a, b) { return isAncestor(a, b) || isAncestor(b, a); }
function checkCollisions() {
  const watchers = [...OBJS.values()].filter(o => o.alive && BEHAVIOURS[o.ident] && BEHAVIOURS[o.ident].collision);
  if (!watchers.length) return;
  const solids = [...OBJS.values()].filter(o => o.alive && o.node.visible && SOLID.has(o.shape));
  const boxes = new Map(solids.map(o => [o, new THREE.Box3().setFromObject(o.node)]));
  for (const a of watchers) {
    const ba = boxes.get(a) || new THREE.Box3().setFromObject(a.node);
    if (ba.isEmpty()) continue;
    for (const b of solids) {
      if (b === a || related(a, b)) continue;
      const key = a.ident + "|" + b.ident;
      if (ba.intersectsBox(boxes.get(b))) {
        // contacts that already exist in the initial layout (e.g. resting on the ground) do not fire
        if (!TOUCHING.has(key)) { TOUCHING.add(key); if (collisionsPrimed) fire(a, "collision", null, { other: b }); }
      } else TOUCHING.delete(key);
    }
  }
  collisionsPrimed = true;
}

// ---------------------------------------------------------------- HUD rendering
function renderHud() {
  if (!hudEl) return;
  let h = `<b>${esc(PROJECT)}</b>`;
  for (const o of OBJS.values()) {
    if (!o.alive) continue;
    const data = Object.entries(o.p).filter(([k, v]) => !NON_DATA.has(k) && !k.startsWith("_") && (typeof v !== "object" || v === null));
    h += `<div class="ent"><span class="nm">${esc(o.name)}</span> <span class="kd">${esc(o.kind)}</span>`;
    for (const [k, v] of data) h += `<div class="pr">${esc(k)}: <b${o.recv.has(k) ? ' class="live"' : ""}>${esc(fmt(v))}</b></div>`;
    h += `</div>`;
  }
  if (CHANNELS.length) {
    h += `<div class="ent"><span class="nm">channels</span>`;
    for (const ch of CHANNELS) {
      const live = ch.liveAt > 0 && performance.now() - ch.liveAt < 5000;
      const state = ch.dir === "in" ? (live ? "live" : "simulated") : ch.dir;
      h += `<div class="pr">${esc(ch.src_name)} &rarr; ${esc(ch.dst_name)} <i>${esc(ch.protocol)}</i> ${esc(state)}</div>`;
    }
    h += `</div>`;
  }
  hudEl.innerHTML = h;
  if (badgeEl) {
    if (!CHANNELS.some(c => c.pollable && c.dir !== "local")) { badgeEl.style.display = "none"; }
    else if (!hubOnline()) { badgeEl.textContent = "hub offline - simulated"; badgeEl.className = "off"; }
    else if (CHANNELS.some(c => c.dir === "in" && c.pollable && !(c.liveAt > 0 && performance.now() - c.liveAt < 5000))) { badgeEl.textContent = "hub online - waiting for data (simulated)"; badgeEl.className = "wait"; }
    else { badgeEl.textContent = "hub online"; badgeEl.className = "on"; }
  }
  for (const o of OBJS.values()) if (o.alive) o.updateLabel();
}

// ---------------------------------------------------------------- build + view
for (const d of DEFS) {
  const o = new NObj(d);
  OBJS.set(d.ident, o);
}
for (const d of DEFS) {
  const o = OBJS.get(d.ident);
  const parent = d.parent ? OBJS.get(d.parent) : null;
  (parent ? parent.node : scene).add(o.node);
}
(function setupView() {
  const camObj = [...OBJS.values()].find(o => o.shape === "camera");
  const box = new THREE.Box3();
  // frame the interesting objects; large floors only count when there is nothing else
  const all = [...OBJS.values()].filter(o => SOLID.has(o.shape) || o.shape === "room");
  const focus = all.filter(o => !["ground", "plane"].includes(o.shape));
  for (const o of (focus.length ? focus : all)) box.expandByObject(o.node);
  const size = box.isEmpty() ? 4 : Math.max(...box.getSize(new THREE.Vector3()).toArray(), 1);
  grid.scale.setScalar(Math.max(1, Math.ceil(size / 20)));
  camera.far = Math.max(5000, size * 50); camera.near = Math.max(0.01, size / 2000);
  if (camObj) { VIEW.cam = camObj; camObj.apply(); }
  else {
    const c = box.isEmpty() ? new THREE.Vector3() : box.getCenter(new THREE.Vector3());
    camera.position.set(c.x + size * 1.2, c.y + size * 0.9, c.z + size * 1.6);
    controls.target.copy(c);
  }
  camera.updateProjectionMatrix();
})();

// ---------------------------------------------------------------- main loop
function step(dt) {
  for (const o of OBJS.values()) {
    if (!o.alive) continue;
    // "movable" objects: WASD / arrow keys at <speed> units per second
    if (o.p.movable === true) {
      const sp = num(o.p.speed, 2) * dt;
      let dx = 0, dz = 0;
      if (HELD.has("a") || HELD.has("arrowleft")) dx -= sp;
      if (HELD.has("d") || HELD.has("arrowright")) dx += sp;
      if (HELD.has("w") || HELD.has("arrowup")) dz -= sp;
      if (HELD.has("s") || HELD.has("arrowdown")) dz += sp;
      if (dx || dz) o.move(dx, 0, dz);
    }
    const b = BEHAVIOURS[o.ident];
    if (!b) continue;
    if (b.update) for (const fn of b.update) run(o, fn, dt);
  }
  checkCollisions();
}
// timer:<ms> behaviours use real elapsed time (not frames), so they keep their rate even when
// requestAnimationFrame is throttled (background tab); at most 10 catch-up runs per tick
const timerAcc = new Map();
let timerLast = performance.now();
setInterval(() => {
  const now = performance.now(), elapsed = Math.min(now - timerLast, 10000);
  timerLast = now;
  for (const o of OBJS.values()) {
    const b = BEHAVIOURS[o.ident];
    if (!o.alive || !b || !b.timer) continue;
    b.timer.forEach(([ms, fn], i) => {
      const k = o.ident + "#" + i;
      let acc = (timerAcc.get(k) || 0) + elapsed, n = 0;
      while (acc >= ms && o.alive && n++ < 10) { acc -= ms; run(o, fn, ms / 1000); }
      timerAcc.set(k, Math.min(acc, ms));
    });
  }
}, 20);
const clock = new THREE.Clock();
function frame() {
  requestAnimationFrame(frame);
  const dt = Math.min(clock.getDelta(), 0.1);
  step(dt);
  controls.update();
  renderer.render(scene, camera);
}
for (const o of OBJS.values()) fire(o, "start");
startChannels();
setInterval(renderHud, 250);
renderHud();
hudLog(`${PROJECT}: ${OBJS.size} objects, ${CHANNELS.length} channels`);
frame();
// handy for debugging from the browser console
window.nlcl = { THREE, scene, camera, OBJS, REMOTE, CHANNELS, deliver, step, fire };
