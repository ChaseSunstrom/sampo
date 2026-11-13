"""T-4524: the drive never says 'the queue is empty ... say so and stop' while an open request names a plan document with
parts not yet built: its next action queues the next part. Under full autonomy the Stop hook refuses a final reply once
per part while one is left. A part is built when a done task names both the document and the part; the hook path reads the
briefs and the plan, never a model and never the concerns fold (steermeter.tick records the plan on the concern)."""
import json
import os

from test_concerns import stamp
from test_hooks import HookCase

from sampo import cli
from sampo import concerns as cn
from sampo import core as c

PLAN = "# Plan\n\n## Phase 0: scaffold\n\nText.\n\n## Phase 1: the store\n\nText.\n\n## Phase 2: the page\n\nText.\n"
TODAY = "say so and stop"
NEXT = "queue the next part of PLAN.md"


class PlanLeft(HookCase):
    def start(self, doc="PLAN.md", plan=PLAN, autonomy="full"):
        """An open concern whose user words name the plan document; the plan file written into the repo."""
        self.fm("init")
        self.fm("autonomy", autonomy)
        if plan is not None:
            with open(os.path.join(self.repo, doc), "w") as f:
                f.write(plan)
        p = self.project()
        row = {"session": "sess-1", "uuid": "u1", "at": stamp(hours=1), "model": "fake",
               "text": f"implement the plan in {doc}, all phases, under full autonomy", "kind": "steer",
               "avoidable": True, "root": "R7", "missed_thought": "finish the plan", "why": "it stopped",
               "concern": "implementing the plan", "surface": "the plan"}
        c.append_line(os.path.join(p.dir, "steers.jsonl"), json.dumps(row, ensure_ascii=False))
        cn.note_plans(p)  # what steermeter.tick does each round, model-free
        # the turn starts here, before any task is built: a done task counts as this turn's work only if it was
        # updated after the turn began (drive's `worked`), and a wall-clock margin of one second made it flaky
        self.hook("UserPromptSubmit", {"prompt": "carry on with the plan", "session_id": "sess-1"})

    def built(self, title):
        tid = self.task(title, focus=False)
        cli.mutate(self.project(), tid, lambda x: x.meta.update(status="done"), "test_done", {})
        return tid

    def empty_queue(self):
        tid = self.task("Current work")
        self.fm("task", "drop", tid, "done elsewhere", "--force")  # the active one: its drop is the drain

    def stop(self, msg="Working on it."):
        return self.hook("Stop", {"stop_hook_active": False, "last_assistant_message": msg,
                                  "session_id": "sess-1"}).stdout

    def block(self, msg):
        out = json.loads(self.stop(msg) or "{}")
        return out.get("decision"), out.get("reason", "")


class DrivePlanLeft(PlanLeft):
    def drive_out(self):
        """The offer gate refuses the first final reply once per part; the drive's own line is the second Stop's."""
        self.stop()
        return self.stop()

    def test_drive_plan_left_queues_the_next_part_of_the_plan(self):
        self.start()
        self.built("PLAN.md Phase 0: scaffold")
        self.empty_queue()
        out = self.drive_out()
        self.assertIn(NEXT, out)
        self.assertIn("Phase 1", out)  # the first unbuilt part, not the built Phase 0
        self.assertNotIn(TODAY, out)

    def test_drive_plan_left_all_phases_built_keeps_todays_line(self):
        self.start()
        for n in (0, 1, 2):
            self.built(f"PLAN.md Phase {n}: part")
        self.empty_queue()
        out = self.stop()  # no part left: the gate stays silent, so the drive's line is this Stop's
        self.assertNotIn(NEXT, out)
        self.assertIn(TODAY, out)

    def test_drive_plan_left_no_plan_file_keeps_todays_line(self):
        self.start(doc="NOPE.md", plan=None)
        self.built("PLAN.md Phase 0: scaffold")
        self.empty_queue()
        out = self.stop()
        self.assertNotIn(NEXT, out)
        self.assertIn(TODAY, out)

    def test_drive_plan_left_a_done_task_that_does_not_name_the_plan_builds_no_part(self):
        self.start()
        self.built("Phase 0 scaffold, no plan named")
        self.empty_queue()
        out = self.drive_out()
        self.assertIn(NEXT, out)
        self.assertIn("Phase 0", out)  # Phase 0 is not built: its task names no plan document


class OfferPlanLeft(PlanLeft):
    def test_a_final_reply_with_no_offer_wording_is_refused_while_a_part_is_left(self):
        self.start()
        self.built("PLAN.md Phase 0: scaffold")
        self.empty_queue()
        decision, reason = self.block("Done with Phase 0.")
        self.assertEqual(decision, "block")
        self.assertIn("nothing is queued for them", reason)
        self.assertIn("yours to start", reason)  # names the action, not just the refusal

    def test_offer_is_not_refused_under_standard_autonomy(self):
        self.start(autonomy="standard")
        self.built("PLAN.md Phase 0: scaffold")
        decision, reason = self.block("Phase 1 is ready. Say the word when you want it.")
        self.assertNotIn("parts left", reason)

    def test_offer_is_not_refused_when_no_part_is_left(self):
        self.start()
        for n in (0, 1, 2):
            self.built(f"PLAN.md Phase {n}: part")
        decision, reason = self.block("All phases are built.")
        self.assertNotIn("parts left", reason)

    def test_offer_of_the_same_part_is_not_refused_twice_whatever_the_reply_says(self):
        self.start()
        self.built("PLAN.md Phase 0: scaffold")
        self.assertEqual(self.block("Done with Phase 0.")[0], "block")
        reason = self.block("Something else entirely.")[1]  # keyed on the part, not the words
        self.assertNotIn("parts left", reason, "the same part is refused once per session")

    def test_offer_of_a_new_part_is_refused_once_more(self):
        self.start()
        self.built("PLAN.md Phase 0: scaffold")
        self.assertEqual(self.block("Done with Phase 0.")[0], "block")
        self.built("PLAN.md Phase 1: the store")
        decision, reason = self.block("Done with Phase 1.")
        self.assertEqual(decision, "block")
        self.assertIn("Phase 2", reason)  # the next part left, not the one already refused
