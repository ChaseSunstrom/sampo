"""T-5706: `fm lane pilot` (lanepilot.py) turns one returned lane into an accepted lane or one exception line. Headless
reviewers, one per lens (haiku for an S lane, sonnet for M or a sensitive diff), record only the clean lenses against
the lane's own tree; a clean lane goes to `sampo lane accept` as a subprocess, with no lead turn. The stub claude is
test_audit_run's: it answers each lens from the env (STUB_BAD_LENS names one that reports a HIGH finding)."""
# map-links: plugin/lib/sampo/lanepilot.py
import json
import os
import subprocess
import time
import unittest
from unittest import mock

from test_audit_run import STUB
from test_lane_land import LandCase, OLD

from sampo import cli
from sampo import core as c
from sampo import lanepilot
from sampo import lanes
from sampo import train


class PilotCase(LandCase):
    tier = "S"
    name = "f1.txt"

    def setUp(self):
        super().setUp()
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir)
        with open(os.path.join(bindir, "claude"), "w") as f:
            f.write(STUB)
        os.chmod(os.path.join(bindir, "claude"), 0o755)
        self.log = os.path.join(self.tmp, "stub.log")
        env = {"PATH": bindir + os.pathsep + os.environ["PATH"], "STUB_LOG": self.log, "FOREMAN_NO_BACKGROUND": "1"}
        patch = mock.patch.dict(os.environ, env)
        patch.start()
        self.addCleanup(patch.stop)
        self.tid = self.lane(1, self.name, when=OLD, tier=self.tier)
        # the builder's own return, stamped as its SubagentStop does (the hook matches the lane's agent-<id> path, which
        # this test lane does not have)
        p = c.find_project(self.repo)
        x = c.find_brief(p, self.tid)
        x.meta["builder_returned"] = c.now()
        c.save_brief(p, x)

    def pilot(self):
        return lanepilot.pilot(c.find_project(self.repo), self.tid, self.repo)

    def calls(self):
        return [json.loads(x) for x in open(self.log)] if os.path.exists(self.log) else []

    def brief(self):
        return c.find_brief(c.find_project(self.repo), self.tid)

    def audits(self):
        return sorted({lens for lens, _, _ in self.brief().audits()})

    def accepted(self):
        return bool(self.brief().meta.get("accepted_at"))

    def arg(self, call, flag):
        return call["args"][call["args"].index(flag) + 1]

    def expire_contract(self):
        self.fm("contract", "--until", "23:59")
        pr = c.find_project(self.repo)
        c.update_meta(pr, contract={**c.read_meta(pr)["contract"], "until": time.time() - 5})


class SmallLane(PilotCase):
    def test_a_clean_s_lane_is_accepted_with_no_lead_turn_on_haiku(self):
        r = self.pilot()
        self.assertEqual(r["outcome"], "accepted", r)
        self.assertEqual(r["lenses"], ["self"])
        self.assertEqual(r["model"], "haiku")
        calls = self.calls()
        self.assertEqual([x["lens"] for x in calls], ["self"], "one reviewer per lens")
        self.assertEqual(self.arg(calls[0], "--model"), "haiku")
        self.assertEqual(self.audits(), ["self"], "the clean lens is recorded against the lane")
        self.assertTrue(self.accepted(), "sampo lane accept ran and accepted the lane")


class SelfLensFormat(PilotCase):
    def test_the_self_lens_is_briefed_on_the_verdict_format_too(self):  # T-5706 item 5
        self.pilot()
        calls = self.calls()
        self.assertEqual([x["lens"] for x in calls], ["self"])
        self.assertIn(cli._REVIEW_OUT, calls[0]["stdin"], "prep builds no self section, so the format rides the prompt")


