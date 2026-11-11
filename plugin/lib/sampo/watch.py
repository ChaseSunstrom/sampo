"""fm watch: live Sampo dashboard (curses) and its plain-text twin (--once). Stdlib only, read-only."""
import contextlib
import json
import os
import re
import statistics
import sys
import time

from sampo import core as c


def _tail_jsonl(path, max_bytes=256 * 1024):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - max_bytes))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _latest_session(project):
    d = os.path.join(c.state_dir(), "sessions")
    try:
        files = sorted((os.path.join(d, f) for f in os.listdir(d) if f.endswith(".json")), key=os.path.getmtime, reverse=True)
    except OSError:
        return None
    for path in files:
        try:
            with open(path) as f:
                snap = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(snap, dict):  # T-1097: a side file (sid.outlined-<agent>.json is a list), not a snapshot
            continue
        if snap.get("project") in (project, None):
            return snap
    return None


def gather(p):
    briefs = c.load_briefs(p)
    sd = c.state_dict(p, briefs)
    act = c.active_brief(briefs, p.lane)
    events = _tail_jsonl(os.path.join(c.state_dir(), "events.jsonl"))
    mine = [e for e in events if e.get("project") in (p.slug, None)]
    running = {}
    for e in events:
        if e.get("kind") == "subagent_start":
            running[e.get("agent_id")] = e
        elif e.get("kind") == "subagent_stop":
            running.pop(e.get("agent_id"), None)
    latency, series = {}, []
    for e in events[-2000:]:
        if e.get("kind") == "hook_ms" and isinstance(e.get("ms"), (int, float)):
            latency.setdefault(e.get("event"), []).append(e["ms"])
            series.append(e["ms"])
    touched, ledger = [], c.ledger_tail(p, 400)
    if act:
        for e in ledger:
            f = (e.get("data") or {}).get("file")
            if e.get("event") == "touched" and e.get("task") == act.id and f and f not in touched:
                touched.append(f)
    checks = next((e for e in reversed(ledger) if e.get("event") == "check_run"), None)
    return {"sd": sd, "active": act, "project": p, "briefs": briefs, "latency": latency, "series": series[-40:],
            "touched": touched[-10:],
            "checks": checks,
            "tools": [e for e in mine if e.get("kind") in ("tool", "tool_fail")][-12:],
            "subagents": list(running.values())[-5:], "guard": [e for e in mine if e.get("kind") == "guard_block"][-5:],
            "session": _latest_session(p.slug),
            "recent": [line for line in map(_recent, ledger) if line][-24:]}  # T-1002: the pane's History fills


def _recent(e):
    """One line for a ledger event worth seeing (evidence, audits, captures, decisions, approvals, done)."""
    d, t, ts = e.get("data") or {}, e.get("task") or "", str(e.get("ts") or "")[11:16]  # T-1026 review: a bad ts
    kind = e.get("event")
    if kind == "evidence":
        what = f"step {d['step']}" if d.get("step") else f"criterion {d['ac']}" if d.get("ac") else "check"
        result, cmd = " ".join(str(d.get("result", "")).split()), " ".join(str(d.get("cmd", "")).split())
        mark = "≈" if d.get("inconclusive") else "✗" if result.startswith("✗") else "✓"  # T-0195: a red run isn't a tick
        text = f"{mark} {t} {what}: {c.fit(cmd, 60)} → {result}"
    elif kind == "surprise":  # T-0253
        text = f"! {t} surprise: {c.fit(c.plain(str(d.get('text', ''))), 80)}"
    elif kind == "audit":
        text = f"◇ {t} audit {d.get('lens')}: {d.get('result', '')}"
    elif kind in ("capture", "intake"):
        text = f"⚑ {t} captured" if t else "⚑ intake"
    elif kind == "decision":
        text = f"◆ {d.get('decision', '')}"
    elif kind == "approval_requested":
        text = f"? {t} asked: {'+'.join(d.get('allow') or [])}"
    elif kind in ("approval_granted", "approval_declined"):
        text = f"{'✔' if kind == 'approval_granted' else '✖'} {t} {kind.split('_')[1]}"
    elif kind == "task_done":
        text = f"■ {t} done"
    else:
        return None
    return f"  {ts}  {text}"


def _history(events, by_id, n=60):
    """T-1026: the pane's History, structured: when, a mark, the task, a short event and the task's title, newest last."""
    out = []
    for e in events:
        d, t, kind = e.get("data") or {}, e.get("task") or "", e.get("event")
        if kind == "evidence":
            what = f"step {d['step']}" if d.get("step") else f"criterion {d['ac']}" if d.get("ac") else "check"
            red, odd = str(d.get("result", "")).lstrip().startswith("✗"), d.get("inconclusive")
            mark, text = ("≈", f"{what} inconclusive") if odd else ("✗", f"{what} failed") if red else ("✓", f"{what} passed")
        elif kind == "surprise":
            mark, text = "!", "surprise: " + c.fit(c.plain(str(d.get("text", ""))), 80)
        elif kind == "audit":
            mark, text = "◇", f"audit {d.get('lens')}"
        elif kind in ("capture", "intake"):
            mark, text = "⚑", "captured" if t else "intake"
        elif kind == "decision":
            mark, text = "◆", c.fit(c.plain(str(d.get("decision", ""))), 120)
        elif kind == "approval_requested":
            mark, text = "?", "asked: " + "+".join(d.get("allow") or [])
        elif kind in ("approval_granted", "approval_declined"):
            mark, text = ("✔", "granted") if kind == "approval_granted" else ("✖", "declined")
        elif kind == "task_done":
            mark, text = "■", "done"
        elif kind == "outcome" and d.get("survival") == "lost":  # T-1060: a close undone within 30 days (a first
            mark, text = "✗", "lost"  # settle grades hundreds; the survivors would flood the 60 rows)
        else:
            continue
        b = by_id.get(t)
        out.append({"time": str(e.get("ts") or "")[11:16], "mark": mark, "task": t or None, "text": text,
                    "title": c.plain(b.title)[:120] if b else None})
    return out[-n:]


def _pct(vals, q):
    vals = sorted(vals)
    return vals[min(len(vals) - 1, int(len(vals) * q))]


def render(d, width=100):
    sd, act, p = d["sd"], d["active"], d["project"]
    out = [f"Sampo · {p.slug} · {time.strftime('%H:%M:%S')}", ""]
    if act:
        a = sd["active"]
        out.append(f"Active: {a['id']} [{a['type']} {a['tier']}] {a['title']}")
        st = a.get("stage")
        out.append("  " + " → ".join(f"[{x}]" if x == st else x for x in c.STAGES)
                   + f" · audits {a['audits']['done']}/{a['audits']['required']}")
        out += [f"  [{'x' if s.done else ' '}] {s.n}. {s.text}{'  <-' if s.current else ''}" for s in act.steps()]
        out.append(f"  evidence: {len(act.evidence())} line(s)")
    else:
        out.append("Active: none")
    out.append(f"Queue ({len(sd['queue'])}): " + ("; ".join(f"{q['id']} {q['type']} {q['tier']}" for q in sd["queue"][:8]) or "empty"))
    out.append(f"Inbox ({len(sd['inbox'])}): " + ("; ".join(f"{q['id']} {q['title'][:30]}" for q in sd["inbox"][:5]) or "empty"))
    if sd.get("asks"):
        out.append("Waiting on you: reply yes = " + "; ".join(f"{'+'.join(x['allow'])} for {x['task']}"
                                                                for x in sd["asks"]))
    out += ["", "Recent:"] + (d["recent"][-8:] or ["  (nothing yet)"])
    out += ["", "Tool timeline:"]
    for e in d["tools"]:
        ms = f"{e['ms']}ms" if e.get("ms") is not None else "-"
        out.append(f"  {e.get('ts', '')[11:19]}  {e.get('tool', ''):<10} {'ok ' if e.get('ok') else 'ERR'} {ms:>8}  {e.get('target', '')}")
    if not d["tools"]:
        out.append("  (no tool calls yet)")
    out.append("Subagents: " + (", ".join(f"{e.get('agent_type')} ({e.get('agent_id')}) since {e.get('ts', '')[11:19]}"
                                            for e in d["subagents"]) or "none running"))
    out.append("Files touched: " + (", ".join(os.path.relpath(f, p.root) if f.startswith(p.root) else f
                                              for f in d["touched"]) or "none"))
    out.append("Guard blocks: " + ("; ".join(f"{e.get('ts', '')[11:19]} {e.get('category')} ({e.get('target', '')[:40]})"
                                             for e in d["guard"]) or "none"))
    out += ["", "Hook latency (ms):"]
    for ev, vals in sorted(d["latency"].items()):
        out.append(f"  {ev:<18} p50 {statistics.median(vals):>6.1f}  p95 {_pct(vals, 0.95):>6.1f}  n={len(vals)}")
    if not d["latency"]:
        out.append("  (no samples)")
    s = d["session"]
    if s:
        rl = ((s.get("rate_limits") or {}).get("five_hour") or {}).get("used_percentage")
        hit = s.get("cache_hit_ratio")
        out.append(f"Session: {s.get('model') or '?'} · ctx {s.get('context_pct')}% · ${s.get('cost_usd')}"
                   + (f" · 5h {rl}%" if rl is not None else "") + (f" · cache {int(hit * 100)}%" if hit is not None else ""))
    else:
        out.append("Session: no statusline snapshot yet")
    return [c.plain(line)[:width] for line in out]  # titles and events are user text: no terminal sequences


