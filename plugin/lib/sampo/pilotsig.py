"""T-5708: a sampo:fm-builder's return starts one detached lane pilot per lane. The pilot runs `sampo lane pilot TID --json`
(T-5706's verb, the plugin's own bin run by this python; FOREMAN_PILOT_VERB overrides it for a test's stub) and its answer
becomes one pilot_done ledger event and one line to the lead, always, even when the settle fails. A failed run is an
exception with its reason, never a silent drop; a verb that is not there yet (argparse: invalid choice 'pilot', or the
lane's own unknown 'pilot') is quiet.
Single flight: the brief's pilot_pid claim names the live pilot, and a dead claim is retaken; pilot_for stamps the return
a claim serves and pilot_done_for the return it reported, so a return is reported once. Off under FOREMAN_NO_BACKGROUND,
when sampo is paused, or with SAMPO_LANE_PILOT=off.
T-5766: the same claim covers a headless run for a lane the land parked for a conflict, or whose builder ended uncommitted:
`resolve` starts `python -m sampo.pilotsig TID resolve`, which runs `claude -p --agent sampo:fm-resolver` (or fm-builder) in
the lane's worktree, at most RESOLVE_TRIES times per lane; a commit on a clean tree is a builder return (the pilot stamps
builder_returned itself, then spawns the review), otherwise the lane is reported once and left. Review round 1: the
headless claude is confined as a builder (its environment carries core.HEADLESS_ENV; it reads only the user's own settings
and MCP servers, never the lane's committed ones), bounded in turns and dollars, at most MAX_LIVE at once across lanes,
and not started over BUDGET_STOP percent of a usage window; its pid is recorded on the brief (claude_pid), so a pilot
killed mid-run leaves a claim that still reads held. Resolvers start only from the land's park hook and `tick` (never
from a read path); FOREMAN_PILOT_CLAUDE is honoured only as one executable file under the state dir."""
import functools
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback

from sampo import core as c

SWITCH = "SAMPO_LANE_PILOT"  # off | 0 | no | false: no pilot starts, and the lead reads each lane as before
OFF = ("off", "0", "no", "false")
NICE = 19  # background work: the user's session goes first (train.spawn_land, selfcheck._spawn)
VERB_WALL_S = 2 * 3600  # a lane's review and land: past this the verb's process group is killed and reported
ANSWER_MAX = 64 * 1024  # the verb's answer: its last JSON line is read from the last this many bytes
ERR_MAX = 4 * 1024  # the verb's stderr: its last bytes name the exit
WHY_MAX = 300  # the reason reaches the lead's line and the task's Log, so it is bounded
LOG_MAX = 256 * 1024  # pilot.log past this is kept aside as pilot.log.1 before the next pilot appends
TRIES = 3  # a busy project lock is tried this many times, as the builder mark is
OUTCOMES = ("queued", "exception")
PREFIX = "pilot-"  # a pilot's ledger session: pilot-<TID>
VERB_ENV = "FOREMAN_PILOT_VERB"  # a test's stub of the verb (a SAMPO_ name would be mirrored to FOREMAN_ at core's import)
ABSENT = re.compile(r"(?:invalid choice: |unknown )'pilot'")  # the answer from a sampo without the verb yet (argparse or the lane's own)
TID_RE = re.compile(r"T-\d+")
RESOLVE_ARG = "resolve"  # T-5766: `python -m sampo.pilotsig TID resolve` is the headless resolver pilot
RESOLVE_TRIES = 2  # T-5766: headless runs a lane gets in all (lanes.RECOVERIES_MAX's number); then it is reported
CLAUDE_ENV = "FOREMAN_PILOT_CLAUDE"  # a test's stub of the headless claude: one executable file under the state dir
CLAUDE_NEEDLE = b"--agent"  # the headless claude's command line names it (the claim's /proc check, as for the pilot's own)
MAX_TURNS = 60  # one headless run's turns (claude -p --max-turns)
MAX_USD = "5"  # one headless run's dollars (claude -p --max-budget-usd)
MAX_LIVE = 2  # headless runs alive at once across all lanes
BUDGET_STOP = 80  # percent of a usage window past which no headless run starts
NAME_MAX = 120  # a conflicted file's name in the prompt is cut to this many characters
KIND = "resolve"  # the brief's pilot_kind while a headless resolver pilot holds the claim (MAX_LIVE counts these)


