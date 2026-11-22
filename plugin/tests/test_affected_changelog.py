# map-links: plugin/lib/sampo/map.py
"""T-5587: every lane appends one CHANGELOG.md line, and 87 recorded test classes held CHANGELOG.md whole because a shared
path (the session note, doctor's release age, a close) stat'ed or read it, so each lane's affected set held them all. An
edit to CHANGELOG.md selects the classes whose test file names CHANGELOG, the ones that check its content."""
import os
import subprocess
from unittest import mock

from helpers import ForemanTestCase

NAMES = "plugin/tests/test_release.py"  # its text names CHANGELOG
SILENT = "plugin/tests/test_sampo_spelling.py"  # its text never does
LIB = "plugin/lib/sampo/hooks.py"


class AffectedChangelog(ForemanTestCase):
    def selected(self, records, files):
        from sampo import map as smap
        import run
        top = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        common = subprocess.run(["git", "-C", top, "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                capture_output=True, text=True, check=True).stdout.strip()
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": os.path.join(self.tmp, "real")}):
            run.save_cache(run.cache_path(os.path.realpath(common)),
                           {cid: ["d", 2, keys] for cid, keys in records.items()}, 0)
            return smap.ran_tests(top, "HEAD", "HEAD", files)

    RECORDS = {"test_release.Release": [LIB + "::f", "CHANGELOG.md", NAMES],
               "test_sampo_spelling.Spelling": [LIB + "::f", "CHANGELOG.md", SILENT]}

    def test_a_changelog_edit_selects_only_the_classes_whose_test_names_changelog(self):
        got = self.selected(self.RECORDS, ["CHANGELOG.md"])
        self.assertEqual([NAMES], got)

    def test_other_files_still_select_every_class_that_read_them(self):
        both = {"test_release.Release": [LIB + "::f", "README.md", NAMES],
                "test_sampo_spelling.Spelling": [LIB + "::f", "README.md", SILENT]}
        self.assertEqual(sorted([NAMES, SILENT]), self.selected(both, ["README.md"]))

    def test_a_changelog_edit_beside_another_file_keeps_that_files_readers(self):
        both = {"test_release.Release": [LIB + "::f", "CHANGELOG.md", NAMES],
                "test_sampo_spelling.Spelling": [LIB + "::f", "README.md", SILENT]}
        self.assertEqual(sorted([NAMES, SILENT]), self.selected(both, ["CHANGELOG.md", "README.md"]))
