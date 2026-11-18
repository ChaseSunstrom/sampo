"""T-3126 (T-3123 part 1): a message that points something out is a miss, logged with its class; `sampo misses` counts
them by class and per day. Fixtures are the user's own words from the evening of 2026-11-05."""
import json

from helpers import ForemanTestCase

from sampo import core as c

MISSES = [
    ("stalled", "why is the graph look or whatever on the local live site not done yet"),
    ("unfiled", "are you also keeping track and fixing all of these slight bugs you come across while using sampo too?"),
    ("unmeasured", "how can we improve the speed/efficiency of the merge queue too?"),
    ("unused", "have you tried using the local models for tasks with you? like the qwen 3.8 27b model"),
    ("invisible", "how can I view all of the cards?"),
    ("other", "think about how to make yourself more self aware and capture things like that in the future so I "
              "don't need to point them out"),
]
NOT_MISSES = ["and also have UI examples of each of the UI cards used and such?", "thanks", "Thanks!",
              "add a dark theme to the settings page", "keep it to no more than 5 retries",
              "there's only 1 failing test left"]


CLOSED = ["there's only 26 tower ideas, but how come there's not more than that?", "we might want more than this",
          "why only 12 cards?", "there’s only 26 tower ideas, but how come there’s not more than that?",
          "why is there only one tower?", "only a few ideas, why?", "why so few"]


class Classify(ForemanTestCase):
    def test_closed_set_steers_get_the_closed_class(self):
        from sampo import selfaware
        self.assertIn("closed", selfaware.CLASSES)
        for text in CLOSED:
            self.assertEqual(selfaware.classify(text), "closed", text)

    def test_a_long_one_line_paste_is_classified_fast(self):
        """T-3327 review: the closed pattern's lookahead re-scanned the line from every start, so a big one-line paste
        (minified JSON) cost its length squared in the prompt hook; only the message's start is read now."""
        import time
        from sampo import selfaware
        blob = '{"k": "' + "x " * 30000 + '"}'  # 60 KB, one line, no trigger word
        started = time.monotonic()
        self.assertIsNone(selfaware.classify(blob))
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(selfaware.classify("why only 12 cards? " + "x " * 30000), "closed")  # a steer at the start still counts

    def test_each_of_tonights_six_gets_its_class_and_requests_and_thanks_get_none(self):
        from sampo import selfaware
        for cls, text in MISSES:
            self.assertEqual(selfaware.classify(text), cls, text)
        for text in NOT_MISSES:
            self.assertIsNone(selfaware.classify(text), text)

    def test_a_model_is_asked_only_when_no_pattern_matches_and_its_label_must_be_a_class(self):
        from sampo import selfaware
        asked = []
        ask = lambda text: asked.append(text) or "unused"
        self.assertEqual(selfaware.classify(MISSES[0][1], ask), "stalled")
        self.assertEqual(asked, [])  # a pattern matched: no model
        self.assertEqual(selfaware.classify("did you look at the gpu box lately", ask), "unused")
        self.assertEqual(len(asked), 1)
        self.assertIsNone(selfaware.classify("did you look at it", lambda t: "a made-up label"))
        self.assertIsNone(selfaware.classify("thanks", ask))  # a thanks never reaches the model
        self.assertEqual(len(asked), 1)


class Ledger(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo, create=False)

    def test_note_logs_a_miss_with_its_class_and_misses_prints_counts_by_class_and_day(self):
        from sampo import selfaware
        for cls, text in MISSES:
            self.assertEqual(selfaware.note(self.p, text, session="s1"), cls)
        self.assertIsNone(selfaware.note(self.p, "thanks", session="s1"))
        rows = c.ledger_events(self.p, ["miss"])
        self.assertEqual([r["data"]["cls"] for r in rows], [m[0] for m in MISSES])
        out = self.fm("misses").stdout
        for cls, _ in MISSES:
            self.assertIn(cls, out)
        self.assertIn(rows[0]["ts"][:10], out)
        self.assertIn("6", out)
        data = json.loads(self.fm("misses", "--json").stdout)
        self.assertEqual(data["total"], 6)
        self.assertEqual(data["by_class"]["stalled"], 1)
        self.assertEqual(data["by_day"][rows[0]["ts"][:10]], 6)

    def test_the_prompt_hook_logs_a_miss_and_not_a_request(self):
        from sampo import hooks
        for text in (MISSES[4][1], NOT_MISSES[0]):
            hooks.user_prompt_submit({"session_id": "s2", "cwd": self.repo, "prompt": text})
        rows = c.ledger_events(self.p, ["miss"])
        self.assertEqual([r["data"]["cls"] for r in rows], ["invisible"])


