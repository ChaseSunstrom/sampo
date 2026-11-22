"""fm next names the installed third-party skills that fit a task's stage (T-0205)."""
import json
import os

from helpers import ForemanTestCase

from sampo import core as c


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text if isinstance(text, str) else json.dumps(text))


def skill(root, name, desc):
    write(os.path.join(root, "skills", name, "SKILL.md"), f"---\nname: {name}\ndescription: {desc}\n---\nbody\n")


class SkillRoute(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.cc = os.path.join(self.tmp, "cc")
        cache = os.path.join(self.cc, "plugins", "cache", "m")
        paths = {n: os.path.join(cache, n, "1.0") for n in ("pony", "front", "sec")}
        skill(paths["pony"], "pony-audit", "Find over-engineering and bloat to simplify or delete.")
        skill(paths["front"], "front-design", "Distinctive frontend design for web interfaces.")
        skill(paths["sec"], "sec-scan", "Security vulnerability scan.")
        write(os.path.join(self.cc, "plugins", "installed_plugins.json"), {"version": 2, "plugins": {
            f"{n}@m": [{"scope": "user", "installPath": p}] for n, p in paths.items()}})
        write(os.path.join(self.cc, "settings.json"),
              {"enabledPlugins": {"pony@m": True, "front@m": True, "sec@m": False}})
        skill(self.cc, "graphy", "Turn anything into a knowledge graph.")
        self.env = {"CLAUDE_CONFIG_DIR": self.cc}
        self.fm("init")

    def start(self, type_, scope=None):
        tid = json.loads(self.fm("task", "new", f"A {type_} task", "--type", type_, "--tier", "S", "--ac", "ok :: true",
                                 "--step", "do it", "--json").stdout)["id"]
        if scope:
            self.fm("task", "set", tid, f"scope={scope}")
        self.fm("focus", tid)
        return self.fm("next", env=self.env).stdout

    def test_a_matching_enabled_skill_is_named(self):
        out = self.start("CLEAN")
        self.assertIn("/pony:pony-audit", out)
        self.assertIn("/simplify", out, "Claude Code's own skill for the stage")
        self.assertNotIn("front-design", out)

    def test_disabled_plugins_and_unrelated_stages_name_nothing(self):
        out = self.start("FIX")
        self.assertNotIn("skills that fit", out)
        self.assertNotIn("sec-scan", self.start("SECURITY"), "sec@m is installed but disabled")

    def test_ui_work_gets_the_design_skill(self):
        self.assertIn("/front:front-design", self.start("FEATURE", "src/App.tsx"))

    def test_skills_used_before_rank_first(self):
        skill(os.path.join(self.cc, "plugins", "cache", "m", "pony", "1.0"), "pony-debt", "Harvest simplify debt.")
        events = os.path.join(c.state_dir(), "events.jsonl")
        slug = json.loads(self.fm("state", "--json").stdout)["project"]
        with open(events, "a") as f:
            for _ in range(2):
                f.write(json.dumps({"kind": "tool", "tool": "Skill", "target": "pony:pony-debt", "project": slug,
                                    "ts": c.now()}) + "\n")
        out = self.start("CLEAN")
        self.assertLess(out.index("/pony:pony-debt"), out.index("/pony:pony-audit"))


class PanelRouting(ForemanTestCase):
    """T-3733: routing.json's panel_words send a diagram or dashboard to the board-panels skill, once a session."""

    def test_routing_json_has_the_panel_words_and_the_hint_names_a_skill_that_exists(self):
        with open(os.path.join(c.PLUGIN_ROOT, "skills", "routing.json"), encoding="utf-8") as f:
            routing = json.load(f)
        self.assertTrue({"diagram", "dashboard", "panels"} <= set(routing["panel_words"]))
        self.assertIsNone(c.routing_problem(routing))
        self.assertIn("Skill sampo:board-panels", c.SKILL_FOR["panels"])
        self.assertTrue(os.path.isfile(os.path.join(c.PLUGIN_ROOT, "skills", "board-panels", "SKILL.md")))
        bad = dict(routing, panel_words="dashboard")
        self.assertIn("panel_words", c.routing_problem(bad), "a word list, not a string")

    def test_diagram_and_dashboard_words_route_to_panels(self):
        for prompt in ("draw a diagram of how the services connect", "build a dashboard of the queue depth",
                       "Add an architecture overview to the docs", "make panels of the pipeline stages",
                       "sketch the architecture map of this repo", "DASHBOARD for the jobs",
                       "draw the diagrams of the repo", "build dashboards for each team", "add a panel for the queue",
                       "an architecture diagram of the repo", "sketch a flow diagram of the pipeline"):
            self.assertEqual(c.ui_request(prompt), "panels", prompt)

    def test_a_change_to_an_existing_dashboard_goes_to_ui_not_panels(self):
        for prompt in ("animate the dashboard", "dark mode the dashboard", "fix the dashboard bug", "rename the dashboards table"):
            self.assertNotEqual(c.ui_request(prompt), "panels", prompt)
        self.assertEqual(c.ui_request("animate the dashboard"), "ui")
        self.assertEqual(c.ui_request("dark mode the dashboard"), "ui")

    def test_bare_architecture_or_board_words_do_not_route(self):
        for prompt in ("explain the architecture of the cache", "the board meeting notes"):
            self.assertNotEqual(c.ui_request(prompt), "panels", prompt)

    def test_other_requests_and_the_older_routes_are_untouched(self):
        for prompt in ("fix the login bug", "a diagrammatic style", "undashboard it",
                       "the panelist list", "", None):
            self.assertNotEqual(c.ui_request(prompt), "panels", prompt)
        self.assertEqual(c.ui_request("add an architecture diagram to the README"), "readme", "a README wins")
        self.assertEqual(c.ui_request("redesign the dashboard page"), "design", "a new look wins")
        self.assertEqual(c.ui_request("animate the frontend"), "ui")
