"""T-4286: sampo lane accept starts the lane's affected check itself when the lane has no run at its tip (detached and
niced, never waited on); green queues the lane, red leaves it out with the failing classes named and no retry until the
tip moves, a cut-off check is started once more. The check always holds the four repo-shape classes, and a merge-commit
tip is checked against the whole branch's files. The detached start is one function (train._spawn_check) these tests
replace: none of them starts a real check or leaves a process behind."""
# map-links: plugin/lib/sampo/train.py plugin/lib/sampo/cli.py
import contextlib
import fcntl
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import types
import unittest
from unittest import mock

from helpers import ForemanTestCase
from test_lane_land import LandCase, OLD

from sampo import cli
from sampo import core as c
from sampo import train

HERE = os.path.dirname(os.path.abspath(__file__))
FRESH = "2026-06-01T00:00:00Z"  # after OLD, the lane commit's date: a run at its tip
STALE = "2025-12-01T00:00:00Z"  # before it
CMD = "python3 plugin/tests/run.py test_docs.PluginFiles test_left_out.LeftOut test_rename_aliases.RenameAliases test_run.EnvClassified"
ASKED_ROW = [["self", "read the diff", "no findings"]]


def stub(exit_code=0, failed=(), cut_off=False, text=None, **extra):
    """A stand-in for the lane's `sampo check --affected --json`: prints what the real one's JSON carries (extra keys
    too, as cli's held record adds them: T-4491's deadline line)."""
    data = {"exit": exit_code, "failed": list(failed), "cut_off": cut_off, "tests": [], "changed": [], **extra}
    shown = json.dumps(data, indent=2)  # T-4418: over many lines, as cli.out prints it; one line was never the real form
    return [sys.executable, "-c", f"import sys; print({text!r} or ''); print({shown!r}); sys.exit({exit_code})"]


class AcceptCase(LandCase):
    def lane(self, *a, checked=False, **k):  # T-4767: accept's own tests start from a lane with no run on record
        return super().lane(*a, checked=checked, **k)

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


class Lenses(AcceptCase):
    """T-4487: accept refuses, recording nothing, a lane whose close would refuse it for the audits it lacks: the
    lenses its tier needs, and adversary when its own diff is security-sensitive."""

    def accept_rows(self, tid, rows):
        args = types.SimpleNamespace(audit=rows, json=False, no_check=False)
        with contextlib.redirect_stdout(io.StringIO()):
            train.accept(self.p, c.find_brief(self.p, tid), self.repo, args)

    def test_lenses_m_lane_with_one_audit_is_refused_and_records_nothing(self):  # T-4487
        tid = self.lane(1, "f1.py", when=OLD, tier="M")  # T-5715: code with no linked test scores normal; a text file reads trivial
        with self.assertRaises(c.PolicyError) as e:
            self.accept_rows(tid, [["edge", "read the diff", "no findings"]])
        self.assertIn("audit missing: --lens 'intent: <result>'", str(e.exception))
        self.assertFalse(list(self.brief(tid).audits()), "nothing recorded when accept refuses")

    def test_lenses_security_sensitive_diff_also_needs_adversary(self):  # T-4487
        tid = self.lane(1, "auth.py", when=OLD, tier="M")
        b = self.brief(tid)
        rows = [["intent", "read the diff", "fine"], ["edge", "read the diff", "no findings"]]
        gaps = train._accept_gaps(b, os.path.join(self.tmp, "lane1"), self.repo, [(r[0], r[1], r[2]) for r in rows])
        # the close's own words (_adversary_gaps): edge does not stand in for adversary on a sensitive diff
        self.assertTrue(any(g.startswith("audit missing: adversary (security-sensitive: auth.py)") for g in gaps), gaps)
        self.assertTrue(all("security-sensitive" in g for g in gaps), gaps)
        with self.assertRaises(c.PolicyError) as e:
            self.accept_rows(tid, rows)
        self.assertIn("audit missing: adversary", str(e.exception))
        self.assertFalse(list(self.brief(tid).audits()), "nothing recorded when accept refuses")

    def test_lenses_full_set_is_accepted(self):  # T-4487
        tid = self.lane(1, "auth.py", when=OLD, tier="M")
        rows = [(lens, "read the diff", "fine") for lens in ("intent", "edge", "adversary")]
        self.assertEqual(train._accept_gaps(self.brief(tid), os.path.join(self.tmp, "lane1"), self.repo, rows), [])


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

    def test_live_accept_queues_the_lane_with_no_check(self):  # T-5447: the behind suite judges a live merge
        tid = self.a_lane(1, run=STALE)
        with mock.patch.object(train, "live_mode", return_value=True):
            out = self.accept(tid)
        self.assertEqual(self.spawned, [], out)
        self.assertEqual(self.queued(), [tid])

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

    def test_a_check_cut_off_twice_is_left_out_by_the_land_not_started_a_third_time(self):  # cut_off
        # T-4404: an explicit accept starts it again (test_accept_restart); the land and the check itself never do
        tid = self.a_lane(1)
        self.accept(tid)
        self.child(tid, 1, stub(124, cut_off=True))
        self.child(tid, 1, stub(124, cut_off=True), tries=2)
        self.assertEqual(len(self.spawned), 2)
        self.assertEqual(self.meta_of(tid)["check_state"], "cut_off")
        self.assertIn("cut off or ended without a result twice", train._check_hold(self.brief(tid), self.tip(1), self.repo))
        self.assertEqual(len(self.spawned), 2)

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
        for name in c.repo_shape_classes():
            self.assertIn(name, msg)
        self.assertEqual(self.spawned, [])
        self.assertEqual(self.queued(), [])
        self.assertNotIn("(audit self)", open(self.brief(tid).path).read(), "nothing recorded")


