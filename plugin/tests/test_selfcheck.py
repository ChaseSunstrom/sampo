"""T-3127: self-checks name what the user would otherwise point out, with its numbers, in every state (sampo next and
the drive), and go quiet once it's resolved. 2026-11-06's questions are the fixtures: landed work unshipped for hours,
a retired .foreman/ tracked beside .sampo/, the user's asks not landed."""
import contextlib
import datetime
import io
import json
import os
import subprocess
import time
import types
import unittest.mock

from helpers import ForemanTestCase

from sampo import core as c
from sampo import selfcheck

first = lambda text: json.JSONDecoder().raw_decode(text)[0]  # noqa: E731


def ago(hours):
    t = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


class SelfCheck(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)

    def git(self, *a, env=None):
        return subprocess.run(["git", "-C", self.repo, *a], check=True, capture_output=True, text=True,
                              env=env).stdout.strip()

    def land(self, name, hours):
        with open(os.path.join(self.repo, name), "w") as f:
            f.write(name)
        env = {**os.environ, "GIT_AUTHOR_DATE": ago(hours), "GIT_COMMITTER_DATE": ago(hours)}
        self.git("add", "-A")
        self.git("commit", "-qm", name, env=env)

    def test_landed_work_unshipped_for_hours_is_counted_and_aged(self):
        self.land("a.txt", 30)
        self.assertIsNone(selfcheck.unshipped(self.p, [], time.time()), "a project that never shipped says nothing")
        self.git("update-ref", "refs/foreman/shipped", "HEAD")
        self.land("b.txt", 5)
        self.land("c.txt", 1)
        self.assertEqual(selfcheck.unshipped(self.p, [], time.time()),
                         "2 landed commit(s) unshipped, the oldest 5 h → sampo ship")
        self.git("update-ref", "refs/foreman/shipped", "HEAD")
        self.assertIsNone(selfcheck.unshipped(self.p, [], time.time()), "shipped: quiet")

    def test_a_ship_ref_off_this_history_says_nothing(self):
        # review: after a rebase or re-pick the range would count commits main already has
        self.land("a.txt", 30)
        self.git("update-ref", "refs/foreman/shipped", "HEAD")
        self.git("checkout", "-q", "--orphan", "other")
        self.land("b.txt", 5)
        self.assertIsNone(selfcheck.unshipped(self.p, [], time.time()))

    def test_fresh_work_is_not_yet_unshipped(self):
        self.land("a.txt", 30)
        self.git("update-ref", "refs/foreman/shipped", "HEAD")
        self.land("b.txt", 0.5)
        self.assertIsNone(selfcheck.unshipped(self.p, [], time.time()))

    def test_a_retired_mirror_tracked_beside_the_new_one_is_named(self):
        old, new = c.MIRRORS[1], c.MIRROR
        os.makedirs(os.path.join(self.repo, old, "tasks"))
        os.makedirs(os.path.join(self.repo, new), exist_ok=True)
        with open(os.path.join(self.repo, old, "tasks", "T-0001.md"), "w") as f:
            f.write("# x\n")
        self.git("add", "-f", old)
        self.git("commit", "-qm", "old mirror back")
        self.assertIn(f"the retired {old}/ is tracked beside {new}/ → sampo sync export",
                      selfcheck.old_mirror(self.p, [], time.time()))
        self.git("rm", "-rq", old)
        self.git("commit", "-qm", "gone")
        self.assertIsNone(selfcheck.old_mirror(self.p, [], time.time()))

    def test_untracked_files_in_the_main_checkout_are_named(self):
        # T-3548: an untracked scripts/ sat in the main checkout all day; every close missed the train's pass
        self.assertIsNone(selfcheck.untracked(self.p, [], time.time()), "a clean checkout says nothing")
        for rel in ("scripts/a.py", "scripts/b.py", "notes.txt", "build.log", f"{c.MIRROR}/tasks/T-0099.md"):
            os.makedirs(os.path.dirname(os.path.join(self.repo, rel)), exist_ok=True)
            with open(os.path.join(self.repo, rel), "w") as f:
                f.write("x\n")
        with open(os.path.join(self.repo, ".git", "info", "exclude"), "a") as f:
            f.write("*.log\n")  # ignored: never part of the code tree
        self.assertIsNone(selfcheck.untracked(self.p, [], time.time()), "files made just now are work in progress")
        old = time.time() - 2 * 3600
        for rel in ("scripts/a.py", "scripts/b.py", "notes.txt", "build.log", f"{c.MIRROR}/tasks/T-0099.md"):
            os.utime(os.path.join(self.repo, rel), (old, old))
        with open(os.path.join(self.repo, "fresh.py"), "w") as f:
            f.write("x\n")  # still being written: not counted
        hit = selfcheck.untracked(self.p, [], time.time())
        self.assertTrue(hit.startswith("3 untracked file(s) in the main checkout (scripts/ (2), notes.txt)"), hit)
        self.assertIn("commit, .gitignore or remove them", hit)
        lane = os.path.join(self.tmp, "lane")
        self.git("commit", "-q", "--allow-empty", "-m", "base")
        self.git("worktree", "add", "-q", lane)
        self.assertEqual(selfcheck.untracked(c.find_project(lane), [], time.time()), hit, "a lane reads the main checkout")
        for rel in ("scripts/a.py", "scripts/b.py", "notes.txt"):
            os.remove(os.path.join(self.repo, rel))
        self.assertIsNone(selfcheck.untracked(self.p, [], time.time()))

    def test_a_map_file_near_its_read_cap_is_named_before_a_close_goes_red(self):
        # T-4057: files-lib-3.md reached 89,990 of 90,000 bytes; the next module mapped there turned test_docs red
        self.assertIsNone(selfcheck.map_room(self.p, [], time.time()), "no MASTER.md, nothing to say")
        os.makedirs(os.path.join(self.repo, "docs", "map"))
        for rel, size in (("MASTER.md", 80000), ("docs/map/files-lib-9.md", c.MAP_FILE_BYTES - 500)):
            with open(os.path.join(self.repo, rel), "w") as f:
                f.write("x" * size)
        hit = selfcheck.map_room(self.p, [], time.time())
        self.assertTrue(hit.startswith("docs/map/files-lib-9.md has 500 bytes left of its 90000"), hit)
        self.assertNotIn("MASTER.md has", hit, "10 KB of room is plenty")
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.assertIsNone(selfcheck.map_room(types.SimpleNamespace(root=root), [], time.time()),
                          "Sampo's own map keeps 2 KB of room in every file")

    def a_shard_and_a_lane_branch(self, left, adds):
        """A shard `left` bytes under its cap on the trunk and a branch `lane1` that adds `adds` bytes to it."""
        os.makedirs(os.path.join(self.repo, "docs", "map"))
        shard = os.path.join(self.repo, "docs", "map", "files-lib-9.md")
        for path, size in ((os.path.join(self.repo, "MASTER.md"), 1000), (shard, c.MAP_FILE_BYTES - left)):
            with open(path, "w") as f:
                f.write("x" * size)
        self.git("add", "-A")
        self.git("commit", "-qm", "the map")
        self.git("checkout", "-q", "-b", "lane1")
        with open(shard, "a") as f:
            f.write("y" * adds)
        self.git("commit", "-qam", "a lane's map line")
        self.git("checkout", "-q", "-")

    def waiting(self, tid="T-0007", **meta):
        return types.SimpleNamespace(id=tid, status="active", meta={"lane_branch": "lane1", **meta})

    def test_waiting_lanes_that_fill_a_map_file_are_named_before_their_train(self):
        # T-4411: 3 KB left on the trunk, four accepted lanes added 5 KB: two trains went red before anyone was told
        self.a_shard_and_a_lane_branch(left=5000, adds=4000)
        now = time.time()
        self.assertIsNone(selfcheck.map_room(self.p, [], now), "5 KB of room on the trunk alone is plenty")
        hit = selfcheck.map_room(self.p, [self.waiting(accepted_at=now)], now)
        self.assertIn("docs/map/files-lib-9.md has 5000 bytes left of its 90000", hit)
        self.assertIn("T-0007", hit)
        self.assertIn("4000", hit)
        self.assertIn("split it", hit)

    def test_waiting_lanes_not_accepted_or_closed_or_small_are_not_counted(self):
        self.a_shard_and_a_lane_branch(left=5000, adds=4000)
        now = time.time()
        self.assertIsNone(selfcheck.map_room(self.p, [self.waiting()], now), "not accepted: still being built")
        done = self.waiting(accepted_at=now)
        done.status = "done"
        self.assertIsNone(selfcheck.map_room(self.p, [done], now), "closed: it landed or was dropped")
        gone = types.SimpleNamespace(id="T-0008", status="active", meta={"lane_branch": "no-such", "accepted_at": now})
        self.assertIsNone(selfcheck.map_room(self.p, [gone], now), "a branch git does not know adds nothing")

    def test_waiting_lanes_cost_no_git_call_while_every_file_has_plenty_of_room(self):
        self.a_shard_and_a_lane_branch(left=40000, adds=4000)
        with unittest.mock.patch.object(c, "_git", side_effect=AssertionError("git was called")):
            self.assertIsNone(selfcheck.map_room(self.p, [self.waiting(accepted_at=time.time())], time.time()))

    def ask(self, words, hours, **kw):
        tid = first(self.fm("capture", words, "--json", *kw.get("args", ())).stdout)["id"]
        b = c.find_brief(self.p, tid)
        b.meta["created"] = ago(hours)
        c.save_brief(self.p, b)
        return tid

    def test_the_users_asks_not_landed_are_counted_oldest_first(self):
        old = self.ask("remake the rune page as kalevala", 20)
        newer = self.ask("re-record the readme demo video", 6)
        self.ask("a request from an hour ago", 1)  # too fresh to ask about
        self.ask("an ask from last week", 24 * 8)  # past the window
        self.ask("a bug found while working", 10, args=("--source", "discovered"))  # not the user's
        self.ask("(noticed by an agent while landing) a stale lock", 10)  # Sampo's own note, user-sourced by default
        hit = selfcheck.asks_waiting(self.p, c.load_briefs(self.p), time.time())
        self.assertTrue(hit.startswith("2 of the user's asks from the last 3 days aren't landed; oldest: "
                                       f"{old} (20 h, captured); {newer} (6 h, captured)"), hit)
        for tid in (old, newer):
            self.fm("task", "drop", tid, "done elsewhere")
        self.assertIsNone(selfcheck.asks_waiting(self.p, c.load_briefs(self.p), time.time()))

    def test_a_pile_of_waiting_lanes_is_named_with_its_landing_rate(self):
        # T-3478: "why is this stuff taking so long just to push?": 50+ lanes waited at ~15 min each, never said
        from unittest import mock
        from sampo import lanes as slanes
        waiting = [f"T-00{n:02d}" for n in range(10, 16)]
        with mock.patch.object(slanes, "awaiting_merge", return_value=waiting):
            self.assertIn("6 finished lanes wait and none landed in the last 2 h",
                          selfcheck.landing_rate(self.p, [], time.time()))
            for tid in ("T-0001", "T-0002", "T-0003"):
                c.log_event(self.p, "lane_merge", task=tid, data={"train": ["T-0001", "T-0002", "T-0003"]})
            self.assertIn("6 finished lanes wait; 3 landed in the last 2 h (~40 min each), so the pile clears in ~4 h",
                          selfcheck.landing_rate(self.p, [], time.time()))
        with mock.patch.object(slanes, "awaiting_merge", return_value=waiting[:4]):
            self.assertIsNone(selfcheck.landing_rate(self.p, [], time.time()), "a short pile says nothing")

    def test_finished_lanes_never_accepted_for_days_are_named_with_the_oldest_and_what_to_do(self):
        # T-4938: 49 lanes 5-12 days old held commits nobody accepted; no check, so no train and no line anywhere
        from unittest import mock
        from sampo import lanes as slanes
        day = 86400
        with mock.patch.object(slanes, "never_accepted", return_value=[("T-0012", 12 * day + 5), ("T-0030", 5 * day)]):
            hit = selfcheck.orphaned_lanes(self.p, [], time.time())
        self.assertEqual(hit, "2 finished lanes never accepted (oldest 12 days): sampo lane accept T-0012, or sampo "
                              "lane rm T-0012 if superseded")
        with mock.patch.object(slanes, "never_accepted", return_value=[("T-0012", 30 * 3600)]):
            self.assertIn("1 finished lane never accepted (oldest 1 day)", selfcheck.orphaned_lanes(self.p, [], time.time()))
        with mock.patch.object(slanes, "never_accepted", return_value=[]):
            self.assertIsNone(selfcheck.orphaned_lanes(self.p, [], time.time()))
        self.assertIn(selfcheck.orphaned_lanes, selfcheck.CHECKS)

    def test_lines_are_cached_and_reach_sampo_next(self):
        self.ask("remake the rune page as kalevala", 20)
        now = time.time()
        first_lines = selfcheck.lines(self.p, now=now)
        self.assertEqual(len(first_lines), 1)
        self.ask("re-record the readme demo video", 6)
        self.assertEqual(selfcheck.lines(self.p, now=now + 10), first_lines, "cached for a hook's every Stop")
        self.assertIn("2 of the user's asks", selfcheck.lines(self.p, now=now + selfcheck.CACHE_S + 1)[0])
        os.remove(os.path.join(self.p.dir, "selfcheck.json"))
        self.assertIn("self-check: 2 of the user's asks", self.fm("next").stdout)
        note = self.hook("UserPromptSubmit", {"prompt": "hi", "session_id": "sess-1"}).stdout
        self.assertNotIn("self-check:", note, "the prompt note's 400 characters stay for the task and its next step")

    def laned(self, title, name=None):
        tid = first(self.fm("task", "new", title, "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true", "--step",
                            "s", "--json").stdout)["id"]
        path = json.loads(self.fm("lane", "new", tid, "--json").stdout)["path"]
        if name:
            self.fm("focus", tid, cwd=path)
            with open(os.path.join(path, name), "w") as f:
                f.write(name)
            self.git("-C", path, "add", name)
            self.git("-C", path, "commit", "-qm", name)
        return tid, path

    def test_lanes_that_outlived_their_task_are_counted_and_named(self):
        # T-3540: 13 closed tasks' lanes and 11 with no task (old /tmp scratch worktrees) sat among 126, never named
        done, _ = self.laned("Closed in its lane")
        self.fm("task", "drop", done, "done elsewhere")
        self.laned("Still open")
        old, young = os.path.join(self.tmp, "scratch-old"), os.path.join(self.tmp, "scratch-young")
        for path in (old, young):
            self.git("worktree", "add", "-q", "--detach", path)
        os.utime(os.path.join(old, ".git"), (time.time() - 7200, time.time() - 7200))
        briefs = c.load_briefs(self.p)
        self.assertEqual(selfcheck.leftover_lanes(self.p, briefs, time.time()),
                         f"2 lanes outlived their task (closed: {done}; no task: 1) → sampo lane rm ID --why … or sampo "
                         "tidy; git worktree remove PATH for one with no task (sampo lane list names it)")
        self.fm("lane", "rm", done)
        self.git("worktree", "remove", old)
        self.assertIsNone(selfcheck.leftover_lanes(self.p, c.load_briefs(self.p), time.time()),
                          "a young worktree with no task may be a builder's not yet focused: quiet")

    def test_a_close_removes_its_own_merged_lane_and_keeps_an_unmerged_one(self):
        # T-3540: a task closed from the main checkout after its lane's merge left the lane on disk
        merged, mpath = self.laned("Merged work", "m.txt")
        unmerged, upath = self.laned("Unmerged work", "u.txt")
        self.git("merge", "-q", "--no-ff", "-m", "merge", f"foreman/{merged}")
        out = self.fm("task", "finish", merged, "--run", "true", "--audit", "self check").stdout
        self.assertIn(f"{merged}: lane {mpath} removed", out)
        self.assertFalse(os.path.isdir(mpath))
        self.assertNotIn("lane", c.find_brief(self.p, merged).meta)
        self.fm("task", "finish", unmerged, "--run", "true", "--audit", "self check")
        self.assertTrue(os.path.isdir(upath), "an unmerged branch's lane stays")
        self.assertIn(f"closed: {unmerged}", selfcheck.leftover_lanes(self.p, c.load_briefs(self.p), time.time()))

    def test_agents_alive_hours_after_reporting_are_named_with_ages(self):
        # T-3540: four builders stayed listed as running 8-18 h after they reported: watchers they started never ended
        from sampo import hooks
        t = time.time()
        for aid, kind in (("a1old", "sampo:fm-builder"), ("a2old", "general-purpose"), ("a9gone", "general-purpose")):
            c.log_event(self.p, "subagent", data={"agent_id": aid, "agent_type": kind})
        at = t + 9 * 3600  # the session's last Stop, nine hours on: all four still listed
        since = {k: t - 600 for k in ("a1old", "a2old", "a3working", "bshell")}
        gate = {"drive": {"sess-1": {"since": since, "since_at": at},
                          "sess-0": {"since": {"a9gone": t - 600}, "since_at": t}}}  # a session that ended
        with open(os.path.join(self.p.dir, "gate.json"), "w") as f:
            json.dump(gate, f)
        self.assertEqual(selfcheck.long_background(self.p, [], at + 60),
                         "2 background jobs alive 9 h after their agent reported: a1old (fm-builder, 9 h), a2old "
                         "(general-purpose, 9 h) → stop them (TaskStop ID) and end the watchers they started")
        gate["drive"]["sess-1"]["since_at"] = t + 60
        with open(os.path.join(self.p.dir, "gate.json"), "w") as f:
            json.dump(gate, f)
        self.assertIsNone(selfcheck.long_background(self.p, [], t + 120), "reported a minute ago: not left over yet")
        self.assertGreater(hooks.LONG_JOB_S, 120)

    def test_the_stop_hook_dates_the_running_set_it_saw(self):
        # T-3540: since (each job first seen running) is read only while its session's last Stop is fresh, and is kept
        # with no task to work too (a session waiting on its builders is when they linger)
        self.hook("Stop", {"stop_hook_active": False, "last_assistant_message": "Waiting on the build.",
                           "background_tasks": [{"id": "a5", "type": "agent", "status": "running"}]})
        with open(os.path.join(self.p.dir, "gate.json")) as f:
            d = json.load(f)["drive"]["sess-1"]
        self.assertIn("a5", d["since"])
        self.assertLess(abs(d["since_at"] - time.time()), 120)

    def push(self, bg, since, scan):
        from sampo import hooks
        sd = {"drive": True, "paused": False, "active": None, "queue": [], "inbox": [], "autonomy": "full",
              "pending": []}
        g = {"drive": {"sess-1": {"count": 0, "since": since}}}
        with unittest.mock.patch.object(hooks, "_running", side_effect=scan) as run:
            hooks._drive_push(self.p, sd, [], {"session_id": "sess-1", "background_tasks": bg}, g)
        return run.call_count, g["drive"]["sess-1"]

    def test_a_stop_with_no_work_and_nothing_listed_skips_the_event_scan(self):
        # T-3540 review: the 6000-row events tail ran on every Stop, work or none, on the hook's p95 bench
        calls, d = self.push([], {}, lambda *a: [])
        self.assertEqual(calls, 0, "no work, no engine list, nothing dated: the scan has nothing to say")
        calls, d = self.push([{"id": "a5", "type": "agent", "status": "running"}], {}, lambda *a: [])
        self.assertEqual(calls, 1)
        self.assertIn("a5", d["since"], "a listed job is still dated with no work")
        calls, d = self.push([], {"a5": time.time() - 60}, lambda *a: [])
        self.assertEqual(calls, 1)
        self.assertEqual(d["since"], {}, "a dated job that is gone is dropped")

    def test_a_close_that_cannot_remove_its_lane_warns_and_stays_closed(self):
        # T-3540 review: remove() raising an OSError after the state change must not fail the close
        from sampo import cli, lanes
        tid, path = self.laned("Merged work", "m.txt")
        self.git("merge", "-q", "--no-ff", "-m", "merge", f"foreman/{tid}")
        out, err, cwd = io.StringIO(), io.StringIO(), os.getcwd()
        os.chdir(self.repo)
        try:
            with unittest.mock.patch.dict(os.environ, {"FOREMAN_HOME": self.home, "FOREMAN_INHIBITED": "1"}), \
                    unittest.mock.patch.object(lanes, "remove", side_effect=OSError("disk gone")), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    code = cli.main(["task", "finish", tid, "--run", "true", "--audit", "self check"])
                except SystemExit as e:
                    code = e.code
        finally:
            os.chdir(cwd)
        self.assertIn(code, (0, None), err.getvalue())
        self.assertIn("warning: lane not removed: disk gone", err.getvalue())
        self.assertEqual(c.find_brief(self.p, tid).status, "done")

    def test_a_close_names_what_ending_the_lane_ended(self):
        # T-3540 review: remove() ends the processes left in the folder; the close says so
        from sampo import lanes
        tid, path = self.laned("Merged work", "m.txt")
        self.git("merge", "-q", "--no-ff", "-m", "merge", f"foreman/{tid}")
        left = {"ended": ["sleep"], "count": 1, "kept": []}
        with unittest.mock.patch.object(lanes, "end_lane_procs", return_value=left):
            note = lanes.close_lane(self.p, c.find_brief(self.p, tid))
        self.assertIn("removed", note)
        self.assertIn("ended 1 leftover process: sleep", note)


