"""T-3542: fm ui --json keeps its JSON while its inputs stand still. The foreman-ui mod runs it on every 2 s poll a
tool call dirties; a rebuild costs ~0.7 s CPU, so a run whose project state, briefs and HEAD are unchanged prints the
stored view, and any of them moving rebuilds it."""
import json
import os
import shutil
import subprocess
import time

from helpers import ForemanTestCase

from sampo import core as c

ON = {"FOREMAN_UI_CACHE_S": "60"}  # the suite runs with the cache off (helpers); these tests turn it on


class UiCache(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)

    def hook_ms(self, *ms):
        """Hook timings in events.jsonl: the view's latency series shows them, and the cache key never reads them, so
        the series tells a rebuilt view from a kept one."""
        os.makedirs(c.state_dir(), exist_ok=True)
        with open(os.path.join(c.state_dir(), "events.jsonl"), "w") as f:
            f.writelines(json.dumps({"kind": "hook_ms", "event": "PreToolUse", "ms": m}) + "\n" for m in ms)

    def latency(self):
        return json.loads(self.fm("ui", "--json", env=ON).stdout)["latency"]

    def test_unchanged_inputs_print_the_kept_view_and_a_moved_one_rebuilds_it(self):
        self.fm("task", "new", "Tidy the parser", "--type", "FIX", "--tier", "S", "--ac", "ok :: true", "--step", "s")
        self.hook_ms(5)
        first = self.fm("ui", "--json", env=ON).stdout
        self.assertEqual(json.loads(first)["latency"], [5])
        self.hook_ms(7)
        self.assertNotIn("cached", json.loads(first))
        kept = json.loads(self.fm("ui", "--json", env=ON).stdout)
        self.assertEqual(kept, dict(json.loads(first), cached=True), "nothing it keys on moved: the kept view, marked")
        with open(os.path.join(self.p.dir, "map.json"), "w") as f:  # a file the key leaves to the age
            f.write("{}")
        self.assertEqual(self.latency(), [5])
        meta = os.path.join(self.p.dir, "meta.json")
        with open(meta, "a") as f:  # touched in place: the same inode, a new size and mtime
            f.write(" ")
        self.assertEqual(self.latency(), [7], "a touched state file rebuilds")
        self.hook_ms(9)
        self.assertEqual(self.latency(), [7])
        c.save_brief(self.p, c.find_brief(self.p, "T-0001"), touch=False)  # no ledger line: only the tasks folder moves
        self.assertEqual(self.latency(), [9], "a saved brief rebuilds")

    def test_the_trust_record_and_the_pause_flag_are_never_served_stale(self):
        self.hook_ms(5)
        self.latency()
        self.hook_ms(7)
        self.assertEqual(self.latency(), [5])
        with open(c.trust_path(), "w") as f:  # /fm-trust on: the mod writes it straight to disk
            json.dump({"on": True, "at": "2026-01-01T00:00:00Z", "via": "composer"}, f)
        v = json.loads(self.fm("ui", "--json", env=ON).stdout)
        self.assertEqual((v["latency"], v["mode"]["trust"]), ([7], True))
        self.hook_ms(9)
        with open(os.path.join(c.state_dir(), "PANIC"), "w") as f:  # sampo pause: autonomy reads as standard
            f.write("paused\n")
        self.assertEqual(self.latency(), [9], "a pause rebuilds")

    def test_an_old_view_is_rebuilt_and_off_never_keeps_one(self):
        self.hook_ms(5)
        self.latency()
        self.hook_ms(6)
        self.assertEqual(self.latency(), [5])
        path = os.path.join(self.p.dir, "cache", "ui.json")
        with open(path, encoding="utf-8") as f:
            kept = json.load(f)
        kept["at"] -= 61  # older than FOREMAN_UI_CACHE_S: clocks and unkeyed inputs are due
        c.write_atomic(path, json.dumps(kept))
        self.hook_ms(7)
        self.assertEqual(self.latency(), [7])
        self.hook_ms(9)
        self.assertEqual(json.loads(self.fm("ui", "--json").stdout)["latency"], [9], "FOREMAN_UI_CACHE_S=0: no cache")

    def test_a_rebuild_never_moves_the_folder_the_mod_watches(self):
        self.latency()  # the first run makes cache/ and the kept band lines
        at = os.stat(self.p.dir).st_mtime_ns
        self.hook_ms(3)
        os.utime(os.path.join(self.p.dir, "ledger.jsonl"))  # a moved input: this run rebuilds and stores again
        self.assertEqual(self.latency(), [3])
        self.assertEqual(os.stat(self.p.dir).st_mtime_ns, at, "a write there would wake the mod's next poll")


