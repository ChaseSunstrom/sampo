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
PASSES = 200  # T-4789: the passes a short run's thread may make before its first slot (about 12 s of polling; measured: 5 to 23)
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
        "[t.join() for t in ts]\n"
        "open(log + '.' + role, 'w').write(repr(sorted(run.left_now())))\n").replace("@JOBS@", str(JOBS)).replace("@THREADS@", str(THREADS))


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
        for name, value in (("_SAID", []), ("_YIELD", {"end": None, "cool": None, "seen": {}, "stuck": {}}),
                            ("_HELD", {"n": 0, "until": 0.0}), ("_DEFER", {"first": None}), ("_LEFT", {})):
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

    def wait_for(self, pid, prefix="wait-", suffix=""):
        """A live waiting marker of run `pid` (suffix "-s": a small run's)."""
        with self.as_pid(pid):
            mark = run._mark(self.folder, prefix, suffix)
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
        self.assertFalse(self.over(100, prio=True), "T-4789: at its half it still takes a slot while more than one is free")
        self.fill(100, [3, 4])  # 5 of 6: one free
        self.assertTrue(self.over(100, prio=True), "and leaves the last free slot for a builder's first")

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


class Gate(Pool):
    """T-4789: fairness is for a waiting run's first slot, not an equal split against a run everything waits behind."""

    def suite(self):
        """This process holds the full-suite lock and another full run waits on it (its suite- marker)."""
        patched = mock.patch.dict(run._SUITE, {"held": True})
        patched.start()
        self.addCleanup(patched.stop)
        self.wait_for(900, "suite-")

    def test_a_waiter_in_a_small_run_is_not_counted_in_the_divisor(self):
        self.fill(100, [0, 1])
        self.fill(300, [2, 3])
        self.wait_for(200)
        self.assertTrue(self.over(100), "an ordinary waiter makes three runs: the share is two")
        for mark in os.listdir(self.folder):
            if mark.startswith("wait-"):
                os.unlink(os.path.join(self.folder, mark))
        self.wait_for(201, suffix="-s")
        self.assertFalse(self.over(100), "a small waiter asks for a reserved slot, not a share: two runs, the share is three")

    def test_a_waiter_in_a_small_run_is_entitled_to_its_first_slot_all_the_same(self):
        self.fill(100, [0, 1, 2])
        self.fill(300, [3, 4])
        self.wait_for(200, suffix="-s")
        self.assertTrue(self.over(100), "the run holding three leaves the free slot to the small waiter")

    def test_a_gate_leaves_a_waiting_builder_its_first_slot_and_takes_the_rest(self):
        self.suite()
        self.fill(100, [0, 1])  # the gate's share beside one waiter would be three (half): it takes freely, leaving one free
        self.wait_for(200)
        self.assertFalse(self.over(100), "two slots free and one waiting run: the gate takes one")
        self.fill(100, [2, 3, 4])
        self.assertTrue(self.over(100), "one slot free: it is the waiting run's first")

    def test_without_a_queued_suite_a_run_holding_half_still_shares_equally(self):
        self.fill(100, [0, 1, 2])
        self.wait_for(200)
        self.assertTrue(self.over(100), "the holder of half beside a waiter leaves it a slot: no gate, the divisor is two")

    def test_a_priority_run_beside_one_short_run_keeps_all_but_that_runs_first_slot(self):
        self.fill(100, [0, 1, 2, 3])
        self.fill(200, [4])
        self.wait_for(100, "prio-")
        self.assertTrue(self.over(200), "the run holding one slot leaves the free one to the priority run below five")

    def test_a_priority_run_is_owed_all_but_one_first_slot_for_each_other_run(self):
        self.fill(100, [0, 1, 2])
        self.fill(200, [3])
        self.fill(300, [4])
        self.wait_for(100, "prio-")
        self.assertTrue(self.over(200), "two other runs: it is owed four of six, and holds three")
        self.assertTrue(self.over(300))

    def test_a_priority_run_at_its_share_leaves_the_short_run_the_slot(self):
        self.fill(100, [0, 1, 2, 3])
        self.wait_for(100, "prio-")
        self.wait_for(200)  # one other run, waiting: the share is five, held four
        self.assertFalse(self.over(100, prio=True), "the priority run takes a free slot beside one waiting run's first")
        self.fill(100, [4])
        self.assertTrue(self.over(100, prio=True), "at five the one free slot is the waiting run's first")

    def test_a_low_run_takes_a_free_slot_there_is_no_waiting_run_for(self):
        self.wait_for(200)
        folder = self.folder
        with self.as_pid(100):
            self.assertFalse(run._builder_waiting(folder, range(ORDINARY), POOL), "six slots free for one waiting run")
        self.fill(300, [0, 1, 2, 3, 4])
        with self.as_pid(100):
            self.assertTrue(run._builder_waiting(folder, range(ORDINARY), POOL), "the last free slot is the waiter's")

    def test_a_low_run_does_not_yield_to_a_run_that_holds_a_slot(self):
        self.fill(300, [0, 1, 2, 3, 4])
        self.wait_for(300)
        with self.as_pid(100):
            self.assertFalse(run._builder_waiting(self.folder, range(ORDINARY), POOL), "300 waits for a second, with a first")

    def test_a_low_run_does_not_yield_while_it_holds_the_suite_with_one_queued_behind_it(self):
        self.fill(300, [0, 1, 2, 3, 4])
        self.wait_for(200)
        with self.as_pid(100):
            self.assertTrue(run._builder_waiting(self.folder, range(ORDINARY), POOL))
        self.suite()
        with self.as_pid(100):
            self.assertFalse(run._builder_waiting(self.folder, range(ORDINARY), POOL))

    def test_a_priority_run_that_holds_none_is_first_and_one_that_holds_a_slot_is_not(self):
        # T-3614 keeps its first slot ahead of every other run's; once it holds one it owes the others theirs
        self.wait_for(200, "prio-")
        self.assertTrue(run._prio_first(self.folder, POOL, range(ORDINARY)))
        self.fill(200, [0])
        self.assertFalse(run._prio_first(self.folder, POOL, range(ORDINARY)), "it holds a slot: the others' first slots go now")
        self.wait_for(300, "wait-")
        self.assertFalse(run._prio_first(self.folder, POOL, range(ORDINARY)), "an ordinary waiter is not a priority run")

    def test_a_run_holding_none_takes_a_slot_beside_a_priority_run_that_holds_one(self):
        self.fill(200, [0])
        self.wait_for(200, "prio-")
        got = self.take(100)
        self.assertIn(got, range(1, ORDINARY), "its first slot is not held for the gate, which already has one")

    def test_the_old_rule_without_the_slots_yields_to_any_marker(self):
        self.fill(300, [0])
        self.wait_for(300)
        with self.as_pid(100):
            self.assertTrue(run._builder_waiting(self.folder))

    def gate_holding_one(self):
        """Run 200 is a priority run that holds slot 0 and waits for more."""
        self.fill(200, [0])
        self.wait_for(200, "prio-")

    def test_a_run_that_never_held_a_slot_takes_one_beside_a_gate_that_holds_one(self):
        self.gate_holding_one()
        with self.fake_clock() as clock:
            slot, waited = self.take_fake(100, clock)
        self.assertEqual((slot in range(1, ORDINARY), waited), (True, 0.0), "its first slot is not the gate's")

    def test_a_builder_between_two_jobs_still_counts_as_holding_a_slot_so_it_leaves_the_freed_one_to_the_gate(self):
        # T-3614: the slot files read a builder whose only slot just ended as holding none for an instant, at every
        # turnover: it then raced the gate for the slot it freed. Its own count holds for HOLD_GRACE s after the last
        self.gate_holding_one()
        with self.fake_clock() as clock:
            run._held_add(1)
            self.assertTrue(run._holding())
            run._held_add(-1)  # the job ended: no slot file of its own now
            self.assertTrue(run._holding(), "still holding, between two jobs")
            slot, waited = self.take_fake(100, clock)
            self.assertGreaterEqual(waited, run.HOLD_GRACE, "it left the freed slot to the gate for the grace")
            self.assertLess(waited, run.HOLD_GRACE + 2.6, "and only for the grace and one sleep step")
            self.assertIn(slot, range(1, ORDINARY))
            clock.now += run.HOLD_GRACE + 1
            self.assertFalse(run._holding(), "a run that let go long ago holds none")

    def test_a_bounded_deferral_is_the_processs_not_each_calls(self):
        # a run holding a slot defers to a waiting priority run for DEFER_MAX at most; every call restarted its own clock,
        # so a marked waiter that held none kept a stream of one run's classes off for 300 s each
        self.gate_holding_one()
        run._held_add(1)
        with self.fake_clock() as clock:
            first = self.take_fake(100, clock)[1]
            self.assertTrue(run.DEFER_MAX <= first <= run.DEFER_MAX + 2.6, f"the first class waits DEFER_MAX: {first}")
            later = [self.take_fake(100, clock)[1] for _ in range(5)]
        self.assertEqual(later, [0.0] * 5, "the next classes of the process do not wait it out again")

    def test_each_priority_episode_gets_its_own_bounded_deferral(self):
        # a verify defers to a small priority run for its DEFER_MAX, then a ship's gate comes: with the start kept for the
        # process the gate got no deferral for the rest of the run
        self.gate_holding_one()
        run._held_add(1)
        with self.fake_clock() as clock:
            first = self.take_fake(100, clock)[1]
            self.assertTrue(run.DEFER_MAX <= first <= run.DEFER_MAX + 2.6, f"episode one spends its budget: {first}")
            self.assertEqual(self.take_fake(100, clock)[1], 0.0, "and the same waiter is not waited for again")
            for name in os.listdir(self.folder):  # the gap: the waiter is gone
                if name.startswith("prio-"):
                    os.unlink(os.path.join(self.folder, name))
            self.assertEqual(self.take_fake(100, clock)[1], 0.0, "no priority waiter: nothing to defer to")
            self.wait_for(201, "prio-")  # a new priority run (holding none: its first slot goes first)
            second = self.take_fake(100, clock)[1]
            self.assertTrue(run.DEFER_MAX <= second <= run.DEFER_MAX + 2.6, f"episode two is deferred to again: {second}")

    def test_with_the_switch_off_each_priority_episode_gets_its_own_bounded_deferral(self):
        self.gate_holding_one()
        with mock.patch.dict(os.environ, {run.FAIR_ENV: "0"}), self.fake_clock() as clock:
            self.assertGreaterEqual(self.take_fake(100, clock)[1], run.DEFER_MAX)
            self.assertEqual(self.take_fake(100, clock)[1], 0.0)
            for name in os.listdir(self.folder):
                if name.startswith("prio-"):
                    os.unlink(os.path.join(self.folder, name))
            self.take_fake(100, clock)
            self.wait_for(201, "prio-")
            self.assertGreaterEqual(self.take_fake(100, clock)[1], run.DEFER_MAX, "the second waiter is deferred to too")

    def test_a_reserve_slot_does_not_make_a_run_a_holder_but_a_pool_slot_does(self):
        # a small run holding only an express slot must not defer its pool passes to a priority waiter as a holder
        with self.as_pid(100):
            with run.cpu_slot(wait=1, poll=0.01, small=True) as s:
                self.assertIn(s, run.lanes(POOL)[0], "a small run's first slot is a reserved one")
                self.assertEqual(run._HELD["n"], 0, "which does not count as holding")
            with run.cpu_slot(wait=1, poll=0.01) as s:
                self.assertIn(s, range(ORDINARY))
                self.assertEqual(run._HELD["n"], 1)
            self.assertEqual(run._HELD["n"], 0)

    def test_a_run_holding_only_an_express_slot_takes_its_first_pool_slot_beside_a_gate_that_holds_one(self):
        self.gate_holding_one()
        with self.fake_clock() as clock, self.as_pid(100), run.cpu_slot(wait=1, poll=0.01, small=True) as express:
            self.assertIn(express, run.lanes(POOL)[0])
            self.assertFalse(run._holding(), "the express slot is not a holding")
            slot, waited = self.take_fake(100, clock)
        self.assertEqual((slot in range(1, ORDINARY), waited), (True, 0.0), "a first pool slot is not left to the gate")

    def test_with_the_switch_off_a_run_defers_to_a_priority_waiter_whatever_it_holds(self):
        self.gate_holding_one()
        with mock.patch.dict(os.environ, {run.FAIR_ENV: "0"}), self.fake_clock() as clock:
            waited = self.take_fake(100, clock)[1]
        self.assertGreaterEqual(waited, run.DEFER_MAX, "before T-4688 a run holding none waited for the gate's marker too")

    def test_with_the_switch_off_a_small_run_makes_a_plain_marker_and_a_low_run_asks_the_old_rule(self):
        for env, small, ordinary in (({run.FAIR_ENV: "0"}, False, False), ({}, True, True)):
            seen = []
            with mock.patch.dict(os.environ, env), mock.patch.object(run, "_wait_leave"), \
                    mock.patch.object(run, "_wait_enter", side_effect=lambda folder, small=False: seen.append(small) or True):
                self.fill(300, range(POOL))
                self.take(100, wait=0.05, small=True)  # nothing free: it waits, and says so with a marker
                self.release()
            self.assertEqual(seen, [small], f"env {env}")
            with mock.patch.dict(os.environ, env), mock.patch.object(run, "_builder_waiting", return_value=False) as asked:
                self.take(100, low=True)
            self.assertEqual(asked.call_args[0][1] is not None, ordinary, f"env {env}: the old rule is called with no slots")

    def test_a_pass_reads_the_slots_and_the_waiters_once_for_all_its_questions(self):
        self.gate_holding_one()
        self.wait_for(300)
        with mock.patch.object(run, "slot_holders", wraps=run.slot_holders) as holders, \
                mock.patch.object(run, "_waiter_pids", wraps=run._waiter_pids) as waiters:
            self.take(100, wait=0.0, low=True)  # one pass: the priority run's first slot, the verify's yield, the share
        self.assertEqual((holders.call_count, waiters.call_count), (1, 1))

    def test_a_pass_whose_free_slot_went_meanwhile_counts_no_time_left(self):
        self.fill(100, range(5))  # 100 holds 5 of 6 and slot 5 is free: it leaves it to the waiting run 200
        self.wait_for(200)
        with mock.patch.object(run, "_free_slot", return_value=False), self.fake_clock() as clock:
            self.take_fake(100, clock, wait=3)
        self.assertEqual(run.left_now(), {}, "the slot was gone when the pass woke: nothing was left")
        with self.fake_clock() as clock:
            self.take_fake(100, clock, wait=3)
        self.assertGreater(run.wall_wait(run.left_now()[run.LEFT_SHARE]), 2.0, "and with it still free the pass counts")


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
    def test_a_priority_run_that_refuses_for_a_waiter_keeps_its_marker_and_the_waiter_holding_none_is_not_deferred(self):
        # the prio run refuses a slot for the waiter's sake: the waiter holds none, so it does not defer to the marker, and goes
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
            # T-4789: the marker stays. A waiter holding no slot no longer defers to it (cpu_slot defers only a run that holds
            # one), so the refusal cannot leave the two waiting for each other, and a run that withdrew the marker was
            # invisible to a gate's share while it waited
            self.assertEqual(len(prio_markers()), 1, "the prio- marker stood while the run refused a slot for a waiting one")
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
        with open(self.log + ".a") as f:  # T-4789: the reasons the first run left free slots to the second (its closing line's)
            self.left = f.read()
        return done, top, both

    def test_a_second_run_gets_a_slot_within_a_jobs_length_and_both_progress(self):
        done, top, both = self.both()
        self.assertLessEqual(done, THREADS * 2, "within the first round or two of jobs, not after the first run's queue")
        self.assertLessEqual(top, ORDINARY, "the pool is never exceeded")
        self.assertGreaterEqual(both, 2, "both runs held slots at the same time")
        # the counter holds only passes after which a slot was still free: the second run took what the first left it, so
        # it may be empty here (the one-pass tests in Gate say what is counted); nothing else may be named
        self.assertNotIn(run.LEFT_PRIO, self.left, "the first run left nothing to a priority run: there was none")

    def test_with_the_switch_off_the_second_run_waits_for_the_queue_as_before(self):
        # T-4789: with the switch off the second run gets a slot only when its poll lands in a gap between the first's jobs: a
        # race, 0 to 21 of 36 jobs in eight foreground runs (it failed at exactly 12 against "more than 12"), so no count of
        # jobs is a margin. What the switch off does is certain: the first run never leaves a slot for a share.
        done, top, both = self.both({run.FAIR_ENV: "0"})
        self.assertEqual(self.left, "[]", "the first run left no slot to a fair share")
        self.assertLessEqual(top, ORDINARY)


