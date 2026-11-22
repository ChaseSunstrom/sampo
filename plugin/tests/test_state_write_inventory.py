"""T-5036 (slice of T-4333): every path the library derives from the real home (os.path.expanduser, Path.home, the HOME
variable, the passwd entry) is on a reviewed list. A test runs under FOREMAN_HOME and XDG_STATE_HOME (helpers.py setUp)
but not under its own HOME, so a state write that goes to the real home instead is a test that damages the machine it
runs on. An AST walk over plugin/lib finds each such derivation, names its enclosing function, guesses whether that
function writes, and fails for any (file, function) that is not on the list with a kind and a reason, for a kind the walk
contradicts (a read that gained a write), and for a stale entry (listed, but the derivation is gone).

What the walk does not see: a value that leaves its function (a helper returns a home path and a caller writes through
it: the helper's entry is reviewed as a write, by hand), a home spelled as a plain string (~ in a literal that a shell or
a child expands), and a derivation through a name it cannot resolve (getattr, exec). The list is a review aid, not a proof."""
import ast
import json
import os
import unittest

from helpers import LIB

# what each REVIEWED entry looked at: its set of derivation kinds and how many there are (state_write_pins.json). A reviewed
# function that gains a use or a second derivation fails until it is reviewed again and its pin is updated by hand
# (pins_of(inventory(lib_trees())) lists the current ones)
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "state_write_pins.json"), encoding="utf-8") as _fh:
    PINS = {tuple(k.split("::")): (tuple(v[0]), v[1]) for k, v in json.load(_fh).items()}

# the call names that make a function a writer. A heuristic over the one function: a helper that only returns a home
# path reads as a read here, and its entry on the list says "write" by hand, naming who writes through it
WRITE_ATTRS = frozenset((
    "write_text", "write_bytes", "mkdir", "makedirs", "touch", "unlink", "rmtree", "rename", "renames", "symlink", "copy2",
    "copyfile", "copytree", "chmod", "utime", "truncate", "write", "writelines", "write_atomic", "append_line", "write_meta",
    "atomic_write"))
SHARED_NAMES = frozenset(("replace", "remove", "link", "copy", "move"))  # str.replace, list.remove: a write only via os, shutil
# a function that names one of these is routed through the sandbox's env: the real home is only its fallback
ROUTED_NAMES = frozenset(("FOREMAN_HOME", "XDG_STATE_HOME", "FOREMAN_STATE"))  # the env names themselves, never a helper:
# setup.paths() names foreman_home() for its link targets and still builds its settings and link paths from the real home
OPEN_WRITE = frozenset("wax+")
KINDS = ("read", "mixed", "write")


def _review(kind, reason, *names):
    """{(file under plugin/lib, qualified function): (kind, reason)} for names written 'file::function'."""
    return {tuple(n.split("::")): (kind, reason) for n in names}


