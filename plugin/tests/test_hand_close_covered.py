# map-links: plugin/lib/sampo/cli.py, plugin/lib/sampo/train.py
"""T-4705: a close of a lane a merge train landed and gated reads the train's pass as its affected run when the files
that task changed (and the tests linked to them) are byte-identical between the train's tip and now, however many
commits landed on the trunk since. T-4684's hand close, minutes after its train, re-ran 559 modules for 1630 s because
two small unrelated commits had changed the whole code tree, which the pass was keyed on; the train's own closes of the
same batch read 13 s. No train's pass, a red or foreign-environment one, a lane merged by hand, and a later change to
one of the task's files or tests all still run the tests."""
import os
import re
import unittest

from helpers import ForemanTestCase

from sampo import cli
from sampo import core as c
from test_lane_train import AffectedBase, OK

STEPS = re.compile(r"Close steps \(([\d.]+) s\): ([^\n]*)")


class HandClose(AffectedBase):
    def finish(self, tid):
        r = self.fm("task", "finish", tid, "--audit", "self", "--run", "true", check=False, env={"FOREMAN_SENTINEL": "off"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout

    def suite_runs(self):
        """The suite's runs in the main checkout (a close's), not in the train's or a probe's scratch tree."""
        with open(self.suite) as f:
            return sum(ln.split(" ", 1)[0] == os.path.basename(self.repo) for ln in f)

    def commit(self, rel, text, msg="later"):
        with open(os.path.join(self.repo, rel), "w") as f:
            f.write(text)
        self.git("add", rel)
        self.git("commit", "-qm", msg)

    def affected_note(self, out):
        m = STEPS.search(out)
        self.assertTrue(m, out)
        got = re.search(r"affected [\d.]+ s(?: \(([^)]*)\))?", m.group(2))
        self.assertTrue(got, out)
        return got.group(1)

    def landed(self):
        """Lane one (a test module) and lane two (a code file) land in one train; the gate ran once."""
        a, b = self.lane(1, "test_a.py", OK.format("A")), self.lane(2, "plugin/newmod.py", "X = 1\n")
        self.train(a, b)
        self.assertTrue(self.gate_args(), "the train's gate ran the suite")
        return a, b

    def test_a_hand_close_after_unrelated_later_commits_reads_the_train_pass_as_covered(self):
        a, b = self.landed()
        self.commit("later.txt", "an unrelated commit\n")
        self.commit("docs-note.md", "a docs move\n", "docs")
        before = self.suite_runs()
        out = self.finish(a)
        self.assertIn("covered by the gates' pass", out)
        self.assertEqual(self.suite_runs(), before, "the linked tests ran again:\n" + out)
        self.assertEqual(self.affected_note(out), "covered by train of " + f"{a} {b}")

    def test_a_later_change_to_the_tasks_own_test_runs_the_tests_again(self):
        a, _ = self.landed()
        self.commit("test_a.py", OK.format("A") + "# edited after the train\n")
        before = self.suite_runs()
        out = self.finish(a)
        self.assertGreater(self.suite_runs(), before, out)
        self.assertTrue(self.affected_note(out).startswith("ran"), out)  # T-4822: "ran 1 of 3": the edited module only

    def test_a_later_change_to_a_file_the_task_changed_runs_the_tests_again(self):
        _, b = self.landed()
        self.commit("plugin/newmod.py", "X = 2\n")
        before = self.suite_runs()
        out = self.finish(b)
        self.assertGreater(self.suite_runs(), before, out)
        self.assertNotIn("covered", self.affected_note(out) or "")

    def test_a_lane_merged_outside_a_train_is_not_covered_by_a_train_that_did_not_hold_it(self):
        a, b = self.lane(1, "test_a.py", OK.format("A")), self.lane(2, "test_b.py", OK.format("B"))
        self.train(a)
        self.git("merge", "-q", "--no-edit", "foreman/lane2")  # lane two reached the trunk by hand, after the train
        before = self.suite_runs()
        out = self.finish(b)
        self.assertGreater(self.suite_runs(), before, out)
        self.assertNotIn("covered", self.affected_note(out) or "")

    def test_a_lane_a_later_hand_merge_changed_again_is_not_covered(self):
        """The task landed in a train, then merged again by hand (its newest merge is no train's): no cover."""
        a, _ = self.landed()
        p = c.find_project(self.repo)
        c.log_event(p, "lane_merge", task=a, data={})
        self.commit("later.txt", "an unrelated commit\n")  # the whole code tree moved: only the files' key could still hold
        before = self.suite_runs()
        out = self.finish(a)
        self.assertGreater(self.suite_runs(), before, out)
        self.assertNotIn("covered", self.affected_note(out) or "")


class Cover(AffectedBase):
    """cli._cached_pass with a task's files: what a recorded train row must hold for it to count."""
    def setUp(self):
        super().setUp()
        self.commit_file("test_a.py", OK.format("A"))
        self.first = self.code_tree()

    def commit_file(self, rel, text):
        with open(os.path.join(self.repo, rel), "w") as f:
            f.write(text)
        self.git("add", rel)
        self.git("commit", "-qm", f"write {rel}")

    def code_tree(self):
        return c.code_tree(self.repo, self.git("rev-parse", "HEAD^{tree}"))

    def checks(self):
        return list(c.read_meta(self.p).get("checks") or [])

    def row(self, tid="T-1", ct=None, train=None, exit=0, env=None, merge=True, part=("test_a",)):
        results = [{"cmd": g, "exit": exit} for g in self.checks()]
        results[-1]["affected"] = list(part)
        c.log_event(self.p, "check_run", data={"tree": "0" * 12, "code_tree": ct or self.first, "env": env or c.env_id(),
                                               "train": train or [tid], "results": results})
        if merge:
            c.log_event(self.p, "lane_merge", task=tid, data={"train": train or [tid]})

    def hit(self, files=("test_a.py",), names=("test_a",), tid="T-1", why=None):
        keep = {"task": tid, "files": set(files)}
        snap = c.worktree_tree(self.repo)
        got = cli._cached_pass(self.p, self.checks(), snap[:12], (), c.code_tree(self.repo, snap), list(names), why, keep=keep)
        return got, keep

    def test_the_pass_holds_over_a_later_commit_that_leaves_the_files_alone(self):
        self.row()
        self.commit_file("later.txt", "x\n")
        got, keep = self.hit()
        self.assertTrue(got, "a later commit to other files")
        self.assertEqual(keep["by"], ["T-1"])

    def test_a_changed_file_or_test_voids_it_and_the_miss_names_the_file(self):
        self.row()
        self.commit_file("test_a.py", OK.format("A") + "# changed\n")
        why = []
        got, _ = self.hit(why=why)
        self.assertIsNone(got)
        self.assertIn("test_a.py", why[0])

    def test_no_row_of_a_train_holding_the_task_covers_nothing(self):
        self.row(tid="T-2")
        self.commit_file("later.txt", "x\n")
        self.assertIsNone(self.hit(tid="T-1")[0])

    def test_a_red_or_foreign_env_or_partial_row_covers_nothing(self):
        for kw in ({"exit": 1}, {"env": "elsewhere"}, {"part": ("test_other",)}):
            with self.subTest(kw=kw):
                self.row(**kw)
                self.commit_file("later.txt", c.now())
                self.assertIsNone(self.hit()[0])

    def test_a_row_with_no_merge_record_of_that_train_covers_nothing(self):
        self.row(merge=False)
        self.commit_file("later.txt", "x\n")
        self.assertIsNone(self.hit()[0], "no lane_merge says this task landed in that train")

    def test_a_newer_merge_of_another_kind_voids_it(self):
        self.row()
        c.log_event(self.p, "lane_merge", task="T-1", data={})
        self.commit_file("later.txt", "x\n")
        self.assertIsNone(self.hit()[0])

    def test_a_file_deleted_or_unreadable_since_covers_nothing(self):
        self.row()
        self.git("rm", "-q", "test_a.py")
        self.git("commit", "-qm", "drop it")
        self.assertIsNone(self.hit()[0], "a deleted file is a changed file")

    def test_a_code_tree_git_cannot_read_covers_nothing(self):
        self.row(ct="1" * 40)  # a code tree git no longer has
        self.commit_file("later.txt", "x\n")
        self.assertIsNone(self.hit()[0], "a tree git can't read says nothing")

    def test_an_empty_file_set_covers_nothing(self):
        self.row()
        self.commit_file("later.txt", "x\n")
        self.assertIsNone(self.hit(files=())[0], "nothing to say the task's files held")


if __name__ == "__main__":
    unittest.main()
