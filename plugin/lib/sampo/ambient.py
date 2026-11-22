"""T-0705 (Frontier 05, first slice): ambient verification. After an edit, the affected tests (fm check --affected's
selection) run in a detached process, out of the model's turns. Only a flip reaches the model — passing tests that now
fail, or failing ones that pass again — as one note on its next tool call; a pass never closes a step (T-0994: that
closed steps mid-work), its note names the evidence line for when the step is done. Opt-in per project (fm check
ambient on); skipped on a strained host; nothing leaves the machine."""
import json
import os
import re

from sampo import core as c
from sampo import editverdict
from sampo.core import subprocess  # T-3577: imported on first use

RERUNS = 3  # edits that land during a run trigger at most this many reruns, then the next edit starts a new one
FAILED = c.lazy(r"^(?:FAIL|ERROR): (\S+)|^FAILED (\S+)", re.M)  # unittest 'FAIL: name (mod.Class.name)', pytest 'FAILED path::name'
RED_STEP = c.lazy(r"(?i)\b(fail|red\b|reproduc|repro\b)")  # a step that wants a red run, which a pass won't verify
ENDED_LINE = re.compile(r"^INCOMPLETE \(ended from outside\)\s*$", re.M)  # run.py's last line when an outside end ended it (T-4399)
RED_NOTE = "Ambient tests: ✗"  # the start of a red note: a no-verdict or skip note never replaces an unread one (T-4823)


def _dir(p):
    d = os.path.join(p.dir, "ambient")
    os.makedirs(d, exist_ok=True)
    return d


