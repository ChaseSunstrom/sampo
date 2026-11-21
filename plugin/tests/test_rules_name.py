"""T-5048 (slice of T-4334): the rules file is plugin/rules/sampo.md and plugin/rules/foreman.md is a symlink to it. One
constant names it (core.RULES_FILE) and every reader takes the name from there; the guard (stdlib only, so its own pair)
protects both names as core; doctor and setup resolve the user's link through the old name to the new file.
reruns: reads the repo's files and builds fake homes in temp folders; nothing kept between runs."""
import os
import re
import shutil
import tempfile
import unittest
from unittest import mock

import helpers  # noqa: F401  (plugin/lib on sys.path)
from helpers import ForemanTestCase

import fmguard
from sampo import core as c
from sampo import doctor, setup

PLUGIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(PLUGIN)
LIB = os.path.join(PLUGIN, "lib")
# files that may still say the old name as a string: the constant itself, the guard's own pair, and the user's link name
# (setup, doctor and hooks name ~/.claude/rules/foreman.md through c.RULES_OLD, not as a literal)
OLD_LITERAL_OK = {"plugin/lib/sampo/core.py", "plugin/lib/fmguard.py"}


def fake_plugin(root):
    """A plugin folder shaped like the repo's: rules/sampo.md and rules/foreman.md as a link to it."""
    rules = os.path.join(root, "plugin", "rules")
    os.makedirs(rules)
    with open(os.path.join(rules, c.RULES_FILE), "w") as f:
        f.write("# rules\n")
    os.symlink(c.RULES_FILE, os.path.join(rules, c.RULES_OLD))
    return os.path.join(root, "plugin")


class TheFile(unittest.TestCase):
    def test_sampo_md_is_the_file_and_foreman_md_a_link_to_it(self):
        new, old = os.path.join(PLUGIN, "rules", "sampo.md"), os.path.join(PLUGIN, "rules", "foreman.md")
        self.assertTrue(os.path.isfile(new) and not os.path.islink(new))
        self.assertTrue(os.path.islink(old), "plugin/rules/foreman.md stays, as a symlink")
        self.assertEqual(os.path.realpath(old), os.path.realpath(new))
        self.assertEqual(os.readlink(old), "sampo.md", "relative, so a moved checkout keeps it")

    def test_the_helper_names_it(self):
        self.assertEqual(c.rules_path(), os.path.join(PLUGIN, "rules", "sampo.md"))
        self.assertEqual(c.rules_path("/x/plugin"), "/x/plugin/rules/sampo.md")
        self.assertEqual(c.RULES_NAMES, ("sampo.md", "foreman.md"))
        self.assertTrue(os.path.isfile(c.rules_path()))


class OneName(unittest.TestCase):
    def test_no_reader_spells_the_old_name(self):
        """A reader that kept its own literal would be missed by the guard the day the name changes again."""
        bad = []
        for dirpath, _dirs, files in os.walk(LIB):
            for name in files:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(dirpath, name)
                rel = os.path.relpath(path, ROOT)
                if rel in OLD_LITERAL_OK:
                    continue
                with open(path, encoding="utf-8") as f:
                    for n, line in enumerate(f, 1):
                        if re.search(r"""["'/ ]rules/foreman\.md\b|"rules", "foreman\.md"|'rules', 'foreman\.md'""", line):
                            bad.append(f"{rel}:{n}: {line.strip()[:90]}")
        self.assertEqual(bad, [])

    def test_the_guards_pair_is_the_constants(self):
        self.assertEqual(tuple(os.path.basename(p) for p in fmguard.RULES_PATHS), c.RULES_NAMES)
        self.assertTrue(all(p.startswith("plugin/rules/") for p in fmguard.RULES_PATHS))


