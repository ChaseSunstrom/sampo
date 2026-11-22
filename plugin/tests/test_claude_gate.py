"""T-5584: no benchmark, eval or comparison that calls a Claude model runs unless the user has turned on claude_bench
(`sampo claude-bench on`, typed by the user: the guard refuses an agent's own). A refusal names that command. A local
profile and the oracle arm are never refused, and the nightly timer skips the paid jobs while the gate is closed."""
import ast
import glob
import hashlib
import json
import os
import shutil
import time
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
    """True for a list or tuple literal that starts `claude -p`: a headless model call. T-5706: a starred run of flags
    between them (`["claude", *READ_ONLY_FLAGS, "-p", ...]`) does not hide it."""
    if not isinstance(node, (ast.List, ast.Tuple)) or len(node.elts) < 2:
        return False
    plain = [e for e in node.elts[1:] if not isinstance(e, ast.Starred)]
    first, second = node.elts[0], (plain[0] if plain else node.elts[1])
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

    def test_a_known_worker_with_no_file_on_path_is_gated_not_run_by_name(self):
        """T-5849 round 2: a built-in name no file answers to has nothing to check, so it is Claude until declared."""
        self.assertGated("codex")
        self.assertGated("gemini")
        self.assertGated("codex", "codex exec {prompt}")

    def test_the_switch_on_lets_a_claude_worker_start(self):
        self.fm("claude-bench", "on")
        self.assertEqual(self.worker("claude")[0], "claude")
        self.assertEqual(self.worker("env claude")[0], "env claude")  # the spec is the command when no template


class WorkerFileCase(ForemanTestCase):
    """T-5849: the base of the tests that start a worker as a run does, with a bin folder first on PATH, a home of its own
    (aider reads ~/.aider.conf.yml) and no AIDER_ variable from the machine."""

    def setUp(self):
        super().setUp()
        self.fm("init")
        self.bin = os.path.join(self.tmp, "bin")
        self.homedir = os.path.join(self.tmp, "home")
        os.makedirs(self.homedir)

    def script(self, name, body="#!/bin/sh\necho ok\n", where=None):
        d = where or self.bin
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        os.chmod(path, 0o755)
        return path

    def run_worker(self, spec, tpl=None, env=None, which=False, cwd=None):
        from sampo import smith
        runners = smith.all_runners()
        if tpl is not None:
            runners = {**runners, spec: {**runners.get(spec, {}), "work": tpl}}
        keep = {k: v for k, v in os.environ.items() if not k.startswith(("AIDER_", "ANTHROPIC_", "OPENROUTER_", "CODEX_"))}
        keep.update({"FOREMAN_PROJECT": "", "HOME": self.homedir,
                     "PATH": os.pathsep.join([self.bin, os.environ.get("PATH", "")]), **(env or {})})
        old = os.getcwd()
        os.chdir(self.repo)
        try:
            with mock.patch.object(smith, "all_runners", return_value=runners), \
                    mock.patch.dict(os.environ, keep, clear=True):
                return smith.worker_argv(spec, "hi", which=which, cwd=cwd)
        finally:
            os.chdir(old)

    def gated(self, spec, tpl=None, env=None):
        from sampo import smith
        with self.assertRaises(smith.CodeError) as ctx:
            self.run_worker(spec, tpl, env)
        self.assertIn(GATE, str(ctx.exception), f"{spec!r} {tpl!r} {env!r}")
        return str(ctx.exception)

    def declared_script(self, name, body="#!/bin/sh\necho ok\n"):
        """A file the agent could write, which the user declared (the way a built-in with a writable file runs)."""
        path = self.script(name, body)
        self.assertEqual(self.declare(name).returncode, 0)
        return path

    def declare(self, name):
        """The user's own `sampo claude-bench worker NAME`, typed with the bin folder first on PATH."""
        return self.fm("claude-bench", "worker", name, check=False,
                       env={"PATH": os.pathsep.join([self.bin, os.environ.get("PATH", "")])})


class BuiltinWorkerFile(WorkerFileCase):
    """T-5849: a built-in non-Claude name (codex, gemini, aider ...) counts as non-Claude only when the file argv[0]
    resolves to is not the Claude CLI: a PATH shim answering to the name that runs claude is judged as Claude, and aider
    is Claude when AIDER_MODEL, --model or an aider config names a Claude model."""

    def test_builtin_shim_script_that_runs_claude_is_judged_claude(self):
        self.script("codex", "#!/bin/sh\nexec claude -p \"$1\"\n")
        self.gated("codex")
        self.script("gemini", "#!/usr/bin/env bash\nset -e\necho start\nenv claude -p \"$@\"\n")
        self.gated("gemini")
        self.script("qwen", "claude -p \"$1\"\n")  # no shebang: a small text file is read as a script all the same
        self.gated("qwen", "qwen {prompt}")

    def test_builtin_shim_through_a_shell_string_or_a_helper_is_judged_claude(self):
        self.script("codex", "#!/bin/sh\nsh -c 'claude -p \"$1\"' x \"$1\"\n")
        self.gated("codex")
        real = self.script("claude", "#!/bin/sh\n", where=os.path.join(self.tmp, "real"))
        os.symlink(real, os.path.join(self.bin, "helper"))
        self.script("gemini", "#!/bin/sh\nexec helper -p \"$1\"\n")  # a name that resolves to claude, not spelled claude
        self.gated("gemini")

    def test_builtin_symlink_to_a_claude_binary_is_judged_claude(self):
        real = self.script("claude", "#!/bin/sh\n", where=os.path.join(self.tmp, "real"))
        os.makedirs(self.bin)
        os.symlink(real, os.path.join(self.bin, "codex"))
        self.gated("codex")

    def test_builtin_real_non_claude_stand_in_stays_non_claude(self):
        self.script("codex", "#!/bin/sh\n# this one never calls claude, whatever its comment says\necho \"$@\"\n")
        self.assertEqual(self.declare("codex").returncode, 0)  # a file the agent could write is the user's to vouch for
        self.assertEqual(os.path.basename(self.run_worker("codex")[0]), "codex")
        self.script("gemini", "#!/usr/bin/env python3\nprint('ok')\n")
        self.assertEqual(self.declare("gemini").returncode, 0)
        self.assertEqual(os.path.basename(self.run_worker("gemini")[0]), "gemini")
        self.gated("aider", "aider run {prompt}")  # nothing on PATH answers to it: Claude, never run by name (T-5849 round 2)

    def test_builtin_binary_stays_non_claude(self):
        path = self.script("codex", "")
        with open(path, "wb") as f:
            f.write(b"\x7fELF\x02\x01\x01" + b"\0" * 64 + b"claude -p" * 50)  # a binary's strings are not its command
        self.assertEqual(self.declare("codex").returncode, 0)
        self.assertEqual(os.path.basename(self.run_worker("codex")[0]), "codex")

    def test_builtin_file_that_cannot_be_read_is_judged_claude(self):
        path = self.script("codex")
        os.chmod(path, 0o111)  # runnable, not readable
        if os.access(path, os.R_OK):
            self.skipTest("this user reads any file")
        self.gated("codex")

    def test_builtin_aider_model_env_naming_claude_is_judged_claude(self):
        for model in ("claude-sonnet", "anthropic/claude-3-5-sonnet", "sonnet", "OPUS", "openrouter/anthropic/x", "haiku"):
            self.gated("aider", env={"AIDER_MODEL": model})
        self.gated("aider", env={"AIDER_WEAK_MODEL": "claude-3-haiku"})
        self.gated("aider", env={"AIDER_SONNET": "true"})

    def test_builtin_aider_model_flag_naming_claude_is_judged_claude(self):
        self.gated("aider", "aider --model anthropic/claude-3-opus --message {prompt}")
        self.gated("aider", "aider --model=sonnet --message {prompt}")
        self.gated("aider", "aider --opus --message {prompt}")

    def test_builtin_aider_config_naming_claude_is_judged_claude(self):
        conf = os.path.join(self.repo, ".aider.conf.yml")
        for text in ("model: claude-3-5-sonnet-20241022\n", "# mine\nmodel: \"anthropic/claude-3\"\n",
                     "weak-model: haiku\n", "sonnet: true\n", "alias:\n  - fast:claude-3-haiku\n"):
            with open(conf, "w", encoding="utf-8") as f:
                f.write(text)
            self.gated("aider")
        os.unlink(conf)
        with open(os.path.join(self.homedir, ".aider.conf.yml"), "w", encoding="utf-8") as f:
            f.write("model: sonnet\n")
        self.gated("aider")

    def test_builtin_aider_with_another_model_stays_non_claude(self):
        self.declared_script("aider")
        self.declared_script("codex")
        base = lambda argv: os.path.basename(argv[0])
        self.assertEqual(base(self.run_worker("aider", env={"AIDER_MODEL": "gpt-4o"})), "aider")
        with open(os.path.join(self.repo, ".aider.conf.yml"), "w", encoding="utf-8") as f:
            f.write("# claude was good once\nmodel: gpt-4o\nsonnet: false\n")
        self.assertEqual(base(self.run_worker("aider")), "aider")
        self.assertEqual(self.run_worker("aider", "aider --model gpt-4o --message {prompt}")[1:3], ["--model", "gpt-4o"])
        self.assertEqual(base(self.run_worker("codex", env={"AIDER_MODEL": "sonnet"})), "codex")  # not aider's variable here

    def test_builtin_switch_on_lets_a_claude_shim_start(self):
        self.script("codex", "#!/bin/sh\nexec claude -p \"$1\"\n")
        self.fm("claude-bench", "on")
        self.assertEqual(os.path.basename(self.run_worker("codex")[0]), "codex")


