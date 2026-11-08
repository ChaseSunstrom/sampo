"""Shared test helpers: isolated FOREMAN_HOME, scratch git repos, running the CLI."""
import atexit
import functools
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

# T-3185: an inherited GIT_DIR (a git hook, a sentinel run) makes every `git -C <scratch repo>` act on the real repo
# instead: on 2026-11-05 a run wrote core.bare=true and the fixture identity into the project's own .git/config.
for _k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
           "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE", "GIT_PREFIX"):
    os.environ.pop(_k, None)

PLUGIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(PLUGIN, "lib")
FM = os.path.join(PLUGIN, "bin", "fm")
HOOK = os.path.join(PLUGIN, "hooks", "hook")
RELAY = "import subprocess, sys; sys.exit(subprocess.run(sys.argv[1:4]).returncode)"
if LIB not in sys.path:
    sys.path.insert(0, LIB)

# T-0283: tests never reach the real claude. A stub dir a test puts first on PATH still wins; a stub that can't run (no
# shebang: ENOEXEC) makes the PATH lookup move on — to this lockout, not to the paid binary further along.
_LOCKOUT = tempfile.mkdtemp(prefix="fm-test-lockout-")
with open(os.path.join(_LOCKOUT, "claude"), "w") as _f:
    _f.write("#!/bin/sh\necho 'fm tests: the real claude is locked out (a stub on PATH is missing or could not run)' >&2"
             "\nexit 97\n")
os.chmod(os.path.join(_LOCKOUT, "claude"), 0o755)
os.environ["PATH"] = _LOCKOUT + os.pathsep + os.environ.get("PATH", "")
os.environ["FOREMAN_NATIVE_BIN"] = os.path.join(_LOCKOUT, "none")  # T-0992: nor fmcore's ~/.local/bin fallback
atexit.register(shutil.rmtree, _LOCKOUT, True)
# T-2166: fm genesis put files a tower doc only after a cold read says CLEAN; a test that files one puts this first
CLEAN_READER = tempfile.mkdtemp(prefix="fm-test-reader-")
with open(os.path.join(CLEAN_READER, "claude"), "w") as _f:
    _f.write("#!/bin/sh\ncat >/dev/null\n"
             "echo '{\"type\": \"result\", \"is_error\": false, \"total_cost_usd\": 0, \"result\": \"CLEAN\"}'\n")
os.chmod(os.path.join(CLEAN_READER, "claude"), 0o755)
atexit.register(shutil.rmtree, CLEAN_READER, True)
# T-0744: every test sees a calm host unless it says otherwise; the machine's real load (a full run beside other work)
# turned lanes and ambient runs off and failed tests only then
CALM_HOST = "load=0.1,cpus=8,mem_mb=8192"
os.environ.setdefault("FOREMAN_HOST", CALM_HOST)
# T-1791: the Claude Code env fm install-user sets (T-1796's 200k compact window) belongs to the running session, not a
# test: the bench stub read an inherited window as a soak and 10 tests failed in every installed session
from sampo import setup  # noqa: E402
for _k in setup.ENV:
    os.environ.pop(_k, None)
for _k in ("SAMPO_CALLS", "FOREMAN_CALLS"):  # T-3014: a test run under sampo check never logs into the live page's calls
    os.environ.pop(_k, None)  # T-3448: core (imported above) has moved an inherited SAMPO_CALLS to FOREMAN_CALLS
from sampo import core as _core  # noqa: E402
# T-3529: the user's own private terms (sampo private add), read before a test points FOREMAN_HOME at its own folder
# (and from the state path itself: state_dir() could move state to a fallback); none (CI): a made-up one
PRIVATE_TERMS = _core.private_terms(os.environ.get("FOREMAN_STATE") or os.path.join(_core.foreman_home(), "state")) \
    or ["*.home.example-private.test"]


def valid_m_plan():
    """T-2335: (interpretation, approach) texts the M/L plan gate accepts: two readings, three options each with an
    outcome (one use-what-exists, one do-less) and a choice. Use it where a test only needs a plannable M brief."""
    return ("Reading 1: a. Reading 2: b.",
            "1) a; fails if: x 2) use what exists: b; fails if: y 3) do less: c; fails if: z Choice 1 (80%): a")


