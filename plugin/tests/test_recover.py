"""T-3928: a restart nobody announced. Long jobs journal themselves; sampo recover brings back what a reboot interrupted."""
import json
import os
import subprocess
import time
import unittest
from unittest import mock

from helpers import ForemanTestCase

from sampo import bus, hooks, lanes, recover
from sampo import core as c

OLD = "11111111-old-boot"


class Recover(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)
        self.boot = os.path.join(self.tmp, "boot_id")
        with open(self.boot, "w") as f:
            f.write("22222222-new-boot\n")
        for patch in (mock.patch.object(hooks, "BOOT_ID", self.boot),
                      mock.patch.object(recover, "btime", lambda: time.time() - 100)):
            patch.start()
            self.addCleanup(patch.stop)

    def old_entry(self, kind="lane", argv=("lane", "land", "--first", "T-0001"), boot=OLD, name="a"):
        os.makedirs(recover._dir(self.p), exist_ok=True)
        path = os.path.join(recover._dir(self.p), f"{boot[:8]}-{name}.json")
        with open(path, "w") as f:
            json.dump({"kind": {"lane": "land"}.get(kind, kind), "argv": list(argv), "cwd": self.repo, "pid": 1,
                       "boot": boot, "project": self.p.slug, "start": time.time() - 9000}, f)
        return path

    def age(self, path):
        os.utime(path, (time.time() - 3600,) * 2)

    def test_a_job_journals_itself_and_clears_on_any_exit(self):
        with recover.journal(self.p, "land", ["lane", "land"]):
            (e,) = recover.entries(self.p)
            self.assertEqual((e["argv"], e["boot"], e["cwd"]), (["lane", "land"], "22222222-new-boot", os.getcwd()))
            self.assertEqual(recover.interrupted(self.p), [], "this boot's job is running, not interrupted")
        self.assertEqual(recover.entries(self.p), [])
        with self.assertRaises(ValueError), recover.journal(self.p, "run", ["run"]):
            raise ValueError("an error exit")
        self.assertEqual(recover.entries(self.p), [])

    def test_only_the_long_verbs_are_journaled(self):
        ns = lambda **k: type("A", (), k)  # noqa: E731
        self.assertEqual(recover._kind(ns(verb="lane", action="land")), "land")
        self.assertEqual(recover._kind(ns(verb="lane", action="land", bg=True)), None, "the detached starter returns at once")
        self.assertEqual(recover._kind(ns(verb="night", tick=True)), None)
        self.assertEqual(recover._kind(ns(verb="night")), "night")
        self.assertEqual(recover._kind(ns(verb="lane", action="list")), None)

    def test_an_older_boots_entry_is_interrupted_and_listed_once(self):
        self.old_entry(name="a")
        self.old_entry(name="b")  # the same job journaled twice
        self.assertEqual(len(recover.entries(self.p)), 2)
        self.assertEqual(len(recover.interrupted(self.p)), 1)

    def test_recover_restarts_with_the_original_arguments_and_a_second_run_changes_nothing(self):
        a = self.old_entry(argv=("lane", "land", "--first", "T-0001,T-0002"))
        started = []
        with mock.patch.object(recover, "spawn", lambda p, e: started.append(e["argv"]) or 4242):
            rep = recover.recover(self.p)
            self.assertEqual(started, [["lane", "land", "--first", "T-0001,T-0002"]], "a land keeps its --first ids")
            self.assertEqual(len(rep["restarted"]), 1)
            self.assertFalse(os.path.exists(a))
            rep2 = recover.recover(self.p)
        self.assertEqual(started, [["lane", "land", "--first", "T-0001,T-0002"]], "not restarted twice")
        self.assertEqual({k: v for k, v in rep2.items() if k != "relaunch"},
                         {"restarted": [], "left": [], "merges": [], "locks": [], "leases": 0, "snapshots": [], "fsync": False})

    def test_a_job_that_cannot_start_stays_listed(self):
        a = self.old_entry()
        rep = recover.recover(self.p)  # FOREMAN_NO_BACKGROUND is set: spawn says None
        self.assertTrue(rep["left"])
        self.assertTrue(os.path.exists(a))

    def test_a_half_done_merge_and_a_stale_index_lock_from_the_old_boot_are_cleared(self):
        self.conflicted_merge("foreman/T-5", clear=True)

    def test_a_persons_hand_merge_in_main_is_left_alone(self):
        git, head = self.conflicted_merge("feature-x", clear=False)
        rep = recover.recover(self.p)
        self.assertEqual(rep["merges"], [])
        self.assertTrue(any("not Sampo's" in x for x in rep["left"]), rep["left"])
        self.assertTrue(os.path.exists(head), "the merge and its resolved conflicts stay")
        self.assertEqual(recover.scan(self.p)["merges"], [], "not announced as Sampo's to abort")

    def conflicted_merge(self, side, clear):
        git = lambda *a, cwd=self.repo: subprocess.run(["git", "-C", cwd, *a], capture_output=True, text=True)  # noqa: E731
        git("checkout", "-q", "-b", side)
        with open(os.path.join(self.repo, "f.txt"), "w") as f:
            f.write("side\n")
        git("add", "f.txt")
        git("commit", "-qm", "side")
        git("checkout", "-q", "-")
        with open(os.path.join(self.repo, "f.txt"), "w") as f:
            f.write("main\n")
        git("add", "f.txt")
        git("commit", "-qm", "main")
        self.assertNotEqual(git("merge", side).returncode, 0, "a conflict leaves MERGE_HEAD")
        head = os.path.join(self.repo, ".git", "MERGE_HEAD")
        lock = os.path.join(self.repo, ".git", "index.lock")
        if not clear:
            self.age(head)
            return git, head
        with open(lock, "w"):
            pass
        self.assertEqual(recover.stale_git(self.p), ([], []), "this boot's merge and lock are not stale")
        self.age(head)
        self.age(lock)
        rep = recover.recover(self.p)
        self.assertEqual((len(rep["merges"]), len(rep["locks"])), (1, 1))
        self.assertFalse(os.path.exists(head) or os.path.exists(lock))
        self.assertEqual(git("status", "--porcelain").stdout.strip(), "")
        self.assertEqual(recover.recover(self.p)["merges"], [], "idempotent")

    def test_leases_of_the_old_boot_are_dropped_and_this_boots_kept(self):
        with c.lock(bus._dir()):
            bus._write_leases({
                "old": {"session": "s", "task": None, "project": self.p.slug, "file": "a", "symbol": "*",
                        "until": time.time() + 600, "boot": OLD},
                "unstamped": {"session": "s", "task": None, "project": self.p.slug, "file": "b", "symbol": "*",
                              "until": time.time() + 60},  # renewed ~19 min ago: before this boot (100 s ago)? no: after
                "new": {"session": "s", "task": None, "project": self.p.slug, "file": "c", "symbol": "*",
                        "until": time.time() + 600, "boot": "22222222-new-boot"}})
        rep = recover.recover(self.p)
        self.assertGreaterEqual(rep["leases"], 1)
        left = bus._leases()
        self.assertIn("new", left)
        self.assertNotIn("old", left)

    def test_a_dead_builders_dirty_lane_is_snapshotted_without_a_commit(self):
        wt = os.path.join(self.tmp, "lane")
        subprocess.run(["git", "-C", self.repo, "worktree", "add", "-q", "-b", "foreman/T-9", wt], check=True)
        tid = self.fm("task", "new", "x", "--type", "FIX", "--tier", "S", "--step", "s", "--json").stdout
        tid = json.JSONDecoder().raw_decode(tid)[0]["id"]
        b = c.find_brief(self.p, tid)
        b.meta["builder_proc"] = f"{bus.host_id()}|999999999|1"
        b.meta["lane"] = wt
        c.save_brief(self.p, b)
        with open(os.path.join(wt, "wip.py"), "w") as f:
            f.write("x = 1\n")
        git = lambda *a: subprocess.run(["git", "-C", wt, *a], capture_output=True, text=True).stdout  # noqa: E731
        head = git("rev-parse", "HEAD")
        rep = recover.recover(self.p)
        self.assertEqual(rep["snapshots"], [tid])
        self.assertIn(tid, rep["relaunch"])
        self.assertEqual(git("rev-parse", "HEAD"), head, "the lane's branch does not move")
        self.assertEqual(len(git("for-each-ref", c.SNAPSHOTS).split("\n")) - 1, 1, "one snapshot ref")
        self.assertTrue(os.path.exists(os.path.join(wt, "wip.py")), "the files stay for the relaunch")
        self.assertNotIn(tid, lanes.awaiting_merge(self.p, c.load_briefs(self.p), quiet_min=0))
        self.assertEqual(recover.recover(self.p)["snapshots"], [], "the same tree is not snapshotted twice")

    def test_the_first_session_of_a_new_boot_says_what_the_reboot_interrupted_and_next_leads_with_it(self):
        self.old_entry()
        text = recover.note(self.p)
        self.assertIn("sampo recover", text)
        self.assertIn("1 job(s) (land)", text)
        self.assertEqual(text.count("land"), 1, "a job is listed once")
        self.assertEqual(recover.note(self.p), text, "said again until it runs")
        self.assertTrue(recover.next_lead(self.p).startswith("The machine restarted"))
        out = hooks.session_start({"session_id": "s3928", "cwd": self.repo, "source": "startup"})
        self.assertIn("sampo recover", json.dumps(out))
        self.assertIn("sampo recover", c.next_for(self.p)[2])
        with mock.patch.object(recover, "spawn", lambda p, e: 1):
            recover.recover(self.p)
        self.assertEqual((recover.note(self.p), recover.next_lead(self.p)), ("", ""), "recovered: quiet")

    def test_a_boot_that_interrupted_nothing_scans_once_and_stays_quiet(self):
        self.assertEqual((recover.note(self.p), recover.next_lead(self.p)), ("", ""))
        self.old_entry()  # journaled later in the same boot by an old run? not a reboot's doing: not scanned again
        self.assertEqual(recover.note(self.p), "")

    def test_a_repo_with_a_lost_object_holds_every_restart(self):
        # T-4074: the 2026-11-09 reboot left 11 loose objects empty, the build branch's tip among them, and recover
        # restarted the land on it
        a = self.old_entry()
        with open(os.path.join(self.repo, "lost.txt"), "w") as f:
            f.write("lost\n")
        subprocess.run(["git", "-C", self.repo, "add", "lost.txt"], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", "lost"], check=True)
        sha = subprocess.run(["git", "-C", self.repo, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        obj = os.path.join(self.repo, ".git", "objects", sha[:2], sha[2:])
        os.chmod(obj, 0o644)
        open(obj, "w").close()  # as the crash left it: an empty file
        started = []
        with mock.patch.object(recover, "spawn", lambda p, e: started.append(e["argv"]) or 4242):
            rep = recover.recover(self.p)
        self.assertEqual(started, [], "nothing restarted on a broken repo")
        self.assertTrue(os.path.exists(a), "the job stays listed for the run after the repair")
        self.assertTrue(any("git fsck" in x and sha[:7] in x for x in rep["left"]), rep["left"])

    def test_recover_sets_core_fsync_on_sampos_own_repo(self):
        # T-4074: T-3727's core.fsync reached only installs that ran sampo setup again
        subprocess.run(["git", "init", "-q", self.home], check=True)
        rep = recover.recover(self.p)
        got = subprocess.run(["git", "-C", self.home, "config", "--local", "core.fsync"], capture_output=True, text=True)
        self.assertEqual(got.stdout.strip(), "objects,reference,index")
        self.assertTrue(rep["fsync"])
        self.assertFalse(recover.recover(self.p)["fsync"], "set once")

    def test_the_verb_runs_and_reports(self):
        self.assertIn("Nothing to recover", self.fm("recover").stdout)
        self.old_entry()  # an older boot than the machine's real one, too
        self.assertIn("left:", self.fm("recover").stdout)  # FOREMAN_NO_BACKGROUND: it can't restart, and says so


if __name__ == "__main__":
    unittest.main()