# kind "read": the function only reads the real home, or matches a path against it. "mixed": it writes, but somewhere
# else (the state folder, a temp home): the real home is only read. "write": the real home is a place it, or a caller
# through the path it returns, writes to on purpose; the reason says what keeps a test off the real one. Never a real
# path, name or host in a reason: placeholders only.
REVIEWED = {
    **_review("read", "expands or matches a path of a command, file or scope it is judging (guard verdict, scope check, "
              "replay of a recorded command); the real home is only compared, nothing is opened for writing",
              "fmguard.py::_expand", "fmguard.py::_here", "fmguard.py::_real_fm.same", "fmguard.py::_run_dir",
              "fmguard.py::_script_text", "fmguard.py::_script_verdict", "fmguard.py::_writes_unrun_draft",
              "fmguard.py::check_bash", "sampo/hooks.py::_cd_path", "sampo/hooks.py::_credential_read",
              "sampo/hooks.py::_guard_ctx", "sampo/hooks.py::_rule_hits", "sampo/hooks.py::_ui_changed",
              "sampo/cli.py::_argv", "sampo/cli.py::_guard_ctx", "sampo/cli.py::_queue_preview",
              "sampo/core.py::_until_path", "sampo/core.py::lint_verify",
              "sampo/core.py::undo_ok", "sampo/core.py::unrunnable", "sampo/doors.py::_ctx", "sampo/doors.py::_full",
              "sampo/edit.py::_check", "sampo/lanes.py::land_holds", "sampo/recall.py::explain", "sampo/replay.py::_ctx",
              "sampo/replay.py::_store_cwd", "sampo/replay.py::resolve_cwd", "sampo/replay.py::mine",
              "sampo/replay.py::score", "sampo/replay.py::verdicts", "sampo/risk.py::_names", "sampo/risk.py::_rewrite",
              "sampo/risk.py::_targets", "sampo/oracle.py::disagreements", "sampo/oracle.py::ran_in",
              "sampo/smith.py::Run.hidden", "sampo/smith.py::Run.judge", "sampo/smith.py::_snapshot",
              "sampo/smith.py::_strict", "sampo/board.py::part_tests", "sampo/deps.py::_grep",
              "sampo/landlock.py::writable_for", "sampo/long.py::_activity", "sampo/tidy.py::dead_paths",
              "sampo/sync.py::_from_mirror", "sampo/rune.py::card", "sampo/settings.py::_guard_block",
              "sampo/callcards.py::<module>", "sampo/callcards.py::_system_reads", "sampo/homemove.py::_parent",
              "sampo/sandbox.py::_homes", "sampo/secrets.py::_claude", "sampo/bench.py::_forms",
              "sampo/hooks.py::_cargo_note.place", "sampo/lanes.py::is_credential", "sampo/localruns.py::rows_path",
              "sampo/smith.py::search_tree_refusal", "sampo/budget.py::_wf_read", "sampo/budget.py::_wf_named",
              "sampo/jobsreg.py::is_secret", "sampo/jobsreg.py::log_ok"),  # T-5911: a script, workflow or log path only read or compared  # T-5553: the path a command names is only compared or read
    **_review("read", "takes the account's login or home name to scrub or recognize it in text; never opens a path under it",
              "sampo/core.py::redact_local", "sampo/profile.py::_home", "sampo/profile.py::_logins", "sampo/page.py::_private",
              "sampo/readme.py::private_hits", "sampo/lake.py::collect", "sampo/sync.py::_namer"),
    **_review("read", "reads Claude Code's or another tool's files under the real home (transcripts, settings, installed "
              "plugins, its json state, the toolchain); a test that reads them sees this machine's, and only reads",
              "sampo/claude.py::config_dir", "sampo/cost.py::_window", "sampo/cost.py::burn_findings",
              "sampo/cost.py::transcripts_dir", "sampo/friction.py::_transcript_commands", "sampo/plugins.py::claude_dir",
              "sampo/sandbox.py::_settings_path", "sampo/serve.py::_sandbox", "sampo/serve.py::_transcript_mark",
              "sampo/serve.py::trusted", "sampo/settings.py::rows", "sampo/which.py::binary", "sampo/which.py::mcp",
              "sampo/core.py::installed_ids", "sampo/core.py::claude_path", "sampo/core.py::_cargo_config_files",
              "sampo/py.py::find", "sampo/shot.py::chrome", "sampo/relproof.py::_rust", "sampo/find.py::_tool_env",
              "sampo/models.py::secrets_path", "sampo/fastbuild.py::_account_name", "sampo/tidy.py::memory_dir",
              "sampo/doctor.py::_enabled_plugin_paths", "sampo/doctor.py::_login_fm", "sampo/doctor.py::check_dual_install",
              "sampo/doctor.py::check_file_map", "sampo/doctor.py::check_fm_link", "sampo/doctor.py::check_invisible_text",
              "sampo/doctor.py::check_mod_release", "sampo/doctor.py::check_name_collisions",
              "sampo/doctor.py::check_rule_conflicts", "sampo/doctor.py::check_rules_symlink",
              "sampo/doctor.py::check_sampo_link", "sampo/doctor.py::check_usage_burn", "sampo/doctor.py::cmd_wiring",
              "sampo/doctor.py::instruction_files", "sampo/doctor.py::rule_files", "sampo/doctor.py::run_all",
              "sampo/hooks.py::session_context", "sampo/bench.py::_audit", "sampo/bench.py::_transcript_asks"),
    **_review("read", "asks where a folder is, or what the account's home is, and only checks it (the default state folder's "
              "name, the account database's home, a cache folder under XDG_CACHE_HOME or ~/.cache); nothing is opened for "
              "writing", "sampo/core.py::_default_home", "sampo/prove.py::_unreal_reason", "sampo/map.py::_run_cache"),
    **_review("read", "reads where Claude Code's config folder is (CLAUDE_CONFIG_DIR, or the account's default) to test whether "
              "a lane's path sits under it, which Claude Code treats as sensitive; read-only (T-5889)",
              "sampo/pilotsig.py::_claude_folder"),
    **_review("read", "reads aider's own config files in the account's home and in the git root and cwd, to tell whether a "
              "worker runs a Claude model; read-only, and a config that can't be read counts as Claude (T-5849)",
              "sampo/smith.py::_aider_claude"),
    **_review("read", "reads the Codex and Gemini CLI config files in the account's home (CODEX_HOME moves the Codex one), to "
              "tell whether a worker runs a Claude model; read-only, and a config that can't be read counts as Claude (T-5849)",
              "sampo/smith.py::_cli_config_claude"),
    **_review("read", "sizes the state folders no project owns, the test runner's cache and Claude Code's plugin cache, for "
              "tidy's footprint; sizing only, a removal is tidy --apply's", "sampo/tidy.py::_machine_units"),
    **_review("read", "lists the test runner's cache files (XDG_CACHE_HOME, or the account's cache folder) with the project each "
              "belongs to, for tidy's footprint; listing only", "sampo/tidy.py::_run_cache_rows"),
    **_review("mixed", "the function writes the state folder (FOREMAN_HOME, XDG_STATE_HOME: sandboxed in a test) or a temp "
              "folder it makes; the real home is only read, or it is the temporary HOME the function sets for a child",
              "sampo/cli.py::_attachment", "sampo/cli.py::_sign_receipt", "sampo/cli.py::cmd_audit",
              "sampo/demo.py::cmd_release_check", "sampo/mcp.py::cmd_new",
              "sampo/replay.py::cmd_replay", "sampo/smith.py::install_warning", "sampo/tidy.py::check_global",
              "sampo/hooks.py::_big_read_rewrite"),  # T-5911: expands a Read's path to test it; its writes go to the state folder
    **_review("write", "removes or moves the session file of the id a bench run just kept, under the Claude config folder; "
              "a test points CLAUDE_CONFIG_DIR at a temp folder (test_bench)", "sampo/bench.py::_compactions"),
    **_review("write", "returns the cache folder a benchmark download writes into; real by design (a cache survives a run)",
              "sampo/benchtb.py::cache_dir"),
    **_review("write", "returns the folders sampo link writes the rules into for each agent (Codex, Gemini, OpenCode); "
              "FOREMAN_NO_LINK and CODEX_HOME keep a test out of them", "sampo/agents.py::_config_dirs"),
    **_review("write", "returns the folder the Jeff export trains and writes into; SAMPO_JEFF_HOME or FOREMAN_JEFF_HOME "
              "moves it", "sampo/jeffexport.py::jeff_home"),
    **_review("write", "the plugin-folder migration rewrites settings.json and the plugin files under this folder; "
              "CLAUDE_CONFIG_DIR moves it", "sampo/pluginmove.py::config_dir"),
    **_review("write", "makes the empty 0700 folder ~/.cache/sampo/children that a headless child's fresh cwd goes under, "
              "when XDG_RUNTIME_DIR is not a safe folder of ours; never /tmp, so the real home's cache is the point (T-5706, "
              "T-5997). Nothing else is written there, and a test that reaches it leaves one empty folder",
              "sampo/ideas.py::safe_root"),
    **_review("write", "returns the folder of the systemd user units sampo serve installs; XDG_CONFIG_HOME moves it",
              "sampo/serve.py::unit_dir"),
    **_review("write", "returns Claude Code's settings folder, which sampo install and doctor repair rewrite; "
              "CLAUDE_CONFIG_DIR moves it but only inside the home folder", "sampo/settings.py::claude_dir"),
    **_review("write", "the MAC key is created 0600 in the account's own config folder, found by the password database and "
              "never moved by HOME or an env name (T-3192): a test run makes it once, on purpose",
              "sampo/selfgate.py::_keyfile"),
    **_review("write", "GAP: the install's settings.json, CLAUDE.md, rules link and the fm and sampo links in the real home; "
              "helpers.py does not move HOME, so a test that runs doctor --repair or models through it reaches the real "
              "ones (T-4198, T-4333)", "sampo/setup.py::paths"),
    **_review("write", "GAP: rewrites the real Codex config.toml's model through fix_model (doctor --repair, models) with "
              "no home or env to move it; helpers.py does not move HOME (T-4198, T-4333)", "sampo/modelup.py::codex_newest"),
    **_review("write", "the home the native fast path installs and looks up cargo files in: the account's, never the "
              "caller's $HOME; fastbuild's callers pass home= to a test", "sampo/fastbuild.py::account_home"),
    **_review("write", "expands the checkout a user names (`sampo update --repo PATH`, a ~ as the shell would) or the one this "
              "sampo runs from, and writes that install's own files; it is not state. Every test names a temp checkout "
              "(test_update's --repo and its temp roots)", "sampo/update.py::run"),
}


