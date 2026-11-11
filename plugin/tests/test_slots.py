"""T-3252: every run.py on the machine shares one pool of CPU slots, so four runners don't put 64 workers on 16 CPUs."""
import os
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from helpers import ForemanTestCase

import run


class Slots(ForemanTestCase):
    def setUp(self):
        super().setUp()
        env = {"XDG_RUNTIME_DIR": self.tmp, "SAMPO_SLOTS": "1"}
        self.patch = mock.patch.dict(os.environ, env)
        self.patch.start()
        for k in (run.SLOT_ENV, "FOREMAN_SLOT", "FOREMAN_SLOTS", "SAMPO_SLOT_PRIORITY", "FOREMAN_SLOT_PRIORITY"):
            os.environ.pop(k, None)  # this test's own runner set one of them
        self.addCleanup(self.patch.stop)

    def priority_waiter(self, flag):
        """T-3614: a priority run.py's class waiting for a slot in another process; it touches flag once it has one."""
        code = ("import sys, time; sys.path.insert(0, %r); import run\n"
                "with run.cpu_slot(wait=30, poll=0.2):\n"
                "    open(%r, 'w').close(); time.sleep(0.3)\n") % (run.HERE, flag)
        p = subprocess.Popen([sys.executable, "-c", code], env=dict(os.environ, SAMPO_SLOT_PRIORITY="1"))
        self.addCleanup(lambda: (p.kill(), p.wait()))
        return p

    def marks(self, until=5.0):
        """The priority waiters' markers in the pool's folder, once one shows (or until s pass)."""
        folder, end = os.path.join(self.tmp, f"sampo-slots-{os.getuid()}"), time.monotonic() + until
        while time.monotonic() < end:
            got = [x for x in os.listdir(folder) if x.startswith("prio-")]
            if got:
                return got
            time.sleep(0.02)
        return []

    def test_a_waiting_priority_run_gets_the_freed_slot_before_a_lane_run(self):
        # T-3614: the merge train's gate drew from the same pool as ~10 builders' runs and got 1/N of the freed slots
        flag, seen = os.path.join(self.tmp, "priority-had-it"), []

        def lane():  # a low run that retries 40x as often as the priority one: without priority it wins the race
            with run.cpu_slot(wait=30, poll=0.005):
                seen.append(os.path.exists(flag))

        with run.cpu_slot() as s:
            self.assertEqual(s, 0)
            t = threading.Thread(target=lane)
            t.start()
            p = self.priority_waiter(flag)
            self.assertTrue(self.marks(), "a waiting priority run marks the pool")
        t.join(30)
        p.wait(30)
        self.assertEqual(seen, [True], "the lane run took the slot only after the priority run had it")
        self.assertEqual(self.marks(until=0), [], "a priority run drops its marker once it holds a slot")

    def test_a_lane_run_defers_to_a_priority_run_for_half_its_wait_only(self):
        # a lane's run under a deadline would otherwise wait out a long gate and end NOT_STARTED
        mark = run._mark(run._slot_folder())  # a live priority waiter's marker; the slot itself is free
        self.addCleanup(os.close, mark[0])
        start = time.monotonic()
        with run.cpu_slot(wait=1, poll=0.01) as s:
            waited = time.monotonic() - start
            self.assertEqual(s, 0)
        self.assertGreaterEqual(waited, 0.5)
        self.assertLess(waited, 1)

    def test_a_dead_priority_waiters_marker_blocks_nobody(self):
        with run.cpu_slot():
            p = self.priority_waiter(os.path.join(self.tmp, "never"))
            self.assertTrue(self.marks())
            p.kill()
            p.wait()
        start = time.monotonic()
        with run.cpu_slot(wait=5) as s:
            self.assertEqual(s, 0)
        self.assertLess(time.monotonic() - start, 2)
        self.assertEqual(self.marks(until=0), [], "the dead waiter's leftover marker is removed")

    def test_a_slot_mark_renamed_by_importing_sampo_still_counts(self):
        # T-3442: sampo.core's mirror_env turns SAMPO_SLOT into FOREMAN_SLOT in every test process, so a nested
        # run.py queued behind the whole machine's pool: CheckSkip took 323-344 s with timeouts, 4 s once it was seen
        with run.cpu_slot() as outer:
            with mock.patch.dict(os.environ, {"FOREMAN_SLOT": str(outer)}):
                start = time.monotonic()
                with run.cpu_slot(wait=30) as inner:
                    self.assertIsNone(inner)
                self.assertLess(time.monotonic() - start, 1)
        os.environ.pop("SAMPO_SLOTS")
        with mock.patch.dict(os.environ, {"FOREMAN_SLOTS": "3"}):
            self.assertEqual(run.slot_count(), 3)

    def test_a_class_that_ran_without_a_slot_still_marks_its_children(self):
        # T-3442: a class whose wait ran out (or a pool that is off) gave its children no mark: theirs queued again
        seen = []
        real = subprocess.Popen

        def spy(argv, **kw):
            seen.append(kw["env"].get(run.SLOT_ENV))
            return real([sys.executable, "-c", "pass"], **{k: v for k, v in kw.items() if k != "cwd"})

        with mock.patch.dict(os.environ, {"SAMPO_SLOTS": "0"}), mock.patch.object(run.subprocess, "Popen", spy):
            run.run_one("test_nothing.Nothing", [])
        self.assertTrue(seen and seen[0], seen)

    def test_a_second_holder_waits_for_the_first(self):
        held, order = threading.Event(), []

        def first():
            with run.cpu_slot() as s:
                order.append(("first", s))
                held.set()
                time.sleep(0.4)
            order.append(("first done", None))

        t = threading.Thread(target=first)
        t.start()
        held.wait(5)
        start = time.monotonic()
        with run.cpu_slot() as s:
            waited = time.monotonic() - start
            order.append(("second", s))
        t.join()
        self.assertGreaterEqual(waited, 0.25)
        self.assertEqual([o[0] for o in order], ["first", "first done", "second"])
        self.assertEqual(order[0][1], 0)

    def test_a_dead_holder_frees_its_slot(self):
        code = ("import sys, time; sys.path.insert(0, %r); import run\n"
                "with run.cpu_slot():\n    print('held', flush=True); time.sleep(60)\n") % run.HERE
        p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True, env=dict(os.environ))
        self.assertEqual(p.stdout.readline().strip(), "held")
        p.kill()
        p.wait()
        start = time.monotonic()
        with run.cpu_slot(wait=5) as s:
            self.assertEqual(s, 0)
        self.assertLess(time.monotonic() - start, 2)

    def test_a_runner_inside_a_slot_never_waits_on_its_parent(self):
        with run.cpu_slot() as outer:
            with mock.patch.dict(os.environ, {run.SLOT_ENV: str(outer)}):
                start = time.monotonic()
                with run.cpu_slot(wait=30) as inner:
                    self.assertIsNone(inner)
                self.assertLess(time.monotonic() - start, 1)

    def test_off_and_a_wait_that_runs_out_run_without_a_slot(self):
        with mock.patch.dict(os.environ, {"SAMPO_SLOTS": "0"}):
            with run.cpu_slot() as s:
                self.assertIsNone(s)
        with run.cpu_slot():
            start = time.monotonic()
            with run.cpu_slot(wait=0.3) as s:
                self.assertIsNone(s)  # never hangs: it runs anyway once the wait is up
            self.assertLess(time.monotonic() - start, 2)

    def test_an_unusable_slot_folder_runs_without_a_slot(self):
        # review (T-3297): a squatted or unwritable folder must not abort the run
        blocker = os.path.join(self.tmp, f"sampo-slots-{os.getuid()}")
        with open(blocker, "w") as f:
            f.write("not a folder")
        with run.cpu_slot(wait=1) as s:
            self.assertIsNone(s)
        os.remove(blocker)
        os.makedirs(blocker)
        os.chmod(blocker, 0o777)  # a folder others may write is someone else's to plant files in
        with run.cpu_slot(wait=1) as s:
            self.assertIsNone(s)

    def test_the_pool_is_the_cpus_this_cgroup_may_use(self):
        os.environ.pop("SAMPO_SLOTS")
        root = os.path.join(self.tmp, "cg")
        os.makedirs(os.path.join(root, "a", "b"))
        for rel, quota in (("a", "800000 100000"), ("a/b", "max 100000")):
            with open(os.path.join(root, rel, "cpu.max"), "w") as f:
                f.write(quota + "\n")
        with mock.patch.object(run, "_cgroup_dir", lambda: os.path.join(root, "a", "b")), \
                mock.patch.object(run, "CGROUP_ROOT", root), mock.patch("os.cpu_count", lambda: 16):
            self.assertEqual(run.slot_count(), 8)  # a 50% cap on 16 CPUs, set on the parent
        with mock.patch.object(run, "_cgroup_dir", lambda: None), mock.patch("os.cpu_count", lambda: 16):
            self.assertEqual(run.slot_count(), 16)

    def test_each_class_runs_in_a_slot_and_tells_its_children(self):
        seen = []
        real = subprocess.Popen

        def spy(argv, **kw):
            seen.append(kw["env"].get(run.SLOT_ENV))
            return real([sys.executable, "-c", "pass"], **{k: v for k, v in kw.items() if k != "cwd"})

        with mock.patch.object(run.subprocess, "Popen", spy):
            run.run_one("test_nothing.Nothing", [])
        self.assertEqual(seen, ["0"])

    def hold(self, small=False):
        """T-4247: a slot held by a thread until the returned event is set: (its name, the event)."""
        held, free, box = threading.Event(), threading.Event(), []

        def body():
            with run.cpu_slot(wait=5, poll=0.01, small=small) as s:
                box.append(s)
                held.set()
                free.wait(30)

        t = threading.Thread(target=body, daemon=True)
        t.start()
        self.addCleanup(lambda: (free.set(), t.join(30)))
        self.assertTrue(held.wait(5), "the slot was not free")
        return box[0], free

    def test_a_small_run_takes_an_express_slot_while_the_pool_is_held_and_a_priority_run_waits(self):
        # T-4247: a run of one or two classes starts at once beside a gate that holds every pool slot and waits
        self.assertEqual(self.hold()[0], 0)
        self.priority_waiter(os.path.join(self.tmp, "never"))
        self.assertTrue(self.marks(), "the priority run waits for the pool")
        start = time.monotonic()
        with run.cpu_slot(wait=5, poll=0.01, small=True) as s:
            self.assertEqual(s, "x0")
        self.assertLess(time.monotonic() - start, 1)
        self.assertTrue(run.small_selection(["test_a.A", "test_a.B", "test_a.A"]))
        self.assertFalse(run.small_selection(["test_a.A", "test_b.B", "test_c.C"]))

    def test_a_third_small_run_falls_back_to_the_pool_once_express_is_full(self):
        _, free_pool = self.hold()
        self.assertEqual(self.hold(small=True)[0], "x0")
        self.assertEqual(self.hold(small=True)[0], "x1")
        got = []

        def third():
            with run.cpu_slot(wait=10, poll=0.01, small=True) as s:
                got.append(s)

        t = threading.Thread(target=third)
        t.start()
        time.sleep(0.3)
        self.assertEqual(got, [], "both express slots are taken: the third run waits for the pool")
        free_pool.set()
        t.join(10)
        self.assertEqual(got, [0])

    def test_a_large_run_never_takes_an_express_slot(self):
        self.hold()  # the pool is held; both express files are free
        with run.cpu_slot(wait=0.5, poll=0.01) as s:
            self.assertIsNone(s, "a larger run waits out its wait, never an express file")
            free = run._take(run._slot_folder(), run.EXPRESS)
            self.assertIsNotNone(free, "an express file is still free")
            os.close(free[1])

    def test_a_dead_express_holder_frees_its_slot(self):
        code = ("import sys, time; sys.path.insert(0, %r); import run\n"
                "with run.cpu_slot(small=True) as s:\n    print(s, flush=True); time.sleep(60)\n") % run.HERE
        p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True, env=dict(os.environ))
        self.addCleanup(lambda: (p.kill(), p.wait()))
        self.assertEqual(p.stdout.readline().strip(), "x0")
        self.assertEqual(self.hold(small=True)[0], "x1")  # x0 is the child's until it is killed
        p.kill()
        p.wait()
        start = time.monotonic()
        with run.cpu_slot(wait=5, poll=0.01, small=True) as s:
            self.assertEqual(s, "x0", "the killed holder's express file is free again")
        self.assertLess(time.monotonic() - start, 2)

    def test_defer_is_half_the_wait_capped_at_defer_max(self):
        self.assertEqual(run.defer_for(3600.0), 300.0)  # DEFER_MAX
        self.assertEqual(run.defer_for(1.0), 0.5)

    def test_a_non_priority_run_defers_to_a_priority_waiter_for_defer_max_at_most(self):
        mark = run._mark(run._slot_folder())  # a live priority waiter's marker; the pool is free
        self.addCleanup(os.close, mark[0])
        start = time.monotonic()
        with mock.patch.object(run, "DEFER_MAX", 0.3):
            with run.cpu_slot(wait=10, poll=0.01) as s:
                waited = time.monotonic() - start
        self.assertEqual(s, 0)
        self.assertGreaterEqual(waited, 0.3)
        self.assertLess(waited, 3)

    def test_a_run_says_how_long_it_waited_for_a_slot_when_over_5s(self):
        self.assertEqual(run.waited_note({"test_a.A": 4.0}), "")
        self.assertEqual(run.waited_note({"test_a.A": 4.0, "test_b.B": 3.0}), "; waited 7 s for a slot")

    def test_run_one_keeps_the_seconds_its_class_waited_for_a_slot(self):
        _, free = self.hold()
        threading.Timer(0.6, free.set).start()
        seen = []
        with mock.patch.object(run, "_run_one", lambda cid, k, t, d, slot: seen.append(slot) or (cid, 0, "", 0, 0.0)):
            run.WAITED.pop("test_w.W", None)
            run.run_one("test_w.W", [])
        self.assertEqual(seen, [0])
        self.assertGreaterEqual(run.WAITED["test_w.W"], 0.4)
        self.hold()  # the pool is held again: a small class's run_one takes express at once, passing small through
        with mock.patch.object(run, "_run_one", lambda cid, k, t, d, slot: seen.append(slot) or (cid, 0, "", 0, 0.0)):
            before = run.WAITED["test_w.W"]
            run.run_one("test_w.W", [], small=True)
        self.assertEqual(seen, [0, "x0"])
        self.assertLess(run.WAITED["test_w.W"] - before, 0.5)