class SelfCheckBehaviour(ForemanTestCase):
    """T-3482: what the user asked next, "why is this stuff taking so long just to push?" and "you need to be aware of
    everything and all of your behavior": a job past its typical time, a command failing over and over, and the agent's
    own wait-only turns, each from data Sampo records."""

    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)

    def gates(self, *totals, train=False):
        for s in totals:
            c.log_event(self.p, "check_run", data={"results": [{"cmd": "a", "exit": 0, "s": s * 0.4},
                                                               {"cmd": "b", "exit": 0, "s": s * 0.6}],
                                                   **({"train": ["T-1"]} if train else {})})

    def test_a_live_job_past_1_5x_its_typical_time_is_named_with_both(self):
        now = time.time()
        self.gates(900, 840, 960, 3000)  # minutes 15, 14, 16 and one slow outlier: the median is 16 min
        self.gates(60, 60, 60, train=True)  # the lane train's gate runs are another job
        c.log_event(self.p, "check_run", data={"results": [{"cmd": "a", "exit": 1, "s": 5}]})  # a red run stops early
        c.update_meta(self.p, gates_running={"pid": os.getpid(), "at": c.iso(now - 41 * 60), "root": self.repo})
        self.assertEqual(selfcheck.slow_jobs(self.p, [], now),
                         "the full gates run has run 41 min, 2.6x its typical 16 min → see what it waits on")
        c.update_meta(self.p, gates_running={"pid": os.getpid(), "at": c.iso(now - 20 * 60), "root": self.repo})
        self.assertIsNone(selfcheck.slow_jobs(self.p, [], now), "1.25x: within its usual spread")
        c.update_meta(self.p, gates_running={"pid": 2 ** 22 + 7, "at": c.iso(now - 90 * 60), "root": self.repo})
        self.assertIsNone(selfcheck.slow_jobs(self.p, [], now), "a marker whose process is gone isn't a running job")

    def test_the_ships_test_run_is_timed_against_its_own_history(self):
        now = time.time()
        c.update_meta(self.p, ship_testing={"pid": os.getpid(), "at": c.iso(now - 30 * 60), "root": "/tmp/x"})
        self.assertIsNone(selfcheck.slow_jobs(self.p, [], now), "no recorded ship: no typical, nothing said")
        for s in (600, 660, 540):
            c.log_event(self.p, "ship_test", data={"cmd": "python3 plugin/tests/run.py", "exit": 0, "s": s})
        self.assertEqual(selfcheck.slow_jobs(self.p, [], now),
                         "sampo ship's test run has run 30 min, 3.0x its typical 10 min → see what it waits on")

    def evidence(self, task, cmd, ok):
        c.log_event(self.p, "evidence", task=task, data={"step": 1, "cmd": cmd, "result": (
            "exit 0 · Ran 3 tests" if ok else "✗ exit 1 · FAILED (failures=1)")})

    def test_a_command_failing_three_times_in_an_hour_is_a_retry_storm(self):
        self.evidence("T-1", "python3 plugin/tests/run.py test_x", True)
        self.evidence("T-1", "python3 plugin/tests/run.py test_x", True)
        self.evidence("T-1", "python3 plugin/tests/run.py test_x", True)  # TDD's own reruns pass: not a storm
        c.log_event(self.p, "evidence", task="T-1", data={"cmd": "python3 plugin/tests/run.py test_x",
                                                          "result": "red as expected (exit 1) · FAILED"})
        self.assertIsNone(selfcheck.repeats(self.p, [], time.time()))
        self.evidence("T-2", "nice -n 10 python3 plugin/tests/run.py   test_y", False)
        self.evidence("T-2", "python3 plugin/tests/run.py test_y", False)
        self.evidence("T-3", "sampo quiet -- python3 plugin/tests/run.py test_y", False)
        self.evidence("T-3", "python3 plugin/tests/run.py test_y", True)
        now = time.time()
        self.assertEqual(selfcheck.repeats(self.p, [], now),
                         "3 of 4 runs of `python3 plugin/tests/run.py test_y` in the last hour failed (T-2, T-3) → "
                         "read why it fails before running it again")
        self.assertIsNone(selfcheck.repeats(self.p, [], now + 3605), "an hour later: quiet")

    def stop(self, msg):
        return self.hook("Stop", {"stop_hook_active": False, "last_assistant_message": msg})

    def test_wait_only_replies_in_a_row_are_counted_and_any_other_reply_resets(self):
        self.assertEqual(selfcheck.lines(self.p), [], "cached quiet before the waits")
        for msg in ("Waiting on the ship's test run.", "Still waiting.", "The gates are running; waiting for them."):
            self.stop(msg)
        hit = selfcheck.wait_turns(self.p, [], time.time())
        self.assertTrue(hit and hit.startswith("3 wait-only replies in a row over 0 min → name the slow job's elapsed"),
                        hit)
        self.assertIn("self-check: 3 wait-only replies", self.fm("next").stdout, "live, not behind the 5-minute cache")
        self.assertIsNone(selfcheck.wait_turns(self.p, [], time.time() + selfcheck.WAIT_STALE_S + 1),
                          "a session gone quiet since says nothing")
        self.stop("Waiting on the build. " + "x" * 400)  # a long reply did work besides
        self.assertIsNone(selfcheck.wait_turns(self.p, [], time.time()))