class LiveLauncher(AcceptCase):
    """T-5359: a workflow agent's lane has builder_proc = the launching session (alive for days) and no builder_returned.
    The accept's review of its tip is its return: the same accept queues it, a commit after that un-finishes it."""

    def launched(self, n=1):
        from sampo import bus
        tid = self.a_lane(n)
        self.record_clean_run(tid)
        b = self.brief(tid)
        b.meta["builder_proc"] = f"{bus.host_id()}|{os.getpid()}|{bus._proc(os.getpid())[1]}"
        self.assertNotIn("builder_returned", b.meta)
        c.save_brief(self.p, b)
        return tid

    def unstamp(self, tid):
        """The lane fixture stamps the accept a lane merge reads (T-5968): the accept under test makes its own."""
        b = self.brief(tid)
        for k in ("accepted_tip", "reviewed_tip"):
            b.meta.pop(k, None)
        c.save_brief(self.p, b)

    def commit_in(self, n, name):
        lane = os.path.join(self.tmp, f"lane{n}")
        with open(os.path.join(lane, name), "w") as f:
            f.write("more\n")
        self.git("add", name, cwd=lane)
        later = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 60))  # after accepted_at, whatever its rounding
        subprocess.run(["git", "-C", lane, "commit", "-qm", "after the accept"], check=True, capture_output=True,
                       env=dict(os.environ, GIT_AUTHOR_DATE=later, GIT_COMMITTER_DATE=later))

    def test_the_first_accept_queues_it_and_a_commit_after_it_un_finishes_it_until_the_next_accept(self):
        tid = self.launched()
        out = self.accept(tid, no_check=True)
        self.assertNotIn("not queued", out)
        self.assertEqual(self.queued(), [tid])
        self.assertIn("accepted_at", self.meta_of(tid))
        self.commit_in(1, "more.txt")
        self.assertNotIn(tid, c.finished_lanes(self.p, c.load_briefs(self.p), 0, fresh=True), "a commit past the review")
        self.record_clean_run(tid, ts=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 120)))  # after the commit
        self.assertNotIn("not queued", self.accept(tid, no_check=True), "the next accept reviews the new tip")
        self.assertIn(tid, c.finished_lanes(self.p, c.load_briefs(self.p), 0, fresh=True))

    def test_an_accept_that_queues_nothing_leaves_no_accepted_at(self):  # T-4239: accepted_at means queued
        tid = self.launched()
        self.unstamp(tid)
        lane = os.path.join(self.tmp, "lane1")
        with open(os.path.join(lane, "wip.txt"), "w") as f:
            f.write("uncommitted\n")  # work in hand
        out = self.accept(tid, no_check=True)
        self.assertIn("not queued", out)
        self.assertEqual(self.queued(), [])
        self.assertNotIn("accepted_at", self.meta_of(tid))
        self.assertNotIn("accepted_tip", self.meta_of(tid), "T-5968 review 6: no queue, no review stamp a land could read")

    def test_a_queued_accept_stamps_the_tip_it_reviewed(self):  # T-5968 review 6
        tid = self.launched()
        self.unstamp(tid)
        self.assertNotIn("not queued", self.accept(tid, no_check=True))
        self.assertEqual(self.meta_of(tid).get("accepted_tip"), self.git("rev-parse", "HEAD", cwd=os.path.join(self.tmp, "lane1")))

    def test_a_refused_accept_marks_no_review(self):
        tid = self.launched()
        b = self.brief(tid)
        b.meta["tier"] = "M"  # T-5516: an S lane's self audit is implicit, so with none typed an M lane (intent needed) refuses
        c.save_brief(self.p, b)
        args = types.SimpleNamespace(audit=[], json=False, no_check=True)
        # T-5715: the lane's file is a plain one-line text file, which scores trivial; the claimed tier is what's tested here
        with mock.patch.dict(os.environ, {"FOREMAN_PROCESS_TIER": "off"}), self.assertRaises(cli.UsageError), \
                contextlib.redirect_stdout(io.StringIO()):
            train.accept(self.p, self.brief(tid), self.repo, args)
        self.assertNotIn(tid, c.finished_lanes(self.p, c.load_briefs(self.p), 0, fresh=True))

    def test_a_review_mark_for_another_tip_is_no_review(self):
        tid = self.launched()
        b = self.brief(tid)
        b.meta["reviewed_tip"] = "0" * 40  # stale or damaged
        c.save_brief(self.p, b)
        self.assertNotIn(tid, c.finished_lanes(self.p, c.load_briefs(self.p), 0, fresh=True))
        b.meta["reviewed_tip"] = self.tip(1)
        c.save_brief(self.p, b)
        self.assertIn(tid, c.finished_lanes(self.p, c.load_briefs(self.p), 0, fresh=True))


