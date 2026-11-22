"""T-5769: the context keeper for the lead. A lead session that sits idle at a task boundary (no active task) with its
context over 60% gets one `/compact <focus>` typed into its tmux pane (the focus is `sampo resume --line`: the next task
and the queue, plain words only), at most once per 30 minutes per session, and never over text in the input box, an open
dialog or a running turn. A session with no usable tmux pane gets the same line printed by the Stop hook instead.

One decision, three harnesses (the standing rule: every feature works in the Claude Code plugin, in `sampo code` and in
headless runs):
  decide(pct, at_boundary, last_at, state)   the neutral rule: act or not, and why not
  read_cc / read_code / read_headless        what each harness can read of its own context: the statusline's snapshot (or
                                             the transcript's last usage row), `sampo code`'s conversation against its
                                             room, a headless run's transcript
  sinks                                      Claude Code: sink_tmux (typed into the pane, after tmuxpane.claude_here and a
                                             strict screen check) or sink_line (the Stop hook prints it); `sampo code`:
                                             code_tick (its own sight/trim already cuts old results: the keeper adds the
                                             journal row); headless: headless_row (the decision as a ledger row, nothing
                                             typed or printed)
State is one file, state/keeper.json: {"panes": {session: {"at", "snap", "pct", "how"}}, "first": epoch of the first
compact typed or shown}; `first` is what `sampo cost --lead` compares the lead's cache reads around.

The Stop hook fires while the pane still shows the turn's spinner, so it never types: it starts a detached, delayed
`fm keeper --session SID` (one per session at a time) that waits out a busy pane and then types.

Models advise and proof decides: nothing here asks a model; the percentage is the statusline's own, or the last usage row
of the session's transcript. The pane is read before anything is typed, and a failed read types nothing."""
import contextlib
import json
import os
import re
import subprocess
import time

from sampo import core as c
from sampo import tmuxpane

THRESHOLD = 60       # percent of the window: over it, at a boundary, the keeper acts
GAP_S = 30 * 60      # one compact per session in this long
FRESH_S = 2 * 3600   # a snapshot older than this is a session that has gone quiet: left alone
TAIL = 256 * 1024    # bytes of a transcript's end read for the last usage row
WINDOW = 200000      # the context size taken when the snapshot gives none
FOCUS_MAX = 300      # characters of the focus after /compact
STAMP_S = 120        # a detached keeper started this recently is still to run: no second one
DELAY_S, POLL_S, TRIES = 5.0, 5.0, 6  # the detached child: wait for the spinner to clear, look this often, this many times


def _file():
    return os.path.join(c.state_dir(), "keeper.json")


def _load():
    try:
        with open(_file(), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    d = d if isinstance(d, dict) else {}
    if not isinstance(d.get("panes"), dict):
        d["panes"] = {}
    return d


def first_compact():
    """Epoch seconds of the first compact the keeper typed or showed, or None."""
    f = _load().get("first")
    return float(f) if isinstance(f, (int, float)) and not isinstance(f, bool) else None


# ---------------------------------------------------------------- the neutral decision

def decide(pct, at_boundary, last_at, state):
    """(act, why): whether a session at `pct` percent of its context, at a task boundary or not, last compacted at
    `last_at` (epoch or None), should be compacted now. `state` carries "now" and, for a harness whose reading only
    changes when it is rewritten, "snap" and "last_snap" (the reading's mark now and at the last compact). Pure: no
    clock, file or pane is touched here."""
    now = state.get("now") or time.time()
    if pct is None:
        return False, "no context reading"
    if pct <= THRESHOLD:
        return False, f"{pct}% is not over {THRESHOLD}%"
    if last_at is not None and now - last_at < GAP_S:
        return False, f"one in {GAP_S // 60} minutes: the last was {int((now - last_at) // 60)} minutes ago"
    snap, last = state.get("snap"), state.get("last_snap")
    if last_at is not None and snap is not None and last is not None and snap <= last:
        return False, "same snapshot as the last one: its context isn't re-read yet"
    if not at_boundary:
        return False, "mid-task"
    return True, ""


# ---------------------------------------------------------------- readers, one per harness

def transcript_pct(sid, snap):
    """The context percent from the last usage row in the session's transcript (its tail only): what the turn read, wrote
    to the cache and sent fresh, over the window. None when there is no transcript or no usage row."""
    from sampo import cost
    cwd, path = str(snap.get("cwd") or ""), snap.get("transcript_path")
    if not (isinstance(path, str) and os.path.isfile(path)):
        path = os.path.join(cost.transcripts_dir(cwd), re.sub(r"[^\w.-]", "_", sid) + ".jsonl")
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - TAIL))
            lines = f.read().splitlines()
    except OSError:
        return None
    if size > TAIL:
        lines = lines[1:]  # the first line of a mid-file read is cut
    for ln in reversed(lines):
        try:
            e = json.loads(ln)
            u = e["message"]["usage"] if e.get("type") == "assistant" else None
            used = sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
        if used:
            try:
                window = int(snap.get("context_size") or 0) or WINDOW
            except (TypeError, ValueError):
                window = WINDOW
            return min(100, int(used * 100 / window))
    return None


