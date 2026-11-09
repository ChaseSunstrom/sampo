"""T-3411: a criterion verify written `python3 -m unittest test_X.Class` only imports from plugin/tests; wherever fm runs a
stored verify (lane merge, finish, re-verify) it runs from the repo root, so run_command rewrites the bare form."""
# map-links: plugin/lib/sampo/core.py plugin/lib/sampo/cli.py plugin/lib/sampo/lanes.py
import os
import subprocess
import unittest
from unittest import mock

from helpers import ForemanTestCase

from sampo import core as c


def _touch(root, rel, text=""):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


class UnittestForm(ForemanTestCase):
    def test_rewritten_to_run_py_when_the_repo_has_one(self):
        _touch(self.repo, "plugin/tests/run.py")
        _touch(self.repo, "plugin/tests/test_exam.py")
        _touch(self.repo, "plugin/tests/test_x.py")
        cwd, cmd, note = c.normalize_verify(self.repo, "python3 -m unittest test_exam.Lean test_x")
        self.assertEqual((cwd, cmd), (self.repo, "python3 plugin/tests/run.py test_exam.Lean test_x"))
        self.assertIn("plugin/tests/run.py", note)

    def test_dotted_path_and_k_forms(self):
        for f in ("run", "test_doctor", "test_bus", "test_lock_wait", "test_g"):
            _touch(self.repo, f"plugin/tests/{f}.py")
        run = "python3 plugin/tests/run.py "
        for cmd, want in (
                ("python3 -m unittest plugin.tests.test_doctor -k worktree", run + "test_doctor -k worktree"),
                ("python3 -m unittest plugin/tests/test_bus.py -k closing_a_task", run + "test_bus -k closing_a_task"),
                ("python3 -m unittest plugin.tests.test_lock_wait", run + "test_lock_wait"),
                ("python3 -m unittest plugin.tests.test_g.Review.test_env", run + "test_g.Review.test_env"),
                ("python3 -m unittest test_doctor -k a -k 'b'", run + "test_doctor -k a -k b")):
            self.assertEqual(c.normalize_verify(self.repo, cmd)[1], want, cmd)
        for cmd in ("python3 -m unittest discover -s plugin/tests -k x", "python3 -m unittest plugin.tests.test_doctor -v",
                    "python3 -m unittest plugin/tests/test_bus -k x", "python3 -m unittest test_doctor.py",
                    "python3 -m unittest plugin.tests.test_doctor -k x; true", "python3 -m unittest -k x"):
            self.assertEqual(c.normalize_verify(self.repo, cmd), (self.repo, cmd, None), cmd)

    def test_runs_from_the_tests_dir_when_there_is_no_run_py(self):
        _touch(self.repo, "tests/test_a.py")
        cwd, cmd, note = c.normalize_verify(self.repo, "python3 -m unittest test_a.A.test_one")
        self.assertEqual((cwd, cmd), (os.path.join(self.repo, "tests"), "python3 -m unittest test_a.A.test_one"))
        self.assertIn("tests", note)

    def test_untouched_forms(self):
        _touch(self.repo, "plugin/tests/run.py")
        _touch(self.repo, "test_root.py")
        for cmd in ("python3 -m unittest test_a\ntest_b", "FOO=1 python3 -m unittest test_a",
                    "python3 -m unittest test_a; echo x", "python3 -m unittest test_root", "python3 -m unittest discover", "python3 -m unittest -v test_a",
                    "python3 -m unittest test_a && echo", "python3 plugin/tests/run.py test_a", "pytest test_a"):
            self.assertEqual(c.normalize_verify(self.repo, cmd), (self.repo, cmd, None), cmd)

    def test_other_project_without_a_tests_dir_is_untouched(self):
        cmd = "python3 -m unittest test_zzz"
        self.assertEqual(c.normalize_verify(self.repo, cmd), (self.repo, cmd, None))

    def test_run_command_uses_the_tests_dir(self):
        _touch(self.repo, "tests/test_ok.py",
               "import unittest\nclass T(unittest.TestCase):\n    def test_a(self): pass\n")
        code, out = c.run_command(self.repo, "python3 -m unittest test_ok.T")
        self.assertEqual(code, 0, out)

    def test_first_error_line(self):
        out = "x\nERROR: test_exam (unittest.loader._FailedTest)\nTraceback\nFAILED (errors=1)\n"
        self.assertEqual(c.first_error_line(out), "ERROR: test_exam (unittest.loader._FailedTest)")
        self.assertEqual(c.first_error_line("all fine\n"), "")


