"""The close and accept read the process tier (T-5715, T-5704 part c). core.required_audits(b, files, diff) replaces the
direct REQUIRED_AUDITS reads: trivial keeps the self audit only, small asks one combined group for a claimed M, normal is
today's M groups and risky today's L. Docs impact, a lesson and why-not-caught are owed at normal and risky only. The
adversary lens for sensitive code is asked at every tier, in the close and in accept. The tier is stamped on the brief,
only ever raised; gates and audit prep name it.
reruns: python3 plugin/tests/run.py test_ptier_audits"""
# map-links: plugin/lib/sampo/core.py plugin/lib/sampo/cli.py plugin/lib/sampo/train.py
import contextlib
import io
import os
import types
import unittest
from unittest import mock

from helpers import ForemanTestCase, valid_m_plan  # noqa: F401  (puts plugin/lib on the import path)
from test_accept_check import AcceptCase  # the lane fixture accept's own tests use
from test_lane_land import OLD

from sampo import cli
from sampo import core as c
from sampo import train

DOCS = "docs/guide.md"
A, B, C, D = "plugin/lib/sampo/a.py", "plugin/lib/sampo/b.py", "plugin/lib/sampo/c.py", "plugin/lib/sampo/d.py"
TA = "plugin/tests/test_a.py"
AUTH = "plugin/lib/sampo/auth.py"
ONE_LENS = {"intent", "adversary", "edge", "operator", "maintainer"}


def patch(path, *lines):
    return (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1,1 +1,1 @@\n"
            + "".join(line + "\n" for line in lines))


def code(path, n=3):
    return patch(path, *[f"+value_{i} = {i}" for i in range(n)])


def brief(tier, type_="FIX", scope=(A,), ptier=None):
    b = c.Brief.new("T-0001", "Fix login", type_, tier, now="2026-01-01T00:00:00Z", scope=list(scope))
    if ptier is not None:
        b.meta["ptier"] = ptier
    return b


def fields(**kw):
    return types.SimpleNamespace(**{"docs": None, "lesson": None, "why_not_caught": None, "numbers": None, **kw})


class NoOff(unittest.TestCase):
    """The score is the subject here, so FOREMAN_PROCESS_TIER is off the table unless a test sets it."""

    def setUp(self):
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("FOREMAN_PROCESS_TIER", None)


class TierOn:
    """T-5714: SampoTestCase runs every test with FOREMAN_PROCESS_TIER=off (today's S, M and L plan gate); the lanes and
    hints here are about the score, so their setUp turns it back on (the subprocess fm calls inherit the env)."""

    def setUp(self):
        super().setUp()
        os.environ.pop("FOREMAN_PROCESS_TIER", None)


