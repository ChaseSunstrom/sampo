"""T-4601: Sampo has two pauses, and `sampo pause off` lifted only one. The machine-wide flag (state/PANIC) stops every
project; a project's own flag (meta paused) is set by a user message that is only a pause word and was cleared only by a
message that is only "resume". The user typed `sampo pause off` twice, read "Pause lifted", and the state still said
PAUSED with the drive off. Now `sampo pause off` lifts the project flag in every project that has it and names them
(with -p, only that project's); bare `sampo pause` reports them; `sampo state` and the session start say which pause it
is. Two fixture projects, each test in its own FOREMAN_HOME; nothing here touches the real state."""
import json
import os
import unittest
from unittest import mock

from helpers import ForemanTestCase, git_repo

from sampo import core as c


class BothPauses(ForemanTestCase):
    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)  # the process environment only, never Sampo state
        for k in ("CLAUDE_CODE_SESSION_ID", "FOREMAN_SESSION_ID"):
            os.environ.pop(k, None)
        self.fm("init")
        self.other = git_repo(self.tmp, "other")
        self.fm("init", cwd=self.other)
        self.a = c.find_project(self.repo)
        self.b = c.find_project(self.other)

    def own_pause(self, repo):
        """The user's word: a message that is only "pause" (the prompt hook's PAUSE override)."""
        self.hook("UserPromptSubmit", {"prompt": "pause", "cwd": repo})

    def paused(self, p):
        return bool(c.read_meta(p).get("paused"))

    def both_paused(self):
        self.own_pause(self.repo)
        self.own_pause(self.other)
        self.assertTrue(self.paused(self.a) and self.paused(self.b), "the fixture: both projects hold their own pause")

    def test_the_user_s_word_sets_the_project_s_own_flag_not_the_machine_one(self):
        self.own_pause(self.repo)
        self.assertTrue(self.paused(self.a))
        self.assertFalse(self.paused(self.b))
        self.assertFalse(c.panicked())

    # ---- sampo pause off

    def test_pause_off_lifts_every_project_s_own_pause_and_names_them(self):
        self.both_paused()
        r = self.fm("pause", "off")
        self.assertFalse(self.paused(self.a))
        self.assertFalse(self.paused(self.b))
        self.assertIn(self.a.slug, r.stdout)
        self.assertIn(self.b.slug, r.stdout)
        self.assertIn("Pause lifted", r.stdout)

    def test_pause_off_lifts_the_machine_pause_and_the_project_pause_together(self):
        self.both_paused()
        self.fm("pause", "on")
        self.assertTrue(c.panicked())
        r = self.fm("pause", "off")
        self.assertFalse(c.panicked())
        self.assertFalse(self.paused(self.a) or self.paused(self.b))
        self.assertIn(self.a.slug, r.stdout)

    def test_pause_off_names_only_the_projects_that_had_one(self):
        self.own_pause(self.other)
        r = self.fm("pause", "off")
        self.assertIn(self.b.slug, r.stdout)
        self.assertNotIn(self.a.slug, r.stdout)

    def test_pause_off_with_nothing_paused_says_so_and_does_not_claim_a_lift(self):
        r = self.fm("pause", "off")
        self.assertIn("Nothing was paused", r.stdout)
        self.assertNotIn("Pause lifted", r.stdout)

    def test_pause_off_with_p_lifts_only_that_project_s_flag_and_leaves_the_machine_pause(self):
        self.both_paused()
        self.fm("pause", "on")
        r = self.fm("-p", self.b.slug, "pause", "off")
        self.assertFalse(self.paused(self.b))
        self.assertTrue(self.paused(self.a), "the other project keeps its own pause")
        self.assertTrue(c.panicked(), "-p names a project: the machine-wide pause stays, and the output says so")
        self.assertIn(self.b.slug, r.stdout)
        self.assertNotIn(self.a.slug, r.stdout)
        self.assertIn("still", r.stdout)

    def test_pause_off_json_lists_the_projects_it_lifted(self):
        self.both_paused()
        d = json.loads(self.fm("pause", "off", "--json").stdout)
        self.assertEqual(sorted(d["projects"]), sorted([self.a.slug, self.b.slug]))

    def test_a_second_pause_off_changes_nothing(self):
        self.both_paused()
        self.fm("pause", "off")
        r = self.fm("pause", "off")
        self.assertIn("Nothing was paused", r.stdout)

    def test_a_session_s_own_pause_off_leaves_the_user_s_project_pause(self):
        # the stamp the PreToolUse hook writes marks a session's own tool call; the user's word is not that session's to lift
        self.both_paused()
        cmd = "sampo pause on"
        self.hook("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": cmd}, "session_id": "sess-1"})
        self.fm("pause", "on", env={"CLAUDE_CODE_SESSION_ID": "sess-1"})
        self.assertEqual(c.pause_info()["by"], "agent")
        cmd = "sampo pause off"
        self.hook("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": cmd}, "session_id": "sess-1"})
        r = self.fm("pause", "off", env={"CLAUDE_CODE_SESSION_ID": "sess-1"})
        self.assertFalse(c.panicked())
        self.assertTrue(self.paused(self.a) and self.paused(self.b))
        self.assertIn("user's", r.stdout)

    def test_a_session_s_own_pause_off_with_p_leaves_that_project_s_pause_too(self):
        # the caller is told the same way with -p: a stamped call is the session's own, never the user's (no machine-wide
        # pause here: with one, the guard refuses the call before it runs)
        self.own_pause(self.other)
        cmd = f"sampo -p {self.b.slug} pause off"
        h = self.hook("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": cmd}, "session_id": "sess-1"})
        self.assertEqual(h.returncode, 2, "the guard refuses a session's own pause off of the user's project pause")
        c.stamp_pause("sess-1")  # and were a stamped call ever to reach the verb, it lifts nothing of the user's
        r = self.fm("-p", self.b.slug, "pause", "off", env={"CLAUDE_CODE_SESSION_ID": "sess-1"})
        self.assertTrue(self.paused(self.b), r.stdout)
        self.assertIn("user's", r.stdout)

    # ---- sampo pause, no argument

    def test_bare_pause_reports_the_projects_paused_on_their_own_and_how_to_lift_them(self):
        self.own_pause(self.other)
        r = self.fm("pause")
        self.assertIn(self.b.slug, r.stdout)
        self.assertNotIn(self.a.slug, r.stdout)
        self.assertNotIn("Not paused", r.stdout)
        self.assertIn("sampo pause off", r.stdout)
        self.assertTrue(self.paused(self.b), "a status check lifts nothing")
        self.assertFalse(c.panicked(), "and pauses nothing")

    def test_bare_pause_with_nothing_paused_still_says_not_paused(self):
        self.assertIn("Not paused", self.fm("pause").stdout)

    def test_bare_pause_json_has_the_projects(self):
        self.own_pause(self.repo)
        d = json.loads(self.fm("pause", "--json").stdout)
        self.assertEqual(d["projects"], [self.a.slug])
        self.assertFalse(d["paused"])

    # ---- the state line and the session start say which pause it is

    def notes(self, repo):
        text = self.fm("state", cwd=repo).stdout
        return [ln for ln in text.splitlines() if "Drive:" in ln][0]

    def test_state_says_the_pause_is_this_project_s(self):
        self.own_pause(self.repo)
        line = self.notes(self.repo)
        self.assertIn("PAUSED in this project", line)
        self.assertNotIn("everywhere", line)
        self.assertNotIn("PAUSED", self.notes(self.other))

    def test_state_says_the_pause_is_everywhere(self):
        self.fm("pause", "on")
        self.assertIn("PAUSED everywhere", self.notes(self.repo))

    def test_state_says_both_when_both_are_on(self):
        self.own_pause(self.repo)
        self.fm("pause", "on")
        line = self.notes(self.repo)
        self.assertIn("PAUSED everywhere", line)
        self.assertIn("this project", line)

    def test_the_session_start_line_says_the_pause_is_this_project_s(self):
        self.own_pause(self.repo)
        r = self.hook("SessionStart", {"source": "startup"}, env={"CLAUDE_ENV_FILE": os.path.join(self.tmp, "env")})
        ctx = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("paused in this project", ctx)


if __name__ == "__main__":
    unittest.main()