def stub_claude(tmp, reply, code=0, name="claude-stub"):
    """T-2338: a `claude` that prints `reply` and exits `code`, in its own folder under `tmp`: the env a test passes to
    fm (env=) so a cold child answers without reaching the real one."""
    d = os.path.join(tmp, name)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "claude")
    with open(path, "w") as f:
        f.write(f"#!/usr/bin/env python3\nimport sys\nsys.stdin.read()\nprint({reply!r})\nsys.exit({code})\n")
    os.chmod(path, 0o755)
    return {"PATH": d + os.pathsep + os.environ["PATH"]}


def busy_factor(cap=8.0):
    """T-1730: how many times slower than a quiet machine a child runs now: runnable tasks (the 1-min load, or the
    count runnable this instant, which a fresh burst shows before the average catches up) per core, at least 1 and at
    most `cap`. A test's hang guard (a subprocess timeout) is multiplied by it; what a test times on purpose is not."""
    from sampo import core  # T-3391: the one signal gates use too; the calm default stand-in (line 46) reads the real host
    host = os.environ.get("FOREMAN_HOST")
    return core.load_factor(cap, "" if host == CALM_HOST else host)


def read_text(path, encoding="utf-8", limit=-1):
    with open(path, encoding=encoding) as f:
        return f.read(limit)


def read_doc(path):
    """T-2915: a doc's text; MASTER.md comes with its docs/map shards (the File map and Operations entries live there)."""
    if os.path.basename(path) == "MASTER.md":
        from sampo import core
        return core.master_text(os.path.dirname(path))
    return read_text(path)


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def headless_chrome():
    """A headless Chromium (T-1499, T-1610): FOREMAN_CHROME, one on PATH, or Playwright's newest download; None if
    absent. FOREMAN_CHROME=none: no browser (CI: a runner's snap chromium starts, then crashes under load; T-1806)."""
    if os.environ.get("FOREMAN_CHROME") == "none":
        return None
    import glob
    import re
    builds = glob.glob(os.path.expanduser(
        "~/.cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell"))
    build = lambda p: int((re.search(r"chromium_headless_shell-(\d+)/", p) or (0, -1))[1])  # 1243, not "1243" < "999"
    newest = sorted(builds, key=build, reverse=True)
    found = [os.environ.get("FOREMAN_CHROME")] + [shutil.which(n) for n in (
        "chrome-headless-shell", "chromium", "chromium-browser", "google-chrome")] + newest
    return next((p for p in found if p and os.access(p, os.X_OK) and _starts(p)), None)


@functools.lru_cache(None)
def _starts(browser):
    """T-1799: a browser that is there may still not start (a CI runner's snap chromium dies at Target.createTarget,
    and fm then rightly says "not run"): browser tests skip unless one answers here."""
    try:
        with tempfile.TemporaryDirectory() as profile:
            return devtools(browser, "about:blank", profile, "", (), [0], "1+1", timeout=20) == [2]
    except Exception:
        return False


# Before any page script: count requestAnimationFrame calls and the context types asked for (T-1610; shared, T-1653);
# T-1728: each frame's timestamp (the page's own clock, the first 5000) and how many frames are asked for and not yet run
# (waiting: 0 means the page rests until an event wakes it).
SPY = """(() => {
  const raf = window.requestAnimationFrame.bind(window), cancel = window.cancelAnimationFrame.bind(window);
  const get = HTMLCanvasElement.prototype.getContext, ids = new Set();
  const seen = window.__spy = { raf: 0, contexts: [], stamps: [] };
  Object.defineProperty(seen, "waiting", { get: () => ids.size, enumerable: true });
  window.requestAnimationFrame = fn => {
    seen.raf++;
    const id = raf(t => { ids.delete(id); if (seen.stamps.length < 5000) seen.stamps.push(t); fn(t); });
    ids.add(id); return id; };
  window.cancelAnimationFrame = id => { ids.delete(id); cancel(id); };
  HTMLCanvasElement.prototype.getContext = function (type, ...rest) { seen.contexts.push(type); return get.call(this, type, ...rest); };
})();"""


