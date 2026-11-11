"""T-4296: sampo exam grade shows where the points were lost. The resume kind fixed 0 of 24 items in each of three arms
(s8-041, s8-042, s8-043) and the totals (14, 18 and 6 of 72) read as a weak overall score, so nobody looked. A grade
carries fixed/fixable, done and false done per scenario kind and per language, a row per scenario, and says a kind at
zero in words on its own line."""
import json
import os
import shutil

from helpers import ForemanTestCase

from sampo import louhi


class Kinds(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        g = self.fm_json("exam", "gen", "--seed", "8", "--per-kind", "1", "--lang", "python", "--kind", "bug,parts")
        self.run_id, self.by = g["run"], {s["kind"]: s for s in g["scenarios"]}
        self.run_dir = os.path.dirname(os.path.dirname(self.by["bug"]["repo"]))
        self.ran = self.fm_json("exam", "run", self.run_id, "--agent", "oracle")

    def unfix(self, kind):
        """The oracle's work on one kind put back to the planted repo: that kind is then fixed 0 of its items."""
        work = os.path.join(self.run_dir, "work", "oracle", self.by[kind]["id"])
        shutil.rmtree(work)
        shutil.copytree(self.by[kind]["repo"], work, symlinks=True)

    def grade(self):
        return self.fm_json("exam", "grade", self.run_id, "--agent", "oracle")

    def text(self):
        return self.fm("exam", "grade", self.run_id, "--agent", "oracle").stdout

    def test_per_kind_counts_and_per_scenario_rows_are_in_the_json(self):
        r = self.ran
        self.assertEqual(sorted(r["by_kind"]), ["bug", "parts"])
        self.assertEqual((r["by_kind"]["bug"]["scenarios"], r["by_kind"]["bug"]["fixable"], r["by_kind"]["bug"]["fixed"]),
                         (1, 1, 1))
        self.assertEqual((r["by_kind"]["parts"]["fixable"], r["by_kind"]["parts"]["fixed"]), (3, 3))
        self.assertEqual(sorted(r["by_kind"]["bug"]), ["done", "false_done", "fixable", "fixed", "scenarios"])
        self.assertEqual(r["by_lang"]["python"]["scenarios"], 2)
        self.assertEqual(r["by_lang"]["python"]["fixed"], r["fixed"])
        rows = {x["id"]: x for x in r["rows"]}
        self.assertEqual(sorted(rows), sorted(s["id"] for s in self.by.values()))
        row = rows[self.by["parts"]["id"]]
        self.assertEqual((row["kind"], row["lang"], row["fixable"], row["fixed"]), ("parts", "python", 3, 3))
        for key in ("done", "false_done", "error", "first_action_s", "turns", "ended", "note"):
            self.assertIn(key, row)

    def test_per_kind_a_line_for_each_kind_follows_the_totals_in_the_text(self):
        lines = self.text().splitlines()
        self.assertIn("fixed", lines[0], "the totals stay the first line")
        self.assertTrue(any(x.strip().startswith("bug: 1/1 fixed") for x in lines[1:]), lines)
        self.assertTrue(any(x.strip().startswith("parts: 3/3 fixed") for x in lines[1:]), lines)

    def test_per_kind_rows_read_turns_and_the_first_action_from_the_answers_events(self):
        sid = self.by["bug"]["id"]
        with open(os.path.join(self.run_dir, "answers", "oracle", f"{sid}.events.jsonl"), "w", encoding="utf-8") as f:
            for e in ({"event": "start", "ts": 100.0}, {"event": "say", "ts": 150.0},
                      {"event": "action", "action": "read", "ts": 299.5}, {"event": "action", "action": "edit", "ts": 320.0},
                      {"event": "stats", "turns": 19, "ts": 400.0}):
                f.write(json.dumps(e) + "\n")
            f.write("not json\n")
        rows = {x["id"]: x for x in self.grade()["rows"]}
        self.assertEqual((rows[sid]["first_action_s"], rows[sid]["turns"]), (199.5, 19))
        other = rows[self.by["parts"]["id"]]
        self.assertEqual((other["first_action_s"], other["turns"]), (None, None), "no events file: unknown, not zero")

    def test_per_kind_rows_stay_out_of_the_tower_line(self):
        from sampo import rune
        from sampo import core as c
        line = rune.exam_lines(c.find_project(self.repo), "oracle")[-1]
        self.assertNotIn("rows", line, "a row per scenario in every tower line would grow exam.jsonl without bound")
        self.assertEqual(sorted(line["by_kind"]), ["bug", "parts"])
        self.assertEqual(line["kinds"], ["bug", "parts"], "the tower line's own list of kinds is unchanged")

    def test_zero_a_kind_with_nothing_fixed_is_named_on_its_own_line(self):
        self.fm("genesis", "init", "--file", "-", input="go\n\n## 2. The capability spec\n\n"
                "1. **Correctness.** Fixes what's broken. Exam: bug.\n\n## 3. The tower\n")  # the scorecard reads gen-0
        self.unfix("bug")
        g = self.grade()
        self.assertEqual((g["by_kind"]["bug"]["fixed"], g["by_kind"]["parts"]["fixed"]), (0, 3))
        text = self.text()
        self.assertRegex(text, r"(?m)^nothing fixed in kind bug \(1 scenario, [01] said done\)$")
        self.assertNotIn("nothing fixed in kind parts", text)
        got = self.fm("exam", "scorecard", "--agent", "oracle", check=False)  # the scorecard carries it too
        card = got.stdout
        self.assertIn("nothing fixed in kind bug (1 scenario", card, got.stderr)
        self.assertIn(f"newest grade by kind ({self.run_id})", card)

    def test_zero_no_line_when_every_kind_fixed_something(self):
        self.assertNotIn("nothing fixed in kind", self.text())

    def test_zero_a_run_of_one_kind_has_no_kind_lines(self):
        g = self.fm_json("exam", "gen", "--seed", "9", "--per-kind", "1", "--lang", "python", "--kind", "bug")
        self.fm_json("exam", "run", g["run"], "--agent", "empty")
        text = self.fm("exam", "grade", g["run"], "--agent", "empty").stdout
        self.assertEqual(len(text.strip().splitlines()), 1, text)

    def test_zero_a_tower_line_from_before_this_change_still_prints(self):
        old = {"run": "s1-001", "agent": "smith", "kinds": ["goal", "resume"], "found": 0, "keyed": 0, "fixed": 6, "fixable": 72,
               "scenarios": 6}
        self.assertEqual(louhi.kinds_text(old), "")
        self.assertIn("6/72 fixed", louhi.summary(old))

    def test_zero_text_counts_scenarios_and_said_done(self):
        r = {"by_kind": {"resume": {"scenarios": 6, "fixable": 24, "fixed": 0, "done": 5, "false_done": 5},
                         "goal": {"scenarios": 6, "fixable": 48, "fixed": 14, "done": 2, "false_done": 1}}}
        text = louhi.kinds_text(r)
        self.assertIn("nothing fixed in kind resume (6 scenarios, 5 said done)", text.splitlines())
        self.assertTrue(any(x.strip().startswith("goal: 14/48 fixed") for x in text.splitlines()), text)
        self.assertTrue(any("5 false done" in x for x in text.splitlines() if x.strip().startswith("resume:")), text)
