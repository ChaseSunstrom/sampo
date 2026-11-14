"""T-4630: T-4629's keep check over the tests that passed before a red suite does not count one flaky test. A held test
that fails at a later keep check is run once more (the whole check, once, only when a held id failed): the ids that
fail both times count (red, named); one that passes on the rerun is flaky, the check is green and the note names it.
A stop whose first run took over half of its bound counts that first result without a rerun (a slow suite is not run
twice past its stop). A suite green before the work is checked as it was. Also here: the held-test check on a real
pytest project (skipped, with its reason, when pytest is not installed), pytest -rA's lines read as held ids, and the
shared scripted-model fake (test_code.Fake) answering the request judge itself with a default when the script's next
reply is the run's own, so a test scripts only the run and its plan is not consumed by the judge.
The flaky test is a file in the project: it fails while flip.txt (ignored by git, so the tree is unchanged) holds a
count above 0, taking one off each run; runs.txt gets a line per run, so a test can count them."""
import importlib.util
import json
import os
import urllib.error
import urllib.request

import test_smith_keep_passing as kp
from test_code import PLAN, CodeCase, Fake, call, plan

from sampo import smith

CALC = kp.CALC
FLAKY = ("import os\nimport time\nimport unittest\n\nimport calc\n\n"
         "with open('runs.txt', 'a') as f:\n    f.write('x\\n')\n\n\n"
         "class T(unittest.TestCase):\n"
         "    def test_zero(self):\n        self.assertEqual(calc.add(1, 0), 1)\n\n"
         "    def test_mul(self):\n        self.assertEqual(calc.mul(2, 3), 6)\n\n"
         "    def test_add_two(self):\n        self.assertEqual(calc.add(2, 3), 5)\n\n"  # red before: add subtracts
         "    def test_flaky(self):\n"
         "        time.sleep(SLEEP)\n"
         "        n = 0\n"
         "        if os.path.exists('flip.txt'):\n            with open('flip.txt') as f:\n                n = int(f.read())\n"
         "        if n > 0:\n            with open('flip.txt', 'w') as f:\n                f.write(str(n - 1))\n"
         "            self.fail('flaked')\n")
SEED = (("calc.py", CALC), ("test_calc.py", FLAKY.replace("SLEEP", "0")), (".gitignore", "flip.txt\nruns.txt\n"))


class Counted(kp.Begun):
    """reruns: Begun's setUp (a Run begun on a real git tree, its keep check over the held tests), with the notes the run
    emits kept and the runs of the suite counted (no test of its own: the classes below hold them)."""

    def setUp(self):
        super().setUp()
        self.notes = []
        emit = self.run_.emit
        self.run_.emit = lambda ev, text, **kw: (self.notes.append(text), emit(ev, text, **kw))[1]

    def flip(self, n):
        self.put("flip.txt", str(n))

    def runs(self):
        with open(os.path.join(self.repo, "runs.txt")) as f:
            return len(f.read().split())

    def change(self):
        """A change to the tree, so the keep check is not skipped as already green on this tree."""
        self.edit(CALC + "\n\ndef sub(a, b):\n    return a - b\n")


