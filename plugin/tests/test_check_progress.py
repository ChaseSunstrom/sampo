"""T-4892: a lane's check is selected wide (1,100-1,950 classes for a one-function edit to a hub), so under load its run
was cut at its deadline, started once more, then held as 'cut off twice' until someone reran it by hand, although
run.py caches green classes and each try runs only what is left. A try that ran classes goes on; a try that ran none,
or the wall budget over all tries, still holds it. Each try's deadline is sized from the selection's recorded class
seconds over the CPU slots, never under the fixed limit. The detached start is replaced as in test_accept_check."""
# map-links: plugin/lib/sampo/train.py plugin/lib/sampo/cli.py plugin/tests/run.py
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

from test_accept_check import AcceptCase, stub

from sampo import cli
from sampo import core as c
from sampo import train

import run as runpy


def cut(classes, not_run):
    """A check stopped at its deadline whose run record says how many classes it had and how many it never reached."""
    return stub(124, cut_off=True, progress={"classes": classes, "not_run": not_run})


class Progress(AcceptCase):
    def test_a_try_that_ran_classes_is_started_again(self):
        tid = self.a_lane(1)
        self.accept(tid)
        self.child(tid, 1, cut(1100, 900))
        self.child(tid, 1, cut(900, 400), tries=2)  # past the old two tries: it still ran 500 classes
        self.assertEqual([s["tries"] for s in self.spawned], [1, 2, 3])
        self.assertEqual(self.meta_of(tid)["check_state"], "running")

    def test_a_try_that_ran_no_class_holds_the_lane_as_cut_off(self):
        tid = self.a_lane(1)
        self.accept(tid)
        self.child(tid, 1, cut(1100, 900))
        self.child(tid, 1, cut(900, 900), tries=2)
        self.assertEqual(len(self.spawned), 2)
        self.assertEqual(self.meta_of(tid)["check_state"], "cut_off")
        # held, not retried by the check itself; an explicit accept starts it again (T-4404, test_accept_restart)
        self.assertIn("cut off or ended without a result twice", train._check_hold(self.brief(tid), self.tip(1), self.repo))

    def test_later_tries_name_their_count(self):
        tid = self.a_lane(1)
        self.accept(tid)
        for n, (k, left) in enumerate(((1100, 900), (900, 500), (500, 500)), 1):
            self.child(tid, 1, cut(k, left), tries=n)
        self.assertEqual(self.meta_of(tid)["check_state"], "cut_off")
        self.assertIn("cut off or ended without a result 3x", train._check_hold(self.brief(tid), self.tip(1), self.repo))

    def test_the_wall_budget_over_all_tries_still_holds_it(self):
        tid = self.a_lane(1)
        self.accept(tid)
        cli.mutate(self.p, tid, lambda b: b.meta.update(check_first=round(time.time() - train.CHECK_WALL_S - 60)),
                   "check", {})
        self.child(tid, 1, cut(1100, 900), tries=2)
        self.assertEqual(len(self.spawned), 1)
        self.assertEqual(self.meta_of(tid)["check_state"], "cut_off")

    def test_a_cut_without_a_progress_record_keeps_todays_two_tries(self):
        tid = self.a_lane(1)
        self.accept(tid)
        self.child(tid, 1, stub(124, cut_off=True))
        self.child(tid, 1, stub(124, cut_off=True), tries=2)
        self.assertEqual(len(self.spawned), 2)
        self.assertEqual(self.meta_of(tid)["check_state"], "cut_off")

    def test_the_first_try_stamps_when_the_tries_began(self):
        tid = self.a_lane(1)
        before = time.time()
        self.accept(tid)
        first = float(self.meta_of(tid)["check_first"])
        self.child(tid, 1, cut(1100, 900))
        self.assertGreaterEqual(first, before - 1)
        self.assertEqual(float(self.meta_of(tid)["check_first"]), first, "a retry keeps the first try's start")


