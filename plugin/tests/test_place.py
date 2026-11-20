"""T-5011: place_capture says where a capture belongs: new, join, merge or duplicate, with the task, the reason, the backend
and the probability. The word-overlap rule answers below the bar; a model that clears the bar replaces it, one that does
not (or none) leaves it. models.decide is replaced by a stub here: it is the one place a model is asked."""
import json
import os
from unittest import mock

from helpers import ForemanTestCase

from sampo import core as c
from sampo import foresight
from sampo import models
from sampo import place

KEYS = {"where", "target", "reason", "backend", "probability"}


class Stub:
    """models.decide's stand-in: answers (choice, probability, cleared); counts the questions and keeps the last."""

    def __init__(self, choice=None, probability=0.9, backend="local-a", fail=False):
        self.choice, self.probability, self.backend, self.fail = choice, probability, backend, fail
        self.calls = []

    def __call__(self, kind, prompt, choices, rule=None, bar=0.8, **kw):
        self.calls.append({"kind": kind, "prompt": prompt, "choices": choices, "rule": rule(prompt), "kw": kw})
        if self.fail:
            raise RuntimeError("backend down")
        if self.choice is None:
            return None
        return {"choice": self.choice, "probability": self.probability, "backend": self.backend,
                "cleared": self.probability >= bar, "tried": []}


