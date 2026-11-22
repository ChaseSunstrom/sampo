"""T-5591: one call shows the load, the disk, the builders, the long verbs' jobs and their log tails; sampo pulse --wait N
returns when the board changes or a job ends, and by N seconds otherwise. A long verb (land, train, lane verify, lane
precheck, the full check, task finish) registers itself in the jobs registry while it runs and ends with its exit code."""
import os
import time
import unittest
from unittest import mock

from helpers import ForemanTestCase

from sampo import board, cli, hooks, jobsreg, pulse, recover
from sampo import core as c


class Clock:
    """A fake clock: sleep advances it, so a wait's cap is tested without waiting."""

    def __init__(self, on_sleep=None):
        self.t, self.on_sleep, self.slept = 0.0, on_sleep, []

    def now(self):
        return self.t

    def sleep(self, secs):
        self.slept.append(secs)
        self.t += secs
        if self.on_sleep:
            self.on_sleep(self)


class Pulse(ForemanTestCase):
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

    def ns(self, **kw):
        return type("A", (), kw)

    def manual(self):
        return [r for r in jobsreg.rows(self.p) if r["source"] == "manual"]

    def test_a_registered_job_that_ends_flips_the_digest(self):
        d0 = pulse.digest(pulse.pulse(self.p))
        e = jobsreg.add(self.p, "sampo lane verify", "the full suite on each landed merge", pid=os.getpid())
        d1 = pulse.digest(pulse.pulse(self.p))
        jobsreg.end(self.p, e["id"], "exit 0")
        d2 = pulse.digest(pulse.pulse(self.p))
        self.assertEqual(len({d0, d1, d2}), 3, "a job started, then ended: each changes the board's digest")

    def test_a_job_ending_during_the_wait_returns_it_early_with_its_result_and_log_tail(self):
        log = os.path.join(self.tmp, "verify.log")
        with open(log, "w", encoding="utf-8") as f:
            f.write("".join(f"line {i}\n" for i in range(1, 9)))
        e = jobsreg.add(self.p, "sampo lane verify", "the full suite", pid=os.getpid(), log=log)
        clock = Clock(on_sleep=lambda k: jobsreg.end(self.p, e["id"], "exit 0"))
        got = pulse.wait(self.p, 5, sleep=clock.sleep, clock=clock.now)
        self.assertEqual(clock.slept, [2.0], "the first 2 s poll saw the job end: no second poll, no wait to the cap")
        self.assertEqual(got["returned"], "changed")
        (ended,) = got["ended"]
        self.assertEqual((ended["name"], ended["result"]), ("sampo lane verify", "exit 0"))
        self.assertEqual(ended["tail"], ["line 4", "line 5", "line 6", "line 7", "line 8"], "the last 5 lines of its log")

    def test_no_change_returns_at_the_cap_and_the_cap_is_170_seconds(self):
        clock = Clock()
        got = pulse.wait(self.p, 5, sleep=clock.sleep, clock=clock.now)
        self.assertEqual((got["returned"], got["waited"]), ("capped", 5))
        self.assertEqual(clock.slept, [2.0, 2.0, 1.0], "polls every 2 s, the last one for the rest of the wait")
        clock = Clock()
        got = pulse.wait(self.p, 9999, sleep=clock.sleep, clock=clock.now)
        self.assertEqual((got["returned"], got["waited"]), ("capped", pulse.MAX_WAIT_S))
        self.assertEqual(pulse.MAX_WAIT_S, 170)
        clock = Clock()
        self.assertEqual(pulse.wait(self.p, -4, sleep=clock.sleep, clock=clock.now)["waited"], 0, "a negative wait is none")
        self.assertEqual(clock.slept, [])

    def test_a_long_verb_registers_while_it_runs_and_ends_with_its_exit_code(self):
        with mock.patch.object(cli, "resolve", return_value=self.p), \
                recover.around(self.ns(verb="lane", action="verify"), ["lane", "verify"]) as job:
            (r,) = self.manual()
            self.assertEqual((r["name"], r["state"], r["end"]), ("sampo lane verify", "running", None))
            self.assertEqual(r["pid"], os.getpid())
            job.code = 3
        (r,) = self.manual()
        self.assertEqual((r["state"], r["result"]), ("ended", "exit 3"))

    def test_an_exception_ends_the_entry_with_what_stopped_it(self):
        with mock.patch.object(cli, "resolve", return_value=self.p):
            with self.assertRaises(RuntimeError), recover.around(self.ns(verb="check", action="run"), ["check"]):
                raise RuntimeError("a gate run that broke")
        (r,) = self.manual()
        self.assertEqual((r["name"], r["result"]), ("sampo check", "stopped by RuntimeError"),
                         "an error main maps to no exit code (here, a bare RuntimeError) is named, not numbered")

    def test_a_verb_that_refuses_ends_with_the_exit_code_cli_main_maps_it_to(self):
        def refuse(a):
            raise c.PolicyError("done without evidence")

        def bug(a):
            raise RuntimeError("a bug in the verb")
        with mock.patch.object(cli, "resolve", return_value=self.p):
            args = self.ns(verb="check", action="run", fn=refuse)
            with self.assertRaises(c.PolicyError), recover.around(args, ["check"]):
                args.fn(args)
            args = self.ns(verb="check", action="run", fn=bug)
            with self.assertRaises(RuntimeError), recover.around(args, ["check"]):
                args.fn(args)
            args = self.ns(verb="check", action="run", fn=lambda a: 3)
            with recover.around(args, ["check"]):
                self.assertEqual(args.fn(args), 3)
        results = sorted(r["result"] for r in self.manual())
        self.assertEqual(results, ["exit 2", "exit 3", "stopped by RuntimeError"],
                         "a refusal ends 'exit 2' as main returns it, an unmapped error 'stopped by X'")

    def test_the_kinds_that_register_and_the_ones_that_do_not(self):
        registers = [self.ns(verb="lane", action="land"), self.ns(verb="lane", action="train"),
                     self.ns(verb="lane", action="precheck"), self.ns(verb="check", action="run"),
                     self.ns(verb="task", task_cmd="finish")]
        skips = [self.ns(verb="lane", action="land", bg=True), self.ns(verb="check", action="run", affected=True),
                 self.ns(verb="check", action="run", lock=True), self.ns(verb="check", action="run", evidence="T-1"),
                 self.ns(verb="lane", action="list"), self.ns(verb="task", task_cmd="show")]
        with mock.patch.object(cli, "resolve", return_value=self.p):
            for args in registers + skips:
                with recover.around(args, ["x"]) as job:
                    job.code = 0
        names = sorted(r["name"] for r in self.manual())
        self.assertEqual(names, sorted(["sampo lane land", "sampo lane train", "sampo lane precheck", "sampo check",
                                        "sampo task finish"]),
                         "a --bg starter, an affected or lock check, and a read verb are not long jobs")

    def test_a_registry_that_cannot_be_written_never_stops_the_verb(self):
        ran = []
        with mock.patch.object(cli, "resolve", return_value=self.p), \
                mock.patch.object(jobsreg, "add", side_effect=OSError("disk full")):
            with recover.around(self.ns(verb="lane", action="verify"), ["lane", "verify"]) as job:
                ran.append(True)
                job.code = 0
        self.assertEqual(ran, [True])
        self.assertEqual(self.manual(), [])

    def test_a_pulse_of_many_running_jobs_stays_under_40_lines(self):
        for i in range(30):
            jobsreg.add(self.p, f"sampo lane verify {i}", "the full suite", pid=os.getpid())
        lines = pulse.text(pulse.pulse(self.p)).splitlines()
        self.assertLess(len(lines), 40, "sampo pulse prints under 40 lines however many jobs run")
        self.assertIn("more", lines[-1], "the jobs it does not show are counted")

    def test_the_verb_prints_load_disk_builders_jobs_and_the_train_line(self):
        out = self.fm("pulse").stdout
        for word in ("load", "GB free", "lanes", "train"):
            self.assertIn(word, out)
        self.assertLess(len(out.splitlines()), 40)
        self.assertEqual(self.fm("pulse", "--wait", "0").returncode, 0)

    def test_a_log_that_is_a_credential_or_outside_the_state_cache_and_temp_folders_is_refused_and_never_tailed(self):
        env = os.path.join(self.tmp, ".env")
        with open(env, "w", encoding="utf-8") as f:
            f.write("API_KEY=hunter2\nlast line\n")
        with self.assertRaises(ValueError, msg="a credential is no log"):
            jobsreg.add(self.p, "sampo lane verify", "the full suite", log=env)
        with self.assertRaises(ValueError, msg="a file outside the allowed folders is no log"):
            jobsreg.add(self.p, "sampo lane verify", "the full suite", log=os.path.join("/", "sampo-test-elsewhere", "b.log"))
        self.assertEqual(pulse._tail(env, 5), [], "a credential's last lines are never read, even from a record that names it")
        self.assertEqual(self.manual(), [], "a refused log registers nothing")

    def test_an_unchanged_board_is_read_every_ten_seconds_not_on_every_poll(self):
        clock = Clock()
        with mock.patch.object(board, "part_lanes", wraps=board.part_lanes) as lanes_read, \
                mock.patch.object(board, "part_train", wraps=board.part_train) as train_read:
            got = pulse.wait(self.p, 30, sleep=clock.sleep, clock=clock.now)
        self.assertEqual(len(clock.slept), 15, "the jobs are still polled every 2 s: 15 polls in 30 s")
        self.assertLessEqual(lanes_read.call_count, 4, "the lanes are read at the start and every 10 s, not on each poll")
        self.assertEqual(lanes_read.call_count, train_read.call_count)
        self.assertEqual((got["returned"], got["waited"]), ("capped", 30))

    def test_a_job_whose_entry_vanishes_during_the_wait_is_named_by_its_id(self):
        e = jobsreg.add(self.p, "sampo lane verify", "the full suite", pid=os.getpid())
        clock = Clock(on_sleep=lambda k: os.remove(jobsreg._path(self.p, e["id"])))
        got = pulse.wait(self.p, 5, sleep=clock.sleep, clock=clock.now)
        self.assertEqual(got["returned"], "changed")
        (ended,) = got["ended"]
        self.assertEqual(ended["id"], e["id"], "the ended list is the ids that were running and are not")
        self.assertIn(e["id"], pulse.text(got), "the id is printed when the name is gone")

    def test_a_verb_outside_a_project_resolves_without_creating_one_and_registers_nothing(self):
        asked, ran = [], []

        def resolve(args, create=True):
            asked.append(create)
            raise cli.UsageError("not in a Sampo project; run `sampo init` here or pass -p SLUG")
        with mock.patch.object(cli, "resolve", side_effect=resolve):
            with recover.around(self.ns(verb="lane", action="verify"), ["lane", "verify"]) as job:
                ran.append(True)
                job.code = 0
        self.assertEqual((ran, asked), ([True], [False]), "resolved with create=False: no project is made")
        self.assertEqual(self.manual(), [])

    def test_cli_main_ends_a_long_verbs_entry_with_the_code_it_returns_or_what_stopped_it(self):
        def returns3(a):
            return 3

        def refuses(a):
            raise c.PolicyError("done without evidence")

        def breaks(a):
            raise RuntimeError("a bug in the verb")
        verb = {"fn": returns3}  # the parser is built once per process: one stand-in that the test switches

        def dispatch(a):
            return verb["fn"](a)
        with mock.patch.object(cli, "resolve", return_value=self.p), mock.patch.object(cli, "cmd_check", dispatch):
            verb["fn"] = returns3
            self.assertEqual(cli.main(["check"]), 3)
            verb["fn"] = refuses
            self.assertEqual(cli.main(["check"]), 2)
            verb["fn"] = breaks
            with self.assertRaises(RuntimeError):
                cli.main(["check"])
        self.assertEqual(sorted(r["result"] for r in self.manual()), ["exit 2", "exit 3", "stopped by RuntimeError"])


    # T-5793: a land's train that runs past twice its typical time is flagged, naming the train and its age, by the pulse
    # and by the hook's drive line (2026-11-22 a land stood an hour on one train and nothing on the board said so)

    def timed(self, minutes, n=5):
        for i in range(n):  # n landed trains of `minutes` each: the typical time
            c.log_event(self.p, "land_train", data={"land": "L-1", "train": i + 1, "lanes": [f"T-{i}"],
                                                    "seconds": minutes * 60, "gate_s": 60, "wait_s": 0, "at": time.time()})

    def running(self, ids, age_s):
        """A train of another live process (a sleeping child) that began age_s ago: its record beside the train lock."""
        import json
        import subprocess
        from sampo import bus, train
        child = subprocess.Popen(["sleep", "120"])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        info = bus._proc(child.pid)
        rec = {"proc": f"{bus.host_id()}|{child.pid}|{info[1]}", "pid": child.pid, "root": self.repo, "lanes": list(ids),
               "at": time.time() - age_s}
        os.makedirs(os.path.join(self.p.dir, "train"), exist_ok=True)
        with open(os.path.join(self.p.dir, "train", train.TRAIN_RUNNING), "w") as f:
            json.dump(rec, f)

    def test_train_overdue_is_flagged_in_the_pulse_with_the_lanes_and_the_age(self):
        from sampo import train
        self.timed(10)
        self.running(["T-1", "T-2"], 90 * 60)
        got = train.overdue_train(self.p)
        self.assertEqual(got["lanes"], ["T-1", "T-2"])
        self.assertEqual((round(got["age"] / 60), round(got["typical"] / 60)), (90, 10))
        snap = pulse.pulse(self.p)
        self.assertEqual(snap["train"]["overdue"]["lanes"], ["T-1", "T-2"])
        self.assertIn("train overdue: T-1, T-2 running 90 min (typical 10 min)", pulse.text(snap))
        self.assertNotEqual(pulse.digest(snap), pulse.digest({**snap, "train": {**snap["train"], "overdue": None}}),
                            "a wait returns when a train turns overdue")

    def test_train_overdue_not_flagged_within_twice_its_typical_time_or_without_a_typical(self):
        from sampo import train
        self.running(["T-1"], 90 * 60)
        self.assertIsNone(train.overdue_train(self.p), "no timed train yet: no typical to compare with")
        self.timed(10)
        self.assertIsNotNone(train.overdue_train(self.p))
        self.running(["T-1"], 19 * 60)
        self.assertIsNone(train.overdue_train(self.p), "19 min of a typical 10: not yet twice")
        self.assertNotIn("overdue", pulse.text(pulse.pulse(self.p)))

    def test_train_overdue_is_said_once_by_the_hook_per_train(self):
        self.timed(10)
        self.running(["T-1", "T-2"], 90 * 60)
        d = {}
        line = hooks._overdue_push(self.p, d, True)
        self.assertIn("train overdue: T-1, T-2 running 90 min (typical 10 min)", line or "")
        self.assertIsNone(hooks._overdue_push(self.p, d, True), "said once for this train")
        self.assertIsNotNone(hooks._overdue_push(self.p, {}, True), "a Stop with no record of it says it")
        self.assertIsNone(hooks._overdue_push(self.p, {}, False), "not recorded as said: said at a Stop that can")


if __name__ == "__main__":
    unittest.main()
