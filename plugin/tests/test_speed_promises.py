"""T-3272 (Moon C-001150): the speed promises table and its check. Time is child CPU time, a ratio to a same-run bare python
(a busy box moves CPU time little, wall time a lot). The first round is warmup; a row over its promise is re-measured
in a second, longer window and fails only if that breaks too. The live tests share one scratch project and enforce the
verbs' ratios and the hook budgets (bench_hooks' own budgets, read live)."""
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import bench_hooks  # noqa: E402
import speed_promises as sp  # noqa: E402


def fixed(ms):
    """A timer whose call to each key costs ms[key] milliseconds of CPU (the bare key is speed_promises.BARE)."""
    return lambda key: ms[key]


def sequence(ms):
    """A timer whose calls to each key cost the listed milliseconds in turn (the last one repeats)."""
    seen = {}

    def timer(key):
        i = seen.get(key, 0)
        seen[key] = i + 1
        return ms[key][min(i, len(ms[key]) - 1)]
    return timer


def counted(timer):
    """The same timer, plus how many calls each key got."""
    calls = {}

    def t(key):
        calls[key] = calls.get(key, 0) + 1
        return timer(key)
    return t, calls


PROMISES = {"fast verb": {"argv": ["fast"], "max_ratio": 5}, "slow verb": {"argv": ["slow"], "max_ratio": 5}}


class Check(unittest.TestCase):
    def test_a_fast_path_passes_and_a_slow_one_is_flagged_with_its_numbers(self):
        rows = sp.check(PROMISES, runs=3, timer=fixed({sp.BARE: 20, "fast verb": 60, "slow verb": 300}))
        by = {r["path"]: r for r in rows}
        self.assertTrue(by["fast verb"]["ok"])
        self.assertFalse(by["slow verb"]["ok"])
        self.assertAlmostEqual(by["slow verb"]["ratio"], 15.0)
        report = sp.report(rows)
        self.assertIn("slow verb", report)
        self.assertNotIn("fast verb", report, "only the path over its promise is reported")
        for want in ("15.0x", "promise 5x", "300 ms", "20 ms"):
            self.assertIn(want, report)

    def test_a_busy_machine_scales_python_and_the_verbs_alike_and_changes_no_verdict(self):
        quiet = {sp.BARE: 20, "fast verb": 60, "slow verb": 300}
        busy = {k: v * 4 for k, v in quiet.items()}
        q = sp.check(PROMISES, runs=3, timer=fixed(quiet))
        b = sp.check(PROMISES, runs=3, timer=fixed(busy))
        self.assertEqual([r["ok"] for r in q], [r["ok"] for r in b])
        self.assertEqual([round(r["ratio"], 6) for r in q], [round(r["ratio"], 6) for r in b])

    def test_the_median_of_the_calls_decides_not_one_outlier(self):
        # each list starts with the warmup call (the first round, discarded), then the three measured calls
        timer = sequence({sp.BARE: [20], "fast verb": [9000, 5000, 60, 60], "slow verb": [9000, 300, 300, 9000]})
        rows = {r["path"]: r for r in sp.check(PROMISES, runs=3, timer=timer)}
        self.assertTrue(rows["fast verb"]["ok"], "one 5 s call is noise; the median is 60 ms")
        self.assertFalse(rows["slow verb"]["ok"], "the median 300 ms is over 5x of 20 ms")
        self.assertEqual(rows["slow verb"]["median_cpu_ms"], 300)

    def test_the_bare_python_median_is_taken_in_the_same_run(self):
        timer = sequence({sp.BARE: [99, 10, 10, 40], "fast verb": [30], "slow verb": [30]})
        rows = sp.check(PROMISES, runs=3, timer=timer)
        self.assertEqual(rows[0]["bare_cpu_ms"], 10, "the median of this run's bare calls, not a constant")
        self.assertEqual({r["bare_cpu_ms"] for r in rows}, {10})

    def test_the_first_round_is_warmup_and_is_discarded(self):
        timer = sequence({sp.BARE: [1000, 20], "fast verb": [9000, 60], "slow verb": [9000, 60]})
        rows = {r["path"]: r for r in sp.check(PROMISES, runs=1, timer=timer)}
        self.assertEqual(rows["fast verb"]["median_cpu_ms"], 60, "the 9 s warmup call is not a sample")
        self.assertEqual(rows["fast verb"]["bare_cpu_ms"], 20)

    def test_a_zero_bare_baseline_cannot_pass_a_verb(self):
        rows = sp.check(PROMISES, runs=2, timer=fixed({sp.BARE: 0, "fast verb": 1, "slow verb": 1}))
        self.assertFalse(any(r["ok"] for r in rows))


