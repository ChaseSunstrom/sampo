"""T-3539: with no active task (a session that keeps the main checkout unfocused for lane merges) a Stop while
background work runs waits instead of pushing the next task, and a drive block that only repeats the last one is said
once: 361 identical "Sampo drive: T-1331 …" blocks in one session, each a forced turn and a Stop hook error."""
from test_hooks import HookCase, parse


class DriveWait(HookCase):
    def stop(self, msg="Waiting on the builders."):
        return parse(self.hook("Stop", {"stop_hook_active": False, "last_assistant_message": msg,
                                        "session_id": "sess-1"})) or {}

    def test_no_active_task_and_a_running_subagent_never_blocks(self):
        self.fm("init")
        self.task("Survey the field", type_="RESEARCH", focus=False)  # no builder lane takes it
        self.hook("SubagentStart", {"agent_id": "a1", "agent_type": "sampo:fm-builder", "session_id": "sess-1"})
        outs = [self.stop() for _ in range(3)]
        self.assertFalse([o for o in outs if o.get("decision") == "block"], outs)
        self.assertIn("the drive waits for background work (a1", outs[0].get("systemMessage", ""))
        self.assertNotIn("waits for background work", outs[1].get("systemMessage", ""), "once per running set")
        self.hook("SubagentStop", {"agent_id": "a1", "agent_type": "sampo:fm-builder", "session_id": "sess-1"})
        self.assertEqual(self.stop("The builder returned.").get("decision"), "block", "nothing runs: the drive goes on")

    def test_no_active_task_offers_only_a_builder_lane_once(self):
        self.fm("init")
        small = self.task("Small fix", focus=False)
        self.hook("SubagentStart", {"agent_id": "a1", "agent_type": "sampo:fm-builder", "session_id": "sess-1"})
        first = self.stop()
        self.assertEqual(first.get("decision"), "block")
        self.assertIn(f"sampo lane brief {small}", first["reason"], "a lane starts without a focus here")
        self.assertNotIn("sampo focus", first["reason"])
        self.assertIsNone(self.stop().get("decision"), "offered once")

    def test_the_same_drive_block_twice_is_said_once(self):
        self.fm("init")
        tid = self.task()
        self.stop("Done for now.")  # the evidence gate's own hold, said once
        said = [self.stop("Done for now.").get("reason") for _ in range(4)]  # (a skill hint rides only the first)
        self.assertTrue(said[0].startswith("Sampo drive"), said)
        self.assertEqual(len([r for r in said if r]), len(set(filter(None, said))), "each text said once")
        self.assertIsNone(said[-1], "nothing changed the work: not repeated")
        self.fm("focus", tid)  # a focus changes the work
        self.assertIn("Sampo drive", self.stop("Done for now.").get("reason", ""))
        self.assertIsNone(self.stop("Done for now.").get("decision"))
        self.hook("UserPromptSubmit", {"prompt": "carry on with the login fix", "session_id": "sess-1"})
        self.assertIn("Sampo drive", self.stop("Done for now.").get("reason", ""), "a new turn of the user's: said again")
