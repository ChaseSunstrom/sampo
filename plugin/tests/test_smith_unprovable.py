"""T-4292: a criterion whose tool cannot run in this sandbox (exam goal-c-0b1e05: AddressSanitizer cannot map its shadow
memory on the hardened kernel, and the model spent about 15 turns on linker flags) has a way out: the action
{"action": "unprovable", "criterion": N}. The model supplies no command. The HARNESS builds the probe from the criterion's
own command (the same program with its own trivial self-check: `--version`, `-h` of the module, a one-line program through
the same compiler flags), runs it in an empty folder of its own, and waives the criterion only when that self-check fails
AND fails the way the criterion's own command last failed (recall's failure signature). A self-check that passes means the
tool runs here and the failure is the work's: refused. A shape the harness cannot build a self-check for stays blocked and
is listed. The waiver is written in-process by the harness: no CLI action and no environment marker waives, so nothing a
shell can type (or a script it writes) can. A waived criterion is listed at the close as not verified, never as proved. One
waiver a criterion and two a run. And a criterion whose verify cannot fail ('true') is refused for every task type
(review finding 10 of the smith stack). Every model reply is scripted; the fixture tools are scripts on PATH."""
import json
import os
import types
from unittest import mock

import helpers  # noqa: F401  (puts the plugin's lib on the path)
from sampo import smith
from test_code import CLOSE, FIX, M_PLAN, PLAN, CodeCase, call, journal_text, plan, text_action
from test_smith_small import DONE, FIX_EDIT

MSG = "AddressSanitizer: failed to mmap shadow memory at 0x7fff8000"
# the fixture tools, as shell scripts on PATH. `sanitize` fails the same way for every argument (a tool that cannot run
# here); `worktool` is a healthy tool: its --version works and its check fails for the work's own reason; `gcc` fails only
# the sanitizer build, the way the hardened kernel does.
SANITIZE = f'#!/bin/sh\necho "{MSG}" >&2\nexit 1\n'
WORKTOOL = '#!/bin/sh\n[ "$1" = "--version" ] && { echo "worktool 1.0"; exit 0; }\necho "ERROR: assertion failed: calc" >&2\nexit 1\n'
GCC = (f'#!/bin/sh\nfor a in "$@"; do\n  if [ "$a" = "-fsanitize=address" ]; then echo "{MSG}" >&2; exit 1; fi\ndone\nexit 0\n')
RUN_SH = "#!/bin/sh\necho 'ERROR: assertion failed: tool' >&2\nexit 1\n"
SEED = ("seed the bug and a project script",
        CodeCase.repo_seed[1] + (("run.sh", RUN_SH), ("t.c", "int main(void){return 0;}\n")))

SOUND = {"text": "calc.add adds", "verify": "python3 test_calc.py"}
TOOL = {"text": "the sanitizer build runs clean", "verify": "sanitize check"}
TOOL2 = {"text": "the sanitizer build runs clean twice", "verify": "sanitize check2"}
TOOL3 = {"text": "the sanitizer build runs clean thrice", "verify": "sanitize check3"}
WORK = {"text": "the work tool passes", "verify": "worktool check"}
GONE = {"text": "the lost tool runs", "verify": "libless check"}
LIBLESS = "#!/bin/sh\necho 'libless: error while loading shared libraries: libgone.so.4: cannot open shared object file' >&2\nexit 127\n"
SCRIPT = {"text": "the project script passes", "verify": "sh run.sh"}
COMPILED = {"text": "the sanitized program runs", "verify": "gcc -fsanitize=address -o t t.c && ./t"}
UNITTEST = {"text": "the new test passes", "verify": "python3 -m unittest test_foo"}
TOOL_PLAN = dict(PLAN, criteria=[SOUND, TOOL])
EDIT = FIX[0]


def records(case, kind, tid="T-0001"):
    return [x for x in (json.loads(l) for l in journal_text(case, tid).splitlines()) if x.get("kind") == kind]


def unprovable(n=2, **more):
    return call("unprovable", criterion=n, **more)