class GroupsPerTier(NoOff):
    def groups(self, tier, files, diff, linked=None, type_="FIX", ptier=None):
        return c.required_audits(brief(tier, type_, scope=files, ptier=ptier), files, diff, linked)

    def test_trivial_keeps_the_self_audit_only(self):
        self.assertEqual(self.groups("M", [DOCS], patch(DOCS, "+A line.")), [{"self"}])
        self.assertEqual(self.groups("M", [A, TA], code(A) + code(TA)), c.REQUIRED_AUDITS["M"])  # code is not trivial
        self.assertEqual(self.groups("M", [TA], code(TA)), [{"self"}])  # a test file alone is

    def test_small_asks_one_combined_group_for_a_claimed_m(self):
        got = self.groups("M", [A], code(A), {A: [TA]})
        self.assertEqual(got, [ONE_LENS])
        self.assertEqual(len(got), 1)

    def test_small_for_a_claimed_s_is_the_self_audit(self):
        self.assertEqual(self.groups("S", [A], code(A)), [{"self"}])

    def test_normal_is_todays_m_groups(self):
        self.assertEqual(self.groups("M", [A], code(A)), c.REQUIRED_AUDITS["M"])

    def test_risky_is_todays_l_groups(self):
        # a claimed M that the change itself scores risky (a sensitive diff) keeps its own groups: the adversary lens is
        # asked on top (Adversary class), so the process tier never asks a small claim for more lenses than it did
        self.assertEqual(self.groups("M", [A], patch(A, "+value = pickle.loads(blob)")), c.REQUIRED_AUDITS["M"])
        self.assertEqual(self.groups("S", [A], patch(A, "+value = pickle.loads(blob)")), c.REQUIRED_AUDITS["S"])
        self.assertEqual(self.groups("L", [DOCS], patch(DOCS, "+A line.")), c.REQUIRED_AUDITS["L"])
        self.assertEqual(self.groups("XL", [A], code(A), {A: [TA]}), c.REQUIRED_AUDITS["L"])

    def test_required_audits_itself_is_unchanged(self):  # smith.py reads it: stricter is safe there
        self.assertEqual(c.REQUIRED_AUDITS["S"], [{"self"}])
        self.assertEqual(len(c.REQUIRED_AUDITS["M"]), 2)
        self.assertEqual(len(c.REQUIRED_AUDITS["L"]), 5)

    def test_a_stored_tier_is_the_floor_of_the_groups(self):
        self.assertEqual(self.groups("M", [DOCS], patch(DOCS, "+A line."), ptier="risky"), c.REQUIRED_AUDITS["M"])
        self.assertEqual(self.groups("S", [DOCS], patch(DOCS, "+A line."), ptier="normal"), c.REQUIRED_AUDITS["M"])
        self.assertEqual(self.groups("M", [DOCS], patch(DOCS, "+A line."), ptier="trivial"), [{"self"}])

    def test_off_gives_todays_s_m_and_l(self):
        with mock.patch.dict(os.environ, {"FOREMAN_PROCESS_TIER": "off"}):
            docs = patch(DOCS, "+A line.")
            self.assertEqual(self.groups("S", [DOCS], docs), [{"self"}])
            self.assertEqual(self.groups("M", [DOCS], docs), c.REQUIRED_AUDITS["M"])
            self.assertEqual(self.groups("L", [DOCS], docs), c.REQUIRED_AUDITS["L"])

    def test_no_files_read_the_scope_and_the_stored_tier_stands(self):
        # a reader with no diff (the stage, the land's field check): the tier scored from a real diff stands
        self.assertEqual(c.required_audits(brief("M", scope=["docs/**"])), c.REQUIRED_AUDITS["M"])
        self.assertEqual(c.required_audits(brief("M", scope=["docs/**"], ptier="trivial")), [{"self"}])
        self.assertEqual(c.required_audits(brief("L", scope=["docs/**"], ptier="trivial")), c.REQUIRED_AUDITS["L"])
        self.assertEqual(c.required_audits(brief("S", scope=["docs/**"])), [{"self"}])

    def test_blockers_follow_the_groups(self):
        b = brief("M")
        self.assertEqual(b.audit_blockers(groups=[{"self"}]), ["audit missing: self"])
        b.add_audit("self", "read it", "no findings")
        self.assertEqual(b.audit_blockers(groups=[{"self"}]), [])
        self.assertEqual(len(brief("M").audit_blockers(groups=c.REQUIRED_AUDITS["M"])), 2)
        self.assertEqual(len(brief("M").audit_blockers()), 2)  # no groups: the claimed tier's, as before

    def test_audit_progress_counts_the_tiers_groups(self):
        self.assertEqual(c.audit_progress(brief("M"))["required"], 2)
        self.assertEqual(c.audit_progress(brief("M", ptier="trivial"))["required"], 1)
        self.assertEqual(c.audit_progress(brief("S"))["required"], 1)
        self.assertEqual(c.audit_progress(brief("L"))["required"], 5)


