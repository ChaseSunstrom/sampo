"""fm ui --json: the view model the foreman-ui mod renders (mods/foreman-ui/types/index.d.ts is its TS twin), and
fm task show --json carrying steps, criteria and depends (T-0076)."""
import json
import os

from helpers import ForemanTestCase


class UiSkills(ForemanTestCase):
    """T-0939: the pane's Skills card: Skill tool calls and fm lib card reads, this week, and the unread suggestions."""

    def setUp(self):
        super().setUp()
        self.fm("init")
        from sampo import core as c
        self.c, self.p = c, c.find_project(self.repo)

    def skills(self):
        return json.loads(self.fm("ui", "--json").stdout)["skills"]

    def test_empty_without_any_use_or_map(self):
        self.assertEqual(self.skills(), {"recent": [], "counts": {}, "suggested": [], "unread": []})

    def test_skill_calls_and_card_reads_this_week(self):
        c, p, slug = self.c, self.p, self.p.slug
        old = c.time.strftime("%Y-%m-%dT%H:%M:%SZ", c.time.gmtime(c.time.time() - 8 * 86400))
        at = [c.time.strftime("%Y-%m-%dT%H:%M:%SZ", c.time.gmtime(c.time.time() - 60 * n)) for n in range(5, 0, -1)]
        rows = [{"ts": old, "kind": "tool", "tool": "Skill", "target": "sampo:old", "project": slug},
                {"ts": at[0], "kind": "tool", "tool": "Skill", "target": "sampo:brainstorm", "project": slug},
                {"ts": at[1], "kind": "tool", "tool": "Skill", "target": "sampo:brainstorm", "project": slug},
                {"ts": at[1], "kind": "tool", "tool": "Skill", "target": "elsewhere:x", "project": "other-1"},
                {"ts": at[2], "kind": "tool", "tool": "Bash", "target": "ls", "project": slug},
                {"kind": "tool", "tool": "Skill", "target": "no:ts", "project": slug},
                {"ts": at[3], "kind": "tool_fail", "tool": "Skill", "target": "sampo:broken", "project": slug}]
        os.makedirs(c.state_dir(), exist_ok=True)
        with open(os.path.join(c.state_dir(), "events.jsonl"), "w") as f:
            f.writelines(json.dumps(r) + "\n" for r in rows)
            f.write("{not json\n")
        with open(os.path.join(p.dir, "ledger.jsonl"), "a") as f:
            f.write(json.dumps({"ts": old, "event": "lib", "data": {"cards": ["bash"]}}) + "\n")
            f.write(json.dumps({"ts": at[2], "event": "lib", "data": {"cards": ["python", "bash"]}}) + "\n")
            f.write(json.dumps({"event": "lib", "data": {"cards": ["nots"]}}) + "\n")
            f.write(json.dumps({"ts": at[4], "event": "lib", "data": {"cards": "python"}}) + "\n")  # malformed
        with open(os.path.join(p.dir, "map.json"), "w") as f:
            json.dump({"lib": ["python", "typescript", "bash", "semver"]}, f)
        s = self.skills()
        self.assertEqual([(x["name"], x["kind"]) for x in s["recent"]],
                         [("python", "card"), ("bash", "card"), ("sampo:brainstorm", "skill")])
        self.assertEqual(s["recent"][2]["at"], at[1], "a name shows once, at its newest use")
        self.assertEqual(s["counts"], {"sampo:brainstorm": 2, "python": 1, "bash": 1})
        self.assertEqual(s["suggested"], ["python", "typescript", "bash", "semver"])
        self.assertEqual(s["unread"], ["typescript", "semver"])

    def test_a_call_from_days_ago_shows_behind_a_busy_events_file(self):
        # a tail of events.jsonl holds an hour or two on a busy day; the week is the whole file
        c, slug = self.c, self.p.slug
        at = c.time.strftime("%Y-%m-%dT%H:%M:%SZ", c.time.gmtime(c.time.time() - 3 * 86400))
        filler = json.dumps({"ts": at, "kind": "tool", "tool": "Read", "target": "x" * 200, "project": slug}) + "\n"
        os.makedirs(c.state_dir(), exist_ok=True)
        with open(os.path.join(c.state_dir(), "events.jsonl"), "w") as f:
            f.write(json.dumps({"ts": at, "kind": "tool", "tool": "Skill", "target": "sampo:intake", "project": slug}) + "\n")
            f.write(filler * 2000)  # ~500 KB
        self.assertEqual(self.skills()["counts"], {"sampo:intake": 1})

    def test_recent_holds_eight_and_a_broken_map_is_no_map(self):
        c, p = self.c, self.p
        for i in range(12):
            c.log_event(p, "lib", data={"cards": [f"card-{i}"]})
        with open(os.path.join(p.dir, "map.json"), "w") as f:
            f.write("{broken")
        s = self.skills()
        self.assertEqual(len(s["recent"]), 8)
        self.assertEqual(s["recent"][0]["name"], "card-11")
        self.assertEqual(len(s["counts"]), 12)
        self.assertEqual((s["suggested"], s["unread"]), ([], []))


