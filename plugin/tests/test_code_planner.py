"""T-2945: fm code --planner P --model A asks the plan and the close notes of P and runs every step on A."""
import json
import os
import tempfile
import unittest
from unittest import mock

from test_code import CLOSE, FIX, M_PLAN, CodeCase, journal_text, plan

from sampo import louhi, models, smithbench  # noqa: E402  (test_code puts plugin/lib on the path)


class CodePlanner(CodeCase):
    def split(self, *flags):
        big = self.serve("anthropic", plan(M_PLAN), CLOSE, name="big")
        small = self.serve("anthropic", *FIX, name="small")
        return self.code("fix add", "--model", "small", *flags, check=False), big, small

    def test_the_plan_and_close_notes_go_to_the_planner_and_the_steps_to_the_model(self):
        r, big, small = self.split("--planner", "big")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(big.seen), 2, "the plan and the close notes")
        self.assertEqual(len(small.seen), 2, "the two step turns")
        self.assertIn("Make calc.add add", big.said(1) + small.said(0))
        start = json.loads(next(l for l in journal_text(self, "T-0001").splitlines() if '"kind": "start"' in l))
        self.assertEqual((start["model"], start["planner"]), ("small", "big"))

    def test_without_planner_every_slot_goes_to_the_model(self):
        small = self.serve("anthropic", plan(M_PLAN), *FIX, CLOSE, name="small")
        r = self.code("fix add", "--model", "small")
        self.assertEqual(len(small.seen), 4, r.stdout)

    def test_a_bad_planner_fails_before_the_first_call(self):
        small = self.serve("anthropic", plan(M_PLAN), *FIX, CLOSE, name="small")
        r = self.code("fix add", "--model", "small", "--planner", "nope", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("nope", r.stdout + r.stderr)
        self.assertEqual(small.seen, [])


class CliPlanner(CodeCase):
    """T-4449: --planner takes a planner CLI's name (claude, codex, gemini): it plans in its read-only mode and writes
    the close notes, the --model profile does every step. The cascade: a big model plans once, a free one works."""

    def setUp(self):
        super().setUp()
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        self.args = os.path.join(self.tmp, "planner-args")

    def claude(self, *replies):
        """A fake claude: each call appends its argv to self.args and prints the next reply as a result line."""
        for n, text in enumerate(replies):
            with open(os.path.join(self.tmp, f"reply{n}.json"), "w") as f:
                f.write(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": text,
                                    "usage": {"input_tokens": 10, "output_tokens": 20}}))
        path = os.path.join(self.bin, "claude")
        with open(path, "w") as f:
            f.write(f'#!/bin/sh\nn=$(cat {self.tmp}/calls 2>/dev/null || echo 0)\nprintf "%s\\n" "== call $n" "$@" >> '
                    f'{self.args}\necho $((n + 1)) > {self.tmp}/calls\ncat {self.tmp}/reply$n.json\n')
        os.chmod(path, 0o755)

    def go(self, *args):
        return self.code("fix add", "--model", "small", *args, check=False,
                         env={"PATH": self.bin + os.pathsep + os.environ["PATH"]})

    def test_cli_planner_plans_read_only_and_the_model_does_the_steps(self):
        self.claude(json.dumps(M_PLAN), CLOSE["text"])
        small = self.serve("anthropic", *FIX, name="small")
        r = self.go("--planner", "claude", "--json")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.task()["status"], "done", r.stdout + r.stderr)
        self.assertIn("return a + b", self.calc())
        self.assertEqual(len(small.seen), 2, "the two step turns, and no plan or close call")
        with open(self.args) as f:
            argv = f.read()
        self.assertEqual(argv.count("== call"), 2, "the plan and the close notes")
        self.assertIn("--permission-mode\nplan\n", argv, "in its read-only mode, both times")
        self.assertIn("fix add", argv, "the request reached it")
        events = [json.loads(line) for line in r.stdout.splitlines() if line.startswith("{")]
        start = next(e for e in events if e["event"] == "start")
        self.assertEqual((start["model"], start["planner"]), ("small", "claude (plan, read-only)"))
        stats = next(e for e in events if e["event"] == "stats")
        self.assertEqual(stats["planner"], "claude (plan, read-only)")

    def test_cli_planner_name_that_is_neither_a_profile_nor_a_cli_names_both_lists(self):
        small = self.serve("anthropic", plan(M_PLAN), *FIX, CLOSE, name="small")
        r = self.go("--planner", "nope")
        self.assertNotEqual(r.returncode, 0)
        out = r.stdout + r.stderr
        self.assertIn("nope", out)
        self.assertIn("code models", out, "where the profiles are listed")
        self.assertIn("claude", out, "and the planner CLIs")
        self.assertEqual(small.seen, [])

    def test_cli_planner_missing_from_the_path_fails_before_any_call(self):
        small = self.serve("anthropic", plan(M_PLAN), *FIX, CLOSE, name="small")
        r = self.code("fix add", "--model", "small", "--planner", "claude", check=False,
                      env={"PATH": self.bin + os.pathsep + "/usr/bin" + os.pathsep + "/bin"})  # no claude in it
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("claude", r.stderr)
        self.assertIn("Next:", r.stderr)
        self.assertEqual(small.seen, [])

    def test_cli_planner_is_refused_on_a_sensitive_repo(self):
        self.claude(json.dumps(M_PLAN), CLOSE["text"])
        self.serve("anthropic", *FIX, name="small")
        self.fm("init")
        self.fm("sensitive", "on")
        r = self.go("--planner", "claude")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("sensitive", r.stderr)
        self.assertFalse(os.path.exists(self.args), "the CLI was never run")


class ExamArm(unittest.TestCase):
    def test_the_smith_arm_passes_planner_through_and_records_it(self):
        fake = "import json,sys\nprint(json.dumps({'event': 'closed', 'text': ' '.join(sys.argv)}))\n"
        done = {"text": '{"findings": []}', "tool_calls": [], "usage": {"in": 1, "out": 1}}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(models, "resolve", lambda m: {"name": m}), \
                mock.patch.object(models, "complete", lambda *a, **k: done):
            os.makedirs(os.path.join(tmp, "base", "repo"))
            fm = os.path.join(tmp, "fm.py")
            with open(fm, "w") as h:
                h.write(fake)
            for planner in ("big", None):
                with mock.patch.object(smithbench, "FM", fm):
                    ans = louhi._smith_arm(os.path.join(tmp, "base"), {"request": "r", "kind": "bug"},
                                           os.path.join(tmp, f"w{planner}"), "small", None,
                                           os.path.join(tmp, "e.jsonl"), planner)
                self.assertEqual(ans["planner"], planner)
                with open(os.path.join(tmp, "e.jsonl")) as h:
                    self.assertEqual("--planner big" in h.read(), planner == "big")
