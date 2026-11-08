"""fm repeats: procedures and commands a project keeps repeating, and what to turn them into (T-0023)."""
import datetime
import json
import os
from unittest import mock

from helpers import ForemanTestCase, git_repo

from sampo import core as c
from sampo import repeats


class Repeats(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        for i in range(1, 4):
            tid = self.fm_json("task", "new", f"Fix bug {i}", "--type", "FIX", "--tier", "S",
                               "--step", "Write a failing test", "--step", f"fix bug {i}",
                               "--step", "Update the CHANGELOG.")["id"]
            self.fm("task", "evidence", tid, "--step", "1", f"python3 -m unittest tests.test_bug{i} -q", "1 failed")
            self.fm("task", "evidence", tid, "--step", "2", "python3 -m unittest discover -s tests", f"Ran {40 + i} OK")
            self.fm("task", "evidence", tid, "--step", "2", "npm run build && npm run lint", "ok")
            self.fm("task", "evidence", tid, "--step", "3", f"fm task log {tid} 'done'", "logged")
        self.fm("task", "evidence", "T-0001", "--step", "2", "cargo fmt --check", "ok")  # once: not a repeat

    def report(self):
        return self.fm_json("repeats")

    def test_repeated_commands_and_procedures_with_what_to_make_of_them(self):
        r = self.report()
        cmds = {x["shape"]: x for x in r["commands"]}
        self.assertEqual(cmds["python3 -m unittest …"]["count"], 6)
        self.assertEqual(cmds["python3 -m unittest …"]["tasks"], 3)
        self.assertIn("fm check add", cmds["python3 -m unittest …"]["suggest"])
        self.assertIn("script", cmds["npm run build && npm run lint"]["suggest"])
        self.assertFalse(any(s.startswith(("fm ", "cargo")) for s in cmds), "Foreman's own commands and one-offs")
        steps = {x["step"]: x for x in r["steps"]}
        self.assertEqual(set(steps), {"write a failing test", "update the changelog"})
        self.assertIn(".claude/skills/", steps["write a failing test"]["suggest"])

    def test_a_sampo_spelled_evidence_command_is_not_a_habit(self):  # T-2845
        for i in range(1, 4):
            self.fm("task", "evidence", f"T-000{i}", "--step", "3", f"sampo task log T-000{i} 'done'", "logged")
        self.assertFalse(any(x["shape"].startswith("sampo") for x in self.report()["commands"]))

    def test_interpreter_flags_do_not_split_a_habit_and_git_reads_are_not_habits(self):
        from sampo import repeats
        self.assertEqual(repeats.shape("python3 -W error::ResourceWarning -m unittest discover -s x"),
                         repeats.shape("python3 -m unittest test_a"))
        for i in range(1, 4):
            self.fm("task", "evidence", f"T-000{i}", "--step", "2", "git log --oneline -1", "abc123 fix")
        self.assertFalse(any(x["shape"].startswith("git log") for x in self.report()["commands"]))

    def test_a_narrowed_test_run_is_never_offered_as_a_gate(self):
        # T-0857: this repo was told to make "unittest … -p test_diff_gates.py -k dirty" a gate for every task
        from sampo import repeats
        for narrow in ("python3 -m unittest discover -s t -q -p test_diff_gates.py -k dirty",
                       "python3 plugin/tests/run.py test_bus", "pytest tests/test_a.py::test_b", "pytest -k slow"):
            self.assertNotIn("fm check add", repeats.suggest("python3 -m unittest …", narrow), narrow)
        cmds = {x["shape"]: x for x in self.report()["commands"]}  # the habit holds both kinds: the whole-suite one
        self.assertIn('fm check add "python3 -m unittest discover -s tests"', cmds["python3 -m unittest …"]["suggest"])

    def test_gate_words_count_in_the_command_not_in_directory_names(self):
        from sampo import repeats
        self.assertIn("fm check add", repeats.suggest("python3 plugin/tests/bench_hooks.py --runs …", "x"))
        self.assertNotIn("fm check add", repeats.suggest("cp dist/build/app.js …", "x"))
        self.assertNotIn("fm check add", repeats.suggest("git checkout main", "x"))
        self.assertEqual(repeats.shape("cd sub &&"), "")

    def test_handled_items_can_be_dismissed_and_bad_meta_is_ignored(self):
        # round-2 audits: a repeat turned into a script or skill stops being reported; a stray non-string gate is skipped
        self.fm("repeats", "dismiss", "npm run build && npm run lint")
        self.fm("repeats", "dismiss", "Write a failing test.")
        r = self.report()
        self.assertNotIn("npm run build && npm run lint", {x["shape"] for x in r["commands"]})
        self.assertEqual({x["step"] for x in r["steps"]}, {"update the changelog"})
        self.assertIn("2 dismissed", self.fm("repeats").stdout)
        p = c.find_project(self.repo)
        c.update_meta(p, checks=[None, 3, "python3 -m unittest discover -s tests"])
        self.assertTrue({x["shape"]: x for x in self.report()["commands"]}["python3 -m unittest …"]["covered"])
        self.assertEqual(self.fm("tidy", check=False).returncode, 0)

    def test_commands_fm_check_already_runs_are_marked_covered(self):
        self.fm("check", "add", "python3 -m unittest discover -s tests")
        cmds = {x["shape"]: x for x in self.report()["commands"]}
        self.assertTrue(cmds["python3 -m unittest …"]["covered"])
        self.assertFalse(cmds["npm run build && npm run lint"]["covered"])
        out = self.fm("repeats").stdout
        self.assertIn("covered by fm check", out)
        self.assertIn("npm run build && npm run lint", out)

    def test_tidy_counts_what_could_become_a_project_tool(self):
        found = [f for f in json.loads(self.fm("tidy", "--json").stdout)["findings"] if f["kind"] == "repeats"]
        self.assertEqual(len(found), 1)
        self.assertIn("fm repeats", found[0]["fix"])

    def test_nothing_repeated(self):
        fresh = git_repo(self.tmp, "fresh")
        self.fm("init", cwd=fresh)
        self.assertIn("Nothing repeated", self.fm("repeats", cwd=fresh).stdout)
        self.assertEqual(self.fm_json("repeats", cwd=fresh), {"commands": [], "steps": []})

    def test_forge_drafts_a_script_and_a_skill_and_files_one_task_each_once(self):
        # T-1070: nothing is adopted; the draft sits in research/ and a captured task carries its path
        paths = {d["path"]: d["task"] for d in self.fm_json("repeats", "--forge")["drafts"]}
        script = next(x for x in paths if x.endswith(".sh") and "npm" in open(x).read())
        self.assertIn("npm run build && npm run lint", open(script).read())
        skill = next(x for x in paths if x.endswith("SKILL.md") and "Write a failing test" in open(x).read())
        gate = next(x for x in paths if x.endswith(".sh") and "unittest" in open(x).read())
        self.assertIn("fm check add", open(gate).read())
        p = c.find_project(self.repo)
        rel = os.path.relpath(script, p.dir)
        self.assertEqual(len([b for b in c.load_briefs(p) if rel in b.section("Raw request")]), 1)
        self.assertEqual(self.fm_json("repeats", "--forge")["drafts"], [], "a second run files nothing new")
        self.assertEqual(len([b for b in c.load_briefs(p) if rel in b.section("Raw request")]), 1)

    def test_forge_repairs_a_missing_draft_without_a_new_task_and_files_a_real_task(self):
        import os
        d = self.fm_json("repeats", "--forge")["drafts"][0]
        self.assertEqual(os.stat(d["path"]).st_mode & 0o111, 0, "a draft is never executable")
        p = c.find_project(self.repo)
        n = len(c.load_briefs(p))
        b = c.find_brief(p, d["task"])
        self.assertTrue(b.title.startswith("Adopt forge draft: "))
        self.assertIn("Forge draft: research/forge-", b.section("Raw request"))
        self.assertEqual(len(b.steps()), 1)
        self.assertTrue(b.acceptance())
        os.remove(d["path"])
        again = self.fm_json("repeats", "--forge")["drafts"]
        self.assertEqual(again, [{"path": d["path"], "task": d["task"]}])
        self.assertTrue(os.path.exists(d["path"]))
        self.assertEqual(len(c.load_briefs(p)), n)

    def test_forge_keeps_long_shared_prefixes_apart_and_redacts_and_quotes(self):
        prefix = "./deploy-to-the-staging-cluster.sh --target"  # one 40+ character prefix, two shapes
        for i in range(1, 4):
            tid = self.fm_json("task", "new", f"Deploy {i}", "--type", "FIX", "--tier", "S", "--step", "s")["id"]
            for tail in ("alpha && ./a", "beta && ./b --token ghp_" + "a1B2" * 9):
                self.fm("task", "evidence", tid, "--step", "1", f"{prefix} {tail}", "ok")
        paths = [d["path"] for d in self.fm_json("repeats", "--forge")["drafts"] if "deploy" in d["path"]]
        self.assertEqual(len(paths), 2)
        self.assertEqual(len(set(paths)), 2)
        self.assertFalse(any("ghp_" in open(x).read() for x in paths))
        self.assertTrue(any("[REDACTED" in open(x).read() for x in paths))

    def test_forge_skips_dismissed_shapes(self):
        self.fm("repeats", "dismiss", "npm run build && npm run lint")
        self.fm("repeats", "dismiss", "Write a failing test.")
        for d in self.fm_json("repeats", "--forge")["drafts"]:
            self.assertNotIn("npm run", open(d["path"]).read())
            self.assertNotIn("Write a failing test", open(d["path"]).read())


class Sequences(ForemanTestCase):  # T-3921: multi-call routines, from the transcripts (main and subagents) and the event log
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.cfg = os.path.join(self.tmp, "claude")
        self.folder = os.path.join(self.cfg, "projects", "".join(ch if ch.isalnum() else "-" for ch in self.repo))
        self.now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        self.env = {"CLAUDE_CONFIG_DIR": self.cfg}

    def write(self, rel, sid, calls):  # one assistant line per tool call, as Claude Code writes them
        path = os.path.join(self.folder, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        lines = [{"type": "assistant", "timestamp": self.now, "sessionId": sid,
                  "message": {"content": [{"type": "tool_use", "id": f"{sid}-{i}", "name": n, "input": inp}]}}
                 for i, (n, inp) in enumerate(calls)]
        with open(path, "w") as f:
            f.write("\n".join(json.dumps(x) for x in lines) + "\n")

    def routine(self, k):  # the same four calls, each with its own ids, paths and quoted text
        return [("Bash", {"command": f"git -C /work/repo{k} status"}),
                ("Bash", {"command": f"sampo task evidence T-000{k} --step 1 --run 'pytest case {k}'"}),
                ("Edit", {"file_path": f"/work/repo{k}/a.py"}),
                ("Bash", {"command": f"python3 -m unittest tests.test_a{k} -q"})]

    def test_repeats_sequences_mine_routines_from_main_and_subagent_transcripts(self):
        self.write("s1.jsonl", "s1", self.routine(1) + self.routine(4))  # two runs in s1
        self.write("s2.jsonl", "s2", self.routine(2) + [("Read", {"file_path": "/x"})])
        self.write("s2/subagents/agent-a1.jsonl", "s2", self.routine(3))  # a subagent of s2: a run of its own
        self.write("s3.jsonl", "s3", [("Bash", {"command": "ls -la"}), ("Read", {"file_path": "/y"}),
                                      ("Grep", {"pattern": "z"})])  # once: not a routine
        r = self.fm_json("repeats", "--sequences", env=self.env)
        seqs = r["sequences"]
        self.assertEqual(len(seqs), 1, "the four-call routine; its three-call parts are the same runs, not more")
        top = seqs[0]
        self.assertEqual((top["count"], top["sessions"], top["saved"]), (4, 3, 12))  # three transcript files: 2 + 1 + 1 runs
        self.assertEqual(top["calls"], ["git status", "sampo task evidence …", "Edit", "python3 -m unittest …"])
        self.assertTrue(top["example"][0].startswith("git -C /work/repo"), top["example"])
        self.assertIn("pytest case", top["example"][1])
        self.assertFalse(r["truncated"])
        text = self.fm("repeats", "--sequences", env=self.env).stdout
        self.assertIn("4× in 3 sessions  saves 12 calls", text)
        self.assertIn("→ Edit →", text)

    def test_repeats_sequences_count_the_event_log_when_no_transcript_has_the_session(self):  # T-3921
        for sid, runs in (("L1", 2), ("L2", 2)):
            for _ in range(runs):
                tid = self.fm_json("task", "new", f"Ship {sid}", "--type", "FIX", "--tier", "S",
                                   "--step", "Ship it")["id"]
                p = c.find_project(self.repo)
                for cmd in ("npm ci", "npm run build", "npm test -- --ci"):  # the evidence events a run writes
                    c.log_event(p, "evidence", task=tid, data={"step": 1, "cmd": cmd, "auto": "runner"}, session=sid)
        r = self.fm_json("repeats", "--sequences", env=self.env)
        # the event log has no run boundaries: each session's two runs read as one six-call routine, which saves more
        self.assertEqual([(x["calls"], x["count"], x["sessions"]) for x in r["sequences"]],
                         [(["npm ci", "npm run build", "npm test -- …"] * 2, 2, 2),
                          (["npm ci", "npm run build", "npm test -- …"], 4, 2)])

    def test_repeats_sequences_one_session_with_many_runs_is_not_two_sessions(self):  # 2+ runs in 2+ sessions
        self.write("s1.jsonl", "s1", self.routine(1) + self.routine(4) + self.routine(5))
        self.assertEqual(self.fm_json("repeats", "--sequences", env=self.env)["sequences"], [])

    def test_repeats_sequences_a_routine_each_session_runs_once_still_repeats(self):  # T-3921: real shape
        # `sampo task show` -> `sampo task set` -> `sampo lane brief`, once in each of two sessions, twice in one
        routine = [("Bash", {"command": "sampo task show T-3921"}),
                   ("Bash", {"command": "sampo task set T-3921 --section Approach --text x"}),
                   ("Bash", {"command": "python3 plugin/bin/sampo lane brief T-3921"})]
        self.write("s1.jsonl", "s1", routine + routine)
        self.write("s2.jsonl", "s2", routine)
        self.write("s3.jsonl", "s3", [("Read", {"file_path": "/x"})])
        r = self.fm_json("repeats", "--sequences", env=self.env)
        self.assertEqual([(x["calls"], x["count"], x["sessions"]) for x in r["sequences"]],
                         [(["sampo task show …", "sampo task set …", "sampo lane brief …"], 3, 2)])

    def test_repeats_sequences_keep_secrets_and_home_paths_out_of_the_output(self):
        token = "ghp_" + "a" * 36
        calls = [("Bash", {"command": f"git clone https://x:{token}@github.com/o/r"}),
                 ("Bash", {"command": "curl -u admin:hunter2 https://h.example/x"}),
                 ("Bash", {"command": "export TOKEN=abcd1234efgh5678"}),
                 ("Edit", {"file_path": os.path.join(os.path.expanduser("~"), "proj", "a.py")})]
        self.write("s1.jsonl", "s1", calls + calls)
        self.write("s2.jsonl", "s2", calls + calls)
        out = json.dumps(self.fm_json("repeats", "--sequences", env=self.env))
        text = self.fm("repeats", "--sequences", env=self.env).stdout
        for shown in (out, text):
            for secret in (token, "hunter2", "abcd1234efgh5678"):
                self.assertNotIn(secret, shown)
        self.assertIn("[REDACTED]", text)
        home = os.path.expanduser("~")
        if home not in ("", "/"):
            self.assertNotIn(home + "/proj", out)

    def test_repeats_sequences_keep_unrelated_calls_apart(self):  # the same routine on other repos still merges
        norm = repeats._norm_cmd
        self.assertEqual(norm("git -C /work/repo1 status"), norm("git -C /work/repo2 status"))
        self.assertNotEqual(norm("git -C /r status"), norm("git -C /r diff"))
        self.assertNotEqual(norm("git -C /r diff"), norm("git -C /r commit -m x"))
        self.assertNotEqual(norm("python3 scripts/a.py"), norm("python3 tools/b.py"))
        self.assertEqual(norm("python3 plugin/bin/sampo lane brief T-1"), norm("fm lane brief T-2"))  # T-3921
        self.assertEqual(norm("S=/tmp/x; cd ~/proj; uptime | head -1"), "uptime")  # setup is not the call

    def test_repeats_sequences_drop_generic_noise_that_has_no_two_specific_calls(self):
        for sid, name in (("s1", "a"), ("s1", "b"), ("s2", "c"), ("s2", "d")):  # Read, Edit, Read, Grep: no Bash
            self.write(f"{sid}-{name}.jsonl", sid, [("Read", {"file_path": f"/{name}"}), ("Edit", {"file_path": "/x"}),
                                                    ("Read", {"file_path": "/y"}), ("Grep", {"pattern": "z"})])
        self.assertEqual(self.fm_json("repeats", "--sequences", env=self.env)["sequences"], [])

    def test_repeats_sequences_read_the_newest_transcripts_first_and_flag_a_cut_read(self):
        old = os.path.join(self.folder, "old.jsonl")
        self.write("old.jsonl", "old", self.routine(1))
        os.utime(old, (0, 0))  # the oldest file: read last, so a cut read leaves it out
        self.write("new.jsonl", "new", self.routine(2))
        p = c.find_project(self.repo)
        since = c.iso(c.clock().timestamp() - 7 * 86400)
        with mock.patch.dict(os.environ, self.env), mock.patch.object(repeats, "SEQ_CALL_CAP", 3):
            units, truncated = repeats._units(p, since)
        paths = [path for _, path in units]
        self.assertTrue(truncated)
        self.assertTrue(any(x.endswith("new.jsonl") for x in paths), paths)
        self.assertFalse(any(x.endswith("old.jsonl") for x in paths), paths)

    def test_repeats_sequences_days_are_checked_and_need_the_flag(self):
        for days in ("inf", "nan", "0", "3651"):
            r = self.fm("repeats", "--sequences", "--days", days, env=self.env, check=False)
            self.assertNotEqual(r.returncode, 0, days)
            self.assertIn("--days needs", r.stderr)
        self.assertEqual(self.fm("repeats", "--sequences", "--days", "3650", env=self.env).returncode, 0)
        r = self.fm("repeats", "--days", "3", env=self.env, check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("goes with --sequences", r.stderr)
