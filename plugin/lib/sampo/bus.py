"""T-0708 (Frontier 08, first slice): the control plane between Claude sessions on one machine, at zero tokens until
something happens. The bus: typed messages to a session or all, delivered by the hooks that already run (a note on
the next tool call; a session about to stop is held for its mail), and --wake types one short line into a session's
tmux pane. Leases: an edit leases the function it lands in; another session's edit inside it is refused, naming the
holder. The conductor: live sessions, their mail and leases, and one steer for all. Files under Sampo's state."""
import json
import os
import re
import sys
import time

from sampo import core as c
from sampo.core import subprocess  # T-2023: imported on first use

socket = c._Later("socket")  # T-3577: off the hooks' path too

LIVE_S = 15 * 60       # a session whose statusline wrote within this is live
LEASE_S = 20 * 60      # an edit holds its function this long, renewed by each edit
FRESH_ALL_S = 15 * 60  # a session reading mail for the first time gets broadcasts this recent
WATCH_S = 10           # a watcher whose heartbeat is this fresh delivers wakes; older, tmux is tried
KEEP = 500             # messages kept
INTERPRETERS = {"sh", "bash", "dash", "zsh", "node", "bun", "deno"}  # T-2512: what runs a shebang script
TYPES = ("note", "steer", "stop")
_DEF = c.lazy(r"^(\s*)(?:(?:export|pub|async|static|public|private)\s+)*(?:def|class|function|fn|func)\s+(\w+)")


def _dir():
    d = os.path.join(c.state_dir(), "bus")
    os.makedirs(d, exist_ok=True)
    return d


def _sid(s):
    return re.sub(r"[^\w-]", "", str(s or ""))[:80]


def _me(pl):
    """Who an edit belongs to for leases: the hook's session, or, inside an fm code worker turn (its own Claude
    session, one per turn), the fm code run that started it (T-1294): its judge and its next turn edit on."""
    run = os.environ.get("FOREMAN_WORKER_OF") or ""  # fm code's private channel: only its own value names a run
    return _sid(run if run.startswith("fm-code-") else pl.get("session_id"))


def drop_session(sid):
    """T-1294: drop every lease a session holds (an fm code run that ended), so its code is free at once."""
    with c.lock(_dir()):
        data = _leases()
        _write_leases({k: v for k, v in data.items() if v["session"] != _sid(sid)})


# ---------------------------------------------------------------- bus

def send(to, text, kind="note", sender=None):
    if kind not in TYPES:
        raise ValueError(f"type is one of {', '.join(TYPES)}")
    msg = {"ts": c.now(), "t": time.time(), "from": _sid(sender) or "user", "to": "all" if to == "all" else _sid(to),
           "type": kind, "text": c.redact(str(text))[:2000]}
    path = os.path.join(_dir(), "mail.jsonl")
    with c.lock(_dir()):
        try:
            with open(path) as f:
                lines = f.read().splitlines()[-(KEEP - 1):]
        except OSError:
            lines = []
        c.write_atomic(path, "\n".join(lines + [json.dumps(msg)]) + "\n")
    return msg


def _for(msg, sid):
    to = msg.get("to") or ""
    return msg.get("from") != sid and (to == "all" or (len(to) >= 6 and sid.startswith(to)))


def unread(sid, mark=True):
    """Messages for this session it hasn't been shown, oldest first; shown now when mark."""
    sid = _sid(sid)
    if not sid:
        return []
    cur = os.path.join(_dir(), f"seen-{sid}")
    try:
        with open(os.path.join(_dir(), "mail.jsonl")) as f:
            msgs = [json.loads(x) for x in f if x.strip()]
    except (OSError, ValueError):
        return []
    try:
        with open(cur) as f:
            seen = float(f.read().strip() or 0)
        first = False
    except (OSError, ValueError):
        seen, first = 0.0, True
    out = [m for m in msgs if m.get("t", 0) > seen and _for(m, sid)
           and not (first and m.get("to") == "all" and m.get("t", 0) < time.time() - FRESH_ALL_S)]
    if mark and msgs:
        try:
            c.write_atomic(cur, str(max(m.get("t", 0) for m in msgs)))
        except OSError:
            pass  # T-1449: a full disk shows the mail again next call rather than losing it
    return out


def render(msgs):
    return " ".join(("Steer" if m["type"] == "steer" else "Stop request" if m["type"] == "stop" else "Message")
                    + f" from {m['from'][:8]} ({m['ts'][11:16]}): {m['text']}"
                    + (" — apply it to your active task and log it (fm task log ID \"steer: …\")."
                       if m["type"] == "steer" else "") for m in msgs)


