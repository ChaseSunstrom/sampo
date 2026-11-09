"""sampo demo (T-0870): Sampo at work in under a minute, with no model calls. A throwaway git repo with a seeded bug, a
Sampo home of its own, and the real sampo CLI and the Ward's PreToolUse hook playing Claude's part with canned moves: an edit
refused for want of a task, the task planned, rm -rf ~ refused, a red test recorded, the fix, a green test, a close
refused for a missing audit and then made. Everything lives in one temp folder, deleted at the end (Ctrl-C too)."""
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

from sampo import core as c

PLUGIN = os.path.dirname(os.path.dirname(os.path.abspath(os.path.dirname(__file__))))
FM = os.path.join(PLUGIN, "bin", "sampo")  # T-3989: its messages say sampo, as a user sees them
HOOK = os.path.join(PLUGIN, "hooks", "hook")
TEST = "python3 -m unittest -q test_calc"
BUG = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"
UNIT = ("import unittest\n\nfrom calc import add\n\n\nclass Add(unittest.TestCase):\n    def test_add(self):\n"
        "        self.assertEqual(add(2, 3), 5)\n")


PACE, COLOR = 0.0, False  # T-0902: --pace seconds per stage (a recording), colour on a terminal or FORCE_COLOR


def _c(code, text):
    return f"\x1b[{code}m{text}\x1b[0m" if COLOR else text


def _out(text="", pause=0.0, typed=False):
    """A line of the demo; with --pace, a pause after it, and a command typed out (at most ~1.2 paces long)."""
    if typed and PACE:
        lead, cmd = text.split("$ ", 1)
        print(lead + _c("2", "$ "), end="", flush=True)
        for ch in cmd:
            print(_c("36", ch), end="", flush=True)
            time.sleep(min(0.02 * PACE, 1.2 * PACE / max(len(cmd), 1)))
        print(flush=True)
    else:
        print(text, flush=True)
    if PACE and pause:
        time.sleep(PACE * pause)


class Off(Exception):
    """A real command did something the demo didn't expect: Sampo's behaviour changed."""


def _env(work):
    """Nothing of the user's: no FOREMAN_*/CLAUDE* variables, a fake HOME, and a `claude` on PATH that only notes it
    was called (the demo never calls a model; the note fails the run if it did)."""
    env = c.sandbox_env(os.environ)  # T-2822: and the load gate off, so a loaded host can't stall or refuse the demo
    bin_ = os.path.join(work, "bin")
    os.makedirs(bin_)
    with open(os.path.join(bin_, "claude"), "w") as f:
        f.write(f"#!/bin/sh\ntouch {shlex.quote(os.path.join(work, 'claude-called'))}\nexit 97\n")
    os.chmod(os.path.join(bin_, "claude"), 0o755)
    for d in ("home", "foreman", "xdg"):
        os.makedirs(os.path.join(work, d))
    env.update(HOME=os.path.join(work, "home"), FOREMAN_HOME=os.path.join(work, "foreman"),
               XDG_STATE_HOME=os.path.join(work, "xdg"), FOREMAN_NO_BACKGROUND="1", PYTHONDONTWRITEBYTECODE="1",
               GIT_CONFIG_NOSYSTEM="1", PATH=bin_ + os.pathsep + env.get("PATH", ""))
    return env


def _run(cmd, env, cwd, stdin=None):
    return subprocess.run(cmd, env=env, cwd=cwd, input=stdin, capture_output=True, text=True, timeout=60,
                          stdin=None if stdin is not None else subprocess.DEVNULL)


def _fm(env, repo, *args, ok=True):
    line = "sampo " + " ".join(f'"{a}"' if " " in a else a for a in args)
    _out("  $ " + line if PACE else "  " + _c("2", "$ ") + _c("36", line), 0.4, typed=True)
    p = _run([sys.executable, FM, *args], env, repo)
    if (p.returncode == 0) != ok:
        raise Off(f"sampo {args[0]} {args[1] if len(args) > 1 else ''} exited {p.returncode}: "
                  f"{(p.stderr or p.stdout).strip()[-400:]}")
    return p


def _hook(env, repo, tool, tool_input):
    """The guard's answer to one tool call, through the real PreToolUse hook: its reason when it denies."""
    payload = {"session_id": "fm-demo", "cwd": repo, "hook_event_name": "PreToolUse", "tool_name": tool,
               "tool_input": tool_input}
    p = _run([sys.executable, HOOK, "PreToolUse"], env, repo, json.dumps(payload))
    try:
        out = json.loads(p.stdout.strip().splitlines()[-1])["hookSpecificOutput"]
    except (ValueError, IndexError, KeyError):
        out = {}
    if out.get("permissionDecision") != "deny":
        raise Off(f"the guard let {tool} {json.dumps(tool_input)} through (exit {p.returncode})")
    return out["permissionDecisionReason"]


