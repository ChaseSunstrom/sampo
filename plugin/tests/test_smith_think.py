"""T-4290: thinking off where a call needs none. Every loop call goes through smith.complete_retrying, which decides the
thinking its first try starts with: off under --reason off (every call of the run), off for a local model's small call
(max_tokens at or under 1024, no think named), and off for a size that a call exhausted its budget thinking on, in this
run or in an earlier run of the same model (kept per model in state). models.complete is faked: each call records the
think value it started with, and nothing calls a real model."""
import json
import os
import tempfile
import types
import unittest
from unittest import mock

import helpers  # noqa: F401 (puts plugin/lib on the import path before sampo)
from sampo import core, models, smith

LOCAL = {"name": "qwen-local", "kind": "openai", "base_url": "http://127.0.0.1:11434/v1", "model": "qwen"}
OTHER = {"name": "other-local", "kind": "openai", "base_url": "http://localhost:8000/v1", "model": "other"}
CLOUD = {"name": "cloud", "kind": "openai", "base_url": "https://api.example.com/v1", "model": "big"}
CLI = {"name": "claude", "kind": "cli", "base_url": "http://127.0.0.1:11434/v1"}
MSG = [{"role": "user", "content": "the question"}]
TEXT = {"text": "{}", "tool_calls": [], "usage": {}, "stop": "stop"}


def spent():
    """The reply of a call whose whole output budget went to thinking (models.complete's own error)."""
    return models.ModelError("qwen-local: reasoning used the whole budget: no text", reasoning=True)


class Fake:
    """models.complete stand-in: each call records the think it started with ("unset": the profile's own), then
    gives the next scripted reply, or raises the next scripted error."""

    def __init__(self, *script):
        self.script, self.thinks = list(script), []

    def __call__(self, profile, messages, **kw):
        self.thinks.append(kw.get("think", "unset"))
        got = self.script.pop(0) if self.script else TEXT
        if isinstance(got, Exception):
            raise got
        return got


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        self.state = os.path.join(tmp.name, "state")  # a fresh Sampo state for each test: no real state is written
        patch = mock.patch.dict(os.environ, {"FOREMAN_STATE": self.state})
        patch.start()
        self.addCleanup(patch.stop)
        self.notes = []

    def fresh(self, reason=None):
        """A fresh Run (one run of the loop), its notes kept in self.notes."""
        run = smith.Run(types.SimpleNamespace(escalate=None, max_steps=None, reason=reason), self.root, self.root)
        run.emit = lambda event, text, quiet=False, **data: self.notes.append(text) if event == "note" else None
        return run

    def calls(self, fake, run, profile, *sizes, **kw):
        """complete_retrying for each size (max_tokens), with fake as models.complete: the thinking each call started with."""
        start = len(fake.thinks)
        with mock.patch.object(models, "complete", fake):
            for size in sizes:
                smith.complete_retrying(run, profile, MSG, **dict(kw, max_tokens=size))
        return fake.thinks[start:]


class ArmOff(Base):
    def test_arm_off_no_model_call_of_the_run_starts_with_thinking_on(self):
        # the type and plan calls (8192 and 600), a plan repair, the findings (1024), a turn with its caller's think on,
        # a cloud and a CLI profile: none starts on under --reason off
        run = self.fresh("off")
        fake = Fake()
        with mock.patch.object(models, "complete", fake):
            for profile in (LOCAL, CLOUD, CLI):
                for size in (8192, 600, 1024, 60):
                    smith.complete_retrying(run, profile, MSG, max_tokens=size)
            smith.complete_retrying(run, LOCAL, MSG, max_tokens=8192, think=True)
            smith.complete_retrying(run, LOCAL, MSG, max_tokens=60, schema={"type": "object"})
        self.assertEqual(set(fake.thinks), {False}, fake.thinks)
        self.assertEqual(len(fake.thinks), 3 * 4 + 2)

    def test_arm_off_through_the_slot_path_a_plan_starts_with_thinking_off(self):
        # the plan and findings slots reach models.complete through _ask, the way a run's first plan does
        run = self.fresh("off")
        fake = Fake()
        with mock.patch.object(models, "complete", fake), mock.patch.object(smith, "fit", lambda r, p, msgs: msgs):
            smith._ask(run, LOCAL, MSG, "plan")
        self.assertEqual(fake.thinks, [False])

    def test_without_the_arm_a_large_call_is_left_as_the_profile_has_it(self):
        self.assertEqual(self.calls(Fake(), self.fresh(), LOCAL, 8192), ["unset"])