def read_cc(sid, snap):
    """Claude Code: the statusline's own percentage, else the transcript's last usage row."""
    v = snap.get("context_pct")
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return int(v)
    return transcript_pct(sid, snap)


_pct = read_cc


def read_headless(sid, pl):
    """A headless run has no statusline: the transcript the Stop payload names."""
    return transcript_pct(sid, {"cwd": pl.get("cwd") or "", "transcript_path": pl.get("transcript_path")})


def read_code(msgs, profile):
    """`sampo code`: its conversation's size (chars over four, as the harness counts) as a percent of its context room.
    None for a priced profile (it is trimmed on every send, no room to fill) and for one with no known context."""
    from sampo import smith
    room = smith._room(profile)
    return min(100, int(smith._tokens(msgs) * 100 / room)) if room else None


# ---------------------------------------------------------------- sinks, one per harness

def compact_text(q):
    """The one line typed or shown: /compact and the resume line as plain words (tmuxpane.plain), FOCUS_MAX at most."""
    from sampo import cli
    return ("/compact " + tmuxpane.plain(cli.resume_line(q), FOCUS_MAX)).strip()


def sink_tmux(pane, text):
    return tmuxpane.send(pane, text)


def sink_line(row, why, q):
    """No pane the keeper may type into: the Stop hook shows the line."""
    row.update(action="print", why=why, line=compact_text(q))


def code_tick(run, name, msgs, profile, now=None):
    """`sampo code`, at the start of a send: the run's own sight/trim cuts old results past 70% of its room (a priced
    profile's every send): that is this harness's compact. At 60% and over, once per 30 minutes, the keeper's row goes
    in the run's journal (kind keeper) so the decision is on record there too. The row, or None."""
    now = now or time.time()
    pct = read_code(msgs, profile)
    act, _ = decide(pct, True, getattr(run, "keeper_at", None), {"now": now})
    if not act:
        return None
    run.keeper_at = now
    row = {"session": name, "pct": pct, "action": "trim", "how": "code", "why": "its own sight cuts old results"}
    run.log(kind="keeper", phase=name, pct=pct, action="trim", how="code")
    return row


def headless_row(p, sid, pl, now=None):
    """A headless run (the SDK, a drive worker in the main checkout): nobody reads its screen and no pane takes keys. The
    same decision, recorded as the `keeper_compact` ledger row with how "headless". Never sets `first`: nothing was
    compacted. The row, or None when the run is no lead of this project."""
    now = now or time.time()
    row = {"session": sid, "pct": read_headless(sid, pl), "action": "skipped", "why": ""}
    try:
        with c.lock(os.path.join(c.state_dir(), "keeper"), timeout=2):
            st = _load()
            rec = st["panes"].get(sid) if isinstance(st["panes"].get(sid), dict) else None
            act, row["why"] = decide(row["pct"], not c.active_brief_fast(p, p.lane), rec.get("at") if rec else None,
                                     {"now": now})
            if act:
                row.update(action="recorded", line=compact_text(p))
                st["panes"][sid] = {"at": now, "snap": 0, "pct": row["pct"], "how": "headless"}
                c.write_atomic(_file(), json.dumps(st), durable=False)
                c.log_event(p, "keeper_compact", data={"pct": row["pct"], "how": "headless",
                                                       "focus": row["line"][:FOCUS_MAX + 9]}, session=sid)
    except c.LockTimeout:
        row["why"] = "locked"
    return row


