"""T-1283: Foreman Code's terminal UI. It reads fm code's --json event stream and nothing else, so the stream carries
everything a live view draws; the views are pure functions of the state those events build, tested without a terminal."""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import unittest.mock

from helpers import PLUGIN, ForemanTestCase
from test_code import CodeCase, call, plan

from sampo import core as c


class Events(CodeCase):
    def events(self, r):
        return [json.loads(line) for line in r.stdout.splitlines() if line.strip()]

    def test_the_event_stream_has_what_a_live_view_draws(self):
        self.serve("openai", plan(), {"text": "Reading it first.", "calls": [("read", {"path": "calc.py"})]},
                   call("edit", file="calc.py", old="a - b", new="a + b"), call("done", summary="adds now"))
        ev = self.events(self.code("fix add", "--model", "fake", "--json", "--no-commit"))
        kinds = [e["event"] for e in ev]
        self.assertEqual((ev[0]["event"], ev[0]["schema"]), ("start", 1), kinds)
        self.assertEqual(ev[0]["model"], "fake")
        self.assertIn("tokens", kinds)
        tok = [e for e in ev if e["event"] == "tokens"][-1]
        self.assertGreater(tok["total_in"], 0)
        self.assertTrue(any("Reading it first." in e["text"] for e in ev if e["event"] == "say"), kinds)
        read = next(e for e in ev if e["event"] == "action" and e["action"] == "read")
        self.assertIn("return a - b", read["detail"], "the action's result, for an expandable log line")
        self.assertIn("calc.py", next(e for e in ev if e["event"] == "changed")["files"])
        failed = next(e for e in ev if e["event"] == "verify" and e["exit"])
        self.assertIn("AssertionError", failed["output"], "the baseline run's failure, to show under it")
        self.assertEqual(kinds[-1], "closed")

    def test_plain_output_is_unchanged_by_the_new_events(self):
        self.serve("openai", plan(), call("edit", file="calc.py", old="a - b", new="a + b"), call("done", summary="x"))
        out = self.code("fix add", "--model", "fake", "--no-commit").stdout
        for word in ("call_in", "schema", '"event"', "Reading"):
            self.assertNotIn(word, out)

    def test_an_error_is_an_event_too(self):
        r = self.code("fix add", "--model", "nosuch", "--json", check=False)
        self.assertEqual(r.returncode, 1)
        last = self.events(r)[-1]
        self.assertEqual(last["event"], "error")
        self.assertIn("nosuch", last["text"])


RUN = [  # what one fm code --json run streams, shortened
    {"event": "start", "text": "", "schema": 1, "request": "fix add", "model": "fake", "worker": None},
    {"event": "plan", "text": "plan", "plan": {"type": "FIX", "tier": "S", "title": "Make calc.add add",
                                               "criteria": [{"text": "calc.add adds", "verify": "python3 test_calc.py"}],
                                               "steps": ["write the failing test", "fix calc.add"]}},
    {"event": "task", "text": "task T-0001 created and focused", "task": "T-0001"},
    {"event": "verify", "text": "before 1", "criterion": 1, "label": "before", "exit": 1, "cmd": "python3 test_calc.py",
     "output": "AssertionError: -1"},
    {"event": "step", "text": "step 1/2", "step": 1},
    {"event": "say", "text": "Reading calc.py first."},
    {"event": "tokens", "text": "", "call_in": 1200, "call_out": 80, "total_in": 1200, "total_out": 80},
    {"event": "action", "text": "", "action": "read", "target": "calc.py", "result": "ok", "detail": "1 def add"},
    {"event": "action", "text": "", "action": "run", "target": "rm -rf ~", "result": "refused",
     "detail": "refused: Foreman guard: rm of your home"},
    {"event": "step", "text": "step 2/2", "step": 2},
    {"event": "action", "text": "", "action": "edit", "target": "calc.py", "result": "ok", "detail": "edited"},
    {"event": "changed", "text": "", "step": 2, "files": ["calc.py"]},
    {"event": "verify", "text": "verify 1", "criterion": 1, "label": "verify", "exit": 0, "cmd": "python3 test_calc.py"},
    {"event": "closed", "text": "T-0001 closed; committed abc1234", "task": "T-0001", "tokens": [1200, 80]},
]


def tui():
    from sampo import smithtui
    return smithtui


class Core(unittest.TestCase):
    def state(self, events=RUN):
        st = tui().State()
        for e in events:
            st.apply(e)
        return st

    def test_the_state_follows_the_run(self):
        st = self.state()
        self.assertEqual((st.task, st.status, st.step, st.tokens), ("T-0001", "closed", 2, [1200, 80]))
        self.assertEqual(st.criteria[0]["state"], "pass")
        self.assertEqual([a["result"] for a in st.actions], ["ok", "refused", "ok"])
        self.assertEqual(st.files, {2: ["calc.py"]})

    def test_the_main_view_draws_every_station(self):
        canvas = tui().render(self.state(), 110, 32, tui().DEFAULTS)
        text = canvas.text()
        for words in ("Make calc.add add", "FIX S", "calc.add adds", "write the failing test", "fix calc.add",
                      "Reading calc.py first.", "rm -rf ~", "calc.py", "T-0001", "closed", "1.2k"):
            self.assertIn(words, text)
        self.assertEqual(canvas.style_of("rm -rf ~"), "bad", "a guard refusal stands out")
        self.assertEqual(len(text.splitlines()), 32)
        self.assertTrue(all(len(line) <= 110 for line in text.splitlines()))

    def test_a_narrow_terminal_gets_one_column(self):
        text = tui().render(self.state(), 66, 24, tui().DEFAULTS).text()
        self.assertTrue(all(len(line) <= 66 for line in text.splitlines()))
        self.assertIn("rm -rf ~", text, "the activity log still shows")
        self.assertIn("calc.add adds", text, "and the checks above it")

    def test_the_run_reads_as_pictures(self):
        app = tui().App(tui().DEFAULTS, spawn=lambda *a, **k: None)
        for e in RUN + [{"event": "changed", "text": "", "step": 2, "files": ["calc.py"], "stat": {"calc.py": [3, 1]}}]:
            app.feed(e)
        cv = tui().render(app.state, 110, 32, tui().DEFAULTS, app)
        self.assertEqual(cv.text().splitlines()[2].count("✓"), 5, "plan, both steps, the checks and the close: done")
        self.assertEqual(cv.style_of(" ✓ "), "chip_steel")  # T-2876: a done station has cooled
        self.assertEqual(cv.style_of("PASS"), "chip_ok")
        self.assertIn("▆▆", cv.text(), "the changed file's lines as a bar")
        self.assertNotIn("Reading calc.py first.", cv.text(), "the model's own words wait behind t")
        app.key("t")
        self.assertIn("Reading calc.py first.", tui().render(app.state, 110, 32, tui().DEFAULTS, app).text())

    def test_time_and_tokens_are_charts(self):
        st = self.state([dict(e, ts=1000 + 10 * i) for i, e in enumerate(RUN)])
        self.assertEqual([s[0] for s in tui().spans(st)], ["plan", "step 1", "step 2", "checks"])
        text = tui().render(st, 110, 32, tui().DEFAULTS).text()
        self.assertIn("2m10s", text, "the run's length on the Time panel")
        top, floor = tui().columns([1, 2, 4], 20, 2)
        self.assertEqual((top.split(), floor.split()), (["██████"], ["▄▄▄▄▄▄", "██████", "██████"]),
                         "each call a column, the biggest the chart's full height")

    def test_a_phase_stamped_before_the_run_stays_on_the_chart(self):
        # T-1922: its bar's padding was " " * (width - start) with start far below 0: gigabytes, killed on macOS
        evs = [dict(e, ts=1000 + 10 * i) for i, e in enumerate(RUN)]
        evs[next(i for i, e in enumerate(evs) if e["event"] == "step")]["ts"] = 1000 - 10 ** 6
        for line in tui()._time(self.state(evs), 80, 12, None):
            self.assertLessEqual(sum(len(t) for t, _ in line), 80)

    def test_partial_runs_and_unknown_events_never_break_the_view(self):
        for n in range(len(RUN) + 1):
            tui().render(self.state(RUN[:n]), 80, 24, tui().DEFAULTS)
        st = self.state(RUN + [{"event": "from-the-future", "text": "?"}, {"no": "event"}])
        self.assertEqual(st.status, "closed")
        with self.assertRaises(ValueError):  # a newer stream than this reader knows
            tui().State().apply({"event": "start", "text": "", "schema": 99})

    def test_keys_run_commands(self):
        app = tui().App(tui().DEFAULTS, spawn=lambda *a, **k: None)
        app.key("?")
        self.assertEqual(app.mode, "help")
        app.key("?")
        self.assertEqual(app.mode, "main")
        app.key("n")
        self.assertEqual(app.mode, "prompt")
        for ch in "fix add":
            app.key(ch)
        started = []
        app.spawn = lambda words, opts: started.append((words, opts))
        app.key("\n")
        self.assertEqual(started, [(["fix add"], {})])
        app.key("q")
        self.assertTrue(app.done)


DIFF = """diff --git a/deploy.py b/deploy.py
--- a/deploy.py
+++ b/deploy.py
@@ -21,6 +21,9 @@ def main(argv=None):
     args = parse(argv)
+    if args.dry_run:
+        print("\\n".join(args.files))
+        return 0
     upload(args.files, args.bucket)
-    return upload_done()
"""


class Keys(unittest.TestCase):
    """T-1348: q or s during a live run asks for a second press within 2 s; the ? poster names the CLI command behind
    each key."""

    def live(self):
        app = tui().App(tui().DEFAULTS, spawn=lambda *a, **k: None)
        app.child = type("Child", (), {"poll": lambda self: None})()  # a run going (no pid: nothing to signal)
        app.state.status = "running"
        now, stopped = [100.0], []
        app.clock, app.stop = (lambda: now[0]), (lambda: stopped.append(1))
        app.commands["stop"] = app.stop
        return app, now, stopped

    def test_q_or_s_during_a_live_run_needs_a_second_press(self):
        app, now, _ = self.live()
        app.key("q")
        self.assertFalse(app.done)
        self.assertIn("q again", app.message)
        now[0] += 1
        app.key("q")
        self.assertTrue(app.done)
        app, now, stopped = self.live()
        app.key("s")
        now[0] += 3  # too late: it asks again
        app.key("s")
        self.assertEqual(stopped, [])
        app.key("s")
        self.assertEqual(stopped, [1])
        app, now, _ = self.live()
        app.key("q")
        app.key("j")  # another key in between: start over
        app.key("q")
        self.assertFalse(app.done)

    def test_a_launch_tip_names_a_command_never_used_and_learns(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "code-keys.json")
            with open(path, "w") as f:
                json.dump(["new", "resume", "diff"], f)
            app = tui().App(tui().DEFAULTS, spawn=lambda *a, **k: None, used=path)
            self.assertEqual(app.message, "tip: h past runs")  # the first in the poster's order not used yet
            app.key("t")
            app.key("q")
            with open(path) as f:
                self.assertEqual(set(json.load(f)), {"new", "resume", "diff", "thoughts", "quit"})
        self.assertEqual(tui().App(tui().DEFAULTS, spawn=lambda *a, **k: None).message, "")  # no store: no tip

    def test_with_no_run_going_q_quits_at_once(self):
        app = tui().App(tui().DEFAULTS, spawn=lambda *a, **k: None)
        app.key("q")
        self.assertTrue(app.done)

    def test_the_keys_poster_names_the_cli_command_behind_a_key(self):
        app = tui().App(tui().DEFAULTS, spawn=lambda *a, **k: None)
        app.key("?")
        text = tui().render(app.state, 140, 44, tui().DEFAULTS, app).text()
        for cli in ("fm code --resume", "fm settings set motion off", "git diff"):
            self.assertIn(cli, text)


class WideLayout(unittest.TestCase):
    """T-1347: 160 columns or more add a third column with the Plan and Check output panels."""

    def test_a_wide_terminal_adds_plan_and_check_output(self):
        app = tui().App(tui().DEFAULTS, spawn=lambda *a, **k: None)
        for e in RUN:
            app.feed(e)
        wide = tui().render(app.state, 170, 44, tui().DEFAULTS, app).text()
        self.assertIn("╭─ Plan ", wide)
        self.assertIn("╭─ Check output ", wide)
        self.assertIn("Activity", wide)
        narrow = tui().render(app.state, 150, 44, tui().DEFAULTS, app).text()
        self.assertNotIn("╭─ Plan ", narrow)


class DiffView(unittest.TestCase):
    """T-1312: the diff view reveals its lines on open (all at once with motion reduced or off) and marks the words a
    changed line changed."""

    def test_changed_words_are_marked_on_a_paired_line(self):
        marks = tui().intraline(["@@ -1 +1 @@", "-    return upload_done()", "+    return upload_done(files)", " x"])
        self.assertEqual(set(marks), {2}, "the removed line lost nothing; the added one gained files")
        line = "+    return upload_done(files)"
        self.assertEqual(["".join(line[1:][a:b] for a, b in marks[2])], ["files"])
        marks = tui().intraline(["-a = old_name(1)", "+a = new_name(1)"])
        self.assertEqual(("-a = old_name(1)"[1:][slice(*marks[0][0])], "+a = new_name(1)"[1:][slice(*marks[1][0])]),
                         ("old_name", "new_name"))

    def test_lines_reveal_over_time_and_at_once_without_motion(self):
        lines = [f"+line {i}" for i in range(60)]
        for motion, first in (("full", True), ("off", False)):
            app = tui().App(dict(tui().DEFAULTS, motion=motion), spawn=lambda *a, **k: None, diff=lambda: "\n".join(lines))
            now = [10.0]
            app.clock = lambda: now[0]
            app.key("d")
            text = tui().render(app.state, 120, 40, app.cfg, app).text()
            self.assertEqual("+line 20" not in text, first, motion)
            now[0] += 2
            self.assertIn("+line 20", tui().render(app.state, 120, 40, app.cfg, app).text())


class Themes(unittest.TestCase):
    """T-1311: every theme side by side on a sample run."""

    def test_the_gallery_shows_every_theme(self):
        out = tui().gallery(130, 18, depth=24)
        for name in tui().PRESETS:
            self.assertIn(name, out)
        self.assertIn("\x1b[", out, "in colour")
        plain = tui().gallery(130, 18, depth=0)
        self.assertNotIn("\x1b[", plain)
        self.assertTrue(all(len(line) <= 130 for line in plain.splitlines()), "fits the width it was given")


class Forge(unittest.TestCase):
    """T-2876: the TUI wears the illustrated forge (design/sampo/tokens.json's night and light themes): night and paper
    read their colours from the tokens, running work glows hot and finished work cools to steel, the banner says SAMPO
    CODE; signal, riso and the cb/riso inks stay exactly as they were (the baseline), and classic is the old night."""
    BASE = os.path.join(PLUGIN, "tests", "fixtures", "tui-baseline.json")
    TOKENS = os.path.join(os.path.dirname(PLUGIN), "design", "sampo", "tokens.json")

    def base(self):
        with open(self.BASE, encoding="utf-8") as f:
            return json.load(f)

    def tokens(self, theme):
        with open(self.TOKENS, encoding="utf-8") as f:
            return {k: v["$value"].upper() for k, v in json.load(f)[theme]["color"].items() if isinstance(v, dict)}

    def test_signal_riso_and_the_inks_are_the_baseline(self):
        b, t = self.base(), tui()
        self.assertEqual(t.PALETTES["signal"], b["signal"])
        self.assertEqual(t.PALETTES["riso"], b["riso"])
        self.assertEqual((t.CB, t.RISO), (b["cb_inks"], b["riso_inks"]))

    def test_classic_is_the_old_night_and_a_preset(self):
        t = tui()
        self.assertEqual(t.PALETTES["classic"], self.base()["classic"])
        self.assertIn("classic", t.PRESETS)

    def test_night_and_paper_read_the_forge_tokens(self):
        t, night, day = tui(), self.tokens("dark"), self.tokens("light")
        n, p = t.PALETTES["night"], t.PALETTES["paper"]
        self.assertEqual((n["bg"], n["ink"], n["dim"], n["accent"], n["ok"], n["bad"], n["warn"]),
                         (night["surface"], night["text"], night["text-muted"], night["glow"], night["success"],
                          night["error"], night["warning"]))
        self.assertEqual((p["bg"], p["ink"], p["dim"]), (day["surface"], day["text"], day["text-muted"]))

    def test_running_is_hot_and_done_is_steel_and_they_part_on_256_colours(self):
        t = tui()
        n = t.PALETTES["night"]
        self.assertEqual(t.NODE["now"][1], "hot")
        self.assertEqual((t.NODE["done"][1], t.LINK["done"][1]), ("steel", "steel"))
        hot, steel = t._theme(n)["hot"]["fg"], t._theme(n)["steel"]["fg"]
        self.assertNotEqual(t.to_256(hot), t.to_256(steel))
        lum = lambda h: 0.2126 * int(h[1:3], 16) + 0.7152 * int(h[3:5], 16) + 0.0722 * int(h[5:7], 16)
        self.assertGreaterEqual(abs(lum(hot) - lum(steel)), 20, (hot, steel))
        for name in ("night", "paper"):  # what's still to come reads as a graphic (3:1), on both grounds
            pal = t.PALETTES[name]
            self.assertGreaterEqual(t.contrast(t._theme(pal)["todo"]["fg"], pal["bg"]), 3, name)

    def load(self, text):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "tui.toml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return tui().load_config(path, env={}, profile=False)

    def test_an_ink_over_night_or_paper_is_the_look_it_was(self):  # T-2876 audit: exactly as before, ground and all
        t, b = tui(), self.base()
        self.assertEqual(t.preset_theme("night", "cb"), t._theme(dict(b["classic"], **b["cb_inks"]["night"])))
        self.assertEqual(t.preset_theme("paper", "riso"), t._theme(dict(b["classic_paper"], **b["riso_inks"]["paper"])))
        self.assertIn("classic_paper", t.PRESETS)

    def test_a_saved_accent_or_ok_reaches_running_and_finished_marks(self):  # T-2876 audit
        cfg, errors = self.load('theme = "classic"\n[colors]\naccent = "#FFAA00"\nok = "#33CC66"\n')
        self.assertEqual(errors, [])
        th = cfg["theme"]
        self.assertEqual((th["hot"]["fg"], th["steel"]["fg"], th["chip_steel"]["bg"]), ("#FFAA00", "#33CC66", "#33CC66"))
        cfg, _ = self.load('theme = "night"\n[colors]\nok = "#33CC66"\n')  # the forge's own steel stays its own
        self.assertEqual(cfg["theme"]["steel"]["fg"], tui().PALETTES["night"]["steel"])

    def test_the_banner_says_sampo_code(self):
        src = open(os.path.join(PLUGIN, "lib", "sampo", "smithtui.py"), encoding="utf-8").read()
        self.assertIn("SAMPO CODE", src)
        self.assertNotIn("FOREMAN CODE", src)

    def test_no_string_a_user_sees_says_foreman(self):
        """The TUI's and statusline's string literals (docstrings too), and the mod's quoted strings, say Sampo. Env
        names (FOREMAN_*) and the regexes that still accept the old name from older hooks stay."""
        import ast
        import re
        for path in (os.path.join(PLUGIN, "lib", "sampo", "smithtui.py"), os.path.join(PLUGIN, "hooks", "statusline")):
            with open(path, encoding="utf-8") as f:
                tree = ast.parse(f.read())
            said = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
            self.assertEqual([s for s in said if re.search(r"\bForeman\b|FOREMAN CODE", s)], [], path)
        mod = os.path.join(os.path.dirname(PLUGIN), "mods", "foreman-ui", "hooks")
        for name in sorted(os.listdir(mod)):
            with open(os.path.join(mod, name), encoding="utf-8") as f:
                for n, line in enumerate(f, 1):
                    code = line.split("//")[0] if "://" not in line else line
                    for lit in re.findall(r"'[^']*'|\"[^\"]*\"|`[^`]*`", code):
                        self.assertNotRegex(lit, r"\bForeman\b", f"{name}:{n}")


class Navigation(unittest.TestCase):
    """T-1313: / filters the Activity log; the bottom bar's hints follow the focused panel."""

    def app(self):
        app = tui().App(tui().DEFAULTS, spawn=lambda *a, **k: None)
        for e in RUN:
            app.feed(e)
        return app

    def test_slash_filters_the_activity_log_and_esc_clears_it(self):
        app = self.app()
        app.key("/")
        self.assertEqual(app.mode, "search")
        for ch in "calc":
            app.key(ch)
        app.key("\n")
        self.assertEqual((app.mode, app.search), ("main", "calc"))
        text = tui().render(app.state, 140, 44, tui().DEFAULTS, app).text()
        self.assertIn("matching calc", text)
        self.assertNotIn("Reading calc.py first", text.replace("calc.py", "CALC"), "only lines that match stay")
        self.assertNotIn("rm -rf ~", text)
        app.key("/")
        app.key("\x1b")
        self.assertEqual((app.mode, app.search), ("main", ""))
        self.assertIn("rm -rf ~", tui().render(app.state, 140, 44, tui().DEFAULTS, app).text())

    def test_the_hints_name_the_focused_panels_keys(self):
        app = self.app()
        tui().render(app.state, 140, 44, tui().DEFAULTS, app)  # the first frame sets the panel order
        app.focus = app.order.index("log")
        self.assertIn("/ search  ↵ open", tui().render(app.state, 140, 44, tui().DEFAULTS, app).text())
        app.focus = app.order.index("files")
        text = tui().render(app.state, 140, 44, tui().DEFAULTS, app).text()
        self.assertIn("d the diff", text)
        self.assertNotIn("/ search  ↵ open", text, "only the focused panel shows its keys")


class LineEditor(unittest.TestCase):
    """T-1346: the request box is a line editor; / names an app command; Tab after @ completes a tracked file; a
    preview line says where the request goes."""

    def ed(self, *keys, text=""):
        e = tui().LineEditor(history=["first ask", "second ask"])
        e.insert(text)
        for k in keys:
            e.key(k)
        return e

    def test_the_readline_keys(self):
        e = self.ed("\x01", "X", text="abc")  # Ctrl-A, then type at the start
        self.assertEqual((e.text, e.cur), ("Xabc", 1))
        e = self.ed("KEY_LEFT", "KEY_LEFT", "\x0b", text="hello")  # Ctrl-K kills to the end
        self.assertEqual(e.text, "hel")
        e = self.ed("\x17", text="fix the add")  # Ctrl-W: the word before the cursor
        self.assertEqual(e.text, "fix the ")
        e = self.ed("KEY_LEFT", "\x15", text="abc")  # Ctrl-U: everything before the cursor
        self.assertEqual((e.text, e.cur), ("c", 0))
        e = self.ed("\x01", "\x05", "!", text="ab")  # Ctrl-E
        self.assertEqual(e.text, "ab!")
        e = self.ed("KEY_HOME", "KEY_DC", text="ab")
        self.assertEqual(e.text, "b")

    def test_history_walks_up_and_back_to_the_draft(self):
        e = self.ed("KEY_UP", text="draft")
        self.assertEqual(e.text, "second ask")
        e.key("KEY_UP")
        self.assertEqual(e.text, "first ask")
        e.key("KEY_UP")
        self.assertEqual(e.text, "first ask", "stays at the oldest")
        e.key("KEY_DOWN")
        e.key("KEY_DOWN")
        self.assertEqual((e.text, e.cur), ("draft", 5))

    def test_the_app_prompt_uses_it_and_remembers_requests(self):
        started = []
        app = tui().App(tui().DEFAULTS, spawn=lambda *a, **k: None)
        app.start = lambda words, **kw: started.append(words)
        app.key("n")
        for k in ["a", "d", "d", "KEY_HOME", "f", "i", "x", " ", "\n"]:
            app.key(k)
        self.assertEqual(started, [["fix add"]])
        app.key("n")
        app.key("KEY_UP")
        self.assertEqual(app.input, "fix add")

    def test_a_slash_request_runs_an_app_command(self):
        app = tui().App(tui().DEFAULTS, spawn=lambda *a, **k: None)
        app.start = lambda *a, **k: self.fail("a /command is not a run")
        app.key("n")
        for k in "/sett":
            app.key(k)
        self.assertIn("settings", tui().render(app.state, 120, 30, tui().DEFAULTS, app).text())
        app.key("\n")
        self.assertEqual(app.mode, "settings")

    def test_tab_after_at_completes_a_tracked_file_and_the_preview_names_the_runner(self):
        app = tui().App(tui().DEFAULTS, spawn=lambda *a, **k: None, opts={"model": "fake"})
        app.tracked = lambda: ["plugin/lib/fmcore.py", "plugin/lib/fmcli.py", "README.md"]
        app.key("n")
        for k in "fix @plugin/lib/fmco":
            app.key(k)
        app.key("\t")
        self.assertEqual(app.input, "fix @plugin/lib/fmcore.py ")
        app.key("x")
        self.assertIn("Enter starts a run in", tui().render(app.state, 120, 30, tui().DEFAULTS, app).text())
        self.assertIn("with fake", tui().render(app.state, 120, 30, tui().DEFAULTS, app).text())