def note(sid):
    """The PreToolUse note: this session's unread mail, once."""
    msgs = unread(sid)
    return ("Sampo mail: " + render(msgs)) if msgs else None


def watched(sid):
    """T-0777: the session runs fm bus watch (its plugin monitor), which beat within WATCH_S."""
    sid = _sid(sid)
    try:
        names = [n for n in os.listdir(_dir()) if n.startswith(f"watch-{sid}")] if sid else []
        return any(time.time() - os.path.getmtime(os.path.join(_dir(), n)) < WATCH_S for n in names)
    except OSError:
        return False


def wake(sid):
    """Wake an idle session to read its mail: its watcher (fm bus watch) prints the mail itself; else one short line
    typed into its tmux pane (the statusline recorded it). False when neither is known."""
    if watched(sid):
        return True
    snap = _snapshot(sid)
    pane = (snap or {}).get("tmux_pane")
    if not pane or not re.fullmatch(r"%\d+", str(pane)):
        return False
    try:
        subprocess.run(["tmux", "send-keys", "-t", pane, "-l", "Sampo mail waiting: read it (fm bus read) and act on it"],
                       capture_output=True, timeout=10, check=True)
        subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"], capture_output=True, timeout=10, check=True)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


# ---------------------------------------------------------------- sessions

def _snapshot(sid):
    try:
        with open(os.path.join(c.state_dir(), "sessions", f"{_sid(sid)}.json")) as f:
            s = json.load(f)
    except (OSError, ValueError):
        return None
    return s if isinstance(s, dict) else None  # T-1097: side files share the folder (sid.outlined-<agent>.json)


