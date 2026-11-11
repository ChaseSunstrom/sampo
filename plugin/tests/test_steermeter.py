"""T-4181: the standing steer meter. Every message a user types (mid-turn ones too) is read, with the agent's last words,
by a local model; the row is write-once; a reply that won't parse is booked unread, never guessed. No test calls a real
model: moonscale._local and models.complete are doubles, as test_foresight's local arm does."""
import datetime
import json
import os
import subprocess
import unittest
import uuid
from unittest import mock

from helpers import ForemanTestCase

from sampo import core as c
from sampo import models, moonscale
from sampo import steermeter as sm

GOOD = {"kind": "steer", "avoidable": True, "missed_thought": "check the live page, not only the tests",
        "root": "R7", "why": "it said done after the tests passed", "concern": "the page really works",
        "surface": "the login page", "same_as": "new"}


def reply(**kw):
    d = dict(GOOD, **kw)
    return json.dumps({k: v for k, v in d.items() if v is not None})  # a None drops the field


def stamp(days=0.0, hours=0.0, seconds=0.0):
    t = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days, hours=hours, seconds=seconds)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


class Meter(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)
        cfg = os.path.join(self.tmp, "cfg")
        self.enterContext(mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": cfg}))
        from sampo import cost
        self.folder = cost.transcripts_dir(self.p.root)
        os.makedirs(self.folder, exist_ok=True)
        self.asked, self.prompts, self.replies, self.down = [], [], [], False

    # ---- the doubles
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

    # ---- a transcript
    def session(self, entries, sid=None, entrypoint="cli"):
        sid = sid or str(uuid.uuid4())
        lines = []
        for i, e in enumerate(entries):
            e = dict(e, sessionId=sid, uuid=f"{sid[:8]}-{i}")
            e.setdefault("timestamp", stamp(hours=len(entries) - i))
            if i == 0:
                e["entrypoint"], e["cwd"] = entrypoint, self.repo
            lines.append(json.dumps(e))
        with open(os.path.join(self.folder, f"{sid}.jsonl"), "w") as f:
            f.write("\n".join(lines) + "\n")
        return sid

    @staticmethod
    def user(text, **kw):
        return dict({"type": "user", "message": {"role": "user", "content": text}}, **kw)

    @staticmethod
    def said(text):
        return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}

    @staticmethod
    def queued(text, **kw):
        return dict({"type": "attachment", "attachment": {"type": "queued_command", "prompt": text}}, **kw)

    def rows(self):
        return sm.rows(self.p)


