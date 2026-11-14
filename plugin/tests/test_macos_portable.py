"""T-4633: the suite on macOS. CI run 43581600540 failed on its three non-blocking macOS jobs; the causes that need no Mac
to show are pinned here by simulating the Mac on Linux: no `timeout` command (a fixture verify ran it: exit 127), no
/proc (a process's liveness and start time were read only there), no /proc/sys/kernel/random/boot_id (a reboot was never
seen), and a job split too thin for the time its runner takes (181, 119 and 54 jobs never started at the deadline).
reruns: each test simulates the host's missing part itself, so none keeps state between runs."""
import builtins
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from test_perf_measure_first import PERF
from test_run_orphan import gone

from sampo import core as c
from sampo import hooks, live, py, recover, train

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WORKFLOW = os.path.join(ROOT, ".github", "workflows", "test.yml")
# CI run 43581600540: the macOS jobs' work in seconds, summed (the three shards ran 1992 to 1996 s against a 1980 s
# deadline with 181 + 119 + 54 jobs not started: about 6860 s with the rest)
MAC_WORK_SECS = 6900


def without_timeout(tmp, hide=("timeout", "gtimeout")):
    """A PATH folder of every executable the real PATH has except `hide`: macOS ships no timeout, gtimeout or setsid."""
    folder = os.path.join(tmp, "bin")
    os.makedirs(folder)
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            if name in hide or os.path.exists(os.path.join(folder, name)):
                continue
            src = os.path.join(d, name)
            if os.access(src, os.X_OK) and not os.path.isdir(src):
                os.symlink(src, os.path.join(folder, name))
    return folder


