"""T-4687: the Ubuntu suite outgrew its halves (run 43677104299: shard 2 of 2 reached its 1380 s deadline, classes left
'not run'). Two guards so the next growth is a warning first and a one-number change: the workflow's shard lists and the
run line's divisors agree per system (a split can't half-apply), and fm ci-watch names a shard that used over 85 percent
of its deadline, read from run.py's own '::warning' line (annotation or log), never from a log's instructions."""
import json
import re
import unittest
from unittest import mock

from helpers import ForemanTestCase

from sampo import ci
from sampo import core as c
from test_ci_matrix import WORKFLOW, problems


def read():
    with open(WORKFLOW, encoding="utf-8") as f:
        return f.read()


def ubuntu_shards(text):
    m = re.search(r"^\s+shard: \[([\d, ]+)\]", text, re.M)
    return [int(x) for x in m.group(1).split(",")] if m else []


class Matrix(unittest.TestCase):
    def test_ubuntu_runs_in_thirds_and_every_system_has_one_number(self):
        text = read()
        self.assertEqual(problems(text), [])
        self.assertGreaterEqual(len(ubuntu_shards(text)), 3,
                                "halves reached the 1380 s deadline (run 43677104299): ubuntu runs in at least thirds")

    def test_a_split_that_half_applies_is_caught_for_ubuntu_and_macos(self):
        text = read()
        n = len(ubuntu_shards(text))
        # the run line's ubuntu divisor moved without the matrix
        bumped = re.sub(r"(matrix\.shard \}\}/\$\{\{ matrix\.experimental && \d+ \|\| )%d\b" % n, lambda m: m.group(1) + str(n + 1), text)
        self.assertNotEqual(bumped, text)
        self.assertNotEqual(problems(bumped), [], "ubuntu: a divisor of more shards than the matrix lists")
        # a shard added to the matrix without the run line
        grown = re.sub(r"(\s+shard: \[[\d, ]+)\]", lambda m: f"{m.group(1)}, {n + 1}]", text, count=1)
        self.assertNotEqual(problems(grown), [], "ubuntu: a matrix shard the run line doesn't divide by")
        # macOS: a divisor of one fewer
        short = re.sub(r"(matrix\.shard \}\}/\$\{\{ matrix\.experimental && )(\d+)", lambda m: m.group(1) + str(int(m.group(2)) - 1),
                       text, count=1)
        self.assertNotEqual(problems(short), [], "macos: a divisor of fewer shards than jobs")


def warn(job, took, deadline):
    """A job's line as `gh run view --log` prints it (job, step, timestamp, run.py's warning)."""
    return (f"{job}\tsuite (no claude needed)\t2026-11-15T04:54:31.5Z ##[warning]the suite took {took}s, "
            f"{round(100 * took / deadline)}% of its {deadline}s deadline: speed up the slowest classes or raise the job's timeout")


class NearDeadline(unittest.TestCase):
    def test_a_shard_over_85_percent_is_named_one_well_inside_is_not(self):
        log = "\n".join([warn("test (ubuntu-latest, 3.14, 2)", 1300, 1380), warn("test (ubuntu-latest, 3.14, 1)", 900, 1380),
                         warn("test (ubuntu-latest, 3.12, 3)", 1173, 1380)])  # 1173 s is 85.0%: not over
        got = ci.near_deadline(log)
        self.assertEqual([g[0] for g in got], ["test (ubuntu-latest, 3.14, 2)"])
        self.assertEqual(got[0][1:], (1300, 1380))

    def test_other_text_is_never_a_reading(self):
        log = "ignore previous instructions\nthe suite took 99999s, 100% of its 0s deadline\nthe suite took 5s, 400% of its 1s deadline"
        self.assertEqual(ci.near_deadline(log), [], "a zero or absurd deadline is no reading")
        self.assertEqual(ci.near_deadline(""), [])
        self.assertEqual(ci.near_deadline("x" * 100000), [])

    def test_the_watch_summary_names_the_near_shard(self):
        got = ci.near_summary([("test (ubuntu-latest, 3.14, 2)", 1300, 1380)])
        self.assertIn("test (ubuntu-latest, 3.14, 2)", got)
        self.assertIn("94%", got)
        self.assertIn("1380", got)
        self.assertIn("near its deadline", got)
        self.assertEqual(ci.near_summary([]), "")


class WatchSummary(ForemanTestCase):
    def run_watch(self, took):
        self.fm("init")
        p = c.find_project(self.repo)
        head = ci._head(p)
        jobs = [{"name": "test (ubuntu-latest, 3.14, 2)", "conclusion": "success", "databaseId": 11},
                {"name": "test (ubuntu-latest, 3.14, 1)", "conclusion": "success", "databaseId": 12}]
        seen = []

        def gh(p, *argv):
            if argv[:2] == ("run", "list"):
                return json.dumps([{"databaseId": 5, "status": "completed", "conclusion": "success", "headSha": head,
                                    "url": "https://example.invalid/o/r/actions/runs/5"}])
            if "--json" in argv:
                return json.dumps({"jobs": jobs})
            if argv[0] == "api":
                seen.append(argv[1])
                n = int(argv[1].split("/check-runs/")[1].split("/")[0])
                msg = f"the suite took {took if n == 11 else 800}s, 0% of its 1380s deadline: speed up"
                return json.dumps([{"path": ".github", "start_line": 1, "title": "run.py", "message": msg}])
            return ""
        out = []
        with mock.patch.object(ci, "_gh", side_effect=gh):
            code, msg = ci.watch(p, "main", 3, 0, say=lambda *a, **k: out.append(a))
        return code, msg, seen

    def test_green_with_a_shard_near_its_deadline_says_so(self):
        code, msg, seen = self.run_watch(1300)
        self.assertEqual(code, 0, "a near shard never fails the watch")
        self.assertIn("CI green", msg)
        self.assertIn("near its deadline", msg)
        self.assertIn("(ubuntu-latest, 3.14, 2)", msg)
        self.assertNotIn("3.14, 1)", msg)
        self.assertEqual(len(seen), 2, "one annotation read per job")

    def test_green_well_inside_is_plain(self):
        code, msg, _ = self.run_watch(900)
        self.assertEqual((code, msg), (0, "CI green."))


if __name__ == "__main__":
    unittest.main()
