"""T-1117: Foreman keeps the suite green itself. A close runs the tests linked to what it changed and refuses a new
failure; a red full-gate run becomes one urgent baseline task; full runs start in the background after closes."""
import json
import os
import re
import shutil
import subprocess
import time
import unittest

from helpers import ForemanTestCase, valid_m_plan

from sampo import core as c

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = "def add(a, b):\n    return a + b\n"
TEST = ("import unittest\nimport sys, os\nsys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))\nimport calc\n\n\n"
        "class Calc(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(calc.add(1, 2), 3)\n")
OTHER = "import unittest\n\n\nclass Other(unittest.TestCase):\n    def test_other(self):\n        pass\n"


class CloseGates(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        os.makedirs(os.path.join(self.repo, "tests"))
        shutil.copy(os.path.join(HERE, "run.py"), os.path.join(self.repo, "tests", "run.py"))
        self.write("calc.py", SRC)
        self.write("tests/test_calc.py", TEST)
        self.write("tests/test_other.py", OTHER)
        self.write(".gitignore", "__pycache__/\n")
        subprocess.run(["git", "-C", self.repo, "add", "-A"], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", "suite"], check=True)
        self.fm("check", "add", "python3 tests/run.py")
        self.fm("check", "affected", "python3 tests/run.py {names}")
        self.p = c.find_project(self.repo)

    def write(self, rel, text):
        with open(os.path.join(self.repo, rel), "w") as f:
            f.write(text)

    def task(self, title="Change add"):
        tid = self.fm_json("task", "new", title, "--type", "FEATURE", "--tier", "S", "--ac", "works :: true", "--step",
                           "s", "--scope", "calc.py")["id"]
        self.fm("focus", tid)
        return tid

    def finish(self, tid, *extra):
        return self.fm("task", "finish", tid, "--audit", "self", "--run", "true", *extra, check=False,
                       env={"FOREMAN_SENTINEL": "off"})

    def events(self, kind):
        return [e for e in c.ledger_events(self.p, (kind,))]

    def test_a_close_that_breaks_a_linked_test_is_refused(self):
        tid = self.task()
        self.write("calc.py", "def add(a, b):\n    return a - b\n")
        r = self.finish(tid)
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn("test_calc", r.stdout + r.stderr)
        self.assertNotEqual(c.find_brief(self.p, tid).status, "done")
        self.write("calc.py", SRC + "\n")
        r = self.finish(tid)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_a_close_runs_no_tests_for_another_tasks_files_its_own_are_none(self):
        # T-1968: a ship task closed while another task's edit to calc.py was uncommitted ran that file's tests
        ship = self.fm_json("task", "new", "Ship it", "--type", "FEATURE", "--tier", "S", "--ac", "works :: true",
                            "--step", "s", "--scope", "README.md")["id"]
        self.fm("focus", ship)
        self.fm("checkpoint")
        other = self.task()
        c.log_event(self.p, "touched", task=other, data={"file": os.path.join(self.repo, "calc.py")})
        self.write("calc.py", "def add(a, b):\n    return a - b\n")  # other's work in progress breaks test_calc
        r = self.finish(ship)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.events("close_affected"), [], "nothing of its own was linked to a test")

    def test_a_failure_already_red_in_the_last_full_run_is_noted_not_refused(self):
        self.write("calc.py", "def add(a, b):\n    return a - b\n")  # broken before the task
        self.fm("check", check=False, env={"FOREMAN_SENTINEL": "off"})
        tid = self.task()
        self.write("calc.py", "def add(a, b):\n    return a - b  # still\n")
        r = self.finish(tid)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("already red", r.stdout + r.stderr)

    def test_a_red_full_run_captures_one_urgent_baseline_task(self):
        self.write("calc.py", "def add(a, b):\n    return a - b\n")
        out = self.fm("check", check=False).stdout
        base = [b for b in c.load_briefs(self.p) if b.meta.get("baseline")]
        self.assertEqual(len(base), 1, out)
        self.assertEqual(base[0].type, "FIX")
        self.assertEqual(base[0].priority, "urgent")
        self.assertIn("test_calc", base[0].title + base[0].section("Raw request"))
        self.assertIn(base[0].id, out)
        self.fm("check", check=False)
        self.assertEqual(len([b for b in c.load_briefs(self.p) if b.meta.get("baseline")]), 1, "once per failing set")
        self.write("calc.py", SRC)
        self.assertIn(f"baseline is green again: {base[0].id}", self.fm("check").stdout)

    def test_a_known_flake_is_not_a_baseline(self):
        self.write("calc.py", "def add(a, b):\n    return a - b\n")
        out = self.fm("check", check=False).stdout
        names = [n for r in self.events("check_run")[-1]["data"]["results"] for n in r.get("failing") or []]
        for b in c.load_briefs(self.p):
            if b.meta.get("baseline"):
                self.fm("task", "drop", b.id, "reset for the flake case")
        with open(os.path.join(self.p.dir, "flakes.json"), "w") as f:
            json.dump({"pending": {}, "flaky": {n: {"seen": 2} for n in names}}, f)
        self.fm("check", check=False)
        self.assertEqual([b for b in c.load_briefs(self.p) if b.meta.get("baseline") and b.status != "dropped"], [], out)

    def test_a_tasks_own_red_mid_work_is_not_a_baseline(self):
        # a task writing its failing test first runs fm check red: that's its work, not a broken baseline
        self.task()
        self.write("calc.py", "def add(a, b):\n    return a - b\n")
        self.fm("check", check=False)
        self.assertEqual([b for b in c.load_briefs(self.p) if b.meta.get("baseline")], [])

    def test_a_gate_that_names_no_failing_test_is_left_to_say_so_itself(self):
        self.fm("check", "add", "false")
        self.fm("check", check=False)
        self.assertEqual([b for b in c.load_briefs(self.p) if b.meta.get("baseline")], [])

    def test_a_close_is_not_judged_by_its_own_earlier_red_run(self):
        # review: the task's own red fm check was read as "the last full run", so its own break closed with a note
        tid = self.task()
        self.write("calc.py", "def add(a, b):\n    return a - b\n")
        self.fm("check", check=False, env={"FOREMAN_SENTINEL": "off"})
        r = self.finish(tid)
        self.assertNotEqual(r.returncode, 0, r.stdout)

    def test_a_slow_close_names_its_slowest_run(self):
        tid = self.task()
        self.write("calc.py", SRC + "# touch\n")
        env = {"FOREMAN_SENTINEL": "off", "FOREMAN_SLOW_CLOSE_S": "0.5"}
        r = self.fm("task", "finish", tid, "--audit", "self", "--run", "sleep 1", check=False, env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        # train 1115k: at load 28 the linked tests took 2 s and outlasted the sleep; the line names whichever was slowest
        self.assertRegex(r.stdout, r"Slowest: (?:sleep 1|python3 tests/run\.py \S+) \d+ s \(of \d+ s\)")
        self.assertIn("s", self.events("close_affected")[-1]["data"])

    def test_a_flaky_linked_test_gets_one_rerun(self):
        tid = self.task()
        flag = os.path.join(self.repo, "once")
        self.write("tests/test_flaky.py", "import os, unittest\n\n\nclass F(unittest.TestCase):\n    def test_f(self):\n"
                   f"        if not os.path.exists({flag!r}):\n            open({flag!r}, 'w').close()\n"
                   "            self.fail('first run')\n")
        self.write("calc.py", SRC + "# touch\n")
        self.write(".gitignore", "__pycache__/\nonce\n")
        self.fm("task", "set", tid, "scope=calc.py,tests/test_flaky.py,.gitignore")
        r = self.finish(tid)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_json_output_stays_json(self):
        self.write("calc.py", "def add(a, b):\n    return a - b\n")
        self.fm("check", check=False, env={"FOREMAN_SENTINEL": "off"})
        tid = self.task()
        self.write("calc.py", "def add(a, b):\n    return a - b  # still\n")
        r = self.fm("task", "finish", tid, "--audit", "self", "--run", "true", "--json", check=False,
                    env={"FOREMAN_SENTINEL": "off"})
        json.loads(r.stdout)

    def test_a_sentinel_run_on_a_tree_edited_since_the_close_captures_nothing(self):
        tid = self.task()
        self.write("calc.py", SRC + "# tidy\n")
        self.fm("task", "finish", tid, "--audit", "self", "--run", "true", env={"FOREMAN_SENTINEL": "off"})
        self.task("Next")
        self.write("calc.py", "def add(a, b):\n    return a - b\n")  # the next task's red-first edit
        self.fm("check", check=False, env={"FOREMAN_SENTINEL_RUN": "1", "FOREMAN_SENTINEL_TREE": "0" * 12})
        self.assertEqual([b for b in c.load_briefs(self.p) if b.meta.get("baseline")], [])

    @unittest.skipUnless(shutil.which("nice"), "no nice on this host: the sentinel starts as before")
    def test_the_background_sentinel_runs_at_low_priority(self):
        # T-1463: started at full priority, it competed for CPU with the next task's foreground finish and tests
        from sampo import cli
        from unittest import mock
        started, real = [], cli.subprocess.Popen
        env = {k: v for k, v in os.environ.items() if k not in ("FOREMAN_SENTINEL", "FOREMAN_NO_BACKGROUND")}
        # subprocess is shared: only the sentinel's own start is faked, git and the rest run for real
        late = mock.Mock(side_effect=cli.subprocess.TimeoutExpired("check", 1))  # T-4015: past the close's short wait
        fake = lambda argv, **kw: (started.append(argv) or mock.Mock(pid=1, wait=late)) if "sentinel" in argv \
            else real(argv, **kw)
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(c, "host_strain", lambda: False), \
                mock.patch.object(cli.subprocess, "Popen", fake):
            self.assertEqual(cli._sentinel(self.p), "started")
        self.assertEqual(started[0][:3], ["nice", "-n", "10"])

    def test_quick_gates_are_waited_for_and_slow_ones_left_running(self):
        # T-4015: on a two-file repo the gates take seconds, yet the close said "still running" and the session
        # reported a result it never saw; a close waits up to SENTINEL_WAIT_S when the last full run was that quick
        from sampo import cli
        from unittest import mock
        real = cli.subprocess.Popen
        env = {k: v for k, v in os.environ.items() if k not in ("FOREMAN_SENTINEL", "FOREMAN_NO_BACKGROUND")}

        def run(wait):
            fake = lambda argv, **kw: mock.Mock(pid=1, wait=wait) if "sentinel" in argv else real(argv, **kw)  # noqa: E731
            with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(c, "host_strain", lambda: False), \
                    mock.patch.object(cli, "_full_pass", lambda p: False), mock.patch.object(cli, "_pm_covers", lambda p: False), \
                    mock.patch.object(cli.subprocess, "Popen", fake):
                return cli._sentinel(self.p)
        self.assertEqual(run(mock.Mock(return_value=0)), "green")  # no full run on record: a short wait is the probe
        self.assertEqual(run(mock.Mock(return_value=1)), "red")
        late = mock.Mock(side_effect=cli.subprocess.TimeoutExpired("check", cli.SENTINEL_WAIT_S))
        self.assertEqual(run(late), "started")
        c.log_event(self.p, "check_run", data={"results": [{"cmd": "python3 tests/run.py", "exit": 0, "s": 600.0}]})
        for _ in range(cli.SENTINEL_CLOSES):
            c.log_event(self.p, "task_done", task="T-0009")
        slow = mock.Mock(return_value=0)
        self.assertEqual(run(slow), "started")  # a 10-minute suite is never waited on
        slow.assert_not_called()

    def test_only_one_sentinel_runs_at_a_time(self):
        from sampo import cli
        with open(os.path.join(self.p.dir, "sentinel.pid"), "w") as f:
            f.write(str(os.getpid()))  # a live run
        os.environ["FOREMAN_SENTINEL"] = "sync"
        try:
            self.assertIsNone(cli._sentinel(self.p))
        finally:
            del os.environ["FOREMAN_SENTINEL"]

    def test_a_file_its_scope_log_names_is_committed_with_it(self):
        # T-1122: a doc edited in the same command as the finish has no touched record yet; another task touched it
        # since this one's first focus, so the commit left it out though the task's own scope reason named it
        self.write("CHANGELOG.md", "# Changelog\n")
        subprocess.run(["git", "-C", self.repo, "add", "-A"], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", "changelog"], check=True)
        a = self.task()
        self.fm("checkpoint")
        b = self.task("Other")
        self.hook("PostToolUse", {"tool_name": "Edit", "tool_input": {"file_path": os.path.join(self.repo, "CHANGELOG.md")},
                                  "tool_response": {}})
        self.fm("checkpoint")
        self.fm("focus", a)
        self.write("calc.py", SRC + "# a\n")
        self.write("CHANGELOG.md", "# Changelog\n- a\n")
        self.fm("task", "log", a, "scope: CHANGELOG.md — its release note")
        r = self.fm("task", "finish", a, "--audit", "self", "--run", "true", "--commit", "A", check=False,
                    env={"FOREMAN_SENTINEL": "off"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        shown = subprocess.run(["git", "-C", self.repo, "show", "--name-only", "--format=", "HEAD"], capture_output=True,
                               text=True).stdout.split()
        self.assertIn("CHANGELOG.md", shown, r.stdout)
        self.assertIn("calc.py", shown)

    def test_a_close_starts_a_full_run_when_the_last_one_is_old(self):
        tid = self.task()
        self.write("calc.py", SRC + "# tidy\n")
        before = len(self.events("check_run"))
        r = self.fm("task", "finish", tid, "--audit", "self", "--run", "true", check=False,
                    env={"FOREMAN_SENTINEL": "sync"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        runs = self.events("check_run")
        self.assertEqual(len(runs), before + 1, "no full run yet: one starts after the close")
        self.assertEqual({x["cmd"] for x in runs[-1]["data"]["results"]}, {"python3 tests/run.py"})
        tid = self.task("Again")
        self.write("calc.py", SRC + "# again\n")
        self.fm("task", "finish", tid, "--audit", "self", "--run", "true", env={"FOREMAN_SENTINEL": "sync"})
        self.assertEqual(len(self.events("check_run")), before + 1, "a fresh full run: none started")

    # T-3576: a train's merges get the full suite in the background (lanes.pm_run): the batch's one full run, so the
    # sentinel after its closes would repeat it
    def pm(self, sha, state, **kv):  # a post-merge verify record, as lanes.pm_record writes it
        os.makedirs(os.path.join(self.p.dir, "postmerge"), exist_ok=True)
        with open(os.path.join(self.p.dir, "postmerge", f"{sha}.json"), "w") as f:
            json.dump({"sha": sha, "task": "T-0001", "state": state, "n": time.time_ns(), "at": c.now(), **kv}, f)

    def sentinel(self):
        from sampo import cli
        from unittest import mock
        with mock.patch.dict(os.environ, {"FOREMAN_SENTINEL": "sync"}), mock.patch.object(c, "host_strain", lambda: False):
            return cli._sentinel(self.p)

    def head(self):
        return subprocess.run(["git", "-C", self.repo, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()

    def test_the_sentinel_waits_for_a_pending_or_running_post_merge_verify(self):
        self.pm("a" * 40, "pending")
        self.assertIsNone(self.sentinel())
        self.pm("a" * 40, "running", pid=os.getpid())
        self.assertIsNone(self.sentinel())

    def test_a_post_merge_verify_of_this_code_tree_stands_for_the_sentinel(self):
        self.pm(self.head(), "green")
        self.write("CHANGELOG.md", "- a close's note\n")  # a close's own edit: the code tree is unchanged
        self.assertIsNone(self.sentinel())

    def test_a_recent_post_merge_verify_counts_as_the_last_full_run(self):
        c.log_event(self.p, "lane_verified", task="T-0001", data={"sha": "b" * 40})
        self.assertIsNone(self.sentinel())

    def test_the_sentinel_still_runs_when_no_verify_covers_this_tree(self):
        old = self.head()
        self.write("calc.py", SRC + "# changed\n")
        subprocess.run(["git", "-C", self.repo, "commit", "-qam", "change"], check=True)
        self.pm(old, "green")  # verified, but an older code tree
        self.pm("c" * 40, "pending", at="2026-01-01T00:00:00Z")  # stuck: landed long ago, no runner took it
        self.pm("d" * 40, "running", pid=4194305)  # its runner is gone (past any pid_max)
        self.assertEqual(self.sentinel(), "ran")


MARKED = ("import os, unittest\n\n\nclass Marked(unittest.TestCase):\n    def test_marked(self):\n"
          "        open(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "
          "'ran.marker'), 'w').close()\n")


class PostMergeProof(ForemanTestCase):
    """T-5368: a criterion whose command is exactly `python3 plugin/tests/run.py NAMES` is proven by a green post-merge full
    suite (lanes.pm_records) on this very code tree, recorded as [ran] evidence citing the merge, and runs no test; any
    other record (red, pending, reverted, another tree) or command (-k, flags, a name that is no test) runs as before.
    The tests' own file writes `ran.marker` when it runs."""
    CMD = "python3 plugin/tests/run.py test_marked"

    def setUp(self):
        super().setUp()
        self.fm("init")
        os.makedirs(os.path.join(self.repo, "plugin", "tests"))
        shutil.copy(os.path.join(HERE, "run.py"), os.path.join(self.repo, "plugin", "tests", "run.py"))
        self.write("calc.py", SRC)
        self.write("plugin/tests/test_marked.py", MARKED)
        self.write(".gitignore", "__pycache__/\nran.marker\n")
        subprocess.run(["git", "-C", self.repo, "add", "-A"], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", "suite"], check=True)
        self.p = c.find_project(self.repo)

    def write(self, rel, text):
        with open(os.path.join(self.repo, rel), "w") as f:
            f.write(text)

    def head(self):
        return subprocess.run(["git", "-C", self.repo, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()

    def pm(self, sha, state, **kv):  # a post-merge verify record, as lanes.pm_record writes it
        os.makedirs(os.path.join(self.p.dir, "postmerge"), exist_ok=True)
        with open(os.path.join(self.p.dir, "postmerge", f"{sha}.json"), "w") as f:
            json.dump({"sha": sha, "task": "T-0001", "state": state, "n": time.time_ns(), "at": c.now(), **kv}, f)

    def close(self, cmd, *records, extra=()):
        """A new task whose one criterion is `cmd`, finished with these post-merge records on file ((sha, state, kv));
        (exit code, the evidence text, whether the test ran)."""
        marker = os.path.join(self.repo, "ran.marker")
        if os.path.exists(marker):
            os.remove(marker)
        shutil.rmtree(os.path.join(self.p.dir, "postmerge"), ignore_errors=True)
        for sha, state, kv in records:
            self.pm(sha, state, **kv)
        tid = self.fm_json("task", "new", "Change add", "--type", "FEATURE", "--tier", "S", "--ac", f"works :: {cmd}",
                           "--step", "s", "--scope", "calc.py")["id"]
        self.fm("focus", tid)
        r = self.fm("task", "finish", tid, "--audit", "self", "--run", "true", *extra, check=False,
                    env={"FOREMAN_SENTINEL": "off"})
        ev = "\n".join(c.find_brief(self.p, tid).evidence())
        return r, ev, os.path.exists(marker)

    def test_post_merge_green_on_this_tree_proves_the_criterion_and_runs_no_test(self):
        sha = self.head()
        r, ev, ran = self.close(self.CMD, (sha, "green", {}))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(ran, "the full suite already ran it on this code: no rerun")
        self.assertIn("post-merge full suite", ev)
        self.assertIn(sha[:9], ev, "the evidence cites the merge it verified")
        self.assertIn("[ran]", ev)

    def test_post_merge_green_that_covered_this_tree_by_a_later_commit_proves_it(self):
        sha = self.head()
        r, ev, ran = self.close(self.CMD, ("e" * 40, "green", {"covered_by": sha, "narrowed": False}))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(ran)
        self.assertIn("post-merge full suite", ev)

    def test_post_merge_red_pending_running_or_reverted_proves_nothing(self):
        sha = self.head()
        for what in ({"state": "red", "classes": ["x.Y"]}, {"state": "pending"}, {"state": "running", "pid": os.getpid()},
                     {"state": "reverted"}, {"state": "conflict"}, {"state": "green", "after_reverts": ["a" * 40]}):
            with self.subTest(what):
                r, ev, ran = self.close(self.CMD, (sha, what.pop("state"), what))
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertTrue(ran, "no green record for this code: the criterion runs")
                self.assertNotIn("post-merge full suite", ev)

    def test_post_merge_green_on_another_code_tree_or_long_ago_proves_nothing(self):
        old = self.head()
        self.write("calc.py", SRC + "# changed\n")
        subprocess.run(["git", "-C", self.repo, "commit", "-qam", "change"], check=True)
        r, ev, ran = self.close(self.CMD, (old, "green", {}))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(ran, "an older code tree: the criterion runs")
        r, ev, ran = self.close(self.CMD, (self.head(), "green", {"at": "2020-01-01T00:00:00Z"}))
        self.assertTrue(ran, "a record past CACHE_DAYS proves nothing")

    def test_post_merge_proof_is_for_the_exact_names_command_only(self):
        sha = self.head()
        for cmd in ("python3 plugin/tests/run.py test_marked -k marked", "python3 plugin/tests/run.py -k marked",
                    "python3 plugin/tests/run.py", "python3 plugin/tests/run.py test_marked && true",
                    "python3 plugin/tests/run.py test_marked; true", "python3 plugin/tests/run.py $(true) test_marked",
                    "python3 plugin/tests/run.py ../test_marked", "python3 plugin/tests/run.py test_marked --fast",
                    "SAMPO_CACHE_FULL=1 python3 plugin/tests/run.py test_marked", "python3 other/run.py test_marked"):
            with self.subTest(cmd):
                r, ev, ran = self.close(cmd, (sha, "green", {}))
                self.assertNotIn("post-merge full suite", ev, cmd)

    def test_post_merge_proof_needs_the_named_test_to_exist(self):
        r, ev, ran = self.close("python3 plugin/tests/run.py test_nothing", (self.head(), "green", {}))
        self.assertNotEqual(r.returncode, 0, "a name that is no test file is not covered by a suite that never ran it")
        self.assertNotIn("post-merge full suite", ev)

    def test_a_dotted_name_or_a_module_with_no_test_is_run_not_proven(self):
        # T-5368 review: Module.Class (or a name matching nothing) fails "no test matches" when run: a green suite
        # that never ran that name proves nothing for it
        self.write("plugin/tests/test_empty.py", "import unittest\n\n\ndef helper():\n    return 1\n")
        subprocess.run(["git", "-C", self.repo, "add", "-A"], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", "empty"], check=True)
        sha = self.head()  # committed: a record proves only the tree the close runs on
        for cmd in ("python3 plugin/tests/run.py test_marked.Marked", "python3 plugin/tests/run.py test_marked.Nothing",
                    "python3 plugin/tests/run.py test_empty", "python3 plugin/tests/run.py test_marked test_empty"):
            with self.subTest(cmd):
                r, ev, ran = self.close(cmd, (sha, "green", {}))
                self.assertNotIn("post-merge full suite", ev, cmd)

    def test_a_record_covered_by_a_narrowed_run_proves_nothing(self):
        # T-5368 review: _pm_cover(how=...) marks merges green on a red-classes-alone run: the full suite never ran there
        sha = self.head()
        r, ev, ran = self.close(self.CMD, ("e" * 40, "green", {"covered_by": sha, "narrowed": True}))
        self.assertTrue(ran, "a narrowed cover is no full suite on this tree: the criterion runs")
        self.assertNotIn("post-merge full suite", ev)

    def test_a_cover_record_proves_only_with_an_explicit_not_narrowed(self):
        # T-5368 re-review: records written before the flag existed carry no `narrowed` key: they may be narrowed covers
        sha = self.head()
        r, ev, ran = self.close(self.CMD, ("e" * 40, "green", {"covered_by": sha}))
        self.assertTrue(ran, "an old cover record (no flag) is not known to be a full suite: the criterion runs")
        self.assertNotIn("post-merge full suite", ev)
        r, ev, ran = self.close(self.CMD, ("e" * 40, "green", {"covered_by": sha, "narrowed": False}))
        self.assertFalse(ran)
        self.assertIn("post-merge full suite", ev)

    def test_a_green_excused_by_unrelated_or_flaky_proves_no_criterion_naming_the_excused_module(self):
        # T-5368 re-review: lanes.pm_run sets green with unrelated=/flaky= for classes that were red: running them fails
        sha = self.head()
        for kv in ({"unrelated": ["test_marked.Marked"]}, {"unrelated": ["test_other.Other"]},
                   {"flaky": ["test_marked.Marked"]}, {"flaky": ["test_marked"], "unrelated": []}):
            with self.subTest(kv):
                r, ev, ran = self.close(self.CMD, (sha, "green", kv))
                self.assertTrue(ran, "an excused red class was never green here: the criterion runs")
                self.assertNotIn("post-merge full suite", ev)
        r, ev, ran = self.close(self.CMD, (sha, "green", {"flaky": ["test_other.Other"], "unrelated": []}))
        self.assertFalse(ran, "a flaky class of another module leaves this module's proof whole")
        self.assertIn("post-merge full suite", ev)

    def test_a_deferred_close_is_refused_when_the_trunk_code_tree_cannot_be_read(self):
        # T-5368 re-review: a None tree before the run must not switch the trunk-moved guard off
        from sampo import cli
        self.assertTrue(cli._trunk_unsteady(self.p, None))
        self.assertFalse(cli._trunk_unsteady(self.p, c.code_tree(self.p.root, "HEAD")))
        self.assertTrue(cli._trunk_unsteady(self.p, "0" * 40))

    def test_a_typed_note_equal_to_a_close_mark_cannot_forge_it(self):
        # T-5368 re-review: `task log` text goes to the Log: a note reading as the pending mark would owe a commit
        from sampo import train
        tid = self.fm_json("task", "new", "Change add", "--type", "FEATURE", "--tier", "S", "--ac", "works :: true",
                           "--step", "s", "--scope", "calc.py")["id"]
        self.fm("task", "log", tid, c.CLOSE_PENDING)
        self.fm("task", "log", tid, c.CLOSE_COMMITTED)
        b = c.find_brief(self.p, tid)
        self.assertFalse(train._commit_pending(b))
        self.fm("task", "log", tid, c.CLOSE_PENDING)
        self.assertFalse(train._commit_pending(c.find_brief(self.p, tid)), "a typed pending mark owes nothing")
        b = c.find_brief(self.p, tid)
        b.append_mark(c.CLOSE_PENDING)
        self.assertTrue(train._commit_pending(b), "fm's own write path still marks")

    def test_a_deferred_close_whose_trunk_moved_while_its_criteria_ran_records_nothing(self):
        # T-5368 review: phase 1 holds no lock now: a train's fast-forward can move trunk mid-run, and evidence stamped
        # after it would claim a tree the criteria never ran on
        mover = "echo '# moved' >> calc.py && git commit -qam moved"  # in the task's scope: only the move can refuse it
        r, ev, ran = self.close(mover, (self.head(), "green", {}), extra=("--defer-changelog",))
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("trunk moved", r.stdout + r.stderr)
        self.assertNotIn("calc.py", ev, "no evidence on a tree the criterion did not run on")
        self.assertNotEqual(c.find_brief(self.p, "T-0001").status, "done")

    def test_a_deferred_close_whose_trunk_moved_during_its_affected_run_is_refused_not_done(self):
        # T-5368 final review: the first check runs before the linked tests, which take minutes: a train may move trunk there
        import contextlib
        import io
        from unittest import mock
        from sampo import cli
        tid = self.fm_json("task", "new", "Change add", "--type", "FEATURE", "--tier", "S", "--ac", "works :: true",
                           "--step", "s", "--scope", "calc.py")["id"]
        self.fm("focus", tid)

        def moves(*a, **k):  # what a train's fast-forward does while the linked tests run
            self.write("calc.py", SRC + "# moved\n")
            subprocess.run(["git", "-C", self.repo, "commit", "-qam", "moved"], check=True)
        env = {"FOREMAN_HOME": self.home, "FOREMAN_INHIBITED": "1", "FOREMAN_DOCTOR_BENCH": "0",
               "FOREMAN_DOCTOR_BUILD": "0", "FOREMAN_SENTINEL": "off"}
        here, err = os.getcwd(), io.StringIO()
        os.chdir(self.repo)
        try:
            with mock.patch.dict(os.environ, env), mock.patch.object(cli, "_close_affected", moves), \
                    contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                rc = cli.main(["task", "finish", tid, "--audit", "self", "--run", "true", "--defer-changelog"])
        finally:
            os.chdir(here)
        self.assertNotEqual(rc, 0, err.getvalue())
        self.assertIn(cli.TREE_MOVED, err.getvalue())
        self.assertNotEqual(c.find_brief(self.p, tid).status, "done")

    def test_a_deferred_close_on_a_steady_trunk_still_closes(self):
        r, ev, ran = self.close("true", extra=("--defer-changelog",))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(c.find_brief(self.p, "T-0001").status, "done")
        self.assertIn(c.CLOSE_PENDING, c.find_brief(self.p, "T-0001").section("Log"), "done and its commit owed, in one write")

    def test_the_evidence_is_labelled_a_post_merge_record_the_close_still_accepts(self):
        # T-5368 review: not a bare [ran] (which says fm ran the command): a distinct mark naming the merge
        sha = self.head()
        r, ev, ran = self.close(self.CMD, (sha, "green", {}))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        line = next(l for l in ev.splitlines() if "post-merge full suite" in l)
        self.assertIn(f"[post-merge record {sha[:9]}]", line)
        self.assertIn("[ran]", line, "the gates read a [ran] pass: the criterion counts as proven")
        typed = c._unmarked("x [post-merge record abc] y")
        self.assertNotIn("[post-merge record", typed, "typed text can't forge the mark")


class FinishListsAll(ForemanTestCase):
    """T-4147: one refusal of sampo task finish names every field the close still lacks, each with its fix, before a
    gate or a verify command starts; --dry prints the same list and changes nothing, --json carries it as an array."""

    def setUp(self):
        super().setUp()
        self.fm("init")
        self.marker = os.path.join(self.tmp, "gate-ran")
        self.p = c.find_project(self.repo)

    def m_fix(self):
        """An M FIX with its criterion's check touching the marker: a gate that ran would leave the marker behind."""
        interpretation, approach = valid_m_plan()
        tid = self.fm_json("task", "new", "Fix add", "--type", "FIX", "--tier", "M", "--interpretation", interpretation,
                           "--approach", approach, "--ac", f"works :: touch {self.marker}", "--step", "s",
                           "--scope", "src/**")["id"]
        self.fm("focus", tid)
        return tid

    def items(self, text):
        return [line.strip() for line in text.splitlines() if re.match(r"\s+\d+\. ", line)]

    def test_the_refusal_lists_all_the_missing_fields_with_their_fixes(self):
        tid = self.m_fix()
        r = self.fm("task", "finish", tid, check=False)
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        items = self.items(r.stderr)
        for want in ("--audit", "audit missing: --lens", "give --run", "docs impact missing", "no red→green proof"):
            self.assertTrue(any(want in item for item in items), (want, r.stderr))
        self.assertIn(f"{len(items)} things missing", r.stderr, "the first line counts the list")
        self.assertIn("nothing ran", r.stderr)
        self.assertFalse(os.path.exists(self.marker), "a gate ran although the close was refused")
        self.assertNotEqual(c.find_brief(self.p, tid).status, "done")

    def test_dry_lists_all_the_same_items_and_changes_nothing(self):
        tid = self.m_fix()
        full = self.fm("task", "finish", tid, check=False)
        before = len(c.ledger_tail(self.p, 100000))
        dry = self.fm("task", "finish", tid, "--dry", check=False)
        self.assertNotEqual(dry.returncode, 0, dry.stdout + dry.stderr)
        self.assertEqual(self.items(dry.stdout + dry.stderr), self.items(full.stderr))
        self.assertEqual(len(c.ledger_tail(self.p, 100000)), before, "a dry listing wrote to the ledger")
        self.assertFalse(os.path.exists(self.marker))

    def test_json_lists_all_as_kind_text_fix_rows(self):
        tid = self.m_fix()
        text = self.fm("task", "finish", tid, check=False).stderr
        r = self.fm("task", "finish", tid, "--dry", "--json", check=False)
        rows = json.loads(r.stdout)
        self.assertEqual(len(rows), len(self.items(text)))
        self.assertEqual({tuple(sorted(row)) for row in rows}, {("fix", "kind", "text")})
        kinds = {row["kind"] for row in rows}
        self.assertTrue({"audit", "lens", "docs", "red_green", "evidence", "lesson", "why_not_caught"} <= kinds, kinds)
        self.assertTrue(all(row["fix"] and row["fix"] != row["text"] for row in rows if row["kind"] != "other"), rows)

    def listed(self, tid, *extra):
        """The one refusal's rows for a close with these flags (--dry --json: the same list, as data)."""
        r = self.fm("task", "finish", tid, "--dry", "--json", *extra, check=False)
        return json.loads(r.stdout)

    def of_kind(self, rows, kind):
        return [row for row in rows if row["kind"] == kind]

    def test_a_missing_audit_and_a_missing_lens_are_different_kinds(self):
        rows = self.listed(self.m_fix())
        (audit,), lens = self.of_kind(rows, "audit"), self.of_kind(rows, "lens")
        self.assertIn("--audit missing", audit["text"])
        self.assertTrue(lens and all("--lens" in row["text"] for row in lens), rows)

    def test_a_bad_lens_is_reported_once_not_also_as_a_missing_lens(self):
        tid = self.m_fix()
        rows = self.listed(tid, "--audit", "x", "--lens", "intent:")  # a known lens with no result
        mentions = [row for row in rows if "intent" in row["text"]]
        self.assertEqual(len(mentions), 1, mentions)
        self.assertIn("--lens takes", mentions[0]["text"])

    def test_the_red_full_check_joins_the_one_list(self):
        tid = self.m_fix()
        tree = c.worktree_tree(self.repo)[:12]
        c.log_event(self.p, "check_run", data={"tree": tree, "results": [
            {"cmd": "python3 tests/run.py", "exit": 1, "failing": ["test_x.X.test_y"]}]})
        rows = self.listed(tid)
        (red,) = [row for row in rows if "newest full sampo check" in row["text"]]
        self.assertIn("test_x.X.test_y", red["text"])
        self.assertGreater(len(rows), 1, "listed alone: the other missing fields went unsaid")

    def test_a_lane_close_without_its_changelog_line_joins_the_one_list(self):
        self.write_repo("CHANGELOG.md", "# Changelog\n\n## Unreleased\n")
        subprocess.run(["git", "-C", self.repo, "add", "-A"], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", "changelog"], check=True)
        tid = self.m_fix()
        subprocess.run(["git", "-C", self.repo, "checkout", "-q", "-b", f"foreman/{tid}"], check=True)
        rows = self.listed(tid)
        (log,) = self.of_kind(rows, "changelog")
        self.assertIn("--changelog", log["fix"])
        self.assertGreater(len(rows), 1)
        self.assertEqual(self.of_kind(self.listed(tid, "--changelog", "Fixed add (T-9)"), "changelog"), [])

    def test_a_credential_in_the_files_to_commit_joins_the_one_list(self):
        tid = self.m_fix()
        os.makedirs(os.path.join(self.repo, "src"), exist_ok=True)
        self.write_repo("src/leak.py", 'KEY = "' + "AKIA" + "Q7ZT4M2XK9" + "PL3VBN" + '"\n')
        rows = self.listed(tid, "--commit", "fix add")
        (leak,) = self.of_kind(rows, "commit")
        self.assertIn("credential", leak["text"])
        self.assertNotIn("AKIA", json.dumps(rows), "the key itself is never printed")
        self.assertGreater(len(rows), 1)

    def test_dry_with_commit_leaves_what_the_user_staged_staged(self):
        tid = self.m_fix()
        os.makedirs(os.path.join(self.repo, "src"), exist_ok=True)
        self.write_repo("src/a.py", "x = 1\n")
        subprocess.run(["git", "-C", self.repo, "add", "src/a.py"], check=True)
        r = self.fm("task", "finish", tid, "--dry", "--commit", "fix add", check=False)
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        staged = subprocess.run(["git", "-C", self.repo, "diff", "--cached", "--name-only"], capture_output=True,
                                text=True, check=True).stdout.split()
        self.assertEqual(staged, ["src/a.py"], "the dry probe touched the real index")
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".git", "index.lock")))

    def test_dry_on_a_done_task_lists_nothing_as_text_and_as_json(self):
        tid = self.fm_json("task", "new", "Tiny", "--type", "FEATURE", "--tier", "S", "--ac", "works :: true", "--step",
                           "s", "--scope", "src/**")["id"]
        self.fm("focus", tid)
        self.fm("task", "finish", tid, "--audit", "self", "--run", "true", env={"FOREMAN_SENTINEL": "off"})
        self.assertEqual(c.find_brief(self.p, tid).status, "done")
        text = self.fm("task", "finish", tid, "--dry", "--commit", "x", check=False)
        self.assertEqual(text.returncode, 0, text.stdout + text.stderr)
        self.assertIn("is done", text.stdout)
        as_json = self.fm("task", "finish", tid, "--dry", "--commit", "x", "--json", check=False)
        self.assertEqual(json.loads(as_json.stdout), [])

    def test_a_label_with_a_colon_inside_its_parentheses_keeps_its_whole_fix(self):
        from sampo import cli
        row = cli._gap_row("step 1 (Fix: parse): its last run failed; record a passing run (sampo task evidence T-1 "
                           "--step 1 --run CMD, or finish with --run CMD)")
        self.assertTrue(row["fix"].startswith("its last run failed; record a passing run"), row)
        row = cli._gap_row("edited outside scope [a: b, c]: x.py; widen it (sampo task set T-1 scope=…)")
        self.assertTrue(row["fix"].startswith("x.py; widen it"), row)

    def write_repo(self, rel, text):
        with open(os.path.join(self.repo, rel), "w") as f:
            f.write(text)


if __name__ == "__main__":
    unittest.main()
