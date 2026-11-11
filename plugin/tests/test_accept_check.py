"""T-4286: sampo lane accept starts the lane's affected check itself when the lane has no run at its tip (detached and
niced, never waited on); green queues the lane, red leaves it out with the failing classes named and no retry until the
tip moves, a cut-off check is started once more. The check always holds the four repo-shape classes, and a merge-commit
tip is checked against the whole branch's files. The detached start is one function (train._spawn_check) these tests
replace: none of them starts a real check or leaves a process behind."""
# map-links: plugin/lib/sampo/train.py
import contextlib
import io
import json
import os
import subprocess
import sys
import types
import unittest
from unittest import mock

from test_lane_land import LandCase, OLD

from sampo import cli
from sampo import core as c
from sampo import train

FRESH = "2026-06-01T00:00:00Z"  # after OLD, the lane commit's date: a run at its tip
STALE = "2025-12-01T00:00:00Z"  # before it
CMD = "python3 plugin/tests/run.py test_docs.PluginFiles test_left_out.LeftOut test_rename_aliases.RenameAliases test_run.EnvClassified"
ASKED_ROW = [["self", "read the diff", "no findings"]]


def stub(exit_code=0, failed=(), cut_off=False, text=None):
    """A stand-in for the lane's `sampo check --affected --json`: prints what the real one's JSON carries."""
    data = {"exit": exit_code, "failed": list(failed), "cut_off": cut_off, "tests": [], "changed": []}
    return [sys.executable, "-c", f"import sys; print({text!r} or ''); print({json.dumps(json.dumps(data))}); sys.exit({exit_code})"]


class AcceptCase(LandCase):
    def setUp(self):
        super().setUp()
        self.spawned = []  # (the keyword arguments _spawn_check was asked with)
        self.pid = os.getpid()  # a live process whose start time _ticks can read: the "running" check
        spawn = mock.patch.object(train, "_spawn_check", side_effect=self._spawn)
        spawn.start()
        self.addCleanup(spawn.stop)
        land = mock.patch.object(train, "spawn_land", return_value=(4242, "land.log"))
        self.land_started = land.start()
        self.addCleanup(land.stop)
        self.p = c.find_project(self.repo)

    def _spawn(self, p, b, main, **kw):
        self.spawned.append(kw)
        return self.pid, os.path.join(self.tmp, "check.log")

    def a_lane(self, n=1, run=None, name=None):
        """A finished lane (and its id); `run`: the timestamp of an affected run recorded on it, with the command."""
        tid = self.lane(n, name or f"f{n}.txt", when=OLD)
        if run:
            self.record_run(tid, run)
        return tid

    def record_run(self, tid, ts, cmd=CMD, result="✓ 10 tests"):
        b = c.find_brief(self.p, tid)
        b.add_evidence(c.AFFECTED_LABEL + cmd, result, step=1, ts=ts, ran=True)
        c.save_brief(self.p, b)

    def accept(self, tid, no_check=False):
        args = types.SimpleNamespace(audit=ASKED_ROW, json=False, no_check=no_check)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            train.accept(self.p, c.find_brief(self.p, tid), self.repo, args)
        return out.getvalue()

    def brief(self, tid):
        return c.find_brief(self.p, tid)

    def meta_of(self, tid):
        return dict(self.brief(tid).meta)

    def queued(self):
        return train.first_ids(self.p)

    def tip(self, n):
        return self.git("rev-parse", "HEAD", cwd=os.path.join(self.tmp, f"lane{n}"))

    def child(self, tid, n, argv, tries=1, since=None):
        """The detached check process's own function, run here with a stub for the lane's check command."""
        return train._accept_check_child({"project": [self.p.slug, self.p.root, self.p.dir, self.p.lane], "main": self.repo,
                                          "id": tid, "tip": self.tip(n), "tries": tries, "argv": argv, "since": since})


