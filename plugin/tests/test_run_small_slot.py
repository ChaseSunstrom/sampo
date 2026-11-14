"""T-4650: a small run (a builder's one module, a proof's start-tree run) gets a slot in seconds while two large runs
hold the pool. The scenario is real: the real run.py in a scratch repo, a pool of POOL slots, two "large runs" (idle
processes that hold slots through run.cpu_slot, so no CPU is used), then a small run; its wall clock is the measure.
Measured on the runner before this change (POOL 8, the pool full): a module of 4 classes of 3 tests waited the whole
LIMIT (killed at 30 s; on the machine 411 s of 411 s for 13 tests, 285 s of 287 s for a slice): only a run of at most
two classes had an express slot."""
import os
import select
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from helpers import ForemanTestCase

import run

POOL = 8  # the scenario's pool: two large runs fill it (or, with a reserve, everything but the reserved slots)
LIMIT = 30  # a small run still waiting after this long is killed: that is the "before" figure, a floor
HOLD = ("import sys, threading, time; sys.path.insert(0, %r); import run\n"
        "def hold():\n"
        "    with run.cpu_slot(wait=120, poll=0.02) as s:\n"
        "        print(s, flush=True); time.sleep(300)\n"
        "ts = [threading.Thread(target=hold, daemon=True) for _ in range(int(sys.argv[1]))]\n"
        "[t.start() for t in ts]\n"
        "time.sleep(300)\n")


def module(classes, tests, nap=0.0, log=""):
    """A test module of `classes` classes of `tests` tests each; with nap, each test idles that long and logs its span."""
    LOG = log
    body = "import os, time, unittest\n"
    for c in range(classes):
        body += f"\n\nclass C{c}(unittest.TestCase):\n"
        for t in range(tests):
            body += f"    def test_{t}(self):\n"
            if nap:
                body += (f"        a = time.time(); time.sleep({nap})\n"
                         f"        open({LOG!r}, 'a').write(f'{{a}} {{time.time()}}\\n')\n")
            else:
                body += "        pass\n"
    return body


def peak(path):
    """The most test spans in the log that overlapped in time."""
    with open(path) as f:
        spans = [tuple(map(float, line.split())) for line in f.read().splitlines() if line.strip()]
    marks = sorted([(a, 1) for a, _ in spans] + [(b, -1) for _, b in spans], key=lambda m: (m[0], m[1]))
    now = top = 0
    for _, d in marks:
        now += d
        top = max(top, now)
    return top


