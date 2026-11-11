"""Evidence quality (T-0261): a run that proves nothing never counts (T-0255), the brief's assumptions say whether they
were checked (T-0254), and where the model of the code was wrong is logged and comes back (T-0253)."""
import json

from helpers import ForemanTestCase


class _Base(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.tid = json.loads(self.fm("task", "new", "Cache the session token", "--type", "FEATURE", "--tier", "M",
                                      "--ac", "it caches :: true", "--step", "do it",
                                      "--interpretation", "Reading 1: cache it. Reading 2: memoize it.", "--approach", "1) a; fails if: x 2) use what exists: b; fails if: y 3) do less: c; fails if: z Choice 1 (80%): a", "--json").stdout)["id"]


class Inconclusive(_Base):
    def test_an_inconclusive_run_never_counts(self):
        self.fm("focus", self.tid)
        self.fm("task", "evidence", self.tid, "--step", "1", "--run", "true", "--inconclusive")
        brief = self.fm("task", "show", self.tid).stdout
        self.assertIn("[inconclusive]", brief)
        p = self.fm("task", "step", self.tid, "done", "1", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("inconclusive", p.stderr)
        p = self.fm("task", "done", self.tid, "--lesson", "none: test", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("no verification evidence", p.stderr)

    def test_a_multi_line_command_is_one_evidence_line_that_keeps_its_exit(self):  # T-4116 (the JARVIS box's 9d09d60c)
        from sampo import core as c
        p = c.find_project(self.repo)
        b = c.find_brief(p, self.tid)
        b.add_evidence("python3 - <<'EOF'\nprint(1)\nEOF", c.run_result(0, "1"), step=1, ran=True)
        c.save_brief(p, b)
        lines = [ln for ln in c.find_brief(p, self.tid).evidence() if "print(1)" in ln]
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("→ exit 0", lines[0])

    def test_next_asks_for_a_sharper_check(self):
        self.fm("focus", self.tid)
        self.fm("task", "evidence", self.tid, "--step", "1", "--run", "true", "--inconclusive")
        self.assertIn("sharper check", self.fm("next").stdout)

    def test_a_failing_inconclusive_run_is_not_a_failed_run(self):
        self.fm("focus", self.tid)
        self.fm("task", "evidence", self.tid, "--step", "1", "--run", "false", "--inconclusive", check=False)
        self.fm("task", "evidence", self.tid, "--step", "1", "--run", "true")
        self.fm("task", "step", self.tid, "done", "1")


class Assume(_Base):
    def test_add_and_verify_set_the_tags(self):
        self.fm("task", "assume", self.tid, "add", "the token lives in auth.py")
        self.fm("task", "assume", self.tid, "add", "the cache is per process")
        self.fm("task", "assume", self.tid, "verify", "1", "--run", "true")
        self.fm("task", "assume", self.tid, "verify", "2", "--run", "false", check=False)
        section = self.fm("task", "show", self.tid).stdout.split("## Assumptions", 1)[1].split("\n## ", 1)[0]
        self.assertRegex(section, r"- \[verified: `true` → exit 0[^\]]*\] the token lives in auth.py")
        self.assertIn("[false: `false` → ✗ exit 1", section)
        self.assertIn("the cache is per process", section)

    def test_a_typed_verification_is_kept(self):
        self.fm("task", "set", self.tid, "--section", "Assumptions (confidence)", "--text", "- tests exist (high)")
        self.fm("task", "assume", self.tid, "verify", "1", "--evidence", "read plugin/tests/run.py:6")
        self.assertIn("- [verified: read plugin/tests/run.py:6] tests exist (high)", self.fm("task", "show", self.tid).stdout)

    def test_done_warns_on_unverified_assumptions(self):
        self.fm("task", "assume", self.tid, "add", "the token lives in auth.py")
        self.fm("focus", self.tid)
        self.fm("task", "evidence", self.tid, "--step", "1", "--run", "true")
        out = self.fm("task", "finish", self.tid, "--audit", "self", "--lens", "intent: ok", "--lens", "edge: ok",
                      "--docs", "none: test", "--lesson", "none: test", check=False)
        self.assertIn("unverified assumption", out.stdout + out.stderr)
        self.assertIn("the token lives in auth.py", out.stdout + out.stderr)

    def test_an_unknown_assumption_is_a_usage_error(self):
        p = self.fm("task", "assume", self.tid, "verify", "3", "--run", "true", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("no assumption 3", p.stderr)


class Surprise(_Base):
    def test_a_surprise_reaches_friction_and_recall(self):
        self.fm("focus", self.tid)
        self.fm("surprise", "the session token is cached per process → it is cached per request in middleware")
        self.assertIn("cached per request in middleware", self.fm("friction").stdout)
        out = self.fm("recall", "where is the session token cached").stdout
        self.assertIn("surprise", out)
        self.assertIn("cached per request in middleware", out)
        self.assertIn(self.tid, out)

    def test_an_unknown_task_is_refused(self):
        p = self.fm("surprise", "a → b", "--task", "T-9999", check=False)
        self.assertNotEqual(p.returncode, 0)

    def test_a_surprise_needs_text(self):
        p = self.fm("surprise", " ", check=False)
        self.assertNotEqual(p.returncode, 0)


class Bench(_Base):
    """T-2284: fm task evidence --bench times a command on the task's start tree and on this one."""

    def _commit(self, text):
        import os
        import subprocess
        with open(os.path.join(self.repo, "work.py"), "w") as f:
            f.write(text)
        subprocess.run(["git", "-C", self.repo, "add", "-A"], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", "w"], check=True)

    def _perf(self):
        return json.loads(self.fm("task", "new", "faster", "--type", "PERFORMANCE", "--tier", "S", "--ac", "fast :: true",
                                  "--step", "speed it up", "--json").stdout)["id"]

    def test_medians_before_and_after_are_recorded_and_finish_accepts_them(self):
        self._commit("import time\ntime.sleep(0.3)\n")
        tid = self._perf()
        self.fm("focus", tid)
        self._commit("pass\n")
        r = self.fm("task", "evidence", tid, "--step", "1", "--bench", "python3 work.py", "--repeat", "3")
        self.assertIn("(3 runs each)", r.stdout)
        shown = self.fm("task", "show", tid).stdout
        self.assertRegex(shown, r"python3 work.py: 0\.[3-9]\d*s → 0\.\d+s \(3 runs each\)")
        self.assertIn("(3 runs each)", shown.split("Measurements", 1)[1])
        self.fm("task", "finish", tid, "--audit", "read it")  # no --numbers: the bench is the numbers

    def test_a_failing_side_is_recorded_as_a_failure(self):
        self._commit("pass\n")
        tid = self._perf()
        self.fm("focus", tid)
        self._commit("raise SystemExit(3)\n")
        r = self.fm("task", "evidence", tid, "--bench", "python3 work.py", "--repeat", "2", check=False)
        self.assertEqual(r.returncode, 3)
        self.assertIn("failed on the current tree", r.stdout)

    def _tool_on_path(self, text):
        """A bin/tool in the repo, reached through a symlink in a PATH folder outside it (like ~/.local/bin/fm)."""
        import os
        import stat
        import subprocess
        os.makedirs(os.path.join(self.repo, "bin"), exist_ok=True)
        tool = os.path.join(self.repo, "bin", "tool")
        with open(tool, "w") as f:
            f.write(text)
        os.chmod(tool, os.stat(tool).st_mode | stat.S_IXUSR)
        subprocess.run(["git", "-C", self.repo, "add", "-A"], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", "t"], check=True)
        linkdir = os.path.join(self.home, "linkbin")
        os.makedirs(linkdir, exist_ok=True)
        if not os.path.islink(os.path.join(linkdir, "tool")):
            os.symlink(tool, os.path.join(linkdir, "tool"))
        return {"PATH": linkdir + os.pathsep + os.environ["PATH"]}

    def test_a_command_resolving_into_the_tree_runs_the_start_trees_copy_before(self):
        env = self._tool_on_path("#!/bin/sh\nexit 0\n")  # base copy: fine
        tid = self._perf()
        self.fm("focus", tid)
        self._tool_on_path("#!/bin/sh\necho CURRENT-COPY\nexit 3\n")
        for cmd in ("tool --x", "FOO=1 tool", "time tool", '"tool"', "tool | tool", "true && tool"):
            r = self.fm("task", "evidence", tid, "--bench", cmd, "--repeat", "1", check=False, env=env)
            self.assertEqual(r.returncode, 3, cmd + r.stdout + r.stderr)  # the before side passed: it ran the base copy
            self.assertIn("failed on the current tree", r.stdout, cmd)
            self.assertIn("CURRENT-COPY", r.stdout, cmd)

    def test_a_command_with_no_word_in_the_tree_says_it_may_time_the_same_code(self):
        import os
        import stat
        self._commit("pass\n")
        tid = self._perf()
        self.fm("focus", tid)
        other = self.repo + "-other"  # shares the repo's path as a string prefix, but is not inside it
        os.makedirs(other, exist_ok=True)
        with open(os.path.join(other, "tool"), "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(os.path.join(other, "tool"), 0o755 | stat.S_IXUSR)
        env = {"PATH": other + os.pathsep + os.environ["PATH"]}
        for cmd in ("tool", "python3 'unclosed"):
            r = self.fm("task", "evidence", tid, "--bench", cmd, "--repeat", "1", check=False, env=env)
            self.assertNotIn("Traceback", r.stderr)
        r = self.fm("task", "evidence", tid, "--bench", "tool", "--repeat", "1", env=env)
        self.assertIn("neither side's entry point is in the tree", r.stdout)

    def test_a_path_into_the_tree_as_an_argument_is_no_warning(self):
        self._commit("pass\n")
        tid = self._perf()
        self.fm("focus", tid)
        r = self.fm("task", "evidence", tid, "--bench", "python3 work.py", "--repeat", "1")
        self.assertIn("(1 runs each)", r.stdout)
        self.assertNotIn("neither side", r.stdout)

    def test_an_entry_point_new_in_the_task_says_the_before_side_ran_the_current_copy(self):
        self._commit("pass\n")
        tid = self._perf()
        self.fm("focus", tid)
        env = self._tool_on_path("#!/bin/sh\nexit 0\n")  # bin/tool is added after the start commit
        r = self.fm("task", "evidence", tid, "--bench", "tool", "--repeat", "1", env=env)
        self.assertIn("the start tree has no bin/tool: the before side ran the current copy", r.stdout)

    def test_a_command_in_the_tree_has_no_same_code_warning(self):
        env = self._tool_on_path("#!/bin/sh\nexit 0\n")
        tid = self._perf()
        self.fm("focus", tid)
        r = self.fm("task", "evidence", tid, "--bench", "tool", "--repeat", "1", env=env)
        self.assertNotIn("neither side", r.stdout)

    def test_no_base_commit_is_refused(self):
        tid = self._perf()  # never focused: no start commit
        r = self.fm("task", "evidence", tid, "--bench", "true", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("start commit", r.stderr)

    def test_bench_does_not_mix_with_run_or_a_huge_repeat(self):
        tid = self._perf()
        for extra in (("--run", "true"), ("--repeat", "999")):
            r = self.fm("task", "evidence", tid, "--bench", "true", *extra, check=False)
            self.assertNotEqual(r.returncode, 0)
