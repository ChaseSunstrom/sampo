"""The line under the prompt (T-0092): one clean Foreman layout by default — a session line (model, folder, branch,
context, rate limits, cost) and a Foreman line, or a chip on the session line when the foreman-ui mod draws the band;
the old stack (your original command, claude-hud, two Foreman lines) only when opted in."""
import json
import os
import re
import subprocess

from helpers import CLEAN_READER, ForemanTestCase

from sampo import core as c

STATUSLINE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hooks", "statusline")
ANSI = re.compile(r"\x1b\[[0-9;]*m")


class StatuslineUi(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("task", "new", "Fix login", "--type", "FIX", "--tier", "S", "--ac", "works :: true", "--step", "reproduce",
                "--step", "fix", "--focus")
        self.uhome = os.path.join(self.tmp, "uhome")
        os.makedirs(os.path.join(self.uhome, ".claude"))
        self.manifest(statusLine_original={"type": "command", "command": "echo OLD-LINE"}, statusline_hud=False)

    def manifest(self, **kw):
        c.write_atomic(os.path.join(self.home, "state", "install-manifest.json"), json.dumps(kw))

    def settings(self, **kw):
        with open(os.path.join(self.uhome, ".claude", "settings.json"), "w") as f:
            json.dump(kw, f)

    def run_line(self, columns="160"):
        payload = {"session_id": "s-ui", "cwd": self.repo, "model": {"display_name": "Opus 5.5"},
                   "workspace": {"current_dir": self.repo, "project_dir": self.repo},
                   "cost": {"total_cost_usd": 12.89, "total_lines_added": 737, "total_lines_removed": 31},
                   "context_window": {"used_percentage": 31, "context_window_size": 1000000},
                   "rate_limits": {"five_hour": {"used_percentage": 5, "resets_at": 4102444800},
                                   "seven_day": {"used_percentage": 25, "resets_at": 4102444800}}}
        env = dict(os.environ, FOREMAN_HOME=self.home, HOME=self.uhome, COLUMNS=columns)
        env.pop("CLAUDE_CONFIG_DIR", None)
        p = subprocess.run([STATUSLINE], input=json.dumps(payload), capture_output=True, text=True, env=env,
                           timeout=15, cwd=self.repo)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout.rstrip("\n").split("\n")

    def test_default_is_one_session_line_and_one_foreman_line_in_true_color(self):
        self.fm("task", "step", "T-0001", "done", "1", "--evidence", "repro.sh", "fails as reported")
        raw = self.run_line()
        lines = [ANSI.sub("", l) for l in raw]
        self.assertEqual(len(lines), 2, lines)
        self.assertNotIn("OLD-LINE", "\n".join(lines), "the old stack is opt-in now")
        for needle in ("Opus 5.5", os.path.basename(self.repo), "ctx", "31%", "5h", "5%", "7d", "25%", "$12.89",
                       "+737", "−31"):
            self.assertIn(needle, lines[0])
        for needle in ("⊖ T-0001", "FIX · small", "executing", "━━━━━─────", "step 2/2", "fix", "audits 0/1", "q0 in0", "standard",
                       "guard on"):
            self.assertIn(needle, lines[1])
        self.assertIn("\x1b[38;2;", raw[0], "true color, not the old plain text")

    def test_the_old_stack_comes_back_when_opted_in(self):
        self.manifest(statusLine_original={"type": "command", "command": "echo OLD-LINE"}, statusline_hud=False,
                      statusline_layout="stack")
        lines = self.run_line()
        self.assertEqual(lines[0], "OLD-LINE")

    def test_with_the_mod_on_foreman_is_a_chip_on_the_session_line(self):
        self.settings(enabledPlugins={"sampo-ui@sampo": True})
        lines = [ANSI.sub("", l) for l in self.run_line()]
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("⊖ T-0001 0/2", lines[0])

    def test_narrow_terminals_get_a_cut_line_not_a_wrapped_one(self):
        for line in self.run_line(columns="40"):
            self.assertLessEqual(len(ANSI.sub("", line)), 40)

    def test_a_genesis_tower_shows_its_stage_amber_when_it_cant_go_on(self):  # T-2118
        from sampo import rune
        p = c.find_project(self.repo)
        rune.init(p, b"go\n")
        c.regen_views(p)
        self.assertIn("⌂ gen 1 · seed", ANSI.sub("", self.run_line("300")[1]))
        seed = os.path.join(self.tmp, "seed.md")
        with open(seed, "w") as f:
            f.write("# Seed\n\nnothing filed\n")
        # the put itself refreshes status.json; T-2166: CLEAN_READER makes its cold read pass
        self.fm("genesis", "put", "gen-1/seed.md", "--file", seed,
                env={"PATH": CLEAN_READER + os.pathsep + os.environ["PATH"]})
        raw = self.run_line("300")[1]
        self.assertIn("⌂ gen 1 · fatal", ANSI.sub("", raw))
        self.assertIn("\x1b[38;2;200;220;90m⌂", raw)  # WARN: the forge's warning (T-2876)

    def test_regen_views_writes_the_structured_status(self):
        st = json.loads(open(os.path.join(c.find_project(self.repo).dir, "status.json")).read())
        self.assertEqual(st["active"]["id"], "T-0001")
        self.assertEqual((st["active"]["done"], st["active"]["total"], st["active"]["step"]), (0, 2, "reproduce"))
        self.assertEqual((st["queue"], st["inbox"], st["autonomy"]), (0, 0, "standard"))

    def test_a_failing_snapshot_or_odd_columns_never_costs_the_lines(self):
        sessions = os.path.join(self.home, "state", "sessions")
        os.makedirs(os.path.dirname(sessions), exist_ok=True)
        if os.path.isdir(sessions):
            os.rename(sessions, sessions + ".bak")
        with open(sessions, "w") as f:  # a file where the snapshot dir should be: every snapshot write fails
            f.write("x")
        lines = [ANSI.sub("", l) for l in self.run_line(columns="abc")]
        self.assertEqual(len(lines), 2, lines)
        self.assertIn("⊖ T-0001", lines[1])


class Palette(ForemanTestCase):
    """T-2876 audit: the forge colours the line takes still read on a dark terminal and a light one (the line can't
    tell which it is on): text 4.5:1 on a dark one and 3:1 on white, the empty track a 3:1 graphic on a dark one."""

    def colours(self):
        import ast
        with open(STATUSLINE, encoding="utf-8") as f:
            for node in ast.parse(f.read()).body:
                if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Tuple):
                    names = [t.id for t in node.targets[0].elts if isinstance(t, ast.Name)]
                    if "DIM" in names:
                        return dict(zip(names, ast.literal_eval(node.value)))
        raise AssertionError("no colour tuple in the statusline")

    @staticmethod
    def contrast(a, b):
        def lum(x):
            ch = [((x >> s) & 255) / 255 for s in (16, 8, 0)]
            ch = [v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4 for v in ch]
            return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2]
        hi, lo = sorted((lum(a), lum(b)), reverse=True)
        return (hi + 0.05) / (lo + 0.05)

    def test_dim_text_and_the_track_read_on_dark_and_light_terminals(self):
        c_ = self.colours()
        dark, white = 0x1E1E1E, 0xFFFFFF
        self.assertGreaterEqual(self.contrast(c_["DIM"], dark), 4.5)
        self.assertGreaterEqual(self.contrast(c_["DIM"], white), 3)
        self.assertGreaterEqual(self.contrast(c_["TRACK"], dark), 3)
        self.assertEqual(c_["ACCENT"], 0xFFC24D, "the forge's gold, as the pane")