class Waiting(unittest.TestCase):
    """T-1310: the terminal title names the run's state, a bell rings once when the run is blocked or the guard
    refuses, and a model silent past 10 s is named as what the run waits on."""

    def test_the_title_names_the_state_in_plain_text(self):
        st = tui().State()
        st.status, st.task = "running", "T-0001 fix \x1b]0;pwn\x07add‮"
        title = tui().title_text(st)
        self.assertTrue(title.startswith("fm code: running"), title)
        self.assertIn("T-0001 fix", title)
        self.assertFalse(any(not ch.isprintable() for ch in title), repr(title))
        self.assertEqual(tui().title_text(tui().State()), "fm code: idle")
        self.assertEqual(tui().title_text(None), "fm code")

    def test_the_header_names_the_runs_containment(self):  # T-1760 (T-1402)
        T = tui()
        app = T.App(T.DEFAULTS, spawn=lambda *a, **k: None)
        app.feed({"event": "start", "request": "fix add", "worker": "claude", "schema": 1,
                  "containment": {"kind": "landlock", "label": "Landlock ABI 6"}})
        self.assertIn("Landlock ABI 6", T.render(app.state, 120, 34, T.DEFAULTS, app, t=100.0).text().splitlines()[0])

    def test_a_failure_shows_the_command_to_run_next_under_it(self):  # T-1754 (T-1334)
        st = tui().State()
        st.apply({"event": "error", "text": "fm code: model: refused", "next": "fm code --resume T-0001"})
        st.apply({"event": "blocked", "text": "T-0001 blocked: red"})
        self.assertEqual([x["kind"] for x in st.log], ["error", "next", "blocked"])
        self.assertEqual(tui().entry_text(st.log[1]), "Next: fm code --resume T-0001")

    def test_the_bell_rings_once_per_block_or_refusal_and_not_with_motion_off(self):
        app = tui().App(tui().DEFAULTS, spawn=lambda *a, **k: None)
        self.assertFalse(app.bell_due())
        app.feed({"event": "action", "action": "edit", "target": "x", "result": "refused", "detail": "guard"})
        self.assertTrue(app.bell_due())
        self.assertFalse(app.bell_due(), "once")
        app.feed({"event": "blocked", "text": "needs you"})
        self.assertTrue(app.bell_due())
        self.assertFalse(app.bell_due())
        quiet = tui().App(dict(tui().DEFAULTS, motion="off"), spawn=lambda *a, **k: None)
        quiet.feed({"event": "blocked", "text": "needs you"})
        self.assertFalse(quiet.bell_due())

    def test_a_silent_model_is_named_past_ten_seconds(self):
        st = tui().State()
        st.status = "running"
        st.heard = 4
        bar = tui().render(st, 120, 30, tui().DEFAULTS).text()
        self.assertIn("last heard 4s ago", bar.replace(" s ago", "s ago"))
        st.heard = 14
        self.assertIn("model: last heard 14s ago", tui().render(st, 120, 30, tui().DEFAULTS).text())

    def test_the_loop_writes_the_title_and_the_bell(self):
        app = tui().App(tui().DEFAULTS, spawn=lambda *a, **k: None)
        app.state.status = "blocked"
        out = []

        class IO:
            first = True

            def resized(self):
                r, IO.first = IO.first, False
                return r

            def size(self):
                return 100, 30

            def write(self, s):
                out.append(s)

            def keys(self, timeout):
                return ["q"]
        tui()._loop(IO(), app)
        text = "".join(out)
        self.assertIn("\x1b]0;fm code: blocked\x07", text)
        self.assertIn("\a", text.replace("\x1b]0;fm code: blocked\x07", ""))


class Views(unittest.TestCase):
    def app(self, **kw):
        return tui().App(tui().DEFAULTS, spawn=lambda *a, **k: None, **kw)

    def test_d_shows_the_runs_diff_coloured_and_q_closes_only_the_view(self):
        app = self.app(diff=lambda: DIFF)
        for e in RUN:
            app.feed(e)
        app.key("d")
        self.assertEqual(app.mode, "diff")
        app.view_opened -= 10  # T-1312: the reveal on open is over
        cv = tui().render(app.state, 100, 30, tui().DEFAULTS, app)
        self.assertEqual(cv.style_of("+    if args.dry_run:"), "ok")
        self.assertEqual(cv.style_of("-    return upload_done()"), "bad")
        self.assertEqual(cv.style_of("@@ -21,6"), "accent")
        app.key("q")
        self.assertEqual((app.mode, app.done), ("main", False))

    def test_h_lists_past_runs_and_enter_replays_one(self):
        import tempfile
        folder = tempfile.mkdtemp()
        for tid, req, end in (("T-0001", "fix add", "closed"), ("T-0002", "add a --dry-run flag", "blocked")):
            with open(os.path.join(folder, f"{tid}.events.jsonl"), "w") as f:
                for e in ({"event": "start", "text": "", "schema": 1, "request": req, "ts": 1760000000},
                          {"event": "task", "text": "", "task": tid}, {"event": end, "text": end, "task": tid}):
                    f.write(json.dumps(e) + "\n")
        app = self.app(history=folder)
        app.key("h")
        self.assertEqual(app.mode, "history")
        text = tui().render(app.state, 100, 30, tui().DEFAULTS, app).text()
        self.assertIn("add a --dry-run flag", text)
        self.assertIn("fix add", text)
        app.key("j")
        app.key("\n")
        self.assertEqual(app.mode, "main")
        self.assertIn(app.state.task, ("T-0001", "T-0002"))
        self.assertIn(app.state.status, ("closed", "blocked"))

    def test_a_replay_steps_through_its_events_with_the_arrows(self):  # T-1307
        path = os.path.join(tempfile.mkdtemp(), "T-0001.events.jsonl")
        with open(path, "w") as f:
            f.write("".join(json.dumps(e) + "\n" for e in RUN))
        app = self.app()
        app.replay(path)
        self.assertEqual(app.state.status, "closed")
        self.assertIn("←/→ step through its 14 events", app.message)
        app.key("KEY_LEFT")
        self.assertNotEqual(app.state.status, "closed", "one event back: before the close")
        self.assertIn("◂▸ 13/14 verify", app.message)
        app.key("KEY_HOME")
        self.assertEqual((app.state.task, app.state.status), (None, "planning"), "the start alone")
        self.assertIn("◂▸ 1/14 start", app.message)
        app.key("KEY_RIGHT")
        app.key("KEY_RIGHT")
        self.assertEqual(app.state.task, "T-0001")
        app.key("KEY_END")
        self.assertEqual(app.state.status, "closed")
        app.key("KEY_RIGHT")  # past the end: stays
        self.assertIn("◂▸ 14/14", app.message)
        live = self.app()
        for e in RUN[:5]:
            live.feed(e)
        before = len(live.state.log)
        live.key("KEY_LEFT")  # a live run isn't a replay: nothing to step
        self.assertEqual(len(live.state.log), before)

    def test_m_picks_the_model_or_worker_the_next_run_uses(self):
        app = self.app(choices=lambda: [("model", "qwen2.5-coder:32b"), ("worker", "claude"), ("worker", "codex")])
        app.key("m")
        self.assertEqual(app.mode, "picker")
        self.assertIn("codex", tui().render(app.state, 100, 30, tui().DEFAULTS, app).text())
        app.key("j")
        app.key("\n")
        self.assertEqual((app.mode, app.opts.get("worker"), app.opts.get("model")), ("main", "claude", None))
        started = []
        app.spawn = lambda words, opts: started.append(opts)
        app.start(["fix add"])
        self.assertEqual(started[0]["worker"], "claude")


def screen(data, rows=34, cols=120):
    """T-1430: the text a terminal shows after data: cursor moves (CSI row;col H), erase-display and printed characters
    (a wide one fills two cells); every other escape is ignored."""
    import re
    import unicodedata
    grid = [[" "] * cols for _ in range(rows)]
    r = c = 0
    for m in re.finditer(r"\x1b\[([0-9;?]*)([A-Za-z])|\x1b[()][A-Za-z0-9]|\x1b.|([^\x1b])",
                         bytes(data).decode("utf-8", "replace")):
        if m.group(3) is not None:
            ch = m.group(3)
            if ch == "\r":
                c = 0
            elif ch == "\n":
                r = min(r + 1, rows - 1)
            elif ch.isprintable() and r < rows and c < cols:
                grid[r][c] = ch
                c += 2 if unicodedata.east_asian_width(ch) in "WF" else 1
        elif m.group(2) == "H":
            at = (m.group(1) or "").split(";")
            r, c = int(at[0] or 1) - 1, int(at[1] if len(at) > 1 and at[1] else 1) - 1
        elif m.group(2) == "J" and m.group(1) == "2":
            grid = [[" "] * cols for _ in range(rows)]
    return "\n".join("".join(row) for row in grid)


class Terminal(unittest.TestCase):
    """T-1300: Foreman's own ANSI renderer (no curses): 24-bit colour where the terminal has it, the nearest of 256
    or 8 elsewhere, none on NO_COLOR, and only the cells that changed are written again."""

    def canvas(self):
        return tui().render(Core.state(self), 80, 24, tui().DEFAULTS)

    def test_the_colour_depth_follows_the_terminal(self):
        depth = tui().colour_depth
        self.assertEqual(depth({"COLORTERM": "truecolor", "TERM": "xterm-256color"}), 24)
        self.assertEqual(depth({"COLORTERM": "24bit"}), 24)
        self.assertEqual(depth({"TERM": "xterm-256color"}), 256)
        self.assertEqual(depth({"TERM": "xterm"}), 8)
        self.assertEqual(depth({"NO_COLOR": "1", "COLORTERM": "truecolor"}), 0)

    def test_each_depth_writes_its_own_colour_codes(self):
        for d, has, hasnt in ((24, "38;2;", "38;5;"), (256, "38;5;", "38;2;"), (0, "\x1b[", "38;")):
            out = tui().Term(d).frame(self.canvas(), tui().DEFAULTS)
            self.assertIn(has, out, d)
            if d:
                self.assertNotIn(hasnt, out, d)
            else:
                self.assertNotIn(";2;", out)
                self.assertNotIn("38;", out)
        eight = tui().Term(8).frame(self.canvas(), tui().DEFAULTS)
        self.assertNotIn("38;2;", eight)
        self.assertNotIn("38;5;", eight)

    def test_a_repeated_frame_writes_almost_nothing_and_a_change_only_its_cells(self):
        term, cv = tui().Term(24), self.canvas()
        first = term.frame(cv, tui().DEFAULTS)
        self.assertGreater(len(first), 2000)
        self.assertLess(len(term.frame(cv, tui().DEFAULTS)), 16)
        cv.put(5, 5, "Z", "bad")
        self.assertLess(len(term.frame(cv, tui().DEFAULTS)), 80)

    def test_keys_arrive_with_the_names_the_app_binds(self):
        self.assertEqual(tui().parse_keys("\x1b[A\x1b[6~q\x12\r\x7f\x1b[B\t"),
                         ["KEY_UP", "KEY_NPAGE", "q", "^R", "\n", "KEY_BACKSPACE", "KEY_DOWN", "\t"])
        self.assertEqual(tui().parse_keys("\x1b"), ["\x1b"])

    def test_any_size_draws_and_under_60_columns_is_one_panel(self):
        st = Core.state(self)
        for w, h in ((1, 1), (10, 5), (30, 8), (59, 20), (79, 24), (200, 60)):
            text = tui().render(st, w, h, tui().DEFAULTS).text().splitlines()
            self.assertEqual(len(text), h, (w, h))
            self.assertTrue(all(len(line) <= w for line in text), (w, h))
        small = tui().render(st, 59, 20, tui().DEFAULTS).text()
        self.assertEqual(small.count("╭"), 1, "just the activity log")
        self.assertIn("rm -rf ~", small)

    def test_no_colour_reverses_chips_but_not_text_on_a_panel(self):
        cv, term = self.canvas(), tui().Term(0)
        rows, text = term.cells(cv, tui().DEFAULTS), cv.text().splitlines()
        y = next(i for i, line in enumerate(text) if "rm -rf ~" in line)
        self.assertNotIn(";7", rows[y][text[y].index("rm -rf ~")][1], "plain text inside a panel isn't inverted")
        self.assertIn(";7", rows[y][text[y].index("GUARD")][1], "a chip still is")

    def test_a_key_sequence_split_across_reads_waits_for_its_end(self):
        split = tui().split_tail
        self.assertEqual(split("ab\x1b["), ("ab", "\x1b["))
        self.assertEqual(split("q\x1b[1;5"), ("q", "\x1b[1;5"))
        self.assertEqual(split("q\x1b"), ("q", "\x1b"))
        self.assertEqual(split("q\x1b[A"), ("q\x1b[A", ""))

    def test_wide_and_combining_characters_keep_the_columns(self):
        cv = tui().Canvas(10, 1)
        cv.put(0, 0, "日本x")
        self.assertEqual([ch for ch, _ in cv.cells[0][:5]], ["日", "", "本", "", "x"])
        cv.put(0, 0, "éxy")
        self.assertEqual([ch for ch, _ in cv.cells[0][:3]], ["é", "x", "y"])
        self.assertEqual(len(cv.cells[0]), 10)

    def test_a_closed_terminal_doesnt_hide_why_the_screen_ended(self):
        import pty
        master, slave = pty.openpty()

        def gone(io):
            os.close(master)
            raise SystemExit(129)
        with self.assertRaises(SystemExit):
            tui().session(gone, slave, slave)
        os.close(slave)

    def test_wrap_is_off_so_the_last_cell_cant_scroll_the_screen(self):
        from sampo import smithterm
        self.assertIn("\x1b[?7l", smithterm.ENTER)
        self.assertIn("\x1b[?7h", smithterm.LEAVE)


class Paste(unittest.TestCase):
    """T-1316: a bracketed paste is one event: the prompt takes it whole (newlines kept, nothing submitted), anywhere
    else it fires nothing, and the terminal's paste mode is on while the screen runs and off however it ends."""

    def app(self):
        spawned = []
        app = tui().App(tui().DEFAULTS, spawn=lambda words, opts: spawned.append(words))
        return app, spawned

    def paste(self, app, text):
        for k in tui().parse_keys("\x1b[200~" + text + "\x1b[201~"):
            app.key(k)

    def test_three_lines_into_the_prompt_are_one_entry(self):
        app, spawned = self.app()
        app.new()
        self.paste(app, "fix add\r\nthen q\rs too\x1b[2J\x07")
        self.assertEqual(app.input, "fix add\nthen q\ns too[2J", "newlines kept, control characters dropped")
        self.assertEqual((app.mode, app.done, spawned), ("prompt", False, []))
        text = tui().render(Core.state(self), 80, 24, tui().DEFAULTS, app).text()
        self.assertNotIn("\x1b", text)
        self.assertEqual(len(text.splitlines()), 24, "the pasted newlines don't break the prompt line")
        app.key("\n")
        self.assertEqual(spawned, [["fix add\nthen q\ns too[2J"]])

    def test_outside_the_prompt_a_paste_fires_nothing(self):
        app, spawned = self.app()
        killed = []
        with unittest.mock.patch.object(os, "killpg", lambda pid, sig: killed.append(pid)):
            for mode in ("main", "help", "diff", "home"):
                app.mode = mode
                self.paste(app, "q\ns\nn\n?\n")
                self.assertEqual((app.mode, app.done), (mode, False), mode)
        self.assertEqual((spawned, killed), ([], []))

    def test_the_apps_runs_are_attended(self):
        """T-1401: the app's run has pipes, not a terminal; it says it is watched, so a stale safety gate warns."""
        from unittest import mock
        T = tui()
        app = T.App(T.DEFAULTS)
        with mock.patch.object(T.subprocess, "Popen") as popen, mock.patch.object(T.threading, "Thread"):
            app._spawn(["fix", "add"], {})
        self.assertEqual(popen.call_args.kwargs["env"].get("FOREMAN_CODE_ATTENDED"), "1")

    def test_a_paste_is_capped_and_one_split_across_reads_waits_for_its_end(self):
        cap = tui().PASTE_CAP
        self.assertEqual(tui().parse_keys("\x1b[200~" + "a" * (cap * 2) + "\x1b[201~q"), [("paste", "a" * cap), "q"])
        split = tui().split_tail
        self.assertEqual(split("x\x1b[200~ab\ncd\x1b[20"), ("x", "\x1b[200~ab\ncd\x1b[20"))
        self.assertEqual(split("x\x1b[200~ab\x1b[201~y"), ("x\x1b[200~ab\x1b[201~y", ""))
        _, held = split("\x1b[200~" + "b" * (cap * 3))
        self.assertLess(len(held), cap + 64, "what waits for a paste's end is bounded")
        self.assertEqual(tui().parse_keys(held + "\x1b[201~"), [("paste", "b" * cap)])

    def test_modes_on_and_off(self):
        from sampo import smithterm
        import pty
        from helpers import pty_reader
        for crash in (False, True):
            master, slave = pty.openpty()
            got = []
            reader = pty_reader(master)  # T-1828: macOS drops output nobody read before the slave closes

            def run(io):
                os.write(master, b"\x1b[200~one\rtw")
                got.extend(io.keys(0.05))
                os.write(master, b"o\x1b[201~q")
                got.extend(io.keys(0.5))
                if crash:
                    raise RuntimeError("boom")
            try:
                smithterm.session(run, slave, slave)
            except RuntimeError:
                self.assertTrue(crash)
            os.close(slave)
            reader.join(10)
            os.close(master)
            self.assertEqual(got, [("paste", "one\ntwo"), "q"], crash)
            text = bytes(reader.out).decode()
            self.assertIn("\x1b[?2004h", text)
            self.assertGreater(text.rindex("\x1b[?2004l"), text.index("\x1b[?2004h"), crash)

    def test_a_paste_whose_end_never_comes_is_taken_once_input_stalls(self):
        from sampo import smithterm
        import pty
        from helpers import pty_reader
        master, slave = pty.openpty()
        got = []
        reader = pty_reader(master)  # T-1828: an unread master stalls the session's writes on macOS

        def run(io):
            os.write(master, b"\x1b[200~abc")
            got.append(io.keys(0.2))
            got.append(io.keys(0.01))
        with unittest.mock.patch.object(smithterm, "PASTE_STALL", 0.0):
            smithterm.session(run, slave, slave)
        os.close(slave)
        reader.join(10)
        os.close(master)
        self.assertEqual(got, [[], [("paste", "abc")]])


RUNNING = RUN[:10]  # step 2 just started, the checks not yet run


def lum(spec):
    from sampo import smithtui
    r, g, b = smithtui._rgb(spec)
    return r + g + b