class Starts(AcceptCase):
    def test_a_stale_run_starts_the_check_detached_and_accept_returns_at_once(self):  # starts
        tid = self.a_lane(1, run=STALE)
        out = self.accept(tid)
        self.assertEqual(len(self.spawned), 1, out)
        self.assertEqual(self.spawned[0]["tip"], self.tip(1))
        self.assertIn("check started", out)
        self.assertEqual(self.queued(), [], "queued only when the check is green")
        self.land_started.assert_not_called()
        m = self.meta_of(tid)
        self.assertEqual((m["check_state"], m["check_tip"], m["check_pid"], m["check_tries"]), ("running", self.tip(1), str(self.pid), "1"))  # meta holds strings
        self.assertIn("(audit self)", open(self.brief(tid).path).read(), "the review is recorded before the check starts")

    def test_a_lane_with_no_run_at_all_starts_the_check(self):  # starts
        tid = self.a_lane(1)
        self.accept(tid)
        self.assertEqual(len(self.spawned), 1)
        self.assertEqual(self.queued(), [])

    def test_no_check_start_under_no_background_says_the_command_to_run_by_hand(self):  # starts
        tid = self.a_lane(1)
        with mock.patch.object(train, "_spawn_check", return_value=None):
            out = self.accept(tid)
        self.assertIn(f"sampo check --affected --hold --evidence {tid}", out)
        self.assertEqual(self.queued(), [])


class Green(AcceptCase):
    def test_a_green_check_queues_the_lane_first_and_starts_the_land(self):  # green
        tid = self.a_lane(1, run=STALE)
        self.accept(tid)
        self.assertEqual(self.child(tid, 1, stub(0)), 0)
        m = self.meta_of(tid)
        self.assertEqual(m["check_state"], "green")
        self.assertIn("accepted_at", m)
        self.assertEqual(self.queued(), [tid])
        self.land_started.assert_called_once()
        self.assertIn("check green", self.brief(tid).section("Log"))

    def test_a_green_check_joins_a_running_land_without_starting_another(self):  # green
        tid = self.a_lane(1)
        self.accept(tid)
        with mock.patch.object(train, "landing", return_value=True):
            self.child(tid, 1, stub(0))
        self.assertEqual(self.queued(), [tid])
        self.land_started.assert_not_called()


class Running(AcceptCase):
    def test_a_second_accept_says_the_check_is_running_and_starts_none(self):  # running
        tid = self.a_lane(1)
        self.accept(tid)
        out = self.accept(tid)
        self.assertEqual(len(self.spawned), 1, out)
        self.assertRegex(out, rf"check is running \(pid {self.pid},")
        self.assertEqual(self.queued(), [])

    def test_a_check_whose_process_is_gone_is_not_running(self):  # running
        tid = self.a_lane(1)
        self.accept(tid)
        cli.mutate(self.p, tid, lambda b: b.meta.__setitem__("check_ticks", 1), "x", {})  # the pid's start is another's now
        out = self.accept(tid)
        self.assertEqual(len(self.spawned), 2, "a dead check is cut off: started once more\n" + out)
        self.assertEqual(self.meta_of(tid)["check_tries"], "2")