class MidReview(PilotCase):
    def test_a_commit_landing_mid_review_stops_the_pilot_before_accept(self):  # T-5706 item 1
        lane = self.brief().meta["lane"]
        os.environ["STUB_RUN"] = json.dumps(["git", "-C", lane, "-c", "user.name=t", "-c", "user.email=t@t",
                                             "commit", "-q", "--allow-empty", "-m", "landed mid-review"])
        self.addCleanup(os.environ.pop, "STUB_RUN", None)
        r = self.pilot()
        self.assertEqual(r["outcome"], "exception", r)
        self.assertIn("tip moved during review", r["reason"])
        self.assertFalse(self.accepted(), "the accept never runs on a tip the review did not read")

    def test_an_edit_landing_mid_review_stops_the_pilot_before_accept(self):  # T-5706 item 1
        lane = self.brief().meta["lane"]
        with open(os.path.join(self.tmp, "edit.txt"), "w") as f:
            f.write("edited mid-review\n")
        os.environ["STUB_RUN"] = json.dumps(["cp", os.path.join(self.tmp, "edit.txt"), os.path.join(lane, self.name)])
        self.addCleanup(os.environ.pop, "STUB_RUN", None)
        r = self.pilot()
        self.assertEqual(r["outcome"], "exception", r)
        self.assertIn("tip moved during review", r["reason"])
        self.assertFalse(self.accepted())


class SensitiveScan(PilotCase):
    def commit_in_lane(self, rel, text):
        import subprocess
        lane = self.brief().meta["lane"]
        os.makedirs(os.path.dirname(os.path.join(lane, rel)) or lane, exist_ok=True)
        with open(os.path.join(lane, rel), "w") as f:
            f.write(text)
        subprocess.run(["git", "-C", lane, "add", "--", rel], check=True)
        subprocess.run(["git", "-C", lane, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "x"],
                       check=True)
        return train._lane_tip(self.repo, self.brief())

    def test_a_git_error_counts_as_sensitive(self):  # T-5706 item 4: fail closed
        import subprocess
        tip = train._lane_tip(self.repo, self.brief())
        with mock.patch.object(train, "_git", return_value=subprocess.CompletedProcess([], 128, "", "fatal")):
            self.assertTrue(lanepilot._sensitive(self.repo, tip), "a diff we could not read is sensitive")

    def test_an_exception_counts_as_sensitive(self):  # T-5706 item 4: fail closed
        tip = train._lane_tip(self.repo, self.brief())
        with mock.patch.object(train, "_git", side_effect=OSError("no git")):
            self.assertTrue(lanepilot._sensitive(self.repo, tip))

    def test_a_plain_lane_is_not_sensitive(self):  # T-5706 item 4: no false alarm
        tip = self.commit_in_lane("plain name.py", "x = 1\n")
        self.assertFalse(lanepilot._sensitive(self.repo, tip))

    def test_a_sensitive_path_with_a_space_is_read_nul_separated(self):  # T-5706 item 4
        tip = self.commit_in_lane("sub dir/auth.py", "x = 1\n")
        self.assertTrue(lanepilot._sensitive(self.repo, tip), "the path with a space is named whole, and it is auth")

    def test_a_token_past_the_old_200000_characters_is_still_seen(self):  # T-5706 item 4
        tip = self.commit_in_lane("big.py", "x = 1\n" * 40000 + "risky = eval(user)\n")
        self.assertTrue(lanepilot._sensitive(self.repo, tip), "the whole diff is scanned, not a 200,000-character prefix")


class SensitiveSmallLane(PilotCase):
    name = "auth.py"

    def test_a_sensitive_s_lane_adds_the_adversary_and_runs_on_sonnet(self):
        r = self.pilot()
        self.assertEqual(r["outcome"], "accepted", r)
        self.assertEqual(r["lenses"], ["self", "adversary"])
        self.assertEqual(r["model"], "sonnet")
        calls = self.calls()
        self.assertEqual(sorted(x["lens"] for x in calls), ["adversary", "self"])
        self.assertEqual({self.arg(x, "--model") for x in calls}, {"sonnet"})
        self.assertTrue(self.accepted())