def cmd_watch(args):
    if getattr(args, "web", False):
        from sampo import live
        return live.cmd_live(args)
    from sampo import cli
    p = cli.resolve(args)
    if args.once:
        print("\n".join(render(gather(p))))
        return
    import curses

    def loop(scr):
        curses.curs_set(0)
        scr.nodelay(True)
        while True:
            h, w = scr.getmaxyx()
            scr.erase()
            for i, line in enumerate(render(gather(p), w - 1)[:h - 1]):
                scr.addnstr(i, 0, line, w - 1)
            scr.addnstr(h - 1, 0, "q quits · refresh %.1fs" % args.interval, w - 1)
            scr.refresh()
            deadline = time.monotonic() + args.interval
            while time.monotonic() < deadline:
                if scr.getch() in (ord("q"), 27):
                    return
                time.sleep(0.05)
    try:
        curses.wrapper(loop)
    except KeyboardInterrupt:
        pass


TYPICAL = ("task_new", "focus", "task_done")  # T-3573: all typical() reads: callers fetch only these


def typical(events):
    """Median minutes from first focus to done per "TYPE/TIER" with 3+ closed tasks, from the ledger: what a size word
    usually means in this project (T-0116)."""
    import statistics
    kind, start, took = {}, {}, {}
    for e in events:
        t, ev = e.get("task"), e.get("event")
        if ev == "task_new" and t:
            d = e.get("data") or {}
            kind[t] = f"{d.get('type')}/{d.get('tier')}"
        elif ev == "focus" and t:
            start.setdefault(t, e.get("ts"))
        elif ev == "task_done" and t in start and t not in took:
            a, b = c.parse_ts(start[t]), c.parse_ts(e.get("ts"))
            if a and b:
                took[t] = (b - a).total_seconds() / 60
    groups = {}
    for t, m in took.items():
        if t in kind:
            groups.setdefault(kind[t], []).append(m)
    return {k: round(statistics.median(v)) for k, v in groups.items() if len(v) >= 3}


_SPLIT_MARKS = (("planning", "task_new"), ("executing", "task_plan"), ("verifying", "ac_check"), ("auditing", "audit"),
                ("closing", "finish"))  # T-1078: the ledger event that starts each stage (stages themselves aren't stored)


def _stage_secs(evs, end):
    """{stage: seconds} for one task's ledger events: each stage runs from its mark to the next mark that came later
    (a stage whose mark is missing or out of order is skipped); the last runs to `end`. T-1137: recorded stage events
    win when the task has any (a stage can recur, so its times add up); older tasks fall back to the proxies."""
    moves = sorted((c.parse_ts(e.get("ts")), (e.get("data") or {}).get("to")) for e in evs if e.get("event") == "stage")
    if moves:
        out = {}
        for i, (t, st) in enumerate(moves):
            if t and st in dict(_SPLIT_MARKS):
                nxt = moves[i + 1][0] if i + 1 < len(moves) else end
                out[st] = out.get(st, 0) + max(0, round((nxt - t).total_seconds()))
        return out
    first = {}
    for e in evs:
        t = c.parse_ts(e.get("ts"))
        if t and (e.get("event") not in first or t < first[e["event"]]):
            first[e["event"]] = t
    marks, last = [], None
    for st, ev in _SPLIT_MARKS:
        if ev in first and (last is None or first[ev] > last):
            marks.append((st, first[ev]))
            last = first[ev]
    return {st: max(0, round(((marks[i + 1][1] if i + 1 < len(marks) else end) - t).total_seconds()))
            for i, (st, t) in enumerate(marks)}


def splits(events, act):
    """T-1078: the active task's seconds in each stage so far against the median for closed tasks of its type and tier
    (3+ samples per stage); sign is "over"/"under"/"even" with a typical, else None."""
    import datetime
    import statistics
    by, kind = {}, {}
    for e in events:
        if e.get("task"):
            by.setdefault(e["task"], []).append(e)
            if e.get("event") == "task_new":
                d = e.get("data") or {}
                kind[e["task"]] = f"{d.get('type')}/{d.get('tier')}"
    want = f"{act.type}/{act.tier}"
    samples = {}
    for t, evs in by.items():
        done = [x for x in evs if x.get("event") == "task_done"]
        if t != act.id and kind.get(t) == want and done:
            for st, s in _stage_secs(evs, max(c.parse_ts(x.get("ts")) for x in done)).items():
                samples.setdefault(st, []).append(s)
    out = []
    for st, s in _stage_secs(by.get(act.id, []), datetime.datetime.now(datetime.timezone.utc)).items():
        v = samples.get(st, [])
        typ = round(statistics.median(v)) if len(v) >= 3 else None
        out.append({"stage": st, "secs": s, "typical": typ, "delta": None if typ is None else s - typ,
                    "sign": None if typ is None else "over" if s > typ * 1.1 else "under" if s < typ * 0.9 else "even"})
    return out