class Reserve(ForemanTestCase):
    """The reserved slots in one process: who may take them, and that the pool is never exceeded."""

    def setUp(self):
        super().setUp()
        self.patch = mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.tmp, "SAMPO_SLOTS": str(POOL)})
        self.patch.start()
        self.addCleanup(self.patch.stop)
        for k in (run.SLOT_ENV, "FOREMAN_SLOT", "FOREMAN_SLOTS", "SAMPO_SLOT_PRIORITY", "FOREMAN_SLOT_PRIORITY", run.RESERVE_ENV):
            os.environ.pop(k, None)
        said = mock.patch.object(run, "_SAID", [])
        said.start()
        self.addCleanup(said.stop)

    def hold(self, small=False, wait=5):
        """A slot held by a thread until the test ends: its name (None when it got none within `wait`)."""
        held, free, box = threading.Event(), threading.Event(), []

        def body():
            with run.cpu_slot(wait=wait, poll=0.01, small=small) as s:
                box.append(s)
                held.set()
                free.wait(30)

        t = threading.Thread(target=body, daemon=True)
        t.start()
        self.addCleanup(lambda: (free.set(), t.join(30)))
        self.assertTrue(held.wait(wait + 5))
        return box[0]

    def test_the_reserve_is_two_slots_of_a_pool_of_eight_and_none_of_a_small_pool(self):
        self.assertEqual([run.reserved(n) for n in (0, 1, 3, 4, 7, 8, 16)], [0, 0, 0, 1, 1, 2, 2])
        self.assertEqual(run.lanes(8), ((6, 7), range(6)))
        self.assertEqual(run.lanes(2), (run.EXPRESS, range(2)), "a pool under four keeps the two extra express files")

    def test_the_off_switch_puts_back_the_express_files_and_the_whole_pool(self):
        with mock.patch.dict(os.environ, {run.RESERVE_ENV: "0"}):
            self.assertEqual(run.reserved(8), 0)
            self.assertEqual(run.lanes(8), (run.EXPRESS, range(8)))
            self.assertFalse(run.small_selection(["A", "B", "C", "D"], [], {}, {}), "only two classes are small again")
            self.assertEqual([self.hold(wait=0.3) for _ in range(8)].count(None), 0)

    def test_a_large_run_never_takes_a_reserved_slot(self):
        got = [self.hold() for _ in range(6)]
        self.assertEqual(sorted(got), list(range(6)))
        self.assertIsNone(self.hold(wait=0.3), "the ordinary slots are held: a large run waits out and gets none")
        self.assertEqual(self.hold(small=True), 6, "and both reserved slots are still free for a small run")

    def test_a_small_run_takes_a_reserved_slot_while_every_ordinary_one_is_held(self):
        [self.hold() for _ in range(6)]
        got = {self.hold(small=True), self.hold(small=True)}
        self.assertEqual(got, {6, 7})

    def test_the_processes_at_once_never_exceed_the_pool(self):
        held = [self.hold() for _ in range(6)] + [self.hold(small=True) for _ in range(2)]
        self.assertEqual(sorted(held), list(range(8)))
        with mock.patch.object(run, "SMALL_WAIT", 0.3):
            self.assertIsNone(self.hold(small=True), "a third small run waits its bound, then runs without a slot")
        self.assertEqual(len([h for h in held if h is not None]), 8)

    def test_a_small_run_with_the_reserve_taken_uses_a_free_ordinary_slot(self):
        self.hold(small=True), self.hold(small=True)
        self.assertIn(self.hold(small=True), range(6))

    def test_small_by_classes_stays_two_and_by_expected_seconds_beyond(self):
        four = ["A", "B", "C", "D"]
        parts = {c: [f"{c}.t{i}" for i in range(3)] for c in four}
        self.assertTrue(run.small_selection(["A", "B"], [], {}, {}))
        self.assertTrue(run.small_selection(four, four, {}, parts), "12 tests of no known time: half a second each")
        self.assertTrue(run.small_selection(four, four, {c: 1.0 for c in four}, parts), "four classes of a second")
        self.assertFalse(run.small_selection(four, four, {c: 40.0 for c in four}, parts), "four classes of 40 s")
        self.assertFalse(run.small_selection([str(i) for i in range(9)], [], {}, {}), "nine classes are too many")


