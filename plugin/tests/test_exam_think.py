"""T-4297: sampo exam grade keeps the thinking arm that fixed more. A local model's default is thinking off; when one run
id has graded lines for one model under --reason off and --reason native, the arm that fixed more is written to the
model's kept record (think-kept.json in the state: arm, run and both counts) and the grade says so in one line; a tie or
fewer keeps off. No model is called: the oracle's answers stand in, with the arm and the model stamped by hand the way
fm code's answers carry them (the `reason` and `model` files of an answer folder), and some graded lines are made by hand."""
import json
import os
import shutil
import time
from unittest import mock

from helpers import ForemanTestCase

from sampo import core as c
from sampo import louhi, rune, smith


class KeptArm(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        g = self.fm_json("exam", "gen", "--seed", "8", "--per-kind", "1", "--lang", "python", "--kind", "bug,parts")
        self.run_id, self.by = g["run"], {s["kind"]: s for s in g["scenarios"]}
        self.run_dir = os.path.dirname(os.path.dirname(self.by["bug"]["repo"]))
        self.fm_json("exam", "run", self.run_id, "--agent", "oracle")
        self.answers = os.path.join(self.run_dir, "answers", "oracle")
        self.state = c.state_dir()

    def stamp(self, reason, model="qwen-local", thinking=None, **extra):
        """The stamps of an answer folder: the --reason (none when reason is None), the model, the arm that ran (`thinking`,
        T-4297: only when given, an older line has none) and any other stamp (planner, small, request)."""
        for name in ("reason", "thinking", "planner", "small", "request"):
            try:
                os.remove(os.path.join(self.answers, name))
            except FileNotFoundError:
                pass
        for name, text in dict(reason=reason, model=model, thinking=thinking, **extra).items():
            if text is not None:
                with open(os.path.join(self.answers, name), "w", encoding="utf-8") as f:
                    f.write(text)

    def unfix(self, kind):
        work = os.path.join(self.run_dir, "work", "oracle", self.by[kind]["id"])
        shutil.rmtree(work)
        shutil.copytree(self.by[kind]["repo"], work, symlinks=True)

    def grade(self):
        return self.fm_json("exam", "grade", self.run_id, "--agent", "oracle")

    def text(self):
        return self.fm("exam", "grade", self.run_id, "--agent", "oracle").stdout

    def kept(self):
        try:
            with open(os.path.join(self.state, smith.THINK_FILE), encoding="utf-8") as f:
                return json.load(f)
        except OSError:
            return None

    def two_arms(self, off_unfixed=None, native_unfixed=None, model="qwen-local"):
        """Grade the run under off, then under native; an arm's kind is put back to the planted repo first."""
        out = []
        for reason, unfixed in (("off", off_unfixed), ("native", native_unfixed)):
            self.fm_json("exam", "run", self.run_id, "--agent", "oracle")
            self.stamp(reason, model)
            if unfixed:
                self.unfix(unfixed)
            out.append(self.grade())
        return out

    def test_kept_arm_native_fixed_more_writes_the_record_and_the_grade_says_so(self):
        off, native = self.two_arms(off_unfixed="bug")
        self.assertEqual((off["fixed"], native["fixed"]), (3, 4))
        rec = self.kept()["qwen-local"]
        self.assertEqual((rec["arm"], rec["run"], rec["native"], rec["off"]), ("native", self.run_id, 4, 3))
        self.assertIsInstance(rec["ts"], float)
        self.assertEqual(native["kept_arm"]["arm"], "native")
        lines = [x for x in self.fm("exam", "grade", self.run_id, "--agent", "oracle").stdout.splitlines()
                 if "thinking" in x]
        self.assertEqual(len(lines), 1, lines)
        for word in ("qwen-local", "native", "4", "3"):
            self.assertIn(word, lines[0])

    def test_kept_arm_a_tie_keeps_off(self):
        self.two_arms()
        rec = self.kept()["qwen-local"]
        self.assertEqual((rec["arm"], rec["native"], rec["off"]), ("off", 4, 4))
        self.assertEqual(smith.think_arm("qwen-local"), "off")

    def test_kept_arm_fewer_keeps_off_and_takes_native_back_from_an_earlier_record(self):
        os.makedirs(self.state, exist_ok=True)
        with open(os.path.join(self.state, smith.THINK_FILE), "w", encoding="utf-8") as f:
            json.dump({"qwen-local": {"arm": "native", "run": "s1-001", "native": 5, "off": 4, "ts": 1.0,
                                      "8192": {"n": 1, "ts": 1.0}}}, f)
        self.two_arms(native_unfixed="bug")
        rec = self.kept()["qwen-local"]
        self.assertEqual((rec["arm"], rec["run"], rec["native"], rec["off"]), ("off", self.run_id, 3, 4))
        self.assertIn("8192", rec, "the sizes stay")

    def test_kept_arm_one_arm_alone_writes_nothing(self):
        self.stamp("native")
        self.grade()
        self.assertIsNone(self.kept())
        self.assertNotIn("thinking kept", self.text())

    def test_kept_arm_no_reason_or_another_arm_writes_nothing(self):
        for reason in ("state", "budget"):
            self.stamp(reason)
            self.grade()
        self.stamp("off")
        self.grade()
        self.assertIsNone(self.kept())

    def test_kept_arm_the_arms_of_two_models_are_not_compared(self):
        self.stamp("off", "model-a")
        self.grade()
        self.unfix("bug")
        self.stamp("native", "model-b")
        self.grade()
        self.assertIsNone(self.kept())

    def test_kept_arm_a_regrade_of_the_same_arm_uses_the_newest_line(self):
        self.stamp("off")
        self.unfix("bug")
        self.grade()
        self.fm_json("exam", "run", self.run_id, "--agent", "oracle")
        self.stamp("off")
        self.grade()  # off fixes all 4 now
        self.fm_json("exam", "run", self.run_id, "--agent", "oracle")
        self.stamp("native")
        self.grade()
        rec = self.kept()["qwen-local"]
        self.assertEqual((rec["arm"], rec["native"], rec["off"]), ("off", 4, 4))

    def test_kept_arm_arms_over_a_different_number_of_items_are_not_compared(self):
        lines = [{"run": "s9-001", "agent": "smith", "model": "m", "reason": "off", "fixed": 3, "fixable": 8, "split": "all",
                  "closed": {}},
                 {"run": "s9-001", "agent": "smith", "model": "m", "reason": "native", "fixed": 5, "fixable": 4, "split": "all",
                  "closed": {}}]
        self.assertIsNone(louhi.think_arm_pick(lines, "s9-001", "m"))

    def test_kept_arm_pick_from_hand_made_lines(self):
        def line(reason, fixed, run="s9-001", model="m", fixable=8, **kw):
            return dict({"run": run, "agent": "smith", "model": model, "reason": reason, "fixed": fixed, "fixable": fixable,
                         "split": "all", "closed": {}}, **kw)
        pick = louhi.think_arm_pick
        self.assertEqual(pick([line("off", 3), line("native", 5)], "s9-001", "m"), ("native", 5, 3))  # (arm, native, off)
        self.assertEqual(pick([line("off", 5), line("native", 5)], "s9-001", "m"), ("off", 5, 5))
        self.assertEqual(pick([line("off", 5), line("native", 4)], "s9-001", "m"), ("off", 4, 5))
        self.assertIsNone(pick([line("off", 5)], "s9-001", "m"))
        self.assertIsNone(pick([line("off", 5, run="s9-002"), line("native", 6)], "s9-001", "m"), "one run id")
        self.assertIsNone(pick([line("off", 5), line("native", 6, split="holdout")], "s9-001", "m"), "one split")
        self.assertIsNone(pick([line("off", 5), line("native", 6, model="n")], "s9-001", "m"), "one model")
        self.assertEqual(pick([line("off", 5), line("native", 6), line("native", 4)], "s9-001", "m"), ("off", 4, 5))
        self.assertIsNone(pick([line("off", "x"), line("native", 6)], "s9-001", "m"), "a count that is no number")
        self.assertIsNone(pick([{"reason": "off"}, "junk", None, line("native", 6)], "s9-001", "m"))

    def test_kept_arm_the_tower_line_keeps_no_extra_rows(self):
        self.two_arms(off_unfixed="bug")
        line = rune.exam_lines(c.find_project(self.repo), "oracle")[-1]
        self.assertNotIn("rows", line)

    # T-4297 review

    def prior(self, **extra):
        os.makedirs(self.state, exist_ok=True)
        rec = dict({"arm": "native", "run": "s1-001", "native": 20, "off": 14, "fixable": 24,
                    "ts": time.mktime((2026, 1, 2, 12, 0, 0, 0, 0, -1))}, **extra)
        with open(os.path.join(self.state, smith.THINK_FILE), "w", encoding="utf-8") as f:
            json.dump({"qwen-local": rec}, f)

    def test_kept_arm_the_record_stores_the_sample_size(self):
        self.two_arms(off_unfixed="bug")
        self.assertEqual(self.kept()["qwen-local"]["fixable"], 4)

    def test_kept_arm_a_smaller_sample_never_replaces_a_larger_record_of_the_other_arm(self):
        self.prior()
        self.two_arms()  # a tie over 4 fixable items: off, but the record is over 24
        rec = self.kept()["qwen-local"]
        self.assertEqual((rec["arm"], rec["run"], rec["fixable"]), ("native", "s1-001", 24))
        line = [x for x in self.text().splitlines() if "thinking" in x]
        self.assertEqual(len(line), 1, line)
        for word in ("native", "stays", "s1-001", "24"):
            self.assertIn(word, line[0])

    def test_kept_arm_a_replaced_record_is_named_in_the_grade(self):
        self.prior(fixable=4)
        graded = self.two_arms()[1]  # a tie over 4: off replaces native
        rec = self.kept()["qwen-local"]
        self.assertEqual((rec["arm"], rec["fixable"]), ("off", 4))
        line = [x for x in louhi.graded_text(graded).splitlines() if "thinking" in x]
        self.assertEqual(len(line), 1, line)
        for word in ("replaced", "native", "s1-001", "20", "14", "2026-01-02"):
            self.assertIn(word, line[0])

    def test_kept_arm_a_first_record_replaces_nothing(self):
        self.two_arms()
        self.assertNotIn("replaced", self.text())

    def test_kept_arm_a_default_run_and_a_native_run_are_a_pair(self):
        for reason, thinking, unfixed in ((None, "off", "bug"), ("native", "native", None)):
            self.fm_json("exam", "run", self.run_id, "--agent", "oracle")
            self.stamp(reason, thinking=thinking)
            if unfixed:
                self.unfix(unfixed)
            self.grade()
        rec = self.kept()["qwen-local"]
        self.assertEqual((rec["arm"], rec["native"], rec["off"]), ("native", 4, 3))

    def test_kept_arm_a_run_with_no_arm_stamp_says_why_no_pair_forms(self):
        for reason, thinking in ((None, None), ("native", "native")):
            self.fm_json("exam", "run", self.run_id, "--agent", "oracle")
            self.stamp(reason, thinking=thinking)
            self.grade()
        self.assertIsNone(self.kept())
        line = [x for x in self.text().splitlines() if "no pair" in x]
        self.assertEqual(len(line), 1, line)
        for word in ("qwen-local", self.run_id, "no thinking arm"):
            self.assertIn(word, line[0])

    def test_kept_arm_two_runs_that_differ_in_a_stamp_are_not_paired_and_it_says_which(self):
        for stamps in ({"planner": "big"}, {"small": "on"}, {"request": "other words"}):
            with self.subTest(stamps=stamps):
                try:
                    os.remove(os.path.join(self.state, smith.THINK_FILE))
                except FileNotFoundError:
                    pass
                self.fm_json("exam", "run", self.run_id, "--agent", "oracle")
                self.stamp("off", thinking="off")
                self.grade()
                self.fm_json("exam", "run", self.run_id, "--agent", "oracle")
                self.stamp("native", thinking="native", **stamps)
                self.grade()
                self.assertIsNone(self.kept())
                line = [x for x in self.text().splitlines() if "no pair" in x]
                self.assertEqual(len(line), 1, line)
                self.assertIn(next(iter(stamps)), line[0])

    def test_kept_arm_the_pick_names_what_differs_for_hand_made_lines(self):
        def line(reason, fixed, **kw):
            return dict({"run": "s9-001", "agent": "smith", "model": "m", "reason": reason, "thinking": reason, "fixed": fixed,
                         "fixable": 8, "split": "all"}, **kw)
        pair = louhi.think_pair
        self.assertEqual(pair([line("off", 3), line("native", 5)], "s9-001", "m")[2], "")
        self.assertEqual(pair([line("off", 3), line("native", 5)], "s9-001", "m")[0]["arm"], "native")
        self.assertEqual(pair([line("off", 5), line("native", 5)], "s9-001", "m")[0]["arm"], "off", "a tie keeps off")
        self.assertIn("planner", pair([line("off", 3), line("native", 5, planner="x")], "s9-001", "m")[2])
        self.assertIn("small", pair([line("off", 3), line("native", 5, small=True)], "s9-001", "m")[2])
        self.assertIn("request", pair([line("off", 3, request="a"), line("native", 5)], "s9-001", "m")[2])
        self.assertIn("split", pair([line("off", 3), line("native", 5, split="holdout")], "s9-001", "m")[2])
        self.assertIn("fixable", pair([line("off", 3), line("native", 5, fixable=9)], "s9-001", "m")[2])
        self.assertEqual(pair([line("off", 3, small=False), line("native", 5)], "s9-001", "m")[2], "", "no small is small off")
        self.assertEqual(pair([line("off", 3)], "s9-001", "m"), (None, None, ""), "one arm alone is not a surprise")
        legacy = {"run": "s9-001", "agent": "smith", "model": "m", "fixed": 3, "fixable": 8, "split": "all"}
        self.assertIn("no thinking arm", pair([legacy, line("native", 5)], "s9-001", "m")[2])
        self.assertEqual(pair([dict(legacy, reason="off", thinking=None), line("native", 5)], "s9-001", "m")[2], "",
                         "an explicit --reason off with no stamp is the arm off")

    def test_kept_arm_a_run_stamps_the_arm_it_ran(self):
        from sampo import models
        profile = {"name": "qwen-local", "kind": "openai", "base_url": "http://127.0.0.1:11434/v1", "model": "q"}
        p = c.find_project(self.repo)
        for kept, reason, want in ((None, None, "off"), ("native", None, "native"), ("native", "off", "off"),
                                   (None, "native", "native"), (None, "state", "state")):
            with self.subTest(kept=kept, reason=reason):
                try:
                    os.remove(os.path.join(self.state, smith.THINK_FILE))
                except FileNotFoundError:
                    pass
                if kept:
                    smith.think_keep("qwen-local", kept, "s1-001", 5, 4, 8)
                os.makedirs(os.path.join(self.run_dir, "answers", "smith"), exist_ok=True)
                with mock.patch.object(models, "resolve", lambda m: profile), \
                        mock.patch.object(louhi, "_smith_arm", lambda *a, **k: {"notes": [], "done": False}):
                    louhi._run_agent(p, self.run_id, "smith", "all", 1, None, "qwen-local", 60, None, 1, None, reason)
                with open(os.path.join(self.run_dir, "answers", "smith", "thinking"), encoding="utf-8") as f:
                    self.assertEqual(f.read(), want)
                line = rune.exam_lines(p, "smith")[-1]
                self.assertEqual(line.get("thinking"), want)

    def test_kept_arm_a_long_model_name_keeps_and_reads_its_arm_under_one_key(self):
        name = "m" * 70
        os.makedirs(self.state, exist_ok=True)
        got = louhi.keep_think_arm(c.find_project(self.repo), "s9-001", "oracle", name)
        self.assertIsNone(got)  # no lines: nothing kept, nothing raised
        smith.think_keep(c.plain(name)[:60], "native", "s9-001", 5, 4, 8)
        self.assertEqual(smith.think_arm(name), "native")