def session(tid):
    """The pilot's own ledger session for a task: its events are the pilot's, not the lead's turns."""
    return f"{PREFIX}{tid}"


def is_pilot(sid):
    return str(sid or "").startswith(PREFIX)


def enabled():
    return os.environ.get(SWITCH, "on").strip().lower() not in OFF


def _off(p):
    """True when no pilot starts: background off, the switch off, the machine-wide pause, or this project's own pause (meta
    "paused", set by a message that is just "pause"; the check ahead.py and hooks.py use). A meta that can't be read is a
    pause: a paid run never starts on a doubt."""
    if os.environ.get("FOREMAN_NO_BACKGROUND") or not enabled() or c.panicked():
        return True
    try:
        return bool(c.read_meta(p).get("paused"))
    except Exception:  # noqa: BLE001
        return True


def verb_path():
    """The verb this python runs: the plugin's own bin/sampo (this file's plugin folder), or a test's stub."""
    here = os.path.dirname(os.path.abspath(__file__))  # plugin/lib/sampo
    return os.environ.get(VERB_ENV) or os.path.join(os.path.dirname(os.path.dirname(here)), "bin", "sampo")


def held(b):
    """T-5766 review: whether a brief's claim is held: its pilot lives, or the headless claude it started (claude_pid, kept
    when the pilot was killed and the claude, in a session of its own, ran on) does."""
    if alive(b.meta.get("pilot_pid")) or alive(b.meta.get("claude_pid"), CLAUDE_NEEDLE):
        return True
    if b.meta.get("claude_pid"):
        return False  # its pid is known and gone
    try:  # a start claimed before the spawn, its pid not yet written (the pilot died between): held until the run's cap
        return time.time() - float(b.meta.get("claude_starting")) < VERB_WALL_S
    except (TypeError, ValueError):
        return False


def alive(pid, needle=b"sampo.pilotsig"):
    """A claim names a live pilot: the pid answers, and where /proc exists its command line is this module's (a reused
    pid, or a zombie whose command line is empty, is dead). `needle` is what that command line must hold."""
    try:
        pid = int(pid)  # a brief's meta is text on disk: "1234"
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        pass  # alive, another user's
    except OSError:
        return False
    if not os.path.isdir("/proc/self"):  # no /proc (macOS): the signal alone says it lives
        return True
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return needle in f.read()
    except OSError:
        return False


def _env(tid):
    """The pilot's environment: the caller's, minus test overrides, its own session, and sampo's modules on the path."""
    env = c.scrubbed_env(os.environ)
    env.pop("CLAUDE_CODE_SESSION_ID", None)  # session_id() reads this first: the pilot's session must win
    env["FOREMAN_SESSION_ID"] = env["FOREMAN_PILOT"] = session(tid)
    lib = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env["PYTHONPATH"] = os.pathsep.join(x for x in (lib, env.get("PYTHONPATH", "")) if x)
    return env


def _locked(fn):
    """fn() under the project lock, retried while another process holds it; the last busy try raises."""
    for attempt in range(TRIES):
        try:
            return fn()
        except c.LockTimeout:
            if attempt == TRIES - 1:
                raise


def _cwd(p):
    """The pilot runs in the main checkout: the lane's own worktree may be gone by the time the verb lands."""
    return c.main_worktree(p.root) or p.root


def _log(p):
    """pilot.log, capped: past LOG_MAX the old log is kept aside as pilot.log.1 before the next pilot appends."""
    path = os.path.join(p.dir, "pilot.log")
    try:
        if os.path.getsize(path) > LOG_MAX:
            os.replace(path, path + ".1")
    except OSError:
        pass  # no log yet
    return path


def spawn(p, tid):
    """The one detached pilot for a task's lane: its pid, or None when none starts (background off, sampo paused, the
    switch off, the task closed or gone, a live pilot already holds the claim, or this return was reported already). The
    claim is written after the start, under the project lock, and names the return it serves (pilot_for)."""
    if _off(p):
        return None

    def claim_and_start():
        with c.lock(p.dir, timeout=5.0):
            b = c.find_brief(p, tid)
            if b is None or b.status in c.CLOSED or held(b):
                return None
            served = str(b.meta.get("builder_returned") or "")
            if served and b.meta.get("pilot_done_for") == served:
                return None  # the pilot of this return settled: a retake would report it twice
            argv = c.nice_argv([sys.executable, "-m", "sampo.pilotsig", tid], NICE)
            with open(_log(p), "a", encoding="utf-8") as log:
                proc = subprocess.Popen(argv, cwd=_cwd(p), stdin=subprocess.DEVNULL, stdout=log,
                                        stderr=subprocess.STDOUT, start_new_session=True, env=_env(tid))
            b.meta["pilot_pid"] = str(proc.pid)  # text, as every meta value is on disk
            b.meta.pop("pilot_kind", None)  # a review pilot, not a resolver (MAX_LIVE counts resolvers)
            b.meta["pilot_for"] = served
            c.save_brief(p, b)
            return proc.pid
    return _locked(claim_and_start)