def _proc(pid):
    """(parent pid, start time) of a live process, or None."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            rest = f.read().rsplit(")", 1)[1].split()
        return int(rest[1]), rest[19]
    except (OSError, IndexError, ValueError):
        pass
    if os.path.isdir("/proc"):
        return None
    for wait in (5, 20):  # no procfs (macOS, BSD); T-2492: a loaded host's ps can pass 5 s, and a timeout read as
        try:              # "no such process" made a live builder look gone; one patient retry first
            out = subprocess.run(["ps", "-o", "ppid=,lstart=", "-p", str(pid)], capture_output=True, text=True,
                                 timeout=wait).stdout.split(None, 1)
            return (int(out[0]), out[1].strip()) if len(out) == 2 else None
        except subprocess.TimeoutExpired:
            continue
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
    return None


def _chain(pid, depth):
    """pid and its ancestors, nearest first, each with its start time."""
    out = []
    while pid > 1 and len(out) < depth and (info := _proc(pid)):
        out.append((pid, info[1]))
        pid = info[0]
    return out


def record_pid(sid):
    """SessionStart: the hook's ancestors (the claude process among them) map to this session, so a monitor the
    session runs finds its id by walking its own parents (fm bus watch). Entries of dead processes go."""
    sid = _sid(sid)
    d = os.path.join(c.state_dir(), "sessions", "by-pid")
    os.makedirs(d, exist_ok=True)
    for n in os.listdir(d):
        try:
            os.kill(int(n), 0)
        except ProcessLookupError:
            try:
                os.remove(os.path.join(d, n))
            except OSError:  # T-0820: an overlapping SessionStart removed it first
                pass
        except (OSError, ValueError):
            pass
    for pid, start in _chain(os.getppid(), 4) if sid else []:  # sh, a re-exec'd python, then claude
        c.write_atomic(os.path.join(d, str(pid)), json.dumps({"sid": sid, "start": start}))
        if _comm(pid) == "claude":  # T-0820: never past it, into the session whose Bash ran this claude -p
            break


def claude_pid():
    """T-0841: the nearest ancestor process named claude (the session's Claude Code), or None."""
    for pid, _ in _chain(os.getpid(), 12):
        if _comm(pid) == "claude":
            return pid
    return None


def host_id():
    """T-1028: this machine and PID namespace; a pid means nothing from another one."""
    try:
        ns = os.readlink("/proc/self/ns/pid")
    except OSError:
        ns = ""
    return f"{socket.gethostname()}|{ns}"


def claude_proc():
    """T-1028: "host|ns|pid|start" of the session's Claude Code, to tell later whether it ended; None if not under one."""
    pid = claude_pid()
    info = pid and _proc(pid)
    return f"{host_id()}|{pid}|{info[1]}" if info else None


def proc_state(rec):
    """T-1028: "alive", "stopped", "unknown" (recorded on another host or PID namespace) or None (no record)."""
    host, ns, pid, start = (str(rec or "").split("|", 3) + ["", "", "", ""])[:4]
    if not pid.isdecimal():
        return None
    if f"{host}|{ns}" != host_id():
        return "unknown"
    return "alive" if proc_alive(int(pid), start) else "stopped"


def proc_alive(pid, start):
    """T-1028: the process pid is still the one that began at `start` (a reused pid has another start time)."""
    info = _proc(pid) if isinstance(pid, int) and pid > 1 else None
    return bool(info) and info[1] == str(start)


def _comm(pid):
    """A process's command name ("claude"), or ""."""
    try:
        with open(f"/proc/{pid}/comm") as f:
            return f.read().strip()
    except OSError:
        pass
    if os.path.isdir("/proc"):
        return ""
    ps = lambda field: subprocess.run(["ps", "-o", f"{field}=", "-p", str(pid)], capture_output=True,  # noqa: E731
                                      text=True, timeout=5).stdout.strip()
    try:  # no procfs (macOS, BSD)
        comm = ps("comm").rsplit("/", 1)[-1]
        if comm in INTERPRETERS:  # T-2512: macOS names a shebang script (npm's claude, the lanes drill's stub) by its
            # interpreter; the script is the interpreter's first argument that isn't a flag
            script = next((w for w in ps("args").split()[1:] if not w.startswith("-")), "")
            if script.rsplit("/", 1)[-1] == "claude":
                return "claude"
        return comm
    except (OSError, subprocess.SubprocessError):
        return ""


def _by_pid():
    """(session id, its claude pid) from the nearest recorded ancestor of this process, or (None, None)."""
    d = os.path.join(c.state_dir(), "sessions", "by-pid")
    for pid, start in _chain(os.getpid(), 12):
        try:
            with open(os.path.join(d, str(pid))) as f:
                rec = json.load(f)
        except (OSError, ValueError):
            continue
        if rec.get("start") == start and rec.get("sid"):  # not a reused pid
            return rec["sid"], pid
    return None, None


def own_session():
    return _sid(c.session_id()) or _by_pid()[0]


def watch(sid=None, interval=2.0, out=None, stop=None):
    """T-0777, the plugin monitor: one line per new message for this session (or all), nothing otherwise; never marks
    mail read (the PreToolUse note does). Polls mail.jsonl's size every interval, beats a heartbeat so senders skip
    tmux (wake), and ends with its session: parent gone, claude process gone, or stdout closed."""
    import sys
    out, ppid = out or sys.stdout, os.getppid()
    stop = stop or (lambda: os.getppid() != ppid)
    mail, since, sig, beat, anchor = os.path.join(_dir(), "mail.jsonl"), time.time(), None, None, None
    follow, tries = sid is None, 30
    try:
        while not stop():
            if follow and (anchor or (tries := tries - 1) >= 0):  # SessionStart records the claude process
                found, a = _by_pid()
                anchor = anchor or a
                now = found or _sid(c.session_id())  # T-0841: a resume moves its claude process to a new id
                if now and now != sid:
                    if beat:
                        try:
                            os.remove(beat)
                        except OSError:
                            pass
                    sid, beat, sig = now, None, None  # re-read the mail for the new id
            if anchor:
                try:
                    os.kill(anchor, 0)
                except ProcessLookupError:
                    return
                except OSError:
                    pass
            if sid:
                beat = beat or os.path.join(_dir(), f"watch-{_sid(sid)}")
                try:
                    os.utime(beat)
                except OSError:
                    open(beat, "a").close()
                try:
                    st = os.stat(mail)
                    now_sig = (st.st_size, st.st_mtime_ns)
                except OSError:
                    now_sig = None
                if now_sig and now_sig != sig:
                    sig = now_sig
                    try:
                        with open(mail) as f:
                            msgs = [json.loads(x) for x in f if x.strip()]
                    except (OSError, ValueError):
                        msgs = []
                    for m in msgs:
                        if m.get("t", 0) > since and _for(m, _sid(sid)):
                            out.write(f"Sampo {m.get('type', 'note')} from {str(m.get('from'))[:8]}: "
                                      f"{c.fit(' '.join(str(m.get('text', '')).split()), 160)} (read it: fm bus read)\n")
                            out.flush()
                    since = max([since] + [m.get("t", 0) for m in msgs])
            time.sleep(interval)
    except (BrokenPipeError, KeyboardInterrupt):
        pass
    finally:
        if beat:
            try:
                os.remove(beat)
            except OSError:
                pass


def live():
    """Snapshots of the sessions seen within LIVE_S, newest first."""
    import datetime
    d = os.path.join(c.state_dir(), "sessions")
    cut = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=LIVE_S)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    out, gone = [], _gone()
    for n in os.listdir(d) if os.path.isdir(d) else []:
        if n.endswith(".json"):
            s = _snapshot(n[:-5])
            if s and str(s.get("ts", "")) >= cut and s.get("session_id") not in gone and _owner_alive(s):
                out.append(s)
    return sorted(out, key=lambda s: str(s.get("ts")), reverse=True)


