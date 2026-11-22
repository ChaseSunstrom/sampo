"""sampo measure (T-5005): accept-to-live and accept-to-pushed read from raw land_train and ship_done ledger events, held
to --max / --min. A scratch project and hand-written ledger events; no real ledger, path or name."""
import json
import os
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

    def used(self, filed_ago_min, used_ago_min=1.0, tid="T-0001"):
        """A pipeline_used row: filed `filed_ago_min` minutes ago, first used `used_ago_min` minutes ago."""
        c.log_event(self.p, "pipeline_used", task=tid, data={"filed": self.now - filed_ago_min * MIN,
                                                             "merged": self.now - 5 * MIN,
                                                             "used": self.now - used_ago_min * MIN})

    def test_pipeline_in_use_is_the_first_used_time_minus_the_filed_time(self):
        self.used(31, 1, "T-0001")   # 30 min
        self.used(61, 1, "T-0002")   # 60 min
        self.used(121, 1, "T-0003")  # 120 min
        got = json.loads(self.run_measure("pipeline-in-use", "--json").stdout)
        self.assertEqual((got["count"], got["median_min"], got["verdict"]), (3, 60.0, "ok"))

    def test_pipeline_in_use_junk_rows_and_rows_out_of_the_window_are_skipped(self):
        self.used(31, 1)
        c.log_event(self.p, "pipeline_used", task="T-0002", data={"filed": "x", "used": None})
        c.log_event(self.p, "pipeline_used", task="T-0003", data={"filed": self.now, "used": self.now - 600})
        c.log_event(self.p, "pipeline_used", task="T-0004", data={"filed": self.now - 9 * 86400, "used": self.now - 8 * 86400})
        self.assertEqual(measure.values(self.p, "pipeline-in-use", 1, now=self.now), [1800.0])

    def test_pipeline_in_use_is_recorded_at_the_merge_and_closed_by_the_next_start_once(self):
        class B:
            id = "T-0007"
            meta = {"created": c.now()}
        measure.note_pipeline_merged(self.p, B())
        self.assertEqual(c.ledger_tail(self.p, 50, ["pipeline_used"]), [], "merged is open, not yet in use")
        measure.note_pipeline_used(self.p)
        measure.note_pipeline_used(self.p)  # a second start finds nothing open
        evs = c.ledger_tail(self.p, 50, ["pipeline_used"])
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["task"], "T-0007")
        self.assertEqual(sorted(evs[0]["data"]), ["filed", "merged", "used"])
        self.assertGreaterEqual(evs[0]["data"]["used"], evs[0]["data"]["merged"])
        self.assertEqual([f for f in os.listdir(self.p.dir) if f.startswith("pipeline-open.json")], [],
                         "no open file and no temp file left behind")

    def test_pipeline_in_use_two_merges_at_once_both_stay_open_and_each_is_used_once(self):
        import threading

        class B:
            def __init__(self, n):
                self.id, self.meta = f"T-{n:04d}", {"created": c.now()}
        ts = [threading.Thread(target=measure.note_pipeline_merged, args=(self.p, B(n))) for n in range(1, 9)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        measure.note_pipeline_used(self.p)
        got = sorted(e["task"] for e in c.ledger_tail(self.p, 50, ["pipeline_used"]))
        self.assertEqual(got, [f"T-{n:04d}" for n in range(1, 9)], "no read-modify-write lost a lane")

    def test_pipeline_in_use_never_raises_on_a_bad_project_or_a_lane_with_no_filed_time(self):
        measure.note_pipeline_used(None)

        class B:
            id = "T-0008"
            meta = {}
        measure.note_pipeline_merged(None, B())
        measure.note_pipeline_merged(self.p, B())
        measure.note_pipeline_used(self.p)
        self.assertEqual(c.ledger_tail(self.p, 50, ["pipeline_used"]), [], "no filed time: no first-used number")

    def test_median_within_hour_is_shown_in_status_and_passes_the_bound(self):
        self.used(31, 1, "T-0001")
        self.used(41, 1, "T-0002")
        line = measure.pipeline_line(self.p)
        self.assertIn("median 35 min from filed to in use over 2 lanes", line)
        self.assertIn("within the hour", line)
        self.assertNotIn("ABOVE", line)
        self.assertEqual(self.run_measure("pipeline-in-use", "--max", "60").returncode, 0)

    def test_median_within_hour_bound_a_median_above_it_is_flagged_in_status_and_misses(self):
        self.used(91, 1, "T-0001")
        self.used(151, 1, "T-0002")
        line = measure.pipeline_line(self.p)
        self.assertIn("median 120 min", line)
        self.assertIn("ABOVE 60 min", line)
        miss = self.run_measure("pipeline-in-use", "--max", "60")
        self.assertEqual(miss.returncode, 1)
        self.assertIn("misses --max 60", miss.stdout)

    def test_median_within_hour_status_prints_nothing_without_a_pipeline_lane_used(self):
        self.assertEqual(measure.pipeline_line(self.p), "")

    def test_median_within_hour_line_reaches_the_status_verb(self):
        self.fm("task", "new", "a task so status has a body", "--type", "FEATURE", "--tier", "S")
        self.used(91, 1)
        self.assertIn("Pipeline fixes: median 90 min", self.fm("status").stdout)

    def test_the_real_writers_records_are_what_measure_reads(self):
        class B:
            meta = {"accepted_at": time.time() - 900}
        t0 = time.time() - 600
        train._train_times(self.p, "L9", 1, {"merged": ["T-0001"]}, {"T-0001": B()}, 5.0)
        train._ship_end(self.p, "T-0001", "a" * 40, 0, "", time.time() + 1, time.time() + 60)
        self.assertEqual(len(measure.values(self.p, "accept-to-live", 1)), 1)
        self.assertEqual(len(measure.values(self.p, "accept-to-pushed", 1)), 1)


class ReviewCost(ForemanTestCase):
    """T-5843: review-cost sums the review_cost events (one per audit run, T-5842) per UTC day, with the saving against
    the five-lens default. Hand-written ledger lines with chosen timestamps; no real ledger."""

    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)
        self.now = time.time()
        self.today = self.now // measure.DAY * measure.DAY

    def day(self, k):
        return time.strftime("%Y-%m-%d", time.gmtime(self.today - k * measure.DAY))

    def review(self, k, task, lenses, usd=0.1, tokens=(100, 50, 0, 0), at=12 * 3600, **over):
        """One review_cost event, k days ago at `at` seconds into the UTC day; tokens is (input, output, cache_read,
        cache_creation). A `lenses` of None leaves the field out, as an event written before it carried a count."""
        data = {"model": "sonnet", "usd": usd, **dict(zip(("input", "output", "cache_read", "cache_creation"), tokens))}
        if lenses is not None:
            data["lenses"] = lenses
        data.update(over)
        with open(os.path.join(self.p.dir, "ledger.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": c.iso(self.today - k * measure.DAY + at), "event": "review_cost", "task": task,
                                "data": data}) + "\n")

    def run_measure(self, *args, check=True):
        return self.fm("measure", "review-cost", *args, check=check)

    def rows(self, *args):
        return json.loads(self.run_measure("--json", *args).stdout)

    def two_days(self):
        """Yesterday: T-1 with 2 lenses (150 tokens, $0.2) and T-2 with 5 (300 tokens, $0.5). Two days back: T-1 with 3."""
        self.review(1, "T-0001", ["intent", "edge"], usd=0.2, tokens=(100, 50, 0, 0))
        self.review(1, "T-0002", ["intent", "adversary", "edge", "operator", "maintainer"], usd=0.5, tokens=(200, 50, 40, 10))
        self.review(2, "T-0001", ["intent", "edge", "operator"], usd=0.3, tokens=(300, 0, 0, 0))

    def test_review_cost_sums_lanes_lenses_tokens_and_dollars_per_day(self):
        self.two_days()
        got = self.rows("--window", "5")
        self.assertEqual(got["name"], "review-cost")
        days = {r["day"]: r for r in got["days"]}
        self.assertEqual(sorted(days), [self.day(2), self.day(1)])
        y = days[self.day(1)]
        self.assertEqual((y["lanes"], y["runs"], y["lenses"], y["tokens"], y["usd"]), (2, 2, 7, 450, 0.7))
        d2 = days[self.day(2)]
        self.assertEqual((d2["lanes"], d2["lenses"], d2["tokens"], d2["usd"]), (1, 3, 300, 0.3))
        self.assertEqual((got["total"]["lanes"], got["total"]["tokens"]), (2, 750))

    def test_review_cost_the_text_names_each_day_and_exits_0_with_no_bound(self):
        self.two_days()
        got = self.run_measure("--window", "5")
        self.assertEqual(got.returncode, 0)
        self.assertIn(self.day(1), got.stdout)
        self.assertIn(self.day(2), got.stdout)
        self.assertIn("$0.70", got.stdout)

    def test_review_cost_window_leaves_older_days_out(self):
        self.two_days()
        got = self.rows("--window", "1.5")
        self.assertEqual([r["day"] for r in got["days"]], [self.day(1)])

    def test_review_cost_an_empty_window_says_so_and_prints_no_saving(self):
        self.review(9, "T-0001", ["intent"])
        got = self.run_measure("--window", "2")
        self.assertEqual((got.returncode, got.stdout.strip()), (0, "no reviews in window"))
        data = self.rows("--window", "2")
        self.assertEqual((data["days"], data["total"]["saving_tokens"]), ([], None))

    def test_review_cost_takes_no_bound(self):
        for flag in ("--max", "--min"):
            got = self.run_measure(flag, "5", check=False)
            self.assertEqual(got.returncode, 1, flag)
            self.assertIn("review-cost", got.stderr)

    def test_review_cost_saving_is_the_lenses_not_run_times_the_mean_tokens_per_lens(self):
        self.two_days()
        days = {r["day"]: r for r in self.rows("--window", "5")["days"]}
        y = days[self.day(1)]  # 7 lenses over 2 runs: (5-2)+(5-5)=3 not run, 450 tokens / 7 lenses
        self.assertEqual(y["saving_tokens"], round(3 * 450 / 7))
        self.assertEqual(y["saving_usd"], round(3 * 0.7 / 7, 4))
        self.assertEqual(days[self.day(2)]["saving_tokens"], 200)  # 2 lenses not run at 100 tokens each
        self.assertIn("saving", self.run_measure("--window", "5").stdout)

    def test_review_cost_saving_a_five_lens_day_saves_zero_and_a_day_with_no_lens_prints_none(self):
        self.review(1, "T-0001", ["intent", "adversary", "edge", "operator", "maintainer"])
        self.review(2, "T-0002", [], tokens=(0, 0, 0, 0), usd=0.0)  # a run that ran no lens: no mean to price a lens by
        days = {r["day"]: r for r in self.rows("--window", "5")["days"]}
        self.assertEqual(days[self.day(1)]["saving_tokens"], 0)
        self.assertIsNone(days[self.day(2)]["saving_tokens"])
        self.assertIsNone(days[self.day(2)]["saving_usd"])
        text = self.run_measure("--window", "5").stdout
        line = next(x for x in text.splitlines() if self.day(2) in x)
        self.assertIn("no saving", line)

    def test_review_cost_an_old_event_without_a_lens_count_is_unknown_not_zero(self):
        self.review(1, "T-0001", None)
        self.review(2, "T-0002", ["intent"], tokens=(100, 0, 0, 0))
        days = {r["day"]: r for r in self.rows("--window", "5")["days"]}
        self.assertIsNone(days[self.day(1)]["lenses"])
        self.assertIsNone(days[self.day(1)]["saving_tokens"])
        self.assertEqual(days[self.day(1)]["runs"], 1)
        self.assertEqual(days[self.day(2)]["lenses"], 1)
        self.assertIn("unknown", self.run_measure("--window", "5").stdout)

    def test_review_cost_junk_fields_are_skipped_not_fatal(self):
        self.review(1, "T-0001", ["intent"], tokens=("x", None, True, -5), usd="free")
        self.review(1, "T-0002", "all of them", tokens=(10, 0, 0, 0))
        got = self.rows("--window", "5")
        self.assertEqual(got["days"][0]["tokens"], 10)
        self.assertEqual(self.run_measure("--window", "5").returncode, 0)

    def test_review_cost_reads_what_the_audit_writer_writes(self):
        with c.lock(self.p.dir):
            c.log_event(self.p, "review_cost", task="T-0001", data={
                "model": "sonnet", "lenses": ["intent", "edge"], "usd": 0.25, "input": 10, "output": 5,
                "cache_read": 3, "cache_creation": 2})
        d = self.rows()["days"]
        self.assertEqual((len(d), d[0]["lenses"], d[0]["tokens"], d[0]["usd"]), (1, 2, 20, 0.25))
