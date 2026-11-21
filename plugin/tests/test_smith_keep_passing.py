"""T-4629: a project whose own tests are red before the work still gets a keep check: every test that passed before the
work still passes. The before-run's per-test results (unittest -v, pytest -rA, read by bench_swe.parse_log) name the
tests that passed; a later run is green when all of them pass, whatever the tests that were red before do, and red when
one of them fails or is gone; the step check, the stop's restore (T-4614) and the notes work on that state unchanged. A
runner whose output has no per-test lines leaves no keep check, with the reason in the stats. A suite green before the
work is checked as it was. On exam run all14-04f8, 2 of 4 runs that broke kept checks had 'keep check (before): red'.
The unit cases drive a Run's own methods on a real git tree; the end-to-end one scripts the model with test_code's fake."""
import json
import os
import types

from test_code import PLAN, CodeCase, call, plan

from sampo import smith

CALC = "def add(a, b):\n    return a - b\n\n\ndef mul(a, b):\n    return a * b\n\n\ndef neg(a):\n    return -a\n"
TESTS = ("import unittest\nimport calc\n\n\nclass T(unittest.TestCase):\n"
         "    def test_zero(self):\n        self.assertEqual(calc.add(1, 0), 1)\n\n"
         "    def test_mul(self):\n        self.assertEqual(calc.mul(2, 3), 6)\n\n"
         "    def test_add_two(self):\n        self.assertEqual(calc.add(2, 3), 5)\n")  # red before: add subtracts
UNITTEST = "python3 -m unittest discover -q"
ADD_OK = CALC.replace("a - b", "a + b")  # fixes the red test, keeps the two held
MUL_BAD = CALC.replace("a * b", "a + b")  # breaks a held test


def events(r):
    return [json.loads(ln) for ln in r.stdout.splitlines() if ln.startswith("{")]


class Begun(CodeCase):
    """A Run on a project, begun through its own _keep_begin (no test of its own: the classes below hold them)."""
    repo_seed = ("seed a red suite", (("calc.py", CALC), ("test_calc.py", TESTS)))
    suite = UNITTEST

    def setUp(self):
        super().setUp()
        self.run_ = run = smith.Run(types.SimpleNamespace(), self.repo, os.path.join(self.tmp, "journal"))
        os.makedirs(run.jdir)
        run.tid, run.survey_tests = "T-0001", self.suite
        smith._keep_begin(run, None)

    def put(self, name, text):
        with open(os.path.join(self.repo, name), "w") as f:
            f.write(text)

    def edit(self, text, name="calc.py"):
        self.run_.touch(name)
        self.put(name, text)
        self.run_.edited = True


class RedBefore(Begun):
    """Two passing tests and one failing one."""

    def test_a_red_suite_before_the_work_gets_a_keep_check_over_the_two_tests_that_passed(self):
        run = self.run_
        self.assertEqual((run.keep_cmd, run.keep_state), (UNITTEST, "green"), run.keep_why)
        self.assertEqual(len(run.keep_held), 2)
        self.assertTrue(all("test_add_two" not in t for t in run.keep_held), run.keep_held)
        self.assertIn("red before the work", run.keep_why)
        self.assertIn("2 tests", run.keep_why)

    def test_breaking_a_held_test_is_red_and_names_it(self):
        self.edit(MUL_BAD)
        state, out = self.run_.keep_run("step")
        self.assertEqual(state, "red")
        self.assertIn("test_mul", out)
        self.assertEqual(self.run_.keep_state, "red")

    def test_fixing_the_test_that_was_red_is_green(self):
        self.edit(ADD_OK)
        self.assertEqual(self.run_.keep_run("step")[0], "green")

    def test_leaving_the_failing_test_failing_is_green(self):
        self.edit(CALC + "\n\ndef sub(a, b):\n    return a - b\n")
        self.assertEqual(self.run_.keep_run("step")[0], "green")

    def test_a_held_test_that_is_gone_is_red(self):
        self.edit(TESTS.replace("def test_mul", "def test_times"), "test_calc.py")
        state, out = self.run_.keep_run("step")
        self.assertEqual(state, "red")
        self.assertIn("test_mul", out)

    def test_a_new_failing_test_of_the_runs_own_is_not_held_against_it(self):
        self.edit("import unittest\n\n\nclass N(unittest.TestCase):\n    def test_new(self):\n        self.fail('later')\n",
                  "test_new.py")
        self.assertEqual(self.run_.keep_run("step")[0], "green")

    def test_a_stop_with_a_held_test_broken_puts_the_start_back(self):
        self.edit(MUL_BAD)
        lost = self.run_.unwind()
        self.assertEqual(lost["restored"], ["calc.py"])
        self.assertEqual(open(os.path.join(self.repo, "calc.py")).read(), CALC)
        self.assertIn("keep check", lost["text"])

    def test_a_stop_with_only_the_red_test_still_red_keeps_the_work(self):
        self.edit(ADD_OK.replace("def neg", "def neg2"))
        self.assertEqual(self.run_.unwind(), {})

    def test_the_held_tests_come_back_on_a_resume_from_the_journal(self):
        held = self.run_.keep_held
        self.assertEqual(smith._unpack_held(smith._pack_held(held)), held)
        self.assertEqual(smith._unpack_held("not a pack"), None)


