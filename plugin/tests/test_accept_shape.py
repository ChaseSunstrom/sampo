"""T-4542: the repo-shape modules are one constant (core.REPO_SHAPE_MODULES). The builder contract names them as required
before the report (fm-builder.md through the <SHAPE> placeholder, lanes.contract() substitutes them), and sampo lane accept
runs them in the lane before it queues the long check: red refuses (nothing recorded, the failing classes named), green
or cut goes on as before. A lane whose check is already running at its tip starts no shape run. The runs here are
stubbed (c.run_command, c.run_record, train._shape_run's callers); none starts a real test run."""
# map-links: plugin/lib/sampo/train.py
import os
import unittest
from unittest import mock

from test_accept_check import AcceptCase, OLD

from sampo import core as c
from sampo import lanes as slanes
from sampo import train

AGENT = os.path.join(os.path.dirname(__file__), "..", "agents", "fm-builder.md")
MODULES = ("test_docs", "test_left_out", "test_rename_aliases", "test_rename_scan", "test_run", "test_fast_equiv")


class ContractShape(unittest.TestCase):
    def test_the_shape_modules_are_one_constant_named_in_the_builder_contract(self):  # T-4542
        self.assertEqual(c.REPO_SHAPE_MODULES, MODULES)
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

    def test_red_shape_module_refuses_before_the_long_check_and_names_the_classes(self):  # T-4542
        tid = self.lane(1, "f1.txt", when=OLD)
        self.shape_run.return_value = ("red", "test_docs.PluginFiles, test_run.EnvClassified")
        with self.assertRaises(c.PolicyError) as cm:
            self.accept(tid)
        self.assertIn("test_docs.PluginFiles, test_run.EnvClassified", str(cm.exception))
        self.assertEqual(self.spawned, [], "the long check is never queued on a red lane")
        self.assertEqual(self.queued(), [])
        self.assertFalse(any("(audit" in x for x in self.brief(tid).evidence()), "nothing recorded on a refused accept")

    def test_green_shape_modules_go_on_to_the_check_as_before(self):  # T-4542
        tid = self.lane(1, "f1.txt", when=OLD)
        out = self.accept(tid)
        self.shape_run.assert_called_once()
        self.assertEqual(len(self.spawned), 1, "the lane's own check starts, as before")
        self.assertIn("check started", out)

    def test_cut_shape_run_is_no_verdict_and_the_check_decides(self):  # T-4542
        tid = self.lane(1, "f1.txt", when=OLD)
        self.shape_run.return_value = ("cut", "")
        self.accept(tid)
        self.assertEqual(len(self.spawned), 1)

    def test_a_lane_with_no_shape_modules_in_its_tree_runs_none(self):  # T-4542
        tid = self.lane(1, "f1.txt", when=OLD)
        with mock.patch.object(train, "_shape_tree", return_value=False):
            self.accept(tid)
        self.shape_run.assert_not_called()
        self.assertEqual(len(self.spawned), 1, "a tree without the modules (not Sampo's) goes on as before")

    def test_a_check_already_running_at_the_tip_starts_no_shape_run(self):  # T-4542
        tid = self.lane(1, "f1.txt", when=OLD)
        b = c.find_brief(self.p, tid)
        b.meta.update(check_state="running", check_tip=self.tip(1), check_pid=self.pid, check_ticks=train._ticks(self.pid))
        c.save_brief(self.p, b)
        self.accept(tid)
        self.shape_run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
