"""T-4841: a clean install puts both `fm` and `sampo` on PATH (~/.local/bin), as the README says (T-3300 finding 2)."""
import os
import shutil
import tempfile
import unittest

from helpers import PLUGIN

from sampo import setup

ROOT = os.path.dirname(PLUGIN)


def _bin(d, name):
    """A plugin/bin folder under a Foreman-named checkout with both commands in it."""
    folder = os.path.join(d, name, "plugin", "bin")
    os.makedirs(folder)
    for cmd in ("fm", "sampo"):
        with open(os.path.join(folder, cmd), "w") as f:
            f.write("#!/bin/sh\n")
    return folder


class Links(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        self.bin = _bin(self.d, "foreman")
        self.fm = os.path.join(self.d, "home", ".local", "bin", "fm")
        self.sampo = os.path.join(os.path.dirname(self.fm), "sampo")

    def test_path_gets_both_commands_on_a_clean_install(self):
        made = setup.ensure_fm_link(self.fm, os.path.join(self.bin, "fm"))
        self.assertEqual(made, self.fm, "the fm link is still what the caller gets back")
        self.assertEqual(os.readlink(self.fm), os.path.join(self.bin, "fm"))
        self.assertEqual(os.readlink(self.sampo), os.path.join(self.bin, "sampo"), "sampo is on PATH too")
        self.assertIsNone(setup.ensure_fm_link(self.fm, os.path.join(self.bin, "fm")), "a second run changes nothing")

    def test_path_sampo_alone_is_made_when_fm_was_there_already(self):
        os.makedirs(os.path.dirname(self.fm))
        os.symlink(os.path.join(self.bin, "fm"), self.fm)  # an install from before sampo was linked
        self.assertEqual(setup.ensure_fm_link(self.fm, os.path.join(self.bin, "fm")), self.sampo)
        self.assertTrue(os.path.islink(self.sampo))

    def test_path_leaves_anyone_elses_sampo_alone(self):
        os.makedirs(os.path.dirname(self.fm))
        with open(self.sampo, "w") as f:  # a file: another tool called sampo
            f.write("#!/bin/sh\n")
        setup.ensure_fm_link(self.fm, os.path.join(self.bin, "fm"))
        self.assertFalse(os.path.islink(self.sampo), "a file stays")
        os.remove(self.sampo)
        os.symlink("/nonexistent/other-tool/bin/sampo", self.sampo)
        setup.ensure_fm_link(self.fm, os.path.join(self.bin, "fm"))
        self.assertEqual(os.readlink(self.sampo), "/nonexistent/other-tool/bin/sampo", "someone else's broken link stays")
        os.remove(self.sampo)
        other = _bin(self.d, "other")
        os.symlink(os.path.join(other, "sampo"), self.sampo)
        setup.ensure_fm_link(self.fm, os.path.join(self.bin, "fm"))
        self.assertEqual(os.readlink(self.sampo), os.path.join(other, "sampo"), "a working link elsewhere stays")

    def test_path_sampo_link_of_a_removed_version_is_repointed(self):
        os.makedirs(os.path.dirname(self.fm))
        os.symlink(os.path.join(self.d, "cache", "foreman", "0.9.1", "plugin", "bin", "sampo"), self.sampo)
        setup.ensure_fm_link(self.fm, os.path.join(self.bin, "fm"))
        self.assertEqual(os.readlink(self.sampo), os.path.join(self.bin, "sampo"))

    def test_path_only_a_whole_path_component_makes_a_dangling_link_ours(self):  # T-4841 review
        """A user or folder merely containing "sampo" is not a Sampo checkout: `/home/sampo/x/plugin/bin/fm` is
        someone else's dangling link and stays. Ours: a folder named foreman or sampo, with at most one
        version folder (the plugin cache) under it, then plugin/bin/fm or sampo."""
        os.makedirs(os.path.dirname(self.fm))
        for theirs in ("/nonexistent/home/sampo/x/plugin/bin/fm", "/nonexistent/u/sampo-other/plugin/bin/fm",
                       "/nonexistent/u/mysampo/plugin/bin/fm", "/nonexistent/u/foreman-dev/plugin/bin/fm",
                       "/nonexistent/u/foreman/plugin/bin/fm.old"):
            os.symlink(theirs, self.fm)
            self.assertIsNone(setup._link_one(self.fm, os.path.join(self.bin, "fm")), theirs)
            self.assertEqual(os.readlink(self.fm), theirs, f"{theirs} stays")
            os.remove(self.fm)
        for ours in ("/nonexistent/u/foreman/plugin/bin/fm", "/nonexistent/u/sampo/plugin/bin/fm",
                     "/nonexistent/cache/foreman/0.9.1/plugin/bin/fm", "foreman/plugin/bin/fm"):
            os.symlink(ours, self.fm)
            self.assertEqual(setup._link_one(self.fm, os.path.join(self.bin, "fm")), self.fm, ours)
            self.assertEqual(os.readlink(self.fm), os.path.join(self.bin, "fm"), f"{ours} is repointed")
            os.remove(self.fm)

    def test_path_no_sampo_beside_fm_means_only_fm(self):
        os.remove(os.path.join(self.bin, "sampo"))
        self.assertEqual(setup.ensure_fm_link(self.fm, os.path.join(self.bin, "fm")), self.fm)
        self.assertFalse(os.path.lexists(self.sampo), "nothing to link to")


class InstallScript(unittest.TestCase):
    def test_path_both_install_branches_run_the_link(self):
        with open(os.path.join(ROOT, "install.sh"), encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(text.count("setup.ensure_fm_link()"), 1, "one call, after the wiring branches, for both")
        wiring = text.index('if [ "$WIRING" = 1 ] && [ "$CLAUDE" = 1 ]')
        link = text.index("setup.ensure_fm_link()")
        self.assertGreater(link, wiring)
        self.assertIn("so any shell finds sampo", text)


if __name__ == "__main__":
    unittest.main()
