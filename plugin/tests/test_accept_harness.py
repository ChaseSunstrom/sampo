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
