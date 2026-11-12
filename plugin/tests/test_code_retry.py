"""T-4205: a transient model failure (a read or connect timeout, a dropped connection, a 502/503/529) is tried again
with a longer timeout before fm code's run stops; a 4xx is never retried. In-process with the model stubbed: no
real stall, no real wait (the wait is injected)."""
import os
import socket
import tempfile
import unittest
from unittest import mock

import helpers  # noqa: F401 (puts plugin/lib on the import path)
from sampo import models, smith

MSG = [{"role": "user", "content": "fix calc.add"}]
OK = {"text": "ok", "tool_calls": [], "stop": "stop", "usage": {}}
PROFILE = {"name": "cloud", "kind": "openai", "base_url": "https://api.example.com/v1", "model": "m1"}
LOCAL = dict(PROFILE, name="local", base_url="http://127.0.0.1:8080/v1")
BIG = smith.SMALL_OUT * 4  # T-4290: a local call of at most SMALL_OUT starts with thinking off; these calls think


class Run:
    """The parts of fm code's run the retry helper touches."""

    def __init__(self):
        self.notes, self.logs = [], []
        self.root, self.unlean = None, None
        # T-4290: what think_start reads; T-4297: with no --reason a local call starts with thinking off, so these ask for native
        self.reason, self.think_floor, self.think_noted = "native", {}, False

    def check_cap(self, what):
        pass

    def usage(self, r, profile, what):
        pass

    def capped(self, profile):
        return profile

    def emit(self, event, text, quiet=False, **data):
        self.notes.append((event, text))

    def log(self, **data):
        self.logs.append(data)


def fail(status=None, transient=False):
    return models.ModelError("read timed out" if transient else "HTTP", status=status, transient=transient)


