"""T-0167: fm run --parallel N runs independent queued tasks at once, each in its own lane (a fresh session, its own
files), then integrates them one by one: rebase on the main branch, gates, fast-forward, lane removed. Tasks that could
collide run one at a time, and a lane that can't be merged cleanly is kept with the reason. A stub claude stands in."""
import json
import os
import subprocess
import sys
import unittest

from helpers import FM, ForemanTestCase


class Parallel(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.uhome, self.bin, self.log = (os.path.join(self.tmp, x) for x in ("uhome", "bin", "sessions.log"))
        os.makedirs(self.uhome)
        os.makedirs(self.bin)
        fm = f"{sys.executable} {FM}"
        # a session that finishes its task in whatever checkout it was started in, slowly enough to overlap another
        self.stub("claude", f't=$FOREMAN_DRIVE_TASK\necho "start $t $PWD" >> {self.log}\n{fm} focus $t >/dev/null\n'
                            f'echo "$t" > "work-$t.txt"\nsleep 1\n'
                            f'{fm} task finish $t --run true --audit "self check" --commit "work $t" >/dev/null\n'
                            f'echo "end $t" >> {self.log}\n')
        self.fm("init")

    def stub(self, name, body):
        path = os.path.join(self.bin, name)
        with open(path, "w") as f:
            f.write("#!/usr/bin/env bash\n" + body)
        os.chmod(path, 0o755)

    def env(self):
        return {"HOME": self.uhome, "PATH": self.bin + os.pathsep + os.environ["PATH"]}

    def task(self, title, scope):
        self.fm("task", "new", title, "--type", "FEATURE", "--tier", "S", "--scope", scope, "--ac", "ok :: true",
                "--step", "a")

    def sessions(self):
        return open(self.log).read().splitlines() if os.path.exists(self.log) else []

    def git(self, *args):
        return subprocess.run(["git", "-C", self.repo, *args], capture_output=True, text=True).stdout

    def overlapped(self):
        return [ln.split()[0] for ln in self.sessions()][:2] == ["start", "start"]

    def test_disjoint_tasks_run_together_and_are_merged(self):
        self.task("one", "work-T-0001.txt")
        self.task("two", "work-T-0002.txt")
        out = self.fm("run", "--parallel", "2", env=self.env()).stdout
        self.assertTrue(self.overlapped(), self.sessions())
        self.assertTrue(all(".lanes/" in ln for ln in self.sessions() if ln.startswith("start")), "each in its lane")
        log = self.git("log", "--format=%s")
        self.assertIn("work T-0001", log)
        self.assertIn("work T-0002", log)
        self.assertTrue(os.path.exists(os.path.join(self.repo, "work-T-0002.txt")))
        self.assertEqual(self.git("worktree", "list").count("\n"), 1, "the lanes are removed")
        self.assertIn("merged", out)

    def serving(self):
        from sampo import core as c
        c.update_meta(c.find_project(self.repo), serve={"unit": "foreman-serve-x.service", "mode": None})

    def test_beside_a_serving_session_even_one_task_runs_in_a_lane(self):
        # T-6399: the Remote Control session owns the main checkout, so the runner works beside it only in lanes
        self.serving()
        self.task("one", "work-T-0001.txt")
        out = self.fm("run", env=self.env()).stdout
        starts = [ln for ln in self.sessions() if ln.startswith("start")]
        self.assertEqual(len(starts), 1, self.sessions())
        self.assertIn(".lanes/", starts[0], "never the main checkout the serving session works in")
        self.assertIn("work T-0001", self.git("log", "--format=%s"))
        self.assertIn("merged", out)

    def test_beside_a_serving_session_a_measured_run_is_refused(self):
        self.serving()
        self.task("one", "work-T-0001.txt")
        p = self.fm("run", "--max-usd", "5", check=False, env=self.env())
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("lanes", p.stdout + p.stderr)
        self.assertEqual(self.sessions(), [], "a measured run works the main checkout, so none starts")

    def test_tasks_that_could_collide_run_one_at_a_time(self):
        self.task("one", "work-*")
        self.task("two", "work-*")  # the same files
        self.fm("run", "--parallel", "2", env=self.env())
        self.assertFalse(self.overlapped(), self.sessions())
        self.assertIn("work T-0002", self.git("log", "--format=%s"))

    def test_a_dirty_main_checkout_keeps_the_lanes(self):
        self.task("one", "work-T-0001.txt")
        self.task("two", "work-T-0002.txt")
        with open(os.path.join(self.repo, "README.md"), "a") as f:
            f.write("my own edit\n")  # the user's uncommitted work
        out = self.fm("run", "--parallel", "2", env=self.env()).stdout
        self.assertNotIn("work T-0001", self.git("log", "--format=%s"))
        self.assertIn("kept", out)
        self.assertIn("uncommitted", out)
        self.assertEqual(self.git("worktree", "list").count("\n"), 3, "both lanes kept with their commits")

    def test_a_dependency_or_an_l_task_never_shares_a_batch(self):
        from sampo import core as c
        from sampo import serve
        self.task("base", "a.txt")                                                        # T-0001
        self.task("needs base", "b.txt")                                                  # T-0002
        self.fm("task", "set", "T-0002", "depends_on=T-0001")
        self.fm("task", "new", "big", "--type", "FEATURE", "--tier", "L", "--scope", "c.txt", "--ac", "ok :: true",
                "--step", "a", "--interpretation", "Reading 1: a. Reading 2: b.", "--approach", "y")              # T-0003
        self.task("free", "d.txt")                                                        # T-0004
        p = c.find_project(self.repo)
        self.assertEqual([b.id for b in serve._batch(p, c.find_brief(p, "T-0001"), set(), 3)], ["T-0001", "T-0004"])

    def test_same_group_never_batches(self):
        # T-0445: fm relate groups related tasks; two of one group in parallel lanes step on each other's work
        from sampo import core as c
        from sampo import serve
        for t, f in (("one", "a.txt"), ("two", "b.txt"), ("three", "c.txt")):
            self.task(t, f)
        p = c.find_project(self.repo)
        for t in ("T-0001", "T-0002"):
            b = c.find_brief(p, t)
            b.meta["group"] = "g1"
            c.save_brief(p, b)
        self.assertEqual([b.id for b in serve._batch(p, c.find_brief(p, "T-0001"), set(), 3)], ["T-0001", "T-0003"])

    def test_cochange_footprint_blocks_batch(self):
        # T-0445: files that nearly always change together (fm map's pairs): a task on one will touch the other
        from unittest import mock
        from sampo import core as c
        from sampo import serve
        for t, f in (("api", "src/api.py"), ("client", "src/client.py"), ("docs", "README.md")):
            self.task(t, f)
        p = c.find_project(self.repo)
        with mock.patch.object(serve, "_pairs", return_value={"src/api.py": ["src/client.py"]}, create=True):
            ids = [b.id for b in serve._batch(p, c.find_brief(p, "T-0001"), set(), 3)]
        self.assertEqual(ids, ["T-0001", "T-0003"])

    def test_scopes_are_compared_as_paths(self):
        from sampo import serve
        for a, b in (("./a.txt", "a.txt"), ("src//x.py", "src/x.py"), ("src/{a,b}.py", "src/a.py"), ("plugin/", "plugin/x")):
            self.assertTrue(serve._overlap(a, b), (a, b))  # review: the same file in another spelling
        self.assertFalse(serve._overlap("src/a.py", "lib/a.py"))

    def test_a_lane_that_cant_be_made_is_skipped_not_retried_forever(self):
        self.task("one", "work-T-0001.txt")
        self.task("two", "work-T-0002.txt")
        subprocess.run(["git", "-C", self.repo, "worktree", "add", "-q", "-b", "foreman/T-0001",
                        os.path.join(self.tmp, "elsewhere")], check=True)  # its branch is checked out already
        out = self.fm("run", "--parallel", "2", env=self.env()).stdout  # returns (helpers time out at 30 s)
        self.assertIn("T-0001: no lane", out)
        self.assertIn("T-0002 done", out)

    def test_a_usage_limit_stops_new_lanes(self):
        self.task("one", "work-T-0001.txt")
        self.task("two", "work-T-0002.txt")
        self.task("three", "work-T-0003.txt")
        body = open(os.path.join(self.bin, "claude")).read().split("\n", 1)[1]
        self.stub("claude", 'if [ "$FOREMAN_DRIVE_TASK" = T-0002 ]; then echo "You\'ve hit your limit"; exit 1; fi\n' + body)
        out = self.fm("run", "--parallel", "2", "--wait", "0", env=self.env(), check=False).stdout
        self.assertIn("usage limit hit", out)
        self.assertIn("T-0002 not finished in its lane", out)
        self.assertEqual(len([ln for ln in self.sessions() if ln.startswith("start")]), 2, "T-0003 ran alone, after")

    def timed_stub(self, naps):
        """The default session, but napping `naps[task]` seconds (default 1) so who ends first is fixed."""
        body = open(os.path.join(self.bin, "claude")).read().split("\n", 1)[1]
        cases = "".join(f"{t}) sleep {s};; " for t, s in naps.items())
        self.stub("claude", f'case $FOREMAN_DRIVE_TASK in {cases}*) sleep 1;; esac\n' + body)

    def test_a_pool_starts_the_next_task_as_soon_as_any_lane_ends(self):
        # T-3294: not batches of N that wait for the slowest: T-0003 starts when T-0001 ends, T-0002 still running
        for i in (1, 2, 3):
            self.task(f"t{i}", f"work-T-000{i}.txt")
        self.timed_stub({"T-0001": 1, "T-0002": 6})
        out = self.fm("run", "--parallel", "2", env=self.env()).stdout
        marks = [(ln.split()[0], ln.split()[1]) for ln in self.sessions()]
        self.assertLess(marks.index(("start", "T-0003")), marks.index(("end", "T-0002")), marks)
        self.assertLess(marks.index(("end", "T-0001")), marks.index(("start", "T-0003")), marks)
        log = self.git("log", "--format=%s")
        for i in (1, 2, 3):
            self.assertIn(f"work T-000{i}", log, out)
        self.assertEqual(self.git("worktree", "list").count("\n"), 1, "every lane merged and removed")

    def test_the_pool_is_capped_by_lane_max_not_three(self):
        from sampo import core as c
        from sampo import serve
        p = c.find_project(self.repo)
        self.assertEqual(serve._pool_cap(p), 3, "unchanged until the user raises it")
        self.fm("lane", "max", "4")
        self.assertEqual(serve._pool_cap(p), 4)
        for i in (1, 2, 3, 4):
            self.task(f"t{i}", f"work-T-000{i}.txt")
        self.timed_stub({f"T-000{i}": 3 for i in (1, 2, 3, 4)})
        self.fm("run", "--parallel", "9", env=self.env())
        self.assertEqual([ln.split()[0] for ln in self.sessions()][:4], ["start"] * 4, "four lanes at once")

    def test_scopes_that_share_only_a_changelog_run_together(self):
        # T-3294: append-only hub files merge by union; any other shared file still keeps two tasks apart
        from sampo import core as c
        from sampo import serve
        with open(os.path.join(self.repo, ".gitattributes"), "w") as f:
            f.write("CHANGELOG.md merge=union\n")  # only then does git merge it for us
        for title, scopes in (("a", ("a.txt", "CHANGELOG.md")),("b", ("b.txt", "CHANGELOG.md")),
                              ("c", ("c.txt", "changelog.d/")), ("d", ("a.txt", "d.txt"))):
            self.fm("task", "new", title, "--type", "FEATURE", "--tier", "S", *[x for s in scopes for x in ("--scope", s)],
                    "--ac", "ok :: true", "--step", "a")
        p = c.find_project(self.repo)
        ids = [b.id for b in serve._batch(p, c.find_brief(p, "T-0001"), set(), 4)]
        self.assertEqual(ids, ["T-0001", "T-0002", "T-0003"], "T-0004 shares a.txt with T-0001")
        busy = [c.find_brief(p, "T-0001")]  # a pool pick against a lane already running
        self.assertEqual([b.id for b in serve._batch(p, c.find_brief(p, "T-0002"), set(), 3, busy)], ["T-0002", "T-0003"])
        self.assertEqual([b.id for b in serve._batch(p, c.find_brief(p, "T-0004"), set(), 3, busy)], ["T-0002", "T-0003"],
                         "a head that clashes with a running lane is passed over, not forced")
        every = {"T-0001", "T-0002", "T-0003"}
        self.assertEqual(serve._batch(p, c.find_brief(p, "T-0004"), every, 3, [c.find_brief(p, t) for t in every]), [])

    def test_wide_scope_never_holds_the_pool(self):
        # T-6412: a task scoping a whole big folder overlapped every queued task: one lane ran, five slots sat empty
        from sampo import core as c
        from sampo import serve
        os.makedirs(os.path.join(self.repo, "lib"))
        for i in range(serve.WIDE_ENTRIES + 1):
            open(os.path.join(self.repo, "lib", f"m{i}.py"), "w").close()
        for title, scope in (("wide", "lib"), ("a", "lib/m1.py"), ("b", "lib/m2.py"), ("c", "x.txt")):
            self.task(title, scope)
        p = c.find_project(self.repo)
        wide, a = c.find_brief(p, "T-0001"), c.find_brief(p, "T-0002")
        ids = lambda bs: [b.id for b in bs]
        self.assertEqual(ids(serve._batch(p, wide, set(), 3, [])), ["T-0002", "T-0003", "T-0004"],
                         "narrow tasks start first; a wide head waits")
        self.assertEqual(ids(serve._batch(p, a, set(), 3, [a])), ["T-0003", "T-0004"],
                         "a passed-over wide task never blocks the narrow ones after it")
        self.assertEqual(ids(serve._batch(p, wide, {"T-0002", "T-0003", "T-0004"}, 3, [])), ["T-0001"],
                         "with nothing running and nothing narrow left, the wide task starts alone")
        self.assertEqual(ids(serve._batch(p, a, set(), 3, [wide])), ["T-0004"], "a running wide lane still blocks what it covers")

    def test_changelog_is_a_hub_only_where_git_merges_it_by_union(self):
        from sampo import core as c
        from sampo import serve
        p = c.find_project(self.repo)
        self.assertEqual(serve._hubs(p), ("changelog.d",), "a user's repo without merge=union: two edits would conflict")
        with open(os.path.join(self.repo, ".gitattributes"), "w") as f:
            f.write("CHANGELOG.md merge=union\n")
        self.assertEqual(serve._hubs(p), ("changelog.d", "CHANGELOG.md"))

    def test_lane_max_up_to_100_sets_the_pool_cap(self):
        from sampo import core as c
        from sampo import serve
        p = c.find_project(self.repo)
        self.fm("lane", "max", "100")
        self.assertEqual(serve._pool_cap(p), 100)
        self.assertNotEqual(self.fm("lane", "max", "101", check=False).returncode, 0)
        self.assertEqual(serve._pool_cap(p), 100, "a refused number changes nothing")

    def run_pool(self, integrate, n=2, left=3, skip=None, new_lane=None):
        """serve._run_pool in-process on the stub claude, with `integrate` standing in for the merge. (finished, out)"""
        import contextlib
        import io
        from types import SimpleNamespace
        from unittest import mock
        from sampo import core as c
        from sampo import serve
        p = c.find_project(self.repo)
        buf = io.StringIO()
        with contextlib.ExitStack() as st:
            st.enter_context(mock.patch.object(serve, "_claude_cmd", lambda *a: [os.path.join(self.bin, "claude")]))
            st.enter_context(mock.patch.object(serve, "_integrate", integrate))
            if new_lane:
                st.enter_context(mock.patch.object(serve, "_new_lane", new_lane))
            st.enter_context(contextlib.redirect_stdout(buf))
            res = serve._run_pool(p, SimpleNamespace(timeout=1), {}, os.path.join(self.tmp, "run.log"),
                                  set() if skip is None else skip, set(), False, n, left)
        return res[0], buf.getvalue()

    def slow_merge(self, secs=2):
        import time

        def integrate(p, tid, path, timing=None):  # T-6851: the runner passes its phase timing
            time.sleep(secs)
            with open(self.log, "a") as f:
                f.write(f"merge-end {tid}\n")
            return "merged into main"
        return integrate

    def order(self):
        return [tuple(ln.split()[:2]) for ln in self.sessions()]

    def test_a_task_on_the_same_files_waits_for_the_pending_merge(self):
        # review: a lane waiting to merge still holds its files; the next task would start on a stale base and conflict
        self.task("one", "work-*")
        self.task("two", "work-*")
        done, out = self.run_pool(self.slow_merge())
        marks = self.order()
        self.assertLess(marks.index(("merge-end", "T-0001")), marks.index(("start", "T-0002")), marks)
        self.assertEqual(done, 2, out)

    def test_a_dependent_waits_for_the_merge_not_just_the_session(self):
        # review: done is not landed; a dependent started when its dependency's session finished, before it merged
        self.task("base", "work-T-0001.txt")
        self.task("needs base", "work-T-0002.txt")
        self.fm("task", "set", "T-0002", "depends_on=T-0001")
        done, out = self.run_pool(self.slow_merge())
        marks = self.order()
        self.assertLess(marks.index(("merge-end", "T-0001")), marks.index(("start", "T-0002")), marks)
        self.assertEqual(done, 2, out)

    def test_no_new_lane_while_the_unmerged_ones_are_at_the_cap(self):
        from unittest import mock
        from sampo import lanes as slanes
        for i in (1, 2, 3):
            self.task(f"t{i}", f"work-T-000{i}.txt")
        self.timed_stub({"T-0002": 6})
        with mock.patch.object(slanes, "UNMERGED_MAX", 1):
            self.run_pool(self.slow_merge(3), left=3)
        marks = self.order()
        self.assertLess(marks.index(("merge-end", "T-0001")), marks.index(("start", "T-0003")), marks)

    def test_a_loaded_host_delays_a_task_it_never_skips_it(self):
        from types import SimpleNamespace
        from unittest import mock
        from sampo import serve
        self.task("one", "work-T-0001.txt")
        refusals = []

        def new_lane(p, tid):
            if len(refusals) < 2:
                refusals.append(tid)
                return SimpleNamespace(returncode=1, stdout="", stderr="host is loaded: no new lane until it eases")
            return real(p, tid)
        real, skip = serve._new_lane, set()
        with mock.patch.object(serve, "LOAD_RETRY_S", 0.1):
            done, out = self.run_pool(self.slow_merge(0), n=2, left=1, skip=skip, new_lane=new_lane)
        self.assertEqual((done, skip, len(refusals)), (1, set(), 2), out)

    def test_a_host_that_stays_loaded_ends_the_run_with_the_task_still_queued(self):
        from types import SimpleNamespace
        from unittest import mock
        from sampo import serve
        self.task("one", "work-T-0001.txt")
        skip = set()
        busy = lambda p, tid: SimpleNamespace(returncode=1, stdout="", stderr="host is loaded: no new lane until it eases")
        with mock.patch.object(serve, "LOAD_RETRY_S", 0.05), mock.patch.object(serve, "LOAD_TRIES", 3):
            with self.assertRaises(SystemExit) as e:
                self.run_pool(self.slow_merge(0), skip=skip, new_lane=busy)
        self.assertIn("stayed loaded", str(e.exception))
        self.assertEqual(skip, set())

    def test_a_merge_that_raises_is_kept_and_the_others_go_on(self):
        # review: one bad merge must not kill every session; a task that waits on it is held, and only landed ones count
        for i, t in enumerate(("one", "two", "needs one"), 1):
            self.task(t, f"work-T-000{i}.txt")
        self.fm("task", "set", "T-0003", "depends_on=T-0001")
        good = self.slow_merge(0)

        def integrate(p, tid, path, timing=None):
            if tid == "T-0001":
                raise RuntimeError("boom")
            return good(p, tid, path)
        skip = set()
        done, out = self.run_pool(integrate, left=3, skip=skip)
        self.assertEqual(done, 1, out)
        self.assertIn("T-0001 done; kept in", out)
        self.assertIn("boom", out)
        self.assertIn("T-0003", skip)
        self.assertNotIn(("start", "T-0003"), self.order())

    def test_a_head_blocked_by_a_running_lane_doesnt_starve_behind_a_later_task(self):
        # nit: T-0002 clashes with the running T-0001; T-0003 clashes with T-0002 only: T-0003 must not jump the queue
        from sampo import core as c
        from sampo import serve
        self.fm("task", "new", "a", "--type", "FEATURE", "--tier", "S", "--scope", "a.txt", "--ac", "ok :: true", "--step", "a")
        self.fm("task", "new", "ab", "--type", "FEATURE", "--tier", "S", "--scope", "a.txt", "--scope", "b.txt",
                "--ac", "ok :: true", "--step", "a")
        self.task("b", "b.txt")
        self.task("c", "c.txt")
        p = c.find_project(self.repo)
        ids = [b.id for b in serve._batch(p, c.find_brief(p, "T-0002"), {"T-0001"}, 3, [c.find_brief(p, "T-0001")])]
        self.assertEqual(ids, ["T-0004"], "T-0003 shares b.txt with the waiting T-0002; T-0004 is free of both")

    def test_a_failure_mid_batch_stops_every_session_and_frees_its_tasks(self):
        # session audit: sessions run in their own process group, so an error or Ctrl-C while waiting left them
        # running unwatched, and the lanes kept holding their tasks
        from sampo import core as c
        from sampo import serve
        from types import SimpleNamespace
        from unittest import mock
        self.task("one", "work-T-0001.txt")
        self.task("two", "work-T-0002.txt")
        pids = os.path.join(self.tmp, "pids")
        # T-1201: on a loaded machine the slow session hadn't written its pid when the quick one ended the batch
        self.stub("quick", f"for i in $(seq 200); do [ -s {pids} ] && break; sleep 0.1; done\nsleep 1\n")
        self.stub("slow", f"echo $$ >> {pids}\nsleep 30\n")
        p = c.find_project(self.repo)
        pool = lambda log: serve._run_pool(p, SimpleNamespace(timeout=1), {}, log, set(), set(), False, 2, 2)  # T-3294
        lanes_freed = lambda: (self.assertEqual(self.git("worktree", "list").count("\n"), 1),
                               self.assertFalse([t for t in ("T-0001", "T-0002") if c.find_brief(p, t).meta.get("lane")]))
        with mock.patch.object(serve, "_claude_cmd", lambda *a, it=iter(["quick", "slow"]): [os.path.join(self.bin, next(it))]):
            with self.assertRaises(IsADirectoryError):  # the run log can't be written once the first session ends
                pool(self.tmp)
        pid = int(open(pids).read().split()[0])
        self.assertRaises(ProcessLookupError, os.kill, pid, 0)
        lanes_freed()
        with mock.patch.object(serve, "_claude_cmd", lambda *a, it=iter(["slow", "missing"]): [os.path.join(self.bin, next(it))]):
            with self.assertRaises(FileNotFoundError):  # claude can't be started for the second
                pool(os.path.join(self.tmp, "run.log"))
        lanes_freed()

    def test_a_lane_with_no_progress_is_blocked(self):
        # T-0968: a lane session that changes nothing is quarantined like a single session (T-0955), not retried
        # by every later run
        from sampo import core as c
        self.task("one", "work-T-0001.txt")
        self.task("two", "work-T-0002.txt")
        self.stub("claude", f'echo "start $FOREMAN_DRIVE_TASK $PWD" >> {self.log}\nsleep 1\n')  # does nothing
        self.fm("run", "--parallel", "2", env=self.env(), check=False)
        p = c.find_project(self.repo)
        for t in ("T-0001", "T-0002"):
            self.assertEqual(c.find_brief(p, t).status, "blocked", t)
            self.assertIn("no progress", c.find_brief(p, t).section("Log"))
        before = len(self.sessions())
        self.fm("run", "--parallel", "2", env=self.env(), check=False)
        self.assertEqual(len(self.sessions()), before, "the next run starts nothing")

    def test_a_lane_kept_with_work_is_resumed_in_the_same_worktree(self):
        # T-6442: a session that ends unfinished but leaves work (a timeout, a stall) is resumed once in its own lane by
        # the run, not left for a hand lane recover; the resumed session sees the earlier work and finishes
        from sampo import core as c
        self.task("one", "work-T-0001.txt")
        self.serving()  # one task runs in a lane only beside serve
        fm = f"{sys.executable} {FM}"
        self.stub("claude", f't=$FOREMAN_DRIVE_TASK\necho "start $t $PWD" >> {self.log}\n{fm} focus $t >/dev/null\n'
                            f'if [ ! -e "work-$t.txt" ]; then echo half > "work-$t.txt"; exit 0; fi\n'
                            f'{fm} task finish $t --run true --audit "self check" --commit "work $t" >/dev/null\n')
        out = self.fm("run", "--parallel", "2", env=self.env(), check=False).stdout
        starts = [ln.split()[2] for ln in self.sessions() if ln.startswith("start T-0001")]
        self.assertEqual(len(starts), 2, out)
        self.assertEqual(starts[0], starts[1], "resumed in the same lane")
        self.assertIn("resumed in its lane", out)
        p = c.find_project(self.repo)
        self.assertEqual(c.find_brief(p, "T-0001").status, "done", out)
        self.assertIn("work T-0001", self.git("log", "--format=%s"))
        self.assertEqual(len(c.find_brief(p, "T-0001").meta.get("recoveries") or []), 1)

    def test_a_lane_that_never_finishes_is_resumed_up_to_the_cap_across_runs(self):
        # T-6442: once per run, and a later run resumes a lane an earlier one kept; lanes.RECOVERIES_MAX caps it
        from sampo import core as c
        from sampo import lanes as slanes
        self.task("one", "work-T-0001.txt")
        self.serving()
        self.stub("claude", f't=$FOREMAN_DRIVE_TASK\necho "start $t $PWD" >> {self.log}\n'
                            f'{sys.executable} {FM} focus $t >/dev/null\necho more >> "work-$t.txt"\n')
        for _ in range(3):
            self.fm("run", "--parallel", "2", env=self.env(), check=False)
        starts = [ln for ln in self.sessions() if ln.startswith("start T-0001")]
        self.assertEqual(len(starts), 1 + slanes.RECOVERIES_MAX, starts)
        p = c.find_project(self.repo)
        self.assertEqual(len(c.find_brief(p, "T-0001").meta.get("recoveries") or []), slanes.RECOVERIES_MAX)
        self.assertTrue(os.path.isdir(starts[0].split()[2]), "the lane and its work are kept")

    def test_a_lane_kept_before_run_kept_is_found_by_the_run_log(self):
        # T-6444: lanes an older run kept carry no run_kept; the run's own session log header names them
        from sampo import core as c
        self.task("one", "work-T-0001.txt")
        self.serving()
        path = json.loads(self.fm("lane", "new", "T-0001", "--json").stdout)["path"]
        with open(os.path.join(path, "work-T-0001.txt"), "w") as f:
            f.write("half\n")
        p = c.find_project(self.repo)
        os.makedirs(os.path.join(c.state_dir(), "logs"), exist_ok=True)
        with open(os.path.join(c.state_dir(), "logs", f"run-{p.slug}.log"), "a") as f:
            f.write(f"== 2026-01-01T00:00:00Z T-0001 (lane {path}) exit None\nsession text\n")
        self.stub("claude", f't=$FOREMAN_DRIVE_TASK\necho "start $t $PWD" >> {self.log}\n')
        out = self.fm("run", "--parallel", "2", env=self.env(), check=False).stdout
        starts = [ln.split()[2] for ln in self.sessions() if ln.startswith("start T-0001")]
        self.assertTrue(starts and starts[0] == path, out)
        self.assertIn("resuming in its lane", out)

    def test_a_committed_mirror_doesnt_count_as_the_users_changes(self):
        # T-0174 review: fm keeps a tracked .sampo/ current from every lane session, in the main checkout
        self.fm("sync", "on")
        subprocess.run(["git", "-C", self.repo, "add", ".sampo"], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", "mirror"], check=True)
        self.task("one", "work-T-0001.txt")
        self.task("two", "work-T-0002.txt")
        out = self.fm("run", "--parallel", "2", env=self.env()).stdout
        self.assertIn("work T-0002", self.git("log", "--format=%s"), out)

    def test_main_moving_during_the_gates_keeps_the_lane(self):
        # session audit: main was checked once, before a gate run of up to an hour; a branch switch meanwhile got the merge
        from sampo import core as c
        from sampo import serve
        self.task("one", "work-T-0001.txt")
        path = json.loads(self.fm("lane", "new", "T-0001", "--json").stdout)["path"]
        with open(os.path.join(path, "work-T-0001.txt"), "w") as f:
            f.write("x\n")
        subprocess.run(["git", "-C", path, "add", "work-T-0001.txt"], check=True)
        subprocess.run(["git", "-C", path, "commit", "-qm", "work T-0001"], check=True)
        move = os.path.join(self.tmp, "move-main.sh")  # outside both trees: T-4033 points a lane's own main paths at the lane
        with open(move, "w") as f:
            f.write(f"git -C {self.repo} checkout -q -b other\n")
        self.fm("check", "add", f"sh {move}")
        how = serve._integrate_lane(c.find_project(self.repo), "T-0001", path)
        self.assertIn("kept", how)
        self.assertNotIn("work T-0001", self.git("log", "other", "--format=%s"))


class IntegrateHolds(ForemanTestCase):
    """T-4785 (b): fm serve's integrate holds a lane the train would hold: an invented private term in its commits or
    files, a file the guard refuses, a git call or scan that fails, a term list that can't be read. A clean lane still lands."""
    TERM = "zorblat-private.example"  # invented: never a real term

    def setUp(self):
        super().setUp()
        self.fm("init")
        self.fm("task", "new", "one", "--type", "FEATURE", "--tier", "S", "--scope", "x.py", "--ac", "ok :: true", "--step", "a")

    def git(self, *args, cwd=None):
        return subprocess.run(["git", "-C", cwd or self.repo, *args], capture_output=True, text=True).stdout

    def lane(self, name="x.py", text="A = 1\n"):
        path = json.loads(self.fm("lane", "new", "T-0001", "--json").stdout)["path"]
        os.makedirs(os.path.dirname(os.path.join(path, name)), exist_ok=True)
        with open(os.path.join(path, name), "w") as f:
            f.write(text)
        self.git("add", name, cwd=path)
        self.git("commit", "-qm", "work T-0001", cwd=path)
        self.stamp_review("T-0001")  # T-5968: reviewed at that tip (serve integrates only a lane its review covers)
        return path

    def integrate(self, path):
        from sampo import core as c
        from sampo import serve
        return serve._integrate_lane(c.find_project(self.repo), "T-0001", path)

    def held(self, how, path, name="x.py"):
        self.assertIn("kept in", how)
        self.assertNotIn("zorblat", how, "the term is never printed")
        self.assertFalse(os.path.exists(os.path.join(self.repo, name)), "nothing reached the trunk")
        self.assertTrue(os.path.isdir(path), "the lane is kept")

    def test_a_clean_lane_still_lands(self):
        how = self.integrate(self.lane())
        self.assertIn("merged into", how)
        self.assertTrue(os.path.exists(os.path.join(self.repo, "x.py")))

    def test_a_lane_with_no_review_at_its_tip_is_kept_not_landed(self):  # T-5968
        from sampo import core as c
        path = self.lane()
        p = c.find_project(self.repo)
        b = c.find_brief(p, "T-0001")
        b.meta.pop("accepted_tip")  # the builder only returned
        c.save_brief(p, b)
        how = self.integrate(path)
        self.held(how, path)
        self.assertIn("not reviewed at this tip", how)
        with open(os.path.join(path, "more.py"), "w") as f:  # a tip that moved after the accept is held again
            f.write("B = 2\n")
        self.git("add", "more.py", cwd=path)
        self.git("commit", "-qm", "after the accept", cwd=path)
        b = c.find_brief(p, "T-0001")
        b.meta["accepted_tip"] = self.git("rev-parse", "HEAD~1", cwd=path).strip()
        c.save_brief(p, b)
        self.assertIn("not reviewed at this tip", self.integrate(path))
        self.assertFalse(os.path.exists(os.path.join(self.repo, "more.py")))

    def test_a_commit_added_between_the_review_check_and_the_merge_is_held(self):
        # T-5968 review: the review was read on the pre-rebase tip; a commit added while the gates ran rode the merge
        path = self.lane()
        add = os.path.join(self.tmp, "add-commit.sh")  # outside both trees
        with open(add, "w") as f:
            f.write(f"echo 'EVIL = 1' > {path}/evil.py\ngit -C {path} add evil.py\ngit -C {path} commit -qm late\n")
        self.fm("check", "add", f"sh {add}")
        how = self.integrate(path)
        self.held(how, path)
        self.assertIn("not reviewed at this tip", how)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "evil.py")), "the late commit never reached the trunk")

    def test_a_merge_with_content_of_its_own_added_while_the_gates_ran_is_held(self):  # T-5968 security review 2
        path = self.lane()
        add = os.path.join(self.tmp, "add-merge.sh")
        with open(add, "w") as f:
            f.write(f"cd {path}\nme=$(git rev-parse --abbrev-ref HEAD)\ngit checkout -q -b side HEAD~1\n"
                    "echo S=1 > s.py\ngit add s.py\ngit commit -qm side\ngit checkout -q $me\n"
                    "git merge --no-ff --no-commit side\necho EVIL=1 > evil.py\ngit add evil.py\ngit commit -qm merge\n")
        self.fm("check", "add", f"sh {add}")
        how = self.integrate(path)
        self.held(how, path)
        self.assertIn("not reviewed at this tip", how)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "evil.py")), "the merge's own content never reached the trunk")

    def test_a_rebase_keeps_the_review_of_the_same_changes(self):
        # T-5968: main moved, the lane is rebased (every sha changes); the same patches stay reviewed and land
        path = self.lane()
        with open(os.path.join(self.repo, "other.txt"), "w") as f:
            f.write("o\n")
        self.git("add", "other.txt")
        self.git("commit", "-qm", "main moved")
        how = self.integrate(path)
        self.assertIn("merged into", how)
        self.assertTrue(os.path.exists(os.path.join(self.repo, "x.py")))

    def test_a_lane_that_landed_is_reported_merged_when_removing_its_folder_raises(self):
        # T-4785 re-check: the fast-forward had landed, then `lane rm` raised and _integrate reported the lane "kept"
        from unittest import mock
        from sampo import serve
        path = self.lane()
        real = serve._fm

        def fm(cwd, *args, **k):
            if args[:2] == ("lane", "rm"):
                raise subprocess.TimeoutExpired("sampo lane rm", 600)
            return real(cwd, *args, **k)

        with mock.patch.object(serve, "_fm", fm):
            from sampo import core as c
            how = serve._integrate(c.find_project(self.repo), "T-0001", path)
        self.assertTrue(os.path.exists(os.path.join(self.repo, "x.py")), "it landed")
        self.assertRegex(how, r"^merged into \S+ \(lane folder not removed: TimeoutExpired\)$")
        self.assertNotIn("kept in", how)

    def test_a_lane_carrying_a_private_term_is_held_naming_file_and_line(self):
        self.fm("private", "add", self.TERM)
        path = self.lane(text=f'A = 1\nHOST = "llm.{self.TERM}"\n')
        how = self.integrate(path)
        self.held(how, path)
        self.assertIn("private term", how)
        self.assertRegex(how, r"x\.py:2\b")

    def test_a_lane_whose_file_the_guard_refuses_is_held(self):
        path = self.lane(".git-credentials", "https://invented-user:invented-token@example.test\n")
        how = self.integrate(path)
        self.held(how, path, ".git-credentials")
        self.assertIn("the guard refuses .git-credentials", how)

    def test_a_failing_file_listing_holds_the_lane_not_lands_it_unchecked(self):
        from unittest import mock
        from sampo import serve
        path = self.lane()
        real = subprocess.run

        def run(cmd, *a, **k):
            if isinstance(cmd, list) and "--name-only" in cmd:
                return subprocess.CompletedProcess(cmd, 128, "", "boom: output text")
            return real(cmd, *a, **k)
        with mock.patch.object(serve.subprocess, "run", side_effect=run):
            how = self.integrate(path)
        self.held(how, path)
        self.assertNotIn("boom", how, "git's output is never quoted")

    def test_an_unreadable_term_list_holds_the_lane_and_says_so(self):
        from sampo import core as c
        with open(os.path.join(c.state_dir(), c.PRIVATE_TERMS), "w") as f:
            f.write("{not json")
        path = self.lane()
        how = self.integrate(path)
        self.held(how, path)
        self.assertIn("term list", how)
        self.assertNotIn(c.PRIVATE_TERMS, how, "no path in the line")

    def test_any_error_out_of_the_holds_keeps_the_lane_and_never_ends_the_serve_loop_or_lands(self):
        # T-4785 review: _integrate caught only OSError and SubprocessError; a PolicyError or UsageError out of the shared
        # holds (or any other error) would end fm serve's loop, so a hold that can't run is a hold, with the class named
        from unittest import mock
        from sampo import cli, core as c, lanes, serve
        path = self.lane()
        p = c.find_project(self.repo)
        for err in (c.PolicyError("boom: output text"), cli.UsageError("boom: output text"), ValueError("boom: output text"),
                    KeyError("boom")):
            with mock.patch.object(lanes, "land_holds", side_effect=err):
                how = serve._integrate(p, "T-0001", path)
            self.held(how, path)
            self.assertIn(type(err).__name__, how)
            self.assertNotIn("boom", how, "an error's text is never quoted into the line")
            self.assertEqual(self.git("status", "--porcelain", cwd=path).strip(), "", "the lane is not left half-rebased")