def _write(repo, name, text):
    with open(os.path.join(repo, name), "w") as f:
        f.write(text)


def _say(n, text):
    _out("\n" + _c("1", f"{n}. {text}"), 1.0)


def demo(work):
    def show(text, pause=1.2):  # the temp folder's long path reads as <demo>
        _out(text.replace(work, "<demo>"), pause)
    env, repo = _env(work), os.path.join(work, "repo")
    os.makedirs(repo)
    _write(repo, "calc.py", BUG)
    for cmd in (["git", "init", "-q", "-b", "main"], ["git", "config", "user.email", "demo@example.com"],
                ["git", "config", "user.name", "fm demo"], ["git", "add", "."], ["git", "commit", "-qm", "seed"]):
        if _run(cmd, env, repo).returncode:
            raise Off(f"{' '.join(cmd)} failed: is git installed?")
    _run([sys.executable, FM, "init"], env, repo)
    _out(_c("1", "Sampo demo") + ": no model is called; the real sampo CLI and the Ward's hook run in a throwaway repo "
         "and a Sampo home of its own.", 0.5)
    _say(1, "The repo has a bug: calc.add(2, 3) returns -1. Claude tries to edit calc.py before any task exists.")
    show("  " + _c("1;31", "Refused") + ": " + _hook(env, repo, "Edit", {"file_path": os.path.join(repo, "calc.py"),
                                                    "old_string": "a - b", "new_string": "a + b"}))
    _say(2, "So it plans the work first: a brief with a done-when and the command that proves it.")
    p = _fm(env, repo, "task", "new", "add() subtracts", "--type", "FIX", "--tier", "S",
            "--ac", f"add(2, 3) is 5 :: {TEST}", "--step", "a test for add, failing first", "--step", "fix add",
            "--focus")
    show("  " + p.stdout.splitlines()[0])
    _say(3, "Mid-task, a careless command: rm -rf ~ (here ~ is the demo's own fake home).")
    show("  " + _c("1;31", "Refused") + ": " + _hook(env, repo, "Bash", {"command": "rm -rf ~"}))
    _say(4, "Test first: Claude writes test_calc.py and records it as the step's evidence.")
    _write(repo, "test_calc.py", UNIT)
    _fm(env, repo, "task", "evidence", "T-0001", "--step", "1", "--run", TEST, ok=False)
    show("  " + _c("31", "red") + ": the test fails (AssertionError: -1 != 5), which proves it catches the bug.")
    _say(5, "The fix: return a + b. The same test, recorded again.")
    _write(repo, "calc.py", FIXED)
    _fm(env, repo, "task", "evidence", "T-0001", "--step", "2", "--run", TEST)
    show("  " + _c("32", "green") + ": the test passes now.")
    _say(6, "Claude tries to close the task without saying how it checked its work.")
    p = _fm(env, repo, "task", "finish", "T-0001", ok=False)
    show("  " + _c("33", "not finished") + ": " + (p.stderr or p.stdout).strip().splitlines()[0].removeprefix("sampo: ").removeprefix("fm: "))
    _say(7, "With the audit, a lesson and a commit message, the close goes through.")
    p = _fm(env, repo, "task", "finish", "T-0001", "--audit", "self checklist: diff read, test red then green",
            "--result", "pass", "--docs", "none: a demo", "--lesson", "a test that failed first is the proof",
            "--why-not-caught", "none: the bug was seeded", "--commit", "Fix add() subtracting (T-0001)")
    if "T-0001 done" not in p.stdout:
        raise Off("sampo task finish exited 0 without closing T-0001: " + p.stdout.strip()[-400:])
    show("  " + _c("1;32", "closed") + ": " + "\n  ".join(p.stdout.strip().splitlines()))
    if os.path.exists(os.path.join(work, "claude-called")):
        raise Off("claude was called")


DOCS = ("README.md", "MASTER.md")  # T-1519: the docs whose runnable fences (```bash runnable) fm release-check runs
FENCE_TIMEOUT = 120  # seconds per fence
_SHELLS = {"bash", "sh", "shell"}


def fences(root=None):
    """[(doc:line, body)] for each shell fence README.md and MASTER.md mark runnable: ```bash runnable."""
    from sampo import playbooks
    out = []
    top = root or os.path.dirname(PLUGIN)
    for doc in DOCS + tuple(os.path.relpath(s, top) for s in c.map_shards(top)):  # T-2915: MASTER.md's shards
        try:
            with open(os.path.join(top, doc), encoding="utf-8", errors="replace") as f:
                blocks, _ = playbooks._blocks(f.read())
        except OSError:
            continue
        out += [(f"{doc}:{n}", body) for n, info, body in blocks
                if info and info[0].lower() in _SHELLS and "runnable" in info[1:]]
    return out


