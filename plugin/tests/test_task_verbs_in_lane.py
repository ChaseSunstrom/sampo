"""T-4606: a task verb run with a lane worktree as the working directory either writes to the task or refuses loudly,
never exits 0 having written nothing. Seen on a real project: `sampo task audit ID intent HOW RESULT` from inside a lane
printed only `Next: sampo next` and recorded nothing, while the same command from the main checkout recorded it. Cause:
the task was a builder's (sampo lane brief marks it), which a verb that closes or audits it refuses from a lane; the
refusal's last line, the one a `| tail` or a skimmer keeps, was the generic `Next: sampo next`. The refusal now ends in
the command that works (from the main checkout) and says nothing was written. The fixture is a project with a lane
worktree and a task made in the lane; each verb runs with cwd inside that lane, on a plain task and on a builder's."""
# map-links: plugin/lib/sampo/cli.py
import json
import os

from test_lane_train import TrainCase

from sampo import core as c


class VerbsInLane(TrainCase):
    builder = False

    def setUp(self):
        super().setUp()
        self.tid = self.lane(1, "a.txt")
        self.path = os.path.join(self.tmp, "lane1")
        if self.builder:
            p = c.find_project(self.repo)
            b = c.find_brief(p, self.tid)
            b.meta["builder"] = c.now()
            c.save_brief(p, b)

    def brief(self):
        return c.find_brief(c.find_project(self.repo), self.tid)

    def run_in_lane(self, *argv):
        return self.fm("task", *argv, cwd=self.path, check=False)

    def written_or_refused(self, r, wrote):
        """exit 0 means it wrote; a refusal exits non-zero, says why, and the last line names the command that works."""
        if r.returncode == 0:
            self.assertTrue(wrote(), f"exit 0 and nothing written:\n{r.stdout}\n{r.stderr}")
            return
        self.assertFalse(wrote(), "a refusal wrote nothing")
        self.assertIn("sampo: refused:", r.stderr)
        last = r.stderr.strip().splitlines()[-1]
        self.assertNotEqual(last, "Next: sampo next", "the last line names the real next step")
        self.assertRegex(last, r"^Next: .*sampo task ")

    def test_audit_from_the_lane(self):
        r = self.run_in_lane("audit", self.tid, "intent", "read the diff", "matches the request")
        self.written_or_refused(r, lambda: bool(self.brief().audits()))
        if r.returncode == 0:
            self.assertIn("audit (intent) recorded", r.stdout)
        else:
            self.assertIn("Nothing was written", r.stderr)
            self.assertIn(f"sampo task audit {self.tid} intent", r.stderr.strip().splitlines()[-1])

    def test_done_finish_and_drop_from_the_lane(self):
        for verb in ("done", "finish", "drop"):
            r = self.run_in_lane(verb, self.tid)
            self.assertTrue(r.returncode != 0 or self.brief().status in ("done", "dropped"), (verb, r.stdout, r.stderr))
            if self.builder:
                self.assertEqual(r.returncode, 2, (verb, r.stdout, r.stderr))
                self.assertIn(f"sampo task {verb} {self.tid}", r.stderr.strip().splitlines()[-1])

    def test_log_from_the_lane(self):
        r = self.run_in_lane("log", self.tid, "steer: a note from the lane")
        self.written_or_refused(r, lambda: "a note from the lane" in self.brief().section("Log"))
        self.assertEqual(r.returncode, 0, "builders log from their lane: it must work")

    def test_set_from_the_lane(self):
        r = self.run_in_lane("set", self.tid, "--section", "Non-goals", "--text", "nothing else from the lane")
        self.written_or_refused(r, lambda: "nothing else from the lane" in self.brief().section("Non-goals"))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_ac_add_from_the_lane(self):
        r = self.run_in_lane("ac", self.tid, "add", "a second criterion", "--verify", "true")
        self.written_or_refused(r, lambda: "a second criterion" in self.brief().section("Acceptance criteria"))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_step_add_from_the_lane(self):
        r = self.run_in_lane("step", self.tid, "add", "a second step from the lane")
        self.written_or_refused(r, lambda: "a second step from the lane" in self.brief().section("Steps"))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_the_same_verbs_from_the_main_checkout_write(self):
        self.fm("task", "audit", self.tid, "intent", "read the diff", "matches the request")
        self.assertTrue(self.brief().audits())
        self.fm("task", "log", self.tid, "steer: a note from main")
        self.assertIn("a note from main", self.brief().section("Log"))
        self.assertTrue(json.loads(self.fm("task", "show", self.tid, "--json").stdout))


class BuilderTaskInLane(VerbsInLane):
    """The same checks on a builder's task: the audit is refused naming the command to run from the main checkout.
    reruns: every verb's check again with the task marked a builder's, since the refusal rule keys on that mark."""
    builder = True

    def test_audit_refusal_names_the_main_checkout(self):
        r = self.run_in_lane("audit", self.tid, "intent", "read the diff", "matches the request")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertEqual(self.brief().audits(), [])
        self.assertIn("builder's task", r.stderr)
        self.assertIn("cd ", r.stderr.strip().splitlines()[-1])
        self.assertNotIn(self.path, r.stderr.strip().splitlines()[-1], "the main checkout, not the lane")
        self.assertTrue(self.brief().meta.get("builder"))
