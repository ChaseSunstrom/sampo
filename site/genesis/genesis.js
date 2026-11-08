/* Rune (T-2080): draws the live tower from genesis.json (written by `fm genesis export site/genesis`).
   The hero is the tower itself, one carved course per generation, under a vault of moons, one per moonshot;
   below it every section's plates. Every course, moon and plate opens what it holds in the drawer.
   No libraries, nothing from another host. Text from genesis.json only ever becomes text: escaped, or textContent. */
(function () {
  "use strict";
  var root = document.documentElement, D = null, G = null, Sky = null, L = null;
  var LIVE = typeof window !== "undefined" && typeof requestAnimationFrame === "function";
  function $(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function plural(n, w) { return n + " " + w + (n === 1 ? "" : "s"); }

  function isDay() {
    var t = root.getAttribute("data-theme");
    return t ? t === "light" : !!(typeof matchMedia === "function" && matchMedia("(prefers-color-scheme: light)").matches);
  }
  if (LIVE) requestAnimationFrame(function () { [].forEach.call(document.querySelectorAll(".rise"), function (el) { el.classList.add("in"); }); });
  var theme = document.getElementById("theme");  /* the site header's button; the choice is the site's fm-theme */
  if (theme && theme.addEventListener) theme.addEventListener("click", function () {
    var next = isDay() ? "dark" : "light";
    root.setAttribute("data-theme", next);
    try { localStorage.setItem("fm-theme", next); } catch (e) {}
    if (Sky) Sky.retheme();
  });

  /* A small markdown renderer: headings, lists, tables, code, bold, italics, links. Escapes first. */
  function inline(s) {
    return String(s).split(/(`[^`]+`)/).map(function (part, i) {
      if (i % 2) return "<code>" + esc(part.slice(1, -1)).replace(/(^|\s)(--?[A-Za-z][\w-]*)/g, '$1<span class="nw">$2</span>') + "</code>";
      return esc(part)
        .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
        .replace(/(^|[\s(])\*([^*\s][^*]*?)\*(?=[\s).,;:]|$)/g, "$1<i>$2</i>")
        .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, function (m, t, h) {
          return /^(https:\/\/|#|[\w./-]+$)/.test(h) ? '<a href="' + h + '">' + t + "</a>" : t;
        });
    }).join("");
  }
  function table(rows) {
    function cells(r) { return r.trim().replace(/^\||\|$/g, "").split("|").map(function (c) { return c.trim(); }); }
    var body = rows.slice(1).filter(function (r) { return !/^\|[\s:|-]+\|$/.test(r.trim()); });
    return '<div class="tbl"><table><thead><tr>' + cells(rows[0]).map(function (c) { return "<th>" + inline(c) + "</th>"; }).join("") +
      "</tr></thead><tbody>" + body.map(function (r) {
        return "<tr>" + cells(r).map(function (c) { return "<td>" + inline(c) + "</td>"; }).join("") + "</tr>";
      }).join("") + "</tbody></table></div>";
  }
  function md(text) {
    var lines = String(text || "").replace(/\r/g, "").split("\n"), out = [], para = [], list = null, i = 0, m;
    function flush() {
      if (para.length) { out.push("<p>" + inline(para.join(" ")) + "</p>"); para = []; }
      if (list) { out.push("</" + list + ">"); list = null; }
    }
    while (i < lines.length) {
      var l = lines[i];
      if (/^```/.test(l)) {
        flush(); var code = [];
        for (i++; i < lines.length && !/^```/.test(lines[i]); i++) code.push(lines[i]);
        out.push("<pre><code>" + esc(code.join("\n")) + "</code></pre>"); i++; continue;
      }
      if ((m = /^(#{1,4})\s+(.*)$/.exec(l))) {
        flush(); var h = m[1].length + 2; out.push("<h" + h + ">" + inline(m[2]) + "</h" + h + ">"); i++; continue;
      }
      if (/^\|.*\|\s*$/.test(l)) {
        flush(); var rows = [];
        while (i < lines.length && /^\|.*\|\s*$/.test(lines[i])) rows.push(lines[i++]);
        out.push(table(rows)); continue;
      }
      if ((m = /^\s*([-*]|\d+\.)\s+(.*)$/.exec(l))) {
        var kind = /\d/.test(m[1]) ? "ol" : "ul", item = [m[2]];
        if (list !== kind) { flush(); list = kind; out.push("<" + kind + ">"); }
        for (i++; i < lines.length && /^\s+\S/.test(lines[i]) && !/^\s*([-*]|\d+\.)\s/.test(lines[i]); i++) item.push(lines[i].trim());
        out.push("<li>" + inline(item.join(" ")) + "</li>"); continue;
      }
      if ((m = /^([A-Z][A-Z-]+[!?]?):\s+(.*)$/.exec(l))) {  /* an intake line (MUST:, CONTEXT:, RESEARCH!:) is its own block, its tag a label */
        flush(); out.push('<p class="tagl"><b class="tg t-' + m[1].replace(/[!?]/g, "").toLowerCase() + '">' + esc(m[1]) + "</b><span>" + inline(m[2]) + "</span></p>"); i++; continue;
      }
      if (!l.trim()) { flush(); i++; continue; }
      if (list) flush();
      para.push(l.trim()); i++;
    }
    flush();
    return out.join("\n");
  }

  /* data helpers */
  /* T-2852: a model shows by its role, never by its endpoint's name: "jarvis" anywhere in the data (a key or a string,
     at any depth) reads as "a large local model". Run once on load, in place, so every renderer and md() see it */
  function roles(v) {
    if (typeof v === "string") return v.replace(/[\w-]*jarvis[\w-]*(?!\s*\(\d)/gi, "a large local model");  // "jarvis (1,853)" is a repo
    if (v && typeof v === "object") Object.keys(v).forEach(function (k) {
      var n = roles(k), x = roles(v[k]);
      if (n !== k) delete v[k];
      if (n !== k || x !== v[k]) v[n] = x;
    });
    return v;
  }
  function exams() {  /* the export can hold a run twice (same run, agent and time): count and list it once */
    var seen = {};
    return (D["exam"] || []).filter(function (e) { var k = [e.run, e.agent, e.at].join("|"); return seen[k] ? false : (seen[k] = true); });
  }
  function human(s) { s = String(s == null ? "" : s).replace(/-/g, " ").replace(/^self /, "self-"); return s.charAt(0).toUpperCase() + s.slice(1); }  /* a mission's slug as people say it */
  function wordCut(s) { s = String(s == null ? "" : s); return /…$/.test(s) ? s.slice(0, -1).replace(/\s*\S*$/, "").replace(/[\s,;:(]+$/, "") + "…" : s; }  /* the export's cut, moved back to a whole word */
  function capName(n) { var c = D.capabilities.filter(function (x) { return x.n === n; })[0]; return c ? c.name : "#" + n; }
  function firstSentence(s) { var m = /^(.*?[.!?])(\s|$)/.exec(s || ""); return m ? m[1] : s || ""; }
  function state(m) {
    if (m.critiqued) return "critiqued";
    if (m.tasks.length && m.tasks.every(function (t) { return t.status === "done"; })) return "done";
    if (m.tasks.length) return "active";
    return m.written ? "written" : "planned";
  }
  var STATE = { planned: "planned", written: "written", active: "building", done: "built", critiqued: "critiqued" };
  function allTasks(g) {
    var seen = {}, out = [];
    g.missions.forEach(function (m) {
      m.tasks.forEach(function (t) { if (!seen[t.id]) { seen[t.id] = 1; out.push({ t: t, m: m }); } });
    });
    g.tasks.forEach(function (t) { if (!seen[t.id]) { seen[t.id] = 1; out.push({ t: t, m: null }); } });
    return out;
  }
  function tierChips(gaps) {
    return gaps.map(function (x) {
      return '<span class="chip' + (x.tier >= 3 ? " t" + x.tier : "") + '">' + (x.tier ? "T" + x.tier + " " : "") + esc(x.text) + "</span>";
    }).join("");
  }
  function counts(gaps) {
    var c = { 1: 0, 2: 0, 3: 0, 4: 0, none: 0 };
    gaps.forEach(function (x) { c[x.tier] !== undefined ? c[x.tier]++ : c.none++; });
    return c;
  }
  function ago(iso) {
    var t = Date.parse(iso);
    if (isNaN(t)) return iso || "";
    var m = Math.round((Date.now() - t) / 60000);
    return m < 1 ? "just now" : m < 60 ? m + " min ago" : m < 2880 ? Math.round(m / 60) + " h ago" : Math.round(m / 1440) + " days ago";
  }
  function bar(t) {
    var pct = t.steps ? Math.round(100 * t.done / t.steps) : (t.status === "done" ? 100 : 0);
    return '<span class="prog" aria-hidden="true"><i style="width:' + pct + '%"></i></span>';
  }
  /* the page speaks Sampo: an export string shown as page text gets the current names (the docs, quoted in the drawer, keep theirs) */
  function say(s) {
    var t = String(s == null ? "" : s);
    if (t === "foreman") return "sampo";  // the exam's arm id
    return t.replace(/\bfm(?=[a-z]+\.)/g, "").replace(/\bfm(?= [a-z])/g, "sampo").replace(/\bForeman\b/g, "Sampo");
  }
  /* the moons: one per catalogue row; the tier counted, the cold reviewer's wild score, the stage */
  var TIER = ["low", "medium", "high", "xhigh", "extreme", "massive"];
  function moonRows() {
    var M = D.moonshots;
    return M && Array.isArray(M.rows) ? M.rows.filter(function (r) { return r && typeof r === "object" && r.id; }) : [];
  }
  function wildOf(r) { return typeof r.wild === "number" && r.wild >= 1 && r.wild <= 5 ? Math.round(r.wild) : 0; }
  function tierOf(r) { var i = TIER.indexOf(String(r.tier)); return i < 0 ? 1 : i; }
  function wildFirst(a, b) { return wildOf(b) - wildOf(a) || tierOf(b) - tierOf(a) || String(a.id).localeCompare(String(b.id)); }
  function stageOf(r) { var s = String(r.stage || "idea").toLowerCase(); return /^(idea|kill|slice|full|not-yet)$/.test(s) ? s : "idea"; }
  var STAGEW = { idea: "a new idea", kill: "at its kill test", slice: "a working slice", full: "built in full", "not-yet": "eclipsed: not yet" };
  function wildWord(r) { return wildOf(r) ? "wild " + wildOf(r) + " of 5" : "not scored for wildness"; }
  function moonDoc(id) {  /* its block in moonshot.md, as "Key: value" lines */
    var t = D.docs["moonshot.md"] || "", i = t.indexOf("### " + id + " "), j, f = {};
    if (i < 0) return f;
    j = t.indexOf("\n### ", i + 4);
    t.slice(i, j < 0 ? t.length : j).split("\n").forEach(function (l) {
      var m = /^\s*[-*]\s*\**([A-Za-z][A-Za-z ]*?):\**\s+(.*)$/.exec(l);
      if (m && !f[m[1].toLowerCase()]) f[m[1].toLowerCase()] = m[2];
    });
    return f;
  }

  /* header */
  function nextLine(n) {  /* "Mission 1 (exhaustive-thinking), #2 Exhaustive thinking. It starts with sampo mission." */
    var a = String(n.action || ""), head = a.split(":")[0].trim(), cmd = /\bfm [a-z][a-z-]*/.exec(a);
    return esc(say(head)) + "." + (cmd ? ' It starts with <code>' + esc(say(cmd[0])) + "</code>." : "");
  }
  function stats() {
    var g = G, written = g.missions.filter(function (m) { return m.written; }).length, steps = 0, done = 0;
    allTasks(g).forEach(function (x) { steps += x.t.steps; done += x.t.done; });
    var mg = [].concat.apply([], g.missions.map(function (m) { return m.gaps; })), c = counts(mg), tiered = c[1] + c[2] + c[3] + c[4];
    var rows = [
      [D.capabilities.length, "abilities to pass"],
      ["gen " + g.n, "of " + plural(D.generations.length, "generation")],
      [written + "/" + g.missions.length, "missions written"],
      [done + "/" + steps, "task steps done"],
      [(tiered ? Math.round(100 * (c[3] + c[4]) / tiered) : 0) + "%", "of planned gaps tier 3–4"],
      [moonRows().length, "moons over the tower"],
      [exams().length, "Louhi results"],
      [D.coverage.filled + "/" + D.coverage.cells, "coverage cells filled"],
      [1, "message from you"]];
    $("stats").innerHTML = rows.map(function (r) { return "<div><dt>" + esc(r[1]) + "</dt><dd>" + esc(r[0]) + "</dd></div>"; }).join("") +
      (D.next ? '<div class="rev"><dt>next</dt><dd>' + nextLine(D.next) + "</dd></div>" : "") +
      '<div class="rev"><dt>rev</dt><dd>' + esc(D.rev) + " · updated " + esc(ago(D.updated)) + " · " + plural(D.not_yet, "not-yet idea") + " waiting</dd></div>";
    $("gen-label").textContent = "generation " + g.n + " · live";
    $("foot").textContent = "Exported by sampo genesis export · " + D.rev;
  }

  /* A: the tower's levels for the picked generation */
  function tower() {
    var g = G, ts = allTasks(g), live = ts.filter(function (x) { return x.t.status !== "done"; }).length;
    var tiles = g.missions.map(function (m) {
      var s = state(m);
      return '<button type="button" class="tile ' + s + '" data-mission="' + esc(m.name) + '" data-n="' + (m.n || "") +
        '" data-deps="' + m.depends.join(" ") + '"><i>' + (m.n ? m.n + " · " : "") + STATE[s] + "</i><b>" + esc(human(m.name)) + "</b></button>";
    }).join("");
    var ci = ts.filter(function (x) { return /^CI failed on this branch/.test(x.t.title) && x.t.status !== "done"; });
    var how = ci.map(function (x) { return x.t.status; }).filter(function (v, i, a) { return a.indexOf(v) === i; });
    var trows = ts.length ? '<ul class="trows">' + ts.filter(function (x) { return ci.indexOf(x) < 0; }).map(function (x) {
      var t = x.t;
      return '<li><button type="button" class="trow' + (t.status === "done" ? " done" : "") + '" data-task="' + esc(t.id) + '"><code>' + esc(t.id) +
        '</code><span class="t">' + esc(say(t.title)) + '</span><span class="n">' + t.done + "/" + t.steps + " · " + esc(t.status) + "</span>" + bar(t) + "</button></li>";
    }).join("") + (ci.length ? '<li><div class="trow fold"><code>CI</code><span class="t">' + esc(plural(ci.length, "CI rerun") + ", " + how.join(" or ")) +
      '</span><span class="n">' + esc(ci.map(function (x) { return x.t.id; }).join(" · ")) + "</span></div></li>" : "") + "</ul>" : '<p class="empty">No task filed yet for this generation.</p>';
    var seed = "gen-" + g.n + "/seed.md";
    $("tower-body").className = "";
    $("tower-body").innerHTML = '<div class="levels"><p class="loop" aria-hidden="true">rewrites the Seed every round</p>' +
      '<button type="button" class="lvl l4" data-l4="1"><span class="flag"><span class="fl">only better rewrites survive</span><span class="fs">better rewrites only</span></span><span class="k">Level 4</span><h3>Louhi &amp; critique</h3>' +
      "<p>" + plural(exams().length, "Louhi result") + " · " + plural(g.critiques.length, "critique") + " · lineage " + (D.docs["lineage.md"] ? "kept" : "starts after gen 1") +
      " · drift " + esc(g.drift || "–") + " · sweep " + esc(g.sweep || "–") + "</p></button>" +
      '<div class="lvl l3"><span class="k">Level 3</span><h3>Tasks, built &amp; proved</h3><p>' + plural(ts.length, "task") + ", " + live + " open · red tests first, spikes and races in lanes</p>" + trows + "</div>" +
      '<div class="lvl l2"><span class="k">Level 2</span><h3>Missions, up to 8</h3><p>One prompt per gap cluster, each its own session. Hover one to see what it waits on.</p>' +
      '<div class="tiles" id="tiles">' + tiles + "</div>" +
      '<div class="legend"><span class="p">planned</span><span class="w">written</span><span class="a">building</span><span class="d">built</span><span class="c">critiqued</span></div></div>' +
      '<button type="button" class="lvl l1" data-doc="' + seed + '"' + (D.docs[seed] ? "" : " disabled") + '><span class="k">Level 1</span><h3>Seed · generation ' + g.n + "</h3>" +
      "<p>Biggest gaps, lenses, order · " + plural(g.missions.length, "mission") + ", " + plural(g.rules.length, "standing rule") + "</p></button>" +
      '<button type="button" class="lvl l0" data-doc="gen-0.md"><span class="k">Level 0 · foundation</span><h3>Your message</h3><p>Goals, MUSTs, NEVERs. Sent once, never edited.' + (D.seal ? " Seal: " + esc(D.seal) + "." : "") + "</p></button>" +
      "</div>" +
      '<div class="genesis-box"><code>sampo genesis</code><p>' + (g.n < 2 ? "Gen 1's Seed was written by hand from your message. From gen 2 on, sampo genesis writes every Seed (#13)."
        : "Gen " + g.n + "'s Seed is written by sampo genesis itself (#13).") + "</p></div>";
  }
  function highlight(on) {
    var box = $("tiles");
    if (!box) return;
    box.classList.toggle("hl", !!on);
    [].forEach.call(box.children, function (t) {
      var deps = on ? on.getAttribute("data-deps").split(" ") : [], n = t.getAttribute("data-n");
      t.classList.toggle("me", t === on);
      t.classList.toggle("dep", !!on && n !== "" && deps.indexOf(n) >= 0);
      t.classList.toggle("needs", !!on && on.getAttribute("data-n") !== "" &&
        t.getAttribute("data-deps").split(" ").indexOf(on.getAttribute("data-n")) >= 0);
    });
  }

  /* B: capabilities */
  var GROUPS = [["finds", "Finds", "work nobody asked for", [1, 3, 11]], ["thinks", "Thinks", "every possibility, then chooses by evidence", [2, 4, 8]],
    ["does", "Does", "whatever it takes, with whatever it needs", [9, 5, 10]], ["proves", "Proves", "and gets better at all of it", [6, 7, 12, 13]]];
  function missionsFor(n, g) { return g.missions.filter(function (m) { return m.caps.indexOf(n) >= 0; }); }
  function caps() {
    var known = {};
    D.capabilities.forEach(function (c) { known[c.n] = c; });
    $("caps-body").innerHTML = GROUPS.map(function (gr) {
      return '<div class="group ' + gr[0] + '"><h3>' + gr[1] + "<small>" + gr[2] + "</small></h3>" + gr[3].filter(function (n) { return known[n]; }).map(function (n) {
        var c = known[n], gaps = D.gaps[String(n)] || [], k = counts(gaps), ms = missionsFor(n, G);
        var chips = (k[4] ? '<span class="chip t4">' + k[4] + "× tier 4</span>" : "") + (k[3] ? '<span class="chip t3">' + k[3] + "× tier 3</span>" : "") +
          (ms.length ? ms.map(function (m) { return '<span class="chip">→ ' + esc(human(m.name)) + "</span>"; }).join("") : '<span class="chip">no mission in gen ' + G.n + "</span>");
        return '<button type="button" class="cap" data-cap="' + n + '"><span class="num">' + n + "</span><span><b>" + esc(c.name) + "</b>" +
          (n === 13 ? '<span class="big">the big one</span>' : "") + "<p>" + inline(say(firstSentence(c.text))) + '</p><span class="chips">' + chips + "</span></span></button>";
      }).join("") + "</div>";
    }).join("");
  }

  /* C: ambition */
  function ambition() {
    var all = [].concat.apply([], Object.keys(D.gaps).map(function (k) { return D.gaps[k]; })), c = counts(all);
    var mg = [].concat.apply([], G.missions.map(function (m) { return m.gaps; })), p = counts(mg), tiered = p[1] + p[2] + p[3] + p[4];
    var share = tiered ? Math.round(100 * (p[3] + p[4]) / tiered) : 0;
    var T = [[1, "Tweak", "wording, ms, a flag"], [2, "Feature", "a new verb or view"], [3, "Capability", "a new class of things it can do"], [4, "Paradigm", "changes what Sampo is"]];
    $("ambition").innerHTML = '<div class="scale">' + T.map(function (t) {
      return '<div class="tier' + (t[0] >= 3 ? " hi" : "") + '"><b>' + t[0] + " · " + t[1] + "</b><span>" + t[2] + "</span><strong>" + c[t[0]] +
        "</strong><small>open in the gap map · " + p[t[0]] + " planned in gen " + G.n + "</small></div>";
    }).join("") + "</div>" +
      '<div class="share" role="img" aria-label="' + share + '% of gen ' + G.n + ' planned gaps are tier 3 or 4; the floor is 70%"><i style="width:' + share + '%"></i><u></u><span>' +
      share + "% of gen " + G.n + "'s planned gaps are tier 3–4 · floor 70%</span></div>" +
      '<p class="note">Tweaks go to the inbox, not the build. At least 70% of everything built is tier 3–4.</p>';
  }

  /* D: exam */
  function frac(a, b) { return b ? esc(a + "/" + b) : "–"; }
  function graded(e) {
    return (e.keyed || 0) + (e.fixable || 0) + (e.tf_keyed || 0) + (e.readings_keyed || 0) + (e.missing_keyed || 0) > 0 || !!(String(e.agent).indexOf("draft-") === 0 && e.closed);
  }
  function exam() {
    var last = exams().slice(-12).reverse(), rows = last.filter(graded), rest = last.filter(function (e) { return !graded(e); });
    var names = rest.map(function (e) { return say(e.agent); }).filter(function (a, i, all) { return all.indexOf(a) === i; });
    $("exam").innerHTML = rows.length ? '<div class="tbl"><table><thead><tr><th>agent</th><th>run</th><th>found</th><th>false alarms</th><th>fixed</th><th>tests first</th><th>readings</th><th>missing tool</th><th>cost</th><th>split</th></tr></thead><tbody>' +
      rows.map(function (e) {
        return "<tr><td><b>" + esc(say(e.agent)) + "</b>" + (/haiku/i.test(e.model || "") ? " <small>Haiku smoke, not counted</small>" : "") + (e.author && !/child/.test(e.author) ? " <small>· template-written, not counted</small>" : "") + "</td><td><code>" + esc(e.run || "") + "</code></td><td>" + (String(e.agent).indexOf("draft-") === 0 && e.closed ? frac((e.closed.holdout || []).length + (e.closed.train || []).length, e.scenarios) + " drafts" + (e.given ? " · " + frac((e.given.holdout || []).length + (e.given.train || []).length, e.scenarios) + " on what it was given" : "") : frac(e.found, e.keyed)) + "</td><td>" + esc(e.false_alarms == null ? "–" : e.false_alarms) + (e.exam_defects ? " · " + esc(e.exam_defects) + " exam defect" + (e.exam_defects === 1 ? "" : "s") : "") + (e.proved_by_arm ? " (" + esc(e.proved_by_arm) + " with the arm's own proof)" : "") +
          "</td><td>" + frac(e.fixed, e.fixable) + "</td><td>" + frac(e.tf_first, e.tf_keyed) + "</td><td>" + frac(e.readings_hit, e.readings_keyed) + "</td><td>" + frac(e.missing_hit, e.missing_keyed) +
          "</td><td>" + (typeof e.cost === "number" && e.cost > 0 ? "$" + e.cost.toFixed(2) : "–") +
          "</td><td>" + esc(e.split || "all") + (e.author && e.author !== "self" ? " · by " + esc(e.author) : "") + (e.holdout && e.holdout.scenarios ? " · held out " + frac(e.holdout.found, e.holdout.keyed) + " found" : "") + (typeof e.errors === "number" && e.errors > 0 ? " · " + e.errors + " errored" : "") + "</td></tr>";
      }).join("") + (rest.length ? '<tr class="fold"><td colspan="10">' + esc(plural(rest.length, "more run")) + " here " + (rest.length === 1 ? "is" : "are") + " not graded yet: " + esc(names.join(", ")) + ".</td></tr>" : "") + "</tbody></table></div>"
      : rest.length ? '<p class="muted">' + esc(plural(rest.length, "recent run")) + ", none graded yet: " + esc(names.join(", ")) + ".</p>"
      : '<div class="rule"><b>No Louhi run yet.</b>Mission 1 (sampo exam) is building it: throwaway repos in six languages with planted defects, vague, ambiguous and missing-tool requests, a held-out third, and the same agent run plain beside Sampo on the same repos. Every result lands here.</div>';
  }

  /* T-2154: the held-out scorecard, one row per capability; every cell is textContent, never markup */
  function scorecard() {
    var box = $("scorecard"), t = document.createElement("table"), wrap = document.createElement("div"), tr, h, p,
      rows = Array.isArray(D.scorecard) ? D.scorecard.filter(function (r) { return r && typeof r === "object"; }) : [];
    box.textContent = "";
    if (!rows.length) {
      p = box.appendChild(document.createElement("p")); p.className = "muted"; p.textContent = "Not graded yet."; return;
    }
    wrap.className = "tbl";
    tr = t.appendChild(document.createElement("tr"));
    ["#", "capability", "state", "closed of held out", "why"].forEach(function (w) {
      h = tr.appendChild(document.createElement("th")); h.textContent = w;
    });
    rows.forEach(function (r) {
      tr = t.appendChild(document.createElement("tr"));
      [r.n, r.name, r.state, r.graded == null ? "–" : r.closed + " of " + r.graded, r.why || ""].forEach(function (v) {
        tr.appendChild(document.createElement("td")).textContent = String(v == null ? "" : v);
      });
    });
    if (rows.some(function (r) { return r.graded != null; })) { box.appendChild(wrap).appendChild(t); return; }
    /* R-k: nothing graded yet: say so once, with the rows behind a disclosure */
    p = box.appendChild(document.createElement("p")); p.className = "muted";
    p.textContent = "No capability has enough held-out grades yet; " + (rows.length === 1 ? "1 is" : rows.length + " are") + " waiting on graded scenarios.";
    h = box.appendChild(document.createElement("details"));
    h.appendChild(document.createElement("summary")).textContent = "What each one waits on";
    h.appendChild(wrap).appendChild(t);
  }

  /* T-2263: the deciding long-horizon row, Sampo vs plain on the common goals (the data keys are the export's own); textContent only, null shows as "?" */
  function longRow() {
    var box = $("long"), L = D.long, t = document.createElement("table"), wrap = document.createElement("div"), tr, p, q = function (v) { return v == null ? "?" : String(v); };
    box.textContent = "";
    if (!L || typeof L !== "object") {
      p = box.appendChild(document.createElement("p")); p.className = "muted"; p.textContent = "No long-horizon run yet."; return;
    }
    p = box.appendChild(document.createElement("p"));
    p.textContent = /^(ahead|level|behind)$/.test(L.state) ? "Sampo is " + { ahead: "ahead of", level: "level with", behind: "behind" }[L.state] + " plain on the " + L.goals + " goals both answered." : "No verdict: " + String(L.state);
    if (!L.foreman || !L.plain) return;
    wrap.className = "tbl";
    [["Sampo", L.foreman], ["plain", L.plain]].forEach(function (s) {
      var x = s[1];
      tr = t.appendChild(document.createElement("tr"));
      [s[0], "true done " + q(x.true_done_same) + " of " + L.goals,
        "checks " + q(x.checks_same) + " of " + q(x.checks_total),  /* T-2326: partial credit, so a tie on true done still reads */
        "false done " + q(x.false_done), "regressions " + q(x.regressions),
        "cost " + (typeof x.cost === "number" ? "$" + x.cost.toFixed(2) : "?")].forEach(function (v) {
        tr.appendChild(document.createElement("td")).textContent = v;
      });
    });
    box.appendChild(wrap).appendChild(t);
  }

  /* T-2626: the Moons campaign's catalogue, wildest first: the sky's text twin (wild, both tiers, stage, domain, novelty,
     what it unblocks, asked or not); each ID opens its moon; textContent only */
  function moonshotTable() {
    var box = $("moonshots"), M = D.moonshots, t, tr, p, wrap, b,
      rows = M && Array.isArray(M.rows) ? M.rows.filter(function (r) { return r && typeof r === "object"; }) : [];
    box.textContent = "";
    p = box.appendChild(document.createElement("p"));
    if (!rows.length) { p.className = "muted"; p.textContent = "No moonshot in the catalogue yet."; return; }
    p.className = "tally";
    p.textContent = String(M.total) + " of 100 moonshots; " + String(M.killed) + " of " + String(M.big)
      + " extreme or massive kill-tested; " + String(M.built) + " built; " + String(M.not_yet) + " not yet. "
      + rows.filter(function (r) { return wildOf(r) === 5; }).length + " burn at wild 5; "
      + rows.filter(function (r) { return r.asked === "no"; }).length + " were never asked for.";
    wrap = box.appendChild(document.createElement("div")); wrap.className = "tbl";
    t = wrap.appendChild(document.createElement("table"));
    tr = t.appendChild(document.createElement("tr"));
    ["ID", "Moonshot", "Wild", "Tier (counted; author → reviewer)", "Stage", "Domain", "Novelty", "Unblocks", "Asked"].forEach(function (h) {
      var th = tr.appendChild(document.createElement("th")); th.textContent = h.slice(0, 4) === "Tier" ? "Tier" : h;
      if (h.slice(0, 4) === "Tier") th.title = h;   /* the short head keeps the column on one line; the title keeps the key */
    });
    rows.slice().sort(wildFirst).forEach(function (r) {
      tr = t.appendChild(document.createElement("tr"));
      tr.className = "w" + wildOf(r);
      b = tr.appendChild(document.createElement("td")).appendChild(document.createElement("button"));
      b.type = "button"; b.className = "mid"; b.setAttribute("data-moon", String(r.id)); b.textContent = String(r.id);
      [r.title, wildOf(r) ? wildOf(r) + " of 5" : "unscored", String(r.tier || "?") + "; " + String(r.author || "?") + " → " + String(r.reviewer || "unrated"),
        r.stage, wordCut(r.domain), r.novelty, Array.isArray(r.unblocks) ? r.unblocks.join(", ") : "", r.asked === "no" ? "never asked" : String(r.asked)
      ].forEach(function (v) { tr.appendChild(document.createElement("td")).textContent = v == null ? "" : String(v); });
    });
  }

  /* T-2852: the Moons funnel at scale (D.moons_scale): every candidate counted by its fate, then the families it left,
     each with its lead's line, both tiers (the funnel's and the cold reviewer's), its stage and evidence. textContent only */
  function num(n) { return typeof n === "number" && isFinite(n) ? String(Math.round(n)).replace(/\B(?=(\d{3})+(?!\d))/g, ",") : "?"; }
  function famOf(id) {  /* the family whose lead was promoted to catalogue block id */
    var f = D && D.moons_scale && Array.isArray(D.moons_scale.families) ? D.moons_scale.families : [];
    for (var i = 0; i < f.length; i++) if (f[i] && f[i].block != null && String(f[i].block) === String(id)) return f[i];
    return null;
  }
  function famWord(m) { return m && m.fam ? " Lead of family " + String(m.fam.id || "?") + ", " + num(m.fam.size) + " ideas." : ""; }
  function moonsScale() {
    var box = $("moons-scale"), S = D.moons_scale, x, fams, ol, li, p, wrap, t, tr, td, b, sum = function (o) {
      return Object.keys(o).reduce(function (a, k) { return a + (typeof o[k] === "number" ? o[k] : 0); }, 0);
    };
    box.textContent = "";
    if (!S || typeof S !== "object" || !S.funnel || typeof S.funnel !== "object") {
      p = box.appendChild(document.createElement("p")); p.className = "muted";
      p.textContent = "No funnel run yet: sampo moons generate writes the candidates, sampo moons funnel sorts them."; return;
    }
    x = S.funnel;
    var w = x.write && typeof x.write === "object" ? x.write : {}, d = x.dups && typeof x.dups === "object" ? x.dups : {},
      r = x.rejected && typeof x.rejected === "object" ? x.rejected : {};
    ol = box.appendChild(document.createElement("ol")); ol.className = "funnel";
    [[num(x.generated) + " generated", ""],
      [num(sum(w)) + " refused at write", Object.keys(w).map(function (k) { return String(k) + " " + num(w[k]); }).join(", ")],
      [num(sum(d)) + " duplicates", num(d.exact) + " exact, " + num(d.embed) + " by embedding, " + num(d.shingle) + " by shingle"],
      [num(sum(r)) + " rejected", num(r.kill) + " at the kill test, " + num(r.tier) + " at the tier screen, " + num(r.novelty) + " not novel"],
      [num(x.merged) + " merged", "into a near survivor"],
      [num(x.kept) + " kept", x.pending ? num(x.pending) + " still pending" : ""]
    ].forEach(function (s, i) {
      if (i === 1 && !s[1]) return;
      li = ol.appendChild(document.createElement("li"));
      li.appendChild(document.createElement("b")).textContent = s[0];
      if (s[1]) li.appendChild(document.createElement("span")).textContent = s[1];
    });
    fams = Array.isArray(S.families) ? S.families.filter(function (f) { return f && typeof f === "object"; }) : [];
    p = box.appendChild(document.createElement("p")); p.className = "tally";
    if (!fams.length) { p.textContent = "No family yet: the survivors are clustered once the funnel keeps one."; return; }
    p.textContent = "Showing " + fams.length + " of " + num(typeof S.families_total === "number" ? S.families_total : fams.length)
      + " families, each a lead and its variants. A family promoted to the catalogue rings its moon in the sky.";
    wrap = box.appendChild(document.createElement("div")); wrap.className = "tbl";
    t = wrap.appendChild(document.createElement("table"));
    tr = t.appendChild(document.createElement("tr"));
    ["Family", "Lead and its line", "Tier (funnel → cold)", "Wild", "Stage", "Evidence"].forEach(function (h) {
      tr.appendChild(document.createElement("th")).textContent = h;
    });
    fams.forEach(function (f) {
      var wi = typeof f.wild === "number" && f.wild >= 1 && f.wild <= 5 ? Math.round(f.wild) : 0, st = String(f.stage || "candidate");
      tr = t.appendChild(document.createElement("tr")); tr.className = "w" + wi;
      td = tr.appendChild(document.createElement("td"));
      td.appendChild(document.createElement("b")).textContent = String(f.id || "?");
      td.appendChild(document.createElement("small")).textContent = num(f.size) + " ideas";
      if (f.block != null && f.block !== "") {
        b = td.appendChild(document.createElement("button"));
        b.type = "button"; b.className = "mid"; b.setAttribute("data-moon", String(f.block)); b.textContent = String(f.block);
      }
      td = tr.appendChild(document.createElement("td"));
      td.appendChild(document.createElement("b")).textContent = String(f.lead || "");
      if (f.today) td.appendChild(document.createElement("span")).textContent = "Today: " + String(f.today);
      if (f.with_this) td.appendChild(document.createElement("span")).textContent = "With this: " + String(f.with_this);
      [String(f.tier || "?") + " → " + String(f.cold || "not rated cold"), wi ? wi + " of 5" : "unscored",
        Object.prototype.hasOwnProperty.call(STAGEW, st) ? STAGEW[st] : st, f.evidence ? String(f.evidence) : "none yet"
      ].forEach(function (v) { tr.appendChild(document.createElement("td")).textContent = v; });
    });
  }

  /* T-3047: every kept moon (D.moons_scale.catalogue names the file beside genesis.json), fetched once the section nears the
     screen, then searched, filtered by grid, tier and family and paged 50 at a time. textContent only */
  function moonsCatalogue() {
    var S = D.moons_scale, C = S && S.catalogue, box = $("moons-all"), q = $("cat-q"), gridSel = $("cat-grid"), tierSel = $("cat-tier"),
      fam = $("cat-fam"), body = $("cat-rows"), status = $("cat-status"), prev = $("cat-prev"), next = $("cat-next"), pageEl = $("cat-page"), ui = $("cat-ui"),
      head = $("cat-label"), retry = $("cat-retry"), PER = 50, TIMEOUT = 20000, idx = null, cache = {}, seq = 0, shown = 0, page = 0, timer = null, started = false;
    if (!S) { box.hidden = true; return; }
    box.hidden = false;
    if (!C || typeof C.file !== "string" || !/^[\w.-]+\.json$/.test(C.file) || !(C.total > 0)) {
      ui.hidden = true; status.textContent = "No kept-moon catalogue yet: the next Genesis export writes every kept moon beside genesis.json."; return;
    }
    function mk(parent, tag, cls, text) {
      var e = parent.appendChild(document.createElement(tag));
      if (cls) e.className = cls;
      if (text !== undefined) e.textContent = text;
      return e;
    }
    /* list: this page's rows; total: how many match; nf: how many families they span, or null when the page hasn't read them all */
    function render(list, total, nf) {
      var from = page * PER, last = from + list.length, i, r, card, top, b, wi, d, k, pages = Math.max(1, Math.ceil(total / PER));
      body.textContent = "";
      for (i = 0; i < list.length; i++) {
        r = list[i]; wi = typeof r.wild === "number" && r.wild >= 1 && r.wild <= 5 ? Math.round(r.wild) : 0;
        card = mk(body, "article", "mc w" + wi); top = mk(card, "div", "mc-top");
        mk(top, "b", "mc-id", String(r.id));
        mk(top, "span", "chip mtier mtier-" + String(r.tier || "none").replace(/[^\w-]/g, ""), String(r.tier || "?"));
        d = mk(top, "span", "dots"); d.setAttribute("role", "img"); d.setAttribute("aria-label", wi ? "wildness " + wi + " of 5" : "wildness unscored");
        for (k = 1; k <= 5; k++) mk(d, "i", k <= wi ? "on" : "");
        mk(top, "span", "gtag", String(r.grid || "user"));
        if (r.family) {
          b = mk(top, "button", "chip fam", String(r.family)); b.type = "button"; b.setAttribute("data-fam", String(r.family));
          b.setAttribute("title", "Show only this family");
          if (r.lead) { b = mk(top, "span", "lead"); b.setAttribute("title", "the family's lead"); b.setAttribute("aria-label", "family lead"); }
        }
        if (r.drawn) {
          b = mk(top, "button", "mid", String(r.drawn)); b.type = "button"; b.setAttribute("data-moon", String(r.drawn));
        } else mk(top, "span", "drawn", r.drawn === "" ? "no kill test" : "queued");
        mk(card, "h4", "mc-title", String(r.title || ""));
        if (r.today) mk(card, "p", "mc-line", String(r.today));
        if (r.with_this) mk(card, "p", "mc-line with", String(r.with_this));
      }
      if (!total) mk(body, "p", "empty", "No kept moon matches. Clear a filter or search a shorter word.");
      /* the status is the one live region: it speaks the range and the page together */
      status.textContent = total ? "Showing " + (from + 1) + "–" + last + " of " + num(total) + " kept moon" + (total === 1 ? "" : "s")
        + (total === idx.total ? "" : " (" + num(idx.total) + " in all)") + (nf === null ? "" : " · " + num(nf) + " famil" + (nf === 1 ? "y" : "ies")) + ". Page " + (page + 1) + " of " + num(pages) + "." : "No kept moon matches.";
      pageEl.textContent = total ? "Page " + (page + 1) + " of " + num(pages) : "";
      shown = total; prev.disabled = page === 0; next.disabled = last >= total;
    }
    /* one file, once, with a timeout that also aborts the request; prep turns a shard's rows into searchable ones */
    function getJson(f, prep) {
      var ac = typeof AbortController === "function" ? new AbortController() : null, t, late;
      if (cache[f]) return Promise.resolve(cache[f]);
      if (typeof f !== "string" || !/^[\w.-]+\.json$/.test(f)) return Promise.reject(new Error("bad file name"));
      late = new Promise(function (_, no) { t = setTimeout(function () { if (ac) ac.abort(); no(new Error("timed out")); }, TIMEOUT); });
      return Promise.race([fetch(f, { cache: "no-cache", signal: ac ? ac.signal : undefined }).then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); }), late]).then(function (d) {
        clearTimeout(t); return (cache[f] = prep ? prep(d) : d);
      }, function (e) { clearTimeout(t); throw e; });
    }
    function rowsOf(d) {
      return (Array.isArray(d) ? d : []).filter(function (r) { return r && typeof r === "object"; }).map(function (r) {
        r._h = (String(r.id) + " " + String(r.title || "") + " " + String(r.today || "") + " " + String(r.with_this || "")).toLowerCase(); return r;
      });
    }
    function fail(e) {
      status.textContent = "The kept moons didn't load (" + (e && e.message ? e.message : "offline") + ")."; retry.hidden = false; body.textContent = "";
    }
    /* the index says where each page lives: with no search, grid or family filter a page is one or two shards of 500; otherwise
       the shards the tier filter leaves are read (once) and filtered here */
    function refresh(scroll) {
      var s = q.value.toLowerCase().trim(), g = gridSel.value, t = tierSel.value, f = fam.value.trim().toLowerCase(), exact = /^f-\d+$/.test(f),
        plain = !s && !g && !f, my = ++seq, start = 0, count = idx.total, from, end, k0, files = [], k;
      retry.hidden = true; status.textContent = "Loading the kept moons…";
      if (plain) {
        if (t) { count = idx.tiers[t] || 0; for (k in idx.tiers) { if (k === t) break; start += idx.tiers[k]; } }
        from = start + page * PER; end = Math.min(start + count, from + PER); k0 = Math.floor(from / idx.per);
        for (k = k0; count && k <= Math.floor((end - 1) / idx.per); k++) if (idx.shards[k]) files.push(idx.shards[k].file);
      } else files = idx.shards.filter(function (x) { return !t || x.tiers.indexOf(t) >= 0; }).map(function (x) { return x.file; });
      Promise.all(files.map(function (x) { return getJson(x, rowsOf); })).then(function (parts) {
        var all = [].concat.apply([], parts), view, fams = {}, nf = 0;
        if (my !== seq) return;
        if (plain) render(all.slice(from - k0 * idx.per, from - k0 * idx.per + (end - from)), count, t ? null : idx.families);
        else {
          view = all.filter(function (r) {
            return (!g || (r.grid || "user") === g) && (!t || r.tier === t) && (!s || r._h.indexOf(s) >= 0)
              && (!f || (exact ? String(r.family).toLowerCase() === f : String(r.family).toLowerCase().indexOf(f) >= 0));
          });
          view.forEach(function (r) { if (r.family && !fams[r.family]) { fams[r.family] = 1; nf++; } });
          render(view.slice(page * PER, page * PER + PER), view.length, nf);
        }
        if (scroll && head.scrollIntoView) head.scrollIntoView({ block: "start" });  // the list's top, not wherever the pager was
      }).catch(function (e) { if (my === seq) fail(e); });
    }
    function apply() { page = 0; refresh(false); }
    function later() { clearTimeout(timer); timer = setTimeout(apply, 120); }
    function load() {
      if (started) return; started = true;
      retry.hidden = true; status.textContent = "Loading the kept moons…";
      getJson(C.file).then(function (d) {
        if (!d || typeof d !== "object" || !Array.isArray(d.shards) || !(d.per > 0) || !(d.total > 0) || !d.tiers || typeof d.tiers !== "object") throw new Error("unreadable index");
        idx = d; delete cache[C.file];
        Object.keys(d.tiers).forEach(function (t) { if (t) tierSel.appendChild(document.createElement("option")).textContent = t; });
        apply();
      }).catch(function (e) { started = false; fail(e); });
    }
    retry.addEventListener("click", function () { if (idx) refresh(false); else load(); });
    q.addEventListener("input", later); fam.addEventListener("input", later);
    body.addEventListener("click", function (e) {
      var t = e.target && e.target.closest ? e.target.closest("button.fam") : null;
      if (t && idx) { fam.value = t.getAttribute("data-fam") || ""; apply(); }
    });
    gridSel.addEventListener("change", apply); tierSel.addEventListener("change", apply);
    prev.addEventListener("click", function () { if (idx && page > 0) { page--; refresh(true); } });
    next.addEventListener("click", function () { if (idx && (page + 1) * PER < shown) { page++; refresh(true); } });
    if (typeof IntersectionObserver === "function") {
      var io = new IntersectionObserver(function (es) { if (es.some(function (e) { return e.isIntersecting; })) { io.disconnect(); load(); } }, { rootMargin: "800px" });
      io.observe(box);
    } else load();
  }

  /* T-2625: the steer-expansion exam: raw words expanded into standing rules, a stronger judge, held-out coverage */
  function steerRow() {
    var box = $("steer"), S = D.steer, p, pct = function (v) { return typeof v === "number" ? Math.round(v * 100) + "%" : "?"; };
    box.textContent = "";
    p = box.appendChild(document.createElement("p"));
    if (!S || typeof S !== "object" || !S.holdout) {
      p.className = "muted"; p.textContent = "No steer-expansion run yet."; return;
    }
    p.textContent = "Held out: " + pct(S.holdout.rate) + " of the reference clauses covered over " + String(S.holdout.cases)
      + " case(s); train " + pct(S.train && S.train.rate) + ". Expanded by a decision model, judged by a stronger one; "
      + String(S.harmful) + " harmful addition(s).";  // T-2852: models show by role, never by name
  }

  /* E: rules */
  var RUN = [["Louhi holds out", "A third of the practice repos are never trained on. They decide \"done\"."], ["Subtract as you add", "Each round cuts as much as it adds. Hooks can't get slower."],
    ["Spikes, not vetoes", "\"Too ambitious\" is never a reason. A failed prototype is."], ["Two new tries, then move on", "Stuck tasks get two fresh angles, then a recorded diagnosis."],
    ["Everything finished", "Tests, docs, site, every surface. Nothing left half-built."], ["No outside sweeps", "Tokens go into building and proving, not reading other repos."]];
  function rules() {
    var steer = G.rules.map(function (r, i) {
      var m = /^\*\*(.+?)\*\*\s*(.*)$/.exec(r), title = m ? m[1].replace(/\.$/, "") : "Standing rule " + (i + 1), rest = m ? m[2] : r;
      return '<button type="button" class="rule steer" data-rule="' + i + '"><b>' + inline(say(title)) + '</b><span class="rb">' + inline(say(firstSentence(rest))) + "</span></button>";
    });
    var all = Array.isArray(D.intent) ? D.intent : [], solo = all.filter(function (r) { return !String(r[1] || "").trim(); });
    var intent = all.filter(function (r) { return solo.indexOf(r) < 0; }).map(function (r) {  // [head, rest], parsed by fm genesis export
      return '<div class="rule intent"><b>' + inline(say(r[0])) + '</b><span class="rb">' + inline(say(r[1])) + "</span></div>";
    });
    $("rules").innerHTML = intent.join("") + RUN.map(function (r) { return '<div class="rule"><b>' + esc(r[0]) + '</b><span class="rb">' + esc(r[1]) + "</span></div>"; }).join("") + steer.join("");
  }

  /* G: docs */
  function docs() {
    var names = Object.keys(D.docs).sort(), groups = { "Foundation": [], "Tower-wide": [] };
    names.forEach(function (n) {
      var key = n === "gen-0.md" ? "Foundation" : n.indexOf("/") < 0 ? "Tower-wide" : n.split("/")[0].replace("gen-", "Generation ");
      (groups[key] = groups[key] || []).push(n);
    });
    var col = function (k) {
      return "<h3>" + esc(k) + "</h3>" + groups[k].map(function (n) {
        return '<button type="button" class="doc" data-doc="' + esc(n) + '"><span class="dn" title="' + esc(n) + '">' + esc(n.split("/").pop()) + "</span><small>" + D.docs[n].split("\n").length + " lines</small></button>";
      }).join("");
    };
    var keys = Object.keys(groups).filter(function (k) { return groups[k].length; }), first = keys.filter(function (k) { return k === "Foundation" || k === "Tower-wide"; });
    $("docs-body").innerHTML = (first.length ? '<div class="first">' + first.map(col).join("") + "</div>" : "") +  /* R-s: the one-file Foundation shares a column */
      keys.filter(function (k) { return first.indexOf(k) < 0; }).map(function (k) { return "<div>" + col(k) + "</div>"; }).join("");
  }

  /* ======================= the scene: the tower and its vault of moons, in scene units (1000 wide) =======================
     One drawing, two uses: the live page renders it from the fetched export, and still.js renders the same functions
     into index.html, so without script the page still shows the tower, a still sky and the moons' table. */
  function F(v) { return Math.round(v * 10) / 10; }
  function pts(a) { return a.map(function (p) { return F(p[0]) + "," + F(p[1]); }).join(" "); }
  function poly(a, fill, extra) { return '<polygon points="' + pts(a) + '"' + (fill ? ' fill="' + fill + '"' : "") + (extra || "") + "/>"; }
  function line(x1, y1, x2, y2, stroke, w, extra) {
    return '<line x1="' + F(x1) + '" y1="' + F(y1) + '" x2="' + F(x2) + '" y2="' + F(y2) + '" stroke="' + stroke + '" stroke-width="' + w + '"' + (extra || "") + "/>";
  }
  function hash(s) { var h = 2166136261; s = String(s); for (var i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); } return (h >>> 0) / 4294967296; }
  function rng(seed) { var s = Math.floor(seed * 2147483645) + 1; return function () { return (s = s * 16807 % 2147483647) / 2147483647; }; }
  function roman(n) {
    var r = "", v = [[10, "X"], [9, "IX"], [5, "V"], [4, "IV"], [1, "I"]];
    for (var i = 0; i < v.length; i++) while (n >= v[i][0]) { r += v[i][1]; n -= v[i][0]; }
    return r || "0";
  }
  function grad(id, stops, attrs) {
    return "<linearGradient id=\"" + id + "\"" + (attrs || ' x1="0" y1="0" x2="0" y2="1"') + ">" + stops.map(function (s) {
      return '<stop offset="' + s[0] + '" stop-color="' + s[1] + '"' + (s[2] != null ? ' stop-opacity="' + s[2] + '"' : "") + "/>";
    }).join("") + "</linearGradient>";
  }
  function rgrad(id, stops, attrs) { return grad(id, stops, attrs).replace("<linearGradient", "<radialGradient").replace("</linearGradient>", "</radialGradient>"); }
  /* a dressed ashlar: square-cut, its face framed by a chiselled bevel lit upper-left and shadowed lower-right; on the course
     being forged the bevels take the heat of the molten joints round them */
  function block(xa, xb, ya, yb, fill, hot) {
    var b = Math.min(2.2, (yb - ya) * .14, (xb - xa) * .14);
    return poly([[xa, ya], [xb, ya], [xb, yb], [xa, yb]], fill) +
      poly([[xa, ya], [xb, ya], [xb - b, ya + b], [xa + b, ya + b]], hot ? "#FFB070" : "#A0949C", ' opacity="' + (hot ? .42 : .3) + '"') +
      poly([[xa, ya], [xa + b, ya + b], [xa + b, yb - b], [xa, yb]], hot ? "#E07040" : "#7A7078", ' opacity="' + (hot ? .34 : .26) + '"') +
      poly([[xa, yb], [xa + b, yb - b], [xb - b, yb - b], [xb, yb]], hot ? "#1E0804" : "#07060A", ' opacity="' + (hot ? .62 : .55) + '"') +
      poly([[xb, ya], [xb, yb], [xb - b, yb - b], [xb - b, ya + b]], hot ? "#1E0804" : "#07060A", ' opacity="' + (hot ? .5 : .4) + '"');
  }
  /* the elder futhark, cut as straight strokes in a 10 × 16 cell, as the mark's runes are */
  var RUNE = ["M2 0V16M2 3L8 0M2 7L8 4", "M2 16V0L8 5V16", "M2 0V16M2 4L7 8L2 12", "M2 0V16M2 1L8 5M2 6L8 10", "M2 16V0L7 4L2 8L8 16",
    "M8 2L2 8L8 14", "M1 2L9 14M9 2L1 14", "M2 16V0L7 4L2 8", "M2 0V16M8 0V16M2 6L8 10", "M5 0V16M2 6L8 10", "M5 0V16",
    "M4 2L1 6L4 10M6 6L9 10L6 14", "M5 0V16M5 0L8 3M5 16L2 13", "M2 0V16M2 0L7 4M2 16L7 12", "M5 16V0M5 6L1 1M5 6L9 1",
    "M7 0L2 6L8 10L3 16", "M5 16V0M1 4L5 0L9 4", "M2 0V16L7 12L2 8L7 4L2 0", "M2 16V0L5 5L8 0V16", "M2 16V0L8 7M8 16V0L2 7",
    "M3 16V0L8 5", "M5 0L9 8L5 16L1 8Z", "M1 2V14L9 2V14Z", "M2 16L8 8L5 3L2 8L8 16"];
  /* a rune cut into the stone as a V-groove: a lit lower-right lip, the cut, its shadowed upper-left wall, and the heat
     only on the narrow floor, fading toward the stroke ends (no outline: the light sits inside the stone) */
  var HV = {};
  function hv(c) { var id = "hv" + c.slice(1); HV[id] = c; return "url(#" + id + ")"; }
  function rune(i, x, y, s, heat, strong, attrs) {
    var d = RUNE[Math.abs(i) % RUNE.length];
    return '<g transform="translate(' + F(x - 5 * s) + " " + F(y - 8 * s) + ") scale(" + (Math.round(s * 100) / 100) + ')" fill="none" stroke-linecap="square"' + (attrs || "") + ">" +
      '<path d="' + d + '" stroke="#B4A9B0" stroke-width="2.9" opacity=".36" transform="translate(.6 .7)"/>' +
      '<path d="' + d + '" stroke="#1E1B24" stroke-width="2.8"/>' +
      '<path d="' + d + '" stroke="#0A090E" stroke-width="1.4" opacity=".8" transform="translate(-.45 -.5)"/>' +
      (heat ? '<path d="' + d + '" stroke="' + hv(heat) + '" stroke-width="' + (strong > 1 ? 1.75 : 1.35) + '"/>' : "") +
      (heat && strong > 1 ? '<path d="' + d + '" stroke="' + hv("#FFF8E8") + '" stroke-width=".55"/>' : "") + "</g>";
  }
  /* a mission's rune: [heat on its floor, strength] by its state, cooled in a finished course, burning in the one being forged */
  var RS = { critiqued: ["#E0B872", 1], done: ["#FFC24D", 1], active: ["#FF9440", 2], written: ["#C9D8F0", 1], planned: [null, 0] };
  var RSH = { critiqued: ["#FFC24D", 2], done: ["#FFC24D", 2], active: ["#FFE7A8", 2], written: ["#FFE7A8", 2], planned: ["#FF8A2A", 1], next: ["#FFF1C8", 2] };
  /* the quench's tempers: gen 0 grey, then blue, purple, straw, round again */
  var TEMPER = [["#26282E", "#3C3F48", "#55585F"], ["#24262E", "#3A5688", "#4A70A8"], ["#26272E", "#6E4A6A", "#5A3E78"], ["#2A2B30", "#8A7444", "#9A6A3A"]];
  var GROUPC = { finds: "#6E9BFF", thinks: "#BC8BFF", does: "#FF9440", proves: "#45E0A0" };

  /* where everything stands, from the export: a new generation adds a course, a new moonshot a moon */
  var TR = { low: 9.5, medium: 12, high: 15, xhigh: 18.5, extreme: 23, massive: 27 };
  var BANDY = { 5: 144, 4: 216, 3: 288, 2: 360, 1: 428 };
  function layout() {
    var gens = D.generations, N = Math.max(1, gens.length), h = Math.max(60, Math.min(92, 368 / N)), extra = Math.max(0, N * h - 368);
    var S = { CX: 590, GY: 960 + extra, SH: 1000 + extra, FH: 128, CH: 56, h: h, courses: [], moons: [] };
    S.FY = S.GY - S.FH;
    var last = gens[gens.length - 1], y = S.FY, nextN = 0, nm = D.next && /mission (\d+)/i.exec(D.next.action || "");
    if (nm && D.next.gen === (last && last.n)) nextN = +nm[1];
    gens.forEach(function (g, i) {
      var done = !!(g.drift && g.sweep) || (g.missions.length && g.missions.every(function (m) { return m.critiqued; }));
      S.courses.push({ g: g, n: g.n, y0: y - h, y1: y, wb: 284 - 64 * i / N, wt: 284 - 64 * (i + 1) / N, hot: g === last && !done, age: gens.length - 1 - i, next: g === last ? nextN : 0 });
      y -= h;
    });
    S.TOP = y - S.CH; S.cw = 284 - 64 + 14;
    S.fire = { x0: S.CX - 160, x1: S.CX + 160, y0: S.TOP - 136, y1: y + h + 6 };
    /* the moons: the wilder, the higher the band it rides; in a band the biggest stand nearest the tower's axis */
    var ex = [S.CX - S.cw / 2 - 64, S.CX + S.cw / 2 + 64], RB = 1500, bands = {}, ms = 1, B = {};
    moonRows().forEach(function (r) { var w = Math.max(1, wildOf(r)); (bands[w] = bands[w] || []).push(r); });
    /* a band that sinks to the crown parts round its fire; a crowded band staggers deeper, and if the sky ever fills past
       what staggering holds, every moon shrinks together so the tiers still compare */
    Object.keys(bands).forEach(function (w) {
      var Y = BANDY[w], iv = Y > S.TOP - 70 ? [[30, ex[0]], [ex[1], 970]] : Y > S.TOP - 125 ? [[30, S.CX - 90], [S.CX + 90, 970]] : [[30, 970]];
      var tot = iv.reduce(function (a, v) { return a + v[1] - v[0]; }, 0), need = bands[w].reduce(function (a, r) { return a + 2 * (TR[r.tier] || 10.5) + 6; }, 0);
      B[w] = { Y: Y, iv: iv, tot: tot, amp: need / tot > .8 ? 15 : 10, jit: need / tot > .8 ? .4 : 1 };
      ms = Math.min(ms, tot * 1.25 / need);
    });
    S.arcs = []; S.ms = ms;
    Object.keys(bands).forEach(function (w) {
      var list = bands[w].sort(wildFirst), Y = B[w].Y, iv = B[w].iv, amp = B[w].amp, jit = B[w].jit;
      var tot = B[w].tot, s = tot / list.length, slots = [];
      for (var j = 0; j < list.length; j++) {
        var at = (j + .5) * s, k = 0;
        while (k < iv.length - 1 && at > iv[k][1] - iv[k][0]) { at -= iv[k][1] - iv[k][0]; k++; }
        slots.push({ x: iv[k][0] + at, k: j });
      }
      slots.sort(function (a, b) { return Math.abs(a.x - S.CX) - Math.abs(b.x - S.CX); });
      list.forEach(function (r, j) {
        var sl = slots[j], hj = hash(r.id), x = sl.x + (hj - .5) * 10 * jit, dx = x - S.CX;
        var yy = Y + RB - Math.sqrt(RB * RB - dx * dx) + (sl.k % 2 ? amp : -amp) + (hash(r.id + "y") - .5) * 8 * jit;
        S.moons.push({ r: r, id: String(r.id), w: wildOf(r), R: (TR[r.tier] || 10.5) * ms, x: x, y: yy, st: stageOf(r),
          ang: Math.atan2(S.TOP - yy, S.CX - x), seed: hj, fam: famOf(r.id) });
      });
      iv.forEach(function (v) { S.arcs.push({ w: +w, Y: Y, x0: v[0], x1: v[1] }); });
    });
    S.RB = RB;
    /* how far the moons may grow on a small screen before two discs touch */
    S.fit = 1.35;
    S.moons.forEach(function (a, i) { for (var j = i + 1; j < S.moons.length; j++) { var b = S.moons[j]; S.fit = Math.min(S.fit, Math.hypot(a.x - b.x, a.y - b.y) / (a.R + b.R) * .96); } });
    S.VH = Math.min(S.SH, S.moons.reduce(function (a, m) { return Math.max(a, m.y + m.R * 3); }, S.TOP + 40) + 30);
    /* the top band is named on the sky: the wildest there are (eight at most, the biggest first) */
    var top = S.moons.reduce(function (a, m) { return Math.max(a, m.w); }, 0);
    S.named = top >= 4 ? S.moons.filter(function (m) { return m.w === top; }).sort(function (a, b) { return wildFirst(a.r, b.r); }).slice(0, 8) : [];
    return S;
  }

  /* the far ridge, the ground and its light: the hearth's door spills on the flags */
  function ground(S) {
    var o = [], R = rng(.31), cx = S.CX, GY = S.GY, b, x, nf = [], df = [], both = [];
    [[3, 150, 300, 96], [2, 112, 230, 56], [1, 70, 170, 22]].forEach(function (B) {
      b = B[0]; x = -760 + R() * 120;
      var body = [[-800, GY]], facets = [];
      while (x < 1180) {
        var hgt = B[1] * (.55 + R() * .5), wl = B[2] * (.5 + R() * .5), wr = B[2] * (.5 + R() * .5), ax = x + wl, ay = GY - B[3] - hgt;
        body.push([x, GY - B[3] * .6], [ax, ay]);
        facets.push([ax, ay, x, ax + wr]);
        x = ax + wr * (.55 + R() * .3);
      }
      body.push([1200, GY - B[3] * .6], [1200, GY]);
      o.push(poly(body, "", ' class="m' + b + 'a"'));
      facets.forEach(function (f) {
        var ax = f[0], ay = f[1], xl = f[2], xr = f[3], ml = ax - (ax - xl) * .45, mr = ax + (xr - ax) * .45, by = GY - B[3] * .6;
        var tf = ax > cx ? "l" : "r";  /* by night the faces toward the forge take the fire; by day the dawn is upper right */
        function face(side, outer) { return "m" + b + (outer ? "k1" : "k2"); }
        function shade(outer) { return "m" + b + (outer ? "s1" : "s2"); }
        var L1 = [[ax, ay], [xl, by], [ml, by]], L2 = [[ax, ay], [ml, by], [ax, by]], R1 = [[ax, ay], [xr, by], [mr, by]], R2 = [[ax, ay], [mr, by], [ax, by]];
        nf.push(poly(L1, "", ' class="' + (tf === "l" ? face(0, 1) : shade(1)) + '"'), poly(L2, "", ' class="' + (tf === "l" ? face(0, 0) : shade(0)) + '"'),
          poly(R1, "", ' class="' + (tf === "r" ? face(0, 1) : shade(1)) + '"'), poly(R2, "", ' class="' + (tf === "r" ? face(0, 0) : shade(0)) + '"'));
        df.push(poly(L1, "", ' class="' + shade(1) + '"'), poly(L2, "", ' class="' + shade(0) + '"'), poly(R1, "", ' class="' + face(0, 1) + '"'), poly(R2, "", ' class="' + face(0, 0) + '"'));
        if (b === 3) both.push(poly([[ax, ay], [ax - (ax - xl) * .16, ay + (by - ay) * .2], [ax + (xr - ax) * .14, ay + (by - ay) * .18]], "", ' class="snow"'));
        both.push(line(ax, ay, ax, by, "", .8, ' class="crease"'));
      });
    });
    o.push('<g class="nf">' + nf.join("") + '</g><g class="df">' + df.join("") + "</g>" + both.join(""));
    o.push('<rect x="-800" y="' + F(GY - 70) + '" width="2000" height="70" fill="url(#hz)"/>');
    /* the flags, in perspective toward the tower */
    var fl = [poly([[-800, GY], [1200, GY], [1200, S.SH], [-800, S.SH]], "", ' class="gnd"')];
    for (var i = -9; i <= 9; i++) fl.push(line(cx + i * 30, GY, cx + i * 30 + (S.SH - GY) * i * 3, S.SH, "", .8, ' class="joint"'));
    [8, 20, 36].forEach(function (d) { fl.push(line(-800, GY + d, 1200, GY + d, "", .8, ' class="joint"')); });
    o.push(fl.join(""));
    o.push('<ellipse cx="' + cx + '" cy="' + F(GY + 8) + '" rx="190" ry="26" fill="url(#pool)"/>');
    return '<g class="mt" id="tground" aria-hidden="true">' + o.join("") + "</g>";
  }

  /* one course: an octagonal drum seen front-on, lit faces left, shade right; a cornice under the moonlight, an inscription
     panel, eight mission blocks with their runes, and an iron band in the generation's temper */
  function course(c, i, S) {
    var cx = S.CX, y0 = c.y0, y1 = c.y1, H = y1 - y0, hot = c.hot, g = c.g, o = [], j, R = rng(hash("course" + c.n));
    function hw(y) { return (c.wt + (c.wb - c.wt) * (y - y0) / H) / 2; }
    function fw(y) { return hw(y) * .6; }
    var lf = [[cx - hw(y0), y0], [cx - fw(y0), y0], [cx - fw(y1), y1], [cx - hw(y1), y1]];
    var rf = [[cx + fw(y0), y0], [cx + hw(y0), y0], [cx + hw(y1), y1], [cx + fw(y1), y1]];
    var ff = [[cx - fw(y0), y0], [cx + fw(y0), y0], [cx + fw(y1), y1], [cx - fw(y1), y1]];
    var all = [lf[0], rf[1], rf[2], lf[3]];
    o.push(poly(lf, "url(#sL)"), poly(rf, "url(#sR)"), poly(ff, hot ? "url(#seam)" : "#121116"));
    /* side faces: three rows of stone with staggered joints */
    var top = y0 + 9, ba = y1 - 9, sj = hot ? ["#B8381A", 1.2, ' opacity=".8"'] : ["#0B0A0E", 1.2, ' opacity=".85"'];
    [1, 2].forEach(function (q) {
      var yy = top + (ba - top) * q / 3;
      o.push(line(cx - hw(yy), yy, cx - fw(yy), yy, sj[0], sj[1], sj[2]), line(cx + fw(yy), yy, cx + hw(yy), yy, sj[0], sj[1], sj[2]));
      if (!hot) o.push(line(cx - hw(yy) + 1, yy + 1.2, cx - fw(yy), yy + 1.2, "#FFC48A", .6, ' opacity=".12"'));
    });
    [0, 1, 2].forEach(function (q) {
      var ya = top + (ba - top) * q / 3, yb = top + (ba - top) * (q + 1) / 3, ym = (ya + yb) / 2, f = q % 2 ? .62 : .38;
      o.push(line(cx - hw(ym) + (hw(ym) - fw(ym)) * f, ya, cx - hw(ym) + (hw(ym) - fw(ym)) * f, yb, sj[0], sj[1], sj[2]),
        line(cx + fw(ym) + (hw(ym) - fw(ym)) * (1 - f), ya, cx + fw(ym) + (hw(ym) - fw(ym)) * (1 - f), yb, sj[0], sj[1], sj[2]));
    });
    /* the inscription panel: cooled courses are engraved in brass, the hot one burns */
    var pa = top + 3, pb = pa + 17, pxa = cx - fw(pa) + 3, pxb = cx + fw(pa) - 3;  /* a square-cut recess: shadowed top and left, a lit lower lip */
    o.push(poly([[pxa, pa], [pxb, pa], [pxb, pb], [pxa, pb]], hot ? "#2A120E" : "#1A1820"));
    o.push(line(pxa, pa + .6, pxb, pa + .6, "#000", 1.2, ' opacity=".6"'), line(pxa + .6, pa, pxa + .6, pb, "#000", 1, ' opacity=".45"'),
      line(pxa, pb - .4, pxb, pb - .4, "#FFE2C8", .7, ' opacity=".16"'), line(pxb - .4, pa, pxb - .4, pb, "#FFE2C8", .6, ' opacity=".1"'));
    var name = "GEN " + roman(c.n);
    if (hot) o.push('<text class="ins" x="' + cx + '" y="' + F(pb - 4.2) + '" text-anchor="middle" font-size="12.5" letter-spacing="2.6" fill="none" stroke="#FF8A2A" stroke-width="3" opacity=".5">' + name + "</text>");
    o.push('<text class="ins" x="' + cx + '" y="' + F(pb - 4.2) + '" text-anchor="middle" font-size="12.5" letter-spacing="2.6" fill="url(#' + (hot ? "hotT" : "brassT") + ')">' + name + "</text>");
    /* eight mission blocks, two rows of four (one row of eight when the courses grow thin) */
    var rows = H >= 84 ? 2 : 1, cols = 8 / rows, ma = pb + 3, mb = ba - 2, rh = (mb - ma - (rows - 1) * 3) / rows;
    for (j = 0; j < 8; j++) {
      var r = Math.floor(j / cols), q = j % cols, ya = ma + r * (rh + 3), yb = ya + rh, ym = (ya + yb) / 2, w = 2 * fw(ym) - 6, bw = (w - (cols - 1) * 3) / cols;
      var xa = cx - w / 2 + q * (bw + 3), xb = xa + bw, m = g.missions[j], blk = "url(#b" + Math.floor(R() * 4) + ")";
      var b = [block(xa, xb, ya, yb, blk, hot)];
      if (R() < .3) b.push('<polyline points="' + pts([[xa + bw * .2, ya + 2], [xa + bw * .35, ya + rh * .45], [xa + bw * .3, ya + rh * .7]]) + '" fill="none" stroke="#08080B" stroke-width=".7" opacity=".8"/>');
      for (var p = 0; p < 3; p++) {
        var px = xa + 3 + R() * (bw - 6), py = ya + 3 + R() * (rh - 6), pr = .6 + R() * .9;
        b.push('<circle cx="' + F(px - .4) + '" cy="' + F(py - .4) + '" r="' + F(pr) + '" fill="#E8E2F0" opacity=".14"/><circle cx="' + F(px) + '" cy="' + F(py) + '" r="' + F(pr) + '" fill="#000" opacity=".36"/>');
      }
      if (m) {
        var s0 = state(m), isNext = hot && c.next === m.n, col = isNext ? RSH.next : (hot ? RSH : RS)[s0];
        b.push(rune(Math.floor(hash(m.name) * 24), (xa + xb) / 2, ym, Math.min(rh * .78, bw * 1.3) / 16, col[0], col[1]));
        o.push('<g class="mb" data-mission="' + esc(m.name) + '" data-gen="' + c.n + '"><title>' + esc("Mission " + (m.n || "") + " · " + human(m.name) + " · " + (isNext ? "next" : STATE[s0])) + "</title>" + b.join("") + "</g>");
      } else o.push(b.join(""));
    }
    if (hot) [ma - 1.5, ma + rh + 1.5, mb + 1].slice(0, rows + 1).forEach(function (yy) {  /* the molten seams' white-hot cores */
      o.push(line(cx - fw(yy) + 3, yy, cx + fw(yy) - 3, yy, "#FFF1C8", .5, ' opacity=".85"'));
    });
    /* faces' edges, the moonlight from above and the fire's warmth from the crown */
    o.push(line(cx - fw(y0), y0, cx - fw(y1), y1, "#FFC8A0", .8, ' opacity=".16"'), line(cx + fw(y0), y0, cx + fw(y1), y1, "#000", 1, ' opacity=".4"'));
    var cool = hot ? 0 : Math.min(.34, .12 + .06 * c.age);
    if (cool) o.push(poly(all, "#0A0F2E", ' opacity="' + F(cool * 100) / 100 + '"'));
    o.push(poly(all, "url(#moonG)"));
    o.push(poly(all, "url(#warmL)", ' opacity="' + F(Math.max(.15, 1 - c.age * .22) * 100) / 100 + '"'));
    if (hot) o.push(poly(all, "url(#heat)"));
    /* the iron band, in its temper; hot iron on the course being forged */
    var t0 = y1 - 8, tp = hot ? "url(#tpH)" : "url(#tp" + ((c.n - 1) % 3 + 1) + ")";
    o.push(poly([[cx - hw(t0) - 3, t0], [cx + hw(t0) + 3, t0], [cx + hw(y1) + 3, y1], [cx - hw(y1) - 3, y1]], tp));
    o.push(line(cx - hw(t0) - 3, t0 + .5, cx + hw(t0) + 3, t0 + .5, hot ? "#FFE2A0" : "#A9B4C6", .7, ' opacity=".45"'));
    for (j = 0, w = 2 * hw(y1 - 4), q = Math.max(4, Math.round(w / 30)); j <= q; j++) {
      var rx = cx - w / 2 + 6 + (w - 12) * j / q;
      o.push('<circle cx="' + F(rx) + '" cy="' + F(y1 - 4) + '" r="2.1" fill="' + (hot ? "url(#rvH)" : "url(#rv)") + '"/>');
    }
    /* the cornice under the moonlight, and its corbels */
    var e0 = hw(y0) + 6;
    o.push(poly([[cx - e0, y0 - 1], [cx + e0, y0 - 1], [cx + e0 - 2, y0 + 8], [cx - e0 + 2, y0 + 8]], "url(#sT)"));
    o.push(line(cx - e0, y0 - .6, cx + e0, y0 - .6, hot ? "#FFC48A" : "#8FA6D6", 1, ' opacity="' + (hot ? .85 : .55) + '"'), line(cx - e0 + 2, y0 + 8.4, cx + e0 - 2, y0 + 8.4, "#000", 1.2, ' opacity=".55"'));
    for (j = cx - e0 + 10; j < cx + e0 - 6; j += 17) o.push(poly([[j - 3.5, y0 + 8], [j + 3.5, y0 + 8], [j, y0 + 13]], "#2B2830"));
    o.push(poly([[cx - hw(t0) - 4, t0 - 2.5], [cx + hw(t0) + 4, t0 - 2.5], [cx + hw(y1) + 4, y1 + 2.5], [cx - hw(y1) - 4, y1 + 2.5]], "url(#seamF)", ' class="flash"'));
    o.push(poly([[cx - e0, y0 - 2], [cx + e0, y0 - 2], [cx + hw(y1) + 4, y1 + 1], [cx - hw(y1) - 4, y1 + 1]], "none", ' class="fr" stroke="#FFC24D" stroke-width="2"'));
    var ms = g.missions, ct = ms.filter(function (m) { return m.critiqued; }).length;
    var label = "Generation " + c.n + ": " + plural(ms.length, "mission") + (ct ? ", " + ct + " critiqued" : ", " + ms.filter(function (m) { return m.written; }).length + " written") +
      (g.drift ? "; drift " + g.drift + ", sweep " + (g.sweep || "–") : "") + (hot ? "; being forged now" : "") + ". Shows its levels in A.";
    return '<a class="crs ' + (hot ? "hot" : "cool") + '" href="#tower" data-gen-pick="' + c.n + '" style="--i:' + (i + 1) + '" aria-label="' + esc(label) + '">' + o.join("") + "</a>";
  }

  /* gen 0, the foundation: your message. Thirteen runes, one per ability, lit by the scorecard; the forge door below */
  function base(S) {
    var cx = S.CX, y0 = S.FY, y1 = S.GY - 15, H = y1 - y0, wt = 318, wb = 338, o = [], R = rng(.77), j;
    function hw(y) { return (wt + (wb - wt) * (y - y0) / H) / 2; }
    function fw(y) { return hw(y) * .6; }
    var lf = [[cx - hw(y0), y0], [cx - fw(y0), y0], [cx - fw(y1), y1], [cx - hw(y1), y1]], rf = [[cx + fw(y0), y0], [cx + hw(y0), y0], [cx + hw(y1), y1], [cx + fw(y1), y1]];
    var ff = [[cx - fw(y0), y0], [cx + fw(y0), y0], [cx + fw(y1), y1], [cx - fw(y1), y1]];
    o.push(poly(lf, "url(#sL)"), poly(rf, "url(#sR)"), poly(ff, "#121116"));
    /* big ashlars across the front, the door cut through their middle */
    var rowsY = [y0 + 9, y0 + 44, y0 + 62, y0 + 86, y1];
    for (var r = 0; r < rowsY.length - 1; r++) {
      var ya = rowsY[r] + 1.5, yb = rowsY[r + 1] - 1.5, ym = (ya + yb) / 2, w = 2 * fw(ym) - 4, n = r === 0 ? 1 : r === 1 ? 1 : 4, bw = (w - (n - 1) * 3) / n;
      for (j = 0; j < n; j++) {
        var xa = cx - w / 2 + j * (bw + 3);
        o.push(block(xa, xa + bw, ya, yb, "url(#b" + Math.floor(R() * 4) + ")"));
      }
      [-1, 1].forEach(function (sd) {
        o.push(line(cx + sd * fw(rowsY[r + 1]), rowsY[r + 1], cx + sd * hw(rowsY[r + 1]), rowsY[r + 1], "#0B0A0E", 1.3, ' opacity=".85"'));
      });
    }
    for (j = 0; j < 14; j++) {
      var px = cx - fw(y1) + 8 + R() * (2 * fw(y1) - 16), py = y0 + 12 + R() * (H - 20), pr = .7 + R();
      o.push('<circle cx="' + F(px - .4) + '" cy="' + F(py - .4) + '" r="' + F(pr) + '" fill="#E8E2F0" opacity=".14"/><circle cx="' + F(px) + '" cy="' + F(py) + '" r="' + F(pr) + '" fill="#000" opacity=".34"/>');
    }
    o.push('<polyline points="' + pts([[cx - 120, y0 + 66], [cx - 112, y0 + 72], [cx - 114, y0 + 80], [cx - 104, y0 + 88]]) + '" fill="none" stroke="#08080B" stroke-width="1"/>');
    /* the message band: one rune per ability */
    var groupOf = {}, sc = {};
    GROUPS.forEach(function (gr) { gr[3].forEach(function (n) { groupOf[n] = gr[0]; }); });
    (Array.isArray(D.scorecard) ? D.scorecard : []).forEach(function (x) { if (x && typeof x === "object") sc[x.n] = String(x.state || ""); });
    var caps = D.capabilities, bw2 = 2 * fw(y0 + 26) - 26, step = bw2 / Math.max(1, caps.length);
    var bxa = cx - fw(y0 + 12) + 6, bxb = cx + fw(y0 + 12) - 6;
    o.push(poly([[bxa, y0 + 12], [bxb, y0 + 12], [bxb, y0 + 41], [bxa, y0 + 41]], "#16151B"),
      line(bxa, y0 + 12.7, bxb, y0 + 12.7, "#000", 1.4, ' opacity=".6"'), line(bxa, y0 + 40.5, bxb, y0 + 40.5, "#FFE2C8", .8, ' opacity=".14"'));
    caps.forEach(function (cp, k) {
      var x = cx - bw2 / 2 + step * (k + .5), st = sc[cp.n] || "", hi = /closed|pass|done|held/.test(st), mid = /partial|some/.test(st);
      var col = GROUPC[groupOf[cp.n]] || "#FFC24D";
      o.push('<g class="cr" data-cap="' + cp.n + '"><title>' + esc("#" + cp.n + " " + cp.name + " · " + (st || "not graded")) + "</title>" +
        rune(cp.n * 5 + 3, x, y0 + 26.5, Math.min(1.25, step / 13), col, hi ? 2 : 1, hi || mid ? "" : ' opacity=".82"') + "</g>");
    });
    /* the lintel: your message, its seal */
    o.push('<text class="ins" x="' + cx + '" y="' + F(y0 + 56.5) + '" text-anchor="middle" font-size="12" letter-spacing="2.4" fill="url(#brassT)">YOUR MESSAGE' + (D.seal ? " · " + esc(String(D.seal).toUpperCase().slice(0, 8)) : "") + "</text>");
    /* the forge door: a triangular head, never an arch, the fire inside spilling onto the steps */
    var dy0 = y0 + 70, dw = 26;
    o.push(poly([[cx - dw - 7, y1], [cx - dw - 7, dy0 + 14], [cx, dy0 - 8], [cx + dw + 7, dy0 + 14], [cx + dw + 7, y1]], "#5A5560"));
    o.push(poly([[cx - dw, y1], [cx - dw, dy0 + 17], [cx, dy0 + 1], [cx + dw, dy0 + 17], [cx + dw, y1]], "url(#door)"));
    for (j = 0; j < 7; j++) o.push(poly([[cx - 20 + j * 6.5, y1], [cx - 17 + j * 6.5, y1 - 3 - R() * 3], [cx - 14 + j * 6.5, y1]], j % 3 ? "#FFB03C" : "#FFE9B0", ' opacity=".9"'));
    o.push(poly([[cx - hw(y0) - 6, y0 - 1], [cx + hw(y0) + 6, y0 - 1], [cx + hw(y0) + 4, y0 + 8], [cx - hw(y0) - 4, y0 + 8]], "url(#sT)"));
    o.push(line(cx - hw(y0) - 6, y0 - .6, cx + hw(y0) + 6, y0 - .6, "#8FA6D6", 1, ' opacity=".5"'));
    o.push(poly([lf[0], rf[1], rf[2], lf[3]], "#0A0F2E", ' opacity=".2"'), poly([lf[0], rf[1], rf[2], lf[3]], "url(#moonG)"), poly([lf[0], rf[1], rf[2], lf[3]], "url(#doorL)"));
    /* three steps down to the flags, lit from the door */
    for (j = 0; j < 3; j++) {
      var sy = y1 + j * 5, sw = hw(y1) + 8 + j * 9;
      o.push(poly([[cx - sw, sy], [cx + sw, sy], [cx + sw + 3, sy + 5], [cx - sw - 3, sy + 5]], j % 2 ? "#3C3A46" : "#4A4550"));
      o.push(line(cx - sw, sy + .4, cx + sw, sy + .4, "#FFC48A", .8, ' opacity="' + (.5 - j * .12) + '"'));
    }
    o.push(poly([[cx - hw(y0) - 7, y0 - 3], [cx + hw(y0) + 7, y0 - 3], [cx + hw(y0) + 5, y0 + 9], [cx - hw(y0) - 5, y0 + 9]], "url(#seamF)", ' class="flash"'));
    o.push(poly([[cx - hw(y0) - 6, y0 - 2], [cx + hw(y0) + 6, y0 - 2], [cx + hw(y1) + 30, y1 + 16], [cx - hw(y1) - 30, y1 + 16]], "none", ' class="fr" stroke="#FFC24D" stroke-width="2"'));
    return '<a class="crs base cool" id="tbase" href="#tower" data-doc="gen-0.md" style="--i:0" aria-label="' +
      esc("The foundation, your message" + (D.seal ? " (seal " + D.seal + ")" : "") + ": thirteen runes, one per ability it must pass. Opens the message.") + '">' + o.join("") + "</a>";
  }

  /* the crown: a parapet of angular merlons round the forge on top, where the next course is made */
  function crown(S) {
    var cx = S.CX, y0 = S.TOP, y1 = S.TOP + S.CH, w = S.cw, hw = w / 2, fw = hw * .6, o = [], mask = [], mw = (2 * fw) / 5;
    /* behind the parapet: the coal bed's glow, and the still fire for a page with no script or no canvas */
    o.push(poly([[cx - fw, y0 + 12], [cx + fw, y0 + 12], [cx + fw, y0 + 26], [cx - fw, y0 + 26]], "url(#coal)"));
    o.push('<g class="stillfire">' + [[0, 26, 92], [-36, 18, 62], [38, 19, 70], [-66, 11, 34], [68, 12, 38]].map(function (f) {
      var x = cx + f[0], b = y0 + 18, t = b - f[2], hw2 = f[1];
      return '<path d="M' + F(x - hw2) + "," + F(b) + "C" + F(x - hw2) + "," + F(b - f[2] * .45) + " " + F(x - hw2 * .2) + "," + F(t + f[2] * .3) + " " + F(x + hw2 * .1) + "," + F(t) +
        "C" + F(x + hw2 * .5) + "," + F(t + f[2] * .35) + " " + F(x + hw2) + "," + F(b - f[2] * .4) + " " + F(x + hw2) + "," + F(b) + 'Z" fill="url(#fo)"/>' +
        '<path d="M' + F(x - hw2 * .5) + "," + F(b) + "C" + F(x - hw2 * .5) + "," + F(b - f[2] * .3) + " " + F(x) + "," + F(b - f[2] * .5) + " " + F(x + hw2 * .05) + "," + F(b - f[2] * .66) +
        "C" + F(x + hw2 * .4) + "," + F(b - f[2] * .4) + " " + F(x + hw2 * .5) + "," + F(b - f[2] * .2) + " " + F(x + hw2 * .5) + "," + F(b) + 'Z" fill="url(#fm)"/>';
    }).join("") + "</g>");
    /* the parapet wall: lit face, front, shade face */
    var lf = [[cx - hw, y0 + 22], [cx - fw, y0 + 22], [cx - fw + 2, y1], [cx - hw + 3, y1]], rf = [[cx + fw, y0 + 22], [cx + hw, y0 + 22], [cx + hw - 3, y1], [cx + fw - 2, y1]];
    var ff = [[cx - fw, y0 + 22], [cx + fw, y0 + 22], [cx + fw - 2, y1], [cx - fw + 2, y1]];
    o.push(poly(lf, "url(#sL)"), poly(rf, "url(#sR)"), poly(ff, "url(#sF)"));
    mask.push(ff, lf, rf);
    /* dressed courses in the wall, and a scored band where the next generation's course will be set */
    [y0 + 32, y0 + 44].forEach(function (yy, k) {
      o.push(line(cx - hw + 2, yy, cx + hw - 2, yy, "#0B0A0E", 1.1, ' opacity=".8"'));
      for (var q = 0; q < 7; q++) { var xx = cx - fw + (2 * fw) * (q + (k ? .5 : 0)) / 6; if (xx > cx - fw + 4 && xx < cx + fw - 4) o.push(line(xx, yy - 10 + (k ? 0 : -2) + 2, xx, yy, "#0B0A0E", 1, ' opacity=".7"')); }
    });
    o.push(poly([[cx - fw + 6, y1 - 9], [cx + fw - 6, y1 - 9], [cx + fw - 6, y1 - 4], [cx - fw + 6, y1 - 4]], "#16151B"));
    o.push(line(cx - fw + 6, y1 - 3.6, cx + fw - 6, y1 - 3.6, "#FFC48A", .6, ' opacity=".25"'));
    /* merlons, square-cut: one on each side face, three on the front, each with a slit lit by the fire inside */
    [[cx - hw, cx - fw - 4, "url(#sL)"], [cx + fw + 4, cx + hw, "url(#sR)"], [cx - fw, cx - fw + mw, "url(#sF)"], [cx - mw / 2, cx + mw / 2, "url(#sF)"], [cx + fw - mw, cx + fw, "url(#sF)"]].forEach(function (m, k) {
      var mp = [[m[0], y0 + 22], [m[0], y0 + 5], [m[0] + 4, y0], [m[1] - 4, y0], [m[1], y0 + 5], [m[1], y0 + 22]], mx = (m[0] + m[1]) / 2;
      o.push(poly(mp, m[2]), line(m[0] + 4, y0 + .5, m[1] - 4, y0 + .5, k < 2 ? "#8FA6D6" : "#FFC48A", .9, ' opacity="' + (k < 2 ? .5 : .75) + '"'));
      if (k >= 2) o.push(poly([[mx - 1.6, y0 + 19], [mx - 1.6, y0 + 10], [mx, y0 + 7], [mx + 1.6, y0 + 10], [mx + 1.6, y0 + 19]], "#FFB03C", ' opacity=".85"'));
      mask.push(mp);
    });
    o.push(line(cx - fw, y0 + 22.5, cx + fw, y0 + 22.5, "#FFC48A", 1, ' opacity=".7"'));
    o.push(poly([[cx - hw, y0], [cx + hw, y0], [cx + hw - 3, y1], [cx - hw + 3, y1]], "url(#warmC)"));
    S.mask = mask;
    return '<g class="crown" style="--i:' + (S.courses.length + 1) + '">' + o.join("") + "</g>";
  }

  var DEFS = '<defs>' +
    grad("sL", [[0, "#5A5560"], [1, "#2B2830"]], ' x1="0" y1="0" x2="1" y2="1"') + grad("sF", [[0, "#3B3A44"], [1, "#1C1B22"]]) +
    grad("sR", [[0, "#22232C"], [.6, "#15151B"], [1, "#3A4258"]], ' x1="0" y1="0" x2="1" y2="0"') + grad("sT", [[0, "#6E6670"], [.35, "#3E3944"], [1, "#24222A"]]) +
    grad("b0", [[0, "#4A4550"], [1, "#24222A"]], ' x1="0" y1="0" x2=".3" y2="1"') + grad("b1", [[0, "#433F4B"], [1, "#1F1E26"]], ' x1="0" y1="0" x2=".3" y2="1"') +
    grad("b2", [[0, "#504853"], [1, "#2A2630"]], ' x1="0" y1="0" x2=".3" y2="1"') + grad("b3", [[0, "#3C3A46"], [1, "#1B1A21"]], ' x1="0" y1="0" x2=".3" y2="1"') +
    grad("seam", [[0, "#4A1208"], [.35, "#9A2A12"], [.7, "#D8501C"], [1, "#8A2410"]]) +
    grad("brassT", [[0, "#FFF6DD"], [.34, "#F0D392"], [.56, "#B9873F"], [.78, "#E9C98A"], [1, "#FFF1CC"]]) +
    grad("hotT", [[0, "#FFFFFF"], [.4, "#FFF1C8"], [1, "#FFB03C"]]) +
    TEMPER.map(function (t, i) { return grad("tp" + i, [[0, t[1]], [.5, t[2]], [1, t[0]]]); }).join("") +
    grad("tpH", [[0, "#FFC24D"], [.45, "#F06A1C"], [1, "#6A1A10"]]) +
    rgrad("rv", [[0, "#B9C2D2"], [.6, "#6B7180"], [1, "#22252D"]], ' cx=".35" cy=".35" r=".7"') +
    rgrad("rvH", [[0, "#FFFFFF"], [.5, "#FFD27A"], [1, "#E2481A"]], ' cx=".35" cy=".35" r=".7"') +
    grad("moonG", [[0, "#A9BEF0", .2], [.3, "#A9BEF0", 0]]) +
    grad("warmL", [[0, "#FF8A3A", .22], [.38, "#FF8A3A", .06], [.62, "#FF8A3A", 0], [1, "#FF8A3A", 0]], ' x1="0" y1="0" x2="1" y2="0"') +
    grad("warmC", [[0, "#FFB060", .3], [.5, "#FF8A3A", .08], [1, "#FF8A3A", 0]]) +
    grad("heat", [[0, "#FF6A1E", .22], [.55, "#FF6A1E", .07], [1, "#FF6A1E", .03]]) +
    grad("seamF", [[0, "#FF6A1E", 0], [.4, "#FFB03C", .9], [.55, "#FFF1C8"], [.7, "#FFB03C", .9], [1, "#FF6A1E", 0]]) +
    grad("door", [[0, "#3A0E06"], [.4, "#B8301A"], [.75, "#FFB03C"], [1, "#FFE9B0"]]) +
    grad("doorL", [[0, "#FF8A3A", 0], [.7, "#FF8A3A", 0], [1, "#FF8A3A", .22]]) +
    grad("coal", [[0, "#FFE9B0"], [.5, "#FFB03C"], [1, "#C8381E"]]) +
    grad("fo", [[0, "#C8281A", 0], [.22, "#E2481A", .85], [.6, "#FF8A1F"], [1, "#FFC24D"]]) + grad("fm", [[0, "#FFB040", 0], [.35, "#FFC24D"], [1, "#FFF1C8"]]) +
    rgrad("pool", [[0, "#FFB050", .5], [.45, "#FF7A2A", .16], [1, "#FF7A2A", 0]]) +
    '<linearGradient id="hz" x1="0" y1="0" x2="0" y2="1"><stop offset="0" class="hz0"/><stop offset="1" class="hz1"/></linearGradient></defs>';

  function towerSVG(S) {
    HV = {};
    var body = ground(S) + '<g id="troot">' + base(S) + S.courses.map(function (c, i) { return course(c, i, S); }).join("") + crown(S) + "</g>";  /* the city reuses #troot */
    return DEFS + "<defs>" + Object.keys(HV).map(function (id) {  /* each heat's floor gradient, in the rune's own 10 × 16 cell */
      return grad(id, [[0, HV[id], 0], [.3, HV[id]], [.7, HV[id]], [1, HV[id], 0]], ' gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="0" y2="16"');
    }).join("") + "</defs>" + body;
  }

  /* the bands' engraved arcs: an astrolabe's almucantars, one per wild score */
  function arcsSVG(S) {
    return '<g class="alm" aria-hidden="true">' + S.arcs.map(function (a) {
      var d = [], x;
      for (x = a.x0; x <= a.x1 + .1; x += 10) d.push([x, a.Y + S.RB - Math.sqrt(S.RB * S.RB - (x - S.CX) * (x - S.CX))]);
      return '<polyline points="' + pts(d) + '"/>';
    }).join("") + "</g>";
  }

  /* a moon's lit part, as an SVG path: a thin sliver for an idea, a crescent at its kill test, half for a slice, full when built.
     The lit limb is drawn on +x; the moon is turned so it faces the forge on the tower, the light of this sky */
  var STAGEF = { idea: .14, kill: .32, slice: .5, full: 1 };
  function phase(r, f) {
    var rx = Math.abs(1 - 2 * f) * r;
    return "M0," + F(-r) + "A" + F(r) + "," + F(r) + " 0 0 1 0," + F(r) + "A" + F(rx) + "," + F(r) + " 0 0 " + (f < .5 ? 0 : 1) + " 0," + F(-r) + "Z";
  }
  var PAL = {  /* lit, mid, terminator, halo (rgb), by wild score, night then day */
    n: [["#E8EAF4", "#9097BC", "#4B5278", "144,151,188"], ["#FFB08A", "#C8381E", "#5A1408", "226,72,26"], ["#FFE6C4", "#FF9440", "#A2460C", "255,148,64"],
      ["#FFF6DD", "#FFC24D", "#B9873F", "255,194,77"], ["#F4ECFF", "#D2C0F4", "#8A72C8", "188,139,255"], ["#FFFFFF", "#DCE8FF", "#7DA2FF", "159,192,255"]],
    d: [["#9097BC", "#555B80", "#343A63", "85,91,128"], ["#E2481A", "#B12E16", "#5A1408", "177,46,22"], ["#FF9440", "#A2460C", "#5A2A08", "162,70,12"],
      ["#E9C98A", "#8A5A00", "#4A3000", "138,90,0"], ["#BC8BFF", "#6A3BB5", "#3A1F70", "106,59,181"], ["#9FC0FF", "#2448B8", "#12206A", "36,72,184"]] };
  var GLOW = [[.45, 1.5], [.5, 1.7], [.66, 2], [.8, 2.4], [1, 3], [1, 4.4]];  /* halo strength and reach × radius */
  function moonsSVG(S) {
    var defs = PAL.n.map(function (p, w) {
      return rgrad("ml" + w, [[0, p[0]], [.55, p[1]], [1, p[2]]], ' cx=".75" cy=".5" r=".9"') +
        rgrad("mh" + w, [[0, "rgb(" + p[3] + ")", GLOW[w][0] * .55], [.4, "rgb(" + p[3] + ")", GLOW[w][0] * .18], [1, "rgb(" + p[3] + ")", 0]]);
    }).join("");
    var byId = {}, lines = [];
    S.moons.forEach(function (m) { byId[m.id] = m; });
    S.moons.forEach(function (m) {
      (Array.isArray(m.r.unblocks) ? m.r.unblocks : []).forEach(function (id) { var b = byId[id]; if (b) { var d = Math.hypot(b.x - m.x, b.y - m.y); lines.push('<path d="M' + F(m.x) + "," + F(m.y) + "Q" + F((m.x + b.x) / 2) + "," + F((m.y + b.y) / 2 - d * .22) + " " + F(b.x) + "," + F(b.y) + '" fill="none" stroke="#D4AE66" stroke-width=".8" opacity=".18"/>'); } });
    });
    return "<defs>" + defs + "</defs>" + lines.join("") + S.moons.map(function (m) {
      var p = PAL.n[m.w], R = m.R, hr = R * GLOW[m.w][1], f = STAGEF[m.st];
      var body = m.st === "not-yet" ? '<circle r="' + R + '" fill="#2A0A06" stroke="#FF7A2A" stroke-width="' + F(R * .1) + '" stroke-opacity=".6"/>'
        : '<circle r="' + R + '" fill="#0E1232" stroke="rgb(' + p[3] + ')" stroke-opacity=".35" stroke-width=".8"/><path d="' + phase(R, f) + '" fill="url(#ml' + m.w + ')"/>';
      var rays = m.w === 5 ? [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11].map(function (k) {
        var a = k * Math.PI / 6 + m.seed, l = R * (k % 2 ? 2.2 : 3.2);
        return line(Math.cos(a) * R * 1.15, Math.sin(a) * R * 1.15, Math.cos(a) * l, Math.sin(a) * l, "#DCE8FF", .8, ' opacity=".45"');
      }).join("") : "";
      return '<g transform="translate(' + F(m.x) + " " + F(m.y) + ")\"><circle r=\"" + F(hr) + '" fill="url(#mh' + m.w + ')"/>' + rays +
        '<g transform="rotate(' + F(m.ang * 180 / Math.PI) + ')">' + body + "</g></g>";
    }).join("");
  }

  /* the brightest moons are named on the sky, by their own titles cut at the first clause */
  function shortTitle(t) { t = String(t || ""); var m = /^(.{16,}?)[,;:]\s/.exec(t); t = m ? m[1] : t; return t.length > 66 ? t.slice(0, 64).replace(/\s+\S*$/, "") + "…" : t; }
  /* each name owns the stretch of sky between the midpoints to its neighbours, so no count of names can overlap */
  function namesHTML(S) {
    var ns = S.named.slice().sort(function (a, b) { return a.x - b.x; });
    return ns.map(function (m, i) {
      var lo = i ? (ns[i - 1].x + m.x) / 2 : 0, hi = i < ns.length - 1 ? (m.x + ns[i + 1].x) / 2 : 1000, w = Math.min(230, hi - lo) - 8;
      var left = Math.max(lo + 4, Math.min(m.x - w / 2, hi - 4 - w)), al = left > m.x - w / 2 + 1 ? " al-l" : left < m.x - w / 2 - 1 ? " al-r" : "";
      return '<div class="sname w' + m.w + al + '" data-moon="' + esc(m.id) + '" style="left:' + F(left / 10) + "%;width:" + F(w / 10) + "%;top:" + F((m.y - m.R * 1.3 - 8) / S.SH * 100) + '%"><b>' +
        esc(m.id) + "</b><span>" + esc(shortTitle(m.r.title)) + "</span></div>";
    }).join("");
  }

  function scene() {
    L = layout();
    var art = $("art"), sc = $("scene"), live = false;
    var ta = $("tower-art"), html = towerSVG(L), sig = String(Math.floor(hash(html) * 1e9));
    if (ta.getAttribute("data-sig") !== sig) { ta.innerHTML = html; ta.setAttribute("data-sig", sig); }  /* the built still is this export's: keep it */
    $("arcs").innerHTML = arcsSVG(L);
    $("sky-names").innerHTML = namesHTML(L);
    if (art && art.setAttribute) art.setAttribute("viewBox", "0 0 1000 " + F(L.SH));
    var fa = $("foot-art");
    if (fa && fa.setAttribute) {  /* the footer reuses the ground and base drawn above, so it links them only once they exist */
      fa.setAttribute("viewBox", F(L.CX - 330) + " " + F(L.FY - 70) + " 660 " + F(L.GY - L.FY + 110));
      fa.innerHTML = '<use href="#tground"/><use href="#tbase"/>';
    }
    if (sc && sc.style && sc.style.setProperty) sc.style.setProperty("--sh", String(L.SH));
    try { live = startSky(); } catch (e) { live = false; }
    if (live) { $("still-moons").textContent = ""; art.classList.add("live"); } else $("still-moons").innerHTML = moonsSVG(L);
    if (LIVE) { keys(); arrive(); }
  }

  /* ======================= the live sky: stars, the vault's moons and embers, the crown's fire ======================= */
  var HEAT = [[1, [234, 242, 255]], [.8, [255, 240, 200]], [.6, [255, 194, 77]], [.4, [255, 148, 64]], [.2, [226, 72, 26]], [0, [120, 30, 16]]];
  function heatRGB(T) {
    for (var i = 1; i < HEAT.length; i++) if (T >= HEAT[i][0]) {
      var a = HEAT[i - 1], b = HEAT[i], u = (T - b[0]) / (a[0] - b[0]);
      return [b[1][0] + (a[1][0] - b[1][0]) * u, b[1][1] + (a[1][1] - b[1][1]) * u, b[1][2] + (a[1][2] - b[1][2]) * u];
    }
    return HEAT[HEAT.length - 1][1];
  }
  function rgba(c, a) { return "rgba(" + (c[0] | 0) + "," + (c[1] | 0) + "," + (c[2] | 0) + "," + Math.max(0, Math.min(1, a)).toFixed(3) + ")"; }
  var SALT = [[69, 224, 160], [188, 139, 255], [110, 155, 255]];
  /* a canvas's backing store is capped near 2.6 million pixels: past that a software rasteriser spends the frame compositing it */
  function capDpr(w, h) { return Math.max(1, Math.min(devicePixelRatio || 1, 2, Math.sqrt(2.6e6 / Math.max(1, w * h)))); }
  var FL = [{ x: 0, w: 28, h: 100, p: 0, core: 1 }, { x: -37, w: 19, h: 70, p: 1.7, core: 1 }, { x: 40, w: 20, h: 78, p: 4.2, core: 1 },
    { x: -69, w: 12, h: 40, p: .9 }, { x: 71, w: 12, h: 44, p: 2.4 }, { x: -17, w: 12, h: 58, p: 2.9 }, { x: 19, w: 12, h: 52, p: 3.7 }];
  var GR = {
    red: [[0, "rgba(138,26,20,0)"], [.35, "rgba(184,38,26,.7)"], [1, "rgba(226,72,26,.9)"]],
    out: [[0, "rgba(200,40,26,0)"], [.22, "rgba(226,72,26,.85)"], [.6, "#FF8A1F"], [1, "#FFC24D"]],
    mid: [[0, "rgba(255,176,64,0)"], [.35, "#FFC24D"], [1, "#FFF1C8"]],
    core: [[0, "rgba(61,107,255,0)"], [.4, "#4C78FF"], [.85, "#9FC0FF"], [1, "#EAF2FF"]],
    grn: [[0, "rgba(69,224,160,0)"], [.5, "rgba(69,224,160,.8)"], [1, "rgba(168,245,210,.9)"]],
    vio: [[0, "rgba(188,139,255,0)"], [.5, "rgba(188,139,255,.8)"], [1, "rgba(226,204,255,.9)"]] };

  function startSky() {
    var cv = $("vault"), xv = $("vfx"), fcv = $("flame"), st = $("stars"), scn = $("scene");
    if (!LIVE || !cv || !cv.getContext || !xv || !xv.getContext || !fcv || !fcv.getContext || typeof Path2D !== "function") return false;
    var c = cv.getContext("2d"), x2 = xv.getContext("2d"), fc = fcv.getContext("2d"), sc = st && st.getContext ? st.getContext("2d") : null;
    if (!c || !x2 || !fc) return false;
    var RM = matchMedia("(prefers-reduced-motion: reduce)").matches, S = L, M = S.moons, byId = {}, edges = [], P = [];
    var k = 1, dpr = 1, fdpr = 1, rs = 1, day = isDay(), T0 = performance.now(), on = true, hov = null, n = 0, last = 0, lastV = 0, acc = 0;
    var shoot = null, nextShoot = 14 + Math.random() * 10, END = 4.2, R0 = rng(.5), dirty = true, boxes = [];
    M.forEach(function (m) { byId[m.id] = m; });
    M.forEach(function (m) { (Array.isArray(m.r.unblocks) ? m.r.unblocks : []).forEach(function (id) { if (byId[id] && byId[id] !== m) edges.push([m, byId[id]]); }); });
    /* arrival: the moons kindle tamest first, and the wildest flare last */
    M.slice().sort(function (a, b) { return -wildFirst(a.r, b.r); }).forEach(function (m, i, all) { m.ign = 1.9 + i * 1.7 / all.length; m.tf = 1.2 + R0() * 1.6; m.tp = R0() * 6.28; });
    var B = S.fire;
    cv.style.height = xv.style.height = F(S.VH / S.SH * 100) + "%";
    fcv.style.left = F(B.x0 / 10) + "%"; fcv.style.width = F((B.x1 - B.x0) / 10) + "%";
    fcv.style.top = F(B.y0 / S.SH * 100) + "%"; fcv.style.height = F((B.y1 - B.y0) / S.SH * 100) + "%";

    function sprite(m) {
      var w = m.w, p = PAL[day ? "d" : "n"][w], R = m.R * rs * k * dpr, S2 = Math.ceil(R * 2.6) + 2, cvs = document.createElement("canvas"), x;
      cvs.width = cvs.height = S2; x = cvs.getContext("2d"); x.translate(S2 / 2, S2 / 2);
      var rr = rng(m.seed);
      if (m.st === "not-yet") {  /* eclipsed: the shadow's copper disc, a thin bright ring, one bead of light */
        var ge = x.createRadialGradient(0, 0, 0, 0, 0, R); ge.addColorStop(0, "#2A0A06"); ge.addColorStop(.8, "#5A1C10"); ge.addColorStop(1, "#8A2A14");
        x.fillStyle = ge; x.beginPath(); x.arc(0, 0, R, 0, 6.2832); x.fill();
        x.strokeStyle = "rgba(255,122,42,.6)"; x.lineWidth = Math.max(1, R * .08); x.stroke();
        x.fillStyle = "#FFF1C8"; x.beginPath(); x.arc(R * .7, -R * .7, Math.max(1, R * .1), 0, 6.2832); x.fill();
      } else {
        /* the dark of the disc, lit faintly by the forge (the ashen light), so its full size always reads */
        var gd = x.createRadialGradient(R * .3, 0, 0, 0, 0, R);
        gd.addColorStop(0, "rgba(" + p[3] + "," + (day ? .3 : w === 5 ? .55 : w === 4 ? .34 : .26) + ")"); gd.addColorStop(1, day ? "rgba(" + p[3] + ",.14)" : "rgba(10,13,36,.96)");
        x.fillStyle = day ? "#E2E6F1" : "#0A0D24"; x.beginPath(); x.arc(0, 0, R, 0, 6.2832); x.fill();
        x.fillStyle = gd; x.fill();
        var lit = new Path2D(phase(R, STAGEF[m.st]));
        x.save(); x.shadowColor = p[1]; x.shadowBlur = R * .4;
        var gl = x.createRadialGradient(R * .55, -R * .15, 0, R * .2, 0, R * 1.25);
        gl.addColorStop(0, p[0]); gl.addColorStop(.55, p[1]); gl.addColorStop(1, p[2]);
        x.fillStyle = gl; x.fill(lit); x.restore();
        /* the surface: dark seas and pitted craters, over the whole disc, stronger where it is lit */
        x.save(); x.beginPath(); x.arc(0, 0, R, 0, 6.2832); x.clip();
        for (var i = 0; i < 3; i++) {
          var mx = (rr() - .5) * R, my = (rr() - .5) * R, mr = R * (.25 + rr() * .3), gm = x.createRadialGradient(mx, my, 0, mx, my, mr);
          gm.addColorStop(0, "rgba(20,14,40,.22)"); gm.addColorStop(1, "rgba(20,14,40,0)"); x.fillStyle = gm; x.fillRect(mx - mr, my - mr, mr * 2, mr * 2);
        }
        if (R > 6) for (i = 0; i < 9; i++) {
          var cx2 = (rr() - .5) * 1.6 * R, cy2 = (rr() - .5) * 1.6 * R, cr = R * (.05 + rr() * .08);
          if (cx2 * cx2 + cy2 * cy2 > R * R * .8) continue;
          x.fillStyle = "rgba(0,0,0,.22)"; x.beginPath(); x.arc(cx2, cy2, cr, 0, 6.2832); x.fill();
          x.strokeStyle = "rgba(255,255,255,.2)"; x.lineWidth = cr * .35; x.beginPath(); x.arc(cx2, cy2, cr, -.2, 2.2); x.stroke();
        }
        x.restore();
        x.save(); x.clip(lit); x.strokeStyle = day ? "rgba(255,255,255,.5)" : p[0]; x.globalAlpha = .7; x.lineWidth = Math.max(.8, R * .07);
        x.beginPath(); x.arc(0, 0, Math.max(0, R - x.lineWidth / 2), -1.5708, 1.5708); x.stroke(); x.restore();  /* a sprite sized before layout can be under a pixel */
        x.strokeStyle = day ? "rgba(18,22,58,.45)" : "rgba(" + p[3] + ",.32)"; x.lineWidth = Math.max(.7, R * .045); x.beginPath(); x.arc(0, 0, R, 0, 6.2832); x.stroke();
      }
      m.spr = cvs; m.ss = S2 / 2 / (k * dpr);
      /* the halo, and for the wildest a corona of rays */
      var hr = R * GLOW[w][1], H2 = Math.ceil(hr * 2) + 2, hc = document.createElement("canvas"), y;
      hc.width = hc.height = H2; y = hc.getContext("2d"); y.translate(H2 / 2, H2 / 2);
      var a0 = GLOW[w][0] * (day ? .5 : 1), gh = y.createRadialGradient(0, 0, R * .8, 0, 0, hr);
      gh.addColorStop(0, "rgba(" + p[3] + "," + (a0 * .55).toFixed(3) + ")"); gh.addColorStop(.3, "rgba(" + p[3] + "," + (a0 * .2).toFixed(3) + ")"); gh.addColorStop(1, "rgba(" + p[3] + ",0)");
      y.fillStyle = gh; y.fillRect(-hr, -hr, hr * 2, hr * 2);
      m.halo = hc; m.hs = H2 / 2 / (k * dpr);
      m.rays = null;
      if (w === 5) {
        var rl = R * 4.4, RC = Math.ceil(rl * 2) + 2, rc = document.createElement("canvas"), z;
        rc.width = rc.height = RC; z = rc.getContext("2d"); z.translate(RC / 2, RC / 2);
        z.drawImage(hc, -H2 / 2, -H2 / 2);
        if (day) {
          z.strokeStyle = "rgba(36,72,184,.75)"; z.lineWidth = Math.max(1, R * .06); z.beginPath(); z.arc(0, 0, R * 1.55, 0, 6.2832); z.stroke();
          z.lineWidth = Math.max(1, R * .07);
          for (i = 0; i < 4; i++) { var a4 = i * 1.5708 + .7854; z.beginPath(); z.moveTo(Math.cos(a4) * R * 1.8, Math.sin(a4) * R * 1.8); z.lineTo(Math.cos(a4) * R * 3.3, Math.sin(a4) * R * 3.3); z.stroke(); }
        }
        for (i = 0; i < (day ? 0 : 22); i++) {
          var a = i / 22 * 6.2832 + rr() * .12, len = R * (1.7 + (i % 2 ? .5 : 1.6) * (.6 + rr() * .6)), wd = R * (i % 2 ? .05 : .09);
          var gr = z.createLinearGradient(0, 0, Math.cos(a) * len, Math.sin(a) * len);
          gr.addColorStop(0, day ? "rgba(36,72,184,.55)" : "rgba(234,242,255,.75)"); gr.addColorStop(1, day ? "rgba(36,72,184,0)" : "rgba(159,192,255,0)");
          z.fillStyle = gr; z.beginPath(); z.moveTo(Math.cos(a - 1.5708) * wd, Math.sin(a - 1.5708) * wd); z.lineTo(Math.cos(a) * len, Math.sin(a) * len);
          z.lineTo(Math.cos(a + 1.5708) * wd, Math.sin(a + 1.5708) * wd); z.closePath(); z.fill();
        }
        m.rays = rc; m.rsz = RC / 2 / (k * dpr);
      }
    }
    function stars() {
      if (!sc) return;
      var W = st.clientWidth, Hh = st.clientHeight, d = capDpr(W, Hh), r = rng(.11);
      st.width = Math.round(W * d); st.height = Math.round(Hh * d); sc.setTransform(d, 0, 0, d, 0, 0); sc.clearRect(0, 0, W, Hh);
      if (day) return;
      var COL = [[235, 240, 255], [170, 196, 255], [255, 214, 160]];
      for (var i = 0; i < 900; i++) {
        var x = r() * W, y = r() * Hh, m = Math.pow(r(), 2.6), s = .6 + m * 1.4, col = COL[r() < .7 ? 0 : r() < .6 ? 1 : 2];
        /* faded in from the left, where the reading is, and down to the ridge */
        var fade = Math.min(1, x / (W * .24)) * Math.max(0, Math.min(1, (Hh * .74 - y) / (Hh * .12)));
        if (fade <= 0) continue;
        sc.fillStyle = rgba(col, (.18 + m * .8) * fade); sc.fillRect(x, y, s, s);
        if (m > .72) { sc.fillStyle = rgba(col, .22 * fade); sc.fillRect(x - 3, y + s / 2 - .25, 6 + s, .5); sc.fillRect(x + s / 2 - .25, y - 3, .5, 6 + s); }
      }
    }
    function size() {
      var w = scn.clientWidth || 1000;
      k = w / 1000; rs = Math.max(1, Math.min(1.35, .6 / k, S.fit));
      dpr = capDpr(w, S.VH * k);
      xv.width = cv.width = Math.round(w * dpr); xv.height = cv.height = Math.round(S.VH * k * dpr); boxes = []; dirty = true;
      fdpr = Math.min(devicePixelRatio || 1, 2) * .5;
      fcv.width = Math.max(1, Math.round((B.x1 - B.x0) * k * fdpr)); fcv.height = Math.max(1, Math.round((B.y1 - B.y0) * k * fdpr));
      day = isDay(); M.forEach(sprite); stars();
      still(RM);
    }
    function vis(m, t) { var u = (t - m.ign) / .55; return u <= 0 ? 0 : u >= 1 ? 1 : 1 - Math.pow(1 - u, 3); }
    /* the vault itself is still: it is redrawn while the moons kindle, and after that only when a moon is in hand,
       the size or the theme changes. What moves (embers, the wildest moons' coronas, a shooting star) is on the fx canvas */
    function drawVault(t) {
      c.setTransform(dpr * k, 0, 0, dpr * k, 0, 0); c.clearRect(0, 0, 1000, S.VH);
      var i, m, e, a, add = day ? "source-over" : "lighter";
      /* what each moon unblocks, as a constellation */
      c.lineWidth = .9 / k;
      for (i = 0; i < edges.length; i++) {
        e = edges[i]; a = Math.min(vis(e[0], t), vis(e[1], t)) * Math.min(1, Math.max(0, (t - 3.2) / .8));
        if (a <= 0) continue;
        var hi = hov && (e[0] === hov || e[1] === hov), dx = e[1].x - e[0].x, dy = e[1].y - e[0].y, d = Math.sqrt(dx * dx + dy * dy), ra = e[0].R * rs * 1.5, rb = e[1].R * rs * 1.5;
        if (d < ra + rb + 4) continue;
        c.strokeStyle = (day ? "rgba(122,90,38," : "rgba(212,174,102,") + (a * (hi ? .85 : hov ? .05 : .1)).toFixed(3) + ")";
        /* each line bows up like an orbit, over the fire rather than through it */
        var mx = (e[0].x + e[1].x) / 2, my = (e[0].y + e[1].y) / 2 - d * .22;
        c.beginPath(); c.moveTo(e[0].x + dx / d * ra, e[0].y + dy / d * ra); c.quadraticCurveTo(mx, my, e[1].x - dx / d * rb, e[1].y - dy / d * rb); c.stroke();
      }
      /* the moons, each turned to face the fire */
      for (i = 0; i < M.length; i++) {
        m = M[i]; a = vis(m, t);
        if (a <= 0) continue;
        var lift = m === hov ? 1.16 : 1, pop = (.55 + .45 * a) * lift;
        if (m.w !== 5) {
          c.globalCompositeOperation = add; c.globalAlpha = Math.min(1, a * (m === hov ? 1.4 : 1));
          c.drawImage(m.halo, m.x - m.hs * lift, m.y - m.hs * lift, m.hs * 2 * lift, m.hs * 2 * lift);
        }
        c.globalCompositeOperation = "source-over"; c.globalAlpha = a * (m.w === 1 ? .82 : m.w === 0 ? .75 : 1);
        c.save(); c.translate(m.x, m.y); c.rotate(m.ang); c.scale(pop, pop); c.drawImage(m.spr, -m.ss, -m.ss, m.ss * 2, m.ss * 2); c.restore();
        var u = (t - m.ign) / .9;
        if (u > 0 && u < 1 && !RM) {  /* each kindles with a ring of light, the wildest with a flare */
          c.globalCompositeOperation = add; c.globalAlpha = 1;
          c.strokeStyle = "rgba(" + PAL[day ? "d" : "n"][m.w][3] + "," + ((1 - u) * (m.w === 5 ? .9 : .5)).toFixed(3) + ")";
          c.lineWidth = (m.w === 5 ? 2 : 1.2) / k; c.beginPath(); c.arc(m.x, m.y, m.R * rs * (1 + u * (m.w === 5 ? 3.5 : 1.6)), 0, 6.2832); c.stroke();
        }
      }
      c.globalAlpha = 1; c.globalCompositeOperation = "source-over";
      /* T-2852: a moon the funnel promoted (a family's lead) wears an engraved brass ring, notched once per tenfold of its family */
      for (i = 0; i < M.length; i++) {
        m = M[i]; a = vis(m, t);
        if (!m.fam || a <= 0) continue;
        var fr = m.R * rs * 1.42 + 3, nn = Math.min(4, 1 + Math.floor(Math.log(Math.max(1, +m.fam.size || 1)) / Math.LN10)), j;
        c.globalAlpha = a; c.strokeStyle = day ? "rgba(94,68,24,.8)" : "rgba(212,174,102,.75)"; c.lineWidth = 1 / k;
        c.beginPath(); c.arc(m.x, m.y, fr, 0, 6.2832); c.stroke();
        c.beginPath();
        for (j = 0; j < nn; j++) { var an = -1.5708 + (j - (nn - 1) / 2) * .22; c.moveTo(m.x + Math.cos(an) * fr, m.y + Math.sin(an) * fr); c.lineTo(m.x + Math.cos(an) * (fr + 5), m.y + Math.sin(an) * (fr + 5)); }
        c.stroke();
      }
      c.globalAlpha = 1;
      if (hov) {  /* an engraved reticle, square-cut: four brass corners round the moon in hand */
        var hr = hov.R * rs * 1.55 + 5, hx = hov.x, hy = hov.y, q = hr * .38;
        c.strokeStyle = day ? "rgba(94,68,24,.95)" : "rgba(240,211,146,.95)"; c.lineWidth = 1.4 / k; c.beginPath();
        [[0, -1], [1, 0], [0, 1], [-1, 0]].forEach(function (v) {
          var px = hx + v[0] * hr, py = hy + v[1] * hr;
          c.moveTo(px - v[0] * q - v[1] * q, py - v[1] * q + v[0] * q); c.lineTo(px, py); c.lineTo(px - v[0] * q + v[1] * q, py - v[1] * q - v[0] * q);
        });
        c.stroke();
      }
    }
    /* what moves in the vault, cleared and redrawn only in the boxes it covers */
    function drawFx(t, dt) {
      var i, m, a, add = day ? "source-over" : "lighter", nb = [];
      x2.setTransform(dpr * k, 0, 0, dpr * k, 0, 0);
      boxes.forEach(function (b) { x2.clearRect(b[0], b[1], b[2] - b[0], b[3] - b[1]); });
      /* the wildest moons: their coronas breathe and turn, the mark's star glints on their lit limb */
      for (i = 0; i < M.length; i++) {
        m = M[i]; if (m.w !== 5 || !m.rays) continue;
        a = vis(m, t); if (a <= 0) continue;
        var lift = m === hov ? 1.12 : 1, br = 1 + .06 * Math.sin(t * .8 + m.tp), rz = m.rsz * lift * br;
        x2.globalCompositeOperation = add; x2.globalAlpha = a * (.78 + .22 * Math.sin(t * 1.1 + m.tp));
        x2.save(); x2.translate(m.x, m.y); x2.rotate(t * .035 + m.tp); x2.drawImage(m.rays, -rz, -rz, rz * 2, rz * 2); x2.restore();
        nb.push([m.x - rz - 2, m.y - rz - 2, m.x + rz + 2, m.y + rz + 2]);
        if (!day) {
          var gz = (3 + 2.5 * Math.pow(Math.max(0, Math.sin(t * .7 + m.tp)), 8)) * a * rs, gx = m.x + Math.cos(m.ang) * m.R * rs * .8, gy = m.y + Math.sin(m.ang) * m.R * rs * .8;
          x2.globalCompositeOperation = "lighter"; x2.globalAlpha = .9; x2.fillStyle = "#F4F8FF";
          x2.beginPath(); x2.moveTo(gx, gy - gz * 2.2); x2.lineTo(gx + gz * .25, gy - gz * .25); x2.lineTo(gx + gz * 2.2, gy); x2.lineTo(gx + gz * .25, gy + gz * .25);
          x2.lineTo(gx, gy + gz * 2.2); x2.lineTo(gx - gz * .25, gy + gz * .25); x2.lineTo(gx - gz * 2.2, gy); x2.lineTo(gx - gz * .25, gy - gz * .25); x2.closePath(); x2.fill();
        }
      }
      x2.globalAlpha = 1;
      /* embers off the crown's fire, climbing into the vault */
      if (!RM && t > 1.6) { acc += dt * 6; while (acc > 1 && P.length < 56) { acc--; var sl = Math.random() < .14; P.push({ x: S.CX + (Math.random() - .5) * 110, y: S.TOP - 20 - Math.random() * 50, vy: -(30 + Math.random() * 60), vx: (Math.random() - .5) * 14, life: 0, max: 2.5 + Math.random() * 3.5, T: .7 + Math.random() * .3, ph: Math.random() * 6.28, salt: sl ? (Math.random() * 3) | 0 : -1, w: .9 + Math.random() * 1.1 }); } }
      if (P.length) {
        var bx0 = 1e4, by0 = 1e4, bx1 = -1e4, by1 = -1e4;
        x2.globalCompositeOperation = add;
        for (i = P.length - 1; i >= 0; i--) {
          var p = P[i]; p.life += dt;
          if (p.life > p.max || p.y < -10) { P.splice(i, 1); continue; }
          p.y += p.vy * dt; p.x += (p.vx + Math.sin(t * 1.7 + p.ph) * 9) * dt; p.T = Math.max(0, p.T - dt * .2);
          var col = p.salt >= 0 && p.T < .85 ? SALT[p.salt] : heatRGB(p.T), fa = Math.min(1, p.T * 2.2) * Math.min(1, p.life * 3) * Math.min(1, (p.max - p.life) * 1.2) * (day ? .7 : 1);
          x2.fillStyle = rgba(col, fa * .22); x2.fillRect(p.x - p.w * 1.4, p.y - p.w * 1.4, p.w * 2.8, p.w * 2.8);
          x2.fillStyle = rgba(col, fa); x2.fillRect(p.x - p.w * .5, p.y - p.w * .5, p.w, p.w);
          bx0 = Math.min(bx0, p.x); by0 = Math.min(by0, p.y); bx1 = Math.max(bx1, p.x); by1 = Math.max(by1, p.y);
        }
        if (bx1 > bx0) nb.push([bx0 - 6, by0 - 6, bx1 + 6, by1 + 6]);
      }
      if (!RM && !day) {  /* now and then a shooting star crosses the vault */
        if (!shoot && t > nextShoot) { shoot = { x: 300 + Math.random() * 600, y: 20 + Math.random() * 120, t0: t }; nextShoot = t + 28 + Math.random() * 18; }
        if (shoot) {
          var su = (t - shoot.t0) / .9;
          if (su > 1) shoot = null;
          else {
            var sx = shoot.x - su * 260, sy = shoot.y + su * 110, g2 = x2.createLinearGradient(sx, sy, sx + 110, sy - 46);
            g2.addColorStop(0, "rgba(240,244,255," + (.9 * Math.sin(Math.PI * su)).toFixed(3) + ")"); g2.addColorStop(1, "rgba(240,244,255,0)");
            x2.globalCompositeOperation = "source-over"; x2.strokeStyle = g2; x2.lineWidth = 1.2 / k; x2.beginPath(); x2.moveTo(sx, sy); x2.lineTo(sx + 110, sy - 46); x2.stroke();
            nb.push([sx - 4, sy - 50, sx + 114, sy + 4]);
          }
        }
      }
      x2.globalCompositeOperation = "source-over";
      boxes = nb;
    }
    /* the crown's fire: the mark's flame anatomy, drawn in scene units at half resolution */
    var TL = [], TRr = [];
    function cr(Pp, rev) {
      var nn = Pp.length / 2, X = function (i) { return Pp[2 * (rev ? nn - 1 - i : i)]; }, Y = function (i) { return Pp[2 * (rev ? nn - 1 - i : i) + 1]; };
      for (var i = 0; i < nn - 1; i++) {
        var a = Math.max(0, i - 1), d = Math.min(nn - 1, i + 2);
        fc.bezierCurveTo(X(i) + (X(i + 1) - X(a)) / 6, Y(i) + (Y(i + 1) - Y(a)) / 6, X(i + 1) - (X(d) - X(i)) / 6, Y(i + 1) - (Y(d) - Y(i)) / 6, X(i + 1), Y(i + 1));
      }
    }
    function tongue(cx, by, w, h, t, ph, sw) {
      var N = 6; TL.length = 0; TRr.length = 0;
      for (var i = 0; i <= N; i++) {
        var s = i / N, y = by - h * s, prof = i === N ? 0 : w * Math.pow(Math.sin(Math.PI * (.2 + .8 * s)), .8) * (1 - .3 * s);
        var d = sw * w * Math.pow(s, 1.4) * (.62 * Math.sin(s * 4.6 - t * 5.4 + ph) + .38 * Math.sin(s * 9.3 - t * 9.1 + ph * 1.9));
        TL.push(cx + d - prof, y); TRr.push(cx + d + prof, y);
      }
      fc.beginPath(); fc.moveTo(TRr[0], TRr[1]); fc.quadraticCurveTo(cx, by + w * .5, TL[0], TL[1]); cr(TL, false); cr(TRr, true); fc.closePath();
    }
    function fillG(key, top, bot, alpha) {
      var g = fc.createLinearGradient(0, top, 0, bot);
      GR[key].forEach(function (s) { g.addColorStop(s[0], s[1]); });
      fc.globalAlpha = alpha; fc.fillStyle = g; fc.fill(); fc.globalAlpha = 1;
    }
    function drawFire(t) {
      fc.setTransform(1, 0, 0, 1, 0, 0); fc.clearRect(0, 0, fcv.width, fcv.height);
      var s = fdpr * k; fc.setTransform(s, 0, 0, s, -B.x0 * s, -B.y0 * s);
      var u = t - 1.55, ig = u <= 0 ? 0 : Math.min(1.15, 1 - Math.exp(-5 * u) * Math.cos(6 * u)), BY = S.TOP + 20, cx = S.CX, redA = day ? .3 : .62;
      var heat = (.9 + .06 * Math.sin(t * 2.1) + .04 * Math.sin(t * 5.3)) * ig;
      if (ig > .01) {
        FL.forEach(function (pl) {
          pl.H = pl.h * heat * (1 + .08 * Math.sin(t * 7.3 + pl.p) + .05 * Math.sin(t * 12.9 + pl.p * 2.1) + .04 * Math.sin(t * 2.7 + pl.p));
        });
        [0, 1, 2, 3, 4, 5, 6].forEach(function (fn) {
          FL.forEach(function (pl) {
            var hh = pl.H, w = pl.w, x = cx + pl.x;
            if (hh < 2 || (fn >= 2 && fn <= 4 && w < 12) || (fn === 6 && !pl.core)) return;
            if (fn === 0) { tongue(x, BY + 4, w * 1.32, hh * 1.12, t * .8, pl.p + .5, .7); fillG("red", BY - hh * 1.12, BY + 4, redA); }
            else if (fn === 1) { tongue(x, BY, w, hh, t, pl.p, .9); fillG("out", BY - hh, BY, 1); }
            else if (fn === 2) { tongue(x - w * .32, BY, w * .55, hh * .68, t * 1.15, pl.p + 2.2, 1.3); fillG("out", BY - hh * .68, BY, 1); }
            else if (fn === 3) { tongue(x + w * .44, BY - hh * .06, w * .28, hh * .5, t * 1.3, pl.p + 1, .9); fillG("grn", BY - hh * .56, BY - hh * .06, 1); }
            else if (fn === 4) { tongue(x - w * .46, BY - hh * .05, w * .26, hh * .46, t * 1.25, pl.p + 4, .9); fillG("vio", BY - hh * .51, BY - hh * .05, 1); }
            else if (fn === 5) { tongue(x, BY, w * .62, hh * .74, t * 1.05, pl.p + .3, .8); fillG("mid", BY - hh * .74, BY, 1); }
            else {
              tongue(x, BY, w * .32, hh * .44, t * 1.2, pl.p + .6, .5); fillG("core", BY - hh * .44, BY, 1);
              fc.globalAlpha = .5 + .2 * Math.sin(t * 9 + pl.p); fc.fillStyle = "#F4F8FF";
              fc.beginPath(); fc.moveTo(x - w * .5, BY - 1.5); fc.lineTo(x, BY - w * .16 - 1.5); fc.lineTo(x + w * .5, BY - 1.5); fc.lineTo(x, BY + w * .16 - 1.5); fc.closePath(); fc.fill(); fc.globalAlpha = 1;
            }
          });
        });
        /* the parapet stands in front of the fire */
        fc.globalCompositeOperation = "destination-out"; fc.fillStyle = "#000";
        (S.mask || []).forEach(function (pg) { fc.beginPath(); pg.forEach(function (p, i) { if (i) fc.lineTo(p[0], p[1]); else fc.moveTo(p[0], p[1]); }); fc.closePath(); fc.fill(); });
      }
      /* the course being forged breathes heat, and its next block burns white */
      fc.globalCompositeOperation = "lighter";
      var hotc = S.courses.filter(function (x) { return x.hot; })[0];
      if (hotc) {
        var hy = (hotc.y0 + hotc.y1) / 2, ha = (.12 + .05 * Math.sin(t * 1.7) + .03 * Math.sin(t * 4.3)) * Math.min(1, t / 1.6);
        var gh = fc.createRadialGradient(cx, hy, 0, cx, hy, 150); gh.addColorStop(0, "rgba(255,130,50," + ha.toFixed(3) + ")"); gh.addColorStop(1, "rgba(255,90,20,0)");
        fc.fillStyle = gh; fc.fillRect(cx - 150, hy - 150, 300, 300);
      }
      var gc = fc.createRadialGradient(cx, S.TOP + 18, 0, cx, S.TOP + 18, 120), ca = .22 * ig * (.85 + .15 * Math.sin(t * 11.3) * Math.sin(t * 17.9));
      gc.addColorStop(0, "rgba(255,190,110," + ca.toFixed(3) + ")"); gc.addColorStop(1, "rgba(255,120,40,0)");
      fc.fillStyle = gc; fc.fillRect(cx - 120, S.TOP - 102, 240, 240);
      fc.globalCompositeOperation = "source-over";
    }
    function still(only) {  /* a whole frame now: the arrival's end state, or a reduced-motion still */
      var t = only ? END + 30 : Math.max(nowT, 0);
      if (only || t > END) { drawVault(t); dirty = false; }
      if (only) { drawFx(END + 30, 0); drawFire(4.4); }
    }
    var nowT = 0;
    function loop(now) {
      requestAnimationFrame(loop);
      if (!on || VIEW === "kalevala") { last = now; return; }  // the city hides the sky's moons and fire: draw none
      var t = (now - T0) / 1000; n++; nowT = t;
      if (n & 1) drawFire(t);
      else {
        if (dirty || t < END + .3) { drawVault(t); dirty = false; }
        drawFx(t, Math.min(.1, t - lastV)); lastV = t;
      }
      last = now;
    }
    var pend = 0;
    addEventListener("resize", function () { if (!pend) pend = requestAnimationFrame(function () { pend = 0; size(); }); });
    if (typeof IntersectionObserver === "function") new IntersectionObserver(function (es) { on = es[0].isIntersecting; }).observe($("hero"));
    var mq = matchMedia("(prefers-color-scheme: light)");
    if (mq.addEventListener) mq.addEventListener("change", function () { if (Sky) Sky.retheme(); });
    requestAnimationFrame(function () { size(); if (!RM) requestAnimationFrame(loop); });  /* sized in the frame, not by a forced layout */
    Sky = {
      retheme: function () { day = isDay(); M.forEach(sprite); stars(); dirty = true; still(RM); },
      hover: function (m) { hov = m || null; dirty = true; if (RM) still(true); },
      byId: byId, k: function () { return k * rs; }, RM: RM, now: function () { return nowT; }
    };
    return true;
  }

  /* the moons, in reach of the keyboard: one tab stop, the arrows walk the sky wildest first; focus or hover names a moon,
     a click or Enter opens it */
  function keys() {
    var ol = $("moon-keys"), tag = $("mtag");
    if (!ol || !ol.appendChild) return;
    ol.textContent = "";
    var order = L.moons.slice().sort(function (a, b) { return wildFirst(a.r, b.r); }), btns = [];
    order.forEach(function (m, i) {
      var li = ol.appendChild(document.createElement("li")), b = li.appendChild(document.createElement("button"));
      b.type = "button"; b.className = "mkey"; b.tabIndex = i ? -1 : 0; b.setAttribute("data-moon", m.id);
      b.setAttribute("aria-label", m.id + ", " + String(m.r.title || "") + ". " + String(m.r.tier || "?") + " tier, " + wildWord(m.r) + ", " + STAGEW[m.st] + "." + famWord(m));
      li.style.left = F(m.x / 10) + "%"; li.style.top = F(m.y / L.SH * 100) + "%";
      btns.push(b); b._m = m;
    });
    function show(b) {
      var m = b && b._m;
      if (Sky) Sky.hover(m);
      if (!tag) return;
      if (!m) { tag.hidden = true; return; }
      tag.textContent = "";
      var id = tag.appendChild(document.createElement("b")), ti = tag.appendChild(document.createElement("span")), mt = tag.appendChild(document.createElement("small"));
      id.textContent = m.id; ti.textContent = String(m.r.title || ""); mt.textContent = String(m.r.tier || "?") + " tier · " + wildWord(m.r) + " · " + STAGEW[m.st] + (m.fam ? " ·" + famWord(m) : "");
      tag.className = "mtag w" + m.w + (m.x > 600 ? " lf" : "");
      tag.style.left = F(m.x / 10) + "%"; tag.style.top = F(m.y / L.SH * 100) + "%";
      tag.hidden = false;
    }
    ol.addEventListener("focusin", function (e) { if (e.target._m) show(e.target); });
    ol.addEventListener("focusout", function () { show(null); });
    ol.addEventListener("mouseover", function (e) { if (e.target._m) show(e.target); });
    ol.addEventListener("mouseout", function (e) { if (e.target._m && document.activeElement !== e.target) show(null); });
    ol.addEventListener("keydown", function (e) {
      var i = btns.indexOf(e.target), j = { ArrowRight: i + 1, ArrowDown: i + 1, ArrowLeft: i - 1, ArrowUp: i - 1, Home: 0, End: btns.length - 1 }[e.key];
      if (i < 0 || j == null) return;
      e.preventDefault(); j = (j + btns.length) % btns.length;
      btns[i].tabIndex = -1; btns[j].tabIndex = 0; btns[j].focus();
    });
    var names = $("sky-names");
    if (names) names.addEventListener("mouseover", function (e) { var n = e.target.closest("[data-moon]"); if (n) show(btns.filter(function (b) { return b._m.id === n.getAttribute("data-moon"); })[0]); });
    if (names) names.addEventListener("mouseout", function () { show(null); });
  }

  /* the cinematic arrival: the camera cranes up as the courses are set, the fire catches, the headline is struck, the moons kindle */
  function arrive() {
    var art = $("art"), h1 = $("h1"), RM = !root.classList.contains("anim");
    if (RM) { [].forEach.call(document.querySelectorAll(".sname"), function (n) { n.classList.add("lit"); }); return; }
    art.classList.add("go"); $("scene").classList.add("go");
    if (h1 && !h1.getAttribute("data-split")) {
      h1.setAttribute("data-split", "1");
      var letters = [];
      (function walk(n) {
        [].slice.call(n.childNodes).forEach(function (ch) {
          if (ch.nodeType === 3) {
            var frag = document.createDocumentFragment();
            ch.textContent.split(/(\s+)/).forEach(function (w) {
              if (!w) return;
              if (/^\s+$/.test(w)) { frag.appendChild(document.createTextNode(w)); return; }
              var ws = document.createElement("span"); ws.className = "w";
              for (var i = 0; i < w.length; i++) { var l = document.createElement("span"); l.className = "l"; l.textContent = w[i]; ws.appendChild(l); letters.push(l); }
              frag.appendChild(ws);
            });
            ch.parentNode.replaceChild(frag, ch);
          } else walk(ch);
        });
      })(h1);
      letters.forEach(function (l, i) { l.style.setProperty("--d", ((letters.length - i) * 11) + "ms"); });
    }
    setTimeout(function () { if (h1) h1.classList.add("go"); }, 1600);
    L.named.forEach(function (m) {
      var n = document.querySelector('.sname[data-moon="' + m.id.replace(/["\\]/g, "") + '"]');
      if (n) setTimeout(function () { n.classList.add("lit"); }, (m.ign || 3.4) * 1000 + 250);
    });
    setTimeout(function () { art.classList.add("done"); }, 4600);
  }

  /* the drawer */
  var dlg = $("drawer");
  $("dr-title").tabIndex = -1;
  function box(title, html) { return html ? '<div class="box"><b>' + esc(title) + "</b>" + html + "</div>" : ""; }
  function show(kicker, title, meta, body, hash, tint) {
    dlg.className = tint || "";
    $("dr-kicker").textContent = kicker;
    $("dr-title").textContent = title;
    $("dr-meta").innerHTML = meta || "";
    $("dr-body").innerHTML = body || "";
    dlg.scrollTop = 0;
    if (dlg.open) $("dr-title").focus();  // T-2083: the chip that was clicked is gone; keep focus in the drawer
    else if (dlg.showModal) dlg.showModal(); else dlg.setAttribute("open", "");
    try { history.replaceState(null, "", hash ? "#" + hash : location.pathname + location.search); } catch (e) {}
  }
  function close() { if (dlg.close) dlg.close(); else dlg.removeAttribute("open"); }
  dlg.addEventListener("close", function () { try { history.replaceState(null, "", location.pathname + location.search + (VIEW === "kalevala" ? "#kalevala" : "")); } catch (e) {} });
  $("dr-close").addEventListener("click", close);
  dlg.addEventListener("click", function (e) { if (e.target === dlg) close(); });
  function title(text, fallback) { var m = /^#\s+(.+)$/m.exec(text || ""); return m ? m[1] : fallback; }
  function openDoc(name) {
    var text = D.docs[name];
    if (text == null) return;
    show(name, title(text, name), "", md(text), "doc=" + name);
  }
  function taskList(ts) {
    return ts.length ? "<ul>" + ts.map(function (t) {
      return "<li><code>" + esc(t.id) + "</code> " + esc(say(t.title)) + " — " + t.done + "/" + t.steps + " steps, " + esc(t.status) + "</li>";
    }).join("") + "</ul>" : "";
  }
  function openMission(name) {
    var m = G.missions.filter(function (x) { return x.name === name; })[0];
    if (!m) return;
    var doc = D.docs["gen-" + G.n + "/mission-" + name + ".md"];
    var deps = m.depends.map(function (n) { var d = G.missions.filter(function (x) { return x.n === n; })[0]; return d ? n + " · " + human(d.name) : "#" + n; });
    var meta = box("State", esc(STATE[state(m)])) +
      box("Capabilities", m.caps.map(function (n) { return '<span class="chip">#' + n + " " + esc(capName(n)) + "</span>"; }).join(" ") || esc(m.caps_text)) +
      box("Gaps it closes", '<span class="chips">' + tierChips(m.gaps) + "</span>") +
      box("Waits on", esc(deps.join(", ") || "nothing: it can start now")) +
      box("Tasks", taskList(m.tasks)) +
      (m.critiqued ? box("Critique", '<button type="button" class="doc" data-doc="gen-' + G.n + "/critique-" + esc(name) + '.md">critique-' + esc(name) + ".md</button>") : "");
    show("gen " + G.n + " · mission " + (m.n || ""), "Mission: " + human(name), meta,
      doc ? md(doc) : '<p class="empty">Not written yet. The Seed queues it' + (deps.length ? " after " + esc(deps.join(", ")) : "") + "; it shows here the moment it's filed.</p>",
      "mission=" + name);
  }
  function capSection(text, n) {
    var lines = (text || "").split("\n"), out = [], on = false, start = new RegExp("^" + n + "\\. \\*\\*");
    for (var i = 0; i < lines.length; i++) {
      if (start.test(lines[i])) on = true;
      else if (on && (/^\d+\. \*\*/.test(lines[i]) || /^## /.test(lines[i]))) break;
      if (on) out.push(lines[i].replace(start, "**"));
    }
    return out.join("\n");
  }
  function openCap(n) {
    var c = D.capabilities.filter(function (x) { return x.n === n; })[0];
    if (!c) return;
    var gap = (D.docs["gap-map.md"] || "").split(/^(?=## )/m).filter(function (s) { return new RegExp("^## " + n + "\\.").test(s); })[0] || "";
    var per = D.generations.map(function (g) {
      var ms = missionsFor(n, g);
      return ms.length ? "<li>gen " + g.n + ": " + ms.map(function (m) { return '<button type="button" class="chip" data-mission="' + esc(m.name) + '" data-gen="' + g.n + '">' + esc(human(m.name)) + " · " + STATE[state(m)] + "</button>"; }).join(" ") + "</li>" : "";
    }).join("");
    var k = counts(D.gaps[String(n)] || []);
    var meta = box("Open gaps", k[4] + " tier 4 · " + k[3] + " tier 3" + (k[2] + k[1] ? " · " + (k[2] + k[1]) + " smaller" : "")) +
      box("Missions", per ? "<ul>" + per + "</ul>" : "none yet") +
      box("Louhi", exams().length ? plural(exams().length, "result") + " in Louhi's log (section D)" : "no run yet: mission 1 builds Louhi");
    show("capability #" + n, c.name, meta, "<h4>Your message, §2</h4>" + md(capSection(D.docs["gen-0.md"], n)) +
      (gap ? "<h4>Gap map</h4>" + md(gap.replace(/^## .*\n/, "")) : ""), "cap=" + n);
  }
  function openTask(id) {
    var x = allTasks(G).filter(function (y) { return y.t.id === id; })[0];
    if (!x) return;
    var t = x.t;
    show(t.id + " · " + [t.type, t.tier].filter(Boolean).join(" "), say(t.title),
      box("Progress", t.done + " of " + t.steps + " steps · " + esc(t.status) + bar(t)) +
      box("Now on", esc(t.current || (t.status === "done" ? "closed" : "–"))) +
      box("Mission", x.m ? '<button type="button" class="chip" data-mission="' + esc(x.m.name) + '">' + esc(human(x.m.name)) + "</button>" : "the Seed itself"),
      '<p class="empty">The brief lives in the project\'s Sampo state; every commit it makes names ' + esc(t.id) + ".</p>", "task=" + t.id);
  }
  function openL4() {
    var g = G, crit = g.critiques.map(function (n) { return '<button type="button" class="doc" data-doc="gen-' + g.n + "/critique-" + esc(n) + '.md">critique-' + esc(n) + ".md</button>"; }).join("");
    show("level 4", "Louhi & critique · generation " + g.n,
      box("Louhi", plural(exams().length, "result") + " logged. A rewrite stays only if its generation closed more Louhi scenarios than its parent did; the held-out third only grades a capability as done.") +
      box("Drift & sweep", ["drift", "sweep"].map(function (k) {
        var n = "gen-" + g.n + "/" + k + ".md";
        return D.docs[n] ? '<button type="button" class="doc" data-doc="' + n + '">' + k + ".md · " + esc(g[k] || "no verdict") + "</button>" : "";
      }).join("") || "When the missions close: a cold read checks the seed against your message (drift), then the §7 sweep.") +
      box("Critiques", crit || "Each mission is critiqued when it closes: scenarios closed vs planned, outside signals, coverage cells filled.") +
      box("Lineage", D.docs["lineage.md"] ? '<button type="button" class="doc" data-doc="lineage.md">lineage.md</button>' : "Starts when generation 1 closes."),
      "", "l4");
  }
  /* a moon: what it is, how it would be killed, what came before it; its tier, wild score and stage */
  function openMoon(id) {
    var r = moonRows().filter(function (x) { return String(x.id) === id; })[0];
    if (!r) return;
    var f = moonDoc(id), w = wildOf(r), st = stageOf(r), needed = moonRows().filter(function (x) { return Array.isArray(x.unblocks) && x.unblocks.indexOf(id) >= 0; });
    var chip = function (x) { return '<button type="button" class="chip" data-moon="' + esc(x) + '">' + esc(x) + "</button>"; };
    var pips = '<span class="pips w' + w + '" aria-hidden="true">' + [1, 2, 3, 4, 5].map(function (i) { return "<i" + (i <= w ? ' class="on"' : "") + "></i>"; }).join("") + "</span>";
    var meta = box("Wild", pips + esc(w ? w + " of 5" : "not scored yet")) +
      box("Tier", esc(String(r.tier || "?")) + " counted<br><small>author " + esc(r.author || "?") + " → reviewer " + esc(r.reviewer || "unrated") + "</small>") +
      box("Stage", esc(STAGEW[st])) + box("Domain", esc(f.domain || r.domain || "–")) + box("Novelty", esc(r.novelty || "?") + " of 5") +
      (Array.isArray(r.unblocks) && r.unblocks.length ? box("Unblocks", r.unblocks.map(chip).join(" ")) : "") +
      (needed.length ? box("Unblocked by", needed.map(function (x) { return chip(x.id); }).join(" ")) : "") +
      box("Asked", esc(r.asked === "no" ? "never asked for" : "asked in " + r.asked)) +
      (famOf(id) ? box("Family", esc(famWord({ fam: famOf(id) }).trim())) : "");
    var body = [["What", f.what || r.what], ["Kill", f.kill], ["Prior art", f["prior art"]], ["Better by", f["better by"]], ["Needs", f.needs], ["Lens", f.lens]]
      .filter(function (x) { return x[1]; }).map(function (x) { return "<h4>" + x[0] + "</h4>" + md(x[1]); }).join("");
    show(id + " · moonshot · " + wildWord(r), String(r.title || id), meta, body || '<p class="empty">Its block in moonshot.md is not in this export.</p>', "moon=" + id, "w" + w);
  }

  /* ===== Kalevala (T-3082, T-3266): a tower for each aspect, watched against what you asked, drawn as a city round the
     root tower. The block's strings go in as text only (kel); markup holds numbers, fixed words and names that pass KNAME ===== */
  var VIEW = "genesis", KNAME = /^[a-z][a-z0-9-]{0,40}$/;
  var KSTATE = {  /* rune.export's state: [word, colour, what it means] */
    unsealed: ["unsealed", "red", "Its gen-0 no longer matches its seal."],
    drifted: ["drifted", "red", "Its drift check found it off what you asked."],
    stalled: ["stalled", "orange", "No new generation within the stall window."],
    unscored: ["no exam yet", "muted", "No graded exam run yet."],
    "new": ["first exam", "blue", "One graded run so far: the next one shows its trend."],
    improving: ["improving", "green", "Its last exam closed more scenarios than the run before."],
    falling: ["falling", "red", "Its last exam closed fewer scenarios than the run before."],
    flat: ["flat", "gold", "Its last exam closed as many scenarios as the run before."] };
  var KORDER = ["improving", "new", "flat", "falling", "stalled", "drifted", "unsealed", "unscored"];
  var KACT = { pause: "paused", resume: "resumed", steer_refused: "steer refused" };
  function own(o, k) { return Object.prototype.hasOwnProperty.call(o, k); }
  function kstate(t) { return own(KSTATE, t.state) ? t.state : "unscored"; }
  function kel(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = String(text); return e; }
  function kput(p) { for (var i = 1; i < arguments.length; i++) if (arguments[i]) p.appendChild(arguments[i]); return p; }
  function usd(v) { return "$" + (+v || 0).toFixed(2); }
  function kint(v) { return Math.max(0, Math.floor(+v || 0)); }
  function ksign(v) { v = Math.round(+v || 0); return (v > 0 ? "+" : "") + v; }
  function kBlock() { var K = D && D.kalevala; return K && typeof K === "object" && !K.error && Array.isArray(K.towers) ? K : null; }
  function kList(k) { var K = kBlock(); return K && Array.isArray(K[k]) ? K[k].filter(function (x) { return x && typeof x === "object"; }) : []; }
  function kName(t) { return KNAME.test(String(t.name)) ? String(t.name) : ""; }
  function kRank(t) { var c = KSTATE[kstate(t)][1]; return t.paused ? 0 : t.stuck ? 1 : c === "red" ? 2 : c === "orange" ? 3 : 4; }
  function kSort(a, b) { return kRank(a) - kRank(b) || kint(b.generation) - kint(a.generation) || String(a.name).localeCompare(String(b.name)); }

  /* an exam trend: the closed train scenarios of each graded run, as the README figures draw a series (grid .07/.16, area .14, line 2.5) */
  function ktrend(a, W, H) {
    a = (Array.isArray(a) ? a : []).slice(-24).map(function (v) { return Math.max(0, +v || 0); });
    var n = a.length, p = 8, top = Math.max(1, Math.max.apply(null, a.concat([0]))), big = H >= 120, lx = big ? 34 : p;
    function X(i) { return n > 1 ? lx + i * (W - lx - p) / (n - 1) : (lx + W - p) / 2; }
    function Y(v) { return H - p - (big ? 14 : 0) - v * (H - 2 * p - (big ? 14 : 0)) / top; }
    var o = [line(lx, Y(top), W - p, Y(top), "currentColor", 1, ' class="g" stroke-opacity=".07"'),
      line(lx, Y(top / 2), W - p, Y(top / 2), "currentColor", 1, ' class="g" stroke-opacity=".07"'),
      line(lx, Y(0), W - p, Y(0), "currentColor", 1, ' class="g" stroke-opacity=".16"' + (n ? "" : ' stroke-dasharray="4 4"'))];
    if (big) o.push('<text class="ax" x="' + (lx - 6) + '" y="' + F(Y(top) + 4) + '" text-anchor="end">' + top + "</text>",
      '<text class="ax" x="' + (lx - 6) + '" y="' + F(Y(0) + 4) + '" text-anchor="end">0</text>',
      '<text class="ax" x="' + lx + '" y="' + (H - 4) + '">run 1</text>', n > 1 ? '<text class="ax" x="' + (W - p) + '" y="' + (H - 4) + '" text-anchor="end">run ' + n + "</text>" : "");
    if (n) {
      var P = a.map(function (v, i) { return [X(i), Y(v)]; });
      if (n > 1) o.push(poly(P.concat([[X(n - 1), Y(0)], [X(0), Y(0)]]), "currentColor", ' fill-opacity=".14"'),
        '<polyline points="' + pts(P) + '" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>');
      o.push('<circle cx="' + F(P[n - 1][0]) + '" cy="' + F(P[n - 1][1]) + '" r="3.5" fill="currentColor"/>');
    }
    return '<svg viewBox="0 0 ' + W + " " + H + '" aria-hidden="true">' + o.join("") + "</svg>";
  }

  /* the Kalevala section and the hero's lede numbers, from the export's kalevala block alone */
  function kalevala() {
    var body = $("k-body"), st = $("k-stats"), K = kBlock(), raw = D && D.kalevala;
    body.className = ""; body.textContent = ""; st.textContent = "";
    function stat(dd, dt, cls) { st.appendChild(kput(kel("div", cls), kel("dt", "", dt), kel("dd", "", dd))); }
    if (!K) {
      body.className = "empty";
      body.textContent = raw && typeof raw === "object" && raw.error ? say(String(raw.error))
        : "Kalevala hasn't opened a tower yet: sampo kalevala start opens one for each aspect, each on the root's sealed gen-0.";
      stat("0", "towers open");
      drawCity();
      return;
    }
    var T = kList("towers"), G = kList("gens"), A = kList("acts"), n = T.length, cnt = {};
    var scored = T.filter(function (t) { return t.closed != null; }).length, paused = T.filter(function (t) { return t.paused; }).length;
    T.forEach(function (t) { var s = kstate(t); cnt[s] = (cnt[s] || 0) + 1; });
    stat(String(n), plural(n, "tower").replace(/^\d+ /, "") + " open");
    stat(scored + " of " + n, "with a graded exam");
    stat(String(kint(K.rounds)), "rounds run");
    stat(String(G.length), "Kalevala gens");
    stat(String(paused), "paused");
    stat(usd(K.spent_today), "spent today, of a " + usd(K.share) + " share");
    if (D.next && D.next.action) stat(say(String(D.next.action)), "the root's next", "rev");
    if (K.halted) body.appendChild(kel("p", "kflag", "Halted: " + say(String(K.halted)) + " · sampo kalevala resume restarts it"));

    /* the summary: every tower's state in one bar, and the day's spend against Kalevala's share */
    var sum = kel("div", "ksum"), kb = kel("div", "kbar"), leg = kel("p", "legend"), sh = +K.share || 0, sp = +K.spent_today || 0, fill = kel("i");
    kb.setAttribute("aria-hidden", "true");
    KORDER.forEach(function (s) {
      if (!cnt[s]) return;
      var i = kel("i", "s-" + KSTATE[s][1]);
      i.style.flexGrow = String(cnt[s]);
      i.title = cnt[s] + " " + KSTATE[s][0];
      kb.appendChild(i);
      leg.appendChild(kel("span", "s-" + KSTATE[s][1], cnt[s] + " " + KSTATE[s][0]));
    });
    fill.style.width = (sh > 0 ? Math.min(100, sp / sh * 100) : 0).toFixed(1) + "%";
    body.appendChild(kput(sum, kb, leg, kput(kel("div", "share"), fill, kel("span", "", usd(sp) + " spent today of Kalevala's " + usd(sh) + " share of the day cap"))));

    /* a card for each tower: the ones that need you first */
    body.appendChild(kel("h3", "", "Towers"));
    var grid = kel("div", "ktowers");
    T.slice().sort(kSort).forEach(function (t) {
      var nm = kName(t), s = kstate(t), w = KSTATE[s], g = kint(t.generation), stg = t.stage && typeof t.stage === "object" ? t.stage : null;
      var card = kel(nm ? "a" : "div", "kt s-" + w[1]), chip = kel("span", "kchip", w[0]), id = kel("span", "kt-id", nm || "unnamed");
      if (nm) { card.setAttribute("href", "#tower=" + nm); card.setAttribute("data-ktower", nm); }
      chip.title = w[2]; id.title = nm || "";
      kput(card, kput(kel("p", "kt-top"), id, chip), kel("h4", "", String(t.aspect || nm)),
        kel("p", "kt-gen", "gen " + g + (stg && stg.stage ? " · next: " + String(stg.stage) : "")),
        stg && (stg.action || stg.error) ? kel("p", "kt-act", stg.action ? say(String(stg.action)) : String(stg.error)) : null);
      if (t.closed != null) {
        var tr = kel("div", "ktr");
        tr.innerHTML = ktrend(t.trend, 240, 64);
        kput(card, tr, kel("p", "kt-cap", w[0] + " · closed " + kint(t.closed) + (t.delta != null ? " (" + ksign(t.delta) + ")" : "") + " · " +
          plural(Array.isArray(t.trend) ? t.trend.length : 0, "graded run")));
      } else card.appendChild(kel("p", "kt-cap", "No exam yet · gen " + g + ", unscored"));
      kput(card, kel("p", "kt-foot", usd(t.spend) + " today · " + plural(kint(t.missions), "open mission") + " · idle " + (Math.round((+t.idle_h || 0) * 10) / 10) + "h"),
        t.paused ? kel("p", "kflag", "paused: " + String(t.paused)) : null,
        t.stuck ? kel("p", "kflag", "stuck: no gain in its last three exams") : null);
      grid.appendChild(card);
    });
    body.appendChild(grid);

    /* Kalevala's generations, newest first: which towers each one ran, what it was based on, and its verdict */
    body.appendChild(kel("h3", "", "Kalevala's generations"));
    if (G.length) {
      var tb = kel("table"), hr = kel("tr"), rows = kel("tbody");
      ["Gen", "Towers", "Based on", "Verdict", "Base", "Why"].forEach(function (h) { hr.appendChild(kel("th", "", h)); });
      G.slice().reverse().forEach(function (g) {
        rows.appendChild(kput(kel("tr"), kput(kel("td"), kel("b", "", "gen " + kint(g.n))),
          kel("td", "", Array.isArray(g.set) ? g.set.map(String).join(", ") : ""), kel("td", "", String(g.based_on || "–")),
          kel("td", "", String(g.verdict || "not judged yet")), kel("td", "", String(g.base || "–")), kel("td", "", String(g.reason || ""))));
      });
      body.appendChild(kput(kel("div", "tbl"), kput(tb, kput(kel("thead"), hr), rows)));
    } else body.appendChild(kel("p", "empty", "No Kalevala generation yet: the first round sets its baseline."));

    /* Väinämöinen's acts, newest first, each with its reason and the line that undoes it */
    body.appendChild(kel("h3", "", "Väinämöinen's acts"));
    if (A.length) body.appendChild(kActs(A));
    else body.appendChild(kel("p", "empty", "No act yet: Väinämöinen logs each pause, resume and refused steer here."));
    drawCity();
  }
  function kActs(A) {
    var ol = kel("ol", "kacts");
    A.slice().reverse().forEach(function (a) {
      var head = kput(kel("p"), kel("b", "", own(KACT, a.act) ? KACT[a.act] : "act"), kel("span", "", String(a.tower || "every tower")), a.at ? kel("small", "", ago(a.at)) : null);
      ol.appendChild(kput(kel("li"), head, kel("p", "", String(a.reason || "")), kput(kel("p", "kundo", "undo: "), kel("code", "", say(String(a.undo || "none"))))));
    });
    return ol;
  }

  /* the city: one tower per aspect round the root (#troot, drawn by the scene, at .6), as tall as its generations, its
     windows and beacon lit by its state. The tallest stand in the back row, the higher gens nearest the root */
  var KGLOW = ["red", "orange", "gold", "green", "blue", "muted"];
  function citySVG(T, S, linked) {
    var CX = S.CX, GY = S.GY, cap = Math.max(100, .54 * (GY - S.TOP + 40)), RT = .6;
    var list = T.filter(kName).sort(function (a, b) { return kint(b.generation) - kint(a.generation) || String(a.name).localeCompare(String(b.name)); });
    var N = list.length, nb = Math.ceil(N * .38), nm = Math.ceil((N - nb) * .55);
    var rows = [[list.slice(0, nb), .72, GY - 6, 40], [list.slice(nb, nb + nm), .92, GY + 8, 95], [list.slice(nb + nm), 1.1, GY + 24, 130]];
    var drawn = rows.map(function (R) {
      var ts = R[0], s = R[1], y = R[2], iv = [[190, CX - R[3]], [CX + R[3], 935]]  /* 935: the right-most tower's foot clears the scene's (the viewport's) edge */, tot = iv[0][1] - iv[0][0] + iv[1][1] - iv[1][0], slots = [];
      for (var j = 0; j < ts.length; j++) {
        var at = (j + .5) * tot / ts.length, k = at > iv[0][1] - iv[0][0] ? 1 : 0;
        slots.push(k ? iv[1][0] + at - (iv[0][1] - iv[0][0]) : iv[0][0] + at);
      }
      slots.sort(function (a, b) { return Math.abs(a - CX) - Math.abs(b - CX); });
      return ts.map(function (t, j) { return { t: t, x: slots[j] + (hash(t.name) - .5) * 8, y: y, s: s }; })
        .sort(function (a, b) { return a.x - b.x; })  /* left to right: the tab order reads the row as the eye does */
        .map(ktower).join("");
    });
    var defs = "<defs>" + KGLOW.map(function (c) { return '<radialGradient id="kg-' + c + '" class="s-' + c + '"><stop offset="0" stop-opacity=".55"/><stop offset="1" stop-opacity="0"/></radialGradient>'; }).join("") + "</defs>";
    var top = GY - (GY - S.TOP) * RT;
    var root = linked ? '<a href="#" data-view="genesis" aria-label="The root tower: back to Genesis"><title>The root tower</title>' +
      '<ellipse cx="' + CX + '" cy="' + F(top + 6) + '" rx="70" ry="70" fill="url(#pool)" opacity=".7"/>' +
      '<use href="#troot" transform="translate(' + CX + " " + F(GY) + ") scale(" + RT + ") translate(" + -CX + " " + F(-GY) + ')"/></a>' : "";
    return defs + '<g class="ctw" style="transform-origin:' + CX + "px " + F(GY) + 'px">' + (linked ? '<use href="#tground"/>' : "") +
      drawn[0] + root + drawn[1] + drawn[2] + "</g>";
    function ktower(o) {
      var t = o.t, x = o.x, y = o.y, s = o.s, nm = kName(t), g = Math.min(kint(t.generation), 12), w = KSTATE[kstate(t)], c = w[1];
      var hw = 26 * s * (.85 + .3 * hash(nm + "w")), H = Math.min(cap, s * (120 + 22 * g)), tw = hw * .78, yb = y - 6 * s, yt = yb - H, q = [];
      var dim = t.paused ? ' class="kx"' : "", lit = t.paused ? "" : ' class="kw"' + (t.stuck ? ' opacity=".45"' : "");
      q.push('<ellipse cx="' + F(x) + '" cy="' + F(y) + '" rx="' + F(hw * 2) + '" ry="' + F(5 * s) + '" fill="url(#pool)" opacity=".35"/>');
      q.push(poly([[x - hw * 1.25, y], [x + hw * 1.25, y], [x + hw * 1.15, yb], [x - hw * 1.15, yb]], "url(#sF)"));
      q.push(poly([[x - hw, yb], [x - hw * .2, yb], [x - tw * .2, yt], [x - tw, yt]], "url(#sL)"));
      q.push(poly([[x - hw * .2, yb], [x + hw, yb], [x + tw, yt], [x - tw * .2, yt]], "url(#sF)"));
      q.push(line(x + hw, yb, x + tw, yt, "url(#sR)", F(2 * s)));
      for (var i = 1; i <= g; i++) {  /* a tempered course for each generation, as the root's */
        var by = yb - i * (H - 10 * s) / (g + 1), bw = hw - (hw - tw) * (yb - by) / H;
        q.push(poly([[x - bw - 1.5 * s, by - 2.5 * s], [x + bw + 1.5 * s, by - 2.5 * s], [x + bw + 1.5 * s, by + 1 * s], [x - bw - 1.5 * s, by + 1 * s]], "url(#tp" + i % 4 + ")"));
      }
      for (var k = 0, nw = 1 + Math.floor(H / (46 * s)); k < nw; k++) {  /* window slits up the shaft */
        var wy = yb - 14 * s - k * (H - 26 * s) / Math.max(1, nw);
        q.push('<rect x="' + F(x - hw * .62) + '" y="' + F(wy - 8 * s) + '" width="' + F(2.6 * s) + '" height="' + F(8 * s) + '"' + (lit || dim) + "/>",
          '<rect x="' + F(x + hw * .38) + '" y="' + F(wy - 8 * s) + '" width="' + F(2.6 * s) + '" height="' + F(8 * s) + '"' + (lit ? lit + ' fill-opacity=".6"' : dim) + "/>");
      }
      var ry = yt - 5 * s, rh = hw * 1.1;
      q.push(poly([[x - tw - 3 * s, yt], [x + tw + 3 * s, yt], [x + tw + 3 * s, ry], [x - tw - 3 * s, ry]], "url(#sT)"));
      q.push(poly([[x - tw - 2 * s, ry], [x, ry - rh], [x, ry]], "url(#sL)"), poly([[x, ry - rh], [x + tw + 2 * s, ry], [x, ry]], "url(#sR)"));
      if (!t.paused) q.push('<circle cx="' + F(x) + '" cy="' + F(ry - rh - 4 * s) + '" r="' + F(16 * s) + '" fill="url(#kg-' + c + ')"/>');
      q.push('<circle cx="' + F(x) + '" cy="' + F(ry - rh - 4 * s) + '" r="' + F(2.6 * s) + '"' + (t.paused ? ' class="kx"' : ' class="kb"') + "/>");
      return '<a href="#tower=' + nm + '" data-ktower="' + nm + '" aria-label="' + nm + " · " + w[0] + " · gen " + kint(t.generation) + (t.paused ? " · paused" : "") +
        (t.stuck ? " · stuck" : "") + '"><title>' + nm + " · " + w[0] + '</title><g class="kt-t s-' + c + '" style="--i:' + kRank(t) + '">' + q.join("") + "</g></a>";
    }
  }
  function drawCity() {
    var S = L || { CX: 590, GY: 960, SH: 1000, TOP: 400 }, ca = $("city-art"), c = $("city");
    if (!ca) return;
    ca.innerHTML = citySVG(kList("towers"), S, !!L);
    if (c && c.setAttribute) c.setAttribute("viewBox", "0 0 1000 " + F(S.SH));
    var sc = $("scene");  /* the root's foot, where the pull-back is centred */
    if (sc && sc.style && sc.style.setProperty) sc.style.setProperty("--gy", F(S.GY / S.SH * 100) + "%");
  }
  /* the switch: Genesis (the root tower) or Kalevala (the city); the city pulls back from the root, and the hash keeps it */
  function setView(v, keep) {
    VIEW = v === "kalevala" ? "kalevala" : "genesis";
    var mn = $("main");
    if (mn && mn.classList) mn.classList.toggle("kv", VIEW === "kalevala");
    [["vs-genesis", "genesis"], ["vs-kalevala", "kalevala"]].forEach(function (b) { var e = $(b[0]); if (e && e.setAttribute) e.setAttribute("aria-pressed", String(VIEW === b[1])); });
    if (!keep) try { history.replaceState(null, "", VIEW === "kalevala" ? "#kalevala" : location.pathname + location.search); } catch (e) {}
  }
  /* one tower in the drawer: its state, stage, exam trend, spend, its acts and the gens it ran in */
  function openKTower(name) {
    var t = kList("towers").filter(function (x) { return kName(x) === name; })[0];
    if (!t) return;
    setView("kalevala");
    var w = KSTATE[kstate(t)], stg = t.stage && typeof t.stage === "object" ? t.stage : {};
    show("Kalevala tower · gen " + kint(t.generation), String(t.aspect || name), "", "", "tower=" + name);
    var meta = $("dr-meta"), body = $("dr-body");
    function bx(k, v) { if (v) meta.appendChild(kput(kel("div", "box"), kel("b", "", k), kel("span", "", v))); }
    bx("Tower", name); bx("State", w[0] + ": " + w[2]); bx("Generation", "gen " + kint(t.generation));
    bx("Next step", stg.action ? say(String(stg.action)) : stg.error ? String(stg.error) : ""); bx("Exam", t.closed == null ? "no exam yet" : "closed " + kint(t.closed) + (t.delta != null ? " (" + ksign(t.delta) + ")" : ""));
    bx("Spend today", usd(t.spend)); bx("Missions", plural(kint(t.missions), "open mission")); bx("Idle", (Math.round((+t.idle_h || 0) * 10) / 10) + " h");
    bx("Drift", t.drift ? String(t.drift) : "not checked"); bx("Seal", t.sealed ? "gen-0 matches its seal" : "gen-0 no longer matches its seal");
    bx("Paused", t.paused ? String(t.paused) : ""); bx("Stuck", t.stuck ? "no gain in its last three exams" : "");
    body.appendChild(kel("h4", "", "Exam trend"));
    if (t.closed != null) { var tr = kel("div", "ktr s-" + w[1]); tr.innerHTML = ktrend(t.trend, 560, 160); body.appendChild(tr); }
    else body.appendChild(kel("p", "empty", "No graded exam yet: its first run starts the trend."));
    var A = kList("acts").filter(function (a) { return a.tower === name; }), G = kList("gens").filter(function (g) { return Array.isArray(g.set) && g.set.indexOf(name) >= 0; });
    body.appendChild(kel("h4", "", "Väinämöinen's acts"));
    body.appendChild(A.length ? kActs(A) : kel("p", "empty", "No act on this tower yet."));
    body.appendChild(kel("h4", "", "Kalevala gens it ran in"));
    body.appendChild(G.length ? kput.apply(null, [kel("ul")].concat(G.map(function (g) { return kel("li", "", "gen " + kint(g.n) + " · " + String(g.verdict || "not judged yet") + (g.reason ? " · " + String(g.reason) : "")); }))) : kel("p", "empty", "None yet."));
  }

  document.addEventListener("click", function (e) {
    var t = e.target.closest && e.target.closest("[data-doc],[data-mission],[data-cap],[data-task],[data-rule],[data-l4],[data-moon],[data-gen-pick],[data-ktower],[data-view]");
    if (t && t.getAttribute("data-view")) { e.preventDefault(); setView(t.getAttribute("data-view")); return; }  // the switch works before the data
    if (!t || !D) return;
    if (t.hasAttribute("data-ktower")) { e.preventDefault(); openKTower(t.getAttribute("data-ktower")); return; }
    if (t.hasAttribute("data-gen-pick")) { selectGen(+t.getAttribute("data-gen-pick")); return; }  // the anchor then scrolls to A
    if (t.closest("a[href]")) e.preventDefault();  // a block or rune inside a course: the drawer, not the jump
    if (t.hasAttribute("data-gen")) selectGen(+t.getAttribute("data-gen"));
    if (t.hasAttribute("data-moon")) openMoon(t.getAttribute("data-moon"));
    else if (t.hasAttribute("data-doc")) openDoc(t.getAttribute("data-doc"));
    else if (t.hasAttribute("data-mission")) openMission(t.getAttribute("data-mission"));
    else if (t.hasAttribute("data-cap")) openCap(+t.getAttribute("data-cap"));
    else if (t.hasAttribute("data-task")) openTask(t.getAttribute("data-task"));
    else if (t.hasAttribute("data-rule")) show("gen " + G.n + " · standing rule " + (+t.getAttribute("data-rule") + 1), "Standing rule", "", md(G.rules[+t.getAttribute("data-rule")]));
    else openL4();
  });
  ["mouseover", "focusin"].forEach(function (ev) {
    document.addEventListener(ev, function (e) { var t = e.target.closest && e.target.closest(".tile"); if (t) highlight(t); });
  });
  ["mouseout", "focusout"].forEach(function (ev) {
    document.addEventListener(ev, function (e) { if (e.target.closest && e.target.closest(".tile")) highlight(null); });
  });

  function selectGen(n) {
    G = D.generations.filter(function (g) { return g.n === n; })[0] || D.generations[D.generations.length - 1];
    $("gens").innerHTML = D.generations.map(function (g) {
      return '<button type="button" aria-pressed="' + (g === G) + '" data-pick="' + g.n + '">gen ' + g.n + "</button>";
    }).join("");
    stats(); tower(); caps(); ambition(); rules();
    if (LIVE) [].forEach.call(document.querySelectorAll(".crs[data-gen-pick]"), function (a) { a.classList.toggle("sel", +a.getAttribute("data-gen-pick") === G.n); });
  }
  $("gens").addEventListener("click", function (e) { var b = e.target.closest("[data-pick]"); if (b) selectGen(+b.getAttribute("data-pick")); });

  if (/^#(kalevala$|tower=)/.test(location.hash)) setView("kalevala", true);  // a deep link lands in the city before the data: no root-tower flash
  fetch("genesis.json", { cache: "no-cache" }).then(function (r) {
    if (!r.ok) throw new Error(r.status);
    return r.json();
  }).then(function (data) {
    D = roles(data);
    if (!D.generations.length) D.generations = [{ n: 1, seed: false, missions: [], critiques: [], tasks: [], rules: [] }];
    selectGen(D.generations[D.generations.length - 1].n);
    exam();
    try { longRow(); } catch (e) {}
    try { steerRow(); } catch (e) {}
    try { moonshotTable(); } catch (e) {}
    try { moonsScale(); } catch (e) {}
    try { moonsCatalogue(); } catch (e) {}
    try { scorecard(); } catch (e) {}  // a odd scorecard row costs its table, never the docs or the route below
    docs();
    try { scene(); } catch (e) { if (typeof console !== "undefined") console.warn("the scene kept its still: " + e); }  // the art never costs the data
    try { kalevala(); } catch (e) { if (typeof console !== "undefined") console.warn("Kalevala kept its note: " + e); }
    var h = location.hash.slice(1);
    try { h = decodeURIComponent(h); } catch (e) { h = ""; }  // a malformed hash (#doc=%) opens nothing, breaks nothing
    var m = /^(tower|doc|cap|mission|task|moon)=(.+)$/.exec(h);
    if (m) ({ tower: openKTower, doc: openDoc, cap: function (v) { openCap(+v); }, mission: openMission, task: openTask, moon: openMoon })[m[1]](m[2]);
    else if (h === "l4") openL4();
    else if (h === "kalevala") setView("kalevala");
  }).catch(function () {
    var kb = $("k-body"); if (kb) { kb.className = "empty"; kb.textContent = "Kalevala's towers come from genesis.json, which didn't load."; }
    $("tower-body").insertAdjacentHTML("afterbegin", '<p class="empty">The tower\'s live data (genesis.json) didn\'t load, so this is the copy the page was built with. Opened from disk? Serve the folder, e.g. from the site build.</p>');
    var art = $("art"); if (art && art.classList) art.classList.add("go", "done");
  });
})();
