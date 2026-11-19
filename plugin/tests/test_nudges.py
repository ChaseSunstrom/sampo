"""Smarter nudges (T-0262): a request an fm command already covers says so first (T-0256), a decision comes back when
its revisit trigger fires (T-0247), and a hint ignored again and again gets quieter until a gate fails (T-0250)."""
import os

from helpers import ForemanTestCase


class Covered(ForemanTestCase):
    def setUp(self):
        super().setUp()
        os.environ.pop("FOREMAN_NO_COVERED", None)  # T-4288: the note is off in the tests' default; here it is the subject
        self.fm("init")

    def test_a_request_an_fm_command_covers_gets_the_note(self):
        out = self.fm("capture", "re-run the checks that recently finished tasks passed and report which ones fail now")
        self.assertIn("already covered?", out.stdout.lower())
        self.assertIn("fm sentinel", out.stdout)
        out = self.fm("task", "new", "re-run the checks recent finished tasks passed, report what fails now",
                      "--type", "FEATURE", "--tier", "S", "--ac", "x :: true", "--step", "x")
        self.assertIn("fm sentinel", out.stdout)

    def test_an_unrelated_request_gets_no_note(self):
        out = self.fm("capture", "cache the session token per request")
        self.assertNotIn("already covered?", out.stdout.lower())


