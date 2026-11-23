"""T-2605: the self-change gate's hook check (fmselfgate._slower) on a loaded bench: a hook that waits shows in the
median wall time even when load inflates the p95, and a small median wobble is noise.
T-2992: base and branch are benched interleaved over 3 pairs; only a rise seen in 2 of 3 pairs holds."""
# map-links: plugin/lib/sampo/louhi.py
# (T-3577: LoadedWallCheck execs louhi's planted hook wait; a louhi-only change must run it)
import json
import os
import unittest
from unittest import mock

import helpers  # noqa: F401  (puts plugin/lib on the path)


class LoadedWallCheck(unittest.TestCase):
    def test_a_waiting_hook_is_caught_when_load_inflates_the_base_p95(self):  # T-2605: the LeanKind flake
        from sampo import selfgate as g
        base = {"Stop": {"p95": 400.0, "p50": 50.0, "cpu_p95": 30.0, "load": 3.0, "budget": 150}}
        rows = g._slower(base, {"Stop": {"p95": 600.0, "p50": 300.0, "cpu_p95": 31.0, "load": 3.0, "budget": 150}})
        self.assertIn("Stop", rows)
        self.assertIn("50.0 -> 300.0", rows["Stop"])
        self.assertEqual(g._slower(base, {"Stop": {"p95": 450.0, "p50": 70.0, "cpu_p95": 31.0, "load": 3.0,
                                                   "budget": 150}}), {}, "a 20 ms median wobble is load noise")

    def planted_wait(self, age):
        """(seconds slept, stderr) of the exam's planted hook line at load 3 per CPU, in a process `age` seconds old
        (None: /proc unreadable). T-3629: it feeds the snippet a known /proc/self/stat and /proc/uptime."""
        import io
        import os
        from sampo import core as c, louhi
        _, old, new = next(e for m in louhi.GATE_MUTATIONS if m[0] == "hook-delay" for e in m[3])
        code = new[len(old):]  # T-3577: what the mutation adds past its anchor, however many lines the anchor is
        tck = os.sysconf("SC_CLK_TCK")
        files = {"/proc/uptime": "1000.00 5000.00\n",
                 "/proc/self/stat": "1 (a b) S " + " ".join(["0"] * 18) + f" {round((1000 - (age or 0)) * tck)} 0\n"}

        def fake_open(path, *a, **k):
            if age is None or path not in files:
                raise OSError(path)
            return io.StringIO(files[path])
        waits, err = [], io.StringIO()
        with mock.patch.dict(os.environ, FOREMAN_HOST="load=48,cpus=16"), mock.patch("builtins.open", fake_open), \
                mock.patch("sys.stderr", err):
            exec(code.replace("\n    ", "\n").lstrip(), {"time": mock.Mock(sleep=waits.append), "c": c, "os": os,
                                                          "sys": __import__("sys")})
        return waits[0], err.getvalue()

    def test_the_planted_wait_is_three_times_the_hook_age_between_the_load_scaled_floor_and_a_cap(self):  # T-3629
        self.assertAlmostEqual(self.planted_wait(0.5)[0], 1.5, places=2)  # 3 x age, over the load-scaled 0.75 s
        self.assertAlmostEqual(self.planted_wait(0.1)[0], 0.75, places=2)  # the floor: 0.25 s x 48/16 load
        self.assertAlmostEqual(self.planted_wait(20.0)[0], 3.0, places=2)  # the cap: 160 bench runs fit the timeout
        wait, err = self.planted_wait(None)  # no /proc: the load-scaled wait, said aloud
        self.assertAlmostEqual(wait, 0.75, places=2)
        self.assertIn("no /proc", err)

    def test_the_exams_planted_wait_still_doubles_a_hook_slowed_by_load_3_per_cpu(self):  # T-3605: the train's flake
        from sampo import selfgate as g
        waits = [self.planted_wait(0.3)[0]]
        base = {"Stop": {"p50": 300.0, "cpu_p50": 40.0, "cpu_p95": 45.0, "load": 3.0}}  # its fastest hook, past 250 ms
        self.assertIn("Stop", g._slower(base, {"Stop": dict(base["Stop"], p50=300.0 + waits[0] * 1000)}))


class Interleaved(unittest.TestCase):  # T-2992: fake benches with fixed timings, never real load
    def run_pairs(self, base_p95, branch_p95):
        """The rows `_paired` holds when the base's benches read base_p95 in turn and the branch's branch_p95."""
        from sampo import selfgate as g
        calls, seq = [], {"base": iter(base_p95), "new": iter(branch_p95)}

        def bench(tree):
            calls.append(tree)
            return {"Stop": {"p95": next(seq[tree]), "load": 0, "budget": 500}}, {}
        with mock.patch.object(g, "_bench", bench):
            rows, _, _ = g._paired("base", "new")
        return rows, calls

    def test_the_trees_are_benched_in_turn_three_pairs(self):
        rows, calls = self.run_pairs([100] * 3, [100] * 3)
        self.assertEqual(calls, ["base", "new"] * 3)
        self.assertEqual(rows, {})

    def test_a_rise_in_one_pair_is_load_noise(self):
        self.assertEqual(self.run_pairs([100, 100, 100], [300, 100, 100])[0], {})

    def test_a_rise_in_two_of_three_pairs_is_chance(self):  # T-3131: 16 hooks x 3 pairs make 2-of-3 likely by chance
        self.assertEqual(self.run_pairs([100, 100, 100], [300, 100, 300])[0], {})

    def test_a_real_rise_in_every_pair_holds(self):
        rows, _ = self.run_pairs([100, 100, 100], [200, 210, 220])
        self.assertIn("Stop", rows)
        self.assertIn("100", rows["Stop"])


