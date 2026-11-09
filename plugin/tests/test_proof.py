"""fm prove --rollup (fm proof until T-2461): done tasks whose claims no run here proved (T-2273): typed evidence, inconclusive runs, UI edits unshot."""
import json
import os

from helpers import ForemanTestCase

from sampo import core as c


class Proof(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)

    def task(self, *files, evidence=(), type="FIX", lines=(), decision=None, base=None, log=()):
        """A done task; evidence: (flags..., cmd, result) tuples handed to fm task evidence; files: its edits; lines:
        (step, cmd, result) evidence lines written as fm's own runs (ran); base: the commit its change starts at."""
        tid = self.fm_json("task", "new", "Do a thing", "--type", type, "--tier", "S", "--ac", "ok :: true",
                           "--step", "s")["id"]
        for f in files:  # the edits come first: a green run only clears a kind when it came after the last edit
            path = os.path.join(self.repo, f)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if not os.path.exists(path):  # the classifiers read the file itself
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write("FROM scratch\n" if f.endswith("Dockerfile") else "x\n")
            c.log_event(self.p, "touched", task=tid, data={"file": path})
        for ev in evidence:
            self.fm("task", "evidence", tid, *ev)
        with c.lock(self.p.dir):
            b = c.find_brief(self.p, tid)
            for step, cmd, result in lines:
                b.add_evidence(cmd, result, step=step, ran=True)
            for x in log:
                b.append_log(x)
            if decision:
                b.set_section("Decision", decision)
            if base:
                b.meta["base"] = base
            b.meta["status"] = "done"
            c.save_brief(self.p, b)
        return tid

    def needs(self, tid):
        return {x["kind"]: x for x in next(r for r in c.unproven(self.p) if r["id"] == tid)["needs"]}

    def listed(self):
        return {x["id"]: x["kinds"] for x in self.fm_json("prove", "--rollup")["tasks"]}

    def test_typed_inconclusive_and_unshot_ui_are_each_listed(self):
        typed = self.task(evidence=[("--step", "1", "pytest", "9 passed")])
        inc = self.task(evidence=[("--step", "1", "--run", "true", "--inconclusive")])
        ui = self.task("web/app.css", evidence=[("--step", "1", "--run", "true")])
        got = self.listed()
        self.assertEqual(got[typed], ["typed"])
        self.assertEqual(got[inc], ["inconclusive"])
        self.assertEqual(got[ui], ["ui-unshot"])

    def test_a_task_whose_every_check_ran_green_is_never_listed(self):
        ok = self.task("calc.py", evidence=[("--step", "1", "--run", "true")])
        shot = self.task("web/app.css", evidence=[("--step", "1", "--run", "true")],
                         lines=[(1, "fm shot app.css", self.GREEN)])
        self.assertNotIn(ok, self.listed())
        self.assertNotIn(shot, self.listed(), "a shot named in the evidence covers the UI change")

    def test_a_later_green_run_supersedes_a_typed_or_inconclusive_line(self):
        t = self.task(evidence=[("--step", "1", "pytest", "ok"), ("--step", "1", "--run", "true")])
        self.assertNotIn(t, self.listed())

    def test_a_warm_status_reads_the_cache_not_the_briefs_or_the_ledger(self):
        from unittest import mock
        t = self.task(evidence=[("--step", "1", "pytest", "ok")])
        tdir = os.path.join(self.p.dir, "tasks")
        src = next(f for f in os.listdir(tdir) if f.startswith(t))
        text = open(os.path.join(tdir, src), encoding="utf-8").read()
        for i in range(500):  # 500 more closed briefs
            n = 900 + i
            with open(os.path.join(tdir, f"T-{n}-x.md"), "w", encoding="utf-8") as f:
                f.write(text.replace(t, f"T-{n}"))
        self.assertIn("unproven: 501", self.fm("status").stdout)  # cold: fills the cache
        with mock.patch.object(c, "ledger_tail", side_effect=AssertionError("ledger read")), \
                mock.patch.object(c, "load_briefs", wraps=c.load_briefs) as lb:
            self.assertGreaterEqual(c.unproven_cached(self.p), 0)
            self.assertEqual(lb.call_count, 0, "a warm call never loads the closed briefs")
        t2 = self.task(evidence=[("--step", "1", "pytest", "ok")])  # a new closed brief changes the key
        self.assertIn(t2, [r["id"] for r in c.unproven(self.p)])
        self.assertEqual(self.fm_json("status", "--json")["unproven"], c.unproven_cached(self.p))

    def test_the_window_bounds_the_list_and_the_count_is_on_status_and_digest(self):
        t = self.task(evidence=[("--step", "1", "pytest", "ok")])
        self.assertEqual(self.fm_json("prove", "--rollup", "--days", "7")["count"], 1)
        with c.lock(self.p.dir):
            b = c.find_brief(self.p, t)
            b.meta["updated"] = "2020-01-01T00:00:00Z"
            c.save_brief(self.p, b, touch=False)
        self.assertEqual(self.fm_json("prove", "--rollup", "--days", "7")["count"], 0)
        self.assertEqual(self.fm_json("prove", "--rollup", "--days", "100000")["count"], 1)
        self.assertEqual(self.fm("prove", "--rollup").stdout.count(t), 0)
        t2 = self.task(evidence=[("--step", "1", "pytest", "ok")])
        self.assertIn("unproven: 1 (sampo prove --rollup)", self.fm("status").stdout)
        self.assertIn("unproven: 1 (sampo prove --rollup)", self.fm("digest").stdout)
        self.assertEqual(self.fm_json("status", "--json")["unproven"], 1)
        self.assertIn(t2, self.fm("prove", "--rollup").stdout)
        self.assertEqual(json.loads(self.fm("prove", "--rollup", "--json").stdout)["count"], 1)

    # --- T-2391: the proof-kinds table, one row per domain
    GREEN, RED, RED1ST = "exit 0 · ok", "✗ exit 1 · boom", "red as expected (exit 1) · boom"

    def test_rollup_red_first_a_red_step_counts_only_when_the_same_command_ran_green_later(self):
        later = self.task(lines=[(1, "pytest -q t", self.RED1ST), (2, "pytest -q t", self.GREEN)])
        never = self.task(lines=[(1, "pytest -q t", self.RED)])
        before = self.task(lines=[(2, "pytest -q t", self.GREEN), (1, "pytest -q t", self.RED)])
        other = self.task(lines=[(1, "pytest -q a", self.RED), (2, "pytest -q b", self.GREEN)])
        got = self.listed()
        self.assertNotIn(later, got, "red then the same command green: proven")
        self.assertEqual(got[never], ["red"])
        self.assertEqual(got[before], ["red"], "a green run before the red one doesn't clear it")
        self.assertEqual(got[other], ["red"], "a different command's green run doesn't clear it")

    def test_rollup_red_first_a_bench_line_is_a_green_run(self):
        bench = "slow: 2.000s → 1.000s (5 runs each)"
        t = self.task(type="PERFORMANCE", lines=[(1, "slow", self.RED1ST), (1, "slow", bench)])
        self.assertNotIn(t, self.listed())

    def test_a_terminal_ui_needs_a_run_that_renders_it(self):  # T-2396: fm shot can't shoot a terminal
        tui = self.task("plugin/lib/fmcodetui.py", evidence=[("--step", "1", "--run", "true")])
        ran = self.task("plugin/lib/fmcodetui.py", lines=[(1, "python3 plugin/lib/fmcodetui.py --demo", self.GREEN)])
        said = self.task("plugin/lib/fmcodetui.py", lines=[(1, "echo fmcodetui", self.GREEN)])  # names it, runs nothing
        pane = self.task("mods/x/hooks/index.ts", evidence=[("--step", "1", "--run", "true")])
        tested = self.task("mods/x/hooks/index.ts", lines=[(1, "claude plugin test .", self.GREEN)])
        web = self.task("web/app.css", lines=[(1, "python3 plugin/lib/fmcodetui.py", self.GREEN)])
        got = self.listed()
        for t in (tui, said, pane):
            self.assertEqual(got.get(t), ["tui-unshot"], t)
        self.assertNotIn(ran, got)
        self.assertNotIn(tested, got)
        self.assertEqual(got[web], ["ui-unshot"], "a web file still needs fm shot")

    def test_proof_kind_table_names_every_kind_with_its_upgrade_command(self):
        for kind in ("typed", "red", "inconclusive", "ui-unshot", "tui-unshot", "docs", "infra", "migration", "data",
                     "perf-typed",
                     "perf-noise", "research-uncited", "hollow"):
            row = c.PROOF_KINDS[kind]
            self.assertTrue(row["need"] and row["upgrade"] and "{id}" in row["upgrade"], kind)
        t = self.task("data/users.csv")
        out = self.fm("prove", "--rollup").stdout
        self.assertIn(t, out)
        self.assertIn(f"sampo task evidence {t} --run", out, "each row prints its upgrade command")

    def _doc_task(self, text):
        base = c._git(self.repo, "rev-parse", "HEAD").strip()
        os.makedirs(os.path.join(self.repo, "docs"), exist_ok=True)
        with open(os.path.join(self.repo, "docs", "guide.md"), "a", encoding="utf-8") as f:
            f.write(text)
        c._git(self.repo, "add", "-A")
        c._git(self.repo, "-c", "user.email=t@e.c", "-c", "user.name=t", "commit", "-qm", "docs")
        return self.task("docs/guide.md", evidence=[("--step", "1", "--run", "true")], base=base)

    def test_proof_kind_docs_needs_a_checkable_claim_in_the_lines_the_task_added(self):
        claim = self._doc_task("Run `fm proof --days 7` to see it.\n")
        prose = self._doc_task("This is only prose about nothing you could run.\n")
        got = self.listed()
        self.assertEqual(got[claim], ["docs"])
        self.assertNotIn(prose, got, "a prose-only edit never counts")
        self.assertIn("sampo prove --docs", self.needs(claim)["docs"]["upgrade"])

    def test_proof_kind_docs_reads_only_the_lines_the_task_itself_landed(self):  # T-2616
        def commit(msg, text):
            with open(os.path.join(self.repo, "docs", "guide.md"), "a", encoding="utf-8") as f:
                f.write(text)
            c._git(self.repo, "add", "-A")
            c._git(self.repo, "-c", "user.email=t@e.c", "-c", "user.name=t", "commit", "-qm", msg)
        os.makedirs(os.path.join(self.repo, "docs"), exist_ok=True)
        base = c._git(self.repo, "rev-parse", "HEAD").strip()
        prose = self.task("docs/guide.md", evidence=[("--step", "1", "--run", "true")], base=base)
        commit(f"{prose}: prose", "Only prose about nothing you could run.\n")
        base = c._git(self.repo, "rev-parse", "HEAD").strip()
        claim = self.task("docs/guide.md", evidence=[("--step", "1", "--run", "true")], base=base)
        commit(f"{claim}: a claim", "Run `fm proof --days 7` to see it.\n")  # lands after prose's commit
        got = self.listed()
        self.assertNotIn(prose, got, "a later task's claim is not this task's")
        self.assertEqual(got[claim], ["docs"])

    def test_proof_kind_docs_skips_history_files_fm_prove_docs_never_reads(self):  # T-2616
        base = c._git(self.repo, "rev-parse", "HEAD").strip()
        with open(os.path.join(self.repo, "CHANGELOG.md"), "a", encoding="utf-8") as f:
            f.write("- `fm ship --chance N` books a bet.\n")  # a record of the time, not a claim fmdocs checks (T-1817)
        c._git(self.repo, "add", "-A")
        c._git(self.repo, "-c", "user.email=t@e.c", "-c", "user.name=t", "commit", "-qm", "changelog")
        tid = self.task("CHANGELOG.md", evidence=[("--step", "1", "--run", "true")], base=base)
        self.assertNotIn(tid, self.listed())

    def test_proof_kind_docs_is_cleared_by_a_green_prove_docs_run(self):
        t = self._doc_task("Use `make test` first.\n")
        with c.lock(self.p.dir):
            b = c.find_brief(self.p, t)
            b.add_evidence("fm prove --docs", self.GREEN, step=1, ran=True)
            c.save_brief(self.p, b)
        self.assertNotIn(t, self.listed())

    def test_proof_kind_infra_migration_and_data_are_read_from_the_files_touched(self):
        run = [("--step", "1", "--run", "true")]
        infra = self.task("Dockerfile", evidence=run)
        mig = self.task("db/migrations/001_init.sql", evidence=run)
        data = self.task("data/users.csv", evidence=run)
        code = self.task("calc.py", evidence=run)
        got = self.listed()
        self.assertEqual((got[infra], got[mig], got[data]), (["infra"], ["migration"], ["data"]))
        self.assertNotIn(code, got)
        self.assertIn("sampo prove --infra", self.needs(infra)["infra"]["upgrade"])
        self.assertIn("sampo prove --migration", self.needs(mig)["migration"]["upgrade"])
        cleared = self.task("Dockerfile", "data/users.csv", evidence=run,
                            lines=[(1, "fm prove --infra", self.GREEN), (1, "dbt test", self.GREEN)])
        self.assertNotIn(cleared, self.listed(), "the green run that clears a kind removes it")

    def test_proof_kind_perf_typed_is_a_performance_close_without_a_bench_run(self):
        plain = self.task(type="PERFORMANCE", evidence=[("--step", "1", "--run", "true")])
        bench = self.task(type="PERFORMANCE", lines=[(1, "slow", "slow: 2.000s → 1.000s (5 runs each)")])
        fix = self.task(evidence=[("--step", "1", "--run", "true")])
        got = self.listed()
        self.assertEqual(got[plain], ["perf-typed"])
        self.assertNotIn(bench, got)
        self.assertNotIn(fix, got, "only PERFORMANCE closes need a bench")
        self.assertIn("--bench", self.needs(plain)["perf-typed"]["upgrade"])

    def test_perf_noise_a_bench_inside_the_gates_noise_band_is_listed(self):
        noisy = self.task(type="PERFORMANCE", lines=[(1, "slow", "slow: 1.000s → 0.950s (5 runs each)")])
        real = self.task(type="PERFORMANCE", lines=[(1, "slow", "slow: 1.000s → 0.500s (5 runs each)")])
        slower = self.task(type="PERFORMANCE", lines=[(1, "slow", "slow: 1.000s → 1.800s (5 runs each)")])
        got = self.listed()
        self.assertEqual(got[noisy], ["perf-noise"])
        self.assertNotIn(real, got)
        self.assertNotIn(slower, got, "a change past the band either way is not noise")
        self.assertIn("--repeat", self.needs(noisy)["perf-noise"]["upgrade"])

    def test_proof_kind_research_uncited_names_no_saved_note_with_a_verified_claim(self):
        notes = os.path.join(self.p.dir, "research")
        os.makedirs(notes, exist_ok=True)
        with open(os.path.join(notes, "queues.md"), "w", encoding="utf-8") as f:
            f.write("# queues\n- ✓ claim one\n")
        with open(os.path.join(notes, "weak.md"), "w", encoding="utf-8") as f:
            f.write("# weak\n- ? claim\n")
        bare = self.task(type="RESEARCH", decision="Use a queue.")
        cited = self.task(type="RESEARCH", decision="Use a queue (research/queues.md).")
        weak = self.task(type="RESEARCH", decision="Use a queue (weak.md).")
        got = self.listed()
        self.assertEqual(got[bare], ["research-uncited"])
        self.assertEqual(got[weak], ["research-uncited"], "a note with no verified claim isn't a citation")
        self.assertNotIn(cited, got)
        self.assertIn("sampo ask", self.needs(bare)["research-uncited"]["upgrade"])

    def test_proof_kind_hollow_is_a_criterion_the_newest_vacuous_run_found_passing_on_the_start_tree(self):
        run = [("--step", "1", "--run", "true")]
        hollow = self.task(evidence=run, log=["vacuous checks: ac 1"])
        fixed = self.task(evidence=run, log=["vacuous checks: ac 1", "vacuous checks: none"])
        got = self.listed()
        self.assertEqual(got[hollow], ["hollow"])
        self.assertNotIn(fixed, got)
        self.assertIn("--vacuous", self.needs(hollow)["hollow"]["upgrade"])

    def test_unproven_line_is_one_function_the_four_renderers_call(self):
        self.assertEqual(c.unproven_line(3), "unproven: 3 (sampo prove --rollup)")
        self.assertEqual(c.unproven_line(3, {"docs": 2, "typed": 1}), "unproven: 3 (sampo prove --rollup) · typed 1, docs 2")
        self.assertEqual(c.unproven_line(0), "")
        self.task(evidence=[("--step", "1", "pytest", "ok")])
        for verb in ("status", "digest"):  # one function's text, with the kind counts
            self.assertIn(c.unproven_line(1, {"typed": 1}), self.fm(verb).stdout, verb)
        self._renderers_call_unproven_line()

    def _renderers_call_unproven_line(self):
        """The genesis status text and the exam scorecard, run with the line function stubbed: each prints its return."""
        import argparse
        import io
        from contextlib import redirect_stdout
        from unittest import mock

        from sampo import louhi
        from sampo import rune
        s = {"dir": "x", "gen0": False, "gap_map": False, "lineage": False, "coverage": False, "not_yet": False,
             "generations": [], "live": None, "exam_lines": 0, "unproven": 2, "unproven_kinds": {"typed": 2}}
        with mock.patch.object(c, "unproven_line", return_value="STUB-LINE") as line:
            self.assertIn("STUB-LINE", rune._text(s))
            line.assert_called_with(2, {"typed": 2})
        card = {"rows": [], "agent": "foreman", "bar": 0.8, "smoke": 0, "selfmade": 0, "gaps": [], "seen": True,
                "ok": True, "long": {"state": "no long run"}}
        args = argparse.Namespace(action="scorecard", agent="foreman", bar=0.8, json=False, project=self.p.slug)
        with mock.patch.object(c, "unproven_line", return_value="STUB-LINE") as line, \
                mock.patch.object(rune, "scorecard", return_value=dict(card)), \
                mock.patch.object(c, "unproven_summary", return_value={"n": 3, "counts": {"docs": 3}}), \
                redirect_stdout(io.StringIO()) as buf:
            louhi.cmd_exam(args)
        self.assertIn("STUB-LINE", buf.getvalue())
        line.assert_called_with(3, {"docs": 3})

    def test_unproven_line_fm_proof_json_carries_per_kind_counts(self):
        self.task(evidence=[("--step", "1", "pytest", "ok")])
        self.task("data/a.csv", evidence=[("--step", "1", "--run", "true")])
        self.task("data/b.csv", evidence=[("--step", "1", "pytest", "ok")])
        d = self.fm_json("prove", "--rollup")
        self.assertEqual(d["counts"], {"typed": 2, "data": 2})
        self.assertEqual(d["count"], 3)

    def test_proof_kind_finish_names_each_rollup_kind_the_closing_task_still_needs(self):
        self.fm("task", "new", "One", "--type", "FEATURE", "--tier", "S", "--ac", "ran :: true", "--step", "a",
                "--focus")
        c.log_event(self.p, "touched", task="T-0001", data={"file": os.path.join(self.repo, "data/users.csv")})
        out = self.fm("task", "finish", "T-0001", "--run", "true", "--audit", "self check").stdout
        block = out.split("Not verified", 1)[1]
        self.assertIn("data", block)
        self.assertIn("sampo task evidence T-0001 --run", block)

    def test_proof_kind_next_names_the_newest_unproven_row_that_can_run_here(self):
        from unittest import mock
        old = self.task(evidence=[("--step", "1", "python3 -m unittest", "ok")])
        wait = self.task("Dockerfile", evidence=[("--step", "1", "--run", "true")])
        with mock.patch.object(c.shutil, "which", return_value=None):
            rows = {r["id"]: r for r in self.fm_json("prove", "--rollup")["tasks"]}
            self.assertTrue(rows[wait]["needs"][0]["waiting"], "a missing tool is waiting, not a to-do")
            self.assertIn("waiting", self.fm("prove", "--rollup").stdout)
            nxt = c.next_for(self.p)[2]
        self.assertIn(old, nxt, "the newer row waits on an install, so the older runnable one is named")
        self.assertNotIn(wait, nxt)
        self.assertIn(f"sampo task evidence {old} --run", nxt)

    def test_proof_kind_next_is_silent_with_a_queue_and_when_nothing_can_run(self):
        from unittest import mock
        wait = self.task("Dockerfile", evidence=[("--step", "1", "--run", "true")])
        with mock.patch.object(c.shutil, "which", return_value=None):
            self.assertNotIn(wait, c.next_for(self.p)[2])
        self.fm("task", "new", "Queued", "--type", "FIX", "--tier", "S", "--ac", "ok :: true", "--step", "s")
        with mock.patch.object(c.shutil, "which", return_value="/bin/true"):
            self.assertNotIn("fm prove", c.next_for(self.p)[2], "a queued task comes first")

    # --- T-2391 review: honest upgrades, unspoofable clears, no stuck cache
    def test_review_an_upgrade_never_falls_back_to_a_run_that_cannot_fail(self):
        empty = self.task(lines=[(1, "", self.RED)])
        prose = self.task(evidence=[("--step", "1", "echo ok", "fine")])
        gone = self.task(evidence=[("--step", "1", "no-such-tool-xyz --go", "fine")])
        real = self.task(evidence=[("--step", "1", "python3 -m unittest", "ok")])
        for tid, kind in ((empty, "red"), (prose, "typed"), (gone, "typed")):
            need = self.needs(tid)[kind]
            self.assertIn("needs a check that can fail", c.proof_waiting(self.p, need), tid)
            self.assertNotIn("--run true", need["upgrade"], tid)
        ok = self.needs(real)["typed"]
        self.assertEqual(c.proof_waiting(self.p, ok), "")
        self.assertIn("python3 -m unittest", ok["upgrade"])
        self.assertIn("needs a check that can fail", self.fm("prove", "--rollup").stdout)

    def test_review_a_command_that_only_mentions_the_proof_does_not_clear_its_kind(self):
        t = self._doc_task("Run `fm proof --days 7` to see it.\n")
        data = self.task("data/a.csv", lines=[(1, "pytest test_schema.py", self.GREEN)])
        ui = self.task("web/app.css", lines=[(1, "echo fm shot app.css", self.GREEN)])
        with c.lock(self.p.dir):
            b = c.find_brief(self.p, t)
            b.add_evidence("echo prove --docs", self.GREEN, step=1, ran=True)
            c.save_brief(self.p, b)
        got = self.listed()
        self.assertEqual(got[t], ["docs"], "echo prove --docs is not a run of fm prove --docs")
        self.assertEqual(got[data], ["data"], "a test file named schema is not a data check")
        self.assertEqual(got[ui], ["ui-unshot"])
        masked = self.task("Dockerfile", lines=[(1, "fm prove --infra || true", self.GREEN)])
        self.assertEqual(self.listed()[masked], ["infra"], "a fallback hides the run's own exit status")
        env = self.task("data/b.csv", lines=[(1, "CI=1 dbt test && echo done", self.GREEN)])
        self.assertNotIn(env, self.listed(), "env assignments and a following && step are still the real run")

    def test_review_a_green_run_must_come_after_the_last_edit_of_the_files_it_covers(self):
        stale = self.task("Dockerfile")
        fresh = self.task("Dockerfile")
        for tid, ts in ((stale, "2020-01-01T00:00:00Z"), (fresh, "2999-01-01T00:00:00Z")):
            with c.lock(self.p.dir):
                b = c.find_brief(self.p, tid)
                b.add_evidence("fm prove --infra", self.GREEN, step=1, ran=True, ts=ts)
                c.save_brief(self.p, b)
        got = self.listed()
        self.assertEqual(got[stale], ["infra"], "the Dockerfile was edited after that run")
        self.assertNotIn(fresh, got)

    def test_review_vacuous_runs_are_read_from_the_start_of_a_log_line(self):
        run = [("--step", "1", "--run", "true")]
        quoted = self.task(evidence=run, log=["steer: the report said vacuous checks: ac 1"])
        self.assertNotIn(quoted, self.listed())

    def test_review_a_git_timeout_is_not_remembered_as_no_claims(self):
        import subprocess
        from unittest import mock
        t = self._doc_task("Run `fm proof --days 7` to see it.\n")
        real = c._git

        def flaky(root, *a, **k):
            if "diff" in a:
                return subprocess.CompletedProcess(a, 124, "", "timed out")
            return real(root, *a, **k)
        memo = {}
        with mock.patch.object(c, "_git", side_effect=flaky):
            self.assertEqual(c.unproven(self.p, memo=memo), [])
            self.assertEqual(c.unproven_summary(self.p)["n"], 0)
        self.assertEqual(memo, {}, "a timed out diff is no verdict")
        self.assertEqual([r["id"] for r in c.unproven(self.p, memo=memo)], [t])
        self.assertTrue(memo)
        self.assertEqual(c.unproven_summary(self.p)["n"], 1, "the timed out rollup was not kept as the answer")

    def test_review_a_docs_edit_with_no_start_commit_is_reported_unknown(self):
        t = self.task("docs/guide.md", lines=[(1, "true", self.GREEN)])  # T-2525: no fm evidence writer, so no late start
        need = self.needs(t)["docs"]
        self.assertIn("unknown", need["why"])
        self.assertIn("sampo prove --docs", need["upgrade"])

    def test_review_perf_noise_reads_a_zero_median(self):
        zero = self.task(type="PERFORMANCE", lines=[(1, "fast", "fast: 0.000s → 0.000s (5 runs each)")])
        worse = self.task(type="PERFORMANCE", lines=[(1, "fast", "fast: 0.000s → 0.100s (5 runs each)")])
        got = self.listed()
        self.assertEqual(got[zero], ["perf-noise"])
        self.assertNotIn(worse, got)

    def test_review_the_cache_ignores_open_briefs_and_other_windows(self):
        from unittest import mock
        self.task(evidence=[("--step", "1", "pytest", "ok")])
        cold = self.fm_json("prove", "--rollup")
        self.assertIsInstance(cold["cold_s"], float, "the cold rollup's own time is printed")
        path = os.path.join(self.p.dir, "unproven.json")
        before = open(path, encoding="utf-8").read()
        open_id = self.fm_json("task", "new", "Open", "--type", "FIX", "--tier", "S", "--ac", "ok :: true",
                               "--step", "s")["id"]
        self.fm("task", "evidence", open_id, "--step", "1", "pytest", "ok")  # an active task saves its brief
        with mock.patch.object(c, "unproven", side_effect=AssertionError("recomputed")):
            self.assertEqual(c.unproven_summary(self.p)["n"], 1, "saving an open brief keeps the cache")
        self.assertEqual(self.fm_json("prove", "--rollup", "--days", "7")["count"], 1)
        self.assertEqual(open(path, encoding="utf-8").read(), before, "another window never overwrites the cache")
        self.task(evidence=[("--step", "1", "pytest", "ok")])  # a done brief does
        self.assertEqual(c.unproven_summary(self.p)["n"], 2)


