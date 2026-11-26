"""T-5241 (child c6 of T-4322): sampo doctor warns when the unwired count grew. The check reads census.json (written by a
capture run, T-5239) and never scans: a grown count warns and names the growth; an equal or lower count, a census with no
previous count, or no census at all passes."""
import json
import os
from unittest import mock

from test_outcomes import Base  # first: it puts plugin/lib on the path

from sampo import doctor
from sampo import wired


class Unwired(Base):
    def write(self, unwired, previous):
        with open(os.path.join(self.p.dir, "census.json"), "w", encoding="utf-8") as f:
            json.dump({"unwired": unwired, "previous": previous, "at": "2026-11-25T12:00:00Z"}, f)

    def check(self):
        return doctor.check_unwired([self.p])

    def test_a_grown_count_warns_naming_the_growth_and_sampo_wired(self):
        self.write(7, 4)
        r = self.check()
        self.assertEqual(r.status, "WARN")
        self.assertIn("4", r.detail)
        self.assertIn("7", r.detail)
        self.assertIn("sampo wired", r.detail)

    def test_an_equal_count_passes(self):
        self.write(4, 4)
        self.assertEqual(self.check().status, "PASS")

    def test_a_lower_count_passes(self):
        self.write(2, 4)
        self.assertEqual(self.check().status, "PASS")

    def test_a_census_with_no_previous_count_passes(self):
        self.write(5, None)
        self.assertEqual(self.check().status, "PASS")

    def test_no_census_yet_passes(self):
        self.assertFalse(os.path.exists(os.path.join(self.p.dir, "census.json")))
        self.assertEqual(self.check().status, "PASS")

    def test_an_unreadable_census_passes_as_no_census(self):
        with open(os.path.join(self.p.dir, "census.json"), "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertEqual(self.check().status, "PASS")

    def test_a_deeply_nested_census_passes_instead_of_raising(self):
        with open(os.path.join(self.p.dir, "census.json"), "w", encoding="utf-8") as f:
            f.write("[" * 100000)
        self.assertEqual(self.check().status, "PASS")

    def test_the_check_never_scans(self):
        self.write(7, 4)
        with mock.patch.object(wired, "scan_full", side_effect=AssertionError("scanned")), \
                mock.patch.object(wired, "scan", side_effect=AssertionError("scanned")):
            self.assertEqual(self.check().status, "WARN")
