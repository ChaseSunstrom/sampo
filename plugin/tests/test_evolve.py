"""fm evolve (T-0224): one bench-gated generation of self-improvement — a tool-less child revises one Foreman
instruction file, the candidate lives on an evolve branch in its own worktree, both arms are benched, the gate decides."""
import json
import os
import sys

from helpers import ForemanTestCase, read_text
from test_bench import git

from sampo import core as c

STUB = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
if args == ["--version"]:  # T-1255: run_arm stamps the Claude Code version
    print("2.1.304 (Claude Code)")
    sys.exit(0)
system = args[args.index("--append-system-prompt") + 1] if "--append-system-prompt" in args else ""
stdin = "" if "--plugin-dir" in args else sys.stdin.read()
with open(os.environ["STUB_LOG"], "a") as f:
    f.write(json.dumps({"args": args, "mutate": "Sampo instruction file" in system, "stdin": stdin}) + "\n")
if "Sampo instruction file" in system:
    print("<file>\nREVISED: say less, verify more\n</file>\nWhy: shorter rules cost fewer tokens")
    sys.exit(0)
plugin = args[args.index("--plugin-dir") + 1]
if os.environ.get("STUB_SOLVE") or (os.environ.get("STUB_SOLVE_LIVE_ONLY") and ".evolve" not in plugin):
    with open("calc.py", "w") as f:
        f.write("def double(x):\n    return 2 * x\n")
