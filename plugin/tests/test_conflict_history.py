"""T-1161: fm lane merge records the files that conflicted (a lane_conflict event); fm lane brief warns when the new
lane's scope names a file that conflicted twice or more in the last 30 days, and not for other files or old events."""
import json
import os
import subprocess
import unittest

from helpers import ForemanTestCase

from sampo import core as c


class ConflictHistory(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")

    def git(self, *args, cwd):
        return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", cwd, *args],
                              capture_output=True, text=True, check=True).stdout

    def task(self, title, scope="x.py"):
        return json.loads(self.fm("task", "new", title, "--type", "FEATURE", "--tier", "M", "--ac", "ok :: true",
                                  "--step", "s", "--interpretation", "Reading 1: a. Reading 2: b.", "--approach", "1) a; fails if: x 2) use what exists: b; fails if: y 3) do less: c; fails if: z Choice 1 (80%): a", "--scope", scope,
                                  "--json").stdout)["id"]

    def conflicting_lane(self, tid, text):
        path = json.loads(self.fm("lane", "new", tid, "--json").stdout)["path"]
        with open(os.path.join(path, "hot.py"), "w") as f:
            f.write(text)
        self.git("add", "hot.py", cwd=path)
        self.git("commit", "-qm", "lane", cwd=path)
        p = c.find_project(self.repo)  # T-5968: reviewed at that tip (a merge holds an unreviewed lane before it conflicts)
        b = c.find_brief(p, tid)
        b.meta["accepted_tip"] = self.git("rev-parse", "HEAD", cwd=path).strip()
        c.save_brief(p, b)

    def brief(self, tid):
        p = self.fm("lane", "brief", tid, "--json")
        return json.loads(p.stdout)

    def test_two_conflicts_on_a_file_warn_the_next_lane_scoped_to_it(self):
        a, b = self.task("A", "hot.py"), self.task("B", "hot.py")
        self.conflicting_lane(a, "a = 1\n")
        self.conflicting_lane(b, "b = 1\n")
        with open(os.path.join(self.repo, "hot.py"), "w") as f:
            f.write("main = 1\n")
        self.git("add", "hot.py", cwd=self.repo)
        self.git("commit", "-qm", "main", cwd=self.repo)
        for t in (a, b):
            self.assertNotEqual(self.fm("lane", "merge", t, check=False).returncode, 0)
        new, other = self.task("C", "hot.py"), self.task("D", "calm.py")
        self.assertNotIn("conflicted", json.dumps(self.brief(other)))
        got = self.brief(new)
        self.assertTrue(any("hot.py" in w and "2" in w for w in got["warnings"]), got)

    def test_one_lane_conflicting_twice_or_a_tasks_own_conflicts_do_not_warn(self):
        p = c.find_project(self.repo)
        self.fm("lane", "max", "6")
        a = self.task("A", "hot.py")
        for _ in range(2):
            c.log_event(p, "lane_conflict", task=a, data={"files": ["hot.py"]})
        self.assertEqual(self.brief(a).get("warnings") or [], [], "its own conflicts")
        self.assertEqual(self.brief(self.task("B", "hot.py")).get("warnings") or [], [], "one lane, twice")
        c.log_event(p, "lane_conflict", task="T-9002", data={"files": ["hot.py"]})
        self.assertEqual(len(self.brief(self.task("C", "hot.py"))["warnings"]), 1, "two lanes")

    def test_one_conflict_or_old_ones_do_not_warn(self):
        p = c.find_project(self.repo)
        c.log_event(p, "lane_conflict", task="T-9000", data={"files": ["hot.py"]})
        old = {"ts": "2020-01-01T00:00:00+00:00", "task": "T-9001", "event": "lane_conflict",
               "data": {"files": ["hot.py"]}}
        with open(os.path.join(p.dir, "ledger.jsonl"), "a") as f:
            f.write(json.dumps(old) + "\n")
        self.assertEqual(self.brief(self.task("E", "hot.py")).get("warnings") or [], [])


if __name__ == "__main__":
    unittest.main()