class Unreadable(Begun):
    """A runner whose output has no per-test lines, in a project with no other suite to find."""
    repo_seed = ("seed no tests", (("calc.py", CALC),))
    suite = "python3 -c 'import sys; print(\"3 checks, 1 failed\"); sys.exit(1)'"

    def test_a_red_suite_whose_runner_gives_no_per_test_results_leaves_no_keep_check_and_says_why(self):
        run = self.run_
        self.assertEqual((run.keep_cmd, run.keep_state), (None, "none"))
        self.assertIn("red before the work", run.keep_why)
        self.assertIn("per-test", run.keep_why)
        self.assertIsNone(run.keep_held)


class NoPassing(Begun):
    """Every test red before the work."""
    repo_seed = ("seed all red", (("calc.py", CALC), ("test_calc.py", "import unittest\n\n\nclass T(unittest.TestCase):\n"
                                                                      "    def test_a(self):\n        self.fail('x')\n")))

    def test_every_test_red_leaves_no_keep_check_and_says_why(self):
        self.assertEqual((self.run_.keep_cmd, self.run_.keep_state), (None, "none"))
        self.assertIn("no test passed", self.run_.keep_why)


class Green(Begun):
    """A green suite: no held set, the check is the exit code as before."""
    repo_seed = ("seed green", (("calc.py", ADD_OK), ("test_calc.py", TESTS)))

    def test_a_green_suite_is_checked_by_its_exit_code_with_nothing_held(self):
        self.assertEqual((self.run_.keep_cmd, self.run_.keep_state), (UNITTEST, "green"))
        self.assertIsNone(self.run_.keep_held)
        self.edit(MUL_BAD)
        self.assertEqual(self.run_.keep_run("step")[0], "red")


class Verbose(CodeCase):
    """The command the per-test results are read from."""

    def test_unittest_loses_its_quiet_flag_and_gains_a_verbose_one(self):
        self.assertEqual(smith._verbose("python3 -m unittest discover -s tests -q"),
                         "python3 -m unittest discover -s tests -v")
        self.assertEqual(smith._verbose("python3 -m unittest test_calc 2>&1"), "python3 -m unittest test_calc -v 2>&1")

    def test_pytest_gains_the_summary_of_every_test(self):
        self.assertEqual(smith._verbose("python3 -m pytest -q"), "python3 -m pytest -q -rA")
        self.assertEqual(smith._verbose("pytest tests && python3 -m unittest -q"),
                         "pytest tests -rA && python3 -m unittest -v")

    def test_a_runner_it_does_not_know_and_a_shell_it_cannot_read_stay_as_they_are(self):
        for cmd in ("npm test", "go test ./...", "python3 plugin/tests/run.py", "pytest | tee out", "pytest; true"):
            self.assertIsNone(smith._verbose(cmd), cmd)


class EndToEnd(CodeCase):
    """The run as a person starts it: out of time with a held test broken puts the tree back."""
    repo_seed = ("seed a red suite", (("calc.py", CALC), ("test_calc.py", TESTS)))
    P = dict(PLAN, criteria=[{"text": "calc.add adds", "verify": "python3 -c 'import calc; assert calc.add(2, 3) == 5'"}])

    def test_out_of_time_with_a_held_test_broken_puts_the_tree_back_and_says_what_was_held(self):
        self.serve("openai", {"text": json.dumps({"wants_change": True, "speed_up": False, "why": "a fix"})},  # the judge
                   plan(self.P), call("edit", file="calc.py", old="a * b", new="a + b"),
                   *[call("run", cmd="sleep 1")] * 12)
        r = self.code("fix add", "--model", "fake", "--json", "--no-commit", "--deadline", "6", check=False)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(self.calc(), CALC, "back where the held tests passed")
        notes = [e.get("text", "") for e in events(r) if e.get("event") == "note"]
        self.assertTrue(any("keep check (before): red" in n for n in notes), notes)
        self.assertTrue(any("2 tests" in n and "passed before" in n for n in notes), notes)
        end = [e for e in events(r) if e.get("event") == "error" and e.get("restored")]
        self.assertEqual([e["restored"] for e in end], [["calc.py"]], r.stdout)


