"""T-4410: the check sampo lane accept starts is run by the trunk's sampo on the lane's tree (the lane's code and tests
are what it runs; the checker is not the lane's older copy, which may not know an option or may carry a check bug the
trunk has fixed), and a check that ends on its own without a result says so with its exit code and last output line
instead of 'cut off at its deadline'. T-4416: the checks of several accepted lanes take turns (one runs at a time in
a project), so none is cut off by the others and the first result comes after one run."""
# map-links: plugin/lib/sampo/train.py
import fcntl
import json
import os
import signal
import subprocess
import sys
import threading
import time
from unittest import mock

from test_accept_check import AcceptCase, stub

from sampo import train

SHOW = "import os; print('child=' + os.environ.get('SAMPO_LANE_CHILD', 'unset'))"
USAGE = "import sys; print('usage: sampo check'); print('sampo: error: unrecognized arguments: --hold'); sys.exit(2)"


class Trunk(AcceptCase):
    def test_the_check_runs_the_trunk_sampo_with_no_hand_over_to_the_lane_plugin(self):  # trunk
        env = {k: v for k, v in os.environ.items() if k != "SAMPO_LANE_CHILD"}
        old = dict(os.environ)
        os.environ.clear()
        os.environ.update(env)
        try:
            r, timed_out = train._run_check([sys.executable, "-c", SHOW], self.repo)
        finally:
            os.environ.clear()
            os.environ.update(old)
        self.assertFalse(timed_out)
        self.assertIn("child=1", r.stdout, "py.lane_reexec hands over to the lane's plugin unless this is set")

    def test_the_trunk_flag_is_not_left_in_this_process(self):  # trunk
        before = os.environ.get("SAMPO_LANE_CHILD")
        train._run_check([sys.executable, "-c", SHOW], self.repo)
        self.assertEqual(os.environ.get("SAMPO_LANE_CHILD"), before)


class NoResult(AcceptCase):
    def test_a_check_that_ended_with_no_result_names_its_exit_and_last_line(self):  # no_result
        tid = self.a_lane(1)
        self.accept(tid)
        self.child(tid, 1, [sys.executable, "-c", USAGE], tries=2)
        self.assertEqual(self.meta_of(tid)["check_state"], "cut_off", "still held the same way")
        text = open(self.brief(tid).path).read()
        self.assertIn("no result", text)
        self.assertIn("exit 2", text)
        self.assertIn("unrecognized arguments: --hold", text)
        self.assertNotIn("at its deadline", text, "nothing was cut off: the check ended on its own")

    def test_a_check_cut_off_with_a_result_still_says_deadline(self):  # no_result
        tid = self.a_lane(1)
        self.accept(tid)
        self.child(tid, 1, stub(124, cut_off=True), tries=2)
        self.assertIn("at its deadline", open(self.brief(tid).path).read())


SLEEPER = "import time; time.sleep(90)"
# what a real check is: a command in a session of its own (sampo check starts each one so), which starts another
# (tests/run.py starts each class so); the innermost writes its pid where the test can read it
MID = ("import subprocess, sys\np = subprocess.Popen([sys.executable, '-c', %r], start_new_session=True)\n"
       "open(sys.argv[1], 'w').write(str(p.pid))\np.wait()\n" % SLEEPER)
TOP = "import subprocess, sys\nsubprocess.Popen([sys.executable, '-c', %r, sys.argv[1]], start_new_session=True).wait()\n" % MID


def gone(pid):
    """Whether a process has ended (a zombie waiting on its new parent counts as ended)."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            stat = f.read()
    except OSError:
        return True
    return stat[stat.rindex(b")") + 2:stat.rindex(b")") + 3] == b"Z"


class Stop(AcceptCase):
    """T-4417: a stopped check leaves nothing running: three stopped checks left three near-full suites alive for 10 to
    23 minutes on 8 of the 16 test slots, each in a session the kill of the check's own group never reached."""

    def sleeper(self, pidfile, seconds=20):
        end = time.time() + seconds
        while time.time() < end and not (os.path.exists(pidfile) and open(pidfile).read().strip()):
            time.sleep(0.05)
        self.assertTrue(os.path.exists(pidfile), "the stand-in check never started its innermost process")
        pid = int(open(pidfile).read())
        self.addCleanup(lambda: gone(pid) or os.kill(pid, signal.SIGKILL))
        return pid

    def ends(self, pid, seconds=10):
        end = time.time() + seconds
        while time.time() < end and not gone(pid):
            time.sleep(0.05)
        return gone(pid)

    def test_a_check_past_its_wall_limit_leaves_nothing_running(self):  # leaves_nothing
        pidfile = os.path.join(self.tmp, "sleeper.pid")
        t0 = time.time()
        with mock.patch.object(train, "CHECK_WALL_S", 8):
            r, timed_out = train._run_check([sys.executable, "-c", TOP, pidfile], self.repo)
        self.assertTrue(timed_out)
        self.assertLess(time.time() - t0, 45, "the runner waited on the output pipe a surviving process held open")
        pid = self.sleeper(pidfile, seconds=1)
        self.assertTrue(self.ends(pid), "the check's innermost process outlived the kill at the wall limit")

    def test_the_tree_kill_refuses_init_and_the_caller_itself_and_leaves_nothing_else_touched(self):  # leaves_nothing
        from sampo import core
        self.assertEqual(core.kill_tree(1), 0, "pid 1's tree is every process")
        self.assertEqual(core.kill_tree(0), 0)
        self.assertEqual(core.kill_tree(os.getpid()), 0, "the caller's own tree holds the caller")
        other = subprocess.Popen([sys.executable, "-c", SLEEPER])
        self.addCleanup(other.kill)
        doomed = subprocess.Popen([sys.executable, "-c", SLEEPER])
        self.assertEqual(core.kill_tree(doomed.pid), 1)
        self.assertEqual(doomed.wait(timeout=10), -signal.SIGKILL)
        self.assertIsNone(other.poll(), "a sibling outside the tree was signalled")

    def test_a_check_stopped_by_lane_rm_leaves_nothing_running(self):  # leaves_nothing
        pidfile = os.path.join(self.tmp, "sleeper.pid")
        code = "import sys\nfrom sampo import train\ntrain._run_check([sys.executable, '-c', sys.argv[1], sys.argv[2]], sys.argv[3])\n"
        lib = os.path.dirname(os.path.dirname(os.path.abspath(train.__file__)))
        runner = subprocess.Popen([sys.executable, "-c", code, TOP, pidfile, self.repo], env={**os.environ, "PYTHONPATH": lib})
        self.addCleanup(runner.kill)
        pid = self.sleeper(pidfile)
        runner.send_signal(signal.SIGTERM)  # what stop_check sends the check's process
        self.assertEqual(runner.wait(timeout=20), 128 + signal.SIGTERM)
        self.assertTrue(self.ends(pid), "the check's innermost process outlived sampo lane rm")


