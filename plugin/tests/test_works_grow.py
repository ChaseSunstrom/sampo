"""T-3110: Design works part 2 — the types grid read thinnest first, and the writer loop per cell: best of N drafts, a
critique of the render, up to K revise rounds, a pairwise rank of the survivors, the best kept in a JSONL store with its
writer, rounds, gate numbers and rank; a cell whose drafts all fail keeps nothing. The writer, the renderer, the critic
and the judge are fakes here: no model is asked and no browser started."""
# map-links: plugin/lib/sampo/worksgrow.py plugin/lib/sampo/works.py plugin/lib/sampo/cli.py
import json
import os
import types

from helpers import ForemanTestCase
from sampo import works

NS = 'xmlns="http://www.w3.org/2000/svg"'


def svg(tag="a", extra=""):
    """A clean 24 grid icon the static gates pass; `tag` is told apart in its id."""
    return (f'<svg {NS} viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">'
            f'<circle cx="12" cy="12" r="8"/><path id="{tag}" d="M12 8v4l3 2"/>{extra}</svg>')


class Fake:
    """The four hooks of one run, each recording what it was asked. A draft names its flaws in its tag: CLIP fails the
    render's clip gate; `fix` turns a draft's revise prompt into the revised reply."""

    def __init__(self, drafts, defects=None, prefer=None, fix=None):
        self.drafts, self.defects, self.prefer, self.fix = list(drafts), defects or {}, prefer, fix
        self.asked, self.revises, self.critiques, self.judged, self.rendered, self.sees = [], [], [], [], [], []
        self.priors = []

    def io(self, **kw):
        return types.SimpleNamespace(write=self.write, render=self.render, critic=self.critic, judge=self.judge,
                                     writer="opus", **kw)

    def write(self, prompt):
        if prompt.startswith("Revise"):
            self.revises.append(prompt)
            return self.fix(prompt) if self.fix else "no change"
        self.asked.append(prompt)
        if not self.drafts:
            raise ValueError("nothing came back")
        return self.drafts.pop(0)

    def render(self, items, out, prior=()):
        self.priors.append(list(prior))
        self.rendered.append([os.path.basename(w["path"]) for w in items])
        for w in items:
            if not w["svg"]:
                continue
            w["rendered"] = True
            w["thumb"] = "0a" * 4
            clipped = "CLIP" in w["svg"]
            w["gates"].append({"gate": "clip", "ok": not clipped, "got": "3 px outside" if clipped else "0 px outside",
                               "bar": "bar 0 px outside the viewBox"})
            w["gates"].append({"gate": "blank", "ok": True, "got": "12.0% painted", "bar": "bar ≥ 0.5% painted"})
            stem = os.path.splitext(os.path.basename(w["path"]))[0]
            for theme in works.THEMES:
                with open(os.path.join(out, f"{stem}-{theme}.png"), "wb") as f:
                    f.write(b"png")

    def critic(self, prompt, pictures):
        self.critiques.append(prompt)
        self.sees.append(list(pictures))
        for tag, text in self.defects.items():
            if f'id="{tag}"' in prompt:
                return text
        return "NONE"

    def judge(self, prompt, pictures):
        self.judged.append(prompt)
        first = prompt.split("Drawing B")[0]
        if self.prefer is None:
            return "A"
        return "A" if f'id="{self.prefer}"' in first else "B"


