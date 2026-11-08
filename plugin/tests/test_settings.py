"""T-1496: one settings schema for both surfaces: the fm code app's tui.toml and the foreman-ui mod's userConfig (kept
by Claude Code in settings.json pluginConfigs). Shared settings share one name and value set; fm settings reads both."""
import json
import os
import unittest

from helpers import ForemanTestCase, read_json, read_text

from sampo import settings

MOD = os.path.join(os.path.dirname(__file__), "..", "..", "mods", "foreman-ui", ".claude-plugin", "plugin.json")


class Schema(unittest.TestCase):
    def test_one_schema_covers_both_surfaces(self):
        rows = settings.schema()
        self.assertEqual({e["surface"] for e in rows}, {"app", "plugin"})
        plugin = {e["key"] for e in rows if e["surface"] == "plugin"}
        self.assertEqual(plugin, set(read_json(MOD)["userConfig"]))
        for e in rows:
            self.assertTrue(e["help"] and e["type"] and "default" in e, e)

    def test_motion_and_palette_share_one_name_and_value_set(self):
        cfg = read_json(MOD)["userConfig"]
        for name, shared in settings.SHARED.items():
            entries = [e for e in settings.schema() if e["key"] == name]
            self.assertEqual({e["surface"] for e in entries}, {"app", "plugin"}, name)
            for e in entries:
                self.assertEqual(tuple(e["choices"]), shared["choices"], (name, e["surface"]))
            # the old words stay listed after them: Claude Code drops a stored value its options don't name
            self.assertEqual(tuple(cfg[name]["options"]), shared["choices"] + tuple(shared["aliases"]),
                             f"plugin.json {name} options")
            self.assertIn(cfg[name]["default"], shared["choices"])

    def test_old_values_are_still_accepted(self):
        import tempfile
        from sampo import smithtui
        self.assertEqual(settings.canon("motion", "calm"), "reduced", "the mod's old word")
        path = os.path.join(tempfile.mkdtemp(), "tui.toml")
        for text, want in (('motion = "calm"\n', ("motion", "reduced")), ('theme = "signal"\n', ("preset", "signal")),
                           ('palette = "cb"\n', ("palette", "cb"))):
            with open(path, "w") as f:
                f.write(text)
            cfg, errors = smithtui.load_config(path)
            self.assertEqual((errors, cfg[want[0]]), ([], want[1]), text)
        with open(path, "w") as f:
            f.write('palette = "cb"\n')
        self.assertEqual(smithtui.load_config(path)[0]["theme"]["ok"], smithtui._theme(smithtui.PALETTES["signal"])["ok"],
                         "cb on night is the signal colours")
        with open(path, "w") as f:
            f.write('theme = "paper"\npalette = "cb"\n')
        cfg, errors = smithtui.load_config(path)
        ok, bad = (smithtui._rgb(cfg["theme"][n]["fg"]) for n in ("ok", "bad"))
        self.assertEqual(errors, [])
        self.assertGreater(ok[2], ok[0], "pass is blue on paper too")
        self.assertGreater(bad[0], bad[2], "fail is orange")
        for n in ("ok", "bad", "warn", "accent"):
            self.assertGreaterEqual(smithtui.contrast(cfg["theme"][n]["fg"], cfg["theme"]["screen"]["bg"]), 3, n)


class Guard(unittest.TestCase):
    def test_a_builder_lane_writes_nothing_outside_its_worktree(self):
        import tempfile
        import types
        import unittest.mock as mock
        from sampo import core
        lane, out = tempfile.mkdtemp(), os.path.join(tempfile.mkdtemp(), "settings.json")
        p, b = types.SimpleNamespace(lane=lane), types.SimpleNamespace(id="T-1", meta={"builder": True})
        with mock.patch.object(core, "load_briefs", return_value=[]), \
                mock.patch.object(core, "active_brief", return_value=b), \
                mock.patch.object(core, "read_meta", return_value={}), \
                mock.patch.object(core, "main_worktree", return_value="/nonexistent-main"):
            self.assertIsNotNone(settings._guard_block(out, p))
            self.assertIsNone(settings._guard_block(os.path.join(lane, "settings.json"), p))
        self.assertIsNone(settings._guard_block(out, None), "outside a lane the same write is ordinary")


