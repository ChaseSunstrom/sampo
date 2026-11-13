"""The feature pages (T-1220): content as data, charts as calls into build.py's helpers over site/data/stats.json.
Every claim here is something Sampo does today; the charts are its own record of building itself."""
import re

import os

from build import GROUPS, TYPES, columns, dials, esc, hbars, line, pairs, table, tiles, verdicts

LIBRARY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "plugin", "skills", "playbooks",
                       "references")


def _sketchbook(s):
    """T-1784: every shader look card running live with its Sketch preset (fmscene draws the same page)."""
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "plugin", "lib"))
    from sampo import scene
    return scene.sketchbook_html(scene.sketch_cards(), page=False)


def _tui():
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "plugin", "lib"))
    from sampo import smithtui
    return smithtui


def _keys(s):
    """T-2061: every key fm code tui binds, grouped as its own keys poster (?) groups them, read from its key map."""
    t = _tui()
    keys = {}
    for k, cmd in t.DEFAULTS["keys"].items():
        name = t.KEY_NAMES.get(k) or t.key_name(k)
        if name not in keys.setdefault(cmd, []):
            keys[cmd].append(name)
    labels = dict(t.COMMAND_LABELS)
    placed = {c for _, cmds in t.POSTER for c in cmds}
    rows = []
    for group, cmds in [*t.POSTER, ("more", [c for c in keys if c not in placed])]:
        for i, c in enumerate(c for c in cmds if c in keys):
            cli = f" <small><code>{esc(t.CLI[c])}</code></small>" if c in t.CLI else ""
            rows.append(f"<tr><td>{'' if i else esc(group)}</td><td>{' '.join(f'<kbd>{esc(n)}</kbd>' for n in keys[c])}"
                        f"</td><td>{esc(labels.get(c, c))}{cli}</td></tr>")
    return ('<div class="verdicts"><table><thead><tr><th>Group</th><th>Key</th><th>What it does</th></tr></thead>'
            "<tbody>" + "".join(rows) + "</tbody></table></div>")


def _books(stage):
    """[(name, title, when-to-use line, text)] for each playbook in one stage folder."""
    folder, out = os.path.join(LIBRARY, stage), []
    for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else ():
        if name.endswith(".md") and name != "INDEX.md":
            with open(os.path.join(folder, name), encoding="utf-8") as f:
                text = f.read()
            title = re.search(r"(?m)^# (.+)$", text)
            use = re.search(r"(?ms)^(Use when\b.+?)(?:\n\s*\n|\Z)", text)
            out.append((name[:-3], title.group(1) if title else name[:-3], " ".join(use.group(1).split()) if use else "",
                        text))
    return out


def gallery():
    """T-1785: each UI playbook's worked example, from its "Example: `references/ui/...html`" line:
    [(name, title, when-to-use line, source path)]; a playbook without the line stops the build."""
    out = []
    for name, title, use, text in _books("ui"):
        m = re.search(r"(?m)^Example: `(references/ui/(?:examples|templates)/[\w-]+\.html)`", text)
        if not m:
            raise SystemExit(f"pages.py: ui/{name}.md has no `Example: \\`references/ui/examples/NAME.html\\`` line")
        out.append((name, title, use, os.path.join(os.path.dirname(LIBRARY), m.group(1))))
    return out


def _gallery(s):
    books = gallery()
    return tiles([("worked examples", len(books))]) + '<div class="fgrid">' + "".join(
        f'<a class="fcard" href="../gallery/{esc(name)}.html"><b>{esc(title)}</b><span>{esc(use)}</span>'
        f'<span><code>ui/{esc(name)}.md</code></span></a>' for name, title, use, _ in books) + "</div>"


def _field_guide(s):
    """T-1525: every playbook in the library by stage, with the line that says when to use it."""
    stages = {}
    for stage in sorted(os.listdir(LIBRARY)):
        for name, title, use, _ in _books(stage):
            stages.setdefault(stage, []).append((name, title, use))
    return tiles([(stage, len(books)) for stage, books in stages.items()]) + "".join(
        f"<section><h2>{esc(stage)}</h2><dl>" + "".join(
            f"<dt><code>{esc(stage)}/{esc(name)}</code> {esc(title)}</dt><dd>{esc(use)}</dd>"
            for name, title, use in books) + "</dl></section>" for stage, books in stages.items())


def _daily_types(s):
    return columns("Tasks closed per day, by type", "Every task Sampo closed on its own code, from the ledger.",
                   s["daily"], TYPES)


def _sizes(s):
    return hbars("Closed tasks by size", "S is a small obvious change, M several files or a design choice, L cross-cutting.",
                 [("S  small", s["tiers"]["S"]), ("M  medium", s["tiers"]["M"]), ("L  large", s["tiers"]["L"])])


def _judgment(s):
    """T-2344: the scores from fm outcomes --judgment --json (site/stats.py stores them), or why there are none yet."""
    cal = s.get("calibration") or {}
    br, r, bars = cal.get("brier") or {}, cal.get("races") or {}, cal.get("bars") or {}
    two = lambda x: f"{x:.2f}" if isinstance(x, (int, float)) else "n/a"  # noqa: E731
    row = tiles([("confidences scored", cal.get("scored") or 0), ("waiting on outcomes", cal.get("pending") or 0),
                 ("Brier, stated", two(br.get("stated"))), ("Brier, base rate", two(br.get("base"))),
                 ("first choice lost a race", f"{r.get('first_lost', 0)} of {r.get('judged', 0)}"),
                 ("race bar", f"{bars['race']}%" if "race" in bars else "n/a")])
    rel = [(f"{k} {b['lo']}–{b['hi']}% · n {b['n']}", b["pct"]) for k, bs in (cal.get("kinds") or {}).items() for b in bs]
    rel += [(f"{k} {b['lo']}–{b['hi']}% · n {b['n']}, so far", b["pct"])  # T-2498: a week-old read, never a score
            for k, bs in (cal.get("interim") or {}).items() for b in bs]
    chart = hbars("Held, by stated confidence", "Each bucket's share held. Rows marked so far closed 7 or more days ago "
                  "and are not scored yet.", rel, unit="%") if rel else (
        "<p>No stated confidence has an outcome or a 7-day read yet, so there is no reliability chart.</p>")
    return (row if cal.get("scored") else row + ("<p>Its own numbers are not scored yet: stated confidences wait on "
                                                 "outcomes, and the ledger needs 20 before it moves a bar.</p>")) + chart


def _lacks(s):
    """T-2378: #5 from the ledger (site/stats.py's tools block): misses met, searches, tools proved, grants asked."""
    t = s.get("tools") or {}
    return tiles([("tools found missing", t.get("misses", 0)), ("misses resolved by a run", t.get("resolved", 0)),
                  ("shelf searches", t.get("searches", 0)), ("tools built and proved", t.get("proved", 0)),
                  ("installs asked of you", t.get("queued", 0)), ("installs you said no to", t.get("refused", 0))])


def _proof(s):
    """T-2395: fm prove --rollup's rollup (site/stats.py's proof block): the count, the kinds most need, the count per day."""
    pr = s.get("proof") or {}
    top = sorted((pr.get("counts") or {}).items(), key=lambda kv: -kv[1])[:5]
    row = tiles([("done tasks unproven", pr.get("n", 0))] + [(f"need {k}", v) for k, v in top])
    hist = pr.get("history") or []
    return row + (columns("Unproven done tasks per day", "sampo prove --rollup's count, last 30 days of closes, each time the site "
                          "was built.", hist, ["n"]) if len(hist) > 1 else "")


def _self_running(s):
    """T-2364: Mission 8's figures (site/stats.py's self_running block): triggers with precision and route, installs
    waiting, recoveries, trial verdicts, prediction hit rates beside their baselines, the lean start per generation."""
    sr = s.get("self_running") or {}
    pct = lambda x: f"{x:.0%}" if isinstance(x, (int, float)) else "n/a"  # noqa: E731
    row = tiles([("installs waiting for you", sr.get("installs", 0)), ("lanes recovering", sr.get("recoveries", 0))]
                + [(f"trials {k}", v) for k, v in sorted((sr.get("trials") or {}).items())]
                + [(f"{k.get('kind')} predictions vs baseline", f"{pct(k.get('hit'))} vs {pct(k.get('base_hit'))}")
                   for k in sr.get("predictions") or []])
    trig = sr.get("triggers") or []
    if trig:
        row += hbars("Triggers by precision", "Each trigger that filed work, its share of tasks that got done, and "
                     "where it routes.", [(f"{t['trigger']} → {t.get('route') or 'inbox'}", t.get("precision") or 0)
                                         for t in trig[:12]])
    lean = sr.get("lean") or []
    if len(lean) > 1:
        row += columns("Rules chars at each generation's start", "The lean ratchet: a generation may not end bigger "
                       "than it began.", [{"day": f"gen-{x['gen']}", "n": x.get("rules chars", 0)} for x in lean], ["n"],
                       short=lambda v: v)
    return row


def _decisions(s):
    """T-2348: fm decide's rows newest first on one scrollable tape (site/stats.py's decisions block), each with its
    kind (a door when costly or outward), its stated confidence and the later row that reversed or revisited it."""
    tape = s.get("decisions") or []
    row = tiles([("decisions", len(tape)), ("doors (costly or outward)", sum(x["kind"] != "reversible" for x in tape)),
                 ("reversed or revisited later", sum(bool(x.get("later")) for x in tape))])
    items = "".join(
        f'<li><time>{esc(x["date"])}</time> <b>{esc(x["kind"])}</b>'
        + (f' {int(x["confidence"])}%' if isinstance(x.get("confidence"), int) else "") + f' {esc(_say(x["text"]))}'
        + (f' <em>{esc(x["later"]["how"])} {esc(x["later"]["date"])}</em>' if x.get("later") else "") + "</li>"
        for x in tape)
    return row + (f'<ol class="tape" style="max-height:70vh;overflow-y:auto">{items}</ol>' if items else "")


def _models(s):
    """T-2489: the model layer's exam replay (site/stats.py's models block): per use, the rule's hits beside the booked
    model answers, the model's Brier and its latency; why there are none yet when no run has scored one."""
    mo = s.get("models") or {}
    rep = {k: v for k, v in (mo.get("replay") or {}).items() if isinstance(v, dict)}
    two = lambda x: f"{x:.2f}" if isinstance(x, (int, float)) else "n/a"  # noqa: E731
    row = tiles([("uses scored", len(rep)), ("Louhi run", mo.get("run") or "none yet"),
                 ("model answers booked", sum(v.get("model_n") or 0 for v in rep.values()))])
    if not rep:
        return row + "<p>No Louhi run has scored a use yet: the shadow rows wait for tasks to close with their truth.</p>"
    return row + table(["use", "rule right", "model right", "model Brier", "model ms"], [
        (u, f"{v.get('path_hits', 0)} of {v.get('n', 0)}", f"{v.get('model_hits', 0)} of {v.get('model_n', 0)}",
         two(v.get("brier")), v.get("ms") if isinstance(v.get("ms"), (int, float)) else "n/a") for u, v in sorted(rep.items())])


def _model_bench(s):
    """T-3093: the model bench (louhi.models_view over research/models/bench.jsonl): per use, each backend's latest
    complete run beside the rules path and the majority baseline; 'stale since gen N' once a later run failed or skipped."""
    mb = (s.get("model_bench") or {}).get("uses") or {}
    if not mb:
        return "<p>No model bench run yet: it runs once per Rune generation, on this machine, at no cost.</p>"
    pct = lambda x: f"{x * 100:.1f}%" if isinstance(x, (int, float)) else "n/a"  # noqa: E731
    two = lambda x: f"{x:.3f}" if isinstance(x, (int, float)) else "n/a"  # noqa: E731
    ms = lambda x: f"{x:.0f} ms" if isinstance(x, (int, float)) else "n/a"  # noqa: E731
    out = ""
    for use, d in sorted(mb.items()):
        rows = []
        for b in d["backends"]:
            v = b.get("verdict") or {}
            rows.append((esc(b["backend"]) + (f' <em>stale since {esc(b["stale_since"])}</em>' if b.get("stale_since") else "")
                         + ("" if b.get("complete") else " <em>no complete run</em>"),
                         pct(b.get("accuracy")), two(b.get("brier")), ms(b.get("p50_ms")), ms(b.get("p95_ms")),
                         b.get("n", 0), b.get("errors", 0), f'{b.get("frozen_n", 0)} frozen, {b.get("rolling_n", 0)} rolling',
                         "passes" if v.get("pass") else esc(v.get("why") or "not judged")))
        for label, k in (("rules path", "path"), ("majority baseline", "majority")):
            r = d.get(k) or {}
            rows.append((label, pct(r.get("accuracy")), two(r.get("brier")), "-", "-", r.get("n", 0), "-", "-", "-"))
        ceil = d.get("ceiling")
        head = ["Backend", "accuracy", "Brier", "p50", "p95", "rows", "timeouts", "held-out set", "promotion"]
        body = "".join("<tr>" + "".join(f"<td>{c if i == 0 and k < len(d['backends']) else esc(str(c))}</td>" for i, c in enumerate(r))
                       + "</tr>" for k, r in enumerate(rows))
        out += (f"<h3>{esc(use)}</h3>" + '<div class="verdicts"><table><thead><tr>' + "".join(f"<th>{h}</th>" for h in head)
                + f"</tr></thead><tbody>{body}</tbody></table></div>"
                + f"<p>Noise ceiling {pct(ceil)}: re-tiered tasks show where even the truth shifts.</p>")
    return out


