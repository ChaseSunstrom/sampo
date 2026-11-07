"""T-2992: the standing-rule tests (self-docs, model rules, left-out, rename scan) run in every affected set, so a
builder branch that misses a MASTER bullet or an fm shim fails in its own lane, not after the merge."""
import os
import subprocess

from helpers import ForemanTestCase

from sampo import core as c


class AffectedRules(ForemanTestCase):
    def test_every_affected_run_carries_the_rule_tests(self):
        from sampo import cli
        self.fm("init")
        self.fm("check", "affected", "python3 -m pytest -q {tests}")
        files = {"plugin/lib/sampo/other.py": "N = 1\n"}
        files.update({f"plugin/tests/{t}.py": "N = 1\n" for t in cli.RULE_TESTS})
        for rel, body in files.items():
            os.makedirs(os.path.dirname(os.path.join(self.repo, rel)), exist_ok=True)
            with open(os.path.join(self.repo, rel), "w") as f:
                f.write(body)
        for args in (["add", "-A"], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "seed"]):
            subprocess.run(["git", "-C", self.repo, *args], check=True)
        self.fm("task", "new", "note it", "--type", "FIX", "--tier", "S", "--ac", "a :: true", "--step", "s", "--focus")
        p = c.find_project(self.repo, create=False)
        with open(os.path.join(self.repo, "plugin/lib/sampo/other.py"), "w") as f:
            f.write("N = 2\n")
        _, tests, _ = cli.affected(p)
        for t in cli.RULE_TESTS:
            self.assertIn(f"plugin/tests/{t}.py", tests)
        self.assertEqual(set(cli.RULE_TESTS), {"test_doctor", "test_models", "test_left_out", "test_rename_scan",
                                               "test_rename_aliases", "test_redefinitions", "test_suite_hygiene"})


class AffectedLaneBase(ForemanTestCase):
    """T-3696: a lane that merged main's branch answers only for its own changes. From the task's start commit, every
    file main changed since counted as the lane's: T-3603's lane picked 104 test modules where its own 6 files pick 21."""

    def commit(self, root, files, msg):
        for rel, body in files.items():
            os.makedirs(os.path.dirname(os.path.join(root, rel)), exist_ok=True)
            with open(os.path.join(root, rel), "w") as f:
                f.write(body)
        for args in (["add", "-A"], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", msg]):
            subprocess.run(["git", "-C", root, *args], check=True, capture_output=True)

    def setUp(self):
        super().setUp()
        self.fm("init")
        self.fm("check", "affected", "python3 -m pytest -q {tests}")
        self.commit(self.repo, {"src/mine.py": "N = 1\n", "src/theirs.py": "N = 1\n",
                                "tests/test_mine.py": "# map-links: src/mine.py\nN = 1\n",
                                "tests/test_theirs.py": "# map-links: src/theirs.py\nN = 1\n"}, "seed")
        self.lane = os.path.join(self.tmp, "lane")
        subprocess.run(["git", "-C", self.repo, "worktree", "add", "-q", "-b", "lane", self.lane], check=True,
                       capture_output=True)
        self.fm("task", "new", "lane work", "--type", "FIX", "--tier", "S", "--ac", "a :: true", "--step", "s",
                "--focus", cwd=self.lane)
        self.commit(self.lane, {"src/mine.py": "N = 2\n"}, "the lane's own change")
        self.commit(self.repo, {"src/theirs.py": "N = 2\n"}, "main moves on: another task's merge")
        tip = c._git(self.repo, "rev-parse", "HEAD").strip()
        subprocess.run(["git", "-C", self.lane, "-c", "user.email=t@t", "-c", "user.name=t", "merge", "-q", "--no-edit",
                        tip], check=True, capture_output=True)

    def test_a_lane_that_merged_main_selects_only_its_own_changes_tests(self):
        from sampo import cli
        _, tests, changed = cli.affected(c.find_project(self.lane, create=False))
        self.assertNotIn("src/theirs.py", changed)
        self.assertNotIn("tests/test_theirs.py", tests)
        self.assertIn("src/mine.py", changed)
        self.assertIn("tests/test_mine.py", tests)

    def test_no_main_worktree_or_the_main_checkout_keeps_the_task_base(self):
        from unittest import mock
        from sampo import cli
        p = c.find_project(self.lane, create=False)
        with mock.patch.object(c, "main_worktree", return_value=None):
            _, tests, changed = cli.affected(p)
        self.assertIn("src/theirs.py", changed)
        self.assertIn("tests/test_theirs.py", tests)
        self.fm("task", "new", "main work", "--type", "FIX", "--tier", "S", "--ac", "a :: true", "--step", "s", "--focus")
        self.commit(self.repo, {"src/theirs.py": "N = 3\n"}, "main's own task commits")
        _, tests, changed = cli.affected(c.find_project(self.repo, create=False))
        self.assertIn("src/theirs.py", changed)
        self.assertIn("tests/test_theirs.py", tests)