class TheClose(NoOff):
    """What the close refuses (cli._finish_gaps, done_blockers) for the tier the change scores."""

    def tier_of(self, b, files, diff, linked=None):
        return c.ptier_now(b, files, diff, linked)[0]

    def test_a_doc_only_m_closes_with_no_lens_docs_impact_or_lesson(self):
        b = brief("M", scope=[DOCS])
        ptier = self.tier_of(b, [DOCS], patch(DOCS, "+A line."))
        self.assertEqual(ptier, "trivial")
        self.assertEqual(cli._finish_gaps(b, fields(), [], ptier=ptier), [])
        self.assertEqual([g for g in cli._field_gaps(b, fields(), ptier=ptier)], [])

    def test_a_doc_only_m_fix_owes_no_why_not_caught(self):
        self.assertEqual(cli._field_gaps(brief("M", "FIX"), fields(), ptier="trivial"), [])

    def test_done_blockers_of_a_doc_only_m_name_no_docs_or_lens(self):
        b = brief("M", scope=[DOCS])
        b.add_audit("self", "read it", "no findings")
        reasons = b.done_blockers(ptier="trivial")
        self.assertFalse([r for r in reasons if r.startswith("docs impact") or r.startswith("audit missing")], reasons)

    def test_the_same_m_at_normal_still_owes_all_of_it(self):
        b = brief("M")
        gaps = cli._finish_gaps(b, fields(), [], ptier="normal")
        text = "\n".join(gaps)
        for owed in ("audit missing: --lens 'intent", "docs impact missing", "lesson missing", "why-not-caught missing"):
            self.assertIn(owed, text)
        self.assertIn("docs impact not recorded", "\n".join(b.done_blockers(ptier="normal")))

    def test_small_owes_one_lens_and_no_docs_lesson_or_why(self):
        b = brief("M")
        gaps = cli._finish_gaps(b, fields(), [], ptier="small")
        self.assertEqual(len(gaps), 1, gaps)
        self.assertIn("audit missing: --lens 'adversary: <result>' (or edge, intent, maintainer, operator)", gaps[0])
        self.assertEqual(cli._finish_gaps(b, fields(), [("edge", "no findings")], ptier="small"), [])

    def test_risky_owes_the_five_lenses(self):
        gaps = cli._audit_gaps(brief("L"), [], brief("L").audit_blockers(groups=c.audit_groups("L", "risky")), ptier="risky")
        self.assertEqual(len(gaps), 5, gaps)

    def test_a_claimed_s_still_gives_the_self_audit_free(self):
        b = brief("S")
        self.assertEqual(cli._finish_gaps(b, fields(), [], ptier="small"), [])

    def test_an_m_that_scores_normal_without_a_lens_is_refused_as_today(self):
        gaps = cli._audit_gaps(brief("M"), [], brief("M").audit_blockers(), ptier="normal")
        self.assertEqual(len(gaps), 2, gaps)

    def test_the_close_fields_follow_the_stored_tier_with_no_diff(self):  # the land's check reads the brief alone
        self.assertEqual(cli._field_gaps(brief("M", ptier="trivial"), fields()), [])
        self.assertTrue(cli._field_gaps(brief("M"), fields()))
        self.assertTrue(cli._field_gaps(brief("L", ptier="trivial"), fields()))

    def test_docs_gap_follows_the_tier(self):
        self.assertFalse(brief("M").docs_gap(ptier="trivial"))
        self.assertFalse(brief("M").docs_gap(ptier="small"))
        self.assertTrue(brief("M").docs_gap(ptier="normal"))
        self.assertTrue(brief("M").docs_gap(ptier="risky"))
        self.assertTrue(brief("L").docs_gap(ptier="risky"))
        self.assertFalse(brief("S").docs_gap(ptier="risky"))  # a sensitive S owes no more than the S claim did
        self.assertTrue(brief("M").docs_gap())  # no tier given: the claimed M, as before
        self.assertFalse(brief("S").docs_gap())