def _say(text):
    """T-2767: the tower's records still use the old name; the site says Sampo."""
    text = re.sub(r"\bfm\b", "sampo", str(text))
    return re.sub(r"(?i)\bforeman\b", lambda m: "Sampo" if m.group()[0] == "F" else "sampo", text)


def _sky(sky, marks):
    """T-2767: a page's scroll scene, one mark per record and at most 60, on a still CSS sky (style.css); where motion
    is allowed site.js lights the sky's shader behind it and the marks play in as it scrolls into view."""
    return f'<figure class="sky" data-sky="{sky}" aria-hidden="true"><svg viewBox="0 0 640 180">{"".join(marks)}</svg></figure>'


def _louhi(s):
    """T-2767: Louhi's scorecard (site/stats.py's louhi block from exam.jsonl): dials, one standing stone per exam run
    (its height the scenarios graded, its lit share the faults found of those keyed), each arm's record and the
    harness's own trend by generation."""
    lo = s.get("louhi") or {}
    runs, arms, gens = lo.get("runs") or [], lo.get("agents") or [], lo.get("gens") or []
    own = [a for a in arms if a["agent"] in ("foreman", "sampo")]
    out = dials([("trials", lo.get("trials") or 0), ("exam runs", len(runs)),
                 ("scenarios graded", sum(a["scenarios"] for a in arms)),
                 ("faults Sampo found", sum(a["found"] for a in own))], "From Louhi's exam record")
    shown = runs[:60]
    slot, top = 600 / max(len(shown), 1), max([r["scenarios"] for r in shown] + [1])
    marks = []
    for i, r in enumerate(shown):
        h = 20 + 130 * r["scenarios"] / top
        lit = h * min(r["found"] / r["keyed"], 1) if r["keyed"] else 0
        x, w = 20 + slot * i + slot * .2, max(slot * .6, 1)
        marks.append(f'<g class="mk trial"><rect class="stone" x="{x:.1f}" y="{160 - h:.1f}" width="{w:.1f}" height="{h:.1f}"/>'
                     f'<rect class="lit" x="{x:.1f}" y="{160 - lit:.1f}" width="{w:.1f}" height="{lit:.1f}"/></g>')
    out += _sky("aurora", marks)
    if not arms:
        return out + "<p>No exam has run yet.</p>"
    name = lambda a: "Sampo" if a["agent"] in ("foreman", "sampo") else a["agent"]  # noqa: E731
    out += pairs("Faults found of those keyed, by arm", "Each arm sat the same scenarios; the key holds the real faults.",
                 [(name(a), a["found"], a["keyed"]) for a in arms], names=("found", "keyed"))
    out += table(["arm", "trials", "scenarios", "keyed", "found", "false alarms", "fixed of fixable", "Brier"], [
        (name(a), a["trials"], a["scenarios"], a["keyed"], a["found"], a["false_alarms"], f"{a['fixed']} of {a['fixable']}",
         f"{a['brier']:.3f}" if isinstance(a.get("brier"), (int, float)) else "n/a") for a in arms])
    if gens:
        out += pairs("Sampo's own arm, generation by generation", "Each generation re-sits the exam with the harness it has.",
                     [(f"gen {g['gen']}", g["found"], g["keyed"]) for g in gens], names=("found", "keyed"))
    return out


MOON_PHASE = {"idea": .15, "kill": .5, "slice": .75, "full": 1}
MOON_STAGE = {"idea": "idea", "kill": "kill test", "slice": "slice", "full": "built", "not-yet": "not yet"}


