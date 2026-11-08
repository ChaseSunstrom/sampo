"""fm install-user / uninstall-user on a fake HOME: merge, idempotence, exact reversal, dry run."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

from helpers import FM, ForemanTestCase, read_text, read_json

from sampo import setup

ORIGINAL = {
    "permissions": {"defaultMode": "bypassPermissions", "deny": ["Bash(sudo *)"]},
    "statusLine": {"type": "command", "command": "$HOME/.claude/hooks/statusline-ratelimits.sh", "padding": 1},
    "env": {"KEEP": "1"},
    "theme": "dark",
}


class FmLink(unittest.TestCase):  # T-1333
    def test_the_link_is_made_or_repaired_only_when_it_is_foremans(self):
        import shutil
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        link = os.path.join(d, "bin", "fm")

        def plugin(name):
            path = os.path.join(d, name, "plugin", "bin", "fm")
            os.makedirs(os.path.dirname(path))
            with open(path, "w") as f:
                f.write("#!/bin/sh\n")
            return path
        target, other = plugin("foreman"), plugin("foreman-dev")
        self.assertEqual(setup.ensure_fm_link(link, target), link, "absent: made, its folder too")
        self.assertEqual(os.readlink(link), target)
        self.assertIsNone(setup.ensure_fm_link(link, target), "already right")
        os.remove(link)
        os.symlink(other, link)
        self.assertIsNone(setup.ensure_fm_link(link, target), "a working Foreman link to another checkout stays")
        os.remove(link)
        os.symlink(os.path.join(d, "cache", "foreman", "0.9.1", "plugin", "bin", "fm"), link)
        self.assertEqual(setup.ensure_fm_link(link, target), link, "a Foreman link whose version is gone: re-pointed")
        self.assertEqual(os.readlink(link), target)
        os.remove(link)
        os.symlink("/nonexistent/other-tool/bin/fm", link)
        self.assertIsNone(setup.ensure_fm_link(link, target), "someone else's broken link stays")
        os.remove(link)
        with open(link, "w") as f:
            f.write("#!/bin/sh\n")
        self.assertIsNone(setup.ensure_fm_link(link, target), "a file stays")


class SessionLink(ForemanTestCase):  # T-1333
    def test_session_start_makes_the_link_unless_told_not_to(self):
        link = os.path.join(self.tmp, ".local", "bin", "fm")
        self.hook("SessionStart", {"source": "startup"}, env={"HOME": self.tmp})
        self.assertFalse(os.path.lexists(link), "FOREMAN_NO_LINK (every test) keeps it from the home")
        self.hook("SessionStart", {"source": "startup"}, env={"HOME": self.tmp, "FOREMAN_NO_LINK": ""})
        self.assertTrue(os.path.islink(link))
        self.assertTrue(os.readlink(link).endswith(os.path.join("plugin", "bin", "fm")))


class SetupCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = os.path.realpath(self._tmp.name)
        self.fhome = os.path.join(self.home, ".claude", "foreman")
        os.makedirs(os.path.join(self.fhome, "plugin", "rules"))
        with open(os.path.join(self.fhome, "plugin", "rules", "foreman.md"), "w") as f:
            f.write("# Foreman operating rules\n")
        self.settings = os.path.join(self.home, ".claude", "settings.json")
        with open(self.settings, "w") as f:
            json.dump(ORIGINAL, f, indent=2)
        self.claude_md = os.path.join(self.home, ".claude", "CLAUDE.md")
        with open(self.claude_md, "w") as f:
            f.write("# graphify\n- keep me\n")

    def tearDown(self):
        self._tmp.cleanup()

    def run_fm(self, *args):
        env = dict(os.environ, HOME=self.home, FOREMAN_HOME=self.fhome)
        return subprocess.run([sys.executable, FM, *args], capture_output=True, text=True, env=env, timeout=30)

    def load(self):
        with open(self.settings) as f:
            return json.load(f)

    def manifest_path(self):
        return os.path.join(self.fhome, "state", "install-manifest.json")


class Install(SetupCase):
    OLD_RM = ["Bash(rm -rf /)", "Bash(rm -rf /*)", "Bash(rm -rf ~)", "Bash(rm -rf ~/)", "Bash(rm -rf ~/*)",
              "Bash(rm -rf $HOME)", "Bash(rm -rf $HOME/*)", "Bash(rm -fr /)", "Bash(rm -fr ~)"]

    def test_no_rm_rule_is_installed_and_an_old_install_is_pruned(self):
        # T-3704: CC reads "*" in a rule as a wildcard and asks (bypass-immune) when a deny rule's text sits in a command
        # it can't parse simply, so "Bash(rm -rf /*)" made every `rm -rf /abs/path` prompt
        self.assertFalse([r for r in setup.DENY_RULES if "rm " in r])
        p = self.run_fm("install-user")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertFalse([r for r in self.load()["permissions"]["deny"] if "rm " in r])
        # an old install: our rm rules in deny_added, plus the user's own rm rule
        s = self.load()
        s["permissions"]["deny"] += self.OLD_RM + ["Bash(rm -rf /data)"]
        with open(self.settings, "w") as f:
            json.dump(s, f)
        m = read_json(self.manifest_path())
        m["deny_added"] += self.OLD_RM
        with open(self.manifest_path(), "w") as f:
            json.dump(m, f)
        self.assertEqual(self.run_fm("install-user").returncode, 0)
        deny = self.load()["permissions"]["deny"]
        self.assertIn("Bash(rm -rf /data)", deny)
        self.assertIn("Bash(sudo *)", deny)
        self.assertEqual([r for r in deny if "rm " in r], ["Bash(rm -rf /data)"])
        self.assertFalse(set(self.OLD_RM) & set(read_json(self.manifest_path())["deny_added"]))
        from sampo import doctor
        self.assertEqual(doctor.check_deny_rules(self.load(), read_json(self.manifest_path())).status, "PASS")

    def test_install_wires_everything_and_records_originals(self):
        p = self.run_fm("install-user")
        self.assertEqual(p.returncode, 0, p.stderr)
        s = self.load()
        self.assertEqual(s["statusLine"]["command"], os.path.join(self.fhome, "plugin", "hooks", "statusline"))
        self.assertEqual(s["statusLine"]["padding"], 1)
        for rule in setup.DENY_RULES:
            self.assertIn(rule, s["permissions"]["deny"])
        self.assertIn("Bash(sudo *)", s["permissions"]["deny"])
        self.assertEqual(s["env"]["KEEP"], "1")
        self.assertEqual(s["env"]["CLAUDE_CODE_STOP_HOOK_BLOCK_CAP"], setup.STOP_CAP)
        self.assertEqual(s["env"]["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"], "70", "compact earlier: Foreman state makes it cheap")
        self.assertEqual(s["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"], "200k", "T-1796: a 1M window re-read every reply")
        self.assertEqual((s["theme"], s["permissions"]["defaultMode"]), ("dark", "bypassPermissions"))
        md = read_text(self.claude_md)
        self.assertTrue(md.startswith("# graphify\n- keep me\n"))
        self.assertIn("<!-- foreman:begin -->", md)
        self.assertIn("~/.claude/rules/foreman.md", md)
        link = os.path.join(self.home, ".claude", "rules", "foreman.md")
        self.assertEqual(os.path.realpath(link), os.path.join(self.fhome, "plugin", "rules", "foreman.md"))
        m = read_json(self.manifest_path())
        self.assertEqual(m["statusLine_original"], ORIGINAL["statusLine"])
        self.assertTrue(m["statusline_hud"])
        self.assertTrue(any(b.startswith("settings.json.") for b in os.listdir(os.path.join(self.fhome, "backups"))))

    def test_install_sets_core_fsync_on_sampos_repo_but_not_on_a_dry_run(self):
        # T-3727: a crash must not leave empty git objects; temp repo only
        subprocess.run(["git", "init", "-q", self.fhome], check=True)
        get = lambda: subprocess.run(["git", "-C", self.fhome, "config", "--local", "--get", "core.fsync"],  # noqa: E731
                                     capture_output=True, text=True).stdout.strip()
        self.assertEqual(self.run_fm("install-user", "--dry-run").returncode, 0)
        self.assertEqual(get(), "")
        p = self.run_fm("install-user")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(get(), "objects,reference,index")
        self.assertEqual(self.run_fm("install-user").returncode, 0, "a second run changes nothing")
        self.assertEqual(get(), "objects,reference,index")

    def test_install_no_longer_sets_the_output_style_and_takes_back_its_own(self):
        # T-1279: the rules carry the reply format; the style's per-turn reminder cost 2.7% of the context
        self.run_fm("install-user")
        self.assertNotIn("outputStyle", self.load())
        self.assertNotIn("outputStyle_set", read_json(self.manifest_path()))
        # an older install set it and recorded that
        s = self.load()
        s["outputStyle"] = setup.OUTPUT_STYLE
        with open(self.settings, "w") as f:
            json.dump(s, f)
        m = read_json(self.manifest_path())
        m["outputStyle_set"] = True
        with open(self.manifest_path(), "w") as f:
            json.dump(m, f)
        out = self.run_fm("install-user").stdout
        self.assertNotIn("outputStyle", self.load())
        self.assertIn("rules", out)
        self.assertNotIn("outputStyle_set", read_json(self.manifest_path()))
        # T-2946: the style's older id, stored by an install before the rename, is taken back the same way
        with open(self.settings, "w") as f:
            json.dump(dict(ORIGINAL, outputStyle="foreman:Foreman"), f)
        m["outputStyle_set"] = True
        with open(self.manifest_path(), "w") as f:
            json.dump(m, f)
        self.run_fm("install-user")
        self.assertNotIn("outputStyle", self.load())
        # a style the user chose is never touched, even Foreman's when Foreman did not set it
        for style in ("Concise", setup.OUTPUT_STYLE):
            with open(self.settings, "w") as f:
                json.dump(dict(ORIGINAL, outputStyle=style), f)
            self.run_fm("install-user")
            self.assertEqual(self.load()["outputStyle"], style)
            self.run_fm("uninstall-user")
            self.assertEqual(self.load()["outputStyle"], style)

    def test_uninstall_on_a_bare_home_leaves_no_shell_behind(self):
        # T-0851 (newcomer walkthrough): settings.json was left as {}, an empty rules/ dir stayed, and the report read
        # "permissions.defaultMode = None"
        os.remove(self.settings)
        os.remove(self.claude_md)
        with open(self.settings, "w") as f:
            f.write("{}\n")  # what `claude plugin install` leaves before Foreman's wiring runs
        self.run_fm("install-user")
        out = self.run_fm("uninstall-user").stdout
        self.assertNotIn("None", out)
        self.assertFalse(os.path.exists(self.settings), "an empty settings.json goes")
        self.assertFalse(os.path.isdir(os.path.join(self.home, ".claude", "rules")), "an empty rules/ goes")

    def test_fm_works_from_a_plain_shell(self):
        # T-0853 (newcomer walkthrough): every doc says "run fm …", but fm was on PATH only inside Claude Code
        link = os.path.join(self.home, ".local", "bin", "fm")
        self.run_fm("install-user")
        self.assertEqual(os.path.realpath(link), os.path.realpath(os.path.join(self.fhome, "plugin", "bin", "fm")))
        self.run_fm("uninstall-user")
        self.assertFalse(os.path.lexists(link))
        with open(link, "w") as f:
            f.write("#!/bin/sh\necho mine\n")  # someone else's fm
        self.run_fm("install-user")
        with open(link) as f:
            self.assertIn("mine", f.read(), "a command of the same name that isn't Foreman's stays")

    def test_builder_worktrees_start_from_head(self):
        # T-0803: worktree.baseRef 'fresh' (the default) made both builder lanes start from origin/main, days behind
        self.run_fm("install-user")
        self.assertEqual(self.load()["worktree"], {"baseRef": "head"})
        self.run_fm("uninstall-user")
        self.assertNotIn("worktree", self.load())
        with open(self.settings, "w") as f:
            json.dump(dict(ORIGINAL, worktree={"baseRef": "fresh", "symlinkDirectories": ["node_modules"]}), f)
        self.run_fm("install-user")
        self.assertEqual(self.load()["worktree"]["baseRef"], "fresh", "the user's own choice stays")
        self.run_fm("uninstall-user")
        self.assertEqual(self.load()["worktree"], {"baseRef": "fresh", "symlinkDirectories": ["node_modules"]})

    def test_compact_output_is_a_user_hook_only_while_on(self):
        # T-0783: off costs nothing (no hook at all); on adds one PostToolUse Bash hook; uninstall takes it away
        hooks = lambda: [h for g in (self.load().get("hooks") or {}).get("PostToolUse", []) for h in g["hooks"]
                         if h.get("command", "").endswith("/hooks/compact")]
        self.run_fm("install-user")
        self.assertEqual(hooks(), [])
        self.assertEqual(self.run_fm("compact-output", "on").returncode, 0)
        self.assertEqual(len(hooks()), 1)
        self.run_fm("compact-output", "on")
        self.assertEqual(len(hooks()), 1, "on twice adds it once")
        self.assertIn("on", self.run_fm("compact-output").stdout)
        self.run_fm("compact-output", "off")
        self.assertEqual(hooks(), [])
        self.assertNotIn("hooks", self.load(), "an emptied hooks object goes too")
        self.run_fm("compact-output", "on")
        self.run_fm("uninstall-user")
        self.assertEqual(hooks(), [])

    def test_second_install_is_a_noop(self):
        self.run_fm("install-user")
        s1, md1, m1 = self.load(), read_text(self.claude_md), read_text(self.manifest_path())
        p = self.run_fm("install-user")
        self.assertEqual(p.returncode, 0)
        self.assertEqual((self.load(), read_text(self.claude_md)), (s1, md1))
        self.assertEqual(read_text(self.manifest_path()), m1)
        self.assertIn("already", p.stdout)

    def test_dry_run_changes_nothing(self):
        before = read_text(self.settings)
        p = self.run_fm("install-user", "--dry-run")
        self.assertEqual(p.returncode, 0)
        self.assertIn("statusLine", p.stdout)
        self.assertEqual(read_text(self.settings), before)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".claude", "rules", "foreman.md")))

    def test_refuses_to_replace_a_foreign_rules_file(self):
        rules_dir = os.path.join(self.home, ".claude", "rules")
        os.makedirs(rules_dir)
        with open(os.path.join(rules_dir, "foreman.md"), "w") as f:
            f.write("my own file")
        p = self.run_fm("install-user")
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(read_text(os.path.join(rules_dir, "foreman.md")), "my own file")

    def test_missing_settings_file_is_created(self):
        os.remove(self.settings)
        self.assertEqual(self.run_fm("install-user").returncode, 0)
        self.assertIn("statusLine", self.load())

    def test_record_disabled_plugins_is_additive_and_only_touches_the_manifest(self):
        self.run_fm("install-user")
        before = self.load()
        self.assertEqual(self.run_fm("install-user", "--record-disabled", "ecc@ecc", "--record-disabled", "x@y").returncode, 0)
        self.run_fm("install-user", "--record-disabled", "ecc@ecc")
        self.assertEqual(read_json(self.manifest_path())["plugins_disabled"], ["ecc@ecc", "x@y"])
        self.assertEqual(self.load(), before)


class Uninstall(SetupCase):
    def test_round_trip_restores_originals(self):
        self.run_fm("install-user")
        p = self.run_fm("uninstall-user")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.load(), ORIGINAL)
        self.assertEqual(read_text(self.claude_md), "# graphify\n- keep me\n")
        self.assertFalse(os.path.lexists(os.path.join(self.home, ".claude", "rules", "foreman.md")))
        self.assertFalse(os.path.exists(self.manifest_path()))

    def test_user_changes_after_install_are_kept(self):
        self.run_fm("install-user")
        s = self.load()
        s["permissions"]["deny"].append("Bash(curl *)")
        s["env"]["CLAUDE_CODE_STOP_HOOK_BLOCK_CAP"] = "99"
        with open(self.settings, "w") as f:
            json.dump(s, f)
        self.run_fm("uninstall-user")
        s = self.load()
        self.assertIn("Bash(curl *)", s["permissions"]["deny"])
        self.assertEqual(s["env"]["CLAUDE_CODE_STOP_HOOK_BLOCK_CAP"], "99", "a value the user changed is not ours to remove")

    def test_empty_plugin_maps_left_by_claude_plugin_uninstall_are_dropped(self):
        # uninstall.sh removes the plugin and marketplace first; Claude Code leaves the emptied maps behind.
        self.run_fm("install-user")
        s = self.load()
        s.update(enabledPlugins={}, extraKnownMarketplaces={})
        with open(self.settings, "w") as f:
            json.dump(s, f)
        self.run_fm("uninstall-user")
        self.assertEqual(self.load(), ORIGINAL)

    def test_plugin_maps_with_entries_are_kept(self):
        self.run_fm("install-user")
        s = self.load()
        s["enabledPlugins"] = {"other@x": True}
        with open(self.settings, "w") as f:
            json.dump(s, f)
        self.run_fm("uninstall-user")
        self.assertEqual(self.load()["enabledPlugins"], {"other@x": True})

    def test_users_own_compaction_threshold_is_kept(self):
        s = dict(ORIGINAL, env={"KEEP": "1", "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "50"})
        with open(self.settings, "w") as f:
            json.dump(s, f)
        self.run_fm("install-user")
        self.assertEqual(self.load()["env"]["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"], "50")
        self.run_fm("uninstall-user")
        self.assertEqual(self.load()["env"]["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"], "50")

    def test_restores_recorded_default_mode(self):
        self.run_fm("install-user")
        m = read_json(self.manifest_path())
        m["defaultMode_original"] = "auto"
        with open(self.manifest_path(), "w") as f:
            json.dump(m, f)
        self.run_fm("uninstall-user")
        self.assertEqual(self.load()["permissions"]["defaultMode"], "auto")

    def test_without_manifest_is_harmless(self):
        p = self.run_fm("uninstall-user")
        self.assertEqual(p.returncode, 0)
        self.assertEqual(self.load(), ORIGINAL)


class UninstallScript(SetupCase):
    def test_purge_uses_the_state_dir_in_use(self):
        alt = os.path.join(self.home, "alt-state")
        os.makedirs(os.path.join(alt, "projects"))
        uninstall = os.path.join(os.path.dirname(os.path.dirname(FM)), "uninstall.sh")
        env = dict(os.environ, HOME=self.home, FOREMAN_HOME=self.fhome, FOREMAN_STATE=alt)
        p = subprocess.run(["bash", uninstall, "--dry-run", "--purge-state", "--yes"], capture_output=True, text=True,
                           env=env, timeout=60)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn(alt, p.stdout)
        self.assertTrue(os.path.isdir(alt), "dry run removes nothing")


AGENT_CLIS = ("codex", "gemini", "opencode", "aider")  # T-2176 review: a machine's real agents never leak into these PATHs


class InstallScript(unittest.TestCase):
    """install.sh with stub claude and setup-plugins.sh: plugin-dev (--build-tools) only when building."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = os.path.realpath(self._tmp.name)
        self.fhome = os.path.join(tmp, "foreman")
        os.makedirs(self.fhome)
        subprocess.run(["git", "init", "-q", "-b", "scratch", self.fhome], check=True)
        subprocess.run(["git", "-C", self.fhome, "-c", "user.email=t@example.com", "-c", "user.name=t",
                        "commit", "-q", "--allow-empty", "-m", "init"], check=True)
        repo = os.path.dirname(os.path.dirname(os.path.dirname(FM)))
        with open(os.path.join(repo, "install.sh")) as src, open(os.path.join(self.fhome, "install.sh"), "w") as dst:
            dst.write(src.read())
        self.args_file = os.path.join(tmp, "setup-args")
        stubs = {os.path.join(self.fhome, "setup-plugins.sh"): f'echo "$@" > {self.args_file}\n',
                 os.path.join(tmp, "bin", "claude"): "exit 0\n"}
        for path, body in stubs.items():
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write("#!/usr/bin/env bash\n" + body)
            os.chmod(path, 0o755)
        self.env = dict(os.environ, HOME=tmp, FOREMAN_HOME=self.fhome,
                        PATH=os.path.join(tmp, "bin") + os.pathsep + os.environ["PATH"])

    def tearDown(self):
        self._tmp.cleanup()

    def setup_args(self, *flags):
        p = subprocess.run(["bash", os.path.join(self.fhome, "install.sh"), "--no-bypass", "--no-wiring", *flags],
                           capture_output=True, text=True, env=self.env, stdin=subprocess.DEVNULL, timeout=60)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return read_text(self.args_file).split()

    def mode_after(self, *flags):
        p = subprocess.run(["bash", os.path.join(self.fhome, "install.sh"), "--no-plugins", "--no-wiring", "--no-mod",
                            *flags], capture_output=True, text=True, env=self.env, stdin=subprocess.DEVNULL,
                           timeout=60, start_new_session=True)  # no controlling terminal, as under curl | bash in CI
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        settings = os.path.join(self.env["HOME"], ".claude", "settings.json")
        mode = (read_json(settings).get("permissions") or {}).get("defaultMode") if os.path.exists(settings) else None
        return mode, p.stdout + p.stderr

    def test_bypass_is_asked_for_not_assumed(self):
        # T-0871 (newcomer walkthrough): the install set bypassPermissions silently
        mode, out = self.mode_after()
        self.assertIsNone(mode, "no terminal to ask on: the permission mode stays as it was")
        self.assertIn("--bypass", out, "and the output says how to turn it on")
        self.assertEqual(self.mode_after("--bypass")[0], "bypassPermissions")

    def test_plain_install_skips_build_tools(self):
        self.assertEqual(self.setup_args("--security"), ["--security"])

    def test_build_install_adds_build_tools(self):
        self.assertIn("--build-tools", self.setup_args("--build"))

    def local_models(self, *flags, ollama=True):
        """T-2880: install.sh with a stub `ollama` that logs its calls (never a real pull), or with none on PATH."""
        calls = os.path.join(self.env["HOME"], "ollama-calls")
        env = self.no_claude(skip=("ollama",))
        if ollama:
            with open(os.path.join(self.env["HOME"], "noclaude", "ollama"), "w") as f:
                f.write(f'#!/usr/bin/env bash\necho "ollama $*" >> {calls}\nexit 0\n')
            os.chmod(os.path.join(self.env["HOME"], "noclaude", "ollama"), 0o755)
        p = subprocess.run(["bash", os.path.join(self.fhome, "install.sh"), "--no-plugins", "--no-bypass", "--no-wiring",
                            "--no-mod", *flags], capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL,
                           timeout=60, start_new_session=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return (read_text(calls) if os.path.exists(calls) else ""), p.stdout + p.stderr

    def test_local_models_pulls_each_pinned_embedder(self):
        calls, _ = self.local_models("--local-models")
        self.assertIn("ollama pull bge-m3:567m\n", calls)
        self.assertIn("ollama pull nomic-embed-text:v1.5\n", calls)

    def test_local_models_without_ollama_prints_the_download_url_and_installs_on(self):
        calls, out = self.local_models("--local-models", ollama=False)
        self.assertEqual(calls, "")
        self.assertIn("https://ollama.com/download", out)
        self.assertIn("Sampo is installed", out)

    def test_local_models_does_nothing_without_a_terminal_and_no_flag(self):
        self.assertEqual(self.local_models()[0], "")

    def test_no_local_models_pulls_nothing(self):
        self.assertEqual(self.local_models("--no-local-models")[0], "")

    def test_the_pins_match_the_doctor_s(self):
        from sampo import doctor
        src = read_text(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(FM))), "install.sh"))
        self.assertIn('EMBED_PINS="' + " ".join(doctor.EMBED_PINS) + '"', src)

    def no_claude(self, *agents, skip=()):
        """T-2174: this machine's tools with no claude anywhere on PATH, the running python3 first, and a stub for
        each named agent CLI."""
        farm = os.path.join(self.env["HOME"], "noclaude")
        os.makedirs(farm)
        os.symlink(sys.executable, os.path.join(farm, "python3"))
        for a in agents:
            with open(os.path.join(farm, a), "w") as f:
                f.write("#!/usr/bin/env bash\nexit 0\n")
            os.chmod(os.path.join(farm, a), 0o755)
        for d in os.environ["PATH"].split(os.pathsep):
            for n in (os.listdir(d) if os.path.isdir(d) else []):
                if n != "claude" and n not in AGENT_CLIS and n not in skip and not os.path.lexists(os.path.join(farm, n)):
                    os.symlink(os.path.join(d, n), os.path.join(farm, n))
        repo =os.path.dirname(os.path.dirname(os.path.dirname(FM)))
        os.symlink(os.path.join(repo, "plugin"), os.path.join(self.fhome, "plugin"))
        return dict(self.env, PATH=farm)

    def install_without_claude(self, *agents, flags=()):
        env = self.no_claude(*agents)
        p = subprocess.run(["bash", os.path.join(self.fhome, "install.sh"), *flags], capture_output=True, text=True,
                           env=env, stdin=subprocess.DEVNULL, timeout=120, start_new_session=True)
        return p, p.stdout + p.stderr

    def test_without_claude_code_it_installs_fm_and_wires_the_agents_it_finds(self):
        p, out = self.install_without_claude("codex")
        self.assertEqual(p.returncode, 0, out)
        home = self.env["HOME"]
        link = os.path.join(home, ".local", "bin", "fm")
        self.assertEqual(os.path.realpath(link), os.path.realpath(os.path.join(self.fhome, "plugin", "bin", "fm")))
        self.assertIn("Sampo wired into codex", out)
        self.assertFalse(os.path.exists(os.path.join(home, ".claude", "settings.json")), out)  # no CC wiring
        self.assertFalse(os.path.exists(self.args_file), "the Claude Code plugins aren't set up")

    def test_a_dangling_fm_link_of_foremans_is_repaired_not_fatal(self):
        # T-2176 review: [ -e ] is false for a dangling link, ln -s then failed and set -e ended the install
        link = os.path.join(self.env["HOME"], ".local", "bin", "fm")
        os.makedirs(os.path.dirname(link))
        os.symlink("/gone/foreman/plugin/bin/fm", link)
        p, out = self.install_without_claude("codex")
        self.assertEqual(p.returncode, 0, out)
        self.assertEqual(os.path.realpath(link), os.path.realpath(os.path.join(self.fhome, "plugin", "bin", "fm")))
        self.assertIn("Sampo wired into codex", out)

    def test_without_any_agent_it_points_at_fm_code(self):
        p, out = self.install_without_claude()
        self.assertEqual(p.returncode, 0, out)
        self.assertIn("fm code", out)
        self.assertIn("fm agents install", out)

    def test_build_without_claude_code_says_why_it_cant(self):
        p, out = self.install_without_claude(flags=("--build",))
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("--build", out)
        self.assertIn("Claude Code", out)

    def install_python(self, system):
        """install.sh where python3 is 3.12.3 (Ubuntu 24.04's, too old) until brew or uv installs one; uname says
        `system`. Returns the run and the installer calls made."""
        home = self.env["HOME"]
        mark, calls = os.path.join(home, "installed"), os.path.join(home, "calls")
        stubs = {"python3": f'[ -e {mark} ] && exec {sys.executable} "$@"\necho "Python 3.12.3"; exit 1\n',
                 "uname": f"echo {system}\n",
                 "brew": f'echo "brew $*" >> {calls}; touch {mark}\n',
                 "uv": f'[ "$2" = dir ] && {{ echo {home}/bin; exit; }}\necho "uv $*" >> {calls}; touch {mark}\n'}
        for name, body in stubs.items():
            path = os.path.join(home, "bin", name)
            with open(path, "w") as f:
                f.write("#!/usr/bin/env bash\n" + body)
            os.chmod(path, 0o755)
        p = subprocess.run(["bash", os.path.join(self.fhome, "install.sh"), "--no-plugins", "--no-bypass", "--no-wiring"],
                           capture_output=True, text=True, env=self.env, stdin=subprocess.DEVNULL, timeout=60)
        return p, read_text(calls) if os.path.exists(calls) else ""

    def test_too_old_python_is_replaced_by_uv_on_linux(self):
        p, calls = self.install_python("Linux")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        # update-shell: later shells (and the hooks) find this python3 too, not only the rest of install.sh
        self.assertEqual(calls, "uv python install 3.13 --default\nuv python update-shell\n")

    def test_too_old_python_is_replaced_by_homebrew_on_macos(self):
        p, calls = self.install_python("Darwin")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(calls, "brew install python\n")

    def test_native_windows_is_pointed_at_wsl(self):
        p, calls = self.install_python("MINGW64_NT-10.0-26100")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("WSL", p.stderr)
        self.assertEqual(calls, "")

    def install_desktop(self, system, machine="x86_64"):
        """install.sh --desktop (T-0326) with the GitHub API, the download and hdiutil stubbed; uname says `system`."""
        home = self.env["HOME"]
        release = {"assets": [{"name": n, "browser_download_url": f"https://dl.example/{n}"} for n in
                              ("Foreman_0.1.0_amd64.AppImage", "Foreman_0.1.0_amd64.deb", "Foreman_0.1.0_universal.dmg")]}
        with open(os.path.join(home, "release.json"), "w") as f:
            json.dump(release, f)
        stubs = {"uname": f'[ "$1" = -m ] && {{ echo {machine}; exit; }}\necho {system}\n',
                 # the API answers the release; any other URL is a download, written where -o says
                 "curl": f'out=""; url=""\nwhile [ $# -gt 0 ]; do case "$1" in -o) out="$2"; shift;; -*) ;; *) url="$1";; esac; shift; done\n'
                         f'echo "$url" >> {home}/urls\ncase "$url" in *api.github.com*) cat {home}/release.json;; '
                         f'*) echo "binary of $url" > "${{out:-/dev/stdout}}";; esac\n',
                 "hdiutil": f'echo "hdiutil $*" >> {home}/calls\n[ "$1" = attach ] && mkdir -p "$5/Foreman.app/Contents" '
                            f'&& echo app > "$5/Foreman.app/Contents/x"\nexit 0\n'}
        for name, body in stubs.items():
            path = os.path.join(home, "bin", name)
            with open(path, "w") as f:
                f.write("#!/usr/bin/env bash\n" + body)
            os.chmod(path, 0o755)
        p = subprocess.run(["bash", os.path.join(self.fhome, "install.sh"), "--no-plugins", "--no-bypass", "--no-wiring",
                            "--desktop"], capture_output=True, text=True, env=self.env, stdin=subprocess.DEVNULL, timeout=60)
        return p, home

    def test_desktop_installs_the_appimage_and_a_menu_entry_on_linux(self):
        p, home = self.install_desktop("Linux")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        app = os.path.join(home, ".local", "bin", "foreman-desktop")
        self.assertIn("Foreman_0.1.0_amd64.AppImage", read_text(app))
        self.assertTrue(os.access(app, os.X_OK))
        entry = read_text(os.path.join(home, ".local", "share", "applications", "foreman-desktop.desktop"))
        self.assertIn(f"Exec={app}", entry)
        self.assertIn("ChaseSunstrom/foreman-desktop", read_text(os.path.join(home, "urls")))

    def test_desktop_copies_the_app_from_the_dmg_on_macos(self):
        p, home = self.install_desktop("Darwin", "arm64")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue(os.path.isfile(os.path.join(home, "Applications", "Foreman.app", "Contents", "x")))
        self.assertIn("hdiutil detach", read_text(os.path.join(home, "calls")))

    def test_desktop_says_which_systems_have_builds(self):
        p, _ = self.install_desktop("Linux", "aarch64")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("x86_64", p.stderr)

    def test_fresh_machine_round_trip_leaves_no_settings_file(self):
        repo = os.path.dirname(os.path.dirname(os.path.dirname(FM)))
        os.symlink(os.path.join(repo, "plugin"), os.path.join(self.fhome, "plugin"))
        settings = os.path.join(self.env["HOME"], ".claude", "settings.json")
        self.assertFalse(os.path.exists(settings))
        p = subprocess.run(["bash", os.path.join(self.fhome, "install.sh"), "--no-plugins", "--bypass"],
                           capture_output=True, text=True, env=self.env, stdin=subprocess.DEVNULL, timeout=60)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(read_json(settings)["permissions"]["defaultMode"], "bypassPermissions")  # T-0871: opted in
        p = subprocess.run([sys.executable, FM, "uninstall-user"], capture_output=True, text=True, env=self.env,
                           timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertFalse(os.path.exists(settings), read_text(settings) if os.path.exists(settings) else "")


if __name__ == "__main__":
    unittest.main()


class SetupPluginsInSession(unittest.TestCase):
    """T-0039: inside a Claude Code session the guard can't see the installs in the script's body, so a real run
    refuses there and points at the pinned path (fm ask ID plugin --pin, then fm plugins install)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = os.path.realpath(self._tmp.name)
        self.calls = os.path.join(tmp, "calls")
        os.makedirs(os.path.join(tmp, "bin"))
        with open(os.path.join(tmp, "bin", "claude"), "w") as f:
            f.write(f'#!/usr/bin/env bash\necho "claude $*" >> {self.calls}\n'
                    f'if [ "$2" = list ]; then echo "[]"; fi\nexit 0\n')
        os.chmod(os.path.join(tmp, "bin", "claude"), 0o755)
        self.script = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(FM))), "setup-plugins.sh")
        self.env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        self.env.update(HOME=tmp, PATH=os.path.join(tmp, "bin") + os.pathsep + os.environ["PATH"])

    def tearDown(self):
        self._tmp.cleanup()

    def run_script(self, *args, **env):
        return subprocess.run(["bash", self.script, *args], capture_output=True, text=True, timeout=120,
                              env=dict(self.env, **env), stdin=subprocess.DEVNULL)

    def installs(self):
        return [l for l in (read_text(self.calls) if os.path.exists(self.calls) else "").splitlines()
                if " install " in l or " enable " in l]

    def test_a_real_run_inside_a_session_refuses_and_changes_nothing(self):
        p = self.run_script(CLAUDECODE="1")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("fm plugins install", p.stderr)
        self.assertEqual(self.installs(), [])
        self.assertEqual(self.run_script("--dry-run", CLAUDECODE="1").returncode, 0)

    def test_outside_a_session_it_runs_as_before(self):
        p = self.run_script()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(self.installs(), "the curated plugins are installed")


class NoClaude(ForemanTestCase):
    """T-2174: Foreman with no claude anywhere on PATH (not even the tests' lockout): a task goes from brief to done
    on fm alone, and fm code says how to give it a model instead of reaching for a CLI that isn't there."""

    def setUp(self):
        super().setUp()
        farm = os.path.join(self.tmp, "noclaude")
        os.makedirs(farm)
        os.symlink(sys.executable, os.path.join(farm, "python3"))
        for d in os.environ["PATH"].split(os.pathsep):
            for n in (os.listdir(d) if os.path.isdir(d) else []):
                if n != "claude" and n not in AGENT_CLIS and not os.path.lexists(os.path.join(farm, n)):
                    os.symlink(os.path.join(d, n), os.path.join(farm, n))
        self.env = {"PATH": farm, "HOME": self.tmp}

    def test_a_task_closes_on_evidence_fm_ran(self):
        self.fm("init", env=self.env)
        tid = json.loads(self.fm("task", "new", "add a greeting", "--type", "FIX", "--tier", "S", "--ac",
                                 "greets :: python3 -c pass", "--step", "write it", "--json", env=self.env).stdout)["id"]
        self.fm("task", "set", tid, "--section", "Raw request", "--text", "> add a greeting", env=self.env)
        self.fm("focus", tid, env=self.env)
        self.fm("task", "evidence", tid, "--step", "1", "--run", "python3 -c pass", env=self.env)
        self.fm("task", "evidence", tid, "--ac", "1", "--run", "python3 -c pass", env=self.env)
        self.fm("task", "ac", tid, "check", "1", env=self.env)
        self.fm("task", "set", tid, "--section", "Regression test", "--text", "none: fixture", env=self.env)
        self.fm("task", "finish", tid, "--audit", "self: ok", "--lens", "intent: it greets", env=self.env, timeout=120)
        self.assertEqual(json.loads(self.fm("task", "show", tid, "--json", env=self.env).stdout)["status"], "done")

    def test_doctor_has_no_claude_code_failures_or_wiring_nags(self):
        # T-2174: plugin validate shelled out to a missing claude (FAIL); the ~/.claude wiring checks told you to wire it
        self.fm("init", env=self.env)
        r = self.fm("doctor", "--json", check=False, env=dict(self.env, FOREMAN_DOCTOR_BENCH="0"), timeout=120)
        got = {x["name"]: x for x in json.loads(r.stdout)["results"]}
        for name in ("plugin validate", "statusline", "deny rules", "rules symlink"):
            self.assertEqual(got[name]["status"], "PASS", got[name])
            self.assertIn("no Claude Code here", got[name]["detail"])
        self.assertNotIn("only inside Claude Code", got["fm link"]["detail"])

    def test_fm_code_with_no_model_and_no_cli_says_how_to_add_one(self):
        import socket
        import urllib.parse
        from sampo import models
        for _, url in models.LOCAL:  # a model server running on this machine is a model: fm code would use it
            u = urllib.parse.urlparse(url)
            with socket.socket() as sock:
                sock.settimeout(0.3)
                if sock.connect_ex((u.hostname, u.port)) == 0:
                    self.skipTest(f"a local model server answers at {url}")
        self.fm("init", env=self.env)
        r = self.fm("code", "make it greet", "--stale-ok", "fresh test home", check=False,
                    env=dict(self.env, FOREMAN_MODEL="", **{cl[4]: "" for cl in models.CLOUD}))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no model", (r.stdout + r.stderr).lower())