class Red(AcceptCase):
    def test_a_red_check_leaves_the_lane_out_with_the_failing_classes_on_the_task(self):  # red
        tid = self.a_lane(1)
        self.accept(tid)
        self.assertEqual(self.child(tid, 1, stub(1, failed=["test_docs.PluginFiles", "test_run.EnvClassified"])), 1)
        m = self.meta_of(tid)
        self.assertEqual(m["check_state"], "red")
        self.assertEqual(m["check_failed"], "test_docs.PluginFiles test_run.EnvClassified")
        self.assertIn("test_docs.PluginFiles", self.brief(tid).section("Log"))
        self.assertEqual(self.queued(), [])
        self.land_started.assert_not_called()

    def test_a_red_lane_is_not_checked_again_until_its_tip_moves(self):  # red
        tid = self.a_lane(1)
        self.accept(tid)
        self.child(tid, 1, stub(1, failed=["test_docs.PluginFiles"]))
        out = self.accept(tid)
        self.assertEqual(len(self.spawned), 1, out)
        self.assertIn("test_docs.PluginFiles", out)
        self.assertIn("no retry until", out)
        lane = os.path.join(self.tmp, "lane1")
        with open(os.path.join(lane, "fix.txt"), "w") as f:
            f.write("fix\n")
        self.git("add", "fix.txt", cwd=lane)
        subprocess.run(["git", "-C", lane, "commit", "-qm", "fix"], check=True, capture_output=True,
                       env=dict(os.environ, GIT_AUTHOR_DATE=OLD, GIT_COMMITTER_DATE=OLD))
        self.accept(tid)
        self.assertEqual(len(self.spawned), 2, "the tip moved: checked again")
        self.assertEqual(self.meta_of(tid)["check_tip"], self.tip(1))
        self.assertNotIn("check_failed", self.meta_of(tid))

    def test_the_land_leaves_a_red_lane_out_and_names_the_classes(self):  # red
        tid = self.a_lane(1)
        self.accept(tid)
        self.child(tid, 1, stub(1, failed=["test_docs.PluginFiles"]))
        r = self.land(check=False)
        self.assertIn(tid, r.stdout + r.stderr)
        self.assertIn("test_docs.PluginFiles", r.stdout + r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "f1.txt")), "left out, not trained")


class CutOff(AcceptCase):
    def test_a_cut_off_check_is_started_once_more_by_its_own_process(self):  # cut_off
        tid = self.a_lane(1)
        self.accept(tid)
        self.child(tid, 1, stub(124, cut_off=True))
        self.assertEqual(len(self.spawned), 2)
        self.assertEqual(self.spawned[1]["tries"], 2)
        self.assertEqual(self.meta_of(tid)["check_state"], "running")
        self.assertEqual(self.queued(), [])

    def test_a_check_cut_off_twice_is_left_out_not_started_a_third_time(self):  # cut_off
        tid = self.a_lane(1)
        self.accept(tid)
        self.child(tid, 1, stub(124, cut_off=True))
        self.child(tid, 1, stub(124, cut_off=True), tries=2)
        self.assertEqual(len(self.spawned), 2)
        self.assertEqual(self.meta_of(tid)["check_state"], "cut_off")
        out = self.accept(tid)
        self.assertEqual(len(self.spawned), 2, out)
        self.assertIn("cut off twice", out)

    def test_a_check_that_printed_no_result_counts_as_cut_off(self):  # cut_off
        tid = self.a_lane(1)
        self.accept(tid)
        self.child(tid, 1, [sys.executable, "-c", "print('boom')"], tries=2)
        self.assertEqual(self.meta_of(tid)["check_state"], "cut_off")


class Listed(AcceptCase):
    def test_lane_list_and_next_say_checking_then_the_red_classes(self):  # running
        tid = self.a_lane(1)
        self.accept(tid)
        self.assertEqual(train.check_notes(c.load_briefs(self.p), self.repo), {tid: "checking"})
        listed = json.loads(self.fm("lane", "list", "--json").stdout)["lanes"]
        self.assertEqual([r["check"] for r in listed if r["tasks"]], ["checking"])
        self.child(tid, 1, stub(1, failed=["test_run.EnvClassified"]))
        self.assertEqual(train.check_notes(c.load_briefs(self.p), self.repo), {tid: "check red: test_run.EnvClassified"})
        self.assertIn("check red: test_run.EnvClassified", self.fm("lane", "list").stdout)


class Fresh(AcceptCase):
    def test_a_lane_with_a_fresh_run_queues_as_today_and_starts_no_check(self):  # fresh
        tid = self.a_lane(1, run=FRESH)
        out = self.accept(tid)
        self.assertEqual(self.spawned, [], out)
        self.assertEqual(self.queued(), [tid])
        self.land_started.assert_called_once()
        self.assertNotIn("check_state", self.meta_of(tid))

    def test_a_fresh_run_that_did_not_hold_the_repo_shape_classes_counts_as_stale_and_starts_the_check(self):  # fresh
        tid = self.lane(1, "f1.txt", when=OLD)
        self.record_run(tid, FRESH, cmd="python3 plugin/tests/run.py test_x")
        out = self.accept(tid)
        self.assertEqual(len(self.spawned), 1, out)
        self.assertEqual(self.queued(), [], "queued only when its own check, holding the classes, is green")


