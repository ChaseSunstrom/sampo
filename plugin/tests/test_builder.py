"""Builder lanes (T-0234): an S/M task worked by a sampo:fm-builder subagent in its own git worktree (Agent isolation
"worktree"); the main thread reviews, merges, re-verifies and closes. At most `fm lane max` builders at once (2 by default)."""
import json
import os
import re
import subprocess

from helpers import PLUGIN, ForemanTestCase, read_text


def git(cwd, *args):
    return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, check=True).stdout.strip()


class _Tasks(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")

    def task(self, title, tier="S"):
        extra = ["--interpretation", "Reading 1: a. Reading 2: b.", "--approach", "1) a; fails if: x 2) use what exists: b; fails if: y 3) do less: c; fails if: z Choice 1 (80%): a"] if tier != "S" else []
        return json.loads(self.fm("task", "new", title, "--type", "FEATURE", "--tier", tier, "--ac",
                                  f"{title} works :: python3 -c 'print(1)'", "--step", "build it", *extra,
                                  "--json").stdout)["id"]


class Agent(ForemanTestCase):
    def test_the_builder_agent_has_a_worktree_only_contract(self):
        text = read_text(os.path.join(PLUGIN, "agents", "fm-builder.md"))
        head = text.split("---")[1]
        tools = re.search(r"(?m)^tools:\s*(.+)$", head).group(1)
        for tool in ("Edit", "Write", "Bash", "Read"):
            self.assertIn(tool, tools)
        self.assertNotIn("Agent", tools)  # it never launches agents of its own
        for rule in ("sampo focus", "worktree", "never push", "never merge", "sampo task evidence", "git commit"):
            self.assertIn(rule, text.lower() if rule.islower() else text)
        flat = " ".join(text.split())
        self.assertIn("Create `.fm-scratch/` files", flat)  # T-2294: the Write tool, not a heredoc
        self.assertIn("heredocs and `cat >` are refused there", flat)


class Brief(_Tasks):
    def test_a_self_contained_brief_and_the_agent_call(self):
        tid = self.task("Parse dates")
        res = json.loads(self.fm("lane", "brief", tid, "--json").stdout)
        brief = read_text(res["path"])
        for part in ("Parse dates works", "python3 -c 'print(1)'", "build it", f"sampo focus {tid}", "never push"):
            self.assertIn(part.lower(), brief.lower())
        self.assertIn('isolation: "worktree"', res["agent"])
        self.assertIn("sampo:fm-builder", res["agent"])
        self.assertIn("builder", self.fm("task", "show", tid).stdout)

    def test_the_first_lane_is_never_starved_by_moderate_load(self):
        # T-3243: Sampo's own sweeps and fills held load at 1.7-3.7 per CPU for three hours and no builder ran
        first = self.task("First lane")
        r = self.fm("lane", "brief", first, check=False, env={"FOREMAN_HOST": "load=60,cpus=16,mem_mb=8192"})
        self.assertNotEqual(r.returncode, 0, "past the hard bar (3.75 per CPU) not even a first lane starts")
        busy = {"FOREMAN_HOST": "load=28,cpus=16,mem_mb=8192"}  # 1.75 per CPU: past the soft bar, under the hard one
        self.assertEqual(self.fm("lane", "brief", first, check=False, env=busy).returncode, 0, "the first lane starts")
        second = self.task("Second lane")
        r = self.fm("lane", "brief", second, check=False, env=busy)
        self.assertNotEqual(r.returncode, 0, "a builder is out: the soft bar holds the next")
        self.assertIn("the host is loaded", r.stderr)

    def test_an_under_planned_task_gets_no_brief(self):
        # T-0823: a builder launched for a task fm focus refuses (no Interpretation) could only stop and report
        tid = json.loads(self.fm("task", "new", "Half planned", "--type", "FIX", "--tier", "M", "--ac",
                                 "ok :: python3 -c 'print(1)'", "--step", "s", "--json").stdout)["id"]
        r = self.fm("lane", "brief", tid, check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Interpretation", r.stderr)
        self.assertNotIn("builder", self.fm("task", "show", tid).stdout.split("---")[1], "no slot taken")

    def test_the_brief_names_its_base_and_the_isolation_rule(self):
        # T-0379 (JARVIS 2026-10-09): Claude Code makes the worktree from the default branch, so a builder can start on
        # stale code; and its isolation refuses make/gradle/"too complex" commands, which builders retried again and again
        tid = self.task("Based")
        brief = read_text(json.loads(self.fm("lane", "brief", tid, "--json").stdout)["path"])
        head = git(self.repo, "rev-parse", "HEAD").strip()
        self.assertIn(head, brief)
        self.assertIn("merge --ff-only", brief)
        # T-3142: main moved between the brief and the launch: a worktree ahead of BASE (BASE its ancestor) builds
        self.assertIn("is-ancestor <BASE> HEAD", read_text(os.path.join(PLUGIN, "agents", "fm-builder.md")))
        self.assertIn("isolated in the worktree", brief)
        self.assertIn("main thread", brief)
        # T-1196: builders hit the isolation refusal 31 times by cd-ing into the main checkout: name it, and pwd
        self.assertIn(f"main checkout ({self.repo})", " ".join(brief.split()))
        self.assertIn("`pwd`", brief)

    def test_the_brief_says_edit_with_the_edit_tool(self):
        # T-0399 (JARVIS 2026-10-09): builders edited files with `python3 - <<'E' … s.replace(…)` and `cat > f <<EOF`;
        # Claude Code's isolation refused each as "too complex", five in ten minutes
        tid = self.task("Edits")
        brief = read_text(json.loads(self.fm("lane", "brief", tid, "--json").stdout)["path"])
        self.assertIn("Edit and Write tools", brief)
        self.assertIn("too complex", brief)

    def test_brief_leaves_the_changelog_to_the_main_thread(self):
        # T-0428: with merge=union a lane's "Unreleased" line merged under a section released after the lane started;
        # the builder reports the line and the main thread adds it at finish
        tid = self.task("Changelog")
        brief = read_text(json.loads(self.fm("lane", "brief", tid, "--json").stdout)["path"])
        self.assertIn("Don't edit CHANGELOG.md", brief)
        self.assertIn("changelog line", brief)
        self.assertIn("final report", brief)

    def test_builder_model_follows_the_tier(self):
        # T-0373: "not everything needs to be opus if opus orchestrates"; T-1796: M builders too (Opus M lanes were
        # 22% of a week's usage); the main thread still reviews, merges and re-verifies
        small, mid = self.task("Small one"), self.task("Mid one", tier="M")
        for tid in (small, mid):
            self.assertIn('model: "sonnet"', json.loads(self.fm("lane", "brief", tid, "--json").stdout)["agent"])

    def test_l_tasks_held_tasks_and_a_third_builder_are_refused(self):
        big = self.task("Rewrite the engine", tier="L")
        self.assertNotEqual(self.fm("lane", "brief", big, check=False).returncode, 0)
        busy = self.task("Busy")
        self.fm("focus", busy)
        self.assertNotEqual(self.fm("lane", "brief", busy, check=False).returncode, 0)  # the main thread has it
        one, two, three = self.task("One"), self.task("Two"), self.task("Three")
        self.fm("lane", "brief", one)
        self.fm("lane", "brief", two)
        p = self.fm("lane", "brief", three, check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("the cap is 2", p.stderr + p.stdout)  # T-1089: fm lane max sets it; 2 by default
        self.fm("task", "drop", one, "not needed")  # a closed one frees its slot
        self.fm("lane", "brief", three)


class Flow(_Tasks):
    def test_focus_in_a_worktree_binds_and_main_takes_it_back_after_the_merge(self):
        tid = self.task("Add greet")
        self.fm("lane", "brief", tid)
        wt = os.path.join(self.tmp, "wt")
        git(self.repo, "worktree", "add", "-q", "-b", "builder/greet", wt)
        self.fm("focus", tid, cwd=wt)  # the builder's first command, in its own worktree
        self.assertEqual(self.meta(tid).get("lane"), os.path.realpath(wt))
        self.assertNotEqual(self.fm("focus", tid, check=False).returncode, 0)  # main sees it held by the worktree
        with open(os.path.join(wt, "greet.py"), "w") as f:
            f.write("def greet():\n    return 'hi'\n")
        git(wt, "add", "greet.py")
        git(wt, "commit", "-qm", f"greet ({tid})")
        git(self.repo, "merge", "-q", "--no-ff", "-m", "merge builder/greet", "builder/greet")
        self.fm("lane", "rm", tid)  # takes it back: the worktree goes, and its branch, now merged
        self.assertFalse(os.path.exists(wt))
        self.assertNotIn("builder/greet", git(self.repo, "branch", "--list", "builder/greet"))
        self.fm("focus", tid)  # back in the main checkout to re-verify and close
        self.assertIsNone(self.meta(tid).get("lane"))
        self.assertTrue(os.path.exists(os.path.join(self.repo, "greet.py")))

    def test_audit_prep_reviews_the_builders_branch_not_main(self):
        # T-0718: a lane task's review brief diffed the main checkout (other tasks' commits), not the builder's work
        tid = self.task("Add greet")
        self.fm("lane", "brief", tid)
        wt = os.path.join(self.tmp, "wt")
        git(self.repo, "worktree", "add", "-q", "-b", "builder/greet", wt)
        self.fm("focus", tid, cwd=wt)
        with open(os.path.join(wt, "greet.py"), "w") as f:
            f.write("def greet():\n    return 'hi'\n")
        git(wt, "add", "greet.py")
        git(wt, "commit", "-qm", f"greet ({tid})")
        with open(os.path.join(self.repo, "other.py"), "w") as f:  # main moves on meanwhile
            f.write("x = 1\n")
        git(self.repo, "add", "other.py")
        git(self.repo, "commit", "-qm", "other work")
        out = self.fm("audit", "prep", tid).stdout
        diff = read_text(re.search(r"Diff: (\S+)", out).group(1))
        self.assertIn("greet.py", diff)
        self.assertNotIn("other.py", diff)

    def meta(self, tid):
        from sampo import core as c
        return c.find_brief(c.find_project(self.repo), tid).meta


class Contract(_Tasks):
    """T-0234 review: the contract is enforced, not only written."""

    def lane(self, title):
        tid = self.task(title)
        self.fm("lane", "brief", tid)
        wt = os.path.join(self.tmp, "wt-" + tid)
        git(self.repo, "worktree", "add", "-q", "-b", f"builder/{tid}", wt)
        self.fm("focus", tid, cwd=wt)
        return tid, wt

    def test_a_builder_cant_close_its_task_or_hand_itself_back(self):
        tid, wt = self.lane("Close it")
        for args in (["task", "finish", tid, "--audit", "self: ok"], ["task", "done", tid],
                     ["task", "audit", tid, "intent", "x", "ok"], ["task", "drop", tid, "meh"], ["lane", "rm", tid]):
            p = self.fm(*args, cwd=wt, check=False)
            self.assertNotEqual(p.returncode, 0, args)

    def test_a_builder_writes_only_in_its_worktree(self):
        tid, wt = self.lane("Stay in")

        def verdict(tool, inp):
            out = self.hook("PreToolUse", {"tool_name": tool, "tool_input": inp, "cwd": wt}).stdout
            return "confine" if "blocked confine" in out else "ok"
        main_file = os.path.join(self.repo, "x.py")
        self.assertEqual(verdict("Write", {"file_path": main_file, "content": "x"}), "confine")
        self.assertEqual(verdict("Write", {"file_path": os.path.join(wt, "x.py"), "content": "x"}), "ok")
        self.assertEqual(verdict("Bash", {"command": f"echo x > {main_file}"}), "confine")
        said = self.hook("PreToolUse", {"tool_name": "Write", "tool_input": {"file_path": main_file, "content": "x"},
                                        "cwd": wt}).stdout
        self.assertIn("outside this builder's worktree", said)  # says why, and that no grant exists
        self.assertEqual(verdict("Bash", {"command": "echo x > /tmp/scratch.txt 2>/dev/null"}), "ok")
        main = self.hook("PreToolUse", {"tool_name": "Write", "tool_input": {"file_path": main_file, "content": "x"}})
        self.assertNotIn("blocked", main.stdout)  # the main thread isn't confined

    def test_lane_merge_and_cache_only_removal(self):
        # T-0377: on Foreman's own repo the guard refuses `git merge` (a tree write over core), so a reviewed branch
        # lands through fm, each file it changes judged as its task's write; and python caches don't hold a lane
        with open(os.path.join(self.repo, ".git", "info", "exclude"), "a") as f:
            f.write("__pycache__/\n")
        tid, wt = self.lane("Merge me")
        with open(os.path.join(wt, "new.py"), "w") as f:
            f.write("x = 1\n")
        git(wt, "add", "new.py")
        git(wt, "commit", "-qm", "builder work")
        os.makedirs(os.path.join(wt, "__pycache__"))
        open(os.path.join(wt, "__pycache__", "new.cpython-314.pyc"), "w").close()
        self.fm("lane", "merge", tid)
        self.assertTrue(os.path.exists(os.path.join(self.repo, "new.py")))
        self.assertIn(f"Merge {tid}", git(self.repo, "log", "-1", "--format=%s"))
        self.fm("lane", "rm", tid)  # only caches were left: they go with the folder
        self.assertFalse(os.path.isdir(wt))
        sneaky, wt2 = self.lane("Sneaky")
        os.makedirs(os.path.join(wt2, "config"))
        with open(os.path.join(wt2, "config", "secrets.yaml"), "w") as f:
            f.write("k: v\n")
        git(wt2, "add", "config")
        git(wt2, "commit", "-qm", "writes a secret")
        p = self.fm("lane", "merge", sneaky, check=False)
        self.assertNotEqual(p.returncode, 0, "fm is no way around the guard")
        self.assertIn("credentials", p.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "config", "secrets.yaml")))

    def test_lane_merge_names_every_function_the_branch_changed(self):
        # T-2312: a stray edit in a second function stands out in the merge result
        body = "".join(f"def {n}():\n    return 1\n\n\n" for n in ("f", "g", "h"))
        with open(os.path.join(self.repo, "m.py"), "w") as fh:
            fh.write(body)
        git(self.repo, "add", "m.py")
        git(self.repo, "commit", "-qm", "m")
        tid, wt = self.lane("Two funcs")
        with open(os.path.join(wt, "m.py"), "w") as fh:
            fh.write(body.replace("return 1", "return 2", 2))  # f and g, not h
        git(wt, "commit", "-qam", "edit f and g")
        out = self.fm("lane", "merge", tid, "--json").stdout
        self.assertEqual(json.loads(out)["functions"], {"m.py": ["f", "g"]})

    def test_lane_merge_output_holds_no_secret_from_a_hunk_header(self):  # T-2312 review
        key = "sk-live-9f8e7d6c5b4a39281706"
        with open(os.path.join(self.repo, "k.py"), "w") as fh:
            fh.write(f'API_KEY = "{key}"\nA = 1\nB = 2\nC = 3\nD = 4\n')
        git(self.repo, "add", "k.py")
        git(self.repo, "commit", "-qm", "k")
        tid, wt = self.lane("Below a key")
        with open(os.path.join(wt, "k.py"), "w") as fh:
            fh.write(f'API_KEY = "{key}"\nA = 1\nB = 2\nC = 3\nD = 5\n')
        git(wt, "commit", "-qam", "edit")
        self.assertNotIn(key, self.fm("lane", "merge", tid, "--json").stdout)

    def test_lane_merge_checks_both_sides_of_a_rename(self):
        # T-0382 (security review): git diff --name-only shows a rename by its new name only, so a lane that moves a
        # guarded file away (deleting it from main) must be judged by its old path too
        os.makedirs(os.path.join(self.repo, "config"))
        with open(os.path.join(self.repo, "config", "secrets.yaml"), "w") as f:
            f.write("k: v\n")
        git(self.repo, "add", "config")
        git(self.repo, "commit", "-qm", "a secret")
        tid, wt = self.lane("Mover")
        git(wt, "mv", "config/secrets.yaml", "notes.txt")
        git(wt, "commit", "-qm", "moves the secret")
        p = self.fm("lane", "merge", tid, check=False)
        self.assertNotEqual(p.returncode, 0, "a rename is a delete of the old path")
        self.assertIn("credentials", p.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.repo, "config", "secrets.yaml")))

    def test_lane_merge_over_unrelated_uncommitted_work(self):
        # T-0403 (JARVIS): the main thread was mid-T-0278 with uncommitted edits, so `fm lane merge T-0280` refused and
        # the lane couldn't land; git merges safely over edits the branch doesn't touch, with nothing staged
        with open(os.path.join(self.repo, "mine.txt"), "w") as f:
            f.write("committed\n")
        git(self.repo, "add", "mine.txt")
        git(self.repo, "commit", "-qm", "mine")
        tid, wt = self.lane("Lands beside my work")
        with open(os.path.join(wt, "theirs.py"), "w") as f:
            f.write("x = 1\n")
        git(wt, "add", "theirs.py")
        git(wt, "commit", "-qm", "builder work")
        with open(os.path.join(self.repo, "mine.txt"), "w") as f:
            f.write("in progress\n")  # the main thread's own uncommitted work
        self.fm("lane", "merge", tid)
        self.assertTrue(os.path.exists(os.path.join(self.repo, "theirs.py")))
        with open(os.path.join(self.repo, "mine.txt")) as f:
            self.assertEqual(f.read(), "in progress\n", "uncommitted work survives the merge")
        self.fm("lane", "rm", tid)  # merged: its slot comes back (two builders at most)
        clash, wt2 = self.lane("Touches my file")
        with open(os.path.join(wt2, "mine.txt"), "w") as f:
            f.write("theirs\n")
        git(wt2, "commit", "-qam", "edits mine.txt")
        p = self.fm("lane", "merge", clash, check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("mine.txt", p.stderr)
        git(self.repo, "add", "mine.txt")  # staged: a merge commit would record it
        other, wt3 = self.lane("Unrelated again")
        with open(os.path.join(wt3, "third.py"), "w") as f:
            f.write("y = 2\n")
        git(wt3, "add", "third.py")
        git(wt3, "commit", "-qm", "third")
        p = self.fm("lane", "merge", other, check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("staged", p.stderr)

    def test_lane_rm_ignores_tool_caches(self):
        # T-0408 (JARVIS): before each fm lane rm the session deleted __pycache__ and .pytest_cache by hand (and hit the
        # guard doing it); a test run's caches are rebuilt on the next run, never anyone's work
        with open(os.path.join(self.repo, ".git", "info", "exclude"), "a") as f:
            f.write(".pytest_cache/\nnode_modules/\n.env\ntarget/\n")
        tid, wt = self.lane("Caches only")
        for d in (".pytest_cache/v/cache", "node_modules/x", "target/debug", "voltc/target/release"):  # T-0833: builds
            os.makedirs(os.path.join(wt, d))
            open(os.path.join(wt, d, "f"), "w").close()
        self.fm("lane", "rm", tid)
        self.assertFalse(os.path.isdir(wt))
        kept, wt2 = self.lane("Has a .env")
        with open(os.path.join(wt2, ".env"), "w") as f:
            f.write("K=v\n")
        p = self.fm("lane", "rm", kept, check=False)
        self.assertNotEqual(p.returncode, 0, "an ignored .env may be someone's config: never discarded")
        self.assertIn(".env", p.stderr)
        # T-0839 (round-2 review): a build dir is rebuilt, but a secret kept inside one isn't
        hid, wt3 = self.lane("Secret in a build dir")
        os.makedirs(os.path.join(wt3, "target", "debug"))
        with open(os.path.join(wt3, "target", "debug", "app.pem"), "w") as f:
            f.write("key\n")
        p = self.fm("lane", "rm", hid, check=False)
        self.assertNotEqual(p.returncode, 0, "a key inside target/ isn't build output")
        self.assertIn("app.pem", p.stderr)

    def test_rm_deletes_only_the_lanes_own_branch(self):
        tid, wt = self.lane("Branches")
        git(self.repo, "branch", "release")  # merged into main
        git(wt, "switch", "-q", "release")  # the lane wandered off its branch
        self.fm("lane", "rm", tid)
        self.assertIn("release", git(self.repo, "branch", "--list", "release"))  # never one it switched to
        self.assertEqual(git(self.repo, "branch", "--list", f"builder/{tid}"), "")  # its own, merged, goes
        self.assertNotIn('"builder"', self.fm("task", "show", tid, "--json").stdout.split('"meta"', 1)[1].split("}", 1)[0])

    def test_a_briefed_task_that_never_launched_frees_its_slot(self):
        one, two, three = self.task("One"), self.task("Two"), self.task("Three")
        self.fm("lane", "brief", one)
        self.fm("lane", "brief", two)
        self.assertNotEqual(self.fm("lane", "brief", three, check=False).returncode, 0)
        self.fm("lane", "rm", one)
        self.fm("lane", "brief", three)


class LaneMergeGate(_Tasks):
    """T-0948: fm lane merge re-runs the lane task's criteria in the worktree and refuses red."""

    def gated(self, title, ac):
        tid = json.loads(self.fm("task", "new", title, "--type", "FEATURE", "--tier", "S", "--ac", ac, "--step", "build",
                                 "--json").stdout)["id"]
        wt = os.path.join(self.tmp, "wt-" + tid)
        git(self.repo, "worktree", "add", "-q", "-b", f"foreman/{tid}", wt)
        self.fm("focus", tid, cwd=wt)
        with open(os.path.join(wt, tid + ".py"), "w") as f:
            f.write("x = 1\n")
        git(wt, "add", tid + ".py")
        git(wt, "commit", "-qm", "builder work")
        return tid, wt

    def test_merge_refuses_red(self):
        tid, wt = self.gated("Red", "it passes :: sh -c 'echo line1; echo broken; exit 3'")
        p = self.fm("lane", "merge", tid, check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("exit 3", p.stderr + p.stdout)
        self.assertIn("broken", p.stderr + p.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.repo, tid + ".py")), "red: nothing merged")
        green, wt2 = self.gated("Green", "it is in the worktree :: test -f T-0002.py")  # the verify runs in the lane
        self.fm("lane", "merge", green)
        self.assertTrue(os.path.exists(os.path.join(self.repo, green + ".py")))
        bare, _ = self.gated("Bare", "nothing to run")
        p = self.fm("lane", "merge", bare, check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("plan verify commands first", p.stderr + p.stdout)

    def test_unverified_guardrails(self):
        from sampo import core as c
        tid, wt = self.gated("Waived", "it passes :: false")
        for reason in ("", "   "):
            p = self.fm("lane", "merge", tid, "--unverified", reason, check=False)
            self.assertNotEqual(p.returncode, 0, repr(reason))
        p = self.fm("lane", "merge", tid, "--unverified", "builder waives", cwd=wt, check=False)
        self.assertNotEqual(p.returncode, 0, "a builder can't waive its own gate")
        self.assertFalse(os.path.exists(os.path.join(self.repo, tid + ".py")))
        self.fm("lane", "merge", tid, "--unverified", "gate is flaky here")
        self.assertTrue(os.path.exists(os.path.join(self.repo, tid + ".py")))
        ev = [e for e in c.ledger_tail(c.find_project(self.repo), 50) if e["event"] == "lane_unverified"]
        self.assertEqual(ev[-1]["data"]["reason"], "gate is flaky here")


class DroppedRedo(_Tasks):
    """T-1823: a builder's branch dropped with fm lane rm --why: the next builder brief starts from the reason, the kept
    branch and the check that was red."""
    gated = LaneMergeGate.gated

    def test_the_redo_brief_starts_from_the_drop(self):
        fresh = self.task("Untouched")
        self.assertNotIn("First attempt", read_text(json.loads(self.fm("lane", "brief", fresh, "--json").stdout)["path"]))
        tid, wt = self.gated("Redo", "it passes :: sh -c 'echo broken; exit 3'")
        self.assertNotEqual(self.fm("lane", "merge", tid, check=False).returncode, 0)
        git(self.repo, "worktree", "lock", "--reason", "claude agent agent-x (pid 1 start 1)", wt)  # T-0725: CC keeps it
        self.assertNotEqual(self.fm("lane", "rm", tid, check=False).returncode, 0, "unmerged and locked: plain rm refuses")
        r = self.fm("lane", "rm", tid, "--why", "builder drops itself", cwd=wt, check=False)
        self.assertNotEqual(r.returncode, 0, "a builder can't drop its own work")
        rm = json.loads(self.fm("lane", "rm", tid, "--why", "it patched the test, not the parser", "--json").stdout)
        self.assertEqual(rm["dropped"]["why"], "it patched the test, not the parser")
        brief = read_text(json.loads(self.fm("lane", "brief", tid, "--json").stdout)["path"])
        part = brief.split("## First attempt (dropped)", 1)[1].split("\n## ", 1)[0]
        for want in ("it patched the test, not the parser", f"foreman/{tid}", "exit 3"):
            self.assertIn(want, part)

    def test_rm_without_why_records_no_drop(self):
        tid, wt = self.gated("Plain", "it passes :: true")
        self.assertNotIn("dropped", json.loads(self.fm("lane", "rm", tid, "--json").stdout))
