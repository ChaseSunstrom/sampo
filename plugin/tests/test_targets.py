"""T-5709: outcome targets. plugin/data/targets.json names six numbers the user cares about (usage run-out, accept-to-merge,
rework rounds, tokens per landed change, Sampo cost against plain Claude Code, CPU). Each has one reader that makes one
bounded read of one state file; a regressed target files a discovered task with its numbers once a day and leads the
Next line. A provisional target only tells. The fixtures are a temp state: burn.json, cpu.json (shared, in the state
folder), targets.json and targets-filed.json (per project), a cost cache, bench results, ledger events."""
import fcntl
import json
import os
import subprocess
import time
import types
import unittest.mock

from helpers import ForemanTestCase

from sampo import cli
from sampo import core as c
from sampo import cost, cpu, selfcheck, targets

KEYS = ["usage_run_out", "accept_to_merge", "rework_rounds", "tokens_per_change", "sampo_vs_bare", "cpu"]
PROVISIONAL = {"accept_to_merge", "tokens_per_change"}  # starting values, marked provisional in targets.json
DAY = 86400


class Targets(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)
        self.now = time.time()

    def state(self, name):
        """A file shared by every project of this foreman (burn.json, cpu.json)."""
        return os.path.join(c.state_dir(), name)

    def pstate(self, name):
        """A file of this project (targets.json, targets-filed.json)."""
        return os.path.join(self.p.dir, name)

    def write_json(self, path, obj):
        with open(path, "w") as f:
            json.dump(obj, f)

    def write_state(self, name, obj):
        self.write_json(self.state(name), obj)

    def values(self, at=None, **vals):
        """targets.json as refresh writes it: the derived values, stamped at (default now), so a check does not redo them."""
        self.write_json(self.pstate("targets.json"), {"ts": self.now if at is None else at, "values": vals})

    def write_costs(self, costs):
        os.makedirs(os.path.dirname(cost._cache_path(self.p)), exist_ok=True)
        self.write_json(cost._cache_path(self.p), {"size": 0, "ts": self.now, "costs": costs})

    def row(self, name, now=None):
        return next(r for r in targets.evaluate(self.p, self.now if now is None else now) if r["name"] == name)

    def test_the_table_names_the_six_targets_with_a_target_and_a_unit(self):
        rows = targets.load()
        self.assertEqual([r["name"] for r in rows], KEYS)
        for r in rows:
            self.assertTrue(r["label"] and r["unit"] and isinstance(r["target"], (int, float)), r)
            self.assertIn(r["reader"], targets.READERS, r)
        sampo = next(r for r in rows if r["name"] == "sampo_vs_bare")
        self.assertEqual(sampo["target"], 0.5, "the user's 2026-11-22 bar: at most half of plain Claude Code's cost")
        self.assertEqual({r["name"] for r in rows if r.get("provisional")}, PROVISIONAL,
                         "the two starting values are provisional, the rest are not")

    def test_each_target_is_ok_or_regressed_or_unmeasured(self):
        self.assertEqual(self.row("usage_run_out")["status"], "unmeasured", "no burn.json is no reading, never ok")
        self.write_state("burn.json", {"ts": self.now, "found": []})
        self.assertEqual(self.row("usage_run_out")["status"], "ok")
        self.write_state("burn.json", {"ts": self.now, "found": [["weekly usage is on pace to run out 93 h before its reset", "x"]]})
        r = self.row("usage_run_out")
        self.assertEqual((r["status"], r["actual"]), ("regressed", 93.0))

        self.values(accept_to_merge=12.0, rework_rounds=0.5, tokens_per_change=1_000_000, sampo_vs_bare=0.42)
        self.assertEqual(self.row("accept_to_merge")["status"], "ok")
        self.assertEqual(self.row("rework_rounds")["status"], "ok")
        self.assertEqual(self.row("tokens_per_change")["status"], "ok")
        self.assertEqual(self.row("sampo_vs_bare")["status"], "ok")
        self.values(accept_to_merge=45.0, rework_rounds=3.0, tokens_per_change=6_000_000, sampo_vs_bare=1.0)
        for name, actual in (("accept_to_merge", 45.0), ("rework_rounds", 3.0), ("tokens_per_change", 6_000_000),
                             ("sampo_vs_bare", 1.0)):
            r = self.row(name)
            self.assertEqual((r["status"], r["actual"]), ("regressed", actual), r)
        self.assertEqual(self.row("sampo_vs_bare", now=self.now)["target"], 0.5)

    def test_the_burn_forecast_has_one_freshness_rule_for_the_target_and_the_capture(self):
        found = [["weekly usage is on pace to run out 93 h before its reset", "x"]]
        self.write_state("burn.json", {"ts": self.now - 12 * 3600, "found": found})
        self.assertEqual(self.row("usage_run_out")["actual"], 93.0, "twelve hours old is still this day's forecast")
        self.assertIsNotNone(selfcheck.usage_burn(self.p, [], self.now).action)
        self.write_state("burn.json", {"ts": self.now - 2 * DAY, "found": found})
        self.assertEqual(self.row("usage_run_out")["status"], "unmeasured", "two days old is no reading")
        self.assertIsNone(selfcheck.usage_burn(self.p, [], self.now))

    def test_cpu_is_the_rate_of_the_newest_window_over_the_cores(self):
        cores = 4
        with unittest.mock.patch.object(cpu, "ncores", return_value=cores):
            # 20 % of 4 cores over 10 minutes is 480 s of CPU time (the counter is in microseconds)
            self.write_state("cpu.json", {"samples": {"a.scope": [[self.now - 600, 0], [self.now, 480_000_000]]}})
            r = self.row("cpu")
            self.assertEqual((r["status"], round(r["actual"])), ("regressed", 20))
            self.write_state("cpu.json", {"samples": {"a.scope": [[self.now - 600, 0], [self.now, 120_000_000]]}})
            self.assertEqual(self.row("cpu")["status"], "ok")
            self.write_state("cpu.json", {"samples": {}})
            self.assertEqual(self.row("cpu")["status"], "unmeasured")

    def test_refresh_derives_accept_to_merge_rework_tokens_and_the_bench_ratio(self):
        tid = "T-9001"
        c.log_event(self.p, "accepted", task=tid, data={"at": self.now - 45 * 60})
        c.log_event(self.p, "lane_landed", task=tid, data={"base": "a", "tip": "b", "tasks": [tid]})
        for _ in range(3):
            c.log_event(self.p, "changes_asked", task=tid, data={})
        self.write_costs({tid: 6_000_000})
        bench = os.path.join(self.p.dir, "bench", "results")
        os.makedirs(bench, exist_ok=True)
        # the real shape (bench/results/duel-*.json): a duel pair labelled by arm; an evolve result is no pair
        self.write_json(os.path.join(bench, "duel-none-foreman-1.json"),
                        {"label": "duel-none-foreman-1", "cases": [{"cost_usd": 0.5, "pass": True}, {"cost_usd": 0.7, "pass": True}]})
        self.write_json(os.path.join(bench, "duel-none-bare-1.json"),
                        {"label": "duel-none-bare-1", "bare": True, "cases": [{"cost_usd": 0.6, "pass": True}, {"cost_usd": 0.6, "pass": True}]})
        self.write_json(os.path.join(bench, "evolve-1.json"), {"label": "evolve-1", "cases": [{"cost_usd": 90.0, "pass": True}]})
        out = targets.refresh(self.p, self.now, force=True)
        v = out["values"]
        self.assertAlmostEqual(v["accept_to_merge"], 45.0, delta=1.0)
        self.assertEqual(v["rework_rounds"], 3.0)
        self.assertEqual(v["tokens_per_change"], 6_000_000)
        # sampo 1.2 $ over 2 passes against bare 1.2 $ over 2: the same cost per landed change, a ratio of 1.0 (over the 0.5 bar)
        self.assertAlmostEqual(v["sampo_vs_bare"], 1.0, places=3)
        with open(self.pstate("targets.json")) as f:
            self.assertEqual(json.load(f)["values"], v, "refresh writes what it returns")

    def test_accept_to_merge_uses_the_first_accept_stamp_not_a_re_accept(self):
        tid = "T-9001"
        c.log_event(self.p, "accepted", task=tid, data={"at": self.now - 3600})
        c.log_event(self.p, "accepted", task=tid, data={"at": self.now - 60})  # a re-accept after changes were asked
        c.log_event(self.p, "lane_landed", task=tid, data={"tasks": [tid]})
        v = targets.refresh(self.p, self.now, force=True)["values"]
        self.assertAlmostEqual(v["accept_to_merge"], 60.0, delta=1.5, msg="the first accept stamp, an hour before landing")

    def test_tokens_per_change_skips_changes_with_no_cost_and_says_how_many(self):
        for tid in ("T-9001", "T-9002"):
            c.log_event(self.p, "lane_landed", task=tid, data={"tasks": [tid]})
        self.write_costs({"T-9001": 6_000_000})  # T-9002 has no cost: left out of the average, not counted as zero
        v = targets.refresh(self.p, self.now, force=True)["values"]
        self.assertEqual(v["tokens_per_change"], 6_000_000)
        self.assertIn("1 of 2", v["notes"]["tokens_per_change"])
        self.assertIn("1 of 2", self.row("tokens_per_change")["note"])

    def test_refresh_is_bounded_no_brief_read_and_single_flight(self):
        tid = "T-9001"
        c.log_event(self.p, "accepted", task=tid, data={"at": self.now - 600})
        c.log_event(self.p, "lane_landed", task=tid, data={"tasks": [tid]})
        boom = AssertionError("refresh read a brief: every archived brief would be parsed on each scan")
        with unittest.mock.patch.object(c, "load_briefs", side_effect=boom), \
                unittest.mock.patch.object(c, "find_brief", side_effect=boom), \
                unittest.mock.patch.object(c, "_read_brief", side_effect=boom):
            v = targets.refresh(self.p, self.now, force=True)["values"]
        self.assertIn("accept_to_merge", v)
        # single flight: a scan another process holds is not run again; the caller gets the file as it is, quickly
        self.write_json(self.pstate("targets.json"), {"ts": self.now - 2 * targets.CACHE_S, "values": {"rework_rounds": 0.5}})
        held = open(os.path.join(self.p.dir, targets.SCAN_LOCK), "a")
        fcntl.flock(held, fcntl.LOCK_EX)
        try:
            t0 = time.perf_counter()
            with unittest.mock.patch.object(targets, "_derive", side_effect=boom):
                out = targets.refresh(self.p, self.now, force=True)
            self.assertLess(time.perf_counter() - t0, 0.05, "a held scan returns at once")
        finally:
            held.close()
        self.assertEqual(out["values"], {"rework_rounds": 0.5}, "the held scan's file is the answer")

    def test_regression_files_a_discovered_capture_with_its_numbers(self):
        self.values(accept_to_merge=12.0, rework_rounds=0.5, tokens_per_change=1_000_000, sampo_vs_bare=1.0)
        self.write_state("cpu.json", {"samples": {}})
        hit = selfcheck.targets_regressed(self.p, [], self.now)
        self.assertIsInstance(hit, selfcheck.Hit, hit)
        self.assertEqual(hit.action[-4:-1], ["capture", "--source", "discovered"], hit.action)
        title = hit.action[-1]
        self.assertTrue(title.startswith("Outcome target regressed: sampo_vs_bare"), title)
        self.assertIn("1", title)
        self.assertTrue(hit.lead, "a regressed target sorts first in sampo next")
        self.assertIn("Outcome target regressed: sampo_vs_bare", hit)

    def test_regression_files_one_capture_per_target_per_day_even_when_the_capture_folds(self):
        self.values(accept_to_merge=12.0, rework_rounds=0.5, tokens_per_change=1_000_000, sampo_vs_bare=1.0)
        self.write_state("cpu.json", {"samples": {}})
        spawned = []
        out = selfcheck.act(self.p, [], self.now, checks=(selfcheck.targets_regressed,),
                            spawn=lambda p, argv: spawned.append(argv) or 4242, tell=lambda p, text: None)
        self.assertEqual([(k, what) for k, what, _ in out], [("targets_regressed", "act")], out)
        self.assertEqual(len(spawned), 1)
        with open(self.pstate("targets-filed.json")) as f:
            self.assertIn("sampo_vs_bare", json.load(f), "the filing is recorded per target, in the project's folder")
        # the record is not the brief: whatever the capture became (folded into another, held, refused) the day's one
        # capture is not filed again; a later scan with the same numbers files again only after a day
        self.values(at=self.now + 3600, accept_to_merge=12.0, rework_rounds=0.5, tokens_per_change=1_000_000, sampo_vs_bare=1.0)
        self.assertIsNone(selfcheck.targets_regressed(self.p, [], self.now + 3600), "filed an hour ago: quiet")
        self.values(at=self.now + DAY + 60, accept_to_merge=12.0, rework_rounds=0.5, tokens_per_change=1_000_000, sampo_vs_bare=1.0)
        self.assertIsNotNone(selfcheck.targets_regressed(self.p, [], self.now + DAY + 60), "filed yesterday: file again")

    def test_regression_usage_run_out_acts_not_only_tells(self):
        self.write_state("burn.json", {"ts": self.now, "found": [["weekly usage is on pace to run out 93 h before its reset", "x"]]})
        hit = selfcheck.usage_burn(self.p, [], self.now)
        self.assertTrue(hit.tell and hit.lead, hit)
        self.assertEqual(hit.action[-4:-1], ["capture", "--source", "discovered"], hit.action)
        self.assertIn("usage_run_out", hit.action[-1])
        selfcheck.act(self.p, [], self.now, checks=(selfcheck.usage_burn,), spawn=lambda p, argv: 4242, tell=lambda p, t: None)
        again = selfcheck.usage_burn(self.p, [], self.now + 60)
        self.assertTrue(again.tell and again.action is None, "still told, not filed twice in a day")
        self.values(accept_to_merge=12.0, rework_rounds=0.5, tokens_per_change=1_000_000, sampo_vs_bare=0.4)
        self.write_state("cpu.json", {"samples": {}})
        self.assertIsNone(selfcheck.targets_regressed(self.p, [], self.now),
                          "usage run-out is filed by usage_burn alone: no second capture from the targets check")

    def test_regression_provisional_targets_only_tell(self):
        self.values(accept_to_merge=12.0, rework_rounds=0.5, tokens_per_change=6_000_000, sampo_vs_bare=0.4)
        self.write_state("cpu.json", {"samples": {}})
        hit = selfcheck.targets_regressed(self.p, [], self.now)
        self.assertIsNotNone(hit, "the provisional miss is said")
        self.assertIsNone(hit.action, "a provisional target does not file")
        self.assertFalse(hit.lead, "nor lead next")
        self.assertTrue(hit.tell)
        self.assertIn("tokens_per_change", hit)
        self.assertIn("(provisional)", hit)
        self.assertTrue(self.row("tokens_per_change")["provisional"])
        # a firm target past its target still files, and the provisional one does not take its place
        self.values(accept_to_merge=12.0, rework_rounds=0.5, tokens_per_change=6_000_000, sampo_vs_bare=1.0)
        firm = selfcheck.targets_regressed(self.p, [], self.now)
        self.assertEqual(firm.action[-4:-1], ["capture", "--source", "discovered"], firm.action)
        self.assertTrue(firm.lead)
        self.assertIn("sampo_vs_bare", firm.action[-1])
        self.assertNotIn("(provisional)", firm.action[-1])

    def test_regression_leads_the_next_line(self):
        def plain(p, briefs, now):
            return selfcheck.Hit("an ordinary line")

        def lead(p, briefs, now):
            return selfcheck.Hit("Outcome target regressed: cpu 20%", lead=True)

        with unittest.mock.patch.object(selfcheck, "CHECKS", (plain, lead)), \
                unittest.mock.patch.object(selfcheck, "LIVE", ()):
            lines = selfcheck.lines(self.p, [])
        self.assertEqual(lines[0], "Outcome target regressed: cpu 20%", lines)

    def test_regression_leads_the_next_action_before_its_action_text(self):
        with unittest.mock.patch.object(selfcheck, "split", return_value=(["Outcome target regressed: cpu 20%"], ["an ordinary line"])):
            _, _, action = c.next_for(self.p, [], checks=True)
        self.assertIn("Outcome target regressed: cpu 20% — ", action, action)
        self.assertLess(action.index("Outcome target regressed"), action.index("self-check: an ordinary line"), action)

    def test_readers_are_one_bounded_read_under_50_ms_and_touch_no_ledger(self):
        big = [[f"weekly usage is on pace to run out {n} h before its reset", "x"] for n in range(2000)]
        self.write_state("burn.json", {"ts": self.now, "found": big})
        self.values(accept_to_merge=12.0, rework_rounds=0.5, tokens_per_change=1_000_000, sampo_vs_bare=0.4)
        self.write_state("cpu.json", {"samples": {f"s{i}.scope": [[self.now - 1800 + k * 60, k * 1_000_000] for k in range(30)]
                                                   for i in range(200)}})
        with unittest.mock.patch.object(c, "ledger_events", side_effect=AssertionError("a reader read the ledger")), \
                unittest.mock.patch.object(subprocess, "run", side_effect=AssertionError("a reader ran a process")):
            for name, reader in targets.READERS.items():
                worst = 0.0
                for _ in range(3):
                    t0 = time.perf_counter()
                    reader(self.p, self.now)
                    worst = max(worst, time.perf_counter() - t0)
                self.assertLess(worst, 0.05, f"{name} took {worst * 1000:.1f} ms")

    def test_sampo_targets_prints_each_target_with_actual_target_and_status(self):
        self.values(accept_to_merge=12.0, rework_rounds=0.5, tokens_per_change=6_000_000, sampo_vs_bare=1.0)
        self.write_state("burn.json", {"ts": self.now, "found": []})
        self.write_state("cpu.json", {"samples": {}})
        out = self.fm("targets").stdout
        for label in ("usage run-out", "accept-to-merge", "rework", "tokens per landed change", "Sampo", "CPU"):
            self.assertIn(label, out, out)
        self.assertIn("regressed", out)
        self.assertIn("(provisional)", out, "the provisional target says so")
        rows = json.loads(self.fm("targets", "--json").stdout)
        self.assertEqual([r["name"] for r in rows], KEYS)
        self.assertEqual(next(r for r in rows if r["name"] == "sampo_vs_bare")["status"], "regressed")
        self.assertTrue(next(r for r in rows if r["name"] == "tokens_per_change")["provisional"])

    def test_sampo_targets_falls_back_to_the_cache_when_the_refresh_fails(self):
        self.write_json(self.pstate("targets.json"), {"ts": self.now - 2 * targets.CACHE_S,
                                                       "values": {"sampo_vs_bare": 1.0}})
        with unittest.mock.patch.object(cli, "resolve", return_value=self.p), \
                unittest.mock.patch.object(targets, "refresh", side_effect=RuntimeError("the scan broke")), \
                unittest.mock.patch.object(cli, "out") as out:
            targets.cmd_targets(types.SimpleNamespace(json=True))
        rows = out.call_args[0][1]
        self.assertEqual(next(r for r in rows if r["name"] == "sampo_vs_bare")["actual"], 1.0,
                         "the cache's value is what the verb prints")
