"""T-3286: sampo flakes forecasts which tests will likely flake (fail count and recency from flakes.json, tagged load or
chance, weighted up when the box is loaded) and tells a red run's known flakes from a new break."""
import datetime
import json
import os

from helpers import ForemanTestCase

from sampo import core as c
from sampo import flake_forecast as ff


def ago(days):
    """A ledger `last` stamp, the writer's format, this many days back."""
    return (c.clock() - datetime.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


# Entries as core.note_flakes writes them: fails counts every failure, passes counts recoveries, last is the last
# recovery stamp, and why is the failing test's own line first, then its failure text.
SLOW = {"fails": 4, "passes": 2, "cmd": "make t", "last": ago(0), "why": "m.C.slow (x)\nAssertionError: timed out after 5 s"}
FLIP = {"fails": 2, "passes": 2, "cmd": "make t", "last": ago(1), "why": "m.C.flip (x)\nAssertionError: 1 != 2"}
ONCE = {"fails": 1, "passes": 1, "cmd": "make t", "last": ago(0), "why": "m.C.once (x)\nAssertionError: 1 != 2"}
STALE = {"fails": 3, "passes": 3, "cmd": "make t", "last": "2020-01-01T00:00:00Z", "why": "m.C.stale (x)\n1 != 2"}
LEDGER = {"pending": {}, "flaky": {"m.C.slow": SLOW, "m.C.flip": FLIP, "m.C.once": ONCE, "m.C.stale": STALE}}
QUIET = {"FOREMAN_HOST": "load=0.2,cpus=4"}
HOT = {"FOREMAN_HOST": "load=6,cpus=2"}  # 3 per cpu


class FlakeForecast(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo, create=False)
        self.path = os.path.join(self.p.dir, "flakes.json")

    def write_ledger(self, body):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(body if isinstance(body, str) else json.dumps(body))

    def test_a_forecast_ranks_by_fails_then_recency_not_by_a_rate(self):
        rows = ff.ranked(LEDGER["flaky"], per_cpu=0.2)
        self.assertEqual([r["name"] for r in rows], ["m.C.slow", "m.C.stale", "m.C.flip", "m.C.once"])
        self.assertEqual([r["trigger"] for r in rows], ["load", "chance", "chance", "chance"])
        self.assertEqual((rows[0]["fails"], rows[0]["recoveries"]), (4, 2))
        self.assertNotIn("rate", rows[0])
        same = {"m.C.old": {"fails": 2, "passes": 1, "last": ago(5)}, "m.C.new": {"fails": 2, "passes": 1, "last": ago(1)}}
        self.assertEqual([r["name"] for r in ff.ranked(same, per_cpu=0.2)], ["m.C.new", "m.C.old"])

    def test_the_load_tag_reads_the_failure_text_not_the_test_name(self):
        for why in ("m.C.t (x)\nDeadline exceeded", "m.C.t (x)\nTiming out", "m.C.t (x)\ntime out after 2s",
                    "m.C.t (x)\ntook 9.3 s, time limit 5"):
            self.assertEqual(ff.trigger({"why": why}), "load", why)
        self.assertEqual(ff.trigger({"why": "test_timeout_handling (m.C)\nAssertionError: 1 != 2"}), "chance")
        self.assertEqual(ff.trigger({"why": "test_x timed out"}), "chance")  # the naming line alone is no evidence
        self.assertEqual(ff.trigger({"why": "AssertionError: 1 != 2"}), "chance")
        self.assertEqual(ff.trigger({}), "chance")

    def test_a_loaded_box_weighs_up_only_the_load_tagged_flakes(self):
        flaky = {"m.C.slow": {"fails": 2, "passes": 2, "why": "m.C.slow (x)\ntimed out"},  # load, 2 fails
                 "m.C.flip": {"fails": 3, "passes": 1, "why": "m.C.flip (x)\n1 != 2"}}  # chance, 3 fails
        self.assertEqual([r["name"] for r in ff.ranked(flaky, per_cpu=0.2)], ["m.C.flip", "m.C.slow"])
        self.assertEqual([r["name"] for r in ff.ranked(flaky, per_cpu=2.0)], ["m.C.slow", "m.C.flip"])
        self.assertEqual([r["name"] for r in ff.ranked(flaky, per_cpu=None)], ["m.C.flip", "m.C.slow"])

    def test_the_known_flake_rule_is_two_fails_and_a_failure_in_thirty_days(self):
        known, new = ff.classify(LEDGER["flaky"], ["m.C.slow", "m.C.once", "m.C.stale", "m.C.none"], per_cpu=0.2)
        self.assertEqual([k["name"] for k in known], ["m.C.slow"])
        why = {n["name"]: n["why"] for n in new}
        self.assertIn("1 fail", why["m.C.once"])
        self.assertIn("30 days", why["m.C.stale"])
        self.assertIn("no flake history", why["m.C.none"])

    def test_sampo_flakes_prints_the_forecast_and_splits_a_red_run(self):
        self.write_ledger(LEDGER)
        out = self.fm("flakes", env=QUIET).stdout
        self.assertLess(out.index("m.C.slow"), out.index("m.C.flip"))
        self.assertIn("4 fails, 2 recoveries", out)
        self.assertIn("load", out)
        self.assertNotIn("%", out)
        d = json.loads(self.fm("flakes", "--red", "m.C.slow", "m.C.once", "m.C.stale", "m.C.brand_new", "--json",
                               env=HOT).stdout)
        self.assertEqual([r["name"] for r in d["forecast"]], ["m.C.slow", "m.C.stale", "m.C.flip", "m.C.once"])
        self.assertTrue(d["hot"])
        self.assertEqual(d["load_per_cpu"], 3.0)
        self.assertEqual([k["name"] for k in d["red"]["known"]], ["m.C.slow"])
        self.assertEqual(d["red"]["known"][0]["trigger"], "load")
        self.assertIn("make t", d["red"]["known"][0]["rerun"])
        self.assertIn("quiet box", d["red"]["known"][0]["rerun"])  # the box is loaded: say so
        self.assertEqual([n["name"] for n in d["red"]["new"]], ["m.C.once", "m.C.stale", "m.C.brand_new"])
        text = self.fm("flakes", "--red", "m.C.slow", "m.C.brand_new", env=QUIET).stdout
        self.assertIn("known: m.C.slow", text)
        self.assertIn("new: m.C.brand_new", text)
        self.assertIn("possibly a real break", text)
        self.assertNotIn("quiet box", text)  # a quiet box needs no quiet-box hint

    def test_counts_are_the_writers_whole_numbers_or_nothing(self):
        self.assertEqual(ff.counts({"fails": 2, "passes": 1}), (2, 1))
        for bad in ({"fails": True, "passes": 1}, {"fails": 0, "passes": 3}, {"fails": 1, "passes": -1},
                    {"seen": 2}, {"fails": 1.5, "passes": 0}):
            self.assertIsNone(ff.counts(bad), bad)

    def test_a_missing_empty_or_damaged_ledger_says_no_flake_history_and_exits_zero(self):
        for body in (None, "", "{not json", "[1, 2]", '{"flaky": "x"}', '{"flaky": {"a": "junk"}}',
                     '{"flaky": {"a": {"fails": true, "passes": 1}}}'):
            with self.subTest(body=body):
                if body is None and os.path.exists(self.path):
                    os.remove(self.path)
                elif body is not None:
                    self.write_ledger(body)
                r = self.fm("flakes")  # check=True: a nonzero exit fails the test
                self.assertIn("no flake history", r.stdout)
        self.assertIn("no flake history", self.fm("flakes", "--red", "m.C.x").stdout)


if __name__ == "__main__":
    import unittest
    unittest.main()
