"""T-5010 (big tiers part C, T-4224): `sampo big close ID` is staged and re-runnable. It refuses while a child is open,
syncs the build branch into big/ID, runs the Integration criteria and the full gates once on that tree, then makes one
Merge commit on the build branch (with the task's changelog line). A red stage changes nothing on the build branch, and
a re-run after a crash finishes the merge that was under way."""
import argparse
import os

import test_big_run as tbr

from sampo import big
from sampo import cli
from sampo import core as c
from sampo import lanes

CLOSE_OK = "criterion: the runner is in place :: grep -q together runner.py\n"
CLOSE_RED = "criterion: the runner is in place :: grep -q missing runner.py\n"
GATES_OK = {"FM_POSTMERGE_CMD": "true"}
GATES_RED = {"FM_POSTMERGE_CMD": "false"}


class Base(tbr.Base):
    def setUp(self):
        super().setUp()
        self.commit("runner.py", "run together\n")
        self.commit("CHANGELOG.md", "# Changelog\n\n## Unreleased\n\n### Everything else\n\n")

    def parent(self, integration=CLOSE_OK):
        """An expanded big task whose children are all still open -> (parent id, {key: child id})"""
        tid = self.make()
        self.good(tid, integration=integration)
        self.fm("big", "expand", tid)
        self.fm("lane", "max", "5")
        got = big._children(c.find_brief(self.p, tid))
        return tid, {k: i for k, (i, _) in got.items()}

    def finish(self, tid, kids):
        """Every child landed on big/<ID>, as each lane's merge leaves it."""
        big.branch(self.p, c.find_brief(self.p, tid))
        for kid in kids.values():
            self.land(tid, kid)

    def close(self, tid, env=GATES_OK, changelog="Big tasks close as one merge commit on the build branch", check=False):
        return self.fm("big", "close", tid, "--changelog", changelog, env=env, check=check)

    def snapshot(self, tid):
        return {"head": self.git("rev-parse", "HEAD"), "tip": self.git("rev-parse", f"big/{tid}"),
                "changelog": self.git("show", "HEAD:CHANGELOG.md"), "status": c.find_brief(self.p, tid).status,
                "meta": dict(self.meta(tid))}

    def merges_since(self, sha):
        return [x for x in self.git("rev-list", "--merges", f"{sha}..HEAD").split() if x]


class Refuses(Base):
    def test_close_refuses_while_a_child_is_open_and_changes_nothing(self):
        tid, kids = self.parent()
        big.branch(self.p, c.find_brief(self.p, tid))
        before = self.snapshot(tid)
        r = self.close(tid)
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(kids["c1"], r.stdout + r.stderr, "the refusal names the open child")
        self.assertEqual(self.snapshot(tid), before, "a refused close leaves the build branch, big/ID and the brief alone")
        self.assertNotIn("big_close", self.meta(tid), "no stage marker is written for a refused close")

    def test_close_of_a_task_that_is_not_big_is_refused(self):
        tid = self.make(tier="M")
        r = self.close(tid)
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("big_close", self.meta(tid))


class Red(Base):
    def test_a_red_integration_criterion_changes_nothing(self):
        tid, kids = self.parent(integration=CLOSE_RED)
        self.finish(tid, kids)
        before = self.snapshot(tid)
        r = self.close(tid)
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("runner is in place", r.stdout + r.stderr, "the refusal names the criterion that is red")
        self.assertEqual(self.snapshot(tid), before, "a red criterion moves nothing: build, big/ID, CHANGELOG, brief")

    def test_a_red_full_gate_changes_nothing(self):
        tid, kids = self.parent()
        self.finish(tid, kids)
        before = self.snapshot(tid)
        r = self.close(tid, env=GATES_RED)
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.snapshot(tid), before, "a red gate moves nothing on the build branch")

    def test_a_rerun_after_a_red_stage_lands_once_it_is_green(self):
        tid, kids = self.parent()
        self.finish(tid, kids)
        head = self.git("rev-parse", "HEAD")
        self.assertNotEqual(self.close(tid, env=GATES_RED).returncode, 0)
        r = self.close(tid, env=GATES_OK)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(self.merges_since(head)), 1, "the re-run makes the one merge, not one per attempt")


