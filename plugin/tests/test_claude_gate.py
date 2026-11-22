"""T-5584: no benchmark, eval or comparison that calls a Claude model runs unless the user has turned on claude_bench
(`sampo claude-bench on`, typed by the user: the guard refuses an agent's own). A refusal names that command. A local
profile and the oracle arm are never refused, and the nightly timer skips the paid jobs while the gate is closed."""
import ast
import glob
import json
import os
from unittest import mock

from helpers import ForemanTestCase

from sampo import cli
from sampo import core as c
from sampo import models

GATE = "! sampo claude-bench on"
LIB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib", "sampo")
# T-5584: every function that builds a headless `claude -p` argv must call the gate, or be listed here with why it is
# not a benchmark, eval or comparison start. A new start path that skips the gate fails test_every_claude_start_path.
UNGATED = {
    ("session.py", "argv"): "the user's own sampo session, not a benchmark, eval or comparison",
    ("research.py", "_child"): "sampo research's child, a question the user asks",
    ("auditrun.py", "_argv"): "the audit lane's review child, a code review",
    ("serve.py", "_claude_cmd"): "sampo serve's job child, the user's own build",
    ("ideas.py", "child_cmd"): "fm relate's brainstorm child: it writes ideas, it is not a benchmark or eval; exam "
                               "authoring through it is noted for a follow-up (T-5584)",
}
GATED_CALLS = ("claude_gate(", "gate_here(", "open_here(")
# the start paths that must gate themselves whatever calls them (a run's cmd gate is not enough alone)
BACKSTOPS = [("bench.py", "_turn"), ("kalevala.py", "session_argv"), ("kalevala.py", "_spawn"),
             ("bench_swe.py", "profile_for"), ("benchtb.py", "run_bench")]


def claude_p_argv(node):
    """True for a list or tuple literal that starts `claude -p`: a headless model call."""
    if not isinstance(node, (ast.List, ast.Tuple)) or len(node.elts) < 2:
        return False
    first, second = node.elts[0], node.elts[1]
    return (isinstance(first, ast.Constant) and first.value == "claude"
            and isinstance(second, ast.Constant) and second.value == "-p")


def lib_functions():
    """{(module file name, function name): (source, starts a claude -p argv)} for every function in the library."""
    found = {}
    for path in sorted(glob.glob(os.path.join(LIB, "*.py"))):
        with open(path, encoding="utf-8") as f:
            text = f.read()
        tree = ast.parse(text)
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                src = ast.get_source_segment(text, fn) or ""
                starts = any(claude_p_argv(n) for n in ast.walk(fn))
                found[(os.path.basename(path), fn.name)] = (src, starts)
    return found


def claude_entries(tasks):
    """Each entry the gate must hold: a Claude session, a Claude-resolving profile, or a Claude reference arm."""
    return {
        "bench duel": ["bench", "duel"],
        "bench court": ["bench", "court"],
        "bench soak": ["bench", "soak"],
        "bench swe on a Claude profile": ["bench", "swe", "--model", "anthropic"],
        "bench tbench on a Claude profile": ["bench", "tbench", "--model", "anthropic", "--tasks", tasks],
        "exam run --agent foreman": ["exam", "run", "exam-x", "--agent", "foreman"],
        "exam run --agent plain": ["exam", "run", "exam-x", "--agent", "plain"],
        "exam run --agent smith with a Claude model": ["exam", "run", "exam-x", "--agent", "smith", "--model", "anthropic"],
        "exam grade --agent genesis": ["exam", "grade", "exam-x", "--agent", "genesis"],
        "exam gap --ref": ["exam", "gap", "exam-x", "--ref"],
        "exam long run --agent plain": ["exam", "long", "run", "exam-x", "--agent", "plain"],
        "smith bench on a Claude profile": ["smith", "bench", "--model", "anthropic", "--seeds", "rename"],
        "smith bench with the claude worker": ["smith", "bench", "--worker", "claude", "--seeds", "rename"],
    }


