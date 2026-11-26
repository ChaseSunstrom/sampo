"""T-6426 and T-6434: the runner fills its idle slots (a planning pass for a queued S/M task with no scope, the one named gap
for any other skip), records each lane's phase times, and names the slowest phase in its summary.

  python3 plugin/tests/run.py test_run -k Starve      (T-6426: the planning pass, the named gaps, the batch's inheritance)
  python3 plugin/tests/run.py test_run -k Sentinel    (T-6434: the phase summary and the idle share)
"""
import json

from helpers import SampoTestCase

from sampo import cli
from sampo import core as c
from sampo import serve as sserve

SERVE_REPLY = json.dumps({"scope": ["src/app.py"], "verify": ["python3 -m unittest test_app"]})


class Starve(SampoTestCase):
    repo_seed = ("seed", (("src/app.py", "x = 1\n"), ("src/other.py", "y = 2\n")))

    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)

    def task(self, title, *extra):
        return self.fm_json("task", "new", title, "--type", "FIX", "--ac", "works", "--step", "fix it", *extra)["id"]

    def brief(self, tid):
        return c.find_brief(self.p, tid)

    def test_a_planned_task_with_no_scope_is_planned_on_a_free_slot(self):
        tid = self.task("tidy the app", "--tier", "S")
        calls = []

        def ask(prompt):
            calls.append(prompt)
            return SERVE_REPLY
        gap = sserve.plan_head(self.p, self.brief(tid), ask=ask)
        self.assertEqual(gap, "")  # planned: no gap named
        self.assertEqual(len(calls), 1)  # one cheap call per task
        self.assertEqual(self.brief(tid).meta.get("scope"), ["src/app.py"])
        self.assertEqual(self.brief(tid).meta.get("plan_verify"), ["python3 -m unittest test_app"])
        # the picker now takes it: a free slot gets it, so the pool starts it rather than skipping it
        self.assertEqual([x.id for x in sserve._batch(self.p, self.brief(tid), set(), 1, [])], [tid])

    def test_a_reply_that_names_no_tracked_file_names_the_gap_and_writes_nothing(self):
        tid = self.task("tidy the app", "--tier", "S")
        gap = sserve.plan_head(self.p, self.brief(tid), ask=lambda prompt: "I cannot tell.")
        self.assertIn("no scope", gap)
        self.assertFalse(self.brief(tid).meta.get("scope"))

    def test_a_reply_naming_an_untracked_path_is_no_scope(self):
        tid = self.task("tidy the app", "--tier", "S")
        reply = json.dumps({"scope": ["nope/missing.py"], "verify": []})
        self.assertIn("no scope", sserve.plan_head(self.p, self.brief(tid), ask=lambda prompt: reply))
        self.assertFalse(self.brief(tid).meta.get("scope"))

    def test_an_L_task_names_its_gap_without_a_call(self):
        tid = self.task("big rewrite", "--tier", "L")
        calls = []
        gap = sserve.plan_head(self.p, self.brief(tid), ask=lambda prompt: calls.append(prompt) or SERVE_REPLY)
        self.assertIn("L tier", gap)
        self.assertEqual(calls, [])

    def test_an_unplanned_task_names_its_gap_without_a_call(self):
        tid = self.task("captured only", "--tier", "S")
        b = self.brief(tid)
        b.meta["status"] = "captured"  # a capture the intake planner has not planned yet (status is meta's)
        c.save_brief(self.p, b)
        self.assertEqual(self.brief(tid).status, "captured")
        calls = []
        gap = sserve.plan_head(self.p, self.brief(tid), ask=lambda prompt: calls.append(prompt) or SERVE_REPLY)
        self.assertIn("unplanned", gap)
        self.assertIn("sampo task plan", gap)
        self.assertEqual(calls, [])

    def test_a_failed_model_call_names_the_gap(self):
        tid = self.task("tidy the app", "--tier", "S")

        def ask(prompt):
            raise OSError("claude is not installed")
        self.assertIn("no scope", sserve.plan_head(self.p, self.brief(tid), ask=ask))

    def test_a_batch_inherits_its_members_scopes_and_verify_commands(self):
        a = self.task("first", "--tier", "S", "--scope", "src/app.py")
        b = self.task("second", "--tier", "S", "--scope", "src/other.py")
        for tid, verify in ((a, "python3 -m unittest test_a"), (b, "python3 -m unittest test_b")):
            cli.mutate(self.p, tid, lambda x, v=verify: x.meta.__setitem__("plan_verify", [v]), None)
        host, _ids = cli._make_batch(self.p, [a, b], "Batch: first; second")
        h = self.brief(host.id)
        self.assertEqual(sorted(h.meta.get("scope")), ["src/app.py", "src/other.py"])
        self.assertEqual(sorted(h.meta.get("plan_verify")), ["python3 -m unittest test_a", "python3 -m unittest test_b"])


class Sentinel(SampoTestCase):
    def phases(self, wait=0, plan=0, build=0, review=0, land=0):
        return {"wait": wait, "plan": plan, "build": build, "review": review, "land": land}

    def test_the_summary_names_the_slowest_phase(self):
        got = sserve.phase_summary({"T-1": self.phases(wait=60, plan=30, build=1800, review=120, land=300),
                                    "T-2": self.phases(wait=0, plan=20, build=900, review=60, land=200)})
        self.assertIn("slowest: build", got)

    def test_a_seeded_slow_land_is_the_top_sink(self):
        got = sserve.phase_summary({"T-1": self.phases(wait=5, plan=5, build=60, review=10, land=3600)})
        self.assertIn("slowest: land", got)
        self.assertIn("land 60.0", got)  # lane-minutes per phase, one decimal

    def test_the_summary_says_how_much_slot_time_sat_idle(self):
        got = sserve.phase_summary({"T-1": self.phases(build=600)}, idle_s=600, slot_s=1200)
        self.assertIn("idle 50%", got)

    def test_no_lanes_no_summary(self):
        self.assertEqual(sserve.phase_summary({}), "")