class AcNote(ForemanTestCase):
    def test_ac_add_and_edit_print_a_note(self):
        self.fm("init")
        for f in ("run", "test_a", "test_b"):
            _touch(self.repo, f"plugin/tests/{f}.py")
        tid = self.fm_json("task", "new", "T", "--type", "FIX", "--tier", "S", "--ac", "ok :: true", "--step", "s")["id"]
        add = self.fm("task", "ac", tid, "add", "x", "--verify", "python3 -m unittest test_a.A", check=False)
        self.assertIn("plugin/tests/run.py", add.stderr)
        edit = self.fm("task", "ac", tid, "edit", "1", "--verify", "python3 -m unittest test_b", check=False)
        self.assertIn("plugin/tests/run.py", edit.stderr)


class MergeRefusal(ForemanTestCase):
    def test_refusal_names_the_command_and_its_first_error_line(self):
        from sampo import lanes as slanes
        out = "..\nERROR: test_exam (unittest.loader._FailedTest)\nTraceback\nFAILED (errors=1)\n"
        b = mock.Mock(id="T-1", meta={"lane": self.repo})
        b.verify_cmds.return_value = [(1, "python3 -m unittest test_exam.X")]
        with mock.patch.object(c, "run_command", return_value=(1, out)), mock.patch.object(c, "wait_for_load"):
            with self.assertRaises(c.PolicyError) as e:
                slanes._verify(b)
        self.assertIn("python3 -m unittest test_exam.X", str(e.exception))
        self.assertIn("first error: ERROR: test_exam (unittest.loader._FailedTest)", str(e.exception))


class MainPathInALane(ForemanTestCase):
    """T-4033: a verify naming the main checkout by absolute path judged a lane by main (T-3133, T-3134: red after
    merge until the lane landed, so the land parked it); in a linked worktree it reads that tree's own copy."""

    def setUp(self):
        super().setUp()
        _touch(self.repo, "CHANGELOG.md", "old\n")
        subprocess.run(["git", "-C", self.repo, "add", "CHANGELOG.md"], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", "log"], check=True)
        self.lane = os.path.join(self.tmp, "app.lanes", "T-1")
        subprocess.run(["git", "-C", self.repo, "worktree", "add", "-q", "-b", "lane", self.lane], check=True)
        _touch(self.lane, "CHANGELOG.md", "old\nthe lane's line (T-1)\n")
        _touch(self.repo, "state/x.json", "{}")  # main's own untracked state: no copy in the lane

    def test_a_lane_runs_a_main_path_check_against_its_own_tree(self):
        code, _ = c.run_command(self.lane, f"grep -q 'T-1' {self.repo}/CHANGELOG.md")
        self.assertEqual(code, 0)
        code, _ = c.run_command(self.lane, f"git -C {self.repo} rev-parse --abbrev-ref HEAD | grep -qx lane")
        self.assertEqual(code, 0)

    def test_only_paths_this_tree_has_are_rewritten(self):
        for cmd, want in ((f"cat {self.repo}/state/x.json", f"cat {self.repo}/state/x.json"),
                          (f"ls {self.repo}.lanes", f"ls {self.repo}.lanes"),
                          (f"test -s '{self.repo}/CHANGELOG.md'", f"test -s '{self.lane}/CHANGELOG.md'"),
                          (f"cd {self.repo} && true", f"cd {self.lane} && true")):
            self.assertEqual(c.normalize_verify(self.lane, cmd)[1], want, cmd)
        cmd = f"grep -q x {self.repo}/CHANGELOG.md"
        self.assertEqual(c.normalize_verify(self.repo, cmd), (self.repo, cmd, None))  # main runs it as written


if __name__ == "__main__":
    unittest.main()
