"""T-5708: a sampo:fm-builder's return starts one detached lane pilot per lane. The pilot runs `sampo lane pilot TID --json`
(T-5706's verb, the plugin's own bin run by this python; FOREMAN_PILOT_VERB overrides it for a test's stub) and its answer
becomes one pilot_done ledger event and one line to the lead, always, even when the settle fails. A failed run is an
exception with its reason, never a silent drop; a verb that is not there yet (argparse: invalid choice 'pilot', or the
lane's own unknown 'pilot') is quiet.
Single flight: the brief's pilot_pid claim names the live pilot, and a dead claim is retaken; pilot_for stamps the return
a claim serves and pilot_done_for the return it reported, so a return is reported once. Off under FOREMAN_NO_BACKGROUND,
when sampo is paused, or with SAMPO_LANE_PILOT=off."""
import json
import os
import re
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


def session(tid):
    """The pilot's own ledger session for a task: its events are the pilot's, not the lead's turns."""
    return f"{PREFIX}{tid}"


def is_pilot(sid):
    return str(sid or "").startswith(PREFIX)


def enabled():
    return os.environ.get(SWITCH, "on").strip().lower() not in OFF


def verb_path():
    """The verb this python runs: the plugin's own bin/sampo (this file's plugin folder), or a test's stub."""
    here = os.path.dirname(os.path.abspath(__file__))  # plugin/lib/sampo
    return os.environ.get(VERB_ENV) or os.path.join(os.path.dirname(os.path.dirname(here)), "bin", "sampo")


def alive(pid):
    """A claim names a live pilot: the pid answers, and where /proc exists its command line is this module's (a reused
    pid, or a zombie whose command line is empty, is dead)."""
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
            return b"sampo.pilotsig" in f.read()
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
    if os.environ.get("FOREMAN_NO_BACKGROUND") or not enabled() or c.panicked():
        return None

    def claim_and_start():
        with c.lock(p.dir, timeout=5.0):
            b = c.find_brief(p, tid)
            if b is None or b.status in c.CLOSED or alive(b.meta.get("pilot_pid")):
                return None
            served = str(b.meta.get("builder_returned") or "")
            if served and b.meta.get("pilot_done_for") == served:
                return None  # the pilot of this return settled: a retake would report it twice
            argv = c.nice_argv([sys.executable, "-m", "sampo.pilotsig", tid], NICE)
            with open(_log(p), "a", encoding="utf-8") as log:
                proc = subprocess.Popen(argv, cwd=_cwd(p), stdin=subprocess.DEVNULL, stdout=log,
                                        stderr=subprocess.STDOUT, start_new_session=True, env=_env(tid))
            b.meta["pilot_pid"] = str(proc.pid)  # text, as every meta value is on disk
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
    try:
        os.killpg(proc.pid, signal.SIGKILL)
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


def main(argv):
    """The detached pilot's entry: `python -m sampo.pilotsig TID`, run from the project's folder."""
    if len(argv) != 1 or not TID_RE.fullmatch(argv[0]):
        return 2
    p = c.find_project(os.getcwd())
    if not p:
        return 2
    run(p, argv[0])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