class SizedDeadline(unittest.TestCase):
    def test_run_py_estimates_a_selection_from_its_recorded_class_seconds(self):
        times = {"test_a.One": 30.0, "test_a.Two": 10.0, "test_b.Three": 60.0, "test_c.Four": 5.0}
        got = runpy.estimate(["test_a", "test_b"], times, slots=4)
        self.assertEqual(got["seconds"], 100.0)
        self.assertEqual(got["known"], 3)
        self.assertEqual(got["slots"], 4)
        self.assertEqual(runpy.estimate(["test_a.Two"], times, slots=4)["seconds"], 10.0)
        self.assertEqual(runpy.estimate(["test_zz"], times, slots=4)["known"], 0)

    def test_a_class_id_is_estimated_in_each_form_it_arrives_in(self):  # T-5588 review: parse gives "mod.Class."
        times = {"test_a.One": 30.0, "test_a.Two": 10.0, "test_a.Two.test_x": 4.0, "test_b.Three": 60.0}
        for name in ("test_a.Two.", "test_a::Two", "test_a.Two"):
            self.assertEqual(runpy.estimate([name], times, slots=4)["seconds"], 10.0, name)
        self.assertEqual(runpy.estimate(runpy.parse(["test_a::One", "test_b"]).names, times, slots=4),
                         {"seconds": 90.0, "known": 2, "slots": 4}, "the names as the command line parses them")
        self.assertEqual(runpy.estimate(["test_a.Tw."], times, slots=4)["known"], 0, "a prefix is no class")

    def test_run_py_answers_estimate_without_running_a_test(self):
        here = os.path.dirname(os.path.abspath(__file__))
        r = subprocess.run([sys.executable, os.path.join(here, "run.py"), "--estimate", "test_zz_no_such_module"],
                           capture_output=True, text=True, timeout=60, cwd=os.path.dirname(os.path.dirname(here)))
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        got = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual((got["seconds"], got["known"]), (0, 0))
        self.assertGreaterEqual(got["slots"], 1)

    def test_the_estimate_reads_each_plain_run_py_part_and_never_runs_one(self):
        """review: `cmd --estimate` under bash ran the first part of `A && B` for real; each plain part is asked alone"""
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "t"))
            ran = os.path.join(d, "ran")
            with open(os.path.join(d, "t", "run.py"), "w") as f:
                f.write("import json, sys\n"
                        "if sys.argv[1] != '--estimate':\n"
                        f"    open({ran!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n'); sys.exit(0)\n"
                        "print(json.dumps({'seconds': 10.0 * len(sys.argv[2:]), 'known': len(sys.argv[2:]), 'slots': 4}))\n")
            py = sys.executable
            got = cli._estimate(d, f"{py} t/run.py test_a test_b && {py} t/run.py test_c.One", dict(os.environ))
            self.assertEqual(got, {"seconds": 30.0, "known": 3, "slots": 4})
            ids = cli._estimate(d, f"{py} t/run.py test_a::One test_b::Two test_c", dict(os.environ))
            self.assertEqual(ids, {"seconds": 30.0, "known": 3, "slots": 4}, "T-5588: the class ids --affected passes")
            for bad in ("test_a::", "test_a::One::x", "::One", "test_a:One", "test_a::One;x"):
                self.assertIsNone(cli._estimate(d, f"{py} t/run.py {bad}", dict(os.environ)), bad)
            self.assertIsNone(cli._estimate(d, f"{py} t/run.py -k x test_a", dict(os.environ)), "a flag: not asked")
            self.assertIsNone(cli._estimate(d, f"(cd sub && {py} t/run.py test_a)", dict(os.environ)))
            self.assertIsNone(cli._estimate(d, "pytest tests/", dict(os.environ)))
            self.assertFalse(os.path.exists(ran), "no part ran its tests")

    def test_progress_needs_both_class_counts(self):
        self.assertEqual(cli.progress_of({"classes": 9, "classes_not_run": 4}), {"classes": 9, "not_run": 4})
        self.assertIsNone(cli.progress_of({"classes": 9, "n_not_run": 4}), "a missing count is no progress")
        self.assertIsNone(cli.progress_of(None))

    def test_the_rerun_hint_doubles_the_deadline_it_really_had(self):
        self.assertIn("--timeout 6000", cli.deadline_line(600, 3000, None))
        self.assertIn("--timeout 1200", cli.deadline_line(600, 540, None))

    def test_the_limit_is_sized_from_the_estimate_never_under_the_fixed_one(self):
        need = {"seconds": 6000.0, "known": 1100, "slots": 8}  # a 1,100-class selection: 750 s of wall on 8 slots
        sized = cli.sized_limit(600.0, need)
        self.assertGreater(sized, 750.0, "a selection that needs more than the fixed limit gets more")
        self.assertEqual(cli.sized_limit(600.0, {"seconds": 40.0, "known": 4, "slots": 8}), 600.0)
        self.assertEqual(cli.sized_limit(600.0, None), 600.0)
        self.assertIsNone(cli.sized_limit(None, need), "no limit stays no limit")
        self.assertLessEqual(cli.sized_limit(600.0, {"seconds": 10 ** 7, "known": 9, "slots": 1}),
                             train.CHECK_WALL_S, "never past the wall the accept runner kills at")


if __name__ == "__main__":
    unittest.main()