class ResumeCorrupt(Begun):
    """T-4629 review: a journal whose held-tests record is damaged or missing drops the keep check, never an empty hold."""

    def resumed(self, *extra):
        run = self.run_
        recs = [{"kind": "start", "task": "T-0001", "plan": {}, "request": "x"},
                {"kind": "keep", "cmd": run.keep_cmd, "state": run.keep_state, "why": run.keep_why,
                 "held": run.keep_held is not None}] + list(extra)
        path = os.path.join(run.jdir, "T-0001.jsonl")
        with open(path, "w") as f:
            f.write("".join(json.dumps(r) + "\n" for r in recs))
        again = smith.Run(types.SimpleNamespace(escalate=None, max_steps=None), self.repo, run.jdir)
        smith._load(again, "T-0001")
        return again

    def check_dropped(self, again):
        self.assertEqual((again.keep_cmd, again.keep_state), (None, "none"))
        self.assertIsNone(again.keep_held)
        self.assertIn("could not be read back", again.keep_why)

    def test_an_unreadable_held_record_drops_the_check(self):
        self.check_dropped(self.resumed({"kind": "keep_held", "held": "not a pack", "vcmd": None}))

    def test_a_journal_cut_between_keep_and_keep_held_drops_the_check(self):
        self.check_dropped(self.resumed())

    def test_the_hold_is_told_by_the_recorded_fact_not_the_prose(self):
        run = self.run_
        self.assertTrue(run.keep_held)
        why = "reworded: nothing like the old phrases"
        recs = [{"kind": "start", "task": "T-0001", "plan": {}, "request": "x"},
                {"kind": "keep", "cmd": run.keep_cmd, "state": run.keep_state, "why": why, "held": True}]
        with open(os.path.join(run.jdir, "T-0001.jsonl"), "w") as f:
            f.write("".join(json.dumps(r) + "\n" for r in recs))
        again = smith.Run(types.SimpleNamespace(escalate=None, max_steps=None), self.repo, run.jdir)
        smith._load(again, "T-0001")
        self.check_dropped(again)

    def test_output_text_with_the_old_phrases_does_not_drop_a_plain_green_check(self):
        run = self.run_
        recs = [{"kind": "start", "task": "T-0001", "plan": {}, "request": "x"},
                {"kind": "keep", "cmd": "true", "state": "green", "why": "the tests that passed ... still pass"}]
        with open(os.path.join(run.jdir, "T-0001.jsonl"), "w") as f:
            f.write("".join(json.dumps(r) + "\n" for r in recs))
        again = smith.Run(types.SimpleNamespace(escalate=None, max_steps=None), self.repo, run.jdir)
        smith._load(again, "T-0001")
        self.assertEqual((again.keep_cmd, again.keep_state), ("true", "green"))

    def test_a_readable_held_record_still_holds(self):
        again = self.resumed({"kind": "keep_held", "held": smith._pack_held(self.run_.keep_held), "vcmd": self.run_.keep_vcmd})
        self.assertEqual(again.keep_held, self.run_.keep_held)


class Spoof(Begun):
    """T-4629 review: test output that prints a passing line cannot make a failing held test read as passed."""

    def feed(self, text):
        r = smith._PerTest()
        for ln in text.splitlines():
            r.feed(ln)
        return r.seen

    def test_a_failing_test_that_prints_ok_is_still_failed(self):
        seen = self.feed("test_a (m.T.test_a) ... ok\nFAIL\ntest_b (m.T.test_b) ... ok\n\n"
                         "FAIL: test_a (m.T.test_a)\nERROR: test_c (m.T.test_c)\n")
        self.assertEqual(seen["test_a (m.T)"], "FAILED")
        self.assertEqual(seen["test_c (m.T)"], "FAILED")
        self.assertEqual(seen["test_b (m.T)"], "PASSED")

    def test_a_bare_ok_after_an_open_id_does_not_replace_a_failed_verdict(self):
        for tail in ("", "\nFAIL: test_a (m.T.test_a)\n"):
            seen = self.feed("test_a (m.T.test_a) ... \nFAIL\ntest_a (m.T.test_a) ... \nok\n" + tail)
            self.assertEqual(seen["test_a (m.T)"], "FAILED", tail)
            if tail:  # a bare FAIL with no id open names no test: only the summary can fail it
                seen = self.feed("FAIL\ntest_a (m.T.test_a) ... \nok\n" + tail)
                self.assertEqual(seen["test_a (m.T)"], "FAILED")

    def test_a_later_passed_line_does_not_replace_a_failed_one(self):
        seen = self.feed("test_mul (test_calc.T.test_mul) ... FAIL\ntest_mul (test_calc.T.test_mul) ... ok\n")
        self.assertEqual(seen["test_mul (test_calc.T)"], "FAILED")

    def test_pytest_summary_failed_beats_an_earlier_passed(self):
        seen = self.feed("PASSED t.py::test_a\nFAILED t.py::test_a - assert 0\nPASSED t.py::test_a\n")
        self.assertEqual(seen["t.py::test_a"], "FAILED")