class Adversary(NoOff):
    """Sensitive code needs the adversary lens at every tier."""

    def test_the_close_asks_for_it_at_every_tier(self):
        for tier in ("S", "M", "L"):
            for stored in (None, "trivial", "small", "normal", "risky"):
                with self.subTest(tier=tier, stored=stored):
                    b = brief(tier, ptier=stored)
                    got = cli._adversary_gaps(None, b, [], [AUTH])
                    self.assertTrue(any(g.startswith("audit missing: adversary") for g in got), got)

    def test_a_given_adversary_lens_covers_it(self):
        self.assertEqual(cli._adversary_gaps(None, brief("M"), [("adversary", "tried it")], [AUTH]), [])

    def test_the_need_is_added_to_the_groups_of_each_tier(self):
        for ptier in c.PTIERS:
            with self.subTest(ptier=ptier):
                got = brief("M").audit_blockers(groups=c.audit_groups("M", ptier), need=("adversary",))
                self.assertTrue(any(g.startswith("audit missing: adversary") for g in got), got)

    def test_a_sensitive_change_never_scores_below_risky(self):
        for tier in ("S", "M"):
            ptier = c.ptier_now(brief(tier), [A], patch(A, "+value = pickle.loads(blob)"))[0]
            self.assertEqual(ptier, "risky")


class Accept(TierOn, AcceptCase):
    """train._accept_gaps judges the lane's own diff the way the close does."""

    def gaps(self, tid, rows=(), **kw):
        return train._accept_gaps(self.brief(tid), os.path.join(self.tmp, "lane1"), self.repo, list(rows), p=self.p, **kw)

    def test_a_doc_only_m_lane_needs_no_lens(self):
        tid = self.lane(1, "guide.md", when=OLD, tier="M")
        self.assertEqual(self.gaps(tid), [])

    def test_a_code_m_lane_with_no_linked_test_needs_todays_two_groups(self):
        tid = self.lane(1, "mod.py", when=OLD, tier="M")
        got = self.gaps(tid)
        self.assertEqual(len(got), 2, got)
        self.assertEqual(self.gaps(tid, [("intent", "x", "y"), ("edge", "x", "y")]), [])

    def test_a_code_m_lane_with_a_linked_test_asks_one_combined_group(self):
        tid = self.lane(1, "mod.py", when=OLD, tier="M")
        linked = {"tests": {"test_mod.py": ["mod.py"]}}
        b = self.brief(tid)  # T-5715 review: the linked test must have run green in the task's evidence
        b.add_evidence("python3 plugin/tests/run.py test_mod", "exit 0 · OK", step=1, ran=True)
        c.save_brief(self.p, b)
        with mock.patch("sampo.map.load", return_value=linked):
            got = self.gaps(tid)
            self.assertEqual(len(got), 1, got)
            self.assertEqual(self.gaps(tid, [("operator", "x", "y")]), [])

    def test_the_adversary_lens_is_asked_for_a_sensitive_lane_at_every_claimed_tier(self):
        for n, tier in enumerate(("S", "M"), 1):  # a claimed L needs approval to start: the core tests hold it
            with self.subTest(tier=tier):
                tid = self.lane(n, f"auth{n}.py", when=OLD, tier=tier)
                got = train._accept_gaps(self.brief(tid), os.path.join(self.tmp, f"lane{n}"), self.repo, [], p=self.p)
                self.assertTrue(any(g.startswith("audit missing: adversary (security-sensitive") for g in got), got)

    def test_accept_stamps_the_tier_and_its_reasons_on_the_brief(self):
        tid = self.lane(1, "guide.md", when=OLD, tier="M")
        self.accept(tid)
        meta = self.meta_of(tid)
        self.assertEqual(meta.get("ptier"), "trivial")
        self.assertIn("docs", meta.get("ptier_why", ""))

    def test_accept_never_lowers_a_stored_tier(self):
        tid = self.lane(1, "guide.md", when=OLD, tier="M")
        b = self.brief(tid)
        b.meta["ptier"] = "risky"
        c.save_brief(self.p, b)
        args = types.SimpleNamespace(audit=[[lens, "x", "y"] for lens in c.DEFAULT_LENSES], json=False, no_check=False)
        with contextlib.redirect_stdout(io.StringIO()):  # a stored risky tier: the doc-only lane owes the full groups
            train.accept(self.p, self.brief(tid), self.repo, args)
        self.assertEqual(self.meta_of(tid).get("ptier"), "risky")


