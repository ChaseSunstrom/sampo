"""T-5322: the hourly scorecard tick logs a scorecard_tick ledger event per counted tick (each measure's value and the names
past their bar), and `sampo measure scorecard-past-bar` reads them: the median count past bar over the window's ticks, in
its own unit. measure.summary returns {name, value, unit, count, note} for it and for accept-to-live (still minutes).
A scratch project and hand-written events; no real ledger, path or name."""
import json
import time
import unittest.mock

from helpers import ForemanTestCase

from sampo import core as c
from sampo import measure
from sampo import scorecard

MIN = 60.0


def row(name, value, ok, bar=1):
    return {"name": name, "label": name, "value": value, "bar": bar, "op": "<=", "unit": "", "ok": ok, "detail": ""}


class TickInputs(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)
        self.now = time.time()

    def ticks(self):
        return [e for e in c.ledger_tail(self.p, 100, ["scorecard_tick"]) if e.get("event") == "scorecard_tick"]

    def seed(self, at, past):
        c.log_event(self.p, "scorecard_tick", data={"at": at, "values": {}, "past": list(past)})

    def test_a_counted_tick_logs_one_event_with_each_value_and_the_names_past_their_bar(self):
        rows = [row("asks", 30.0, False), row("shipped", 1.5, True), row("cpu", None, None)]
        with unittest.mock.patch("sampo.scorecard.compute", return_value=rows):
            scorecard.tick(self.p, self.now)
        got = self.ticks()
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["data"]["values"], {"asks": 30.0, "shipped": 1.5, "cpu": None})
        self.assertEqual(got[0]["data"]["past"], ["asks"])
        self.assertEqual(got[0]["data"]["at"], self.now)

    def test_a_tick_inside_the_gap_logs_none(self):
        rows = [row("asks", 30.0, False)]
        with unittest.mock.patch("sampo.scorecard.compute", return_value=rows):
            scorecard.tick(self.p, self.now)
            scorecard.tick(self.p, self.now + 10)  # inside GAP_S: the same tick's view
        self.assertEqual(len(self.ticks()), 1)

    def test_the_median_count_past_bar_over_the_windows_ticks_is_the_verb_value(self):
        for k, past in enumerate([["asks"], ["asks", "lanes"], ["asks", "lanes", "steers"]]):
            self.seed(self.now - 3600 * (k + 1), past)
        got = json.loads(self.fm("measure", "scorecard-past-bar", "--json").stdout)
        self.assertEqual((got["median"], got["unit"], got["count"]), (2, "measures past bar", 3))
        self.assertEqual(got["verdict"], "ok")

    def test_the_verb_exits_1_past_max_under_min_and_with_no_tick_in_the_window(self):
        for k, past in enumerate([["asks"], ["asks", "lanes"], ["asks", "lanes", "steers"]]):
            self.seed(self.now - 3600 * (k + 1), past)
        self.assertEqual(self.fm("measure", "scorecard-past-bar", "--max", "1", check=False).returncode, 1)
        self.assertEqual(self.fm("measure", "scorecard-past-bar", "--min", "3", check=False).returncode, 1)
        self.assertEqual(self.fm("measure", "scorecard-past-bar", "--max", "2", check=False).returncode, 0)
        old = self.fm("measure", "scorecard-past-bar", "--window", "0.0001", check=False)
        self.assertEqual(old.returncode, 1, "no tick in the window is a miss")

    def test_a_tick_whose_every_read_failed_is_no_reading_not_a_count_of_zero(self):
        self.seed(self.now - 3600, [])  # a tick that read something: none past its bar
        c.log_event(self.p, "scorecard_tick", data={"at": self.now - 1800, "values": {"asks": None, "cpu": None}, "past": []})
        got = measure.summary(self.p, "scorecard-past-bar", window_days=1, now=self.now)
        self.assertEqual((got["value"], got["count"]), (0, 1), "an outage tick is left out of the median")

    def test_a_read_cut_short_with_no_tick_in_it_exits_1_with_no_bound(self):
        import argparse
        args = argparse.Namespace(name="scorecard-past-bar", max=None, min=None, window=1.0)
        short = ([], "ledger read covers only back to 2026-01-01T00:00:00Z")
        with unittest.mock.patch("sampo.cli.resolve", return_value=self.p), unittest.mock.patch("sampo.cli.out"), \
                unittest.mock.patch.object(measure, "read", return_value=short):
            self.assertEqual(measure.cmd_measure(args), 1, "no tick found, and the read cannot say there was none")

    def test_ticks_older_than_the_window_are_not_counted(self):
        self.seed(self.now - 3 * 86400, ["asks", "lanes", "steers", "cpu"])
        self.seed(self.now - 3600, [])
        got = measure.summary(self.p, "scorecard-past-bar", window_days=1, now=self.now)
        self.assertEqual((got["value"], got["count"]), (0, 1))

    def test_summary_names_value_unit_count_and_note_for_both_kinds(self):
        at = self.now - 3600
        c.log_event(self.p, "land_train", data={"land": "L1", "train": 1, "lanes": ["T-0001"], "seconds": 1.0, "gate_s": 1.0,
                                                "wait_s": 0.0, "at": at, "accepted": {"T-0001": at - 1200}})
        self.seed(at, ["asks", "lanes"])
        live = measure.summary(self.p, "accept-to-live", window_days=1, now=self.now)
        self.assertEqual(set(live), {"name", "value", "unit", "count", "note"})
        self.assertEqual((live["value"], live["unit"], live["count"]), (20.0, "min", 1))
        past = measure.summary(self.p, "scorecard-past-bar", window_days=1, now=self.now)
        self.assertEqual(set(past), {"name", "value", "unit", "count", "note"})
        self.assertEqual((past["value"], past["unit"], past["count"]), (2, "measures past bar", 1))
