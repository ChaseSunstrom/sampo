"""T-2644: fm task finish reuses a criterion's passing run when the same command already ran on this exact tree and
world, instead of running it again; a different tree, world or command, or a failed later run, runs it again."""
import os

from helpers import ForemanTestCase


class FinishReuse(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.count = os.path.join(self.tmp, "runs")
        self.flag = os.path.join(self.tmp, "fail-now")  # outside the repo: flipping it leaves the tree alone
        self.cmd = f"sh -c 'echo x >> {self.count}; test ! -e {self.flag}'"
        self.fm("task", "new", "Reuse a pass", "--type", "FEATURE", "--tier", "S", "--ac", f"it passes :: {self.cmd}",
                "--step", "do it", "--focus")

    def runs(self):
        return len(open(self.count).read().splitlines()) if os.path.exists(self.count) else 0

    def test_a_pass_on_this_tree_and_world_is_reused_not_run_again(self):
        self.fm("task", "evidence", "T-0001", "--ac", "1", "--run", self.cmd)
        self.assertEqual(self.runs(), 1)
        out = self.fm("task", "finish", "T-0001", "--audit", "self").stdout
        self.assertEqual(self.runs(), 1)  # not run a second time
        self.assertIn("reused 1 run", out.lower())
        self.assertIn("status: done", self.fm("task", "show", "T-0001").stdout)

    def test_an_edit_since_the_pass_runs_it_again(self):
        self.fm("task", "evidence", "T-0001", "--ac", "1", "--run", self.cmd)
        with open(os.path.join(self.repo, "changed.txt"), "w") as f:
            f.write("new\n")
        self.fm("task", "finish", "T-0001", "--audit", "self")
        self.assertEqual(self.runs(), 2)  # the tree moved: the pass is stale

    def test_a_failed_run_after_the_pass_runs_it_again(self):
        self.fm("task", "evidence", "T-0001", "--ac", "1", "--run", self.cmd)
        open(self.flag, "w").close()
        self.fm("task", "evidence", "T-0001", "--ac", "1", "--run", self.cmd, check=False)  # the same command, red now
        os.remove(self.flag)
        self.fm("task", "finish", "T-0001", "--audit", "self")
        self.assertEqual(self.runs(), 3)  # its newest run failed: the close runs it again


class FinishPriority(ForemanTestCase):
    """T-5792: a close from the main checkout is landing work every lane waits on, so its criterion runs take freed
    test slots ahead of builder runs (the slot-priority switch), as the gates already do."""

    def test_a_close_from_the_main_checkout_runs_its_criteria_at_slot_priority(self):
        self.fm("init")
        seen = os.path.join(self.tmp, "prio")
        cmd = f"sh -c 'echo \"${{FOREMAN_SLOT_PRIORITY:-unset}}\" > {seen}'"
        env = {k: v for k, v in os.environ.items() if k not in ("FOREMAN_SLOT_PRIORITY", "SAMPO_SLOT_PRIORITY")}
        self.fm("task", "new", "Close at priority", "--type", "FEATURE", "--tier", "S", "--ac", f"it runs :: {cmd}",
                "--step", "do it", "--focus", env=env)
        self.fm("task", "finish", "T-0001", "--audit", "self", env=env)
        self.assertEqual(open(seen).read().strip(), "1")
