"""fm budget (T-0227): every child run and subagent records what it cost, and caps refuse what would overspend —
bounded use, not avoidance."""
import json
import os
import time

from helpers import ForemanTestCase, read_text

from sampo import budget as bud
from sampo import core as c

STUB = r'''#!/usr/bin/env python3
import json, os, sys
sys.stdin.read()
print(json.dumps({"type": "result", "is_error": False, "total_cost_usd": 0.07,
                  "result": "## Examples\n- GIVEN a WHEN b THEN c\n## Ambiguities\n- none"}))
'''


class Budget(ForemanTestCase):
    def setUp(self):
        super().setUp()
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir)
        with open(os.path.join(bindir, "claude"), "w") as f:
            f.write(STUB)
        os.chmod(os.path.join(bindir, "claude"), 0o755)
        self.env = {"PATH": bindir + os.pathsep + os.environ["PATH"]}
        self.fm("init")

    def spend(self):
        path = os.path.join(c.state_dir(), "spend.jsonl")
        return [json.loads(l) for l in read_text(path).splitlines()] if os.path.exists(path) else []

    def test_a_child_run_records_its_cost(self):
        tid = json.loads(self.fm("task", "new", "Export CSV", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true",
                                 "--step", "do", "--json").stdout)["id"]
        self.fm("oracle", tid, env=self.env)
        rec = self.spend()[-1]
        self.assertEqual((rec["feature"], rec["usd"], rec["runs"]), ("oracle", 0.07, 1))
        self.assertIn("oracle", self.fm("budget").stdout)
        self.assertIn("GIVEN a WHEN b THEN c", self.fm("task", "show", tid).stdout, "the JSON result's text is used")

    def test_caps_refuse_what_would_overspend(self):
        bud.set_caps(day=10, run=4)
        bud.record("bench", 9.5, runs=3)
        bud.RUN["usd"] = 0.0  # that spend was earlier commands' (T-2871: this process is a new command)
        bud.check("oracle", 0.3)  # fits
        with self.assertRaises(bud.BudgetError) as e:
            bud.check("bench", 1.0)
        self.assertIn("today", str(e.exception))
        with self.assertRaises(bud.BudgetError) as e:
            bud.check("evolve", 5.0)
        self.assertIn("per command", str(e.exception))

    def test_a_command_of_many_small_calls_stops_at_the_per_command_cap(self):
        # T-2871: moons grow's 30 calls each fit the cap alone; their total did not
        bud.set_caps(day=100, run=1.0)
        for _ in range(3):
            bud.check("moons-grow", 0.3)
            bud.record("moons-grow", 0.3)
        with self.assertRaises(bud.BudgetError) as e:
            bud.check("moons-grow", 0.3)  # 0.9 spent this command + 0.3 > 1.0
        self.assertIn("this command", str(e.exception))

    def test_estimates_come_from_recent_runs(self):
        for usd in (0.2, 0.4):
            bud.record("research", usd, runs=1)
        self.assertAlmostEqual(bud.estimate("research", 3, fallback=9), 0.9)
        self.assertEqual(bud.estimate("never-ran", 2, fallback=0.5), 1.0)

    def test_caps_halve_when_weekly_usage_is_high(self):
        bud.set_caps(day=10, run=8)
        folder = os.path.join(c.state_dir(), "sessions")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "s1.json"), "w") as f:
            json.dump({"rate_limits": {"seven_day": {"used_percentage": 85}}}, f)
        with self.assertRaises(bud.BudgetError) as e:
            bud.check("bench", 6.0)
        self.assertIn("usage", str(e.exception))

    def test_raising_a_cap_is_recorded_as_a_costly_decision(self):
        self.fm("budget", "set", "--day", "5")
        self.fm("budget", "set", "--day", "20", "--because", "night bench")
        decisions = read_text(os.path.join(c.find_project(self.repo).dir, "decisions.md"))
        self.assertIn("night bench", decisions)
        self.assertEqual(decisions.count("budget"), 1, "lowering needs no decision")
        self.assertNotEqual(self.fm("budget", "set", "--day", "30", check=False).returncode, 0, "raising says why")

    def test_bad_caps_never_switch_the_budget_off(self):
        # T-0227 review: --day nan made every comparison false; a corrupt file crashed every command
        for bad in ("nan", "-1", "inf"):
            with self.subTest(bad=bad):
                self.assertNotEqual(self.fm("budget", "set", "--day", bad, "--because", "x", check=False).returncode, 0)
        with open(os.path.join(c.state_dir(), "budget.json"), "w") as f:
            f.write('{"day": null, "run": [1]}')
        self.assertEqual(bud.caps(), bud.DEFAULTS)
        with open(os.path.join(c.state_dir(), "budget.json"), "w") as f:
            f.write("[1, 2]")
        self.assertEqual(bud.caps(), bud.DEFAULTS)
        self.assertIn("today", self.fm("budget").stdout)

    def test_subagents_are_counted_and_capped(self):
        transcript = os.path.join(self.tmp, "agent.jsonl")
        with open(transcript, "w") as f:
            for i in range(2):
                f.write(json.dumps({"type": "assistant", "timestamp": c.now(), "message": {
                    "id": f"m{i}", "usage": {"input_tokens": 1000, "output_tokens": 100}}}) + "\n")
        self.hook("SubagentStop", {"agent_id": "a1", "agent_type": "sampo:fm-recon",
                                   "agent_transcript_path": transcript})
        rec = self.spend()[-1]
        self.assertEqual((rec["feature"], rec["tokens"]), ("subagent:sampo:fm-recon", 3000))
        self.assertIn("3,000", self.fm("budget").stdout)  # counted and shown, never a cap (T-0320)
        agent = {"tool_name": "Agent", "tool_input": {"subagent_type": "sampo:fm-recon", "prompt": "x"}}
        self.assertEqual(self.hook("PreToolUse", agent).returncode, 0)
        self.usage(seven_day=(50, 6 * 86400))  # half the week used with a seventh of it gone: under 75% (T-0364)
        self.assertEqual(self.hook("PreToolUse", agent).returncode, 0)
        self.usage(seven_day=(78, 6 * 86400))
        out = self.hook("PreToolUse", agent)
        self.assertEqual(out.returncode, 2)
        self.assertIn("weekly usage 78%", out.stderr)

    def test_fm_drive_pace_off_drops_no_work_for_usage_pace(self):
        from sampo import budget as bud  # T-2327 (user steer 2026-10-31, 3): never skip work for budget or pace
        self.usage(five_hour=(95, 3600))
        p = c.find_project(self.repo)
        self.assertIn("5-hour usage 95%", bud.degrade(p) or "")
        self.fm("drive", "on", "--pace", "off")
        self.assertIsNone(bud.degrade(c.find_project(self.repo)))
        self.fm("drive", "on", "--pace", "on")
        self.assertIn("5-hour usage 95%", bud.degrade(c.find_project(self.repo)) or "")

    def test_a_project_can_turn_the_weekly_lean_off(self):
        # T-3049: "remove the weekly usage 80% lean on this project specifically" - the 5-hour window keeps its rule
        p = c.find_project(self.repo)
        self.usage(seven_day=(85, 2 * 86400))
        self.assertIn("weekly usage 85%", bud.usage_high(p) or "")
        self.assertIn("weekly usage 85%", bud.subagent_pause(p) or "")
        self.fm("drive", "on", "--pace", "off")
        self.assertIsNone(bud.usage_high(p))
        self.assertIsNone(bud.subagent_pause(p))
        self.assertIsNone(bud.effective(p)[1], "no halved caps")
        self.assertIs(json.loads(self.fm("ui", "--json").stdout)["weekly_lean"], False, "the pane reads it")
        agent = {"tool_name": "Agent", "tool_input": {"subagent_type": "sampo:fm-recon", "prompt": "x"}}
        self.assertEqual(self.hook("PreToolUse", agent).returncode, 0, "the hook lets subagents run past 80% weekly")
        self.assertNotIn("halved", self.fm("budget").stdout)
        old = os.getcwd()
        os.chdir(self.repo)  # no project given: the one holding the cwd
        try:
            self.assertIsNone(bud.usage_high())
        finally:
            os.chdir(old)
        self.usage(seven_day=(85, 2 * 86400), five_hour=(95, 3600))
        self.assertIn("5-hour usage 95%", bud.usage_high(p) or "")
        self.assertIn("5-hour usage 95%", bud.subagent_pause(p) or "")
        self.fm("drive", "on", "--pace", "on")
        self.usage(seven_day=(85, 2 * 86400))
        self.assertIn("weekly usage 85%", bud.usage_high(p) or "")

    def usage(self, **windows):
        """A statusline snapshot: window=(used %, seconds until it resets)."""
        folder = os.path.join(c.state_dir(), "sessions")
        os.makedirs(folder, exist_ok=True)
        rl = {k: {"used_percentage": u, "resets_at": int(time.time() + s)} for k, (u, s) in windows.items()}
        with open(os.path.join(folder, "s.json"), "w") as f:
            json.dump({"rate_limits": rl}, f)

    def test_another_sessions_stale_reading_never_counts(self):  # T-2752: a previous subscription's 96% held subagents
        folder = os.path.join(c.state_dir(), "sessions")
        os.makedirs(folder, exist_ok=True)
        old, cur = os.path.join(folder, "old.json"), os.path.join(folder, "cur.json")
        week = {"used_percentage": 96, "resets_at": int(time.time() + 86400)}
        with open(old, "w") as f:
            json.dump({"rate_limits": {"seven_day": week}}, f)
        os.utime(old, (time.time() - 86400, time.time() - 86400))  # a day older than this session's
        with open(cur, "w") as f:
            json.dump({"model": "x"}, f)  # this session's newest snapshot carries no rate_limits yet
        self.assertNotIn("seven_day", bud.rate_limits())
        with open(cur, "w") as f:
            json.dump({"rate_limits": {"seven_day": dict(week, used_percentage=11)}}, f)
        self.assertEqual(bud.rate_limits()["seven_day"], 11)

    def test_subagents_follow_the_weeks_pace(self):
        self.assertIsNone(bud.subagent_pause(), "unknown usage never pauses")
        day = 86400
        for windows, paused in [({"seven_day": (63, 30 * 3600)}, None),           # 82% of the week gone: on pace
                                ({"seven_day": (50, 6 * day)}, None),  # T-0364: ahead of pace, but under 75% never waits
                                ({"seven_day": (78, 6 * day)}, "weekly usage 78%"),  # 14% gone: ahead of pace
                                ({"seven_day": (91, 3600)}, "weekly usage 91%"),     # never past 90%
                                ({"five_hour": (95, 3600)}, "5-hour usage 95%"),
                                ({"five_hour": (95, -60)}, None),                    # that window already reset
                                ({"seven_day": (15, 30 * day)}, None),  # a reset past a week out: pace unknown, 80% rule
                                ({"five_hour": (40, 3600), "seven_day": (20, 6 * day)}, None)]:
            self.usage(**windows)
            got = bud.subagent_pause()
            if paused is None:
                self.assertIsNone(got, windows)
            else:
                self.assertIn(paused, got or "", windows)