def _is_write_call(n):
    fn = n.func
    name = fn.attr if isinstance(fn, ast.Attribute) else fn.id if isinstance(fn, ast.Name) else ""
    if name == "open":
        mode = n.args[1] if len(n.args) > 1 else next((k.value for k in n.keywords if k.arg == "mode"), None)
        if mode is None:
            return False
        return not isinstance(mode, ast.Constant) or bool(OPEN_WRITE & set(str(mode.value)))
    if name in SHARED_NAMES:
        base = fn.value if isinstance(fn, ast.Attribute) else None
        return isinstance(base, ast.Name) and base.id in ("os", "shutil")
    return name in WRITE_ATTRS


def _home_use(n, alias):
    """Why a node is a derivation from the real home, or ''. alias: {local name: the name it was imported as}."""
    if isinstance(n, ast.Call):
        fn = n.func
        name = fn.attr if isinstance(fn, ast.Attribute) else alias.get(fn.id, fn.id) if isinstance(fn, ast.Name) else ""
        if name == "expanduser":
            return "expanduser"
        if name == "expandvars" and n.args and isinstance(n.args[0], ast.Constant) and "HOME" in str(n.args[0].value):
            return "expandvars"
        if name == "home" and isinstance(fn, ast.Attribute):
            b = fn.value
            if (isinstance(b, ast.Name) and alias.get(b.id, b.id) == "Path") or (isinstance(b, ast.Attribute) and b.attr == "Path"):
                return "Path.home"
        if name == "getpwuid":
            return "getpwuid"
        if name in ("get", "getenv", "pop") and n.args and isinstance(n.args[0], ast.Constant) and n.args[0].value == "HOME":
            return "HOME"
    if isinstance(n, ast.Subscript) and isinstance(n.ctx, ast.Load) and isinstance(n.slice, ast.Constant) \
            and n.slice.value == "HOME":
        return "HOME"  # a read of the variable: env["HOME"] = tmp sets a child's home, it derives nothing
    return ""


