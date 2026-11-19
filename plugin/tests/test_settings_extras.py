"""T-1658: T-1544's settings extras. A changelog every set, import, macro, words change and undo writes (fm settings
log), and undo naming a key undone again and again; macros (named changes run as one undo step); two tui.toml files
drawn on the same run side by side (duel); past runs and a stress burst replayed at several sizes, with a change left
unwritten to see what it breaks (replay --set); words to a settings change through claude -p (optional: a stub here),
never proposing a change undone twice."""
import contextlib
import io
import json
import os
import unittest.mock as mock

from helpers import ForemanTestCase, read_text

from sampo import cli
from sampo import smithtui as T
from sampo import ideas
from sampo import settings as ssettings

STUB = """#!/usr/bin/env python3
import json, os, sys
sys.stdin.read()
with open(os.environ["STUB_ARGS"], "w") as f:
    json.dump(sys.argv[1:], f)
print(json.dumps({"type": "result", "result": os.environ["STUB_REPLY"]}))
"""


class _Home(ForemanTestCase):
    """A Foreman home with an empty tui.toml, outside Claude Code."""
    def setUp(self):
        super().setUp()
        self.uhome = os.path.join(self.tmp, "uhome")
        os.makedirs(os.path.join(self.uhome, ".claude"))
        self.toml = os.path.join(self.home, "tui.toml")
        self.env = {"HOME": self.uhome, "CLAUDECODE": "", "CLAUDE_CODE_ENTRYPOINT": ""}
        self.patches = [mock.patch.dict(os.environ, {"HOME": self.uhome}),
                        mock.patch.object(ssettings, "in_claude_code", return_value=False)]
        for x in self.patches:
            x.start()
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        self.put("")

    def tearDown(self):
        for x in reversed(self.patches):
            x.stop()
        super().tearDown()

    def put(self, text, path=None):
        with open(path or self.toml, "w", encoding="utf-8") as f:
            f.write(text)
        return path or self.toml

    def cfg(self):
        return T.load_config(self.toml, profile=False)[0]

    def settings(self, *args, env=None, check=True):
        return self.fm("settings", *args, env=dict(self.env, **(env or {})), check=check)

    def term(self, *args, env=None):
        """fm settings as the user's own terminal runs it (a pty, the Claude Code variables blank): its writes go
        through, where with no terminal the guard decides (T-1572). Its output."""
        import pty
        import subprocess
        import sys
        from helpers import FM, pty_reader
        env = dict(os.environ, FOREMAN_HOME=self.home, FOREMAN_INHIBITED="1", **self.env, **(env or {}))
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        main, sub = pty.openpty()
        reader = pty_reader(main)
        try:
            p = subprocess.run([sys.executable, FM, "settings", *args], cwd=self.repo, env=env, stdin=sub, stdout=sub,
                               stderr=subprocess.PIPE, text=True, timeout=60)
        finally:
            os.close(sub)
        reader.join(10)
        out = bytes(reader.out).decode(errors="replace")
        os.close(main)
        self.assertEqual(p.returncode, 0, f"fm settings {' '.join(args)}\n{out}{p.stderr}")
        return out


class Log(_Home):
    def test_sets_and_undos_are_logged_and_filtered_by_key(self):
        self.put('theme = "paper"\n')
        ssettings.set_key("theme", "riso", surface="app")
        ssettings.set_key("border", "heavy", surface="app")
        ssettings.undo()
        got = [(e["how"], [(c["key"], c["old"], c["new"]) for c in e["changes"]]) for e in ssettings.changelog()]
        self.assertEqual(got, [("set", [("theme", "paper", "riso")]), ("set", [("border", None, "heavy")]),
                               ("undo", [("border", "heavy", None)])])
        self.assertEqual([e["how"] for e in ssettings.changelog("theme")], ["set"])
        out = self.settings("log", "border").stdout
        self.assertIn("set · app border: unset → heavy", out)
        self.assertIn("undo · app border: heavy → unset", out)
        self.assertNotIn("theme", out)
        self.assertEqual(len(json.loads(self.settings("log", "--json").stdout)), 3)

    def test_a_key_set_by_hand_three_times_gets_a_macro_suggested(self):  # T-1876
        for v in ("heavy", "double", "heavy"):
            ssettings.set_key("border", v, surface="app")
        ssettings.set_key("theme", "riso", surface="app")
        tip = "border set by hand 3 times: fm settings macro border --set border=heavy saves it as one command"
        self.assertIn(tip, self.settings("log").stdout)
        self.assertNotIn("theme set by hand", self.settings("log").stdout)
        self.assertNotIn("set by hand", self.settings("log", "theme").stdout)
        self.assertEqual(len(json.loads(self.settings("log", "--json").stdout)), 4)  # the JSON stays the log
        self.settings("macro", "border", "--set", "border=heavy")
        self.assertNotIn("set by hand", self.settings("log").stdout)  # a macro already holds it

    def test_a_key_undone_three_times_is_named_with_where_it_came_from(self):
        for v in ("heavy", "double"):
            ssettings.set_key("border", v, surface="app")
            ssettings.undo()
        self.term("set", "border", "heavy")
        out = self.term("undo")
        self.assertIn("app border: undone 3 times", out)
        self.assertIn("set ×3", out)


