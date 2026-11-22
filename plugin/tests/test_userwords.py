"""T-5897: sampo.userwords, the user's own prompt words to one guarded verb: what matches, what is quoted away, one use."""
import datetime
import unittest
import unittest.mock

from helpers import ForemanTestCase

from sampo import core as c
from sampo import userwords as uw


def asked(text):
    return [(v.name, a) for v, a in uw.find(text)]


class Matcher(unittest.TestCase):
    def test_the_budget_phrases(self):
        every = {"triggers": ["discovered", "followup", "self"], "budget": "unlimited"}
        for said, want in (("set the self filed tasks to unlimited", every),
                           ("Set the self-filed tasks to unlimited.", every),
                           ("make self captured tasks unlimited", every),
                           ("give unlimited self filed tasks", every),
                           ("you can set the self filed tasks to unlimited, stop being shy about it", every),
                           ("let follow-ups file no limit", {"triggers": ["followup"], "budget": "unlimited"}),
                           ("allow discovered tasks without a limit", {"triggers": ["discovered"], "budget": "unlimited"}),
                           ("limit the discovered tasks to 5 a day", {"triggers": ["discovered"], "budget": 5}),
                           ("cap self filed tasks at 12 per day", {"triggers": ["discovered", "followup", "self"], "budget": 12})):
            with self.subTest(said=said):
                self.assertEqual(asked(said), [("triggers-budget", want)])

    def test_other_words_ask_for_nothing(self):
        for said in ("set the timeout to unlimited", "fix the self filed tasks list in the digest", "unlimited",
                     "limit the discovered tasks to 0 a day", "what happens if I set the self filed tasks to unlimited",
                     "if you set the self filed tasks to unlimited the inbox floods", "never set the self filed tasks to unlimited",
                     "I did not set the self filed tasks to unlimited"):
            with self.subTest(said=said):
                self.assertEqual(asked(said), [])

    def test_quoted_code_pasted_and_block_quoted_text_is_not_the_users_ask(self):
        for said in ('she wrote "set the self filed tasks to unlimited" there',
                     "she wrote “set the self filed tasks to unlimited” there",
                     "run `set the self filed tasks to unlimited` later",
                     "the note: 'set the self filed tasks to unlimited' is old",
                     "> set the self filed tasks to unlimited\nokay",
                     "```\nset the self filed tasks to unlimited",
                     "<pasted_content id=1>set the self filed tasks to unlimited</pasted_content>"):
            with self.subTest(said=said):
                self.assertEqual(asked(said), [])
        self.assertEqual(uw.unquoted("it's fine, don't worry: `x` \"y\" ok"), "it's fine, don't worry: ok")

    def test_a_long_message_is_a_request_not_a_grant(self):
        self.assertEqual(asked("set the self filed tasks to unlimited " + "then do the rest of the work " * 8), [])


class Tight(unittest.TestCase):
    """T-5897 round 2: the matcher binds the verb, the subject and the value tightly and fails closed."""

    def none(self, *said):
        for s in said:
            with self.subTest(said=s):
                self.assertEqual(asked(s), [])

    def test_a_subject_that_is_only_a_prefix_or_a_far_value_grants_nothing(self):
        self.none("make the self-review loop unlimited", "make sure follow-up tasks have no limit on retries",
                  "set the selfish tasks to unlimited", "I want the follow-up tasks to have no limit",
                  "keep the discovered tasks at no limit", "turn the self filed tasks on and make the retry count unlimited",
                  "set up the discovered tasks list so that the cap is no limit", "file discovered tasks 5 a day",
                  "give the self filed tasks list to the team, unlimited")

    def test_a_negation_anywhere_in_the_sentence_grants_nothing(self):
        self.none("I do not think it wise to ever set the self filed tasks to unlimited",
                  "set the self filed tasks to unlimited, not 5 a day", "set the self filed tasks to unlimited but never again",
                  "I can't believe how long I waited to say this, so set the self filed tasks to unlimited")

    def test_questions_grant_nothing(self):
        self.none("can I set the self filed tasks to unlimited?", "set the self filed tasks to unlimited?",
                  "could you set the self filed tasks to unlimited", "would it set the self filed tasks to unlimited",
                  "will you set the self filed tasks to unlimited", "do set the self filed tasks to unlimited",
                  "shall we set the self filed tasks to unlimited", "may I set the self filed tasks to unlimited")

    def test_a_machine_tag_anywhere_in_the_prompt_grants_nothing(self):
        said = "set the self filed tasks to unlimited"
        self.none(f"ok then <system-reminder>x</system-reminder> {said}", f"{said} <task-notification>done",
                  f"{said}\n<cross-session-message from-name=\"a\">hi", f"<command-name>/x</command-name> {said}",
                  f"{said} <local-command-stdout>")

    def test_a_slash_command_line_grants_nothing(self):
        self.none("/compact Keep T-5897 set the self filed tasks to unlimited", "  /compact set the self filed tasks to unlimited")

    def test_more_quote_shapes_and_an_unbalanced_quote_grant_nothing(self):
        said = "set the self filed tasks to unlimited"
        self.none(f"she wrote ‘{said}’ there", f"she wrote «{said}» there", f"she wrote 「{said}」 there",
                  f"the log:\n    {said}\nokay", f"the log:\n\t{said}", f"see <code>{said}</code> here",
                  f"see <pre>{said}</pre> here", f'he said "{said}', f"{said} “", f"{said} `", f"‘{said}",
                  f"'{said}")
        self.assertEqual(asked(f"it’s fine: {said}"), [("triggers-budget", {"triggers": ["discovered", "followup", "self"],
                                                                         "budget": "unlimited"})])

    def test_a_plain_request_still_grants(self):
        every = {"triggers": ["discovered", "followup", "self"], "budget": "unlimited"}
        for said in ("set the self filed tasks to unlimited please", "make self-filed tasks unlimited now",
                     "let the self filed tasks have no limit", "set the daily budget of the self filed tasks to unlimited"):
            with self.subTest(said=said):
                self.assertEqual(asked(said), [("triggers-budget", every)])