class Place(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.fm("task", "new", "Add the parser cache layer", "--type", "FEATURE", "--tier", "S", "--ac", "works :: true",
                "--step", "do it")  # T-0001, open, FEATURE
        self.fm("task", "new", "Fix the importer retry loop", "--type", "FIX", "--tier", "S", "--ac", "works :: true",
                "--step", "do it")  # T-0002, open, FIX
        self.fm("task", "new", "Render the settings dialog", "--type", "FEATURE", "--tier", "S", "--ac", "works :: true",
                "--step", "do it", "--scope", "plugin/lib/sampo/dialog.py")  # T-0003, open, scoped
        self.fm("task", "new", "Rename the export column headers", "--type", "FEATURE", "--tier", "S", "--ac",
                "works :: true", "--step", "do it")  # T-0004, done below
        self.p = c.find_project(self.repo)
        b = c.find_brief(self.p, "T-0004")
        b.meta["status"] = "done"
        c.save_brief(self.p, b)

    def place(self, text, type_="FEATURE", scope=(), stub=None, live=True, **kw):
        with mock.patch.object(models, "decide", stub or Stub()), mock.patch.object(models, "live", return_value=live):
            return place.place_capture(self.p, text, type_, scope, backends=[], **kw)

    def test_a_verdict_has_the_five_fields_and_a_reason(self):
        for text, type_ in (("Add the parser cache layer", "FEATURE"), ("Something nobody asked yet", "FEATURE"),
                            ("Rename the export column headers", "FEATURE"), ("Fix the importer retry loop", "FEATURE")):
            got = self.place(text, type_)
            self.assertEqual(set(got), KEYS)
            self.assertIn(got["where"], ("new", "join", "merge", "duplicate"))
            self.assertTrue(got["reason"] and "\n" not in got["reason"])
            self.assertEqual(got["backend"], "rule")
            self.assertTrue(0 <= got["probability"] <= 1)

    def test_the_rule_names_each_verdict_and_stays_under_the_bar(self):
        merge = self.place("Add the parser cache layer", "FEATURE")
        self.assertEqual((merge["where"], merge["target"]), ("merge", "T-0001"))
        self.assertIn("T-0001", merge["reason"])
        dup = self.place("Rename the export column headers", "FEATURE")
        self.assertEqual((dup["where"], dup["target"]), ("duplicate", "T-0004"))
        join = self.place("Fix the importer retry loop", "FEATURE")  # an open task of another type
        self.assertEqual((join["where"], join["target"]), ("join", "T-0002"))
        new = self.place("Something nobody asked yet", "FEATURE")
        self.assertEqual((new["where"], new["target"]), ("new", None))
        for got in (merge, dup, join, new):
            self.assertLess(got["probability"], place.BAR)

    def test_a_file_in_an_open_tasks_scope_joins_it(self):
        got = self.place("The cancel button misaligns in plugin/lib/sampo/dialog.py", "FIX")
        self.assertEqual((got["where"], got["target"]), ("join", "T-0003"))
        self.assertIn("plugin/lib/sampo/dialog.py", got["reason"])

    def test_a_deferred_task_is_not_joined_by_scope(self):
        b = c.find_brief(self.p, "T-0003")
        b.meta["status"] = "deferred"
        c.save_brief(self.p, b)
        got = self.place("The cancel button misaligns in plugin/lib/sampo/dialog.py", "FIX")
        self.assertEqual(got["where"], "new")

    def test_nothing_to_land_on_asks_no_model(self):
        stub = Stub("merge")
        got = self.place("Something nobody asked yet", stub=stub)
        self.assertEqual((got["where"], stub.calls), ("new", []))

    def test_a_model_over_the_bar_replaces_the_rule(self):
        stub = Stub("new", 0.93)
        got = self.place("Add the parser cache layer", stub=stub)
        self.assertEqual((got["where"], got["target"], got["backend"], got["probability"]), ("new", None, "local-a", 0.93))
        self.assertIn("local-a", got["reason"])
        call = stub.calls[0]
        self.assertEqual((call["kind"], call["rule"][0]), ("capture-place", "merge"))
        self.assertEqual(set(call["choices"]), {"new", "merge"})  # only verbs with a task to land on
        self.assertLess(call["rule"][1], place.BAR)
        join = self.place("Add the parser cache layer", stub=Stub("merge", 0.9))
        self.assertEqual((join["where"], join["target"], join["backend"]), ("merge", "T-0001", "local-a"))
        self.assertIn("T-0001", join["reason"])

    def test_a_model_under_the_bar_or_down_leaves_the_rule(self):
        for stub in (Stub(None), Stub(fail=True)):
            got = self.place("Add the parser cache layer", stub=stub)
            self.assertEqual((got["where"], got["target"], got["backend"]), ("merge", "T-0001", "rule"))

    def test_a_model_not_yet_proven_live_is_shadow_only(self):
        got = self.place("Add the parser cache layer", stub=Stub("new", 0.95), live=False)
        self.assertEqual((got["where"], got["target"], got["backend"]), ("merge", "T-0001", "rule"))
        self.assertIn("shadow", got["reason"])

    def real(self, reply, live=True):
        prof = {"name": "local-a", "profile": {"kind": "chat"}, "local": True}
        with mock.patch.object(models, "complete", return_value={"text": json.dumps(reply)}), \
                mock.patch.object(models, "live", return_value=live):
            return place.place_capture(self.p, "Add the parser cache layer", "FEATURE", (), backends=[prof])

    def test_the_real_decide_takes_a_reply_over_the_bar_and_names_an_under_bar_one(self):
        got = self.real({"choice": "new", "probability": 0.93})
        self.assertEqual((got["where"], got["target"], got["backend"], got["probability"]), ("new", None, "local-a", 0.93))
        got = self.real({"choice": "new", "probability": 0.75})
        self.assertEqual((got["where"], got["backend"]), ("merge", "rule"))
        self.assertIn("under the 80% bar", got["reason"])
        self.assertEqual(self.real({"choice": "new", "probability": 0.5})["where"], "merge")  # under the rule's own
        self.assertEqual(self.real({"choice": "duplicate", "probability": 0.99})["where"], "merge")  # not a listed verb

    def test_an_asleep_task_is_never_a_merge_target(self):
        b = c.find_brief(self.p, "T-0001")
        b.meta["status"] = "deferred"
        c.save_brief(self.p, b)
        got = self.place("Add the parser cache layer")
        self.assertEqual((got["where"], got["target"]), ("new", None))

    def test_the_wait_is_one_total_deadline_across_backends(self):
        clock = [0.0]
        seen = []

        def slow(kind, prompt, choices, rule=None, bar=0.8, timeout=None, **kw):
            seen.append(timeout)
            clock[0] += 5
            return None
        bs = [{"name": n, "profile": {}, "local": True} for n in "abc"]
        with mock.patch.object(models, "decide", slow), mock.patch.object(place.time, "monotonic", lambda: clock[0]):
            place.place_capture(self.p, "Add the parser cache layer", "FEATURE", (), backends=bs)
        self.assertEqual(seen, [place.TIMEOUT, place.TIMEOUT - 5])

    def test_only_the_users_own_and_loopback_backends_are_asked_at_most_backends(self):
        reg = [{"name": "cloud", "profile": {}, "local": False, "own": False},
               {"name": "loop", "profile": {}, "local": True, "own": False},
               {"name": "mine", "profile": {}, "local": False, "own": True},
               {"name": "loop2", "profile": {}, "local": True, "own": False}]
        asked = []

        def rec(kind, prompt, choices, rule=None, bar=0.8, backends=None, **kw):
            asked.extend(b["name"] for b in backends)
        with mock.patch.object(models, "registry", return_value=reg), mock.patch.object(models, "decide", rec):
            place.place_capture(self.p, "Add the parser cache layer", "FEATURE", ())
        self.assertEqual(asked, ["loop", "mine"])
        self.assertEqual(len(asked), place.BACKENDS)

    def test_the_model_reads_a_redacted_one_line_title_and_a_cut_text(self):
        stub = Stub("merge", 0.9)
        self.place("Add the parser cache layer\nkey sk-ant-api03-" + "a" * 40 + " " + "x" * 5000, stub=stub)
        prompt = stub.calls[0]["prompt"]
        self.assertLess(len(prompt), 1500)
        self.assertIn("Add the parser cache layer", prompt)
        self.assertNotIn("sk-ant-api03", prompt)

    def test_with_no_backend_decide_answers_by_the_rule_and_books_it(self):
        got = place.place_capture(self.p, "Add the parser cache layer", "FEATURE", (), backends=[])
        self.assertEqual((got["where"], got["target"], got["backend"]), ("merge", "T-0001", "rule"))
        with open(os.path.join(self.p.dir, foresight.PREDICTIONS), encoding="utf-8") as f:
            rows = [json.loads(x) for x in f if x.strip()]
        self.assertEqual([r["use"] for r in rows if r.get("kind") == "decide"], ["capture-place"])

    def test_empty_text_is_new_and_a_second_call_is_the_same(self):
        self.assertEqual(self.place("")["where"], "new")
        a = self.place("Add the parser cache layer")
        self.assertEqual(a, self.place("Add the parser cache layer"))
