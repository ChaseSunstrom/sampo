"""Round I (T-0082 brainstorm): literal-aware sensitive(), section aliases and kept ticks, check timeouts, one-call
planning."""
from helpers import ForemanTestCase

from sampo import core as c


class RoundI(ForemanTestCase):
    def test_sensitive_ignores_patterns_inside_string_literals(self):
        self.assertEqual(c.sensitive([], '+_RE = re.compile(r"(\\beval\\(|shell=True)")\n'), [])
        self.assertEqual(c.sensitive([], "+msg = 'never call eval( here'\n"), [])
        self.assertEqual(c.sensitive([], "+x = eval(user_input)\n"), ["eval("])
        self.assertEqual(c.sensitive([], '+subprocess.run(cmd, shell=True)  # "quoted"\n'), ["shell=True"])
        self.assertTrue(c.sensitive(["plugin/lib/fmguard.py"]), "a guard is security code")
        self.assertEqual(c.sensitive([], '+x = f"{eval(user)}"\n'), ["eval("], "an f-string's fields are code")
        self.assertEqual(c.sensitive([], "+x = rf'{exec(user)}'\n"), ["exec("])
        self.assertEqual(c.sensitive([], "+x = fr'{exec(user)}'\n"), ["exec("])

    def test_a_regex_literal_exec_is_matching_not_running_code(self):
        # T-0118: `/^✔ (T-\d+) done/.exec(line)` asked for an adversary lens on a mod's toast parsing
        self.assertEqual(c.sensitive([], "+const id = /^✔ (T-\\d+) done/.exec(l)?.[1]\n"), [])
        self.assertEqual(c.sensitive([], "+const m = /a\\/b/gi.exec(s)\n"), [])
        self.assertEqual(c.sensitive([], "+child_process.exec(cmd)\n"), ["exec("], "a shell exec is still code")
        self.assertEqual(c.sensitive([], "+exec(source)\n"), ["exec("])

    def brief(self, tid):
        p = c.find_project(self.repo)
        return next(b for b in c.load_briefs(p) if b.id == tid)

    def test_section_short_name_fills_the_template_heading(self):
        self.fm("init")
        self.fm("task", "new", "Big", "--type", "FEATURE", "--tier", "M")
        self.fm("task", "set", "T-0001", "--section", "Approach", "--text", "a vs b: a")
        b = self.brief("T-0001")
        self.assertEqual(b.section("Approach (options → choice → why)").strip(), "a vs b: a")
        self.assertEqual([h for h, _ in b.sections].count("Approach"), 0, "no second, bare Approach section")

    def test_rewriting_criteria_keeps_ticks_of_unchanged_ones(self):
        self.fm("init")
        self.fm("task", "new", "Fix", "--type", "FIX", "--tier", "S", "--ac", "works :: true", "--step", "do")
        self.fm("task", "evidence", "T-0001", "--ac", "1", "--run", "true")
        self.fm("task", "ac", "T-0001", "check", "1", check=False)
        self.assertTrue(self.brief("T-0001").acceptance()[0].checked)
        old = self.brief("T-0001").section("Acceptance criteria").strip()
        self.fm("task", "set", "T-0001", "--section", "Acceptance criteria", "--text",
                old.replace("[x]", "[ ]") + "\n- [ ] also fast")
        acs = self.brief("T-0001").acceptance()
        self.assertEqual([a.checked for a in acs], [True, False])

    def test_timeouts_say_so(self):
        code, out = c.run_command(self.repo, "sleep 5", timeout=1)
        self.assertEqual(code, 124)
        self.assertTrue(c.run_result(code, out).startswith("✗ exit 124 (timed out after 1s)"))
        self.assertIn("timed out after", self.fm("quiet", "--timeout", "1", "--", "sleep 5", check=False).stdout)

    def test_task_new_plans_an_m_brief_in_one_call(self):
        self.fm("init")
        self.fm("task", "new", "Mid", "--type", "FEATURE", "--tier", "M", "--interpretation", "Reading 1: what it means. Reading 2: something else.",
                "--approach", "1) a; fails if: x 2) use what exists: b; fails if: y 3) do less: c; fails if: z Choice 1 (80%): a", "--ac", "works :: pytest -q", "--step", "build", "--focus")
        b = self.brief("T-0001")
        self.assertEqual(b.status, "active")
        self.assertEqual(b.section("Interpretation").strip(), "Reading 1: what it means. Reading 2: something else.")
        self.assertEqual(b.section("Approach (options → choice → why)").strip(), "1) a; fails if: x 2) use what exists: b; fails if: y 3) do less: c; fails if: z Choice 1 (80%): a")

    def test_finish_closes_an_m_task_in_one_call(self):
        # T-0097: close-out cost ~18 fm calls a task across 287 real tasks; one call does it now
        self.fm("init")
        self.fm("task", "new", "Mid", "--type", "FEATURE", "--tier", "M", "--interpretation", "Reading 1: a. Reading 2: b.", "--approach", "1) a; fails if: x 2) use what exists: b; fails if: y 3) do less: c; fails if: z Choice 1 (80%): a",
                "--ac", "works :: python3 -c 'print(1)'", "--ac", "fast :: python3 -c 'print(2)'", "--step", "build", "--focus")
        self.fm("task", "step", "T-0001", "done", "1", "--evidence", "python3 -c 'print(0)'", "ok")
        p = self.fm("task", "finish", "T-0001", "--audit", "fm-reviewer pass (research T-0001-review)",
                    "--lens", "intent: matches the request", "--lens", "edge: empty input handled",
                    "--docs", "README.md", "--lesson", "one call closes it", check=False)
        self.assertEqual(p.returncode, 0, p.stderr)
        b = self.brief("T-0001")
        self.assertEqual(b.status, "done")
        self.assertTrue(all(a.checked for a in b.acceptance()))
        self.assertEqual(b.section("Docs impact").strip(), "README.md")

    def test_finish_closes_a_step_that_already_has_evidence_and_names_what_needs_run(self):
        # T-0142: 'give --run: a step or criterion has no verify command of its own', after the step's evidence was in
        self.fm("init")
        self.fm("task", "new", "Mid", "--type", "FEATURE", "--tier", "M", "--interpretation", "Reading 1: a. Reading 2: b.", "--approach", "1) a; fails if: x 2) use what exists: b; fails if: y 3) do less: c; fails if: z Choice 1 (80%): a",
                "--ac", "works :: python3 -c 'print(1)'", "--step", "build", "--step", "live check", "--focus")
        self.fm("task", "step", "T-0001", "done", "1", "--evidence", "python3 -c 'print(0)'", "ok")
        p = self.fm("task", "finish", "T-0001", "--audit", "self", "--lens", "intent: ok", "--lens", "edge: ok",
                    "--docs", "none", "--lesson", "l", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("step 2 (live check)", p.stderr)  # says which one
        self.fm("task", "evidence", "T-0001", "--step", "2", "seen in the harness", "rows drawn")
        p = self.fm("task", "finish", "T-0001", "--audit", "self", "--lens", "intent: ok", "--lens", "edge: ok",
                    "--docs", "none", "--lesson", "l", check=False)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.brief("T-0001").status, "done")

    def test_finish_commit_takes_only_the_tasks_files_and_only_after_a_close(self):
        # T-0129: `fm task finish … | tail -1 && git commit` committed after a refused close, twice in one session
        import os
        import subprocess
        self.fm("init")
        with open(os.path.join(self.repo, "wip.txt"), "w") as f:
            f.write("work from before the task\n")
        self.fm("task", "new", "Small", "--type", "FEATURE", "--tier", "S", "--ac", "works :: python3 -c 'print(1)'",
                "--step", "build", "--focus")
        with open(os.path.join(self.repo, "feature.py"), "w") as f:
            f.write("x = 1\n")
        log = lambda: subprocess.run(["git", "-C", self.repo, "log", "--format=%s"], capture_output=True, text=True).stdout
        bad = self.fm("task", "finish", "T-0001", "--run", "python3 -c 'import sys; sys.exit(1)'", "--audit", "self",
                      "--commit", "Add the feature", check=False)
        self.assertNotEqual(bad.returncode, 0)
        self.assertNotIn("Add the feature", log())
        ok = self.fm("task", "finish", "T-0001", "--run", "python3 -c 'print(0)'", "--audit", "self checklist",
                     "--lens", "adversary: no input reaches a shell", "--commit", "Add the feature\n\nwhy", check=False)
        self.assertEqual(ok.returncode, 0, ok.stderr)  # T-0129: extra lenses don't drop a small task's self audit
        self.assertIn("Add the feature", log())
        names = subprocess.run(["git", "-C", self.repo, "show", "--name-only", "--format="], capture_output=True,
                               text=True).stdout.split()
        self.assertEqual(names, ["feature.py"], "work from before the task stays out of its commit")

    def test_finish_commit_counts_what_the_commit_holds(self):
        # T-0380: "committed f2c88b6 (70 path(s))" for a commit that changed one file
        import os
        import subprocess
        git = lambda *a: subprocess.run(["git", "-C", self.repo, *a], capture_output=True, text=True).stdout
        self.fm("init")
        with open(os.path.join(self.repo, "notes.txt"), "w") as f:
            f.write("kept\n")
        git("add", "notes.txt")
        git("commit", "-qm", "notes")
        self.fm("task", "new", "Tiny", "--type", "FEATURE", "--tier", "S", "--ac", "works :: python3 -c 'print(1)'",
                "--step", "build", "--focus")
        with open(os.path.join(self.repo, "notes.txt"), "w") as f:
            f.write("changed\n")
        self.fm("task", "log", "T-0001", "touch")
        with open(os.path.join(self.repo, "notes.txt"), "w") as f:
            f.write("kept\n")  # touched, then back as it was
        with open(os.path.join(self.repo, "feature.py"), "w") as f:
            f.write("x = 1\n")
        out = self.fm("task", "finish", "T-0001", "--run", "python3 -c 'print(0)'", "--audit", "self checklist",
                      "--commit", "Add it").stdout
        self.assertEqual(git("show", "--name-only", "--format=").split(), ["feature.py"])
        self.assertIn("(1 file)", out)

    def test_finish_commit_names_edits_in_another_repo(self):
        # T-0360: T-0359's only edit was in another checkout, and the commit said just "nothing to commit"
        import os
        import subprocess
        from sampo import core as c
        self.fm("init")
        other = os.path.join(self.tmp, "desktop")
        subprocess.run(["git", "init", "-q", other], check=True)
        self.fm("task", "new", "Small", "--type", "FIX", "--tier", "S", "--ac", "works :: python3 -c 'print(1)'",
                "--step", "build", "--focus")
        conf = os.path.join(other, "conf.json")
        with open(conf, "w") as f:
            f.write("{}\n")
        c.log_event(c.find_project(self.repo), "touched", task="T-0001", data={"file": conf, "tool": "Edit"})
        self.fm("task", "set", "T-0001", "--section", "Regression test", "--text", "none: test")
        ok = self.fm("task", "finish", "T-0001", "--run", "python3 -c 'print(0)'", "--audit", "self",
                     "--commit", "Fix it", check=False)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertIn(os.path.realpath(other), ok.stdout + ok.stderr)  # named, so it gets committed there
        self.assertIn("not committed", ok.stdout + ok.stderr)

    def test_edits_gone_or_committed_in_another_repo_are_not_named(self):
        # T-4039: a recovered lane's close named 6 'not committed' files in the main checkout: they were edits in a
        # builder worktree removed since (its path under the checkout), and the checkout was clean
        import os
        import subprocess
        from sampo import core as c
        self.fm("init")
        other = os.path.join(self.tmp, "desktop")
        git = lambda *a: subprocess.run(["git", "-C", other, *a], check=True, capture_output=True)  # noqa: E731
        os.makedirs(other)
        git("init", "-q")
        with open(os.path.join(other, "conf.json"), "w") as f:
            f.write("{}\n")
        git("add", "-A")
        git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "base")
        self.fm("task", "new", "Small", "--type", "FIX", "--tier", "S", "--ac", "works :: python3 -c 'print(1)'",
                "--step", "build", "--focus")
        p = c.find_project(self.repo)
        for f in (os.path.join(other, "conf.json"), os.path.join(other, "wt", "agent-1", "gone.py")):
            c.log_event(p, "touched", task="T-0001", data={"file": f, "tool": "Edit"})
        self.fm("task", "set", "T-0001", "--section", "Regression test", "--text", "none: test")
        ok = self.fm("task", "finish", "T-0001", "--run", "python3 -c 'print(0)'", "--audit", "self",
                     "--commit", "Fix it", check=False)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertNotIn("another repo", ok.stdout + ok.stderr)

    def test_a_red_step_closes_when_the_same_command_passed_later(self):
        # T-0165: step 1 holds the intended red run, step 2 the same command passing; T-0164 needed a third run
        import os
        self.fm("init")
        self.fm("task", "new", "Small", "--type", "FEATURE", "--tier", "S", "--ac", "works :: true", "--step", "red",
                "--step", "green", "--focus")
        check = "test -f done.txt"
        self.fm("task", "evidence", "T-0001", "--step", "1", "--run", check, check=False)
        open(os.path.join(self.repo, "done.txt"), "w").close()
        self.fm("task", "evidence", "T-0001", "--step", "2", "--run", check)
        p = self.fm("task", "finish", "T-0001", "--audit", "self check", check=False)
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_a_later_failure_of_the_same_command_still_refuses(self):
        import os
        self.fm("init")
        self.fm("task", "new", "Small", "--type", "FEATURE", "--tier", "S", "--ac", "works :: true", "--step", "a",
                "--step", "b", "--focus")
        check = "test -f done.txt"
        open(os.path.join(self.repo, "done.txt"), "w").close()
        self.fm("task", "evidence", "T-0001", "--step", "2", "--run", check)
        os.remove(os.path.join(self.repo, "done.txt"))
        self.fm("task", "evidence", "T-0001", "--step", "1", "--run", check, check=False)  # fails after the pass
        p = self.fm("task", "finish", "T-0001", "--audit", "self check", check=False)
        self.assertNotEqual(p.returncode, 0)

    def test_finish_on_an_m_task_refuses_an_unknown_lens(self):
        self.fm("init")
        self.fm("task", "new", "Mid", "--type", "FEATURE", "--tier", "M", "--interpretation", "Reading 1: a. Reading 2: b.", "--approach", "1) a; fails if: x 2) use what exists: b; fails if: y 3) do less: c; fails if: z Choice 1 (80%): a",
                "--ac", "works :: python3 -c 'print(1)'", "--step", "build", "--focus")
        p = self.fm("task", "finish", "T-0001", "--run", "python3 -c 'print(0)'", "--audit", "review",
                    "--lens", "vibes: fine", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("vibes", p.stderr)
