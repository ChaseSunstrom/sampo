"""T-4570: the post-merge verify covers many unverified merges with one full run at the newest tip. After a revert moves
HEAD, the merges still pending are covered by one run at the new HEAD, not a full suite each: on 2026-11-13 the
verify ran individual suites after three reverts (about 2 h of slots) because its probe was keyed on the newest merge,
which a revert does not change."""
import json
import os
import subprocess
import unittest.mock

from helpers import ForemanTestCase, read_text

from sampo import core as c
from sampo import lanes as slanes

first = lambda text: json.JSONDecoder().raw_decode(text)[0]


class CoverOnce(ForemanTestCase):
    RED = "if ls bad*.txt >/dev/null 2>&1; then echo 'FAIL: test_x (test_mod.Cls.test_x)'; exit 1; fi"

    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)
        self.runs = os.path.join(self.tmp, "runs.txt")
        os.environ["FM_POSTMERGE_CMD"] = f"echo run >> {self.runs}; " + self.RED
        self.addCleanup(os.environ.pop, "FM_POSTMERGE_CMD", None)
        self.tid = first(self.fm("task", "new", "Landed", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true",
                                 "--step", "s", "--json").stdout)["id"]

    def git(self, *args):
        return subprocess.run(["git", "-C", self.repo, *args], capture_output=True, text=True).stdout.strip()

    def land(self, name, bad=False):
        """A --no-ff merge commit on main, recorded as pending the way merge() records it."""
        self.git("checkout", "-qb", f"b-{name}")
        with open(os.path.join(self.repo, "bad.txt" if bad else f"{name}.txt"), "w") as f:
            f.write(name)
        self.git("add", "-A")
        self.git("commit", "-qm", name)
        self.git("checkout", "-q", "-")
        self.git("merge", "-q", "--no-ff", "-m", f"Merge {name}", f"b-{name}")
        sha = self.git("rev-parse", "HEAD")
        with unittest.mock.patch.object(slanes, "pm_spawn"):
            slanes.pm_record(self.p, sha, self.tid, self.repo)
        return sha

    def state(self, sha):
        return slanes.pm_records(self.p)[sha]["state"]

    def test_the_merges_pending_after_a_revert_are_covered_by_one_run_at_the_new_head(self):
        # a, x (red), n, m land together: the probe at the first HEAD is red and names no class to narrow to, so a is
        # verified alone and x is found red and reverted; n and m stay pending on the new HEAD, which one run covers
        a, bad, n, m = self.land("a"), self.land("x", bad=True), self.land("n"), self.land("m")
        slanes.pm_run(self.p, self.repo)
        self.assertEqual(self.state(a), "green")
        self.assertEqual(self.state(bad), "reverted")
        self.assertEqual([self.state(n), self.state(m)], ["green", "green"])
        self.assertTrue(slanes.pm_records(self.p)[n].get("covered_by"), "n is covered by the run at the new HEAD")
        self.assertEqual(read_text(self.runs).count("run"), 4,
                         "the first probe, a, x, then one run for n and m (was five: n and m each alone)")

    def test_every_suite_run_of_the_verify_yields_its_slots_to_waiting_builders(self):
        # T-4570: the verify's runs are the low class: a waiting builder's class goes before a queued verify class
        seen, real = [], c.run_command

        def spy(*a, **k):
            seen.append(bool(k.get("low")))
            return real(*a, **k)

        self.land("a"), self.land("x", bad=True), self.land("n")
        with unittest.mock.patch.object(c, "run_command", spy):
            slanes.pm_run(self.p, self.repo)
        self.assertGreaterEqual(len(seen), 4)
        self.assertTrue(all(seen), f"every run of the verify is low: {seen}")

    def test_a_second_run_settles_nothing_twice(self):
        self.land("a"), self.land("x", bad=True), self.land("n"), self.land("m")
        slanes.pm_run(self.p, self.repo)
        before = read_text(self.runs).count("run")
        slanes.pm_run(self.p, self.repo)
        self.assertEqual(read_text(self.runs).count("run"), before, "nothing is pending: no suite runs")

    def test_a_red_probe_at_one_head_is_not_run_again_at_that_head(self):
        # the key is (newest, HEAD): a red probe at a HEAD is not repeated while neither moves, so the merges go oldest
        # first as before (a, x, n: one probe and three verifies, with no second probe)
        a, bad, n = self.land("a"), self.land("x", bad=True), self.land("n")
        slanes.pm_run(self.p, self.repo)
        self.assertEqual(read_text(self.runs).count("run"), 4, "one probe on the newest, then a, x and n oldest first")
        self.assertEqual(self.state(n), "green")