class Booked(Meter):
    def test_booked_a_typed_and_a_mid_turn_message_are_read_with_the_agents_last_words_once(self):
        self.session([self.user("fix the login page"), self.said("I changed auth.py; all 12 tests pass, so it is done."),
                      self.queued("you never opened the page, did you? it still 500s"), self.said("Checking it now.")])
        self.model(reply(kind="new-request", avoidable=False, root="none", missed_thought=""), reply())
        got = sm.tick(self.p)
        self.assertEqual((got["read"], got["unread"]), (2, 0))
        first, second = self.rows()
        self.assertEqual((first["kind"], first["avoidable"], first["root"]), ("new-request", False, "none"))
        self.assertEqual((second["kind"], second["avoidable"], second["root"], second["model"]), ("steer", True, "R7", "fake"))
        self.assertEqual(second["missed_thought"], GOOD["missed_thought"])
        for k in ("session", "uuid", "at", "text"):
            self.assertTrue(second[k], k)
        self.assertTrue(second["at"].endswith("Z"))
        # the second message was read against what the agent last said, and the first against nothing
        asked = json.dumps(self.prompts[1])
        self.assertIn("all 12 tests pass", asked)
        self.assertIn("never opened the page", asked)
        self.assertIn("fix the login page", asked)  # an earlier user message rides along
        self.assertNotIn("all 12 tests pass", json.dumps(self.prompts[0]))
        self.assertIn("R7", asked)  # the roots come from the data file into the prompt
        # write-once: a second tick asks nothing and books nothing
        before, n = self.rows(), len(self.prompts)
        again = sm.tick(self.p)
        self.assertEqual((again["read"], len(self.prompts), self.rows()), (0, n, before))

    def test_booked_text_is_cut_and_redacted_and_the_agents_words_cut_in_the_case(self):
        token = "ghp_" + "a1B2c3D4" * 4
        self.session([self.said("x" * 5000 + " TAIL-OF-THE-REPLY"),
                      self.user(f"use {token} and " + "y" * 400)])
        self.model(reply())
        sm.tick(self.p)
        (row,) = self.rows()
        self.assertLessEqual(len(row["text"]), 160)
        self.assertNotIn(token, json.dumps(row))
        self.assertNotIn(token, json.dumps(self.prompts))
        self.assertIn("TAIL-OF-THE-REPLY", json.dumps(self.prompts[0]))  # the end of the reply is what the user answers
        self.assertNotIn("x" * 1500, json.dumps(self.prompts[0]))  # cut to 1,200

    def test_booked_headless_sessions_are_not_the_users_words(self):
        self.session([self.user("builder brief: do the thing"), self.said("ok")], entrypoint="sdk-cli")
        self.model(reply())
        self.assertEqual(sm.tick(self.p)["read"], 0)
        self.assertEqual(self.prompts, [])

    def test_booked_a_message_in_a_row_is_found_by_its_session_and_uuid(self):
        sid = self.session([self.user("one"), self.said("a"), self.user("two")])
        self.model(reply())
        sm.tick(self.p)
        self.assertEqual({(r["session"], r["uuid"]) for r in self.rows()}, {(sid, f"{sid[:8]}-0"), (sid, f"{sid[:8]}-2")})


class Unread(Meter):
    def test_unread_bad_json_is_retried_once_then_booked_unread_never_guessed(self):
        self.session([self.said("done"), self.user("no, that is wrong")])
        self.model("sorry, I cannot say", "still not json")
        got = sm.tick(self.p)
        self.assertEqual((got["read"], got["unread"], len(self.prompts)), (0, 1, 2))
        self.assertIn("not valid", json.dumps(self.prompts[1]).lower())  # the retry says what was wrong
        (row,) = self.rows()
        self.assertTrue(row["unread"])
        self.assertNotIn("avoidable", row)
        self.assertNotIn("root", row)
        self.assertEqual(sm.rate(self.p, stamp(days=2), stamp()), (0, 0))  # unread rows are in no rate
        sm.tick(self.p)
        self.assertEqual(len(self.prompts), 2, "an unread message is not asked again")

    def test_unread_the_retry_tells_the_model_what_was_wrong_and_a_good_second_answer_is_booked(self):
        self.session([self.said("done"), self.user("no, that is wrong")])
        self.model(reply(root="R99"), reply(root="R1"))
        sm.tick(self.p)
        self.assertIn("R99", json.dumps(self.prompts[1]))
        (row,) = self.rows()
        self.assertEqual((row["root"], row.get("unread")), ("R1", None))

    def test_unread_each_wrong_shape_is_unparseable(self):
        bad = ['{"kind": "steer", "avoidable": "yes", "missed_thought": "x", "root": "R1", "why": "y"}',
               '{"kind": "rant", "avoidable": true, "missed_thought": "x", "root": "R1", "why": "y"}',
               '{"kind": "steer", "avoidable": true, "root": "R1"}',
               '["steer"]', "", '{"kind": "steer", "avoidable": true, "missed_thought": 5, "root": "R1", "why": "y"}']
        for text in bad:
            self.assertIsNone(sm.parse(text)[0], text)
        ok, _ = sm.parse("Sure!\n```json\n" + reply(missed_thought="m" * 500, why="w" * 500) + "\n```")
        self.assertEqual((len(ok["missed_thought"]), len(ok["why"])), (200, 160))  # a long answer is cut, not refused

    def test_unread_machine_text_is_skipped_and_never_asked(self):
        self.session([self.user("<task-notification>build finished</task-notification>"),
                      self.queued("<task-notification><task-id>x</task-id></task-notification>"),
                      self.user("<system-reminder>be careful</system-reminder>"), self.said("ok"), self.user("   ")])
        self.model(reply())
        self.assertEqual(sm.tick(self.p)["read"], 0)
        self.assertEqual((self.prompts, self.rows()), ([], []))

    def test_unread_no_model_up_books_nothing_and_a_transport_error_stops_the_tick(self):
        self.session([self.user("one"), self.said("a"), self.user("two"), self.said("b"), self.user("three")])
        self.model(reply(), down=True)
        got = sm.tick(self.p)
        self.assertEqual((got["read"], got["model"], self.rows()), (0, None, []))
        self.assertEqual(self.prompts, [])
        self.model(models.ModelError("connection refused"))
        got = sm.tick(self.p)
        self.assertEqual((got["read"], self.rows(), len(self.prompts)), (0, [], 1), "one failed call, then the tick stops")
        self.model(reply())  # the next tick retries them all
        self.assertEqual(sm.tick(self.p)["read"], 3)

    def test_unread_a_sensitive_project_asks_for_a_loopback_model_only(self):
        self.fm("sensitive", "on")
        self.session([self.user("one")])
        self.model(reply())
        sm.tick(self.p)
        self.assertEqual(self.asked, [True])


