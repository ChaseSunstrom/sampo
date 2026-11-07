"""T-3252: the tests' fm calls go through a fork server: one process imports Sampo once and each call is a fork of it,
which must behave exactly like a fresh `python3 fm ...` (argv, cwd, env, stdin, exit code, output) and cost far less."""
import ast
import json
import os
import statistics
import subprocess
import sys
import time
import unittest
from unittest import mock

from helpers import FM, ForemanTestCase

import forkcli

HERE = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.join(os.path.dirname(HERE), "lib")


def base(**kw):
    """A small env a forked call can serve: PATH plus the locale and interpreter settings the server started with
    (without them forkcli runs the call fresh, and the comparison would prove nothing)."""
    return {**{k: v for k, v in os.environ.items() if k.startswith(forkcli.START_ENV)}, "PATH": os.environ["PATH"], **kw}


ECHO = r'''
import os, sys
data = sys.stdin.read()
print("argv", sys.argv[1:])
print("cwd", os.getcwd())
print("env", os.environ.get("PROBE"), "HOME" in os.environ)
print("stdin", repr(data))
sys.stderr.write("to stderr\n")
if sys.argv[1:2] == ["sleep"]:
    import time; time.sleep(30)
if sys.argv[1:2] == ["crash"]:
    raise RuntimeError("boom")
if sys.argv[1:2] == ["neg"]:
    sys.exit(-1)
if sys.argv[1:2] == ["tmp"]:
    import tempfile; print("tmp", tempfile.gettempdir())
if sys.argv[1:2] == ["ppid"]:
    print("parent", os.getppid())
if sys.argv[1:2] == ["path"]:
    print("tests on path", any(p.rstrip("/").endswith("plugin/tests") for p in sys.path))
if sys.argv[1:2] == ["nap"]:
    import time; time.sleep(0.3)
if sys.argv[1:2] == ["real"]:  # the real parent, which os.getppid() hides in a fork
    print("real parent", open("/proc/self/stat").read().rsplit(")", 1)[1].split()[1])
if sys.argv[1:2] == ["inherit"]:  # T-3678: nothing of the server's loop reaches a call: no socket, pipe, poll, handler
    import signal
    fds = []
    for fd in sorted(int(n) for n in os.listdir("/proc/self/fd") if int(n) > 2):
        try:
            fds.append(os.readlink(f"/proc/self/fd/{fd}").split(":")[0])
        except OSError:  # the listing's own folder, closed by now
            pass
    print("fds", [k for k in fds if k in ("socket", "pipe", "anon_inode")])
    print("signals", signal.getsignal(signal.SIGCHLD), signal.getsignal(signal.SIGTERM), signal.set_wakeup_fd(-1))
sys.exit(int(sys.argv[1]) if sys.argv[1:2] and sys.argv[1].isdigit() else 0)
'''