class IntegrateThroughPilot(ForemanTestCase):
    """T-6480: with the pilot on, a run's finished lane goes to sampo lane pilot (review, accept, land train), not to
    serve's own unreviewed hold and hour-long serial gate. Its outcome is what the run reports."""
    git, lane = IntegrateHolds.git, IntegrateHolds.lane  # its helpers, not its tests

    def setUp(self):
        super().setUp()
        self.fm("init")
        self.fm("task", "new", "one", "--type", "FEATURE", "--tier", "S", "--scope", "x.py", "--ac", "ok :: true", "--step", "a")

    def via_pilot(self, answer, rc=0):
        from unittest import mock
        from sampo import core as c
        from sampo import pilotsig, serve
        stub = os.path.join(self.repo, ".stub-pilot.py")
        with open(stub, "w") as f:
            f.write(f"import sys\nprint('log line')\nprint({json.dumps(json.dumps(answer))})\nsys.exit({rc})\n")
        path = self.lane()
        p = c.find_project(self.repo)
        b = c.find_brief(p, "T-0001")
        b.meta.pop("accepted_tip", None)  # unreviewed: serve's own path would hold it
        c.save_brief(p, b)
        with mock.patch.dict(os.environ, {pilotsig.VERB_ENV: stub}), mock.patch.object(pilotsig, "_off", return_value=False):
            return serve._integrate(p, "T-0001", path), path

    def test_an_accepted_lane_is_queued_into_the_train(self):
        how, path = self.via_pilot({"tid": "T-0001", "outcome": "accepted", "rounds": 1, "why": ""})
        self.assertTrue(how.startswith("accepted"), how)
        self.assertIn("land train", how)
        self.assertNotIn("not reviewed", how)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "x.py")), "serve merges nothing itself: the train does")

    def test_the_runs_session_counts_as_its_builders_return_for_the_pilots_gate(self):
        """T-6490: only the SubagentStop hook stamps builder_returned; a run session has none, so the real pilot refused
        every run lane ('its builder has not returned'). The stubbed verb above hid it: this asks the real gate."""
        from sampo import core as c
        from sampo import lanepilot, lanes
        from unittest import mock
        p = c.find_project(self.repo)

        def lane_then_warm():  # T-6496: the run's loop reads finished lanes (a 60 s cache) while the session still works,
            path = json.loads(self.fm("lane", "new", "T-0001", "--json").stdout)["path"]  # before its commit; the
            self.assertNotIn("T-0001", lanes.awaiting_merge(p, c.load_briefs(p)))  # pilot then read that stale answer
            with open(os.path.join(path, "x.py"), "w") as f:
                f.write("A = 1\n")
            self.git("add", "x.py", cwd=path)
            self.git("commit", "-qm", "work T-0001", cwd=path)
            return path
        with mock.patch.dict(os.environ, {"FM_STALL_CACHE_S": "60"}), mock.patch.object(self, "lane", lane_then_warm):
            self.via_pilot({"tid": "T-0001", "outcome": "accepted", "rounds": 1, "why": ""})
            b = c.find_brief(p, "T-0001")
            self.assertTrue(lanes._builder_returned(b), b.meta)
            said = lanepilot.gate(p, b, self.repo)
        self.assertNotIn("has not returned", said)
        self.assertNotIn("not awaiting a merge", said)

    def test_changes_keep_the_lane_with_the_reason(self):
        how, path = self.via_pilot({"tid": "T-0001", "outcome": "changes", "rounds": 1, "why": "edge: off by one"})
        self.assertTrue(how.startswith("kept in"), how)
        self.assertIn("off by one", how)
        self.assertTrue(os.path.isdir(path))

    def test_an_exception_keeps_the_lane(self):
        how, path = self.via_pilot({"tid": "T-0001", "outcome": "exception", "rounds": 0, "why": "gate red"}, rc=1)
        self.assertTrue(how.startswith("kept in"), how)
        self.assertIn("gate red", how)

    def test_pilot_off_keeps_the_serial_path(self):
        from unittest import mock
        from sampo import core as c
        from sampo import pilotsig, serve
        path = self.lane()
        with mock.patch.object(pilotsig, "_off", return_value=True):
            how = serve._integrate(c.find_project(self.repo), "T-0001", path)
        self.assertIn("merged into", how)


