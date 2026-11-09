# map-links: plugin/lib/sampo/spar.py plugin/lib/sampo/night.py
"""T-3281: sampo spar feeds mutated hook payloads (malformed, huge, unicode, raw, missing fields) through every hook event
in a temp home, stops at its time bound, and files each distinct traceback once as a FIX with a sandboxed replay. The
nightly job runs it at idle priority."""
import json
import os
import random
import time
from unittest import mock

from helpers import ForemanTestCase

from sampo import core as c
from sampo import drill, hooks, night, spar

KINDS = {"malformed", "huge", "unicode", "missing", "raw"}
TB = 'Traceback (most recent call last):\n  File "hook", line 9, in main\nKeyError: boom\n'
STOP = ("Stop", '{"stop_hook_active": false}', TB, 0)


class FakeDay:
    """A stand-in for drill.Day, so the run tests execute no real hook."""

    def __init__(self, home):
        self.env, self.home, self.repo = {}, home, home
        self.log = os.path.join(home, "hooks.log")
        self.removed = False

    def remove(self):
        self.removed = True


class BrokenDay(FakeDay):
    def fm(self, *a):
        raise RuntimeError("setup failed")


def quick(day, event, body, timeout):
    time.sleep(0.01)
    return 0, "", "", False


class Mutate(ForemanTestCase):
    def test_the_same_seed_gives_the_same_variants(self):
        seed = ("UserPromptSubmit", {"prompt": "fix the thing"})
        self.assertEqual(list(spar.mutate(seed, random.Random(4))), list(spar.mutate(seed, random.Random(4))))
        self.assertNotEqual(list(spar.mutate(seed, random.Random(4))), list(spar.mutate(seed, random.Random(5))))

    def test_every_seed_yields_all_the_kinds(self):
        self.assertTrue(spar.SEEDS, "the seeds are the hook events")
        for seed in spar.SEEDS:
            self.assertTrue(KINDS <= {k for k, _ in spar.mutate(seed, random.Random(1))}, seed[0])

    def test_every_hook_event_is_seeded(self):
        # PreToolUse is the fail-closed guard: hooks.main routes it to _pre_tool_use before HANDLERS, so it is not a HANDLERS key
        self.assertEqual({e for e, _ in spar.SEEDS}, set(hooks.HANDLERS) | {"PreToolUse"})
        self.assertTrue(all(isinstance(p, dict) for _, p in spar.SEEDS))

    def test_no_seeds_means_no_mutants_so_the_loop_ends(self):
        with mock.patch.object(spar, "SEEDS", ()):
            self.assertEqual(list(spar._mutants(random.Random(1))), [])

    def test_each_kind_is_what_its_name_says(self):
        seed = ("PreToolUse", {"tool_name": "Edit", "tool_input": {"file_path": "README.md"}})
        first = {}
        for kind, body in spar.mutate(seed, random.Random(1)):
            first.setdefault(kind, body)
        with self.assertRaises(ValueError):
            json.loads(first["malformed"])
        self.assertGreaterEqual(len(first["huge"]), 100_000)
        self.assertTrue(any(ord(ch) > 127 for ch in first["unicode"]), "non-ASCII chars reach the hook raw")
        self.assertLess(len(json.loads(first["missing"])), len(seed[1]), "a field is gone")

    def test_the_raw_kind_is_not_utf8(self):
        seed = ("Stop", {"stop_hook_active": False})
        raw = dict(spar.mutate(seed, random.Random(1)))["raw"]
        with self.assertRaises(UnicodeEncodeError):
            raw.encode("utf-8")  # a lone surrogate: the bytes the hook reads are invalid UTF-8