class RepoShape(AcceptCase):
    def test_the_started_check_holds_the_four_repo_shape_classes(self):  # repo_shape
        tid = self.a_lane(1)
        self.accept(tid)
        argv = train._check_argv(self.brief(tid), None)
        self.assertEqual(argv[argv.index("check") + 1:][:1], ["--affected"])
        self.assertIn("--hold", argv)
        self.assertIn(f"--evidence", argv)
        self.assertEqual(c.repo_shape_classes(), ("test_docs.PluginFiles", "test_left_out.LeftOut",
                                                  "test_rename_aliases.RenameAliases", "test_run.EnvClassified"))

    def test_hold_adds_the_classes_to_an_affected_run_that_links_no_test(self):  # repo_shape
        os.makedirs(os.path.join(self.repo, "plugin", "tests"), exist_ok=True)
        with open(os.path.join(self.repo, "plugin", "tests", "run.py"), "w") as f:
            f.write("# the runner\n")
        self.fm("check", "affected", "python3 -m pytest -q {tests}")
        cmd, tests, _ = cli.affected(self.p, since="HEAD", hold=True)
        for name in c.repo_shape_classes():
            self.assertIn(name, cmd)
        self.assertTrue(tests)
        plain, _, _ = cli.affected(self.p, since="HEAD")
        self.assertFalse(plain and "PluginFiles" in plain, "a plain affected run is unchanged")

    def test_hold_with_shape_green_leaves_the_classes_out_of_a_run_that_has_linked_tests(self):  # T-4563
        """The shape modules ran green at this tip (they hold the classes), so the hold run keeps only the linked tests;
        with no linked test the classes stay, so the run is never empty."""
        os.makedirs(os.path.join(self.repo, "plugin", "tests"), exist_ok=True)
        with open(os.path.join(self.repo, "plugin", "tests", "run.py"), "w") as f:
            f.write("# the runner\n")
        self.fm("check", "affected", "python3 -m pytest -q {tests}")
        only = ["plugin/tests/test_zz_shape.py"]
        cmd, tests, _ = cli.affected(self.p, since="HEAD", hold=True, only=only, shape_green=True)
        self.assertIn("test_zz_shape", cmd)
        for name in c.repo_shape_classes():
            self.assertNotIn(name, cmd)
        cmd, _, _ = cli.affected(self.p, since="HEAD", hold=True, shape_green=True)  # nothing linked: the classes stay
        for name in c.repo_shape_classes():
            self.assertIn(name, cmd)

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
    def test_a_check_that_died_twice_is_started_again_by_accept_and_names_the_hand_way_too(self):  # dead
        # T-4404: a dead second try is restartable like a cut-off one (a reboot or an OOM kill took both), as try 1
        tid = self.a_lane(1)
        self.accept(tid)
        cli.mutate(self.p, tid, lambda b: b.meta.update(check_ticks=1, check_tries="2"), "x", {})
        why = train._check_hold(self.brief(tid), self.tip(1), self.repo)
        self.assertIn("ended without a result twice", why)
        self.assertIn(f"sampo lane accept {tid} starts it again", why)
        self.assertIn(f"sampo check --affected --hold --evidence {tid}", why)
        out = self.accept(tid)
        self.assertEqual(len(self.spawned), 2, out)
        self.assertEqual(self.spawned[1]["tries"], 1)
        self.assertIn("ended without a result twice", out)
        self.assertIn("again as try 1", out)

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

    def test_the_builder_brief_leaves_the_accept_check_to_the_main_thread(self):  # no_check
        # T-4514: the builder no longer runs the wide check (it doubled a 30 to 50 minute run); accept starts it
        with open(os.path.join(c.PLUGIN_ROOT, "agents", "fm-builder.md"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("Never run `sampo check --affected` or the full `sampo check`", text)
        self.assertIn("sampo lane accept", text)

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


class Result(AcceptCase):
    """T-4418: the check's result is read in the form the real check prints it. From T-4286's landing every accept
    check (10 of 10 on 2026-11-11) ended 'cut off 2x at its deadline' after running its suite twice: sampo check
    --json prints an object over 17 lines and the reader looked for it on one."""

    def shown(self, text):
        return train._result_of(subprocess.CompletedProcess([], 0, text, ""))

    def test_a_result_printed_by_the_real_check_is_read(self):  # result
        self.fm("check", "affected", "python3 -m pytest -q {tests}")
        with open(os.path.join(self.repo, "new.txt"), "w") as f:
            f.write("a change\n")
        r = self.fm("check", "--affected", "--hold", "--json", "--since", "HEAD", check=False)
        self.assertGreater(len(r.stdout.splitlines()), 3, "the real check prints its result over many lines: " + r.stderr[-300:])
        got = self.shown("sampo: running the lane's own plugin at /lane\n" + r.stdout)
        self.assertIsNotNone(got, "the accept check found no result in what the check printed")
        self.assertEqual((got["exit"], got["cut_off"], got["failed"]), (0, False, []))

    def test_the_last_result_is_read_with_other_output_around_it(self):  # result
        first_one = json.dumps({"exit": 1, "failed": ["a.B"], "cut_off": False}, indent=2)
        last = json.dumps({"exit": 0, "failed": [], "cut_off": False, "hubs": {"x.py": {"tests": None}}}, indent=2)
        got = self.shown(f"noise\n{first_one}\n{{'not': json}}\n{last}\na line a hook printed after it\n")
        self.assertEqual(got["exit"], 0)
        self.assertEqual(got["hubs"], {"x.py": {"tests": None}})

    def test_an_object_that_is_not_a_result_and_no_object_at_all_are_no_result(self):  # result
        self.assertIsNone(self.shown("usage: sampo check\nsampo: error: unrecognized arguments: --hold\n"))
        self.assertIsNone(self.shown(json.dumps({"tests": 3}, indent=2) + "\n"), "a test's own JSON is not the check's")
        self.assertIsNone(self.shown(""))

    def test_a_green_result_over_many_lines_queues_the_lane(self):  # result
        tid = self.a_lane(1)
        self.accept(tid)
        self.child(tid, 1, stub(0, text="Ran 3 tests\nOK"))
        self.assertEqual(self.meta_of(tid)["check_state"], "green")
        self.assertEqual(self.queued(), [tid])

    def test_a_red_result_over_many_lines_names_its_classes(self):  # result
        tid = self.a_lane(1)
        self.accept(tid)
        self.child(tid, 1, stub(1, failed=["test_x.Y"]))
        m = self.meta_of(tid)
        self.assertEqual((m["check_state"], m["check_failed"]), ("red", "test_x.Y"))


class NamedTask(AcceptCase):
    """T-4418: the accept check answers for the task it is given. It took its changed files from the lane's active
    task; for a lane whose task was not focused there (T-4416's, accepted while 'planned') the list was empty and
    the check ran the four held classes and nothing of the change."""

    def test_the_check_lists_the_named_task_changes_when_it_is_not_the_lanes_active_task(self):  # named_task
        tid = self.a_lane(1)
        lane = os.path.join(self.tmp, "lane1")

        self.fm("check", "affected", "python3 -m pytest -q {tests}")

        def park(x):
            x.meta["status"] = "planned"
        cli.mutate(self.p, tid, park, "test", {})
        self.assertIsNone(c.active_brief(c.load_briefs(self.p), lane), "the lane has no active task now")
        r = self.fm("check", "--affected", "--hold", "--evidence", tid, "--json", cwd=lane, check=False)
        got = train._result_of(r)
        self.assertIsNotNone(got, r.stdout[-300:] + r.stderr[-300:])
        self.assertIn("f1.txt", got["changed"], "the lane's own commit is what the check answers for")

    def test_a_plain_affected_run_still_follows_the_active_task(self):  # named_task
        self.a_lane(1)
        self.fm("check", "affected", "python3 -m pytest -q {tests}")
        lane = os.path.join(self.tmp, "lane1")
        r = self.fm("check", "--affected", "--json", cwd=lane, check=False)
        self.assertIn("f1.txt", json.loads(r.stdout)["changed"], r.stderr[-300:])


def probe(d, gate=None):
    """A stand-in check (T-4491): it holds the file `busy` in d while it runs, and leaves `overlap` there when another one
    finds it held; with a gate file it waits for that file to exist before it ends. Then a green result."""
    code = "\n".join([
        "import json, os, time",
        f"d, gate = {d!r}, {gate!r}",
        "try:",
        "    os.close(os.open(os.path.join(d, 'busy'), os.O_CREAT | os.O_EXCL))",
        "except FileExistsError:",
        "    open(os.path.join(d, 'overlap'), 'w').close()",
        "else:",
        "    end = time.time() + 30",
        "    while gate and not os.path.exists(gate) and time.time() < end:",
        "        time.sleep(0.05)",
        "    time.sleep(0.2)",
        "    os.remove(os.path.join(d, 'busy'))",
        "print(json.dumps({'exit': 0, 'failed': [], 'cut_off': False, 'tests': [], 'changed': []}))",
    ])
    return [sys.executable, "-c", code]


class Queued(AcceptCase):
    """T-4491: sampo lane accept runs at most SAMPO_LANE_CHECKS checks at a time (default 2). A further check waits as
    'check queued' (its own detached process, no session open) and starts when a running one ends."""

    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ, {"SAMPO_LANE_CHECKS": "1"})
        env.start()
        self.addCleanup(env.stop)

    def hold(self, n=1):
        """n slots taken the way another lane's check process takes one: an flock on its own open file."""
        d = os.path.join(self.p.dir, "check")
        os.makedirs(d, exist_ok=True)
        held = [open(os.path.join(d, f"turn-{i}.lock"), "a") for i in range(n)]
        for f in held:
            fcntl.flock(f, fcntl.LOCK_EX)
        self.addCleanup(lambda: [f.close() for f in held])
        return held

    def start(self, tid, n, argv):
        t = threading.Thread(target=self.child, args=(tid, n, argv), daemon=True)
        t.start()
        return t

    def notes(self):
        return train.check_notes(c.load_briefs(self.p), self.repo)

    def until(self, done, seconds=30):
        end = time.time() + seconds
        while time.time() < end and not done():
            time.sleep(0.05)

    def test_three_accepts_with_one_slot_are_queued_and_run_one_check_at_a_time(self):  # queued
        tids = [self.a_lane(n, name=f"f{n}.txt") for n in (1, 2, 3)]
        for tid in tids:
            self.accept(tid)
        held = self.hold(1)  # the one slot is another lane's check: all three wait behind it
        threads = [self.start(tid, n, probe(self.tmp)) for n, tid in zip((1, 2, 3), tids)]
        self.until(lambda: all(self.notes().get(t) == "check queued" for t in tids))
        self.assertEqual([self.notes().get(t) for t in tids], ["check queued"] * 3, "sampo lane list says queued")
        held[0].close()
        for t in threads:
            t.join(60)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "overlap")), "two checks ran at once with one slot")
        self.assertEqual([self.meta_of(t)["check_state"] for t in tids], ["green"] * 3)

    def test_with_two_slots_a_third_check_is_queued_behind_the_two_running(self):  # queued
        os.environ.pop("SAMPO_LANE_CHECKS", None)  # the default: two at a time (restored by the setUp's patch)
        tids = [self.a_lane(n, name=f"f{n}.txt") for n in (1, 2, 3)]
        for tid in tids:
            self.accept(tid)
        gate = os.path.join(self.tmp, "gate")
        held = self.hold(1)  # one slot is taken elsewhere: the first of ours runs, the other two queue
        threads = [self.start(tid, n, probe(self.tmp, gate)) for n, tid in zip((1, 2, 3), tids)]
        want = ["checking", "check queued", "check queued"]
        self.until(lambda: sorted(self.notes().get(t, "") for t in tids) == sorted(want))
        self.assertEqual(sorted(self.notes().get(t, "") for t in tids), sorted(want))
        open(gate, "w").close()
        held[0].close()
        for t in threads:
            t.join(60)
        self.assertEqual([self.meta_of(t)["check_state"] for t in tids], ["green"] * 3, "the queued ones start when one ends")


