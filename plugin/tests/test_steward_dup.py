"""T-5019 (slice of T-4315): sampo steward dup finds open tasks that say one thing twice. Open task titles and
criteria become hashed word and bigram TF-IDF vectors; a cosine past DUP is a duplicate, past FOLD a fold candidate;
unrelated tasks, closed ones and a task and the one it depends on are never a pair; a project with no tasks is fine; the
pass is bounded and gives the same rows twice."""
import json
import os
import sys
import time
import types
import unittest

from helpers import SampoTestCase

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
from sampo import core as c  # noqa: E402
from sampo import steward_dup as sd  # noqa: E402

LOGIN = ("Login page rejects a valid password after the session cookie expires",
         ["The login form accepts a valid password once the session cookie has expired",
          "A regression test covers the expired cookie and the valid password"])
LOGIN_REWORDED = ("The login page refuses a valid password once the session cookie expired",
                  ["The login form accepts a valid password after the session cookie has expired",
                   "A regression test covers the expired session cookie with a valid password"])
LOGIN_PART = ("Login form keeps a valid password when the session cookie expires",
              ["The login form accepts a valid password once the session cookie has expired",
               "The error message after a refused login names the reason",
               "The refused login is counted in the metrics panel"])
CHART = ("Render the usage chart as a stacked area with a legend",
         ["The usage chart draws one stacked area per model", "The legend lists every model once",
          "A snapshot test pins the svg output"])
DOCKER = ("Prune dangling docker images after a benchmark run",
          ["Benchmark containers are removed when the run ends", "Dangling images older than a day are pruned"])


