"""T-5708: a sampo:fm-builder's return starts one detached lane pilot per lane (a reviewer's stop starts none, a second
return while the pilot lives starts none, a dead claim is retaken, FOREMAN_NO_BACKGROUND, pause and the switch stop it);
the pilot's result is one pilot_done event and one line to the lead; `sampo lane stats` prints the builder-done to
landed median and the lead turns per landed lane. Review round 1: an absent verb is quiet; the verb is the plugin's own
bin run by this python; a return is reported once (the claim carries the return it serves); a failed settle still sends
the lead its line; a hung verb's children die with it; the answer is the last JSON line; pilot.log is capped; the pilot
runs in the main checkout; the landing metric counts the lead's kinds in a window and splits comma-list task ids."""
import os
import subprocess
import sys
import time
from unittest import mock

from helpers import ForemanTestCase

from sampo import core as c
from sampo import hooks
from sampo import lanes
from sampo import pilotsig

AGENT = "a1b2c3d4e5f6"  # a builder's lane is .claude/worktrees/agent-<its agent id>
TID = "T-0001"
RETURNED = "2026-11-22T10:00:00Z"  # the builder_returned stamp a lane's brief carries when its builder stops
REAL_VERB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "sampo")  # the plugin's own
STUB = (  # the lane pilot verb, stubbed: a grandchild that outlives it (STUB_PIDFILE), its answer, its stderr, a sleep, its exit
    "import os, subprocess, sys, time\n"
    "if os.environ.get('STUB_PIDFILE'):\n"
    "    g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
    "    open(os.environ['STUB_PIDFILE'], 'w').write(str(g.pid))\n"
    "sys.stdout.write(os.environ.get('STUB_OUT', '') + '\\n')\n"
    "sys.stderr.write(os.environ.get('STUB_ERR', ''))\n"
    "time.sleep(float(os.environ.get('STUB_SLEEP', '0')))\n"
    "sys.exit(int(os.environ.get('STUB_RC', '0')))\n"
)


