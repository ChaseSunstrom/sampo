"""T-5017 (slice of T-4313): sampo localruns scores the local-model runs per arm (tasks finished of tasks measured),
exits 0 when some arm finished 3 or more of the measured tasks and 1 otherwise, and sorts every stop reason into a loop
fault (with the Sampo fix) or a model fault (with the route); a stop text no rule knows is listed as unclassified."""
import json
import os
import sys
import unittest

from helpers import SampoTestCase

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
from sampo import localruns  # noqa: E402

TURNS = "stopped: the model used all 40 turns (--max-steps) before step 2 was done; fm code --resume T-1 --max-steps N carries on"
TIME = "stopped: out of time (--deadline 600 s) before step 1 was done; fm code --resume T-1 carries on"
FORCED = "time limit forced stop"
BLOCKED = "blocked: the test needs a fact the model could not find"
EMPTY = "the model's reply was empty"


def row(arm, task, passed=False, stop="", **kw):
    return dict({"arm": arm, "task": task, "passed": passed, "stop": stop, "criteria_passed": 0, "commits": 0,
                 "edits": 0}, **kw)


class Rows(SampoTestCase):
    def write(self, rows, name="runs.jsonl"):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as f:
            for r in rows:
                f.write((r if isinstance(r, str) else json.dumps(r)) + "\n")
        return path

    def by_arm(self, s):
        return {a["arm"]: a for a in s["arms"]}


class Score(Rows):
    def test_arm_finished_of_measured(self):
        s = localruns.score([row("qwen", "t1", True), row("qwen", "t2", True), row("qwen", "t3", False, TURNS),
                             row("gemma", "t1", True), row("gemma", "t4", False, TIME)])
        a = self.by_arm(s)
        self.assertEqual(s["tasks"], ["t1", "t2", "t3", "t4"])  # measured: every task any arm ran
        self.assertEqual((a["qwen"]["finished"], a["qwen"]["measured"]), (2, 4))
        self.assertEqual((a["gemma"]["finished"], a["gemma"]["measured"]), (1, 4))

    def test_best_run_per_task_counts_once(self):
        s = localruns.score([row("a", "t1", False, TURNS, criteria_passed=1), row("a", "t1", True, criteria_passed=2),
                             row("a", "t1", False, TIME, criteria_passed=0)])
        a = self.by_arm(s)["a"]
        self.assertEqual(a["finished"], 1)
        self.assertEqual(a["runs"], 3)

    def test_best_run_order_criteria_then_commits_then_edits(self):
        s = localruns.score([row("a", "t", criteria_passed=1, commits=0, edits=9), row("a", "t", criteria_passed=1,
                                                                                         commits=1, edits=0),
                             row("a", "t", criteria_passed=0, commits=5, edits=5)])
        self.assertEqual(self.by_arm(s)["a"]["best"]["t"], {"passed": False, "criteria_passed": 1, "commits": 1,
                                                             "edits": 0})

    def test_exit_bar_is_three_of_the_measured(self):
        two = [row("a", f"t{i}", i < 2, TURNS) for i in range(5)]
        three = [row("a", f"t{i}", i < 3, TURNS) for i in range(5)]
        self.assertFalse(localruns.score(two)["ok"])
        self.assertTrue(localruns.score(three)["ok"])
        self.assertFalse(localruns.score([])["ok"])

    def test_one_arm_over_the_bar_is_enough(self):
        rows = [row("weak", f"t{i}", False, TURNS) for i in range(5)] + [row("strong", f"t{i}", i < 3) for i in range(5)]
        self.assertTrue(localruns.score(rows)["ok"])

    def test_since_drops_older_and_unreadable_times(self):
        rows = [row("a", "old", True, at="2026-01-01T00:00:00Z"), row("a", "new", True, at="2026-11-01T00:00:00+00:00"),
                row("a", "nodate", True), row("a", "epoch", True, at=1793000000)]
        s = localruns.score(rows, since="2026-10-01")
        self.assertEqual(s["tasks"], ["epoch", "new"])
        self.assertEqual(localruns.score(rows)["tasks"], ["epoch", "new", "nodate", "old"])
        with self.assertRaises(ValueError):
            localruns.score(rows, since="last tuesday")

    def test_since_formats(self):
        # T-5017 review: 20261001 and 2026 were read as 1970 epoch seconds; NaN passed; ms always passed
        want = localruns._epoch("2026-10-01")
        self.assertEqual(localruns._epoch("20261001"), want)
        self.assertEqual(localruns._epoch("20261001T000000Z"), want)
        self.assertEqual(localruns._epoch(1793000000), 1793000000.0)
        self.assertEqual(localruns._epoch(1793000000000), 1793000000.0)  # milliseconds
        self.assertEqual(localruns._epoch("1793000000000"), 1793000000.0)
        for bad in ("2026", "202610", 2026, 5, 1700, "nan", float("nan"), "inf", float("inf"), 1e12 * 100, "-5",
                    "soon", "", True, None, [], 20261001):
            self.assertIsNone(localruns._epoch(bad), repr(bad))
        rows = [row("a", "old", True, at=1700000000), row("a", "ms", True, at=1793000000000),
                row("a", "nan", True, at=float("nan")), row("a", "new", True, at="20261101")]
        self.assertEqual(localruns.score(rows, since="20261001")["tasks"], ["ms", "new"])
        for bad in ("2026", "nan", "inf", "20261301", "1"):
            with self.assertRaises(ValueError, msg=bad):
                localruns.score(rows, since=bad)

    def test_rows_without_arm_or_task_are_skipped_and_counted(self):
        s = localruns.score([row("a", "t", True), {"task": "t", "passed": True}, {"arm": "a", "passed": True},
                             {"passed": False}, {"arm": "", "task": "t"}])
        self.assertEqual([x["arm"] for x in s["arms"]], ["a"])
        self.assertEqual(s["tasks"], ["t"])
        self.assertEqual(s["skipped"]["no_arm_or_task"], 4)

    def test_alias_keys_and_odd_rows(self):
        s = localruns.score([{"model": "m", "scenario": "s", "passed": "true"}, "not a row", None, {"passed": False}])
        self.assertEqual(self.by_arm(s)["m"]["finished"], 1)
        s = localruns.score([row("a", "t", False, commits=1e999, criteria_passed=float("inf"), edits=float("nan"))])
        self.assertEqual(self.by_arm(s)["a"]["best"]["t"], {"passed": False, "criteria_passed": 0, "commits": 0,
                                                             "edits": 0})