class MedianCpu(unittest.TestCase):  # T-3131: the p95 of 10 runs is the slowest run; the median CPU time is steady
    def test_the_median_cpu_time_is_judged_when_both_sides_have_it(self):
        from sampo import selfgate as g
        base = {"Stop": {"p95": 100.0, "cpu_p95": 100.0, "cpu_p50": 60.0, "load": 0, "budget": 500}}
        spike = {"Stop": {"p95": 140.0, "cpu_p95": 140.0, "cpu_p50": 61.0, "load": 0, "budget": 500}}
        self.assertEqual(g._slower(base, spike), {}, "one slow run moves the p95, not the median")
        # a bigger one-pair jump trips the tail bar in that pair, and _paired's every-pair rule drops it
        with mock.patch.object(g, "_bench", side_effect=[(base, {}), ({"Stop": dict(spike["Stop"], p95=180.0)}, {}),
                                                          (base, {}), (base, {}), (base, {}), (base, {})]):
            self.assertEqual(g._paired("base", "new")[0], {})
        real = {"Stop": {"p95": 101.0, "cpu_p95": 101.0, "cpu_p50": 80.0, "load": 0, "budget": 500}}
        self.assertIn("60.0 -> 80.0", g._slower(base, real)["Stop"])

    def test_a_hook_that_waits_holds_on_a_quiet_bench(self):  # T-3131 review: CPU time misses a sleep or an I/O wait
        from sampo import selfgate as g
        base = {"Stop": {"p50": 60.0, "p95": 70.0, "cpu_p95": 60.0, "cpu_p50": 55.0, "load": 0.4, "budget": 500}}
        waits = {"Stop": {"p50": 100.0, "p95": 110.0, "cpu_p95": 60.0, "cpu_p50": 55.0, "load": 0.4, "budget": 500}}
        self.assertIn("wall", g._slower(base, waits)["Stop"])

    def test_a_slowdown_in_the_tail_alone_holds(self):  # T-3131 review: extra work on a few calls leaves the median
        from sampo import selfgate as g
        base = {"Stop": {"p50": 60.0, "p95": 100.0, "cpu_p95": 100.0, "cpu_p50": 55.0, "load": 0.4, "budget": 500}}
        tail = {"Stop": {"p50": 60.0, "p95": 200.0, "cpu_p95": 200.0, "cpu_p50": 55.0, "load": 0.4, "budget": 500}}
        self.assertIn("tail", g._slower(base, tail)["Stop"])
        wobble = {"Stop": {"p50": 60.0, "p95": 130.0, "cpu_p95": 130.0, "cpu_p50": 55.0, "load": 0.4, "budget": 500}}
        self.assertEqual(g._slower(base, wobble), {}, "a +30% max of 10 runs is the noise the median basis avoids")

    def test_an_old_bench_without_the_median_is_judged_as_before(self):
        from sampo import selfgate as g
        base = {"Stop": {"p95": 100.0, "load": 0, "budget": 500}}
        self.assertIn("Stop", g._slower(base, {"Stop": {"p95": 130.0, "load": 0, "budget": 500}}))

    def test_the_bench_reports_the_median_cpu_time(self):
        import os
        src = open(os.path.join(os.path.dirname(__file__), "bench_hooks.py"), encoding="utf-8").read()
        self.assertIn('"cpu_p50"', src)


class AaFixtures(unittest.TestCase):  # T-3131: the same tree benched against itself (with cpu_p50), 2026-11-05 UTC, load ~2 per core
    def test_identical_trees_hold_nothing(self):
        import json
        import os
        from sampo import selfgate as g
        d = os.path.join(os.path.dirname(__file__), "fixtures", "selfgate-aa")
        seq = {"base": iter([f"{i}a" for i in (1, 2, 3)]), "new": iter([f"{i}b" for i in (1, 2, 3)])}

        def bench(tree):
            with open(os.path.join(d, next(seq[tree]) + ".json"), encoding="utf-8") as f:
                return json.load(f)["bench"], {}
        with mock.patch.object(g, "_bench", bench):
            rows, _, _ = g._paired("base", "new")
        self.assertEqual(rows, {})


