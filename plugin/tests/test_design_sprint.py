"""T-1538: fm design sprint walks five stages (brief, direction, tokens, build, critique), each with a check, and names
the next step of the first that fails; fm design vague is the veto list; a UI task's receipt shows its swatch strip."""
import json
import os
import subprocess
import unittest

from helpers import ForemanTestCase

from sampo import design as sdesign

POV = "## Point of view\n\nA ledger for a bakery's three shop managers, read at 6am before the ovens are lit.\n\n"
DIRECTION = "## Direction\n\nlook-lido, audacity bold.\n\n"
TOKENS = "## Tokens\n\n| Role | Value |\n|---|---|\n| bg | #faf7f2 |\n| text | #1d1b18 |\n\n"


class Sprint(ForemanTestCase):
    def walk(self, design=None):
        root = os.path.join(self.tmp, "site")
        os.makedirs(root, exist_ok=True)
        if design is not None:
            with open(os.path.join(root, "DESIGN.md"), "w") as f:
                f.write(design)
        p = self.fm("design", "sprint", root, "--json", check=False)
        return p.returncode, json.loads(p.stdout)["stages"]

    def test_each_stage_stops_the_walk_until_its_check_passes(self):
        code, rows = self.walk()
        self.assertEqual((code, [(r["stage"], r["pass"], r["why"]) for r in rows]), (1, [("brief", False, "no DESIGN.md")]))
        self.assertIn("fm design vague", rows[0]["next"])
        _, rows = self.walk("# Design\n\n## Point of view\n\nMake it modern and clean.\n")
        self.assertEqual(rows[-1]["why"], "vague: modern, clean")
        _, rows = self.walk("# Design\n\n" + POV)
        self.assertEqual([(r["stage"], r["pass"]) for r in rows], [("brief", True), ("direction", False)])
        self.assertIn("/sampo:directions", rows[-1]["next"])
        _, rows = self.walk("# Design\n\n" + POV + DIRECTION)
        self.assertEqual((rows[-1]["stage"], rows[-1]["why"]), ("tokens", "no ## Tokens table"))
        _, rows = self.walk("# Design\n\n" + POV + DIRECTION + TOKENS)
        self.assertEqual((rows[2]["why"], rows[3]["stage"], rows[3]["why"]),
                         ("2 token row(s)", "build", "no UI files under the root"))

    def test_the_text_marks_stages_not_reached(self):
        root = os.path.join(self.tmp, "site")
        os.makedirs(root)
        text = self.fm("design", "sprint", root, check=False).stdout
        self.assertIn("✗ 1 brief", text)
        self.assertIn("· 5 critique", text)
        self.assertIn("next, two minutes:", text)


class Vague(ForemanTestCase):
    def test_the_veto_list_names_each_vague_word_once(self):
        got = json.loads(self.fm("design", "vague", "make", "it", "pop,", "modern", "and", "Modern", "--json").stdout)
        self.assertEqual([w["word"] for w in got["vague"]], ["modern", "pop"])
        self.assertTrue(all(w["ask"] for w in got["vague"]))
        self.assertEqual(sdesign.vague("a ledger read at 6am, three columns, ink on cream"), [])
        self.assertEqual(sdesign.vague("pop-up menus and a populated list"), [], "only whole words count")


class Receipt(ForemanTestCase):
    def test_a_ui_task_receipt_shows_the_colours_and_motion_it_added(self):
        self.fm("init")
        tid = json.loads(self.fm("task", "new", "Warm the buttons", "--type", "FEATURE", "--tier", "S", "--ac",
                                 "done :: true", "--step", "style them", "--json").stdout)["id"]
        self.fm("focus", tid)
        css = os.path.join(self.repo, "app.css")
        with open(css, "w") as f:
            f.write(".btn { color: #C99C74; background: #fff; transition: opacity 150ms ease-out, transform .2s; }\n")
        subprocess.run(["git", "-C", self.repo, "add", "app.css"], check=True)
        got = json.loads(self.fm("task", "receipt", tid, "--json").stdout)
        self.assertEqual(got["swatches"], {"colours": ["#c99c74", "#fff"], "motion": ["150ms"]})
        self.assertIn("Swatches: #c99c74 #fff · motion: 150ms", self.fm("task", "receipt", tid).stdout)


if __name__ == "__main__":
    unittest.main()
