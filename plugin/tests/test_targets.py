"""T-5709: outcome targets. plugin/data/targets.json names six numbers the user cares about (usage run-out, accept-to-merge,
rework rounds, tokens per landed change, Sampo cost against plain Claude Code, CPU). Each has one reader that makes one
bounded read of one state file; a regressed target files a discovered task with its numbers once a day and leads the
Next line. A provisional target only tells. The fixtures are a temp state: burn.json, cpu.json (shared, in the state
folder), targets.json and targets-filed.json (per project), a cost cache, bench results, ledger events."""
import contextlib
import fcntl
import io
import json
import os
import shutil
import signal
import subprocess
import threading
import time
import types
import unittest.mock

from helpers import ForemanTestCase

from sampo import cli
from sampo import core as c
from sampo import cost, cpu, selfcheck, targets

KEYS = ["usage_run_out", "accept_to_merge", "rework_rounds", "tokens_per_change",
        "push_lag", "lead_context", "compactions", "disk_growth", "steers",  # T-5973: what a user steered on
        "sampo_vs_bare", "cpu"]
PROVISIONAL = {"accept_to_merge", "tokens_per_change", "disk_growth"}  # starting values, marked provisional in targets.json
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

    def test_the_table_names_the_targets_with_a_target_and_a_unit(self):
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


