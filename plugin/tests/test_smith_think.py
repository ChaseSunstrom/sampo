"""T-4290: thinking off where a call needs none. Every loop call goes through smith.complete_retrying, which decides the
thinking its first try starts with: off under --reason off (every call of the run), off for a local model's small call
(max_tokens at or under 1024, no think named), and off for a size that a call exhausted its budget thinking on, in this
run or in an earlier run of the same model (kept per model in state). models.complete is faked: each call records the
think value it started with, and nothing calls a real model."""
import json
import os
import tempfile
import time
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

    def test_under_reason_native_a_large_call_is_left_as_the_profile_has_it(self):
        self.assertEqual(self.calls(Fake(), self.fresh("native"), LOCAL, 8192), ["unset"])


class SmallCall(Base):
    def test_small_call_of_a_local_model_starts_with_thinking_off_in_every_arm(self):
        for reason in (None, "state", "native", "budget", "bias", "schema"):
            with self.subTest(reason=reason):
                self.assertEqual(self.calls(Fake(), self.fresh(reason), LOCAL, 60, 600, 1024), [False, False, False])

    def test_a_call_over_1024_is_left_as_it_was_under_an_explicit_arm(self):
        self.assertEqual(self.calls(Fake(), self.fresh("native"), LOCAL, 1025), ["unset"])

    def test_a_callers_think_or_think_budget_is_kept(self):
        self.assertEqual(self.calls(Fake(), self.fresh(), LOCAL, 600, think=True), [True])
        self.assertEqual(self.calls(Fake(), self.fresh(), LOCAL, 600, think_budget=512), ["unset"])

    def test_a_cloud_or_cli_profile_is_unchanged(self):
        self.assertEqual(self.calls(Fake(), self.fresh(), CLOUD, 600), ["unset"])
        self.assertEqual(self.calls(Fake(), self.fresh(), CLI, 600), ["unset"])


class Learned(Base):
    """The exhausted-size rule applies where the default does not turn thinking off, so these runs are --reason native
    (T-4297: with no --reason a local model starts off, and a model whose kept arm is native runs as today)."""

    def fresh(self, reason="native"):
        return super().fresh(reason)

    def test_learned_after_one_exhausted_call_later_calls_of_its_size_or_smaller_start_off(self):
        run, fake = self.fresh(), Fake(spent(), TEXT, TEXT, TEXT, TEXT)
        self.assertEqual(self.calls(fake, run, LOCAL, 8192), ["unset", False], "asked again with thinking off")
        self.assertEqual(self.calls(fake, run, LOCAL, 8192, 4096), [False, False])
        self.assertEqual(self.calls(fake, run, LOCAL, 8193), ["unset"], "a larger call is not learned from this")

    def test_learned_the_count_is_kept_per_model_and_the_next_run_starts_there_with_one_note(self):
        self.calls(Fake(spent(), TEXT), self.fresh(), LOCAL, 8192)
        with open(os.path.join(core.state_dir(), smith.THINK_FILE), encoding="utf-8") as f:
            kept = json.load(f)
        self.assertEqual(sorted(kept), ["qwen-local"])
        self.assertEqual(kept["qwen-local"]["8192"]["n"], 1)
        self.assertAlmostEqual(kept["qwen-local"]["8192"]["ts"], time.time(), delta=60)
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


def put_kept(state, data):
    """think-kept.json written by hand, in the new or the old shape."""
    os.makedirs(state, exist_ok=True)
    with open(os.path.join(state, smith.THINK_FILE), "w", encoding="utf-8") as f:
        json.dump(data, f)


def read_kept(state):
    with open(os.path.join(state, smith.THINK_FILE), encoding="utf-8") as f:
        return json.load(f)