class BuiltinTrust(WorkerFileCase):
    """T-5849 round 1: a built-in name is non-Claude because of the file it resolves to being one the agent could not have
    written (or because the user declared it), never because a script's words look harmless. The words only add a detector."""

    DECLARE = "declare it: ! sampo claude-bench worker codex"

    def sealed(self, *dirs):
        """Treats these folders (and what is in them) as ones this uid can't write, on top of what really isn't."""
        from sampo import smith
        real, orig = [os.path.realpath(d) for d in dirs], smith._sealed_path
        return mock.patch.object(smith, "_sealed_path",
                                 lambda p: orig(p) or any(p == d or p.startswith(d + os.sep) for d in real))

    def trusted_dir(self):
        return os.path.join(self.tmp, "root-owned")

    def passes(self, spec="codex", tpl=None, env=None):
        return os.path.basename(self.run_worker(spec, tpl, env)[0])

    def test_trust_a_writable_path_shim_is_claude_whatever_its_words_and_says_how_to_declare(self):
        for body in ("#!/bin/sh\necho ok\n",  # a plain stand-in: it is the agent's file all the same
                     "#!/bin/sh\nc=laude; ${0:+c}$c -p x\n", "#!/bin/sh\nexec c${1:-l}aude\n",
                     "#!/bin/sh\nx=cl; y=aude; $x$y -p\n", "#!/bin/sh\n$(printf 'cl%s' aude) -p\n",
                     "#!/bin/sh\nset -- c laude; exec \"$1$2\"\n", "#!/bin/sh\neval \"$(echo Y2xhdWRl | base64 -d)\"\n",
                     "#!/bin/sh\necho ok\n" + "#" * 5000 + "\n", "#!/bin/sh\necho ok\n\0" + "\0" * 9000 + "exec claude\n"):
            self.script("codex", body)
            self.assertIn(self.DECLARE, self.gated("codex"), body[:40])
        self.script("codex", "#!/usr/bin/env node\nconsole.log(1)\n")  # a node shim one level down
        self.gated("codex")

    def test_trust_a_sealed_file_stands_but_an_interpreter_shell_or_claude_does_not(self):
        sealed = self.trusted_dir()
        env = {"PATH": os.pathsep.join([sealed, os.environ.get("PATH", "")])}

        def binary(name):
            os.makedirs(sealed, exist_ok=True)
            path = os.path.join(sealed, name)
            with open(path, "wb") as f:
                f.write(b"\x7fELF\x02\x01\x01" + b"\0" * 64)
            os.chmod(path, 0o755)
            return path

        def link(target, name):
            path = os.path.join(sealed, name)
            if os.path.lexists(path):
                os.unlink(path)
            os.symlink(target, path)

        with self.sealed(sealed):
            binary("codex")
            self.assertEqual(self.passes(env=env), "codex")  # a binary the agent can't write
            os.unlink(os.path.join(sealed, "codex"))
            self.script("codex", "#!/bin/sh\necho ok\n", where=sealed)
            self.gated("codex", env=env)  # a script whose interpreter is a shell
            for name in ("sh", "bash", "env", "node", "python3", "perl", "xargs", "timeout"):
                link(binary(name), "codex")
                self.assertIn(self.DECLARE, self.gated("codex", env=env), name)  # named as an interpreter, shell or wrapper
            os.unlink(os.path.join(sealed, "codex"))
            binary("gemini")
            link(os.path.join(sealed, "gemini"), "claude")  # the file `claude` resolves to is the file gemini is
            self.gated("gemini", env=env)
            link(os.path.join(sealed, "claude"), "codex")
            self.gated("codex", env=env)
            os.unlink(os.path.join(sealed, "claude"))
            self.assertEqual(self.passes("gemini", env=env), "gemini")  # with no claude about, it is only a binary

    def test_trust_a_script_interpreter_and_its_env_target_must_pass_too(self):
        sealed = self.trusted_dir()
        elf = b"\x7fELF\x02\x01\x01" + b"\0" * 64
        env = {"PATH": os.pathsep.join([sealed, os.environ.get("PATH", "")])}
        with self.sealed(sealed):
            self.script("codex", "#!/usr/bin/env node\nconsole.log(1)\n", where=sealed)
            with open(self.script("node", "", where=self.bin), "wb") as f:  # node found first on a folder the agent can write
                f.write(elf)
            self.gated("codex", env={"PATH": os.pathsep.join([self.bin, sealed, os.environ.get("PATH", "")])})
            with open(self.script("node", "", where=sealed), "wb") as f:  # node in a folder it can't: that one may stand
                f.write(elf)
            self.assertEqual(self.passes(env=env), "codex")
            self.script("node", "#!/bin/sh\nexec claude \"$@\"\n", where=sealed)  # node, a script one level down
            self.gated("codex", env=env)
            self.script("codex", "#!/usr/bin/env python3\nprint(1)\n", where=sealed)
            with open(self.script("python3", "", where=sealed), "wb") as f:
                f.write(elf)
            self.assertEqual(self.passes(env=env), "codex")
            self.script("codex", "#!/usr/bin/env -S python3\nprint(1)\n", where=sealed)
            self.assertEqual(self.passes(env=env), "codex")
            self.script("codex", "#!/usr/bin/env -S python3 -u\nprint(1)\n", where=sealed)  # words the check would drop: declare
            self.gated("codex", env=env)
            self.script("codex", "#!/usr/bin/env -i python3\nprint(1)\n", where=sealed)
            self.gated("codex", env=env)

    def test_trust_a_declaration_still_stands_for_a_writable_built_in_and_is_judged_on_its_words(self):
        self.script("codex", "#!/bin/sh\necho ok\n")
        self.assertEqual(self.declare("codex").returncode, 0)
        self.assertEqual(self.passes(), "codex")
        self.script("codex", "#!/bin/sh\necho changed\n")  # edited after declaring: Claude until the user re-declares
        self.assertIn(self.DECLARE, self.gated("codex"))
        self.script("codex", "#!/bin/sh\nexec claude -p \"$1\"\n")
        self.assertEqual(self.declare("codex").returncode, 0)
        self.gated("codex")  # declared, and its words still run claude

    def test_size_a_built_in_too_big_to_hash_or_to_read_is_claude_never_judged_by_its_name(self):
        from sampo import smith
        sealed = self.trusted_dir()
        with self.sealed(sealed):
            self.script("codex", "#!/usr/bin/env python3\n" + "x = 1\n" * 40, where=sealed)
            with mock.patch.object(smith, "PIN_MAX", 50):
                self.assertIn(self.DECLARE, self.gated("codex", env={"PATH": sealed}))
            with mock.patch.object(smith, "WORKER_SCRIPT_MAX", 100):
                self.gated("codex", env={"PATH": sealed})
            self.script("codex", "#!/bin/sh\n\0\0 plain\n", where=sealed)
            self.gated("codex", env={"PATH": sealed})
        self.script("codex", "#!/bin/sh\necho ok\n")
        self.assertEqual(self.declare("codex").returncode, 0)
        with mock.patch.object(smith, "PIN_MAX", 5):  # declared, then too big to hash: the pin can't hold
            self.gated("codex")

    def test_list_only_the_built_ins_that_get_a_config_check_are_trusted_by_name(self):
        from sampo import smith
        self.assertEqual(smith.NON_CLAUDE_WORKERS, {"codex", "gemini", "aider"})
        for name in ("opencode", "goose", "amp", "cursor-agent", "qwen"):
            self.gated(name, f"{name} {{prompt}}")

    def test_config_codex_and_gemini_files_that_point_at_claude_or_openrouter_make_them_claude(self):
        self.script("codex", "#!/bin/sh\necho codex\n")
        self.script("gemini", "#!/bin/sh\necho gemini\n")
        for name in ("codex", "gemini"):
            self.assertEqual(self.declare(name).returncode, 0)
        conf = os.path.join(self.homedir, ".codex", "config.toml")
        os.makedirs(os.path.dirname(conf))
        with open(conf, "w", encoding="utf-8") as f:
            f.write('model = "gpt-5"\n')
        self.assertEqual(self.passes(), "codex")
        for text in ('model = "claude-sonnet-4"\n', 'model_provider = "openrouter"\n',
                     '[model_providers.x]\nbase_url = "https://openrouter.ai/api"\n',
                     'model = "Opus"\n', '[model_providers.a]\nname = "Anthropic"\n', 'model = "haiku"\n'):
            with open(conf, "w", encoding="utf-8") as f:
                f.write(text)
            self.gated("codex")
        os.unlink(conf)
        other = os.path.join(self.tmp, "chome")
        os.makedirs(other)
        with open(os.path.join(other, "config.toml"), "w", encoding="utf-8") as f:
            f.write('model = "claude-3"\n')
        self.gated("codex", env={"CODEX_HOME": other})  # $CODEX_HOME names the folder
        self.assertEqual(self.passes(), "codex")  # no config at all: nothing says Claude
        self.gated("codex", "codex -c model_provider=openrouter exec {prompt}")
        self.gated("codex", "codex --model claude-3 exec {prompt}")
        self.gated("codex", "codex --config=model=sonnet exec {prompt}")
        self.assertEqual(self.run_worker("codex", "codex -c model=gpt-5 exec {prompt}")[0].rsplit("/", 1)[-1], "codex")
        gem = os.path.join(self.homedir, ".gemini", "settings.json")
        os.makedirs(os.path.dirname(gem))
        with open(gem, "w", encoding="utf-8") as f:
            f.write('{"model": "gemini-2.5-pro"}')
        self.assertEqual(self.passes("gemini"), "gemini")
        with open(gem, "w", encoding="utf-8") as f:
            f.write('{"baseUrl": "https://openrouter.ai/api", "model": "anthropic/claude-3"}')
        self.gated("gemini")

    def test_config_a_codex_or_gemini_config_that_cannot_be_read_is_claude(self):
        self.script("codex", "#!/bin/sh\necho codex\n")
        self.assertEqual(self.declare("codex").returncode, 0)
        conf = os.path.join(self.homedir, ".codex", "config.toml")
        os.makedirs(os.path.dirname(conf))
        with open(conf, "w", encoding="utf-8") as f:
            f.write('model = "gpt-5"\n')
        os.chmod(conf, 0)
        if os.access(conf, os.R_OK):
            self.skipTest("this user reads any file")
        self.gated("codex")

    def test_aider_a_model_key_in_any_form_is_judged_and_an_unparseable_one_is_claude(self):
        self.declared_script("aider")
        conf = os.path.join(self.repo, ".aider.conf.yml")
        for text in ("  model: claude-3\n", "\tmodel: sonnet\n", '"model": "anthropic/claude-3"\n', "'model': 'opus'\n",
                     "{model: sonnet, auto-commits: false}\n", '{"weak-model": "haiku"}\n', "settings:\n  model: claude-3-5\n",
                     "model:\n", "model: |\n  gpt-4o\n", "model: &m gpt-4o\n", "model: *m\n", "model: >\n  x\n", "model: !!str gpt\n",
                     "model: [gpt-4o]\n", "alias: [fast:claude-3]\n", "alias:\n  - 'fast:claude-3-haiku'\n",
                     "editor-model:\n", "  sonnet: true\n"):
            with open(conf, "w", encoding="utf-8") as f:
                f.write(text)
            self.gated("aider")
        for text in ("model: gpt-4o\n", "  model: 'gpt-4o'\n", '"model": "o3"\n', "{model: gpt-4o, auto-commits: false}\n",
                     "# model: claude\nmodel: gpt-4o\n", "alias:\n  - 'fast:gpt-4o-mini'\n"):
            with open(conf, "w", encoding="utf-8") as f:
                f.write(text)
            self.assertEqual(self.passes("aider"), "aider", text)

    def test_aider_with_no_explicit_model_an_anthropic_or_openrouter_key_makes_it_claude(self):
        self.declared_script("aider")
        for key in ("ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            self.gated("aider", env={key: "sk-x"})
            self.assertEqual(self.passes("aider", env={key: "sk-x", "AIDER_MODEL": "gpt-4o"}), "aider")
            self.assertEqual(self.run_worker("aider", "aider --model gpt-4o --message {prompt}", env={key: "sk-x"})[1], "--model")
            self.gated("aider", "aider --model sonnet --message {prompt}", env={key: "sk-x"})
        with open(os.path.join(self.repo, ".aider.conf.yml"), "w", encoding="utf-8") as f:
            f.write("model: gpt-4o\n")
        self.assertEqual(self.passes("aider", env={"ANTHROPIC_API_KEY": "sk-x"}), "aider")
        os.unlink(os.path.join(self.repo, ".aider.conf.yml"))
        with open(os.path.join(self.repo, ".env"), "w", encoding="utf-8") as f:
            f.write("export OPENROUTER_API_KEY=sk-x\n")
        # round 4 (item 6): a repo .env key with no explicit model is Claude's, for a declared aider as for a built-in
        self.gated("aider", env={})
        self.assertEqual(self.passes("aider", env={"AIDER_MODEL": "gpt-4o"}), "aider")
        with open(os.path.join(self.repo, ".env"), "w", encoding="utf-8") as f:
            f.write("export OPENROUTER_BASE_URL=http://x\n")
        self.gated("aider", env={})  # a route to a base URL in the worker folder's .env still counts

    def test_aider_config_is_searched_from_the_workers_cwd_not_this_process(self):
        self.declared_script("aider")
        elsewhere = os.path.join(self.tmp, "elsewhere")
        os.makedirs(elsewhere)
        with open(os.path.join(elsewhere, ".aider.conf.yml"), "w", encoding="utf-8") as f:
            f.write("model: claude-3\n")
        self.assertEqual(self.passes("aider"), "aider")  # this process stands in the repo: no config there
        self.gated_cwd("aider", elsewhere)
        with open(os.path.join(self.repo, ".aider.conf.yml"), "w", encoding="utf-8") as f:
            f.write("model: claude-3\n")
        self.assertEqual(os.path.basename(self.run_worker("aider", cwd=os.path.join(self.tmp, "clean"))[0]), "aider")

    def gated_cwd(self, spec, cwd):
        from sampo import smith
        with self.assertRaises(smith.CodeError) as ctx:
            self.run_worker(spec, cwd=cwd)
        self.assertIn(GATE, str(ctx.exception))

    def test_env_a_worker_gets_no_shell_or_interpreter_startup_file(self):
        from sampo import smith
        names = ("BASH_ENV", "ENV", "LD_PRELOAD", "LD_AUDIT", "NODE_OPTIONS", "PERL5OPT", "RUBYOPT", "PYTHONSTARTUP")
        with mock.patch.dict(os.environ, {n: "/tmp/x" for n in names}):
            got = smith.worker_env(start=True)
        self.assertEqual([n for n in names if n in got], [])
        self.assertIn("PATH", got)

    def test_env_the_worker_shell_reads_no_profile_or_rc(self):
        from sampo import smith
        if not os.path.isdir("/proc/self"):
            self.skipTest("no /proc")
        code, out = smith.run_bounded("tr '\\0' ' ' < /proc/$$/cmdline", self.repo, 20, dict(os.environ))
        self.assertEqual(code, 0)
        self.assertIn("--noprofile --norc", out)


class RoundTwo(WorkerFileCase):
    """T-5849 round 2: a built-in name is trusted only for the built-in's own file (a link to /usr/bin/setsid is not codex),
    a worker never gets an exported bash function, a loader variable or a user site, a model hidden behind an escape, a tag or
    an env-file is Claude, a trusted script runs under the interpreter that was checked, the sealed copy is hashed once sealed,
    a built-in with no file is not run by name."""

    DECLARE = "declare it: ! sampo claude-bench worker codex"
    ELF = b"\x7fELF\x02\x01\x01" + b"\0" * 64

    def sealed(self, *dirs):
        from sampo import smith
        real, orig = [os.path.realpath(d) for d in dirs], smith._sealed_path
        return mock.patch.object(smith, "_sealed_path",
                                 lambda p: orig(p) or any(p == d or p.startswith(d + os.sep) for d in real))

    def sealed_dir(self):
        return os.path.join(self.tmp, "root-owned")

    def binary(self, name, where=None):
        d = where or self.sealed_dir()
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, name)
        with open(path, "wb") as f:
            f.write(self.ELF)
        os.chmod(path, 0o755)
        return path

    def link(self, target, name, where=None):
        d = where or self.sealed_dir()
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, name)
        if os.path.lexists(path):
            os.unlink(path)
        os.symlink(target, path)
        return path

    def path_env(self):
        return {"PATH": os.pathsep.join([self.sealed_dir(), os.environ.get("PATH", "")])}

    def passes(self, spec="codex", tpl=None, env=None):
        return os.path.basename(self.run_worker(spec, tpl, env if env is not None else self.path_env())[0])

    # 1. the file must be the built-in
    def test_item1_a_link_to_another_sealed_binary_is_not_the_built_in(self):
        with self.sealed(self.sealed_dir()):
            for other in ("setsid", "git", "exec", "tool"):
                self.link(self.binary(other), "codex")
                self.assertIn(self.DECLARE, self.gated("codex", env=self.path_env()), other)
            real = os.path.realpath(shutil.which("git") or shutil.which("ls"))  # a real system binary, linked as codex
            os.makedirs(self.bin, exist_ok=True)
            os.symlink(real, os.path.join(self.bin, "codex"))
            self.assertIn(self.DECLARE, self.gated("codex", env={"PATH": self.bin}))

    def test_item1_the_vendor_build_and_the_node_package_entry_pass(self):
        sealed = self.sealed_dir()
        with self.sealed(sealed):
            self.binary("node")
            self.link(self.binary("codex-x86_64-unknown-linux-musl"), "codex")
            self.assertEqual(self.passes(), "codex-x86_64-unknown-linux-musl")
            pkg = os.path.join(sealed, "lib", "node_modules", "@openai", "codex")
            for rel, spec, ok in (("bin/codex.js", "codex", True), ("dist/index.js", "codex", False),
                                  ("dist/other.js", "codex", False)):
                entry = self.script(os.path.basename(rel), "#!/usr/bin/env node\nconsole.log(1)\n",
                                    where=os.path.join(pkg, os.path.dirname(rel)))
                self.link(entry, "codex")
                if ok:
                    self.assertEqual(self.passes(), os.path.basename(rel), rel)
                else:
                    self.assertIn(self.DECLARE, self.gated("codex", env=self.path_env()), rel)
            lone = self.script("index.js", "#!/usr/bin/env node\nconsole.log(1)\n", where=os.path.join(sealed, "lib"))
            self.link(lone, "codex")  # an index.js outside the built-in's own package folder
            self.assertIn(self.DECLARE, self.gated("codex", env=self.path_env()))
            gem = self.script("index.js", "#!/usr/bin/env node\nconsole.log(1)\n",
                              where=os.path.join(sealed, "lib", "node_modules", "@google", "gemini-cli", "dist"))
            self.link(gem, "gemini")
            self.assertEqual(self.passes("gemini"), "index.js")
            self.link(entry, "gemini")  # codex's file answering to gemini's name
            self.gated("gemini", env=self.path_env())

    # 2 and 5. the environment
    def test_item2_5_a_worker_gets_no_exported_function_loader_variable_or_user_site(self):
        from sampo import smith
        import fmguard
        names = ("BASH_FUNC_exec%%", "BASH_FUNC_x%%", "SHELLOPTS", "BASHOPTS", "CDPATH", "GLOBIGNORE", "PYTHONPATH", "PYTHONHOME",
                 "PYTHONUSERBASE", "PYTHONSTARTUP", "NODE_PATH", "PERL5LIB", "PERLLIB", "RUBYLIB", "GCONV_PATH", "ZDOTDIR",
                 "LD_LIBRARY_PATH", "LD_PRELOAD", "LD_AUDIT", "DYLD_INSERT_LIBRARIES", "DYLD_LIBRARY_PATH")
        with mock.patch.dict(os.environ, {**{n: "x" for n in names}, "PYTHONNOUSERSITE": "", "KEEP_ME": "1"}):
            got = smith.worker_env(start=True)
        self.assertEqual([n for n in names if n in got], [])
        self.assertEqual(got.get("PYTHONNOUSERSITE"), "1")
        self.assertEqual(got.get("KEEP_ME"), "1")
        self.assertLessEqual(set(fmguard._PY_PATH_VARS), smith.WORKER_LOADS)

    def test_item2_an_exported_exec_function_does_not_reach_the_worker_shell(self):
        from sampo import smith
        if not os.path.isdir("/proc/self"):
            self.skipTest("no /proc")
        env = {**os.environ, "BASH_FUNC_exec%%": "() { echo overridden; }"}
        with mock.patch.dict(os.environ, env):
            got = smith.worker_env(start=True)
        code, out = smith.run_bounded("exec echo real", self.repo, 20, got)
        self.assertEqual((code, out.strip()), (0, "real"))

    # 3. escapes, tags, non-ASCII
    def test_item3_an_escape_a_tag_or_non_ascii_in_an_aider_model_is_claude(self):
        self.declared_script("aider")
        conf = os.path.join(self.repo, ".aider.conf.yml")
        for text in ('model: "\\x61nthropic/\\x63laude-3"\n', "model: '\\u0073onnet'\n", 'model: "ａnthropic/x"\n',
                     "weak-model: !!str gpt-4o\n", "editor-model: &x gpt-4o\n", "model: *x\n", 'model: "gpt-4o"  \\\n',
                     '"m\\x6fdel": gpt-4o\n', "alias:\n  - fast:\\x63laude\n", "model: gpt-4\u00e9\n"):
            with open(conf, "w", encoding="utf-8") as f:
                f.write(text)
            self.gated("aider")
        with open(conf, "w", encoding="utf-8") as f:
            f.write("model: gpt-4o\n")
        self.assertEqual(self.passes("aider", env={}), "aider")
        self.gated("aider", env={"AIDER_MODEL": "\\x63laude"})
        self.gated("aider", env={"AIDER_MODEL": "gpt-\u00e9"})

    def test_item3_an_escape_in_a_codex_or_gemini_config_or_flag_is_read_as_the_word_it_spells(self):
        self.declared_script("codex")
        self.declared_script("gemini")
        conf = os.path.join(self.homedir, ".codex", "config.toml")
        os.makedirs(os.path.dirname(conf))
        for text in ('model = "\\u0061nthropic/claude"\n', 'model = "\\u0073onnet"\n', 'model = "ａnthropic"\n',
                     '[model_providers.x]\nbase_url = "https://\\u006fpenrouter.ai"\n', 'model = "\n'):
            with open(conf, "w", encoding="utf-8") as f:
                f.write(text)
            self.gated("codex")
        with open(conf, "w", encoding="utf-8") as f:
            f.write('model = "gpt-5"\n[tui]\nname = "\u00e9"\n')
        self.assertEqual(self.passes("codex", env={}), "codex")
        gem = os.path.join(self.homedir, ".gemini", "settings.json")
        os.makedirs(os.path.dirname(gem))
        for text in ('{"model": "\\u0061nthropic/claude"}', '{"mo\\u0064el": "\\u006fpus"}', '{"model": "\u0430nthropic"}',
                     '{"model": '):
            with open(gem, "w", encoding="utf-8") as f:
                f.write(text)
            self.gated("gemini")
        self.gated("codex", "codex -c 'model=\"\\x61nthropic\"' exec {prompt}")
        self.gated("codex", "codex --model=\u0430nthropic exec {prompt}")

    # 4. aider's env-file
    def test_item4_an_aider_env_file_is_not_followed_so_it_is_claude(self):
        self.declared_script("aider")
        conf = os.path.join(self.repo, ".aider.conf.yml")
        with open(conf, "w", encoding="utf-8") as f:
            f.write("env-file: other.env\n")
        self.gated("aider")
        os.unlink(conf)
        self.gated("aider", env={"AIDER_ENV_FILE": "/somewhere/else.env"})
        self.gated("aider", "aider --env-file other.env --message {prompt}")
        self.gated("aider", "aider --env-file=other.env --message {prompt}")
        self.assertEqual(self.passes("aider", env={}), "aider")
        env_file = os.path.join(self.repo, ".env")
        for line in ("AIDER_MODEL=gpt-4o", "AIDER_WEAK_MODEL=gpt-4o-mini", "AIDER_EDITOR_MODEL=o3",
                     "export ANTHROPIC_BASE_URL=http://x", "AIDER_ENV_FILE=y"):
            with open(env_file, "w", encoding="utf-8") as f:
                f.write(line + "\n")
            self.gated("aider")
        with open(env_file, "w", encoding="utf-8") as f:
            f.write("FOO=1\n# AIDER_MODEL=sonnet\n")
        self.assertEqual(self.passes("aider", env={}), "aider")

    # 6. the interpreter that was checked is the one that runs
    def test_item6_a_trusted_script_runs_under_the_interpreter_realpath_that_was_checked(self):
        from sampo import smith
        sealed = self.sealed_dir()
        with self.sealed(sealed):
            node = self.binary("node")
            self.script("codex", "#!/usr/bin/env node\nconsole.log(1)\n", where=sealed)
            argv = self.run_worker("codex", env=self.path_env())
            self.assertEqual(argv.pin.interp, os.path.realpath(node))
            with smith.checked_command(argv) as (cmd, fds):
                # round 3: the realpath is what is trusted and run, under the shebang path as its name
                found = argv.pin.interp_argv0
                self.assertTrue(cmd.startswith(f"exec -a {found} {os.path.realpath(node)} /proc/self/fd/"), cmd)
                self.assertNotIn(" node ", cmd)
                self.assertEqual(len(fds), 1)
            plain = self.binary("codex-aarch64-unknown-linux-musl")
            self.link(plain, "codex")
            argv = self.run_worker("codex", env=self.path_env())
            self.assertIsNone(argv.pin.interp)

    # 7 and 8. the sealed copy and the held descriptor
    def test_item7_the_copy_is_hashed_only_after_it_is_sealed(self):
        from sampo import smith
        if not os.path.isdir("/proc/self/fd"):
            self.skipTest("no /proc/self/fd here")
        import fcntl
        path = self.script("mycli", "#!/bin/sh\necho hi\n")
        seen = []
        real = smith._hash_fd

        def spy(fd):
            seen.append(fcntl.fcntl(fd, 1034))  # F_GET_SEALS
            return real(fd)
        fd = os.open(path, os.O_RDONLY)
        try:
            with mock.patch.object(smith, "_hash_fd", spy):
                got = smith._sealed_copy(fd)
        finally:
            os.close(fd)
        if got is None:
            self.skipTest("no memfd_create here")
        try:
            self.assertEqual(seen, [15])  # SEAL | SHRINK | GROW | WRITE, and one hash: the sealed bytes'
            self.assertEqual(got[1], real(got[0]))
        finally:
            os.close(got[0])

    def test_item8_with_no_sealed_copy_the_held_descriptor_is_what_runs_never_the_name(self):
        from sampo import smith
        if not smith.FD_EXEC:
            self.skipTest("no /proc/self/fd here")
        self.script("mycli", "#!/bin/sh\necho original \"$1\"\n")
        self.fm("claude-bench", "worker", "mycli", env={"PATH": os.pathsep.join([self.bin, os.environ.get("PATH", "")])})
        argv = self.run_worker("mycli")
        with mock.patch.object(smith, "_sealed_copy", return_value=None):
            # round 3: a script this user can write, with no sealed copy to run, is refused, not run from the held fd
            with self.assertRaises(smith.CodeError) as ctx:
                with smith.checked_command(argv):
                    self.fail("a writable script with no sealed copy must not run")
            self.assertIn("sealed copy", str(ctx.exception))
        self.binary("mybin", where=self.bin)
        self.fm("claude-bench", "worker", "mybin", env={"PATH": os.pathsep.join([self.bin, os.environ.get("PATH", "")])})
        argv = self.run_worker("mybin")
        with mock.patch.object(smith, "_sealed_copy", return_value=None):
            with self.assertRaises(smith.CodeError) as ctx:  # round 4: a writable binary too is refused, not run from its inode
                with smith.checked_command(argv):
                    self.fail("a writable binary with no sealed copy must not run")
            self.assertIn("sealed copy", str(ctx.exception))

    # 9. a built-in with no file
    def test_item9_a_built_in_with_no_file_is_claude_and_is_never_run_by_name(self):
        from sampo import smith
        self.assertIn(self.DECLARE, self.gated("codex", env={"PATH": self.tmp}))
        with self.assertRaises(smith.CodeError) as ctx:
            self.run_worker("codex", env={"PATH": self.tmp}, which=True)
        self.assertIn("isn't on PATH", str(ctx.exception))
        self.fm("claude-bench", "on")
        argv = self.run_worker("codex", env={"PATH": self.tmp})  # the switch on: Claude may start, nothing is pinned
        self.assertEqual(argv[0], "codex")
        self.assertIsNone(getattr(argv, "pin", None))