class Settle(unittest.TestCase):
    @staticmethod
    def measure(timer):
        return lambda names, runs: sp.check({n: PROMISES[n] for n in names}, runs=runs, timer=timer)

    def test_a_row_that_breaks_in_one_window_passes_on_the_second(self):
        # window 1 (runs 3 + warmup = 4 calls) has the slow verb at 300 ms; window 2 (runs 9 + warmup = 10) at 20 ms
        timer = sequence({sp.BARE: [20] * 14, "fast verb": [60] * 14, "slow verb": [300] * 4 + [20] * 10})
        rows = {r["path"]: r for r in sp.settle(sp.check(PROMISES, runs=3, timer=timer), self.measure(timer))}
        self.assertTrue(rows["slow verb"]["ok"])
        self.assertEqual(rows["slow verb"]["window"], 2)
        self.assertEqual(rows["fast verb"]["window"], 1)

    def test_a_row_over_in_both_windows_fails_and_is_reported(self):
        timer = sequence({sp.BARE: [20] * 14, "fast verb": [60] * 14, "slow verb": [300] * 14})
        rows = {r["path"]: r for r in sp.settle(sp.check(PROMISES, runs=3, timer=timer), self.measure(timer))}
        self.assertFalse(rows["slow verb"]["ok"])
        self.assertEqual(rows["slow verb"]["window"], 2)
        self.assertIn("slow verb", sp.report(list(rows.values())))

    def test_the_second_window_measures_only_the_failing_rows(self):
        timer, calls = counted(sequence({sp.BARE: [20] * 14, "fast verb": [60] * 14, "slow verb": [300] * 4 + [20] * 10}))
        sp.settle(sp.check(PROMISES, runs=3, timer=timer), self.measure(timer))
        self.assertEqual(calls["fast verb"], 4, "window 1 only: warmup + 3")
        self.assertEqual(calls["slow verb"], 4 + 10, "window 1, then window 2 at 9 runs")
        self.assertEqual(calls[sp.BARE], 4 + 10, "bare is timed in each window, so the same run's baseline")

    def test_a_clean_first_window_is_not_measured_again(self):
        timer, calls = counted(fixed({sp.BARE: 20, "fast verb": 60, "slow verb": 60}))
        rows = sp.settle(sp.check(PROMISES, runs=3, timer=timer), self.measure(timer))
        self.assertTrue(all(r["ok"] and r["window"] == 1 for r in rows))
        self.assertEqual(calls[sp.BARE], 4)


