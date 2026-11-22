"""T-5518: one red post-merge record must not stop every land for hours. On 2026-11-21 T-2282's verify named two red
classes; one (EnvClassified) already failed on the merge's first parent, the other (PrivateTerm) was green on a rerun, yet
nothing was excused (the inherited test needed every red class on the parent), the revert conflicted, and 93 lanes waited
about 5 hours with nothing in `sampo next`. So: a class the first parent also fails is excused per class, a class green
alone is a flake per class, a conflicted revert settles itself when its classes pass on trunk now, and a red or conflict
record shows in `sampo next` with the command that settles it. All in temp repos with shell stand-ins for the suite."""
import json
import os
import subprocess
import time
import unittest.mock

from helpers import ForemanTestCase

from sampo import core as c
from sampo import lanes as slanes

first = lambda text: json.JSONDecoder().raw_decode(text)[0]
OLD = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 7200))


class PmSettle(ForemanTestCase):
    # the full suite and the alone rerun, as one script: A is red while base_red.txt exists, B while bad.txt holds a line "a"
    SUITE = ("[ -f base_red.txt ] && echo 'FAIL: t (m.A.t)'; grep -qx a bad.txt 2>/dev/null && echo 'FAIL: t (m.B.t)'; "
             "[ -f base_red.txt ] || grep -qx a bad.txt 2>/dev/null && exit 1; exit 0")

    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)
        for k, v in (("FM_POSTMERGE_CMD", self.SUITE), ("FM_POSTMERGE_RERUN", self.SUITE)):
            os.environ[k] = v
            self.addCleanup(os.environ.pop, k, None)
        self.tid = first(self.fm("task", "new", "Landed", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true",
                                 "--step", "s", "--json").stdout)["id"]

    def git(self, *args):
        return subprocess.run(["git", "-C", self.repo, *args], capture_output=True, text=True).stdout.strip()

    def land(self, name, text=None):
        """A --no-ff merge of one file (bad.txt or base_red.txt hold the red) recorded pending like merge() does."""
        self.git("checkout", "-qb", f"b-{name}")
        with open(os.path.join(self.repo, name), "w") as f:
            f.write(text or name)
        self.git("add", "-A")
        self.git("commit", "-qm", name)
        self.git("checkout", "-q", "-")
        self.git("merge", "-q", "--no-ff", "-m", f"Merge {name}", f"b-{name}")
        sha = self.git("rev-parse", "HEAD")
        with unittest.mock.patch.object(slanes, "pm_spawn"):
            slanes.pm_record(self.p, sha, self.tid, self.repo)
        return sha

    def rec(self, sha):
        return slanes.pm_records(self.p)[sha]

    def conflicted(self, later):
        """bad.txt lands red (class m.B), then an edit on top makes its revert conflict; pm_run leaves the record there."""
        sha = self.land("bad.txt", "a\n")
        with open(os.path.join(self.repo, "bad.txt"), "w") as f:
            f.write(later)
        self.git("commit", "-qam", "edit on top")
        slanes.pm_run(self.p, self.repo)
        return sha

    def test_inherited_per_class_a_red_the_parent_also_fails_is_excused_and_the_rest_blamed(self):
        base = self.land("base_red.txt")
        slanes.pm_set(self.p, base, state="green")  # trunk-red before the merge under test
        sha = self.land("bad.txt", "a\n")
        slanes.pm_run(self.p, self.repo)
        r = self.rec(sha)
        self.assertEqual((r["state"], r["classes"]), ("reverted", ["m.B"]), "only B is the merge's: A was red before it")
        self.assertFalse(os.path.exists(os.path.join(self.repo, "bad.txt")), "reverted")
        self.assertEqual(c.ledger_events(self.p, ["postmerge_inherited"])[0]["data"]["classes"], ["m.A"])
        self.assertIn("m.A", c.find_brief(self.p, self.tid).section("Log"))
        captured = [b for b in c.load_briefs(self.p) if b.id != self.tid and "m.A" in b.section("Raw request")]
        self.assertEqual(len(captured), 1, "the trunk-red class is captured as a task")

    def test_inherited_per_class_captures_the_trunk_red_once(self):
        base = self.land("base_red.txt")
        slanes.pm_set(self.p, base, state="green")
        for name in ("bad.txt", "bad2.txt"):  # two merges meet the same trunk red
            sha = self.land(name, "a\n") if name == "bad.txt" else self.land(name)
            slanes.pm_run(self.p, self.repo)
        again = [b for b in c.load_briefs(self.p, include_archive=True) if b.id != self.tid and "m.A" in b.section("Raw request")]
        self.assertEqual(len(again), 1)

    def test_inherited_per_class_all_red_on_the_parent_is_inherited_as_before(self):
        base = self.land("base_red.txt")
        slanes.pm_set(self.p, base, state="green")
        sha = self.land("other.txt")  # only A is red here, and A is the parent's
        slanes.pm_run(self.p, self.repo)
        r = self.rec(sha)
        self.assertEqual((r["state"], r["classes"]), ("inherited", ["m.A"]))
        self.assertTrue(os.path.exists(os.path.join(self.repo, "other.txt")), "not reverted")

    def test_inherited_per_class_a_class_green_alone_is_a_flake_not_the_merges(self):
        # the full suite names A and B, the alone rerun only B: A is green alone, so B is the one red
        os.environ["FM_POSTMERGE_CMD"] = "echo 'FAIL: t (m.A.t)'; echo 'FAIL: t (m.B.t)'; exit 1"
        os.environ["FM_POSTMERGE_RERUN"] = "echo 'FAIL: t (m.B.t)'; exit 1"
        sha = self.land("bad.txt", "a\n")
        slanes.pm_run(self.p, self.repo)
        self.assertEqual(self.rec(sha)["classes"], ["m.B"])

    def test_inherited_per_class_a_merge_with_only_flaky_classes_stays_green(self):
        os.environ["FM_POSTMERGE_CMD"] = "echo 'FAIL: t (m.A.t)'; echo 'FAIL: t (m.B.t)'; exit 1"
        os.environ["FM_POSTMERGE_RERUN"] = "exit 0"
        sha = self.land("bad.txt", "a\n")
        slanes.pm_run(self.p, self.repo)
        self.assertEqual((self.rec(sha)["state"], self.rec(sha)["flaky"]), ("green", ["m.A", "m.B"]))

    def test_conflict_settles_when_the_blamed_classes_pass_on_head(self):
        sha = self.conflicted("fixed forward\n")  # B no longer fails here: the later edit fixed it
        r = self.rec(sha)
        self.assertEqual(r["state"], "settled", r)
        self.assertIn("fixed forward on " + self.git("rev-parse", "HEAD")[:9], r["note"])
        self.assertIn("settled", c.find_brief(self.p, self.tid).section("Log"))
        slanes.pm_wait(self.p, self.repo)  # nothing stops the next land

    def test_conflict_stays_when_the_blamed_classes_still_fail_on_head(self):
        sha = self.conflicted("a\nlater edit\n")  # B is still red on HEAD
        self.assertEqual(self.rec(sha)["state"], "conflict")
        with self.assertRaises(c.PolicyError):
            slanes.pm_wait(self.p, self.repo)

    def test_conflict_is_retried_once_head_moves_and_not_before(self):
        sha = self.conflicted("a\nlater edit\n")
        with unittest.mock.patch.object(c, "run_command", return_value=(0, "")) as run:
            slanes.pm_run(self.p, self.repo)  # same HEAD: the answer is known, no run
        self.assertEqual((run.call_count, self.rec(sha)["state"]), (0, "conflict"))
        with open(os.path.join(self.repo, "bad.txt"), "w") as f:
            f.write("repaired\n")
        self.git("commit", "-qam", "repair")
        slanes.pm_run(self.p, self.repo)
        self.assertEqual(self.rec(sha)["state"], "settled")

    def test_conflict_settle_leaves_main_untouched(self):
        sha = self.conflicted("fixed forward\n")
        self.assertEqual(self.git("status", "--porcelain", "--untracked-files=no"), "")
        self.assertEqual(self.git("worktree", "list", "--porcelain").count("worktree "), 1, "the scratch tree is gone")

    def test_surfaces_a_conflict_record_with_the_command_that_settles_it(self):
        sha = self.conflicted("a\nlater edit\n")
        self.assertEqual(slanes.pm_stuck_line(self.p), "", "fresh: the revert has just conflicted")
        slanes.pm_set(self.p, sha, red_at=OLD)
        line = slanes.pm_stuck_line(self.p)
        self.assertIn(sha[:9], line)
        self.assertIn(f"sampo lane verify --reset {sha[:9]}", line)
        self.assertIn("m.B", line)
        self.assertIn(self.tid, line)
        self.assertIn(sha[:9], slanes.recovery_line(self.p, c.load_briefs(self.p), False), "it reaches sampo next")

    def test_surfaces_a_red_record_and_drops_it_once_settled(self):
        sha = self.land("bad.txt", "a\n")
        slanes.pm_set(self.p, sha, state="red", classes=["m.B"], red_at=OLD)
        line = slanes.pm_stuck_line(self.p)
        self.assertIn("red", line)
        self.assertNotIn("--reset", line, "a red record is still on trunk: --reset would call it reverted")
        self.assertIn("sampo lane verify", line, "it retries the revert")
        slanes.pm_set(self.p, sha, state="settled")
        self.assertEqual(slanes.pm_stuck_line(self.p), "")

    def test_conflict_only_record_spawns_a_pass_until_its_head_was_checked(self):
        sha = self.conflicted("a\nlater edit\n")  # settle ran once on this HEAD and stayed red
        head = self.git("rev-parse", "HEAD")
        self.assertEqual(self.rec(sha)["settle_head"], head)
        with unittest.mock.patch.object(slanes, "pm_spawn") as spawn:
            with self.assertRaises(c.PolicyError) as got:
                slanes.pm_wait(self.p, self.repo)
            slanes.pm_after_train(self.p, self.repo)
            from sampo import train
            train.start_verify(self.p, self.repo, [False])
        self.assertEqual(spawn.call_count, 0, "checked at this HEAD: nothing new to learn")
        self.assertNotIn("reverted automatically", str(got.exception))
        self.assertIn("settles itself", str(got.exception))
        slanes.pm_set(self.p, sha, settle_head="0" * 40)  # trunk moved since
        with unittest.mock.patch.object(slanes, "pm_spawn") as spawn:
            with self.assertRaises(c.PolicyError):
                slanes.pm_wait(self.p, self.repo)
            self.assertEqual(spawn.call_count, 1, "_pm_check")
            slanes.pm_after_train(self.p, self.repo)
            self.assertEqual(spawn.call_count, 2, "pm_after_train")
            train.start_verify(self.p, self.repo, [False])
            self.assertEqual(spawn.call_count, 3, "start_verify")

    def test_inherited_per_class_a_class_that_did_not_finish_alone_is_not_excused_as_flaky(self):
        os.environ["FM_POSTMERGE_CMD"] = "echo 'FAIL: t (m.A.t)'; echo 'FAIL: t (m.B.t)'; exit 1"
        for ran in ("echo 'FAIL: t (m.A.t)'; echo 'run.py: m.B still running after 900s: killed (T-1808)'; exit 1",
                    "echo 'FAIL: t (m.A.t)'; echo 'run.py: not started: the run reached its --deadline (T-1809)'; exit 1",
                    "echo 'FAIL: t (m.A.t)'; echo 'run.py: m.B still running when the run was ended from outside'; exit 1",
                    "echo 'FAIL: t (m.A.t)'; exit 124"):
            with self.subTest(ran=ran[:60]):
                os.environ["FM_POSTMERGE_RERUN"] = "ls bad*.txt >/dev/null 2>&1 || exit 0; " + ran  # green on the parent
                sha = self.land(f"bad{len(ran)}.txt", "a\n")
                slanes.pm_run(self.p, self.repo)
                self.assertEqual(self.rec(sha)["classes"], ["m.A", "m.B"], "B did not finish alone: it is not green alone")

    def test_inherited_per_class_a_module_level_error_alone_blames_every_red_class_of_the_module(self):
        os.environ["FM_POSTMERGE_CMD"] = "echo 'FAIL: t (m.A.t)'; echo 'FAIL: t (m.B.t)'; echo 'FAIL: t (n.C.t)'; exit 1"
        os.environ["FM_POSTMERGE_RERUN"] = ("ls bad*.txt >/dev/null 2>&1 || exit 0; "
                                            "echo 'ERROR: setUpClass (m.A)'; exit 1")  # names the module m, as run.py maps it
        sha = self.land("bad.txt", "a\n")
        slanes.pm_run(self.p, self.repo)
        self.assertEqual(self.rec(sha)["classes"], ["m.A", "m.B"], "n.C is green alone: a flake")

    def test_surfaces_in_sampo_next(self):
        sha = self.conflicted("a\nlater edit\n")
        slanes.pm_set(self.p, sha, red_at=OLD)
        out = self.fm("next").stdout
        self.assertIn(f"sampo lane verify --reset {sha[:9]}", out)


if __name__ == "__main__":
    unittest.main()
