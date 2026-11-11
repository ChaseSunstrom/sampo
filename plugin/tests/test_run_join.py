"""T-4267 (part A of T-4266): two runs never run the same test class on the same inputs at once. A run about to start a
class publishes a marker for (class, inputs digest) in the shared run-cache folder; another run that reaches the same
class on the same inputs does its other classes first, then waits for the owner and takes its green result ("joined N").
A red or dead owner, an owner too old, differing inputs or SAMPO_NO_JOIN=1 mean the class runs as before."""
import contextlib
import io
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import run  # noqa: E402
from test_run_cache import git, outer, scratch  # noqa: E402

SLEEPY = '''import os, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))


class Sleepy(unittest.TestCase):
    def test_x(self):
        with open(os.path.join(HERE, "data.txt")) as f:
            f.read()
        with open(os.environ["JOIN_COUNTER"], "a") as f:
            f.write("ran\\n")
        stop = time.time() + 180
        while os.path.exists(os.environ["JOIN_HOLD"]) and time.time() < stop:  # the test releases it
            time.sleep(0.05)
'''


def wait_for(cond, secs=180):
    end = time.time() + secs
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.1)
    return False


class Fixture(unittest.TestCase):
    """The real run.py in two worktrees of one scratch repo, one shared cache, one counter file the class appends to."""

    def setUp(self):
        self.top = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.top, True)
        self.a = os.path.join(self.top, "a")
        tests = os.path.join(self.a, "plugin", "tests")
        os.makedirs(tests)
        for name in ("run.py", "runcover.py"):
            shutil.copy(os.path.join(run.HERE, name), os.path.join(tests, name))
        scratch(self.a, {"plugin/tests/test_sleepy.py": SLEEPY, "plugin/tests/data.txt": "x\n"})
        self.b = os.path.join(self.top, "b")
        git(self.a, "worktree", "add", "-q", "--detach", self.b)
        self.counter = os.path.join(self.top, "counter")
        self.hold = os.path.join(self.top, "hold")
        self.env = outer(XDG_CACHE_HOME=os.path.join(self.top, "cache"), FOREMAN_SLOTS="0", JOIN_COUNTER=self.counter,
                         JOIN_HOLD=self.hold, FM_TEST_TIMES=os.path.join(self.top, "t.json"), SAMPO_NO_JOIN="")
        self.procs = []
        self.addCleanup(self.reap)
        self.lines()
        self.go("a")  # a first run of the class: the cache then knows what it reads
        self.assertEqual(self.lines(), 1)

    def reap(self):
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.hold)
        for p in self.procs:
            if p.poll() is None:
                p.kill()
            p.wait()

    def lines(self):
        try:
            with open(self.counter) as f:
                return len(f.read().splitlines())
        except FileNotFoundError:
            return 0

    def start(self, where, **env):
        """run.py in a worktree, not waited for: (the process, its stderr file)."""
        d = getattr(self, where)
        err = open(os.path.join(self.top, f"{where}.{len(self.procs)}.err"), "w+")
        self.addCleanup(err.close)
        p = subprocess.Popen([sys.executable, os.path.join(d, "plugin", "tests", "run.py"), "-p", "test_sleepy.py"],
                             stdout=subprocess.DEVNULL, stderr=err, text=True, env=dict(self.env, **env), cwd=d)
        self.procs.append(p)
        return p, err

    def text(self, err):
        err.flush()
        with open(err.name) as f:
            return f.read()

    def go(self, where, **env):
        p, err = self.start(where, **env)
        self.assertEqual(p.wait(timeout=300), 0, self.text(err))
        return self.text(err)

    def hold_on(self):
        open(self.hold, "w").close()

    def release(self):
        os.unlink(self.hold)

    def second_waits(self, err):
        return wait_for(lambda: "joining" in self.text(err))