class Green(Base):
    def test_a_green_close_is_one_merge_commit_on_the_build_branch(self):
        tid, kids = self.parent()
        self.finish(tid, kids)
        head, tip = self.git("rev-parse", "HEAD"), self.git("rev-parse", f"big/{tid}")
        r = self.close(tid)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        new = self.git("rev-parse", "HEAD")
        self.assertNotEqual(new, head, "the build branch moved")
        parents = self.git("rev-list", "--parents", "-n1", "HEAD").split()[1:]
        self.assertEqual(parents, [head, tip], "one merge commit: the build head, then big/ID")
        self.assertEqual(self.git("log", "-1", "--format=%s"), f"Merge {tid}: {c.find_brief(self.p, tid).title}")
        self.assertEqual(self.git("show", "--name-only", "--format=", "HEAD").split(), ["CHANGELOG.md"],
                         "the merge carries the changelog line, and nothing else")
        self.assertEqual(self.git("show", "HEAD:CHANGELOG.md").count(f"({tid})"), 1, "one changelog entry")
        self.assertEqual(lanes.pm_records(self.p)[new]["state"], "green", "the gates ran on this merge's tree: green, not rerun")

    def test_a_second_close_makes_no_second_merge(self):
        tid, kids = self.parent()
        self.finish(tid, kids)
        self.assertEqual(self.close(tid).returncode, 0)
        landed = self.git("rev-parse", "HEAD")
        r = self.close(tid)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD"), landed, "the second close finds the merge and makes nothing")
        self.assertEqual(self.git("show", "HEAD:CHANGELOG.md").count(f"({tid})"), 1)


class Resume(Base):
    def test_a_rerun_after_a_crash_finishes_the_merge_under_way(self):
        tid, kids = self.parent()
        self.finish(tid, kids)
        head = self.git("rev-parse", "HEAD")
        # the crash: a merge of big/ID that was started and never committed (MERGE_HEAD is left behind)
        self.git("merge", "--no-ff", "--no-commit", f"big/{tid}")
        self.assertTrue(self.git("rev-parse", "-q", "--verify", "MERGE_HEAD"))
        r = self.close(tid)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(self.merges_since(head)), 1, "the resumed close commits the merge it found, once")
        self.assertEqual(self.git("rev-parse", "-q", "--verify", "MERGE_HEAD", check=False), "",
                         "no merge is left in progress")

    def test_a_rerun_after_a_crash_past_the_commit_records_it_and_says_so(self):
        tid, kids = self.parent()
        self.finish(tid, kids)
        self.assertEqual(self.close(tid).returncode, 0)
        landed = self.git("rev-parse", "HEAD")
        # the crash: the merge commit is on the build branch, the bookkeeping after it never ran (the stage marker is left)
        for sha in list(lanes.pm_records(self.p)):
            os.remove(os.path.join(lanes._pm_dir(self.p), f"{sha}.json"))
        cli.mutate(self.p, tid, lambda x: (x.meta.pop("big_closed", None),
                                           x.meta.__setitem__(big.CLOSE_MARK, {"stage": "merging"})), "test", {})
        r = self.close(tid)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD"), landed, "no second merge")
        self.assertNotIn("no gate ran", r.stdout + r.stderr, "the gates did run, in the close that crashed")
        self.assertIn("crash", r.stdout + r.stderr)
        self.assertEqual(self.meta(tid).get("big_closed"), landed)
        self.assertNotIn(big.CLOSE_MARK, self.meta(tid))
        self.assertIn(landed, lanes.pm_records(self.p), "the merge has a verify record again")


class Gates(Base):
    """What counts as the full gates: the suite command, else the project's own checks, never none when checks exist."""
    NO_SUITE = {"FM_POSTMERGE_CMD": ""}

    def test_a_project_with_only_checks_runs_them_and_a_red_one_refuses(self):
        tid, kids = self.parent()
        self.finish(tid, kids)
        self.fm("check", "add", "false")
        before = self.snapshot(tid)
        r = self.close(tid, env=self.NO_SUITE)
        self.assertNotEqual(r.returncode, 0, "a red check refuses the close: " + r.stdout + r.stderr)
        self.assertIn("gates are red", r.stdout + r.stderr)
        self.assertEqual(self.snapshot(tid), before)

    def test_a_project_with_only_green_checks_lands(self):
        tid, kids = self.parent()
        self.finish(tid, kids)
        self.fm("check", "add", "true")
        head = self.git("rev-parse", "HEAD")
        r = self.close(tid, env=self.NO_SUITE)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(self.merges_since(head)), 1)
        self.assertNotIn("no gate ran", r.stdout + r.stderr)