class UiCadence(ForemanTestCase):
    """T-3580: the pane ran fm ui ~1,665 times an hour at ~0.5 s CPU: it watched files that move with every tool call,
    and each run started ~20 git processes and re-read the ledgers whole. fm ui now names what refreshes the pane at
    once apart from the busy files, keeps git's answers while the refs stand still and reads the ledger from where it
    stopped."""

    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)

    def view(self, env=None):
        return json.loads(self.fm("ui", "--json", env=env).stdout)

    def test_the_view_names_the_states_the_user_acts_on_apart_from_the_busy_files(self):
        self.fm("task", "new", "Tidy the parser", "--type", "FIX", "--tier", "S", "--ac", "ok :: true", "--step", "s",
                "--focus")
        v = self.view()
        brief = c.find_brief(self.p, "T-0001").path
        self.assertEqual(v["watch_now"], [os.path.join(self.p.dir, "meta.json"), os.path.join(self.p.dir, "asks.json"),
                                          c.trust_path(), os.path.join(c.state_dir(), "risk_off.json"),
                                          os.path.join(self.p.dir, "risk_off.json"),
                                          os.path.join(c.state_dir(), "PANIC"), brief])
        self.assertNotIn(self.p.dir, v["watch"], "every derived file written there moved it: a rerun per tool call")
        self.assertIn(os.path.join(self.p.dir, "ledger.jsonl"), v["watch"])

    def git_log(self):
        """A git on PATH that logs each call: what a run of fm ui costs in processes."""
        shim, log = os.path.join(self.tmp, "shim"), os.path.join(self.tmp, "git.log")
        os.makedirs(shim, exist_ok=True)
        with open(os.path.join(shim, "git"), "w") as f:
            f.write(f'#!/bin/sh\necho "$*" >> "{log}"\nexec {shutil.which("git")} "$@"\n')
        os.chmod(os.path.join(shim, "git"), 0o755)

        def calls():
            if not os.path.exists(log):
                return 0
            with open(log) as f:
                return f.read().count("\n")
        return {"PATH": shim + os.pathsep + os.environ.get("PATH", "")}, calls

    def test_git_answers_are_kept_until_a_ref_moves(self):
        env, calls = self.git_log()
        lane = os.path.join(self.tmp, "lane")
        subprocess.run(["git", "-C", self.repo, "worktree", "add", "-q", "-b", "foreman/T-0009", lane], check=True)
        subprocess.run(["git", "-C", lane, "commit", "-q", "--allow-empty", "-m", "one"], check=True)

        def ahead(v):
            return next(w for w in v["worktrees"] if w["branch"] == "foreman/T-0009")["ahead"]
        self.assertEqual(ahead(self.view(env)), 1)
        first = calls()
        self.assertTrue(first, "the first run asks git")
        self.assertEqual(ahead(self.view(env)), 1)
        self.assertEqual(calls(), first, "nothing moved: no git process at all")
        subprocess.run(["git", "-C", lane, "commit", "-q", "--allow-empty", "-m", "two"], check=True)
        self.assertEqual(ahead(self.view(env)), 2, "a branch's commit moves its ref: asked again")
        subprocess.run(["git", "-C", self.repo, "merge", "-q", "--ff-only", "foreman/T-0009"], check=True)
        self.assertEqual(ahead(self.view(env)), 0, "and so does a move of the checked-out branch")

    def test_a_ref_only_in_packed_refs_and_worktrees_coming_and_going_are_asked_again(self):
        env, calls = self.git_log()
        lane, base = os.path.join(self.tmp, "lane"), c._git(self.repo, "rev-parse", "HEAD").strip()
        subprocess.run(["git", "-C", self.repo, "worktree", "add", "-q", "-b", "foreman/T-0009", lane], check=True)
        subprocess.run(["git", "-C", lane, "commit", "-q", "--allow-empty", "-m", "one"], check=True)
        subprocess.run(["git", "-C", self.repo, "pack-refs", "--all"], check=True)  # the nested loose ref is packed

        def trees(v):
            return {w["branch"]: w["ahead"] for w in v["worktrees"]}
        self.assertEqual(trees(self.view(env))["foreman/T-0009"], 1)
        n = calls()
        self.assertEqual(trees(self.view(env))["foreman/T-0009"], 1)
        self.assertEqual(calls(), n)
        packed = os.path.join(self.repo, ".git", "packed-refs")
        with open(packed, encoding="utf-8") as f:
            text = f.read()
        tip = next(ln.split()[0] for ln in text.splitlines() if ln.endswith(" refs/heads/foreman/T-0009"))
        with open(packed, "w", encoding="utf-8") as f:  # the branch moves where only packed-refs holds it
            f.write(text.replace(f"{tip} refs/heads/foreman/T-0009", f"{base} refs/heads/foreman/T-0009"))
        self.assertEqual(trees(self.view(env))["foreman/T-0009"], 0)
        other = os.path.join(self.tmp, "other")
        subprocess.run(["git", "-C", self.repo, "worktree", "add", "-q", "-b", "foreman/T-0010", other], check=True)
        subprocess.run(["git", "-C", other, "commit", "-q", "--allow-empty", "-m", "x"], check=True)
        self.assertEqual(trees(self.view(env)).get("foreman/T-0010"), 1, "a new worktree is listed")
        subprocess.run(["git", "-C", self.repo, "worktree", "remove", "--force", other], check=True)
        self.assertNotIn("foreman/T-0010", trees(self.view(env)), "and a removed one is gone")

    def test_only_answers_are_kept_never_a_git_failure(self):
        bare = os.path.join(self.tmp, "not-a-repo")
        os.makedirs(bare)
        c.GIT_KEPT = {}
        try:
            c._git(self.repo, "rev-parse", "--verify", "-q", "refs/nope")  # exit 1, quiet: no such ref
            c._git(self.repo, "rev-parse", "--verify", "refs/nope")  # exit 128: Needed a single revision
            c._git(self.repo, "rev-list", "--count", "HEAD")
            c._git(bare, "rev-parse", "HEAD")  # exit 128: not a git repository
            kept = {k.split("\0", 1)[1]: v[0] for k, v in c.GIT_KEPT.items()}
        finally:
            c.GIT_KEPT = None
        self.assertEqual(kept, {"rev-parse\0--verify\0-q\0refs/nope": 1, "rev-parse\0--verify\0refs/nope": 128,
                                "rev-list\0--count\0HEAD": 0})

    def done(self, tid, ts):
        with open(os.path.join(self.p.dir, "ledger.jsonl"), "a") as f:
            f.write(json.dumps({"ts": ts, "project": self.p.slug, "task": tid, "event": "task_done", "data": {}}) + "\n")

    def test_closes_are_read_from_where_the_last_run_stopped_and_a_rolled_ledger_whole(self):
        today = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        for n in (1, 2):
            self.fm("task", "new", f"Task {n}", "--type", "FIX", "--tier", "S", "--ac", "ok :: true", "--step", "s")
            b = c.find_brief(self.p, f"T-000{n}")
            b.meta["status"] = "done"
            c.save_brief(self.p, b)
        self.done("T-0001", today)
        self.assertEqual(self.view()["today_done"], 1)
        with open(os.path.join(self.p.dir, "cache", "closes.json"), encoding="utf-8") as f:
            kept = json.load(f)
        ledger = os.path.join(self.p.dir, "ledger.jsonl")
        self.assertEqual(kept[ledger]["at"], os.path.getsize(ledger), "how far it read, kept")
        self.done("T-0002", today)
        self.assertEqual(self.view()["today_done"], 2, "an appended close counts")
        with open(ledger, "w") as f:  # fm tidy rolled the month out: a shorter file in its place
            f.write("")
        self.done("T-0002", today)
        self.assertEqual(self.view()["today_done"], 1, "a rewritten ledger is read again whole")

    def lines(self, *tids, pad=True):
        """Ledger lines of one length each: a task_done names its task in the first bytes, a pad line is a note."""
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        return "".join(json.dumps({"task": t, "ts": ts, "event": "task_done" if t != "pad" else "note",
                                   "data": {"pad": "x" * 80}}) + "\n" for t in tids)

    def test_a_ledger_rolled_and_grown_back_past_the_offset_is_read_whole(self):
        self.fm("task", "new", "Task 1", "--type", "FIX", "--tier", "S", "--ac", "ok :: true", "--step", "s")
        self.fm("task", "new", "Task 2", "--type", "FIX", "--tier", "S", "--ac", "ok :: true", "--step", "s")
        b = c.find_brief(self.p, "T-0002")
        b.meta["status"] = "done"  # only T-0002's close counts
        c.save_brief(self.p, b)
        ledger = os.path.join(self.p.dir, "ledger.jsonl")
        with open(ledger, "w") as f:
            f.write(self.lines("T-0001", "pad"))
        self.assertEqual(self.view()["today_done"], 0)
        with open(ledger, "w") as f:  # rolled in place and grown back: the same inode and last-read bytes, a new head
            f.write(self.lines("T-0002", "pad", "pad"))
        self.assertEqual(self.view()["today_done"], 1, "the first bytes changed: read whole")
        with open(ledger, "w") as f:
            f.write(self.lines("pad", "T-0001", "pad"))
        self.assertEqual(self.view()["today_done"], 0)
        c.write_atomic(ledger, self.lines("pad", "T-0002", "pad", "pad"))  # the same first and last-read bytes
        self.assertEqual(self.view()["today_done"], 1, "another inode: read whole")

    def test_rows_older_than_the_window_are_not_kept(self):
        self.fm("task", "new", "Task 1", "--type", "FIX", "--tier", "S", "--ac", "ok :: true", "--step", "s")
        self.done("T-0001", "2020-01-01T00:00:00Z")
        self.done("T-0001", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        self.view()
        with open(os.path.join(self.p.dir, "cache", "closes.json"), encoding="utf-8") as f:
            kept = json.load(f)
        self.assertEqual(kept[os.path.join(self.p.dir, "ledger.jsonl")]["rows"],
                         [[time.strftime("%Y-%m-%d", time.gmtime()), "T-0001"]])

    def test_the_slow_band_lines_stand_longer_than_the_pane_polls(self):
        os.makedirs(os.path.join(self.p.dir, "cache"), exist_ok=True)
        with open(os.path.join(self.p.dir, "cache", "learned-line.json"), "w") as f:  # ~2.5 s CPU to count a week
            json.dump({"at": time.time() - 300, "value": "kept: 3 lessons this week"}, f)
        self.assertEqual(self.view()["learned"], "kept: 3 lessons this week")