def returned(pl):
    """The SubagentStop hook's call (after the builder_returned stamp): one spawn per open brief this builder's lane
    returned. A reviewer's stop, or a stop with no lane, starts none. Returns the pids started."""
    aid = str(pl.get("agent_id") or "")
    if pl.get("agent_type") not in c.ns_names("fm-builder") or not re.fullmatch(r"[\w-]{4,64}", aid):
        return []
    p = c.find_project(pl.get("cwd") or os.getcwd())
    if not p:
        return []
    started = []
    for b in c.load_briefs(p):
        mine = str(b.meta.get("lane") or "").rstrip("/").endswith(f"agent-{aid}")
        if mine and b.status not in c.CLOSED and b.meta.get("builder_returned"):
            pid = spawn(p, b.id)
            if pid:
                started.append(pid)
    return started


def _exception(why):
    return {"outcome": "exception", "rounds": 0, "why": why[:WHY_MAX]}


def _tail(f, cap):
    """The last `cap` bytes of a temporary file, decoded: a bounded read whatever the verb wrote."""
    f.seek(0, os.SEEK_END)
    f.seek(max(0, f.tell() - cap))
    return f.read().decode("utf-8", "replace")


def _kill_group(proc):
    """The verb's children die with it: its session is its own process group, and the group is killed whole."""
    pid = proc.pid
    if type(pid) is not int or pid <= 1:  # T-5899: killpg(1) is every process of the user; a Mock's pid converts to 1
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        pass  # the group is already gone


def _verb(p, tid):
    """The verb's answer as {outcome, rounds, why}. A verb that is not there yet is outcome 'absent' (quiet); any other
    failure, a missing or unreadable answer, or a run past VERB_WALL_S is an exception naming why."""
    verb = verb_path()
    if not os.path.isfile(verb):
        return _exception("pilot could not run: no sampo bin to run")
    argv = [sys.executable, verb, "lane", "pilot", tid, "--json"]
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            proc = subprocess.Popen(argv, cwd=_cwd(p), stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                    start_new_session=True, env=_env(tid))
        except OSError as e:
            return _exception(f"pilot could not run: {e}")
        try:
            rc = proc.wait(timeout=VERB_WALL_S)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            proc.wait()
            return _exception(f"pilot could not run: no answer in {VERB_WALL_S} s")
        _kill_group(proc)  # a verb that ended with children still running: they are not left behind
        text = _tail(out, ANSWER_MAX)
        errs = " ".join(_tail(err, ERR_MAX).split())
    if rc == 2 and ABSENT.search(errs):
        return {"outcome": "absent", "rounds": 0, "why": ""}
    if rc != 0:
        tail = errs[-WHY_MAX:]
        return _exception(f"pilot could not run: exit {rc}" + (f": {tail}" if tail else ""))
    lines = [x for x in text.splitlines() if x.strip()]
    try:
        data = json.loads(lines[-1]) if lines else None  # the answer is the verb's last line; its log lines come before
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return _exception("pilot could not run: no JSON answer")
    if data.get("tid") not in (None, tid):
        return _exception("pilot could not run: answered for another task")
    outcome = data.get("outcome")
    if outcome not in OUTCOMES:
        return _exception(f"pilot could not run: unknown outcome {str(outcome)[:40]!r}")
    rounds = data.get("rounds")
    return {"outcome": outcome, "rounds": rounds if type(rounds) is int and rounds >= 0 else 0,
            "why": " ".join(str(data.get("why") or "").split())[:WHY_MAX]}


