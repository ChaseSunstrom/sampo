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


def read(*lines):
    """A rival read's reply: readings, then one approach the plan has."""
    return {"text": "## Readings\n" + "".join(f"- {x}\n" for x in lines)
            + "## Approaches\n- change the operator in calc.add\n## Failure modes\n- a caller relies on it\n"}


SAME = "calc.add subtracts by mistake, so it should add"  # the plan's own first reading
OTHER = "`notes.db` is a ledger other jobs append to, so removing it loses their rows"


class RivalGate(CodeCase):
    """T-4466: the cold rival read the task gate asks for a one-way step, which is only marked when the task starts.
    The run fetches it from its own model and answers what it opens; before, the cascade died here before step 1."""

    def go(self, big, small):
        with open(os.path.join(self.repo, "notes.db"), "w") as f:
            f.write("totals\n")
        p = dict(M_PLAN, steps=["fix calc.add, then drop the stale totals: `rm notes.db`"])
        self.big = self.serve("anthropic", plan(p), *big, name="big")
        self.small = self.serve("anthropic", *small, name="small")
        r = self.code("fix add", "--model", "small", "--planner", "big", check=False, env={"FOREMAN_NO_RIVAL": "0"})
        from sampo import core
        return r, core.find_brief(core.find_project(self.repo), "T-0001")

    def test_rival_read_comes_from_the_runs_own_model_and_the_run_reaches_step_1(self):
        r, b = self.go([CLOSE], [read(SAME), *FIX])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.task()["status"], "done", r.stdout)
        self.assertTrue(b.meta.get("rival_at"), r.stdout)
        self.assertIn("A cold read on small", b.section("Rival plan"))
        self.assertIn("Failure modes", self.small.said(0), "the read's own prompt")
        self.assertNotIn("Make calc.add add", self.small.said(0), "the reader never sees the plan")
        self.assertEqual(len(self.big.seen), 2, "the plan and the close notes: nothing was open to answer")

    def test_rival_lines_are_answered_by_the_plans_author_and_recorded(self):
        r, b = self.go([{"text": "1: rejected: the request asks only for calc.add, and calls the totals stale"}, CLOSE],
                       [read(SAME, OTHER), *FIX])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.task()["status"], "done", r.stdout)
        self.assertIn("a ledger other jobs append to", self.big.said(1), "the open line was put to the planner")
        self.assertIn("answered: rejected: the request asks only for calc.add", b.section("Dissent"))
        self.assertNotIn("- [ ] rival", b.section("Dissent"))

    def test_rival_lines_left_unanswered_stay_open_and_the_gate_still_refuses(self):
        r, b = self.go([{"text": "I would rather not say."}], [read(SAME, OTHER), *FIX])
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("- [ ] rival reading: `notes.db` is a ledger", b.section("Dissent"))
        self.assertIn("rival", r.stdout + r.stderr)
        self.assertIn("a - b", self.calc(), "no step ran")


class VerifyDoor(CodeCase):
    """T-4466: a verify that writes over a file git doesn't track. The plan check ran it before any work with no
    snapshot behind it, and the gate then met the check's own output as somebody's data (exam cascade3: the run ended
    before step 1, the same verify sent back three times because the gate's line names no path)."""

    def run_with(self, verify, *big, kind="FIX"):
        p = dict(M_PLAN, type=kind, criteria=[{"text": "calc.add adds", "verify": verify}])
        self.big = self.serve("anthropic", plan(p), *big, CLOSE, name="big")
        self.serve("anthropic", *FIX, name="small")
        r = self.code("fix add", "--model", "small", "--planner", "big", check=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def test_verify_door_at_the_plan_is_never_run_and_the_planner_is_told_the_path(self):
        log = os.path.join(self.tmp, "day.log")
        with open(log, "w") as f:
            f.write("somebody's day\n")
        r = self.run_with(f"python3 test_calc.py > {log}", plan(M_PLAN))
        self.assertEqual(self.task()["status"], "done", r.stdout)
        self.assertIn(log, self.big.said(1), "the path the verify would write over is named")
        with open(log) as f:
            self.assertEqual(f.read(), "somebody's day\n", "and it was never run")

    def test_verify_door_is_not_made_of_the_plan_checks_own_output(self):
        r = self.run_with(f"python3 test_calc.py > {os.path.join(self.tmp, 'fresh.log')}")
        self.assertEqual(self.task()["status"], "done", r.stdout)
        from sampo import core
        b = core.find_brief(core.find_project(self.repo), "T-0001")
        self.assertFalse(b.meta.get("door_acs"), "the file the plan check made was gone again when the task started")
        self.assertEqual(len(self.big.seen), 2, "the plan and the close notes")

    def test_verify_door_at_the_gate_is_asked_again_with_the_path_named(self):
        log = os.path.join(self.tmp, "day.log")
        with open(log, "w") as f:
            f.truncate(21 * 1024 * 1024)  # sparse, and over the snapshot cap: no copy can cover it, so the gate parks
        r = self.run_with(f"python3 test_calc.py > {log}", {"text": "python3 test_calc.py"}, kind="CLEAN")  # not run
        self.assertEqual(self.task("T-0002")["status"], "done", r.stdout)  # at the plan: the gate is the first to read it
        self.assertIn(log, self.big.said(1), "the path the verify would write over is named")
        self.assertEqual(os.path.getsize(log), 21 * 1024 * 1024, "and the file is as it was")


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