def brainstorm(p):
    """The newest brainstorm (T-0124): while fm ideas runs, how many lens answers are in and the ideas so far; after,
    how many ideas and the first ones. None when the project has none. A run that never wrote ideas.md and started
    over an hour ago counts as stopped."""
    import re
    root = os.path.join(p.dir, "research")
    try:
        name = max(d for d in os.listdir(root) if d.startswith("brainstorm-") and os.path.isdir(os.path.join(root, d)))
    except (OSError, ValueError):
        return None
    d = os.path.join(root, name)
    try:
        with open(os.path.join(d, "status.json"), encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {}
    answers = sorted(f for f in os.listdir(d) if f.endswith(".md") and f != "ideas.md")
    age_h = round((time.time() - os.path.getmtime(d)) / 3600, 1)  # T-0146: an old one folds to a line in the pane
    if os.path.exists(os.path.join(d, "ideas.md")):
        with open(os.path.join(d, "ideas.md"), encoding="utf-8") as f:
            body = f.read().split("## New ideas per lens")[0]
        ideas = [ln[2:].strip() for ln in body.splitlines() if ln.startswith("- ")]
        made = os.path.getmtime(os.path.join(d, "ideas.md"))  # T-0196: a slate recorded after it grounded its ideas
        grounded = any(f.startswith("brainstorm-") and f.endswith(".md") and os.path.getmtime(os.path.join(root, f)) > made
                       for f in os.listdir(root))
        return {"name": name, "running": False, "answers": len(answers), "count": len(ideas), "ideas": ideas[:8],
                "age_h": age_h, "grounded": grounded}
    ideas = []
    for a in answers:
        with open(os.path.join(d, a), encoding="utf-8") as f:
            ideas += [c.plain(t).strip() for t in re.findall(r"(?m)^\s*[-*]\s*\*\*(.+?)\*\*", f.read())]
    ideas = list(dict.fromkeys(ideas))
    running = (c.age_days(st.get("started")) or 1) * 24 < 1
    return {"name": name, "running": running, "answers": len(answers),
            "expected": len(st.get("lenses") or []) * int(st.get("rounds") or 1), "count": len(ideas), "ideas": ideas[:8],
            "age_h": age_h}


LEARNED_TTL_S = 120
SLOW_TTL_S = 900  # T-3580: a week's lessons, the Brier line, muted finders: ~2.5 s CPU to count, the same news 15 min


def _kept(p, name, build, ttl=LEARNED_TTL_S):
    """build(), kept in the project's state for ttl seconds: every pane poll is a new process (no in-process memo
    helps), and a count two minutes old is the same news. T-3542: under cache/, so a rewrite never moves the state
    folder the mod watches (each one woke its next poll)."""
    path = os.path.join(p.dir, "cache", name)
    try:
        with open(path, encoding="utf-8") as f:
            hit = json.load(f)
        if time.time() - hit["at"] < ttl:
            return hit["value"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    value = build()
    c.write_atomic(path, json.dumps({"at": time.time(), "value": value}))
    return value


def _learned_line(p):
    """T-1045: what Sampo learned this week, the pane's idle line (a week's ledger and briefs cost ~0.45 s to count)."""
    from sampo import cost
    return _kept(p, "learned-line.json", lambda: cost.learned(p, 7, len(cost._week(p, 7)[0]))["line"], SLOW_TTL_S)


_PATHS = ("watch", "watch_now", "trust_file", "risk_files", "root")  # paths a surface stats or writes: kept as they are
_ESCAPES = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b.")  # CSI, OSC, ESC x


def _shown(x, key=None):
    """T-1674: every string of the view as plain text (newlines kept): Claude Code refuses the whole pane when a text
    child holds a control character, and a task title or step can hold anything."""
    if key in _PATHS:
        return x
    if isinstance(x, dict):
        return {k: _shown(v, k) for k, v in x.items()}
    if isinstance(x, list):
        return [_shown(v, key) for v in x]
    return c.plain_lines(_ESCAPES.sub("", x)) if isinstance(x, str) else x


def _cpu_series(p):
    from sampo import cpu
    return cpu.series(cpu.goal(p))


def _guarded(fn):
    """A pane section that fails is left out, never the whole view (the mod renders what it gets)."""
    try:
        return fn()
    except Exception:
        return None


def _unproven(got):
    """T-2395: {n, top}: how many done tasks no run proved, and the kind most of them need, from
    c.unproven_summary(p, c.PROOF_DAYS) (the cached rollup; T-3580: read once per view, the band's signal shares it)."""
    counts = got.get("counts") or {}
    return {"n": got["n"], "top": max(counts, key=lambda k: (counts[k], k)) if counts else None}


def _weekly_lean(p):
    from sampo import budget
    return budget.weekly_lean(p)


def _budget(p=None):
    """T-0228: today's spend against the caps (fm budget), for the pane."""
    from sampo import budget
    rows = budget.ledger()
    usd, tokens = budget.spent(rows)
    cur, high = budget.effective(p)
    by = {}
    for e in rows:
        f = by.setdefault(str(e.get("feature")), {"feature": str(e.get("feature")), "usd": 0.0, "runs": 0, "tokens": 0})
        f["usd"] = round(f["usd"] + (e.get("usd") or 0), 4)
        f["runs"] += e.get("runs") or 0
        f["tokens"] += e.get("tokens") or 0
    top = sorted(by.values(), key=lambda f: -(f["usd"] + f["tokens"] / 1e6))[:4]
    return {"today_usd": usd, "subagent_tokens": tokens, "caps": cur, "halved": high,
            "subagents_paused": budget.subagent_pause(p), "top": top}


def _bench(p, events):
    """T-0228: the bench's case count, its newest run and the latest fm evolve generations, or None."""
    folder = os.path.join(p.dir, "bench")
    try:
        with open(os.path.join(folder, "cases.json"), encoding="utf-8") as f:
            cases = len(json.load(f))
    except (OSError, ValueError, TypeError):
        cases = 0
    last = None
    results = os.path.join(folder, "results")
    names = [n for n in (os.listdir(results) if os.path.isdir(results) else []) if n.endswith(".json")]
    if names:
        newest = max(names, key=lambda n: os.path.getmtime(os.path.join(results, n)))
        try:
            with open(os.path.join(results, newest), encoding="utf-8") as f:
                r = json.load(f)
            rows = r.get("cases") or []
            last = {"label": r.get("label") or newest[:-5], "passed": sum(1 for x in rows if x.get("pass")),
                    "total": len(rows), "cost_usd": round(sum(x.get("cost_usd") or 0 for x in rows), 2),
                    "at": r.get("at")}
            scores = [x["score"] for x in rows if isinstance(x.get("score"), (int, float))]
            if scores:  # T-0280: judged cases' mean score
                last["score"] = round(sum(scores) / len(scores), 1)
            if any("complaints" in x for x in rows):  # a soak: what the shadow user had to repeat
                last["repeats"] = sum(len(x.get("complaints") or []) for x in rows)
        except (OSError, ValueError, AttributeError, TypeError):
            pass
    evolve = [dict({k: (e.get("data") or {}).get(k) for k in ("kept", "branch", "target", "why")}, at=e.get("ts"))
              for e in reversed(events) if e.get("event") == "evolve"][:3]
    verdicts = [{"kind": e["event"][6:], "ok": (e.get("data") or {}).get("ok"),
                 "verdict": (e.get("data") or {}).get("verdict"), "at": e.get("ts")}
                for e in reversed(events) if e.get("event") in ("bench_duel", "bench_versions")][:2]
    return ({"cases": cases, "last": last, "evolve": evolve, "verdicts": verdicts}
            if cases or last or evolve or verdicts else None)


def _research_asks(events):
    """T-0228: the newest fm research ask notes with their claim counts."""
    return [{"name": d.get("name"), "claims": d.get("claims"), "verified": d.get("verified"),
             "not_found": d.get("not found"), "unchecked": d.get("unchecked"), "at": e.get("ts"),
             "conflicts": d.get("conflicts") or 0, "single": d.get("single") or 0}  # T-0277
            for e in reversed(events) if e.get("event") == "research" and "claims" in (d := e.get("data") or {})][:3]


_FRESH = {"at": 0, "ino": None, "head": "", "tail": "", "rows": []}


def _grep_kept(p, name, paths, needle, row, since):
    """row(e) for each JSON object on a line of `paths` holding `needle` (bytes), oldest first, where row keeps it (not
    None) and its first item (a ts or a day) is at or after `since`. T-3580: kept in cache/<name> with how far each file
    was read, so a run parses only what was appended since (each pane poll re-read ~90 MB of ledgers and events whole);
    rows older than `since` are dropped as they age out. A file with another inode, or other first or last-read bytes
    (fm tidy rolled or rotated it, even one that grew back past the offset), is read whole again; a half-written last
    line waits for the next run. Missing files and malformed lines are skipped."""
    cpath = os.path.join(p.dir, "cache", name)
    try:
        with open(cpath, encoding="utf-8") as f:
            kept = json.load(f)
    except (OSError, ValueError):
        kept = {}
    kept, out, now = kept if isinstance(kept, dict) else {}, [], {}
    for path in dict.fromkeys(paths):
        k = kept.get(path)
        k = k if isinstance(k, dict) and isinstance(k.get("at"), int) and isinstance(k.get("head"), str) \
            and isinstance(k.get("tail"), str) and isinstance(k.get("rows"), list) else _FRESH
        head, tail = k["head"].encode("latin-1"), k["tail"].encode("latin-1")
        try:
            with open(path, "rb") as f:
                ino = os.fstat(f.fileno()).st_ino
                same = k["at"] >= len(tail) and k["ino"] == ino and f.read(len(head)) == head
                if same:
                    f.seek(k["at"] - len(tail))
                    same = f.read(len(tail)) == tail
                if not same:
                    k, head, tail = _FRESH, b"", b""
                    f.seek(0)
                data = f.read()
        except OSError:
            continue
        end = data.rfind(b"\n") + 1
        rows = [r for r in k["rows"] if isinstance(r, list) and r and str(r[0] or "") >= since]
        for ln in data[:end].split(b"\n"):
            if needle in ln:
                try:
                    e = json.loads(ln.decode("utf-8", "replace"))
                except (ValueError, RecursionError):
                    continue
                r = row(e) if isinstance(e, dict) else None
                if r is not None and str(r[0] or "") >= since:
                    rows.append(r)
        now[path] = {"at": k["at"] + end, "ino": ino, "head": (head or data[:end][:64]).decode("latin-1"),
                     "tail": (tail + data[:end])[-64:].decode("latin-1"), "rows": rows}
        out += rows
    if now != kept:
        with contextlib.suppress(OSError):
            c.write_atomic(cpath, json.dumps(now))
    return out


def _skills(p):
    """T-0939: this week's Skill tool calls and fm lib card reads (newest first, a name once), uses per name, the map's
    suggested cards and those not read this week. Events without ts or a usable name are skipped; events.jsonl rotated
    by fm tidy (state/logs/) isn't read, so a week straddling a rotation is partial."""
    now = time.time()
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 7 * 86400))
    def call(e):
        mine = e.get("kind") == "tool" and e.get("tool") == "Skill" and e.get("project") == p.slug
        return [e.get("ts"), e.get("target")] if mine else None
    calls = _grep_kept(p, "skill-calls.json", [os.path.join(c.state_dir(), "events.jsonl")], b'"Skill"', call, since)
    ledgers = [os.path.join(p.dir, "archive", f"ledger-{m}.jsonl")
               for m in sorted({since[:7], time.strftime("%Y-%m", time.gmtime(now))})]
    reads = _grep_kept(p, "lib-reads.json", ledgers + [os.path.join(p.dir, "ledger.jsonl")], b'"lib"',
                       lambda e: [e.get("ts"), (e.get("data") or {}).get("cards")] if e.get("event") == "lib" else None,
                       since)
    uses = [(at, i, target, "skill") for i, (at, target) in enumerate(calls)]
    for i, (at, cards) in enumerate(reads):
        uses += [(at, i, n, "card") for n in (cards if isinstance(cards, list) else [])]
    # newest first by ts, then by file order within a second; stable, so one read's cards keep their order
    uses = sorted(((str(at), i, str(n), k) for at, i, n, k in uses if at and n and str(at) >= since),
                  key=lambda u: u[:2], reverse=True)
    counts, recent = {}, []
    for at, _, name, kind in uses:
        counts[name] = counts.get(name, 0) + 1
        if counts[name] == 1 and len(recent) < 8:
            recent.append({"name": c.plain(name)[:80], "kind": kind, "at": at})
    try:
        with open(os.path.join(p.dir, "map.json"), encoding="utf-8") as f:
            suggested = [str(x) for x in json.load(f).get("lib") or []]
    except (OSError, ValueError, AttributeError, TypeError):
        suggested = []
    return {"recent": recent, "counts": counts, "suggested": suggested,
            "unread": [x for x in suggested if x not in counts]}