class Deadline(AcceptCase):
    """T-4491: a check stopped at its deadline says so in one line (the limit, the classes that ran of how many, the
    rerun with a larger one), and the lane's record keeps that line."""

    def test_a_check_stopped_at_its_deadline_records_the_deadline_line(self):  # deadline
        tid = self.a_lane(1)
        self.accept(tid)
        line = (f"stopped at its deadline (540 s): 315 of 1685 classes ran; rerun with a larger one: "
                f"sampo check --affected --timeout 1200 --hold --evidence {tid}")
        self.child(tid, 1, stub(124, cut_off=True, deadline=line), tries=2)
        self.assertEqual(self.meta_of(tid)["check_state"], "cut_off")
        self.assertIn(line, open(self.brief(tid).path).read())
        self.assertEqual(self.meta_of(tid)["check_cut_timeout"], "1200", "T-4767: the land's hold line sizes its rerun from it")

    def test_the_deadline_line_names_the_limit_the_classes_that_ran_and_the_larger_rerun(self):  # deadline
        line = cli.deadline_line(600, 540, {"classes": 1685, "n_not_run": 1370}, hold=True, evidence="T-1")
        for part in ("stopped at its deadline (540 s)", "315 of 1685 classes ran", "sampo check --affected --timeout 1200",
                     "--hold", "--evidence T-1"):
            self.assertIn(part, line)
        self.assertNotIn("\n", line, "one line")

    def test_a_deadline_with_no_run_record_still_names_the_limit_and_the_rerun(self):  # deadline
        line = cli.deadline_line(600, 540, None)
        self.assertIn("(540 s)", line)
        self.assertIn("--timeout 1200", line)
        self.assertIn("classes ran not recorded", line)

    def test_a_run_ended_from_outside_is_not_called_a_deadline_and_suggests_no_larger_timeout(self):  # deadline
        for code in (143, 130):
            line = cli.deadline_line(600, 540, {"classes": 1685, "n_not_run": 1370}, hold=True, evidence="T-1", code=code)
            self.assertIn(f"ended from outside (exit {code})", line)
            self.assertIn("315 of 1685 classes ran", line)
            self.assertNotIn("deadline", line)
            self.assertNotIn("--timeout", line, "a larger timeout would not have kept it from being killed")
            self.assertNotIn("\n", line)
        self.assertIn("stopped at its deadline", cli.deadline_line(600, 540, None, code=124))

    def test_no_limit_says_so_instead_of_failing(self):  # deadline
        line = cli.deadline_line(0, None, {"classes": 10, "n_not_run": 4}, code=124)
        self.assertIn("no limit", line)
        self.assertIn("6 of 10 classes ran", line)


