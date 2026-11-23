/* Sampo · the Board (T-3659): the live page's modern default. Fourteen panels (T-3731 added the last five), each a small
   instrument for one Sampo mechanism, drawn on canvas from the snapshot the server streams (snapshot.board, built by sampo.board).
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
    {key: "asks", name: "Asks", what: "your asks this week, by stage", accent: "pink", wide: true},  // T-3731
    {key: "sessions", name: "Sessions", what: "live sessions and their context", accent: "violet"},
    {key: "ship", name: "Ship & CI", what: "the last push through CI", accent: "green"},
    {key: "usage", name: "Usage", what: "the 5-hour and weekly tanks", accent: "orange"},
    {key: "guard", name: "Guard", what: "commands through the Ward gate", accent: "red"},
  ];

  // ---------- the model: any snapshot, empty or huge, to fourteen bounded panels ----------
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
    // T-4076: known once there is a reading: a fresh server's first snapshot has none (now: null), not 0.0 cores
    const c = obj(b.cpu), now = obj(c.now), out = {known: !!b.cpu && !!c.now, kinds: KINDS, goal: num(c.goal_pct, 10) || 10,
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
      gap: Object.entries(obj(obj(s.stats).gap)).slice(0, 5).map(([name, rows]) =>  // T-3930: points behind the Claude reference
        ({name: str(name, 24), pts: arr(rows).map((r) => num(obj(r).gap, NaN)).filter(isFinite).slice(-14)})).filter((x) => x.pts.length > 0),
      native: (() => {  // T-6060: Sampo's dollars per landed lane over plain Claude Code's, per day (NaN: nothing landed that day)
        const n = obj(obj(s.stats).native);
        return {pts: arr(n.days).map((r) => num(obj(r).ratio, NaN)).slice(-14), target: num(n.target_ratio, 0.1),
          src: str(obj(n.reference).source, 60)};
      })(),
      cpm: Math.round(calls / 5), loaded: models.filter((m) => m.loaded).length};
  }

  function mSampo(s, b, A) {
    const q = A ? arr(s.queue).filter(A.vis) : arr(s.queue);
    return {commands: arr(b.commands).slice(-12).map((x) => ({at: str(obj(x).at, 32), cmd: str(obj(x).cmd, 96), ok: obj(x).ok !== false})),
      queue: (A ? spread(q, 8) : q.slice(0, 8)).map((x) => ({...(A ? A.mark(x) : {}), id: str(obj(x).id, 12), title: str(obj(x).title, 64),
        status: str(obj(x).status, 10), tier: str(obj(x).tier, 2), type: str(obj(x).type, 10)})),
      queued: q.length,
      caught: obj(b.state).caught ? {caught: int(obj(b.state).caught.caught), scored: int(obj(b.state).caught.scored)} : null,  // T-4665
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

  // ---------- T-3731: five pictures of the machine and the user's asks ----------
  const clamp = (x, lo, hi) => Math.min(hi, Math.max(lo, num(x, lo)));
  function dur(s) {  // seconds as a short span: "40 s", "21 min", "2 h 9 min", "3 d 5 h"
    s = Math.max(0, Math.round(num(s)));
    if (s < 60) return `${s} s`;
    if (s < 3600) return `${Math.floor(s / 60)} min`;
    if (s < 86400) return `${Math.floor(s / 3600)} h${s % 3600 >= 60 ? ` ${Math.floor((s % 3600) / 60)} min` : ""}`;
    const d = Math.floor(s / 86400);
    return d > 999 ? "999+ d" : `${d} d${s % 86400 >= 3600 ? ` ${Math.floor((s % 86400) / 3600)} h` : ""}`;
  }

  function mSessions(b, A) {
    const p = obj(b.sessions);
    let rows = arr(p.rows).map(obj);
    if (A) rows = rows.filter(A.vis);
    const shown = (A ? spread(rows, 24) : rows.slice(0, 24)).map((r) => ({...(A ? A.mark(r) : {}), id: str(r.id, 8),
      ctx: r.context_pct == null || !isFinite(r.context_pct) ? null : Math.round(clamp(r.context_pct, 0, 100)),
      task: str(r.task, 12), model: str(r.model, 16), mine: r.mine !== false, age: Math.min(864000, int(r.age_s))}));
    return {known: !!b.sessions, bodies: shown, more: Math.min(1e6, int(p.more) + Math.max(0, rows.length - shown.length)),
      private: Math.min(1e6, int(p.private))};
  }

  const ASK_STAGES = ["asked", "planned", "building", "verifying", "merging", "blocked", "landed"];
  function mAsks(b, A) {
    const p = obj(b.asks), days = Math.max(1, Math.min(31, int(p.days) || 7));
    let rows = arr(p.rows).map(obj).filter((r) => ASK_STAGES.includes(r.stage));
    if (A) rows = rows.filter(A.vis);
    const n = rows.length;  // the server sends the newest, oldest first: keep the newest (each project's, turn about)
    rows = (A ? spread(rows.slice().reverse(), 60) : rows.slice(-60)).map((r) => ({...(A ? A.mark(r) : {}), id: str(r.id, 12), title: str(r.title, 90),
      stage: r.stage, age: Math.round(clamp(r.age_h, 0, days * 24) * 10) / 10}));
    rows.sort((x, y) => y.age - x.age || (x.id < y.id ? -1 : 1));
    return {known: !!b.asks, rows, days, more: Math.min(1e6, int(p.more) + n - rows.length)};
  }

  const CI_STATES = ["waiting", "running", "red", "green", "local", "offline", "nogh", "ghfail", "reading"];
  const CI_WORD = {nogh: "no gh", ghfail: "gh failing"};  // the label and footer's word for a state
  function mShip(b, A) {
    const all = A ? arr(b.ships).map(obj).filter(A.vis) : b.ship ? [obj(b.ship)] : [], src = all.slice(0, 6);
    const ships = src.map((x) => {
      const ci = obj(x.ci), J = arr(ci.jobs).map(obj);
      return {...(A ? A.mark(x) : {}), sha: str(x.sha, 12), subject: str(x.subject, 120), age: Math.min(1e8, int(x.age_s)),
        ref: str(x.ref, 16), unshipped: Math.min(1e6, int(x.unshipped)), state: CI_STATES.includes(ci.state) ? ci.state : "reading",
        jobs: J.slice(0, 16).map((j) => {
          const st = arr(j.steps), total = Math.min(99, int(st[1]));
          return {name: str(j.name, 40), wf: str(j.workflow, 24), status: str(j.status, 12), conclusion: str(j.conclusion, 12),
            done: Math.min(total, int(st[0])), total, secs: Math.min(1e6, int(j.secs))};
        }), more: Math.max(0, J.length - 16)};
    });
    return {known: ships.length > 0, ships, more: Math.min(1e6, all.length - src.length)};
  }

  const TANKS = [["five_hour", "5 h"], ["seven_day", "week"]];
  function mUsage(b) {
    const u = obj(b.usage), W = obj(u.windows), sp = obj(u.spend);
    const tanks = TANKS.map(([key, label]) => {
      const x = obj(W[key]), known = typeof x.used === "number" && isFinite(x.used);
      return {key, label, used: known ? Math.round(clamp(x.used, 0, 100) * 10) / 10 : null, span: int(x.span),
        left: Math.min(1e7, int(x.resets_in)), projected: x.projected == null || !isFinite(x.projected) ? null : Math.round(clamp(x.projected, 0, 200)),
        fullIn: x.full_in == null || !isFinite(x.full_in) || x.full_in < 0 ? null : Math.min(1e7, int(x.full_in)), why: str(x.why, 80)};
    });
    const wk = arr(u.week).map(arr).filter((r) => isFinite(r[0]) && isFinite(r[1])), step = Math.max(1, Math.ceil(wk.length / 84));
    const week = wk.filter((_, i) => (wk.length - 1 - i) % step === 0).slice(-84)
      .map((r) => [Math.round(clamp(r[0], 0, 168) * 100) / 100, Math.round(clamp(r[1], 0, 100))]);
    const spend = {today: Math.min(1e6, r2(sp.today)), cap: Math.min(1e6, r2(sp.cap)), hourly: arr(sp.hourly).slice(-24).map((v) => Math.min(1e6, r2(v)))};
    return {known: !!b.usage && (tanks.some((t) => t.used != null) || spend.today > 0), tanks, spend, week};
  }

  function guardSum(p, A) {  // All mode: the gate's figures summed over the projects the chips show
    const by = obj(p.by), keys = Object.keys(by).filter((k) => A.vis({project: k})).slice(0, 100);
    const out = {allowed: 0, refused: 0, minutes: Array.from({length: 60}, () => [0, 0])}, rules = {}, tools = {};
    const add = (m, list, k) => { for (const x of arr(list).map(obj)) m[str(x[k], 40)] = (m[str(x[k], 40)] || 0) + Math.min(1e7, int(x.n)); };
    for (const k of keys) {
      const q = obj(by[k]), mm = arr(q.minutes).slice(-60);
      out.allowed += int(q.allowed);
      out.refused += int(q.refused);
      mm.forEach((m, i) => { const r = out.minutes[60 - mm.length + i]; r[0] += int(arr(m)[0]); r[1] += int(arr(m)[1]); });
      add(rules, q.rules, "rule");
      add(tools, q.tools, "tool");
    }
    const ranked = (m, k) => Object.entries(m).sort((x, y) => y[1] - x[1] || (x[0] < y[0] ? -1 : 1)).map(([name, n]) => ({[k]: name, n}));
    return {...p, ...out, rules: ranked(rules, "rule"), tools: ranked(tools, "tool")};
  }
  function mGuard(b, A) {
    const p = A && b.guard && typeof obj(b.guard).by === "object" ? guardSum(obj(b.guard), A) : obj(b.guard);
    let recent = arr(p.recent).map(obj);
    if (A) recent = recent.filter(A.vis);
    recent = recent.slice(0, 12).map((r) => ({...(A ? A.mark(r) : {}), ago: Math.min(1e6, int(r.ago_s)), rule: str(r.rule || "rule", 24),
      tool: str(r.tool, 12), cmd: str(r.cmd, 80)}));
    const rules = arr(p.rules).map(obj).slice(0, 6).map((r) => {
      const rule = str(r.rule || "rule", 24), last = recent.find((x) => x.rule === rule);
      return {rule, n: Math.min(1e7, int(r.n)), last: last ? last.cmd : "", pc: last && last.pc, tag: last && last.tag};
    });
    const minutes = arr(p.minutes).slice(-60).map((m) => [Math.min(1e6, int(arr(m)[0])), Math.min(1e6, int(arr(m)[1]))]);
    const allowed = Math.min(1e9, int(p.allowed)), refused = Math.min(1e9, int(p.refused));
    return {known: !!b.guard && allowed + refused > 0, allowed, refused, rules, recent, minutes,
      tools: arr(p.tools).slice(0, 5).map((t) => ({tool: str(obj(t).tool, 12), n: Math.min(1e7, int(obj(t).n))}))};
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
        foot: {text: (LANE_STATES.filter((k) => n[k]).map((k) => `${k} ${n[k]}`).join(" · ") || "no builder lanes out") +
          (d.more ? ` · +${fmt(d.more)} more` : ""),  // T-4076: the label counts every lane; say the undrawn ones
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
      // T-4665: the command count stays the stat; once predictions are scored, what they caught before the user said it
      // trails the footer's text (fitDom sheds a trailing " · " part first when the width is short)
      const c = d.caught && d.caught.scored ? `caught before you noticed: ${d.caught.caught} of ${d.caught.scored}` : null;
      return {label: `${fmt(d.queued)} queued`,
        foot: {text: (last ? `$ ${last.cmd}` : "no sampo commands seen yet") + (c ? ` · ${c}` : ""),
          stat: d.commands.length ? `${ok}/${d.commands.length} ok` : ""}};
    },
    state: (d) => ({label: "state/",
      foot: {text: d.known ? `${plural(d.tree.length, "entry", "entries")} · newest ${d.newest || "—"}` : "the state folder isn't read yet",
        stat: d.doc ? `latest ${d.doc.kind}` : ""}}),
    tests: (d) => ({label: plural(d.cells.length + d.more, "module"),
      foot: {text: d.known ? `${d.count.cached} cached · ${d.count.running} running · ${d.count.fail} failing${d.source ? " · " + d.source : ""}`
        : "no test run seen yet", stat: d.total ? `${d.done}/${d.total} checks passed` : "", pct: d.total ? pct(d.done, d.total) : null}}),
    asks: (d) => {
      const n = {};
      for (const r of d.rows) n[r.stage] = (n[r.stage] || 0) + 1;
      return {label: `${plural(d.rows.length + d.more, "ask")} · ${d.days} d`,
        foot: {text: d.rows.length ? ASK_STAGES.filter((k) => n[k]).map((k) => `${k} ${n[k]}`).join(" · ") : `no asks in ${d.days} days`,
          stat: d.rows.length ? `${n.landed || 0} landed` : ""}};
    },
    sessions: (d) => {
      const B = d.bodies, c = B.filter((x) => x.ctx != null).map((x) => x.ctx), top = c.length ? Math.max(...c) : null;
      return {label: plural(B.length + d.more, "session"),
        foot: {text: B.length ? `${B.filter((x) => x.task).length} on a task · ${B.filter((x) => x.age < 120).length} heard from in 2 min` +
          (d.private ? ` · ${d.private} private` : "") : "no live session",
          stat: top == null ? "" : `fullest ${top}%`, pct: top}};
    },
    ship: (d) => {
      const s = d.ships[0];
      if (!s) return {label: "not pushed", foot: {text: "nothing pushed yet", stat: ""}};
      const many = d.ships.length > 1, J = many ? d.ships.flatMap((x) => x.jobs) : s.jobs, ok = J.filter((j) => j.conclusion === "success").length;
      const n = {};  // All mode: the pushes counted by CI state, not the first project's commit
      for (const x of d.ships) n[x.state] = (n[x.state] || 0) + 1;
      return {label: many ? plural(d.ships.length + d.more, "push", "pushes") : `CI ${CI_WORD[s.state] || s.state}`,
        foot: {text: many ? CI_STATES.filter((k) => n[k]).map((k) => `${n[k]} ${CI_WORD[k] || k}`).join(" · ") + (d.more ? ` · +${fmt(d.more)} more` : "")
          : `${s.sha} · ${dur(s.age)} ago · ${s.subject}`,
          stat: J.length ? `${ok}/${J.length} jobs green` : "",
          pct: J.length ? pct(J.filter((j) => j.status === "completed").length, J.length) : null}};
    },
    usage: (d) => {
      const [f, wk] = d.tanks, say = (t) => `${t.label} ${t.used == null ? "unknown" : Math.round(t.used) + "%"}`;
      return {label: d.spend.cap ? `$${d.spend.today.toFixed(2)} of $${fmt(d.spend.cap)}` : `$${d.spend.today.toFixed(2)} today`,
        foot: {text: d.known ? `${say(f)} · ${say(wk)}${f.used != null && f.left ? ` · resets in ${dur(f.left)}` : ""}` : "no usage reading yet",
          stat: f.projected != null ? `heading for ${f.projected}%` : "", pct: f.used}};
    },
    guard: (d) => ({label: `${fmt(d.refused)} refused`,
      foot: {text: d.allowed + d.refused ? `${fmt(d.allowed)} allowed · ${fmt(d.refused)} refused in the last hour` : "no command in the last hour",
        stat: d.rules[0] ? `most: ${d.rules[0].rule}` : ""}}),
  };

  function boardModel(snap, hist, hidden) {  // hidden: project slugs the chips turned off (All mode)
    const s = obj(snap), b = obj(s.board), h = arr(hist), prev = obj(h[h.length - 1]), A = allCtx(s, hidden);
    const data = {lanes: mLanes(s, b, A), cpu: mCpu(b), loop: mLoop(s, b, A), library: mLibrary(s, b, prev), models: mModels(s),
      sampo: mSampo(s, b, A), state: mState(b), tests: mTests(b),
      asks: mAsks(b, A), sessions: mSessions(b, A), ship: mShip(b, A), usage: mUsage(b), guard: mGuard(b, A)};
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
  // T-4078: a panel with no data: nothing read yet (its data key is still "") or a snapshot with nothing in focus
  const idle = (dk) => dk ? "no task in focus" : "waiting for the first snapshot";
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
      const top = Math.max(0, ...d.hourly), peak = Math.max(1, top), bw = (w - 2 * pad) / 24;
      hline(g, pad, w - pad, hy1, T.line);
      d.hourly.forEach((v, i) => {
        const bh = Math.round(((hy1 - hy0 - 10) * v) / peak);
        if (!bh) return;
        g.fillStyle = i === 23 ? T.ink : rgba(T.cyan, 0.4 + (0.45 * i) / 23);
        g.fillRect(Math.round(pad + i * bw + 1), hy1 - bh, Math.max(1, Math.round(bw) - 2), bh);
      });
      txt(g, `kept / hour · peak ${fmt(top)}`, pad, hy0 + 2, T.dim, 8);
      if (!top) txt(g, "nothing kept in 24 h", w / 2, (hy0 + hy1) / 2 + 3, T.dim, 9, "center");  // T-4076: not a blank box
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
      const nh = 46, gapH = d.gap.length ? 54 : 18, pad = 14, gc = GROUP_COL(T), gh = gapH + nh, y1 = h - pad - (d.gpu ? 28 : 0) - gh;
      mem.L = [];
      mem.quiet = !d.links.length;
      {  // T-6060: Sampo's dollars per landed lane over plain Claude Code's, per day: 1 is native, the dashed line is 10x cheaper
        const nTop = h - pad - (d.gpu ? 28 : 0) - nh + 12, nBot = nTop + nh - 24, x0 = pad + 4, x1 = w - pad - 60, nat = obj(d.native), pts = Array.isArray(nat.pts) ? nat.pts : [];
        const seen = pts.filter(isFinite), last = seen.length ? seen[seen.length - 1] : null;
        txt(g, "COST VS NATIVE", pad, nTop - 4, T.orange, 7.5, "left", 600);
        if (!seen.length) txt(g, "no landed lane priced yet", pad + 86, nTop - 4, T.dim, 7.5);
        else {
          const hi = Math.max(1.2, ...seen), Y = (v) => nBot - (nBot - nTop) * Math.min(v, hi) / hi;
          g.strokeStyle = T.grid;
          g.beginPath();
          g.moveTo(x0, Y(1) + 0.5);
          g.lineTo(x1, Y(1) + 0.5);
          g.stroke();
          const tgt = Number.isFinite(nat.target);  // T-6156: no target, the points alone: no dashed 10x line, no label
          if (tgt) {
            g.strokeStyle = T.green;
            g.setLineDash([3, 3]);
            g.beginPath();
            g.moveTo(x0, Y(nat.target) + 0.5);
            g.lineTo(x1, Y(nat.target) + 0.5);
            g.stroke();
            g.setLineDash([]);
          }
          const X = (i) => (pts.length > 1 ? x0 + (x1 - x0) * i / (pts.length - 1) : x1);
          g.strokeStyle = g.fillStyle = T.orange;
          g.beginPath();
          let pen = false;
          pts.forEach((v, i) => {
            if (!isFinite(v)) { pen = false; return; }
            g[pen ? "lineTo" : "moveTo"](X(i), Y(v));
            pen = true;
          });
          g.stroke();
          pts.forEach((v, i) => { if (isFinite(v)) { g.beginPath(); g.arc(X(i), Y(v), 1.6, 0, TAU); g.fill(); } });
          txt(g, `${last.toFixed(2)}x`, x1 + 6, Y(last) + 3, T.orange, 7.5);
          if (tgt) txt(g, `10x ${nat.target}x`, x1 + 6, Y(nat.target) + 3, T.green, 7);
        }
      }
      {  // the frontier gap: one line per local profile, points behind Claude per run (lower is closer; below 0 beats Claude)
        const top = h - pad - (d.gpu ? 28 : 0) - gh + 12, bot = top + Math.max(4, gapH - 22), x0 = pad + 4, x1 = w - pad - 60;
        txt(g, "GAP TO CLAUDE", pad, top - 4, T.orange, 7.5, "left", 600);
        if (!d.gap.length) txt(g, "no gap yet: first paid reference run pending", pad + 78, top - 4, T.dim, 7.5);
        else {
          const all = d.gap.flatMap((s) => s.pts), hi = Math.max(10, ...all), lo = Math.min(0, ...all), cols = [T.cyan, T.orange, T.violet, T.green, T.pink];
          const Y = (v) => bot - (bot - top) * (v - lo) / (hi - lo), zero = Y(0);  // 0 is Claude: nothing draws below the box
          g.strokeStyle = T.grid;
          g.beginPath();
          g.moveTo(x0, zero + 0.5);
          g.lineTo(x1, zero + 0.5);
          g.stroke();
          d.gap.forEach((s, k) => {
            g.strokeStyle = g.fillStyle = cols[k % cols.length];
            const X = (i) => (s.pts.length > 1 ? x0 + (x1 - x0) * i / (s.pts.length - 1) : x1);
            g.beginPath();
            s.pts.forEach((v, i) => g[i ? "lineTo" : "moveTo"](X(i), Y(v)));
            g.stroke();
            g.beginPath();
            g.arc(X(s.pts.length - 1), Y(s.pts[s.pts.length - 1]), 2, 0, TAU);
            g.fill();
            txt(g, `${fit(g, s.name, 40)} ${s.pts[s.pts.length - 1]}`, x1 + 6, Y(s.pts[s.pts.length - 1]) + 3, cols[k % cols.length], 7.5);
          });
        }
      }
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

  // ---------- T-3731: sessions, asks, ship & CI, usage, guard ----------
  const GAUGE = (T, v) => (v == null ? T.dim : v < 60 ? T.green : v < 85 ? T.yellow : T.red);
  const MODEL_COL = (T, m) => (/opus/i.test(m) ? T.violet : /sonnet/i.test(m) ? T.cyan : /haiku/i.test(m) ? T.pink : T.muted);
  function disc(g, x, y, r, col) {
    g.fillStyle = col;
    g.beginPath();
    g.arc(x, y, Math.max(0, r), 0, TAU);
    g.fill();
  }
  function ring(g, x, y, r, a0, a1, col, lw) {
    g.strokeStyle = col;
    g.lineWidth = Math.max(0, lw);
    g.beginPath();
    g.arc(x, y, Math.max(0, r), a0, a1);
    g.stroke();
  }

  DRAW.sessions = {  // 11: the live sessions on orbits around the machine, each a ring gauge of its context window
    base(g, d, w, h, T, mem) {
      mem.b = [];
      mem.c = null;
      mem.orbits = [];
      mem.labs = [];
      mem.quiet = true;
      const B = d.bodies, pad = 14;
      if (!B.length) {
        waiting(g, w, h, T, "waiting for a Claude session to report");
        if (d.private) txt(g, `${plural(d.private, "private session")} not drawn`, w / 2, h / 2 + 16, T.muted, 9, "center");
        return;
      }
      const legend = h >= 150 && w >= 260 ? 16 : 0, top = pad, bottom = h - legend - 6;
      const rings = B.length <= 8 ? 1 : B.length <= 16 ? 2 : 3, labs = h >= 200 && w >= 300, lab = labs ? 26 : 0;
      let br = clamp(Math.min(w, h) / (rings === 1 ? 10 : 14), 5, 20);
      const cx = w / 2, cy = (top + bottom - lab) / 2;
      const RY = Math.max(6, (bottom - lab - top) / 2 - br), RX = Math.max(8, w / 2 - pad - Math.max(br, labs ? 40 : 0));
      // a chip wide enough for "15 live" is tall enough for its two lines (the name and the count)
      const mw = Math.round(clamp(Math.min(RX, RY) * 0.75, 30, 76)), mh = Math.round(mw >= 50 ? Math.max(36, mw * 0.6) : mw * 0.6);
      const f0 = rings === 1 ? 1 : clamp(Math.max((mh / 2 + br + 10) / RY, (mw / 2 + br + 10) / RX), 0.35, 0.85);
      const F = Array.from({length: rings}, (_, k) => (rings === 1 ? 1 : f0 + ((1 - f0) * k) / (rings - 1)));
      const sumF = F.reduce((a, f) => a + f, 0), cnt = F.map((f) => Math.floor((B.length * f) / sumF));
      cnt[rings - 1] += B.length - cnt.reduce((a, c) => a + c, 0);
      const sp = F.map((f, k) => (Math.PI * f * (RX + RY)) / Math.max(1, cnt[k]));
      // where each body sits; then the radius shrinks to what the closest two (on any orbit) leave room for
      const at = [];
      F.forEach((f, k) => {
        for (let j = 0; j < cnt[k]; j++) {
          const a = -Math.PI / 2 + (j / Math.max(1, cnt[k])) * TAU + k * 0.5;
          at.push({x: cx + RX * f * Math.cos(a), y: cy + RY * f * Math.sin(a), k, j});
        }
      });
      let dmin = Infinity;
      for (let p = 0; p < at.length; p++) for (let q = p + 1; q < at.length; q++) dmin = Math.min(dmin, Math.hypot(at[p].x - at[q].x, at[p].y - at[q].y));
      br = Math.max(4, Math.min(br, Math.min(...sp) * 0.36, dmin * 0.42 - 1));
      const lw = Math.max(1.5, br * 0.22), inside = br >= 16;
      // the orbits
      g.lineWidth = 1;
      F.forEach((f, k) => {
        g.strokeStyle = T.line;
        g.setLineDash(k % 2 ? [2, 4] : []);
        g.beginPath();
        g.ellipse(cx, cy, Math.max(1, RX * f), Math.max(1, RY * f), 0, 0, TAU);
        g.stroke();
        mem.orbits.push({rx: RX * f, ry: RY * f, k});
      });
      g.setLineDash([]);
      // place each body; the tethers first, so the bodies sit on top
      at.forEach(({x: bx, y: by, k}, i) => {
        const x = B[i], dx = bx - cx, dy = by - cy, dl = Math.hypot(dx, dy) || 1;
        const s = Math.min((mw / 2 + 4) / (Math.abs(dx) || 1e-6), (mh / 2 + 4) / (Math.abs(dy) || 1e-6));
        mem.b.push({x: bx, y: by, k, sx: cx + dx * s, sy: cy + dy * s, ex: bx - (dx / dl) * (br + 2), ey: by - (dy / dl) * (br + 2),
          bc: (x.pc && T[x.pc]) || MODEL_COL(T, x.model), gc: GAUGE(T, x.ctx), ctx: x.ctx, age: x.age, mine: x.mine, br, lw,
          rr: br - lw / 2, labs: labs && sp[k] >= 70, sp: sp[k], s: x});
      });
      for (const b of mem.b) {
        g.strokeStyle = rgba(b.bc, b.mine ? 0.26 : 0.16);
        g.lineWidth = 1;
        g.setLineDash(b.mine ? [] : [2, 3]);
        g.beginPath();
        g.moveTo(b.sx, b.sy);
        g.lineTo(b.ex, b.ey);
        g.stroke();
      }
      g.setLineDash([]);
      // the machine: a die with its pins
      const mx = Math.round(cx - mw / 2), my = Math.round(cy - mh / 2), np = Math.max(2, Math.floor(mw / 8)), nq = Math.max(2, Math.floor(mh / 8));
      g.fillStyle = T.muted;
      for (let p = 0; p < np; p++) {
        const px = Math.round(mx + 5 + (p * (mw - 10)) / (np - 1));
        g.fillRect(px - 1, my - 4, 2, 4);
        g.fillRect(px - 1, my + mh, 2, 4);
      }
      for (let p = 0; p < nq; p++) {
        const py = Math.round(my + 5 + (p * (mh - 10)) / (nq - 1));
        g.fillRect(mx - 4, py - 1, 4, 2);
        g.fillRect(mx + mw, py - 1, 4, 2);
      }
      g.fillStyle = T.bg;
      g.fillRect(mx, my, mw, mh);
      box(g, mx, my, mw, mh, T.muted, 0.07);
      // a top strip holds the pin-1 mark and the activity lights; the name and the count sit below it
      ring(g, mx + 5.5, my + 5.5, 1.5, 0, TAU, rgba(T.muted, 0.6), 1);
      g.font = `500 9px ${FONT}`;
      // under the strip: the name, and the count when two lines fit whole above the bottom edge
      const big = mh >= 36, body = my + 9 + (mh - 9) / 2, live = Math.min(body + 11, my + mh - 4);
      txt(g, fit(g, "machine", mw - 6) || "machine", cx, big ? live - 11 : body + 3, T.ink, 9, "center", 500);
      if (big) {  // the count, its word dropped when the chip is too narrow for both
        const n = fmt(B.length + d.more);
        g.font = `400 9px ${FONT}`;
        txt(g, g.measureText(`${n} live`).width <= mw - 6 ? `${n} live` : fit(g, n, mw - 6), cx, live, T.muted, 9, "center");
      }
      mem.c = {cx, cy, led: {x: Math.round(mx + mw - 24), y: my + 4}, fresh: B.filter((x) => x.age < 120).length};
      // each label under its body, kept only where it clears the machine, every other body and the labels kept before it
      const hit = (a, r) => a.x < r.x + r.w && r.x < a.x + a.w && a.y < r.y + r.h && r.y < a.y + a.h;
      const keep = [], walls = [{x: mx - 6, y: my - 6, w: mw + 12, h: mh + 12}];
      for (const b of mem.b) walls.push({x: b.x - br - 3, y: b.y - br - 3, w: 2 * br + 6, h: 2 * br + 6, b});
      for (const b of mem.b) {
        if (!b.labs) continue;
        const x = b.s, lw2 = Math.min(84, b.sp - 10), say = x.ctx == null ? "?" : `${x.ctx}%`;
        g.font = `400 9px ${FONT}`;
        const l1 = fit(g, (inside ? "" : say + " ") + (x.task || x.id), lw2), w1 = g.measureText(l1).width;
        const l2 = fit(g, x.tag || x.model, lw2), bw2 = Math.max(g.measureText(l2).width, w1) + 6;
        const r = {x: b.x - bw2 / 2, y: b.y + br + 3, w: bw2, h: 23};
        if (r.x < 2 || r.x + r.w > w - 2 || r.y + r.h > h - legend - 2) continue;
        if (walls.some((o) => o.b !== b && hit(r, o)) || keep.some((o) => hit(r, o))) continue;
        keep.push(r);
        b.lab = {l1, l2, r};
      }
      mem.labs = keep;
      // the bodies: a ring gauge each, its fill the context used; the core in the model's (or project's) colour
      for (const b of mem.b) {
        disc(g, b.x, b.y, br + 2, T.bg);
        disc(g, b.x, b.y, br - lw - 1, rgba(b.bc, 0.13));
        ring(g, b.x, b.y, b.rr, 0, TAU, T.grid, lw);
        if (b.ctx != null && b.ctx > 0) ring(g, b.x, b.y, b.rr, -Math.PI / 2, -Math.PI / 2 + (TAU * b.ctx) / 100, b.gc, lw);
        ring(g, b.x, b.y, br - lw - 1.5, 0, TAU, rgba(b.bc, 0.8), 1);
        g.fillStyle = T.line;
        for (let q = 0; q < 4; q++) {  // quarter ticks outside the ring
          const a = -Math.PI / 2 + (q * Math.PI) / 2;
          g.fillRect(Math.round(b.x + (br + 2.5) * Math.cos(a)) - 0.5, Math.round(b.y + (br + 2.5) * Math.sin(a)) - 0.5, 1, 1);
        }
        const say = b.ctx == null ? "?" : `${b.ctx}%`;
        if (inside) txt(g, say, b.x, b.y + 3, b.ctx == null ? T.dim : T.ink, 9, "center", 500);
        if (b.lab) {
          const {l1, l2, r} = b.lab;
          g.fillStyle = T.bg;  // a backing, so a tether passing under the label doesn't strike it through
          g.fillRect(Math.round(r.x), Math.round(r.y), Math.round(r.w), r.h);
          txt(g, l1, b.x, b.y + br + 12, b.s.task ? T.ink : T.muted, 9, "center");
          txt(g, l2, b.x, b.y + br + 23, b.bc, 9, "center");
        }
      }
      // the legend, and what isn't drawn
      const extra = [d.more ? `+${fmt(d.more)} more` : "", d.private ? `+${fmt(d.private)} private` : ""].filter(Boolean).join(" · ");
      if (extra) txt(g, extra, w - pad, h - 7, T.muted, 9, "right");
      if (legend) {
        let lx = pad;
        g.font = `400 9px ${FONT}`;
        txt(g, "context", lx, h - 7, T.muted, 9);
        g.font = `400 9px ${FONT}`;
        lx += g.measureText("context").width + 10;
        for (const [c, s] of [[T.green, "<60%"], [T.yellow, "<85%"], [T.red, "fuller"]]) {
          ring(g, lx + 3, h - 10, 3, 0, TAU, c, 1.5);
          txt(g, s, lx + 10, h - 7, T.muted, 9);
          g.font = `400 9px ${FONT}`;
          lx += 18 + g.measureText(s).width;
        }
        g.font = `400 9px ${FONT}`;
        const room = w - pad - (extra ? g.measureText(extra).width + 14 : 0);
        if (!B.some((x) => x.tag)) {
          for (const m of [...new Set(B.map((x) => x.model).filter(Boolean))].slice(0, 4)) {
            g.font = `400 9px ${FONT}`;
            const mwid = g.measureText(m).width + 18;
            if (lx + 10 + mwid > room) break;
            disc(g, lx + 13, h - 10, 2.5, MODEL_COL(T, m));
            txt(g, m, lx + 19, h - 7, T.muted, 9);
            lx += mwid + 6;
          }
        }
      }
      mem.quiet = false;
    },
    live(g, d, w, h, t, T, mem) {
      const c = mem.c;
      if (!c) return;
      const under = (x, y) => mem.labs.some((r) => x > r.x - 2 && x < r.x + r.w + 2 && y > r.y - 2 && y < r.y + r.h + 2);  // a label hides it
      // a tracer runs each orbit, hidden where a body or a label sits
      mem.orbits.forEach((o) => {
        const dir = o.k % 2 ? -1 : 1;
        for (let j = 0; j < 5; j++) {
          const a = dir * (t * 0.2) / (1 + o.k * 0.35) - dir * j * 0.03 + o.k * 1.3;
          const x = c.cx + o.rx * Math.cos(a), y = c.cy + o.ry * Math.sin(a);
          if (under(x, y) || mem.b.some((b) => b.k === o.k && Math.abs(b.x - x) < b.br + 3 && Math.abs(b.y - y) < b.br + 3)) continue;
          g.fillStyle = rgba(T.violet, 0.6 - j * 0.11);
          g.fillRect(x - 1, y - 1, 2, 2);
        }
      });
      mem.b.forEach((b, i) => {
        // requests out along the tether, faster for a session heard from lately; replies back for the freshest
        const v = b.age < 120 ? 0.5 : b.age < 900 ? 0.2 : 0.06, u = (t * v + i * 0.29) % 1;
        const px = b.sx + (b.ex - b.sx) * u, py = b.sy + (b.ey - b.sy) * u;
        g.fillStyle = b.bc;
        if (!under(px, py)) g.fillRect(px - 1.5, py - 1.5, 3, 3);
        if (b.age < 120) {
          const u2 = (t * v * 0.8 + i * 0.29 + 0.5) % 1, qx = b.ex + (b.sx - b.ex) * u2, qy = b.ey + (b.sy - b.ey) * u2;
          g.fillStyle = rgba(b.gc, 0.85);
          if (!under(qx, qy)) g.fillRect(qx - 1, qy - 1, 2, 2);
        }
        if (b.ctx == null) return;
        // the gauge's tip glows; a nearly full one sends out a warning ring
        const a = -Math.PI / 2 + (TAU * b.ctx) / 100, p = 0.5 + 0.5 * Math.sin(t * 3 + i);
        disc(g, b.x + b.rr * Math.cos(a), b.y + b.rr * Math.sin(a), b.lw * 0.5 + p * 0.8, rgba(b.gc, 0.55 + 0.45 * p));
        if (b.ctx >= 85) {
          const k = (t * 0.7 + i * 0.2) % 1, L = b.lab && b.lab.r;
          g.save();
          if (L) {  // the ring stops at its label's plate
            g.beginPath();
            g.rect(0, 0, w, h);
            g.rect(L.x, L.y, L.w, L.h);
            g.clip("evenodd");
          }
          ring(g, b.x, b.y, b.br + 3 + k * 7, 0, TAU, rgba(T.red, 0.4 * (1 - k)), 1);
          g.restore();
        }
      });
      for (let i = 0; i < 4; i++) {  // the machine's activity lights, one per session heard from in two minutes
        const on = i < c.fresh && Math.sin(t * (2.3 + i * 0.9) + i * 1.9) > -0.2;
        g.fillStyle = on ? T.green : rgba(T.green, 0.18);
        g.fillRect(c.led.x + i * 5, c.led.y, 3, 2);
      }
    },
  };

  const ASK_COL = (T) => ({asked: T.cyan, planned: T.violet, building: T.yellow, verifying: T.orange, merging: T.pink, blocked: T.red,
    landed: T.green});
  DRAW.asks = {  // 10: the week's asks as a river, a channel per stage, each ask where its day falls; an open one trails to now
    base(g, d, w, h, T, mem) {
      mem.m = [];
      mem.lanes = [];
      mem.quiet = true;
      if (!d.rows.length) return waiting(g, w, h, T, "waiting for your next ask");
      const col = ASK_COL(T), pad = 14, span = d.days * 24;
      g.font = `500 9px ${FONT}`;
      const gut = w >= 200 ? Math.ceil(Math.max(...ASK_STAGES.map((k) => g.measureText(k).width))) + 30 : 0;
      const head = h >= 120 ? 18 : 0, axis = h >= 110 ? 16 : 2;
      const x0 = pad + gut, x1 = w - pad, y0 = pad + head, y1 = h - axis - 8, lh = (y1 - y0) / ASK_STAGES.length;
      const X = (age) => x1 - (age / span) * (x1 - x0), n = {};
      for (const r of d.rows) n[r.stage] = (n[r.stage] || 0) + 1;
      // the channels, the blocked one hatched, the stage names in the gutter
      ASK_STAGES.forEach((k, i) => {
        const y = Math.round(y0 + i * lh), c = col[k], hh = Math.max(1, Math.round(lh) - 1);
        g.fillStyle = rgba(c, 0.04);
        g.fillRect(x0, y + 1, x1 - x0, hh);
        if (k === "blocked") hatch(g, x0, y + 1, x1 - x0, hh, rgba(T.red, 0.1));
        hline(g, x0, x1, y, T.grid);
        if (gut) {
          const ly = y0 + i * lh + lh / 2;
          g.fillStyle = c;
          g.fillRect(pad, Math.round(ly) - 3, 2, Math.min(6, Math.max(1, Math.round(lh) - 2)));
          if (lh >= 10) {  // a channel too thin for a 9 px name keeps its colour tick alone
            txt(g, k, pad + 7, ly + 3, n[k] ? T.ink : T.muted, 9);
            txt(g, n[k] ? String(n[k]) : "·", x0 - 8, ly + 3, n[k] ? c : T.muted, 9, "right", 500);
          }
        }
        mem.lanes.push({y: y0 + i * lh + lh / 2, h: lh, c});
      });
      hline(g, x0, x1, Math.round(y1), T.grid);
      // the days, as many labels as fit
      for (let k = 1; k <= d.days; k++) {
        g.fillStyle = rgba(T.line, 0.9);
        g.fillRect(Math.round(X(k * 24)), y0, 1, y1 - y0);
      }
      if (axis > 2) {
        const every = Math.max(1, Math.ceil(48 / Math.max(1, (x1 - x0) / d.days)));
        for (let k = every; k <= d.days; k += every) txt(g, `−${k} d`, X(k * 24), h - 9, T.muted, 9, k === d.days ? "left" : "center");
        txt(g, "now", x1, h - 9, T.pink, 9, "right", 500);
      }
      // the asks: an open one trails a wake to now; close ones in a channel step aside rather than hide each other
      const mr = clamp(lh * 0.2, 2, 4.5), last = {};
      let newest = null;
      for (const r of d.rows) if (!newest || r.age < newest.age) newest = r;
      d.rows.forEach((r) => {
        const L = mem.lanes[ASK_STAGES.indexOf(r.stage)], c = (r.pc && T[r.pc]) || L.c, x = X(r.age), prev = last[r.stage];
        const off = Math.min(lh * 0.26, mr + 2), dy = prev && x - prev.x < mr * 2 + 2 ? (prev.dy === 0 ? -off : prev.dy < 0 ? off : 0) : 0;
        const m = {x, y: L.y + dy, dy, r: mr, c, lane: r.stage, active: r.stage !== "landed", newest: r === newest, id: r.id};
        last[r.stage] = m;
        mem.m.push(m);
      });
      for (const m of mem.m) {
        if (!m.active) continue;
        g.strokeStyle = rgba(m.c, 0.3);
        g.lineWidth = 1;
        g.beginPath();
        g.moveTo(m.x + m.r, Math.round(m.y) + 0.5);
        g.lineTo(x1, Math.round(m.y) + 0.5);
        g.stroke();
      }
      const labEnd = {};
      mem.m.forEach((m, i) => {
        disc(g, m.x, m.y, m.r + 1.5, T.bg);
        disc(g, m.x, m.y, m.r, m.active ? rgba(m.c, 0.28) : m.c);
        ring(g, m.x, m.y, m.r, 0, TAU, m.c, 1);
        if (lh < 20) return;
        g.font = `400 9px ${FONT}`;
        const next = mem.m.slice(i + 1).find((o) => o.lane === m.lane), lx = m.x + m.r + 4, tw = g.measureText(m.id).width;
        if (lx > (labEnd[m.lane] || 0) && lx + tw < Math.min(next ? next.x - next.r - 3 : x1 - 2, x1 - 2)) {
          txt(g, m.id, lx, m.y - 3, T.muted, 9);
          labEnd[m.lane] = lx + tw + 3;
        }
      });
      // the newest ask, by name
      if (head) {
        const r = newest, c = (r.pc && T[r.pc]) || col[r.stage];
        let hx = pad;
        txt(g, "newest", hx, pad + 6, T.muted, 9);
        g.font = `400 9px ${FONT}`;
        hx += g.measureText("newest").width + 10;
        if (r.tag) { txt(g, r.tag, hx, pad + 6, c, 9, "left", 500); g.font = `500 9px ${FONT}`; hx += g.measureText(r.tag).width + 8; }
        txt(g, r.id, hx, pad + 6, c, 9.5, "left", 500);
        g.font = `500 9.5px ${FONT}`;
        hx += g.measureText(r.id).width + 8;
        const more = d.more ? `+${fmt(d.more)} older` : "";
        g.font = `400 9px ${FONT}`;
        const mwid = more ? g.measureText(more).width + 12 : 0;
        if (more) txt(g, more, w - pad, pad + 6, T.muted, 9, "right");
        g.font = `400 9.5px ${FONT}`;
        txt(g, fitWords(g, r.title, w - pad - mwid - hx, 1), hx, pad + 6, T.ink, 9.5);
      }
      Object.assign(mem, {x0, x1, y0, y1});
      mem.quiet = false;
    },
    live(g, d, w, h, t, T, mem) {
      if (!mem.lanes.length) return;
      const span = mem.x1 - mem.x0;
      if (span > 8) {  // the current: faint dashes drift toward now down every channel
        mem.lanes.forEach((L, i) => {
          g.fillStyle = rgba(L.c, 0.28);
          for (let j = 0; j < 4; j++) {
            const x = mem.x0 + ((t * (12 + i * 1.7) + (j * span) / 4 + i * 37) % span);
            g.fillRect(Math.round(x), Math.round(L.y + (j % 2 ? 0.3 : -0.3) * L.h), Math.max(1, Math.min(9, mem.x1 - x)), 1);
          }
        });
      }
      mem.m.forEach((m, i) => {
        if (m.active) ring(g, m.x, m.y, m.r + 2 + Math.sin(t * 2.6 + i), 0, TAU, rgba(m.c, 0.18 + 0.14 * Math.sin(t * 2.6 + i)), 1);
        if (m.newest) {
          const k = (t * 0.6) % 1;
          ring(g, m.x, m.y, m.r + 2 + k * 12, 0, TAU, rgba(m.c, 0.6 * (1 - k)), 1);
        }
      });
      g.save();  // now: a marching line at the river's mouth
      g.strokeStyle = rgba(T.pink, 0.75);
      g.lineWidth = 1;
      g.setLineDash([3, 3]);
      g.lineDashOffset = -t * 6;
      g.beginPath();
      g.moveTo(Math.round(mem.x1) - 0.5, mem.y0);
      g.lineTo(Math.round(mem.x1) - 0.5, mem.y1);
      g.stroke();
      g.restore();
    },
  };

  const JOB_COL = (T, j) => (j.conclusion === "success" ? T.green : /failure|timed_out|startup_failure/.test(j.conclusion) ? T.red
    : j.status === "in_progress" ? T.yellow : j.status === "completed" ? T.dim : T.muted);
  const CI_COL = (T) => ({waiting: T.muted, running: T.yellow, red: T.red, green: T.green, local: T.dim, offline: T.dim,
    nogh: T.orange, ghfail: T.orange, reading: T.dim});
  const CI_SAY = {waiting: "waiting for CI to pick the push up", local: "no GitHub remote here: CI isn't read",
    offline: "GitHub isn't reachable: CI unknown", nogh: "gh isn't installed: CI unknown",
    ghfail: "gh can't read CI: signed out or rate-limited", reading: "reading CI…"};
  function shipOne(g, s, w, h, T, mem) {
    const pad = 14, narrow = w < 420, cc = CI_COL(T)[s.state] || T.dim;
    const cw = narrow ? w - 2 * pad : Math.round(clamp(w * 0.32, 120, 210)), ch = narrow ? 28 : Math.round(Math.min(h - 2 * pad, 122));
    const cx0 = pad, cy0 = narrow ? pad : Math.round((h - ch) / 2);
    // the commit, as a card
    box(g, cx0, cy0, cw, ch, T.line, 0);
    g.fillStyle = rgba(T.ink, 0.025);
    g.fillRect(cx0 + 1, cy0 + 1, cw - 2, ch - 2);
    g.fillStyle = cc;
    g.fillRect(cx0, cy0, 2, ch);
    if (narrow) {
      const pw = pill(g, s.sha, cx0 + 8, cy0 + ch / 2, T.green, T.bg, 9, "left"), ago = `${dur(s.age)} ago`;
      g.font = `400 9px ${FONT}`;
      const aw = g.measureText(ago).width;
      txt(g, ago, cx0 + cw - 8, cy0 + ch / 2 + 3, T.dim, 9, "right");
      g.font = `400 9.5px ${FONT}`;
      txt(g, fitWords(g, s.subject, cw - pw - aw - 30, 1), cx0 + pw + 14, cy0 + ch / 2 + 3, T.ink, 9.5);
    } else {
      const pw = pill(g, s.sha, cx0 + 10, cy0 + 15, T.green, T.bg, 9, "left");
      g.font = `400 9px ${FONT}`;
      if (g.measureText(s.ref).width <= cw - pw - 26) txt(g, s.ref, cx0 + cw - 8, cy0 + 18, T.dim, 9, "right");  // whole or not at all
      g.font = `400 9.5px ${FONT}`;
      const lines = wrap(g, s.subject, cw - 20, Math.max(1, Math.min(3, Math.floor((ch - 66) / 13) + 1)));
      lines.forEach((l, i) => txt(g, l, cx0 + 10, cy0 + 39 + i * 13, T.ink, 9.5));
      txt(g, `pushed ${dur(s.age)} ago`, cx0 + 10, cy0 + ch - (s.unshipped ? 23 : 10), T.dim, 9);
      if (s.unshipped) txt(g, `${plural(s.unshipped, "commit")} not pushed`, cx0 + 10, cy0 + ch - 10, T.yellow, 9);
    }
    // the jobs, grouped by workflow, a lane each fed from the commit
    const jx0 = narrow ? pad : cx0 + cw + Math.max(36, Math.round(w * 0.07)), jx1 = w - pad;
    const pillY = narrow ? cy0 + ch + 16 : pad + 6, jy0 = pillY + 14, jy1 = h - pad + 4;
    pill(g, `CI ${CI_WORD[s.state] || s.state}`, jx1, pillY, cc, T.bg, 9, "right");
    if (s.jobs.length) txt(g, plural(s.jobs.length + s.more, "job"), jx0, pillY + 3, T.dim, 9);
    const port = {x: cx0 + cw, y: cy0 + ch / 2};
    if (!narrow) {
      disc(g, port.x, port.y, 3, T.bg);
      ring(g, port.x, port.y, 3, 0, TAU, cc, 1);
    }
    if (!s.jobs.length) {
      const say = s.state === "running" || s.state === "red" || s.state === "green" ? "no jobs listed for this push" : CI_SAY[s.state];
      g.font = `400 9.5px ${FONT}`;
      const L = wrap(g, say, jx1 - jx0 - 8, 2), my = (jy0 + jy1) / 2;
      L.forEach((l, i) => txt(g, l, (jx0 + jx1) / 2, my + 3 + (i - (L.length - 1) / 2) * 13, T.dim, 9.5, "center"));
      if (!narrow) {
        g.strokeStyle = rgba(cc, 0.4);
        g.setLineDash([2, 3]);
        g.beginPath();
        g.moveTo(port.x + 3, port.y);
        g.lineTo(jx0 - 6, port.y);
        g.stroke();
        g.setLineDash([]);
      }
      mem.wait = s.state === "waiting" || s.state === "reading" ? {x: jx0 - 6, y: port.y} : null;
      return;
    }
    const groups = [];
    for (const j of s.jobs) {
      let G = groups.find((x) => x.wf === j.wf);
      if (!G) groups.push((G = {wf: j.wf, jobs: []}));
      G.jobs.push(j);
    }
    const rows = s.jobs.length + (s.more ? 1 : 0), span = Math.max(1, jy1 - jy0);
    let rh = span / (rows + groups.length * 0.8);
    const heads = rh >= 13;
    if (!heads) rh = span / rows;
    rh = Math.min(rh, 24);
    const nw = Math.round(clamp((jx1 - jx0) * 0.3, 36, 120)), bx0 = jx0 + nw + 4, dw = jx1 - bx0 > 90 ? 46 : 0, bw = Math.max(4, jx1 - dw - bx0);
    let y = jy0;
    groups.forEach((G) => {
      if (heads) {
        const head = G.wf || "jobs";
        g.font = `500 9px ${FONT}`;
        txt(g, head, jx0, y + rh * 0.8 - 3, T.dim, 9, "left", 500);
        g.font = `500 9px ${FONT}`;
        hline(g, jx0 + 6 + g.measureText(head).width, jx1, Math.round(y + rh * 0.8 - 6), T.grid);
        y += rh * 0.8;
      }
      for (const j of G.jobs) {
        const cy = y + rh / 2, c = JOB_COL(T, j), bh = Math.round(clamp(rh * 0.32, 2, 6)), by = Math.round(cy - bh / 2), run = j.status === "in_progress";
        g.font = `400 9px ${FONT}`;
        if (rh >= 11) txt(g, fit(g, j.name, nw - 6), jx0, cy + 3, j.status === "completed" ? T.muted : T.ink, 9);
        g.fillStyle = T.grid;
        g.fillRect(bx0, by, bw, bh);
        const fw = j.total ? (bw * j.done) / j.total : j.status === "completed" ? bw : 0;
        g.fillStyle = rgba(c, j.status === "completed" ? 0.85 : 0.6);
        g.fillRect(bx0, by, Math.round(fw), bh);
        if (j.total > 1 && bw / j.total >= 4) {
          g.fillStyle = T.bg;
          for (let k = 1; k < j.total; k++) g.fillRect(Math.round(bx0 + (bw * k) / j.total), by, 1, bh);
        }
        if (dw && rh >= 11) txt(g, j.status === "queued" ? "queued" : dur(j.secs), jx1, cy + 3, run ? T.yellow : T.dim, 9, "right");
        if (!narrow) {
          const P = link(port.x + 3, port.y, jx0 - 6, cy);
          g.strokeStyle = rgba(c, run ? 0.4 : 0.22);
          g.lineWidth = 1;
          g.beginPath();
          curve(g, P);
          g.stroke();
          if (run || j.status === "queued") mem.pipes.push({P, c});
        }
        if (run) mem.run.push({x: bx0, y: by, w: fw, h: bh, step: j.total ? bw / j.total : bw * 0.1, full: bw});
        if (j.status === "queued") mem.q.push({x: bx0, y: by, w: bw, h: bh});
        y += rh;
      }
    });
    if (s.more && rh >= 11) txt(g, `+${fmt(s.more)} more jobs`, jx0, y + rh / 2 + 3, T.dim, 9);
  }
  function shipMany(g, ships, more, w, h, T, mem) {  // All mode: a row per project's last push, its jobs as a strip of cells
    const pad = 14, foot = more ? 16 : 0, room = h - 2 * pad - foot, rh = Math.min(60, room / ships.length);
    const top = pad + Math.max(0, (room - rh * ships.length) / 2);
    if (more) txt(g, `+${fmt(more)} more ${more === 1 ? "push" : "pushes"} not drawn`, pad + 8, Math.min(h - pad + 2, top + rh * ships.length + 13), T.dim, 9);
    ships.forEach((s, i) => {
      const y = top + i * rh, c = (s.pc && T[s.pc]) || T.green, cc = CI_COL(T)[s.state] || T.dim, two = rh >= 30, three = rh >= 52;
      const l1 = three ? y + rh * 0.26 : two ? y + rh * 0.36 : y + rh / 2;
      if (i) hline(g, pad, w - pad, Math.round(y), T.grid);
      g.fillStyle = c;
      g.fillRect(pad, Math.round(y + 4), 2, Math.max(1, Math.round(rh - 8)));
      if (rh < 11) return;  // too thin for a line of text: the project's bar alone
      let x = pad + 8;
      if (s.tag) {
        txt(g, s.tag, x, l1 + 3, c, 9, "left", 500);
        g.font = `500 9px ${FONT}`;
        x += g.measureText(s.tag).width + 8;
      }
      x += pill(g, s.sha, x, l1, T.green, T.bg, 9, "left") + 8;
      const ago = `${dur(s.age)} ago`;
      g.font = `400 9px ${FONT}`;
      const aw = g.measureText(ago).width;
      txt(g, ago, w - pad, l1 + 3, T.dim, 9, "right");
      g.font = `400 9.5px ${FONT}`;
      txt(g, fitWords(g, s.subject, w - pad - aw - 10 - x, 1), x, l1 + 3, T.ink, 9.5);
      if (!two) return;
      const l2 = three ? y + rh * 0.52 : y + rh * 0.72, st = `CI ${CI_WORD[s.state] || s.state}`;
      if (three) {  // the jobs that matter by name: what failed, else what runs, else how many passed
        const pick = (f) => s.jobs.filter(f).map((j) => j.name);
        const bad = pick((j) => /failure|timed_out|startup_failure/.test(j.conclusion)), run = pick((j) => j.status === "in_progress");
        const [say, sc] = bad.length ? [`failed: ${bad.join(", ")}`, T.red] : run.length ? [`running: ${run.join(", ")}`, T.yellow]
          : s.jobs.length ? [`${s.jobs.filter((j) => j.conclusion === "success").length}/${s.jobs.length} jobs green`, T.muted] : ["", T.dim];
        const not = s.unshipped ? `${plural(s.unshipped, "commit")} not pushed` : "";
        g.font = `400 9px ${FONT}`;
        const nw = not ? g.measureText(not).width + 12 : 0;
        if (not) txt(g, not, w - pad, y + rh * 0.78 + 3, T.yellow, 9, "right");
        g.font = `400 9px ${FONT}`;
        txt(g, fitWords(g, say, w - 2 * pad - 8 - nw, 1), pad + 8, y + rh * 0.78 + 3, sc, 9);
      }
      g.font = `500 9px ${FONT}`;
      const sw = g.measureText(st).width;
      txt(g, st, w - pad, l2 + 3, cc, 9, "right", 500);
      const room = w - 2 * pad - 8 - sw - 14, cw = s.jobs.length ? Math.min(14, room / s.jobs.length) : 0;
      g.font = `400 9px ${FONT}`;
      if (!s.jobs.length) txt(g, fitWords(g, CI_SAY[s.state] || "no jobs listed", room, 1), pad + 8, l2 + 3, T.dim, 9);
      s.jobs.forEach((j, k) => {
        const jx = pad + 8 + k * cw, jc = JOB_COL(T, j);
        g.fillStyle = rgba(jc, j.status === "completed" ? 0.85 : 0.45);
        g.fillRect(Math.round(jx), Math.round(l2 - 3), Math.max(1, Math.round(cw) - 2), 6);
        if (j.status === "in_progress") mem.run.push({x: Math.round(jx), y: Math.round(l2 - 3), w: Math.max(1, Math.round(cw) - 2), h: 6, cell: true});
      });
    });
  }
  DRAW.ship = {  // 12: the last push, its commit feeding a lane per CI job, each lane a bar of its steps
    base(g, d, w, h, T, mem) {
      mem.pipes = [];
      mem.run = [];
      mem.q = [];
      mem.wait = null;
      mem.quiet = true;
      if (!d.ships.length) return waiting(g, w, h, T, "waiting for the first push");
      if (d.ships.length > 1 || d.more) shipMany(g, d.ships, int(d.more), w, h, T, mem);
      else shipOne(g, d.ships[0], w, h, T, mem);
      mem.quiet = !mem.run.length && !mem.q.length && !mem.wait;
    },
    live(g, d, w, h, t, T, mem) {
      mem.pipes.forEach((p, i) => {  // packets flow down the pipes to the jobs still going
        g.fillStyle = p.c;
        for (let j = 0; j < 2; j++) {
          const [px, py] = along(p.P, (t * 0.35 + i * 0.23 + j * 0.5) % 1);
          g.fillRect(px - 1.5, py - 1.5, 3, 3);
        }
      });
      mem.run.forEach((r, i) => {
        if (r.cell) {
          g.fillStyle = rgba(T.yellow, 0.35 + 0.35 * Math.sin(t * 3 + i));
          g.fillRect(r.x, r.y, r.w, r.h);
          return;
        }
        // the step underway fills and fades; a glint runs along what's done
        const sw = Math.max(1, Math.min(r.step, r.full - r.w));
        g.fillStyle = rgba(T.yellow, 0.2 + 0.25 * (0.5 + 0.5 * Math.sin(t * 3.2 + i)));
        g.fillRect(Math.round(r.x + r.w), r.y, Math.round(sw * ((t * 0.25 + i * 0.3) % 1)), r.h);
        if (r.w > 8) {
          const gx = r.x + ((t * 40 + i * 23) % r.w);
          g.fillStyle = rgba(T.ink, 0.45);
          g.fillRect(Math.round(gx), r.y, 2, r.h);
        }
      });
      mem.q.forEach((r, i) => {
        g.strokeStyle = rgba(T.muted, 0.25 + 0.25 * Math.sin(t * 2 + i));
        g.lineWidth = 1;
        g.strokeRect(r.x + 0.5, r.y - 0.5, r.w - 1, r.h + 1);
      });
      if (mem.wait) disc(g, mem.wait.x, mem.wait.y, 2 + Math.sin(t * 2.5), rgba(T.muted, 0.6));
    },
  };

  DRAW.usage = {  // 13: the 5-hour and weekly tanks, each filled to what's used with its projection marked; the day's spend
    base(g, d, w, h, T, mem) {
      mem.t = [];
      mem.last = null;
      mem.quiet = true;
      if (!d.known) return waiting(g, w, h, T, "waiting for a usage reading from the statusline");
      const pad = 14, narrow = w < 330, cap = h >= 150;
      const colW = narrow ? (w - 2 * pad) / 2 : Math.round(clamp(w * 0.17, 84, 120)), tw = Math.round(clamp(colW * 0.5, 22, 56));
      const ty0 = pad + 30, ty1 = h - pad - (cap ? 25 : 0) - (narrow ? 14 : 0), th = Math.max(4, ty1 - ty0);
      d.tanks.forEach((k, i) => {
        const cx = pad + colW * i + colW / 2, x = Math.round(cx - tw / 2), r = Math.min(7, tw / 4), lc = GAUGE(T, k.used);
        txt(g, k.label, cx, pad + 7, T.muted, 9, "center", 500);
        txt(g, k.used == null ? "?" : `${Math.round(k.used)}%`, cx, pad + 23, k.used == null ? T.dim : T.ink, 13, "center", 500);
        const glass = () => {
          g.beginPath();
          if (g.roundRect) g.roundRect(x + 0.5, ty0 + 0.5, tw - 1, th - 1, Math.max(0, Math.min(r, (th - 1) / 2)));
          else g.rect(x + 0.5, ty0 + 0.5, tw - 1, th - 1);
        };
        glass();
        g.fillStyle = rgba(T.ink, 0.025);
        g.fill();
        g.save();
        glass();
        g.clip();
        if (k.used == null) hatch(g, x, ty0, tw, th, rgba(T.dim, 0.3));
        else {
          const ly = ty0 + th * (1 - k.used / 100), gr = g.createLinearGradient(0, ly, 0, ty0 + th);
          gr.addColorStop(0, rgba(lc, 0.55));
          gr.addColorStop(1, rgba(lc, 0.16));
          g.fillStyle = gr;
          g.fillRect(x, Math.round(ly + 1), tw, Math.max(0, ty0 + th - ly));
          const bx = [], rng = seed(k.key);
          for (let j = 0; j < 5; j++) bx.push(rng());
          mem.t.push({x, w: tw, top: ly, y0: ty0, bottom: ty0 + th, c: lc, bx});
        }
        g.fillStyle = rgba(T.ink, 0.07);  // a glint down the glass
        g.fillRect(x + 3, ty0 + 4, 2, Math.max(0, th - 8));
        g.restore();
        glass();
        g.strokeStyle = rgba(T.muted, 0.7);
        g.lineWidth = 1;
        g.stroke();
        g.fillStyle = rgba(T.muted, 0.5);
        for (let q = 1; q < 4; q++) g.fillRect(x + tw - 6, Math.round(ty0 + (th * q) / 4), 4, 1);
        if (k.projected != null && k.used != null) {
          const pv = Math.min(100, k.projected), py = Math.round(ty0 + th * (1 - pv / 100)) + 0.5, pc = GAUGE(T, k.projected);
          g.strokeStyle = pc;
          g.setLineDash([3, 2]);
          g.beginPath();
          g.moveTo(x - 4, py);
          g.lineTo(x + tw + 4, py);
          g.stroke();
          g.setLineDash([]);
          if (k.projected > 100) {  // past the brim: an arrow over the top
            g.fillStyle = pc;
            g.beginPath();
            g.moveTo(cx - 4, ty0 - 2);
            g.lineTo(cx + 4, ty0 - 2);
            g.lineTo(cx, ty0 - 6);
            g.closePath();
            g.fill();
          }
        }
        if (cap) {
          g.font = `400 9px ${FONT}`;
          if (k.used == null) {
            wrap(g, k.why || "no reading", colW - 4, 2).forEach((l, j) => txt(g, l, cx, ty1 + 12 + j * 11, T.muted, 9, "center"));
          } else {
            const pr = [k.projected != null ? `${k.projected}% by reset` : "no projection", k.projected != null ? `→ ${k.projected}%` : "no proj."]
              .find((s) => g.measureText(s).width <= colW - 4) || `${k.projected}%`;
            txt(g, fit(g, pr, colW - 4), cx, ty1 + 12, k.projected != null ? GAUGE(T, k.projected) : T.muted, 9, "center");
            if (k.left) {  // the longest wording that fits whole: a cut "2 h 9 m…" reads as a different time
              g.font = `400 9px ${FONT}`;
              const say = [`resets ${dur(k.left)}`, `in ${dur(k.left)}`, dur(k.left)].find((s) => g.measureText(s).width <= colW - 4);
              txt(g, say || fit(g, dur(k.left), colW - 4), cx, ty1 + 23, T.muted, 9, "center");
            }
          }
        }
      });
      const sp = d.spend, money = `$${sp.today.toFixed(2)}`;
      if (narrow) {
        txt(g, `${money}${sp.cap ? ` of $${fmt(sp.cap)}` : ""} today`, pad, h - 8, sp.cap && sp.today > sp.cap ? T.red : T.orange, 9.5, "left", 500);
        mem.quiet = !mem.t.length;
        return;
      }
      // the day's spend against its cap, then by hour, then the week's 5-hour use
      const sx0 = pad + colW * 2 + 20, sx1 = w - pad, sw = Math.max(10, sx1 - sx0);
      txt(g, money, sx0, pad + 13, T.ink, 15, "left", 500);
      g.font = `500 15px ${FONT}`;
      const mw = g.measureText(money).width + 8;
      g.font = `400 9.5px ${FONT}`;
      txt(g, fit(g, sp.cap ? `of $${fmt(sp.cap)} today` : "today, no cap set", sw - mw), sx0 + mw, pad + 12, T.muted, 9.5);
      if (sp.cap) {
        const f = Math.min(1, sp.today / sp.cap), over = sp.today > sp.cap;
        g.fillStyle = T.grid;
        g.fillRect(sx0, pad + 21, sw, 4);
        g.fillStyle = over ? T.red : T.orange;
        g.fillRect(sx0, pad + 21, Math.round(sw * f), 4);
        g.fillStyle = T.muted;
        g.fillRect(sx1 - 1, pad + 18, 1, 10);
      }
      const rest = h - pad - (pad + 40), weekOn = rest >= 120 && d.week.length > 1;
      const hy0 = pad + 46, hy1 = weekOn ? hy0 + Math.round(rest * 0.42) : h - pad - 12, H = d.spend.hourly;
      if (hy1 - hy0 >= 16) {
        const peak = Math.max(0.01, ...H), bw = sw / 24, off = 24 - H.length;
        txt(g, "spend / hour", sx0, hy0 - 4, T.muted, 9);
        g.font = `400 9px ${FONT}`;
        const pk = `peak $${peak.toFixed(2)}`;
        if (g.measureText("spend / hour" + pk).width + 12 <= sw) txt(g, pk, sx1, hy0 - 4, T.muted, 9, "right");
        hline(g, sx0, sx1, hy1, T.line);
        H.forEach((v, i) => {
          const bh = Math.round(((hy1 - hy0 - 4) * v) / peak), bx = Math.round(sx0 + (off + i) * bw);
          if (bh <= 0) return;
          g.fillStyle = rgba(T.orange, i === H.length - 1 ? 0.9 : 0.55);
          g.fillRect(bx, hy1 - bh, Math.max(1, Math.round(bw) - 2), bh);
          if (i === H.length - 1) mem.last = {x: bx, y: hy1 - bh, w: Math.max(1, Math.round(bw) - 2)};
        });
        if (!weekOn) {
          txt(g, "−24 h", sx0, h - pad + 4, T.muted, 9);
          txt(g, "now", sx1, h - pad + 4, T.muted, 9, "right");
        }
      }
      if (weekOn) {
        const wy0 = hy1 + 22, wy1 = h - pad - 8, wh = Math.max(4, wy1 - wy0), wk = d.tanks[1], lc = GAUGE(T, wk.used);
        const X = (ago) => sx1 - (ago / 168) * sw, Y = (v) => wy1 - (v / 100) * wh;
        txt(g, "week used · 7 d", sx0, wy0 - 5, T.muted, 9);
        txt(g, "100%", sx1, wy0 - 5, T.muted, 9, "right");
        g.strokeStyle = rgba(T.red, 0.45);
        g.setLineDash([2, 3]);
        g.beginPath();
        g.moveTo(sx0, Math.round(wy0) + 0.5);
        g.lineTo(sx1, Math.round(wy0) + 0.5);
        g.stroke();
        g.setLineDash([]);
        hline(g, sx0, sx1, wy1, T.line);
        const P = d.week.slice().sort((a, b) => b[0] - a[0]);
        g.beginPath();
        g.moveTo(X(P[0][0]), wy1);
        for (const [a, v] of P) g.lineTo(X(a), Y(v));
        g.lineTo(X(P[P.length - 1][0]), wy1);
        g.closePath();
        g.fillStyle = rgba(lc, 0.12);
        g.fill();
        g.beginPath();
        P.forEach(([a, v], i) => (i ? g.lineTo(X(a), Y(v)) : g.moveTo(X(a), Y(v))));
        g.strokeStyle = lc;
        g.lineWidth = 1.25;
        g.stroke();
        g.lineWidth = 1;
        txt(g, "−7 d", sx0, h - pad + 4, T.muted, 9);
        txt(g, "now", sx1, h - pad + 4, T.muted, 9, "right");
      }
      mem.quiet = !mem.t.length && !mem.last;
    },
    live(g, d, w, h, t, T, mem) {
      mem.t.forEach((k, i) => {
        if (k.bottom - k.top < 2) return;
        g.save();
        g.beginPath();
        g.rect(k.x + 1, k.y0 + 1, k.w - 2, k.bottom - k.y0 - 2);
        g.clip();
        // the surface: a slow wave across the top of the liquid
        g.beginPath();
        g.moveTo(k.x, k.top + 3);
        for (let x = 0; x <= k.w; x += 2) g.lineTo(k.x + x, k.top + 1.2 * Math.sin(x * 0.32 + t * 2.2 + i * 1.7));
        g.lineTo(k.x + k.w, k.top + 3);
        g.closePath();
        g.fillStyle = rgba(k.c, 0.5);
        g.fill();
        g.beginPath();
        for (let x = 0; x <= k.w; x += 2) {
          const y = k.top + 1.2 * Math.sin(x * 0.32 + t * 2.2 + i * 1.7);
          if (x) g.lineTo(k.x + x, y);
          else g.moveTo(k.x, y);
        }
        g.strokeStyle = rgba(k.c, 0.95);
        g.lineWidth = 1;
        g.stroke();
        if (k.bottom - k.top > 12) {  // bubbles rise to it
          g.strokeStyle = rgba(T.ink, 0.35);
          k.bx.forEach((f, j) => {
            const y = k.bottom - 3 - ((t * 0.18 * (1 + j * 0.3) + j * 0.27) % 1) * (k.bottom - k.top - 6);
            g.beginPath();
            g.arc(k.x + 4 + f * (k.w - 8) + Math.sin(t * 2 + j) * 1, y, 1.1, 0, TAU);
            g.stroke();
          });
        }
        g.restore();
      });
      if (mem.last) {
        g.fillStyle = rgba(T.orange, 0.5 + 0.5 * Math.sin(t * 2.4));
        g.fillRect(mem.last.x, mem.last.y - 2, mem.last.w, 2);
      }
    },
  };

  DRAW.guard = {  // 14: commands from each tool into the Ward gate; out the far side, allowed in one stream, refused by rule
    base(g, d, w, h, T, mem) {
      mem.in = [];
      mem.ok = null;
      mem.no = [];
      mem.gate = null;
      mem.quiet = true;
      if (!d.known) return waiting(g, w, h, T, "waiting for a command at the gate");
      const pad = 14, narrow = w < 330, hist = h >= 170 && d.minutes.length ? 30 : 0, total = d.allowed + d.refused;
      const top = pad, bot = h - pad - (hist ? hist + 14 : 0), sw = narrow ? 44 : Math.round(clamp(w * 0.2, 76, 130));
      const gx = pad + sw + Math.max(26, Math.round(w * 0.07)), gh = Math.max(20, Math.min(bot - top - 8, 96)), gy = (top + bot) / 2 + 4;
      // the source: the hour's commands by tool
      txt(g, "commands", pad, top + 7, T.muted, 9);
      txt(g, fmt(total), pad, top + 24, T.ink, 14, "left", 500);
      const tools = narrow ? [] : d.tools.slice(0, Math.max(0, Math.floor((bot - top - 38) / 15))), maxT = Math.max(1, ...tools.map((x) => x.n));
      tools.forEach((x, i) => {
        const y = top + 44 + i * 15;
        g.fillStyle = rgba(T.muted, 0.1);
        g.fillRect(pad, y - 9, Math.max(1, Math.round((sw * x.n) / maxT)), 12);
        g.font = `400 9px ${FONT}`;
        const nstr = fmt(x.n);
        txt(g, fit(g, x.tool, sw - 12 - g.measureText(nstr).width), pad + 4, y, T.muted, 9);
        txt(g, nstr, pad + sw - 4, y, T.ink, 9, "right");
        const P = link(pad + sw, y - 3, gx, gy - gh * 0.3 + (tools.length > 1 ? (i * gh * 0.6) / (tools.length - 1) : gh * 0.3));
        g.strokeStyle = rgba(T.muted, 0.22);
        g.lineWidth = 1 + (3 * x.n) / maxT;
        g.beginPath();
        curve(g, P);
        g.stroke();
        mem.in.push({P, n: x.n});
      });
      if (!tools.length) {
        const P = link(pad + sw, gy, gx, gy);
        g.strokeStyle = rgba(T.muted, 0.22);
        g.lineWidth = 3;
        g.beginPath();
        curve(g, P);
        g.stroke();
        mem.in.push({P, n: 1});
      }
      g.lineWidth = 1;
      // the gate: two posts and a portcullis between
      const gw = 12, g0 = Math.round(gy - gh / 2);
      g.fillStyle = rgba(T.red, 0.06);
      g.fillRect(gx, g0, gw, gh);
      g.fillStyle = T.muted;
      g.fillRect(gx, g0 - 3, 2, gh + 6);
      g.fillRect(gx + gw - 2, g0 - 3, 2, gh + 6);
      g.fillStyle = rgba(T.muted, 0.45);
      for (let y = g0 + 4; y < g0 + gh - 2; y += 6) g.fillRect(gx + 2, y, gw - 4, 1);
      g.fillStyle = rgba(T.muted, 0.35);  // the portcullis's uprights, spiked at the foot
      for (const x of [gx + 4, gx + gw - 5]) {
        g.fillRect(x, g0, 1, gh - 2);
        g.fillRect(x - 1, g0 + gh - 3, 3, 1);
      }
      g.fillStyle = T.muted;  // the lintel
      g.fillRect(gx - 3, g0 - 4, gw + 6, 2);
      txt(g, "Ward", gx + gw / 2, g0 - 9, T.red, 9, "center", 500);
      mem.gate = {x: gx, y: g0, w: gw, h: gh};
      // out: the allowed stream to its bin, the refused ones to a bin per rule
      const ox = gx + gw, rx0 = ox + Math.max(28, Math.round(w * 0.08)), rx1 = w - pad, rw = Math.max(20, rx1 - rx0);
      const ah = 34, aw = Math.max(1.5, Math.min(5, (5 * d.allowed) / Math.max(1, total)));
      box(g, rx0, top, rw, ah, T.green, 0.05);
      txt(g, "allowed", rx0 + 8, top + 13, T.green, 9, "left", 500);
      txt(g, fmt(d.allowed), rx0 + 8, top + 28, T.ink, 12, "left", 500);
      if (total) txt(g, `${pct(d.allowed, total)}%`, rx1 - 8, top + 28, T.muted, 9, "right");
      mem.ok = link(ox, gy - 4, rx0, top + ah / 2);
      g.strokeStyle = rgba(T.green, 0.3);
      g.lineWidth = aw;
      g.beginPath();
      curve(g, mem.ok);
      g.stroke();
      g.lineWidth = 1;
      const ry = top + ah + 18;
      txt(g, "refused", rx0, ry, T.red, 9, "left", 500);
      txt(g, fmt(d.refused), rx1, ry, T.ink, 9, "right", 500);
      const R = d.rules.length ? d.rules : d.refused ? [{rule: "no rule named", n: d.refused, last: ""}] : [];
      g.font = `400 9px ${FONT}`;
      if (!R.length) txt(g, fit(g, "nothing refused this hour", rw), rx0, ry + 16, T.muted, 9);
      const bh = Math.min(30, (bot - ry - 6) / Math.max(1, R.length)), maxN = Math.max(1, ...R.map((x) => x.n));
      if (bh >= 13) {
        R.forEach((x, i) => {
          const by = ry + 6 + i * bh, two = bh >= 26, mid = by + (bh - 3) / 2;
          g.fillStyle = rgba(T.red, 0.05);
          g.fillRect(rx0, by, rw, bh - 3);
          g.strokeStyle = rgba(T.red, 0.5);
          g.lineWidth = 1;
          g.strokeRect(Math.round(rx0) + 0.5, Math.round(by) + 0.5, Math.round(rw) - 1, Math.round(bh - 3) - 1);
          g.font = `500 9px ${FONT}`;
          const nstr = fmt(x.n), nwid = g.measureText(nstr).width;
          txt(g, nstr, rx1 - 7, (two ? by + 11 : mid + 3), T.red, 9, "right", 500);
          g.font = `500 9px ${FONT}`;
          const rl = fit(g, x.rule, rw - nwid - 24), rlw = g.measureText(rl).width;
          txt(g, rl, rx0 + 7, two ? by + 11 : mid + 3, T.ink, 9, "left", 500);
          if (!two && x.tag && x.pc && T[x.pc]) {  // one line: the project goes after the rule, when it fits whole
            g.font = `500 9px ${FONT}`;
            if (rx0 + 7 + rlw + 10 + g.measureText(x.tag).width < rx1 - nwid - 14) txt(g, x.tag, rx0 + 7 + rlw + 10, mid + 3, T[x.pc], 9, "left", 500);
          }
          if (two && x.last) {  // the last command it refused; in All mode led by its project, in the project's colour
            g.font = `500 9px ${FONT}`;
            const tag = x.tag && x.pc && T[x.pc] ? x.tag : "", tw = tag ? g.measureText(tag).width + 6 : 0;
            if (tag) txt(g, tag, rx0 + 7, by + 22, T[x.pc], 9, "left", 500);
            g.font = `400 9px ${FONT}`;
            txt(g, fit(g, x.last, rw - 14 - tw), rx0 + 7 + tw, by + 22, T.muted, 9);
          }
          if (x.pc && T[x.pc]) {
            g.fillStyle = T[x.pc];
            g.fillRect(rx0 + 1, by + 1, 2, bh - 5);
          }
          const P = link(ox, gy + 4, rx0, mid);
          g.strokeStyle = rgba(T.red, 0.3);
          g.lineWidth = 1 + (2.5 * x.n) / maxN;
          g.beginPath();
          curve(g, P);
          g.stroke();
          mem.no.push({P, n: x.n});
        });
        g.lineWidth = 1;
      }
      // the hour, a minute a bar: allowed under, refused on top
      if (hist) {
        const hy1 = h - pad - 8, hy0 = hy1 - hist + 8, M = d.minutes, bw = (w - 2 * pad) / 60, off = 60 - M.length;
        const peak = Math.max(1, ...M.map((m) => m[0] + m[1]));
        hline(g, pad, w - pad, hy1, T.line);
        M.forEach(([a, r], i) => {
          const x = Math.round(pad + (off + i) * bw), bwi = Math.max(1, Math.round(bw) - 1), ah2 = Math.round(((hy1 - hy0) * a) / peak);
          g.fillStyle = rgba(T.green, 0.45);
          if (ah2 > 0) g.fillRect(x, hy1 - ah2, bwi, ah2);
          if (r > 0) {
            const rh = Math.max(2, Math.round(((hy1 - hy0) * r) / peak));
            g.fillStyle = T.red;
            g.fillRect(x, hy1 - ah2 - rh, bwi, rh);
          }
        });
        txt(g, "−60 min", pad, h - pad + 5, T.muted, 9);
        txt(g, "now", w - pad, h - pad + 5, T.muted, 9, "right");
      }
      mem.share = total ? d.refused / total : 0;
      mem.quiet = false;
    },
    live(g, d, w, h, t, T, mem) {
      const G = mem.gate;
      if (!G || !mem.in.length) return;
      const sumIn = mem.in.reduce((a, x) => a + x.n, 0), sumNo = mem.no.reduce((a, x) => a + x.n, 0);
      const pick = (L, sum, u) => {
        let acc = 0;
        for (const x of L) if ((acc += x.n) >= u * sum) return x;
        return L[L.length - 1];
      };
      let flash = 0;
      for (let i = 0; i < 14; i++) {  // a command each: in on its tool's pipe, through the gate, out by its verdict
        const ph = t * 0.2 + i / 14, cyc = Math.floor(ph), u = ph - cyc, rnd = seed(`${i}:${cyc}`);
        const r1 = rnd(), r2 = rnd(), r3 = rnd(), no = mem.no.length && mem.share > 0 && r1 < Math.max(mem.share, 0.05);
        let p, c;
        if (u < 0.5) {
          p = along(pick(mem.in, sumIn, r2).P, u * 2);
          c = T.muted;
        } else {
          p = along(no ? pick(mem.no, sumNo, r3).P : mem.ok, (u - 0.5) * 2);
          c = no ? T.red : T.green;
          if (no && u < 0.6) flash = Math.max(flash, 1 - (u - 0.5) * 10);
        }
        g.fillStyle = c;
        g.fillRect(p[0] - 1.5, p[1] - 1.5, 3, 3);
      }
      if (flash > 0) {
        g.fillStyle = rgba(T.red, 0.35 * flash);
        g.fillRect(G.x, G.y, G.w, G.h);
      }
      const sy = G.y + ((t * 0.45) % 1) * G.h;  // a glint passes down the portcullis
      g.fillStyle = rgba(T.ink, 0.35);
      g.fillRect(G.x + 2, Math.round(sy), G.w - 4, 1);
    },
  };

  const PLACEHOLDER = {base: (g, d, w, h, T) => waiting(g, w, h, T, "drawing soon")};

  // ---------- T-3739: the layout. Order, span 1-3, height 1-3, hidden, style and motion, as sampo.board keeps them ----------
  // layoutOf mends anything (a snapshot's layout, junk, null) into the whole fourteen-panel layout: what the server
  // refuses, the page never shows half-applied; what it leaves out is the built-in.
  const LAYOUT_KEYS = PANELS.map((P) => P.key), STYLES = ["card", "flat"], MOTIONS = ["full", "calm", "still"];
  const size3 = (v) => (typeof v === "number" && isFinite(v) ? Math.max(1, Math.min(3, Math.round(v))) : 1);
  function layoutOf(x) {
    const o = obj(x), ps = obj(o.panels), seen = new Set(), panels = {};
    const order = arr(o.order).filter((k) => LAYOUT_KEYS.includes(k) && !seen.has(k) && seen.add(k));
    for (const k of LAYOUT_KEYS) if (!seen.has(k)) order.push(k);
    for (const k of LAYOUT_KEYS) {
      const q = obj(ps[k]);
      panels[k] = {span: size3(q.span), height: size3(q.height), hidden: q.hidden === true};
    }
    return {version: 1, order, panels, style: STYLES.includes(o.style) ? o.style : "card",
      motion: MOTIONS.includes(o.motion) ? o.motion : "full"};
  }
  function layoutMove(L, key, index) {  // a new layout with key at index (clamped) in the order
    const M = layoutOf(L);
    if (!LAYOUT_KEYS.includes(key)) return M;
    const order = M.order.filter((k) => k !== key), i = Math.round(Number(index));
    order.splice(isFinite(i) ? Math.max(0, Math.min(order.length, i)) : order.length, 0, key);
    M.order = order;
    return M;
  }
  // a drag snaps to whole columns (a column and the gap after it), at most the grid's; to whole heights, 1 to 3
  function snapSpan(w, colW, gap, cols) {
    const g = num(gap), unit = num(colW) + g, n = Math.max(1, Math.floor(num(cols, 1)));
    return unit > 0 ? Math.max(1, Math.min(n, Math.round((num(w) + g) / unit))) : 1;
  }
  const snapHeight = (px, base) => (num(base) > 0 ? Math.max(1, Math.min(3, Math.round(num(px) / base))) : 1);

  function mount(el, opts) {
    if (!el || typeof document === "undefined") return null;
    const doc = document, win = window, O = obj(opts);  // O.save(layout, scope, done(ok)), O.reset(scope, done(ok)): T-3739
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
    const btn = (cls, parent, text, label) => {
      const b = mk("button", cls, parent, text);
      b.type = "button";
      if (label) { b.setAttribute("aria-label", label); b.title = label; }
      return b;
    };
    el.replaceChildren();
    // T-3739: the layout bar. Edit turns on the edit mode (drag ⠿ to reorder, the corner to resize, × to hide); the
    // hidden panels sit here as + buttons in either mode, so one is never lost
    const tools = mk("div", "bd-tools", el);
    tools.setAttribute("role", "toolbar");
    tools.setAttribute("aria-label", "Board layout");
    const editB = btn("bd-edit", tools, "Edit layout");
    editB.setAttribute("aria-pressed", "false");
    const restoreBox = mk("span", "bd-restores", tools);
    const eds = mk("span", "bd-eds bd-x", tools);
    const scopeB = btn("bd-scope", eds, ""), styleB = btn("bd-style", eds, ""), motionB = btn("bd-motion", eds, "");
    const resetB = btn("bd-reset", eds, "Reset", "Reset this layout to the one it falls back to");
    const sayEl = mk("span", "bd-say", tools, "");
    sayEl.setAttribute("role", "status");
    const panels = PANELS.map((P, i) => {
      const sec = mk("section", "bd-p", el);
      sec.dataset.k = P.key;
      sec.dataset.accent = P.accent;
      sec.dataset.span = "1";
      sec.dataset.h = "1";
      if (P.wide) sec.dataset.wide = "1";
      sec.setAttribute("aria-labelledby", "bd-t-" + P.key);
      const head = mk("header", "bd-h", sec);
      const drag = btn("bd-drag bd-x", head, "⠿", `Move ${P.name}: drag onto another panel`);
      const chip = mk("span", "bd-chip", head, String(i + 1).padStart(2, "0"));
      chip.setAttribute("aria-hidden", "true");
      const h2 = mk("h2", "bd-t", head, P.name);
      h2.id = "bd-t-" + P.key;
      const wh = mk("span", "bd-w", h2, " · " + P.what);
      const label = mk("span", "bd-l", head, "");
      const tw = mk("span", "bd-pt bd-x", head);
      const ups = [btn("bd-up", tw, "‹", `Move ${P.name} earlier`), btn("bd-down", tw, "›", `Move ${P.name} later`)];
      const hideB = btn("bd-hide", tw, "×", `Hide ${P.name}`);
      const grip = btn("bd-grip bd-x", sec, "", `Resize ${P.name}: drag, or the arrow keys`);
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
        w: 0, h: 0, sx: 1, sy: 1, vis: true, d: null, dk: "", base: null, off: null, mem: {}, chip, drag, ups, hideB, grip};
    });
    const byBox = new Map(panels.map((p) => [p.box, p]));
    const bySec = new Map(panels.map((p) => [p.sec, p]));
    const byKey = new Map(panels.map((p) => [p.key, p]));
    let lastPaint = 0, raf = 0, still = false, shown = true, frames = 0, hidden = !!doc.hidden, held = false;
    // the layout in force; pend: the one this page changed to and has not yet seen come back in a snapshot (it wins
    // until then, or until `until`); gest: a drag or a resize in progress
    let L = layoutOf(null), from = "builtin", editing = false, all = false, pick = "project", pend = "", until = 0, saveT = 0;
    let gest = null;

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
          else waiting(o, p.w, p.h, T, idle(p.dk));
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
      // T-3715: at most ~30 fps, half the canvas work of a 60 Hz loop; T-3739: motion calm, ~15
      if (now - lastPaint < (L.motion === "calm" ? 66 : 30)) { raf = win.requestAnimationFrame(frame); return; }
      lastPaint = now;
      const t = now / 1000;
      // T-3731: a panel below the fold (fourteen panels pass a desktop's height) still gets its first frame, once, so it is
      // drawn before it scrolls in and the board reads painted; after that only the visible ones move
      for (const p of panels) if (p.vis || p.cv.dataset.painted !== "1") paint(p, t);
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

    // ---------- T-3739: the layout on the page, and its edit mode ----------
    const wide = win.matchMedia ? [win.matchMedia("(min-width:700px)"), win.matchMedia("(min-width:1200px)")] : [];
    const cols = () => 1 + wide.filter((q) => q.matches).length;  // board.css's breakpoints: 1, 2 or 3 columns
    const scope = () => (all ? "global" : pick);
    const tell = (text, bad) => { sayEl.textContent = text; sayEl.classList.toggle("bad", !!bad); };
    const named = (k) => byKey.get(k).name;
    let restoreKey = "";

    function fillRow() {  // the last panel takes the rest of its row rather than leave empty cells
      const n = cols();
      let at = 0, last = null, lastAt = 0, end = 0;
      for (const k of L.order) {
        const p = byKey.get(k);
        p.sec.style.gridColumn = "";
        delete p.sec.dataset.fill;
        if (L.panels[k].hidden) continue;
        const s = Math.min(n, Math.max(L.panels[k].span, p.sec.dataset.wide && n >= 3 ? 2 : 1));  // T-3731: a wide panel takes two cells
        if (at + s > n) at = 0;
        last = p; lastAt = at; end = at + s; at = end % n;
      }
      if (last && n > 1 && end < n) {
        last.sec.style.gridColumn = lastAt === 0 ? "1 / -1" : "span " + (n - lastAt);
        last.sec.dataset.fill = "1";
      }
    }
    for (const q of wide) if (q.addEventListener) q.addEventListener("change", fillRow);

    function renderTools() {
      if (editing) el.dataset.edit = "1"; else delete el.dataset.edit;
      el.dataset.style = L.style;
      el.dataset.motion = L.motion;
      editB.setAttribute("aria-pressed", String(editing));
      editB.textContent = editing ? "Done" : "Edit layout";
      editB.title = "This layout comes from " + (from === "project" ? "this project" : from === "global" ? "every project's" : "the built-in");
      scopeB.textContent = scope() === "global" ? "for every project" : "for this project";
      scopeB.disabled = all;
      scopeB.title = all ? "All mode saves the layout every project falls back to" : "Save for this project, or for every project";
      styleB.textContent = "style · " + L.style;
      motionB.textContent = "motion · " + L.motion;
      const gone = L.order.filter((k) => L.panels[k].hidden);
      if (gone.join() !== restoreKey) {  // rebuilt only when the list changes, so a focused button stays
        restoreKey = gone.join();
        restoreBox.replaceChildren(...gone.map((k) => {
          const b = btn("", null, "+ " + named(k), `Show ${named(k)} again`);
          b.dataset.restore = k;
          return b;
        }));
      }
    }

    function apply(next) {
      const was = L;
      L = next;
      const cur = Array.from(el.children).filter((n) => bySec.has(n)).map((n) => bySec.get(n).key);
      if (cur.join() !== L.order.join()) {
        const f = doc.activeElement;
        for (const k of L.order) el.appendChild(byKey.get(k).sec);
        if (f && f !== doc.activeElement && f.isConnected && f.focus) f.focus({preventScroll: true});
      }
      let n = 0;
      for (const k of L.order) {
        const p = byKey.get(k), q = L.panels[k];
        if (p.sec.hidden !== q.hidden) p.sec.hidden = q.hidden;
        if (p.sec.dataset.span !== String(q.span)) p.sec.dataset.span = String(q.span);
        if (p.sec.dataset.h !== String(q.height)) p.sec.dataset.h = String(q.height);
        const say = String(++n).padStart(2, "0");
        if (!q.hidden && p.chip.textContent !== say) p.chip.textContent = say;
        if (q.hidden) { n--; p.vis = false; }
        p.ups[0].disabled = n <= 1 && !q.hidden;
        p.grip.setAttribute("aria-valuetext", `span ${q.span}, height ${q.height}`);
      }
      const lastShown = L.order.filter((k) => !L.panels[k].hidden).pop();
      if (lastShown) byKey.get(lastShown).ups[1].disabled = true;
      for (const k of L.order) if (k !== lastShown) byKey.get(k).ups[1].disabled = false;
      still = held || L.motion === "still";
      fillRow();
      renderTools();
      if (was.motion !== L.motion || was.style !== L.style) invalidate(); else kick();
    }

    function save() {
      saveT = 0;
      if (typeof O.save !== "function") return;
      const mine = pend, where = scope();
      tell("saving…");
      O.save(layoutOf(L), where, (ok) => {
        if (pend === mine) until = Date.now() + (ok ? 4000 : 1500);  // not echoed by then: the server's layout wins
        tell(ok ? "saved " + (where === "global" ? "for every project" : "for this project") : "not saved", !ok);
      });
    }
    function change(next, soon) {  // the page shows it now; the save waits for the last of a burst of changes
      apply(next);
      pend = JSON.stringify(L);
      until = Infinity;
      if (saveT) clearTimeout(saveT);
      saveT = soon === false ? 0 : setTimeout(save, soon ? 0 : 450);
    }
    function withPanel(k, f) {
      const M = layoutOf(L);
      f(M.panels[k]);
      return M;
    }
    function setEditing(on) {
      editing = !!on;
      if (!editing && saveT) { clearTimeout(saveT); save(); }
      renderTools();
    }
    function neighbour(k, d) {  // the next shown panel before (-1) or after (+1) k
      const shown_ = L.order.filter((x) => !L.panels[x].hidden || x === k), i = shown_.indexOf(k) + d;
      return i >= 0 && i < shown_.length ? shown_[i] : null;
    }

    editB.addEventListener("click", () => setEditing(!editing));
    scopeB.addEventListener("click", () => {  // where the next change saves; nothing is written until then
      pick = pick === "project" ? "global" : "project";
      renderTools();
      tell("changes save " + scopeB.textContent);
    });
    styleB.addEventListener("click", () => change(Object.assign(layoutOf(L), {style: L.style === "card" ? "flat" : "card"})));
    motionB.addEventListener("click", () => change(Object.assign(layoutOf(L), {motion: MOTIONS[(MOTIONS.indexOf(L.motion) + 1) % 3]})));
    resetB.addEventListener("click", () => {
      if (saveT) clearTimeout(saveT);
      saveT = 0;
      const where = scope();
      apply(layoutOf(null));
      pend = JSON.stringify(L);
      until = Infinity;
      if (typeof O.reset !== "function") return;
      tell("resetting…");
      O.reset(where, (ok) => {  // the scope falls back to the next one: its layout comes in the next snapshot
        until = Date.now() + (ok ? 1500 : 0);
        tell(ok ? "reset" : "not reset", !ok);
      });
    });
    restoreBox.addEventListener("click", (e) => {
      const b = e.target.closest ? e.target.closest("button[data-restore]") : null;
      if (!b || !byKey.has(b.dataset.restore)) return;
      const k = b.dataset.restore;
      change(withPanel(k, (q) => { q.hidden = false; }));
      (restoreBox.querySelector("button") || (editing ? byKey.get(k).hideB : editB)).focus();
    });
    for (const p of panels) {
      const k = p.key;
      p.hideB.addEventListener("click", () => {
        change(withPanel(k, (q) => { q.hidden = true; }));
        editB.focus();
      });
      p.ups.forEach((b, i) => b.addEventListener("click", () => {
        const o = neighbour(k, i ? 1 : -1);
        if (!o) return;
        const rest = L.order.filter((x) => x !== k);
        change(layoutMove(L, k, rest.indexOf(o) + i));
      }));
      p.grip.addEventListener("keydown", (e) => {
        const d = {ArrowRight: [1, 0], ArrowLeft: [-1, 0], ArrowDown: [0, 1], ArrowUp: [0, -1]}[e.key];
        if (!d || !editing) return;
        e.preventDefault();
        const q = L.panels[k];
        change(withPanel(k, (m) => { m.span = size3(q.span + d[0]); m.height = size3(q.height + d[1]); }));
      });
      p.drag.addEventListener("pointerdown", (e) => grab(e, p, "move"));
      p.grip.addEventListener("pointerdown", (e) => grab(e, p, "size"));
    }

    function grab(e, p, kind) {
      if (!editing || gest || e.button !== 0) return;
      e.preventDefault();
      const handle = kind === "move" ? p.drag : p.grip, r = p.sec.getBoundingClientRect(), q = L.panels[p.key];
      try { handle.setPointerCapture(e.pointerId); } catch (err) { /* a synthetic pointer: the moves still come here */ }
      const n = cols(), gap = parseFloat(win.getComputedStyle(el).columnGap) || 0;
      gest = {kind, p, handle, id: e.pointerId, x: e.clientX, y: e.clientY, w: r.width, h: r.height, q: Object.assign({}, q),
        n, gap, colW: (el.clientWidth - (n - 1) * gap) / n, unit: r.height / q.height, mark: null, before: L};
      p.sec.classList.add(kind === "move" ? "bd-moving" : "bd-sizing");
      handle.addEventListener("pointermove", drag_);
      handle.addEventListener("pointerup", drop);
      handle.addEventListener("pointercancel", drop);
      handle.addEventListener("lostpointercapture", drop);
    }
    function target(G, x, y) {  // the shown panel under the pointer, and which half of it
      const hit = doc.elementFromPoint(x, y), t = hit && hit.closest ? hit.closest(".bd-p") : null;
      if (!t || t.parentNode !== el || !bySec.has(t) || t === G.p.sec) return null;
      const r = t.getBoundingClientRect();
      return {p: bySec.get(t), after: x > r.left + r.width / 2};
    }
    function mark(t) {
      if (gest.mark && (!t || gest.mark !== t.p.sec)) delete gest.mark.dataset.drop;
      gest.mark = t ? t.p.sec : null;
      if (t) t.p.sec.dataset.drop = t.after ? "after" : "before";
    }
    function sized(e) {
      const G = gest, dx = e.clientX - G.x, dy = e.clientY - G.y;
      const sp = G.n > 1 ? snapSpan(G.w + dx, G.colW, G.gap, G.n) : G.q.span;
      return {span: sp === Math.min(G.q.span, G.n) ? G.q.span : sp, height: snapHeight(G.h + dy, G.unit)};  // a span wider than the grid stays
    }
    function drag_(e) {
      if (!gest || e.pointerId !== gest.id) return;
      if (gest.kind === "move") return mark(target(gest, e.clientX, e.clientY));
      const s = sized(e), q = L.panels[gest.p.key];
      if (s.span !== q.span || s.height !== q.height) apply(withPanel(gest.p.key, (m) => Object.assign(m, s)));  // live, saved on release
    }
    function drop(e) {
      const G = gest;
      if (!G || e.pointerId !== G.id) return;
      gest = null;
      for (const t of ["pointermove", "pointerup", "pointercancel", "lostpointercapture"]) G.handle.removeEventListener(t, t === "pointermove" ? drag_ : drop);
      G.p.sec.classList.remove("bd-moving", "bd-sizing");
      if (G.mark) delete G.mark.dataset.drop;
      if (e.type !== "pointerup") return G.kind === "size" ? apply(G.before) : undefined;  // cancelled: as it was
      if (G.kind === "move") {
        const t = target(G, e.clientX, e.clientY);
        if (!t) return;
        const rest = L.order.filter((x) => x !== G.p.key);
        change(layoutMove(L, G.p.key, rest.indexOf(t.p.key) + (t.after ? 1 : 0)));
      } else {
        const now = L.panels[G.p.key];
        if (now.span !== G.q.span || now.height !== G.q.height) change(layoutOf(L));
      }
    }
    el.addEventListener("keydown", (e) => {
      if (e.key !== "Escape" || !editing || gest) return;
      setEditing(false);
      editB.focus();
    });

    // a snapshot's layout (sampo.board's layout_view: the layout and the scope it came from); isAll: the page shows All
    function layout(x, isAll) {
      all = !!isAll;
      const v = obj(x), next = layoutOf(v), key = JSON.stringify(next);
      from = typeof v.from === "string" ? v.from : "builtin";
      if (pend && (key === pend || Date.now() > until)) pend = "";
      if (pend || gest || key === JSON.stringify(L)) return renderTools();
      apply(next);
    }

    apply(L);
    kick();
    return {
      update,
      layout,
      still(on) { held = !!on; still = held || L.motion === "still"; kick(); },
      show(on) { shown = !!on; kick(); },
    };
  }

  return {PANELS, KINDS, STAGES, TOKENS, DRAW, boardModel, projectList, mount, idle, layoutOf, layoutMove, snapSpan, snapHeight};
})();
if (typeof module !== "undefined" && module.exports) module.exports = SampoBoard;
else window.SampoBoard = SampoBoard;  // a top-level const is no window property: live.js finds it here
