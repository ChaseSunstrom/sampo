"""T-3703: sampo readme plans a README's figures from the repo, screenshots the running app (refusing a page that shows a
private string), draws dark and light SVG diagrams from a spec, and checks a README's images; the readme-visuals skill
is named when a prompt or an edit is about a README."""
import json
import os
import subprocess
import sys
import unittest
import urllib.request

from helpers import PLUGIN, SampoTestCase, headless_chrome

from sampo import core as c
from sampo import img as simg
from sampo import readme
from sampo import works

LEAK = "Contact alice@jarvis.home.example.test"  # made-up values only


def write(root, rel, text, mode="w"):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, mode) as f:
        f.write(text)
    return path


def ctx_of(proc):
    out = proc.stdout.strip()
    data = json.loads(out) if out.startswith("{") else {}
    return (data.get("hookSpecificOutput") or {}).get("additionalContext", "")


class Plan(SampoTestCase):
    def setUp(self):
        super().setUp()
        write(self.repo, "app.py", 'from flask import Flask\napp = Flask(__name__)\n\n@app.route("/")\ndef home():\n'
                                   '    return "hi"\n\n@app.get("/settings")\ndef settings():\n    return "s"\n\n'
                                   '@app.get("/api/items")\ndef items():\n    return []\n\n'
                                   '@app.post("/settings/save")\ndef save():\n    return ""\n')
        write(self.repo, "users.py", 'from fastapi import APIRouter\nrouter = APIRouter(prefix="/api")\n\n'
                                     '@router.get("/me")\ndef me():\n    return {}\n')
        write(self.repo, "static/index.html", '<nav><a href="#/watch">Watch</a> <a href="#/music">Music</a></nav>\n')
        write(self.repo, "static/app.js", 'const pages = [["Home", "#/"], ["Tv", `#/tv/${id}`]];\n'
                                          'api.get("/users"); router.get("/team", show);\n')
        write(self.repo, "tool/cli.py", 'import argparse\np = argparse.ArgumentParser()\nsub = p.add_subparsers()\n'
                                        'sub.add_parser("sync")\nsub.add_parser("export")\n')
        subprocess.run(["git", "-C", self.repo, "add", "."], check=True)

    def test_the_plan_finds_pages_api_routes_and_commands_and_proposes_figures(self):
        got = self.fm_json("readme", "plan")
        self.assertEqual(got["pages"][:2], ["/", "/settings"], "page routes, the start page first")
        self.assertIn("#/watch", got["pages"])
        self.assertIn("#/music", got["pages"])
        self.assertIn("/api/items", got["api"])
        self.assertNotIn("/api/items", got["pages"], "an API route is no screen")
        self.assertNotIn("/settings/save", got["pages"], "a POST is no screen")
        self.assertIn("/api/me", got["api"], "a router's prefix")
        self.assertNotIn("/me", got["pages"], "@router.get in Python is never read as a JS route")
        self.assertNotIn("/users", got["pages"], "a client call is no route")
        self.assertIn("/team", got["pages"], "an Express-style route")
        self.assertFalse([p for p in got["pages"] if "${" in p or p == "#/"], "only whole hash literals")
        self.assertEqual(sorted(got["cli"]), ["export", "sync"])
        kinds = [f["kind"] for f in got["figures"]]
        self.assertEqual(kinds[0], "screenshot", "the product first")
        self.assertIn("diagram", kinds)
        for f in got["figures"]:
            self.assertTrue(f["what"] and f["where"], f)
        self.assertIn("sampo readme shots", got["next"][0])
        self.assertIn("/settings", got["next"][0])

    def test_the_plan_goes_into_the_active_brief(self):
        tid = json.loads(self.fm("task", "new", "Readme figures", "--type", "FEATURE", "--tier", "S", "--ac",
                                 "ok :: true", "--step", "do it", "--json").stdout)["id"]
        self.fm("focus", tid)
        self.fm("readme", "plan", "--brief")
        brief = c.find_brief(c.find_project(self.repo), tid)
        self.assertIn("screenshot", brief.section("Figures"))
        self.assertIn("#/watch", brief.section("Figures"))


