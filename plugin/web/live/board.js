/* Sampo · the Board (T-3659): the live page's modern default. Nine panels, each a small instrument for one Sampo
   mechanism, drawn on canvas from the snapshot the server streams (snapshot.board, built by sampo.board).
   boardModel(snapshot, history) is the one mapping from snapshots to panels: plain, bounded data (test_board runs it
   under node). mount(el) builds the panels and runs one requestAnimationFrame loop over the visible ones: each panel
   keeps its still parts in a cached layer and draws only its moving parts per frame. No libraries; the palette is
   read from board.css's custom properties, so the canvases and the chrome share it. */
"use strict";
const SampoBoard = (() => {
  const KINDS = ["tests", "claude", "models", "hooks", "other"];
  const STAGES = ["captured", "planning", "ready", "executing", "verifying", "documenting", "auditing", "closing"];
  const LANE_STATES = ["building", "waiting", "parked", "merged"];
  const TEST_STATES = ["cached", "running", "pass", "fail", "idle"];
  const PANELS = [
    {key: "lanes", name: "Lanes", what: "builders and the files they hold", accent: "cyan"},
    {key: "train", name: "Merge train", what: "finished lanes into one build", accent: "yellow"},
    {key: "cpu", name: "CPU", what: "the machine by kind of work", accent: "green"},
    {key: "loop", name: "The loop", what: "your words into a brief", accent: "pink"},
    {key: "library", name: "Library", what: "cards and moons kept", accent: "violet"},
    {key: "models", name: "Models", what: "who calls which model", accent: "orange"},
    {key: "sampo", name: "Sampo", what: "commands and the queue", accent: "cyan"},
    {key: "state", name: "State", what: "the files Sampo keeps", accent: "yellow"},
    {key: "tests", name: "Tests", what: "every module, cached or run", accent: "green"},
  ];

  // ---------- the model: any snapshot, empty or huge, to nine bounded panels ----------
  const arr = (x) => (Array.isArray(x) ? x : []);
  const obj = (x) => (x && typeof x === "object" && !Array.isArray(x) ? x : {});
  const num = (x, d = 0) => (typeof x === "number" && isFinite(x) ? x : d);
  const int = (x) => Math.max(0, Math.round(num(x)));
  const r2 = (x) => Math.round(Math.max(0, num(x)) * 100) / 100;
  const fmt = (n) => Math.round(num(n)).toLocaleString("en-US");
  const HOME = /\/(?:home|Users)\/[^/\s"']+/g;  // the server redacts; this keeps a stray home path off the page too
  function str(x, n) {
    const s = (x == null ? "" : String(x)).replace(HOME, "~").replace(/\s+/g, " ").trim();
    return s.length > n ? s.slice(0, n - 1) + "…" : s;
  }
  const plural = (n, one, many) => `${fmt(n)} ${n === 1 ? one : many || one + "s"}`;

  // T-3730 All mode: every project's items at once, coloured and tagged by project
  const PCOL = ["cyan", "yellow", "green", "pink", "violet", "orange"];  // board-tokens.json names, so both themes follow
  function projColors(slugs) {  // a stable colour per slug (its hash), probing on to the next free one so a few projects differ
    const out = {}, used = new Set();
    for (const s of [...new Set(slugs)].sort()) {
      let h = 5381;
      for (let i = 0; i < s.length; i++) h = (Math.imul(h, 33) + s.charCodeAt(i)) >>> 0;
      let i = h % PCOL.length, n = 0;
      while (used.has(i) && n++ < PCOL.length) i = (i + 1) % PCOL.length;
      used.add(i);
      if (used.size >= PCOL.length) used.clear();
      out[s] = PCOL[i];
    }
    return out;
  }
  const projTag = (name) => str(String(name || ""), 10);
  function projectList(snap) {  // [{slug, name, tag, pc, sensitive}] for the chips; [] outside All mode
    const s = obj(snap);
    if (!s.all) return [];
    const ps = arr(s.projects).map(obj).filter((p) => typeof p.slug === "string"), col = projColors(ps.filter((p) => !p.sensitive).map((p) => p.slug));
    return ps.map((p) => ({slug: str(p.slug, 80), name: str(p.name, 24), tag: projTag(p.name), pc: col[p.slug] || "dim", sensitive: !!p.sensitive}));
  }
  function allCtx(s, hidden) {
    if (!s.all) return null;
    const list = projectList(s), by = new Map(list.map((p) => [p.slug, p])), off = new Set(arr(hidden));
    return {vis: (x) => !off.has(obj(x).project) && !(by.get(obj(x).project) || {}).sensitive,
      mark: (x) => {
        const p = by.get(obj(x).project) || {};
        return {proj: str(obj(x).project, 80), tag: p.tag || "", pc: p.pc || "cyan"};
      }};
  }
  function spread(rows, n) {  // up to n rows, taking turns across projects so a busy one doesn't crowd the rest out
    const g = new Map();
    for (const r of rows) (g.get(obj(r).project) || g.set(obj(r).project, []).get(obj(r).project)).push(r);
    const out = [], q = [...g.values()];
    for (let i = 0; out.length < n && q.some((x) => i < x.length); i++) for (const x of q) if (i < x.length && out.length < n) out.push(x[i]);
    return out;
  }

  function mLanes(s, b, A) {
    let src = Array.isArray(b.lanes) ? b.lanes
      : arr(s.lanes).map((l) => ({task: obj(l).task, title: obj(l).title, state: obj(l).waiting ? "waiting" : "building",
        steps: obj(l).steps}));
    if (A) src = src.filter(A.vis);
    const shown = A ? spread(src, 16) : src.slice(0, 16);
    const clusters = shown.map((l) => {
      l = obj(l);
      const files = arr(l.files), st = obj(l.steps);
      return {...(A ? A.mark(l) : {}), task: str(l.task, 12), title: str(l.title, 80), state: LANE_STATES.includes(l.state) ? l.state : "building",
        steps: {done: int(st.done), total: int(st.total)},
        nodes: files.slice(0, 24).map((f) => {
          const p = str(obj(f).path, 160), i = p.lastIndexOf("/");
          return {p: str(p.slice(i + 1), 32), d: str(p.slice(0, i + 1), 48), hot: !!obj(f).hot};
        }),
        more: int(l.more) + Math.max(0, files.length - 24)};
    });
    return {clusters, more: Math.max(0, src.length - clusters.length)};
  }

  function mTrain(b, lanes, A) {
    const ts = A ? arr(b.trains).map(obj).filter(A.vis) : [obj(b.train)], rank = {waiting: 0, building: 1};
    const waiting = lanes.clusters.filter((c) => c.state in rank).sort((x, y) => rank[x.state] - rank[y.state]).slice(0, 6)
      .map((c) => ({task: c.task, title: c.title, state: c.state, files: c.nodes.length + c.more, tag: c.tag, pc: c.pc}));
    const landed = [];
    for (const t of ts) for (const x of arr(t.landed)) {
      landed.push({...(A ? A.mark(t) : {}), task: str(obj(x).task, 12), title: str(obj(x).title, 64), at: str(obj(x).at, 32)});
    }
    if (A) landed.sort((x, y) => (x.at < y.at ? -1 : x.at > y.at ? 1 : 0));
    const sum = (k) => ts.reduce((a, t) => a + int(t[k]), 0), mp = ts.filter((t) => t.minutes_per_lane != null);
    return {known: A ? ts.length > 0 : !!b.train, waiting, landed: landed.slice(-8),
      landed24: sum("landed_24h"), trains24: sum("trains_24h"), left: sum("left_out"), running: ts.some((t) => !!t.running),
      mpl: mp.length ? Math.round(mp.reduce((a, t) => a + int(t.minutes_per_lane), 0) / mp.length) : null};
  }

  function mCpu(b) {
    const c = obj(b.cpu), now = obj(c.now), out = {known: !!b.cpu, kinds: KINDS, goal: num(c.goal_pct, 10) || 10,
      cores: Math.max(1, int(c.cores)), minutes: arr(c.minutes).slice(-30).map((r) => [int(arr(r)[0])].concat(
        KINDS.map((_, i) => r2(arr(r)[i + 1])))), now: {busy: r2(now.busy)}};
    for (const k of KINDS) out.now[k] = r2(now[k]);
    return out;
  }

  function mLoop(s, b, A) {
    if (A) {  // one compact words → brief row per project that has a task in focus
      const loops = arr(b.loops).map(obj).filter(A.vis).slice(0, 6).map((l) => ({...A.mark(l), id: str(l.id, 12), words: str(l.words, 240),
        title: str(l.title, 120), stage: str(l.stage, 16), choice: l.choice == null || !isFinite(l.choice) ? null : Math.min(100, int(l.choice)),
        ok: arr(l.criteria).filter((x) => obj(x).ok).length, of: arr(l.criteria).length,
        done: arr(l.steps).filter((x) => obj(x).done).length, steps: arr(l.steps).length,
        list: arr(l.steps).slice(0, 12).map((x) => ({text: str(obj(x).text, 80), done: !!obj(x).done, current: !!obj(x).current}))}));
      return loops.length ? {all: true, loops} : null;
    }
    const l = b.loop ? obj(b.loop) : null, f = obj(s.focus);
    if (!l && !f.id) return null;
    if (!l) {
      const [done, total] = String(f.step || "").split("/").map((x) => int(Number(x)));
      return {id: str(f.id, 12), words: str(f.title, 240), title: str(f.title, 120), criteria: [],
        steps: Array.from({length: Math.min(9, total || 0)}, (_, i) => ({text: `step ${i + 1}`, done: i < done - 1,
          current: i === done - 1})), choice: null, stage: str(f.stage, 16)};
    }
    return {id: str(l.id, 12), words: str(l.words, 240), title: str(l.title, 120),
      criteria: arr(l.criteria).slice(0, 6).map((x) => ({text: str(obj(x).text, 90), ok: !!obj(x).ok})),
      steps: arr(l.steps).slice(0, 9).map((x) => ({text: str(obj(x).text, 70), done: !!obj(x).done, current: !!obj(x).current})),
      choice: l.choice == null || !isFinite(l.choice) ? null : Math.min(100, int(l.choice)), stage: str(l.stage || f.stage, 16)};
  }

  const LIB_GRIDS = ["cards", "ui", "moons", "calling"];
  const GLOBE = 600;  // the globe's lattice: a dot per kept card or moon up to this many, past it each dot stands for several
  function libCounts(s) {
    const st = obj(s.stats), cards = obj(st.cards), moons = obj(st.moons), lib = obj(obj(s.board).library);
    return {cards: cards.general, ui: cards.ui, moons: moons.candidates, calling: lib.calling};
  }
  function mLibrary(s, b, prev) {
    const raw = libCounts(s), was = libCounts(prev), lib = obj(b.library), counts = {}, grew = {}, dots = {};
    for (const k of LIB_GRIDS) {
      counts[k] = int(raw[k]);
      grew[k] = typeof was[k] === "number" && typeof raw[k] === "number" ? Math.max(0, int(raw[k]) - int(was[k])) : 0;
    }
    const total = LIB_GRIDS.reduce((a, k) => a + counts[k], 0);
    const scale = total ? Math.min(1, (GLOBE - LIB_GRIDS.length) / total) : 0;
    for (const k of LIB_GRIDS) dots[k] = counts[k] ? Math.max(1, Math.floor(counts[k] * scale)) : 0;
    const hourly = arr(lib.hourly).slice(-24).map(int);
    while (hourly.length < 24) hourly.unshift(0);
    return {known: !!b.library, counts, total, dots, grew, hourly,
      recent: arr(lib.recent).slice(-6).map((r) => ({grid: str(obj(r).grid, 12), at: str(obj(r).at, 32), kept: int(obj(r).kept)}))};
  }

  const GROUPS = ["claude", "jeff", "ollama"];
  const group = (family, name) => {
    const f = String(family || "").toLowerCase(), n = String(name || "").toLowerCase();
    if (f === "claude" || /^(claude|opus|sonnet|haiku)/.test(n)) return "claude";
    if (f === "jeff" || n.startsWith("jeff")) return "jeff";
    return "ollama";
  };
  const agent = (x) => str(String(x == null ? "" : x).split(/\s--?/)[0].split(" ").slice(0, 2).join(" "), 20);
  function mModels(s) {
    const models = arr(s.models).map(obj), byName = new Map(models.map((m) => [String(m.name), m]));
    const links = new Map();
    const add = (a, m, kind) => {
      if (!a || !m) return;
      const k = a + "\u0000" + m, l = links.get(k) || {a, m, n: 0, kind};
      l.n++;
      links.set(k, l);
    };
    for (const f of arr(s.flows).slice(0, 600)) add(agent(obj(f).from), str(obj(f).to, 32), str(obj(f).kind, 12));
    for (const j of arr(s.jobs).slice(0, 60)) if (obj(j).model) add(agent(j.name), str(j.model, 32), str(j.kind, 12));
    let L = [...links.values()].sort((x, y) => y.n - x.n || (x.a + x.m < y.a + y.m ? -1 : 1));
    const agents = [...new Set(L.map((l) => l.a))].slice(0, 8);
    L = L.filter((l) => agents.includes(l.a));
    const names = [...new Set(L.map((l) => l.m))].slice(0, 10);
    const ms = names.map((n) => {
      const m = obj(byName.get(n));
      return {name: n, group: group(m.family, n), calls: int(m.calls_5m), loaded: m.loaded !== false};
    }).sort((x, y) => GROUPS.indexOf(x.group) - GROUPS.indexOf(y.group) || y.calls - x.calls);
    const at = new Map(ms.map((m, i) => [m.name, i]));
    const gpu = obj(s.gpu), calls = models.reduce((a, m) => a + int(m.calls_5m), 0);
    return {agents: agents.map((name) => ({name})), models: ms,
      links: L.filter((l) => at.has(l.m)).slice(0, 24).map((l) => ({a: agents.indexOf(l.a), m: at.get(l.m), n: l.n, kind: l.kind})),
      gpu: s.gpu ? {name: str(gpu.name, 40), used: int(gpu.used_mb), total: int(gpu.total_mb), util: Math.min(100, int(gpu.util_pct))} : null,
      cpm: Math.round(calls / 5), loaded: models.filter((m) => m.loaded).length};
  }

  function mSampo(s, b, A) {
    const q = A ? arr(s.queue).filter(A.vis) : arr(s.queue);
    return {commands: arr(b.commands).slice(-12).map((x) => ({at: str(obj(x).at, 32), cmd: str(obj(x).cmd, 96), ok: obj(x).ok !== false})),
      queue: (A ? spread(q, 8) : q.slice(0, 8)).map((x) => ({...(A ? A.mark(x) : {}), id: str(obj(x).id, 12), title: str(obj(x).title, 64),
        status: str(obj(x).status, 10), tier: str(obj(x).tier, 2), type: str(obj(x).type, 10)})),
      queued: q.length,
      deps: arr(b.deps).slice(0, 40).map((d) => arr(d).slice(0, 4).map((x) => str(x, 12))).filter((d) => d.length > 1)};
  }

  function mState(b) {
    const st = obj(b.state), doc = st.doc ? obj(st.doc) : null;
    return {known: !!b.state,
      tree: arr(st.tree).slice(0, 12).map((e) => ({name: str(obj(e).name, 32), dir: !!obj(e).dir, at: str(obj(e).at, 32), n: int(obj(e).n),
        kb: int(obj(e).kb)})),
      newest: str(st.newest, 32),
      doc: doc && {kind: str(doc.kind, 12), title: str(doc.title, 80), text: str(doc.text, 240), at: str(doc.at, 32)}};
  }

  function mTests(b) {
    const t = obj(b.tests), mods = arr(t.modules), count = {};
    for (const k of TEST_STATES) count[k] = 0;
    for (const m of mods.slice(0, 5000)) count[TEST_STATES.includes(obj(m).state) ? m.state : "idle"]++;
    return {known: !!b.tests, count, more: Math.max(0, mods.length - 400), done: int(t.done), total: int(t.total),
      running: int(t.running), source: str(t.source, 24),
      cells: mods.slice(0, 400).map((m) => ({n: str(String(obj(m).name || "").replace(/^test_/, ""), 28),
        s: TEST_STATES.includes(obj(m).state) ? m.state : "idle"}))};
  }

  // each panel's right-hand label and footer
  const pct = (a, b) => (b ? Math.min(100, Math.round((a / b) * 1000) / 10) : 0);
  const FOOT = {
    lanes: (d) => {
      const n = {};
      let files = 0, hot = 0;
      for (const c of d.clusters) {
        n[c.state] = (n[c.state] || 0) + 1;
        files += c.nodes.length + c.more;
        hot += c.nodes.filter((x) => x.hot).length;
      }
      return {label: plural(d.clusters.length + d.more, "lane"),
        foot: {text: LANE_STATES.filter((k) => n[k]).map((k) => `${k} ${n[k]}`).join(" · ") || "no builder lanes out",
          stat: files ? `${hot} hot of ${plural(files, "file")}` : ""}};
    },
    train: (d) => ({label: d.running ? "train running" : "foreman/build",
      foot: {text: d.known ? `${plural(d.landed24, "lane")} landed in 24 h · ${plural(d.trains24, "train")} · ${d.left} left out`
        : "no train has run yet", stat: d.mpl == null ? "" : `${d.mpl} min / lane`}}),
    cpu: (d) => ({label: `goal ${d.goal}%`,
      foot: {text: d.known ? `${d.now.busy.toFixed(1)} of ${plural(d.cores, "core")} busy now` : "no CPU reading yet",
        stat: d.known ? `${pct(d.now.busy, d.cores).toFixed(1)}%` : "", pct: d.known ? pct(d.now.busy, d.cores) : null}}),
    loop: (d) => {
      if (!d) return {label: "idle", foot: {text: "no task in focus", stat: ""}};
      if (d.all) return {label: plural(d.loops.length, "loop"), foot: {text: d.loops.map((l) => `${l.tag} ${l.id}`).join(" · "), stat: ""}};
      const ok = d.criteria.filter((c) => c.ok).length, done = d.steps.filter((x) => x.done).length;
      return {label: d.stage || "focus",
        foot: {text: d.criteria.length ? `${d.id} · ${ok} of ${plural(d.criteria.length, "criterion", "criteria")} met` : `${d.id} · ${d.title}`,
          stat: d.choice == null ? "" : `choice ${d.choice}%`, pct: d.steps.length ? pct(done, d.steps.length) : null}};
    },
    library: (d) => ({label: `${fmt(d.total)} kept`,
      foot: {text: LIB_GRIDS.map((k) => `${k} ${fmt(d.counts[k])}`).join(" · "), stat: `${fmt(d.hourly[23])} / h`}}),
    models: (d) => ({label: `${fmt(d.cpm)} calls / min`,
      foot: {text: d.gpu ? `GPU ${d.gpu.util}% · VRAM ${(d.gpu.used / 1024).toFixed(1)} of ${(d.gpu.total / 1024).toFixed(1)} GB`
        : "no GPU reading", stat: `${d.loaded} loaded`, pct: d.gpu ? d.gpu.util : null}}),
    sampo: (d) => {
      const last = d.commands[d.commands.length - 1], ok = d.commands.filter((c) => c.ok).length;
      return {label: `${fmt(d.queued)} queued`,
        foot: {text: last ? `$ ${last.cmd}` : "no sampo commands seen yet", stat: d.commands.length ? `${ok}/${d.commands.length} ok` : ""}};
    },
    state: (d) => ({label: "state/",
      foot: {text: d.known ? `${plural(d.tree.length, "entry", "entries")} · newest ${d.newest || "—"}` : "the state folder isn't read yet",
        stat: d.doc ? `latest ${d.doc.kind}` : ""}}),
    tests: (d) => ({label: plural(d.cells.length + d.more, "module"),
      foot: {text: d.known ? `${d.count.cached} cached · ${d.count.running} running · ${d.count.fail} failing${d.source ? " · " + d.source : ""}`
        : "no test run seen yet", stat: d.total ? `${d.done}/${d.total} checks passed` : "", pct: d.total ? pct(d.done, d.total) : null}}),
  };

  function boardModel(snap, hist, hidden) {  // hidden: project slugs the chips turned off (All mode)
    const s = obj(snap), b = obj(s.board), h = arr(hist), prev = obj(h[h.length - 1]), A = allCtx(s, hidden);
    const data = {lanes: mLanes(s, b, A), cpu: mCpu(b), loop: mLoop(s, b, A), library: mLibrary(s, b, prev), models: mModels(s),
      sampo: mSampo(s, b, A), state: mState(b), tests: mTests(b)};
    data.train = mTrain(b, data.lanes, A);
    return {at: str(s.at, 32), panels: PANELS.map((P, i) => {
      const f = FOOT[P.key](data[P.key]);
      return {n: String(i + 1).padStart(2, "0"), key: P.key, name: P.name, what: P.what, accent: P.accent, label: f.label,
        foot: f.foot, data: data[P.key]};
    })};
  }

  // ---------- drawing ----------
  const TOKENS = {bg: "#0a0e16", panel: "#0e1420", line: "#1c2433", grid: "#141b28", ink: "#d6deeb", dim: "#717e97",
    muted: "#98a4ba", bracket: "#4cc9f0", on: "#0a0e16", yellow: "#f5c84c", green: "#5ee6a0", pink: "#ff6b9a",
    cyan: "#4cc9f0", violet: "#9d8cff", orange: "#ff9f43", red: "#ff5d6c"};  // dark, if board.css didn't load
  const FONT = '"Martian Mono", ui-monospace, Menlo, Consolas, monospace';
  const STILL_T = 7.25;  // Still and reduced motion draw every panel at this one instant

  function rgba(hex, a) {
    const m = /^#([0-9a-f]{6})$/i.exec(hex);
    if (!m) return hex;
    const n = parseInt(m[1], 16);
    return `rgba(${n >> 16},${(n >> 8) & 255},${n & 255},${a})`;
  }
  function txt(g, s, x, y, col, px = 10, align = "left", weight = 400) {
    g.font = `${weight} ${px}px ${FONT}`;
    g.fillStyle = col;
    g.textAlign = align;
    g.textBaseline = "alphabetic";
    g.fillText(s, x, y);
  }
  function fit(g, s, max) {  // the longest prefix of s (plus …) that fits max px in the current font
    if (g.measureText(s).width <= max) return s;
    let lo = 0, hi = s.length;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (g.measureText(s.slice(0, mid) + "…").width <= max) lo = mid;
      else hi = mid - 1;
    }
    return lo ? s.slice(0, lo) + "…" : "";
  }
  function fitWords(g, s, max, min = 1) {  // like fit, but ends on a whole word; "" when fewer than `min` words fit (never a stub like "Ben…")
    if (g.measureText(s).width <= max) return s;
    const cut = fit(g, s, max).replace(/…$/, ""), at = cut.lastIndexOf(" ");
    const r = at > 0 && s[cut.length] !== " " ? cut.slice(0, at) + "…" : cut.length && s[cut.length] === " " ? cut + "…" : "";
    return r.replace(/…$/, "").split(" ").length >= min ? r : "";
  }
  function box(g, x, y, w, h, col, fill = 0.08) {
    x = Math.round(x); y = Math.round(y); w = Math.round(w); h = Math.round(h);
    g.fillStyle = rgba(col, fill);
    g.fillRect(x, y, w, h);
    g.strokeStyle = col;
    g.lineWidth = 1;
    g.strokeRect(x + 0.5, y + 0.5, w - 1, h - 1);
  }
  function hline(g, x0, x1, y, col) {
    g.fillStyle = col;
    g.fillRect(Math.round(x0), Math.round(y), Math.round(x1 - x0), 1);
  }
  function waiting(g, w, h, T, say) {
    txt(g, say, w / 2, h / 2, T.dim, 10, "center");
  }
  function seed(s) {  // mulberry32 seeded from a string: a layout holds still from one snapshot to the next
    let a = 2166136261;
    for (let i = 0; i < s.length; i++) a = Math.imul(a ^ s.charCodeAt(i), 16777619);
    return () => {
      a = (a + 0x6d2b79f5) | 0;
      let t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }
  function pill(g, s, x, y, col, bg, px = 8.5, align = "center", solid = false) {  // a rounded tag; y is its middle
    g.font = `500 ${px}px ${FONT}`;
    const w = Math.ceil(g.measureText(s).width) + 12, h = Math.round(px + 7);
    const x0 = Math.round(align === "center" ? x - w / 2 : align === "right" ? x - w : x), y0 = Math.round(y - h / 2);
    g.beginPath();
    if (g.roundRect) g.roundRect(x0 + 0.5, y0 + 0.5, w - 1, h - 1, (h - 1) / 2);
    else g.rect(x0 + 0.5, y0 + 0.5, w - 1, h - 1);
    g.fillStyle = solid ? col : bg;
    g.fill();
    g.strokeStyle = col;
    g.lineWidth = 1;
    g.stroke();
    g.fillStyle = solid ? bg : col;
    g.textAlign = "left";
    g.textBaseline = "middle";
    g.fillText(s, x0 + 6, y0 + h / 2 + 0.5);
    return w;
  }
  function wrap(g, s, max, lines) {  // s in at most `lines` lines of max px in the current font, the last cut with …
    const out = [], words = String(s || "").split(" ");
    let cur = "";
    if (lines < 1) return out;
    for (let i = 0; i < words.length; i++) {
      const next = cur ? cur + " " + words[i] : words[i];
      if (!cur || g.measureText(next).width <= max) {
        cur = next;
        continue;
      }
      if (out.length === lines - 1) return out.concat(fit(g, words.slice(i).reduce((a, x) => a + " " + x, cur), max));
      out.push(fit(g, cur, max));
      cur = words[i];
    }
    if (cur) out.push(fit(g, cur, max));
    return out;
  }
  function hatch(g, x, y, w, h, col) {  // diagonal rules in a box, the reference's sidebar look
    g.save();
    g.beginPath();
    g.rect(x, y, w, h);
    g.clip();
    g.strokeStyle = col;
    g.lineWidth = 1;
    g.beginPath();
    for (let i = -h; i < w; i += 5) {
      g.moveTo(x + i, y + h);
      g.lineTo(x + i + h, y);
    }
    g.stroke();
    g.restore();
  }
  const bez = (a, b, c, d, t) => {
    const u = 1 - t;
    return u * u * u * a + 3 * u * u * t * b + 3 * u * t * t * c + t * t * t * d;
  };
  const link = (x0, y0, x1, y1) => {  // an S-curve from (x0, y0) to (x1, y1), as cubic control points
    const m = (x0 + x1) / 2;
    return {x: [x0, m, m, x1], y: [y0, y0, y1, y1]};
  };
  const along = (P, u) => [bez(P.x[0], P.x[1], P.x[2], P.x[3], u), bez(P.y[0], P.y[1], P.y[2], P.y[3], u)];
  function curve(g, P) {
    g.moveTo(P.x[0], P.y[0]);
    g.bezierCurveTo(P.x[1], P.y[1], P.x[2], P.y[2], P.x[3], P.y[3]);
  }
  function hm(iso) {  // an ISO time as the viewer's local HH:MM
    const d = new Date(iso);
    return isNaN(d) ? "" : String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
  }
  const TAU = Math.PI * 2;

  // each panel: base(g, data, w, h, T, mem) draws the still layer (cached until the data, size or palette change);
  // live(g, data, w, h, t, T, mem) draws what moves, every frame. mem is the panel's own scratch (geometry, last seen).
  const DRAW = {};

  DRAW.cpu = {
    base(g, d, w, h, T, mem) {
      mem.g = null;
      if (!d.known && !d.minutes.length) return waiting(g, w, h, T, "waiting for the first CPU reading");
      const col = {tests: T.yellow, claude: T.pink, models: T.cyan, hooks: T.violet, other: T.dim};
      const pad = 14, narrow = w < 360, side = narrow ? 0 : Math.min(150, Math.round(w * 0.3));
      const busy = d.now.busy, share = pct(busy, d.cores), over = share > d.goal;
      // the "now" figure and the kinds, left
      if (!narrow) {
        txt(g, busy.toFixed(1), pad, pad + 30, T.ink, 32, "left", 500);
        txt(g, `cores busy of ${d.cores}`, pad, pad + 48, T.dim, 9.5);
        txt(g, `${share.toFixed(1)}% · ${over ? "over" : "under"} goal`, pad, pad + 64, over ? T.orange : T.green, 9.5, "left", 500);
        KINDS.forEach((k, i) => {
          const y = pad + 92 + i * 17;
          if (y > h - 10) return;
          g.fillStyle = col[k];
          g.fillRect(pad, y - 7, 7, 7);
          txt(g, k, pad + 13, y, T.muted, 9.5);
          txt(g, d.now[k].toFixed(1), side - 8, y, T.ink, 9.5, "right");
        });
      } else {
        txt(g, `${busy.toFixed(1)} cores busy · ${share.toFixed(1)}%`, pad, pad + 10, over ? T.orange : T.green, 10, "left", 500);
      }
      // the stacked bars, right: one per minute, the newest at the right edge
      // the bars stop short of a right gutter: the axis figures and the goal's mark sit there, never on a bar
      const gut = 30, cx = side + (narrow ? pad : 10), cy = pad + (narrow ? 26 : 8), cw = w - cx - pad - gut, ch = h - cy - 24;
      const goal = (d.cores * d.goal) / 100;
      const tops = d.minutes.map((r) => r.slice(1).reduce((a, v) => a + v, 0));
      const peak = Math.max(goal * 1.6, ...tops, 0.1) * 1.12;
      const ys = (v) => cy + ch - (v / peak) * ch;
      const gy = Math.round(ys(goal)) + 0.5;
      for (let i = 0; i <= 4; i++) {
        const v = (peak * i) / 4, y = Math.round(ys(v));
        hline(g, cx, cx + cw, y, i ? T.grid : T.line);
        if (i && Math.abs(y - gy) > 10) txt(g, v.toFixed(v < 10 ? 1 : 0), cx + cw + gut, y + 3, T.dim, 8.5, "right");
      }
      const n = 30, bw = cw / n, gap = Math.max(1, Math.round(bw * 0.24)), off = n - d.minutes.length;
      d.minutes.forEach((r, j) => {
        const x = Math.round(cx + (off + j) * bw);
        let y = cy + ch;
        KINDS.forEach((k, i) => {
          const hh = (r[i + 1] / peak) * ch;
          if (hh <= 0) return;
          g.fillStyle = rgba(col[k], k === "other" ? 0.55 : 0.82);
          g.fillRect(x, Math.round(y - hh), Math.max(1, Math.round(bw) - gap), Math.max(1, Math.round(y) - Math.round(y - hh)));
          y -= hh;
        });
      });
      txt(g, "−30 min", cx, h - 9, T.dim, 8.5);
      txt(g, "−15", cx + cw / 2, h - 9, T.dim, 8.5, "center");
      txt(g, "now", cx + cw, h - 9, T.dim, 8.5, "right");
      txt(g, `${d.goal}%`, cx + cw + gut, gy + 3, T.green, 8.5, "right", 500);
      const last = d.minutes.length - 1;
      mem.g = {cx, cw, gy, bw, gap, lastX: Math.round(cx + (off + last) * bw), lastTop: last >= 0 ? ys(tops[last]) : cy + ch,
        base: cy + ch, cy};
    },
    live(g, d, w, h, t, T, mem) {
      const m = mem.g;
      if (!m) return;
      // the goal line marches slowly; the newest minute's top breathes; a faint sweep reads the bars left to right
      g.save();
      g.strokeStyle = rgba(T.green, 0.9);
      g.lineWidth = 1;
      g.setLineDash([4, 4]);
      g.lineDashOffset = -t * 8;
      g.beginPath();
      g.moveTo(m.cx, m.gy);
      g.lineTo(m.cx + m.cw, m.gy);
      g.stroke();
      g.restore();
      const a = 0.35 + 0.35 * Math.sin(t * 2.4);
      g.fillStyle = rgba(T.ink, a);
      g.fillRect(m.lastX, Math.round(m.lastTop) - 2, Math.max(1, Math.round(m.bw) - m.gap), 2);
      const sx = m.cx + ((t * 0.12) % 1) * m.cw;
      const grad = g.createLinearGradient(sx - 40, 0, sx, 0);
      grad.addColorStop(0, rgba(T.cyan, 0));
      grad.addColorStop(1, rgba(T.cyan, 0.08));
      g.fillStyle = grad;
      g.fillRect(sx - 40, m.cy, 40, m.base - m.cy);
    },
  };

  const LANE_COL = (T) => ({building: T.cyan, waiting: T.yellow, parked: T.violet, merged: T.green});
  DRAW.lanes = {  // 01: a force cluster per lane, its leased files around a hub, the task as a pill
    base(g, d, w, h, T, mem) {
      const C = d.clusters, col = LANE_COL(T);
      mem.c = [];
      mem.quiet = !C.length;
      if (!C.length) return waiting(g, w, h, T, "no builder lanes out");
      const n = C.length, cols = Math.max(1, Math.min(n, Math.round(Math.sqrt((n * w) / h)))), rows = Math.ceil(n / cols);
      const cw = w / cols, ch = (h - 8) / rows, r = Math.max(8, Math.min(cw * 0.36, ch * 0.3, (ch - 38) / 2, 70));  // a hub, its pill above and its state below all fit one row
      const at = C.map((c, i) => {
        const row = Math.floor(i / cols), inRow = row === rows - 1 ? n - row * cols : cols, rnd = seed(c.task || String(i));
        return {x: (w - inRow * cw) / 2 + (i - row * cols + 0.5) * cw + (rnd() - 0.5) * cw * 0.12,
          y: 6 + (row + 0.5) * ch + (rnd() - 0.5) * ch * 0.08, rnd};
      });
      // lanes holding files in one folder: a faint dashed tie between their hubs
      const dirs = C.map((c) => new Set(c.nodes.map((x) => x.d)));
      g.save();
      g.setLineDash([2, 4]);
      g.strokeStyle = rgba(T.muted, 0.22);
      g.lineWidth = 1;
      g.beginPath();
      for (let i = 0; i < n; i++) for (let j = i + 1; j < n; j++) {
        if (![...dirs[i]].some((x) => dirs[j].has(x))) continue;
        g.moveTo(at[i].x, at[i].y);
        g.lineTo(at[j].x, at[j].y);
      }
      g.stroke();
      g.restore();
      let hotLeft = 48;
      C.forEach((c, i) => {
        const {x, y, rnd} = at[i], k = (c.pc && T[c.pc]) || col[c.state] || T.cyan, N = c.nodes.length, spin = rnd() * TAU;
        const glow = g.createRadialGradient(x, y, 0, x, y, r * 1.25);
        glow.addColorStop(0, rgba(k, 0.13));
        glow.addColorStop(1, rgba(k, 0));
        g.fillStyle = glow;
        g.fillRect(x - r * 1.3, y - r * 1.3, r * 2.6, r * 2.6);
        const pts = c.nodes.map((nd, j) => {
          const a = spin + j * 2.39996, rr = r * (0.34 + 0.66 * Math.sqrt((j + 0.5) / Math.max(1, N)));
          return {x: x + Math.cos(a) * rr, y: y + Math.sin(a) * rr * 0.86, nd};
        });
        g.strokeStyle = rgba(k, 0.2);
        g.beginPath();
        for (const p of pts) { g.moveTo(x, y); g.lineTo(p.x, p.y); }
        g.stroke();
        g.strokeStyle = rgba(k, 0.42);  // files in one folder chain together
        g.beginPath();
        pts.forEach((p, j) => {
          const prev = pts.slice(0, j).reverse().find((q) => q.nd.d === p.nd.d);
          if (prev) { g.moveTo(prev.x, prev.y); g.lineTo(p.x, p.y); }
        });
        g.stroke();
        g.fillStyle = rgba(k, 0.35);
        for (let j = 0; j < Math.min(40, c.more); j++) {
          const a = rnd() * TAU, rr = r * (0.95 + rnd() * 0.3);
          g.fillRect(x + Math.cos(a) * rr, y + Math.sin(a) * rr * 0.86, 1, 1);
        }
        const hot = [];
        for (const p of pts) {
          g.beginPath();
          g.arc(p.x, p.y, p.nd.hot ? 3 : 2, 0, TAU);
          g.fillStyle = p.nd.hot ? k : rgba(k, 0.55);
          g.fill();
          if (p.nd.hot && hotLeft > 0) { hot.push({x: p.x, y: p.y, ph: rnd()}); hotLeft--; }
        }
        g.beginPath();
        g.arc(x, y, 5, 0, TAU);
        g.fillStyle = T.bg;
        g.fill();
        g.strokeStyle = k;
        g.lineWidth = 1.5;
        g.stroke();
        g.lineWidth = 1;
        g.beginPath();
        g.arc(x, y, 2, 0, TAU);
        g.fillStyle = k;
        g.fill();
        if (r >= 26) {  // the hot files by name, beside their nodes
          g.font = `400 8px ${FONT}`;
          pts.filter((p) => p.nd.hot).slice(0, 2).forEach((p) => {
            const right = p.x >= x, s = fit(g, p.nd.p, Math.max(30, cw / 2 - Math.abs(p.x - x) - 10));
            txt(g, s, p.x + (right ? 6 : -6), p.y + 3, T.muted, 8, right ? "left" : "right");
          });
        }
        g.font = `500 8.5px ${FONT}`;
        const full = (c.tag ? c.tag + " · " : "") + (c.task || "lane");  // whole words or the bare task: the colour already names the project
        pill(g, g.measureText(full).width + 12 <= cw - 6 ? full : (c.task || "lane"), x, y - r - 8, k, rgba(k, 0.12), 8.5);
        if (y + r + 16 < h) {
          g.font = `400 8.5px ${FONT}`;
          const s = c.steps.total && g.measureText(`${c.state} · ${c.steps.done}/${c.steps.total}`).width <= cw - 8 ? `${c.state} · ${c.steps.done}/${c.steps.total}` : c.state;
          txt(g, fit(g, s, cw - 8), x, y + r + 14, T.dim, 8.5, "center");
        }
        mem.c.push({x, y, k, state: c.state, hot, ph: rnd() * TAU});
      });
      mem.quiet = !mem.c.some((c) => c.hot.length || c.state === "building" || c.state === "waiting");
    },
    live(g, d, w, h, t, T, mem) {
      g.lineWidth = 1.5;
      for (const c of mem.c) {
        if (c.state === "building") {  // a working hub: a slow arc going round
          const a = t * 1.4 + c.ph;
          g.strokeStyle = rgba(c.k, 0.85);
          g.beginPath();
          g.arc(c.x, c.y, 9, a, a + 1.7);
          g.stroke();
        } else if (c.state === "waiting") {  // a waiting one breathes
          g.strokeStyle = rgba(c.k, 0.25 + 0.25 * Math.sin(t * 1.8 + c.ph));
          g.beginPath();
          g.arc(c.x, c.y, 9 + 1.5 * Math.sin(t * 1.8 + c.ph), 0, TAU);
          g.stroke();
        }
        for (const p of c.hot) {  // a write runs hub → file, then rings out
          const u = (t / 2.4 + p.ph) % 1;
          if (u < 0.6) {
            const v = u / 0.6;
            g.fillStyle = c.k;
            g.fillRect(c.x + (p.x - c.x) * v - 1.25, c.y + (p.y - c.y) * v - 1.25, 2.5, 2.5);
          } else {
            const v = (u - 0.6) / 0.4;
            g.strokeStyle = rgba(c.k, 0.8 * (1 - v));
            g.lineWidth = 1;
            g.beginPath();
            g.arc(p.x, p.y, 3 + v * 7, 0, TAU);
            g.stroke();
            g.lineWidth = 1.5;
          }
        }
      }
      g.lineWidth = 1;
    },
  };

  DRAW.train = {  // 02: a Sankey, lanes waiting → the gate → foreman/build
    base(g, d, w, h, T, mem) {
      const pad = 14, W = d.waiting, col = LANE_COL(T);
      mem.p = [];
      mem.out = null;
      mem.quiet = !W.length && !d.running;
      if (!d.known && !W.length) return waiting(g, w, h, T, "no train has run yet");
      const lw = Math.min(160, w * 0.32), fx0 = Math.round(w * 0.5), fx1 = fx0 + Math.min(40, w * 0.07);
      const fy = Math.round((h - 6) / 2), fh = Math.min(h * 0.62, 150), rx0 = fx1 + Math.max(16, (w - pad - fx1) * 0.16);
      const rw = w - pad - rx0, rowH = Math.min(30, (h - 2 * pad) / Math.max(1, W.length)), top = fy - (rowH * W.length) / 2;
      const maxF = Math.max(1, ...W.map((x) => x.files));
      W.forEach((x, i) => {
        const k = (x.pc && T[x.pc]) || col[x.state] || T.cyan, y = Math.round(top + i * rowH), bh = Math.round(rowH - 6);
        const nm = x.task, dot = x.tag ? 9 : 0;  // All mode: a dot in the project's colour, the chip bar names it
        box(g, pad, y, lw, bh, T.line, 0);
        g.fillStyle = rgba(T.ink, 0.025);
        g.fillRect(pad + 1, y + 1, lw - 2, bh - 2);
        if (dot) { g.fillStyle = k; g.beginPath(); g.arc(pad + 10, y + bh / 2, 2.5, 0, 6.2832); g.fill(); }
        txt(g, nm, pad + 7 + dot + (dot ? 3 : 0), y + bh / 2 + 3, k, 8.5, "left", 500);
        g.font = `400 8px ${FONT}`;
        const tw = g.measureText(nm + "  ").width * 1.06 + dot + (dot ? 3 : 0);
        if (bh >= 14) txt(g, fitWords(g, x.title, lw - 14 - tw, 2), pad + 7 + tw, y + bh / 2 + 3, T.dim, 8);
        g.fillStyle = k;
        g.fillRect(pad, y + bh - 2, Math.max(4, Math.round((lw * Math.min(24, x.files)) / 24)), 2);
        const ty = fy - fh * 0.4 + (W.length > 1 ? (i * fh * 0.8) / (W.length - 1) : fh * 0.4);
        const P = link(pad + lw, y + bh / 2, fx0, ty), bw = 1.5 + (6 * x.files) / maxF;
        g.strokeStyle = rgba(k, 0.26);
        g.lineWidth = bw;
        g.beginPath();
        curve(g, P);
        g.stroke();
        mem.p.push({P, k});
      });
      g.lineWidth = 1;
      if (!W.length) txt(g, "no lanes waiting", pad + lw / 2, fy + 3, T.dim, 9, "center");
      // the gate: a funnel the bands pour into
      const grad = g.createLinearGradient(fx0, 0, fx1, 0);
      grad.addColorStop(0, rgba(T.yellow, 0.24));
      grad.addColorStop(1, rgba(T.yellow, 0.08));
      g.beginPath();
      g.moveTo(fx0, fy - fh / 2);
      g.lineTo(fx1, fy - fh * 0.14);
      g.lineTo(fx1, fy + fh * 0.14);
      g.lineTo(fx0, fy + fh / 2);
      g.closePath();
      g.fillStyle = grad;
      g.fill();
      g.strokeStyle = rgba(T.yellow, 0.75);
      g.stroke();
      txt(g, d.running ? "gate · running" : "gate", (fx0 + fx1) / 2, fy - fh / 2 - 6, T.yellow, 8.5, "center", 500);
      // out: foreman/build, then what landed
      const by = fy - 17, out = link(fx1, fy, rx0, fy);
      g.strokeStyle = rgba(T.green, 0.4);
      g.lineWidth = 3;
      g.beginPath();
      curve(g, out);
      g.stroke();
      g.lineWidth = 1;
      box(g, rx0, by, rw, 34, T.green, 0.06);
      const nb = Math.min(12, d.landed24, Math.max(0, Math.floor((rw - 128) / 4)));  // bars only where the name still fits
      const room = rw - 20 - nb * 4;  // the words stop before the bars
      g.font = `500 9.5px ${FONT}`;
      txt(g, fit(g, "foreman/build", room), rx0 + 8, by + 14, T.ink, 9.5, "left", 500);
      g.font = `400 8px ${FONT}`;
      txt(g, fit(g, `${fmt(d.landed24)} landed · 24 h`, room), rx0 + 8, by + 27, T.green, 8);
      for (let i = 0; i < nb; i++) {  // a bar per lane landed today, like the reference's stacked .md
        g.fillStyle = rgba(T.green, 0.35 + (0.5 * i) / 12);
        g.fillRect(rx0 + rw - 8 - (nb - i) * 4, by + 8, 3, 18);
      }
      const L = d.landed.slice().reverse();
      for (let i = 0, y = by + 52; i < L.length && y < h - 6; i++, y += 15) {
        const lk = (L[i].pc && T[L[i].pc]) || T.green;
        txt(g, "✓", rx0 + 2, y, lk, 9);
        txt(g, L[i].task, rx0 + 16, y, L[i].pc ? lk : T.muted, 8.5);  // the task id always shows; the colour names the project
        txt(g, hm(L[i].at), rx0 + rw, y, T.dim, 8, "right");
      }
      if (!L.length) txt(g, "nothing landed yet", rx0 + 2, by + 52, T.dim, 8.5);
      mem.out = out;
      mem.v = d.running ? 0.3 : 0.12;
    },
    live(g, d, w, h, t, T, mem) {  // square packets ride the bands; faster while the train runs
      mem.p.forEach((x, i) => {
        g.fillStyle = x.k;
        for (let j = 0; j < 2; j++) {
          const [px, py] = along(x.P, (t * mem.v + i * 0.37 + j * 0.5) % 1);
          g.fillRect(px - 1.5, py - 1.5, 3, 3);
        }
      });
      if (mem.out && (mem.p.length || d.running)) {
        g.fillStyle = T.green;
        for (let j = 0; j < 3; j++) {
          const [px, py] = along(mem.out, (t * mem.v * 1.6 + j / 3) % 1);
          g.fillRect(px - 1.5, py - 1.5, 3, 3);
        }
      }
    },
  };

  function chevron(g, x, y, s) {
    g.moveTo(x - s / 2, y - s);
    g.lineTo(x + s / 2, y);
    g.lineTo(x - s / 2, y + s);
  }
  function loopAll(g, d, w, h, T, mem) {  // T-3730: a words >>> brief row per project, each in its own colour, over a mini stage track
    const pad = 12, n = d.loops.length, rh = Math.min(150, (h - 2 * pad + 8) / n), bh = rh - 8, lab = bh >= 62, lw = Math.round(w * 0.36), cx = pad + lw + 34;
    d.loops.forEach((l, i) => {
      const k = T[l.pc] || T.pink, y = Math.round(pad + i * rh);
      box(g, pad, y, lw, bh, k, 0.03);
      g.fillStyle = rgba(k, 0.14);
      g.fillRect(pad + 1, y + 1, lw - 2, 12);
      txt(g, fit(g, `${l.tag} · YOU ASKED`, lw - 14), pad + 6, y + 10, k, 7, "left", 600);
      g.font = `400 8.5px ${FONT}`;
      hatch(g, pad + 1, y + 13, 8, bh - 14, rgba(T.line, 1));  // the page's gutter, as on the single-project board
      const q = "“" + l.words + "”", ql = Math.max(1, Math.floor((bh - 20) / 11));
      (ql === 1 ? [fitWords(g, q, lw - 22) || fit(g, q, lw - 22)] : wrap(g, q, lw - 22, ql)).forEach((s, j) => txt(g, s, pad + 15, y + 25 + j * 11, T.muted, 8.5));
      mem.rows.push({x: pad + lw + 8, y: y + bh / 2});
      const cw = w - pad - cx;
      box(g, cx, y, cw, bh, k, 0.05);
      g.font = `500 8.5px ${FONT}`;
      txt(g, fit(g, `${l.id}.md`, cw * 0.5), cx + 6, y + 11, k, 8.5, "left", 500);
      const meta = [l.of ? `${l.ok}/${l.of} met` : "", l.steps ? `${l.done}/${l.steps} steps` : ""].filter(Boolean).join(" · ");
      g.font = `400 7.5px ${FONT}`;
      if (meta) txt(g, fit(g, meta, cw * 0.45), cx + cw - 6, y + 11, T.dim, 7.5, "right");
      g.font = `500 9px ${FONT}`;
      const tiny = bh < 46, ty0 = tiny ? 22 : 25;  // many projects: one title line, the stage bar tucked under it
      const tl = tiny ? [fitWords(g, l.title, cw - 12) || fit(g, l.title, cw - 12)] : wrap(g, l.title, cw - 12, Math.min(2, Math.max(1, Math.floor((bh - 30 - (lab ? 12 : 0)) / 12))));
      tl.forEach((s, j) => txt(g, s, cx + 6, y + ty0 + j * 12, T.ink, 9, "left", 500));
      let sy = y + 25 + tl.length * 12 + 6;
      const cur = Math.max(0, l.list.findIndex((s) => s.current));  // room under the title: its steps, as the single board lists them
      for (let j = Math.max(0, Math.min(cur - 1, l.list.length - 4)); j < l.list.length && sy < y + bh - (lab ? 26 : 12); j++, sy += 11) {
        const s = l.list[j];
        g.font = `400 8px ${FONT}`;
        txt(g, fit(g, `${s.current ? "▸" : s.done ? "✓" : "·"} ${j + 1}. ${s.text}`, cw - 14), cx + 8, sy, s.current ? T.pink : s.done ? T.dim : T.muted, 8);
      }
      const si = STAGES.indexOf(l.stage), sw = (cw - 12) / STAGES.length;  // the stage track, this project's task lit
      STAGES.forEach((s, j) => {
        g.fillStyle = j < si ? rgba(k, 0.45) : j === si ? k : T.grid;
        g.fillRect(Math.round(cx + 6 + j * sw), y + bh - (lab ? 21 : 6), Math.max(2, Math.round(sw) - 2), 3);
        if (lab && (j === si || sw >= 52)) {  // room: name the stages, as the single-project track does
          g.font = `400 7.5px ${FONT}`;
          const x0 = Math.round(cx + 6 + j * sw), tw = g.measureText(s).width;
          if (j === si) txt(g, s, Math.min(x0, cx + cw - 6 - tw), y + bh - 8, T.ink, 7.5);
          else if (tw <= sw - 3) txt(g, s, x0, y + bh - 8, T.dim, 7.5);
        }
      });
    });
  }
  DRAW.loop = {  // 04: the words you typed, >>> into the brief they became, over the stage track
    base(g, d, w, h, T, mem) {
      mem.chev = null;
      mem.caret = null;
      mem.seg = null;
      mem.rows = [];
      if (d.all) return loopAll(g, d, w, h, T, mem);
      const pad = 14, wide = w >= 420, bot = h - 34;
      let cx = pad;
      if (wide) {  // "you asked": a page with a hatched gutter, the words quoted
        const bw = Math.round(w * 0.34), bh = bot - pad;
        box(g, pad, pad, bw, bh, T.line, 0);
        g.fillStyle = rgba(T.pink, 0.14);
        g.fillRect(pad + 1, pad + 1, bw - 2, 15);
        txt(g, "YOU ASKED", pad + 8, pad + 11, T.pink, 7.5, "left", 600);
        hatch(g, pad + 1, pad + 16, 8, bh - 17, rgba(T.line, 1));
        g.font = `400 9px ${FONT}`;
        const lines = wrap(g, "“" + d.words + "”", bw - 26, Math.floor((bh - 30) / 13));
        lines.forEach((s, i) => txt(g, s, pad + 16, pad + 32 + i * 13, T.muted, 9));
        mem.chev = {x: pad + bw + 12, y: pad + bh / 2};
        cx = pad + bw + 40;
      }
      const cw = w - pad - cx, ch = bot - pad;
      box(g, cx, pad, cw, ch, T.pink, 0.035);
      g.font = `500 9px ${FONT}`;
      txt(g, `${d.id || "task"}.md`, cx + 8, pad + 14, T.pink, 9, "left", 500);
      if (d.choice != null) txt(g, `choice ${d.choice}%`, cx + cw - 8, pad + 14, T.dim, 8, "right");
      hline(g, cx + 1, cx + cw - 1, pad + 21, rgba(T.pink, 0.35));
      let y = pad + 36;
      if (!wide && d.words && d.words !== d.title) {
        g.font = `400 8.5px ${FONT}`;
        txt(g, fit(g, "“" + d.words + "”", cw - 16), cx + 8, y, T.muted, 8.5);
        y += 15;
      }
      g.font = `500 10px ${FONT}`;
      for (const s of wrap(g, "# " + d.title, cw - 16, 2)) {
        txt(g, s, cx + 8, y, T.ink, 10, "left", 500);
        y += 14;
      }
      y += 3;
      const room = Math.max(0, Math.floor((pad + ch - 6 - y) / 12)), C = d.criteria, S = d.steps;
      let nc = C.length, ns = S.length;
      if (nc + ns + (nc && ns ? 1 : 0) > room) {
        nc = Math.min(nc, ns ? Math.ceil(room / 2) : room);
        ns = Math.max(0, room - nc - (nc ? 1 : 0));
      }
      g.font = `400 8.5px ${FONT}`;
      C.slice(0, nc).forEach((c) => {
        txt(g, fit(g, `${c.ok ? "[x]" : "[ ]"} ${c.text}`, cw - 16), cx + 8, y, c.ok ? T.green : T.dim, 8.5);
        y += 12;
      });
      if (nc && ns) y += 12;
      const cur = Math.max(0, S.findIndex((s) => s.current)), s0 = Math.max(0, Math.min(cur - 1, S.length - ns));
      S.slice(s0, s0 + ns).forEach((s, i) => {
        const col = s.current ? T.ink : s.done ? T.dim : T.muted, mark = s.current ? "▸" : s.done ? "✓" : " ";
        g.font = `400 8.5px ${FONT}`;
        const line = fit(g, `${mark} ${s0 + i + 1}. ${s.text}`, cw - 26);
        txt(g, line, cx + 8, y, s.current ? T.pink : col, 8.5);
        if (s.current) mem.caret = {x: cx + 10 + g.measureText(line).width, y: y - 8};
        y += 12;
      });
      // the stage track: eight steps of the loop, the one this task stands at lit
      const si = STAGES.indexOf(d.stage), sw = (w - 2 * pad) / STAGES.length, ty = h - 24;
      STAGES.forEach((s, i) => {
        const x = pad + i * sw;
        g.fillStyle = i < si ? rgba(T.pink, 0.45) : i === si ? T.pink : T.grid;
        g.fillRect(Math.round(x), ty, Math.round(sw) - 2, 4);
        if (sw >= 58 || i === si) {
          g.font = `400 8px ${FONT}`;
          const lab = fit(g, s, sw >= 58 ? sw - 4 : 120), lx = sw >= 58 ? x : Math.min(Math.max(x, pad), w - pad - g.measureText(lab).width);
          txt(g, lab, lx, ty + 15, i === si ? T.ink : T.dim, 8);
        }
      });
      mem.seg = si >= 0 ? {x: pad + si * sw, y: ty, w: sw - 2} : null;
    },
    live(g, d, w, h, t, T, mem) {
      if (d.all) {  // each row's >>> pulses in its project's colour
        g.lineWidth = 1.6;
        (mem.rows || []).forEach((r, i) => {
          for (let k = 0; k < 2; k++) {
            g.strokeStyle = rgba(T[d.loops[i].pc] || T.pink, 0.2 + 0.8 * Math.max(0, Math.sin(t * 3 - k * 0.9 - i * 0.7)));
            g.beginPath();
            chevron(g, r.x + k * 7, r.y, 4);
            g.stroke();
          }
        });
        g.lineWidth = 1;
        return;
      }
      if (mem.chev) {  // >>> pulses left to right, the words flowing in
        g.lineWidth = 1.6;
        for (let k = 0; k < 3; k++) {
          g.strokeStyle = rgba(T.pink, 0.2 + 0.8 * Math.max(0, Math.sin(t * 3 - k * 0.9)));
          g.beginPath();
          chevron(g, mem.chev.x + k * 7, mem.chev.y, 4.5);
          g.stroke();
        }
        g.lineWidth = 1;
      }
      if (mem.caret && (t * 1.6) % 1 < 0.55) {
        g.fillStyle = T.pink;
        g.fillRect(mem.caret.x, mem.caret.y, 5, 9);
      }
      if (mem.seg) {
        const s = mem.seg, x = s.x + ((t * 0.45) % 1) * s.w, gr = g.createLinearGradient(x - 18, 0, x, 0);
        gr.addColorStop(0, rgba(T.ink, 0));
        gr.addColorStop(1, rgba(T.ink, 0.7));
        g.fillStyle = gr;
        g.fillRect(Math.max(s.x, x - 18), s.y, Math.min(18, x - s.x), 4);
      }
    },
  };

  const SPHERE = (() => {  // GLOBE points spread evenly on a unit sphere (a Fibonacci lattice), poles first
    const N = GLOBE, P = new Float32Array(N * 3);
    for (let i = 0; i < N; i++) {
      const y = 1 - (2 * (i + 0.5)) / N, r = Math.sqrt(1 - y * y), a = i * 2.39996323;
      P[i * 3] = Math.cos(a) * r;
      P[i * 3 + 1] = y;
      P[i * 3 + 2] = Math.sin(a) * r;
    }
    return P;
  })();
  const TILT = 0.42;
  DRAW.library = {  // 05: a dotted globe, a dot per kept card or moon (≤ GLOBE, then one per several), over a kept-per-hour histogram
    base(g, d, w, h, T, mem) {
      const pad = 14, col = {cards: T.cyan, ui: T.yellow, moons: T.violet, calling: T.pink};
      const hy1 = h - 22, hy0 = hy1 - Math.max(34, Math.round(h * 0.22));
      const R = Math.max(20, Math.min((hy0 - pad - 14) / 2, w * 0.3)), gy = pad + 4 + R;
      const gx = Math.min(w - pad - R - 4, Math.max(w / 2, pad + 120 + R));
      mem.geo = {gx, gy, R};
      const halo = g.createRadialGradient(gx, gy, R * 0.6, gx, gy, R * 1.35);
      halo.addColorStop(0, rgba(T.cyan, 0.07));
      halo.addColorStop(1, rgba(T.cyan, 0));
      g.fillStyle = halo;
      g.fillRect(gx - R * 1.4, gy - R * 1.4, R * 2.8, R * 2.8);
      g.strokeStyle = rgba(T.cyan, 0.5);
      g.beginPath();
      g.arc(gx, gy, R, 0, TAU);
      g.stroke();
      g.strokeStyle = rgba(T.cyan, 0.24);  // parallels every 30°
      for (const lat of [-60, -30, 0, 30, 60]) {
        const f = (lat * Math.PI) / 180;
        g.beginPath();
        g.ellipse(gx, gy - R * Math.sin(f) * Math.cos(TILT), R * Math.cos(f), R * Math.cos(f) * Math.sin(TILT), 0, 0, TAU);
        g.stroke();
      }
      // each grid gets a band of the lattice; the dots it doesn't fill stay faint
      let a = 0;
      mem.ranges = [];
      mem.fresh = [];
      let freshLeft = 40;
      for (const k of LIB_GRIDS) {
        const n = Math.min(GLOBE - a, d.dots[k]);
        if (!n) continue;
        mem.ranges.push({a, b: a + n, col: col[k]});
        const f = Math.min(freshLeft, d.grew[k], n);
        if (f) mem.fresh.push({a: a + n - f, b: a + n, col: col[k]});
        freshLeft -= f;
        a += n;
      }
      if (a < GLOBE) mem.ranges.push({a, b: GLOBE, col: T.line, faint: true});
      // the legend, top left
      LIB_GRIDS.forEach((k, i) => {
        const y = pad + 8 + i * 15;
        g.fillStyle = col[k];
        g.fillRect(pad, y - 6, 6, 6);
        txt(g, k, pad + 12, y, T.muted, 8.5);
        txt(g, fmt(d.counts[k]), pad + 96, y, T.ink, 8.5, "right", 500);
        if (d.grew[k]) txt(g, `+${fmt(d.grew[k])}`, pad + 102, y, T.green, 8);
      });
      // kept per hour, the last 24
      const peak = Math.max(1, ...d.hourly), bw = (w - 2 * pad) / 24;
      hline(g, pad, w - pad, hy1, T.line);
      d.hourly.forEach((v, i) => {
        const bh = Math.round(((hy1 - hy0 - 10) * v) / peak);
        if (!bh) return;
        g.fillStyle = i === 23 ? T.ink : rgba(T.cyan, 0.4 + (0.45 * i) / 23);
        g.fillRect(Math.round(pad + i * bw + 1), hy1 - bh, Math.max(1, Math.round(bw) - 2), bh);
      });
      txt(g, `kept / hour · peak ${fmt(peak)}`, pad, hy0 + 2, T.dim, 8);
      txt(g, "−24 h", pad, h - 9, T.dim, 8);
      txt(g, "now", w - pad, h - 9, T.dim, 8, "right");
      if (!d.total) txt(g, "nothing kept yet", gx, gy + 3, T.dim, 9, "center");
      mem.px = mem.px || new Float32Array(GLOBE);
      mem.py = mem.py || new Float32Array(GLOBE);
      mem.pz = mem.pz || new Float32Array(GLOBE);
    },
    live(g, d, w, h, t, T, mem) {
      const {gx, gy, R} = mem.geo, th = t * 0.16, c = Math.cos(th), s = Math.sin(th), ct = Math.cos(TILT), st = Math.sin(TILT);
      // meridians every 30°, their front halves
      g.strokeStyle = rgba(T.cyan, 0.3);
      g.beginPath();
      for (let m = 0; m < 12; m++) {
        const a = (m * Math.PI) / 6 + th;
        let pen = false;
        for (let j = 0; j <= 24; j++) {
          const f = -Math.PI / 2 + (j * Math.PI) / 24, y = Math.sin(f), r = Math.cos(f);
          const z = r * Math.cos(a), dep = y * st + z * ct;
          if (dep < -0.02) { pen = false; continue; }
          const X = gx + R * r * Math.sin(a), Y = gy - R * (y * ct - z * st);
          if (pen) g.lineTo(X, Y);
          else g.moveTo(X, Y);
          pen = true;
        }
      }
      g.stroke();
      const P = SPHERE, px = mem.px, py = mem.py, pz = mem.pz;
      for (let i = 0; i < GLOBE; i++) {
        const x = P[i * 3], y = P[i * 3 + 1], z = P[i * 3 + 2], x2 = x * c - z * s, z2 = x * s + z * c;
        px[i] = gx + R * x2;
        py[i] = gy - R * (y * ct - z2 * st);
        pz[i] = y * st + z2 * ct;
      }
      // three depth bands per colour: one fill each, the back dimmer and smaller
      for (const rg of mem.ranges) {
        for (let b = 0; b < 3; b++) {
          const lo = [-0.1, 0.3, 0.65][b], hi = [0.3, 0.65, 2][b], sz = [1.3, 1.9, 2.5][b];
          g.fillStyle = rgba(rg.col, rg.faint ? [0.25, 0.45, 0.6][b] : [0.3, 0.62, 1][b]);
          g.beginPath();
          for (let i = rg.a; i < rg.b; i++) if (pz[i] >= lo && pz[i] < hi) g.rect(px[i] - sz / 2, py[i] - sz / 2, sz, sz);
          g.fill();
        }
      }
      const pulse = 0.5 + 0.5 * Math.sin(t * 3);
      for (const f of mem.fresh) {  // the cards kept since the last snapshot, lit
        g.fillStyle = rgba(f.col, 0.35 + 0.5 * pulse);
        g.beginPath();
        for (let i = f.a; i < f.b; i++) if (pz[i] > -0.1) g.arc(px[i], py[i], 2 + pulse, 0, TAU), g.closePath();
        g.fill();
      }
    },
  };

  const GROUP_COL = (T) => ({claude: T.orange, jeff: T.violet, ollama: T.cyan});
  DRAW.models = {  // 06: agents → the models they call, each line as thick as its calls; the GPU underneath
    base(g, d, w, h, T, mem) {
      const pad = 14, gc = GROUP_COL(T), y1 = h - pad - (d.gpu ? 28 : 0);
      mem.L = [];
      mem.quiet = !d.links.length;
      if (d.gpu) {
        const gy = h - pad - 14, half = (w - 2 * pad - 70) / 2;
        txt(g, "GPU", pad, gy + 10, T.orange, 8.5, "left", 600);
        [["util", d.gpu.util / 100, `${d.gpu.util}%`], ["vram", d.gpu.total ? d.gpu.used / d.gpu.total : 0,
          `${(d.gpu.used / 1024).toFixed(1)}/${(d.gpu.total / 1024).toFixed(1)} GB`]].forEach(([k, f, s], i) => {
          const x = pad + 34 + i * (half + 18);
          txt(g, k, x, gy + 4, T.dim, 7.5);
          txt(g, s, x + half, gy + 4, T.muted, 7.5, "right");
          g.fillStyle = T.grid;
          g.fillRect(x, gy + 8, half, 3);
          g.fillStyle = rgba(T.orange, 0.85);
          g.fillRect(x, gy + 8, Math.round(half * Math.min(1, f)), 3);
        });
      }
      if (!d.models.length) return waiting(g, w, y1 + pad / 2, T, "no model calls in the last minutes");
      const aw = Math.min(130, w * 0.27), mw = Math.min(160, w * 0.32), mx = w - pad - mw;
      const nA = Math.max(1, d.agents.length), ah = Math.min(26, (y1 - pad) / nA), aTop = pad + (y1 - pad - ah * nA) / 2;
      const per = d.agents.map((_, i) => d.links.filter((l) => l.a === i).reduce((a, l) => a + l.n, 0)), maxA = Math.max(1, ...per);
      const tint = [T.cyan, T.green, T.yellow, T.pink, T.violet, T.orange];  // each caller its own colour, like a source
      const A = d.agents.map((a, i) => {
        const y = Math.round(aTop + i * ah), bh = Math.round(ah - 6), k = tint[i % tint.length];
        box(g, pad, y, aw, bh, k, 0.08);
        g.font = `400 8.5px ${FONT}`;
        txt(g, fit(g, a.name, aw - 12), pad + 6, y + bh / 2 + 3, T.ink, 8.5);
        g.fillStyle = rgba(k, 0.85);
        g.fillRect(pad, y + bh - 2, Math.max(3, Math.round((aw * per[i]) / maxA)), 2);
        return y + bh / 2;
      });
      const groups = GROUPS.filter((k) => d.models.some((m) => m.group === k));
      const mh = Math.min(24, (y1 - pad) / (d.models.length + groups.length * 0.6));
      let y = pad + (y1 - pad - mh * (d.models.length + groups.length * 0.6)) / 2;
      const M = [];
      for (const k of groups) {
        txt(g, k.toUpperCase(), mx, y + mh * 0.6 - 3, gc[k], 7.5, "left", 600);
        y += mh * 0.6;
        d.models.forEach((m, i) => {
          if (m.group !== k) return;
          const bh = Math.round(mh - 5), yy = Math.round(y);
          if (m.loaded) box(g, mx, yy, mw, bh, gc[k], 0.06);
          else {
            g.save();
            g.setLineDash([3, 3]);
            g.strokeStyle = rgba(gc[k], 0.7);
            g.strokeRect(mx + 0.5, yy + 0.5, mw - 1, bh - 1);
            g.restore();
          }
          g.font = `400 8.5px ${FONT}`;
          txt(g, fit(g, m.name, mw - 44), mx + 6, yy + bh / 2 + 3, T.ink, 8.5);
          txt(g, fmt(m.calls), mx + mw - 6, yy + bh / 2 + 3, T.dim, 8, "right");
          M[i] = {y: yy + bh / 2, k: gc[k]};
          y += mh;
        });
      }
      const maxN = Math.max(1, ...d.links.map((l) => l.n));
      for (const l of d.links) {
        if (A[l.a] == null || !M[l.m]) continue;
        const P = link(pad + aw, A[l.a], mx, M[l.m].y), f = Math.log(1 + l.n) / Math.log(1 + maxN);
        g.strokeStyle = rgba(M[l.m].k, 0.4);
        g.lineWidth = 1 + 3.5 * f;
        g.beginPath();
        curve(g, P);
        g.stroke();
        mem.L.push({P, k: M[l.m].k, n: Math.min(3, 1 + Math.floor(Math.log2(1 + l.n) / 2)), v: 0.16 + 0.14 * f, ph: seed(l.a + "," + l.m)()});
      }
      g.lineWidth = 1;
    },
    live(g, d, w, h, t, T, mem) {
      for (const l of mem.L) {
        g.fillStyle = l.k;
        for (let j = 0; j < l.n; j++) {
          const [x, y] = along(l.P, (t * l.v + l.ph + j / l.n) % 1);
          g.fillRect(x - 1.5, y - 1.5, 3, 3);
        }
      }
    },
  };

  const STATUS_COL = (T) => ({active: T.green, open: T.cyan, ready: T.yellow, blocked: T.pink, parked: T.violet});
  DRAW.sampo = {  // 07: a terminal typing the sampo commands run here, the queue with status pills, depends_on
    base(g, d, w, h, T, mem) {
      const pad = 14, th = 26, tw = w - 2 * pad, sc = STATUS_COL(T);
      box(g, pad, pad - 4, tw, th, T.line, 0);
      g.fillStyle = rgba(T.ink, 0.025);
      g.fillRect(pad + 1, pad - 3, tw - 2, th - 2);
      txt(g, "$", pad + 8, pad + 13, T.cyan, 10, "left", 600);
      txt(g, "sampo cli", pad + tw - 8, pad + 13, T.dim, 7.5, "right");
      g.font = `400 9px ${FONT}`;
      mem.cmds = d.commands.slice(-5).map((c) => ({s: fit(g, c.cmd, tw - 150), ok: c.ok, at: hm(c.at)}));
      mem.term = {x: pad + 22, y: pad + 13};
      if (!mem.cmds.length) txt(g, "waiting for a sampo command", pad + 22, pad + 13, T.dim, 9);
      // the queue as a table
      const qy = pad + th + 6, deps = w >= 430, qw = deps ? Math.round((w - 2 * pad) * 0.6) : w - 2 * pad, qh = h - pad - qy;
      box(g, pad, qy, qw, qh, T.line, 0);
      g.fillStyle = rgba(T.violet, 0.12);
      g.fillRect(pad + 1, qy + 1, qw - 2, 15);
      const cId = pad + 8, cSt = pad + 62, cTier = pad + 116, cTi = pad + 146;
      [["ID", cId], ["STATUS", cSt], ["TIER", cTier], ["TITLE", cTi]].forEach(([s, x]) => txt(g, s, x, qy + 11, T.violet, 7, "left", 600));
      const rows = Math.max(0, Math.floor((qh - 20) / 15));
      d.queue.slice(0, rows).forEach((q, i) => {
        const y = qy + 27 + i * 15, k = sc[q.status] || T.muted;
        if (i % 2) { g.fillStyle = rgba(T.ink, 0.02); g.fillRect(pad + 1, y - 9, qw - 2, 15); }
        const pk = q.pc && T[q.pc];
        txt(g, q.id, cId, y + 2, pk || T.ink, 8.5);
        pill(g, q.status || "—", cSt, y - 1, k, rgba(k, 0.12), 7, "left");
        txt(g, q.tier, cTier, y + 2, T.dim, 8.5);
        g.font = `400 8.5px ${FONT}`;
        const tg = q.tag ? q.tag + " " : "", tgw = tg ? g.measureText(tg).width + 2 : 0;
        if (tg) txt(g, tg, cTi, y + 2, pk, 8.5, "left", 500);
        txt(g, fit(g, q.title, pad + qw - 8 - cTi - tgw), cTi + tgw, y + 2, T.muted, 8.5);
      });
      if (!d.queue.length) txt(g, "the queue is empty", pad + qw / 2, qy + qh / 2 + 6, T.dim, 9, "center");
      mem.E = [];
      if (deps) {  // depends_on: prerequisites left, the tasks waiting on them right
        const dx = pad + qw + 10, dw = w - pad - dx;
        box(g, dx, qy, dw, qh, T.line, 0);
        g.fillStyle = rgba(T.violet, 0.12);
        g.fillRect(dx + 1, qy + 1, dw - 2, 15);
        txt(g, "DEPENDS_ON", dx + 8, qy + 11, T.violet, 7, "left", 600);
        const depth = new Map(), on = new Map();
        for (const [id, ...pre] of d.deps) {
          on.set(id, (on.get(id) || []).concat(pre));
          for (const x of pre) if (!depth.has(x)) depth.set(x, 0);
          depth.set(id, 1);
        }
        const top = Math.min(3, Math.max(1, Math.floor((dw - 12) / 58) - 1));  // as many columns as pills fit side by side
        for (let it = 0; it < 4; it++) for (const [id, pre] of on) depth.set(id, Math.min(top, 1 + Math.max(...pre.map((x) => depth.get(x) || 0))));
        const levels = Math.max(...depth.values(), 0) + 1, col = [];
        for (const [id, k] of depth) (col[k] = col[k] || []).push(id);
        const lx = (k) => dx + 12 + ((dw - 70) * k) / Math.max(1, levels - 1), room = Math.max(1, Math.floor((qh - 24) / 17));
        const pos = new Map();
        col.forEach((ids, k) => (ids || []).slice(0, room).forEach((id, i) => pos.set(id, {x: lx(k), y: qy + 28 + i * 17 + ((room - Math.min(room, ids.length)) * 17) / 2})));
        g.strokeStyle = rgba(T.violet, 0.45);
        for (const [id, pre] of on) for (const x of pre) {
          const a = pos.get(x), b = pos.get(id);
          if (!a || !b) continue;
          const P = link(a.x + 50, a.y, b.x, b.y);
          g.beginPath();
          curve(g, P);
          g.stroke();
          if (mem.E.length < 16) mem.E.push(P);
        }
        for (const [id, p] of pos) pill(g, id, p.x, p.y, on.has(id) ? T.violet : T.muted, T.bg, 7.5, "left");
        if (!pos.size) txt(g, "no depends_on links", dx + dw / 2, qy + qh / 2 + 6, T.dim, 8.5, "center");
      }
      mem.quiet = !mem.cmds.length && !mem.E.length;
    },
    live(g, d, w, h, t, T, mem) {
      const C = mem.cmds;
      if (C.length) {  // type each of the last five commands, 5.5 s apiece, then show how it went
        const i = Math.floor(t / 5.5) % C.length, c = C[i], u = t % 5.5, n = Math.min(c.s.length, Math.floor(u * 40));
        const s = c.s.slice(0, n), {x, y} = mem.term;
        txt(g, s, x, y, T.ink, 9);
        const cx = x + g.measureText(s).width + 2;
        if (n < c.s.length || (t * 2) % 1 < 0.6) { g.fillStyle = T.cyan; g.fillRect(cx, y - 8, 5, 10); }
        if (n === c.s.length) txt(g, `${c.ok ? "✓" : "✗"} ${c.at}`, cx + 12, y, c.ok ? T.green : T.red, 8.5);
      }
      g.fillStyle = T.violet;
      mem.E.forEach((P, i) => {
        const [x, y] = along(P, (t * 0.35 + i * 0.21) % 1);
        g.fillRect(x - 1.25, y - 1.25, 2.5, 2.5);
      });
    },
  };

  DRAW.state = {  // 08: the state folder as a tree, the newest entry lit; the latest decision as a card
    base(g, d, w, h, T, mem) {
      const pad = 14, wide = w >= 440;
      mem.nb = null;
      mem.caret = null;
      if (!d.known) { mem.quiet = true; return waiting(g, w, h, T, "the state folder isn't read yet"); }
      const tw = wide ? Math.round(w * 0.52) - pad : w - 2 * pad, th = wide ? h - 2 * pad : h - 2 * pad - 88;
      box(g, pad, pad, tw, th, T.line, 0);
      txt(g, "state/", pad + 8, pad + 14, T.yellow, 9, "left", 500);
      const rows = Math.max(0, Math.floor((th - 26) / 15)), E = d.tree.slice(0, rows);
      if (E.length) {
        g.fillStyle = T.line;
        g.fillRect(pad + 12, pad + 19, 1, 15 * (E.length - 1) + 12);
      }
      E.forEach((e, i) => {
        const y = pad + 34 + i * 15, newest = e.name === d.newest;
        if (newest) {
          g.fillStyle = rgba(T.pink, 0.12);
          g.fillRect(pad + 1, y - 10, tw - 2, 14);
          mem.nb = {x: pad + 1, y: y - 10, w: tw - 2, h: 14};
        }
        g.fillStyle = T.line;
        g.fillRect(pad + 12, y - 3, 6, 1);
        const right = hm(e.at), meta = e.dir ? String(e.n) : e.kb ? `${e.kb} kb` : "";
        let rx = pad + tw - 8;
        txt(g, right, rx, y, T.dim, 8, "right");
        g.font = `400 8px ${FONT}`;
        rx -= g.measureText(right).width + 8;
        if (newest) rx -= pill(g, "newest", rx, y - 3, T.pink, rgba(T.pink, 0.14), 7, "right") + 6;
        if (meta) { txt(g, meta, rx, y, T.dim, 8, "right"); rx -= g.measureText(meta).width + 8; }
        g.font = `${e.dir ? 500 : 400} 8.5px ${FONT}`;
        txt(g, fit(g, e.name + (e.dir ? "/" : ""), rx - pad - 24), pad + 22, y, newest ? T.pink : e.dir ? T.ink : T.muted, 8.5, "left", e.dir ? 500 : 400);
      });
      if (!E.length) txt(g, "empty", pad + tw / 2, pad + th / 2, T.dim, 9, "center");
      // the latest decision, as the doc it lives in
      const dx = wide ? pad + tw + 12 : pad, dy = wide ? pad : pad + th + 10, dw = wide ? w - pad - dx : tw, dh = wide ? th : 78;
      box(g, dx, dy, dw, dh, T.pink, 0.035);
      const doc = d.doc;
      if (!doc) {
        txt(g, "no decision recorded yet", dx + dw / 2, dy + dh / 2 + 3, T.dim, 8.5, "center");
      } else {
        txt(g, `${doc.kind || "note"}s.md`, dx + 8, dy + 14, T.pink, 9, "left", 500);
        txt(g, doc.at.slice(0, 10), dx + dw - 8, dy + 14, T.dim, 8, "right");
        hline(g, dx + 1, dx + dw - 1, dy + 20, rgba(T.pink, 0.35));
        let y = dy + 34;
        g.font = `500 9.5px ${FONT}`;
        for (const s of wrap(g, "# " + doc.title, dw - 16, wide ? 2 : 1)) { txt(g, s, dx + 8, y, T.ink, 9.5, "left", 500); y += 13; }
        g.font = `400 8.5px ${FONT}`;
        const lines = wrap(g, doc.text, dw - 22, Math.floor((dy + dh - 4 - y) / 12) + 1);
        lines.forEach((s) => { txt(g, s, dx + 8, y + 1, T.muted, 8.5); y += 12; });
        const last = lines[lines.length - 1];
        if (last != null) mem.caret = {x: dx + 10 + g.measureText(last).width, y: y - 19};
      }
      mem.quiet = !mem.nb && !mem.caret;
    },
    live(g, d, w, h, t, T, mem) {
      const b = mem.nb;
      if (b) {  // the newest entry: its bar pulses, a shimmer passes along it
        g.fillStyle = rgba(T.pink, 0.55 + 0.45 * Math.sin(t * 2.2));
        g.fillRect(b.x, b.y, 2, b.h);
        const x = b.x + ((t * 0.22) % 1) * (b.w + 80) - 40, gr = g.createLinearGradient(x - 40, 0, x + 40, 0);
        gr.addColorStop(0, rgba(T.pink, 0));
        gr.addColorStop(0.5, rgba(T.pink, 0.12));
        gr.addColorStop(1, rgba(T.pink, 0));
        g.fillStyle = gr;
        g.fillRect(Math.max(b.x, x - 40), b.y, Math.min(80, b.x + b.w - Math.max(b.x, x - 40)), b.h);
      }
      if (mem.caret && (t * 1.6) % 1 < 0.55) {
        g.fillStyle = T.pink;
        g.fillRect(mem.caret.x, mem.caret.y, 5, 9);
      }
    },
  };

  DRAW.tests = {  // 09: a cell per test module, coloured by how it stands
    base(g, d, w, h, T, mem) {
      const pad = 14, col = {cached: rgba(T.green, 0.34), pass: rgba(T.green, 0.85), running: T.yellow, fail: T.red, idle: T.line};
      mem.run = [];
      mem.quiet = true;
      if (!d.known && !d.cells.length) return waiting(g, w, h, T, "no test run seen yet");
      let lx = pad;
      for (const k of TEST_STATES) {
        if (!d.count[k]) continue;
        const s = `${k} ${fmt(d.count[k])}`;
        if (k === "idle") { g.strokeStyle = T.line; g.strokeRect(lx + 0.5, pad - 1.5, 6, 6); }
        else { g.fillStyle = col[k]; g.fillRect(lx, pad - 2, 7, 7); }
        txt(g, s, lx + 11, pad + 5, T.muted, 8.5);
        g.font = `400 8.5px ${FONT}`;
        lx += g.measureText(s).width + 24;
      }
      const x0 = pad, y0 = pad + 16, gw = w - 2 * pad, gh = h - y0 - 26, n = d.cells.length + (d.more ? 1 : 0);
      let s = 26, gap = 2, cols = 1;
      for (; s > 3; s--) {
        gap = s >= 10 ? 2 : 1;
        cols = Math.max(1, Math.floor((gw + gap) / (s + gap)));
        if (Math.ceil(n / cols) * (s + gap) - gap <= gh) break;
      }
      d.cells.forEach((c, i) => {
        const x = x0 + (i % cols) * (s + gap), y = y0 + Math.floor(i / cols) * (s + gap);
        if (c.s === "idle") { g.strokeStyle = T.line; g.strokeRect(x + 0.5, y + 0.5, s - 1, s - 1); }
        else { g.fillStyle = col[c.s]; g.fillRect(x, y, s, s); }
        if (c.s === "running" && mem.run.length < 64) mem.run.push({x, y, s});
      });
      if (d.more) {
        const i = d.cells.length, x = x0 + (i % cols) * (s + gap), y = y0 + Math.floor(i / cols) * (s + gap);
        txt(g, `+${fmt(d.more)}`, x + 2, y + s / 2 + 3, T.dim, 8);
      }
      const run = d.cells.filter((c) => c.s === "running").map((c) => c.n), bad = d.cells.filter((c) => c.s === "fail").map((c) => c.n);
      g.font = `400 8.5px ${FONT}`;
      if (bad.length) txt(g, fit(g, "✗ " + bad.join(" "), run.length ? gw / 2 - 6 : gw), w - pad, h - 9, T.red, 8.5, "right");
      if (run.length) txt(g, fit(g, "▸ " + run.join(" "), bad.length ? gw / 2 - 6 : gw), pad, h - 9, T.yellow, 8.5);
      if (!run.length && !bad.length) txt(g, d.cells.length ? "nothing running, nothing failing" : "no test modules found", pad, h - 9, T.dim, 8.5);
      mem.quiet = !mem.run.length;
    },
    live(g, d, w, h, t, T, mem) {
      mem.run.forEach((c, i) => {
        g.fillStyle = rgba(T.bg, 0.55 * (0.5 + 0.5 * Math.sin(t * 4 + i * 0.7)));
        g.fillRect(c.x, c.y, c.s, c.s);
      });
    },
  };

  const PLACEHOLDER = {base: (g, d, w, h, T) => waiting(g, w, h, T, "drawing soon")};

  function mount(el) {
    if (!el || typeof document === "undefined") return null;
    const doc = document, win = window;
    const mk = (tag, cls, parent, text) => {
      const n = doc.createElement(tag);
      if (cls) n.className = cls;
      if (text != null) n.textContent = text;
      if (parent) parent.appendChild(n);
      return n;
    };
    let T = Object.assign({}, TOKENS);
    const readTokens = () => {
      const cs = win.getComputedStyle(el);
      for (const k of Object.keys(TOKENS)) T[k] = cs.getPropertyValue("--bd-" + k).trim() || TOKENS[k];
    };
    el.replaceChildren();
    const panels = PANELS.map((P, i) => {
      const sec = mk("section", "bd-p", el);
      sec.dataset.k = P.key;
      sec.dataset.accent = P.accent;
      sec.setAttribute("aria-labelledby", "bd-t-" + P.key);
      const head = mk("header", "bd-h", sec);
      mk("span", "bd-chip", head, String(i + 1).padStart(2, "0")).setAttribute("aria-hidden", "true");
      const h2 = mk("h2", "bd-t", head, P.name);
      h2.id = "bd-t-" + P.key;
      const wh = mk("span", "bd-w", h2, " · " + P.what);
      const label = mk("span", "bd-l", head, "");
      const box_ = mk("div", "bd-c", sec);
      const cv = mk("canvas", "", box_);
      cv.setAttribute("role", "img");
      cv.setAttribute("aria-label", P.name);
      const foot = mk("footer", "bd-f", sec);
      const cap = mk("span", "bd-cap", foot, "");
      const bar = mk("span", "bd-bar", foot);
      bar.hidden = true;
      bar.setAttribute("aria-hidden", "true");
      const fill = mk("i", "", bar);
      const stat = mk("span", "bd-s", foot, "");
      return {key: P.key, name: P.name, sec, box: box_, cv, ctx: cv.getContext("2d"), label, cap, bar, fill, stat, h2, wh, full: "",
        w: 0, h: 0, sx: 1, sy: 1, vis: true, d: null, dk: "", base: null, off: null, mem: {}};
    });
    const byBox = new Map(panels.map((p) => [p.box, p]));
    const bySec = new Map(panels.map((p) => [p.sec, p]));
    let lastPaint = 0, raf = 0, still = false, shown = true, frames = 0, hidden = !!doc.hidden;

    const size = (p) => {
      const r = p.box.getBoundingClientRect(), dpr = Math.min(2, win.devicePixelRatio || 1);
      const W = Math.max(0, Math.round(r.width * dpr)), H = Math.max(0, Math.round(r.height * dpr));
      if (W === p.cv.width && H === p.cv.height && p.w) return;
      p.cv.width = W;
      p.cv.height = H;
      p.w = r.width;
      p.h = r.height;
      p.sx = r.width ? W / r.width : 1;
      p.sy = r.height ? H / r.height : 1;
      p.base = null;
    };
    const invalidate = () => {
      readTokens();
      for (const p of panels) p.base = null;
      kick();
    };
    const paint = (p, t) => {
      if (!p.w) size(p);  // a frame can land before the first ResizeObserver call, on the canvas's default 300×150
      if (!p.cv.width || !p.cv.height || !p.d && p.key !== "loop") return;
      if (p.base && p.mem.quiet && p.cv.dataset.painted === "1") return;  // nothing moves here: the last frame stands
      const R = DRAW[p.key] || PLACEHOLDER, g = p.ctx;
      try {
        if (!p.base) {
          const off = p.off || (p.off = doc.createElement("canvas"));
          off.width = p.cv.width;
          off.height = p.cv.height;
          const o = off.getContext("2d");
          o.setTransform(p.sx, 0, 0, p.sy, 0, 0);
          p.mem.quiet = !p.d;
          if (p.d) R.base(o, p.d, p.w, p.h, T, p.mem);
          else waiting(o, p.w, p.h, T, "no task in focus");
          p.base = off;
        }
        g.setTransform(1, 0, 0, 1, 0, 0);
        g.clearRect(0, 0, p.cv.width, p.cv.height);
        g.drawImage(p.base, 0, 0);
        if (R.live && p.d) {
          g.setTransform(p.sx, 0, 0, p.sy, 0, 0);
          R.live(g, p.d, p.w, p.h, t, T, p.mem);
        }
        if (p.cv.dataset.painted !== "1") p.cv.dataset.painted = "1";
      } catch (e) {
        if (!p.err) console.error("board: the " + p.key + " panel failed to draw", e);
        p.err = true;
      }
    };
    // a panel that says it is quiet (nothing in it moves) neither paints nor keeps the loop alive
    const want = () => shown && !still && !hidden && panels.some((p) => p.vis && !p.mem.quiet);
    function paintAll() {
      if (hidden || !shown) return;
      const t = still ? STILL_T : performance.now() / 1000;
      for (const p of panels) paint(p, t);
      el.dataset.frames = String(++frames);
    }
    function frame(now) {
      raf = 0;
      if (!want()) return kick();
      if (now - lastPaint < 30) { raf = win.requestAnimationFrame(frame); return; }  // T-3715: at most ~30 fps, half the canvas work of a 60 Hz loop
      lastPaint = now;
      const t = now / 1000;
      for (const p of panels) if (p.vis) paint(p, t);
      el.dataset.frames = String(++frames);
      raf = win.requestAnimationFrame(frame);
    }
    function kick() {
      let go = want();
      if (!go) {
        if (raf) win.cancelAnimationFrame(raf);
        raf = 0;
        paintAll();
        go = want();  // a repaint can wake a panel whose new data moves
      }
      el.dataset.running = go ? "1" : "0";
      if (go && !raf) raf = win.requestAnimationFrame(frame);
    }
    el.dataset.frames = "0";
    el.dataset.running = "0";
    if (win.ResizeObserver) {
      const ro = new win.ResizeObserver((ents) => {
        for (const e of ents) { const p = byBox.get(e.target); size(p); fitDom(p); }
        kick();
      });
      for (const p of panels) ro.observe(p.box);
    }
    if (win.IntersectionObserver) {
      const io = new win.IntersectionObserver((ents) => {
        for (const e of ents) bySec.get(e.target).vis = e.isIntersecting;
        kick();
      }, {rootMargin: "48px"});
      for (const p of panels) io.observe(p.sec);
    }
    doc.addEventListener("visibilitychange", () => {
      hidden = !!doc.hidden;
      kick();
    });
    const light = win.matchMedia ? win.matchMedia("(prefers-color-scheme: light)") : null;
    if (light && light.addEventListener) light.addEventListener("change", invalidate);
    if (doc.fonts && doc.fonts.load) doc.fonts.load(`500 10px ${FONT}`).then(invalidate, () => {});
    readTokens();

    function fitDom(p) {  // words whole or none: the subtitle goes when it doesn't fit; the footer sheds its trailing " · " parts
      p.wh.hidden = false;
      p.wh.hidden = p.h2.scrollWidth > p.h2.clientWidth;
      const segs = p.full.split(" · ");
      let n = segs.length;
      p.cap.textContent = p.full;
      while (n > 1 && p.cap.scrollWidth > p.cap.clientWidth) p.cap.textContent = segs.slice(0, --n).join(" · ");
      p.cap.title = n < segs.length ? p.full : "";
    }

    function update(m) {
      const ps = arr(obj(m).panels);
      let changed = false;
      panels.forEach((p, i) => {
        const P = obj(ps[i]);
        if (P.key !== p.key) return;
        const dk = JSON.stringify(P.data);
        if (dk !== p.dk) {
          p.dk = dk;
          p.d = P.data;
          p.base = null;
          changed = true;
        }
        const f = obj(P.foot);
        if (p.label.textContent !== P.label) p.label.textContent = P.label || "";
        p.full = f.text || "";
        fitDom(p);
        if (p.stat.textContent !== f.stat) p.stat.textContent = f.stat || "";
        p.bar.hidden = f.pct == null;
        if (f.pct != null) p.fill.style.width = Math.max(0, Math.min(100, f.pct)) + "%";
        const say = `${p.name}: ${f.text || ""}${f.stat ? ", " + f.stat : ""}`;
        if (p.cv.getAttribute("aria-label") !== say) p.cv.setAttribute("aria-label", say);
      });
      if (changed) kick();  // same data: a running loop already paints it, a stopped one needs nothing
    }
    kick();
    return {
      update,
      still(on) { still = !!on; kick(); },
      show(on) { shown = !!on; kick(); },
    };
  }

  return {PANELS, KINDS, STAGES, TOKENS, DRAW, boardModel, projectList, mount};
})();
if (typeof module !== "undefined" && module.exports) module.exports = SampoBoard;
else window.SampoBoard = SampoBoard;  // a top-level const is no window property: live.js finds it here