class QueuedFlag(AcceptCase):
    """T-4491: check_queued is a state of waiting: a check that has its turn never carries it, however it ends."""

    def stale(self, tid):
        cli.mutate(self.p, tid, lambda b: b.meta.update(check_queued="1"), "check", {})

    def test_a_green_check_leaves_no_queued_flag(self):  # queued
        tid = self.a_lane(1)
        self.accept(tid)
        self.stale(tid)
        self.child(tid, 1, stub(0))
        self.assertNotIn("check_queued", self.meta_of(tid))

    def test_a_red_check_leaves_no_queued_flag(self):  # queued
        tid = self.a_lane(1)
        self.accept(tid)
        self.stale(tid)
        self.child(tid, 1, stub(1, ["test_x.X"]))
        self.assertEqual(self.meta_of(tid)["check_state"], "red")
        self.assertNotIn("check_queued", self.meta_of(tid))

    def test_a_restarted_check_claims_without_the_queued_flag(self):  # queued
        tid = self.a_lane(1)
        self.accept(tid)
        self.stale(tid)
        b = cli.need_brief(self.p, tid)
        with mock.patch.object(train, "_spawn_check", return_value=(1234, "/x.log")):
            train._start_check(self.p, b, self.repo, self.tip(1), None, 2)
        self.assertNotIn("check_queued", self.meta_of(tid))

    def test_the_number_of_slots_is_bounded(self):  # queued
        with mock.patch.dict(os.environ, {"SAMPO_LANE_CHECKS": "100000"}):
            self.assertEqual(train.lane_checks(), 64)
        with mock.patch.dict(os.environ, {"SAMPO_LANE_CHECKS": "0"}):
            self.assertEqual(train.lane_checks(), 1)