def after_edit(p, path):
    """The PostToolUse hook's call after an edit: start a run, or mark the running one dirty so it runs again."""
    if not c.read_meta(p).get("ambient") or not path.startswith(p.root.rstrip("/") + "/"):
        return
    try:  # T-3277: the file waits for the next run, which rules it keep/neutral/harmful; never breaks the hook
        editverdict.queue(p, path)
    except OSError:
        pass
    if c.host_strain() \
            or c.gates_live(p, here=True) \
            or c.gates_live(p, key="ship_testing"):  # T-1965, T-1984: no second suite beside full gates or fm ship's run
        # ponytail: an edit during fm ship's run waits for the next edit or the close gates, no run after the ship
        return
    if os.environ.get("FOREMAN_AMBIENT_SYNC"):  # tests: inline, so the result is there when the hook returns
        return run(p)
    if os.environ.get("FOREMAN_NO_BACKGROUND"):
        return
    if running(p):
        open(os.path.join(_dir(p), "dirty"), "w").close()
        return
    subprocess.Popen([os.path.join(c.PLUGIN_ROOT, "bin", "fm"), "check", "ambient", "run"], cwd=p.root,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


def running(p):
    """A run is live: run.pid holds "pid ticks" (T-5985: the start time too, so a reused pid is no live run; a file
    with the pid alone, from an older run, falls back to the pid answering)."""
    try:
        with open(os.path.join(_dir(p), "run.pid")) as f:
            parts = f.read().split()
        return c.same_process(int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)
    except (OSError, ValueError, IndexError):
        return False


def run(p):
    """Run the affected tests until no edit landed meanwhile (at most RERUNS more times); record flips and evidence."""
    from sampo import cli
    d = _dir(p)
    pid = os.path.join(d, "run.pid")
    try:
        fd = os.open(pid, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        if running(p):
            return
        os.unlink(pid)  # a run that died left it
        fd = os.open(pid, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        os.write(fd, f"{os.getpid()} {c.proc_ticks(os.getpid())}".encode())
        os.close(fd)
        for _ in range(1 + RERUNS):
            try:
                os.unlink(os.path.join(d, "dirty"))
            except OSError:
                pass
            edited = editverdict.take(p)  # T-3277: the files edited since the last run: this run rules them
            cmd, tests, _ = cli.affected(p)
            if not cmd:  # an edit that undid the change selects nothing; a failing last run still needs its answer
                cmd, tests = _last_failing(p)
                if not cmd and edited:  # nothing linked to what was edited: neutral, no run
                    _rule(p, edited, None, {}, 0, c.active_brief(c.load_briefs(p), p.lane))  # no run: neutral
            try:  # T-0901: the affected template is a stored command; one the guard refuses never runs here
                if cmd:
                    cli.guard_stored(p, None, cmd)
            except c.PolicyError as e:  # said where fm check --affected would say it, never silently
                c.log_event(p, "ambient_refused", data={"cmd": c.fit(cmd, 200), "why": c.fit(str(e), 300)})
                cmd = None
            if cmd:
                # T-4267: at nice 10 like the lane gates (T-4197): an edit's check never competes with a gate
                env, _ = cli._venv_env(p.root, cli._skip_env({}), cmd)  # T-4150: the project's own interpreter
                limit, said = _limit(p, cmd, tests, env)  # T-4823: sized from the set's recorded seconds (T-4788)
                if limit is None:  # too big for an early warning: no run, no record; one note per set, later skips only logged
                    c.log_event(p, "ambient_skipped", data={"tests": len(tests), "why": c.fit(said, 200)})
                    if _skipped(p) != cmd and _say(p, f"Ambient tests skipped: {c.fit(said, 200)}", keep_red=True):
                        c.write_atomic(os.path.join(_dir(p), "skipped"), cmd)
                else:
                    _forget_skip(p)  # a run starts the set again: its next skip is noted again
                    before = c.worktree_tree(p.root)  # T-3270: the verdict is for the tree the run began on
                    # T-5589: an early warning no one waits on: low, so a land's gate and a lane's criteria take freed slots first
                    code, output = c.run_command(p.root, cmd, limit, niced=True, env=env, low=True)
                    cut = _no_verdict(code, output, limit)
                    if cut:  # T-4823: no verdict is no red and no pass: last.json and the pass record stay as they were
                        _say(p, f"Ambient tests: {cut}: {c.fit(cmd, 150)}", keep_red=True)  # never over an unread red
                        c.log_event(p, "ambient_cut", data={"tests": len(tests), "exit": code, "limit": limit})
                    else:
                        _record(p, cmd, tests, code, output, edited)
                        _write_pass(p, cmd, before, code)
            editverdict.release(p)  # T-3277: a batch no ledger line was written for (skipped, cut, refused) waits again
            if not os.path.exists(os.path.join(d, "dirty")):
                break
    except Exception as e:  # never a crash in a detached run: one line in the folder says why
        c.write_atomic(os.path.join(d, "error.txt"), f"{type(e).__name__}: {e}\n")
    finally:
        try:
            editverdict.release(p)  # T-3277: a run that raised before ruling its batch leaves the edits queued
        except OSError:
            pass
        try:
            os.unlink(pid)
        except OSError:
            pass


def _limit(p, cmd, tests, env):
    """T-4823: (the run's limit in s, or None to skip it, what to say), sized as ship's affected stage is (T-4788): the
    set's recorded seconds (release._run_times, a child read of run.py's record) through release._affected_limit. A
    sizing that raises gets the old limit: a detached run never crashes on its own sizing."""
    from sampo import release
    try:
        times, suite, jobs = release._run_times(p.root, cmd, {k: v for k, v in env.items() if not k.startswith("GIT_")})
        return release._affected_limit(tests, times, suite, c.host_load(), jobs)
    except Exception as e:
        c.log_event(p, "ambient_sizing_failed", data={"why": c.fit(c.plain(str(e)), 200)})
        return release.AFFECTED_OLD, ""


def _no_verdict(code, out, limit):
    """T-4823: what a run that never reached a verdict says, or None when it has one. A red line in the output is a red
    whatever ended the run (a runner that printed FAIL and then hung still failed). Otherwise a run its limit ended
    exits 124 (run.py's own deadline, or the timeout run_command kills it at); an outside end exits 128 + the signal
    (130 or 143) and run.py says 'INCOMPLETE (ended from outside)' last; a test's own message saying so is no end (T-4399)."""
    from sampo import release
    out = out or ""
    if release._TEST_RED.search(out) or FAILED.search(out):
        return None
    if code == 124:
        return f"timed out after {limit} s (no verdict)"
    if code in (130, 143) or (code > 128 and ENDED_LINE.search(out)):
        return "ended from outside (no verdict)"
    return None


def _say(p, text, keep_red=False):
    """The note for the model's next tool call (one, then gone; see note). keep_red: a note that must not replace an
    unread red one (T-4823: a no-verdict or skip note never hides a flip of the same detached run). True when written."""
    path = os.path.join(_dir(p), "note.json")
    if keep_red and _unread_red(path):
        return False
    c.write_atomic(path, json.dumps({"text": text, "ts": c.now()}))
    return True


def _unread_red(path):
    try:
        with open(path) as f:
            text = json.load(f).get("text")
    except (OSError, ValueError, AttributeError):
        return False
    return isinstance(text, str) and text.startswith(RED_NOTE)


def _skipped(p):
    """T-4823: the command whose skip note was written last, until a run starts that set again (one note per set)."""
    try:
        with open(os.path.join(_dir(p), "skipped")) as f:
            return f.read()
    except OSError:
        return None


def _forget_skip(p):
    try:
        os.unlink(os.path.join(_dir(p), "skipped"))
    except OSError:
        pass


def _last_failing(p):
    try:
        with open(os.path.join(_dir(p), "last.json")) as f:
            last = json.load(f)
    except (OSError, ValueError):
        return None, []
    return (last.get("cmd"), last.get("tests") or []) if last.get("exit") else (None, [])


def _prev(p):
    """The exit of the run before this one (None: none yet)."""
    try:
        with open(os.path.join(_dir(p), "last.json")) as f:
            return json.load(f).get("exit")
    except (OSError, ValueError, AttributeError):
        return None


def _rule(p, files, code, linked, tests, act, prev=False):
    """T-3277: one ledger line per edited file, then the taken batch is done; the files ruled harmful (a linked one
    first: the note names it before the bystanders of its batch). A ledger that cannot be written never breaks the run
    (the flip note still goes out, the batch stays queued)."""
    try:
        rows = editverdict.record(p, files, _prev(p) if prev is False else prev, code, linked, act.id if act else None,
                                  tests)
        editverdict.done(p)
        return [r["file"] for r in sorted(rows, key=lambda r: not r["linked"]) if r["verdict"] == editverdict.HARMFUL]
    except Exception as e:
        c.log_event(p, "ambient_ledger_failed", data={"why": c.fit(c.plain(str(e)), 200)})
        return []


def _record(p, cmd, tests, code, output, edited=()):
    d = _dir(p)
    last = os.path.join(d, "last.json")
    prev = _prev(p)
    act = c.active_brief(c.load_briefs(p), p.lane)
    harmful = _rule(p, list(edited), code, editverdict.linked_for(p, edited, tests), len(tests), act, prev=prev) \
        if edited else []  # T-3277: ruled against the exit before this run, which last.json is about to replace
    c.write_atomic(last, json.dumps({"cmd": cmd, "tests": tests, "exit": code, "ts": c.now()}))
    cur = act.current_step() if act else None
    if code and (prev is None or not prev):
        # T-1275: the failing tests' own names, then the command; never the runner's timing or class counts
        bad = list(dict.fromkeys(m.group(1) or m.group(2) for m in FAILED.finditer(c.redact(output)[:200000])))
        head = f"Ambient tests: ✗ now fail ({', '.join(c.fit(b, 50) for b in bad[:3]) if bad else f'exit {code}'})"
        head += (f" after editing {editverdict.names(harmful)}" if harmful else "") + ": "
        text = head + c.fit(cmd, max(20, 220 - len(head)))
    elif not code and prev:
        # T-0994: a pass is no proof the step is done (recorded as evidence, it closed steps mid-work); when it is:
        text = "Ambient tests: ✓ affected tests pass again."
        if cur and not RED_STEP.search(cur.text):
            tail = f" When step {cur.n} is done: fm task evidence {act.id} --step {cur.n} --run \""
            text += tail + c.fit(cmd, max(20, 200 - len(text) - len(tail) - 2)) + "\"."
    else:
        text = None
    if text:
        _say(p, text)
    c.log_event(p, "ambient_run", task=act.id if act else None, data={"tests": len(tests), "exit": code,
                                                                       "flip": bool(text)})


def _write_pass(p, cmd, before, code):
    """T-3270: the run's verdict for the close, kept only when the working tree is the one the run began on and no edit
    landed during the run (the dirty marker: an edit then a revert leaves the tree id as it was). An edit, or a file a
    test wrote during the run, moves the tree: then the run answers for neither tree and nothing is kept."""
    if before is None or os.path.exists(os.path.join(_dir(p), "dirty")) or c.worktree_tree(p.root) != before:
        return
    c.write_atomic(os.path.join(_dir(p), "verdict.json"), json.dumps({"cmd": cmd, "tree": before, "exit": code,
                                                                     "env": c.env_id(), "ts": c.now()}))


def passed(p, cmd):
    """T-3270: True when the last ambient verdict for this command passed on the working tree as it is now, in this
    environment (c.env_id, as the gate cache keys on it) and no older than the gate cache's window (cli.CACHE_DAYS).
    The close then skips its own run; a red verdict, another command, another environment, a stale one or a moved tree
    reruns it. Limit: only the tree id is compared, and the ignored files are not in it, so a gitignored input's change
    is not seen."""
    from sampo import cli
    try:
        with open(os.path.join(p.dir, "ambient", "verdict.json")) as f:
            v = json.load(f)
    except (OSError, ValueError):
        return False
    if not isinstance(v, dict) or v.get("cmd") != cmd or v.get("exit") != 0 or not v.get("tree") \
            or v.get("env") != c.env_id():
        return False
    age = c.age_days(v.get("ts"))
    if age is None or age >= cli.CACHE_DAYS:
        return False
    return c.worktree_tree(p.root) == v["tree"]


def note(p):
    """The flip waiting for the model, once: the PreToolUse hook's note, then it's gone."""
    path = os.path.join(p.dir, "ambient", "note.json")
    try:
        with open(path) as f:
            text = json.load(f).get("text")
        os.unlink(path)
    except (OSError, ValueError):
        return None
    return text