class Stamp(NoOff):
    def test_a_stamp_is_only_raised(self):
        b = brief("M")
        self.assertTrue(c.stamp_ptier(b, "small", ["M, 1 file(s)"]))
        self.assertEqual((b.meta["ptier"], b.meta["ptier_why"]), ("small", "M, 1 file(s)"))
        self.assertFalse(c.stamp_ptier(b, "trivial", ["docs only"]))
        self.assertEqual(b.meta["ptier"], "small")
        self.assertFalse(c.stamp_ptier(b, "small", ["again"]))
        self.assertEqual(b.meta["ptier_why"], "M, 1 file(s)")
        self.assertTrue(c.stamp_ptier(b, "risky", ["sensitive: auth.py"]))
        self.assertEqual((b.meta["ptier"], b.meta["ptier_why"]), ("risky", "sensitive: auth.py"))
        self.assertFalse(c.stamp_ptier(b, "normal", ["claimed M"]))
        self.assertEqual(b.meta["ptier"], "risky")

    def test_a_value_that_is_not_a_tier_is_not_stamped_and_a_stored_one_that_is_not_is_replaced(self):
        b = brief("M")
        self.assertFalse(c.stamp_ptier(b, "bogus"))
        self.assertNotIn("ptier", b.meta)
        b.meta["ptier"] = "bogus"
        self.assertTrue(c.stamp_ptier(b, "trivial", ["docs only"]))
        self.assertEqual(b.meta["ptier"], "trivial")

    def test_the_reasons_are_one_bounded_line(self):
        b = brief("M")
        c.stamp_ptier(b, "normal", ["a\nb"] + ["x" * 100] * 10)
        self.assertNotIn("\n", b.meta["ptier_why"])
        self.assertLessEqual(len(b.meta["ptier_why"]), 300)

    def test_off_stamps_nothing(self):
        with mock.patch.dict(os.environ, {"FOREMAN_PROCESS_TIER": "off"}):
            b = brief("M")
            self.assertFalse(c.stamp_ptier(b, "normal", ["claimed M"]))
            self.assertNotIn("ptier", b.meta)


class GatesAndPrep(NoOff):
    def test_gates_name_the_tier_and_its_audits(self):
        need = dict(cli.gates("FIX", "M", "trivial"))
        self.assertEqual(need["audits"].split(",")[0].split(" recorded")[0], "self")
        self.assertNotIn("docs impact", need)
        self.assertNotIn("lesson", need)
        self.assertNotIn("why-not-caught", need)
        self.assertEqual(need["process tier"].split(" ")[0], "trivial")

    def test_gates_at_normal_are_todays_m(self):
        need = dict(cli.gates("FIX", "M", "normal"))
        self.assertTrue(need["audits"].startswith("intent + adversary or edge or maintainer or operator"))
        for k in ("docs impact", "lesson", "why-not-caught"):
            self.assertIn(k, need)

    def test_gates_at_small_ask_one_group_and_at_risky_five(self):
        self.assertTrue(dict(cli.gates("FEATURE", "M", "small"))["audits"].startswith(
            "adversary or edge or intent or maintainer or operator"))
        self.assertNotIn("lesson", dict(cli.gates("FEATURE", "M", "small")))
        self.assertTrue(dict(cli.gates("FEATURE", "L", "risky"))["audits"].startswith("intent + adversary + edge"))

    def test_gates_with_no_tier_read_the_claimed_one(self):
        self.assertEqual(cli.gates("FEATURE", "M"), cli.gates("FEATURE", "M", "normal"))
        self.assertEqual(cli.gates("FEATURE", "S"), cli.gates("FEATURE", "S", "small"))
        self.assertEqual(cli.gates("FEATURE", "L"), cli.gates("FEATURE", "L", "risky"))


