"""The process tier (T-5713): a model-free risk score for each change. process_tier maps a claimed tier, the type,
the files, the diff and the tests linked to each code file to trivial, small, normal or risky; ptier_of reads a brief.
Escalate-only: a floor never lowers, a stored ptier is only ever raised, and FOREMAN_PROCESS_TIER=off gives today's
S, M and L."""
import os
import unittest
from unittest import mock

from helpers import ForemanTestCase  # noqa: F401  (puts plugin/lib on the import path)
from sampo import core as c


def patch(path, *lines):
    """A one-hunk unified diff of path; lines are the body, each already prefixed with + or -."""
    return (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1,1 +1,1 @@\n"
            + "".join(line + "\n" for line in lines))


def code(path, n=3):
    """A diff that adds n plain code lines to path."""
    return patch(path, *[f"+value_{i} = {i}" for i in range(n)])


DOCS = "docs/guide.md"
A, B, C, D = "plugin/lib/sampo/a.py", "plugin/lib/sampo/b.py", "plugin/lib/sampo/c.py", "plugin/lib/sampo/d.py"
TA, TB, TC, TD = "plugin/tests/test_a.py", "plugin/tests/test_b.py", "plugin/tests/test_c.py", "plugin/tests/test_d.py"

# (name, claimed tier, type, files, diff, tests_linked, expected ptier)
TRIVIAL = [
    ("docs only, M", "M", "FEATURE", [DOCS], patch(DOCS, "+A new sentence."), {}, "trivial"),
    ("comments only, M", "M", "FIX", [A], patch(A, "-# old note", "+# a clearer note"), {}, "trivial"),
    ("comments only, S", "S", "FIX", [A], patch(A, "+# why it is so"), {}, "trivial"),
    ("comments only, 59 changed lines", "M", "FIX", [A], patch(A, *["+# note"] * 59), {}, "trivial"),
    ("tests only, M", "M", "FIX", [TA], patch(TA, "+self.assertEqual(1, 1)"), {}, "trivial"),
]

SMALL = [
    ("S code change, no test", "S", "FIX", [A], code(A), {}, "small"),
    ("M, one file, linked test", "M", "FIX", [A], code(A), {A: [TA]}, "small"),
    ("M, three files, 80 lines, each linked", "M", "FIX", [A, B, C],
     code(A, 30) + code(B, 30) + code(C, 20), {A: [TA], B: [TB], C: [TC]}, "small"),
    ("M, 60 changed lines is not a comment-only trivial", "M", "FIX", [A],
     patch(A, *["+# note"] * 60), {A: [TA]}, "small"),
    ("M, test removal over 20 lines is not trivial", "M", "FIX", [TA],
     patch(TA, *[f"-self.assertEqual({i}, {i})" for i in range(25)]), {}, "small"),
]

NORMAL = [
    ("M, four files", "M", "FIX", [A, B, C, D], code(A) + code(B) + code(C) + code(D),
     {A: [TA], B: [TB], C: [TC], D: [TD]}, "normal"),
    ("M, 81 lines", "M", "FIX", [A], code(A, 81), {A: [TA]}, "normal"),
    ("M, code with no linked test", "M", "FIX", [A], code(A), {}, "normal"),
    ("M, one code file of two has no test", "M", "FIX", [A, B], code(A) + code(B), {A: [TA]}, "normal"),
    ("M, no diff to measure", "M", "FIX", [A], "", {A: [TA]}, "normal"),
    ("M, no files", "M", "FIX", [], code(A), {A: [TA]}, "normal"),
    ("M, comments only but no files: the paths are unknown, so no trivial", "M", "FIX", [],
     patch("plugin/hooks/pre.py", "+# a note"), {}, "normal"),
    ("M, a production file named test_*: code, not a test", "M", "FIX", ["plugin/lib/sampo/test_runner.py"],
     code("plugin/lib/sampo/test_runner.py"), {}, "normal"),
]

RISKY = [
    ("claimed L, docs only", "L", "FEATURE", [DOCS], patch(DOCS, "+A line."), {}, "risky"),
    ("claimed XL", "XL", "FIX", [A], code(A), {A: [TA]}, "risky"),
    ("claimed GIANT", "GIANT", "FIX", [A], code(A), {A: [TA]}, "risky"),
    ("an unknown tier name reads as L", "BOGUS", "FIX", [A], code(A), {A: [TA]}, "risky"),
    ("SECURITY type on a docs change", "M", "SECURITY", [DOCS], patch(DOCS, "+A line."), {}, "risky"),
    ("sensitive code: pickle.loads added, S", "S", "FIX", [A],
     patch(A, "+value = pickle.loads(blob)"), {}, "risky"),
    ("sensitive manifest, a .txt name, docs-only diff", "M", "FIX", ["requirements.txt"],
     patch("requirements.txt", "+requests==2.0"), {}, "risky"),
    ("guard path, comments only", "M", "FIX", ["plugin/lib/sampo/fmguard.py"],
     patch("plugin/lib/sampo/fmguard.py", "+# a note"), {}, "risky"),
    ("a guard-named test is sensitive (c.sensitive reads the name), tests only", "M", "FIX",
     ["plugin/tests/test_guard.py"], patch("plugin/tests/test_guard.py", "+self.assertTrue(x)"), {}, "risky"),
    ("hook path, S", "S", "FIX", ["plugin/hooks/pre.py"], code("plugin/hooks/pre.py"), {}, "risky"),
    ("schema path, a comment", "M", "FIX", ["db/schema.sql"], patch("db/schema.sql", "+-- a note"), {}, "risky"),
    ("migration path, S", "S", "FIX", ["db/migrations/0002_add.py"],
     code("db/migrations/0002_add.py"), {}, "risky"),
    ("a hook named test_*, comments only: the hook path still floors it", "M", "FIX", ["plugin/hooks/test_pre.py"],
     patch("plugin/hooks/test_pre.py", "+# a note"), {}, "risky"),
]