class MachineAndSpread(unittest.TestCase):
    """T-3091: a rise holds only past RISE plus that hook's spread across the base's own benches (the base against itself
    in the same window, no extra bench), and after dividing out the median ratio across all hooks on a loaded bench."""
    FIX = os.path.join(os.path.dirname(__file__), "fixtures")
    FIELDS = ("p50", "p95", "cpu_p50", "cpu_p95")

    def aa(self, name):
        with open(os.path.join(self.FIX, "selfgate-aa", name + ".json"), encoding="utf-8") as f:
            return json.load(f)["bench"]

    def scaled(self, bench, ratio):
        """bench with each hook's timings times ratio[hook] (ratio['*'] for the others)."""
        out = {}
        for k, v in bench.items():
            r = ratio.get(k, ratio.get("*", 1.0))
            out[k] = dict(v, **{f: round(v[f] * r, 3) for f in self.FIELDS if f in v})
        return out

    def pairs(self, base, branch):
        """The rows `_paired` holds when its benches read base[i] then branch[i], i in turn; the call count too."""
        from sampo import selfgate as g
        seq, calls = {"base": iter(base), "new": iter(branch)}, []

        def bench(tree):
            calls.append(tree)
            return next(seq[tree]), {}
        with mock.patch.object(g, "_bench", bench):
            rows, _, _ = g._paired("base", "new")
        return rows, calls

    def replay(self, hold):
        base = [self.aa(f"{i}a") for i in (1, 2, 3)]
        branch = [self.scaled(self.aa(f"{i}b"), hold["ratio"] if n in hold["pairs"] else {}) for n, i in enumerate((1, 2, 3))]
        return self.pairs(base, branch)[0]

    def test_the_reconstructed_holds_of_2026_11_04_and_05_pass_except_the_one_only_the_machine_divisor_excused(self):
        """The ledger kept each hold's verdict rows, not its benches: these 8 are rebuilt rises, not recordings. Seven
        show in 2 of the 3 pairs, so the HOLD = 3 rule passes them on trunk without T-3091's spread or divisor: only
        "T-3026 first" (every hook up 35% in all 3 pairs) exercises the new code. The divisor is clamped to what the base
        itself moved (a loaded bench's own spread), so a 35% rise in every hook is no longer excused as 'the machine':
        that hold is refused, and that is the right outcome (B1), not a number tuned to pass."""
        with open(os.path.join(self.FIX, "selfgate-holds.json"), encoding="utf-8") as f:
            holds = json.load(f)["holds"]
        self.assertEqual(len(holds), 8)
        every_pair = [h["task"] for h in holds if len(h["pairs"]) == 3]
        self.assertEqual(every_pair, ["T-3026 first"], "the only reconstructed hold that reaches the new code")
        for hold in holds:
            rows = self.replay(hold)
            if hold["task"] in every_pair:
                self.assertGreaterEqual(len(rows), 5, "a uniform +35% on a loaded bench is a change, not the machine")
            else:
                self.assertEqual(rows, {}, hold["task"])

    def flat(self, n=16, load=3.0, cpu=100.0):
        """n steady hooks, loaded; three base benches wobbling 5% against each other (the base against itself)."""
        one = {f"h{i}": {"cpu_p50": cpu, "p50": cpu, "p95": cpu, "cpu_p95": cpu, "load": load, "budget": 1000}
               for i in range(n)}
        return [self.scaled(one, {"*": w}) for w in (1.0, 1.05, 1.02)]

    def test_every_hook_up_40_percent_on_a_loaded_bench_is_held(self):
        base = self.flat()
        rows, _ = self.pairs(base, [self.scaled(b, {"*": 1.4}) for b in base])
        self.assertEqual(len(rows), 16, rows)

    def test_every_hook_of_the_recorded_a_a_benches_up_40_percent_is_held(self):
        rows = self.replay({"ratio": {"*": 1.4}, "pairs": [0, 1, 2]})
        self.assertGreaterEqual(len(rows), 10, rows)

    def test_three_of_five_hooks_up_50_percent_on_a_loaded_bench_are_held(self):
        base = self.flat(5)
        rows, _ = self.pairs(base, [self.scaled(b, {"h0": 1.5, "h1": 1.5, "h2": 1.5}) for b in base])
        self.assertEqual(sorted(rows), ["h0", "h1", "h2"], rows)

    def test_nine_of_sixteen_hooks_up_40_percent_on_a_loaded_bench_are_held(self):
        base = self.flat(16)
        rows, _ = self.pairs(base, [self.scaled(b, {f"h{i}": 1.4 for i in range(9)}) for b in base])
        self.assertEqual(sorted(rows), sorted(f"h{i}" for i in range(9)), rows)

    def test_the_machine_divisor_never_exceeds_what_the_base_itself_moved(self):
        from sampo import selfgate as g
        base = self.flat(8)[0]
        up = self.scaled(base, {"*": 1.35})
        every = {k: 0.30 for k in base}
        self.assertAlmostEqual(g._machine(base, up, every), 1.30, places=2, msg="the base moved 30%: that much, no more")
        self.assertAlmostEqual(g._machine(base, up, {k: 0.05 for k in base}), 1.05, places=2)
        self.assertEqual(g._machine(base, up, {}), 1.0, "no spread measured: nothing is excused")
        self.assertEqual(g._machine(base, self.scaled(base, {"*": 0.8}), every), 1.0, "a faster branch never raises the bar")

    def test_a_hooks_wall_wobble_does_not_widen_its_cpu_bar(self):
        def bench(cpu, wall):
            return dict({"Stop": {"cpu_p50": cpu, "p50": wall, "p95": wall, "cpu_p95": cpu, "load": 0, "budget": 500}},
                        **{f"h{i}": {"cpu_p50": 50.0, "p50": 50.0, "p95": 50.0, "cpu_p95": 50.0, "load": 0, "budget": 500}
                           for i in range(6)})
        base = [bench(100.0, 40.0), bench(102.0, 55.0), bench(100.0, 50.0)]  # CPU 2% apart, wall 37% apart
        from sampo import selfgate as g
        self.assertLess(g._spread([b for b in base])["Stop"], 0.05)
        rows, _ = self.pairs(base, [bench(120.0, 50.0), bench(122.0, 50.0), bench(120.0, 50.0)])
        self.assertIn("Stop", rows, "CPU +20% against a 2% CPU spread: held, whatever the wall clock did")

    def test_the_wall_wait_row_on_a_quiet_bench_keeps_the_wall_spread(self):
        def bench(wall):
            return dict({"Stop": {"cpu_p50": 100.0, "p50": wall, "p95": wall, "cpu_p95": 100.0, "load": 0, "budget": 500}},
                        **{f"h{i}": {"cpu_p50": 50.0, "p50": 50.0, "p95": 50.0, "cpu_p95": 50.0, "load": 0, "budget": 500}
                           for i in range(6)})
        base = [bench(40.0), bench(52.0), bench(46.0)]  # wall 30% apart
        rows, _ = self.pairs(base, [bench(55.0), bench(66.0), bench(60.0)])  # +25% on each: inside its own wall spread
        self.assertEqual(rows, {})
        rows, _ = self.pairs(base, [bench(90.0), bench(100.0), bench(95.0)])
        self.assertIn("Stop", rows)

    def test_a_held_row_prints_the_raw_percent_the_bar_and_the_divisor(self):
        import re
        base = self.flat(8)
        rows, _ = self.pairs(base, [self.scaled(b, {"h0": 1.6}) for b in base])
        self.assertRegex(rows["h0"], r"\(\+60% raw, bar \+(1[5-9]|2\d)%, machine x1\.0\)", rows)
        up = [self.scaled(b, {"*": 1.1, "h0": 1.8}) for b in base]
        rows, _ = self.pairs(base, up)
        self.assertRegex(rows["h0"], r"\(\+8\d% raw, bar \+\d+%, machine x1\.0\d*\)", rows)

    def test_a_hook_excused_by_its_spread_is_recorded_with_its_raw_rise_and_bar(self):
        from sampo import selfgate as g

        def bench(ms):
            return dict({"Stop": {"cpu_p50": ms, "p50": ms, "p95": ms, "cpu_p95": ms, "load": 0, "budget": 500}},
                        **{f"h{i}": {"cpu_p50": 50.0, "p50": 50.0, "p95": 50.0, "cpu_p95": 50.0, "load": 0, "budget": 500}
                           for i in range(6)})
        base = [bench(40.0), bench(45.0), bench(50.0)]
        seq, excused = {"base": iter(base), "new": iter([bench(48.0), bench(54.0), bench(60.0)])}, []
        with mock.patch.object(g, "_bench", lambda tree: (next(seq[tree]), {})):
            rows, _, _ = g._paired("base", "new", None, excused)
        self.assertEqual(rows, {})
        self.assertEqual([e["hook"] for e in excused], ["Stop"])
        e = excused[0]
        self.assertAlmostEqual(e["raw"], 1.2, places=2)
        self.assertAlmostEqual(e["divisor"], 1.0, places=2)
        self.assertGreater(e["bar"], 1.2)

    def test_a_hook_held_at_the_plain_bar_nowhere_is_not_recorded(self):
        from sampo import selfgate as g
        base = self.flat(8)
        seq, excused = {"base": iter(base), "new": iter([self.scaled(b, {"h0": 1.05}) for b in base])}, []
        with mock.patch.object(g, "_bench", lambda tree: (next(seq[tree]), {})):
            g._paired("base", "new", None, excused)
        self.assertEqual(excused, [])

    def test_the_gate_logs_each_excused_hook_and_says_how_many_on_one_line(self):
        import io
        from contextlib import redirect_stderr
        entry = {"hook": "Stop", "raw": 1.2, "divisor": 1.0, "bar": 1.4}

        def paired(base, new, cache=None, excused=None):
            excused.append(entry)
            excused.append(dict(entry, hook="Notification", raw=1.31))
            return {}, {}, {}
        err = io.StringIO()
        with redirect_stderr(err):
            out, _, events = GateEndToEnd().run_gate("plugin/lib/json.py", "x = 1\n", paired=paired)
        self.assertEqual(out, [], "an excused hook is a pass, not a refusal")
        self.assertEqual(events.count("self_gate_excused"), 2)
        lines = [x for x in err.getvalue().splitlines() if x.startswith("self-change gate:")]
        self.assertEqual(len(lines), 1, err.getvalue())
        line = lines[0]
        self.assertIn("2 hooks", line)
        self.assertIn("+31%", line)
        self.assertIn("Notification", line)

    def test_a_pair_that_ran_loaded_alone_does_not_excuse_a_uniform_rise(self):
        loaded = lambda b: {k: dict(v, load=3.0) for k, v in b.items()}  # noqa: E731
        quiet = lambda b: {k: dict(v, load=0.2) for k, v in b.items()}  # noqa: E731
        base = [loaded(self.aa("1a")), quiet(self.aa("2a")), quiet(self.aa("3a"))]
        branch = [loaded(self.scaled(self.aa("1b"), {"*": 1.4})), quiet(self.scaled(self.aa("2b"), {"*": 1.4})),
                  quiet(self.scaled(self.aa("3b"), {"*": 1.4}))]
        rows, _ = self.pairs(base, branch)
        self.assertGreaterEqual(len(rows), 10, "the divisor needs the whole window loaded, not one pair of it")

    def test_a_nan_never_silences_a_hooks_rise(self):
        from sampo import selfgate as g
        nan = float("nan")
        base = self.flat(8)
        self.assertEqual(g._spread([{"Stop": {"cpu_p50": nan}}, {"Stop": {"cpu_p50": 10.0}}]), {})
        one = self.scaled(base[0], {"*": 1.35})
        one["h7"] = dict(one["h7"], cpu_p50=nan, cpu_p95=nan)
        self.assertTrue(1.0 <= g._machine(base[0], one, {k: 0.3 for k in one}) <= 1.3)
        rows, _ = self.pairs(base, [dict(self.scaled(b, {"h0": 1.6}), h7=dict(b["h7"], cpu_p50=nan, cpu_p95=nan)) for b in base])
        self.assertIn("h0", rows)

    def test_one_hook_up_60_percent_in_every_pair_still_refuses(self):
        rows = self.replay({"ratio": {"PreCompact": 1.6}, "pairs": [0, 1, 2]})
        self.assertEqual(list(rows), ["PreCompact"], rows)

    def test_one_hook_up_60_percent_among_a_machine_up_20_percent_still_refuses(self):
        rows = self.replay({"ratio": {"PreCompact": 1.6 * 1.2, "*": 1.2}, "pairs": [0, 1, 2]})
        self.assertEqual(list(rows), ["PreCompact"], rows)
        self.assertIn("machine", rows["PreCompact"])

    def test_a_uniform_rise_on_a_quiet_bench_is_the_branch_not_the_machine(self):
        quiet = lambda b: {k: dict(v, load=0.2) for k, v in b.items()}  # noqa: E731
        base = [quiet(self.aa(f"{i}a")) for i in (1, 2, 3)]
        branch = [quiet(self.scaled(self.aa(f"{i}b"), {"*": 1.35})) for i in (1, 2, 3)]
        rows, _ = self.pairs(base, branch)
        self.assertGreaterEqual(len(rows), 10, "every hook up 35% with nothing else running: a shared import, held")

    def test_a_hook_that_wobbles_in_the_bases_own_benches_needs_a_rise_past_its_spread(self):
        def bench(ms, others=()):
            return dict({"Stop": {"cpu_p50": ms, "p50": ms, "p95": ms, "cpu_p95": ms, "load": 0, "budget": 500}},
                        **{f"h{i}": {"cpu_p50": 50.0, "p50": 50.0, "p95": 50.0, "cpu_p95": 50.0, "load": 0, "budget": 500}
                           for i in range(6)})
        base = [bench(40.0), bench(45.0), bench(50.0)]  # the base against itself: 25% apart
        rows, _ = self.pairs(base, [bench(48.0), bench(54.0), bench(60.0)])  # +20% each pair
        self.assertEqual(rows, {}, "a +20% rise from a hook that moves 25% on its own is not a finding")
        rows, _ = self.pairs(base, [bench(70.0), bench(75.0), bench(80.0)])  # +60%+: past RISE plus the spread
        self.assertIn("Stop", rows)

    def test_a_steady_hook_still_holds_at_fifteen_percent(self):
        steady = {"Stop": {"cpu_p50": 100.0, "p50": 100.0, "p95": 100.0, "cpu_p95": 100.0, "load": 0, "budget": 500}}
        rows, _ = self.pairs([steady] * 3, [self.scaled(steady, {"Stop": 1.2})] * 3)
        self.assertIn("Stop", rows)

    def test_the_spread_is_capped_so_a_noisy_base_cannot_excuse_everything(self):
        from sampo import selfgate as g
        wild = [{"Stop": {"cpu_p50": x, "load": 0}} for x in (10.0, 30.0, 90.0)]
        self.assertEqual(g._spread(wild), {"Stop": g.SPREAD_CAP})

    def test_the_machine_ratio_needs_enough_hooks_and_a_loaded_bench(self):
        from sampo import selfgate as g
        one = {"Stop": {"cpu_p50": 100.0, "load": 3.0}}
        self.assertEqual(g._machine(one, {"Stop": {"cpu_p50": 200.0, "load": 3.0}}), 1.0, "one hook: no median to trust")
        base = self.aa("1a")
        wide = {k: 0.5 for k in base}
        self.assertAlmostEqual(g._machine(base, self.scaled(base, {"*": 1.35}), wide), 1.35, places=2)
        calm = {k: dict(v, load=0.1) for k, v in base.items()}
        self.assertEqual(g._machine(calm, self.scaled(calm, {"*": 1.35}), wide), 1.0)

    def test_the_spread_and_the_machine_cost_no_extra_bench_and_microseconds(self):
        import time
        rows, calls = self.pairs([self.aa(f"{i}a") for i in (1, 2, 3)], [self.aa(f"{i}b") for i in (1, 2, 3)])
        self.assertEqual(calls, ["base", "new"] * 3, "the same six benches as before: the spread comes from the base's own three")
        from sampo import selfgate as g
        a, z = self.aa("1a"), self.aa("1b")
        t = time.perf_counter()
        for _ in range(100):
            sp = g._spread([a, a, a])
            g._slower(a, z, spread=sp, machine=g._machine(a, z, sp))
        self.assertLess((time.perf_counter() - t) / 100, 0.01, "well under 10 ms per pair")