# ---------------------------------------------------------------- Claude Code: one pass over the snapshots

def tick(p, now=None, dry=False, only_sid=None, print_ok=False, tmux_ok=True):
    """One pass over the lead sessions of project `p` (the statusline's snapshots in state/sessions): rows
    {session, pct, action, why[, line]}. action is "sent" (typed into the pane), "print" (print_ok: no pane the keeper
    may type into, the caller shows `line`), "would send" (dry) or "skipped" with the why. `only_sid` looks at that
    session alone (the Stop hook, the detached keeper); `tmux_ok` False treats every pane as unusable (the Stop hook
    already found it so). A session of another project, a worktree's (a builder's) or one whose snapshot is stale gets no
    row. A state file another keeper holds for more than two seconds is one "locked" row, not an error."""
    now = now or time.time()
    sdir = os.path.join(c.state_dir(), "sessions")
    try:
        names = [only_sid + ".json"] if only_sid else sorted(n for n in os.listdir(sdir) if n.endswith(".json"))
    except OSError:
        return []
    rows = []
    try:
        with contextlib.nullcontext() if dry else c.lock(os.path.join(c.state_dir(), "keeper"), timeout=2):
            _pass(p, now, dry, only_sid, print_ok, tmux_ok, sdir, names, rows)
    except c.LockTimeout:
        return [{"session": "-", "pct": None, "action": "skipped", "why": "locked"}]
    return rows


def _pass(p, now, dry, only_sid, print_ok, tmux_ok, sdir, names, rows):
    st, changed = _load(), False
    for name in names:
        sid, path = name[:-5], os.path.join(sdir, name)
        try:
            with open(path, encoding="utf-8") as f:
                snap = json.load(f)
            mtime = os.stat(path).st_mtime_ns
        except (OSError, ValueError):
            continue
        ts = c.parse_ts(snap.get("ts")) if isinstance(snap, dict) else None
        if not ts or not 0 <= now - ts.timestamp() <= FRESH_S:
            continue
        cwd = snap.get("cwd")
        if only_sid:
            q = p
        else:
            q = c.find_project(cwd) if isinstance(cwd, str) and os.path.isdir(cwd) else None
        if not q or q.slug != p.slug or q.lane:
            continue
        pct = read_cc(sid, snap)
        row = {"session": sid, "pct": pct, "action": "skipped", "why": ""}
        rows.append(row)
        rec = st["panes"].get(sid) if isinstance(st["panes"].get(sid), dict) else None
        act, row["why"] = decide(pct, not c.active_brief_fast(q, q.lane), rec.get("at") or 0 if rec else None,
                                 {"now": now, "snap": mtime, "last_snap": (rec.get("snap") or 0) if rec else None})
        if not act:
            continue
        pane = snap.get("tmux_pane")
        pane = pane if tmuxpane.valid(pane) else None
        usable, why_not = (False, "no tmux pane") if not (pane and tmux_ok) else tmuxpane.claude_here(pane, snap)
        if not usable:
            if not print_ok:
                row["why"] = why_not
                continue
            sink_line(row, why_not, q)
            how = "print"
        else:
            state = tmuxpane.state(pane, strict=True)
            if state != "idle":
                row["why"] = {"gone": "no pane", "foreign": "not a Claude Code screen"}.get(state, state)
                continue
            row.update(action="would send" if dry else "sent", why="", line=compact_text(q))
            how = "tmux"
            if not dry and not sink_tmux(pane, row["line"]):
                row.update(action="skipped", why="tmux failed")
                row.pop("line", None)
                continue
        if not dry:
            st["panes"][sid] = {"at": now, "snap": mtime, "pct": pct, "how": how}
            if not st.get("first"):
                st["first"] = now  # the printed line counts too: `cost --lead` can measure without tmux
            changed = True
            c.log_event(q, "keeper_compact", data={"pct": pct, "how": how, "focus": row["line"][:FOCUS_MAX + 9]},
                        session=sid)
    if changed:
        live = {n[:-5] for n in names}  # a session whose snapshot is gone keeps no record
        st["panes"] = {k: v for k, v in st["panes"].items() if k in live or now - (v.get("at") or 0) < 7 * 86400}
        c.write_atomic(_file(), json.dumps(st), durable=False)


