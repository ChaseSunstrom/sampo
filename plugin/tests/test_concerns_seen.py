"""T-5026: a task whose result the user would look for closes only with a real-surface run. surface_gaps gates a task
whose scope or attributed edits touch user-visible paths (core.ui_paths: markup, styles, components), not only a task
reopened by a repeated concern. The decision reads files, never the words of a title. No test here calls a model."""
import os
import json
from unittest import mock

from helpers import ForemanTestCase

from sampo import concerns as cn
from sampo import core as c


class Seen(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)

    def task(self, title="tidy the page", scope=()):
        tid = json.loads(self.fm("capture", title, "--type", "FEATURE", "--tier", "S", "--json").stdout)["id"]
        b = c.find_brief(self.p, tid)
        if scope:
            b.meta["scope"] = list(scope)
            c.save_brief(self.p, b)
        return tid

    def brief(self, tid):
        return c.find_brief(self.p, tid)

    def touch(self, tid, rel):
        c.log_event(self.p, "touched", task=tid, data={"file": os.path.join(self.repo, rel)})

    def run_line(self, tid, cmd, step=1, ok=True):
        b = self.brief(tid)
        if not b.steps():
            b.add_step("do it")
        b.add_evidence(cmd, "exit 0" if ok else "✗ exit 1", step=step, ran=True)
        c.save_brief(self.p, b)

    def test_a_web_edit_with_only_unit_evidence_is_refused_and_a_smoke_run_closes_it(self):
        tid = self.task()
        self.touch(tid, "web/app.css")
        self.run_line(tid, "python3 plugin/tests/run.py test_x")
        got = cn.surface_gaps(self.p, self.brief(tid))
        self.assertEqual(len(got), 1)
        self.assertIn("web/app.css", got[0])
        self.assertIn("sampo shot URL", got[0], "the refusal names the surface commands that run here (T-6023)")
        self.assertNotIn("sampo smoke", got[0], "no smoke target is set: sampo smoke would exit 1")
        self.assertIn("python3 plugin/tests/run.py test_x", got[0], "and the runs that are not one")
        self.run_line(tid, "sampo smoke")
        self.assertEqual(cn.surface_gaps(self.p, self.brief(tid)), [])

    def test_a_red_surface_run_or_a_typed_line_does_not_close_it(self):
        tid = self.task()
        self.touch(tid, "web/index.html")
        self.run_line(tid, "sampo smoke", ok=False)
        self.assertEqual(len(cn.surface_gaps(self.p, self.brief(tid))), 1)
        b = self.brief(tid)
        b.add_evidence("sampo shot http://localhost:1/", "exit 0", step=1, ran=False)
        c.save_brief(self.p, b)
        self.assertEqual(len(cn.surface_gaps(self.p, self.brief(tid))), 1)

    def test_the_run_the_close_is_about_to_make_counts(self):
        tid = self.task()
        self.touch(tid, "web/app.tsx")
        self.assertEqual(cn.surface_gaps(self.p, self.brief(tid), then=[(1, "sampo shot http://localhost:1/")]), [])
        self.assertEqual(len(cn.surface_gaps(self.p, self.brief(tid), then=[(1, "python3 -m unittest")])), 1)

    def test_scope_alone_gates_a_task_that_has_not_edited_yet(self):
        tid = self.task(scope=["web/**/*.css"])
        self.assertEqual(len(cn.surface_gaps(self.p, self.brief(tid))), 1)

    def test_a_task_with_no_user_visible_path_is_not_gated_and_reads_no_events(self):
        tid = self.task(title="fix the page layout css", scope=["plugin/lib/x.py"])
        self.touch(tid, "plugin/lib/x.py")
        self.touch(tid, "plugin/tests/test_ui.css")
        self.assertEqual(cn.user_facing(self.p, self.brief(tid)), [], "the title's words and a test's css decide nothing")
        with mock.patch.object(cn, "_events", side_effect=AssertionError("read")):
            self.assertEqual(cn.surface_gaps(self.p, self.brief(tid)), [], "words in the title decide nothing")

    def test_another_tasks_edit_is_not_this_tasks(self):
        a, b = self.task(), self.task(title="other")
        self.touch(a, "web/app.css")
        self.assertEqual(cn.surface_gaps(self.p, self.brief(b)), [])

    def test_user_facing_names_the_files(self):
        tid = self.task(scope=["site/*.html"])
        self.touch(tid, "web/app.css")
        self.touch(tid, "plugin/lib/x.py")
        self.assertEqual(sorted(cn.user_facing(self.p, self.brief(tid))), ["site/*.html", "web/app.css"])

    def test_a_reader_that_breaks_fails_open(self):
        tid = self.task(scope=["web/*.css"])
        with mock.patch.object(c, "all_touches", side_effect=OSError("torn")):
            self.assertEqual(len(cn.surface_gaps(self.p, self.brief(tid))), 1, "scope still gates; the edits are skipped")
        with mock.patch.object(c, "ui_paths", side_effect=RuntimeError("x")):
            self.assertEqual(cn.safe_gaps(self.p, self.brief(tid)), [])

    def test_many_tasks_read_the_ledger_once_until_it_grows(self):
        a, b = self.task(), self.task(title="other")
        self.touch(a, "web/app.css")
        with mock.patch.object(c, "all_touches", wraps=c.all_touches) as m:
            cn.surface_gaps(self.p, self.brief(a))
            cn.surface_gaps(self.p, self.brief(b))
            self.assertEqual(m.call_count, 1)
            self.touch(b, "web/b.css")
            self.assertEqual(len(cn.surface_gaps(self.p, self.brief(b))), 1, "a new edit is seen")
            self.assertEqual(m.call_count, 2)

    def test_a_reopened_task_with_its_own_step_keeps_its_own_message(self):
        tid = self.task(scope=["web/*.css"])
        b = self.brief(tid)
        b.add_step(f"{cn.SURFACE_STEP} (K-abc123): look")
        c.save_brief(self.p, b)
        got = cn.surface_gaps(self.p, self.brief(tid))
        self.assertEqual(len(got), 1, "one refusal for the step, not a second for the paths")
        self.assertIn("reported again", got[0])

    # --- review fixes (T-5026)
    def ac_line(self, tid, cmd, n=1, ts=None):
        b = self.brief(tid)
        b.add_evidence(cmd, "exit 0", ac=n, ran=True, ts=ts)
        c.save_brief(self.p, b)

    def test_a_criterions_surface_run_counts_in_a_task_with_no_steps(self):
        tid = self.task()
        self.touch(tid, "web/app.css")
        self.assertEqual(len(cn.surface_gaps(self.p, self.brief(tid))), 1)
        self.ac_line(tid, "sampo shot http://localhost:1/")
        self.assertEqual(cn.surface_gaps(self.p, self.brief(tid)), [])
        other = self.task(title="other")
        self.touch(other, "web/b.css")
        self.assertEqual(cn.surface_gaps(self.p, self.brief(other), then_ac=[(1, "sampo smoke")]), [])
        self.assertEqual(len(cn.surface_gaps(self.p, self.brief(other), then_ac=[(1, "python3 -m unittest")])), 1)

    def test_a_criterions_run_is_not_a_surface_steps_run(self):
        tid = self.task(scope=["web/*.css"])
        b = self.brief(tid)
        b.add_step(f"{cn.SURFACE_STEP}: look")
        c.save_brief(self.p, b)
        self.ac_line(tid, "sampo smoke")
        self.assertEqual(len(cn.surface_gaps(self.p, self.brief(tid))), 1)

    def test_a_terminal_ui_edit_is_not_gated_on_a_surface_run(self):
        tid = self.task()
        for rel in ("plugin/lib/sampo/fmcodetui.py", "mods/x/hooks/a.ts", "plugin/bin/statusline"):
            self.touch(tid, rel)
        self.assertEqual(cn.user_facing(self.p, self.brief(tid)), [])
        self.assertEqual(cn.surface_gaps(self.p, self.brief(tid)), [])
        self.touch(tid, "web/app.css")
        self.assertEqual(cn.user_facing(self.p, self.brief(tid)), ["web/app.css"])

    def test_a_run_before_the_last_ui_edit_does_not_count(self):
        tid = self.task()
        self.touch(tid, "web/app.css")
        b = self.brief(tid)
        b.add_step("do it")
        b.add_evidence("sampo smoke", "exit 0", step=1, ran=True, ts="2000-01-01T00:00:00Z")
        c.save_brief(self.p, b)
        got = cn.surface_gaps(self.p, self.brief(tid))
        self.assertEqual(len(got), 1, "the smoke run is older than the edit")
        self.assertEqual(cn.surface_gaps(self.p, self.brief(tid), then=[(1, "sampo smoke")]), [], "the close's own run is new")
        b = self.brief(tid)
        b.add_evidence("sampo smoke", "exit 0", step=1, ran=True, ts="2999-01-01T00:00:00Z")
        c.save_brief(self.p, b)
        self.assertEqual(cn.surface_gaps(self.p, self.brief(tid)), [])

    def test_only_a_command_that_starts_with_a_surface_program_counts(self):
        tid = self.task()
        self.touch(tid, "web/app.css")
        for cmd in ("echo sampo smoke", "pytest -k curl", "which curl", "true sampo shot", "cat curl.txt && true",
                    "sampo smoke || true"):
            self.assertEqual(len(cn.surface_gaps(self.p, self.brief(tid), then=[(1, cmd)])), 1, cmd)
        for cmd in ("FOO=1 python3 plugin/bin/sampo smoke", "cd web && sampo shot http://localhost:1/",
                    "curl -sf http://localhost:1/", "python3 plugin/bin/sampo stories", "fm flow x"):
            self.assertEqual(cn.surface_gaps(self.p, self.brief(tid), then=[(1, cmd)]), [], cmd)

    def test_a_projects_own_surface_command_is_an_exact_head(self):
        tid = self.task()
        self.touch(tid, "web/app.css")
        meta = c.read_meta(self.p)
        meta["surface_commands"] = ["make deploy-staging"]
        c.write_meta(self.p, meta)
        self.assertEqual(cn.surface_gaps(self.p, self.brief(tid), then=[(1, "make deploy-staging")]), [])
        self.assertEqual(len(cn.surface_gaps(self.p, self.brief(tid), then=[(1, "echo make deploy-staging")])), 1)
        self.assertEqual(len(cn.surface_gaps(self.p, self.brief(tid), then=[(1, "make test")])), 1)

    def test_a_logged_waiver_with_a_reason_is_the_way_out_and_the_refusal_names_it(self):
        tid = self.task()
        self.touch(tid, "web/app.css")
        got = cn.surface_gaps(self.p, self.brief(tid))
        self.assertIn("surface waived:", got[0])
        b = self.brief(tid)
        b.append_log("surface waived:")
        c.save_brief(self.p, b)
        self.assertEqual(len(cn.surface_gaps(self.p, self.brief(tid))), 1, "a waiver needs a reason")
        b = self.brief(tid)
        b.append_log("surface waived: CI box has no browser and the page has no URL")
        c.save_brief(self.p, b)
        self.assertEqual(cn.surface_gaps(self.p, self.brief(tid)), [])

    def test_task_done_refuses_a_web_edit_on_unit_evidence_and_names_the_surface_run(self):  # the CLI close is wired
        tid = self.task()
        self.touch(tid, "web/app.css")
        self.fm("task", "step", tid, "add", "restyle the page")
        self.fm("task", "step", tid, "done", "1", "--evidence", "python3 plugin/tests/run.py test_x", "ok", check=False)
        r = self.fm("task", "done", tid, check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("web/app.css", r.stdout + r.stderr)
        self.assertIn("sampo shot URL", r.stdout + r.stderr)
