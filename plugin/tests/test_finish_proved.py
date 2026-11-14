#!/usr/bin/env python3
# T-2222: a close that changed a path the repo's .fmproved lists needs a red→green proof, whatever the task's type
import os
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace

from helpers import ForemanTestCase  # noqa: F401  (puts plugin/lib on sys.path)
from sampo import cli
from sampo import core as c

RED = "python3 plugin/tests/run.py test_exam"


def brief(type_="FEATURE", red=False, regression=""):
    b = c.Brief.new("T-0001", "a score change", type_, "S", raw="x")
    if red:  # the test failed first; the close's run (then) is its green
        b.add_evidence(RED, "✗ exit 1 · FAILED (1 classes)", ac=1, ran=True)
    if regression:
        b.set_section("Regression test", regression)
    return b


def gaps(changed, b, listed="# scores\nplugin/lib/fmexam.py\nsite/*.py\n", then=()):
    with tempfile.TemporaryDirectory() as root:
        if listed is not None:
            with open(os.path.join(root, ".fmproved"), "w") as f:
                f.write(listed)
        with mock.patch.object(cli.c, "git_root", return_value=root), \
                mock.patch.object(cli.c, "task_base", return_value="abc"), \
                mock.patch("sampo.map.changed", return_value=changed), \
                mock.patch.object(cli, "_theirs", return_value=[]):  # T-2791: no other lane's merge in this unit
            return cli._proved_gaps(SimpleNamespace(root=root, lane=None), b, then=list(then))


class ProvedPaths(unittest.TestCase):
    def test_a_listed_path_without_a_proof_is_refused(self):
        (g,) = gaps(["plugin/lib/fmexam.py"], brief())
        self.assertIn("plugin/lib/fmexam.py is listed in .fmproved", g)
        self.assertIn("red→green", g)

    def test_a_glob_matches_and_names_what_it_matched(self):
        self.assertIn("site/pages.py", gaps(["site/pages.py", "README.md"], brief())[0])

    def test_a_red_then_green_or_a_regression_note_satisfies_it(self):
        self.assertEqual(gaps(["plugin/lib/fmexam.py"], brief(red=True), then=[RED]), [])
        self.assertEqual(gaps(["plugin/lib/fmexam.py"], brief(regression="none: docs-only wording")), [])

    def test_unlisted_paths_no_file_and_fix_tasks_are_left_alone(self):
        self.assertEqual(gaps(["plugin/lib/fmdoctor.py"], brief()), [])
        self.assertEqual(gaps(["plugin/lib/fmexam.py"], brief(), listed=None), [])
        self.assertEqual(gaps(["plugin/lib/fmexam.py"], brief("FIX")), [])  # FIX has its own red→green gate


if __name__ == "__main__":
    unittest.main()


class FinishAfterARedGate(ForemanTestCase):  # T-2526: T-2334 closed and committed right after its full gate failed
    def test_a_red_full_check_on_this_tree_refuses_the_close_and_a_green_one_after_it_lets_it_go(self):
        import json
        self.fm("init")
        p = c.find_project(self.repo)
        tid = json.loads(self.fm("task", "new", "Close me", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true",
                                 "--step", "s", "--json").stdout)["id"]
        self.fm("focus", tid)
        tree = c.worktree_id(self.repo)
        c.log_event(p, "check_run", data={"tree": tree, "results": [
            {"cmd": "python3 plugin/tests/run.py", "exit": 1, "failing": ["test_proof.Proof"]}]})
        r = self.fm("task", "finish", tid, "--run", "true", "--audit", "self check", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("python3 plugin/tests/run.py", r.stdout + r.stderr)
        self.assertIn("test_proof.Proof", r.stdout + r.stderr)
        self.assertNotEqual(c.find_brief(p, tid).status, "done")
        c.log_event(p, "check_run", data={"tree": tree, "results": [{"cmd": "python3 plugin/tests/run.py", "exit": 0}]})
        self.fm("task", "finish", tid, "--run", "true", "--audit", "self check")
        self.assertEqual(c.find_brief(p, tid).status, "done")