class NoCheck(AcceptCase):
    def test_no_check_on_a_lane_whose_fresh_run_held_the_classes_queues_as_today(self):  # no_check
        tid = self.a_lane(1, run=FRESH)
        self.accept(tid, no_check=True)
        self.assertEqual(self.spawned, [])
        self.assertEqual(self.queued(), [tid])
        self.assertIn("accepted_at", self.meta_of(tid))

    def test_no_check_with_no_run_at_its_tip_is_refused_with_the_command_that_runs_the_classes(self):  # no_check repo_shape
        tid = self.a_lane(1, run=STALE)
        with self.assertRaises(c.PolicyError) as e:
            self.accept(tid, no_check=True)
        msg = str(e.exception)
        self.assertIn("sampo check --affected --hold --evidence " + tid, msg)
        for name in train.c.REPO_SHAPE_CLASSES:
            self.assertIn(name, msg)
        self.assertEqual(self.spawned, [])
        self.assertEqual(self.queued(), [])
        self.assertNotIn("(audit self)", open(self.brief(tid).path).read(), "nothing recorded")


class RepoShape(AcceptCase):
    def test_the_started_check_holds_the_four_repo_shape_classes(self):  # repo_shape
        tid = self.a_lane(1)
        self.accept(tid)
        argv = train._check_argv(self.brief(tid), None)
        self.assertEqual(argv[argv.index("check") + 1:][:1], ["--affected"])
        self.assertIn("--hold", argv)
        self.assertIn(f"--evidence", argv)
        self.assertEqual(c.REPO_SHAPE_CLASSES, ("test_docs.PluginFiles", "test_left_out.LeftOut",
                                                "test_rename_aliases.RenameAliases", "test_run.EnvClassified"))

    def test_hold_adds_the_classes_to_an_affected_run_that_links_no_test(self):  # repo_shape
        os.makedirs(os.path.join(self.repo, "plugin", "tests"), exist_ok=True)
        with open(os.path.join(self.repo, "plugin", "tests", "run.py"), "w") as f:
            f.write("# the runner\n")
        self.fm("check", "affected", "python3 -m pytest -q {tests}")
        cmd, tests, _ = cli.affected(self.p, since="HEAD", hold=True)
        for name in c.REPO_SHAPE_CLASSES:
            self.assertIn(name, cmd)
        self.assertTrue(tests)
        plain, _, _ = cli.affected(self.p, since="HEAD")
        self.assertFalse(plain and "PluginFiles" in plain, "a plain affected run is unchanged")

    def a_template_in_its_own_folder(self):
        """A by-extension template that changes directory first (as the mod's runner does) and a changed test for it."""
        os.makedirs(os.path.join(self.repo, "plugin", "tests"), exist_ok=True)
        with open(os.path.join(self.repo, "plugin", "tests", "run.py"), "w") as f:
            f.write("# the runner\n")
        os.makedirs(os.path.join(self.repo, "sub"), exist_ok=True)
        with open(os.path.join(self.repo, "sub", "a.test.ts"), "w") as f:
            f.write("// a test\n")
        self.fm("check", "affected", "python3 -m pytest -q {tests}")
        self.fm("check", "affected", "--for", ".ts", "cd sub && true {tests}")

    def test_a_template_that_changes_directory_leaves_the_held_classes_at_the_project_root(self):  # T-4409
        self.a_template_in_its_own_folder()
        cmd, _, _ = cli.affected(self.p, since="HEAD", hold=True)
        self.assertIn("cd sub && true", cmd)
        self.assertIn("PluginFiles", cmd.split("cd sub", 1)[1], "the held classes follow the template")
        r = subprocess.run(cmd, shell=True, cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, "the command after the template ran in sub/: " + r.stderr.strip()[-200:])

    def test_a_template_that_changes_directory_and_comes_last_is_joined_as_before(self):  # T-4409
        self.a_template_in_its_own_folder()
        cmd, _, _ = cli.affected(self.p, since="HEAD")
        self.assertTrue(cmd.endswith("cd sub && true sub/a.test.ts"), cmd)
        self.assertNotIn("(", cmd, "nothing follows it: no subshell")


