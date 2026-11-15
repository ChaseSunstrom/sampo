"""T-4692: a land closes its lanes by task id, so an unrelated task focused in the main checkout does not hold it; only
uncommitted changes that touch a lane's files (or the changelog every close writes) do, and the refusal names the file
(2026-11-14, T-4681: a focus made during a train left two lanes for hand closes)."""
# map-links: plugin/lib/sampo/train.py, plugin/lib/sampo/core.py
import os

from test_lane_land import OLD, LandCase
from test_lane_train import first

from sampo import core as c


class BesideFocus(LandCase):
    def busy(self):
        """A task focused in the main checkout, as a session's own work."""
        return first(self.fm("task", "new", "Mine", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true", "--step",
                             "s", "--json", "--focus").stdout)["id"]

    def land_log(self):
        path = os.path.join(c.find_project(self.repo).dir, "land.log")
        return open(path).read() if os.path.exists(path) else ""

    def two(self):
        a, b = self.lane(1, "a.txt", when=OLD), self.lane(2, "b.txt", when=OLD)
        self.audited(a, b)
        self.returned(a, "Lane one")
        self.returned(b, "Lane two")
        return a, b

    def write(self, name, text="mine\n"):
        with open(os.path.join(self.repo, name), "w") as f:
            f.write(text)

    def test_an_unrelated_focused_task_in_a_clean_checkout_does_not_hold_the_land(self):
        a, b = self.two()
        busy = self.busy()
        out = self.land().stdout
        self.assertEqual((self.brief(a).status, self.brief(b).status), ("done", "done"), out)
        self.assertEqual(self.brief(busy).status, "active", "the focused task is still the one in hand")
        self.assertEqual(getattr(c.active_brief_fast(c.find_project(self.repo)), "id", None), busy)
        self.assertIn("2 closed, 0 left for a hand close", out)
        self.assertEqual(self.land_log(), "", "nothing refused")

    def test_uncommitted_work_that_touches_no_lane_file_is_left_where_it_is(self):
        a, b = self.two()
        busy = self.busy()
        self.write("mine.txt")  # the focused task's own work in progress
        out = self.land().stdout
        self.assertEqual((self.brief(a).status, self.brief(b).status), ("done", "done"), out)
        self.assertEqual(self.git("status", "--porcelain", "-uall", "--", "mine.txt"), "?? mine.txt",
                         "never committed by a lane's close")
        self.assertEqual(self.brief(busy).status, "active")

    def test_uncommitted_changes_to_a_lanes_file_still_hold_and_name_the_file(self):
        a, b = self.two()
        busy = self.busy()
        self.write("b.txt")  # the file lane two adds
        r = self.land(check=False)
        self.assertNotEqual(r.returncode, 0, r.stdout)
        text = r.stdout + r.stderr
        self.assertIn(f"{busy} is active in the main checkout", text)
        self.assertIn("b.txt", text)
        self.assertIn("uncommitted", text)
        self.assertNotEqual(self.brief(a).status, "done", "nothing landed")
        self.assertIn("b.txt", self.land_log(), "the refusal is in the land log, with the file")

    def test_an_uncommitted_changelog_holds_the_land_because_every_close_writes_it(self):
        self.two()
        busy = self.busy()
        with open(self.changelog, "a") as f:
            f.write("- the focused task's own line (T-9990).\n")
        r = self.land(check=False)
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn("CHANGELOG.md", r.stdout + r.stderr)
        self.assertIn(busy, r.stdout + r.stderr)

    def test_next_holds_the_land_only_for_a_focus_with_uncommitted_lane_files(self):
        self.two()
        busy = self.busy()
        out = self.fm("next").stdout
        self.assertNotIn(f"{busy} is active in the main checkout and holds the land", out)
        self.assertIn("sampo lane land (2 lanes", out)
        self.write("a.txt")
        out = self.fm("next").stdout
        self.assertIn(f"{busy} is active in the main checkout and holds the land of 2 finished lane(s)", out)
        self.assertIn("a.txt", out)
