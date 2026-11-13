"""T-4542, T-4563: the repo-shape tests are one list (core.REPO_SHAPE: its modules, then its module.Class entries). The builder
contract names the modules before the report (fm-builder.md through the <SHAPE> placeholder, lanes.contract() substitutes
them). sampo lane accept starts the lane's check and returns at once (T-4563): the check's child runs the modules first,
before its long part. Red records the refusal on the lane and ends the child; green is recorded at the tip and the hold
check runs without the classes (--shape-green), a fresh run without them holds the lane clean at that tip; cut or a tree
without the modules goes on as before, and a retry runs no module again. The runs here are stubbed (c.run_command,
c.run_record, train._shape_run, train._run_check); none starts a real test run."""
# map-links: plugin/lib/sampo/train.py
import os
import unittest
from unittest import mock

from test_accept_check import AcceptCase, OLD, FRESH, stub

from sampo import cli
from sampo import core as c
from sampo import lanes as slanes
from sampo import train

AGENT = os.path.join(os.path.dirname(__file__), "..", "agents", "fm-builder.md")
MODULES = ("test_docs", "test_left_out", "test_rename_aliases", "test_rename_scan", "test_run", "test_fast_equiv")
CLASSES = ("test_docs.PluginFiles", "test_left_out.LeftOut", "test_rename_aliases.RenameAliases", "test_run.EnvClassified")


class ContractShape(unittest.TestCase):
    def test_the_shape_modules_and_classes_are_one_list(self):  # T-4563
        self.assertEqual(c.REPO_SHAPE, MODULES + CLASSES)
        self.assertEqual(c.repo_shape_modules(), MODULES)
        self.assertEqual(c.repo_shape_classes(), CLASSES)
        self.assertFalse(hasattr(c, "REPO_SHAPE_MODULES"), "one list, not two constants")
        self.assertFalse(hasattr(c, "REPO_SHAPE_CLASSES"), "one list, not two constants")

    def test_the_shape_modules_are_named_in_the_builder_contract(self):  # T-4542
        with open(AGENT, encoding="utf-8") as f:
            agent = f.read()
        self.assertIn("<SHAPE>", agent)
        self.assertIn("Before your report", agent)
        text = slanes.contract("T-9", "abc123", "/main")
        self.assertNotIn("<SHAPE>", text)
        self.assertIn("python3 plugin/tests/run.py " + " ".join(MODULES), text)
        self.assertIn("Before your report", text)


class ShapeRun(unittest.TestCase):
    def run_shape(self, code, out="", record=None):
        with mock.patch.object(c, "run_command", return_value=(code, out)) as run, \
                mock.patch.object(c, "run_record", return_value=record):
            verdict = train._shape_run("/lane")
        return verdict, run

    def test_green_run_goes_on_with_no_names(self):  # T-4542
        verdict, run = self.run_shape(0, "Ran 65 tests in 3s\nOK")
        self.assertEqual(verdict, ("green", ""))
        argv, kw = run.call_args[0], run.call_args[1]
        self.assertIn("python3 plugin/tests/run.py " + " ".join(MODULES), argv[1])
        self.assertTrue(kw.get("niced"))
        self.assertEqual(argv[2], train.SHAPE_WALL_S)

    def test_red_run_names_the_failing_classes_from_the_record(self):  # T-4542
        rec = {"ok": False, "failed": [{"class": "test_docs.PluginFiles", "test": "t", "first": "x"},
                                       {"class": "test_run.EnvClassified", "test": "u", "first": "y"},
                                       {"class": "test_docs.PluginFiles", "test": "v", "first": "z"}]}
        verdict, _ = self.run_shape(1, "failed", rec)
        self.assertEqual(verdict, ("red", "test_docs.PluginFiles, test_run.EnvClassified"))

    def test_red_run_with_no_record_names_the_classes_its_output_names(self):  # T-4542
        verdict, _ = self.run_shape(1, "  failed class: test_left_out.LeftOut\n")
        self.assertEqual(verdict, ("red", "test_left_out.LeftOut"))

    def test_a_cut_run_or_one_with_no_named_class_is_no_verdict(self):  # T-4542
        self.assertEqual(self.run_shape(124, "timed out after 900s")[0], ("cut", ""))
        self.assertEqual(self.run_shape(1, "could not run bash", None)[0], ("cut", ""))
        self.assertEqual(self.run_shape(1, "", {"ok": False, "not_run": ["x"], "failed": []})[0], ("cut", ""))


