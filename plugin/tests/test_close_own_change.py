"""T-4625: a close's linked-test set comes from the task's own change. A task that is on the trunk (a `Merge T-ID:` commit,
or commits whose subject ends `(T-ID)`) is judged by the diff those commits brought in (first parent to commit), plus
what is still uncommitted and what it touched or claims, not by everything other lanes landed since its recorded base
(T-4607: about 480 modules for a change to two test files and helpers.py). A task nothing landed is selected as before."""
# map-links: plugin/lib/sampo/cli.py, plugin/lib/sampo/core.py
import os
import shutil
import subprocess
import unittest

from helpers import ForemanTestCase

from sampo import cli
from sampo import core as c

HERE = os.path.dirname(os.path.abspath(__file__))
OTHERS = [f"m{n}" for n in range(6)]  # the modules another lane lands code in
TEST = ("import os, sys, unittest\nsys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))\nimport {mod}\n\n\n"
        "class T(unittest.TestCase):\n    def test_it(self):\n        open({count!r}, 'a').write('{mod}\\n')\n"
        "        self.assertEqual({mod}.VALUE, {mod}.VALUE)\n")


class OwnChange(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.count = os.path.join(self.tmp, "count")  # outside the repo: a run must not change the tree it's judged on
        os.makedirs(os.path.join(self.repo, "tests"))
        shutil.copy(os.path.join(HERE, "run.py"), os.path.join(self.repo, "tests", "run.py"))
        for mod in ["calc", "extra", *OTHERS]:
            self.write(f"{mod}.py", "VALUE = 1\n")
            self.write(f"tests/test_{mod}.py", TEST.format(mod=mod, count=self.count))
        self.write(".gitignore", "__pycache__/\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "suite")
        self.main = self.git("rev-parse", "--abbrev-ref", "HEAD").strip()
        self.fm("check", "add", "python3 tests/run.py")
        self.fm("check", "affected", "python3 tests/run.py {names}")
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

    def merge(self, tid, title, files, base):
        """Commit `files` on a branch from `base` and merge it into the main branch the way a lane merge does."""
        self.git("checkout", "-q", "-b", f"lane-{tid}", base)
        for f in files:
            self.write(f, f"VALUE = 1\n# {tid}\n")
        self.git("add", "-A")
        self.git("commit", "-qm", f"{tid} work")
        self.git("checkout", "-q", self.main)
        self.git("merge", "--no-ff", "-qm", f"Merge {tid}: {title}", f"lane-{tid}")

    def direct(self, tid, title, files):
        """The files committed straight onto the main branch, the title ending (T-ID), as a close's commit does."""
        for f in files:
            self.write(f, f"VALUE = 1\n# {tid}\n")
        self.git("add", "-A")
        self.git("commit", "-qm", f"{title} ({tid})")

    def task(self, scope="calc.py"):
        tid = self.fm_json("task", "new", "Change calc", "--type", "FEATURE", "--tier", "S", "--ac", "works :: true",
                           "--step", "s", "--scope", scope)["id"]
        self.fm("focus", tid)
        return tid

    def chosen(self, tid):
        _, tests, changed = cli.affected(self.p, c.find_brief(self.p, tid))
        return sorted(os.path.basename(t) for t in tests), sorted(changed)

    def trunk_moved(self):
        """Task A starts; another lane's code lands as plain commits and as a merge: the trunk moved past A's base."""
        tid = self.task()
        base = self.head()
        self.direct("T-0999", "Other lane", [f"{m}.py" for m in OTHERS[:4]])
        self.merge("T-0998", "Other merge", ["m4.py", "m5.py"], base)
        return tid, base

    def test_a_landed_task_is_selected_by_its_merge_not_by_the_code_other_lanes_landed_since_its_base(self):
        tid, base = self.trunk_moved()
        self.merge(tid, "Change calc", ["calc.py"], base)
        tests, changed = self.chosen(tid)
        self.assertEqual(tests, ["test_calc.py"], tests)
        self.assertEqual(changed, ["calc.py"])

    def test_a_lane_that_landed_after_the_task_is_left_out_too(self):
        tid, base = self.trunk_moved()
        self.merge(tid, "Change calc", ["calc.py"], base)
        self.direct("T-0997", "Later lane", ["m3.py"])
        self.assertEqual(self.chosen(tid)[0], ["test_calc.py"])

    def test_a_task_committed_directly_is_selected_by_its_own_commits(self):
        tid, base = self.trunk_moved()
        self.direct(tid, "Change calc", ["calc.py"])
        self.direct("T-0997", "Later lane", ["m3.py"])
        self.assertEqual(self.chosen(tid)[0], ["test_calc.py"])

    def test_an_edit_after_the_merge_is_still_tested(self):
        tid, base = self.trunk_moved()
        self.merge(tid, "Change calc", ["calc.py"], base)
        self.write("calc.py", "VALUE = 1\n# hand edit after the merge\n")  # uncommitted, the task's file
        self.write("extra.py", "VALUE = 2\n")  # uncommitted: nobody else's, so this task's to answer for
        self.assertEqual(self.chosen(tid)[0], ["test_calc.py", "test_extra.py"])

    def test_a_committed_edit_after_the_merge_counts_when_the_task_names_the_file(self):
        tid = self.task("calc.py, extra.py")
        base = self.head()
        self.direct("T-0999", "Other lane", [f"{m}.py" for m in OTHERS[:4]])
        self.merge(tid, "Change calc", ["calc.py"], base)
        self.write("extra.py", "VALUE = 2\n")
        self.git("commit", "-qam", "hand edit after the merge")
        self.direct("T-0997", "Later lane", ["m3.py"])
        self.assertEqual(self.chosen(tid)[0], ["test_calc.py", "test_extra.py"])

    def test_a_task_nothing_landed_is_selected_from_its_base_as_before(self):
        tid, _ = self.trunk_moved()
        self.write("calc.py", "VALUE = 1\n# edit\n")
        self.assertEqual(self.chosen(tid)[0], sorted(f"test_{m}.py" for m in ["calc", *OTHERS[:4]]))  # the merge is left out

    def test_another_tasks_merge_titled_with_this_id_is_not_this_tasks_landing(self):
        tid, base = self.trunk_moved()
        self.merge("T-0997", f"Mentions {tid}", ["extra.py"], base)
        self.write("calc.py", "VALUE = 1\n# edit\n")
        self.assertEqual(self.chosen(tid)[0], sorted(f"test_{m}.py" for m in ["calc", *OTHERS[:4]]))

    def test_the_closing_run_runs_only_the_tasks_own_module(self):
        # other lanes land as merges here: the close's scope check (a separate path, T-3968) leaves those out, so the
        # finish gets as far as the linked tests; plain commits by others are covered by the selection tests above
        tid = self.task()
        base = self.head()
        self.merge("T-0998", "Other merge", ["m4.py", "m5.py"], base)
        self.merge(tid, "Change calc", ["calc.py"], base)
        r = self.fm("task", "finish", tid, "--audit", "self", "--run", "true", check=False,
                    env={"FOREMAN_SENTINEL": "off"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(self.count) as f:
            ran = f.read().split()
        self.assertEqual(ran, ["calc"], ran)


if __name__ == "__main__":
    unittest.main()
