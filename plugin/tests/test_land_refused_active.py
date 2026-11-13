"""T-4526: a land refused because a task is active in the main checkout is written to the state's land.log, and sampo next
names that task as the one thing holding every finished lane (2026-11-12: 15 accepted lanes waited 2 h while the refusal
went unread and next kept saying to run the land)."""
# map-links: plugin/lib/sampo/train.py, plugin/lib/sampo/core.py
import io
import os
import sys

from test_lane_land import OLD, LandCase
from test_lane_train import first

from sampo import core as c


class RefusedWhileActive(LandCase):
    def busy(self):
        """A task focused in the main checkout: the land refuses while it is active."""
        return first(self.fm("task", "new", "Mine", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true", "--step",
                             "s", "--json", "--focus").stdout)["id"]

    def land_log(self):
        path = os.path.join(c.find_project(self.repo).dir, "land.log")
        return open(path).read() if os.path.exists(path) else ""

    def test_a_refusal_for_an_active_task_is_one_line_in_the_land_log(self):
        self.lane(1, "a.txt", when=OLD)
        busy = self.busy()
        r = self.land(check=False)
        self.assertNotEqual(r.returncode, 0)
        lines = [ln for ln in self.land_log().splitlines() if busy in ln]
        self.assertEqual(len(lines), 1, self.land_log())
        self.assertIn("active in the main checkout", lines[0])
        self.assertIn(f"sampo task set {busy} status=planned", lines[0])
        self.land(check=False)
        self.assertEqual(len([ln for ln in self.land_log().splitlines() if busy in ln]), 2,
                         "each refusal appends its own line")

    def test_next_names_the_active_task_as_what_holds_every_waiting_lane(self):
        self.lane(1, "a.txt", when=OLD)
        self.lane(2, "b.txt", when=OLD)
        busy = self.busy()
        out = self.fm("next").stdout
        self.assertIn(f"{busy} is active in the main checkout and holds the land of 2 finished lane(s)", out)
        self.assertIn(f"finish {busy}, or sampo task set {busy} status=planned", out)
        self.assertNotIn("sampo lane merge", out)
        self.assertNotIn("sampo lane land (2", out, "the hint that the land cannot run")
        self.fm("task", "set", busy, "status=planned")
        out = self.fm("next").stdout
        self.assertIn("sampo lane land (2 lanes", out, "set planned, the land is offered again")
        self.assertNotIn(f"{busy} is active in the main checkout", out)

    def test_a_land_under_way_still_says_so_beside_an_active_task(self):
        # a land that started before the task was focused takes these lanes: the rider does not name the task
        self.lane(1, "a.txt", when=OLD)
        busy = self.busy()
        with c.lock(os.path.join(c.find_project(self.repo).dir, "land")):
            out = self.fm("next").stdout
        self.assertIn("a land is under way: it trains these 1 lanes", out)
        self.assertNotIn(f"{busy} is active in the main checkout", out)

    def test_a_detached_land_writes_no_second_line_for_its_refusal(self):
        # the detached land's stderr is land.log already: _note_refusal leaves it alone there
        from sampo import train
        p, saved = c.find_project(self.repo), sys.stderr
        sys.stderr = train._Stamped(io.StringIO())
        try:
            train._note_refusal(p, "T-0009 is active in the main checkout")
        finally:
            sys.stderr = saved
        self.assertNotIn("T-0009", self.land_log(), "no line from the detached land itself")
        train._note_refusal(p, "T-0009 is active in the main checkout")
        self.assertIn("T-0009", self.land_log(), "a foreground land writes its refusal")