def _is_routed_name(n):
    return (isinstance(n, ast.Constant) and n.value in ROUTED_NAMES) or (isinstance(n, ast.Name) and n.id in ROUTED_NAMES)


def _reads_routed(n):
    """True when a node reads one of the sandbox env names: env.get(NAME), getenv(NAME), env[NAME], NAME in env. A bare
    mention (env.pop(NAME), a scrub list, a keep tuple) routes nothing."""
    if isinstance(n, ast.Call):
        fn = n.func
        name = fn.attr if isinstance(fn, ast.Attribute) else fn.id if isinstance(fn, ast.Name) else ""
        return name in ("get", "getenv") and bool(n.args) and _is_routed_name(n.args[0])
    if isinstance(n, ast.Subscript):
        return isinstance(n.ctx, ast.Load) and _is_routed_name(n.slice)
    if isinstance(n, ast.Compare):
        return _is_routed_name(n.left) and any(isinstance(o, (ast.In, ast.NotIn)) for o in n.ops)
    return False


def home_uses(tree):
    """({(qualified function, use): [line, ...]} of the real-home derivations in one parsed file that are not routed,
    {qualified function: True if it writes}). A function that reads FOREMAN_HOME, XDG_STATE_HOME or FOREMAN_STATE keeps
    the real home as a fallback only: routed, not reported."""
    alias = {a.asname: a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names if a.asname}
    uses, writes, named = {}, {}, {}

    def scope(node, trail):
        here = trail
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            here = trail + [node.name]
        q = ".".join(here) or "<module>"
        why = _home_use(node, alias)
        if why:
            uses.setdefault((q, why), []).append(node.lineno)
        if isinstance(node, ast.Call) and _is_write_call(node):
            writes[q] = True
        if _reads_routed(node):
            named[q] = True
        for ch in ast.iter_child_nodes(node):
            # a decorator and a default belong to the scope around the function, not to it
            outside = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                ch in node.decorator_list or ch in node.args.defaults or ch in node.args.kw_defaults)
            scope(ch, trail if outside else here)

    scope(tree, [])
    return {k: v for k, v in uses.items() if not named.get(k[0])}, writes