def _settle(p, tid, res, served):
    """Under one project lock: the pilot_done event, the stamp that this return is reported, the exception on the task's
    Log, and the claim dropped when it is still this pilot's. False when this return was reported already."""
    with c.lock(p.dir, timeout=5.0):
        b = c.find_brief(p, tid)
        if b is None:
            return False
        if served and b.meta.get("pilot_done_for") == served:
            return False
        if str(b.meta.get("pilot_pid")) == str(os.getpid()):
            b.meta.pop("pilot_pid", None)
        if served:
            b.meta["pilot_done_for"] = served
        if res["outcome"] == "exception":
            b.append_log(f"lane pilot: {res['why']}")
        c.save_brief(p, b)
        c.log_event(p, "pilot_done", task=tid, data={"outcome": res["outcome"], "rounds": res["rounds"],
                                                      "why": res["why"], "seconds": res["seconds"]},
                    session=session(tid))
    return True


def _release(p, tid):
    """An absent verb: this pilot is gone, so its claim is dropped; nothing is logged and the lead gets no line."""
    def drop():
        with c.lock(p.dir, timeout=5.0):
            b = c.find_brief(p, tid)
            if b is not None and str(b.meta.get("pilot_pid")) == str(os.getpid()):
                b.meta.pop("pilot_pid", None)
                c.save_brief(p, b)
    try:
        _locked(drop)
    except Exception:
        traceback.print_exc()  # the stderr is pilot.log; the claim stays and the next return retakes it


def report(p, tid, res, served):
    """The lead's one line, always: the settle under the project lock first; a failed settle is a traceback in pilot.log
    and the line still goes (a `finally`). A return reported already sends nothing."""
    from sampo import selfcheck
    n = res["rounds"]
    if res["outcome"] == "queued":
        text = f"{tid}: queued to land, fixed in {n} {'round' if n == 1 else 'rounds'} ({res['seconds']} s)"
    else:
        text = f"{tid}: lane pilot exception: {res['why'] or 'no reason given'}"
    fresh = True
    try:
        fresh = _locked(lambda: _settle(p, tid, res, served))
    except Exception:
        traceback.print_exc()
    finally:
        if fresh:
            selfcheck.notify_detached(p, c.redact(text))


def run(p, tid):
    """One pilot run for a task: the verb, then the report, once per return. Returns the answer it reported."""
    b = c.find_brief(p, tid)
    served = str(b.meta.get("pilot_for") or "") if b else ""
    if served and b.meta.get("pilot_done_for") == served:
        return {"outcome": "already", "rounds": 0, "why": "", "seconds": 0.0}
    t0 = time.monotonic()
    res = _verb(p, tid)
    res["seconds"] = round(time.monotonic() - t0, 1)
    if res["outcome"] == "absent":
        _release(p, tid)
        return res
    report(p, tid, res, served)
    return res


def claude_cmd():
    """The headless claude the resolver starts, as an argv prefix: `claude`, or FOREMAN_PILOT_CLAUDE (a test's stub). The
    override is honoured only as one executable file under the Sampo state dir (a link is followed first), never as a shell
    line: a stray or planted variable runs nothing else, and an override that fails the test is [] (no claude to run)."""
    stub = os.environ.get(CLAUDE_ENV)
    if not stub:
        return ["claude"]
    real = os.path.realpath(stub)
    root = os.path.realpath(c.state_dir()) + os.sep
    return [real] if real.startswith(root) and os.path.isfile(real) and os.access(real, os.X_OK) else []


def claude_found():
    cmd = claude_cmd()
    return bool(cmd) and shutil.which(cmd[0]) is not None


