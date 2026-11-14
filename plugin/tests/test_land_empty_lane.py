"""T-4603: `sampo lane land --merged` counted a lane in which nothing was ever built (its tip is an old trunk commit, so an
ancestor of the trunk) as landed work and said 'no close fields on its brief (its builder's report had no CHANGELOG line)'
for a task that was never built (2026-11-13: T-4139, T-4147, T-4152). The sweep now tells an empty lane from landed work:
a lane whose tip (and worktree HEAD) sits on the trunk's own first-parent line with no trunk commit naming its task is
empty. It is named empty, its task stays open, and `--drop-empty` removes the clean empty lanes (the task returns to the
queue); a landed lane, one landed under another task's merge, one fast-forwarded in and a dirty empty lane are never
dropped."""
# map-links: plugin/lib/sampo/train.py
import json
import os
import subprocess
import unittest

from test_lane_land import OLD, LandCase
from test_lane_train import first


class EmptyLane(LandCase):
    def lanes_ready(self, n, bare=()):
        """n lanes each with one commit, audited and reported (`bare`: lane numbers whose builder never reported)."""
        ids = [self.lane(i, f"f{i}.txt", when=OLD) for i in range(1, n + 1)]
        self.audited(*ids)
        for i, tid in enumerate(ids, 1):
            if i not in bare:
                self.returned(tid, f"Lane {i}")
        return ids

    def hand_merge(self, *ns):
        for n in ns:
            self.git("merge", "--no-ff", "-q", "-m", f"merge lane {n}", f"foreman/lane{n}")

    def sweep(self, *a, check=True):
        return self.land("--merged", *a, check=check)

    def done(self, tid):
        return self.brief(tid).status == "done"

    def folder(self, n):
        return os.path.isdir(os.path.join(self.tmp, f"lane{n}"))

    def unbuilt(self, n, dirty=False):
        """A lane made on an old trunk commit with its task and nothing built in it (the three real lanes)."""
        path = os.path.join(self.tmp, f"lane{n}")
        subprocess.run(["git", "-C", self.repo, "worktree", "add", "-q", "-b", f"foreman/lane{n}", path, "HEAD~1"],
                       check=True)
        tid = first(self.fm("task", "new", f"Lane {n}", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true",
                            "--step", "s", "--json", "--focus", cwd=path).stdout)["id"]
        if dirty:
            with open(os.path.join(path, "wip.txt"), "w") as f:
                f.write("work in progress\n")
        return tid

    def branch(self, n):
        return self.git("branch", "--list", f"foreman/lane{n}")

    def test_a_never_built_lane_is_named_empty_and_its_task_stays_open_beside_a_landed_lane(self):
        landed = self.lanes_ready(1)[0]
        empty = self.unbuilt(2)
        self.hand_merge(1)
        r = self.sweep()
        self.assertTrue(self.done(landed), r.stdout + r.stderr)
        self.assertRegex(r.stdout, rf"(?m)^  empty {empty}: its lane is empty, the task was never built")
        self.assertNotIn("close fields", r.stdout, "an unbuilt task is not a landed one with no report")
        self.assertNotRegex(r.stdout, rf"(?m)^  left {empty}")
        self.assertEqual(self.brief(empty).status, "active", "the task stays as it was")
        self.assertTrue(self.folder(2) and self.branch(2), "without the flag only the message changes")
        self.assertIn("--drop-empty", r.stdout, "it offers the drop")
        self.assertNotIn("left active", self.brief(empty).section("Log"))

    def test_an_empty_lane_alone_runs_no_gate_and_closes_nothing(self):
        empty = self.unbuilt(1)
        r = self.sweep()
        self.assertEqual(self.gate_runs(), 0, r.stdout + r.stderr)
        self.assertRegex(r.stdout, rf"(?m)^  empty {empty}: ")
        self.assertFalse(self.done(empty))
        self.assertNotIn("no lane with an open task", r.stdout)

    def test_drop_empty_removes_a_clean_empty_lane_and_returns_its_task_to_the_queue(self):
        landed = self.lanes_ready(1)[0]
        empty = self.unbuilt(2)
        self.hand_merge(1)
        r = self.sweep("--drop-empty")
        self.assertTrue(self.done(landed), r.stdout + r.stderr)
        self.assertFalse(self.folder(2), r.stdout + r.stderr)
        self.assertEqual(self.branch(2), "", "its branch held nothing and goes")
        b = self.brief(empty)
        self.assertEqual(b.status, "planned", "back in the queue, never closed")
        self.assertFalse(b.meta.get("lane"))
        self.assertRegex(r.stdout, rf"(?m)^  dropped {empty}: ")
        again = self.sweep("--drop-empty")
        self.assertNotIn(empty, again.stdout, "a second run finds nothing more to drop")
        self.assertEqual(self.brief(empty).status, "planned")

    def test_drop_empty_keeps_a_dirty_empty_lane_with_its_work(self):
        empty = self.unbuilt(1, dirty=True)
        r = self.sweep("--drop-empty")
        self.assertTrue(self.folder(1) and self.branch(1), r.stdout + r.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "lane1", "wip.txt")), "nothing was discarded")
        self.assertEqual(self.brief(empty).status, "active")
        self.assertRegex(r.stdout, rf"(?m)^  empty {empty}: .*uncommitted")
        self.assertNotRegex(r.stdout, rf"(?m)^  dropped {empty}")

    def test_a_lane_landed_under_another_tasks_merge_is_not_empty_and_never_dropped(self):
        ids = self.lanes_ready(1, bare=(1,))
        self.git("merge", "--no-ff", "-q", "-m", "Merge T-9999 (the batch that carried it)", "foreman/lane1")
        r = self.sweep("--drop-empty")
        self.assertRegex(r.stdout, rf"(?m)^  left {ids[0]}: .*close fields", r.stdout)
        self.assertNotIn("empty", r.stdout.replace("--drop-empty", ""))
        self.assertTrue(self.folder(1))
        self.assertFalse(self.done(ids[0]))

    def test_a_lane_fast_forwarded_in_is_landed_not_empty_by_its_commit_naming_the_task(self):
        tid = self.lanes_ready(1, bare=(1,))[0]
        path = os.path.join(self.tmp, "lane1")
        subprocess.run(["git", "-C", path, "commit", "--amend", "-qm", f"Lane 1 work ({tid})"], check=True,
                       capture_output=True)
        self.git("merge", "--ff-only", "-q", "foreman/lane1")
        r = self.sweep("--drop-empty")
        self.assertRegex(r.stdout, rf"(?m)^  left {tid}: .*close fields", r.stdout)
        self.assertTrue(self.folder(1), "a lane with its work on the trunk is never dropped as empty")
        self.assertNotRegex(r.stdout, rf"(?m)^  (empty|dropped) {tid}")

    def test_a_trunk_commit_naming_a_longer_id_does_not_make_the_lane_landed(self):
        empty = self.unbuilt(1)
        num = empty.split("-")[1]
        with open(os.path.join(self.repo, "other.txt"), "w") as f:
            f.write("x\n")
        self.git("add", "other.txt")
        self.git("commit", "-qm", f"Something else (T-{num}9)")
        r = self.sweep()
        self.assertRegex(r.stdout, rf"(?m)^  empty {empty}: ", r.stdout)

    def test_the_sweep_reads_json_and_the_flag_is_in_the_help(self):
        empty = self.unbuilt(1)
        got = json.loads(self.sweep("--json").stdout)
        self.assertEqual([t for t, _ in got["empty"]], [empty])
        self.assertEqual(got["dropped"], [])
        got = json.loads(self.sweep("--drop-empty", "--json").stdout)
        self.assertEqual(got["dropped"], [empty])
        self.assertIn("--drop-empty", self.fm("lane", "--help").stdout)

    def test_drop_empty_without_merged_is_refused(self):
        r = self.land("--drop-empty", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--merged", r.stderr + r.stdout)


if __name__ == "__main__":
    unittest.main()