class HeldBack(ForemanTestCase):
    """T-6487: an idle pool slot says why the queue fills none, one tally over every queued task."""

    def test_each_queued_task_is_counted_under_what_holds_it(self):
        from sampo import core as c
        from sampo import serve
        self.fm("init")
        self.fm("task", "new", "scoped", "--type", "FEATURE", "--tier", "S", "--scope", "a.py", "--ac", "ok :: true",
                "--step", "a")
        self.fm("task", "new", "unscoped", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true", "--step", "a")
        self.fm("task", "new", "big", "--type", "FEATURE", "--tier", "L", "--scope", "b.py", "--ac", "ok :: true",
                "--step", "a")
        self.fm("task", "new", "same file", "--type", "FEATURE", "--tier", "S", "--scope", "a.py", "--ac", "ok :: true",
                "--step", "a")
        p = c.find_project(self.repo)
        running = [c.find_brief(p, "T-0001")]
        held = serve._held_back(p, set(), running)
        self.assertIn("1 no scope", held)
        self.assertIn("1 L tier", held)
        self.assertIn("1 files overlap a lane", held)
        self.assertNotIn("T-0001", held)
        self.assertIn("1 held this run", serve._held_back(p, {"T-0002"}, running))
        self.assertEqual(serve._held_back(p, set(), []).count("free"), 1, serve._held_back(p, set(), []))


if __name__ == "__main__":
    unittest.main()