class LanePilot(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.fm("task", "new", "Build it", "--type", "FEATURE", "--tier", "S", "--ac", "works :: true", "--step", "do it")
        self.p = c.find_project(self.repo)
        b = c.find_brief(self.p, TID)
        b.meta["lane"] = os.path.join(self.tmp, "worktrees", f"agent-{AGENT}")
        b.meta["builder_returned"] = RETURNED
        c.save_brief(self.p, b)
        self.pl = {"agent_type": "sampo:fm-builder", "agent_id": AGENT, "cwd": self.repo}
        patch = mock.patch.dict(os.environ)  # the spawn tests need the background on; restored after each test
        patch.start()
        self.addCleanup(patch.stop)
        self.stub_bin = os.path.join(self.tmp, "stub_sampo.py")
        with open(self.stub_bin, "w", encoding="utf-8") as f:
            f.write(STUB)
        os.environ["FOREMAN_PILOT_VERB"] = self.stub_bin  # the verb the pilot runs in these tests (no PATH stub)
        self.nothing = os.path.join(self.tmp, "no-such-verb.py")

    def background(self, on=True):
        if on:
            os.environ.pop("FOREMAN_NO_BACKGROUND", None)
        else:
            os.environ["FOREMAN_NO_BACKGROUND"] = "1"

    def stub(self, out="", rc=0, err="", sleep=0, pidfile=None):
        os.environ["STUB_OUT"], os.environ["STUB_RC"] = out, str(rc)
        os.environ["STUB_ERR"], os.environ["STUB_SLEEP"] = err, str(sleep)
        if pidfile:
            os.environ["STUB_PIDFILE"] = pidfile
        else:
            os.environ.pop("STUB_PIDFILE", None)

    def claim(self, pid, served=""):
        b = c.find_brief(self.p, TID)
        b.meta["pilot_pid"] = str(pid)  # text on disk, as spawn writes it
        if served:
            b.meta["pilot_for"] = served
        c.save_brief(self.p, b)

    def meta(self, key):
        return c.find_brief(self.p, TID).meta.get(key)

    def claimed(self):
        return self.meta("pilot_pid")

    def pilot_done(self):
        return c.ledger_events(self.p, ("pilot_done",))

    def live_pid(self):  # a process whose command line names the pilot: it lives until the test ends
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "sampo.pilotsig"])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        end = time.monotonic() + 10  # between the fork and the exec the command line is still the test's: wait for it
        while time.monotonic() < end:
            try:
                with open(f"/proc/{proc.pid}/cmdline", "rb") as f:
                    if b"sampo.pilotsig" in f.read():
                        break
            except OSError:
                pass
            time.sleep(0.01)
        return proc.pid

    def gone_pid(self):  # a pid that has exited and been reaped
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        return proc.pid

    def gone(self, pid, wait=5.0):  # gone, or a zombie nobody has reaped yet: it no longer runs (the kill takes a moment)
        end = time.monotonic() + wait
        while True:
            try:
                with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
                    state = f.read().rsplit(")", 1)[1].split()[0]
                if state in ("Z", "X"):
                    return True
            except FileNotFoundError:
                return True
            if time.monotonic() > end:
                return False
            time.sleep(0.05)

    def lines(self):
        path = os.path.join(self.p.dir, "alerts.jsonl")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            return f.read().splitlines()

    # --- the trigger: the hook starts one pilot per return, builders only

    def test_the_return_of_a_builder_starts_its_pilot_and_a_reviewers_stop_starts_none(self):
        starts = []
        with mock.patch.object(pilotsig, "spawn", side_effect=lambda p, tid: starts.append(tid)):
            hooks.subagent_stop(dict(self.pl, agent_type="sampo:fm-reviewer"))
            self.assertEqual(starts, [], "a reviewer's stop started a pilot")
            hooks.subagent_stop(self.pl)
        self.assertEqual(starts, [TID])

    def test_the_pilot_is_asked_after_the_close_fields_are_taken(self):  # review 6
        order = []
        with mock.patch.object(hooks, "_close_fields", side_effect=lambda pl: order.append("close")), \
                mock.patch.object(pilotsig, "returned", side_effect=lambda pl: order.append("pilot") or []):
            hooks.subagent_stop(self.pl)
        self.assertEqual(order, ["close", "pilot"])

    def test_a_second_return_while_its_pilot_lives_spawns_none(self):
        live = self.live_pid()
        self.claim(live)
        self.assertTrue(pilotsig.alive(self.claimed()), "the live pilot is not seen as live")  # the precondition, named
        self.background()
        with mock.patch.object(subprocess, "Popen", side_effect=AssertionError("a second pilot for one lane")):
            self.assertIsNone(pilotsig.spawn(self.p, TID))
        self.assertEqual(self.claimed(), str(live), "the live pilot's claim was overwritten")

    def test_a_dead_claim_is_retaken(self):
        self.claim(self.gone_pid())
        self.background()
        proc = mock.Mock(pid=777)
        with mock.patch.object(subprocess, "Popen", return_value=proc) as popen:
            self.assertEqual(pilotsig.spawn(self.p, TID), 777)
        self.assertEqual(popen.call_count, 1)
        self.assertEqual(self.claimed(), "777")
        self.assertEqual(self.meta("pilot_for"), RETURNED, "the claim does not say which return it serves")
        argv = popen.call_args[0][0]
        self.assertEqual(argv[-3:], ["-m", "sampo.pilotsig", TID])  # the nice wrapper comes before the module
        self.assertEqual(popen.call_args[1]["stdin"], subprocess.DEVNULL)
        self.assertTrue(popen.call_args[1]["start_new_session"], "the pilot is not detached")

    def test_no_background_pause_or_the_switch_starts_no_pilot(self):
        with mock.patch.object(subprocess, "Popen", side_effect=AssertionError("started")):
            self.background(on=False)
            self.assertIsNone(pilotsig.spawn(self.p, TID))
            self.background()
            with mock.patch.object(c, "panicked", return_value=True):
                self.assertIsNone(pilotsig.spawn(self.p, TID))
            os.environ["SAMPO_LANE_PILOT"] = "off"  # (the switch is read by the pilot's own name, SAMPO_LANE_PILOT: mirrored, read the same way)
            self.assertIsNone(pilotsig.spawn(self.p, TID))
        self.assertIsNone(self.claimed())

    def test_the_pilot_runs_in_the_main_checkout(self):  # review 6
        main = os.path.join(self.tmp, "main")
        os.makedirs(main, exist_ok=True)
        self.background()
        with mock.patch.object(c, "main_worktree", return_value=main), \
                mock.patch.object(subprocess, "Popen", return_value=mock.Mock(pid=779)) as popen:
            pilotsig.spawn(self.p, TID)
        self.assertEqual(popen.call_args[1]["cwd"], main)

    def test_a_pilot_that_died_mid_report_is_retaken_and_a_reported_return_is_not(self):  # review 3
        self.background()
        self.claim(self.gone_pid(), served=RETURNED)  # its verb answered; the pilot died before it settled
        with mock.patch.object(subprocess, "Popen", return_value=mock.Mock(pid=777)) as popen:
            self.assertEqual(pilotsig.spawn(self.p, TID), 777)
        self.assertEqual(popen.call_count, 1, "the dead pilot's return was not retaken")
        b = c.find_brief(self.p, TID)
        b.meta["pilot_done_for"] = RETURNED  # a pilot that settled before it died: the return is reported
        b.meta.pop("pilot_pid", None)
        c.save_brief(self.p, b)
        with mock.patch.object(subprocess, "Popen", side_effect=AssertionError("a second pilot for one return")):
            self.assertIsNone(pilotsig.spawn(self.p, TID))

    def test_the_pilot_log_is_rotated_past_its_cap(self):  # review 6
        log = os.path.join(self.p.dir, "pilot.log")
        with open(log, "w", encoding="utf-8") as f:
            f.write("x" * (pilotsig.LOG_MAX + 1))
        self.background()
        with mock.patch.object(subprocess, "Popen", return_value=mock.Mock(pid=778)):
            pilotsig.spawn(self.p, TID)
        self.assertTrue(os.path.exists(log + ".1"), "the old log was not kept aside")
        self.assertLess(os.path.getsize(log), pilotsig.LOG_MAX)

    # --- the verb: the plugin's own bin, its absence is quiet, its answer is the last JSON line, a hung one is cut off

    def test_the_verb_is_the_plugins_own_bin_run_by_this_python_not_the_path(self):  # review 2
        os.environ.pop("FOREMAN_PILOT_VERB", None)
        self.assertTrue(os.path.isfile(REAL_VERB), REAL_VERB)
        with mock.patch.object(subprocess, "Popen", side_effect=FileNotFoundError("stopped here")) as popen:
            pilotsig._verb(self.p, TID)
        self.assertEqual(popen.call_args[0][0], [sys.executable, REAL_VERB, "lane", "pilot", TID, "--json"])

    def test_a_verb_that_is_not_there_yet_stays_quiet(self):  # review 1
        self.stub(out="", rc=2, err="sampo: error: argument command: invalid choice: 'pilot' (choose from 'task', 'lane')\n")
        self.claim(os.getpid(), served=RETURNED)
        self.background(on=False)
        res = pilotsig.run(self.p, TID)
        self.assertEqual(res["outcome"], "absent")
        self.assertEqual(self.pilot_done(), [], "an absent verb wrote an exception event")
        self.assertEqual(self.lines(), [], "an absent verb sent the lead a line")
        self.assertIsNone(self.claimed(), "the claim was not released, so the next return cannot retry")
        self.assertIsNone(self.meta("pilot_done_for"))
        self.assertNotIn("lane pilot", c.find_brief(self.p, TID).section("Log"))

    def test_the_last_json_line_is_the_answer(self):  # review 6
        self.stub(out='a line of the verb\'s own log\n{"tid": "T-0001", "outcome": "queued", "rounds": 2, "why": ""}')
        self.claim(os.getpid(), served=RETURNED)
        self.background(on=False)
        pilotsig.run(self.p, TID)
        events = self.pilot_done()
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0]["data"]["outcome"], events[0]["data"]["rounds"]), ("queued", 2))

    def test_a_hung_verb_is_cut_and_its_children_die_with_it(self):  # review 5
        pidfile = os.path.join(self.tmp, "grandchild.pid")
        self.stub(sleep=60, pidfile=pidfile)
        self.claim(os.getpid(), served=RETURNED)
        self.background(on=False)
        t0 = time.monotonic()
        with mock.patch.object(pilotsig, "VERB_WALL_S", 5):
            res = pilotsig.run(self.p, TID)
        self.assertLess(time.monotonic() - t0, 50, "the verb was not cut off")
        self.assertEqual(res["outcome"], "exception")
        self.assertIn("no answer in 5 s", res["why"])
        with open(pidfile, encoding="utf-8") as f:
            grandchild = int(f.read())
        self.assertTrue(self.gone(grandchild), "the verb's child outlived its cut-off verb")

    def test_report_queued_after_rounds_is_one_event_and_one_line(self):
        self.stub(out='{"tid": "T-0001", "outcome": "queued", "rounds": 2, "why": ""}')
        self.claim(os.getpid(), served=RETURNED)
        self.background(on=False)
        pilotsig.run(self.p, TID)
        events = self.pilot_done()
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0]["task"], events[0]["data"]["outcome"], events[0]["data"]["rounds"]),
                         (TID, "queued", 2))
        self.assertEqual(len(self.lines()), 1)
        self.assertIn("fixed in 2 rounds", self.lines()[0])
        self.assertIsNone(self.claimed(), "the pilot kept its claim after it reported")
        self.assertEqual(self.meta("pilot_done_for"), RETURNED)

    def test_a_second_run_for_the_same_return_writes_no_second_event(self):  # review 3
        self.stub(out='{"tid": "T-0001", "outcome": "queued", "rounds": 1, "why": ""}')
        self.background(on=False)
        self.claim(os.getpid(), served=RETURNED)
        pilotsig.run(self.p, TID)
        self.claim(os.getpid(), served=RETURNED)  # the same return's pilot once more
        pilotsig.run(self.p, TID)
        self.assertEqual(len(self.pilot_done()), 1)
        self.assertEqual(len(self.lines()), 1)

    def test_a_failed_settle_still_sends_the_lead_its_line(self):  # review 4
        self.stub(out='{"tid": "T-0001", "outcome": "queued", "rounds": 2, "why": ""}')
        self.claim(os.getpid(), served=RETURNED)
        self.background(on=False)
        with mock.patch.object(c, "log_event", side_effect=RuntimeError("the ledger is full")):
            res = pilotsig.run(self.p, TID)
        self.assertEqual(res["outcome"], "queued")
        self.assertEqual(len(self.lines()), 1, "the lead got no line")
        self.assertIn("fixed in 2 rounds", self.lines()[0])

    def test_report_exception_when_the_verb_file_is_missing_names_why_and_logs_on_the_task(self):
        os.environ["FOREMAN_PILOT_VERB"] = self.nothing
        self.claim(os.getpid(), served=RETURNED)
        self.background(on=False)
        pilotsig.run(self.p, TID)
        events = self.pilot_done()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["data"]["outcome"], "exception")
        self.assertTrue(events[0]["data"]["why"].startswith("pilot could not run: "), events[0]["data"])
        self.assertIn("pilot could not run", c.find_brief(self.p, TID).section("Log"))
        self.assertEqual(len(self.lines()), 1)

    def test_report_exception_on_a_nonzero_exit_names_the_exit_and_the_stderr(self):
        self.stub(out="", rc=3, err="boom\n")
        self.claim(os.getpid(), served=RETURNED)
        self.background(on=False)
        pilotsig.run(self.p, TID)
        why = self.pilot_done()[0]["data"]["why"]
        self.assertIn("exit 3", why)
        self.assertIn("boom", why)

    def test_report_the_pilot_runs_as_a_module_and_reports_once(self):
        self.stub(out='{"tid": "T-0001", "outcome": "queued", "rounds": 0, "why": ""}')
        self.background(on=False)  # the child's own notify spawns nothing either
        lib = os.path.dirname(os.path.dirname(os.path.abspath(pilotsig.__file__)))
        env = dict(os.environ, PYTHONPATH=lib)
        proc = subprocess.Popen([sys.executable, "-m", "sampo.pilotsig", TID], cwd=self.repo, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out, _ = proc.communicate(timeout=120)
        self.assertEqual(proc.returncode, 0, out)
        events = self.pilot_done()
        self.assertEqual(len(events), 1, out)
        self.assertEqual(events[0]["data"]["outcome"], "queued", events[0]["data"]["why"])

    # --- the metrics: `sampo lane stats` (lanes.landing)

    def test_report_lane_stats_prints_the_builder_done_to_landed_median_and_the_lead_turns(self):
        t0 = int(time.time()) - 3600
        clock = {"t": t0}
        with mock.patch.object(c, "now", lambda: c.iso(clock["t"])):
            c.log_event(self.p, "builder_mark", task=TID, data={"returned": True, "agent": AGENT}, session="sess-lead")
            clock["t"] = t0 + 300
            c.log_event(self.p, "subagent", task=TID, data={}, session="sess-lead")  # a lead turn on the lane
            clock["t"] = t0 + 360
            c.log_event(self.p, "pilot_done", task=TID, data={"outcome": "queued"}, session=f"pilot-{TID}")
            clock["t"] = t0 + 600
            c.log_event(self.p, "lane_landed", task=TID, data={"tasks": [TID]}, session="sess-lead")
        out = self.fm("lane", "stats").stdout
        self.assertIn("Builder done to landed: median 10.0 min over 1 landed lane", out)
        self.assertIn("lead turns per landed lane: 1.0", out)
        landing = self.fm("lane", "stats", "--json").stdout
        self.assertIn('"median_s": 600.0', landing)
        self.assertIn('"turns_per_lane": 1.0', landing)

    def test_report_lane_stats_counts_a_landing_of_several_tasks_once_each(self):  # review 7
        t0 = int(time.time()) - 3600
        clock = {"t": t0}
        with mock.patch.object(c, "now", lambda: c.iso(clock["t"])):
            for tid in ("T-0001", "T-0002"):
                c.log_event(self.p, "builder_mark", task=tid, data={"returned": True}, session="sess-lead")
            clock["t"] = t0 + 600
            c.log_event(self.p, "lane_landed", task="T-0001,T-0002", data={}, session="sess-lead")
            landing = lanes.landing(self.p)
        self.assertEqual(landing["landed"], 2)
        self.assertEqual(landing["median_s"], 600.0)

    def test_report_lane_stats_leaves_out_a_return_past_the_window_and_the_pilots_own_events(self):  # review 7
        t0 = int(time.time()) - 3600
        clock = {"t": t0 - 40 * 86400}
        with mock.patch.object(c, "now", lambda: c.iso(clock["t"])):
            c.log_event(self.p, "builder_mark", task=TID, data={"returned": True}, session="sess-lead")
            clock["t"] = t0 + 600
            c.log_event(self.p, "lane_landed", task=TID, data={}, session="sess-lead")
            self.assertIsNone(lanes.landing(self.p), "a return from 40 days ago counted")
            clock["t"] = t0 + 700
            c.log_event(self.p, "builder_mark", task=TID, data={"returned": True}, session="sess-lead")
            clock["t"] = t0 + 760
            c.log_event(self.p, "subagent", task=TID, data={}, session=f"pilot-{TID}")  # the pilot's own, not the lead's
            clock["t"] = t0 + 800
            c.log_event(self.p, "subagent", task=TID, data={}, session="sess-lead")
            clock["t"] = t0 + 1000
            c.log_event(self.p, "lane_landed", task=TID, data={}, session="sess-lead")
            landing = lanes.landing(self.p)
        self.assertEqual(landing, {"landed": 1, "median_s": 300.0, "turns_per_lane": 1.0})
