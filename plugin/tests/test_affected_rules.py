"""T-2992: the standing-rule tests (self-docs, model rules, left-out, rename scan) run in every affected set, so a
builder branch that misses a MASTER bullet or an fm shim fails in its own lane, not after the merge."""
import json
import os
import shutil
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


class SubprocessTestsAreLinked(ForemanTestCase):
    """T-3963: 2026-11-08's train left out T-3929 (night.py, moonscale.py: test_kalevala asserts the grow exit code by
    running the CLI) and T-3324 (hooks.py: test_breaker runs the hook as a subprocess and counts its log lines). No
    import or name links such a test to the module it drives, so the test names it on a map-links line."""

    def test_a_test_that_drives_a_module_through_a_subprocess_is_linked_to_it(self):
        from helpers import PLUGIN
        from sampo import map as smap
        root = os.path.dirname(PLUGIN)
        for test, mods in (("test_kalevala", ("night", "moonscale")), ("test_breaker", ("hooks",))):
            linked = smap._marked(root, f"plugin/tests/{test}.py")
            for mod in mods:
                self.assertIn(f"plugin/lib/sampo/{mod}.py", linked, f"{test} drives {mod} in a subprocess")


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


class SelfDocsAffected(ForemanTestCase):
    """T-4279: `sampo check --affected` runs the doctor's tree-only self docs check on the checked tree, so a docs/map
    line over 2000 characters fails in the lane, not 15 minutes later at the train's doctor gate. A docs-only edit with
    no linked test is checked too."""

    def setUp(self):
        super().setUp()
        from sampo import doctor
        self.fm("init")
        self.fm("check", "affected", "python3 -m pytest -q {tests}")
        real = os.path.dirname(os.path.abspath(doctor.PLUGIN))  # the running Sampo tree: its docs pass the check
        for name in ("MASTER.md", "README.md", "install.sh"):
            shutil.copyfile(os.path.join(real, name), os.path.join(self.repo, name))
        shutil.copytree(os.path.join(real, "docs", "map"), os.path.join(self.repo, "docs", "map"))
        for folder in ("rules", "agents"):
            shutil.copytree(os.path.join(real, "plugin", folder), os.path.join(self.repo, "plugin", folder))
        shutil.copytree(os.path.join(real, "plugin", "skills"), os.path.join(self.repo, "plugin", "skills"),
                        ignore=shutil.ignore_patterns("playbooks", "__pycache__"))
        os.makedirs(os.path.join(self.repo, "plugin", "lib", "sampo"), exist_ok=True)  # the marker of a Sampo tree
        for rel, body in (("src/mine.py", "N = 1\n"), ("tests/test_mine.py", "# map-links: src/mine.py\nN = 1\n")):
            os.makedirs(os.path.join(self.repo, os.path.dirname(rel)), exist_ok=True)
            with open(os.path.join(self.repo, rel), "w", encoding="utf-8") as f:
                f.write(body)
        for args in (["add", "-A"], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "seed"]):
            subprocess.run(["git", "-C", self.repo, *args], check=True, capture_output=True)
        self.fm("task", "new", "docs edit", "--type", "FIX", "--tier", "S", "--ac", "a :: true", "--step", "s", "--focus")
        self.shard = os.path.join(self.repo, "docs", "map", "files-1.md")
        with open(self.shard, encoding="utf-8") as f:
            self.original = f.read()

    def write_shard(self, text):
        with open(self.shard, "w", encoding="utf-8") as f:
            f.write(text)

    def test_self_docs_fails_a_docs_only_edit_with_a_map_line_over_2000_characters(self):
        self.write_shard(self.original + "- " + "x" * 2100 + "\n")
        line = self.original.count("\n") + 1  # the appended line's number
        r = self.fm("check", "--affected", check=False)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f"✗ self docs: docs/map/files-1.md:{line} is over 2000 characters", r.stdout)

    def test_self_docs_is_green_and_silent_once_the_line_is_fixed(self):
        self.write_shard(self.original + "- " + "x" * 2100 + "\n")
        self.assertEqual(self.fm("check", "--affected", check=False).returncode, 1)
        self.write_shard(self.original + "- short line\n")
        r = self.fm("check", "--affected", check=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("self docs", r.stdout)

    def test_self_docs_fails_a_code_change_whose_linked_tests_pass(self):
        """The case that parked three lanes: code and a map line changed together, the linked tests green."""
        self.fm("check", "affected", "true {tests}")
        with open(os.path.join(self.repo, "src", "mine.py"), "w", encoding="utf-8") as f:
            f.write("N = 2\n")
        self.write_shard(self.original + "- " + "x" * 2100 + "\n")
        r = self.fm("check", "--affected", "--evidence", "T-0001", check=False)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("tests/test_mine.py", r.stdout)
        self.assertIn("✗ self docs: docs/map/files-1.md:", r.stdout)
        shown = self.fm("task", "show", "T-0001").stdout
        recorded = [l for l in shown.splitlines() if "self docs: docs/map/files-1.md:" in l]
        self.assertEqual(len(recorded), 1, shown)  # the evidence names the line and is a failed run
        self.assertIn("exit 1", recorded[0])
        self.write_shard(self.original)
        r = self.fm("check", "--affected", "--json", check=False)
        self.assertEqual((json.loads(r.stdout)["exit"], r.returncode), (0, 0), r.stdout + r.stderr)

    def test_self_docs_is_a_json_field_on_the_affected_check(self):
        self.write_shard(self.original + "- " + "x" * 2100 + "\n")
        r = self.fm("check", "--affected", "--json", check=False)
        self.assertEqual(json.loads(r.stdout)["self_docs"]["status"], "FAIL", r.stdout)
        self.write_shard(self.original)
        r = self.fm("check", "--affected", "--json", check=False)
        self.assertEqual(json.loads(r.stdout)["self_docs"]["status"], "PASS", r.stdout)
