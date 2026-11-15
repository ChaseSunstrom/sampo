"""T-4712: two misreads of a plan by the planning gate, both by surface marks. (a) An Approach whose option 1 held an inner
numbered list '(1) … (2) …' was refused as 'option 1 has no likely outcome or failure mode': the inner (2) was taken
for the start of option 2 (T-4692). Now an inner mark of another shape, or a mid-sentence one when a real sentence-start
mark of the same number follows, opens no option; and a refusal about options quotes each option's opening words, so
the author sees the split. (b) A test-only S fix was held for a cold rival read as 'a one-way step (release)' because a
step said 'release held pushes' (T-4719). The verb is a pre-filter; the plan's structure (scope, steps' paths, verify
commands) decides, and a plan that runs a publishing command or has an outside-delete step is still held."""
import os
import unittest
from unittest import mock

import helpers  # noqa: F401 (puts plugin/lib on the import path before sampo)
from sampo import core as c

AP = "Approach (options → choice → why)"
# T-4692's option 1, as first written in spirit: a list of its own inside the option's text
INNER = ("1) use what exists: the train's lock and the drive's next-action code. Failing tests first: (1) a train lands "
         "beside a focused task; (2) the drive offers a lane, never a focus. Likely outcome: under 120 lines. fails if: "
         "the hold protects uncommitted work. "
         "2) only the drive half. Likely outcome: the hook stops offering a focus. fails if: a hand focus still loses lanes. "
         "3) do less: a line in the rules. fails if: memory lines are what the user asked to stop relying on. "
         "Choice 1 (80%): both halves.")
SAME_SHAPE = INNER.replace("1) use what exists", "(1) use what exists", 1).replace(" 2) only", " (2) only").replace(" 3) do", " (3) do")


def brief(tier="M", ap=INNER, steps=("red test",), scope=None, acs=(("it works", "pytest -k x"),), raw="x", title="T"):
    b = c.Brief.new("T-0001", title, "FIX", tier, now="2026-01-01T00:00:00Z")
    b.set_section("Raw request", "> " + raw)
    b.set_section("Interpretation", "Reading 1: the gate counts. Reading 2: the gate reads the text.")
    b.set_section(AP, ap)
    for text, cmd in acs:
        b.add_ac(text, verify=cmd)
    for s in steps:
        b.add_step(s)
    if scope is not None:
        b.meta["scope"] = scope
    return b


class OptionSplit(unittest.TestCase):
    def test_an_inner_numbered_list_is_part_of_its_option(self):
        parts = c._split_options(INNER)
        self.assertEqual(len(parts), 3, parts)
        self.assertIn("(2) the drive offers a lane", parts[0])
        self.assertIn("fails if: the hold protects", parts[0])
        self.assertEqual(c.plan_gaps(brief(), "full"), [])

    def test_an_inner_list_of_the_options_own_shape_after_a_colon_is_still_one_option(self):
        parts = c._split_options(SAME_SHAPE)
        self.assertEqual(len(parts), 3, parts)
        self.assertEqual(c.plan_gaps(brief(ap=SAME_SHAPE), "full"), [])

    def test_inline_options_without_full_stops_still_split(self):  # the shapes people already write
        e, l, o = "reuse existing code; fails if: p", "do less, skip it; fails if: q", "a queue; breaks r"
        for text in (f"Options: (1) {e} (2) {l} (3) {o} Choice 2 (80%): two", f"1) {e} 2) {l} 3) {o} Choice 1 (80%)"):
            self.assertEqual(len(c._split_options(text)), 3, text)

    def test_a_refusal_about_an_option_quotes_each_options_opening_words(self):
        ap = ("1) reuse existing code with the longer opening words here. fails if: p 2) do less and skip it, "
              "likely outcome: q 3) a queue with a worker pool. Choice 1 (80%): why")
        g = " | ".join(c.plan_gaps(brief(ap=ap), "full"))
        self.assertIn("option 3", g)
        self.assertIn("has no likely outcome or failure mode", g)
        self.assertIn("read as: 1 'reuse existing code with the longer", g)
        self.assertIn("2 'do less and skip it", g)
        self.assertIn("3 'a queue with a worker pool", g)

    def test_a_real_marker_after_a_semicolon_is_kept_when_the_run_goes_on_without_the_later_one(self):
        # review of T-4712: options 2 and 3 followed ';' and a later 'Steps: 1) … 2) …' held a sentence-start '2)'
        text = "1) a, fails if: x; 2) b; fails if: y; 3) c. Steps: 1) red test. 2) code."
        parts = c._split_options(text)
        self.assertEqual(len(parts), 3, parts)
        self.assertTrue(parts[1].strip().startswith("b"), parts)
        self.assertTrue(parts[2].strip().startswith("c"), parts)
        # the inner list that a real option 2 follows is still one option (the later '2)' has a '3)' after it)
        inner = "1) use it: 1) a; 2) b; 3) c. fails if: x. 2) only half. fails if: y. 3) do less. fails if: z."
        self.assertEqual(len(c._split_options(inner)), 3, c._split_options(inner))

    def test_the_count_refusal_quotes_the_split_too(self):
        g = " | ".join(c.plan_gaps(brief(ap="1) a first thing 2) a second thing Choice 1 (80%): why"), "full"))
        self.assertIn("names 2 options", g)
        self.assertIn("1 'a first thing'", g)


