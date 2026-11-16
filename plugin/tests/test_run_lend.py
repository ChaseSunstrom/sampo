"""T-4688: the ordinary slots are shared fairly between runs at the moment a slot is taken. Measured 2026-11-14 (T-4685's slot
trace): one gate run.py held all 6 ordinary slots for 10+ minutes while its jobs used about 1.6 of 8 cores, and a builder's
12-module run waited 674 s, most of it for slots. A run's jobs queue behind each other in a pool of threads, and each job that
ends took the next at once, so a second run never found a slot free. Now a run that holds its share (ceil(pool / runs), the
runs being those holding a slot or waiting for one) leaves a freed slot to a waiting run that holds less. The refusal is
bounded for the process, not per job: one window (FAIR_MAX) from its first refusal, then a cooldown (FAIR_COOL); and a waiter
that kept its marker without taking a slot it was left for FAIR_STUCK is not waited for. (The brief's option A, lending a slot
by measured CPU, was not built: a job's slot cannot be re-taken without stopping its process tree, and nothing measures CPU
while a job runs; research T-4688-rival.) The slot files beyond the pool that nobody holds are removed at the pool's first use.
The tests: a unit slice with fake runs (os.getpid patched, so the processes need not be real) and a fake clock for the bounds
(no assertion waits on wall seconds), and the scenario with real processes, two runs of six threads each queuing six short
jobs, the second started beside the first; the measure is how many of the first run's jobs ended before the second got its
first slot (events, not seconds: the time a job takes is the machine's)."""
import contextlib
import fcntl
import os
import subprocess
import sys
import threading
import time
import types
import unittest
from unittest import mock

from helpers import ForemanTestCase

import run

POOL, ORDINARY = 8, 6  # a pool of 8 is 6 ordinary slots and 2 reserved for small runs
THREADS, JOBS, NAP = 6, 6, 0.4  # a run: 6 threads (-j) each queue 6 jobs; thread i naps NAP * (1 + 0.15 i), so ends stagger
# one os.write per event: a line is never split between two runs (O_APPEND writes of a few bytes are atomic). Run "a" is the
# one that came first: it runs warm-up jobs until another run's waiting marker is in the slot folder, says "m", then queues its
# JOBS jobs; run "b" just queues its JOBS jobs.
LOOP = ("import os, sys, threading, time; sys.path.insert(0, @HERE@); import run\n"
        "log, nap, role = sys.argv[1], float(sys.argv[2]), sys.argv[3]\n"
        "fd = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT)\n"
        "folder, said, lock = run._slot_folder(), [], threading.Lock()\n"
        "def put(c):\n"
        "    os.write(fd, f'{time.time()} {os.getpid()} {c}\\n'.encode())\n"
        "def other_waits():\n"
        "    return any(x.startswith(('wait-', 'prio-')) and x.split('-')[1] != str(os.getpid()) for x in os.listdir(folder))\n"
        "def job(i):\n"
        "    with run.cpu_slot(wait=300, poll=0.02) as s:\n"
        "        put('+' if s is not None else 'n'); time.sleep(nap * (1 + 0.15 * i)); put('-')\n"
        "def lane(i):\n"
        "    if role == 'a':\n"
        "        end = time.monotonic() + 120\n"
        "        while time.monotonic() < end:\n"
        "            if other_waits():\n"
        "                with lock:\n"
        "                    if not said:\n"
        "                        said.append(1); put('m')\n"
        "                break\n"
        "            job(i)\n"
        "    for _ in range(@JOBS@):\n"
        "        job(i)\n"
        "ts = [threading.Thread(target=lane, args=(i,)) for i in range(@THREADS@)]\n"
        "[t.start() for t in ts]\n"
        "[t.join() for t in ts]\n").replace("@JOBS@", str(JOBS)).replace("@THREADS@", str(THREADS))


