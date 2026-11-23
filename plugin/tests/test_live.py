"""T-2998: sampo watch --web: a read-only, loopback-only server (settingsserve's token, cookie, Host check and CSP) with one
snapshot at /api/state and the same as a Server-Sent Events stream at /api/stream. A collector that fails leaves its field
empty; the snapshot names jobs, loaded models with VRAM, the GPU and the last minute's flows from calls already logged."""
import http.client
import json
import os
import threading
import time
import unittest.mock as mock

from helpers import ForemanTestCase

from sampo import core as c
from sampo import live

FIELDS = {"at", "project", "focus", "queue", "closed_today", "lanes", "jobs", "models", "gpu", "flows", "events", "stats",
          "board"}


class Snapshot(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("task", "new", "Show the live page", "--type", "FEATURE", "--tier", "S", "--ac", "it shows :: true",
                "--step", "build it", "--focus")
        self.p = c.find_project(self.repo)
        live._CACHE.clear()

    def test_every_field_of_the_contract_is_there_and_json(self):
        s = live.snapshot(self.p)
        self.assertEqual(set(s), FIELDS)
        json.dumps(s)
        self.assertEqual(s["project"]["slug"], self.p.slug)
        self.assertEqual(s["focus"]["step"], "0/1")
        self.assertEqual(s["focus"]["title"], "Show the live page")
        self.assertEqual([q["title"] for q in s["queue"]], ["Show the live page"])

    def test_stats_carry_the_moons_pace_for_the_page(self):  # T-3122: kept per hour and the projected date for 10,000
        pace = live.stats(self.p)["moons"]["pace"]
        self.assertEqual((pace["kept"], pace["target"], pace["finish"]), (0, 10000, None))
        self.assertEqual(set(pace["per_hour"]), {"1h", "6h", "24h"})
        json.dumps(pace)

    def test_stats_carry_the_card_fill_kept_rate_and_eta_for_the_page(self):  # T-3105: the fill to 30,000 kept
        fill = live.stats(self.p)["cards"]["fill"]
        self.assertEqual((fill["kept"], fill["target"], fill["eta_hours"]), (0, 30000, None))
        self.assertEqual(set(fill), {"kept", "target", "per_hour", "eta_hours"})
        json.dumps(fill)

    def test_the_snapshot_never_waits_on_the_slow_stats_and_two_clients_compute_them_once(self):  # T-4071
        gate, calls = threading.Event(), []

        def slow(p):  # moonscale.status read 569k rows: 12.8 s a snapshot, past 120 s with clients piling up
            calls.append(1)
            gate.wait(10)
            return {"total": 7, "stages": {}}
        with mock.patch("sampo.moonscale.status", side_effect=slow):
            got = []
            ts = [threading.Thread(target=lambda: got.append(live.snapshot(self.p)["stats"])) for _ in range(2)]
            t0 = time.monotonic()
            for t in ts:
                t.start()
            for t in ts:
                t.join(5)
            self.assertLess(time.monotonic() - t0, 5, "a snapshot waited on the slow stats")
            self.assertEqual(len(got), 2)
            self.assertNotIn("moons", got[0])  # not there yet: the page shows them on a later snapshot
            refresh = live._STALE[("stats", self.p.dir)]  # gone from the map once it's done
            gate.set()
            refresh.join(10)
            self.assertEqual(len(calls), 1, "two clients each read every moon")
            live._CACHE.pop(("snap", self.p.dir))
            self.assertEqual(live.snapshot(self.p)["stats"]["moons"]["candidates"], 7)

    def test_a_builder_lane_carries_its_steps_done_of_total_and_stage(self):  # T-3083
        b = c.load_briefs(self.p)[0]
        with mock.patch.object(live, "_briefs", return_value=[b]):
            b.meta["lane"] = "agent-x"
            row = live.lanes(self.p)[0]
        self.assertEqual((row["task"], row["step"]), (b.id, "0/1"))
        self.assertTrue(row["stage"])
        self.assertEqual(live.focus(self.p)["step"], "0/1")

    def test_a_returned_or_merged_lane_is_waiting_not_at_work(self):  # T-3170
        b = c.load_briefs(self.p)[0]
        with mock.patch.object(live, "_briefs", return_value=[b]):
            b.meta["lane"] = "agent-x"
            self.assertFalse(live.lanes(self.p)[0]["waiting"])
            b.meta["builder_returned"] = "now"
            self.assertTrue(live.lanes(self.p)[0]["waiting"])
            del b.meta["builder_returned"]
            with mock.patch("sampo.lanes._stop", return_value=("merged", 0.0, "")):
                self.assertTrue(live.lanes(self.p)[0]["waiting"])
        js = open(os.path.join(os.path.dirname(live.__file__), "..", "..", "web", "live", "live.js")).read()
        self.assertIn("waiting to merge", js)

    def test_a_failing_collector_leaves_its_field_empty_the_rest_stand(self):
        def boom(p):
            raise RuntimeError("down")
        with mock.patch.dict(live.COLLECTORS, {"gpu": boom, "models": boom, "jobs": boom, "flows": boom}):
            s = live.snapshot(self.p)
        self.assertEqual((s["gpu"], s["models"], s["jobs"], s["flows"]), (None, [], [], []))
        self.assertEqual(s["project"]["slug"], self.p.slug)

    def test_jobs_models_gpu_and_flows_from_fakes(self):
        procs = [(4242, "python3 plugin/bin/sampo moons funnel --stage kill"), (1, "bash")]
        ps = {"models": [{"name": "qwen3:14b", "size_vram": 9728 * 1048576, "size": 10000 * 1048576}]}
        smi = "NVIDIA RTX 4090, 11000, 24564, 37\n"
        with mock.patch.object(live, "_procs", return_value=procs), mock.patch.object(live, "_get_json", side_effect=[ps, None]), \
                mock.patch.object(live, "_smi", return_value=smi):
            c.log_event(self.p, "cards_grow", data={"calls": 3, "models": {"qwen3:14b": 3}})
            s = live.snapshot(self.p)
        self.assertEqual([(j["name"], j["kind"], j["pid"]) for j in s["jobs"]], [("moons funnel --stage kill", "funnel", 4242)])
        m = {x["name"]: x for x in s["models"]}["qwen3:14b"]
        self.assertEqual((m["family"], m["where"], m["loaded"], m["vram_mb"]), ("ollama", "gpu", True, 9728))
        self.assertEqual(s["gpu"], {"name": "NVIDIA RTX 4090", "used_mb": 11000, "total_mb": 24564, "util_pct": 37})
        self.assertEqual([(f["from"], f["to"], f["kind"]) for f in s["flows"]], [("card grow", "qwen3:14b", "write")])
        self.assertIn("cards_grow", [e["kind"] for e in s["events"]])

    def test_spend_today_is_dollars_per_hour_for_the_chart(self):  # T-3092: the time series has data
        t0 = time.localtime()
        start = time.mktime((t0.tm_year, t0.tm_mon, t0.tm_mday, 0, 0, 0, 0, 0, -1))
        rows = [{"ts": c.iso(start + 30), "usd": 1.5}, {"ts": c.iso(start + 40), "usd": 0.25}, {"ts": c.iso(start - 99), "usd": 9},
                {"ts": "junk", "usd": 1}, {"ts": c.iso(start + 5), "usd": "nan"}, {"ts": c.iso(start + 5), "usd": None}]
        path = os.path.join(c.state_dir(), "spend.jsonl")
        os.makedirs(c.state_dir(), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(json.dumps(r) for r in rows) + "\n")
        hrs = live.spend_today()
        self.assertEqual(len(hrs), t0.tm_hour + 1)
        self.assertEqual(hrs[0], 1.75)
        self.assertEqual(sum(hrs), 1.75)

    def test_flows_older_than_a_minute_are_out_and_event_text_is_redacted(self):
        old = c.log_event(self.p, "cards_grow", data={"models": {"haiku": 1}})
        with mock.patch.object(c, "now", return_value="2026-01-01T00:00:00Z"):
            c.log_event(self.p, "cards_grow", data={"models": {"old-one": 1}, "note": "sk-ant-api03-" + "a" * 40})
        s = live.snapshot(self.p)
        self.assertEqual([f["to"] for f in s["flows"]], ["haiku"])
        self.assertNotIn("a" * 40, json.dumps(s))
        self.assertTrue(old)

    def test_logged_model_calls_become_lanterns_and_flows(self):  # T-3014 live: Jarvis and Smith never showed
        from sampo import models
        with mock.patch.object(models.sys, "argv", ["sampo", "exam", "run", "s5-019", "--model", "jarvis"]):
            models.note_call({"name": "jarvis", "model": "qwen3.8-27b", "base_url": "https://jarvis.example/llm/v1"})
        with mock.patch.object(models.sys, "argv", ["sampo", "playbooks", "small", "grow"]):
            models.note_call({"name": "gpu-qwen4", "model": "qwen3.5:4b", "base_url": "http://127.0.0.1:11434/v1"})
        with mock.patch.object(live, "_get_json", return_value=None):
            s = live.snapshot(self.p)
        self.assertEqual(sorted((f["from"], f["to"], f["kind"]) for f in s["flows"]),
                         [("exam run", "jarvis", "act"), ("playbooks small grow", "qwen3.5:4b", "write")])
        m = {x["name"]: x for x in s["models"]}
        self.assertEqual((m["jarvis"]["where"], m["jarvis"]["calls_5m"]), ("remote", 1))

    def test_a_called_but_unloaded_local_model_hangs_on_the_gantry(self):  # T-3017
        from sampo import models
        models.note_call({"name": "gpu-gemma", "model": "gemma4-12b-32k", "base_url": "http://127.0.0.1:11434/v1"})
        models.note_call({"name": "jarvis", "model": "qwen3.8-27b", "base_url": "https://jarvis.example/v1"})
        with open(os.path.join(c.state_dir(), models.CALLS), "a") as f:  # an old row, no where
            f.write(json.dumps({"ts": c.now(), "to": "old-model", "from": "sampo"}) + "\n")
        with mock.patch.object(live, "_get_json", return_value=None):
            m = {x["name"]: x["where"] for x in live.snapshot(self.p)["models"]}
        self.assertEqual((m["gemma4-12b-32k"], m["jarvis"], m["old-model"]), ("gpu", "remote", "remote"))

    def test_a_ledger_row_seen_first_does_not_hide_a_local_calls_row(self):  # T-3017
        from sampo import models
        models.note_call({"name": "gpu-gemma", "model": "gemma4-12b-32k", "base_url": "http://127.0.0.1:11434/v1"})
        with mock.patch.object(c, "now", return_value=c.iso(time.time() + 60)):  # newer, so it sorts first
            c.log_event(self.p, "cards_grow", data={"models": {"gemma4-12b-32k": 1}})
        with mock.patch.object(live, "_get_json", return_value=None):
            m = {x["name"]: x["where"] for x in live.snapshot(self.p)["models"]}
        self.assertEqual(m["gemma4-12b-32k"], "gpu")

    def test_a_child_under_a_temp_home_logs_to_the_inherited_file(self):  # T-3014: exam Smith runs
        from sampo import models
        path = os.path.join(self.tmp, "real-calls.jsonl")
        with mock.patch.dict(os.environ, {"FOREMAN_CALLS": path}):
            models.note_call({"name": "jarvis", "model": "qwen3.8-27b", "base_url": "https://jarvis.example/v1"})
        with open(path) as f:
            self.assertEqual(json.loads(f.read())["to"], "jarvis")
        self.assertFalse(os.path.exists(os.path.join(c.state_dir(), models.CALLS)))

    def test_complete_and_embed_log_their_calls_even_when_the_call_fails(self):  # T-3014 review
        from sampo import models
        prof = {"name": "gpu-x", "kind": "openai", "base_url": "http://127.0.0.1:11434/v1", "model": "m:1", "key_env": None}
        with mock.patch.object(models, "_post", side_effect=models.ModelError("down")):
            for call in (lambda: models.complete(prof, [{"role": "user", "content": "hi"}]),
                         lambda: models.embed(prof, ["a"])):
                with self.assertRaises(models.ModelError):
                    call()
        with open(os.path.join(c.state_dir(), models.CALLS)) as f:
            rows = [json.loads(x) for x in f]
        self.assertEqual([(r["to"], r.get("kind")) for r in rows], [("m:1", None), ("m:1", "embed")])
        with mock.patch.object(live, "_get_json", return_value=None):
            self.assertIn("embed", [f["kind"] for f in live.snapshot(self.p)["flows"]])

    def test_the_cli_hands_its_call_log_to_children(self):  # T-3014 review: an exam's Smith under a temp home
        from sampo import cli
        with mock.patch.dict(os.environ), mock.patch.object(cli.sys, "argv", ["sampo", "--version"]):
            os.environ.pop("FOREMAN_CALLS", None)
            with self.assertRaises(SystemExit):
                cli.main()
            self.assertEqual(os.environ.get("FOREMAN_CALLS"), os.path.join(c.state_dir(), "calls.jsonl"))

    def test_the_call_log_rotates_past_its_cap(self):
        from sampo import models
        with mock.patch.object(models, "CALLS_CAP", 200):
            for _ in range(10):
                models.note_call({"name": "gpu-qwen4", "model": "qwen3.5:4b", "base_url": "http://127.0.0.1:11434/v1"})
        path = os.path.join(c.state_dir(), models.CALLS)
        self.assertLessEqual(os.path.getsize(path), 200 + 200)
        self.assertTrue(os.path.exists(path + ".1"))

    def test_events_read_in_words(self):  # T-3014 live: the ticker printed {"file": "/home/…", "tool": "Edit"}
        c.log_event(self.p, "touched", task="T-0001", data={"file": "/home/x/proj/plugin/tests/test_traces.py", "tool": "Edit"})
        c.log_event(self.p, "evidence", task="T-0001", data={"step": 3, "ac": None, "cmd": "python3 plugin/bin/sampo shot x"})
        text = {e["kind"]: e["text"] for e in live.snapshot(self.p)["events"]}
        self.assertEqual(text["touched"], "T-0001 Edit test_traces.py")
        self.assertEqual(text["evidence"], "T-0001 step 3 · python3 plugin/bin/sampo shot x")
        self.assertNotIn("{", text["touched"] + text["evidence"])

    def test_the_snapshot_is_cached_a_moment(self):
        with mock.patch.object(live, "_procs", return_value=[]) as g:
            live.snapshot(self.p)
            live.snapshot(self.p)
        self.assertEqual(g.call_count, 1)

    def test_each_loop_is_named_by_its_verb_or_its_old_name(self):  # T-3044: moons grow and genesis exam never showed
        cmds = {"sampo moons grow 200": "grow", "python3 plugin/bin/sampo genesis moonshot grow": "grow",
                "fm genesis moonshot funnel --stage kill": "funnel", "sampo -p foreman-4a056f moons funnel": "funnel",
                "sampo --project=x exam run s5 --agent smith": "exam", "sampo louhi run s5": "exam",
                "sampo genesis exam --arm oracle": "exam", "sampo rune exam": "exam", "sampo louhi models": "bench",
                "sampo playbooks small grow": "grow", "sampo genesis moonshot steer": None, "sampo task list": None,
                # review: a word in the args is no train job; -p SLUG anywhere names no verb
                'sampo task new "fix jeff-train"': None, 'python3 plugin/bin/sampo capture "jeffexport is slow"': None,
                "python3 /x/jeff-train --profile decide": "train", "/v/bin/python3 -u /v/bin/jeff-train --profile d": "train",
                "sampo moons -p x funnel": "funnel", "fm genesis --project=x moonshot grow 50": "grow",
                # T-3199: an option's value is no program
                "python3 -X dev plugin/bin/sampo moons grow 10": "grow", "python3 -W ignore /x/jeff-train --p d": "train",
                "python3 -X importtime -u plugin/bin/sampo louhi run s5": "exam",
                "python3 -m sampo moons grow 5": "grow", "python3 -c 'import sampo' moons grow": None}
        for cmd, kind in cmds.items():
            with mock.patch.object(live, "_procs", return_value=[(4242, cmd)]):
                got = live.jobs(self.p)
            self.assertEqual(got[0]["kind"] if got else None, kind, cmd)
        with mock.patch.object(live, "_procs", return_value=[(4242, "sampo moons funnel -p foreman-4a056f --stage kill")]):
            self.assertEqual(live.jobs(self.p)[0]["name"], "moons funnel --stage kill")

    def test_each_queued_task_carries_its_steps_done_of_total_and_stage(self):  # T-3044
        q = live.queue(self.p)[0]
        self.assertEqual((q["step"], q["stage"]), ("0/1", "executing"))


class NativeRatio(ForemanTestCase):
    """T-6060: the Board's daily trend of Sampo's dollars per landed lane against the plain Claude Code reference."""

    def setUp(self):
        super().setUp()
        self.fm("task", "new", "Price the lanes", "--type", "FEATURE", "--tier", "S", "--ac", "it prices :: true",
                "--step", "build it", "--focus")
        self.p = c.find_project(self.repo)
        live._CACHE.clear()
        self.day = lambda back: c.iso(time.time() - back * 86400)[:10]

    def lanes(self):
        """T-0001 landed today for $0.40 and T-0002 for $0.20, T-0003 three days ago for $1.00 (reference $0.50)."""
        sh = lambda usd: {"usd": usd, "local": False, "tokens": {}}  # noqa: E731
        return {"days": 14, "landed": 3, "git": {}, "ledger": {}, "median": {}, "unattributed": {},
                "lanes": {"T-0001": {"lead": sh(0.1), "builder": sh(0.3)}, "T-0002": {"builder": sh(0.2)},
                          "T-0003": {"builder": sh(1.0)}},
                "landed_at": {"T-0001": self.day(0) + "T01:00:00", "T-0002": self.day(0) + "T02:00:00",
                              "T-0003": self.day(3) + "T02:00:00"}}

    def series(self):
        from sampo import cost
        ref = {"usd_per_task": 0.5, "n": 4, "source": "tests", "date": "2026-11-22", "measured": True}
        with mock.patch.object(cost, "native_reference", lambda p: ref):
            return cost.native_series(self.p, 14, priced=self.lanes())

    def test_native_ratio_is_one_point_per_day_over_14_days_with_the_10x_line(self):
        got = self.series()
        self.assertEqual(len(got["days"]), 14)
        self.assertEqual(got["target_ratio"], 0.1)
        self.assertAlmostEqual(got["target_usd"], 0.05)
        today, back3 = got["days"][-1], got["days"][-4]
        self.assertEqual((today["day"], back3["day"]), (self.day(0), self.day(3)))
        self.assertAlmostEqual(today["ratio"], 0.3 / 0.5, places=4)  # the median of $0.40 and $0.20
        self.assertAlmostEqual(back3["ratio"], 1.0 / 0.5, places=4)
        self.assertEqual((today["lanes"], back3["lanes"]), (2, 1))
        self.assertIsNone(got["days"][-2]["ratio"], "a day nothing landed is a gap, not zero")

    def test_the_snapshot_carries_the_trend_in_stats_from_the_same_numbers(self):
        from sampo import cost
        want = self.series()
        with mock.patch.object(cost, "native_series", lambda p, days=14, priced=None: want):
            live.stats(self.p, wait=True)
            for t in list(threading.enumerate()):  # the refresh runs in the background: let it finish
                if t is not threading.current_thread() and t.name.startswith("live-"):
                    t.join(10)
            s = live.stats(self.p, wait=True)
        self.assertEqual(s["native"], want)
        json.dumps(s["native"])

    def test_wait_true_never_blocks_on_the_transcript_read_either(self):
        from sampo import cost
        gate = threading.Event()
        with mock.patch.object(cost, "native_series", lambda p, days=14, priced=None: gate.wait(10) or {"days": []}):
            t0 = time.monotonic()
            live.stats(self.p, wait=True)
            took = time.monotonic() - t0
            gate.set()
        self.assertLess(took, 5)

    def test_an_error_in_the_trend_leaves_the_other_panels_intact(self):
        from sampo import cost
        def boom(p, days=14, priced=None):
            raise RuntimeError("transcripts unreadable")
        with mock.patch.object(cost, "native_series", boom):
            s = live.stats(self.p, wait=True)
            time.sleep(0.3)
            s = live.stats(self.p, wait=True)
        self.assertEqual(s["native"], {})
        self.assertIn("cards", s)

    def test_the_page_never_waits_on_the_transcript_read(self):
        from sampo import cost
        gate = threading.Event()
        with mock.patch.object(cost, "native_series", lambda p, days=14, priced=None: gate.wait(10) or {"days": []}):
            t0 = time.monotonic()
            s = live.stats(self.p, wait=False)
            took = time.monotonic() - t0
            gate.set()
        self.assertLess(took, 5)
        self.assertEqual(s["native"], {})

    def test_the_board_reads_the_trend_from_stats_native(self):
        js = open(os.path.join(c.PLUGIN_ROOT, "web", "live", "board.js")).read()
        self.assertIn("obj(s.stats).native", js)
        self.assertIn("target_ratio", js, "the 10x line is drawn from the same numbers")


class JobProgress(ForemanTestCase):  # T-3044: the loops the page shows write done of total as they go
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)
        c._PROG.clear()

    def _prog(self):
        try:
            with open(os.path.join(c.state_dir(), "progress", f"{os.getpid()}.json")) as f:
                return json.load(f)
        except FileNotFoundError:
            return None

    def test_a_funnel_stage_reports_rows_done_as_each_batch_lands(self):
        from test_moons_scale import reply
        from sampo import moonscale as ms
        with mock.patch.object(ms, "_ask", return_value=(reply(4), "jarvis")):
            ms.grow(self.p, 8, batch=4)
        seen, n = [], []

        def stage(p, r, rs, f, todo, cap):  # two batches, as a judged stage logs them
            n.append(len(todo))
            for part in (todo[:3], todo[3:]):
                ms._log(p, [ms._event(x["id"], "exact", "kept") for x in part], r, "exact")
                seen.append(self._prog())
        with mock.patch.dict(ms.__dict__, {"_exact": stage}):
            ms.funnel(self.p, stage="exact")
        self.assertGreater(n[0], 3)
        self.assertEqual([(x["done"], x["total"], x["unit"], x["note"]) for x in seen],
                         [(3, n[0], "rows", "exact"), (n[0], n[0], "rows", "exact")])
        self.assertIsNone(self._prog())

    def test_an_exam_run_counts_scenarios_answered_and_clears_at_the_end(self):
        from sampo import louhi
        src, seen, after = os.path.join(self.tmp, "answer-src"), [], []
        os.makedirs(src)
        os.makedirs(louhi._answers(self.p, "r1", "fake"))

        def fake(base, key):
            seen.append(self._prog())
            c.progress(1, 1, "rows", "grow")  # a loop nested in the exam (review: it reset the exam's bar and rate)
            c.progress_end()
            after.append(self._prog())
            return {"found": []}, src
        picked = [{"id": f"s{i}", "kind": "bug", "split": "holdout"} for i in range(3)]
        with mock.patch.dict(louhi.AGENTS, {"fake": fake}), \
                mock.patch.object(louhi, "load", return_value={"scenarios": picked}), \
                mock.patch.object(louhi, "_key", return_value={}), mock.patch.object(louhi, "grade", return_value={}), \
                mock.patch.object(louhi, "_pool", lambda fn, items: [fn(x) for x in items]):
            louhi._run_agent(self.p, "r1", "fake", "all", None, None, None, 20, None, 1, None, None)
        self.assertEqual([(x["done"], x["total"], x["unit"], x["note"]) for x in seen],
                         [(k, 3, "scenarios", "exam fake") for k in range(3)])
        self.assertEqual([(x["done"], x["note"]) for x in after], [(k, "exam fake") for k in range(3)])
        self.assertEqual(len({x["t0"] for x in seen + after}), 1, "one rate over the exam")
        self.assertIsNone(self._prog())

    def _genesis(self):
        from sampo import louhi
        g0 = os.path.join(self.tmp, "g0.md")
        with open(g0, "w") as f:
            f.write("super improve it\n\nMUST: full autonomy.\n")
        self.fm("genesis", "init", "--file", g0)
        return louhi

    def test_a_genesis_exam_counts_its_drafts_and_clears_at_the_end(self):
        from sampo import rune
        louhi = self._genesis()
        seen, after, render = [], [], louhi.render

        def fake(*a):
            seen.append(self._prog())
            c.progress(1, 1, "rows", "funnel")  # nested, as above
            c.progress_end()
            after.append(self._prog())
            return render(*a)
        with mock.patch.object(louhi, "render", side_effect=fake):
            r = rune.exam(self.p, arm="empty", per_kind=3)
        self.assertEqual([(x["done"], x["total"], x["unit"], x["note"]) for x in seen],
                         [(k, r["scenarios"], "scenarios", "genesis empty") for k in range(r["scenarios"])])
        self.assertEqual([(x["done"], x["note"]) for x in after], [(k, "genesis empty") for k in range(r["scenarios"])])
        self.assertEqual(len({x["t0"] for x in seen + after}), 1)
        self.assertIsNone(self._prog())

    def test_a_genesis_exam_that_raises_leaves_no_bar(self):  # review: the file stayed until the pid was gone
        from sampo import rune
        louhi = self._genesis()
        with mock.patch.object(louhi, "render", side_effect=OSError("disk full")), self.assertRaises(OSError):
            rune.exam(self.p, arm="empty", per_kind=3)
        self.assertIsNone(self._prog())
        self.assertEqual(c._OUTER, {})

    def test_a_keep_run_keeps_its_own_bar_after_the_funnel_inside_it_ends(self):  # the 10,000-moons fill
        from sampo import moonscale as ms
        seen, n, inner = [], [0], []

        def grow(p, target=None, **kw):  # a round as the real one writes it; called right after the real funnel ends
            seen.append(self._prog())
            n[0] += 5
            c.progress(5, 5, "moons", "grow")
            inner.append(self._prog()["note"])
            c.progress_end()
            return {"calls": 1, "added": 5, "kept": 5, "rejected": 0, "stopped": "target", "error": "",
                    "models": {}, "total": n[0], "reasons": {}}
        with mock.patch.object(ms, "grow", grow), mock.patch.object(ms, "_embedder", return_value=None):
            r = ms._keep(self.p, 2)
        self.assertEqual(r["stopped"], "stalled")
        self.assertEqual([(x["done"], x["total"], x["unit"], x["note"]) for x in seen if x], [(0, 2, "moons", "kept")] * 3)
        self.assertEqual(len(seen), 3, "the keep bar is there each time the funnel hands over")
        self.assertEqual(len({x["t0"] for x in seen}), 1, "one rate over the whole run: an inner end doesn't restart it")
        self.assertEqual(inner, ["kept 0/2 · grow"] * 3, "review: the keep count shows while the inner bar runs too")
        self.assertIsNone(self._prog())

    def test_a_card_fill_keeps_its_own_bar_after_the_grow_inside_it_ends(self):  # T-3105: the fill's kept count on the page
        from sampo import smallcards as sc
        seen, inner = [], []

        def grow(p, target=None, **kw):  # a round as the real one writes it, with the fill's bar already up
            seen.append(self._prog())
            c.progress(5, 5, "cards", "grow")
            inner.append(self._prog()["note"])
            c.progress_end()
            return {"calls": 1, "added": 5, "kept": 0, "rejected": 0, "stopped": "target", "error": "",
                    "models": {}, "total": 0, "reasons": {}}
        with mock.patch.object(sc, "grow", grow):
            r = sc._keep(self.p, 2)
        self.assertEqual(r["stopped"], "stalled")
        self.assertEqual([(x["done"], x["total"], x["unit"], x["note"]) for x in seen if x], [(0, 2, "cards", "kept")] * 3)
        self.assertEqual(inner, ["kept 0/2 · grow"] * 3, "the kept count shows while the inner bar runs too")
        self.assertIsNone(self._prog())