def _owner_alive(s):
    """T-1003: the claude pid the statusline recorded in the snapshot still runs with the same start (older snapshots
    carry none: alive)."""
    pid = s.get("claude_pid")
    if not isinstance(pid, int):
        return True
    info = _proc(pid)
    return bool(info) and info[1] == s.get("claude_start")


def _gone():
    """T-1001: sessions whose every recorded process (SessionStart's record_pid) has ended or is a reused pid: a killed
    session leaves no SessionEnd, and its snapshot read as live for LIVE_S."""
    d = os.path.join(c.state_dir(), "sessions", "by-pid")
    alive, seen = set(), set()
    for n in os.listdir(d) if os.path.isdir(d) else []:
        try:
            with open(os.path.join(d, n)) as f:
                rec = json.load(f)
            pid = int(n)
        except (OSError, ValueError):
            continue
        if not rec.get("sid"):
            continue
        seen.add(rec["sid"])
        info = _proc(pid)
        if info and info[1] == rec.get("start"):
            alive.add(rec["sid"])
    return seen - alive


# ---------------------------------------------------------------- leases

def _leases():
    try:
        with open(os.path.join(_dir(), "leases.json")) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    now = time.time()
    return {k: v for k, v in data.items() if v.get("until", 0) > now}


def _write_leases(data):
    c.write_atomic(os.path.join(_dir(), "leases.json"), json.dumps(data, indent=1))