def _name(f):
    """A conflicted file's name as data in the prompt: control characters (a newline starts a new instruction line, an escape
    repaints a terminal) become spaces, instruction-like text is marked, the name is cut to NAME_MAX."""
    return c.defang(c.fit(" ".join(re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", str(f)).split()), NAME_MAX))


def _plugin_dir(own=None):
    """The plugin the headless claude loads (--plugin-dir): this one (plugin/lib/sampo -> plugin), or, when this pilot runs from
    a lane's own copy (a bare `sampo` in a lane runs the lane's plugin: py.lane_reexec), the main checkout's same folder.
    T-5868: Claude Code refuses an Edit or Write of any file under a --plugin-dir root as "a sensitive file" (the plugin's code
    runs as hooks and commands) and with -p nobody approves it, so a lane's plugin/ held its own resolver's conflicted files
    out of reach; the lane's plugin also holds the very modules and hooks the conflict broke. The main checkout's is the
    installed one. No main checkout, or none with that folder: the pilot's own."""
    own = own or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    try:
        root = c.git_root(own)
        main = c.main_worktree(root) if root else None
        if not main:
            return own
        cand = os.path.join(main, os.path.relpath(os.path.realpath(own), os.path.realpath(root)))
        return cand if os.path.isfile(os.path.join(cand, ".claude-plugin", "plugin.json")) else own
    except Exception:  # any doubt (git gone, a timeout, a path that is not under its root): the pilot's own, as before
        return own


def _conflict_prompt(p, b, lane, main):
    """The recover brief for a conflict (lanes.recover's): what clashed, and the one move. Its words are data to the agent."""
    from sampo import lanes
    files = next(((e.get("data") or {}).get("files") or [] for e in reversed(c.ledger_tail(p, 500))
                  if e.get("task") == b.id and e.get("event") == "lane_conflict"), [])
    files = [_name(f) for f in (files if isinstance(files, list) else [])[:200]]  # repo-controlled names: data, bounded
    brief = os.path.join(p.dir, "audits", f"{b.id}.conflict.md")
    trunk = _name(lanes._git(main, "rev-parse", "--abbrev-ref", "HEAD").stdout) or "HEAD"  # a branch name is repo-controlled too
    return (f"Resolve the merge conflict of {b.id} in your worktree {lane}. Merging this branch into {trunk} conflicted in: "
            f"{c.joinmore(files, 20) or 'see the conflict brief'}. Run `git merge {trunk}` in your worktree, resolve the "
            f"clash keeping both sides' intent, run the task's tests and commit. The task's brief: "
            f"{os.path.join(p.dir, 'audits', f'{b.id}.builder.md')}."
            + (f" What clashed: {brief}" if os.path.exists(brief) else ""))


def plan(p, b):
    """(what, agent, cause, prompt) for the headless run a lane needs, or None. A lane the land parked for a conflict, whose
    builder has returned (or whose lane shows no file change since the park), gets the resolver; a lane whose builder's
    process ended with the work uncommitted (lanes.stop_causes) gets a builder on its brief. A refusal, a stall, a usage
    limit, a lane waiting on a merge, a guard (the user's grant) or a lane already moved on: None."""
    from sampo import bus
    from sampo import lanes
    lane = str(b.meta.get("lane") or "")
    if not lane or not os.path.isdir(lane) or b.status in c.CLOSED or b.status == "blocked":
        return None
    if lanes._ignored(lane):  # recover can't free a lane holding ignored files (the recovery line's own skip): a person reads it
        return None
    main = c.main_worktree(p.root) or p.root
    kind, at, _ = lanes._stop(b)
    if kind == "conflict" and lanes.parked(b):
        if bus.proc_state(b.meta.get("builder_proc")) == "alive" and not (
                b.meta.get("builder_returned") and lanes._newest(lane, lanes._git) <= at + 2):
            return None  # the launching session lives and the lane shows work since the park: a builder may be in it
        return ("merge conflict", c.ns_names("fm-resolver")[0], "merge_conflict", _conflict_prompt(p, b, lane, main))
    got = lanes.stop_causes(p, [b]).get(b.id)
    if got and got[0] == "uncommitted":
        brief = os.path.join(p.dir, "audits", f"{b.id}.builder.md")
        return ("stopped lane", c.ns_names("fm-builder")[0], "uncommitted",
                f"Read {brief} and work the task it describes. The previous builder's process ended before it reported: "
                f"its work is in your worktree {lane}; finish it, run the task's tests and commit.")
    return None


def _resolves(b):
    got = b.meta.get("resolves") or []
    return got if isinstance(got, list) else [got]


def _stuck(p, tid, why, what, tries):
    """The lead's one line when the pilot gives up on a lane (the cap, or no claude to run), said once per reason: the
    brief's resolve_reported names the reason already told. The claim is dropped when it is still this pilot's."""
    if why == "claude":
        text = f"{tid}: lane pilot cannot resolve the {what}: no claude to run ({(claude_cmd() or ['claude'])[0]})"
    else:
        text = (f"{tid}: lane pilot resolve gave up on the {what} after {tries} "
                f"{'try' if tries == 1 else 'tries'}: the lane waits for a person")

    def mark():
        with c.lock(p.dir, timeout=5.0):
            b = c.find_brief(p, tid)
            if b is None:
                return False
            mine = str(b.meta.get("pilot_pid")) == str(os.getpid())
            if mine:
                b.meta.pop("pilot_pid", None)
            fresh = b.meta.get("resolve_reported") != why
            if fresh:
                b.meta["resolve_reported"] = why
                b.append_log(f"lane pilot: {text.partition(': ')[2]}")
            if mine or fresh:  # a capped lane is asked about at every park and tick: an unchanged brief is not written again
                c.save_brief(p, b)
            return fresh
    try:
        fresh = _locked(mark)
    except Exception:
        traceback.print_exc()
        fresh = False
    if fresh:
        try:
            from sampo import selfcheck
            selfcheck.notify_detached(p, c.redact(text))
        except Exception:
            traceback.print_exc()  # the alerts.jsonl row comes first: a failed desktop notice never stops the land
    return fresh


def budget_high():
    """True when a usage window is spent past BUDGET_STOP percent: a headless run is paid work, and it starts only while the
    user's own sessions have room (lanes._usage_wall stops at 100). A snapshot that can't be read is no reason to stop."""
    from sampo import budget
    try:
        return any(w["used"] >= BUDGET_STOP for w in budget.forecast().values())
    except Exception:
        return False


def tick(p):
    """The explicit pilot tick: `resolve` for each open lane that stopped with a conflict or uncommitted work, the pids
    started. Its callers are the land (the park hook at the land's end) and a resolver run's end (T-5876, so a park that
    waited on MAX_LIVE starts at once); with note_park's per-park `resolve` they are the only starts: the lead's reads
    (lanes.recovery_line, fm next, the drive) never launch a paid session. One lane's failure costs none of the others."""
    from sampo import lanes
    started = []
    if _off(p):
        return started
    for tid, (cause, _) in sorted(lanes.stop_causes(p).items()):
        if cause not in ("uncommitted", "merge_conflict"):
            continue
        try:
            pid = resolve(p, tid)
        except Exception as e:  # noqa: BLE001 - the others are still asked
            print(f"lane pilot: {tid}: no resolver: {c.fit(str(e), 200)}", file=sys.stderr)
            continue
        if pid:
            started.append(pid)
    return started


def _start(argv, **kw):
    """The one detached resolver pilot's process (the seam a test replaces: git runs through Popen too)."""
    return subprocess.Popen(argv, **kw)


def resolve(p, tid):
    """The land's call for a lane it parked (and the lead's for a stopped one): one detached headless resolver or builder,
    its pid, or None when none starts. Under the pilot's own single-flight claim (a live pilot_pid starts none, so a second
    park within it is quiet) and its off switches (background off, paused, SAMPO_LANE_PILOT=off: quiet). A lane that has
    had RESOLVE_TRIES attempts, or a host with no claude, is reported once and started no more."""
    if _off(p):
        return None
    b = c.find_brief(p, tid)
    if b is None or b.status in c.CLOSED or held(b):
        return None
    pl = plan(p, b)
    if pl is None:
        return None
    what = pl[0]

    def claim_and_start():
        with c.lock(p.dir, timeout=5.0):
            b = c.find_brief(p, tid)
            if b is None or b.status in c.CLOSED or held(b):
                return None
            if len(_resolves(b)) >= RESOLVE_TRIES:
                return "cap"
            if not claude_found():
                return "claude"
            if budget_high() or sum(1 for x in c.load_briefs(p) if x.id != tid and x.meta.get("pilot_kind") == KIND
                                    and held(x)) >= MAX_LIVE:
                return None  # a quiet wait: the lane is the lead's recovery line until a window or a run frees
            argv = c.nice_argv([sys.executable, "-m", "sampo.pilotsig", tid, RESOLVE_ARG], NICE)
            with open(_log(p), "a", encoding="utf-8") as log:
                proc = _start(argv, cwd=_cwd(p), stdin=subprocess.DEVNULL, stdout=log,
                              stderr=subprocess.STDOUT, start_new_session=True, env=_env(tid))
            b.meta["pilot_pid"] = str(proc.pid)
            b.meta["pilot_kind"] = KIND
            b.meta.pop("claude_pid", None)  # a dead claude's pid from an earlier run (held() said it is gone)
            c.save_brief(p, b)
            return proc.pid
    got = _locked(claim_and_start)
    if got in ("cap", "claude"):
        _stuck(p, tid, got, what, RESOLVE_TRIES if got == "cap" else 0)
        return None
    return got


def _head(lane):
    from sampo import lanes
    return lanes._git(lane, "rev-parse", "HEAD", timeout=10).stdout.strip()


def _clean(lane):
    from sampo import lanes
    return not lanes._git(lane, "status", "--porcelain", "--untracked-files=no", timeout=30).stdout.strip()


def _unmerged(lane):
    """Whether the lane's index holds unmerged paths (a merge or rebase stopped on a conflict)."""
    from sampo import lanes
    return bool(lane and lanes._git(lane, "ls-files", "--unmerged", timeout=30).stdout.strip())


def _claude_env(lane=None, agent=""):
    """The headless agent's environment: the pilot's, as its own main session (the pilot's session id is not its), marked
    (core.HEADLESS_ENV) so the hooks confine that main session to the lane as they do a subagent builder. T-5868: for the
    resolver, and for any agent while the lane has unmerged paths, its `sampo` runs the main install's plugin
    (SAMPO_LANE_CHILD ends py.lane_reexec's hand-over to the lane's own), which imports modules and cards that still hold
    conflict markers and fails. (The resolver's own `git merge` makes the conflict after it starts: the lane may be clean now.)"""
    env = c.scrubbed_env(os.environ)
    for k in ("CLAUDE_CODE_SESSION_ID", "FOREMAN_SESSION_ID", "FOREMAN_PILOT"):
        env.pop(k, None)
    env[c.HEADLESS_ENV] = "1"
    if agent in c.ns_names("fm-resolver") or _unmerged(lane):
        env["SAMPO_LANE_CHILD"] = "1"
    return env


def _note_claude(p, tid, pid):
    """The headless claude's run on the brief, so a pilot killed mid-run, whose claude runs on in a session of its own,
    still leaves a claim that reads held (`held`). pid "starting": the start is claimed before the spawn (claude_starting,
    no pid yet); a pid: it runs; None: it ended. True when the brief says so (a start the brief cannot record is not made)."""
    def mark():
        with c.lock(p.dir, timeout=5.0):
            b = c.find_brief(p, tid)
            if b is None:
                return False
            if pid == "starting":
                b.meta["claude_starting"] = str(int(time.time()))
                b.meta.pop("claude_pid", None)
            elif pid:
                b.meta["claude_pid"] = str(pid)
            else:
                b.meta.pop("claude_pid", None)
                b.meta.pop("claude_starting", None)
            c.save_brief(p, b)
            return True
    try:
        return bool(_locked(mark))
    except Exception:
        traceback.print_exc()
        return False


def _headless(p, tid, lane, agent, prompt, left):
    """One `claude -p` run of `agent` in the lane's own worktree, cut (its process group killed) after `left` seconds.
    True when it ended inside the time; its output goes to pilot.log (stdout) as its last bytes. It reads only the user's
    settings and MCP servers (the lane's own .claude/settings.json and .mcp.json are repo-controlled, and `-p` trusts the
    folder without asking) and is bounded in turns and dollars."""
    argv = [*claude_cmd(), "-p", prompt, "--agent", agent, "--plugin-dir", _plugin_dir(),
            "--setting-sources", "user", "--strict-mcp-config", "--max-turns", str(MAX_TURNS), "--max-budget-usd", MAX_USD,
            "--permission-mode", "acceptEdits", "--allowedTools", "Bash"]
    if not _note_claude(p, tid, "starting"):
        print("pilot resolve: the start could not be recorded on the brief; no claude is started")
        return False
    with tempfile.TemporaryFile() as out:
        try:
            proc = subprocess.Popen(argv, cwd=lane, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                    start_new_session=True, env=_claude_env(lane, agent))
        except OSError as e:
            _note_claude(p, tid, None)
            print(f"pilot resolve: claude could not start: {e}")
            return False
        _note_claude(p, tid, proc.pid)
        try:
            proc.wait(timeout=max(left, 1.0))
            ended = True
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            proc.wait()
            ended = False
        _kill_group(proc)
        _note_claude(p, tid, None)
        print(f"pilot resolve: {agent} {'ended' if ended else 'cut'}: " + " ".join(_tail(out, ERR_MAX).split())[-WHY_MAX:])
    return ended


def _returned(p, tid, what):
    """The resolver's exit as a builder return: under one lock the claim is dropped, builder_returned is stamped as the
    SubagentStop hook would (a headless main session has no such stop), and the park mark is cleared (the lane is taken
    back); then the pilot's own spawn reviews it, as it does a builder's return."""
    with c.lock(p.dir, timeout=5.0):
        b = c.find_brief(p, tid)
        if b is None:
            return
        if str(b.meta.get("pilot_pid")) == str(os.getpid()):
            b.meta.pop("pilot_pid", None)
        for k in ("lane_stop", "lane_stop_tip", "resolve_reported"):
            b.meta.pop(k, None)
        b.meta["builder_returned"] = c.now()
        b.append_log(f"lane pilot: a headless agent resolved the {what}; it returns to the review")
        c.save_brief(p, b)
        c.log_event(p, "pilot_resolve", task=tid, data={"what": what, "tries": len(_resolves(b))}, session=session(tid))


def _then_tick(fn):
    """T-5876: a resolver run's end hands its place to the next: after `fn` returns (any outcome) one `tick` starts a park
    that waited on MAX_LIVE. The run's own claim is already dropped, and RESOLVE_TRIES and MAX_LIVE bound what the tick starts.
    A run that raised drops its own claim first, so a dead claim holds no slot. A tick that fails never costs the run its
    answer."""
    @functools.wraps(fn)
    def wrapped(p, tid):
        try:
            return fn(p, tid)
        except BaseException:
            _release(p, tid)  # a no-op unless the claim is still this pilot's
            raise
        finally:
            try:
                tick(p)
            except Exception:  # noqa: BLE001
                traceback.print_exc()  # the stderr is pilot.log
    return wrapped


@_then_tick
def resolve_run(p, tid):
    """The detached resolver pilot: up to RESOLVE_TRIES headless runs for the lane (counted on its brief across parks),
    within VERB_WALL_S in all. A run that moved the lane (a new commit on a clean tree) is a builder return: _returned,
    then the review pilot. Otherwise the lane is reported and left. Returns {outcome: returned | failed | none}."""
    deadline = time.monotonic() + VERB_WALL_S
    for _ in range(RESOLVE_TRIES + 1):  # the last pass only reports: no unbounded loop if a count can't be saved
        b = c.find_brief(p, tid)
        if b is None or str(b.meta.get("pilot_pid")) != str(os.getpid()) or alive(b.meta.get("claude_pid"), CLAUDE_NEEDLE):
            return {"outcome": "none", "why": "the claim is not this pilot's"}  # a claim lost (retaken, recovered) or a claude
            # of an earlier pilot still running in the lane: no second claude; the claim is its holder's, not dropped here
        pl = plan(p, b) if b.status not in c.CLOSED else None
        if pl is None:
            _release(p, tid)
            return {"outcome": "none", "why": "nothing to resolve"}
        what, agent, cause, prompt = pl
        tries = len(_resolves(b))
        if tries >= RESOLVE_TRIES or time.monotonic() >= deadline:
            _stuck(p, tid, "cap", what, tries)
            return {"outcome": "failed", "why": f"{tries} tries"}
        if not claude_found():
            _stuck(p, tid, "claude", what, tries)
            return {"outcome": "failed", "why": "no claude"}
        if budget_high():  # a window filled while this pilot waited its turn: the lane is the lead's again, no try is used
            _release(p, tid)
            return {"outcome": "none", "why": "over the budget stop"}

        def count():
            with c.lock(p.dir, timeout=5.0):
                x = c.find_brief(p, tid)
                if x is not None:
                    x.meta["resolves"] = _resolves(x) + [f"{cause}@{c.now()}"]
                    c.save_brief(p, x)
        _locked(count)  # counted before the run: a pilot that dies mid-run still used its try
        lane = str(b.meta["lane"])
        before = _head(lane)
        _headless(p, tid, lane, agent, prompt, deadline - time.monotonic())
        if _head(lane) != before and _clean(lane):
            _returned(p, tid, what)
            spawn(p, tid)
            return {"outcome": "returned", "why": ""}
    _release(p, tid)
    return {"outcome": "failed", "why": "no count"}


def main(argv):
    """The detached pilot's entry: `python -m sampo.pilotsig TID [resolve]`, run from the project's folder."""
    if len(argv) not in (1, 2) or not TID_RE.fullmatch(argv[0]) or (len(argv) == 2 and argv[1] != RESOLVE_ARG):
        return 2
    p = c.find_project(os.getcwd())
    if not p:
        return 2
    (resolve_run if len(argv) == 2 else run)(p, argv[0])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
