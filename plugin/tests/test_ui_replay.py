"""T-1534: the foreman-ui pane drawn from a recorded fixture with no live session, and two runs of one fixture
byte-identical. Tests never reach the real claude (T-0283), so the drawing itself is the mod's own test,
mods/foreman-ui/tests/replay.test.tsx, run by `claude plugin test mods/foreman-ui` (a gate) and, two processes and an
environment matrix apart, by mods/foreman-ui/tools/pane_replay.py twin; this checks the tool, the fixture and the
wiring."""
import base64
import contextlib
import importlib.util
import io
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

MOD = os.path.join(os.path.dirname(__file__), "..", "..", "mods", "sampo-ui")
FIXTURE = os.path.join(MOD, "tests", "fixtures", "pane-busy.ts")


def load():
    spec = importlib.util.spec_from_file_location("pane_replay_t1534", os.path.join(MOD, "tools", "pane_replay.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def read(*parts):
    with open(os.path.join(MOD, *parts), encoding="utf-8") as f:
        return f.read()


def text(s):
    return {"type": "Text", "props": {}, "children": [s]}


class Layout(unittest.TestCase):
    """The drawn tree as plain text."""

    def test_columns_stack_rows_sit_side_by_side_and_rasters_draw_their_cells(self):
        cells = base64.b64encode(b"".join(struct.pack("<III", ord(c), 0, 0) for c in "━━─")).decode()
        tree = {"type": "Box", "props": {"flexDirection": "column", "paddingX": 1}, "children": [
            {"type": "Box", "props": {"flexDirection": "row", "gap": 1}, "children": [
                text("FIX"), {"type": "Text", "props": {}, "children": [text("T-0007"), " Login"]}]},
            {"type": "Box", "props": {"flexDirection": "row"}, "children": [
                {"type": "Box", "props": {"flexDirection": "column"}, "children": [text("a"), text("b")]},
                {"type": "Raster", "props": {"columns": 3, "rows": 1, "cells": cells}}]},
            {"type": "Button", "props": {"label": "next"}},
        ]}
        self.assertEqual(load().text(tree, 12, 5), " FIX T-0007\n a━━─\n b\n next\n\n", "five rows, each ending in a newline")

    def test_a_line_wider_than_the_pane_is_cut_and_a_fixed_height_clips(self):
        tree = {"type": "Box", "props": {"flexDirection": "column", "height": 1}, "children": [
            text("界" * 10), text("hidden")]}
        self.assertEqual(load().text(tree, 5, 2), "界界\n\n")

    def test_odd_cells_and_nodes_draw_as_blanks_not_errors(self):
        cells = base64.b64encode(struct.pack("<III", 0xD800, 0, 0) + struct.pack("<III", 0, 0, 0) + b"\x01").decode()
        tree = {"type": "Box", "props": {}, "children": [
            {"type": "Raster", "props": {"columns": 2, "cells": cells}}, None, 7, {"type": "Engine", "children": ["x"]}]}
        self.assertEqual(load().text(tree, 10, 3), "\n7\nx\n")

    def test_escape_sequences_controls_and_bidi_never_reach_the_terminal(self):
        # review: a crafted title retitled the terminal and cleared the screen through replay's own stdout
        evil = "\x1b]0;x\x07\x1b[2J\x9b31m\u202eA\u2066\x00\x7f\tB\nC\x1bPq#0\x1b\\D"
        cells = base64.b64encode(b"".join(struct.pack("<III", c, 0, 0) for c in (0x9B, 0x202E, ord("x")))).decode()
        tree = {"type": "Box", "props": {"flexDirection": "column"}, "children": [
            text(evil), {"type": "Button", "props": {"label": "\x1b[31mgo"}},
            {"type": "Input", "props": {"placeholder": "\x07idea"}},
            {"type": "Raster", "props": {"columns": 3, "cells": cells}}]}
        self.assertEqual(load().text(tree, 20, 6), "A  B\nCD\ngo\nidea\n  x\n\n", "the newline still splits")


class Tool(unittest.TestCase):
    def test_the_view_reaches_the_kit_as_data_never_as_code(self):
        # a title can hold anything: quotes, a template literal, a closing tag, a __proto__ key
        view = {"title": "\"'`${process.exit(1)}`</x>  }, print: false, x: {", "__proto__": {"polluted": True}}
        src = load().options(view, 80, 40)
        m = re.fullmatch(r'export default \{ view: JSON\.parse\(("(?:[^"\\]|\\.)*")\), columns: 80, rows: 40, '
                         r'print: true \}\n', src)
        self.assertIsNotNone(m, src)
        self.assertEqual(json.loads(json.loads(m.group(1))), view)

    def test_twin_runs_diff_on_the_text_else_on_the_tree(self):
        tool = load()
        same = ("run 1", '{"c":"#fff"}', "a\n")
        self.assertEqual(tool.diff([same, ("run 2", *same[1:])]), [])
        moved = tool.diff([same, ("run 2", '{"c":"#fff"}x', "b\n")])
        self.assertIn("-a\n", moved)
        self.assertIn("+b\n", moved)
        tinted = "".join(tool.diff([same, ("TZ=x", '{"c":"#000"}', "a\n")]))  # same text, another colour
        self.assertIn('+ "c": "#000"', tinted)
        self.assertIn("+++ TZ=x", tinted)

    def test_the_copy_holds_the_replay_test_alone_with_its_own_options(self):
        tool = load()
        with tempfile.TemporaryDirectory() as tmp:
            mod = tool.stage({"v": 1, "project": "p"}, 100, 30, tmp)
            tests = sorted(n for _, _, files in os.walk(mod) for n in files if re.search(r"\.test\.tsx?$", n))
            self.assertEqual(tests, ["replay.test.tsx"], "the mod's other tests stay behind")
            self.assertFalse(os.path.exists(os.path.join(mod, "tools")))
            self.assertTrue(os.path.isfile(os.path.join(mod, ".claude-plugin", "plugin.json")))
            self.assertTrue(os.path.isfile(os.path.join(mod, "hooks", "register.tsx")))
            with open(os.path.join(mod, "tests", "fixtures", "replay.ts"), encoding="utf-8") as f:
                self.assertIn("columns: 100, rows: 30, print: true", f.read())
        self.assertIn("print: false", read("tests", "fixtures", "replay.ts"), "the mod's own run stays quiet")
        with self.assertRaises(ValueError):
            tool.stage(["not", "a", "view"], 80, 40, tempfile.gettempdir())

    def test_the_kit_test_is_wired(self):
        # a wiring check only: the engine proof is the kit test itself, run by claude plugin test mods/foreman-ui
        src = read("tests", "replay.test.tsx")
        self.assertIn("from './fixtures/replay'", src)
        self.assertIn(f"const MARK = '{load().MARK}'", src, "the tool finds the line the test prints")
        self.assertIn("component: 'Pane'", src)
        self.assertIn("mock.clock(on)", src, "a frozen clock")
        self.assertIn("expect(drawn).toBe(runs[0]!)", src, "the second run draws the same bytes")


class Run(unittest.TestCase):
    """The child claude, played by a stub first on PATH (never the real one, T-0283)."""

    def stub(self, tmp, body):
        path = os.path.join(tmp, "claude")
        with open(path, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\n" + body)
        os.chmod(path, 0o755)
        return {**load().PINNED, "PATH": tmp + os.pathsep + os.environ.get("PATH", "")}

    def test_a_failed_run_reports_its_output_cleaned_and_decoded(self):
        tool = load()
        with tempfile.TemporaryDirectory() as tmp:
            env = self.stub(tmp, "printf '\\033]0;x\\007\\033[2Jboom \\377\\n'\nexit 1\n")
            with self.assertRaises(RuntimeError) as e:
                tool.drawn({"v": 1}, 80, 40, env)
        msg = str(e.exception)
        self.assertIn("exit 1", msg)
        self.assertIn("boom \ufffd", msg, "bytes that aren't UTF-8 read as replacement characters")
        self.assertNotIn("\x1b", msg)
        self.assertNotIn("\x07", msg)

    def test_a_run_past_the_timeout_ends_claude_and_every_child(self):
        tool = load()
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = os.path.join(tmp, "child.pid")
            env = self.stub(tmp, f"sleep 30 &\necho $! > {pid_file}\nsleep 30\n")
            with mock.patch.object(tool, "TIMEOUT", 1), self.assertRaises(subprocess.TimeoutExpired):
                tool.drawn({"v": 1}, 80, 40, env)
            with open(pid_file, encoding="utf-8") as f:
                child = int(f.read())
        end = time.monotonic() + 5
        while time.monotonic() < end and alive(child):
            time.sleep(0.05)
        self.assertFalse(alive(child), "claude's own child outlived the timeout")

    def test_render_json_prints_the_tree_escaped(self):
        tool = load()
        line = '{"type":"Text","children":["\u009b2J\u202eok\\u001b"]}'  # JSON.stringify escapes C0, not C1 or bidi
        out = io.StringIO()
        with mock.patch.object(tool, "drawn", return_value=line), mock.patch.object(sys, "argv", [
                "pane_replay.py", "render", FIXTURE, "--json"]), contextlib.redirect_stdout(out):
            self.assertEqual(tool.main(), 0)
        printed = out.getvalue()
        self.assertTrue(printed.isascii(), printed)
        self.assertEqual(json.loads(printed), json.loads(line))


class Frames(unittest.TestCase):
    """T-1670: a replayed frame as SVG (PNG with rsvg-convert), and the committed golden frame the kit test checks."""

    def test_a_frame_becomes_an_escaped_monospace_svg(self):
        import xml.etree.ElementTree as ET
        svg = load().svg("  demo-1a2b3c\n<b> & co\n\n  T-0007\n", 80, 4)
        root = ET.fromstring(svg)
        texts = [t.text for t in root.iter("{http://www.w3.org/2000/svg}text")]
        self.assertEqual(texts, ["  demo-1a2b3c", "<b> & co", "  T-0007"])
        self.assertIn("monospace", root.get("font-family"))
        self.assertEqual(root.find("{http://www.w3.org/2000/svg}g").get("{http://www.w3.org/XML/1998/namespace}space"),
                         "preserve")

    def test_a_line_separator_in_a_title_stays_on_its_row(self):  # T-2048
        import xml.etree.ElementTree as ET
        root = ET.fromstring(load().svg("  a b title\n  T-0007\n", 80, 2))
        self.assertEqual(len(list(root.iter("{http://www.w3.org/2000/svg}text"))), 2)

    def test_the_verb_writes_the_svg_of_a_replay(self):
        tool = load()
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "pane.svg")
            with mock.patch.object(tool, "drawn", return_value=json.dumps(text("hello pane"))), \
                    mock.patch.object(sys, "argv", ["pane_replay.py", "svg", FIXTURE, "--out", out]), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(tool.main(), 0)
            with open(out, encoding="utf-8") as f:
                self.assertIn(">hello pane</text>", f.read())

    def test_golden_writes_then_compares_and_names_the_drift(self):
        tool = load()
        with tempfile.TemporaryDirectory() as tmp:
            gold = os.path.join(tmp, "golden.ts")
            with mock.patch.object(tool, "GOLDEN", gold), mock.patch.object(tool, "drawn", return_value='{"a":1}'):
                same, lines = tool.golden()  # T-1670 review: a missing golden is never blessed by a compare
                self.assertFalse(same)
                self.assertIn("golden --update", "".join(lines))
                self.assertIsNone(tool.read_golden())
                self.assertEqual(tool.golden(update=True), (True, []))
                self.assertEqual(tool.read_golden(), '{"a":1}')
                self.assertEqual(tool.golden(), (True, []))
            with mock.patch.object(tool, "GOLDEN", gold), mock.patch.object(tool, "drawn", return_value='{"a":2}'):
                same, lines = tool.golden()
                self.assertFalse(same)
                self.assertIn('- "a": 1\n', lines)
                self.assertEqual(tool.read_golden(), '{"a":1}', "a compare never rewrites it")
                self.assertEqual(tool.golden(update=True), (True, []))
                self.assertEqual(tool.read_golden(), '{"a":2}')
            with open(gold, "w", encoding="utf-8") as f:
                f.write("<<<<<<< HEAD\nexport default 'x\n")  # a conflicted golden: --update still recovers
            with mock.patch.object(tool, "GOLDEN", gold), mock.patch.object(tool, "drawn", return_value='{"a":3}'):
                self.assertEqual(tool.golden(update=True), (True, []))
                self.assertEqual(tool.read_golden(), '{"a":3}')

    def test_golden_drift_shows_the_text_rows_not_the_tree(self):  # T-1670 review: rasters are 30 KB lines
        tool = load()
        with tempfile.TemporaryDirectory() as tmp:
            gold = os.path.join(tmp, "golden.ts")
            with mock.patch.object(tool, "GOLDEN", gold), \
                    mock.patch.object(tool, "drawn", return_value=json.dumps(text("old row"))):
                tool.golden(update=True)
            with mock.patch.object(tool, "GOLDEN", gold), \
                    mock.patch.object(tool, "drawn", return_value=json.dumps(text("new row"))):
                same, lines = tool.golden()
        self.assertFalse(same)
        self.assertTrue(any(ln.startswith("-old row") for ln in lines), lines)
        self.assertFalse(any('"type"' in ln for ln in lines), "the text frame, not the JSON tree")

    def test_the_staged_copy_has_a_golden_to_import(self):  # T-1670 review: --update from a missing/broken golden
        tool = load()
        with tempfile.TemporaryDirectory() as tmp:
            mod = tool.stage({"tasks": []}, 80, 40, tmp)
            self.assertTrue(os.path.exists(os.path.join(mod, "tests", "fixtures", "golden.ts")))

    def test_png_goes_through_rsvg_convert_and_other_extensions_are_refused(self):
        tool = load()
        ok = subprocess.CompletedProcess([], 0, b"", b"")
        with mock.patch.object(tool.shutil, "which", return_value="/usr/bin/rsvg-convert"), \
                mock.patch.object(tool.subprocess, "run", return_value=ok) as run:
            tool.write_svg("/nowhere/pane.PNG", "hi\n", 20, 4)
        self.assertEqual(run.call_args[0][0][:2], ["rsvg-convert", "-o"])
        with mock.patch.object(tool.shutil, "which", return_value=None):
            self.assertRaisesRegex(RuntimeError, "rsvg-convert", tool.write_svg, "/nowhere/p.png", "hi\n", 20, 4)
        self.assertRaisesRegex(ValueError, r"\.svg or \.png", tool.write_svg, "/nowhere/p.jpg", "hi\n", 20, 4)
        bad = subprocess.CalledProcessError(1, "rsvg-convert", b"", b"bad frame")
        with mock.patch.object(tool.shutil, "which", return_value="/usr/bin/rsvg-convert"), \
                mock.patch.object(tool.subprocess, "run", side_effect=bad):
            self.assertRaisesRegex(RuntimeError, "bad frame", tool.write_svg, "/nowhere/p.png", "hi\n", 20, 4)

    def test_the_committed_golden_is_the_kit_tests_and_is_checked(self):
        # string checks only: the comparison itself runs under the mod gate, claude plugin test mods/foreman-ui
        tool = load()
        frame = json.loads(tool.read_golden())
        self.assertIn('"key":"fm-pane"', json.dumps(frame, separators=(",", ":")))
        kit = read("tests", "replay.test.tsx")
        self.assertIn("import GOLDEN from './fixtures/golden'", kit)
        self.assertIn("expect(drawn).toBe(GOLDEN)", kit)
        self.assertIn("if (REPLAY.print) return", kit, "a tool's own copy draws another view: no golden there")


def alive(pid):
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"  # a zombie has ended
    except OSError:
        return False


class Fixture(unittest.TestCase):
    def test_the_fixture_is_synthetic_data(self):
        view = load().load(FIXTURE)  # JSON after `export default`: data, no code
        self.assertEqual((view["project"], view["root"]), ("demo-1a2b3c", "/repo"))
        raw = read("tests", "fixtures", "pane-busy.ts")
        for real in (os.path.expanduser("~"), "/home/", "/Users/", "@", "http"):
            self.assertNotIn(real, raw)

    def test_a_module_reads_from_its_line_starting_export_and_one_without_is_refused(self):
        tool = load()
        with tempfile.TemporaryDirectory() as tmp:
            good, bad = os.path.join(tmp, "good.ts"), os.path.join(tmp, "bad.ts")
            with open(good, "w", encoding="utf-8") as f:
                f.write('// see the export default below\nexport default {"v": 1, "title": "x export default y"};\n')
            with open(bad, "w", encoding="utf-8") as f:
                f.write("export const x = 1\n")
            self.assertEqual(tool.load(good), {"v": 1, "title": "x export default y"})
            with self.assertRaises(ValueError):
                tool.load(bad)


if __name__ == "__main__":
    unittest.main()