class ForkServer(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.script = os.path.join(self.tmp, "echo.py")
        with open(self.script, "w") as f:
            f.write(ECHO)
        self.addCleanup(forkcli.stop)
        env = mock.patch.dict(os.environ)  # these test a class's own server, not the one run.py shares (T-3572)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(forkcli.SHARED, None)

    def both(self, args, env, cwd, input=None):
        env = base(**env)
        fresh = subprocess.run([sys.executable, self.script, *args], cwd=cwd, env=env, input=input,
                               capture_output=True, text=True, timeout=30)
        forked = forkcli.run(self.script, args, cwd=cwd, env=env, input=input, timeout=30)
        self.assertIn(os.getpid(), forkcli._SERVER, "the call went through the server")
        return fresh, forked

    def test_a_changed_locale_runs_fresh(self):
        forkcli.run(self.script, ["0"], cwd=self.tmp, env=base(LC_ALL="C", TZ="Pacific/Apia"), timeout=30)
        self.assertNotIn(os.getpid(), forkcli._SERVER, "a call with another locale or zone never reaches the server")

    def test_a_forked_call_sees_what_a_fresh_one_does(self):
        cwd = os.path.join(self.tmp, "here")
        os.makedirs(cwd)
        for args, env, input in ((["7", "x"], {"PROBE": "one", "PATH": os.environ["PATH"]}, "in put\n"),
                                 (["0"], {"PROBE": "two", "HOME": self.tmp, "PATH": os.environ["PATH"]}, None),
                                 (["crash"], {"PATH": os.environ["PATH"]}, None)):
            fresh, forked = self.both(args, env, cwd, input)
            self.assertEqual(forked.returncode, fresh.returncode, forked.stderr)
            self.assertEqual(forked.stdout, fresh.stdout)
            self.assertEqual(forked.stderr.splitlines()[:1], fresh.stderr.splitlines()[:1])
            if args == ["crash"]:
                self.assertIn("RuntimeError: boom", forked.stderr)

    def test_exit_codes_temp_dir_and_path_match_a_fresh_process(self):
        # review (T-3297): sys.exit(-1) is 255 fresh; a call's own TMPDIR wins; the tests folder isn't on fm's path;
        # T-3572: its parent is the caller, as the hook's Claude Code lookup (T-1226) needs
        other = os.path.join(self.tmp, "othertmp")
        os.makedirs(other)
        for args, env in ((["neg"], {"PATH": os.environ["PATH"]}),
                          (["tmp"], {"PATH": os.environ["PATH"], "TMPDIR": other}),
                          (["path"], {"PATH": os.environ["PATH"]}), (["ppid"], {"PATH": os.environ["PATH"]}),
                          (["inherit"], {"PATH": os.environ["PATH"]})):
            fresh, forked = self.both(args, env, self.tmp)
            self.assertEqual((forked.returncode, forked.stdout), (fresh.returncode, fresh.stdout), args)

    def test_a_call_is_one_fork_of_the_server(self):
        # T-3678: no manager per call (a fork and a waiting process fewer each): the worker is the server's own child
        r = forkcli.run(self.script, ["real"], cwd=self.tmp, env=base(), timeout=30)
        self.assertIn(f"real parent {forkcli._SERVER[os.getpid()][1].pid}\n", r.stdout)

    def test_concurrent_calls_keep_their_own_env_cwd_and_stdin_and_a_stalled_client_blocks_none(self):
        # T-3678: one server loop serves every call; a client that sent half a request holds up no other
        import socket
        from concurrent.futures import ThreadPoolExecutor
        forkcli.run(self.script, ["0"], cwd=self.tmp, env=base(), timeout=30)
        stalled = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(stalled.close)
        stalled.connect(forkcli._SERVER[os.getpid()][0])
        stalled.sendall(b"\0\0")  # half a length prefix, and never the rest
        dirs = [os.path.join(self.tmp, f"d{i}") for i in range(8)]
        for d in dirs:
            os.makedirs(d)
        with ThreadPoolExecutor(8) as pool:
            got = list(pool.map(lambda i: forkcli.run(self.script, ["nap", str(i)], cwd=dirs[i], env=base(PROBE=f"p{i}"),
                                                      input=f"in{i}", timeout=60), range(8)))
        for i, r in enumerate(got):
            self.assertEqual((r.returncode, r.stdout.splitlines()),
                             (0, [f"argv ['nap', '{i}']", f"cwd {dirs[i]}", f"env p{i} False", f"stdin 'in{i}'"]))

    def test_the_server_leaves_no_folder_behind(self):
        forkcli.run(self.script, ["0"], cwd=self.tmp, env=base(), timeout=30)
        path = forkcli._SERVER[os.getpid()][0]
        forkcli.stop()
        self.assertFalse(os.path.exists(os.path.dirname(path)))

    def test_a_slow_call_times_out_and_is_killed(self):
        start = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            forkcli.run(self.script, ["sleep"], cwd=self.tmp, env=base(), timeout=1)
        self.assertLess(time.monotonic() - start, 10)

    def test_fm_calls_match_and_cost_a_fraction(self):
        env = dict(os.environ, FOREMAN_HOME=self.home, FOREMAN_INHIBITED="1")
        subprocess.run([sys.executable, FM, "init"], cwd=self.repo, env=env, capture_output=True, check=True)
        new = ["task", "new", "Speed", "--type", "FIX", "--tier", "S", "--ac", "ok :: true", "--step", "s"]
        a = forkcli.run(FM, new, cwd=self.repo, env=env, timeout=60)
        self.assertEqual(a.returncode, 0, a.stderr)
        fresh = subprocess.run([sys.executable, FM, "task", "show", "T-0001"], cwd=self.repo, env=env,
                               capture_output=True, text=True)
        forked = forkcli.run(FM, ["task", "show", "T-0001"], cwd=self.repo, env=env, timeout=60)
        self.assertEqual((forked.returncode, forked.stdout), (fresh.returncode, fresh.stdout))
        bad = forkcli.run(FM, ["no-such-verb"], cwd=self.repo, env=env, timeout=60)
        self.assertEqual(bad.returncode, 2)

        def timed(call):
            t = time.perf_counter()
            call()
            return time.perf_counter() - t

        # T-3629: A/B samples alternate, so a load spike hits both arms alike (sequential arms flaked at load 50+)
        slows, fasts = [], []
        for _ in range(15):
            slows.append(timed(lambda: subprocess.run([sys.executable, FM, "task", "show", "T-0001"], cwd=self.repo,
                                                      env=env, capture_output=True)))
            fasts.append(timed(lambda: forkcli.run(FM, ["task", "show", "T-0001"], cwd=self.repo, env=env, timeout=60)))
        slow, fast = statistics.median(slows), statistics.median(fasts)
        # measured 2.4x both quiet (23 vs 55 ms) and at load 60 (85 vs 205 ms): the bar is a 40% cut
        self.assertLess(fast, slow * 0.6, f"forked {fast * 1000:.0f} ms, fresh {slow * 1000:.0f} ms")


# T-3572: a class process run by run.py: one call through forkcli, then which process served it and its own servers
CLASS = ("import json, sys, forkcli; p = forkcli.run(sys.argv[1], [], timeout=60); "
         "print(json.dumps([p.returncode, p.stdout.strip(), [v[0] for v in forkcli._SERVER.values()]]))")
# the real parent's argv[3] (os.getppid() reads as the caller in a fork): a forked call's parent is
# `python3 forkcli.py serve SOCKET PID` (T-3678); a fresh call's parent is `python3 -c CLASS SCRIPT`
PARENT = ("ppid = open('/proc/self/stat').read().rsplit(')', 1)[1].split()[1]\n"
          "print(open(f'/proc/{ppid}/cmdline').read().split(chr(0))[3])\n")


class SharedServer(ForemanTestCase):
    """T-3572: run.py starts one server and names it in its classes' environment; their calls fork it instead of each
    class starting its own (an interpreter start and ~50 ms of imports per class)."""

    def setUp(self):
        super().setUp()
        self.addCleanup(forkcli.stop)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        self.script = os.path.join(self.tmp, "parent.py")
        with open(self.script, "w") as f:
            f.write(PARENT)

    def klass(self):
        got = subprocess.run([sys.executable, "-c", CLASS, self.script], cwd=HERE, env=dict(os.environ),
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(got.returncode, 0, got.stderr)
        return json.loads(got.stdout)

    def test_a_class_calls_through_the_runs_server_and_starts_none(self):
        path = forkcli.share()
        self.assertEqual(json.loads(os.environ[forkcli.SHARED]), [forkcli.LIB, path])
        self.assertEqual(self.klass(), [0, path, []])

    def test_another_trees_server_or_a_gone_one_is_not_used(self):
        path = forkcli.share()  # another tree's server has another Sampo preloaded: the class's own code must run
        for lib, sock in (("/elsewhere/plugin/lib", path), (forkcli.LIB, path + ".gone")):
            os.environ[forkcli.SHARED] = json.dumps([lib, sock])
            code, served, own = self.klass()
            self.assertEqual(code, 0)
            self.assertEqual(own, [served], f"{lib}: the class starts its own server and calls through it")
            self.assertNotEqual(served, path)

    def test_a_killed_class_takes_its_running_call_with_it(self):
        # the run's server outlives a class run.py kills at its timeout; its hung call must not run on until the run ends
        forkcli.share()
        hang, pidfile = os.path.join(self.tmp, "hang.py"), os.path.join(self.tmp, "pid")
        with open(hang, "w") as f:
            f.write("import os, sys, time\nopen(sys.argv[1], 'w').write(str(os.getpid()))\ntime.sleep(120)\n")
        klass = subprocess.Popen([sys.executable, "-c", "import sys, forkcli; forkcli.run(sys.argv[1], sys.argv[2:])",
                                  hang, pidfile], cwd=HERE, env=dict(os.environ))
        self.addCleanup(klass.wait)
        self.addCleanup(klass.kill)
        until = time.monotonic() + 60
        while not (os.path.exists(pidfile) and open(pidfile).read()) and time.monotonic() < until:
            time.sleep(0.1)
        call = int(open(pidfile).read())
        klass.kill()
        while time.monotonic() < until:
            try:
                os.kill(call, 0)
            except ProcessLookupError:
                return
            time.sleep(0.1)
        os.kill(call, 9)
        self.fail("the call outlived its class")

    def test_off_names_no_server_and_runs_fresh(self):
        os.environ[forkcli.SHARED] = json.dumps([forkcli.LIB, "/inherited/s"])  # an outer run's: never passed on
        os.environ["FOREMAN_FORK_CLI"] = "0"
        self.assertIsNone(forkcli.share())
        self.assertNotIn(forkcli.SHARED, os.environ)
        self.assertEqual(self.klass(), [0, self.script, []])


class ImportAudit(unittest.TestCase):
    """What the server imports ahead must read nothing from the environment at import, or every fork inherits the
    server's value: a module-level statement may not call os.environ, getenv, expanduser or Path.home."""

    def test_the_preloaded_modules_read_no_environment_at_import(self):
        bad = []
        for mod in (m for m in forkcli.PRELOAD if m.startswith("sampo.")):  # the stdlib ones read nothing at import
            path = os.path.join(LIB, *mod.split(".")) + ".py"
            with open(path, encoding="utf-8") as f:
                tree = ast.parse(f.read())
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    continue
                src = ast.unparse(node)
                if src in {f"{n}(os.environ)" for n in forkcli.REAPPLY}:  # done again in each forked call
                    continue
                if any(k in src for k in ("os.environ", "getenv(", "expanduser(", "Path.home(", "getcwd(")):
                    bad.append(f"{mod}:{node.lineno}: {src[:100]}")
        self.assertEqual(bad, [])

    def test_a_forked_call_maps_sampo_names_as_a_fresh_one_does(self):
        env = base(SAMPO_PROBE_X="1", FOREMAN_HOME="/nowhere")
        code = ("import os, sys; sys.path.insert(0, %r); import sampo.core; "
                "print(os.environ.get('FOREMAN_PROBE_X'), 'SAMPO_PROBE_X' in os.environ)") % LIB
        fresh = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
        probe = os.path.join(forkcli.tempfile.mkdtemp(), "probe.py")
        with open(probe, "w") as f:
            f.write("import os; print(os.environ.get('FOREMAN_PROBE_X'), 'SAMPO_PROBE_X' in os.environ)\n")
        forked = forkcli.run(probe, [], env=env, timeout=30)
        forkcli.stop()
        self.assertEqual(forked.stdout, fresh.stdout)
        self.assertEqual(fresh.stdout.strip(), "1 False")


if __name__ == "__main__":
    unittest.main()
