"""T-5067 (child of T-4307, the land never waits for a ship): a second land starts while the first ship runs. Two lands
run back to back while one slow ship is held at its push: the second land's train merges before that ship ends, and
the trains that landed beside it cost one more push, not one each. The ledger orders each merge before the ship_done
that ends the ship run after it."""
# map-links: plugin/lib/sampo/train.py
import os
import unittest

from test_land_ship import ShipCase, until
from test_lane_land import OLD

from sampo import core as c
from sampo import train


class BackToBack(ShipCase):
    def test_two_lands_back_to_back_while_one_slow_ship_runs_merge_before_it_ends_and_one_more_ship_follows(self):
        self.lanes_ready(1)
        r1 = self.fm("lane", "land", "--batch", "1", env={"SAMPO_LAND_SHIP_WAIT_S": "4"}, timeout=240)
        out1 = r1.stdout + r1.stderr
        self.assertTrue(os.path.exists(os.path.join(self.repo, "a.txt")), out1)  # the first land merged its lane
        self.assertIn("ship still running", out1, "the first land ends while its ship is held at the push")
        until(lambda: len(self.pushes()) == 1, what="the first ship to reach its held push")

        # a second lane becomes ready while that ship still runs, and a second land starts beside it
        self.ready_more(2, "b.txt")
        r2 = self.fm("lane", "land", "--batch", "1", env={"SAMPO_LAND_SHIP_WAIT_S": "4"}, timeout=240)
        out2 = r2.stdout + r2.stderr
        self.assertTrue(os.path.exists(os.path.join(self.repo, "b.txt")), out2)  # merged before the ship ended
        self.assertIn("ship still running", out2, "the second land ends while the first ship still runs")
        self.assertTrue(train._ship_alive(self.p), "the first ship is still running when the second land ends")
        self.assertEqual(len(self.pushes()), 1, "the second land started no ship beside the running one")
        self.assertEqual(self.files() & {"a.txt", "b.txt"}, set(), "the held ship has not pushed either lane yet")

        self.open(1)  # the first ship pushes; the trains that landed meanwhile share one follow-up push
        until(lambda: len(self.pushes()) == 2, what="the one follow-up push")
        self.open(2)
        until(lambda: not train._ship_alive(self.p), what="the ship to finish")

        self.assertEqual(len(self.pushes()), 2, "two lands during one slow ship: one more push, not two")
        self.assertEqual(self.files() & {"a.txt", "b.txt"}, {"a.txt", "b.txt"})
        ships = self.events("ship_done")
        self.assertEqual(len(ships), 2, "one ship_done per push")
        self.assertEqual([e["data"]["exit"] for e in ships], [0, 0])

        # the ledger is append-only: the second merge is logged before the first ship_done, so it landed while that
        # ship was still running
        order = [e["event"] for e in c.ledger_tail(self.p, 400, ["land_train", "ship_done"])]
        trains = [i for i, k in enumerate(order) if k == "land_train"]
        done = [i for i, k in enumerate(order) if k == "ship_done"]
        self.assertEqual(len(trains), 2, order)
        self.assertLess(trains[0], trains[1], order)
        self.assertLess(trains[1], done[0], f"the second merge precedes the first ship's end: {order}")

    def ready_more(self, start, name):
        """One more finished lane from `start`, shaped like lanes_ready's: quiet past OLD, audited, returned."""
        tid = self.lane(start, name, when=OLD)
        self.audited(tid)
        self.returned(tid, f"Lane {tid}")
        return tid


if __name__ == "__main__":
    unittest.main()
