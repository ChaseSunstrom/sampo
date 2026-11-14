"""T-4634: Smith's per-test reader (T-4629) reads a unittest -v result a warning splits across lines. unittest prints
`name (mod.Class.name) ... ` and the verdict (`ok`, `FAIL` ...) when the test ends: whatever the test writes in between
(a ResourceWarning, a print) lands between them, so the verdict stands alone on a later line and a line-by-line reader saw
no result for that id: a held test read as gone at both runs and the keep check was red for a test that passed. The reader
now keeps the opened id and joins the next bare verdict line to it. What it cannot decide (two tests opened at once, a
verdict too far away, a verdict glued to the text) is left unread, so the id is not held (before the work) or is gone
(after it), never taken as passed. The fixture project's test writes to stderr mid-result, with the suite's stderr merged
into its output as the keep check runs it; a pytest fixture does the same where pytest is installed (it skips with a
reason otherwise)."""
import importlib.util
import os

import test_smith_keep_passing as kp
from test_code import CodeCase

from sampo import smith

HAS_PYTEST = importlib.util.find_spec("pytest") is not None
CALC = kp.CALC
WARN = ("import sys\nimport unittest\n\nimport calc\n\n\nclass T(unittest.TestCase):\n"
        "    def test_zero(self):\n        sys.stderr.write('ResourceWarning: unclosed file\\n')\n"
        "        self.assertEqual(calc.add(1, 0), 1)\n\n"  # passes (a - 0 is 1), its result split by the warning
        "    def test_mul(self):\n        self.assertEqual(calc.mul(2, 3), 6)\n\n"
        "    def test_add_two(self):\n        sys.stderr.write('a warning\\n')\n"
        "        self.assertEqual(calc.add(2, 3), 5)\n")  # red before: add subtracts, split by a warning too
VERBOSE = "python3 -m unittest discover -v"


def feed(lines):
    reader = smith._PerTest()
    for ln in lines:
        reader.feed(ln)
    return reader.seen


class Lines(CodeCase):
    """The reader on written-out lines (no project)."""

    def test_a_clean_line_is_read_as_before(self):
        self.assertEqual(feed(["test_a (m.C.test_a) ... ok", "test_b (m.C.test_b) ... FAIL"]),
                         {"test_a (m.C)": "PASSED", "test_b (m.C)": "FAILED"})

    def test_a_result_split_by_a_warning_is_joined_to_its_verdict(self):
        seen = feed(["test_a (m.C.test_a) ... /x/m.py:3: ResourceWarning: unclosed file", "  warnings.warn('x')", "ok"])
        self.assertEqual(seen, {"test_a (m.C)": "PASSED"})

    def test_a_result_whose_open_line_is_only_the_id_is_joined_too(self):
        self.assertEqual(feed(["test_a (m.C.test_a) ... ", "printed by the test", "ok"]), {"test_a (m.C)": "PASSED"})

    def test_a_split_failure_is_read_as_failed_and_a_split_skip_as_skipped(self):
        seen = feed(["test_a (m.C.test_a) ... warn", "FAIL", "test_b (m.C.test_b) ... warn", "ERROR",
                     "test_c (m.C.test_c) ... warn", "skipped 'why'", "test_d (m.C.test_d) ... warn", "expected failure",
                     "test_e (m.C.test_e) ... warn", "unexpected success"])
        self.assertEqual(seen, {"test_a (m.C)": "FAILED", "test_b (m.C)": "FAILED", "test_c (m.C)": "SKIPPED",
                                "test_d (m.C)": "PASSED", "test_e (m.C)": "FAILED"})

    def test_noise_that_is_not_a_verdict_does_not_attach(self):
        self.assertEqual(feed(["test_a (m.C.test_a) ... warn", "okay then", "all ok", "Ran 1 test in 0.1s", ""]), {})

    def test_a_verdict_with_no_open_id_is_ignored(self):
        self.assertEqual(feed(["ok", "FAIL", "test_a (m.C.test_a) ... ok", "ok"]), {"test_a (m.C)": "PASSED"})

    def test_a_verdict_belongs_to_one_id_only(self):
        self.assertEqual(feed(["test_a (m.C.test_a) ... w", "ok", "ok", "FAIL"]), {"test_a (m.C)": "PASSED"})

    def test_two_ids_opened_at_once_are_both_left_unread(self):
        """Parallel output: the verdict that follows could be either one's, so neither takes it."""
        seen = feed(["test_a (m.C.test_a) ... w", "test_b (m.C.test_b) ... w", "ok", "FAIL", "test_c (m.C.test_c) ... ok"])
        self.assertEqual(seen, {"test_c (m.C)": "PASSED"})

    def test_a_clean_line_between_an_open_id_and_its_verdict_leaves_the_open_id_unread(self):
        seen = feed(["test_a (m.C.test_a) ... w", "test_b (m.C.test_b) ... ok", "ok"])
        self.assertEqual(seen, {"test_b (m.C)": "PASSED"})

    def test_a_verdict_too_far_from_its_open_line_is_not_taken(self):
        seen = feed(["test_a (m.C.test_a) ... w"] + ["noise"] * (smith.PER_TEST_REACH + 1) + ["ok"])
        self.assertEqual(seen, {})
        near = feed(["test_a (m.C.test_a) ... w"] + ["noise"] * (smith.PER_TEST_REACH - 1) + ["ok"])
        self.assertEqual(near, {"test_a (m.C)": "PASSED"})

    def test_a_line_that_is_not_an_id_before_the_dots_does_not_open_one(self):
        self.assertEqual(feed(["loading ... done", "ok"]), {})

    def test_pytest_lines_still_read(self):
        self.assertEqual(feed(["PASSED t.py::a", "FAILED t.py::b - assert 1"]), {"t.py::a": "PASSED", "t.py::b": "FAILED"})

    def test_the_feed_returns_false_so_it_is_an_on_line_that_shows_nothing(self):
        self.assertIs(smith._PerTest().feed("test_a (m.C.test_a) ... ok"), False)