class Fields(Meter):
    """The coordinator's review of a real tick (T-4181): concern and surface were missing, 'new' went to messages not
    avoidable, the judgement was lenient, machine text and duplicates and sub-agent lines were counted."""

    def test_fields_a_steer_or_new_request_needs_a_concern_and_a_surface_else_one_retry_then_unread(self):
        for kind in ("steer", "new-request"):
            for field in ("concern", "surface"):
                got, why = sm.parse(reply(kind=kind, **{field: None}))
                self.assertIsNone(got, (kind, field))
                self.assertIn(field, why)
        self.assertIsNone(sm.parse(reply(concern="   "))[0])  # blank is missing
        self.assertEqual(sm.parse(reply(surface="none"))[0]["surface"], "none")  # the word none is an answer
        for kind in ("answer", "other"):  # these two need neither
            self.assertEqual(sm.parse(reply(kind=kind, avoidable=False, root="none", concern=None, surface=None))[1], None)
        self.session([self.said("done"), self.user("no, that is wrong")])
        self.model(reply(concern=None), reply(concern="the page really works"))
        sm.tick(self.p)
        self.assertIn("concern", json.dumps(self.prompts[1]))  # the retry names the missing field
        (row,) = self.rows()
        self.assertEqual((row["concern"], row["surface"], row.get("unread")), ("the page really works", "the login page", None))
        self.session([self.said("done"), self.user("again, wrong")])
        self.model(reply(surface=None), reply(surface=None))
        sm.tick(self.p)
        self.assertEqual([bool(r.get("unread")) for r in self.rows()], [False, True])

    def test_fields_not_avoidable_always_means_root_none_and_new_is_only_for_an_avoidable_row(self):
        for root in ("new", "R3", "none", "R99"):
            got, _ = sm.parse(reply(avoidable=False, root=root))
            self.assertEqual((got["avoidable"], got["root"]), (False, "none"), root)
        self.assertEqual(sm.parse(reply(root="new"))[0]["root"], "new")
        self.assertIsNone(sm.parse(reply(avoidable=True, root="none"))[0])
        self.assertIsNone(sm.parse(reply(avoidable=True, root="R99"))[0])

    def test_fields_the_prompt_carries_the_rules_and_every_worked_example_from_the_data_file(self):
        g = sm.guide()
        self.assertTrue(8 <= len(g["examples"]) <= 10, len(g["examples"]))
        self.assertGreaterEqual(len(g["avoidable"]), 6)
        self.assertGreaterEqual(len(g["not_avoidable"]), 4)
        self.assertEqual({x["reply"]["avoidable"] for x in g["examples"]}, {True, False})
        for x in g["examples"]:
            self.assertEqual(sm.parse(json.dumps(x["reply"]))[1], None, x)  # an example the reader would book
            self.assertLess(len(x["said"]) + len(x["message"]), 260, "kept short")
        self.session([self.said("done"), self.user("one more thing")])
        self.model(reply())
        sm.tick(self.p)
        asked = json.dumps(self.prompts[0])
        for line in g["avoidable"] + g["not_avoidable"]:
            self.assertIn(json.dumps(line)[1:-1], asked)
        for x in g["examples"]:
            self.assertIn(json.dumps(x["message"])[1:-1], asked)
        self.assertIn("should not have needed to say", asked)

    def test_fields_the_examples_name_no_one(self):
        blob = json.dumps(sm.guide()).lower()
        for bad in ("http", ".com", "@", "/home", "chase", "sampo", "foreman", "jarvis", "sk-", "ghp_"):
            self.assertNotIn(bad, blob)


