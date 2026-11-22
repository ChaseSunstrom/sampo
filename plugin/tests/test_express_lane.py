"""T-5023: an express lane (PERFORMANCE, SECURITY or urgent) named in a land's --first queue trains next and alone, a
batch of one, ahead of the ordinary lanes queued before it. land_key already sorted such lanes first, but a --first queue
is FIFO and a batch of 20 carried them with the ordinary lanes' gate. Without --first the order is unchanged."""
# map-links: plugin/lib/sampo/train.py
import os
import sys
import types
import unittest

from test_lane_land import CLEAN_CMD, LandCase

from sampo import core as c
from sampo import train


def brief(**kw):
    return types.SimpleNamespace(**{"type": "FEATURE", "priority": "normal", "meta": {}, **kw})


class IsExpress(unittest.TestCase):
    def test_performance_security_and_urgent_lanes_are_express_the_rest_are_not(self):
        self.assertTrue(train.is_express(brief(type="PERFORMANCE")))
        self.assertTrue(train.is_express(brief(type="SECURITY")))
        self.assertTrue(train.is_express(brief(priority="urgent")))
        self.assertFalse(train.is_express(brief()))
        self.assertFalse(train.is_express(brief(type="FIX", meta={"source": "user"})))


class ExpressBase(LandCase):
    def made(self, n=4):
        ids = [self.lane(i, f"{i}.txt", when="2026-01-01T00:00:00") for i in range(1, n + 1)]
        self.audited(*ids)
        for tid in ids:
            self.returned(tid, f"Lane {tid}")
        return ids

    def queue(self, *ids):
        """--first ids while a land runs: queued in order, nothing trained here."""
        p = c.find_project(self.repo)
        with c.lock(os.path.join(p.dir, "land"), timeout=1):
            for tid in ids:
                self.land("--first", tid)


