"""T-4851: the lane check's log. `check started ... log check/ID.log` named a file the check process's own stdout was
(sampo lane accept starts it so), but _run_check held the check's output in memory and printed none of it: every such log
was 0 bytes (T-4147, T-4139, T-4189), and a check cut off at its deadline (60 of 1546 classes in 1200 s) left nothing to
read. Now the check's lines are written to that stdout as they arrive, the result is still read from the same text, and
a hold for a cut-off check names the log."""
# map-links: plugin/lib/sampo/train.py
import json
import os
import subprocess
import sys
import time

from test_accept_check import AcceptCase, stub
from test_express_lane import ExpressBase

from sampo import train

LIB = os.path.dirname(os.path.dirname(os.path.abspath(train.__file__)))

# the process sampo lane accept starts: its stdout is the log file; it runs one check and leaves what _run_check gave back
RUNNER = (
    "import json, sys\n"
    "from sampo import train\n"
    "wall, out, cwd, *argv = sys.argv[1:]\n"
    "train.CHECK_WALL_S = float(wall)\n"
    "r, timed_out = train._run_check(argv, cwd)\n"
    "json.dump({'stdout': r.stdout, 'rc': r.returncode, 'timed_out': timed_out, 'result': train._result_of(r)}, open(out, 'w'))\n"
)
# a check that prints a class line, waits for the test to have seen it in the log, then prints the rest and its result
LINES = (
    "import json, os, sys, time\n"
    "print('class test_a.A ok', flush=True)\n"
    "end = time.time() + 60\n"
    "while not os.path.exists(sys.argv[1]) and time.time() < end:\n"
    "    time.sleep(0.05)\n"
    "print('class test_b.B ok', flush=True)\n"
    "print(json.dumps({'exit': 0, 'failed': [], 'cut_off': False, 'tests': [], 'changed': []}, indent=2))\n"
)
HANG = "import time\nprint('class test_a.A ok', flush=True)\nprint('class test_b.B ok', flush=True)\ntime.sleep(600)\n"


def read(path):
    with open(path) as f:
        return f.read()


class CheckLog(AcceptCase):
    def runner(self, argv, wall=300):
        """_run_check in a process of its own whose stdout is a log file, as the detached check's is: (Popen, log, result file)."""
        log, res = os.path.join(self.tmp, "check-run.log"), os.path.join(self.tmp, "check-run.json")
        with open(log, "w") as out:
            proc = subprocess.Popen([sys.executable, "-c", RUNNER, str(wall), res, self.repo, *argv], stdout=out,
                                    stderr=subprocess.STDOUT, env={**os.environ, "PYTHONPATH": LIB})
        self.addCleanup(proc.kill)
        return proc, log, res

    def until(self, done, seconds=30):
        end = time.time() + seconds
        while time.time() < end:
            if done():
                return True
            time.sleep(0.05)
        return False

    def test_check_log_lines_arrive_while_the_check_runs_and_the_result_is_read_as_before(self):  # check_log_lines
        gate = os.path.join(self.tmp, "gate")
        proc, log, res = self.runner([sys.executable, "-c", LINES, gate])
        seen = self.until(lambda: "class test_a.A ok" in read(log))
        text = read(log)
        open(gate, "w").close()
        self.assertEqual(proc.wait(timeout=60), 0)
        self.assertTrue(seen, "the first class line is in the log before the check ends")
        self.assertNotIn("class test_b.B ok", text, "the second line came after the gate: the first was read while it ran")
        done = read(log)
        self.assertIn("class test_a.A ok", done)
        self.assertIn("class test_b.B ok", done)
        self.assertIn('"exit": 0', done, "the result is in the log too")
        got = json.loads(read(res))
        self.assertEqual((got["rc"], got["timed_out"]), (0, False))
        self.assertEqual(got["result"]["exit"], 0, "_check_result's text is the whole output, as before")
        self.assertIn("class test_a.A ok", got["stdout"])

    def test_check_log_lines_an_empty_or_unterminated_last_line_is_kept(self):  # check_log_lines
        code = "import sys; sys.stdout.write('no newline at the end')"
        proc, log, res = self.runner([sys.executable, "-c", code])
        self.assertEqual(proc.wait(timeout=60), 0)
        self.assertEqual(read(log), "no newline at the end")
        self.assertEqual(json.loads(read(res))["stdout"], "no newline at the end")

    def test_check_log_cut_a_check_killed_at_its_deadline_leaves_its_lines_in_the_log(self):  # check_log_cut
        proc, log, res = self.runner([sys.executable, "-c", HANG], wall=3)
        t0 = time.time()
        self.assertEqual(proc.wait(timeout=90), 0)
        self.assertLess(time.time() - t0, 60, "the reader did not hold the runner past the kill")
        got = json.loads(read(res))
        self.assertTrue(got["timed_out"])
        self.assertEqual(got["rc"], 124)
        done = read(log)
        self.assertIn("class test_a.A ok", done)
        self.assertIn("class test_b.B ok", done)
        self.assertIn("class test_b.B ok", got["stdout"])

    def test_check_log_cut_the_hold_for_a_check_cut_off_twice_names_the_log(self):  # check_log_cut
        tid = self.a_lane(1)
        self.accept(tid)
        self.child(tid, 1, stub(124, cut_off=True))
        self.child(tid, 1, stub(124, cut_off=True), tries=2)
        log = os.path.join(self.tmp, "check.log")
        self.assertEqual(self.meta_of(tid)["check_state"], "cut_off")
        self.assertIn(log, train._check_hold(self.brief(tid), self.tip(1), self.repo))
        self.assertIn(log, self.accept(tid))

    def test_check_log_cut_a_check_that_ended_without_a_result_names_the_log(self):  # check_log_cut
        tid = self.a_lane(1)
        self.accept(tid)
        self.child(tid, 1, [sys.executable, "-c", "print('boom')"], tries=2)
        said = [x for x in self.brief(tid).section("Log").splitlines() if "no result" in x]
        self.assertTrue(said and os.path.join(self.tmp, "check.log") in said[-1], said)