def _fence_env(d):
    """A fence's variables: HOME, Sampo's home and state and TMPDIR in its own temporary folder, this checkout's fm
    first on PATH, and a claude that only fails (a fence never calls a model)."""
    env = _env(d)
    keep = ("HOME", "FOREMAN_HOME", "XDG_STATE_HOME", "FOREMAN_NO_BACKGROUND", "PYTHONDONTWRITEBYTECODE",
            "GIT_CONFIG_NOSYSTEM", "FOREMAN_LOAD_GATE")
    os.makedirs(os.path.join(d, "tmp"))
    return dict({k: env[k] for k in keep}, PATH=os.path.join(PLUGIN, "bin") + os.pathsep + env["PATH"],
                FOREMAN_STATE=os.path.join(env["FOREMAN_HOME"], "state"),  # never a fallback outside the folder
                TMPDIR=os.path.join(d, "tmp"))


def run_fences(root=None, timeout=FENCE_TIMEOUT):
    """['doc:line: why'] for each runnable fence that failed. Each runs as bash -e through fm check playbooks' runner:
    its own new temporary folder (the cwd, holding HOME, Sampo's home and state, TMPDIR and the claude stub), stdin
    closed, its limits and timeout, then its process group killed. A fence is trusted like the repo's tests: that is
    hygiene, not a sandbox, so it can still reach any path and the network."""
    from sampo import playbooks
    limits, bash = playbooks._limits(), ("bash", "--noprofile", "--norc", "-e", "-c")
    return [f"{where}: {why}" for where, body in fences(root)
            for why in [playbooks._run(body, timeout, limits, argv=bash, env=_fence_env, what="fence")] if why]


def release_env(work):
    """A newcomer's environment: nothing of the user's Sampo, Claude Code or git, except FOREMAN_DOCTOR_BENCH, so
    the suite's run can skip hook timings that a loaded machine makes fail (T-1829)."""
    env = c.sandbox_env(os.environ, keep=("FOREMAN_DOCTOR_BENCH",))  # T-2822: the load gate off too
    env.update(HOME=os.path.join(work, "home"), XDG_STATE_HOME=os.path.join(work, "xdg"),
               FOREMAN_HOME=os.path.dirname(PLUGIN), FOREMAN_STATE=os.path.join(work, "state"),
               FOREMAN_NO_BACKGROUND="1", PYTHONDONTWRITEBYTECODE="1", GIT_CONFIG_NOSYSTEM="1")
    return env


def cmd_release_check(args):
    """T-0960: what a newcomer runs first, the way they run it: fm doctor and fm demo on this checkout with a temporary
    HOME and Sampo state (nothing of the user's), each passing, and (T-1519) the docs' runnable fences, which are
    trusted like tests (run_fences). Exit 0 when all do. fm demo stubs claude itself."""
    work = tempfile.mkdtemp(prefix="fm-release-")
    try:
        env = release_env(work)
        os.makedirs(env["HOME"])
        bad = 0
        for verb in ("doctor", "demo"):
            p = subprocess.run([sys.executable, FM, verb], env=env, cwd=env["HOME"], capture_output=True, text=True,
                               timeout=300, stdin=subprocess.DEVNULL)
            ok = p.returncode == 0
            bad += not ok
            print(f"{'PASS' if ok else 'FAIL'}  sampo {verb}" + ("" if ok else f" (exit {p.returncode}):\n"
                  + "\n".join("  " + ln for ln in (p.stdout + p.stderr).strip().splitlines()[-15:])))
        fails = run_fences()
        bad += bool(fails)
        print(f"{'FAIL' if fails else 'PASS'}  {len(fences())} runnable fence(s) in {' and '.join(DOCS)} (trusted "
              f"like tests: a throwaway home, not a sandbox)" + "".join(f"\n  {x}" for x in fails))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print(f"sampo release-check: {'OK' if not bad else 'FAILED'} (a temporary HOME, deleted)")
    return 1 if bad else 0


def cmd_demo(args):
    global PACE, COLOR
    PACE = max(0.0, float(getattr(args, "pace", 0) or 0))
    COLOR = bool(os.environ.get("FORCE_COLOR")) or (sys.stdout.isatty() and not os.environ.get("NO_COLOR"))
    work = tempfile.mkdtemp(prefix="fm-demo-")
    try:
        demo(work)
    except KeyboardInterrupt:
        print("\nStopped.")
        return 130
    except (Off, OSError, subprocess.TimeoutExpired) as e:
        print(f"\nsampo demo: Sampo didn't do what the demo expects: {e}", file=sys.stderr)
        return 1
    finally:
        shutil.rmtree(work, ignore_errors=True)
    here = c.find_project(os.getcwd())  # T-0919: run inside a project, end on that project's own next step
    print("\nThe demo's repo and Sampo home are deleted: nothing was left behind. " + (
        f"Back in this project ({here.slug}), next: {c.next_for(here)[2].split(' — ')[0]}" if here else
        "To start for real: sampo init in your repo, or just give Claude a request."))
    return 0