class Privacy(unittest.TestCase):
    def kinds(self, text):
        return sorted({k for k, _ in readme.private_hits(text)})

    def test_private_strings_are_named_by_kind(self):
        self.assertEqual(self.kinds(LEAK), ["domain", "email"])
        self.assertEqual(self.kinds("saved to /home/alice/notes.txt"), ["home"])
        self.assertEqual(self.kinds("on 10.0.0.7 and 192.168.4.20"), ["ip"])
        self.assertEqual(self.kinds("key " + "sk-ant-" + "a1b2" * 6), ["secret"])
        self.assertEqual(self.kinds("llm.devbox.home.example.test"), ["domain"])

    def test_demo_data_and_public_names_pass(self):
        for text in ("Open http://127.0.0.1:8000/ or localhost:5173", "jane@example.com signed in",
                     "Source on github.com, docs at react.dev", "Version 1.2.3.4 of 10 items", "0.0.0.0:80"):
            self.assertEqual(self.kinds(text), [], text)

    def test_a_shown_value_never_holds_a_secret(self):
        token = "sk-ant-" + "a1b2" * 6
        for kind, shown in readme.private_hits("key " + token + " at /home/alice/x"):
            self.assertNotIn(token[8:], shown, kind)


@unittest.skipUnless(headless_chrome(), "no headless Chromium here")
class Shots(SampoTestCase):
    def setUp(self):
        super().setUp()
        dark = "<style>body{background:#fff;color:#111}@media (prefers-color-scheme: dark){body{background:#111;color:#eee}}</style>"
        write(self.repo, "index.html", f"<!doctype html><title>Demo home</title>{dark}<h1>Demo</h1><p>Ten items.</p>\n")
        write(self.repo, "leak.html", f"<!doctype html><title>Leak</title><p>{LEAK}</p>\n")
        write(self.repo, "plain.html", "<!doctype html><title>Plain</title><p>No dark scheme here.</p>\n")

    def test_shots_start_the_app_shoot_every_view_refuse_a_leak_and_stop_it(self):
        got = self.fm("readme", "shots", "--cmd", "python3 -m http.server 0 --bind 127.0.0.1",
                      "--routes", "/,/leak.html,/plain.html", "--json", check=False, timeout=240)
        self.assertEqual(got.returncode, 1, got.stdout + got.stderr)
        data = json.loads(got.stdout)
        refused = {r["route"]: r for r in data["refused"]}
        self.assertEqual(list(refused), ["/leak.html"])
        self.assertEqual(sorted(refused["/leak.html"]["kinds"]), ["domain", "email"])
        self.assertNotIn("jarvis", got.stdout, "the leak is named by kind, never printed whole")
        screens = os.path.join(self.repo, "docs", "assets", "screens")
        made = sorted(os.listdir(screens))
        self.assertEqual(made, ["home-desktop-dark.png", "home-desktop-light.png", "home-phone-dark.png",
                                "home-phone-light.png", "plain-desktop.png", "plain-phone.png"])
        for name, size in (("home-desktop-dark.png", (1440, 900)), ("plain-phone.png", (390, 844))):
            info = simg.info(os.path.join(screens, name))
            self.assertEqual((info["width"], info["height"]), size, name)
        home = next(s for s in data["snippets"] if "home-desktop" in s)
        self.assertIn('media="(prefers-color-scheme: dark)" srcset="docs/assets/screens/home-desktop-dark.png"', home)
        self.assertIn('src="docs/assets/screens/home-desktop-light.png"', home)
        self.assertIn('alt="Demo home', home)
        with self.assertRaises(OSError):  # stopped: nothing answers there any more
            urllib.request.urlopen(data["url"], timeout=3)
        self.assertEqual(self.fm("readme", "check", check=False).returncode, 0, "no README image yet: nothing to flag")


SPEC = {"title": "How the demo works",
        "groups": [{"id": "web", "label": "Browser", "sub": "the page"}, {"id": "srv", "label": "Server"}],
        "nodes": [{"id": "ui", "label": "index.html", "sub": "hash routes", "group": "web"},
                  {"id": "api", "label": "app.py", "sub": "FastAPI", "group": "srv"},
                  {"id": "jobs", "label": "worker", "group": "srv"},
                  {"id": "db", "label": "SQLite"}],
        "edges": [{"from": "ui", "to": "api", "label": "fetch /api"}, {"from": "api", "to": "jobs"},
                  {"from": "jobs", "to": "db"}, {"from": "db", "to": "api", "label": "rows"}]}


