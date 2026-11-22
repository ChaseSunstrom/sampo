"""T-5710: a feature that declared a real-use signal, landed 7 or more days ago and shows no real use in the last 14 days is
one 'Wire or retire: X' capture a day, worst first; a used feature is silent; a calling card counts only its real calls,
and a calling card counts only its real calls: a row whose via is bench or test is not real, and a row with no via is unknown, so not real either."""
import json
import os
import time
from unittest import mock

from test_outcomes import Base  # first: it puts plugin/lib on the path

from sampo import core as c
from sampo import callcards
from sampo import deadfeatures as d


class Dead(Base):
    def feature(self, signal, days=9):
        tid = self.done("Add the widget", "FEATURE", "true", "--outcome", signal)
        self.age(tid, days)
        return tid

    def briefs(self):
        return c.load_briefs(self.p)

    def hit(self, extra=()):
        return d.hit(self.p, self.briefs(), time.time(), extra)

    def test_a_silent_feature_gets_one_wire_or_retire_capture(self):
        tid = self.feature("file:widget.out")
        hit = self.hit()
        self.assertIsNotNone(hit)
        self.assertIn(tid, hit)
        self.assertEqual(hit.action[1:4], ["capture", "--source", "discovered"])
        self.assertTrue(hit.action[-1].startswith(f"Wire or retire: {tid} "))

    def test_a_used_feature_is_silent(self):
        self.feature("file:widget.out")
        with open(os.path.join(self.repo, "widget.out"), "w") as f:
            f.write("x\n")
        self.assertIsNone(self.hit())

    def test_a_young_feature_is_not_judged(self):
        self.feature("file:widget.out", days=3)
        self.assertIsNone(self.hit())

    def test_a_signal_that_cannot_be_read_is_never_called_dead(self):
        self.feature("gibberish")
        self.assertIsNone(self.hit())

    def test_one_filing_a_day_and_a_filed_feature_is_not_filed_again(self):
        tid = self.feature("file:widget.out")
        self.fm("capture", f"Wire or retire: {tid} Add the widget: no real use in 14 days", "--source", "discovered")
        self.assertEqual(d.dead_rows(self.p, self.briefs(), time.time()), [])
        self.assertIsNone(self.hit())

    def test_the_oldest_silent_feature_is_named_first(self):
        newer = self.feature("file:a.out", days=9)
        oldest = self.feature("file:b.out", days=20)
        rows = d.dead_rows(self.p, self.briefs(), time.time())
        self.assertEqual([r["task"] for r in rows], [oldest, newer])
        self.assertIn(oldest, self.hit())

    def test_rank_orders_by_age_then_per_turn_cost_then_task(self):
        rows = [{"task": "T-0002", "age_days": 9, "cost": None}, {"task": "T-0003", "age_days": 9, "cost": 4000},
                {"task": "T-0001", "age_days": 20, "cost": None}, {"task": "T-0004", "age_days": 9, "cost": None}]
        self.assertEqual([r["task"] for r in d.rank(rows)], ["T-0003", "T-0001", "T-0002", "T-0004"])

    def test_three_hundred_silent_features_still_make_one_hit(self):
        extra = [(f"seed:x{i}", f"feature {i}", "file:nope.out", c.iso(time.time() - 9 * 86400)) for i in range(300)]
        hit = self.hit(extra)
        self.assertIsNotNone(hit)
        self.assertIn("300 silent in all", hit)
        self.assertEqual(len(hit.action), 5)

    def test_a_card_counts_only_its_real_calls_by_via(self):
        now = time.time()
        path = callcards.calls_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)

        def row(at, via=None):
            r = {"at": at, "card": "x", "class": "x", "seconds": 1, "cached": False}
            if via is not None:
                r["via"] = via  # T-5613's row key: cli, smith, harness, bench, test, mcp
            return r

        old = c.iso(now - 2 * 86400)
        rows = [row(old, "bench"), row(old, "test"), row(old, "cli"), row(old, "mcp"), row(old)]
        with open(path, "w") as f:
            f.write("\n".join(json.dumps(r) for r in rows) + "\n")
            f.write("{not json\n")  # a torn line is skipped
            f.write(json.dumps(row(c.iso(now - 30 * 86400), "cli")) + "\n")  # outside the 14-day window
        since = c.iso(now - d.DEAD_DAYS * d.DAY)
        # cli and mcp are real; bench and test are not; a row with no via is unknown, so not real either
        self.assertEqual(d.real_card_calls(since), 2)

    def test_a_call_through_record_call_carries_its_via_and_only_a_real_one_counts(self):
        """T-5613 made _record_call write a via (cli unless the process names itself): a plain call is real, a call under
        SAMPO_CARD_ORIGIN=test or bench is not, and a row with no via (an old one) is unknown, so not real."""
        path = callcards.calls_path()
        since = c.iso(time.time() - d.DEAD_DAYS * d.DAY)
        env = {k: v for k, v in os.environ.items() if k not in ("SAMPO_CARD_ORIGIN", "FOREMAN_CARD_ORIGIN")}
        with mock.patch.dict(os.environ, env, clear=True):
            callcards._record_call("x", {"status": "applied", "cached": False}, 1.0)
        with open(path) as f:
            self.assertEqual([json.loads(line)["via"] for line in f], ["cli"])
        self.assertEqual(d.real_card_calls(since), 1)
        for origin in ("test", "bench"):
            with mock.patch.dict(os.environ, {"SAMPO_CARD_ORIGIN": origin}):
                callcards._record_call("x", {"status": "applied", "cached": False}, 1.0)
        self.assertEqual(d.real_card_calls(since), 1)  # the test and bench rows added none
        with open(path, "a") as f:
            f.write(json.dumps({"at": c.now(), "card": "x", "class": "pass", "seconds": 1, "cached": False}) + "\n")
        self.assertEqual(d.real_card_calls(since), 1)  # no via: unknown

    def test_the_hit_names_the_window_it_judged_for_a_young_feature(self):
        self.feature("file:widget.out", days=9)
        hit = self.hit()
        self.assertIn("no real use in 9 days", hit)
        self.assertIn("no real use in 9 days", hit.action[-1])
        self.assertNotIn("14 days", hit.action[-1])

    def test_an_old_feature_is_judged_over_the_fourteen_day_window(self):
        self.feature("file:widget.out", days=20)
        hit = self.hit()
        self.assertIn("no real use in 14 days", hit)
        self.assertIn("no real use in 14 days", hit.action[-1])

    def test_an_archived_filing_is_not_filed_again(self):
        tid = self.feature("file:widget.out")
        self.fm("capture", f"Wire or retire: {tid} Add the widget: no real use in 14 days", "--source", "discovered")
        filed = [b for b in self.briefs() if str(b.title).startswith(d.FILED)]
        self.assertEqual(len(filed), 1)
        folder = os.path.join(self.p.dir, "archive", "2026-11")
        os.makedirs(folder, exist_ok=True)
        os.replace(filed[0].path, os.path.join(folder, os.path.basename(filed[0].path)))
        self.assertEqual(d.dead_rows(self.p, self.briefs(), time.time()), [])  # the filing still counts, archived or not
        self.assertIsNone(self.hit())

    def test_the_call_cards_feature_is_judged_on_its_real_calls(self):
        now = time.time()
        seed = (d.CARD_ID, "call cards are run on a real task", "verb:card", c.iso(now - 9 * 86400))
        self.assertIn(d.CARD_ID, [r["task"] for r in d.dead_rows(self.p, self.briefs(), now, [seed])])
        path = callcards.calls_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(json.dumps({"at": c.iso(now - 86400), "card": "x", "class": "x", "seconds": 1,
                                "cached": False, "via": "cli"}) + "\n")
        self.assertNotIn(d.CARD_ID, [r["task"] for r in d.dead_rows(self.p, self.briefs(), now, [seed])])

    def test_selfcheck_files_one_wire_or_retire_capture_and_is_silent_for_a_used_feature(self):
        from sampo import selfcheck
        self.assertIn(selfcheck.dead_feature, selfcheck.CHECKS)
        tid = self.feature("file:widget.out")
        spawned = []

        def spawn(p, argv):
            spawned.append(argv)
            return 1

        out = selfcheck.act(self.p, self.briefs(), time.time(), checks=(selfcheck.dead_feature,), spawn=spawn,
                            tell=lambda p, text: None)
        self.assertEqual([k for k, kind, _ in out], ["dead_feature"])
        self.assertEqual(len(spawned), 1)
        self.assertEqual(spawned[0][1:4], ["capture", "--source", "discovered"])
        self.assertIn(f"Wire or retire: {tid}", spawned[0][-1])
        with open(os.path.join(self.repo, "widget.out"), "w") as f:
            f.write("x\n")
        self.assertEqual(selfcheck.act(self.p, self.briefs(), time.time(), checks=(selfcheck.dead_feature,),
                                       spawn=spawn, tell=lambda p, text: None), [])
