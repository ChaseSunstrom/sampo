"""Builder lanes (T-0234): an S/M task worked by a sampo:fm-builder subagent in its own git worktree (Agent isolation
"worktree"); the main thread reviews, merges, re-verifies and closes. At most `fm lane max` builders at once (2 by default)."""
import json
import os
import re
import shlex
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
        self.assertIn("Append to a test file with the Edit tool", flat)  # T-3114
        self.assertIn("never read /tmp or the scratchpad from a lane", flat)


class Brief(_Tasks):
    def test_the_contract_tells_a_fix_builder_to_record_task_prove(self):  # T-4209
        """Lanes landed 'left for a hand close: no red→green proof': the contract names the verb, in the agent file and in
        the contract text contract() renders for a real id (the one a brief carries when its agent lacks the rules)."""
        flat = " ".join(read_text(os.path.join(PLUGIN, "agents", "fm-builder.md")).split())
        self.assertIn("For a FIX record `sampo task prove <ID>", flat)
        from sampo import lanes as slanes
        copy = " ".join(slanes.contract("T-9", "abc123", "/main").split())
        self.assertIn("For a FIX record `sampo task prove T-9 --run", copy)
        self.assertIn("lane accept", copy)  # why: the close refuses a FIX with no proof

    def test_the_contract_has_a_code_start_card(self):  # T-5682: the brief's map first, then a card, then a range
        """2026-11-22: 0 of 100 builders called sampo outline or the calling cards; they opened with grep, sed and whole-file
        Reads. The contract's card names the brief's map, the two cards and the range read, and what a whole-file Read costs."""
        from sampo import lanes as slanes
        text = read_text(os.path.join(PLUGIN, "agents", "fm-builder.md"))
        rules = [r for r in text.split("\n- ") if r.startswith("Code-start card")]
        self.assertEqual(len(rules), 1, "one rule, in the contract")
        flat = " ".join(rules[0].split())
        for want in ("Where things are", "Callers of the named functions", "sampo sym PATH:NAME", "sampo outline PATH",
                     "Read with offset and limit", "400 lines"):
            self.assertIn(want, flat)
        self.assertLess(len(rules[0].encode()), 450, "the rule stays under 450 bytes")
        copy = " ".join(slanes.contract("T-9", "abc123", "/main").split())
        self.assertIn("sampo sym PATH:NAME", copy, "the brief's copy carries the card")

    def test_a_recovered_builder_may_merge_the_branch_its_resume_section_names(self):  # T-5475
        """2026-11-21: recovered builders read 'git merge <old branch>' in the Resume section and 'never merge another
        branch' in the contract, obeyed the contract and re-authored their lanes from scratch."""
        import argparse
        import contextlib
        import io
        flat = " ".join(read_text(os.path.join(PLUGIN, "agents", "fm-builder.md")).split())
        self.assertIn("never merge another branch (the fast-forward above and the branch a Resume section names aside)",
                      flat)
        from sampo import core as c, lanes as slanes
        self.assertIn("the branch a Resume section names aside", " ".join(slanes.contract("T-9", "abc", "/m").split()))
        tid = self.task("Parse dates")
        p = c.find_project(self.repo)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            slanes.builder_brief(p, c.find_brief(p, tid), argparse.Namespace(json=True, model=None, why=None),
                                 resume_branch="worktree-old-lane")
        brief = " ".join(read_text(json.loads(buf.getvalue())["path"]).split())
        self.assertIn("run `git merge worktree-old-lane`", brief)
        self.assertIn("the one merge besides the fast-forward that the rules allow", brief)

    def test_the_brief_points_at_the_agent_contract(self):  # T-5595
        """The brief's contract section is the base line sampo lane start reads, the real ids and a legend. The rules
        live once, in the agent definition the builder loads as its system prompt: the 9.7 KB copy is not in the brief."""
        from sampo import lanes as slanes
        tid = self.task("Pointed")
        head = git(self.repo, "rev-parse", "HEAD").strip()
        brief = read_text(json.loads(self.fm("lane", "brief", tid, "--json").stdout)["path"])
        self.assertEqual(brief.count(slanes.CONTRACT_HEADING), 1)
        section = brief.split(slanes.CONTRACT_HEADING, 1)[1]
        flat = " ".join(section.split())
        self.assertIn(f"Start from the main checkout's commit {head} with `sampo lane start {tid}`", flat)
        for key, value in (("<ID>", tid), ("<BASE>", head), ("<MAIN>", self.repo)):
            self.assertIn(f"{key} = {value}", flat, "the legend gives the real values")
        self.assertNotIn("Never run the heavy repo-shape modules", flat)
        self.assertNotIn("Never push, never merge", flat)
        self.assertLess(len(section.encode()), 2500, "the short contract, not the whole rules copy")

    def test_the_brief_carries_the_full_contract_when_the_agent_lacks_it(self):  # T-5595
        """An agent definition with no rules marker: the brief carries the whole contract text, so a builder is never
        left without rules. contract() still renders the same text for the same agent file."""
        import argparse
        import contextlib
        import io
        from unittest import mock
        from sampo import core as c, lanes as slanes
        tid = self.task("Lacks")
        head = git(self.repo, "rev-parse", "HEAD").strip()
        real = slanes.agent_text()
        self.assertIn("break none of it:\n", real)
        bare = real.replace("break none of it:\n", "the rules follow:\n")
        p = c.find_project(self.repo)
        buf = io.StringIO()
        with mock.patch.object(slanes, "agent_text", return_value=bare), contextlib.redirect_stdout(buf):
            slanes.builder_brief(p, c.find_brief(p, tid), argparse.Namespace(json=True, model=None, why=None))
            full = " ".join(slanes.contract(tid, head, self.repo).split())
        brief = read_text(json.loads(buf.getvalue())["path"])
        flat = " ".join(brief.split())
        self.assertEqual(brief.count(slanes.CONTRACT_HEADING), 1)
        self.assertIn(full, flat, "the brief's contract is contract()'s text")
        self.assertIn("Never push, never merge another branch", flat)
        self.assertIn(f"Start from the main checkout's commit {head} with `sampo lane start {tid}`", flat)

    def test_a_brief_in_a_project_with_the_test_runner_carries_the_repo_rules(self):  # T-5595
        """The Sampo-repo-only rules (the repo-shape modules, run.py's --deadline) reach a builder whose project has
        plugin/tests/run.py; the same brief in a project without it does not carry them (the Brief test above)."""
        os.makedirs(os.path.join(self.repo, "plugin", "tests"), exist_ok=True)
        open(os.path.join(self.repo, "plugin", "tests", "run.py"), "w", encoding="utf-8").close()
        tid = self.task("Runner")
        brief = " ".join(read_text(json.loads(self.fm("lane", "brief", tid, "--json").stdout)["path"]).split())
        self.assertIn("Never run the heavy repo-shape modules", brief)
        self.assertIn("--deadline S", brief)
        self.assertIn("Start from the main checkout's commit", brief)

    def test_a_self_contained_brief_and_the_agent_call(self):
        tid = self.task("Parse dates")
        res = json.loads(self.fm("lane", "brief", tid, "--json").stdout)
        brief = read_text(res["path"])
        for part in ("Parse dates works", "python3 -c 'print(1)'", "build it", f"sampo lane start {tid}"):
            self.assertIn(part.lower(), brief.lower())
        self.assertIn("never push", read_text(os.path.join(PLUGIN, "agents", "fm-builder.md")).lower(), "the rules are the agent's")
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
        agent = read_text(os.path.join(PLUGIN, "agents", "fm-builder.md"))  # T-5595: the rules are the agent's
        self.assertIn(head, brief)  # the base line of the brief's short contract
        self.assertIn(f"<MAIN> = {self.repo}", brief, "the legend gives the main checkout the rules name")
        self.assertIn("merge --ff-only", agent)
        # T-3142: main moved between the brief and the launch: a worktree ahead of BASE (BASE its ancestor) builds
        self.assertIn("is-ancestor <BASE> HEAD", agent)
        self.assertIn("isolated in the worktree", agent)
        self.assertIn("main thread", agent)
        # T-1196: builders hit the isolation refusal 31 times by cd-ing into the main checkout: the rule names it, and pwd
        self.assertIn("main checkout (<MAIN>)", agent)
        self.assertIn("`pwd`", agent)

    def test_the_brief_says_edit_with_the_edit_tool(self):
        # T-0399 (JARVIS 2026-10-09): builders edited files with `python3 - <<'E' … s.replace(…)` and `cat > f <<EOF`;
        # Claude Code's isolation refused each as "too complex", five in ten minutes
        tid = self.task("Edits")
        agent = read_text(os.path.join(PLUGIN, "agents", "fm-builder.md"))  # T-5595: the rule is the agent's
        self.assertIn("Edit and Write tools", agent)
        self.assertIn("too complex", agent)

    def test_brief_leaves_the_changelog_to_the_main_thread(self):
        # T-0428: with merge=union a lane's "Unreleased" line merged under a section released after the lane started;
        # the builder reports the line and the main thread adds it at finish
        tid = self.task("Changelog")
        agent = read_text(os.path.join(PLUGIN, "agents", "fm-builder.md"))  # T-5595: the rule is the agent's
        self.assertIn("Don't edit CHANGELOG.md", agent)
        self.assertIn("changelog line", agent)
        self.assertIn("final report", agent)

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


    def test_the_launch_prompt_opens_with_read_and_lane_start(self):  # T-5594: no preamble calls before the work
        tid = self.task("Parse dates")
        res = json.loads(self.fm("lane", "brief", tid, "--json").stdout)
        self.assertIn(f'prompt: "Read {res["path"]} and run sampo lane start {tid} in your first message', res["agent"])
        self.assertIn("both calls in one turn, then work the task", res["agent"])

    def test_the_contract_leaves_leases_to_lane_start(self):  # T-5594: lane start takes the scope, SubagentStop drops it
        from sampo import lanes as slanes
        flat = " ".join(read_text(os.path.join(PLUGIN, "agents", "fm-builder.md")).split())
        self.assertNotIn("read it first", flat, "the brief is read in the same first message as lane start")
        self.assertNotIn("Before editing, claim what you plan to change", flat)
        self.assertIn("lane start took the leases on your scope files", flat)
        self.assertIn("sampo lease take FILE[::SYMBOL]", flat, "a file outside the scope is still taken by hand")
        copy = " ".join(slanes.contract("T-9", "abc123", "/main").split())
        self.assertNotIn("Before editing, claim what you plan to change", copy)
        self.assertIn("lane start took the leases on your scope files", copy)


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

    def test_the_main_thread_in_a_subagent_builder_s_worktree_is_not_that_builder(self):
        # T-3802: Claude Code moves the main session's cwd into a builder's .claude/worktrees/agent-<id> when it cd's
        # there (to run the lane's tests, to merge main in); its next write elsewhere was refused as the builder's
        # (2026-11-08, three times). Only the subagent's own calls carry an agent_id
        tid = self.task("Sub")
        self.fm("lane", "brief", tid)
        wt = os.path.join(self.repo, ".claude", "worktrees", "agent-a1b2c3d4")
        git(self.repo, "worktree", "add", "-q", "-b", "worktree-agent-a1b2c3d4", wt)
        self.fm("focus", tid, cwd=wt)
        main_file = os.path.join(self.repo, "x.py")

        def said(**extra):
            return self.hook("PreToolUse", {"tool_name": "Write", "cwd": wt, **extra,
                                            "tool_input": {"file_path": main_file, "content": "x"}}).stdout
        self.assertNotIn("blocked confine", said(), "the main thread, visiting")
        self.assertIn("blocked confine", said(agent_id="a1b2c3d4", agent_type="sampo:fm-builder"), "the builder")
        # T-5766 review: the lane pilot's `claude -p --agent sampo:fm-resolver` is a main session (no agent_id) in that
        # worktree: it is no visitor, it is the editing agent, and its writes (and Bash) are confined
        for agent in ("sampo:fm-resolver", "sampo:fm-builder", "sampo:fm-fixer"):
            self.assertIn("blocked confine", said(agent_type=agent), f"a headless {agent}")
        bash = self.hook("PreToolUse", {"tool_name": "Bash", "cwd": wt, "agent_type": "sampo:fm-resolver",
                                        "tool_input": {"command": f"echo x > {main_file}"}}).stdout
        self.assertIn("blocked confine", bash, "a headless resolver's Bash is confined")
        marked = self.hook("PreToolUse", {"tool_name": "Write", "cwd": wt, "tool_input": {"file_path": main_file, "content": "x"}},
                           env={"FOREMAN_PILOT_HEADLESS": "1"}).stdout
        self.assertIn("blocked confine", marked, "the pilot's environment marker confines a main session in the lane")

    def test_a_fixer_writes_only_in_its_worktree_with_no_builder_brief(self):
        # T-5681 review: the editing set confines a fixer too; a fixer gets no lane brief, so its task has no builder mark
        tid = self.task("Fix it")
        wt = os.path.join(self.repo, ".claude", "worktrees", "agent-f1x2e3d4")
        git(self.repo, "worktree", "add", "-q", "-b", "worktree-agent-f1x2e3d4", wt)
        self.fm("focus", tid, cwd=wt)
        main_file = os.path.join(self.repo, "x.py")

        def write(path):
            return self.hook("PreToolUse", {"tool_name": "Write", "cwd": wt, "agent_id": "f1x2e3d4",
                                            "agent_type": "sampo:fm-fixer",
                                            "tool_input": {"file_path": path, "content": "x"}}).stdout
        self.assertIn("blocked confine", write(main_file), "the fixer's write outside its worktree")
        self.assertNotIn("blocked", write(os.path.join(wt, "x.py")), "its own worktree")

    def test_a_fixer_or_resolver_launch_without_worktree_isolation_is_refused(self):
        # T-5681 review: an edit-capable agent launched in the main checkout would edit the user's own tree
        for agent in ("sampo:fm-fixer", "sampo:fm-resolver"):
            # T-5837: a launch that names its model gets no table rewrite, so an allowed one prints nothing
            for tool_input, refused in (({"subagent_type": agent, "prompt": "x", "model": "sonnet"}, True),
                                        ({"subagent_type": agent, "prompt": "x", "model": "sonnet",
                                          "isolation": "worktree"}, False)):
                r = self.hook("PreToolUse", {"tool_name": "Agent", "tool_input": tool_input, "cwd": self.repo}).stdout
                self.assertEqual("permissionDecision" in r, refused, (agent, tool_input, r))

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