def pty_reader(master):
    """T-1828: a thread reading a pty's master until its other end closes (EIO on Linux, b'' on macOS): .join(), then
    .out. macOS drops what was written once the writer closes, and stalls the writer (and tcdrain) while the master
    goes unread, so a test keeps this reading from before the writer starts."""
    import threading
    out = bytearray()

    def run():
        while True:
            try:
                chunk = os.read(master, 65536)
            except OSError:
                return
            if not chunk:
                return
            out.extend(chunk)
    t = threading.Thread(target=run, daemon=True)
    t.out = out
    t.start()
    return t


def polls(upto=30, every=0.5):
    """T-1728: the seconds to sample a page at until `until` holds: every `every` s for `upto` s of a quiet machine,
    stretched by busy_factor."""
    return [i * every for i in range(1, int(upto * busy_factor() / every) + 1)]


def at_rest(got, quiet=3):
    """T-1728: the page drew frames and its last `quiet` samples show none asked for and none drawn between them: it
    rests (no wall-clock window to guess). A sample that failed (the page not parsed yet) is None."""
    tail = got[-quiet:]
    return (len(tail) == quiet and all(tail) and tail[0]["raf"] > 0
            and all(s["waiting"] == 0 and s["raf"] == tail[0]["raf"] for s in tail))


def anim_time(stamps, since=None, cap=0.1):
    """T-1728: seconds of animation time across frame timestamps (ms), as the pages advance their clock: each frame's
    step capped at `cap` s, so a loaded machine's slow frames don't count as more animation. Since `since` (ms): only
    the steps that end at or after it."""
    return sum(min((b - a) / 1000, cap) for a, b in zip(stamps, stamps[1:]) if since is None or b >= since)


def devtools(browser, url, profile, init, media, at, expr, timeout=60, until=None):
    """Open `url` in a headless Chromium driven over --remote-debugging-pipe (it reads fd 3 and writes fd 4,
    NUL-separated JSON: no websocket, stdlib only), `init` run before any page script and `media` emulated; `expr`
    evaluated at each of `at` seconds of real time, stopping early once `until(values so far)` holds (T-1728). The
    values, in order (None where expr threw); the browser is killed either way. `timeout` is past the last of `at`."""
    import fcntl
    import select
    import signal
    import time
    child_in, feed = os.pipe()
    out, child_out = os.pipe()
    high = [fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, 10) for fd in (child_in, child_out)]  # dup2 to 3 and 4 is safe
    try:
        pid = os.posix_spawn(browser, [browser, "--headless", "--use-angle=swiftshader", "--enable-unsafe-swiftshader",
                                       "--window-size=1280,1100", "--remote-debugging-pipe", "--user-data-dir=" + profile,
                                       "about:blank"], os.environ, setsid=True,
                             file_actions=[(os.POSIX_SPAWN_DUP2, high[0], 3), (os.POSIX_SPAWN_DUP2, high[1], 4),
                                           (os.POSIX_SPAWN_OPEN, 1, os.devnull, os.O_WRONLY, 0),
                                           (os.POSIX_SPAWN_OPEN, 2, os.devnull, os.O_WRONLY, 0)])
    except OSError:
        os.close(feed)
        os.close(out)
        raise
    finally:
        for fd in (child_in, child_out, *high):
            os.close(fd)
    at = list(at)
    buf, n, end = b"", 0, time.monotonic() + timeout + max(at, default=0)

    def call(method, session=None, **params):
        nonlocal buf, n
        if "url" in params:  # T-3691: run.py's recorder judges each page whether it may load the repo's files
            sys.audit("sampo.page", params["url"])
        n += 1
        os.write(feed, json.dumps({"id": n, "method": method, "params": params,
                                   **({"sessionId": session} if session else {})}).encode() + b"\0")
        while True:
            while b"\0" not in buf:
                if not select.select([out], [], [], max(0.0, end - time.monotonic()))[0]:
                    raise TimeoutError(f"no answer to {method} in {timeout} s")
                chunk = os.read(out, 1 << 16)
                if not chunk:
                    raise EOFError(f"the browser closed during {method}")
                buf += chunk
            line, buf = buf.split(b"\0", 1)
            got = json.loads(line)
            if got.get("id") == n:
                if "error" in got:
                    raise RuntimeError(f"{method}: {got['error']}")
                return got.get("result", {})

    try:
        start = time.monotonic()
        sid = call("Target.attachToTarget", targetId=call("Target.createTarget", url="about:blank")["targetId"],
                   flatten=True)["sessionId"]
        call("Page.enable", sid)
        call("Page.addScriptToEvaluateOnNewDocument", sid, source=init)
        call("Emulation.setEmulatedMedia", sid, features=[{"name": k, "value": v} for k, v in media])
        call("Page.navigate", sid, url=url)
        got = []
        for s in at:
            time.sleep(max(0.0, s - (time.monotonic() - start)))
            got.append(call("Runtime.evaluate", sid, expression=expr, returnByValue=True)["result"].get("value"))
            if until and until(got):
                break
        return got
    finally:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        os.waitpid(pid, 0)
        os.close(feed)
        os.close(out)