class Join(Fixture):
    def two(self, where2, **env2):
        """The first run in a, held mid-class; the second started in where2 after it."""
        self.hold_on()
        p1, e1 = self.start("a")
        self.assertTrue(wait_for(lambda: self.lines() == 2), "the first run's class started")
        p2, e2 = self.start(where2, **env2)
        return p1, e1, p2, e2

    def finish(self, *runs):
        self.release()
        for p, err in runs:
            self.assertEqual(p.wait(timeout=300), 0, self.text(err))

    def test_a_class_two_worktrees_run_on_identical_inputs_runs_once_and_the_second_says_joined_1(self):
        p1, e1, p2, e2 = self.two("b")
        self.assertTrue(self.second_waits(e2), self.text(e2))
        self.finish((p1, e1), (p2, e2))
        self.assertEqual(self.lines(), 2, "the warm-up and one run, not two")
        self.assertRegex(self.text(e2), r"joined 1\b")
        self.assertNotRegex(self.text(e1), r"joined \d")
        self.assertIn("OK", self.text(e2))

    def test_a_class_two_runs_in_one_worktree_run_on_identical_inputs_runs_once(self):
        p1, e1, p2, e2 = self.two("a")
        self.assertTrue(self.second_waits(e2), self.text(e2))
        self.finish((p1, e1), (p2, e2))
        self.assertEqual(self.lines(), 2)
        self.assertRegex(self.text(e2), r"joined 1\b")

    def test_a_class_whose_inputs_differ_runs_in_both(self):
        with open(os.path.join(self.b, "plugin", "tests", "data.txt"), "a") as f:
            f.write("edited\n")  # a file the class reads
        p1, e1, p2, e2 = self.two("b")
        self.assertTrue(wait_for(lambda: self.lines() == 3), "the second run started its own: " + self.text(e2))
        self.finish((p1, e1), (p2, e2))
        self.assertNotRegex(self.text(e2), r"joined \d")

    def test_a_waiter_runs_the_class_itself_when_the_owner_is_killed_mid_class(self):
        p1, e1, p2, e2 = self.two("b")
        self.assertTrue(self.second_waits(e2))
        p1.send_signal(signal.SIGKILL)
        p1.wait()
        self.assertTrue(wait_for(lambda: self.lines() == 3), "the waiter ran the class: " + self.text(e2))
        self.release()
        self.assertEqual(p2.wait(timeout=300), 0, self.text(e2))
        self.assertNotRegex(self.text(e2), r"joined \d")

    def test_a_run_with_sampo_no_join_set_runs_the_class_though_another_holds_it(self):
        p1, e1, p2, e2 = self.two("b", SAMPO_NO_JOIN="1")
        self.assertTrue(wait_for(lambda: self.lines() == 3), self.text(e2))
        self.finish((p1, e1), (p2, e2))
        self.assertNotRegex(self.text(e2), r"joined|joining")

    def test_sampo_no_join_0_is_off_like_the_cache_s_switch(self):
        p1, e1, p2, e2 = self.two("b", SAMPO_NO_JOIN="0")
        self.assertTrue(self.second_waits(e2), self.text(e2))
        self.finish((p1, e1), (p2, e2))
        self.assertRegex(self.text(e2), r"joined 1\b")


