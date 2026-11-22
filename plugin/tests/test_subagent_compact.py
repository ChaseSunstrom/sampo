"""T-5685: a subagent's compaction gets one short line (its task, its lane and its next action), not the lead's project
block, which every builder and reviewer that compacts used to receive again (82 of 100 builders, 2026-11-18 to 11-21).
A subagent's compaction is told by its own payload (agent_id, or a transcript under subagents/) or by the marker its
PreCompact left, and a marker is taken only by the start whose transcript or cwd it names: a subagent shares its lead's
session_id, so the lead's own compaction must never take a builder's marker. A main-session compaction and a startup keep
the full block, and the ledger's session_start row on a compaction records the payload's key names."""
import json
import os

from helpers import ForemanTestCase

from sampo import core as c

AGENT = "a1b2c3d4e5f6"  # a subagent's agent id (the builder's worktree is agent-<id>)
OTHER_AGENT = "ffff0000aaaa"  # a second subagent of the same session
ONE_SHOT = "sess-1.note-t5685"  # a one-shot note of the session: the start clears it (the sid. prefix), a subagent's keeps it
SUB_T = "/projects/app/fake/subagents/agent-a1b2c3d4e5f6.jsonl"  # the builder's transcript
OTHER_T = "/projects/app/fake/subagents/agent-ffff0000aaaa.jsonl"  # the other builder's transcript
LEAD_T = "/projects/app/fake/lead.jsonl"  # the lead's transcript: the lead shares sess-1's session_id