class Flaky(Counted):
    """A red suite with a held test which fails as many times as flip.txt says."""
    repo_seed = ("seed a red suite with a flaky test", SEED)

    def test_the_flaky_test_is_held_with_the_two_that_passed(self):
        self.assertEqual(len(self.run_.keep_held), 3, self.run_.keep_held)
        self.assertTrue(any("test_flaky" in t for t in self.run_.keep_held), self.run_.keep_held)

    def test_a_held_test_failing_once_and_passing_on_the_rerun_is_green_and_named_flaky(self):
        self.change()
        self.flip(1)
        before = self.runs()
        state, out = self.run_.keep_run("step")
        self.assertEqual(state, "green", out)
        self.assertEqual(self.runs() - before, 2, "the check, then the one rerun")
        self.assertEqual(self.run_.keep_state, "green")
        note = next(n for n in self.notes if "keep check (step)" in n)
        self.assertIn("flaky", note)
        self.assertIn("test_flaky", note)

    def test_a_held_test_failing_twice_is_red_and_named(self):
        self.change()
        self.flip(2)
        before = self.runs()
        state, out = self.run_.keep_run("step")
        self.assertEqual(state, "red")
        self.assertEqual(self.runs() - before, 2)
        self.assertIn("test_flaky", out)
        self.assertEqual(self.run_.keep_state, "red")

    def test_a_check_with_no_held_test_failing_is_not_run_again(self):
        self.change()
        before = self.runs()
        self.assertEqual(self.run_.keep_run("step")[0], "green")
        self.assertEqual(self.runs() - before, 1)
        self.assertFalse(any("flaky" in n for n in self.notes), self.notes)

    def test_only_the_ids_that_fail_both_times_count(self):
        """One held test flakes, another is really broken: red for the broken one, the flaky one is not named as failing."""
        self.put("calc.py", CALC.replace("a * b", "a + b"))
        self.run_.touch("calc.py")
        self.run_.edited = True
        self.flip(1)
        state, out = self.run_.keep_run("step")
        self.assertEqual(state, "red")
        self.assertIn("test_mul", out)
        self.assertNotIn("test_flaky", out.splitlines()[0])
        self.assertIn("1 of the 3", out.splitlines()[0])

    def test_a_flaky_green_is_kept_for_a_stop_to_go_back_to(self):
        self.change()
        self.flip(1)
        self.assertEqual(self.run_.keep_run("step")[0], "green")
        self.assertIn("calc.py", self.run_.keep_snap)


class Slow(Counted):
    """A suite whose flaky test takes 1.6 s, for a stop with a 3 s bound."""
    repo_seed = ("seed a slow suite", (("calc.py", CALC), ("test_calc.py", FLAKY.replace("SLEEP", "1.6")),
                                       (".gitignore", "flip.txt\nruns.txt\n")))

    def test_a_stop_whose_first_run_took_over_half_its_bound_counts_it_without_a_rerun(self):
        self.change()
        self.flip(1)
        before = self.runs()
        state, out = self.run_.keep_run("stop", 3)
        self.assertEqual(state, "red", out)
        self.assertEqual(self.runs() - before, 1, "no rerun past half the bound")

    def test_a_step_with_a_slow_suite_still_reruns(self):
        self.change()
        self.flip(1)
        before = self.runs()
        self.assertEqual(self.run_.keep_run("step", 10)[0], "green")
        self.assertEqual(self.runs() - before, 2)


class GreenBefore(Counted):
    """reruns: Counted's setUp, on a suite green before the work: no held set, so a red check is red at once, no rerun."""
    repo_seed = ("seed a green suite", (("calc.py", kp.ADD_OK), ("test_calc.py", FLAKY.replace("SLEEP", "0")),
                                        (".gitignore", "flip.txt\nruns.txt\n")))

    def test_a_green_suite_that_goes_red_is_checked_once(self):
        self.assertIsNone(self.run_.keep_held)
        self.edit(kp.MUL_BAD)
        before = self.runs()
        state, out = self.run_.keep_run("step")
        self.assertEqual(state, "red", out)
        self.assertEqual(self.runs() - before, 1, (self.run_.keep_cmd, out))


PYTEST_TESTS = ("import calc\n\n\ndef test_zero():\n    assert calc.add(1, 0) == 1\n\n\n"
                "def test_mul():\n    assert calc.mul(2, 3) == 6\n\n\n"
                "def test_add_two():\n    assert calc.add(2, 3) == 5\n")  # red before: add subtracts
HAS_PYTEST = importlib.util.find_spec("pytest") is not None


class PytestLines(CodeCase):
    """pytest -rA's summary lines (written out, so no pytest is needed) read as held and failed ids."""

    def test_the_summary_lines_of_pytest_are_per_test_results(self):
        seen = {}
        for ln in ("PASSED test_calc.py::test_zero", "PASSED test_calc.py::test_mul",
                   "FAILED test_calc.py::test_add_two - assert 1 == 5", "SKIPPED [1] test_calc.py:9: why"):
            seen.update(smith._per_test(ln))
        self.assertEqual(seen["test_calc.py::test_zero"], "PASSED")
        self.assertEqual(seen["test_calc.py::test_mul"], "PASSED")
        self.assertEqual(seen["test_calc.py::test_add_two"], "FAILED")