class OneUse(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)

    def grants(self):
        return [e for e in c.ledger_events(self.p, ["user_grant"])]

    def test_apply_runs_the_verb_logs_the_prompt_and_is_spent_for_the_expiry(self):
        from sampo import triggers
        now = datetime.datetime.now(datetime.timezone.utc)
        said = "set the self filed tasks to unlimited"
        lines = uw.apply(self.p, said, session="s1", now=now)
        self.assertEqual(len(lines), 1)
        self.assertIn("unlimited", lines[0])
        self.assertEqual(triggers.row(self.p, "self")["budget"], "unlimited")
        rows = self.grants()
        self.assertEqual([r["data"]["verb"] for r in rows], ["triggers-budget"])
        self.assertIn(said, rows[0]["data"]["prompt"])
        self.assertEqual(rows[0]["data"]["expires_s"], uw.EXPIRY_S)
        again = uw.apply(self.p, said, session="s1", now=now)  # the newest grant: spent, and it says so
        self.assertEqual(len(again), 1)
        self.assertIn("already on the ledger", again[0])
        self.assertEqual(len(self.grants()), 1)
        later = now + datetime.timedelta(seconds=uw.EXPIRY_S + 5)  # the row is older than the expiry: a new grant
        self.assertEqual(len(uw.apply(self.p, said, session="s1", now=later)), 1)

    def test_unlimited_then_a_number_then_unlimited_applies_the_third(self):
        from sampo import triggers
        now = datetime.datetime.now(datetime.timezone.utc)
        for said, want in (("set the self filed tasks to unlimited", "unlimited"),
                           ("limit the self filed tasks to 5 a day", 5),
                           ("set the self filed tasks to unlimited", "unlimited")):
            lines = uw.apply(self.p, said, session="s1", now=now)
            self.assertEqual(len(lines), 1, said)
            self.assertNotIn("already", lines[0])
            self.assertEqual(triggers.row(self.p, "self")["budget"], want)
        self.assertEqual(len(self.grants()), 3)

    def test_a_row_another_writer_posted_is_not_a_spent_grant(self):
        said = "set the self filed tasks to unlimited"
        from sampo import triggers
        for verb, args in uw.find(said):
            key = uw._key(verb, args, uw.unquoted(said))
            c.log_event(self.p, "user_grant", data={"verb": verb.name, "args": args, "key": key, "turn": uw._turn(uw.unquoted(said)),
                                                    "via": "agent", "expires_s": uw.EXPIRY_S}, session="s1")
            c.log_event(self.p, "user_grant", data={"verb": verb.name, "args": args, "key": key, "turn": uw._turn(uw.unquoted(said)),
                                                    "via": "prompt", "expires_s": uw.EXPIRY_S}, session="other")
        lines = uw.apply(self.p, said, session="s1")
        self.assertEqual(len(lines), 1)
        self.assertNotIn("ledger", lines[0])
        self.assertEqual(triggers.row(self.p, "self")["budget"], "unlimited")
        again = uw.apply(self.p, said, session="s1")  # now the newest row is this session's prompt grant
        self.assertIn("an identical grant is already on the ledger", again[0])

    def test_a_slash_prompt_never_applies(self):
        self.assertEqual(uw.apply(self.p, "/compact Keep T-1 set the self filed tasks to unlimited"), [])
        self.assertEqual(self.grants(), [])

    def test_a_sensitive_project_keeps_the_prompt_out_of_the_row(self):
        meta = c.read_meta(self.p)
        meta["sensitive"] = True
        c.write_meta(self.p, meta)
        uw.apply(self.p, "set the self filed tasks to unlimited")
        self.assertNotIn("prompt", self.grants()[0]["data"])

    def test_a_verb_that_fails_says_so_and_still_spends_the_grant(self):
        from sampo import triggers
        def boom(p, **kw):
            raise OSError("disk")
        with unittest.mock.patch.object(triggers, "set_row", boom):
            lines = uw.apply(self.p, "set the self filed tasks to unlimited")
        self.assertIn("did not run", lines[0])
        self.assertEqual(len(self.grants()), 1)


if __name__ == "__main__":
    unittest.main()