class SmallCall(Base):
    def test_small_call_of_a_local_model_starts_with_thinking_off_in_every_arm(self):
        for reason in (None, "state", "native", "budget", "bias", "schema"):
            with self.subTest(reason=reason):
                self.assertEqual(self.calls(Fake(), self.fresh(reason), LOCAL, 60, 600, 1024), [False, False, False])

    def test_a_call_over_1024_is_left_as_it_was(self):
        self.assertEqual(self.calls(Fake(), self.fresh(), LOCAL, 1025), ["unset"])

    def test_a_callers_think_or_think_budget_is_kept(self):
        self.assertEqual(self.calls(Fake(), self.fresh(), LOCAL, 600, think=True), [True])
        self.assertEqual(self.calls(Fake(), self.fresh(), LOCAL, 600, think_budget=512), ["unset"])

    def test_a_cloud_or_cli_profile_is_unchanged(self):
        self.assertEqual(self.calls(Fake(), self.fresh(), CLOUD, 600), ["unset"])
        self.assertEqual(self.calls(Fake(), self.fresh(), CLI, 600), ["unset"])


class Learned(Base):
    def test_learned_after_one_exhausted_call_later_calls_of_its_size_or_smaller_start_off(self):
        run, fake = self.fresh(), Fake(spent(), TEXT, TEXT, TEXT, TEXT)
        self.assertEqual(self.calls(fake, run, LOCAL, 8192), ["unset", False], "asked again with thinking off")
        self.assertEqual(self.calls(fake, run, LOCAL, 8192, 4096), [False, False])
        self.assertEqual(self.calls(fake, run, LOCAL, 8193), ["unset"], "a larger call is not learned from this")

    def test_learned_the_count_is_kept_per_model_and_the_next_run_starts_there_with_one_note(self):
        self.calls(Fake(spent(), TEXT), self.fresh(), LOCAL, 8192)
        with open(os.path.join(core.state_dir(), smith.THINK_FILE), encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"qwen-local": {"8192": 1}})
        nxt = self.fresh()
        self.assertEqual(self.calls(Fake(TEXT, TEXT), nxt, LOCAL, 8192, 600), [False, False])
        self.assertEqual(len([n for n in self.notes if "earlier run" in n]), 1, self.notes)

    def test_learned_a_different_model_starts_clean(self):
        self.calls(Fake(spent(), TEXT), self.fresh(), LOCAL, 8192)
        self.assertEqual(self.calls(Fake(), self.fresh(), OTHER, 8192), ["unset"])

    def test_learned_a_cloud_call_that_exhausts_teaches_nothing(self):
        self.calls(Fake(spent(), TEXT), self.fresh(), CLOUD, 8192)
        self.assertFalse(os.path.exists(os.path.join(core.state_dir(), smith.THINK_FILE)))
        self.assertEqual(self.calls(Fake(), self.fresh(), CLOUD, 8192), ["unset"])

    def test_learned_a_call_the_caller_asked_to_think_teaches_nothing(self):
        # review of T-4290: an explicit think=True call (a deliberate, large one) that runs out is the caller's choice;
        # it must not turn thinking off for every default call of that size or smaller, here and in later runs
        run, fake = self.fresh(), Fake(spent(), TEXT, TEXT)
        self.assertEqual(self.calls(fake, run, LOCAL, 16384, think=True), [True, False])
        self.assertFalse(os.path.exists(os.path.join(core.state_dir(), smith.THINK_FILE)))
        self.assertEqual(self.calls(fake, run, LOCAL, 8192), ["unset"])

    def test_learned_an_unreadable_state_file_is_no_count(self):
        os.makedirs(core.state_dir(), exist_ok=True)
        with open(os.path.join(core.state_dir(), smith.THINK_FILE), "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertEqual(self.calls(Fake(), self.fresh(), LOCAL, 8192), ["unset"])


if __name__ == "__main__":
    unittest.main()
