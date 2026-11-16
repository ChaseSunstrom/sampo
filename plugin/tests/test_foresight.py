"""T-1810: session start names problems nobody raised (CI red on main, a changed model, window or CC version, a stale
blocked pile, overdue tidy), each with its action, from caches the Stop hook's detached refresh writes."""
import contextlib
import datetime
import io
import json
import os
import time
import unittest
from unittest import mock

from helpers import ForemanTestCase, git_repo
from sampo import core as c
from sampo import foresight as fs


class Foresight(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.p = c.find_project(self.repo, create=True)
        old = os.environ.get("XDG_CONFIG_HOME")  # the host's real systemd timers never reach these lines (T-2569)
        os.environ["XDG_CONFIG_HOME"] = os.path.join(self.tmp, "xdgconfig")
        self.addCleanup(lambda: os.environ.pop("XDG_CONFIG_HOME") if old is None else os.environ.update(XDG_CONFIG_HOME=old))

    def _snap(self, model, size, version):
        path = os.path.join(c.state_dir(), "sessions", f"s{time.time_ns()}.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump({"model": model, "context_size": size, "version": version}, f)

    def test_a_blind_tick_is_named_at_session_start_only_while_a_timer_runs_it(self):  # T-2607
        from sampo import foresight
        from sampo import night
        from sampo import serve
        with open(os.path.join(self.p.dir, night.BLIND), "w") as f:
            json.dump({"blind": [["CI", "no watch yes"]], "changed": time.time()}, f)
        self.assertFalse([x for x in foresight.lines(self.p) if "Tick can't watch" in x], "no timer, no line")
        os.makedirs(serve.unit_dir(), exist_ok=True)
        open(os.path.join(serve.unit_dir(), "foreman-tick-x.timer"), "w").close()
        self.assertTrue([x for x in foresight.lines(self.p) if "Tick can't watch CI" in x])

    def test_a_changed_model_window_or_cc_is_named_and_the_cost_math_reruns(self):
        self._snap("Opus 5", 200_000, "2.1.305")
        self.assertFalse(fs.refresh(self.p, net=False))  # the first setup seen is the baseline, not a change
        self.assertEqual(fs.lines(self.p), [])
        self._snap("Opus 5.5", 1_000_000, "2.1.312")
        self.assertTrue(fs.refresh(self.p, net=False))  # True: the burn scan reruns now
        text = "\n".join(fs.lines(self.p))
        for words in ("Opus 5 → Opus 5.5", "window 200k → 1M", "CC 2.1.305 → 2.1.312", "fm cost --by-model"):
            self.assertIn(words, text)
        self.assertFalse(fs.refresh(self.p, net=False))  # the same setup again: no second change

    def test_ci_red_on_main_is_named_and_captured_once_per_sha(self):
        with mock.patch.object(fs, "github_repo", return_value="o/r"), \
                mock.patch.object(fs, "ci_main", return_value=("abc1234def", ["test"])) as fetch:
            fs.refresh(self.p)
            fs.refresh(self.p)  # within CI_EVERY: no second fetch
            self.assertEqual(fetch.call_count, 1)
            with mock.patch.object(fs, "CI_EVERY", 0):
                fs.refresh(self.p)  # fetched again, same red sha: no second task
        caught = [b for b in c.load_briefs(self.p) if "CI on main is red" in b.title]
        self.assertEqual(len(caught), 1)
        self.assertEqual(caught[0].type, "FIX")
        line = "\n".join(fs.lines(self.p))
        self.assertIn("CI on main is red at abc1234 (test)", line)
        self.assertIn(caught[0].id, line)
        caught[0].meta["status"] = "dropped"  # its fix went into another task, not yet green on CI
        c.save_brief(self.p, caught[0])
        line = "\n".join(fs.lines(self.p))
        self.assertNotIn("fix it first", line)  # it pointed at a dropped task
        self.assertIn(f"{caught[0].id} is dropped", line)

    def test_green_ci_or_no_network_says_nothing(self):
        with mock.patch.object(fs, "github_repo", return_value="o/r"), \
                mock.patch.object(fs, "ci_main", return_value=("abc1234def", [])):
            fs.refresh(self.p)
        self.assertEqual(fs.lines(self.p), [])
        with mock.patch.dict(os.environ, {"FOREMAN_NO_NET": "1"}), mock.patch.object(fs, "ci_main") as fetch:
            fs.refresh(self.p)
            fetch.assert_not_called()

    def test_a_failed_fetch_keeps_what_was_known_and_a_failed_capture_retries_without_a_call(self):
        with mock.patch.object(fs, "github_repo", return_value="o/r"), \
                mock.patch("sampo.eco.capture_once", side_effect=RuntimeError("locked")), \
                mock.patch.object(fs, "ci_main", return_value=(SHA, ["test"])) as fetch:
            with self.assertRaises(RuntimeError):
                fs.refresh(self.p)
        self.assertEqual(fetch.call_count, 1)
        with mock.patch.object(fs, "github_repo", return_value="o/r"), \
                mock.patch.object(fs, "ci_main") as fetch:
            fs.refresh(self.p)  # the repo's result was stored before the capture: no second call, the capture retried
            fetch.assert_not_called()
            self.assertIn("red at aaaaaaa", "\n".join(fs.lines(self.p)))
            with mock.patch.object(fs, "CI_EVERY", 0):
                fetch.return_value = None  # rate limited or offline
                fs.refresh(self.p)
        self.assertIn("red at aaaaaaa", "\n".join(fs.lines(self.p)))  # the known red line stays

    def test_clones_of_one_repo_share_one_call_and_green_clears_the_line(self):
        other = c.find_project(git_repo(self.tmp, "clone"), create=True)
        with mock.patch.object(fs, "github_repo", return_value="o/r"), \
                mock.patch.object(fs, "ci_main", return_value=(SHA, ["test"])) as fetch:
            fs.refresh(self.p)
            fs.refresh(other)
            self.assertEqual(fetch.call_count, 1)
            fetch.return_value = (SHA, [])
            with mock.patch.object(fs, "CI_EVERY", 0):
                fs.refresh(self.p)
        self.assertEqual(fs.lines(self.p), [])

    def test_session_start_names_the_cached_problems_and_a_bad_cache_costs_only_its_line(self):
        from sampo import hooks
        with mock.patch.object(fs, "github_repo", return_value="o/r"), \
                mock.patch.object(fs, "ci_main", return_value=(SHA, ["test"])):
            fs.refresh(self.p)
        sd = c.state_dict(self.p, c.load_briefs(self.p))
        self.assertIn("CI on main is red at aaaaaaa", hooks.session_context(self.p, sd))
        sd["tidy_overdue_days"], sd["last_tidy"] = 12, None
        with mock.patch.object(fs, "lines", side_effect=KeyError("sha")):
            self.assertIn("Watch: tidy overdue 12 days → fm tidy.", hooks.session_context(self.p, sd))

    def test_every_projects_stop_hook_spawns_the_sweep_at_most_every_sweep_every(self):
        stamp = os.path.join(self.p.dir, "sweep.spawned")
        self.hook("Stop", {"stop_hook_active": False, "last_assistant_message": "Done."})
        self.assertTrue(os.path.exists(stamp))  # no cost cap or contract needed
        from sampo import cost
        with mock.patch("subprocess.Popen") as spawn, mock.patch.dict(os.environ, {"FOREMAN_NO_BACKGROUND": ""}):
            cost.sweep_detached(self.p)
            spawn.assert_not_called()  # stamped just now
            os.utime(stamp, (0, 0))
            cost.sweep_detached(self.p)
            self.assertEqual(spawn.call_args[0][0][-2:], ["cost", "--sweep"])

    def test_a_setup_change_reruns_the_burn_scan_at_once(self):
        from sampo import cost
        cost.write_burn([])
        with mock.patch.object(fs, "refresh", return_value=True), mock.patch.object(cost, "burn_findings",
                                                                                    return_value=[]) as scan:
            cost.sweep(self.p)
            scan.assert_called_once()
        with mock.patch.object(fs, "refresh", return_value=False), mock.patch.object(cost, "burn_findings") as scan:
            cost.sweep(self.p)
            scan.assert_not_called()  # burn.json is fresh: the account-wide scan waits its 10 min

    def test_github_repo_reads_origin(self):
        for url, want in (("git@github.com:ChaseSunstrom/sampo.git", "ChaseSunstrom/sampo"),
                          ("https://github.com/o/r", "o/r"), ("https://gitlab.com/o/r.git", None)):
            with mock.patch.object(c, "run_command", return_value=(0, url + "\n")):
                self.assertEqual(fs.github_repo("/x"), want)

    def test_a_stale_blocked_pile_and_overdue_tidy_carry_their_action(self):
        sd = {"blocked": [{"id": "T-0001", "updated": "2026-01-01T00:00:00Z"}, {"id": "T-0002", "updated": None}],
              "tidy_overdue_days": 12, "last_tidy": "2026-10-15T14:04:19Z"}
        text = "\n".join(fs.pile_lines(sd))
        self.assertIn("oldest untouched", text)
        self.assertIn("T-0001", text)
        self.assertIn("fm tidy", text)
        self.assertIn("tidy overdue", text)
        self.assertEqual(fs.pile_lines({"blocked": [], "tidy_overdue_days": None, "last_tidy": None}), [])

    def test_the_pile_names_the_one_action_that_frees_most(self):  # T-2683
        b = lambda i, **k: dict({"id": i, "updated": None}, **k)
        sd = {"blocked": [b("T-1"), b("T-2"), b("T-3"), b("T-4", allow=["push"]), b("T-5", waits_on=["T-9"])],
              "asks": [{"task": "T-1", "allow": ["publish"]}, {"task": "T-2", "allow": ["publish"]},
                       {"task": "T-3", "allow": ["core"]}]}
        line = fs.pile_lines(sd)[0]
        self.assertIn("5 blocked, 2 of them wait on your yes to publish: fm ask list", line)
        self.assertLess(line.index("fm ask list"), line.index("fm tidy"))
        sd["asks"] = sd["asks"][:1]  # no group above one task: nothing new
        self.assertNotIn("of them", fs.pile_lines(sd)[0])
        sd["blocked"] = [b("T-6", waits_on=["T-9"]), b("T-7", waits_on=["T-9"]), b("T-8")]
        self.assertIn("2 of them wait on T-9 closing", fs.pile_lines(sd)[0])
        sd["blocked"] = [b("T-6", freed=True), b("T-7", freed=True), b("T-8", waits_on=["T-9"])]
        self.assertIn("2 of them wait on tasks already closed: fm task unblock T-6,T-7 →", fs.pile_lines(sd)[0])
        sd["blocked"] = [b(f"T-{i}", freed=True) for i in range(1, 9)]  # T-2738: a runnable command, the rest named after
        self.assertIn("fm task unblock T-1,T-2,T-3,T-4,T-5,T-6 (2 more: fm state)", fs.pile_lines(sd)[0])

    def test_a_block_reason_naming_a_task_counts_as_its_wait(self):  # T-2683: real piles name waits in prose
        def b(tid, status, why=""):
            x = c.Brief.new(tid, f"task {tid}", "FIX", "S", now="2026-01-01T00:00:00Z")
            x.meta["status"] = status
            if why:
                x.append_log(f"blocked: {why}")
            return x
        briefs = [b("T-0001", "planned"), b("T-0002", "done"), b("T-0003", "blocked", "waits for T-0001's lane"),
                  b("T-0004", "blocked", "waits on T-0002 to land"), b("T-0005", "blocked", "depends on T-0002"),
                  b("T-0006", "blocked", "needs the user's login"), b("T-0007", "blocked", "waits for T-0999")]
        got = {x["id"]: (x["waits_on"], x["freed"]) for x in c.state_dict(self.p, briefs)["blocked"]}
        self.assertEqual(got, {"T-0003": (["T-0001"], False), "T-0004": ([], True), "T-0005": ([], True),
                               "T-0006": ([], False), "T-0007": ([], False)})  # an unknown id frees nothing


SHA = "a" * 40


def _api(runs):
    body = io.BytesIO(json.dumps({"workflow_runs": runs}).encode())
    return mock.patch("urllib.request.urlopen", return_value=contextlib.nullcontext(body))


def _run(name, conclusion, sha=SHA, status="completed"):
    return {"name": name, "status": status, "conclusion": conclusion, "head_sha": sha}


class PairedLead(unittest.TestCase):  # T-3069: a lead over the naive guess is a paired sign test, not a few rows

    @staticmethod
    def rows(w, l, both, n=376):
        mine = [True] * w + [False] * l + [True] * both
        base = [False] * w + [True] * l + [True] * both
        return mine + [False] * (n - len(mine)), base + [False] * (n - len(base))

    def test_259_against_256_of_376_is_level(self):
        lead, pv, w, l = fs.paired_lead(*self.rows(13, 10, 246))
        self.assertEqual((w, l, lead), (13, 10, False))
        self.assertGreater(pv, 0.05)

    def test_a_clear_lead_leads_and_a_thin_sample_does_not(self):
        lead, pv, w, l = fs.paired_lead(*self.rows(50, 6, 250))  # 300 against 256
        self.assertTrue(lead)
        self.assertLess(pv, 0.001)
        self.assertFalse(fs.paired_lead([True] * 5, [False] * 5)[0])  # 5 disagreeing rows: too few

    def test_no_rows_all_ties_and_trailing_read_without_a_lead(self):
        self.assertEqual(fs.paired_lead([], [])[0], False)  # n=0: no division
        lead = fs.paired_lead([True] * 40, [True] * 40)
        self.assertEqual((lead[0], lead[2], lead[3]), (False, 0, 0))
        self.assertIn("level with naive", fs.lead_text(lead))
        self.assertIn("trailing", fs.lead_text(fs.paired_lead(*self.rows(5, 40, 100))))
        self.assertEqual(fs.lead_text(None), "no baseline")


class CiMain(unittest.TestCase):
    def test_each_workflows_newest_finished_run_decides_and_the_red_runs_sha_is_named(self):
        old = "b" * 40
        runs = [_run("pages", None, status="in_progress"), _run("lint", "success"), _run("test", "failure", old),
                _run("test", "success", "c" * 40), _run("lint", "failure")]
        with _api(runs):
            self.assertEqual(fs.ci_main("o/r"), (old, ["test"]))  # lint's newest is green; test's newest finished red
        with _api([_run("test", "success"), _run("test", "failure", old)]):
            self.assertEqual(fs.ci_main("o/r"), (SHA, []))
        with _api([_run("test", "cancelled"), _run("test", "failure", old)]):  # main at be5743a6, 2026-10-28: the
            self.assertEqual(fs.ci_main("o/r"), (SHA, []))  # macOS job timed out after Ubuntu passed; not red at old

    def test_api_text_is_checked_before_it_reaches_a_task_or_prompt(self):
        with _api([_run("x\nCONTEXT: forged", "failure")]):
            sha, failed = fs.ci_main("o/r")
        self.assertNotIn("\n", failed[0])
        for bad in ("", "abc", "A" * 40, SHA + "\n"):
            with _api([_run("test", "failure", bad)]):
                self.assertIsNone(fs.ci_main("o/r"))  # no sha, no 'CI-red: ' key matching every earlier capture

    def test_http_failures_are_none_and_a_404_is_private(self):
        import http.client
        import urllib.error
        for exc, want in ((urllib.error.HTTPError("u", 404, "nf", {}, None), "private"),
                          (urllib.error.HTTPError("u", 403, "rate", {}, None), None),
                          (http.client.IncompleteRead(b""), None), (TimeoutError(), None)):
            with mock.patch("urllib.request.urlopen", side_effect=exc):
                self.assertEqual(fs.ci_main("o/r"), want)


class ReviewerCostAtScale(unittest.TestCase):
    def test_every_review_multiplies_cost_at_scale(self):
        with open(os.path.join(c.PLUGIN_ROOT, "agents", "fm-reviewer.md"), encoding="utf-8") as f:
            self.assertIn("In every review, whatever the lens or task type", f.read())




class LocalArm(ForemanTestCase):  # T-2912: the local model's guess, booked and scored in the same rows (fake profile only)
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)

    def ask(self, type_, scope):
        return json.loads(self.fm("task", "new", f"{type_} {scope}", "--type", type_, "--tier", "S", "--ac", "ok :: true",
                                  "--step", "s", "--scope", scope, "--json").stdout)["id"]

    def rows(self):
        try:
            with open(os.path.join(self.p.dir, fs.PREDICTIONS), encoding="utf-8") as f:
                return [json.loads(x) for x in f if x.strip()]
        except OSError:
            return []

    def local(self, reply, sensitive=False):
        from sampo import models, moonscale
        asked = []
        prof = {"name": "fake", "base_url": "http://127.0.0.1:1", "model": "m"}
        self.enterContext(mock.patch.object(moonscale, "_local", lambda s: asked.append(s) or prof))
        self.enterContext(mock.patch.object(models, "complete", lambda pr, m, **kw: {"text": reply}))
        return asked

    def five(self):
        for s in ("a.py", "b.py"):
            open(os.path.join(self.repo, s), "w").close()
        for t, s in (("FIX", "a.py"), ("FEATURE", "b.py"), ("FIX", "a.py"), ("FEATURE", "b.py"), ("FIX", "a.py")):
            self.ask(t, s)
        fs.prediction_tick(self.p)

    def test_the_guess_is_booked_beside_the_transition_row_scored_and_listed_as_its_own_kind(self):
        self.five()
        self.local('```json\n{"type": "FIX", "paths": ["b.py", "../x.py", "ghost.py"], "words": ["Totals", "round"]}\n```')
        fs.local_tick(self.p)
        fs.local_tick(self.p)  # once per prediction
        (pred,) = [r for r in self.rows() if r["kind"] == "next-request"]
        (loc,) = [r for r in self.rows() if r["kind"] == "local"]
        self.assertEqual(loc["id"], pred["id"])
        self.assertEqual(loc["pred"], {"type": "FIX", "paths": ["b.py"], "words": ["totals", "round"]})
        self.ask("FEATURE", "b.py")
        fs.prediction_tick(self.p)
        (s,) = [r for r in self.rows() if r["kind"] == "score"]
        self.assertEqual((s["type_hit"], s["local_type_hit"], s["local_overlap"]), (True, False, 1.0))
        self.assertEqual(s["local_word_overlap"], 0.0)
        k = {k["kind"]: k for k in fs.prediction_kinds(self.p)}["next-request-local"]
        self.assertEqual((k["n"], k["hit"], k["base_hit"]), (1, 0.0, 1.0))  # beside the transition guess

    def test_a_malformed_or_unknown_reply_books_no_guess_and_a_sensitive_project_asks_loopback_only(self):
        self.five()
        for bad in ("no json", '{"type": "WISH", "paths": []}', '["FIX"]'):
            self.local(bad)
            fs.local_tick(self.p)
        locs = [r for r in self.rows() if r["kind"] == "local"]
        self.assertEqual([r["pred"] for r in locs], [None])  # one abstain row: asked once, never retried
        reqs = fs._requests(self.p)
        asked = self.local('{"type": "FIX", "paths": [], "words": []}')
        self.fm("sensitive", "on")
        self.assertEqual(fs.local_predict(self.p, reqs, [], [])["type"], "FIX")
        self.assertEqual(asked, [True])  # moonscale._local(sensitive=True): loopback profiles only
        fs.local_tick(self.p)  # a sensitive project books nothing
        self.assertEqual(len([r for r in self.rows() if r["kind"] == "local"]), 1)

    def test_session_start_reads_the_local_arm_only_when_it_leads_over_twenty_scored_rows(self):
        self.five()
        self.local('{"type": "FIX", "paths": ["a.py"], "words": []}')  # the transition guess says FEATURE here
        fs.local_tick(self.p)

        def score(n):
            with open(os.path.join(self.p.dir, fs.PREDICTIONS), "a", encoding="utf-8") as f:
                for i in range(n):
                    f.write(json.dumps({"kind": "score", "id": f"Q{i}", "at": c.now(), "type_hit": False,
                                        "base_type_hit": False, "local_type_hit": True}) + "\n")
        score(19)
        self.assertEqual(fs.lines(self.p), [])  # 19 rows: too few to lead
        score(1)
        (line,) = fs.lines(self.p)
        self.assertIn("FIX on a.py", line)
        self.assertIn("100% vs 0%", line)
        # T-2950: the shown row names the local arm, and its hit is scored by that arm, not the transition guess
        (row,) = [r for r in self.rows() if r["kind"] == "shown"]
        self.assertEqual(row["arm"], fs.LOCAL_KIND)
        with open(os.path.join(self.p.dir, fs.PREDICTIONS), "a", encoding="utf-8") as f:
            f.write(json.dumps({"kind": "score", "id": row["id"], "at": c.now(), "type_hit": False,
                                "base_type_hit": False, "local_type_hit": True}) + "\n")
        k = next(k for k in fs.prediction_kinds(self.p) if k["kind"] == "next-request")
        self.assertEqual((k["shown_n"], k["shown_hit"]), (1, 1.0))


class DraftAhead(ForemanTestCase):  # T-2407 (#3): the top prediction planned ahead while its kind beats the baseline
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)

    def ask(self, title, type_, scope):
        return json.loads(self.fm("task", "new", title, "--type", type_, "--tier", "S", "--ac", "ok :: true", "--step",
                                  "s", "--scope", scope, "--approach", "1) edit the export; fails if: x 2) use what exists: "
                                  "y; fails if: z 3) do less: w; fails if: v Choice 1 (80%): edit it", "--json").stdout)["id"]

    def book(self, n, beats=True):
        with open(os.path.join(self.p.dir, fs.PREDICTIONS), "a", encoding="utf-8") as f:
            for i in range(n):
                f.write(json.dumps({"kind": "score", "id": f"Q{i}", "for": "T-9", "at": c.now(), "type_hit": beats,
                                    "base_type_hit": not beats, "overlap": None, "base_overlap": None}) + "\n")

    def ahead(self):
        from sampo import draft as sdraft
        return sdraft.ahead(c.find_project(self.repo))

    def draft(self):
        from sampo import draft as sdraft
        try:
            with open(os.path.join(self.p.dir, sdraft.AHEAD), encoding="utf-8") as f:
                return json.load(f)
        except OSError:
            return None

    def setup_requests(self):
        self.ask("Add a CSV export to the report", "FEATURE", "report.py")
        self.ask("Fix the totals in the report", "FIX", "totals.py")
        self.ask("Add a PDF export to the report", "FEATURE", "report.py")
        self.ask("Fix rounding in the totals", "FIX", "totals.py")
        fs.prediction_tick(self.p)  # next after a FIX: a FEATURE on report.py

    def test_a_plan_only_draft_is_made_while_the_kind_beats_its_baseline_and_never_when_muted(self):
        import subprocess
        self.setup_requests()
        self.assertIsNone(self.ahead())  # no scored rows yet: nothing beats anything
        self.book(12)
        table = os.path.join(self.p.dir, "trigger-table.json")
        with open(table, "w", encoding="utf-8") as f:
            json.dump({"draft-ahead": {"route": "muted"}}, f)
        self.assertIsNone(self.ahead())  # the user muted its trigger route
        os.remove(table)
        d = self.ahead()
        self.assertTrue(d and os.path.isdir(d["wt"]), d)
        plan = open(os.path.join(d["wt"], "DRAFTED-AHEAD.md"), encoding="utf-8").read()
        self.assertIn("Speculative", plan)
        self.assertIn("edit the export", plan)  # the plan of this user's closest past request of that type
        changed = subprocess.run(["git", "-C", d["wt"], "diff", "--name-only", "HEAD~1", "HEAD"], capture_output=True,
                                 text=True).stdout.split()
        self.assertEqual(changed, ["DRAFTED-AHEAD.md"])  # a plan, no code
        self.assertEqual(self.ahead(), None)  # one draft per prediction
        from sampo import draft as sdraft
        sdraft._drop_ahead(self.p)
        self.book(20, beats=False)  # now it trails: muted, so nothing is drafted
        self.assertIsNone(self.ahead())

    def test_an_unreadable_private_term_list_stops_the_plan_before_any_worktree(self):
        # T-4817: the plan is committed to a branch that may be pushed, so it is redacted for the user's own terms
        # (strict); a damaged list raises the fixed line (the night tick's note), makes no worktree, files no draft
        import subprocess
        self.setup_requests()
        self.book(12)
        with open(os.path.join(c.state_dir(), c.PRIVATE_TERMS), "w", encoding="utf-8") as f:
            f.write("")  # a truncated list
        with self.assertRaises(c.PrivateTermsUnreadable) as e:
            self.ahead()
        self.assertEqual(str(e.exception), c.PRIVATE_UNREADABLE)
        self.assertIsNone(self.draft())
        self.assertEqual(subprocess.run(["git", "-C", self.repo, "branch", "--list", "draft/*"], capture_output=True,
                                        text=True).stdout.strip(), "")
        os.remove(os.path.join(c.state_dir(), c.PRIVATE_TERMS))
        self.assertTrue(self.ahead(), "repaired: the plan is made")

    def test_a_draft_older_than_three_days_is_removed_and_scored_as_expired(self):
        self.setup_requests()
        self.book(12)
        d = self.ahead()
        from sampo import draft as sdraft
        path = os.path.join(self.p.dir, sdraft.AHEAD)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(dict(d, at=c.iso(time.time() - 4 * 86400)), f)
        self.ahead()
        self.assertFalse(os.path.isdir(d["wt"]))
        rows = [json.loads(x) for x in open(os.path.join(self.p.dir, fs.PREDICTIONS), encoding="utf-8")]
        self.assertTrue(any(r.get("kind") == "draft" and r.get("expired") for r in rows))

    def test_a_request_overlapping_the_draft_gets_its_plan_and_one_that_does_not_leaves_it(self):
        self.setup_requests()
        self.book(12)
        d = self.ahead()
        other = self.ask("Rename the login page title", "FEATURE", "login.py")
        self.assertNotIn("Drafted ahead", c.find_brief(self.p, other).section("Drafted ahead") or "")
        self.assertTrue(os.path.isdir(d["wt"]))  # under 20%: left alone
        self.fm("capture", "Add an Excel export to the report", "--source", "discovered", "--anyway")
        self.assertTrue(os.path.isdir(d["wt"]))  # Foreman's own capture never adopts a draft of the user's next ask
        tid = self.ask("Add an Excel export to the report", "FEATURE", "report.py")
        b = c.find_brief(c.find_project(self.repo), tid)
        self.assertIn("Speculative", b.section("Drafted ahead"))
        rows = [json.loads(x) for x in open(os.path.join(self.p.dir, fs.PREDICTIONS), encoding="utf-8")]
        self.assertTrue(any(r.get("kind") == "draft" and r.get("adopted") and r.get("for") == tid for r in rows))
        kinds = {k["kind"]: k for k in self.fm_json("outcomes", "--predictions")["kinds"]}
        self.assertEqual(kinds["draft-ahead"]["n"], 1)


