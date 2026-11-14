#!/usr/bin/env python3
# CLEAN T-0764: fm task finish refuses a close whose diff touches fmcli.py or plugin/skills while Foreman's self docs fail
import unittest
from unittest import mock
from types import SimpleNamespace

from helpers import ForemanTestCase  # noqa: F401  (puts plugin/lib on sys.path)
from sampo import cli
from sampo import doctor


def gap(changed, status):
    """_self_docs_gaps with a stubbed diff and a stubbed self docs check."""
    with mock.patch.object(cli.c, "git_root", return_value="/x"), \
            mock.patch.object(cli.c, "task_base", return_value="abc"), \
            mock.patch("sampo.map.changed", return_value=changed), \
            mock.patch("sampo.doctor.check_self_docs", return_value=doctor.Result("self docs", status, "stale")):
        return cli._self_docs_gaps(SimpleNamespace(root="/x", lane=None), SimpleNamespace(id="T-0001", meta={}))


class SelfDocsGap(unittest.TestCase):
    def test_skill_change_with_failing_docs_is_refused(self):
        self.assertEqual(gap(["plugin/skills/intake/SKILL.md"], "FAIL"), ["self docs: stale"])

    def test_fmcli_change_with_failing_docs_is_refused(self):
        self.assertEqual(gap(["plugin/lib/sampo/cli.py"], "FAIL"), ["self docs: stale"])

    def test_passing_docs_do_not_block(self):
        self.assertEqual(gap(["plugin/skills/intake/SKILL.md"], "PASS"), [])

    def test_unrelated_change_skips_the_check(self):
        self.assertEqual(gap(["plugin/lib/sampo/doctor.py"], "FAIL"), [])


if __name__ == "__main__":
    unittest.main()
