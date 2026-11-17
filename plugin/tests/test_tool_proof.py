"""T-2372 (Mission 6, #5): a task that built a tool (fm new tool, fm mcp new) closes only after four proofs, each
refused on its own and named in the refusal: a tool_search receipt for its need before the build, the tool's test run
by fm red and then green, that test failing on a stubbed copy of the tool, and the tool run by fm for real (a [ran]
evidence line: the tool's path in executable position, exit 0, after the green run). Review fixes: a tool that can't be
checked is refused, not skipped; naming the file (cat, git add, py_compile) is no use; no transcript is read."""
import os
import time

from helpers import ForemanTestCase

from sampo import core as c
from sampo import toolproof

SID = "0a1b2c3d-0000-4000-8000-0123456789ab"
TOOL = ("#!/usr/bin/env python3\n# fm new tool\nimport sys\n\n\ndef run(text):\n    return text.upper()\n\n\n"
        "def main(argv=None):\n    a = sys.argv[1:] if argv is None else argv\n"
        "    if not a or not a[0]:\n        return 1\n    print(run(a[0]))\n    return 0\n\n\n"
        "if __name__ == '__main__':\n    sys.exit(main())\n")
TEST = ("import os, sys, unittest\nsys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "
        "'..', 'tools'))\nimport shout\n\n\nclass T(unittest.TestCase):\n    def test_it(self):\n"
        "        self.assertEqual(shout.run('abc'), 'ABC')\n        self.assertEqual(shout.main(['']), 1)\n\n\n"
        "if __name__ == '__main__':\n    unittest.main()\n")
WEAK = TEST.replace("self.assertEqual(shout.run('abc'), 'ABC')", "pass").replace("self.assertEqual(shout.main(['']), 1)",
                                                                                   "pass")
RUN = "python3 tests/test_shout.py"
USE = "python3 tools/shout.py hello"
PHRASE = {"search": "no search receipt", "red": "no red then green", "stub": "stub check", "use": "never run",
          "check": "could not be checked"}