class MediumLane(PilotCase):
    tier = "M"

    def test_a_clean_m_lane_gets_every_default_lens_on_sonnet_and_is_accepted(self):
        r = self.pilot()
        self.assertEqual(r["outcome"], "accepted", r)
        self.assertEqual(sorted(r["lenses"]), sorted(c.DEFAULT_LENSES))
        self.assertEqual(r["model"], "sonnet")
        calls = self.calls()
        self.assertEqual(sorted(x["lens"] for x in calls), sorted(c.DEFAULT_LENSES))
        self.assertEqual({self.arg(x, "--model") for x in calls}, {"sonnet"})
        self.assertEqual(self.audits(), sorted(c.DEFAULT_LENSES))
        self.assertTrue(self.accepted())


class ChangesNeeded(PilotCase):
    tier = "M"

    def test_a_high_finding_is_not_recorded_and_the_lane_is_not_accepted(self):
        os.environ["STUB_BAD_LENS"] = "edge"
        self.addCleanup(os.environ.pop, "STUB_BAD_LENS", None)
        r = self.pilot()
        self.assertEqual(r["outcome"], "changes", r)
        self.assertEqual(self.audits(), sorted(x for x in c.DEFAULT_LENSES if x != "edge"),
                         "only the clean lenses are recorded")
        self.assertTrue(any("edge" in f and "HIGH" in f for f in r["findings"]), r["findings"])
        self.assertFalse(self.accepted())
        self.assertNotIn("reviewed_tip", self.brief().meta, "no accept ran on it")

    def test_the_reviewers_run_in_the_checkout_and_read_the_lane_through_add_dir(self):  # round 3 (security)
        self.pilot()
        lane = os.path.realpath(self.brief().meta["lane"])
        self.assertTrue(self.calls())
        repo = os.path.realpath(self.repo)
        for x in self.calls():
            self.assertNotEqual(os.path.commonpath([os.path.realpath(x["cwd"]), repo]), repo, "cwd outside the repo")
        self.assertEqual({self.arg(x, "--add-dir") for x in self.calls()}, {lane})

    def test_a_changes_round_stamps_its_tip_and_the_next_sweep_skips_it_until_the_tip_moves(self):  # round 3 item 2
        os.environ["STUB_BAD_LENS"] = "edge"
        self.addCleanup(os.environ.pop, "STUB_BAD_LENS", None)
        self.assertEqual(self.pilot()["outcome"], "changes")
        tip = train._lane_tip(self.repo, self.brief())
        self.assertEqual(self.brief().meta.get("pilot_tip"), tip)
        n = len(self.calls())
        again = self.pilot()
        self.assertEqual(again["outcome"], "exception", again)
        self.assertIn("changes needed", again["reason"])
        self.assertEqual(len(self.calls()), n, "no reviewer is spent twice on the same tip")
        self.assertNotIn(self.tid, self.fm("lane", "pilot", "--all", "--dry").stdout)
        lane = self.brief().meta["lane"]
        subprocess.run(["git", "-C", lane, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty",
                        "-m", "next"], check=True)
        self.assertNotEqual(train._lane_tip(self.repo, self.brief()), tip)
        # the new commit is not quiet yet, so the lane waits on that; the stamp is no longer what holds it
        self.assertNotIn("changes needed", lanepilot.gate(c.find_project(self.repo), self.brief(), self.repo))