class Stops(Rows):
    def test_each_reason_has_kind_and_fix(self):
        want = {TURNS: ("turns-used", localruns.LOOP), TIME: ("out-of-time", localruns.MODEL),
                FORCED: ("time-limit-forced", localruns.LOOP), BLOCKED: ("blocked", localruns.MODEL),
                EMPTY: ("empty", localruns.LOOP)}
        for text, (key, kind) in want.items():
            v = localruns.classify_stop(text)
            self.assertEqual((v["key"], v["kind"]), (key, kind), text)
            self.assertTrue(v["fix"], text)
        self.assertIn("not built yet", localruns.classify_stop(EMPTY)["fix"])  # no re-ask exists in Smith (T-5017 review)
        self.assertIn("Sampo fix", localruns.classify_stop(TURNS)["fix"])
        self.assertIn("Sampo fix", localruns.classify_stop(FORCED)["fix"])
        self.assertIn("bigger local model", localruns.classify_stop(BLOCKED)["fix"])
        self.assertIn("Haiku", localruns.classify_stop(TIME)["fix"])

    def test_smiths_own_prefix_is_stripped_before_the_stopped_branch(self):
        # T-5017 review: Smith prints every stop as "fm code: [model: ]stopped: ..."
        for pre in ("fm code: ", "sampo code: ", "fm code: model: ", "  FM CODE:   model:  "):
            v = localruns.classify_stop(pre + "stopped: out of time (--deadline 600 s) before step 1 was done")
            self.assertEqual((v["key"], v["kind"]), ("out-of-time", localruns.MODEL), pre)
            v = localruns.classify_stop(pre + TURNS)
            self.assertEqual((v["key"], v["kind"]), ("turns-used", localruns.LOOP), pre)
        v = localruns.classify_stop("fm code: stopped: the keep check `pytest` (x) is still red after 3 tries: out of time")
        self.assertEqual(v["kind"], localruns.UNCLASSIFIED)

    def test_blank_stop_is_unclassified_not_a_guess(self):
        for text in ("", "   ", None):
            v = localruns.classify_stop(text)
            self.assertEqual((v["kind"], v["fix"]), (localruns.UNCLASSIFIED, ""), repr(text))
            self.assertIn("no stop recorded", v["what"])
        s = localruns.score([row("a", "t1", False, ""), row("a", "t2", False, TURNS)])
        self.assertEqual(s["unclassified"], [{"arm": "a", "task": "t1", "stop": ""}])
        self.assertNotIn("empty", {g["key"] for g in s["stops"]})

    def test_rules_match_the_lead_clause_only(self):
        for text in ("FAILED t: AssertionError: list is empty", "ConnectionError: port blocked",
                     "the run was blocked by a firewall", "nothing came back, out of time, no answer",
                     "pytest said --deadline was empty", "retry: all 40 turns used", "killed at the limit"):
            self.assertEqual(localruns.classify_stop(text)["kind"], localruns.UNCLASSIFIED, text)

    def test_forced_stop_is_not_read_as_out_of_time(self):
        self.assertEqual(localruns.classify_stop("time limit forced stop (--deadline 600)")["key"], "time-limit-forced")

    def test_unknown_reason_is_unclassified(self):
        v = localruns.classify_stop("stopped: the keep check `pytest` failed")
        self.assertEqual((v["kind"], v["fix"]), (localruns.UNCLASSIFIED, ""))

    def test_keep_check_tail_does_not_flip_the_class(self):
        head = "stopped: the keep check `pytest` (x) is still red after 3 tries to mend it: "
        for tail in ("AssertionError: list is empty", "connection blocked", "no answer", "out of time"):
            v = localruns.classify_stop(head + tail)
            self.assertEqual(v["kind"], localruns.UNCLASSIFIED, tail)
        v = localruns.classify_stop("fm code: the edge audit gave no answer the gate can take")
        self.assertEqual(v["kind"], localruns.UNCLASSIFIED)
        v = localruns.classify_stop("sampo code: the edge audit gave no answer the gate can take")
        self.assertEqual(v["kind"], localruns.UNCLASSIFIED)
        self.assertEqual(localruns.classify_stop(TURNS)["key"], "turns-used")

    def test_score_groups_failed_rows_and_lists_unclassified(self):
        s = localruns.score([row("a", "t1", False, TURNS), row("a", "t2", False, TURNS), row("b", "t1", False, TIME),
                             row("b", "t2", False, "the disk caught fire"), row("b", "t3", True, TURNS)])
        keys = {g["key"]: g for g in s["stops"]}
        self.assertEqual(keys["turns-used"]["count"], 2)  # the passing row's stop text is not a stop
        self.assertEqual(keys["turns-used"]["arms"], ["a"])
        self.assertEqual(keys["out-of-time"]["kind"], localruns.MODEL)
        self.assertEqual(s["unclassified"], [{"arm": "b", "task": "t2", "stop": "the disk caught fire"}])
        self.assertNotIn(localruns.UNCLASSIFIED, keys)