class Motion(unittest.TestCase):
    """T-1300: motion is a pure function of state and time. Each test renders at two times and compares."""

    def app(self, events, motion="full"):
        cfg = dict(tui().DEFAULTS, motion=motion)
        app = tui().App(cfg, spawn=lambda *a, **k: None)
        for e in events:
            app.feed(e)
        return app, cfg

    def frame(self, app, cfg, t, w=110, h=32):
        return tui().render(app.state, w, h, cfg, app, t=t)

    def cell(self, cv, needle, cfg, offset=0):
        for y, row in enumerate(cv.cells):
            i = "".join(ch for ch, _ in row).find(needle)
            if i >= 0:
                return tui().resolve(row[i + offset][1], cfg, cv.bg[y][i + offset])
        self.fail(f"{needle!r} not drawn")

    def test_the_projects_motion_score_sets_how_long_values_roll(self):  # T-1626
        score = ('[[motion]]\nname = "fm-code-counts"\nduration = 2000\neasing = "linear"\nproperties = ["opacity"]\n'
                 'reduced = "fade"\n[[motion]]\nname = "fm-code-deck"\nduration = 0\neasing = "linear"\n'
                 'properties = ["opacity"]\nreduced = "instant"\n')
        score += "".join(f'[[motion]]\nname = "fm-code-{n}"\nduration = {ms}\neasing = "linear"\nproperties = ["opacity"]\n'
                         'reduced = "fade"\n' for n, ms in (("bars", 300), ("columns", 500)))
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(tui().score_durations(d), {})  # no score: fm code keeps its own
            with open(os.path.join(d, "motion.toml"), "w") as f:
                f.write(score)
            got = tui().score_durations(d)
            self.assertEqual(got, {"n": 2.0, "tok": 2.0, "deck": 0.001, "bar": 0.3, "col": 0.5})  # 0 ms is 1 ms: no divide by zero
            cfg = dict(tui().DEFAULTS, motion="full")
            self.assertEqual(tui().App(cfg, spawn=lambda *a, **k: None, root=d).motion.score, got)
            with open(os.path.join(d, "motion.toml"), "w") as f:
                f.write("[[motion]\n")  # unreadable: ignored, not a crash
            self.assertEqual(tui().score_durations(d), {})
        for score, mid in (({}, 100), ({"n": 2.0}, None)):
            m = tui().Motion(score)
            m.frames = 5  # past the first frame: a new value rolls up from 0
            m.eased("n+:a.py", 100, 10.0)
            shown = m.eased("n+:a.py", 100, 11.0)
            self.assertEqual(shown, 100) if mid else self.assertLess(shown, 100)
            self.assertEqual(m.eased("bar+:a.py", 7, 10.0), 0)  # another family keeps the default dur
            self.assertEqual(m.eased("bar+:a.py", 7, 10.8), 7)
        m = tui().Motion({"deck": 0.001})
        m.frames = 5
        m.eased("deck_y", 3, 10.0)
        self.assertEqual(m.eased("deck_y", 3, 10.01), 3)  # 1 ms: there at once, no ZeroDivisionError

    def test_a_switch_between_views_fades_in_unless_motion_is_off(self):  # T-1307
        for motion, fades in (("full", True), ("reduced", True), ("off", False)):
            app, cfg = self.app(RUN, motion)
            self.frame(app, cfg, 100.0)
            app.key("h")
            self.frame(app, cfg, 100.0)
            mid, end = self.frame(app, cfg, 100.08), self.frame(app, cfg, 101.0)
            a, b = self.cell(mid, "Past runs", cfg), self.cell(end, "Past runs", cfg)
            self.assertEqual(a != b, fades, motion)
            app.key("q")  # back to the run: it fades in too
            self.frame(app, cfg, 101.0)
            mid, end = self.frame(app, cfg, 101.08), self.frame(app, cfg, 102.0)
            self.assertEqual(self.cell(mid, "fix add", cfg) != self.cell(end, "fix add", cfg), fades, motion)

    def test_the_station_being_worked_pulses_and_its_spinner_turns(self):
        app, cfg = self.app(RUNNING)
        a, b = self.frame(app, cfg, 100.0), self.frame(app, cfg, 100.8)
        track_a, track_b = a.text().splitlines()[2], b.text().splitlines()[2]
        self.assertNotEqual(track_a, track_b, "the spinner moved")
        chip = lambda cv: next(tui().resolve(st, cfg)["bg"] for ch, st in cv.cells[2] if tui().resolve(st, cfg).get("bg")
                               and isinstance(st, tuple))
        self.assertNotEqual(chip(a), chip(b), "the pulse changed the chip's colour")

    def test_a_light_travels_along_the_link_into_the_current_station(self):
        app, cfg = self.app(RUNNING)
        a, b = self.frame(app, cfg, 100.0), self.frame(app, cfg, 100.35)
        link = lambda cv: [tui().resolve(st, cfg).get("fg") for ch, st in cv.cells[2] if ch == "━"]
        self.assertNotEqual(link(a), link(b))

    def test_counters_roll_up_to_new_values(self):
        app, cfg = self.app(RUN[:6])
        self.frame(app, cfg, 100.0)
        app.feed({"event": "tokens", "text": "", "call_in": 9000, "call_out": 900, "total_in": 10200, "total_out": 980})
        early, late = self.frame(app, cfg, 100.05).text(), self.frame(app, cfg, 103.0).text()
        self.assertNotIn("10.2k in", early, "still rolling")
        self.assertIn("10.2k in", late)

    def test_a_check_that_flips_flashes_then_settles(self):
        app, cfg = self.app(RUN[:12])
        self.frame(app, cfg, 100.0)
        app.feed(RUN[12])  # the check passes
        hot, cool = self.frame(app, cfg, 100.05), self.frame(app, cfg, 104.0)
        self.assertGreater(lum(self.cell(hot, "PASS", cfg)["bg"]), lum(self.cell(cool, "PASS", cfg)["bg"]))
        self.assertEqual(self.cell(cool, "PASS", cfg)["bg"], tui().resolve("chip_ok", cfg)["bg"])

    def test_a_new_activity_line_fades_in(self):
        app, cfg = self.app(RUN[:7])
        self.frame(app, cfg, 100.0)
        app.feed(RUN[7])  # read calc.py
        dim, full = self.frame(app, cfg, 100.03), self.frame(app, cfg, 103.0)
        self.assertLess(lum(self.cell(dim, "read", cfg)["fg"]), lum(self.cell(full, "read", cfg)["fg"]))

    def test_closing_sends_a_wave_along_the_track(self):
        app, cfg = self.app(RUN[:-1])
        self.frame(app, cfg, 100.0)
        app.feed(RUN[-1])
        self.frame(app, cfg, 100.05)  # the frame that sees it close
        during, after = self.frame(app, cfg, 100.45), self.frame(app, cfg, 105.0)
        bright = lambda cv: max(lum(tui().resolve(st, cfg, cv.bg[2][x]).get("fg") or "#000000")
                                for x, (ch, st) in enumerate(cv.cells[2]) if ch == "━")
        self.assertGreater(bright(during), bright(after))

    def test_a_guard_refusal_lands_with_a_flash(self):
        app, cfg = self.app(RUN[:8])
        self.frame(app, cfg, 100.0)
        app.feed(RUN[8])  # rm -rf ~ refused
        self.frame(app, cfg, 100.02)
        hot, cool = self.frame(app, cfg, 100.3), self.frame(app, cfg, 104.0)
        self.assertGreater(lum(self.cell(hot, "GUARD", cfg, -1)["bg"]), lum(self.cell(cool, "GUARD", cfg, -1)["bg"]))
        self.assertEqual(self.cell(cool, "GUARD", cfg, -1)["bg"], tui().resolve("chip_bad", cfg)["bg"])

    def test_the_idle_screen_is_a_welcome_not_a_blank(self):
        app, cfg = self.app([])
        a, b = self.frame(app, cfg, 100.0), self.frame(app, cfg, 100.9)
        text = a.text()
        for words in ("Smith Code", "new request", "past runs"):
            self.assertIn(words, text)
        self.assertNotEqual([r for r in a.cells], [r for r in b.cells], "its mark breathes")

    def test_motion_off_draws_the_same_at_any_time(self):
        app, cfg = self.app(RUNNING, motion="off")
        a, b = self.frame(app, cfg, 100.0), self.frame(app, cfg, 101.37)
        self.assertEqual(a.cells, b.cells)

    def test_reduced_motion_keeps_colour_changes_but_nothing_moves(self):
        app, cfg = self.app(RUN[:12], motion="reduced")
        self.frame(app, cfg, 100.0)
        app.feed(RUN[12])
        a, b = self.frame(app, cfg, 100.05), self.frame(app, cfg, 100.55)
        self.assertEqual(a.text(), b.text(), "no spinner, no rolling numbers")
        self.assertGreater(lum(self.cell(a, "PASS", cfg)["bg"]), lum(tui().resolve("chip_ok", cfg)["bg"]))

    def test_the_frame_rate_follows_what_moves(self):
        app, cfg = self.app(RUNNING)
        self.frame(app, cfg, 100.0)
        self.assertLessEqual(app.fps(100.0), 30)
        self.assertGreaterEqual(app.fps(100.0), 8, "live work animates")
        done, cfg2 = self.app(RUN)
        self.frame(done, cfg2, 100.0)
        self.assertLessEqual(done.fps(110.0), 4, "a settled screen barely redraws")
        off, cfg3 = self.app(RUNNING, motion="off")
        self.assertLessEqual(off.fps(100.0), 4)

    def test_gradients_colour_the_header_and_the_chart(self):
        app, cfg = self.app(RUN)
        cv = self.frame(app, cfg, 100.0)
        brand = [tui().resolve(st, cfg).get("bg") for ch, st in cv.cells[0][:16]]
        self.assertGreater(len(set(brand)), 3, "the brand chip is a ramp, not one colour")
        rows = [y for y, row in enumerate(cv.cells) if "Tokens" in "".join(ch for ch, _ in row)]
        cols = [tui().resolve(st, cfg).get("fg") for y in range(rows[0] + 1, cv.h - 3)
                for ch, st in cv.cells[y] if ch in tui().BLOCKS]
        self.assertGreater(len(set(cols)), 1, "the token columns are shaded")

    def test_panels_sit_on_their_own_background(self):
        cv = self.frame(*self.app(RUN), 100.0)
        inside = cv.bg[5][3]
        self.assertIsNotNone(inside, "a panel's inside has its own tint")
        self.assertNotEqual(inside, tui().resolve("screen", tui().DEFAULTS)["bg"])

    def test_the_svg_draws_bars_without_seams(self):
        """Backgrounds first, then glyphs that overlap their neighbours: no hairline between a bar's cells."""
        T, rgb = tui(), tui().rgb
        cv = T.Canvas(4, 2)
        cv.tint(0, 0, 2, 4, "#111B2B")
        cv.put(0, 0, "▆", rgb(fg="#3366FF"))
        cv.put(0, 1, "▆", rgb(fg="#4477FF"))
        cv.put(1, 0, "█", rgb(fg="#5588FF"))
        svg = T.to_svg(cv, T.DEFAULTS).splitlines()
        rects = [s for s in svg if s.startswith("<rect x=")]
        backs = [k for k, s in enumerate(rects) if 'fill="#111b2b"' in s.lower()]
        bars = [k for k, s in enumerate(rects) if "#3366" in s.upper() or "#4477" in s.upper() or "#5588" in s.upper()]
        self.assertEqual((len(bars), len(backs) > 0), (3, True), rects)
        self.assertLess(max(backs), min(bars), "every background comes before every bar")
        a, b = (float(rects[k].split('x="')[1].split('"')[0]) for k in bars[:2])
        w = float(rects[bars[0]].split('width="')[1].split('"')[0])
        self.assertGreater(a + w, b, "a bar cell overlaps the next one")

    def row_of(self, cv, needle):
        return next((y, "".join(ch for ch, _ in row).find(needle)) for y, row in enumerate(cv.cells)
                    if needle in "".join(ch for ch, _ in row))

    def test_the_newest_line_is_the_focal_line_while_running(self):
        app, cfg = self.app(RUNNING)
        cv = self.frame(app, cfg, 100.0)
        y, x = self.row_of(cv, "2  fix calc.add")  # step 2 has just started: its header is the newest line
        panel = tui().col(cfg, "panel", "bg")
        self.assertNotEqual(tui().resolve(cv.cells[y][x + 4][1], cfg, cv.bg[y][x + 4]).get("bg"), panel, "the now line")
        y2, x2 = self.row_of(cv, "rm -rf ~")
        self.assertEqual(tui().resolve(cv.cells[y2][x2][1], cfg, cv.bg[y2][x2]).get("bg"), panel, "older lines")
        app, cfg = self.app(RUN)
        cv = self.frame(app, cfg, 100.0)
        y, x = self.row_of(cv, "T-0001 closed")
        self.assertEqual(tui().resolve(cv.cells[y][x][1], cfg, cv.bg[y][x]).get("bg"), panel, "a closed run has none")

    def test_a_file_that_just_changed_glows_then_cools(self):
        app, cfg = self.app(RUN[:11])
        self.frame(app, cfg, 100.0)
        app.feed(RUN[11])  # calc.py changed
        self.frame(app, cfg, 100.05)
        def name(t):  # the Changed panel's calc.py (the Activity panel names it too, further left)
            cv = self.frame(app, cfg, t)
            y = next(y for y, row in enumerate(cv.cells) if " +0" in "".join(ch for ch, _ in row))
            x = "".join(ch for ch, _ in cv.cells[y]).rfind("calc.py")
            return tui().resolve(cv.cells[y][x][1], cfg, cv.bg[y][x])
        hot, cold = name(100.3), name(105.0)
        ink = tui().col(cfg, "screen")
        self.assertGreater(lum(hot["fg"]), lum(cold.get("fg") or ink) + 20, "just changed: bright")
        self.assertEqual(cold.get("fg") or ink, ink, "then plain text again")

    def test_rolling_totals_hold_their_place(self):
        app, cfg = self.app(RUN[:6])
        self.frame(app, cfg, 100.0)
        app.feed(RUN[6])
        self.frame(app, cfg, 100.02)
        places = {self.row_of(self.frame(app, cfg, t), " in ")[1] for t in (100.05, 100.4, 105.0)}
        self.assertEqual(len(places), 1, "the labels don't slide while the numbers roll")

    def test_an_empty_panel_shimmers_while_the_run_starts(self):
        app, cfg = self.app(RUN[:1])
        looks = {tuple(lum(self.cell(self.frame(app, cfg, t), "nothing changed yet", cfg, k)["fg"])
                       for k in range(0, 18, 3))
                 for t in (100.0, 100.5, 101.0)}
        self.assertGreater(len(looks), 1, "a light passes over the words")
        self.row_of(self.frame(app, cfg, 101.0), "planning the work")  # not the idle welcome
        app, cfg = self.app(RUN[:1], motion="off")
        self.assertEqual(self.cell(self.frame(app, cfg, 100.0), "nothing changed yet", cfg).get("fg"),
                         tui().col(cfg, "dim"))

    def test_a_theme_without_colours_keeps_its_chips_reversed_while_moving(self):
        cfg = dict(tui().DEFAULTS, theme=tui().PRESETS["mono"](), preset="mono")
        app = tui().App(cfg, spawn=lambda *a, **k: None)
        for e in RUN[:8]:
            app.feed(e)
        self.frame(app, cfg, 100.0)
        app.feed(RUN[8])  # the guard refuses
        for t in (100.05, 100.3):
            cv = self.frame(app, cfg, t)
            self.assertTrue(self.cell(cv, "GUARD", cfg).get("reverse"), t)
            self.assertTrue(self.cell(cv, "RUNNING", cfg).get("reverse"), t)

    def test_a_note_names_the_runner_in_activity(self):
        # T-1328: with nothing configured fm code picks a runner and says which; the screen shows it
        app, cfg = self.app(RUN[:1] + [{"event": "note", "text": "runner: claude (no model is set up)", "ts": 1.0}])
        self.assertIn("runner: claude", self.frame(app, cfg, 100.0, w=110, h=32).text())

    def test_tool_uses_show_and_a_heartbeat_ticks_until_the_next_line(self):
        # T-1329: a CLI's tool use is an Activity line; "still working" ticks in one line, gone at the next real one
        beat = {"event": "working", "worker": "claude", "secs": 10, "text": "claude working (10 s)", "ts": 2.0}
        app, cfg = self.app(RUN[:1] + [{"event": "tool", "action": "edit", "target": "calc.py", "ts": 1.0}, beat,
                                       dict(beat, secs=20, text="claude working (20 s)")])
        text = self.frame(app, cfg, 100.0, w=110, h=32).text()
        self.assertIn("edit", text)
        self.assertIn("calc.py", text)
        self.assertIn("working (20 s)", text)
        self.assertNotIn("working (10 s)", text, "one ticking line, not one per beat")
        app.feed({"event": "tool", "action": "run", "target": "python3 test_calc.py", "ts": 3.0})
        self.assertNotIn("working (20 s)", self.frame(app, cfg, 101.0, w=110, h=32).text())

    def test_a_stall_is_a_warning_line_and_the_status_bar_says_when_the_cli_was_last_heard(self):
        # T-1336: a CLI silent past FM_STALL gets a stalled line in Activity; the bar says how long it has been quiet
        beat = {"event": "working", "worker": "claude", "secs": 130, "silent": 125, "text": "claude working (130 s)",
                "ts": 2.0}
        stall = {"event": "stalled", "worker": "claude", "secs": 125, "cause": "prompt", "last": "Overwrite? (y/n)",
                 "text": "claude stalled: waiting at a prompt: Overwrite? (y/n)", "ts": 2.5}
        app, cfg = self.app(RUN[:1] + [beat, stall], motion="off")
        cv = self.frame(app, cfg, 100.0, w=110, h=32)
        self.assertIn("Overwrite? (y/n)", cv.text().splitlines()[self.row_of(cv, "waiting at a prompt")[0]])
        self.assertEqual(self.cell(cv, "waiting at a prompt", cfg).get("fg"), tui().col(cfg, "warn"))
        bar = cv.text().splitlines()[-1]
        self.assertIn("last heard 2m05s ago", bar)
        app.feed({"event": "tool", "action": "run", "target": "pytest", "ts": 3.0})
        self.assertNotIn("last heard", self.frame(app, cfg, 101.0, w=110, h=32).text().splitlines()[-1],
                         "heard again: nothing to say")
        self.assertIn("waiting at a prompt", self.frame(app, cfg, 101.0, w=110, h=32).text(), "the warning stays")

    def test_an_answer_wraps_and_the_run_reads_as_finished(self):
        # T-1320: a question's answer is one long line; it wraps in Activity, and nothing reads as still running
        long = "parse_dates treats a date with no timezone as UTC " * 6
        app, cfg = self.app(RUN[:1] + [{"event": "answer", "text": long.strip(), "ts": 5.0}])
        cv = self.frame(app, cfg, 100.0, w=110, h=32)
        text = cv.text()
        self.assertGreaterEqual(text.count("treats a date"), 4, "every part of the answer is on screen")
        self.assertIn("ANSWERED", text)
        self.assertNotIn("…", "".join(line for line in text.splitlines() if "treats a date" in line))

    def test_no_state_is_shown_by_colour_alone(self):
        """Mono theme: every verdict still reads as a word or a mark."""
        cfg = dict(tui().DEFAULTS, theme=tui().PRESETS["mono"](), preset="mono")
        for events, words in ((RUNNING, ("RED", "GUARD", "✓")), (RUN, ("PASS", "DONE", "GUARD"))):
            app = tui().App(cfg, spawn=lambda *a, **k: None)
            for e in events:
                app.feed(e)
            text = "\n".join("".join(ch for ch, _ in row) for row in self.frame(app, cfg, 100.0).cells)
            for word in words:
                self.assertIn(word, text)


class Record(unittest.TestCase):
    def test_a_run_records_to_a_video(self):
        import shutil
        import tempfile
        if not (shutil.which("ffmpeg") and shutil.which("rsvg-convert")):
            self.skipTest("needs ffmpeg and rsvg-convert")
        out = os.path.join(tempfile.mkdtemp(), "run.mp4")
        tui().record(RUN, out, size=(60, 20), fps=4, seconds=2)
        self.assertGreater(os.path.getsize(out), 1000)

    def test_without_ffmpeg_it_says_so(self):
        with unittest.mock.patch("shutil.which", lambda name: None):
            with self.assertRaises(RuntimeError) as e:
                tui().record(RUN, "x.mp4")
        self.assertIn("ffmpeg", str(e.exception))


class Config(unittest.TestCase):
    def write(self, text):
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), "tui.toml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def test_contrast_is_fmdesigns_one_wcag_formula(self):
        # T-1675: the TUI turns its colours (#rrggbb or a 256-colour index) into sRGB and asks fmdesign; no copy here
        import inspect
        from sampo import design
        T = tui()
        self.assertFalse("0.03928" in inspect.getsource(T), "one WCAG implementation: fmdesign.contrast")
        self.assertAlmostEqual(T.contrast("#000000", "#FFFFFF"), 21.0)
        self.assertAlmostEqual(T.contrast(16, 231), 21.0, msg="256-colour cube black and white")
        self.assertAlmostEqual(T.contrast("#777777", 0), design.contrast((0x77 / 255,) * 3, (0, 0, 0)))

    def test_a_users_file_sets_theme_colours_layout_and_keys(self):
        cfg, errors = tui().load_config(self.write(
            'theme = "paper"\nborder = "heavy"\n[colors]\nok = "#007A00"\naccent = { fg = 33, bold = false }\n'
            '[layout]\ncolumns = [["log", "files"]]\nwidths = [100]\n[keys]\nx = "stop"\nq = "none"\n"ctrl+r" = "resume"\n'))
        self.assertEqual(errors, [])
        self.assertEqual(cfg["theme"]["ok"], {"fg": "#007A00"})
        self.assertEqual(cfg["theme"]["accent"], {"fg": 33, "bold": False})
        self.assertEqual(cfg["theme"]["screen"]["bg"], tui().PALETTES["paper"]["bg"])
        self.assertEqual((cfg["keys"]["x"], cfg["keys"]["^R"]), ("stop", "resume"))
        self.assertNotIn("q", cfg["keys"])
        text = tui().render(tui().State(), 100, 30, cfg).text()
        self.assertIn("┏", text)
        self.assertEqual(tui().DEFAULTS["keys"]["q"], "quit", "the defaults stay as they were")

    def test_each_bad_setting_is_one_line_and_the_rest_still_apply(self):
        cfg, errors = tui().load_config(self.write(
            'theme = "neon"\nborders = 1\n[colors]\nok = "green"\nnosuch = "#000000"\n'
            '[layout]\ncolumns = [["log", "nosuch"]]\nwidths = [0]\n[keys]\nx = "explode"\n"ctrl+" = "stop"\ny = "stop"\n'))
        self.assertEqual(len(errors), 8, "\n".join(errors))
        self.assertTrue(all("\n" not in e for e in errors))
        for words in ("neon", "borders", "green", "nosuch", "widths", "explode", "ctrl+"):
            self.assertTrue(any(words in e for e in errors), words)
        self.assertEqual(cfg["keys"]["y"], "stop")

    def test_print_config_prints_a_file_that_loads_back_the_same(self):
        cfg, errors = tui().load_config(self.write(tui().print_config(tui().DEFAULTS)))
        self.assertEqual(errors, [])
        self.assertEqual(cfg, tui().DEFAULTS)
        paper, _ = tui().load_config(self.write('theme = "paper"\n'))
        again, _ = tui().load_config(self.write(tui().print_config(paper)))
        self.assertEqual(again, paper)

    def test_an_unreadable_file_names_itself(self):
        path = self.write("theme = \n")
        cfg, errors = tui().load_config(path)
        self.assertEqual(len(errors), 1)
        self.assertIn(path, errors[0])
        self.assertEqual(cfg, tui().DEFAULTS)

    def test_signal_is_a_colour_blind_safe_theme(self):
        from sampo import smithtui
        cfg, errors = tui().load_config(self.write('theme = "signal"\n'))
        self.assertEqual(errors, [])
        ok, bad = (smithtui._rgb(cfg["theme"][n]["fg"]) for n in ("ok", "bad"))
        self.assertGreater(ok[2], ok[0], "pass is blue, not green")
        self.assertGreater(bad[0], bad[2], "fail is orange-red")
        self.assertGreater(abs(ok[2] - bad[2]) + abs(ok[0] - bad[0]), 250, "far apart for red-green blindness")

    def test_a_colour_too_faint_to_read_is_named_and_skipped(self):
        cfg, errors = tui().load_config(self.write('[colors]\ndim = "#18202C"\nok = "#7CFC8A"\n'))
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("colors.dim", errors[0])
        self.assertIn(":1", errors[0], "it says the contrast")
        self.assertNotEqual(cfg["theme"]["dim"].get("fg"), "#18202C", "the theme's own dim stays")
        self.assertEqual(cfg["theme"]["ok"]["fg"], "#7CFC8A", "a readable one applies")

    def test_a_hostile_name_in_the_file_never_reaches_the_screen_as_a_terminal_escape(self):
        # a shared tui.toml naming a style field, a table, a key or a layout setting with a title-setting OSC
        osc = r'"\u001b]0;x\u0007"'
        cfg, errors = tui().load_config(self.write(f'{osc} = 1\n[colors]\nok = {{ {osc} = true }}\n{osc} = "#000000"\n'
                                                   f'[layout]\n{osc} = 1\n[keys]\n{osc} = "stop"\n'))
        self.assertEqual(len(errors), 5, "\n".join(errors))
        for e in errors:
            self.assertFalse({"\x1b", "\x07"} & set(e), repr(e))
            self.assertIn("\\x1b]0;x\\x07", e, "named, escaped")

    def test_mono_has_no_colours(self):
        cfg, _ = tui().load_config(self.write('theme = "mono"\n'))
        self.assertFalse([n for n, spec in cfg["theme"].items() if n != "screen" and ("fg" in spec or "bg" in spec)])


class Settings(unittest.TestCase):
    """T-1496: one schema of every setting, and a writer that changes one key in tui.toml in place."""
    TEXT = ('# my settings\ntheme = "night"  # dark\nmotion = "full"\n\n[colors]  # styles\nok = "#47C98B"\n'
            '# keep this\n[layout]\ncolumns = [\n  ["log"],  # left\n  ["checks", "files"],\n]\nwidths = [58, 42]\n'
            '[keys]\nx = "stop"\n"ctrl+r" = "resume"  # mine\n')

    def path(self, text=None):
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), "tui.toml")
        if text is not None:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        return path

    def read(self, path):
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_every_default_has_a_schema_entry(self):
        from sampo import settings as ssettings
        keys = {e["key"] for e in ssettings.app_schema()}
        T = tui()
        names = {"preset": "theme", "motion": "motion", "border": "border", "live_theme": "live_theme"}  # T-3055
        for k in T.DEFAULTS:
            if k in names:
                self.assertIn(names[k], keys, k)
        self.assertTrue({f"colors.{n}" for n in T.DEFAULTS["theme"]} <= keys)
        self.assertTrue({f"layout.{n}" for n in T.DEFAULTS["layout"]} <= keys)
        self.assertTrue({f"keys.{T.key_name(k)}" for k in T.DEFAULTS["keys"]} <= keys)
        self.assertTrue({f"ext.{n}.{k}" for n, mine in T.DEFAULTS["ext"].items() for k in mine} <= keys)  # T-1657
        self.assertEqual(set(T.DEFAULTS) - {"preset", "theme", "layout", "keys", "ext"} - set(names), {"palette"})
        self.assertIn("palette", keys)
        for e in ssettings.app_schema():
            self.assertTrue(e["help"] and e["group"] and e["effect"] in ("now", "next run"), e)
            self.assertNotIn("\n", e["help"])

    def test_a_set_changes_one_line_and_keeps_the_rest_byte_identical(self):
        from sampo import settings as ssettings
        path = self.path(self.TEXT)
        self.assertEqual(ssettings.set_app("theme", "signal", path), ("signal", []))
        self.assertEqual(self.read(path), self.TEXT.replace('theme = "night"  # dark', 'theme = "signal"  # dark'))
        ssettings.set_app("keys.ctrl+r", "stop", path)
        self.assertIn('"ctrl+r" = "stop"  # mine\n', self.read(path))
        ssettings.set_app("colors.ok", "#4DA3FF", path)
        self.assertIn('ok = "#4DA3FF"\n# keep this\n', self.read(path))
        cfg, errors = tui().load_config(path)
        self.assertEqual((errors, cfg["preset"], cfg["keys"]["^R"]), ([], "signal", "stop"))
        _, skipped = ssettings.set_app("theme", "paper", path)  # the user's ok colour is too faint on paper
        self.assertEqual(len(skipped), 1)
        self.assertIn("colors.ok", skipped[0])

    def test_a_multiline_value_is_replaced_whole(self):
        from sampo import settings as ssettings
        path = self.path(self.TEXT)
        _, skipped = ssettings.set_app("layout.columns", '[["log", "files"]]', path)
        self.assertTrue(skipped and "layout.widths" in skipped[0], "two widths for one column: said, not refused")
        ssettings.set_app("layout.widths", "[100]", path)
        text = self.read(path)
        self.assertIn('[layout]\ncolumns = [["log", "files"]]\nwidths = [100]\n[keys]', text)
        self.assertEqual(tui().load_config(path)[1], [])

    def test_a_missing_key_goes_into_its_table_and_a_missing_table_is_added(self):
        from sampo import settings as ssettings
        path = self.path('# top\ntheme = "night"\n\n[keys]\nx = "stop"\n')
        ssettings.set_app("border", "heavy", path)
        ssettings.set_app("keys.y", "quit", path)
        ssettings.set_app("layout.narrow_below", "90", path)
        self.assertEqual(self.read(path), '# top\ntheme = "night"\nborder = "heavy"\n\n[keys]\nx = "stop"\ny = "quit"\n'
                                          '\n[layout]\nnarrow_below = 90\n')
        new = self.path()
        ssettings.set_app("motion", "reduced", new)
        self.assertEqual(self.read(new), 'motion = "reduced"\n')

    def test_odd_but_valid_files_are_edited_in_place(self):
        from sampo import settings as ssettings
        text = '"a".b = 1\r\n[keys]\r\nx = "stop" # c#d\r\n'
        self.assertEqual(ssettings.edit_toml(text, "keys", "x", "quit"), text.replace('"stop"', '"quit"'))
        real = self.path('motion = "full"\n')
        link = os.path.join(os.path.dirname(self.path()), "tui.toml")
        os.symlink(real, link)
        ssettings.set_app("motion", "calm", link)  # the old word, written as the one it means
        self.assertTrue(os.path.islink(link), "a linked tui.toml stays linked")
        self.assertEqual(self.read(real), 'motion = "reduced"\n')

    def test_each_line_keeps_its_own_ending(self):
        from sampo import settings as ssettings
        text = 'a = 1\r\nmotion = "full"\nb = 2\r\n[keys]\r\nx = "stop"\n'
        self.assertEqual(ssettings.edit_toml(text, None, "motion", "off"), text.replace('"full"', '"off"'))
        self.assertEqual(ssettings.edit_toml(text, "keys", "y", "quit"), text + 'y = "quit"\n')
        self.assertEqual(ssettings.edit_toml(text, None, "c", 3), text.replace('b = 2\r\n', 'b = 2\r\nc = 3\r\n'))

    def test_a_word_where_the_app_wants_one_never_raises(self):
        from sampo import settings as ssettings
        path = self.path('theme = ["x"]\nborder = {}\nmotion = [1]\npalette = {}\n')
        cfg, errors = tui().load_config(path)
        self.assertEqual(len(errors), 4, errors)
        ssettings.set_app("theme", "paper", path)  # a file the app chokes on can be repaired
        self.assertTrue(self.read(path).startswith('theme = "paper"\n'))

    def test_a_value_utf8_cant_hold_is_refused(self):
        from sampo import settings as ssettings
        path = self.path(self.TEXT)
        with self.assertRaises(ssettings.SettingError):
            ssettings.set_app("colors.ok", '"\udcff"', path)
        with self.assertRaises(ssettings.SettingError):
            ssettings.set_app("colors.ok", {"fg": "\udcff"}, path)
        self.assertEqual(self.read(path), self.TEXT)

    def test_the_write_holds_the_folders_lock(self):
        import contextlib
        from sampo import core
        from sampo import settings as ssettings
        path, held = self.path(self.TEXT), []

        @contextlib.contextmanager
        def lock(d, timeout=5.0):
            held.append(d)
            yield
        with unittest.mock.patch.object(core, "lock", lock):
            ssettings.set_app("border", "heavy", path)
        self.assertEqual(held, [os.path.dirname(os.path.realpath(path))])

    def test_a_bad_value_leaves_the_file(self):
        from sampo import settings as ssettings
        path = self.path(self.TEXT)
        for key, value in (("motion", "wobbly"), ("colors.dim", "#18202C"), ("nosuch", "1"), ("layout.widths", "[0]"),
                           ("keys.x", "explode"), ("layout.narrow_below", "lots")):
            with self.subTest(key=key), self.assertRaises(ssettings.SettingError):
                ssettings.set_app(key, value, path)
            self.assertEqual(self.read(path), self.TEXT)
        with unittest.mock.patch("sampo.smithtui.render", side_effect=RuntimeError("boom")), \
                self.assertRaises(ssettings.SettingError) as e:
            ssettings.set_app("border", "heavy", path)
        self.assertIn("boom", str(e.exception))
        self.assertEqual(self.read(path), self.TEXT)
        broken = self.path("theme = \n")
        with self.assertRaises(ssettings.SettingError):
            ssettings.set_app("border", "heavy", broken)
        self.assertEqual(self.read(broken), "theme = \n")


def git_repo():
    import subprocess
    import tempfile
    root = tempfile.mkdtemp()
    git = lambda *a: subprocess.run(["git", "-C", root, *a], check=True, capture_output=True)
    git("init", "-q")
    git("config", "user.email", "t@example.invalid")
    git("config", "user.name", "t")
    with open(os.path.join(root, "a.py"), "w") as f:
        f.write("x = 1\n")
    git("add", ".")
    git("commit", "-qm", "base")
    return root, git