class ExceptionLines(PilotCase):
    tier = "M"

    def test_exception_land_hold_is_reported_and_nothing_runs(self):
        with mock.patch.object(lanes, "land_holds", return_value="held: guard.py is a protected core file"):
            r = self.pilot()
        self.assertEqual(r["outcome"], "exception", r)
        self.assertIn("guard.py", r["reason"])
        self.assertEqual(self.calls(), [])
        self.assertFalse(self.accepted())

    def test_exception_red_check_is_reported_and_nothing_runs(self):
        with mock.patch.object(train, "_check_hold", return_value="held: check red: test_x"):
            r = self.pilot()
        self.assertEqual(r["outcome"], "exception", r)
        self.assertIn("check red", r["reason"])
        self.assertEqual(self.calls(), [])
        self.assertFalse(self.accepted())

    def test_exception_contract_over_is_reported_and_nothing_runs(self):
        self.expire_contract()
        r = self.pilot()
        self.assertEqual(r["outcome"], "exception", r)
        self.assertIn("contract", r["reason"])
        self.assertEqual(self.calls(), [])
        self.assertFalse(self.accepted())

    def test_exception_on_the_cli_is_one_line_and_nonzero(self):
        self.expire_contract()
        p = self.fm("lane", "pilot", self.tid, check=False)
        lines = [x for x in (p.stdout + p.stderr).splitlines() if x.strip()]
        self.assertEqual(len(lines), 1, lines)
        self.assertIn(self.tid, lines[0])
        self.assertIn("exception", lines[0])
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(self.calls(), [])
        self.assertFalse(self.accepted())


class CheckRunning(PilotCase):
    def test_a_check_the_accept_started_is_accepted_with_exit_zero(self):  # T-5706 item 3
        def starts_check(tid, main):
            pr = c.find_project(self.repo)
            x = c.find_brief(pr, tid)
            x.meta.update(check_state="running", check_tip=train._lane_tip(self.repo, x))  # no pid: none is ever signalled
            c.save_brief(pr, x)
            return 0, f"  {tid}: check is running (log x); the lane joins the land when it is green"
        with mock.patch.object(lanepilot, "_accept", side_effect=starts_check):
            r = self.pilot()
        self.assertEqual(r["outcome"], "accepted", r)
        self.assertEqual(r["reason"], "check running")
        self.assertIn("accepted, check running", lanepilot.line(r))

    def test_a_running_check_on_another_tip_is_no_accept(self):  # round 3 item 6
        def other_tip(tid, main):
            pr = c.find_project(self.repo)
            x = c.find_brief(pr, tid)
            x.meta.update(check_state="running", check_tip="0" * 40)
            c.save_brief(pr, x)
            return 0, "check is running"
        with mock.patch.object(lanepilot, "_accept", side_effect=other_tip):
            r = self.pilot()
        self.assertEqual(r["outcome"], "exception", r)

    def test_an_accept_that_queues_nothing_is_still_an_exception(self):  # T-5706 item 3: no check, no accepted_at
        with mock.patch.object(lanepilot, "_accept", return_value=(0, "not queued: no")):
            r = self.pilot()
        self.assertEqual(r["outcome"], "exception", r)


class DrySweep(PilotCase):
    def stamp(self, tid, **meta):
        pr = c.find_project(self.repo)
        x = c.find_brief(pr, tid)
        x.meta.update(meta)
        c.save_brief(pr, x)

    def test_all_picks_a_lane_reviewed_at_an_older_tip(self):  # T-5706 item 3: compare to the current tip
        self.stamp(self.tid, reviewed_tip="0" * 40)
        self.assertIn(self.tid, self.fm("lane", "pilot", "--all", "--dry").stdout)

    def test_all_skips_a_lane_reviewed_at_its_current_tip(self):
        self.stamp(self.tid, reviewed_tip=train._lane_tip(self.repo, self.brief()))
        self.assertNotIn(self.tid, self.fm("lane", "pilot", "--all", "--dry").stdout)

    def test_max_takes_the_first_n_returned_lanes(self):  # T-5706 item 6
        other = self.lane(2, "f2.txt", when=OLD)
        self.stamp(other, builder_returned=c.now())
        out = self.fm("lane", "pilot", "--all", "--dry", "--max", "1").stdout
        self.assertEqual(sum(t in out for t in (self.tid, other)), 1, out)

    def test_a_lane_that_raises_does_not_stop_the_next_one(self):  # T-5706 item 6: per-lane rows
        other = self.lane(2, "f2.txt", when=OLD)
        self.stamp(other, builder_returned=c.now())
        seen = []

        def fake(p, tid, main):
            seen.append(tid)
            if len(seen) == 1:
                raise RuntimeError("boom")
            return {"tid": tid, "outcome": "accepted", "lenses": [], "findings": [], "model": "haiku",
                    "seconds": 0.0, "reason": ""}
        args = cli.build_parser().parse_args(["lane", "pilot", "--all"])
        with mock.patch.object(lanepilot, "pilot", side_effect=fake):
            with self.assertRaises(cli.UsageError) as e:
                lanepilot.cmd(c.find_project(self.repo), args, self.repo)
        self.assertEqual(sorted(seen), sorted([self.tid, other]), "the second lane still ran")
        self.assertIn("boom", str(e.exception))
        self.assertIn("accepted", str(e.exception))