class ProcessTierTable(unittest.TestCase):
    def test_every_named_case_reaches_its_tier(self):
        for table in (TRIVIAL, SMALL, NORMAL, RISKY):
            for name, tier, type_, files, diff, linked, want in table:
                with self.subTest(name):
                    ptier, why = c.process_tier(tier, type_, files, diff, linked)
                    self.assertEqual(ptier, want, f"{name}: {why}")
                    self.assertTrue(why, "every tier names its reason")

    def test_every_tier_is_reached(self):
        seen = {case[-1] for table in (TRIVIAL, SMALL, NORMAL, RISKY) for case in table}
        self.assertEqual(seen, set(c.PTIERS))

    def test_no_tier_reads_as_small(self):
        ptier, _ = c.process_tier("", "FIX", [A], code(A), {A: [TA]})
        self.assertEqual(ptier, "small")

    def test_a_floor_wins_over_a_trivial_shape(self):
        ptier, why = c.process_tier("L", "FIX", [DOCS], patch(DOCS, "+A line."), {})
        self.assertEqual(ptier, "risky")
        self.assertIn("L", " ".join(why))

    def test_the_result_is_pure(self):
        files, linked = [A], {A: [TA]}
        first = c.process_tier("M", "FIX", files, code(A), linked)
        self.assertEqual(first, c.process_tier("M", "FIX", files, code(A), linked))
        self.assertEqual(files, [A])
        self.assertEqual(linked, {A: [TA]})


class BriefScore(unittest.TestCase):
    def brief(self, tier, type_="FIX", scope=(A,), ptier=None):
        b = c.Brief.new("T-0001", "Fix login", type_, tier, now="2026-01-01T00:00:00Z", scope=list(scope))
        if ptier is not None:
            b.meta["ptier"] = ptier
        return b

    def test_ptier_of_reads_the_brief_and_its_linked_tests(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("FOREMAN_PROCESS_TIER", None)
            b = self.brief("M", scope=[A])
            self.assertEqual(c.ptier_of(b, [A], code(A), {A: [TA]})[0], "small")
            self.assertEqual(c.ptier_of(b, [A], code(A))[0], "normal")

    def test_ptier_of_reads_the_type(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("FOREMAN_PROCESS_TIER", None)
            b = self.brief("M", type_="SECURITY", scope=[DOCS])
            self.assertEqual(c.ptier_of(b, [DOCS], patch(DOCS, "+A line."))[0], "risky")

    def test_scope_globs_read_as_unknown_code_and_stay_at_the_claimed_level(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("FOREMAN_PROCESS_TIER", None)
            self.assertEqual(c.ptier_of(self.brief("M", scope=["docs/**"]))[0], "normal")
            self.assertEqual(c.ptier_of(self.brief("S", scope=["docs/**"]))[0], "small")

    def test_a_stored_ptier_is_only_ever_raised(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("FOREMAN_PROCESS_TIER", None)
            docs = patch(DOCS, "+A line.")
            # a computed trivial, stored small: raised to small
            self.assertEqual(c.ptier_of(self.brief("S", scope=[DOCS], ptier="small"), [DOCS], docs)[0], "small")
            # a computed trivial, stored risky: raised to risky
            self.assertEqual(c.ptier_of(self.brief("S", scope=[DOCS], ptier="risky"), [DOCS], docs)[0], "risky")
            # a computed risky (claimed L), stored trivial: never lowered
            self.assertEqual(c.ptier_of(self.brief("L", scope=[DOCS], ptier="trivial"), [DOCS], docs)[0], "risky")
            # a stored value that is not a tier name is ignored
            self.assertEqual(c.ptier_of(self.brief("S", scope=[DOCS], ptier="bogus"), [DOCS], docs)[0], "trivial")

    def test_off_gives_s_small_m_normal_and_l_risky(self):
        with mock.patch.dict(os.environ, {"FOREMAN_PROCESS_TIER": "off"}):
            docs = patch(DOCS, "+A line.")
            for tier, want in (("S", "small"), ("M", "normal"), ("L", "risky"), ("XL", "risky")):
                with self.subTest(tier):
                    b = self.brief(tier, scope=[DOCS])
                    self.assertEqual(c.ptier_of(b, [DOCS], docs)[0], want)

    def test_off_still_keeps_a_stored_ptier_as_a_floor(self):
        with mock.patch.dict(os.environ, {"FOREMAN_PROCESS_TIER": "off"}):
            b = self.brief("S", scope=[DOCS], ptier="risky")
            self.assertEqual(c.ptier_of(b, [DOCS], patch(DOCS, "+A line."))[0], "risky")
