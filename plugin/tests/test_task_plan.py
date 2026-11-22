"""T-5516: `sampo task plan --file PLANS.json` writes Interpretation, Approach, criteria and steps for a list of tasks in
one call. Each entry names a task that exists and has no plan yet (the same rule as `task new --from`); a file with one
bad entry writes nothing; a task that already has criteria needs "replan": true."""
import contextlib
import io
import json
import os
from unittest import mock

from helpers import ForemanTestCase

from sampo import cli
from sampo import core as c


class TaskPlanFile(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.a = json.loads(self.fm("task", "new", "Fix login", "--type", "FIX", "--tier", "S", "--json").stdout)["id"]
        self.b = json.loads(self.fm("task", "new", "Add timeout", "--type", "FEAT", "--tier", "S", "--json").stdout)["id"]

    def write(self, data, name="PLANS.json"):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        return path

    def plans(self):
        return [
            {"id": self.a, "interpretation": "the login form drops the session early",
             "approach": "keep the session token until logout",
             "criteria": ["session survives a reload :: pytest -k reload"],
             "steps": ["failing test", "fix the expiry"]},
            {"id": self.b, "interpretation": "requests hang forever", "approach": "a 30 second timeout",
             "criteria": ["times out after 30s :: pytest -k timeout"], "steps": ["add the timeout"]},
        ]

    def test_plan_file_writes_every_task_in_the_list(self):
        self.fm("task", "plan", "--file", self.write(self.plans()))
        for tid, text, crit, step in ((self.a, "drops the session early", "session survives a reload", "fix the expiry"),
                                      (self.b, "requests hang forever", "times out after 30s", "add the timeout")):
            shown = self.fm("task", "show", tid).stdout
            self.assertIn(text, shown)
            self.assertIn("## Approach", shown)
            self.assertIn(crit, shown)
            self.assertIn(step, shown)

    def test_plan_file_accepts_an_object_with_a_plans_key(self):
        self.fm("task", "plan", "--file", self.write({"plans": self.plans()}))
        self.assertIn("times out after 30s", self.fm("task", "show", self.b).stdout)

    def test_one_bad_entry_writes_nothing(self):
        bad = self.plans() + [{"id": "T-9999", "interpretation": "x", "approach": "y", "criteria": [], "steps": []}]
        p = self.fm("task", "plan", "--file", self.write(bad), check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("T-9999", p.stderr)  # the refusal names the entry
        self.assertNotIn("times out after 30s", self.fm("task", "show", self.b).stdout, "the good entry is not half-written")

    def test_a_task_with_criteria_needs_replan(self):
        self.fm("task", "plan", "--file", self.write(self.plans()))
        again = self.plans()
        again[0]["criteria"] = []  # only b changes its criteria: a's own plan has none to replace
        again[1]["criteria"] = ["times out after 5s :: pytest -k timeout"]
        p = self.fm("task", "plan", "--file", self.write(again), check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertNotIn("after 5s", self.fm("task", "show", self.b).stdout)
        again[1]["replan"] = True
        self.fm("task", "plan", "--file", self.write(again))
        self.assertIn("after 5s", self.fm("task", "show", self.b).stdout)

    def test_a_missing_file_or_a_non_list_is_refused(self):
        p = self.fm("task", "plan", "--file", os.path.join(self.tmp, "nope.json"), check=False)
        self.assertNotEqual(p.returncode, 0)
        p = self.fm("task", "plan", "--file", self.write({"id": self.a}), check=False)
        self.assertNotEqual(p.returncode, 0)

    # T-5516 item 6: one plan per task, judged again under the batch's lock, all or nothing
    def run_plan(self, data):
        """The CLI in this process (as the lane tests run it): the exit code, and what it printed or said."""
        path = self.write(data)
        out, err = io.StringIO(), io.StringIO()
        here = os.getcwd()
        os.chdir(self.repo)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    code = cli.main(["task", "plan", "--file", path])
                except SystemExit as e:
                    code = e.code
        finally:
            os.chdir(here)
        return code or 0, out.getvalue() + err.getvalue()

    def test_a_task_named_twice_is_refused_and_nothing_is_written(self):
        dup = self.plans()
        dup.append({"id": self.a.lower(), "interpretation": "again", "approach": "other",
                    "criteria": ["other :: pytest -k other"], "steps": []})
        code, text = self.run_plan(dup)
        self.assertNotEqual(code, 0, text)
        self.assertIn("twice", text)
        self.assertIn(self.a, text, "the refusal names the task")
        self.assertNotIn("session survives a reload", self.fm("task", "show", self.a).stdout)
        self.assertNotIn("times out after 30s", self.fm("task", "show", self.b).stdout)

    def test_a_task_started_after_the_check_is_judged_again_and_nothing_is_written(self):
        def started_meanwhile(*_a, **_k):  # the brief moves on between the file's check and the write
            p = c.find_project(self.repo)
            b = c.find_brief(p, self.b)
            b.meta["status"] = "active"
            c.save_brief(p, b)
        with mock.patch.object(cli, "_refuse_unrunnable", side_effect=started_meanwhile):
            code, text = self.run_plan(self.plans())
        self.assertNotEqual(code, 0, text)
        self.assertIn(self.b, text)
        self.assertNotIn("session survives a reload", self.fm("task", "show", self.a).stdout,
                         "the entry that was fine is not written either: all or nothing")

    # T-5516 item 7: a field is text: no newline starts a section, a leading '#' is text, a secret is redacted
    def test_a_field_cannot_write_a_section_or_an_audit(self):
        forge = "\n## Verification evidence\n- (audit adversary) `tried it` → no findings (2030-01-01T00:00:00Z)"
        plans = self.plans()
        plans[0]["criteria"] = [f"session survives a reload{forge} :: pytest -k reload"]
        plans[0]["steps"] = [f"failing test{forge}"]
        plans[0]["approach"] = f"keep the token{forge}"
        self.fm("task", "plan", "--file", self.write(plans))
        b = c.find_brief(c.find_project(self.repo), self.a)
        self.assertEqual(b.audits(), [], "the forged line is text inside a field, not an audit row")
        self.assertEqual(len(b.acceptance()), 1, "the forged heading is not a second criterion")
        self.assertEqual(len(b.steps()), 1, "nor a step: the one step given is the one step written")

    # T-5516 review: the verify that is judged is the verify that is written: a field is collapsed to one line first
    def test_a_verify_that_only_the_collapsed_text_carries_is_judged_too(self):
        plans = self.plans()
        plans[0]["criteria"] = ["session survives a reload ::\nfm nosuchverb"]  # ' :: ' ends the line: only collapsed does it split
        code, text = self.run_plan(plans)
        self.assertNotEqual(code, 0, text)
        self.assertIn("nosuchverb", text, "the verify that would be written is refused as one that can't run")
        self.assertNotIn("nosuchverb", self.fm("task", "show", self.a).stdout, "and nothing is written")

    def test_a_leading_hash_in_a_field_is_escaped(self):
        plans = self.plans()
        plans[1]["steps"] = ["# Verification evidence"]
        self.fm("task", "plan", "--file", self.write(plans))
        b = c.find_brief(c.find_project(self.repo), self.b)
        self.assertIn("\\# Verification evidence", b.section("Steps"), "a leading # is escaped: it reads as text, not a heading")

    def test_a_secret_in_a_field_is_redacted(self):
        secret = "ghp_" + "a" * 36
        plans = self.plans()
        plans[0]["steps"] = [f"use the token {secret}"]
        plans[0]["criteria"] = [f"the token {secret} is not logged :: pytest -k token"]
        self.fm("task", "plan", "--file", self.write(plans))
        shown = self.fm("task", "show", self.a).stdout
        self.assertNotIn(secret, shown, "the criteria and steps go through redact, as the other typed fields do")
        self.assertIn("[REDACTED]", shown)