class Steered(ForemanTestCase):
    """T-5973: a target for each thing the user had to steer on (push lag, lead context per turn, compactions, Sampo's disk
    footprint, steers) and tokens per change from the per-lane cost reader. Their readers scan transcripts, git and the disk,
    so refresh_heavy runs them (detached, each at its own pace) and writes targets-heavy.json; the readers only read it."""

    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)
        self.now = time.time() // DAY * DAY + 12 * 3600  # noon UTC: a day's compactions never straddle midnight mid-test
        self.cfg = os.path.join(self.tmp, "cfg")  # no test reads the machine's transcripts
        patch = unittest.mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.cfg})
        patch.start()
        self.addCleanup(patch.stop)
        self.folder = cost.transcripts_dir(self.repo)
        os.makedirs(self.folder)

    def row(self, name, now=None):
        return next(r for r in targets.evaluate(self.p, self.now if now is None else now) if r["name"] == name)

    def heavy(self, *keys, now=None, force=True):
        return targets.refresh_heavy(self.p, self.now if now is None else now, keys=keys or None, force=force)

    def write_lines(self, path, rows):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("".join(json.dumps(r) + "\n" for r in rows))

    def ts(self, ago_s):
        return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(self.now - ago_s))

    # ----- push lag -----

    def git(self, *args, when=None):
        env = dict(os.environ, GIT_COMMITTER_DATE=c.iso(when), GIT_AUTHOR_DATE=c.iso(when)) if when else None
        return subprocess.run(["git", "-C", self.repo, *args], check=True, capture_output=True, text=True, env=env).stdout

    def commit(self, name, hours_ago):
        with open(os.path.join(self.repo, name), "w") as f:
            f.write(name)
        self.git("add", name)
        self.git("commit", "-qm", name, when=self.now - hours_ago * 3600)

    def origin(self):
        bare = os.path.join(self.tmp, "origin.git")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", bare], check=True)
        self.git("remote", "add", "origin", bare)
        self.git("push", "-q", "origin", "main")

    def test_push_lag_is_the_age_of_the_oldest_commit_origin_main_lacks(self):
        self.origin()
        self.heavy("push_lag")
        r = self.row("push_lag")
        self.assertEqual((r["status"], r["actual"]), ("ok", 0.0), "nothing unpushed is no lag")
        self.commit("a.txt", 30)  # the ship died here: a day and a half of commits never reached origin
        self.commit("b.txt", 3)
        self.heavy("push_lag")
        r = self.row("push_lag")
        self.assertEqual(r["status"], "regressed", r)
        self.assertAlmostEqual(r["actual"], 30.0, delta=0.1, msg="the OLDEST unpushed commit, not the newest")
        self.assertIn("2 commit", r["note"])
        self.git("push", "-q", "origin", "main")
        self.heavy("push_lag")
        self.assertEqual(self.row("push_lag")["actual"], 0.0)

    def test_push_lag_ignores_a_commit_whose_patch_origin_already_has(self):
        self.origin()
        self.commit("a.txt", 40)
        self.git("push", "-q", "origin", "main")
        self.git("commit", "-q", "--amend", "-m", "a again", when=self.now - 40 * 3600 + 5)  # same patch, a new hash: a rebase
        self.heavy("push_lag")
        self.assertEqual(self.row("push_lag")["actual"], 0.0, "git cherry knows the patch is on origin")

    def test_push_lag_with_no_origin_is_unmeasured_never_ok(self):
        self.heavy("push_lag")
        r = self.row("push_lag")
        self.assertEqual((r["status"], r["actual"]), ("unmeasured", None))
        self.assertIn("origin", r["note"])

    def test_a_push_lag_breach_in_a_fixture_repo_files_one_task(self):
        self.origin()
        self.commit("a.txt", 26)
        self.heavy("push_lag")
        spawned = []
        out = selfcheck.act(self.p, [], self.now, checks=(selfcheck.targets_regressed,),
                            spawn=lambda p, argv: spawned.append(argv) or 4242, tell=lambda p, text: None)
        self.assertEqual([(k, what) for k, what, _ in out], [("targets_regressed", "act")], out)
        self.assertEqual(len(spawned), 1, spawned)
        self.assertEqual(spawned[0][-4:-1], ["capture", "--source", "discovered"], spawned)
        self.assertTrue(spawned[0][-1].startswith("Outcome target regressed: push_lag 26"), spawned[0][-1])
        again = selfcheck.act(self.p, [], self.now + 600, checks=(selfcheck.targets_regressed,),
                              spawn=lambda p, argv: spawned.append(argv) or 4243, tell=lambda p, text: None)
        self.assertEqual(len(spawned), 1, "once a day per target: a second pass files nothing")
        self.assertEqual([what for _, what, _ in again], [])

    def test_push_lag_names_no_hash_and_no_path(self):
        self.origin()
        self.commit("a.txt", 5)
        self.heavy("push_lag")
        note = self.row("push_lag")["note"]
        self.assertNotIn(self.tmp, note)
        self.assertNotRegex(note, r"[0-9a-f]{7,}")

    # ----- lead context -----

    def lead_turns(self, reads, name="lead-1", sidechain=False, ago=3600):
        rows = [{"type": "assistant", "timestamp": self.ts(ago + i), "sessionId": name,
                 "message": {"id": f"{name}-{i}", "usage": {"input_tokens": 5, "cache_read_input_tokens": r,
                                                           "cache_creation_input_tokens": 0, "output_tokens": 9},
                             "content": []}} for i, r in enumerate(reads)]
        where = os.path.join(self.folder, name, "subagents", "agent-1.jsonl") if sidechain else os.path.join(self.folder, name + ".jsonl")
        self.write_lines(where, rows)

    def test_lead_context_is_the_median_cache_read_per_lead_turn(self):
        self.lead_turns([180_000, 200_000, 160_000, 220_000, 190_000])
        self.lead_turns([900_000] * 9, name="agent-9", sidechain=True)  # a subagent's context is not the lead's
        self.heavy("lead_context")
        r = self.row("lead_context")
        self.assertEqual((r["status"], r["actual"]), ("regressed", 190_000.0), r)
        self.lead_turns([40_000, 60_000, 50_000, 70_000, 45_000])
        self.heavy("lead_context")
        self.assertEqual(self.row("lead_context")["status"], "ok")

    def test_lead_context_with_too_few_turns_is_unmeasured(self):
        self.lead_turns([180_000, 200_000])
        self.heavy("lead_context")
        r = self.row("lead_context")
        self.assertEqual((r["status"], r["actual"]), ("unmeasured", None))
        self.assertIn("2 lead turns", r["note"])

    # ----- compactions -----

    def summary(self, ago, session="s1"):
        text = "This session is being continued from a previous conversation that ran out of context. Summary: ..."
        return {"type": "user", "isCompactSummary": True, "uuid": f"{session}-sum-{ago}", "timestamp": self.ts(ago),
                "message": {"role": "user", "content": [{"type": "text", "text": text}]}}

    def chatter(self, ago, session="s1"):
        return {"type": "user", "uuid": f"{session}-u-{ago}", "timestamp": self.ts(ago),
                "message": {"role": "user", "content": "go on"}}

    def test_compactions_are_summaries_per_session_day(self):
        day = 86400
        # one session over two days with three compactions; a second session on one day with none: 3 / 3 session-days
        self.write_lines(os.path.join(self.folder, "s1.jsonl"),
                         [self.chatter(day + 100), self.summary(day + 50), self.summary(200), self.summary(100)])
        self.write_lines(os.path.join(self.folder, "s2.jsonl"), [self.chatter(300, "s2")])
        self.heavy("compactions")
        r = self.row("compactions")
        self.assertEqual((r["status"], r["actual"]), ("ok", 1.0), r)
        self.write_lines(os.path.join(self.folder, "s2.jsonl"), [self.chatter(300, "s2"), self.summary(10, "s2")])
        self.heavy("compactions")
        self.assertEqual(self.row("compactions")["actual"], 1.33, "four summaries over three session-days")
        self.assertEqual(self.row("compactions")["status"], "regressed")

    def test_compactions_count_each_summary_once_and_only_the_users_summary_entry(self):
        quoted = {"type": "user", "uuid": "q", "timestamp": self.ts(60),
                  "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content":
                              "This session is being continued from a previous conversation"}]}}
        self.write_lines(os.path.join(self.folder, "s1.jsonl"),
                         [self.chatter(500), self.summary(100), self.summary(100), quoted])  # the same uuid twice, and a quote
        self.heavy("compactions")
        self.assertEqual(self.row("compactions")["actual"], 1.0)

    def test_compactions_with_no_session_in_the_window_is_unmeasured(self):
        self.heavy("compactions")
        self.assertEqual(self.row("compactions")["status"], "unmeasured")
        self.write_lines(os.path.join(self.folder, "old.jsonl"), [self.chatter(30 * 86400)])
        self.heavy("compactions")
        self.assertEqual(self.row("compactions")["status"], "unmeasured", "a month-old session is not this week's")

    # ----- steers -----

    def book(self, rows):
        with open(os.path.join(self.p.dir, "steers.jsonl"), "w") as f:
            for i, (ago, avoidable) in enumerate(rows):
                f.write(json.dumps({"session": "s", "uuid": f"u{i}", "at": c.iso(self.now - ago), "text": f"msg {i}",
                                    "kind": "steer", "avoidable": avoidable, "root": "r" if avoidable else "none",
                                    "missed_thought": "", "why": ""}) + "\n")

    def test_steers_are_the_avoidable_messages_per_day_over_a_week(self):
        h = 3600
        self.book([(h, True), (2 * h, True), (3 * h, True), (30 * h, True), (50 * h, False), (9 * 86400, True)])
        self.heavy("steers")
        r = self.row("steers")
        self.assertEqual(r["actual"], 0.57, "four avoidable in the week (the ninth day is out) over seven days")
        self.assertEqual(r["status"], "ok")
        self.book([(h * i, True) for i in range(1, 15)])
        self.heavy("steers")
        self.assertEqual((self.row("steers")["status"], self.row("steers")["actual"]), ("regressed", 14.0),
                         "fourteen in the one day the meter covers: a day is the shortest span, not seven of them")

    def test_steers_divide_by_the_days_the_meter_has_read_not_by_a_week(self):
        h = 3600
        self.book([(h, True), (2 * h, True), (30 * h, True), (47 * h, True), (40 * h, False)])  # the meter began two days ago
        self.heavy("steers")
        r = self.row("steers")
        self.assertEqual(r["actual"], 2.04, "four avoidable over the 47 hours its rows cover, not over seven days (0.57)")
        self.assertEqual(r["status"], "regressed")
        self.assertIn("2 days", r["note"])

    def test_steers_are_unmeasured_when_the_meters_record_has_stopped(self):
        h = 3600
        self.book([(30 * h, True), (50 * h, True), (4 * 86400, True)])  # the newest message is a day and a quarter old
        self.heavy("steers")
        r = self.row("steers")
        self.assertEqual((r["status"], r["actual"]), ("unmeasured", None), "a rate over a gap is no rate")
        self.assertIn("stopped", r["note"])

    def test_steers_leave_out_machine_text_and_say_nothing_of_the_words(self):
        with open(os.path.join(self.p.dir, "steers.jsonl"), "w") as f:
            for i, text in enumerate(("<task-notification>x</task-notification>", "fix the push, sk-secret-123")):
                f.write(json.dumps({"session": "s", "uuid": f"u{i}", "at": c.iso(self.now - 60), "text": text,
                                    "kind": "steer", "avoidable": True, "root": "r", "missed_thought": "", "why": ""}) + "\n")
        self.heavy("steers")
        r = self.row("steers")
        self.assertEqual(r["actual"], 1.0, "one message counts, the notification is the program's")
        self.assertNotIn("push", r["note"])
        self.assertNotIn("sk-secret", r["note"])

    def test_steers_with_nothing_read_is_unmeasured(self):
        self.heavy("steers")
        self.assertEqual(self.row("steers")["status"], "unmeasured")

    # ----- disk growth -----

    def footprint(self, nbytes, unsized=()):
        return {"total_bytes": nbytes, "unsized": list(unsized)}

    def test_disk_growth_is_the_change_in_sampos_footprint_per_day(self):
        from sampo import tidy
        day = 86400
        with unittest.mock.patch.object(tidy, "footprint", return_value=self.footprint(10_000_000_000)):
            self.heavy("disk_growth", now=self.now - 2 * day)
        r = self.row("disk_growth")
        self.assertEqual((r["status"], r["actual"]), ("unmeasured", None), "one sample is no growth")
        with unittest.mock.patch.object(tidy, "footprint", return_value=self.footprint(10_500_000_000)):
            self.heavy("disk_growth", now=self.now)
        r = self.row("disk_growth")
        self.assertEqual((r["status"], r["actual"]), ("regressed", 250.0), "500 MB in two days; the target is no growth")
        self.assertTrue(r["provisional"])
        with unittest.mock.patch.object(tidy, "footprint", return_value=self.footprint(9_000_000_000)):
            self.heavy("disk_growth", now=self.now + day)
        self.assertEqual(self.row("disk_growth", now=self.now + day)["status"], "ok", "a shrinking footprint is ok")

    def test_disk_growth_does_not_sample_a_footprint_the_clock_cut_short(self):
        from sampo import tidy
        day = 86400
        with unittest.mock.patch.object(tidy, "footprint", return_value=self.footprint(1_000_000_000)):
            self.heavy("disk_growth", now=self.now - day)
        with unittest.mock.patch.object(tidy, "footprint", return_value=self.footprint(5_000, ["cargo_lanes: not looked at"])):
            self.heavy("disk_growth", now=self.now)  # a lower bound: not a sample
        r = self.row("disk_growth")
        self.assertEqual(r["status"], "unmeasured", r)
        self.assertIn("not fully sized", r["note"])

    # ----- tokens per change -----

    def lanes(self, per_lane_tokens):
        return {"days": 14, "landed": len(per_lane_tokens) + 1, "git": {"source": "git"},
                "lanes": {f"T-{i}": {"builder": {"input": t, "cache_read": 0, "cache_write": 0, "output": 0},
                                     "lead": {"input": t, "cache_read": 0, "cache_write": 0, "output": 0}}
                          for i, t in enumerate(per_lane_tokens, 1)}, "median": {}, "unattributed": {}}

    def test_tokens_per_change_is_measured_from_the_per_lane_reader(self):
        with unittest.mock.patch.object(cost, "per_lane", return_value=self.lanes([1_000_000, 2_000_000])) as pl:
            self.heavy("tokens_per_change")
        self.assertEqual(pl.call_args[0][1], 14)
        r = self.row("tokens_per_change")
        self.assertEqual((r["status"], r["actual"]), ("ok", 3_000_000.0), "each lane's lead and builder, input-equivalent, averaged")
        self.assertIn("1 of 3", r["note"], "a landed change with no transcript tokens is left out, and said")
        with unittest.mock.patch.object(cost, "per_lane", return_value=self.lanes([5_000_000, 6_000_000])):
            self.heavy("tokens_per_change")
        self.assertEqual(self.row("tokens_per_change")["status"], "regressed")

    def test_tokens_per_change_beats_the_no_cost_cache_note(self):
        tid = "T-9001"
        c.log_event(self.p, "lane_landed", task=tid, data={"tasks": [tid]})
        self.assertIn("no cost cache", targets.refresh(self.p, self.now, force=True)["values"]["notes"]["tokens_per_change"])
        with unittest.mock.patch.object(cost, "per_lane", return_value=self.lanes([1_000_000])):
            self.heavy("tokens_per_change")
        r = self.row("tokens_per_change")
        self.assertEqual(r["actual"], 2_000_000.0)
        self.assertNotIn("no cost cache", r["note"] or "")

    def test_tokens_per_change_with_no_landing_keeps_the_cache_reading(self):
        with unittest.mock.patch.object(cost, "per_lane", return_value={"days": 14, "landed": 0, "lanes": {}}):
            self.heavy("tokens_per_change")
        self.assertEqual(self.row("tokens_per_change")["status"], "unmeasured")

    # ----- the machinery -----

    def test_each_heavy_reader_has_its_own_pace_and_a_failing_one_costs_the_rest_nothing(self):
        calls = []

        def boom(p, now, state, run):
            raise RuntimeError("the transcripts folder is gone: /home/someone/secret")

        def fine(p, now, state, run):
            calls.append("fine")
            return 7.0, None
        table = {"lead_context": (boom, 3600), "steers": (fine, 86400)}
        with unittest.mock.patch.object(targets, "HEAVY", table):
            self.assertEqual(sorted(targets.heavy_due(self.p, self.now)), ["lead_context", "steers"])
            targets.refresh_heavy(self.p, self.now)
            self.assertEqual(calls, ["fine"], "one reader raising does not stop the next")
            self.assertEqual(targets.heavy_due(self.p, self.now + 100), [], "just read: neither is due")
            self.assertEqual(targets.heavy_due(self.p, self.now + 4000), ["lead_context"], "the hourly one, not the daily")
        r = self.row("lead_context")
        self.assertEqual(r["status"], "unmeasured")
        self.assertIn("RuntimeError", r["note"])
        self.assertNotIn("secret", r["note"], "the exception's words stay out of a note: it can hold a private path")
        self.assertNotIn("/home", r["note"])

    def test_a_heavy_value_that_stops_being_refreshed_goes_unmeasured_not_stale_ok(self):
        self.book([(60, True)])
        self.heavy("steers")
        self.assertEqual(self.row("steers")["status"], "ok")
        later = self.now + 5 * 86400  # the 6-hour reader, not refreshed for days
        self.assertEqual(self.row("steers", now=later)["status"], "unmeasured")
        self.assertIn("not refreshed", self.row("steers", now=later)["note"])

    def test_heavy_refresh_is_single_flight(self):
        held = open(os.path.join(self.p.dir, targets.HEAVY_LOCK), "a")
        fcntl.flock(held, fcntl.LOCK_EX)
        try:
            with unittest.mock.patch.object(targets, "HEAVY", {"steers": (lambda *a: self.fail("ran under the lock"), 60)}):
                targets.refresh_heavy(self.p, self.now, force=True)
        finally:
            held.close()

    def test_refresh_spawns_the_heavy_scan_detached_at_most_once_in_a_while(self):
        started = []
        with unittest.mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FOREMAN_NO_BACKGROUND")
            fake = unittest.mock.Mock(side_effect=lambda argv, **kw: started.append((argv, kw)) or types.SimpleNamespace(pid=0))
            with unittest.mock.patch.object(subprocess, "Popen", fake):
                targets.refresh(self.p, self.now, force=True)
                targets.refresh(self.p, self.now + 60, force=True)
        self.assertEqual(len(started), 1, "the second refresh finds the spawn stamp")
        argv, kw = started[0]
        self.assertEqual(argv[-2:], ["targets", "--refresh-heavy"])
        if shutil.which("nice"):
            self.assertEqual(argv[:3], [shutil.which("nice"), "-n", "10"], "the background read yields the CPU")
        self.assertTrue(kw["start_new_session"])
        self.assertIs(kw["stdin"], subprocess.DEVNULL)
        self.assertIs(kw["stdout"], subprocess.DEVNULL)
        self.assertTrue(kw["stderr"].closed, "its exit line goes to a log file, which the parent let go of")
        log = os.path.join(c.state_dir(), "logs", f"targets-heavy-{os.path.basename(self.p.dir)}.log")
        self.assertEqual(kw["stderr"].name, log)
        self.assertTrue(os.path.exists(log))

    def test_the_spawn_is_a_no_child_when_the_log_cannot_be_opened(self):
        started = []
        os.makedirs(os.path.join(c.state_dir(), "logs", f"targets-heavy-{os.path.basename(self.p.dir)}.log"))  # a dir: no open
        with unittest.mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FOREMAN_NO_BACKGROUND")
            fake = unittest.mock.Mock(side_effect=lambda argv, **kw: started.append(kw) or types.SimpleNamespace(pid=0))
            with unittest.mock.patch.object(subprocess, "Popen", fake):
                targets._spawn_heavy(self.p, self.now)
        self.assertEqual(len(started), 1, "a log that can't be opened costs the child its log, not its start")
        self.assertIs(started[0]["stderr"], subprocess.DEVNULL)

    def test_refresh_spawns_nothing_when_every_heavy_reader_is_fresh(self):
        with unittest.mock.patch.object(targets, "HEAVY", {"steers": (lambda p, now, state, run: (1.0, None), 3600)}):
            targets.refresh_heavy(self.p, self.now, force=True)
            os.environ.pop("FOREMAN_NO_BACKGROUND")
            with unittest.mock.patch.object(subprocess, "Popen", side_effect=AssertionError("spawned with nothing due")):
                targets.refresh(self.p, self.now + 60, force=True)

    def test_sampo_targets_by_hand_prints_the_last_values_and_starts_the_heavy_scan_detached(self):
        spawned, inline = [], []
        with unittest.mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FOREMAN_NO_BACKGROUND")
            with unittest.mock.patch.object(cli, "resolve", return_value=self.p), \
                    unittest.mock.patch.object(targets, "refresh_heavy", side_effect=lambda *a, **k: inline.append(1)), \
                    unittest.mock.patch.object(targets, "_spawn_heavy", side_effect=lambda *a: spawned.append(1)), \
                    unittest.mock.patch.object(subprocess, "Popen", return_value=types.SimpleNamespace(pid=0)), \
                    unittest.mock.patch.object(cli, "out") as out:
                targets.cmd_targets(types.SimpleNamespace(json=True, refresh_heavy=False))
        self.assertEqual(inline, [], "a person waiting at the verb never waits for the transcripts to be read")
        self.assertTrue(spawned, "the scan is the detached child's (the stamp keeps a second ask from a second child)")
        self.assertEqual([r["name"] for r in out.call_args[0][1]], KEYS)

    def test_sampo_targets_runs_the_due_heavy_readers_inline_only_where_no_child_may_start(self):
        inline = []
        with unittest.mock.patch.object(cli, "resolve", return_value=self.p), \
                unittest.mock.patch.object(targets, "refresh_heavy", side_effect=lambda *a, **k: inline.append(1)), \
                unittest.mock.patch.object(targets, "_spawn_heavy", side_effect=AssertionError("spawned under NO_BACKGROUND")), \
                unittest.mock.patch.object(cli, "out"):
            self.assertTrue(os.environ.get("FOREMAN_NO_BACKGROUND"))
            targets.cmd_targets(types.SimpleNamespace(json=True, refresh_heavy=False))
        self.assertEqual(inline, [1])
        out = self.fm("targets", "--json").stdout
        rows = {r["name"]: r for r in json.loads(out)}
        self.assertEqual(list(rows), KEYS)
        for name in ("push_lag", "lead_context", "compactions", "disk_growth", "steers"):
            self.assertEqual(rows[name]["status"], "unmeasured", name + ": a source missing is unmeasured, never ok")

    def test_refresh_heavy_flag_runs_the_scan_and_prints_nothing_to_stdout_but_one_exit_line_to_stderr(self):
        called = []
        err = io.StringIO()
        with unittest.mock.patch.object(cli, "resolve", return_value=self.p), \
                unittest.mock.patch.object(targets, "refresh_heavy", side_effect=lambda *a, **k: called.append(k)), \
                unittest.mock.patch.object(cli, "out") as out, contextlib.redirect_stderr(err):
            self.assertEqual(targets.cmd_targets(types.SimpleNamespace(json=False, refresh_heavy=True)), 0)
        self.assertEqual(len(called), 1)
        out.assert_not_called()
        self.assertIn("another run holds the lock", err.getvalue(), "refresh_heavy gave None: the lock was held")

    def test_the_detached_childs_exit_line_says_what_it_read_and_what_it_could_not(self):
        err = io.StringIO()
        table = {"steers": (lambda p, now, state, run: (1.0, None), 60), "lead_context": (lambda p, now, state, run: (None, "x"), 60)}
        with unittest.mock.patch.object(cli, "resolve", return_value=self.p), unittest.mock.patch.object(targets, "HEAVY", table), \
                contextlib.redirect_stderr(err):
            self.assertEqual(targets.cmd_targets(types.SimpleNamespace(json=False, refresh_heavy=True)), 0)
        line = err.getvalue().strip()
        self.assertRegex(line, r"1 of 2 read in [0-9.]+ s; unmeasured: lead_context$")
        err = io.StringIO()
        with unittest.mock.patch.object(cli, "resolve", return_value=self.p), \
                unittest.mock.patch.object(targets, "refresh_heavy", side_effect=RuntimeError("/home/someone/secret")), \
                contextlib.redirect_stderr(err):
            self.assertEqual(targets.cmd_targets(types.SimpleNamespace(json=False, refresh_heavy=True)), 1)
        self.assertIn("RuntimeError", err.getvalue())
        self.assertNotIn("secret", err.getvalue(), "the exit line names the type, never the words: they can hold a path")

    def test_the_readers_of_the_heavy_values_stay_one_small_read(self):
        h = {"values": {k: 1.0 for k in targets.HEAVY}, "at": {k: self.now for k in targets.HEAVY},
             "notes": {k: "x" * 50 for k in targets.HEAVY}, "disk": [[self.now - i, i] for i in range(60)]}
        with open(os.path.join(self.p.dir, targets.HEAVY_FILE), "w") as f:
            json.dump(h, f)
        with unittest.mock.patch.object(c, "ledger_events", side_effect=AssertionError("a reader read the ledger")), \
                unittest.mock.patch.object(subprocess, "run", side_effect=AssertionError("a reader ran a process")):
            for name in targets.HEAVY:
                t0 = time.perf_counter()
                targets.READERS[name](self.p, self.now)
                self.assertLess(time.perf_counter() - t0, 0.05, name)

    def test_a_torn_or_foreign_heavy_file_is_no_reading(self):
        for body in ("{", "[1, 2]", '{"values": {"steers": NaN}, "at": {"steers": %f}}' % self.now, '{"values": 3, "at": "x"}'):
            with open(os.path.join(self.p.dir, targets.HEAVY_FILE), "w") as f:
                f.write(body)
            self.assertEqual(self.row("steers")["status"], "unmeasured", body)

    # ----- a reader's own deadline -----

    def slow(self, p, now, state, run):
        while True:  # a reader that never ends, and swallows what it can: only a BaseException gets through
            try:
                time.sleep(0.01)
                run.check()
            except Exception:  # noqa: BLE001
                pass

    def test_a_heavy_reader_past_its_own_deadline_is_cut_and_the_next_still_runs(self):
        table = {"lead_context": (self.slow, 3600), "steers": (lambda p, now, state, run: (7.0, None), 3600)}
        with unittest.mock.patch.object(targets, "HEAVY", table), unittest.mock.patch.object(targets, "READER_BUDGET_S", 0.3):
            t0 = time.monotonic()
            targets.refresh_heavy(self.p, self.now)
            self.assertLess(time.monotonic() - t0, 10)
        r = self.row("lead_context")
        self.assertEqual((r["status"], r["actual"]), ("unmeasured", None))
        self.assertIn("did not finish", r["note"])
        self.assertEqual(self.row("steers")["actual"], 7.0, "one reader's overrun costs the others nothing")

    def test_a_reader_blocked_in_a_call_is_cut_by_the_alarm_and_the_alarm_leaves_no_trace(self):
        if threading.current_thread() is not threading.main_thread():
            self.skipTest("SIGALRM is the main thread's")
        table = {"lead_context": (lambda p, now, state, run: time.sleep(60), 3600),
                 "steers": (lambda p, now, state, run: (7.0, None), 3600)}
        with unittest.mock.patch.object(targets, "HEAVY", table), unittest.mock.patch.object(targets, "READER_BUDGET_S", 0.3):
            t0 = time.monotonic()
            targets.refresh_heavy(self.p, self.now)
            self.assertLess(time.monotonic() - t0, 10, "a sleep of a minute was cut at the deadline")
        self.assertEqual(signal.getsignal(signal.SIGALRM), signal.SIG_DFL)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL)[0], 0)
        self.assertEqual(self.row("steers")["actual"], 7.0)
        self.assertIn("did not finish", self.row("lead_context")["note"])

    def test_the_deadline_leaves_another_alarm_alone(self):
        if threading.current_thread() is not threading.main_thread():
            self.skipTest("SIGALRM is the main thread's")
        theirs = lambda *a: None  # noqa: E731
        old = signal.signal(signal.SIGALRM, theirs)
        self.addCleanup(signal.signal, signal.SIGALRM, old)
        table = {"lead_context": (self.slow, 3600)}
        with unittest.mock.patch.object(targets, "HEAVY", table), unittest.mock.patch.object(targets, "READER_BUDGET_S", 0.3):
            targets.refresh_heavy(self.p, self.now)  # no alarm of ours: run.check() alone cuts it
        self.assertIs(signal.getsignal(signal.SIGALRM), theirs, "someone else's handler is not replaced")
        self.assertIn("did not finish", self.row("lead_context")["note"])

    # ----- one pass over the transcripts -----

    def test_lead_context_compactions_and_tokens_share_one_walk_of_the_transcripts(self):
        self.lead_turns([180_000, 200_000, 160_000, 220_000, 190_000])
        self.write_lines(os.path.join(self.folder, "s1.jsonl"), [self.chatter(500), self.summary(100)])
        with unittest.mock.patch.object(targets, "_walk", wraps=targets._walk) as walk, \
                unittest.mock.patch.object(cost, "per_lane", return_value=self.lanes([1_000_000])) as pl:
            self.heavy("lead_context", "compactions", "tokens_per_change")
        self.assertEqual(walk.call_count, 1, "three readers, one pass")
        self.assertEqual(len(pl.call_args.kwargs["lead_replies"]), 5, "per_lane is handed the pass's lead replies")
        self.assertEqual(self.row("lead_context")["actual"], 190_000.0)
        self.assertEqual(self.row("compactions")["actual"], 0.5, "one summary over two session-days (a lead's file and s1)")

    def test_a_failed_walk_fails_each_transcript_reader_without_walking_again(self):
        with unittest.mock.patch.object(targets, "_walk", side_effect=OSError("/home/someone/secret")) as walk:
            self.heavy("lead_context", "compactions", "tokens_per_change")
        self.assertEqual(walk.call_count, 1)
        for key in ("lead_context", "compactions"):
            r = self.row(key)
            self.assertEqual(r["status"], "unmeasured", key)
            self.assertIn("OSError", r["note"])
            self.assertNotIn("secret", r["note"])

    def test_a_compaction_summary_in_two_transcripts_counts_once(self):
        self.write_lines(os.path.join(self.folder, "s1.jsonl"), [self.chatter(500), self.summary(100)])
        self.write_lines(os.path.join(self.folder, "s1-resumed.jsonl"), [self.chatter(300), self.summary(100)])  # the same uuid
        self.heavy("compactions")
        self.assertEqual(self.row("compactions")["actual"], 0.5, "one summary over two session files' days")

    def test_a_transcript_that_cannot_be_opened_is_skipped_not_the_reading(self):
        self.write_lines(os.path.join(self.folder, "s1.jsonl"), [self.chatter(500), self.summary(100)])
        bad = os.path.join(self.folder, "locked.jsonl")
        self.write_lines(bad, [self.chatter(200, "s9")])
        os.chmod(bad, 0)
        self.addCleanup(os.chmod, bad, 0o600)
        if os.access(bad, os.R_OK):
            self.skipTest("this user can read a mode-0 file")
        self.heavy("compactions", "lead_context")
        self.assertEqual(self.row("compactions")["actual"], 1.0, "the readable session's summary, the locked one skipped")

    # ----- push lag: the trunk's time, bounded -----

    def test_push_lag_counts_a_lane_from_the_merge_that_landed_it_not_its_own_old_commit(self):
        self.origin()
        self.git("checkout", "-q", "-b", "lane")
        self.commit("lane.txt", 40)  # the builder's commit is a day and a half old
        self.git("checkout", "-q", "main")
        self.git("merge", "-q", "--no-ff", "--no-edit", "lane", when=self.now - 1.5 * 3600)  # but it landed an hour and a half ago
        self.heavy("push_lag")
        r = self.row("push_lag")
        self.assertAlmostEqual(r["actual"], 1.5, delta=0.1, msg="the trunk lacks the landing from 1.5 h ago, not the 40 h commit")
        self.assertEqual(r["status"], "ok", "under the target, not a breach")

    def test_push_lag_reads_the_oldest_commits_when_the_trunk_is_longer_than_the_cap(self):
        self.origin()
        for name, hours in (("a.txt", 50), ("b.txt", 40), ("c.txt", 30), ("d.txt", 3)):
            self.commit(name, hours)
        with unittest.mock.patch.object(targets, "PUSH_MAX", 2):
            self.heavy("push_lag")
        r = self.row("push_lag")
        self.assertAlmostEqual(r["actual"], 50.0, delta=0.1, msg="the oldest lacking commit is in the oldest window")
        self.assertIn("2+ commits", r["note"])

    def test_push_lag_past_the_cap_with_nothing_lacking_in_the_window_is_unmeasured_not_zero(self):
        self.origin()
        self.commit("a.txt", 60)
        self.commit("b.txt", 59)
        a, b = self.git("rev-parse", "HEAD~1").strip(), self.git("rev-parse", "HEAD").strip()
        self.git("push", "-q", "origin", "main")
        self.git("reset", "-q", "--hard", "HEAD~2")
        self.git("cherry-pick", a, b)  # new hashes, the same patches: a rebase
        self.commit("c.txt", 3)  # the one thing origin lacks, beyond the window
        with unittest.mock.patch.object(targets, "PUSH_MAX", 2):
            self.heavy("push_lag")
        r = self.row("push_lag")
        self.assertEqual((r["status"], r["actual"]), ("unmeasured", None), "never an ok it could not see")
        self.assertIn("more than 2 commits", r["note"])

    # ----- a derived value is only as fresh as targets.json -----

    def light(self, at, **vals):
        with open(os.path.join(self.p.dir, "targets.json"), "w") as f:
            json.dump({"ts": at, "values": vals}, f)

    def test_a_derived_value_from_an_old_targets_json_is_unmeasured_and_names_its_source(self):
        vals = {"accept_to_merge": 90.0, "rework_rounds": 3.0, "tokens_per_change": 9_000_000, "sampo_vs_bare": 0.9}
        self.light(self.now - 60, **vals)
        for key, want in (("accept_to_merge", 90.0), ("rework_rounds", 3.0), ("tokens_per_change", 9_000_000.0),
                          ("sampo_vs_bare", 0.9)):
            self.assertEqual(self.row(key)["actual"], want, key + ": a recent file is read")
        self.light(self.now - 7 * 3600, **vals)
        for key, source in (("accept_to_merge", "ledger scan"), ("rework_rounds", "ledger scan"),
                            ("tokens_per_change", "cost cache"), ("sampo_vs_bare", "bench results")):
            r = self.row(key)
            self.assertEqual((r["status"], r["actual"]), ("unmeasured", None), key + ": a value from a stalled scan is no reading")
            self.assertIn(source, r["note"], key)
            self.assertIn("7 h", r["note"], key)

    def test_a_targets_json_with_no_time_or_one_ahead_of_the_clock_is_not_trusted(self):
        with open(os.path.join(self.p.dir, "targets.json"), "w") as f:
            json.dump({"values": {"rework_rounds": 3.0}}, f)
        r = self.row("rework_rounds")
        self.assertEqual(r["status"], "unmeasured")
        self.assertIn("no time", r["note"])
        self.light(self.now + 3600, rework_rounds=3.0)
        r = self.row("rework_rounds")
        self.assertEqual(r["status"], "unmeasured")
        self.assertIn("ahead of the clock", r["note"])

    def test_the_public_names_the_targets_use_are_what_cost_and_steermeter_keep(self):
        from sampo import steermeter
        self.assertIs(cost._lead_replies, cost.lead_replies)
        self.assertIs(cost._mtime, cost.mtime)
        self.assertIs(cost._equiv, cost.equiv)
        self.assertIs(cost._BLOB, cost.BLOB)
        self.assertIs(steermeter._read_rows, steermeter.read_rows)