class ToolProof(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.fm("task", "new", "Shout", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true", "--step", "s",
                "--focus")
        self.p = c.find_project(self.repo)
        self.tid = c.load_briefs(self.p)[-1].id
        self.env = {"CLAUDE_CODE_SESSION_ID": SID, "CLAUDE_CONFIG_DIR": os.path.join(self.tmp, "cc")}
        self.tool, self.test = (os.path.join(self.repo, d) for d in ("tools/shout.py", "tests/test_shout.py"))

    def f(self, *a, **kw):
        return self.fm(*a, env=self.env, check=kw.pop("check", True), **kw)

    def write(self, path, text):
        with open(path, "w") as fh:
            fh.write(text)

    def search(self):
        self.f("which", "shout", check=False)

    def build(self, searched=True):
        if searched:
            self.search()
        self.f("new", "tool", "shout")

    def ran(self, cmd):
        return self.f("task", "evidence", self.tid, "--step", "1", "--run", cmd, check=False)

    def run_test(self):
        return self.ran(RUN)

    def red_green(self, body=TEST):
        self.run_test()  # the scaffold's own test fails until the tool works
        self.write(self.tool, TOOL)
        self.write(self.test, body)
        self.run_test()

    def use(self, cmd=USE):
        time.sleep(1.05)  # timestamps are whole seconds and "after" is strict
        r = self.ran(cmd)
        self.assertIn("exit 0", r.stdout + r.stderr, "the use fixture must itself exit 0: " + r.stdout + r.stderr)

    def lock_tool(self):
        """chmod 000 the tool after the green run (skip where root ignores it)."""
        os.chmod(self.tool, 0)
        if os.access(self.tool, os.R_OK):
            os.chmod(self.tool, 0o644)
            self.skipTest("this user reads a chmod 000 file")

    def finish(self):
        return self.f("task", "finish", self.tid, "--audit", "self", "--run", "true", check=False)

    def refused(self, *words):
        r = self.finish()
        out = r.stderr + r.stdout
        self.assertNotEqual(r.returncode, 0, out)
        for w in words:
            self.assertIn(PHRASE[w], out)
        self.assertEqual(c.find_brief(self.p, self.tid).status, "active")
        return out

    def proved(self):
        return c.ledger_events(self.p, ["tool_proved"])

    def test_all_four_close_it_and_log_tool_proved(self):
        self.build()
        self.red_green()
        self.use()
        r = self.finish()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        (e,) = self.proved()
        self.assertEqual((e["task"], e["data"]["name"], e["data"]["path"]), (self.tid, "shout", self.tool))

    def test_a_dry_finish_logs_no_tool_proved_line(self):
        # T-4147: --dry changes nothing, the ledger included: the proof is read, not recorded
        self.build()
        self.red_green()
        self.use()
        before = len(c.ledger_tail(self.p, 100000))
        r = self.f("task", "finish", self.tid, "--dry", "--audit", "self", "--run", "true", check=False)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(self.proved(), [])
        self.assertEqual(len(c.ledger_tail(self.p, 100000)), before, "a dry finish wrote to the ledger")

    def test_a_task_that_built_nothing_gets_no_check(self):
        r = self.finish()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual(self.proved(), [])

    def test_the_build_is_logged_with_the_task_the_files_and_the_need(self):
        self.build()
        (e,) = c.ledger_events(self.p, ["tool_built"])
        self.assertEqual((e["task"], e["data"]["path"], e["data"]["test"], e["data"]["name"]),
                         (self.tid, self.tool, self.test, "shout"))

    def test_no_search_receipt_before_the_build_is_refused_alone(self):
        self.build(searched=False)
        self.red_green()
        self.use()
        out = self.refused("search")
        for other in ("stub", "red", "use"):
            self.assertNotIn(PHRASE[other], out)
        self.search()  # a search after the build is no receipt for it
        self.refused("search")
        self.assertEqual(self.proved(), [])

    def test_a_search_for_something_else_is_no_receipt(self):
        self.f("which", "zorblat frobnicate", check=False)
        self.build(searched=False)
        self.red_green()
        self.use()
        self.refused("search")

    def test_a_test_never_seen_red_is_refused_alone(self):
        self.build()
        self.write(self.tool, TOOL)
        self.write(self.test, TEST)
        self.run_test()  # green the first time: nothing says it could fail
        self.use()
        out = self.refused("red")
        for other in ("search", "stub", "use"):
            self.assertNotIn(PHRASE[other], out)

    def test_a_test_left_red_is_refused(self):
        self.build()
        self.run_test()
        self.refused("red")

    def test_a_command_that_only_mentions_the_test_is_no_run_of_it(self):  # T-2372 review, low
        self.build()
        self.run_test()
        self.write(self.tool, TOOL)
        self.write(self.test, TEST)
        self.ran("test -f /tmp/none # tests/test_shout.py")  # red, naming the test
        self.ran("true # tests/test_shout.py")  # "green", naming it
        self.use()
        self.refused("red")

    def test_a_test_that_cannot_fail_on_a_stub_is_refused_alone(self):
        self.build()
        self.red_green(WEAK)
        self.use()
        out = self.refused("stub")
        for other in ("search", "red", "use"):
            self.assertNotIn(PHRASE[other], out)

    def test_no_run_of_the_tool_is_refused_alone_and_names_the_command_to_run(self):
        self.build()
        self.red_green()
        out = self.refused("use")
        self.assertIn("fm task evidence", out)
        self.assertIn("tools/shout.py", out)
        for other in ("search", "red", "stub"):
            self.assertNotIn(PHRASE[other], out)
        self.assertEqual(self.proved(), [])

    def test_naming_the_file_is_no_use_but_a_run_after_green_is(self):  # T-2372 review, high
        self.build()
        self.red_green()
        time.sleep(1.05)  # all of these come after the green run: only what they are keeps them from counting
        for cmd in ("cat tools/shout.py", "python3 -m py_compile tools/shout.py", "python3 tools/shout.py --help",
                    "python3 tools/shout.py hello || true", "python3 tools/shout.py hello; true", RUN,
                    "echo tools/shout.py", "git add tools/shout.py"):
            self.ran(cmd)
        self.refused("use")
        self.use()
        r = self.finish()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)

    def test_a_run_before_the_green_run_is_no_use(self):
        self.build()
        self.run_test()  # red
        self.write(self.tool, TOOL)
        self.write(self.test, TEST)
        self.ran(USE)
        time.sleep(1.05)
        self.run_test()  # green, later than the use
        self.refused("use")

    def test_a_failing_run_of_the_tool_is_no_use(self):
        self.build()
        self.red_green()
        time.sleep(1.05)
        self.ran("python3 tools/shout.py ''")  # the tool exits 1 on empty input
        self.refused("use")

    def test_without_a_transcript_the_use_is_still_refused(self):  # T-2372 review: no transcript path, no tool_unproven
        self.build()
        self.red_green()
        self.refused("use")
        self.assertEqual(c.ledger_events(self.p, ["tool_unproven"]), [])

    def test_every_missing_piece_is_named_in_one_refusal_and_no_typed_reason_replaces_one(self):
        self.build(searched=False)
        out = self.refused("search", "red")  # the use is judged only once a green run says when to look
        r = self.f("task", "evidence", self.tid, "--step", "1", "tool proved, trust me", "exit 0", check=False)
        self.assertEqual(r.returncode, 0, r.stderr)  # a typed line is evidence of the step, not of the tool
        self.refused("search", "red")
        self.assertIn("shout", out)

    def test_proving_resolves_a_miss_of_the_same_name(self):
        c.log_event(self.p, "capability_miss", task=self.tid, data={"name": "shout", "kind": "command",
                                                                    "cmd": "shout hi"})
        time.sleep(1.05)
        self.build()
        self.red_green()
        self.use()
        r = self.finish()
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)

    def test_a_tool_that_cannot_be_read_is_refused_not_skipped(self):  # T-2372 review, high: chmod 000 after green
        self.build()
        self.red_green()
        self.use()
        self.lock_tool()
        out = self.refused("check")
        self.assertIn("PermissionError", out)
        self.assertEqual(self.proved(), [])

    def test_one_tool_that_errors_does_not_hide_another_tools_gaps(self):
        self.build()
        self.red_green()
        self.use()
        self.f("new", "tool", "other")  # built, never searched for, never run
        self.lock_tool()
        out = self.refused("check", "search")
        self.assertIn("tool other", out)

    def test_a_broken_ledger_read_still_fails_open(self):
        from sampo import core
        real = core.ledger_events
        self.addCleanup(setattr, core, "ledger_events", real)

        def boom(*a, **k):
            raise OSError("ledger gone")
        core.ledger_events = boom
        self.assertEqual(toolproof.safe_gaps(self.p, c.find_brief(self.p, self.tid)), [])

    def test_the_scaffolds_tests_fail_on_a_stub_and_pass_on_the_real_tool(self):
        self.build()
        self.write(self.tool, TOOL)
        self.write(self.test, TEST)
        self.assertIsNone(toolproof.stub_problem(self.tool, self.test))
        self.write(self.test, WEAK)
        self.assertIn("stub", toolproof.stub_problem(self.tool, self.test))
        self.write(self.test, "import sys\nsys.exit(3)\n")  # fails on the real one too: nothing to tell
        why = toolproof.stub_problem(self.tool, self.test)
        self.assertIn("real", why)
        self.assertIn("environment", why)  # an environment cause is named, not just the exit code

    def test_the_real_tool_runs_in_the_users_environment_minus_credentials(self):  # T-2372 review, medium
        self.build()
        self.write(self.tool, TOOL)
        self.write(self.test, "import os, sys\nsys.exit(0 if os.environ.get('FM_TP_PROBE') == '1' and "
                              "os.environ.get('HOME') and 'FM_TP_API_TOKEN' not in os.environ and "
                              "'GITHUB_TOKEN' not in os.environ else 7)\n")
        old = {k: os.environ.get(k) for k in ("FM_TP_PROBE", "FM_TP_API_TOKEN", "GITHUB_TOKEN")}
        os.environ.update(FM_TP_PROBE="1", FM_TP_API_TOKEN="x", GITHUB_TOKEN="y")
        try:
            self.assertIsNone(toolproof._run(self.tool, self.test, TOOL, 20))
            self.assertIsNone(toolproof._run(self.tool, self.test, "# stub\n", 20))  # the stub run is the same
        finally:
            for k, v in old.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

    def test_a_generated_mcp_server_is_logged_and_its_test_fails_on_a_stub(self):
        d = os.path.join(self.tmp, "srv")
        self.f("mcp", "new", "gizmo", "--wrap", "echo", "--dir", d)
        (e,) = c.ledger_events(self.p, ["tool_built"])
        self.assertEqual((e["task"], e["data"]["name"], e["data"]["path"], e["data"]["test"]),
                         (self.tid, "gizmo", os.path.join(d, "server.py"), os.path.join(d, "test_server.py")))
        self.assertIsNone(toolproof.stub_problem(e["data"]["path"], e["data"]["test"]))

    def test_a_stub_run_that_hangs_is_bounded(self):
        self.build()
        self.write(self.tool, TOOL)
        self.write(self.test, "import time\ntime.sleep(60)\n" + TEST)  # the real run hangs too: killed, no verdict
        t0 = time.monotonic()
        why = toolproof.stub_problem(self.tool, self.test, seconds=1)
        self.assertLess(time.monotonic() - t0, 20)
        self.assertIn("timed out", why)


