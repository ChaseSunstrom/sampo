"""T-1117: Foreman keeps the suite green itself. A close runs the tests linked to what it changed and refuses a new
failure; a red full-gate run becomes one urgent baseline task; full runs start in the background after closes."""
import json
import os
import shutil
import subprocess
import time
import unittest

from helpers import ForemanTestCase

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
        self.assertRegex(r.stdout, r"Slowest: sleep 1 \d+ s \(of \d+ s\)")
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


if __name__ == "__main__":
    unittest.main()