class Machine(Meter):
    COMPACT = "This session is being continued from a previous conversation that ran out of context. Summary: ..."

    def test_machine_summaries_and_pasted_machine_blocks_are_never_read(self):
        self.session([self.user(self.COMPACT, isCompactSummary=True), self.user(self.COMPACT),
                      self.user("<cross-session-message from=x>do this</cross-session-message>"),
                      self.user("<teammate-message>hi</teammate-message>"),
                      self.user("<bash-input>ls</bash-input><bash-stdout>a\nb</bash-stdout>"),
                      self.user("<task-notification>a</task-notification>\n<system-reminder>b</system-reminder>"),
                      self.said("ok"), self.user("a real one")])
        self.model(reply())
        sm.tick(self.p)
        self.assertEqual([r["text"] for r in self.rows()], ["a real one"])

    def test_machine_a_reminder_after_the_users_words_is_cut_off_the_words(self):
        self.session([self.user("it is broken\n<system-reminder>be careful, SECRET-REMINDER</system-reminder>")])
        self.model(reply())
        sm.tick(self.p)
        self.assertNotIn("SECRET-REMINDER", json.dumps(self.prompts) + json.dumps(self.rows()))
        self.assertIn("it is broken", json.dumps(self.prompts))

    def test_machine_the_same_text_in_two_entries_within_two_minutes_books_once(self):
        self.session([self.queued("fix it now", timestamp=stamp(hours=1)),
                      self.user("fix it now", timestamp=stamp(hours=1, seconds=-90)),
                      self.user("fix it now", timestamp=stamp(hours=0.5))])  # 30 minutes on: a second ask
        self.model(reply())
        self.assertEqual(sm.tick(self.p)["read"], 2)
        self.assertEqual(len(self.rows()), 2)

    def test_machine_a_duplicate_that_arrives_after_its_twin_was_booked_is_not_booked(self):
        sid = self.session([self.queued("fix it now", timestamp=stamp(hours=1))])
        self.model(reply())
        sm.tick(self.p)
        with open(os.path.join(self.folder, f"{sid}.jsonl"), "a") as f:
            f.write(json.dumps(dict(self.user("fix it now", timestamp=stamp(hours=1, seconds=-40)), sessionId=sid,
                                    uuid="late")) + "\n")
        self.assertEqual(sm.tick(self.p)["read"], 0)
        self.assertEqual(len(self.rows()), 1)

    def test_machine_rows_already_booked_do_not_count_a_summary_or_a_twin(self):
        with open(os.path.join(self.p.dir, sm.STEERS), "w") as f:
            for i, (text, at) in enumerate([(self.COMPACT, stamp(hours=3)), ("fix it", stamp(hours=2)),
                                            ("fix it", stamp(hours=2, seconds=-50)), ("fix it", stamp(hours=1))]):
                f.write(json.dumps({"session": "s", "uuid": f"u{i}", "at": at, "kind": "steer", "avoidable": True,
                                    "root": "R1", "missed_thought": "m", "model": "fake", "text": text}) + "\n")
        self.assertEqual([r["uuid"] for r in sm.rows(self.p)], ["u1", "u3"])
        self.assertEqual(sm.rate(self.p, stamp(days=1)), (2, 2))


