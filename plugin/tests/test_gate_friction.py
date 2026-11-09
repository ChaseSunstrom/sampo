"""Gate and CLI friction (T-0274): a flaky test is recorded, never hidden (T-0272); an fm typo names the nearest real
command or flag (T-0273)."""
import os

from helpers import ForemanTestCase


class Flakes(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.flag = os.path.join(self.tmp, "flag")  # outside the repo: the tree doesn't change between runs
        self.fm("check", "add", f"test -f {self.flag} || {{ echo 'FAIL: test_login (auth.tests.Login.test_login)'; "
                                f"echo 'FAILED api/test_api.py::test_list - AssertionError'; exit 1; }}")

    def test_a_fail_then_pass_on_the_same_tree_is_flaky(self):
        first = self.fm("check", check=False)
        self.assertNotEqual(first.returncode, 0)
        open(self.flag, "w").close()
        second = self.fm("check")
        self.assertIn("flaky", second.stdout)
        self.assertIn("auth.tests.Login.test_login", second.stdout)
        self.assertIn("api/test_api.py::test_list", second.stdout)
        os.remove(self.flag)
        third = self.fm("check", "--fresh", check=False)  # a cached pass on this tree would skip the run
        self.assertNotEqual(third.returncode, 0)  # a known flake still fails the gate
        self.assertIn("known flaky", third.stdout)

    def test_a_pass_on_the_in_run_rerun_records_the_names(self):
        self.fm("check", "rm", "1")
        marker = os.path.join(self.tmp, "ran-once")  # fails the first time it runs, passes the rerun
        self.fm("check", "add", f"test -f {marker} || {{ touch {marker}; echo 'FAIL: test_x (pkg.T.test_x)'; exit 1; }}")
        out = self.fm("check").stdout
        self.assertIn("flaky: pkg.T.test_x", out)
        import json  # T-1789: the record keeps how it failed, so a race can be found after the rerun passed
        from sampo import core as c
        with open(os.path.join(c.find_project(self.repo).dir, "flakes.json")) as f:
            flaky = json.load(f)["flaky"]
        self.assertIn("FAIL: test_x (pkg.T.test_x)", flaky["pkg.T.test_x"]["why"])

    def test_the_flaky_note_shows_the_assertion_not_unittests_closing_rule(self):
        # T-1898: the note printed the excerpt's last line, the ----- rule that closes a unittest failure
        self.fm("check", "rm", "1")
        marker = os.path.join(self.tmp, "ran-once")
        fail = (r"printf 'FAIL: test_x (pkg.T.test_x)\n----------\nTraceback:\nAssertionError: 1 != 2\n"
                r" : got poster\n\n----------\nRan 1 test\n'")
        self.fm("check", "add", f"test -f {marker} || {{ touch {marker}; {fail}; exit 1; }}")
        self.assertIn("  pkg.T.test_x: AssertionError: 1 != 2\n", self.fm("check").stdout)

    def test_a_failure_excerpt_ends_at_the_assertion_not_the_closing_rule(self):
        from sampo import core
        out = "FAIL: test_x (pkg.T.test_x)\n----------\nTraceback:\nAssertionError: 1 != 2\n\n----------\nRan 1 test\n"
        self.assertTrue(core.failure_excerpt(out, "test_x").endswith("AssertionError: 1 != 2"))

    def test_log_lines_are_not_test_names(self):
        from sampo import core as c
        self.assertEqual(c.failing_tests("ERROR: timeout (30)\nERROR: failed (retrying)\n"), [])
        self.assertEqual(c.failing_tests("FAIL: test_a (m.C.test_a) (i=1)\n--- FAIL: TestX (0.01s)\n"),
                         ["TestX", "m.C.test_a"])

    def test_a_fix_in_between_is_not_a_flake(self):
        self.fm("check", check=False)
        with open(os.path.join(self.repo, "fix.py"), "w") as f:
            f.write("x = 1\n")  # the tree changed: the pass is the fix, not luck
        open(self.flag, "w").close()
        self.assertNotIn("flaky", self.fm("check").stdout)


class Typos(ForemanTestCase):
    def test_a_parse_outside_main_still_gives_a_usage_error(self):  # found by fm task prove --hunks
        import contextlib
        import io
        from sampo import cli
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["tsk"])
        self.assertIn("did you mean task", err.getvalue())

    def test_nearest_subcommand_and_flag(self):
        self.fm("init")
        for argv, want in ((["tsk", "show", "T-0001"], "task"), (["task", "evidense", "T-1"], "evidence"),
                           (["check", "--evidance", "T-1"], "--evidence"), (["capture", "x", "--tyep", "FIX"], "--type"),
                           (["exam", "list"], "runs")):  # T-2735: "list" is nowhere near "runs" by spelling
            p = self.fm(*argv, check=False)
            self.assertNotEqual(p.returncode, 0)
            self.assertIn(f"did you mean {want}", p.stderr, argv)

    def test_a_mistyped_flag_is_matched_against_the_typed_commands_own_flags_only(self):
        # T-2475: fm night --print-timer suggested --print, which fm night doesn't take
        self.fm("init")
        p = self.fm("night", "--print-timer", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertNotIn("did you mean --print?", p.stderr)
        p = self.fm("night", "--instal", check=False)
        self.assertIn("did you mean --install", p.stderr)  # its own flag still comes back

    def test_a_folded_verb_names_its_new_home_and_runs_nothing(self):
        # T-2461: six verbs folded into the verbs that hold their job; the old name is a hint, never an alias (an
        # alias would carry user-only words past the guard's per-verb table, e.g. prove's --yes-run)
        self.fm("init")
        for old, home in (("proof", "prove --rollup"), ("calibration", "outcomes --judgment"),
                          ("incident", "capture --incident"), ("sweep", "projects --sweep"),
                          ("instruments", "which --instruments"), ("suspects", "whyred --suspects")):
            p = self.fm(old, "x", check=False)
            self.assertEqual(p.returncode, 2, old)
            self.assertIn(f"did you mean {home}?", p.stderr, old)

    def test_version_prints_the_plugin_version(self):
        # T-3612: --version and version were the top unknown_word guesses (21 in events.jsonl)
        import json
        from sampo import core as c
        with open(os.path.join(c.PLUGIN_ROOT, ".claude-plugin", "plugin.json"), encoding="utf-8") as f:
            want = json.load(f)["version"]
        for argv in (["--version"], ["version"]):
            p = self.fm(*argv, check=False)
            self.assertEqual(p.returncode, 0, (argv, p.stderr))
            self.assertIn(want, p.stdout, argv)

    def test_version_builds_no_parser(self):
        # T-3677: --version built all ~230 subparsers (~34 ms) to print one line
        import contextlib
        import io
        import json
        import sys
        from unittest import mock
        from sampo import cli
        with open(os.path.join(cli.c.PLUGIN_ROOT, ".claude-plugin", "plugin.json"), encoding="utf-8") as f:
            want = json.load(f)["version"]
        for argv in (["--version"], ["version"]):
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(cli, "_PARSER", None), mock.patch.dict(cli._PARTS, clear=True), \
                    mock.patch.object(cli, "_build_parser", side_effect=AssertionError("built a parser")), \
                    mock.patch.object(sys, "argv", ["/x/sampo"]), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    rc = cli.main(argv)
                except SystemExit as e:  # --version exits as argparse's version action did
                    rc = e.code
            self.assertEqual((rc or 0, out.getvalue()), (0, f"sampo {want}\n"), argv)

    def test_the_sampo_launcher_answers_version_without_importing_cli(self):
        # T-3692: importing sampo.cli cost ~40 ms of CPU in a fresh process to print one line
        import json
        import subprocess
        import sys
        import tempfile
        from helpers import PLUGIN
        with open(os.path.join(PLUGIN, ".claude-plugin", "plugin.json"), encoding="utf-8") as f:
            want = json.load(f)["version"]
        probe = ("import runpy, sys\nsys.argv = sys.argv[1:]\ntry:\n    runpy.run_path(sys.argv[0], run_name='__main__')\n"
                 "finally:\n    print('cli' if 'sampo.cli' in sys.modules else 'no cli')\n")
        env = {k: v for k, v in os.environ.items() if k != "SAMPO_LANE_CHILD"}
        with tempfile.TemporaryDirectory() as tmp:
            run = lambda *argv: subprocess.run([sys.executable, "-c", probe, *argv], capture_output=True, text=True,  # noqa: E731
                                               cwd=tmp, env=env, timeout=60)
            p = run(os.path.join(PLUGIN, "bin", "sampo"), "--version")
            self.assertEqual((p.returncode, p.stdout), (0, f"sampo {want}\nno cli\n"), p.stderr)
            link = os.path.join(tmp, "fm")  # another name: cli answers, saying the name back
            os.symlink(os.path.join(PLUGIN, "bin", "sampo"), link)
            p = run(link, "--version")
            self.assertEqual((p.returncode, p.stdout), (0, f"fm {want}\ncli\n"), p.stderr)

    def test_guessed_verbs_name_the_real_one_and_run_nothing(self):
        # T-3612: the other top unknown_word guesses in events.jsonl; each named the wrong verb by spelling
        self.fm("init")
        for argv, home in ((["models", "list"], "code models"),  # T-3742: bare `models` is a verb now
                           (["where"], "wiring"), (["task", "scope", "T-1"], "task set ID scope+=a,b"),
                           (["scope"], "task set ID scope+=a,b"), (["lane", "awaiting"], "next"),
                           (["affected"], "check --affected"), (["task", "approve", "T-1"], "task set ID approved=true")):
            p = self.fm(*argv, check=False)
            self.assertEqual(p.returncode, 2, argv)
            self.assertIn(f"did you mean {home}", p.stderr, argv)

    def test_common_guesses_run_the_real_command(self):
        # T-1180: builders and small models guess `fm task list`; the guess runs what it meant, with one quiet note
        self.fm("init")
        self.fm("task", "new", "Alpha", "--type", "FIX", "--tier", "S", "--step", "s", "--ac", "x :: true")
        for argv, ran in ((["task", "list"], "fm queue"), (["task", "ls"], "fm queue"), (["tasks"], "fm queue"),
                          (["ls"], "fm queue"), (["task", "get", "T-0001"], "fm task show"),
                          (["task", "view", "T-0001"], "fm task show"), (["lane", "ls"], "fm lane list"),
                          (["inbox", "list"], "fm inbox")):
            r = self.fm(*argv, check=False)
            self.assertEqual(r.returncode, 0, (argv, r.stderr))
            self.assertIn(f"(ran `{ran}", r.stderr, argv)
        self.assertIn("Alpha", self.fm("task", "view", "T-0001").stdout)
        self.assertNotEqual(self.fm("task", "nonsense", check=False).returncode, 0)

    def test_an_unknown_command_skips_the_list_of_every_command(self):
        # T-1170: argparse's "choose from" named all ~170 commands (2 KB of output for one typo)
        p = self.fm("tsk", check=False)
        self.assertIn("did you mean task", p.stderr)
        self.assertIn("fm help", p.stderr)
        self.assertNotIn("choose from", p.stderr)
        self.assertLess(len(p.stderr), 400)
        self.assertIn("choose from", self.fm("loop", "bogus", check=False).stderr, "a short list still helps")
