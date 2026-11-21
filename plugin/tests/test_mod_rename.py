"""T-5059 (T-3854's slice): the UI mod's folder is mods/sampo-ui; mods/foreman-ui stays a relative link to it, so an
install whose marketplace or installed_plugins.json names the old folder keeps loading. Every reader names the new
folder first and takes the old one, and the rename scan's baseline does not rise."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import helpers  # noqa: F401  (plugin/lib on sys.path)

from sampo import doctor, settings

import test_rename_scan as scan

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MANIFEST = os.path.join(".claude-plugin", "plugin.json")


def read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as f:
        return f.read()


def put(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class Layout(unittest.TestCase):
    def test_sampo_ui_is_the_real_folder_and_foreman_ui_a_relative_link(self):
        new, old = os.path.join(ROOT, "mods", "sampo-ui"), os.path.join(ROOT, "mods", "foreman-ui")
        self.assertTrue(os.path.isdir(new) and not os.path.islink(new))
        self.assertTrue(os.path.islink(old))
        self.assertEqual(os.readlink(old), "sampo-ui", "relative, so a copy of the checkout keeps it pointing inside itself")
        self.assertEqual(os.path.realpath(old), os.path.realpath(new))
        self.assertEqual(read(new, MANIFEST), read(old, MANIFEST), "one manifest, read through either path")

    def test_the_marketplace_names_the_new_folder(self):
        mk = json.loads(read(ROOT, ".claude-plugin", "marketplace.json"))
        src = {e["name"]: e["source"] for e in mk["plugins"]}
        self.assertEqual(src["sampo-ui"], "./mods/sampo-ui")

    def test_the_rename_scan_reads_the_new_folder(self):
        self.assertIn("mods/sampo-ui/**", scan.SURFACES)
        self.assertNotIn("mods/foreman-ui/**", scan.SURFACES, "the link is no surface: its content is the new folder's")
        self.assertIn("mods/sampo-ui/tests/foreman-ui.test.tsx", scan.OLD_NS_ALLOWED)

    def test_the_baseline_does_not_rise(self):
        rows = scan.leftover() + scan.library_strings()
        self.assertLessEqual(len(rows), scan.BASELINE, f"{len(rows)} lines still say Foreman: {rows[:3]}")
        self.assertLessEqual(scan.BASELINE, 16, "T-5059 starts from 16: lower it, never raise it")

    def test_install_sh_names_the_mod_by_its_new_name_and_still_reads_both(self):
        text = read(ROOT, "install.sh")
        self.assertIn("don't install the sampo-ui mod", text)
        self.assertIn("had 'foreman|sampo|foreman-ui|sampo-ui'", text, "an install that has either id still moves")

    def test_git_tracks_the_link_as_a_link(self):
        out = subprocess.run(["git", "-C", ROOT, "ls-files", "-s", "mods/foreman-ui"], capture_output=True, text=True).stdout
        self.assertTrue(out.startswith("120000 "), out)  # a symlink entry, not a copy that could drift


class SettingsReadBoth(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.t, ignore_errors=True)

    def test_the_manifest_path_is_the_new_folder(self):
        self.assertEqual(settings._MOD_JSON, os.path.join(ROOT, "mods", "sampo-ui", MANIFEST))
        self.assertEqual(settings.mod_manifest()["name"], "sampo-ui")

    def test_mod_dir_prefers_sampo_ui_and_accepts_foreman_ui(self):
        put(self.t, "mods/foreman-ui/" + MANIFEST, "{}")
        self.assertEqual(settings.mod_dir(self.t), os.path.join(self.t, "mods", "foreman-ui"), "a checkout from before")
        put(self.t, "mods/sampo-ui/" + MANIFEST, "{}")
        self.assertEqual(settings.mod_dir(self.t), os.path.join(self.t, "mods", "sampo-ui"))
        self.assertEqual(settings.mod_dir(os.path.join(self.t, "none")), os.path.join(self.t, "none", "mods", "sampo-ui"),
                         "neither: the new name, so the error names it")

    def test_an_old_checkout_still_gives_its_userconfig(self):
        put(self.t, "mods/foreman-ui/" + MANIFEST, json.dumps({"name": "foreman-ui", "userConfig": {"mascot": {"type": "string"}}}))
        with mock.patch.object(settings, "_MOD_JSON", os.path.join(settings.mod_dir(self.t), MANIFEST)), \
                mock.patch.object(settings, "claude_dir", return_value=os.path.join(self.t, "nowhere")):
            self.assertIn("mascot", settings.mod_manifest()["userConfig"])


class DoctorReadsBoth(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.t, ignore_errors=True)

    def check(self, folder):
        repo = os.path.join(self.t, "home")
        put(repo, f"mods/{folder}/{MANIFEST}", json.dumps({"name": "sampo-ui", "version": "0.5.0"}))
        put(repo, f"mods/{folder}/hooks/register.tsx", "new panels\n")
        put(self.t, "cache/0.4.0/hooks/register.tsx", "old rows\n")
        listing = os.path.join(self.t, "installed_plugins.json")
        put(self.t, "installed_plugins.json", json.dumps(
            {"plugins": {"sampo-ui@sampo": [{"installPath": os.path.join(self.t, "cache", "0.4.0"), "version": "0.4.0"}]}}))
        return doctor.check_mod_release(repo, listing)

    def test_a_lagging_install_is_told_from_either_folder(self):
        for folder in ("sampo-ui", "foreman-ui"):
            r = self.check(folder)
            self.assertEqual(r.status, "WARN", (folder, r.detail))
            self.assertIn("0.5.0", r.detail)

    def test_the_recordings_name_the_new_folder(self):
        code = {c for _, c, _ in doctor.RECORDINGS if c}
        self.assertIn("mods/sampo-ui", code)
        self.assertNotIn("mods/foreman-ui", code)


if __name__ == "__main__":
    unittest.main()
