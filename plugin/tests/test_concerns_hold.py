"""T-4202: a concern opened this turn with no task, proof or answer holds the stop once (T-4188 part C). The Stop hook
reads the folded concerns only (the meter's booked rows and the ledger; no model runs there); rows are written as the
meter writes them (steers.jsonl). hold: it fires once per concern per session. release: a task, a proof or an answer
each let it go, and a drive that is off or a machine that is paused never holds."""
import datetime
import json
import os
import time

from test_hooks import HookCase, parse

from sampo import concerns as cn
from sampo import core as c


def stamp(seconds=0.0):
    t = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=seconds)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


class Case(HookCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = self.project()

    def prompt(self, session="sess-1"):
        self.hook("UserPromptSubmit", {"prompt": "where can I see it", "session_id": session})

    def book(self, uuid="u1", concern="the work reaches GitHub", session="sess-1", at=None, repeat_of=None, text=None):
        r = {"session": session, "uuid": uuid, "at": at or stamp(), "model": "fake", "text": text or f"{concern}, why not",
             "kind": "steer", "avoidable": True, "root": "R7", "missed_thought": "check the push", "why": "said done",
             "concern": concern, "surface": "the push"}
        if repeat_of:
            r["repeat_of"] = repeat_of
        c.append_line(os.path.join(self.p.dir, "steers.jsonl"), json.dumps(r, ensure_ascii=False))
        return cn.concern_id(session, uuid)

    def stop(self, msg="Done, the tests pass.", session="sess-1"):
        out = self.hook("Stop", {"stop_hook_active": False, "last_assistant_message": msg, "session_id": session})
        return parse(out) or {}

    def held(self, out):
        return out.get("decision") == "block" and "Sampo concerns" in (out.get("reason") or "")


class Hold(Case):
    def test_hold_an_open_concern_with_nothing_behind_it_holds_the_stop_once(self):
        self.prompt()
        cid = self.book()
        out = self.stop()
        self.assertTrue(self.held(out), out)
        self.assertIn(cid, out["reason"])
        self.assertIn("the work reaches GitHub", out["reason"])
        self.assertIn("sampo concerns", out["reason"])
        self.assertFalse(self.held(self.stop()), "the same concern never holds twice")
        self.prompt()
        self.assertFalse(self.held(self.stop()), "nor in the next turn of the session")

    def test_hold_a_second_concern_holds_again_and_names_only_the_new_one(self):
        self.prompt()
        first = self.book("u1", "the work reaches GitHub")
        self.assertTrue(self.held(self.stop()))
        self.prompt()
        second = self.book("u2", "the login page works")
        out = self.stop()
        self.assertTrue(self.held(out), out)
        self.assertIn(second, out["reason"])
        self.assertNotIn(first, out["reason"])

    def test_hold_a_concern_booked_after_the_turn_ended_holds_at_the_next_stop(self):
        self.prompt()
        self.assertFalse(self.held(self.stop()), "nothing booked yet: the meter is detached")
        self.book("u1", "the work reaches GitHub", at=stamp(5))
        self.prompt()
        self.assertTrue(self.held(self.stop()))

    def test_hold_quiet_with_no_concerns_another_sessions_or_an_old_one(self):
        self.prompt()
        self.assertFalse(self.held(self.stop()), "no concerns at all")
        self.book("u1", "someone else's ask", session="other")
        self.assertFalse(self.held(self.stop()), "another session's concern is not this turn's")
        self.book("u2", "asked yesterday", at=stamp(86400))
        self.assertFalse(self.held(self.stop()), "a concern from long before this turn")

    def test_hold_the_reply_that_asks_the_user_something_is_not_held(self):
        self.prompt()
        self.book()
        self.assertFalse(self.held(self.stop("Which branch should I push to?")))


class Reopen(Case):
    def test_hold_a_concern_said_again_after_it_was_held_holds_once_more(self):
        self.prompt()
        cid = self.book("u1", "the work reaches GitHub")
        self.assertTrue(self.held(self.stop()))
        self.prompt()
        self.book("u2", "the work reaches GitHub", repeat_of="u1", text="I said the work must reach GitHub")
        out = self.stop()
        self.assertTrue(self.held(out), "said again: it holds once more")
        self.assertIn(cid, out["reason"])
        self.assertIn("said 2x", out["reason"])
        self.assertFalse(self.held(self.stop()), "but never twice for one saying")
        self.prompt()
        self.assertFalse(self.held(self.stop()), "nor in the next turn")

    def test_hold_a_concern_said_again_with_a_task_behind_it_is_released(self):
        self.prompt()
        self.book("u1", "the work reaches GitHub", at=stamp(30))
        c.log_event(self.p, "capture", task="T-0001", data={"source": "user", "type": "FEATURE"}, session="sess-1")
        self.assertFalse(self.held(self.stop()))
        self.prompt()
        self.book("u2", "the work reaches GitHub", repeat_of="u1", text="I said the work must reach GitHub")
        self.assertFalse(self.held(self.stop()), "its task is still behind it")


    def test_hold_a_concern_first_said_before_this_turn_and_said_again_names_its_first_id(self):
        cid = self.book("u1", "the work reaches GitHub", at=stamp(3 * 3600))
        self.prompt()
        self.book("u2", "the work reaches GitHub", repeat_of="u1", text="I said the work must reach GitHub")
        out = self.stop()
        self.assertTrue(self.held(out), out)
        self.assertIn(cid, out["reason"])
        self.assertIn("said 2x", out["reason"])
        self.assertFalse(self.held(self.stop()), "one saying holds once, and its next Stop reads no whole book")


class Cost(Case):
    """The Stop hook runs in every session, so the hold reads this turn's tail of the book and the ledger, not the 30
    days of either (T-4202 review: 5,000 rows and 50,000 ledger lines were read at every Stop)."""
    ROWS, EVENTS, BUDGET = 5000, 50000, 64 * 1024

    def setUp(self):
        super().setUp()
        now = time.time()
        rows, events = [], []
        for i in range(self.ROWS):  # oldest first, spread over 30 days, none of them this session's
            at = datetime.datetime.fromtimestamp(now - 30 * 86400 + i * (30 * 86400 - 3600) / self.ROWS,
                                                 datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            rows.append(json.dumps({"session": f"other-{i % 40}", "uuid": f"o{i}", "at": at, "model": "fake",
                                    "text": f"an ask number {i}, " + "words " * 12, "kind": "steer", "avoidable": True,
                                    "root": "R7", "missed_thought": "check the push", "why": "said done",
                                    "concern": f"concern number {i} of the project", "surface": "the push",
                                    "repeat_of": None}))
        for i in range(self.EVENTS):
            at = datetime.datetime.fromtimestamp(now - 30 * 86400 + i * (30 * 86400 - 3600) / self.EVENTS,
                                                 datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            events.append(json.dumps({"ts": at, "event": "capture", "task": f"T-{i:04d}", "session_id": f"other-{i % 40}",
                                      "data": {"source": "user", "type": "FIX"}}))
        with open(os.path.join(self.p.dir, "steers.jsonl"), "a", encoding="utf-8") as f:
            f.write("\n".join(rows) + "\n")
        with open(os.path.join(self.p.dir, "ledger.jsonl"), "a", encoding="utf-8") as f:
            f.write("\n".join(events) + "\n")

    def decide(self, session="sess-1", calls=1):
        """The hold's decision (what the Stop hook calls) `calls` times, each with a fresh gate record: (reason of the
        last call, bytes read from the project's files in total, ms per call)."""
        import builtins
        from unittest import mock
        from sampo import hooks
        counted, real = [0], builtins.open

        class Counting:
            def __init__(self, f):
                self.f = f

            def __enter__(self):
                self.f.__enter__()
                return self

            def __exit__(self, *a):
                return self.f.__exit__(*a)

            def __iter__(self):
                for line in self.f:
                    counted[0] += len(line)
                    yield line

            def read(self, *a):
                got = self.f.read(*a)
                counted[0] += len(got)
                return got

            def __getattr__(self, name):
                return getattr(self.f, name)

        def opening(path, *a, **k):
            f = real(path, *a, **k)
            if isinstance(path, str) and path.startswith(self.p.dir) and "jsonl" in path:
                return Counting(f)
            return f

        sd = {"panic": False, "drive": True, "paused": False, "autonomy": "standard"}
        pl = {"session_id": session, "last_assistant_message": "Done, the tests pass."}
        reason, t0 = None, time.perf_counter()
        with mock.patch("builtins.open", opening):
            for _ in range(calls):
                g = {"drive": {session: {"count": 0, "turn_at": time.time() - 60}}}
                reason = hooks._concern_hold(self.p, sd, [], pl, g)
        ms = (time.perf_counter() - t0) * 1000 / calls
        if os.environ.get("HOLD_BENCH"):
            with open(os.environ["HOLD_BENCH"], "a") as out:  # the path to append the numbers to
                out.write(f"{self._testMethodName}: {ms:.1f} ms/call, {counted[0] // calls} bytes/call\n")
        return reason, counted[0] // calls, ms

    def test_hold_reads_only_this_turns_tail_of_a_large_book_and_ledger(self):
        reason, read, _ = self.decide(calls=20)
        self.assertIsNone(reason, "no row of this session: nothing to hold")
        self.assertLess(read, self.BUDGET, f"nothing booked this turn: {read} bytes read per call")
        self.book("u1", "the work reaches GitHub", at=stamp(30))
        reason, read, _ = self.decide(calls=20)
        self.assertIn("the work reaches GitHub", reason or "", "a fresh concern of this session still holds")
        self.assertLess(read, self.BUDGET, f"a fresh concern: {read} bytes read per call")

    def test_hold_another_sessions_fresh_concern_does_not_hold_this_one_on_a_large_book(self):
        self.book("u1", "someone else's ask", session="other-3", at=stamp(30))
        reason, read, _ = self.decide()
        self.assertIsNone(reason)
        self.assertLess(read, self.BUDGET)


class Release(Case):
    def test_release_a_task_made_from_the_message_lets_it_go(self):
        self.prompt()
        self.book()
        c.log_event(self.p, "capture", task="T-0001", data={"source": "user", "type": "FEATURE"}, session="sess-1")
        self.assertFalse(self.held(self.stop()))

    def test_release_a_proof_lets_it_go(self):
        self.prompt()
        cid = self.book()
        cn.show(self.p, cid, "path:README.md")
        self.assertFalse(self.held(self.stop()))

    def test_release_an_answer_lets_it_go(self):
        self.prompt()
        cid = self.book()
        cn.answer(self.p, cid, "it is on main since 10:42")
        self.assertFalse(self.held(self.stop()))

    def test_release_a_drive_that_is_off_never_holds(self):
        self.fm("drive", "off")
        self.prompt()
        self.book()
        self.assertFalse(self.held(self.stop()))
        self.fm("drive", "on")
        self.assertTrue(self.held(self.stop()), "the hold was not spent while the drive was off")

    def test_release_a_paused_machine_never_holds(self):
        self.fm("pause", "on")
        self.prompt()
        self.book()
        self.assertFalse(self.held(self.stop()))

    def test_release_the_hooks_load_concerns_only_in_the_branch_that_needs_it(self):
        from sampo import hooks
        self.assertFalse(hasattr(hooks, "concerns"), "no module-level import on the hooks' path (the hook bench)")