class Diagram(SampoTestCase):
    def draw(self, spec=SPEC, check=True):
        write(self.repo, "arch.json", json.dumps(spec))
        return self.fm("readme", "diagram", "arch.json", check=check)

    def files(self):
        d = os.path.join(self.repo, "docs", "assets", "diagrams")
        out = []
        for t in ("dark", "light"):
            with open(os.path.join(d, f"arch-{t}.svg"), "rb") as f:
                out.append(f.read())
        return out

    def test_a_spec_draws_dark_and_light_that_pass_the_source_gates(self):
        out = self.draw().stdout
        self.assertIn('<picture>', out)
        self.assertIn('srcset="docs/assets/diagrams/arch-dark.svg"', out)
        dark, light = self.files()
        self.assertNotEqual(dark, light)
        for data in (dark, light):
            bad = [g for g in works.inspect(data, "figure")["gates"] if g["ok"] is False]
            self.assertEqual(bad, [])
            text = data.decode()
            for label in ("index.html", "app.py", "worker", "SQLite", "fetch /api", "BROWSER", "01"):
                self.assertIn(f">{label}<", text)
            self.assertIn('role="img"', text)
        self.assertIn(readme.BOARD["dark"]["bg"].lower(), dark.decode().lower(), "no DESIGN.md: the Board look")
        with open(os.path.join(PLUGIN, "web", "live", "board-tokens.json"), encoding="utf-8") as f:
            kit = json.load(f)
        self.assertEqual(readme.BOARD["light"]["accents"][0], kit["light"]["cyan"], "the Board kit's own tokens")
        self.draw()
        self.assertEqual(self.files(), [dark, light], "deterministic: the same spec draws the same bytes")

    def test_the_repos_own_tokens_win(self):
        write(self.repo, "DESIGN.md", "# DESIGN.md: demo\n\n## Tokens\n\n| Token | Kind | Light | Dark | Source |\n"
                                      "|---|---|---|---|---|\n| `--bg` | colour | `#fafaf7` #fafaf7 | `#1c1a24` #1c1a24 | x |\n"
                                      "| `--text` | colour | `#1c1a24` #1c1a24 | `#f2efe9` #f2efe9 | x |\n"
                                      "| `--accent` | colour | `#965800` #965800 | `#ffb547` #ffb547 | x |\n")
        self.draw()
        dark, light = (x.decode().lower() for x in self.files())
        self.assertIn("#1c1a24", dark)
        self.assertIn("#ffb547", dark)
        self.assertIn("#fafaf7", light)
        self.assertNotIn(readme.BOARD["dark"]["bg"].lower(), dark)

    def test_a_bad_spec_is_named(self):
        got = self.draw(dict(SPEC, edges=[{"from": "ui", "to": "nowhere"}]), check=False)
        self.assertNotEqual(got.returncode, 0)
        self.assertIn("nowhere", got.stdout + got.stderr)

    @unittest.skipUnless(headless_chrome(), "no headless Chromium here")
    def test_both_pass_works_check_rendered(self):
        self.draw()
        d = os.path.join(self.repo, "docs", "assets", "diagrams")
        got = self.fm("works", "check", "--type", "figure", os.path.join(d, "arch-dark.svg"),
                      os.path.join(d, "arch-light.svg"), check=False, timeout=300)
        self.assertEqual(got.returncode, 0, got.stdout[-3000:] + got.stderr[-2000:])


