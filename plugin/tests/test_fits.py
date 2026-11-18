"""T-3499: every Sampo feature earns its use. On 2026-11-06 sampo usage showed 112 of 171 verbs never run and 23 of
26 skills never loaded while tidy, release and the debug-test skill each had their moment (seven stale scratch
worktrees, 322 unreleased CHANGELOG entries, seven failed closes): the report existed and nothing acted on it."""
import contextlib
import datetime
import json
import os
import shutil
import subprocess
import time

from test_hooks import HookCase

from sampo import core as c
from sampo import fits
from sampo import selfcheck


def ago(days):
    return c.iso(time.time() - days * 86400)


class Fits(HookCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)

    def git(self, *a):
        return subprocess.run(["git", "-C", self.repo, *a], check=True, capture_output=True, text=True).stdout

    def scratch(self, name, hours_old):
        path = os.path.join(self.tmp, name)
        self.git("worktree", "add", "-q", "--detach", path)
        t = time.time() - hours_old * 3600
        os.utime(path, (t, t))
        return path

    def release_due(self, entries=150, days=4):
        os.makedirs(os.path.join(self.repo, "plugin", ".claude-plugin"))
        with open(os.path.join(self.repo, "plugin", ".claude-plugin", "plugin.json"), "w") as f:
            json.dump({"name": "x", "version": "1.0.0"}, f)
        day = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
        with open(os.path.join(self.repo, "CHANGELOG.md"), "w") as f:
            f.write("# Changelog\n\n## Unreleased\n\n" + "".join(f"- change {n}\n" for n in range(entries))
                    + f"\n## 1.0.0 — {day}\n\n- first\n")

    def test_tidy_counts_stale_scratch_worktrees_and_the_days_since_the_last_tidy(self):
        self.scratch("fm-verify-old", 2)
        gone = self.scratch("sampo-train-gone", 0)
        shutil.rmtree(gone)  # deleted by hand: git lists it until pruned
        self.scratch("sampo-train-live", 0)  # a train running now is no stale worktree
        c.update_meta(self.p, last_tidy=ago(3.2))
        self.assertEqual(fits.tidy(self.p, [], time.time()),
                         "sampo tidy (2 stale scratch worktrees, last tidy 3 days ago)")
        self.git("worktree", "prune")
        self.git("worktree", "remove", "--force", os.path.join(self.tmp, "fm-verify-old"))
        c.update_meta(self.p, last_tidy=c.now())
        self.assertIsNone(fits.tidy(self.p, [], time.time()), "tidied, nothing stale: quiet")
        c.update_meta(self.p, last_tidy=ago(9))
        self.assertEqual(fits.tidy(self.p, [], time.time()), "sampo tidy (last tidy 9 days ago)")

    def test_release_counts_unreleased_entries_and_days_since_the_version_moved(self):
        self.assertIsNone(fits.release(self.p, [], time.time()), "no plugin.json: not Sampo's repo, no release")
        self.release_due()
        self.assertEqual(fits.release(self.p, [], time.time()),
                         "sampo release (150 unreleased CHANGELOG entries, 4 days since 1.0.0)")
        shutil.rmtree(os.path.join(self.repo, "plugin"))
        os.remove(os.path.join(self.repo, "CHANGELOG.md"))
        self.release_due(entries=3, days=4)
        self.assertIsNone(fits.release(self.p, [], time.time()), "3 entries 4 days old: not due yet")

    def test_debug_test_counts_todays_failed_closes(self):
        self.assertIsNone(fits.debug_test(self.p, [], time.time()))
        for code in (1, 0, 2):
            c.log_event(self.p, "close_affected", task="T-0001", data={"cmd": "pytest", "exit": code})
        self.assertEqual(fits.debug_test(self.p, [], time.time()), "the debug-test skill (2 failed closes today)")

    def test_doctor_counts_days_since_it_last_ran_and_a_run_records_it(self):
        self.assertIsNone(fits.doctor(self.p, [], time.time()), "a project set up today: nothing to check yet")
        c.update_meta(self.p, created=ago(10.2))
        self.assertEqual(fits.doctor(self.p, [], time.time()), "sampo doctor (never run here, set up 10 days ago)")
        c.update_meta(self.p, doctor_at=ago(9.1))
        self.assertEqual(fits.doctor(self.p, [], time.time()), "sampo doctor (last run 9 days ago)")
        self.fm("doctor", check=False, timeout=120)
        self.assertIsNone(fits.doctor(self.p, [], time.time()), "a doctor run is recorded")

    def test_the_self_check_names_the_top_two_unused_fits_and_drops_a_used_one(self):
        self.scratch("fm-verify-old", 2)
        c.update_meta(self.p, last_tidy=ago(3.2), doctor_at=c.now())
        self.release_due()
        c.log_event(self.p, "close_affected", task="T-0001", data={"cmd": "pytest", "exit": 1})
        self.assertIn(selfcheck.feature_fit, selfcheck.CHECKS)
        self.assertEqual(selfcheck.feature_fit(self.p, [], time.time()),
                         "unused, and fits now: sampo tidy (1 stale scratch worktree, last tidy 3 days ago) · "
                         "sampo release (150 unreleased CHANGELOG entries, 4 days since 1.0.0) · 1 left out")
        self.hook("PostToolUse", {"tool_name": "Bash", "tool_input": {"command": "sampo tidy"}})
        self.assertEqual(selfcheck.feature_fit(self.p, [], time.time()),
                         "unused, and fits now: sampo release (150 unreleased CHANGELOG entries, 4 days since "
                         "1.0.0) · the debug-test skill (1 failed close today)", "tidy ran in the window: used")
        with contextlib.suppress(FileNotFoundError):
            os.remove(os.path.join(self.p.dir, "selfcheck.json"))  # what an earlier hook cached
        self.assertIn("unused, and fits now: sampo release", self.fm("next").stdout)

    def test_the_night_runs_the_fitting_read_only_verbs_and_logs_what_they_found(self):
        from sampo import night
        briefs = c.load_briefs(self.p)
        c.update_meta(self.p, created=ago(10))
        self.assertEqual(fits.due(self.p, briefs, time.time()), ["doctor", "usage", "repeats"],
                         "doctor never ran in 10 days; tidy has nothing stale and no age")
        c.update_meta(self.p, doctor_at=c.now(), last_tidy=c.now())
        self.assertEqual([(n, a) for n, a, _ in night.jobs(self.p) if n.startswith("fits ")],
                         [("fits usage", ["usage"]), ("fits repeats", ["repeats"])])
        self.assertTrue(all("--apply" not in v for v in fits.READ_ONLY))
        sessions = os.path.join(c.state_dir(), "sessions")  # usage known and low
        os.makedirs(sessions, exist_ok=True)
        with open(os.path.join(sessions, "s.json"), "w") as f:
            json.dump({"rate_limits": {"seven_day": {"used_percentage": 10}}}, f)
        res = json.loads(self.fm("night", "--only", "fits", "--max-usd", "0.05", "--json").stdout)
        ran = {r["name"]: r for r in res["ran"]}
        self.assertEqual(sorted(ran), ["fits repeats", "fits usage"])
        self.assertRegex(ran["fits usage"]["tail"], r"^\d+ lines?: Last 30 day\(s\) in ")
        night_ev = [e for e in c.ledger_tail(self.p, 100) if e.get("event") == "night"][-1]
        self.assertEqual(sorted(r["name"] for r in night_ev["data"]["ran"]), ["fits repeats", "fits usage"])

    def test_usage_counts_unused_features_with_no_probe_yet(self):
        got = self.fm_json("usage")
        self.assertIn("brainstorm", got["no_probe"])
        self.assertNotIn("tidy", got["no_probe"], "tidy has a probe")
        self.assertRegex(self.fm("usage").stdout, r"unused with no situation probe yet: \d+ of \d+ \(")