class Run(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fake = FakeDay(self.tmp)

    def run_with(self, exec_fn, seed, budget, upto=None):
        with mock.patch.object(spar, "_day", return_value=self.fake), mock.patch.object(spar, "_exec", exec_fn):
            return spar.run(seed, budget, upto=upto)

    def test_a_planted_traceback_is_one_crash_with_its_event_payload_stderr_and_index(self):
        def planted(day, event, body, timeout):
            return (1, "", TB, False) if event == "Stop" else (0, "", "", False)

        r = self.run_with(planted, 1, 1)
        self.assertEqual(len(r["crashes"]), 1, "one signature, one crash entry")
        event, body, tail, k = r["crashes"][0]
        self.assertEqual(event, "Stop")
        self.assertIsInstance(body, str)
        self.assertIn("KeyError: boom", tail)
        self.assertIsInstance(k, int)
        self.assertTrue(self.fake.removed, "the temp home is removed")

    def test_a_hook_that_hangs_before_the_bound_is_a_crash(self):
        def hang(day, event, body, timeout):
            return (-1, "", "", True) if event == "Stop" else (0, "", "", False)

        r = self.run_with(hang, 1, 1)
        self.assertIn("timed out", " ".join(tail for ev, _, tail, _k in r["crashes"] if ev == "Stop"))

    def test_the_time_bound_stops_the_run_and_still_reports(self):
        r = self.run_with(quick, 3, 0.5)
        self.assertGreater(r["ran"], 0)
        self.assertLess(r["elapsed"], 0.5 + 3)
        self.assertEqual(r["crashes"], [])
        self.assertEqual(r["budget_s"], 0.5)

    def test_a_zero_budget_is_a_well_formed_empty_report(self):
        r = self.run_with(quick, 3, 0.0)
        self.assertEqual(r["ran"], 0)
        self.assertEqual(r["crashes"], [])

    def test_an_unbounded_run_needs_a_replay_index_or_it_never_ends(self):
        with self.assertRaises(ValueError):
            self.run_with(quick, 3, None)

    def test_replay_reruns_the_same_mutants_and_the_crash_recurs(self):
        seen = []

        def third(day, event, body, timeout):
            seen.append(body)
            return (1, "", TB, False) if len(seen) == 3 else (0, "", "", False)

        first = self.run_with(third, 5, 60, upto=3)
        self.assertEqual(len(first["crashes"]), 1)
        k = first["crashes"][0][3]
        self.assertEqual(k, 2, "the third mutant")
        bodies = list(seen)
        seen.clear()
        again = self.run_with(third, 5, None, upto=k + 1)
        self.assertEqual(seen, bodies, "the same mutants, in the same order")
        self.assertEqual(spar.signature(again["crashes"][0]), spar.signature(first["crashes"][0]))

    def test_the_hook_ceiling_scales_with_load(self):
        with mock.patch("sampo.core.load_scaled", return_value=42.0) as scaled:
            self.assertEqual(spar._ceiling(), 42.0)
        scaled.assert_called_once_with(spar.HOOK_S)

    def test_a_day_that_fails_to_set_up_is_removed(self):
        broken = BrokenDay(self.tmp)
        with mock.patch.object(drill, "Day", return_value=broken):
            with self.assertRaises(RuntimeError):
                spar._day()
        self.assertTrue(broken.removed)


class Capture(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)

    def test_two_identical_crashes_file_one_fix_and_a_second_run_files_none(self):
        got = spar.capture(self.p, {"seed": 7, "crashes": [STOP, STOP]})
        self.assertEqual([new for _, new in got], [True, False])
        again = spar.capture(self.p, {"seed": 7, "crashes": [STOP]})
        self.assertEqual([new for _, new in again], [False])

    def test_a_different_traceback_is_a_different_fix(self):
        other = ("Stop", STOP[1], TB.replace("KeyError: boom", "ValueError: other"), 1)
        got = spar.capture(self.p, {"seed": 7, "crashes": [STOP, other]})
        self.assertEqual([new for _, new in got], [True, True])

    def test_at_most_five_new_fixes_per_run(self):
        crashes = [("Stop", STOP[1], TB.replace("KeyError: boom", f"Err{chr(97 + i)}: x"), i) for i in range(7)]
        got = spar.capture(self.p, {"seed": 7, "crashes": crashes})
        self.assertEqual(len(got), 7)
        self.assertEqual(sum(new for _, new in got), 5)
        self.assertEqual(got[5], (None, False), "the sixth waits for the next run")

    def test_numbers_and_quoted_names_do_not_split_one_bug(self):
        a = ("Stop", "", "Traceback\nKeyError: 'resume_1' at 12\n", 0)
        b = ("Stop", "", "Traceback\nKeyError: 'other_22' at 99\n", 3)
        self.assertEqual(spar.signature(a), spar.signature(b))

    def test_the_brief_names_its_signature_and_a_sandboxed_replay_by_seed(self):
        text = spar.brief_text(STOP, 7)
        self.assertTrue(text.splitlines()[0].startswith(f"spar:{spar.signature(STOP)}"), text)
        self.assertIn("KeyError: boom", text)
        self.assertIn("sampo spar --seed 7 --replay 0", text)

    def test_a_nul_byte_payload_is_fine_because_the_replay_regenerates_it(self):
        text = spar.brief_text(("Stop", "a\x00b", TB, 4), 7)
        self.assertNotIn("\x00", text)
        self.assertIn("--replay 4", text)

    def test_a_huge_payload_stays_out_of_the_brief(self):
        text = spar.brief_text(("Stop", "x" * 200_000, TB, 0), 7)
        self.assertLess(len(text), 2000)
        self.assertNotIn("x" * 50, text)


class Cli(ForemanTestCase):
    def test_fm_spar_reports_a_bounded_run_at_idle_priority(self):
        p = self.fm("spar", "--seed", "3", "--budget", "2", "--json", check=False)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        got = json.loads(p.stdout)
        self.assertEqual(got["seed"], 3)
        self.assertEqual(got["priority"], "idle")
        self.assertGreaterEqual(got["ran"], 1, "real hooks ran inside the bound")
        self.assertLess(got["elapsed"], 2 + 10)

    def test_the_replay_flag_reruns_one_mutant_in_a_sandbox(self):
        p = self.fm("spar", "--seed", "3", "--replay", "0", "--json", check=False)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        got = json.loads(p.stdout)
        self.assertEqual((got["replay"], got["ran"]), (0, 1))
        self.assertIsInstance(got["recurs"], bool)

    def test_json_crash_bodies_are_clipped_to_500_chars(self):
        shown = spar.for_json({"crashes": [("Stop", "x" * 200_000, TB, 0)]})
        body = shown["crashes"][0][1]
        self.assertLessEqual(len(body), 520)
        self.assertIn("200000", body)

    def test_a_lone_surrogate_body_still_prints_as_utf8_json(self):
        # the raw mutant carries a lone surrogate: printing it in --json must not raise UnicodeEncodeError
        shown = spar.for_json({"crashes": [("Stop", "ab\ud800cd", TB, 0)]})
        json.dumps(shown, ensure_ascii=False).encode("utf-8")
        self.assertIn("\\ud800", shown["crashes"][0][1])


class RealHook(ForemanTestCase):
    def test_a_real_hook_run_counts_at_least_one_mutant(self):
        r = spar.run(1, 0.5)
        self.assertGreaterEqual(r["ran"], 1, "the first mutant starts before the bound")


class NightJob(ForemanTestCase):
    def test_the_spar_job_is_listed_as_a_free_job_with_its_argv(self):
        self.fm("init")
        p = c.find_project(self.repo)
        jobs = {n: (argv, usd) for n, argv, usd in night.jobs(p)}
        self.assertEqual(jobs["spar"], (["spar", "--capture", "--budget", "120"], 0.0))


if __name__ == "__main__":
    import unittest
    unittest.main()
