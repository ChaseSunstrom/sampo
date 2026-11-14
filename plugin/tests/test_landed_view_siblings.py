"""T-4632: the close's Landed line (cli._landed) and the owed-companions check (cli._owed_companions) judge a task the
trunk already holds by its own landing (core.landed_change, as T-4625 did for the linked tests and T-4628 for the scope
check), not by base..tree, which also holds every commit other tasks put on the trunk since the base. A task nothing
landed is judged from its recorded base as before."""
# map-links: plugin/lib/sampo/cli.py, plugin/lib/sampo/core.py
import os
import subprocess
import unittest
from unittest import mock

from helpers import ForemanTestCase

from sampo import cli
from sampo import core as c
from sampo import shape


class Base(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        for f in ("a.py", "b.py"):
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

    def brief(self, tid):
        return c.find_brief(self.p, tid)


class LandedLine(Base):
    def sha(self, *files):
        return self.git("log", "-1", "--format=%h", "--", *files).strip()

    def test_a_later_commit_of_another_task_is_not_the_landed_sha(self):
        tid, base = self.task()
        self.merge(tid, ["a.py"], base)
        self.direct("T-0999", ["b.py"])  # landed after the task: not its change
        line = cli._landed(self.p, self.brief(tid))
        self.assertIn(f"committed {self.sha('a.py')}", line)
        self.assertNotIn(self.sha("b.py"), line)

    def test_another_tasks_commit_before_the_landing_is_not_counted_either(self):
        tid, base = self.task()
        self.direct("T-0999", ["b.py"])
        self.merge(tid, ["a.py"], base)
        line = cli._landed(self.p, self.brief(tid))
        self.assertIn(f"committed {self.sha('a.py')}", line)
        self.assertNotIn(self.sha("b.py"), line)

    def test_an_uncommitted_edit_after_the_landing_is_still_counted(self):
        tid, base = self.task()
        self.merge(tid, ["a.py"], base)
        self.direct("T-0999", ["b.py"])
        self.write("a.py", "VALUE = 2\n")
        self.assertEqual(cli._landed(self.p, self.brief(tid)), "Landed: not committed (1 file uncommitted)")

    def test_a_task_nothing_landed_is_judged_from_its_base_as_before(self):
        tid, base = self.task()
        self.write("a.py", "VALUE = 2\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "plain commit")  # no task id on it: not a landing
        self.direct("T-0999", ["b.py"])
        line = cli._landed(self.p, self.brief(tid))
        self.assertIn(f"committed {self.sha('a.py', 'b.py')}", line)  # as before: b.py counts, the newest sha shows

    def test_a_task_with_no_change_says_so(self):
        tid, _ = self.task()
        self.assertEqual(cli._landed(self.p, self.brief(tid)), "Landed: no file changes in git")


class OwedCompanions(Base):
    """reruns: a Base fixture (repo, task, landing helpers), not another test class's tests."""

    OWED = [{"template": "tests/test_<stem>.py"}]

    def owed(self, tid, outside=("tests/test_n.py",)):
        with mock.patch.object(shape, "owed", return_value=self.OWED):
            return cli._owed_companions(self.p, self.brief(tid), list(outside))

    def test_a_file_another_task_added_since_the_base_is_not_this_tasks_to_accompany(self):
        tid, _ = self.task("n.py")
        self.direct("T-0999", ["n.py"])  # another task adds the file named in scope
        self.direct(tid, ["n.py"])  # the landing only changes it
        self.assertEqual(self.owed(tid), set())

    def test_a_file_its_own_landing_adds_owes_its_companions(self):
        tid, _ = self.task("n.py")
        self.direct("T-0999", ["b.py"])
        self.direct(tid, ["n.py"])
        self.assertEqual(self.owed(tid), {"tests/test_n.py"})

    def test_a_task_nothing_landed_owes_from_its_base_as_before(self):
        tid, _ = self.task("n.py")
        self.write("n.py", "VALUE = 1\n")
        self.assertEqual(self.owed(tid), {"tests/test_n.py"})

    def test_a_plain_commit_of_the_file_by_another_task_is_no_landing_so_the_base_rules(self):
        tid, _ = self.task("n.py")
        self.direct("T-0999", ["n.py"])  # plain commit, not a landing: judged from the base, as before
        self.assertEqual(self.owed(tid), {"tests/test_n.py"})


if __name__ == "__main__":
    unittest.main()