class Marker(unittest.TestCase):
    """Joins itself: what makes a marker count, and the class states."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        self.cache = os.path.join(self.d, "run-cache", "abc.json")
        patch = mock.patch.object(run, "POLL", 0.01)
        patch.start()
        self.addCleanup(patch.stop)

    def joins(self, classes=("m.A",), times=None, value=lambda p: "", **kw):
        return run.Joins(self.cache, {c: "d1" for c in classes}, value, lambda j: j, list(classes), times or {}, **kw)

    def marker(self, j, c="m.A"):
        return j.path(c)

    def test_the_second_run_to_reach_a_class_defers_it_and_the_first_runs_it(self):
        one, two = self.joins(), self.joins()
        self.assertTrue(one.start("m.A"))
        self.assertFalse(two.start("m.A"))
        self.assertEqual(two.deferred(), {"m.A"})
        self.assertTrue(one.start("m.A"), "the owner's second job of the class")
        one.close()
        two.close()

    def test_a_class_with_no_inputs_digest_runs_unmarked(self):
        one = run.Joins(self.cache, {}, lambda p: "", lambda j: j, ["m.A"], {})
        self.assertTrue(one.start("m.A"))
        self.assertTrue(self.joins().start("m.A"), "no marker was made")

    def test_a_marker_whose_pid_was_recycled_does_not_count(self):
        one, two = self.joins(), self.joins()
        one.start("m.A")
        marker = self.marker(one)
        run._write_json(marker, {"pid": os.getpid(), "ticks": "1", "at": time.time(), "state": "running"})
        if os.path.exists("/proc/self/stat"):
            self.assertTrue(two.start("m.A"), "same pid, another start time: another process")

    def test_a_marker_whose_owner_is_gone_does_not_count(self):
        one, two = self.joins(), self.joins()
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        one.start("m.A")
        run._write_json(self.marker(one), {"pid": p.pid, "ticks": "", "at": time.time(), "state": "running"})
        self.assertTrue(two.start("m.A"))

    def test_the_waiter_runs_the_class_when_the_marker_is_older_than_three_times_its_time(self):
        one, two = self.joins(times={"m.A": 100}), self.joins(times={"m.A": 100})
        one.start("m.A")
        self.assertFalse(two.start("m.A"))
        run._write_json(one.path("m.A"), {"pid": os.getpid(), "ticks": run.proc_ticks(os.getpid()), "at": time.time() - 301,
                                          "state": "running"})
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(two.wait(time.time() + 60), {})
        self.assertTrue(two.start("m.A"), "it runs it itself now")

    def test_the_least_wait_is_two_minutes(self):
        self.assertEqual(self.joins(times={"m.A": 5}).limit("m.A"), 120)
        self.assertEqual(self.joins(times={"m.A": 100}).limit("m.A"), 300)

    def test_a_red_owner_means_the_waiter_runs_it(self):
        one, two = self.joins(), self.joins()
        one.start("m.A")
        two.start("m.A")
        one.done(("m.A", 1, "FAIL", 1, 0.5))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(two.wait(time.time() + 60), {})
        self.assertTrue(two.start("m.A"))

    def test_a_green_owner_is_joined_with_its_test_count_and_a_skip_is_not_green(self):
        one, two = self.joins(), self.joins()
        one.start("m.A")
        two.start("m.A")
        one.done(("m.A", 0, "OK", 3, 0.5))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(two.wait(time.time() + 60), {"m.A": 3})
        one.close()
        two.close()
        three, four = self.joins(), self.joins()
        three.start("m.A")
        four.start("m.A")
        three.done(("m.A", 0, "OK (skipped=1)", 3, 0.5))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(four.wait(time.time() + 60), {}, "a skip may pass here and not there")

    def test_a_split_class_is_green_once_every_job_is(self):
        one = run.Joins(self.cache, {"m.A": "d1"}, lambda p: "", lambda j: "m.A", ["m.A.t1", "m.A.t2"], {})
        two = run.Joins(self.cache, {"m.A": "d1"}, lambda p: "", lambda j: "m.A", ["m.A.t1", "m.A.t2"], {})
        one.start("m.A.t1")
        two.start("m.A.t2")
        one.done(("m.A.t1", 0, "OK", 1, 0.1))
        self.assertEqual(run.read_marker(one.path("m.A"))["state"], "running")
        one.done(("m.A.t2", 0, "OK", 1, 0.1))
        self.assertEqual(run.read_marker(one.path("m.A"))["state"], "green")

    def test_a_red_owner_that_exited_is_never_joined_from_an_older_green_record(self):
        # review: the owner's marker went at its exit and the waiter then took any cache record green at this tree, so
        # a class that had just failed on these inputs read "joined"
        one, two = self.joins(), self.joins()
        one.start("m.A")
        two.start("m.A")
        one.done(("m.A", 1, "FAIL", 4, 0.5))
        one.close()
        run.save_cache(self.cache, {"m.A": ["any", 4, []]}, 0)
        with mock.patch.object(run, "digest", return_value="any"), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(two.wait(time.time() + 60), {})
        self.assertTrue(two.start("m.A"), "it runs it itself")

    def test_an_owner_gone_mid_class_leaves_no_marker_and_the_waiter_runs_it_whatever_the_cache_says(self):
        one, two = self.joins(), self.joins()
        one.start("m.A")
        two.start("m.A")
        one.close()  # the owner exited before the class finished
        self.assertFalse(os.path.exists(one.path("m.A")))
        run.save_cache(self.cache, {"m.A": ["any", 4, []]}, 0)
        with mock.patch.object(run, "digest", return_value="any"), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(two.wait(time.time() + 60), {})
        self.assertTrue(two.start("m.A"))

    def test_a_green_marker_outlives_its_owner_and_is_joined_by_a_run_that_was_waiting(self):
        one, two = self.joins(), self.joins()
        one.start("m.A")
        two.start("m.A")
        one.done(("m.A", 0, "OK", 3, 0.5))
        one.close()
        self.assertEqual(run.read_marker(one.path("m.A"))["state"], "green")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(two.wait(time.time() + 60), {"m.A": 3})

    def test_a_green_from_before_this_run_began_is_not_taken(self):
        # a plain full run takes no earlier result (the cache's rule): only one made while it ran
        one = self.joins()
        one.start("m.A")
        one.done(("m.A", 0, "OK", 3, 0.5))
        one.close()
        late = self.joins(t0=time.time() + 5)
        self.assertTrue(late.start("m.A"), "it runs the class and marks it its own")
        self.assertEqual(run.read_marker(late.path("m.A"))["state"], "running")
        late.close()

    def test_a_file_the_class_read_that_changed_while_the_owner_ran_makes_its_green_nobody_elses(self):
        # review: green was published before the owner looked at its tree again
        one = self.joins(value=lambda p: "v1", read=lambda jobs: ["lib/x.py"], fresh=lambda: (lambda p: "v2"))
        two = self.joins(value=lambda p: "v1")
        one.start("m.A")
        two.start("m.A")
        one.done(("m.A", 0, "OK", 3, 0.5))
        one.flush()
        self.assertEqual(run.read_marker(one.path("m.A"))["state"], "red")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(two.wait(time.time() + 60), {})
        self.assertTrue(two.start("m.A"))

    def test_a_tree_that_can_not_be_read_again_or_a_class_with_no_record_of_what_it_read_is_not_green(self):
        for kw in ({"fresh": lambda: None}, {"read": lambda jobs: None}):
            with self.subTest(kw=sorted(kw)):
                shutil.rmtree(os.path.dirname(self.cache), True)
                one = self.joins(**kw)
                one.start("m.A")
                one.done(("m.A", 0, "OK", 3, 0.5))
                one.flush()
                self.assertEqual(run.read_marker(one.path("m.A"))["state"], "red")

    def test_a_green_waits_for_the_tree_read_and_one_read_vouches_for_every_class_finished_by_then(self):
        reads = []
        one = self.joins(("m.A", "m.B"), fresh=lambda: reads.append(1) or (lambda p: ""))
        one.start("m.A")
        one.start("m.B")
        with mock.patch.object(run, "FLUSH_S", 30):
            one.done(("m.A", 0, "OK", 1, 0.5))
            one.done(("m.B", 0, "OK", 2, 0.5))
            self.assertEqual(run.read_marker(one.path("m.A"))["state"], "running", "not said before the tree is read")
            one.flush()
        self.assertEqual((len(reads), run.read_marker(one.path("m.A"))["state"], run.read_marker(one.path("m.B"))["n"]),
                         (1, "green", 2))
        with mock.patch.object(run, "FLUSH_S", 0.01):
            two = self.joins(("m.C",), fresh=lambda: (lambda p: ""))
            two.start("m.C")
            two.done(("m.C", 0, "OK", 1, 0.5))
            self.assertTrue(wait_for(lambda: run.read_marker(two.path("m.C"))["state"] == "green", 20), "said unasked")

    def test_what_the_owners_class_read_must_read_the_same_in_the_waiters_tree(self):
        # review: the owner's class may read more than its last record did; the cache would refuse on those paths
        own = {"lib/x.py": "v1", "lib/new.py": "a"}
        for theirs, want in (({"lib/x.py": "v1", "lib/new.py": "a"}, {"m.A": 3}), ({"lib/x.py": "v1", "lib/new.py": "b"}, {})):
            with self.subTest(theirs=theirs):
                shutil.rmtree(os.path.dirname(self.cache), True)
                one = self.joins(value=own.get, paths={"m.A": ["lib/x.py"]}, read=lambda jobs: ["lib/new.py", "lib/x.py"])
                two = self.joins(value=theirs.get, paths={"m.A": ["lib/x.py"]})
                one.start("m.A")
                two.start("m.A")
                one.done(("m.A", 0, "OK", 3, 0.5))
                self.assertEqual(run.read_marker(one.path("m.A"))["paths"], ["lib/new.py", "lib/x.py"])
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(two.wait(time.time() + 60), want)

    def test_a_marker_names_the_paths_only_when_the_class_read_other_than_its_record(self):
        one = self.joins(paths={"m.A": ["lib/x.py"]}, read=lambda jobs: ["lib/x.py"])
        one.start("m.A")
        one.done(("m.A", 0, "OK", 3, 0.5))
        self.assertNotIn("paths", run.read_marker(one.path("m.A")))

    def test_a_marker_that_is_not_what_a_run_writes_is_no_result(self):
        for bad in ({"n": "3"}, {"n": True}, {"paths": [1]}, {"paths": "x"}, {"sum": None}, {"done": "now"}):
            with self.subTest(bad=bad):
                shutil.rmtree(os.path.dirname(self.cache), True)
                one, two = self.joins(), self.joins()
                one.start("m.A")
                two.start("m.A")
                one.done(("m.A", 0, "OK", 3, 0.5))
                run._write_json(one.path("m.A"), {**run.read_marker(one.path("m.A")), **bad})
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(two.wait(time.time() + 60), {})

    def test_a_joined_class_brings_its_owners_cpu_and_one_taken_back_runs_here(self):
        # review: a joined class had no CPU sample, so a gate with a baseline never compared it
        one, two = self.joins(), self.joins()
        one.start("m.A")
        two.start("m.A")
        with mock.patch.dict(run.CPU, {"m.A": 7.5}):
            one.done(("m.A", 0, "OK", 3, 0.5))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(two.wait(time.time() + 60), {"m.A": 3})
        self.assertEqual(two.cpu, {"m.A": [7.5, 1]})
        self.assertFalse(two.start("m.A"))
        two.take({"m.A"})
        self.assertTrue(two.start("m.A"), "over its margin: measured here")

    def test_a_claims_temp_file_is_the_process_own(self):
        # review: two processes wrote one temp name, so one could publish the other's pid as its own marker
        seen = []
        real = run._write_json
        with mock.patch.object(run, "_write_json", lambda path, d: (seen.append(path), real(path, d))[1]):
            one = self.joins()
            one.start("m.A")
        self.assertEqual(seen, [f"{one.path('m.A')}.{os.getpid()}.new"])
        self.assertEqual(os.listdir(one.dir), [os.path.basename(one.path("m.A"))], "and it is gone")

    def test_old_finished_and_dead_markers_and_temp_files_are_swept_and_a_live_run_s_stays(self):
        one = self.joins(("m.A", "m.B", "m.C"))
        for c in ("m.A", "m.B", "m.C"):
            one.start(c)
        one.done(("m.A", 0, "OK", 1, 0.5))  # finished
        run._write_json(one.path("m.B"), {"pid": 0, "ticks": "", "at": 0, "state": "running"})  # dead
        temp = one.path("m.A") + ".123.new"
        open(temp, "w").close()
        self.joins(("m.D",))
        self.assertEqual(len(os.listdir(one.dir)), 4, "younger than the sweep's age: all stay")
        old = time.time() - run.SWEEP_S - 5
        for name in os.listdir(one.dir):
            os.utime(os.path.join(one.dir, name), (old, old))
        self.joins(("m.D",))
        self.assertEqual(os.listdir(one.dir), [os.path.basename(one.path("m.C"))], "the one a live run is running")

    def test_a_deadline_ends_the_wait_and_close_removes_only_its_own_markers(self):
        one, two = self.joins(), self.joins()
        one.start("m.A")
        two.start("m.A")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(two.wait(time.time() + 0.2), {})
        two.close()
        self.assertTrue(os.path.exists(one.path("m.A")), "the owner's marker stays")
        one.close()
        self.assertFalse(os.path.exists(one.path("m.A")), "a class it never finished: nobody waits for it")


if __name__ == "__main__":
    unittest.main()