class MergeTip(AcceptCase):
    def merged_lane(self):
        tid = self.a_lane(1)
        lane = os.path.join(self.tmp, "lane1")
        with open(os.path.join(self.repo, "m.txt"), "w") as f:
            f.write("main moved\n")
        self.git("add", "m.txt")
        self.git("commit", "-qm", "main moved")
        subprocess.run(["git", "-C", lane, "merge", "-q", "--no-ff", "-m", "merge main", "main"], check=True,
                       capture_output=True, env=dict(os.environ, GIT_AUTHOR_DATE=OLD, GIT_COMMITTER_DATE=OLD))
        return tid, lane

    def test_a_merge_commit_tip_is_checked_against_the_whole_branchs_files(self):  # merge_tip
        tid, lane = self.merged_lane()
        self.accept(tid)
        since = self.spawned[0]["since"]
        self.assertTrue(since, "a merge tip names where the branch's own work started")
        self.assertEqual(since, self.brief(tid).meta["base"])
        self.fm("check", "affected", "python3 -m pytest -q {tests}")
        lane_p = c.find_project(lane)
        _, _, whole = cli.affected(lane_p, since=since, hold=True)
        self.assertIn("f1.txt", whole)

    def test_a_plain_tip_names_no_since(self):  # merge_tip
        tid = self.a_lane(1)
        self.accept(tid)
        self.assertIsNone(self.spawned[0]["since"])

    def test_the_since_rides_the_started_checks_command_line(self):  # merge_tip
        tid, _ = self.merged_lane()
        argv = train._check_argv(self.brief(tid), "abc123")
        self.assertEqual(argv[argv.index("--since") + 1], "abc123")


RED_TS = "2026-07-01T00:00:00Z"  # a red check's own evidence line
GREEN_TS = "2026-07-02T00:00:00Z"  # a later green run in the lane