class ExpressLand(ExpressBase):
    def test_a_performance_lane_queued_last_trains_first_and_alone(self):
        ids = self.made(3)
        self.fm("task", "set", ids[2], "type=PERFORMANCE")
        self.queue(ids[0], ids[1], ids[2])
        r = self.land("--batch", "3")
        self.assertEqual(self.trains(r), [ids[2], f"{ids[0]}, {ids[1]}"], r.stderr)
        self.assertEqual([self.brief(t).status for t in ids], ["done"] * 3, r.stdout + r.stderr)

    def test_security_and_urgent_lanes_each_train_alone_before_the_ordinary_ones(self):
        ids = self.made(4)
        self.fm("task", "set", ids[1], "type=SECURITY")
        self.fm("task", "set", ids[3], "priority=urgent")
        self.queue(ids[0], ids[1], ids[2], ids[3])
        r = self.land("--batch", "4")
        self.assertEqual(self.trains(r), [ids[1], ids[3], f"{ids[0]}, {ids[2]}"], r.stderr)

    def test_an_express_lane_still_trains_ahead_of_the_ordinary_ones_when_the_project_is_live(self):
        # T-5444: live mode trains every ready lane as one train; T-5857: an express lane at the head leads that
        # train (merged first) instead of leaving the ordinary lanes for a train of their own
        ids = self.made(3)
        self.fm("lane", "mode", "live")
        self.fm("task", "set", ids[2], "type=PERFORMANCE")
        self.queue(ids[0], ids[1], ids[2])
        r = self.land("--batch", "3")
        self.assertEqual(self.trains(r), [f"{ids[2]}, {ids[0]}, {ids[1]}"], r.stderr)

    def test_t5848_live_express_lanes_past_the_default_batch_train_as_one_train(self):
        # T-5848: no --batch given: the default batch (TRAIN_BATCH, 8) does not cap the leading express lanes;
        # T-5857: nor the ordinary lanes riding behind them
        ids = self.made(c.TRAIN_BATCH + 2)
        self.fm("lane", "mode", "live")
        express = ids[:c.TRAIN_BATCH + 1]
        for t in express:
            self.fm("task", "set", t, "type=PERFORMANCE")
        self.queue(*express)
        r = self.land()
        self.assertEqual(self.trains(r), [", ".join(express + ids[-1:])], r.stderr)

    def test_t5857_live_one_train_the_express_pair_leads_the_ordinary_lanes(self):
        ids = self.made(5)
        self.fm("lane", "mode", "live")
        self.fm("task", "set", ids[3], "type=PERFORMANCE")
        self.fm("task", "set", ids[4], "type=SECURITY")
        self.queue(ids[3], ids[4])
        r = self.land()
        self.assertEqual(self.trains(r), [", ".join([ids[3], ids[4], ids[0], ids[1], ids[2]])], r.stderr)
        self.assertIn("train 1 carries 2 express and 3 ordinary lanes", r.stderr)
        self.assertEqual([self.brief(t).status for t in ids], ["done"] * 5, r.stdout + r.stderr)

    def test_t5857_live_one_train_an_explicit_batch_still_caps_it(self):
        ids = self.made(5)
        self.fm("lane", "mode", "live")
        self.fm("task", "set", ids[3], "type=PERFORMANCE")
        self.fm("task", "set", ids[4], "type=SECURITY")
        self.queue(ids[3], ids[4])
        r = self.land("--batch", "3")
        self.assertEqual(self.trains(r)[0], f"{ids[3]}, {ids[4]}, {ids[0]}", r.stderr)

    def test_t5848_gated_express_lanes_still_train_alone_with_no_batch_given(self):
        ids = self.made(3)
        for t in ids[:2]:
            self.fm("task", "set", t, "type=PERFORMANCE")
        self.queue(*ids[:2])
        r = self.land()
        self.assertEqual(self.trains(r), [ids[0], ids[1], ids[2]], r.stderr)

    def test_without_first_the_batch_is_unchanged(self):
        ids = self.made(3)
        self.fm("task", "set", ids[2], "type=PERFORMANCE")
        r = self.land("--batch", "3")
        self.assertEqual(self.trains(r), [f"{ids[2]}, {ids[0]}, {ids[1]}"], r.stderr)

    def test_an_express_lane_held_by_the_recheck_and_readmitted_still_trains_alone_ahead_of_the_rest(self):
        x, e, a, b, d = self.made(5)
        self.fm("task", "set", x, "type=PERFORMANCE")
        self.fm("task", "set", e, "type=SECURITY")
        self.queue(x, e)
        late, m1, m2 = os.path.join(self.tmp, "lane2"), os.path.join(self.tmp, "m1"), os.path.join(self.tmp, "m2")
        script = os.path.join(self.tmp, "green.py")
        with open(script, "w") as f:  # a clean affected run at e's tip, dated now
            f.write("import sys, time\nsys.path.insert(0, %r)\nfrom sampo import core as c\n"
                    "p = c.find_project(%r)\nb = c.find_brief(p, %r)\n"
                    "b.add_evidence(c.AFFECTED_LABEL + %r, '✓ 10 tests', step=1, ran=True, "
                    "ts=time.strftime('%%Y-%%m-%%dT%%H:%%M:%%SZ', time.gmtime()))\nc.save_brief(p, b)\n"
                    % (os.path.dirname(os.path.dirname(c.__file__)), self.repo, e, CLEAN_CMD))
        # gate of train 1: e gets a commit with no check (held at train 2's start); gate of train 2: its check is green
        self.fm("check", "add", f"if [ ! -e {m1} ]; then touch {m1}; git -C {late} commit -q --allow-empty -m late; "
                f"elif [ ! -e {m2} ]; then touch {m2}; {sys.executable} {script}; fi")
        r = self.land("--batch", "2", check=False)
        self.assertEqual(self.trains(r), [x, f"{a}, {b}", e, d], r.stderr)

    def test_an_ordinary_first_queue_still_trains_as_one_batch(self):
        ids = self.made(3)
        self.queue(ids[2], ids[0])
        r = self.land("--batch", "3")
        self.assertEqual(self.trains(r), [f"{ids[2]}, {ids[0]}, {ids[1]}"], r.stderr)

    def test_an_express_train_that_stops_red_trains_nothing_after_it(self):
        ids = self.made(3)
        self.fm("task", "set", ids[2], "type=PERFORMANCE")
        self.queue(ids[2])
        self.fm("check", "add", "exit 1")  # the gate of its train is red: the land stops there
        r = self.land("--batch", "3", check=False)
        self.assertEqual(self.trains(r), [ids[2]], r.stderr)
        self.assertEqual([self.brief(t).status != "done" for t in ids], [True] * 3, r.stdout + r.stderr)

    def test_an_express_lane_parked_on_a_conflict_trains_alone_when_its_tip_moves_past_it(self):
        # T-4013's re-entry (`again`): a --first lane parked on a conflict rejoins the queue once its tip has moved
        with open(os.path.join(self.repo, "same.txt"), "w") as f:
            f.write("main\n")
        a = self.lane(1, "same.txt", "lane\n", when="2026-01-01T00:00:00")
        self.git("add", "same.txt")
        self.git("commit", "-qm", "main moves")
        self.fm("task", "set", a, "type=PERFORMANCE")
        one, fix = os.path.join(self.tmp, "lane1"), os.path.join(self.tmp, "resolve.sh")
        with open(fix, "w") as f:  # b's gate resolves a's conflict while the land runs
            f.write(f"printf 'main\\n' > {one}/same.txt; GIT_AUTHOR_DATE=2026-01-01T00:00:00 "
                    f"GIT_COMMITTER_DATE=2026-01-01T00:00:00 git -C {one} commit -qam resolve; true\n")
        b = self.lane(2, "b.txt", ac=f"sh {fix}", when="2026-01-01T00:00:00")
        c3, d = (self.lane(n, f"{n}.txt", when="2026-01-01T00:00:00") for n in (3, 4))
        self.audited(a, b, c3, d)
        for tid in (a, b, c3, d):
            self.returned(tid, f"Lane {tid}")
        r = self.land("--first", a, "--batch", "2")
        self.assertEqual(self.trains(r), [f"{b}, {c3}", a, d], r.stderr)


