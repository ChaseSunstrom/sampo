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

    def test_a_lane_task_with_no_start_commit_answers_from_the_merge_base_with_main(self):
        """T-4908: a hand-made lane's task had no base, so --affected counted from HEAD (nothing changed) and a change to
        fmguard.py passed its lane check on 4 classes"""
        from sampo import cli
        p = c.find_project(self.lane, create=False)
        b = c.active_brief(c.load_briefs(p), p.lane)
        for k in ("base", "base_tree"):
            b.meta.pop(k, None)
        c.save_brief(p, b)
        _, tests, changed = cli.affected(p)
        self.assertIn("src/mine.py", changed)
        self.assertIn("tests/test_mine.py", tests)
        self.assertNotIn("src/theirs.py", changed)

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


class AffectedFixture(ForemanTestCase):
    """A project with two source files and a test each, a focused task, and the commit helper."""
    commit = AffectedLaneBase.commit

    def setUp(self):
        super().setUp()
        self.fm("init")
        self.fm("check", "affected", "python3 -m pytest -q {tests}")
        self.commit(self.repo, {".gitignore": ".venv/\n", "src/mine.py": "N = 1\n", "src/other.py": "N = 1\n",
                                "tests/test_old.py": "# map-links: src/other.py\nN = 1\n",
                                "tests/test_moved.py": "# map-links: src/other.py\nN = 1\n" * 3,
                                "tests/test_mine.py": "# map-links: src/mine.py\nN = 1\n"}, "seed")
        self.fm("task", "new", "edit", "--type", "FIX", "--tier", "S", "--ac", "a :: true", "--step", "s", "--focus")
        self.p = c.find_project(self.repo, create=False)


class AffectedDeletedTests(AffectedFixture):
    """T-4149: a test file the diff deleted is no test to run (pytest exits 4 on a path that isn't there, and the
    affected run could never pass); a renamed one is run under its new name."""

    def test_a_deleted_test_file_is_not_listed_and_the_rest_still_run(self):
        from sampo import cli
        os.remove(os.path.join(self.repo, "tests", "test_old.py"))
        with open(os.path.join(self.repo, "src", "mine.py"), "w") as f:
            f.write("N = 2\n")
        _, tests, changed = cli.affected(self.p)
        self.assertIn("tests/test_old.py", changed, "the deletion is a change")
        self.assertNotIn("tests/test_old.py", tests)
        self.assertEqual(tests, ["tests/test_mine.py"])

    def test_a_deleted_test_alone_leaves_nothing_to_run_not_a_failing_command(self):
        from sampo import cli
        os.remove(os.path.join(self.repo, "tests", "test_old.py"))
        cmd, tests, _ = cli.affected(self.p)
        self.assertEqual(tests, [])
        self.assertIsNone(cmd)

    def test_deleted_old_name_of_a_renamed_test_file_is_dropped_and_the_new_one_runs(self):
        from sampo import cli
        os.rename(os.path.join(self.repo, "tests", "test_moved.py"), os.path.join(self.repo, "tests", "test_renamed.py"))
        cmd, tests, _ = cli.affected(self.p)
        self.assertEqual(tests, ["tests/test_renamed.py"])
        self.assertNotIn("test_moved", cmd)

    def test_a_committed_deleted_test_and_rename_are_followed_too(self):
        from sampo import cli
        subprocess.run(["git", "-C", self.repo, "rm", "-q", "tests/test_old.py"], check=True)
        subprocess.run(["git", "-C", self.repo, "mv", "tests/test_moved.py", "tests/test_renamed.py"], check=True)
        subprocess.run(["git", "-C", self.repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "tidy"],
                       check=True, capture_output=True)
        _, tests, _ = cli.affected(self.p)
        self.assertEqual(tests, ["tests/test_renamed.py"])

    def test_a_deleted_test_is_named_as_left_out_not_as_one_the_runner_cant_run(self):
        # T-4155: the old note called it "can't run … run their own suite", the wrong reason
        os.remove(os.path.join(self.repo, "tests", "test_old.py"))
        with open(os.path.join(self.repo, "src", "mine.py"), "w") as f:
            f.write("N = 2\n")
        r = self.fm("check", "--affected", "--json", check=False)
        d = json.loads(r.stdout)
        self.assertEqual(d["deleted_tests"], ["tests/test_old.py"])
        self.assertEqual(d["unrunnable"], [])
        text = self.fm("check", "--affected", check=False).stdout
        self.assertIn("1 deleted test file(s) left out of the command: tests/test_old.py", text)
        self.assertNotIn("can't run", text)

    def test_the_lock_counts_and_slices_only_the_test_files_that_exist(self):
        # T-4155: a deleted file took a LOCK_MODULES slot and was counted as linked
        from sampo import cli
        os.remove(os.path.join(self.repo, "tests", "test_old.py"))
        info = {}
        old, cli.LOCK_MODULES = cli.LOCK_MODULES, 1
        try:
            _, tests, _ = cli.affected(self.p, only=["src/other.py"], lock=info)
        finally:
            cli.LOCK_MODULES = old
        self.assertEqual(tests, ["tests/test_moved.py"], "the one slot goes to a file that exists")
        self.assertEqual(info["linked"], 1)

    def test_the_affected_run_of_a_deleted_test_passes(self):
        self.fm("check", "affected", "true {tests}")  # a runner that passes whatever file it is handed, if it exists
        os.remove(os.path.join(self.repo, "tests", "test_old.py"))
        with open(os.path.join(self.repo, "src", "mine.py"), "w") as f:
            f.write("N = 2\n")
        r = self.fm("check", "--affected", "--json", check=False)
        self.assertEqual(json.loads(r.stdout)["tests"], ["tests/test_mine.py"], r.stdout)