def events(path):
    """[(time, pid, '+' | '-' | 'n' | 'm')] of the log, in time order."""
    with open(path) as f:
        rows = [ln.split() for ln in f.read().splitlines() if ln.strip()]
    return sorted((float(t), int(p), c) for t, p, c in rows)


def shape(evs, first, second):
    """(the first run's jobs that ended after it saw the second waiting and before the second's first slot, the most slots held
    at once by both, and the most each held at the same moment while both held any)."""
    done, now, top, both = 0, {first: 0, second: 0}, 0, 0
    seen = began = False
    for _, pid, c in evs:
        if c == "m":
            seen = True
        elif c == "-":
            now[pid] -= 1
            done += seen and not began and pid == first
        elif c == "+":
            began = began or pid == second
            now[pid] += 1
        top = max(top, now[first] + now[second])
        both = max(both, min(now[first], now[second]))
    return done, top, both


class Clock:
    """A clock that moves only when run.py sleeps: a bound on waiting is then read in its own units, not the machine's."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Pool(ForemanTestCase):
    """A pool of 8 in a scratch folder; runs are faked by the pid the calls are made under."""

    def setUp(self):
        super().setUp()
        env = {"XDG_RUNTIME_DIR": self.tmp, "SAMPO_SLOTS": str(POOL)}
        self.patch = mock.patch.dict(os.environ, env)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        for k in (run.SLOT_ENV, "FOREMAN_SLOT", "FOREMAN_SLOTS", "SAMPO_SLOT_PRIORITY", "FOREMAN_SLOT_PRIORITY",
                  run.RESERVE_ENV, "SAMPO_SLOT_LOW", "FOREMAN_SLOT_LOW", run.TRACE_ENV, run.FAIR_ENV, "FOREMAN_SLOT_FAIR"):
            os.environ.pop(k, None)
        for name, value in (("_SAID", []), ("_YIELD", {"end": None, "cool": None, "seen": {}, "stuck": {}})):
            patched = mock.patch.object(run, name, value)
            patched.start()
            self.addCleanup(patched.stop)
        self.folder = run._slot_folder()
        self.fds = []
        self.addCleanup(self.release)

    def release(self):
        for fd in self.fds:
            with contextlib.suppress(OSError):
                os.close(fd)
        self.fds = []

    def as_pid(self, pid):
        return mock.patch.object(run.os, "getpid", return_value=pid)

    def fill(self, pid, names):
        """Slots held as run `pid` would hold them, until the test ends."""
        with self.as_pid(pid):
            for i in names:
                got = run._take(self.folder, [i])
                self.assertIsNotNone(got, f"slot {i} was not free")
                self.fds.append(got[1])

    def wait_for(self, pid, prefix="wait-"):
        """A live waiting marker of run `pid`."""
        with self.as_pid(pid):
            mark = run._mark(self.folder, prefix)
        self.assertIsNotNone(mark)
        self.fds.append(mark[0])
        return mark

    def over(self, pid, prio=False):
        with self.as_pid(pid):
            return run._over_share(self.folder, POOL, range(ORDINARY), prio)

    def take(self, pid, wait=0.4, **kw):
        with self.as_pid(pid), run.cpu_slot(wait=wait, poll=0.01, **kw) as s:
            return s

    @contextlib.contextmanager
    def fake_clock(self):
        clock = Clock()
        with mock.patch.object(run, "time", types.SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep, time=time.time)):
            yield clock

    def take_fake(self, pid, clock, wait=1e6, poll=0.5):
        """(the slot, the fake seconds the call waited) of a call that sleeps on the fake clock."""
        start = clock.now
        with self.as_pid(pid), run.cpu_slot(wait=wait, poll=poll) as s:
            return s, clock.now - start


class Share(Pool):
    def test_the_cap_is_the_pool_over_the_runs_and_never_under_one(self):
        self.assertEqual([run.fair_cap(6, n) for n in (1, 2, 3, 4, 6, 7, 20)], [6, 3, 2, 2, 1, 1, 1])
        self.assertEqual(run.fair_cap(6, 0), 6)

    def test_a_run_holding_most_slots_leaves_a_freed_one_to_a_waiting_run(self):
        self.fill(100, range(5))
        self.wait_for(200)
        self.assertTrue(self.over(100), "100 holds 5 of 6 and run 200 waits with none: the cap is 3")
        self.assertFalse(self.over(200), "the waiting run holds none: it takes what is free")

    def test_with_no_slot_free_there_is_nothing_to_leave(self):
        self.fill(100, range(ORDINARY))
        self.wait_for(200)
        self.assertFalse(self.over(100), "every ordinary slot is held: a refusal would leave nothing to anyone")

    def test_nobody_waiting_means_no_cap(self):
        self.fill(100, range(5))
        self.assertFalse(self.over(100), "nobody else wants one: take it")

    def test_a_run_under_its_share_takes_a_slot(self):
        self.fill(100, range(2))
        self.fill(300, [2, 3])
        self.wait_for(200)
        self.assertTrue(self.over(100), "3 runs make the cap 2: 100 holds 2 and run 200 waits with none")
        self.fill(200, [4])
        self.assertTrue(self.over(100), "200 holds 1, still under 2")
        self.fill(200, [5])
        self.assertFalse(self.over(100), "200 holds 2 and no slot is free")

    def test_a_waiter_at_its_share_is_not_waited_for_so_no_slot_idles(self):
        self.fill(100, [0, 1])
        self.fill(300, [2])
        self.fill(200, [3, 4])
        self.wait_for(200)
        self.assertFalse(self.over(100), "3 runs make the cap 2: 200 holds its 2, so 100 may take the free slot 5")

    def test_two_runs_one_under_its_share_the_other_leaves_the_free_slot(self):
        self.fill(100, [0, 1, 2])
        self.fill(200, [3, 4])
        self.wait_for(200)
        self.assertTrue(self.over(100), "100 holds its 3 of 6 and 200, holding 2, is left the free slot")
        self.assertFalse(self.over(200))

    def test_every_run_keeps_at_least_one_slot(self):
        self.fill(100, range(5))
        for pid in range(201, 207):
            self.wait_for(pid)  # 7 runs: the cap is 1
        self.assertTrue(self.over(100))
        self.assertFalse(self.over(201), "a run holding none always takes one")

    def test_a_priority_run_keeps_half_the_pool_beside_many_waiting_builders(self):
        # T-3614: the merge train's gate got 1/N of the slots beside ~10 builders; the share must not give that back
        self.fill(100, range(2))
        for pid in range(201, 207):
            self.wait_for(pid)  # 7 runs: an ordinary run's share is 1
        self.assertTrue(self.over(100), "an ordinary run holding 2 leaves the next to the builders")
        self.assertFalse(self.over(100, prio=True), "a priority run holds 2 of its 3 (half the 6)")
        self.fill(100, [2])
        self.assertTrue(self.over(100, prio=True), "at its half it leaves the rest")

    def test_a_waiting_priority_run_is_entitled_to_half_before_an_ordinary_run_takes_more(self):
        self.fill(100, range(4))  # an ordinary run holds 4 of 6
        self.wait_for(200, "prio-")  # a gate waits with none
        self.assertTrue(self.over(100), "its share is 3 of 2 runs, and the gate holds none (under its half)")
        self.fill(200, [4])
        self.assertTrue(self.over(100), "the gate holds 1, under its half of 3")

    def test_a_dead_runs_marker_is_not_a_waiter(self):
        self.fill(100, range(5))
        mark = self.wait_for(200)
        os.close(mark[0])  # the holder dies: the lock goes
        self.fds.pop()
        self.assertFalse(self.over(100))
        self.assertFalse(os.path.exists(mark[1]), "and the dead marker is removed")

    def test_the_run_a_marker_names_is_not_another_run_when_it_is_this_one(self):
        self.fill(100, range(5))
        self.wait_for(100)  # my own class waiting in my own pool
        self.assertFalse(self.over(100))

    def test_the_slot_goes_to_the_waiting_run_the_moment_the_holder_lets_go(self):
        self.fill(100, range(5))
        self.wait_for(200)  # 200 waits with none; slot 5 is free: 100 refuses it, 200 takes it
        self.assertIsNone(self.take(100, wait=0.3), "100 waited out its short wait without taking 200's slot")
        self.assertEqual(self.take(200, wait=60), 5)

    def test_the_off_switch_is_the_old_behaviour(self):
        self.fill(100, range(5))
        self.wait_for(200)
        with mock.patch.dict(os.environ, {run.FAIR_ENV: "0"}):
            self.assertEqual(self.take(100, wait=60), 5, "a run takes a free slot whoever waits")


class Bounds(Pool):
    """How long a refusal can cost, on a clock that moves only when run.py sleeps."""

    def setUp(self):
        super().setUp()
        self.fill(100, range(5))  # 100 holds 5 of 6 and slot 5 is free
        self.wait_for(200)  # 200 waits and never takes it

    def test_the_window_is_the_processs_not_a_jobs_so_many_jobs_cost_one_window_then_a_cooldown(self):
        with mock.patch.object(run, "FAIR_STUCK", 1e9), mock.patch.object(run, "FAIR_MAX", 50.0), \
                mock.patch.object(run, "FAIR_COOL", 300.0), self.fake_clock() as clock:
            slot, first = self.take_fake(100, clock)
            self.assertEqual(slot, 5)
            self.assertTrue(50.0 <= first <= 52.6, f"one window, 50 s and one sleep step: {first}")
            later = [self.take_fake(100, clock)[1] for _ in range(30)]
            self.assertEqual(later, [0.0] * 30, "thirty more jobs in the cooldown wait for nothing")
            clock.now += 300.0
            again = self.take_fake(100, clock)[1]
            self.assertTrue(50.0 <= again <= 52.6, f"after the cooldown the next refusal opens a new window: {again}")

    def test_the_cooldown_runs_from_the_windows_end_not_from_the_first_call_after_it(self):
        with mock.patch.object(run, "FAIR_STUCK", 1e9), mock.patch.object(run, "FAIR_MAX", 50.0), \
                mock.patch.object(run, "FAIR_COOL", 300.0), self.fake_clock() as clock:
            start = clock.now
            self.take_fake(100, clock)  # the window ends at start + 50; this call ends a sleep step later
            self.assertGreater(clock.now, start + 50.0)
            clock.now = start + 50.0 + 299.0
            self.assertEqual(self.take_fake(100, clock)[1], 0.0, "299 s after the window's end: still resting")
            clock.now = start + 50.0 + 300.0
            self.assertGreaterEqual(self.take_fake(100, clock)[1], 50.0, "300 s after the window's end: the next refusal opens a window")

    def test_a_waiter_that_holds_its_marker_and_takes_nothing_costs_the_stuck_time_once(self):
        with self.fake_clock() as clock:
            slot, first = self.take_fake(100, clock)
            self.assertEqual(slot, 5)
            self.assertTrue(run.FAIR_STUCK <= first <= run.FAIR_STUCK + 2.6, f"left for FAIR_STUCK, then not waited for: {first}")
            later = [self.take_fake(100, clock)[1] for _ in range(30)]
            self.assertEqual(later, [0.0] * 30, "the stuck waiter costs the run's other jobs nothing")
            for _ in range(5):  # the pool fills (another run takes the free slot) and frees again: still stuck, still free
                self.fill(300, [5])
                with self.as_pid(100):
                    self.assertFalse(run._over_share(self.folder, POOL, range(ORDINARY)), "full: nothing to leave")
                os.close(self.fds.pop())
                self.assertEqual(self.take_fake(100, clock)[1], 0.0, "freed again: the stuck waiter is not waited for again")
        self.assertLess(first, run.FAIR_MAX, "and the stuck rule is what ended it, not the window")

    def test_a_stuck_waiter_that_takes_a_slot_or_dies_is_forgotten(self):
        marker = self.fds[5]  # 200's waiting marker
        over = lambda: run._over_share(self.folder, POOL, range(ORDINARY))  # noqa: E731
        with self.fake_clock() as clock:
            self.take_fake(100, clock)  # found stuck at a count of 0
            self.assertEqual(run._YIELD["stuck"], {200: 0})
            os.close(self.fds.pop(4))  # 100 gives slot 4 back; 200 takes it: its count changes
            self.fill(200, [4])
            with self.as_pid(100):
                self.assertTrue(over(), "it took a slot: not the stuck waiter it was, so it is left the free one")
            self.assertEqual(run._YIELD["stuck"], {})
            clock.now += run.FAIR_STUCK + 1
            with self.as_pid(100):
                self.assertFalse(over(), "and stuck again at its new count")
            self.assertEqual(run._YIELD["stuck"], {200: 1})
            os.close(marker)  # 200 dies
            self.fds.remove(marker)
            with self.as_pid(100):
                over()
            self.assertEqual(run._YIELD["stuck"], {}, "a dead waiter is forgotten")

    def test_a_waiter_that_took_a_slot_starts_its_clock_again(self):
        os.close(self.fds.pop(4))  # setUp's 5 slots are 100's; this test wants 4, so slots 4 and 5 are free
        with self.fake_clock() as clock, self.as_pid(100):
            over = lambda: run._over_share(self.folder, POOL, range(ORDINARY))  # noqa: E731
            self.assertTrue(over())
            clock.now += run.FAIR_STUCK - 1
            self.fill(200, [4])  # it took a slot: it holds 1 now, and slot 5 is still free
            self.assertTrue(over(), "its count changed: the clock began again")
            clock.now += run.FAIR_STUCK - 1
            self.assertTrue(over(), "less than FAIR_STUCK since it last took one")
            clock.now += 2
            self.assertFalse(over(), "FAIR_STUCK with the same count: stuck")


class Priority(Pool):
    def test_a_priority_run_that_refuses_withdraws_its_marker_so_the_waiter_it_defers_to_goes(self):
        # the waiter defers to a prio- marker, and the prio run refuses for the waiter's sake: left alone, each waits for the other
        self.fill(100, [0, 1, 2])
        self.fill(300, [3, 4, 5])  # no slot is free: the prio run (100) marks itself and waits
        waiter = self.wait_for(200)[0]
        box, ev, calls, real = [], threading.Event(), [], run._over_share

        def watch(*a, **k):
            result = real(*a, **k)
            calls.append(result)
            if calls.count(True) >= 3:  # the first refusal has already withdrawn the marker
                ev.set()
            return result

        def body():
            with run.cpu_slot(wait=600, poll=0.01) as s:
                box.append(s)

        def prio_markers():
            return [x for x in os.listdir(self.folder) if x.startswith("prio-")]

        with self.as_pid(100), mock.patch.dict(os.environ, {run.PRIO_ENV: "1"}), mock.patch.object(run, "_over_share", watch), \
                mock.patch.object(run, "FAIR_MAX", 1e9), mock.patch.object(run, "FAIR_STUCK", 1e9):
            t = threading.Thread(target=body)
            t.start()
            for _ in range(6000):  # the prio run waits with a marker while no slot is free (a failure's bound: ends on the marker)
                if prio_markers():
                    break
                time.sleep(0.01)
            self.assertEqual(len(prio_markers()), 1, "the prio run holds its marker while it waits")
            # run 300 gives slot 5 back: 100 holds its half (3), 200 holds none, so 100 refuses it for 200 and lets its marker go
            os.close(self.fds.pop(5))
            self.assertTrue(ev.wait(60), "the prio run refused a slot for the waiter")
            self.assertEqual(prio_markers(), [], "no prio- marker stood while the run refused a slot for a waiting one")
            os.close(waiter)  # the waiter dies: nobody is left to refuse for
            self.fds.remove(waiter)
            t.join(60)
        self.assertEqual(box, [5], "and with nobody waiting it takes the slot")


class Tidy(Pool):
    def stale(self, names, age=3600):
        """Files of `age` seconds ago: a slot file's age is its last take, a temp file's its birth."""
        for n in names:
            path = os.path.join(self.folder, n)
            with open(path, "w"):
                pass
            os.utime(path, (time.time() - age, time.time() - age))

    def names(self):
        return sorted(os.listdir(self.folder))

    def test_slot_files_beyond_the_pool_that_nobody_holds_are_removed(self):
        self.stale([str(i) for i in range(8, 16)] + ["x0", "x1", "7", "3"])
        with mock.patch.object(run, "_TIDIED", set()):
            self.assertEqual(self.take(100, wait=60, small=True), 6)
        left = self.names()
        self.assertEqual([x for x in left if x.isdigit() and int(x) >= POOL], [], "slots 8 to 15 are gone")
        self.assertNotIn("x0", left)
        self.assertNotIn("x1", left)
        self.assertIn("3", left)
        self.assertIn("7", left, "the pool's own files stay")

    def test_a_stale_file_somebody_holds_is_kept(self):
        self.stale(["9", "x1", "12"])
        fd = os.open(os.path.join(self.folder, "9"), os.O_RDWR)
        self.fds.append(fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        with mock.patch.object(run, "_TIDIED", set()):
            run.tidy_slots(self.folder, POOL)
        self.assertEqual([x for x in self.names() if x in ("9", "x1", "12")], ["9"])

    def test_a_file_touched_a_moment_ago_is_left_for_a_run_that_may_be_taking_it(self):
        self.stale(["9"], age=0)
        self.stale(["10"], age=run.TIDY_AGE + 60)
        run.tidy_slots(self.folder, POOL)
        self.assertEqual([x for x in self.names() if x in ("9", "10")], ["9"])

    def test_with_the_reserve_off_the_express_files_belong_to_the_pool_and_stay(self):
        self.stale(["x0", "x1", "10"])
        with mock.patch.dict(os.environ, {run.RESERVE_ENV: "0"}):
            run.tidy_slots(self.folder, POOL)
        self.assertEqual([x for x in self.names() if x in ("x0", "x1", "10")], ["x0", "x1"])

    def test_the_off_switch_leaves_the_files(self):
        self.stale(["9", "x0"])
        with mock.patch.dict(os.environ, {run.FAIR_ENV: "0"}), mock.patch.object(run, "_TIDIED", set()):
            self.take(100, wait=60)
        self.assertEqual([x for x in self.names() if x in ("9", "x0")], ["9", "x0"])

    def test_a_second_pass_is_a_no_op_and_files_that_are_not_slots_are_left(self):
        self.stale(["08", "-1", "notes", "wait-1-abc", ".keep"])
        run.tidy_slots(self.folder, POOL)
        run.tidy_slots(self.folder, POOL)
        self.assertEqual([x for x in self.names() if x in ("08", "-1", "notes", "wait-1-abc", ".keep")],
                         ["-1", ".keep", "08", "notes", "wait-1-abc"])

    def test_a_file_whose_name_is_a_digit_that_is_not_an_ascii_number_cannot_stop_a_run(self):
        # str.isdigit() is true of the superscript two, and int() of it raises: that was a crash in cpu_slot
        self.stale(["\u00b2", "\u0663", "9"])
        with mock.patch.object(run, "_TIDIED", set()):
            self.assertEqual(self.take(100, wait=60, small=True), 6)
        self.assertEqual([x for x in self.names() if x in ("\u00b2", "\u0663", "9")], ["\u00b2", "\u0663"])

    def test_the_temp_file_of_a_marker_whose_process_died_is_removed_when_old_and_unheld(self):
        self.stale([".wait-5-aa", ".prio-5-bb"])  # old: a process died between creating a marker and renaming it
        self.stale([".wait-6-cc"], age=0)  # young: a run may be making it now
        self.stale([".prio-7-dd"])
        fd = os.open(os.path.join(self.folder, ".prio-7-dd"), os.O_RDWR)
        self.fds.append(fd)
        fcntl.flock(fd, fcntl.LOCK_EX)  # old but held
        run.tidy_slots(self.folder, POOL)
        self.assertEqual([x for x in self.names() if x.startswith(".")], [".prio-7-dd", ".wait-6-cc"])


class Scenario(ForemanTestCase):
    """Two real runs of six threads each, the second started beside the first: how long the second waits for a slot."""

    def setUp(self):
        super().setUp()
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("SAMPO_SLOT", "FOREMAN_SLOT")) and k not in (run.SLOT_ENV, "FM_RUN_DEADLINE")}
        self.env.update(XDG_RUNTIME_DIR=self.tmp, SAMPO_SLOTS=str(POOL))
        self.log = os.path.join(self.tmp, "events.log")
        self.script = os.path.join(self.tmp, "loop.py")
        with open(self.script, "w") as f:
            f.write(LOOP.replace("@HERE@", repr(run.HERE)))

    def start(self, role, env=None):
        p = subprocess.Popen([sys.executable, self.script, self.log, str(NAP), role], env=dict(self.env, **(env or {})))
        self.addCleanup(lambda: (p.kill(), p.wait()))
        return p

    def both(self, env=None):
        """(jobs of the first run that ended after it saw the second wait and before the second's first slot, the most slots
        both held at once, the most each held at the same moment). The second run starts once the first holds slots (events
        in the log, not seconds), and the first runs warm-up jobs until it sees the second's waiting marker: neither depends on
        how fast the second process starts."""
        a = self.start("a", env)
        end = time.monotonic() + 120  # a failure's bound: the loop below ends on the events
        while time.monotonic() < end and not (os.path.exists(self.log) and len(events(self.log)) >= THREADS):
            time.sleep(0.02)
        b = self.start("b", env)
        self.assertEqual((a.wait(300), b.wait(300)), (0, 0))
        evs = events(self.log)
        self.assertIn("m", [c for _, _, c in evs], "the first run saw the second waiting")
        done, top, both = shape(evs, a.pid, b.pid)
        print(f"T-4688 scenario {'fair' if not env else 'off'}: the second run's first slot came after {done} of "
              f"{THREADS * JOBS} jobs of the first that ended once it saw the second wait; most at once {top} of {ORDINARY}; "
              f"each held {both} at once", file=sys.stderr)
        self.assertNotIn("n", [c for _, _, c in evs], "no job ran without a slot")
        return done, top, both

    def test_a_second_run_gets_a_slot_within_a_jobs_length_and_both_progress(self):
        done, top, both = self.both()
        self.assertLessEqual(done, THREADS * 2, "within the first round or two of jobs, not after the first run's queue")
        self.assertLessEqual(top, ORDINARY, "the pool is never exceeded")
        self.assertGreaterEqual(both, 2, "both runs held slots at the same time")

    def test_with_the_switch_off_the_second_run_waits_for_the_queue_as_before(self):
        done, top, both = self.both({run.FAIR_ENV: "0"})
        self.assertGreater(done, THREADS * 2, "the first run's jobs take every slot a job frees until its queue is empty")
        self.assertLessEqual(top, ORDINARY)


if __name__ == "__main__":
    unittest.main()