def _lines(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(2_000_000)
    except OSError:
        return None


def _region(text, snippet):
    """(first, last) 0-based lines of snippet in text, or None."""
    i = text.find(snippet) if snippet else -1
    if i < 0:
        return None
    a = text.count("\n", 0, i)
    return a, a + snippet.count("\n")


def _indent(s):
    return len(s) - len(s.lstrip())


def enclosing(text, region):
    """The innermost definition around a region, by indentation: its name, or None at top level.
    ponytail: indentation, not a parser; a language whose bodies aren't indented gets file-free edits."""
    lines = text.split("\n")
    a = region[0]
    want = next((_indent(lines[i]) for i in range(a, min(region[1] + 1, len(lines))) if lines[i].strip()), 0)
    for i in range(min(a, len(lines) - 1), -1, -1):
        m = _DEF.match(lines[i])
        if m and (len(m.group(1)) < want or i == a):
            return m.group(2)
    return None


def span(text, name):
    """(first, last) lines of the definition named name, or None."""
    lines = text.split("\n")
    for i, ln in enumerate(lines):
        m = _DEF.match(ln)
        if m and m.group(2) == name:
            ind, end = len(m.group(1)), len(lines) - 1
            for j in range(i + 1, len(lines)):
                if lines[j].strip() and _indent(lines[j]) <= ind:
                    end = j - 1
                    break
            return i, end
    return None


def _edits(tool, ti):
    if tool == "MultiEdit":
        return [(e.get("old_string"), e.get("new_string")) for e in ti.get("edits") or [] if isinstance(e, dict)]
    return [(ti.get("old_string"), ti.get("new_string"))] if tool == "Edit" else []


def after_edit(pl, p, path):
    """PostToolUse: lease what this session's edit landed in (a Write leases the file)."""
    sid, tool = _me(pl), pl.get("tool_name")
    if not sid or not path.startswith(p.root.rstrip("/") + "/"):
        return
    rel = os.path.relpath(path, p.root)
    if tool == "Write":
        names = ["*"]
    else:
        text = _lines(path) or ""
        names = {n for _, new in _edits(tool, pl.get("tool_input") or {})
                 if (r := _region(text, new)) and (n := enclosing(text, r))}
    if not names:
        return
    act = c.active_brief(c.load_briefs(p), p.lane)
    with c.lock(_dir()):
        data = _leases()
        for n in names:
            _claim(data, p, sid, act.id if act else None, rel, n, LEASE_S)
        _write_leases(data)


def _sh(s):  # a lane claim's id is short already and its prefix is what names it
    return s if s.startswith("lane-") else s[:8]


def _who(v):
    return f"session {_sh(v['session'])}" + (f" ({v['task']})" if v.get("task") else "")


def claim_overlaps(p, rel, sym, me, act=None, data=None):
    """T-1068: live leases of other sessions on the same file where either side is file-wide or the symbols match.
    A lease under my own task (a lane brief's pre-claim) is mine. T-1588: builder lanes share their main session's
    id, so a lease of my session under another task is that task's, not mine."""
    task = act.id if act else None
    return [v for v in (_leases() if data is None else data).values()
            if v["project"] == p.slug and v["file"] == rel
            and (v["session"] != me or (task and v.get("task") and v["task"] != task))
            and not (act and v["session"] == f"lane-{act.id}") and (v["symbol"] == sym or "*" in (sym, v["symbol"]))]


def _claim(data, p, me, task, rel, sym, seconds, advisory=False):
    """A claim is keyed per session and task (T-1588: lanes share a session), so claims on one file and symbol coexist
    instead of displacing each other. An advisory one (fm lease take --warn over an overlap) is listed but denies
    nobody's edit."""
    data[f"{p.slug}:{rel}::{sym}@{me}" + (f"#{task}" if task else "")] = {
        "session": me, "task": task, "project": p.slug, "file": rel, "symbol": sym, "until": time.time() + seconds,
        "advisory": advisory}


def preclaim(p, task_id, files, seconds=LEASE_S):
    """T-1068: a lane brief claims the task's concrete scope files up front, under the task id. A file another
    session holds live isn't taken from it: those come back as (file, holder) pairs."""
    me, skipped = f"lane-{task_id}", []
    with c.lock(_dir()):
        data = _leases()
        for f in files:
            held = claim_overlaps(p, f, "*", me, data=data)
            if held:
                skipped.append((f, _who(held[0])))
            else:
                _claim(data, p, me, task_id, f, "*", seconds)
        _write_leases(data)
    return skipped


def release(p, task_id, preclaim=True):
    """T-1068: drop a lane's pre-claims (its lane is merged, removed or being resumed), and (T-1588) the leases its
    builder's edits and takes made under the task. T-3398: only this project's (task ids repeat across projects);
    preclaim=False keeps the lane pre-claim (a blocked lane resumes)."""
    with c.lock(_dir()):
        data = _leases()
        lane = lambda v: v["session"] == f"lane-{task_id}"  # its pre-claim is tagged with the task too
        keep = {k: v for k, v in data.items() if v.get("project") != p.slug
                or (lane(v) and not preclaim) or (not lane(v) and v.get("task") != task_id)}
        if len(keep) != len(data):
            _write_leases(keep)


def conflict(pl, p, path):
    """PreToolUse: why another session's lease forbids this edit, or None."""
    sid, tool = _me(pl), pl.get("tool_name")
    if not path.startswith(p.root.rstrip("/") + "/"):
        return None
    rel = os.path.relpath(path, p.root)
    # T-1138: a lane's pre-claim (lane-<id>) is advisory: lanes edit separate worktrees and the merge settles overlaps;
    # blocking stalled a parallel builder on a 5,000-line shared file. Explicit leases (fm lease take) still deny.
    held = [v for v in _leases().values() if v["project"] == p.slug and v["file"] == rel and v["session"] != sid
            and not v.get("advisory") and not v["session"].startswith("lane-")]
    if not held:
        return None
    text = _lines(path) or ""
    regions = [r for old, _ in _edits(tool, pl.get("tool_input") or {}) if (r := _region(text, old))]
    for v in held:
        s = None if v["symbol"] == "*" else span(text, v["symbol"])
        hit = tool in ("Write", "NotebookEdit") or v["symbol"] == "*" or (
            s and any(r[0] <= s[1] and s[0] <= r[1] for r in regions))
        if hit:
            left = int((v["until"] - time.time()) // 60) + 1
            what = rel if v["symbol"] == "*" else f"{rel}::{v['symbol']}"
            return (f"Sampo lease: {what} is leased by {_who(v)} for {left} more min, which is editing it now. "
                    f"Work elsewhere meanwhile, or ask it: fm bus send {_sh(v['session'])} \"<what you need>\".")
    return None


# ---------------------------------------------------------------- commands

def cmd_bus(args):
    from sampo import cli
    me = cli.session()
    if args.action == "send":
        if len(args.words) < 2:
            raise cli.UsageError('fm bus send <session|all> "<text>" [--type note|steer|stop] [--wake]')
        msg = send(args.words[0], " ".join(args.words[1:]), args.type, me)
        woke = [s for s in ([x["session_id"] for x in live()] if msg["to"] == "all" else [msg["to"]])
                if args.wake and wake(s)]
        return cli.out(args, dict(msg, woke=woke), f"Sent to {msg['to']}" + (f"; woke {len(woke)}" if woke else "")
                         + ".")
    if args.action == "watch":
        import signal
        import sys
        for s in (signal.SIGTERM, signal.SIGHUP):  # T-0819: Claude Code ends a monitor with a signal; exit runs finally
            signal.signal(s, lambda *_: sys.exit(0))
        watch(args.words[0] if args.words else None)
        return 0
    msgs = unread(me or (args.words[0] if args.words else ""), mark=True)
    return cli.out(args, {"messages": msgs}, render(msgs) if msgs else "No unread mail.")


def cmd_lease(args):
    from sampo import cli
    p = cli.resolve(args)
    me = _sid(cli.session()) or "user"
    with c.lock(_dir()):
        data = _leases()
        before = dict(data)
        briefs = c.load_briefs(p) if args.action in ("take", "drop") else []
        act = c.active_brief(briefs, p.lane) if briefs else None
        open_ids = {b.id for b in briefs if b.status not in c.CLOSED}

        def mine(v):  # T-1216, T-1416: the active task's only: builder lanes share their main session's id
            if v["project"] != p.slug or v["session"] != me:
                return False
            if act is not None:
                return v.get("task") in (act.id, None)
            return v.get("task") not in open_ids  # T-2870: no active task: never another open task's lease
        if args.action == "drop" and not args.target:  # T-1134: bare drop releases what this session holds here
            data = {k: v for k, v in data.items() if not mine(v)}
            _write_leases(data)
        elif args.action in ("take", "drop"):
            if not args.target:
                raise cli.UsageError(f"fm lease {args.action} FILE[::SYMBOL]...")
            todo = []
            for t in args.target:
                rel, _, sym = t.partition("::")
                if not os.path.exists(rel) and os.path.exists(os.path.join(p.root, rel)):  # T-1148: root-relative from a subdir
                    rel = os.path.join(p.root, rel)
                rel = os.path.relpath(os.path.abspath(rel), p.root)
                todo.append((rel, sym or "*"))
            hits = [(f"{r}::{s}", v) for r, s in todo for v in claim_overlaps(p, r, s, me, act, data)]
            # T-3283: a lane's pre-claim (lane-<id>) is advisory, as edits treat it (T-1138): a take notes it, never refuses
            lanes = [(t, v) for t, v in hits if v["session"].startswith("lane-")]
            hits = [(t, v) for t, v in hits if not v["session"].startswith("lane-")]
            if args.action == "take" and hits and not args.warn:  # T-1068: refuse the whole claim, taking none of it
                if getattr(args, "json", False):
                    cli.out(args, {"refused": [{"target": t, "session": v["session"], "task": v.get("task")}
                                                 for t, v in hits]}, None)
                    return 1
                raise cli.UsageError("; ".join(f"{t} overlaps {_who(v)}" for t, v in hits)
                                       + " (--warn takes it anyway)")
            for t, v in hits if args.action == "take" else []:
                print(f"warning: {t} overlaps {_who(v)}", file=sys.stderr)
            for t, v in lanes if args.action == "take" else []:
                print(f"note: {t} overlaps lane pre-claim {_who(v)}; taken (pre-claims are advisory)", file=sys.stderr)
            for rel, sym in todo:
                if args.action == "drop":  # T-1216: a file named alone drops each of its symbol leases too
                    data = {k: v for k, v in data.items() if not (mine(v) and v["file"] == rel
                                                                   and (sym == "*" or v["symbol"] == sym))}
                else:
                    _claim(data, p, me, act.id if act else None, rel, sym, args.minutes * 60,
                           advisory=any(not v["session"].startswith("lane-")  # T-3283: a lane's pre-claim alone isn't a hold
                                        for v in claim_overlaps(p, rel, sym, me, act, data)))
            _write_leases(data)
    rows = [v for v in data.values() if v["project"] == p.slug]
    if args.action == "drop":  # T-1205, T-1729: what this drop released, only that (fm lease list shows the rest)
        gone = [f"{v['file']}::{v['symbol']}" for k, v in before.items() if k not in data]
        # T-1731 review: a lane's pre-claims are held under lane-<ID>, not this session: say they stay
        lane = sum(1 for v in rows if act and v.get("task") == act.id and v["session"] != me) if not args.target else 0
        return cli.out(args, {"leases": rows, "dropped": len(gone), "released": gone, "lane_claims": lane},
                         f"Dropped {len(gone)} lease(s)." + "".join(f"\ndropped: {x}" for x in gone[:8])
                         + (f"\n… {len(gone) - 8} more" if len(gone) > 8 else "")
                         + (f"\nstill held: {lane} lane claim(s) of {act.id}, freed when its lane merges or is removed "
                            "(fm lease list)" if lane else ""))  # T-1837: a bare count read as unfinished work
    if args.action == "take":  # T-1748: what this take claimed, only that (overlaps were warned on stderr)
        took = [f"{rel}::{sym}" for rel, sym in todo]
        return cli.out(args, {"leases": rows, "took": took},
                         f"Took {len(took)} lease(s)." + "".join(f"\ntook: {x}" for x in took[:8])
                         + (f"\n… {len(took) - 8} more" if len(took) > 8 else ""))
    return cli.out(args, {"leases": rows}, "\n".join(
        f"{v['file']}::{v['symbol']}  {_who(v)}"
        + f", {int((v['until'] - time.time()) // 60) + 1} min left" for v in rows) or "No leases held.")


REMOTE_S = 60  # a remote's tiles are refreshed (detached, over the user's own ssh) when older than this
REMOTE_FM = "$HOME/.claude/foreman/plugin/bin/fm"


def rows(sessions=None):
    """The conductor's view of each live session here."""
    leases = _leases().values()
    out = []
    for s in live() if sessions is None else sessions:
        sid = s["session_id"]
        p = c.find_project(s["cwd"]) if s.get("cwd") and os.path.isdir(s["cwd"]) else None
        act = c.active_brief_fast(p, p.lane) if p else None  # T-3580: not every brief parsed per session per pane poll
        out.append({"session": sid, "project": s.get("project"), "task": act.id if act else None,
                    "title": c.fit(act.title, 80) if act else None, "context_pct": s.get("context_pct"),
                    "seen": s.get("ts"), "tmux": bool(s.get("tmux_pane")), "mail": len(unread(sid, mark=False)),
                    "leases": sum(v["session"] == sid for v in leases)})
    return out


def remotes():
    """{name: {"target": ssh target, "fm": its fm path}}: only machines the user added (fm conductor remote add)."""
    try:
        with open(os.path.join(_dir(), "remotes.json")) as f:
            data = json.load(f)
        return {k: v for k, v in data.items() if isinstance(v, dict) and v.get("target")}
    except (OSError, ValueError):
        return {}


def _ssh(r, command, timeout=20):
    return subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", r["target"], command],
                          capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)


def refresh(name):
    """Fetch a remote's live sessions into its cache (run detached by fleet())."""
    r = remotes().get(name)
    if not r:
        return
    try:
        out = _ssh(r, f"{r.get('fm') or REMOTE_FM} conductor --json")
        data = {"ts": c.now(), "sessions": json.loads(out.stdout).get("sessions") or []} if not out.returncode else \
            {"ts": c.now(), "sessions": [], "error": c.fit(out.stderr.strip() or f"exit {out.returncode}", 160)}
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        data = {"ts": c.now(), "sessions": [], "error": c.fit(str(e), 160)}
    c.write_atomic(os.path.join(_dir(), f"remote-{name}.json"), json.dumps(data))


def fleet():
    """Live sessions here and the cached tiles of each added remote; a stale cache starts a detached refresh."""
    import datetime
    out = rows()
    for name in remotes():
        path = os.path.join(_dir(), f"remote-{name}.json")
        try:
            with open(path) as f:
                cache = json.load(f)
        except (OSError, ValueError):
            cache = {}
        try:
            age = (datetime.datetime.now(datetime.timezone.utc) - datetime.datetime.fromisoformat(
                str(cache.get("ts")).replace("Z", "+00:00"))).total_seconds()
        except ValueError:
            age = None
        if (age is None or age > REMOTE_S) and not os.environ.get("FOREMAN_NO_BACKGROUND"):
            subprocess.Popen([os.path.join(c.PLUGIN_ROOT, "bin", "fm"), "conductor", "refresh", name],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        out += [dict(x, remote=name, age_s=round(age) if age is not None else None) for x in cache.get("sessions") or []
                if isinstance(x, dict)]
        if cache.get("error"):
            out.append({"session": "-", "remote": name, "error": cache["error"]})
    return out


def cmd_conductor(args):
    from sampo import cli
    import shlex
    if args.action == "remote":
        data = remotes()
        if args.words[:1] == ["add"] and len(args.words) in (3, 4):
            name, target = args.words[1], args.words[2]
            if not re.fullmatch(r"[\w.-]{1,40}", name) or not re.fullmatch(r"[\w.@:-]{1,200}", target):
                raise cli.UsageError("fm conductor remote add NAME [user@]host [FM_PATH]")
            data[name] = {"target": target, "fm": args.words[3] if len(args.words) == 4 else REMOTE_FM}
        elif args.words[:1] == ["rm"] and len(args.words) == 2:
            data.pop(args.words[1], None)
            try:
                os.unlink(os.path.join(_dir(), f"remote-{args.words[1]}.json"))
            except OSError:
                pass
        elif args.words:
            raise cli.UsageError("fm conductor remote [add NAME [user@]host [FM_PATH] | rm NAME]")
        if args.words:
            c.write_atomic(os.path.join(_dir(), "remotes.json"), json.dumps(data, indent=1))
        return cli.out(args, {"remotes": data}, "\n".join(f"{k}: {v['target']}" for k, v in data.items())
                         or "No remotes (fm conductor remote add NAME user@host). Nothing is fetched from elsewhere.")
    if args.action == "refresh":
        for name in args.words or list(remotes()):
            refresh(name)
        return 0
    sessions = live()
    if args.action == "send":  # T-3932: one remote the user added, or one live session by its id prefix
        if len(args.words) < 2:
            raise cli.UsageError('fm conductor send <session|remote> "<text>"')
        name, text = args.words[0], " ".join(args.words[1:])
        r = remotes().get(name)
        if r:
            try:
                ok = not _ssh(r, f"{r.get('fm') or REMOTE_FM} bus send all {shlex.quote(text)}").returncode
            except (OSError, subprocess.SubprocessError):
                ok = False
            if not ok:
                raise cli.UsageError(f"{name} did not take the message (ssh or its fm failed)")
            return cli.out(args, {"remote": name}, f"Sent to {name}.")
        ids = [s["session_id"] for s in sessions]
        hits = [i for i in ids if _sid(name) and i.startswith(_sid(name))]
        if len(hits) == 1:
            msg = send(hits[0], text, "note", cli.session())
            return cli.out(args, {"session": msg["to"]}, f"Sent to session {msg['to'][:8]}.")
        if len(hits) > 1:
            raise cli.UsageError(f"{name} matches {len(hits)} live sessions: give more of the id")
        known = [f"remote {k}" for k in remotes()] + [f"session {i}" for i in ids]
        raise cli.UsageError(f"No remote or live session named {name}. Known: {', '.join(known) or 'none'}")
    if args.action == "steer":
        if not args.words:
            raise cli.UsageError("fm conductor steer \"<text>\" [--wake]")
        text = " ".join(args.words)
        send("all", text, "steer", cli.session())
        woke = [s["session_id"] for s in sessions if args.wake and wake(s["session_id"])]
        reached = []
        for name, r in remotes().items():  # the user's own machines, added by them
            try:
                ok = not _ssh(r, f"{r.get('fm') or REMOTE_FM} bus send all {shlex.quote(text)} --type steer").returncode
            except (OSError, subprocess.SubprocessError):
                ok = False
            reached.append(name) if ok else None
        return cli.out(args, {"sessions": len(sessions), "woke": woke, "remotes": reached},
                         f"Steer sent to {len(sessions)} live session(s)" + (f" and {', '.join(reached)}" if reached
                                                                             else "")
                         + (f"; woke {len(woke)}" if woke else "") + ".")
    found = rows(sessions)
    return cli.out(args, {"sessions": found}, ("Live sessions:\n" + "\n".join(
        f"  {r['session'][:8]}  {r['project'] or '-'}  {r['task'] or 'no task'}  ctx {r['context_pct']}%  "
        f"seen {str(r['seen'])[11:16]}  mail {r['mail']}  leases {r['leases']}" + ("  tmux" if r["tmux"] else "")
        for r in found)) if found else "No live sessions (none wrote a statusline in the last 15 min).")