class Macro(_Home):
    def test_saved_listed_run_as_one_undo_step(self):
        self.settings("macro", "calm", "--set", "theme=paper", "--set", "border=square")
        self.assertEqual(read_text(self.toml), "", "saving writes no setting")
        self.assertIn("calm: theme=paper border=square", self.settings("macro").stdout)
        self.term("macro", "calm")
        self.assertEqual((self.cfg()["preset"], self.cfg()["border"]), ("paper", "square"))
        self.assertEqual(ssettings.changelog()[-1]["how"], "macro calm")
        self.term("undo")
        self.assertEqual((self.cfg()["preset"], self.cfg()["border"]), (T.DEFAULTS["preset"], T.DEFAULTS["border"]))

    def test_a_bad_name_key_or_value_saves_nothing(self):
        for args in (("calm", "--set", "nope=1"), ("../x", "--set", "theme=paper"), ("calm", "--set", "theme=wiggle"),
                     ("calm", "--set", "theme"), ("ghost",)):
            self.assertNotEqual(self.settings("macro", *args, check=False).returncode, 0, args)
        self.assertEqual(ssettings.macros(), {})


class Duel(_Home):
    def test_two_files_drawn_on_one_run_side_by_side(self):
        a = self.put('border = "heavy"\n', os.path.join(self.tmp, "a.toml"))
        b = self.put('border = "double"\ntheme = "paper"\n', os.path.join(self.tmp, "b.toml"))
        out = self.settings("duel", a, b).stdout
        self.assertIn("━", out)
        self.assertIn("═", out)
        self.assertIn("they differ in: border, theme", out)
        got = json.loads(self.settings("duel", a, b, "--json").stdout)
        self.assertEqual(got["differ"], ["border", "theme"])
        self.assertEqual(self.settings("duel", a, os.path.join(self.tmp, "none.toml"), check=False).returncode, 2)


class Replay(_Home):
    def events(self):
        path = os.path.join(self.tmp, "run.events.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(json.dumps(e) + "\n" for e in T.SAMPLE_RUN)
        return path

    def test_runs_and_a_stress_burst_replay_at_each_size(self):
        got = json.loads(self.settings("replay", "--events", self.events(), "--json").stdout)
        self.assertEqual([r["run"] for r in got["runs"]], ["the sample run", "run.events.jsonl", "a stress burst"])
        for r in got["runs"]:
            self.assertIsNone(r["failure"], r)
            self.assertGreaterEqual(r["frames"], len(ssettings.REPLAY_SIZES))

    def test_a_change_names_what_it_breaks_and_writes_nothing(self):
        draw = T.render

        def render(st, w, h, cfg, *a, **k):
            if cfg.get("border") == "double" and w < 50:  # the 40-wide replay; set's own check draws 60 and 100
                raise IndexError("narrow double border")
            return draw(st, w, h, cfg, *a, **k)
        with mock.patch.object(T, "render", render):
            got = ssettings.replay([("border", "double")], events=[self.events()])
        self.assertTrue(got["runs"] and all(r["breaks"] for r in got["runs"]), got)
        self.assertIn("IndexError", got["runs"][0]["with"]["failure"])
        self.assertEqual(read_text(self.toml), "")
        out = self.settings("replay", "--set", "border=heavy")
        self.assertIn("nothing breaks", out.stdout)
        self.assertNotEqual(self.settings("replay", "--set", "nope=1", check=False).returncode, 0)


class Words(_Home):
    def stub(self, reply):
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir, exist_ok=True)
        with open(os.path.join(bindir, "claude"), "w") as f:
            f.write(STUB)
        os.chmod(os.path.join(bindir, "claude"), 0o755)
        self.args = os.path.join(self.tmp, "args.json")
        return {"PATH": bindir + os.pathsep + os.environ["PATH"], "STUB_REPLY": reply, "STUB_ARGS": self.args}

    def test_proposed_checked_shown_then_applied_as_one_undo(self):
        env = self.stub('Here: {"theme": "paper", "border": "heavy", "nope": 1, "palette": "wiggle"}')
        out = self.settings("words", "a light calm look", env=env).stdout
        self.assertIn("theme: board → paper", out)  # T-3699: board is the default
        self.assertIn("border: rounded → heavy", out)
        self.assertIn("nope", out)
        self.assertIn("palette", out)
        self.assertEqual(read_text(self.toml), "", "shown, not applied")
        self.assertIn("border", " ".join(json.load(open(self.args))), "the settings it may change go to the model")
        self.term("words", "a light calm look", "--apply", env=env)
        self.assertEqual((self.cfg()["preset"], self.cfg()["border"]), ("paper", "heavy"))
        self.assertEqual(ssettings.changelog()[-1]["how"], "words")
        self.term("undo")
        self.assertEqual(self.cfg()["preset"], T.DEFAULTS["preset"])

    def test_without_claude_exits_2_naming_it(self):
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty)
        r = self.settings("words", "calm", env={"PATH": empty}, check=False)
        self.assertEqual(r.returncode, 2)
        self.assertIn("claude", r.stderr)

    def test_a_change_undone_twice_is_never_proposed_again(self):
        for _ in range(2):
            ssettings.set_key("theme", "paper", surface="app")
            ssettings.undo()
        env = self.stub('{"theme": "paper"}')
        out = self.settings("words", "light", "--apply", env=env).stdout
        self.assertIn("undone 2 times", out)
        self.assertIn("theme = paper", " ".join(json.load(open(self.args))), "the model is told not to")
        self.assertEqual(read_text(self.toml), "")