def stub_corpus_replay(repo, drifted=0, days=0, cases=3):
    """T-1401: the ledger record fm replay --cases leaves, without running the replay: red when drifted, days old."""
    import time
    from unittest import mock
    from sampo import core as c
    p = c.find_project(repo, create=True)
    with mock.patch.object(c, "now", return_value=c.iso(time.time() - days * 86400)):
        c.log_event(p, "guard_corpus", data={"cases": cases, "drifted": drifted, "broken": 0})
    return p


@functools.cache
def _repo_template(seed=None):
    """T-3581: the scratch repo up to its commit (init, identity, README added), made once a process. T-3678: with a
    seed (message, ((path, text), ...)), init is committed here and the seed's files are added for git_repo's commit."""
    base = tempfile.mkdtemp(prefix="fm-test-repo-")
    atexit.register(shutil.rmtree, base, True)
    root = os.path.join(base, "app")
    subprocess.run(["git", "init", "-q", "-b", "main", root], check=True)
    with open(os.path.join(root, ".git", "config"), "a") as f:  # what `git config user.*` writes, minus 2 processes
        # T-3678: and no detached `git maintenance run --auto` after each commit (a process each; one raced copytree)
        f.write("[user]\n\temail = t@example.com\n\tname = t\n[maintenance]\n\tauto = false\n")
    with open(os.path.join(root, "README.md"), "w") as f:
        f.write("# app\n")
    subprocess.run(["git", "-C", root, "add", "."], check=True)
    if seed:  # the init commit is the process's (its time and hash shared); each test's seed commit is its own, made now
        subprocess.run(["git", "-C", root, "commit", "-qm", "init"], check=True)
        for rel, text in seed[1]:
            os.makedirs(os.path.dirname(os.path.join(root, rel)), exist_ok=True)
            with open(os.path.join(root, rel), "w") as f:
                f.write(text)
        subprocess.run(["git", "-C", root, "add", "."], check=True)
    return root


def git_repo(parent, name="app", seed=None):
    """A copy of the template, then its own init commit made now: 1 git process instead of 5 (T-3581). The commit
    also refreshes the copied index, whose stat data would otherwise read dirty to diff-files/diff-index. T-3678: a
    seed's commit, on the template's init, is that one process (a class's own files and commit cost 2 more)."""
    root = os.path.join(parent, name)
    shutil.copytree(_repo_template(seed), root, copy_function=shutil.copy, ignore=shutil.ignore_patterns("*.sample"))
    subprocess.run(["git", "-C", root, "commit", "-qm", seed[0] if seed else "init"], check=True)
    return root