cost = 0.1 if ".evolve" in plugin and os.environ.get("STUB_CHEAPER") else 0.2
sid = "stub-session"
d = os.path.join(os.environ["CLAUDE_CONFIG_DIR"], "projects", "p")  # T-3364: a fresh run's transcript is audited
os.makedirs(d, exist_ok=True)
with open(os.path.join(d, sid + ".jsonl"), "w") as f:
    f.write(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Write", "input": {"file_path": "calc.py"}}]}}) + "\n")
print(json.dumps({"type": "result", "is_error": False, "total_cost_usd": cost, "num_turns": 5, "result": "done",
                  "session_id": sid}))
'''


class Evolve(ForemanTestCase):
    def setUp(self):
        super().setUp()
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir)
        with open(os.path.join(bindir, "claude"), "w") as f:
            f.write(STUB)
        os.chmod(os.path.join(bindir, "claude"), 0o755)
        self.log = os.path.join(self.tmp, "stub.log")
        self.env = {"PATH": bindir + os.pathsep + os.environ["PATH"], "STUB_LOG": self.log,
                    "CLAUDE_CONFIG_DIR": os.path.join(self.tmp, "cc")}  # T-3364: where the stub keeps its transcript
        # a Foreman repo to evolve: a plugin that ships the guard, an fm that runs the real one, one skill
        self.frepo = os.path.join(self.tmp, "fake-foreman")
        files = {"plugin/.claude-plugin/plugin.json": '{"name": "foreman"}',
                 "plugin/hooks/hooks.json": '{"hooks": {"PreToolUse": [{"matcher": "Bash|Write", "hooks": []}]}}',
                 "plugin/lib/fmguard.py": "", "plugin/skills/demo/SKILL.md": "OLD: the rules as they are\n",
                 "plugin/bin/fm": f"#!/bin/sh\nexec {sys.executable} {os.path.join(c.PLUGIN_ROOT, 'bin', 'fm')} \"$@\"\n"}
        for rel, text in files.items():
            path = os.path.join(self.frepo, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(text)
        os.chmod(os.path.join(self.frepo, "plugin", "bin", "fm"), 0o755)
        git(self.tmp, "init", "-q", self.frepo)
        git(self.frepo, "add", "-A")
        git(self.frepo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "fake foreman")
        # one bench case in the project, as in test_bench
        self.fm("init")
        self.write("calc.py", "def double(x):\n    return x + x + 1\n")
        git(self.repo, "add", "calc.py")
        git(self.repo, "commit", "-qm", "calc")
        tid = json.loads(self.fm("task", "new", "double() is off by one", "--type", "FIX", "--tier", "S", "--ac",
                                 "fixed :: PYTHONPATH=. python3 tests/test_calc.py", "--step", "fix", "--json").stdout)["id"]
        self.fm("focus", tid)
        self.fm("task", "step", tid, "done", "1", "--evidence", "x", "ok")
        self.fm("task", "ac", tid, "check", "1", "--evidence", "x", "ok")
        self.fm("task", "audit", tid, "self", "x", "ok")
        self.fm("task", "set", tid, "--section", "Regression test", "--text", "none: fixture")
        self.fm("task", "done", tid)
        self.write("calc.py", "def double(x):\n    return x * 2\n")
        self.write("tests/test_calc.py", "from calc import double\nassert double(3) == 6\nprint('ok')\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", f"fix ({tid})")
        self.fm("bench", "build", env=self.env)

    def write(self, rel, text):
        path = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def evolve(self, *extra, env=None):
        out = self.fm("evolve", "--repo", self.frepo, "--target", "plugin/skills/demo/SKILL.md", "--json", *extra,
                      env=dict(self.env, **(env or {})), check=False)
        return json.loads(out.stdout)

    def calls(self):
        return [json.loads(l) for l in read_text(self.log).splitlines()]

    def test_a_candidate_no_worse_is_kept_on_its_branch(self):
        res = self.evolve(env={"STUB_SOLVE": "1", "STUB_CHEAPER": "1"})
        self.assertTrue(res["kept"], res)
        self.assertTrue(res["improved"])
        self.assertTrue(res["branch"].startswith("evolve/"))
        self.assertIn(res["branch"], git(self.frepo, "branch", "--list", "evolve/*"))
        self.assertEqual(read_text(os.path.join(res["worktree"], "plugin/skills/demo/SKILL.md")),
                         "REVISED: say less, verify more\n")
        self.assertEqual(read_text(os.path.join(self.frepo, "plugin/skills/demo/SKILL.md")),
                         "OLD: the rules as they are\n", "the live copy is untouched")
        mutate = [x for x in self.calls() if x["mutate"]]
        self.assertEqual(len(mutate), 1)
        self.assertIn("OLD: the rules as they are", mutate[0]["stdin"], "the child sees the file it revises")
        self.assertIn("--tools", mutate[0]["args"])
        self.assertEqual(mutate[0]["args"][mutate[0]["args"].index("--tools") + 1], "", "and nothing else")

    def test_a_worse_candidate_is_dropped_without_a_trace(self):
        res = self.evolve(env={"STUB_SOLVE_LIVE_ONLY": "1"})
        self.assertFalse(res["kept"], res)
        self.assertIn("fewer passes", " ".join(res["gate"]))
        self.assertEqual(git(self.frepo, "branch", "--list", "evolve/*"), "")
        self.assertFalse(os.path.exists(res["worktree"]))

    def test_a_tie_is_not_kept_unless_it_is_an_ablation(self):
        # T-0224 review: "no worse" isn't "better"; a tie would only leave a branch behind
        res = self.evolve(env={"STUB_SOLVE": "1"})
        self.assertFalse(res["kept"], res)
        self.assertIn("tie", res["why_not"])
        self.assertEqual(git(self.frepo, "branch", "--list", "evolve/*"), "")

    def test_only_instruction_text_can_be_evolved(self):
        # T-0224 review: an LLM-written guard or hook would run unsandboxed in the replays
        for target in ("plugin/lib/fmguard.py", "plugin/hooks/hooks.json", "plugin/bin/fm", "../x.md"):
            with self.subTest(target=target):
                out = self.fm("evolve", "--repo", self.frepo, "--target", target, env=self.env, check=False)
                self.assertNotEqual(out.returncode, 0)
                self.assertIn("instruction", out.stdout + out.stderr)
        self.assertFalse(os.path.exists(self.log), "nothing ran")

    def test_both_arms_start_from_the_last_commit(self):
        # T-0224 review: uncommitted edits made the arms differ by more than the revision
        with open(os.path.join(self.frepo, "plugin/skills/demo/SKILL.md"), "w") as f:
            f.write("DIRTY: not committed\n")
        res = self.evolve(env={"STUB_SOLVE": "1", "STUB_CHEAPER": "1"})
        mutate = next(x for x in self.calls() if x["mutate"])
        self.assertIn("OLD: the rules as they are", mutate["stdin"])
        self.assertNotIn("DIRTY", mutate["stdin"])
        plugins = [x["args"][x["args"].index("--plugin-dir") + 1] for x in self.calls() if "--plugin-dir" in x["args"]]
        self.assertFalse(any(os.path.realpath(x).startswith(os.path.realpath(self.frepo) + os.sep) for x in plugins),
                         "neither arm runs the working tree")
        self.assertTrue(res["kept"])

    def test_a_revision_that_loses_the_frontmatter_is_refused(self):
        path = os.path.join(self.frepo, "plugin/skills/fm/SKILL.md")
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as f:
            f.write("---\nname: fm\ndescription: x\n---\nbody\n")
        git(self.frepo, "add", "-A")
        git(self.frepo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "fm skill")
        out = self.fm("evolve", "--repo", self.frepo, "--target", "plugin/skills/fm/SKILL.md", env=self.env, check=False)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("frontmatter", out.stdout + out.stderr)

    def test_drop_is_an_ablation_without_a_mutation_child(self):
        res = self.evolve("--drop", env={"STUB_SOLVE": "1"})
        self.assertTrue(res["kept"], res)
        self.assertEqual(read_text(os.path.join(res["worktree"], "plugin/skills/demo/SKILL.md")), "")
        self.assertFalse(any(x["mutate"] for x in self.calls()))


class BetterOutsideNoise(__import__("unittest").TestCase):
    """T-1435: evolve kept a candidate on any pass gain or cost drop, even one inside the bench noise band (T-1260)."""

    @staticmethod
    def result(rates, cost, spread):
        return {"cases": [{"id": f"c{i}", "pass_rate": r, "runs": 3, "cost_usd": cost,
                           "attempts": [{"cost_usd": cost - spread}, {"cost_usd": cost}, {"cost_usd": cost + spread}]}
                          for i, r in enumerate(rates)]}

    def test_a_win_inside_the_noise_band_is_not_better(self):
        from sampo import evolve as sevolve
        base = self.result([2 / 3] * 6, 1.00, 0.30)
        self.assertFalse(sevolve._better(base, self.result([2 / 3] * 5 + [1.0], 1.00, 0.30)), "one pass inside the band")
        self.assertFalse(sevolve._better(base, self.result([2 / 3] * 6, 0.85, 0.30)), "cheaper, inside the cost band")
        self.assertTrue(sevolve._better(base, self.result([1.0] * 6, 1.00, 0.30)), "six passes ahead: outside")
        self.assertTrue(sevolve._better(base, self.result([2 / 3] * 6, 0.40, 0.01)), "much cheaper, tight spread")

    def test_a_pass_rate_out_of_range_is_read_within_0_and_1(self):
        from sampo import bench
        self.assertEqual(bench._rate({"pass_rate": 5}), 1.0)
        self.assertEqual(bench._rate({"pass_rate": -1}), 0.0)
        self.assertEqual(bench._rate({"pass_rate": "x"}), 0.0)
        self.assertEqual(bench._rate({"pass": True}), 1.0)