class Review(unittest.TestCase):
    """T-1283's review: each finding as a test."""

    def test_the_diff_view_runs_git_without_a_planted_fsmonitor(self):
        root, git = git_repo()
        marker = os.path.join(root, "ran")
        git("config", "core.fsmonitor", f"touch {marker}; false")
        with open(os.path.join(root, "a.py"), "w") as f:
            f.write("x = 2\n")
        app = tui().App(tui().DEFAULTS, spawn=lambda *a, **k: None, root=root)
        app.key("d")
        self.assertFalse(os.path.exists(marker), "a worker's git config ran when d was pressed")
        self.assertIn("+x = 2", app.view_lines)

    def test_the_diff_view_goes_step_by_step_and_shows_new_files(self):
        root, _ = git_repo()
        with open(os.path.join(root, "a.py"), "w") as f:
            f.write("x = 2\n")
        with open(os.path.join(root, "b.py"), "w") as f:
            f.write("y = 1\n")
        app = tui().App(tui().DEFAULTS, spawn=lambda *a, **k: None, root=root)
        for e in RUN[:2] + [{"event": "changed", "text": "", "step": 1, "files": ["a.py"]},
                            {"event": "changed", "text": "", "step": 2, "files": ["a.py", "b.py"]}]:
            app.feed(e)
        app.key("d")
        lines = app.view_lines
        first, second = (next(i for i, x in enumerate(lines) if x.startswith(f"── step {n}")) for n in (1, 2))
        self.assertLess(first, lines.index("+x = 2"))
        self.assertLess(second, lines.index("+y = 1"), "the new file, under the step that made it")
        self.assertLess(lines.index("+x = 2"), second)

    def test_plain_text_carries_no_control_characters(self):
        e = {"kind": "action", "action": "write", "target": "a\x1b]52;c;aGk=\x07.py", "result": "ok"}
        self.assertNotIn("\x1b", tui().entry_text(e))
        self.assertNotIn("\x07", tui().entry_text({"kind": "say", "text": "hi\x07\x1b[2J"}))

    def test_a_resumed_run_keeps_its_plan_on_screen(self):
        st = Core.state(self, RUN[:6] + [{"event": "start", "text": "", "schema": 1, "request": "fix add",
                                          "task": "T-0001", "resume": True}, RUN[9]])
        self.assertEqual(st.plan.get("title"), "Make calc.add add")
        self.assertEqual([s["state"] for s in st.steps], ["done", "now"])
        self.assertEqual(st.criteria[0]["text"], "calc.add adds")

    def test_control_keys_reach_their_ctrl_bindings(self):
        self.assertEqual(tui().key_of("\x12"), "^R")
        self.assertEqual([tui().key_of(k) for k in ("\t", "\n", "\r", "\x1b", "q", "KEY_UP")],
                         ["\t", "\n", "\n", "\x1b", "q", "KEY_UP"])

    def test_the_run_is_stopped_when_the_screen_dies(self):
        app = tui().App(tui().DEFAULTS, spawn=lambda *a, **k: None)
        stopped = []
        app.stop = lambda: stopped.append(1)

        def wrapper(fn):
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            tui().run_screen(app, wrapper)
        self.assertEqual(stopped, [1])

    def test_plain_prints_the_plan_the_task_the_red_runs_and_why_a_command_was_refused(self):
        import io
        app = tui().App(tui().DEFAULTS, spawn=lambda *a, **k: None)
        for e in RUN + [{"event": "exit", "code": 0}]:
            app.events.put(e)
        out = io.StringIO()
        self.assertEqual(tui().run_plain(app, out), 0)
        text = out.getvalue()
        for words in ("Make calc.add add", "T-0001", "RED", "rm of your home", "closed"):
            self.assertIn(words, text)

    def test_a_panel_that_returns_bad_lines_says_so_and_the_screen_stays(self):
        tui().PANELS["broken"] = ("Broken", lambda st, w, h, app: ["not a line"])
        try:
            cfg = dict(tui().DEFAULTS, layout=dict(tui().DEFAULTS["layout"], columns=[["log"], ["broken"]]))
            text = tui().render(Core.state(self), 110, 30, cfg).text()
        finally:
            del tui().PANELS["broken"]
        self.assertIn("panel broken failed", text)

    def test_line_counts_name_a_non_ascii_file_once_and_stop_reading_a_big_one(self):
        from sampo import smith
        root, git = git_repo()
        with open(os.path.join(root, "café.py"), "w") as f:
            f.write("a = 1\n")
        git("add", ".")
        git("commit", "-qm", "café")
        with open(os.path.join(root, "café.py"), "w") as f:
            f.write("a = 2\nb = 3\n")
        with open(os.path.join(root, "big.txt"), "w") as f:
            f.write("x\n" * 100)
        with unittest.mock.patch.object(smith, "NUMSTAT_CAP", 10):
            stat = smith._numstat(root, ["café.py", "big.txt"])
        self.assertEqual(stat, {"café.py": [2, 1], "big.txt": [5, 0]})

    def test_s_stops_the_run_and_r_resumes_its_task(self):
        class Child:
            pid, code = 999999, None

            def poll(self):
                return self.code
        spawned, child = [], Child()
        app = tui().App(tui().DEFAULTS, spawn=lambda words, opts: spawned.append((words, opts)) or child)
        app.start(["fix add"])
        for e in RUN[:5]:
            app.feed(e)
        killed = []
        with unittest.mock.patch.object(os, "killpg", lambda pid, sig: killed.append(pid)):
            app.key("s")
            self.assertEqual(killed, [], "T-1348: a live run asks for a second press")
            app.key("s")
        self.assertEqual(killed, [999999])
        child.code = -15
        app.key("r")
        self.assertEqual(spawned[-1], ([], {"resume": "T-0001"}))


SAMPLE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates", "tui", "cost_meter.py")


class Extensions(unittest.TestCase):
    def setUp(self):
        t = tui()
        self.live = (t.PANELS, t.COMMANDS, t.HANDLERS, t.EXT_COMMANDS, t.EXT_LABELS, t.DEFAULTS["keys"])
        self.saved = [type(x)(x) for x in self.live]
        self.saved[2] = {k: list(v) for k, v in t.HANDLERS.items()}

    def tearDown(self):
        for live, saved in zip(self.live, self.saved):
            live.clear()
            live.update(saved) if isinstance(live, dict) else live.extend(saved)

    def folder(self, **files):
        import tempfile
        d = tempfile.mkdtemp()
        for name, body in files.items():
            with open(os.path.join(d, name), "w", encoding="utf-8") as f:
                f.write(body)
        return d

    def test_the_sample_adds_a_panel_a_command_and_an_event_handler(self):
        import shutil
        d = self.folder()
        shutil.copy(SAMPLE, d)
        self.assertEqual(tui().load_extensions(d), [])
        cfg, errors = tui().load_config(Config.write(self, '[layout]\ncolumns = [["log"], ["cost"]]\nwidths = [60, 40]\n'))
        self.assertEqual(errors, [], "the layout can name the extension's panel")
        app = tui().App(cfg, spawn=lambda *a, **k: None)
        for e in RUN:
            app.feed(e)
        text = tui().render(app.state, 110, 32, cfg, app).text()
        self.assertIn("$0.0016", text)
        self.assertIn("1 model call", text, "its handler counted the tokens events")
        app.key("c")
        self.assertIn("$0.0048", tui().render(app.state, 110, 32, cfg, app).text())
        app.key("?")
        self.assertIn("the cost panel's next price", tui().render(app.state, 110, 32, cfg, app).text())

    def test_a_failing_extension_says_why_and_the_others_still_load(self):
        d = self.folder(**{"a_bad.py": "def setup(api):\n    raise RuntimeError('boom')\n",
                           "b_good.py": "def setup(api):\n    api.panel('hello', 'Hello', lambda st, w, h, app: [[('hi', 'ok')]])\n"
                                        "    api.on('*', lambda st, e: 1 / 0)\n"})
        errors = tui().load_extensions(d)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("a_bad.py", errors[0])
        self.assertIn("boom", errors[0])
        self.assertIn("hello", tui().PANELS)
        st = tui().State()
        for e in RUN:
            st.apply(e)
        self.assertEqual(st.status, "closed", "a handler that throws doesn't stop the run's view")
        self.assertEqual(sum("ZeroDivisionError" in str(n.get("text")) for n in st.log), 1, "said once, then dropped")

    def budgets(self, **values):
        """T-1315: tiny budgets so a short sleep is over them."""
        for name, value in values.items():
            p = unittest.mock.patch.object(tui(), name, value)
            p.start()
            self.addCleanup(p.stop)

    def app(self, panel="slow"):
        cfg, errors = tui().load_config(Config.write(self, f'[layout]\ncolumns = [["log"], ["{panel}"]]\nwidths = [60, 40]\n'))
        self.assertEqual(errors, [])
        return tui().App(cfg, spawn=lambda *a, **k: None), cfg

    def notes(self, app):
        return [str(n.get("text")) for n in app.state.log if n.get("kind") == "error"]

    def test_a_command_that_raises_is_said_in_activity_and_switched_off(self):
        calls = []

        def bad(app):
            calls.append(1)
            raise RuntimeError("kaboom")
        tui().API().command("bad_cmd", bad, key="b")
        app, _ = self.app("checks")
        app.key("b")
        app.key("b")
        self.assertEqual(len(calls), 1, "switched off after the first failure")
        self.assertEqual(len(self.notes(app)), 1, self.notes(app))
        self.assertIn("command bad_cmd failed: RuntimeError: kaboom; switched off", self.notes(app)[0])
        app.key("?")  # the screen keeps running: a built-in still works
        self.assertEqual(app.mode, "help")

    def test_a_slow_command_is_switched_off_with_how_long_it_took(self):
        self.budgets(COMMAND_BUDGET=0.001)
        calls = []
        tui().API().command("slow_cmd", lambda app: (calls.append(1), time.sleep(0.01)), key="b")
        app, _ = self.app("checks")
        app.key("b")
        app.key("b")
        self.assertEqual(len(calls), 1)
        note = self.notes(app)[0]
        self.assertIn("command slow_cmd took", note)
        self.assertIn("ms; switched off", note)

    def test_a_slow_handler_is_switched_off_once(self):
        self.budgets(HANDLER_BUDGET=0.001)
        calls = []
        tui().API().on("*", lambda st, e: (calls.append(1), time.sleep(0.01)))
        st = tui().State()
        for e in RUN:
            st.apply(e)
        self.assertEqual(len(calls), 1)
        self.assertEqual(st.status, "closed")
        notes = [str(n.get("text")) for n in st.log if n.get("kind") == "error"]
        self.assertEqual(len(notes), 1, notes)
        self.assertIn("ms; switched off", notes[0])

    def test_a_slow_panel_is_switched_off_after_three_frames_running(self):
        self.budgets(PANEL_BUDGET=0.001)
        calls, slow = [], [True]

        def panel(st, w, h, app):
            calls.append(1)
            if slow[0]:
                time.sleep(0.01)
            return [[("tick", "ok")]]
        tui().API().panel("slow", "Slow", panel)
        app, cfg = self.app()
        tui().render(app.state, 110, 32, cfg, app)
        tui().render(app.state, 110, 32, cfg, app)
        slow[0] = False
        tui().render(app.state, 110, 32, cfg, app)  # a fast frame resets the count
        slow[0] = True
        for _ in range(2):
            tui().render(app.state, 110, 32, cfg, app)
        self.assertEqual(self.notes(app), [], "not three frames running yet")
        tui().render(app.state, 110, 32, cfg, app)
        self.assertEqual(len(calls), 6)
        text = tui().render(app.state, 110, 32, cfg, app).text()
        self.assertEqual(len(calls), 6, "switched off: not called again")
        self.assertIn("switched off", text)
        self.assertEqual(len(self.notes(app)), 1)
        self.assertIn("panel slow took", self.notes(app)[0])

    def test_a_slow_built_in_panel_is_never_switched_off(self):
        self.budgets(PANEL_BUDGET=-1.0)  # every frame is over it
        app, cfg = self.app("checks")
        for e in RUN:
            app.feed(e)
        for _ in range(5):
            text = tui().render(app.state, 110, 32, cfg, app).text()
        self.assertNotIn("switched off", text)
        self.assertEqual(self.notes(app), [])

    def test_a_file_others_can_change_is_skipped(self):
        d = self.folder(**{"open.py": "def setup(api):\n    api.panel('open', 'Open', lambda st, w, h, app: [])\n"})
        os.chmod(os.path.join(d, "open.py"), 0o666)
        errors = tui().load_extensions(d)
        self.assertIn("another user can change it", errors[0])
        self.assertNotIn("open", tui().PANELS)


GOLDEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "golden")
GOLDEN_AT = (0.0, 3.4, 9.1, 30.0)  # seconds into the demo's 30 s playback, from planning to closed


def golden_frame(at, size=(120, 34)):
    """The demo run played to `at` seconds: each row's text plus a digest of every cell's resolved look, so a change
    to any panel's words or colours shows as a diff."""
    import hashlib
    T = tui()
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    with open(os.path.join(root, "demo", "code-tui.jsonl"), encoding="utf-8") as f:
        events = [json.loads(line) for line in f if line.strip()]
    app = T.App(T.DEFAULTS, spawn=lambda *a, **k: None)
    gap = 30.0 / max(1, len(events))
    fed, out = 0, []
    for k in range(int(at * 10) + 1):  # ten frames a second, so motion has the same history at every instant
        t = k / 10
        while fed < len(events) and fed * gap <= t:
            app.feed(events[fed])
            fed += 1
        cv = T.render(app.state, size[0], size[1], T.DEFAULTS, app, t=1000.0 + t)
    for y, row in enumerate(cv.cells):
        looks = "|".join(repr(sorted(T.resolve(s, T.DEFAULTS, cv.bg[y][x]).items())) for x, (_, s) in enumerate(row))
        out.append("".join(ch for ch, _ in row).rstrip() + "  ⟨" + hashlib.sha1(looks.encode()).hexdigest()[:10] + "⟩")
    return "\n".join(out) + "\n"


class Export(unittest.TestCase):
    def test_snapshot_scrubs_secrets(self):
        # T-1302: a token a worker printed must not land in the exported file
        import tempfile
        from sampo import smithtui
        secret = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2"
        events = [{"event": "start", "text": "", "request": f"deploy with {secret}", "ts": 1.0},
                  {"event": "say", "text": f"found {secret} in output", "ts": 2.0}]
        with tempfile.TemporaryDirectory() as d:
            path = smithtui.snapshot(events, os.path.join(d, "s.svg"), (100, 20))
            with open(path, encoding="utf-8") as f:
                body = f.read()
        self.assertTrue(body.startswith("<svg"))
        self.assertIn("deploy with [REDACTED]", body)
        self.assertNotIn(secret, body)
        self.assertNotIn(secret[:12], body)

    def test_record_and_plain_replay_scrub_secrets(self):
        # T-1302: the video frames and the plain replay lines are built from scrubbed text too
        import io
        import tempfile
        from sampo import smithtui
        secret = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2"
        events = [{"event": "start", "text": "", "request": f"deploy with {secret}", "ts": 1.0},
                  {"event": "say", "text": f"found {secret} in output", "ts": 2.0}]
        drawn = []
        real = smithtui.to_svg
        with unittest.mock.patch.object(smithtui.shutil, "which", return_value="/bin/x"), \
                unittest.mock.patch.object(smithtui.subprocess, "run"), \
                unittest.mock.patch.object(smithtui, "to_svg", lambda *a, **k: drawn.append(real(*a, **k)) or drawn[-1]):
            smithtui.record(events, "out.gif", (100, 20), fps=2, seconds=1, hold=0)
        self.assertTrue(drawn)
        self.assertNotIn(secret[:12], "".join(drawn))
        self.assertIn("[REDACTED]", "".join(drawn))
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "e.jsonl")
            with open(src, "w", encoding="utf-8") as f:
                f.write("".join(json.dumps(e) + "\n" for e in events))
            out = io.StringIO()
            smithtui.replay_file(src, smithtui.DEFAULTS, True, out)
        self.assertNotIn(secret[:12], out.getvalue())
        self.assertIn("[REDACTED]", out.getvalue())


class Golden(unittest.TestCase):
    """T-1306: the demo run at fixed instants, compared with plugin/tests/fixtures/golden. A deliberate change to the
    screen rewrites them: FM_GOLDEN=regen python3 plugin/tests/run.py test_code_tui."""

    def test_the_demo_run_draws_as_recorded(self):
        for at in GOLDEN_AT:
            path = os.path.join(GOLDEN, f"demo-{at:04.1f}s.txt")
            got = golden_frame(at)
            if os.environ.get("FM_GOLDEN") == "regen":
                os.makedirs(GOLDEN, exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(got)
            with self.subTest(at=at):
                with open(path, encoding="utf-8") as f:
                    want = f.read()
                self.assertEqual(got, want, f"the screen at {at}s changed: if that's meant, FM_GOLDEN=regen")


class Fuzz(unittest.TestCase):
    """T-1306: a newer fm code, a hand-edited events file or a bug can send any event; the screen never falls over."""
    KINDS = ["start", "plan", "task", "step", "say", "tokens", "action", "worker", "revert", "fix", "verify", "changed",
             "closed", "blocked", "error", "answer", "resume", "exit", "nonsense", "__init__", None, 5]
    VALUES = [None, "", "x", "日本語テキスト", "é", "\x1b[31mred\x07", "a" * 3000, 0, -1, 10 ** 12, 3.5, [], {},
              [1, 2], {"a": 1}, True, "\n\n\t", " \n ", float("nan"), float("inf"), float("-inf"), 1e300]
    FIELDS = ["text", "plan", "step", "criterion", "label", "exit", "cmd", "output", "files", "stat", "total_in",
              "total_out", "call_in", "call_out", "action", "target", "result", "detail", "tokens", "task", "worker",
              "ts", "request", "model", "reason", "file", "round", "schema", "verify", "resume", "code", "stderr"]
    SIZES = ((1, 1), (10, 5), (40, 12), (80, 24), (132, 34), (200, 60))

    def event(self, r):
        e = {"event": r.choice(self.KINDS)}
        for k in r.sample(self.FIELDS, r.randint(0, 8)):
            e[k] = r.choice(self.VALUES)
        if r.random() < .3:
            e["plan"] = {"type": r.choice(["FIX", None, 3]), "tier": r.choice(["S", "M", None]),
                         "title": r.choice(self.VALUES), "steps": r.choice([[], ["a", "b"], None, [1, None], "s"]),
                         "criteria": r.choice([[], [{"text": r.choice(self.VALUES), "verify": r.choice(self.VALUES)}],
                                               "x", None, [None]])}
        if r.random() < .3:
            e["stat"] = r.choice([{"a.py": [1, 2]}, {"b": "x"}, {"c": [None, "x"]}, [], {1: [1, 1]}])
        if r.random() < .5:  # mostly sane times, sometimes out of order
            e["ts"] = 1000 + r.uniform(-50, 300)
        return e

    def test_any_event_stream_draws_at_any_size(self):
        import random
        T = tui()
        for seed in range(300):
            r = random.Random(seed)  # a failure names its seed; Random(seed) replays it
            events = [self.event(r) for _ in range(r.randint(0, 40))]
            app = T.App(T.DEFAULTS, spawn=lambda *a, **k: None)
            for e in events:
                app.feed(e)
            for name in T.PANELS:  # T-1926: a panel that throws draws one "failed" line; none may need it
                row = (T._draw(name, app.state, 80, 24, app) or [[]])[0]
                self.assertFalse(row and str(row[0][0]).startswith(f"panel {name} failed"), (seed, row))
            for w, h in self.SIZES:
                with self.subTest(seed=seed, size=(w, h)):
                    for t in (100.0, 100.3, 101.7):
                        cv = T.render(app.state, w, h, T.DEFAULTS, app, t=t)
                    self.assertTrue(T.to_svg(cv, T.DEFAULTS).startswith("<svg"))
                    self.assertEqual(len(cv.cells), h)

    def test_a_malformed_event_changes_nothing(self):
        T = tui()
        st = Core.state(self)
        before = repr(vars(st))
        for e in ({"event": "tokens", "total_in": "lots"}, {"event": "step", "step": "two"},
                  {"event": "verify", "criterion": [1]}, {"event": "changed", "files": 5},
                  {"event": "closed", "tokens": ["a", 1]}, {"event": "exit", "code": 1, "stderr": " \n "}):
            st.apply(e)
            self.assertEqual(repr(vars(st)), before, e)
        with self.assertRaises(ValueError, msg="a newer stream is still said, not skipped"):
            st.apply({"event": "start", "schema": T.SCHEMA + 1})
        self.assertEqual(repr(vars(st)), before)

    def test_odd_values_that_are_kept_still_draw(self):
        T = tui()
        st = T.State()
        for e in ({"event": "start", "request": ["a", 1], "model": 5, "worker": {"x": 1}, "task": 7, "ts": float("nan")},
                  {"event": "say", "text": 3, "ts": float("inf")}, {"event": "step", "step": 1, "ts": float("-inf")}):
            st.apply(e)
        self.assertIsNone(st.first_ts, "a time that isn't a finite number isn't a time")
        T.render(st, 80, 24, T.DEFAULTS, None, t=5.0)


HOSTILE = {  # T-1516: what a terminal draws in an odd number of columns, or none
    "cjk": "日本語テキスト漢字かな", "emoji": "🚀🔥 ok 👍🏽 ❤️ ✔️", "combining": "éé̂ ạ̈ ́lead",
    "rtl": "שלום עולם مرحبا ‮abc‬", "zerowidth": "a​b‍c⁠d﻿e­f",
    "control": "a\x00b\x07c\x1b[31md\x7fe\x85f\r\n\tg", "variation": "x️y︎ ❤️", "mix": "日́本🚀‍🔥 שׁ",
    "long": "日本語🚀" * 60,
}
CRASH_MAX = 50
CRASHES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "crashes")


def column_width(cell):
    """Columns a terminal gives one canvas cell, by its own rule (not fmcodetui's): wide 2, marks and format 0."""
    import unicodedata
    return 2 if unicodedata.east_asian_width(cell[0]) in "WF" or ("\ufe0f" in cell and unicodedata.category(cell[0]) == "So") else 1


class HostileWidth(unittest.TestCase):
    """T-1516: every panel and view, fed every hostile string, draws rows that fill exactly their width. A crash saves
    the case under fixtures/crashes (and a file there replays: test_a_saved_crash_replays)."""
    SIZES = ((20, 12), (40, 16), (80, 24), (121, 34))

    def events(self, s):
        e = [{"event": "start", "request": s, "model": s, "worker": s, "schema": 1, "ts": 1000},
             {"event": "plan", "plan": {"type": "FIX", "tier": "S", "title": s, "steps": [s, s],
                                        "criteria": [{"text": s, "verify": s}]}},
             {"event": "task", "task": s}, {"event": "step", "step": 1, "text": s},
             {"event": "say", "text": s}, {"event": "tokens", "total_in": 10, "total_out": 5, "call_in": 1, "call_out": 1},
             {"event": "action", "action": s, "target": s, "result": s, "detail": s},
             {"event": "verify", "criterion": 1, "label": s, "exit": 1, "cmd": s, "output": s},
             {"event": "changed", "step": 1, "files": [s], "stat": {s: [1, 2]}},
             {"event": "error", "text": s, "next": s}, {"event": "blocked", "text": s, "reason": s, "next": s}]
        return e

    def screens(self, s):
        """(name, app) for the main screen with each panel on its own, and each view with s in its rows."""
        T = tui()
        out = []
        for name in T.PANELS:
            cfg = dict(T.DEFAULTS, layout={"columns": [[name]], "widths": [1], "heights": [[1]]})
            out.append((f"panel {name}", cfg, "main", {}))
        extra = {"history": dict(items=[{"status": "closed", "task": s, "request": s, "ts": 1760000000}]),
                 "picker": dict(items=[("model", s), ("worker", s)]),
                 "home": dict(items=[{"project": s, "root": s, "exists": False}, {"project": s, "error": s},
                                     {"project": s, "exists": True, "active": {"id": s, "title": s}}]),
                 "project": dict(items=[{"kind": "error", "text": s}, {"kind": "run", "status": "x", "task": s,
                                                                     "request": s},
                                        {"section": "queue", "task": {"id": s, "type": s, "tier": s, "title": s,
                                                                      "reason": s}}]),
                 "diff": dict(view_lines=["+" + s, "-" + s, "@@ " + s, s]),
                 "tickets": dict(tickets=[s, s], items=[(0, s, True), (1, s, False)]),  # T-1350
                 "looks": dict(look_answers=[], look_pairs=[(s, s)], items=[(s, s)]),
                 "backlog": dict(items=[{"id": s, "title": s, "why": s}, {"id": s, "title": s, "why": ""}]),  # T-1856
                 "help": {},
                 "breakout": dict(game=__import__("sampo.extras").extras.Breakout(), game_t=0.0),  # T-1556
                 "settings": dict(set_rows=[1, 2], set_query=s, set_note=(s, "warn"), items=[
                     {"head": s, "count": 1, "surface": "app"},
                     {"key": s, "value": s, "default": s, "group": "colors", "surface": "app", "help": s, "choices": [s]}]),
                 "layout": dict(set_note=(s, "warn")),
                 "runners": dict(set_note=(s, "warn"), items=[{"name": s, "installed": True, "work": s,
                                                     "result": {"ok": False, "said": s}}])}
        self.assertEqual(set(extra), set(T.VIEWS), "a new view must join the sweep")
        for mode, attrs in extra.items():
            out.append((f"view {mode}", T.DEFAULTS, mode, attrs))
        lay = dict(T.DEFAULTS, layout={"columns": [[s, "log"], ["track"]], "widths": [1, 1], "heights": [[1, 1], [1]]})
        out.append(("view layout", lay, "layout", extra["layout"]))
        out.append(("prompt", T.DEFAULTS, "prompt", dict(input=s)))
        return out

    def rows_of(self, cv):
        bad = []
        for y, row in enumerate(cv.cells):
            cols = sum(column_width(c) if c else 0 for c, _ in row)
            if cols != cv.w:
                bad.append((y, cols))
            for x, (c, _) in enumerate(row):  # a wide character is followed by its empty half, and only then
                if c == "" and not (x and row[x - 1][0] and column_width(row[x - 1][0]) == 2):
                    bad.append((y, f"orphan half at {x}"))
        return bad

    def sweep(self, label, s, size, cfg, mode, attrs):
        T = tui()
        app = T.App(cfg, spawn=lambda *a, **k: None)
        try:
            for e in self.events(s):
                app.feed(e)
            for k, v in attrs.items():
                setattr(app, k, v)
            app.mode = mode
            cv = T.render(app.state, size[0], size[1], cfg, app, t=100.0)
        except Exception as ex:
            import hashlib
            blob = {"label": label, "text": s, "size": list(size), "mode": mode, "error": f"{type(ex).__name__}: {ex}"}
            # the name keys on what crashed, not on the message or the text: one bug is one file, and at most CRASH_MAX
            key = json.dumps([label, list(size), mode, type(ex).__name__])
            name = hashlib.sha1(key.encode()).hexdigest()[:10]
            os.makedirs(CRASHES, exist_ok=True)  # the crash replays: test_a_saved_crash_replays
            if len([f for f in os.listdir(CRASHES) if f.endswith(".json")]) < CRASH_MAX:
                with open(os.path.join(CRASHES, f"{name}.json"), "w", encoding="utf-8") as f:
                    json.dump(blob, f, ensure_ascii=False, indent=1)
            raise
        return cv

    # what a screen must show of each string at the widest size (a hostile string that never reached the output
    # proves nothing), and the screens that never show a run's text
    PROBE = {"cjk": "日", "emoji": "🚀", "combining": "lead", "rtl": "ש", "zerowidth": "a b c d", "control": "a b c",
             "variation": "y︎", "mix": "🚀", "long": "日本語🚀"}
    NO_TEXT = {"panel tokens", "panel time", "view help"}

    def test_every_panel_and_view_fills_its_width_for_each_hostile_string(self):
        seen = 0
        for key, s in HOSTILE.items():
            for label, cfg, mode, attrs in self.screens(s):
                for size in self.SIZES:
                    with self.subTest(string=key, screen=label, size=size):
                        cv = self.sweep(label, s, size, cfg, mode, attrs)
                        self.assertEqual(self.rows_of(cv), [])
                        if size == self.SIZES[-1] and label not in self.NO_TEXT:
                            seen += 1
                            self.assertIn(self.PROBE[key], cv.text(), "the string never reached the screen")
        self.assertGreater(seen, 100)

    def test_a_saved_crash_replays(self):
        saved = sorted(f for f in (os.listdir(CRASHES) if os.path.isdir(CRASHES) else []) if f.endswith(".json"))
        for f in saved:  # each file is a bug not yet fixed: it fails here until it renders
            with open(os.path.join(CRASHES, f), encoding="utf-8") as fh:
                c = json.load(fh)
            screens = {x[0]: x for x in self.screens(c["text"])}
            with self.subTest(crash=f):
                self.assertIn(c["label"], screens, f"{f}: no screen is called {c['label']} any more: delete or rename it")
                label, cfg, mode, attrs = screens[c["label"]]
                self.assertEqual(self.rows_of(self.sweep(label, c["text"], tuple(c["size"]), cfg, mode, attrs)), [])

    def test_a_crash_writes_a_fixture(self):
        import shutil
        import tempfile
        T = tui()
        global CRASHES
        old, CRASHES = CRASHES, tempfile.mkdtemp()
        try:
            with unittest.mock.patch.object(T, "render", side_effect=RuntimeError("boom")):
                with self.assertRaises(RuntimeError):
                    label, cfg, mode, attrs = self.screens("x")[0]
                    self.sweep(label, "x", (20, 12), cfg, mode, attrs)
            self.assertEqual(len(os.listdir(CRASHES)), 1)
            with open(os.path.join(CRASHES, os.listdir(CRASHES)[0]), encoding="utf-8") as f:
                self.assertEqual(json.load(f)["error"], "RuntimeError: boom")
        finally:
            shutil.rmtree(CRASHES)
            CRASHES = old