def marks(path, exit_code=0):
    """A stand-in check that leaves a file behind when it runs, then prints a green (or red) result."""
    data = json.dumps({"exit": exit_code, "failed": [], "cut_off": False, "tests": [], "changed": []})
    return [sys.executable, "-c", f"import sys; open({path!r}, 'w').close(); print({data!r}); sys.exit({exit_code})"]


class Turn(AcceptCase):
    def another_check_runs(self):
        """The turn held the way another lane's check process holds it (an flock on its own open file)."""
        d = os.path.join(self.p.dir, "check")
        os.makedirs(d, exist_ok=True)
        held = open(os.path.join(d, "turn.lock"), "a")
        fcntl.flock(held, fcntl.LOCK_EX)
        self.addCleanup(held.close)
        return held

    def log_of(self, tid):
        return open(self.brief(tid).path).read()

    def until(self, done, seconds=20):
        end = time.time() + seconds
        while time.time() < end and not done():
            time.sleep(0.05)

    def test_a_check_takes_its_turn_behind_another_lanes_check(self):  # turn
        tid = self.a_lane(1)
        self.accept(tid)
        ran = os.path.join(self.tmp, "ran")
        held = self.another_check_runs()
        t = threading.Thread(target=self.child, args=(tid, 1, marks(ran)), daemon=True)
        t.start()
        self.until(lambda: "waits its turn" in self.log_of(tid) or os.path.exists(ran))
        self.assertFalse(os.path.exists(ran), "its command started while another lane's check ran")
        self.assertIn("waits its turn", self.log_of(tid))
        hold = train._check_hold(self.brief(tid), self.tip(1), self.repo) or ""  # waits
        self.assertIn("check running", hold, "a check that waits is still the lane's one check")
        held.close()
        t.join(30)
        self.assertTrue(os.path.exists(ran), "it starts when the other one ends")
        self.assertEqual(self.meta_of(tid)["check_state"], "green")

    def test_a_free_turn_is_taken_without_a_word(self):  # turn
        tid = self.a_lane(1)
        self.accept(tid)
        self.child(tid, 1, stub(0))
        self.assertEqual(self.meta_of(tid)["check_state"], "green")
        self.assertNotIn("waits its turn", self.log_of(tid))

    def test_a_lane_whose_tip_moved_while_it_waited_its_turn_runs_nothing(self):  # turn
        tid = self.a_lane(1)
        self.accept(tid)
        ran = os.path.join(self.tmp, "ran")
        a = {"project": [self.p.slug, self.p.root, self.p.dir, self.p.lane], "main": self.repo, "id": tid,
             "tip": self.tip(1), "tries": 1, "argv": marks(ran), "since": None}
        held = self.another_check_runs()
        t = threading.Thread(target=train._accept_check_child, args=(a,), daemon=True)
        t.start()
        self.until(lambda: "waits its turn" in self.log_of(tid) or os.path.exists(ran))
        lane = os.path.join(self.tmp, "lane1")
        with open(os.path.join(lane, "later.txt"), "w") as f:
            f.write("more\n")
        self.git("add", "later.txt", cwd=lane)
        self.git("commit", "-q", "-m", "more work", cwd=lane)
        held.close()
        t.join(30)
        self.assertFalse(os.path.exists(ran), "a check of a tip the lane has left is a suite for nothing")
        self.assertIn("the lane tip moved", self.log_of(tid))
        self.assertNotIn("check_state", self.meta_of(tid))

    def test_a_check_that_waits_ends_on_the_signal_lane_rm_sends(self):  # waits
        self.another_check_runs()
        code = ("import sys, types\nfrom sampo import train\nprint('in', flush=True)\n"
                "with train._check_turn(types.SimpleNamespace(dir=sys.argv[1])):\n    print('got', flush=True)\n")
        lib = os.path.dirname(os.path.dirname(os.path.abspath(train.__file__)))
        proc = subprocess.Popen([sys.executable, "-c", code, self.p.dir], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, env={**os.environ, "PYTHONPATH": lib})
        self.addCleanup(proc.kill)
        self.assertEqual(proc.stdout.readline().strip(), "in", "the stand-in did not start")
        time.sleep(0.5)
        self.assertIsNone(proc.poll(), "it waits for its turn")
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=20)
        self.assertEqual(proc.returncode, -signal.SIGTERM)
        self.assertNotIn("got", out)