class Boundary(ForemanTestCase):
    """T-3128 (T-3123 part 3): at a task boundary the predicted asks no open task covers become 'predicted' captures;
    a later user message that matches one is a catch."""
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo, create=False)
        from sampo import selfaware
        self.sa = selfaware
        self._orig = selfaware.predicted_asks
        selfaware.predicted_asks = lambda p: [
            ("FEATURE", "dashboard chart for merge queue wait times"), ("FIX", "retry flaky deploy script"),
            ("CLEAN", "remove stale lanes folder"), ("PERF", "faster ledger scan"), ]
        self.addCleanup(setattr, selfaware, "predicted_asks", self._orig)

    def _predicted(self):
        return [b for b in c.load_briefs(self.p) if b.meta.get("source") == "predicted"]

    def test_a_boundary_files_at_most_three_predicted_captures(self):
        self.assertEqual(len(self.sa.boundary(self.p)), 3)
        got = self._predicted()
        self.assertEqual(len(got), 3)
        self.assertTrue(all(b.status == "captured" for b in got))

    def test_an_ask_an_open_task_covers_or_already_filed_is_not_filed_again(self):
        self.fm("capture", "FEATURE: dashboard chart for merge queue wait times")
        self.sa.boundary(self.p)
        titles = [b.title for b in self._predicted()]
        self.assertFalse(any("dashboard chart" in t for t in titles), titles)
        n = len(self._predicted())
        self.sa.boundary(self.p)  # a second boundary files nothing new from the same guesses beyond the cap's next
        self.assertEqual(len({b.title for b in self._predicted()}), len(self._predicted()))
        self.assertGreaterEqual(len(self._predicted()), n)

    def test_a_later_matching_user_request_records_a_catch_and_misses_shows_it(self):
        self.sa.boundary(self.p)
        self.assertEqual(self.sa.catch(self.p, "add a dashboard chart of the merge queue wait times please"), [self._predicted()[0].id])
        self.assertEqual(self.sa.catch(self.p, "rename the settings page"), [])
        self.assertEqual(len(c.ledger_events(self.p, ["predicted_catch"])), 1)
        self.assertEqual(self.sa.catch(self.p, "add a dashboard chart of the merge queue wait times"), [])  # once each
        self.assertIn("1 catch", self.fm("misses").stdout)

    def test_a_dropped_guess_is_not_refiled_and_open_predicted_captures_are_capped_at_three(self):
        self.sa.boundary(self.p)
        self.assertEqual(self.sa.boundary(self.p), [])  # three are open: the cap is full, the 4th guess waits
        for b in self._predicted():
            self.fm("task", "drop", b.id, "--reason", "not wanted")
        self.assertEqual(len(self.sa.boundary(self.p)), 1)  # only the never-filed 4th guess; the dropped three stay dropped
        self.assertEqual(len(self._predicted()), 4)
        self.assertEqual(self.sa.boundary(self.p), [])

    def test_a_short_guess_neither_catches_nor_counts_as_covered(self):
        self.sa.predicted_asks = lambda p: [("FIX", "work")]
        self.fm("capture", "FIX: work through the backlog of stale branches")
        self.assertEqual(len(self.sa.boundary(self.p)), 1)  # one word never counts as covered
        self.assertEqual(self.sa.catch(self.p, "work on the settings page"), [])
        self.sa.predicted_asks = lambda p: [("FIX", "retry flaky deploy script")]
        self.sa.boundary(self.p)
        self.assertEqual(self.sa.catch(self.p, "do the retry work"), [])  # one matched word of four
        self.assertEqual(len(self.sa.catch(self.p, "retry the flaky deploy")), 1)