class EvidenceForms(_Tasks):
    """T-4139: evidence without command text the harness refuses (`bash -c`, heredocs, git in --run): `--ac N` alone runs
    the criterion's recorded verify, and `--run-file PATH` runs a script inside the lane, never one outside it."""

    def test_evidence_form_ac_without_text_runs_the_recorded_verify(self):
        tid = self.task("Runs")  # its verify is python3 -c 'print(1)'
        res = json.loads(self.fm("task", "evidence", tid, "--ac", "1", "--json").stdout)
        self.assertEqual(res["exit"], 0)
        self.assertIn("python3 -c 'print(1)'", self.fm("task", "show", tid).stdout)

    def test_evidence_form_ac_without_text_reports_the_verify_exit_code(self):
        tid = json.loads(self.fm("task", "new", "Fails", "--type", "FEATURE", "--tier", "S", "--ac",
                                 "Fails :: python3 -c 'import sys; sys.exit(3)'", "--step", "build it",
                                 "--json").stdout)["id"]
        res = json.loads(self.fm("task", "evidence", tid, "--ac", "1", "--json", check=False).stdout)
        self.assertEqual(res["exit"], 3, "the verify's real exit code, not a typed pass")

    def test_evidence_form_ac_without_a_verify_says_so(self):
        tid = self.task("Plain")
        self.fm("task", "ac", tid, "add", "Looks right by eye")
        r = self.fm("task", "evidence", tid, "--ac", "2", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no verify", r.stderr + r.stdout)

    def test_evidence_form_run_file_runs_a_script_inside_the_lane(self):
        tid = self.task("Scripted")
        os.makedirs(os.path.join(self.repo, ".fm-scratch"), exist_ok=True)
        with open(os.path.join(self.repo, ".fm-scratch", "check.sh"), "w") as f:
            f.write("touch ran.marker\n")
        self.fm("task", "evidence", tid, "--step", "1", "--run-file", ".fm-scratch/check.sh")
        self.assertTrue(os.path.exists(os.path.join(self.repo, "ran.marker")), "the script ran in the lane root")
        self.assertIn("check.sh", self.fm("task", "show", tid).stdout)

    def test_evidence_form_run_file_is_judged_by_the_script_it_runs(self):
        # the guard reads the script's text as it reads a typed --run: a script holding a refused command is refused
        tid = self.task("Judged")
        os.makedirs(os.path.join(self.repo, ".fm-scratch"), exist_ok=True)
        with open(os.path.join(self.repo, ".fm-scratch", "bad.sh"), "w") as f:
            f.write("sampo task set T-1 --allow core\n")
        typed = json.loads(self.fm("guard", "try", f"sampo task evidence {tid} --step 1 --run 'sampo task set T-1 --allow core'",
                                   "--cwd", self.repo, "--json").stdout)
        held = json.loads(self.fm("guard", "try", f"sampo task evidence {tid} --step 1 --run-file .fm-scratch/bad.sh",
                                  "--cwd", self.repo, "--json").stdout)
        self.assertEqual(typed["decision"], "deny")
        self.assertEqual(held["decision"], "deny", "the script's own text is the run text")

    def test_evidence_form_run_file_refuses_a_path_outside_the_lane(self):
        tid = self.task("Escapes")
        outside = os.path.join(self.tmp, "outside.sh")
        with open(outside, "w") as f:
            f.write("touch escaped.marker\n")
        os.makedirs(os.path.join(self.repo, ".fm-scratch"), exist_ok=True)
        os.symlink(outside, os.path.join(self.repo, ".fm-scratch", "link.sh"))  # a link inside the lane to outside
        for path in (outside, "../outside.sh", ".fm-scratch/link.sh"):
            r = self.fm("task", "evidence", tid, "--step", "1", "--run-file", path, check=False)
            self.assertNotEqual(r.returncode, 0, path)
            self.assertIn("outside", r.stderr + r.stdout, path)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "escaped.marker")), "no refused script ran")

    def test_evidence_form_ac_runs_no_stored_verify_the_guard_refuses(self):
        # a stored verify is a command too: --ac N gets the guard's checks before it runs
        tid = json.loads(self.fm("task", "new", "Stored", "--type", "FEATURE", "--tier", "S", "--ac",
                                 "Stored :: touch stored.marker && sampo task set T-1 --allow core", "--step", "build it",
                                 "--json").stdout)["id"]
        r = self.fm("task", "evidence", tid, "--ac", "1", "--json", check=False)
        self.assertNotEqual(r.returncode, 0, "the guard refuses the stored verify")
        self.assertFalse(os.path.exists(os.path.join(self.repo, "stored.marker")), "a refused verify never ran")

    def test_evidence_form_run_file_refuses_a_script_over_the_run_cap(self):
        tid = self.task("Big")
        os.makedirs(os.path.join(self.repo, ".fm-scratch"), exist_ok=True)
        with open(os.path.join(self.repo, ".fm-scratch", "big.sh"), "w") as f:
            f.write("true\n" * 30000)  # 150 KB: over the 120 KB one bash -c takes as an argument
        r = self.fm("task", "evidence", tid, "--step", "1", "--run-file", ".fm-scratch/big.sh", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("120 KB", r.stderr + r.stdout)

    def test_evidence_form_run_file_refuses_at_the_cli_a_script_the_guard_refuses(self):
        # T-4139: the CLI judges the bytes it runs too (not only the hook): a refused script never runs
        tid = self.task("Refused")
        self.script(".fm-scratch/bad.sh", "touch bad.marker\nsampo task set T-1 --allow core\n")
        r = self.fm("task", "evidence", tid, "--step", "1", "--run-file", ".fm-scratch/bad.sh", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "bad.marker")), "a refused script never ran")

    def test_evidence_form_run_file_repeat_runs_the_judged_bytes_once_each(self):
        tid = self.task("Repeats")
        self.script(".fm-scratch/check.sh", "echo ok >> runs.log\n")
        self.fm("task", "evidence", tid, "--step", "1", "--run-file", ".fm-scratch/check.sh", "--repeat", "2")
        self.assertEqual(read_text(os.path.join(self.repo, "runs.log")).split(), ["ok", "ok"])

    def test_evidence_form_run_file_never_runs_text_a_run_rewrote(self):
        # the script rewrites its own file on the first run: the second run runs the bytes the guard judged, not that
        tid = self.task("Rewrites")
        self.script(".fm-scratch/check.sh", "echo ok >> runs.log\n"
                                            "printf 'echo SWAPPED >> runs.log\\n' > .fm-scratch/check.sh\n")
        self.fm("task", "evidence", tid, "--step", "1", "--run-file", ".fm-scratch/check.sh", "--repeat", "2")
        self.assertEqual(read_text(os.path.join(self.repo, "runs.log")).split(), ["ok", "ok"])

    def test_evidence_form_run_file_allows_a_var_target_the_hook_allows(self):
        # _check_script tolerates a $VAR target: the hook's verdict on the script and the CLI's must agree, and it runs
        tid = self.task("Var")
        self.script(".fm-scratch/var.sh", "OUT=var.marker\ntouch \"$OUT\"\n")
        typed = json.loads(self.fm("guard", "try", "bash .fm-scratch/var.sh", "--cwd", self.repo, "--json").stdout)
        held = json.loads(self.fm("guard", "try", f"sampo task evidence {tid} --step 1 --run-file .fm-scratch/var.sh",
                                  "--cwd", self.repo, "--json").stdout)
        self.assertEqual(held["decision"], typed["decision"])
        self.fm("task", "evidence", tid, "--step", "1", "--run-file", ".fm-scratch/var.sh")
        self.assertTrue(os.path.exists(os.path.join(self.repo, "var.marker")), "the script ran")

    def test_evidence_form_run_file_gives_the_script_its_own_path_as_dollar_zero(self):
        # bash -c would leave $0 as bash, so the script's cd "$(dirname "$0")/.." would land in the lane's parent:
        # $0 is the lane-relative path, as bash FILE sets it, and the script lands in the lane root
        tid = self.task("Zero")
        self.script(".fm-scratch/zero.sh", 'cd "$(dirname "$0")/.." && touch zero.marker\n')
        self.fm("task", "evidence", tid, "--step", "1", "--run-file", ".fm-scratch/zero.sh")
        self.assertTrue(os.path.exists(os.path.join(self.repo, "zero.marker")), "$0 names the script's own path")
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(self.repo), "zero.marker")), "not the lane's parent")

    def test_evidence_form_run_file_refuses_a_script_that_reads_bash_source(self):
        # bash -c leaves BASH_SOURCE empty, so such a script would see another file than bash FILE gives it: $0 is the way
        tid = self.task("Source")
        self.script(".fm-scratch/src.sh", 'echo "$BASH_SOURCE" > src.out\n')
        r = self.fm("task", "evidence", tid, "--step", "1", "--run-file", ".fm-scratch/src.sh", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("BASH_SOURCE", r.stderr + r.stdout)
        self.assertIn("$0", r.stderr + r.stdout, "the reason says to use $0")
        self.assertFalse(os.path.exists(os.path.join(self.repo, "src.out")), "a refused script never ran")

    def test_evidence_form_run_file_records_a_name_with_a_space_quoted(self):
        # the recorded command is what resume re-runs: its path must survive a shell split as one word
        tid = self.task("Spaced")
        self.script(".fm-scratch/my check.sh", "touch spaced.marker\n")
        self.fm("task", "evidence", tid, "--step", "1", "--run-file", ".fm-scratch/my check.sh")
        self.assertTrue(os.path.exists(os.path.join(self.repo, "spaced.marker")), "the script ran")
        recorded = re.search(r"`(bash [^`]*)`", self.fm("task", "show", tid).stdout).group(1)
        self.assertEqual(shlex.split(recorded), ["bash", ".fm-scratch/my check.sh"])

    def test_evidence_form_run_file_records_a_leading_dash_name_as_a_path(self):
        # a name that starts with - would read as an option to bash: it is recorded as ./NAME
        tid = self.task("Dashed")
        self.script("-dash.sh", "touch dashed.marker\n")
        self.fm("task", "evidence", tid, "--step", "1", "--run-file=-dash.sh")
        self.assertTrue(os.path.exists(os.path.join(self.repo, "dashed.marker")), "the script ran")
        self.assertIn("`bash ./-dash.sh`", self.fm("task", "show", tid).stdout)

    def test_evidence_form_run_file_runs_crlf_text_the_way_bash_file_runs_it(self):
        # CRLF is not refused: the run exits as bash FILE exits on the same bytes (the \r stays in each command)
        tid = self.task("Crlf")
        self.script_bytes(".fm-scratch/crlf.sh", b"true\r\n")
        expected = subprocess.run(["bash", ".fm-scratch/crlf.sh"], cwd=self.repo, capture_output=True).returncode
        res = json.loads(self.fm("task", "evidence", tid, "--step", "1", "--run-file", ".fm-scratch/crlf.sh", "--json",
                                 check=False).stdout)
        self.assertEqual(res["exit"], expected)

    def test_evidence_form_run_file_refuses_text_that_is_not_utf8(self):
        tid = self.task("Latin")
        self.script_bytes(".fm-scratch/latin.sh", b"touch latin.marker\n# caf\xe9\n")
        r = self.fm("task", "evidence", tid, "--step", "1", "--run-file", ".fm-scratch/latin.sh", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("UTF-8", r.stderr + r.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "latin.marker")), "a refused script never ran")

    def script(self, rel, text):
        os.makedirs(os.path.dirname(os.path.join(self.repo, rel)), exist_ok=True)
        with open(os.path.join(self.repo, rel), "w") as f:
            f.write(text)

    def script_bytes(self, rel, data):
        os.makedirs(os.path.dirname(os.path.join(self.repo, rel)), exist_ok=True)
        with open(os.path.join(self.repo, rel), "wb") as f:
            f.write(data)