class Review(_Home):
    """What T-1658's review found."""
    def test_a_broken_duel_file_is_refused_not_drawn_as_the_defaults(self):
        a = self.put('border = "heavy"\n', os.path.join(self.tmp, "a.toml"))
        b = self.put("border = \n", os.path.join(self.tmp, "b.toml"))
        r = self.settings("duel", a, b, check=False)
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn("b.toml", r.stderr)

    def test_a_project_scope_set_is_logged(self):
        self.term("set", "border", "heavy", "--scope", "project")
        self.assertEqual([(e["how"], e["changes"][0]["key"], e["changes"][0]["new"]) for e in ssettings.changelog()],
                         [("set --scope project", "border", "heavy")])

    def test_a_run_failing_without_the_change_is_said_not_nothing_breaks(self):
        draw = T.render

        def render(st, w, h, cfg, *a, **k):
            if cfg.get("border") != "heavy":
                raise IndexError("fails as it is")
            return draw(st, w, h, cfg, *a, **k)
        out = io.StringIO()
        with mock.patch.object(T, "render", render), contextlib.redirect_stdout(out):
            rc = cli.main(["settings", "replay", "--set", "border=heavy", "--runs", "0"])
        self.assertEqual(rc, 1)
        self.assertIn("already fail without border=heavy", out.getvalue())
        self.assertNotIn("nothing breaks", out.getvalue())

    def test_a_newline_in_a_value_stays_one_line(self):
        self.assertEqual(ssettings._plain("a\nb"), "a\\nb")

    def test_undo_json_carries_the_hint(self):
        for v in ("heavy", "double"):
            ssettings.set_key("border", v, surface="app")
            ssettings.undo()
        ssettings.set_key("border", "heavy", surface="app")
        self.assertIn("undone 3 times", " ".join(json.loads(self.term("undo", "--json"))["hints"]))

    def test_without_a_terminal_the_guard_decides_macro_and_words_apply(self):
        self.settings("macro", "calm", "--set", "border=heavy")
        self.assertEqual(self.settings("macro", "calm", check=False).returncode, 2)
        env = Words.stub(self, '{"border": "heavy"}')
        self.assertEqual(self.settings("words", "heavy", "--apply", env=env, check=False).returncode, 2)
        self.assertEqual((read_text(self.toml), ssettings.changelog()), ("", []))

    def test_a_child_timeout_doesnt_print_its_prompt(self):
        import subprocess

        class Slow:
            pid = 0

            def communicate(self, *a, **k):
                raise subprocess.TimeoutExpired(["claude", "-p", "THE WHOLE PROMPT"], 180)
        with mock.patch.object(ideas, "_popen", return_value=Slow()), mock.patch.object(ideas, "_reap"), \
                mock.patch.object(ideas.budget, "check"):
            with self.assertRaises(ValueError) as e:
                ideas.run_child("settings words", "THE WHOLE PROMPT", "x", "haiku", 180)
        self.assertNotIn("THE WHOLE PROMPT", str(e.exception))
        self.assertIn("180 s", str(e.exception))