class Meaning(ForemanTestCase):  # T-2393 (Mission 7, #6): each clause of the request bound to a criterion that ran green
    RAW = ("Add a CSV export button to the report page.\nAlso email the weekly digest to every admin on Mondays.\n"
           "DONE-WHEN: the report page offers a CSV export")

    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)

    def new(self, ac="the report page offers a CSV export button :: true"):
        out = self.fm("task", "new", "CSV export", "--type", "FEATURE", "--tier", "S", "--raw", self.RAW,
                      "--ac", ac, "--step", "s", "--focus", "--json").stdout
        return json.JSONDecoder().raw_decode(out)[0]["id"]

    def test_meaning_unproven_a_clause_no_criterion_checks_is_listed_at_the_close(self):
        tid = self.new()
        r = self.fm("task", "finish", tid, "--audit", "self", "--run", "true")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)  # a label, never a block
        self.assertIn("meaning unproven", r.stdout)
        self.assertIn("email the weekly digest", r.stdout)
        self.assertNotIn("meaning unproven: Add a CSV export", r.stdout)  # bound to the criterion that ran green
        from sampo import recall
        got = {cl[:20]: (n, why) for cl, n, why in recall.bind(c.find_brief(self.p, tid))}
        self.assertEqual(got["Add a CSV export but"], (1, ""))
        self.assertEqual(got["DONE-WHEN: the repor"][0], 1)  # the user's own done-when line, checked by the same criterion

    def test_an_s_fix_close_asks_for_nothing_it_does_not_require(self):  # T-4015: the demo relayed two nags
        title = "calc.add(2, 3) returns -1 instead of 5"
        out = self.fm("task", "new", title, "--type", "FIX", "--tier", "S", "--raw", f"FIX: {title}",
                      "--ac", "add(2, 3) is 5 :: true", "--step", "s", "--focus", "--json").stdout
        tid = json.JSONDecoder().raw_decode(out)[0]["id"]
        self.fm("task", "set", tid, "--section", "Regression test", "--text", "none: a fixture")
        r = self.fm("task", "finish", tid, "--audit", "self", "--run", "true")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("what would have caught", r.stdout)  # only an M/L fix needs --why-not-caught
        self.assertNotIn("meaning unproven", r.stdout)  # the request only restates the title: the summary, not an ask

    def test_meaning_kind_lists_the_unbound_clause_in_fm_proof(self):
        tid = self.new()
        self.fm("task", "finish", tid, "--audit", "self", "--run", "true")
        rows = {x["id"]: x for x in self.fm_json("prove", "--rollup")["tasks"]}
        self.assertIn("meaning", rows[tid]["kinds"])
        need = next(n for r in c.unproven(self.p) if r["id"] == tid for n in r["needs"] if n["kind"] == "meaning")
        self.assertIn("email the weekly digest", need["why"])

    def test_meaning_rollup_splits_the_user_s_words_from_sampo_s_own_notes_and_the_two_add_up(self):  # T-2914
        mine = self.new()
        out = self.fm("task", "new", "CSV export", "--type", "FEATURE", "--tier", "S", "--raw", self.RAW, "--source",
                      "discovered", "--ac", "the report page offers a CSV export button :: true", "--step", "s",
                      "--json").stdout
        theirs = json.JSONDecoder().raw_decode(out)[0]["id"]
        for t in (mine, theirs):
            self.fm("focus", t)
            self.fm("task", "finish", t, "--audit", "self", "--run", "true")
        counts = c.unproven_summary(self.p)["counts"]
        self.assertEqual((counts.get("meaning-user"), counts.get("meaning-notes")), (1, 1))
        self.assertNotIn("meaning", counts)
        self.assertIn("meaning-user 1", c.unproven_line(2, counts))

    def test_meaning_hollow_a_clause_bound_only_to_a_vacuous_check_counts_as_unbound(self):
        from sampo import recall
        tid = self.new()
        self.fm("task", "finish", tid, "--audit", "self", "--run", "true")
        with c.lock(self.p.dir):
            b = c.find_brief(self.p, tid)
            b.append_log("vacuous checks: ac 1")  # what fm task prove --vacuous writes when ac 1 passed on the start tree
            c.save_brief(self.p, b)
        why = dict((cl[:20], w) for cl, n, w in recall.bind(c.find_brief(self.p, tid)))
        self.assertIn("start tree", why["Add a CSV export but"])
