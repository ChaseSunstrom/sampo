"""T-5900: no stub or bad pid reaches a kill that signals every process.

On 2026-11-22 a test stubbed subprocess.Popen with a MagicMock; its pid converts to 1 and os.killpg(1, SIGKILL) is
kill(-1, SIGKILL): every process of the user died, three times. Three layers: the runner refuses such a pid in every
class (killguard.py, installed by run.py's BOOT and helpers.py), lib has one helper (sampo.killsafe) that signals only a
plain int pid above 1, and a shape test keeps every other nonzero-signal os.kill/os.killpg out of lib.

SAFETY: no test here sends a signal to a pid it did not start. The refusals are proved on a fake `real` function, and
the installed wrappers are only called after the test has checked they are installed."""
import ast
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.join(os.path.dirname(HERE), "lib")
for _p in (HERE, LIB):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import killguard  # noqa: E402
from sampo import killsafe  # noqa: E402

BAD = [("MagicMock", mock.MagicMock()), ("Mock", mock.Mock()), ("None", None), ("True", True), ("'7'", "7"),
       ("0", 0), ("1", 1), ("-1", -1)]


def _sleeper():
    """A child of this test in its own session and group: the one process these tests may signal."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class TheRunnerRefuses(unittest.TestCase):
    """Proved on a fake `real`: a wrapper that let one through would signal the fake, never a process."""

    def wrapped(self, name):
        real = mock.Mock()
        return killguard._guard(real, name), real

    def test_bad_pids_raise_and_signal_nothing(self):
        for name in ("kill", "killpg"):
            for label, pid in BAD:
                with self.subTest(call=name, pid=label):
                    fn, real = self.wrapped(name)
                    with self.assertRaises(killguard.KillRefused):
                        fn(pid, signal.SIGKILL)
                    real.assert_not_called()

    def test_a_pid_that_is_not_a_plain_int_is_refused_even_for_signal_zero(self):
        for label, pid in BAD[:5]:
            with self.subTest(pid=label):
                fn, real = self.wrapped("kill")
                with self.assertRaises(killguard.KillRefused):
                    fn(pid, 0)
                real.assert_not_called()

    def test_signal_zero_to_a_small_int_pid_is_a_check_not_a_kill(self):
        for pid in (0, 1):
            fn, real = self.wrapped("kill")
            fn(pid, 0)
            real.assert_called_once_with(pid, 0)

    def test_a_real_pid_passes_with_its_signal(self):
        fn, real = self.wrapped("killpg")
        fn(4242, signal.SIGTERM)
        real.assert_called_once_with(4242, signal.SIGTERM)

    def test_a_bad_signal_is_refused(self):
        fn, real = self.wrapped("kill")
        with self.assertRaises(killguard.KillRefused):
            fn(4242, None)
        real.assert_not_called()

    def test_the_refusal_is_loud_on_stderr(self):
        fn, _ = self.wrapped("killpg")
        with mock.patch.object(sys, "stderr") as err, self.assertRaises(killguard.KillRefused):
            fn(1, signal.SIGKILL)
        self.assertIn("killguard", "".join(c.args[0] for c in err.write.call_args_list))


class TheRunnerInstallsIt(unittest.TestCase):
    def test_os_kill_and_killpg_are_the_wrappers_in_this_worker(self):
        killguard.install()
        killguard.install()  # twice: still one wrapper, not a wrapper of a wrapper
        self.assertTrue(getattr(os.kill, "killguard", False), "run.py's BOOT installs it before every class")
        self.assertTrue(getattr(os.killpg, "killguard", False))
        self.assertFalse(getattr(os.kill.__wrapped__, "killguard", False))

    def test_the_installed_wrappers_refuse_a_mock_pid_and_still_signal_a_real_child(self):
        killguard.install()
        self.assertTrue(os.killpg.killguard and os.kill.killguard)  # checked first: only then is the next line safe
        for label, pid in BAD:
            with self.subTest(pid=label):
                with self.assertRaises(killguard.KillRefused):
                    os.killpg(pid, signal.SIGKILL)
                with self.assertRaises(killguard.KillRefused):
                    os.kill(pid, signal.SIGKILL)
        child = _sleeper()
        try:
            os.killpg(child.pid, signal.SIGKILL)
            self.assertEqual(child.wait(timeout=30), -signal.SIGKILL)
            os.kill(os.getpid(), 0)  # an existence check with an int pid is allowed
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()

    def test_run_py_boots_every_class_with_it_plain_unittest_included(self):
        """The runner's own BOOT, run on a class that never imports helpers: the wrappers are in place before it starts.
        The class only reads the marker; it signals nothing."""
        import run
        self.assertLess(run.BOOT.index("killguard.install()"), run.BOOT.index("unittest.main("))
        with tempfile.TemporaryDirectory(prefix="killguard-") as tmp:
            with open(os.path.join(tmp, "plainmod.py"), "w") as f:
                f.write("import os, unittest\n"
                        "class Plain(unittest.TestCase):\n"
                        "    def test_guarded(self):\n"
                        "        self.assertTrue(getattr(os.kill, 'killguard', False) and getattr(os.killpg, 'killguard', False))\n")
            env = {k: v for k, v in os.environ.items() if k != "RUNPY_CPU"}
            env["PYTHONPATH"] = tmp
            got = subprocess.run([sys.executable, "-c", run.BOOT, "0", "-q", "plainmod.Plain"], cwd=HERE, env=env,
                                 capture_output=True, text=True, timeout=100)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("OK", got.stderr)

    def test_helpers_installs_it_for_a_plain_unittest_run(self):
        got = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); import helpers, os; "
                              "print(getattr(os.kill, 'killguard', False), getattr(os.killpg, 'killguard', False))",
                              HERE], capture_output=True, text=True, timeout=100)
        self.assertEqual(got.stdout.strip(), "True True", got.stderr)


class TheLibHelper(unittest.TestCase):
    def test_bad_pids_signal_nothing(self):
        for label, pid in BAD:
            with self.subTest(pid=label):
                with mock.patch("os.killpg") as g, mock.patch("os.kill") as k:
                    self.assertIs(killsafe.killpg(pid, signal.SIGKILL), False)
                    self.assertIs(killsafe.kill(pid, signal.SIGKILL), False)
                g.assert_not_called()
                k.assert_not_called()

    def test_a_plain_int_above_one_is_signalled_and_reported(self):
        with mock.patch("os.killpg") as g, mock.patch("os.kill") as k:
            self.assertIs(killsafe.killpg(4242, signal.SIGTERM), True)
            self.assertIs(killsafe.kill(4243, signal.SIGKILL), True)
        g.assert_called_once_with(4242, signal.SIGTERM)
        k.assert_called_once_with(4243, signal.SIGKILL)

    def test_a_real_child_dies_and_a_missing_process_raises_for_the_caller(self):
        child = _sleeper()
        try:
            self.assertIs(killsafe.killpg(child.pid, signal.SIGKILL), True)
            self.assertEqual(child.wait(timeout=30), -signal.SIGKILL)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
        with self.assertRaises(OSError):  # past pid_max: no such process, ESRCH reaches the site's own except
            killsafe.kill(2 ** 22 + 7, signal.SIGKILL)

    def test_call_sites_with_a_stubbed_popen_signal_nothing(self):
        """The 2026-11-22 shape on the sites that take a Popen: core.kill_group and pilotsig._kill_group."""
        from sampo import core, pilotsig
        for fn in (core.kill_group, pilotsig._kill_group):
            for label, pid in BAD[:5]:
                with self.subTest(fn=fn.__name__, pid=label):
                    proc = mock.MagicMock()
                    proc.pid = pid
                    with mock.patch("os.killpg") as g, mock.patch("os.kill") as k:
                        fn(proc)
                    g.assert_not_called()
                    k.assert_not_called()


KILLS = ("kill", "killpg", "pthread_kill")
MODS = ("os", "posix", "nt", "signal")


def _os_names(tree):
    """The names this module binds to a module that has kill (`import os`, `import os as _os`, `import posix`, signal)."""
    names = {"os", "posix", "nt", "signal"}
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            names |= {a.asname for a in n.names if a.name in MODS and a.asname}
    return names


def _chain(n):
    """The dotted names under an Attribute/Name (core.os.kill -> ['core', 'os', 'kill']), or None when it is not a plain chain."""
    out = []
    while isinstance(n, ast.Attribute):
        out.append(n.attr)
        n = n.value
    if isinstance(n, ast.Name):
        return [n.id, *reversed(out)]
    return None


def kill_sites(src, filename="<src>"):
    """[(line, why)] of every os.kill/os.killpg use in `src` that is not a call with the constant signal 0 (an existence
    check). A call with any other signal, a reference that is not a call (an alias, `(os.killpg if g else os.kill)(...)`,
    a getattr) and `from os import kill` are all sites."""
    tree = ast.parse(src, filename)
    oss, found, calls = _os_names(tree), [], set()

    def is_kill(n):
        c = _chain(n.value) if isinstance(n, ast.Attribute) and n.attr in KILLS else None
        return bool(c) and any(p in oss for p in c)

    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and is_kill(n.func):
            calls.add(id(n.func))
            zero = len(n.args) == 2 and isinstance(n.args[1], ast.Constant) and n.args[1].value == 0 \
                and not isinstance(n.args[1].value, bool) and not n.keywords
            if not zero:
                found.append((n.lineno, f"{n.func.attr} with a signal that is not the constant 0"))
        elif isinstance(n, ast.ImportFrom) and n.module in MODS and any(a.name in KILLS for a in n.names):
            found.append((n.lineno, "from os import kill/killpg"))
        elif (isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "getattr" and len(n.args) >= 2
              and any(p in oss for p in (_chain(n.args[0]) or ()))
              and isinstance(n.args[1], ast.Constant) and n.args[1].value in KILLS):
            found.append((n.lineno, "getattr(.., 'kill')"))
    for n in ast.walk(tree):
        if is_kill(n) and id(n) not in calls:
            found.append((n.lineno, f"os.{n.attr} used without being called"))
    return sorted(found)


def _python_file(path):
    if path.endswith(".py"):
        return True
    try:
        with open(path, "rb") as f:
            return b"python" in f.readline()  # a hook script with a python shebang
    except OSError:
        return False


def lib_files():
    """Every python file that can signal: plugin/lib, plugin/hooks, and the test helpers (forkcli, run.py, ...: not the
    test_*.py modules, which only stub or signal a child they started, and not killguard.py, which wraps the real call)."""
    plugin, out = os.path.dirname(HERE), []
    for top in ("lib", "hooks", "tests"):
        for root, dirs, files in os.walk(os.path.join(plugin, top)):
            dirs[:] = [d for d in dirs if d not in ("__pycache__", "fixtures")]
            for f in files:
                if top == "tests" and (f.startswith("test_") or f == "killguard.py"):
                    continue
                if _python_file(os.path.join(root, f)):
                    out.append(os.path.join(root, f))
    return sorted(out)


def installs_guard(src):
    """True when `src` calls killguard.install(): a test helper that cannot import sampo.killsafe (run.py runs alone in a
    scratch folder) has the runner's guard on os.kill/os.killpg itself, which refuses what killsafe would."""
    return any(isinstance(n, ast.Call) and _chain(n.func) == ["killguard", "install"] for n in ast.walk(ast.parse(src)))


