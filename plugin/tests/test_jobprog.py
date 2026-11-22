"""T-5034: the live page's background job rows carry elapsed against the typical time (the ledger's median, as
selfcheck.slow_jobs reads it), the job's last log line, and, once it has ended, its result."""
import os
import time
import unittest.mock as mock

from helpers import ForemanTestCase

from sampo import core as c
from sampo import jobprog, live

CHECK = "python3 plugin/bin/sampo check --affected"
FUNNEL = "python3 plugin/bin/sampo moons funnel --stage kill"


class JobRows(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)
        live._CACHE.clear()
        jobprog.reset()
        self.now = float(int(time.time()))  # whole seconds: a row's start is an iso time

    def gates(self, *totals, exit=0, ago=3600):
        """Finished runs the ledger holds (a past hour by default, before any job here began; ago=0: just now)."""
        with mock.patch.object(c, "now", return_value=c.iso(time.time() - ago)):
            for s in totals:
                c.log_event(self.p, "check_run", data={"results": [{"cmd": "a", "exit": exit, "s": s}]})

    def log(self, text, name="job.log"):
        path = os.path.join(self.repo, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def jobs(self, procs, log=None, ended=False, now=None):
        with mock.patch.object(live, "_procs", return_value=procs), mock.patch.object(jobprog, "log_path", return_value=log), \
                mock.patch.object(live, "_started", return_value=(now or self.now) - 90), \
                mock.patch.object(time, "time", return_value=now or self.now):
            return live.jobs(self.p, ended=ended)

    # ---- elapsed, typical, ratio
    def test_a_running_row_has_elapsed_the_ledger_median_as_typical_and_their_ratio(self):
        self.gates(60, 40, 50, 900)  # the median of four is the upper middle, as slow_jobs takes it
        j = self.jobs([(4242, CHECK)])[0]
        self.assertEqual(j["elapsed"], 90)
        self.assertEqual(j["typical"], 60)
        self.assertEqual(j["ratio"], 1.5)
        self.assertNotIn("ended", j)

    def test_a_row_with_no_history_has_elapsed_and_no_typical_or_ratio(self):
        self.gates(60, 70)  # under 3 passing runs: no typical, as slow_jobs stays quiet
        j = self.jobs([(4242, CHECK)])[0]
        self.assertEqual((j["elapsed"], j["typical"], j["ratio"]), (90, None, None))
        self.assertEqual((self.jobs([(4243, FUNNEL)])[0]["typical"]), None)  # a kind the ledger records no seconds for

    def test_red_runs_and_train_runs_are_not_the_typical(self):
        self.gates(5, 5, 5, exit=1)
        c.log_event(self.p, "check_run", data={"train": ["T-1"], "results": [{"cmd": "a", "exit": 0, "s": 1}]})
        self.assertIsNone(self.jobs([(4242, CHECK)])[0]["typical"])

    def test_a_row_with_no_start_has_no_elapsed(self):
        with mock.patch.object(live, "_procs", return_value=[(4242, CHECK)]), mock.patch.object(live, "_started", return_value=None):
            j = live.jobs(self.p)[0]
        self.assertEqual((j["since"], j["elapsed"], j["ratio"]), (None, None, None))

    def test_the_typical_is_one_ledger_read_a_while_not_one_a_page(self):
        self.gates(60, 60, 60)
        with mock.patch.object(c, "ledger_events", wraps=c.ledger_events) as ev:
            for _ in range(3):
                self.jobs([(4242, CHECK)])
        self.assertEqual(ev.call_count, 1)

    # ---- the last log line
    def test_the_last_line_is_the_logs_last_non_blank_one_cleaned_and_cut(self):
        self.assertEqual(jobprog.last_line(self.log("one\n\x1b[32mtwo: ok\x1b[0m\n\n  \n")), "two: ok")
        self.assertEqual(jobprog.last_line(self.log("a\rb 10%\rb 20%")), "b 20%")  # a progress bar redraws with \r
        self.assertEqual(jobprog.last_line(self.log("x" * 500 + "\n")), "x" * (jobprog.LINE_W - 1) + "…")
        self.assertEqual(jobprog.last_line(self.log("bell\x07 and\x00nul\n")), "bell and nul")
        key = "sk-ant-api03-" + "A1b2C3d4" * 6
        self.assertNotIn(key, jobprog.last_line(self.log(f"using key {key}\n")))  # a secret in a log never reaches the page
        self.assertIsNone(jobprog.last_line(self.log("")))
        self.assertIsNone(jobprog.last_line(self.log("\n \n")))

    def test_a_huge_log_reads_only_its_tail(self):
        path = self.log("early line\n" + "y" * (jobprog.TAIL_B * 3) + "\nlate line\n")
        with mock.patch.object(os, "lseek", wraps=os.lseek) as seek:
            self.assertEqual(jobprog.last_line(path), "late line")
        seek.assert_called_once_with(mock.ANY, os.path.getsize(path) - jobprog.TAIL_B, os.SEEK_SET)  # one seek: the last TAIL_B bytes

    def test_a_missing_log_a_symlink_a_fifo_a_directory_and_another_users_file_give_no_line(self):
        self.assertIsNone(jobprog.last_line(os.path.join(self.repo, "nope.log")))
        real = self.log("secret\n")
        link = os.path.join(self.repo, "link.log")
        os.symlink(real, link)
        self.assertIsNone(jobprog.last_line(link))
        fifo = os.path.join(self.repo, "f.fifo")
        os.mkfifo(fifo)
        self.assertIsNone(jobprog.last_line(fifo))
        self.assertIsNone(jobprog.last_line(self.repo))
        with mock.patch.object(os, "getuid", return_value=os.stat(real).st_uid + 1):
            self.assertIsNone(jobprog.last_line(real))
        self.assertIsNone(jobprog.last_line(None))

    def test_the_log_is_the_regular_file_on_the_jobs_stdout_or_stderr(self):
        real = self.log("x\n")
        links = {"/proc/7/fd/1": "/dev/null", "/proc/7/fd/2": real}
        with mock.patch.object(os, "readlink", side_effect=lambda q: links[q]):
            self.assertEqual(jobprog.log_path(7), real)
        links["/proc/7/fd/2"] = real + " (deleted)"
        with mock.patch.object(os, "readlink", side_effect=lambda q: links[q]):
            self.assertIsNone(jobprog.log_path(7))
        with mock.patch.object(os, "readlink", side_effect=OSError):
            self.assertIsNone(jobprog.log_path(7))

    def test_a_running_row_carries_the_last_line(self):
        j = self.jobs([(4242, FUNNEL)], log=self.log("starting\nkill stage: 40 of 90\n"))[0]
        self.assertEqual(j["last"], "kill stage: 40 of 90")
        self.assertIsNone(self.jobs([(4242, FUNNEL)], log=None)[0]["last"])

    # ---- ended rows keep their result
    def test_a_job_that_is_gone_stays_as_an_ended_row_with_its_result_and_final_elapsed(self):
        self.gates(60, 60, 60)
        log = self.log("gates: 3 of 3\n")
        self.assertEqual(len(self.jobs([(4242, CHECK)], log=log, ended=True)), 1)
        self.gates(95, ago=0)  # this run's own record
        with open(log, "a") as f:
            f.write("all green\n")
        rows = self.jobs([], log=log, ended=True, now=self.now + 30)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["pid"], r["kind"], r["result"], r["last"]), (4242, "check", "passed", "all green"))
        self.assertEqual(r["elapsed"], 120)  # 90 s when last seen running, and the 30 s to its end as seen
        self.assertEqual((r["typical"], r["ratio"]), (60, 2.0))
        self.assertEqual(r["ended"], c.iso(self.now + 30))
        later = self.jobs([], log=log, ended=True, now=self.now + 90)[0]  # a second look changes nothing
        self.assertEqual((later["elapsed"], later["ended"], later["result"]), (120, r["ended"], "passed"))

    def test_a_red_check_is_failed_and_a_job_the_ledger_cannot_judge_is_just_ended(self):
        self.jobs([(4242, CHECK), (4243, FUNNEL)])
        self.gates(5, exit=1, ago=0)
        rows = {r["kind"]: r for r in self.jobs([], ended=True, now=self.now + 5)}
        self.assertEqual((rows["check"]["result"], rows["funnel"]["result"]), ("failed", "ended"))

    def test_a_check_that_ended_before_it_started_is_not_its_result(self):
        self.gates(5, exit=1)  # a red run from an hour ago, before this job began (it began 90 s ago)
        self.jobs([(4242, CHECK)])
        self.assertEqual(self.jobs([], ended=True)[0]["result"], "ended")

    def test_a_gap_before_the_job_was_missed_ends_it_when_last_seen_not_when_noticed(self):
        self.gates(60, 60, 60)
        self.jobs([(4242, CHECK)])  # seen running at self.now, 90 s old
        r = self.jobs([], ended=True, now=self.now + 300)[0]
        self.assertEqual((r["elapsed"], r["ended"]), (90, c.iso(self.now)))
        self.assertEqual(self.jobs([], ended=True, now=self.now + 3600), [])  # and it expires from when it ended

    def test_a_run_logged_after_the_job_vanished_does_not_decide_its_result(self):
        self.jobs([(4242, CHECK)])
        r = self.jobs([], ended=True, now=self.now + 5)[0]  # killed: gone at now+5
        self.assertEqual(r["result"], "ended")
        self.gates(5, exit=1, ago=-120)  # another process's run, minutes after its end
        again = self.jobs([], ended=True, now=self.now + 6)[0]
        self.assertEqual(again["result"], "ended")

    def test_two_runs_in_the_window_are_ambiguous(self):
        self.jobs([(4242, CHECK)])
        self.gates(5, exit=0, ago=0)
        self.gates(5, exit=1, ago=0)
        self.assertEqual(self.jobs([], ended=True, now=self.now + 5)[0]["result"], "ended")

    def test_a_train_run_is_not_a_checks_result(self):
        self.jobs([(4242, CHECK)])
        c.log_event(self.p, "check_run", data={"train": 1, "results": [{"cmd": "a", "exit": 0, "s": 1}]})
        self.assertEqual(self.jobs([], ended=True, now=self.now + 5)[0]["result"], "ended")

    def test_another_projects_check_is_not_judged_by_this_ledger(self):
        other = CHECK + " -p some-other-project"
        self.jobs([(4242, other)])
        self.gates(5, exit=0, ago=0)
        self.assertEqual(self.jobs([], ended=True, now=self.now + 5)[0]["result"], "ended")

    def test_running_calls_without_ended_never_show_ended_rows(self):
        self.jobs([(4242, CHECK)])
        self.assertEqual(self.jobs([]), [])
        self.assertEqual(len(self.jobs([], ended=True)), 1)

    def test_ended_rows_are_kept_for_a_while_and_a_few(self):
        self.jobs([(4242, CHECK)])
        self.jobs([], now=self.now + 1)
        self.assertEqual(len(self.jobs([], ended=True, now=self.now + jobprog.ENDED_KEEP_S - 1)), 1)
        self.assertEqual(self.jobs([], ended=True, now=self.now + jobprog.ENDED_KEEP_S + 5), [])
        procs = [(1000 + i, CHECK) for i in range(jobprog.ENDED_MAX + 5)]
        self.jobs(procs, now=self.now + 700)
        rows = self.jobs([], ended=True, now=self.now + 701)
        self.assertEqual(len(rows), jobprog.ENDED_MAX)
        self.assertEqual(len(jobprog._SEEN) + len(jobprog._GONE), jobprog.ENDED_MAX)  # nothing piles up unseen

    def test_a_reused_pid_ends_the_old_job_and_shows_the_new_one(self):
        self.jobs([(4242, CHECK)])
        with mock.patch.object(live, "_procs", return_value=[(4242, CHECK)]), mock.patch.object(jobprog, "log_path", return_value=None), \
                mock.patch.object(live, "_started", return_value=self.now + 50), mock.patch.object(time, "time", return_value=self.now + 60):
            rows = live.jobs(self.p, ended=True)
        self.assertEqual(sorted(("ended" in r) for r in rows), [False, True])

    def test_a_project_sees_only_its_own_ended_rows(self):
        self.jobs([(4242, CHECK)])
        self.jobs([])
        other = c.Project(self.p.slug, self.p.root, self.p.dir + "-other")  # a second project's state folder
        with mock.patch.object(live, "_procs", return_value=[]):
            self.assertEqual(live.jobs(other, ended=True), [])

    def test_an_ended_row_is_json(self):
        import json
        self.jobs([(4242, CHECK)], log=self.log("x\n"))
        json.dumps(self.jobs([], ended=True))