class Pytest(kp.Begun):
    """reruns: Begun's setUp, on a small pytest project red before the work."""
    repo_seed = ("seed a red pytest suite", (("calc.py", CALC), ("test_calc.py", PYTEST_TESTS)))
    suite = "python3 -m pytest -q -p no:cacheprovider"

    def setUp(self):
        if not HAS_PYTEST:
            self.skipTest("pytest is not installed here: the pytest fixture project cannot run")
        super().setUp()

    def test_the_passing_ids_of_a_pytest_project_are_held(self):
        run = self.run_
        self.assertEqual((run.keep_cmd, run.keep_state), (self.suite, "green"), run.keep_why)
        self.assertEqual(run.keep_held, {"test_calc.py::test_zero", "test_calc.py::test_mul"})

    def test_breaking_a_held_pytest_test_is_red_and_fixing_the_red_one_is_green(self):
        self.edit(kp.MUL_BAD)
        state, out = self.run_.keep_run("step")
        self.assertEqual(state, "red")
        self.assertIn("test_mul", out)
        self.edit(kp.ADD_OK)
        self.assertEqual(self.run_.keep_run("step")[0], "green")


class Judge(CodeCase):
    """The shared fake answers the request judge itself."""

    def post(self, fake, system):
        body = {"model": "m1", "messages": [{"role": "system", "content": system}, {"role": "user", "content": "Request: x"}]}
        req = urllib.request.Request(fake.url + "/chat/completions", json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())["choices"][0]["message"]["content"]

    def test_a_judge_call_before_a_run_reply_gets_the_default_and_leaves_the_script_alone(self):
        fake = self.serve("openai", plan(), call("done", summary="x"))
        said = json.loads(self.post(fake, smith.WANTS_PROMPT))
        self.assertIs(said["wants_change"], True)
        self.assertEqual(len(fake.script), 2, "the plan reply was not consumed")
        self.assertEqual(fake.seen, [], "the judge is not one of the run's requests")
        self.assertEqual(len(fake.judged), 1)

    def test_a_scripted_judge_reply_is_still_taken_first(self):
        fake = self.serve("openai", {"text": json.dumps({"wants_change": False, "why": "x"})}, plan())
        said = json.loads(self.post(fake, smith.WANTS_PROMPT))
        self.assertIs(said["wants_change"], False)
        self.assertEqual(len(fake.script), 1)
        self.assertEqual(len(fake.seen), 1)

    def test_a_scripted_reply_that_is_not_a_run_reply_is_taken_too(self):
        fake = self.serve("openai", {"text": "maybe, hard to say"}, plan())
        self.assertEqual(self.post(fake, smith.WANTS_PROMPT), "maybe, hard to say")
        self.assertEqual(len(fake.script), 1)

    def test_an_empty_script_still_answers_the_judge_with_an_error_as_before(self):
        fake = self.serve("openai")
        with self.assertRaises(urllib.error.HTTPError):
            self.post(fake, smith.WANTS_PROMPT)

    def test_a_request_that_is_not_the_judge_takes_the_next_reply(self):
        fake = self.serve("openai", plan())
        self.assertIn("Here is the plan", self.post(fake, "You plan."))
        self.assertEqual(fake.script, [])


class EndToEnd(CodeCase):
    """A run scripted with no judge reply: the plan is the plan."""
    repo_seed = kp.EndToEnd.repo_seed
    P = dict(PLAN, criteria=[{"text": "calc.add adds", "verify": "python3 -c 'import calc; assert calc.add(2, 3) == 5'"}])

    def test_a_run_with_no_judge_reply_in_its_script_gets_its_plan_accepted_and_finishes(self):
        f = self.serve("openai", plan(self.P), call("edit", file="calc.py", old="a - b", new="a + b"),
                       call("done", summary="adds"))
        r = self.code("fix add", "--model", "fake", "--json", "--no-commit", check=False)
        notes = [e.get("text", "") for e in kp.events(r) if e.get("event") == "note"]
        self.assertTrue(any(n.startswith("wants_change: True") for n in notes), notes)
        self.assertIn("return a + b", self.calc(), r.stdout + r.stderr)
        self.assertEqual(len(f.judged), 1)