class WhichDiffsAreTimed(unittest.TestCase):  # T-3139: time hooks only when the diff touches what a hook process imported
    MAP = {"Stop": ["plugin/lib/sampo/hooks.py", "plugin/lib/sampo/core.py"], "PostToolUse": ["plugin/lib/sampo/core.py"]}

    def timed(self, *files, hmap=None):
        from sampo import selfgate as g
        return g.needs_timing(list(files), self.MAP if hmap is None else hmap)

    def test_a_page_script_no_hook_reads_skips_timing(self):
        self.assertFalse(self.timed("plugin/web/live/live.js"))

    def test_a_module_a_hook_imported_is_timed(self):
        self.assertTrue(self.timed("plugin/lib/sampo/hooks.py"))

    def test_a_module_no_hook_imported_skips_timing(self):
        self.assertFalse(self.timed("plugin/lib/sampo/louhi.py"))

    def test_a_top_level_lib_file_can_shadow_the_stdlib_so_is_timed(self):  # lib/ is first on the hook's sys.path
        self.assertTrue(self.timed("plugin/lib/json.py"))
        self.assertTrue(self.timed("plugin/lib/fmguard.py", hmap={"Stop": ["plugin/lib/sampo/core.py"]}))

    def test_the_cache_refreshes_on_the_bench_harness_but_not_the_changelog(self):
        from sampo import selfgate as g
        m = self.MAP
        for f in ("plugin/tests/bench_hooks.py", "plugin/tests/fixtures/hook_payloads.json",
                  "plugin/tests/fixtures/other.json", "plugin/lib/sampo/hooks.py"):
            self.assertTrue(g._refresh([f], m), f)
        self.assertFalse(g._refresh(["CHANGELOG.md", "plugin/web/live/live.js"], m))

    def test_a_hook_entry_hooks_json_or_data_file_is_timed(self):
        for f in ("plugin/hooks/hook", "plugin/hooks/hooks.json", "plugin/settings.json", "plugin/lib/sampo/data.json",
                  "plugin/bin/sampo", "CHANGELOG.md"):
            self.assertTrue(self.timed(f), f)

    def test_one_mapped_file_among_unmapped_ones_is_timed(self):
        self.assertTrue(self.timed("plugin/web/live/live.js", "plugin/lib/sampo/core.py"))

    def test_an_empty_map_times_as_before(self):
        self.assertTrue(self.timed("plugin/web/live/live.js", hmap={}))

    def test_the_map_is_reused_until_a_hook_file_changes_then_unioned(self):
        import os
        import tempfile
        from types import SimpleNamespace
        from sampo import selfgate as g
        p = SimpleNamespace(dir=tempfile.mkdtemp())
        runs = [{"Stop": ["plugin/lib/sampo/a.py"]}, {"Stop": ["plugin/lib/sampo/b.py"], "Pre": ["plugin/lib/sampo/c.py"]}]
        changed = [["plugin/web/x.js", "CHANGELOG.md"], ["plugin/lib/sampo/a.py"]]
        with mock.patch.object(g, "_hook_modules", side_effect=runs) as run, \
                mock.patch.object(g, "_changed", side_effect=changed):
            first = g.hook_map(p, "m", "s1", "base")
            self.assertEqual(g.hook_map(p, "m", "s2", "base"), first, "no mapped file changed: reused")
            self.assertEqual(run.call_count, 1)
            third = g.hook_map(p, "m", "s3", "base")
        self.assertEqual(run.call_count, 2)
        self.assertEqual(third["Stop"], ["plugin/lib/sampo/a.py", "plugin/lib/sampo/b.py"], "union over runs")
        self.assertIn("Pre", third)
        self.assertTrue(os.path.isfile(os.path.join(p.dir, "hook-modules.json")))

    def test_concurrent_holds_each_write_the_map_and_the_base_cache_through_a_temp_file_of_their_own(self):
        """T-6031: the push hold judges four commits at once, each saving hook-modules.json and the base cache: one shared
        ".tmp" name let a writer replace another's half-written file."""
        import os
        import tempfile
        import threading
        from types import SimpleNamespace
        from sampo import selfgate as g
        p = SimpleNamespace(dir=tempfile.mkdtemp())
        real, moved, lock = os.replace, [], threading.Lock()
        barrier = threading.Barrier(4, timeout=20)

        def replace(src, dst):
            with lock:
                moved.append((src, dst))
            return real(src, dst)

        def meet(base):
            barrier.wait()
            return {}

        def work(i):
            g.hook_map(p, "m", f"s{i}", "base")
            g._save(p, "key", {"verdicts": {str(i): "allow"}}, "base-cache.json")
        with mock.patch.object(g.os, "replace", replace), mock.patch.object(g, "_secret", lambda: b"k" * 32), \
                mock.patch.object(g, "_hook_modules", meet):
            threads = [threading.Thread(target=work, args=(i,)) for i in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        for name in ("hook-modules.json", "base-cache.json"):
            tmps = [s for s, d in moved if os.path.basename(d) == name]
            self.assertEqual(len(tmps), 4, name)
            self.assertEqual(len(set(tmps)), 4, f"{name}: four writers, four temp files")
            with open(os.path.join(p.dir, name), encoding="utf-8") as f:
                json.load(f)
        self.assertEqual([f for f in os.listdir(p.dir) if f.endswith(".tmp")], [], "no temp file is left behind")

    def test_a_failed_map_run_is_an_empty_map(self):
        import tempfile
        from types import SimpleNamespace
        from sampo import selfgate as g
        with mock.patch.object(g, "_hook_modules", side_effect=RuntimeError("no")):
            self.assertEqual(g.hook_map(SimpleNamespace(dir=tempfile.mkdtemp()), "m", "s1", "base"), {})


class GateEndToEnd(unittest.TestCase):  # T-3139: git-built trees, the timing bench faked
    def run_gate(self, path, text, paired=None):
        import os
        import subprocess
        import tempfile
        from types import SimpleNamespace
        from sampo import selfgate as g
        main = tempfile.mkdtemp()
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x")

        def git(*a):
            return subprocess.run(["git", "-C", main, *a], check=True, capture_output=True, text=True, env=env).stdout.strip()
        git("init", "-q", "-b", "main")
        for rel in ("plugin/lib/fmguard.py", "plugin/lib/sampo/core.py", "plugin/web/live/live.js"):
            os.makedirs(os.path.dirname(os.path.join(main, rel)), exist_ok=True)
            open(os.path.join(main, rel), "w").write("x = 1\n")
        git("add", "-A")
        git("commit", "-q", "-m", "base")
        os.makedirs(os.path.dirname(os.path.join(main, path)), exist_ok=True)
        open(os.path.join(main, path), "w").write(text)
        git("add", "-A")
        git("commit", "-q", "-m", "branch")
        tip, base = git("rev-parse", "HEAD"), git("rev-parse", "HEAD~1")
        git("checkout", "-q", base)
        p, b, events = SimpleNamespace(dir=tempfile.mkdtemp()), SimpleNamespace(id="T-9"), []
        with mock.patch.object(g, "approved", return_value=False), mock.patch.object(g, "_corpus", return_value=[]), \
                mock.patch.object(g, "_verdicts", return_value=[]), mock.patch.object(g, "_footprint", return_value={}), \
                mock.patch("sampo.doctor.grown", return_value=[]), \
                mock.patch.object(g, "_hook_modules", return_value={"Stop": ["plugin/lib/sampo/core.py"]}), \
                mock.patch.object(g, "_paired", side_effect=paired, **({} if paired else {"return_value": ({}, {}, {})})) as pm, \
                mock.patch.object(g.c, "log_event", side_effect=lambda *a, **k: events.append(a[1])):
            out = g.gate(p, b, main, tip, [path])
        return out, pm, events

    def test_a_web_only_diff_skips_the_timing_bench_and_says_so(self):
        out, paired, events = self.run_gate("plugin/web/live/live.js", "x = 2\n")
        self.assertEqual(out, [])
        paired.assert_not_called()
        self.assertIn("self_gate_timing_skipped", events)

    def test_a_lib_file_shadowing_the_stdlib_is_timed(self):
        out, paired, events = self.run_gate("plugin/lib/json.py", "x = 1\n")
        paired.assert_called_once()
        self.assertNotIn("self_gate_timing_skipped", events)


class MergedJudgment(unittest.TestCase):  # T-3647: a train lands the lane merged onto the base, so judge that, not the tip
    CASE = '{"cmd": "fm capture x", "cwd": "", "id": "c1"}\n'

    def run_gate(self, lane_edit):
        """Base HEAD tightens the guard after the lane forked; lane_edit(put) builds the lane's commit."""
        import os
        import subprocess
        import tempfile
        from types import SimpleNamespace
        from sampo import selfgate as g
        main = tempfile.mkdtemp()
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x")

        def git(*a):
            return subprocess.run(["git", "-C", main, *a], check=True, capture_output=True, text=True, env=env).stdout.strip()

        def put(rel, text):
            os.makedirs(os.path.dirname(os.path.join(main, rel)), exist_ok=True)
            open(os.path.join(main, rel), "w").write(text)
        git("init", "-q", "-b", "main")
        for rel in ("plugin/lib/fmguard.py", "plugin/lib/other.py", "plugin/lib/rule.txt"):
            put(rel, "allow\n" + "pad\n" * 5 + "allow\n" if rel.endswith("rule.txt") else "x = 1\n")
        git("add", "-A")
        git("commit", "-q", "-m", "fork")
        git("checkout", "-q", "-b", "lane")
        lane_edit(put)
        git("add", "-A")
        git("commit", "-q", "-m", "lane")
        tip = git("rev-parse", "HEAD")
        git("checkout", "-q", "main")
        put("plugin/lib/rule.txt", "block\n" + "pad\n" * 5 + "allow\n")  # the stricter guard that landed after the lane forked
        put("plugin/tests/fixtures/guard_cases.jsonl", self.CASE)
        git("add", "-A")
        git("commit", "-q", "-m", "tighten")

        def verdicts(tree, cmds):
            text = open(os.path.join(tree, "lib", "rule.txt")).read()
            return ["block" if "block" in text and "loosen" not in text else "allow"] * len(cmds)
        p, b = SimpleNamespace(dir=tempfile.mkdtemp()), SimpleNamespace(id="T-9")
        with mock.patch.object(g, "approved", return_value=False), mock.patch.object(g, "_corpus", return_value=[]), \
                mock.patch.object(g, "_verdicts", side_effect=verdicts), mock.patch.object(g, "_footprint", return_value={}), \
                mock.patch("sampo.doctor.grown", return_value=[]), mock.patch.object(g, "_hook_modules", return_value={}), \
                mock.patch.object(g, "_paired", return_value=({}, {}, {})), mock.patch.object(g.c, "log_event"):
            return g.gate(p, b, main, tip, ["plugin/lib/other.py"])

    def test_a_lane_built_before_a_tighter_guard_is_not_held_for_lacking_it(self):
        self.assertEqual(self.run_gate(lambda put: put("plugin/lib/other.py", "x = 2\n")), [])

    def test_a_lane_that_loosens_the_guard_on_top_of_the_base_is_still_held(self):
        # a clean merge: the lane edits the rule's last line, the base its first; the merged rule allows
        out = self.run_gate(lambda put: put("plugin/lib/rule.txt", "allow\n" + "pad\n" * 5 + "loosen\n"))
        self.assertTrue(any(r.startswith("guard:") and "branch allow" in r for r in out), out)

    def test_a_conflicting_lane_is_judged_by_its_tip(self):
        out = self.run_gate(lambda put: put("plugin/lib/rule.txt", "allow, loosened\n" + "pad\n" * 5 + "allow\n"))
        self.assertTrue(any(r.startswith("guard:") and "branch allow" in r for r in out), out)


class BaseCache(unittest.TestCase):  # T-3176: the base tree's bench and guard replay are kept per base tree hash
    def setUp(self):
        import os
        import tempfile
        from types import SimpleNamespace
        from sampo import selfgate as g
        self.p, self.base = SimpleNamespace(dir=tempfile.mkdtemp()), tempfile.mkdtemp()
        os.makedirs(os.path.join(self.base, "lib"))
        self.put("lib/a.py", "x = 1\n")
        self.keyfile, self.load, self.logged = os.path.join(tempfile.mkdtemp(), "selfgate.key"), 0.5, []
        for m in (mock.patch.object(g, "_keyfile", lambda: self.keyfile),
                  mock.patch.object(g, "_load_pc", lambda: self.load),
                  mock.patch.object(g.c, "log_event", lambda p, ev, **k: self.logged.append(ev))):
            m.start()
            self.addCleanup(m.stop)

    def cache_path(self):
        import os
        return os.path.join(self.p.dir, "selfgate-base.json")

    def edit_cache(self, fn):
        import json
        with open(self.cache_path()) as f:
            d = json.load(f)
        fn(d)
        with open(self.cache_path(), "w") as f:
            json.dump(d, f)

    def test_the_cache_and_key_files_are_private(self):
        import os
        self.gate_side()
        self.assertEqual(os.stat(self.cache_path()).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(self.keyfile).st_mode & 0o777, 0o600)
        self.assertEqual(len(open(self.keyfile, "rb").read()), 32)

    def test_a_forged_cache_reruns_the_base_half(self):
        def slow(d):
            d["bench"][0][0]["Stop"]["p95"] = 1.0
        for name, forge in (("a tampered bench value", slow), ("a tampered key", lambda d: d.update(key="x")),
                            ("a missing MAC", lambda d: d.pop("mac")), ("a bad MAC", lambda d: d.update(mac="0" * 64))):
            with self.subTest(name):
                import os
                if os.path.exists(self.cache_path()):
                    os.remove(self.cache_path())
                self.gate_side()
                self.edit_cache(forge)
                self.assertEqual(self.gate_side()[0], 3)
        self.assertIn("self_gate_cache_ignored", self.logged)

    def test_a_cache_written_with_another_key_reruns_it(self):
        import os
        self.gate_side()
        os.remove(self.keyfile)
        self.assertEqual(self.gate_side()[0], 3)

    def test_an_unsafe_key_file_ignores_the_cache(self):
        import os
        self.gate_side()
        os.chmod(self.keyfile, 0o644)
        self.assertEqual(self.gate_side()[0], 3)
        real = self.keyfile + ".real"
        os.rename(self.keyfile, real)
        os.symlink(real, self.keyfile)
        self.assertEqual(self.gate_side()[0], 3)

    def test_the_bench_is_reused_only_within_a_load_band_of_1_5(self):
        self.gate_side()  # benched at load 0.5 per CPU
        for load, reused in ((0.74, True), (0.34, True), (0.80, False), (0.32, False)):
            with self.subTest(load=load):
                self.load = load
                self.assertEqual(self.gate_side()[0], 0 if reused else 3)
                self.load = 0.5
                self.gate_side()

    def put(self, rel, text):
        import os
        with open(os.path.join(self.base, rel), "w") as f:
            f.write(text)

    def gate_side(self, key=None):
        """One gate's base half with a fake bench and replay; returns (bench calls on base, replayed cmds, rows)."""
        from sampo import selfgate as g
        key = key or g._key(self.base)
        cache = g._load(self.p, key)
        seen = {"base": 0, "cmds": []}

        def bench(tree):
            seen["base"] += tree == "base"
            return {"Stop": {"p95": 100.0, "load": 0, "budget": 500}}, {}

        def verdicts(tree, cmds):
            seen["cmds"] += cmds
            return ["allow"] * len(cmds)
        with mock.patch.object(g, "_bench", bench), mock.patch.object(g, "_verdicts", verdicts):
            rows = g._paired("base", "new", cache)[0]
            g._base_verdicts(self.base, [["ls", ""], ["pwd", ""]], cache)
        g._save(self.p, key, cache)
        return seen["base"], seen["cmds"], rows

    def test_a_second_gate_on_the_same_base_skips_the_base_half(self):
        self.assertEqual(self.gate_side()[:2], (3, [["ls", ""], ["pwd", ""]]))
        self.assertEqual(self.gate_side()[:2], (0, []))

    def test_a_changed_base_or_setting_reruns_it(self):
        self.gate_side()
        self.put("lib/a.py", "x = 2\n")
        self.assertEqual(self.gate_side()[0], 3)
        from sampo import selfgate as g
        with mock.patch.object(g, "RUNS", 5):
            self.assertEqual(self.gate_side()[0], 3)

    def test_tests_and_pycache_do_not_change_the_key(self):
        import os
        from sampo import selfgate as g
        k = g._key(self.base)
        os.makedirs(os.path.join(self.base, "tests"))
        os.makedirs(os.path.join(self.base, "lib", "__pycache__"))
        self.put("tests/test_x.py", "1\n")
        self.put("lib/__pycache__/a.pyc", "1\n")
        self.assertEqual(g._key(self.base), k)

    def test_a_cache_older_than_a_day_reruns_it(self):
        import time
        from sampo import selfgate as g
        self.gate_side()
        with mock.patch.object(time, "time", return_value=time.time() + 86500):
            self.assertEqual(g._load(self.p, g._key(self.base)), {})

    def test_a_new_command_is_replayed_alone(self):
        from sampo import selfgate as g
        self.gate_side()
        cache = g._load(self.p, g._key(self.base))
        with mock.patch.object(g, "_verdicts", lambda t, cmds: ["deny"] * len(cmds)) as _:
            got = g._base_verdicts(self.base, [["ls", ""], ["rm", ""]], cache)
        self.assertEqual(got, ["allow", "deny"])

    def test_a_corrupt_cache_is_empty(self):
        import os
        from sampo import selfgate as g
        with open(os.path.join(self.p.dir, "selfgate-base.json"), "w") as f:
            f.write("{")
        self.assertEqual(g._load(self.p, g._key(self.base)), {})


class KeyPath(unittest.TestCase):  # T-3192: the MAC key lives in the account's own ~/.config/sampo, whatever SAMPO_SECRETS says
    def setUp(self):
        import os
        import tempfile
        from types import SimpleNamespace
        home, elsewhere = tempfile.TemporaryDirectory(), tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.addCleanup(elsewhere.cleanup)
        self.home, self.elsewhere = home.name, elsewhere.name
        fake_pw = SimpleNamespace(pw_dir=self.home)
        env = {"SAMPO_SECRETS": os.path.join(self.elsewhere, "secrets.env"),
               "FOREMAN_SECRETS": os.path.join(self.elsewhere, "secrets.env")}
        for m in (mock.patch("pwd.getpwuid", lambda uid: fake_pw), mock.patch.dict(os.environ, env)):
            m.start()
            self.addCleanup(m.stop)

    def test_the_key_path_is_the_account_home_whatever_the_env_says(self):
        import os
        from sampo import selfgate as g
        self.assertEqual(g._keyfile(), os.path.join(self.home, ".config", "sampo", "selfgate.key"))

    def test_an_env_redirected_run_neither_reads_nor_writes_a_key_there(self):
        import os
        from sampo import selfgate as g
        planted = os.path.join(self.elsewhere, "selfgate.key")  # an agent-written key where the env points
        with open(planted, "wb") as f:
            f.write(b"k" * 32)
        key = g._secret()
        self.assertEqual(len(key), 32)
        self.assertNotEqual(key, b"k" * 32)
        self.assertTrue(os.path.isfile(os.path.join(self.home, ".config", "sampo", "selfgate.key")))
        self.assertEqual(os.listdir(self.elsewhere), ["selfgate.key"])


if __name__ == "__main__":
    unittest.main()
