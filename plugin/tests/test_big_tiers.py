"""T-4220 (part A of T-4219): XL, HUGE and GIANT are tiers wherever a tier is read, from one core.TIERS constant.
S, M and L keep their gates, audits and routing; a big task never reaches a builder, and the refusal names the way
forward. Run one group with -k: tiers (accepted), unchanged (S, M, L and no bare literal), lane (never briefed)."""
import json
import os
import re
import unittest

from helpers import ForemanTestCase

from sampo import core as c

BIG = ("XL", "HUGE", "GIANT")
# any case: task new, task set and capture store the canonical name
WORDS = {"XL": ("xl", "Xl", "XL"), "HUGE": ("huge", "Huge", "HUGE"), "GIANT": ("giant", "Giant", "GIANT")}
LIB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib", "sampo")
# the three shapes a bare tier list takes: a tuple/list/set/choices of S, M, L; a dict keyed by them; the "SML" string
BARE = [re.compile(r"""["']S["']\s*(?::\s*[^,}\]\n]+)?,\s*["']M["']\s*(?::\s*[^,}\]\n]+)?,\s*["']L["']"""),
        re.compile(r"""["']SML["']""")]


class BigTiersAccepted(ForemanTestCase):
    """-k tiers: the names exist once, and every entry point accepts them in any case."""

    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)

    def brief(self, tid):
        return next(b for b in c.load_briefs(self.p) if b.id == tid)

    def test_tiers_one_constant_and_its_helpers(self):
        self.assertEqual(c.TIERS, ("S", "M", "L", "XL", "HUGE", "GIANT"))
        self.assertEqual(c.BIG_TIERS, BIG)
        ranks = [c.tier_rank(t) for t in c.TIERS]
        self.assertEqual(ranks, sorted(ranks))
        self.assertEqual(len(set(ranks)), len(c.TIERS))
        self.assertTrue(all(c.is_big(t) for t in BIG))
        self.assertFalse(any(c.is_big(t) for t in ("S", "M", "L")))

    def test_tiers_accepted_by_task_new_in_any_case(self):
        for tier, words in WORDS.items():
            for word in words:
                tid = self.fm("task", "new", f"Big {word}", "--type", "FEATURE", "--tier", word,
                              "--ac", "ok :: true", "--step", "s", "--json").stdout
                tid = json.loads(tid)["id"]
                self.assertEqual(self.brief(tid).tier, tier, word)

    def test_tiers_accepted_by_task_set_in_any_case(self):
        tid = json.loads(self.fm("task", "new", "Grow it", "--type", "FEATURE", "--tier", "S",
                                               "--ac", "ok :: true", "--step", "s", "--json").stdout)["id"]
        self.fm("task", "set", tid, "tier=giant")
        self.assertEqual(self.brief(tid).tier, "GIANT")

    def test_tiers_accepted_by_capture_and_trigger_cap(self):
        from sampo import triggers
        self.fm("capture", "Plan the big thing", "--type", "FEATURE", "--tier", "huge")
        self.assertEqual([b.tier for b in c.load_briefs(self.p) if b.title.startswith("Plan the big")], ["HUGE"])
        self.fm("triggers", "set", "scout", "--tier-cap", "xl")
        self.assertEqual(triggers.row(self.p, "scout")["tier_cap"], "XL")

    def test_tiers_required_audits_for_big_are_l_five(self):
        for tier in BIG:
            self.assertEqual(c.REQUIRED_AUDITS[tier], c.REQUIRED_AUDITS["L"], tier)

    def test_tiers_big_needs_approval_under_standard_not_full(self):
        tid = json.loads(self.fm("task", "new", "Huge plan", "--type", "FEATURE", "--tier", "HUGE",
                                               "--ac", "ok :: true", "--step", "s", "--json").stdout)["id"]
        b = self.brief(tid)
        self.assertTrue(c.needs_approval(b, "standard"))
        self.assertFalse(c.needs_approval(b, "full"))

    def test_tiers_old_tree_reads_xl_brief_without_a_crash(self):
        # an old tree reads "tier: XL" as an unknown tier: every gate that takes a brief's tier must not raise
        tid = json.loads(self.fm("task", "new", "Read me", "--type", "FEATURE", "--tier", "XL",
                                               "--ac", "ok :: true", "--step", "s", "--json").stdout)["id"]
        b = self.brief(tid)
        c.needs_approval(b, "standard")
        c.plan_gaps(b, "standard")
        c.brief_summary(b)
        self.assertEqual(c.tier_rank("NOPE"), c.tier_rank("L"))

    def test_tiers_lowercase_frontmatter_loads_canonical(self):
        tid = json.loads(self.fm("task", "new", "Frontmatter", "--type", "FEATURE", "--tier", "S",
                                               "--ac", "ok :: true", "--step", "s", "--json").stdout)["id"]
        path = self.brief(tid).path
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(re.sub(r"(?m)^tier: .*$", "tier: giant", text, count=1))
        self.assertEqual(self.brief(tid).tier, "GIANT")