class NoTimeoutCommand(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(subprocess.run, ["rm", "-rf", self.tmp])
        self.env = dict(os.environ, PATH=without_timeout(self.tmp))

    def bash(self, cmd, **kw):
        return subprocess.run(["bash", "-c", cmd], env=self.env, capture_output=True, text=True, **kw)

    def test_the_simulated_host_has_no_timeout(self):
        self.assertEqual(self.bash("timeout 1 true").returncode, 127)

    def test_the_perf_fixture_verify_runs_on_a_host_without_timeout(self):
        with open(os.path.join(self.tmp, "test_calc.py"), "w") as f:
            f.write("print('ok')\n")
        got = self.bash(PERF["criteria"][0]["verify"], cwd=self.tmp)
        self.assertEqual(got.returncode, 0, got.stderr)


class NoProc(unittest.TestCase):
    """A host with no /proc: every open of a /proc path fails, as on macOS."""

    def setUp(self):
        real = builtins.open

        def opener(path, *a, **kw):
            if isinstance(path, str) and path.startswith("/proc"):
                raise FileNotFoundError(path)
            return real(path, *a, **kw)

        p = mock.patch.object(builtins, "open", opener)
        p.start()
        self.addCleanup(p.stop)
        self.child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(self.child.wait)
        self.addCleanup(self.child.kill)

    def test_a_running_process_is_not_gone_without_proc(self):
        self.assertFalse(gone(self.child.pid))

    def test_a_zombie_is_gone_without_proc(self):
        self.child.kill()
        end = time.monotonic() + 10
        while not gone(self.child.pid) and time.monotonic() < end:
            time.sleep(0.05)
        self.assertTrue(gone(self.child.pid))  # killed, not yet waited for: a zombie

    def test_a_running_process_has_a_start_time_without_proc(self):
        self.assertGreater(train._ticks(self.child.pid), 0)

    def test_the_start_time_is_steady_and_a_zombie_has_none_without_proc(self):
        first = train._ticks(self.child.pid)
        time.sleep(0.2)
        self.assertEqual(train._ticks(self.child.pid), first)
        self.child.kill()
        end = time.monotonic() + 10
        while train._ticks(self.child.pid) and time.monotonic() < end:
            time.sleep(0.05)
        self.assertEqual(train._ticks(self.child.pid), 0)

    def test_a_missing_process_has_no_start_time_without_proc(self):
        self.child.kill()
        self.child.wait()
        self.assertEqual(train._ticks(self.child.pid), 0)


class StartTimeWithoutProc(unittest.TestCase):
    """T-4672: the other /proc readers (core._self_ticks, live._ticks and _started) answer on a host with no /proc: the
    start time from ps, the same value for the same process, None for one that is gone; none raises."""

    def setUp(self):
        real = builtins.open

        def opener(path, *a, **kw):
            if isinstance(path, str) and path.startswith("/proc"):
                raise FileNotFoundError(path)
            return real(path, *a, **kw)

        p = mock.patch.object(builtins, "open", opener)
        p.start()
        self.addCleanup(p.stop)
        self.child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(self.child.wait)
        self.addCleanup(self.child.kill)

    def test_this_process_has_a_start_stamp_that_stays_the_same(self):
        c._SELF_START.clear()
        first = c._self_ticks()
        self.assertIsInstance(first, int)
        self.assertGreater(first, 0)
        self.assertEqual(c._self_ticks(), first)

    def test_the_stamp_is_read_once_per_process(self):
        c._SELF_START.clear()
        c._self_ticks()
        with mock.patch("subprocess.run", side_effect=AssertionError("ps asked twice for this process")):
            self.assertGreater(c._self_ticks(), 0)

    def test_a_running_process_has_a_stamp_and_a_start_time_near_now(self):
        stamp = live._ticks(self.child.pid)
        self.assertGreater(stamp, 0)
        self.assertEqual(live._ticks(self.child.pid), stamp)
        self.assertLess(abs(live._started(self.child.pid) - time.time()), 120)

    def test_a_process_that_is_gone_has_none_and_nothing_raises(self):
        self.child.kill()
        self.child.wait()
        self.assertIsNone(live._ticks(self.child.pid))
        self.assertIsNone(live._started(self.child.pid))

    def test_a_host_with_no_ps_either_gives_none_not_an_error(self):
        c._SELF_START.clear()
        with mock.patch("subprocess.run", side_effect=FileNotFoundError("ps")):
            self.assertIsNone(c._self_ticks())
            self.assertIsNone(live._ticks(self.child.pid))
            self.assertIsNone(live._started(self.child.pid))

    def test_a_job_s_progress_file_is_read_on_this_host(self):
        c._SELF_START.clear()
        rec = {"done": 5, "total": 10, "unit": "x", "note": "", "t0": 1.0, "t": 3.0, "d0": 1.0, "start": c._self_ticks()}
        self.assertEqual(live._file_progress(os.getpid(), rec)["done"], 5)


class HealOnMac(unittest.TestCase):
    """T-4672: hooks._heal_note on a host with no boot id file takes the kernel's boot session id (macOS), so the heal
    runs once per boot there too instead of at every SessionStart."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(subprocess.run, ["rm", "-rf", self.tmp])
        for p in (mock.patch.object(hooks, "BOOT_ID", os.path.join(self.tmp, "none")),
                  mock.patch.object(sys, "platform", "darwin"),
                  mock.patch.dict(os.environ, {"FOREMAN_STATE": self.tmp}),
                  mock.patch.object(c, "state_dir", lambda: self.tmp)):
            p.start()
            self.addCleanup(p.stop)
        for cached in (recover._read_boot, recover._mac_boot):
            cached.cache_clear()
            self.addCleanup(cached.cache_clear)

    def sysctl(self, out="6A1C8E0F-0000-4000-8000-0123456789AB\n"):
        return mock.patch("subprocess.run", lambda argv, *a, **kw: subprocess.CompletedProcess(argv, 0, out, ""))

    def test_a_boot_that_was_healed_is_not_healed_again(self):
        with open(os.path.join(self.tmp, "healed-boot"), "w") as f:
            f.write("6A1C8E0F-0000-4000-8000-0123456789AB\n")
        with self.sysctl(), mock.patch.object(py, "heal", side_effect=AssertionError("healed twice in a boot")):
            self.assertEqual(hooks._heal_note(), "")

    def test_a_new_boot_heals_and_records_it(self):
        calls = []
        with self.sysctl(), mock.patch.object(py, "heal", lambda root: calls.append(root) or ([], [])):
            self.assertEqual(hooks._heal_note(), "")
        self.assertEqual(len(calls), 1)
        with open(os.path.join(self.tmp, "healed-boot")) as f:
            self.assertEqual(f.read().strip(), "6A1C8E0F-0000-4000-8000-0123456789AB")

    def test_a_failing_sysctl_leaves_the_heal_at_every_session_as_before(self):
        calls = []
        with mock.patch("subprocess.run", side_effect=FileNotFoundError("sysctl")), \
                mock.patch.object(py, "heal", lambda root: calls.append(root) or ([], [])):
            hooks._heal_note()
            hooks._heal_note()
        self.assertEqual(len(calls), 2)


class SetsidFreeChild(unittest.TestCase):
    def test_the_bounded_run_test_makes_its_outliving_child_without_the_setsid_command(self):
        import test_run_bounded_closes as t
        self.assertNotRegex(t.OUTLIVING, r"(?<![\w.])setsid\s")  # os.setsid in python3: macOS has no setsid command

    def test_the_outliving_child_starts_where_no_setsid_command_exists(self):
        import test_run_bounded_closes as t
        tmp = tempfile.mkdtemp()
        self.addCleanup(subprocess.run, ["rm", "-rf", tmp])
        env = dict(os.environ, PATH=without_timeout(tmp, hide=("setsid",)))
        got = subprocess.run(["bash", "-c", f"echo parent; {t.OUTLIVING}; true"], env=env, capture_output=True,
                             text=True, timeout=30)
        self.assertEqual((got.returncode, got.stdout.strip(), got.stderr.strip()), (0, "parent", ""))


class BootOnMac(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(subprocess.run, ["rm", "-rf", self.tmp])
        self.missing = os.path.join(self.tmp, "no-boot-id")  # /proc/sys/kernel/random/boot_id does not exist on macOS
        for p in (mock.patch.object(hooks, "BOOT_ID", self.missing), mock.patch.object(sys, "platform", "darwin")):
            p.start()
            self.addCleanup(p.stop)
        for cached in (recover._read_boot, recover._mac_boot):
            cached.cache_clear()
            self.addCleanup(cached.cache_clear)

    def sysctl(self, out="6A1C8E0F-0000-4000-8000-0123456789AB\n"):
        def run(argv, *a, **kw):
            self.assertEqual(argv[:2], ["sysctl", "-n"])
            return subprocess.CompletedProcess(argv, 0, out, "")
        return mock.patch("subprocess.run", run)

    def test_the_boot_id_comes_from_the_kernel_boot_session_on_a_mac(self):
        with self.sysctl():
            self.assertEqual(recover.boot(), "6A1C8E0F-0000-4000-8000-0123456789AB")

    def test_a_failing_sysctl_leaves_no_boot_id_as_before(self):
        def run(argv, *a, **kw):
            raise FileNotFoundError("sysctl")
        with mock.patch("subprocess.run", run):
            self.assertEqual(recover.boot(), "")


    def test_the_boot_time_comes_from_kern_boottime_on_a_mac(self):
        real = builtins.open

        def opener(path, *a, **kw):
            if isinstance(path, str) and path.startswith("/proc"):
                raise FileNotFoundError(path)
            return real(path, *a, **kw)

        with self.sysctl("{ sec = 1731500000, usec = 123456 } Fri Nov 13 12:13:20 2026\n"), \
                mock.patch.object(builtins, "open", opener):
            self.assertEqual(recover.btime(), 1731500000.0)


class Shards(unittest.TestCase):
    """The workflow's text, read without a YAML parser: every macOS shard is listed, the divisor counts them, and each
    gets a share its deadline can finish; the Linux jobs stay as they were."""

    def setUp(self):
        with open(WORKFLOW) as f:
            self.text = f.read()
        self.run_line = re.search(r"run: python3 plugin/tests/run\.py (.*)", self.text).group(1)

    def mac_shards(self):
        return [int(n) for n in re.findall(r"- os: macos-latest.*\n(?:\s+(?!- os).*\n)*?\s+shard: (\d+)", self.text)]

    def pair(self, flag):
        return [int(n) for n in re.search(rf"--{flag} \$\{{\{{ matrix\.experimental && (\d+) \|\| (\d+) \}}\}}",
                                          self.run_line).groups()]

    def test_every_macos_shard_is_listed_and_the_divisor_counts_them(self):
        shards = self.mac_shards()
        self.assertEqual(shards, list(range(1, len(shards) + 1)))
        divisor = re.search(r"--shard \$\{\{ matrix\.shard \}\}/\$\{\{ matrix\.experimental && (\d+) \|\| (\d+) \}\}",
                            self.run_line)
        self.assertEqual(int(divisor.group(1)), len(shards))

    def test_the_macos_shards_have_the_time_the_suite_takes_there(self):
        n, (mac_deadline, _) = len(self.mac_shards()), self.pair("deadline")
        # 80%: a shard's deadline also pays for the checkout, the setup and the slowest class, which runs alone
        self.assertGreaterEqual(n * mac_deadline * 0.8, MAC_WORK_SECS, f"{n} shards of {mac_deadline} s")

    def test_the_linux_jobs_are_unchanged(self):
        self.assertIn("os: [ubuntu-latest]", self.text)
        self.assertIn("shard: [1, 2]", self.text)
        self.assertEqual(self.pair("deadline")[1], 1380)
        self.assertEqual(self.pair("timeout")[1], 480)
        self.assertRegex(self.run_line, r"--shard \$\{\{ matrix\.shard \}\}/\$\{\{ matrix\.experimental && \d+ \|\| 2 \}\}")


if __name__ == "__main__":
    unittest.main()