class UnreadableDiff(NoOff):
    """T-5715 review 1: a change whose diff can't be read is never lighter than its claim, whatever was stamped."""

    def test_the_close_reads_the_claim_when_the_diff_is_unreadable(self):
        self.assertEqual(cli._task_tier(None, brief("M", ptier="trivial"), {A: "t"}, None)[0], "normal")
        self.assertEqual(cli._task_tier(None, brief("S", ptier="trivial"), {A: "t"}, None)[0], "small")
        self.assertEqual(cli._task_tier(None, brief("M"), {A: "t"}, None)[0], "normal")
        self.assertEqual(cli._task_tier(None, brief("L", ptier="trivial"), {A: "t"}, None)[0], "risky")

    def test_a_stored_tier_above_the_claim_stands(self):
        self.assertEqual(cli._task_tier(None, brief("M", ptier="risky"), {A: "t"}, None)[0], "risky")
        self.assertEqual(cli._task_tier(None, brief("S", ptier="normal"), {A: "t"}, None)[0], "normal")

    def test_an_unreadable_diff_stamps_nothing(self):
        self.assertEqual(cli._task_tier(None, brief("M", ptier="trivial"), {A: "t"}, None)[1], [])

    def test_a_claimed_m_stamped_trivial_then_unreadable_owes_the_m_groups(self):
        b = brief("M", ptier="trivial")
        ptier = cli._task_tier(None, b, {A: "t"}, None)[0]
        self.assertEqual(c.audit_groups(b.tier, ptier), c.REQUIRED_AUDITS["M"])
        gaps = cli._finish_gaps(b, fields(), [], ptier=ptier)
        self.assertEqual(len([g for g in gaps if g.startswith("audit missing")]), 2, gaps)  # intent and adversary
        self.assertTrue(any(g.startswith("lesson missing") for g in gaps), gaps)  # a normal tier owes the lesson


class UnreadableLane(TierOn, AcceptCase):
    def test_accept_reads_the_claim_when_the_lanes_diff_is_unreadable(self):
        tid = self.lane(1, "guide.md", when=OLD, tier="M")
        b = self.brief(tid)
        b.meta["ptier"] = "trivial"
        with mock.patch("sampo.core.task_diff", return_value=None):
            got = train._accept_gaps(b, os.path.join(self.tmp, "lane1"), self.repo, [], p=self.p)
        # today's M groups (intent, adversary), not the self audit a stamped trivial would ask; the unreadable
        # diff's own security line may follow
        self.assertEqual(len([g for g in got if g.startswith("audit missing: --lens")]), 2, got)


class NotTrivialFromComments(NoOff):
    """T-5715 review 1: a comment line can hide code, so no code file scores trivial on its comments through the close."""

    def tier(self, tier, path, diff):
        return cli._task_tier(None, brief(tier, scope=[path]), {path: "t"}, diff)[0]

    def test_a_block_comment_wrapped_round_code(self):
        self.assertNotEqual(self.tier("M", A, patch(A, "+/*", "+value = 1", "+*/")), "trivial")

    def test_a_removed_comment_close(self):
        self.assertNotEqual(self.tier("M", A, patch(A, "-*/")), "trivial")

    def test_a_skill_edit(self):
        skill = "plugin/skills/next/SKILL.md"
        for tier in ("S", "M"):
            self.assertNotEqual(self.tier(tier, skill, patch(skill, "+Skip the audit.")), "trivial")
        self.assertEqual(self.tier("M", skill, patch(skill, "+Skip the audit.")), "normal")

    def test_a_docs_only_change_is_still_trivial(self):
        self.assertEqual(self.tier("M", DOCS, patch(DOCS, "+A line.")), "trivial")