# T-4789: one long run (six threads, a queue of jobs) beside streams of short runs (a process of three jobs, then the next
# process at once, as builders' small modules come). Every job sleeps: the measure is how long the long run held no slot
# while it had jobs queued, and how many short runs got their slots meanwhile.
# What each process records of its decisions, to the log's own ".d" file: for every refusal question (O: over its share, B: the
# verify's yield) the slots the process held, the ordinary slots free and the waiters holding none that the pass SAW (the very
# snapshot the decision read, not another read of the folder: no race with it), and the answer; and the number of passes of each
# short run's thread before its first slot. The scenarios assert on these decisions and print the wall figures.
SPY = ("def spy(path, who):\n"
       "    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT)\n"
       "    real_over, real_low, real_take, tl = run._over_share, run._builder_waiting, run._take, threading.local()\n"
       "    def seen(tag, snap, ordinary, result):\n"
       "        if 'holders' not in snap or 'waiters' not in snap:\n"
       "            return\n"
       "        me, held = os.getpid(), snap['holders']\n"
       "        owners = {p for s, p in held.items() if s in ordinary}\n"
       "        needy = [p for p, k in snap['waiters'].items() if p != me and k != 'prio' and p not in owners]\n"
       "        mine = sum(1 for s, p in held.items() if s in ordinary and p == me)\n"
       "        free = sum(1 for s in ordinary if s not in held)\n"
       "        os.write(fd, f'{tag} {who} {mine} {free} {len(needy)} {int(result)}\\n'.encode())\n"
       "    def over(folder, n, ordinary, prio=False, snap=None):\n"
       "        result = real_over(folder, n, ordinary, prio, snap)\n"
       "        seen('O', snap, ordinary, result)\n"
       "        return result\n"
       "    def low(folder, ordinary=None, n=0, snap=None):\n"
       "        result = real_low(folder, ordinary, n, snap)\n"
       "        seen('B', snap, ordinary or (), result)\n"
       "        return result\n"
       "    def take(folder, slots):\n"
       "        got = real_take(folder, slots)\n"
       "        tl.n = getattr(tl, 'n', 0) + 1\n"
       "        if got and not getattr(tl, 'done', False):\n"
       "            tl.done = True\n"
       "            os.write(fd, f'T {who} {tl.n}\\n'.encode())\n"
       "        return got\n"
       "    run._over_share, run._builder_waiting, run._take = over, low, take\n")