class Unprovable(CodeCase):
    repo_seed = SEED

    def setUp(self):
        super().setUp()
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        for name, body in (("sanitize", SANITIZE), ("worktool", WORKTOOL), ("gcc", GCC), ("libless", LIBLESS)):
            path = os.path.join(self.bin, name)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
            os.chmod(path, 0o755)

    def go(self, *script, p=TOOL_PLAN, tools=True):
        f = self.serve("openai", plan(p), *script, tools=tools)
        r = self.code("fix add", "--model", "fake", "--json", check=False,
                      env={"PATH": self.bin + os.pathsep + os.environ["PATH"]})
        return f, r

    def closed(self, r):
        evs = [json.loads(l) for l in r.stdout.splitlines() if l.startswith("{")]
        return next(e for e in evs if e["event"] == "closed")

    def brief(self):
        with open(self.task()["path"], encoding="utf-8") as fh:
            return fh.read()

    # ---- the waiver -------------------------------------------------------------------------------------------------
    def test_waived_after_the_harness_self_check_fails_the_same_way_and_the_close_lists_it_as_not_verified(self):
        f, r = self.go(EDIT, unprovable(), call("done", summary="fixed; the sanitizer cannot run here"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.task()["status"], "done", r.stdout + r.stderr)
        end = self.closed(r)
        mine = [x for x in end["unproven"] if TOOL["text"] in x]
        self.assertEqual(len(mine), 1, end["unproven"])
        self.assertTrue(mine[0].startswith("not proven: "), mine)
        self.assertIn("waived", mine[0])
        self.assertIn("failed to mmap shadow memory", mine[0], "the self-check's last line is named")
        self.assertIn(TOOL["verify"], mine[0], "the criterion's command is named")
        self.assertFalse([x for x in end["unproven"] if SOUND["text"] in x], "the proved one is not in the list")
        self.assertIn("not proven: " + TOOL["text"], end["text"])
        self.assertNotIn("each criterion was proved", end["text"])

    def test_waived_is_noted_journaled_and_kept_in_the_brief_as_waived_never_as_met(self):
        f, r = self.go(EDIT, unprovable(), DONE)
        self.assertEqual(self.task()["status"], "done", r.stdout + r.stderr)
        got = records(self, "waive")
        self.assertEqual([(x["criterion"], x["probe"]) for x in got], [(2, "sanitize --version")], got)
        self.assertIn("failed to mmap shadow memory", got[0]["line"])
        self.assertTrue(any("waived" in e.get("text", "") and TOOL["text"] in e.get("text", "")
                            for e in map(json.loads, (l for l in r.stdout.splitlines() if l.startswith("{")))
                            if e["event"] == "note"), "a note says it when it happens")
        text = self.brief()
        self.assertIn("(ac 2)", text)
        self.assertIn("waived", text)
        self.assertNotIn("sanitize check` → exit 0", text, "the tool never ran green")

    def test_a_tool_that_cannot_load_its_library_is_sent_back_at_the_plan_gate_not_waived_later(self):
        # the plan gate already knows a missing library or program as the tool's own failure: no criterion, no waiver
        three = dict(PLAN, criteria=[SOUND, GONE])
        f, r = self.go(EDIT, unprovable(), DONE, p=three)
        self.assertIn("fails on its own", r.stdout)
        self.assertNotIn("waived", r.stdout)

    def test_waived_a_sanitizer_build_is_checked_with_a_one_line_program_through_the_same_flags(self):
        # the exam's case: the compiler works, the sanitized binary cannot run; a one-line program shows it
        f, r = self.go(EDIT, unprovable(), DONE, p=dict(PLAN, criteria=[SOUND, COMPILED]))
        self.assertEqual(self.task()["status"], "done", r.stdout + r.stderr)
        got = records(self, "waive")
        self.assertEqual([x["criterion"] for x in got], [2], got)
        self.assertIn("-fsanitize=address", got[0]["probe"])
        self.assertIn("failed to mmap shadow memory", got[0]["line"])

    def test_waived_a_model_without_tools_asks_in_text(self):
        f, r = self.go(EDIT_TEXT, text_action("unprovable", criterion=2), DONE_TEXT, tools=False)
        self.assertEqual(self.task()["status"], "done", r.stdout + r.stderr)
        self.assertEqual(len(records(self, "waive")), 1)

    def test_waived_the_prompt_lists_the_action_and_asks_for_no_command(self):
        f, r = self.go(EDIT, unprovable(), DONE)
        first = next(x for x in f.seen if "Now do step 1" in json.dumps(x["messages"]))
        sent = json.dumps(first)
        self.assertIn("unprovable", sent)
        self.assertNotIn("probe", sent, "the model gives the criterion's number, never a command")

    def test_waived_the_plan_prompt_names_what_the_sandbox_lacks_when_a_before_run_shows_it(self):
        f, r = self.go(EDIT, unprovable(), DONE)
        step = next(x for x in f.seen if "Now do step 1" in json.dumps(x["messages"]))
        sent = json.dumps(step["messages"])
        self.assertIn("failed to mmap shadow memory", sent, "the failing criterion's last line is shown")
        self.assertIn("unprovable", sent)

    def test_waived_a_command_the_model_sends_is_never_run(self):
        # the model names no command; one it sends anyway is ignored, not run, and grants nothing of its own
        mark = os.path.join(self.repo, "ran-by-the-model")
        f, r = self.go(EDIT, unprovable(probe=f"touch {mark}"), DONE)
        self.assertFalse(os.path.exists(mark))
        self.assertEqual([x["probe"] for x in records(self, "waive")], ["sanitize --version"])

    # ---- the refusals -----------------------------------------------------------------------------------------------
    def refused(self, plan_, n, word, why_n=3):
        """A refused claim, then a grant: the model is told why, the run goes on."""
        f, r = self.go(EDIT, unprovable(n), unprovable(why_n), DONE, p=plan_)
        sent = json.dumps(f.seen)
        self.assertIn("unprovable refused", sent)
        self.assertIn(word, sent, "the reason names what was wrong")
        self.assertEqual([x["criterion"] for x in records(self, "waive")], [why_n], "only the sound claim was granted")
        return f, r

    def test_refused_when_the_tool_runs_here_and_the_work_is_what_fails(self):
        f, r = self.refused(dict(PLAN, criteria=[SOUND, WORK, TOOL]), 2, "runs here")
        self.assertNotEqual(self.task()["status"], "done", "the work's criterion is still red")

    def test_refused_a_test_module_never_written_is_the_work_not_the_tool(self):
        # review round 3, finding B: `python3 -m unittest test_foo` with no test_foo was waived by a model-chosen probe
        f, r = self.refused(dict(PLAN, criteria=[SOUND, UNITTEST, TOOL]), 2, "runs here")
        self.assertNotEqual(self.task()["status"], "done")

    def test_refused_a_project_script_has_no_self_check_and_stays_blocked_and_listed(self):
        f, r = self.refused(dict(PLAN, criteria=[SOUND, SCRIPT, TOOL]), 2, "stays blocked")
        self.assertNotEqual(self.task()["status"], "done", "the script's criterion is still red: never closed as proved")
        self.assertIn("listed for the user", json.dumps(f.seen), "the model is told the criterion stays blocked and is listed")
        self.assertNotIn(2, [x["criterion"] for x in records(self, "waive")], "the script criterion is never waived")

    def test_refused_for_a_criterion_that_does_not_exist(self):
        self.refused(TOOL_PLAN, 9, "no criterion 9", why_n=2)

    def test_refused_for_a_criterion_that_never_failed(self):
        # criterion 1 failed before the work; once it passes its failure is no longer on record
        f, r = self.go(EDIT, call("run", cmd=SOUND["verify"]), unprovable(1), unprovable(), DONE)
        self.assertIn("no failed run on record", json.dumps(f.seen), r.stdout[-2500:] + r.stderr)
        self.assertEqual([x["criterion"] for x in records(self, "waive")], [2])

    def test_refused_a_second_waiver_of_the_same_criterion(self):
        f, r = self.go(EDIT, unprovable(), unprovable(), DONE)
        self.assertEqual(len(records(self, "waive")), 1)
        self.assertIn("already waived", json.dumps(f.seen))
        self.assertEqual(self.task()["status"], "done", r.stdout + r.stderr)

    def test_refused_a_third_waiver_in_one_run(self):
        four = dict(PLAN, criteria=[SOUND, TOOL, TOOL2, TOOL3])
        f, r = self.go(EDIT, unprovable(2), unprovable(3), unprovable(4), DONE, p=four)
        self.assertEqual([x["criterion"] for x in records(self, "waive")], [2, 3], "two a run")
        self.assertIn("two waivers", json.dumps(f.seen))
        self.assertNotEqual(self.task()["status"], "done", "the third criterion is still red: not closed as proved")

    def test_refused_a_refused_claim_changes_nothing_in_the_brief(self):
        f, r = self.go(EDIT, unprovable(2), DONE, p=dict(PLAN, criteria=[SOUND, WORK]))
        self.assertNotIn("waived", self.brief())
        self.assertEqual(records(self, "waive"), [])

    def test_refused_every_counted_refusal_is_journaled(self):
        f, r = self.go(EDIT, unprovable(2), unprovable(2), unprovable(3), DONE, p=dict(PLAN, criteria=[SOUND, WORK, TOOL]))
        self.assertEqual(len(records(self, "waive_refused")), 2, "a self-check that ran and a claim the rules refused")

    # ---- finding A: nothing a shell can type waives; the harness writes the waiver in-process -------------------------------
    def test_a_script_the_model_writes_and_runs_cannot_waive(self):
        # review round 3, finding A: `bash w.sh` held the typed command the guard's string match never saw
        script = ("FM_CODE_WAIVE=T-0001:2 python3 plugin/bin/sampo task ac T-0001 waive 2 --evidence x "
                  f"'{MSG}'\nX=FM_CODE_; env \"$X\"WAIVE=T-0001:2 python3 plugin/bin/sampo task ac T-0001 waive 2 "
                  f"--evidence x '{MSG}'\n")
        f, r = self.go(EDIT, call("write", file="w.sh", content=script), call("run", cmd="bash w.sh"), DONE)
        self.assertEqual(records(self, "waive"), [])
        self.assertNotIn("→ waived", self.brief())
        self.assertNotEqual(self.task()["status"], "done", "the criterion is still red")

    def test_a_typed_waive_has_no_action_to_run_and_no_environment_marker(self):
        f, r = self.go(EDIT, DONE)
        before = self.brief()
        for env in (None, {"FM_CODE_WAIVE": "T-0001:2"}):
            got = self.fm("task", "ac", "T-0001", "waive", "2", "--evidence", "sanitize --version", MSG, env=env, check=False)
            self.assertNotEqual(got.returncode, 0, got.stdout)
            self.assertEqual(self.brief(), before, "a refused waive writes nothing")
        from sampo import cli
        self.assertNotIn("FM_CODE_WAIVE", open(cli.__file__, encoding="utf-8").read(), "no marker is read anywhere")
        self.assertNotIn("FM_CODE_WAIVE", open(smith.__file__, encoding="utf-8").read())

    def test_the_waiver_is_written_by_the_harness_in_process(self):
        with mock.patch.object(smith.Run, "fm", side_effect=AssertionError("no CLI call may write the waiver")) as fm:
            fm.side_effect = lambda *a, **k: (0, "") if a[:2] == ("task", "evidence") else self.fail(f"fm {a}")
            f, r = self.go(EDIT, unprovable(), DONE)
        self.assertEqual(len(records(self, "waive")), 1)

    # ---- the rule, on its own: what self-check the harness builds for a command ---------------------------------------
    ASAN = ("gcc -fsanitize=address -o t t.c\n./t\n==41==AddressSanitizer: failed to mmap shadow memory at 0x7ff0\n"
            "make: *** [Makefile:3: test-asan] Error 1\n")

    def checks(self, verify, out=""):
        return [c for c, _ in smith._self_checks(verify, out)]

    def test_self_checks_an_interpreter_module_is_asked_for_its_help_a_script_for_the_interpreters_version(self):
        self.assertEqual(self.checks("python3 -m pytest tests/x.py -x"), ["python3 -m pytest -h"])
        self.assertEqual(self.checks("python3 check.py"), ["python3 --version"])
        self.assertEqual(self.checks("python3 -c 'import calc'"), ["python3 --version"])
        self.assertEqual(self.checks("node build.js"), ["node --version"])

    def test_self_checks_a_bare_tool_is_asked_for_its_version_past_wrappers_env_pipes_and_chains(self):
        self.assertEqual(self.checks("pytest -x | tail -3"), ["pytest --version"])
        self.assertEqual(self.checks("FOO=1 timeout 5 pytest"), ["pytest --version"])
        self.assertEqual(self.checks("cd build && ctest --output-on-failure"), ["ctest --version"])
        self.assertEqual(self.checks("go test ./..."), ["go version"])
        self.assertEqual(self.checks("pytest 2>&1 >/dev/null || cargo test"), ["pytest --version", "cargo --version"])

    def test_self_checks_a_project_file_or_a_shell_or_a_builtin_has_none(self):
        for verify in ("./run.sh", "sh run.sh", "bash -c 'x'", "echo ok", "test -f calc.py", "/opt/tool/bin/check", "./build/t",
                       "python3 -m unittest -h >/dev/null; ./t", ""):
            self.assertEqual([c for c in self.checks(verify) if "unittest" not in c and "python3" not in c], [], verify)
        self.assertEqual(self.checks("./run.sh"), [])
        self.assertEqual(self.checks("sh run.sh"), [])

    def test_self_checks_a_sanitized_build_is_compiled_from_a_one_line_program_with_the_same_flags(self):
        got = smith._self_checks("gcc -fsanitize=address -o t t.c && ./t", "")
        cmds = [c for c, _ in got]
        self.assertEqual(cmds[0], "gcc --version")
        self.assertEqual(cmds[1], "gcc -fsanitize=address -x c - -o t && ./t")
        self.assertIn("main", got[1][1], "the one-line program goes in on stdin")
        self.assertIn("-x c++", self.checks("g++ -fsanitize=thread -O1 x.cpp")[1])
        self.assertIn("-fsanitize=address,undefined", self.checks("clang -fsanitize=address,undefined a.c")[1])
        self.assertNotIn("a.c", self.checks("clang -fsanitize=address,undefined a.c")[1], "none of the criterion's own files")

    def test_self_checks_a_make_whose_failure_names_a_sanitizer_gets_the_compiler_check_too(self):
        got = self.checks("make test-asan", self.ASAN)
        self.assertEqual(got[0], "make --version")
        self.assertEqual(got[1], "cc -fsanitize=address -x c - -o t && ./t")
        self.assertEqual(self.checks("make test"), ["make --version"], "no sanitizer named, none built")

    def test_self_checks_are_few_and_the_program_names_are_plain(self):
        self.assertLessEqual(len(self.checks("a1 && b2 && c3 && d4 && e5 && f6 && g7")), 4)
        for verify in ("'$(touch x)' --run", "`id` check", "foo\\;bar x", "-rf /"):
            self.assertEqual(self.checks(verify), [], verify)

    # ---- the verdict, on its own ---------------------------------------------------------------------------------------
    def why(self, out, *results):
        return smith._selfcheck_why(out, list(results))

    def test_verdict_waived_when_the_self_check_fails_the_way_the_command_did(self):
        why, cmd, line = self.why(self.ASAN, ("gcc --version", 0, "gcc 13\n"),
                                  ("cc -fsanitize=address -x c - -o t && ./t", 1,
                                   "==7==AddressSanitizer: failed to mmap shadow memory at 0x7ff8\n"))
        self.assertEqual(why, "")
        self.assertIn("-fsanitize", cmd)
        self.assertIn("failed to mmap", line)

    def test_verdict_a_missing_command_matches_whichever_way_the_shell_prefixes_it(self):
        why = self.why("bash: line 1: nosuch: command not found\n", ("nosuch --version", 127, "bash: nosuch: command not found\n"))[0]
        self.assertEqual(why, "")

    def test_verdict_refused_when_every_self_check_passes(self):
        self.assertIn("runs here", self.why(self.ASAN, ("make --version", 0, "GNU Make 4\n"))[0])

    def test_verdict_refused_a_timeout_shows_nothing(self):
        self.assertIn("timed out", self.why(self.ASAN, ("make --version", None, ""))[0])

    def test_verdict_refused_a_self_check_that_fails_differently(self):
        got = self.why(self.ASAN, ("cc -fsanitize=address -x c - -o t", 1, "cc: error: no input files\n"))[0]
        self.assertIn("differently", got)

    def test_verdict_refused_digits_alone_and_silence_are_no_signature(self):
        self.assertTrue(self.why(self.ASAN, ("x --version", 1, "12345\n"))[0])
        self.assertTrue(self.why(self.ASAN, ("x --version", 1, ""))[0])

    def test_verdict_refused_a_missing_python_module_may_be_the_projects_own_unwritten_one(self):
        got = self.why("ModuleNotFoundError: No module named 'foo'\n",
                       ("python3 -m foo -h", 1, "/usr/bin/python3: No module named foo\n"))[0]
        self.assertIn("No module", got)

    def test_verdict_refused_no_failure_on_record_means_nothing_to_compare(self):
        self.assertIn("run it", self.why("", ("make --version", 1, "make: error\n"))[0])

    def test_verdict_refused_no_self_check_to_build_stays_blocked(self):
        self.assertIn("stays blocked", self.why(self.ASAN)[0])

    # ---- the criterion's own missing target is the work, not the tool ---------------------------------------------------
    def missing(self, verify, out):
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, "sanitize.py"), "w") as fh:
                fh.write("x = 1\n")
            return smith._missing_target(verify, out, root)

    def test_missing_target_a_criterion_whose_file_was_never_created_is_work_not_a_tool(self):
        got = self.missing("python3 -m pytest tests/test_new.py", "ERROR: file or directory not found: tests/test_new.py\n")
        self.assertIn("create", got)
        self.assertIn("tests/test_new.py", got)
        self.assertIn("create", self.missing("python3 check.py", "python3: can't open file '/work/check.py': No such file\n"))

    def test_missing_target_a_file_that_is_there_lets_the_tool_through(self):
        self.assertEqual(self.missing("python3 sanitize.py check", f"{MSG}\n"), "")

    def test_missing_target_end_to_end_a_script_never_created_is_not_waived(self):
        gone = {"text": "the new check runs", "verify": "python3 gone.py"}
        f, r = self.go(EDIT, unprovable(2), unprovable(3), DONE, p=dict(PLAN, criteria=[SOUND, gone, TOOL]))
        self.assertIn("unprovable refused", json.dumps(f.seen))
        self.assertEqual([x["criterion"] for x in records(self, "waive")], [3], "only the tool that exists was waived")
        self.assertNotEqual(self.task()["status"], "done", "the missing file is still red")

    # ---- finding 5: a waived criterion is never one a command of its own proved ------------------------------------------
    def test_waived_a_failed_baseline_run_plus_a_waiver_is_listed_not_proved_by_the_finish(self):
        from sampo import cli, core
        with open(os.path.join(self.repo, "fail.py"), "w") as fh:
            fh.write("import sys\nsys.stderr.write('boom failed here\\n')\nsys.exit(1)\n")
        self.fm("task", "new", "waive close", "--type", "FIX", "--tier", "S", "--ac", "it runs :: python3 fail.py",
                "--step", "run it", "--focus")
        tid = next(t for t in ("T-0001", "T-0002") if self.fm("task", "show", t, check=False).returncode == 0)
        self.fm("task", "evidence", tid, "--ac", "1", "--run", "python3 fail.py", check=False)
        self.fm("task", "evidence", tid, "--step", "1", "--run", "python3 -c pass", check=False)
        cli.mutate(core.find_project(self.repo), tid, lambda b: b.waive_ac(1, "fail --version", "boom failed here"),
                   "ac_waive", {"ac": 1})
        self.fm("task", "set", tid, "--section", "Regression test", "--text", "none: the criterion is waived", check=False)
        got = self.fm("task", "finish", tid, "--audit", "by hand", check=False)
        text = got.stdout + got.stderr
        self.assertNotIn("each criterion was proved by its own check", text, text)
        self.assertIn("criterion 1", text, text)
        self.assertIn("waived", text, text)

    def test_waived_the_brief_wont_waive_a_criterion_with_no_failed_run_on_record(self):
        from sampo import cli, core
        self.fm("task", "new", "waive none", "--type", "FIX", "--tier", "S", "--ac", "it runs :: python3 test_calc.py",
                "--step", "run it", "--focus")
        tid = next(t for t in ("T-0001", "T-0002") if self.fm("task", "show", t, check=False).returncode == 0)
        with self.assertRaises(core.PolicyError):
            cli.mutate(core.find_project(self.repo), tid, lambda b: b.waive_ac(1, "x --version", "boom failed"),
                       "ac_waive", {"ac": 1})

    # ---- finding 6: a resume and a restart keep the count --------------------------------------------------------------
    def test_refused_probes_are_replayed_so_a_resume_keeps_the_count(self):
        import tempfile
        recs = [{"kind": "start", "task": "T-0001", "plan": dict(PLAN, criteria=[SOUND, TOOL]), "request": "x", "options": {}}]
        recs += [{"kind": "waive_refused", "criterion": 2, "probe": "sanitize --version", "why": "w"}] * 4
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "T-0001.jsonl"), "w", encoding="utf-8") as fh:
                fh.write("".join(json.dumps(x) + "\n" for x in recs))
            run = smith.Run(types.SimpleNamespace(escalate=None, max_steps=None), tmp, tmp)
            smith._load(run, "T-0001")
        self.assertEqual(run.waive_tries, 4)

    def run_with_brief_waivers(self, waived):
        """A Run of tool criteria whose brief already holds `waived` ({n: evidence line}); the self-check fails the same way."""
        run = smith.Run(types.SimpleNamespace(escalate=None, max_steps=None), "/nonexistent", "/nonexistent")
        run.tid, run.root = "T-0001", "/nonexistent"
        run.plan = dict(PLAN, criteria=[SOUND, TOOL, TOOL2, TOOL3])
        run.guard = lambda *a, **k: None
        run.fm = lambda *a, **k: (0, "")
        for n in (2, 3, 4):
            run.last_fail[n] = (run.plan["criteria"][n - 1]["verify"], f"{MSG}\n")
        brief = types.SimpleNamespace(waived_acs=lambda: waived)
        patches = [mock.patch.object(smith.core, "find_project", return_value=object()),
                   mock.patch.object(smith.core, "find_brief", return_value=brief),
                   mock.patch.object(smith, "run_bounded", return_value=(1, f"{MSG}\n")),
                   mock.patch.object(smith, "_missing_target", return_value="")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return run

    def test_refused_a_restart_does_not_reset_the_waivers_the_brief_already_holds(self):
        run = self.run_with_brief_waivers({1: "x", 2: "y"})
        got = run.unprovable({"criterion": 3})
        self.assertIn("unprovable refused", got)
        self.assertIn("two waivers", got)

    def test_refused_a_restart_still_knows_the_criterion_the_brief_waived(self):
        run = self.run_with_brief_waivers({2: "y"})
        got = run.unprovable({"criterion": 2})
        self.assertIn("already waived", got)

    def test_refused_a_restart_does_not_reset_the_claims_counter_it_is_seeded_from_the_journal(self):
        lines = [json.dumps({"kind": "waive_refused", "criterion": 2, "probe": "p", "why": "w"}) + "\n"] * 4
        lines += [json.dumps({"kind": "waive", "criterion": 2, "probe": "p", "line": "l"}) + "\n",
                  json.dumps({"kind": "note", "text": "waive_refused"}) + "\n", "not json waive\n",
                  json.dumps({"kind": "waive_refused", "criterion": 3}) + "\n"]
        self.assertEqual(smith._claims_in(lines), 6, "the claims that reached a self-check count; noise does not")
        self.assertEqual(smith._claims_in([]), 0)

    def test_refused_a_restart_end_to_end_the_set_aside_runs_claims_are_still_spent(self):
        from test_code_restart import code_dir
        tid = json.loads(self.fm("task", "new", "make the sanitizer run", "--type", "FIX", "--tier", "S", "--json",
                                 "--step", "fix it", "--ac", f"the sanitizer runs :: {TOOL['verify']}").stdout)["id"]
        jd = code_dir(self, tid)
        os.makedirs(jd, exist_ok=True)
        recs = [{"kind": "start", "task": tid}] + [{"kind": "waive_refused", "criterion": 1, "probe": "p", "why": "w"}] * 6
        with open(os.path.join(jd, f"{tid}.jsonl"), "w", encoding="utf-8") as fh:
            fh.write("".join(json.dumps(x) + "\n" for x in recs))
        f = self.serve("openai", unprovable(1), DONE)
        self.code("--task", tid, "--model", "fake", "--restart", "--json", check=False,
                  env={"PATH": self.bin + os.pathsep + os.environ["PATH"]})
        self.assertIn("6 claims were checked this run; no more", json.dumps(f.seen))

    # ---- round 4: a command that is not found is a wrong name, unless the brief held the criterion before the run -------
    ZZ = {"text": "the invented check runs", "verify": "zzcheck --all"}
    NOT_FOUND = "bash: line 1: zzcheck: command not found\n"

    def test_unknown_command_the_plan_gate_refuses_a_verify_whose_program_is_on_no_path(self):
        f, r = self.go(EDIT, unprovable(2), DONE, p=dict(PLAN, criteria=[SOUND, self.ZZ]))
        sent = json.dumps(f.seen)  # the planner is sent the refusal back
        self.assertIn("unknown command: check the name or use a tool that is installed", sent)
        self.assertIn("runs `zzcheck`", sent)
        self.assertNotIn("waived", r.stdout)

    def test_unknown_command_a_planned_criterion_is_never_waived_even_past_the_gate(self):
        run = self.run_with_brief_waivers({})
        run.plan = dict(PLAN, criteria=[SOUND, self.ZZ])
        run.last_fail[2] = (self.ZZ["verify"], self.NOT_FOUND)
        with mock.patch.object(smith, "run_bounded", return_value=(127, self.NOT_FOUND)):
            got = run.unprovable({"criterion": 2})
        self.assertIn("unprovable refused", got)
        self.assertIn("unknown command: check the name or use a tool that is installed", got)
        self.assertEqual(run.waived, {})

    def test_unknown_command_a_planned_criterion_whose_failure_says_not_found_is_refused_for_a_name_that_exists(self):
        run = self.run_with_brief_waivers({})
        run.plan = dict(PLAN, criteria=[SOUND, {"text": "t", "verify": "python3 inner.py"}])
        run.last_fail[2] = ("python3 inner.py", "sh: 1: gone: command not found\n")
        with mock.patch.object(smith, "run_bounded", return_value=(127, "sh: 1: gone: command not found\n")):
            got = run.unprovable({"criterion": 2})
        self.assertIn("unknown command", got)

    def test_unknown_command_a_criterion_the_brief_held_before_the_run_is_still_waivable(self):
        run = self.run_with_brief_waivers({})
        run.own_plan = True
        run.plan = dict(PLAN, criteria=[SOUND, self.ZZ])
        run.last_fail[2] = (self.ZZ["verify"], self.NOT_FOUND)
        with mock.patch.object(smith, "run_bounded", return_value=(127, self.NOT_FOUND)), \
                mock.patch("sampo.cli.mutate"):
            got = run.unprovable({"criterion": 2})
        self.assertNotIn("unknown command", got, "the brief's own criterion is the user's: the tool's absence is the tool's")
        self.assertIn("criterion 2 is waived", got)

    def test_unknown_command_end_to_end_a_brief_criterion_of_a_missing_tool_is_waived_and_listed(self):
        tid = json.loads(self.fm("task", "new", "make calc.add add", "--type", "FIX", "--tier", "S", "--json",
                                 "--step", "fix calc.add", "--ac", f"calc.add adds :: {SOUND['verify']}",
                                 "--ac", f"the lost check runs :: {self.ZZ['verify']}").stdout)["id"]
        f = self.serve("openai", call("run", cmd=self.ZZ["verify"]), unprovable(2), EDIT, DONE)
        self.code("--task", tid, "--model", "fake", "--json", check=False,
                  env={"PATH": self.bin + os.pathsep + os.environ["PATH"]})
        self.assertEqual([x["criterion"] for x in records(self, "waive", tid)], [2], json.dumps(f.seen)[-600:])

    def test_the_audit_turns_text_marks_a_waived_criterion_so_a_lens_never_reads_it_as_proved(self):
        run = types.SimpleNamespace(plan=dict(PLAN, criteria=[SOUND, TOOL]), own_plan=True, tid="T-0001", request="r",
                                    waived={2: {"probe": "p", "line": "l"}}, context=None, small=False)
        with mock.patch.object(smith, "_context_note", return_value=""):
            text = smith._task_text(run)
        mine = [x for x in text.splitlines() if TOOL["text"] in x]
        self.assertIn("waived", mine[0])
        self.assertIn("not proved", mine[0])
        self.assertNotIn("waived", [x for x in text.splitlines() if SOUND["text"] in x][0])

    def test_unknown_program_reads_the_first_word_of_each_command_by_the_sandboxs_own_lookup(self):
        path = os.defpath + os.pathsep + "/usr/bin" + os.pathsep + "/bin"
        for ok in ("python3 test_calc.py", "test -f calc.py && python3 -V", "cd sub && sh run.sh", "FOO=1 python3 -V",
                   "timeout 5 python3 -V", "./run.sh", "bin/tool check", "echo hi | cat", "for f in a b; do echo $f; done",
                   "chk() { echo ok; }; chk", "if [ -f a ]; then echo a; fi", "$TOOL check", "python3 -c 'zzcheck'"):
            self.assertEqual(smith._unknown_program(ok, path), "", ok)
        for bad, name in (("zzcheck --all", "zzcheck"), ("cd sub && zzcheck", "zzcheck"), ("python3 -V; zzcheck",
                          "zzcheck"), ("FOO=1 zzcheck x", "zzcheck"), ("python3 -V | zzcheck", "zzcheck")):
            self.assertEqual(smith._unknown_program(bad, path), name, bad)


EDIT_TEXT = text_action("edit", file="calc.py", old="a - b", new="a + b")
DONE_TEXT = text_action("done", summary="fixed; the sanitizer cannot run here")


# ---- a verify that cannot fail, for every task type ---------------------------------------------------------------------
CLAUSE_ASK = "fix add, and keep the tests as they are"
CLAUSE_WHY = 'fm focus refused the plan: a criterion for "keep the tests as they are" (sampo task ac T-0001 add ...)'
TYPES = ("FIX", "FEATURE", "CLEAN", "PERFORMANCE", "SECURITY", "RESEARCH")


def piece(kind, reply):
    """smith._piece on a plan of this type, the planner's reply to the clause question being `reply`."""
    pl = dict(M_PLAN, type=kind)
    run = types.SimpleNamespace(plan=pl, request=CLAUSE_ASK, emit=mock.Mock(), plan_check=smith.check_plan, root=".")
    msgs = [{"role": "user", "content": "x"}]
    with mock.patch.object(smith, "_ask", return_value={"text": reply}):
        return smith._piece(run, mock.Mock(), msgs, CLAUSE_WHY)


class CannotFail(CodeCase):
    def test_cannot_fail_a_true_verify_for_a_clause_is_refused_for_every_task_type(self):
        for kind in TYPES:
            self.assertIsNone(piece(kind, "0\nthe test file is unchanged :: true"), kind)

    def test_cannot_fail_the_plan_gate_refuses_it_for_every_task_type_with_or_without_a_change_word(self):
        # review round 3, finding C: the gate applied the rule only when the request asked for a change
        for kind in TYPES:
            pl = dict(PLAN, type=kind, criteria=[dict(SOUND, verify="true")], steps=["do it"])
            for change in (None, "", "fix", "add"):
                if kind == "RESEARCH" and change:
                    continue  # a RESEARCH plan for a request that asks for a change is refused for that, before the verify
                self.assertIn("can't fail", smith.check_plan(pl, change) or "", (kind, change))
            self.assertIsNone(smith.check_plan(dict(pl, criteria=[SOUND]), None), kind)

    def test_cannot_fail_the_other_commands_that_cannot_fail_are_refused_too(self):
        for verify in ("echo ok", "python3 test_calc.py; exit 0", "python3 test_calc.py | tail -1", ":"):
            self.assertIsNone(piece("CLEAN", f"0\nthe test file is unchanged :: {verify}"), verify)

    def test_cannot_fail_every_segment_is_read_not_only_the_last_after_a_semicolon(self):
        # review round 1, finding 4: segments joined by && are seen too
        for verify in ("true && echo ok", "cd . && echo ok", "/bin/true", "test 1", "[ 1 ]", "python3 -c pass",
                       "echo a && echo b", "/usr/bin/env true", "true || false", "exit 0 && echo done",
                       "sh -c true", "bash -c 'echo ok'", "sh -c 'true && echo done'", "(exit 0)", "(true)", "( echo ok )"):
            self.assertTrue(smith._cant_fail(verify), verify)
            self.assertIsNone(piece("CLEAN", f"0\nthe test file is unchanged :: {verify}"), verify)

    def test_cannot_fail_a_chain_with_a_real_check_in_it_stays_allowed(self):
        for verify in ("make && true", "python3 test_calc.py && echo ok", "cd src && python3 test_calc.py",
                       "test -f calc.py", "[ -f calc.py ]", "python3 -c 'assert 1 == 2'", "true && python3 test_calc.py",
                       "python3 test_calc.py || true && false", "make | tail -1 && pytest",
                       "test_calc.py && exit 1 || exit 0", "sh -c 'exit 1'", "sh run.sh", "(exit 1)", "(python3 test_calc.py)",
                       "bash -c 'python3 test_calc.py'", "sh -c 'true && python3 test_calc.py'"):
            self.assertFalse(smith._cant_fail(verify), verify)

    def test_cannot_fail_pipefail_makes_a_pipe_into_tail_a_real_check(self):
        # review round 3, finding C: `set -o pipefail; pytest | tail` fails when pytest does
        for verify in ("set -o pipefail; python3 test_calc.py | tail -3", "set -euo pipefail; pytest | tail",
                       "set -o pipefail && make | tee build.log", "set -o pipefail\npytest -x | tail -5"):
            self.assertFalse(smith._cant_fail(verify), verify)
        for verify in ("python3 test_calc.py | tail -3", "set -e; pytest | tail", "set -o pipefail; echo ok | tail"):
            self.assertTrue(smith._cant_fail(verify), verify)

    def test_cannot_fail_a_real_check_is_still_accepted_for_every_task_type(self):
        for kind in TYPES:
            got = piece(kind, "0\nthe test file is unchanged :: python3 test_calc.py")
            self.assertIsNotNone(got, kind)
            self.assertEqual(got["criteria"][-1]["verify"], "python3 test_calc.py", kind)

    def test_cannot_fail_the_old_question_asked_once_is_checked_the_same_way(self):
        # an unreadable first reply asks the old question; its 'what :: check' line is the same piece
        run = types.SimpleNamespace(plan=dict(M_PLAN, type="CLEAN"), request=CLAUSE_ASK, emit=mock.Mock(),
                                    plan_check=smith.check_plan, root=".")
        replies = iter([{"text": "I am not sure"}, {"text": "the test file is unchanged :: true"}])
        with mock.patch.object(smith, "_ask", side_effect=lambda *a, **k: next(replies)):
            self.assertIsNone(smith._piece(run, mock.Mock(), [{"role": "user", "content": "x"}], CLAUSE_WHY))

    def test_cannot_fail_end_to_end_the_run_replans_instead_of_taking_a_true_criterion(self):
        clean = dict(M_PLAN, type="CLEAN")
        again = dict(clean, criteria=clean["criteria"] + [
            {"text": "keep the tests as they are: test_calc.py is unchanged", "verify": "python3 test_calc.py"}])
        f = self.serve("anthropic", plan(clean), {"text": "0\nthe test file is unchanged :: true"}, plan(again),
                       FIX_EDIT, DONE, CLOSE)
        r = self.code("fix add, and keep the tests as they are", "--model", "fake", check=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("The plan gate refused this plan", f.said(2), "the whole re-plan, not the 'true' criterion")
        for t in ("T-0001", "T-0002", "T-0003"):
            if self.fm("task", "show", t, check=False).returncode == 0:
                with open(self.task(t)["path"], encoding="utf-8") as fh:
                    self.assertNotIn("`true`", fh.read(), t)
