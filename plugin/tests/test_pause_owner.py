"""T-4190: the pause knows who set it. A pause an agent's own tool call set (the PreToolUse hook stamps the session
just before `sampo pause` runs) reads by=agent and an agent may lift it; a pause with no fresh stamp (a typed `!`
command, a terminal, the pane) reads by=user, as does a flag an older tree wrote, and only the user lifts it. An
agent can make a pause stricter, never looser. Tests run in their own FOREMAN_HOME; none registers a cleanup that
writes Sampo state in-process (tearDown restores the real FOREMAN_HOME before cleanups run)."""
import calendar
import json
import os
import time
from unittest import mock

from helpers import ForemanTestCase
from test_guard import GuardCase, g

from sampo import core as c

ME = {"CLAUDE_CODE_SESSION_ID": "sess-1"}


class Owner(ForemanTestCase):
    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ)  # in-process reads (c.pause_line) see no session: "this session's"
        env.start()
        self.addCleanup(env.stop)  # the process environment only, never Sampo state
        for k in ("CLAUDE_CODE_SESSION_ID", "FOREMAN_SESSION_ID"):
            os.environ.pop(k, None)
        self.fm("init")

    def flag(self):
        return os.path.join(c.state_dir(), "PANIC")

    def tell(self, cmd, session="sess-1"):
        """The hook's answer to one Bash call: its stdout (a deny shows as "deny")."""
        return self.hook("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": cmd}, "session_id": session})

    def over(self, cmd, session="sess-1"):
        """PostToolUse: the call is over (its stamp, run or not, is spent)."""
        return self.hook("PostToolUse", {"tool_name": "Bash", "tool_input": {"command": cmd}, "session_id": session,
                                         "tool_response": {"stdout": "", "stderr": ""}})

    def agent_runs(self, *args, session="sess-1"):
        """`sampo pause …` the way a session runs it: PreToolUse sees the Bash command, sampo runs, PostToolUse follows."""
        cmd = "sampo pause " + " ".join(args)
        r = self.tell(cmd, session)
        self.assertNotIn('"deny"', r.stdout)
        done = self.fm("pause", *args, env={"CLAUDE_CODE_SESSION_ID": session})
        self.over(cmd, session)
        return done

    def user_types(self, *args):
        """`! sampo pause …`: Claude Code runs it without a tool call, so no hook sees it (the session env may be set)."""
        return self.fm("pause", *args, env=ME)

    # ---- agent: a pause an agent's tool call set is the agent's to lift

    def test_agent_pause_reads_by_agent_with_its_session_and_says_so(self):
        r = self.agent_runs("on")
        info = c.pause_info()
        self.assertEqual((info["by"], info["session"]), ("agent", "sess-1"))
        self.assertTrue(info["at"])
        self.assertIn("this session's command", r.stdout)
        self.assertIn("sampo pause off lifts it", r.stdout)
        self.assertTrue(c.panicked())
        self.assertFalse(c.panicked(hard=True))

    def test_agent_pause_now_reads_by_agent_and_stays_hard(self):
        self.agent_runs("on", "--now")
        self.assertEqual(c.pause_info()["by"], "agent")
        self.assertTrue(c.panicked(hard=True))

    def test_agent_can_lift_its_own_pause_and_the_ledger_says_who_paused_and_who_lifted(self):
        self.agent_runs("on")
        r = self.tell("sampo pause off")
        self.assertNotIn('"deny"', r.stdout)
        self.assertEqual(r.returncode, 0)
        self.agent_runs("off")
        self.assertFalse(c.panicked())
        evs = [e for e in c.ledger_events(c.find_project(self.repo)) if e["event"] in ("pause_on", "pause_off")]
        self.assertEqual([(e["event"], e["data"]["by"], e["data"].get("session")) for e in evs],
                         [("pause_on", "agent", "sess-1"), ("pause_off", "agent", "sess-1")])
        with open(os.path.join(c.state_dir(), "pauses.jsonl"), encoding="utf-8") as f:
            rows = [json.loads(line) for line in f]
        self.assertEqual([(x["event"], x["by"]) for x in rows], [("pause_on", "agent"), ("pause_off", "agent")])

    def test_agent_pause_is_named_where_the_pause_is_shown(self):
        self.agent_runs("on")
        line = c.pause_line()
        self.assertIn("paused by this session's command at ", line)
        self.assertIn("(sampo pause off lifts it)", line)
        sd = c.state_dict(c.find_project(self.repo))
        self.assertEqual(sd["panic_by"], "agent")
        self.assertIn("by this session's command", c.render_state(sd))
        from sampo import doctor
        r = doctor.check_pause()
        self.assertEqual(r.status, "WARN")
        self.assertIn("by this session's command", r.detail)
        self.assertIn("paused by this session's command", json.loads(self.fm("wiring", "--json").stdout)["paused_by"])
        self.assertIn("by this session's command", self.fm("status").stdout)
        note = self.hook("SessionStart", {"source": "startup"}).stdout
        self.assertIn("paused by this session's command", note)

    def test_agent_stamp_is_consumed_and_one_sessions_stamp_is_not_anothers(self):
        self.tell("sampo pause on", session="sess-1")
        sess2 = {"CLAUDE_CODE_SESSION_ID": "sess-2"}
        self.fm("pause", "on", env=sess2)  # sess-2 never ran it through the hook
        self.assertEqual(c.pause_info()["by"], "user")
        self.fm("pause", "off", env=sess2)
        self.fm("pause", "on", env=ME)  # sess-1's stamp was not spent by sess-2's write
        self.assertEqual(c.pause_info()["by"], "agent")
        self.agent_runs("off")
        self.fm("pause", "on", env=ME)  # now spent: no stamp, a user pause
        self.assertEqual(c.pause_info()["by"], "user")

    def test_agent_stamp_older_than_a_minute_is_not_a_stamp(self):
        self.tell("sampo pause on")
        path = c.pause_stamp_path()
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        d["sess-1"] = time.time() - 120
        with open(path, "w", encoding="utf-8") as f:
            json.dump(d, f)
        self.fm("pause", "on", env=ME)
        self.assertEqual(c.pause_info()["by"], "user")

    def test_agent_stamp_is_written_only_for_a_command_that_names_pause(self):
        self.tell("ls -la")
        self.tell("sampo status")
        self.assertFalse(os.path.exists(c.pause_stamp_path()))
        self.tell("sampo pause on")
        self.assertTrue(os.path.exists(c.pause_stamp_path()))

    def test_agent_pause_that_lingers_is_told_the_users_only_named_after_an_hour(self):
        from sampo import selfcheck
        p = c.find_project(self.repo)
        self.agent_runs("on")
        at = c.pause_info()["at"]
        t0 = calendar.timegm(time.strptime(at, "%Y-%m-%dT%H:%M:%SZ"))
        self.assertIsNone(selfcheck.pause_lingers(p, [], t0 + 5 * 60))
        hit = selfcheck.pause_lingers(p, [], t0 + 15 * 60)
        self.assertTrue(hit.tell)
        self.assertIn("by this session's command", hit)
        self.fm("pause", "off", env=ME)
        self.user_types("on")
        self.assertIsNone(selfcheck.pause_lingers(p, [], t0 + 30 * 60))
        hit = selfcheck.pause_lingers(p, [], t0 + 90 * 60)
        self.assertIn("by the user", hit)
        self.assertFalse(getattr(hit, "tell", False))

    # ---- user: a pause nobody's tool call set is the user's, as before

    def test_user_pause_is_refused_to_an_agent_as_before(self):
        self.user_types("on")
        info = c.pause_info()
        self.assertEqual(info["by"], "user")
        r = self.tell("sampo pause off")
        self.assertIn('"deny"', r.stdout)
        self.assertIn("sampo pause off is the user's to run", r.stdout)
        self.assertTrue(c.panicked())

    def test_user_pause_names_the_user_and_says_only_they_lift_it(self):
        self.user_types("on")
        line = c.pause_line()
        self.assertIn("paused by the user at ", line)
        self.assertIn("only they lift it", line)

    def test_user_flag_an_older_tree_wrote_reads_as_the_users_and_is_refused(self):
        for text in ("2026-11-09T00:00:00Z\n", "2026-11-09T00:00:00Z hard\n"):
            with open(self.flag(), "w", encoding="utf-8") as f:
                f.write(text)
            self.assertEqual(c.pause_info()["by"], "user", text)
            self.assertTrue(c.panicked())
            self.assertEqual(c.panicked(hard=True), text.endswith("hard\n"))
            self.assertIn('"deny"', self.tell("sampo pause off").stdout, text)
            self.assertIn("by the user", c.pause_line())

    def test_user_flag_unreadable_or_forged_fails_closed(self):
        for text in ("2026-11-09T00:00:00Z\n{not json\n", "2026-11-09T00:00:00Z\n[1, 2]\n",
                     '2026-11-09T00:00:00Z\n{"by": "root"}\n', '2026-11-09T00:00:00Z\n{"by": ["agent"]}\n', "\x00\xff\n"):
            with open(self.flag(), "w", encoding="utf-8", errors="surrogateescape") as f:
                f.write(text)
            self.assertEqual(c.pause_info()["by"], "user", repr(text))
            self.assertIn('"deny"', self.tell("sampo pause off").stdout, repr(text))

    def test_user_for_user_flag_marks_an_agents_pause_theirs(self):
        self.tell("sampo pause on --for-user")
        self.fm("pause", "on", "--for-user", env=ME)  # the session carries out the user's spoken "pause everything"
        self.assertEqual(c.pause_info()["by"], "user")
        self.assertIn('"deny"', self.tell("sampo pause off").stdout)

    def test_user_who_types_pause_over_an_agents_pause_makes_it_theirs(self):
        self.agent_runs("on")
        self.user_types("on")
        self.assertEqual(c.pause_info()["by"], "user")
        self.assertIn('"deny"', self.tell("sampo pause off").stdout)

    # ---- downgrade: an agent can make a pause stricter, never looser

    def test_downgrade_agent_pause_on_over_a_users_pause_leaves_it_the_users(self):
        self.user_types("on")
        before = c.pause_info()
        self.agent_runs("on")
        after = c.pause_info()
        self.assertEqual(after["by"], "user")
        self.assertEqual((after.get("session"), after["at"]), (before.get("session"), before["at"]))
        self.assertIn('"deny"', self.tell("sampo pause off").stdout)

    def test_downgrade_agent_now_over_a_users_pause_keeps_by_user_and_goes_hard(self):
        self.user_types("on")
        self.agent_runs("on", "--now")
        self.assertEqual(c.pause_info()["by"], "user")
        self.assertTrue(c.panicked(hard=True))
        self.assertIn('"deny"', self.tell("sampo pause off").stdout)

    def test_downgrade_a_stamp_a_call_left_over_never_turns_a_users_pause_into_the_agents(self):
        self.tell("sampo pause --help")  # names pause, stamps, never writes the flag
        self.over("sampo pause --help")  # the call ends: its stamp is spent
        self.user_types("on")
        self.assertEqual(c.pause_info()["by"], "user")
        self.assertIn('"deny"', self.tell("sampo pause off").stdout)

    def test_downgrade_a_stamped_pause_off_over_a_flag_that_turned_the_users_is_refused(self):
        self.agent_runs("on")  # the guard saw an agent's flag and let `pause off` by; before it ran the user typed theirs
        self.user_types("on")
        c.stamp_pause("sess-1")  # the hook stamped that `pause off` before the user's write (set up by hand: it now denies)
        r = self.fm("pause", "off", env=ME, check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("the user's to run", r.stderr)
        self.assertTrue(c.panicked())

    def test_downgrade_agent_off_over_a_users_pause_changes_nothing(self):
        self.user_types("on")
        self.assertIn('"deny"', self.tell("sampo pause off").stdout)
        self.assertEqual(c.pause_info()["by"], "user")
        self.assertTrue(c.panicked())

    # ---- T-4132 meets T-4190: a bare `sampo pause` only reports, and says who; the PAUSED text fits both owners

    def test_bare_pause_reports_who_set_it_and_changes_nothing(self):
        self.assertIn("Not paused", self.fm("pause").stdout)
        self.user_types("on")
        out = self.fm("pause").stdout
        self.assertIn("Paused by the user", out)
        self.assertIn("only they lift it", out)
        self.assertEqual(c.pause_info()["by"], "user")
        self.fm("pause", "off", env=ME)
        self.agent_runs("on")
        out = self.fm("pause").stdout
        self.assertIn("Paused by this session's command", out)
        self.assertEqual(c.pause_info()["by"], "agent")  # reporting rewrote nothing

    def test_the_paused_text_is_right_for_both_owners(self):
        text = c.PAUSED
        self.assertNotIn("until the user runs", text)  # an agent's own pause is lifted by that session
        self.assertIn("a session's own pause: that session may", text)
        self.assertIn("the user's: only they do", text)
        self.assertTrue(text.startswith("paused (sampo pause): nothing unattended starts"))  # callers read the lead


class GuardReadsTheFlag(GuardCase):
    """The guard reads the flag file itself (no import of the CLI) and fails closed."""
    def flag(self, text=None):
        path = os.path.join(self.fhome, "state", "PANIC")
        if text is None:
            if os.path.exists(path):
                os.remove(path)
        else:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        return path

    def run_off(self, text):
        self.flag(text)
        try:
            return self.bash("sampo pause off")
        finally:
            self.flag(None)

    def test_agent_flag_lets_pause_off_through_and_nothing_else_user_only(self):
        agent = '2026-11-11T00:00:00Z\n{"by": "agent", "session": "s", "at": "2026-11-11T00:00:00Z"}\n'
        self.assertIsNone(self.run_off(agent))
        self.assertIsNone(self.run_off(agent.replace("00:00Z\n{", "00:00Z hard\n{", 1)))
        self.flag(agent)
        try:
            self.assertBlocked(self.bash("sampo posture lite"), "self-authorize")  # only pause is keyed on the flag
        finally:
            self.flag(None)

    def test_user_old_missing_and_malformed_flags_are_refused(self):
        for text in (None, "2026-11-11T00:00:00Z\n", '2026-11-11T00:00:00Z\n{"by": "user"}\n',
                     "2026-11-11T00:00:00Z\n{broken\n", '2026-11-11T00:00:00Z\n{"by": "Agent"}\n'):
            self.assertBlocked(self.run_off(text), "self-authorize", repr(text))

    def test_user_flag_in_a_directory_or_unreadable_fails_closed(self):
        path = os.path.join(self.fhome, "state", "PANIC")
        os.makedirs(path)
        try:
            self.assertBlocked(self.bash("sampo pause off"), "self-authorize")
        finally:
            os.rmdir(path)
