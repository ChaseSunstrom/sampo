"""T-3484: the exam loop. Each round runs the smith arm on unseen holdout scenarios per local profile, the failure taxonomy
over those answers, and appends one row per failure class to research/local-loop.jsonl. The arm is a stub that writes the
event logs a Smith run would leave; the answers, grade and taxonomy are the real ones."""
import fcntl
import json
import os
from unittest import mock

from helpers import ForemanTestCase  # noqa: F401  (puts plugin/lib on the path)

from sampo import core as c, louhi, models, moonscale

LOOP = [{"event": "action", "action": "edit", "target": "a", "result": "ok"},
        {"event": "error", "text": "fm code: stopped: the model used all 40 turns (--max-steps) before step:2 was done"}]
TIMEOUT = [{"event": "timeout", "text": "no end after 900 s"}]


def stub(events_by_id, calls):
    """Stands in for louhi._smith_arm: records the scenario, writes its event log, returns an answer with no findings."""
    def arm(base, key, work, model, request, events, planner=None, reason=None, small=False, search=None):
        calls.append(key["id"])
        with open(events, "w", encoding="utf-8") as f:
            f.write("".join(json.dumps(e) + "\n" for e in events_by_id.get(key["id"], [])))
        return {"findings": [], "notes": [], "done": False, "cost": 0, "tokens": 0, "error": None,
                "model": model, "planner": None, "reason": reason}
    return arm


class ExamLoop(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)
        self.run = self.fm_json("exam", "gen", "--seed", "3", "--per-kind", "6", "--lang", "python", "--kind", "bug")["run"]
        self.held = [s["id"] for s in louhi.load(self.p, self.run)["scenarios"] if s["split"] == "holdout"]
        self.assertEqual(len(self.held), 2, "per-kind 6 holds out two scenarios")

    def rows(self):
        with open(os.path.join(self.p.root, "research", "local-loop.jsonl"), encoding="utf-8") as f:
            return [json.loads(x) for x in f if x.strip()]

    def test_two_rounds_append_rows_whose_counts_match_the_failure_classes(self):
        evs, calls = {self.held[0]: LOOP, self.held[1]: TIMEOUT}, []
        with mock.patch.object(models, "resolve", lambda m: {"name": m}), \
                mock.patch.object(louhi, "_smith_arm", stub(evs, calls)):
            louhi.exam_loop(self.p, profiles=["fake"], rounds=3, n=1, run=self.run)
        self.assertEqual(calls, self.held, "each round answers the next unseen holdout scenario")
        rows = self.rows()
        for rd, sid in ((1, self.held[0]), (2, self.held[1])):
            got = [r for r in rows if r.get("round") == rd]
            want = {k: louhi.failure_classes(evs[sid]).get(k, 0) for k in louhi.FAILURE_CLASSES}
            self.assertEqual({r["class"]: r["count"] for r in got}, want, f"round {rd}")
            self.assertEqual({r["class"]: r["rate"] for r in got}, {k: float(want[k]) for k in want}, f"round {rd} rates")
            self.assertTrue(all(r["model"] == "fake" and r["ids"] == [sid] and r["n"] == 1 for r in got))
        third = [r for r in rows if r.get("round") == 3]
        self.assertEqual(len(third), 1, "a round with no unseen holdout left is one skipped row")
        self.assertEqual((third[0]["model"], "unseen" in third[0]["skipped"]), ("fake", True))

    def test_a_held_store_skips_the_round_with_a_row_saying_why(self):
        calls = []
        os.makedirs(moonscale._dir(self.p), exist_ok=True)
        fd = os.open(os.path.join(moonscale._dir(self.p), ".funnel.lock"), os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            with mock.patch.object(models, "resolve", lambda m: {"name": m}), \
                    mock.patch.object(louhi, "_smith_arm", stub({}, calls)):
                louhi.exam_loop(self.p, profiles=["fake"], rounds=1, n=1, run=self.run)
        finally:
            os.close(fd)  # the flock goes with the descriptor
        self.assertEqual(calls, [], "a skipped round answers nothing")
        rows = self.rows()
        self.assertEqual([(r["round"], r["model"]) for r in rows], [(1, "*")])
        self.assertIn("funnel", rows[0]["skipped"])
        with mock.patch.object(models, "resolve", lambda m: {"name": m}), \
                mock.patch.object(louhi, "_smith_arm", stub({}, calls)):
            louhi.exam_loop(self.p, profiles=["fake"], rounds=1, n=1, run=self.run)
        self.assertEqual(calls, self.held[:1], "the next round runs once the store is free, under the next round number")
        self.assertEqual({r["round"] for r in self.rows() if "ids" in r}, {2})

    def test_a_profile_that_errors_leaves_its_scenarios_unseen(self):
        calls, down = [], {"bad": True}

        def resolve(m):
            if m == "bad" and down["bad"]:
                raise ValueError("no such profile")
            return {"name": m}
        with mock.patch.object(models, "resolve", resolve), mock.patch.object(louhi, "_smith_arm", stub({}, calls)):
            louhi.exam_loop(self.p, profiles=["bad"], rounds=1, n=1, run=self.run)
            down["bad"] = False
            louhi.exam_loop(self.p, profiles=["bad"], rounds=1, n=1, run=self.run)
        rows = self.rows()
        self.assertEqual(("skipped" in rows[0], rows[0]["model"]), (True, "bad"))
        self.assertEqual(calls, self.held[:1], "the failed round's scenario is answered again, not counted as seen")
        self.assertEqual({r["round"] for r in rows if "ids" in r}, {2})


if __name__ == "__main__":
    import unittest
    unittest.main()