class Table(unittest.TestCase):
    def test_the_table_names_the_hot_paths_with_a_max_ratio(self):
        self.assertEqual(set(sp.PROMISES), {"sampo next", "sampo task show", "sampo state", "sampo which"})
        for name, p in sp.PROMISES.items():
            self.assertIsInstance(p["max_ratio"], (int, float), name)
            self.assertGreater(p["max_ratio"], 1, name)
            self.assertTrue(p["argv"], name)

    def test_the_hook_budgets_are_read_from_bench_hooks_not_copied(self):
        with mock.patch.dict(bench_hooks.BUDGET_MS, {"default": 777, "MessageDisplay": 555}):
            got = sp.hook_budgets()
        self.assertEqual(got, {"hook default": 777, "hook MessageDisplay": 555})

    def test_the_scratch_env_drops_the_developers_session_and_points_home_at_the_scratch(self):
        inherited = {"FOREMAN_PROJECT": "/home/dev/app", "FOREMAN_STATE": "/home/dev/state", "FOREMAN_SESSION_ID": "s",
                     "CLAUDE_CODE_SESSION_ID": "c", "FOREMAN_HOME": "/tmp/h", "PATH": "/usr/bin"}
        env = sp.scratch_env(inherited, "/tmp/scratch")
        for k in ("FOREMAN_PROJECT", "FOREMAN_STATE", "FOREMAN_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
            self.assertNotIn(k, env)
        self.assertEqual(env["FOREMAN_NO_BACKGROUND"], "1")
        self.assertEqual(env["HOME"], "/tmp/scratch")
        self.assertEqual(env["FOREMAN_HOME"], "/tmp/h", "the scratch's own FOREMAN_HOME is kept")
        self.assertEqual(env["PATH"], "/usr/bin")

    def test_a_hook_row_reads_its_budget_from_bench_hooks_run_bench(self):
        got = {"Stop": {"p50": 20, "p95": 40, "cpu_p95": 30, "budget": 150, "load": 0.2, "ok": True, "exit_codes": [0]},
               "MessageDisplay": {"p50": 20, "p95": 300, "cpu_p95": 200, "budget": 100, "load": 0.2, "ok": False,
                                  "exit_codes": [0]}}
        with mock.patch.object(bench_hooks, "run_bench", return_value=got) as run:
            rows = {r["path"]: r for r in sp.hook_check(5)}
        self.assertTrue(rows["hook default"]["ok"])
        self.assertFalse(rows["hook MessageDisplay"]["ok"])
        self.assertEqual(rows["hook MessageDisplay"]["budget_ms"], 100)
        self.assertIn("hook MessageDisplay: p95 300 ms over its 100 ms budget", sp.report(list(rows.values())))
        self.assertEqual(run.call_args.args[0], 5)


class Cpu(unittest.TestCase):
    def test_a_sleeping_child_costs_no_cpu_and_a_busy_one_does(self):
        # T-3272: the promises are on CPU time, so waiting on a busy box does not count against a verb
        slept, p = sp.run_cpu([sys.executable, "-c", "import time; time.sleep(0.5)"], os.getcwd(), os.environ)
        self.assertEqual(p.returncode, 0)
        self.assertLess(slept, 150, "0.5 s asleep is a few ms of CPU")
        burned, p = sp.run_cpu([sys.executable, "-c", "sum(range(30000000))"], os.getcwd(), os.environ)
        self.assertEqual(p.returncode, 0)
        self.assertGreater(burned, 150)


class Live(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.repo, cls.env = sp.scratch_project(cls._tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_the_real_verbs_keep_their_promises_in_a_scratch_project(self):
        timer = sp.live_timer(self.repo, self.env, sp.PROMISES)
        rows = sp.settle(sp.check(sp.PROMISES, runs=5, timer=timer), lambda names, runs: sp.check(
            {n: sp.PROMISES[n] for n in names}, runs=runs, timer=timer))
        self.assertEqual(sp.report(rows), "", "a verb is over its promise in two windows")
        self.assertEqual({r["path"] for r in rows}, set(sp.PROMISES))

    def test_the_hook_budgets_hold_in_the_live_bench(self):
        rows = sp.settle(sp.hook_check(5), lambda labels, runs: sp.hook_check(runs, labels))
        self.assertEqual(sp.report(rows), "", "a hook is over its budget in two windows")
        self.assertEqual({r["path"] for r in rows}, set(sp.HOOK_PROMISES))

    def test_a_verb_that_fails_fast_is_an_error_not_a_fast_pass(self):
        timer = sp.live_timer(self.repo, self.env, {"no such verb": {"argv": ["no-such-verb-xyz"], "max_ratio": 10}})
        with self.assertRaises(RuntimeError):
            timer("no such verb")


if __name__ == "__main__":
    unittest.main()