class Dup(SampoTestCase):
    def setUp(self):
        super().setUp()
        self.p = c._project(c.slug_for(self.repo), self.repo)
        os.makedirs(os.path.join(self.p.dir, "tasks"))
        self.n = 0

    def task(self, spec, status="planned", depends=(), tid=None):
        title, criteria = spec
        self.n += 1
        tid = tid or f"T-{100 + self.n:04d}"
        b = c.Brief.new(tid, title, "FEATURE", "M", status=status, depends=depends)
        for text in criteria:
            b.add_ac(text, verify="python3 plugin/tests/run.py test_nothing")
        with open(os.path.join(self.p.dir, "tasks", f"{tid}.md"), "w") as f:
            f.write(b.render())
        return tid

    def rows(self, **kw):
        return sd.run(self.p, types.SimpleNamespace(**kw))

    def test_a_reworded_duplicate_is_found_and_the_newer_folds_into_the_older(self):
        a, b = self.task(LOGIN), self.task(LOGIN_REWORDED)
        self.task(CHART)
        self.task(DOCKER)
        rows = self.rows()
        self.assertEqual([(r["kind"], r["keep"], r["fold"]) for r in rows], [("duplicate", a, b)], rows)
        self.assertGreaterEqual(rows[0]["score"], sd.DUP)

    def test_an_exact_copy_scores_one(self):
        self.task(CHART)
        self.task(CHART)
        self.task(DOCKER)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["score"], 1.0, places=2)

    def test_a_task_that_is_part_of_another_is_a_fold_candidate_not_a_duplicate(self):
        a, b = self.task(LOGIN), self.task(LOGIN_PART)  # LOGIN_PART is the whole, and the newer
        self.task(CHART)
        self.task(DOCKER)
        rows = self.rows()
        self.assertEqual([(r["kind"], r["keep"], r["fold"]) for r in rows], [("fold", b, a)], rows)
        self.assertGreaterEqual(rows[0]["score"], sd.FOLD)
        self.assertLess(rows[0]["score"], sd.DUP)

    def test_a_part_folds_into_the_whole_when_the_whole_is_the_older(self):
        whole, part = self.task(LOGIN_PART), self.task(LOGIN)
        self.task(CHART)
        self.task(DOCKER)
        rows = self.rows()
        self.assertEqual([(r["kind"], r["keep"], r["fold"]) for r in rows], [("fold", whole, part)], rows)

    def test_unrelated_tasks_make_no_pair(self):
        for spec in (LOGIN, CHART, DOCKER):
            self.task(spec)
        self.assertEqual(self.rows(), [])

    def test_a_shared_boilerplate_does_not_make_a_pair(self):
        """Every criterion names the same checkout and verify words: that is the project's, not the task's."""
        same = ["The change is covered by a test in the plugin tests folder", "The docs map line is updated"]
        for title in ("Render the usage chart as a stacked area", "Prune dangling docker images after a benchmark",
                      "Parse the changelog headings into release notes", "Sort the queue by cost per task",
                      "Mirror the briefs into the repo folder", "Warn when the disk is nearly full",
                      "Replay the tool calls of a session offline", "Name the owner of each stale lease"):
            self.task((title, same))
        self.assertEqual(self.rows(), [])

    def test_closed_tasks_are_left_out(self):
        self.task(LOGIN, status="done")
        self.task(LOGIN_REWORDED, status="dropped")
        self.task(LOGIN_REWORDED)
        self.assertEqual(self.rows(), [])

    def test_every_open_status_is_read(self):
        a = self.task(LOGIN, status="captured")
        b = self.task(LOGIN_REWORDED, status="blocked")
        self.assertEqual([(r["keep"], r["fold"]) for r in self.rows()], [(a, b)])

    def test_a_task_and_the_one_it_depends_on_are_a_deliberate_split(self):
        a = self.task(LOGIN)
        self.task(LOGIN_REWORDED, depends=[a])
        self.assertEqual(self.rows(), [])

    def test_a_verify_command_and_a_task_id_do_not_count_as_words(self):
        one = ("Prune dangling docker images", ["Dangling images are pruned, verify with python3 run.py test_prune T-1234"])
        two = ("Chart the usage by model", ["The chart shows usage, verify with python3 run.py test_prune T-1234"])
        self.task(one)
        self.task(two)
        self.assertEqual(self.rows(), [])

    def test_no_tasks_and_one_task_give_no_rows(self):
        self.assertEqual(self.rows(), [])
        self.task(LOGIN)
        self.assertEqual(self.rows(), [])

    def test_a_project_with_no_tasks_folder_is_fine(self):
        q = c._project("empty", self.repo)
        self.assertEqual(sd.run(q, types.SimpleNamespace()), [])
        self.assertEqual(sd.run(q), [])

    def test_a_task_with_no_criteria_and_an_empty_title_makes_no_crash(self):
        self.task(("", []))
        self.task(("", []))
        self.task(("Only a title", []))
        self.assertEqual(self.rows(), [])  # nothing to say: no words, no pair

    def test_rows_are_best_first_and_the_same_twice(self):
        self.task(LOGIN)
        self.task(LOGIN_PART)
        self.task(CHART)
        self.task(LOGIN_REWORDED)
        self.task(CHART, tid="T-0900")
        first, second = self.rows(), self.rows()
        self.assertEqual(first, second)
        self.assertEqual([r["score"] for r in first], sorted((r["score"] for r in first), reverse=True))
        self.assertEqual({r["kind"] for r in first}, {"duplicate", "fold"})
        self.assertEqual(self.rows(limit=1), first[:1])
        self.assertEqual(self.rows(limit=0), [])

    def test_thresholds_move_the_verdict_and_a_bad_one_is_refused(self):
        self.task(LOGIN)
        self.task(LOGIN_PART)
        self.assertEqual([r["kind"] for r in self.rows(dup=0.3, fold=0.2)], ["duplicate"])
        self.assertEqual(self.rows(fold=0.99, dup=1.0), [])
        from sampo.cli import UsageError
        for bad in ({"dup": "x"}, {"dup": 0}, {"dup": 1.5}, {"fold": "nan"}, {"fold": 0.9, "dup": 0.5},
                    {"limit": "abc"}, {"limit": "1.5"}):
            with self.assertRaises(UsageError, msg=bad):
                self.rows(**bad)

    def test_one_threshold_alone_is_not_refused_against_the_others_default(self):
        self.task(LOGIN)
        self.task(LOGIN_PART)
        self.rows(dup=0.3)   # fold default 0.45 is above 0.3: clamped, not refused
        self.rows(fold=0.9)  # dup default 0.8 is below 0.9: clamped, not refused
        self.assertEqual([r["kind"] for r in self.rows(dup=0.3)], ["duplicate"])

    def test_a_cluster_bigger_than_a_third_of_a_small_backlog_is_still_found(self):
        for _ in range(3):
            self.task(CHART)
        for spec in (DOCKER, LOGIN, ("Mirror the briefs into the repo folder", ["Briefs are copied once"]),
                     ("Warn when the disk is nearly full", ["A warning shows at ninety percent"]),
                     ("Rotate the ledger monthly", ["Old ledgers are compressed"])):
            self.task(spec)
        self.assertEqual(len(self.rows()), 3)

    def test_a_long_run_of_spaces_in_a_criterion_is_cheap(self):
        b = c.Brief.new("T-0900", "Spaces", "FIX", "S")
        b.add_ac("The thing works" + " " * 200000 + "and more", verify="true")
        t0 = time.monotonic()
        sd.text_of(b)
        self.assertLess(time.monotonic() - t0, 1.0)

    def test_nothing_is_written_or_changed(self):
        self.task(LOGIN)
        self.task(LOGIN_REWORDED)
        path = os.path.join(self.p.dir, "tasks")
        def read():
            out = {}
            for f in os.listdir(path):
                with open(os.path.join(path, f)) as h:
                    out[f] = h.read()
            return out
        before = read()
        self.rows()
        self.assertEqual(before, read())

    def test_a_pass_over_hundreds_of_tasks_takes_milliseconds_and_is_bounded(self):
        briefs = []
        topics = ("chart", "docker", "login", "ledger", "parser", "cache", "queue", "lease", "audit", "merge")
        for i in range(400):
            t = topics[i % 10]
            b = c.Brief.new(f"T-{i + 1:04d}", f"Handle the {t} case number {i} in module {t}{i % 37}", "FIX", "S")
            b.add_ac(f"The {t} path number {i} no longer fails on input {i * 7} and logs once", verify="true")
            b.add_ac("The change is covered by a test " + "and a long pasted log line " * 400)
            briefs.append(b)
        info = {}
        rows = sd.find(briefs, info=info)
        self.assertFalse(info["timed_out"], "400 tasks should finish inside the pairing budget")
        self.assertLessEqual(len(rows), sd.MAX_ROWS)

    def test_a_pasted_log_is_clipped_to_max_tokens(self):
        got = sd.counts(" ".join(f"w{i}" for i in range(5000)))
        self.assertLessEqual(sum(got.values()), 2 * sd.MAX_TOKENS)

    def test_the_pairing_stops_at_its_deadline(self):
        vecs = [{1: 1.0}] * 50
        self.assertTrue(sd.similar(vecs, 0.1))
        self.assertEqual(sd.similar(vecs, 0.1, deadline=time.monotonic() - 1), [])

    def test_short_titles_with_one_shared_criterion_make_no_pair_in_a_small_backlog(self):
        """15 tasks, below MIN_COMMON: a one-line criterion they all share must not pair unrelated short titles."""
        titles = ["Fix chart", "Fix queue", "Fix lease", "Fix ledger", "Fix mirror", "Fix disk", "Fix replay",
                  "Fix cards", "Fix hooks", "Fix sync", "Fix audit", "Fix drive", "Fix pause", "Fix focus", "Fix scope"]
        for t in titles:
            self.task((t, ["The change is covered by a test in the plugin tests folder"]))
        self.assertEqual(self.rows(), [])

    def test_a_cut_list_and_a_timeout_and_a_clip_are_reported(self):
        info = {}
        for _ in range(3):
            self.task(CHART)
        rows = sd.find(c.load_briefs(self.p), limit=1, info=info)
        self.assertEqual(len(rows), 1)
        self.assertEqual(info["left_out"], 2)
        info = {}
        sd.find(c.load_briefs(self.p), budget=-1, info=info)
        self.assertTrue(info["timed_out"])
        info = {}
        old = sd.MAX_TASKS
        sd.MAX_TASKS = 2
        try:
            sd.find(c.load_briefs(self.p), info=info)
        finally:
            sd.MAX_TASKS = old
        self.assertEqual(info["clipped"], 1)
        info = {}
        sd.similar([{1: 1.0}] * (sd.MAX_POST + 1), 0.1, info=info)
        self.assertEqual(info["skipped"], 1)

    def test_the_verb_says_what_it_left_out(self):
        from sampo import cli
        for _ in range(3):
            self.task(CHART)
        out = []
        args = types.SimpleNamespace(json=False, limit="1")
        orig_resolve, orig_out = cli.resolve, cli.out
        cli.resolve = lambda args: self.p
        cli.out = lambda args, data, text: out.append((data, text))
        try:
            sd.cmd_dup(args)
        finally:
            cli.resolve, cli.out = orig_resolve, orig_out
        self.assertIn("2 left out (--limit)", out[0][1])
        self.assertEqual(out[0][0]["left_out"], 2)

    def test_an_unreadable_brief_is_counted_and_never_reads_as_all_clear(self):
        from sampo import cli
        a, b = self.task(LOGIN), self.task(LOGIN_REWORDED)
        with open(os.path.join(self.p.dir, "tasks", "T-0999.md"), "w") as f:
            f.write("---\nid: T-0999\nstatus: planned\n")  # no closing fence: unreadable
        info = {}
        rows = sd.run(self.p, types.SimpleNamespace(), info)
        self.assertEqual([(r["keep"], r["fold"]) for r in rows], [(a, b)])
        self.assertEqual(info["unreadable"], 1)
        out = []
        orig_resolve, orig_out = cli.resolve, cli.out
        cli.resolve = lambda args: self.p
        cli.out = lambda args, data, text: out.append((data, text))
        try:
            sd.cmd_dup(types.SimpleNamespace(json=False))
        finally:
            cli.resolve, cli.out = orig_resolve, orig_out
        data, text = out[0]
        self.assertEqual(data["unreadable"], 1)
        self.assertIn("1 brief(s) unreadable", text)

    def test_a_state_error_is_raised_not_read_as_no_rows(self):
        self.task(LOGIN)
        self.task(LOGIN_REWORDED)
        orig = c.load_briefs

        def broken(*a, **k):
            raise c.CorruptState("state file: broken")
        c.load_briefs = broken
        try:
            with self.assertRaises(c.CorruptState):
                self.rows()
        finally:
            c.load_briefs = orig

    def test_a_pair_that_sums_to_just_under_one_is_a_duplicate_at_dup_one(self):
        a, b = self.task(CHART), self.task(CHART)
        orig = sd.similar
        sd.similar = lambda vecs, floor, deadline=None, info=None: [(0, 1, 0.9999999)]
        try:
            rows = self.rows(dup=1.0)
        finally:
            sd.similar = orig
        self.assertEqual([(r["kind"], r["keep"], r["fold"]) for r in rows], [("duplicate", a, b)], rows)

    def test_only_one_steward_verb_is_registered(self):
        import re
        with open(os.path.join(os.path.dirname(__file__), "..", "lib", "sampo", "cli.py")) as f:
            src = f.read()
        self.assertEqual(len(re.findall(r'\badd\(\s*"steward"', src)), 1)

    def test_the_command_runs_through_the_cli(self):
        a, b = self.task(LOGIN), self.task(LOGIN_REWORDED)
        self.task(CHART)
        r = self.fm("steward", "dup", "--json")
        rows = json.loads(r.stdout)["rows"]
        self.assertEqual([(x["keep"], x["fold"]) for x in rows], [(a, b)])
        self.assertIn("steward", self.fm("help", "--all").stdout)

    def test_the_verb_prints_the_rows(self):
        from sampo import cli
        a, b = self.task(LOGIN), self.task(LOGIN_REWORDED)
        out = []
        args = types.SimpleNamespace(json=False)
        orig_resolve, orig_out = cli.resolve, cli.out
        cli.resolve = lambda args: self.p
        cli.out = lambda args, data, text: out.append((data, text))
        try:
            sd.cmd_dup(args)
        finally:
            cli.resolve, cli.out = orig_resolve, orig_out
        data, text = out[0]
        self.assertEqual([r["fold"] for r in data["rows"]], [b])
        self.assertIn(f"{b} into {a}", text)


if __name__ == "__main__":
    unittest.main()