class GuardCore(ForemanTestCase):
    def test_both_names_are_core(self):
        fh = os.path.join(self.tmp, "fh")
        plugin = fake_plugin(fh)
        ctx = fmguard.Ctx(cwd=self.tmp, project_root=self.tmp, home=self.tmp, foreman_home=fh)
        for name in c.RULES_NAMES:
            self.assertTrue(fmguard._is_core(os.path.join(plugin, "rules", name), ctx), name)
        roots = {p for p, cat in fmguard._protected_roots(ctx) if cat == "core"}
        for name in c.RULES_NAMES:
            self.assertIn(os.path.join(fh, "plugin", "rules", name), roots)


class Links(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(self.home, ".claude", "rules"))
        self.plugin = fake_plugin(os.path.join(self.tmp, "fh"))
        self.link = os.path.join(self.home, ".claude", "rules", c.RULES_OLD)

    def test_doctor_resolves_the_users_old_link_to_the_new_file(self):
        os.symlink(os.path.join(self.plugin, "rules", c.RULES_OLD), self.link)  # what an install made before the move
        with mock.patch.dict(os.environ, {"HOME": self.home}):
            r = doctor.check_rules_symlink(self.plugin)
        self.assertEqual(r.status, "PASS", r.detail)
        self.assertTrue(os.path.realpath(self.link).endswith("rules/sampo.md"))

    def test_doctor_resolves_a_link_made_to_the_new_name(self):
        os.symlink(c.rules_path(self.plugin), self.link)
        with mock.patch.dict(os.environ, {"HOME": self.home}):
            self.assertEqual(doctor.check_rules_symlink(self.plugin).status, "PASS")

    def test_doctor_warns_when_a_checkout_lost_the_old_name_link(self):
        """A checkout without symlink support writes foreman.md as a text file holding 'sampo.md'."""
        os.symlink(c.rules_path(self.plugin), self.link)
        old = os.path.join(self.plugin, "rules", c.RULES_OLD)
        os.remove(old)
        with open(old, "w") as f:
            f.write("sampo.md")
        with mock.patch.dict(os.environ, {"HOME": self.home}):
            r = doctor.check_rules_symlink(self.plugin)
        self.assertEqual(r.status, "WARN")
        self.assertIn("symlink", r.detail)
        self.assertIn("foreman.md", r.detail)

    def test_doctor_warns_when_the_old_link_resolves_through_a_text_stub(self):
        old = os.path.join(self.plugin, "rules", c.RULES_OLD)
        os.remove(old)
        with open(old, "w") as f:
            f.write("sampo.md")
        os.symlink(old, self.link)  # a link made before the move, now reading the stub
        with mock.patch.dict(os.environ, {"HOME": self.home}):
            r = doctor.check_rules_symlink(self.plugin)
        self.assertEqual(r.status, "WARN", r.detail)
        self.assertIn("sampo setup", r.detail)

    def test_setup_links_the_new_file(self):
        with mock.patch.object(c, "foreman_home", return_value=os.path.join(self.tmp, "fh")), \
                mock.patch.dict(os.environ, {"HOME": self.home}):
            P = setup.paths()
        self.assertEqual(P["rules_target"], os.path.join(self.tmp, "fh", "plugin", "rules", "sampo.md"))
        self.assertEqual(P["rules_link"], self.link, "the user's link keeps its name: installs already have it")
        os.symlink(os.path.join(self.plugin, "rules", c.RULES_OLD), P["rules_link"])
        self.assertEqual(setup._link_state(P), "ours", "a link made before the move still counts as ours")

    def test_the_rules_footprint_reads_through_the_link(self):
        with open(c.rules_path(self.plugin), "w") as f:
            f.write("x" * 123)
        with open(os.path.join(self.plugin, "rules", c.RULES_OLD), encoding="utf-8") as f:
            self.assertEqual(len(f.read()), 123)


class RenameScan(unittest.TestCase):
    def test_the_scan_baseline_does_not_rise(self):
        import test_rename_scan as t
        rows = t.leftover() + t.library_strings()
        self.assertLessEqual(len(rows), t.BASELINE, f"{len(rows)} lines say Foreman; first: {rows[:3]}")


if __name__ == "__main__":
    unittest.main()
