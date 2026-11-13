"""T-4519: a new project starts at the autonomy the user's machine profile holds (sampo autonomy full --standing writes a
structured key in the profile), not at standard; a project's own explicit setting always wins. Made-up values only."""
import json
import os

from helpers import SampoTestCase, git_repo

from sampo import core as c
from sampo import profile


class _Base(SampoTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)

    def fresh(self, name):
        """A new project: its own git repo, fm init'd there, with no autonomy of its own in its meta."""
        root = git_repo(self.tmp, name=name)
        self.fm("init", cwd=root)
        p = c.find_project(root)
        self.assertNotIn("autonomy", c.read_meta(p), "a fresh project has no autonomy of its own")
        return p

    def settings(self, text):
        os.makedirs(profile.folder(), exist_ok=True)
        with open(os.path.join(profile.folder(), "settings.json"), "w", encoding="utf-8") as f:
            f.write(text)


class StandingAutonomy(_Base):
    def test_a_fresh_project_on_a_full_profile_reads_full_and_drive_on_from_its_first_turn(self):
        self.fm("autonomy", "full", "--standing")
        p = self.fresh("fresh-full")
        self.assertEqual(c.effective_autonomy(p), "full")
        self.assertTrue(c.read_meta(p)["drive"], "drive is on in a new project")
        self.assertEqual(self.fm_json("autonomy", cwd=p.root)["effective"], "full")
        self.assertEqual(self.fm_json("autonomy", cwd=p.root)["autonomy"], "full")

    def test_a_fresh_project_with_no_such_setting_reads_standard_as_before(self):
        p = self.fresh("fresh-plain")
        self.assertEqual(c.effective_autonomy(p), "standard")
        self.assertEqual(self.fm_json("autonomy", cwd=p.root)["autonomy"], "standard")
        self.settings('{"autonomy": "standard"}\n')
        self.assertEqual(c.effective_autonomy(p), "standard")

    def test_a_projects_own_explicit_setting_wins_over_the_profile(self):
        self.fm("autonomy", "full", "--standing")
        p = self.fresh("explicit-standard")
        self.fm("autonomy", "standard", cwd=p.root)
        self.assertEqual(c.effective_autonomy(p), "standard", "its own standard beats the profile's full")
        self.settings('{"autonomy": "standard"}\n')
        q = self.fresh("explicit-full")
        self.fm("autonomy", "full", cwd=q.root)
        self.fm("autonomy", "standard", "--standing")
        self.assertEqual(c.effective_autonomy(q), "full", "its own full beats a profile's standard")

    def test_the_standing_flag_writes_the_profile_key_and_this_project(self):
        self.fm("autonomy", "full", "--standing")
        self.assertEqual(profile.standing_autonomy(), "full")
        self.assertEqual(c.read_meta(self.p)["autonomy"], "full")
        self.fm("autonomy", "standard", "--standing")
        self.assertEqual(profile.standing_autonomy(), "standard")
        self.assertEqual(c.read_meta(self.p)["autonomy"], "standard")
        self.assertNotEqual(self.fm("autonomy", "--standing", check=False).returncode, 0, "a level is needed")

    def test_the_profile_value_is_a_key_never_its_free_text(self):
        os.makedirs(profile.folder(), exist_ok=True)
        with open(os.path.join(profile.folder(), "seed.md"), "w", encoding="utf-8") as f:
            f.write("- Full autonomy, standing: Never ask; decide and report.\n")
        p = self.fresh("text-only")
        self.assertIsNone(profile.standing_autonomy())
        self.assertEqual(c.effective_autonomy(p), "standard", "the words 'Full autonomy' don't set it")
        self.settings('{"autonomy": "standard"}\n')
        self.assertEqual(c.effective_autonomy(p), "standard")

    def test_an_unknown_or_unreadable_value_reads_as_no_setting(self):
        p = self.fresh("bad-value")
        for text in ('{"autonomy": "slow"}\n', '{"autonomy": "FULL"}\n', '["full"]\n', "not json {\n", '{"autonomy": null}\n'):
            self.settings(text)
            self.assertIsNone(profile.standing_autonomy(), text)
            self.assertEqual(c.effective_autonomy(p), "standard", text)

    def test_profile_show_prints_the_standing_autonomy(self):
        self.assertIsNone(self.fm_json("profile")["autonomy"])
        self.fm("autonomy", "full", "--standing")
        self.assertEqual(self.fm_json("profile")["autonomy"], "full")
        self.assertIn("full", self.fm("profile").stdout)
        self.assertIn("standing autonomy", self.fm("profile").stdout)

    def test_the_standing_key_is_a_plain_json_value_and_adds_no_profile_problem(self):
        self.fm("autonomy", "full", "--standing")
        with open(os.path.join(profile.folder(), "settings.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"autonomy": "full"})
        self.assertEqual(profile.check(), [], "the key adds no problem to the profile check")
