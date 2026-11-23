"""The UI snapshot harness (mods/sampo-ui/tools/shot.py) never leaks FOREMAN_* into a tmux server's global
environment, where every later session would inherit it (T-0138: a leaked FOREMAN_STATE pointed the user's own
Claude Code session at the demo state)."""
import json
import os
import shutil
import subprocess
import sys
import unittest

from helpers import ForemanTestCase

SHOT = os.path.join(os.path.dirname(__file__), "..", "..", "mods", "sampo-ui", "tools", "shot.py")


@unittest.skipUnless(shutil.which("tmux"), "needs tmux")
class Harness(unittest.TestCase):
    def test_foreman_variables_stay_in_the_harness_session(self):
        env = dict(os.environ, FOREMAN_STATE="/tmp/fm-demo-state", FOREMAN_SESSION_ID="x",
                   FM_SHOT_SOCKET=f"fm-shot-test-{os.getpid()}")  # T-0744: two suites at once never share it
        p = subprocess.run([sys.executable, SHOT, "selftest"], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_watch_refuses_outside_tmux_and_stops_on_time(self):
        # T-0143: 'add a hook to screenshot every few seconds'; run detached, it must never outlive --for
        env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        p = subprocess.run([sys.executable, SHOT, "watch", os.devnull, "--every", "1", "--for", "0"], env=env,
                           capture_output=True, text=True, timeout=30)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("not inside tmux", p.stderr)


class OneOff(unittest.TestCase):
    def test_a_session_with_its_own_state_is_a_working_session(self):
        # T-1013: the demo recorder runs its session on a state of its own; marked one-off (T-0995) it was never driven
        # to close its task, and the recording timed out with nothing written
        import argparse
        import importlib.util
        spec = importlib.util.spec_from_file_location("shot_t1013", SHOT)
        shot = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(shot)
        seen = []
        shot.tmux = lambda *args, check=True: seen.append(args)
        a = argparse.Namespace(dir=".", cols=10, rows=5, model=None)
        saved = os.environ.pop("FOREMAN_STATE", None)
        try:
            shot.start(a, cmd="true")  # the user's own state: a one-off, never driven
            shared = " ".join(seen[-1])
            os.environ["FOREMAN_STATE"] = "/tmp/fm-demo-state"
            shot.start(a, cmd="true")
            own = " ".join(seen[-1])
        finally:
            os.environ.pop("FOREMAN_STATE", None)
            if saved is not None:
                os.environ["FOREMAN_STATE"] = saved
        self.assertIn("FOREMAN_ONEOFF=1", shared)
        self.assertNotIn("FOREMAN_STATE=", shared)
        self.assertIn("FOREMAN_STATE=/tmp/fm-demo-state", own)
        self.assertNotIn("FOREMAN_ONEOFF", own)

    def test_settings_reach_claude_as_one_quoted_argument(self):
        # T-2055: the demo recorder turns off every non-Foreman plugin through --settings, so a recording shows Foreman alone
        import argparse
        import importlib.util
        spec = importlib.util.spec_from_file_location("shot_t2055", SHOT)
        shot = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(shot)
        seen = []
        shot.tmux = lambda *args, check=True: seen.append(args)
        shot.answer_trust = lambda *a, **k: False
        js = '{"enabledPlugins": {"x@y": false}}'
        shot.start(argparse.Namespace(dir=".", cols=10, rows=5, model=None, settings=js))
        self.assertTrue(seen[-1][-1].endswith(" --settings '" + js + "'"), seen[-1][-1])
        shot.start(argparse.Namespace(dir=".", cols=10, rows=5, model=None))
        self.assertNotIn("--settings", seen[-1][-1])

    def test_only_foreman_turns_off_every_other_plugin(self):
        import importlib.util
        import json
        import tempfile
        spec = importlib.util.spec_from_file_location("record_live_t2055",
                                                      os.path.join(os.path.dirname(SHOT), "..", "..", "..", "demo", "record_live.py"))
        rl = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rl)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "settings.json")
            self.assertIsNone(rl.only_foreman(p))  # missing
            with open(p, "w") as f:
                f.write("{not json")
            self.assertIsNone(rl.only_foreman(p))  # unreadable
            with open(p, "w") as f:
                json.dump({"enabledPlugins": {"ponytail@x": True, "sampo@sampo": True, "sampo-ui@sampo": True,
                                              "old@y": False, "claude-hud@z": True}}, f)
            got = json.loads(rl.only_foreman(p))
        self.assertEqual(got, {"enabledPlugins": {"ponytail@x": False, "claude-hud@z": False}})
        # a disabled plugin's hooks.json warning still prints at startup: blanked up to the Foreman panel's │
        screen = ("\x1b[1mClaude Code\x1b[0m v2\n\x1b[33m⚠\x1b[39m Your login expires in 2 days · run /login\n"
                  "│ Your Max plan includes $200 a month in API credi│ foreman-ui\n"
                  "│ at https://clau.de/api-credits\n"
                  "● ecc: hooks.json: unknown keys \"description\" in hooks.PreToolUse[0] │ Next  queue is empty\n"
                  "  hooks.PreToolUse[0], \"description\" in hooks.PreToolUse[1] and 43 more ignored\n> FIX: calc.add")
        for sep in ("\n", "\r\n"):
            got = rl.quiet(screen.replace("\n", sep), sep).split(sep)
            plain = [rl.ANSI.sub("", x) for x in got]
            self.assertEqual([len(x) for x in plain], [len(rl.ANSI.sub("", x)) for x in screen.split("\n")])
            self.assertEqual([x.strip() for x in plain if x.strip()],
                             ["Claude Code v2", "│ foreman-ui", "│ Next  queue is empty", "> FIX: calc.add"])

    def test_the_harness_keeps_true_color(self):
        # T-1209: Claude Code drops to 256 colours whenever TMUX is set unless CLAUDE_CODE_TMUX_TRUECOLOR is; the demo
        # was recorded with every colour on the 256 palette, so the pane's tinted chart wells became neutral gray blocks
        import argparse
        import importlib.util
        spec = importlib.util.spec_from_file_location("shot_t1209", SHOT)
        shot = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(shot)
        seen = []
        shot.tmux = lambda *args, check=True: seen.append(args)
        shot.start(argparse.Namespace(dir=".", cols=10, rows=5, model=None), cmd="true")
        args = seen[-1]
        self.assertIn("CLAUDE_CODE_TMUX_TRUECOLOR=1", args)
        self.assertEqual(args[args.index("CLAUDE_CODE_TMUX_TRUECOLOR=1") - 1], "-e")