class ChildPath(unittest.TestCase):
    """T-4455: the detached child is started with the trunk's lib on PYTHONPATH so `-m sampo.train` imports; what the
    child then runs (a lane's check, the ship's tests) must not inherit that entry: with it, every Python the tests
    start imported the trunk's sampo, and two classes went red in each lane check that selected them."""

    def test_the_childs_pythonpath_entry_is_dropped_and_the_callers_kept(self):
        lib = os.path.dirname(os.path.dirname(os.path.abspath(train.__file__)))
        for had, want in ((lib + os.pathsep, None), (lib + os.pathsep + "/opt/x", "/opt/x"),
                          ("/opt/x" + os.pathsep + lib, "/opt/x"), ("/opt/x", "/opt/x")):
            with self.subTest(had=had), mock.patch.dict(os.environ, {"PYTHONPATH": had}):
                train._own_path_off()
                self.assertEqual(os.environ.get("PYTHONPATH"), want)

    def test_a_check_the_child_runs_sees_no_pythonpath_of_the_childs(self):
        lib = os.path.dirname(os.path.dirname(os.path.abspath(train.__file__)))
        arg = json.dumps({"probe_pythonpath": True})
        r = subprocess.run([sys.executable, "-m", "sampo.train", arg], capture_output=True, text=True, timeout=60,
                           env=dict(os.environ, PYTHONPATH=lib + os.pathsep))
        self.assertEqual(r.stdout.strip(), "None", r.stderr)