class Command(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.uhome = os.path.join(self.tmp, "uhome")
        os.makedirs(os.path.join(self.uhome, ".claude"))
        self.settings = os.path.join(self.uhome, ".claude", "settings.json")
        self.before = json.dumps({"model": "x", "pluginConfigs": {"sampo-ui@sampo": {"options": {"motion": "calm"}},
                                                                   "other@m": {"options": {"a": 1}}}}, indent=2)
        with open(self.settings, "w") as f:
            f.write(self.before)
        with open(os.path.join(self.home, "tui.toml"), "w") as f:
            f.write('# mine\nborder = "heavy"\n')
        self.env = {"HOME": self.uhome, "CLAUDECODE": "", "CLAUDE_CODE_ENTRYPOINT": ""}  # the user's own terminal
        self.claude = dict(self.env, CLAUDECODE="1")  # inside a Claude Code session: the guard decides

    def term(self, *args):
        """fm run as the user's own terminal runs it: stdin and stdout a pty, the Claude Code variables blank (T-1572:
        with no terminal the guard decides, whatever the variables say)."""
        import pty
        import subprocess
        import sys
        from helpers import FM, pty_reader
        env = dict(os.environ, FOREMAN_HOME=self.home, FOREMAN_INHIBITED="1", **self.env)
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        main, sub = pty.openpty()
        reader = pty_reader(main)
        try:
            p = subprocess.run([sys.executable, FM, *args], cwd=self.repo, env=env, stdin=sub, stdout=sub,
                               stderr=subprocess.PIPE, text=True, timeout=30)
        finally:
            os.close(sub)
        reader.join(10)
        out = bytes(reader.out)
        os.close(main)
        self.assertEqual(p.returncode, 0, f"fm {' '.join(args)}\n{out.decode(errors='replace')}{p.stderr}")
        return out.decode(errors="replace")

    def test_blank_claude_variables_with_no_terminal_still_go_through_the_guard(self):
        p = self.fm("settings", "set", "mascot", "orange", env=self.env, check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("guard", p.stdout + p.stderr)
        self.assertEqual(read_text(self.settings), self.before, "settings.json untouched")

    def test_list_shows_user_bindings_and_what_the_app_cant_read(self):
        with open(os.path.join(self.home, "tui.toml"), "w") as f:
            f.write('theme = ["x"]\n[keys]\nx = "stop"\n')  # x: a key the defaults leave unbound
        by = {(r["surface"], r["key"]): r for r in json.loads(self.fm("settings", "list", "--json", env=self.env).stdout)}
        self.assertEqual((by["app", "keys.x"]["value"], by["app", "keys.x"]["default"]), ("stop", "none"))
        text = self.fm("settings", "list", env=self.env).stdout
        self.assertIn("keys.x", text)
        self.assertIn("theme = ['x']", text, "the bad setting is named, not hidden behind the default")
        self.assertIn("stop", self.fm("settings", "get", "keys.x", env=self.env).stdout)
        self.term("settings", "set", "theme", "paper")  # repairs a file the app chokes on
        self.assertIn('theme = "paper"', read_text(os.path.join(self.home, "tui.toml")))

    def test_inside_claude_code_tui_toml_needs_the_guard(self):
        path = os.path.join(self.home, "tui.toml")
        p = self.fm("settings", "set", "theme", "paper", env=self.claude, check=False)
        self.assertEqual(p.returncode, 2)
        self.assertIn(path, p.stdout + p.stderr)
        self.assertIn('theme = "paper"', p.stdout + p.stderr, "the exact change to make")
        self.assertEqual(read_text(path), '# mine\nborder = "heavy"\n')

    def test_a_config_dir_outside_home_is_refused(self):
        other = os.path.join(self.tmp, "cc")
        os.makedirs(other)
        with open(os.path.join(other, "settings.json"), "w") as f:
            f.write(self.before)
        p = self.fm("settings", "set", "palette", "cb", "--surface", "plugin", env=dict(self.env, CLAUDE_CONFIG_DIR=other),
                    check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(read_text(os.path.join(other, "settings.json")), self.before)

    def test_list_shows_both_surfaces_with_value_default_and_where(self):
        rows = json.loads(self.fm("settings", "list", "--json", env=self.env).stdout)
        by = {(r["surface"], r["key"]): r for r in rows}
        self.assertEqual((by["app", "border"]["value"], by["app", "border"]["default"]), ("heavy", "rounded"))
        self.assertEqual(by["app", "border"]["where"], os.path.join(self.home, "tui.toml"))
        self.assertEqual((by["plugin", "motion"]["value"], by["plugin", "motion"]["default"]), ("reduced", "full"))
        self.assertIn(self.settings, by["plugin", "motion"]["where"])
        self.assertIn("sampo-ui@sampo", by["plugin", "motion"]["where"])
        self.assertEqual(by["plugin", "mascot"]["value"], "sampo", "unset: the default (T-3113: the crest)")
        text = self.fm("settings", "list", env=self.env).stdout
        self.assertIn("app", text)
        self.assertIn("plugin", text)
        self.assertIn("heavy", text)

    def test_list_keys_or_layout_shows_that_group_alone(self):
        """T-1511: /sampo:settings keys and layout show the bindings and the panel layout through fm settings list."""
        keys = self.fm("settings", "list", "keys", env=self.env).stdout
        self.assertIn('keys.d', keys)
        self.assertIn('"diff"', keys)
        self.assertNotIn("border", keys)
        self.assertNotIn("layout.", keys)
        rows = json.loads(self.fm("settings", "list", "layout", "--json", env=self.env).stdout)
        self.assertEqual({r["group"] for r in rows}, {"layout"})
        self.assertIn("layout.columns", {r["key"] for r in rows})
        p = self.fm("settings", "list", "nope", env=self.env, check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("keys", p.stdout + p.stderr, "the groups it takes are named")
        self.assertIn("list: a group", " ".join(self.fm("settings", "--help", env=self.env).stdout.split()))
        body = read_text(os.path.join(PluginSurface.ROOT, "plugin", "commands", "settings.md"))
        for cmd in ("sampo settings list keys", "sampo settings list layout", "sampo smith runners --check"):
            self.assertIn(f"`{cmd}", body, cmd)

    def test_get_shows_each_surface(self):
        out = self.fm("settings", "get", "motion", env=self.env).stdout
        self.assertIn("app", out)
        self.assertIn("plugin", out)
        self.assertIn("reduced", out)
        self.assertNotEqual(self.fm("settings", "get", "nosuch", env=self.env, check=False).returncode, 0)

    def test_an_app_set_writes_tui_toml_in_place(self):
        self.term("settings", "set", "theme", "paper")
        self.assertEqual(read_text(os.path.join(self.home, "tui.toml")), '# mine\nborder = "heavy"\ntheme = "paper"\n')

    def test_a_plugin_set_the_guard_protects_prints_the_change_and_writes_nothing(self):
        p = self.fm("settings", "set", "mascot", "orange", env=self.claude, check=False)
        self.assertEqual(p.returncode, 2)
        said = p.stdout + p.stderr
        for words in (self.settings, "pluginConfigs", "sampo-ui@sampo", '"mascot": "orange"'):
            self.assertIn(words, said)
        self.assertEqual(read_text(self.settings), self.before)

    def test_in_the_users_terminal_settings_json_is_written(self):
        self.term("settings", "set", "mascot", "orange")
        got = read_json(self.settings)
        self.assertEqual(got["pluginConfigs"]["sampo-ui@sampo"]["options"], {"motion": "calm", "mascot": "orange"})
        self.assertEqual((got["model"], got["pluginConfigs"]["other@m"]), ("x", {"options": {"a": 1}}))

    def test_a_plugin_set_the_guard_allows_keeps_the_rest_of_settings(self):
        other = os.path.join(self.uhome, "cc")  # a config folder of the user's the guard doesn't protect
        os.makedirs(other)
        with open(os.path.join(other, "settings.json"), "w") as f:
            f.write(self.before)
        self.fm("settings", "set", "palette", "cb", "--surface", "plugin", env=dict(self.claude, CLAUDE_CONFIG_DIR=other))
        got = read_json(os.path.join(other, "settings.json"))
        self.assertEqual(got["pluginConfigs"]["sampo-ui@sampo"]["options"], {"motion": "calm", "palette": "cb"})
        self.assertEqual((got["model"], got["pluginConfigs"]["other@m"]), ("x", {"options": {"a": 1}}))

    def test_a_shared_set_writes_what_it_may_and_says_what_the_rest_needs(self):
        other = os.path.join(self.uhome, "cc")
        os.makedirs(other)
        p = self.fm("settings", "set", "motion", "off", env=dict(self.claude, CLAUDE_CONFIG_DIR=other), check=False)
        self.assertEqual(p.returncode, 2, "the app half was refused")
        self.assertIn('motion = "off"', p.stdout + p.stderr)
        self.assertEqual(read_text(os.path.join(self.home, "tui.toml")), '# mine\nborder = "heavy"\n')
        self.assertEqual(read_json(os.path.join(other, "settings.json"))["pluginConfigs"]["sampo-ui@sampo"],
                         {"options": {"motion": "off"}})
        self.term("settings", "set", "motion", "off")  # the terminal: both
        self.assertIn('motion = "off"', read_text(os.path.join(self.home, "tui.toml")))
        self.assertEqual(read_json(self.settings)["pluginConfigs"]["sampo-ui@sampo"]["options"]["motion"], "off")

    def test_a_bad_plugin_value_is_refused(self):
        for key, value in (("freshAt", "500"), ("freshAt", "lots"), ("mascot", "pink"), ("motion", "wobbly")):
            p = self.fm("settings", "set", key, value, "--surface", "plugin", env=self.env, check=False)
            self.assertNotEqual(p.returncode, 0, (key, value))
        self.assertEqual(read_text(self.settings), self.before)


class History(ForemanTestCase):
    """T-1510: fm settings diff (the keys the files change from the defaults, dead ones flagged), export/import through
    a share code (the changed keys only, every one checked before any is written) and undo (the history, backwards)."""
    term = Command.term

    def setUp(self):
        super().setUp()
        import unittest.mock as mock
        self.uhome = os.path.join(self.tmp, "uhome")
        os.makedirs(os.path.join(self.uhome, ".claude"))
        self.settings = os.path.join(self.uhome, ".claude", "settings.json")
        self.env = {"HOME": self.uhome, "CLAUDECODE": "", "CLAUDE_CODE_ENTRYPOINT": ""}
        self.toml = os.path.join(self.home, "tui.toml")
        self.patches = [mock.patch.dict(os.environ, {"HOME": self.uhome}),
                        mock.patch.object(settings, "in_claude_code", return_value=False)]
        for x in self.patches:
            x.start()
        os.environ.pop("CLAUDE_CONFIG_DIR", None)

    def tearDown(self):
        for x in reversed(self.patches):  # before the base restores the environment it set
            x.stop()
        super().tearDown()

    def write(self, toml, options):
        with open(self.toml, "w", encoding="utf-8") as f:
            f.write(toml)
        with open(self.settings, "w", encoding="utf-8") as f:
            json.dump({"model": "x", "pluginConfigs": {"sampo-ui@sampo": {"options": options}}}, f)

    def options(self):
        return read_json(self.settings)["pluginConfigs"]["sampo-ui@sampo"]["options"]

    def test_diff_shows_changed_keys_and_flags_dead_ones(self):
        import base64
        self.write('theme = "paper"\nmotion = "full"\nfoo = 1\n[colors]\nbogus = 3\nok = "#4DA3FF"\n[keys]\nz = "stop"\n',
                   {"motion": "calm", "old": 1, "freshAt": 40})
        got = {(r["surface"], r["key"]): r for r in settings.diff()}
        self.assertEqual({k: (r["value"], r.get("dead", False)) for k, r in got.items()},
                         {("app", "theme"): ("paper", False), ("app", "keys.z"): ("stop", False),
                          ("plugin", "motion"): ("reduced", False), ("app", "foo"): (1, True),
                          ("app", "colors.bogus"): (3, True), ("plugin", "old"): (1, True),
                          ("app", "colors.ok"): ("#4DA3FF", False)},
                         "motion = full and freshAt = 40 are the defaults; the theme's colours aren't keys of the file")
        self.assertEqual(got["app", "theme"]["default"], "night")
        self.assertTrue(got["app", "colors.ok"].get("skipped"), "too faint on paper: the app skips it")
        code = settings.share_code()
        shared = json.loads(base64.urlsafe_b64decode(code + "=" * (-len(code) % 4)))
        self.assertEqual(shared, {"app": {"theme": "paper", "keys.z": "stop"}, "plugin": {"motion": "reduced"}},
                         "neither dead nor skipped keys are shared")

    def test_export_and_import_round_trip_through_a_share_code(self):
        self.write('border = "heavy"\nnope = 1\n[colors]\nok = "#47C98C"\n[layout]\nwidths = [60, 40]\n',
                   {"mascot": "orange", "palette": "cb"})
        code = settings.share_code()
        self.assertRegex(code, r"^[A-Za-z0-9_-]+$", "one pasteable word")
        want = [r for r in settings.diff() if not r.get("dead")]
        self.write("# theirs\n", {})
        done, refused, same = settings.import_code(code, only="plugin")
        self.assertEqual(([k for _, k, _, _ in done], refused, read_text(self.toml)), (["mascot", "palette"], [], "# theirs\n"))
        done, refused, same = settings.import_code(code)
        self.assertEqual((refused, len(same)), ([], 2), "the plugin's two are already so")
        self.assertEqual(len(done), 3)
        self.assertEqual(settings.diff(), want, "the changed keys, and only those")
        text = read_text(self.toml)
        self.assertTrue(text.startswith("# theirs\n") and "nope" not in text, text)
        self.assertEqual(read_json(self.settings)["model"], "x", "the rest of settings.json kept")
        done, refused, same = settings.import_code(code)
        self.assertEqual((done, len(same)), ([], 5), "a second import writes nothing")
        self.assertEqual(read_text(self.toml), text)

    def test_a_bad_share_code_is_refused_whole_and_names_the_key(self):
        import base64
        self.write('# mine\nborder = "heavy"\n', {"motion": "calm"})
        enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
        for code, says in (("!!!", "share code"), (enc([1]), "share code"), ("x" * 40000, "long"),
                           (base64.urlsafe_b64encode(b"\xff\xfe").decode(), "share code"),
                           (enc({"app": {"border": "double", "nope": 1}}), "nope"),
                           (enc({"app": {"border": "wavy"}}), "border"),
                           (enc({"app": {"border": None}}), "border"),
                           (enc({"app": {"colors.ok": [[[[[[[[1]]]]]]]]}}), "colors.ok"),
                           (enc({"plugin": {"freshAt": 500}}), "freshAt"),
                           (enc({"plugin": {"mascot": {"a": 1}}}), "mascot"),
                           (enc({"elsewhere": {"x": 1}}), "elsewhere"),
                           (enc({"app": {"keys.\x1b[2J": "quit"}, "\x1b]0;x\x07": {}}), "\\x1b"),
                           ("W" * 30 + "[" * 4000, "share code"),
                           (base64.urlsafe_b64encode(b"[" * 20000).decode(), "share code"),
                           (base64.urlsafe_b64encode(b'{"plugin": {"freshAt": NaN}}').decode(), "share code")):
            with self.subTest(code=code[:40]), self.assertRaises(settings.SettingError) as e:
                settings.import_code(code)
            self.assertIn(says, str(e.exception))
            self.assertNotIn("\x1b", str(e.exception), "a code's text never reaches the terminal raw")
        self.assertEqual(read_text(self.toml), '# mine\nborder = "heavy"\n', "the good key in a bad code isn't written")
        self.assertEqual(self.options(), {"motion": "calm"})
        with self.assertRaises(settings.SettingError):
            settings.undo()  # nothing was recorded

    def test_each_key_is_checked_as_it_would_land(self):
        import base64
        self.write("", {})
        enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode()
        with self.assertRaises(settings.SettingError) as e:  # a dark grey on night: too faint, at its turn
            settings.import_code(enc({"app": {"colors.ok": "#2B3442", "theme": "paper"}}))
        self.assertIn("colors.ok", str(e.exception))
        self.assertEqual(read_text(self.toml), "")
        done, refused, _ = settings.import_code(enc({"app": {"theme": "paper", "colors.ok": "#2B3442"}}))
        self.assertEqual((len(done), refused), (2, []))

    def test_undo_restores_the_previous_value(self):
        mine = '# mine\nborder = "heavy"\n'
        self.write(mine, {"motion": "calm"})
        settings.set_key("border", "double")
        settings.set_key("theme", "paper")
        settings.set_key("mascot", "orange", "plugin")
        settings.set_key("motion", "off")  # both surfaces: one change, one undo
        self.assertEqual(self.options(), {"motion": "off", "mascot": "orange"})
        settings.undo()
        self.assertEqual(self.options(), {"motion": "reduced", "mascot": "orange"})
        self.assertNotIn("motion", read_text(self.toml), "it wasn't set: undone, it is gone again")
        settings.undo()
        self.assertEqual(self.options(), {"motion": "reduced"})
        settings.undo()
        self.assertEqual(read_text(self.toml), '# mine\nborder = "double"\n')
        settings.undo()
        self.assertEqual(read_text(self.toml), mine)
        with self.assertRaises(settings.SettingError):
            settings.undo()
        code = settings.share_code()
        self.write("", {})
        settings.import_code(code)
        settings.undo()  # an import is one change
        self.assertEqual((read_text(self.toml), self.options()), ("", {}))

    def test_undo_leaves_a_hand_edit_and_a_guarded_file_alone(self):
        import unittest.mock as mock
        self.write('border = "heavy"\n', {})
        settings.set_key("border", "double")
        self.write('border = "ascii"\n', {})  # by hand, after
        done, refused = settings.undo()
        self.assertEqual(done, [])
        self.assertIn("changed since", refused[0])
        self.assertEqual(read_text(self.toml), 'border = "ascii"\n')
        with self.assertRaises(settings.SettingError):
            settings.undo()  # dropped: nothing left to undo
        settings.set_key("mascot", "orange", "plugin")
        with mock.patch.object(settings, "in_claude_code", return_value=True), \
                mock.patch.object(settings, "_guard_block", return_value="a protected file"):
            done, refused = settings.undo()
            self.assertIn("guard", refused[0])
        self.assertEqual(self.options(), {"mascot": "orange"}, "refused: kept for the next undo")
        self.assertEqual(len(settings.undo()[0]), 1)
        self.assertEqual(self.options(), {})

    def test_no_message_prints_raw_escapes(self):
        import base64
        self.write("", {})
        code = base64.urlsafe_b64encode(json.dumps({"app": {"colors.ok": {"\x1b]0;x\x07": True}}}).encode()).decode()
        with self.assertRaises(settings.SettingError) as e:
            settings.import_code(code)
        done, refused = settings.set_key("colors.ok", '{ "\\u001b[2J" = true }')
        p = self.fm("settings", "import", code, env=self.env, check=False)
        self.assertNotEqual(p.returncode, 0)
        for said in (str(e.exception), refused[0], p.stdout + p.stderr):
            self.assertIn("colors.ok", said)
            self.assertFalse({"\x1b", "\x07"} & set(said), said)

    def test_a_file_the_app_chokes_on_never_blocks_the_set_that_repairs_it(self):
        import unittest.mock as mock
        from sampo import smithtui
        real = smithtui.load_config

        def load(path=None):
            try:
                with open(path, encoding="utf-8") as f:
                    broken = 'heights = "BROKEN"' in f.read()
            except (OSError, TypeError):
                broken = False
            if broken:
                raise TypeError("a layout it chokes on")
            return real(path)
        self.write('[layout]\nheights = "BROKEN"\n', {})
        with mock.patch.object(smithtui, "load_config", side_effect=load):
            problems = []
            settings.diff(problems)
            self.assertIn("a layout it chokes on", str(problems))
            settings.share_code()
            done, refused = settings.set_key("layout.heights", '[[1], ["auto", "auto", "auto", 1]]')
            self.assertEqual((len(done), refused), (1, []))
            done, refused = settings.undo()
            self.assertEqual((len(done), refused), (1, []))
        self.assertNotIn("heights", read_text(self.toml), "unread by the app: undone, it goes rather than comes back")

    def test_undo_says_a_skipped_value_is_removed(self):
        self.write('theme = ["x"]  # mine\n', {})
        settings.set_key("theme", "paper")
        done, refused = settings.undo()
        self.assertEqual(refused, [])
        self.assertIn("skipped", done[0][4])
        self.assertNotIn("theme", read_text(self.toml))

    def test_history_trouble_never_fails_a_written_change(self):
        import contextlib
        import io
        import unittest.mock as mock
        self.write('border = "heavy"\n', {})
        err = io.StringIO()
        with mock.patch.object(settings, "_keep", side_effect=OSError("disk full")), contextlib.redirect_stderr(err):
            done, refused = settings.set_key("border", "double")
        self.assertEqual((len(done), refused, read_text(self.toml)), (1, [], 'border = "double"\n'))
        self.assertIn("disk full", err.getvalue())
        settings.set_key("border", "ascii")
        path = settings._history_path()
        kept = read_text(path)
        os.chmod(path, 0)
        try:
            if os.access(path, os.R_OK):
                self.skipTest("root reads any file")
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(len(settings.set_key("border", "rounded")[0]), 1)
            with self.assertRaises(settings.SettingError):
                settings.undo()
        finally:
            os.chmod(path, 0o600)
        self.assertEqual(read_text(path), kept, "an unreadable history is never taken for an empty one")

    def test_a_key_named_twice_in_one_code_is_one_change(self):
        import base64
        self.write("", {})
        code = base64.urlsafe_b64encode(json.dumps({"app": {"keys.enter": "quit", "keys.ENTER": "help"}}).encode())
        done, refused, _ = settings.import_code(code.decode())
        self.assertEqual(([v for _, _, v, _ in done], refused), (["help"], []))
        done, refused = settings.undo()
        self.assertEqual((len(done), refused), (1, []))
        self.assertNotIn("enter", read_text(self.toml))

    def test_diff_json_keeps_its_problems(self):
        import argparse
        import contextlib
        import io
        import unittest.mock as mock
        self.write('border = "heavy"\n', {"motion": "calm"})
        out = io.StringIO()
        with mock.patch.object(settings, "mod_manifest", return_value={}), contextlib.redirect_stdout(out):
            settings.cmd_settings(argparse.Namespace(action="diff", json=True, surface=None, key=None, value=None))
        got = json.loads(out.getvalue())
        self.assertEqual([r["key"] for r in got["changed"]], ["border"])
        self.assertIn("plugin.json", json.dumps(got["problems"]))

    def test_the_verbs_from_fm_and_from_claude_code(self):
        self.term("settings", "set", "border", "double")
        self.assertIn("border", self.term("settings", "diff"))
        code = self.term("settings", "export").strip()
        self.write("", {})
        self.assertIn("border", self.term("settings", "import", code))
        self.assertIn('border = "double"', read_text(self.toml))
        self.term("settings", "undo")
        self.assertEqual(read_text(self.toml), "")
        body = read_text(os.path.join(PluginSurface.ROOT, "plugin", "commands", "settings.md"))
        for cmd in ("sampo settings diff", "sampo settings export", "sampo settings import 'CODE'", "sampo settings undo"):
            self.assertIn(f"`{cmd}", body, cmd)
        self.assertIn("`^[A-Za-z0-9_-]+$`", body, "a pasted code runs only when it is one share-code word")


class PreviewApp(unittest.TestCase):
    """T-1573: the fm code app's preview refuses what set_app refuses, the guard's refusal too, and writes nothing."""
    def test_it_refuses_what_set_app_refuses_and_writes_nothing(self):
        import tempfile
        import unittest.mock as mock
        path, mine = os.path.join(tempfile.mkdtemp(), "tui.toml"), '# mine\ntheme = "night"\n'
        with open(path, "w", encoding="utf-8") as f:
            f.write(mine)
        bad = (("theme", "nope"), ("colors.ok", '"\udcff"'), ("colors.ok", {"fg": "\udcff"}), ("nope", "x"))
        with mock.patch.object(settings, "in_claude_code", return_value=False):
            for key, value in bad:
                for fn in (settings.set_app, settings.preview_app):
                    with self.subTest(fn=fn.__name__, key=key, value=value), self.assertRaises(settings.SettingError):
                        fn(key, value, path)
            value, cfg = settings.preview_app("motion", "calm", path)
            self.assertEqual((value, cfg["motion"], cfg["preset"]), ("reduced", "reduced", "night"))
        with mock.patch.object(settings, "in_claude_code", return_value=True), \
                mock.patch.object(settings, "_guard_block", return_value="a protected file"):
            for fn in (settings.set_app, settings.preview_app):
                with self.subTest(fn=fn.__name__), self.assertRaises(settings.SettingError) as e:
                    fn("theme", "paper", path)
                self.assertIn("guard", str(e.exception))
        self.assertEqual(read_text(path), mine, "nothing written")


class Unknown(ForemanTestCase):
    """T-1723: set_key refuses a key no surface has, so fm code's :set line and fm settings serve say so. Its own
    FOREMAN_HOME (T-3222: it locked the real state's settings, found when run.py --sandbox made that read-only)."""
    def test_an_unknown_key_is_refused_by_name(self):
        done, refused = settings.set_key("no.such.setting", "1")
        self.assertEqual(done, [])
        self.assertTrue(refused and "no.such.setting" in refused[0] and "no such setting" in refused[0], refused)
        done, refused = settings.set_key("motion", "calm", surface="nowhere")
        self.assertEqual(done, [])
        self.assertIn("nowhere", refused[0])


class PluginSurface(unittest.TestCase):
    """T-1574: /sampo:settings runs fm settings (one implementation); the pane re-reads its options on each poll."""
    ROOT = os.path.join(os.path.dirname(__file__), "..", "..")

    def test_the_command_lists_and_sets_through_fm_settings(self):
        text = read_text(os.path.join(self.ROOT, "plugin", "commands", "settings.md"))
        head, _, body = text.partition("\n---\n")
        self.assertTrue(head.startswith("---\n") and "\ndescription: " in head, "frontmatter with a description")
        self.assertIn("disable-model-invocation: true", head, "the person's command: no share of the skill listing")
        for cmd in ("sampo settings list", "sampo settings get", "sampo settings set"):
            self.assertIn(f"`{cmd}", body, cmd)
        self.assertIn("$ARGUMENTS", body)

    def test_the_pane_applies_changed_options_on_its_poll(self):
        mod = os.path.join(self.ROOT, "mods", "foreman-ui")
        src = read_text(os.path.join(mod, "hooks", "register.tsx"))
        # a wiring check only: the behaviour's proof is the mod's own test, run by claude plugin test mods/foreman-ui
        poll = src[src.index("async function pollOptions("):src.index("async function tick(")]
        tick = src[src.index("async function tick("):src.index("async function gauge(")]
        self.assertIn("$.config.list()", poll, "the engine's current option values")
        self.assertIn("applyOptions(", poll)
        self.assertIn("pollOptions($)", tick, "read on every 2 s poll")
        tests = read_text(os.path.join(mod, "tests", "foreman-ui.test.tsx"))
        self.assertIn("applies on the next poll, no restart (T-1574)", tests, "claude plugin test runs the behaviour")


if __name__ == "__main__":
    unittest.main()