class TheShape(unittest.TestCase):
    """The next new kill site that signals with a nonzero signal must go through sampo.killsafe, or this fails."""

    def test_no_nonzero_signal_os_kill_in_lib_outside_the_helper(self):
        bad = []
        for path in lib_files():
            if os.path.basename(path) == "killsafe.py":
                continue
            with open(path, encoding="utf-8") as f:
                src = f.read()
            if "kill" not in src:
                continue
            if os.sep + "tests" + os.sep in path and installs_guard(src):
                continue  # a test helper under the runner's own guard (the next test pins which)
            try:
                sites = kill_sites(src, path)
            except SyntaxError:
                if path.endswith(".py"):
                    raise
                continue  # a script whose first line says python but is not
            for line, why in sites:
                bad.append(f"{os.path.relpath(path, os.path.dirname(HERE))}:{line}: {why}")
        self.assertFalse(bad, "route these through sampo.killsafe.kill/killpg (T-5900: killpg(1) kills every process):\n"
                         + "\n".join(bad))

    def test_the_scan_reaches_the_test_helpers_and_the_hooks(self):
        names = {os.path.relpath(p, os.path.dirname(HERE)) for p in lib_files()}
        for must in ("tests/forkcli.py", "tests/run.py", "tests/helpers.py", "lib/sampo/core.py", "hooks/wait-timer"):
            self.assertIn(must, names)
        self.assertNotIn("tests/killguard.py", names)

    def test_a_test_helper_counts_as_guarded_only_when_it_installs_the_guard(self):
        self.assertTrue(installs_guard("import killguard\nkillguard.install()\n"))
        self.assertFalse(installs_guard("import killguard\n"))
        self.assertFalse(installs_guard("def f():\n    install()\n"))
        for name in ("forkcli.py", "run.py", "helpers.py", "bench_hooks.py"):
            with self.subTest(name=name), open(os.path.join(HERE, name), encoding="utf-8") as f:
                self.assertTrue(installs_guard(f.read()))

    def test_the_helper_is_the_only_file_that_calls_them(self):
        with open(os.path.join(LIB, "sampo", "killsafe.py"), encoding="utf-8") as f:
            sites = kill_sites(f.read())
        self.assertEqual(len(sites), 2, sites)

    def test_the_scan_sees_each_form(self):
        cases = {
            "import os\nos.killpg(p.pid, 9)\n": 1,
            "import os\nos.kill(pid, signal.SIGTERM)\n": 1,
            "import os as _o\n_o.kill(pid, sig)\n": 1,
            "import os\n(os.killpg if g else os.kill)(pid, sig)\n": 2,
            "from os import killpg\n": 1,
            "import os\nk = os.kill\n": 1,
            "import os\ngetattr(os, 'killpg')(1, 9)\n": 1,
            "import os\nos.kill(pid, 0)\nos.killpg(pid, 0)\n": 0,
            "import os\nos.kill(pid, signal=0)\n": 1,
            "import os\nos.kill(pid, False)\n": 1,
            "import subprocess\nproc.kill()\n": 0,
            "from sampo import core\ncore.os.kill(pid, 9)\n": 1,
            "import posix\nposix.kill(pid, 9)\n": 1,
            "import posix as p\np.killpg(pid, 9)\n": 1,
            "import signal\nsignal.pthread_kill(tid, 9)\n": 1,
            "from signal import pthread_kill\n": 1,
            "from os import kill as k\n": 1,
            "from posix import killpg\n": 1,
            "from sampo import core\ngetattr(core.os, 'kill')(1, 9)\n": 1,
            "import os\ngetattr(os, \"killpg\")(1, 9)\n": 1,
            "import signal\nsignal.pthread_kill(tid, 0)\n": 0,
            "import os\nself.os.kill(pid, 9)\n": 1,
        }
        for src, n in cases.items():
            with self.subTest(src=src):
                self.assertEqual(len(kill_sites(src)), n, kill_sites(src))


if __name__ == "__main__":
    unittest.main()