class FullPage(ForemanTestCase):
    """T-6029: sampo shot --height H sets the viewport's height and --full captures the whole page, not the fold: a
    live Board's 14 panels showed only 9 in the fixed 1280x800 viewport. A real Chromium, against a page taller than
    it (the PNG's header says how tall each shot is)."""
    TALL = 3000

    def setUp(self):
        super().setUp()
        from helpers import headless_chrome
        self.chrome = headless_chrome()
        self.fm("init")
        self.out = os.path.join(self.tmp, "shots")
        self.tall = os.path.join(self.repo, "tall.html")
        with open(self.tall, "w") as f:  # a stack of panels: the second half starts below the fold
            f.write("<style>body{margin:0}div{height:1000px}</style>"
                    "<div style='background:#c00'></div><div style='background:#0a0'></div>"
                    "<div style='background:#00c'></div>")

    def shoot(self, *args, page=None):
        if not self.chrome:
            self.skipTest("no headless Chromium")
        data = json.loads(self.fm("shot", page or self.tall, "--out", self.out, "--json", *args).stdout)
        return data["pages"]

    def size(self, path):
        from sampo import img
        i = img.info(path)
        return i["width"], i["height"]

    def test_the_viewport_stops_at_the_fold_and_height_moves_the_fold(self):
        [p] = self.shoot("--frames", "0")
        self.assertEqual(self.size(p["shots"][0]), (1280, 800))
        [p] = self.shoot("--frames", "0", "--height", "500")
        self.assertEqual(self.size(p["shots"][0]), (1280, 500))
        [p] = self.shoot("--frames", "0", "--height", "1500", "--width", "600")
        self.assertEqual(self.size(p["shots"][0]), (600, 1500))

    def test_full_is_the_whole_page_whatever_the_viewport(self):
        for extra, want in (([], (1280, 3000)), (["--height", "500"], (1280, 3000)), (["--width", "400"], (400, 3000)),
                            (["--height", "5000"], (1280, 5000))):  # a viewport taller than the page: the viewport
            [p] = self.shoot("--frames", "0", "--full", *extra)
            self.assertEqual(self.size(p["shots"][0]), want, extra)

    def test_full_shoots_every_frame_the_poster_and_every_view_whole(self):
        page = os.path.join(self.repo, "spin.html")
        with open(page, "w") as f:
            f.write("<style>@keyframes pulse{from{background:#c00}to{background:#00c}}body{margin:0}"
                    "div{height:2400px;animation:pulse 1.2s linear infinite}</style><div></div>")  # width-proof
        pages = self.shoot("--frames", "3", "--every", "200", "--full", "--width", "500,300", "--scheme", "both",
                           page=page)
        self.assertEqual([os.path.basename(p["folder"]) for p in pages],
                         ["spin-500-light", "spin-500-dark", "spin-300-light", "spin-300-dark"])
        for p in pages:
            names = [os.path.basename(s) for s in p["shots"]]
            self.assertEqual(names[:4], ["viewport.png", "frame-00.png", "frame-01.png", "frame-02.png"])
            self.assertIn("poster.png", names, "the page moves")
            for s in p["shots"]:
                self.assertEqual(self.size(s), (p["width"], 2400), s)

    def test_a_page_shorter_than_the_viewport_is_the_viewport_and_a_huge_one_is_cut_with_a_note(self):
        short = os.path.join(self.repo, "short.html")
        with open(short, "w") as f:
            f.write("<p>hi</p>")
        [p] = self.shoot("--frames", "0", "--full", page=short)
        self.assertEqual(self.size(p["shots"][0]), (1280, 800))
        huge = os.path.join(self.repo, "huge.html")
        with open(huge, "w") as f:
            f.write("<style>body{margin:0}div{height:40000px;background:#c00}</style><div></div>")
        [p] = self.shoot("--frames", "0", "--full", "--width", "800", page=huge)
        w, h = self.size(p["shots"][0])
        self.assertEqual(w, 800)
        self.assertEqual(h, 16384, "cut where one texture ends")
        self.assertTrue(any("40000" in n and "16384" in n for n in p["notes"]), p["notes"])

    def test_a_bad_height_is_refused_before_any_browser(self):
        env = {"FOREMAN_CHROME": "none"}  # no browser: a good height is skipped, a bad one still fails
        for bad in ("0", "-5", "10001", "tall", "", "500,600", "1e3", " "):
            r = self.fm("shot", self.tall, "--height", bad, env=env, check=False)
            self.assertNotEqual(r.returncode, 0, bad)
            self.assertIn("--height", r.stderr + r.stdout, bad)
        for good in ("1", "10000", " 800 "):
            r = self.fm("shot", self.tall, "--height", good, env=env)
            self.assertEqual(r.returncode, 0, good)
            self.assertIn("skipped", r.stdout)
        r = self.fm("shot", self.tall, "--full", env=env)
        self.assertEqual(r.returncode, 0)

    def test_a_browser_that_cant_say_how_tall_the_page_is_fails_that_page(self):
        # a browser that answers getLayoutMetrics with nothing is a failed page, not a blank or a viewport-sized lie
        import stat
        stub = os.path.join(self.tmp, "chrome")
        with open(stub, "w") as f:
            f.write(f"#!{sys.executable}\n"
                    "import json, os, sys\nbuf = b''\n"
                    "while True:\n    c = os.read(3, 65536)\n    if not c: break\n    buf += c\n"
                    "    while b'\\0' in buf:\n        l, buf = buf.split(b'\\0', 1)\n        m = json.loads(l)\n"
                    "        r = {'targetId': 't'} if m['method'] == 'Target.createTarget' else "
                    "{'sessionId': 's'} if m['method'] == 'Target.attachToTarget' else {}\n"
                    "        os.write(4, json.dumps({'id': m['id'], 'result': r}).encode() + b'\\0')\n"
                    "        if m['method'] == 'Page.navigate':\n"
                    "            os.write(4, json.dumps({'method': 'Page.loadEventFired', 'params': {}}).encode() + b'\\0')\n")
        os.chmod(stub, stat.S_IRWXU)
        r = self.fm("shot", self.tall, "--out", self.out, "--full", "--frames", "0", "--json",
                    env={"FOREMAN_CHROME": stub}, check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("content size", json.loads(r.stdout)["failed"][0]["error"])


class Trust(unittest.TestCase):
    """T-1032: the harness stalled at Claude Code's folder-trust prompt on every new folder (each demo recording); it
    answers it itself, but only for a folder Foreman made (a temp or scratch folder, its own repo): trusting a cloned
    repo would let that repo's hooks run."""

    def load(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("shot_t1032", SHOT)
        shot = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(shot)
        return shot

    def test_it_answers_for_a_folder_foreman_made_and_never_for_others(self):
        import tempfile
        import types
        shot = self.load()
        sent, screens = [], []

        def tmux(*args, check=True):
            if args[0] == "capture-pane":
                return types.SimpleNamespace(stdout=screens.pop(0) if screens else "❯ ", returncode=0)
            sent.append(args)
            return types.SimpleNamespace(stdout="", returncode=0)
        shot.tmux = tmux
        from unittest import mock
        nap = mock.patch.object(shot.time, "sleep", lambda s: None)  # review: never the whole test process's sleep
        nap.start()
        self.addCleanup(nap.stop)
        dialog = "Quick safety check: Is this a project you created or one you trust?\n❯ No, exit\n  Yes, I trust this folder"
        with tempfile.TemporaryDirectory() as d:
            screens[:] = [dialog, "❯ "]
            self.assertTrue(shot.answer_trust(d, wait=2))
            self.assertTrue(any("Enter" in a for a in sent), sent)
        sent.clear()
        screens[:] = [dialog]
        self.assertFalse(shot.answer_trust("/srv/someone-elses-clone", wait=2))
        self.assertEqual(sent, [])
        screens[:] = ["❯ "]  # no dialog: nothing pressed
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(shot.answer_trust(d, wait=1))
        self.assertEqual(sent, [])
        with tempfile.TemporaryDirectory() as d:  # a repo cloned into /tmp with its own Claude Code config
            os.makedirs(os.path.join(d, ".claude"))
            screens[:] = [dialog]
            self.assertFalse(shot.answer_trust(d, wait=2))
        self.assertEqual(sent, [])

    def test_the_live_recorder_trusts_only_through_the_shared_check(self):
        # T-1034: demo/record_live.py pressed Down/Enter on the trust prompt for any folder, a cloned --repo included
        with open(os.path.join(os.path.dirname(SHOT), "..", "..", "..", "demo", "record_live.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn("shot.answer_trust(", src)
        self.assertNotIn('"Down")', src)