CLOCK = "import datetime\nOK = datetime.date.today() < datetime.date(2100, 1, 1)\n"  # T-4680: a date counts when its file compares the clock


class BreakageWarnings(ForemanTestCase):  # T-2408 (#3): dates about to pass, stale co-change partners, state growth
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)
        self.today = datetime.date.today()

    def commit(self, files, msg):
        import subprocess
        for name, text in files.items():
            with open(os.path.join(self.repo, name), "w", encoding="utf-8") as f:
                f.write(text)
        subprocess.run(["git", "-C", self.repo, "add", "-A"], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", msg], check=True)

    def tick(self, days=0, budget=10 ** 12):
        return fs.breakage_tick(c.find_project(self.repo), today=self.today + datetime.timedelta(days=days),
                                budget=budget)

    def rows(self, kind):
        try:
            with open(os.path.join(self.p.dir, fs.PREDICTIONS), encoding="utf-8") as f:
                return [r for r in map(json.loads, f) if r.get("kind") == kind]
        except OSError:
            return []

    def day(self, n):
        return (self.today + datetime.timedelta(days=n)).isoformat()

    def test_a_clean_repo_files_no_warning(self):
        self.commit({"app.py": "x = 1\n", "notes.md": f"due {self.day(3)}\n"}, "app")  # prose dates don't count
        self.tick()
        self.commit({"app.py": "x = 2\n"}, "edit")
        self.tick(1)
        self.assertEqual(self.rows("warning"), [])
        self.assertFalse([b for b in c.load_briefs(c.find_project(self.repo)) if str(b.meta.get("trigger", "")).startswith("foresight")])

    def test_a_date_passing_within_14_days_is_filed_through_the_trigger_table_and_a_far_one_is_not(self):
        self.commit({"lic.py": f"EXPIRES = \"{self.day(5)}\"\n{CLOCK}", "far.py": f"LATER = \"{self.day(40)}\"\n{CLOCK}"}, "dates")
        self.tick()
        w = self.rows("warning")
        self.assertEqual([(x["wkind"], x["target"], x["line"]) for x in w], [("date", "lic.py", 1)])
        self.assertEqual(w[0]["until"][:10], self.day(5))
        b = c.find_brief(c.find_project(self.repo), w[0]["task"])
        self.assertEqual(b.meta.get("trigger"), "foresight-date")
        self.tick()  # once per day, and a warning is filed once
        self.assertEqual(len(self.rows("warning")), 1)

    def test_a_commit_without_its_usual_co_change_partner_is_warned_and_one_with_it_is_not(self):
        self.tick()
        api = lambda i: f"def fetch(x):\n    return x + {i}\n"  # noqa: E731  # T-4680: the partner calls what the commit changes
        client = lambda i: f"from api import fetch\n\ndef go(x):\n    return fetch(x)  # {i}\n"  # noqa: E731
        for i in range(fs.PAIR_MIN - 1):
            self.commit({"api.py": api(i), "api_client.py": client(i)}, f"pair {i}")
        self.commit({"api.py": api(9), "api_client.py": client(9)}, "both")
        self.tick(1)
        self.assertEqual(self.rows("warning"), [])
        self.commit({"api.py": api(10)}, "alone")
        self.tick(2)
        self.assertEqual([(x["wkind"], x["target"]) for x in self.rows("warning")], [("cochange", "api_client.py")])

    def test_state_growing_toward_its_budget_within_30_days_is_warned(self):
        with open(os.path.join(self.p.dir, fs.WARN_STATE), "w", encoding="utf-8") as f:
            json.dump({"sizes": {self.day(-10): 1000, self.day(-5): 50_000}}, f)
        size = fs._state_bytes(c.find_project(self.repo))
        self.tick(budget=size * 100)  # flat-ish: crossing far beyond 30 days
        self.assertEqual(self.rows("warning"), [])
        with open(os.path.join(self.p.dir, fs.WARN_STATE), "w", encoding="utf-8") as f:
            json.dump({"sizes": {self.day(-10): size // 10, self.day(-5): size // 2}}, f)
        self.tick(1, budget=size * 3)  # about 0.08x a day: the 3x budget in about 24 days
        self.assertEqual([x["wkind"] for x in self.rows("warning")], ["state"])

    def test_warnings_are_scored_hit_acted_on_or_missed_by_their_window(self):
        self.commit({"a.py": f"D = \"{self.day(5)}\"\n{CLOCK}", "b.py": f"D = \"{self.day(6)}\"\n{CLOCK}",
                     "c.py": f"D = \"{self.day(7)}\"\n{CLOCK}"}, "dates")
        self.tick()
        w = {x["target"]: x for x in self.rows("warning")}
        self.commit({"a.py": f"D = None\n{CLOCK}"}, "T-0999: unrelated cleanup drops the date")  # another task: a hit
        self.commit({"b.py": f"D = None\n{CLOCK}"}, f"{w['b.py']['task']}: fix from the warning")  # its own fix: acted on
        self.tick(3)
        scores = {x["id"]: x for x in self.rows("warning-score")}
        self.assertTrue(scores[w["a.py"]["id"]]["hit"])
        self.assertNotIn(w["c.py"]["id"], scores)  # its window is still open
        self.tick(15)
        scores = {x["id"]: x for x in self.rows("warning-score")}
        self.assertFalse(scores[w["c.py"]["id"]]["hit"])  # the window passed with neither: a miss
        self.assertTrue(scores[w["b.py"]["id"]]["acted"] and not scores[w["b.py"]["id"]]["hit"])
        kinds = {k["kind"]: k for k in self.fm_json("outcomes", "--predictions")["kinds"]}
        self.assertEqual((kinds["foresight-date"]["n"], kinds["foresight-date"]["hit"]), (2, 0.5))
        self.assertEqual(kinds["foresight-date"]["acted"], 1)


if __name__ == "__main__":
    unittest.main()


class PredictionBook(ForemanTestCase):  # T-2406 (#3): the next request predicted, scored against the naive baseline
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)

    def ask(self, type_, scope):
        return json.loads(self.fm("task", "new", f"{type_} {scope}", "--type", type_, "--tier", "S", "--ac", "ok :: true",
                                  "--step", "s", "--scope", scope, "--json").stdout)["id"]

    def rows(self):
        try:
            with open(os.path.join(self.p.dir, fs.PREDICTIONS), encoding="utf-8") as f:
                return [json.loads(x) for x in f if x.strip()]
        except OSError:
            return []

    def test_the_tick_stores_one_prediction_beside_the_baseline_and_the_next_user_request_scores_it_once(self):
        for t, s in (("FIX", "a.py"), ("FEATURE", "b.py"), ("FIX", "a.py"), ("FEATURE", "b.py"), ("FIX", "a.py")):
            self.ask(t, s)
        fs.prediction_tick(self.p)
        fs.prediction_tick(self.p)  # nothing new arrived: still one
        (pred,) = [r for r in self.rows() if r["kind"] == "next-request"]
        self.assertEqual(pred["pred"]["type"], "FEATURE")  # after a FIX this user asks for a FEATURE
        self.assertEqual(pred["base"], {"type": "FIX", "paths": ["a.py"]})  # the naive baseline: the last request again
        tid = self.ask("FEATURE", "b.py")
        fs.prediction_tick(self.p)
        (score,) = [r for r in self.rows() if r["kind"] == "score"]
        self.assertEqual((score["id"], score["for"], score["type_hit"], score["base_type_hit"]),
                         (pred["id"], tid, True, False))
        self.assertEqual(score["base_overlap"], 0.0)
        fs.prediction_tick(self.p)
        self.assertEqual(len([r for r in self.rows() if r["kind"] == "score"]), 1)  # never rescored
        self.assertEqual(len([r for r in self.rows() if r["kind"] == "next-request"]), 2)  # the next one, after tid

    def test_predicted_paths_come_from_the_newest_request_of_that_type_the_last_included_and_keep_every_path(self):
        # T-2449: the replay over this project's own 376 held-out requests had the paths trail the naive baseline
        # (0.053 vs 0.086): the last request was skipped, and a guess was cut to 5 of up to 20 paths
        for t, s in (("FIX", "a.py"), ("FEATURE", "b.py"), ("FEATURE", "c.py")):
            self.ask(t, s)
        fs.prediction_tick(self.p)
        (pred,) = [r for r in self.rows() if r["kind"] == "next-request"]
        self.assertEqual(pred["pred"], {"type": "FEATURE", "paths": ["c.py"]})  # the newest FEATURE is the last one
        many = [f"m{i}.py" for i in range(8)]
        self.assertEqual(fs.predict([(None, "FIX", ["x.py"]), (None, "FEATURE", many), (None, "FIX", ["y.py"])]),
                         ("FEATURE", many))  # every path the request touched, as _requests keeps them

    def test_session_start_names_the_unscored_prediction_once_while_it_beats_its_baseline(self):
        # T-2681: shown only while the kind beats 'same as last'; the book gains a write-once shown row
        for t, s in (("FIX", "a.py"), ("FEATURE", "b.py"), ("FIX", "a.py"), ("FEATURE", "b.py"), ("FIX", "a.py")):
            self.ask(t, s)
        fs.prediction_tick(self.p)
        (pred,) = [r for r in self.rows() if r["kind"] == "next-request"]

        def book(n, hit, base):
            with open(os.path.join(self.p.dir, fs.PREDICTIONS), "a", encoding="utf-8") as f:
                for i in range(n):
                    f.write(json.dumps({"kind": "score", "id": f"Q{i}", "at": c.now(), "type_hit": hit,
                                        "base_type_hit": base}) + "\n")
        shown = lambda: [r for r in self.rows() if r["kind"] == "shown"]  # noqa: E731
        self.assertEqual(fs.lines(self.p), [])  # no scored rows: nothing beats anything
        book(20, False, True)  # trailing and muted
        self.assertEqual(fs.lines(self.p), [])
        self.assertEqual(shown(), [])
        with open(os.path.join(self.p.dir, fs.PREDICTIONS), "w", encoding="utf-8") as f:
            f.write(json.dumps(pred) + "\n")
        book(fs.MUTE_MIN - 1, True, False)
        self.assertEqual(fs.lines(self.p), [])  # beating it on too few scores shows nothing
        book(1, True, False)
        (line,) = fs.lines(self.p)
        self.assertIn("FEATURE on b.py", line)
        self.assertIn("100% vs 0%", line)
        self.assertEqual(shown(), [{"kind": "shown", "id": pred["id"], "arm": "next-request"}])  # T-2950: its arm
        self.assertEqual(fs.lines(self.p), [])  # once per prediction
        self.assertEqual(len(shown()), 1)
        with open(os.path.join(self.p.dir, fs.PREDICTIONS), "a", encoding="utf-8") as f:  # its score, read apart
            f.write(json.dumps({"kind": "score", "id": pred["id"], "at": c.now(), "type_hit": False,
                                "base_type_hit": True}) + "\n")
        k = next(k for k in fs.prediction_kinds(self.p) if k["kind"] == "next-request")
        self.assertEqual((k["shown_n"], k["shown_hit"]), (1, 0.0))
        self.assertIn("shown at session start: 1 scored, hit 0%", self.fm("outcomes", "--predictions").stdout)

    def test_session_start_says_nothing_of_a_sensitive_projects_prediction(self):
        for t, s in (("FIX", "a.py"), ("FEATURE", "b.py"), ("FIX", "a.py")):
            self.ask(t, s)
        fs.prediction_tick(self.p)
        with open(os.path.join(self.p.dir, fs.PREDICTIONS), "a", encoding="utf-8") as f:
            f.write(json.dumps({"kind": "score", "id": "Q", "at": c.now(), "type_hit": True, "base_type_hit": False}) + "\n")
        self.fm("sensitive", "on")
        self.assertEqual(fs.lines(self.p), [])

    def test_files_touched_outside_the_repo_are_not_part_of_a_requests_paths(self):
        # T-2454: the live book's first row guessed a scratchpad file (../../../../tmp/...) the request had touched
        tid = self.ask("FIX", "a.py")
        p = c.find_project(self.repo)
        c.log_event(p, "touched", task=tid, data={"file": os.path.join(self.tmp, "elsewhere", "note.md")})
        c.log_event(p, "touched", task=tid, data={"file": os.path.join(self.repo, "b.py")})
        c.log_event(p, "touched", task=tid, data={"file": "../outside.py"})
        ((_, _, paths),) = fs._requests(p)
        self.assertEqual(paths, ["a.py", "b.py"])

    def test_predictions_report_hit_rates_beside_the_baseline_and_mute_a_kind_that_trails_it(self):
        with open(os.path.join(self.p.dir, fs.PREDICTIONS), "w", encoding="utf-8") as f:
            for i in range(20):
                f.write(json.dumps({"kind": "next-request", "id": f"P{i}", "at": c.now(), "after": "T-0001",
                                    "pred": {"type": "FIX", "paths": []}, "base": {"type": "FEATURE", "paths": []}}) + "\n")
                f.write(json.dumps({"kind": "score", "id": f"P{i}", "for": f"T-{i + 2:04d}", "at": c.now(),
                                    "type_hit": False, "base_type_hit": True, "overlap": None, "base_overlap": None}) + "\n")
        from sampo import outcomes
        for m, pm in ((30, 30), (40, 30), (200, 30)):
            outcomes.record_close(self.p, {"type": "FIX", "tier": "S", "minutes": m, "predict_minutes": pm})
        got = self.fm_json("outcomes", "--predictions")
        kinds = {k["kind"]: k for k in got["kinds"]}
        self.assertEqual((kinds["next-request"]["n"], kinds["next-request"]["hit"], kinds["next-request"]["base_hit"]),
                         (20, 0.0, 1.0))
        self.assertTrue(kinds["next-request"]["muted"])
        self.assertIn("effort", kinds)
        doc = self.fm("doctor", check=False).stdout
        self.assertIn("next-request", doc)
        self.assertIn("fm outcomes --predictions --unmute next-request", doc)
        self.fm("outcomes", "--predictions", "--unmute", "next-request")
        self.assertFalse({k["kind"]: k for k in self.fm_json("outcomes", "--predictions")["kinds"]}["next-request"]["muted"])

    def test_rows_scored_in_the_same_second_as_an_unmute_count_as_before_it(self):
        # T-2455: CI on main went red when the unmute landed in the same second as the rows (at >= since kept them fresh)
        at = c.now()
        with open(os.path.join(self.p.dir, fs.PREDICTIONS), "w", encoding="utf-8") as f:
            for i in range(fs.MUTE_MIN):
                f.write(json.dumps({"kind": "score", "id": f"P{i}", "for": f"T-{i + 2:04d}", "at": at, "type_hit": False,
                                    "base_type_hit": True, "overlap": None, "base_overlap": None}) + "\n")
        meta = c.read_meta(self.p)
        meta["predictions_unmuted"] = {"next-request": at}
        c.write_meta(self.p, meta)
        self.assertFalse({k["kind"]: k for k in fs.prediction_kinds(self.p)}["next-request"]["muted"])

    def test_a_sensitive_project_predicts_nothing(self):
        for t, s in (("FIX", "a.py"), ("FEATURE", "b.py"), ("FIX", "a.py")):
            self.ask(t, s)
        self.fm("sensitive", "on")
        fs.prediction_tick(c.find_project(self.repo))
        self.assertEqual(self.rows(), [])

    def test_the_tick_runs_the_prediction_book(self):  # T-2406: fm night --tick is where predictions are made
        for t, s in (("FIX", "a.py"), ("FEATURE", "b.py")):
            self.ask(t, s)
        self.fm("night", "--tick")
        self.assertEqual([r["kind"] for r in self.rows()], ["next-request"])


class TimerWatch(ForemanTestCase):  # T-2542: the sweep caches a dead or failing timer; session start names it
    def test_the_sweep_caches_it_and_lines_names_it(self):
        from sampo import foresight as fs
        from sampo import night
        p = c.find_project(self.repo, create=True)
        rows = [("foreman-tick-proj-abc123", "hasn't fired in 7 h")]
        with mock.patch.object(night, "liveness", return_value=rows), \
                mock.patch.object(night, "tick", return_value=None), \
                mock.patch.object(fs, "_ci", return_value=None):
            fs.refresh(p)
        text = "\n".join(fs.lines(p))
        self.assertIn("foreman-tick-proj-abc123", text)
        self.assertIn("journalctl --user -u foreman-tick-proj-abc123.service", text)
        with mock.patch.object(night, "liveness", return_value=[]), \
                mock.patch.object(night, "tick", return_value=None), \
                mock.patch.object(fs, "_ci", return_value=None):
            fs.refresh(p)
        self.assertNotIn("foreman-tick", "\n".join(fs.lines(p)), "a timer that recovered drops off")


class OwnAsks(ForemanTestCase):  # T-2554: the user model learns from the user's asks, not the tower's briefs or slips
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)

    def ask(self, title):
        return json.loads(self.fm("task", "new", title, "--type", "FIX", "--tier", "S", "--ac", "ok :: true",
                                  "--step", "s", "--json").stdout)["id"]

    def test_mission_briefs_and_mistaken_captures_are_left_out(self):
        from sampo import rune
        mine, tower, slip = self.ask("fix the parser"), self.ask("a Mission brief"), self.ask("a sample line")
        home = rune.home(self.p)
        os.makedirs(os.path.join(home, "gen-2"), exist_ok=True)
        with open(os.path.join(home, "gen-2", "mission-x.md"), "w", encoding="utf-8") as f:
            f.write(f"# Mission\n\nTask: {tower} (brief 1).\n")
        self.fm("task", "drop", slip, "a sample line fed to fm intake", "--mistake")  # T-4693: a field, not Log words
        ids = [b.id for b, _, _ in fs._requests(self.p)]
        self.assertIn(mine, ids)
        self.assertNotIn(tower, ids, "the tower's own Mission brief isn't the user's ask")
        self.assertNotIn(slip, ids, "a capture dropped as a mistake isn't the user's ask")
        self.assertEqual(len([b for b, _, _ in fs._requests(self.p, clean=False)]), 3, "clean=False keeps the old stream")