def inventory(trees):
    """{(file, function): (expected kind, [uses], [lines])} for {file: parsed tree}. The expected kind is "mixed" when the
    function writes at all, else "read": whether the real home is what it writes to is the reviewer's to say."""
    found = {}
    for f, tree in sorted(trees.items()):
        uses, writes = home_uses(tree)
        for (q, why), lines in uses.items():
            _, seen, at = found.get((f, q), ("", [], []))
            found[(f, q)] = ("mixed" if writes.get(q) else "read", sorted(set(seen) | {why}), at + lines)
    return found


def pins_of(found):
    """{(file, function): (sorted uses, number of derivations)}: what a review looked at."""
    return {k: (tuple(v[1]), len(v[2])) for k, v in found.items()}


def judge(found, reviewed, pins=None):
    """The problems between what the walk found and a reviewed list: [] when they agree. A reviewed "write" stands on
    either expected kind (a helper that returns the path writes nothing itself); "read" and "mixed" must match the walk."""
    out = []
    for key, (expected, uses, lines) in sorted(found.items()):
        f, q = key
        if key not in reviewed:
            out.append(f"{f}:{min(lines)} {q}: {'/'.join(uses)} reaches the real home ({expected}): route it through "
                       f"FOREMAN_HOME or XDG_STATE_HOME, or review it into REVIEWED with a reason")
        elif reviewed[key][0] not in (expected, "write"):
            out.append(f"{f}:{min(lines)} {q}: reviewed as {reviewed[key][0]}, but the walk finds a {expected} now: "
                       f"review it again")
    for key, (expected, uses, lines) in sorted(found.items()) if pins is not None else ():
        if key in reviewed and key in pins and (not set(uses) <= set(pins[key][0]) or len(lines) > pins[key][1]):
            out.append(f"{key[0]}:{min(lines)} {key[1]}: {'/'.join(uses)} x{len(lines)} goes beyond what was reviewed "
                       f"({'/'.join(pins[key][0])} x{pins[key][1]}): review it again, then update PINS")
        elif key in reviewed and key not in pins:
            out.append(f"{key[0]} {key[1]}: reviewed but not pinned: add it to PINS")
    for key in sorted(set(reviewed) - set(found)):
        out.append(f"{key[0]} {key[1]}: reviewed, but no longer reaches the real home: drop it from REVIEWED")
    for key, (kind, reason) in sorted(reviewed.items()):
        if kind not in KINDS or not reason.strip():
            out.append(f"{key[0]} {key[1]}: a kind (read, mixed or write) and a reason are both needed")
    return out