class Reload(ForemanTestCase):
    def test_it_reloads_when_its_code_changes(self):
        # T-3852: merged live-page work (T-3659, T-3662) stayed invisible for hours, until a hand restart
        self.fm("task", "new", "Serve it", "--type", "FEATURE", "--tier", "S", "--ac", "x :: true", "--step", "s", "--focus")
        server, _ = live.make(c.find_project(self.repo))
        ids = iter(["a", "a"])
        with mock.patch.object(live, "_code_id", lambda: next(ids, "b")), mock.patch.object(live, "RELOAD_S", 0):
            t = threading.Thread(target=live.serve, args=(server,), daemon=True)
            t.start()
            t.join(10)
        self.assertFalse(t.is_alive(), "serve returns once its code changed")
        self.assertTrue(server.reload)
        argv = ["/x/bin/sampo", "watch", "--web"]
        self.assertEqual(live._again(argv, 4321), argv + ["--port", "4321"], "the same port, so open tabs reconnect")
        self.assertEqual(live._again(argv + ["--port", "4321"], 4321), argv + ["--port", "4321"])
        self.assertEqual(live._again(argv + ["--port=9"], 4321), argv + ["--port=9"])


class Serve(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("task", "new", "Serve it", "--type", "FEATURE", "--tier", "S", "--ac", "x :: true", "--step", "s", "--focus")
        live._CACHE.clear()
        self.p = c.find_project(self.repo)
        self.peer = mock.patch.object(live, "peer_uid", return_value=None)  # the real /proc says: this test's own uid
        self.peer_mock = self.peer.start()
        self.addCleanup(self.peer.stop)
        self.server, self.token = live.make(self.p)
        self.port = self.server.server_address[1]
        self.stop = threading.Event()
        self.server.stop = self.stop
        self.thread = threading.Thread(target=live.serve, args=(self.server, self.stop), daemon=True)
        self.thread.start()
        self.cookie = None

    def tearDown(self):
        self.stop.set()
        self.thread.join(5)
        super().tearDown()

    def req(self, path="/", cookie=True, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"Host": f"127.0.0.1:{self.port}", **(headers or {})}
        if cookie and self.cookie:
            h["Cookie"] = self.cookie
        conn.request("GET", path, headers=h)
        r = conn.getresponse()
        out = r.status, r.read().decode(), dict(r.getheaders())
        conn.close()
        return out

    def login(self):
        st, _, h = self.req(f"/?t={self.token}", cookie=False)
        self.assertEqual(st, 303)
        self.cookie = h["Set-Cookie"].split(";")[0]

    def test_bound_to_loopback_only(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

    def test_no_cookie_bad_host_and_the_token_works_once(self):
        for path in ("/", "/api/state", "/api/stream"):
            self.assertEqual(self.req(path, cookie=False)[0], 403)
        self.assertEqual(self.req("/", cookie=False, headers={"Host": "evil.example"})[0], 403)
        self.login()
        self.assertEqual(self.req(f"/?t={self.token}", cookie=False)[0], 403)  # spent
        self.assertEqual(self.req("/", headers={"Host": "evil.example"})[0], 403)  # a good cookie, a rebound host

    def test_state_is_json_with_the_contract_fields_and_a_csp(self):
        self.login()
        st, body, h = self.req("/api/state")
        self.assertEqual(st, 200)
        self.assertIn("application/json", h["Content-Type"])
        self.assertEqual(set(json.loads(body)), FIELDS)
        self.assertEqual(self.req("/api/nope")[0], 404)
        st, page, h = self.req("/")
        self.assertEqual(st, 200)
        self.assertIn("default-src 'none'", h["Content-Security-Policy"])
        self.assertNotIn("unsafe", h["Content-Security-Policy"])

    def test_the_live_theme_setting_sets_the_look_the_page_is_sent_with(self):  # T-3055
        from sampo import smithtui
        self.login()
        for want, cfg in (("modern", {}), ("forge", {"live_theme": "forge"}), ("modern", {"live_theme": "modern"})):
            with mock.patch.object(smithtui, "load_config", return_value=(cfg, [])):
                page = self.req("/")[1]
            self.assertIn(f'data-theme="{want}"', page if isinstance(page, str) else page.decode())
        with mock.patch.object(smithtui, "load_config", side_effect=OSError("no")):
            self.assertIn('data-theme="modern"', (lambda p: p if isinstance(p, str) else p.decode())(self.req("/")[1]))

    def test_a_collector_that_raises_is_not_a_500(self):
        self.login()

        def boom(p):
            raise OSError("no")
        with mock.patch.dict(live.COLLECTORS, {"jobs": boom, "stats": boom}):
            st, body, _ = self.req("/api/state")
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(body)["jobs"], [])

    def test_static_files_come_from_the_folder_and_nothing_outside_it(self):
        self.login()
        d = os.path.join(self.tmp, "web")
        os.makedirs(d)
        with open(os.path.join(d, "live.css"), "w") as f:
            f.write("body{}")
        with mock.patch.object(live, "WEB", d):
            st, body, h = self.req("/static/live.css")
            self.assertEqual((st, body, h["Content-Type"].split(";")[0]), (200, "body{}", "text/css"))
            self.assertEqual(self.req("/static/..%2Flive.css")[0], 404)
            self.assertEqual(self.req("/static/../../x")[0], 404)
            self.assertEqual(self.req("/static/missing.js")[0], 404)

    def test_fonts_are_served_as_bytes(self):  # T-3014 live: every font fell back, _static 404'd anything not text
        self.login()
        d = os.path.join(self.tmp, "web")
        os.makedirs(d)
        raw = b"wOF2\x00\x01\xff\xfe" * 8
        with open(os.path.join(d, "A-normal.woff2"), "wb") as f:
            f.write(raw)
        with mock.patch.object(live, "WEB", d):
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            conn.request("GET", "/static/A-normal.woff2", headers={"Host": f"127.0.0.1:{self.port}", "Cookie": self.cookie})
            r = conn.getresponse()
            self.assertEqual((r.status, r.getheader("Content-Type"), r.read()), (200, "font/woff2", raw))
            conn.close()
            self.assertEqual(self.req("/static/..%2FA-normal.woff2")[0], 404)

    def test_a_restart_keeps_an_open_tabs_cookie(self):  # T-3021 live: each restart 403'd the user's tab
        self.login()
        again, _ = live.make(self.p)
        try:
            self.assertEqual(again.session, self.server.session)
        finally:
            again.server_close()
        self.assertEqual(os.stat(os.path.join(self.p.dir, "live.session")).st_mode & 0o777, 0o600)
        st, body, _ = self.req("/api/state", cookie=False)
        self.assertEqual(st, 403)
        self.assertIn("sampo watch --web --url", body)

    def test_the_stream_sends_state_events(self):
        self.login()
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/stream", headers={"Host": f"127.0.0.1:{self.port}", "Cookie": self.cookie})
        r = conn.getresponse()
        self.assertEqual((r.status, r.getheader("Content-Type")), (200, "text/event-stream"))
        self.assertEqual(r.readline().decode().strip(), "event: state")
        data = r.readline().decode()
        self.assertTrue(data.startswith("data: "))
        self.assertEqual(set(json.loads(data[6:])), FIELDS)
        conn.close()

    def test_the_servers_own_uid_gets_in_with_no_link_another_uid_does_not(self):  # T-3023
        self.peer_mock.return_value = os.getuid()
        self.assertEqual(self.req("/api/state", cookie=False)[0], 200)
        self.assertEqual(self.req("/api/state", cookie=False, headers={"Host": "evil.example"})[0], 403)  # Host still applies
        self.peer_mock.return_value = os.getuid() + 1
        self.assertEqual(self.req("/api/state", cookie=False)[0], 403)
        self.peer_mock.return_value = None  # not found: the cookie path
        self.assertEqual(self.req("/api/state", cookie=False)[0], 403)
        self.login()
        self.assertEqual(self.req("/api/state")[0], 200)

    def test_machine_lists_projects_and_sessions_a_sensitive_one_by_name_only(self):  # T-3023
        self.login()
        other = os.path.join(self.tmp, "secretrepo")
        os.makedirs(other)
        sp = c.init_project(other, sensitive=True)
        sess = [{"session_id": "abcdef1234567890", "project": sp.slug, "cwd": other, "model": "Opus", "context_pct": 41,
                 "ts": c.now()},
                {"session_id": "99887766aaaa", "project": self.p.slug, "cwd": self.repo, "model": "Sonnet", "context_pct": 7,
                 "ts": c.now()}]
        with mock.patch("sampo.bus.live", return_value=sess):
            st, body, _ = self.req("/api/machine")
        self.assertEqual(st, 200)
        m = json.loads(body)
        mine = {x["slug"]: x for x in m["projects"]}
        self.assertEqual(mine[sp.slug], {"slug": sp.slug, "name": "secretrepo", "sensitive": True})
        self.assertEqual((mine[self.p.slug]["queue"], mine[self.p.slug]["task"], mine[self.p.slug]["drive"]),
                         (1, "T-0001", True))
        self.assertTrue(mine[self.p.slug]["stage"])
        ses = {x["id"]: x for x in m["sessions"]}
        self.assertEqual(ses["abcdef12"], {"id": "abcdef12", "project": sp.slug, "task": None, "model": None,
                                           "context_pct": 41, "seen": sess[0]["ts"]})
        self.assertEqual((ses["99887766"]["model"], ses["99887766"]["task"]), ("Sonnet", "T-0001"))

    def test_the_project_param_picks_a_snapshot_unknown_is_404_none_is_the_busiest_session(self):  # T-3023
        self.login()
        other = os.path.join(self.tmp, "otherrepo")
        os.makedirs(other)
        op = c.init_project(other, sensitive=False)
        self.assertEqual(json.loads(self.req(f"/api/state?project={op.slug}")[1])["project"]["slug"], op.slug)
        self.assertEqual(json.loads(self.req(f"/api/state?project={self.p.slug}")[1])["project"]["slug"], self.p.slug)
        self.assertEqual(self.req("/api/state?project=nope")[0], 404)
        self.assertEqual(self.req("/api/stream?project=nope")[0], 404)
        with mock.patch("sampo.bus.live", return_value=[{"session_id": "s1", "project": op.slug}]):
            self.assertEqual(json.loads(self.req("/api/state")[1])["project"]["slug"], op.slug)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", f"/api/stream?project={op.slug}", headers={"Host": f"127.0.0.1:{self.port}", "Cookie": self.cookie})
        r = conn.getresponse()
        r.readline()
        self.assertEqual(json.loads(r.readline().decode()[6:])["project"]["slug"], op.slug)
        conn.close()

    def test_a_sensitive_project_is_not_served_by_slug(self):  # T-3023
        self.login()
        other = os.path.join(self.tmp, "hush")
        os.makedirs(other)
        sp = c.init_project(other, sensitive=True)
        self.assertEqual(self.req(f"/api/state?project={sp.slug}")[0], 404)

    def _two_more(self):  # T-3730: an open project beside the server's own, and a sensitive one
        other = os.path.join(self.tmp, "shop-api")
        os.makedirs(other)
        op = c.init_project(other, sensitive=False)
        self.fm("task", "new", "Other work", "--type", "FIX", "--tier", "S", "--ac", "x :: true", "--step", "s", "--focus", cwd=other)
        hush = os.path.join(self.tmp, "hushrepo")
        os.makedirs(hush)
        sp = c.init_project(hush, sensitive=True)
        live._CACHE.clear()
        return op, sp

    def test_all_serves_every_open_project_tagged_a_sensitive_one_by_name_only(self):  # T-3730
        self.login()
        op, sp = self._two_more()
        st, body, _ = self.req("/api/state?project=all")
        self.assertEqual(st, 200)
        s = json.loads(body)
        self.assertEqual((s["project"]["slug"], s["all"]), ("all", True))
        names = {x["slug"]: x for x in s["projects"]}
        self.assertEqual(names[sp.slug], {"slug": sp.slug, "name": "hushrepo", "sensitive": True})
        self.assertEqual({x["project"] for x in s["queue"]}, {self.p.slug, op.slug})
        self.assertTrue(all(x["project"] for x in s["board"]["lanes"]))
        for key in ("trains", "loops"):
            self.assertTrue(all(x["project"] in (self.p.slug, op.slug) for x in s["board"][key]), key)
        self.assertEqual({x["project"] for x in s["board"]["trains"]}, {self.p.slug, op.slug})
        self.assertNotIn(sp.slug, json.dumps([s["queue"], s["lanes"], s["board"]]))  # a sensitive project adds no items
        self.assertTrue({"models", "gpu", "stats", "events", "flows", "jobs", "at"} <= set(s))  # the machine-wide fields, once

    def test_all_streams_and_an_unknown_slug_is_still_404(self):  # T-3730
        self.login()
        op, _ = self._two_more()
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/stream?project=all", headers={"Host": f"127.0.0.1:{self.port}", "Cookie": self.cookie})
        r = conn.getresponse()
        r.readline()
        s = json.loads(r.readline().decode()[6:])
        conn.close()
        self.assertEqual((s["project"]["slug"], s["all"]), ("all", True))
        self.assertIn(op.slug, {x["project"] for x in s["queue"]})
        self.assertEqual(self.req("/api/state?project=al")[0], 404)

    def test_all_builds_other_projects_at_most_every_ten_seconds(self):  # T-3730
        op, _ = self._two_more()
        n = {"train": 0}
        from sampo import board as bd
        real = bd.part_train

        def counted(p):
            n["train"] += p.slug == op.slug
            return real(p)
        with mock.patch("sampo.board.part_train", counted), mock.patch.object(live, "CACHE_S", 0):
            live.snapshot_all(self.p)
            live.snapshot_all(self.p)
            live.snapshot_all(self.p)
        self.assertEqual(n["train"], 1)  # the other project's slice came from its cache the 2nd and 3rd time
        with mock.patch("sampo.board.part_train", counted), mock.patch.object(live, "CACHE_S", 0), \
                mock.patch.object(live, "ALL_S", 0):
            live.snapshot_all(self.p)
        self.assertEqual(n["train"], 2)

    def _open(self, qs="", port=None):  # T-5549: a stream; its reader, and the time the request went out
        port = port or self.port
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        t0 = time.monotonic()
        conn.request("GET", "/api/stream" + qs, headers={"Host": f"127.0.0.1:{port}", "Cookie": self.cookie})
        return conn, conn.getresponse(), t0

    def _frame(self, r):  # T-5549: the next state event's frame
        while True:
            line = r.readline().decode()
            if not line:
                raise AssertionError("the stream ended before a state event")
            if line.startswith("data: "):
                return json.loads(line[6:])

    def _kept_lands(self, path):  # T-6013: wait until the kept file holds a whole frame (a server writes it after publishing)
        end = time.monotonic() + 5
        while time.monotonic() < end:
            try:
                with open(path, encoding="utf-8") as f:
                    if isinstance(json.load(f), dict):
                        return
            except (OSError, ValueError):
                pass
            time.sleep(0.02)

    def _restart(self):  # T-5549: a second server on the same project (a restart), serving on its own port; its
        # session secret is its own, so this logs in there and the cookie after it is the new server's
        again, token = live.make(self.p)
        stop = threading.Event()
        again.stop = stop
        t = threading.Thread(target=live.serve, args=(again, stop), daemon=True)
        t.start()
        self.addCleanup(lambda: (stop.set(), t.join(5)))
        port = again.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", f"/?t={token}", headers={"Host": f"127.0.0.1:{port}"})
        r = conn.getresponse()
        r.read()
        conn.close()
        self.assertEqual(r.status, 303)
        self.cookie = r.getheader("Set-Cookie").split(";")[0]
        return port

    def test_first_frame_a_new_stream_gets_the_last_frame_at_once_marked_stale(self):  # T-5549
        self.login()
        gate, started = threading.Event(), threading.Event()
        gate.set()
        stamp = {"v": "a"}
        real = live.COLLECTORS["project"]

        def held(p):  # a build that takes long once the gate is closed
            started.set()
            gate.wait(10)
            return real(p)
        self.addCleanup(gate.set)
        with mock.patch.dict(live.COLLECTORS, {"project": held}), mock.patch.object(live, "_stamp", lambda p: stamp["v"]), \
                mock.patch.object(live, "STREAM_S", 0.1):
            c1, r1, _ = self._open()
            fresh = self._frame(r1)
            self.assertFalse(fresh.get("stale"), "a frame built for this stream is not stale")
            gate.clear()
            started.clear()  # the first build set it: wait for the held one
            stamp["v"] = "b"  # the inputs changed: the next build is due and holds
            self.assertTrue(started.wait(5))
            c2, r2, t0 = self._open()
            second = self._frame(r2)
            took = time.monotonic() - t0
            self.assertLess(took, 1.0, "the new stream's first state event does not wait for the held build")
            self.assertEqual((second.get("stale"), second["at"]), (True, fresh["at"]), "the last built frame, marked stale")
            self.assertTrue(FIELDS <= set(second))
            gate.set()
            self.assertFalse(self._frame(r1).get("stale"), "the next fresh frame clears the stale mark")
            c1.close()
            c2.close()

    def test_first_frame_cold_a_restart_paints_the_kept_frame_while_a_build_is_held(self):  # T-5549
        self.login()
        c1, r1, _ = self._open()
        built = self._frame(r1)
        c1.close()
        self._kept_lands(os.path.join(self.p.dir, "live-frame.json"))
        self.assertTrue(os.path.exists(os.path.join(self.p.dir, "live-frame.json")), "a built frame is kept on disk")
        live._CACHE.clear()
        gate, started = threading.Event(), threading.Event()
        real = live.COLLECTORS["project"]

        def held(p):  # the cold build of a fresh server: it takes long
            started.set()
            gate.wait(10)
            return real(p)
        self.addCleanup(gate.set)
        with mock.patch.dict(live.COLLECTORS, {"project": held}):
            port = self._restart()
            c2, r2, t0 = self._open(port=port)
            first = self._frame(r2)
            took = time.monotonic() - t0
            gate.set()
            c2.close()
        self.assertLess(took, 1.0, "the first frame paints within 1 s of a cold start under a long build")
        self.assertEqual((first.get("stale"), first["at"]), (True, built["at"]))
        self.assertEqual(first["project"]["slug"], self.p.slug)

    def test_first_frame_a_corrupt_or_linked_kept_frame_is_not_served(self):  # T-5549
        self.login()
        c1, r1, _ = self._open()
        self._frame(r1)
        c1.close()
        path = os.path.join(self.p.dir, "live-frame.json")
        self._kept_lands(path)  # T-6013: the server keeps the frame just after it publishes it: let that write land first
        with open(path, "w", encoding="utf-8") as f:  # a torn or foreign file: nothing is served from it
            f.write("{not json")
        c2, r2, _ = self._open(port=self._restart())
        self.assertFalse(self._frame(r2).get("stale"), "a corrupt kept frame is ignored: a fresh build answers")
        c2.close()
        self._kept_lands(path)  # the restart's own build keeps its frame over the corrupt file: wait for it, then remove
        os.remove(path)
        planted = os.path.join(self.tmp, "planted.json")
        with open(planted, "w", encoding="utf-8") as f:
            json.dump({"at": "2000-01-01T00:00:00", "project": {}}, f)
        os.symlink(planted, path)  # a link to a file outside the state folder is not read either
        c3, r3, _ = self._open(port=self._restart())
        third = self._frame(r3)
        c3.close()
        self.assertFalse(third.get("stale"))
        self.assertNotEqual(third["at"], "2000-01-01T00:00:00")

    def test_shared_build_two_streams_share_one_build_and_an_unchanged_stamp_skips_it(self):  # T-5549
        self.login()
        n = {"builds": 0}
        real = live.COLLECTORS["project"]

        def counted(p):
            n["builds"] += 1
            return real(p)
        stamp = {"v": "a"}
        with mock.patch.dict(live.COLLECTORS, {"project": counted}), mock.patch.object(live, "_stamp", lambda p: stamp["v"]), \
                mock.patch.object(live, "STREAM_S", 0.1):
            c1, r1, _ = self._open()
            c2, r2, _ = self._open()
            names = [t.name for t in threading.enumerate() if t.name == "live-frames"]
            self.assertEqual(len(names), 1, "one builder thread for every stream, not one per stream")
            for r in (r1, r2):
                for _ in range(3):
                    self._frame(r)
            self.assertEqual(n["builds"], 1, "two streams, three ticks each: one build, the stamp never changed")
            stamp["v"] = "b"  # a changed stamp: one more build, shared by both streams
            for _ in range(40):
                self._frame(r1)
                self._frame(r2)
                if n["builds"] >= 2:
                    break
            self.assertEqual(n["builds"], 2)
            c1.close()
            c2.close()

    def test_shared_build_the_ceiling_rebuilds_an_unchanged_stamp(self):  # T-5549
        self.login()
        n = {"builds": 0}
        real = live.COLLECTORS["project"]

        def counted(p):
            n["builds"] += 1
            return real(p)
        with mock.patch.dict(live.COLLECTORS, {"project": counted}), mock.patch.object(live, "_stamp", lambda p: "same"), \
                mock.patch.object(live, "STREAM_S", 0.1), mock.patch.object(live, "FRAME_MAX_S", 0):
            c1, r1, _ = self._open()
            for _ in range(3):
                self._frame(r1)
            c1.close()
        self.assertGreaterEqual(n["builds"], 2, "a frame older than FRAME_MAX_S is rebuilt even when the stamp is unchanged")

    def test_shared_build_events_moving_do_not_rebuild_the_frame(self):  # T-5549: a hook writes events every tick
        self.login()
        n = {"builds": 0}
        real = live.COLLECTORS["project"]

        def counted(p):
            n["builds"] += 1
            return real(p)
        path = os.path.join(c.state_dir(), "events.jsonl")
        with mock.patch.dict(live.COLLECTORS, {"project": counted}), mock.patch.object(live, "STREAM_S", 0.1):
            c1, r1, _ = self._open()
            self._frame(r1)
            for _ in range(10):  # the event log is no input of the frame: its writes cost no build
                with open(path, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"ts": c.now(), "kind": "guard_block", "rule": "rm-rf", "tool": "Bash",
                                        "project": self.p.slug, "cmd": "rm -rf x"}) + "\n")
                self._frame(r1)
            c1.close()
        self.assertEqual(n["builds"], 1, "ten events moved and no rebuild: FRAME_MAX_S bounds how stale the ticker is")

    def test_first_frame_a_failed_build_holds_the_frame_as_stale(self):  # T-5549
        self.login()
        stamp, broke = {"v": "a"}, {"on": False}
        real = live.snapshot

        def failing(p, fresh=False):
            if broke["on"]:
                raise RuntimeError("a build that raises")
            return real(p, fresh=fresh)
        with mock.patch.object(live, "_stamp", lambda p: stamp["v"]), mock.patch.object(live, "STREAM_S", 0.1), \
                mock.patch.object(live, "snapshot", failing):
            c1, r1, _ = self._open()
            self.assertFalse(self._frame(r1).get("stale"))
            broke["on"], stamp["v"] = True, "b"  # the next build is due, and it raises
            fr = self.server.frames[("p", self.p.dir)]
            for _ in range(100):
                if fr.redo:
                    break
                time.sleep(0.05)
            self.assertTrue(fr.redo, "the failed build is noted for a retry")
            c2, r2, _ = self._open()
            self.assertTrue(self._frame(r2).get("stale"), "the held frame after a failed build is marked stale")
            c1.close()
            c2.close()

    def test_first_frame_a_kept_frame_of_another_owner_or_over_the_cap_is_not_read(self):  # T-5549
        path = os.path.join(self.p.dir, "live-frame.json")
        body = json.dumps({"at": "2000-01-01T00:00:00", "project": {}}).encode("utf-8")
        with open(path, "wb") as f:
            f.write(body)

        def build():
            return {"at": c.now()}
        self.assertIsNotNone(live.Frames(build, lambda: None, path).frame, "this uid's kept frame is read")
        with mock.patch.object(live.os, "getuid", return_value=os.getuid() + 1):  # a file another uid owns
            self.assertIsNone(live.Frames(build, lambda: None, path).frame)
        with mock.patch.object(live, "KEPT_MAX", len(body) - 1):  # larger than the cap: not read
            self.assertIsNone(live.Frames(build, lambda: None, path).frame)

    def test_strip_hidden_drops_a_sensitive_projects_items_and_refuses_its_slug_elsewhere(self):  # T-5549
        hidden = type("P", (), {"slug": "secret-zz", "root": "/x/secret-zz", "dir": "/x"})()
        open_ = type("P", (), {"slug": "open-yy", "root": "/x/open-yy", "dir": "/x"})()
        frame = {"at": "t", "queue": [{"project": "secret-zz", "id": "a"}, {"project": "open-yy", "id": "b"}],
                 "lanes": [], "board": {"lanes": [{"project": "secret-zz"}], "asks": {"rows": [{"project": "open-yy"}]}},
                 "projects": [{"slug": "secret-zz", "name": "secret-zz", "sensitive": False, "drive": True}]}
        with mock.patch.object(live.c, "all_projects", return_value=[(hidden, {"sensitive": True}), (open_, {})]):
            out = live._strip_hidden(frame)
            self.assertEqual([r["id"] for r in out["queue"]], ["b"])
            self.assertEqual(out["board"]["lanes"], [])
            self.assertEqual(out["projects"][0], {"slug": "secret-zz", "name": "secret-zz", "sensitive": True})
            frame["guard"] = "secret-zz"  # the slug in a field this strip does not know: the frame is not served
            self.assertIsNone(live._strip_hidden(frame))

    def test_first_frame_the_kept_file_is_0600_from_its_first_byte(self):  # T-5549: a frame holds what the page shows
        path = os.path.join(self.p.dir, "live-frame.json")
        fr = live.Frames(lambda: {"at": c.now()}, lambda: None, path)
        real, modes = os.replace, []

        def checked(src, dst):  # the temp file's mode at the moment it takes the kept file's name
            modes.append(os.stat(src).st_mode & 0o777)
            return real(src, dst)
        with mock.patch.object(live.os, "replace", side_effect=checked):
            fr._keep(b'{"at": "x"}')
        self.assertEqual(modes, [0o600], "the temp file is 0600 before it is renamed over the kept file")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(open(path, "rb").read(), b'{"at": "x"}')

    def test_all_carries_every_open_projects_asks_ships_and_gate_tagged_and_none_of_a_sensitive_one(self):  # T-3731
        op, sp = self._two_more()
        at = c.now()
        with open(os.path.join(c.state_dir(), "events.jsonl"), "a") as f:
            for slug in (self.p.slug, op.slug, sp.slug):
                f.write(json.dumps({"ts": at, "kind": "guard_block", "rule": "rm-rf", "tool": "Bash", "project": slug,
                                    "cmd": f"rm -rf {slug}"}) + "\n")
        from sampo import board as bd
        asks = lambda p: {"rows": [{"id": f"T-{h}", "title": f"ask in {p.slug}", "stage": "asked", "age_h": h}
                                   for h in reversed(range(bd.ASKS_MAX))], "days": 7, "more": 0}
        ship = lambda p: {"sha": "abc1234", "subject": f"ship {p.slug}", "age_s": 5, "ref": "shipped", "unshipped": 0,
                          "ci": {"state": "local", "runs": [], "jobs": [], "failed": []}}
        with mock.patch("sampo.board.part_asks", asks), mock.patch("sampo.board.part_ship", ship), \
                mock.patch.dict(bd.PARTS, {"asks": asks, "ship": ship}):
            s = live.snapshot_all(self.p)
        b = s["board"]
        self.assertEqual({r["project"] for r in b["asks"]["rows"]}, {self.p.slug, op.slug})
        self.assertEqual(b["asks"]["days"], 7)
        ages = [r["age_h"] for r in b["asks"]["rows"]]  # review: the newest kept across projects, drawn oldest first
        self.assertEqual((len(ages), b["asks"]["more"]), (bd.ASKS_MAX, bd.ASKS_MAX))
        self.assertEqual(ages, sorted(ages, reverse=True))
        self.assertLess(max(ages), bd.ASKS_MAX // 2 + 1)
        self.assertEqual(set(b["guard"]["by"]), {self.p.slug, op.slug}, "each project's own gate counts for the chips")
        self.assertEqual({x["project"] for x in b["ships"]}, {self.p.slug, op.slug})
        self.assertEqual(b["guard"]["refused"], 2)
        self.assertEqual({r["project"] for r in b["guard"]["recent"]}, {self.p.slug, op.slug})
        self.assertTrue({"sessions", "usage"} <= set(b), "the machine-wide pictures stay")
        self.assertNotIn(sp.slug, json.dumps([b["asks"], b["ships"], b["guard"]]))

    def test_no_slug_with_a_sensitive_newest_session_falls_back_to_the_servers_own(self):  # T-3024 review
        self.login()
        other = os.path.join(self.tmp, "hush2")
        os.makedirs(other)
        sp = c.init_project(other, sensitive=True)
        with mock.patch("sampo.bus.live", return_value=[{"session_id": "s1", "project": sp.slug}]):
            st, body, _ = self.req("/api/state")
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(body)["project"]["slug"], self.p.slug)

    def test_a_spent_token_is_replaced_and_the_url_file_follows(self):
        self.login()
        deadline = time.monotonic() + 5
        while self.server.token is None and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIsNotNone(self.server.token)  # a fresh one, so `sampo watch --web --url` always prints a URL that works
        self.assertNotEqual(self.server.token, self.token)