class DefaultOff(Base):
    """T-4297: with no --reason every call of a local profile's run starts with thinking off, whatever its size."""

    def test_default_off_every_call_of_a_local_run_starts_with_thinking_off_with_one_note(self):
        run, fake, why = self.fresh(), Fake(), []
        run.log = lambda **rec: why.append(rec)
        sizes = (8192, 600, 1024, 4096, 16384, smith.MAX_TOKENS, 60)
        self.assertEqual(self.calls(fake, run, LOCAL, *sizes), [False] * len(sizes))
        notes = [n for n in self.notes if "thinking off by default" in n]
        self.assertEqual(len(notes), 1, self.notes)
        self.assertIn("qwen-local", notes[0])
        self.assertIn("--reason native", notes[0])
        self.assertIn("local default", [x.get("why") for x in why])

    def test_default_off_the_slot_path_and_a_plan_start_off(self):
        run, fake = self.fresh(), Fake()
        with mock.patch.object(models, "complete", fake), mock.patch.object(smith, "fit", lambda r, p, msgs: msgs):
            smith._ask(run, LOCAL, MSG, "plan")
            smith._ask(run, LOCAL, MSG, "type")
        self.assertEqual(fake.thinks, [False, False])

    def test_default_off_no_note_when_every_call_was_small_or_the_profile_is_not_local(self):
        run = self.fresh()
        self.calls(Fake(), run, LOCAL, 60)
        self.calls(Fake(), run, CLOUD, 8192)
        self.assertEqual([n for n in self.notes if "by default" in n], [])

    def test_default_off_a_callers_think_or_think_budget_is_as_before(self):
        self.assertEqual(self.calls(Fake(), self.fresh(), LOCAL, 8192, think=True), [True])
        self.assertEqual(self.calls(Fake(), self.fresh(), LOCAL, 8192, think_budget=512), ["unset"])

    def test_default_off_a_cloud_or_cli_profile_is_as_before(self):
        self.assertEqual(self.calls(Fake(), self.fresh(), CLOUD, 8192), ["unset"])
        self.assertEqual(self.calls(Fake(), self.fresh(), CLI, 8192), ["unset"])

    def test_default_off_every_explicit_reason_arm_is_as_before(self):
        for reason in ("state", "native", "budget", "bias", "schema"):
            with self.subTest(reason=reason):
                self.assertEqual(self.calls(Fake(), self.fresh(reason), LOCAL, 8192), ["unset"])
        self.assertEqual(self.calls(Fake(), self.fresh("off"), LOCAL, 8192), [False])

    def test_default_off_a_run_that_answers_with_no_thinking_teaches_nothing(self):
        self.calls(Fake(), self.fresh(), LOCAL, 8192, 8192)
        self.assertFalse(os.path.exists(os.path.join(core.state_dir(), smith.THINK_FILE)))


class KeptArm(Base):
    """T-4297: one model's kept record can say it keeps thinking, because it measured better."""

    def native(self, name="qwen-local", **extra):
        return {name: dict({"arm": "native", "run": "s8-001", "native": 18, "off": 14, "ts": time.time()}, **extra)}

    def test_kept_arm_native_keeps_thinking_with_no_reason_and_no_note(self):
        put_kept(self.state, self.native())
        self.assertEqual(self.calls(Fake(), self.fresh(), LOCAL, 8192, 4096, 16384), ["unset"] * 3)
        self.assertEqual([n for n in self.notes if "by default" in n], [])

    def test_kept_arm_native_is_per_model(self):
        put_kept(self.state, self.native())
        self.assertEqual(self.calls(Fake(), self.fresh(), OTHER, 8192), [False])

    def test_kept_arm_native_small_calls_and_exhausted_sizes_still_start_off(self):
        put_kept(self.state, self.native(**{"8192": {"n": 2, "ts": time.time()}}))
        self.assertEqual(self.calls(Fake(), self.fresh(), LOCAL, 600, 8192, 16384), [False, False, "unset"])
        run, fake = self.fresh(), Fake(spent(), TEXT, TEXT)
        self.assertEqual(self.calls(fake, run, LOCAL, 32768), ["unset", False])
        self.assertEqual(self.calls(fake, run, LOCAL, 32768), [False], "learned in this run")

    def test_kept_arm_off_or_an_unknown_arm_is_the_default(self):
        for arm in ("off", "state", "", None, 5):
            with self.subTest(arm=arm):
                put_kept(self.state, {"qwen-local": {"arm": arm}})
                self.assertEqual(self.calls(Fake(), self.fresh(), LOCAL, 8192), [False])

    def test_kept_arm_native_does_not_override_reason_off(self):
        put_kept(self.state, self.native())
        self.assertEqual(self.calls(Fake(), self.fresh("off"), LOCAL, 8192), [False])

    def test_kept_arm_a_malformed_file_is_the_default(self):
        for data in ([], {"qwen-local": [1]}, {"qwen-local": "native"}, {"qwen-local": None}):
            with self.subTest(data=data):
                put_kept(self.state, data)
                self.assertEqual(self.calls(Fake(), self.fresh(), LOCAL, 8192), [False])

    def test_kept_arm_write_keeps_the_sizes_and_a_count_keeps_the_arm(self):
        smith.think_record("qwen-local", 8192)
        smith.think_keep("qwen-local", "native", "s8-001", 18, 14)
        kept = read_kept(self.state)["qwen-local"]
        self.assertEqual((kept["arm"], kept["run"], kept["native"], kept["off"]), ("native", "s8-001", 18, 14))
        self.assertAlmostEqual(kept["ts"], time.time(), delta=60)
        self.assertEqual(kept["8192"]["n"], 1)
        smith.think_record("qwen-local", 8192)
        kept = read_kept(self.state)["qwen-local"]
        self.assertEqual((kept["arm"], kept["8192"]["n"]), ("native", 2))
        self.assertEqual(smith.think_arm("qwen-local"), "native")
        self.assertEqual(smith.think_arm("nobody"), "")