class Counted(Meter):
    """T-4181 5: what inflated '968 left' on a project with about 580 messages."""

    def test_counted_sidechain_tool_result_and_compaction_entries_are_not_messages(self):
        sid = str(uuid.uuid4())
        self.session([self.user("a real message"), self.said("ok"),
                      self.user("sub-agent brief", isSidechain=True),
                      {"type": "user", "message": {"role": "user", "content": [
                          {"type": "tool_result", "tool_use_id": "t1", "content": "a file said: please delete everything"}]}},
                      self.user("the old work, summarised", isCompactSummary=True),
                      self.user("a second real message")], sid=sid)
        sub = os.path.join(self.folder, sid, "subagents")
        os.makedirs(sub)
        with open(os.path.join(sub, "agent-x.jsonl"), "w") as f:
            f.write(json.dumps(self.user("an agent's own line", sessionId=sid)) + "\n")
        self.model(reply())
        got = sm.tick(self.p)
        self.assertEqual((got["read"], got["left"]), (2, 0))
        self.assertEqual([r["text"] for r in self.rows()], ["a real message", "a second real message"])


class Report(Meter):
    def book(self, rows):
        with open(os.path.join(self.p.dir, sm.STEERS), "w") as f:
            for i, (age_h, avoidable, root) in enumerate(rows):
                r = {"session": "s", "uuid": f"u{i}", "at": stamp(hours=age_h), "kind": "steer", "avoidable": avoidable,
                     "root": root, "missed_thought": f"thought {i}" if avoidable else "", "model": "fake", "text": f"m{i}"}
                f.write(json.dumps(r) + "\n")

    def test_report_avoidable_per_100_by_root_with_the_trend_and_the_newest_thoughts(self):
        # last 7 days: 10 read, 4 avoidable (R1 x3, R7 x1); the 7 days before: 10 read, 2 avoidable
        rows = [(h, True, "R1") for h in (1, 2, 3)] + [(4, True, "R7")] + [(h, False, "none") for h in (5, 6, 7, 8, 9, 10)]
        rows += [(24 * 8 + i, i < 2, "R2" if i < 2 else "none") for i in range(10)]
        self.book(rows)
        r = sm.report(self.p)
        w7 = r["windows"]["7d"]
        self.assertEqual((w7["read"], w7["avoidable"], w7["per100"]), (10, 4, 40.0))
        self.assertEqual(w7["by_root"], {"R1": 30.0, "R7": 10.0})
        self.assertEqual((w7["previous"]["read"], w7["previous"]["per100"]), (10, 20.0))
        self.assertEqual(w7["trend"], "up")
        self.assertEqual((r["windows"]["1d"]["read"], r["windows"]["30d"]["read"]), (10, 20))
        self.assertEqual(r["messages"], 20)
        self.assertEqual(len(r["newest"]), 3)
        self.assertEqual(r["newest"][0]["missed_thought"], "thought 0")  # newest first
        self.assertEqual(r["roots"]["R1"]["name"], "read alone")

    def test_report_rate_gives_before_and_after_counts_for_a_two_proportion_test(self):
        self.book([(1, True, "R1"), (2, False, "none"), (24 * 3, True, "R2"), (24 * 3 + 1, True, "R2"), (24 * 3 + 2, False, "none")])
        self.assertEqual(sm.rate(self.p, stamp(days=2), stamp()), (1, 2))
        self.assertEqual(sm.rate(self.p, stamp(days=5), stamp(days=2)), (2, 3))
        self.assertEqual(sm.rate(self.p, stamp(days=5), stamp(days=2), root="R2"), (2, 3))
        self.assertEqual(sm.rate(self.p, stamp(days=5), stamp(days=2), root="R1"), (0, 3))
        lead = sm.compare((2, 3), (1, 2))
        self.assertEqual(set(lead), {"p", "diff", "before", "after"})
        self.assertEqual(sm.compare((0, 0), (1, 2))["p"], 1.0)

    def test_report_the_verb_prints_json_and_text_and_all_adds_every_project(self):
        self.book([(1, True, "R1"), (2, False, "none")])
        j = json.loads(self.fm("steers", "--json").stdout)
        self.assertEqual(j["windows"]["7d"]["read"], 2)
        text = self.fm("steers").stdout
        self.assertIn("per 100", text)
        self.assertIn("R1", text)
        both = json.loads(self.fm("steers", "--all", "--json").stdout)
        self.assertEqual(both["machine"]["windows"]["7d"]["read"], 2)
        self.assertIn(self.p.slug, [x["project"] for x in both["projects"]])

    def test_report_status_shows_a_line_only_after_twenty_messages(self):
        self.fm("capture", "a request so status has a task to show")
        self.book([(h, h % 2 == 0, "R1" if h % 2 == 0 else "none") for h in range(1, 20)])
        self.assertIsNone(sm.status_line(self.p))
        self.assertNotIn("per 100", self.fm("status").stdout)
        self.book([(h, h % 2 == 0, "R1" if h % 2 == 0 else "none") for h in range(1, 21)])
        line = sm.status_line(self.p)
        self.assertIn("avoidable", line)
        self.assertIn("per 100", line)
        self.assertIn(line, self.fm("status").stdout)

    def test_report_an_empty_book_says_so(self):
        text = self.fm("steers").stdout
        self.assertIn("no messages read", text)