class Columns(unittest.TestCase):
    """T-1516: widths as kitty, WezTerm, iTerm2 and Windows Terminal draw them, and Canvas.put over a wide cell."""
    TABLE = [("日", 2), ("é", 1), ("👍🏽", 2), ("❤️", 2), ("❤", 1), ("1️", 1), ("a​b", 3),
             ("́", 0), ("日本語", 6), ("שׁ", 1), ("x️", 1), ("⃝", 0)]

    def test_width_matches_real_terminals(self):
        T = tui()
        for s, want in self.TABLE:
            with self.subTest(s=s):
                self.assertEqual(T.width(s), want)

    def test_fit_and_tail_keep_to_their_columns_with_vs16(self):
        T = tui()
        for s in ("❤️" * 5, "a❤️b❤️"):
            for n in range(0, 12):
                with self.subTest(s=s, n=n):
                    self.assertLessEqual(T.width(T.fit(s, n)), n)
                    self.assertLessEqual(T.width(T.tail(s, n)), n)

    def row(self, cv):
        return [c for c, _ in cv.cells[0]]

    def test_put_over_half_a_wide_character_blanks_the_other_half(self):
        T = tui()
        cv = T.Canvas(4, 1)
        cv.put(0, 0, "日本")
        cv.put(0, 1, "x")
        self.assertEqual(self.row(cv), [" ", "x", "本", ""])
        cv = T.Canvas(4, 1)
        cv.put(0, 0, "日本")
        cv.put(0, 2, "y")
        self.assertEqual(self.row(cv), ["日", "", "y", " "])
        cv = T.Canvas(4, 1)
        cv.put(0, 0, "日本")
        cv.put(0, 1, "語")
        self.assertEqual(self.row(cv), [" ", "語", "", " "])

    def test_a_wide_character_that_does_not_fit_leaves_no_orphan(self):
        T = tui()
        cv = T.Canvas(4, 1)
        cv.put(0, 0, "日本")
        cv.put(0, 1, "語", right=2)
        self.assertEqual(self.row(cv), [" ", " ", "本", ""])

    def test_vs16_widens_an_emoji_base_and_not_a_digit(self):
        T = tui()
        cv = T.Canvas(4, 1)
        cv.put(0, 0, "❤️a1️")
        self.assertEqual(self.row(cv), ["❤️", "", "a", "1️"])
        cv = T.Canvas(3, 1)
        self.assertEqual(cv.put(0, 0, "ab❤️"), 3)  # no room for the second column: the heart is dropped
        self.assertEqual(self.row(cv), ["a", "b", " "])


class Home(unittest.TestCase):
    """T-1330: fm code opens the app: home lists every Foreman project, a project's view its work and past runs, and a
    run starts in that project's folder."""
    PROJECTS = [
        {"project": "old", "root": "/w/old", "exists": True, "active": None, "queue": 0, "inbox": 1, "blocked": 0,
         "updated": 100.0},
        {"project": "new", "root": "/w/new", "exists": True, "active": {"id": "T-0002", "title": "fix login"},
         "queue": 1, "inbox": 1, "blocked": 1, "updated": 200.0},
        {"project": "gone", "root": "/w/gone", "exists": False, "updated": 150.0},
    ]
    STATE = {"active": {"id": "T-0002", "type": "FIX", "tier": "S", "title": "fix login"},
             "queue": [{"id": "T-0003", "type": "FEATURE", "tier": "S", "title": "add export"}],
             "inbox": [{"id": "T-0004", "type": "FIX", "tier": "S", "title": "typo in help"}],
             "blocked": [{"id": "T-0005", "type": "FIX", "tier": "M", "title": "flaky upload",
                          "reason": "needs a device"}]}

    def app(self, state=None):
        T = tui()
        self.spawned = []
        app = T.App(T.DEFAULTS, spawn=lambda words, opts: self.spawned.append((words, app.root)),
                    projects=lambda: [dict(p) for p in self.PROJECTS],
                    project_state=state or (lambda p: dict(self.STATE, code_dir=None)))
        return T, app

    def text(self, T, app):
        return T.render(app.state, 120, 34, T.DEFAULTS, app, t=100.0).text()

    def test_home_lists_every_project_newest_first_and_a_missing_one_last_saying_why(self):
        T, app = self.app()
        app.show_home()
        self.assertEqual([p["project"] for p in app.items], ["new", "old", "gone"], "a missing folder last")
        text = self.text(T, app)
        for want in ("new", "fix login", "gone", "folder missing", "old"):
            self.assertIn(want, text)

    def test_ranked_by_need(self):
        """T-1344: asks/blocked > failed run > running > queue age (longest waiting first), a missing folder last;
        the note says what changed since the home was last opened."""
        P = lambda n, **k: dict({"project": n, "root": f"/w/{n}", "exists": True, "queue": 0, "updated": 100.0}, **k)  # noqa
        self.PROJECTS = [P("idle", updated=900.0), P("queued-new", queue=1, updated=800.0),
                         P("queued-old", queue=1, updated=50.0), P("running", active={"id": "T-1", "title": "go"}),
                         P("failed", failed=True), P("asks", waits=2), P("blocked", blocked=1, updated=1.0),
                         P("gone", exists=False, waits=9)]
        T, app = self.app()
        app.home_seen = 120.0
        app.show_home()
        self.assertEqual([p["project"] for p in app.items],
                         ["asks", "blocked", "failed", "running", "queued-old", "queued-new", "idle", "gone"])
        self.assertIn("changed since you last looked: queued-new, idle", app.home_note)
        self.assertIn("changed since", self.text(T, app))
        app.show_home()  # opened again: nothing moved since
        self.assertIn("nothing changed", app.home_note)

    def test_prefix_and_jump(self):
        """T-1344: a unique prefix or folder name picks a project, an ambiguous one names the matches; typing a name
        in the home jumps to it."""
        rows = [("alpha-1f2e", "/w/alpha"), ("alpine-3a4b", "/w/alpine"), ("beta-9c8d", "/x/zeta")]
        m = c.match_project
        self.assertEqual(m("beta-9c8d", rows), [rows[2]])
        self.assertEqual(m("bet", rows), [rows[2]])
        self.assertEqual(m("zeta", rows), [rows[2]], "the folder's name")
        self.assertEqual(m("alp", rows), rows[:2], "ambiguous: both named")
        self.assertEqual(m("nope", rows), [])
        self.PROJECTS = [{"project": n, "root": r, "exists": True, "updated": float(i)}
                         for i, (n, r) in enumerate(rows)]
        T, app = self.app()
        app.show_home()
        for ch in "/bet":
            app.key(ch)
        self.assertEqual(app.items[app.item_index]["project"], "beta-9c8d")
        app.key("KEY_BACKSPACE")
        app.key("KEY_BACKSPACE")
        app.key("KEY_BACKSPACE")
        for ch in "alpi":
            app.key(ch)
        self.assertEqual(app.items[app.item_index]["project"], "alpine-3a4b")
        self.assertEqual(app.mode, "home")

    def test_a_project_view_lists_its_work_and_runs_a_queued_task_there(self):
        T, app = self.app()
        app.show_home()
        app.key("\n")  # opens "new"
        text = self.text(T, app)
        for want in ("fix login", "add export", "typo in help", "flaky upload", "needs a device"):
            self.assertIn(want, text)
        app.item_index = next(i for i, it in enumerate(app.items) if (it.get("task") or {}).get("id") == "T-0003")
        app.key("r")
        self.assertEqual(self.spawned, [(["--task", "T-0003"], "/w/new")])

    def test_n_in_a_project_starts_a_new_request_in_its_folder(self):
        T, app = self.app()
        app.show_home()
        app.key("\n")
        app.key("n")
        for ch in "add a flag":
            app.key(ch)
        app.key("\n")
        self.assertEqual(self.spawned, [(["add a flag"], "/w/new")])

    def test_a_project_whose_state_cant_be_read_says_why_and_home_still_draws(self):
        def broken(p):
            raise RuntimeError("state looks corrupt")
        T, app = self.app(state=broken)
        app.show_home()
        app.key("\n")
        self.assertIn("state looks corrupt", self.text(T, app))
        app.key("\x1b")
        self.assertIn("old", self.text(T, app))

    def test_a_missing_folder_never_starts_a_run(self):
        T, app = self.app()
        app.show_home()
        app.item_index = [p["project"] for p in app.items].index("gone")
        app.key("\n")
        self.assertIn("folder missing", self.text(T, app))
        app.key("n")
        app.key("r")
        self.assertEqual(self.spawned, [])
        self.assertIn("folder", app.message)

    def test_browsing_never_moves_the_runs_folder(self):
        # a resume (r) or diff (d) on the run screen stays with the run's own project
        T, app = self.app()
        app.root = "/w/run"
        app.key("p")
        app.key("\n")
        app.key("\x1b")
        app.key("\x1b")
        self.assertEqual((app.mode, app.root), ("main", "/w/run"))

    def test_esc_in_the_request_box_goes_back_to_the_project(self):
        T, app = self.app()
        app.show_home()
        app.key("\n")
        app.key("n")
        app.key("\x1b")
        self.assertEqual(app.mode, "project")
        self.assertEqual(self.spawned, [])

    def test_p_opens_home_from_a_run(self):
        T, app = self.app()
        app.key("p")
        self.assertEqual(app.mode, "home")


class Delight(unittest.TestCase):  # T-1349
    def test_a_projects_first_close_is_noted_once_and_a_picked_runner_says_hello(self):
        from sampo import smith
        T = tui()
        app = T.App(T.DEFAULTS, spawn=lambda *a, **k: None, choices=lambda: [("worker", "claude")])
        app.feed({"event": "start", "request": "fix add", "schema": 1})
        app.feed({"event": "closed", "text": "T-0001 closed", "task": "T-0001", "first_in": "calc"})
        self.assertEqual(T.entry_text(app.state.log[-1]), "✦ first proved change in calc")
        self.assertTrue(app.state.first)
        with unittest.mock.patch.object(smith, "last_run_of", return_value=("T-0007", "closed", time.time() - 120)):
            app.show_picker()
            app.key("\n")
        self.assertIn("next run: worker claude · last: closed T-0007, 2m", app.message)
        with unittest.mock.patch.object(smith, "last_run_of", return_value=None):
            app.show_picker()
            app.key("\n")
        self.assertIn("no run with it yet", app.message)


class FollowUpKey(unittest.TestCase):  # T-1340
    def test_enter_after_a_close_opens_a_follow_up_and_n_a_fresh_request(self):
        T = tui()
        started = []
        app = T.App(T.DEFAULTS, spawn=lambda words, opts: started.append((words, opts)) or None)
        app.feed({"event": "start", "request": "fix add", "schema": 1})
        app.feed({"event": "task", "task": "T-0001"})
        app.feed({"event": "closed", "text": "T-0001 closed", "task": "T-0001"})
        app.key("\n")
        self.assertEqual(app.mode, "prompt")
        self.assertIn("follow-up to T-0001", app.message)
        for ch in "document it\n":
            app.key(ch)
        self.assertEqual(started[-1][0], ["document it"])
        self.assertEqual(started[-1][1].get("after"), "T-0001")
        app.feed({"event": "closed", "text": "T-0002 closed", "task": "T-0002"})
        app.new()
        for ch in "fresh\n":
            app.key(ch)
        self.assertNotIn("after", started[-1][1])


class AskBeside(unittest.TestCase):  # T-1770
    def test_n_during_a_run_asks_beside_it_and_the_answer_lands_without_touching_the_run(self):
        T = tui()
        asked, started = [], []

        class Live:
            pid = 0

            def poll(self):
                return None
        app = T.App(T.DEFAULTS, spawn=lambda words, opts: started.append(words) or Live(),
                    asker=lambda q: asked.append(q) or Live())
        app.start(["fix add"])
        app.feed({"event": "start", "request": "fix add", "schema": 1})
        app.feed({"event": "task", "task": "T-0001"})
        app.new()
        self.assertEqual((app.mode, app.asking), ("prompt", True))
        self.assertIn("beside the run", app.preview())
        for ch in "where is add?\n":
            app.key(ch)
        self.assertEqual((asked, started), (["where is add?"], [["fix add"]]), "asked, no second run")
        app.feed({"event": "aside", "question": "where is add?", "text": "calc.py line 2\n\x1b[2Jdef add"})
        texts = [T.entry_text(e) for e in app.state.log]
        self.assertIn("? where is add?", texts)
        self.assertIn("calc.py line 2", texts)
        self.assertNotIn("\x1b", "".join(texts), "an answer never drives the terminal")
        self.assertEqual(app.state.task, "T-0001")
        self.assertNotEqual(app.state.status, "answered", "the run goes on")
        app.feed({"event": "aside", "question": "and sub?", "error": "fm code: --ask answers questions only"})
        self.assertIn("? and sub?: no answer (fm code: --ask answers questions only)", [T.entry_text(e) for e in app.state.log])
        app.new()
        app.key("\x1b")
        self.assertFalse(app.asking, "Esc puts the prompt back as it was")


class WorkQueue(unittest.TestCase):  # T-1779
    def test_g_works_the_queue_until_two_tasks_in_a_row_block_and_ends_with_a_digest(self):
        T = tui()
        line = ["T-0001", "T-0002", "T-0003", "T-0004", "T-0005"]
        started = []

        class Live:
            pid = 0

            def __init__(self):
                self.done = False

            def poll(self):
                return 0 if self.done else None

        def spawn(words, opts):
            started.append(opts.get("task"))
            return Live()

        app = T.App(T.DEFAULTS, spawn=spawn, queued=lambda: line[0] if line else None)

        def finish(how, text=""):
            tid = line.pop(0)
            app.child.done = True
            app.feed({"event": "task", "task": tid})
            if how:
                app.feed({"event": how, "text": text, "task": tid, "reason": text})
            app.feed({"event": "exit", "code": 0 if how == "closed" else 1})

        app.key("g")
        self.assertEqual(started, ["T-0001"])
        finish("closed", "T-0001 closed")
        finish("blocked", "needs a key")
        finish("closed", "T-0003 closed")
        self.assertEqual(started, ["T-0001", "T-0002", "T-0003", "T-0004"], "a block alone doesn't stop it")
        finish("blocked", "no network")
        finish("blocked", "no network")
        self.assertIsNone(app.queue_run)
        self.assertEqual(started, ["T-0001", "T-0002", "T-0003", "T-0004", "T-0005"])
        texts = [T.entry_text(e) for e in app.state.log]
        digest = next(t for t in texts if "closed" in t and "blocked;" in t)
        self.assertIn("2 closed (T-0001, T-0003), 3 blocked; stopped: 2 tasks in a row blocked", digest)

    def test_an_empty_queue_says_so_and_a_stop_ends_it(self):
        T = tui()
        app = T.App(T.DEFAULTS, spawn=lambda *a: None, queued=lambda: None)
        app.work_queue()
        self.assertIn("the queue is empty", app.message)
        app.queue_run = None

        class Live:
            pid = 0

            def poll(self):
                return None
        app = T.App(T.DEFAULTS, spawn=lambda *a: Live(), queued=lambda: "T-0009")
        app.work_queue()
        app.feed({"event": "exit", "code": 143})  # s stopped it
        self.assertIsNone(app.queue_run)
        self.assertIn("stopped: T-0009", app.message)

    def test_g_during_a_run_starts_the_queue_when_that_run_ends(self):  # T-1783
        T = tui()
        started = []

        class Live:
            pid = 0
            done = False

            def poll(self):
                return 0 if self.done else None
        app = T.App(T.DEFAULTS, spawn=lambda words, opts: started.append(opts.get("task") or words) or Live(),
                    queued=lambda: "T-0002")
        app.start(["fix add"])
        app.feed({"event": "task", "task": "T-0001"})
        app.key("g")
        self.assertEqual(started, [["fix add"]], "the run going is left alone")
        self.assertIn("when this run ends", app.message)
        app.child.done = True
        app.feed({"event": "closed", "text": "T-0001 closed", "task": "T-0001"})
        app.feed({"event": "exit", "code": 0})
        self.assertEqual(started, [["fix add"], "T-0002"], "then the queue starts")
        self.assertEqual(app.queue_run["closed"], ["T-0001"])


class Receipt(unittest.TestCase):  # T-1305
    def test_a_close_adds_a_receipt_and_quit_leaves_the_last_frame_in_the_scrollback(self):
        import io
        T = tui()
        app = T.App(T.DEFAULTS, spawn=lambda *a, **k: None)
        for e in T.SAMPLE_RUN + [{"event": "changed", "step": 2, "files": ["calc.py"], "stat": {"calc.py": [1, 1]}},
                                 {"event": "closed", "text": "T-0001 closed; committed 0123abcd9 (1 file)",
                                  "task": "T-0001", "tokens": [3300, 220], "usd": 0.0123, "ts": 1060}]:
            app.feed(dict(e, ts=e.get("ts", 1000)))
        line = T.entry_text(app.state.log[-1])
        self.assertEqual(line, "▣ receipt  T-0001 · checks 1/1 · 1 file +1 −1 · 3,520 tokens · $0.01 · 1m00s · 0123abc")
        out = io.StringIO()
        with unittest.mock.patch("sys.stdout", out):
            T.run_screen(app, wrapper=lambda fn: None)
        text = out.getvalue()
        self.assertIn("receipt", text)
        self.assertFalse(any(not ch.isprintable() for ch in text.replace("\n", "")), "no escape reaches the scrollback")
        self.assertLessEqual(len(T.final_frame(app, (40, 12)).splitlines()), 12)


class Cards(unittest.TestCase):  # T-1762 (T-1755, T-1756, T-1761)
    def app(self):
        T = tui()
        self.spawned = []
        app = T.App(T.DEFAULTS, spawn=lambda words, opts: self.spawned.append((words, opts.get("resume"))))
        app.feed({"event": "start", "request": "fix add", "worker": "claude", "schema": 1})
        app.feed({"event": "task", "task": "T-0001"})
        return T, app

    def text(self, T, app):
        return T.render(app.state, 120, 34, T.DEFAULTS, app, t=100.0).text()

    def test_a_failure_card_says_what_happened_and_enter_runs_a_read_only_next(self):
        T, app = self.app()
        app.feed({"event": "error", "text": "fm code: model: boom", "task": "T-0001", "next": "fm task show T-0001"})
        text = self.text(T, app)
        for want in ("Stopped", "what happened", "boom", "Smith did", "T-0001 stays as it was", "fm task show T-0001"):
            self.assertIn(want, text)
        app.fm_output = lambda argv: f"ran {' '.join(argv)}\nline two"
        app.key("\n")
        self.assertEqual((app.mode, app.view_title, app.view_lines[0]), ("diff", "fm task show T-0001",
                                                                          "ran task show T-0001"))
        app.key("\x1b")
        self.assertEqual(app.mode, "main")
        app.key("\x1b")  # Esc on the card hides it
        self.assertNotIn("what happened", self.text(T, app))

    def test_enter_resumes_or_names_a_command_for_the_users_shell(self):
        T, app = self.app()
        app.feed({"event": "error", "text": "fm code: model: refused", "task": "T-0001",
                  "next": "fm code --resume T-0001"})
        app.key("\n")
        self.assertEqual(self.spawned, [([], "T-0001")])
        T, app = self.app()
        app.feed({"event": "blocked", "text": "T-0001 blocked: waits", "reason": "waits", "task": "T-0001",
                  "next": "fm ask T-0001 confirm"})
        self.assertIn("blocked T-0001", self.text(T, app))
        app.key("\n")
        self.assertEqual(app.message, "run it in your shell: fm ask T-0001 confirm")
        self.assertEqual(app.mode, "main")

    def test_a_stall_card_offers_wait_stop_and_another_runner_until_the_turn_shows_life(self):
        T, app = self.app()
        app.feed({"event": "stalled", "text": "    claude stalled: no output since launch (130 s)", "secs": 130})
        text = self.text(T, app)
        self.assertIn("Stalled", text)
        self.assertIn("no output since launch", text)
        app.key("w")
        self.assertNotIn("Stalled", self.text(T, app))
        app.feed({"event": "stalled", "text": "    claude stalled: silent 300 s after: reading", "secs": 300})
        self.assertIn("Stalled", self.text(T, app), "a new stall shows again")
        app.key("m")
        self.assertEqual((app.mode, app.after_pick), ("picker", "resume"))
        app.key("\x1b")
        app.feed({"event": "tool", "action": "read", "target": "calc.py", "worker": "claude"})
        self.assertNotIn("Stalled", self.text(T, app), "life again: no card")

    def test_x_hands_a_stopped_task_to_a_live_cli_then_rechecks_it(self):  # T-1341
        T, app = self.app()
        app.feed({"event": "plan", "plan": {"type": "FIX", "tier": "S", "title": "Make calc.add add",
                                            "criteria": [{"text": "calc.add adds", "verify": "python3 test_calc.py"}],
                                            "steps": ["fix it"]}})
        app.feed({"event": "blocked", "text": "T-0001 blocked: checks never passed", "reason": "checks never passed",
                  "task": "T-0001", "next": "fm task show T-0001"})
        with unittest.mock.patch.object(T, "live_cli", return_value=("claude", "claude {prompt}")):
            self.assertIn("x hands it to claude", self.text(T, app))
            app.key("x")
        argv, root, tid, name = app.handoff
        self.assertEqual((argv[0], len(argv), tid, name), ("claude", 2, "T-0001", "claude"))
        for want in ("T-0001", "Make calc.add add", "calc.add adds", "python3 test_calc.py", "checks never passed",
                     "re-runs"):
            self.assertIn(want, argv[1])
        ran, rechecks = [], []
        app.live = lambda argv, cwd: ran.append((argv[0], cwd)) or 0
        app.spawn = lambda words, opts: rechecks.append((words, opts.get("recheck")))
        app.run_handoff()
        self.assertEqual((ran[0][0], app.handoff, rechecks), ("claude", None, [([], "T-0001")]))
        self.assertIn("re-running T-0001's checks", app.message)

    def test_x_on_a_stall_stops_the_run_and_with_no_cli_says_so(self):  # T-1341
        T, app = self.app()
        app.feed({"event": "stalled", "text": "    claude stalled: no output since launch (130 s)", "secs": 130})
        stopped = []
        app.stop = lambda: stopped.append(1)
        with unittest.mock.patch.object(T, "live_cli", return_value=None):
            app.key("x")
        self.assertEqual((app.handoff, stopped), (None, []), "nothing to hand to: the run goes on")
        self.assertIn("no interactive CLI", app.message)
        with unittest.mock.patch.object(T, "live_cli", return_value=("claude", "claude {prompt}")):
            app.key("x")
        self.assertEqual(stopped, [1])
        self.assertEqual(app.handoff[2], "T-0001")

    def test_the_live_cli_is_the_runs_own_when_interactive_and_installed(self):  # T-1341
        T = tui()
        with unittest.mock.patch.object(T.shutil, "which", side_effect=lambda n: f"/bin/{n}" if n != "gemini" else None):
            self.assertEqual(T.live_cli("codex")[0], "codex")
            self.assertEqual(T.live_cli("gemini")[0], "claude", "not installed: claude")
            self.assertEqual(T.live_cli("aider")[0], "claude", "no interactive form: claude")
        with unittest.mock.patch.object(T.shutil, "which", return_value=None):
            self.assertIsNone(T.live_cli("claude"))

    def test_the_app_sweeps_runners_a_kill_left_when_it_opens(self):
        from sampo import smith
        T, app = self.app()
        with unittest.mock.patch.object(smith, "stop_survivors", return_value=[4242]):
            self.assertEqual(app.sweep(), [4242])
        self.assertIn("pid 4242", app.message)

    def running(self):
        """An app whose fm code (a sleep) registered a runner (another sleep): (app, runner)."""
        import subprocess
        import tempfile
        from sampo import smith
        from sampo import ideas
        home = tempfile.mkdtemp()
        patch = unittest.mock.patch.dict(os.environ, {"FOREMAN_HOME": home})
        patch.start()
        self.addCleanup(patch.stop)
        T, app = self.app()
        child = subprocess.Popen(["sleep", "60"], start_new_session=True)
        runner = subprocess.Popen(["sleep", "61"], start_new_session=True)
        self.addCleanup(lambda: [x.kill() or x.wait() for x in (child, runner)])
        for _ in range(200):  # until the fork has become sleep (its name is what the app shows)
            if ideas._cmdline(runner.pid)[0].startswith(b"sleep"):
                break
            time.sleep(0.01)
        os.makedirs(os.path.dirname(smith._registry(runner.pid)), exist_ok=True)
        with open(smith._registry(runner.pid), "w") as f:
            f.write(f"{ideas._starttime(runner.pid)} {child.pid} {ideas._starttime(child.pid)}")
        app.child = child
        return app, runner

    def test_a_runner_is_named_without_proc_too(self):  # T-1828: macOS has no /proc; ps names it
        import builtins
        from sampo import ideas
        app, runner = self.running()
        real = builtins.open

        def no_proc(path, *a, **k):
            if str(path).startswith("/proc/"):
                raise FileNotFoundError(path)
            return real(path, *a, **k)
        with unittest.mock.patch.object(ideas, "PROC", "/nonexistent-proc"), \
                unittest.mock.patch.object(builtins, "open", no_proc):
            self.assertEqual(app._runners_of_child(), [("sleep", runner.pid)])

    def test_quitting_mid_run_names_and_stops_each_runner(self):
        app, runner = self.running()
        app.stop()
        self.assertEqual(app.stopping, [("sleep", runner.pid)])
        self.assertEqual(app.stop_report(wait=0.3), [f"stopping sleep (pid {runner.pid})… stopped (killed)"])
        runner.wait(5)