class Runs(ForemanTestCase):
    """The executable-position reading of a command, as a pure function: `runs(cmd, path, root)` is true when the last
    command of `cmd` runs `path` (directly, or as the script of an interpreter), with no || or backgrounding to
    hide a failure, and not --help."""
    ROOT = "/r"
    TOOL = "/r/tools/x.py"

    def test_real_runs(self):
        for cmd in ("python3 tools/x.py a", "./tools/x.py a", "/r/tools/x.py", "python3 /r/tools/x.py", "FOO=1 python3 tools/x.py",
                    "env FOO=1 python3 tools/x.py", "echo hi | python3 tools/x.py", "make && python3 tools/x.py a 2>&1",
                    "python3 -u tools/x.py", "bash tools/x.py"):
            self.assertTrue(toolproof.runs(cmd, self.TOOL, self.ROOT), cmd)

    def test_mentions_are_not_runs(self):
        for cmd in ("cat tools/x.py", "git add tools/x.py", "python3 -m py_compile tools/x.py", "echo tools/x.py",
                    "python3 tools/x.py --help", "python3 tools/x.py -h", "python3 tools/x.py || true",
                    "python3 tools/x.py; true", "python3 tools/x.py &", "python3 tools/x.py\ntrue", "grep x tools/x.py",
                    "python3 tools/xy.py", "python3 tools/x.pyc", "python3 -c 'tools/x.py'", "ls tools/x.py && true",
                    "python3 \"unterminated tools/x.py", ""):
            self.assertFalse(toolproof.runs(cmd, self.TOOL, self.ROOT), cmd)

    def test_the_test_is_run_by_path_after_an_interpreter_unittest_or_pytest(self):
        t = "/r/tests/test_x.py"
        for cmd in ("python3 tests/test_x.py", "./tests/test_x.py", "python3 -m unittest tests/test_x.py",
                    "pytest tests/test_x.py", "python3 -m pytest -q tests/test_x.py::T::test_it",
                    "cd /r && python3 tests/test_x.py"):
            self.assertTrue(toolproof.runs_test(cmd, t, self.ROOT), cmd)
        for cmd in ("test -f /tmp/f # tests/test_x.py", "true # test_x.py", "cat tests/test_x.py", "echo tests/test_x.py",
                    "python3 tests/test_x.py || true", "python3 tests/test_xy.py", "python3 -m unittest other"):
            self.assertFalse(toolproof.runs_test(cmd, t, self.ROOT), cmd)
