"""T-0705 (Frontier 05, first slice): ambient verification. After an edit, the affected tests (fm check --affected's
selection) run in a detached process, out of the model's turns. Only a flip reaches the model — passing tests that now
fail, or failing ones that pass again — as one note on its next tool call; a pass never closes a step (T-0994: that
closed steps mid-work), its note names the evidence line for when the step is done. Opt-in per project (fm check
ambient on); skipped on a strained host; nothing leaves the machine."""
import json
import os
import re

from sampo import core as c
from sampo.core import subprocess  # T-3577: imported on first use

RERUNS = 3  # edits that land during a run trigger at most this many reruns, then the next edit starts a new one
FAILED = c.lazy(r"^(?:FAIL|ERROR): (\S+)|^FAILED (\S+)", re.M)  # unittest 'FAIL: name (mod.Class.name)', pytest 'FAILED path::name'
RED_STEP = c.lazy(r"(?i)\b(fail|red\b|reproduc|repro\b)")  # a step that wants a red run, which a pass won't verify


def _dir(p):
    d = os.path.join(p.dir, "ambient")
    os.makedirs(d, exist_ok=True)
    return d


def after_edit(p, path):
    """The PostToolUse hook's call after an edit: start a run, or mark the running one dirty so it runs again."""
    if not c.read_meta(p).get("ambient") or not path.startswith(p.root.rstrip("/") + "/") or c.host_strain() \
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
    try:
        with open(os.path.join(_dir(p), "run.pid")) as f:
            os.kill(int(f.read().strip()), 0)
        return True
    except (OSError, ValueError):
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
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        for _ in range(1 + RERUNS):
            try:
                os.unlink(os.path.join(d, "dirty"))
            except OSError:
                pass
            cmd, tests, _ = cli.affected(p)
            if not cmd:  # an edit that undid the change selects nothing; a failing last run still needs its answer
                cmd, tests = _last_failing(p)
            try:  # T-0901: the affected template is a stored command; one the guard refuses never runs here
                if cmd:
                    cli.guard_stored(p, None, cmd)
            except c.PolicyError as e:  # said where fm check --affected would say it, never silently
                c.log_event(p, "ambient_refused", data={"cmd": c.fit(cmd, 200), "why": c.fit(str(e), 300)})
                cmd = None
            if cmd:
                before = c.worktree_tree(p.root)  # T-3270: the verdict is for the tree the run began on
                # T-4267: at nice 10 like the lane gates (T-4197): an edit's check never competes with a gate
                code, output = c.run_command(p.root, cmd, 600, niced=True)
                _record(p, cmd, tests, code, output)
                _write_pass(p, cmd, before, code)
            if not os.path.exists(os.path.join(d, "dirty")):
                break
    except Exception as e:  # never a crash in a detached run: one line in the folder says why
        c.write_atomic(os.path.join(d, "error.txt"), f"{type(e).__name__}: {e}\n")
    finally:
        try:
            os.unlink(pid)
        except OSError:
            pass


def _last_failing(p):
    try:
        with open(os.path.join(_dir(p), "last.json")) as f:
            last = json.load(f)
    except (OSError, ValueError):
        return None, []
    return (last.get("cmd"), last.get("tests") or []) if last.get("exit") else (None, [])


def _record(p, cmd, tests, code, output):
    d = _dir(p)
    last = os.path.join(d, "last.json")
    try:
        with open(last) as f:
            prev = json.load(f).get("exit")
    except (OSError, ValueError):
        prev = None
    c.write_atomic(last, json.dumps({"cmd": cmd, "tests": tests, "exit": code, "ts": c.now()}))
    act = c.active_brief(c.load_briefs(p), p.lane)
    cur = act.current_step() if act else None
    if code and (prev is None or not prev):
        # T-1275: the failing tests' own names, then the command; never the runner's timing or class counts
        bad = list(dict.fromkeys(m.group(1) or m.group(2) for m in FAILED.finditer(c.redact(output)[:200000])))
        head = f"Ambient tests: ✗ now fail ({', '.join(c.fit(b, 50) for b in bad[:3]) if bad else f'exit {code}'}): "
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
        c.write_atomic(os.path.join(d, "note.json"), json.dumps({"text": text, "ts": c.now()}))
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