class Noticed(unittest.TestCase):  # T-1339
    def test_home_shows_what_foreman_noticed_first_and_enter_opens_its_project(self):
        T = tui()
        projects = Home.PROJECTS + [{"project": "foreman-x", "root": "/f", "exists": True, "updated": 1.0}]
        app = T.App(T.DEFAULTS, spawn=lambda *a: None, projects=lambda: [dict(p) for p in projects],
                    project_state=lambda p: {"code_dir": None},
                    noticed=lambda: [{"project": "foreman-x", "task": "T-0042", "title": "Improve fm code: …",
                                      "causes": ["blocked: the verify still fails", "stalled: launch"]}])
        app.show_home()
        self.assertEqual(app.items[0]["kind"], "noticed")
        text = T.render(app.state, 120, 34, T.DEFAULTS, app, t=100.0).text()
        self.assertIn("Smith noticed", text)
        self.assertIn("stalled: launch", text)
        app.key("\n")
        self.assertEqual((app.mode, app.project["project"]), ("project", "foreman-x"))


class Reopen(unittest.TestCase):  # T-1338
    PROJECTS = Home.PROJECTS

    def app(self, place, code_dir=None):
        T = tui()
        self.spawned = []
        app = T.App(T.DEFAULTS, spawn=lambda words, opts: self.spawned.append((words, opts.get("resume"), app.root)),
                    projects=lambda: [dict(p) for p in self.PROJECTS],
                    project_state=lambda p: {"code_dir": code_dir}, place=place)
        return T, app

    def test_an_unfinished_run_shows_its_resume_command_and_r_resumes_it_in_its_project(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        with open(os.path.join(d, "T-0007.events.jsonl"), "w") as f:
            f.write(json.dumps({"event": "start", "request": "fix add", "ts": 100}) + "\n")
        T, app = self.app(os.path.join(d, "place.json"), code_dir=d)
        row = "".join(t for t, _ in T._history_row(T.history(d)[0]))
        self.assertIn("fm code --resume T-0007", row)
        app.show_home()
        app.key("\n")  # opens "new"
        app.item_index = next(i for i, it in enumerate(app.items) if it.get("kind") == "run")
        app.key("r")
        self.assertEqual(self.spawned, [([], "T-0007", "/w/new")])
        self.assertEqual(app.mode, "main")

    def test_the_app_reopens_the_project_left_last_with_a_welcome_back(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        place = os.path.join(d, "place.json")
        T, app = self.app(place)
        app.show_home()
        app.open_project(next(p for p in app.items if p["project"] == "old"))
        app.quit()
        T, again = self.app(place)
        again.show_home()
        again.reopen()
        self.assertEqual((again.mode, again.project["project"]), ("project", "old"))
        self.assertIn("welcome back", again.message)
        with open(place, "w") as f:
            json.dump({"project": "gone"}, f)  # a folder missing since: home stays
        T, third = self.app(place)
        third.show_home()
        third.reopen()
        self.assertEqual(third.mode, "home")


class Width(unittest.TestCase):
    """T-1301: text is cut and padded by the columns it fills, so wide (CJK, emoji) text keeps its ellipsis and the
    columns after it stay put."""
    WIDE = ["日本語のタイトルがとても長い", "🚀🚀🚀🚀🚀🚀🚀🚀 launch", "café résumé naïve façade", "mixed 日本 text here"]

    def test_fit_tail_and_wrap_stay_within_n_columns_and_mark_the_cut(self):
        T = tui()
        for s in self.WIDE:
            for n in range(1, 30):
                for got, marks in ((T.fit(s, n), True), (T.tail(s, n), n > 1)):  # a 1-column tail is the last char
                    self.assertLessEqual(T.width(got), n, (s, n, got))
                    if T.width(s) > n:
                        self.assertTrue("…" in got or not marks, (s, n, got))
                    else:
                        self.assertEqual(got, s)
                for line in T.wrap(s, n, lines=3):
                    self.assertLessEqual(T.width(line), n, (s, n, line))
        self.assertEqual(T.fit("日本語", 4), "日…")
        self.assertEqual(T.tail("日本語", 4), "…語")
        self.assertEqual(T.fit("café", 4), "café", "an accent fills no column of its own")

    def test_labels_and_titles_measure_columns(self):
        """T-1431: a wide-glyph track label fits its slot, and a long note leaves the box title cut, not gone."""
        T = tui()
        st = Core.state(Core(), RUN[:3])
        st.steps = [{"text": "日本語のステップ名前が長い", "state": "now"}, {"text": "🚀🚀🚀🚀🚀🚀 launch", "state": "todo"},
                    {"text": "日本語日本語日本語日本語", "state": "todo"}]
        for w in (60, 80, 110):
            for row in T._track(st, w, 3, None):
                self.assertEqual(sum(T.width(t) for t, _ in row), w, (w, row))
        cv = T.Canvas(80, 3)
        T.box(cv, 0, 0, 3, 80, "Projects", T.DEFAULTS, note="n" * 90)
        self.assertIn("Pro…", cv.text().splitlines()[0])

    def test_overlay_and_status_measure_columns(self):
        """T-1468: the focus band and a selected log row were padded by len(), so a line holding wide glyphs ran
        past its width and lost its tail to a stray ellipsis."""
        T = tui()
        text = "日本語のテキスト 🚀 wide"
        band = T._focus_line([(text, "text")], 40, "#202020", T.DEFAULTS)
        self.assertEqual(sum(T.width(t) for t, _ in band), 40)
        app = T.App(T.DEFAULTS, spawn=lambda *a, **k: None)
        for e in RUN[:6] + [{"event": "action", "text": "", "action": "read", "target": "日本語のテキスト🚀.py",
                             "result": "ok"}]:
            app.feed(e)
        app.selected = len(app.state.log) - 1
        rows = T._log(app.state, 60, 20, app)
        row = next(r for r in rows if "日本語" in "".join(t for t, _ in r))
        self.assertEqual(sum(T.width(t) for t, _ in row), 60, row)
        cv = T.Canvas(60, 1)
        cv.line(0, 0, row, 60)
        self.assertNotIn("…", cv.text(), "the padded row fits, so nothing is cut")

    def test_plain_ascii_is_cut_as_before(self):
        T = tui()
        self.assertEqual((T.fit("hello world", 5), T.fit("hi", 5), T.fit("abc", 0)), ("hell…", "hi", ""))
        self.assertEqual((T.tail("a/b/c.py", 5), T.tail("ab", 5), T.tail("abc", 1)), ("…c.py", "ab", "c"))
        self.assertEqual(T.wrap("one two three four", 9), ["one two", "three fo…"])
        self.assertEqual(T.pad("ab", 4), "ab  ")
        self.assertEqual(T.pad("日本", 6), "日本  ")

    @staticmethod
    def col(cv, needle):
        """The display column needle starts at on its row, and the row."""
        for row in cv.cells:
            for i in range(len(row)):
                if "".join(c for c, _ in row[i:]).startswith(needle):
                    return i, row
        raise AssertionError(f"{needle!r} not drawn:\n{cv.text()}")

    def test_a_cjk_project_card_keeps_its_border_and_cuts_its_title(self):  # T-1345: cards, from rows (T-1330)
        T = tui()
        projects = [{"project": "plain", "root": "/w/p", "exists": True, "active": None, "queue": 1, "inbox": 2,
                     "blocked": 3, "updated": 200.0},
                    {"project": "日本語のプロジェクト", "root": "/w/j", "exists": True, "updated": 100.0, "queue": 4,
                     "inbox": 5, "blocked": 6, "active": {"id": "T-0009", "title": "長いタイトル" * 20}}]
        for w in (80, 120):
            app = T.App(T.DEFAULTS, spawn=lambda *a: None, projects=lambda: [dict(p) for p in projects],
                        project_state=lambda p: {"code_dir": None})
            app.show_home()
            for selected in (0, 1):
                app.item_index = selected
                cv = T.render(app.state, w, 30, T.DEFAULTS, app, t=100.0)
                self.assertIn("queue 1", cv.text())
                self.assertIn("queue 4", cv.text())
                y, x, h, cw = app.deck_rects[1]
                _, row = self.col(cv, "T-0009")
                self.assertEqual(row[x + cw - 1][0], "│", f"the card's right border survives at {w}:\n{cv.text()}")
                self.assertIn("…", "".join(c for c, _ in row[x:x + cw]), "a cut title ends in …")
        long = dict(projects[1], project="日本語のプロジェクト" * 3)
        app = T.App(T.DEFAULTS, spawn=lambda *a: None, projects=lambda: [dict(long)],
                    project_state=lambda p: {"code_dir": None})
        app.show_home()
        app.key("\n")  # the project's own view: its long CJK name is the box title, cut to leave room for the note
        cv = T.render(app.state, 120, 30, T.DEFAULTS, app, t=100.0)
        top = "".join(c for c, _ in self.col(cv, "日本語")[1])
        self.assertIn("…", top, cv.text())
        self.assertIn("Esc projects ─╮", top, f"the note and the corner both still draw:\n{cv.text()}")

    def test_a_cjk_history_row_keeps_the_border(self):
        T = tui()
        for w in (80, 120):
            app = T.App(T.DEFAULTS, spawn=lambda *a: None)
            app.items = [{"status": "closed", "task": "T-0001", "ts": 1760000000, "request": "plain request"},
                         {"status": "closed", "task": "T-0002", "ts": 1760000000, "request": "日本語の依頼" * 30}]
            app.mode = "history"
            for selected in (0, 1):
                app.item_index = selected
                cv = T.render(app.state, w, 30, T.DEFAULTS, app, t=100.0)
                a, row_a = self.col(cv, "plain request")
                b, row_b = self.col(cv, "日本語の依頼")
                self.assertEqual(a, b, f"the requests line up at {w} columns:\n{cv.text()}")
                edge = [i for i, (c, _) in enumerate(row_a) if c == "│"][-1]
                self.assertEqual(row_b[edge][0], "│", f"the right border survives at {w} columns:\n{cv.text()}")
                self.assertIn("…", "".join(c for c, _ in row_b), "a cut request ends in …")


class Command(CodeCase):
    def test_fm_code_with_no_request_and_no_terminal_says_what_to_give(self):
        # T-1330: piped or without a terminal, fm code alone is the usage error it always was (exit 1)
        r = self.code(check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("needs a request", r.stderr)

    def test_fm_code_alone_on_a_terminal_opens_the_app(self):
        # T-1330: like claude, fm code with nothing after it opens the app: this project's view, Esc for every project
        import fcntl
        import pty
        import select
        import struct
        import termios
        from test_code import FM
        self.fm("task", "new", "add export", "--type", "FEATURE", "--tier", "S", "--ac", "done :: true")
        slug = self.fm_json("state")["project"].encode()
        env = dict(self.env(), TERM="xterm-256color")
        pid, fd = pty.fork()
        if pid == 0:
            try:
                fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 34, 120, 0, 0))
                os.chdir(self.repo)
                os.execve(sys.executable, [sys.executable, FM, "code"], env)
            finally:
                os._exit(127)
        deadline = time.time() + 60  # two fm subprocesses (projects, state) before the first frame, on a busy machine
        stream = bytearray()

        def until(want):  # T-1430: the screen the stream draws, not its bytes: a redraw skips cells already right
            while want not in screen(stream) and time.time() < deadline:
                if select.select([fd], [], [], 0.3)[0]:
                    try:
                        chunk = os.read(fd, 65536)
                    except OSError:
                        chunk = b""
                    if not chunk:  # T-1828: the child closed the terminal (EIO on Linux, b'' on macOS)
                        break
                    stream.extend(chunk)
            return screen(stream)
        try:
            seen = until("add export")
            self.assertIn("add export", seen, seen[-1500:])
            self.assertIn(slug.decode(), seen, "the project's own view")
            os.write(fd, b"\x1b")
            self.assertIn("Smith projects", until("Smith projects"))
            os.write(fd, b"q")
            while time.time() < deadline:
                done, status = os.waitpid(pid, os.WNOHANG)
                if done:
                    self.assertEqual(os.waitstatus_to_exitcode(status), 0)
                    break
                until("\x00")
            else:
                self.fail("q didn't quit")
        finally:
            try:
                os.kill(pid, 9)
            except OSError:
                pass
            os.close(fd)

    def test_fm_code_tui_draws_a_recorded_run(self):
        svg = os.path.join(self.home, "shot.svg")
        fixture = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "demo",
                               "code-tui.jsonl")
        self.code("tui", "--snapshot", svg, "--events", fixture, "--size", "100x30")
        with open(svg, encoding="utf-8") as f:
            body = f.read()
        self.assertTrue(body.startswith("<svg"), body[:80])
        self.assertIn("4e1b9c2", body)

    def test_events_alone_replays_the_run_as_plain_lines(self):
        # T-1363: --events FILE without --snapshot or --record replays it; with no terminal, as Activity lines
        fixture = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "demo",
                               "code-tui.jsonl")
        for extra in ([], ["--plain"]):
            r = self.code("tui", "--events", fixture, *extra, check=False)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("DONE", r.stdout)
            self.assertIn("edit   deploy.py", r.stdout)
        r = self.code("tui", "--events", os.path.join(self.tmp, "missing.jsonl"), check=False)
        self.assertEqual(r.returncode, 2)
        self.assertIn("missing.jsonl", r.stderr)

    def test_tour_plain_prints_the_captions_and_a_tour_svg_is_written(self):  # T-1309
        r = self.code("tui", "--tour", "--plain", check=False)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("▶ fm code works one task to proof", r.stdout)
        self.assertIn("rm -rf ~", r.stdout)
        self.assertIn("▶ Closed and committed", r.stdout)
        svg = os.path.join(self.tmp, "tour.svg")
        r = self.code("tui", "--tour", "--snapshot", svg, check=False)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.getsize(svg) > 1000)

    def test_print_config_and_a_bad_file(self):
        out = self.code("tui", "--print-config").stdout
        self.assertIn('theme = "night"', out)
        with open(os.path.join(self.home, "tui.toml"), "w", encoding="utf-8") as f:
            f.write('theme = "neon"\n')
        r = self.code("tui", "--snapshot", os.path.join(self.home, "x.svg"), "--events", os.devnull, check=False)
        self.assertEqual(r.returncode, 0, "a bad setting is said and skipped; the screen still runs")
        self.assertIn("neon", r.stderr)
        r = self.code("tui", "--print-config", check=False)
        self.assertEqual(r.returncode, 2)
        self.assertIn("neon", r.stderr)

    def test_resume_reaches_fm_code(self):
        r = self.code("tui", "--plain", "--resume", check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("STOP", r.stdout, "fm code --resume's own error, as a line")

    def test_extensions_load_from_foremans_home_and_never_from_the_project(self):
        panel = "def setup(api):\n    api.panel('{0}', '{0}', lambda st, w, h, app: [])\n"
        for folder, name in ((os.path.join(self.home, "tui"), "fromhome"), (os.path.join(self.repo, "tui"), "fromrepo"),
                             (os.path.join(self.repo, ".sampo", "tui"), "fromrepo2"),
                             (os.path.join(self.repo, ".foreman", "tui"), "fromrepo3")):
            os.makedirs(folder, exist_ok=True)
            with open(os.path.join(folder, f"{name}.py"), "w", encoding="utf-8") as f:
                f.write(panel.format(name))
        out = self.code("tui", "--print-config").stdout
        self.assertIn("fromhome", out)
        self.assertNotIn("fromrepo", out)

    def test_the_screen_runs_a_request_in_a_terminal(self):
        import fcntl
        import pty
        import select
        import struct
        import termios
        from test_code import FM
        self.serve("openai", plan(), call("edit", file="calc.py", old="a - b", new="a + b"), call("done", summary="x"))
        env = dict(self.env(), TERM="xterm-256color")
        env.pop("NO_COLOR", None)
        pid, fd = pty.fork()
        if pid == 0:  # the child: a 100x30 terminal running fm code tui
            try:
                fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
                os.chdir(self.repo)
                os.execve(sys.executable, [sys.executable, FM, "code", "tui", "fix add", "--model", "fake",
                                           "--no-commit"], env)
            finally:
                os._exit(127)
        seen, deadline = b"", time.time() + 40
        try:
            while b"CLOSED" not in seen and time.time() < deadline:
                if select.select([fd], [], [], 0.5)[0]:
                    try:
                        seen += os.read(fd, 65536)
                    except OSError:
                        break
            self.assertIn(b"CLOSED", seen, seen[-2000:])
            self.assertIn(b"\x1b[", seen, "drawn with curses, not printed")
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))  # T-1306: the child gets SIGWINCH
            seen = b""
            while b"\x1b[2J" not in seen and time.time() < deadline:
                if select.select([fd], [], [], 0.5)[0]:
                    seen += os.read(fd, 65536)
            self.assertIn(b"\x1b[2J", seen, "a resize clears and draws again")
            os.write(fd, b"q")
            after = b""
            while time.time() < deadline:  # T-1828: read to the end (EIO on Linux, b'' on macOS), then reap
                if select.select([fd], [], [], 0.2)[0]:
                    try:
                        chunk = os.read(fd, 65536)
                    except OSError:
                        chunk = b""
                    if not chunk:
                        break
                    after += chunk
            else:
                self.fail("q didn't quit")
            _, status = os.waitpid(pid, 0)
            self.assertEqual(os.waitstatus_to_exitcode(status), 0)
            for mode, why in ((b"\x1b[?1049l", "the alternate screen is left"), (b"\x1b[?25h", "the cursor shown"),
                              (b"\x1b[?7h", "wrap back on")):
                self.assertIn(mode, after, why)
        finally:
            try:
                os.kill(pid, 9)
                os.waitpid(pid, 0)
            except (OSError, ChildProcessError):
                pass
            os.close(fd)

    def _in_terminal(self, *argv, want):
        """T-1343: fm code ARGV in a 100x30 pseudo-terminal: (what it wrote by `want`, its exit code or None)."""
        import fcntl
        import pty
        import select
        import struct
        import termios
        from test_code import FM
        env = dict(self.env(), TERM="xterm-256color")
        env.pop("NO_COLOR", None)
        pid, fd = pty.fork()
        if pid == 0:
            try:
                fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
                os.chdir(self.repo)
                os.execve(sys.executable, [sys.executable, FM, "code", *argv], env)
            finally:
                os._exit(127)
        seen, code, deadline = b"", None, time.time() + 40
        try:
            while want not in seen and time.time() < deadline:
                if select.select([fd], [], [], 0.5)[0]:
                    try:
                        seen += os.read(fd, 65536)
                    except OSError:
                        break
            if b"\x1b[?1049h" in seen:  # the app: q quits it
                os.write(fd, b"q")
            while time.time() < deadline:
                done, status = os.waitpid(pid, os.WNOHANG)
                if done:
                    code = os.waitstatus_to_exitcode(status)
                    break
                if select.select([fd], [], [], 0.2)[0]:
                    try:
                        seen += os.read(fd, 65536)
                    except OSError:
                        pass
            return seen, code
        finally:
            try:
                os.kill(pid, 9)
                os.waitpid(pid, 0)
            except (OSError, ChildProcessError):
                pass
            os.close(fd)

    def test_fm_code_with_a_request_on_a_terminal_runs_it_in_the_app(self):
        # T-1343: like claude "<prompt>", fm code "<request>" on a terminal opens the app with that run going
        self.serve("openai", plan(), call("edit", file="calc.py", old="a - b", new="a + b"), call("done", summary="x"))
        seen, code = self._in_terminal("fix add", "--model", "fake", "--no-commit", want=b"CLOSED")
        self.assertIn(b"CLOSED", seen, seen[-2000:])
        self.assertIn(b"\x1b[?1049h", seen, "the app's screen, not printed lines")
        self.assertEqual(code, 0, "q quits")
        self.assertIn("a + b", self.calc())

    def test_plain_on_a_terminal_prints_fm_codes_own_lines(self):
        # T-1343: --plain keeps today's output on a terminal: fm code's own lines, no screen
        self.serve("openai", plan(), call("edit", file="calc.py", old="a - b", new="a + b"), call("done", summary="x"))
        seen, code = self._in_terminal("fix add", "--model", "fake", "--no-commit", "--plain", want=b"closed (tokens")
        self.assertIn(b"T-0001 closed (tokens", seen, seen[-2000:])
        self.assertNotIn(b"\x1b[?1049h", seen, "no screen")
        self.assertNotIn(b"DONE", seen, "fm code's lines, not the app's Activity lines")
        self.assertEqual(code, 0)

    def test_fm_code_tui_plain_runs_a_request_line_by_line(self):
        self.serve("openai", plan(), call("edit", file="calc.py", old="a - b", new="a + b"), call("done", summary="x"))
        r = self.code("tui", "--plain", "fix add", "--model", "fake", "--no-commit")
        self.assertIn("edit   calc.py", r.stdout)
        self.assertTrue(any("closed" in x for x in r.stdout.splitlines()[-3:]), r.stdout)  # T-1349, T-1305: notes after
        self.assertIn("▣ receipt  T-0001 · checks 1/1 · 1 file", r.stdout)  # T-1305: --plain prints the receipt too
        self.assertIn("not committed", r.stdout.splitlines()[-2])
        self.assertNotIn('"event"', r.stdout)


class RunHistory(CodeCase):
    def test_fm_code_keeps_each_runs_events_for_the_history(self):
        self.serve("openai", plan(), call("edit", file="calc.py", old="a - b", new="a + b"), call("done", summary="x"))
        self.code("fix add", "--model", "fake", "--no-commit")
        path = os.path.join(os.path.dirname(os.path.dirname(self.task()["path"])), "code", "T-0001.events.jsonl")
        kinds = [json.loads(line)["event"] for line in open(path)]
        self.assertEqual((kinds[0], kinds[-1]), ("start", "closed"))
        self.assertIn("plan", kinds)


