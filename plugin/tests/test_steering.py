"""Steering as data (T-0264): the user's "no" becomes a check before matching actions (T-0251), and the routing tables
live in plugin/skills/routing.json where fm evolve can tune them (T-0252)."""
import json
import os

from helpers import ForemanTestCase


def _ctx(proc):
    try:
        return (json.loads(proc.stdout or "{}").get("hookSpecificOutput") or {}).get("additionalContext", "")
    except ValueError:
        return ""


class Veto(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.fm("task", "new", "Ship it", "--type", "FEATURE", "--tier", "S", "--ac", "x :: true", "--step", "x",
                "--focus")

    def bash(self, cmd):
        return _ctx(self.hook("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": cmd}}))

    def test_a_no_becomes_a_note_on_the_matching_command(self):
        self.hook("UserPromptSubmit", {"prompt": "never push to main without asking me first"})
        note = self.bash("git push origin main")
        self.assertIn("never push to main without asking me first", note)
        self.assertNotIn("never push", self.bash("git status"))

    def test_an_edit_matches_on_its_path(self):
        self.hook("UserPromptSubmit", {"prompt": "don't touch the lockfile"})
        p = self.hook("PreToolUse", {"tool_name": "Write", "tool_input": {
            "file_path": os.path.join(self.repo, "lockfile"), "content": "x"}})
        self.assertIn("don't touch the lockfile", _ctx(p))

    def test_curly_quotes_and_the_clause_that_is_the_veto(self):
        self.hook("UserPromptSubmit", {"prompt": "No, don’t touch the migrations"})
        p = self.hook("PreToolUse", {"tool_name": "Edit", "tool_input": {
            "file_path": os.path.join(self.repo, "migrations", "0001.sql"), "old_string": "a", "new_string": "b"}})
        self.assertIn("touch the migrations", _ctx(p))
        self.hook("UserPromptSubmit", {"prompt": "No, I don't think so, never deploy on fridays"})
        self.assertIn("never deploy on fridays", self.bash("make deploy ENV=prod DAY=fridays"))
        self.assertNotIn("think so", self.bash("echo think"))

    def test_listed_deduped_and_removable(self):
        for _ in range(2):
            self.hook("UserPromptSubmit", {"prompt": "never push to main without asking"})
        listed = self.fm("vetoes").stdout
        self.assertEqual(listed.count("never push to main without asking"), 1)
        self.assertIn("never push", self.bash("git push origin main"))
        self.fm("vetoes", "rm", "1")
        self.assertNotIn("never push", self.bash("git push origin main"))
        self.assertNotEqual(self.fm("vetoes", "rm", "5", check=False).returncode, 0)

    def test_plain_requests_record_no_veto(self):
        self.hook("UserPromptSubmit", {"prompt": "please push the branch when it is green"})
        self.assertNotIn("push the branch", self.bash("git push origin main"))


class Routing(ForemanTestCase):
    def test_the_readers_take_the_tables_from_routing_json(self):
        from sampo import cli
        from sampo import core as c
        from sampo import plugins
        with open(os.path.join(c.PLUGIN_ROOT, "skills", "routing.json")) as f:
            r = json.load(f)
        self.assertEqual({k: tuple(v) for k, v in r["stage_words"].items()}, plugins.STAGE_WORDS)
        self.assertEqual(tuple(r["ui_words"]), plugins.UI_WORDS)
        self.assertEqual({k: tuple(v) for k, v in r["builtin_skills"].items()}, plugins.BUILTIN)
        self.assertEqual(tuple(tuple(g) for g in r["review_groups"]), cli.SPLIT_GROUPS)

    def test_a_wrong_shaped_table_is_refused(self):
        from sampo import core as c
        good = {"stage_words": {"FIX": ["debug"]}, "ui_words": ["ui"], "builtin_skills": {"CLEAN": ["simplify"]},
                "review_groups": [["edge"]]}
        self.assertIsNone(c.routing_problem(good))
        for bad in (dict(good, review_groups=5), dict(good, stage_words={"FIX": "debug"}),
                    dict(good, builtin_skills={"CLEAN": ["run this; rm -rf"]}), dict(good, review_groups=[[]]), [1]):
            self.assertIsNotNone(c.routing_problem(bad), bad)

    def test_evolve_takes_it_as_a_target_and_refuses_a_broken_revision(self):
        from sampo import evolve
        self.assertTrue(evolve.TEXT.match("skills/routing.json"))
        old = json.dumps({"stage_words": {}, "review_groups": []})
        evolve.check_revision("plugin/skills/routing.json", old, json.dumps({"stage_words": {"FIX": ["x"]},
                                                                              "review_groups": [["edge"]]}))
        for bad in ("{not json", json.dumps({"stage_words": {}}), json.dumps([1, 2]),
                    json.dumps({"stage_words": {}, "review_groups": 5})):
            with self.assertRaises(ValueError):
                evolve.check_revision("plugin/skills/routing.json", old, bad)
        with self.assertRaises(ValueError):  # the old frontmatter rule still holds for instruction files
            evolve.check_revision("plugin/skills/x/SKILL.md", "---\nname: x\n---\nbody", "body only")