@unittest.skipUnless(hasattr(os, "sched_getscheduler"), "scheduler policies (SCHED_IDLE) are Linux only")
class Idle(unittest.TestCase):
    def on_thread(self, fn):  # sched_setscheduler(0) changes the calling thread only: the test process stays normal
        out = []
        t = threading.Thread(target=lambda: out.append(fn()))
        t.start()
        t.join()
        return out[0]

    def child(self, name):
        from sampo import night
        code = "import os;print(os.sched_getscheduler(0), os.nice(0))"
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, preexec_fn=night.priority(name))
        return [int(x) for x in r.stdout.split()]

    def test_generator_jobs_run_at_nice_19_and_never_sched_idle(self):
        # T-3296: the moons jobs take the project lock per row; SCHED_IDLE would stretch each hold under load
        for name in ("moons grow", "moons funnel"):
            policy, nice = self.child(name)
            self.assertEqual((policy, nice), (os.SCHED_OTHER, 19), name)

    def test_other_jobs_stay_at_the_normal_policy_and_nice_10_at_most(self):
        for name in ("queue", "compost", "sentinel", "model bench"):
            policy, nice = self.child(name)
            self.assertEqual(policy, os.SCHED_OTHER, name)
            self.assertLessEqual(nice, max(10, os.nice(0)), name)  # a niced parent keeps its own nice


if __name__ == "__main__":
    unittest.main()
