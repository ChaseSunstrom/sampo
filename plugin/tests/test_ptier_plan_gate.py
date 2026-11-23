"""The plan gate reads the process tier (T-5714): plan_gaps asks a trivial or small plan (scope only: docs, tests, a
claimed S) for criteria and steps and nothing more; the cold rival read uses a 60 percent bar for a normal plan and 80
for a risky one; the builder brief names haiku for trivial and small. One-way steps and L still always ask for the read;
FOREMAN_PROCESS_TIER=off restores today's gate."""
import os
import unittest
from unittest import mock

import helpers  # noqa: F401 (puts plugin/lib on the import path before sampo)
from sampo import core as c
from sampo import lanes

AP = "Approach (options → choice → why)"
OPTIONS = ("1) use what exists: the old gate. fails if: it stays. 2) do less: a line. fails if: nothing changes. "
           "3) a new queue. fails if: it is slow. ")
DOCS, TESTS, CODE, SENS = ["docs/guide.md"], ["plugin/tests/test_x.py"], ["plugin/lib/sampo/x.py"], ["plugin/lib/sampo/auth.py"]


def brief(tier="M", scope=CODE, pct=None, steps=("red test",), sections=True, raw="x", type_="FIX", verify="true"):
    b = c.Brief.new("T-0001", "T", type_, tier, now="2026-01-01T00:00:00Z")
    b.set_section("Raw request", "> " + raw)
    if sections:
        b.set_section("Interpretation", "Reading 1: the gate counts. Reading 2: the gate reads the text.")
        b.set_section(AP, OPTIONS + (f"Choice 1 ({pct}%): why." if pct is not None else "Choice 1: why."))
    b.add_ac("it works", verify=verify)
    for s in steps:
        b.add_step(s)
    if scope is not None:
        b.meta["scope"] = scope
    return b