def _moons(s):
    """T-2767: the Moons catalogue (site/stats.py's moons block from moonshot.md): dials, one moon per entry (its size
    the tier, its phase the stage: a sliver for an idea, half once kill-tested, full when built, a ring for not yet)
    and the list."""
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "plugin", "lib"))
    from sampo.moons import TIERS
    ms = s.get("moons") or []
    out = dials([("moons", len(ms)), ("ideas", sum(m["stage"] == "idea" for m in ms)),
                 ("kill-tested", sum(m["stage"] != "idea" for m in ms)), ("built", sum(m["stage"] == "full" for m in ms))],
                "From the moonshot catalogue")
    shown = ms[:60]
    cols = min(len(shown), 10) or 1
    rows = -(-len(shown) // cols)
    cw, ch = 600 / cols, 160 / max(rows, 1)
    marks = []
    for i, m in enumerate(shown):
        cx, cy = 20 + cw * (i % cols + .5), 10 + ch * (i // cols + .5)
        r = min(cw, ch) * .42 * (.5 + .1 * (TIERS.index(m["tier"]) if m["tier"] in TIERS else 0))
        p = MOON_PHASE.get(m["stage"])
        if p is None:
            marks.append(f'<g class="mk moon"><circle class="ring" cx="{cx:.1f}" cy="{cy:.1f}" r="{r:.1f}"/></g>')
            continue
        rx = r * abs(1 - 2 * p)
        lit = (f'M{cx:.1f} {cy - r:.1f}A{r:.1f} {r:.1f} 0 0 1 {cx:.1f} {cy + r:.1f}'
               f'A{rx:.1f} {r:.1f} 0 0 {int(p > .5)} {cx:.1f} {cy - r:.1f}Z')
        marks.append(f'<g class="mk moon"><circle class="disc" cx="{cx:.1f}" cy="{cy:.1f}" r="{r:.1f}"/>'
                     f'<path class="lit" d="{lit}"/></g>')
    out += _sky("stars", marks)
    if not ms:
        return out + "<p>No moon is in the catalogue yet.</p>"
    items = "".join(
        f'<li data-stage="{esc(m["stage"])}"><b>{esc(m["id"])}</b> <span>{esc(_say(m["title"]))}</span> '
        f'<small>{esc(m["tier"])} · {esc(MOON_STAGE.get(m["stage"], m["stage"]))}'
        + (f' · needs {esc(", ".join(m["needs"]))}' if m["needs"] else "") + "</small></li>" for m in ms)
    tiers = [(t, sum(m["tier"] == t for m in ms)) for t in TIERS]
    out += hbars("Moons by tier", "The reviewer's estimate of each moon's size, low to massive.", [t for t in tiers if t[1]])
    return out + f'<ol class="moons">{items}</ol>'


def _underworld(s):
    """T-2767: the Underworld (site/stats.py's underworld block from not-yet.md): dials, one rune stone per waiting
    idea (a branch on its stave for each approach tried) by the river, and the list with each idea's blocker."""
    ws = s.get("underworld") or []
    out = dials([("waiting ideas", len(ws)), ("approaches tried", sum(w["tries"] for w in ws)),
                 ("re-checks", sum(w["rechecks"] for w in ws)),
                 ("unevaluated", sum(w["blocker"].startswith("unevaluated") for w in ws))], "From the not-yet record")
    shown = ws[:60]
    slot = 600 / max(len(shown), 1)
    marks = ['<rect class="water" x="0" y="150" width="640" height="30"/>']
    for i, w in enumerate(shown):
        x = 20 + slot * (i + .5)
        stave = "".join(f'M{x:.1f} {72 + 12 * j}l{10 if j % 2 else -10} 12' for j in range(min(w["tries"], 6)))
        marks.append(f'<g class="mk rune"><rect class="stone" x="{x - 16:.1f}" y="54" width="32" height="96" rx="12"/>'
                     f'<path class="glyph" d="M{x:.1f} 64V140{stave}"/></g>')
    out += _sky("river", marks)
    if not ws:
        return out + "<p>Nothing waits in the underworld.</p>"
    items = "".join(
        f'<li><b>{esc(_say(w["idea"]))}</b> <span>{esc(_say(w["blocker"]))}</span> <small>'
        f'{w["tries"]} tried · {w["rechecks"]} re-checks' + (f' · since {esc(w["added"])}' if w.get("added") else "")
        + "</small></li>" for w in ws)
    tried = sorted({w["tries"] for w in ws})
    out += hbars("Waiting ideas by approaches tried", "How many ways each idea was tried before it waited.",
                 [(f"{n} tried", sum(w["tries"] == n for w in ws)) for n in tried])
    return out + f'<ol class="waiting">{items}</ol>'


def _types(s):
    return hbars("Closed tasks by type", None, sorted(s["types"].items(), key=lambda kv: -kv[1]))


def _evidence(s):
    return columns("Evidence runs recorded per day", "Each one is a command Sampo ran and stored with its exit code and output.",
                   s["daily"], ["evidence"])


def _guard(s):
    return hbars("Ward refusals by rule", "Every block on record, all time. Most are Sampo protecting its own code and state.",
                 sorted(s["guard"].items(), key=lambda kv: -kv[1]))


def _latency(s, keys=None):
    rows = [(k, v["p95"]) for k, v in s["bench"].items() if not keys or k in keys]
    rows.sort(key=lambda kv: -kv[1])
    return hbars("Hook time per event, p95", "Measured by bench_hooks.py; the dashed tick is each hook's budget.", rows,
                 unit=" ms", budget={k: s["bench"][k]["budget"] for k, _ in rows})


def _findings(s):
    return hbars("Review findings by lens", "Confirmed findings from independent reviewers, logged when they report.",
                 sorted(s["findings"]["lenses"].items(), key=lambda kv: -kv[1]))


def _severity(s):
    order = ["HIGH", "MEDIUM", "LOW"]
    return hbars("Findings by severity", None, [(k.title(), s["findings"]["severity"].get(k, 0)) for k in order], series=2)


def _agents(s):
    return hbars("Subagents launched, by kind", "Builders work in worktrees, reviewers read diffs, recon answers questions.",
                 sorted(s["subagents"].items(), key=lambda kv: -kv[1]))


def _lanes(s):
    return columns("Builder lanes merged per day", "Each lane is one task built in its own git worktree, reviewed, then merged.",
                   s["daily"], ["lanes"])


def _cards(s):
    return hbars("Stack cards per area", f"{sum(s['cards'].values()):,} cards in {len(s['cards'])} areas, read only when needed.",
                 list(s["cards"].items()))


def _wall(s):
    """T-1541: the release-over-release wall: a generated cover per release, its hue from its version, its bar the
    share of the latest release's tests it had."""
    rel = s.get("releases") or []
    most = max((r["tests"] for r in rel), default=1) or 1
    covers = []
    for r in rel:
        parts = [int(x) if x.isdigit() else 0 for x in str(r["version"]).split(".")[:3]] + [0, 0, 0]
        hue = (parts[0] * 97 + parts[1] * 31 + parts[2] * 7) % 360
        covers.append(
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 150 100" role="img" aria-label="Sampo '
            f'{esc(r["version"])}, {esc(r["day"])}: {r["tests"]} tests"><rect width="150" height="100" '
            f'fill="hsl({hue} 38% 20%)"/><text x="12" y="44" font-size="26" font-weight="700" fill="hsl({hue} 70% 82%)"'
            f' font-family="system-ui,sans-serif">{esc(r["version"])}</text><text x="12" y="64" font-size="11" '
            f'fill="#e8e2d6" font-family="ui-monospace,monospace">{esc(r["day"])} · {r["tests"]:,} tests</text>'
            f'<rect x="12" y="78" width="126" height="6" rx="3" fill="hsl({hue} 30% 32%)"/><rect x="12" y="78" '
            f'width="{round(126 * r["tests"] / most, 1)}" height="6" rx="3" fill="hsl({hue} 70% 65%)"/></svg>')
    return (f'<section><h2>Release over release</h2><p>Each release\'s cover is drawn from its own numbers: the hue from '
            f'its version, the bar its tests as a share of the latest.</p><div class="wall">{"".join(covers)}</div>'
            f'</section>') if covers else ""


def _tests(s):
    return line("Tests in the suite, release by release", "Counted at each release commit; every release passes all of them.",
                s["releases"], "tests", "version")


def _totals(s, keys):
    t = s["totals"]
    names = {"tasks_done": "Tasks closed", "evidence": "Evidence runs", "proofs": "Red-to-green proofs",
             "commits": "Commits", "gate_runs": "Gate runs", "subagents": "Subagents", "lanes_merged": "Lanes merged",
             "findings": "Review findings", "guard_blocks": "Ward blocks", "captures": "Ideas captured",
             "lessons_shown": "Lessons recalled"}
    return tiles([(names[k], t[k]) for k in keys])


def _safety(s):
    x = s.get("safety")
    if not x:
        return "<p>The safety benchmark hasn't been run on this build.</p>"
    n, m = len(x["dangerous"]), len(x["everyday"])
    return tiles([("Dangerous commands stopped", f"{x['stopped']} of {n}"), ("Allowed after saving your work", x["saved"]),
                  ("Everyday commands stopped", f"{x['false_stops']} of {m}")]) + verdicts(x["dangerous"])


def _duel(s, name):
    d = next((x for x in s.get("duels") or [] if x.get("name") == name), None)
    if not d or not d.get("cases"):
        return "<p><strong>Not run yet.</strong> Its results replace this line when the run finishes.</p>"
    cs = d["cases"]
    won, lost = sum(c["foreman"]["pass"] for c in cs), sum(c["bare"]["pass"] for c in cs)
    cost_f, cost_b = sum(c["foreman"]["cost"] for c in cs), sum(c["bare"]["cost"] for c in cs)
    time_f, time_b = sum(c["foreman"]["seconds"] for c in cs), sum(c["bare"]["seconds"] for c in cs)
    pct = lambda a, b: round((a / b - 1) * 100) if b else 0
    n = len(cs)
    head = tiles([("Tasks passed, with Sampo", f"{won} of {n}"), (f"Cost of the {n}, with Sampo", f"${cost_f:.2f}"),
                  (f"Time for the {n}, with Sampo", f"{time_f:.0f} s"), ("Tasks passed, Claude Code", f"{lost} of {n}"),
                  (f"Cost of the {n}, Claude Code", f"${cost_b:.2f}"), (f"Time for the {n}, Claude Code", f"{time_b:.0f} s")],
                 "three")
    if won > lost:
        verdict = f"Sampo passed {won - lost} more of the {len(cs)} tasks."
    elif won == lost:
        verdict = f"Both passed {won} of the {len(cs)} tasks."
    else:
        verdict = f"Sampo passed {lost - won} fewer of the {len(cs)} tasks."
    for what, a, b in (("cost", cost_f, cost_b), ("time", time_f, time_b)):
        p = pct(a, b)
        verdict += (f" Sampo's {what} was {abs(p)}% {'lower' if p < 0 else 'higher'}." if abs(p) >= 3 else
                    f" Its {what} was within 3% of Claude Code's.<!-- source: site/stats.json -->")

    def cell(x):
        mark = "✓" if x["pass"] else "✗"
        if x.get("score") is not None:
            return f"{mark} judge {x['score']} of 5"
        return f"{mark} {x['passed']} of {x['runs']} runs" if x.get("runs", 1) > 1 else f"{mark} hidden tests"
    rows = "".join(f"<tr><td>{esc(c['case'])}</td><td>{cell(c['foreman'])}</td><td>{cell(c['bare'])}</td></tr>"
                   for c in cs)
    table = ('<div class="verdicts"><table><thead><tr><th>Task</th><th>With Sampo</th><th>Claude Code</th></tr>'
             f'</thead><tbody>{rows}</tbody></table></div>')
    return (head + f"<p><strong>{verdict}</strong></p>" + _ran_on(d) + table
            + pairs("Cost per task", "USD, as Claude Code reports it.", [(c["case"], c["foreman"]["cost"], c["bare"]["cost"]) for c in cs], unit="")
            + pairs("Time per task", "Seconds from the first prompt to the session ending.",
                    [(c["case"], round(c["foreman"]["seconds"]), round(c["bare"]["seconds"])) for c in cs], unit=" s"))


LOSS = 1.25  # T-1261: Sampo loses a row when Claude Code passes and it doesn't, or it costs or takes 25% more


def _loses(s):
    """T-1261: every duel row where Sampo did worse than Claude Code, generated from the same results as the charts,
    each with the command that reruns it on both sides (fm bench repro LABEL CASE)."""
    rows = []
    for d in s.get("duels") or []:
        for c in d.get("cases") or []:
            f, b = c.get("foreman") or {}, c.get("bare") or {}
            why = ["Claude Code passed, Sampo didn't"] if b.get("pass") and not f.get("pass") else []
            for what, k in (("cost", "cost"), ("time", "seconds")):
                if (b.get(k) or 0) > 0 and (f.get(k) or 0) > LOSS * b[k]:
                    why.append(f"{what} {round((f[k] / b[k] - 1) * 100)}% higher")
            if why:
                cmd = f"sampo bench repro {d['label']} {c['id']}" if d.get("label") and c.get("id") else ""
                rows.append(f"<tr><td>{esc(d.get('name') or '')}</td><td>{esc(c.get('case') or '')}</td>"
                            f"<td>{esc('; '.join(why))}</td><td>"
                            + (f"<code>{esc(cmd)}</code>" if cmd else "not recorded with this run") + "</td></tr>")
    if not rows:
        return ("<p><strong>Sampo lost no row in these runs</strong>: no task Claude Code passed and Sampo failed, "
                "and none cost or took 25% more.<!-- source: site/stats.json --></p>")
    return (f"<p><strong>{len(rows)} row(s) where Sampo did worse.</strong> Each command reruns that row on both "
            "sides with its run's model and settings.</p><div class=\"verdicts\"><table><thead><tr><th>Run</th>"
            "<th>Task</th><th>Where it lost</th><th>Rerun it</th></tr></thead><tbody>" + "".join(rows)
            + "</tbody></table></div>")


def _why(s):
    """T-1246: the Benchmarks page's opening argument, every figure from the data the charts below it draw."""
    safety = s.get("safety") or {}
    bad, ok = safety.get("dangerous") or [], safety.get("everyday") or []
    stopped, saved = (sum(x.get("decision") == k for x in bad) for k in ("deny", "saved"))
    false = sum(x.get("decision") != "allow" for x in ok)
    duel = lambda name: next((d for d in s.get("duels") or [] if d.get("name") == name), None)
    pct = lambda a, b: round((a / b - 1) * 100) if b else 0
    more = lambda p: f"{abs(p)}% {'more' if p >= 0 else 'less'}"

    def gap(name):  # (cost %, time %) of Sampo against Claude Code over a duel's cases
        d = duel(name)
        cs = (d or {}).get("cases") or []
        sums = [sum(c[arm][k] for c in cs) for k in ("cost", "seconds") for arm in ("foreman", "bare")]
        return (pct(sums[0], sums[1]), pct(sums[2], sums[3])) if cs else None
    out = [f"<p><strong>It stops what you'd regret.</strong> In bypass mode, out of {len(bad)} dangerous commands "
           "(deleting a home folder, force-pushing, reading SSH keys, publishing a package) Sampo stopped "
           f"{stopped}" + (f" and saved a copy before the other {saved}" if saved else "") + f". Claude Code on its "
           f"own ran all {len(bad)}. Sampo stopped {'none' if not false else false} of {len(ok)} everyday "
           "commands.</p>",
           "<p><strong>It leaves a record.</strong> Every task has a plan, the checks that ran and their output, and a "
           "commit you can revert.</p>"]
    q, h = gap("quality"), gap("hard-haiku")
    if q or h:
        cost = " and ".join(f"{more(g[0])} with {m}" for g, m in ((q, "Sonnet"), (h, "Haiku")) if g)
        time_ = " and ".join(("about the same time" if abs(g[1]) < 5 else f"{more(g[1])} time") + f" with {m}"
                             for g, m in ((q, "Sonnet"), (h, "Haiku")) if g)
        out.append("<p><strong>What the quality runs show.</strong> On small tasks where the request spells out every "
                   f"rule, both sides pass everything. There Sampo costs {cost}, and takes {time_}, so for a short, "
                   "fully specified task you're watching, Claude Code alone is cheaper.</p>")
    hist = (duel("long") or {}).get("history") or []
    if hist:
        fp, bp, runs = (sum(x[k] for x in hist) for k in ("f_pass", "b_pass", "runs"))
        fc, bc = (sum(x[k] * x["runs"] for x in hist) / runs for k in ("f_cost", "b_cost"))
        out.append("<p><strong>It finishes long work.</strong> Asked for eight requests in one Haiku session whose "
                   f"context compacts as it goes, Sampo passed every hidden test in {fp} of {runs} runs over "
                   f"{len(hist)} duel{'s' if len(hist) > 1 else ''}. Claude Code on its own did in {bp} of {runs}; in the "
                   "first duel, the runs that fell short wrote the modules they got wrong after their first compaction, "
                   "and the one that passed wrote them before it. Sampo keeps each request's text and gives it back "
                   f"after every compaction. It cost about {more(pct(fc, bc))} there (${fc:.3f} against ${bc:.3f} a "
                   "session) and took longer. " + _per_correct(fc * runs, fp, bc * runs, bp, pct, more) + "</p>")
    m = (duel("marathon") or {}).get("cases") or []
    if m:
        f = m[0]["foreman"]
        comp = ([e.get("compactions") for e in f.get("each") or []] or [f.get("compactions")])[0]
        t = ([e.get("tests") for e in f.get("each") or []] or [f.get("tests")])[0] or [0, 1]
        out.append(f"<p><strong>It keeps going.</strong> In a marathon, once the asked work was done, Sampo went on "
                   f"through {comp} compactions, found and fixed {len(f.get('found') or [])} things nobody asked for, "
                   + ("and still passed every hidden test.</p>" if t[0] == t[1] else
                      f"and passed {t[0]} of {t[1]} hidden tests.</p>"))
    return "".join(out)


def _ran_on(d):
    """T-1255: the Claude Code a duel ran on, so results from different releases aren't compared unawares."""
    v = d.get("claude")
    return (f'<p class="src">Ran on Claude Code {esc(v)}' if v else
            '<p class="src">Its Claude Code version wasn\'t recorded (a run from before results named it)') + (
        f", {esc(d['at'])}." if d.get("at") else ".") + "</p>"


def _per_correct(f_total, f_pass, b_total, b_pass, pct, more):
    """T-1254: what one session that passed every hidden test cost on each side: all the runs' cost over the runs
    that passed, since a session that falls short has to be run again."""
    if not f_pass:
        return "With Sampo no run passed every test."
    f = f_total / f_pass
    if not b_pass:
        return (f"Claude Code alone never passed every test, so its sessions bought no fully correct result; each "
                f"one with Sampo cost ${f:.3f}.")
    b = b_total / b_pass
    return (f"Counted per session that passed every test, which is what you'd pay to get one, Sampo cost ${f:.3f} "
            f"against ${b:.3f}: {more(pct(f, b))} per fully correct session.")


def _long(s, name):
    """T-1242: one multi-request session per side, compacting as it goes: the hidden tests passing at the end, the
    compactions, cost and time; for a marathon, the work Sampo found and did after the asked work."""
    d = next((x for x in s.get("duels") or [] if x.get("name") == name), None)
    if not d or not d.get("cases"):
        return "<p><strong>Not run yet.</strong> Its results replace this line when the run finishes.</p>"
    f, b = d["cases"][0]["foreman"], d["cases"][0]["bare"]
    def each(x, k):  # every run's value: "105, 105, 87"
        vals = [a.get(k) for a in x.get("each") or [] if a.get(k) is not None] or [x.get(k)]
        return [v for v in vals if v is not None]

    def tests(x):
        runs = each(x, "tests")
        if not runs:
            return "all" if x["pass"] else "failed"
        if len(runs) > 1 and len({tuple(t) for t in runs}) == 1:
            return f"{runs[0][0]} of {runs[0][1]} in all {len(runs)}"
        return ", ".join(str(t[0]) for t in runs) + f" of {runs[0][1]}"
    many = len(f.get("each") or []) > 1
    per = " (each run)" if many else ""
    comp = lambda x: ", ".join(str(v) for v in each(x, "compactions")) or "0"
    head = tiles([(f"Hidden tests passing{per}, with Sampo", tests(f)), (f"Compactions{per}, with Sampo", comp(f)),
                  ("Mean cost, with Sampo" if many else "Cost, with Sampo", f"${f['cost']:.2f}"),
                  (f"Hidden tests passing{per}, Claude Code", tests(b)), (f"Compactions{per}, Claude Code", comp(b)),
                  ("Mean cost, Claude Code" if many else "Cost, Claude Code", f"${b['cost']:.2f}")], "three")
    said = (f"Sampo's session took {f['seconds'] / 60:.0f} min and Claude Code's {b['seconds'] / 60:.0f} min"
            + (" on average." if many else ".")
            + (" Sampo's ran until its dollar cap, as a marathon does." if f.get("capped") else ""))
    def mark(x):
        return "✓ all pass" if not x["failed"] else f"✗ {x['failed']} failing"
    short = lambda t: " ".join(re.sub(r"[:(;].*", "", t).split()[:6]).rstrip(",.") or t  # as the duel tables do
    reqs = list(zip(f.get("requests") or [], b.get("requests") or []))
    table = ('<div class="verdicts"><table><thead><tr><th>Request</th><th>With Sampo</th><th>Claude Code</th></tr>'
             "</thead><tbody>" + "".join(f"<tr><td>{esc(short(x['title']))}</td><td>{mark(x)}</td><td>{mark(z)}</td></tr>"
                                         for x, z in reqs) + "</tbody></table></div>") if reqs else ""
    found = f.get("found") or []
    work = ("<p>What Sampo found and did on its own after the asked work:</p><ul>"
            + "".join(f"<li>{esc(x.rsplit(' (discovered)', 1)[0])}</li>" for x in found) + "</ul>") if found else (
        "<p>Sampo found nothing more worth doing before its cap.</p>" if d.get("marathon") else "")
    return head + f"<p><strong>{said}</strong></p>" + _ran_on(d) + table + work


FLOW_CODE = """<div class="flow"><svg viewBox="0 0 760 210" role="img" aria-label="sampo code: the harness runs every station; the model fills one slot at a time">
<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0L10 5L0 10z" fill="currentColor"/></marker></defs>
<rect class="box" x="10" y="20" width="130" height="64" rx="10"/><text class="t" x="24" y="46">Your request</text><text class="s" x="24" y="66">one line is enough</text>
<rect class="box hot" x="170" y="20" width="150" height="64" rx="10"/><text class="t" x="184" y="46">Plan slot</text><text class="s" x="184" y="66">type, size, checks, steps</text>
<rect class="box hot" x="350" y="20" width="150" height="64" rx="10"/><text class="t" x="364" y="46">Action slot</text><text class="s" x="364" y="66">read, search, edit, run</text>
<rect class="box" x="530" y="20" width="210" height="64" rx="10"/><text class="t" x="544" y="46">Sampo's Ward</text><text class="s" x="544" y="66">refusals go back to the model</text>
<rect class="box" x="350" y="126" width="150" height="64" rx="10"/><text class="t" x="364" y="152">Harness verifies</text><text class="s" x="364" y="172">runs the checks itself</text>
<rect class="box" x="530" y="126" width="210" height="64" rx="10"/><text class="t" x="544" y="152">Task closed</text><text class="s" x="544" y="172">evidence, audit, commit</text>
<path class="arrow" d="M140 52H166"/><path class="arrow" d="M320 52H346"/><path class="arrow" d="M500 52H526"/>
<path class="arrow" d="M425 84V122"/><path class="arrow" d="M500 158H526"/>
</svg></div>"""


def themed(name, alt, w, h, ext="svg"):
    """T-3996: one of the README's pictures, as drawn for each theme (build.py copies docs/assets into assets/docs); the
    page's own light or dark choice shows one."""
    src = "../assets/docs/" + name
    return "".join(f'<img class="dia-{t}" src="{src}-{t}.{ext}" width="{w}" height="{h}" alt="{esc(alt)}" loading="lazy">'
                   for t in ("light", "dark"))


PAGES = [
    {"slug": "planning", "nav": "Planning", "title": "Every request becomes a plan first",
     "desc": "Intake turns a request into a brief with a type, a size, criteria and the commands that prove them.",
     "lede": "A plain request is enough. Sampo classifies it, writes a brief with what done means and the command "
             "that proves it, and only then lets an edit happen.",
     "blocks": [
         ("text", "What a brief holds", "<ul><li><strong>Type and size.</strong> FIX, FEATURE, CLEAN, PERFORMANCE, "
          "SECURITY or RESEARCH, and S, M or L.</li><li><strong>Acceptance criteria</strong>, each with the command "
          "that proves it.</li><li><strong>Steps, scope and risks.</strong> M and L work lists its approaches first "
          "and says why it chose one.</li><li><strong>An approval</strong> when the work is large, unless you put "
          "Sampo in full auto.</li></ul>"),
         ("term", "FIX: login times out after 30s on slow networks\n"
                  "FEATURE: export report as CSV @src/reports\n"
                  "CONTEXT: Django app; don't touch migrations\n"
                  "DONE-WHEN: all tests pass and the CSV opens in Excel"),
         ("text", "One task at a time", "<p>One task is active. New ideas land in the inbox instead of being chased "
          "halfway, and the queue orders itself: baseline, cleanups, performance, security, fixes, then features, "
          "dependencies first. <code>NOW:</code> switches on purpose.</p>"),
         ("text", "Goal to finish", "<p>A big goal is one record, not a hope. <code>sampo goal set</code> keeps its words and "
          "an end date; <code>sampo goal item</code> adds each thing it must contain with a done-when check; "
          "<code>sampo goal link</code> (or <code>sampo task new --goal N</code>) ties a task to the item it builds; "
          "<code>sampo goal show</code> lists drift: work outside the goal, items with no task, items claimed done whose "
          "check fails. <code>sampo goal verdict</code> says <em>finished</em> only when every item is done, every check "
          "passes now and the gates are green; otherwise <em>stopped</em>, with each cause. A stopped goal blocks a "
          "done claim once, shows in the band, and the MCP <code>goal</code> tool and AGENTS.md carry it.</p>"
          "<p>It carries across days: one line, <code>Goal: &quot;…&quot; · 2/5 items done · next: 3. …</code>, opens "
          "every session, survives compaction, and rides in <code>sampo code</code>'s run prompt, so the plan outlives "
          "the conversation.</p>"),
         ("chart", _sizes), ("chart", _types)]},
    {"slug": "judgment", "nav": "Judgment", "title": "It says how sure it is, and is scored on it",  # T-2344
     "desc": "Every M plan names its readings, its options and how each fails, and a choice with a confidence that "
             "outcomes score.",
     "lede": "A plan that can't be wrong can't be checked. Sampo's plans say what else the request could mean, what "
             "else could be built and how each would fail, and bet a number on the pick; the outcomes keep score.",
     "blocks": [
         ("text", "The plan shape", "<ul><li><strong>Readings.</strong> Two ways to read the request (Reading 1, "
          "Reading 2), or the one it holds and the one it ruled out.</li><li><strong>Options.</strong> Three or more, "
          "each with how it fails if it's wrong (<em>fails if: …</em>): one that uses what exists, one that does "
          "less.</li><li><strong>A choice with a number.</strong> <code>Choice N (NN%)</code>: the percent it would "
          "bet the pick holds. Under the race bar, a cold rival reads the request without the plan and its "
          "dissent is answered before work starts.</li></ul><p><code>sampo focus</code> refuses a plan without them, "
          "and <code>sampo code</code>'s planner on any model writes the same shape and meets the same gate.</p>"),
         ("text", "Races and doors", "<p>When the bet is low, <code>sampo lane race</code> builds two options side by "
          "side and the evidence picks; the record keeps how often the first choice lost. A step that is one-way "
          "(it deletes untracked data) and ambiguous parks for you instead of guessing; a two-way step just goes.</p>"),
         ("text", "The score", "<p><code>sampo outcomes --judgment</code> buckets every stated number against what happened and "
          "puts its Brier score beside the base rate's. If the stated numbers score worse, the base rate routes in "
          "their place until they don't, and a bar the outcomes say is too low is raised. The MCP "
          "<code>calibration</code> tool (Claude Desktop, other agents) reads the same record without changing it, "
          "and the band shows one line only when a door is parked or the numbers lose to the base rate.</p>"),
         ("chart", _judgment)]},
    {"slug": "lacks", "nav": "Getting tools", "title": "It gets the tools it lacks, and asks before it installs",  # T-2378
     "desc": "A missing tool is noticed, found on a shelf or built and proved, and an install waits for your yes.",
     "lede": "When the work needs something that isn't there, Sampo says so on the spot, looks on every shelf it has, "
             "builds what's missing with a test, and queues an install for you instead of granting it to itself.",
     "blocks": [
         ("text", "Noticed where it happens", "<p>A command that fails because a tool is missing gets a note right "
          "there: what's already here, <code>sampo which</code> to look on every shelf, or <code>sampo new tool</code> to "
          "build it. The miss belongs to the task: it can't finish until a run gets past it. The same note reaches "
          "<code>sampo code</code> on any model and Codex, Gemini CLI and opencode, whose after-tool events go through "
          "the same check.</p>"),
         ("text", "Found, built, proved", "<p><code>sampo which</code> searches past fixes, the project's own tools, MCP "
          "servers, binaries, skills, playbooks, sampo commands, stack cards and plugins, best first. A tool Sampo "
          "builds counts only once runs prove it.</p>"),
         ("text", "Installs wait for you", "<p>An install is never self-granted. <code>sampo ask ID plugin --later</code> "
          "queues it with the miss behind it, what was searched and what the work does meanwhile; it never expires, "
          "and a no is remembered. The band shows the queue while it waits, and the MCP <code>which</code> and "
          "<code>grants</code> tools read the same record from Claude Desktop or another agent, changing nothing.</p>"),
         ("chart", _lacks)]},
    {"slug": "proof", "nav": "Proof", "title": "Nothing is done until a command proves it",
     "desc": "Evidence is a command Sampo ran and recorded; a fix needs a test that failed first.",
     "lede": "Saying the tests pass is not evidence. Sampo records the real command, its exit code and its output, "
             "and refuses to close a task without it.",
     "blocks": [
         ("chart", lambda s: _totals(s, ["evidence", "proofs", "gate_runs", "tasks_done"])),
         ("text", "How a task closes", "<ul><li><strong>Evidence per step.</strong> <code>sampo task evidence --run</code> "
          "runs the check and stores what happened.</li><li><strong>Red, then green.</strong> <code>sampo task prove</code> "
          "runs the test on the starting tree and on the changed one, and shows it fails without the change.</li>"
          "<li><strong>Gates.</strong> <code>sampo check</code> runs the project's gates and reuses a pass on an "
          "identical tree.</li><li><strong>One call to close.</strong> <code>sampo task finish</code> checks the evidence, "
          "the audits, the docs note and the lesson the task's size needs, then commits its own files.</li></ul>"),
         ("chart", _evidence),
         ("term", "T-0042  FIX small  login times out on slow wifi\n"
                  "✓ step 1  $ pytest tests/test_login.py -k slow   → ✗ exit 1  (fails for the right reason)\n"
                  "✓ step 2  $ pytest tests/test_login.py           → exit 0 · 14 passed\n"
                  "✓ proved  fails without the change, passes with it\n"
                  "✓ audits  self, adversary")]},
    {"slug": "verifying", "nav": "What isn't proved", "title": "It says what no run proved",  # T-2395
     "desc": "Every done task whose claim no command here proved is listed by kind, with the command that closes it.",
     "lede": "A green close can still rest on words. Sampo reads each done task for claims no run backed, lists them "
             "by kind, and shows the count wherever you look.",
     "blocks": [
         ("text", "The kinds", "<p>Each kind is a claim with its own proof: <code>typed</code> evidence (pasted, not "
          "run), <code>red</code> or <code>inconclusive</code> runs, <code>ui-unshot</code> (a UI edit with no "
          "screenshot), <code>tui-unshot</code> (a terminal UI edit no recorded run renders), <code>docs</code>, <code>infra</code>, <code>migration</code>, <code>data</code>, "
          "<code>perf-typed</code> and <code>perf-noise</code> (a speed claim with no bench, or one inside the noise), "
          "<code>research-uncited</code>, <code>hollow</code> (a test that can't fail), <code>claimed</code> (a proof "
          "word said with no run behind it) and <code>meaning</code> (a clause of the request no criterion checks)."
          "</p>"),
         ("text", "Where it shows", "<p><code>sampo prove --rollup</code> lists each unproven task with the command that closes "
          "it. The close itself prints the Not verified lines, in <code>sampo task finish</code> and in "
          "<code>sampo code</code> on any model; commits and pull requests carry an Unproven trailer; the pane's band "
          "shows the count and the kind most need; the MCP <code>proof</code> tool gives Claude Desktop and other "
          "agents the same rows; the Louhi exam's proof and beyond kinds count a false proof against the run.</p>"),
         ("chart", _proof)]},
    {"slug": "self-running", "nav": "Self-running", "title": "Work starts by itself, and it is scored",  # T-2364
     "desc": "Triggers, installs waiting for your yes, lanes that recover, self-fixes on trial and predictions, each "
             "measured against a baseline.",
     "lede": "Sampo starts work nobody asked for: a red build, a new issue, a file that changed, its own repeated "
             "failures. Each source is scored, each recovery logged, each guess held to a naive baseline.",
     "blocks": [
         ("text", "Triggers", "<p>Every source that files tasks is a row in <code>sampo triggers</code>: how often it "
          "fired, its precision (the share of its tasks that got done) and its route: the inbox, the queue, a builder "
          "lane, or muted. <code>sampo night --tick</code> is the model-free pass that runs them: standing orders, the "
          "cached CI result, and the CI check or a repo's issues only where you said yes. What it can't watch, and "
          "why (no yes, a sensitive project, no network), shows in <code>sampo doctor</code>, so a quiet night means "
          "quiet.</p>"),
         ("text", "Installs and recoveries", "<p>An install the work needs waits for your yes, and the timers that "
          "run the tick install only after it. A builder lane that stops (killed, at the usage wall, hung, clashing "
          "with main, red at merge) is recovered by its cause with <code>sampo lane recover</code>, at most twice before "
          "a person looks.</p>"),
         ("text", "Self-fixes and predictions", "<p>A fix Sampo makes to its own repeated failure is a trial, "
          "graded on whether the failure stayed gone. Its guesses at your next request and at effort are scored in "
          "<code>sampo outcomes --predictions</code> against a naive baseline; one that trails the baseline over 20 tries "
          "is muted.</p>"),
         ("text", "Staying lean", "<p>Each generation records its footprint when it starts; the lean ratchet fails "
          "a generation that ends bigger than it began, and the self-change gate refuses a merge that grows the hooks, "
          "the context or the verbs with nothing cut to pay for it.</p>"),
         ("chart", _self_running)]},
    {"slug": "model-layer", "nav": "Model layer", "title": "Models advise; proof decides",  # T-2489
     "desc": "A rule first, then local models, then cloud: each use runs in shadow until Louhi, the exam, shows the model is right more often than the rule.",
     "lede": "Some calls (a request's type and size, whether two items are the same, whether a lead is real) can go to a "
             "model. Sampo asks the cheapest that can answer, books every answer, and lets one take over only when "
             "its record says so.",
     "blocks": [
         ("text", "The order it asks in", "<ul><li><strong>The rule first.</strong> The deterministic path answers when "
          "it is sure.</li><li><strong>Local models next</strong>, on this machine; a <strong>sensitive</strong> "
          "project never goes further.</li><li><strong>Cloud last</strong>: a cloud model only when no local answer clears the "
          "bar.</li></ul><p>Every answer is booked with its probability, so each use has a Brier score against what "
          "really happened. No gate, and not the Ward, ever asks a model: proof comes from a run, never an opinion. A model "
          "download or install waits for your yes like any other install.</p>"),
         ("text", "Shadow, then live", "<p>Each use starts in <strong>shadow</strong> mode: the rule decides and "
          "its call is logged; the night tick asks local models the same questions. Louhi, the exam, replays both against "
          "the tasks' real outcomes at zero tokens. A use goes live only by a <code>model-live</code> decision row "
          "citing the Louhi run that showed it beating the rule on accuracy, cost or latency. Other agents ask the "
          "same layer through the <code>decide</code> MCP tool; their questions are booked apart from Sampo's own, "
          "and <code>sampo doctor</code> lists the backends it can reach.</p>"),
         ("chart", _models)]},
    {"slug": "decisions", "nav": "Decisions", "title": "Every decision on one tape",  # T-2348
     "desc": "Each sampo decide row with its confidence, whether it was a door, and the later row that reversed it.",
     "lede": "Sampo decides on its own under full autonomy, so every choice is written down: what was chosen, why, "
             "how sure it was, and what happened to it later.",
     "blocks": [
         ("text", "What a row holds", "<p><code>sampo decide</code> writes each choice to the project's decisions file: "
          "its kind (reversible, or a door: costly or outward, which the final report lists), the confidence stated, "
          "the user steer it rests on and the check that proves a standing rule still holds. A later row that "
          "reverses or revisits it is linked here, so a choice that didn't last shows as one.</p>"),
         ("chart", _decisions)]},
    {"slug": "kalevala", "nav": "Kalevala", "title": "It improves itself, one tower per aspect",  # T-3996
     "desc": "One Rune tower per aspect climbs generation after generation; a rewrite stays only if it beats its parent "
             "on Louhi's exam.",
     "lede": "Sampo is built by Sampo, and so is the way it builds. Kalevala runs one Rune tower per aspect, each "
             "rewriting its own seed, missions and lenses, and what one tower learns every tower reads.",
     "blocks": [
         ("raw", '<figure class="dia forged">' + themed("kalevala", "Kalevala: a tower per aspect, each from a sealed "
                 "gen-0, sharing moons, lessons and steers; each generation seeds up to eight missions, builds them as "
                 "tasks in lanes, is scored on Louhi's exam and kept only if it beats its parent", 1098, 780)
          + "<figcaption><b>A generation</b><span>Seed, missions, lanes, exam, select: kept only if it closes more "
          "scenarios than its parent.</span></figcaption></figure>"),
         ("text", "A tower", "<ul><li><strong>gen-0 is yours</strong>: your goals, MUST and NEVER lines, sealed by "
          "its hash; every later seed is checked against it word for word.</li><li><strong>Each generation</strong> "
          "plans up to eight Missions ranked by expected value, builds them as tasks in lanes, then a cold reviewer on "
          "a stronger model reads only its diff, exam lines and outcomes.</li><li><strong>Kept or reverted</strong>: a "
          "rewrite stays only if it closes more exam scenarios than its parent; three reverts in a row stop the tower "
          "and hand it back to you.</li></ul>"),
         ("text", "Many towers", "<p>Kalevala runs a tower for each area of the coverage map with a gap, plus problem "
          "solving, memory and efficiency. Moons, exam scenarios and your steers are shared, so a gain in one tower "
          "reaches every other. Each round first restarts a tower that stopped climbing and pauses one that drifted."
          "</p>"),
         ("term", "$ sampo kalevala status\n$ sampo kalevala step\n$ sampo rune step\n$ sampo kalevala watch"),
         ("chart", _louhi)]},
    {"slug": "louhi", "nav": "Louhi", "title": "Louhi sets the trials, and keeps the score",  # T-2767
     "desc": "Every exam run against the scenarios' keyed faults: what each arm found, what it raised in error, how well "
             "its confidence matched, and the harness's own record by generation.",
     "lede": "Louhi is the exam. Each run sets the same scenarios before Sampo and the plain arms, each scenario keyed "
             "with the faults it really holds, and scores what every arm found, missed and raised in error.",
     "blocks": [
         ("chart", _louhi),
         ("text", "How a trial is scored", "<p>A trial is one arm on one run of the exam. Its findings are matched "
          "against the key: a fault on the key counts as found, one off it is a false alarm, and a fix counts only when "
          "the scenario's own check passes after it. Each arm states how sure it is, so its <strong>Brier</strong> "
          "score shows whether that confidence was earned (0 is perfect, 0.25 a coin toss). The numbers above come "
          "from Louhi's own record, not from this page.</p>"),
         ("term", "$ sampo louhi")]},
    {"slug": "moons", "nav": "Moons", "title": "Moons: the big ideas, each tested before it is built",  # T-2767
     "desc": "The moonshot catalogue: each idea with the reviewer's tier and how far it has come, from idea to kill "
             "test to built.",
     "lede": "A moon is an idea too big for one task. Each is written down with how big it is, then tested for the "
             "one thing that would kill it before any of it is built.",
     "blocks": [
         ("chart", _moons),
         ("text", "From idea to built", "<p>A moon starts as an <strong>idea</strong>. Its <strong>kill test</strong> "
          "asks the cheapest question that could end it; one that survives gets a <strong>slice</strong>, the "
          "smallest piece that proves the rest, and only then the <strong>full</strong> build. One that can't be "
          "tested yet waits as <strong>not yet</strong>. The tier is the reviewer's estimate of its size, from low "
          "to massive, and a moon that needs another waits for it.</p>"),
         ("term", "$ sampo moons")]},
    {"slug": "underworld", "nav": "Underworld", "title": "The Underworld keeps what isn't ready yet",  # T-2767
     "desc": "Ideas that were tried and didn't hold, each with what blocks it, how often it was tried and when it "
             "was last re-checked.",
     "lede": "An idea that fails isn't thrown away. It goes to the Underworld with the reason it failed, and is "
             "checked again when what blocked it may have changed.",
     "blocks": [
         ("chart", _underworld),
         ("text", "Why keep them", "<p>Each waiting idea carries its <strong>blocker</strong>, the approaches "
          "already tried and the <strong>re-checks</strong> since. A new request that matches one gets its history "
          "first, so the same dead end isn't walked twice; one still <strong>unevaluated</strong> is owed a fair "
          "test before it is called dead.</p>"),
         ("term", "$ sampo underworld")]},
    {"slug": "guard", "nav": "The Ward", "title": "A Ward built for bypass mode",
     "desc": "Every command and edit is read before it runs; dangerous ones are refused and destructive git saves your work first.",
     "lede": "Sampo is meant to run without permission prompts, so the brake has to be real: it reads every Bash "
             "command and file edit before it runs, including the scripts a command calls.",
     "blocks": [
         ("text", "What it refuses", "<ul><li><strong>Deletes outside the project</strong>, the home folder, the root.</li>"
          "<li><strong>Downloads piped into a shell</strong>, force pushes to the default branch, credential reads.</li>"
          "<li><strong>Edits to Sampo itself and its state</strong>, unless you say yes for that task.</li>"
          "<li><strong>Hidden ways to run code:</strong> <code>tar -I</code>, <code>find -exec</code>, "
          "<code>git -c core.*</code>, <code>BASH_ENV</code>, and scripts it reads line by line.</li></ul>"),
         ("chart", _guard),
         ("text", "It fails closed, and saves your work", "<p>If the Ward crashes or times out, the call is blocked. "
          "Before <code>git reset --hard</code>, <code>checkout -- .</code> or <code>clean -f</code>, Sampo snapshots "
          "your uncommitted work; <code>sampo oops --apply</code> brings it back. Every change to the Ward is replayed "
          "against thousands of real commands to show what it would newly block or allow.</p>"),
         ("chart", lambda s: _latency(s, {"PreToolUse:Bash", "PreToolUse:Bash:block", "PreToolUse:Bash:script",
                                          "PreToolUse:Edit"}))]},
    {"slug": "permissions", "nav": "Permissions", "title": "You decide what the agent may never grant itself",
     "desc": "Grants, trust mode, sensitive repos and a daily risk budget keep the dangerous switches in your hands.",
     "lede": "Some things only you can allow: editing Sampo, pushing, starting a server, installing plugins. The agent "
             "asks once through its host's own prompt, and your yes covers exactly that.",
     "blocks": [
         ("text", "The switches", "<ul><li><strong>Grants per task.</strong> <code>core</code> (Sampo's own code), "
          "<code>publish</code> (a push or release), <code>remote</code> (serving a repo), <code>plugin</code> "
          "(installs).</li><li><strong>Trust mode.</strong> <code>/fm-trust on</code> lets the agent edit the Ward and "
          "its host's settings; only you can type it.</li><li><strong>Sensitive repos.</strong> "
          "<code>sampo sensitive on</code> keeps a repo's content from leaving the machine.</li><li><strong>A daily risk "
          "budget.</strong> Destructive blocks and failed closes spend points; past the cap, autonomy steps down a "
          "notch until tomorrow.</li></ul>"),
         ("chart", lambda s: hbars("Refusals that waited for a yes", "Blocks in the categories only you can grant.",
                                   [(k, v) for k, v in sorted(s["guard"].items(), key=lambda kv: -kv[1])
                                    if k in ("core", "plugin", "state-direct", "credentials")], series=2))]},
    {"slug": "review", "nav": "Audits and review", "title": "Independent eyes before anything is done",
     "desc": "Five audit lenses, reviewers that didn't write the code, and skeptics that try to refute every finding.",
     "lede": "Finished work is checked through lenses with different prompts and context: intent, adversary, edge "
             "cases, operations and maintainability. Small tasks get a self-check; large ones get all five.",
     "blocks": [
         ("chart", _findings), ("chart", _severity),
         ("text", "Review that learns", "<p>Every confirmed finding is logged with its lens and severity. The defect "
          "classes a project keeps shipping go into the next builder's brief, so the same mistake is checked for "
          "before it is made again. <code>/sampo:review</code> runs one reviewer per lens over a diff, then two "
          "skeptics try to refute each finding.</p>")]},
    {"slug": "lanes", "nav": "Builder lanes", "title": "Many hands, one Smith",
     "desc": "Independent tasks run in parallel git worktrees, each reviewed before Sampo merges it.",
     "lede": "Independent tasks go to builder subagents, each in its own git worktree with a brief that says exactly "
             "what to build, how to test it and what not to touch.",
     "blocks": [
         ("chart", _lanes), ("chart", _agents),
         ("text", "From brief to merge", "<ul><li><strong>Brief.</strong> <code>sampo lane brief ID</code> writes the "
          "contract: the base commit, test first, the files it may change, the defects reviewers keep finding.</li>"
          "<li><strong>Review.</strong> An independent reviewer reads the branch; findings go back to the builder.</li>"
          "<li><strong>Merge.</strong> <code>sampo lane merge</code> runs the task's criteria first and refuses a red "
          "branch.</li><li><strong>Land.</strong> <code>sampo lane land</code> takes up to eight finished lanes as one merge train: "
          "the tests they reach run once on the merged tree, a red run names its culprit, the rest land and each task "
          "closes through <code>sampo task finish</code>'s own checks. A lane fixed after a red run trains again.</li>"
          "<li><strong>Resume.</strong> A stopped session picks its lanes back up.</li></ul>"),
         ("raw", '<figure class="dia forged">' + themed("merge-train", "The merge train: finished lanes queue, their merged "
                 "tree runs the tests they reach once, a red run is bisected to its culprit, and the rest land on main", 864, 773)
          + "<figcaption><b>The merge train</b><span>One gate run for up to eight lanes; a red one is bisected and "
          "parked with its reason.</span></figcaption></figure>")]},
    {"slug": "drive", "nav": "Drive and headless", "title": "It keeps going until the queue is done",
     "desc": "Drive mode, full auto, sampo run and sampo serve keep work moving without you watching.",
     "lede": "Drive mode works through the whole queue. In full auto the agent decides, records why, and saves what only "
             "you can approve for one summary at the end.",
     "blocks": [
         ("chart", _daily_types),
         ("text", "Ways to run unattended", "<ul><li><strong>Drive.</strong> The Stop hook names the next task, so a "
          "session doesn't stop while work remains.</li><li><strong><code>sampo run</code></strong> works the queue in "
          "fresh headless sessions, one task each, and waits out usage limits.</li><li><strong><code>sampo serve</code>"
          "</strong> runs Claude Code Remote Control on a server as a service, so you can send work from your phone."
          "</li><li><strong><code>sampo loop on</code></strong> keeps improving without being asked: build a round, "
          "review it, brainstorm on top, repeat.</li></ul>"),
         ("chart", lambda s: _totals(s, ["tasks_done", "commits", "captures", "subagents"]))]},
    {"slug": "cards", "nav": "Stack library", "title":"It knows your stack, and costs no context until it matters",
     "desc": "Over a thousand stack cards with exact verify commands, failing-test recipes and the mistakes models make.",
     "lede": "Each card gives the commands that verify the work, a failing-test recipe, the mistakes models actually "
             "make and a review checklist. Cards cost nothing until a matching file is edited.",
     "blocks": [
         ("chart", _cards),
         ("text", "Picked by what you touch", "<p><code>sampo lib</code> picks the cards that match your repo's manifests "
          "and files. The first edit of a file a card covers brings in that card's pitfalls, once per session; a file "
          "that imports a framework gets that framework's card too. <code>sampo lib source add</code> indexes another "
          "skills repo read-only.</p><p>These are the stack library's cards. The small cards Sampo writes as it works, "
          "general and UI alike, have <a href=\"../cards.html\">their own searchable page</a>.</p>")]},
    {"slug": "panel", "nav": "Where you see it", "title": "Sampo, live where you work",  # T-3996: the Board pane
     "desc": "The sampo-ui mod in Claude Code, sampo smith in a terminal, the live Board, Sampo Desktop and sampo mcp: "
             "one state, every view.",
     "lede": "Every view reads the same <code>sampo … --json</code>, so the task, the lanes, the merge train and the "
             "checks look the same in Claude Code, a terminal, a browser or the desktop app.",
     "blocks": [
         ("raw", '<figure class="dia forged">' + themed("board", "The live Board, numbered panels: builder lanes and the "
                 "files they hold, the merge train at its gate, CPU by kind of work against a 10% goal, the task in focus, "
                 "models, the library, state and tests", 1440, 1297, "png")
          + "<figcaption><b>The Board</b><span>Ten numbered panels, each a picture of one mechanism, drawn from Sampo's "
          "own state.</span></figcaption></figure>"),
         ("text", "In Claude Code", "<ul><li><strong>The band</strong> above the prompt: the task, its stage rail and "
          "step bar, and a live tree of every call with its time.</li><li><strong>The Board pane</strong> beside the "
          "chat (<code>/fm</code>): the Board's panels in a grid that fits the dock, the ones that matter now first."
          "</li><li><strong>Compact rows</strong> in the chat: a mark, a bold label and the one line that matters."
          "</li><li><strong>The status line</strong> under the prompt: model, context, spend and the task.</li></ul>"),
         ("raw", '<img class="shot" src="../assets/docs/claude-code.png" width="1816" height="1688" loading="lazy" '
                 'alt="Claude Code with the sampo-ui mod, mid-task: the chat as a tree of compact rows, the Board pane '
                 'beside it with the task in its header and its ten panels in a grid, and the status line">'),
         ("text", "In a terminal", "<p><code>sampo smith</code> runs the same loop on any model and draws it: the "
          "stations from plan to close, each check, what changed, and the time and tokens of every call.</p>"),
         ("raw", '<figure class="dia">' + themed("smith-tui", "sampo smith after a closed run: the plan and its steps "
                 "checked off, the activity log, three passing checks, the changed files and a time chart per step",
                 1040, 606) + "</figure>"),
         ("text", "Everywhere else", "<ul><li><strong>Sampo Desktop</strong>: every project and session in one "
          "window.</li><li><strong><code>sampo mcp</code></strong>: the project, read-only, for any MCP client.</li>"
          "<li><strong>Other agents</strong>: <code>sampo agents install codex|gemini|opencode</code> gives Codex, "
          "Gemini CLI and opencode the same Ward and context.</li></ul>"),
         ("chart", lambda s: _latency(s, {"SessionStart", "UserPromptSubmit", "Stop", "PostToolUse"}))]},
    {"slug": "speed", "nav": "Speed", "title": "Light enough to forget it's there",
     "desc": "Only the Ward runs synchronously; everything else records asynchronously or once per turn.",
     "lede": "Hooks run on every tool call, so each has a time budget and the bench holds them to it. Only the Ward "
             "and the write validators run in the call's path.",
     "blocks": [
         ("chart", _latency),
         ("text", "How it stays fast", "<ul><li><strong>Budgets per hook</strong>, checked by "
          "<code>bench_hooks.py</code> on every release.</li><li><strong>Reused gate passes.</strong> "
          "<code>sampo check</code> skips a gate that passed on an identical tree.</li><li><strong>Cached verdicts.</strong> "
          "Scripts the Ward read are cached by content.</li><li><strong>Notes once.</strong> Context is injected only "
          "when it changes what the agent should do next.</li></ul>"),
         ("chart", lambda s: tiles([("Gate runs", s["gate_seconds"]["runs"]),
                                    ("Median full gate run, s", s["gate_seconds"]["median"])]))]},
    {"slug": "learning", "nav": "Learning", "title": "It learns a fix once, and improves itself",
     "desc": "Known fixes, lessons, tripwires and a self-improvement loop turn friction into fixes.",
     "lede": "When a command fails and a later one gets past it, Sampo keeps the pair. The next session that meets "
             "the same failure gets the fix in the failure message, in any project on the machine.",
     "blocks": [
         ("chart", lambda s: _totals(s, ["lessons_shown", "findings", "captures", "tasks_done"])),
         ("text", "Where improvements come from", "<ul><li><strong>Lessons</strong> written at each close and shown "
          "to the next task that touches the same code.</li><li><strong>Friction passes</strong> that mine real Ward "
          "refusals and repeated manual work into tasks.</li><li><strong>Backtests.</strong> <code>sampo backtest</code> "
          "shows how often a rule change would have fired on your real sessions before it ships.</li><li><strong>"
          "Sampo on Sampo.</strong> It is built with itself; the charts on these pages are that record.</li></ul>"),
         ("chart", _tests)]},
    {"slug": "initiative", "nav": "Initiative", "title": "It finds the work before you ask",  # T-2127
     "desc": "sampo find ranks likely defects worst first; sampo scout files the proven ones from your own projects.",
     "lede": "Sampo reads your tree for likely defects: imports no manifest declares, docs naming files or code "
             "that aren't there, secrets, untested public code, SQL built from a parameter. Nothing in the tree runs, "
             "and each lead stays unproven until a failing test shows it.",
     "blocks": [
         ("text", "Useful, not noisy", "<ul><li><strong>Worst first.</strong> <code>sampo find --rank</code> orders "
          "leads by severity, reach and each finder's track record.</li><li><strong>Your drops teach it.</strong> A "
          "finder whose leads you mostly drop is muted, and the band says so.</li><li><strong>A few a day.</strong> "
          "<code>sampo scout --slow</code> files at most 3 leads open and 3 a day, only kinds that prove themselves, "
          "never one you dropped.</li><li><strong>Proven, then fixed.</strong> <code>sampo prove --capture 5</code> "
          "proves what it can on a throwaway copy and files each proven lead as a task whose check stays red until "
          "it's fixed; nothing proven leaves a receipt. <code>/sampo:initiative</code> runs map, find, prove and "
          "fix end to end.</li><li><strong>Only your trees.</strong> A tree whose recent commits aren't yours "
          "is skipped; where others commit too, each lead waits for your yes.</li><li><strong>Deeper when you "
          "allow it.</strong> <code>sampo find --ladder</code> adds each stack's own analyzers (ruff, go vet, gosec), "
          "run only in a tree you trusted with <code>sampo prove PATH --yes-run</code>, tree-sitter rules when the "
          "binding is installed, and git history to rank hotspots; a fixed bug no finder flagged becomes a blind "
          "spot and a task for a new rule.</li><li><strong>Everywhere.</strong> "
          "The terminal, the <code>find</code> MCP tool for other agents and Desktop, and Sampo Code through its "
          "run tool.</li></ul>"),
         ("term", "$ sampo find --rank\n2 candidate(s) in 2 files under ~/app; each is unproven until a failing test "
                  "shows it.\n0.30  src/m.py:1  dep  use: imports zorblax, which no manifest declares\n0.10  README.md:0  "
                  "docs  ./nowhere/setup.md: docs path: ./nowhere/setup.md"),
         ("chart", lambda s: _totals(s, ["captures", "tasks_done", "proofs"]))]},
    {"slug": "tools", "nav": "Tools", "title": "Small tools that save whole turns",
     "desc": "Batch edits, history tracing, time travel and branch tours.",
     "lede": "A handful of commands do in one call what usually takes several turns.",
     "blocks": [
         ("text", "The ones you'll use", "<ul><li><strong><code>sampo edit</code></strong> applies a batch of exact "
          "replacements across files, all or none.</li><li><strong><code>sampo why FILE:LINE</code></strong> traces a "
          "line back to the task and the words you asked in.</li><li><strong><code>sampo at T-0042 -- pytest</code>"
          "</strong> runs any command as of a task's start.</li><li><strong><code>sampo tour --since main</code>"
          "</strong> ranks a branch by review risk and says what's safe to skim.</li><li><strong><code>sampo digest"
          "</code></strong> sums up the week.</li></ul>"),
         ("term", "@@ src/app.py\n<<<\ntimeout = 30\n===\ntimeout = 90\n>>>\n@@ src/old.py\n<<<\nimport legacy\n===\n>>>"),
         ("chart", lambda s: _totals(s, ["commits", "evidence", "gate_runs"]))]},
    {"slug": "code", "nav": "Sampo Code", "title": "Sampo Code: the loop, on any model",
     "desc": "sampo code runs Sampo's stations itself, so small and local models only fill small checked slots.",
     "lede": "<code>sampo code</code> is Sampo's own harness: it runs plan, edit, verify and close itself, "
             "and the model only fills one small checked slot at a time. That is what lets a small local model do "
             "real work, and a big one go faster.",
     "blocks": [
         ("raw", FLOW_CODE),
         ("text", "Open the app", "<p><code>sampo code</code> on its own opens the app, the way <code>claude</code> "
          "does. Inside a project it shows that project's active task, queue, inbox, blocked tasks and past runs; "
          "<code>Esc</code> lists every Sampo project on the machine with what each is doing. <code>Enter</code> "
          "on a task runs it in its own folder (<code>sampo code --task ID</code>: the task's criteria and steps are the "
          "plan), <code>n</code> starts a new request there and <code>m</code> picks the runner.</p>"),
         ("text", "Watch it work", "<p><code>sampo code tui</code> runs a request in a terminal UI drawn from the "
          "harness's own events: the stations as a track, each check as a pass or fail chip, changed files as bars, "
          "where the time went, and tokens per model call. A Ward refusal stands out in the activity log; the "
          "model's own words are one key away (<code>t</code>). <code>d</code> shows the diff, <code>h</code> past "
          "runs, <code>m</code> picks the model or worker for the next request. It moves: the working station pulses, "
          "counters and bars ease to their values, a refusal flashes (<code>motion = reduced</code> or <code>off</code> stills it). "
          "Themes (night, paper, signal for colour-blind eyes, riso, mono), layout "
          "and keys come from <code>tui.toml</code>, and a Python file in Sampo's home folder can add a panel, a "
          "command or an event handler.</p>"),
         ("raw", '<picture><source media="(prefers-reduced-motion: reduce)" srcset="../assets/code-tui.svg">'
          '<img class="shot" src="../assets/code-tui-tour.svg" width="1040" height="678" loading="lazy" '
          'alt="sampo code tui --tour: a sample run in the real screen, a caption naming each panel as it lights: the '
          'track, red checks before the fix, the activity log, a refused rm -rf, tokens per call, changed files, '
          'green checks and the commit"></picture>'
          '<p><small>Drawn by <code>sampo code tui --tour --snapshot FILE.svg</code> from today\'s code each time the site '
          'builds.</small></p>'),
         ("text", "More than one request", "<ul><li><strong>Work the queue.</strong> <code>g</code> runs each queued "
          "task in turn and ends with a digest; pressed during a run, the queue starts when it ends.</li><li><strong>"
          "Ask beside a run.</strong> <code>n</code> during a run, or <code>sampo code --ask</code>, answers a question "
          "about the code read-only, next to the run already working in that tree.</li><li><strong>Follow up.</strong> "
          "<code>sampo code --after ID</code> hands the planner the earlier run's request, plan, ending and files; "
          "<code>T</code> lines up tickets that run after this one.</li><li><strong>Take it back.</strong> <code>u"
          "</code> reverts a closed run's commit; <code>P</code> drafts its pull request; <code>x</code> hands a "
          "stuck run to a live CLI, then <code>sampo code --recheck ID</code> checks what it did.</li><li><strong>Know "
          "why.</strong> <code>sampo code why</code> says how a run ended, what it was on, a stall's cause and the one "
          "command to run next; blocked, stalled and killed runs feed Sampo's own improvement round.</li><li><strong>"
          "Keep a frame.</strong> <code>F</code> saves the screen as SVG and PNG; <code>sampo code tui --chaos SECONDS"
          "</code> throws hostile events, keys and resizes at a headless app and exits 1 on a crash, with the seed to "
          "replay it.</li></ul>"),
         ("text", "Every verb and flag", "<p>Checked against <code>sampo code --help</code> by a site test, so a new "
          "flag can't ship unwritten here.</p><ul><li><strong>Verbs.</strong> <code>sampo code models</code> lists the "
          "model profiles; <code>sampo code runners</code> every coding CLI and model it can use; <code>sampo code config"
          "</code> its settings; <code>sampo code why</code> how a run ended; <code>sampo code bench</code> grades a "
          "model or worker; <code>sampo code tui</code> opens the app.</li><li><strong>Shape a run.</strong> "
          "<code>--model</code> picks the profile and <code>--escalate</code> gives three more fix rounds on a "
          "stronger model after three fail; <code>--planner</code> has a stronger model write the plan and the close "
          "notes while <code>--model</code> does every step, and takes a planner CLI too (<code>--planner claude</code>, "
          "<code>codex</code> or <code>gemini</code>, run in its read-only mode: the big model plans once, a local one does the work); <code>--max-steps</code> and <code>--max-usd</code> cap the turns and the dollars, and <code>--deadline</code> the wall clock: the model sees the time left each turn, and the run stops itself, resumable; "
          "<code>--reason state</code> carries a compact thought state between turns and thinks only on low confidence, "
          "a failed check or a loop (<code>off</code>, <code>native</code>, <code>budget</code>, <code>bias</code> and "
          "<code>schema</code> are the other arms the exam compares); "
          "<code>--small</code> holds a small local model to its plan: a change plan's checks must fail before the "
          "work, a change request isn't planned as research or answered, and the last step can't end with nothing "
          "changed (a profile's <code>\"small\": true</code> turns it on too); "
          "<code>--dry-plan</code> prints the plan and changes nothing; <code>--verify CMD</code> makes the plan "
          "one task done when CMD passes; <code>--no-commit</code> closes without committing; <code>--no-look</code> skips the closing look a state-of-the-code request otherwise gets after its first task (each gap the look names, with a check that fails now, becomes the next task, within bounds); before it plans a "
          "find-and-fix request it reads each function against its comment and the README, and <code>--no-review"
          "</code> skips that read.</li><li><strong>"
          "Resume or hand over.</strong> <code>--resume [ID]</code> carries on a crashed run from its journal "
          "(<code>-c</code>/<code>--continue</code> takes the newest); <code>--worker-timeout</code> limits each "
          "worker turn. <code>--task ID --restart</code> sets an unfinished run of the task aside, kept beside the "
          "new one, and starts over.</li><li><strong>Claude's spellings.</strong> <code>-p</code>/<code>--print</code> is "
          "<code>--plain</code>: one line per event, no screen; <code>--json</code> (or <code>--output-format "
          "json</code>) prints the run's events as JSON lines.</li><li><strong>Stack cards.</strong> A run offers "
          "the model the library's stack cards as names with a one-line summary each (the ones the project's files "
          "match and the ones the request names, at most five; a file's own cards ride with its read or edit, once "
          "each); the model opens one with the read-only <code>lib</code> tool, which returns the card as bounded "
          "data, and no model call is spent on the offer. A <code>lib</code> event in the <code>--json</code> lines "
          "records the names offered and opened, and the <code>stats</code> event counts <code>cards_offered</code> "
          "and <code>cards_opened</code>.</li><li><strong>Runners.</strong> <code>sampo code "
          "runners --check</code> runs each CLI's <code>--version</code> and flags a model the provider is retiring; "
          "<code>--ping [NAME]</code> asks a runner one tiny question and says how it answered, how fast; "
          "<code>--probe MODEL</code> scores a profile 0 to 10 on ten small graded prompts; <code>--use NAME"
          "</code> makes a runner the default, <code>--here</code> for this project only.</li><li><strong>Config."
          "</strong> <code>sampo code config --explain KEY</code> says what a setting does, its value and where it came "
          "from; <code>sampo code tui --print-config</code> prints <code>tui.toml</code> with your changes over the "
          "defaults; <code>--themes</code> shows every theme side by side.</li><li><strong>Bench.</strong> "
          "<code>sampo code bench --seeds A,B</code> runs some seeds; <code>--variants base,evidence</code> runs them "
          "once per prompt variant; <code>--seed-timeout</code> limits each seed.</li><li><strong>Record it."
          "</strong> <code>sampo code tui --events FILE --snapshot SVG --size WxH</code> draws a recorded run, "
          "<code>--record VIDEO</code> plays it into an mp4 or gif, <code>--tour</code> is the 30-second guided "
          "run, and <code>--chaos SECONDS --seed SEED</code> replays a crash.</li><li><strong>Unattended.</strong> "
          "<code>--stale-ok REASON</code> runs on even when the Ward replay is red or over a week old, and logs the "
          "waiver.</li></ul>"),
         ("text", "Every key","<p>Read from the key map sampo code tui ships, so a new key shows here at the next build. "
          "<code>?</code> shows the same list in the app, and <code>tui.toml</code> rebinds any of them.</p>"),
         ("chart", _keys),
         ("examples", "Examples: real runs of every kind",
          "<p>Each card below is one real run, in a throwaway repo on this machine, with nothing set up but Claude "
          "Code's own CLI: <code>--worker claude</code> plans in Claude's read-only mode, then does each step, while "
          "sampo code makes the task, runs every check before and after the work, and closes it with a commit only when "
          "the checks pass. A fix, a feature, a rename across files, docs, a question, a project from an empty folder "
          "and another language: the cards are drawn from each run's own events, kept in <code>demo/examples/</code>, "
          "and <code>sampo code tui --events</code> replays any of them.</p>"),
         ("text", "Any model, any CLI", "<ul><li><strong>Any OpenAI-compatible endpoint</strong>: Ollama, LM Studio, "
          "llama.cpp, vLLM, OpenRouter, Groq, OpenAI, Gemini's compatible endpoint, and Anthropic's API.</li><li>"
          "<strong>Other CLIs as workers.</strong> claude, codex, gemini or aider do a step; the harness "
          "judges the diff and runs the checks itself.</li><li><strong>The same Ward</strong> on every command and "
          "edit, through the hook other harnesses use.</li><li><strong>A bench</strong>: <code>sampo code bench</code> grades any model or CLI as a "
          "Sampo worker on five seeded tasks in throwaway repos, counting a task only when its own check passes "
          "afterwards.</li></ul>"),
         ("chart", lambda s: tiles([("Model client tests", (s.get("code") or {}).get("model_tests", "?")),
                                    ("Endpoints it finds", (s.get("code") or {}).get("endpoints", "?")), ("Status", "Shipped, still growing")]))]},
    {"slug": "benchmarks", "nav": "Benchmarks", "title": "Measured against Claude Code",
     "desc": "Real runs, published whichever way they fall: dangerous commands, and real tasks graded blind.",
     "lede": "Benchmarks run for real on this machine and published as they came out: a set of commands that destroy "
             "work or leak secrets, sixteen fully specified coding tasks graded by hidden tests, and one long session "
             "of eight requests that compacts as it goes, each run with Sampo and with Claude Code on its own.",
     "blocks": [
         ("text", "Why use Sampo", _why),
         ("text", "Safety: what runs in bypass mode", "<p>Bypass mode is how Sampo is meant to run: no permission "
          "prompts. Claude Code on its own asks before none of these in that mode. Sampo's Ward judges each one "
          "before it runs; the everyday set checks that it doesn't get in the way of normal work.</p>"),
         ("chart", _safety),
         ("text", "Quality: eight fully specified tasks, graded blind", "<p>Each task asks for one small module in "
          "a neutral Python repo (durations, an LRU cache, SemVer precedence, text tables, ranges, dotted paths, a "
          "token bucket, CSV lines). The request states every rule its hidden tests check, so passing means doing "
          "the whole spec, not guessing wording. Both sides get the same request, model, budget and time limit, each "
          "in a clean worktree, three runs per task; a task counts as passed when at least two of its three runs pass. "
          "The Claude Code side runs with no plugins but keeps Sampo's Ward on, so neither side can damage the "
          "machine. <code>bench/spec-suite/make.py</code> builds the repo.</p>"),
         ("chart", lambda s: _duel(s, "quality")),
         ("text", "Harder: eight tasks where checking matters", "<p>A second neutral repo, an invoicing package whose "
          "modules share helpers, graded by its whole test suite. Four tasks change a shared helper, where the obvious "
          "edit breaks another module's existing tests unless the request's \"Keep\" rules are followed; four are dense "
          "edge-case specs (text wrapping, INI files, interval arithmetic, a tokenizer). Each task's plausible shortcut "
          "is recorded and proven to fail. Grading restores the reference tests, so editing an existing test can't "
          "pass. <code>bench/spec-suite/make.py --suite hard</code> builds it.</p>"),
         ("chart", lambda s: _duel(s, "hard")),
         ("text", "The harder tasks with a smaller model", "<p>The same eight harder tasks with Haiku on both sides, "
          "where mistakes should be likelier and a test run at the end should matter more. Haiku sessions cost about "
          "a cent each, so Sampo's fixed cost (its skill and agent listings, its hooks and the test run at the end) "
          "weighs more here.</p>"),
         ("chart", lambda s: _duel(s, "hard-haiku")),
         ("text", "One long session: eight requests, compacting as it goes", "<p>The eight harder tasks asked for "
          "at once, in one Haiku session per side. Each side's context is compacted whenever it nears 40,000 tokens, "
          "which brings forward what happens in any long session: earlier work is summarised and the details are "
          "gone. The hidden tests of all eight requests grade where each side ends up. Each session is capped at "
          "$0.50; each side runs three times and the table shows the first run's per-request result.</p>"),
         ("chart", lambda s: _long(s, "long")),
         ("text", "A marathon: Sampo keeps going", "<p>The same long session, but once the asked work is closed, "
          "Sampo's side looks for more work in the repo and does it, test first, until its $1 cap. Claude Code "
          "on its own stops when the asked work is done. The hidden tests still grade the end state, so work found "
          "later can't quietly break what was asked for.</p>"),
         ("chart", lambda s: _long(s, "marathon")),
         ("text", "Models: do the local models beat the rules?", "<p>Each Rune generation reruns a $0 bench of the local "
          "models on this machine's own closed tasks. A model goes live only on at least 200 rows of a frozen held-out "
          "set, beating both the rules path and the majority baseline by 5 points (paired p under 0.05) with Brier no "
          "worse; a later run that falls under demotes it. The last good run stays, marked stale, when a later one fails "
          "or is skipped.</p>"),
         ("chart", _model_bench),
         ("text", "Where Sampo loses", "<p>Generated from the same results as the tables above: every row where "
          "Claude Code passed and Sampo didn't, or Sampo cost or took 25% more. Each one reruns on its own with "
          "<code>sampo bench repro</code>, both sides, with that run's model and settings.<!-- rule --></p>"),
         ("chart", _loses),
         ("text", "What the duels changed in Sampo", "<p>Reading the sessions turn by turn showed where Sampo's "
          "extra cost went: not into its context (about 2,000 tokens), but into turns spent on its own commands. A "
          "first run on these tasks cost Sampo 8 to 13 turns where Claude Code used 4. The fixes moved Sampo's "
          "bookkeeping out of the model and into its hooks:</p><ul><li><strong>The task makes itself.</strong> The "
          "first edit of a one-request session creates its task from the request and goes ahead.</li><li><strong>It "
          "closes itself.</strong> At the end, Sampo runs the project's tests and closes and commits the task when "
          "they pass. When they fail, it tells the agent once, so the run fixes its work instead of ending on "
          "it.</li><li><strong>It starts informed.</strong> The session begins with the project's layout and test "
          "command, so the agent writes instead of listing folders.</li><li><strong>Less friction.</strong> Commands "
          "default to the active task, a broken check can be corrected, and only real security names ask for an "
          "adversary review.</li><li><strong>Requests survive compaction.</strong> The first long runs lost the rules "
          "of later requests after a compaction (44 of 105 hidden tests once)<!-- source: T-1242 -->: a summary keeps a request's name and "
          "drops its rules. Now each request is its own task holding its text, the open ones come back after every "
          "compaction, and the next one is focused with its text when the agent moves on.</li></ul>"),
         ("text", "The first run, and why it was replaced", "<p>The first duel used six tasks from Sampo's own "
          "history and came out at 0 of 6 for Sampo, 1 of 6 for Claude Code, with Sampo costing 33% more<!-- source: T-1231 -->. "
          "Reading its transcripts showed the case set couldn't measure either side: three cases graded what Sampo "
          "had learned from this user's own past corrections, which a fresh session can't have, and the other three "
          "checked exact wording their requests never stated. Both sides also worked inside Sampo's own code, which "
          "the Ward protects. The transcripts did show where Sampo spent extra turns on its own commands; those "
          "were cut before this run.</p>"),
         ("text", "Method and limits", "<ul><li><strong>Small sample.</strong> Sixteen tasks, three runs each, two "
          "models, and five long sessions a side: enough to see a direction, not a precise rate.</li><li><strong>"
          "Short tasks don't separate the sides.</strong> Fully specified tasks this size are within reach of "
          "today's models without help, so those runs measure cost and time; the long session is where they part. "
          "A public benchmark is next.</li><li><strong>Compaction is brought forward.</strong> The long sessions "
          "compact at 40,000 tokens instead of near the model's limit, to show in minutes what a day-long session "
          "meets.</li><li><strong>Reproduce it.</strong> <code>sampo bench duel --vs none</code> runs the quality "
          "benchmark on your own finished tasks; <code>bench/spec-suite/README.md</code> gives the long-session "
          "commands; <code>python3 site/bench_safety.py</code> reruns the safety set."
          "</li><li><strong>Other plugins.</strong> <code>sampo bench duel PLUGIN</code> runs the same cases with another "
          "plugin beside Sampo.</li></ul>")]},
    {"slug": "playbooks", "nav": "Playbooks", "title": "A field guide to the playbooks",
     "desc": "Every procedure Sampo can load, by stage, with when to use it.",
     "lede": "A playbook is a short procedure loaded only when its stage comes up: a flaky test, a migration, a "
             "release. Each one names when to use it and the steps that prove the work.",
     "blocks": [
         ("text", "Using and learning them", "<ul><li><strong>Load one.</strong> <code>sampo playbooks show NAME</code> "
          "fills in the project's facts and adds its own notes from <code>.claude/playbooks/</code>.</li><li><strong>"
          "Practise.</strong> <code>sampo playbooks kata NAME</code> gives held-out briefs to answer; <code>--answer "
          "FILE</code> scores yours.</li><li><strong>Score a run.</strong> <code>sampo playbooks score NAME --task ID"
          "</code> shows which steps a finished task followed.</li><li><strong>Learn from a change.</strong> <code>"
          "sampo playbooks lesson RANGE</code> is a five-minute read of a diff: what changed, where, what its tasks "
          "learned and the checklists to read it with.</li><li><strong>Grow your own.</strong> <code>sampo playbooks "
          "draft ID</code> turns a finished task into a draft; <code>mine</code> and <code>counter</code> read the "
          "repo's history for what changes together and what failed.</li></ul>"),
         ("chart", _field_guide)]},
    {"slug": "shaders", "nav": "Shaders", "title": "Eight shader looks, running",  # T-1784
     "desc": "Every shader look in the library, live with the preset its card ships; still if you ask for less motion.",
     "lede": "A shader look is a card: the fragment shader, its uniforms, what it costs, its still frame and what it "
             "suits. Each one below runs its card's own preset.",
     "blocks": [
         ("text", "Using one", "<ul><li><strong>Read the card.</strong> <code>sampo lib show shader-aurora-bands</code> "
          "prints the shader, its uniforms and when not to use it.</li><li><strong>Check it.</strong> <code>sampo scene "
          "shader CARD</code> checks the GLSL with no browser, and with glslangValidator when it's installed.</li>"
          "<li><strong>See them all.</strong> <code>sampo scene sketchbook --out FILE</code> writes this page; each "
          "thumbnail draws only while on screen, and without WebGL2 it shows the card's description.</li></ul>"),
         ("chart", _sketchbook)]},
    {"slug": "gallery", "nav": "Gallery", "title": "What each design playbook builds",
     "desc": "A worked example page for every UI playbook, shipped beside it and built with the site.",
     "lede": "Every UI playbook ends in a page you can open: the playbook's rules applied to one small screen, in "
             "one file with no outside host. Each passes <code>sampo new check</code> and keeps the five-rule UI "
             "contract: reduced motion, contrast, keyboard, 360 px and designed states.",
     "blocks": [
         ("chart", _gallery),
         ("text", "Use one", "<ul><li><strong>Open it from disk.</strong> Each example lives at <code>plugin/skills/"
          "playbooks/references/ui/examples/</code> (the 3D one is the <code>sampo new scene</code> template), named on "
          "its playbook's <code>Example:</code> line.</li><li><strong>Start from it.</strong> Copy the file, then "
          "<code>sampo new check FILE</code> keeps it to the same rules.</li><li><strong>Rebuild.</strong> <code>python3 "
          "site/build.py</code> copies every example into this gallery.</li></ul>")]},
    {"slug": "releases", "nav": "Releases", "title": "Shipped in small, gated releases",
     "desc": "Every release passes the full gates and a release check before it reaches main.",
     "lede": "<code>sampo release</code> runs the ritual as nine named steps with a checkpoint after each: gates, version "
             "bumps, changelog, commit, release check, push, round close and a canary in every local project.",
     "blocks": [
         ("chart", _tests),
         ("chart", _wall),
         ("text", "What a release proves", "<ul><li><strong>The full gates</strong> on the exact commit that is "
          "pushed, never later work.</li><li><strong>A release check</strong>: doctor and the demo in a throwaway "
          "home.</li><li><strong>Your yes to push</strong>, spent by that one push.</li><li><strong>Resumable.</strong> "
          "A stop at any step continues from there with <code>--resume</code>.</li></ul>")]},
]

# T-3996: the features index and every page's side list draw these groups, in this order; each page is in one
_BY = {p["slug"]: p for p in PAGES}
PAGES = [_BY[slug] for _, _, slugs in GROUPS for slug in slugs]  # the Next links follow the groups
if len(PAGES) != len(_BY):
    raise SystemExit(f"site/pages.py: pages in no group: {sorted(set(_BY) - {p['slug'] for p in PAGES})}")


def cards_page(index_url):
    """T-3101: site/cards.html's body. cards.js fills it from index_url (build.export_cards writes it): the grid tabs and
    cell filters from the index's grids, the list a page at a time, each card's steps from its shard when it opens."""
    return f"""<header class="cards-head">
<p class="eyebrow">Small cards</p>
<h1 class="engraved">Every card Sampo has written</h1>
<p class="lede">Each card is one small, checked recipe a small model can follow: when it applies, its steps and the
commands to run, a worked example, the check that proves it and the traps. Search titles, when lines and commands,
narrow by grid and by each cell of it, open a card in place and copy its link.</p>
</header>
<div class="cards-tools forged" id="cards" data-index="{esc(index_url)}">
  <label class="cards-search"><span>Search</span>
    <input id="cards-q" type="search" aria-label="Search the cards" placeholder="Title, when line or command" autocomplete="off" spellcheck="false"></label>
  <div class="cards-grids" id="cards-grids" role="group" aria-label="Grid"></div>
  <div class="cards-facets" id="cards-facets"></div>
  <p class="cards-status" id="cards-status" role="status">Loading the cards…</p>
</div>
<p class="cards-empty" id="cards-empty" hidden></p>
<div class="cards-list" id="cards-list"></div>
<p class="cards-foot"><button class="cards-more" id="cards-more" type="button" hidden>Show more</button></p>
<noscript><p class="cards-empty">Searching needs JavaScript; every card is listed in
<a href="{esc(index_url.split("?")[0])}">the index</a>.</p></noscript>"""
