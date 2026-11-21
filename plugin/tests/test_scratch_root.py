"""T-4735: Sampo's scratch trees (the train's, the probes', the batch's, the post-merge verifies', the ship's) live in one
folder Sampo owns, not directly under the shared temp root, where a cleanup by name, age or prefix (the 2026-11-15
incident: a live train's tree vanished, remover never identified) could reach them. Each tree holds a lock while its owner
lives; doctor lists the trees whose lock is free and whose owner is gone, tidy --apply removes only those; a tree a train
finds gone is logged with its folder, its age and whether the scratch root itself survived."""
# map-links: plugin/lib/sampo/core.py, plugin/lib/sampo/train.py, plugin/lib/sampo/tidy.py, plugin/lib/sampo/doctor.py
import fnmatch
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import test_lane_train as tlt
from helpers import SampoTestCase
from test_lane_train import TrainCase

from sampo import core as c

LIB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib")

# a process that makes a tree, says where, and ends WITHOUT cleaning up: what a killed train leaves
MAKER = ("import sys\nsys.path.insert(0, sys.argv[1])\nfrom sampo import core as c\n"
         "print(c.scratch_tree('sampo-train-', sys.argv[2]), flush=True)\n")
# a process that makes a tree and keeps it (and its lock) until stdin closes: a live train
KEEPER = ("import sys\nsys.path.insert(0, sys.argv[1])\nfrom sampo import core as c\n"
          "print(c.scratch_tree('sampo-train-', sys.argv[2]), flush=True)\nsys.stdin.read()\n")