class RerunHint(unittest.TestCase):
    """T-4864: the deadline line's rerun names a timeout larger than the limit that was hit; it doubled the asked
    --timeout, so a 600 s default scaled to 1200 s under load said 'rerun with --timeout 1200' (T-4892 fixed the line
    too: these pin it for a load-scaled limit as well as a sized one)."""

    def test_rerun_hint_is_larger_than_the_scaled_limit_hit(self):  # rerun_hint
        line = cli.deadline_line(600, 1200, {"classes": 1921, "n_not_run": 1817}, hold=True, evidence="T-1")
        self.assertIn("(1200 s)", line)
        self.assertIn("--timeout 2400", line)

    def test_rerun_hint_after_a_sized_limit(self):  # rerun_hint
        line = cli.deadline_line(600, 1437.5, None)
        hint = float(line.split("--timeout ", 1)[1].split()[0])
        self.assertGreater(hint, 1437.5)
        self.assertEqual(hint, int(hint), "a whole number of seconds")

    def test_rerun_hint_still_doubles_an_asked_timeout_over_its_limit(self):  # rerun_hint
        self.assertIn("--timeout 1200", cli.deadline_line(600, 540, None))
        self.assertIn("--timeout 1200", cli.deadline_line(0, None, None))


SRC = "def add(a, b):\n    return a + b\n"