class Detached(Meter):
    def test_detached_the_stop_hook_spawns_the_tick_and_never_asks_a_model(self):
        self.session([self.user("one"), self.said("a"), self.user("two")])
        self.model(RuntimeError("a model call on a hook path"))
        spawned = []
        env = {k: v for k, v in os.environ.items() if k != "FOREMAN_NO_BACKGROUND"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(subprocess, "Popen", lambda argv, **kw: spawned.append((argv, kw))):
            sm.spawn_detached(self.p)
            sm.spawn_detached(self.p)  # inside the window: no second child
        self.assertEqual(len(spawned), 1)
        argv, kw = spawned[0]
        self.assertEqual(argv[1:], ["steers", "--tick"])
        self.assertTrue(kw["start_new_session"])
        self.assertEqual(kw["stdin"], subprocess.DEVNULL)
        self.assertEqual(self.prompts, [])
        self.assertEqual(self.rows(), [])

    def test_detached_the_stop_hook_path_calls_the_spawner_not_the_reader(self):
        import inspect
        from sampo import hooks
        src = inspect.getsource(hooks)
        self.assertIn("steermeter.spawn_detached", src)
        self.assertNotIn("steermeter.tick", src)

    def test_detached_a_tick_reads_at_most_its_cap_newest_first_and_stops_at_its_deadline(self):
        entries = []
        for i in range(25):
            entries += [self.user(f"message {i}"), self.said("ok")]
        self.session(entries)
        self.model(reply())
        got = sm.tick(self.p)  # the default cap
        self.assertEqual((got["read"], got["left"], len(self.prompts)), (sm.LIMIT, 25 - sm.LIMIT, sm.LIMIT))
        self.assertEqual(sm.LIMIT, 20)
        self.assertIn("message 24", json.dumps(self.rows()))  # the newest were read first
        self.assertNotIn('"message 0"', json.dumps(self.rows()))
        with mock.patch.object(sm, "BUDGET_S", -1):  # no time left: nothing is asked
            self.assertEqual(sm.tick(self.p)["read"], 0)
        self.assertEqual(len(self.prompts), sm.LIMIT)

    def test_detached_a_second_tick_while_one_runs_does_nothing(self):
        import fcntl
        self.session([self.user("one")])
        self.model(reply())
        with open(os.path.join(self.p.dir, "steers.lock"), "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(sm.tick(self.p)["read"], 0)
        self.assertEqual(self.prompts, [])


class Folding(Meter):
    """T-4200 review: the meter decides which message says an open concern again (same_as), with a fake model; the
    replies name the K- ids the test can compute (concern_id of the session and the entry's uuid)."""

    def talk(self, sid, *texts):
        entries = []
        for t in texts:
            entries += [self.said("on it"), self.user(t)]
        return self.session(entries, sid=sid)

    @staticmethod
    def uuid_of(sid, k):
        return f"{sid[:8]}-{2 * k + 1}"  # talk(): the k-th user message is entry 2k+1

    def book(self, sid, uuid, text, at, concern):
        """A row as the meter booked it before it made a decision (no repeat_of key)."""
        r = {"session": sid, "uuid": uuid, "at": at, "model": "fake", "text": text, "kind": "steer", "avoidable": True,
             "root": "R7", "missed_thought": "check the live page", "why": "it said done", "concern": concern,
             "surface": "the push"}
        c.append_line(os.path.join(self.p.dir, "steers.jsonl"), json.dumps(r, ensure_ascii=False))

    def test_folding_three_messages_about_one_thing_become_one_concern_said_three_times(self):
        from sampo import concerns as cn
        sid = "aaaaaaaa-1111-4111-8111-000000000001"
        self.talk(sid, "why is the page blank", "still blank after the deploy", "it is still blank, look at it live")
        k1 = cn.concern_id(sid, self.uuid_of(sid, 0))
        self.model(reply(concern="the page works", same_as="new"), reply(concern="the page works", same_as=k1),
                   reply(concern="the page works", same_as=k1))
        self.assertEqual(sm.tick(self.p)["read"], 3)
        self.assertIn(k1, self.prompts[1][0]["content"])  # the second message was asked with the first concern open
        self.assertEqual([(x["id"], x["said"]) for x in cn.fold(self.p)], [(k1, 3)])

    def test_folding_an_unrelated_message_stays_apart(self):
        from sampo import concerns as cn
        sid = "aaaaaaaa-1111-4111-8111-000000000002"
        self.talk(sid, "why is the page blank", "and the push never reached GitHub")
        k1 = cn.concern_id(sid, self.uuid_of(sid, 0))
        self.model(reply(concern="the page works", same_as="new"),
                   reply(concern="the push reaches GitHub", same_as="new"))
        sm.tick(self.p)
        self.assertIn(k1, self.prompts[1][0]["content"])
        self.assertEqual(sorted(x["said"] for x in cn.fold(self.p)), [1, 1])

    def test_folding_a_wrong_merge_can_be_split(self):
        from sampo import concerns as cn
        sid = "aaaaaaaa-1111-4111-8111-000000000003"
        self.talk(sid, "why is the page blank", "and the push never reached GitHub")
        k1 = cn.concern_id(sid, self.uuid_of(sid, 0))
        self.model(reply(concern="the page works", same_as="new"),
                   reply(concern="the push reaches GitHub", same_as=k1))  # the model linked two unrelated asks
        sm.tick(self.p)
        self.assertEqual([x["said"] for x in cn.fold(self.p)], [2])
        self.assertEqual(len(cn.split(self.p, k1)["into"]), 2)
        self.assertEqual(sorted(x["said"] for x in cn.fold(self.p)), [1, 1])

    def test_folding_a_same_as_that_names_no_open_concern_is_unread_not_guessed(self):
        sid = "aaaaaaaa-1111-4111-8111-000000000004"
        self.talk(sid, "why is the page blank", "and the push never reached GitHub")
        self.model(reply(concern="the page works", same_as="new"), reply(concern="the push", same_as="K-FFFFFF"),
                   reply(concern="the push", same_as="K-FFFFFF"))
        got = sm.tick(self.p)
        self.assertEqual((got["read"], got["unread"]), (1, 1))
        self.assertEqual([bool(r.get("unread")) for r in self.rows()], [False, True])

    def test_folding_fold_rows_decides_the_old_rows_once_and_says_which_repeat(self):
        from sampo import concerns as cn
        sid = "bbbbbbbb-2222-4222-8222-000000000001"
        self.book(sid, "u1", "why is the page blank", stamp(hours=3), "the page works")
        self.book(sid, "u2", "still blank after the deploy", stamp(hours=2), "the page works")
        self.book(sid, "u3", "and the push never reached GitHub", stamp(hours=1), "the push reaches GitHub")
        k1 = cn.concern_id(sid, "u1")
        self.model(json.dumps({"same_as": "new"}), json.dumps({"same_as": k1}), json.dumps({"same_as": "new"}))
        got = sm.fold_rows(self.p)
        self.assertEqual((got["asked"], got["same"], got["new"], got["unread"], got["left"]), (3, 1, 2, 0, 0))
        self.assertEqual(sorted(x["said"] for x in cn.fold(self.p)), [1, 2])
        self.assertEqual(sm.fold_rows(self.p)["asked"], 0)  # decided rows are never asked again
        self.assertEqual(len(self.prompts), 3)

    def test_folding_fold_rows_asks_at_most_its_limit_and_the_rest_next_run(self):
        sid = "bbbbbbbb-2222-4222-8222-000000000002"
        for i, h in enumerate((3, 2, 1)):
            self.book(sid, f"u{i}", f"message {i}", stamp(hours=h), f"concern {i}")
        self.model(json.dumps({"same_as": "new"}))
        self.assertEqual(sm.fold_rows(self.p, limit=2)["left"], 1)
        self.assertEqual(sm.fold_rows(self.p, limit=2)["asked"], 1)
        self.assertEqual(len(self.prompts), 3)

    def test_folding_no_model_up_writes_nothing(self):
        sid = "bbbbbbbb-2222-4222-8222-000000000003"
        self.book(sid, "u1", "why is the page blank", stamp(hours=1), "the page works")
        self.model(json.dumps({"same_as": "new"}), down=True)
        got = sm.fold_rows(self.p)
        self.assertEqual((got["asked"], got["model"]), (0, None))
        self.assertFalse(os.path.exists(os.path.join(self.p.dir, "concerns.jsonl")))

    def test_folding_two_unreadable_replies_book_the_row_unread_as_its_own_concern(self):
        from sampo import concerns as cn
        sid = "bbbbbbbb-2222-4222-8222-000000000004"
        self.book(sid, "u1", "why is the page blank", stamp(hours=2), "the page works")
        self.book(sid, "u2", "and the push never reached GitHub", stamp(hours=1), "the push reaches GitHub")
        self.model(json.dumps({"same_as": "new"}), "nope", json.dumps({"same_as": "K-FFFFFF"}))
        got = sm.fold_rows(self.p)
        self.assertEqual((got["asked"], got["unread"]), (2, 1))
        self.assertEqual(sorted(x["said"] for x in cn.fold(self.p)), [1, 1])

    def test_folding_refused_while_paused_by_the_verb_and_the_function(self):
        from sampo import concerns as cn
        self.book("bbbbbbbb-2222-4222-8222-000000000005", "u1", "why", stamp(hours=1), "the page works")
        self.model(json.dumps({"same_as": "new"}))
        self.fm("pause", "on")
        with self.assertRaises(cn.Refused):
            sm.fold_rows(self.p)
        got = self.fm("concerns", "fold", check=False)
        self.assertNotEqual(got.returncode, 0)
        self.assertIn("paused", got.stderr + got.stdout)
        self.assertEqual(self.prompts, [])


if __name__ == "__main__":
    unittest.main()
