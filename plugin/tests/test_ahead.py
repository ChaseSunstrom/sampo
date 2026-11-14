"""T-4662 (part A of T-4185, foresight): `sampo ahead`. At a turn's end a local model writes up to three predictions of what
the user would have to say next, in a strict line format (PREDICT / MISSED / PROBE / ACTION, never one large JSON); each is
checked before it is believed (its probe, one of a closed set of state facts Sampo already reads, is run; a false one is
booked as dropped, one with probe none can only capture or tell) and booked as a write-once row. Nothing here acts on a
row (part B) or scores it (part C). No test calls a real model or the network: moonscale._local and models.complete are
doubles, as test_steermeter's are."""
import datetime
import json
import os
import types
import unittest
import uuid
from unittest import mock

from helpers import ForemanTestCase

from sampo import ahead
from sampo import core as c
from sampo import models, moonscale
from sampo import steermeter as sm


def block(pred="you never pushed it", missed="push before asking the user to look", probe="unpushed",
          action="do sampo ship"):
    return f"PREDICT: {pred}\nMISSED: {missed}\nPROBE: {probe}\nACTION: {action}\n"


def stamp(hours=0.0):
    t = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


class Ahead(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)
        self.prompts, self.replies, self.asked = [], [], []
        self.hits = {}
        self.calls = []
        self.enterContext(mock.patch.dict(ahead.PROBES, {n: self._probe(n) for n in ahead.NAMES}))
        self.enterContext(mock.patch.object(ahead, "verbs", lambda: {"ship", "status", "next", "capture", "steers"}))
        self.enterContext(mock.patch.object(sm, "_standing", lambda: ["standing: never ask the user to run a command"]))

    # ---- the doubles
    def _probe(self, name):
        def probe(p, briefs, now):
            self.calls.append(name)
            got = self.hits.get(name)
            if isinstance(got, Exception):
                raise got
            return got
        return probe

    def model(self, *replies, down=False):
        """Each call answers the next reply (the last repeats); down: no local model is up."""
        self.replies = list(replies)
        prof = {"name": "fake", "base_url": "http://127.0.0.1:1", "model": "m"}

        def local(sensitive):
            self.asked.append(sensitive)
            return None if down else prof

        def complete(pr, messages, **kw):
            self.prompts.append(messages)
            got = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
            if isinstance(got, Exception):
                raise got
            return {"text": got}

        self.enterContext(mock.patch.object(moonscale, "_local", local))
        self.enterContext(mock.patch.object(models, "complete", complete))

    def steer(self, text, missed="", avoidable=True, root="R1", hours=1.0, unread=False):
        n = len(sm._booked(self.p))
        row = {"session": "s1", "uuid": f"u{n}", "at": stamp(hours), "model": "fake", "text": text}
        row.update(unread=True) if unread else row.update(kind="steer", avoidable=avoidable, root=root,
                                                          missed_thought=missed, why="")
        os.makedirs(self.p.dir, exist_ok=True)
        c.append_line(os.path.join(self.p.dir, sm.STEERS), json.dumps(row))

    def rows(self):
        return ahead.rows(self.p)

    def tick(self, **kw):
        return ahead.tick(self.p, reply=kw.pop("reply", "I changed auth.py and all 12 tests pass."),
                          turn=kw.pop("turn", "t1"), **kw)


