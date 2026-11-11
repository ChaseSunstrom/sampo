"""T-4410: the check sampo lane accept starts is run by the trunk's sampo on the lane's tree (the lane's code and tests
are what it runs; the checker is not the lane's older copy, which may not know an option or may carry a check bug the
trunk has fixed), and a check that ends on its own without a result says so with its exit code and last output line
instead of 'cut off at its deadline'."""
# map-links: plugin/lib/sampo/train.py
import os
import sys

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
