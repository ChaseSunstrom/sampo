#!/usr/bin/env python3
# T-4510: a clause of the user's request that a plan put under Non-goals is queued at the close and said, never dropped
import json

from helpers import ForemanTestCase  # noqa: F401  (puts plugin/lib on sys.path)
from sampo import cli
from sampo import core as c

RAW = "Show the map as flat tiles.\nAdd bridges and tunnels to the map."
GOAL = "- Bridges and tunnels on the map are a later task."
LENS = ["--lens", "intent: a deferred clause is queued and said", "--lens", "edge: an empty map closes too",
        "--lens", "adversary: a second close captures nothing"]


class NonGoalCarry(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)

    def plan(self, raw=RAW, non_goals=GOAL, ac="the map shows flat tiles :: true"):
        t = json.loads(self.fm("task", "new", "Flat map", "--type", "FEATURE", "--tier", "M", "--raw", raw,
                               "--ac", ac, "--step", "draw the tiles",
                               "--interpretation", "Reading 1: draw the map as flat tiles. Reading 2: draw it in 3D.",
                               "--approach", "1) draw flat tiles; likely outcome: the map shows tiles. 2) use what exists: "
                                             "reuse the tile view; fails if: it can't take flat tiles. 3) do less: leave "
                                             "the map as it is; likely outcome: nothing shows. Choice 1 (80%): the "
                                             "smallest change that meets the request.",
                               "--json").stdout)["id"]
        self.fm("task", "set", t, "--section", "Non-goals", "--text", non_goals)
        self.fm("focus", t)
        return t

    def close(self, t):
        return self.fm("task", "finish", t, "--run", "true", "--audit", "self check", "--docs", "none: test only",
                       "--lesson", "none: test only", *LENS)

    def carried(self, t):
        return [x for x in c.load_briefs(self.p) if x.meta.get("carried_from") == t]

    def test_a_non_goal_that_answered_a_clause_is_queued_and_said_at_the_close(self):
        t = self.plan()
        r = self.close(t)
        self.assertEqual(c.find_brief(self.p, t).status, "done", r.stdout + r.stderr)
        (n,) = self.carried(t)
        self.assertIn(n.id, r.stdout)
        self.assertIn("Left out, queued next", r.stdout)
        self.assertEqual(n.meta.get("source"), "followup")
        self.assertIn("Add bridges and tunnels to the map", n.section("Raw request"))
        self.assertIn(t, n.title)

    def test_a_second_capture_for_the_same_line_is_not_made(self):
        t = self.plan()
        self.close(t)
        (n,) = self.carried(t)
        self.assertEqual(cli._carry_left_out(self.p, c.find_brief(self.p, t)), [])
        self.assertEqual([x.id for x in self.carried(t)], [n.id])

    def test_users_skip_non_goal_is_not_carried(self):
        t = self.plan(raw="Show the map as flat tiles.\nSKIP: bridges and tunnels on the map",
                      non_goals="- bridges and tunnels on the map")
        r = self.close(t)
        self.assertEqual(c.find_brief(self.p, t).status, "done", r.stdout + r.stderr)
        self.assertEqual(self.carried(t), [])
        self.assertNotIn("Left out", r.stdout)

    def test_users_never_non_goal_is_not_carried(self):
        t = self.plan(raw="Show the map as flat tiles.\nNEVER: draw bridges and tunnels on the map",
                      non_goals="- draw bridges and tunnels on the map")
        r = self.close(t)
        self.assertEqual(c.find_brief(self.p, t).status, "done", r.stdout + r.stderr)
        self.assertEqual(self.carried(t), [])

    def test_a_non_goal_that_echoes_no_clause_is_not_carried(self):
        t = self.plan(non_goals="- Nothing else is changed in the legend.",
                      ac="bridges and tunnels show on the map :: true")  # the clause has its own criterion
        r = self.close(t)
        self.assertEqual(c.find_brief(self.p, t).status, "done", r.stdout + r.stderr)
        self.assertEqual(self.carried(t), [])


if __name__ == "__main__":
    import unittest
    unittest.main()
