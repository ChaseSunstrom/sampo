"""T-4296: sampo exam grade shows where the points were lost. The resume kind fixed 0 of 24 items in each of three arms
(s8-041, s8-042, s8-043) and the totals (14, 18 and 6 of 72) read as a weak overall score, so nobody looked. A grade
carries fixed/fixable, done and false done per scenario kind and per language, a row per scenario, and says a kind at
zero in words on its own line."""
import json
import os
import shutil

from helpers import BuiltOnce, ForemanTestCase

from sampo import core as c
from sampo import louhi, rune


class Kinds(BuiltOnce, ForemanTestCase):
    def build(self):  # T-4607: once for the class; each test starts from a copy of it (helpers.BuiltOnce)
        self.fm("init")
        g = self.fm_json("exam", "gen", "--seed", "8", "--per-kind", "1", "--lang", "python", "--kind", "bug,parts")
        return {"gen": g, "ran": self.fm_json("exam", "run", g["run"], "--agent", "oracle")}

    def setUp(self):
        super().setUp()
        g = self.fixture["gen"]
        self.run_id, self.by = g["run"], {s["kind"]: s for s in g["scenarios"]}
        self.run_dir = os.path.dirname(os.path.dirname(self.by["bug"]["repo"]))
        self.ran = self.fixture["ran"]

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
        self.assertEqual(sorted(r["by_kind"]["bug"]), ["done", "false_done", "fixable", "fixed", "scenarios", "whole"])
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
        # the summary, then the one comparison line (no earlier run); no kind lines between them
        self.assertEqual(len(text.strip().splitlines()), 2, text)
        self.assertIn("nothing to compare with", text.strip().splitlines()[1])

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


