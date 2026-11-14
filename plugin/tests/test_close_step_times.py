"""T-4600: every close says where its seconds went (a `Close steps` line before the Landed line, a `close_steps` ledger
event), a close with a full gate pass on record for the same code tree skips the linked tests and says so, a changed
tree still runs them, and a state-index file a test once leaked into plugin/tests/ no longer moves the code tree (it
made three serial closes of one land rerun 8 to 18 minutes of tests each: "1 file(s) differ: plugin/tests/briefs-closed.json")."""
import os
import re
import shutil
import subprocess
import unittest
from unittest import mock

from helpers import ForemanTestCase

from sampo import cli
from sampo import core as c
from sampo import train

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
SRC = "def add(a, b):\n    return a + b\n"
STEPS = re.compile(r"^Close steps \((\d+(?:\.\d)?) s\): (.+)$", re.M)


class CloseStepTimes(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.count = os.path.join(self.tmp, "count")  # outside the repo: a run must not change the tree it's judged on
        os.makedirs(os.path.join(self.repo, "tests"))
        shutil.copy(os.path.join(HERE, "run.py"), os.path.join(self.repo, "tests", "run.py"))
        self.write("calc.py", SRC)
        self.write("tests/test_calc.py",
                   "import os, sys, unittest\nsys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))\n"
                   "import calc\n\n\nclass Calc(unittest.TestCase):\n    def test_add(self):\n"
                   f"        open({self.count!r}, 'a').write('x\\n')\n        self.assertEqual(calc.add(1, 2), 3)\n")
        self.write(".gitignore", "__pycache__/\n")
        subprocess.run(["git", "-C", self.repo, "add", "-A"], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", "suite"], check=True)
        self.fm("check", "add", "python3 tests/run.py")
        self.fm("check", "affected", "python3 tests/run.py {names}")
        self.p = c.find_project(self.repo)

    def write(self, rel, text):
        os.makedirs(os.path.dirname(os.path.join(self.repo, rel)), exist_ok=True)
        with open(os.path.join(self.repo, rel), "w") as f:
            f.write(text)

    def runs(self):
        try:
            with open(self.count) as f:
                return len(f.read().split())
        except OSError:
            return 0

    def task(self):
        tid = self.fm_json("task", "new", "Change add", "--type", "FEATURE", "--tier", "S", "--ac", "works :: true",
                           "--step", "s", "--scope", "calc.py")["id"]
        self.fm("focus", tid)
        self.write("calc.py", SRC + "# tidy\n")
        return tid

    def finish(self, tid):
        r = self.fm("task", "finish", tid, "--audit", "self", "--run", "true", check=False,
                    env={"FOREMAN_SENTINEL": "off"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout

    def steps_of(self, out):
        m = STEPS.search(out)
        self.assertTrue(m, f"no 'Close steps' line in:\n{out}")
        return float(m.group(1)), {k: (float(v), note) for k, v, note in
                                   re.findall(r"(\w+) (\d+(?:\.\d)?) s(?: \(([^)]*)\))?", m.group(2))}

    def test_every_close_reports_its_per_step_seconds_before_the_landed_line_and_in_the_ledger(self):
        out = self.finish(self.task())
        total, steps = self.steps_of(out)
        for name in ("verify", "record", "affected", "done", "landed", "other"):
            self.assertIn(name, steps, out)
        self.assertLess(out.index("Close steps ("), out.index("Landed:"), "the Landed line stays the close's last")
        ev = c.ledger_events(self.p, ("close_steps",))[-1]
        self.assertGreaterEqual(ev["data"]["s"], 0)
        self.assertEqual(set(ev["data"]["steps"]), set(steps))
        self.assertAlmostEqual(sum(v for v, _ in steps.values()), total, delta=0.6 + 0.1 * len(steps))

    def test_the_linked_tests_ran_once_here_and_the_step_says_ran(self):
        out = self.finish(self.task())
        self.assertEqual(self.runs(), 1)
        self.assertEqual(self.steps_of(out)[1]["affected"][1], "ran")

    def test_a_full_pass_on_the_same_tree_skips_the_linked_tests_and_the_step_says_so(self):
        tid = self.task()
        self.fm("check", env={"FOREMAN_SENTINEL": "off"})  # the train's full pass on the merged code
        before = self.runs()
        out = self.finish(tid)
        self.assertEqual(self.runs(), before, "the linked tests ran again on code the gates just passed")
        self.assertIn("linked tests: covered by the gates' pass", out)
        self.assertEqual(self.steps_of(out)[1]["affected"][1], "covered")

    def test_a_changed_tree_still_runs_them_and_names_the_file(self):
        tid = self.task()
        self.fm("check", env={"FOREMAN_SENTINEL": "off"})
        self.write("scripts/stray.py", "x = 1\n")
        self.fm("task", "log", tid, "scope: a stray untracked file, not this task's")
        before = self.runs()
        out = self.finish(tid)
        self.assertEqual(self.runs(), before + 1)
        self.assertIn("1 file(s) differ: scripts/stray.py", out)
        self.assertEqual(self.steps_of(out)[1]["affected"][1], "ran")

    def test_a_state_index_file_in_an_ignored_path_leaves_the_pass_standing(self):
        # the leak: a gate run left plugin/tests/briefs-closed.json in the tree and every later close was "on another tree"
        self.write(".gitignore", "__pycache__/\nbriefs-*.json\n")
        subprocess.run(["git", "-C", self.repo, "commit", "-qam", "ignore the index files"], check=True)
        tid = self.task()
        self.fm("check", env={"FOREMAN_SENTINEL": "off"})
        self.write("briefs-closed.json", '{"v":1,"e":{}}')
        before = self.runs()
        out = self.finish(tid)
        self.assertEqual(self.runs(), before, out)
        self.assertIn("covered by the gates' pass", out)

    def test_this_repo_ignores_the_index_files_a_test_leaked_and_tracks_none(self):
        for f in ("plugin/tests/briefs-closed.json", "plugin/tests/briefs-index.json", "plugin/tests/briefs-open.json"):
            self.assertEqual(subprocess.run(["git", "-C", ROOT, "check-ignore", "-q", f]).returncode, 0, f)
        tracked = subprocess.run(["git", "-C", ROOT, "ls-files", "plugin/tests"], capture_output=True, text=True).stdout
        self.assertFalse([f for f in tracked.split() if os.path.basename(f).startswith("briefs-")], tracked)

    def test_a_project_without_a_state_folder_writes_no_index_into_the_caller_s_folder(self):
        # the leak itself: a Project whose dir is "" joins to a bare file name, so load_briefs wrote briefs-closed.json
        # into whatever folder the test ran from (plugin/tests): the file that moved the code tree
        old = os.getcwd()
        os.chdir(self.tmp)
        try:
            with mock.patch.object(c, "brief_paths", return_value=[]):
                c.load_briefs(c.Project("x", self.repo, ""))
        finally:
            os.chdir(old)
        self.assertEqual([f for f in os.listdir(self.tmp) if f.startswith("briefs-")], [])

    def test_a_failed_close_still_logs_where_its_time_went(self):
        tid = self.task()
        r = self.fm("task", "finish", tid, "--audit", "self", "--run", "false", check=False,
                    env={"FOREMAN_SENTINEL": "off"})
        self.assertNotEqual(r.returncode, 0)
        ev = c.ledger_events(self.p, ("close_steps",))[-1]
        self.assertIn("verify", ev["data"]["steps"])

    def test_the_trains_close_line_carries_the_close_steps(self):
        steps = "Close steps (12.5 s): verify 0.1 s, affected 9.0 s (ran), commit 3.2 s, other 0.2 s"
        out = f"Done.\n{steps}\nLanded: abc1234 (committed)\n"
        self.assertEqual(train.close_steps(out), steps)
        self.assertEqual(train.close_steps("Done.\nLanded: x\n"), "")
        err = []
        with mock.patch.object(train, "_close", return_value=("", f"title — Landed: abc1234 [{steps}]")), \
                mock.patch("sys.stderr") as se:
            se.write.side_effect = err.append
            train.close_landed(self.p, self.repo, ["T-0001"])
        lines = "".join(err).splitlines()
        self.assertIn("sampo lane land: closed T-0001", lines)  # unchanged: land's log and tests read it whole
        self.assertTrue(any(ln.startswith("sampo lane land: T-0001 Close steps (12.5 s)") and "affected 9.0 s (ran)" in ln
                            for ln in lines), lines)


if __name__ == "__main__":
    unittest.main()
