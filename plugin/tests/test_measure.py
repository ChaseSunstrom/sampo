"""sampo measure (T-5005): accept-to-live and accept-to-pushed read from raw land_train and ship_done ledger events, held
to --max / --min. A scratch project and hand-written ledger events; no real ledger, path or name."""
import json
import time

from helpers import ForemanTestCase

from sampo import core as c
from sampo import measure
from sampo import train

MIN = 60.0


class Measure(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)
        self.now = time.time()

    def train(self, at, accepted, land="L1"):
        c.log_event(self.p, "land_train", data={"land": land, "train": 1, "lanes": list(accepted), "seconds": 1.0,
                                                "gate_s": 1.0, "wait_s": 0.0, "at": at, "accepted": accepted})

    def ship(self, start, end, exit=0):
        c.log_event(self.p, "ship_done", task="T-0001", data={"head": "a" * 7, "pushed": "b" * 7, "start": start,
                                                              "end": end, "exit": exit})

    def run_measure(self, *args, check=False):
        return self.fm("measure", *args, check=check)

    def three_lanes(self):
        """One train 1800 s after accept for T-1, 1200 s for T-2 and 600 s for T-3: the median is 20 minutes."""
        at = self.now - 3600
        self.train(at, {"T-0001": at - 1800, "T-0002": at - 1200, "T-0003": at - 600})
        return at

    def test_live_median_is_the_trains_at_minus_each_accept(self):
        self.three_lanes()
        got = json.loads(self.run_measure("accept-to-live", "--json").stdout)
        self.assertEqual((got["count"], got["median_min"], got["verdict"]), (3, 20.0, "ok"))

    def test_live_even_count_takes_the_mean_of_the_middle_two(self):
        at = self.now - 3600
        self.train(at, {"T-0001": at - 600, "T-0002": at - 1800})
        got = json.loads(self.run_measure("accept-to-live", "--json").stdout)
        self.assertEqual((got["count"], got["median_min"]), (2, 20.0))

    def test_pushed_is_the_end_of_the_first_good_ship_started_after_the_train(self):
        at = self.three_lanes()
        self.ship(at - 100, at - 50)            # started before the train: not this train's push
        self.ship(at + 10, at + 20, exit=1)     # failed: pushed nothing
        self.ship(at + 100, at + 400)           # the first good one
        self.ship(at + 500, at + 900)           # a later one does not count
        self.assertEqual(sorted(measure.values(self.p, "accept-to-pushed", 1, now=self.now)),
                         [1000.0, 1600.0, 2200.0])
        got = json.loads(self.run_measure("accept-to-pushed", "--json").stdout)
        self.assertEqual((got["count"], got["median_min"]), (3, round(1600 / 60, 2)))

    def test_pushed_picks_the_first_by_start_when_the_ledger_holds_them_out_of_order(self):
        at = self.now - 3600
        self.train(at, {"T-0001": at - 100})
        self.ship(at + 500, at + 900)
        self.ship(at + 100, at + 400)
        self.assertEqual(measure.values(self.p, "accept-to-pushed", 1, now=self.now), [500.0])

    def test_pushed_a_train_no_ship_followed_is_not_a_number_but_live_still_counts_it(self):
        at = self.three_lanes()
        self.ship(at - 100, at - 50)
        self.assertEqual(measure.values(self.p, "accept-to-pushed", 1, now=self.now), [])
        self.assertEqual(len(measure.values(self.p, "accept-to-live", 1, now=self.now)), 3)

    def test_window_cuts_the_trains_older_than_the_days_asked(self):
        old = self.now - 3 * 86400
        self.train(old, {"T-0009": old - 6000})
        self.three_lanes()
        self.assertEqual(json.loads(self.run_measure("accept-to-live", "--json").stdout)["count"], 3)
        wide = json.loads(self.run_measure("accept-to-live", "--window", "5", "--json").stdout)
        self.assertEqual((wide["count"], wide["window_days"]), (4, 5.0))

    def test_max_a_median_over_it_exits_1_and_under_it_exits_0(self):
        self.three_lanes()
        miss = self.run_measure("accept-to-live", "--max", "15")
        self.assertEqual(miss.returncode, 1)
        self.assertIn("misses --max 15", miss.stdout)
        self.assertEqual(self.run_measure("accept-to-live", "--max", "20").returncode, 0)
        self.assertEqual(self.run_measure("accept-to-live", "--max", "25").returncode, 0)

    def test_min_a_median_under_it_exits_1_and_over_it_exits_0(self):
        self.three_lanes()
        miss = self.run_measure("accept-to-live", "--min", "25")
        self.assertEqual(miss.returncode, 1)
        self.assertIn("misses --min 25", miss.stdout)
        self.assertEqual(self.run_measure("accept-to-live", "--min", "20").returncode, 0)

    def test_no_bound_exits_0_whatever_the_median(self):
        self.three_lanes()
        got = self.run_measure("accept-to-live")
        self.assertEqual(got.returncode, 0)
        self.assertIn("median 20.0 min over 3 lane(s)", got.stdout)

    def test_unknown_name_is_a_usage_error_listing_the_registered_names(self):
        got = self.run_measure("accept-to-nowhere")
        self.assertEqual(got.returncode, 1)
        self.assertIn(f"registered: {', '.join(sorted(measure.MEASURES))}", got.stderr)

    def test_each_registered_name_runs_through_the_verb_under_its_own_name(self):
        for name in measure.MEASURES:
            got = self.run_measure(name, "--json")
            self.assertEqual(got.returncode, 0, name)
            self.assertEqual(json.loads(got.stdout)["name"], name)

    def test_no_name_lists_the_names_too(self):
        got = self.run_measure()
        self.assertEqual(got.returncode, 1)
        self.assertIn("accept-to-live", got.stderr)

    def test_empty_ledger_says_so_and_exits_0_without_a_bound(self):
        got = self.run_measure("accept-to-live")
        self.assertEqual((got.returncode, got.stdout.strip()), (0, "no landings in window"))
        data = json.loads(self.run_measure("accept-to-pushed", "--json").stdout)
        self.assertEqual((data["count"], data["median_min"]), (0, None))

    def test_empty_ledger_with_a_bound_exits_1(self):
        for flag in ("--max", "--min"):
            got = self.run_measure("accept-to-live", flag, "5")
            self.assertEqual((got.returncode, got.stdout.strip()), (1, "no landings in window"), flag)

    def test_a_bad_bound_or_window_is_refused(self):
        for args in (("--max", "-1"), ("--min", "nan"), ("--window", "0"), ("--window", "-2"), ("--window", "inf")):
            got = self.run_measure("accept-to-live", *args)
            self.assertEqual(got.returncode, 1, args)
            self.assertIn("measure:", got.stderr, args)

    def test_junk_in_the_events_is_skipped_not_fatal(self):
        at = self.now - 3600
        self.train(at, {"T-0001": "soon", "T-0002": None, "T-0003": True, "T-0004": at - 600})
        c.log_event(self.p, "land_train", data={"land": "L2", "at": "now", "accepted": {"T-0005": at - 5}})
        c.log_event(self.p, "land_train", data={"land": "L3", "at": at, "accepted": ["T-0006"]})
        c.log_event(self.p, "ship_done", data={"start": "x", "end": None, "exit": 0})
        self.assertEqual(measure.values(self.p, "accept-to-live", 1, now=self.now), [600.0])
        self.assertEqual(measure.values(self.p, "accept-to-pushed", 1, now=self.now), [])

    def test_status_and_measure_share_one_helper_and_one_name_for_pushed(self):
        at = self.three_lanes()
        self.ship(at + 100, at + 400)
        pairs = [x for x in (train.train_accepts(d) for d in [{"at": at, "accepted": {"T-1": at - 600}}]) if x]
        self.assertEqual(train.pushed_seconds([{"event": "ship_done", "data": {"start": at + 100, "end": at + 400, "exit": 0}}],
                                              pairs), [1000.0])
        self.assertIsNone(train.train_accepts({"at": "x", "accepted": {}}))
        import inspect
        self.assertIn("pushed_seconds", inspect.getsource(train.last_land))
        self.assertIn("pushed_seconds", inspect.getsource(measure.accept_to_pushed))
        helptxt = self.fm("measure", "--help").stdout
        self.assertIn("merge", helptxt)
        self.assertIn("pushed", helptxt)

    def test_a_window_reaching_past_a_busy_ledger_tail_still_finds_the_train(self):
        at = self.three_lanes()
        for i in range(120):
            c.log_event(self.p, "noise", data={"i": i})
        old = measure.TAIL
        measure.TAIL = 20
        try:
            self.assertEqual(len(measure.values(self.p, "accept-to-live", 1, now=self.now)), 3)
        finally:
            measure.TAIL = old

    def test_a_read_that_cannot_reach_the_window_says_so_and_does_not_pass(self):
        self.three_lanes()
        for i in range(120):
            c.log_event(self.p, "noise", data={"i": i})
        old = (measure.TAIL, measure.MAXTAIL)
        measure.TAIL = measure.MAXTAIL = 20
        try:
            secs, note = measure.read(self.p, "accept-to-live", 1, now=self.now)
        finally:
            measure.TAIL, measure.MAXTAIL = old
        self.assertEqual(secs, [])
        self.assertIn("ledger read covers only back to", note)

    def test_the_real_writers_records_are_what_measure_reads(self):
        class B:
            meta = {"accepted_at": time.time() - 900}
        t0 = time.time() - 600
        train._train_times(self.p, "L9", 1, {"merged": ["T-0001"]}, {"T-0001": B()}, 5.0)
        train._ship_end(self.p, "T-0001", "a" * 40, 0, "", time.time() + 1, time.time() + 60)
        self.assertEqual(len(measure.values(self.p, "accept-to-live", 1)), 1)
        self.assertEqual(len(measure.values(self.p, "accept-to-pushed", 1)), 1)
