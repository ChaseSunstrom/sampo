"""Sampo's own friction since the last self-improvement pass (T-0125): the digest one read-only subagent turns into
fixes for the self-inbox, and the trigger fm next uses. A pass is recursive: the next digest reports what became of
the self items, so a fix that didn't remove its friction shows up again. Reads the global events and every project's
ledger; writes only this project's meta (rsi_at, rsi_every), under its lock."""
import collections
import glob
import json
import os
import re
import statistics
import time

from sampo import core as c

WINDOW_DAYS = 7  # the first pass looks back this far
MAX_LINES = 6  # per section
MIN_GAP_H = 2  # T-0154: a pass needs time to gather friction (pass 2 came due 13 min after pass 1 with one line)


def _start(meta):
    return meta.get("rsi_at") or c.iso(time.time() - WINDOW_DAYS * 86400)


def _ledgers(start, here=None):
    for p, _ in c.all_projects():
        if here and p.slug != here.slug and c.read_meta(p).get("sensitive"):
            continue  # T-0435: a sensitive project's ledger stays out of another project's digest
        for e in c.tail_jsonl(os.path.join(p.dir, "ledger.jsonl"), 6000):
            if (e.get("ts") or "") > start:
                yield p, e


def _transcript_commands(sids):
    """{(session id, command as the guard event shows it): (command, cwd)} from those sessions' transcripts."""
    base = os.path.join(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude"), "projects")
    out = {}
    for sid in sids:
        for path in glob.glob(os.path.join(base, "*", f"{sid}.jsonl")) if re.fullmatch(r"[\w-]+", sid) else []:
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        if '"Bash"' not in line:
                            continue
                        try:
                            entry = json.loads(line)
                        except ValueError:
                            continue
                        content = (entry.get("message") or {}).get("content") if isinstance(entry, dict) else None
                        for b in content if isinstance(content, list) else []:
                            cmd = (b.get("input") or {}).get("command") if isinstance(b, dict) and \
                                b.get("type") == "tool_use" and b.get("name") == "Bash" else None
                            if isinstance(cmd, str):
                                flat = c.redact(cmd[:CMD_CAP].replace("\n", " "))
                                out.setdefault((sid, flat), (cmd, entry.get("cwd") or ""))
            except OSError:
                continue
    return out


CMD_CAP = 20000  # chars of a command read before redacting and parsing it


def _lookup(found, e):
    """The (command, cwd) a guard event ran: the hook logs ~160 chars around the judged target (T-0423, so "…" at
    either end), so match by containment of that text in the transcript's whitespace-flattened command."""
    logged = str(e.get("cmd") or "").strip("…")
    if not logged:
        return None
    return next((v for (sid, flat), v in found.items() if sid == e.get("session_id") and logged in flat), None)


def _example(e, found, allowed=False):
    """T-2280: the blocked command with data-heredoc bodies dropped (the full text from the transcript when found, as
    the logged window can start past the heredoc), fitted, and the rule's hint after it (not when today's guard
    allows it). A fm edit body is the judged target, so it stays."""
    import fmguard
    cmd = (_lookup(found, e) or (str(e["cmd"]),))[0][:CMD_CAP]
    if not re.search(r"^\s*(?:fm|sampo) edit\b", cmd, re.M):
        cmd = fmguard._drop_data_heredocs(cmd)
    shown = c.fit(c.plain(c.redact(cmd).replace("\n", " ")), 160)
    hint = "" if allowed else fmguard._hint(str(e.get("target") or ""), e.get("category") or "").strip()
    return f" — e.g. `{shown}`", (f" — hint: {c.fit(hint, 200)}" if hint else "")


def _recheck(events, found=None):
    """{event index: what today's guard says of the command it blocked then} (T-0187): a fix that removed the friction
    shows as 'allows it'; a command no transcript holds any more gets no word."""
    from sampo import replay
    bash = [(i, e) for i, e in enumerate(events) if e.get("tool") == "Bash" and e.get("cmd") and e.get("session_id")]
    found = found if found is not None else _transcript_commands({e["session_id"] for _, e in bash})
    runs = {i: r for i, e in bash if (r := _lookup(found, e))}
    now = replay.verdicts(runs.values())
    say = lambda v: "today's guard allows it" if v == "allow" else \
        "today's guard is unable to judge it (it raised)" if replay.unable(v) else "still blocks"  # T-1517
    return {i: say(now.get(replay._key(*run))) for i, run in runs.items()}


def _rate(n, calls):
    return f"{n * 100 / calls:.1f}".rstrip("0").rstrip(".")


STAGES = {"task_plan": "plan", "focus": "focus", "step_done": "step", "evidence": "evidence", "check_run": "check",
          "audit": "audit", "finish": "finish", "task_done": "done", "task_block": "block"}
SMOOTH = 3  # stage events or fewer for a finished task: too smooth to have been checked much


def _failed(e):
    d = e.get("data") or {}
    if e.get("event") == "evidence":
        return bool(c.result_exit(d.get("result")))  # "✗ exit 1 · …": a failed run (T-0750)
    return e.get("event") == "check_run" and any((r or {}).get("exit") for r in d.get("results") or [])


def _mine(ledger, label):
    """T-0658: process mining over the window's ledgers — the paths tasks really take (stage sequences, repeats
    collapsed), finished tasks that went smoothly but closed with a weak grade, and how many failed runs came before
    each block (where work gives up)."""
    runs = collections.defaultdict(list)
    for lp, e in ledger:
        if e.get("task") and e.get("event") in STAGES:
            runs[(lp.slug, e["task"])].append((lp, e))
    paths, smooth, gave_up = collections.Counter(), [], collections.defaultdict(list)
    for (_, tid), pairs in runs.items():
        lp, evs = pairs[0][0], [e for _, e in pairs]
        seq = [STAGES[e["event"]] for e in evs]
        seq = [s for i, s in enumerate(seq) if not i or s != seq[i - 1]]
        if seq[-1] in ("done", "block"):
            paths[" → ".join(seq)] += 1
        end = evs[-1]
        if end["event"] == "task_done" and len(evs) <= SMOOTH \
                and (end.get("data") or {}).get("verified") not in ("strong", "ok"):
            smooth.append(f"{label(lp)}{tid} ({len(evs)} stage events, graded "
                          f"{(end.get('data') or {}).get('verified') or 'ungraded'})")
        if end["event"] == "task_block":
            why = c.fit(c.plain(str((end.get("data") or {}).get("reason") or "")), 80)  # T-2295
            gave_up[sum(_failed(e) for e in evs)].append(f"{label(lp)}{tid}" + (f" ({why})" if why else ""))
    return {
        "common paths (stage sequences of tasks that finished or blocked)": [
            f"{s} ×{n}" for s, n in paths.most_common(3)],
        "smooth but unverified (few stage events, weak grade: check these held)": smooth[:MAX_LINES],
        "give-up points (failed runs before each block)": [
            f"after {n} failed run(s): {', '.join(t[:5])}" for n, t in sorted(gave_up.items())][:MAX_LINES]}  # not a list: five names per row


_ANSWERS_NO = {"grep", "egrep", "rg", "test", "[", "diff", "cmp"}  # T-2733: their exit 1 is a 'no', not a failure


def _prog(e):
    """The program a Bash call ran first, past any leading `cd DIR &&` and VAR=value words."""
    cmd = re.sub(r"^(?:\s*cd\s+\S+\s*&&\s*)+", "", str(e.get("target") or ""))
    m = re.match(r"\s*(?:\w+=\S*\s+)*([\w./\[-]+)", cmd)
    return os.path.basename(m.group(1)) if m else ""


def _tool(e):
    """T-2733: 'Bash fm' rather than 'Bash', so fm's and python3's failures aren't one bucket with grep's no-match."""
    return f"Bash {_prog(e)}".strip() if e.get("tool") == "Bash" else e.get("tool")


def _a_no(e):
    return e.get("tool") == "Bash" and _prog(e) in _ANSWERS_NO and str(e.get("error") or "").startswith("Exit code 1")


def digest(p, recheck=True):
    """{section: [lines]} of what went wrong or slow since the last pass, newest kinds first, each line bounded, and the
    window's counts (kept at --mark for the next digest's trend)."""
    meta = c.read_meta(p)
    start = _start(meta)
    events = [e for e in c.tail_jsonl(os.path.join(c.state_dir(), "events.jsonl"), 30000) if (e.get("ts") or "") > start]
    ledger = list(_ledgers(start, p))
    label = lambda lp: "" if lp.slug == p.slug else f"[{lp.slug}] "  # T-0435: which project a line came from
    out = {}

    guard = [e for e in events if e.get("kind") == "guard_block"]
    key = lambda e: (e.get("category"), c.fit(c.plain(str(e.get("target") or "")), 110))
    blocks = collections.Counter(key(e) for e in guard)
    shown = blocks.most_common(MAX_LINES)
    newest = {key(e): e for e in guard if e.get("cmd")}  # T-0172: the newest event of each key that has a command
    evs = [newest.get(k, {}) for k, _ in shown]
    full = _transcript_commands({e["session_id"] for e in evs
                                 if e.get("session_id") and e.get("tool") == "Bash"}) if recheck else {}
    now = _recheck(evs, full) if recheck else {}
    lines = []
    for i, ((cat, target), n) in enumerate(shown):
        eg, hint = _example(evs[i], full, now.get(i) == "today's guard allows it") if evs[i] else ("", "")
        lines.append(f"{n}× {cat}: {target}" + eg + (f" — {now[i]}" if i in now else "") + hint)
    out["guard blocks (each a stop Claude had to work around; a false one costs a rewrite)"] = lines
    rules = collections.Counter(e["rule"] for e in guard if e.get("rule"))  # T-0672: which checks fire most
    if rules:
        out["guard rules by fire count (a rule that fires often on harmless work is the one to look at)"] = [
            f"{n}× {r}" for r, n in rules.most_common(MAX_LINES)]

    failed = [e for e in events if e.get("kind") == "tool_fail" and not _a_no(e)]  # T-0301: grouped by why
    why = lambda e: (_tool(e), c.fit(c.plain(str(e.get("error") or "no error recorded")), 90))
    fails = collections.Counter(why(e) for e in failed)
    target = {why(e): c.fit(c.plain(str(e.get("target") or "")), 90) for e in failed}  # the newest example
    out["failed tool calls"] = [f"{n}× {k[0]}: {k[1]}" + (f" — e.g. {target[k]}" if target[k] else "")
                                for k, n in fails.most_common(MAX_LINES)]

    votes = collections.Counter((c.plain(str(e.get("cmd") or "")), c.plain(str(e.get("word") or "")))
                                for e in {(e.get("cmd"), e.get("word"), e.get("session_id")): e
                                          for e in events if e.get("kind") == "unknown_word"}.values())  # T-2382
    out["unknown fm verbs and flags (each a session that reached for one that isn't there)"] = [
        f"{n} session{'s' if n != 1 else ''}: fm {' '.join(x for x in k if x)}" for k, n in votes.most_common(MAX_LINES)]

    kinds = collections.Counter(e.get("event") for _, e in ledger)
    waits = collections.Counter(e.get("kind") for e in events if e.get("kind") == "drive_reload")  # T-0301: a wait on
    # background work is the drive working as meant (the notification resumes it), not a stop
    out["nudges and drive stops"] = [x for x in (
        f"{kinds['stop_gate']}× the Stop hook held a turn for missing evidence" if kinds["stop_gate"] else "",
        f"{waits['drive_reload']}× the drive ended a turn for a mod reload" if waits["drive_reload"] else "",
        f"{kinds['task_block']}× a task was blocked" if kinds["task_block"] else "") if x]

    runs = collections.defaultdict(list)
    for i, (_, e) in enumerate(ledger):
        if e.get("event") == "check_run":
            d = e.get("data") or {}
            load = d.get("load") if isinstance(d.get("load"), (int, float)) else None  # T-2763: load per core
            for r in d.get("results") or []:
                if isinstance(r, dict) and isinstance(r.get("s"), (int, float)) and r["s"] > 0:  # T-0424: a reused pass is 0.0 s
                    runs[r.get("cmd")].append((r["s"], i, load))
    slow = [(cmd, xs[-1], statistics.median(x[0] for x in xs)) for cmd, xs in runs.items()
            if len(xs) >= 3 and xs[-1][0] > 1.5 * statistics.median(x[0] for x in xs) and xs[-1][0] > 5]
    per_run = collections.Counter(x[1][1] for x in slow)  # T-2763: gates slow in the same run
    mach = [x for x in slow if per_run[x[1][1]] > 1 or (x[1][2] or 0) > 1]  # together, or the box was over one per core
    lines = [f"{cmd}: {last[0]:.0f}s vs usual {med:.0f}s" for cmd, last, med in slow if (cmd, last, med) not in mach]
    if mach:
        lines.insert(0, f"{len(mach)} gate{'s slow together' if len(mach) != 1 else ' slow'} under load "
                        f"({', '.join(x[0] for x in mach)}): likely the machine, not the code")
    out["slow gates (latest vs usual)"] = lines[:MAX_LINES]

    ms = sorted(e["ms"] for e in events if e.get("kind") == "hook_ms" and isinstance(e.get("ms"), (int, float)))
    if ms:
        out["hook latency"] = [f"p95 {ms[int(len(ms) * 0.95) - 1 if len(ms) > 1 else 0]:.0f} ms over {len(ms)} hook runs"]

    try:  # T-0437: hooks never fail a call, so the errors they swallow are friction only hooks.log saw
        from sampo import doctor
        errs = [e for e in doctor.recent_hook_errors() if " paused for " not in e[0]]  # the breaker's note
    except Exception:
        errs = []
    if errs:
        top = collections.Counter((e[0].split(" ") + ["?"])[1] for e in errs).most_common(1)[0]
        at = re.findall(r'File "([^"]+)", line (\d+)', "\n".join(errs[-1]))  # the innermost frame is the last
        where = f"{c.plain(os.path.basename(at[-1][0]))}:{at[-1][1]}, " if at else ""
        out["errors the hooks swallowed"] = [f"{len(errs)} in the last 24 h, most in {top[0]} ({top[1]}); latest: "
                                             f"{c.fit(c.plain(errs[-1][-1].strip()), 160)} ({where}state/logs/hooks.log)"]
    try:  # T-3535: what a busy project lock made the hooks skip, one rolled-up line instead of a traceback each
        from sampo import hooks as shooks
        busy = shooks.lock_busy()
        by = sorted(((str(k), int(n)) for k, n in (busy or {}).get("by", {}).items()), key=lambda x: -x[1])
        if by:
            out["hook writes a busy lock skipped"] = [
                f"busy project lock: {sum(n for _, n in by)} hook write(s) skipped since {c.plain(busy['since'])}: "
                + ", ".join(f"{c.plain(k)} {n}" for k, n in by)
                + f"; latest {c.fit(c.plain(busy.get('holder') or ''), 200)}"]
    except Exception:  # a report line; never the digest
        pass

    steer = lambda e: e.get("event") == "note" and str((e.get("data") or {}).get("text") or "").startswith("steer:")
    said = [label(lp) + c.fit(c.plain(str((e.get("data") or {}).get("text") or "")), 200) for lp, e in ledger
            if e.get("event") == "correction" or steer(e)]
    out["what the user corrected or steered (their words)"] = said[-MAX_LINES:]
    stalls = collections.Counter(str((e.get("data") or {}).get("after")) for lp, e in ledger
                                 if lp.slug == p.slug and e.get("event") == "stall")
    out["stalls: the user had to say 'continue' (T-0887)"] = [  # Claude stopped early; the Stop hook's last act says why
        f"{sum(stalls.values())} keep-going prompt(s): " + ", ".join(f"{n}× after {k}" for k, n in stalls.most_common())
    ] if stalls else []

    out["escapes: defects found after a task closed (the lenses that passed it)"] = [  # T-0285: review recall over time
        f"{label(lp)}{e.get('task')} ({', '.join((e.get('data') or {}).get('lenses') or []) or 'no lens'}) → "
        f"{(e.get('data') or {}).get('by')} {c.fit(c.plain(str((e.get('data') or {}).get('title') or '')), 140)}"
        for lp, e in ledger if e.get("event") == "escape"][-MAX_LINES:]

    out["surprises: where the model of the code was wrong (fm surprise)"] = [
        f"{label(lp)}{e.get('task') or '-'}: {c.fit(c.plain(str((e.get('data') or {}).get('text') or '')), 200)}"
        for lp, e in ledger if e.get("event") == "surprise"][-MAX_LINES:]

    others = collections.defaultdict(collections.Counter)  # T-0435: how much friction each other project had
    for lp, e in ledger:
        kind = "steer" if steer(e) else e.get("event")
        if lp.slug != p.slug and kind in ("guard_block", "correction", "steer", "task_block", "escape", "surprise",
                                          "stop_gate", "task_done"):
            others[lp.slug][kind.replace("_", " ")] += 1
    out["other projects (since the last pass)"] = [
        f"{slug}: " + ", ".join(f"{n} {k}" for k, n in cnt.most_common()) for slug, cnt in sorted(others.items())][:MAX_LINES]

    out["lessons recorded"] = [f"{e.get('task')}: {c.fit(c.plain(str(e['data']['lesson'])), 160)}" for _, e in ledger
                               if e.get("event") == "task_done" and (e.get("data") or {}).get("lesson")][-MAX_LINES:]

    briefs = c.load_briefs(p)
    out["requests from other projects (fm projects --sweep, fm -p SLUG capture --source cross-project), open here"] = [
        f"{b.id} {c.fit(b.title, 140)}" for b in briefs
        if b.meta.get("source") == "cross-project" and b.status not in c.CLOSED][:MAX_LINES]  # T-0443

    mine = [b for b in briefs if b.meta.get("source") == "self"]
    open_ = [b for b in mine if b.status not in c.CLOSED]
    closed = [b for b in mine if b.status == "done" and (b.meta.get("updated") or "") > start]  # dropped ones fixed nothing
    out["self-inbox: what became of earlier passes"] = (
        [f"done since: {b.id} {c.fit(b.title, 90)}" for b in closed][:MAX_LINES] +
        [f"open: {b.id} {c.fit(b.title, 90)}" for b in open_][:MAX_LINES])
    budget = collections.defaultdict(lambda: [0, 0])  # T-0499: what the hooks put into the model's context
    same = collections.Counter()  # T-0860: the same note, word for word (h), not one kind of note about many things
    for e in events:
        if e.get("kind") == "inject":
            b = budget[(e.get("event"), e.get("key"))]
            b[0], b[1] = b[0] + 1, b[1] + int(e.get("chars") or 0) - int(e.get("addon") or 0)  # T-2589
            same[(e.get("event"), e.get("key"), e.get("h"))] += 1
    most = collections.Counter()
    for (ev, k, _), n in same.items():
        most[(ev, k)] = max(most[(ev, k)], n)
    out["injected notes (event: kind — times, characters)"] = [
        f"{ev}: {k} — {n}×, {ch:,} chars" for (ev, k), (n, ch) in sorted(budget.items(), key=lambda x: -x[1][1])
    ][:MAX_LINES]
    out["notes repeated 3+ times (a candidate for a hard check instead of prose: T-0470)"] = [
        f"{k} ({most[(ev, k)]}× from {ev})" for (ev, k), (n, _) in budget.items()
        if most[(ev, k)] >= 3 and ev not in ("SessionStart", "UserPromptSubmit")][:MAX_LINES]  # those two: every turn
    out["notes averaging over 1,500 characters per injection (trim them: T-2109)"] = [
        f"{ev}: {k} — {ch // n:,} chars avg × {n}" for (ev, k), (n, ch) in sorted(budget.items(), key=lambda x: -x[1][1])
        if n and ch / n > 1500][:MAX_LINES]
    out.update(_mine(ledger, label))  # T-0658
    try:  # T-0620: where this project's work hasn't held, for the pass to look at first
        from sampo import outcomes
        out["weak areas (fm outcomes --atlas)"] = [x[2:] for x in outcomes.atlas_lines(p) if x.startswith("- ")][:MAX_LINES]
    except Exception:  # a report line; never the digest
        pass
    lessons = collections.Counter(e.get("task") for e in events if e.get("kind") == "lesson_shown")  # T-0454
    out["lessons shown (one shown often while the same blocks recur may need rewording)"] = [
        f"{t}: {n}×" for t, n in lessons.most_common(MAX_LINES)]
    dead = [d for d in meta.get("deadends") or [] if isinstance(d, dict) and d.get("text")]  # T-0471
    skipped = 0
    for k in list(out) if dead else []:
        keep = [x for x in out[k] if not any(d["text"].lower() in str(x).lower() for d in dead)]
        skipped, out[k] = skipped + len(out[k]) - len(keep), keep
    if skipped:
        out["dead ends (fm friction --reject)"] = [f"{skipped} line(s) skipped: " + "; ".join(
            f"\"{d['text']}\" ({d.get('why') or 'no reason given'})" for d in dead[:5])]
    kinds_ev = collections.Counter(e.get("kind") for e in events)
    counts = {"tool calls": kinds_ev["tool"], "guard blocks": kinds_ev["guard_block"],
              "failed tool calls": kinds_ev["tool_fail"],
              "drive stops": kinds_ev["drive_reload"] + kinds["stop_gate"], "steers": len(said)}
    last = meta.get("rsi_counts") or {}
    if last.get("tool calls") and counts["tool calls"]:  # T-0187: did the last pass's fixes make it better
        out["trend: the last pass's window → this one, per 100 tool calls"] = [" · ".join(
            f"{k} {_rate(last.get(k) or 0, last['tool calls'])} → {_rate(counts[k], counts['tool calls'])}"
            for k in counts if k != "tool calls" and (last.get(k) or counts[k]))]
    return {"since": start, "sections": {k: v for k, v in out.items() if v}, "counts": counts}


def render(d):
    lines = [f"Sampo friction since {d['since']}:"]
    for title, items in d["sections"].items():
        lines.append(f"\n{title}:")
        lines += [f"  - {x}" for x in items]
    if len(lines) == 1:
        lines.append("  (nothing recorded)")
    return "\n".join(lines)


BRIEF = """# Sampo self-improvement pass

You review Sampo (a Claude Code plugin: `~/.claude/foreman`, CLI `fm`, library `plugin/lib/sampo/*.py`, hooks
`plugin/hooks/`, UI mod `mods/foreman-ui/`) from its own friction, read-only. The digest below is what went wrong or
slow since the last pass, from Sampo's ledgers and events.

{digest}

Return at most 5 improvements, most valuable first. For each:
- the friction it removes, citing the digest line(s) it comes from;
- the cause, grounded in the code (file:line you read), not guessed;
- the change, as small as works, and its type (FIX/PERFORMANCE/CLEAN/FEATURE/SECURITY) and size (S/M/L);
- done when: one observable check.
A guard block marked "today's guard allows it" was fixed since: leave it. The trend line compares the last pass's
window with this one. Also say whether an earlier pass's item (the self-inbox section) failed to remove its friction,
and whether this loop itself should change (how often it runs, what it reads). For the guard, propose shell forms to add to FORMS in
plugin/tests/test_guard_diff.py (it runs them in real bash and fails on any the guard misses). Skip anything you
can't ground; say so instead.
"""


def due(p):
    """True at a task boundary once rsi_every tasks of this project closed since the last pass, and at least MIN_GAP_H
    hours after it (0 or unset: off)."""
    meta = c.read_meta(p)
    every = int(meta.get("rsi_every") or 0)
    if every <= 0 or (meta.get("rsi_at") and (c.age_days(meta["rsi_at"]) or 0) * 24 < MIN_GAP_H):
        return False
    brief = meta.get("rsi_brief_at")  # T-0858: its brief is out and its recon running: in progress, not due again
    if brief and str(brief) > str(meta.get("rsi_at") or "") and (c.age_days(brief) or 0) * 24 < MIN_GAP_H:
        return False
    start = meta.get("rsi_at") or meta.get("created") or ""
    # T-3580/T-3573: only the task_done lines are decoded (6,000 lines on each fm ui and next_for); second-resolution stamps
    done = sum(1 for e in c.ledger_tail(p, 6000, kinds=("task_done",)) if str(e.get("ts") or "") >= start)
    return done >= every


ACTION = ("self-improvement pass due: fm friction --brief, then one sampo:fm-recon subagent on that brief; capture "
          "its grounded findings with fm capture --source self, then fm friction --mark")


RETRY_S = 30  # ponytail: a flat guess at the turn a guard block or failed call costs; measure from transcripts if it matters


def minutes(days=7):
    """T-0959: {kind: minutes} lost to friction in the last `days`, from the global events: each guard block and failed
    tool call costs a retry, a failed call also the time it ran, and every hook its run time."""
    since = c.iso(time.time() - days * 86400)
    ev = list(c.global_events(since))  # rotated logs too
    ms = lambda kind: sum(e["ms"] for e in ev if e.get("kind") == kind and isinstance(e.get("ms"), (int, float)))
    n = lambda kind: sum(e.get("kind") == kind for e in ev)
    return {"guard blocks": round(n("guard_block") * RETRY_S / 60, 1),
            "failed tool calls": round((ms("tool_fail") / 1000 + n("tool_fail") * RETRY_S) / 60, 1),
            "hook time": round(ms("hook_ms") / 60000, 1)}


def classes(p, day=None):
    """T-2992: [{class, count, repeated}] for today's (UTC) refusals, most first: guard blocks by category, merge
    refusals by reason, self-change holds, failed closes. A class that happened twice or more repeated."""
    day = day or c.now()[:10]
    n = collections.Counter()
    for e in c.tail_jsonl(os.path.join(c.state_dir(), "events.jsonl"), 30000):
        if e.get("kind") == "guard_block" and str(e.get("ts") or "").startswith(day):
            n[f"guard block: {c.plain(str(e.get('category') or '?'))}"] += 1
    for e in c.ledger_events(p, ("enforce", "self_change_refused", "first_brief_close_failed"), since=day):
        d, ev = e.get("data") or {}, e.get("event")
        if ev == "enforce" and d.get("kind") == "merge_refused":
            n["merge refused: " + c.fit(re.sub(r"\d+", "N", c.plain(str(d.get("reason") or "?"))), 60)] += 1
        elif ev == "self_change_refused":
            n["self-change hold"] += 1
        elif ev == "first_brief_close_failed":
            n["close failed"] += 1
    return [{"class": k, "count": v, "repeated": v > 1} for k, v in n.most_common()]


def cmd_friction(args):
    from sampo import cli
    p = cli.resolve(args)
    if getattr(args, "classes", False):
        rows = classes(p)
        return cli.out(args, {"classes": rows}, "Today's refusals by class:\n" + "\n".join(
            f"  {r['class']} ×{r['count']}{' (repeated)' if r['repeated'] else ''}" for r in rows)
            if rows else "No refusals today.")
    if getattr(args, "minutes", False):
        by = minutes()
        total = round(sum(by.values()), 1)
        return cli.out(args, {"minutes": total, "by_kind": by, "days": 7},
                         f"Friction: ≈ {total:g} min lost per week (last 7 days): "
                         + " · ".join(f"{k} {v:g} min" for k, v in by.items()))
    if args.every is not None:
        if args.every < 0:
            raise cli.UsageError("--every takes 0 (off) or a number of closed tasks")
        with c.lock(p.dir):
            meta = c.read_meta(p)
            meta["rsi_every"] = args.every
            c.write_meta(p, meta)
        return print(f"Self-improvement pass: every {args.every} closed tasks." if args.every else
                     "Self-improvement pass: off.")
    if getattr(args, "reject", None):  # T-0471: a dead end later passes skip (and cite) instead of proposing again
        with c.lock(p.dir):
            meta = c.read_meta(p)
            meta["deadends"] = (meta.get("deadends") or []) + [{"text": c.redact(args.reject), "why": c.redact(
                args.why or ""), "at": c.now()}]
            c.write_meta(p, meta)
        return print(f"Dead end recorded: lines with \"{args.reject}\" are skipped from now on.")
    if args.mark:
        with c.lock(p.dir):
            meta = c.read_meta(p)
            meta["rsi_counts"] = digest(p, recheck=False)["counts"]
            meta["rsi_at"] = c.now()
            c.write_meta(p, meta)
        c.log_event(p, "rsi_pass")
        return print("Pass recorded; the next digest starts here and reports what became of its items.")
    d = digest(p)
    if args.json:
        return cli.out(args, d, "")
    text = render(d)
    if args.brief:
        path = os.path.join(p.dir, "audits", f"rsi-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}.md")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        c.write_atomic(path, BRIEF.format(digest=text))
        c.update_meta(p, rsi_brief_at=c.now())  # T-0858
        return print(f"Self-improvement brief ({len(text)} chars of digest): {path}")
    print(text)