class SharedSeverity(PilotCase):  # T-5706 item 6: one severity definition, MEDIUM blocking
    def test_the_lane_findings_block_on_the_same_set_as_the_pilot(self):
        self.assertIs(lanes._BLOCKING, c.BLOCKING_SEVERITIES)
        self.assertIn("MEDIUM", lanes._BLOCKING)

    def test_the_past_findings_read_a_lower_case_or_short_severity(self):
        self.assertIsNotNone(cli._FINDING.search("- medium: x.py:1 odd"))
        self.assertIsNotNone(cli._FINDING.search("- Med: x.py:1 odd"))


class DryAndAll(PilotCase):
    def test_dry_prints_the_plan_and_runs_no_reviewer(self):
        p = self.fm("lane", "pilot", self.tid, "--dry")
        self.assertIn(self.tid, p.stdout)
        self.assertIn("haiku", p.stdout)
        self.assertEqual(self.calls(), [])
        self.assertFalse(self.accepted())

    def test_the_gate_runs_no_hook_timing_bench_dry_or_not(self):  # round 3 item 5
        with mock.patch.object(lanes, "land_holds", return_value="") as lh:
            lanepilot.plan(c.find_project(self.repo), self.brief(), self.repo)
            lanepilot.gate(c.find_project(self.repo), self.brief(), self.repo)
        self.assertTrue(lh.called)
        for call in lh.call_args_list:
            self.assertIs(call.kwargs.get("timing"), False, "the bench takes the machine lock: the accept's own holds run it")

    def test_the_land_after_an_accept_still_runs_the_hook_timing_hold(self):  # round 3 (c)
        """The gate skips the bench, but the hold the land runs (train._held, what an accepted lane meets) and `lane merge`
        keep timing on: the pilot's timing=False is the gate's alone."""
        p = c.find_project(self.repo)
        with mock.patch.object(lanes, "land_holds", return_value="") as lh:
            train._held(p, self.brief(), self.repo, "0" * 40)
        self.assertEqual(lh.call_count, 1)
        self.assertIs(lh.call_args.kwargs.get("timing"), True)
        import inspect
        self.assertIs(inspect.signature(lanes.land_holds).parameters["timing"].default, True)

    def test_a_changes_round_stamps_pilot_tip_through_the_ledger(self):  # round 3 (b)
        with mock.patch.object(lanepilot, "review", return_value=({"self": "changes needed"}, [], "")):
            r = lanepilot.pilot(c.find_project(self.repo), self.tid, self.repo)
        self.assertEqual(r["outcome"], "changes")
        self.assertTrue(self.brief().meta.get("pilot_tip"))
        evs = [e for e in c.ledger_events(c.find_project(self.repo), ["pilot_changes"])]
        self.assertEqual([e.get("task") for e in evs], [self.tid])

    def test_all_plans_only_the_returned_lanes_without_a_reviewed_tip(self):
        other = self.lane(2, "f2.txt", when=OLD)  # never returned: its builder is still out
        p = self.fm("lane", "pilot", "--all", "--dry")
        self.assertIn(self.tid, p.stdout)
        self.assertNotIn(other, p.stdout)


if __name__ == "__main__":
    unittest.main()