def lib_trees():
    trees = {}
    for root, dirs, files in os.walk(LIB):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in files:
            if name.endswith(".py"):
                path = os.path.join(root, name)
                with open(path, encoding="utf-8") as fh:
                    trees[os.path.relpath(path, LIB).replace(os.sep, "/")] = ast.parse(fh.read(), path)
    return trees


def parsed(**files):
    """{name: tree} for sources given as name=text, the way lib_trees returns them."""
    return {name.replace("__", "/") + ".py": ast.parse(text) for name, text in files.items()}


class StateWriteInventory(unittest.TestCase):
    def test_every_real_home_derivation_in_the_library_is_reviewed(self):
        self.maxDiff = None  # the whole list of findings, not a cut of it
        found = inventory(lib_trees())
        self.assertGreater(len(found), 50, "the walk found about 120: a walk that finds none is broken, not clean")
        self.assertEqual(judge(found, REVIEWED, PINS), [])

    def test_a_synthetic_home_write_is_caught(self):
        # the walk's own proof: each way to reach the real home, written into a made-up module, is found and named
        src = {"fresh": (
            "import os\nfrom pathlib import Path\nfrom os.path import expanduser as eu\n\n"
            "def save(text):\n    path = os.path.join(os.path.expanduser('~'), '.notes')\n"
            "    with open(path, 'w') as f:\n        f.write(text)\n\n"
            "def alias_save():\n    Path(eu('~/x')).write_text('y')\n\n"
            "def lib_save():\n    (Path.home() / 'y').mkdir()\n\n"
            "def env_save():\n    os.makedirs(os.environ['HOME'] + '/z')\n\n"
            "def getenv_save():\n    os.makedirs(os.getenv('HOME'))\n\n"
            "def var_save():\n    os.makedirs(os.path.expandvars('$HOME/w'))\n")}
        found = inventory(parsed(**src))
        self.assertEqual(sorted(q for _, q in found), ["alias_save", "env_save", "getenv_save", "lib_save", "save", "var_save"])
        self.assertEqual({v[0] for v in found.values()}, {"mixed"}, "each one writes")
        problems = judge(found, {})
        self.assertEqual(len(problems), 6, problems)
        self.assertIn("fresh.py:6 save: expanduser reaches the real home (mixed)", problems[-2])

    def test_a_reviewed_entry_is_kept_honest(self):
        src = parsed(fresh="import os\n\ndef peek():\n    return open(os.path.expanduser('~/.rc')).read()\n")
        found = inventory(src)
        self.assertEqual(found, {("fresh.py", "peek"): ("read", ["expanduser"], [4])})
        self.assertEqual(judge(found, {("fresh.py", "peek"): ("read", "only reads the user's rc file")}), [])
        # a read that gained a write: the walk now says mixed, so the old review stands no longer
        grown = parsed(fresh="import os\n\ndef peek():\n    p = os.path.expanduser('~/.rc')\n    os.remove(p)\n")
        again = judge(inventory(grown), {("fresh.py", "peek"): ("read", "only reads")})
        self.assertEqual(len(again), 1, again)
        self.assertIn("reviewed as read, but the walk finds a mixed now", again[0])
        self.assertEqual(judge(inventory(grown), {("fresh.py", "peek"): ("write", "removes the rc on purpose")}), [])
        # stale: reviewed, the derivation gone
        gone = judge({}, {("fresh.py", "peek"): ("read", "only reads")})
        self.assertEqual(len(gone), 1, gone)
        self.assertIn("no longer reaches the real home", gone[0])
        # a bare entry: no reason, and a kind that is not one
        bare = judge(found, {("fresh.py", "peek"): ("read", "  ")})
        self.assertEqual(len(bare), 1, bare)
        self.assertEqual(len(judge(found, {("fresh.py", "peek"): ("maybe", "text")})), 2)

    def test_a_derivation_routed_through_the_sandbox_env_is_not_reported(self):
        routed = parsed(core=(
            "import os\n\ndef foreman_home():\n    h = os.environ.get('FOREMAN_HOME')\n    if h:\n        return h\n"
            "    return os.path.join(os.path.expanduser('~'), '.state')\n\n"
            "def other():\n    return os.path.expanduser('~')\n"))
        self.assertEqual([q for _, q in inventory(routed)], ["other"], "only the unrouted one")
        # a helper's name is not the env's: setup.paths() names foreman_home() and still builds real-home paths
        helper = parsed(setup="import os\n\ndef paths():\n    fh = foreman_home()\n    return os.path.expanduser('~'), fh\n")
        self.assertEqual([q for _, q in inventory(helper)], ["paths"])

    def test_the_walk_finds_a_nested_function_and_a_class_method_by_name(self):
        src = parsed(fresh=(
            "import os\n\nclass Run:\n    def go(self):\n        def inner():\n            return os.path.expanduser('~')\n"
            "        return inner\n"))
        self.assertEqual([q for _, q in inventory(src)], ["Run.go.inner"])

    def test_a_reviewed_function_cannot_gain_a_home_use(self):
        old = parsed(fresh="import os\n\ndef keep():\n    os.makedirs(os.path.expanduser('~/.a'))\n")
        found = inventory(old)
        pins = pins_of(found)
        reviewed = {("fresh.py", "keep"): ("mixed", "writes its own folder")}
        self.assertEqual(judge(found, reviewed, pins), [])
        # a second kind of derivation in the same function
        grown = parsed(fresh="import os\nfrom pathlib import Path\n\ndef keep():\n    os.makedirs(os.path.expanduser('~/.a'))\n"
                       "    (Path.home() / 'x').mkdir()\n")
        again = judge(inventory(grown), reviewed, pins)
        self.assertEqual(len(again), 1, again)
        self.assertIn("Path.home", again[0])
        # a second derivation of the same kind
        twice = parsed(fresh="import os\n\ndef keep():\n    os.makedirs(os.path.expanduser('~/.a'))\n"
                       "    os.makedirs(os.path.expanduser('~/.b'))\n")
        self.assertEqual(len(judge(inventory(twice), reviewed, pins)), 1)
        # a reviewed entry with no pin is a problem too
        self.assertEqual(len(judge(found, reviewed, {})), 1)

    def test_a_mention_of_the_env_name_that_does_not_read_it_is_not_routing(self):
        src = parsed(fresh=(
            "import os\nfrom pathlib import Path\n\ndef scrub():\n    os.environ.pop('FOREMAN_HOME')\n"
            "    Path.home().joinpath('.x').write_text('y')\n\n"
            "def keep():\n    k = ('FOREMAN_HOME', 'PATH')\n    return os.path.expanduser('~'), k\n\n"
            "def routed():\n    return os.environ.get('FOREMAN_HOME') or os.path.expanduser('~')\n\n"
            "def routed_in():\n    if 'FOREMAN_HOME' in os.environ:\n        return os.environ['FOREMAN_HOME']\n"
            "    return os.path.expanduser('~')\n"))
        self.assertEqual(sorted(q for _, q in inventory(src)), ["keep", "scrub"])


if __name__ == "__main__":
    unittest.main()