def _ledger_lines(b):
    """T-0228: the active task's hypotheses, its oracle (example count, open questions) and batch members; T-0261: its
    inconclusive runs and the assumptions nobody checked."""
    hyps = [{"n": n, "status": status, "text": c.plain(text)[:200]} for n, status, text in b.hypotheses()]
    oracle = None
    sec = b.section("Oracle")
    if sec.strip():
        head, _, tail = sec.partition("\nAmbiguities")
        oracle = {"examples": sum(1 for x in head.splitlines() if x.startswith("- ")),
                  "ambiguities": [c.plain(x[2:])[:200] for x in tail.splitlines() if x.startswith("- ")]}
    return {"hypotheses": hyps, "oracle": oracle, "batch": list(b.meta.get("batch") or []),
            "inconclusive": len(b.inconclusive()), "unverified": [c.plain(x)[:200] for x in b.unverified()][:5]}


YOURS = ("user", "steer")  # T-3664: the sources that are the user's own words


def _lanes(p, briefs, sd):
    """T-3664: the open work in lanes, which the queue leaves out: building (a builder is at it), merging (finished,
    its commits wait on a merge) and closing (merged, its close still owed), newest first. The waiting rule is
    live.lanes()'s (T-3170); finished_lanes is cached per lane tip (T-3585), so a poll costs no git call."""
    held = {s["id"] for s in sd["lanes"]}
    finished = set(c.finished_lanes(p, briefs, quiet_min=0))
    out = {"building": [], "merging": [], "closing": []}
    from sampo import bus
    leased = {}  # T-3701: the files each lane's leases hold, for the pane's LANES picture (one small file read)
    for v in bus._leases().values():
        if isinstance(v, dict) and v.get("project") == p.slug and v.get("file") and v.get("task"):
            f = str(v["file"]).partition("::")[0]
            if f not in leased.setdefault(v["task"], []):
                leased[v["task"]].append(f)
    for b in sorted((b for b in briefs if b.id in held and b.status in ("active", "verifying")),
                    key=lambda b: b.meta.get("updated") or "", reverse=True):
        steps = b.steps()
        k = ("closing" if str(b.meta.get("lane_stop") or "").startswith("merged ")
             else "merging" if b.id in finished or b.meta.get("builder_returned") else "building")
        out[k].append({"id": b.id, "type": b.type, "tier": b.tier, "title": c.plain(b.title)[:160],
                       "steps_done": sum(s.done for s in steps), "steps_total": len(steps),
                       "since": b.meta.get("updated"), "files": sorted(c.plain(f)[:120] for f in leased.get(b.id, []))[:12]})
    return out