class ClaudeGate(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.tasks = os.path.join(self.tmp, "tasks")  # an empty task set: tbench never fetches one from the network
        os.makedirs(self.tasks)

    def run_fm(self, args, env=None):
        p = self.fm(*args, check=False, timeout=60, env=env)
        return p.returncode, p.stdout + p.stderr

    def test_each_claude_entry_refuses_without_the_switch_and_names_the_user_command(self):
        for name, args in claude_entries(self.tasks).items():
            code, out = self.run_fm(args)
            self.assertIn(GATE, out, name)
            self.assertNotEqual(code, 0, name)

    def test_the_switch_on_gets_each_entry_past_the_gate(self):
        self.fm("claude-bench", "on")
        self.assertTrue(c.read_meta(c.find_project(self.repo)).get("claude_bench"))
        for name, args in claude_entries(self.tasks).items():
            code, out = self.run_fm(args, env={"PATH": os.path.join(self.tmp, "nobin")})  # no docker, no claude here
            self.assertNotIn(GATE, out, name)

    def test_a_local_profile_is_never_refused(self):
        with open(os.path.join(self.home, "models.json"), "w", encoding="utf-8") as f:
            json.dump({"default": "mine", "profiles": {"mine": {"kind": "openai", "base_url": "http://127.0.0.1:9/v1",
                                                                "model": "x"}}}, f)
        for args in (["bench", "swe", "--model", "mine"], ["bench", "tbench", "--model", "mine", "--tasks", self.tasks],
                     ["exam", "run", "exam-x", "--agent", "smith", "--model", "mine"]):
            code, out = self.run_fm(args, env={"PATH": os.path.join(self.tmp, "nobin")})
            self.assertNotIn(GATE, out, " ".join(args))

    def test_the_oracle_arm_is_never_refused(self):
        code, out = self.run_fm(["exam", "run", "exam-x", "--agent", "oracle"])
        self.assertNotIn(GATE, out)

    def test_read_only_verbs_are_never_refused(self):
        for args in (["bench", "list"], ["bench", "show", "nope"], ["exam", "gap", "exam-x"]):
            code, out = self.run_fm(args)
            self.assertNotIn(GATE, out, " ".join(args))

    def test_the_switch_is_the_users_and_turns_off(self):
        self.fm("claude-bench", "on")
        self.fm("claude-bench", "off")
        code, out = self.run_fm(["bench", "duel"])
        self.assertIn(GATE, out)

    def test_claude_is_judged_by_the_profile_not_the_name(self):
        claude = {"kind": "anthropic", "base_url": "https://api.anthropic.com/v1", "model": "x"}
        routed = {"kind": "openai", "base_url": "https://openrouter.ai/api/v1", "model": "anthropic/claude-sonnet"}
        local = {"kind": "openai", "base_url": "http://127.0.0.1:11434/v1", "model": "claude-x"}
        other = {"kind": "openai", "base_url": "https://api.openai.com/v1", "model": "gpt-4.1-mini"}
        self.assertTrue(models.is_claude(claude))
        self.assertTrue(models.is_claude(routed))
        self.assertFalse(models.is_claude(local), "a loopback profile is local, whatever its model is called")
        self.assertFalse(models.is_claude(other))

    def test_the_night_books_no_paid_claude_job_while_the_gate_is_closed(self):
        from sampo import night
        names = [n for n, argv, _ in night.jobs(c.find_project(self.repo)) if "--ref" in argv or "long" in argv
                 or "court" in argv]
        self.assertEqual(names, [])

    def test_every_claude_start_path_is_gated(self):
        funcs = lib_functions()
        starts = {k for k, (_, starts_claude) in funcs.items() if starts_claude}
        self.assertIn(("bench.py", "_turn"), starts, "the scan must see the bench's claude -p builder")
        for key in sorted(starts):
            src = funcs[key][0]
            if key in UNGATED:
                continue
            self.assertTrue(any(call in src for call in GATED_CALLS), f"{key} starts claude -p and skips the gate")
        for key in UNGATED:
            self.assertIn(key, starts, f"{key} is listed as ungated but no longer starts claude -p: drop it")

    def test_each_backstop_gates_itself(self):
        funcs = lib_functions()
        for key in BACKSTOPS:
            self.assertIn(key, funcs, key)
            self.assertTrue(any(call in funcs[key][0] for call in GATED_CALLS), f"{key} must call the gate itself")

    def test_no_project_keeps_the_gate_closed(self):
        old = os.getcwd()
        os.chdir(self.tmp)  # not a project, not a worktree of one
        try:
            with mock.patch.dict(os.environ, {"FOREMAN_PROJECT": ""}):
                self.assertFalse(models.open_here())
                with self.assertRaises(cli.UsageError) as ctx:
                    models.gate_here("a bench turn")
                self.assertIn(GATE, str(ctx.exception))
        finally:
            os.chdir(old)


class ClaudeWorkerGate(ForemanTestCase):
    """T-5584 round 2: a worker start (smith.worker_argv) is judged by the command it resolves to, not by how the worker
    is spelled. A known non-Claude runner passes; any other worker, and any command that reaches the claude binary (env,
    sh -c, CLAUDE, a symlink), goes through claude_gate and is refused while claude_bench is off."""

    def setUp(self):
        super().setUp()
        self.fm("init")

    def worker(self, spec, tpl=None):
        """worker_argv as a run starts it, in the project's folder; tpl stands in for the runner's work template (as a
        code.toml line would), with which off so the test needs no CLI on PATH."""
        from sampo import smith
        runners = smith.all_runners()
        if tpl is not None:
            runners = {**runners, spec: {**runners.get(spec, {}), "work": tpl}}
        old = os.getcwd()
        os.chdir(self.repo)
        try:
            with mock.patch.object(smith, "all_runners", return_value=runners), \
                    mock.patch.dict(os.environ, {"FOREMAN_PROJECT": ""}):
                return smith.worker_argv(spec, "hi", which=False)
        finally:
            os.chdir(old)

    def assertGated(self, spec, tpl=None):
        from sampo import smith
        with self.assertRaises(smith.CodeError) as ctx:
            self.worker(spec, tpl)
        self.assertIn(GATE, str(ctx.exception), f"{spec!r} {tpl!r}")

    def test_a_plain_claude_worker_is_gated(self):
        self.assertGated("claude")

    def test_env_claude_is_gated(self):
        self.assertGated("env claude")
        self.assertGated("codex", "env claude -p {prompt}")

    def test_sh_c_claude_is_gated(self):
        self.assertGated("sh -c 'claude -p'")
        self.assertGated("codex", "sh -c 'claude -p {prompt}'")
        self.assertGated("codex", 'bash -c "claude -p {prompt}"')

    def test_uppercase_claude_is_gated(self):
        self.assertGated("CLAUDE")
        self.assertGated("codex", "CLAUDE -p {prompt}")

    def test_a_symlink_to_claude_under_another_name_is_gated(self):
        real = os.path.join(self.tmp, "bin", "claude")
        os.makedirs(os.path.dirname(real))
        with open(real, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\n")
        os.chmod(real, 0o755)
        link = os.path.join(self.tmp, "bin", "helper")
        os.symlink(real, link)
        self.assertGated(link)
        self.assertGated("codex", f"{link} -p {{prompt}}")

    def test_a_shell_option_before_c_is_judged(self):
        self.assertGated("codex", "sh -e -c 'claude -p {prompt}'")
        self.assertGated("codex", "bash -x -c 'claude -p {prompt}'")

    def test_env_split_string_is_judged(self):
        self.assertGated("codex", "env -S 'claude -p {prompt}'")

    def test_shell_syntax_in_a_string_is_judged_unreadable(self):
        self.assertGated("codex", "sh -c 'x=claude; $x -p {prompt}'")
        self.assertGated("codex", "sh -c 'exec `which claude` -p {prompt}'")

    def test_a_symlink_into_a_claude_install_is_gated_under_any_name(self):
        real = os.path.join(self.tmp, "lib", "claude-code", "cli.js")
        os.makedirs(os.path.dirname(real))
        with open(real, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\n")
        os.chmod(real, 0o755)
        link = os.path.join(self.tmp, "bin", "codex")
        os.makedirs(os.path.dirname(link))
        os.symlink(real, link)
        self.assertGated("codex", f"{link} -p {{prompt}}")

    def test_a_known_local_worker_still_passes(self):
        self.assertEqual(self.worker("codex")[0], "codex")
        self.assertEqual(self.worker("gemini")[0], "gemini")
        self.assertEqual(self.worker("codex", "codex exec {prompt}")[0], "codex")

    def test_the_switch_on_lets_a_claude_worker_start(self):
        self.fm("claude-bench", "on")
        self.assertEqual(self.worker("claude")[0], "claude")
        self.assertEqual(self.worker("env claude")[0], "env claude")  # the spec is the command when no template


class UserDeclaredWorker(ForemanTestCase):
    """T-5812: the user, and only the user, names a custom --worker as non-Claude (`sampo claude-bench worker NAME`, stored
    in the project's meta), and a bench child gets the parent's switch and declarations through its own meta, never
    through an env var or a flag an agent could set."""

    def setUp(self):
        super().setUp()
        self.fm("init")
        self.bin = os.path.join(self.tmp, "bin")
        for name in ("mycli", "claude"):  # a declaration pins the file a name runs, so the name has to be one
            self.script(name)

    def script(self, name, body="#!/bin/sh\necho ok\n", where=None):
        d = where or self.bin
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        os.chmod(path, 0o755)
        return path

    def env(self, *dirs):
        """The PATH a user types the declaration under, and a worker starts under: dirs first, then the bin folder."""
        return {"PATH": os.pathsep.join([*dirs, self.bin, os.environ.get("PATH", "")])}

    def worker(self, spec, tpl=None, env=None):
        from sampo import smith
        runners = smith.all_runners()
        if tpl is not None:
            runners = {**runners, spec: {**runners.get(spec, {}), "work": tpl}}
        old = os.getcwd()
        os.chdir(self.repo)
        try:
            with mock.patch.object(smith, "all_runners", return_value=runners), \
                    mock.patch.dict(os.environ, {"FOREMAN_PROJECT": "", **self.env(), **(env or {})}):
                return smith.worker_argv(spec, "hi", which=False)
        finally:
            os.chdir(old)

    def refused(self, spec, tpl=None, env=None):
        from sampo import smith
        with self.assertRaises(smith.CodeError) as ctx:
            self.worker(spec, tpl, env)
        self.assertIn(GATE, str(ctx.exception), f"{spec!r}")
        return str(ctx.exception)

    def meta(self):
        return c.read_meta(c.find_project(self.repo))

    def test_user_declared_worker_runs_with_the_switch_off(self):
        self.refused("mycli")  # undeclared: fail closed
        self.fm("claude-bench", "worker", "mycli", env=self.env())
        self.assertFalse(self.meta().get("claude_bench"), "declaring a worker never turns the switch on")
        pin = self.meta()["non_claude_workers"]["mycli"]
        self.assertEqual(pin["path"], os.path.realpath(os.path.join(self.bin, "mycli")))
        self.assertEqual(len(pin["sha256"]), 64)
        self.assertEqual(self.worker("mycli"), ["mycli", "hi"])
        self.refused("othercli")  # one declared name frees that name only

    def test_user_declared_refusal_names_the_declaring_command(self):
        self.assertIn("sampo claude-bench worker mycli", self.refused("mycli"))

    def test_user_declared_name_never_hides_a_command_that_runs_claude(self):
        self.fm("claude-bench", "worker", "claude", env=self.env())
        self.fm("claude-bench", "worker", "mycli", env=self.env())
        self.refused("claude")
        self.refused("mycli", "sh -c 'claude -p {prompt}'")
        self.refused("mycli", "env claude -p {prompt}")

    def test_user_declared_worker_is_removed_with_rm_and_listed(self):
        self.fm("claude-bench", "worker", "mycli", env=self.env())
        self.fm("claude-bench", "worker", "mycli", env=self.env())  # a second declaration is one entry
        self.assertEqual(list(self.meta()["non_claude_workers"]), ["mycli"])
        self.assertIn("mycli", self.fm("claude-bench").stdout)
        self.fm("claude-bench", "worker", "mycli", "--rm")
        self.assertEqual(self.meta()["non_claude_workers"], {})
        self.refused("mycli")
        with open(os.path.join(c.find_project(self.repo).dir, "ledger.jsonl"), encoding="utf-8") as f:
            events = [json.loads(line)["event"] for line in f if line.strip()]
        self.assertEqual(events.count("claude_bench_worker"), 3)

    def test_user_declared_form_needs_a_name_and_only_the_worker_form(self):
        for args in (["worker"], ["worker", ""], ["on", "mycli"], ["worker", "mycli", "extra"], ["--rm"]):
            p = self.fm("claude-bench", *args, check=False)
            self.assertNotEqual(p.returncode, 0, args)
        self.assertFalse(self.meta().get("non_claude_workers"))

    def test_user_declared_verb_is_a_claude_bench_guard_argv(self):
        import fmguard as g
        ctx = g.Ctx(cwd=self.repo, project_root=g.project_root_for(self.repo, self.home), home=self.home,
                    foreman_home=self.home, scratch=["/tmp"], allow=set(), task_id="T-0007")
        for cmd in ("sampo claude-bench worker mycli", "fm claude-bench worker mycli --rm",
                    "sampo -p x claude-bench worker mycli", "sampo claude-bench --rm worker mycli", "fm claude-bench on"):
            r = g.check("Bash", {"command": cmd}, ctx)
            self.assertIsNotNone(r, cmd)
            self.assertEqual(r.category, "self-authorize", cmd)
        for cmd in ("sampo claude-bench", "fm claude-bench off", "sampo -p x claude-bench"):
            self.assertIsNone(g.check("Bash", {"command": cmd}, ctx), cmd)

    def test_user_declared_names_are_not_read_from_the_environment(self):
        for env in ({"SAMPO_NON_CLAUDE_WORKER": "mycli"}, {"FM_NON_CLAUDE_WORKERS": "mycli"},
                    {"SAMPO_NON_CLAUDE_WORKERS": "mycli"}, {"SAMPO_CLAUDE_BENCH": "1", "FM_CLAUDE_BENCH": "1"}):
            self.refused("mycli", env=env)

    def seeded(self):
        """A bench child's home the way smithbench builds it for a repo, from this project's meta."""
        import subprocess
        from sampo import smithbench
        tmp = os.path.join(self.tmp, "seed")
        env, _ = smithbench.box(tmp, True)
        repo = os.path.join(tmp, "repo")
        os.makedirs(repo)
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=env)
        smithbench.seed_claude_meta(env["FOREMAN_HOME"], repo, c.find_project(self.repo))
        return env, repo

    def child_says(self, env, repo):
        import subprocess
        e = dict(env, FOREMAN_INHIBITED="1")
        sampo = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bin", "sampo")
        p = subprocess.run([sampo, "claude-bench", "--json"], cwd=repo, env=e, capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return json.loads(p.stdout)

    def test_user_declared_switch_and_names_reach_the_bench_child(self):
        self.fm("claude-bench", "on")
        self.fm("claude-bench", "worker", "mycli", env=self.env())
        env, repo = self.seeded()
        got = self.child_says(env, repo)
        self.assertTrue(got["claude_bench"])
        self.assertEqual(got["non_claude_workers"], ["mycli"])
        self.assertEqual(got["pinned"], self.meta()["non_claude_workers"], "the pin crosses as the user made it")

    def test_user_declared_child_of_a_closed_parent_stays_closed(self):
        self.fm("claude-bench", "worker", "mycli", env=self.env())
        env, repo = self.seeded()
        got = self.child_says(env, repo)
        self.assertFalse(got["claude_bench"])
        self.assertEqual(got["non_claude_workers"], ["mycli"])

    def test_user_declared_child_with_no_parent_project_gets_nothing(self):
        from sampo import smithbench
        tmp = os.path.join(self.tmp, "seed2")
        env, _ = smithbench.box(tmp, True)
        repo = os.path.join(tmp, "repo")
        os.makedirs(repo)
        smithbench.seed_claude_meta(env["FOREMAN_HOME"], repo, None)
        self.assertFalse(os.path.exists(os.path.join(env["FOREMAN_HOME"], "state", "projects")))

    def test_user_declared_child_ignores_the_environment_for_its_own_switch(self):
        env, repo = self.seeded()
        got = self.child_says(dict(env, SAMPO_CLAUDE_BENCH="1", SAMPO_NON_CLAUDE_WORKER="mycli"), repo)
        self.assertFalse(got["claude_bench"])
        self.assertEqual(got["non_claude_workers"], [])

    # T-5812 fix round: a declaration is trust in one pinned file, not in a name

    def test_user_declared_script_edited_after_declaring_is_refused(self):
        path = self.script("mycli")
        self.fm("claude-bench", "worker", "mycli", env=self.env())
        self.assertEqual(self.worker("mycli"), ["mycli", "hi"])
        with open(path, "a", encoding="utf-8") as f:
            f.write("claude -p \"$1\"\n")  # an agent edits the declared script to call claude
        msg = self.refused("mycli")
        self.assertIn("re-declares it with: ! sampo claude-bench worker mycli", msg)
        self.fm("claude-bench", "worker", "mycli", env=self.env())  # the user looked and re-declared: the new file counts
        self.assertEqual(self.worker("mycli"), ["mycli", "hi"])

    def test_user_declared_script_replaced_by_a_symlink_to_another_file_is_refused(self):
        path = self.script("mycli")
        self.fm("claude-bench", "worker", "mycli", env=self.env())
        other = self.script("elsewhere", "#!/bin/sh\necho other\n", where=os.path.join(self.tmp, "else"))
        os.unlink(path)
        os.symlink(other, path)
        self.refused("mycli")

    def test_user_declared_path_shim_with_the_declared_name_is_refused(self):
        self.fm("claude-bench", "worker", "mycli", env=self.env())
        shim = os.path.join(self.tmp, "shim")
        self.script("mycli", "#!/bin/sh\nexec claude -p \"$1\"\n", where=shim)
        self.refused("mycli", env=self.env(shim))  # the shim answers to the name first on PATH
        self.assertEqual(self.worker("mycli"), ["mycli", "hi"])  # the pinned file, found again, still counts

    def test_user_declared_spec_with_whitespace_and_no_prompt_is_not_pinnable(self):
        # no {prompt}: the process runs the raw spec as argv[0], which no command file answers to
        p = self.fm("claude-bench", "worker", "mycli --fast", env=self.env(), check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("nothing to pin", p.stdout + p.stderr)
        self.assertFalse(self.meta().get("non_claude_workers"))
        # a pin written by hand (a meta an agent edited) for such a spec still doesn't count
        pin = {"path": os.path.realpath(os.path.join(self.bin, "mycli")), "sha256": "0" * 64}
        c.update_meta(c.find_project(self.repo), non_claude_workers={"mycli --fast": pin})
        self.refused("mycli --fast")
        self.refused("claude -p", env=None)

    def test_user_declared_raw_spec_is_judged_when_the_template_has_no_prompt(self):
        from sampo import smith
        spec = "env claude"  # argv[0] is the whole raw spec, whatever the template says
        with mock.patch.object(smith, "declared_state", return_value=True):
            self.assertTrue(smith.claude_worker(spec, "mycli"))
            self.assertFalse(smith.claude_worker("mycli", "mycli"))

    def test_user_declared_old_name_only_meta_declares_nothing(self):
        c.update_meta(c.find_project(self.repo), non_claude_workers=["mycli"])  # the first-round form: a list of names
        self.refused("mycli")
        c.update_meta(c.find_project(self.repo), non_claude_workers={"mycli": "x", "other": {"path": 1}})
        self.refused("mycli")

    def test_user_declared_bench_with_p_seeds_that_projects_meta_not_the_cwds(self):
        import argparse
        import subprocess
        from helpers import git_repo
        from sampo import smithbench
        other = git_repo(self.tmp, name="other")
        self.fm("init", cwd=other)
        self.fm("claude-bench", "on", cwd=other)
        self.fm("claude-bench", "worker", "mycli", cwd=other, env=self.env())  # declared in B, not in A (the cwd)
        b = c.find_project(other)
        seen = {}

        def fake_bench(*a, **kw):
            seen["parent"] = kw.get("parent", "unset")
            return {}
        args = argparse.Namespace(project=b.slug, seeds=None, model=None, worker="mycli", json=True, seed_timeout=None,
                                  max_steps=None, variants=None)
        old = os.getcwd()
        os.chdir(self.repo)  # cwd is project A: switch off, nothing declared
        try:
            with mock.patch.object(smithbench, "bench", fake_bench), mock.patch.dict(os.environ, self.env()), \
                    mock.patch("builtins.print"):
                self.assertEqual(smithbench.cmd(args, []), 0)
        finally:
            os.chdir(old)
        self.assertEqual(seen["parent"].slug, b.slug)
        tmp = os.path.join(self.tmp, "seedb")
        env, _ = smithbench.box(tmp, True)
        repo = os.path.join(tmp, "repo")
        os.makedirs(repo)
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=env)
        smithbench.seed_claude_meta(env["FOREMAN_HOME"], repo, seen["parent"])
        got = self.child_says(env, repo)
        self.assertTrue(got["claude_bench"], "B's switch, not A's")
        self.assertEqual(got["non_claude_workers"], ["mycli"])

    def test_user_declared_corrupt_meta_is_a_closed_gate(self):
        p = c.find_project(self.repo)
        self.fm("claude-bench", "on")
        self.assertTrue(models.claude_on(p))
        with open(os.path.join(p.dir, "meta.json"), "w", encoding="utf-8") as f:
            f.write("{ not json")
        self.assertFalse(models.claude_on(p))
        self.assertEqual(models.declared_non_claude(p), {})
        with self.assertRaises(cli.UsageError):
            models.claude_gate(p, "a bench")
        with open(os.path.join(p.dir, "meta.json"), "w", encoding="utf-8") as f:
            f.write("[1, 2]")  # valid JSON, not a meta
        self.assertFalse(models.claude_on(p))