class SizedDeadline(ForemanTestCase):
    """T-4864: sampo check --affected with no --timeout sizes its deadline by T-4892's rule (cli.sized_limit over run.py
    --estimate: the selection's recorded seconds over the CPU slots, never under the fixed 600 s), as the lane's own
    --hold check does; an explicit --timeout wins for a foreground run; no record keeps the fixed 600 s. The layout is
    Sampo's own (plugin/tests/run.py with the suite beside it), as test_close_affected.CloseSized's."""

    def setUp(self):
        super().setUp()
        self.fm("init")
        tests = os.path.join(self.repo, "plugin", "tests")
        os.makedirs(tests)
        shutil.copy(os.path.join(HERE, "run.py"), os.path.join(tests, "run.py"))
        self.times = os.path.join(self.tmp, "times.json")
        self.write("calc.py", SRC)
        self.write("plugin/tests/test_calc.py",
                   "import os, sys, unittest\nsys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname("
                   "os.path.abspath(__file__)))))\nimport calc\n\n\nclass Calc(unittest.TestCase):\n    def test_add(self):\n"
                   "        self.assertEqual(calc.add(1, 2), 3)\n")
        for i in range(4):
            self.write(f"plugin/tests/test_other{i}.py", "import unittest\n\n\nclass Other(unittest.TestCase):\n"
                       "    def test_it(self):\n        pass\n")
        self.write(".gitignore", "__pycache__/\n")
        subprocess.run(["git", "-C", self.repo, "add", "-A"], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", "suite"], check=True)
        self.fm("check", "affected", "python3 plugin/tests/run.py {names}")
        self.p = c.find_project(self.repo)
        self.tid = self.fm_json("task", "new", "Change add", "--type", "FEATURE", "--tier", "S", "--ac", "works :: true",
                                "--step", "s", "--scope", "calc.py")["id"]
        self.fm("focus", self.tid)
        self.write("calc.py", SRC + "# tidy\n")

    def write(self, rel, text):
        with open(os.path.join(self.repo, rel), "w") as f:
            f.write(text)

    def record(self, calc, others, cpus=1):
        with open(self.times, "w") as f:
            json.dump({"test_calc.Calc": calc, **{f"test_other{i}.Other": others for i in range(4)}}, f)
        self.host = {"FM_TEST_TIMES": self.times, "FOREMAN_HOST": f"load=0,cpus={cpus}", "FOREMAN_SENTINEL": "off",
                     "SAMPO_SLOTS": str(cpus)}

    def check(self, *extra):
        """sampo check --affected --json in this process, the run.py command stubbed green: (exit, result, text, limits)."""
        seen, real = [], c.run_command

        def run(root, cmd, timeout_s, env=None, **kw):
            if "run.py" not in cmd:
                return real(root, cmd, timeout_s, env=env, **kw)
            seen.append(timeout_s)
            return 0, "Ran 1 test\nOK\n"
        out, err, cwd = io.StringIO(), io.StringIO(), os.getcwd()
        os.chdir(self.repo)
        try:
            with mock.patch.dict(os.environ, self.host), mock.patch.object(c, "run_command", side_effect=run), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    code = cli.main(["check", "--affected", "--json", *extra])
                except SystemExit as e:
                    code = e.code
        finally:
            os.chdir(cwd)
        got = train._result_of(subprocess.CompletedProcess([], 0, out.getvalue(), ""))
        return code, got, out.getvalue() + err.getvalue(), seen

    def test_sized_deadline_from_the_recorded_seconds(self):  # sized_deadline
        self.record(calc=800.0, others=300.0)  # the selection is calc: 800 s on one slot x 1.5 = 1200 s
        code, got, text, seen = self.check()
        self.assertEqual(code, 0, text)
        self.assertEqual(seen, [1200], text)

    def test_sized_deadline_never_below_the_fixed_one(self):  # sized_deadline
        self.record(calc=2.0, others=100.0)  # 2 s of work: the fixed 600 s is more, and a run never stops sooner
        code, got, text, seen = self.check()
        self.assertEqual(seen, [600], text)

    def test_sized_deadline_explicit_timeout_wins(self):  # sized_deadline
        self.record(calc=800.0, others=300.0)
        code, got, text, seen = self.check("--timeout", "700")
        self.assertEqual(seen, [700], text)

    def test_sized_deadline_with_no_record_keeps_the_fixed_one(self):  # sized_deadline
        with open(self.times, "w") as f:
            json.dump({}, f)
        self.host = {"FM_TEST_TIMES": self.times, "FOREMAN_HOST": "load=0,cpus=1", "FOREMAN_SENTINEL": "off",
                     "SAMPO_SLOTS": "1"}
        code, got, text, seen = self.check()
        self.assertEqual((code, seen), (0, [600]), text)

    def test_sized_deadline_is_the_hold_checks_rule_not_a_second_one(self):  # sized_deadline
        self.record(calc=800.0, others=300.0)
        code, got, text, seen = self.check("--hold", "--evidence", self.tid)
        self.assertEqual(seen, [1200], "the lane's own check and a bare run size the same way: cli.sized_limit")


if __name__ == "__main__":  # T-4491: last, so every class above is defined when it runs
    unittest.main()
