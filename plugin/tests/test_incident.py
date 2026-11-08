"""T-2388: fm capture --incident. A harm signal recorded red opens an urgent FIX with a timeline; the close needs the same signal
green twice after the fix's last edit and one captured follow-up; the signal is left behind for fm sentinel."""
import os
import re
import subprocess
import unittest

from helpers import ForemanTestCase, PLUGIN

from sampo import core as c

SIG = "test -f healthy.flag"  # red until the fix writes the flag


class _IncidentRepo(ForemanTestCase):
    """setUp, open(), brief() and fix(); no tests of its own (T-3815), so IncidentSentinel shares them without rerunning
    Incident's tests."""
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)

    def open(self, signal=SIG, impact="checkout is down", **kw):
        return self.fm("capture", "--incident", impact, "--signal", signal, **kw)

    def brief(self):
        return next(b for b in c.load_briefs(self.p) if b.meta.get("incident_signal"))

    def fix(self):
        """The fix: the file the signal wants, and the ledger's record of the edit (the hooks write it in a session)."""
        with open(os.path.join(self.repo, "healthy.flag"), "w") as f:
            f.write("ok\n")
        c.log_event(self.p, "touched", task=self.brief().id, data={"file": os.path.join(self.repo, "healthy.flag")})

class Incident(_IncidentRepo):
    def test_a_passing_signal_opens_nothing(self):
        self.fix_flag = open(os.path.join(self.repo, "healthy.flag"), "w").close()
        r = self.open(check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("passes", r.stderr + r.stdout)
        self.assertEqual(c.load_briefs(self.p), [])

    def test_a_broken_signal_is_not_harm(self):
        for sig in ("definitely-not-a-command-xyz", "exit 126", "sleep 0 && exit 127"):
            r = self.open(sig, check=False)
            self.assertNotEqual(r.returncode, 0, sig)
            self.assertIn("signal broken", r.stderr + r.stdout, sig)
        self.assertEqual(c.load_briefs(self.p), [])

    def test_a_signal_with_a_backtick_is_refused(self):
        r = self.open("test -f `pwd`/x", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(c.load_briefs(self.p), [])

    def test_the_signal_runs_without_credentials(self):
        # red only when the token is gone: a signal that could read it would pass and open nothing
        r = self.open('test -n "${GITHUB_TOKEN-}"', env={"GITHUB_TOKEN": "ghp_secret"}, check=False)
        self.assertEqual(r.returncode, 0, r.stderr)
        for var, val in (("ANTHROPIC_API_KEY", "sk-ant-x"), ("CLAUDE_CODE_OAUTH_TOKEN", "oat-x")):  # T-2388 review
            with self.subTest(var=var):  # the model's own credentials are credentials too
                self.setUp()
                r = self.open(f'test -n "${{{var}-}}"', env={var: val}, check=False)  # pragma: allowlist secret
                self.assertEqual(r.returncode, 0, r.stderr)

    def test_a_red_signal_opens_an_urgent_fix_with_the_red_run_as_evidence(self):
        r = self.open()
        b = self.brief()
        self.assertIn(b.id, r.stdout)
        self.assertEqual((b.type, b.priority, b.status), ("FIX", "urgent", "active"))
        self.assertEqual(b.meta["incident_signal"], SIG)
        first = b.section("Timeline").strip().splitlines()[0]
        self.assertRegex(first, r"^- \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ ")
        self.assertIn("checkout is down", first)
        ev = b.evidence()
        self.assertTrue(any(l.startswith("- (ac 1)") and f"`{SIG}` → ✗ exit 1" in l and c._RAN_MARK in l for l in ev), ev)
        self.assertEqual(b.verify_cmds(), [(1, SIG)])

    def test_log_adds_a_timeline_row_with_the_expected_effect(self):
        self.open()
        self.fm("capture", "--incident", "log", "rolled back deploy 812", "--expect", "5xx under 1% in 5 min")
        rows = self.brief().section("Timeline").strip().splitlines()
        self.assertEqual(len(rows), 2)
        self.assertIn("rolled back deploy 812", rows[1])
        self.assertIn("expect: 5xx under 1% in 5 min", rows[1])
        r = self.fm("capture", "--incident", "log", "no expectation", check=False)
        self.assertNotEqual(r.returncode, 0, "an action without its expected effect is not a timeline row")

    def test_log_with_no_open_incident_says_so(self):
        r = self.fm("capture", "--incident", "log", "x", "--expect", "y", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no open incident", r.stderr + r.stdout)


class IncidentSentinel(_IncidentRepo):
    def finish(self):
        return self.fm("task", "finish", self.brief().id, "--audit", "self", "--result", "ok", check=False)

    def green(self):
        return self.fm("task", "evidence", self.brief().id, "--ac", "1", "--run", SIG)

    def test_sentinel_ritual_in_order_closes_and_leaves_the_signal_behind(self):
        # the session transcript: the commands a session runs, in order, with the refusals between them
        from sampo import reverify
        reverify.scan(self.p)  # the watermark: later commits are the ones that count
        self.open()
        tid = self.brief().id
        self.fm("capture", "--incident", "log", "restore the flag", "--expect", "signal green")
        r = self.finish()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("green", r.stderr + r.stdout)  # no fix yet: the signal is still red
        self.fix()
        self.green()
        r = self.finish()  # one green plus the close's own run is two, but no follow-up yet
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn("follow-up", r.stderr + r.stdout)
        self.assertNotIn("twice", r.stderr + r.stdout)
        self.fm("capture", "--incident", "follow-up", "alert on the flag file going missing")
        again = self.fm("capture", "--incident", "follow-up", "a second one", check=False)
        self.assertNotEqual(again.returncode, 0, "exactly one follow-up")
        followups = [b for b in c.load_briefs(self.p) if b.meta.get("source") == "followup"]
        self.assertEqual(len(followups), 1)
        self.assertEqual(followups[0].status, "captured")
        self.assertEqual(followups[0].meta.get("incident"), tid)
        r = self.finish()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(c.find_brief(self.p, tid).status, "done")
        # the sentinel: a later commit touching the incident's file queues the signal
        subprocess.run(["git", "-C", self.repo, "add", "-A"], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", "unrelated"], check=True)
        with open(os.path.join(self.repo, "healthy.flag"), "w") as f:
            f.write("changed\n")
        subprocess.run(["git", "-C", self.repo, "commit", "-qam", "touch the flag"], check=True)
        queued = [(e["task"], e["cmd"]) for e in reverify.scan(self.p)]
        self.assertIn((tid, SIG), queued)

    def test_sentinel_close_needs_two_greens_after_the_last_edit(self):
        self.open()
        self.fix()
        self.fm("capture", "--incident", "follow-up", "alert on it")
        r = self.finish()  # the close's own run is the only green: a flake check needs a second
        self.assertNotEqual(r.returncode, 0)
        self.assertRegex(r.stderr + r.stdout, r"green twice|second green")
        self.green()
        r = self.finish()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_sentinel_signal_cannot_be_swapped_after_the_red_run(self):
        self.open()
        self.fix()
        tid = self.brief().id
        self.fm("task", "ac", tid, "edit", "1", "--verify", "true", check=False)
        self.fm("capture", "--incident", "follow-up", "alert on it")
        self.green()
        self.green()
        r = self.finish()
        if self.brief().verify_cmds() != [(1, SIG)]:  # the edit went through: the close must notice
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("re-open", r.stderr + r.stdout)


class IncidentSkill(unittest.TestCase):
    def read(self, *parts):
        with open(os.path.join(PLUGIN, "skills", *parts), encoding="utf-8") as f:
            return f.read()

    def test_the_intake_skill_and_the_playbook_start_fm_incident(self):
        for text in (self.read("intake", "SKILL.md"), self.read("playbooks", "references", "ops", "incident.md")):
            self.assertRegex(text, r"sampo capture --incident '<impact>' --signal")
        self.assertIn("live harm", self.read("intake", "SKILL.md"))

    def test_every_fm_incident_line_in_the_skill_docs_names_a_real_form(self):
        forms = set()
        for text in (self.read("intake", "SKILL.md"), self.read("playbooks", "references", "ops", "incident.md")):
            forms |= set(re.findall(r"(?:fm|sampo) capture --incident (log|follow-up|status)\b", text))
        self.assertTrue({"log", "follow-up"} <= forms, forms)


if __name__ == "__main__":
    unittest.main()