class ExpressRestart(ExpressBase):
    """A restart (the train changed Sampo's own modules) hands the rest of the queue to a fresh land through the --first
    file: only the lanes that were express from --first stay alone, never one that merely sat in the queue."""
    def setUp(self):
        super().setUp()
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.repo)
        self.p = c.find_project(self.repo)

    def restarted(self, first, batch=1):
        """In process: --first `first` queued, one train, the code stamp changes: the land restarts (its stderr)."""
        import contextlib
        import io
        from unittest import mock
        err, seq = io.StringIO(), []
        train.first_ids(self.p, first)
        with mock.patch("sampo.train._code_stamp", side_effect=lambda: "new" if seq.append(0) or len(seq) > 1 else "old"), \
                contextlib.redirect_stderr(err):
            got = train.land(self.p, self.repo, batch)
        self.assertTrue(got["restart"], err.getvalue())
        return err.getvalue()

    def test_a_lane_that_only_waited_in_the_queue_is_not_promoted_by_the_restart(self):
        o1, p, o2, o3 = self.made(4)
        self.fm("task", "set", p, "type=PERFORMANCE")  # never named --first
        self.restarted([o1])  # train 1: o1; the restart queue is p, o2, o3
        r = self.land("--batch", "3")  # the successor: p is just the first of one batch
        self.assertEqual(self.trains(r), [f"{p}, {o2}, {o3}"], r.stderr)
        self.assertNotIn("is express", r.stderr)

    def test_a_lane_express_from_first_stays_alone_after_the_restart(self):
        o1, e1, e2, o2 = self.made(4)
        self.fm("task", "set", e1, "type=SECURITY")
        self.fm("task", "set", e2, "type=PERFORMANCE")
        self.restarted([o1, e1, e2])  # train 1: e1 (express leads, alone); the queue left is e2, o1, o2
        r = self.land("--batch", "3")
        self.assertEqual(self.trains(r), [e2, f"{o1}, {o2}"], r.stderr)

    def test_the_carry_is_taken_by_the_successor_and_not_kept_for_a_later_land(self):
        o1, p, o2 = self.made(3)
        self.fm("task", "set", p, "type=PERFORMANCE")
        self.restarted([o1], batch=1)
        self.land("--batch", "3")
        self.assertEqual(train._restart_carry(self.p), {"ready": [], "alone": []})


if __name__ == "__main__":
    unittest.main()