class SelfAct(ForemanTestCase):
    """T-4118: a finding acts or tells; the steer audit (T-4115) found every check ended as a line nobody acted on."""
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)
        self.spawned, self.told = [], []

    def act(self, checks, now):
        return selfcheck.act(self.p, briefs=[], now=now, checks=checks,
                             spawn=lambda p, argv: self.spawned.append(argv) or 4242,
                             tell=lambda p, text: self.told.append(text))

    def test_act_runs_a_safe_action_once_backs_off_and_logs_it_with_drive_off(self):
        c.update_meta(self.p, drive=False)

        def landing(p, briefs, now):
            return selfcheck.Hit("3 finished lanes wait", action=["fm", "lane", "land"])
        t = 1_800_000_000
        self.act((landing,), t)
        self.act((landing,), t + 60)
        self.assertEqual(self.spawned, [["fm", "lane", "land"]], "once, then backs off")
        for i in range(1, 5):
            self.act((landing,), t + i * selfcheck.ACT_BACKOFF_S + 1)
        self.assertEqual(len(self.spawned), selfcheck.ACT_PER_DAY, "at most ACT_PER_DAY a day")
        ev = [e for e in c.ledger_events(self.p, ("self_act",))]
        self.assertTrue(ev and ev[0]["data"]["check"] == "landing" and ev[0]["data"]["pid"] == 4242, ev)

    def test_tell_reaches_the_user_once_a_day_per_check_and_again_only_when_worse(self):
        level = [1]

        def asks(p, briefs, now):
            return selfcheck.Hit(f"{level[0]} asks wait", tell=True, level=level[0])
        t = 1_800_000_000
        self.act((asks,), t)
        self.act((asks,), t + 3600)
        self.assertEqual(self.told, ["1 asks wait"])
        level[0] = 2
        self.act((asks,), t + 7200)
        self.assertEqual(self.told, ["1 asks wait", "2 asks wait"], "worse: told again the same day")
        self.act((asks,), t + 7200 + selfcheck.TELL_EVERY_S)
        self.assertEqual(len(self.told), 3, "a day later: told again")
        self.assertEqual(self.spawned, [], "a tell runs nothing")
        self.act((lambda p, b, n: "only printed",), t)
        self.assertEqual(len(self.told), 3, "a plain line only prints, as before")

    def test_an_action_whose_finding_outlives_its_deadline_or_never_started_escalates(self):
        def stuck(p, briefs, now):
            return selfcheck.Hit("lanes wait", action=["fm", "lane", "land"])
        t = 1_800_000_000
        self.act((stuck,), t)
        self.assertEqual(self.told, [], "acting first")
        self.act((stuck,), t + selfcheck.ESCALATE_S + 1)
        self.assertEqual(self.told, ["lanes wait"], "still there after its deadline: the user is told")
        self.told.clear()
        selfcheck.act(self.p, briefs=[], now=t, checks=(lambda p, b, n: selfcheck.Hit("x", action=["nope"]),),
                      spawn=lambda p, argv: None, tell=lambda p, text: self.told.append(text))
        self.assertEqual(self.told, ["x"], "an action that couldn't start escalates at once")

    def test_the_first_checks_carry_their_action_or_tell(self):
        b = types.SimpleNamespace(id="T-0009", status="planned", meta={"source": "user", "created": ago(5)},
                                  section=lambda name: "make it fast")
        hit = selfcheck.asks_waiting(self.p, [b], time.time())
        self.assertTrue(isinstance(hit, selfcheck.Hit) and hit.tell, hit)
        with open(os.path.join(c.state_dir(), "burn.json"), "w") as f:
            json.dump({"ts": time.time(), "found": [["weekly usage is on pace to run out 93 h before its reset", "x"]]}, f)
        hit = selfcheck.usage_burn(self.p, [], time.time())
        self.assertTrue(isinstance(hit, selfcheck.Hit) and hit.tell and "93 h" in hit, hit)

    def test_a_tell_is_a_row_the_pane_reads_back_for_a_day(self):
        selfcheck.notify(self.p, "weekly usage runs out 93 h early")
        rows = selfcheck.alerts(self.p)
        self.assertEqual([r["text"] for r in rows], ["weekly usage runs out 93 h early"])
        self.assertEqual(selfcheck.alerts(self.p, now=time.time() + 2 * 86400), [], "a day later it's gone")
        from sampo import watch
        self.assertEqual([r["text"] for r in watch.view(self.p)["alerts"]], ["weekly usage runs out 93 h early"])

    def test_the_tick_runs_act_so_the_sweep_and_the_timer_both_do(self):
        from sampo import night
        ran, real = [], selfcheck.act
        selfcheck.act = lambda p, **kw: ran.append(p.slug) or []
        try:
            night.tick(self.p, fetch_ci=False)
        finally:
            selfcheck.act = real
        self.assertEqual(ran, [self.p.slug])