class ExpressTrainsTogether(ExpressBase):
    """T-5726: live, the express lanes (performance, security, urgent) named --first train together as one train, capped
    at the batch size, ahead of the ordinary lanes; gated, each still trains alone (T-5023)."""

    def test_express_live_lanes_train_together_ahead_of_the_ordinary_ones(self):
        ids = self.made(5)
        self.fm("lane", "mode", "live")
        self.fm("task", "set", ids[1], "type=PERFORMANCE")
        self.fm("task", "set", ids[3], "type=SECURITY")
        self.fm("task", "set", ids[4], "priority=urgent")
        self.queue(ids[0], ids[1], ids[3], ids[4])
        r = self.land("--batch", "5")
        self.assertEqual(self.trains(r), [f"{ids[1]}, {ids[3]}, {ids[4]}", f"{ids[0]}, {ids[2]}"], r.stderr)
        self.assertEqual([self.brief(t).status for t in ids], ["done"] * 5, r.stdout + r.stderr)

    def test_express_live_train_is_capped_at_the_batch_size(self):
        ids = self.made(4)
        self.fm("lane", "mode", "live")
        for t in ids[:3]:
            self.fm("task", "set", t, "type=PERFORMANCE")
        self.queue(*ids[:3])
        r = self.land("--batch", "2")
        self.assertEqual(self.trains(r), [f"{ids[0]}, {ids[1]}", ids[2], ids[3]], r.stderr)

    def test_express_gated_lanes_still_train_alone(self):
        ids = self.made(4)
        self.fm("task", "set", ids[1], "type=PERFORMANCE")
        self.fm("task", "set", ids[3], "type=SECURITY")
        self.queue(ids[0], ids[1], ids[3])
        r = self.land("--batch", "4")
        self.assertEqual(self.trains(r), [ids[1], ids[3], f"{ids[0]}, {ids[2]}"], r.stderr)

    def test_express_live_log_line_says_it_trains_with_the_other_express_lanes(self):
        ids = self.made(3)
        self.fm("lane", "mode", "live")
        self.fm("task", "set", ids[1], "type=PERFORMANCE")
        self.queue(ids[1])
        r = self.land("--batch", "3")
        self.assertIn(f"{ids[1]} is express (performance, security or urgent): it trains next, with the other express lanes",
                      r.stderr)
