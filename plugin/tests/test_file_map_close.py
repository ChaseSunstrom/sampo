#!/usr/bin/env python3
# T-1179: fm task finish refuses a close that added a plugin/<entry> MASTER.md's file map doesn't list
import os
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace

from helpers import ForemanTestCase  # noqa: F401  (puts plugin/lib on sys.path)
from sampo import cli

MAP = "# M\n## 3. File map\n| Path | Purpose |\n|---|---|\n| `plugin/bin/` | cli |\n## 4. Next\n"


def gaps(changed, master):
    with tempfile.TemporaryDirectory() as root:
        for rel in ("plugin/bin/fm", "plugin/newdir/x"):
            os.makedirs(os.path.join(root, os.path.dirname(rel)), exist_ok=True)
            open(os.path.join(root, rel), "w").close()
        if master is not None:
            with open(os.path.join(root, "MASTER.md"), "w") as f:
                f.write(master)
        with mock.patch.object(cli.c, "git_root", return_value=root), \
                mock.patch.object(cli.c, "task_base", return_value="abc"), \
                mock.patch("sampo.map.changed", return_value=changed):
            return cli._file_map_gaps(SimpleNamespace(root=root, lane=None),
                                         SimpleNamespace(id="T-0001", meta={}))


class FileMapClose(unittest.TestCase):
    def test_new_entry_without_row_is_refused(self):
        g = gaps(["plugin/newdir/x"], MAP)
        self.assertEqual(len(g), 1)
        self.assertIn("plugin/newdir", g[0])

    def test_row_present_closes(self):
        self.assertEqual(gaps(["plugin/newdir/x"], MAP.replace("## 4.", "| `plugin/newdir/` | n |\n## 4.")), [])

    def test_known_entry_skips(self):
        self.assertEqual(gaps(["plugin/bin/fm"], MAP), [])

    def test_no_map_is_unaffected(self):
        self.assertEqual(gaps(["plugin/newdir/x"], None), [])
        self.assertEqual(gaps(["plugin/newdir/x"], "# M\nno map here\n"), [])


if __name__ == "__main__":
    unittest.main()
