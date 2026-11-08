"""sampo bisect --hunks (T-3269): the first bad commit's hunks, each reverted alone in a throwaway worktree, name the hunks
whose revert makes the check pass (file and line range). The user's tree and worktree list stay as they were, and without
--hunks the output is the one bisect always printed. A hunk whose file name git quotes is counted, not dropped; a check's
children die with it at the deadline."""
import json
import os
import shutil
import subprocess
import time

from helpers import ForemanTestCase
from sampo import bisecthunk

GOOD_CALC = "def f():\n    return 1\n\n\ndef g():\n    return 1\n"
BAD_CALC = "def f():\n    return 2\n\n\ndef g():\n    return 1\n"  # f: the culprit hunk; same size as the good line
GOOD_OTHER = "K = 1\n\n\ndef k():\n    return K\n"
BAD_OTHER = "K = 2\n\n\ndef k():\n    return K\n"  # an innocent hunk of the same commit, in a second file
QUOTED = 'q"x.txt'  # git C-quotes this name in diff headers, so its hunk can't be parsed
F_ONLY = "python3 -c 'import calc; assert calc.f() == 1'"  # other.py's change doesn't matter to this check
BOTH = "python3 -c 'import calc, other; assert calc.f() == 1 and other.K == 1'"  # needs both hunks back


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, check=True).stdout


def read(path):
    with open(path) as f:
        return f.read()


class Hunks(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.write("calc.py", GOOD_CALC)
        self.write("other.py", GOOD_OTHER)
        self.good = self.commit("good")
        self.write("n.txt", "1\n")
        self.commit("unrelated")
        self.write("calc.py", BAD_CALC)
        self.write("other.py", BAD_OTHER)
        self.write(QUOTED, "1\n")
        self.bad = self.commit("break f and K")
        self.write("m.txt", "2\n")
        self.commit("later")

    def write(self, rel, text):
        with open(os.path.join(self.repo, rel), "w") as f:
            f.write(text)

    def commit(self, msg):
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", msg)
        return git(self.repo, "rev-parse", "HEAD").strip()

    def bisect(self, check, *extra):
        return self.fm("bisect", "--run", check, "--good", self.good, *extra)

    def test_the_hunk_whose_revert_passes_is_named(self):
        res = json.loads(self.bisect(F_ONLY, "--hunks", "--json").stdout)
        self.assertEqual(res["sha"], self.bad)
        self.assertEqual([(h["file"], h["lines"]) for h in res["hunks"]], [("calc.py", "2")])
        self.assertIn("return 2", res["hunks"][0]["text"])
        self.assertIn("calc.py:2", self.bisect(F_ONLY, "--hunks").stdout)
        self.assertEqual(json.loads(self.bisect(F_ONLY, "--hunks", "--json").stdout), res)  # a second run agrees

    def test_no_single_hunk_is_named_when_two_are_needed(self):
        res = json.loads(self.bisect(BOTH, "--hunks", "--json").stdout)
        self.assertEqual(res["sha"], self.bad)
        self.assertEqual(res["hunks"], [])
        self.assertIn("no single hunk", self.bisect(BOTH, "--hunks").stdout)

    def test_a_hunk_git_quotes_is_counted_not_dropped(self):
        res = json.loads(self.bisect(F_ONLY, "--hunks", "--json").stdout)
        self.assertEqual(res["hunks_skipped"], 1)
        self.assertIn("not tested", self.bisect(F_ONLY, "--hunks").stdout)

    def test_the_json_carries_the_outcome_fields(self):
        res = json.loads(self.bisect(F_ONLY, "--hunks", "--json").stdout)
        self.assertEqual((res["passes_at_sha"], res["unreverted"], res["fails_at_parent"], res["error"]),
                         (False, 0, False, ""))

    def test_the_tree_worktrees_and_a_failing_run_leave_the_user_alone(self):
        stale = os.path.join(self.tmp, "stale")
        git(self.repo, "worktree", "add", "--detach", "-q", stale, self.good)
        shutil.rmtree(stale)  # a stale worktree record: a whole-repo prune would drop it

        def state():
            files = [read(os.path.join(self.repo, f)) for f in ("calc.py", "other.py", "m.txt")]
            return (git(self.repo, "rev-parse", "HEAD"), git(self.repo, "status", "--porcelain"),
                    git(self.repo, "worktree", "list", "--porcelain"), files)
        before = state()
        self.bisect(F_ONLY, "--hunks")
        self.assertEqual(state(), before)
        res = bisecthunk.culprit_hunks(self.repo, "0" * 40, F_ONLY, 1)  # a sha that doesn't exist: the failure path
        self.assertNotEqual(res["error"], "")
        self.assertEqual(state(), before)

    def test_a_check_that_forks_loses_its_children_at_the_deadline(self):
        bisecthunk._passes("sleep 57.3 & wait", self.repo, 1)
        deadline = time.time() + 2
        while time.time() < deadline:
            if "sleep 57.3" not in subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True).stdout:
                return
            time.sleep(0.1)
        self.fail("the check's background sleep outlived its deadline")

    def test_a_timed_out_run_reports_no_result(self):
        rc, _ = bisecthunk.run_group(["sh", "-c", "sleep 30"], self.repo, 0.5)
        self.assertIsNone(rc)

    def test_a_check_that_also_fails_at_the_parent_names_nothing(self):
        res = bisecthunk.culprit_hunks(self.repo, self.bad, "false", 1)
        self.assertEqual(res["hunks"], [])
        self.assertTrue(res["fails_at_parent"])
        self.assertEqual(res["tested"], 2)
        self.assertEqual(res["error"], "")

    def test_without_the_flag_the_output_is_unchanged(self):
        res = json.loads(self.bisect(F_ONLY, "--json").stdout)
        self.assertEqual(sorted(res), ["files", "sha", "subject"])
        self.assertEqual(res["sha"], self.bad)