class PassingShapes(_Tasks):
    """T-4139: every worktree agent is told, before its first command, the shapes the harness passes."""

    def block(self):
        text = read_text(os.path.join(PLUGIN, "agents", "fm-builder.md"))
        self.assertIn("**Passing shapes:**", text)
        block = text[text.index("**Passing shapes:**"):].split("\n", 1)[0]  # one bullet line
        self.assertLess(len(block.encode()), 600, "it is paid on every subagent start")
        return block

    def start_context(self, cwd):
        out = self.hook("SubagentStartContext", {"agent_type": "general-purpose", "agent_id": "a1", "cwd": cwd})
        return json.loads(out.stdout)["hookSpecificOutput"]["additionalContext"] if out.stdout.strip() else ""

    def test_the_builder_contract_carries_the_passing_shapes(self):
        text = read_text(os.path.join(PLUGIN, "agents", "fm-builder.md"))
        contract = text.split("break none of it:\n", 1)[1].split("\n\nReport in", 1)[0]
        self.assertIn(self.block(), contract)
        for shape in ("--ac", "--run-file", "git -C", ".venv/bin/python", "env NAME=v"):
            self.assertIn(shape, self.block())
        self.assertIn("1500 characters", contract)  # T-5598: the report cap is a rule, so the brief's copy carries it

    def test_the_lane_brief_leaves_the_passing_shapes_to_the_agent(self):  # T-5595: the agent file carries them once
        tid = self.task("Shaped")
        brief = read_text(json.loads(self.fm("lane", "brief", tid, "--json").stdout)["path"])
        self.assertNotIn(self.block(), brief, "the brief does not repeat the agent's shapes")
        self.assertIn("sampo lane start " + tid, brief)

    def test_passing_shapes_go_to_a_subagent_in_a_managed_worktree_only(self):
        # T-4139: a subagent in a linked worktree of a managed project is told the shapes; one in the main checkout (a
        # scout, a reviewer: no evidence there) and one in a folder Sampo doesn't manage are not
        wt = os.path.join(self.tmp, "agent-a1")
        subprocess.run(["git", "-C", self.repo, "worktree", "add", "-q", "-b", "agent-a1", wt], check=True)
        self.assertIn(self.block(), self.start_context(wt))
        self.assertNotIn("**Passing shapes:**", self.start_context(self.repo))
        free = os.path.join(self.tmp, "free")
        os.makedirs(free)
        self.assertNotIn("**Passing shapes:**", self.start_context(free))

    def test_passing_shapes_ac_add_notes_only_the_forms_the_harness_refuses(self):
        tid = self.task("Noted")
        r = self.fm("task", "ac", tid, "add", "Compound check :: cd . && python3 -c 'print(3)'", check=False)
        self.assertIn("--run-file", r.stderr)
        r = self.fm("task", "ac", tid, "add", "Sed check :: sed -n 1p README.md", check=False)
        self.assertIn("--run-file", r.stderr)
        plain = self.fm("task", "ac", tid, "add", "Plain check :: python3 -c 'print(2)'", check=False)
        self.assertNotIn("--run-file", plain.stderr)
        git = self.fm("task", "ac", tid, "add", "Git check :: git status --short", check=False)
        self.assertNotIn("--run-file", git.stderr, "plain git passes the harness, so it gets no note")


class IsolationRefusals(_Tasks):
    """T-4139: `sampo lane stats` counts the harness's isolation refusals, from the failed calls the hooks log."""

    def test_lane_stats_counts_the_isolation_refusals_of_this_project(self):
        refused = "This agent is isolated in the worktree: cannot be shown not to be git"
        for _ in range(2):
            self.hook("PostToolUseFailure", {"session_id": "s1", "cwd": self.repo, "tool_name": "Bash", "agent_id": "a1",
                                              "tool_input": {"command": "cat > x <<EOF"}, "error": refused})
        self.hook("PostToolUseFailure", {"session_id": "s1", "cwd": self.repo, "tool_name": "Bash",
                                          "tool_input": {"command": "false"}, "error": "exit 1"})
        out = json.loads(self.fm("lane", "stats", "--json").stdout)
        self.assertEqual(out["refusals"], {"all": 2, "last_7_days": 2}, "the other failure is not a refusal")