class RoundThree(WorkerFileCase):
    """T-5849 round 3: an aider model-settings file or a short flag is a route to Claude, a declared script runs the interpreter
    that was pinned (found where the shebang names it, run under that name so a venv still finds its pyvenv.cfg), a copy
    that can't be made refuses a writable script, a codex or gemini config is judged on the keys that name a model, provider
    or base URL and not on a trusted-folder path, a worker gets no PYTHON* variable, and only a package's own entry file is
    the built-in."""

    DECLARE, ELF = RoundTwo.DECLARE, RoundTwo.ELF
    sealed, sealed_dir, binary, link, path_env, passes = (RoundTwo.sealed, RoundTwo.sealed_dir, RoundTwo.binary, RoundTwo.link,
                                                          RoundTwo.path_env, RoundTwo.passes)

    def write(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def gated_in(self, spec, cwd):
        from sampo import smith
        with self.assertRaises(smith.CodeError) as ctx:
            self.run_worker(spec, cwd=cwd)
        self.assertIn(GATE, str(ctx.exception))

    def test_item1_an_aider_model_settings_file_is_claude_wherever_it_is_named_or_found(self):
        self.declared_script("aider")
        self.assertEqual(self.passes("aider", env={}), "aider")
        settings, body = ".aider.model.settings.yml", "- name: gpt-4o\n  extra_params:\n    model: anthropic/claude-3\n"
        sub = os.path.join(self.repo, "sub")
        os.makedirs(sub)
        for where in (self.homedir, self.repo):
            path = os.path.join(where, settings)
            self.write(path, body)
            self.gated("aider", env={"AIDER_MODEL": "gpt-4o"})
            os.unlink(path)
        self.write(os.path.join(self.repo, settings), body)
        self.gated_in("aider", sub)  # the git root's, from a folder below it
        os.unlink(os.path.join(self.repo, settings))
        self.gated("aider", env={"AIDER_MODEL_SETTINGS_FILE": "ms.yml"})
        for tpl in ("aider --model-settings-file ms.yml --message {prompt}", "aider --model-settings-file=ms.yml --message {prompt}",
                    "aider --model-settings ms.yml --message {prompt}", "aider --model gpt-4o --env-f x.env --message {prompt}"):
            self.gated("aider", tpl)
        self.write(os.path.join(self.repo, ".env"), "AIDER_MODEL_SETTINGS_FILE=ms.yml\n")
        self.gated("aider", env={"AIDER_MODEL": "gpt-4o"})
        os.unlink(os.path.join(self.repo, ".env"))
        self.assertEqual(self.passes("aider", env={}), "aider")

    def test_item1_a_short_flag_or_an_abbreviation_is_read_as_the_flag_it_stands_for(self):
        self.declared_script("aider")
        self.write(os.path.join(self.repo, "other.yml"), "model: claude-3\n")
        for tpl in ("aider --son --message {prompt}", "aider --opu --message {prompt}", "aider --hai --message {prompt}",
                    "aider --ali fast:claude-3 --message {prompt}", "aider --weak-m claude-3-haiku --message {prompt}",
                    "aider --editor-m sonnet --message {prompt}", "aider --mode claude-3 --message {prompt}",
                    "aider --mod claude-3 --message {prompt}", "aider -c other.yml --message {prompt}",
                    "aider -cother.yml --message {prompt}", "aider --conf other.yml --message {prompt}",
                    "aider --con=other.yml --message {prompt}", "aider --env-fi x.env --message {prompt}"):
            self.gated("aider", tpl)
        for tpl in ("aider --message {prompt}", "aider --map-tokens 0 --auto-commits --message {prompt}",
                    "aider --model gpt-4o --message {prompt}"):
            self.assertEqual(self.passes("aider", tpl, env={}), "aider", tpl)

    def test_item2_a_declared_script_runs_the_interpreter_that_was_pinned_not_one_found_on_path_later(self):
        from sampo import smith
        if not smith.FD_EXEC:
            self.skipTest("no /proc/self/fd here")
        sh = os.path.realpath(shutil.which("sh"))
        self.script("mycli", "#!/usr/bin/env sh\necho ran \"$1\"\n")
        self.assertEqual(self.declare("mycli").returncode, 0)
        pin = c.read_meta(c.find_project(self.repo)).get("non_claude_workers")["mycli"]
        self.assertEqual(pin["interp"]["path"], sh)
        self.assertEqual(len(pin["interp"]["sha256"]), 64)
        argv = self.run_worker("mycli")
        self.assertEqual(argv.pin.interp, sh)
        with smith.checked_command(argv) as (cmd, fds):
            self.assertRegex(cmd, r"^exec -a \S+ /proc/self/fd/\d+ /proc/self/fd/\d+ hi$")
            self.assertEqual(len(fds), 2)
            code, out = smith.run_bounded(cmd, self.repo, 20, dict(os.environ), keep_fds=fds)
        self.assertEqual((code, out.strip()), (0, "ran hi"))
        argv.pin.interp_sha256 = "0" * 64  # the interpreter's bytes are not the pinned ones
        with self.assertRaises(smith.CodeError) as ctx:
            with smith.checked_command(argv):
                pass
        self.assertIn("changed since", str(ctx.exception))
        self.script("sh", "#!/bin/dash\necho shim\n")  # a shim a later PATH finds first: the declaration no longer holds
        self.gated("mycli")

    def test_item2_a_declaration_made_before_the_interpreter_was_pinned_no_longer_holds(self):
        self.script("mycli", "#!/bin/sh\necho ok\n")
        self.assertEqual(self.declare("mycli").returncode, 0)
        self.assertEqual(self.run_worker("mycli").pin["path"], os.path.join(self.bin, "mycli"))
        meta = c.read_meta(c.find_project(self.repo))
        del meta["non_claude_workers"]["mycli"]["interp"]  # the shape an earlier round stored
        c.write_meta(c.find_project(self.repo), meta)
        self.gated("mycli")

    def test_item3_memfd_noexec_one_asks_for_an_exec_copy_and_two_makes_none(self):
        from sampo import smith
        if not hasattr(os, "memfd_create"):
            self.skipTest("no memfd_create here")
        real, seen = os.memfd_create, []

        def spy(name, flags=0):
            seen.append(flags)
            return real(name, os.MFD_ALLOW_SEALING)
        path = self.script("mycli", "#!/bin/sh\necho hi\n")
        for value, want in (("1", os.MFD_ALLOW_SEALING | 0x10), ("0", os.MFD_ALLOW_SEALING), ("", os.MFD_ALLOW_SEALING),
                            ("2", None)):
            del seen[:]
            fd = os.open(path, os.O_RDONLY)
            try:
                with mock.patch.object(smith, "_memfd_noexec", return_value=value), mock.patch.object(os, "memfd_create", spy):
                    got = smith._sealed_copy(fd)
            finally:
                os.close(fd)
            self.assertEqual(seen, [] if want is None else [want], value)
            self.assertEqual(got is None, want is None, value)
            if got:
                os.close(got[0])

    def test_item3_with_no_copy_a_writable_script_or_binary_is_refused(self):
        from sampo import smith
        if not smith.FD_EXEC:
            self.skipTest("no /proc/self/fd here")
        self.script("mycli", "#!/bin/sh\necho hi\n")
        self.assertEqual(self.declare("mycli").returncode, 0)
        argv = self.run_worker("mycli")
        with mock.patch.object(smith, "_sealed_copy", return_value=None):
            with self.assertRaises(smith.CodeError) as ctx:
                with smith.checked_command(argv):
                    pass
        self.assertIn("sealed copy", str(ctx.exception))
        echo = os.path.realpath(shutil.which("echo"))
        with open(echo, "rb") as f:
            if f.read(4) != b"\x7fELF":
                self.skipTest("echo is not a binary here")
        os.makedirs(self.bin, exist_ok=True)
        shutil.copy(echo, os.path.join(self.bin, "myelf"))
        self.assertEqual(self.declare("myelf").returncode, 0)
        argv = self.run_worker("myelf")
        with mock.patch.object(smith, "_sealed_copy", return_value=None):  # round 4: a binary too, never run from a writable inode
            with self.assertRaises(smith.CodeError) as ctx:
                with smith.checked_command(argv):
                    pass
        self.assertIn("sealed copy", str(ctx.exception))

    def test_item4_a_trusted_folder_path_or_a_claude_named_file_is_not_a_route_to_claude(self):
        self.declared_script("codex")
        self.declared_script("gemini")
        conf = os.path.join(self.homedir, ".codex", "config.toml")
        self.write(conf, 'model = "gpt-5"\n[projects."/home/u/.claude/foreman"]\ntrust_level = "trusted"\n'
                         '[mcp_servers.claude-code]\ncommand = "claude-notify"\n')
        self.assertEqual(self.passes("codex", env={}), "codex")
        for text in ('[model_providers.a]\nname = "Anthropic"\n', '[profiles.x]\nmodel = "claude-3"\n',
                     'model_provider = "OpenRouter"\n', 'openai_base_url = "https://openrouter.ai/api"\n',
                     '[projects."/x"]\ntrust_level = "trusted"\n[model_providers."anthropic"]\nx = 1\n'):
            self.write(conf, text)
            self.gated("codex")
        gem = os.path.join(self.homedir, ".gemini", "settings.json")
        self.write(gem, '{"contextFileName": "CLAUDE.md", "model": "gemini-2.5-pro", "mcpServers": {"claude": {"command": "x"}}}')
        self.assertEqual(self.passes("gemini", env={}), "gemini")
        for text in ('{"baseUrl": "https://openrouter.ai/api"}', '{"model": {"name": "claude-3"}}', '{"modelProvider": "anthropic"}',
                     '{"model": "\\u0061nthropic"}'):
            self.write(gem, text)
            self.gated("gemini")

    def test_item4_a_repo_env_key_gates_a_declared_aider_with_no_model_and_an_explicit_model_is_the_way_out(self):
        self.declared_script("aider")  # round 4 item 6: a declared aider counts a file's provider key too
        env_file = os.path.join(self.repo, ".env")
        self.write(env_file, "ANTHROPIC_API_KEY=sk-x\n")
        self.gated("aider", env={})
        self.assertEqual(self.passes("aider", env={"AIDER_MODEL": "gpt-4o"}), "aider")
        self.gated("aider", env={"ANTHROPIC_API_KEY": "sk-x"})  # the process's own key still chooses a Claude model
        self.write(env_file, "ANTHROPIC_BASE_URL=http://x\n")
        self.gated("aider")
        os.unlink(env_file)
        self.write(os.path.join(self.repo, ".aider.conf.yml"), "anthropic-api-key: sk-x\n")
        self.gated("aider", env={})
        self.assertEqual(self.passes("aider", "aider --model gpt-4o --message {prompt}", env={}), "aider")
        self.write(os.path.join(self.repo, ".aider.conf.yml"), "anthropic-api-key: sk-x\nmodel: claude-3\n")
        self.gated("aider")
        os.unlink(os.path.join(self.repo, ".aider.conf.yml"))
        sealed = self.sealed_dir()  # an undeclared built-in keeps the heuristic: the way out is the declaration
        with self.sealed(sealed):
            self.binary("aider")
            self.write(env_file, "ANTHROPIC_API_KEY=sk-x\n")
            self.assertIn(self.DECLARE.replace("codex", "aider"), self.gated("aider", env=self.path_env()))

    def test_item5_a_root_owned_venv_runs_under_the_shebang_path_and_every_link_must_be_sealed(self):
        from sampo import smith
        if not smith.FD_EXEC:
            self.skipTest("no /proc/self/fd here")
        sealed = self.sealed_dir()
        real = os.path.join(sealed, "usr", "bin", "python3.11")
        os.makedirs(os.path.dirname(real))
        shutil.copy(os.path.realpath(shutil.which("sh")), real)
        os.chmod(real, 0o755)
        venv = os.path.join(sealed, "venv", "bin")
        py = self.link(real, "python", where=venv)
        self.write(os.path.join(sealed, "venv", "pyvenv.cfg"), "home = /usr/bin\n")
        self.script("codex", f"#!{py}\ntr '\\0' ' ' < /proc/$$/cmdline\n", where=sealed)
        with self.sealed(sealed):
            argv = self.run_worker("codex", env=self.path_env())
            self.assertEqual(argv.pin.interp, os.path.realpath(real))
            self.assertEqual(argv.pin.interp_argv0, py)
            with smith.checked_command(argv) as (cmd, fds):
                self.assertTrue(cmd.startswith(f"exec -a {py} {os.path.realpath(real)} /proc/self/fd/"), cmd)
                code, out = smith.run_bounded(cmd, self.repo, 20, dict(os.environ), keep_fds=fds)
            self.assertEqual(code, 0)
            self.assertTrue(out.startswith(py + " /proc/self/fd/"), out)  # the venv's name is argv[0]: pyvenv.cfg is found
            orig = smith._sealed_path
            with mock.patch.object(smith, "_sealed_path", lambda p: False if p.endswith("pyvenv.cfg") else orig(p)):
                self.assertIn(self.DECLARE, self.gated("codex", env=self.path_env()))  # a config the agent could write
            mine = self.link(real, "python", where=os.path.join(self.tmp, "uvenv", "bin"))  # a link in a folder it can write
            self.script("codex", f"#!{mine}\necho ok\n", where=sealed)
            self.assertIn(self.DECLARE, self.gated("codex", env=self.path_env()))

    def test_item6_a_worker_gets_no_python_variable_at_all(self):
        from sampo import smith
        names = ("PYTHONWARNINGS", "PYTHONINSPECT", "PYTHONBREAKPOINT", "PYTHONSAFEPATH", "PYTHONEXECUTABLE", "PYTHONDEVMODE",
                 "PYTHONPYCACHEPREFIX", "PYTHONPATH")
        with mock.patch.dict(os.environ, {**{n: "x" for n in names}, "PYTHONNOUSERSITE": "", "KEEP_ME": "1"}):
            got = smith.worker_env(start=True)
        self.assertEqual([n for n in names if n in got], [])
        self.assertEqual(got.get("PYTHONNOUSERSITE"), "1")
        self.assertEqual(got.get("KEEP_ME"), "1")

    def test_item7_only_a_packages_own_entry_file_is_the_built_in_not_one_nested_in_a_dependency(self):
        sealed = self.sealed_dir()
        body = "#!/usr/bin/env node\nconsole.log(1)\n"
        gem = os.path.join(sealed, "lib", "node_modules", "@google", "gemini-cli")
        cod = os.path.join(sealed, "lib", "node_modules", "@openai", "codex")
        cases = (("gemini", os.path.join(gem, "dist"), "index.js", True), ("gemini", gem, "index.js", False),
                 ("gemini", os.path.join(gem, "dist"), "cli.js", False),
                 ("gemini", os.path.join(gem, "node_modules", "dep"), "cli.js", False),
                 ("gemini", os.path.join(gem, "node_modules", "dep", "dist"), "index.js", False),
                 ("gemini", os.path.join(gem, "node_modules", "@google", "gemini-cli", "dist"), "index.js", True),
                 ("codex", os.path.join(cod, "bin"), "codex.js", True), ("codex", os.path.join(cod, "dist"), "index.js", False),
                 ("codex", os.path.join(cod, "node_modules", "dep", "bin"), "codex.js", False),
                 ("codex", os.path.join(sealed, "lib"), "codex.js", False))
        with self.sealed(sealed):
            self.binary("node")
            for spec, where, name, ok in cases:
                self.link(self.script(name, body, where=where), spec)
                if ok:
                    self.assertEqual(self.passes(spec), name, (spec, where))
                else:
                    self.assertIn(f"worker {spec}", self.gated(spec, env=self.path_env()), (spec, where))


class RoundFour(WorkerFileCase):
    """T-5849 round 4: a provider key counts wherever aider is given it (variables, flags and their abbreviations, files) and a
    provider's base URL in the environment is a route, for a declared aider too (the way out is an explicit model); a codex
    short flag with its value attached is read; a declared script whose #! line can't be followed is not declared; the
    interpreter runs under its pinned realpath unless every link of the name is sealed; a file this user can write is never
    run from its own inode (a writable interpreter, a writable package .js or a binary with no sealed copy is refused) and a
    sealed one runs from its real file's descriptor; a PYTHON* variable is dropped only for a worker's start; a hash is kept
    by stat, never at the exec."""

    DECLARE, ELF = RoundTwo.DECLARE, RoundTwo.ELF
    sealed, sealed_dir, binary, link, path_env, passes = (RoundTwo.sealed, RoundTwo.sealed_dir, RoundTwo.binary, RoundTwo.link,
                                                          RoundTwo.path_env, RoundTwo.passes)

    def write(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    # 1. a provider key, wherever it is given
    def test_item1_an_aider_provider_key_in_the_environment_a_flag_or_an_abbreviation_is_claude_unless_a_model_is_chosen(self):
        self.declared_script("aider")
        self.assertEqual(self.passes("aider", env={}), "aider")
        for env in ({"AIDER_ANTHROPIC_API_KEY": "k"}, {"AIDER_OPENROUTER_API_KEY": "k"}, {"AIDER_API_KEY": "anthropic=k"},
                    {"AIDER_API_KEY": "openrouter=k"}, {"AIDER_SET_ENV": "ANTHROPIC_API_KEY=k"},
                    {"AIDER_SET_ENV": "OPENROUTER_API_KEY=k"}, {"ANTHROPIC_API_KEY": "k"}, {"OPENROUTER_API_KEY": "k"}):
            self.gated("aider", env=env)
            self.assertEqual(self.passes("aider", env={**env, "AIDER_MODEL": "gpt-4o"}), "aider", env)
        for env in ({"AIDER_API_KEY": "openai=k"}, {"AIDER_SET_ENV": "FOO=bar"}, {"OPENAI_API_KEY": "k"}, {"AIDER_ANTHROPIC_API_KEY": ""}):
            self.assertEqual(self.passes("aider", env=env), "aider", env)
        for tpl in ("--anthropic-api-key K", "--anthropic-api-key=K", "--openrouter-api-key K", "--openrouter-api-key=K",
                    "--api-key anthropic=K", "--api-key=openrouter=K", "--set-env ANTHROPIC_API_KEY=K", "--set-env=OPENROUTER_API_KEY=K",
                    "--anthropic-a K", "--openr K", "--api-k anthropic=K", "--api-k=openrouter=K", "--set-e ANTHROPIC_API_KEY=K"):
            self.gated("aider", f"aider {tpl} --message {{prompt}}")
            self.assertEqual(self.passes("aider", f"aider --model gpt-4o {tpl} --message {{prompt}}", env={}), "aider", tpl)
        for tpl in ("--api-key openai=K", "--set-env FOO=bar", "--api-key=openai=K"):
            self.assertEqual(self.passes("aider", f"aider {tpl} --message {{prompt}}", env={}), "aider", tpl)

    def test_item1_a_provider_key_line_of_an_env_file_or_a_config_is_claude_unless_a_model_is_chosen(self):
        self.declared_script("aider")
        env_file = os.path.join(self.repo, ".env")
        for line in ("AIDER_ANTHROPIC_API_KEY=k", "AIDER_OPENROUTER_API_KEY=k", "AIDER_API_KEY=anthropic=k", "AIDER_API_KEY=openrouter=k",
                     "AIDER_SET_ENV=ANTHROPIC_API_KEY=k", "export OPENROUTER_API_KEY=k", "ANTHROPIC_API_KEY=k"):
            self.write(env_file, line + "\n")
            self.gated("aider", env={})
            self.assertEqual(self.passes("aider", env={"AIDER_MODEL": "gpt-4o"}), "aider", line)
        self.write(env_file, "AIDER_API_KEY=openai=k\n")
        self.assertEqual(self.passes("aider", env={}), "aider")
        os.unlink(env_file)
        conf = os.path.join(self.repo, ".aider.conf.yml")
        for text in ("anthropic-api-key: k\n", "openrouter-api-key: k\n", "api-key:\n  - anthropic=k\n", "set-env: ANTHROPIC_API_KEY=k\n"):
            self.write(conf, text)
            self.gated("aider", env={})
            self.write(conf, text + "model: gpt-4o\n")
            self.assertEqual(self.passes("aider", env={}), "aider", text)

    def test_item1_a_base_url_naming_openrouter_or_anthropic_in_the_environment_is_a_route_even_with_a_model(self):
        self.declared_script("aider")
        for env in ({"OPENAI_API_BASE": "https://openrouter.ai/api/v1"}, {"OPENAI_API_BASE": "https://api.anthropic.com"},
                    {"ANTHROPIC_BASE_URL": "http://x"}, {"AIDER_OPENROUTER_API_BASE": "http://x"}, {"OPENROUTER_API_BASE": "http://x"}):
            self.gated("aider", env=env)
            self.gated("aider", env={**env, "AIDER_MODEL": "gpt-4o"})
        self.assertEqual(self.passes("aider", env={"OPENAI_API_BASE": "http://localhost:8080", "AIDER_MODEL": "gpt-4o"}), "aider")

    # 2. codex short flags with the value attached
    def test_item2_a_codex_short_flag_with_its_value_attached_is_read(self):
        self.declared_script("codex")
        self.assertEqual(self.passes("codex", "codex exec {prompt}", env={}), "codex")
        for flag in ("-cmodel=claude-3", "-c=model=claude", "-cmodel_provider=openrouter", "-mclaude-3", "-m=claude-3",
                     "-pclaude", "-p=anthropic", "-msonnet", "-c", "-m"):
            tpl = f"codex exec {flag} {{prompt}}" if flag not in ("-c", "-m") else f"codex exec {flag} claude-3 {{prompt}}"
            self.gated("codex", tpl)
        for flag in ("-mgpt-5", "-m=gpt-5", "-cmodel=gpt-5", "-c=model=gpt-5", "-pwork"):
            self.assertEqual(self.passes("codex", f"codex exec {flag} {{prompt}}", env={}), "codex", flag)

    # 3. an unfollowable shebang
    def test_item3_a_script_whose_shebang_line_cannot_be_followed_is_not_declared(self):
        for i, line in enumerate(("#!/bin/sh -e", "#!/usr/bin/env -S sh -e", "#!/usr/bin/env -i sh", "#!/bin/nowhere-x", "#!")):
            name = f"mycli{i}"
            self.script(name, f"{line}\necho ok\n")
            p = self.declare(name)
            self.assertNotEqual(p.returncode, 0, line)
            self.assertIn("shebang", p.stdout + p.stderr, line)
            self.assertFalse(c.read_meta(c.find_project(self.repo)).get("non_claude_workers"), line)
        self.script("good", "#!/usr/bin/env sh\necho ok\n")
        self.assertEqual(self.declare("good").returncode, 0)

    def test_item3_a_hand_written_pin_of_such_a_script_does_not_hold(self):
        from sampo import smith
        sh = os.path.realpath(shutil.which("sh"))
        sh_sha = hashlib.sha256(open(sh, "rb").read()).hexdigest()
        path = self.script("mycli", "#!/bin/sh -e\necho ok\n")
        sha = hashlib.sha256(open(path, "rb").read()).hexdigest()
        for pin in ({"path": os.path.realpath(path), "sha256": sha},
                    {"path": os.path.realpath(path), "sha256": sha, "interp": {"path": sh, "sha256": sh_sha}}):
            meta = c.read_meta(c.find_project(self.repo))
            meta["non_claude_workers"] = {"mycli": pin}
            c.write_meta(c.find_project(self.repo), meta)
            self.assertIn(GATE, self.gated("mycli"))
        with mock.patch.dict(os.environ, {"PATH": os.pathsep.join([self.bin, os.environ.get("PATH", "")])}):
            self.assertIsNone(smith.worker_pin("mycli", "mycli"))
            self.assertIn("shebang", smith.declaration_refusal("mycli", "mycli") or "")

    # 4. argv0 from an unchecked shim
    def test_item4_the_interpreter_runs_under_its_pinned_realpath_unless_every_link_of_the_name_is_sealed(self):
        from sampo import smith
        sealed = self.sealed_dir()
        real = self.binary("python3.11", where=os.path.join(sealed, "usr", "bin"))
        mine = self.link(real, "python", where=os.path.join(self.tmp, "uvenv", "bin"))  # a link in a folder the agent can write
        self.script("mycli", f"#!{mine}\necho ok\n")
        self.assertEqual(self.declare("mycli").returncode, 0)
        argv = self.run_worker("mycli")
        self.assertEqual(argv.pin.interp, os.path.realpath(real))
        self.assertEqual(argv.pin.interp_argv0, os.path.realpath(real), "a shim the agent can re-point never names the process")
        with self.sealed(os.path.dirname(mine), sealed):
            argv = self.run_worker("mycli")
            self.assertEqual(argv.pin.interp_argv0, mine)  # every link sealed: the venv's name is kept (pyvenv.cfg is found by it)
        self.assertNotEqual(self.run_worker("mycli").pin.interp_argv0, mine)

    # 5. a file this user can write is never run from its own inode
    def test_item5_a_writable_interpreter_is_refused_and_a_sealed_one_runs(self):
        from sampo import smith
        if not smith.FD_EXEC:
            self.skipTest("no /proc/self/fd here")
        mysh = os.path.join(self.tmp, "writable", "sh")
        os.makedirs(os.path.dirname(mysh))
        shutil.copy(os.path.realpath(shutil.which("sh")), mysh)
        os.chmod(mysh, 0o755)
        path = self.script("mycli", f"#!{mysh}\necho hi\n")
        self.assertEqual(self.declare("mycli").returncode, 0)
        with self.sealed(self.bin):  # the script is sealed (it runs from its own descriptor): the interpreter alone is writable
            argv = self.run_worker("mycli")
            with self.assertRaises(smith.CodeError) as ctx:
                with smith.checked_command(argv):
                    pass
            self.assertIn("interpreter", str(ctx.exception))
            self.assertIn(os.path.realpath(mysh), str(ctx.exception))
            with self.sealed(self.bin, os.path.dirname(mysh)):  # the same file in a folder only root could write runs
                argv = self.run_worker("mycli")
                with smith.checked_command(argv) as (cmd, fds):
                    code, out = smith.run_bounded(cmd, self.repo, 20, dict(os.environ), keep_fds=fds)
                self.assertEqual((code, out.strip()), (0, "hi"))
        self.assertTrue(os.path.exists(path))

    def test_item5_a_writable_binary_with_no_sealed_copy_is_refused_and_with_one_runs_from_the_copy(self):
        from sampo import smith
        if not smith.FD_EXEC:
            self.skipTest("no /proc/self/fd here")
        echo = os.path.realpath(shutil.which("echo"))
        with open(echo, "rb") as f:
            if f.read(4) != b"\x7fELF":
                self.skipTest("echo is not a binary here")
        os.makedirs(self.bin, exist_ok=True)
        shutil.copy(echo, os.path.join(self.bin, "myelf"))
        self.assertEqual(self.declare("myelf").returncode, 0)
        argv = self.run_worker("myelf")
        with mock.patch.object(smith, "_sealed_copy", return_value=None):
            with self.assertRaises(smith.CodeError) as ctx:
                with smith.checked_command(argv):
                    pass
        self.assertIn("sealed copy", str(ctx.exception))
        if smith._memfd_noexec() != "2" and hasattr(os, "memfd_create"):
            with smith.checked_command(argv) as (cmd, fds):
                self.assertTrue(os.readlink(f"/proc/self/fd/{fds[0]}").startswith("/memfd:"))  # a copy, never the inode
                code, out = smith.run_bounded(cmd, self.repo, 20, dict(os.environ), keep_fds=fds)
            self.assertEqual(code, 0)

    # 6. declared workers: a key in a file counts
    def test_item6_a_declared_aider_with_a_key_in_a_file_and_no_model_is_claude_and_a_model_is_the_way_out(self):
        self.declared_script("aider")
        self.write(os.path.join(self.repo, ".env"), "OPENROUTER_API_KEY=k\n")
        self.gated("aider", env={})
        self.gated("aider", "aider --message {prompt}", env={})
        self.assertEqual(self.passes("aider", env={"AIDER_MODEL": "gpt-4o"}), "aider")
        self.assertEqual(self.passes("aider", "aider --model gpt-4o --message {prompt}", env={}), "aider")
        self.write(os.path.join(self.repo, ".env"), "OPENROUTER_API_KEY=k\nAIDER_MODEL=openrouter/anthropic/claude-3\n")
        self.gated("aider", env={"AIDER_MODEL": "gpt-4o"})  # the file picks a Claude model itself
        os.unlink(os.path.join(self.repo, ".env"))
        self.write(os.path.join(self.homedir, ".aider.conf.yml"), "openrouter-api-key: k\n")
        self.gated("aider", env={})
        self.write(os.path.join(self.homedir, ".aider.conf.yml"), "openrouter-api-key: k\nmodel: gpt-4o\n")
        self.assertEqual(self.passes("aider", env={}), "aider")

    # 7. a package's .js
    def test_item7_a_sealed_package_js_runs_from_its_real_file_and_a_writable_one_is_refused(self):
        from sampo import smith
        if not smith.FD_EXEC:
            self.skipTest("no /proc/self/fd here")
        sealed = self.sealed_dir()
        pkg = os.path.join(sealed, "lib", "node_modules", "@openai", "codex")
        self.write(os.path.join(pkg, "package.json"), '{"name": "@openai/codex"}\n')
        node = self.binary("node")
        js = self.script("codex.js", "#!/usr/bin/env node\nconsole.log(1)\n", where=os.path.join(pkg, "bin"))
        self.link(js, "codex")
        with self.sealed(sealed):
            argv = self.run_worker("codex", env=self.path_env())
            with smith.checked_command(argv) as (cmd, fds):
                self.assertEqual(os.readlink(f"/proc/self/fd/{fds[0]}"), os.path.realpath(js))  # a package finds its files by this path
                self.assertIn(os.path.realpath(node), cmd)
        mine = os.path.join(self.tmp, "mine", "node_modules", "@openai", "codex")  # the same package in a folder the agent can write
        self.write(os.path.join(mine, "package.json"), '{"name": "@openai/codex"}\n')
        mine_js = self.script("codex.js", f"#!{node}\nconsole.log(1)\n", where=os.path.join(mine, "bin"))
        os.makedirs(self.bin, exist_ok=True)
        os.symlink(mine_js, os.path.join(self.bin, "codex"))
        self.assertEqual(self.declare("codex").returncode, 0)
        with self.sealed(sealed):  # the interpreter is sealed; the package file is not
            argv = self.run_worker("codex")
            with self.assertRaises(smith.CodeError) as ctx:
                with smith.checked_command(argv):
                    pass
            self.assertIn("package", str(ctx.exception))
            self.assertIn("root", str(ctx.exception))
            with self.sealed(sealed, mine):
                argv = self.run_worker("codex")
                with smith.checked_command(argv) as (cmd, fds):
                    self.assertEqual(os.readlink(f"/proc/self/fd/{fds[0]}"), os.path.realpath(mine_js))
        plain = self.script("lone.js", f"#!{node}\nconsole.log(1)\n", where=os.path.join(self.tmp, "nopkg"))
        self.assertTrue(os.path.exists(plain))  # a .js with no package.json above it is no package's file: it is copied as any script

    # 8. PYTHON* only for a worker's start
    def test_item8_a_worker_start_drops_every_python_variable_and_a_verify_run_keeps_the_projects(self):
        from sampo import smith
        names = ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONUSERBASE", "PYTHONWARNINGS", "PYTHONINSPECT")
        with mock.patch.dict(os.environ, {**{n: "x" for n in names}, "PYTHONNOUSERSITE": "", "BASH_FUNC_f%%": "() { :; }",
                                          "LD_PRELOAD": "x", "KEEP_ME": "1"}):
            start, plain = smith.worker_env(start=True), smith.worker_env()
        self.assertEqual([n for n in names if n in start], [])
        self.assertEqual(start["PYTHONNOUSERSITE"], "1")
        self.assertEqual([n for n in names if n not in plain], [], "a verify or test run keeps the project's own Python variables")
        self.assertEqual(plain.get("PYTHONNOUSERSITE"), "", "and sets no user-site switch of its own")
        for got in (start, plain):
            self.assertNotIn("BASH_FUNC_f%%", got)
            self.assertNotIn("LD_PRELOAD", got)
            self.assertEqual(got["KEEP_ME"], "1")

    def test_item8_every_start_of_a_worker_passes_start_true(self):
        with open(os.path.join(LIB, "smith.py"), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        seen, bad = [], []

        class Visit(ast.NodeVisitor):
            def __init__(self):
                self.stack = []

            def visit_FunctionDef(self, node):
                self.stack.append(node.name)
                self.generic_visit(node)
                self.stack.pop()

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_Call(self, node):
                f = node.func
                if (f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")) == "worker_env":
                    seen.append(node.lineno)
                    on = any(k.arg == "start" and isinstance(k.value, ast.Constant) and k.value.value is True for k in node.keywords)
                    if not on and self.stack[-1:] != ["env"]:  # Run.env is the verify's environment: it keeps the project's
                        bad.append(node.lineno)
                self.generic_visit(node)
        Visit().visit(tree)
        self.assertGreaterEqual(len(seen), 5)
        self.assertEqual(bad, [], "a worker_env() call outside Run.env must be worker_env(start=True)")

    # 9. a hash is kept by stat
    def test_item9_a_file_is_hashed_once_until_it_changes_and_a_forged_mtime_gives_no_stale_hash(self):
        from sampo import smith
        smith._HASHES.clear()
        calls = []
        real = hashlib.sha256

        def counting(*a, **k):
            calls.append(1)
            return real(*a, **k)
        path = self.script("f", "#!/bin/sh\necho one\n")
        with mock.patch.object(smith.hashlib, "sha256", counting):
            first = smith._hash_file(path)
            self.assertEqual(smith._hash_file(path), first)
            self.assertEqual(len(calls), 1)
            self.write(path, "#!/bin/sh\necho changed size\n")
            second = smith._hash_file(path)
            self.assertNotEqual(second, first)
            self.assertEqual(len(calls), 2)
            st = os.stat(path)
            time.sleep(0.05)
            self.write(path, "#!/bin/sh\necho changed sizE\n")  # the same size
            os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))  # with the old mtime put back
            third = smith._hash_file(path)
            self.assertEqual(third, real(open(path, "rb").read()).hexdigest())
            self.assertNotEqual(third, second)
            self.assertEqual(len(calls), 3)  # a changed ctime is a new key

    def test_item9_a_start_hashes_each_file_once_and_the_exec_check_is_never_cached(self):
        from sampo import smith
        if not smith.FD_EXEC:
            self.skipTest("no /proc/self/fd here")
        path = self.declared_script("mycli", "#!/bin/sh\necho original\n")
        smith._HASHES.clear()
        calls = []
        real = hashlib.sha256

        def counting(*a, **k):
            calls.append(1)
            return real(*a, **k)
        with mock.patch.object(smith.hashlib, "sha256", counting):
            argv = self.run_worker("mycli")
            once = len(calls)
            self.assertGreaterEqual(once, 1)
            self.run_worker("mycli")
            self.assertEqual(len(calls), once, "the second check of the same unchanged files hashes nothing")
        st = os.stat(path)
        time.sleep(0.05)
        self.write(path, "#!/bin/sh\necho changed!\n")  # the same size, the old mtime put back: only ctime tells
        self.assertEqual(os.path.getsize(path), st.st_size)
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
        with self.assertRaises(smith.CodeError) as ctx:  # the exec hashes the descriptor it holds, never a kept value
            with smith.checked_command(argv):
                pass
        self.assertIn("changed since", str(ctx.exception))
        self.gated("mycli")  # and a new check reads the file again: the declaration no longer holds


class InterpreterDeclaration(WorkerFileCase):
    """T-5849: a declaration pins one file, so an interpreter, shell or wrapper as argv[0] (which loads other files the
    pin never sees) is refused with the fix: an executable script with a shebang, declared as argv[0]."""

    def pins(self):
        return c.read_meta(c.find_project(self.repo)).get("non_claude_workers")

    def test_interpreter_shell_and_wrapper_argv0_is_refused_naming_the_fix(self):
        self.script("mycli")
        for spec in ("sh {prompt}", "bash -c 'mycli {prompt}'", "zsh", "dash x {prompt}", "fish {prompt}", "env mycli {prompt}",
                     "python3 mycli.py {prompt}", "python3.12 x.py {prompt}", "python x.py {prompt}", "node mycli.js {prompt}",
                     "deno run x.ts {prompt}", "bun x.ts {prompt}", "perl x.pl {prompt}", "ruby x.rb {prompt}",
                     "php x.php {prompt}", "lua x.lua {prompt}", "osascript x.scpt {prompt}", "nice mycli {prompt}",
                     "nohup mycli {prompt}", "timeout 5 mycli {prompt}", "xargs mycli {prompt}", "/usr/bin/env mycli {prompt}",
                     "/bin/bash", "BASH {prompt}"):
            p = self.declare(spec)
            out = p.stdout + p.stderr
            self.assertNotEqual(p.returncode, 0, spec)
            self.assertIn("shebang", out, spec)
            self.assertIn("executable", out, spec)
        self.assertFalse(self.pins(), "a refused declaration pins nothing")

    def test_interpreter_reached_through_a_symlink_is_refused(self):
        os.makedirs(self.bin)
        sh = os.path.realpath("/bin/sh")
        os.symlink(sh, os.path.join(self.bin, "mycli"))
        p = self.declare("mycli")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("shebang", p.stdout + p.stderr)
        self.assertFalse(self.pins())

    def test_interpreter_refusal_leaves_an_executable_script_declarable(self):
        self.script("mycli", "#!/usr/bin/env python3\nprint('ok')\n")
        p = self.declare("mycli")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(list(self.pins()), ["mycli"])
        p = self.fm("claude-bench", "worker", "sh {prompt}", "--rm", check=False)  # taking one back is never refused
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)


class CheckedFile(WorkerFileCase):
    """T-5849: worker_argv runs the file the check resolved. argv[0] is that realpath and, on Linux, the process execs the
    file it hashed (opened once, hashed from the fd, run as /proc/self/fd/N), so a swap after the check runs nothing new."""

    def declared(self, name="mycli", body="#!/bin/sh\necho original \"$1\"\n"):
        path = self.script(name, body)
        self.fm("claude-bench", "worker", name,
                env={"PATH": os.pathsep.join([self.bin, os.environ.get("PATH", "")])})
        return path

    def run_cmd(self, argv, hook=None):
        from sampo import smith
        with smith.checked_command(argv, after_open=hook) as (cmd, fds):
            return smith.run_bounded(cmd, self.repo, 20, dict(os.environ), keep_fds=fds)

    def test_checked_file_argv0_is_the_realpath_the_check_resolved(self):
        real = self.script("mycli-real", where=os.path.join(self.tmp, "real"))
        os.makedirs(self.bin)
        os.symlink(real, os.path.join(self.bin, "mycli"))
        self.fm("claude-bench", "worker", "mycli",
                env={"PATH": os.pathsep.join([self.bin, os.environ.get("PATH", "")])})
        argv = self.run_worker("mycli")
        self.assertEqual(argv, [os.path.realpath(real), "hi"])
        self.assertEqual(argv.pin["path"], os.path.realpath(real))
        self.script("codex", where=os.path.join(self.tmp, "b2"))
        self.fm("claude-bench", "worker", "codex", env={"PATH": os.path.join(self.tmp, "b2")})
        argv = self.run_worker("codex", env={"PATH": os.path.join(self.tmp, "b2")})
        self.assertEqual(argv[0], os.path.realpath(os.path.join(self.tmp, "b2", "codex")))
        self.assertEqual(argv[1:], ["exec", "--full-auto", "hi"])

    def test_checked_file_an_undeclared_worker_keeps_its_name(self):
        self.fm("claude-bench", "on")
        self.script("hop")
        argv = self.run_worker("hop {prompt}")
        self.assertEqual(argv, ["hop", "hi"])
        self.assertIsNone(getattr(argv, "pin", None))

    def test_checked_file_runs_the_hashed_file(self):
        self.declared()
        argv = self.run_worker("mycli")
        code, out = self.run_cmd(argv)
        self.assertEqual((code, out.strip()), (0, "original hi"))

    def test_checked_file_swapped_after_the_check_is_refused(self):
        from sampo import smith
        path = self.declared()
        argv = self.run_worker("mycli")
        with open(path, "w", encoding="utf-8") as f:  # an agent edits the declared script between check and start
            f.write("#!/bin/sh\nclaude -p \"$1\"\n")
        with self.assertRaises(smith.CodeError) as ctx:
            self.run_cmd(argv)
        self.assertIn("changed since", str(ctx.exception))

    def test_checked_file_swapped_after_it_was_hashed_is_not_what_runs(self):
        if not os.path.isdir("/proc/self/fd"):
            self.skipTest("no /proc/self/fd here")
        path = self.declared()
        argv = self.run_worker("mycli")

        def swap():  # after the open and the hash, before the process starts: a new file takes the path
            other = path + ".new"
            with open(other, "w", encoding="utf-8") as f:
                f.write("#!/bin/sh\necho swapped\n")
            os.chmod(other, 0o755)
            os.replace(other, path)
        code, out = self.run_cmd(argv, hook=swap)
        self.assertEqual((code, out.strip()), (0, "original hi"))
        with open(path, encoding="utf-8") as f:
            self.assertIn("swapped", f.read())

    def test_checked_file_fd_is_closed_after_the_run(self):
        from sampo import smith
        self.declared()
        argv = self.run_worker("mycli")
        before = set(os.listdir("/proc/self/fd")) if os.path.isdir("/proc/self/fd") else set()
        with smith.checked_command(argv) as (cmd, fds):
            self.assertTrue(cmd)
        if before:
            self.assertEqual(set(os.listdir("/proc/self/fd")) - before, set())

    def test_checked_file_without_proc_runs_the_realpath_after_a_rehash(self):
        from sampo import smith
        path = self.declared()
        argv = self.run_worker("mycli")
        with mock.patch.object(smith, "FD_EXEC", False):
            code, out = self.run_cmd(argv)
            self.assertEqual((code, out.strip()), (0, "original hi"))
            with open(path, "a", encoding="utf-8") as f:
                f.write("echo more\n")
            with self.assertRaises(smith.CodeError):
                self.run_cmd(argv)

    def test_inplace_a_write_to_a_declared_file_after_the_hash_is_not_what_runs(self):
        if not os.path.isdir("/proc/self/fd"):
            self.skipTest("no /proc/self/fd here")
        path = self.declared()
        argv = self.run_worker("mycli")

        def edit():  # the same inode, rewritten after the hash: a held fd alone would run this
            with open(path, "w", encoding="utf-8") as f:
                f.write("#!/bin/sh\necho edited\n")
        code, out = self.run_cmd(argv, hook=edit)
        self.assertEqual((code, out.strip()), (0, "original hi"))
        with open(path, encoding="utf-8") as f:
            self.assertIn("edited", f.read())

    def test_inplace_the_script_sees_the_fd_path_as_its_own_name_and_the_copy_is_sealed(self):
        from sampo import smith
        if not os.path.isdir("/proc/self/fd"):
            self.skipTest("no /proc/self/fd here")
        self.declared("mycli", "#!/bin/sh\necho \"$0\"\n")
        code, out = self.run_cmd(self.run_worker("mycli"))
        self.assertEqual(code, 0)
        self.assertTrue(out.strip().startswith("/proc/self/fd/"), out)
        path = os.path.join(self.bin, "mycli")
        fd = os.open(path, os.O_RDONLY)
        try:
            got = smith._sealed_copy(fd)
        finally:
            os.close(fd)
        if got is None:
            self.skipTest("no memfd_create here")
        try:
            for attempt in (lambda: os.write(got[0], b"x"), lambda: os.ftruncate(got[0], 0)):
                with self.assertRaises(OSError):
                    attempt()
        finally:
            os.close(got[0])

    def test_inplace_a_file_the_user_cannot_write_keeps_the_held_fd(self):
        from sampo import smith
        if not os.path.isdir("/proc/self/fd"):
            self.skipTest("no /proc/self/fd here")
        self.declared()
        argv = self.run_worker("mycli")
        with mock.patch.object(smith, "_sealed_path", return_value=True), \
                mock.patch.object(smith, "_sealed_copy", side_effect=AssertionError("copied")):
            code, out = self.run_cmd(argv)
        self.assertEqual((code, out.strip()), (0, "original hi"))

    def test_inplace_without_proc_a_write_after_the_hash_is_refused_at_the_spawn(self):
        from sampo import smith
        path = self.declared()
        argv = self.run_worker("mycli")

        def edit():
            with open(path, "a", encoding="utf-8") as f:
                f.write("echo more\n")
        with mock.patch.object(smith, "FD_EXEC", False):
            with self.assertRaises(smith.CodeError):
                self.run_cmd(argv, hook=edit)


class DocsSay(ForemanTestCase):
    """T-5849 round 1: the docs say what holds a built-in name trusted and that the INTERPRETERS list is a guard-rail."""

    def test_docs_ops_2_and_readme_say_the_interpreters_list_is_a_guard_rail_not_a_boundary(self):
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
        for rel in ("docs/map/ops-2.md", "README.md"):
            with open(os.path.join(root, rel), encoding="utf-8") as f:
                text = f.read()
            self.assertIn("guard-rail", text, rel)
            self.assertIn("not a boundary", text, rel)
            self.assertIn("/proc/self/fd/N", text, rel)
            self.assertIn("declare it", text, rel)
            self.assertIn("OPENAI_BASE_URL", text, rel)  # round 2: base-URL routes are not judged, and the docs say so
            self.assertIn("GOOGLE_GEMINI_BASE_URL", text, rel)
            self.assertIn("accidental Claude use", text, rel)
            self.assertIn("is not pinned", text, rel)  # round 3: a user-owned package tree, and the off-Linux residual
            self.assertIn("until the interpreter reads the file", text, rel)
            self.assertIn("Homebrew", text, rel)


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
        self.assertEqual(self.worker("mycli"), [pin["path"], "hi"])  # T-5849: argv[0] is the file the check resolved
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
        real = os.path.realpath(path)  # T-5849: a declared worker runs as the file the check resolved
        self.assertEqual(self.worker("mycli"), [real, "hi"])
        with open(path, "a", encoding="utf-8") as f:
            f.write("claude -p \"$1\"\n")  # an agent edits the declared script to call claude
        msg = self.refused("mycli")
        self.assertIn("re-declares it with: ! sampo claude-bench worker mycli", msg)
        self.fm("claude-bench", "worker", "mycli", env=self.env())  # the user looked and re-declared: the new file counts
        self.assertEqual(self.worker("mycli"), [real, "hi"])

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
        self.assertEqual(self.worker("mycli"), [os.path.realpath(os.path.join(self.bin, "mycli")), "hi"])  # the pinned file, found again, still counts

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