class Env(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.dict(os.environ)
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop("FOREMAN_PROCESS_TIER", None)
        os.environ["FOREMAN_NO_RIVAL"] = ""  # the suite turns the rival gate off; these tests are about it


class PlanTier(Env):
    def test_scope_alone_names_the_tier_for_a_plan(self):
        for tier, scope, want in (("M", DOCS, "trivial"), ("M", TESTS, "trivial"), ("M", ["docs/map"], "trivial"),
                                  ("M", CODE, "normal"), ("M", SENS, "risky"), ("M", DOCS + CODE, "normal"),
                                  ("M", None, "normal"), ("M", [], "normal"), ("S", CODE, "small"), ("L", DOCS, "risky")):
            self.assertEqual(c.plan_ptier(brief(tier, scope))[0], want, (tier, scope))

    def test_a_stored_floor_is_kept_and_a_docs_scope_does_not_lower_it(self):
        b = brief("M", DOCS)
        b.meta["ptier"] = "normal"
        self.assertEqual(c.plan_ptier(b)[0], "normal")
        b.meta["ptier"] = "small"
        self.assertEqual(c.plan_ptier(b)[0], "small")

    def test_the_off_switch_gives_the_claimed_tier(self):
        os.environ["FOREMAN_PROCESS_TIER"] = "off"
        self.assertEqual(c.plan_ptier(brief("M", DOCS))[0], "normal")
        self.assertEqual(c.plan_ptier(brief("S", DOCS))[0], "small")

    def test_a_scope_entry_that_climbs_out_of_tests_or_docs_is_measured_by_where_it_lands(self):
        for scope in (["tests/../plugin/lib/sampo/core.py"], ["docs/../plugin/lib/sampo/core.py"],
                      ["./tests/../plugin/lib/sampo/core.py"], DOCS + ["tests/x/../../plugin/lib/sampo/lanes.py"]):
            self.assertEqual(c.plan_ptier(brief("M", scope))[0], "normal", scope)
            g = " | ".join(c.plan_gaps(brief("M", scope, sections=False), "full"))
            self.assertIn("Interpretation", g, scope)
            self.assertIsNone(lanes.plan_model(brief("M", scope)), scope)
        self.assertEqual(c.plan_ptier(brief("M", ["docs/a/../b.md"]))[0], "trivial")  # it stays in docs

    def test_an_entry_that_escapes_the_repo_or_is_absolute_is_unmeasurable_so_it_keeps_the_claimed_tier(self):
        for scope in (["../outside/x.md"], ["tests/../../x.md"], ["/etc/hosts.md"], ["~/notes.md"], ["docs\\..\\x.py"],
                      ["tests/**/../x.md"], DOCS + ["../x.md"]):
            self.assertEqual(c.plan_ptier(brief("M", scope))[0], "normal", scope)
            self.assertEqual(c.plan_ptier(brief("S", scope))[0], "small", scope)
            self.assertIn("Interpretation", " | ".join(c.plan_gaps(brief("M", scope, sections=False), "full")), scope)

    def test_only_doc_files_read_as_docs(self):
        for scope in (["docs/render.rb"], ["docs/gen.mjs"], ["docs/.vitepress/config.mts"], ["docs/.vitepress/*"],
                      ["CMakeLists.txt"], ["docs/CMakeLists.txt"], ["docs/Makefile"], ["requirements.txt"],
                      ["docs/conf.py"], ["docs/*.rb"], ["docs/guide.md", "docs/render.rb"], ["scripts/docs/build.sh"]):
            self.assertNotIn(c.plan_ptier(brief("M", scope))[0], ("trivial", "small"), scope)  # a build file may read risky
            self.assertIn("Interpretation", " | ".join(c.plan_gaps(brief("M", scope, sections=False), "full")), scope)
        for scope in (["README.md"], ["docs/*"], ["docs/**"], ["docs/guide.rst"], ["docs/a.adoc"], ["docs/notes.txt"],
                      ["docs/map"], ["docs/guide/*.md", "plugin/tests/test_x.py"]):
            self.assertEqual(c.plan_ptier(brief("M", scope))[0], "trivial", scope)


class PlanGate(Env):
    def test_a_docs_or_tests_scope_m_plan_with_no_readings_or_options_passes(self):
        for scope in (DOCS, TESTS):
            self.assertEqual(c.plan_gaps(brief("M", scope, sections=False), "full"), [], scope)

    def test_a_code_scope_m_plan_still_names_its_gaps(self):
        g = " | ".join(c.plan_gaps(brief("M", CODE, sections=False), "full"))
        self.assertIn("Interpretation", g)
        self.assertIn("Approach", g)
        thin = brief("M", CODE)
        thin.set_section(AP, "1) one thing. Choice 1 (90%): why.")
        self.assertIn("3+ options", " | ".join(c.plan_gaps(thin, "full")))

    def test_a_docs_plan_still_needs_criteria_and_steps(self):
        b = c.Brief.new("T-0002", "T", "FIX", "M", now="2026-01-01T00:00:00Z")
        b.set_section("Raw request", "> x")
        b.meta["scope"] = DOCS
        g = " | ".join(c.plan_gaps(b, "full"))
        self.assertIn("acceptance criterion", g)
        self.assertIn("step", g)

    def test_a_claimed_s_plan_is_gated_as_before(self):
        self.assertEqual(c.plan_gaps(brief("S", CODE, sections=False), "full"), [])

    def test_the_off_switch_restores_todays_gate(self):
        os.environ["FOREMAN_PROCESS_TIER"] = "off"
        g = " | ".join(c.plan_gaps(brief("M", DOCS, sections=False), "full"))
        self.assertIn("Interpretation", g)
        self.assertIn("Approach", g)
        self.assertIn("80%", " | ".join(c.plan_gaps(brief("M", DOCS, pct=70), "full")), "off: the old bar of 80")

    def test_a_light_m_plan_skips_the_verify_command_and_recall_clause_checks_a_code_plan_keeps_them(self):
        raw = "make the gate lighter. also name the bar in the docs. and add a table of tiers."
        for scope, asks in ((DOCS, False), (TESTS, False), (CODE, True)):
            b = brief("M", scope, pct=90, raw=raw, verify=None)
            b.meta["source"] = "user"
            g = " | ".join(c.plan_gaps(b, "full"))
            self.assertEqual("verify command on criterion 1" in g, asks, (scope, g))
            self.assertEqual("a criterion for" in g, asks, (scope, g))

    def test_a_low_assumption_on_a_light_m_plan_waits_for_evidence_like_an_s_plan_and_a_plain_one_does_not(self):
        for line, asks in (("- [assumed] the guide lists the bar (low)", True), ("- [assumed] the guide lists the bar (40%)", False)):
            b = brief("M", DOCS, sections=False)
            b.set_section(b.ASSUMPTIONS, line)
            g = " | ".join(c.plan_gaps(b, "full"))
            self.assertEqual("low-confidence assumption" in g, asks, (line, g))
            self.assertNotIn("research bar", g, line)
        code = brief("M", CODE, pct=90)
        code.set_section(code.ASSUMPTIONS, "- [assumed] the gate counts (40%)")
        self.assertIn("research bar", " | ".join(c.plan_gaps(code, "full")))

    def test_l_keeps_its_gaps_whatever_the_scope(self):
        self.assertIn("Risks and rollback", " | ".join(c.plan_gaps(brief("L", DOCS), "full")))


class RivalBar(Env):
    def test_a_normal_plan_at_70_asks_no_rival_read_and_one_at_55_does(self):
        self.assertEqual(c.rival_gap(brief("M", CODE, pct=70)), [])
        self.assertEqual(c.rival_gap(brief("M", CODE, pct=60)), [])
        g = c.rival_gap(brief("M", CODE, pct=55))
        self.assertEqual(len(g), 1, g)
        self.assertIn("under 60%", g[0])

    def test_a_sensitive_scope_plan_asks_one_at_70(self):
        g = c.rival_gap(brief("M", SENS, pct=70))
        self.assertEqual(len(g), 1, g)
        self.assertIn("under 80%", g[0])
        self.assertEqual(c.rival_gap(brief("M", SENS, pct=85)), [])

    def test_a_trivial_plan_asks_no_rival_read_for_its_confidence(self):
        self.assertEqual(c.rival_gap(brief("M", DOCS, pct=30)), [])

    def test_a_one_way_step_asks_for_it_at_every_tier(self):
        step = ("deploy the build",)
        for tier, scope in (("M", DOCS), ("S", CODE), ("M", CODE), ("M", SENS), ("L", CODE)):
            g = c.rival_gap(brief(tier, scope, pct=95, steps=step))
            self.assertEqual(len(g), 1, (tier, scope, g))
            self.assertIn("an L plan" if tier == "L" else "one-way step", g[0])

    def test_an_l_plan_always_asks(self):
        self.assertIn("an L plan", c.rival_gap(brief("L", DOCS, pct=95))[0])

    def test_the_off_switch_keeps_the_old_bar_of_80(self):
        os.environ["FOREMAN_PROCESS_TIER"] = "off"
        g = c.rival_gap(brief("M", CODE, pct=70))
        self.assertEqual(len(g), 1, g)
        self.assertIn("under 80%", g[0])

    def test_the_refusal_text_names_the_bar_and_the_off_switch_says_80_for_every_plan(self):
        g = " | ".join(c.plan_gaps(brief("M", CODE, sections=False), "full"))
        self.assertIn("under 60%", g)
        self.assertIn("80% on a sensitive scope", g)
        os.environ["FOREMAN_PROCESS_TIER"] = "off"
        g = " | ".join(c.plan_gaps(brief("M", CODE, sections=False), "full"))
        self.assertIn("under 80%", g)
        self.assertNotIn("60%", g)
        self.assertEqual((c.RACE_BAR_NORMAL, c.RACE_BAR), (60, 80))


class BuilderModel(Env):
    def test_trivial_and_small_pick_haiku_at_low_effort(self):  # T-6124: the light scope launches at low effort
        for tier in ("trivial", "small"):
            self.assertEqual(lanes.light_model(tier), ("haiku", "low"), tier)
        for tier in ("normal", "risky"):
            self.assertIsNone(lanes.light_model(tier), tier)

    def test_the_brief_for_a_docs_plan_names_haiku_and_off_restores_the_route(self):
        self.assertEqual(lanes.plan_model(brief("M", DOCS)), ("haiku", "low", "ptier trivial"))
        self.assertEqual(lanes.plan_model(brief("S", CODE)), ("haiku", "low", "ptier small"))
        self.assertIsNone(lanes.plan_model(brief("M", CODE)))
        os.environ["FOREMAN_PROCESS_TIER"] = "off"
        self.assertIsNone(lanes.plan_model(brief("S", CODE)), "off: today's route, sonnet unless the history says otherwise")
        self.assertIsNone(lanes.plan_model(brief("M", DOCS)))


if __name__ == "__main__":
    unittest.main()