class Lapse(Base):
    """T-4297: a learned exhausted size is a measurement of one day's model and load; it stops applying after 14 days."""

    def aged(self, days, size="8192", **extra):
        return {"qwen-local": dict({size: {"n": 1, "ts": time.time() - days * 86400}}, **extra)}

    def test_lapse_an_entry_older_than_14_days_is_not_applied(self):
        put_kept(self.state, self.aged(15))
        self.assertEqual(self.calls(Fake(), self.fresh("native"), LOCAL, 8192), ["unset"])
        self.assertEqual(smith.THINK_LAPSE_DAYS, 14)

    def test_lapse_an_entry_of_13_days_is_applied(self):
        put_kept(self.state, self.aged(13))
        self.assertEqual(self.calls(Fake(), self.fresh("native"), LOCAL, 8192), [False])

    def test_lapse_the_old_shape_with_no_time_is_still_read_and_applied(self):
        put_kept(self.state, {"qwen-local": {"8192": 2}})
        self.assertEqual(self.calls(Fake(), self.fresh("native"), LOCAL, 8192, 600), [False, False])
        self.assertEqual(smith.think_kept("qwen-local"), 8192)

    def test_lapse_a_count_on_an_old_shape_file_is_written_in_the_new_shape(self):
        put_kept(self.state, {"qwen-local": {"4096": 3}})
        smith.think_record("qwen-local", 8192)
        kept = read_kept(self.state)["qwen-local"]
        self.assertEqual(kept["8192"]["n"], 1)
        self.assertEqual(smith.think_kept("qwen-local"), 8192)
        self.assertIn("4096", kept)

    def test_lapse_a_count_refreshes_the_time(self):
        put_kept(self.state, self.aged(20))
        self.assertEqual(smith.think_kept("qwen-local"), 0)
        smith.think_record("qwen-local", 8192)
        self.assertEqual(smith.think_kept("qwen-local"), 8192)
        self.assertEqual(read_kept(self.state)["qwen-local"]["8192"]["n"], 2)

    def test_lapse_a_call_that_thought_and_answered_at_that_size_removes_the_entry(self):
        put_kept(self.state, self.aged(20, "8192", **{"4096": {"n": 1, "ts": time.time()}}))
        self.assertEqual(self.calls(Fake(TEXT), self.fresh("native"), LOCAL, 8192), ["unset"])
        kept = read_kept(self.state)["qwen-local"]
        self.assertNotIn("8192", kept)
        self.assertIn("4096", kept, "another size stays")

    def test_lapse_a_call_that_ran_out_again_keeps_the_entry_with_a_new_time(self):
        put_kept(self.state, self.aged(20))
        self.assertEqual(self.calls(Fake(spent(), TEXT), self.fresh("native"), LOCAL, 8192), ["unset", False])
        entry = read_kept(self.state)["qwen-local"]["8192"]
        self.assertEqual(entry["n"], 2)
        self.assertAlmostEqual(entry["ts"], time.time(), delta=60)

    def test_lapse_a_call_the_caller_asked_to_think_removes_nothing(self):
        put_kept(self.state, self.aged(20))
        self.calls(Fake(TEXT), self.fresh("native"), LOCAL, 8192, think=True)
        self.assertIn("8192", read_kept(self.state)["qwen-local"])

    def test_lapse_the_removal_leaves_the_arm(self):
        put_kept(self.state, self.aged(20, "8192", arm="native", run="s8-001", native=3, off=1, ts=1.0))
        self.calls(Fake(TEXT), self.fresh(), LOCAL, 8192)
        kept = read_kept(self.state)["qwen-local"]
        self.assertEqual((kept["arm"], "8192" in kept), ("native", False))


