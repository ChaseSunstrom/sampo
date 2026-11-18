"""T-3160: fm code offers its task the calling cards that fit it, as a native tool and as a JSON action; a card call
runs through the card runner (schema, confinement, checker) and fm code's own guard, and the model reads its result."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

from test_callcards import confined
from test_code import CLOSE, CodeCase, call, journal_text, plan, text_action

GUARD = {"path": "calc.py", "function": "add", "param": "a", "value": "0"}
NONE_BUG = "import calc\ncalc.add(None, 3)\nprint('ok')\n"  # T-3181: the first failure is the None, a TypeError
SLOT = {"text": json.dumps({"param": "a"})}  # the one open slot of py-none-guard: which parameter may be None


def card(name, inputs):  # test_code's call() takes the action's name as its own first argument
    return {"calls": [("card", {"name": name, "inputs": inputs})]}


def card_text(name, inputs):
    return {"text": "I'll do this:\n" + json.dumps({"action": "card", "name": name, "inputs": inputs})}


@confined
class Cards(CodeCase):
    def test_a_native_card_call_changes_the_file_and_the_model_reads_its_diff(self):
        f = self.serve("openai", plan(), card("py-none-guard", GUARD),
                       call("edit", file="calc.py", old="a - b", new="a + b"), call("done", summary="adds"), CLOSE)
        r = self.code("make calc.add add, and return early if the parameter a is None", "--model", "fake",
                      check=False)
        self.assertIn("    if a is None:\n        return 0\n    return a + b", self.calc(), r.stdout + r.stderr)
        tools = [t["function"]["name"] for t in f.seen[1]["tools"]]
        self.assertIn("card", tools)
        self.assertIn("py-none-guard", f.said(1), "the card is offered by name")
        self.assertIn("card py-none-guard: applied", f.said(2))
        self.assertIn("+    if a is None:", f.said(2), "the model reads the diff")
        self.assertEqual(self.task()["status"], "done", r.stdout + r.stderr)

    def test_the_json_action_path_calls_a_read_card(self):
        f = self.serve("openai", plan(), card_text("py-outline", {"path": "calc.py"}),
                       text_action("edit", file="calc.py", old="a - b", new="a + b"),
                       text_action("done", summary="adds"), CLOSE, tools=False)
        r = self.code("outline calc.py, then make calc.add add instead of subtract", "--model", "fake", check=False)
        self.assertIn('\\"action\\": \\"card\\"', f.said(1), "the text form is taught")
        self.assertIn("py-outline", f.said(1))
        self.assertIn("card py-outline: facts", f.said(2))
        self.assertIn("return a + b", self.calc(), r.stdout + r.stderr)

    def test_a_card_not_offered_or_misspelt_is_named(self):
        f = self.serve("openai", plan(), card("py-none-gaurd", GUARD),
                       call("edit", file="calc.py", old="a - b", new="a + b"), call("done", summary="adds"), CLOSE)
        self.code("make calc.add add, and return early if the parameter a is None", "--model", "fake", check=False)
        self.assertIn("did you mean 'py-none-guard'", f.said(2))
        self.assertNotIn("if a is None", self.calc())

    def test_an_unknown_action_lists_card_among_the_actions(self):
        f = self.serve("openai", plan(), call("call_card", card="py-none-guard"),
                       call("edit", file="calc.py", old="a - b", new="a + b"), call("done", summary="adds"), CLOSE)
        self.code("make calc.add add, and return early if the parameter a is None", "--model", "fake", check=False)
        self.assertIn("no action 'call_card'; the actions are read, search, edit, write, run, done and card",
                      f.said(2))

    def test_cards_off_offers_none(self):
        f = self.serve("openai", plan(), call("edit", file="calc.py", old="a - b", new="a + b"),
                       call("done", summary="adds"), CLOSE)
        self.code("make calc.add add, and return early if the parameter a is None", "--model", "fake",
                  env={"SAMPO_CARDS": "off"}, check=False)
        self.assertNotIn("card", [t["function"]["name"] for t in f.seen[1]["tools"]])
        self.assertNotIn("py-none-guard", f.said(1))

    def test_a_model_step_card_and_a_chain_of_it_are_called_like_any_card(self):  # T-4837
        pick = {"text": json.dumps({"name": "add"})}  # the card's own local-model call: the fake server answers it next
        wrong = {"text": json.dumps({"name": "plus"})}
        f = self.serve("openai", plan(), card("pick-function", {"path": "calc.py", "description": "adds two numbers"}),
                       pick, card("locate-function", {"path": "calc.py", "description": "adds two numbers"}), pick,
                       card("locate-function", {"path": "calc.py", "description": "sums two values"}), wrong,
                       call("done", summary="calc.add (calc.py line 1) subtracts"))
        self.code("which function in calc.py adds two numbers, and which lines does it span", "--model", "fake", check=False)
        self.assertIn("locate-function (reads)", f.said(1), "a chain card is offered by name like any card")
        self.assertIn("card pick-function: facts", f.said(3), "a model-step card returns its checked answer")
        self.assertIn('\\"name\\": \\"add\\"', f.said(3))
        self.assertIn("card locate-function: facts", f.said(5), "a chain runs its steps and returns the last one's facts")
        self.assertIn('\\"end_line\\": 2', f.said(5))
        self.assertIn("step 1 (pick-function)", f.said(7), "a step the checker refuses is named")  # requests 2, 4, 6 are the model steps
        self.assertEqual(self.calc(), "def add(a, b):\n    return a - b\n")

    def test_a_question_runs_read_cards_only(self):
        judged = {"text": json.dumps({"wants_change": False, "why": "it asks what the code does"})}  # T-4284: no --ask
        f = self.serve("openai", {"text": json.dumps({"answer": "it subtracts"})}, judged,
                       card("py-outline", {"path": "calc.py"}), card("py-none-guard", GUARD),
                       call("done", summary="calc.add (calc.py line 1) subtracts"))
        r = self.code("outline calc.py: what does calc.add do?", "--model", "fake", "--json")
        self.assertIn("card py-outline: facts", f.said(3), r.stdout)
        notes = [json.loads(ln) for ln in r.stdout.splitlines() if '"cards"' in ln and ln.startswith("{")]
        self.assertIn("py-outline", notes[0]["cards"], "the run says what it was offered (the card bench reads it)")
        self.assertIn("refused", f.said(4))
        self.assertNotIn("py-none-guard", json.dumps(f.seen[2]["tools"]), "a write card isn't offered to a question")
        self.assertEqual(self.calc(), "def add(a, b):\n    return a - b\n")


@confined
class Harness(CodeCase):  # T-3181: the harness runs the read cards before turn 1, and applies a fix card a failure matches
    def code(self, *args, env=None, **kw):  # the harness is opt-in (SAMPO_CARDS=on) until a pilot proves it
        return super().code(*args, env={"SAMPO_CARDS": "on", **(env or {})}, **kw)

    def seed_none_bug(self, test=NONE_BUG, calc=None):
        for name, body in (("test_calc.py", test), ("calc.py", calc)):
            if body:
                with open(os.path.join(self.repo, name), "w") as f:
                    f.write(body)

    def test_read_cards_run_before_turn_one_and_their_facts_reach_the_prompt(self):
        f = self.serve("openai", plan(), call("done", summary="looked"), CLOSE)  # the seeded assertion: no fix card
        self.code("make calc.add add, and return early if the parameter a is None", "--model", "fake", check=False)
        self.assertIn("card failing-frame: facts", f.said(1), "the first turn already has the failing frame")
        self.assertIn("AssertionError: -1 at test_calc.py:2", f.said(1), "the frame's summary, with its numbered lines, is in the prompt")
        self.assertIn("assert calc.add(2, 3) == 5", f.said(1), "the frame's code line is in the prompt")
        s = f.said(1)  # the frame names only the test file here: its outline is run and its facts are in the prompt
        self.assertIn("card py-outline: facts", s, "the outline of each file the frame names is run too")
        self.assertIn("symbols", s[s.index("card py-outline: facts"):], "the outline's facts reach the prompt")
        self.assertNotIn("response_format", json.dumps(f.seen), "no fix card matched, so no slot was asked")
        self.assertIn("return a - b", self.calc())

    def test_a_matched_fix_card_is_applied_and_the_model_fills_one_schema_slot(self):
        self.seed_none_bug()
        f = self.serve("openai", plan(), SLOT, call("done", summary="guarded"), CLOSE)
        r = self.code("make calc.add handle None", "--model", "fake", check=False)
        self.assertIn("    if a is None:\n        return None\n    return a - b\n", self.calc(), r.stdout + r.stderr)
        schema = f.seen[1]["response_format"]["json_schema"]["schema"]
        self.assertEqual(sorted(schema["properties"]), ["param"], "the model fills the card's one open slot")
        self.assertEqual(schema["properties"]["param"]["enum"], ["a", "b"], "and only a parameter of the function")
        self.assertIn('"harness"', journal_text(self), "the journal records the harness step, so a resume replays it")
        self.assertIn("card py-none-guard: applied", f.said(2), "the step's first turn reads what the harness did")
        self.assertIn("def add(a, b)", f.said(2), "the frame's file was outlined too: the function the fix went into is listed")
        self.assertEqual(self.task()["status"], "done", r.stdout + r.stderr)

    def test_a_slot_answer_outside_the_schema_changes_nothing_and_the_run_goes_on(self):
        self.seed_none_bug()
        f = self.serve("openai", plan(), {"text": json.dumps({"param": "c"})}, call("done", summary="looked"), CLOSE)
        r = self.code("make calc.add handle None", "--model", "fake", check=False)
        self.assertEqual(self.calc(), "def add(a, b):\n    return a - b\n", r.stdout + r.stderr)
        self.assertIn("do step 1", f.said(2), "the run still reaches its first step")

    def test_a_failure_no_fix_card_matches_asks_no_slot(self):
        f = self.serve("openai", plan(), call("done", summary="looked"), CLOSE)
        self.code("make calc.add add, and return early if the parameter a is None", "--model", "fake", check=False)
        self.assertNotIn("response_format", json.dumps(f.seen))
        self.assertIn("return a - b", self.calc())

    def test_cards_off_runs_no_harness_step(self):
        self.seed_none_bug()
        f = self.serve("openai", plan(), call("done", summary="looked"), CLOSE)
        self.code("make calc.add handle None", "--model", "fake", env={"SAMPO_CARDS": "off"}, check=False)
        self.assertNotIn("response_format", json.dumps(f.seen))
        self.assertNotIn("card failing-frame", f.said(1))
        self.assertIn("return a - b", self.calc())

    def test_a_fix_that_leaves_the_verify_red_is_reverted_and_the_log_says_so(self):
        self.seed_none_bug("import calc\ncalc.add(None, 3)\nassert calc.add(2, 3) == 5\nprint('ok')\n")
        f = self.serve("openai", plan(), SLOT, call("done", summary="looked"), CLOSE)
        r = self.code("make calc.add handle None", "--model", "fake", check=False)
        self.assertEqual(self.calc(), "def add(a, b):\n    return a - b\n", r.stdout + r.stderr)
        self.assertIn("reverted", f.said(2), "the first turn is told the fix was undone")
        self.assertIn("TypeError", f.said(2), "and its baseline is the failure from before the fix")
        self.assertIn("reverted", journal_text(self))

    def test_a_method_is_fixed_by_its_dotted_name_not_a_same_named_module_function(self):
        self.seed_none_bug("import calc\ncalc.Calc().add(None, 3)\nprint('ok')\n",
                           "def add(a, b):\n    return a - b\n\n\nclass Calc:\n    def add(self, a, b):\n"
                           "        return a - b\n")
        # T-4388: two functions are a whole a review could read, so the request's kind is judged first (one named change)
        named = {"text": json.dumps({"wants_change": True, "whole": False, "find_fix": False, "why": "one named change"})}
        self.serve("openai", named, plan(), SLOT, call("done", summary="guarded"), CLOSE)
        r = self.code("make Calc.add handle None", "--model", "fake", check=False)
        self.assertEqual(self.calc(), "def add(a, b):\n    return a - b\n\n\nclass Calc:\n    def add(self, a, b):\n"
                         "        if a is None:\n            return None\n        return a - b\n", r.stdout + r.stderr)

    def test_a_failure_in_a_test_file_is_never_fixed_there(self):
        body = "import calc\n\n\ndef helper(x):\n    return x - 1\n\n\nhelper(None)\nprint('ok')\n"
        self.seed_none_bug(body)
        f = self.serve("openai", plan(), call("done", summary="looked"), CLOSE)
        self.code("make calc.add handle None", "--model", "fake", check=False)
        with open(os.path.join(self.repo, "test_calc.py")) as g:
            self.assertEqual(g.read(), body)
        self.assertNotIn("card py-none-guard: applied", f.said(1))

    def test_a_passing_verify_runs_no_harness(self):
        with open(os.path.join(self.repo, "calc.py"), "w") as g:
            g.write("def add(a, b):\n    return a + b\n")
        f = self.serve("openai", plan(), call("done", summary="nothing to do"), CLOSE)
        r = self.code("make calc.add add", "--model", "fake", check=False)
        self.assertNotIn("card failing-frame", json.dumps(f.seen), r.stdout + r.stderr)
        self.assertNotIn("response_format", json.dumps(f.seen))

    def test_no_model_means_no_slot_is_asked_and_nothing_is_changed(self):
        self.seed_none_bug()
        bin_ = os.path.join(self.tmp, "bin")
        os.makedirs(bin_)
        with open(os.path.join(bin_, "noop"), "w") as g:
            g.write("#!/bin/sh\nexit 0\n")
        os.chmod(os.path.join(bin_, "noop"), 0o755)
        r = self.code("FIX: make calc.add handle None", "--worker", "noop {prompt}", "--verify", "python3 test_calc.py",
                      "--no-commit", env={"PATH": bin_ + os.pathsep + os.environ["PATH"]}, check=False)
        self.assertNotIn("AttributeError", r.stdout + r.stderr)
        self.assertEqual(self.calc(), "def add(a, b):\n    return a - b\n")
        self.assertIn("no model", journal_text(self))


@confined
class HarnessOff(CodeCase):  # T-3181: the pilot is "not proven", so a default run (no SAMPO_CARDS=on) has no harness step
    def test_a_default_run_runs_no_harness(self):
        with open(os.path.join(self.repo, "test_calc.py"), "w") as f:
            f.write(NONE_BUG)
        f = self.serve("openai", plan(), call("done", summary="looked"), CLOSE)
        self.code("make calc.add handle None", "--model", "fake", env={"SAMPO_CARDS": "", "FOREMAN_CARDS": ""},
                  check=False)
        self.assertNotIn("response_format", json.dumps(f.seen))
        self.assertNotIn("card failing-frame", f.said(1))
        self.assertNotIn('"harness"', journal_text(self))
        self.assertIn("return a - b", self.calc())


class Dispatch(CodeCase):  # T-4837: what a card call does to the run around it
    def run_object(self, session):
        import types
        from sampo import smith
        run = smith.Run(types.SimpleNamespace(), self.repo, self.tmp)
        run.emit = lambda *a, **k: None
        run.cards_session = types.SimpleNamespace(call=session)
        return run

    def test_a_card_that_raises_is_one_error_line_and_the_run_goes_on(self):
        def boom(name, inputs):
            raise RuntimeError("the card layer broke")
        res, finished = self.run_object(boom).act("card", {"name": "pick-function", "inputs": {}})
        self.assertTrue(res.startswith("error: "), res)
        self.assertIn("the card layer broke", res)
        self.assertIn("failed inside the runner", res)
        self.assertIn("RuntimeError", res)
        self.assertRegex(res, r"(?i)files may have changed.*re-read", "the model is told to look before redoing an edit")
        self.assertFalse(finished)

    def test_a_deeply_nested_card_reply_that_escaped_as_recursion_error_is_the_same_error_line(self):
        def deep(name, inputs):
            raise RecursionError("maximum recursion depth exceeded")
        run = self.run_object(deep)
        res, finished = run.act("card", {"name": "pick-function", "inputs": {}})
        self.assertIn("failed inside the runner (RecursionError", res)
        self.assertFalse(finished)
        again, _ = run.act("card", {"name": "pick-function", "inputs": {}})  # and the run takes the next action
        self.assertTrue(again.startswith("error: "), again)

    def test_an_applied_write_is_recorded_before_the_card_s_tokens_are_charged(self):
        from sampo import callcards, smith
        r = callcards._result("pick-function", "applied", changed=["a.txt"], usage={"in": 1, "out": 1})
        run = self.run_object(lambda name, inputs: r)
        seen = []
        run.after = lambda tool, ti, to: seen.append(ti["file_path"])

        def over(*a, **k):
            raise smith.CodeError("the cap is reached")
        run.usage = over
        with self.assertRaises(smith.CodeError):
            run.act("card", {"name": "pick-function", "inputs": {}})
        self.assertEqual(seen, [os.path.join(self.repo, "a.txt")], "the write that landed was recorded before the cap ended the run")

    def test_the_budget_cap_still_ends_the_run_through_a_card_call(self):
        from sampo import smith

        def capped(name, inputs):
            raise smith.CodeError("the cap is reached")
        with self.assertRaises(smith.CodeError):
            self.run_object(capped).act("card", {"name": "pick-function", "inputs": {}})

    def test_a_spent_cap_when_a_card_closes_stops_the_run_with_the_cap_s_own_message(self):
        # T-4860: the card's escalation asks the adviser, whose first act is the cap check: its CodeError is the run's
        # stop, not swallowed as "no advice" to surface later from some other call
        import types
        from unittest import mock
        from sampo import callcards, smith
        run = smith.Run(types.SimpleNamespace(), self.repo, self.tmp)
        run.emit = lambda *a, **k: None
        run.max_usd, run.usd, run.tid, run.request = 1.0, 2.5, None, "fix add"
        failed = lambda name, inputs, root, **kw: callcards._result(name, "checker_failed", error="still red")
        with mock.patch.object(run, "aide", return_value={"kind": "openai"}), \
                mock.patch.object(callcards, "call", failed), \
                mock.patch.object(callcards, "cards", return_value=({}, [])):
            s = run.session()
            for i in range(callcards.ATTEMPTS - 1):
                self.assertEqual(s.call("pick-function", {"try": i})["status"], "checker_failed")
            with self.assertRaisesRegex(smith.CodeError, r"spent \$2\.50 of its \$1 cap .* before card was done"):
                s.call("pick-function", {"try": "last"})

    def test_a_model_cards_tokens_are_charged_to_the_run_even_when_its_answer_failed(self):
        from sampo import callcards
        good = callcards._result("pick-function", "facts", facts={"name": "add"}, usage={"in": 10, "out": 5}, model="m")
        bad = callcards._result("pick-function", "generator_failed", error="no JSON", usage={"in": 7, "out": 3})
        run = self.run_object(lambda name, inputs: good)
        run.act("card", {"name": "pick-function", "inputs": {}})
        self.assertEqual(run.tokens, [10, 5])
        run.cards_session.call = lambda name, inputs: bad
        run.act("card", {"name": "pick-function", "inputs": {}})
        self.assertEqual(run.tokens, [17, 8], "a failed model answer cost tokens too")
        run.cards_session.call = lambda name, inputs: callcards._result("measure", "facts", facts=1)
        run.act("card", {"name": "measure", "inputs": {}})
        self.assertEqual(run.tokens, [17, 8], "a card that asked no model costs none")


class Skips(unittest.TestCase):
    def test_a_test_file_or_the_verify_target_is_off_limits_to_a_fix(self):
        from sampo import smith
        failed = [(1, "python3 check_calc.py", "")]
        for rel in ("test_calc.py", "tests/helpers.py", "calc_test.py", "conftest.py", "check_calc.py"):
            self.assertTrue(smith._off_limits(rel, failed), rel)
        self.assertFalse(smith._off_limits("calc.py", failed))

    def test_a_methods_name_is_dotted_with_its_class(self):
        from sampo import smith
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "m.py"), "w") as g:
                g.write("def add(a):\n    return a\n\n\nclass K:\n    def add(self, a):\n        return a\n"
                        "\n    def outer(self):\n        def inner(a):\n            return a\n")
            for line, want in ((2, "add"), (7, "K.add"), (11, "K.outer.inner")):
                self.assertEqual(smith._enclosing(d, "m.py", line)[1], want)
            self.assertEqual(smith._enclosing(d, "m.py", 5), (None, None))


class Portable(unittest.TestCase):
    """T-3238: on macOS (no Landlock) every test that runs a card for real must skip, not fail: CI was red on main."""

    def test_card_tests_skip_where_calls_cannot_be_confined(self):
        here = os.path.dirname(os.path.abspath(__file__))
        boot = ("import sys\nsys.path[:0] = [sys.argv[1], sys.argv[2]]\nfrom sampo import landlock\n"
                "landlock.available = lambda: (False, 'Landlock is Linux only (this is Darwin)')\nimport unittest\n"
                "unittest.main(module=None, argv=['unittest', 'test_callcards', 'test_smith_cards.Cards', "
                "'test_smith_cards.Harness', 'test_smith_cards.HarnessOff'])\n")
        r = subprocess.run([sys.executable, "-c", boot, os.path.join(here, "..", "lib"), here], cwd=here,
                           capture_output=True, text=True, timeout=600)
        self.assertEqual(r.returncode, 0, r.stderr[-3000:])
        self.assertIn("skipped=", r.stderr)