class Review(AcceptCase):
    """T-4286's review: the check's own evidence line (red too) must not read as a run that lets accept queue, one hold
    rule for accept and the land, one claim before a spawn, and the small ends."""

    def checked(self, tid, n, code, failed=(), ts=None):
        """The lane's check as the real one ends: its evidence line recorded on the task (a red one '✗ exit 1'), its JSON printed."""
        def run(argv, cwd):
            self.record_run(tid, ts or c.now(), result=c.run_result(code, "boom" if code else "ok"))
            return subprocess.run(stub(code, failed), capture_output=True, text=True), False
        with mock.patch.object(train, "_run_check", side_effect=run):
            return self.child(tid, n, ["unused"])

    def red(self, tid, failed=("test_docs.PluginFiles",)):
        self.accept(tid)
        self.checked(tid, 1, 1, failed, ts=RED_TS)

    # -- 1: a red check's evidence line is not a fresh run
    def test_accept_after_a_red_check_that_recorded_its_evidence_is_held_and_not_queued(self):  # hold
        tid = self.a_lane(1)
        self.red(tid)
        self.assertIn("✗ exit 1", "\n".join(self.brief(tid).evidence()), "the real evidence line is on the task")
        out = self.accept(tid)
        self.assertIn("held: check red: test_docs.PluginFiles", out)
        self.assertNotIn("queued first", out)
        self.assertNotIn("land started", out)
        self.assertEqual(self.queued(), [])
        self.land_started.assert_not_called()
        self.assertEqual(len(self.spawned), 1, "no second check either")

    def test_the_land_says_the_same_hold_for_that_lane(self):  # hold
        tid = self.a_lane(1)
        self.red(tid)
        r = self.land(check=False)
        self.assertIn(f"{tid}: held: check red: test_docs.PluginFiles", r.stdout + r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "f1.txt")), "left out, not trained")

    def test_a_failing_run_by_hand_is_not_a_fresh_run_either(self):  # hold
        tid = self.a_lane(1)
        self.record_run(tid, FRESH, result="✗ exit 1 · boom")
        out = self.accept(tid)
        self.assertEqual(len(self.spawned), 1, "no passing run at its tip: its own check decides\n" + out)
        self.assertEqual(self.queued(), [])

    # -- 2: a newer green run clears the hold, one rule for both
    def test_a_newer_green_run_that_held_the_classes_clears_the_red_hold_for_accept_and_the_land(self):  # hold
        tid = self.a_lane(1)
        self.red(tid)
        self.record_run(tid, GREEN_TS)
        self.assertIsNone(train._check_hold(self.brief(tid), self.tip(1), self.repo))
        out = self.accept(tid)
        self.assertEqual(self.queued(), [tid], out)
        self.assertEqual(self.meta_of(tid)["check_state"], "green", "the remedy leaves no red behind")
        self.assertEqual(len(self.spawned), 1)
        self.assertNotIn(tid, train.check_notes(c.load_briefs(self.p), self.repo))

    def test_a_newer_green_run_that_did_not_hold_the_classes_clears_nothing(self):  # hold
        tid = self.a_lane(1)
        self.red(tid)
        self.record_run(tid, GREEN_TS, cmd="python3 plugin/tests/run.py test_x")
        self.assertIn("check red", train._check_hold(self.brief(tid), self.tip(1), self.repo))

    def test_an_older_green_run_does_not_clear_a_newer_red_one(self):  # hold
        tid = self.a_lane(1, run=FRESH)
        cli.mutate(self.p, tid, lambda b: b.meta.update(check_state="red", check_tip=self.tip(1), check_failed="test_run.EnvClassified"), "x", {})
        self.record_run(tid, RED_TS, result="✗ exit 1 · boom")
        self.assertIn("test_run.EnvClassified", train._check_hold(self.brief(tid), self.tip(1), self.repo))

    # -- 3: the dead-check message is true
    def test_a_check_that_died_twice_names_the_real_way_out_not_accept(self):  # dead
        tid = self.a_lane(1)
        self.accept(tid)
        cli.mutate(self.p, tid, lambda b: b.meta.update(check_ticks=1, check_tries="2"), "x", {})
        why = train._check_hold(self.brief(tid), self.tip(1), self.repo)
        self.assertNotIn("accept", why)
        self.assertIn(f"sampo check --affected --hold --evidence {tid}", why)
        out = self.accept(tid)
        self.assertEqual(len(self.spawned), 1, out)
        self.assertIn(f"sampo check --affected --hold --evidence {tid}", out)

    def test_a_check_that_died_once_says_accept_starts_it_again_and_does(self):  # dead
        tid = self.a_lane(1)
        self.accept(tid)
        cli.mutate(self.p, tid, lambda b: b.meta.update(check_ticks=1), "x", {})
        self.assertIn(f"sampo lane accept {tid} starts it again", train._check_hold(self.brief(tid), self.tip(1), self.repo))
        self.accept(tid)
        self.assertEqual(len(self.spawned), 2)

    # -- 4: one claim before the spawn
    def test_two_accepts_at_once_start_one_check(self):  # claim
        tid = self.a_lane(1)
        before = self.brief(tid)  # the second accept read the task before the first claimed it
        self.accept(tid)
        args = types.SimpleNamespace(audit=ASKED_ROW, json=False, no_check=False)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            train.accept(self.p, before, self.repo, args)
        self.assertEqual(len(self.spawned), 1, out.getvalue())
        self.assertIn("check is running", out.getvalue())

    def test_the_claim_is_on_the_task_before_the_check_is_spawned(self):  # claim
        tid = self.a_lane(1)
        seen = []
        def spawn(p, b, main, **kw):
            seen.append(self.meta_of(tid).get("check_state"))
            return self.pid, os.path.join(self.tmp, "check.log")
        with mock.patch.object(train, "_spawn_check", side_effect=spawn):
            self.accept(tid)
        self.assertEqual(seen, ["running"])

    def test_a_check_not_started_gives_the_claim_back(self):  # claim
        tid = self.a_lane(1)
        with mock.patch.object(train, "_spawn_check", return_value=None):
            self.accept(tid)
        self.assertNotIn("check_state", self.meta_of(tid))
        self.accept(tid)
        self.assertEqual(len(self.spawned), 1, "the next accept is not told a check is running")

    # -- 5: one rule for what skips the check
    def test_a_fresh_run_that_held_the_classes_skips_the_check_and_one_that_did_not_cannot_use_no_check(self):  # no_check
        tid = self.lane(1, "f1.txt", when=OLD)
        self.record_run(tid, FRESH, cmd="python3 plugin/tests/run.py test_x")
        with self.assertRaises(c.PolicyError) as e:
            self.accept(tid, no_check=True)
        self.assertIn("sampo check --affected --hold --evidence " + tid, str(e.exception))

    def test_the_builder_brief_tells_the_builder_to_hold_the_classes(self):  # no_check
        with open(os.path.join(c.PLUGIN_ROOT, "agents", "fm-builder.md"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("sampo check --affected --hold --evidence <ID>", text)

    # -- 6: the small ends
    def test_a_check_whose_tip_moved_clears_every_check_key(self):  # tip_moved
        tid = self.a_lane(1)
        self.accept(tid)
        cli.mutate(self.p, tid, lambda b: b.meta.update(check_failed="x", check_tries="2"), "x", {})
        got = train._accept_check_child({"project": [self.p.slug, self.p.root, self.p.dir, self.p.lane], "main": self.repo,
                                         "id": tid, "tip": "0" * 40, "tries": 1, "argv": stub(0), "since": None})
        self.assertEqual(got, 0)
        self.assertEqual([k for k in self.meta_of(tid) if k.startswith("check_")], [])

    def test_a_second_accept_of_the_same_review_adds_no_duplicate_audit_row(self):  # audits
        tid = self.a_lane(1)
        self.accept(tid)
        out = self.accept(tid)
        self.assertEqual(open(self.brief(tid).path).read().count("(audit self)"), 1)
        self.assertIn("already on the brief", out)

    def test_the_scrub_drops_the_legacy_test_run_id_too(self):  # scrub
        got = train._scrub_env({"FOREMAN_TEST_RUN_ID": "a", "SAMPO_TEST_RUN_ID": "b", "FM_STALL_CACHE_S": "0", "PATH": "/x"})
        self.assertEqual(got, {"PATH": "/x"})

    def test_stop_check_ends_the_recorded_check_process_and_only_that_one(self):  # rm
        proc = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE,
                                start_new_session=True)
        self.addCleanup(proc.kill)
        tid = self.a_lane(1)
        cli.mutate(self.p, tid, lambda b: b.meta.update(check_state="running", check_pid=proc.pid,
                                                        check_ticks=train._ticks(proc.pid)), "x", {})
        self.assertTrue(train.stop_check(self.brief(tid)))
        self.assertEqual(proc.wait(timeout=20), -15)
        cli.mutate(self.p, tid, lambda b: b.meta.update(check_ticks=1), "x", {})  # a reused pid is not it
        self.assertFalse(train.stop_check(self.brief(tid)))

    def test_lane_rm_ends_the_check_and_clears_its_record(self):  # rm
        tid = self.a_lane(1)
        self.accept(tid)
        with mock.patch.object(train, "stop_check", return_value=True) as stop:
            from sampo import lanes
            lanes.remove(self.p, self.brief(tid), self.repo, drop={"why": "x"})
        stop.assert_called_once()
        self.assertEqual([k for k in self.meta_of(tid) if k.startswith("check_")], [])

    def test_a_check_with_no_tests_to_run_reports_exit_cut_off_and_failed(self):  # no_tests
        self.fm("check", "affected", "python3 -m pytest -q {tests}")
        with open(os.path.join(self.repo, "notes.txt"), "w") as f:
            f.write("a note\n")
        got = json.loads(self.fm("check", "--affected", "--json", "--since", "HEAD", check=False).stdout)
        self.assertEqual((got["exit"], got["cut_off"], got["failed"]), (0, False, []))


if __name__ == "__main__":
    unittest.main()
