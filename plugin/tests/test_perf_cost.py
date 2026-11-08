"""T-0676 (performance and cost): gate order by failure odds per second (T-0467), a hook latency SLO by revision
(T-0486), degrading optional work while usage runs ahead of pace (T-0449), the model behind each finished task (T-0487)
and compacting at an M/L step boundary (T-0448)."""
import json
import os
import shlex
import time
import unittest

from helpers import read_text
from test_hooks import HookCase, parse
import test_ideas

from sampo import core as c
from sampo import doctor
from sampo import eco


def usage(**windows):
    """A statusline snapshot: window=(used %, seconds until it resets)."""
    folder = os.path.join(c.state_dir(), "sessions")
    os.makedirs(folder, exist_ok=True)
    rl = {k: {"used_percentage": u, "resets_at": int(time.time() + s)} for k, (u, s) in windows.items()}
    with open(os.path.join(folder, "s.json"), "w") as f:
        json.dump({"rate_limits": rl}, f)


AHEAD = {"seven_day": (78, 6 * 86400)}  # 78% used with a seventh of the week gone: ahead of pace, under 90%


class GateOrder(HookCase):
    def gate(self, name):
        return f"echo {name} >> {shlex.quote(self.log)}; test ! -e {shlex.quote(self.flag)}-{name}"

    def ran(self):
        out = read_text(self.log).split() if os.path.exists(self.log) else []
        if os.path.exists(self.log):
            os.remove(self.log)
        return out

    def test_gates_run_by_failure_odds_per_second_and_list_in_configured_order(self):
        self.log, self.flag = os.path.join(self.tmp, "order.log"), os.path.join(self.tmp, "fail")
        self.fm("init")
        gates = [self.gate(x) for x in "abc"]
        for g in gates:
            self.fm("check", "add", g)
        res = self.fm_json("check", "--fresh")
        self.assertEqual(self.ran(), ["a", "b", "c"], "no history: the configured order")
        p = self.project()
        for i in range(5):  # a: slow, never fails · b: fast, never fails · c: fails often, 2 s
            c.log_event(p, "check_run", data={"results": [{"cmd": gates[0], "exit": 0, "s": 9.0},
                                                          {"cmd": gates[1], "exit": 0, "s": 1.0},
                                                          {"cmd": gates[2], "exit": int(i < 2), "s": 2.0}]})
        res = self.fm_json("check", "--fresh")
        self.assertEqual(self.ran(), ["c", "b", "a"], "most failure odds per second of runtime first")
        self.assertEqual([r["cmd"] for r in res["results"]], gates, "results still listed in configured order")
        run = [e for e in c.ledger_tail(p, 50) if e.get("event") == "check_run"][-1]
        self.assertEqual([r["cmd"] for r in run["data"]["results"]], gates, "the ledger too (fm check's cache reads it)")
        open(self.flag + "-c", "w").close()
        r = self.fm("check", "--fresh", "--fail-fast", check=False)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(set(self.ran()), {"c"}, "fail-fast: the likeliest failure ran first and ended the run")
        self.assertIn("2 later gate(s) not run", r.stdout)


