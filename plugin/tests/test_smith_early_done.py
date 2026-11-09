"""T-3180: a done with no edit made while a plan criterion's verify is red gets that failure back, at most twice, then
the run stops early; the journal and the bench result count these stops."""
import json

from test_code import PLAN, CodeCase, call, journal_text, plan

READ = call("read", path="calc.py")
DONE = call("done", summary="looks fine")
FIX = [call("edit", file="calc.py", old="a - b", new="a + b"), DONE]


def early(case):
    return [json.loads(l) for l in journal_text(case).splitlines() if '"early_done"' in l]


class EarlyDone(CodeCase):
    def test_a_done_after_one_read_gets_the_failing_verify_back_and_must_edit(self):
        f = self.serve("openai", plan(PLAN), READ, DONE, *FIX)
        r = self.code("fix add", "--model", "fake")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("test_calc.py", f.said(3))  # the turn after the refused done names the check
        self.assertIn("AssertionError", f.said(3))
        self.assertEqual(self.task()["status"], "done")
        self.assertEqual(len(early(self)), 1)

    def test_a_third_done_without_an_edit_ends_the_run_stopped_early(self):
        self.serve("openai", plan(PLAN), READ, DONE, DONE, DONE)
        r = self.code("fix add", "--model", "fake", check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("stopped early", r.stderr)
        self.assertEqual(len(early(self)), 3)

    def test_an_edit_made_through_run_counts_as_an_edit(self):
        self.serve("openai", plan(PLAN), call("run", cmd="perl -pi -e 's/a - b/a * b/' calc.py"),
                   *[call("done", summary="edited") for _ in range(4)])
        self.code("fix add", "--model", "fake", check=False)
        self.assertEqual(early(self), [], "the tree changed, so done was not refused for lack of an edit")
        self.assertEqual(self.task()["status"], "blocked")  # the red check is reported by the block, as before
        self.assertIn("test_calc.py", self.task().get("blocked_reason", "") + journal_text(self))

    def test_the_bench_counts_them(self):
        from sampo import smithbench
        self.assertEqual(smithbench.early_dones([{"event": "early_done"}, {"event": "verify"}]), 1)