class PeerUid(ForemanTestCase):  # T-3023: /proc/net/tcp as a fake root
    HEAD = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"

    def root(self, tcp="", tcp6=None):
        d = os.path.join(self.tmp, "proc")
        os.makedirs(os.path.join(d, "net"), exist_ok=True)
        for name, rows in (("tcp", tcp), ("tcp6", tcp6)):
            if rows is not None:
                with open(os.path.join(d, "net", name), "w") as f:
                    f.write(self.HEAD + rows)
        return d

    def row(self, la, lp, ra, rp, uid):
        return f"   0: {la}:{lp:04X} {ra}:{rp:04X} 01 00000000:00000000 00:00000000 00000000  {uid}        0 12345 1 0 20 4 30 10 -1\n"

    def test_ipv4_ipv6_and_mapped_rows_the_client_side_socket_gives_the_uid(self):
        v4 = self.row("0100007F", 40000, "0100007F", 8000, 1000) + self.row("0100007F", 8000, "0100007F", 40000, 0)
        self.assertEqual(live.peer_uid("127.0.0.1", 40000, 8000, self.root(v4)), 1000)  # not the server-side row (uid 0)
        self.assertEqual(live.peer_uid("127.0.0.1", 40001, 8000, self.root(v4, "")), None)
        m = self.row("0000000000000000FFFF00000100007F", 41000, "0000000000000000FFFF00000100007F", 8000, 1234)
        self.assertEqual(live.peer_uid("127.0.0.1", 41000, 8000, self.root("", m)), 1234)
        six = self.row("00000000000000000000000001000000", 42000, "00000000000000000000000001000000", 8000, 77)
        self.assertEqual(live.peer_uid("::1", 42000, 8000, self.root("", six), saddr="::1"), 77)

    def test_another_uid_is_reported_and_unparseable_or_missing_rows_are_none(self):
        other = self.row("0100007F", 40000, "0100007F", 8000, 4242)
        self.assertEqual(live.peer_uid("127.0.0.1", 40000, 8000, self.root(other)), 4242)
        junk = "garbage\n   0: zz:yy 1:2 01\n   1: 0100007F:9C40 0100007F:1F40 01 a b c d notauid 0 1\n"
        self.assertIsNone(live.peer_uid("127.0.0.1", 40000, 8000, self.root(junk)))
        self.assertIsNone(live.peer_uid("127.0.0.1", 40000, 8000, os.path.join(self.tmp, "nowhere")))
        foreign = self.row("0A00000A", 40000, "0A00000A", 8000, 5)  # not a loopback address
        self.assertIsNone(live.peer_uid("127.0.0.1", 40000, 8000, self.root(foreign)))

    def test_another_address_with_the_users_port_number_never_inherits_the_users_uid(self):  # T-3027 review: HIGH
        user = self.row("0100007F", 40000, "0100007F", 8000, 1000)
        attacker = self.row("0200007F", 40000, "0100007F", 8000, 4242)  # 127.0.0.2:40000 -> the server
        self.assertEqual(live.peer_uid("127.0.0.2", 40000, 8000, self.root(user + attacker)), None)
        self.assertEqual(live.peer_uid("127.0.0.1", 40000, 8000, self.root(user + attacker)), 1000)
        self.assertIsNone(live.peer_uid("127.0.0.1", 40000, 8000, self.root(user + user)), "two rows: ambiguous")
        to_other = self.row("0100007F", 40000, "0100007F", 9000, 1000)  # the user's socket to another server
        self.assertIsNone(live.peer_uid("127.0.0.1", 40000, 8000, self.root(to_other)))


