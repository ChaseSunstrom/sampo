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
        batch = [c.find_brief(p, "T-0001"), c.find_brief(p, "T-0002")]
        lanes_freed = lambda: (self.assertEqual(self.git("worktree", "list").count("\n"), 1),
                               self.assertFalse([t for t in ("T-0001", "T-0002") if c.find_brief(p, t).meta.get("lane")]))
        with mock.patch.object(serve, "_claude_cmd", lambda *a, it=iter(["quick", "slow"]): [os.path.join(self.bin, next(it))]):
            with self.assertRaises(IsADirectoryError):  # the run log can't be written once the first session ends
                serve._run_lanes(p, batch, SimpleNamespace(timeout=1), {}, self.tmp)
        pid = int(open(pids).read().split()[0])
        self.assertRaises(ProcessLookupError, os.kill, pid, 0)
        lanes_freed()
        with mock.patch.object(serve, "_claude_cmd", lambda *a, it=iter(["slow", "missing"]): [os.path.join(self.bin, next(it))]):
            with self.assertRaises(FileNotFoundError):  # claude can't be started for the second
                serve._run_lanes(p, batch, SimpleNamespace(timeout=1), {}, os.path.join(self.tmp, "run.log"))
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


if __name__ == "__main__":
    unittest.main()