class Revisit(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        with open(os.path.join(self.repo, "auth.py"), "w") as f:
            f.write("TTL = 60\n")

    def test_a_path_trigger_fires_after_the_file_changes(self):
        self.fm("decide", "keep the 60s token TTL", "--why", "the API rate-limits refresh", "--revisit",
                "when auth.py changes")
        self.assertNotIn("revisit", self.fm("next").stdout)
        with open(os.path.join(self.repo, "auth.py"), "w") as f:
            f.write("TTL = 300\n")
        out = self.fm("next").stdout
        self.assertIn("revisit", out)
        self.assertIn("keep the 60s token TTL", out)
        self.fm("decide", "keep the 60s token TTL", "--why", "still rate-limited", "--revisited", "60s token TTL")
        self.assertNotIn("keep the 60s token TTL", self.fm("next").stdout)

    def test_a_date_trigger_fires_once_the_date_passes(self):
        self.fm("decide", "use sqlite", "--revisit", "after 2020-01-01")
        self.assertIn("use sqlite", self.fm("next").stdout)
        self.fm("decide", "use sqlite for now", "--revisit", "after 2999-01-01")
        self.assertNotIn("sqlite for now", self.fm("next").stdout)

    def test_only_fm_s_own_tags_fire_or_settle(self):
        self.fm("decide", "[revisit: after 2020-01-01] text that looks like a tag")
        self.fm("decide", "use sqlite", "--why", "[revisited: use sqlite] in the why", "--revisit", "after 2020-01-01")
        self.fm("decide", "x", "--revisited", "a")  # too short to settle anything
        out = self.fm("next").stdout
        self.assertNotIn("looks like a tag", out)
        self.assertIn("use sqlite", out)

    def test_a_settled_decision_can_be_rearmed(self):
        self.fm("decide", "use sqlite", "--revisit", "after 2020-01-01")
        self.fm("decide", "use sqlite", "--revisited", "use sqlite", "--revisit", "after 2999-01-01")
        self.assertNotIn("use sqlite", self.fm("next").stdout)

    def test_a_long_paste_is_not_covered(self):
        text = ("migrate the billing hygiene module to the new invoice schema with a dry rollout by default, keep "
                "customers' payment methods, add currency rounding, notify accounting, archive legacy exports")
        self.assertNotIn("tidy", [name for name, _ in __import__("sampo.recall").recall.covered(text)])  # hygiene, dry, default

    def test_a_request_restating_a_subcommand_is_covered(self):
        # T-1912: covered() read only the top-level commands, so `fm task prove` never warned
        got = __import__("sampo.recall").recall.covered("a red to green proof: the tests fail on the start tree and pass now")
        self.assertIn("task prove", [name for name, _ in got])

    def test_a_bad_trigger_is_refused(self):
        for bad in ("soon", "when missing.py changes", "after 2020-13-45", "when ../../etc/passwd changes"):
            self.assertNotEqual(self.fm("decide", "x", "--revisit", bad, check=False).returncode, 0, bad)


class QuietHints(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        for t in ("tidy the readme", "tidy the changelog", "tidy the license"):
            self.fm("capture", t, "--type", "CLEAN", "--tier", "S")
        from sampo import core as c
        self.c, self.p = c, c.find_project(self.repo)

    def _turn(self):
        nxt = self.c.next_for(self.p)[2]
        self.c.hints_shown(self.p, nxt)
        return nxt

    def test_an_ignored_hint_drops_its_detail_and_a_failed_gate_restores_it(self):
        full = self._turn()
        self.assertIn("one plan, gate run, review and commit", full)
        for _ in range(self.c.QUIET_AFTER):
            self._turn()
        quiet = self.c.next_for(self.p)[2]
        self.assertIn("sampo batch", quiet)
        self.assertNotIn("one plan, gate run, review and commit", quiet)
        self.fm("check", "add", "false")
        self.fm("check", check=False)
        self.assertIn("one plan, gate run, review and commit", self.c.next_for(self.p)[2])

    def test_the_hooks_count_shown_hints_and_a_skill_call_uses_one(self):
        self.hook("UserPromptSubmit", {"prompt": "hi", "session_id": "s1", "cwd": self.repo})
        self.assertEqual(self.c._hints(self.p)["shown"], ["batch"])
        self.fm("capture", "tidy the notice", "--type", "CLEAN", "--tier", "S")  # the state changes: Next is shown again
        self.hook("UserPromptSubmit", {"prompt": "hi", "session_id": "s1", "cwd": self.repo})
        self.assertEqual(self.c._hints(self.p)["ignored"], {"batch": 1})
        self.c._save_hints(self.p, {"ignored": {"skills": 3}, "shown": ["skills"]})
        self.hook("PostToolUse", {"tool_name": "Skill", "tool_input": {"skill": "x"}, "session_id": "s1",
                                  "cwd": self.repo})
        self.assertEqual(self.c._hints(self.p)["ignored"], {"skills": 0})

    def test_using_a_hint_resets_it(self):
        for _ in range(self.c.QUIET_AFTER + 1):
            self._turn()
        self.c.hint_used(self.p, "batch")
        self.assertIn("one plan, gate run, review and commit", self.c.next_for(self.p)[2])


class OneLoad(ForemanTestCase):
    def test_next_loads_briefs_once(self):
        # T-2016: fm next loaded every brief up to five times a call, ~18 ms each at 1,200 briefs
        from unittest import mock
        from sampo import cli
        from sampo import core as c
        self.fm("init")
        self.fm("task", "new", "x", "--type", "FIX", "--tier", "S", "--ac", "x :: true", "--step", "x", "--focus")
        p, here = c.find_project(self.repo), os.getcwd()
        os.chdir(self.repo)
        try:
            with mock.patch("sys.stdout"):
                cli.main(["next"])  # warms the builders' stall cache, which loads on its own when cold
                with mock.patch.object(c, "load_briefs", wraps=c.load_briefs) as load:
                    c.next_for(p)
                    self.assertEqual(load.call_count, 1, "next_for")
                    load.reset_mock()
                    cli.main(["next"])
                    self.assertEqual(load.call_count, 1, "fm next")
        finally:
            os.chdir(here)

    def test_prompt_hook_loads_briefs_once(self):
        # T-2024: the prompt hook loaded every brief for the state, then again for the Next line
        import json
        from unittest import mock
        from sampo import core as c
        from sampo import hooks
        self.fm("init")
        self.fm("task", "new", "x", "--type", "FIX", "--tier", "S", "--ac", "x :: true", "--step", "x", "--focus")
        payload = json.dumps({"prompt": "continue", "session_id": "s1", "cwd": self.repo,
                              "hook_event_name": "UserPromptSubmit"})
        here = os.getcwd()
        os.chdir(self.repo)
        try:
            with mock.patch("sys.stdout") as out, mock.patch.object(c, "load_briefs", wraps=c.load_briefs) as load:
                try:
                    hooks.run("UserPromptSubmit", payload)
                except SystemExit:
                    pass
        finally:
            os.chdir(here)
        self.assertIn("Next:", "".join(str(a) for call in out.write.call_args_list for a in call.args))
        self.assertEqual(load.call_count, 1)