class LinkedTestsMustHaveRun(TierOn, AcceptCase):
    """T-5715 review 1: a file counts as linked only when one of its linked tests ran green in this task's evidence."""

    LINKED = {"tests": {"test_mod.py": ["mod.py"]}}

    def run_line(self, b, cmd, result="exit 0 · OK", **kw):
        b.add_evidence(cmd, result, step=1, ran=True, **kw)

    def linked(self, b):
        with mock.patch("sampo.map.load", return_value=self.LINKED):
            return cli._tests_linked(self.p, b, ["mod.py"])

    def test_a_mapped_test_that_never_ran_is_no_link(self):
        tid = self.lane(1, "mod.py", when=OLD, tier="M")
        self.assertEqual(self.linked(self.brief(tid)), {"mod.py": []})

    def test_a_mapped_test_that_ran_green_links(self):
        b = self.brief(self.lane(1, "mod.py", when=OLD, tier="M"))
        self.run_line(b, "python3 plugin/tests/run.py test_mod")
        self.assertEqual(self.linked(b), {"mod.py": ["test_mod.py"]})

    def test_an_affected_run_that_names_it_links(self):
        b = self.brief(self.lane(1, "mod.py", when=OLD, tier="M"))
        self.run_line(b, c.AFFECTED_LABEL + "python3 plugin/tests/run.py test_other test_mod")
        self.assertEqual(self.linked(b), {"mod.py": ["test_mod.py"]})

    def test_a_red_run_is_no_link(self):
        b = self.brief(self.lane(1, "mod.py", when=OLD, tier="M"))
        self.run_line(b, "python3 plugin/tests/run.py test_mod", result="✗ exit 1 · FAILED")
        self.assertEqual(self.linked(b), {"mod.py": []})

    def test_a_run_naming_another_test_is_no_link(self):
        b = self.brief(self.lane(1, "mod.py", when=OLD, tier="M"))
        self.run_line(b, "python3 plugin/tests/run.py test_module_two")
        self.assertEqual(self.linked(b), {"mod.py": []})

    def test_a_typed_line_that_ran_nothing_is_no_link(self):
        b = self.brief(self.lane(1, "mod.py", when=OLD, tier="M"))
        b.add_evidence("python3 plugin/tests/run.py test_mod", "exit 0 · OK", step=1)  # no [ran] mark
        self.assertEqual(self.linked(b), {"mod.py": []})

    def test_a_mapped_file_whose_test_never_ran_still_owes_intent(self):
        tid = self.lane(1, "mod.py", when=OLD, tier="M")
        with mock.patch("sampo.map.load", return_value=self.LINKED):
            got = train._accept_gaps(self.brief(tid), os.path.join(self.tmp, "lane1"), self.repo, [], p=self.p)
        self.assertEqual(len(got), 2, got)
        self.assertTrue(any("intent" in g for g in got), got)

    def test_a_mapped_file_whose_test_ran_asks_one_group(self):
        tid = self.lane(1, "mod.py", when=OLD, tier="M")
        b = self.brief(tid)
        self.run_line(b, "python3 plugin/tests/run.py test_mod")
        with mock.patch("sampo.map.load", return_value=self.LINKED):
            got = train._accept_gaps(b, os.path.join(self.tmp, "lane1"), self.repo, [], p=self.p)
        self.assertEqual(len(got), 1, got)