LONG = ("import os, sys, threading, time; sys.path.insert(0, @HERE@); import run\n" + SPY +
        "log, nap, jobs = sys.argv[1], float(sys.argv[2]), int(sys.argv[3])\n"
        "spy(log + '.d', 'L')\n"
        "fd = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT)\n"
        "def lane():\n"
        "    for _ in range(jobs):\n"
        "        with run.cpu_slot(wait=300, poll=0.02, low=run.is_low()):\n"
        "            os.write(fd, f'{time.time()} + L\\n'.encode()); time.sleep(nap); os.write(fd, f'{time.time()} - L\\n'.encode())\n"
        "ts = [threading.Thread(target=lane) for _ in range(@THREADS@)]\n"
        "[t.start() for t in ts]\n"
        "[t.join() for t in ts]\n").replace("@THREADS@", str(THREADS))
SHORT = ("import os, sys, threading, time; sys.path.insert(0, @HERE@); import run\n" + SPY +
         "log, nap = sys.argv[1], float(sys.argv[2])\n"
         "spy(log + '.d', 'S')\n"
         "fd = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT)\n"
         "def lane():\n"
         "    with run.cpu_slot(wait=300, poll=0.02):\n"
         "        os.write(fd, f'{time.time()} + S\\n'.encode()); time.sleep(nap); os.write(fd, f'{time.time()} - S\\n'.encode())\n"
         "ts = [threading.Thread(target=lane) for _ in range(3)]\n"
         "[t.start() for t in ts]\n"
         "[t.join() for t in ts]\n")


