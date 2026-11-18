// T-2154: runs site/genesis/genesis.js against a stub DOM; prints what the scorecard table did with a hostile why.
const fs = require("fs"), vm = require("vm"), dir = process.argv[2], texts = [], html = [];
const data = JSON.parse(fs.readFileSync(dir + "/genesis.json", "utf8"));
function el() {
  const e = {children: [], classList: {add() {}, remove() {}, toggle() {}}, style: {}, dataset: {}, value: "", addEventListener(t, f) { (this.h = this.h || {})[t] = f; },
    setAttribute(k, v) { (this.attrs = this.attrs || {})[k] = String(v); }, getAttribute() { return null; }, closest() { return null; }, querySelector() { return null; },
    querySelectorAll() { return []; }, appendChild(c) { if (!c || typeof c !== "object" || Array.isArray(c)) throw new TypeError("appendChild: not a Node"); this.children.push(c); return c; }, append(...c) { this.children.push(...c); },
    scrollIntoView() { scrolled++; }};
  for (const k of ["textContent", "innerHTML"]) {
    let v = "";
    Object.defineProperty(e, k, {get() { return v; }, set(x) { v = String(x); if (k === "textContent" && v === "") this.children = []; (k === "innerHTML" ? html : texts).push(v); }});
  }
  return e;
}
let CAT = [], lastEls = {}, scrolled = 0, fetched = [], mode = "", aborted = [];
// the catalogue as the export writes it: shards of 500 behind a small index
function catFile(u) {
  const per = 500, m = /moons-catalogue-(\d+)\.json/.exec(u);
  if (m) return CAT.slice((m[1] - 1) * per, m[1] * per);
  const tiers = {}, shards = [];
  CAT.forEach(r => { tiers[r.tier] = (tiers[r.tier] || 0) + 1; });
  for (let k = 0; k * per < CAT.length; k++) shards.push({file: "moons-catalogue-" + String(k + 1).padStart(3, "0") + ".json", n: Math.min(per, CAT.length - k * per),
    tiers: [...new Set(CAT.slice(k * per, k * per + per).map(r => r.tier))]});
  return {total: CAT.length, per, tiers, families: new Set(CAT.map(r => r.family)).size, shards};
}
async function page(extra, hash) {
  texts.length = html.length = 0;
  const D = Object.assign({}, data, extra), els = {}, loc = {hash: hash || "", pathname: "/genesis/", search: ""};
  const ctx = {document: {documentElement: el(), getElementById: id => els[id] = els[id] || el(), querySelector: () => null,
    addEventListener() {}, createElement: el, createTextNode: t => ({textContent: t})}, location: loc, localStorage: {},
    history: {replaceState(s, t, u) { const i = String(u).indexOf("#"); loc.hash = i < 0 ? "" : String(u).slice(i); }},
    matchMedia: () => ({matches: false}), AbortController, console, clearTimeout,
    setTimeout: (f, ms) => setTimeout(f, ms >= 20000 ? 30 : ms),  // the page's 20 s timeout, shortened
    fetch: async (u, o) => {
      if (mode === "stall" && /genesis\.json/.test(u)) return new Promise(() => {});  // the data, still on its way
      if (/catalogue/.test(u)) {
        fetched.push(u);
        if (mode === "hang" && /-\d+\.json/.test(u)) return new Promise((_, no) => o.signal.addEventListener("abort", () => { aborted.push(u); no(new Error("aborted")); }));
        if (mode === "down" && /-\d+\.json/.test(u)) return {ok: false, status: 503};
      }
      return {ok: true, json: async () => /catalogue/.test(u) ? catFile(u) : D};
    }};
  lastEls = els;
  vm.runInNewContext(fs.readFileSync(dir + "/genesis.js", "utf8"), ctx);
  await new Promise(r => setTimeout(r, 50));
  return {texts: texts.slice(), html: html.join("\n"), docs: !!(els["docs-body"] && els["docs-body"].innerHTML),
    pressed: ((els["vs-kalevala"] || {}).attrs || {})["aria-pressed"], city: (els["city-art"] || {}).innerHTML || "", hash: loc.hash};
}
// T-2852: `genesis_page.js DIR scale` prints what the Moons part did with moons_scale: a hostile family, then none
async function scale() {
  const bad = "<img src=x onerror=alert(1)>", says = (x, re) => x.texts.some(t => re.test(t));
  const s = await page({moons_scale: {funnel: {generated: 10000, write: {schema: 3}, dups: {exact: 40, embed: 1200, shingle: 7},
    rejected: {kill: 5000, tier: 2600, novelty: 900}, merged: 150, kept: 103, pending: 0},
    families: [{id: "F-007", size: 12, lead: "Lead " + bad, today: "today it can't", with_this: "with this it can, on a large local model; repo jarvis (1,853)",
      tier: "massive", cold: "extreme", wild: 5, block: "M-61", stage: "kill", evidence: "ev " + bad, kinds: ["kill"]}],
    families_total: 340}});
  const n = await page({moons_scale: undefined});
  console.log(JSON.stringify({text: s.texts.some(t => t.includes("Lead " + bad)), markup: s.html.includes("onerror"),
    funnel: says(s, /10,000 generated/) && says(s, /40 exact/) && says(s, /1,200 by embedding/) && says(s, /5,000 at the kill test/)
      && says(s, /150 merged/) && says(s, /103 kept/),
    lead: says(s, /today it can't/) && says(s, /with this it can/), tiers: says(s, /massive.*extreme/), stage: says(s, /kill test/),
    evidence: s.texts.some(t => t.includes("ev " + bad)), more: says(s, /1 of 340 families/), none: says(n, /no funnel run yet/i),
    // T-2895: the export wrote the role; the page swaps no words, so a repo named jarvis shows as itself
    jarvis: says(s, /on a large local model; repo jarvis \(1,853\)/)}));
}
// T-3047: `genesis_page.js DIR catalogue`: 10,000 kept moons paged 50 at a time, searched and filtered by grid, tier and family
async function catalogue() {
  const bad = "<img src=x onerror=alert(1)>", tiers = ["extreme", "massive", "high"];
  CAT = Array.from({length: 10000}, (_, i) => ({id: "C-" + String(i + 1).padStart(6, "0"), title: i === 7 ? "Needle " + bad : "Moon " + i,
    today: "Today you can't " + i, with_this: "With this, you can " + i, grid: i % 3 ? "user" : "sampo", tier: tiers[i % 3],
    wild: i % 5 + 1, family: "F-" + String(Math.floor(i / 4) + 1).padStart(6, "0"), lead: i % 4 === 0, drawn: i === 4 ? "M-09" : null}));
  CAT.forEach((r, i) => { r.tier = tiers[Math.floor(i / 3334)]; });  // sorted by tier, as the export is
  delete CAT[10].grid;  // a row with no grid is the user's
  const S = {funnel: {generated: 12000, write: {}, dups: {}, rejected: {}, merged: 0, kept: 10000, pending: 0}, families: [], families_total: 0,
    catalogue: {file: "moons-catalogue.json", total: 10000}};
  const s = await page({moons_scale: S}), says = re => texts.some(t => re.test(t)), E = id => lastEls[id];
  const fire = async (id, v, type) => { texts.length = 0; E(id).value = v; E(id).h[type || "input"](); await new Promise(r => setTimeout(r, 250)); };
  const res = {total: s.texts.some(t => /Showing 1.50 of 10,000 kept moons/.test(t)), markup: s.html.includes("onerror")};
  res.rows = E("cat-rows").children.length;
  res.index_first = fetched.length === 2 && /moons-catalogue\.json$/.test(fetched[0]) && /-001\.json$/.test(fetched[1]);  // not 10,000 rows
  await fire("cat-q", "needle");
  res.search = says(/Showing 1.1 of 1 kept moon\b/) && texts.some(t => t.includes("Needle " + bad)) && !html.join("").includes("onerror");
  await fire("cat-q", "");
  await fire("cat-grid", "sampo", "change");
  res.grid = says(/of 3,334 kept moons/);
  await fire("cat-grid", "user", "change");
  res.user = says(/of 6,666 kept moons/);  // 6,667 rows have a grid of user, one of them with none: all count
  await fire("cat-grid", "", "change"); await fire("cat-tier", "extreme", "change");
  res.tier = says(/of 3,334 kept moons/);
  fetched.length = 0; await fire("cat-tier", "high", "change");
  res.tier_shard = says(/Showing 1.50 of 3,332/) && fetched.length <= 1;  // rows 6668.. live in shard 14, which the earlier searches already cached
  E("cat-next").h.click(); await new Promise(r => setTimeout(r, 100));
  res.scroll = scrolled === 1;  // a page change scrolls to the list's head, a filter doesn't
  await fire("cat-tier", "", "change"); await fire("cat-fam", "F-000002");
  res.family = says(/of 4 kept moons/);
  const wait = () => new Promise(r => setTimeout(r, 100));
  texts.length = 0; await fire("cat-fam", ""); E("cat-next").h.click(); await wait(); texts.length = 0; E("cat-next").h.click(); await wait();
  res.page = says(/Showing 101.150 of 10,000/);
  await fire("cat-q", ""); texts.length = 0; E("cat-rows").h.click({target: {closest: () => ({getAttribute: () => "F-000009"})}}); await wait(); await wait();
  res.famclick = E("cat-fam").value === "F-000009" && says(/of 4 kept moons/);
  res.page_of = E("cat-page").textContent === "Page 1 of 1";
  await fire("cat-fam", ""); await fire("cat-q", "zzzz-nothing");
  res.empty = says(/No kept moon matches/) && E("cat-rows").children.length === 1;
  res.nolabel = !texts.some(t => /^(Today|With this): /.test(t)); await fire("cat-q", "");
  const t0 = Date.now(); await fire("cat-q", "moon 99"); res.fast = Date.now() - t0 < 1500;
  // a shard that never answers: the timeout aborts it and offers a retry; one that errs does the same; a retry then loads
  mode = "hang"; await page({moons_scale: S}); await new Promise(r => setTimeout(r, 300));
  res.timeout = says(/didn't load \(timed out\)/) && E("cat-retry").hidden === false && aborted.length >= 1;
  mode = ""; E("cat-retry").h.click(); await new Promise(r => setTimeout(r, 100));
  res.retry = E("cat-retry").hidden === true && says(/Showing 1.50 of 10,000/);
  mode = "down"; await page({moons_scale: S}); await new Promise(r => setTimeout(r, 100));
  res.down = says(/didn't load \(503\)/) && E("cat-retry").hidden === false;
  console.log(JSON.stringify(res));
}
// T-3266: `genesis_page.js DIR kalevala` prints what the Kalevala view did with the export's kalevala block: hostile text,
// an unscored gen-0 tower, an improving one, a stuck one, a gen and an act; then #tower=api; then no block; then a damaged one
async function kalevala() {
  const bad = "<img src=x onerror=alert(1)>", says = (x, re) => x.texts.some(t => re.test(t));
  const tower = (name, o) => Object.assign({name, aspect: name, sealed: true, generation: 0, drift: null, closed: null, delta: null,
    idle_h: 1, spend: 0, missions: 0, restarted: null, stuck: false, state: "unscored", paused: null, trend: [],
    stage: {stage: "spec", gen: 1, action: "Write gen 1's spec: fm genesis spec", kind: "ok"}}, o);
  const K = {towers: [
    tower("memory", {aspect: "Memory " + bad, paused: "off intent " + bad}),
    tower("api", {aspect: "API surface", generation: 2, closed: 2, delta: 1, trend: [1, 2], state: "improving", spend: 0.12}),
    tower("docs", {aspect: "Docs", generation: 3, closed: 1, delta: 0, trend: [1, 1, 1], state: "flat", stuck: true,
      stage: {error: "its next stage doesn't read: " + bad}})],
    gens: [{n: 1, set: ["memory", "api"], based_on: "root gen-0", at: "2026-11-05T09:00:00Z", verdict: "baseline", base: "gen-1",
      reason: "first gen " + bad, scores: {}, helped: null, new_capabilities: []}],
    acts: [{act: "pause", tower: "memory", reason: "drifted " + bad, undo: "fm kalevala resume memory", at: "2026-11-05T10:00:00Z"}],
    halted: null, rounds: 1, spent_today: 0.12, share: 5};
  const s = await page({kalevala: K}), t = await page({kalevala: K}, "#tower=api");
  const n = await page({kalevala: undefined}), e = await page({kalevala: {error: "Kalevala's manifest doesn't parse: x"}});
  // the opened city: 26 gen-0 towers, unscored, plus two names no tower may have; a #kalevala link lands in the city
  const many = {towers: Array.from({length: 26}, (_, i) => tower("aspect-" + String.fromCharCode(97 + i)))
    .concat([tower("Bad Name"), tower('x" onload="alert(1)')]), gens: [], acts: [], halted: null, rounds: 0, spent_today: 0, share: 5};
  const c = await page({kalevala: many}, "#kalevala"), xs = [...c.city.matchAll(/data-ktower="[^"]*"[^]*?<ellipse cx="([\d.-]+)"/g)].map(m => +m[1]);
  mode = "stall";  // the data still loading: the deep link switches first, so the root tower never flashes
  const w = await page({kalevala: many}, "#kalevala"), wt = await page({kalevala: many}, "#tower=api");
  mode = "";
  console.log(JSON.stringify({text: s.texts.some(x => x.includes("Memory " + bad)) && s.texts.some(x => x.includes("off intent " + bad))
      && s.texts.some(x => x.includes("drifted " + bad)) && s.texts.some(x => x.includes("first gen " + bad))
      && s.texts.some(x => x.includes("its next stage doesn't read: " + bad)),  // one tower's unreadable stage, as text
    city: (c.city.match(/data-ktower="/g) || []).length === 26 && !/Bad Name|onload/.test(c.city) && c.pressed === "true" && c.hash === "#kalevala"
      && xs.length === 26 && xs.slice(1).filter((x, i) => x < xs[i]).length <= 2  // left to right in each of the three rows
      && Math.max(...xs) <= 915,  // the right-most tower clears the scene's edge (the viewport's, at 1280)
    early: w.pressed === "true" && w.hash === "#kalevala" && wt.pressed === "true" && wt.hash === "#tower=api",
    markup: s.html.includes("onerror") || t.html.includes("onerror"),
    honest: says(s, /\bgen 0\b/) && says(s, /no exam yet/i), trend: says(s, /improving/) && says(s, /\+1\b/),
    stuck: says(s, /\bstuck\b/i), paused: says(s, /\bpaused\b/i), stage: says(s, /sampo rune spec/) && !says(s, /\b(?:fm|sampo) genesis\b/),
    spend: says(s, /\$0\.12/), gens: says(s, /baseline/) && says(s, /root gen-0/), acts: says(s, /sampo kalevala resume memory/),
    share: says(s, /\$5\.00/), drawer: says(t, /^Kalevala tower/) && t.texts.includes("API surface") && says(t, /^gen 1 · baseline · first gen/) && !says(t, /towers come from genesis\.json/)
      && t.pressed === "true" && s.pressed !== "true",  // a tower's link lands in the city, behind its drawer
    none: says(n, /Kalevala hasn't opened a tower yet/), error: says(e, /manifest doesn't parse: x/)}));
}
(async () => {
  if (process.argv[3] === "scale") return scale();
  if (process.argv[3] === "catalogue") return catalogue();
  if (process.argv[3] === "kalevala") return kalevala();
  const bad = "<img src=x onerror=alert(1)>";
  const a = await page({scorecard: [null, 7, {n: 1, name: "Initiative", state: "partial", closed: 2, graded: 4, why: bad}]});
  const b = await page({scorecard: undefined});
  // T-2263: the deciding long-horizon row; a key with a verdict, a key with only a reason, and no key at all
  const side = c => ({true_done_same: 2, false_done: 1, regressions: 0, resumes_needed: null, unfinished: 0, cost: c});
  const l = await page({long: {state: "ahead", goals: 3, foreman: side(1.5), plain: Object.assign(side(null), {true_done_same: 1})}});
  const r = await page({long: {state: "no common goals", goals: 0, foreman: null, plain: null}});
  const n = await page({long: undefined});
  const says = (x, re) => x.texts.some(t => re.test(t));
  console.log(JSON.stringify({text: a.texts.includes(bad), markup: a.html.includes("onerror"),
    none: b.texts.concat(b.html.split("\n")).some(t => /not graded yet/i.test(t)),
    docs: a.docs,
    long: says(l, /ahead/) && says(l, /Sampo/) && says(l, /\$1\.50/) && says(l, /cost \?/) && says(l, /2 of 3/),
    longReason: says(r, /no common goals/) && !says(r, /Sampo is (ahead of|level with|behind) plain/) /* T-2960: the row's own sentence */, longNone: says(n, /no long-horizon run yet/i)}));
})();