class Retry(unittest.TestCase):
    def setUp(self):
        self.run = Run()
        state = tempfile.TemporaryDirectory()  # T-4290: an exhausted local call is counted in the state; never the real one
        self.addCleanup(state.cleanup)
        mock.patch.dict(os.environ, {"FOREMAN_STATE": state.name}).start()
        self.wait = mock.patch.object(smith, "_wait").start()
        self.addCleanup(mock.patch.stopall)

    def complete(self, profile, *results):
        """Calls the helper with models.complete answering each result in turn (an exception is raised)."""
        with mock.patch.object(models, "complete", side_effect=list(results)) as mc:
            try:
                out = smith.complete_retrying(self.run, profile, MSG, max_tokens=BIG)
            except models.ModelError as e:
                out = e
        return out, [c.kwargs.get("timeout") for c in mc.call_args_list]

    def test_a_timeout_once_is_retried_with_a_longer_timeout_and_a_note_says_so(self):
        out, timeouts = self.complete(PROFILE, fail(transient=True), OK)
        self.assertIs(out, OK)
        self.assertEqual(timeouts, [120, 300])
        self.assertEqual(self.wait.call_count, 1)
        self.assertTrue(any("timed out" in text and "300 s" in text for _, text in self.run.notes), self.run.notes)
        self.assertEqual(self.run.logs[0]["kind"], "retry")

    def test_a_local_profile_waits_longer_from_the_first_try(self):
        out, timeouts = self.complete(LOCAL, fail(transient=True), OK)
        self.assertIs(out, OK)
        self.assertEqual(timeouts, [300, 600])

    def test_a_cloud_timeout_twice_stops_after_one_retry(self):  # each retry re-bills the prompt
        err = fail(transient=True)
        out, timeouts = self.complete(PROFILE, err, err, OK)
        self.assertIs(out, err)
        self.assertEqual(timeouts, [120, 300])

    def test_a_local_timeout_after_three_tries_stops_with_the_last_error(self):
        err = fail(transient=True)
        out, timeouts = self.complete(LOCAL, err, err, err, OK)
        self.assertIs(out, err)
        self.assertEqual(timeouts, [300, 600, 900])
        self.assertEqual(self.wait.call_count, 2)

    def test_the_planners_first_call_is_retried_on_a_timeout(self):  # smith._ask: the planner's model call
        with mock.patch.object(smith, "fit", side_effect=lambda run, p, msgs: msgs), \
                mock.patch.object(models, "complete", side_effect=[fail(transient=True), OK]) as mc:
            out = smith._ask(self.run, PROFILE, MSG, "plan")
        self.assertIs(out, OK)
        self.assertEqual([c.kwargs.get("timeout") for c in mc.call_args_list], [120, 300])

    def test_a_transport_timeout_from_the_socket_is_transient(self):
        # the real wrapping in models._send: a read timeout from the opener becomes a ModelError marked transient
        with mock.patch.object(models, "_OPENER") as op:
            op.open.side_effect = socket.timeout("timed out")
            with self.assertRaises(models.ModelError) as cm:
                models.complete(dict(PROFILE, base_url="http://127.0.0.1:9/v1"), MSG, max_tokens=5)
        self.assertTrue(cm.exception.transient)
        self.assertIsNone(cm.exception.status)

    def test_a_502_503_or_529_is_retried_and_stops_at_the_end_of_the_ladder(self):
        for status in (502, 503, 529):
            with self.subTest(status=status):
                self.wait.reset_mock()
                err = fail(status=status)
                out, timeouts = self.complete(PROFILE, err, err)
                self.assertIs(out, err)
                self.assertEqual(timeouts, [120, 300])
                out, timeouts = self.complete(LOCAL, err, err, err, OK)
                self.assertIs(out, err)
                self.assertEqual(timeouts, [300, 600, 900])

    def test_a_4xx_is_never_retried_and_stops_at_once(self):
        for status in (400, 401, 404, 422):
            with self.subTest(status=status):
                self.wait.reset_mock()
                err = fail(status=status)
                out, timeouts = self.complete(PROFILE, err, OK)
                self.assertIs(out, err)
                self.assertEqual(timeouts, [120])
                self.assertEqual(self.wait.call_count, 0)

    def test_a_refused_request_stops_at_once(self):  # an error that is not transient: no retry
        err = fail()
        out, timeouts = self.complete(PROFILE, err, OK)
        self.assertIs(out, err)
        self.assertEqual(timeouts, [120])

    def test_a_reply_that_is_all_reasoning_is_retried_once_with_thinking_off_and_a_note_says_so(self):  # T-4210
        spent = models.ModelError("local: reasoning used the whole budget", reasoning=True)
        with mock.patch.object(models, "complete", side_effect=[spent, OK]) as mc:
            out = smith.complete_retrying(self.run, LOCAL, MSG, max_tokens=BIG)
        self.assertIs(out, OK)
        self.assertIs(mc.call_args_list[0].kwargs.get("think"), None)
        self.assertIs(mc.call_args_list[1].kwargs.get("think"), False)
        self.assertTrue(any("thinking off" in text for _, text in self.run.notes), self.run.notes)
        self.assertEqual(self.run.logs[0]["kind"], "retry")

    def test_a_second_all_reasoning_reply_stops_the_run_as_before(self):  # T-4210
        spent = models.ModelError("local: reasoning used the whole budget", reasoning=True)
        with mock.patch.object(models, "complete", side_effect=[spent, spent, OK]) as mc:
            with self.assertRaises(models.ModelError) as cm:
                smith.complete_retrying(self.run, LOCAL, MSG, max_tokens=BIG)
        self.assertIs(cm.exception, spent)
        self.assertEqual(mc.call_count, 2)
        self.assertEqual(self.wait.call_count, 0)

    def test_a_reasoning_failure_from_a_call_with_thinking_off_is_not_asked_again(self):  # T-4210
        spent = models.ModelError("local: reasoning used the whole budget", reasoning=True)
        with mock.patch.object(models, "complete", side_effect=[spent, OK]) as mc:
            with self.assertRaises(models.ModelError):
                smith.complete_retrying(self.run, LOCAL, MSG, max_tokens=BIG, think=False)
        self.assertEqual(mc.call_count, 1)

    def test_the_retry_of_an_all_reasoning_reply_is_written_from_its_own_notes(self):  # T-4358
        # live (exam s8-047): 8 of 12 plans thought for 250 to 530 s, were cut with no text, and were asked again from
        # nothing; the server had handed the reasoning back each time
        spent = models.ModelError("local: reasoning used the whole budget", reasoning=True,
                                  thought="app.c: slice_page drops a short last page; fix that, then test it")
        before = [dict(x) for x in MSG]
        with mock.patch.object(models, "complete", side_effect=[spent, OK]) as mc:
            out = smith.complete_retrying(self.run, LOCAL, MSG, max_tokens=BIG)
        self.assertIs(out, OK)
        self.assertEqual(MSG, before, "the caller's messages are never changed")
        first, second = (c.args[1] for c in mc.call_args_list)
        self.assertEqual(first, MSG)
        self.assertIn("slice_page drops a short last page", second[-1]["content"])
        self.assertTrue(second[-1]["content"].startswith(MSG[-1]["content"]), "the request still leads")
        self.assertIs(mc.call_args_list[1].kwargs.get("think"), False)
        note = next(text for _, text in self.run.notes if "thinking off" in text)
        self.assertIn("from its notes", note)
        self.assertEqual(self.run.logs[-1].get("notes"), len(spent.thought))

    def test_a_retry_with_no_notes_says_nothing_of_notes(self):  # T-4358: a server that hands no reasoning back
        spent = models.ModelError("local: reasoning used the whole budget", reasoning=True)
        with mock.patch.object(models, "complete", side_effect=[spent, OK]) as mc:
            smith.complete_retrying(self.run, LOCAL, MSG, max_tokens=BIG)
        self.assertEqual(mc.call_args_list[1].args[1], MSG)
        self.assertFalse(any("notes" in text for _, text in self.run.notes), self.run.notes)

    def test_an_all_reasoning_reply_with_thinking_already_off_is_asked_once_more_from_its_notes(self):  # T-4367
        # live (s8-048 goal-rust-ea4f63, goal-c-ab86e2): the call was sent with thinking off, the model reasoned all
        # the same, and the run ended on it; with its notes in the prompt the second ask has something to write from
        spent = models.ModelError("local: reasoning used the whole budget", reasoning=True, thought="so cargo test, then")
        with mock.patch.object(models, "complete", side_effect=[spent, OK]) as mc:
            out = smith.complete_retrying(self.run, LOCAL, MSG, max_tokens=BIG, think=False)
        self.assertIs(out, OK)
        self.assertEqual(mc.call_count, 2)
        self.assertIn("so cargo test, then", mc.call_args_list[1].args[1][-1]["content"])
        self.assertIs(mc.call_args_list[1].kwargs.get("think"), False)
        self.assertTrue(any("from its notes" in text for _, text in self.run.notes), self.run.notes)
        self.run.reason = "off"  # the arm that turns every call off reaches the same path
        with mock.patch.object(models, "complete", side_effect=[spent, OK]) as mc:
            self.assertIs(smith.complete_retrying(self.run, LOCAL, MSG, max_tokens=BIG), OK)
        self.assertEqual(mc.call_count, 2)
        with mock.patch.object(models, "complete", side_effect=[spent, spent, OK]) as mc:  # once, never a loop
            with self.assertRaises(models.ModelError):
                smith.complete_retrying(self.run, LOCAL, MSG, max_tokens=BIG, think=False)
        self.assertEqual(mc.call_count, 2)
        self.assertEqual(self.run.think_floor, {}, "a call sent with thinking off teaches the default nothing")

    def test_a_reasoning_failure_of_a_budgeted_call_is_not_repeated_with_thinking_off(self):  # T-4210: _reasoned closes it
        spent = models.ModelError("local: reasoning used the whole budget", reasoning=True)
        with mock.patch.object(models, "complete", side_effect=[spent, OK]) as mc:
            with self.assertRaises(models.ModelError):
                smith.complete_retrying(self.run, LOCAL, MSG, max_tokens=BIG, think=True, think_budget=64)
        self.assertEqual(mc.call_count, 1)

    def test_a_reasoning_retry_then_a_transient_failure_still_gets_the_timeout_ladder(self):  # T-4210
        spent = models.ModelError("local: reasoning used the whole budget", reasoning=True)
        err = fail(transient=True)
        out, timeouts = self.complete(LOCAL, spent, err, OK)
        self.assertIs(out, OK)
        self.assertEqual(timeouts, [300, 300, 600])


if __name__ == "__main__":
    unittest.main()