def watch(p, sid, delay=DELAY_S, poll=POLL_S, tries=TRIES):
    """The detached keeper: wait `delay` s for the turn's spinner to clear, then look up to `tries` times, `poll` s
    apart, while the pane is busy; any other answer (typed, dialog, sent, skipped) is final. The last rows."""
    if delay > 0:
        time.sleep(delay)
    rows = []
    for i in range(max(1, tries)):
        rows = tick(p, only_sid=sid)
        if not (rows and rows[0]["action"] == "skipped" and rows[0]["why"] == "busy"):
            break
        if i + 1 < tries and poll > 0:
            time.sleep(poll)
    return rows


def _stamp(sid):
    name = re.sub(r"[^\w.-]", "_", str(sid))[:80]  # a file name, cut at 80 characters
    return os.path.join(c.state_dir(), "keeper-run-" + name)


def spawn(p, sid):
    """The Stop hook's part with a pane: a detached `fm keeper --session SID --delay 5`, one per session at a time (a
    stamp younger than STAMP_S means the one before is still to run; a child that died leaves no lock for good). Tests
    (FOREMAN_NO_BACKGROUND) run it inline at once."""
    if os.environ.get("FOREMAN_NO_BACKGROUND"):
        watch(p, sid, delay=0, poll=0, tries=1)
        return
    stamp = _stamp(sid)
    try:
        if time.time() - os.path.getmtime(stamp) < STAMP_S:
            return
    except OSError:
        pass
    try:
        os.makedirs(os.path.dirname(stamp), exist_ok=True)
        open(stamp, "w").close()
    except OSError:
        pass
    try:
        subprocess.Popen([os.path.join(c.PLUGIN_ROOT, "bin", "fm"), "keeper", "--session", str(sid), "--delay", str(DELAY_S)],
                         cwd=p.root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError:
        pass


def after_stop(pl):
    """The Stop hook's part: the line to show the user (no pane the keeper may type into) or None. A stop that continues a
    block and a session in a lane worktree are left alone. A headless run is recorded as a row. With a pane that is the
    session's own Claude Code the hook only starts the detached keeper (the pane still shows the spinner now) and shows
    nothing."""
    sid = pl.get("session_id")
    if not sid or pl.get("stop_hook_active"):
        return None
    sid = re.sub(r"[^\w.-]", "_", str(sid))
    p = c.find_project(pl.get("cwd") or os.getcwd())
    if not p or p.lane:
        return None
    if c.headless():
        headless_row(p, sid, pl)
        return None
    try:
        with open(os.path.join(c.state_dir(), "sessions", sid + ".json"), encoding="utf-8") as f:
            snap = json.load(f)
    except (OSError, ValueError):
        return None
    pct = read_cc(sid, snap) if isinstance(snap, dict) else None
    if pct is None or pct <= THRESHOLD or c.active_brief_fast(p, p.lane):
        return None  # cheap: the decision proper is tick's
    pane = snap.get("tmux_pane")
    if tmuxpane.valid(pane) and tmuxpane.claude_here(pane, snap)[0]:
        spawn(p, sid)
        return None
    for r in tick(p, only_sid=sid, print_ok=True, tmux_ok=False):
        if r["action"] == "print":
            return (f"Sampo: this session's context is {r['pct']}% and no task is open: a good time to compact. "
                    f"Run `{r['line']}`")
    return None


def cmd_keeper(args):
    """sampo keeper [--dry] [--session SID]: one pass of the keeper over this project's lead sessions: what each one was,
    or would be, sent. --session SID is the Stop hook's detached run: that session alone, after a short wait."""
    from sampo import cli
    p = cli.resolve(args)
    sid = getattr(args, "session", None)
    if sid:
        sid = re.sub(r"[^\w.-]", "_", str(sid))
        try:
            rows = watch(p, sid, getattr(args, "delay", 0) or 0, getattr(args, "poll", POLL_S), getattr(args, "tries", TRIES))
        finally:
            with contextlib.suppress(OSError):
                os.unlink(_stamp(sid))
    else:
        rows = tick(p, dry=getattr(args, "dry", False))
    text = "\n".join(f"{r['session'][:12]} {r['pct'] if r['pct'] is not None else '?'}% {r['action']}"
                     + (f": {r['why']}" if r["why"] else "") for r in rows) or "No lead session of this project is live."
    return cli.out(args, {"sessions": rows}, text)
