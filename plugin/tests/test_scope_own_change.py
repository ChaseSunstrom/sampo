"""T-4628: the scope check at a close (cli._task_files, then _scope_gaps) judges a task the trunk already holds by its own
landing (core.landed_change, T-4625) plus what is uncommitted, not by everything committed since its recorded base: a
task with no lane_branch meta was refused as 'edited outside scope' for the files other tasks committed straight onto
the trunk (or merged) after it started. A stray file in its own landing is still named; a task nothing landed is judged
from its base as before."""
# map-links: plugin/lib/sampo/cli.py, plugin/lib/sampo/core.py
import os
import subprocess
import unittest

from helpers import ForemanTestCase

from sampo import cli
from sampo import core as c


class ScopeOwnChange(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        for f in ("a.py", "b.py", "c.py"):
            self.write(f, "VALUE = 1\n")
        self.write(".gitignore", "__pycache__/\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "suite")
        self.main = self.git("rev-parse", "--abbrev-ref", "HEAD").strip()
        self.p = c.find_project(self.repo)

    def git(self, *args):
        return subprocess.run(["git", "-C", self.repo, "-c", "user.name=t", "-c", "user.email=t@localhost", *args],
                              check=True, capture_output=True, text=True).stdout

    def write(self, rel, text):
        os.makedirs(os.path.dirname(os.path.join(self.repo, rel)), exist_ok=True)
        with open(os.path.join(self.repo, rel), "w") as f:
            f.write(text)

    def head(self):
        return self.git("rev-parse", "HEAD").strip()

    def task(self, scope="a.py"):
        tid = self.fm_json("task", "new", "Change a", "--type", "FEATURE", "--tier", "S", "--ac", "works :: true",
                           "--step", "s", "--scope", scope)["id"]
        self.fm("focus", tid)
        return tid, self.head()

    def merge(self, tid, files, base):
        self.git("checkout", "-q", "-b", f"lane-{tid}", base)
        for f in files:
            self.write(f, f"VALUE = 1\n# {tid}\n")
        self.git("add", "-A")
        self.git("commit", "-qm", f"{tid} work")
        self.git("checkout", "-q", self.main)
        self.git("merge", "--no-ff", "-qm", f"Merge {tid}: change", f"lane-{tid}")

    def direct(self, tid, files):
        for f in files:
            self.write(f, f"VALUE = 1\n# {tid}\n")
        self.git("add", "-A")
        self.git("commit", "-qm", f"Other work ({tid})")

    def gaps(self, tid):
        b = c.find_brief(self.p, tid)
        touches = cli._task_files(self.p, b)[0]
        return cli._scope_gaps(self.p, b, touches), sorted(touches)

    def test_another_tasks_direct_commit_is_no_scope_gap_for_a_landed_task(self):
        tid, base = self.task()
        self.direct("T-0999", ["b.py"])
        self.merge(tid, ["a.py"], base)
        gaps, touched = self.gaps(tid)
        self.assertEqual(gaps, [], gaps)
        self.assertNotIn("b.py", touched)

    def test_another_tasks_merge_is_no_scope_gap_either(self):
        tid, base = self.task()
        self.merge("T-0998", ["b.py"], base)
        self.direct(tid, ["a.py"])
        self.direct("T-0997", ["c.py"])  # landed after the task: not its edit
        gaps, touched = self.gaps(tid)
        self.assertEqual(gaps, [], gaps)
        self.assertNotIn("c.py", touched)

    def test_a_stray_file_in_its_own_landing_is_still_named(self):
        tid, base = self.task()
        self.direct("T-0999", ["b.py"])
        self.merge(tid, ["a.py", "c.py"], base)
        gaps, _ = self.gaps(tid)
        self.assertEqual(len(gaps), 1, gaps)
        self.assertIn("c.py", gaps[0])
        self.assertNotIn("b.py", gaps[0])

    def test_an_uncommitted_stray_after_the_landing_is_still_named(self):
        tid, base = self.task()
        self.direct("T-0999", ["b.py"])
        self.merge(tid, ["a.py"], base)
        self.write("c.py", "VALUE = 2\n")
        gaps, _ = self.gaps(tid)
        self.assertEqual(len(gaps), 1, gaps)
        self.assertIn("c.py", gaps[0])
        self.assertNotIn("b.py", gaps[0])

    def test_a_task_nothing_landed_is_judged_from_its_base_as_before(self):
        tid, base = self.task()
        self.direct("T-0999", ["b.py"])
        self.write("a.py", "VALUE = 2\n")
        gaps, touched = self.gaps(tid)
        self.assertIn("b.py", touched)  # as before: the plain commit since the base is counted
        self.assertEqual(len(gaps), 1, gaps)
        self.assertIn("b.py", gaps[0])

    def test_another_tasks_commit_titled_with_this_id_is_not_this_tasks_landing(self):
        tid, base = self.task()
        self.direct("T-0999", ["b.py"])
        self.merge("T-0997", ["c.py"], base)
        self.git("commit", "-q", "--amend", "-m", f"Merge T-0997: mentions {tid}")
        self.write("a.py", "VALUE = 2\n")
        _, touched = self.gaps(tid)
        self.assertIn("b.py", touched)  # still unlanded: judged from its base


if __name__ == "__main__":
    unittest.main()