class TiersUnchanged(ForemanTestCase):
    """-k unchanged: S, M and L keep their audits, approval and routing, and no bare tier literal is left."""

    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)

    def test_unchanged_s_m_l_required_audits(self):
        self.assertEqual(c.REQUIRED_AUDITS["S"], [{"self"}])
        self.assertEqual(c.REQUIRED_AUDITS["M"], [{"intent"}, {"adversary", "edge", "operator", "maintainer"}])
        self.assertEqual(c.REQUIRED_AUDITS["L"], [{"intent"}, {"adversary"}, {"edge"}, {"operator"}, {"maintainer"}])

    def test_unchanged_s_m_l_approval_under_standard(self):
        import json
        want = {"S": False, "M": False, "L": True}
        for tier, approval in want.items():
            tid = json.loads(self.fm("task", "new", f"Tier {tier}", "--type", "FEATURE", "--tier", tier,
                                     "--ac", "ok :: true", "--step", "s", "--json").stdout)["id"]
            b = next(x for x in c.load_briefs(self.p) if x.id == tid)
            self.assertEqual(bool(c.needs_approval(b, "standard")), approval, tier)
            self.assertFalse(c.needs_approval(b, "full"), tier)

    def test_unchanged_l_still_stays_in_the_main_thread(self):
        import json
        tid = json.loads(self.fm("task", "new", "An L", "--type", "FEATURE", "--tier", "L",
                                 "--ac", "ok :: true", "--step", "s", "--json").stdout)["id"]
        r = self.fm("lane", "brief", tid, check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("an L task stays in the main thread", r.stdout + r.stderr)

    def test_unchanged_no_bare_tier_literal_left(self):
        # every tier list reads core.TIERS (or a rank); core.py alone defines the names
        hits = []
        for root, _dirs, files in os.walk(LIB):
            for name in files:
                if not name.endswith(".py") or name == "core.py":
                    continue
                path = os.path.join(root, name)
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
                for rx in BARE:
                    for m in rx.finditer(text):
                        hits.append(f"{os.path.relpath(path, LIB)}:{text.count(chr(10), 0, m.start()) + 1}")
        self.assertEqual(hits, [], "a bare S/M/L tier list: read core.TIERS instead")


class BigNeverLane(ForemanTestCase):
    """-k lane: a builder is never handed a big task, and the refusal names the way forward."""

    def setUp(self):
        super().setUp()
        self.fm("init")

    def test_lane_brief_refuses_big_with_the_way_forward(self):
        import json
        for tier in BIG:
            tid = json.loads(self.fm("task", "new", f"Big {tier}", "--type", "FEATURE", "--tier", tier,
                                     "--ac", "ok :: true", "--step", "s", "--json").stdout)["id"]
            r = self.fm("lane", "brief", tid, check=False)
            self.assertNotEqual(r.returncode, 0, tier)
            self.assertIn(f"a big task runs through its children: sampo big expand {tid}", r.stdout + r.stderr, tier)
            self.assertNotIn("stays in the main thread", r.stdout + r.stderr, tier)

    def test_lane_brief_ready_never_offers_a_big_task(self):
        import json
        tid = json.loads(self.fm("task", "new", "Giant one", "--type", "FEATURE", "--tier", "GIANT",
                                 "--ac", "ok :: true", "--step", "s", "--json").stdout)["id"]
        r = self.fm("lane", "brief", "--ready", check=False)
        out = r.stdout + r.stderr
        self.assertNotIn(f"sampo:fm-builder {tid}", out)
        self.assertIn("a big task runs through its children", out)


if __name__ == "__main__":
    unittest.main()