class Grow(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        from sampo import core as c
        from sampo import worksgrow
        self.c, self.g = c, worksgrow
        self.p = c.find_project(self.repo)

    def run_grow(self, fake, **kw):
        kw.setdefault("target", 1)
        kw.setdefault("n", 2)
        kw.setdefault("rounds", 1)
        return self.g.grow(self.p, io=fake.io(), **kw)


class TheGrid(Grow):
    def test_the_grid_is_type_by_subject_by_style_by_technique_and_never_cartoonish(self):
        cells = self.g.cells()
        self.assertGreater(len(cells), 500)
        self.assertEqual(len({tuple(sorted(x.items())) for x in cells}), len(cells), "a cell is listed once")
        for x in cells:
            self.assertEqual(sorted(x), ["style", "subject", "technique", "type"])
        self.assertEqual({x["type"] for x in cells}, set(works.TYPES))
        self.assertEqual({x["style"] for x in cells}, {"crafted", "detailed", "lit", "animated"})
        self.assertFalse([s for s in self.g.STYLES if "cartoon" in s])
        self.assertIn("never cartoonish", self.g.WRITE_SYSTEM)

    def test_a_figure_is_drawn_about_a_chart_or_table_and_an_icon_is_a_still_symbol(self):
        for x in self.g.cells():
            if x["type"] == "figure":
                self.assertIn(x["subject"], self.g.FIGURE_SUBJECTS, x)
            else:
                self.assertNotIn(x["subject"], self.g.FIGURE_SUBJECTS, x)
            if x["type"] == "icon":
                self.assertNotEqual(x["style"], "animated", x)
                self.assertNotIn("phases", x["subject"], x)

    def test_the_thinnest_cell_is_picked_first_and_a_cell_that_keeps_failing_waits(self):
        every = self.g.cells()
        for x in every[2:]:  # all but two cells hold a work
            self.g.add(self.p, x, {"svg": svg(), "type": x["type"], "gates": [], "viewbox": [0, 0, 24, 24]},
                       writer="opus", rounds=0, rank={"place": 1, "of": 1, "points": 0}, drafts=1)
        self.assertEqual({tuple(sorted(x.items())) for x in self.g.pick(self.p, 2)},
                         {tuple(sorted(x.items())) for x in every[:2]})
        self.g.note_tried(self.p, every[0], "all failed")
        self.assertEqual(self.g.pick(self.p, 2)[0], every[1], "the cell that failed goes behind the one never tried")
        self.assertEqual(self.g.counts(self.p)[self.g.key(every[5])], 1)


class Reading(Grow):
    def test_a_draft_is_the_svg_inside_its_prose_and_fence(self):
        self.assertEqual(self.g.extract_svg("Here:\n```svg\n" + svg() + "\n```\nDone."), svg())
        self.assertIsNone(self.g.extract_svg("I cannot draw that."))
        self.assertIsNone(self.g.extract_svg("<svg viewBox='0 0 1 1'><path d='M0 0'/>"), "a cut-off reply is no drawing")

    def test_a_critique_is_a_few_concrete_lines_and_none_is_no_advice(self):
        self.assertEqual(self.g.parse_defects("NONE"), [])
        got = self.g.parse_defects("Looks like:\n- the right wing is clipped at x=24\n* contrast of the gold on cream is weak\n"
                                   "1. the glow is muddy")
        self.assertEqual(got, ["the right wing is clipped at x=24", "contrast of the gold on cream is weak",
                               "the glow is muddy"])
        self.assertLessEqual(len(self.g.parse_defects("\n".join(f"- defect {i}" for i in range(20)))), 5)
        self.assertLessEqual(len(self.g.parse_defects("- " + "x" * 5000)[0]), 300)

    def test_a_judge_reply_is_a_or_b_and_anything_else_is_a_tie(self):
        self.assertEqual([self.g.parse_pick(t) for t in ("A", "B is better", "**B**", "  a.", "both", "", "AB")],
                         ["A", "B", "B", None, None, None, None])


class TheLoop(Grow):
    def test_best_of_n_keeps_the_ranked_best_with_its_writer_rounds_gate_numbers_and_rank(self):
        fake = Fake([svg("one"), svg("two"), svg("three")], prefer="two")
        r = self.run_grow(fake, n=3, rounds=1)
        self.assertEqual((r["added"], r["kept"], r["empty"], r["stopped"]), (1, 1, 0, "target"))
        (row,) = self.g.rows(self.p)
        self.assertEqual(row["id"], "W-000001")
        self.assertIn('id="two"', row["svg"], "the judge's pick is the one kept")
        self.assertEqual((row["writer"], row["rounds"], row["drafts"]), ("opus", 0, 3))
        self.assertEqual(row["rank"]["place"], 1)
        self.assertEqual(row["rank"]["of"], 3)
        self.assertEqual({g["gate"]: g["got"] for g in row["gates"]}["clip"], "0 px outside")
        self.assertTrue(all(g["ok"] is not False for g in row["gates"]))
        self.assertEqual(sorted(row["cell"]), ["grid", "style", "subject", "technique", "type"])
        self.assertEqual(row["cell"]["grid"], "works")
        self.assertEqual(len(fake.asked), 3, "one write call per draft")
        self.assertEqual(len(fake.judged), 6, "each of the three pairs is judged in both orders")

    def test_a_clipped_draft_is_critiqued_from_its_render_revised_and_kept_when_the_revision_passes(self):
        fake = Fake([svg("CLIPa")], defects={"CLIPa": "- the right wing is clipped at x=24"},
                    fix=lambda prompt: svg("fixed"))
        r = self.run_grow(fake, n=1, rounds=2)
        self.assertEqual((r["added"], r["empty"]), (1, 0), r)
        (row,) = self.g.rows(self.p)
        self.assertEqual(row["rounds"], 1)
        self.assertIn('id="fixed"', row["svg"])
        self.assertEqual(len(fake.revises), 1)
        self.assertIn("the right wing is clipped at x=24", fake.revises[0], "the writer is told what the critic saw")
        self.assertIn("clip", fake.revises[0], "and which gate failed, with its number")
        self.assertIn("3 px outside", fake.revises[0])
        self.assertTrue(fake.sees[0] and all(p.endswith(".png") for p in fake.sees[0]), "the critic gets the render")
        self.assertEqual(row["critique"], ["the right wing is clipped at x=24"])
        self.assertEqual(len(fake.rendered), 2, "the draft, then its revision")
        self.assertEqual(row["rank"], {"place": 1, "of": 1, "points": 0})

    def test_a_fixable_defect_is_not_thrown_away_but_a_still_failing_revision_loses_to_nothing(self):
        fake = Fake([svg("CLIPa")], fix=lambda prompt: svg("CLIPb"))
        r = self.run_grow(fake, n=1, rounds=2)
        self.assertEqual((r["added"], r["empty"]), (0, 1))
        self.assertEqual(len(fake.revises), 2, "both rounds are spent on it")
        self.assertEqual(self.g.rows(self.p), [])

    def test_critique_is_advice_a_passing_work_is_not_replaced_by_a_failing_revision(self):
        fake = Fake([svg("fine")], defects={"fine": "- the glow could be softer"}, fix=lambda prompt: svg("CLIPworse"))
        self.run_grow(fake, n=1, rounds=2)
        (row,) = self.g.rows(self.p)
        self.assertIn('id="fine"', row["svg"], "proof decides: the revision failed the clip gate, the first draft stays")
        self.assertEqual(row["rounds"], 0)

    def test_a_critic_that_errors_gates_nothing(self):
        fake = Fake([svg("fine")])
        io = fake.io()
        io.critic = lambda prompt, pictures: (_ for _ in ()).throw(ValueError("nothing came back"))
        r = self.g.grow(self.p, target=1, n=1, rounds=2, io=io)
        self.assertEqual(r["added"], 1)

    def test_a_cell_whose_drafts_all_fail_keeps_nothing_and_the_next_cell_is_tried(self):
        fake = Fake([svg("CLIPa"), svg("CLIPb"), "not a drawing", svg("ok1"), svg("ok2")])
        r = self.run_grow(fake, n=2, rounds=0, target=1)
        self.assertEqual((r["added"], r["empty"], r["cells"]), (1, 1, 2), r)
        (row,) = self.g.rows(self.p)
        self.assertIn(row["svg"].count('id="ok'), (1,))
        tried = self.g.tried(self.p)
        self.assertEqual(len(tried), 1)
        self.assertNotEqual(tried[0]["cell"], {k: v for k, v in row["cell"].items() if k != "grid"})
        self.assertIn("clip", tried[0]["why"])

    def test_every_draft_failing_stores_no_row_for_the_cell(self):
        fake = Fake([svg("CLIPa"), "prose only"])
        r = self.run_grow(fake, n=2, rounds=0, target=1, max_cells=1)
        self.assertEqual((r["added"], r["empty"], r["stopped"]), (0, 1, "cells"))
        self.assertEqual(self.g.rows(self.p), [])

    def test_a_script_in_a_draft_fails_safe_and_what_is_stored_is_the_cleaned_text(self):
        evil = svg("evil", '<script>alert(1)</script>')
        fake = Fake([evil, svg("clean")])
        self.run_grow(fake, n=2, rounds=0)
        (row,) = self.g.rows(self.p)
        self.assertNotIn("script", row["svg"])
        self.assertIn('id="clean"', row["svg"])

    def test_the_target_counts_the_store_so_a_second_run_asks_nothing(self):
        fake = Fake([svg("a1"), svg("a2")])
        self.run_grow(fake, n=1, rounds=0, target=2)
        again = Fake([])
        r = self.run_grow(again, n=1, rounds=0, target=2)
        self.assertEqual((r["added"], r["kept"], r["stopped"], again.asked), (0, 2, "target", []))
        first = self.g.rows(self.p)
        self.assertEqual(len(first), 2)
        self.assertNotEqual(first[0]["cell"], first[1]["cell"], "thinnest first: two works, two cells")

    def test_a_cell_is_tried_once_a_run_so_a_target_past_the_grid_ends_run_out_of_cells(self):
        fake = Fake([svg(f"w{i}") for i in range(8)])
        r = self.run_grow(fake, n=1, rounds=0, target=500, max_cells=3)
        self.assertEqual((r["added"], r["stopped"], r["cells"]), (3, "cells", 3))

    def test_the_call_cap_is_checked_before_a_cell_so_a_cell_is_never_half_paid(self):
        fake = Fake([svg("a1"), svg("a2")])
        need = self.g.calls_needed(2, 1)
        r = self.run_grow(fake, n=2, rounds=1, target=5, max_calls=need)
        self.assertEqual((r["added"], r["stopped"]), (1, "max-calls"))
        self.assertLessEqual(r["calls"], need)

    def test_a_kept_work_hands_its_thumb_to_the_next_render_so_uniqueness_spans_the_store(self):
        fake = Fake([svg("a"), svg("b")])
        self.run_grow(fake, n=1, rounds=0, target=2)
        row = self.g.rows(self.p)[0]
        self.assertEqual(row["thumb"], "0a" * 4)
        self.assertEqual(fake.priors[0], [])
        self.assertEqual(fake.priors[-1], [(row["id"], bytes.fromhex("0a" * 4))])

    def test_a_budget_stop_after_the_drafts_passed_keeps_the_best_finished_draft(self):
        fake = Fake([svg("a"), svg("b")])
        io = fake.io()
        io.critic = lambda prompt, pics: (_ for _ in ()).throw(ValueError("budget: this command would pass the cap"))
        r = self.g.grow(self.p, target=1, n=2, rounds=1, io=io)
        self.assertEqual((r["added"], r["stopped"]), (1, "budget"))
        self.assertEqual(len(self.g.rows(self.p)), 1)

    def test_the_cost_of_a_run_is_told_by_role_before_it_starts(self):
        e = self.g.estimate(3, 1, 10)
        self.assertEqual(e["per_cell"], self.g.calls_needed(3, 1))
        self.assertEqual(e["writer"] + e["critic"] + e["judge"], e["per_cell"])
        self.assertEqual(e["cells"], 10)
        self.assertEqual(e["calls"], 10 * e["per_cell"])
        self.assertGreater(e["usd"], 0)

    def test_the_critic_and_the_judge_run_on_their_own_models(self):
        seen = []
        from sampo import ideas, shot
        from unittest import mock
        def child(feature, system, prompt, model, *a, **k):
            seen.append((k.get("detail"), model))
            return "A"
        with mock.patch.object(shot, "find_browser", lambda *a: ("chrome", "")), \
                mock.patch.object(ideas, "run_child", child):
            io = self.g.default_io(self.p, "opus", critic="haiku", judge="sonnet")
            io.write("x")
            io.critic("x", [])
            io.judge("x", [])
        self.assertEqual(seen, [("write", "opus"), ("critic", "haiku"), ("judge", "sonnet")])

    def test_a_budget_refusal_stops_the_run_and_keeps_nothing_of_the_cell(self):
        fake = Fake([])
        io = fake.io()
        io.write = lambda prompt: (_ for _ in ()).throw(ValueError("budget: this command would pass the cap"))
        r = self.g.grow(self.p, target=1, n=2, rounds=1, io=io)
        self.assertEqual((r["added"], r["stopped"]), (0, "budget"))
        self.assertEqual(self.g.rows(self.p), [])

    def test_three_failed_calls_in_a_row_stop_the_run(self):
        fake = Fake([])  # every write raises "nothing came back"
        r = self.run_grow(fake, n=2, rounds=0, target=5)
        self.assertEqual((r["added"], r["stopped"]), (0, "failed"))
        self.assertLessEqual(r["calls"], 3)

    def test_no_browser_means_nothing_is_proved_and_nothing_kept(self):
        fake = Fake([svg("a")])
        io = fake.io()

        def render(items, out, prior=()):
            raise self.g.NoBrowser("no browser")
        io.render = render
        r = self.g.grow(self.p, target=1, n=1, rounds=0, io=io)
        self.assertEqual((r["added"], r["stopped"]), (0, "browser"))
        self.assertEqual(self.g.rows(self.p), [])

    def test_bad_arguments_are_refused_before_any_call(self):
        fake = Fake([svg("a")])
        for kw in ({"n": 0}, {"n": 99}, {"rounds": -1}, {"rounds": 99}, {"target": 0}, {"max_calls": 0}):
            with self.assertRaises(ValueError, msg=str(kw)):
                self.run_grow(fake, **kw)
        self.assertEqual(fake.asked, [])

    def test_one_grow_at_a_time_a_second_is_refused_and_writes_nothing(self):
        fake = Fake([svg("a")])
        with self.g.grow_lock(self.p):
            r = self.run_grow(fake, n=1, rounds=0)
        self.assertEqual((r["stopped"], r["added"], fake.asked), ("busy", 0, []))
        self.assertIn("another", r["error"])

    def test_a_store_with_a_torn_or_foreign_line_still_reads_and_still_takes_a_row(self):
        d = os.path.join(self.g.rune.root(self.p), "works")
        os.makedirs(d)
        with open(os.path.join(d, "works.jsonl"), "w") as f:
            f.write('{"id": "W-000007", "cell": {"type": "icon"}}\nnot json\n[1]\n{"id": "W-0000')
        self.assertEqual(len(self.g.rows(self.p)), 1)
        self.run_grow(Fake([svg("a")]), n=1, rounds=0, target=2)
        ids = [r["id"] for r in self.g.rows(self.p)]
        self.assertEqual(ids, ["W-000007", "W-000008"])


class TheVerb(Grow):
    def args(self, **kw):
        base = dict(action="grow", files=[], grid="works", target=1, batch=2, rounds=1, max_calls=None, seed=0,
                    writer="opus", critic="sonnet", judge="sonnet", json=True, type=None, out=None)
        base.update(kw)
        return types.SimpleNamespace(**base)

    def test_works_grow_runs_the_loop_and_prints_what_it_kept(self):
        fake = Fake([svg("a"), svg("b")], prefer="b")
        import contextlib
        import io as iolib
        buf = iolib.StringIO()
        with contextlib.redirect_stdout(buf):
            code = self.g.cmd_grow(self.args(), self.p, fake.io())
        self.assertEqual(code, 0)
        out = json.loads(buf.getvalue())
        self.assertEqual((out["added"], out["kept"], out["target"]), (1, 1, 1))
        self.assertEqual(len(self.g.rows(self.p)), 1)

    def test_the_parser_takes_grow_with_its_flags_and_check_still_needs_files(self):
        r = self.fm("works", "grow", "--help")
        for flag in ("--grid", "--target", "--batch", "--rounds", "--max-calls", "--writer", "--critic", "--judge"):
            self.assertIn(flag, r.stdout)
        r = self.fm("works", "check", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("FILE", r.stdout + r.stderr)
        r = self.fm("works", "grow", "x.svg", "--grid", "works", check=False)
        self.assertNotEqual(r.returncode, 0)

    def test_without_a_browser_the_verb_refuses_before_asking_a_model(self):
        r = self.fm("works", "grow", "--grid", "works", "--target", "1", check=False, env={"FOREMAN_CHROME": "none"})
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("browser", r.stdout + r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.g.rune.root(self.p), "works", "works.jsonl")))