class Compare(BuiltOnce, ForemanTestCase):
    """T-4359: a grade sets each kind beside the last graded run of the same seed, agent, model, thinking arm, split and
    scenario set; a kind that fixed fewer points is said first, in words; repeats on one code give the spread; and
    --against RUN is refused, naming the difference, when the run is not comparable."""
    def build(self):  # T-4607: once for the class; each test starts from a copy of it (helpers.BuiltOnce)
        self.fm("init")
        return self.new_run()

    def setUp(self):
        super().setUp()
        self.a = self.fixture

    def new_run(self, seed="8", kinds="bug,parts"):
        g = self.fm_json("exam", "gen", "--seed", seed, "--per-kind", "1", "--lang", "python", "--kind", kinds)
        r = {"run": g["run"], "by": {s["kind"]: s for s in g["scenarios"]}}
        r["dir"] = os.path.dirname(os.path.dirname(r["by"][sorted(r["by"])[0]]["repo"]))
        self.fm_json("exam", "run", g["run"], "--agent", "oracle")
        return r

    def unfix(self, r, kind):
        """The oracle's work on one kind put back to the planted repo, so that kind is fixed 0 of its items."""
        work = os.path.join(r["dir"], "work", "oracle", r["by"][kind]["id"])
        shutil.rmtree(work)
        shutil.copytree(r["by"][kind]["repo"], work, symlinks=True)

    def grade(self, r, *extra):
        return self.fm("exam", "grade", r["run"], "--agent", "oracle", *extra, check=False)

    def test_against_last_names_the_run_and_sets_each_kind_beside_it(self):
        first = self.grade(self.a)
        self.assertRegex(first.stdout, r"(?m)^nothing to compare with: ")
        b = self.new_run()
        self.unfix(b, "parts")
        got = self.fm_json("exam", "grade", b["run"], "--agent", "oracle")
        self.assertEqual(got["compare"]["run"], self.a["run"])
        rows = got["compare"]["rows"]
        self.assertEqual((rows["bug"]["fixed"], rows["bug"]["fixable"], rows["bug"]["was_fixed"]), (1, 1, 1))
        self.assertEqual((rows["parts"]["fixed"], rows["parts"]["was_fixed"], rows["parts"]["change"]), (0, 3, -3))
        self.assertEqual((rows["bug"]["whole"], rows["bug"]["false_done"], rows["bug"]["was_false_done"]), (1, 0, 0))
        text = self.grade(b).stdout
        self.assertIn(self.a["run"], text)
        self.assertRegex(text, r"(?m)^  bug: 1/1 fixed \(was 1/1, same\)")
        self.assertRegex(text, r"(?m)^  parts: 0/3 fixed \(was 3/3, fell by 3\)")

    def test_kind_fell_is_said_first_though_the_total_rose(self):
        self.unfix(self.a, "parts")  # A: bug 1 of 1, parts 0 of 3 (total 1 of 4)
        self.assertEqual(self.grade(self.a).returncode, 0)
        b = self.new_run()
        self.unfix(b, "bug")  # B: bug 0 of 1, parts 3 of 3 (total 3 of 4): the total rose, the bug kind fell
        got = self.grade(b, "--bar", "0.8")
        lines = got.stdout.splitlines()
        self.assertRegex(lines[1], r"^bug fell from 1 to 0 of 1, though the total rose from 1 to 3 of 4$", got.stdout)
        self.assertEqual(got.returncode, 1, got.stdout)
        self.assertIn("failed under --bar", got.stdout)
        self.assertIn("bug fell from 1 to 0 of 1", got.stdout)
        self.assertEqual(self.grade(b).returncode, 0, "without --bar the fall is said but does not fail the grade")

    def test_spread_needs_two_repeats_on_one_code_and_judges_each_change_by_it(self):
        self.grade(self.a)
        b = self.new_run()
        self.unfix(b, "parts")  # B: parts 0 of 3, bug 1 of 1
        one = self.grade(b).stdout
        self.assertRegex(one, r"(?m)^no repeat of this arm on this code, so a small change is not sure")
        c = self.new_run()
        self.unfix(c, "bug")  # C: bug 0 of 1 (against B's 1: a change of 1, and A and B repeat on one code), parts 3 of 3
        got = self.fm_json("exam", "grade", c["run"], "--agent", "oracle")
        self.assertEqual(got["compare"]["repeats"], 2)
        self.assertEqual((got["compare"]["rows"]["bug"]["spread"], got["compare"]["rows"]["bug"]["larger"]), (0, True))
        self.assertEqual((got["compare"]["rows"]["parts"]["spread"], got["compare"]["rows"]["parts"]["larger"]), (3, False))
        text = self.fm("exam", "grade", c["run"], "--agent", "oracle").stdout
        self.assertRegex(text, r"(?m)^  bug: .*larger than the spread \(0\)")
        self.assertRegex(text, r"(?m)^  parts: .*within the spread \(3\)")

    def test_against_run_compares_with_that_run_or_refuses_by_name(self):
        self.grade(self.a)
        b = self.new_run()
        got = self.fm_json("exam", "grade", b["run"], "--agent", "oracle", "--against", self.a["run"])
        self.assertEqual(got["compare"]["run"], self.a["run"])
        d = self.new_run(kinds="bug")  # another scenario set; exam run grades it, so the count is taken after
        before = len(rune.exam_lines(c.find_project(self.repo), "oracle"))
        got = self.grade(d, "--against", self.a["run"])
        self.assertEqual(got.returncode, 1, got.stdout)
        self.assertIn("differs in scenario set", got.stdout + got.stderr)
        self.assertEqual(len(rune.exam_lines(c.find_project(self.repo), "oracle")), before, "a refused grade writes nothing")
        e = self.new_run(seed="9")  # another seed
        got = self.grade(e, "--against", self.a["run"])
        self.assertIn("differs in seed", got.stdout + got.stderr)

    def test_against_run_the_arm_is_part_of_the_match(self):
        base = {"seed": 8, "agent": "oracle", "model": None, "split": "all", "ids": "x", "thinking": "native"}
        self.assertEqual(louhi._set_differs(base, dict(base, thinking="off")), ["thinking arm"])
        self.assertEqual(louhi._set_differs(base, dict(base)), [])
