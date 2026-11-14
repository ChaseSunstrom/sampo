"""T-1618: fm playbooks eval: held-out briefs scored with and without a playbook loaded; the lift is with minus without."""
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

from helpers import FM, PLUGIN, ForemanTestCase

from sampo import core as c
from sampo import playbooks as pb

BOOK = """# Fixture: migrations
- Write the down migration before the up one.
- Run the migration twice: the second run must change nothing.
"""
BRIEFS = {"briefs": [
    {"task": "Add a NOT NULL column to the users table.",
     "checks": [{"has": "down migration"}, {"re": r"twice|idempotent"}]},
    {"task": "Rename the orders table; mention the users table.",
     "checks": [{"has": "users table"}]},
]}


class Lift(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.book = os.path.join(self.tmp, "migrations.md")
        self.briefs = os.path.join(self.tmp, "migrations.json")
        with open(self.book, "w") as f:
            f.write(BOOK)
        with open(self.briefs, "w") as f:
            json.dump(BRIEFS, f)

    def run_fm(self, *a):
        return subprocess.run([sys.executable, FM, "playbooks", *a], capture_output=True, text=True, cwd=self.tmp)

    def test_the_literal_model_obeys_a_line_only_when_the_playbook_is_loaded(self):
        r = pb.evaluate(BOOK, BRIEFS["briefs"], pb.literal)
        self.assertEqual((r["with"], r["without"], r["n"]), (2, 1, 2))
        self.assertAlmostEqual(r["lift"], 0.5)

    def test_eval_prints_and_records_the_lift(self):
        p = self.run_fm("eval", self.book, "--briefs", self.briefs)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("lift +0.50", p.stdout)
        self.assertIn("with 2/2, without 1/2", p.stdout)
        with open(os.path.join(c.state_dir(), "playbook-evals.jsonl")) as f:
            row = json.loads(f.read().splitlines()[-1])
        self.assertEqual((row["model"], row["with"], row["without"], row["lift"]), ("literal", 2, 1, 0.5))

    def test_a_real_model_runs_only_with_real_and_each_child_is_budgeted(self):
        calls = []

        def child(feature, system, prompt, model, timeout, **k):
            calls.append((feature, "migrations" in system))
            return "down migration, run it twice" if "migrations" in system else "users table"
        with mock.patch("sampo.ideas.run_child", side_effect=child):
            r = pb.evaluate(BOOK, BRIEFS["briefs"], pb.real_model("haiku"))
        self.assertEqual(len(calls), 4, "each brief with and without")
        self.assertTrue(all(f == "playbook-eval" for f, _ in calls), "run_child checks and records each against fm budget")
        self.assertEqual((r["with"], r["without"]), (1, 1))
        with mock.patch("sampo.ideas.run_child") as rc:
            pb.evaluate(BOOK, BRIEFS["briefs"], pb.literal)
        rc.assert_not_called()

    def test_real_is_refused_up_front_when_the_budget_cant_cover_it(self):
        from sampo import budget
        with mock.patch("sampo.budget.check", side_effect=budget.BudgetError("today's cap is spent")), \
                mock.patch("sampo.ideas.run_child") as rc:
            with self.assertRaises(ValueError):
                pb.preflight_real(BRIEFS["briefs"])
        rc.assert_not_called()

    def test_bad_briefs_are_refused_with_the_reason(self):
        with open(self.briefs, "w") as f:
            json.dump({"briefs": [{"task": "x", "checks": [{"oops": 1}]}]}, f)
        p = self.run_fm("eval", self.book, "--briefs", self.briefs)
        self.assertEqual(p.returncode, 1)
        self.assertIn("check", p.stderr)

    def test_brief_files_that_could_hang_or_say_nothing_are_refused(self):
        # review: a nested quantifier backtracks without end, an empty check carries no signal, deep JSON recurses
        bad = [{"briefs": [{"task": "x", "checks": [{"re": "(a+)+$"}]}]},
               {"briefs": [{"task": "x", "checks": [{"re": "a" * 300}]}]},
               {"briefs": [{"task": "x", "checks": [{"re": "(unclosed"}]}]},
               {"briefs": [{"task": "x", "checks": [{"has": ""}]}]}]
        for data in bad:
            with open(self.briefs, "w") as f:
                json.dump(data, f)
            with self.assertRaises(ValueError, msg=data):
                pb.load_briefs(self.briefs)
        with open(self.briefs, "w") as f:
            f.write("[" * 100000)
        with self.assertRaises(ValueError):
            pb.load_briefs(self.briefs)

    def test_a_failing_real_child_names_its_brief(self):
        def child(feature, system, prompt, model, timeout, **k):
            raise ValueError("nothing came back")
        with mock.patch("sampo.ideas.run_child", side_effect=child):
            with self.assertRaisesRegex(ValueError, "brief 1"):
                pb.evaluate(BOOK, BRIEFS["briefs"], pb.real_model("haiku"))

    def test_a_library_name_wins_over_a_file_of_the_same_path(self):
        os.makedirs(os.path.join(self.tmp, "references", "verify"))
        with open(os.path.join(self.tmp, "references", "verify", "tdd-workflow.md"), "w") as f:
            f.write("# a stray copy\n")
        p = self.run_fm("eval", "references/verify/tdd-workflow.md")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("references/verify/tdd-workflow.md: lift", p.stdout)
        self.assertIn("text-presence", p.stdout)

    def test_the_shipped_brief_sets_name_real_playbooks_and_show_lift(self):
        root = os.path.join(PLUGIN, "evals", "playbooks")
        sets = [os.path.join(d, f) for d, _, fs in os.walk(root) for f in fs if f.endswith(".json")]
        self.assertTrue(sets, "at least one held-out brief set ships")
        books = pb.listed()
        for s in sets:
            name = os.path.relpath(s, root)[:-5]
            hits = pb.resolve(name, books)
            self.assertEqual(len(hits), 1, name)
            book = pb.load(hits[0])
            for i, b in enumerate(pb.load_briefs(s), 1):  # each brief, not the set: one weak brief can't hide
                self.assertTrue(pb.passes(pb.literal(b["task"], book), b["checks"]), f"{name} brief {i} with")
                self.assertFalse(pb.passes(pb.literal(b["task"], None), b["checks"]), f"{name} brief {i} without")



class Ablate(ForemanTestCase):
    """T-1619: each top-level section's share of the lift, the section dropped one at a time; zero share is flagged."""
    BOOK = ("# Fixture\nIntro line.\n\n## Down first\n- Write the down migration before the up one.\n\n"
            "## Twice\n- Run the migration twice.\n\n## Style\n- Name files in kebab-case.\n")

    def test_each_section_gets_its_share_and_a_zero_share_one_is_flagged(self):
        rows = pb.ablate(self.BOOK, BRIEFS["briefs"], pb.literal)
        self.assertEqual([r["section"] for r in rows], ["Down first", "Twice", "Style"])
        share = {r["section"]: r["share"] for r in rows}
        self.assertEqual(share, {"Down first": 0.5, "Twice": 0.5, "Style": 0.0})
        self.assertEqual([r["section"] for r in rows if r["cut"]], ["Style"])

    def test_a_heading_inside_a_code_fence_is_not_a_section(self):
        book = "# T\n\n## Real\n```md\n## not a section\n```\n\n## Also real\n"
        self.assertEqual([t for t, _, _ in pb.sections(book)], ["Real", "Also real"])

    def test_eval_ablate_prints_each_section_and_the_cut_candidate(self):
        book = os.path.join(self.tmp, "migrations.md")
        briefs = os.path.join(self.tmp, "migrations.json")
        with open(book, "w") as f:
            f.write(self.BOOK)
        with open(briefs, "w") as f:
            json.dump(BRIEFS, f)
        p = subprocess.run([sys.executable, FM, "playbooks", "eval", book, "--briefs", briefs, "--ablate"],
                           capture_output=True, text=True, cwd=self.tmp)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertRegex(p.stdout, r"Down first\s+\+0\.50")
        self.assertRegex(p.stdout, r"Style\s+\+0\.00.*cut")


PROF = {"name": "gpu-q", "kind": "openai", "model": "q", "context": 8192, "own": True}


class Small(ForemanTestCase):
    """T-2917: eval on a local profile. The variant tests moved to test_small_cards.SmallVariant (T-4659)."""

    def setUp(self):
        super().setUp()
        self.fm("init")
        self.sent = []
        for patch in (mock.patch.dict(os.environ, {"SAMPO_CARDS": os.path.join(self.tmp, "cards")}),
                      mock.patch("sampo.moonscale._generative", lambda s: iter([PROF])),
                      mock.patch("sampo.models.resolve", lambda n: PROF if n == "gpu-q" else {"name": n}),
                      mock.patch("sampo.models.complete", self.complete),
                      mock.patch("sampo.ideas.run_child", side_effect=AssertionError("claude -p"))):
            patch.start()
            self.addCleanup(patch.stop)

    def complete(self, prof, messages, **k):
        self.sent.append(messages)
        return {"text": " ".join(m["content"] for m in messages), "usage": {}}  # echoes: answers carry the playbook

    def test_a_local_profile_answers_through_models_complete_and_is_booked(self):
        r = pb.evaluate(BOOK, BRIEFS["briefs"], pb.real_model("gpu-q"))
        self.assertEqual((r["n"], len(self.sent)), (2, 4))
        self.assertEqual(pb.real_model("gpu-q").name, "gpu-q")
        self.assertIsNone(pb.local_profile("haiku"), "a cloud name keeps the claude -p path")
        path = os.path.join(self.tmp, "b.json")
        with open(path, "w") as f:
            json.dump(BRIEFS, f)
        from types import SimpleNamespace as NS
        pb.cmd_eval(NS(name="fix/migration", briefs=path, real=True, model="gpu-q", ablate=False, json=True))
        with open(os.path.join(c.state_dir(), "playbook-evals.jsonl")) as f:
            self.assertEqual(json.loads(f.read().splitlines()[-1])["model"], "gpu-q")


if __name__ == "__main__":
    unittest.main()