class HookSlo(HookCase):
    def write_events(self, rows):
        path = os.path.join(c.state_dir(), "events.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            for rev, ms in rows:
                f.write(json.dumps({"ts": c.now(), "kind": "hook_ms", "event": "PreToolUse", "ms": ms, "rev": rev, "load": 0.2}) + "\n")

    def test_each_hook_ms_event_carries_the_revision(self):
        self.fm("init")
        self.hook("PreToolUse", {"tool_name": "Read", "tool_input": {"file_path": os.path.join(self.repo, "README.md")}})
        ms = [e for e in self.events() if e.get("kind") == "hook_ms"]
        self.assertTrue(ms)
        self.assertEqual(ms[-1].get("rev"), eco.version())

    def test_doctor_warns_past_the_slo_and_names_the_revision_where_it_rose(self):
        self.assertEqual(doctor.check_hook_slo().status, "PASS", "no runs recorded: nothing to judge")
        self.write_events([("1.2.15", 40)] * 150 + [("1.2.16", 60)] * 100)
        r = doctor.check_hook_slo()
        self.assertEqual(r.status, "PASS", r.detail)
        self.write_events([("1.2.15", 40)] * 150 + [("1.2.16", 60)] * 100 + [("1.2.17", 400)] * 100)
        r = doctor.check_hook_slo()
        self.assertEqual(r.status, "WARN", r.detail)
        self.assertIn("1.2.17", r.detail)
        self.assertIn("1.2.16", r.detail, "the revision before it, for the range")
        self.assertIn("150", r.detail, "bench_hooks' budget is the SLO")
        self.assertNotIn("1.2.15", r.detail, "only the last 200 runs count")


    def test_runs_taken_at_high_load_are_left_out_and_counted(self):  # T-2798
        path = os.path.join(c.state_dir(), "events.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)

        def put(rows, ts=None):
            with open(path, "w") as f:
                for ms, load in rows:
                    f.write(json.dumps({"ts": ts or c.now(), "kind": "hook_ms", "event": "PreToolUse", "ms": ms, "rev": "1",
                                        **({} if load is None else {"load": load})}) + "\n")
        put([(40, 0.3)] * 30 + [(900, 3.0)] * 40)
        r = doctor.check_hook_slo()
        self.assertEqual(r.status, "PASS", r.detail)
        self.assertIn("40 left out", r.detail)
        put([(40, 0.3)] * 250 + [(900, 3.0)] * 10)  # more calm runs than the window: only the loaded ones are counted
        self.assertIn("; 10 left out", doctor.check_hook_slo().detail)
        put([(400, 0.3)] * 30 + [(40, 3.0)] * 40)
        self.assertEqual(doctor.check_hook_slo().status, "WARN", "calm runs over the SLO still warn")
        put([(400, None)] * 30)
        self.assertEqual(doctor.check_hook_slo().status, "PASS", "events without a load are not known calm: not judged")
        put([(40, 0.3)] * 200 + [(400, 1.2)] * 200)  # T-2933: T-2798's bar (1.5) let these in; only under 0.5 counts
        r = doctor.check_hook_slo()
        self.assertEqual(r.status, "PASS", r.detail)
        self.assertIn("200 left out", r.detail)
        put([(400, 0.3)] * 200 + [(40, 1.2)] * 200)
        self.assertEqual(doctor.check_hook_slo().status, "WARN", "calm runs over the SLO warn")
        put([(400, 1.2)] * 200 + [(40, 0.3)] * 5)
        r = doctor.check_hook_slo()
        self.assertEqual(r.status, "PASS", r.detail)
        self.assertIn("not enough calm", r.detail)
        put([(900, 3.0)] * 500 + [(40, 0.3)] * 30)  # loaded runs before the oldest calm one aren't counted
        r = doctor.check_hook_slo()
        self.assertNotIn("left out", r.detail)
        self.assertIn("oldest calm sample 0.0 d old", r.detail)
        put([(400, 0.49)] * 30 + [(40, 0.5)] * 30)  # the bar: 0.49 kept, 0.5 left out
        r = doctor.check_hook_slo()
        self.assertEqual(r.status, "WARN", r.detail)
        self.assertIn("over the last 30 runs", r.detail)
        self.assertIn("30 left out", r.detail)
        put([(400, 0.3)] * 30, ts=c.iso(time.time() - 3 * 86400))  # calm runs days old say nothing about now
        r = doctor.check_hook_slo()
        self.assertEqual(r.status, "PASS", r.detail)
        self.assertIn("not enough recent calm samples", r.detail)
        self.assertIn("3.0 d old", r.detail)

    def test_each_hook_ms_event_carries_the_load_per_cpu(self):  # T-2798
        self.fm("init")
        self.hook("PreToolUse", {"tool_name": "Read", "tool_input": {"file_path": os.path.join(self.repo, "README.md")}},
                  env={"FOREMAN_HOST": "load=8,cpus=4"})
        ms = [e for e in self.events() if e.get("kind") == "hook_ms"]
        self.assertEqual(ms[-1].get("load"), 2.0)


class DegradeOnPace(HookCase):
    def test_fm_next_drops_the_friction_pass_while_ahead_of_pace(self):
        self.fm("init")
        self.fm("friction", "--every", "1")
        p = self.project()
        c.log_event(p, "task_done", task="T-0099")
        self.assertIn("self-improvement pass due", self.fm("next").stdout)
        usage(**AHEAD)
        out = self.fm("next").stdout
        self.assertNotIn("self-improvement pass due", out)
        self.assertIn("optional work is dropped (weekly usage 78%", out)
        self.assertIn("required gates still run", out)

    def test_the_drive_offers_no_side_work_while_ahead_of_pace(self):
        self.fm("init")
        queued = self.task("Second fix", focus=False)
        self.task("Current work")
        usage(**AHEAD)
        self.hook("SubagentStart", {"agent_id": "a1", "agent_type": "sampo:fm-reviewer"})
        p = self.hook("Stop", {"stop_hook_active": False, "last_assistant_message": "Waiting for the review.",
                               "session_id": "sess-1"})
        reason = (parse(p) or {}).get("reason", "")
        self.assertNotIn(f"lane brief {queued}", reason, "no side task while usage runs ahead of pace")
        self.assertNotIn(f"second plan {queued}", reason)
        self.assertIn("Optional work is dropped (weekly usage 78%", reason)
        self.assertIn(queued, reason, "the queue is the user's work: it's still named as what comes next")

    def test_ideas_deepen_runs_no_rounds_and_over_the_cap_still_blocks(self):
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir)
        with open(os.path.join(bindir, "claude"), "w") as f:
            f.write(test_ideas.STUB)
        os.chmod(os.path.join(bindir, "claude"), 0o755)
        log = os.path.join(self.tmp, "stub.log")
        env = {"PATH": bindir + os.pathsep + os.environ["PATH"], "STUB_LOG": log}
        pack = os.path.join(self.tmp, "pack.md")
        with open(pack, "w") as f:
            f.write("Project: a tiny calculator app.\n")
        self.fm("init")
        usage(**AHEAD)
        r = self.fm("ideas", "--pack", pack, "--lens", "user value", "--deepen", "2", env=env)
        calls = [json.loads(l) for l in read_text(log).splitlines()]
        self.assertEqual(len(calls), 1, "the lens ran; no deepen round")
        self.assertFalse(any("Lens: deepen:" in x["stdin"] for x in calls))
        self.assertIn("optional work is dropped (weekly usage 78%", r.stdout + r.stderr)
        self.fm("check", "add", "true")
        self.assertEqual(self.fm("check", "--fresh", check=False).returncode, 0, "required gates still run")
        self.fm("budget", "set", "--day", "0")
        r = self.fm("ideas", "--pack", pack, "--lens", "user value", env=env, check=False)
        self.assertNotEqual(r.returncode, 0, "over the cap still blocks")
        self.assertIn("budget:", r.stderr)


class ModelLog(HookCase):
    def test_finish_logs_the_model_and_cost_shows_passes_by_model(self):
        self.fm("init")
        sessions = os.path.join(c.state_dir(), "sessions")
        os.makedirs(sessions, exist_ok=True)
        with open(os.path.join(sessions, "sess-m.json"), "w") as f:
            json.dump({"model": "Opus 5.5"}, f)
        env = {"FOREMAN_SESSION_ID": "sess-m"}
        self.fm("task", "new", "Add a", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true", "--step", "a",
                "--focus", env=env)
        self.fm("task", "finish", "T-0001", "--run", "true", "--audit", "self check", env=env)
        done = [e for e in c.ledger_tail(self.project(), 200) if e.get("event") == "task_done"][-1]["data"]
        self.assertEqual({k: done.get(k) for k in ("model", "type", "tier")}, {"model": "Opus 5.5", "type": "FEATURE",
                                                                                "tier": "S"})
        self.assertIn(done.get("verified"), ("weak", "ok", "strong"))
        out = self.fm("cost", "--by-model").stdout
        self.assertIn("Opus 5.5", out)
        self.assertIn("FEATURE S", out)
        self.assertIn("1 passed", out)
        data = self.fm_json("cost", "--by-model")
        self.assertEqual(data["by_model"]["Opus 5.5"]["FEATURE S"]["passed"], 1)


class StepCompact(HookCase):
    def context(self, pct):
        sessions = os.path.join(c.state_dir(), "sessions")
        os.makedirs(sessions, exist_ok=True)
        with open(os.path.join(sessions, "sess-1.json"), "w") as f:
            json.dump({"context_pct": pct}, f)

    def stop(self):
        p = self.hook("Stop", {"stop_hook_active": False, "last_assistant_message": "Working on it.",
                               "session_id": "sess-1"})
        return (parse(p) or {}).get("reason", "")

    def test_a_full_context_at_an_ml_step_boundary_says_checkpoint_and_compact(self):
        self.fm("init")
        tid = self.task("Big change", type_="FEATURE", tier="M")
        self.context(72)
        self.assertNotIn("step boundary", self.stop(), "mid-step: no note")
        self.fm("task", "evidence", tid, "--step", "1", "pytest", "red as expected")
        reason = self.stop()
        self.assertIn("step boundary", reason)
        self.assertIn("sampo checkpoint", reason)
        self.assertIn("Resume here", reason)
        self.assertIn("/compact", reason)
        self.assertNotIn("step boundary", self.stop(), "said once per step boundary")

    def test_no_note_for_s_tasks_low_context_or_once_work_resumed(self):
        self.fm("init")
        small = self.task("Small fix")
        self.context(72)
        self.fm("task", "evidence", small, "--step", "1", "pytest", "red")
        self.assertNotIn("step boundary", self.stop(), "S: compaction overhead isn't worth it")
        tid = self.task("Big change", type_="FEATURE", tier="M")  # pauses the S task
        self.context(30)
        self.fm("task", "evidence", tid, "--step", "1", "pytest", "red")
        self.assertNotIn("step boundary", self.stop(), "context is fine")
        self.context(72)
        self.hook("PostToolUse", {"tool_name": "Edit", "tool_input": {"file_path": os.path.join(self.repo, "README.md")}})
        self.assertNotIn("step boundary", self.stop(), "the next step is under way: not a boundary")


class UnexplainedSpend(HookCase):
    """T-1079: spend from a session with no ledger events, or a turn 30+ minutes from any, is flagged."""

    def setup_spend(self, rows, events=(60, 2)):
        """rows: (session, minutes ago, output tokens); session 'known' has ledger events at `events` minutes ago."""
        import datetime
        import re
        self.fm("init")
        p = c.find_project(self.repo)
        stamp = lambda m: (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(os.path.join(p.dir, "ledger.jsonl"), "a") as f:
            for m in events:
                f.write(json.dumps({"ts": stamp(m), "session_id": "known", "task": "T-0001", "event": "note", "data": {}}) + "\n")
        cfg = os.path.join(self.tmp, "cfg")
        folder = os.path.join(cfg, "projects", re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(self.repo)))
        os.makedirs(folder)
        with open(os.path.join(folder, "t.jsonl"), "w") as f:
            for i, (sid, ago, out) in enumerate(rows):
                f.write(json.dumps({"type": "assistant", "timestamp": stamp(ago), "sessionId": sid, "message": {
                    "id": f"m{i}", "content": [], "usage": {"output_tokens": out}}}) + "\n")
        return {"CLAUDE_CONFIG_DIR": cfg}

    def test_a_session_with_no_ledger_events_is_flagged_in_cost_and_digest(self):
        env = self.setup_spend([("known", 59, 20000), ("known", 30, 20000), ("stray", 10, 40000), ("stray", 9, 40000)])
        d = json.loads(self.fm("cost", "--json", env=env).stdout)["unexplained"]
        self.assertEqual((d["sessions"], d["tokens"], round(d["share"])), (["stray"], 400000, 67))
        for cmd in ("cost", "digest"):
            self.assertIn("Unexplained spend: 67% (≈ 400k tokens) in 1 session(s) with no Sampo activity within 30 min; top: stray", self.fm(cmd, env=env).stdout)

    def test_a_turn_past_the_gap_from_its_sessions_events_counts(self):
        env = self.setup_spend([("known", 85, 10000), ("known", 45, 100000)], events=(90, 2))
        d = json.loads(self.fm("cost", "--json", env=env).stdout)["unexplained"]
        self.assertEqual((d["tokens"], d["sessions"]), (500000, ["known"]))

    def test_events_only_in_an_archive_month_explain_a_turn(self):
        env = self.setup_spend([("known", 59, 40000), ("known", 10, 40000)], events=())
        p = c.find_project(self.repo)
        os.makedirs(os.path.join(p.dir, "archive"), exist_ok=True)
        import datetime
        now = datetime.datetime.now(datetime.timezone.utc)
        with open(os.path.join(p.dir, "archive", now.strftime("ledger-%Y-%m.jsonl")), "w") as f:
            for m in (60, 11):
                ts = (now - datetime.timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%SZ")
                f.write(json.dumps({"ts": ts, "session_id": "known", "task": "T-0001", "event": "note", "data": {}}) + "\n")
        self.assertEqual(json.loads(self.fm("cost", "--json", env=env).stdout)["unexplained"]["tokens"], 0)

    def test_a_turn_with_an_unparseable_time_is_left_out_of_the_total(self):
        env = self.setup_spend([("stray", 10, 40000)])
        folder = os.path.join(env["CLAUDE_CONFIG_DIR"], "projects")
        path = os.path.join(folder, os.listdir(folder)[0], "t.jsonl")
        with open(path, "a") as f:
            f.write(json.dumps({"type": "assistant", "timestamp": "garbage", "sessionId": "x", "message": {
                "id": "mx", "content": [], "usage": {"output_tokens": 999999}}}) + "\n")
        d = json.loads(self.fm("cost", "--json", env=env).stdout)["unexplained"]
        self.assertEqual((d["tokens"], d["share"]), (200000, 100.0))

    def test_explained_or_small_spend_shows_no_line(self):
        env = self.setup_spend([("known", 59, 40000), ("stray", 10, 1000)])  # the stray is under 20% and 100k
        out = self.fm("cost", env=env).stdout + self.fm("digest", env=env).stdout
        self.assertNotIn("Unexplained", out)


class TaskAt(unittest.TestCase):
    def test_a_turn_goes_to_the_newest_event_at_or_before_it(self):
        # T-1116: a bisect replaced the per-turn linear scan (4 s of fm cost's 6.8 s on a real ledger)
        from sampo import cost
        line = [("2026-10-01T00:00:00", "T-1"), ("2026-10-01T00:00:00", "T-2"), ("2026-10-02T00:00:00", "T-3")]
        self.assertIsNone(cost._task_at(line, "2026-09-30T23:59:59"))
        self.assertEqual(cost._task_at(line, "2026-10-01T00:00:00"), "T-2")  # same second: the later event
        self.assertEqual(cost._task_at(line, "2026-10-01T12:00:00"), "T-2")
        self.assertEqual(cost._task_at(line, "2026-10-03T00:00:00"), "T-3")
        self.assertIsNone(cost._task_at([], "2026-10-03T00:00:00"))
        big = [(f"2026-10-{d:02d}T{h:02d}:{m:02d}:{s:02d}", "T-1") for d in range(1, 3) for h in range(24)
               for m in range(60) for s in range(0, 60, 2)]  # 172,800 events
        start = time.process_time()
        for _ in range(2000):  # a linear scan: ~340M steps; a bisect: ~36k
            cost._task_at(big, "2026-10-02T23:59:59")
        self.assertLess(time.process_time() - start, 1.0)  # CPU: in-process bisect bar


class TranscriptPrefilter(unittest.TestCase):
    def test_old_files_and_lines_are_skipped_but_a_nested_old_stamp_hides_nothing(self):
        # T-1116: fm cost parsed 559 MB of transcripts twice; files and lines wholly before the cutoff are skipped now
        import tempfile
        from sampo import cost
        with tempfile.TemporaryDirectory() as d:
            new = {"type": "assistant", "timestamp": "2026-10-10T00:00:00.000Z", "sessionId": "s",
                   "message": {"id": "m2", "usage": {"input_tokens": 5}, "content": [
                       {"type": "tool_use", "id": "u", "name": "Bash", "input": {"timestamp": "2020-01-01T00:00:00"}}]}}
            old = dict(new, timestamp="2026-09-01T00:00:00.000Z", message={"id": "m1", "usage": {"input_tokens": 7}})
            with open(os.path.join(d, "a.jsonl"), "w") as f:
                f.write(json.dumps(old, separators=(",", ":")) + "\n" + json.dumps(new, separators=(",", ":")) + "\n")
            with open(os.path.join(d, "stale.jsonl"), "w") as f:
                f.write(json.dumps(dict(new, message={"id": "m3", "usage": {"input_tokens": 9}})) + "\n")
            os.utime(os.path.join(d, "stale.jsonl"), (0, 0))  # not written since the cutoff: never opened
            msgs, _ = cost.scan(d, "2026-10-01T00:00:00")
            self.assertEqual([u["input_tokens"] for _, _, u in msgs], [5])