class Check(SampoTestCase):
    def test_check_flags_each_problem_by_file(self):
        write(self.repo, "img/plain.png", b"\x89PNG small", "wb")
        write(self.repo, "img/big.png", b"\x89PNG" + b"\0" * 500_000, "wb")
        write(self.repo, "img/arch-dark.svg", '<svg xmlns="http://www.w3.org/2000/svg"><text>ok</text></svg>')
        write(self.repo, "img/leak-dark.svg", '<svg xmlns="http://www.w3.org/2000/svg"><text>/home/alice/x</text></svg>')
        write(self.repo, "img/leak-light.svg", '<svg xmlns="http://www.w3.org/2000/svg"><text>fine</text></svg>')
        write(self.repo, "README.md", "# app\n\n![](img/plain.png)\n\n![Gone](img/gone.png)\n\n![Big](img/big.png)\n\n"
                                      "![Arch](img/arch-dark.svg)\n\n<picture>\n  <source media=\"(prefers-color-scheme: "
                                      "dark)\" srcset=\"img/leak-dark.svg\">\n  <img alt=\"Leak\" src=\"img/leak-light.svg\">"
                                      "\n</picture>\n\n<picture><source srcset=\"img/plain.png\"></picture>\n")
        got = self.fm("readme", "check", "--json", check=False)
        self.assertEqual(got.returncode, 1, got.stdout + got.stderr)
        found = {(f["file"], f["problem"]) for f in json.loads(got.stdout)["findings"]}
        self.assertIn(("img/plain.png", "no alt text"), found)
        self.assertIn(("img/gone.png", "missing"), found)
        self.assertIn(("img/big.png", "over 400 KB"), found)
        self.assertIn(("img/arch-dark.svg", "no light pair"), found)
        self.assertIn(("img/leak-dark.svg", "private text: home"), found)
        self.assertIn(("img/plain.png", "a picture with no img fallback"), found)
        self.assertEqual(len(found), 6, found)

    def test_a_clean_readme_passes(self):
        svg = '<svg xmlns="http://www.w3.org/2000/svg"><text>Sampo</text></svg>'
        write(self.repo, "img/a-dark.svg", svg)
        write(self.repo, "img/a-light.svg", svg)
        write(self.repo, "README.md", "# app\n\n<picture>\n  <source media=\"(prefers-color-scheme: dark)\" "
                                      "srcset=\"img/a-dark.svg\">\n  <img alt=\"How it works\" src=\"img/a-light.svg\">\n"
                                      "</picture>\n\n![Badge](https://img.shields.io/badge/x-y-blue)\n\n"
                                      "![Logo](img/a-dark.svg#gh-dark-mode-only)![Logo](img/a-light.svg#gh-light-mode-only)\n")
        got = self.fm("readme", "check", check=False)
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        self.assertIn("2 images", got.stdout)


class Routing(SampoTestCase):
    def test_readme_words_route_to_the_skill(self):
        self.assertTrue(os.path.isfile(os.path.join(PLUGIN, "skills", "readme-visuals", "SKILL.md")))
        self.assertIn("sampo:readme-visuals", c.SKILL_FOR["readme"])
        for prompt in ("add screenshots to the README", "rework readme.md", "the docs page needs an architecture diagram"):
            self.assertEqual(c.ui_request(prompt), "readme", prompt)
        for prompt in ("redesign the landing page", "fix the login", "read me the error"):
            self.assertNotEqual(c.ui_request(prompt), "readme", prompt)

    def test_a_prompt_and_a_first_readme_edit_name_the_skill(self):
        self.fm("init")
        ups = self.hook("UserPromptSubmit", {"prompt": "rework the README for this tool", "session_id": "s-r"})
        self.assertIn("README request: load: Skill sampo:readme-visuals", ctx_of(ups))
        tid = json.loads(self.fm("task", "new", "Docs", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true",
                                 "--step", "do it", "--json").stdout)["id"]
        self.fm("focus", tid)

        def edit(rel):
            proc = self.hook("PreToolUse", {"tool_name": "Edit", "tool_input": {
                "file_path": os.path.join(self.repo, rel), "old_string": "x", "new_string": "y"}})
            return ctx_of(proc)
        self.assertNotIn("readme-visuals", edit("app.py"))
        self.assertIn("readme-visuals", edit("README.md"), "an imageless README")
        self.assertNotIn("readme-visuals", edit("README.md"), "once a task")
        write(self.repo, "docs/README.md", "# docs\n\n![Shot](a.png)\n")
        tid2 = json.loads(self.fm("task", "new", "Docs 2", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true",
                                  "--step", "do it", "--json").stdout)["id"]
        self.fm("focus", tid2)
        self.assertNotIn("readme-visuals", edit("docs/README.md"), "it has images already")


if __name__ == "__main__":
    unittest.main()