class Stages(Base):
    def moving_gate(self, tid, how):
        """A gate that passes and, as it runs, moves the build branch ('head') or big/ID ('tip')."""
        # the repo by a symlink: a gate that names the main checkout's own path is run in the scratch tree instead
        alias = os.path.join(self.tmp, "main-alias")
        if not os.path.exists(alias):
            os.symlink(self.repo, alias)
        if how == "head":
            return f"git -c user.email=t@t -c user.name=t -C {alias} commit --allow-empty -qm moved"
        return (f"git -C {alias} update-ref refs/heads/big/{tid} "
                f"$(git -C {alias} commit-tree 'big/{tid}^{{tree}}' -p refs/heads/big/{tid} -m moved)")

    def test_the_build_branch_moving_during_the_gates_refuses_and_merges_nothing(self):
        tid, kids = self.parent()
        self.finish(tid, kids)
        r = self.close(tid, env={"FM_POSTMERGE_CMD": self.moving_gate(tid, "head")})
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("moved", r.stdout + r.stderr)
        self.assertEqual(self.merges_since(self.git("rev-parse", "HEAD~1")), [], "no merge commit was made")
        self.assertNotIn("big_closed", self.meta(tid))

    def test_big_id_moving_during_the_gates_refuses_and_merges_nothing(self):
        tid, kids = self.parent()
        self.finish(tid, kids)
        head = self.git("rev-parse", "HEAD")
        r = self.close(tid, env={"FM_POSTMERGE_CMD": self.moving_gate(tid, "tip")})
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("moved", r.stdout + r.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD"), head, "the build branch did not move")

    def hand_commit(self, tid, name="hand.py"):
        """A commit made on big/<ID> by hand: a patch no child was reviewed at."""
        alias = os.path.join(self.tmp, "bigwt")
        self.git("worktree", "add", "-q", alias, f"big/{tid}")
        with open(os.path.join(alias, name), "w") as f:
            f.write("HAND = 1\n")
        self.git("add", name, cwd=alias)
        self.git("commit", "-qm", "by hand", cwd=alias)
        return alias

    def test_a_hand_commit_on_the_integration_branch_is_refused_and_merges_nothing(self):  # T-5968
        tid, kids = self.parent()
        self.finish(tid, kids)
        self.hand_commit(tid)
        head = self.git("rev-parse", "HEAD")
        r = self.close(tid)
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("no child's reviewed work", r.stdout + r.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD"), head, "nothing reached the build branch")

    def test_a_commit_a_child_was_accepted_at_is_carried_into_the_close(self):  # T-5968
        tid, kids = self.parent()
        self.finish(tid, kids)
        cut = self.git("rev-parse", f"big/{tid}")
        alias = self.hand_commit(tid)
        self.child_stamps(next(iter(kids.values())), cut, accepted=self.git("rev-parse", f"big/{tid}"))  # accepted at this work
        r = self.close(tid)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.repo, "hand.py")))
        self.assertTrue(alias)

    def child_stamps(self, kid, cut, accepted="", reviewed=""):
        """The brief of a child as its launch and its review leave it: the commit its lane was cut at, the tips accepted."""
        b = c.find_brief(self.p, kid)
        b.meta["lane_cut"] = cut
        for k, v in (("accepted_tip", accepted), ("reviewed_tip", reviewed)):
            if v:
                b.meta[k] = v
        c.save_brief(self.p, b)

    def commit_in(self, alias, name, body="X = 1\n", msg="work"):
        with open(os.path.join(alias, name), "w") as f:
            f.write(body)
        self.git("add", name, cwd=alias)
        self.git("commit", "-qm", msg, cwd=alias)
        return self.git("rev-parse", "HEAD", cwd=alias)

    def test_a_hand_commit_below_a_childs_stamp_is_held_not_carried_by_it(self):  # T-5968 security review 1
        tid, kids = self.parent()
        self.finish(tid, kids)
        alias = self.hand_commit(tid)  # nobody's work ...
        hand = self.git("rev-parse", f"big/{tid}")
        own = self.commit_in(alias, "child.py", msg="the child's own commit on top of it")  # ... under a child's reviewed one
        self.child_stamps(next(iter(kids.values())), hand, accepted=own)  # its lane was cut after the hand commit
        head = self.git("rev-parse", "HEAD")
        r = self.close(tid)
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("1 commit(s)", r.stdout + r.stderr, "only the hand commit is unreviewed: the child's own is carried")
        self.assertIn("no child's reviewed work", r.stdout + r.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD"), head, "nothing reached the build branch")

    def merged_pair(self, tid, kids, extra=False):
        """big/<ID> takes a merge of two commits two reviews cover (a child's accepted tip, another's reviewed tip); with
        `extra` the merge also adds a file of its own."""
        alias = os.path.join(self.tmp, "mergewt")
        self.git("worktree", "add", "-q", alias, f"big/{tid}")
        cut = self.git("rev-parse", "HEAD", cwd=alias)
        self.git("checkout", "-q", "-b", "side", cwd=alias)
        side = self.commit_in(alias, "side.py", msg="side work")
        self.git("checkout", "-q", f"big/{tid}", cwd=alias)
        main = self.commit_in(alias, "main.py", msg="main work")
        self.git("merge", "--no-ff", "--no-commit", "side", cwd=alias)
        if extra:
            with open(os.path.join(alias, "smuggled.py"), "w") as f:
                f.write("EVIL = 1\n")
            self.git("add", "smuggled.py", cwd=alias)
        self.git("commit", "-qm", "merge side", cwd=alias)
        self.child_stamps(next(iter(kids.values())), cut, accepted=main, reviewed=side)

    def test_a_merge_that_adds_content_of_its_own_is_held(self):  # T-5968 security review 2
        tid, kids = self.parent()
        self.finish(tid, kids)
        self.merged_pair(tid, kids, extra=True)
        head = self.git("rev-parse", "HEAD")
        ids = lanes.patch_ids(self.repo, f"big/{tid}", head)  # the serve rebase check reads the same tokens
        self.assertTrue(ids[-1].startswith("merge-own:"), ids)
        r = self.close(tid)
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("carry content of their own", r.stdout + r.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD"), head, "nothing reached the build branch")

    def test_a_clean_merge_of_reviewed_work_passes(self):  # T-5968 security review 2
        tid, kids = self.parent()
        self.finish(tid, kids)
        self.merged_pair(tid, kids)
        self.assertEqual(lanes.patch_ids(self.repo, f"big/{tid}", self.git("rev-parse", "HEAD"))[-1], "merge")
        r = self.close(tid)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.repo, "side.py")))
        self.assertTrue(os.path.exists(os.path.join(self.repo, "main.py")))

    def test_a_merge_with_a_hand_resolved_conflict_is_held(self):  # T-5968 security review 2
        tid, kids = self.parent()
        self.finish(tid, kids)
        alias = os.path.join(self.tmp, "conflictwt")
        self.git("worktree", "add", "-q", alias, f"big/{tid}")
        cut = self.git("rev-parse", "HEAD", cwd=alias)
        self.git("checkout", "-q", "-b", "side", cwd=alias)
        side = self.commit_in(alias, "same.py", "A = 1\n", "side")
        self.git("checkout", "-q", f"big/{tid}", cwd=alias)
        main = self.commit_in(alias, "same.py", "A = 2\n", "main")
        self.git("merge", "--no-ff", "--no-commit", "side", cwd=alias, check=False)
        with open(os.path.join(alias, "same.py"), "w") as f:
            f.write("A = 3\n")
        self.git("add", "same.py", cwd=alias)
        self.git("commit", "-qm", "merge side by hand", cwd=alias)
        self.child_stamps(next(iter(kids.values())), cut, accepted=main, reviewed=side)
        r = self.close(tid)
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("a conflict resolved by hand", r.stdout + r.stderr)

    def test_a_dirty_main_checkout_is_refused_before_any_gate_runs(self):
        tid, kids = self.parent()
        self.finish(tid, kids)
        with open(os.path.join(self.repo, "runner.py"), "a") as f:
            f.write("# edited\n")
        marker = os.path.join(self.tmp, "gate-ran")
        r = self.close(tid, env={"FM_POSTMERGE_CMD": f"touch {marker}"})
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("uncommitted", r.stdout + r.stderr)
        self.assertFalse(os.path.exists(marker), "the gates never ran: the tree was refused first")

    def test_a_tip_the_build_branch_already_has_adds_nothing_and_is_refused(self):
        tid, kids = self.parent()
        self.finish(tid, kids)
        self.git("merge", "--ff-only", f"big/{tid}")
        head = self.git("rev-parse", "HEAD")
        r = self.close(tid)
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("adds nothing", r.stdout + r.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertNotIn(big.CLOSE_MARK, self.meta(tid), "the stage marker is cleared with the refusal")

    def test_a_merge_conflict_at_the_last_stage_is_aborted_and_changes_nothing(self):
        tid, kids = self.parent()
        self.finish(tid, kids)
        self.git("checkout", "-q", f"big/{tid}")
        self.commit("runner.py", "run together, on the big branch\n")
        base = self.git("rev-parse", "HEAD~1")
        self.git("checkout", "-q", "-")
        self.commit("runner.py", "run together, on the build branch\n")
        head, tip = self.git("rev-parse", "HEAD"), self.git("rev-parse", f"big/{tid}")
        self.assertNotEqual(base, head)
        b = c.find_brief(self.p, tid)
        with self.assertRaises(c.PolicyError) as got:
            big._merge(self.p, argparse.Namespace(changelog="x"), b, head, tip, "true")
        self.assertIn("conflicted", str(got.exception))
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual(self.git("rev-parse", "-q", "--verify", "MERGE_HEAD", check=False), "", "the merge was aborted")
        self.assertEqual(self.git("status", "--porcelain", "--untracked-files=no"), "")
        self.assertNotIn(big.CLOSE_MARK, c.find_brief(self.p, tid).meta)

    def test_a_task_already_done_is_refused(self):
        tid, kids = self.parent()
        self.finish(tid, kids)
        cli.mutate(self.p, tid, lambda x: x.meta.__setitem__("status", "done"), "test", {})
        r = self.close(tid)
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("done", r.stdout + r.stderr)