class SubagentCompact(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.tid = self.fm_json("task", "new", "Fix login", "--type", "FIX", "--tier", "S", "--step", "reproduce")["id"]
        self.fm("task", "ac", self.tid, "add", "works", "--verify", "pytest")
        self.fm("focus", self.tid)
        self.env_file = os.path.join(self.tmp, "claude_env")
        open(self.env_file, "w").close()
        self.sessions = os.path.join(c.state_dir(), "sessions")
        os.makedirs(self.sessions, exist_ok=True)
        open(os.path.join(self.sessions, ONE_SHOT), "w").close()
        self.sub_cwd = os.path.join(self.tmp, "agent-" + AGENT)  # the builder's worktree: named by its marker only
        self.other_cwd = os.path.join(self.tmp, "agent-" + OTHER_AGENT)
        for d in (self.sub_cwd, self.other_cwd):  # a hook runs in its payload's cwd, which must exist
            os.makedirs(d, exist_ok=True)

    def start(self, source="compact", sid="sess-1", **extra):
        proc = self.hook("SessionStart", dict({"source": source, "session_id": sid}, **extra),
                         env={"CLAUDE_ENV_FILE": self.env_file})
        return (json.loads(proc.stdout.strip() or "{}").get("hookSpecificOutput") or {}).get("additionalContext", "")

    def pre(self, sid="sess-1", **extra):
        return self.hook("PreCompact", dict({"session_id": sid, "trigger": "auto"}, **extra))

    def sub_pre(self, agent=AGENT, cwd=None, transcript=SUB_T, sid="sess-1"):
        """A subagent's PreCompact: its agent id, its own worktree and its transcript, under the lead's session_id."""
        return self.pre(sid, agent_id=agent, cwd=self.sub_cwd if cwd is None else cwd, transcript_path=transcript)

    def markers(self, sid):
        try:
            return [n for n in os.listdir(self.sessions) if n.startswith(sid + ".subcompact-")]
        except OSError:
            return []

    def test_subagent_compact_gets_one_line(self):
        ctx = self.start(agent_id=AGENT, agent_type="sampo:fm-builder")
        self.assertTrue(ctx, "a subagent's compaction still starts knowing its task")
        self.assertNotIn("\n", ctx)
        self.assertLessEqual(len(ctx), 300, ctx)
        self.assertIn(self.tid, ctx)
        self.assertIn("Fix login", ctx)
        self.assertIn("lane main", ctx)
        self.assertIn("Next:", ctx)
        self.assertNotIn("Sampo project", ctx, "the lead's project block is not sent to a subagent")
        self.assertTrue(os.path.exists(os.path.join(self.sessions, ONE_SHOT)),
                        "a subagent's compaction leaves the session's one-shot state files alone")

    def test_main_session_compact_is_unchanged(self):
        ctx = self.start()
        self.assertTrue(ctx.startswith("Sampo project"), ctx[:80])
        self.assertIn("Active:", ctx)
        self.assertFalse(os.path.exists(os.path.join(self.sessions, ONE_SHOT)),
                         "a main compaction clears the one-shot files, as before")
        self.assertTrue(self.start("startup", sid="sess-2").startswith("Sampo project"))

    def test_precompact_marker_marks_a_subagent(self):
        self.sub_pre()
        self.assertEqual(len(self.markers("sess-1")), 1, "a subagent's PreCompact leaves a marker")
        self.pre(sid="sess-2")
        self.assertEqual(self.markers("sess-2"), [], "a main PreCompact leaves none")
        ctx = self.start(sid="sess-1", transcript_path=SUB_T)  # the marker names this transcript: the subagent's start
        self.assertEqual(self.markers("sess-1"), [], "the SessionStart the marker names consumes it")
        self.assertNotIn("Sampo project", ctx)
        self.assertLessEqual(len(ctx), 300)
        self.assertIn(self.tid, ctx)
        self.assertTrue(self.start(sid="sess-1").startswith("Sampo project"),
                        "the marker is spent: the next compaction of this session is the lead's, whole")

    def test_precompact_marker_refuses_an_agent_id_that_is_not_a_plain_name(self):  # the payload is untrusted text
        for bad in ("../../x", "a/b", "x y", "a" * 65):
            self.pre(agent_id=bad, cwd=self.sub_cwd, transcript_path=SUB_T)
        self.assertEqual(self.markers("sess-1"), [], "no marker, and no file outside the sessions folder")
        self.assertFalse(os.path.exists(os.path.join(self.sessions, "..", "x")))

    def test_compact_row_records_payload_keys(self):
        self.start(agent_id=AGENT, model="m")  # a subagent's compaction
        self.start(sid="sess-2")  # a main one
        p = c.find_project(self.repo)
        rows = [e["data"] for e in c.ledger_events(p, ["session_start"]) if e["data"].get("source") == "compact"]
        self.assertEqual(len(rows), 2, rows)
        sub, main = rows
        self.assertEqual(sub["keys"], sorted(sub["keys"]))
        for key in ("agent_id", "model", "source", "session_id", "hook_event_name"):
            self.assertIn(key, sub["keys"])
        self.assertNotIn("agent_id", main["keys"])
        self.assertNotIn(AGENT, json.dumps(sub), "the key names are recorded, never the values")

    def test_a_main_precompact_does_not_take_a_subagents_marker(self):  # the lead and its builder share sess-1
        self.sub_pre()
        self.pre(transcript_path=LEAD_T)  # the lead's PreCompact, during the same compaction
        ctx = self.start(sid="sess-1", transcript_path=LEAD_T)
        self.assertTrue(ctx.startswith("Sampo project"), ctx[:80])
        self.assertEqual(len(self.markers("sess-1")), 1, "the lead's start leaves the subagent's marker alone")
        self.assertTrue(self.start(sid="sess-1", transcript_path=SUB_T).startswith("Sampo, after compaction"))

    def test_the_overlap_order_gives_the_lead_the_full_block(self):
        self.pre(transcript_path=LEAD_T)  # the lead's PreCompact
        self.sub_pre()  # the builder's PreCompact, while the lead is summarising
        self.assertTrue(self.start(sid="sess-1", transcript_path=LEAD_T).startswith("Sampo project"),
                        "the lead's own compaction is whole, whatever its builder marked")
        self.assertEqual(len(self.markers("sess-1")), 1)
        ctx = self.start(sid="sess-1", transcript_path=SUB_T)
        self.assertTrue(ctx.startswith("Sampo, after compaction"), ctx[:80])
        self.assertEqual(self.markers("sess-1"), [])

    def test_a_transcript_under_subagents_is_a_subagent(self):
        ctx = self.start(transcript_path="/projects/app/fake/subagents/agent-x.jsonl")
        self.assertTrue(ctx.startswith("Sampo, after compaction"), ctx[:80])

    def test_agent_type_alone_is_not_a_subagent(self):  # a session started with --agent carries agent_type too
        self.assertTrue(self.start(agent_type="sampo:fm-builder").startswith("Sampo project"))

    def test_a_start_takes_only_the_marker_it_names(self):
        self.sub_pre(agent=AGENT, cwd=self.sub_cwd, transcript=SUB_T)
        self.sub_pre(agent=OTHER_AGENT, cwd=self.other_cwd, transcript=OTHER_T)
        self.assertTrue(self.start(sid="sess-1", transcript_path=SUB_T).startswith("Sampo, after compaction"))
        left = self.markers("sess-1")
        self.assertEqual(len(left), 1, left)
        self.assertIn(OTHER_AGENT, left[0], "the other builder's marker is not consumed")

    def test_two_markers_naming_the_same_start_are_ambiguous_and_give_the_full_block(self):
        self.sub_pre(agent=AGENT, cwd=self.repo, transcript=SUB_T)
        self.sub_pre(agent=OTHER_AGENT, cwd=self.repo, transcript=OTHER_T)
        self.assertTrue(self.start(sid="sess-1").startswith("Sampo project"), "neither marker is the one this start is")
        self.assertEqual(len(self.markers("sess-1")), 2, "an ambiguous start takes no marker")

    def test_a_start_without_a_transcript_is_matched_by_its_cwd(self):
        self.sub_pre(agent=AGENT, cwd=self.repo, transcript="")
        self.assertTrue(self.start(sid="sess-1").startswith("Sampo, after compaction"))
        self.assertEqual(self.markers("sess-1"), [])


if __name__ == "__main__":
    import unittest
    unittest.main()