class Scenario(ForemanTestCase):
    """Two large runs hold the pool; the small run's wait is what it took beyond a run with nothing in the way."""

    def setUp(self):
        super().setUp()
        top = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, top, True)
        self.top, self.tests = top, os.path.join(top, "repo", "plugin", "tests")
        os.makedirs(self.tests)
        for name in ("run.py", "runcover.py"):
            shutil.copy(os.path.join(run.HERE, name), os.path.join(self.tests, name))
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("SAMPO_SLOT", "FOREMAN_SLOT")) and k not in (run.SLOT_ENV, "FM_RUN_DEADLINE")}
        self.env.update(XDG_RUNTIME_DIR=top, SAMPO_SLOTS=str(POOL), XDG_CACHE_HOME=os.path.join(top, "cache"),
                        FOREMAN_NO_TEST_CACHE="1", FM_TEST_TIMES=os.path.join(top, "t.json"))
        self.holders = []
        self.log = os.path.join(top, "conc.log")  # per test: the jobs of this module run side by side

    def module(self, name, classes, tests, nap=0.0):
        with open(os.path.join(self.tests, name + ".py"), "w") as f:
            f.write(module(classes, tests, nap, self.log))

    def large_runs(self, each=POOL // 2, env=None):
        """Two large runs, each asking for `each` pool slots and holding what it gets until the test ends; the slots
        they got (the names), once nothing more arrives for a second."""
        for _ in range(2):
            p = subprocess.Popen([sys.executable, "-c", HOLD % run.HERE, str(each)], stdout=subprocess.PIPE, bufsize=0,
                                 env=dict(self.env, **(env or {})))
            self.addCleanup(lambda p=p: (p.kill(), p.wait(), p.stdout.close()))
            self.holders.append(p)
        got, quiet = [], time.monotonic() + 1.0
        while time.monotonic() < quiet or (len(got) < 2 and time.monotonic() < quiet + 10):
            ready, _, _ = select.select([p.stdout for p in self.holders], [], [], 0.2)
            for f in ready:
                got += f.read1(4096).decode().split() if hasattr(f, "read1") else os.read(f.fileno(), 4096).decode().split()
                quiet = time.monotonic() + 1.0
        return got

    def small_run(self, *names, env=None, limit=LIMIT):
        """run.py on the named modules: (its wall clock in s, its exit status or None when killed at the limit)."""
        start = time.monotonic()
        try:
            r = subprocess.run([sys.executable, os.path.join(self.tests, "run.py"), *names], capture_output=True, text=True,
                               timeout=limit, env=dict(self.env, **(env or {})), cwd=os.path.dirname(os.path.dirname(self.tests)))
            return time.monotonic() - start, r.returncode
        except subprocess.TimeoutExpired:
            return float(limit), None

    def measure(self, label, *names, env=None):
        """(wait beyond an empty pool in s, exit status, the slots the large runs hold)."""
        base, code = self.small_run(*names, env=env)
        self.assertEqual(code, 0)
        if os.path.exists(self.log):
            os.unlink(self.log)  # the spans of the run with the pool empty are not the measured run's
        held = self.large_runs(env=env)
        t, code = self.small_run(*names, env=env)
        print(f"T-4650 scenario {label}: {t:.1f} s wall with {len(held)} of {POOL} slots held by two large runs, {base:.1f} s "
              f"with the pool empty -> waited {max(0.0, t - base):.1f} s (exit {code})", file=sys.stderr)
        return t - base, code, held

    def test_measure_one_class(self):
        self.module("test_one", 1, 13)
        wait, code, _ = self.measure("one class of 13 tests", "test_one")
        self.assertEqual(code, 0)
        self.assertLess(wait, 5)

    def test_measure_a_module_of_four_classes(self):
        self.module("test_four", 4, 3)
        wait, code, held = self.measure("one module of 4 classes, 12 tests", "test_four")
        self.assertEqual(code, 0, f"killed at {LIMIT} s: waited for a slot the whole time")
        self.assertLess(wait, 8, "a few seconds, not the large runs' time")
        self.assertEqual(sorted(map(int, held)), list(range(POOL - 2)), "the large runs hold the ordinary slots only")

    def test_a_small_module_runs_at_most_as_many_tests_at_once_as_the_slots_it_may_take(self):
        self.module("test_six", 6, 1, nap=0.6)
        wait, code, held = self.measure("one module of 6 classes, 6 idle tests", "test_six")
        self.assertEqual(code, 0, f"killed at {LIMIT} s")
        self.assertLessEqual(peak(self.log), 2, "the two reserved slots, with the pool full of large runs: no more at once")
        self.assertEqual(len(held), POOL - 2)

    def test_the_off_switch_is_todays_runner(self):
        self.module("test_four", 4, 3)
        off = {run.RESERVE_ENV: "0"}
        held = self.large_runs(env=off)
        self.assertEqual(sorted(map(int, held)), list(range(POOL)), "a large run takes any pool slot")
        t, code = self.small_run("test_four", env=off, limit=6)
        self.assertIsNone(code, "four classes are not small: it waits for the pool, as before")
        self.module("test_two", 2, 3)
        t, code = self.small_run("test_two", env=off, limit=LIMIT)
        self.assertEqual(code, 0)
        self.assertLess(t, 8, "two classes still take an express slot")


if __name__ == "__main__":
    unittest.main()
