"""T-5517: sampo task evidence --run for a task a lane holds runs its command in the lane and stamps the lane's tree,
whichever folder it was called from (called from the main checkout it recorded trunk's result as the lane's); a merged
or laneless task still runs where it was called."""
import json
import os
import subprocess

from helpers import ForemanTestCase

TEST = "import sys\nsys.exit(0 if open('app.txt').read().strip() == 'new' else 1)\n"
RUN = "python3 test_app.py"


class EvidenceLane(ForemanTestCase):
    def git(self, *a, cwd=None):
        return subprocess.run(["git", "-C", cwd or self.repo, *a], check=True, capture_output=True, text=True).stdout

    def put(self, root, rel, text):
        with open(os.path.join(root, rel), "w") as f:
            f.write(text)

    def setUp(self):
        super().setUp()
        self.fm("init")
        self.put(self.repo, "app.txt", "old\n")
        self.put(self.repo, "test_app.py", TEST)
        self.git("add", "-A")
        self.git("commit", "-qm", "base")
        self.lane = os.path.join(self.tmp, "lane")
        self.git("worktree", "add", "-q", "-b", "foreman/lane", self.lane)
        self.tid = json.loads(self.fm("task", "new", "fix app", "--type", "FIX", "--tier", "S", "--ac", "ok :: true",
                                      "--step", "s", "--json", "--focus", cwd=self.lane).stdout)["id"]
        self.put(self.lane, "app.txt", "new\n")
        self.git("add", "app.txt", cwd=self.lane)
        self.git("commit", "-qm", "fix", cwd=self.lane)

    def evidence(self, *extra, cwd=None):
        return self.fm("task", "evidence", self.tid, "--step", "1", "--run", RUN, *extra, cwd=cwd, check=False)

    def lines(self):
        return self.fm("task", "show", self.tid).stdout.split("## Verification evidence", 1)[1].split("\n## ", 1)[0]

    def test_from_the_main_checkout_it_runs_the_lanes_files_and_stamps_the_lanes_tree(self):
        from sampo import core as c
        r = self.evidence()  # main's app.txt says old: a run there would fail
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ran in lane", r.stdout)
        self.assertIn(f"[tree {c.worktree_id(self.lane)}]", self.lines())
        self.assertNotIn(f"[tree {c.worktree_id(self.repo)}]", self.lines())

    def test_the_lanes_own_working_tree_decides_red_then_green(self):
        self.put(self.lane, "app.txt", "wip\n")  # uncommitted: it is what the builder is testing
        r = self.evidence()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.put(self.lane, "app.txt", "new\n")
        r = self.evidence()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_called_from_the_lane_it_runs_there_and_says_nothing_of_a_move(self):
        r = self.evidence(cwd=self.lane)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("ran in lane", r.stdout)

    def test_a_repeat_runs_in_the_lane_too(self):
        r = self.evidence("--repeat", "2")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_a_background_run_from_main_stamps_the_lanes_tree(self):
        from sampo import core as c
        r = self.evidence("--background")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.fm("task", "wait", self.tid, "--timeout", "60", timeout=90)
        self.assertIn(f"[tree {c.worktree_id(self.lane)}]", self.lines())
        self.assertIn("ran in lane", self.lines())

    def test_a_merged_lane_runs_in_the_callers_folder(self):
        self.git("merge", "--ff-only", "foreman/lane")
        self.put(self.repo, "app.txt", "later\n")  # main moved on; the lane's old files must not answer
        r = self.evidence()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertNotIn("ran in lane", r.stdout)

    def test_a_laneless_task_runs_in_the_callers_folder(self):
        tid = json.loads(self.fm("task", "new", "other", "--type", "FIX", "--tier", "S", "--ac", "ok :: true",
                                 "--step", "s", "--json").stdout)["id"]
        self.fm("focus", tid)
        r = self.fm("task", "evidence", tid, "--step", "1", "--run", RUN, check=False)  # main's app.txt says old
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertNotIn("ran in lane", r.stdout)

    def test_check_evidence_runs_the_gates_in_the_lane(self):
        # one gate, which passes only on the lane's files
        self.fm("check", "add", RUN, cwd=self.repo)
        r = self.fm("check", "--evidence", self.tid, "--step", "1", check=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ran in lane", self.lines())
        from sampo import core as c
        self.assertIn(f"[tree {c.worktree_id(self.lane)}]", self.lines())

    def script_pair(self):
        # main's copy of the script always passes; the lane's copy runs the test, so only it can fail the lane
        self.put(self.repo, "x.sh", "exit 0\n")
        self.put(self.lane, "x.sh", RUN + "\n")

    def run_file(self, path, cwd=None):
        return self.fm("task", "evidence", self.tid, "--step", "1", "--run-file", path, cwd=cwd, check=False)

    def test_a_run_file_from_main_runs_and_is_judged_as_the_lanes_copy(self):
        self.script_pair()
        self.put(self.lane, "app.txt", "wip\n")
        r = self.run_file("x.sh")  # main's x.sh passes; the lane's fails on the lane's app.txt
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.put(self.lane, "app.txt", "new\n")
        r = self.run_file("x.sh")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ran in lane", r.stdout)

    def test_a_run_file_named_by_its_lane_path_is_accepted_from_main(self):
        self.script_pair()
        r = self.run_file(os.path.join(self.lane, "x.sh"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("outside the lane", r.stdout + r.stderr)

    def test_a_lane_script_that_links_out_of_the_lane_is_refused_from_main(self):
        outside = os.path.join(self.tmp, "outside.sh")
        self.put(self.tmp, "outside.sh", "exit 0\n")
        self.put(self.repo, "x.sh", "exit 0\n")
        os.symlink(outside, os.path.join(self.lane, "x.sh"))
        r = self.run_file("x.sh")  # main's x.sh is a plain file; the lane's copy links out
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("outside the lane", r.stdout + r.stderr)

    def test_a_leftover_folder_that_is_no_worktree_is_not_a_lane(self):
        from sampo import core as c
        stale = os.path.join(self.tmp, "stale")
        os.makedirs(stale)
        self.put(stale, "app.txt", "new\n")
        self.put(stale, "test_app.py", TEST)
        p = c.find_project(self.repo)
        b = c.find_brief(p, self.tid)
        b.meta["lane"] = stale
        c.save_brief(p, b)
        r = self.evidence()  # main's app.txt says old: the leftover folder must not answer
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertNotIn("ran in lane", r.stdout)

    def test_an_audit_from_main_stamps_the_lanes_tree(self):
        from sampo import core as c
        self.fm("task", "audit", self.tid, "self", "read the diff", "no gaps")
        self.assertIn(f"[tree {c.worktree_id(self.lane)}]", self.lines())
        self.assertNotIn(f"[tree {c.worktree_id(self.repo)}]", self.lines())


if __name__ == "__main__":
    import unittest
    unittest.main()
