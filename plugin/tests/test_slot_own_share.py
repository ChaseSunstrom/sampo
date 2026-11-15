"""T-4685: a narrow run (a handful of named modules) starts when Sampo's own jobs leave a slot of the pool free, whatever the
machine's load, which the slot never reads; it still waits when the pool is full, never takes the last reserved slot, and
never goes past the pool (the cap). A whole-suite run is unchanged. The load is a fake: os.getloadavg is patched, nothing
is spun."""
import io
import os
import threading
import time
from unittest import mock

from helpers import ForemanTestCase

import run

LOCKS = """\
1: FLOCK  ADVISORY  WRITE 4242 00:41:100 0 EOF
2: FLOCK  ADVISORY  WRITE 4242 00:41:101 0 EOF
3: POSIX  ADVISORY  WRITE 777 00:41:102 0 EOF
4: FLOCK  ADVISORY  WRITE 5151 00:41:999 0 EOF
5: FLOCK  ADVISORY  WRITE 4343 00:41:106 0 EOF
garbled line
"""


class OwnShare(ForemanTestCase):
    N = 8  # a pool of 8: slots 0..5 ordinary, 6 narrow-or-small, 7 small only

    def setUp(self):
        super().setUp()
        env = {"XDG_RUNTIME_DIR": self.tmp, "SAMPO_SLOTS": str(self.N)}
        self.patch = mock.patch.dict(os.environ, env)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        for k in (run.SLOT_ENV, "FOREMAN_SLOT", "FOREMAN_SLOTS", "SAMPO_SLOT_PRIORITY", "FOREMAN_SLOT_PRIORITY",
                  "SAMPO_SLOT_RESERVE", "FOREMAN_SLOT_RESERVE", "SAMPO_SLOT_LOW", "FOREMAN_SLOT_LOW", run.TRACE_ENV):
            os.environ.pop(k, None)
        said = mock.patch.object(run, "_SAID", [])
        said.start()
        self.addCleanup(said.stop)
        load = mock.patch.object(run.os, "getloadavg", return_value=(90.0, 80.0, 70.0))  # the other project's load
        load.start()
        self.addCleanup(load.stop)
        self.folder = run._slot_folder()
        self.fds = []
        self.addCleanup(self.release)

    def release(self):
        for fd in self.fds:
            os.close(fd)
        self.fds = []

    def fill(self, names):
        """Hold these slots as another run.py would: a lock on each, until the test ends."""
        for i in names:
            got = run._take(self.folder, [i])
            self.assertIsNotNone(got, f"slot {i} was not free")
            self.fds.append(got[1])

    def take(self, wait=0.4, **kw):
        t = time.monotonic()
        with run.cpu_slot(wait=wait, poll=0.01, **kw) as s:
            return s, time.monotonic() - t

    # the criterion: under the share, a narrow run starts, with the machine's load high
    def test_a_narrow_run_starts_on_the_reserve_when_the_ordinary_slots_are_all_held(self):
        self.fill(range(6))  # one other run.py holds the 6 ordinary slots (measured: 3+ minutes), the reserve is idle
        slot, waited = self.take(wait=5, narrow=True)
        self.assertEqual(slot, 6)
        self.assertLess(waited, 1, "load 90 on the machine is not Sampo's: it started at once")

    def test_a_narrow_run_takes_an_ordinary_slot_before_the_reserve(self):
        self.fill([0, 1, 2, 3, 4])
        self.assertEqual(self.take(narrow=True)[0], 5)

    def test_a_narrow_run_takes_the_reserve_beside_a_waiting_gate(self):
        # measured: the other run.py was a priority one with prio- markers; a waiting ordinary run leaves freed slots to
        # them, and the reserve's slot sat idle for the 3+ minutes of the wait
        self.fill(range(6))
        mark = run._mark(self.folder)
        self.addCleanup(os.close, mark[0])
        self.assertTrue(run._prio_waiting(self.folder))
        slot, waited = self.take(wait=5, narrow=True)
        self.assertEqual(slot, 6)
        self.assertLess(waited, 1)
        self.assertIsNone(self.take(wait=0.3)[0], "an ordinary run still defers and waits")

    def test_an_ordinary_run_still_waits_when_the_ordinary_slots_are_held(self):
        self.fill(range(6))
        slot, waited = self.take(wait=0.4)
        self.assertIsNone(slot, "a whole-suite run does not draw on the reserve")
        self.assertGreaterEqual(waited, 0.35)

    def test_a_narrow_run_waits_when_the_pool_is_full_and_leaves_the_last_reserved_slot_to_small_runs(self):
        self.fill(range(7))  # 0..6 held: only slot 7, the small runs' own, is free
        slot, waited = self.take(wait=0.4, narrow=True)
        self.assertIsNone(slot, "the last reserved slot is not a narrow run's")
        self.assertGreaterEqual(waited, 0.35)
        self.assertEqual(self.take(small=True)[0], 7, "and a small run still gets it")

    def test_a_small_run_is_unchanged(self):
        self.fill(range(6))
        self.assertEqual(self.take(small=True)[0], 6)
        self.fill([6])
        self.assertEqual(self.take(small=True)[0], 7)

    # the cap: the pool is the share; narrow runs never hold more than it
    def test_narrow_runs_never_hold_more_than_the_pool_less_the_last_reserved_slot(self):
        held, peak, lock, got = [0], [0], threading.Lock(), []

        def job():
            with run.cpu_slot(wait=0.6, poll=0.01, narrow=True) as s:
                if s is None:
                    return
                with lock:
                    held[0] += 1
                    peak[0] = max(peak[0], held[0])
                    got.append(s)
                time.sleep(0.3)
                with lock:
                    held[0] -= 1

        ts = [threading.Thread(target=job) for _ in range(20)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(peak[0], 7, "6 ordinary + 1 narrow, never the 8th")
        self.assertNotIn(7, got)

    def test_a_pool_without_two_reserved_slots_gives_a_narrow_run_nothing_extra(self):
        for n, extra in ((8, (6,)), (12, (10,)), (16, (14,)),(7, ()), (4, ()), (3, ())):
            self.assertEqual(run.narrow_lane(n), extra, n)
        with mock.patch.dict(os.environ, {"SAMPO_SLOT_RESERVE": "0"}):
            self.assertEqual(run.narrow_lane(8), (), "the reserve's off switch takes the narrow lane with it")

    def test_a_priority_or_low_run_does_not_draw_on_the_reserve(self):
        self.fill(range(6))
        for env in ({"SAMPO_SLOT_PRIORITY": "1"}, {"SAMPO_SLOT_LOW": "1"}):
            with mock.patch.dict(os.environ, env):
                self.assertIsNone(self.take(wait=0.3, narrow=True, low=env.keys() == {"SAMPO_SLOT_LOW"})[0], env)

    # what is narrow
    def test_narrow_is_a_handful_of_named_modules_and_never_the_whole_suite(self):
        self.assertTrue(run.is_narrow(["test_a"]))
        self.assertTrue(run.is_narrow(["test_a"] * run.NARROW_MODULES))
        self.assertFalse(run.is_narrow(["test_a"] * (run.NARROW_MODULES + 1)))
        self.assertFalse(run.is_narrow([], [], None), "no names, -k or -p: the whole suite")
        self.assertFalse(run.is_narrow([], [], ["test_*.py"]))
        self.assertFalse(run.is_narrow(["test_a"], [], ["test_*.py"]))
        self.assertTrue(run.is_narrow([], ["name"], None), "-k alone is a part of a module")

    def test_run_one_passes_narrow_to_the_slot(self):
        seen = {}

        def fake(wait, small=False, low=False, narrow=False, **kw):
            seen["narrow"] = narrow
            raise RuntimeError("stop")

        with mock.patch.object(run, "cpu_slot", fake):
            with self.assertRaises(RuntimeError):
                run.run_one("test_x.X", [], narrow=True)
        self.assertTrue(seen["narrow"])

    # what the wait read, kept (the debug env)
    def test_lock_holders_reads_the_flock_lines_of_the_slots_only(self):
        inodes = {0: 100, 1: 101, 2: 102, 6: 106, "x0": 500}
        self.assertEqual(run.lock_holders(LOCKS, inodes), {0: 4242, 1: 4242, 6: 4343})

    def test_the_trace_line_names_the_holders_and_says_the_load_is_not_read(self):
        held = {i: 4242 for i in range(6)}
        line = run.trace_line(146, False, 8, held, 117.6, 16, (1463, 1556), (100, 200), narrow=True)
        self.assertIn("waited 146 s, narrow run", line)
        self.assertIn("pool 8 (6 ordinary, 2 small-only)", line)
        self.assertIn("held 6/6 ordinary, 0 small-only/express, by 1 run.py (pids 4242)", line)
        self.assertIn("load 117.6 on 16 CPUs (not read by the slot)", line)
        self.assertIn("cgroup throttled 1363 of 1356 periods since the first trace", line)
        self.assertTrue(line.endswith("\n") and line.count("\n") == 1)

    def test_a_waiting_run_prints_the_trace_only_with_the_env_and_once_per_interval(self):
        self.fill(range(8))
        err = io.StringIO()
        with mock.patch.object(run.sys, "stderr", err), mock.patch.dict(run._TRACE, {"at": None, "base": None}):
            self.take(wait=0.3)
        self.assertNotIn("slot trace", err.getvalue(), "off without SAMPO_SLOT_TRACE=1")
        err = io.StringIO()
        with mock.patch.object(run.sys, "stderr", err), mock.patch.dict(run._TRACE, {"at": None, "base": None}), \
                mock.patch.dict(os.environ, {run.TRACE_ENV: "1"}):
            self.take(wait=0.3)
        lines = [ln for ln in err.getvalue().splitlines() if "slot trace" in ln]
        self.assertEqual(len(lines), 1, "many refusals in the interval, one line")
        self.assertIn("held 6/6 ordinary, 2 small-only/express", lines[0])
        self.assertIn("load 90.0 on", lines[0])
        self.assertIn(f"by 1 run.py (pids {os.getpid()})", lines[0], "this process holds the fake run's locks")

    def test_the_trace_counts_the_holders_without_proc_locks_as_macos_has_none(self):
        """T-4708: macOS has no /proc/locks, and the holder count read only that: 'held 0/6, by 0 run.py' with the pool
        full. The count is the slots' own lock state (a probe), and the pid the holder wrote in its slot file."""
        self.fill(range(8))
        real_open = open

        def no_proc(path, *a, **kw):
            if str(path).startswith("/proc/locks"):
                raise FileNotFoundError(path)
            return real_open(path, *a, **kw)

        err = io.StringIO()
        with mock.patch("builtins.open", no_proc), mock.patch.object(run.sys, "stderr", err), \
                mock.patch.dict(run._TRACE, {"at": None, "base": None}), mock.patch.dict(os.environ, {run.TRACE_ENV: "1"}):
            self.assertRaises(FileNotFoundError, open, "/proc/locks")
            self.take(wait=0.3)
        lines = [ln for ln in err.getvalue().splitlines() if "slot trace" in ln]
        self.assertEqual(len(lines), 1)
        self.assertIn("held 6/6 ordinary, 2 small-only/express", lines[0])
        self.assertIn(f"by 1 run.py (pids {os.getpid()})", lines[0])

    def test_a_probe_of_the_slots_leaves_a_free_slot_free_and_a_held_one_held(self):
        self.fill([1, 3])
        self.assertEqual(run.slot_holders(self.folder, 8), {1: os.getpid(), 3: os.getpid()})
        self.assertEqual(run._take(self.folder, [0, 1, 2])[0], 0, "the probe let go of every slot it tried")
        self.assertEqual(run._take(self.folder, [2])[0], 2)

    def test_a_slot_file_without_a_pid_is_still_held(self):
        got = run._take(self.folder, [4])
        self.fds.append(got[1])
        os.truncate(os.path.join(self.folder, "4"), 0)
        self.assertEqual(run.slot_holders(self.folder, 8), {4: 0})


if __name__ == "__main__":
    import unittest
    unittest.main()