class Record(Base):
    """T-4297 review: how the kept file is written (the sample size, the lock, a read that failed, the age of an old
    entry, the key)."""

    def test_record_keep_stores_the_sample_size_and_says_what_it_replaced(self):
        self.assertEqual(smith.think_keep("qwen-local", "native", "s8-001", 5, 4, 8), (True, None))
        self.assertEqual(read_kept(self.state)["qwen-local"]["fixable"], 8)
        wrote, prior = smith.think_keep("qwen-local", "off", "s8-002", 6, 6, 8)
        self.assertTrue(wrote)
        self.assertEqual((prior["arm"], prior["run"], prior["native"], prior["off"], prior["fixable"]),
                         ("native", "s8-001", 5, 4, 8))
        self.assertIsInstance(prior["ts"], float)

    def test_record_keep_a_smaller_sample_never_replaces_a_larger_record_of_the_other_arm(self):
        smith.think_keep("qwen-local", "native", "s8-001", 20, 14, 24)
        wrote, held = smith.think_keep("qwen-local", "off", "s9-001", 3, 3, 4)
        self.assertFalse(wrote)
        self.assertEqual((held["arm"], held["run"], held["fixable"]), ("native", "s8-001", 24))
        rec = read_kept(self.state)["qwen-local"]
        self.assertEqual((rec["arm"], rec["run"], rec["fixable"]), ("native", "s8-001", 24))
        self.assertEqual(smith.think_arm("qwen-local"), "native")

    def test_record_keep_an_equal_or_larger_sample_replaces(self):
        smith.think_keep("qwen-local", "native", "s8-001", 5, 4, 8)
        self.assertTrue(smith.think_keep("qwen-local", "off", "s9-001", 7, 7, 8)[0])
        self.assertTrue(smith.think_keep("qwen-local", "native", "s9-002", 20, 14, 24)[0])
        self.assertEqual(read_kept(self.state)["qwen-local"]["fixable"], 24)

    def test_record_keep_a_record_with_no_sample_size_is_replaced(self):
        put_kept(self.state, {"qwen-local": {"arm": "native", "run": "s1-001", "native": 5, "off": 4, "ts": 1.0}})
        wrote, prior = smith.think_keep("qwen-local", "off", "s9-001", 1, 1, 2)
        self.assertEqual((wrote, prior["arm"]), (True, "native"))

    def test_record_a_write_takes_the_lock_and_reads_inside_it(self):
        order = []
        real_read = smith._think_read

        class Held:
            def __enter__(self_):
                order.append("lock")

            def __exit__(self_, *a):
                order.append("unlock")

        def read():
            order.append("read")
            return real_read()
        with mock.patch.object(core, "lock", lambda d, *a, **k: Held()), mock.patch.object(smith, "_think_read", read):
            smith.think_record("qwen-local", 8192)
        self.assertEqual(order[0], "lock")
        self.assertEqual(order[-1], "unlock")
        self.assertIn("read", order[1:-1])

    def test_record_parallel_writers_lose_no_update(self):
        import threading
        sizes = [2048 * (i + 1) for i in range(8)]
        threads = [threading.Thread(target=smith.think_record, args=("qwen-local", s)) for s in sizes]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(int(k) for k in read_kept(self.state)["qwen-local"]), sizes)

    def test_record_a_read_that_fails_is_not_followed_by_a_write(self):
        path = os.path.join(self.state, smith.THINK_FILE)
        os.makedirs(path)  # reading a directory is an OSError that is not "missing"
        for write in (lambda: smith.think_record("qwen-local", 8192),
                      lambda: smith.think_keep("qwen-local", "native", "s8-001", 5, 4, 8)):
            with self.assertRaises(OSError):
                write()
        self.assertTrue(os.path.isdir(path), "nothing replaced it")
        self.assertEqual((smith.think_kept("qwen-local"), smith.think_arm("qwen-local")), (0, ""), "a reader still reads none")

    def test_record_a_read_that_fails_keeps_every_models_record(self):
        put_kept(self.state, {"other-local": {"arm": "native"}, "qwen-local": {}})
        path = os.path.join(self.state, smith.THINK_FILE)
        os.chmod(path, 0)
        try:
            if os.access(path, os.R_OK):
                self.skipTest("this user reads a mode 0 file")
            with self.assertRaises(OSError):
                smith.think_record("qwen-local", 8192)
        finally:
            os.chmod(path, 0o600)
        self.assertEqual(read_kept(self.state)["other-local"], {"arm": "native"})

    def test_record_a_missing_or_corrupt_file_still_starts_again(self):
        smith.think_record("qwen-local", 8192)
        self.assertEqual(smith.think_kept("qwen-local"), 8192)
        with open(os.path.join(self.state, smith.THINK_FILE), "w", encoding="utf-8") as f:
            f.write("{not json")
        smith.think_record("qwen-local", 4096)
        self.assertEqual(smith.think_kept("qwen-local"), 4096)

    def test_record_an_old_shape_entry_ages_from_the_files_modified_time(self):
        put_kept(self.state, {"qwen-local": {"8192": 2}})
        path = os.path.join(self.state, smith.THINK_FILE)
        old = time.time() - 20 * 86400
        os.utime(path, (old, old))
        self.assertEqual(smith.think_kept("qwen-local"), 0, "20 days old: lapsed")
        os.utime(path, (time.time() - 13 * 86400,) * 2)
        self.assertEqual(smith.think_kept("qwen-local"), 8192, "13 days old: applied")

    def test_record_a_write_keeps_the_age_of_the_old_shape_entries_it_rewrites(self):
        put_kept(self.state, {"qwen-local": {"4096": 3}, "other-local": {"2048": 1}})
        path = os.path.join(self.state, smith.THINK_FILE)
        old = time.time() - 20 * 86400
        os.utime(path, (old, old))
        smith.think_record("qwen-local", 8192)
        self.assertEqual(smith.think_kept("qwen-local"), 8192)
        rec = read_kept(self.state)
        self.assertEqual(rec["qwen-local"]["4096"]["n"], 3)
        self.assertAlmostEqual(rec["qwen-local"]["4096"]["ts"], old, delta=5)
        self.assertEqual(smith.think_kept("other-local"), 0, "the other model's old entry did not get younger")

    def test_record_one_helper_keys_both_sides_by_the_same_name(self):
        long = "m" * 70 + "‮" + "tail"
        profile = dict(LOCAL, name=long)
        key = smith.think_key(long)
        self.assertEqual(key, core.plain(long)[:60])
        smith.think_keep(long, "native", "s8-001", 5, 4, 8)
        self.assertIn(key, read_kept(self.state))
        self.assertEqual(smith.think_arm(long), "native")
        self.assertEqual(smith.think_arm(key), "native", "the exam's cut name finds it too")
        self.assertEqual(self.calls(Fake(), self.fresh(), profile, 8192), ["unset"], "the run reads its arm")
        smith.think_record(long, 4096)
        self.assertEqual(smith.think_kept(key), 4096)

    def test_record_the_effective_arm_a_run_stamps(self):
        self.assertEqual(smith.think_ran(LOCAL, None), "off", "the default of a local profile")
        self.assertEqual(smith.think_ran(LOCAL, "native"), "native")
        self.assertEqual(smith.think_ran(LOCAL, "off"), "off")
        self.assertEqual(smith.think_ran(LOCAL, "state"), "state")
        self.assertEqual(smith.think_ran(CLOUD, None), "native", "a cloud profile is as given")
        smith.think_keep("qwen-local", "native", "s8-001", 5, 4, 8)
        self.assertEqual(smith.think_ran(LOCAL, None), "native", "the kept arm is what runs by default")
        self.assertEqual(smith.think_ran(LOCAL, "off"), "off")


if __name__ == "__main__":
    unittest.main()