class Warned(kp.Begun):
    """reruns: Begun's setUp, on a red unittest suite whose tests write to stderr mid-result (run with -v, so the keep
    check's own command reads the lines)."""
    repo_seed = ("seed a red suite whose tests warn", (("calc.py", CALC), ("test_calc.py", WARN)))
    suite = VERBOSE

    def test_the_fixture_output_really_splits_the_result_line_by_line(self):
        """The premise: read one line at a time, as T-4629 did, the split tests have no result at all."""
        code, out = smith.run_bounded(VERBOSE, self.repo, 60, dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        line_by_line = {}
        for ln in out.splitlines():
            line_by_line.update(smith._per_test(ln))
        passed = [t for t, s in line_by_line.items() if s == "PASSED"]
        self.assertEqual(len(passed), 1, (line_by_line, out))  # test_mul alone
        joined = feed(out.splitlines())
        self.assertEqual(sorted(t for t, s in joined.items() if s == "PASSED"),
                         ["test_mul (test_calc.T)", "test_zero (test_calc.T)"], out)
        self.assertEqual(joined["test_add_two (test_calc.T)"], "FAILED", out)  # the split failure is read as failed

    def test_a_passing_test_split_by_a_warning_is_held(self):
        """Without the joined reader the before-run read test_mul only: the two split tests were gone."""
        held = sorted(self.run_.keep_held or ())
        self.assertEqual(len(held), 2, held)
        self.assertTrue(any("test_zero" in t for t in held), held)
        self.assertTrue(any("test_mul" in t for t in held), held)

    def test_the_failing_split_test_is_not_held(self):
        self.assertFalse(any("test_add_two" in t for t in self.run_.keep_held or ()), self.run_.keep_held)

    def test_the_held_tests_still_passing_is_green_not_gone(self):
        self.edit(CALC + "\n\ndef sub(a, b):\n    return a - b\n")
        state, out = self.run_.keep_run("step")
        self.assertEqual(state, "green", out)

    def test_breaking_the_split_held_test_is_red_and_names_it(self):
        self.edit(CALC.replace("return a - b", "return 7"))
        state, out = self.run_.keep_run("step")
        self.assertEqual(state, "red")
        self.assertIn("test_zero", out.splitlines()[0])

    def test_a_split_held_test_that_is_gone_is_red(self):
        self.edit(WARN.replace("def test_zero", "def test_nought"), "test_calc.py")
        state, out = self.run_.keep_run("step")
        self.assertEqual(state, "red")
        self.assertIn("test_zero", out)


class WarnedQuiet(kp.Begun):
    """reruns: Begun's setUp, on the same project with the quiet command: the held ids come from the one more run in the
    verbose form (T-4629's keep_hold), which reads the split lines the same way."""
    repo_seed = Warned.repo_seed
    suite = kp.UNITTEST

    def test_the_verbose_rerun_holds_the_split_passing_test(self):
        held = sorted(self.run_.keep_held or ())
        self.assertEqual(len(held), 2, (held, self.run_.keep_why))
        self.assertTrue(any("test_zero" in t for t in held), held)


PYTEST_WARN = ("import sys\n\nimport calc\n\n\ndef test_zero():\n    sys.stderr.write('a warning\\n')\n"
               "    assert calc.add(1, 0) == 1\n\n\n"
               "def test_mul():\n    assert calc.mul(2, 3) == 6\n\n\n"
               "def test_add_two():\n    assert calc.add(2, 3) == 5\n")  # red before: add subtracts


class PytestWarned(kp.Begun):
    """reruns: Begun's setUp, on a small pytest project red before the work, a test of which writes to stderr with the
    output not captured (-s). Skips, with its reason, where pytest is not installed."""
    repo_seed = ("seed a red pytest suite whose test warns", (("calc.py", CALC), ("test_calc.py", PYTEST_WARN)))
    suite = "python3 -m pytest -q -s -p no:cacheprovider"

    def setUp(self):
        if not HAS_PYTEST:
            self.skipTest("pytest is not installed here: the pytest fixture project cannot run")
        super().setUp()

    def test_the_passing_ids_are_held_with_the_warning_written(self):
        self.assertEqual(self.run_.keep_held, {"test_calc.py::test_zero", "test_calc.py::test_mul"}, self.run_.keep_why)

    def test_breaking_a_held_pytest_test_is_red_and_fixing_the_red_one_is_green(self):
        self.edit(kp.MUL_BAD)
        state, out = self.run_.keep_run("step")
        self.assertEqual(state, "red")
        self.assertIn("test_mul", out)
        self.edit(kp.ADD_OK)
        self.assertEqual(self.run_.keep_run("step")[0], "green")