class Tick(Ahead):
    def test_tick_writes_up_to_three_rows_in_the_strict_line_format(self):
        self.hits = {"unpushed": "3 landed commits unshipped", "cpu": "CPU 40% over the goal"}
        self.model(block() + block("why is the machine so slow", "look at the CPU first", "cpu", "tell the CPU is over goal")
                   + block("did you read my message", "answer it", "none", "capture answer the user's message")
                   + block("a fourth one", "never booked", "none", "tell nothing"))
        got = self.tick()
        rows = self.rows()
        self.assertEqual(len(rows), 3)  # up to three: the fourth block is left out
        self.assertEqual([r["probe"] for r in rows], ["unpushed", "cpu", "none"])
        self.assertEqual(len(got["rows"]), 3)
        first = rows[0]
        self.assertEqual((first["text"], first["missed"], first["action"]["kind"], first["action"]["text"]),
                         ("you never pushed it", "push before asking the user to look", "do", "sampo ship"))
        self.assertEqual((first["turn"], first["acted"], first["model"]), ("t1", False, "fake"))
        for r in rows:
            self.assertIn(r["probe"], ahead.NAMES + ("none",))
            self.assertTrue(r["id"].startswith("A-"))
            for k in ("at", "verdict", "action"):
                self.assertTrue(r[k], k)
        self.assertEqual(len({r["id"] for r in rows}), 3)
        self.assertEqual(len(self.prompts), 1)  # one call: a good reply is not asked twice

    def test_tick_a_malformed_reply_writes_no_row_after_one_retry_that_says_what_was_wrong(self):
        self.hits = {"unpushed": "x"}
        self.model("The user will probably want tests.")
        got = self.tick()
        self.assertEqual((self.rows(), got["rows"]), ([], []))
        self.assertEqual(len(self.prompts), 2)  # asked, then once more with the reason
        self.assertIn("not valid", json.dumps(self.prompts[1]))
        self.assertFalse(os.path.exists(ahead._memo_path(self.p)))  # nothing was understood: the next tick asks again

    def test_tick_one_bad_block_in_a_reply_makes_the_whole_reply_malformed(self):
        self.hits = {"unpushed": "x"}
        self.model(block() + "PREDICT: a second one\nMISSED: the thought\nPROBE: unpushed\n")
        self.tick()
        self.assertEqual(self.rows(), [])

    def test_tick_a_probe_outside_the_closed_set_is_malformed_and_the_retry_may_fix_it(self):
        self.hits = {"unpushed": "x"}
        self.model(block(probe="the user is angry"), block())
        self.tick()
        self.assertEqual(len(self.rows()), 1)
        self.assertIn("unpushed", json.dumps(self.prompts[1]))  # the retry names the closed set

    def test_tick_a_reply_of_none_is_an_answer_not_an_error(self):
        self.hits = {"unpushed": "x"}
        self.model("NONE")
        got = self.tick()
        self.assertEqual((self.rows(), len(self.prompts)), ([], 1))
        self.assertEqual(got["asked"], 1)

    def test_tick_a_do_action_needs_a_real_sampo_verb_and_no_shell_syntax(self):
        self.hits = {"unpushed": "x"}
        for bad in ("do sampo nosuchverb", "do rm -rf /tmp/x", "do sampo ship; rm x", "do sampo ship && echo hi",
                    "do sampo ship $(id)", "do sampo ship `id`", "do sampo ship > f", "do"):
            self.prompts.clear()
            self.model(block(action=bad))
            self.tick(turn=bad)
            self.assertEqual(self.rows(), [], bad)
        self.model(block(action="do sampo steers --tick"))
        self.tick(turn="good")
        self.assertEqual(self.rows()[0]["action"], {"kind": "do", "text": "sampo steers --tick"})

    def test_tick_fields_are_one_clean_line_and_redacted(self):
        self.hits = {"unpushed": "x"}
        self.model(block(pred="say it \x1b[31mred\x1b[0m " + "long " * 80, action="tell token sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAA"))
        self.tick()
        r = self.rows()[0]
        self.assertNotIn("\x1b", r["text"])
        self.assertLessEqual(len(r["text"]), 200)
        self.assertNotIn("sk-ant-api03-AAAA", json.dumps(r))

    def test_tick_paused_asks_nothing_and_writes_nothing(self):
        self.hits = {"unpushed": "x"}
        self.model(block())
        with mock.patch.object(c, "panicked", return_value=True):
            got = self.tick()
        self.assertEqual((self.prompts, self.asked, self.rows(), self.calls), ([], [], [], []))
        self.assertEqual(got["skipped"], "paused")

    def test_tick_no_steer_rows_and_no_state_gap_asks_nothing_but_either_one_asks(self):
        self.model(block(probe="none", action="tell hello"))
        got = self.tick(turn="a")
        self.assertEqual((self.prompts, self.rows(), got["skipped"]), ([], [], "nothing to go on"))
        self.steer("you never looked at the page", "open the page", hours=2)  # a steer row alone is something to go on
        self.tick(turn="b")
        self.assertEqual(len(self.prompts), 1)
        self.prompts.clear()
        self.hits = {"cpu": "over goal"}
        self.tick(turn="c")  # a state gap alone is too
        self.assertEqual(len(self.prompts), 1)

    def test_tick_no_local_model_or_a_transport_error_books_nothing_and_asks_again_next_time(self):
        self.hits = {"unpushed": "x"}
        self.model(block(), down=True)
        got = self.tick()
        self.assertEqual((self.rows(), got["model"]), ([], None))
        self.assertEqual(self.prompts, [])
        self.model(models.ModelError("boom"))
        self.tick()
        self.assertEqual(self.rows(), [])
        self.assertFalse(os.path.exists(ahead._memo_path(self.p)))

    def test_tick_asks_a_local_model_only_never_a_paid_one(self):
        self.hits = {"unpushed": "x"}
        self.model(block())
        self.tick()
        self.assertEqual(self.asked, [False])  # moonscale._local(sensitive): own hardware, loopback-only when sensitive

    def test_tick_a_sensitive_project_asks_for_a_loopback_model_only(self):
        self.hits = {"unpushed": "x"}
        self.model(block())
        with mock.patch.object(c, "read_meta", return_value={"sensitive": True}):
            self.tick()
        self.assertEqual(self.asked, [True])

    def test_tick_the_same_turn_twice_asks_once(self):
        self.hits = {"unpushed": "x"}
        self.model(block())
        self.tick(turn="same")
        self.tick(turn="same")
        self.assertEqual((len(self.prompts), len(self.rows())), (1, 1))
        self.tick(turn="next")
        self.assertEqual((len(self.prompts), len(self.rows())), (2, 2))

    def test_tick_a_turn_with_nothing_predicted_is_not_asked_again(self):
        self.hits = {"unpushed": "x"}
        self.model("NONE")
        self.tick(turn="same")
        self.tick(turn="same")
        self.assertEqual(len(self.prompts), 1)

    def test_tick_the_prompt_holds_the_reply_the_state_the_profile_and_the_newest_avoidable_steers_from_the_rows(self):
        self.hits = {"unpushed": "3 landed commits unshipped, the oldest 5 h"}
        for i in range(7):
            self.steer(f"message {i}", f"MISSEDTHOUGHT{i} think ahead", hours=20 - i)
        self.steer("not avoidable", "NOTAVOIDABLEMISS", avoidable=False, hours=1)
        self.steer("unread one", "UNREADMISS", unread=True, hours=0.5)
        self.model(block())
        self.tick(reply="Everything is done and 12 tests pass.")
        text = json.dumps(self.prompts[0])
        self.assertIn("Everything is done and 12 tests pass.", text)
        self.assertIn("3 landed commits unshipped", text)
        self.assertIn("standing: never ask the user to run a command", text)
        for n in ahead.NAMES:
            self.assertIn(n, text)  # the closed set is shown to the model
        self.assertIn("MISSEDTHOUGHT6", text)  # the nearest: the newest avoidable ones
        self.assertNotIn("MISSEDTHOUGHT0", text)  # only the newest few
        self.assertNotIn("NOTAVOIDABLEMISS", text)
        self.assertNotIn("UNREADMISS", text)

    def test_tick_the_prompt_has_no_hard_coded_complaints_only_what_the_rows_hold(self):
        self.hits = {"unpushed": "x"}
        self.model(block())
        self.tick(turn="a")
        bare = self.prompts[0][0]["content"]
        self.steer("USERWORDSMARKER", "MISSEDMARKER do the thing", hours=3)
        self.tick(turn="b")
        withrow = self.prompts[1][0]["content"]
        extra = [ln for ln in withrow.splitlines() if ln not in bare.splitlines()]
        self.assertTrue(extra and all("MISSEDMARKER" in ln for ln in extra), extra)  # the rows are the only difference
        self.assertNotIn("USERWORDSMARKER", withrow)  # a message's own words never ride along: the missed thought does

    def test_tick_a_reply_is_clipped_and_redacted_before_the_model_sees_it(self):
        self.hits = {"unpushed": "x"}
        self.model(block())
        self.tick(reply="head " + "x" * 5000 + " tail sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAA end")
        text = self.prompts[0][0]["content"]
        self.assertNotIn("sk-ant-api03-AAAA", text)
        self.assertLess(len(text), 9000)

    def test_tick_reads_the_newest_transcripts_last_assistant_reply_when_none_is_given(self):
        from sampo import cost
        cfg = os.path.join(self.tmp, "cfg")
        self.enterContext(mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": cfg}))
        folder = cost.transcripts_dir(self.p.root)
        os.makedirs(folder)
        sid = str(uuid.uuid4())
        lines = [{"type": "user", "message": {"role": "user", "content": "do it"}, "uuid": "u1"},
                 {"type": "assistant", "uuid": "a1", "message": {"role": "assistant", "content": [{"type": "text", "text": "first reply"}]}},
                 {"type": "user", "message": {"role": "user", "content": "more"}, "uuid": "u2"},
                 {"type": "assistant", "uuid": "a2", "message": {"role": "assistant", "content": [
                     {"type": "tool_use", "name": "Bash", "id": "x", "input": {}}, {"type": "text", "text": "the final words"}]}}]
        with open(os.path.join(folder, f"{sid}.jsonl"), "w") as f:
            f.write("\n".join(json.dumps(x) for x in lines) + "\n")
        self.hits = {"unpushed": "x"}
        self.model(block())
        ahead.tick(self.p)
        text = self.prompts[0][0]["content"]
        self.assertIn("the final words", text)
        self.assertNotIn("first reply", text)
        self.assertEqual(self.rows()[0]["turn"], f"{sid}:a2")
        ahead.tick(self.p)  # the same turn: not asked again
        self.assertEqual(len(self.prompts), 1)

    def test_tick_a_second_tick_while_one_runs_does_nothing(self):
        import fcntl
        self.hits = {"unpushed": "x"}
        self.model(block())
        os.makedirs(self.p.dir, exist_ok=True)
        fd = os.open(os.path.join(self.p.dir, "ahead.lock"), os.O_CREAT | os.O_RDWR)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        got = self.tick()
        self.assertEqual((self.prompts, self.rows(), got["skipped"]), ([], [], "running"))


class Probes(Ahead):
    def test_probe_a_false_one_is_booked_as_dropped_and_a_true_one_confirmed(self):
        self.hits = {"unpushed": "3 landed commits unshipped", "cpu": None}
        self.model(block() + block("the machine is slow", "check the CPU", "cpu", "tell the CPU is over goal"))
        self.tick()
        yes, no = self.rows()
        self.assertEqual((yes["verdict"], yes["evidence"]), ("confirmed", "3 landed commits unshipped"))
        self.assertEqual((no["verdict"], no["evidence"]), ("dropped", None))
        self.assertEqual(no["probe"], "cpu")  # booked, not discarded

    def test_probe_one_that_raises_is_taken_as_false_and_booked_with_the_error(self):
        self.hits = {"unpushed": RuntimeError("git is gone"), "cpu": "x"}
        self.model(block() + block("slow", "cpu", "cpu", "tell over goal"))
        self.tick()
        bad, good = self.rows()
        self.assertEqual(bad["verdict"], "dropped")
        self.assertIn("git is gone", bad["error"])
        self.assertEqual(good["verdict"], "confirmed")  # one bad probe never stops the next

    def test_probe_none_can_only_capture_or_tell_never_interrupt(self):
        self.hits = {"unpushed": "x"}
        self.model(block("say it again", "the thought", "none", "do sampo ship")
                   + block("say it once more", "the thought", "none", "capture look into the slow page")
                   + block("and again", "the thought", "none", "tell the page is slow"))
        self.tick()
        do, cap, tell = self.rows()
        self.assertEqual(do["action"]["kind"], "tell")  # a do without a probe is demoted, its line kept as the words
        self.assertEqual(do["action"]["text"], "sampo ship")
        self.assertEqual((cap["action"]["kind"], tell["action"]["kind"]), ("capture", "tell"))
        for r in (do, cap, tell):
            self.assertEqual((r["probe"], r["verdict"]), ("none", "unchecked"))
            self.assertFalse(ahead.may_interrupt(r))
        self.assertNotIn("none", self.calls)  # probe none has nothing to run

    def test_probe_only_a_confirmed_do_may_interrupt(self):
        self.hits = {"unpushed": "x", "cpu": None}
        self.model(block() + block("slow", "cpu", "cpu", "do sampo status") + block("t", "m", "unpushed", "tell it"))
        self.tick()
        a, b, c3 = self.rows()
        self.assertEqual([ahead.may_interrupt(r) for r in (a, b, c3)], [True, False, False])

    def test_probe_is_run_again_after_the_model_answers_not_trusted_from_the_prompts_state(self):
        self.hits = {"unpushed": "x"}
        self.model(block() + block("again", "same", "unpushed", "tell it"))
        self.tick()
        self.assertEqual(self.calls.count("unpushed"), 2)  # once for the state the prompt shows, once to check both rows
        self.assertEqual(self.calls.count("cpu"), 1)  # a probe no row names is run only for the prompt

    def test_probe_the_closed_set_is_the_seven_state_facts(self):
        self.assertEqual(ahead.NAMES, ("unpushed", "unproven", "unseen", "slow_wait", "cpu", "idle_lanes", "unanswered"))


class Readers(unittest.TestCase):
    """Each probe is a thin function over a reader Sampo already has: it asks that reader and nothing else."""

    def setUp(self):
        from sampo import selfcheck
        self.sc = selfcheck
        self.p, self.now = object(), 1_700_000_000.0

    def test_probe_unpushed_is_selfchecks_unshipped(self):
        with mock.patch.object(self.sc, "unshipped", return_value="3 commits") as m:
            self.assertEqual(ahead.PROBES["unpushed"](self.p, [], self.now), "3 commits")
        m.assert_called_once()
        with mock.patch.object(self.sc, "unshipped", return_value=None):
            self.assertIsNone(ahead.PROBES["unpushed"](self.p, [], self.now))

    def test_probe_unproven_is_selfchecks_asks_waiting(self):
        with mock.patch.object(self.sc, "asks_waiting", return_value="2 of the user's concerns aren't shown") as m:
            self.assertIn("concerns", ahead.PROBES["unproven"](self.p, [], self.now))
        m.assert_called_once()

    def test_probe_slow_wait_is_a_job_past_its_typical_or_a_run_of_wait_only_replies(self):
        with mock.patch.object(self.sc, "slow_jobs", return_value=None), \
                mock.patch.object(self.sc, "wait_turns", return_value="4 wait-only replies in a row"):
            self.assertIn("wait-only", ahead.PROBES["slow_wait"](self.p, [], self.now))
        with mock.patch.object(self.sc, "slow_jobs", return_value="the full gates run has run 40 min"), \
                mock.patch.object(self.sc, "wait_turns", return_value=None):
            self.assertIn("gates", ahead.PROBES["slow_wait"](self.p, [], self.now))
        with mock.patch.object(self.sc, "slow_jobs", return_value=None), mock.patch.object(self.sc, "wait_turns", return_value=None):
            self.assertIsNone(ahead.PROBES["slow_wait"](self.p, [], self.now))

    def test_probe_cpu_is_selfchecks_cpu_goal(self):
        with mock.patch.object(self.sc, "cpu_goal", return_value="CPU 31% over the 10% goal") as m:
            self.assertIn("CPU", ahead.PROBES["cpu"](self.p, [], self.now))
        m.assert_called_once()

    def test_probe_unseen_is_a_recent_done_task_whose_surface_step_has_no_surface_run(self):
        from sampo import concerns
        recent = c.iso(self.now - 3600)
        old = c.iso(self.now - 30 * 86400)
        mk = lambda tid, status, upd: types.SimpleNamespace(id=tid, status=status, meta={"updated": upd})  # noqa: E731
        briefs = [mk("T-1", "done", recent), mk("T-2", "done", old), mk("T-3", "active", recent)]
        seen = []

        def gaps(p, b, then=()):
            seen.append(b.id)
            return ["step 2 has no run on the real surface"] if b.id == "T-1" else []
        with mock.patch.object(concerns, "surface_gaps", gaps):
            got = ahead.PROBES["unseen"](self.p, briefs, self.now)
        self.assertIn("T-1", got)
        self.assertEqual(seen, ["T-1"])  # an old task and an open one are not asked about
        with mock.patch.object(concerns, "surface_gaps", lambda p, b, then=(): []):
            self.assertIsNone(ahead.PROBES["unseen"](self.p, briefs, self.now))

    def test_probe_idle_lanes_needs_a_free_builder_slot_and_a_planned_task_waiting(self):
        from sampo import hooks
        planned = types.SimpleNamespace(id="T-9", status="planned", meta={}, title="do the thing")
        with mock.patch.object(hooks, "lanes_free", return_value=True), \
                mock.patch.object(c, "order_queue", return_value=([planned], [])):
            self.assertIn("T-9", ahead.PROBES["idle_lanes"](self.p, [planned], self.now))
        with mock.patch.object(hooks, "lanes_free", return_value=False), \
                mock.patch.object(c, "order_queue", return_value=([planned], [])):
            self.assertIsNone(ahead.PROBES["idle_lanes"](self.p, [planned], self.now))
        with mock.patch.object(hooks, "lanes_free", return_value=True), mock.patch.object(c, "order_queue", return_value=([], [])):
            self.assertIsNone(ahead.PROBES["idle_lanes"](self.p, [], self.now))

    def test_probe_unanswered_is_the_concerns_hold_for_the_newest_session_since_its_last_message(self):
        from sampo import concerns
        held = types.SimpleNamespace
        rows = [{"session": "old", "at": c.iso(self.now - 7200)}, {"session": "s2", "at": c.iso(self.now - 600)}]
        got = {}

        def hold(p, session, floor, told=(), briefs=None, limit=5):
            got.update(session=session, floor=floor)
            return [{"id": "K-ab12", "text": "the login page 500s", "said": 1}], []
        with mock.patch.object(ahead.sm, "rows", return_value=rows), mock.patch.object(concerns, "hold", hold):
            line = ahead.PROBES["unanswered"](self.p, [], self.now)
        self.assertIn("K-ab12", line)
        self.assertEqual(got["session"], "s2")  # the newest message's session
        self.assertLessEqual(got["floor"], self.now - 600)  # from that message on, with the hook's slack
        with mock.patch.object(ahead.sm, "rows", return_value=rows), \
                mock.patch.object(concerns, "hold", lambda *a, **k: ([], [])):
            self.assertIsNone(ahead.PROBES["unanswered"](self.p, [], self.now))
        with mock.patch.object(ahead.sm, "rows", return_value=[]):
            self.assertIsNone(ahead.PROBES["unanswered"](self.p, [], self.now))


class Show(Ahead):
    def make(self):
        self.hits = {"unpushed": "3 landed commits unshipped", "cpu": None}
        self.model(block() + block("slow machine", "look at CPU", "cpu", "tell the CPU is over goal")
                   + block("did you read it", "answer", "none", "capture answer the user"))
        self.tick()

    def test_show_with_no_rows_prints_one_quiet_line(self):
        r = self.fm("ahead")
        self.assertEqual(len(r.stdout.strip().splitlines()), 1)
        self.assertIn("no predictions", r.stdout.lower())
        self.assertEqual(json.loads(self.fm("ahead", "--json").stdout)["open"], [])

    def test_show_lists_the_open_predictions_with_probe_verdict_and_action(self):
        self.make()
        out = self.fm("ahead").stdout
        for want in ("you never pushed it", "unpushed", "confirmed", "do sampo ship", "unchecked", "capture"):
            self.assertIn(want, out)
        self.assertNotIn("slow machine", out)  # the dropped one is booked but not open
        self.assertIn("1 dropped", out)

    def test_show_json_has_the_open_rows_and_the_counts(self):
        self.make()
        d = json.loads(self.fm("ahead", "--json").stdout)
        self.assertEqual([r["probe"] for r in d["open"]], ["unpushed", "none"])
        for r in d["open"]:
            for k in ("id", "turn", "text", "missed", "probe", "verdict", "action", "acted"):
                self.assertIn(k, r)
        self.assertEqual((d["dropped"], d["rows"]), (1, 3))

    def test_show_an_acted_row_is_no_longer_open(self):
        self.make()
        rows = self.rows()
        path = os.path.join(self.p.dir, ahead.ROWS)
        with open(path, "w") as f:  # part B marks a row acted; this part only reads that mark
            for r in rows:
                f.write(json.dumps(dict(r, acted=r["probe"] == "unpushed")) + "\n")
        d = json.loads(self.fm("ahead", "--json").stdout)
        self.assertEqual([r["probe"] for r in d["open"]], ["none"])

    def test_show_a_torn_line_in_the_rows_file_is_skipped(self):
        self.make()
        with open(os.path.join(self.p.dir, ahead.ROWS), "a") as f:
            f.write('{"id": "A-9", "tur\n')
        self.assertEqual(len(json.loads(self.fm("ahead", "--json").stdout)["open"]), 2)

    def test_show_tick_by_hand_with_the_project_paused_asks_nothing_and_says_so(self):
        self.fm("pause", "on")  # the test's own FOREMAN_HOME: no other session is paused by it
        r =self.fm("ahead", "--tick", env={"MOONS_LOCAL": ""})
        self.assertIn("paused", r.stdout)
        self.assertEqual(self.rows(), [])

    def test_show_tick_by_hand_with_no_local_model_says_so_and_books_nothing(self):
        self.hits = {}
        r = self.fm("ahead", "--tick", env={"MOONS_LOCAL": ""})
        self.assertEqual(self.rows(), [])
        self.assertIn("ahead", r.stdout)

    def test_show_the_verb_is_registered_with_help_that_names_what_it_does(self):
        line = next(x for x in self.fm("help", "--all").stdout.splitlines() if x.strip().startswith("ahead"))
        self.assertIn("steer", line)
        self.assertIn("--tick", self.fm("ahead", "--help").stdout)


if __name__ == "__main__":
    unittest.main()