class Url(ForemanTestCase):
    def test_url_reprints_what_the_server_wrote(self):
        self.fm("task", "new", "x", "--type", "FEATURE", "--tier", "S", "--ac", "x :: true", "--step", "s", "--focus")
        p = c.find_project(self.repo)
        self.assertNotEqual(self.fm("watch", "--web", "--url", check=False).returncode, 0)  # none running
        live.write_url(p, "http://127.0.0.1:1/?t=abc")
        out = self.fm("watch", "--web", "--url")
        self.assertEqual(out.stdout.strip(), "http://127.0.0.1:1/?t=abc")
        self.assertEqual(os.stat(os.path.join(p.dir, "live.url")).st_mode & 0o777, 0o600)


class Act(ForemanTestCase):  # T-3026: POST /api/act, with the same-uid peer the browser is: only the refusals stand between it and a verb
    def setUp(self):
        super().setUp()
        self.fm("task", "new", "Steer it", "--type", "FEATURE", "--tier", "S", "--ac", "x :: true", "--step", "s", "--focus")
        live._CACHE.clear()
        self.p = c.find_project(self.repo)
        peer = mock.patch.object(live, "peer_uid", return_value=os.getuid())
        peer.start()
        self.addCleanup(peer.stop)
        self.server, _ = live.make(self.p)
        self.port = self.server.server_address[1]
        self.stop = threading.Event()
        self.thread = threading.Thread(target=live.serve, args=(self.server, self.stop), daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 5)
        self.addCleanup(self.stop.set)
        self.nonce = self.page_nonce()
        self.origin = f"http://127.0.0.1:{self.port}"

    def call(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, body=body, headers={"Host": f"127.0.0.1:{self.port}", **(headers or {})})
        r = conn.getresponse()
        out = r.status, r.read().decode()
        conn.close()
        return out

    def page_nonce(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/", headers={"Host": f"127.0.0.1:{self.port}"})
        html = conn.getresponse().read().decode()
        conn.close()
        return html.split('<meta name="sampo-act" content="')[1].split('"')[0]  # the act token, in the page's HTML

    def act(self, body, **over):
        h = {"Content-Type": "application/json", "Origin": self.origin, "X-Sampo-Nonce": self.nonce, **over}
        h = {k: v for k, v in h.items() if v is not None}
        return self.call("POST", "/api/act", body if isinstance(body, str) else json.dumps(body), h)

    def rows(self):
        return c.ledger_events(self.p, ["live_act"])

    def test_each_action_runs_its_verb_and_is_logged(self):
        slug = self.p.slug
        cases = [({"action": "drive_on", "project": slug}, ["drive", "on", "-p", slug]),
                 ({"action": "drive_off", "project": slug}, ["drive", "off", "-p", slug]),
                 ({"action": "checkpoint", "project": slug}, ["checkpoint", "-p", slug]),
                 ({"action": "resume", "project": slug}, ["resume", "-p", slug]),
                 ({"action": "pause_now", "confirmed": True}, ["pause", "on", "--now"]),
                 ({"action": "pause_off"}, ["pause", "off"])]
        for body, argv in cases:
            with mock.patch.object(live, "_run", return_value=0) as run:
                st, out = self.act(body)
            self.assertEqual((st, json.loads(out)["exit"]), (200, 0), body)
            run.assert_called_once_with(argv, self.p)
        self.assertEqual([r["data"]["action"] for r in self.rows()], [b["action"] for b, _ in cases])
        self.assertEqual(self.rows()[0]["data"], {"action": "drive_on", "project": slug, "exit": 0})

    def test_a_real_drive_off_changes_the_project(self):
        with mock.patch.dict(os.environ, {"FOREMAN_HOME": self.home}):
            st, out = self.act({"action": "drive_off", "project": self.p.slug})
        self.assertEqual((st, json.loads(out)["exit"]), (200, 0), out)
        self.assertIs(c.read_meta(self.p).get("drive"), False)

    def test_a_failing_verb_is_reported_and_logged(self):
        with mock.patch.object(live, "_run", return_value=3):
            st, out = self.act({"action": "resume", "project": self.p.slug})
        self.assertEqual((st, json.loads(out)["exit"]), (200, 3))
        self.assertEqual(self.rows()[0]["data"]["exit"], 3)

    def test_pause_now_needs_confirmed_true(self):
        with mock.patch.object(live, "_run") as run:
            for body in ({"action": "pause_now"}, {"action": "pause_now", "confirmed": "yes"}, {"action": "pause_now", "confirmed": False}):
                self.assertEqual(self.act(body)[0], 400)
        run.assert_not_called()

    def test_a_note_goes_through_the_bus_and_the_ledger_keeps_only_its_length(self):
        live_ = [{"session_id": "abcdef0123456789", "project": self.p.slug}]
        with mock.patch("sampo.bus.live", return_value=live_), mock.patch("sampo.bus.send") as send:
            st, _ = self.act({"action": "note", "session": "abcdef01", "text": "hello there"})
        self.assertEqual(st, 200)
        send.assert_called_once_with("abcdef0123456789", "hello there", kind="note", sender="live")
        d = self.rows()[0]["data"]
        self.assertEqual((d["action"], d["len"], d["session"]), ("note", 11, "abcdef01"))
        self.assertNotIn("hello", json.dumps(self.rows()))

    def test_a_note_to_a_session_with_no_project_is_refused(self):
        with mock.patch("sampo.bus.live", return_value=[{"session_id": "abcdef0123456789"}]), mock.patch("sampo.bus.send") as send:
            self.assertEqual(self.act({"action": "note", "session": "abcdef01", "text": "x"})[0], 400)
        send.assert_not_called()

    def test_a_board_layout_is_saved_to_its_scope_logged_and_in_the_next_snapshot(self):  # T-3739
        from sampo import board
        lay = {"order": ["tests", "lanes"], "panels": {"lanes": {"span": 2, "height": 3}, "cpu": {"hidden": True}},
               "style": "flat", "motion": "calm"}
        with mock.patch.object(live, "_run") as run:
            st, out = self.act({"action": "board_layout", "scope": "project", "project": self.p.slug, "layout": lay})
            self.assertEqual((st, json.loads(out)["ok"]), (200, True), out)
            got = json.loads(self.call("GET", "/api/state")[1])["board"]["layout"]
            self.assertEqual((got["order"][:2], got["panels"]["lanes"]["span"], got["panels"]["cpu"]["hidden"]),
                             (["tests", "lanes"], 2, True), "the next snapshot has it, not a cached one")
            self.assertEqual(self.act({"action": "board_layout", "scope": "global", "layout": {"style": "flat"}})[0], 200)
            self.assertEqual(board.layout_view(None)["style"], "flat")
            self.assertEqual(self.act({"action": "board_layout_reset", "scope": "project", "project": self.p.slug})[0], 200)
        run.assert_not_called()
        self.assertEqual(board.layout_view(self.p)["from"], "global")
        rows = [r["data"] for r in self.rows()]
        self.assertEqual(rows[0], {"action": "board_layout", "scope": "project", "project": self.p.slug, "exit": 0})
        self.assertEqual([r["action"] for r in rows], ["board_layout", "board_layout", "board_layout_reset"])
        self.assertNotIn("tests", json.dumps(rows), "the ledger keeps the scope, never the layout")

    def test_a_bad_board_layout_is_a_400_naming_the_field_and_writes_nothing(self):  # T-3739
        from sampo import board
        slug = self.p.slug
        bad = [({"action": "board_layout", "scope": "project", "project": slug, "layout": {"panels": {"lanes": {"span": 4}}}},
                "panels.lanes.span"),
               ({"action": "board_layout", "scope": "project", "project": slug, "layout": "x"}, "layout"),
               ({"action": "board_layout", "scope": "project", "project": slug}, "layout"),
               ({"action": "board_layout", "scope": "room", "layout": {}}, "scope"),
               ({"action": "board_layout", "scope": "project", "layout": {}}, "unknown project"),
               ({"action": "board_layout", "scope": "project", "project": "all", "layout": {}}, "unknown project"),
               ({"action": "board_layout_reset", "scope": "project", "project": "nope"}, "unknown project")]
        for body, field in bad:
            st, out = self.act(body)
            self.assertEqual(st, 400, body)
            self.assertIn(field, json.loads(out)["error"], body)
        self.assertEqual(self.rows(), [])
        self.assertFalse(os.path.exists(board.layout_path("project", self.p)))
        self.assertFalse(os.path.exists(board.layout_path("global", None)))

    def test_unknown_things_are_a_400_and_run_nothing(self):
        os.makedirs(os.path.join(self.tmp, "hush"))
        sp = c.init_project(os.path.join(self.tmp, "hush"), sensitive=True)
        bad = [{"action": "rm_rf"}, {"action": 5}, {}, [1], {"action": "drive_on"}, {"action": "drive_on", "project": "nope"},
               {"action": "drive_on", "project": sp.slug}, {"action": "note", "session": "zzz", "text": "x"},
               {"action": "note", "session": "abcdef01", "text": "x"},  # a sensitive project's session
               {"action": "note", "session": "abcdef01", "text": ""}, {"action": "note", "session": "abcdef01", "text": "x" * 2500}]
        with mock.patch.object(live, "_run") as run, mock.patch("sampo.bus.send") as send, \
                mock.patch("sampo.bus.live", return_value=[{"session_id": "abcdef0123456789", "project": sp.slug}]):
            for b in bad:
                self.assertIn(self.act(b)[0], (400, 404), b)
            self.assertEqual(self.act("{not json")[0], 400)
        run.assert_not_called()
        send.assert_not_called()
        self.assertEqual(self.rows(), [])

    def test_every_refusal_runs_nothing_even_from_the_servers_own_uid(self):
        ok = {"action": "drive_off", "project": self.p.slug}
        form = {"Content-Type": "application/x-www-form-urlencoded"}
        with mock.patch.object(live, "_run") as run:
            self.assertEqual(self.call("GET", "/api/act")[0], 405)
            self.assertEqual(self.call("PUT", "/api/act", json.dumps(ok), {"Content-Type": "application/json"})[0], 405)
            for over in (form, {"Content-Type": "text/plain"}, {"Content-Type": None},
                         {"Origin": None}, {"Origin": "http://evil.example"}, {"Origin": "null"},
                         {"Origin": f"http://127.0.0.1:{self.port + 1}"}, {"Origin": f"https://127.0.0.1:{self.port}"},
                         {"X-Sampo-Nonce": None}, {"X-Sampo-Nonce": "guess"}):
                self.assertEqual(self.act(ok, **over)[0], 403, over)
            self.assertEqual(self.act("action=drive_off&project=" + self.p.slug, **form)[0], 403)
        run.assert_not_called()
        self.assertEqual(self.rows(), [])
        with mock.patch.object(live, "_run", return_value=0):  # the same request, whole: accepted (the refusals are the only gate)
            self.assertEqual(self.act(ok, Origin=f"http://localhost:{self.port}")[0], 200)
            self.assertEqual(self.act(ok, **{"Content-Type": "application/json; charset=utf-8"})[0], 200)

    def test_each_act_refuses_an_attacker_and_runs_for_the_user(self):  # T-3063: one attack list, every act
        slug = self.p.slug
        acts = [{"action": "drive_on", "project": slug}, {"action": "drive_off", "project": slug},
                {"action": "checkpoint", "project": slug}, {"action": "pause_now", "confirmed": True},
                {"action": "pause_off"}, {"action": "note", "session": "abcdef01", "text": "hi"},
                {"action": "board_layout", "scope": "project", "project": slug, "layout": {"style": "flat"}},  # T-3739
                {"action": "board_layout_reset", "scope": "global"}]
        port, stale = self.port, self.nonce
        self.server.session = "rotated-secret"  # the secret the page's token came from is gone: that token is stale
        fresh = self.page_nonce()
        self.assertNotEqual(fresh, stale)
        attacks = {"no token": {"X-Sampo-Nonce": None}, "a stale token": {"X-Sampo-Nonce": stale},
                   "another site's Origin": {"Origin": "http://evil.example"},
                   "a forwarded Host with its own Origin": {"Host": "evil.example", "Origin": "http://evil.example"},
                   "a forwarded Host, right Origin": {"Host": "evil.example"},
                   "a forwarded port": {"Host": f"localhost:{port + 1}", "Origin": f"http://localhost:{port + 1}"},
                   "a forwarded port with no port": {"Host": "localhost", "Origin": "http://localhost"},
                   "a proxy's X-Forwarded-For": {"X-Forwarded-For": "203.0.113.9"},
                   "a proxy's X-Forwarded-Host": {"X-Forwarded-Host": "evil.example"},
                   "a proxy's Forwarded": {"Forwarded": "for=203.0.113.9"}}
        live_ = [{"session_id": "abcdef0123456789", "project": slug}]
        with mock.patch.object(live, "_run", return_value=0) as run, mock.patch("sampo.bus.send") as send, \
                mock.patch("sampo.bus.live", return_value=live_):
            for body in acts:
                for why, over in attacks.items():
                    over = {"X-Sampo-Nonce": fresh, **over}  # the attack's one wrong header; the rest is the user's request
                    self.assertEqual(self.act(body, **over)[0], 403, f"{body['action']}: {why}")
            run.assert_not_called()
            send.assert_not_called()
            self.assertEqual(self.rows(), [])
            from sampo import board
            self.assertFalse(os.path.exists(board.layout_path("project", self.p)), "a refused layout writes nothing")
            for body in acts:
                self.assertEqual(self.act(body, **{"X-Sampo-Nonce": fresh})[0], 200, body["action"])
        self.assertEqual(run.call_count, 5)
        send.assert_called_once()

    def test_page_loads_cannot_evict_the_token(self):  # a cross-site page can GET / as often as it likes
        with mock.patch.object(live, "_run", return_value=0):
            for _ in range(100):
                self.assertEqual(self.page_nonce(), self.nonce)
            self.assertEqual(self.act({"action": "pause_off"})[0], 200)

    def test_a_cross_site_fetch_is_refused_by_sec_fetch_site(self):
        with mock.patch.object(live, "_run", return_value=0) as run:
            for v in ("cross-site", "same-site", "none", ""):
                self.assertEqual(self.act({"action": "pause_off"}, **{"Sec-Fetch-Site": v})[0], 403, v)
            run.assert_not_called()
            self.assertEqual(self.act({"action": "pause_off"}, **{"Sec-Fetch-Site": "same-origin"})[0], 200)

    def test_deeply_nested_json_is_a_400(self):
        with mock.patch.object(live, "_run") as run:
            self.assertEqual(self.act("[" * 2000 + "]" * 2000)[0], 400)
        run.assert_not_called()

    def test_a_timeout_kills_the_whole_process_group(self):
        child = mock.Mock(pid=4242)
        child.wait.side_effect = [live.subprocess.TimeoutExpired("x", 1), 0]
        with mock.patch.object(live.subprocess, "Popen", return_value=child) as popen, mock.patch.object(live.os, "killpg") as kill:
            self.assertEqual(live._run(["resume", "-p", "x"], self.p), 124)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        kill.assert_called_once_with(4242, live.signal.SIGKILL)
        self.assertEqual(child.wait.call_args_list[0].kwargs["timeout"], live.SLOW_ACT_TIMEOUT)

    def test_a_body_over_the_cap_is_refused(self):
        with mock.patch.object(live, "_run") as run:
            self.assertEqual(self.act(json.dumps({"action": "note", "text": "x" * 20000}))[0], 413)
        run.assert_not_called()


class IdleCost(ForemanTestCase):  # T-6013: an idle sampo watch --web costs next to nothing; a change still reaches the page
    def setUp(self):
        super().setUp()
        self.fm("task", "new", "Idle it", "--type", "FEATURE", "--tier", "S", "--ac", "x :: true", "--step", "s", "--focus")
        self.p = c.find_project(self.repo)
        live._CACHE.clear()
        live._BRIEFS.clear()

    def _touch(self, *parts):
        path = os.path.join(self.p.dir, *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write("x\n")

    def test_the_stamp_ignores_what_the_servers_own_build_writes(self):  # the build wrote cache/ and a hook writes lastcmd/
        self._touch("cache", "board.json")
        self._touch("lastcmd", "s1")
        self._touch(".lock")
        before = live._stamp(self.p)
        self._touch("cache", "board.json")
        self._touch("cache", "git.json")
        self._touch("lastcmd", "s2")
        self._touch(".lock")
        self.assertEqual(live._stamp(self.p), before, "a write into cache/, lastcmd/ or .lock is no input of the frame: it woke a rebuild")
        self._touch("ledger.jsonl")
        self.assertNotEqual(live._stamp(self.p), before, "a ledger write still moves the stamp")

    def _counted(self, names):
        n = dict.fromkeys(names, 0)
        wrapped = {}
        for name in names:
            def one(p, name=name, real=live.COLLECTORS[name]):
                n[name] += 1
                return real(p)
            wrapped[name] = one
        return n, mock.patch.dict(live.COLLECTORS, wrapped)

    def _parses(self):
        n, real = {"n": 0}, c.load_briefs

        def counted(*a, **k):
            n["n"] += 1
            return real(*a, **k)
        return n, mock.patch.object(c, "load_briefs", counted)

    def test_the_briefs_are_parsed_again_only_when_a_brief_file_moved(self):
        n, patch = self._parses()
        with patch:
            live._build_one(self.p)
            live._build_one(self.p)
            live._build_one(self.p)
            self.assertEqual(n["n"], 1, "no brief file moved: the three builds share one parse")
            self._touch("tasks", "T-0099-new.md")  # a brief appears: the folder's listing moves
            live._build_one(self.p)
            self.assertEqual(n["n"], 2)

    def test_every_collector_runs_on_every_build(self):  # T-6013 review: nothing but the parse is kept
        n, patch = self._counted(["lanes", "queue", "focus", "closed_today", "project"])
        with patch:
            live._build_one(self.p)
            live._build_one(self.p)
        self.assertEqual(n, {"lanes": 2, "queue": 2, "focus": 2, "closed_today": 2, "project": 2})

    def test_the_stage_follows_the_autonomy_at_every_build_not_the_one_the_briefs_were_read_under(self):
        got = []
        auto = {"v": "standard"}
        with mock.patch.object(c, "effective_autonomy", lambda *a, **k: auto["v"]), \
                mock.patch.object(c, "stage", lambda b, a="standard", *r, **k: got.append(a) or "x"):
            live._build_one(self.p)
            auto["v"] = "slow"  # the risk budget, a pause, the plan record: none is a brief file
            got.clear()
            live._build_one(self.p)
        self.assertTrue(got and set(got) == {"slow"}, f"the stage was computed under {set(got)}")

    def test_a_lane_that_finished_shows_waiting_on_the_next_build_with_no_brief_file_moved(self):
        b = live._briefs(self.p)[0]
        b.meta["lane"] = self.repo  # the parse is the kept one: give its brief a lane
        finished = {"ids": []}
        with mock.patch.object(c, "finished_lanes", lambda *a, **k: finished["ids"]):
            self.assertFalse(live.lanes(self.p)[0]["waiting"])
            finished["ids"] = [b.id]  # git or the builder's process moved: no brief file did
            self.assertTrue(live.lanes(self.p)[0]["waiting"])

    def test_the_kept_briefs_expire_before_the_frame_ceiling(self):  # an edit that keeps a file's mtime and size is seen by then
        self.assertLess(live.BRIEFS_S, live.FRAME_MAX_S)
        n, patch = self._parses()
        with patch, mock.patch.object(live, "BRIEFS_S", 0):
            live._build_one(self.p)
            live._build_one(self.p)
        self.assertGreater(n["n"], 1, "a keep of no time parses again (once per use), not once")

    def test_a_collector_that_raised_leaves_its_field_empty_and_the_next_build_runs_it(self):
        real = live.COLLECTORS["lanes"]
        calls = {"n": 0}

        def flaky(p):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return real(p)
        with mock.patch.dict(live.COLLECTORS, {"lanes": flaky}):
            self.assertEqual(live._build_one(self.p)["lanes"], [])
            live._build_one(self.p)
        self.assertEqual(calls["n"], 2)

    def test_lanes_reads_the_autonomy_once_not_once_per_lane(self):
        briefs = [mock.Mock(id=f"T-{i:04d}", status="active", meta={"lane": "x"}, title="t", steps=lambda: []) for i in range(6)]
        seen = {"n": 0}
        real = c.effective_autonomy

        def counted(*a, **k):
            seen["n"] += 1
            return real(*a, **k)
        with mock.patch.object(live, "_briefs", return_value=briefs), mock.patch.object(c, "finished_lanes", return_value=[]), \
                mock.patch.object(c, "effective_autonomy", counted), mock.patch.object(c, "stage", return_value="build"):
            self.assertEqual(len(live.lanes(self.p)), 6)
        self.assertLessEqual(seen["n"], 1)

    def _frames(self):
        n = {"builds": 0}

        def build():
            n["builds"] += 1
            return {"at": c.now(), "n": n["builds"]}
        return n, live.Frames(build, lambda: live._stamp(self.p), os.path.join(self.p.dir, "live-frame-t.json"))

    def test_no_stream_no_build_and_no_wake(self):
        n, fr = self._frames()
        with mock.patch.object(live, "STREAM_S", 0.05):
            fr.subscribe()
            for _ in range(100):
                if n["builds"]:
                    break
                time.sleep(0.02)
            fr.unsubscribe()
            time.sleep(0.4)  # the thread may be mid-wait: let the last tick end
            seen = n["builds"]
            for _ in range(10):
                self._touch("ledger.jsonl")  # the state moves and nobody watches: nothing runs
                time.sleep(0.1)
        fr.close()
        self.assertEqual(n["builds"], seen, "with no stream open the frame thread builds nothing")
        self.assertGreaterEqual(seen, 1)

    def test_a_state_change_reaches_a_connected_stream_within_a_tick(self):
        n, fr = self._frames()
        with mock.patch.object(live, "STREAM_S", 0.1):
            fr.subscribe()
            f, _ = fr.take(0)
            self.assertIsNotNone(f)
            self._touch("ledger.jsonl")
            t0, seen, got = time.monotonic(), f.seq, None
            while time.monotonic() - t0 < 3 and not got:
                nf, _ = fr.take(seen)
                if nf is not None and nf.s["n"] > f.s["n"]:
                    got = nf
                seen = nf.seq if nf is not None else seen
            fr.unsubscribe()
            fr.close()
        self.assertIsNotNone(got, "a ledger write is rebuilt and pushed")
        self.assertLess(time.monotonic() - t0, 1.0)

    def test_an_idle_stream_costs_under_five_percent_of_a_core(self):
        n, fr = self._frames()
        with mock.patch.object(live, "STREAM_S", 0.1):
            fr.subscribe()
            f, _ = fr.take(0)
            seen = f.seq
            t0, w0 = time.process_time(), time.monotonic()
            while time.monotonic() - w0 < 3:
                nf, _ = fr.take(seen)
                seen = nf.seq if nf is not None else seen
            cpu, wall = time.process_time() - t0, time.monotonic() - w0
            fr.unsubscribe()
            fr.close()
        self.assertEqual(n["builds"], 1, "nothing changed: one build, then the held frame again")
        self.assertLess(cpu / wall, 0.05, f"{cpu:.3f} s of cpu over {wall:.1f} s idle")