class NextHint(TierOn, ForemanTestCase):
    """T-5715 review 1: the stage and the next action follow the tier the close will use, not the unmeasured claim."""

    def ready(self, tier="M", touch=DOCS):
        self.fm("init")
        self.fm("task", "new", "Change docs", "--type", "FEATURE", "--tier", tier, "--ac", "ok :: true", "--step", "s")
        interpretation, approach = valid_m_plan()
        self.fm("task", "set", "T-0001", "--section", "Interpretation", "--text", interpretation)
        self.fm("task", "set", "T-0001", "--section", "Approach (options → choice → why)", "--text", approach)
        self.fm("focus", "T-0001")
        self.fm("task", "evidence", "T-0001", "--step", "1", "--run", "true")
        self.fm("task", "ac", "T-0001", "check", "1", "--evidence", "true", "ok")
        p = c.find_project(self.repo)
        if touch:
            c.log_event(p, "touched", task="T-0001", data={"file": os.path.join(p.root, touch)})
        return p

    def hint(self, p):
        b = c.find_brief(p, "T-0001")
        return c.next_for(p, c.load_briefs(p))[2], c.stage(b, "standard", None), b

    def test_a_docs_only_m_is_not_asked_for_lenses_docs_or_a_lesson(self):
        p = self.ready(touch=DOCS)
        text = c.next_for(p, c.load_briefs(p))[2]
        for owed in ("--lens", "--docs", "--lesson", "Docs impact"):
            self.assertNotIn(owed, text)

    def test_a_code_m_is_still_asked_for_docs_and_both_lenses(self):
        p = self.ready(touch="plugin/lib/sampo/a.py")
        self.assertIn("Docs impact", c.next_for(p, c.load_briefs(p))[2])  # the documenting stage comes first
        self.fm("task", "set", "T-0001", "--section", "Docs impact", "--text", "none: test")
        text = c.next_for(p, c.load_briefs(p))[2]
        for owed in ("audit missing: intent", "audit missing: adversary"):  # the M groups, still asked
            self.assertIn(owed, text)

    def test_the_stage_of_a_docs_only_m_skips_documenting(self):
        p = self.ready(touch=DOCS)
        b = c.find_brief(p, "T-0001")
        self.assertEqual(c.stage(b, "standard", None, c.ptier_hint(p, b)), "auditing")
        self.assertEqual(c.stage(b), "documenting")  # unmeasured: the claimed M, as before

    def test_the_hint_is_the_unmeasured_tier_with_no_touched_files(self):
        p = self.ready(touch=None)
        self.assertEqual(c.ptier_hint(p, c.find_brief(p, "T-0001")), "normal")

    def test_the_hint_never_drops_below_a_stamp(self):
        p = self.ready(touch=DOCS)
        b = c.find_brief(p, "T-0001")
        b.meta["ptier"] = "risky"
        self.assertEqual(c.ptier_hint(p, b), "risky")

    def test_a_skill_edit_is_not_a_docs_only_hint(self):
        p = self.ready(touch="plugin/skills/next/SKILL.md")
        self.assertEqual(c.ptier_hint(p, c.find_brief(p, "T-0001")), "normal")

    def test_audit_progress_counts_the_given_tier(self):
        self.assertEqual(c.audit_progress(brief("M"), None, "trivial")["required"], 1)
        self.assertEqual(c.audit_progress(brief("M"), None, "normal")["required"], 2)


class StampOnlyOnARise(TierOn, AcceptCase):
    def ptier_events(self):
        return c.ledger_tail(self.p, 500, kinds=("ptier",))

    def test_accept_logs_no_ptier_event_when_the_stamp_does_not_rise(self):
        tid = self.lane(1, "guide.md", when=OLD, tier="M")
        b = self.brief(tid)
        b.meta["ptier"] = "risky"
        c.save_brief(self.p, b)
        args = types.SimpleNamespace(audit=[[lens, "x", "y"] for lens in c.DEFAULT_LENSES], json=False, no_check=False)
        with contextlib.redirect_stdout(io.StringIO()):
            train.accept(self.p, self.brief(tid), self.repo, args)
        self.assertEqual(self.ptier_events(), [])

    def test_accept_logs_the_event_when_the_stamp_rises(self):
        tid = self.lane(1, "guide.md", when=OLD, tier="M")
        self.accept(tid)
        self.assertEqual(len(self.ptier_events()), 1)


class CommandGates(TierOn, ForemanTestCase):
    def test_sampo_gates_prints_the_process_tier(self):
        self.fm("init")
        self.fm("task", "new", "Change docs", "--type", "FEATURE", "--tier", "M", "--ac", "ok :: true", "--step", "s")
        out = self.fm("gates", "T-0001").stdout
        self.assertIn("process tier", out)
        self.assertIn("normal", out)


if __name__ == "__main__":
    unittest.main()
