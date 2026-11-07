/* Sampo · the live forge (T-2999). The scene and the panels for `sampo watch --web`, under a strict CSP: no inline
   code, snapshot text only ever through textContent. The scene: Claude at the anvil, builders at their benches, jobs as
   furnaces, Claude models as sky lanterns, local models as lanterns on the gantry, Jeff's towers, Jarvis on the hill;
   each call a spark from caller to model. With prefers-reduced-motion (or the Still button) nothing moves: changes are
   lit instead. */
(() => {
"use strict";

const $ = id => document.getElementById(id);
const root = document.documentElement;
const modern = root.getAttribute("data-theme") !== "forge";  // theme.js set it before this ran
const TAU = Math.PI * 2;
const clamp = (v, a, b) => v < a ? a : v > b ? b : v;
const lerp = (a, b, f) => a + (b - a) * f;
const arr = x => Array.isArray(x) ? x : [];
const obj = x => x && typeof x === "object" && !Array.isArray(x) ? x : {};
const str = x => x == null ? "" : String(x).slice(0, 400);
const num = x => (typeof x === "number" || (typeof x === "string" && x.trim() !== "")) && Number.isFinite(+x) ? +x : null;
const now = () => performance.now() / 1000;

// seeded randomness: the same scene for the same names, and no Math.random
function rng(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6D2B79F5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
function hash(s) {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); }
  return h >>> 0;
}
const R = rng(hash("sampo-forge"));

// ---------- colours ----------
const KIND = {write: [255, 194, 77], judge: [188, 139, 255], embed: [69, 224, 160], train: [255, 148, 64],
  plan: [110, 155, 255], act: [255, 106, 74], review: [234, 242, 255]};
// T-3054 review: the light scheme's --k-* tokens (live.css), so a line holds 3:1 on a light ground
const KLITE = {write: [138, 83, 0], judge: [109, 63, 208], embed: [10, 122, 83], train: [178, 58, 7],
  plan: [47, 98, 224], act: [198, 40, 40], review: [58, 69, 86]};
const JOBC = {funnel: "#BC8BFF", grow: "#FFC24D", exam: "#6E9BFF", train: "#FF9440", bench: "#45E0A0", check: "#EAF2FF",
  other: "#BAC0DD"};
const TYPEC = {FIX: "#FF6A4A", FEATURE: "#FFC24D", CLEAN: "#45E0A0", PERF: "#FF9440", SECURITY: "#BC8BFF", RESEARCH: "#6E9BFF"};
const USEC = {write: "#FFC24D", judge: "#BC8BFF", embed: "#45E0A0", train: "#FF9440", plan: "#6E9BFF", act: "#FF6A4A",
  review: "#EAF2FF"};
const EVC = [[/close|done|finish/, "#45E0A0"], [/funnel|moon|judge/, "#BC8BFF"], [/grow|card|write/, "#FFC24D"],
  [/exam|plan|lane|brief/, "#6E9BFF"], [/train/, "#FF9440"], [/session|act|run/, "#FF6A4A"],
  [/check|test|review|audit/, "#EAF2FF"], [/embed|bench|model/, "#45E0A0"]];
const VRAMC = ["#FFC24D", "#FF9440", "#F0D392", "#E07A3A"];
// the kind colours as CSS tokens: the forge defines them as the hexes above, the modern look per scheme (T-3055)
const HEXK = {"#FFC24D": "amber", "#BC8BFF": "violet", "#45E0A0": "green", "#FF9440": "orange", "#6E9BFF": "blue", "#FF6A4A": "red",
  "#EAF2FF": "pale", "#BAC0DD": "other", "#D4AE66": "dflt"};
const kv = hex => "var(--k-" + (HEXK[hex] || "dflt") + ")";
const VRAMK = ["var(--k-blue)", "var(--k-amber)", "var(--k-red)", "var(--k-orange)"];
const HOT = [[120, 30, 16], [226, 72, 26], [255, 148, 64], [255, 194, 77], [255, 240, 200], [234, 242, 255]];
const EMBER = [255, 148, 64], GOLD = [255, 194, 77], WARM = [255, 214, 150], PALE = [255, 240, 214], BRASS = [240, 211, 146];
const rgba = (c, a) => "rgba(" + c[0] + "," + c[1] + "," + c[2] + "," + clamp(a, 0, 1).toFixed(3) + ")";
function heat(v, a) {
  const f = clamp(v, 0, 1) * (HOT.length - 1), i = Math.min(HOT.length - 2, Math.floor(f)), k = f - i;
  const p = HOT[i], q = HOT[i + 1];
  return rgba([p[0] + (q[0] - p[0]) * k | 0, p[1] + (q[1] - p[1]) * k | 0, p[2] + (q[2] - p[2]) * k | 0], a);
}
const flick = (t, s) => 0.55 + 0.22 * Math.sin(t * 7.3 + s) + 0.14 * Math.sin(t * 13.1 + s * 2.1) + 0.09 * Math.sin(t * 23.7 + s * 0.7);

// ---------- text ----------
const NF = new Intl.NumberFormat("en-US");
const fmt = n => n == null ? "—" : NF.format(Math.round(n));
const gb = mb => (mb / 1024).toFixed(1) + " GB";
const pad = n => String(n).padStart(2, "0");
function clock(at, secs) {
  const d = new Date(at);
  if (!Number.isFinite(d.getTime())) return secs ? "--:--:--" : "--:--";
  return pad(d.getHours()) + ":" + pad(d.getMinutes()) + (secs ? ":" + pad(d.getSeconds()) : "");
}
function span(ms) {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return s + " s";
  if (s < 3600) return Math.floor(s / 60) + " m";
  if (s < 172800) return Math.floor(s / 3600) + " h " + Math.floor(s % 3600 / 60) + " m";
  return Math.floor(s / 86400) + " d";
}
function since(at, ref) {
  const a = Date.parse(str(at)), b = Date.parse(str(ref));
  return Number.isFinite(a) && Number.isFinite(b) ? span(b - a) : "";
}
const cut = (s, n) => s.length > n ? s.slice(0, n - 1) + "…" : s;
function h(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = String(text);
  return e;
}
function setText(el, v) { if (el && el.textContent !== v) el.textContent = v; }
function evText(e) {
  let t = str(e.text);
  if (/^\s*[{[]/.test(t)) {
    try {
      const o = JSON.parse(t);
      if (o && typeof o === "object") {
        t = Object.entries(o).slice(0, 8).map(([k, v]) => k.replace(/_/g, " ") + " " + (typeof v === "number"
          ? (Number.isInteger(v) ? NF.format(v) : String(+v.toFixed(3))) : v && typeof v === "object" ? "…" : str(v))).join(" · ");
      }
    } catch (err) { /* not JSON after all: show it as it came */ }
  }
  return cut(t.replace(/\s+/g, " ").trim(), 160);
}
const evColour = k => { for (const [re, c] of EVC) if (re.test(k)) return c; return "#D4AE66"; };

// ---------- motion ----------
const reduce = window.matchMedia ? window.matchMedia("(prefers-reduced-motion: reduce)") : {matches: false};
let userStill = false;
const still = () => !!reduce.matches || userStill;

// ---------- the scene ----------
const cv = $("scene"), cx = cv && cv.getContext ? cv.getContext("2d") : null;
const bg = document.createElement("canvas");
const nodes = new Map();
const sparks = [], pending = [], flares = [], embers = [], streaks = [], smoke = [], labels = [];
const seen = new Map();
const cnt = {nodes: 0, sparks: 0, flown: 0, lit: 0}, shown = {};
let W = 0, H = 0, U = 1, dpr = 1, G = null, stars = [];
let raf = 0, lastT = 0, intro = !still(), introAt = now(), strikeAt = 0, flash = 0, shoot = null, nextShoot = now() + 9;
let litTimer = 0, acc = {hearth: 0, smoke: 0};

function publish() {
  if (!cv) return;
  for (const k of Object.keys(cnt)) if (shown[k] !== cnt[k]) { shown[k] = cnt[k]; cv.dataset[k] = String(cnt[k]); }
}

function geometry() {
  const wide = W / H > 1.25, x = f => f * W, y = f => f * H;
  U = clamp(Math.min(W / 1600, H / 500), 0.55, 1.3);
  const g = {wide};
  if (wide) {
    g.horizon = y(0.56); g.ground = y(0.70);
    g.anvil = {x: x(0.5) + 6 * U, y: y(0.79)};
    g.hearth = {x: x(0.5) - 44 * U, y: y(0.69)};
    const half = Math.min(x(0.17), 330 * U);
    g.beam = {x0: x(0.5) - half, x1: x(0.5) + half, y: y(0.36)};
    g.sky = {x0: x(0.38), x1: x(0.74), y0: y(0.09), y1: y(0.23)};
    g.moon = {x: x(0.885), y: y(0.15), r: 24 * U};
    g.hill = {x: x(0.815), top: y(0.42), w: x(0.065)};
    g.jeff = {x: x(0.135), base: y(0.655)};
    // a short stage puts the towers under the focus card: step them out to its right
    const fc = $("focus"), s = cv.getBoundingClientRect(), f = fc ? fc.getBoundingClientRect() : null;
    // (only while they fit before the gantry's post; on a narrower stage they stay behind the card)
    const jx = f ? f.right - s.left + 56 * U : 0;
    if (f && f.width && f.top - s.top < H && g.jeff.base - 130 * U < f.bottom - s.top + 6 && jx < g.beam.x0 - 66 * U) g.jeff.x = Math.max(g.jeff.x, jx);
    g.lanes = {x0: x(0.05), x1: x(0.29), y: y(0.9)};
    g.jobs = {x0: x(0.69), x1: x(0.95), y: y(0.83)};
    g.road = [[x(0.585), H + 6], [x(0.63), y(0.8)], [x(0.79), y(0.78)], [x(0.8), y(0.565)]];
    g.ridges = [[0.47, 0.075, "#22264A"], [0.56, 0.045, "#121330"], [0.655, 0.03, "#0A0A1A"]];
  } else {
    g.horizon = y(0.55); g.ground = y(0.68);
    g.anvil = {x: x(0.5), y: y(0.79)};
    g.hearth = {x: x(0.5) - 40 * U, y: y(0.67)};
    g.beam = {x0: x(0.23), x1: x(0.77), y: y(0.36)};
    g.sky = {x0: x(0.1), x1: x(0.74), y0: y(0.07), y1: y(0.19)};
    g.moon = {x: x(0.88), y: y(0.09), r: 16 * U};
    g.hill = {x: x(0.85), top: y(0.38), w: x(0.12)};
    g.jeff = {x: x(0.13), base: y(0.6)};
    g.lanes = {x0: x(0.05), x1: x(0.29), y: y(0.93)};
    g.jobs = {x0: x(0.66), x1: x(0.96), y: y(0.91)};
    g.road = [[x(0.6), H + 6], [x(0.64), y(0.76)], [x(0.84), y(0.74)], [x(0.85), y(0.535)]];
    g.ridges = [[0.45, 0.07, "#22264A"], [0.53, 0.045, "#121330"], [0.62, 0.03, "#0A0A1A"]];
  }
  g.strike = {x: g.anvil.x - 16 * U, y: g.anvil.y - 3 * U};
  g.smith = {x: g.anvil.x + 30 * U, y: g.anvil.y - 60 * U};
  g.flue = {x: g.hearth.x, y: g.beam.y - 34 * U};
  return g;
}

function ridge(i) {
  const [fy, amp] = G.ridges[i], r = rng(97 + i * 131), ph = [r() * TAU, r() * TAU, r() * TAU, r() * TAU];
  const base = fy * H, bump = i === 1 ? Math.max(0, base - G.hill.top) : 0, pts = [];
  for (let px = -12; px <= W + 12; px += 5) {
    const k = px / (380 * U);
    const v = 0.5 * Math.sin(k + ph[0]) + 0.3 * Math.sin(k * 2.3 + ph[1]) + 0.14 * Math.sin(k * 5.1 + ph[2]) +
      0.06 * Math.sin(k * 11.7 + ph[3]);
    let py = base - amp * H * (v * 0.5 + 0.5);
    if (bump) py = Math.min(py, base - bump * Math.exp(-(((px - G.hill.x) / G.hill.w) ** 2)) - amp * H * 0.2);
    pts.push([px, py]);
  }
  return pts;
}

function cubic(p, t) {
  const m = 1 - t, a = m * m * m, b = 3 * m * m * t, c = 3 * m * t * t, d = t * t * t;
  return [a * p[0][0] + b * p[1][0] + c * p[2][0] + d * p[3][0], a * p[0][1] + b * p[1][1] + c * p[2][1] + d * p[3][1]];
}

function backdrop() {
  bg.width = cv.width; bg.height = cv.height;
  const b = bg.getContext("2d"), r = rng(7031);
  b.setTransform(dpr, 0, 0, dpr, 0, 0);
  // the night sky, warmed low by the forge
  let g = b.createLinearGradient(0, 0, 0, G.ground);
  g.addColorStop(0, "#03040D"); g.addColorStop(0.42, "#0A0F2E"); g.addColorStop(0.74, "#1B1A47");
  g.addColorStop(0.9, "#2C1E3E"); g.addColorStop(1, "#3B2236");
  b.fillStyle = g; b.fillRect(0, 0, W, H);
  g = b.createRadialGradient(G.hearth.x, G.horizon, 0, G.hearth.x, G.horizon, Math.max(W, H) * 0.5);
  g.addColorStop(0, "rgba(224,144,96,.22)"); g.addColorStop(1, "rgba(224,144,96,0)");
  b.fillStyle = g; b.fillRect(0, 0, W, H);
  g = b.createRadialGradient(G.moon.x, G.moon.y, 0, G.moon.x, G.moon.y, Math.max(W, H) * 0.32);
  g.addColorStop(0, "rgba(110,155,255,.12)"); g.addColorStop(1, "rgba(110,155,255,0)");
  b.fillStyle = g; b.fillRect(0, 0, W, H);
  // the band of the galaxy, then the stars
  const band = Math.min(1400, W * H / 900);
  for (let i = 0; i < band; i++) {
    const u = r(), off = (r() + r() + r() - 1.5) * 0.11 * H;
    b.fillStyle = rgba([200, 210, 255], 0.04 + r() * 0.12);
    b.fillRect(u * W, lerp(G.horizon * 0.72, H * 0.02, u) + off, 1, 1);
  }
  stars = [];
  const ns = Math.min(380, Math.round(W * G.horizon / 2400));
  for (let i = 0; i < ns; i++) {
    const sx = r() * W, sy = r() * G.horizon * 0.96, m = r(), sz = m > 0.97 ? 1.5 : m > 0.82 ? 1 : 0.65;
    b.fillStyle = rgba(m > 0.9 ? [255, 236, 206] : [214, 224, 255], 0.3 + r() * 0.6);
    b.beginPath(); b.arc(sx, sy, sz * 0.62, 0, TAU); b.fill();
    if (m > 0.88 && stars.length < 46) stars.push({x: sx, y: sy, s: sz, p: r() * TAU, f: 0.5 + r() * 1.7});
  }
  // the moon
  const M = G.moon;
  g = b.createRadialGradient(M.x, M.y, M.r * 0.6, M.x, M.y, M.r * 5);
  g.addColorStop(0, "rgba(240,224,180,.2)"); g.addColorStop(1, "rgba(240,224,180,0)");
  b.fillStyle = g; b.beginPath(); b.arc(M.x, M.y, M.r * 5, 0, TAU); b.fill();
  g = b.createRadialGradient(M.x - M.r * 0.35, M.y - M.r * 0.35, M.r * 0.1, M.x, M.y, M.r);
  g.addColorStop(0, "#FBF5E2"); g.addColorStop(0.7, "#E4D9BA"); g.addColorStop(1, "#BFB291");
  b.fillStyle = g; b.beginPath(); b.arc(M.x, M.y, M.r, 0, TAU); b.fill();
  for (const [cxr, cyr, rr] of [[-0.3, 0.1, 0.22], [0.25, -0.3, 0.14], [0.2, 0.35, 0.18], [-0.05, -0.45, 0.1], [0.45, 0.05, 0.1]]) {
    b.fillStyle = "rgba(140,124,96,.22)";
    b.beginPath(); b.arc(M.x + cxr * M.r, M.y + cyr * M.r, rr * M.r, 0, TAU); b.fill();
  }
  // three ridges, rim-lit by the forge on the near side and by the moon on the far one
  for (let i = 0; i < G.ridges.length; i++) {
    const pts = ridge(i), col = G.ridges[i][2];
    b.beginPath(); b.moveTo(pts[0][0], H + 2);
    for (const p of pts) b.lineTo(p[0], p[1]);
    b.lineTo(W + 12, H + 2); b.closePath();
    b.fillStyle = col; b.fill();
    b.beginPath(); pts.forEach((p, j) => j ? b.lineTo(p[0], p[1]) : b.moveTo(p[0], p[1]));
    g = b.createLinearGradient(0, 0, W, 0);
    const hx = clamp(G.hearth.x / W, 0.05, 0.95), warm = [0.12, 0.3, 0.55][i];
    g.addColorStop(0, "rgba(224,144,96,0)"); g.addColorStop(clamp(hx - 0.25, 0.01, 0.98), "rgba(224,144,96,0)");
    g.addColorStop(hx, "rgba(224,144,96," + warm + ")");
    g.addColorStop(clamp(hx + 0.25, 0.02, 0.99), "rgba(224,144,96,0)"); g.addColorStop(1, "rgba(224,144,96,0)");
    b.strokeStyle = g; b.lineWidth = 1.3 * U; b.stroke();
    if (i < 2) { b.strokeStyle = "rgba(150,170,240," + (i ? 0.1 : 0.2) + ")"; b.lineWidth = 0.8 * U; b.stroke(); }
    if (i === 1) {
      let best = pts[0];
      for (const p of pts) if (Math.abs(p[0] - G.hill.x) < Math.abs(best[0] - G.hill.x)) best = p;
      G.peak = {x: best[0], y: best[1]};
    }
  }
  // the yard
  g = b.createLinearGradient(0, G.ground, 0, H);
  g.addColorStop(0, "rgba(10,9,20,0)"); g.addColorStop(0.3, "rgba(9,8,16,.85)"); g.addColorStop(1, "#050409");
  b.fillStyle = g; b.fillRect(0, G.ground, W, H - G.ground);
  for (let i = 0; i < Math.min(500, W * (H - G.ground) / 500); i++) {
    b.fillStyle = rgba([200, 180, 160], 0.02 + r() * 0.05);
    b.fillRect(r() * W, G.ground + r() * (H - G.ground), 1 + r() * 1.5, 1);
  }
  // the night road to Jarvis's hill, with its lamps
  const L = [], Rt = [], steps = 48;
  for (let i = 0; i <= steps; i++) {
    const t = i / steps, p = cubic(G.road, t), q = cubic(G.road, Math.min(1, t + 0.01)), d = Math.hypot(q[0] - p[0], q[1] - p[1]) || 1;
    const nx = -(q[1] - p[1]) / d, ny = (q[0] - p[0]) / d, hw = lerp(46 * U, 1.2 * U, Math.pow(t, 0.55));
    L.push([p[0] + nx * hw, p[1] + ny * hw]); Rt.push([p[0] - nx * hw, p[1] - ny * hw]);
  }
  b.beginPath(); L.forEach((p, i) => i ? b.lineTo(p[0], p[1]) : b.moveTo(p[0], p[1]));
  for (let i = Rt.length - 1; i >= 0; i--) b.lineTo(Rt[i][0], Rt[i][1]);
  b.closePath();
  g = b.createLinearGradient(0, H, 0, G.road[3][1]);
  g.addColorStop(0, "#1A1828"); g.addColorStop(1, "#242643");
  b.fillStyle = g; b.fill();
  b.strokeStyle = "rgba(212,174,102,.16)"; b.lineWidth = 0.8 * U; b.stroke();
  G.lamps = [];
  [0.1, 0.25, 0.39, 0.52, 0.63, 0.73, 0.82, 0.9].forEach((t, i) => {
    const p = cubic(G.road, t), q = cubic(G.road, t + 0.01), d = Math.hypot(q[0] - p[0], q[1] - p[1]) || 1;
    const side = i % 2 ? 1 : -1, s = lerp(1, 0.16, Math.pow(t, 0.8)), hw = lerp(46 * U, 1.2 * U, Math.pow(t, 0.55));
    const bx = p[0] + side * (-(q[1] - p[1]) / d) * (hw + 9 * U * s), by = p[1] + side * ((q[0] - p[0]) / d) * (hw + 9 * U * s);
    const top = by - 44 * U * s;
    b.strokeStyle = "#0B0A12"; b.lineWidth = Math.max(1, 2.4 * U * s);
    b.beginPath(); b.moveTo(bx, by); b.lineTo(bx, top); b.lineTo(bx - side * 7 * U * s, top); b.stroke();
    b.fillStyle = "#14121C"; b.fillRect(bx - side * 7 * U * s - 2.5 * U * s, top, 5 * U * s, 6 * U * s);
    G.lamps.push({x: bx - side * 7 * U * s, y: top + 4 * U * s, s, seed: i * 1.7});
  });
  // Jeff's towers
  G.towers = [[-36, 82, 22], [0, 118, 30], [34, 68, 22]].map(([dx, ht, w]) => ({x: G.jeff.x + dx * U, top: G.jeff.base - ht * U, w: w * U}));
  for (const T of G.towers) {
    b.fillStyle = "#121433";
    b.beginPath(); b.moveTo(T.x - T.w / 2, G.jeff.base + 8 * U); b.lineTo(T.x - T.w * 0.44, T.top); b.lineTo(T.x + T.w * 0.44, T.top);
    b.lineTo(T.x + T.w / 2, G.jeff.base + 8 * U); b.closePath(); b.fill();
    b.fillStyle = "#171A3D"; b.fillRect(T.x - T.w * 0.56, T.top - 5 * U, T.w * 1.12, 5 * U);
    for (let k = 0; k < 3; k++) b.fillRect(T.x - T.w * 0.56 + k * T.w * 0.46, T.top - 9 * U, T.w * 0.2, 4 * U);
    b.strokeStyle = "rgba(150,170,240,.42)"; b.lineWidth = 1.1 * U;
    b.beginPath(); b.moveTo(T.x + T.w * 0.44, T.top); b.lineTo(T.x + T.w / 2, G.jeff.base); b.stroke();
    b.beginPath(); b.moveTo(T.x - T.w * 0.56, T.top - 5 * U); b.lineTo(T.x + T.w * 0.56, T.top - 5 * U); b.stroke();
    b.strokeStyle = "rgba(224,144,96,.34)";
    b.beginPath(); b.moveTo(T.x - T.w * 0.44, T.top); b.lineTo(T.x - T.w / 2, G.jeff.base); b.stroke();
    b.fillStyle = "rgba(4,4,12,.7)";
    for (let k = 0; k < 4; k++) b.fillRect(T.x - 2 * U, T.top + 12 * U + k * 17 * U, 4 * U, 7 * U);
  }
  // Jarvis's lighthouse on the hill
  const P = G.peak || {x: G.hill.x, y: G.hill.top};
  b.fillStyle = "#151837";
  b.beginPath(); b.moveTo(P.x - 7 * U, P.y + 4 * U); b.lineTo(P.x - 4 * U, P.y - 44 * U); b.lineTo(P.x + 4 * U, P.y - 44 * U);
  b.lineTo(P.x + 7 * U, P.y + 4 * U); b.closePath(); b.fill();
  b.fillRect(P.x - 6 * U, P.y - 47 * U, 12 * U, 3 * U);
  b.fillStyle = "#0F1130"; b.fillRect(P.x - 4.5 * U, P.y - 56 * U, 9 * U, 9 * U);
  b.beginPath(); b.moveTo(P.x - 6 * U, P.y - 56 * U); b.lineTo(P.x, P.y - 63 * U); b.lineTo(P.x + 6 * U, P.y - 56 * U); b.closePath(); b.fill();
  b.strokeStyle = "rgba(150,170,240,.25)"; b.lineWidth = 0.8 * U;
  b.beginPath(); b.moveTo(P.x + 4 * U, P.y - 44 * U); b.lineTo(P.x + 7 * U, P.y + 4 * U); b.stroke();
  G.lamp = {x: P.x, y: P.y - 51.5 * U};
  // the hearth: stonework, hood and flue
  const hx = G.hearth.x, hy = G.hearth.y;
  b.fillStyle = "#17121A"; b.fillRect(G.flue.x - 15 * U, G.flue.y, 30 * U, hy - 76 * U - G.flue.y);
  b.fillStyle = "#1F1820"; b.fillRect(G.flue.x - 18 * U, G.flue.y - 4 * U, 36 * U, 6 * U);
  b.strokeStyle = "rgba(224,144,96,.28)"; b.lineWidth = 1 * U;
  b.beginPath(); b.moveTo(G.flue.x - 15 * U, G.flue.y + 2 * U); b.lineTo(G.flue.x - 15 * U, hy - 76 * U); b.stroke();
  b.fillStyle = "#1A141C";
  b.beginPath(); b.moveTo(hx - 80 * U, hy - 38 * U); b.lineTo(hx - 28 * U, hy - 78 * U); b.lineTo(hx + 28 * U, hy - 78 * U);
  b.lineTo(hx + 80 * U, hy - 38 * U); b.closePath(); b.fill();
  b.fillStyle = "#241B22"; b.fillRect(hx - 84 * U, hy - 40 * U, 168 * U, 5 * U);
  g = b.createLinearGradient(0, hy - 2 * U, 0, hy + 58 * U);
  g.addColorStop(0, "#2A1F25"); g.addColorStop(1, "#110D13");
  b.fillStyle = g; b.fillRect(hx - 88 * U, hy - 2 * U, 176 * U, 60 * U);
  b.strokeStyle = "rgba(0,0,0,.42)"; b.lineWidth = 1;
  for (let row = 0; row < 5; row++) {
    const yy = hy - 2 * U + row * 12 * U;
    b.beginPath(); b.moveTo(hx - 88 * U, yy); b.lineTo(hx + 88 * U, yy); b.stroke();
    for (let xx = hx - 88 * U + (row % 2 ? 16 : 0) * U + r() * 6 * U; xx < hx + 88 * U; xx += (26 + r() * 12) * U) {
      b.beginPath(); b.moveTo(xx, yy); b.lineTo(xx, yy + 12 * U); b.stroke();
    }
  }
  b.fillStyle = "#33262A"; b.fillRect(hx - 92 * U, hy - 5 * U, 184 * U, 5 * U);
  // the lantern gantry: two posts and a beam, lit from below
  const B = G.beam, foot = G.anvil.y + 74 * U;
  for (const [px, side] of [[B.x0, 1], [B.x1, -1]]) {
    g = b.createLinearGradient(px - 6 * U, 0, px + 6 * U, 0);
    g.addColorStop(0, side > 0 ? "#120C0C" : "#3A2420"); g.addColorStop(1, side > 0 ? "#3A2420" : "#120C0C");
    b.fillStyle = g; b.fillRect(px - 6 * U, B.y, 12 * U, foot - B.y);
    b.strokeStyle = "#1A100E"; b.lineWidth = 5 * U;
    b.beginPath(); b.moveTo(px + side * 4 * U, B.y + 64 * U); b.lineTo(px + side * 58 * U, B.y + 12 * U); b.stroke();
  }
  g = b.createLinearGradient(0, B.y - 2 * U, 0, B.y + 15 * U);
  g.addColorStop(0, "#2B1C17"); g.addColorStop(0.6, "#160E0C"); g.addColorStop(1, "#5A2C26");
  b.fillStyle = g; b.fillRect(B.x0 - 26 * U, B.y - 2 * U, B.x1 - B.x0 + 52 * U, 16 * U);
  b.fillStyle = "rgba(224,144,96,.55)"; b.fillRect(B.x0 - 26 * U, B.y + 13 * U, B.x1 - B.x0 + 52 * U, 1.2 * U);
  b.fillStyle = "rgba(150,170,240,.16)"; b.fillRect(B.x0 - 26 * U, B.y - 2 * U, B.x1 - B.x0 + 52 * U, 1 * U);
}

// ---------- nodes ----------
function place(m) {
  const fam = str(m.family), where = str(m.where), name = str(m.name).toLowerCase();
  if (fam === "claude") return "sky";
  if (fam === "jeff" || /^jeff\b/.test(name)) return "jeff";
  if (fam === "jarvis" || where === "remote" || /jarvis/.test(name)) return "hill";
  return "hang";
}
function guess(name) {
  const n = name.toLowerCase();
  const family = /haiku|sonnet|opus|claude/.test(n) ? "claude" : /^jeff/.test(n) ? "jeff" : /jarvis/.test(n) ? "jarvis"
    : /embed/.test(n) ? "embed" : "ollama";
  return {name, family, where: family === "claude" ? "cloud" : "gpu", loaded: true, vram_mb: null, calls_5m: 1};
}
function mk(key, kind, data, delay) {
  const n = {key, place: kind, data, seed: hash(key), x: 0, y: 0, tx: 0, ty: 0, a: 0, ta: 1, born: now() + (delay || 0),
    pulse: 0, litUntil: 0, s: 1, fresh: true, until: 0, transient: false};
  dress(n);
  nodes.set(key, n);
  return n;
}
function dress(n) {
  const d = obj(n.data);
  if (n.place === "lane") {  // T-3044 review: the bench says how far its builder is, "2/5 · executing"
    const sp = stepsOf(d);
    n.label = str(d.task); n.sub = [sp ? sp.done + "/" + sp.total : "", str(d.stage)].filter(Boolean).join(" · ") || str(d.model) || "builder";
    return;
  }
  if (n.place === "job") {
    const p = obj(d.progress), done = num(p.done), total = num(p.total);
    n.prog = done != null && total ? clamp(done / total, 0, 1) : null;
    n.kind = JOBC[str(d.kind)] ? str(d.kind) : "other";
    n.label = n.prog != null ? n.kind + " " + Math.round(n.prog * 100) + "%" : n.kind;
    n.sub = str(d.name).split(/\s+/).filter(w => !/^-/.test(w)).slice(0, 2).join(" ");
    return;
  }
  if (n.place === "anvil") return;
  const calls = num(d.calls_5m) || 0, vram = num(d.vram_mb) || 0;
  n.loaded = d.loaded !== false;
  n.act = n.loaded ? 0.35 + 0.65 * Math.min(1, Math.log1p(calls) / Math.log1p(120)) : 0;
  n.k = 0.82 + 0.4 * Math.min(1, Math.log1p(calls) / Math.log1p(60));
  n.r = (6 + 12 * Math.sqrt(clamp(vram / 24576, 0, 1))) * (vram ? 1 : 0.85);
  n.label = str(d.name);
  n.sub = n.place === "hang" ? (vram ? gb(vram) : n.loaded ? "" : "unloaded") : calls ? calls + " calls" : "";
  n.fam = str(d.family);
}

function sceneTake(s, first) {
  if (!cx) return;
  const t = now(), want = new Map();
  arr(s.lanes).slice(0, 10).forEach(l => { const k = str(obj(l).task); if (k) want.set("lane:" + k, ["lane", l]); });
  arr(s.jobs).slice(0, 12).forEach(j => {
    const o = obj(j), id = num(o.pid) != null ? "pid" + o.pid : str(o.name);
    if (id) want.set("job:" + id, ["job", o]);
  });
  arr(s.models).slice(0, 28).forEach(m => { const o = obj(m), k = str(o.name); if (k) want.set("mdl:" + k, [place(o), o]); });
  for (const n of nodes.values()) {
    if (n.place === "anvil") continue;
    const w = want.get(n.key);
    if (w) {
      n.data = w[1]; if (n.place !== w[0]) { n.place = w[0]; } n.ta = 1; n.transient = false; dress(n); want.delete(n.key);
    } else if (!(n.transient && n.until > t)) n.ta = 0;
  }
  const stagger = first && intro ? 0.12 : 0.06;
  let i = 0;
  for (const [k, [kind, d]] of want) mk(k, kind, d, (first && intro ? 1.1 : 0) + stagger * i++);
  if (G) layout();
  flowsIn(s.flows, t);
  cnt.nodes = [...nodes.values()].filter(n => n.ta > 0).length;
  cnt.benches = [...nodes.values()].filter(n => n.place === "lane" && n.ta > 0).map(n => n.label + " " + n.sub).join(" | ");
  publish();
  if (still()) paintStill();
}

function row(list, seg, gap) {
  const n = list.length;
  if (!n) return;
  const span = seg.x1 - seg.x0;
  let per = Math.max(1, Math.min(n, Math.floor(span / gap) + 1));
  const rows = Math.ceil(n / per);
  per = Math.ceil(n / rows);
  list.forEach((nd, i) => {
    const r = Math.floor(i / per), c = i % per, m = Math.min(per, n - r * per);
    const step = m > 1 ? Math.min(gap * 1.4, span / (m - 1)) : 0, mid = (seg.x0 + seg.x1) / 2;
    nd.tx = mid - step * (m - 1) / 2 + step * c;
    nd.ty = seg.y - r * gap * 0.62;
    nd.s = 1 - r * 0.14;
  });
}

function layout() {
  if (!G) return;  // the forge canvas has no size while the network view is up
  const groups = {lane: [], job: [], sky: [], hang: [], jeff: [], hill: []};
  for (const n of nodes.values()) if (n.ta > 0 && groups[n.place]) groups[n.place].push(n);
  row(groups.lane, G.lanes, 92 * U);
  row(groups.job, G.jobs, 96 * U);
  const S = G.sky, sk = groups.sky;
  sk.forEach((n, i) => {
    const f = sk.length === 1 ? 0.5 : i / (sk.length - 1);
    n.tx = lerp(S.x0, S.x1, f) + ((n.seed % 100) / 100 - 0.5) * 18 * U;
    n.ty = (i % 2 ? S.y1 : S.y0) + ((n.seed >>> 8) % 100) / 100 * 0.25 * (S.y1 - S.y0);
  });
  const hg = groups.hang, x0 = G.beam.x0 + 44 * U, x1 = G.beam.x1 - 44 * U;
  hg.forEach((n, i) => {
    n.tx = hg.length === 1 ? (x0 + x1) / 2 : lerp(x0, x1, i / (hg.length - 1));
    n.ty = G.beam.y + 14 * U;
    n.chain = (22 + (n.seed % 5) * 9) * U;
  });
  groups.jeff.forEach((n, i) => {
    const T = G.towers[[1, 0, 2][i % 3]], lvl = Math.floor(i / 3);
    n.tx = T.x; n.ty = T.top + 15.5 * U + lvl * 17 * U;
  });
  groups.hill.forEach((n, i) => { n.tx = G.lamp.x; n.ty = G.lamp.y + (i ? 14 * U + i * 9 * U : 0); });
  const a = nodes.get("anvil");
  a.tx = G.anvil.x; a.ty = G.anvil.y;
  for (const n of nodes.values()) if (n.fresh || still()) { n.x = n.tx; n.y = n.ty; n.fresh = false; }
}

// where a spark leaves from and lands on
function anchor(n, t) {
  const s = n.s * U;
  switch (n.place) {
    case "anvil": return [G.strike.x, G.strike.y - 4 * U];
    case "lane": return [n.x - 8 * s, n.y - 30 * s];
    case "job": return [n.x, n.y - 16 * s];
    case "sky": { const p = bob(n, t); return [p[0], p[1] + 15 * U * n.k]; }
    case "hang": { const p = hang(n, t); return [p.x, p.y]; }
    default: return [n.x, n.y];
  }
}
function bob(n, t) {
  const s = n.seed % 1000 / 159;
  return still() ? [n.x, n.y] : [n.x + 3 * U * Math.sin(t * 0.37 + s), n.y + 5 * U * Math.sin(t * 0.6 + s * 1.3)];
}
function hang(n, t) {
  const sw = still() ? 0 : 0.045 * Math.sin(t * 0.7 + n.seed % 97), len = n.chain + n.r * U * 1.1;
  return {x: n.x - Math.sin(sw) * len, y: n.y + Math.cos(sw) * len, sw};
}

const KW = [[/funnel|moon/, "funnel"], [/grow|card/, "grow"], [/exam/, "exam"], [/train/, "train"], [/bench/, "bench"],
  [/check|test/, "check"]];
function source(from) {
  const f = from.toLowerCase().trim(), anvil = nodes.get("anvil");
  if (!f || /^(session|claude)\b/.test(f)) return anvil;
  const jobs = [], lanes = new Map();
  for (const n of nodes.values()) if (n.ta > 0) {
    if (n.place === "job") jobs.push(n);
    else if (n.place === "lane") lanes.set(str(n.data.task).toUpperCase(), n);
  }
  for (const n of jobs) { const nm = str(n.data.name).toLowerCase(); if (nm && f && (nm === f || nm.startsWith(f) || f.startsWith(nm))) return n; }
  const tid = /\bT-\d+\b/i.exec(from);
  if (tid) return lanes.get(tid[0].toUpperCase()) || anvil;
  for (const [re, kind] of KW) if (re.test(f)) { const n = jobs.find(j => str(j.data.kind) === kind); if (n) return n; }
  return anvil;
}
function target(to) {
  let n = nodes.get("mdl:" + to);
  if (n) { if (n.ta === 0) { n.ta = 1; n.transient = true; n.until = now() + 30; layout(); } return n; }
  if (nodes.size > 90) return null;
  n = mk("mdl:" + to, place(guess(to)), guess(to), 0);
  n.transient = true; n.until = now() + 30;
  layout();
  return n;
}

function flowsIn(list, t) {
  const fresh = [];
  for (const f of arr(list).slice(0, 120)) {
    const o = obj(f), to = str(o.to).trim();
    if (!to) continue;
    const k = str(o.from) + "|" + to + "|" + str(o.kind) + "|" + str(o.at);
    if (seen.has(k)) continue;
    seen.set(k, 1);
    fresh.push({from: str(o.from), to, kind: KIND[str(o.kind)] ? str(o.kind) : "review"});
  }
  while (seen.size > 600) seen.delete(seen.keys().next().value);
  if (!fresh.length || document.hidden) return;
  if (still()) {
    for (const f of fresh) {
      const a = source(f.from), b = target(f.to);
      if (a) a.litUntil = t + 6;
      if (b) b.litUntil = t + 6;
      if (a || b) cnt.lit++;
    }
    return;
  }
  fresh.reverse();  // oldest first, so the burst plays in the order it happened
  const base = Math.max(t, intro ? introAt + 1.9 : t), spread = Math.min(1.8, 0.22 * fresh.length);
  fresh.forEach((f, i) => {
    if (pending.length < 200) pending.push(Object.assign(f, {at: base + (fresh.length > 1 ? spread * i / (fresh.length - 1) : 0) + R() * 0.1}));
  });
}

function spawnDue(t) {
  for (let i = 0; i < pending.length;) {
    if (pending[i].at > t) { i++; continue; }
    const p = pending.splice(i, 1)[0], a = source(p.from), b = target(p.to);
    if (!a || !b || a === b || sparks.length >= 160) continue;
    const A = anchor(a, t), B = anchor(b, t), d = Math.hypot(B[0] - A[0], B[1] - A[1]);
    sparks.push({a, b, c: KIND[p.kind], t0: t, dur: 0.95 + Math.min(1.25, d / (650 * U)), lift: 0.16 + R() * 0.18, A, B});
    a.pulse = Math.max(a.pulse, 0.55);
    cnt.sparks++;
  }
}

// ---------- drawing ----------
function glow(x, y, r, c, a) {
  if (a <= 0.004 || r <= 0.5) return;
  const g = cx.createRadialGradient(x, y, 0, x, y, r);
  g.addColorStop(0, rgba(c, a)); g.addColorStop(0.3, rgba(c, a * 0.42)); g.addColorStop(1, rgba(c, 0));
  cx.fillStyle = g;
  cx.beginPath(); cx.arc(x, y, r, 0, TAU); cx.fill();
}
function pool(x, y, rx, ry, c, a) {
  if (a <= 0.004) return;
  cx.save(); cx.translate(x, y); cx.scale(1, ry / rx);
  glow(0, 0, rx, c, a);
  cx.restore();
}
function label(x, y, text, size, color, pri, a, face) {
  if (!text || a < 0.15) return;
  const s = Math.max(9, size);   // a floored size grows upward from its baseline: drop it by the growth so it clears what it labels
  labels.push({x, y: y + s - size, text, size: s, color, pri, a, face});
}
function drawLabels() {
  labels.sort((p, q) => q.pri - p.pri);
  const placed = [];
  cx.textBaseline = "alphabetic"; cx.textAlign = "left";
  cx.shadowColor = "rgba(2,3,10,.92)"; cx.shadowBlur = 5;
  for (const L of labels) {
    cx.font = L.face === "display" ? "500 " + (L.size * 1.25).toFixed(1) + "px \"Bodoni Moda\", Didot, Georgia, serif"
      : "500 " + L.size.toFixed(1) + "px \"Martian Mono\", ui-monospace, Menlo, monospace";
    const w = cx.measureText(L.text).width, hgt = L.face === "display" ? L.size * 1.2 : L.size;
    const x0 = clamp(L.x - w / 2, 4, W - 4 - w), box = [x0 - 4, L.y - hgt, x0 + w + 4, L.y + 3];
    if (placed.some(p => box[0] < p[2] && box[2] > p[0] && box[1] < p[3] && box[3] > p[1])) continue;
    placed.push(box);
    cx.globalAlpha = L.a; cx.fillStyle = L.color; cx.fillText(L.text, x0, L.y);
  }
  cx.shadowBlur = 0; cx.shadowColor = "transparent"; cx.globalAlpha = 1;
  labels.length = 0;
}

function drawSky(n, t, still) {
  const [x, y] = bob(n, t), w = 22 * U * n.k, hh = 29 * U * n.k, a = n.a;
  const fl = still ? 0.9 : 0.78 + 0.22 * flick(t * 0.6, n.seed % 50);
  const I = clamp(n.act * fl + n.pulse * 0.7, 0, 1.4);
  cx.globalCompositeOperation = "lighter";
  glow(x, y, w * 3.4, GOLD, 0.2 * I * a);
  cx.globalCompositeOperation = "source-over";
  cx.beginPath();
  cx.moveTo(x - w * 0.34, y - hh / 2);
  cx.bezierCurveTo(x - w * 0.66, y - hh * 0.22, x - w * 0.64, y + hh * 0.28, x - w * 0.36, y + hh / 2);
  cx.lineTo(x + w * 0.36, y + hh / 2);
  cx.bezierCurveTo(x + w * 0.64, y + hh * 0.28, x + w * 0.66, y - hh * 0.22, x + w * 0.34, y - hh / 2);
  cx.closePath();
  const g = cx.createRadialGradient(x, y + hh * 0.22, 1, x, y, hh * 0.72);
  g.addColorStop(0, rgba([255, 246, 220], a * (0.55 + 0.45 * I)));
  g.addColorStop(0.5, rgba([250, 178, 92], a * (0.5 + 0.45 * I)));
  g.addColorStop(1, rgba([150, 60, 28], a * 0.9));
  cx.fillStyle = g; cx.fill();
  cx.strokeStyle = rgba([110, 44, 20], 0.4 * a); cx.lineWidth = 0.8 * U;
  for (const f of [-0.3, 0, 0.3]) {
    cx.beginPath(); cx.moveTo(x + f * w * 0.8, y - hh / 2);
    cx.quadraticCurveTo(x + f * w * 1.5, y, x + f * w * 0.85, y + hh / 2); cx.stroke();
  }
  cx.fillStyle = rgba([52, 24, 14], a);
  cx.fillRect(x - w * 0.36, y - hh / 2 - 1.5 * U, w * 0.72, 2.6 * U);
  cx.fillRect(x - w * 0.38, y + hh / 2 - 1 * U, w * 0.76, 2.4 * U);
  cx.globalCompositeOperation = "lighter";
  glow(x, y + hh / 2 + 1.5 * U, 7 * U, PALE, 0.75 * a * fl);
  cx.globalCompositeOperation = "source-over";
  label(x, y + hh / 2 + 15 * U, n.label, 10.5 * U, rgba([238, 240, 250], 0.92), 6, a);
  label(x, y + hh / 2 + 27 * U, n.sub, 9 * U, rgba([144, 151, 188], 1), 3, a);
}

function drawHang(n, t, still) {
  const a = n.a, top = {x: n.x, y: G.beam.y + 14 * U}, p = hang(n, t), r = n.r * U;
  cx.strokeStyle = rgba([150, 116, 72], 0.7 * a); cx.lineWidth = 1.1 * U;
  cx.setLineDash([2.6 * U, 1.8 * U]);
  cx.beginPath(); cx.moveTo(top.x, top.y); cx.lineTo(p.x + Math.sin(p.sw) * r * 1.1, p.y - Math.cos(p.sw) * r * 1.1); cx.stroke();
  cx.setLineDash([]);
  cx.save(); cx.translate(p.x, p.y); cx.rotate(p.sw);
  const fl = still ? 0.9 : flick(t, n.seed % 40), I = n.loaded ? clamp(n.act * (0.75 + 0.3 * fl) + n.pulse * 0.6, 0, 1.4) : 0;
  const col = n.fam === "embed" ? [120, 240, 190] : WARM;
  cx.fillStyle = rgba([24, 20, 26], a);
  cx.beginPath(); cx.moveTo(-r * 0.5, -r * 1.1); cx.lineTo(-r * 0.25, -r * 1.45); cx.lineTo(r * 0.25, -r * 1.45); cx.lineTo(r * 0.5, -r * 1.1);
  cx.closePath(); cx.fill();
  const g = cx.createRadialGradient(0, r * 0.15, 0, 0, 0, r * 1.2);
  if (I > 0) {
    g.addColorStop(0, rgba([255, 250, 235], a * (0.6 + 0.4 * I)));
    g.addColorStop(0.45, rgba(col, a * (0.45 + 0.45 * I)));
    g.addColorStop(1, rgba([90, 50, 30], a * 0.85));
  } else {
    g.addColorStop(0, rgba([40, 44, 66], a)); g.addColorStop(1, rgba([18, 20, 34], a));
  }
  cx.fillStyle = g;
  cx.beginPath(); cx.moveTo(-r * 0.62, -r * 1.05); cx.lineTo(r * 0.62, -r * 1.05); cx.lineTo(r * 0.7, r * 0.75);
  cx.lineTo(-r * 0.7, r * 0.75); cx.closePath(); cx.fill();
  cx.strokeStyle = rgba([28, 22, 26], a); cx.lineWidth = Math.max(1, 1.3 * U);
  cx.stroke();
  cx.beginPath(); cx.moveTo(0, -r * 1.05); cx.lineTo(0, r * 0.75); cx.stroke();
  if (!I) { cx.strokeStyle = rgba([150, 170, 240], 0.22 * a); cx.beginPath(); cx.moveTo(-r * 0.4, -r * 0.8); cx.lineTo(-r * 0.45, r * 0.3); cx.stroke(); }
  cx.fillStyle = rgba([24, 20, 26], a);
  cx.fillRect(-r * 0.78, r * 0.72, r * 1.56, Math.max(2, r * 0.22));
  cx.restore();
  if (I > 0) {
    cx.globalCompositeOperation = "lighter";
    glow(p.x, p.y, r * 4.2, col, 0.24 * I * a);
    cx.globalCompositeOperation = "source-over";
  }
  label(p.x, p.y + r * 1.15 + 13 * U, cut(n.label, G.wide ? 20 : 11), 10 * U, rgba([238, 240, 250], I ? 0.9 : 0.55), 5, a);
  label(p.x, p.y + r * 1.15 + 25 * U, n.sub, 9 * U, rgba([144, 151, 188], 1), 2, a);
}

function drawTowerLight(n, t, still) {
  const a = n.a, fl = still ? 0.9 : 0.8 + 0.2 * flick(t * 0.5, n.seed % 30), I = n.loaded ? clamp(n.act * fl + n.pulse * 0.7, 0, 1.4) : 0;
  if (n.place === "hill" && n === hillHead) {
    if (I && !still) {
      cx.globalCompositeOperation = "lighter";
      const ang = t * 0.45;
      for (const off of [0, Math.PI]) {
        const q = ang + off, L = 300 * U;
        const g = cx.createLinearGradient(n.x, n.y, n.x + Math.cos(q) * L, n.y + Math.sin(q) * L * 0.3);
        g.addColorStop(0, rgba([190, 210, 255], 0.13 * a * I)); g.addColorStop(1, rgba([190, 210, 255], 0));
        cx.fillStyle = g;
        cx.beginPath(); cx.moveTo(n.x, n.y);
        cx.lineTo(n.x + Math.cos(q - 0.07) * L, n.y + Math.sin(q - 0.07) * L * 0.3);
        cx.lineTo(n.x + Math.cos(q + 0.07) * L, n.y + Math.sin(q + 0.07) * L * 0.3);
        cx.closePath(); cx.fill();
      }
      cx.globalCompositeOperation = "source-over";
    }
    cx.globalCompositeOperation = "lighter";
    const pulse = still ? 1 : 0.75 + 0.25 * Math.sin(t * 1.3);
    glow(n.x, n.y, 46 * U, [150, 185, 255], 0.38 * I * a * pulse);
    glow(n.x, n.y, 6 * U, [234, 242, 255], 0.95 * a * (I ? 1 : 0.2));
    cx.globalCompositeOperation = "source-over";
    label(n.x, n.y - 18 * U, "Jarvis", 11 * U, rgba(BRASS, 0.95), 4, a, "display");
    label(n.x, n.y + 62 * U, cut(n.label, 22), 9.5 * U, rgba([238, 240, 250], 0.85), 4, a);
    return;
  }
  const c = n.place === "hill" ? [180, 205, 255] : [214, 190, 255];
  cx.fillStyle = rgba(I ? [250, 240, 255] : [30, 30, 56], a * (I ? 0.55 + 0.45 * I : 1));
  cx.fillRect(n.x - 2.4 * U, n.y - 3.5 * U, 4.8 * U, 7 * U);
  if (I) {
    cx.globalCompositeOperation = "lighter";
    glow(n.x, n.y, 26 * U, c, 0.5 * I * a);
    glow(n.x, n.y, 7 * U, [255, 250, 255], 0.5 * I * a);
    cx.globalCompositeOperation = "source-over";
  }
}
let hillHead = null;

function drawFurnace(n, t, still, ig) {
  const s = n.s * U, x = n.x, y = n.y, a = n.a;
  const I = clamp((n.prog != null ? 0.35 + 0.65 * n.prog : 0.62 + 0.16 * Math.sin(t * 0.9 + n.seed % 7) +
    (still ? 0 : 0.1 * (flick(t, n.seed % 13) - 0.55))) * ig + n.pulse * 0.5, 0, 1.4);
  cx.globalAlpha = a;
  cx.globalCompositeOperation = "lighter";
  pool(x, y + 2 * s, 74 * s, 13 * s, EMBER, 0.2 * I);
  cx.globalCompositeOperation = "source-over";
  cx.fillStyle = "#130F16"; cx.fillRect(x + 8 * s, y - 100 * s, 12 * s, 42 * s);
  cx.fillStyle = "#211A22"; cx.fillRect(x + 6 * s, y - 104 * s, 16 * s, 5 * s);
  cx.beginPath();
  cx.moveTo(x - 32 * s, y); cx.lineTo(x - 27 * s, y - 56 * s);
  cx.quadraticCurveTo(x, y - 84 * s, x + 27 * s, y - 56 * s); cx.lineTo(x + 32 * s, y); cx.closePath();
  const g = cx.createLinearGradient(x - 32 * s, 0, x + 32 * s, 0);
  g.addColorStop(0, "#2A1D21"); g.addColorStop(0.5, "#1A1419"); g.addColorStop(1, "#0E0B11");
  cx.fillStyle = g; cx.fill();
  cx.save(); cx.clip();
  cx.strokeStyle = "rgba(0,0,0,.4)"; cx.lineWidth = 1;
  for (let k = 0; k < 9; k++) {
    const yy = y - k * 9 * s;
    cx.beginPath(); cx.moveTo(x - 34 * s, yy); cx.lineTo(x + 34 * s, yy); cx.stroke();
    for (let xx = x - 34 * s + (k % 2) * 7 * s; xx < x + 34 * s; xx += 14 * s) { cx.beginPath(); cx.moveTo(xx, yy - 9 * s); cx.lineTo(xx, yy); cx.stroke(); }
  }
  cx.globalCompositeOperation = "lighter";
  pool(x, y - 8 * s, 40 * s, 30 * s, EMBER, 0.12 * I);
  cx.restore();
  cx.strokeStyle = "rgba(224,144,96,.3)"; cx.lineWidth = 1;
  cx.beginPath(); cx.moveTo(x - 32 * s, y); cx.lineTo(x - 27 * s, y - 56 * s); cx.quadraticCurveTo(x - 16 * s, y - 72 * s, x, y - 70 * s); cx.stroke();
  // the mouth and its fire
  cx.beginPath();
  cx.moveTo(x - 13 * s, y - 2 * s); cx.lineTo(x - 13 * s, y - 17 * s); cx.arc(x, y - 17 * s, 13 * s, Math.PI, 0);
  cx.lineTo(x + 13 * s, y - 2 * s); cx.closePath();
  const m = cx.createRadialGradient(x, y - 6 * s, 0, x, y - 12 * s, 20 * s);
  m.addColorStop(0, heat(0.62 + 0.36 * I, 1)); m.addColorStop(0.55, heat(0.3 + 0.3 * I, 1)); m.addColorStop(1, "#1A0A08");
  cx.fillStyle = m; cx.fill();
  if (!still) {
    cx.save(); cx.clip(); cx.globalCompositeOperation = "lighter";
    for (let k = 0; k < 4; k++) {
      const fx = x + (k - 1.5) * 6 * s, fh = (12 + 10 * flick(t * 1.4 + k, n.seed % 9 + k)) * s * I;
      const fg = cx.createLinearGradient(0, y - 2 * s, 0, y - 2 * s - fh);
      fg.addColorStop(0, heat(0.85, 0.7)); fg.addColorStop(1, heat(0.5, 0));
      cx.fillStyle = fg;
      cx.beginPath(); cx.moveTo(fx - 4 * s, y - 2 * s);
      cx.quadraticCurveTo(fx - 3 * s, y - 2 * s - fh * 0.6, fx + Math.sin(t * 5 + k) * 2 * s, y - 2 * s - fh);
      cx.quadraticCurveTo(fx + 3 * s, y - 2 * s - fh * 0.6, fx + 4 * s, y - 2 * s); cx.fill();
    }
    cx.restore();
  }
  cx.globalCompositeOperation = "lighter";
  glow(x, y - 12 * s, 56 * s, EMBER, 0.34 * I);
  cx.globalCompositeOperation = "source-over";
  // the brass plate with the job's colour, and the progress around it
  const kc = hex(JOBC[n.kind]);
  cx.fillStyle = "#3A2C1E"; cx.fillRect(x - 7 * s, y - 45 * s, 14 * s, 8 * s);
  cx.fillStyle = rgba(kc, 0.95); cx.beginPath(); cx.arc(x, y - 41 * s, 2.2 * s, 0, TAU); cx.fill();
  if (n.prog != null) {
    cx.strokeStyle = rgba([144, 151, 188], 0.3); cx.lineWidth = 1.6 * s;
    cx.beginPath(); cx.arc(x, y - 41 * s, 9 * s, 0, TAU); cx.stroke();
    cx.strokeStyle = rgba(GOLD, 0.95);
    cx.beginPath(); cx.arc(x, y - 41 * s, 9 * s, -Math.PI / 2, -Math.PI / 2 + TAU * n.prog); cx.stroke();
  }
  cx.globalAlpha = 1;
  label(x, y + 14 * s, n.label.toUpperCase(), 9.5 * U, JOBC[n.kind], 7, a);
  label(x, y + 26 * s, cut(n.sub, 16), 9 * U, rgba([186, 192, 221], 0.9), 4, a);
}
const hex = c => [parseInt(c.slice(1, 3), 16), parseInt(c.slice(3, 5), 16), parseInt(c.slice(5, 7), 16)];

function figure(x, y, s, a, rimSide) {
  // a backlit smith: head, neck, shoulders, apron; the rim light falls on the side facing the fire
  const p = new Path2D();
  p.arc(x + 1 * s, y - 25 * s, 9.5 * s, 0, TAU);
  p.moveTo(x - 5 * s, y - 16 * s); p.lineTo(x + 6 * s, y - 16 * s); p.lineTo(x + 7 * s, y - 10 * s);
  p.quadraticCurveTo(x + 24 * s, y - 9 * s, x + 25 * s, y + 2 * s);
  p.lineTo(x + 20 * s, y + 30 * s); p.lineTo(x + 23 * s, y + 70 * s); p.lineTo(x - 22 * s, y + 70 * s); p.lineTo(x - 19 * s, y + 30 * s);
  p.lineTo(x - 24 * s, y + 2 * s); p.quadraticCurveTo(x - 23 * s, y - 9 * s, x - 6 * s, y - 10 * s); p.closePath();
  cx.save();
  cx.translate(rimSide * 1.6 * s, -1.2 * s);
  cx.fillStyle = rgba([255, 168, 96], 0.75 * a); cx.fill(p);
  cx.restore();
  cx.fillStyle = rgba([7, 6, 12], a); cx.fill(p);
}
function limb(x0, y0, x1, y1, w, a, rim) {
  cx.lineCap = "round";
  if (rim) { cx.strokeStyle = rgba([255, 168, 96], 0.6 * a); cx.lineWidth = w + 2.4 * U; cx.beginPath(); cx.moveTo(x0 - U, y0 - U); cx.lineTo(x1 - U, y1 - U); cx.stroke(); }
  cx.strokeStyle = rgba([7, 6, 12], a); cx.lineWidth = w;
  cx.beginPath(); cx.moveTo(x0, y0); cx.lineTo(x1, y1); cx.stroke();
}

function drawAnvil(t, dt, still, ig) {
  const A = G.anvil, s = U, I = G.strike, M = G.smith;
  // the hammer arm: raise, strike, rest
  const S = [M.x - 19 * s, M.y - 2 * s], D = Math.hypot(I.x - S[0], I.y - S[1]), th = Math.atan2(I.y - S[1], I.x - S[0]);
  let ang = th, lean = 0;
  if (!still) {
    const P = 1.45, u = ((t - introAt) % P + P) % P / P, raised = th + 2.05;
    if (u < 0.62) { const e = u / 0.62; ang = lerp(th, raised, e * e * (3 - 2 * e)); lean = -e * 2; }
    else if (u < 0.74) { const e = (u - 0.62) / 0.12; ang = lerp(raised, th, e * e); lean = -2 + 3 * e; }
    else { const e = (u - 0.74) / 0.26; ang = th + 0.14 * Math.sin(Math.PI * e) * (1 - e); lean = 1 - e; }
    const cyc = Math.floor((t - introAt) / P);
    if (u >= 0.74 && cyc !== strikeAt && t - introAt > 1.2) { strikeAt = cyc; strike(I.x, I.y, ig); }
  }
  flash *= Math.exp(-dt * 7);
  figure(M.x + lean * s * 0.5, M.y, s, 1, -1);
  // the free arm holds the work with tongs
  const L = [M.x + 20 * s, M.y + 1 * s], hand = [A.x + 12 * s, A.y - 14 * s];
  limb(L[0], L[1], hand[0], hand[1], 8 * s, 1, false);
  cx.strokeStyle = "#0A0910"; cx.lineWidth = 2 * s;
  cx.beginPath(); cx.moveTo(hand[0], hand[1]); cx.lineTo(I.x + 10 * s, I.y - 1 * s); cx.stroke();
  // the hammer arm
  const hx = S[0] + Math.cos(ang) * D * 0.5, hy = S[1] + Math.sin(ang) * D * 0.5;
  const ex = S[0] + Math.cos(ang) * (D - 5 * s), ey = S[1] + Math.sin(ang) * (D - 5 * s);
  limb(S[0], S[1], hx, hy, 9 * s, 1, true);
  cx.strokeStyle = "#140F12"; cx.lineWidth = 2.6 * s; cx.lineCap = "round";
  cx.beginPath(); cx.moveTo(hx, hy); cx.lineTo(ex, ey); cx.stroke();
  cx.save(); cx.translate(ex, ey); cx.rotate(ang);
  cx.fillStyle = "#1B1A22"; cx.fillRect(-3 * s, -7.5 * s, 9 * s, 15 * s);
  cx.fillStyle = "rgba(255,170,110,.5)"; cx.fillRect(-3 * s, -7.5 * s, 9 * s, 1.4 * s);
  cx.restore();
  // the anvil on its stump
  cx.fillStyle = "#1E130F"; cx.fillRect(A.x - 36 * s, A.y + 52 * s, 72 * s, 24 * s);
  cx.fillStyle = "rgba(224,144,96,.2)"; cx.fillRect(A.x - 36 * s, A.y + 52 * s, 72 * s, 1.5 * s);
  cx.beginPath();
  cx.moveTo(A.x - 80 * s, A.y + 5 * s); cx.quadraticCurveTo(A.x - 60 * s, A.y - 1 * s, A.x - 40 * s, A.y);
  cx.lineTo(A.x + 54 * s, A.y); cx.lineTo(A.x + 58 * s, A.y + 2 * s); cx.lineTo(A.x + 58 * s, A.y + 14 * s);
  cx.lineTo(A.x + 30 * s, A.y + 18 * s); cx.quadraticCurveTo(A.x + 20 * s, A.y + 30 * s, A.x + 38 * s, A.y + 46 * s);
  cx.lineTo(A.x + 44 * s, A.y + 53 * s); cx.lineTo(A.x - 40 * s, A.y + 53 * s); cx.lineTo(A.x - 34 * s, A.y + 46 * s);
  cx.quadraticCurveTo(A.x - 18 * s, A.y + 30 * s, A.x - 26 * s, A.y + 18 * s);
  cx.quadraticCurveTo(A.x - 52 * s, A.y + 15 * s, A.x - 80 * s, A.y + 5 * s); cx.closePath();
  const g = cx.createLinearGradient(0, A.y, 0, A.y + 53 * s);
  g.addColorStop(0, "#2A2A36"); g.addColorStop(0.25, "#15161F"); g.addColorStop(1, "#08080C");
  cx.fillStyle = g; cx.fill();
  cx.strokeStyle = rgba([255, 184, 120], 0.55 + 0.4 * flash); cx.lineWidth = 1.4 * s;
  cx.beginPath(); cx.moveTo(A.x - 78 * s, A.y + 4.5 * s); cx.quadraticCurveTo(A.x - 60 * s, A.y - 1 * s, A.x - 40 * s, A.y);
  cx.lineTo(A.x + 54 * s, A.y); cx.stroke();
  cx.strokeStyle = "rgba(150,170,240,.22)"; cx.lineWidth = 1 * s;
  cx.beginPath(); cx.moveTo(A.x + 58 * s, A.y + 2 * s); cx.lineTo(A.x + 58 * s, A.y + 14 * s); cx.stroke();
  // the work, white-hot after each blow
  const wv = clamp(0.72 + 0.1 * Math.sin(t * 2) * (still ? 0 : 1) + 0.25 * flash, 0, 1) * ig;
  cx.fillStyle = heat(wv, 1); cx.fillRect(I.x - 10 * s, I.y - 3 * s, 24 * s, 3.6 * s);
  cx.globalCompositeOperation = "lighter";
  glow(I.x, I.y - 1 * s, (34 + 26 * flash) * s, EMBER, (0.5 + 0.4 * flash) * ig);
  glow(I.x, I.y - 1 * s, 9 * s, PALE, (0.6 + 0.4 * flash) * ig);
  cx.globalCompositeOperation = "source-over";
  const n = nodes.get("anvil");
  if (n.pulse > 0.02) { cx.globalCompositeOperation = "lighter"; glow(I.x, I.y, 50 * s, GOLD, 0.4 * n.pulse); cx.globalCompositeOperation = "source-over"; }
  label(A.x, A.y + 90 * s, "Claude", 12 * U, rgba(BRASS, 0.96), 10, 1, "display");
}

function strike(x, y, ig) {
  flash = 1;
  const n = 12 + Math.floor(R() * 10);
  for (let i = 0; i < n && streaks.length < 180; i++) {
    const a = -Math.PI / 2 + (R() - 0.5) * 2.6, v = (170 + R() * 360) * U;
    streaks.push({x, y, vx: Math.cos(a) * v, vy: Math.sin(a) * v - 60 * U, life: 0.3 + R() * 0.55, max: 0.85, w: 0.9 + R() * 0.8});
  }
  for (let i = 0; i < 4 && embers.length < 320; i++) emit(x + (R() - 0.5) * 20 * U, y - 2 * U, 1, 0.9 * ig);
}
function emit(x, y, scale, hot, c) {
  embers.push({x, y, vx: (R() - 0.5) * 24 * U, vy: -(30 + R() * 70) * U * scale, life: 1 + R() * 2, max: 3,
    r: (0.7 + R() * 1.2) * U, hot, seed: R() * 9, c});
}

function drawBench(n, t, still) {
  const s = n.s * U, x = n.x, y = n.y, a = n.a, sd = n.seed % 1000 / 1000;
  const fl = still ? 0.9 : flick(t * 0.8, sd * 20);
  cx.globalAlpha = a;
  cx.globalCompositeOperation = "lighter";
  glow(x + 22 * s, y - 31 * s, 40 * s, WARM, 0.2 * fl + n.pulse * 0.3);
  pool(x, y + 1 * s, 52 * s, 8 * s, WARM, 0.12);
  cx.globalCompositeOperation = "source-over";
  // the smith, tapping at the work
  const P = 0.95 + sd * 0.9, u = still ? 0.3 : ((t + sd * 5) % P) / P;
  const lift = u < 0.75 ? Math.sin(u / 0.75 * Math.PI / 2) : 1 - (u - 0.75) / 0.25;
  cx.save(); cx.translate(x + 2 * s, y - 50 * s); cx.scale(0.42, 0.42);
  figure(0, 0, s, a, 1);
  cx.restore();
  limb(x - 6 * s, y - 50 * s, x - 9 * s - 2 * s * lift, y - 30 * s - 6 * s * lift, 3.6 * s, a, false);
  if (!still && u > 0.995 - 0.01 && streaks.length < 180) {
    for (let i = 0; i < 2; i++) streaks.push({x: x - 9 * s, y: y - 28 * s, vx: (R() - 0.5) * 120 * U, vy: -(60 + R() * 90) * U, life: 0.25 + R() * 0.2, max: 0.45, w: 0.7});
  }
  // the bench and its lamp
  cx.fillStyle = "#24170F"; cx.fillRect(x - 30 * s, y - 27 * s, 60 * s, 5 * s);
  cx.fillStyle = "rgba(255,200,140,.32)"; cx.fillRect(x - 30 * s, y - 27 * s, 60 * s, 1 * s);
  cx.fillStyle = "#150D09"; cx.fillRect(x - 27 * s, y - 22 * s, 3.5 * s, 22 * s); cx.fillRect(x + 23.5 * s, y - 22 * s, 3.5 * s, 22 * s);
  cx.fillRect(x - 24 * s, y - 9 * s, 48 * s, 2.5 * s);
  cx.fillStyle = "#1E1A22"; cx.fillRect(x - 14 * s, y - 31 * s, 9 * s, 4 * s);
  cx.fillStyle = "#2A2228"; cx.fillRect(x + 19 * s, y - 37 * s, 6 * s, 10 * s);
  cx.globalCompositeOperation = "lighter";
  glow(x + 22 * s, y - 33 * s, 6 * s, PALE, 0.8 * fl);
  cx.globalCompositeOperation = "source-over";
  cx.globalAlpha = 1;
  label(x, y + 14 * s, n.label, 10.5 * U, rgba([238, 240, 250], 0.95), 8, a);
  label(x, y + 26 * s, cut(n.sub, 16), 9 * U, rgba([144, 151, 188], 1), 4, a);
}

function drawHearth(t, still, ig) {
  const hx = G.hearth.x, hy = G.hearth.y, f = still ? 0.9 : flick(t * 0.9, 3);
  const I = ig * (0.85 + 0.15 * f) + flash * 0.12;
  cx.globalCompositeOperation = "lighter";
  glow(hx, hy - 10 * U, 300 * U, EMBER, 0.2 * I);
  pool(G.anvil.x - 20 * U, G.anvil.y + 60 * U, Math.min(W * 0.42, 520 * U), 70 * U, EMBER, 0.2 * I);
  const g = cx.createLinearGradient(0, hy - 36 * U, 0, hy);
  g.addColorStop(0, heat(0.2, 0)); g.addColorStop(0.6, heat(0.5 + 0.2 * I, 0.6 * I)); g.addColorStop(1, heat(0.85, 0.95 * I));
  cx.fillStyle = g; cx.fillRect(hx - 78 * U, hy - 36 * U, 156 * U, 32 * U);
  if (!still) {
    for (let k = 0; k < 7; k++) {
      const fx = hx + (k - 3) * 19 * U, fh = (16 + 18 * flick(t * 1.2 + k * 0.7, k * 1.9)) * U * ig;
      const fg = cx.createLinearGradient(0, hy - 4 * U, 0, hy - 4 * U - fh);
      fg.addColorStop(0, heat(0.9, 0.75)); fg.addColorStop(0.6, heat(0.6, 0.35)); fg.addColorStop(1, heat(0.4, 0));
      cx.fillStyle = fg;
      cx.beginPath(); cx.moveTo(fx - 9 * U, hy - 4 * U);
      cx.quadraticCurveTo(fx - 6 * U, hy - 4 * U - fh * 0.6, fx + Math.sin(t * 4 + k) * 4 * U, hy - 4 * U - fh);
      cx.quadraticCurveTo(fx + 6 * U, hy - 4 * U - fh * 0.6, fx + 9 * U, hy - 4 * U); cx.fill();
    }
  }
  cx.fillStyle = heat(0.78 + 0.18 * f, 0.9 * ig);
  cx.fillRect(hx - 76 * U, hy - 6 * U, 152 * U, 3 * U);
  glow(hx, hy - 6 * U, 90 * U, GOLD, 0.3 * I);
  cx.globalCompositeOperation = "source-over";
}

function drawSmoke(still) {
  for (const p of smoke) {
    const u = 1 - p.life / p.max, al = Math.sin(Math.PI * u) * 0.09;
    const g = cx.createRadialGradient(p.x, p.y, 0, p.x, p.y, p.r);
    g.addColorStop(0, rgba([140, 138, 168], al)); g.addColorStop(1, rgba([140, 138, 168], 0));
    cx.fillStyle = g; cx.beginPath(); cx.arc(p.x, p.y, p.r, 0, TAU); cx.fill();
  }
}

function step(t, dt, ig) {
  // nodes: fade, glide, forget
  let live = 0;
  for (const [k, n] of nodes) {
    if (n.transient && n.until <= t && n.ta > 0) n.ta = 0;
    if (t >= n.born) n.a += (n.ta - n.a) * (1 - Math.exp(-dt * 3.2));
    n.x += (n.tx - n.x) * (1 - Math.exp(-dt * 2.6));
    n.y += (n.ty - n.y) * (1 - Math.exp(-dt * 2.6));
    n.pulse *= Math.exp(-dt * 2.4);
    if (n.ta === 0 && n.a < 0.02 && n.place !== "anvil") nodes.delete(k);
    else if (n.ta > 0) live++;
  }
  if (live !== cnt.nodes) { cnt.nodes = live; layout(); }
  spawnDue(t);
  // embers from the hearth and the furnaces, smoke from the flue and the chimneys
  acc.hearth += dt * 15 * ig;
  while (acc.hearth >= 1 && embers.length < 320) { acc.hearth--; emit(G.hearth.x + (R() - 0.5) * 120 * U, G.hearth.y - 6 * U, 1, 0.8 * ig); }
  acc.smoke += dt * 2.2;
  while (acc.smoke >= 1) {
    acc.smoke--;
    if (smoke.length < 120) smoke.push({x: G.flue.x + (R() - 0.5) * 8 * U, y: G.flue.y - 4 * U, vx: (6 + R() * 8) * U, vy: -(14 + R() * 10) * U, r: 8 * U, life: 6, max: 6});
    for (const n of nodes.values()) if (n.place === "job" && n.a > 0.3 && smoke.length < 120 && R() < 0.6) {
      smoke.push({x: n.x + 14 * n.s * U, y: n.y - 104 * n.s * U, vx: (5 + R() * 7) * U, vy: -(12 + R() * 9) * U, r: 5 * U * n.s, life: 5, max: 5});
      if (embers.length < 320) emit(n.x + (R() - 0.5) * 16 * n.s * U, n.y - 18 * n.s * U, 0.6, 0.7);
    }
  }
  for (let i = embers.length - 1; i >= 0; i--) {
    const e = embers[i];
    e.vx += Math.sin((t + e.seed) * 2.6) * 16 * U * dt; e.vy -= 6 * U * dt;
    e.x += e.vx * dt; e.y += e.vy * dt; e.life -= dt;
    if (e.life <= 0 || e.y < -10) embers.splice(i, 1);
  }
  for (let i = streaks.length - 1; i >= 0; i--) {
    const p = streaks[i];
    p.vy += 900 * U * dt; p.x += p.vx * dt; p.y += p.vy * dt; p.life -= dt;
    if (p.life <= 0) streaks.splice(i, 1);
  }
  for (let i = smoke.length - 1; i >= 0; i--) {
    const p = smoke[i];
    p.x += p.vx * dt; p.y += p.vy * dt; p.r += 7 * U * dt; p.life -= dt;
    if (p.life <= 0) smoke.splice(i, 1);
  }
  for (let i = flares.length - 1; i >= 0; i--) if (t - flares[i].t0 > 0.75) flares.splice(i, 1);
}

function drawSparks(t) {
  cx.globalCompositeOperation = "lighter";
  for (let i = sparks.length - 1; i >= 0; i--) {
    const k = sparks[i], u = (t - k.t0) / k.dur;
    if (u >= 1) {
      const B = anchor(k.b, t);
      flares.push({x: B[0], y: B[1], c: k.c, t0: t});
      for (let j = 0; j < 7 && embers.length < 320; j++) {
        const an = R() * TAU, v = (40 + R() * 70) * U;
        embers.push({x: B[0], y: B[1], vx: Math.cos(an) * v, vy: Math.sin(an) * v, life: 0.5 + R() * 0.5, max: 1, r: 1.1 * U, hot: 1, seed: R() * 9, c: k.c});
      }
      k.b.pulse = 1;
      cnt.flown++;
      sparks.splice(i, 1);
      continue;
    }
    if (k.a.ta > 0 || k.a.a > 0.05) k.A = anchor(k.a, t);
    if (k.b.ta > 0 || k.b.a > 0.05) k.B = anchor(k.b, t);
    const A = k.A, B = k.B, mx = (A[0] + B[0]) / 2, my = (A[1] + B[1]) / 2, dx = B[0] - A[0], dy = B[1] - A[1];
    const d = Math.hypot(dx, dy) || 1;
    let nx = -dy / d, ny = dx / d;
    if (ny > 0) { nx = -nx; ny = -ny; }
    const C = [mx + nx * d * k.lift, my + ny * d * k.lift - d * 0.08];
    const at = e => { const m = 1 - e; return [m * m * A[0] + 2 * m * e * C[0] + e * e * B[0], m * m * A[1] + 2 * m * e * C[1] + e * e * B[1]]; };
    const ease = v => { v = clamp(v, 0, 1); return v < 0.5 ? 2 * v * v : 1 - (-2 * v + 2) ** 2 / 2; };
    // the trail: a continuous streak that tapers and cools toward its tail (butt caps, so joints don't bead)
    const TR = 22;
    cx.lineCap = "butt";
    let q = at(ease(u - TR * 0.012));
    for (let j = TR - 1; j >= 0; j--) {
      const p = at(ease(u - j * 0.012)), f = 1 - j / TR;
      cx.strokeStyle = rgba(k.c, 0.8 * f * f); cx.lineWidth = (0.4 + 3 * f) * U;
      cx.beginPath(); cx.moveTo(q[0], q[1]); cx.lineTo(p[0], p[1]); cx.stroke();
      q = p;
    }
    const p = at(ease(u));
    glow(p[0], p[1], 16 * U, k.c, 0.55);
    glow(p[0], p[1], 3.2 * U, [255, 255, 255], 0.95);
  }
  for (const f of flares) {
    const u = (t - f.t0) / 0.75, e = 1 - (1 - u) ** 3;
    cx.strokeStyle = rgba(f.c, 0.85 * (1 - u)); cx.lineWidth = 1.6 * U;
    cx.beginPath(); cx.arc(f.x, f.y, (3 + 24 * e) * U, 0, TAU); cx.stroke();
    glow(f.x, f.y, (34 - 10 * u) * U, f.c, 0.55 * (1 - u));
  }
  cx.globalCompositeOperation = "source-over";
}

function drawParticles() {
  cx.globalCompositeOperation = "lighter";
  for (const e of embers) {
    const u = clamp(e.life / e.max, 0, 1);
    cx.fillStyle = e.c ? rgba(e.c, u * 1.2) : heat(0.25 + 0.7 * u * e.hot, Math.min(1, u * 1.5));
    cx.beginPath(); cx.arc(e.x, e.y, e.r * (0.5 + 0.5 * u), 0, TAU); cx.fill();
  }
  cx.lineCap = "round";
  for (const p of streaks) {
    const u = clamp(p.life / p.max, 0, 1);
    cx.strokeStyle = heat(0.55 + 0.45 * u, Math.min(1, u * 1.6)); cx.lineWidth = p.w * U;
    cx.beginPath(); cx.moveTo(p.x, p.y); cx.lineTo(p.x - p.vx * 0.024, p.y - p.vy * 0.024); cx.stroke();
  }
  cx.globalCompositeOperation = "source-over";
}

function draw(t, dt, still) {
  let e = 1, z = 1;
  if (!still && intro) {
    const u = clamp((t - introAt) / 2.6, 0, 1);
    e = 1 - (1 - u) ** 3; z = 1 + 0.05 * (1 - e);
    if (u >= 1) intro = false;
  }
  const ig = still || !intro ? 1 : clamp((t - introAt - 0.3) / 1.2, 0, 1) * (1 + 0.5 * Math.sin(Math.PI * clamp((t - introAt - 0.3) / 1.2, 0, 1)));
  const ox = G.anvil.x * (1 - z), oy = G.anvil.y * (1 - z);
  cx.setTransform(1, 0, 0, 1, 0, 0);
  cx.globalAlpha = 1; cx.globalCompositeOperation = "source-over";
  cx.setTransform(z, 0, 0, z, ox * dpr, oy * dpr);
  cx.drawImage(bg, 0, 0);
  cx.setTransform(dpr * z, 0, 0, dpr * z, ox * dpr, oy * dpr);
  // the sky: twinkles, the odd shooting star, mist on the ridges
  cx.globalCompositeOperation = "lighter";
  for (const s of stars) {
    const tw = still ? 0.5 : 0.5 + 0.5 * Math.sin(t * s.f + s.p);
    cx.fillStyle = rgba([255, 244, 226], 0.55 * tw);
    cx.beginPath(); cx.arc(s.x, s.y, s.s * 0.8, 0, TAU); cx.fill();
    if (s.s > 1.2) glow(s.x, s.y, 5 * s.s, [200, 215, 255], 0.25 * tw);
  }
  if (!still) {
    if (!shoot && t > nextShoot) {
      const dir = R() < 0.5 ? -1 : 1;
      shoot = {x: lerp(0.2, 0.8, R()) * W, y: lerp(0.03, 0.16, R()) * H, vx: dir * (380 + R() * 240) * U, vy: (80 + R() * 70) * U, t0: t};
      nextShoot = t + 18 + R() * 22;
    }
    if (shoot) {
      const u = (t - shoot.t0) / 0.9;
      if (u >= 1) shoot = null;
      else {
        const x = shoot.x + shoot.vx * (t - shoot.t0), y = shoot.y + shoot.vy * (t - shoot.t0);
        const g = cx.createLinearGradient(x - shoot.vx * 0.2, y - shoot.vy * 0.2, x, y);
        g.addColorStop(0, "rgba(234,242,255,0)"); g.addColorStop(1, rgba([234, 242, 255], 0.9 * Math.sin(Math.PI * u)));
        cx.strokeStyle = g; cx.lineWidth = 1.3 * U;
        cx.beginPath(); cx.moveTo(x - shoot.vx * 0.2, y - shoot.vy * 0.2); cx.lineTo(x, y); cx.stroke();
      }
    }
  }
  cx.globalCompositeOperation = "source-over";
  for (let i = 0; i < 3; i++) {
    const my = (G.ridges[i][0] - 0.02) * H, mx = ((t * (4 + i * 3) * U + i * W * 0.37) % (W * 1.6)) - W * 0.3;
    cx.save(); cx.translate(still ? W * (0.2 + i * 0.3) : mx, my); cx.scale(1, 0.12);
    const g = cx.createRadialGradient(0, 0, 0, 0, 0, W * 0.3);
    g.addColorStop(0, "rgba(120,130,190,.07)"); g.addColorStop(1, "rgba(120,130,190,0)");
    cx.fillStyle = g; cx.beginPath(); cx.arc(0, 0, W * 0.3, 0, TAU); cx.fill();
    cx.restore();
  }
  // far lights: Jarvis on the hill, Jeff's towers, the road lamps
  hillHead = null;
  for (const n of nodes.values()) if (n.place === "hill" && n.a > 0.01 && (!hillHead || n.ty < hillHead.ty)) hillHead = n;
  for (const n of nodes.values()) if ((n.place === "hill" || n.place === "jeff") && n.a > 0.01) drawTowerLight(n, t, still);
  label(G.jeff.x, G.jeff.base + 22 * U, "Jeff", 11 * U, rgba(BRASS, 0.9), 3, 1, "display");
  cx.globalCompositeOperation = "lighter";
  for (const L of G.lamps) {
    const f = still ? 0.9 : 0.85 + 0.15 * flick(t * 0.5, L.seed);
    glow(L.x, L.y, 22 * U * L.s + 4, [255, 190, 110], 0.38 * f);
    glow(L.x, L.y, 2.6 * U * L.s + 1, PALE, 0.9 * f);
  }
  cx.globalCompositeOperation = "source-over";
  // the sky lanterns
  for (const n of nodes.values()) if (n.place === "sky" && n.a > 0.01) drawSky(n, t, still);
  drawSmoke(still);
  // the forge
  drawHearth(t, still, ig);
  drawAnvil(t, dt, still, ig);
  for (const n of nodes.values()) if (n.place === "hang" && n.a > 0.01) drawHang(n, t, still);
  const front = [...nodes.values()].filter(n => (n.place === "lane" || n.place === "job") && n.a > 0.01).sort((p, q) => p.y - q.y);
  for (const n of front) n.place === "lane" ? drawBench(n, t, still) : drawFurnace(n, t, still, ig);
  if (!still) { drawParticles(); drawSparks(t); }
  // what changed, when nothing may move
  for (const n of nodes.values()) if (n.litUntil > t && n.a > 0.01) {
    const [x, y] = anchor(n, t);
    cx.globalCompositeOperation = "lighter";
    glow(x, y, 34 * U, BRASS, 0.42);
    cx.globalCompositeOperation = "source-over";
    cx.strokeStyle = rgba(BRASS, 0.95); cx.lineWidth = 1.8 * U;
    cx.beginPath(); cx.arc(x, y, 15 * U, 0, TAU); cx.stroke();
  }
  // vignette, labels, the arrival
  cx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const v = cx.createRadialGradient(W / 2, H * 0.55, Math.min(W, H) * 0.35, W / 2, H * 0.55, Math.max(W, H) * 0.75);
  v.addColorStop(0, "rgba(2,3,10,0)"); v.addColorStop(1, "rgba(2,3,10,.55)");
  cx.fillStyle = v; cx.fillRect(0, 0, W, H);
  cx.setTransform(dpr * z, 0, 0, dpr * z, ox * dpr, oy * dpr);
  drawLabels();
  if (e < 1) {
    cx.setTransform(1, 0, 0, 1, 0, 0);
    cx.fillStyle = rgba([2, 3, 10], 0.94 * (1 - e)); cx.fillRect(0, 0, cv.width, cv.height);
  }
}

function frame() {
  raf = requestAnimationFrame(frame);
  const t = now(), dt = lastT ? Math.min(0.05, t - lastT) : 0.016;
  lastT = t;
  if (view === "net") {  // the forge canvas is hidden: only the network draws (the ledger still scrolls)
    pending.length = 0;
    if (NW) {  // draw while something moves; running jobs breathe at 10 fps; otherwise the last picture stands
      const mv = netStep(t, dt);
      if (mv || ndirty || (t - nlast > 0.1 && nedges.some(e => e.dim))) { netDraw(t, false); nlast = t; ndirty = false; }
    }
    tickerStep(dt);
    netPublish();
    return;
  }
  if (!G) return;
  const ig = intro ? clamp((t - introAt - 0.3) / 1.2, 0, 1) : 1;
  step(t, dt, ig);
  draw(t, dt, false);
  tickerStep(dt);
  publish();
}
function start() {
  if (raf || still() || document.hidden || !cx || view === "board") return;  // the Board runs its own loop
  lastT = 0;
  raf = requestAnimationFrame(frame);
}
function stop() { if (raf) cancelAnimationFrame(raf); raf = 0; }
function paintStill() {
  if (view === "board") return;
  if (view === "net") { netPaint(); return; }
  if (!cx || !G) return;
  const t = now();
  for (const [k, n] of nodes) {
    if (n.ta === 0 && n.place !== "anvil") { nodes.delete(k); continue; }
    n.a = n.ta; n.x = n.tx; n.y = n.ty;
  }
  draw(t, 0, true);
  publish();
  clearTimeout(litTimer);
  let next = 0;
  for (const n of nodes.values()) if (n.litUntil > t) next = Math.max(next, n.litUntil);
  if (next) litTimer = setTimeout(paintStill, (next - t) * 1000 + 50);
}

function resize() {
  if (!cx) return;
  const r = cv.getBoundingClientRect();
  if (r.width < 2 || r.height < 2) return;
  W = Math.round(r.width); H = Math.round(r.height);
  dpr = Math.max(1, Math.min(window.devicePixelRatio || 1, 2, Math.sqrt(2.6e6 / (W * H))));
  cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr);
  G = geometry();
  backdrop();
  layout();
  for (const n of nodes.values()) { n.x = n.tx; n.y = n.ty; }
  if (still()) paintStill();
}

// ---------- the panels ----------
let ticker = [], tkOff = 0, tkPause = false;
const tkList = $("tk-list"), tkBand = $("tk-band");

function kindle(el) { el.classList.add("is-new"); el._k = now(); }
function sync(list, items, key, build, first) {
  if (!list) return;
  const old = new Map(), used = new Set(), t = now();
  for (const el of Array.from(list.children)) old.set(el.dataset.k, el);
  let i = 0;
  for (const it of items) {
    let k = key(it);
    while (used.has(k)) k += "+";
    used.add(k);
    let el = old.get(k);
    if (el) old.delete(k);
    else { el = document.createElement(list.tagName === "TBODY" ? "tr" : "li"); el.dataset.k = k; if (!first) kindle(el); }
    build(el, it);
    const at = list.children[i] || null;
    if (at !== el) list.insertBefore(el, at);
    i++;
  }
  for (const el of old.values()) el.remove();
  for (const el of list.querySelectorAll(".is-new")) if (t - (el._k || 0) > 3) el.classList.remove("is-new");
}

function evItem(el, e) {
  const kind = str(e.kind), k = h("span", "tk-k", kind.replace(/_/g, " "));
  k.style.setProperty("--c", kv(evColour(kind.toLowerCase())));
  el.replaceChildren(h("span", "tk-t", clock(e.at, false)), k, h("span", null, evText(e)));
}
function renderTicker(evs, first) {
  ticker = evs;
  const key = e => str(e.at) + "|" + str(e.kind) + "|" + str(e.text).slice(0, 120);
  if (still()) {
    sync(tkList, evs, key, evItem, first);
    tkList.style.transform = ""; tkOff = 0;
  } else {
    // chronological, left to right; new events join at the end, the marquee keeps its place
    const want = new Map(), used = new Set();
    for (const e of evs.slice().reverse()) { let k = key(e); while (used.has(k)) k += "+"; used.add(k); want.set(k, e); }
    for (const el of Array.from(tkList.children)) { if (want.has(el.dataset.k)) { evItem(el, want.get(el.dataset.k)); want.delete(el.dataset.k); } else el.remove(); }
    for (const [k, e] of want) { const el = h("li"); el.dataset.k = k; evItem(el, e); if (!first) kindle(el); tkList.appendChild(el); }
    const t = now();
    for (const el of tkList.querySelectorAll(".is-new")) if (t - (el._k || 0) > 3) el.classList.remove("is-new");
  }
  $("tk-empty").hidden = evs.length > 0;
}
function tickerStep(dt) {
  if (tkPause || !tkList.firstElementChild) return;
  const bw = tkBand.clientWidth, lw = tkList.scrollWidth;
  if (lw <= bw) { if (tkOff) { tkOff = 0; tkList.style.transform = ""; } return; }
  tkOff += 36 * dt;
  const f = tkList.firstElementChild, fw = f.offsetWidth;
  if (tkOff > fw + 14) { tkList.appendChild(f); tkOff -= fw; }
  tkList.style.transform = "translateX(" + (-tkOff).toFixed(1) + "px)";
}
if (tkBand) {
  tkBand.addEventListener("mouseenter", () => { tkPause = true; });
  tkBand.addEventListener("mouseleave", () => { tkPause = false; });
  tkBand.addEventListener("focusin", () => { tkPause = true; });
  tkBand.addEventListener("focusout", () => { tkPause = false; });
}

function renderFocus(s, first) {
  const f = s.focus && typeof s.focus === "object" ? s.focus : null, id = f ? str(f.id) : "";
  const was = $("focus-id").textContent;
  setText($("focus-id"), id || "—");
  setText($("focus-stage"), f ? str(f.stage) || "working" : "idle");
  setText($("focus-step"), f && f.step ? "step " + str(f.step) : "");
  setText($("focus-title"), f ? str(f.title) || "Untitled task" : modern ? "No task in focus" : "No task at the anvil");
  const m = /^(\d+)\s*\/\s*(\d+)$/.exec(f ? str(f.step).trim() : ""), pips = $("focus-pips");
  const total = m ? Math.min(12, +m[2]) : 0, on = m ? Math.min(total, +m[1]) : 0;
  if (pips.children.length !== total || pips.querySelectorAll(".on").length !== on) {
    pips.replaceChildren(...Array.from({length: total}, (_, i) => h("li", i < on ? "on" : null)));
  }
  if (!first && was !== (id || "—")) kindle($("focus"));
}

function renderGpu(s) {
  const g = s.gpu && typeof s.gpu === "object" ? s.gpu : null;
  const total = g ? num(g.total_mb) : null, used = g ? num(g.used_mb) : null, util = g ? num(g.util_pct) : null;
  setText($("gpu-name"), g ? str(g.name) || "GPU" : "No GPU reading");
  const mods = arr(s.models).map(obj).filter(m => m.loaded === true && m.where === "gpu" && (num(m.vram_mb) || 0) > 0).slice(0, 12);
  const cap = total || Math.max(used || 0, mods.reduce((a, m) => a + num(m.vram_mb), 0)) || 1;
  let sum = 0;
  const segs = [], rows = [];
  mods.forEach((m, i) => {
    const mb = num(m.vram_mb), c = str(m.family) === "embed" ? kv("#45E0A0") : str(m.family) === "jeff" ? kv("#BC8BFF") : modern ? VRAMK[i % VRAMK.length] : VRAMC[i % VRAMC.length];
    sum += mb;
    const seg = h("div", "seg"); seg.style.setProperty("--h", String(clamp(mb / cap, 0, 1))); seg.style.setProperty("--c", c);
    segs.push(seg);
    const li = h("li"); li.dataset.model = str(m.name); li.style.setProperty("--c", c);
    li.append(h("span", "vn", str(m.name)), h("span", "vg", gb(mb)));
    rows.push(li);
  });
  const other = used != null ? Math.max(0, used - sum) : 0;
  if (other > 64) {
    const seg = h("div", "seg seg-other"); seg.style.setProperty("--h", String(clamp(other / cap, 0, 1))); segs.push(seg);
    const li = h("li"); li.style.setProperty("--c", modern ? "var(--muted)" : "rgba(144,151,188,.5)");
    li.append(h("span", "vn", "other processes"), h("span", "vg", gb(other))); rows.push(li);
  }
  if (total != null && used != null) {
    const li = h("li", "v-free"); li.style.setProperty("--c", "transparent");
    li.append(h("span", "vn", "free"), h("span", "vg", gb(Math.max(0, total - used)))); rows.push(li);
  }
  if (modern && window.Charts) Charts.stack($("ch-gpu"), {label: "GPU memory", total: cap, fmt: gb, empty: g ? "No models on the GPU" : "No GPU reading",
    segs: mods.map((m, i) => ({name: str(m.name), v: num(m.vram_mb), c: rows[i].style.getPropertyValue("--c")})).concat(other > 64 ? [{name: "other processes", v: other, c: "var(--muted)"}] : [])});
  $("tank-fill").replaceChildren(...segs);
  $("gpu-list").replaceChildren(...rows);
  const read = $("gpu-read");
  if (g && used != null) {
    const bar = h("span", "util"); bar.style.setProperty("--f", clamp(util || 0, 0, 100) + "%");
    read.replaceChildren(h("b", null, gb(used)), document.createTextNode(" of " + (total != null ? gb(total) : "?") +
      (util != null ? " · " + Math.round(util) + "% busy" : "")), bar);
  } else read.replaceChildren(document.createTextNode(g ? "no memory reading" : "—"));
}

function renderJobs(s, first) {
  const jobs = arr(s.jobs).map(obj).slice(0, 20);
  sync($("jobs-list"), jobs, j => num(j.pid) != null ? "pid" + j.pid : str(j.name), (el, j) => {
    const kind = JOBC[str(j.kind)] ? str(j.kind) : "other", p = obj(j.progress), done = num(p.done), tot = num(p.total);
    const frac = done != null && tot ? clamp(done / tot, 0, 1) : null;
    el.className = "job"; el.style.setProperty("--c", kv(JOBC[kind]));
    const bits = [str(j.model), j.since ? "for " + since(j.since, s.at) : ""].filter(Boolean);
    let bar = h("div", frac != null ? "jbar" : "jbar heat");
    if (window.Charts) bar = Charts.progress({done, total: tot, unit: str(p.unit), rate: num(p.rate), eta: num(p.eta_s != null ? p.eta_s : p.eta), note: str(p.note), label: "Progress of " + str(j.name)});
    else { const fill = h("span", "jfill"); if (frac != null) fill.style.setProperty("--p", frac.toFixed(4)); bar.append(fill); }
    const parts = [h("span", "jd"), h("span", "jn", str(j.name)), h("span", "jk", frac != null ? Math.round(frac * 100) + "%" : kind),
      h("p", "jm", bits.join(" · ") || "pid " + str(j.pid))];
    if (str(j.line).trim()) parts.push(h("p", "jl", str(j.line)));
    parts.push(bar);
    el.replaceChildren(...parts);
  }, first);
  $("jobs-empty").hidden = jobs.length > 0;
  setText($("jobs-sub"), jobs.length ? jobs.length + (modern ? " running" : " burning") : modern ? "idle" : "banked");
}

function stepsOf(o) {  // {steps: {done, total}} or "3/5"; null when the snapshot has none
  o = obj(o);
  const a = obj(o.steps), m = /^(\d+)\s*\/\s*(\d+)$/.exec(str(o.step));
  const done = num(a.done) != null ? num(a.done) : m ? +m[1] : null, total = num(a.total) != null ? num(a.total) : m ? +m[2] : null;
  return done != null && total ? {done, total} : null;
}

function renderQueue(s, first) {
  const q = arr(s.queue).map(obj).slice(0, 12), lanes = new Map(arr(s.lanes).map(obj).map(l => [str(l.task), l]));
  sync($("queue-list"), q, t => str(t.id), (el, t) => {
    el.className = "q"; el.dataset.status = str(t.status);
    const ty = str(t.type).toUpperCase(), chip = h("span", "qt", ty.toLowerCase() || "task");
    chip.style.setProperty("--c", kv(TYPEC[ty] || "#BAC0DD"));
    const kids = [h("span", "qs"), h("span", "qid", str(t.id)), chip, h("span", "qtier", str(t.tier)), h("p", "qn", str(t.title))];
    const sp = window.Charts ? stepsOf(t) || stepsOf(lanes.get(str(t.id))) : null;  // a task's steps, or its builder lane's
    if (sp) kids.push(Charts.progress({done: sp.done, total: sp.total, unit: "steps", note: str(t.stage) || str(obj(lanes.get(str(t.id))).stage),
      label: "Steps of " + str(t.id)}));  // T-3044: and its stage
    el.replaceChildren(...kids);
  }, first);
  $("queue-empty").hidden = q.length > 0;
  setText($("queue-sub"), q.length ? q.length + " waiting · " + q.filter(t => t.status === "active").length + " active" : "clear");
}

function renderCloses(s, first) {
  const c = arr(s.closed_today).map(obj).slice(0, 40);
  sync($("closes-list"), c, t => str(t.id) + "|" + str(t.at), (el, t) => {
    el.className = "done";
    el.replaceChildren(h("span", "dt", clock(t.at, false)), h("span", "did", str(t.id)), h("span", "dn", str(t.title)));
  }, first);
  $("closes-empty").hidden = c.length > 0;
  setText($("closes-sub"), c.length ? c.length + (modern ? " finished" : " struck and quenched") : modern ? "finished" : "struck and quenched");
}

function renderBench(s, first) {
  const rows = arr(obj(s.stats).bench).map(obj).slice(0, 30);
  sync($("bench-rows"), rows, r => str(r.use) + "|" + str(r.contestant), (el, r) => {
    const use = str(r.use), acc = num(r.accuracy), cov = num(r.coverage), pct = v => v == null ? "—" : Math.round(v <= 1 ? v * 100 : v) + "%";
    const u = h("td", "bu", use); u.style.setProperty("--c", kv(USEC[use] || "#BAC0DD"));
    const a = h("td", null, pct(acc)), bar = h("span", "acc");
    bar.style.setProperty("--f", acc == null ? "0%" : clamp(acc <= 1 ? acc * 100 : acc, 0, 100) + "%");
    a.append(bar);
    el.replaceChildren(u, h("td", null, str(r.contestant)), a, h("td", null, pct(cov)));
  }, first);
  $("bench-table").hidden = rows.length === 0;
  $("bench-empty").hidden = rows.length > 0;
}

function renderCounters(s, first) {
  const st = obj(s.stats), cards = obj(st.cards), moons = obj(st.moons), models = arr(s.models).map(obj);
  const bump = (id, v) => { const el = $(id); if (el.textContent !== v) { el.textContent = v; if (!first) kindle(el.parentElement); } };
  bump("c-general", fmt(num(cards.general)));
  bump("c-ui", fmt(num(cards.ui)));
  const cells = num(cards.ui_cells), grid = num(cards.grid_ui);
  setText($("c-cells"), cells != null && grid ? fmt(cells) + " of " + fmt(grid) + " cells" : cells != null ? fmt(cells) + " cells" : "no grid yet");
  $("c-cellbar").style.setProperty("--f", (cells != null && grid ? clamp(cells / grid * 100, 0, 100) : 0) + "%");
  bump("c-moons", fmt(num(moons.candidates)));
  const pace = obj(moons.pace), per = obj(pace.per_hour), fin = obj(pace.finish);
  const rate = num(per["1h"]) || num(per["6h"]) || num(per["24h"]) || 0;
  setText($("c-pace"), num(pace.kept) == null ? "kept per hour: —"
    : fmt(rate) + " kept/hour · " + fmt(num(pace.kept)) + " of " + fmt(num(pace.target) || 10000) + " kept"
      + (fin.from ? " · 10,000 by " + (fin.from === fin.to ? str(fin.from) : str(fin.from) + " to " + str(fin.to)) : ""));
  const stages = Object.entries(obj(moons.stages)).slice(0, 8).map(([k, v]) => {
    const o = obj(v), kept = num(o.kept) || 0, rej = num(o.rejected) || 0, wait = num(o.waiting) || 0;
    return {name: k, kept, rej, wait, total: kept + rej + wait};
  });
  const most = Math.max(1, num(moons.candidates) || 0, ...stages.map(x => x.total));
  sync($("c-stages"), stages, x => x.name, (el, x) => {
    const bar = h("span", "sb"), t = x.total || 1;
    bar.style.setProperty("--k", (x.kept / t * 100).toFixed(1) + "%");
    bar.style.setProperty("--w", ((x.kept + x.wait) / t * 100).toFixed(1) + "%");
    bar.style.setProperty("--s", Math.max(0.04, x.total / most).toFixed(3));
    el.title = x.name + ": " + fmt(x.kept) + " kept, " + fmt(x.rej) + " rejected, " + fmt(x.wait) + " waiting";
    el.replaceChildren(h("span", "sn", x.name), bar, h("b", null, fmt(x.kept)));
  }, first);
  const closed = num(st.closed_today) != null ? num(st.closed_today) : arr(s.closed_today).length;
  bump("c-closed", fmt(closed));
  const lastClose = obj(arr(s.closed_today)[0]);
  setText($("c-closed-sub"), lastClose.id ? "last " + str(lastClose.id) : "tasks");
  const calls = models.reduce((a, m) => a + (num(m.calls_5m) || 0), 0);
  let busy = null;
  for (const m of models) if ((num(m.calls_5m) || 0) > (busy ? num(busy.calls_5m) : 0)) busy = m;
  bump("c-calls", fmt(calls));
  setText($("c-calls-sub"), busy ? "busiest " + str(busy.name) : "across every model");
  const lanes = arr(s.lanes).filter(l => obj(l).waiting !== true).length, jobs = arr(s.jobs).length;  // T-3170: a returned or merged lane isn't at work
  const waiting = arr(s.lanes).length - lanes;
  bump("c-work", fmt(lanes + jobs));
  setText($("c-work-sub"), lanes + " at work · " + waiting + " waiting to merge");  // the big number adds the jobs: too long to name here
  $("c-work-sub").title = lanes + " builders + " + jobs + " jobs at work; " + waiting + " finished lanes wait on a merge";
}

// ---------- the charts (T-3092): drawn by charts.js from the snapshot and from what this page has seen since it opened ----------
const HIST_MAX = 150, SERIES_C = ["var(--k-blue)", "var(--k-violet)", "var(--k-green)", "var(--k-orange)", "var(--k-amber)"];
const hist = {slug: "", at: 0, calls: new Map(), spark: {}};
const push = (a, v) => { a.push(v); if (a.length > HIST_MAX) a.shift(); };
function sample(s) {  // at most one reading a second, and a different project starts a fresh history
  const at = Date.parse(str(s.at)), slug = str(obj(s.project).slug);
  if (!Number.isFinite(at) || (slug === hist.slug && at - hist.at < 1000)) return;
  if (slug !== hist.slug) { hist.calls.clear(); hist.spark = {}; hist.slug = slug; }
  hist.at = at;
  const models = arr(s.models).map(obj), seen = new Set(), st = obj(s.stats);
  for (const m of models) {
    const n = str(m.name);
    if (!n || (!hist.calls.has(n) && hist.calls.size >= 12)) continue;
    if (!hist.calls.has(n)) hist.calls.set(n, []);
    push(hist.calls.get(n), [at, (num(m.calls_5m) || 0) / 5]);  // calls_5m is a five-minute count: per minute
    seen.add(n);
  }
  for (const [n, a] of hist.calls) if (!seen.has(n)) push(a, [at, 0]);
  const sp = (k, v) => { if (v != null) push(hist.spark[k] || (hist.spark[k] = []), v); };
  sp("general", num(obj(st.cards).general)); sp("ui", num(obj(st.cards).ui));
  sp("closed", num(st.closed_today) != null ? num(st.closed_today) : arr(s.closed_today).length);
  sp("calls", models.reduce((a, m) => a + (num(m.calls_5m) || 0), 0));
  sp("work", arr(s.lanes).filter(l => obj(l).waiting !== true).length + arr(s.jobs).length);
}
function renderCharts(s) {
  if (!modern || !window.Charts) return;
  const hm = x => String(new Date(x).getHours()).padStart(2, "0") + ":" + String(new Date(x).getMinutes()).padStart(2, "0");
  const top = [...hist.calls].map(([n, a]) => ({n, a, now: a.length ? a[a.length - 1][1] : 0, peak: Math.max(0, ...a.map(p => p[1]))}))
    .sort((x, y) => y.now - x.now || y.peak - x.peak).filter(x => x.peak > 0).slice(0, 5);
  Charts.line($("ch-calls"), {label: "Calls per minute by model", area: true, xfmt: hm, yfmt: v => String(Math.round(v * 10) / 10), empty: "No calls yet",
    series: top.map((x, i) => ({name: x.n, c: SERIES_C[i % SERIES_C.length], pts: x.a}))});
  const hrs = arr(obj(s.stats).spend).map(num).filter(v => v != null), usd = v => "$" + v.toFixed(2);
  let run = 0;  Charts.line($("ch-spend"), {label: "Spend today, running total", area: true, xfmt: v => String(Math.round(v)).padStart(2, "0") + ":00", yfmt: usd, empty: "No spend reported",
    series: hrs.length ? [{name: "spend", c: "var(--k-green)", pts: hrs.map((v, i) => [i, (run += v)])}] : []});
  setText($("ch-spend-sub"), hrs.length ? usd(run) + " so far today" : "dollars so far today");
  const stages = Object.entries(obj(obj(obj(s.stats).moons).stages)).slice(0, 8).map(([k, v]) => ({name: k, kept: num(obj(v).kept) || 0, rej: num(obj(v).rejected) || 0, wait: num(obj(v).waiting) || 0}));
  Charts.funnel($("ch-funnel"), {label: "Moons funnel", stages, fmt: fmt, empty: "No funnel yet"});
  const names = {general: "Cards", ui: "UI cards", closed: "Closed today", calls: "Calls in five minutes", work: "At work"};
  for (const k in names) Charts.spark($("sp-" + k), hist.spark[k], {label: names[k], c: "var(--k-blue)"});
}
let chartW = 0;
window.addEventListener("resize", () => { if (snap && window.innerWidth !== chartW) { chartW = window.innerWidth; renderCharts(snap); } });

function renderHead(s) {
  const p = obj(s.project), where = $("project");
  where.replaceChildren(h("b", null, str(p.slug) || "no project"),
    document.createTextNode(" · drive " + (p.drive ? "on" : "off") + (p.autonomy ? " · " + str(p.autonomy) + " autonomy" : "")));
  setText($("clock"), clock(s.at, true));
  age();
}
function age() {
  if (!snap) return;
  const at = Date.parse(str(snap.at));
  setText($("clock-age"), Number.isFinite(at) ? (Date.now() - at < 5000 ? "· now" : "· " + span(Date.now() - at) + " old") : "");
}

let snap = null, snapAt = -Infinity, firstRender = true;
function take(s) {
  if (!s || typeof s !== "object" || Array.isArray(s)) return;
  const at = Date.parse(str(s.at));
  if (Number.isFinite(at) && at < snapAt) return;  // an older snapshot that arrived late
  snap = s;
  if (!currentProject && SLUG.test(str(obj(s.project).slug))) currentProject = str(s.project.slug);  // pin it (not in the hash): a reconnect must not switch projects
  if (Number.isFinite(at)) snapAt = at;
  const first = firstRender;
  firstRender = false;
  renderHead(s);
  renderFocus(s, first);
  renderGpu(s);
  renderJobs(s, first);
  renderQueue(s, first);
  renderCloses(s, first);
  renderBench(s, first);
  renderCounters(s, first);
  sample(s);
  renderCharts(s);
  renderTicker(arr(s.events).map(obj).slice(0, 60), first);
  sceneTake(s, first);
  netIngest(s);
  if (board) { board.update(window.SampoBoard.boardModel(s, boardPrev ? [boardPrev] : [])); boardPrev = s; }
}

// ---------- the machine: sessions, projects, and the pick (kept in #project=SLUG) ----------
const SLUG = /^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$/;
function hashParts() {  // #project=SLUG&view=net: the keys in any order
  const o = {};
  for (const kv of location.hash.replace(/^#/, "").split("&")) {
    const i = kv.indexOf("=");
    if (i > 0) { try { o[kv.slice(0, i)] = decodeURIComponent(kv.slice(i + 1)); } catch (err) { /* a bad escape is no value */ } }
  }
  return o;
}
function fromHash() {
  const v = hashParts().project || "";
  return SLUG.test(v) ? v : "";
}
const hashFor = (p, v) => [p ? "project=" + encodeURIComponent(p) : "", v === "net" ? "view=net" : ""].filter(Boolean).join("&");
const hashView = () => hashParts().view === "net" ? "net" : modern ? "board" : "forge";  // modern: Board|Network; the forge: Forge|Network
let hashProject = fromHash();  // the project the hash named last: a view change leaves it alone, so the stream stays
let currentProject = hashProject, loadId = 0, machine = null, machineGone = false, machineTimer = 0;
const qs = () => currentProject ? "?project=" + encodeURIComponent(currentProject) : "";
const rail = $("rail"), topRow = rail ? rail.parentElement : null;
const picked = () => currentProject || str(obj(snap && snap.project).slug);

function seenAgo(v) {
  const t = typeof v === "number" ? (v < 1e12 ? v * 1000 : v) : Date.parse(str(v));
  return Number.isFinite(t) ? span(Date.now() - t) + " ago" : "—";
}
function markCurrent() {
  if (!rail) return;
  for (const b of rail.querySelectorAll("button[data-project]")) {
    if (b.dataset.project === picked()) b.setAttribute("aria-current", "true"); else b.removeAttribute("aria-current");
  }
}
function pickRow(el, slug, label, live, kids) {
  el.className = el.classList.contains("is-new") ? "is-new" : "";  // kindle() may have just lit it
  let b = el.firstElementChild;  // kept between polls, so a focused row stays focused
  if (!b || b.tagName !== "BUTTON" || b.dataset.project !== slug) {
    b = h("button", "pick"); b.type = "button"; b.dataset.project = slug;
    el.replaceChildren(b);
  }
  b.className = "pick" + (live ? " live" : "");
  b.setAttribute("aria-label", label + ". Show this project.");
  b.title = label;
  b.replaceChildren(...kids);
}
// ---------- steering: POST /api/act (T-3026) ----------
// JSON with the page's token in a header (the server injects it as <meta name="sampo-act">); every result goes to one status line.
const actMeta = document.querySelector('meta[name="sampo-act"]'), actStatus = $("act-status"), pauseBtn = $("act-pause");
function say(text, bad) { if (actStatus) { setText(actStatus, text); actStatus.classList.toggle("bad", !!bad); } }
let actSeq = 0;
// btn (the control pressed) stays disabled until the fetch settles; done(ok) runs then. Only the latest act writes the status line.
function act(body, btn, done) {
  const what = body.action.replace(/_/g, " ") + (body.project ? " · " + body.project : ""), mine = ++actSeq;
  const out = (text, bad) => { if (mine === actSeq) say(text, bad); };
  const end = ok => {
    if (btn) { const had = document.activeElement === document.body; btn.disabled = false; if (had && btn.isConnected) btn.focus(); }
    if (done) done(ok);
  };
  if (!actMeta) { out(what + ": this page has no steering token, reload the page", true); end(false); return; }
  if (btn) btn.disabled = true;
  out(what + "…");
  fetch("/api/act", {method: "POST", cache: "no-store", headers: {"Content-Type": "application/json", "X-Sampo-Nonce": actMeta.content},
    body: JSON.stringify(body)})
    .then(r => r.json().catch(() => null).then(j => ({r, j: obj(j)})))
    .then(({r, j}) => {
      const ok = r.ok && j.ok === true;
      if (ok) { out(what + ": ok"); if (/^(drive|resume)/.test(body.action)) pollMachine(); }
      else if (r.ok) out(what + ": failed (exit " + str(j.exit) + ")", true);
      else {
        const e = str(j.error) || "refused (" + r.status + ")";
        out(what + ": " + e + (r.status === 403 && !/reload/i.test(e) ? ". Reload the page" : ""), true);
      }
      end(ok);
    })
    .catch(() => { out(what + ": the server did not answer", true); end(false); });
}
function ensureActs(el, defs) {  // the buttons are made once per row and updated in place, so a focused one stays focused
  let box = el.querySelector(":scope > .acts");
  if (!box) { box = h("div", "acts"); box.setAttribute("role", "group"); el.appendChild(box); }
  defs.forEach((d, i) => {
    let b = box.children[i];
    if (!b || b.tagName !== "BUTTON") { b = h("button", "act"); b.type = "button"; box.insertBefore(b, box.children[i] || null); }
    b.dataset.act = d.act; b.dataset.target = d.target;
    setText(b, d.text);
    b.setAttribute("aria-label", d.label);
    if (d.pressed === undefined) b.removeAttribute("aria-pressed"); else b.setAttribute("aria-pressed", d.pressed ? "true" : "false");
  });
  while (box.children.length > defs.length && box.children[defs.length].tagName === "BUTTON") box.children[defs.length].remove();
  box.setAttribute("aria-label", "Controls for " + defs[0].who);
}
function noteField(btn) {  // Enter sends, Esc cancels
  const box = btn.parentElement, open = box.querySelector(".note-in");
  if (open) { open.focus(); return; }
  const f = h("input", "note-in"); f.type = "text"; f.maxLength = 2000; f.dataset.target = btn.dataset.target;
  f.setAttribute("aria-label", "Note to this session; Enter sends, Escape cancels");
  f.placeholder = "note, Enter sends";
  box.appendChild(f);
  f.focus();
}
let armTimer = 0, armAt = 0;
function disarm() {
  clearTimeout(armTimer);
  if (!pauseBtn) return;
  pauseBtn.classList.remove("armed"); setText(pauseBtn, "pause now");
}
function press(b) {
  const a = b.dataset.act, t = b.dataset.target;
  if (b.disabled) return;
  if (a === "note") noteField(b);
  else if (a === "pause_now") {  // asks in the page: a second press 0.6 to 4 s later sends it (a double-click or held key doesn't)
    if (!pauseBtn.classList.contains("armed")) {
      pauseBtn.classList.add("armed"); setText(pauseBtn, "press again to pause"); armAt = now();
      say("press again within 4 s to pause every session");
      armTimer = setTimeout(() => { disarm(); say("pause now cancelled"); }, 4000);
    } else if (now() - armAt >= 0.6) { disarm(); act({action: "pause_now", confirmed: true}, b); }
  } else if (a === "pause_off") act({action: a}, b);
  else if (t) act({action: a, project: t}, b);
}

function renderMachine(m) {
  if (!m || typeof m !== "object" || Array.isArray(m)) return;
  const first = machine === null;
  machine = m;
  const projects = arr(m.projects).map(obj).filter(p => SLUG.test(str(p.slug))).slice(0, 80);
  const names = new Map(projects.map(p => [str(p.slug), str(p.name) || str(p.slug)]));
  const sessions = arr(m.sessions).map(obj).filter(s => str(s.id)).slice(0, 40);
  sync($("sessions-list"), sessions, s => str(s.id), (el, s) => {
    const slug = str(s.project), name = names.get(slug) || slug || "unknown", task = str(s.task), model = str(s.model);
    const pct = num(s.context_pct), ago = seenAgo(s.seen), quiet = projects.some(p => str(p.slug) === slug && p.sensitive === true);
    const bar = h("span", "tankbar" + (pct != null && pct >= 80 ? " hot" : ""));
    bar.style.setProperty("--f", clamp(pct || 0, 0, 100) + "%");
    const line = [model, pct != null ? Math.round(pct) + "%" : "", ago].filter(Boolean).join(" · ");
    if (quiet) {  // a sensitive project shows its name only: task and model never reach the DOM
      el.className = "plate private";
      el.replaceChildren(h("span", "ember"), h("span", "rn", name), h("span", "rt", "private"));
      return;
    }
    const kids = [h("span", "ember"), h("span", "rn", name), h("span", "rt", task), h("p", "rm", line), bar];
    const label = "Live session in " + name + (task ? ", task " + task : "") + (model ? ", " + model : "") +
      (pct != null ? ", context " + Math.round(pct) + "%" : "") + ", seen " + ago;
    if (!SLUG.test(slug)) { el.className = "plate private"; el.replaceChildren(...kids); return; }
    pickRow(el, slug, label, true, kids);
    ensureActs(el, [{act: "note", target: str(s.id), text: "note", who: name + " session", label: "Send a note to the session in " + name}]);
  }, first);
  sync($("projects-list"), projects, p => str(p.slug), (el, p) => {
    const slug = str(p.slug), name = str(p.name) || slug;
    if (p.sensitive === true) {
      el.className = "plate private";
      el.replaceChildren(h("span", "ember"), h("span", "rn", name), h("span", "rt", "private"));
      return;
    }
    const q = num(p.queue), task = str(p.task), stage = str(p.stage);
    const line = [task ? task + (stage ? " · " + stage : "") : "idle", p.drive === undefined ? "" : "drive " + (p.drive ? "on" : "off")]
      .filter(Boolean).join(" · ");
    const kids = [h("span", "ember"), h("span", "rn", name), h("span", "rt", q != null ? q + " queued" : ""), h("p", "rm", line)];
    pickRow(el, slug, name + (task ? ", task " + task + (stage ? " " + stage : "") : ", no task") + (q != null ? ", " + q + " queued" : ""),
      false, kids);
    const on = p.drive === true, who = name;
    ensureActs(el, [  // no drive toggle when the row doesn't say (a fallback row)
      ...(typeof p.drive === "boolean" ? [{act: on ? "drive_off" : "drive_on", target: slug, text: "drive", pressed: on, who,
       label: "Drive is " + (on ? "on" : "off") + " for " + name + ". Press to turn it " + (on ? "off" : "on")}] : []),
      {act: "checkpoint", target: slug, text: "checkpoint", who, label: "Checkpoint " + name},
      {act: "resume", target: slug, text: "resume", who, label: "Resume " + name}]);
  }, first);
  $("sessions-empty").hidden = sessions.length > 0;
  $("projects-empty").hidden = projects.length > 0;
  setText($("sessions-n"), sessions.length ? String(sessions.length) : "");
  setText($("projects-n"), projects.length ? String(projects.length) : "");
  markCurrent();
  netBuild();
}
function pollMachine() {
  clearTimeout(machineTimer);
  fetch("/api/machine", {cache: "no-store"})
    .then(r => { if (r.status === 404) { machineGone = true; setText($("projects-empty"), "This server has no machine view"); } return r.ok ? r.json() : null; })
    .then(renderMachine).catch(() => {})
    .then(() => { if (!machineGone) machineTimer = setTimeout(pollMachine, 5000); });  // the server caches it for a few seconds
}
if (rail) {
  rail.addEventListener("click", ev => {
    const a = ev.target.closest ? ev.target.closest("button[data-act]") : null;  // a control never also picks the project
    if (a) { press(a); return; }
    const b = ev.target.closest ? ev.target.closest("button[data-project]") : null;
    if (b && b.dataset.project !== picked()) location.hash = hashFor(b.dataset.project, view);  // the pick keeps the view
  });
  rail.addEventListener("keydown", ev => {
    const f = ev.target;
    if (!f.classList || !f.classList.contains("note-in") || (ev.key !== "Enter" && ev.key !== "Escape")) return;
    const btn = f.parentElement.querySelector('button[data-act="note"]'), text = f.value.trim();
    if (f.readOnly) return;  // a send is in flight
    if (ev.key === "Enter") {  // the field and its text stay until the server says ok
      if (!text) return;
      f.readOnly = true;
      act({action: "note", session: f.dataset.target, text}, null, ok => {
        f.readOnly = false;
        if (ok && f.isConnected) { const had = document.activeElement === f; f.remove(); if (had && btn && btn.isConnected) btn.focus(); }
      });
      return;
    }
    f.remove();
    if (btn) btn.focus();
  });
  window.addEventListener("hashchange", () => {  // the one way in: a click, the back button and a typed link all arrive here
    const p = fromHash(), v = hashView();
    if (v !== view) { showView(v); keep(v); }  // Back returns to the other view; no reconnect
    if (p !== hashProject) { hashProject = currentProject = p; load(); }
  });
}

// ---------- the link ----------
const LINK = {connecting: "Connecting", live: "Live", reconnecting: "Reconnecting"};
function setLink(v) {
  if (root.getAttribute("data-link") === v) return;
  root.setAttribute("data-link", v);
  setText($("link-label"), LINK[v] || v);
}
let es = null, esQs = "", backoff = 1, retry = 0, unknown = false;
function parse(d) { try { return JSON.parse(d); } catch (err) { return null; } }
function connect() {
  clearTimeout(retry);
  if (!window.EventSource || unknown) return;
  es = new EventSource("/api/stream" + (esQs = qs()));
  es.addEventListener("open", () => { backoff = 1; snapAt = -Infinity; setLink("live"); });  // a restarted server's clock may be behind
  const on = ev => { const s = parse(ev.data); if (s) { setLink("live"); take(s); } };
  es.addEventListener("state", on);
  es.addEventListener("message", on);
  es.addEventListener("error", () => {
    setLink("reconnecting");
    // the browser gave up (a refusal, a 503), or would reconnect to a stream opened before the project was pinned: reopen ours
    if (es && (es.readyState === 2 || esQs !== qs())) {  // try again, slower each time
      es.close(); es = null;
      retry = setTimeout(connect, backoff * 1000);
      backoff = Math.min(30, backoff * 2);
    }
  });
}

// ---------- the network view (T-3034) ----------
// The same snapshot drawn as a net: agents, tools and jobs, models in three columns; an edge for each recent call (thickness
// by count, colour by kind); a bead runs the edge when a new call arrives. Held still, a called edge is lit instead.
const nv = $("net"), nx = nv && nv.getContext ? nv.getContext("2d") : null;
const vForge = $("view-forge"), vNet = $("view-net");
const VKEY = "sampo-live-view", NCAP = 8, NLIT = 6;
const NCOL = modern ? [[122, 162, 255], [150, 180, 255], [176, 196, 232]] : [WARM, EMBER, BRASS], NHEAD = ["AGENTS", "TOOLS · JOBS", "MODELS"];
const JKIND = {funnel: "judge", grow: "write", exam: "plan", train: "train", bench: "embed", check: "review"};
const store = (v) => { try { return v === undefined ? localStorage.getItem(VKEY) : localStorage.setItem(VKEY, v); } catch (err) { return null; } };
const keep = v => { if (!modern) store(v); };  // the forge remembers its view; the modern look opens on the Board unless the address says net
let view = "forge";
{
  const hv = hashParts().view;
  view = modern ? hashView() : hv === "net" ? "net" : hv === "forge" ? "forge" : store() === "net" ? "net" : "forge";
  if (!modern && view === "net" && hv !== "net" && window.history && history.replaceState) {  // the remembered view goes into the address
    try { history.replaceState(null, "", location.pathname + location.search + "#" + hashFor(hashProject, "net")); } catch (err) { /* keep the view without the hash */ }
  }
}
const boardEl = $("board"), vBoard = $("view-board");
const board = modern && boardEl && window.SampoBoard ? window.SampoBoard.mount(boardEl) : null;  // the forge look has no Board
let boardPrev = null;  // the snapshot before: the library's kept-per-hour reads the change
let nflows = [], nsnap = null, nnodes = new Map(), nedges = [], npulses = [];
const nseen = new Map(), nlitE = new Map(), nlitN = new Map(), nglowE = new Map(), ncnt = {pulses: 0, lit: 0, frames: 0}, nshown = {};
let NW = 0, NH = 0, ndpr = 1, npainted = false, nlitTimer = 0, ncols = [[], [], []];

function toolName(s) {  // "cards grow --ui --writer haiku" and "card grow" are one tool
  const w = str(s).toLowerCase().replace(/\bt-\d+\b/g, " ").split(/\s+/).filter(x => x && x[0] !== "-");
  if (w[0] && w[0].length > 3 && /[^su]s$/.test(w[0])) w[0] = w[0].slice(0, -1);
  return w.slice(0, 2).join(" ") || "unknown";
}
const tidOf = s => { const m = /\bT-\d+\b/i.exec(str(s)); return m ? m[0].toUpperCase() : ""; };
// who made a call: its own T-id, or the focus session when the caller says "session"; otherwise nobody (no guess at the lead)
const callerOf = (s, foc) => tidOf(s) || (/^\s*session\b/i.test(str(s)) ? str(obj(foc).id).replace("—", "") : "");
const toolOf = s => /[a-z]/i.test(str(s).replace(/\bT-\d+\b/gi, "").replace(/(^|\s)-\S*/g, "")) ? toolName(s) : "";  // "T-2998" alone names no tool
const nat = (key, col) => !key ? "" : nnodes.has(key) ? key : nnodes.has("g:" + col) ? "g:" + col : "";  // a hidden node is its "+N" group
const nroute = f => ({a: f.tid ? nat("a:" + f.tid, 0) : "", tl: f.tool ? nat("t:" + f.tool, 1) : "", m: nat("m:" + f.to, 2)});
let ndirty = true, nlast = 0;
const LIGHT = window.matchMedia ? matchMedia("(prefers-color-scheme: light)") : {matches: false}, NLITE = [[37, 78, 190], [52, 92, 200], [70, 84, 110]];
if (LIGHT.addEventListener) LIGHT.addEventListener("change", () => { ndirty = true; if (nx) netPaint(); if (snap) renderCharts(snap); });

function netPublish() {
  if (!nv) return;
  const o = {cols: JSON.stringify(ncols.map(c => c.map(n => n.label))), nodes: nnodes.size, edges: nedges.length,
    pulses: ncnt.pulses, lit: ncnt.lit};
  o.frames = ncnt.frames;
  for (const k of Object.keys(o)) if (nshown[k] !== o[k]) { nshown[k] = o[k]; nv.dataset[k] = String(o[k]); }
}
function netList() {  // the edges as text for a screen reader: who called what, how many
  const ul = $("net-list");
  if (!ul) return;
  const name = k => { const n = nnodes.get(k); return n ? n.label : k; };
  const rows = nedges.slice(0, 80).map(e => name(e.a) + " to " + name(e.b) + ": " + (e.dim ? "running" : e.w + (e.w === 1 ? " call" : " calls")));
  const sig = rows.join("\n");
  if (sig === nshown.list) return;
  nshown.list = sig;
  ul.textContent = "";
  for (const r of rows) { const li = document.createElement("li"); li.textContent = r; ul.appendChild(li); }
}

function netBuild() {
  if (!nx) return;
  const s = obj(nsnap), foc = obj(s.focus), mm = obj(machine);
  const taken = (list, cap, col) => {  // the first NCAP stay as nodes, the rest become one "+N" group
    const shown = list.slice(0, cap).map(e => Object.assign({col}, e));
    const rest = list.slice(cap);  // the group lights when a hidden member is running (models: was called)
    if (rest.length) shown.push({col, key: "g:" + col, label: "+" + rest.length + " more", group: true, on: rest.some(e => col === 2 ? e.n > 0 : e.on)});
    return shown;
  };
  // agents: the focus, each lane, the machine's other live sessions (a sensitive project's session is only "private")
  const A = [], aseen = new Set();
  const addA = (label, key, on) => { if (label && !aseen.has(key)) { aseen.add(key); A.push({key, label, on}); } };
  const fid = str(foc.id);
  if (fid && fid !== "—") addA(fid, "a:" + fid, true);
  for (const l of arr(s.lanes).slice(0, 10)) { const k = str(obj(l).task); if (k) addA(k, "a:" + k, obj(l).waiting !== true); }  // T-3170: a waiting lane is dim
  const quiet = new Set(arr(mm.projects).map(obj).filter(p => p.sensitive === true).map(p => str(p.slug)));
  for (const x of arr(mm.sessions).map(obj).filter(x => str(x.id)).slice(0, 40)) {
    if (quiet.has(str(x.project))) addA("private", "a:private:" + str(x.id), false);
    else { const k = str(x.task) || str(x.id).slice(0, 6); addA(k, "a:" + k, false); }
  }
  // tools: the verbs that called, then the jobs running
  const T = new Map(), tool = (name, kind) => { let e = T.get(name); if (!e) T.set(name, e = {key: "t:" + name, label: name, n: 0, on: false, kind}); return e; };
  for (const f of nflows) if (f.tool) { const e = tool(f.tool, f.kind); e.n++; e.kind = f.kind; }
  const J = arr(s.jobs).map(obj).slice(0, 12);
  for (const j of J) { const nm = toolName(j.name); if (nm) { const e = tool(nm, JKIND[str(j.kind)] || "review"); e.on = true; } }
  // models: those that were called, then the snapshot's, busiest first
  const M = new Map(), mdl = (name) => { let e = M.get(name); if (!e) M.set(name, e = {key: "m:" + name, label: name, n: 0, calls: 0, on: false}); return e; };
  for (const m of arr(s.models).slice(0, 40)) { const o = obj(m), nm = str(o.name); if (nm) { const e = mdl(nm); e.calls = num(o.calls_5m) || 0; e.on = o.loaded === true; } }
  for (const f of nflows) { const e = mdl(f.to); e.n++; e.kind = f.kind; }
  const byN = (p, q) => q.n - p.n || (q.calls || 0) - (p.calls || 0) || (q.on ? 1 : 0) - (p.on ? 1 : 0) || (p.label < q.label ? -1 : 1);
  const toolL = [...T.values()].sort((p, q) => (q.on ? 1 : 0) - (p.on ? 1 : 0) || q.n - p.n || (p.label < q.label ? -1 : 1));  // running first
  const cols = [taken(A, NCAP, 0), taken(toolL, NCAP, 1), taken(arr([...M.values()]).sort(byN), NCAP, 2)];
  const old = nnodes;
  nnodes = new Map();
  for (const c of cols) for (const e of c) {
    const o = old.get(e.key);
    const n = Object.assign(o || {glow: 0, seed: hash(e.key) % 1000 / 100, x: 0, y: 0}, e);
    nnodes.set(e.key, n);
  }
  ncols = cols.map(c => c.map(e => nnodes.get(e.key)));
  // edges: a call is agent → tool → model; a running job is a dim edge
  const E = new Map();
  const edge = (a, b, kind, dim, t) => {
    if (!a || !b || a === b) return;
    const id = a + "|" + b;
    let e = E.get(id);
    if (!e) E.set(id, e = {id, a, b, w: 0, kind, dim, t: 0});
    if (!dim) { if (e.dim) { e.dim = false; e.w = 0; } e.w++; e.kind = kind; e.t = Math.max(e.t, t); } else if (e.dim) e.w++;
  };
  for (const f of nflows) {  // an unattributed call draws tool → model only; a caller with no tool goes straight to the model
    const r = nroute(f);
    if (r.tl) { edge(r.a, r.tl, f.kind, false, f.t); edge(r.tl, r.m, f.kind, false, f.t); } else edge(r.a, r.m, f.kind, false, f.t);
  }
  for (const l of arr(s.lanes).map(obj)) {  // a lane runs on its model
    if (str(l.task) && str(l.model)) edge(nat("a:" + str(l.task), 0), nat("m:" + str(l.model), 2), "act", true);
  }
  for (const j of J) {
    const t = nat("t:" + toolName(j.name), 1), kind = JKIND[str(j.kind)] || "review", c = tidOf(j.name);
    if (c) edge(nat("a:" + c, 0), t, kind, true);
    const md = str(j.model);
    if (md) edge(t, nat("m:" + md, 2), kind, true);
  }
  nedges = [...E.values()];
  for (const k of nglowE.keys()) if (!E.has(k)) nglowE.delete(k);
  netLayout();
  netPublish(); netList();
  ndirty = true;
  if (still() && view === "net") netPaint();
}

function netLayout() {
  if (!NW) return;
  // the legend sits along the bottom edge (two rows when narrow); on a phone it is under the canvas
  const px = clamp(NW * 0.13, 52, 150), xs = [px, NW / 2, NW - px], top = 38, bot = NH - (NW <= 700 ? 26 : NW < 1000 ? 82 : 50);
  ncols.forEach((c, ci) => c.forEach((n, i) => { n.x = xs[ci]; n.y = c.length === 1 ? (top + bot) / 2 : top + (bot - top) * (i + 0.5) / c.length; }));
}

function netIngest(s) {
  nsnap = s;
  const t = now(), fresh = [];
  for (const f of arr(s.flows).slice(0, 120)) {
    const o = obj(f), to = str(o.to).trim();
    if (!to) continue;
    const k = str(o.from) + "|" + to + "|" + str(o.kind) + "|" + str(o.at);
    if (nseen.has(k)) continue;
    nseen.set(k, 1);
    fresh.push({tool: toolOf(o.from), tid: callerOf(o.from, s.focus), to, kind: KIND[str(o.kind)] ? str(o.kind) : "review", t});
  }
  while (nseen.size > 600) nseen.delete(nseen.keys().next().value);
  nflows = nflows.concat(fresh.slice().reverse()).filter(f => t - f.t < 300).slice(-120);  // oldest first
  netBuild();
  if (!fresh.length || document.hidden) return;
  // the legs of a call: caller → tool → model; with no tool the caller goes straight to the model, with no caller it starts at the tool
  const legs = f => { const r = nroute(f); return r.tl ? [[r.a, r.tl], [r.tl, r.m]] : [[r.a, r.m]]; };
  if (still()) {
    fresh.forEach(f => {
      for (const [p, q] of legs(f)) { if (p && q) nlitE.set(p + "|" + q, t + NLIT); for (const k of [p, q]) if (k) nlitN.set(k, t + NLIT); }
      ncnt.lit++;
    });
    netPublish();
    if (view === "net") netPaint();
    return;
  }
  if (view !== "net") return;
  const base = Math.max(t, npulses.length ? npulses[npulses.length - 1].t0 : t);
  const gap = Math.min(0.5, 2.2 / Math.max(1, fresh.length));
  fresh.slice().reverse().forEach((f, i) => {  // oldest first, so a burst plays in the order it happened
    const t0 = base + gap * i;
    if (npulses.length > 150) return;
    legs(f).forEach(([p, q], j) => { if (p && q && p !== q) { npulses.push({a: p, b: q, kind: f.kind, t0: t0 + 0.95 * j, dur: 1.0 + 0.1 * j}); ncnt.pulses++; } });
  });
  ndirty = true;
  netPublish();
}

function netReset() {
  nflows = []; nsnap = null; npulses.length = 0;
  nseen.clear(); nlitE.clear(); nlitN.clear(); nglowE.clear();
  netBuild();
}

function netStep(t, dt) {
  const k = Math.exp(-dt * 1.3);
  let moving = npulses.length > 0;
  for (const n of nnodes.values()) { n.glow = n.glow < 0.01 ? 0 : n.glow * k; if (n.glow) moving = true; }
  for (const [id, v] of nglowE) { if (v < 0.02) nglowE.delete(id); else { nglowE.set(id, v * k); moving = true; } }
  for (let i = npulses.length - 1; i >= 0; i--) {
    const p = npulses[i];
    if (t < p.t0 + p.dur) continue;
    const n = nnodes.get(p.b);
    if (n) n.glow = Math.min(1.4, n.glow + 0.85);
    nglowE.set(p.a + "|" + p.b, 1);
    npulses.splice(i, 1);
    moving = true;
  }
  return moving;
}

function nbez(a, b, u) {  // the synapse: leaves and lands level, like a wire between columns
  const mx = (a.x + b.x) / 2, v = 1 - u;
  return [v * v * v * a.x + 3 * v * v * u * mx + 3 * v * u * u * mx + u * u * u * b.x,
    v * v * v * a.y + 3 * v * v * u * a.y + 3 * v * u * u * b.y + u * u * u * b.y];
}
function nglow(c, x, y, r, col, a) {
  const g = c.createRadialGradient(x, y, 0, x, y, r);
  g.addColorStop(0, rgba(col, a)); g.addColorStop(0.4, rgba(col, a * 0.35)); g.addColorStop(1, rgba(col, 0));
  c.fillStyle = g; c.fillRect(x - r, y - r, r * 2, r * 2);
}

function nfit(c, s, w) {  // cut by width, in the middle, so both ends of "jeff/exam-smith" show
  if (c.measureText(s).width <= w) return s;
  for (let k = s.length - 2; k > 2; k--) {
    const f = s.slice(0, Math.ceil(k / 2)) + "…" + s.slice(s.length - Math.floor(k / 2));
    if (c.measureText(f).width <= w) return f;
  }
  return s.slice(0, 2) + "…";
}
function netHover(ev) {  // the canvas's own tooltip names the node under the pointer in full
  const r = nv.getBoundingClientRect(), x = ev.clientX - r.left, y = ev.clientY - r.top;
  let best = "", d = 20;
  for (const n of nnodes.values()) { const e = Math.hypot(n.x - x, n.y - y); if (e < d) { d = e; best = n.label; } }
  if (nv.title !== best) nv.title = best;
}

function netDraw(t, st) {
  const c = nx;
  c.setTransform(ndpr, 0, 0, ndpr, 0, 0);
  const bgd = c.createLinearGradient(0, 0, 0, NH);
  const lite = modern && LIGHT.matches;  // the modern light scheme paints the net on a light ground
  if (lite) { bgd.addColorStop(0, "#F4F6FB"); bgd.addColorStop(1, "#E9EDF5"); } else if (modern) { bgd.addColorStop(0, "#1D2640"); bgd.addColorStop(1, "#161D32"); } else { bgd.addColorStop(0, "#080A18"); bgd.addColorStop(1, "#04050E"); }
  c.fillStyle = bgd; c.fillRect(0, 0, NW, NH);
  if (!modern) {
    c.globalCompositeOperation = "lighter";
    nglow(c, NW / 2, NH * 0.9, Math.max(NW, NH) * 0.55, EMBER, 0.05);  // the forge's warmth under the net
    c.globalCompositeOperation = "source-over";
  }
  const HEADC = lite ? [74, 86, 110] : modern ? [150, 168, 205] : BRASS, INKC = lite ? [24, 32, 48] : modern ? [226, 232, 244] : PALE;
  ncnt.frames++;
  const P = nnodes;  // nodes hold still: the net redraws only while something moves
  const font = s => modern ? "500 " + s + "px system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif" : "500 " + s + "px \"Martian Mono\", ui-monospace, Menlo, monospace";
  c.font = font(11); c.textAlign = "center"; c.textBaseline = "alphabetic";
  NHEAD.forEach((head, i) => {
    const x = i === 0 ? clamp(NW * 0.13, 52, 150) : i === 1 ? NW / 2 : NW - clamp(NW * 0.13, 52, 150);
    c.fillStyle = rgba(HEADC, modern ? 1 : 0.7); c.fillText(head, x, 20);
    const g = c.createLinearGradient(0, 30, 0, NH);
    g.addColorStop(0, rgba(HEADC, 0.10)); g.addColorStop(1, rgba(HEADC, 0));
    c.fillStyle = g; c.fillRect(x - 0.5, 30, 1, NH - 30);
  });
  // the synapses
  c.lineCap = "round";
  for (const e of nedges) {
    const a = P.get(e.a), b = P.get(e.b);
    if (!a || !b) continue;
    const KC = lite ? KLITE : KIND, col = KC[e.kind] || KC.review, lit = (nlitE.get(e.id) || 0) > t, g = nglowE.get(e.id) || 0;
    // a running job: dashed, steady (breathing when animated); a past call: solid, fading with its age over the 5 minutes
    const base = e.dim ? 0.4 + (st ? 0 : 0.1 * Math.sin(t * 1.6)) : 0.3 * (1 - 0.6 * clamp((t - e.t) / 300, 0, 1));
    const al = clamp(base + 0.5 * g + (lit ? 0.55 : 0), lite ? 0.6 : 0, 1);  // a floor on light: graphics hold 3:1
    const lw = 0.8 + clamp(e.w, 1, 10) * 0.45, mx = (a.x + b.x) / 2;
    c.setLineDash(e.dim ? [5, 5] : []);
    c.beginPath(); c.moveTo(a.x, a.y); c.bezierCurveTo(mx, a.y, mx, b.y, b.x, b.y);
    c.strokeStyle = rgba(col, al * 0.22); c.lineWidth = lw * 3.2; c.stroke();  // the soft bloom
    c.strokeStyle = rgba(col, al); c.lineWidth = lw; c.stroke();
  }
  c.setLineDash([]);
  // the beads: a head with a short trail
  c.globalCompositeOperation = lite ? "source-over" : "lighter";  // additive glow whitens out on a light ground
  for (const p of npulses) {
    if (t < p.t0) continue;
    const a = P.get(p.a), b = P.get(p.b);
    if (!a || !b) continue;
    const u = clamp((t - p.t0) / p.dur, 0, 1), col = (lite ? KLITE : KIND)[p.kind] || (lite ? KLITE : KIND).review;
    for (let i = 7; i >= 1; i--) {
      const q = nbez(a, b, clamp(u - i * 0.03, 0, 1)), f = 1 - i / 8;
      nglow(c, q[0], q[1], 3 + 6 * f, col, 0.38 * f);
    }
    const h = nbez(a, b, u);
    nglow(c, h[0], h[1], 14, col, 0.9); nglow(c, h[0], h[1], 4, modern ? col : PALE, 0.95);
  }
  c.globalCompositeOperation = "source-over";
  // the nodes
  for (const [k, n] of nnodes) {
    const q = P.get(k), lit = (nlitN.get(k) || 0) > t;
    const a = clamp(Math.max(n.glow, n.on ? 0.5 : 0.18, lit ? 1 : 0), 0, 1.4);
    const KC = lite ? KLITE : KIND, col = n.kind && KC[n.kind] && n.col !== 0 ? KC[n.kind] : lite ? NLITE[n.col] : NCOL[n.col], r = n.group ? 4 : n.col === 0 ? 6.5 : 5;
    c.globalCompositeOperation = lite ? "source-over" : "lighter";
    nglow(c, q.x, q.y, 12 + 22 * a, col, 0.18 + 0.4 * a);
    c.globalCompositeOperation = "source-over";
    if (!n.group) { c.fillStyle = rgba(INKC, 0.35 + 0.6 * Math.min(1, a)); c.beginPath(); c.arc(q.x, q.y, r - 1.2, 0, TAU); c.fill(); }
    c.strokeStyle = rgba(col, 0.45 + 0.5 * Math.min(1, a)); c.lineWidth = lit ? 2.2 : 1.4;
    c.beginPath(); c.arc(q.x, q.y, r, 0, TAU); c.stroke();
    c.font = font(11);
    const lab = nfit(c, n.label, NW / 3 - 14), hw = c.measureText(lab).width / 2, lx = clamp(q.x, hw + 4, NW - hw - 4), ly = q.y + r + 14;
    c.lineJoin = "round"; c.lineWidth = 3; c.strokeStyle = lite ? "rgba(244,246,251,.9)" : modern ? "rgba(22,29,50,.85)" : "rgba(4,5,14,.85)"; c.strokeText(lab, lx, ly);  // a halo, so a line behind it doesn't cut the name
    c.fillStyle = rgba(INKC, (modern ? 0.8 : 0.5) + (modern ? 0.2 : 0.45) * Math.min(1, a)); c.fillText(lab, lx, ly);  // 11px: 4.5:1
  }
  // the vignette, as the scene has
  if (!modern) {
    const v = c.createRadialGradient(NW / 2, NH * 0.5, Math.min(NW, NH) * 0.4, NW / 2, NH * 0.5, Math.max(NW, NH) * 0.75);
    v.addColorStop(0, "rgba(2,3,10,0)"); v.addColorStop(1, "rgba(2,3,10,.5)");
    c.fillStyle = v; c.fillRect(0, 0, NW, NH);
  }
  if (!npainted) { npainted = true; nv.dataset.painted = "1"; }
}

function netPaint() {  // held still: one frame now, and one more when the lit edges go out
  if (!nx || !NW) return;
  const t = now();
  for (const m of [nlitE, nlitN]) for (const [k, u] of m) if (u <= t) m.delete(k);
  netDraw(t, true);
  netPublish();
  clearTimeout(nlitTimer);
  let next = 0;
  for (const u of nlitE.values()) next = Math.max(next, u);
  for (const u of nlitN.values()) next = Math.max(next, u);
  if (next > t && view === "net") nlitTimer = setTimeout(netPaint, (next - t) * 1000 + 50);
}

function netResize() {
  if (!nx) return;
  const r = nv.getBoundingClientRect();
  if (r.width < 2 || r.height < 2) return;
  NW = Math.round(r.width); NH = Math.round(r.height);
  ndpr = Math.max(1, Math.min(window.devicePixelRatio || 1, 2, Math.sqrt(2.6e6 / (NW * NH))));
  nv.width = Math.round(NW * ndpr); nv.height = Math.round(NH * ndpr);
  netLayout();
  ndirty = true;
  if (view === "net" && still()) netPaint();
}

const stageEl = $("stage"), stageLabel = stageEl ? stageEl.getAttribute("aria-label") : "";
function showView(v) {
  view = v === "net" || (modern && !board) ? "net" : modern ? "board" : "forge";  // no board.js: the network, not an empty page
  root.setAttribute("data-view", view);
  if (stageEl) stageEl.setAttribute("aria-label", view === "net" ? "The network: agents, tools and jobs, and models, with a line for each recent call; the calls are listed as text after the picture" : stageLabel);
  ndirty = true;
  if (vForge) vForge.setAttribute("aria-pressed", String(view === "forge"));
  if (vBoard) vBoard.setAttribute("aria-pressed", String(view === "board"));
  if (vNet) vNet.setAttribute("aria-pressed", String(view === "net"));
  if (board) board.show(view === "board");
  if (view === "board") { stop(); return; }
  if (view === "net") { netResize(); netBuild(); } else resize();
  if (still()) paintStill(); else start();
}
function pickView(v) {
  if (v === view) return;
  showView(v); keep(v);
  if (view !== "board" && stageEl && stageEl.scrollIntoView && stageEl.getBoundingClientRect().bottom > window.innerHeight) {  // below 1400 px the stage sits under the rail's strips
    stageEl.scrollIntoView({block: "nearest", behavior: still() ? "auto" : "smooth"});
  }
  location.hash = hashFor(hashProject, v);  // its hashchange finds the view already set: no reload, no reconnect
}

// ---------- modes and start ----------
const btn = $("motion");
function applyMode() {
  const s = still();
  root.setAttribute("data-motion", s ? "still" : "full");
  if (board) board.still(s);
  if (btn) {
    btn.setAttribute("aria-pressed", String(s));
    btn.textContent = s && !reduce.matches ? "Animate" : "Still";  // the label names what a press does
    btn.disabled = !!reduce.matches;
    btn.title = reduce.matches ? "Your system asks for reduced motion: the scene holds still and changes are lit"
      : "Hold the scene still: no sparks, changes shown by highlight";
  }
  if (s) {
    stop();
    sparks.length = 0; pending.length = 0; embers.length = 0; streaks.length = 0; smoke.length = 0; flares.length = 0;
    npulses.length = 0;
    intro = false;
    paintStill();
  } else start();
  if (tkList) { tkList.replaceChildren(); tkOff = 0; tkList.style.transform = ""; renderTicker(ticker, true); }
}
if (btn) btn.addEventListener("click", () => { userStill = !userStill; applyMode(); });
const themeBtn = $("theme-btn");
if (themeBtn) {  // the viewer's look is kept in this browser; a reload applies it before the first paint
  themeBtn.setAttribute("aria-pressed", String(!modern));  // one fixed name; pressed means the forge look is on
  try {  // after the switch's reload: focus is back on the button and the status line says what happened
    const said = sessionStorage.getItem("sampo-live-said");
    if (said) { sessionStorage.removeItem("sampo-live-said"); themeBtn.focus(); setTimeout(() => setText($("theme-say"), said), 80); }
  } catch (err) { /* no session storage: no announcement */ }
  themeBtn.addEventListener("click", () => {
    try { localStorage.setItem("sampo-live-theme", modern ? "forge" : "modern"); } catch (err) { /* no storage: the switch lasts this load only */ }
    try { sessionStorage.setItem("sampo-live-said", modern ? "Forge look on" : "Forge look off"); } catch (err) { /* nothing to announce */ }
    location.reload();
  });
}
if (reduce.addEventListener) reduce.addEventListener("change", applyMode);
else if (reduce.addListener) reduce.addListener(applyMode);
document.addEventListener("visibilitychange", () => {
  if (document.hidden) { stop(); sparks.length = 0; pending.length = 0; npulses.length = 0; }
  else if (!still()) start();
  else paintStill();
});

if (cx) {
  mk("anvil", "anvil", {}, 0);
  if (window.ResizeObserver) { const ro = new ResizeObserver(resize); ro.observe(cv); if ($("focus")) ro.observe($("focus")); }
  else window.addEventListener("resize", resize);
  resize();
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(() => { if (still()) paintStill(); });
}
cnt.nodes = nodes.size;
publish();
if (nx) {
  if (window.ResizeObserver) new ResizeObserver(netResize).observe(nv); else window.addEventListener("resize", netResize);
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(netResize);
  nv.addEventListener("mousemove", netHover);
}
if (vForge) vForge.addEventListener("click", () => pickView("forge"));
if (vNet) vNet.addEventListener("click", () => pickView("net"));
if (vBoard) vBoard.addEventListener("click", () => pickView("board"));
showView(view);
applyMode();
setInterval(age, 1000);
// (re)load the scene for currentProject: fetch its state, reopen its stream; a late answer for a project we left is dropped
function load() {
  const id = ++loadId;
  if (es) { es.close(); es = null; }
  clearTimeout(retry);
  backoff = 1; snapAt = -Infinity; firstRender = true; unknown = false;
  netReset();
  if (topRow) topRow.setAttribute("data-swap", "");
  markCurrent();
  fetch("/api/state" + qs(), {cache: "no-store"}).then(r => r.status === 404 ? 404 : r.ok ? r.json() : null).then(s => {
    if (id !== loadId) return;
    if (s === 404) {  // not a project this server serves: show that, not the last project's data, and stop asking
      unknown = true;
      if (es) { es.close(); es = null; }
      clearTimeout(retry);
      take({project: {slug: currentProject}});  // an empty snapshot clears the scene, panels, queue, closes and ticker
      firstRender = true; snapAt = -Infinity;
      setText($("project"), "no project " + currentProject + " here");
    } else if (s) take(s);
  }).catch(() => {}).then(() => { if (id === loadId && topRow) topRow.removeAttribute("data-swap"); });
  connect();
}
load();
pollMachine();
})();