class UiView(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.fm("task", "new", "Login times out", "--type", "FIX", "--tier", "S", "--ac", "slow wifi logs in :: true",
                "--step", "red test", "--step", "raise the timeout", "--focus")
        self.fm("task", "evidence", "T-0001", "--step", "1", "--run", "true")
        self.fm("task", "step", "T-0001", "done", "1")
        self.fm("task", "new", "CSV export", "--type", "FEATURE", "--tier", "L", "--interpretation", "Reading 1: a. Reading 2: b.",
                "--approach", "y", "--ac", "csv opens :: true", "--step", "build", "--depends", "T-0001")
        self.fm("capture", "Merge the date helpers")

    def test_unproven_count_and_top_kind_ride_in_the_view_model(self):  # T-2395: the pane's band line reads it
        from sampo import core as c
        self.assertEqual(self.fm_json("ui")["unproven"]["n"], 0)
        p = c.find_project(self.repo)
        self.fm("task", "evidence", "T-0002", "--step", "1", "pytest", "9 passed")  # typed: the rollup holds it once done
        b = c.find_brief(p, "T-0002")
        b.meta["status"] = "done"
        c.save_brief(p, b)
        got = self.fm_json("ui")["unproven"]
        self.assertEqual((got["n"], got["top"]), (1, "typed"))

    def test_no_string_holds_a_control_character(self):
        """T-1674: Claude Code refuses the whole pane when a text child holds a control character."""
        import unicodedata
        import glob
        self.fm("capture", "Evil title")
        self.fm("task", "step", "T-0001", "add", "step with clear")
        for path, old, new in ((glob.glob(os.path.join(self.home, "state", "projects", "*", "tasks", "T-0004-*.md"))[0],
                                "# Evil title", "# Evil \x1b[31mred\x1b]0;pwn\x07 title‮️"),
                               (glob.glob(os.path.join(self.home, "state", "projects", "*", "tasks", "T-0001-*.md"))[0],
                                "step with clear", "step with \x1b[2J\x08 clear")):  # a brief edited by hand or synced
            with open(path, encoding="utf-8") as f:
                text = f.read()
            self.assertIn(old, text)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text.replace(old, new))
        v = json.loads(self.fm("ui", "--json").stdout)

        def strings(x, key=None):
            if isinstance(x, dict):
                for k, y in x.items():
                    yield from strings(y, k)
            elif isinstance(x, list):
                for y in x:
                    yield from strings(y, key)
            elif isinstance(x, str) and key != "watch":
                yield x
        bad = [s for s in strings(v) if any(unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp") and ch != "\n"
                                            or "︀" <= ch <= "️" for ch in s)]
        self.assertEqual(bad, [])
        self.assertIn("title", [x for x in v["inbox"] if x["id"] == "T-0004"][0]["title"])  # the text is kept

    def test_every_string_of_the_view_is_shown_plain_but_paths(self):
        from sampo import watch
        raw = {"recent": ["a \x1b[31mred\x1b[0m b\x07", "x‮y"], "signals": [{"text": "\x1b]0;pwn\x07ok\nnext"}],
               "watch": ["/tmp/a\x1bb"], "trust_file": "/t\x07", "n": 3, "none": None}
        got = watch._shown(raw)
        self.assertEqual(got["recent"], ["a red b", "xy"])
        self.assertEqual(got["signals"], [{"text": "ok\nnext"}])
        self.assertEqual((got["watch"], got["trust_file"], got["n"], got["none"]), (["/tmp/a\x1bb"], "/t\x07", 3, None))

    def test_view_model_contract(self):
        v = json.loads(self.fm("ui", "--json").stdout)
        self.assertEqual(v["v"], 1)
        self.assertTrue(v["project"])
        self.assertEqual(v["mode"], {"autonomy": "standard", "drive": True, "sensitive": False, "trust": False, "risk_off": None,
                                     "standing": []})
        a = v["active"]
        self.assertEqual((a["id"], a["type"], a["tier"], a["stage"]), ("T-0001", "FIX", "S", "executing"))
        self.assertIn("executing", a["stages"])
        self.assertEqual([(s["n"], s["done"], s["current"]) for s in a["steps"]], [(1, True, False), (2, False, True)])
        self.assertEqual(a["criteria"], [{"n": 1, "text": "slow wifi logs in", "verify": "true", "checked": False}])
        self.assertEqual(set(a["audits"]), {"done", "need"})
        self.assertTrue(a["blockers"])
        self.assertIn("T-0001", v["next"])
        q = {x["id"]: x for x in v["queue"]}
        self.assertEqual(q["T-0002"]["waits"], "plan approval")
        self.assertNotIn("T-0001", q, "the active task isn't repeated in the queue")
        self.assertEqual([x["title"] for x in v["inbox"]], ["Merge the date helpers"])
        self.assertEqual(v["inbox_total"], 1)
        self.assertEqual(v["approvals"], [])
        self.assertIsInstance(v["recent"], list)
        self.assertEqual(set(v["skills"]), {"recent", "counts", "suggested", "unread"})  # T-0939
        self.assertEqual(set(v["health"]), {"hook_p95_ms", "guard_blocks", "hook_errors", "paused_hooks"})
        self.assertTrue(v["watch"] and all(os.path.exists(w) for w in v["watch"]))
        self.assertTrue(any(w.endswith("ledger.jsonl") for w in v["watch"]), "appends move the ledger, not the dir")
        plan = q["T-0002"]["plan"]  # what a yes approves is shown where it's given
        self.assertEqual((plan["interpretation"], plan["approach"]), ("Reading 1: a. Reading 2: b.", "y"))
        self.assertEqual([s["text"] for s in plan["steps"]], ["build"])
        self.assertEqual([a["text"] for a in plan["criteria"]], ["csv opens"])
        self.assertNotIn("plan", q.get("T-0003", {}))

    def test_the_view_carries_budget_bench_research_and_the_tasks_ledger(self):
        # T-0228: what the last sessions added, for the pane
        from sampo import budget
        from sampo import core as c
        budget.record("bench", 0.4, runs=2)
        budget.record("subagent:sampo:fm-reviewer", tokens=12000)
        self.fm("task", "hypo", "T-0001", "add", "the timeout is per request", "--probe", "grep -n timeout x.py")
        self.fm("task", "assume", "T-0001", "add", "the timeout lives in x.py")
        self.fm("task", "evidence", "T-0001", "--step", "1", "--run", "true", "--inconclusive")
        self.fm("task", "set", "T-0001", "--section", "Oracle", "--text",
                "Examples from the request alone:\n- GIVEN a WHEN b THEN c\nAmbiguities (decide each):\n- which timeout?\n")
        p = c.find_project(self.repo)
        with c.lock(p.dir):
            c.log_event(p, "research", data={"name": "ask-parser-speed", "claims": 5, "verified": 3, "not found": 1,
                                             "unchecked": 1})
            c.log_event(p, "evolve", data={"kept": False, "branch": "evolve/x", "target": "plugin/rules/foreman.md",
                                           "why": "shorter"})
        v = json.loads(self.fm("ui", "--json").stdout)
        self.assertEqual((v["budget"]["today_usd"], v["budget"]["subagent_tokens"]), (0.4, 12000))
        self.assertIn("day", v["budget"]["caps"])
        self.assertEqual(v["budget"]["top"][0]["feature"], "bench")
        self.assertEqual(v["research"][0], {"name": "ask-parser-speed", "claims": 5, "verified": 3, "not_found": 1,
                                            "unchecked": 1, "at": v["research"][0]["at"], "conflicts": 0, "single": 0})
        self.assertEqual(v["bench"]["evolve"][0]["target"], "plugin/rules/foreman.md")
        a = v["active"]
        self.assertEqual(a["hypotheses"], [{"n": 1, "status": "open",
                                            "text": "the timeout is per request — probe: `grep -n timeout x.py`"}])
        self.assertEqual(a["oracle"], {"examples": 1, "ambiguities": ["which timeout?"]})
        self.assertEqual(a["batch"], [])
        self.assertEqual((a["inconclusive"], a["unverified"]), (1, ["the timeout lives in x.py"]))
        self.assertEqual(v["revisit"], [])
        self.assertEqual(v["vetoes"], [])
        self.assertTrue(any("≈" in x and "true" in x for x in v["recent"]), v["recent"])

    def test_step_text_is_plain(self):
        self.fm("task", "step", "T-0001", "add", "bell\x07 and \x1b[31mred")
        steps = json.loads(self.fm("ui", "--json").stdout)["active"]["steps"]
        self.assertEqual(steps[-1]["text"], "bell and [31mred")

    def test_closed_tasks_are_listed_newest_first(self):
        self.fm("task", "drop", "T-0003", "not needed")
        v = json.loads(self.fm("ui", "--json").stdout)
        self.assertEqual({k: v["closed"][0][k] for k in ("id", "status")}, {"id": "T-0003", "status": "dropped"})

    def test_the_view_carries_a_structured_history_with_task_titles(self):
        # T-1026: the pane's History draws id, event and the task's title in their own colours, filling its width
        self.fm("task", "evidence", "T-0001", "--step", "2", "--run", "false", check=False)  # a red run exits 1
        h = json.loads(self.fm("ui", "--json").stdout)["history"]
        self.assertLessEqual(len(h), 60)
        last = h[-1]
        self.assertEqual((last["mark"], last["task"], last["text"], last["title"]),
                         ("✗", "T-0001", "step 2 failed", "Login times out"))
        self.assertRegex(last["time"], r"^\d\d:\d\d$")
        self.assertTrue(any(x["text"] == "step 1 passed" for x in h), h)
        self.assertTrue(any(x["text"] == "captured" and x["title"] == "Merge the date helpers" for x in h), h)
        from sampo import watch  # the newest 60 only, and a malformed time never takes the view down (review)
        many = [{"event": "task_done", "task": "T-0001", "ts": f"2026-01-01T10:{i % 60:02d}:00Z"} for i in range(70)]
        self.assertEqual(len(watch._history(many + [{"event": "task_done", "ts": 5}], {})), 60)
        self.assertIsNone(watch._recent({"event": "note", "ts": 5}))

    def test_the_view_lists_worktrees_with_their_agent_task_and_unmerged_commits(self):
        # T-1023: the pane's Agents graph joins a builder's worktree to its subagent (Claude Code names the branch
        # worktree-agent-<id>) and shows a lone or stale one
        import subprocess
        git = lambda *a, cwd=self.repo: subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *a],  # noqa: E731
                                                       cwd=cwd, check=True, capture_output=True)
        git("commit", "--allow-empty", "-qm", "base")
        tree = os.path.join(self.tmp, "wt-agent")
        git("worktree", "add", "-q", "-b", "worktree-agent-a1b2c3d4", tree)
        git("commit", "--allow-empty", "-qm", "work", cwd=tree)
        from sampo import core as c
        p = c.find_project(self.repo)
        b = c.find_brief(p, "T-0001")
        b.meta["lane"] = tree  # what fm lane new records
        c.save_brief(p, b)
        w = json.loads(self.fm("ui", "--json").stdout)["worktrees"]
        self.assertEqual([(x["branch"], x["agent"], x["task"], x["ahead"]) for x in w],
                         [("worktree-agent-a1b2c3d4", "a1b2c3d4", "T-0001", 1)])
        self.assertIsInstance(w[0]["age_s"], int)
        self.assertFalse(w[0]["stopped"])
        self.assertTrue(w[0]["open"], "T-1152: its task is still open, so 0 ahead would mean 'working', not 'merged'")
        from sampo import bus  # T-1119: a lane whose builder's process ended is flagged for the pane
        b.meta["builder_proc"] = f"{bus.host_id()}|999999999|1"
        c.save_brief(p, b)
        self.assertTrue(json.loads(self.fm("ui", "--json").stdout)["worktrees"][0]["stopped"])
        # review (T-1023): a detached worktree's merge state is unknown, not 'nothing to merge'; the newest come first
        loose = os.path.join(self.tmp, "wt-detached")
        git("worktree", "add", "-q", "--detach", loose)
        os.utime(os.path.join(tree, ".git"), (1, 1))  # made long ago
        w = json.loads(self.fm("ui", "--json").stdout)["worktrees"]
        self.assertEqual([(x["branch"], x["ahead"]) for x in w], [("", None), ("worktree-agent-a1b2c3d4", 1)])

    def test_outside_a_project_the_view_is_empty_not_an_error(self):
        elsewhere = os.path.join(self.tmp, "plain")
        os.makedirs(elsewhere)
        p = self.fm("ui", "--json", cwd=elsewhere)
        self.assertEqual(json.loads(p.stdout), {"v": 1, "project": None})
        self.assertFalse(os.path.exists(os.path.join(elsewhere, ".sampo")))
        self.assertFalse(os.path.exists(os.path.join(elsewhere, ".foreman")))

    def test_task_show_json_has_steps_criteria_and_depends(self):
        t = json.loads(self.fm("task", "show", "T-0002", "--json").stdout)
        self.assertEqual(t["steps"], [{"n": 1, "text": "build", "done": False, "current": True}])
        self.assertEqual(t["criteria"], [{"n": 1, "text": "csv opens", "verify": "true", "checked": False}])
        self.assertEqual(t["depends"], ["T-0001"])


    def test_cards_get_queue_progress_inbox_age_latency_and_the_last_check(self):
        self.fm("check", "add", "python3 -c 'print(1)'")
        self.fm("check")
        self.hook("UserPromptSubmit", {"prompt": "hello"})
        v = json.loads(self.fm("ui", "--json").stdout)
        q = {x["id"]: x for x in v["queue"]}
        self.assertEqual((q["T-0002"]["steps_done"], q["T-0002"]["steps_total"]), (0, 1))
        self.assertEqual(v["inbox"][0]["age_days"], 0)
        self.assertTrue(v["latency"] and all(isinstance(x, (int, float)) for x in v["latency"]))
        self.assertEqual([(r["cmd"], r["exit"]) for r in v["checks"]["results"]], [("python3 -c 'print(1)'", 0)])
        self.assertTrue(v["checks"]["at"])

    def test_today_done_counts_tasks_closed_today(self):
        self.fm("task", "evidence", "T-0001", "--step", "2", "--run", "true")
        self.fm("task", "step", "T-0001", "done", "2")
        self.fm("task", "set", "T-0001", "--section", "Regression test", "--text", "none: fixture")
        self.fm("task", "finish", "T-0001", "--audit", "self checklist")
        self.assertEqual(json.loads(self.fm("ui", "--json").stdout)["today_done"], 1)

    def test_the_view_carries_the_work_in_lanes_by_where_it_stands(self):
        # T-3664: the queue leaves out tasks in lanes, so the pane showed none of the 79 tasks actually moving
        from sampo import core as c
        p = c.find_project(self.repo)
        lane = os.path.join(self.tmp, "lane")
        os.makedirs(lane)
        for title, extra in (("Building", {}), ("Returned", {"builder_returned": True}),
                             ("Merged", {"lane_stop": "merged 2026-11-07T12:00:00Z"})):
            tid = self.fm("capture", title, "--type", "FIX", "--tier", "S").stdout.split()[2]
            b = c.find_brief(p, tid)
            b.meta.update(lane=lane, status="active", **extra)
            c.save_brief(p, b)
        v = json.loads(self.fm("ui", "--json").stdout)
        got = {k: [x["title"] for x in v["lanes"][k]] for k in ("building", "merging", "closing")}
        self.assertEqual(got, {"building": ["Building"], "merging": ["Returned"], "closing": ["Merged"]})
        self.assertNotIn("Building", [x["title"] for x in v["queue"]], "a lane's task is in the lanes block, not the queue")

    def test_the_view_carries_the_sampo_sessions_cpu_against_the_goal(self):
        # T-3701: the pane's CPU panel, from the samples `sampo cpu`'s self-check keeps (no new /proc or cgroup reads)
        import time
        from sampo import core as c
        from sampo import cpu
        now, cores = time.time(), cpu.ncores()
        a, b = "/sys/fs/cgroup/user.slice/x.slice/term-1.scope", "/sys/fs/cgroup/user.slice/x.slice/term-2.scope"
        usec = lambda pct, s: int(pct / 100 * cores * s * 1e6)  # noqa: E731: what pct of the machine for s seconds used
        samples = {a: [[now - 900, 0], [now - 600, usec(4, 300)], [now - 300, usec(4, 300) + usec(8, 300)]],
                   b: [[now - 1200, 5], [now - 60, 5 + usec(2, 1140)]],
                   "/gone.scope": [[now - 9000, 0], [now - 8000, 10]]}  # older than the window: left out
        c.write_atomic(os.path.join(c.state_dir(), "cpu.json"), json.dumps({"samples": samples}))
        got = json.loads(self.fm("ui", "--json").stdout)["cpu"]
        self.assertEqual((got["goal"], got["cores"], got["step_s"]), (10.0, cores, 150))
        names = [s["name"] for s in got["sessions"]]
        self.assertEqual(sorted(names), ["term-1", "term-2"])
        one = next(s["pct"] for s in got["sessions"] if s["name"] == "term-1")
        self.assertEqual(len(one), 12)
        self.assertEqual(one[:6], [0.0] * 6)  # nothing sampled before 15 min ago
        self.assertAlmostEqual(one[7], 4.0, places=1)
        self.assertAlmostEqual(one[9], 8.0, places=1)
        self.assertAlmostEqual(one[-1], 8.0, places=1)  # since the newest sample: its rate still holds
        self.assertAlmostEqual(got["now"], 10.0, places=1)  # 8 + 2
        self.fm("cpu", "goal", "25")
        self.assertEqual(json.loads(self.fm("ui", "--json").stdout)["cpu"]["goal"], 25.0)

    def test_the_view_carries_the_train_and_the_files_each_lane_holds(self):
        # T-3701: the pane's MERGE picture (a train running, what landed in the last day, lanes parked) and LANES picture
        # (a dot per file a lane's leases hold), all without git: the train's lock, the ledger's lane_merge events, leases
        import fcntl
        import time
        from sampo import bus
        from sampo import core as c
        p = c.find_project(self.repo)
        lane = os.path.join(self.tmp, "lane")
        os.makedirs(lane)
        tid = self.fm("capture", "Building", "--type", "FIX", "--tier", "S").stdout.split()[2]
        b = c.find_brief(p, tid)
        b.meta.update(lane=lane, status="active")
        c.save_brief(p, b)
        park = self.fm("capture", "Parked", "--type", "FIX", "--tier", "S").stdout.split()[2]
        b = c.find_brief(p, park)
        b.meta.update(lane=lane, status="active", lane_stop=f"conflict {c.iso(time.time())} src/a.py")
        c.save_brief(p, b)
        bus._write_leases({f"k{i}": {"project": p.slug, "task": tid, "file": f, "until": time.time() + 600}
                           for i, f in enumerate(["src/app.py", "src/util.py::main"])})
        c.log_event(p, "lane_merge", task="T-0900")
        v = json.loads(self.fm("ui", "--json").stdout)
        self.assertEqual((v["train"]["running"], v["train"]["landed"], v["train"]["landed_24h"], v["train"]["parked"]),
                         (False, ["T-0900"], 1, 1))
        self.assertEqual(v["train"]["branch"], "main")  # T-4008: the branch it lands on, the repo's, not foreman/build
        self.assertRegex(v["train"]["landed_at"][0], r"^\d{4}-\d\d-\d\dT\d\d:\d\d")  # when it landed: the list's time column
        lane_row = next(x for x in v["lanes"]["building"] if x["id"] == tid)
        self.assertEqual(lane_row["files"], ["src/app.py", "src/util.py"])  # a symbol's lease names its file
        os.makedirs(os.path.join(p.dir, "train"), exist_ok=True)
        with open(os.path.join(p.dir, "train", ".lock"), "a+") as f:  # a train holds its lock while it runs
            fcntl.flock(f, fcntl.LOCK_EX)
            self.assertTrue(json.loads(self.fm("ui", "--json").stdout)["train"]["running"])

    def test_the_view_carries_who_called_which_model(self):
        # T-3701: the pane's MODELS picture: a link a caller and model over five minutes, its calls in the last minute
        # hot; from the call log and Claude's spend log (older rows, and rows without a caller, left out)
        import time
        from sampo import core as c
        now = time.time()
        os.makedirs(c.state_dir(), exist_ok=True)
        with open(os.path.join(c.state_dir(), "calls.jsonl"), "w") as f:
            for at, a, to in [(now - 20, "cost", "qwen3.5:4b"), (now - 200, "cost", "qwen3.5:4b"), (now - 30, "cost", "qwen3.5:9b"),
                              (now - 900, "cost", "old:1b"), (now - 10, "", "qwen3.5:4b")]:
                f.write(json.dumps({"ts": c.iso(at), "from": a, "to": to}) + "\n")
        with open(os.path.join(c.state_dir(), "spend.jsonl"), "w") as f:
            f.write(json.dumps({"ts": c.iso(now - 100), "feature": "review", "usd": 0.1}) + "\n")
        v = json.loads(self.fm("ui", "--json").stdout)
        self.assertEqual([(x["from"], x["to"], x["n"], x["hot"]) for x in v["flows"]],
                         [("cost", "qwen3.5:4b", 2, 1), ("cost", "qwen3.5:9b", 1, 1), ("review", "claude", 1, 0)])

    def test_the_board_panels_read_a_cache_a_detached_run_refreshes(self):
        # T-3701: LIBRARY, SAMPO, STATE and TESTS come from cache/board.json, which `fm ui --board-cache` writes; a
        # stale or missing file starts that run detached (once in 30 s), never reads it inline on the poll
        import subprocess
        import time
        from unittest import mock
        from sampo import core as c
        from sampo import watch
        self.fm("task", "new", "Login retries", "--type", "FIX", "--tier", "S", "--ac", "it retries :: true", "--step", "retry",
                "--focus")
        self.fm("ui", "--board-cache")
        p = c.find_project(self.repo)
        with open(os.path.join(p.dir, "cache", "board.json")) as f:
            kept = json.load(f)
        self.assertTrue({"library", "state", "tests", "commands", "counts", "cpu"} <= set(kept))
        v = json.loads(self.fm("ui", "--json").stdout)
        self.assertEqual(v["board"], kept)
        self.assertEqual(v["active"]["words"], "Login retries")  # THE LOOP's "you asked": the raw request, else the title
        # the card and moon totals read every card and moon: a refresh keeps them for ten minutes
        c.write_atomic(os.path.join(p.dir, "cache", "board.json"), json.dumps(dict(kept, counts=dict(kept["counts"], cards=4242))))
        self.fm("ui", "--board-cache")
        with open(os.path.join(p.dir, "cache", "board.json")) as f:
            kept = json.load(f)
        self.assertEqual(kept["counts"]["cards"], 4242)
        old = time.time() - 120
        os.utime(os.path.join(p.dir, "cache", "board.json"), (old, old))
        with mock.patch.dict(os.environ, {"FOREMAN_NO_BACKGROUND": ""}), mock.patch.object(subprocess, "Popen") as run:
            self.assertEqual(watch._board_extras(p), kept)  # the stale file answers this poll
            watch._board_extras(p)  # a refresh already started: no second one
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args[0][0][-2:], ["ui", "--board-cache"])

    def test_a_sampo_command_shows_on_the_next_poll_not_the_next_board_cache(self):
        # T-4008: the SAMPO panel said "no sampo commands seen yet" for the 30 s board.json lives, while they ran
        from sampo import core as c
        self.fm("ui", "--board-cache")
        p = c.find_project(self.repo)
        with open(os.path.join(c.state_dir(), "events.jsonl"), "a") as f:
            for i, (cmd, kind) in enumerate([("ls && sampo task show T-0001", "tool"), ("sampo nope", "tool_fail"),
                                             ("git status", "tool")]):
                f.write(json.dumps({"ts": f"2030-01-01T00:00:0{i}Z", "kind": kind, "tool": "Bash", "project": p.slug,
                                    "target": cmd}) + "\n")
            f.write(json.dumps({"ts": "2030-01-01T00:00:09Z", "kind": "tool", "tool": "Bash", "project": "other-1",
                                "target": "sampo state"}) + "\n")
        v = json.loads(self.fm("ui", "--json").stdout)
        self.assertEqual([(x["cmd"], x["ok"]) for x in v["board"]["commands"]],
                         [("sampo task show T-0001", True), ("sampo nope", False)])

    def test_the_board_cache_keeps_the_cpu_by_kind_a_minute_a_bar(self):
        # T-3701: the pane's CPU panel as the web Board's: the cores each kind uses now, and a bar a minute of the last
        # 30, from the readings of this refresh and the earlier ones the cache kept
        import time
        from unittest import mock
        from sampo import board, watch
        from sampo import core as c
        p = c.find_project(self.repo)
        now = time.time()
        m = int(now // 60 * 60)
        was = {"samples": [[now - 3000, 9, 9, 9, 9, 9], [m - 59, 1, 0, 0, 0, 1], [m, 2, 1, 0, 0, 0]]}  # too old, a minute ago, now
        part = {"kinds": list(board.KINDS), "goal_pct": 10, "cores": 16, "minutes": [],
                "now": {"tests": 4.0, "claude": 3.0, "models": 0.0, "hooks": 0.0, "other": 1.0, "busy": 8.0}}
        with mock.patch.object(board, "cpu_tick"), mock.patch.object(board, "cpu_part", return_value=part), \
                mock.patch.object(time, "sleep"):
            got = watch._board_cpu(p, was)
            self.assertEqual(watch._board_cpu(p, None)["minutes"], [[m, 4.0, 3.0, 0.0, 0.0, 1.0]])
            with mock.patch.object(board, "cpu_part", return_value=dict(part, now=None)):
                self.assertIs(watch._board_cpu(p, was), was)  # /proc unreadable: the last reading stands
        self.assertEqual(got["minutes"], [[m - 60, 1.0, 0.0, 0.0, 0.0, 1.0], [m, 3.0, 2.0, 0.0, 0.0, 0.5]])
        self.assertEqual((len(got["samples"]), got["now"]["busy"], got["cores"]), (3, 8.0, 16))

    def test_the_inbox_lists_the_users_own_asks_first(self):
        # T-3664: intake rank put 154 discovered items above the user's 10 asks; the pane shows theirs first
        self.fm("capture", "Grounded brainstorm of the parser", "--type", "RESEARCH", "--source", "discovered")
        self.fm("capture", "Make the live view look like the reference", "--type", "FEATURE", "--source", "user")
        v = json.loads(self.fm("ui", "--json").stdout)
        self.assertEqual([x["source"] for x in v["inbox"]], ["user", "user", "discovered"])
        self.assertIn("Make the live view look like the reference", [x["title"] for x in v["inbox"][:2]])
        self.assertEqual((v["inbox_total"], v["inbox_yours"]), (3, 2), "setUp's capture is the user's too")
        self.assertEqual(v["queue_total"], len(v["queue"]), "the count the pane shows is the queue's, not the list's cap")

    def test_the_view_counts_blocked_tasks_for_the_work_bar(self):
        # T-1026: the pane shows done and left at a glance, blocked included
        self.assertEqual(json.loads(self.fm("ui", "--json").stdout)["blocked_total"], 0)
        self.fm("task", "block", "T-0001", "waits on a vendor")
        self.assertEqual(json.loads(self.fm("ui", "--json").stdout)["blocked_total"], 1)

    def test_done_days_counts_closes_per_day_for_two_weeks(self):
        # T-0909: the pane's day bars; oldest first, today last, a day counts a task once
        import datetime
        from sampo import core as c
        p = c.find_project(self.repo)
        day = lambda n: (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=n)).strftime("%Y-%m-%dT12:00:00Z")  # noqa
        for t in ("T-0001", "T-0002"):  # done briefs; T-0003 and T-0004 aren't (gone or reopened): they don't count
            b = c.find_brief(p, t)
            with open(b.path) as f:
                text = f.read()
            with open(b.path, "w") as f:
                f.write(text.replace("status: planned", "status: done", 1).replace("status: active", "status: done", 1))
        rows = [(day(0), "T-0001"), (day(0), "T-0001"), (day(2), "T-0002"), (day(2), "T-0003"), (day(20), "T-0004")]
        noise = [{"ts": day(0), "event": "note", "task": "T-0009", "data": {}}] * 6000  # past a 5000-event tail (review)
        with open(os.path.join(p.dir, "ledger.jsonl"), "a") as f:
            f.writelines(json.dumps({"ts": ts, "event": "task_done", "task": t, "data": {}}) + "\n" for ts, t in rows)
            f.writelines(json.dumps(e) + "\n" for e in noise)
        v = json.loads(self.fm("ui", "--json").stdout)
        days = v["done_days"]
        self.assertEqual(len(days), 14)
        self.assertEqual((days[-1], days[-3], sum(days)), (1, 1, 2))
        self.assertEqual(v["today_done"], days[-1])

    def test_today_done_ignores_tasks_closed_earlier_and_edited_today(self):
        from sampo import core as c
        p = c.find_project(self.repo)
        with open(os.path.join(p.dir, "ledger.jsonl"), "a") as f:  # T-0002 was closed long ago…
            f.write(json.dumps({"ts": "2000-01-01T00:00:00Z", "event": "task_done", "task": "T-0002", "data": {}}) + "\n")
        b = c.find_brief(p, "T-0002")
        with open(b.path) as f:
            text = f.read()
        with open(b.path, "w") as f:  # …and its brief, done, was saved today (an audit note, a lesson)
            f.write(text.replace("status: planned", "status: done", 1))
        self.assertEqual(json.loads(self.fm("ui", "--json").stdout)["today_done"], 0)

    def test_typical_minutes_per_type_and_size_and_time_on_task(self):
        # T-0116: "medium" means more beside "usually ~30 min", from this project's own history
        from sampo import core as c
        p = c.find_project(self.repo)
        rows = []
        for i, took in enumerate([10, 30, 50, 20]):  # three closed FIX/S tasks (median 30) and one FEATURE/M
            tid, kind = f"T-09{i:02d}", ("FEATURE", "M") if i == 3 else ("FIX", "S")
            rows += [{"ts": f"2026-01-0{i + 1}T09:00:00Z", "event": "task_new", "task": tid,
                      "data": {"type": kind[0], "tier": kind[1]}},
                     {"ts": f"2026-01-0{i + 1}T10:00:00Z", "event": "focus", "task": tid},
                     {"ts": f"2026-01-0{i + 1}T10:{took:02d}:00Z", "event": "task_done", "task": tid}]
        with open(os.path.join(p.dir, "ledger.jsonl"), "a") as f:
            f.writelines(json.dumps(r) + "\n" for r in rows)
        v = json.loads(self.fm("ui", "--json").stdout)
        self.assertEqual(v["typical"], {"FIX/S": 30}, "one FEATURE/M sample is too few to call typical")
        self.assertGreaterEqual(v["active"]["on_task_s"], 0)
        self.assertLess(v["active"]["on_task_s"], 600)

    def test_the_newest_brainstorm_shows_while_it_runs_and_after(self):
        # T-0124: brainstorms ran unseen inside one long Bash call until it ended
        from sampo import core as c
        p = c.find_project(self.repo)
        d = os.path.join(p.dir, "research", "brainstorm-20261003-010203")
        os.makedirs(d)
        with open(os.path.join(d, "status.json"), "w") as f:
            json.dump({"lenses": ["user value", "delight"], "rounds": 2, "started": c.now()}, f)
        with open(os.path.join(d, "r1-01-user-value.md"), "w") as f:
            f.write("# Brainstorm — lens: user value\n\n- **Faster gates** — PERF — value 5\n- **A mascot** — FEATURE\n")
        b = json.loads(self.fm("ui", "--json").stdout)["brainstorm"]
        self.assertEqual((b["running"], b["answers"], b["expected"]), (True, 1, 4))
        self.assertEqual(b["ideas"], ["Faster gates", "A mascot"])
        with open(os.path.join(d, "ideas.md"), "w") as f:
            f.write("# Ideas by round\n\n## Round 1\n- Faster gates\n- A mascot\n- Wild: talk to it\n\n"
                    "## New ideas per lens\n- user value: 3\n")
        b = json.loads(self.fm("ui", "--json").stdout)["brainstorm"]
        self.assertFalse(b["running"])
        self.assertEqual(b["count"], 3)
        self.assertEqual(b["ideas"][:2], ["Faster gates", "A mascot"])
        self.assertEqual(b["name"], "brainstorm-20261003-010203")
        self.assertLess(b["age_h"], 1)  # T-0146: an old one folds to a line in the pane
        self.assertFalse(b["grounded"])
        note = os.path.join(p.dir, "research", "brainstorm-20261003-round1.md")  # T-0196: its slate is built
        with open(note, "w") as f:
            f.write("# Grounded slate\n")
        os.utime(note, (c.time.time() + 5,) * 2)
        self.assertTrue(json.loads(self.fm("ui", "--json").stdout)["brainstorm"]["grounded"])
