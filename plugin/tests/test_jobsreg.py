"""T-5031: a background job started by hand registers itself in the running journal with a why; it shows in sampo jobs
and sampo recover never restarts it."""
import json
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

from helpers import ForemanTestCase

from sampo import hooks, jobsreg, recover
from sampo import core as c

OLD = "11111111-old-boot"


class Jobsreg(ForemanTestCase):
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

    def dead_pid(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        return proc.pid

    def test_a_hand_started_job_registers_with_a_why_and_lists(self):
        e = jobsreg.add(self.p, "cargo build --release", "the release build the next lane needs", pid=os.getpid(),
                        log="build.log")
        (r,) = [r for r in jobsreg.rows(self.p) if r["source"] == "manual"]
        self.assertEqual((r["id"], r["name"], r["why"], r["state"], r["pid"]),
                         (e["id"], "cargo build --release", "the release build the next lane needs", "running", os.getpid()))
        self.assertTrue(os.path.isabs(r["log"]))
        self.assertIn("the release build the next lane needs", jobsreg.text(jobsreg.rows(self.p)))
        self.assertTrue(os.path.exists(os.path.join(recover._dir(self.p), f"manual-{e['id']}.json")), "one journal: running/")

    def test_a_job_needs_a_name_and_a_why(self):
        for name, why in (("", "x"), ("x", ""), ("x", "   "), ("x", None)):
            with self.assertRaises(ValueError):
                jobsreg.add(self.p, name, why)
        for pid in (0, -3, True, "12"):
            with self.assertRaises(ValueError):
                jobsreg.add(self.p, "x", "y", pid=pid)
        self.assertEqual(jobsreg.rows(self.p), [])

    def test_text_is_one_clean_bounded_line(self):
        e = jobsreg.add(self.p, "a\nb\x1b[31m" + "n" * 500, "why\r\nstill\x00why " + "w" * 900)
        self.assertNotRegex(e["name"] + e["why"], r"[\x00-\x08\x0a-\x1f\x7f]")
        self.assertLessEqual(len(e["name"]), 80)
        self.assertLessEqual(len(e["why"]), 300)
        self.assertEqual(len(jobsreg.text(jobsreg.rows(self.p)).splitlines()), 1)

    def test_recover_never_restarts_a_manual_job_and_leaves_it_listed(self):
        e = jobsreg.add(self.p, "slow import", "feeds the board", pid=self.dead_pid())
        path = os.path.join(recover._dir(self.p), f"manual-{e['id']}.json")
        with open(path, encoding="utf-8") as f:
            old = {**json.load(f), "boot": OLD}  # registered before the reboot
        with open(path, "w", encoding="utf-8") as f:
            json.dump(old, f)
        started = []
        with mock.patch.object(recover, "spawn", lambda p, e: started.append(e) or 4242):
            self.assertEqual(recover.entries(self.p), [], "a manual entry is not a restartable journal entry")
            self.assertEqual(recover.interrupted(self.p), [])
            self.assertEqual(recover.scan(self.p)["jobs"], [])
            rep = recover.recover(self.p)
        self.assertEqual((started, rep["restarted"]), ([], []))
        self.assertTrue(os.path.exists(path), "the record stays")
        (r,) = jobsreg.rows(self.p)
        self.assertEqual((r["id"], r["state"]), (e["id"], "gone"), "a reboot ended it: listed as gone")

    def test_a_dead_pid_reads_gone_and_a_pidless_job_runs_until_ended(self):
        gone = jobsreg.add(self.p, "died", "w", pid=self.dead_pid())
        free = jobsreg.add(self.p, "no pid", "w")
        by = {r["id"]: r["state"] for r in jobsreg.rows(self.p)}
        self.assertEqual((by[gone["id"]], by[free["id"]]), ("gone", "running"))

    def test_end_records_the_result_and_a_second_end_keeps_the_first(self):
        e = jobsreg.add(self.p, "build", "w", pid=os.getpid())
        done = jobsreg.end(self.p, e["id"][:4], "built in 90s")
        self.assertEqual((done["result"], bool(done["end"])), ("built in 90s", True))
        (r,) = jobsreg.rows(self.p)
        self.assertEqual((r["state"], r["result"]), ("ended", "built in 90s"))
        again = jobsreg.end(self.p, e["id"], "something else")
        self.assertTrue(again["again"])
        self.assertEqual(jobsreg.rows(self.p)[0]["result"], "built in 90s")
        self.assertEqual(jobsreg.end(self.p, jobsreg.add(self.p, "x", "y")["id"])["result"], "done", "no result: done")

    def test_end_of_an_unknown_or_ambiguous_id_is_refused(self):
        with self.assertRaises(KeyError):
            jobsreg.end(self.p, "nope")
        with self.assertRaises(KeyError):
            jobsreg.end(self.p, "")
        a, b = jobsreg.add(self.p, "a", "w"), jobsreg.add(self.p, "b", "w")
        with mock.patch.object(jobsreg.uuid, "uuid4") as u:
            u.return_value.hex = "abc123" + "0" * 26
            jobsreg.add(self.p, "c", "w")
            u.return_value.hex = "abc124" + "0" * 26
            jobsreg.add(self.p, "d", "w")
        with self.assertRaises(ValueError):
            jobsreg.end(self.p, "abc12")
        self.assertEqual(jobsreg.end(self.p, "abc123")["id"], "abc123")
        self.assertNotEqual(a["id"], b["id"])

    def test_old_ended_jobs_age_out_and_running_ones_stay(self):
        old = jobsreg.add(self.p, "old", "w", pid=os.getpid())
        keep = jobsreg.add(self.p, "keep", "w", pid=os.getpid())
        jobsreg.end(self.p, old["id"], "ok")
        later = time.time() + 3 * 86400
        with mock.patch.object(jobsreg.time, "time", lambda: later):
            jobsreg.add(self.p, "new", "w", pid=os.getpid())  # an add prunes
        ids = {r["id"] for r in jobsreg.rows(self.p)}
        self.assertNotIn(old["id"], ids)
        self.assertIn(keep["id"], ids)

    def test_a_long_dead_job_is_kept_a_day_from_when_it_was_seen_gone(self):
        old = jobsreg.add(self.p, "long", "w", pid=self.dead_pid())
        path = os.path.join(recover._dir(self.p), f"manual-{old['id']}.json")
        with open(path) as fh:
            e = json.load(fh)
        e["start"] = time.time() - 2 * 86400
        with open(path, "w") as fh:
            json.dump(e, fh)
        jobsreg.add(self.p, "x", "y", pid=os.getpid())
        self.assertTrue(os.path.exists(path), "a job that ran 2 days then died survives the next add")
        later = time.time() + 2 * 86400
        with mock.patch.object(jobsreg.time, "time", lambda: later):
            jobsreg.add(self.p, "z", "y", pid=os.getpid())
        self.assertFalse(os.path.exists(path), "a day after it was seen gone it ages out")

    def test_the_verb_takes_options_before_the_name(self):
        for argv in (("--why", "feeds", "nightly", "import"), ("--pid", str(os.getpid()), "--why", "feeds", "nightly", "import")):
            out = json.loads(self.fm("jobs", "add", *argv, "--json").stdout)
            self.assertEqual((out["name"], out["why"]), ("nightly import", "feeds"))

    def test_running_jobs_are_capped(self):
        with mock.patch.object(jobsreg, "MAX_LIVE", 2):
            jobsreg.add(self.p, "a", "w")
            jobsreg.add(self.p, "b", "w")
            with self.assertRaises(ValueError):
                jobsreg.add(self.p, "c", "w")

    def test_this_boots_long_jobs_list_beside_the_manual_ones(self):
        with recover.journal(self.p, "land", ["lane", "land"]):
            jobsreg.add(self.p, "mine", "w", pid=os.getpid())
            kinds = sorted((r["source"], r["kind"]) for r in jobsreg.rows(self.p))
        self.assertEqual(kinds, [("journal", "land"), ("manual", "manual")])

    def test_a_torn_or_foreign_file_in_running_is_skipped(self):
        os.makedirs(recover._dir(self.p), exist_ok=True)
        for n, body in (("manual-bad.json", "{"), ("manual-list.json", "[]"), ("manual-kind.json", '{"kind": "land", "id": "k"}')):
            with open(os.path.join(recover._dir(self.p), n), "w") as f:
                f.write(body)
        self.assertEqual(jobsreg.rows(self.p), [])

    def test_the_verb_adds_lists_and_ends(self):
        out = json.loads(self.fm("jobs", "add", "nightly", "import", "--why", "feeds the board", "--pid", str(os.getpid()),
                                 "--json").stdout)
        self.assertEqual((out["name"], out["why"], out["kind"]), ("nightly import", "feeds the board", "manual"))
        listed = json.loads(self.fm("jobs", "--json").stdout)["jobs"]
        self.assertEqual([(r["id"], r["state"]) for r in listed], [(out["id"], "running")])
        self.assertIn("feeds the board", self.fm("jobs").stdout)
        self.assertIn("Ended", self.fm("jobs", "end", out["id"], "all", "good").stdout)
        (r,) = json.loads(self.fm("jobs", "ls", "--json").stdout)["jobs"]
        self.assertEqual((r["state"], r["result"]), ("ended", "all good"))
        bad = self.fm("jobs", "add", "x", check=False)
        self.assertNotEqual(bad.returncode, 0, "no why: refused")
        self.assertIn("why", bad.stderr)
        self.assertNotEqual(self.fm("jobs", "end", "zzzzzz", check=False).returncode, 0)


if __name__ == "__main__":
    unittest.main()