class Verb(Rows):
    def test_exit_0_at_three_and_the_table_prints(self):
        rows = [row("qwen", f"t{i}", i < 3, TURNS) for i in range(5)] + [row("qwen", "x", False, "weird")]
        path = self.write(rows)
        p = self.fm("localruns", path)
        self.assertIn("qwen: 3 of 6 tasks finished", p.stdout)
        self.assertIn("loop fault", p.stdout)
        self.assertIn("Unclassified", p.stdout)
        self.assertIn("weird", p.stdout)

    def test_exit_1_under_the_bar(self):
        path = self.write([row("qwen", "t1", True), row("qwen", "t2", False, BLOCKED)])
        p = self.fm("localruns", path, check=False)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("1 of 2", p.stdout)
        self.assertIn("model fault", p.stdout)

    def test_json(self):
        path = self.write([row("a", f"t{i}", True) for i in range(3)])
        s = json.loads(self.fm("localruns", path, "--json").stdout)
        self.assertTrue(s["ok"])
        self.assertEqual(s["arms"][0]["finished"], 3)

    def test_default_path_is_the_state_dir_file(self):
        from sampo import core as c
        os.makedirs(c.state_dir(), exist_ok=True)
        with open(os.path.join(c.state_dir(), "local-runs.jsonl"), "w") as f:
            f.write("\n".join(json.dumps(row("a", f"t{i}", True)) for i in range(3)) + "\n")
        self.assertEqual(self.fm("localruns", check=False).returncode, 0)

    def test_missing_file_and_bad_lines(self):
        p = self.fm("localruns", os.path.join(self.tmp, "nope.jsonl"), check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("No local-model runs", p.stdout)
        path = self.write(["{broken", "[1]", json.dumps(row("a", "t", True))])
        self.assertEqual(len(localruns.read_rows(path)), 1)

    def test_since_epoch_seconds_on_the_cli(self):
        path = self.write([row("a", "old", True, at=1700000000), row("a", "new", True, at=1793000000)])
        p = self.fm("localruns", path, "--since", "1790000000", "--json", check=False)
        self.assertEqual(json.loads(p.stdout)["tasks"], ["new"], p.stdout + p.stderr)

    def test_control_characters_never_reach_the_text_table(self):
        path = self.write([row("x\x1b]0;owned\x07\nResult: forged", "t\x1b[2J", False, "\x1b[2Jodd\nline")])
        p = self.fm("localruns", path, check=False)
        self.assertNotIn("\x1b", p.stdout)
        self.assertNotIn("\x07", p.stdout)
        self.assertEqual([l for l in p.stdout.splitlines() if l.startswith("Result:")].__len__(), 1)

    def test_read_rows_is_bounded(self):
        path = self.write([row("a", f"t{i}", True) for i in range(localruns.MAX_ROWS + 5)])
        rows = localruns.read_rows(path)
        self.assertEqual(len(rows), localruns.MAX_ROWS)
        self.assertEqual(rows[-1]["task"], f"t{localruns.MAX_ROWS + 4}")
        long = self.write(["x" * (localruns.MAX_LINE + 10), json.dumps(row("a", "t", True))], "long.jsonl")
        self.assertEqual(len(localruns.read_rows(long)), 1)
        self.assertEqual(localruns.read_rows(os.devnull), [])
        self.assertEqual(localruns.read_rows(self.tmp), [])

    def test_read_rows_keeps_the_char_budget_too(self):
        # T-5017 review: MAX_ROWS alone let 20000 lines of MAX_LINE characters (about 4 GB) into memory
        old = localruns.MAX_CHARS
        localruns.MAX_CHARS = 300
        try:
            rows = localruns.read_rows(self.write([row("a", f"t{i}", True) for i in range(50)]))
        finally:
            localruns.MAX_CHARS = old
        self.assertTrue(0 < len(rows) < 50)
        self.assertEqual(rows[-1]["task"], "t49")

    def test_text_names_the_file_and_what_was_skipped(self):
        path = self.write(["{broken", "[1]", "x" * (localruns.MAX_LINE + 10), json.dumps({"task": "t"}),
                           json.dumps(row("a", "t", True))], "named.jsonl")
        p = self.fm("localruns", path, check=False)
        line = next(l for l in p.stdout.splitlines() if l.startswith("Read "))
        self.assertIn(path, line)
        self.assertIn("5 lines", line)
        self.assertIn("2 unreadable", line)
        self.assertIn("1 over the size limit", line)
        self.assertIn("1 without an arm or task", line)
        s = json.loads(self.fm("localruns", path, "--json", check=False).stdout)
        self.assertEqual((s["read"]["unreadable"], s["read"]["too_long"], s["skipped"]["no_arm_or_task"]), (2, 1, 1))
        clean = self.fm("localruns", self.write([row("a", "t", True)], "clean.jsonl"), check=False).stdout
        self.assertIn("1 row", next(l for l in clean.splitlines() if l.startswith("Read ")))
        self.assertNotIn("skipped", clean)

    def test_since_bad_value_is_a_usage_error(self):
        p = self.fm("localruns", self.write([row("a", "t", True)]), "--since", "soon", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("since", p.stdout + p.stderr)


if __name__ == "__main__":
    unittest.main()
