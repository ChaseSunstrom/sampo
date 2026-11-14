"""T-4593: a Smith run that did its work ended blocked at the close because fm task finish refused on a capability miss
for a tool the worker tried once and dropped (a test runner not installed here; every criterion and the keep check were
green). The worker is told at the failed command that the tool is not here and what the project's own test command is;
the close sets aside a miss no criterion's verify and no keep check invokes, and says so; a miss a verify invokes still
blocks. The tool name is made up: certainly not installed anywhere."""
import json
import os

from test_code import CLOSE, PLAN, call, plan
from test_smith_small import DONE, FIX_EDIT, wants
from test_smith_survey import READ_ALL, SURVEY_DONE, SurveyCase

TOOL = "zzdetourtool4593"
TRY = call("run", cmd=f"python3 -m {TOOL} test_calc.py")


class ToolDetour(SurveyCase):
    def closed(self, ev):
        return next(e for e in ev if e["event"] == "closed")

    def test_a_tool_the_worker_tried_and_dropped_does_not_block_the_close_and_is_named(self):
        f, r, ev = self.go(wants(True), *READ_ALL, SURVEY_DONE, plan(PLAN), TRY, FIX_EDIT, DONE, CLOSE)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.task()["status"], "done")
        text = self.closed(ev)["text"]
        self.assertIn(TOOL, text)
        self.assertIn("tried and not needed", text)

    def test_the_failed_command_says_the_tool_is_not_here_and_names_the_projects_own_test_command(self):
        f, r, ev = self.go(wants(True), *READ_ALL, SURVEY_DONE, plan(PLAN), TRY, FIX_EDIT, DONE, CLOSE)
        after = f.said(self.planner(f) + 2)  # the request after the failed run carries its result
        self.assertIn(f"`{TOOL}` is not installed here", after)
        self.assertIn("python3 test_ok.py", after.split("is not installed here", 1)[1][:400], "the project's own runner")

    def test_a_miss_a_criterions_verify_invokes_still_blocks_the_close(self):
        masked = dict(PLAN, criteria=[{"text": "calc.add adds, and the tool is absent",
                                       "verify": f"python3 test_calc.py && ! python3 -m {TOOL} -h"}])
        # T-4597: the refusal goes back to the worker for two rounds (it mends nothing here), then blocks as before
        f, r, ev = self.go(wants(True), *READ_ALL, SURVEY_DONE, plan(masked), TRY, FIX_EDIT, DONE, CLOSE, DONE, DONE)
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.task()["status"], "blocked")
        self.assertIn("capability miss", self.task().get("blocked_reason", "") + r.stderr + r.stdout)

    def test_a_tool_counts_as_invoked_by_a_whole_word_of_a_command(self):
        from sampo import miss
        self.assertTrue(miss.invoked("pytest", "python3 -m pytest -q"))
        self.assertTrue(miss.invoked("pytest", "cd x && pytest"))
        self.assertFalse(miss.invoked("pytest", "python3 -m unittest discover -q"))
        self.assertFalse(miss.invoked("pytest", "python3 test_pytest_helper.py"), "a longer word is another word")
        self.assertFalse(miss.invoked("pytest", "python3 -m pytest-cov-not"), "nor a hyphenated one")

    def test_a_missing_tool_is_read_from_the_output_and_a_project_module_is_not_one(self):
        from sampo import miss
        self.assertEqual(miss.absent(self.repo, f"/usr/bin/python3: No module named {TOOL}\n"), TOOL)
        self.assertEqual(miss.absent(self.repo, f"bash: {TOOL}: command not found\n"), TOOL)
        self.assertEqual(miss.absent(self.repo, "No module named calc\n"), "", "calc.py is the project's own file")
        self.assertEqual(miss.absent(self.repo, "AssertionError: 5 != -1\n"), "")

    def test_a_second_run_of_the_close_logs_nothing_more(self):
        f, r, ev = self.go(wants(True), *READ_ALL, SURVEY_DONE, plan(PLAN), TRY, FIX_EDIT, DONE, CLOSE)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        from sampo import core
        p = core.find_project(self.repo)
        b = core.find_brief(p, "T-0001")
        from sampo import miss
        self.assertEqual(miss.open_misses(p, b), [])
        self.assertEqual(miss.set_aside(p, b, "s", ["python3 test_ok.py"], "again"), [], "nothing open: nothing logged")
        why = [json.loads(ln)["data"].get("why") for ln in open(os.path.join(p.dir, "ledger.jsonl"))
               if '"miss_resolved"' in ln]
        self.assertEqual(len(why), 1)
        self.assertIn("no criterion", why[0])
