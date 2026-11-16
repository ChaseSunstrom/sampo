"""T-4605: closes after the first in one land reuse the gates' pass. What a close itself writes (CHANGELOG.md, a brief
or log line in the sync mirror) is no code change; a code change reruns the linked tests; a train's affected-only pass
plus a green post-merge full suite on the same code tree covers every linked test (the T-4607 close reran about 480
modules for 27 minutes after the train that landed it had passed and only close commits followed: the later train's
pass had run few modules, and the full suite that verified its tree was never asked). Also: no test builds a Project
with an empty state folder (test_doctor's disk check did, and a briefs-closed.json leaked into plugin/tests)."""
import glob
import json
import os
import re
import subprocess
import unittest

from helpers import ForemanTestCase

import test_close_step_times as base
from sampo import cli
from sampo import core as c
from sampo import lanes

HERE = os.path.dirname(os.path.abspath(__file__))
TREE = {"cmd": "python3 tests/run.py", "exit": 0}


class CloseReuseRow(ForemanTestCase):
    def setUp(self):
        super().setUp()
        base.CloseStepTimes.fixture(self)  # the fixture project: a suite whose one test counts its own runs
        # a second module, so each task owns its own file (two tasks on one file leave it to neither: cli.affected)
        self.write("calc2.py", base.SRC.replace("add", "sub"))
        with open(os.path.join(self.repo, "tests", "test_calc.py")) as f:
            self.write("tests/test_calc2.py", f.read().replace("import calc\n", "import calc2 as calc\n")
                       .replace("calc.add", "calc.sub"))
        self.commit("a second module")
        self.fm("check", "affected", "python3 tests/run.py {names}")

    write, runs, steps_of = base.CloseStepTimes.write, base.CloseStepTimes.runs, base.CloseStepTimes.steps_of
    finish = base.CloseStepTimes.finish

    def new(self, title):
        """A task focused now, before the code changes: focus is where its base (the tree its diff is read from) is set.
        Its scope names what the close writes too (the changelog and the sync mirror)."""
        tid = self.fm_json("task", "new", title, "--type", "FEATURE", "--tier", "S", "--ac", "works :: true",
                           "--step", "s", "--scope", f"calc.py,calc2.py,CHANGELOG.md,{c.MIRROR}")["id"]
        self.fm("focus", tid)
        return tid

    def commit(self, msg):
        subprocess.run(["git", "-C", self.repo, "add", "-A"], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", msg], check=True)

    def close(self, tid):  # no refocus: focusing a task again re-bases it on the tree as it is then
        return self.finish(tid)

    def test_a_second_close_after_only_a_changelog_and_a_mirror_brief_changed_reuses_the_pass(self):
        a, b = self.new("First"), self.new("Second")
        self.write("calc.py", base.SRC + "# tidy\n")
        self.write("calc2.py", base.SRC.replace("add", "sub") + "# tidy\n")
        self.fm("check", env={"FOREMAN_SENTINEL": "off"})  # the train's pass on the merged code
        ran = self.runs()
        out = self.close(a)
        self.assertEqual(self.steps_of(out)[1]["affected"][1], "covered", out)
        # what the first close wrote: its changelog line and its brief's mirror copy, committed as the close commits
        self.write("CHANGELOG.md", "# Changelog\n\n- the first change (T-1)\n")
        self.write(f"{c.MIRROR}/tasks/T-9001-first.md", "---\nid: T-9001\nstatus: done\n---\n# First\n")
        self.write(f"{c.MIRROR}/log.jsonl", '{"event": "done"}\n')
        self.commit("close the first")
        out = self.close(b)
        self.assertEqual(self.runs(), ran, "the second close reran the linked tests: only close-written files differ")
        self.assertEqual(self.steps_of(out)[1]["affected"][1], "covered", out)

    def test_a_code_change_between_the_closes_reruns_them(self):
        a, b = self.new("First"), self.new("Second")
        self.write("calc.py", base.SRC + "# tidy\n")
        self.write("calc2.py", base.SRC.replace("add", "sub") + "# tidy\n")
        self.fm("check", env={"FOREMAN_SENTINEL": "off"})
        self.close(a)
        ran = self.runs()
        self.write("CHANGELOG.md", "# Changelog\n\n- the first change (T-1)\n")
        self.write("calc2.py", base.SRC.replace("add", "sub") + "# tidy\n# another lane's code\n")  # real code landed
        self.commit("a train landed another lane")
        out = self.close(b)
        self.assertGreater(self.runs(), ran, "a code change between the closes must rerun the linked tests")
        # T-4822: the module that reads the changed file reruns, the one the pass held does not
        self.assertEqual(self.runs(), ran + 1, out)
        self.assertTrue(self.steps_of(out)[1]["affected"][1].startswith("ran"), out)

    # a train's affected-only pass names the modules it ran: the full suite after landing (the post-merge verify)
    # covers the rest, so a close whose linked tests the pass never ran may count on a green verify of the same code
    def _train_row(self, modules):
        p = self.p
        ct = c.code_tree(self.repo)
        c.log_event(p, "check_run", data={
            "tree": "0" * 12, "code_tree": ct, "env": c.env_id(), "train": ["T-1"],
            "results": [dict(TREE, affected=modules)]})
        return ct

    def _checks(self):
        return list(c.read_meta(self.p).get("checks") or [])

    def _pass(self, names, why):
        snap = c.worktree_tree(self.repo)
        return cli._cached_pass(self.p, self._checks(), snap[:12], (), c.code_tree(self.repo, snap), names, why)

    def _verify(self, state="green", sha=None):
        sha = sha or subprocess.run(["git", "-C", self.repo, "rev-parse", "HEAD"], capture_output=True, text=True,
                                    check=True).stdout.strip()
        os.makedirs(lanes._pm_dir(self.p), exist_ok=True)
        c.write_atomic(os.path.join(lanes._pm_dir(self.p), f"{sha}.json"), json.dumps(
            {"sha": sha, "task": "T-1", "state": state, "run_at": sha, "n": 1, "at": c.now()}))

    def test_an_affected_only_pass_is_enough_only_for_the_modules_it_ran(self):
        self._train_row(["test_other"])
        why = []
        self.assertIsNone(self._pass(["test_calc"], why))
        self.assertIn("ran only 1 affected module(s)", why[0])
        self.assertTrue(self._pass(["test_other"], []))

    def test_a_green_post_merge_suite_on_the_same_code_tree_covers_what_the_train_did_not_run(self):
        self._train_row(["test_other"])
        self._verify("green")
        why = []
        hit = self._pass(["test_calc", "test_other"], why)
        self.assertTrue(hit, why)
        self.assertIn("post-merge", hit)

    def test_a_pending_or_other_tree_post_merge_run_covers_nothing(self):
        self._train_row(["test_other"])
        self._verify("pending")
        self.assertIsNone(self._pass(["test_calc"], []), "a verify still pending says nothing")
        self._verify("green")
        self.write("calc.py", base.SRC + "# code the green suite never saw\n")
        self._train_row(["test_other"])  # the train's pass on the code as it is now
        why = []
        self.assertIsNone(self._pass(["test_calc"], why), why)

    # T-4822: a pass that covers most of the linked tests leaves the close only the rest. T-4820's close ran 407 modules
    # for 361 s because the train's set lacked ONE module the close's own set held; T-4826's, 311 s, because three files
    # differed from the train's tree: the all-or-nothing rule reran everything either time
    def _two_changed(self):
        tid = self.new("Both")
        self.write("calc.py", base.SRC + "# tidy\n")
        self.write("calc2.py", base.SRC.replace("add", "sub") + "# tidy\n")
        return tid

    def _note(self, out):
        return self.steps_of(out)[1]["affected"][1]

    def test_a_pass_that_ran_all_but_one_linked_module_leaves_the_close_that_one(self):
        tid = self._two_changed()
        self._train_row(["test_calc"])  # the train's set lacked test_calc2
        before = self.runs()
        out = self.close(tid)
        self.assertEqual(self.runs(), before + 1, "only the module the pass never ran: " + out)
        self.assertTrue(self._note(out).startswith("ran 1 of 2"), out)
        self.assertIn("test_calc2", out)
        ev = c.ledger_events(self.p, ("close_affected",))[-1]["data"]
        self.assertEqual(ev["residual"]["ran"], ["test_calc2"])
        self.assertEqual(ev["residual"]["covered"], 1)

    def test_a_pass_on_other_code_leaves_the_close_only_what_depends_on_the_difference(self):
        tid = self._two_changed()
        self.fm("check", env={"FOREMAN_SENTINEL": "off"})  # a full pass on the merged code
        self.write("calc2.py", base.SRC.replace("add", "sub") + "# tidy\n# changed after the pass\n")
        before = self.runs()
        out = self.close(tid)
        self.assertEqual(self.runs(), before + 1, "test_calc does not read calc2.py: " + out)
        self.assertTrue(self._note(out).startswith("ran 1 of 2"), out)

    def test_a_pass_on_other_code_with_nothing_depending_on_the_difference_covers_the_close(self):
        tid = self._two_changed()
        self.fm("check", env={"FOREMAN_SENTINEL": "off"})
        self.write("lonely.py", "X = 1\n")  # code no test links
        self.fm("task", "log", tid, "scope: a file of another lane, no test links it")
        before = self.runs()
        out = self.close(tid)
        self.assertEqual(self.runs(), before, out)
        self.assertEqual(self._note(out).split(" ")[0], "covered", out)
        self.assertIn("linked tests: covered by the gates' pass", out)

    def test_the_changelog_is_read_only_when_the_residual_leaves_nothing_to_run(self):
        # T-4822 review: test_docs.Judgment (up to 300 s) ran on nearly every close after a train; a close that runs
        # tests reads no changelog (as before), one that would run none must not let a bad line ride in
        from unittest import mock
        tests = ["tests/test_calc.py", "tests/test_calc2.py"]
        self.write("CHANGELOG.md", "# Changelog\n\n- a line the rows never saw (T-1)\n")  # no row holds its hash
        with mock.patch.object(cli, "_changelog_reads_clean", return_value=False) as reads:
            self._train_row(["test_calc"])
            got = cli._residual(self.p, tests, {})
            self.assertEqual(got["run"], ["test_calc2"], got)
            reads.assert_not_called()
            self._train_row(["test_calc", "test_calc2"])
            self.assertIsNone(cli._residual(self.p, tests, {}), "nothing left to run and a changelog that reads dirty")
            reads.assert_called()

    def test_a_failed_or_foreign_pass_leaves_nothing(self):
        tid = self._two_changed()
        c.log_event(self.p, "check_run", data={
            "tree": "0" * 12, "code_tree": c.code_tree(self.repo), "env": "another-machine", "train": ["T-1"],
            "results": [dict(TREE, affected=["test_calc", "test_calc2"])]})
        before = self.runs()
        out = self.close(tid)
        self.assertEqual(self.runs(), before + 2, "a pass from another environment covers no module: " + out)
        self.assertEqual(self._note(out), "ran", out)

    def test_the_post_merge_suite_of_a_reverted_merge_covers_nothing(self):
        self._train_row(["test_other"])
        self._verify("green")
        sha = next(iter(lanes.pm_records(self.p)))
        lanes.pm_set(self.p, sha, after_reverts=["abc"])
        self.assertIsNone(self._pass(["test_calc"], []))


class NoEmptyStateFolder(unittest.TestCase):
    def test_no_test_builds_a_project_with_an_empty_state_folder(self):
        """Project(name, root, "") joins state files to a bare name: a load of its briefs wrote briefs-closed.json into
        the working directory (plugin/tests), and one was committed. T-4600's own guard test builds one on purpose."""
        bad = []
        for path in sorted(glob.glob(os.path.join(HERE, "test_*.py"))):
            name = os.path.basename(path)
            if name in ("test_close_step_times.py", os.path.basename(__file__)):
                continue
            with open(path, encoding="utf-8") as f:
                for n, line in enumerate(f, 1):
                    if re.search(r"\bProject\([^()]*,\s*(?:\"\"|'')\s*[,)]", line):
                        bad.append(f"{name}:{n}")
        self.assertEqual(bad, [], "give each Project a real temp state folder")

    def test_the_tests_folder_holds_no_state_index_file(self):
        stray = [f for f in os.listdir(HERE) if f.startswith("briefs-") and f.endswith(".json")]
        self.assertEqual(stray, [], "a test leaked its briefs index into the tests folder")


if __name__ == "__main__":
    unittest.main()