class AffectedVenv(AffectedFixture):
    """T-4150: the linked tests run with the project's own interpreter: its .venv's bin first on PATH, so a module only
    the venv has is found (12 refusals: 'a module is missing' while the project's venv held it)."""

    def stub(self, root):
        """A .venv whose python3 and pytest only leave a mark, so the test sees which interpreter the run used."""
        mark = os.path.join(self.tmp, "venv-mark")
        for name in ("python3", "python", "pytest"):
            path = os.path.join(root, ".venv", "bin", name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(f'#!/bin/sh\necho "{name} $VIRTUAL_ENV" >> {mark}\nexit 0\n')
            os.chmod(path, 0o755)
        return mark

    def edit(self, root):
        with open(os.path.join(root, "tests", "test_mine.py"), "a") as f:
            f.write("N = 2\n")

    def test_the_linked_tests_run_with_the_projects_venv(self):
        mark = self.stub(self.repo)
        self.edit(self.repo)
        r = self.fm("check", "--affected", "--json", check=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(mark) as f:
            self.assertIn(os.path.join(self.repo, ".venv"), f.read(), "the run used the venv's python3")
        self.assertEqual(json.loads(r.stdout)["venv"], ".venv")

    def test_no_venv_keeps_the_interpreter_on_path(self):
        from sampo import cli
        self.assertEqual(cli._venv_env(self.repo, {"PATH": "/usr/bin"}), ({"PATH": "/usr/bin"}, None))

    def test_a_venv_already_active_is_left_alone(self):
        from sampo import cli
        self.stub(self.repo)
        env = {"PATH": os.path.join(self.repo, ".venv", "bin") + os.pathsep + "/usr/bin",
               "VIRTUAL_ENV": os.path.join(self.repo, ".venv")}
        self.assertEqual(cli._venv_env(self.repo, env), (env, None))

    def test_a_lane_without_its_own_venv_uses_the_main_checkouts(self):
        """A lane is a worktree: untracked folders (.venv) are not copied there, so its tests found no module."""
        mark = self.stub(self.repo)
        lane = os.path.join(self.tmp, "lane")
        subprocess.run(["git", "-C", self.repo, "worktree", "add", "-q", "-b", "lane", lane], check=True,
                       capture_output=True)
        self.fm("task", "new", "lane work", "--type", "FIX", "--tier", "S", "--ac", "a :: true", "--step", "s",
                "--focus", cwd=lane)
        self.edit(lane)
        r = self.fm("check", "--affected", "--json", cwd=lane, check=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(mark) as f:
            self.assertIn(os.path.join(self.repo, ".venv"), f.read())
        self.assertEqual(json.loads(r.stdout)["venv"], "the main checkout's .venv")  # T-4155: named, not "the project's"
        self.assertIn("the main checkout's .venv", self.fm("check", "--affected", cwd=lane, check=False).stdout)

    def lane(self, name="lane"):
        lane = os.path.join(self.tmp, name)
        subprocess.run(["git", "-C", self.repo, "worktree", "add", "-q", "-b", name, lane], check=True,
                       capture_output=True)
        self.fm("task", "new", "lane work", "--type", "FIX", "--tier", "S", "--ac", "a :: true", "--step", "s",
                "--focus", cwd=lane)
        return lane

    def test_a_lanes_edit_is_what_the_main_checkouts_editable_venv_imports(self):
        """T-4155: an editable install in the main checkout's venv points at the main checkout's src; the lane's own
        edit must still be what the linked tests import (before the fix the lane's break passed unseen)."""
        import sys
        self.fm("check", "affected", "python3 {tests}")
        self.commit(self.repo, {"src/pkg/mod.py": "VALUE = 1\n",
                                "tests/test_pkg.py": ("# map-links: src/pkg/mod.py\nimport sys\nimport pkg.mod\n"
                                                      "sys.exit(0 if pkg.mod.VALUE == 2 else 3)\n")}, "pkg")
        for name in ("python3", "python"):  # the editable install: main's src comes last on the path, as a .pth adds it
            path = os.path.join(self.repo, ".venv", "bin", name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(f'#!/bin/sh\nexport PYTHONPATH="$PYTHONPATH:{self.repo}/src"\nexec {sys.executable} "$@"\n')
            os.chmod(path, 0o755)
        lane = self.lane()
        with open(os.path.join(lane, "src", "pkg", "mod.py"), "w") as f:
            f.write("VALUE = 2\n")
        r = self.fm("check", "--affected", "--json", cwd=lane, check=False)
        self.assertEqual(r.returncode, 0, "the lane's VALUE = 2 is what the test imported: " + r.stdout + r.stderr)
        self.assertEqual(json.loads(r.stdout)["venv"], "the main checkout's .venv")
        with open(os.path.join(lane, "src", "pkg", "mod.py"), "w") as f:
            f.write("VALUE = 3\n")  # the lane breaks it: the test must see that, not main's VALUE = 1
        self.assertEqual(self.fm("check", "--affected", cwd=lane, check=False).returncode, 1)

    def test_a_sampo_trees_own_stdlib_runner_ignores_any_venv(self):
        """T-4155: plugin/tests/run.py is Sampo testing itself: a .venv beside it must not change which python runs it."""
        from sampo import cli
        self.stub(self.repo)
        os.makedirs(os.path.join(self.repo, "plugin", "lib", "sampo"))  # the marker of a Sampo tree
        env = {"PATH": "/usr/bin"}
        self.assertEqual(cli._venv_env(self.repo, env, "python3 plugin/tests/run.py test_mine"), (env, None))
        got, which = cli._venv_env(self.repo, env, "python3 -m pytest -q tests/test_mine.py")
        self.assertEqual(which, ".venv", "a pytest template in that tree still takes the venv")
        self.assertEqual(cli._venv_env(self.repo, env, None)[1], ".venv")

    def test_the_close_runs_the_linked_tests_with_the_projects_venv(self):
        """T-4155: the refusal 'the linked tests couldn't run here (a module is missing)' was the close's own run, which
        ran under the PATH python; it takes the venv too."""
        mark = self.stub(self.repo)
        self.edit(self.repo)
        import types
        from sampo import cli
        args = types.SimpleNamespace(timeout=0, json=True, skip_affected=None, timings=[])
        cli._close_affected(self.p, c.active_brief(c.load_briefs(self.p), self.p.lane), {}, args)
        self.assertTrue(args.timings, "the close ran the linked tests")
        with open(mark) as f:
            self.assertIn(os.path.join(self.repo, ".venv"), f.read())


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