def _train(p, briefs):
    """T-3701: the merge train for the pane's MERGE picture, without git: whether one runs (its lock, or a land's, is
    held: a shared lock probe, nothing written), the lanes landed in the last day (the ledger's lane_merge events, and
    when) and the lanes parked in it (a conflict or a refusal on the brief)."""
    import fcntl
    from sampo import lanes as slanes
    running = False
    for name in ("train", "land"):
        try:
            with open(os.path.join(p.dir, name, ".lock"), "rb") as f:
                try:
                    fcntl.flock(f, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    fcntl.flock(f, fcntl.LOCK_UN)
                except BlockingIOError:
                    running = True
        except OSError:
            pass
    now = time.time()
    landed = [e for e in c.ledger_events(p, ["lane_merge"], c.iso(now - 86400)) if e.get("task")]
    parked = sum(1 for b in briefs if b.meta.get("lane") and (lambda k: k[0] in ("conflict", "refused") and now - k[1] < 86400)(slanes._stop(b)))
    return {"running": running, "landed": [c.plain(str(e["task"]))[:16] for e in landed[-12:]],
            "landed_at": [str(e.get("ts") or "")[:25] for e in landed[-12:]], "landed_24h": len(landed), "parked": parked,
            "branch": _branch(p.root)}


def _branch(root):
    """T-4008: the branch a train lands on, the main checkout's, read from .git/HEAD without git ('' when detached or
    unreadable): the pane named foreman/build in every repo."""
    try:
        with open(os.path.join(root, ".git", "HEAD"), encoding="utf-8") as f:
            head = f.read().strip()
    except OSError:  # a worktree's .git is a file: the pane runs from the main checkout
        return ""
    return c.plain(head.removeprefix("ref: refs/heads/"))[:60] if head.startswith("ref: refs/heads/") else ""


BOARD_CACHE_S = 30  # T-3701: how often the Board's slow parts are read again for the pane
COMMANDS_TAIL = 120  # T-4008: the events.jsonl lines the 2 s poll reads for the newest sampo commands
COMMANDS_MAX = 12
SAMPO_CMD = re.compile(r"(?:^|\s)(?:\S*/)?(?:sampo|fm)\s+(\S.*)")


def commands(p, tail=1500):
    """The Board's SAMPO part: this project's newest COMMANDS_MAX sampo commands (from the hook's tool events), oldest
    first: each its time, the command from sampo on (the rest of a chain or pipe dropped, redacted) and whether it
    failed. T-4008: here, not in sampo.board, so the 2 s poll reads the newest without importing board (live, urllib)."""
    out, terms = [], None
    for e in c.tail_jsonl(os.path.join(c.state_dir(), "events.jsonl"), tail):
        if not isinstance(e, dict) or e.get("project") != p.slug or e.get("tool") != "Bash" \
                or e.get("kind") not in ("tool", "tool_fail"):
            continue
        m = SAMPO_CMD.search(str(e.get("target") or "").split("\n", 1)[0])
        if m:
            terms = c.private_terms() if terms is None else terms
            rest = re.split(r"\s*(?:&&|\|\||;|\|)\s*", m.group(1), maxsplit=1)[0]
            cmd = c.redact_local(c.redact(c.plain(" ".join(("sampo " + rest).split()))), terms)
            out.append({"at": str(e.get("ts") or ""), "cmd": c.fit(cmd, 96),
                        "ok": e.get("kind") == "tool" and e.get("ok") is not False})
    return out[-COMMANDS_MAX:]
BOARD_COUNTS_S = 600  # T-3701: … and the card and moon totals, which read every card and moon


def _board_cache_path(p):
    return os.path.join(p.dir, "cache", "board.json")  # cache/: a write at the top would wake the mod's watch


def _board_extras(p):
    """T-3701: the live page Board's slow parts for the pane's LIBRARY, SAMPO, STATE and TESTS panels, as
    cache/board.json holds them. Older than BOARD_CACHE_S (or missing), a detached `fm ui --board-cache` reads them
    again (one at a time: a .building mark as old as that) and this poll answers with what the file holds: the read
    (callcards, card and moon counts, the test modules, live's import) never runs on the 2 s poll."""
    path = _board_cache_path(p)
    try:
        age = time.time() - os.path.getmtime(path)
        with open(path, encoding="utf-8") as f:
            kept = json.load(f)
    except (OSError, ValueError):
        age, kept = None, {}
    if isinstance(kept, dict):  # T-4008: a sampo command shows within a poll, not up to BOARD_CACHE_S later
        with contextlib.suppress(Exception):
            seen = kept.get("commands") or []
            rows = sorted(seen + [x for x in commands(p, COMMANDS_TAIL) if x not in seen], key=lambda x: str(x.get("at") or ""))
            kept["commands"] = rows[-COMMANDS_MAX:]
    if (age is None or not 0 <= age < BOARD_CACHE_S) and not os.environ.get("FOREMAN_NO_BACKGROUND"):
        mark = path + ".building"
        try:
            fresh = 0 <= time.time() - os.path.getmtime(mark) < BOARD_CACHE_S
        except OSError:
            fresh = False
        if not fresh:
            import subprocess
            with contextlib.suppress(OSError):
                os.makedirs(os.path.dirname(path), exist_ok=True)
                open(mark, "w").close()
                subprocess.Popen([os.path.join(c.PLUGIN_ROOT, "bin", "fm"), "ui", "--board-cache"], cwd=p.root,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
    return kept if isinstance(kept, dict) else {}


def board_cache(p):
    """T-3701: `fm ui --board-cache`: the Board's library (calling cards, kept per hour, card and moon counts), state
    tree and newest decision, test modules, newest sampo commands and the CPU by kind, written to cache/board.json
    for the pane."""
    from sampo import board, live
    with contextlib.suppress(OSError):
        os.nice(10)  # a background refresh: never ahead of the work it shows
    path, now = _board_cache_path(p), time.time()
    try:
        with open(path, encoding="utf-8") as f:
            was = json.load(f)
    except (OSError, ValueError):
        was = {}
    out = {"at": c.iso(now)}
    for name, fn in (("library", board.part_library), ("state", board.part_state), ("tests", board.part_tests),
                     ("commands", commands)):
        try:
            out[name] = fn(p)
        except Exception:  # one part down leaves the rest
            out[name] = None
    # the card and moon totals read every card and moon (~8 s here): kept BOARD_COUNTS_S, the rest every refresh
    kept = was.get("counts") if isinstance(was, dict) and isinstance(was.get("counts"), dict) else None
    if kept and isinstance(was.get("counts_at"), (int, float)) and 0 <= now - was["counts_at"] < BOARD_COUNTS_S:
        out["counts"], out["counts_at"] = dict(kept, calling=int((out["library"] or {}).get("calling") or kept.get("calling") or 0)), was["counts_at"]
    else:
        try:
            st = live.stats(p)
            out["counts"] = {"cards": int(st["cards"]["general"]), "ui": int(st["cards"]["ui"]), "moons": int(st["moons"]["candidates"]),
                             "calling": int((out["library"] or {}).get("calling") or 0)}
            out["counts_at"] = now
        except Exception:
            out["counts"] = kept
    try:
        out["cpu"] = _board_cpu(p, was.get("cpu") if isinstance(was, dict) else None)
    except Exception:
        out["cpu"] = None
    os.makedirs(os.path.dirname(path), exist_ok=True)
    c.write_atomic(path, json.dumps(out))


def _board_cpu(p, was, proc="/proc"):
    """T-3701: the machine's cores by kind (board.KINDS: tests, claude, models, hooks, other) for the pane's CPU panel,
    as the web Board's: two reads of /proc a second apart (board.cpu_tick), kept with the earlier refreshes' readings
    of the last 30 minutes, averaged a minute a bar. /proc unreadable: the last reading stands."""
    from sampo import board
    board.cpu_tick(proc=proc, every=0)
    time.sleep(1)
    board.cpu_tick(proc=proc, every=0)
    part = board.cpu_part(p)
    if not part["now"]:
        return was
    now, n = time.time(), 1 + len(board.KINDS)
    samples = [x for x in (was or {}).get("samples") or [] if isinstance(x, list) and len(x) == n and 0 <= now - x[0] < 1800]
    samples.append([int(now)] + [part["now"].get(k, 0.0) for k in board.KINDS])
    by = {}
    for x in samples:
        by.setdefault(int(x[0] // 60 * 60), []).append(x[1:])
    minutes = [[m] + [round(sum(col) / len(rows), 2) for col in zip(*rows)] for m, rows in sorted(by.items())]
    return dict(part, minutes=minutes[-30:], samples=samples[-120:])


def _flows(now=None):
    """T-3701: who called which model in the last five minutes, for the pane's MODELS picture: a link a caller and a
    model, its calls (n) and those of the last minute (hot). Read from the call log each models.complete call writes and
    Claude's spend log, as live._calls does, but here: importing live costs the 2 s poll ~35 ms (urllib). Machine-wide,
    as the web Board's."""
    now = time.time() if now is None else now
    since, hot = c.iso(now - 300), c.iso(now - 60)
    rows = [(e.get("ts"), e.get("from"), e.get("to")) for e in c.tail_jsonl(os.path.join(c.state_dir(), "calls.jsonl"), 400)]
    rows += [(e.get("ts"), e.get("feature"), "claude") for e in c.tail_jsonl(os.path.join(c.state_dir(), "spend.jsonl"), 200)]
    links = {}
    for at, a, to in rows:  # ponytail: the ledger's card and moon grow calls (live.FLOW_OF) are left out
        at = str(at or "")
        if at < since or not a or not to:
            continue
        a, to = c.plain(str(a))[:40], c.plain(str(to))[:40]
        link = links.setdefault((a, to), {"from": a, "to": to, "n": 0, "hot": 0})
        link["n"] += 1
        link["hot"] += at >= hot
    return sorted(links.values(), key=lambda x: (-x["n"], x["from"], x["to"]))[:16]


def _worktrees(p, briefs):
    """T-1023: the git worktrees beside the main checkout, for the pane's Agents graph: the branch, the task whose lane
    it is, the Claude Code subagent it was made for (its branch is worktree-agent-<id>), its age and the commits it
    holds that HEAD doesn't."""
    import re
    root = c.git_root(p.root)
    if not root:
        return []
    from sampo import lanes as slanes
    lanes = {os.path.realpath(b.meta["lane"]): b.id for b in briefs if b.meta.get("lane")}
    open_ids = {b.id for b in briefs if b.meta.get("lane") and b.status not in c.CLOSED}  # T-1152
    gone = set(slanes.stopped(p, briefs))  # T-1119: builders whose Claude Code session ended
    out = []
    for block in c._git(root, "worktree", "list", "--porcelain").strip().split("\n\n")[1:]:
        f = dict(line.split(" ", 1) for line in block.splitlines() if " " in line)
        path, branch = f.get("worktree"), (f.get("branch") or "").removeprefix("refs/heads/")
        if not path:
            continue
        try:  # the .git file a worktree gets once, when it's made
            age = int(time.time() - os.stat(os.path.join(path, ".git")).st_mtime)
        except OSError:
            age = None
        m = re.fullmatch(r"worktree-agent-([0-9a-f]+)", branch)
        task = lanes.get(os.path.realpath(path))
        out.append({"path": path, "branch": branch, "task": task, "agent": m.group(1) if m else None,
                    "age_s": age, "ahead": None, "stopped": task in gone, "open": task in open_ids})
    out = sorted(out, key=lambda w: w["age_s"] if w["age_s"] is not None else 1 << 40)[:12]  # newest first, then capped
    for w in out:  # one git call each, only for those listed; a detached one's merge state stays unknown (None)
        n = c._git(root, "rev-list", "--count", f"HEAD..refs/heads/{w['branch']}").strip() if w["branch"] else ""
        w["ahead"] = int(n) if n.isdigit() else None
    return out


def view(p):
    """The `fm ui --json` view model (v1; its TS twin is mods/foreman-ui/types/index.d.ts): one snapshot for a
    surface to render, built from gather() so it shows what fm watch shows."""
    from sampo import doctor, selfcheck
    d = gather(p)
    sd, act = d["sd"], d["active"]
    meta = c.read_meta(p)
    autonomy = sd["autonomy"]
    pending = c.pending_tasks(meta)
    briefs = d["briefs"]
    by_id = {b.id: b for b in briefs}
    active = None
    if act:
        changed = c.last_change(p, act.id)
        prog = c.audit_progress(act, changed)
        active = dict(c.brief_detail(act), stage=c.stage(act, autonomy, changed), stages=list(c.STAGES),
                      audits={"done": prog["done"], "need": prog["required"]},
                      blockers=act.done_blockers(changed)[:6], **_ledger_lines(act))
    events = c.ledger_tail(p, 5000)
    if active:
        focused = next((e.get("ts") for e in events if e.get("event") == "focus" and e.get("task") == act.id), None)
        active["on_task_s"] = round((c.age_days(focused) or 0) * 86400) if focused else None
        asked = " ".join(x.lstrip("> ").strip() for x in (act.section("Raw request") or "").splitlines()).strip()
        active["words"] = c.plain(asked or act.title)[:240]  # T-3701: THE LOOP's "you asked", unquoted
        active["splits"] = splits(events, act)
        active["children"] = [{"id": k.id, "title": c.plain(k.title)[:120], "status": k.status,  # T-0711: the tree
                               "steps_done": sum(s.done for s in k.steps()), "steps_total": len(k.steps())}
                              for k in briefs if k.meta.get("parent") == act.id]

    def item(s):
        b = by_id.get(s["id"])
        waits = c.waits_on_user(b, pending, autonomy) if b else None
        out = {"id": s["id"], "type": s["type"], "tier": s["tier"], "title": s["title"], "status": s["status"],
               "source": s.get("source"), "waits": waits, "steps_done": s["steps_done"], "steps_total": s["steps_total"],
               "age_days": round(c.age_days(s.get("created")) or 0, 1)}
        if waits == "plan approval":  # a yes is given where what it approves is shown
            d = c.brief_detail(b)
            out["plan"] = {"interpretation": c.plain(b.section("Interpretation").strip())[:600],
                           "approach": c.plain(b.section("Approach (options → choice → why)").strip())[:600],
                           "steps": d["steps"], "criteria": d["criteria"]}
        return out

    closed = sorted((b for b in briefs if b.status in c.CLOSED), key=lambda b: b.meta.get("updated") or "",
                    reverse=True)[:5]
    today = time.strftime("%Y-%m-%d", time.gmtime())  # closed today by the ledger, not "saved today" (any later edit)
    done_ids = {b.id for b in briefs if b.status == "done"}
    days = [time.strftime("%Y-%m-%d", time.gmtime(time.time() - 86400 * n)) for n in range(13, -1, -1)]
    closes = _closes_since(p, days[0])
    # T-0909: the pane's day bars, today last; a task counts once a day, and only while it is still done
    done_days = [len({t for dt, t in closes if dt == x} & done_ids) for x in days]
    today_done = done_days[-1] if days[-1] == today else len({t for dt, t in closes if dt == today} & done_ids)
    lat = [ms for vals in d["latency"].values() for ms in vals]
    bs = brainstorm(p)
    resume = meta.get("resume_after_reload") or None
    resume_age = c.age_days(resume.get("at")) if isinstance(resume, dict) else None
    # T-3580: read once each, for the view and the band's signals
    d.update(proof=_guarded(lambda: c.unproven_summary(p, c.PROOF_DAYS)), hook_errors=len(doctor.recent_hook_errors()))
    return _shown({
        "v": 1, "project": p.slug, "root": p.root,
        "mode": {"autonomy": autonomy, "drive": bool(sd["drive"]), "sensitive": bool(sd["sensitive"]),
                 "trust": bool(c.trusted()), "risk_off": _guarded(lambda: __import__("sampo.risk").risk.off_line(p)),
                 "standing": sorted(meta.get("standing") or {})},
        "active": active,
        "next": c.plain(c.next_for(p, briefs)[2]),
        "genesis": sd.get("genesis"),  # T-2118: the tower's next action (fmgenesis.health)
        "queue": [item(s) for s in sd["queue"][:20]], "queue_total": len(sd["queue"]),  # T-3664: the list is capped
        # T-3664: the user's own asks first (intake rank kept within each part): by rank alone 154 discovered items
        # sat above them; lanes: the work the queue leaves out
        "inbox": [item(s) for s in sorted(sd["inbox"], key=lambda s: s.get("source") not in YOURS)[:10]],
        "inbox_total": len(sd["inbox"]), "inbox_yours": sum(s.get("source") in YOURS for s in sd["inbox"]),
        "lanes": _guarded(lambda: _lanes(p, briefs, sd)) or {"building": [], "merging": [], "closing": []},
        "cpu": _guarded(lambda: _cpu_series(p)),  # T-3701: the pane's CPU panel, from state/cpu.json's samples
        "train": _guarded(lambda: _train(p, briefs)),  # T-3701: the pane's MERGE picture
        "flows": _guarded(_flows) or [],  # T-3701: the pane's MODELS picture
        "board": _guarded(lambda: _board_extras(p)) or {},  # T-3701: LIBRARY, SAMPO, STATE, TESTS, from cache/board.json
        "blocked_total": len(sd["blocked"]),  # T-1026: the pane's work bar
        "approvals": [{"task": a.get("task"), "allow": list(a.get("allow") or []), "why": c.plain(a.get("why") or "")}
                      for a in meta.get("pending_approvals") or [] if isinstance(a, dict) and a.get("task")],
        "closed": [{"id": b.id, "status": b.status, "title": c.plain(b.title)[:120],  # T-0473: the close-out card
                    "grade": b.meta.get("verified") if b.status == "done" else None,
                    "lenses": sorted({lens for lens, _, _ in b.audits()}) if b.status == "done" else []}
                   for b in closed],
        "signals": _guarded(lambda: _signals(sd, d, p)) or [],  # T-0472
        "fleet": _guarded(_fleet) or [],  # T-0711
        "models": {"downgraded": _guarded(__import__("sampo.modelup").modelup.downgraded) or []},  # T-3742
        "today_done": today_done,
        "learned": _guarded(lambda: _learned_line(p)),  # T-1045
        "look": _guarded(lambda: __import__("sampo.taste").taste.roll_line()),  # T-1672: the look of the day, once taste exists
        "done_days": done_days,
        "trust_file": c.trust_path(),  # where /fm-trust on writes (the mod, never a tool call)
        "risk_files": [os.path.join(c.state_dir(), "risk_off.json"), os.path.join(p.dir, "risk_off.json")],  # T-3752: /fm-risk off
        # T-0145: a driven turn ended so a session's mod could reload; that mod starts the next turn (fresh ones only)
        "resume_after_reload": resume if resume_age is not None and resume_age * 1440 < 10 else None,
        "typical": typical(events),
        "brainstorm": bs,
        "loop": _guarded(lambda: __import__("sampo.loop").loop.view(p)),  # T-1057: the forever loop's round
        "worktrees": _guarded(lambda: _worktrees(p, briefs)) or [],  # T-1023
        "budget": _guarded(lambda: _budget(p)),  # T-0228
        "unproven": _guarded(lambda: _unproven(d["proof"])) or {"n": 0, "top": None},  # T-2395: the band's proof line
        "bench": _guarded(lambda: _bench(p, events)),
        "research": _guarded(lambda: _research_asks(events)) or [],
        "vetoes": _guarded(lambda: [c.plain(str(v.get("said", "")))[:200] for v in c.vetoes(p)][::-1][:3]) or [],  # T-0251
        "revisit": _guarded(lambda: [{"date": dt, "decision": c.plain(x)[:200], "why": c.plain(w)}  # T-0247
                                     for dt, x, w in c.fired_decisions(p)]) or [],
        "recent": d["recent"],
        "history": _guarded(lambda: _history(events, by_id)) or [],  # T-1026
        "skills": _guarded(lambda: _skills(p))
        or {"recent": [], "counts": {}, "suggested": [], "unread": []},  # T-0939
        "health": {"hook_p95_ms": round(_pct(lat, 0.95)) if lat else None, "guard_blocks": len(d["guard"]),
                   "hook_errors": d["hook_errors"], "paused_hooks": doctor.paused_hooks()},
        "watch": _watch(p)
        + ([os.path.join(p.dir, "research", bs["name"])] if bs and bs["running"] else []),  # each lens answer moves it
        "watch_now": _watch_now(p, act),  # T-3580
        "latency": [round(ms) for ms in d["series"]],
        "palette_custom": _guarded(c.pane_palette) or {},  # T-1717: fm design tokens --to pane --save
        "weekly_lean": _guarded(lambda: _weekly_lean(p)) is not False,  # T-3049: false: the pane's economy mode ignores weekly usage here
        "alerts": _guarded(lambda: selfcheck.alerts(p)) or [],  # T-4118: the day's tells, toasted once
        "checks": d["checks"] and {"at": d["checks"].get("ts"), "results": [
            {"cmd": c.plain(str(r.get("cmd")))[:200], "exit": r.get("exit"), "s": r.get("s"), "note": r.get("note")}
            for r in (d["checks"].get("data") or {}).get("results") or []]},
    })


def _closes_since(p, first):
    """T-0909: {(day, task)} for every task_done from `first` (YYYY-MM-DD) on: the whole ledger, not a tail (a busy
    project's 5000 events spanned six days), and the month `first` falls in from the archive fm tidy rolls past months
    into. Only task_done lines are parsed, and T-3580 only those appended since the last run."""
    rows = _grep_kept(p, "closes.json", [os.path.join(p.dir, "archive", f"ledger-{first[:7]}.jsonl"),
                                         os.path.join(p.dir, "ledger.jsonl")], b'"task_done"',
                      lambda e: [str(e.get("ts") or "")[:10], e.get("task")] if e.get("event") == "task_done" else None,
                      first)
    return {(dt, t) for dt, t in rows}


def _fleet():
    from sampo import bus
    return bus.fleet()


def _signals(sd, d, p=None):
    """T-0472: what the band shows as one quiet glyph, only when something is amber or red: a gate failing now, usage
    ahead of its pace, blocked tasks, hook errors, a finder your drops muted (T-2127). Calm: []."""
    from sampo import budget
    from sampo import find
    out = []
    for r in ((d.get("checks") or {}).get("data") or {}).get("results") or []:
        if r.get("exit"):
            out.append({"level": "red", "text": f"gate failing: {c.plain(str(r.get('cmd')))[:80]}"})
    from sampo import modelup  # T-3742: a session the refusal fallback dropped to a lower model
    for r in modelup.downgraded():
        out.append({"level": "amber", "text": c.plain(f"session {r['sid'][:8]} dropped {r['from']} -> {r['to']} (refusal fallback); {r['way_back']}")[:200]})
    pace = budget.degrade()
    if pace:
        out.append({"level": "amber", "text": f"usage ahead of pace ({c.plain(str(pace))[:80]})"})
    if sd.get("blocked"):
        out.append({"level": "amber", "text": f"{len(sd['blocked'])} blocked"})
    g = sd.get("genesis") or {}
    if g.get("kind") in ("blocked", "stopped", "fatal"):  # T-2118: the tower can't go on by itself
        out.append({"level": "red" if g["kind"] == "fatal" else "amber",
                    "text": f"genesis gen {g['gen']} {g['kind']}: {c.plain(g['action'])[:120]}"})
    from sampo import mandate
    gv = (mandate.fresh_verdict(p) if p else None) or {}  # T-2141: the stored verdict only, no check runs here
    if gv.get("verdict") == "stopped":
        out.append({"level": "amber", "text": c.plain(f"goal stopped: {c.fit(str((gv.get('causes') or ['no cause stored'])[0]), 100)}")})
    from sampo import grants as sgrants  # T-2378: install grants waiting on the user (a brief scan, kept like the others)
    grants = _kept(p, "grants-line.json", lambda: sgrants.line(p)) if p else ""
    if grants:
        out.append({"level": "amber", "text": c.plain(grants)[:200]})
    proof = d.get("proof")  # T-2395: keyed cache, no TTL; T-3580: view read it
    if proof and proof.get("n"):
        out.append({"level": "amber", "text": c.plain(c.unproven_line(proof["n"], proof.get("counts")))[:200]})
    from sampo import triggers  # T-2364: what ran or waits by itself (lanes and books read), kept like the others
    sr = _kept(p, "self-running-line.json", lambda: triggers.self_running(p)["line"]) if p else ""
    if sr:
        out.append({"level": "amber", "text": c.plain(sr)[:200]})
    from sampo import outcomes  # T-2344: parked doors and a Brier worse than the base rate; outcomes() is slow, so kept
    judged = _kept(p, "judgment-line.json", lambda: outcomes.band_line(p), SLOW_TTL_S) if p else ""
    if judged:
        out.append({"level": "amber", "text": c.plain(judged)[:200]})
    errors = d.get("hook_errors")
    if errors:
        out.append({"level": "amber", "text": f"{errors} hook error(s)"})
    # precision() reads the archive and, once a lead is done, git log for reverts: ~180 ms, kept like the learned line
    muted = _kept(p, "find-muted.json", lambda: sorted(k for k, v in find.precision(p).items() if v < find.FLOOR),
                  SLOW_TTL_S) if p else []
    if muted:
        out.append({"level": "amber", "text": f"fm find muted ({', '.join(muted)}): you dropped most of its leads"})
    return out


def cmd_ui(args):
    """fm ui --json: the view model for the foreman-ui mod and any other surface; {"v": 1, "project": null} outside a
    project (never creates one)."""
    from sampo import cli
    if getattr(args, "demo", False):  # T-0962: a canned view (ui-demo.json) for building a surface without a project
        with open(os.path.join(os.path.dirname(os.path.abspath(os.path.dirname(__file__))), "ui-demo.json"), encoding="utf-8") as f:
            return print(json.dumps(json.load(f)))
    try:
        p = cli.resolve(args, create=False)
    except cli.UsageError:
        p = None
    if getattr(args, "board_cache", False):  # T-3701: the detached refresh _board_extras starts
        return board_cache(p) if p else None
    if getattr(args, "follow", False) and p:
        return _follow(lambda: view(p), lambda v: v["watch_now"] + v["watch"], args.interval)
    if getattr(args, "json", False):
        if not p:
            return print(json.dumps({"v": 1, "project": None}))
        with _git_kept(p):
            text = _cached_view(p)
        return print(text)
    v = view(p) if p else {"v": 1, "project": None}
    a = v.get("active")
    print(f"{v['project'] or 'not a Sampo project'}" + (f" · {a['id']} {a['stage']}" if a else "")
          + (f"\nNext: {v['next']}" if v.get("next") else ""))


UI_CACHE_S = 10  # T-3542: how long a kept view may stand (FOREMAN_UI_CACHE_S; 0: never kept)


def _watch(p):
    """The busy paths a surface stats to know the view moved (a running brainstorm's folder joins them in view): they
    move with every tool call, so the mod reruns fm ui for them at most every 10 s (T-3580). Not the state folder: every
    derived file a reader rewrites there moved it (risk.json, genesis-step.json: several a second while lanes run)."""
    return [os.path.join(p.dir, "tasks"), os.path.join(p.dir, "ledger.jsonl")]


def _watch_now(p, act=None):
    """T-3580: what the user acts on, which the mod reruns fm ui for at once: meta.json (drive, autonomy, a pending
    approval, the pause flag), asks.json, the trust record, the PANIC flag and the active task's brief (its focus moving
    off, a step done)."""
    return [os.path.join(p.dir, "meta.json"), os.path.join(p.dir, "asks.json"), c.trust_path(),
            os.path.join(c.state_dir(), "risk_off.json"), os.path.join(p.dir, "risk_off.json"),
            os.path.join(c.state_dir(), "PANIC")] + ([act.path] if act and act.path else [])


def _ui_key(p, watch):
    """T-3542: what a kept view stands on: the checkout, its lane and HEAD, and (inode, mtime, size) of each watched path
    (T-3580: what the user acts on, then the ledger, the tasks folder, a running brainstorm's). Derived files in the
    state folder follow the keyed inputs, and the rest is bounded by the age."""
    out = [p.root, p.lane, c._git(p.root, "rev-parse", "HEAD").strip()]
    for path in watch:
        try:
            st = os.stat(path)
            out.append([path, st.st_ino, st.st_mtime_ns, st.st_size])
        except OSError:
            out.append([path])
    return out


def _cached_view(p):
    """T-3542: json.dumps(view(p)), kept in cache/ui.json while its key holds and for at most FOREMAN_UI_CACHE_S
    seconds: the mod's poll runs fm ui after every tool call, and a rebuild costs ~0.7 s CPU against ~0.1 s for a kept
    one. The age bounds what the key leaves out: clocks, events.jsonl (hook timings, skills), the bus, lane commits.
    The key is taken before the build, so a change while it builds rebuilds next time; cache/ is a subfolder because a
    write at the top would move the folder the mod watches and wake its next poll."""
    try:
        ttl = float(os.environ.get("FOREMAN_UI_CACHE_S", UI_CACHE_S))
    except ValueError:
        ttl = UI_CACHE_S
    # ponytail: one file per project (root and lane are in the key): a pane in a lane's own session and the main one
    # would take turns rebuilding; a file per lane if that use shows up
    path, kept = os.path.join(p.dir, "cache", "ui.json"), {}
    if ttl > 0:
        try:
            with open(path, encoding="utf-8") as f:
                kept = json.load(f)
        except (OSError, ValueError):
            pass
    kept = kept if isinstance(kept, dict) else {}
    watch = kept.get("watch")
    watch = watch if isinstance(watch, list) and all(isinstance(w, str) for w in watch) else _watch_now(p) + _watch(p)
    at, key = time.time(), _ui_key(p, watch)
    if isinstance(kept.get("at"), (int, float)) and 0 <= at - kept["at"] < ttl and kept.get("key") == key \
            and isinstance(kept.get("text"), str) and kept["text"].startswith('{"'):
        return '{"cached": true, ' + kept["text"][1:]  # the mod polls again after a move that a kept view answered
    v = view(p)
    text = json.dumps(v)
    if ttl > 0:  # a new watch list (a brainstorm started or ended, a focus) is keyed from the next build on
        now = v["watch_now"] + v["watch"]
        with contextlib.suppress(OSError):
            c.write_atomic(path, json.dumps({"at": at, "key": key if now == watch else None, "watch": now,
                                             "text": text}))
    return text


@contextlib.contextmanager
def _git_kept(p):
    """T-3580: core._git's ref-only answers for this run, kept in cache/git.json while the stamp of what they stand on
    (core.git_stamp) holds: the pane's run started ~20 git processes, nearly all asking what the last run asked."""
    stamp, path, kept = c.git_stamp(p.root), os.path.join(p.dir, "cache", "git.json"), {}
    if stamp:
        try:
            with open(path, encoding="utf-8") as f:
                got = json.load(f)
            if isinstance(got, dict) and got.get("stamp") == stamp and isinstance(got.get("answers"), dict):
                kept = got["answers"]
        except (OSError, ValueError):
            pass
    asked = dict(kept)
    c.GIT_KEPT = kept if stamp else None
    try:
        yield
    finally:
        c.GIT_KEPT = None
    if stamp and kept != asked:  # stamped before the run: a ref that moved during it is asked again next time
        with contextlib.suppress(OSError):
            c.write_atomic(path, json.dumps({"stamp": stamp, "answers": kept}))


def _follow(build, paths, interval, refresh=30.0):
    """T-0322: print build() as a JSON line now and again whenever one of paths(view) changes (or every `refresh`
    seconds, for ages and clocks), skipping repeats; ends quietly when the reader goes away (the app, an ssh pipe)."""
    def sig(v):
        out = []
        for path in paths(v):
            try:
                out.append(os.stat(path).st_mtime_ns)
            except OSError:
                out.append(None)
        return out
    last = None
    try:
        v = build()  # the first build only names the paths to watch
        while True:
            seen, at = sig(v), time.time()
            v = build()  # T-1882: built after the snapshot, so a change while it builds or prints is caught next round
            line = json.dumps(v)
            if line != last:
                print(line, flush=True)
                last = line
            while sig(v) == seen and time.time() - at < refresh:
                time.sleep(interval)
    except (BrokenPipeError, KeyboardInterrupt):
        try:
            sys.stdout = open(os.devnull, "w")  # no second BrokenPipeError when Python flushes at exit
        except OSError:
            pass


def projects():
    """T-0322: every Sampo project on this device with a light summary, most recently active first."""
    from sampo import eco
    out, changes = [], eco.changelog()
    for p, meta in c.all_projects():
        try:
            sd = c.state_dict(p)
        except Exception as e:  # one damaged project never hides the others
            out.append({"project": p.slug, "root": p.root, "error": c.fit(str(e), 200)})
            continue
        a = sd["active"]
        try:
            updated = os.path.getmtime(os.path.join(p.dir, "ledger.jsonl"))
        except OSError:
            updated = None
        out.append({"project": p.slug, "root": p.root, "exists": os.path.isdir(p.root),
                    "active": a and {k: a.get(k) for k in ("id", "type", "tier", "title", "stage", "steps_done",
                                                            "steps_total")},
                    "queue": len(sd["queue"]), "inbox": len(sd["inbox"]), "blocked": len(sd["blocked"]),
                    "waits": len(sd["pending"]) + len(sd["asks"]), "drive": bool(sd["drive"]),
                    "autonomy": sd["autonomy"], "sensitive": sd["sensitive"], "updated": updated,
                    "runner": meta.get("code_runner"),  # T-1345: the app's home card names it
                    # T-0463: the Foreman its last session ran (and on which machine), and the released fixes it lacks
                    "foreman": meta.get("foreman"), "lacks": eco.lacks((meta.get("foreman") or {}).get("version"),
                                                                         changes), "next": _next(sd)})
    return sorted(out, key=lambda r: r.get("updated") or 0, reverse=True)


def _next(sd):
    """T-0890: (rank, line) of a project's next action: waiting on the user 0, active 1, queued 2, inbox 3; None idle."""
    waits = len(sd["pending"]) + len(sd["asks"])
    if waits:
        return 0, f"waiting on you: {waits} ask(s) or approval(s)"
    if a := sd["active"]:
        return 1, f"{a['id']} {a['title']} ({a.get('stage')}, step {a.get('steps_done', 0)}/{a.get('steps_total', 0)})"
    for rank, key, how in ((2, "queue", "queued"), (3, "inbox", "inbox, to plan")):
        if sd[key]:
            b = sd[key][0]
            return rank, f"{b['id']} {b['title']} ({how}{', urgent' if b.get('priority') == 'urgent' else ''})"
    return None


def _usage():
    """T-0346: this device's plan usage (5-hour %, weekly %, share of the week gone) from the newest statusline
    snapshot, for the app's sidebar; {} when no session has reported one. --follow refreshes it every 30 s, not on
    every statusline write."""
    try:
        from sampo import budget
        return budget.rate_limits()
    except Exception:
        return {}


def _project_paths(_v):
    root = c.projects_dir()
    paths = [root]
    for slug in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        d = os.path.join(root, slug)
        paths += [os.path.join(d, "meta.json"), os.path.join(d, "ledger.jsonl"), os.path.join(d, "tasks")]
    return paths


def cmd_projects(args):
    """fm projects [--json] [--follow]: the device-wide list the desktop app's sidebar shows (T-0322)."""
    if getattr(args, "sweep", None):  # T-2461: fm sweep folded in here
        from sampo import eco
        args.fix = args.sweep
        return eco.cmd_sweep(args)
    build = lambda: {"v": 1, "projects": projects(), "usage": _usage()}  # noqa: E731
    if args.follow:
        return _follow(build, _project_paths, args.interval)
    v = build()
    if args.json:
        print(json.dumps(v))
        return
    if args.next:  # T-0890: one list across projects; sorted() keeps most recently active first within a rank
        for i, r in enumerate(sorted((r for r in v["projects"] if r.get("next")), key=lambda r: r["next"][0]), 1):
            print(f"{i}. {r['project']}: {r['next'][1]}")
        return
    for r in v["projects"]:
        a, ran, lacks = r.get("active"), r.get("foreman") or {}, r.get("lacks") or []
        print(f"{r['project']}  {r['root']}" + (f"  · {a['id']} {a['stage']}" if a else "")
              + f"  · queue {r.get('queue', 0)} · inbox {r.get('inbox', 0)}"
              + (f"  · Sampo {ran['version']}" + (f" on {ran['machine']}" if ran.get("machine") else "") if
                 ran.get("version") else "")
              + (f", lacks {len(lacks)} fixes ({', '.join(lacks[:5])}{' …' if len(lacks) > 5 else ''})" if lacks else ""))