class Scratch(SampoTestCase):
    def setUp(self):
        super().setUp()
        self.base = os.path.join(self.tmp, "temproot")  # the shared temp root of this test
        os.makedirs(self.base)
        self.root = os.path.join(self.tmp, "owned")
        for patch in (mock.patch.object(tempfile, "tempdir", self.base),
                      mock.patch.dict(os.environ, {"FOREMAN_SCRATCH_ROOT": self.root})):
            patch.start()
            self.addCleanup(patch.stop)
        self.proj = os.path.join(self.tmp, "proj")
        os.makedirs(self.proj)

    def mode(self, path):
        return stat.S_IMODE(os.stat(path).st_mode)

    def spawn(self, script):
        env = dict(os.environ, TMPDIR=self.base)  # the child's temp root is this test's
        p = subprocess.Popen([sys.executable, "-c", script, LIB, getattr(self, "spawn_repo", self.proj)],
                             stdout=subprocess.PIPE, stdin=subprocess.PIPE,
                             text=True, env=env)
        self.addCleanup(lambda: (p.stdin.close(), p.wait()))
        self.said = p.stdout.readline().strip()
        return p, self.said.split(" ")[0]

    def made(self):
        """A tree made by a process that is now gone (lock free, owner dead), as a killed train leaves it."""
        p, tree = self.spawn(MAKER)
        p.wait()
        return tree

    # ---- where the trees live

    def test_a_tree_lies_under_the_owned_root_and_the_root_is_private(self):
        tree = c.scratch_tree("sampo-train-", self.proj)
        self.addCleanup(c.scratch_release, tree)
        self.assertTrue(os.path.isdir(tree))
        self.assertTrue(os.path.basename(tree).startswith("sampo-train-"))
        proj_dir = os.path.dirname(tree)
        self.assertEqual(os.path.dirname(proj_dir), self.root, "one folder per project inside the root")
        self.assertEqual(self.mode(self.root), 0o700)
        self.assertEqual(self.mode(proj_dir), 0o700)
        self.assertNotEqual(os.path.dirname(tree), self.base)

    def test_the_default_root_is_a_named_folder_under_the_temp_root(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("FOREMAN_SCRATCH_ROOT")
            tree = c.scratch_tree("sampo-base-", self.proj)
            self.addCleanup(c.scratch_release, tree)
            root = os.path.dirname(os.path.dirname(tree))
            self.assertEqual(os.path.dirname(root), self.base)
            self.assertIn(str(os.getuid()), os.path.basename(root), "named for the user")
            self.assertTrue(os.path.basename(root).startswith("sampo-scratch-"))
            self.assertFalse(os.path.basename(root).startswith(("sampo-train-", "sampo-base-", "sampo-probe-", "sampo-batch-", "sampo-shape-")))
            self.assertEqual(self.mode(root), 0o700)

    def test_a_root_that_is_a_link_is_never_followed(self):
        elsewhere = os.path.join(self.tmp, "elsewhere")
        os.makedirs(elsewhere)
        os.symlink(elsewhere, self.root)
        tree = c.scratch_tree("sampo-train-", self.proj)
        self.addCleanup(c.scratch_release, tree)
        self.assertEqual(os.listdir(elsewhere), [], "nothing is made behind a link someone else planted")
        self.assertTrue(os.path.isdir(tree))

    def test_a_cleanup_of_names_directly_under_the_temp_root_cannot_reach_a_tree(self):
        """The incident's shape: something removes the entries of the shared temp root by prefix or by name."""
        tree = c.scratch_tree("sampo-train-", self.proj)
        self.addCleanup(c.scratch_release, tree)
        for name in ("sampo-train-x", "sampo-base-x", "fm-verify-x", "tmpabc"):  # its victims' look-alikes
            os.makedirs(os.path.join(self.base, name))
        for name in os.listdir(self.base):  # remove everything under the temp root that the old prefixes name
            if fnmatch.fnmatch(name, "sampo-train-*") or fnmatch.fnmatch(name, "sampo-base-*") \
                    or fnmatch.fnmatch(name, "sampo-probe-*") or fnmatch.fnmatch(name, "sampo-batch-*") \
                    or fnmatch.fnmatch(name, "sampo-shape-*") \
                    or fnmatch.fnmatch(name, "fm-verify-*"):
                shutil.rmtree(os.path.join(self.base, name))
        self.assertTrue(os.path.isdir(tree))
        self.assertTrue(c.scratch_root(self.proj, create=False).startswith(self.root))

    def test_a_cargo_throwaway_tree_is_still_known_in_the_owned_root(self):
        repo = self.repo
        tree = c.scratch_tree("sampo-train-", repo)
        self.addCleanup(c.scratch_release, tree)
        os.rmdir(tree)
        subprocess.run(["git", "-C", repo, "worktree", "add", "-q", "--detach", tree], check=True, capture_output=True)
        self.assertTrue(c.cargo_throwaway(os.path.realpath(tree)))
        sub = c.scratch_tree("fm-verify-", repo)
        self.addCleanup(c.scratch_release, sub)
        wt = os.path.join(sub, "wt")
        subprocess.run(["git", "-C", repo, "worktree", "add", "-q", "--detach", wt], check=True, capture_output=True)
        self.assertTrue(c.cargo_throwaway(os.path.realpath(wt)))

    # ---- the lock

    def test_a_tree_holds_a_lock_while_its_owner_lives(self):
        tree = c.scratch_tree("sampo-train-", self.proj)
        self.assertTrue(c.scratch_held(tree))
        c.scratch_release(tree)
        self.assertFalse(c.scratch_held(tree))
        self.assertFalse(os.path.exists(tree + ".lock"), "a released tree leaves no lock file")

    def test_the_run_lock_helpers_are_the_shared_ones(self):
        folder = os.path.join(self.tmp, "run")
        os.makedirs(folder)
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import run
        f = run.hold(folder)  # run.py's hold is core's lock on the same file: core reads it held
        self.assertTrue(c.held(folder))
        self.assertTrue(run.held(folder))
        f.close()
        self.assertFalse(c.held(folder))
        g = c.hold(folder)
        self.assertTrue(run.held(folder))
        g.close()

    # ---- leftovers

    def test_a_live_owners_tree_is_never_listed_or_removed(self):
        p, tree = self.spawn(KEEPER)
        self.assertEqual([x["path"] for x in c.scratch_leftovers()], [])
        self.assertEqual(c.scratch_sweep(), [])
        self.assertTrue(os.path.isdir(tree), "a held tree stays")
        self.assertTrue(os.path.exists(tree + ".lock"))

    def test_a_dead_owners_tree_is_listed_and_swept(self):
        tree = self.made()
        self.assertTrue(os.path.isdir(tree))
        found = c.scratch_leftovers()
        self.assertEqual([x["path"] for x in found], [tree])
        self.assertGreaterEqual(found[0]["age_s"], 0)
        self.assertEqual(c.scratch_sweep(), [tree])
        self.assertFalse(os.path.exists(tree))
        self.assertFalse(os.path.exists(tree + ".lock"))
        self.assertEqual(c.scratch_leftovers(), [])

    def test_a_sweep_takes_the_lock_itself_so_a_tree_taken_meanwhile_stays(self):
        tree = self.made()
        found = c.scratch_leftovers()
        keep = c.lock_hold(tree + ".lock")  # an owner appears between the listing and the removal
        self.assertTrue(keep)
        self.assertEqual(c.scratch_sweep(found), [])
        self.assertTrue(os.path.isdir(tree))
        keep.close()

    def test_a_registered_tree_is_unregistered_when_swept(self):
        tree = c.scratch_tree("sampo-train-", self.repo)
        os.rmdir(tree)
        subprocess.run(["git", "-C", self.repo, "worktree", "add", "-q", "--detach", tree], check=True, capture_output=True)
        c._SCRATCH.pop(tree)[0].close()  # its owner is gone: the lock is free (the pid file says a dead process)
        with open(tree + ".lock", "w") as f:
            f.write("2147483646 0\n")
        self.assertEqual(c.scratch_sweep(), [tree])
        self.assertNotIn(os.path.basename(tree), subprocess.run(["git", "-C", self.repo, "worktree", "list"],
                                                                capture_output=True, text=True).stdout)

    def test_a_folder_in_the_root_with_no_lock_is_left_while_young(self):
        young = os.path.join(c.scratch_root(self.proj), "sampo-train-young")
        os.makedirs(young)
        self.assertEqual(c.scratch_leftovers(), [], "a tree whose lock is not taken yet is being made")
        old = time.time() - 3600
        os.utime(young, (old, old))
        self.assertEqual([x["path"] for x in c.scratch_leftovers()], [young])

    def test_doctor_lists_and_tidy_removes_only_the_dead_ones(self):
        from sampo import doctor, tidy
        dead = self.made()
        p, live = self.spawn(KEEPER)
        r = doctor.check_scratch_leftovers()
        self.assertEqual(r.status, "WARN", r.detail)
        self.assertIn("1 scratch tree", r.detail)
        self.assertIn(os.path.basename(dead), r.detail)
        self.assertNotIn(os.path.basename(live), r.detail)
        actions = []
        out = tidy.scratch_findings(False, actions)
        self.assertEqual([f["kind"] for f in out], ["scratch_leftovers"])
        self.assertTrue(os.path.isdir(dead), "tidy without --apply removes nothing")
        tidy.scratch_findings(True, actions)
        self.assertFalse(os.path.exists(dead))
        self.assertTrue(os.path.isdir(live), "a held tree is never removed")
        self.assertEqual(doctor.check_scratch_leftovers().status, "PASS")

    def test_the_temp_roots_other_contents_are_never_listed_or_removed(self):
        other = os.path.join(self.base, "sampo-train-someone-elses")
        os.makedirs(other)
        old = time.time() - 86400 * 30
        os.utime(other, (old, old))
        self.assertEqual(c.scratch_leftovers(), [])
        self.assertEqual(c.scratch_sweep(), [])
        self.assertTrue(os.path.isdir(other))

    # ---- a root or folder that is not ours (T-4735 review)

    def victim(self):
        """A folder of someone else's with an unlocked, old tree two levels down: what a planted link would aim a sweep at."""
        victim = os.path.join(self.tmp, "victim")
        tree = os.path.join(victim, "proj-abc123", "sampo-train-old")
        os.makedirs(tree)
        with open(os.path.join(tree, "keep.txt"), "w") as f:
            f.write("x")
        old = time.time() - 86400
        os.utime(tree, (old, old))
        return victim, tree

    def forged(self, tree):
        return [{"path": tree, "age_s": 86400, "pid": 0}]

    def test_a_root_that_is_a_link_is_neither_listed_nor_swept(self):
        victim, tree = self.victim()
        os.symlink(victim, self.root)
        self.assertEqual(c.scratch_leftovers(), [])
        self.assertEqual(c.scratch_sweep(), [])
        via = os.path.join(self.root, "proj-abc123", "sampo-train-old")
        self.assertEqual(c.scratch_sweep(self.forged(via)), [], "a listing taken through the link is refused too")
        self.assertTrue(os.path.exists(os.path.join(tree, "keep.txt")))
        self.assertEqual(sorted(os.listdir(os.path.dirname(tree))), ["sampo-train-old"], "no lock file is made behind it")

    def test_a_root_with_group_or_other_bits_is_neither_listed_nor_swept(self):
        victim, tree = self.victim()
        os.rename(victim, self.root)
        tree = os.path.join(self.root, "proj-abc123", "sampo-train-old")
        for mode in (0o755, 0o770, 0o707):
            os.chmod(self.root, mode)
            self.assertEqual(c.scratch_leftovers(), [], oct(mode))
            self.assertEqual(c.scratch_sweep(self.forged(tree)), [], oct(mode))
            self.assertTrue(os.path.isdir(tree))
        os.chmod(self.root, 0o700)
        os.chmod(os.path.dirname(tree), 0o755)  # the project folder too
        self.assertEqual(c.scratch_leftovers(), [])
        self.assertEqual(c.scratch_sweep(self.forged(tree)), [])
        self.assertTrue(os.path.isdir(tree))

    def test_a_project_folder_swapped_for_a_link_between_listing_and_sweep_is_skipped(self):
        tree = self.made()
        found = c.scratch_leftovers()
        self.assertEqual([x["path"] for x in found], [tree])
        proj = os.path.dirname(tree)
        keep = os.path.join(self.tmp, "victim", "p", os.path.basename(tree))
        os.makedirs(keep)
        with open(os.path.join(keep, "keep.txt"), "w") as f:
            f.write("x")
        shutil.rmtree(proj)
        os.symlink(os.path.dirname(keep), proj)  # the project folder is now a link to someone else's folder
        self.assertEqual(c.scratch_sweep(found), [])
        self.assertTrue(os.path.exists(os.path.join(keep, "keep.txt")))
        self.assertEqual(os.listdir(os.path.dirname(keep)), [os.path.basename(keep)], "no lock file made behind it")

    def test_a_tree_that_is_a_link_is_never_followed(self):
        victim, vtree = self.victim()
        proj = c.scratch_root(self.proj)
        link = os.path.join(proj, "sampo-train-link")
        os.symlink(vtree, link)
        self.assertEqual(c.scratch_sweep(self.forged(link)), [])
        self.assertTrue(os.path.exists(os.path.join(vtree, "keep.txt")))

    # ---- fail safe

    def test_a_tree_whose_lock_cannot_be_made_is_not_left_in_the_root(self):
        p = c.find_project(self.repo, create=True)
        with mock.patch.object(c, "lock_hold", return_value=None):
            tree = c.scratch_tree("sampo-train-", self.repo)
        self.addCleanup(shutil.rmtree, tree, True)
        self.assertTrue(os.path.isdir(tree))
        self.assertFalse(tree.startswith(self.root + os.sep), "no unlocked tree in the root: a sweep would take a live one")
        self.assertEqual(c.scratch_leftovers(), [])
        got = [e for e in c.ledger_events(p, kinds=["scratch_fallback"])]
        self.assertTrue(got and got[-1]["data"]["reason"] == "lock", got)

    def test_a_root_and_its_fallback_both_failing_is_logged_and_the_tree_still_made(self):
        p = c.find_project(self.repo, create=True)
        real = tempfile.mkdtemp

        def mkdtemp(suffix=None, prefix=None, dir=None):
            if prefix and prefix.startswith("sampo-scratch-"):
                raise OSError(28, "No space left on device")
            return real(suffix, prefix, dir)
        with mock.patch.object(c, "_own_dir_chain", return_value=False), mock.patch.object(tempfile, "mkdtemp", mkdtemp):
            tree = c.scratch_tree("sampo-train-", self.repo)
        self.addCleanup(shutil.rmtree, tree, True)
        self.assertTrue(os.path.isdir(tree))
        reasons = [e["data"]["reason"] for e in c.ledger_events(p, kinds=["scratch_fallback"])]
        self.assertIn("root", reasons)
        self.assertEqual(c.scratch_leftovers(), [], "an unlocked tree in the shared temp root is never swept")

    def test_a_tree_in_the_private_fallback_root_is_found_by_a_later_process(self):
        victim, _ = self.victim()
        os.symlink(victim, self.root)  # the planted root: the maker falls back to a private one in the temp root
        p = c.find_project(self.repo, create=True)
        self.spawn_repo = self.repo
        tree = self.made()
        self.assertNotIn(self.root, tree)
        self.assertEqual(os.path.dirname(os.path.dirname(os.path.dirname(tree))), self.base)
        self.assertEqual([x["path"] for x in c.scratch_leftovers()], [tree])
        reasons = [e["data"]["reason"] for e in c.ledger_events(p, kinds=["scratch_fallback"])]
        self.assertIn("untrusted", reasons)
        self.assertEqual(c.scratch_sweep(), [tree])
        self.assertFalse(os.path.exists(tree))

    def test_a_look_alike_of_the_fallback_name_in_the_temp_root_is_never_read(self):
        fake = os.path.join(self.base, f"sampo-scratch-{os.getuid()}-planted", "proj-abc123", "sampo-train-old")
        os.makedirs(fake)
        old = time.time() - 86400
        os.utime(fake, (old, old))
        self.assertEqual(c.scratch_leftovers(), [], "only a root Sampo itself recorded is read")
        self.assertEqual(c.scratch_sweep(self.forged(fake)), [])
        self.assertTrue(os.path.isdir(fake))

    # ---- the loss

    def test_a_lost_tree_is_described_by_folder_age_and_root(self):
        tree = c.scratch_tree("sampo-train-", self.proj)
        self.addCleanup(c.scratch_release, tree)
        c._SCRATCH[tree] = (c._SCRATCH[tree][0], time.time() - 90)  # made 90 s ago
        shutil.rmtree(tree)  # only the tree went
        said = c.scratch_loss(tree)
        self.assertEqual(said["folder"], tree)
        self.assertTrue(89 <= said["age_s"] <= 120, said)
        self.assertNotIn("at", said, "'at' is the commit in the train's event: the time is lost_at")
        self.assertAlmostEqual(said["lost_at"], time.time(), delta=60)
        self.assertIs(said["root_exists"], True)
        shutil.rmtree(self.root)  # the whole folder went: a cleanup of the root itself
        self.assertIs(c.scratch_loss(tree)["root_exists"], False)


class InTheTrain(TrainCase):
    """The train's trees are made in the owned root, and a tree lost mid-run says what the loss looked like."""
    def setUp(self):
        super().setUp()
        self.root = os.path.join(self.tmp, "owned")
        self.env = {"FOREMAN_SCRATCH_ROOT": self.root}

    def test_every_gate_runs_in_a_tree_under_the_owned_root(self):
        seen = os.path.join(self.tmp, "pwds")
        self.fm("check", "add", f'echo "$PWD" >> {seen}')
        self.train(self.lane(1, "a.txt"), env=self.env)
        paths = [ln.strip() for ln in open(seen) if ln.strip()]
        self.assertTrue(paths)
        for path in paths:
            if os.path.basename(path).startswith("sampo-train-"):
                self.assertTrue(os.path.realpath(path).startswith(os.path.realpath(self.root) + os.sep), path)
        self.assertTrue(any(os.path.basename(p).startswith("sampo-train-") for p in paths), paths)
        leftover = [n for _, ds, _ in os.walk(self.root) for n in ds if n.startswith("sampo-")]
        self.assertEqual(leftover, [], "the train removes its own trees")

    def test_a_lost_train_tree_is_logged_with_folder_age_and_root(self):
        count = os.path.join(self.tmp, "eater-runs")
        script = os.path.join(self.tmp, "eater.sh")
        from test_train_worktree_gone import EATER
        with open(script, "w") as f:
            f.write(EATER.format(count=count, losses=1))
        self.fm("check", "add", f"sh {script}")
        self.train(self.lane(1, "a.txt"), self.lane(2, "b.txt"), env=self.env)
        p = c.find_project(self.repo)
        got = [e for e in c.ledger_events(p, kinds=["lane_train_wt_gone"])]
        self.assertTrue(got, "the loss is an event")
        data = got[0]["data"]
        self.assertTrue(os.path.basename(data["folder"]).startswith("sampo-train-"), data)
        self.assertIn(os.path.realpath(self.root), os.path.realpath(data["folder"]))
        self.assertIsInstance(data["age_s"], (int, float))
        self.assertIs(data["root_exists"], True, "only the tree went, not the root")
        self.assertRegex(data["at"], r"^[0-9a-f]{9}$", "the commit the tree was at, not overwritten by the time")
        self.assertIsInstance(data["lost_at"], int)
        self.assertGreater(data["lost_at"], 1_700_000_000)

    def test_a_failed_worktree_add_leaves_no_tree_and_no_lock(self):
        """The train's own tree is made before `git worktree add`; when that fails both the folder and its lock go."""
        git = shutil.which("git")
        fake = os.path.join(self.tmp, "fakebin")
        os.makedirs(fake)
        script = os.path.join(fake, "git")
        with open(script, "w") as f:
            f.write(f'#!/bin/sh\ncase "$*" in *"worktree add"*sampo-train-*) echo boom >&2; exit 1;; esac\nexec {git} "$@"\n')
        os.chmod(script, 0o755)
        env = dict(self.env, PATH=fake + os.pathsep + os.environ["PATH"])
        r = self.train(self.lane(1, "a.txt"), check=False, env=env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("git worktree add failed", r.stdout + r.stderr)
        left = [os.path.join(d, n) for d, ds, fs in os.walk(self.root) for n in ds + fs if n.startswith("sampo-train-")]
        self.assertEqual(left, [], "no folder and no .lock of the train's is left")


if __name__ == "__main__":
    unittest.main()
