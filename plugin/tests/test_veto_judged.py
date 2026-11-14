"""T-4577: a stop command for now is no standing veto, a real prohibition keeps at least two key words, a later message
that resumes the work lifts what the judge says it lifts, and a veto with one key word never matches by words alone.
The judge is scripted here: a function (system, user) -> reply text, as hooks.veto_ask hands the real one (in hooks, not core: the evidence core never imports the model layer)."""
import json
import os
import sys
from unittest import mock

from helpers import ForemanTestCase

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
from sampo import core, hooks  # noqa: E402


def judge(standing=None, prohibits="", lifts=(), calls=None):
    def ask(system, user):
        if calls is not None:
            calls.append(user)
        return json.dumps({"standing": standing, "prohibits": prohibits, "lifts": list(lifts)})
    return ask


class Base(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = core.find_project(self.repo)

    def with_judge(self, ask):
        m = mock.patch.object(hooks, "veto_ask", lambda p: ask)
        m.start()
        self.addCleanup(m.stop)

    def words(self):
        return [v["words"] for v in core.vetoes(self.p)]


class Stored(Base):
    def test_a_command_for_now_is_acted_on_and_not_stored(self):
        self.with_judge(judge(standing=False))
        notes = hooks._veto_turn(self.p, "stop the exam run right away", True)
        self.assertEqual(core.vetoes(self.p), [])
        self.assertEqual(notes, [])

    def test_a_real_prohibition_is_stored_with_at_least_two_key_words(self):
        self.with_judge(judge(standing=True, prohibits="push to main"))
        hooks._veto_turn(self.p, "never push to main", True)
        self.assertEqual(self.words(), [["push", "main"]])

    def test_the_judges_phrase_gives_the_words_when_the_message_hides_them(self):
        self.with_judge(judge(standing=True, prohibits="delete release branches"))
        hooks._veto_turn(self.p, "don't do that again", True)
        self.assertEqual(self.words(), [["delete", "release", "branches"]])

    def test_a_judge_that_sees_no_prohibition_stores_nothing(self):
        self.with_judge(judge(standing=None))
        hooks._veto_turn(self.p, "don't you think the plan is odd", True)
        self.assertEqual(core.vetoes(self.p), [])

    def test_a_one_word_veto_is_kept_and_said_to_be_context_only(self):
        self.with_judge(judge(standing=True, prohibits="mocks"))
        notes = hooks._veto_turn(self.p, "never mocks", True)
        self.assertEqual(self.words(), [["mocks"]])
        self.assertTrue(any("one key word" in n for n in notes), notes)


class Offline(Base):
    def setUp(self):
        super().setUp()
        m = mock.patch.object(hooks, "veto_ask", lambda p: None)  # no model reachable
        m.start()
        self.addCleanup(m.stop)

    def test_a_one_clause_stop_command_with_one_key_word_is_not_stored(self):
        self.assertIsNone(core.add_veto(self.p, "stop work"))
        self.assertEqual(core.vetoes(self.p), [])

    def test_real_prohibitions_still_store(self):
        for said in ("never push to main", "stop deleting tests"):
            self.assertIsNotNone(core.add_veto(self.p, said), said)
        self.assertEqual(self.words(), [["push", "main"], ["deleting", "tests"]])

    def test_the_prompt_hook_runs_without_a_model(self):
        self.hook("UserPromptSubmit", {"prompt": "stop work"})
        self.hook("UserPromptSubmit", {"prompt": "never push to main"})
        self.assertEqual(self.words(), [["push", "main"]])


class Lifted(Base):
    def setUp(self):
        super().setUp()
        core.add_veto(self.p, "never push to main")
        core.add_veto(self.p, "don't release on Fridays")

    def test_a_resume_message_lifts_what_the_judge_says_and_the_judge_sees_the_stored_ones(self):
        calls = []
        self.with_judge(judge(lifts=[2], calls=calls))
        notes = hooks._veto_turn(self.p, "carry on with the release work", False)
        self.assertEqual(self.words(), [["push", "main"]])
        self.assertEqual(len(calls), 1, "one call covers every stored veto")
        self.assertIn("push to main", calls[0])
        self.assertIn("release on Fridays", calls[0])
        self.assertTrue(any("lifted" in n and "Fridays" in n for n in notes), notes)

    def test_numbers_that_name_no_veto_lift_nothing(self):
        self.with_judge(judge(lifts=[0, 3, 99, "x"]))
        hooks._veto_turn(self.p, "carry on", False)
        self.assertEqual(len(core.vetoes(self.p)), 2)

    def test_a_judge_that_fails_lifts_nothing(self):
        def boom(system, user):
            raise OSError("down")
        self.with_judge(boom)
        hooks._veto_turn(self.p, "carry on", False)
        self.assertEqual(len(core.vetoes(self.p)), 2)
        self.with_judge(lambda s, u: "not json at all")
        hooks._veto_turn(self.p, "carry on", False)
        self.assertEqual(len(core.vetoes(self.p)), 2)

    def test_no_model_lifts_nothing(self):
        m = mock.patch.object(hooks, "veto_ask", lambda p: None)
        m.start()
        self.addCleanup(m.stop)
        self.assertEqual(hooks._veto_turn(self.p, "carry on", False), [])
        self.assertEqual(len(core.vetoes(self.p)), 2)

    def test_the_message_that_states_a_veto_does_not_lift_it(self):
        self.with_judge(judge(standing=True, prohibits="touch the lockfile", lifts=[1, 2, 3]))
        hooks._veto_turn(self.p, "never touch the lockfile", True)
        self.assertEqual(self.words()[-1], ["touch", "lockfile"], "the new veto is not in the judge's list to lift")


class Calls(Base):
    def test_no_call_without_a_veto_pattern_or_a_stored_veto(self):
        calls = []
        self.with_judge(judge(calls=calls))
        hooks._veto_turn(self.p, "please add a button to the page", False)
        hooks._veto_turn(self.p, "no, that is wrong", True)  # a correction with no never/don't/stop clause
        self.assertEqual(calls, [])

    def test_a_stored_veto_or_a_fired_pattern_asks_once(self):
        calls = []
        self.with_judge(judge(standing=False, calls=calls))
        hooks._veto_turn(self.p, "stop the run", True)
        self.assertEqual(len(calls), 1)
        core.add_veto(self.p, "never push to main")
        hooks._veto_turn(self.p, "go on", False)
        self.assertEqual(len(calls), 2)

    def test_a_down_model_is_not_probed_again_within_minutes(self):
        from sampo import moonscale
        with mock.patch.object(moonscale, "_local", return_value=None) as probe:
            self.assertIsNone(hooks.veto_ask(self.p))
            self.assertIsNone(hooks.veto_ask(self.p))
        self.assertEqual(probe.call_count, 1)


class Recoverable(Base):
    """A lift moves the veto to a lifted list, visible and restorable: a small model misreading a plain "continue"
    must never silently delete a standing rule."""
    KEY = "sk-ant-api03-" + "Q7f2Lm9xWv4Rt8Yp3Kd6Hs1Jc5Nb0Ga"  # key-shaped, made up

    def setUp(self):
        super().setUp()
        core.add_veto(self.p, "never push to main")
        core.add_veto(self.p, "don't release on Fridays")

    def test_a_reply_listing_every_rule_for_a_plain_continue_moves_them_to_lifted(self):
        self.with_judge(judge(lifts=[1, 2]))
        notes = hooks._veto_turn(self.p, "continue", False)
        self.assertEqual(core.vetoes(self.p), [])
        gone = core.lifted(self.p)
        self.assertEqual([g["words"] for g in gone], [["push", "main"], ["release", "fridays"]])
        for g in gone:
            self.assertEqual(g["by"], "continue")
            self.assertTrue(g["lifted_at"])
        self.assertEqual(len(notes), 2)
        for i, n in enumerate(notes, 1):
            self.assertTrue(n.endswith(f"say so in your reply; sampo vetoes restore {i} puts it back"), n)

    def test_restore_brings_one_back_and_it_matches_again(self):
        self.with_judge(judge(lifts=[1, 2]))
        hooks._veto_turn(self.p, "continue", False)
        self.assertEqual(core.veto_hits(self.p, "git push origin main"), [])
        self.fm("vetoes", "restore", "1")
        self.assertEqual(len(core.veto_hits(self.p, "git push origin main")), 1)
        self.assertEqual([g["words"] for g in core.lifted(self.p)], [["release", "fridays"]])
        self.assertNotEqual(self.fm("vetoes", "restore", "5", check=False).returncode, 0)

    def test_the_list_shows_the_lifted_ones_under_their_own_heading(self):
        self.with_judge(judge(lifts=[1]))
        hooks._veto_turn(self.p, "carry on", False)
        text = self.fm("vetoes").stdout
        self.assertIn("Lifted", text)
        self.assertIn("sampo vetoes restore 1", text)
        listed = json.loads(self.fm("vetoes", "--json").stdout)
        self.assertEqual([bool(v.get("lifted")) for v in listed], [False, True])

    def test_restoring_over_a_rule_with_the_same_words_keeps_one(self):
        self.with_judge(judge(lifts=[1]))
        hooks._veto_turn(self.p, "carry on", False)
        core.add_veto(self.p, "never push to main again")
        self.fm("vetoes", "restore", "1")
        self.assertEqual(self.words().count(["push", "main"]), 1)

    def test_the_lifted_list_keeps_the_newest_twenty(self):
        for i in range(25):
            core.add_veto(self.p, f"never touch gadget{i} again")
        recs = core.vetoes(self.p)
        self.with_judge(judge(lifts=list(range(1, len(recs) + 1))))
        hooks._veto_turn(self.p, "continue", False)
        gone = core.lifted(self.p)
        self.assertEqual(len(gone), 20)
        self.assertEqual(gone[-1]["words"], recs[-1]["words"])

    def test_the_lifting_message_is_cut_and_redacted(self):
        self.with_judge(judge(lifts=[1]))
        hooks._veto_turn(self.p, f"carry on, {self.KEY} " + "x" * 200, False)
        g = core.lifted(self.p)[0]
        self.assertLessEqual(len(g["by"]), 80)
        self.assertNotIn(self.KEY[13:30], json.dumps(g))

    def test_the_guard_leaves_restore_to_the_agent_and_still_refuses_rm(self):
        import fmguard  # restoring a rule tightens; only the word rm is the user's (and "restore" is not it)
        self.assertEqual(fmguard.USER_ONLY_FM["vetoes"][0], "rm")
        self.assertFalse(fmguard._user_only_hit("rm", "restore"))


class OneWord(Base):
    def test_a_one_word_veto_never_matches_by_words_alone(self):
        core.add_veto(self.p, "never mocks", words=["exam"])
        self.assertEqual(core.veto_hits(self.p, "capture: write the exam request into the brief"), [])
        self.assertEqual(core.veto_hits(self.p, "exam"), [])

    def test_a_two_word_veto_still_matches(self):
        core.add_veto(self.p, "never push to main")
        self.assertEqual(len(core.veto_hits(self.p, "git push origin main")), 1)
        self.assertEqual(core.veto_hits(self.p, "git push origin dev"), [])

    def test_the_capture_note_stays_quiet_for_a_one_word_veto(self):
        from sampo import cli
        core.add_veto(self.p, "x", words=["exam"])
        self.assertEqual(cli._reask_note(self.p, "T-0001", "run the exam suite"), "")