class SampoTestCase(unittest.TestCase):
    """Each test gets its own FOREMAN_HOME and a scratch git repo."""

    repo_seed = None  # T-3678: (message, ((path, text), ...)), committed on init in each test's repo (git_repo)

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = os.path.realpath(self._tmp.name)
        self.home = os.path.join(self.tmp, "fhome")
        os.makedirs(self.home)
        self._env = {k: os.environ.get(k) for k in ("FOREMAN_HOME", "FOREMAN_SESSION_ID", "FOREMAN_PROJECT",
                                                     "FOREMAN_STATE", "XDG_STATE_HOME", "FOREMAN_NO_BACKGROUND",
                                                     "FOREMAN_ONEOFF", "CLAUDE_CODE_ENTRYPOINT", "FOREMAN_DRIVE_TASK",
                                                     "FOREMAN_GUARD_CASES", "FOREMAN_NO_LINK", "FOREMAN_NO_RIVAL",
                                                     "FOREMAN_UI_CACHE_S")}
        os.environ["FOREMAN_NO_RIVAL"] = "1"  # T-2341: no plan waits on a cold rival read (the Rival tests turn it on)
        os.environ["FOREMAN_HOME"] = self.home
        os.environ["FOREMAN_NO_BACKGROUND"] = "1"  # a detached child writing into a test's folder races its cleanup
        os.environ["FOREMAN_NO_LINK"] = "1"  # T-1333: a test's session start never makes ~/.local/bin/fm
        os.environ["FOREMAN_UI_CACHE_S"] = "0"  # T-3542: each fm ui builds its view (test_ui_cache turns it on)
        # T-1004: a suite run from a one-off or headless session (the UI harness) sees what a working session sees
        for k in ("FOREMAN_SESSION_ID", "FOREMAN_PROJECT", "FOREMAN_STATE", "FOREMAN_ONEOFF", "CLAUDE_CODE_ENTRYPOINT",
                  "FOREMAN_DRIVE_TASK"):
            os.environ.pop(k, None)
        os.environ["XDG_STATE_HOME"] = os.path.join(self.tmp, "xdg")  # a state fallback never reaches the real one
        try:  # T-2871: one test is one command: a spend recorded in an earlier test is not this one's
            from sampo import budget as _budget
            _budget.RUN["usd"] = 0.0
        except (ImportError, AttributeError):
            pass
        # T-1197: a temp repo has no plugin/, so fm guard wrong fell back to the real checkout's corpus
        os.environ["FOREMAN_GUARD_CASES"] = os.path.join(self.tmp, "guard_cases.jsonl")
        try:  # T-3195: an in-process gate signs its base cache with this test's own key, never the user's
            from unittest import mock as _mock
            from sampo import selfgate as _selfgate
            key = _mock.patch.object(_selfgate, "_keyfile", lambda: os.path.join(self.tmp, "selfgate.key"))
            key.start()
            self.addCleanup(key.stop)
        except (ImportError, AttributeError):
            pass
        self.repo = git_repo(self.tmp, seed=self.repo_seed)

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        until = time.monotonic() + 5 * busy_factor()
        while True:  # T-1901: a detached child a test started on purpose may still be writing into state/
            try:
                return self._tmp.cleanup()
            except OSError:  # Errno 39: it wrote between rmtree's listing and its rmdir
                if time.monotonic() > until:
                    raise
                time.sleep(0.1)

    def fm(self, *args, cwd=None, check=True, env=None, input=None, timeout=30):
        # fm run never takes a real inhibitor lock; T-1379: fm doctor skips its hook bench (no test reads its timings)
        e = dict(os.environ, FOREMAN_HOME=self.home, FOREMAN_INHIBITED="1", FOREMAN_DOCTOR_BENCH="0")
        e.pop("CLAUDE_CODE_SESSION_ID", None)  # tests may run inside a live Claude Code session
        e.pop("CLAUDE_CODE_BRIDGE_SESSION_ID", None)  # T-2443: its commits would carry this session's link
        if args[:1] == ("which",):  # T-2371: fm which reads ~/.claude.json and bin folders: never this machine's
            e.update(HOME=os.path.join(self.tmp, "nohome"), CLAUDE_CONFIG_DIR=os.path.join(self.tmp, "nocc"))
        e.update(env or {})
        import forkcli  # T-3252: a fork of a warm Sampo, not a fresh interpreter; the same result
        p = forkcli.run(FM, args, cwd=cwd or self.repo, env=e, input=input, timeout=timeout * busy_factor())
        if check and p.returncode != 0:
            raise AssertionError(f"fm {' '.join(args)} exited {p.returncode}\nstdout:{p.stdout}\nstderr:{p.stderr}")
        return p

    def fm_ask(self, tid, *cats, session="sess-1", why="needs it", env=None, check=True, pin=None, flags=None):
        """fm ask the way a session runs it: PreToolUse sees the Bash command (trusted session id), then fm runs."""
        extra = ["--pin", pin] if pin else []
        cmd = f"fm ask {tid} {' '.join(cats + tuple(extra))} --why '{why}'"
        self.hook("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": cmd}, "session_id": session}, flags=flags)
        return self.fm("ask", tid, *cats, *extra, "--why", why, env=env, check=check)

    def fm_json(self, *args, **kw):
        return json.loads(self.fm(*args, "--json", **kw).stdout)

    def hook(self, event, payload, env=None, timeout=10, flags=None):
        e = dict(os.environ, FOREMAN_HOME=self.home, FOREMAN_HOOK_SLACK=str(busy_factor()))  # T-3366
        e.pop("SAMPO_HOOK_SLACK", None)  # core.mirror_env would let it win over the value above
        e.update(env or {})
        payload = dict({"session_id": "sess-1", "cwd": self.repo, "hook_event_name": event}, **payload)
        # T-1226: the hook asks which Claude Code runs it and how it was started; by default this test's process (a
        # fork of the warm server, as fm() calls are, whose parent reads as this test: T-3572), else a new stand-in
        if flags is None:
            import forkcli
            return forkcli.run(HOOK, [event], cwd=payload["cwd"], env=e, input=json.dumps(payload),
                               timeout=timeout * busy_factor())  # T-1730: a hang guard, not a bench
        return subprocess.run([self.claude_stand_in(), "-c", RELAY, sys.executable, HOOK, event, *flags],
                              input=json.dumps(payload), env=e, capture_output=True, text=True,
                              timeout=timeout * busy_factor(), cwd=payload["cwd"])

    def slow_start(self, delay=0.4):
        """T-1280: env for CI's slow interpreter start: every new Python process waits `delay` s (a sitecustomize)."""
        slow = os.path.join(self.tmp, "slow")
        os.makedirs(slow, exist_ok=True)
        with open(os.path.join(slow, "sitecustomize.py"), "w") as f:
            f.write(f"import time\ntime.sleep({delay})\n")
        return {"PYTHONPATH": slow}

    def claude_stand_in(self):
        """A process named claude (python under that name) that runs the command after its -c code."""
        path = os.path.join(self.tmp, "stand-in", "claude")
        if not os.path.exists(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            os.symlink(sys.executable, path)
        return path


ForemanTestCase = SampoTestCase  # T-2748: the old name, which most test files still import


# T-1361: misbehaving fake runner CLIs. One small sh script per failure mode; each ends by itself in seconds even if
# nothing stops it, so a test that goes wrong fails instead of hanging.
FAKE_MODES = {
    "ok": "echo done\n",
    "hang": "exec sleep {secs}\n",  # no output at all
    "silent": "echo starting\nexec sleep {secs}\n",  # talks, then goes quiet mid-stream
    "tty-prompt": "printf 'Overwrite settings? (y/n) '\nexec sleep {secs}\n",  # asks and waits; no TTY is there to answer
    "garbage-json": "echo '{{\"type\": \"result\", \"result\": '\necho 'not json at all {{{{'\nexit 0\n",
    "usage-limit": "echo 'Claude usage limit reached. Your limit will reset at 5pm.'\nexec sleep {secs}\n",
    "fork-exit": "sleep {secs} &\nexit 0\n",  # a child outlives the parent, still holding the pipe
}


def fake_cli(dir_, name, mode, secs=20):
    """Write an executable fake `name` in dir_ that misbehaves as `mode` (FAKE_MODES); returns its path."""
    os.makedirs(dir_, exist_ok=True)
    path = os.path.join(dir_, name)
    with open(path, "w") as f:
        f.write("#!/bin/sh\n" + FAKE_MODES[mode].format(secs=secs))
    os.chmod(path, 0o755)
    return path