def idle(path):
    """(seconds the long run held no job between its first start and its last end, the longest such stretch, the most jobs at
    once, its mean jobs at once, and the seconds of those stretches in which a slot of the pool was free: the long run's
    own to take). The log has the long run's jobs (L) and the short runs' (S)."""
    with open(path) as f:
        evs = sorted((float(t), c, who) for t, c, who in (ln.split() for ln in f.read().splitlines() if ln.strip()))
    first = next(t for t, _, who in evs if who == "L")
    end = max(t for t, _, who in evs if who == "L")
    now, last, zero, longest, top, area, lost = {"L": 0, "S": 0}, first, 0.0, 0.0, 0, 0.0, 0.0
    for t, c, who in evs:
        t = min(max(t, first), end)
        if t > last:
            if now["L"] == 0:
                zero += t - last
                longest = max(longest, t - last)
                lost += (t - last) * (now["L"] + now["S"] < ORDINARY)
            area += now["L"] * (t - last)
        last = t
        now[who] += 1 if c == "+" else -1
        top = max(top, now["L"])
    return zero, longest, top, area / (end - first), lost


class Stream(ForemanTestCase):
    """T-4789: a long run beside a stream of short runs never sits at zero jobs while a slot is free or its own: the post-merge
    full suite took 45 minutes of 15 to 22, at 2% CPU with 0 to 2 jobs alive; the scenario with a stream of ordinary runs
    reproduced it, 387 of 429 s at zero jobs (the low run yielded to every waiting marker)."""
    JOBS_LONG, NAP_LONG, NAP_SHORT, STREAMS = 8, 0.3, 0.15, 3

    def setUp(self):
        super().setUp()
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("SAMPO_SLOT", "FOREMAN_SLOT")) and k not in (run.SLOT_ENV, "FM_RUN_DEADLINE")}
        self.env.update(XDG_RUNTIME_DIR=self.tmp, SAMPO_SLOTS=str(POOL))
        self.log = os.path.join(self.tmp, "long.log")
        self.long = os.path.join(self.tmp, "long.py")
        self.short = os.path.join(self.tmp, "short.py")
        for path, text in ((self.long, LONG), (self.short, SHORT)):
            with open(path, "w") as f:
                f.write(text.replace("@HERE@", repr(run.HERE)))

    def measure(self, env=None, streams=None):
        """(idle(log), short runs done) with the long run's env `env`; the shorts run the ordinary kind."""
        env = dict(self.env, **(env or {}))
        stop, done = threading.Event(), [0]
        plain = {k: v for k, v in self.env.items()}

        def stream():
            while not stop.is_set():
                p = subprocess.Popen([sys.executable, self.short, self.log, str(self.NAP_SHORT)],
                                     env=dict(plain, **({run.FAIR_ENV: "0"} if env.get(run.FAIR_ENV) == "0" else {})))
                self.addCleanup(lambda p=p: (p.kill(), p.wait()))
                p.wait()
                done[0] += 1
        long = subprocess.Popen([sys.executable, self.long, self.log, str(self.NAP_LONG), str(self.JOBS_LONG)], env=env)
        self.addCleanup(lambda: (long.kill(), long.wait()))
        ts = [threading.Thread(target=stream) for _ in range(streams or self.STREAMS)]
        for t in ts:
            t.start()
        try:
            self.assertEqual(long.wait(300), 0)
        finally:
            stop.set()
            for t in ts:
                t.join(120)
        return idle(self.log), done[0]

    def decisions(self):
        """(the long run's decisions [(tag, held, free, needy, refused)], the passes each short thread made before its first
        slot) of the scenario just measured, from the ".d" file the spies wrote."""
        path = self.log + ".d"
        lines = []
        if os.path.exists(path):
            with open(path) as f:
                lines = [ln.split() for ln in f.read().splitlines()]
        long = [(t, int(m), int(f), int(n), bool(int(r))) for t, who, m, f, n, r in (x for x in lines if x[0] != "T" and x[1] == "L")]
        passes = [int(x[2]) for x in lines if x[0] == "T" and x[1] == "S"]
        return long, passes

    def seen(self, label, env=None, streams=None):
        """(the long run's decisions, the short threads' passes to a first slot) of a measure; the wall figures (the long run's
        zero-job time, its mean jobs at once, the short runs done) are printed for the report's numbers, not asserted: a loaded
        machine moves them, and the decisions are what the criteria are about."""
        (zero, longest, top, mean, lost), shorts = self.measure(env, streams)
        long, passes = self.decisions()
        print(f"T-4789 stream {label}: the long run held no job {zero:.1f} s (longest {longest:.1f} s, {lost:.1f} s with a slot "
              f"free), most {top}, mean {mean:.2f} of {THREADS}; {shorts} short runs done; {len(long)} decisions, "
              f"{sum(1 for d in long if d[4])} refusals; first slot after at most {max(passes, default=0)} passes", file=sys.stderr)
        return long, passes

    def check(self, long, passes):
        """What every scenario owes: the long run never refuses a free slot while it holds none unless every free one is the
        first of a waiting run (the post-merge verify's yield: never to a run that holds one already), and a short run's first
        slot comes within a few passes of its poll (a job is NAP_LONG s, a pass about 0.06 s)."""
        for tag, held, free, needy, refused in long:
            if held == 0 and refused:
                self.assertEqual(tag, "B", "only the verify's yield refuses a run holding no slot")
                self.assertLessEqual(free, needy, f"a free slot beyond the waiters' first ones was left: {tag, held, free, needy}")
            if tag == "B" and refused:
                self.assertLessEqual(free, needy, "the verify yields only when the free slots are the waiting runs' first")
        self.assertGreaterEqual(len(passes), 3, "short runs ran beside it")
        self.assertLessEqual(max(passes), PASSES, "a short run's first slot came within a few jobs' lengths of passes")

    def test_an_ordinary_long_run_keeps_jobs_going_beside_streams_of_short_runs(self):
        long, passes = self.seen("fair")
        self.check(long, passes)
        self.assertTrue(any(d[0] == "O" for d in long), "the share was asked")

    def test_the_post_merge_verify_does_not_sit_at_zero_jobs_beside_a_stream_of_ordinary_runs(self):
        self.NAP_SHORT = 0.4  # five streams of runs that each last a job or two: a waiting marker is always there
        long, passes = self.seen("low", {"SAMPO_SLOT_LOW": "1"}, streams=5)
        self.check(long, passes)  # the old rule yielded to every waiting marker with free slots under it: 387 of 429 s at zero
        self.assertTrue(any(d[0] == "B" for d in long), "the yield was asked")

    def test_a_priority_run_keeps_more_than_half_the_pool_beside_two_streams_of_short_runs(self):
        self.JOBS_LONG = 25
        long, passes = self.seen("priority", {run.PRIO_ENV: "1"}, streams=2)
        self.check(long, passes)
        refusals = [d for d in long if d[4]]
        self.assertTrue(refusals and all(d[2] <= 1 for d in refusals), "it leaves a waiting run the last free slot and no other")
        self.assertTrue(any(d[1] > ORDINARY // 2 for d in long), "and it held more than half the pool (the old plateau) beside them")

    def test_with_the_switch_off_it_asks_nothing_and_keeps_every_slot_as_before(self):
        long, passes = self.seen("off", {run.FAIR_ENV: "0"})
        self.assertEqual(long, [], "no share and no yield is asked with the switch off")


if __name__ == "__main__":
    unittest.main()