class SettingsScreen(unittest.TestCase):
    """T-1572: the app's settings screen lists fmsettings' one schema (the app's tui.toml and the Claude Code pane's
    options) and writes through fm settings' own writer."""
    def setUp(self):
        import tempfile
        self.home, self.uhome = tempfile.mkdtemp(), tempfile.mkdtemp()
        env = {"FOREMAN_HOME": self.home, "HOME": self.uhome, "CLAUDECODE": "", "CLAUDE_CODE_ENTRYPOINT": ""}
        patch = unittest.mock.patch.dict(os.environ, env)
        patch.start()
        self.addCleanup(patch.stop)
        tty = unittest.mock.patch("sampo.settings._interactive", return_value=True)  # the app runs in a terminal
        tty.start()
        self.addCleanup(tty.stop)
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        self.toml = os.path.join(self.home, "tui.toml")

    def write(self, text):
        with open(self.toml, "w", encoding="utf-8") as f:
            f.write(text)

    def app(self):
        app = tui().App(tui().load_config()[0], spawn=lambda *a, **k: None)
        app.key(",")
        self.assertEqual(app.mode, "settings")
        return app

    def text(self, app, w=120, h=40):
        return tui().render(app.state, w, h, app.cfg, app).text()

    def row(self, app, key, **kw):
        return next(line for line in self.text(app, **kw).splitlines() if f" {key} " in line)

    def keys(self, app):
        return [(r["surface"], r["key"]) for r in app.items if "head" not in r]

    def test_every_setting_is_listed_by_group_with_value_default_and_badge(self):
        from sampo import settings as ssettings
        app = self.app()
        self.assertEqual(sorted(self.keys(app)), sorted((e["surface"], e["key"]) for e in ssettings.schema()))
        heads = [r["head"] for r in app.items if "head" in r]
        self.assertEqual(heads, list(dict.fromkeys(e["group"] for e in ssettings.schema())), "one header a group")
        self.assertNotIn("head", app.items[app.item_index], "the selection starts on a setting")
        text = self.text(app)
        self.assertIn("Settings", text)
        self.assertIn("LOOK", text)
        line = self.row(app, "border")
        for want in ("rounded", "NOW"):
            self.assertIn(want, line)
        self.assertEqual(line.count("rounded"), 2, "the value and the default")
        for _ in range(len(app.items)):
            app.key("j")
        self.assertIn("IN SECONDS", self.row(app, "mascot"), "the pane reads its options again within seconds")
        for k in "/nothing-is-called-this":
            app.key(k)
        self.assertEqual(app.items, [])
        self.assertIn("no setting matches", self.text(app))

    def test_search_narrows_and_modified_only_shows_changed_keys(self):
        self.write('# mine\nmotion = "off"\n')
        app = self.app()
        for k in "/pal":
            app.key(k)
        self.assertEqual(sorted(self.keys(app)), [("app", "palette"), ("plugin", "palette")])
        text = self.text(app)
        self.assertIn("palette", text)
        self.assertNotIn("border", text)
        app.key("\x1b")  # Esc in the search clears it
        self.assertGreater(len(self.keys(app)), 20)
        app.key("m")
        self.assertEqual(self.keys(app), [("app", "motion")])
        line = self.row(app, "motion")
        self.assertIn("●", line, "changed is marked, not only coloured")
        self.assertLess(line.index("off"), line.index("full"), "the value, then the default")
        app.key("m")
        self.assertGreater(len(self.keys(app)), 20)

    def test_set_line(self):
        self.write("# my settings\nborder = \"rounded\"  # keep\n")
        app = self.app()
        for k in ":set border heavy":
            app.key(k)
        self.assertIn(":set border heavy", self.text(app))
        app.key("\n")
        with open(self.toml, encoding="utf-8") as f:
            self.assertEqual(f.read(), "# my settings\nborder = \"heavy\"  # keep\n", "fm settings' writer: in place")
        self.assertEqual(app.cfg["border"], "heavy", "the app takes it now")
        self.assertIn("heavy", self.row(app, "border"))
        self.assertIn("┏", self.text(app), "the screen draws with it")
        for k in ":set border wobbly\n":
            app.key(k)
        self.assertIn("one of", self.text(app))
        with open(self.toml, encoding="utf-8") as f:
            self.assertIn('"heavy"', f.read(), "a refused value writes nothing")
        for k in ":set --surface plugin palette cb\n":
            app.key(k)
        with open(os.path.join(self.uhome, ".claude", "settings.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["pluginConfigs"]["sampo-ui@sampo"]["options"], {"palette": "cb"})
        self.assertEqual(app.cfg["palette"], "default", "the pane's value only: the app's own is unchanged")

    def test_enter_on_a_row_starts_a_set_line_for_it_and_esc_closes(self):
        app = self.app()
        app.key("\n")
        self.assertIn(":set theme night", self.text(app))
        app.key("\x1b")
        app.key("\x1b")
        self.assertEqual(app.mode, "main")
        app.projects_source = lambda: [{"project": "demo", "root": self.home}]
        app.show_home()
        app.key(",")  # fm code app opens on its home: the settings are a key away there too
        self.assertEqual(app.mode, "settings")
        app.key("\x1b")
        self.assertEqual(app.mode, "home")

    def test_any_width_draws_and_rows_keep_their_columns(self):
        self.write('motion = "off"\n[colors]\nok = "#4DA3FF"\n')
        app = self.app()
        for w in (60, 80, 140):
            lines = self.text(app, w, 30).splitlines()
            head = next(line for line in lines if "VALUE" in line and "DEFAULT" in line)
            for key, value, default in (("motion", "off", "full"), ("colors.ok", "#4DA3FF", tui().PALETTES["night"]["ok"])):
                line = next(line for line in lines if f" {key} " in line)
                self.assertEqual(line.index(value, line.index(key) + len(key)), head.index("VALUE")
                                 + (3 if key.startswith("colors") else 0), (w, key, "the value under VALUE (a swatch "
                                                                             "before a colour)"))
                self.assertEqual(line.rindex(default[:5]), head.index("DEFAULT"), (w, key, "the default under DEFAULT"))

    def test_a_small_terminal_keeps_a_row_and_the_line_being_typed(self):
        app = self.app()
        for h in range(8, 15):
            self.assertIn(" theme ", self.text(app, 80, h), h)
        for k in ":set x":
            app.key(k)
        self.assertIn(":set x", self.text(app, 80, 10))

    def test_a_paste_types_only_printable_characters(self):
        app = self.app()
        app.key("/")
        app.key(("paste", "bo\x1brd\x7fer"))
        self.assertEqual((app.mode, app.set_typing, app.set_query), ("settings", "search", "border"))

    def test_a_value_nested_past_the_parsers_depth_is_refused_not_a_crash(self):
        from sampo import settings as ssettings
        for k in ":set layout.widths " + "[" * 600 + "\n":
            app = getattr(self, "_app", None) or self.app()
            self._app = app
            app.key(k)
        self.assertEqual(app.set_note[1], "bad")
        self.assertFalse(os.path.exists(self.toml))
        with self.assertRaises(ssettings.SettingError):
            ssettings.set_app("layout.widths", "[" * 600)

    def test_a_value_the_file_already_gets_wrong_is_still_refused(self):
        self.write("[layout]\nwidths = [50]\n")
        app = self.app()
        for k in ":set layout.widths [10, 20, 30]\n":
            app.key(k)
        self.assertEqual(app.set_note[1], "bad", app.set_note)
        with open(self.toml, encoding="utf-8") as f:
            self.assertEqual(f.read(), "[layout]\nwidths = [50]\n")

    def test_inside_claude_code_the_guard_decides_a_set(self):
        os.environ["CLAUDECODE"] = "1"
        app = self.app()
        for k in ":set border heavy\n":
            app.key(k)
        self.assertEqual(app.set_note[1], "bad")
        self.assertIn("guard", app.set_note[0])
        self.assertFalse(os.path.exists(self.toml), "nothing written")

    def test_blank_claude_variables_without_a_terminal_still_ask_the_guard(self):
        from sampo import settings as ssettings
        settings = os.path.join(self.uhome, ".claude", "settings.json")
        with unittest.mock.patch("sampo.settings._interactive", return_value=False):
            self.assertTrue(ssettings.in_claude_code(), "no terminal: an agent's shell, whatever its variables say")
            with self.assertRaises(ssettings.SettingError) as e:
                ssettings.set_plugin("motion", "off")
        self.assertIn("guard", str(e.exception))
        self.assertFalse(os.path.exists(settings))
        self.assertFalse(ssettings.in_claude_code(), "the user's own terminal writes directly")

    def test_a_genesis_tower_heads_the_project_view(self):  # T-2118
        app = self.app()
        app.key("\x1b")
        app.state_source = lambda p: {"genesis": {"gen": 1, "stage": "build", "kind": "ok", "action": "Mission 3",
                                                  "command": "fm focus T-1"}, "queue": [{"id": "T-0001", "title": "x"}]}
        app.open_project({"project": "a", "root": self.home})
        self.assertEqual([r["kind"] for r in app.project_items[:2]], ["genesis", "task"])

    def test_leaving_settings_returns_to_the_view_it_came_from_where_it_was(self):
        app = self.app()
        app.key("\x1b")
        app.projects_source = lambda: [{"project": n, "root": self.home} for n in ("a", "b", "c")]
        app.state_source = lambda p: {"queue": [{"id": "T-0001", "title": "x"}, {"id": "T-0002", "title": "y"}]}
        app.show_home()
        app.key("j")
        app.key("j")
        app.key(",")
        app.key("\x1b")
        self.assertEqual((app.mode, app.item_index), ("home", 2), "the home keeps its cursor")
        app.key("\n")
        app.key("j")
        app.key(",")
        app.key("\x1b")
        self.assertEqual((app.mode, app.item_index), ("project", 1))


class SettingsPreview(unittest.TestCase):
    """T-1573: a theme, palette or motion change on the settings screen shows at once on the live config (the screen
    behind the overlay repaints) and writes nothing; any key within 10 s keeps it (written then), else it goes back."""
    setUp, write, text = SettingsScreen.setUp, SettingsScreen.write, SettingsScreen.text
    MINE = '# my look\ntheme = "night"  # keep\n'

    def app(self):
        self.write(self.MINE)
        app, self.now = SettingsScreen.app(self), 100.0
        app.clock = lambda: self.now
        return app

    def set(self, app, line):
        for k in f":{line}\n":
            app.key(k)

    def file(self):
        with open(self.toml, encoding="utf-8") as f:
            return f.read()

    def svg(self, app):
        return tui().to_svg(tui().render(app.state, 120, 40, app.cfg, app), app.cfg)

    def test_a_theme_change_repaints_behind_the_overlay_before_it_is_kept(self):
        app = self.app()
        shade = f'fill="{tui().PALETTES["paper"]["shade"].lower()}"'  # T-2876: the palette's, not a copy of it
        self.assertNotIn(shade, self.svg(app))
        self.set(app, "set theme paper")
        self.assertEqual(app.cfg["preset"], "paper")
        self.assertIn(shade, self.svg(app), "paper's shade: the screen behind repainted")
        self.assertEqual(self.file(), self.MINE, "a preview writes nothing")
        text = self.text(app)
        self.assertIn(" any key  keeps it", text)
        self.assertIn("reverts in 10 s", text)

    def test_an_unkept_change_goes_back_after_10_seconds_on_screen_and_in_the_file(self):
        app = self.app()
        night = app.cfg
        self.set(app, "set theme paper")
        self.now += 9.5
        app.poll()
        self.assertEqual(app.cfg["preset"], "paper", "still a preview")
        self.assertIn("1 s", self.text(app))
        self.now += 0.5
        app.poll()  # the screen's loop polls at least once a second
        self.assertEqual(app.cfg, night, "the screen as it was")
        self.assertIn(f'fill="{tui().PALETTES["night"]["shade"].lower()}"', self.svg(app))
        self.assertEqual(self.file(), self.MINE, "the file as it was")
        self.assertIn("not kept", self.text(app))
        app.key("j")
        self.assertEqual(self.file(), self.MINE, "a key once it went back keeps nothing")

    def test_a_key_after_the_10_seconds_keeps_nothing_even_before_the_poll(self):
        app = self.app()
        night = app.cfg
        self.set(app, "set theme paper")
        self.now += 10
        app.key("j")
        self.assertEqual((app.cfg, self.file()), (night, self.MINE))

    def test_any_key_keeps_it_and_writes_it_then(self):
        for key in ("j", "\x1b", "q", ("paste", "x")):
            with self.subTest(key=key):
                app = self.app()
                self.set(app, "set theme paper")
                at, shown = app.item_index, app.cfg
                self.now += 9
                app.key(key)
                self.assertEqual(self.file(), self.MINE.replace('"night"', '"paper"'), "written in place, kept")
                self.assertEqual((app.mode, app.item_index, app.cfg["preset"]), ("settings", at, "paper"),
                                 "the key keeps it and does nothing else")
                self.assertEqual(app.cfg, shown, "what is kept is what was shown")
                self.now += 60
                app.poll()
                self.assertEqual(app.cfg["preset"], "paper", "kept: nothing goes back later")

    def test_motion_and_palette_preview_too_and_a_shared_one_is_written_on_both_when_kept(self):
        settings = os.path.join(self.uhome, ".claude", "settings.json")
        app = self.app()
        self.set(app, "set palette cb")
        self.assertEqual((app.cfg["palette"], app.cfg["theme"]["ok"]["fg"]), ("cb", "#4DA3FF"))
        self.now += 10
        app.poll()
        self.assertEqual(app.cfg["palette"], "default")
        self.set(app, "set motion off")
        self.assertEqual(app.cfg["motion"], "off")
        self.assertEqual(self.file(), self.MINE)
        self.assertFalse(os.path.exists(settings), "the pane's half waits for the keep too")
        app.key("k")
        self.assertIn('motion = "off"', self.file())
        with open(settings, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["pluginConfigs"]["sampo-ui@sampo"]["options"], {"motion": "off"})

    def test_what_isnt_a_look_of_the_app_is_written_at_once(self):
        settings = os.path.join(self.uhome, ".claude", "settings.json")
        app = self.app()
        self.set(app, "set --surface plugin motion reduced")
        self.assertIsNone(app.set_pending, "the pane's own value: nothing on this screen to preview")
        with open(settings, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["pluginConfigs"]["sampo-ui@sampo"]["options"], {"motion": "reduced"})

    def test_a_value_it_would_refuse_is_refused_at_once_not_previewed(self):
        app = self.app()
        night = app.cfg
        self.set(app, "set theme nope")
        self.assertEqual(app.set_note[1], "bad")
        self.assertIn("one of", app.set_note[0])
        self.assertIsNone(app.set_pending)
        self.assertIs(app.cfg, night)

    def test_inside_claude_code_a_look_the_guard_refuses_is_refused_at_the_preview(self):
        os.environ["CLAUDECODE"] = "1"
        app = self.app()
        night = app.cfg
        self.set(app, "set theme paper")
        self.assertIsNone(app.set_pending, "nothing shown that couldn't be kept")
        self.assertIs(app.cfg, night)
        self.assertEqual(app.set_note[1], "bad")
        self.assertIn("guard", app.set_note[0])
        self.assertEqual(self.file(), self.MINE)

    def test_a_keep_refused_after_the_preview_goes_back_and_says_why(self):
        app = self.app()
        night = app.cfg
        self.set(app, "set theme paper")
        os.environ["CLAUDECODE"] = "1"  # the guard decides by the time the key comes
        app.key("j")
        self.assertEqual(app.cfg, night)
        self.assertEqual(app.set_note[1], "bad")
        self.assertIn("guard", app.set_note[0])
        self.assertEqual(self.file(), self.MINE)

    def test_no_colour_keeps_the_mono_theme_through_a_preview(self):
        app = self.app()
        app.cfg, app.no_colour = dict(app.cfg, theme=tui()._mono(), preset="mono"), True
        self.set(app, "set motion off")
        self.assertEqual((app.cfg["preset"], app.cfg["motion"]), ("mono", "off"))


class RunnerPing(unittest.TestCase):  # T-1335
    setUp, text = SettingsScreen.setUp, SettingsScreen.text

    def test_the_runners_screen_shows_each_last_ping_and_p_pings_the_selected(self):
        from sampo import smith
        os.makedirs(os.path.join(self.home, "state"), exist_ok=True)
        with open(os.path.join(self.home, "state", "code-runners-ping.json"), "w") as f:
            json.dump({"claude": {"state": "not logged in", "ms": 900, "version": "2.1.0"}}, f)
        app = SettingsScreen.app(self)
        app.key("r")
        self.assertIn("not logged in", self.text(app))
        app.item_index = next(i for i, r in enumerate(app.items) if r["name"] == "claude")
        answer = {"name": "claude", "state": "ok", "ms": 1200, "version": "2.1.0", "said": "ok"}
        with unittest.mock.patch.object(smith, "ping_runner", return_value=answer) as ping:
            app.key("p")
            app.runner_test.join(10)
        ping.assert_called_once()
        self.assertTrue(app.poll())
        text = self.text(app)
        self.assertIn("ok", text)
        self.assertIn("1.2 s", text)


class Editors(unittest.TestCase):
    """T-1511: three editors inside the settings screen, each writing through fm settings' writer (the guard, the lock,
    the history fm settings undo walks back): b binds a pressed key to the selected key row's command, l moves panels
    between and within columns with keys, r tests a runner of the registry or code.toml where it is."""
    setUp, write, text = SettingsScreen.setUp, SettingsScreen.write, SettingsScreen.text

    def app(self):
        return SettingsScreen.app(self)

    def pick(self, app, key):
        app.item_index = next(i for i, r in enumerate(app.items) if r.get("key") == key)

    def file(self):
        with open(self.toml, encoding="utf-8") as f:
            return f.read()

    def test_a_captured_key_binds_to_the_rows_command_and_persists(self):
        from sampo import settings as ssettings
        self.write("# mine\n")
        app = self.app()
        self.pick(app, "keys.d")
        app.key("b")
        self.assertIn("press the key for diff", self.text(app))
        app.key("x")
        self.assertIn('[keys]\nx = "diff"', self.file(), "written in place by fm settings' writer")
        self.assertEqual(app.cfg["keys"]["x"], "diff", "the app takes it now")
        self.assertEqual(tui().load_config()[0]["keys"]["x"], "diff", "persists: the next run reads it")
        self.assertEqual((app.mode, app.set_typing, app.set_note[1]), ("settings", "", "ok"))
        self.assertEqual(app.items[app.item_index]["key"], "keys.x", "the selection on the new binding")
        ssettings.undo()
        self.assertNotIn("x =", self.file(), "fm settings undo puts it back")

    def test_capture_refuses_a_reserved_key_and_shows_the_conflict(self):
        app = self.app()
        for k, why in (("q", "quit"), ("^C", "quit"), ("?", "help"), ("z", "hush"), (",", "settings"),
                       ("KEY_DC", "can't be named"), (("paste", "x"), "can't be named")):
            with self.subTest(key=k):
                self.pick(app, "keys.d")
                app.key("b")
                app.key(k)
                self.assertEqual(app.set_note[1], "bad", app.set_note)
                self.assertIn(why, app.set_note[0])
                self.assertEqual((app.mode, app.set_typing), ("settings", ""))
        self.assertFalse(os.path.exists(self.toml), "nothing written")
        self.pick(app, "keys.d")
        app.key("b")
        app.key("\x1b")
        self.assertEqual((app.mode, app.set_typing, os.path.exists(self.toml)), ("settings", "", False), "Esc cancels")
        self.pick(app, "keys.d")
        app.key("b")
        app.key("h")  # not reserved, but taken: bound, and what it ran is said
        self.assertIn('h = "diff"', self.file())
        self.assertIn("history", app.set_note[0])

    def test_a_panel_moved_by_keys_changes_the_layout_and_one_undo_puts_a_move_back(self):
        from sampo import settings as ssettings
        app = self.app()
        app.key("l")
        self.assertEqual(app.mode, "layout")
        self.assertIn("checks", self.text(app))
        app.key("L")  # log is its column's only panel: a column keeps one
        self.assertEqual(app.set_note[1], "bad")
        self.assertFalse(os.path.exists(self.toml))
        for k in ("KEY_RIGHT", "j", "H"):  # to checks, down to files, files moved to the left column
            app.key(k)
        cols = [["log", "files"], ["checks", "time", "tokens"]]
        self.assertEqual(tui().load_config()[0]["layout"]["columns"], cols, "written to tui.toml")
        self.assertEqual(tui().load_config()[0]["layout"]["heights"], [[1, "auto"], ["auto", "auto", 1]],
                         "each panel keeps its height")
        self.assertEqual(app.cfg["layout"]["columns"], cols, "the screen behind takes it now")
        tui().render(app.state, 120, 40, app.cfg, app)
        self.assertEqual(app.order[:3], ["log", "files", "checks"], "drawn in its new place")
        app.key("K")
        self.assertEqual(tui().load_config()[0]["layout"]["columns"][0], ["files", "log"])
        ssettings.undo()
        self.assertEqual(tui().load_config()[0]["layout"]["columns"], cols, "one undo: one move")
        self.assertEqual(tui().load_config()[0]["layout"]["heights"], [[1, "auto"], ["auto", "auto", 1]])
        app.key("\x1b")
        self.assertEqual(app.mode, "settings")

    def runner(self, script):
        import stat
        cli = os.path.join(self.home, "fakecli")
        with open(cli, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\n" + script + "\n")
        os.chmod(cli, os.stat(cli).st_mode | stat.S_IEXEC)
        with open(os.path.join(self.home, "code.toml"), "w", encoding="utf-8") as f:
            f.write(f'[runners.fake]\nwork = "{cli} {{prompt}}"\n')
        return cli

    def test_a_runner_test_reports_its_result(self):
        self.runner('echo "fake 1.2.3"')
        with open(os.path.join(self.home, "code.toml"), encoding="utf-8") as f:
            before = f.read()
        app = self.app()
        app.key("r")
        self.assertEqual(app.mode, "runners")
        names = [r["name"] for r in app.items]
        self.assertIn("fake", names)
        self.assertIn("claude", names, "the built-in registry's too")
        app.item_index = names.index("fake")
        app.key("\n")
        app.runner_test.join(10)
        self.assertTrue(app.poll(), "a result in: the screen redraws")
        text = self.text(app)
        self.assertIn("fake 1.2.3", text)
        self.assertIn("✓", text)
        with open(os.path.join(self.home, "code.toml"), encoding="utf-8") as f:
            self.assertEqual(f.read(), before, "a test adds no command")
        app.key("\x1b")
        self.assertEqual(app.mode, "settings")

    def test_a_runner_that_hangs_is_cut_off_at_the_timeout(self):
        from sampo import smith
        self.runner("sleep 30")
        t0 = time.monotonic()
        got = smith.test_runner("fake", timeout=0.5)
        self.assertLess(time.monotonic() - t0, 10)  # wall clock: test_runner's timeout=0.5 kill deadline
        self.assertFalse(got["ok"])
        self.assertIn("0.5 s", got["said"])

    def test_arrows_pages_and_enter_always_move_and_open_a_list(self):
        """Review: tui.toml can bind them away; in a list they still move and open, so none can be locked out."""
        self.write('[keys]\nup = "diff"\ndown = "diff"\npgup = "diff"\npgdn = "diff"\nenter = "history"\nj = "none"\n'
                   'k = "none"\n')
        app = self.app()
        start = app.item_index
        app.key("KEY_DOWN")
        self.assertGreater(app.item_index, start)
        app.key("KEY_UP")
        self.assertEqual(app.item_index, start)
        app.key("KEY_NPAGE")
        self.assertGreater(app.item_index, start + 5)
        app.key("KEY_PPAGE")
        self.assertEqual(app.item_index, start)
        app.key("\n")
        self.assertEqual(app.set_typing, "set", "Enter opens the row's :set line")
        app.key("\x1b")
        app.key("r")
        app.key("KEY_DOWN")
        self.assertEqual((app.mode, app.item_index), ("runners", 1))
        app.key("\x1b")
        app.key("\x1b")
        app.projects_source = lambda: [{"project": n, "root": self.home} for n in "ab"]
        app.state_source = lambda p: {"queue": [{"id": "T-0001", "title": "x"}]}
        app.show_home()
        app.key("KEY_DOWN")
        app.key("KEY_ENTER")
        self.assertEqual((app.mode, app.project["project"]), ("project", "b"))
        app.mode, app.choices_source = "main", lambda: [("model", "a"), ("model", "b")]
        app.show_picker()
        app.key("KEY_DOWN")
        app.key("\n")
        self.assertEqual(app.opts.get("model"), "b")

    def test_the_last_key_that_moves_or_opens_is_never_bound_away(self):
        self.write('[keys]\nj = "none"\nk = "none"\n')
        app = self.app()
        for k, cmd in (("KEY_DOWN", "down"), ("KEY_UP", "up"), ("\n", "expand"), ("\t", "focus")):
            with self.subTest(key=k):
                self.pick(app, "keys.d")
                app.key("b")
                app.key(k)
                self.assertEqual(app.set_note[1], "bad", app.set_note)
                self.assertIn(f"last key for {cmd}", app.set_note[0])
        self.assertEqual(self.file(), '[keys]\nj = "none"\nk = "none"\n', "nothing written")
        self.write("")
        app = self.app()
        self.pick(app, "keys.d")
        app.key("b")
        app.key("KEY_DOWN")  # j still moves down: this one may go
        self.assertIn('down = "diff"', self.file())

    def test_a_quit_help_hush_or_settings_key_moved_elsewhere_is_kept_too(self):
        self.write('[keys]\nQ = "quit"\nH = "help"\nZ = "hush"\nS = "settings"\n')
        app = self.app()
        for k, why in (("Q", "quit"), ("H", "help"), ("Z", "hush"), ("S", "settings")):
            with self.subTest(key=k):
                self.pick(app, "keys.d")
                app.key("b")
                app.key(k)
                self.assertEqual(app.set_note[1], "bad", app.set_note)
                self.assertIn(why, app.set_note[0])
        self.assertNotIn("diff", self.file())

    def test_quitting_mid_runner_test_stops_its_cli(self):
        from sampo import smith
        pidfile = os.path.join(self.home, "pid")
        self.runner(f"echo $$ > {pidfile}; exec sleep 90")
        real = smith.test_runner  # T-1941: a 60 s timeout, so a slow runner's 30 s join still tells the bug from a fix
        self.enterContext(unittest.mock.patch.object(smith, "test_runner", lambda name, **kw: real(name, timeout=60, **kw)))
        app = self.app()
        app.key("r")
        app.item_index = [r["name"] for r in app.items].index("fake")
        app.key("\n")
        for _ in range(250):
            if os.path.exists(pidfile) and os.path.getsize(pidfile):
                break
            time.sleep(0.02)
        with open(pidfile) as f:
            pid = int(f.read())

        def alive():  # T-1828: /proc where there is one, else signal 0 (macOS)
            return tui()._alive(pid)
        self.assertTrue(alive())
        app.quit()
        app.runner_test.join(30)  # T-1941: a 1338 s macOS run needed more than 5 s
        self.assertFalse(app.runner_test.is_alive(), "the test ends with the app, not at its 60 s timeout")
        for _ in range(100):
            if not alive():
                break
            time.sleep(0.02)
        self.assertFalse(alive(), "its CLI is gone too")

    def test_quitting_before_the_app_has_seen_the_runner_cli_still_ends_it(self):
        # T-2030: a loaded macOS runner quit after the CLI started but before on_spawn told the app: nothing was killed
        from sampo import smith
        pidfile = os.path.join(self.home, "pid")
        self.runner(f"echo $$ > {pidfile}; exec sleep 90")
        real = smith.test_runner

        def late(name, on_spawn=None, **kw):
            def spawned(p):
                for _ in range(500):  # the quit lands first
                    if app.done:
                        break
                    time.sleep(0.01)
                on_spawn(p)
            return real(name, timeout=60, on_spawn=spawned, **kw)
        self.enterContext(unittest.mock.patch.object(smith, "test_runner", late))
        app = self.app()
        app.key("r")
        app.item_index = [r["name"] for r in app.items].index("fake")
        app.key("\n")
        for _ in range(250):
            if os.path.exists(pidfile) and os.path.getsize(pidfile):
                break
            time.sleep(0.02)
        app.quit()
        app.runner_test.join(30)
        self.assertFalse(app.runner_test.is_alive(), "the CLI ends with the app, not at its 60 s timeout")

    def test_a_failure_between_the_two_layout_keys_writes_nothing_and_the_screen_shows_the_file(self):
        from sampo import settings as ssettings
        real = ssettings._plan_app

        def plan(key, *a, **k):
            if key == "layout.heights":
                raise RuntimeError("disk on fire")
            return real(key, *a, **k)
        app = self.app()
        app.key("l")
        for k in ("KEY_RIGHT", "j"):
            app.key(k)
        with unittest.mock.patch("sampo.settings._plan_app", side_effect=plan):
            app.key("H")
        self.assertFalse(os.path.exists(self.toml), "never new columns with old heights")
        self.assertEqual(app.set_note[1], "bad")
        self.assertIn("disk on fire", app.set_note[0])
        self.assertEqual(app.cfg["layout"], tui().load_config()[0]["layout"], "the screen as the file is")
        with self.assertRaises(ssettings.SettingError):
            ssettings.undo()  # nothing in the history
        os.environ["CLAUDECODE"] = "1"  # the guard refuses the first key: the second isn't written either
        app.key("H")
        self.assertIn("guard", app.set_note[0])
        self.assertFalse(os.path.exists(self.toml))

    def test_the_layout_editor_says_a_narrow_screen_hides_a_move_and_leaving_clears_its_note(self):
        app = self.app()
        app.key("l")
        self.assertIn("narrow", self.text(app, 70, 30))
        self.assertNotIn("narrow", self.text(app, 120, 30))
        app.key("L")
        self.assertEqual(app.set_note[1], "bad")
        app.key("\x1b")
        self.assertEqual((app.mode, app.set_note), ("settings", None))

    def test_only_a_runner_of_the_registry_or_code_toml_is_tested(self):
        from sampo import smith
        mark = os.path.join(self.home, "ran")
        for name in (f"touch {mark}", "nope", "opencode"):  # opencode: known, but no command to run
            with self.subTest(name=name), self.assertRaises(smith.CodeError):
                smith.test_runner(name)
        self.assertFalse(os.path.exists(mark), "never taken as a command")
        self.assertFalse(os.path.exists(os.path.join(self.home, "code.toml")))


class MotionVisuals(unittest.TestCase):
    """T-1512: the hush key cycles motion full → reduced → off through fm settings' writer (tui.toml, and the pane's
    option of the same name, which the pane takes on its next poll); the spinner's tempo says what the agent is doing;
    the riso theme in the app and the pane; a glyph-width probe that talks only to a terminal; the half-block track."""
    setUp, text = SettingsScreen.setUp, SettingsScreen.text

    def app(self, events=(), **kw):
        app = tui().App(kw.pop("cfg", None) or tui().load_config()[0], spawn=lambda *a, **k: None, **kw)
        for e in events:
            app.feed(e)
        return app

    def options(self):
        path = os.path.join(self.uhome, ".claude", "settings.json")
        if not os.path.exists(path):
            return {}
        with open(path, encoding="utf-8") as f:
            return json.load(f)["pluginConfigs"]["sampo-ui@sampo"]["options"]

    def test_the_hush_key_cycles_motion_on_the_screen_in_tui_toml_and_for_the_pane(self):
        from sampo import settings as ssettings
        pane = ssettings.mod_manifest()["userConfig"]["motion"]["options"]
        app = self.app(projects=lambda: [])
        for want in ("reduced", "off", "full"):
            app.key("z")
            self.assertEqual(app.cfg["motion"], want)
            self.assertEqual(tui().load_config()[0]["motion"], want, "in tui.toml: the next run starts hushed too")
            self.assertEqual(self.options(), {"motion": want}, "the pane's own option: it takes it on its next poll")
            self.assertIn(want, pane, "a value the pane's options list (Claude Code drops one they don't)")
            self.assertIn(f"motion: {want}", self.text(app))
        app.key("?")
        app.key("z")
        self.assertEqual((app.mode, app.cfg["motion"]), ("main", "reduced"), "from the keys poster: it hushes and closes")
        app.key("p")
        self.assertEqual(app.mode, "home")
        app.key("z")
        self.assertEqual(app.cfg["motion"], "off", "from the app's home too")
        app.key(",")
        app.key("z")
        self.assertEqual((app.mode, app.cfg["motion"]), ("settings", "off"), "the settings list keeps its own keys")
        app.key("\x1b")
        app.mode = "prompt"
        app.key("z")
        self.assertEqual((app.input, app.cfg["motion"]), ("z", "off"), "typed into a request, never a hush")

    def test_a_hush_the_guard_refuses_still_hushes_this_screen_and_says_why(self):
        os.environ["CLAUDECODE"] = "1"
        app = self.app()
        app.key("z")
        self.assertEqual(app.cfg["motion"], "reduced")
        self.assertIn("this screen only", app.message)
        self.assertIn("guard", app.message)
        self.assertFalse(os.path.exists(self.toml))
        self.assertEqual(self.options(), {})

    def test_the_spinner_turns_fastest_running_slower_thinking_and_slowest_waiting(self):
        cfg = dict(tui().DEFAULTS, motion="full")

        def turns(events):
            app = self.app(events, cfg=cfg)
            glyphs = [tui().render(app.state, 100, 30, cfg, app, t=100 + i / 50).text().splitlines()[-1].split()[0]
                      for i in range(50)]  # one second of the status chip's spinner
            return sum(a != b for a, b in zip(glyphs, glyphs[1:]))
        thinking, running = turns(RUN[:1]), turns(RUNNING)
        waiting = turns(RUNNING + [{"event": "working", "text": "claude is working", "silent": 20}])
        self.assertGreater(running, thinking)
        self.assertGreater(thinking, waiting)
        self.assertGreater(waiting, 0, "a quiet turn still turns: the run is alive")

    def test_the_riso_theme_loads_in_the_app_and_every_word_on_it_reads(self):
        T = tui()
        for text, preset in (('theme = "riso"\n', "riso"), ('theme = "riso"\npalette = "cb"\n', "riso"),
                             ('palette = "riso"\n', "night"), ('theme = "paper"\npalette = "riso"\n', "paper")):
            with self.subTest(text=text):
                with open(self.toml, "w", encoding="utf-8") as f:
                    f.write(text)
                cfg, errors = T.load_config()
                self.assertEqual((errors, cfg["preset"]), ([], preset))
                th = cfg["theme"]
                for name, spec in th.items():  # the app's own floor for a [colors] style, on the screen and a panel
                    if name not in ("screen", "panel", "border", "track", "shade", "glow", "todo"):  # todo: ○ and ─, a graphic
                        self.assertIsNone(T._faint(spec, th), name)
                        self.assertIsNone(T._faint(dict(spec, bg=spec.get("bg") or th["panel"]["bg"]), th), name)
                T.render(T.State(), 100, 30, cfg)
        self.assertEqual(T.load_config()[0]["theme"]["bad"]["fg"], T.RISO["paper"]["bad"], "riso's pink on paper")
        self.assertTrue(T.PRESETS["riso"]()["screen"].get("paint"), "a paper stock: it paints a dark terminal")
        riso = T.PALETTES["riso"]
        self.assertGreaterEqual(min(T.contrast(riso["dim"], riso[k]) for k in ("bar", "bg")), 4.5, "the hints read")

    def test_the_riso_palette_is_the_panes_too_in_the_same_inks(self):
        import re
        from sampo import settings as ssettings
        self.assertIn("riso", ssettings.mod_manifest()["userConfig"]["palette"]["options"])
        self.assertEqual(ssettings.set_plugin("palette", "riso"), "riso")
        self.assertEqual(self.options(), {"palette": "riso"})
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        with open(os.path.join(root, "mods", "foreman-ui", "hooks", "kit.ts"), encoding="utf-8") as f:
            riso = re.search(r"\briso: \{([^}]*)\}", f.read())
        night = tui().RISO["night"]  # the pane sits on the terminal's own (dark) background
        self.assertEqual(dict(re.findall(r"(\w+): 0x([0-9a-f]{6})", riso.group(1))),
                         {"ok": night["ok"][1:].lower(), "err": night["bad"][1:].lower(),
                          "accent": night["accent"][1:].lower(), "accent2": night["accent2"][1:].lower()})
        apart = sum((a - b) ** 2 for a, b in zip(tui()._rgb(night["accent"]), tui()._rgb(night["accent2"]))) ** 0.5
        self.assertGreater(apart, 150, "running and reading rows told apart (blue and sunflower, not two blues)")
        with open(os.path.join(root, ".claude-plugin", "marketplace.json"), encoding="utf-8") as f:
            listed = next(p["version"] for p in json.load(f)["plugins"] if p["name"] in ("sampo-ui", "foreman-ui"))
        self.assertEqual(listed, ssettings.mod_manifest()["version"], "a new option ships with a new version, both")

    def test_the_glyph_probe_writes_nothing_into_a_pipe(self):
        from sampo import smithterm
        r, w = os.pipe()
        try:
            self.assertEqual(smithterm.probe(r, w, "▀"), ({}, ""))
            os.set_blocking(r, False)
            with self.assertRaises(BlockingIOError):
                os.read(r, 100)
        finally:
            os.close(r)
            os.close(w)

    def test_the_glyph_probe_measures_where_the_cursor_lands_and_keeps_keys_typed_meanwhile(self):
        import pty
        import tty
        from sampo import smithterm
        master, slave = pty.openpty()
        try:
            tty.setcbreak(slave)
            os.write(master, b"\x1b[1;2Rq\x1b[1;3R")  # the terminal's answers, and a key typed between them
            self.assertEqual(smithterm.probe(slave, slave, "▀◉", timeout=1), ({"▀": 1, "◉": 2}, "q"))
            self.assertIn("\x1b[6n", os.read(master, 4096).decode())
            t0 = time.monotonic()
            self.assertEqual(smithterm.probe(slave, slave, "▀◉", timeout=0.2), ({}, ""), "no answer: no width")
            self.assertLess(time.monotonic() - t0, 1, "a terminal that never answers costs the timeout, once")  # wall clock: probe's timeout=0.2 deadline
        finally:
            os.close(master)
            os.close(slave)

    def test_the_track_is_drawn_in_half_blocks_once_the_probe_says_they_fit(self):
        T = tui()
        cfg = dict(T.DEFAULTS, motion="off")
        app = self.app(RUNNING, cfg=cfg)
        row = lambda: T.render(app.state, 110, 32, cfg, app, t=100.0)
        flat = row().text().splitlines()[2]
        self.assertIn("━", flat)
        self.assertNotIn("▀", flat)
        for widths in ({"▀": 2}, {}):  # drawn two columns wide, or no answer: the flat track
            app.glyphs = widths
            self.assertEqual(row().text().splitlines()[2], flat)
        app.glyphs = {"▀": 1}
        cv = row()
        rail = cv.text().splitlines()[2]
        self.assertIn("▀", rail)
        self.assertNotIn("━", rail)
        x = rail.index("▀")
        spec = T.resolve(cv.cells[2][x][1], cfg, cv.bg[2][x])
        self.assertNotEqual(spec["fg"], spec["bg"], "a lit top and a shaded side: the rail stands up")

    def test_the_keys_poster_groups_every_command_as_keycaps_with_the_hush_key(self):
        app = self.app()
        app.key("?")
        text = self.text(app)
        for words in ("RUN", "SEE", "MOVE", " z ", "motion: full, reduced, off", " q ", "quit"):
            self.assertIn(words, text)


if __name__ == "__main__":
    import unittest
    unittest.main()


class UndoAndPR(unittest.TestCase):  # T-1352
    def closed_run(self):
        import subprocess
        T = tui()
        root, hist = tempfile.mkdtemp(), tempfile.mkdtemp()
        git = lambda *a: subprocess.run(["git", "-C", root, *a], capture_output=True, text=True, check=True).stdout  # noqa: E731
        git("init", "-q", "-b", "main")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "t")
        with open(os.path.join(root, "calc.py"), "w") as f:
            f.write("def add(a, b):\n    return a - b\n")
        git("add", ".")
        git("commit", "-qm", "seed")
        with open(os.path.join(root, "calc.py"), "w") as f:
            f.write("def add(a, b):\n    return a + b\n")
        git("commit", "-qam", "fix add (T-0001)")
        sha = git("rev-parse", "--short", "HEAD").strip()
        events = [{"event": "start", "request": "fix add", "schema": 1, "ts": 1760000000},
                  {"event": "task", "task": "T-0001"},
                  {"event": "closed", "text": f"T-0001 closed; committed {sha} (tokens: 1 in, 1 out)", "task": "T-0001"}]
        with open(os.path.join(hist, "T-0001.events.jsonl"), "w") as f:
            f.write("".join(json.dumps(e) + "\n" for e in events))
        app = T.App(T.DEFAULTS, spawn=lambda *a: None, root=root, history=hist)
        for e in events:
            app.feed(e)
        return T, app, git, sha, hist

    def test_u_twice_reverts_the_runs_commit_and_marks_its_history_undone(self):
        T, app, git, sha, hist = self.closed_run()
        app.key("u")
        self.assertIn("u again", app.message)
        self.assertIn(sha, app.message)
        app.key("u")
        self.assertIn("a - b", read(os.path.join(app.root, "calc.py")), "the run's change is reverted")
        self.assertIn("Revert", git("log", "-1", "--format=%s"))
        self.assertIn("undid T-0001", app.message)
        self.assertEqual(T.history(hist)[0]["status"], "undone")
        self.assertIn(" UNDONE ", "".join(t for t, _ in T._history_row(T.history(hist)[0])))
        app.key("u")
        app.key("u")
        self.assertEqual(git("log", "--format=%s").count("Revert"), 1, "undone once")

    def test_p_drafts_the_pr_and_enter_posts_it_through_fm_pr(self):
        T, app, git, sha, hist = self.closed_run()
        calls = []
        app.fm_output = lambda argv: calls.append(argv) or ("## Fix add\nbody" if "--post" not in argv else
                                                            "refused: T-0001 has no publish grant")
        app.key("P")
        self.assertEqual((app.mode, calls[-1]), ("diff", ["pr", "T-0001"]))
        self.assertIn("## Fix add", app.view_lines)
        self.assertIn("Enter posts", app.view_title)
        app.key("\n")
        self.assertEqual(calls[-1], ["pr", "T-0001", "--post"])
        self.assertIn("publish grant", app.message)

    def test_neither_acts_without_a_closed_run(self):
        T = tui()
        app = T.App(T.DEFAULTS, spawn=lambda *a: None)
        app.feed({"event": "start", "request": "fix add", "schema": 1})
        app.fm_output = lambda argv: self.fail("nothing to draft")
        app.key("u")
        app.key("u")
        app.key("P")
        self.assertEqual(app.mode, "main")


class Tickets(unittest.TestCase):  # T-1350
    def app(self):
        T = tui()
        self.started = []

        class Child:
            code = None

            def poll(self):
                return self.code
        app = T.App(T.DEFAULTS, spawn=lambda words, opts: self.started.append(words) or Child())
        app.start(["fix add"])
        return T, app

    def type(self, app, text):
        for ch in text:
            app.key(ch)

    def test_tab_turns_the_question_box_into_a_ticket_that_runs_when_this_run_closes(self):
        T, app = self.app()
        app.key("n")
        self.assertTrue(app.asking)
        app.key("\t")
        self.assertEqual((app.asking, app.ticketing), (False, True))
        self.assertIn("next ›", T.render(app.state, 120, 34, T.DEFAULTS, app, t=100.0).text())
        self.type(app, "fix typo")
        app.key("\n")
        app.key("n")
        app.key("\t")
        self.type(app, "add export")
        app.key("\n")
        self.assertEqual(app.tickets, ["fix typo", "add export"])
        text = T.render(app.state, 120, 34, T.DEFAULTS, app, t=100.0).text()
        self.assertIn("① fix typo", text)
        self.assertIn("② add export", text)
        app.child.code = 0
        app.feed({"event": "closed", "text": "T-0001 closed", "task": "T-0001"})
        app.feed({"event": "exit", "code": 0})
        self.assertEqual((self.started[-1], app.tickets), (["fix typo"], ["add export"]))

    def test_a_blocked_run_holds_the_rail(self):
        T, app = self.app()
        app.tickets = ["fix typo"]
        app.child.code = 1
        app.feed({"event": "blocked", "text": "T-0001 blocked: x", "reason": "x", "task": "T-0001"})
        app.feed({"event": "exit", "code": 1})
        self.assertEqual((self.started, app.tickets), ([["fix add"]], ["fix typo"]))
        self.assertIn("tickets wait", " ".join(T.entry_text(e) for e in app.state.log))

    def test_the_tickets_view_picks_moves_and_drops(self):
        T, app = self.app()
        app.tickets = ["a", "b", "c"]
        app.key("T")
        self.assertEqual(app.mode, "tickets")
        self.assertIn("① a", T.render(app.state, 120, 34, T.DEFAULTS, app, t=100.0).text())
        app.key(" ")
        app.key("KEY_DOWN")
        app.key("KEY_DOWN")
        app.key(" ")
        self.assertEqual(app.tickets, ["b", "c", "a"])
        app.key("KEY_DC")
        self.assertEqual(app.tickets, ["b", "c"])
        app.key("\x1b")
        self.assertEqual(app.mode, "main")


class Deck(unittest.TestCase):  # T-1345
    PROJECTS = [{"project": "alpha", "root": "/w/a", "exists": True, "updated": 300.0, "queue": 2, "inbox": 1,
                 "blocked": 1, "runner": "claude",
                 "active": {"id": "T-0007", "title": "fix login", "stage": "executing", "steps_done": 1,
                            "steps_total": 3}},
                {"project": "beta", "root": "/w/b", "exists": True, "updated": 200.0, "queue": 0, "inbox": 0,
                 "blocked": 0, "active": None},
                {"project": "gamma", "root": "/w/g", "exists": True, "updated": 100.0, "queue": 0, "inbox": 4,
                 "blocked": 0, "active": None}]

    def app(self, motion="full"):
        T = tui()
        app = T.App(dict(T.DEFAULTS, motion=motion), spawn=lambda *a: None,
                    projects=lambda: [dict(p) for p in self.PROJECTS], project_state=lambda p: {"code_dir": None})
        app.show_home()
        return T, app

    def test_each_project_is_a_card_with_its_track_task_badges_and_runner(self):
        T, app = self.app()
        cv = T.render(app.state, 120, 34, app.cfg, app, t=100.0)
        text = cv.text()
        for want in ("alpha", "beta", "gamma", "claude", "T-0007", "fix login", "work 1/3", "blocked 1", "inbox 4",
                     "nothing active"):
            self.assertIn(want, text)
        self.assertEqual(len(app.deck_rects), 3)
        (ya, xa, _, _), (yb, xb, _, _), (yg, xg, _, _) = (app.deck_rects[i] for i in range(3))
        self.assertEqual((ya, yb), (ya, ya), "two cards side by side at 120 columns")
        self.assertGreater(yg, ya, "the third on the next row")
        self.assertEqual(app.deck_cols, 2)
        cv = T.render(app.state, 60, 34, app.cfg, app, t=100.0)
        self.assertEqual(app.deck_cols, 1, "one column when narrow")

    def test_arrows_move_by_card_in_the_grid(self):
        T, app = self.app()
        T.render(app.state, 120, 34, app.cfg, app, t=100.0)
        app.key("KEY_RIGHT")
        self.assertEqual(app.item_index, 1)
        app.key("KEY_LEFT")
        app.key("KEY_DOWN")
        self.assertEqual(app.item_index, 2, "down: the card below")
        app.key("KEY_DOWN")
        self.assertEqual(app.item_index, 2, "the last row stays")
        app.key("KEY_UP")
        self.assertEqual(app.item_index, 0)

    def test_the_ring_glides_and_the_project_view_grows_out_of_its_card_unless_motion_is_off(self):
        for motion, moves in (("full", True), ("off", False)):
            T, app = self.app(motion)
            app.clock = lambda: 100.0
            T.render(app.state, 120, 34, app.cfg, app, t=100.0)
            app.key("KEY_RIGHT")
            mid = T.render(app.state, 120, 34, app.cfg, app, t=100.05)
            self.assertEqual(app.deck_ring != app.deck_rects[1][:2], moves, f"{motion}: the ring is on its way")
            T.render(app.state, 120, 34, app.cfg, app, t=101.0)
            self.assertEqual(app.deck_ring, app.deck_rects[1][:2], f"{motion}: and lands")
            app.key("\n")
            self.assertEqual(app.mode, "project", "Enter opens it at once; the grow is drawing only")
            early = T.render(app.state, 120, 34, app.cfg, app, t=100.05).text()
            late = T.render(app.state, 120, 34, app.cfg, app, t=101.0).text()
            self.assertEqual("beta" in late and early != late, moves, f"{motion}: it grows out of the card")
            del mid


class FirstRun(unittest.TestCase):  # T-1333
    CHECKS = [("fm on PATH", False, "l", "link it"), ("a runner", True, "m", "pick one"),
              ("this folder is a project", False, "i", "make it one")]

    def app(self, past=False):
        hist = tempfile.mkdtemp()
        if past:
            with open(os.path.join(hist, "T-0001.events.jsonl"), "w") as f:
                f.write(json.dumps(RUN[0]) + "\n")
        app = tui().App(tui().DEFAULTS, spawn=lambda *a, **k: None, history=hist, root=tempfile.mkdtemp())
        app.setup_checks = lambda: list(self.CHECKS)
        return app

    def test_the_welcome_before_a_first_run_shows_three_chips_and_their_fixes(self):
        app = self.app()
        text = tui().render(app.state, 110, 30, tui().DEFAULTS, app).text()
        for what in ("fm on PATH", "a runner", "this folder is a project", "l  link it", "i  make it one",
                     "e  ask: explain this repo"):
            self.assertIn(what, text)
        self.assertNotIn("m  pick one", text, "a check that passes needs no fix")
        fixed = []
        app._link_fm = lambda: fixed.append("l") or "linked"
        app._init_project = lambda: fixed.append("i") or "made"
        app.key("l")
        app.key("i")
        self.assertEqual(fixed, ["l", "i"])
        app.key("e")
        self.assertEqual((app.mode, app.asking, app.input), ("prompt", True, "explain this repo"))

    def test_a_project_with_a_past_run_shows_the_usual_welcome(self):
        app = self.app(past=True)
        text = tui().render(app.state, 110, 30, tui().DEFAULTS, app).text()
        self.assertNotIn("fm on PATH", text)
        self.assertIn("new request", text)


class Tour(unittest.TestCase):  # T-1309
    def test_the_tour_feeds_the_sample_run_on_its_clock_and_captions_each_panel(self):
        now = [10.0]
        app = tui().App(tui().DEFAULTS, spawn=lambda *a, **k: None)
        app.clock = lambda: now[0]
        app.start_tour()
        app.poll()
        self.assertEqual(len(app.state.log) >= 0 and app.tour[1], 1)
        self.assertTrue(app.caption.startswith("fm code works one task to proof"), app.caption)
        self.assertGreaterEqual(app.fps(now[0]), 4, "the next event comes on time")
        now[0] += tui().TOUR_GAP * 7
        app.poll()
        self.assertEqual(app.tour[1], 8)
        self.assertIn("guard", app.caption)
        text = tui().render(app.state, 110, 32, tui().DEFAULTS, app, t=now[0]).text()
        self.assertIn("▶ The guard", text)
        now[0] += 100
        app.poll()
        self.assertEqual(app.state.status, "closed")
        self.assertTrue(app.caption.startswith("Closed and committed"))
        text = tui().render(app.state, 110, 32, tui().DEFAULTS, app, t=now[0]).text()
        captions = " ".join(tui().TOUR.values())
        for panel in ("Checks", "Activity", "Tokens per call", "Changed", "Time"):
            self.assertIn(panel, text, "a panel the tour names is drawn")
            self.assertIn(panel, captions, "and the tour names it")
        self.assertAlmostEqual(len(tui().TOUR_RUN) * tui().TOUR_GAP + tui().TOUR_HOLD, 30, delta=3)

    def test_the_tour_as_one_looping_smil_svg(self):
        import xml.etree.ElementTree as ET
        path = os.path.join(tempfile.mkdtemp(), "tour.svg")
        tui().tour_svg(path, (100, 30))
        raw = read(path)
        self.assertNotIn("<script", raw)
        root = ET.fromstring(raw)
        ns = "{http://www.w3.org/2000/svg}"
        frames = [g for g in root.iter(ns + "g") if g.find(ns + "set") is not None]
        self.assertEqual(len(frames), len(tui().TOUR_RUN), "a frame per event")
        self.assertEqual([g.get("visibility") for g in frames].count("visible"), 1, "the last one stands still")
        clock = next(a for a in root.iter(ns + "animate") if a.get("id") == "tour")
        self.assertEqual(clock.get("begin"), "0s;tour.end", "it loops")
        self.assertIn("The guard", raw)
        self.assertEqual(raw.count("<style>"), 1)


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class Extras(ForemanTestCase):
    """T-1546: the photo booth (F) writes the frame on screen as an SVG and, with rsvg-convert, a PNG; the chaos twin
    throws hostile events, keys and sizes at a headless app for N seconds without a crash."""

    def test_the_photo_booth_writes_an_svg_and_a_png(self):
        T = tui()
        app = T.App(T.DEFAULTS, spawn=lambda *a, **k: None)
        for e in T.SAMPLE_RUN:
            app.feed(e)
        folder = os.path.join(self.tmp, "photos")
        svg, png, note = T.photo(app, folder, (80, 24), 0.0)
        self.assertTrue(read(svg).startswith("<svg"))
        if shutil.which("rsvg-convert"):
            with open(png, "rb") as f:
                self.assertEqual(f.read(8), b"\x89PNG\r\n\x1a\n")
        else:
            self.assertIsNone(png)
            self.assertIn("rsvg-convert", note)
        self.assertNotEqual(T.photo(app, folder, (80, 24), 0.0)[0], svg, "a second photo never overwrites the first")
        app.size = (80, 24)
        app.key("F")
        self.assertIn("photo: ", app.message)
        self.assertIn(os.path.join(c.foreman_home(), "photos"), app.message)

    def test_the_chaos_twin_runs_without_a_crash(self):
        got = tui().chaos(2.0, seed=7)
        self.assertIsNone(got["crash"], got["crash"])
        self.assertTrue(got["frames"] > 20 and got["keys"] and got["events"] and got["resizes"], got)
        p = self.fm("code", "tui", "--chaos", "1", "--seed", "3")
        self.assertIn("(seed 3): no crash", p.stdout)

    @unittest.skipUnless(shutil.which("rsvg-convert"), "a PNG needs rsvg-convert")
    def test_a_png_snapshot_of_a_recorded_run(self):
        out = os.path.join(self.tmp, "run.png")
        self.fm("code", "tui", "--snapshot", out, "--events", os.path.join(os.path.dirname(PLUGIN), "demo", "examples",
                                                                          "fix.jsonl"))
        with open(out, "rb") as f:
            self.assertEqual(f.read(8), b"\x89PNG\r\n\x1a\n")