class OneWayByStructure(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {"FOREMAN_NO_RIVAL": "0"})
        env.start()
        self.addCleanup(env.stop)

    TESTS = (("the hook ends by itself", "python3 plugin/tests/run.py test_land_ship"),)

    def rival(self, **kw):
        return c.rival_gap(brief("S", ap="", **kw))

    FILE = "plugin/tests/test_land_ship.py"
    HOOK = f"failing test in {FILE}, bound the hook's loop, release held pushes in tearDown"  # T-4719's step, naming its test file

    def test_a_test_only_fix_is_not_one_way_for_a_word_in_a_step(self):  # T-4719
        step = self.HOOK
        self.assertEqual(c._one_way(brief("S", ap="", steps=(step,), acs=self.TESTS)), "")
        self.assertEqual(self.rival(steps=(step,), acs=self.TESTS), [])
        self.assertEqual(self.rival(steps=(step,), acs=self.TESTS, scope=[self.FILE]), [])

    def test_a_one_way_word_is_let_through_only_on_positive_evidence_of_tests(self):
        # no test path in the step and no stated scope: nothing says what the step touches, so the word holds the plan
        for step in ("failing test, bound the hook's loop, release held pushes in tearDown",
                     "add the test, then publish the package to the registry", "add the test, then deploy to prod"):
            self.assertTrue(c._one_way(brief("S", ap="", steps=(step,), acs=self.TESTS)), step)
            self.assertTrue(self.rival(steps=(step,), acs=self.TESTS), step)
            self.assertEqual(c.tests_only_waiver(brief("S", ap="", steps=(step,), acs=self.TESTS)), "", step)
        # a stated scope of tests only is the other evidence (the plan said where it works)
        step = "add the test, then release held pushes in tearDown"
        self.assertEqual(c._one_way(brief("S", ap="", steps=(step,), acs=self.TESTS, scope=["plugin/tests/*.py"])), "")
        # a stated scope outside the tests, or another path in the step, still holds it
        self.assertEqual(c._one_way(brief("S", ap="", steps=(step,), acs=self.TESTS, scope=["plugin/lib/x.py"])), "release")
        self.assertEqual(c._one_way(brief("S", ap="", steps=(f"test in {self.FILE}, release plugin/lib/x.py",), acs=self.TESTS)), "release")

    def test_the_one_way_pattern_is_a_floor_that_only_keeps_a_hold(self):
        for step in (f"test in {self.FILE}, then run `make deploy` to release it", f"test in {self.FILE}, `sampo ship` to release it",
                     f"test in {self.FILE}, then git -C x push origin main to release it",
                     f"test in {self.FILE}, then `kubectl apply -f x` to deploy it"):
            self.assertTrue(c._one_way(brief("S", ap="", steps=(step,), acs=self.TESTS)), step)

    def test_the_same_words_with_a_non_test_verify_or_scope_are_still_one_way(self):
        step = f"failing test in {self.FILE}, release held pushes in tearDown"
        self.assertEqual(c._one_way(brief("S", ap="", steps=(step,), acs=(("it works", "true"),))), "release")
        self.assertEqual(c._one_way(brief("S", ap="", steps=(step,), acs=self.TESTS, scope=["plugin/lib/x.py"])), "release")
        self.assertEqual(c._one_way(brief("S", ap="", steps=("release plugin/lib/x.py to users",), acs=self.TESTS)), "release")
        for step in ("delete old.csv", "test it, delete users.db", "delete data/old.csv"):  # a data file is not a test
            self.assertTrue(c._one_way(brief("S", ap="", steps=(step,), acs=self.TESTS)), step)

    def test_an_M_plan_is_tests_only_only_when_its_scope_says_so(self):  # T-4543 (M, no scope, a step naming docs/map)
        step = "failing tests in plugin/tests/test_disk_pressure.py; the tidy item merged into the trunk; map entry in docs/map"
        self.assertEqual(c._one_way(brief("M", ap="", steps=(step,), acs=self.TESTS)), "merged")
        step = "failing test, then the tidy item merged into the trunk"
        self.assertEqual(c._one_way(brief("M", ap="", steps=(step,), acs=self.TESTS)), "merged")
        self.assertEqual(c._one_way(brief("M", ap="", steps=(step,), acs=self.TESTS, scope=["plugin/tests/*.py"])), "")
        self.assertEqual(c._one_way(brief("S", ap="", steps=("a step touching docs/map, release it",), acs=self.TESTS)), "release")

    def test_a_test_only_plan_with_a_publishing_step_is_still_held(self):
        for step in ("add the test, then run `sampo ship` to publish", "test it, then release it with gh release create v1",
                     "test it, then migrate the data with `python3 manage.py migrate`",
                     "test it, then delete the folder with rm -rf ~/old-data", "test it, then publish with npm publish"):
            self.assertTrue(c._one_way(brief("S", ap="", steps=(step,), acs=self.TESTS)), step)
            self.assertTrue(self.rival(steps=(step,), acs=self.TESTS), step)

    def test_a_publish_verify_command_is_not_a_test_run(self):
        acs = (("it ships", "sampo ship --dry-run"),)
        self.assertEqual(c._one_way(brief("S", ap="", steps=("publish the notes",), acs=acs)), "publish")

    def test_a_verify_command_counts_as_a_test_run_by_its_command_words_in_every_part(self):
        step = self.HOOK
        for cmd in ("sampo ship --tag pytest", "git push origin HEAD:pytest", "pytest -k x | sampo ship",
                    "pytest -k x & sampo ship", "pytest -k x\nsampo ship", "pytest $(sampo ship)", "pytest `sampo ship`",
                    "pytest -k x && make deploy", "pytest -k x; kubectl apply -f x", "echo pytest", "cat run_tests.sh"):
            acs = (("it works", cmd),)
            self.assertEqual(c._one_way(brief("S", ap="", steps=(step,), acs=acs)), "release", cmd)
            self.assertEqual(c.tests_only_waiver(brief("S", ap="", steps=(step,), acs=acs)), "", cmd)
        for cmd in ("pytest -k x 2>&1", "FOREMAN_NO_TEST_CACHE=1 nice python3 plugin/tests/run.py test_a -k b",
                    "python3 -m pytest tests/x.py && python3 plugin/tests/run.py a", "python3 -m unittest -v a.b",
                    "./plugin/tests/run.py test_a; pytest -q", "(pytest -k x)", "time pytest"):
            acs = (("it works", cmd),)
            self.assertEqual(c._one_way(brief("S", ap="", steps=(step,), acs=acs)), "", cmd)

    def test_a_delete_outside_the_project_is_still_marked_by_its_path(self):  # the doors mark it at focus; the gate reads it
        b = brief("S", ap="", steps=("tidy up",), acs=self.TESTS)
        b.meta["doors"] = [1]
        self.assertTrue(c.rival_gap(b))

    def test_the_verb_a_test_only_plan_was_let_through_with_is_named_for_the_close(self):  # T-4712: recorded at focus
        step = self.HOOK
        self.assertEqual(c.tests_only_waiver(brief("S", ap="", steps=(step,), acs=self.TESTS)), "release")
        self.assertEqual(c.tests_only_waiver(brief("S", ap="", steps=("failing test, fix the loop",), acs=self.TESTS)), "")
        self.assertEqual(c.tests_only_waiver(brief("S", ap="", steps=(step,), acs=(("it works", "true"),))), "")  # held, not let through
        self.assertEqual(c.tests_only_waiver(brief("S", ap="", steps=(step, "then run `sampo ship`"), acs=self.TESTS)), "")
        self.assertEqual(c.tests_only_waiver(brief("S", ap="", steps=("publish the notes",), raw="publish the notes")), "")

    def test_the_raw_request_naming_the_verb_still_waives_it(self):
        self.assertEqual(c._one_way(brief("S", ap="", steps=("publish the notes",), raw="publish the notes")), "")


if __name__ == "__main__":
    unittest.main()