class AcceptShape(AcceptCase):
    def setUp(self):
        super().setUp()
        self.shape = mock.patch.object(train, "_shape_run", return_value=("green", ""))
        self.shape_run = self.shape.start()
        self.addCleanup(self.shape.stop)
        tree = mock.patch.object(train, "_shape_tree", return_value=True)  # the lane has the modules (a Sampo tree)
        tree.start()
        self.addCleanup(tree.stop)

    def test_accept_returns_at_once_and_runs_no_module_itself(self):  # T-4563
        tid = self.lane(1, "f1.txt", when=OLD)
        out = self.accept(tid)
        self.shape_run.assert_not_called()
        self.assertEqual(len(self.spawned), 1, "the lane's check starts; its child runs the modules first")
        self.assertIn("check started", out)
        self.assertEqual(self.queued(), [])

    def test_a_red_module_run_ends_the_child_before_the_long_check_and_is_recorded_on_the_lane(self):  # T-4563
        tid = self.lane(1, "f1.txt", when=OLD)
        self.accept(tid)
        self.shape_run.return_value = ("red", "test_docs.PluginFiles, test_run.EnvClassified")
        with mock.patch.object(train, "_run_check") as long_check:
            self.assertEqual(self.child(tid, 1, stub(0)), 1)
        long_check.assert_not_called()
        m = self.meta_of(tid)
        self.assertEqual((m["check_state"], m["check_failed"]), ("red", "test_docs.PluginFiles test_run.EnvClassified"))
        self.assertIn("repo-shape modules are red", self.brief(tid).section("Log"))
        self.assertEqual(self.queued(), [])
        self.land_started.assert_not_called()
        out = self.accept(tid)  # held at its tip: named, no retry
        self.assertIn("test_docs.PluginFiles", out)
        self.assertEqual(len(self.spawned), 1, out)

    def test_a_green_module_run_is_recorded_at_the_tip_and_the_hold_check_runs_without_the_classes(self):  # T-4563
        tid = self.lane(1, "f1.txt", when=OLD)
        self.accept(tid)
        with mock.patch.object(train, "_run_check", wraps=train._run_check) as long_check:
            self.assertEqual(self.child(tid, 1, stub(0)), 0)
        self.assertIn("--shape-green", long_check.call_args[0][0])
        self.assertEqual(self.meta_of(tid)["check_shape"], self.tip(1))
        self.shape_run.assert_called_once()
        self.assertEqual(self.meta_of(tid)["check_state"], "green")
        self.assertEqual(self.queued(), [tid])

    def test_a_fresh_run_without_the_classes_is_clean_once_the_module_run_is_green_at_its_tip(self):  # T-4563
        tid = self.lane(1, "f1.txt", when=OLD)
        self.record_run(tid, FRESH, cmd="python3 plugin/tests/run.py test_x")
        b = c.find_brief(self.p, tid)
        b.meta["check_shape"] = self.tip(1)
        c.save_brief(self.p, b)
        self.accept(tid)
        self.assertEqual(self.spawned, [], "the reused green holds the classes: no check again")
        self.assertEqual(self.queued(), [tid])

    def test_a_cut_module_run_is_no_verdict_and_the_long_check_runs_the_classes_itself(self):  # T-4563
        tid = self.lane(1, "f1.txt", when=OLD)
        self.accept(tid)
        self.shape_run.return_value = ("cut", "")
        with mock.patch.object(train, "_run_check", wraps=train._run_check) as long_check:
            self.assertEqual(self.child(tid, 1, stub(0)), 0)
        self.assertNotIn("--shape-green", long_check.call_args[0][0])
        self.assertNotIn("check_shape", self.meta_of(tid))
        self.assertEqual(self.queued(), [tid])

    def test_a_retry_runs_no_module_again(self):  # T-4563
        tid = self.lane(1, "f1.txt", when=OLD)
        self.accept(tid)
        self.assertEqual(self.child(tid, 1, stub(0), tries=2), 0)
        self.shape_run.assert_not_called()
        self.assertEqual(self.queued(), [tid])

    def test_a_lane_with_no_shape_modules_in_its_tree_runs_none_and_its_check_goes_on(self):  # T-4542
        tid = self.lane(1, "f1.txt", when=OLD)
        self.accept(tid)
        with mock.patch.object(train, "_shape_tree", return_value=False):
            self.assertEqual(self.child(tid, 1, stub(0)), 0)
        self.shape_run.assert_not_called()
        self.assertEqual(self.queued(), [tid], "a tree without the modules (not Sampo's) goes on as before")

    def test_a_check_already_running_at_the_tip_starts_no_module_run(self):  # T-4542
        tid = self.lane(1, "f1.txt", when=OLD)
        b = c.find_brief(self.p, tid)
        b.meta.update(check_state="running", check_tip=self.tip(1), check_pid=self.pid, check_ticks=train._ticks(self.pid))
        c.save_brief(self.p, b)
        self.accept(tid)
        self.shape_run.assert_not_called()

    def test_the_parser_takes_shape_green_on_a_hold_check(self):  # T-4563
        args = cli.build_parser().parse_args(["check", "--affected", "--hold", "--shape-green", "--evidence", "T-1"])
        self.assertTrue(args.shape_green)
        self.assertTrue(args.hold)


if __name__ == "__main__":
    unittest.main()
