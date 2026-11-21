"""T-2868: Moonscale part 1 — the candidate store, the write-time admit rules, the grid read thin-first, and grow routed
local-first then Haiku, budget-capped and resumable. T-2869: part 2 — the funnel (exact, embed or shingle dedupe, kill
check, tier screen and merge, novelty), families, promotion through the cold read, the export and check. Every model
call and embedding here is mocked."""
# map-links: plugin/lib/sampo/moonscale.py plugin/lib/sampo/rune.py plugin/lib/sampo/models.py plugin/lib/sampo/moons.py
import json
import os
import re
from unittest import mock

from helpers import ForemanTestCase
from test_genesis import GEN0_CAPS


def good(**kw):
    row = {"title": "Catch a flaky test before it reaches anyone",
           "today": "Today you can't tell a flaky test from a real failure until it has cost you a morning.",
           "with_this": "With this, you can ship a release knowing which red tests in your code are noise.",
           "what": "It reruns each new failure under changed timing and flags the ones that flip.",
           "kill": "Run it on 20 known flaky tests in one repo for a day; if it flags fewer than 15, it is dead.",
           "prior_art": "flaky-test dashboards", "tier": "high"}
    row.update(kw)
    return row


def reply(n, bad=0):
    """A model's batch: n moons as JSON lines inside a fence, the last `bad` of them internal-only."""
    lines = [json.dumps(good(title=f"Moon number {i}")) for i in range(n - bad)]
    lines += [json.dumps(good(title=f"Bad moon {i}", with_this="With this, you can keep the ledger and the guard in step."))
              for i in range(bad)]
    return "Here they are:\n```json\n" + "\n".join(lines) + "\n```\n"


class _Moons(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        from sampo import core as c
        from sampo import moonscale
        self.c, self.ms = c, moonscale
        self.p = c.find_project(self.repo)


class Admit(_Moons):
    def test_a_moon_with_the_line_and_a_falsifiable_kill_test_is_kept(self):
        self.assertEqual(self.ms.admit(good()), (True, ""))
        self.assertTrue(self.ms.admit(good(today="Today you can’t see it coming."))[0], "a curly apostrophe is the line")

    def test_each_rejection_names_its_reason(self):
        cases = {
            "line": good(today="You can't tell a flaky test from a real one."),
            "line ": good(with_this="Ship faster with your code."),
            "payoff": good(with_this="With this, you can keep the ledger, the guard and every lane in step."),
            "kill test": good(kill=""),
            "kill test ": good(kill="Try it on a project and see how it feels."),
            "code": good(today="Today you can't trust the parse_rows helper in loader.py."),
            "code ": good(with_this="With this, you can run your tests with --fast in your project."),
        }
        for want, row in cases.items():
            with self.subTest(want):
                ok, why = self.ms.admit(row)
                self.assertFalse(ok)
                self.assertTrue(why.startswith(want.strip() + ":"), why)

    def test_a_payoff_on_the_users_things_passes_even_with_an_internal_word(self):
        ok, why = self.ms.admit(good(with_this="With this, you can let a guard stop a bad deploy before your users see it."))
        self.assertTrue(ok, why)

    def test_product_names_and_plain_slashes_are_not_code(self):
        ok, why = self.ms.admit(good(today="Today you can't upgrade a Node.js app or a macOS build 24/7 without a pager."))
        self.assertTrue(ok, why)


class Store(_Moons):
    def cell(self, kind="bugs", scale="file", surface="terminal", mechanism="biology", problem="slow tests",
             source="pain"):
        return {"problem": problem, "source": source, "kind": kind, "mechanism": mechanism, "scale": scale,
                "surface": surface}

    def test_add_stores_every_row_and_the_funnel_keeps_each_fate(self):
        out = self.ms.add(self.p, [good(), good(kill="")], self.cell(), "haiku", 1)
        self.assertEqual([(i, v) for i, v, _ in out], [("C-000001", "kept"), ("C-000002", "rejected")])
        rows = self.ms.rows(self.p)
        self.assertEqual([r["id"] for r in rows], ["C-000001", "C-000002"])
        self.assertEqual((rows[0]["model"], rows[0]["batch"], rows[0]["cell"]["kind"]), ("haiku", 1, "bugs"))
        fate = self.ms.fate(self.p)
        self.assertEqual(fate["C-000001"]["verdict"], "kept")
        self.assertEqual((fate["C-000002"]["verdict"], fate["C-000002"]["stage"]), ("rejected", "write"))
        self.assertTrue(fate["C-000002"]["reason"].startswith("kill test:"))
        self.assertEqual(self.ms.add(self.p, [good()], self.cell(), "haiku", 2)[0][0], "C-000003")
        cov = self.ms.coverage(self.p)
        self.assertEqual((cov["total"], cov["kept"], cov["rejected"]), (3, 2, 1))
        self.assertEqual(cov["reasons"], {"kill test": 1})
        self.assertEqual(cov["cells"]["bugs/file/terminal"], 3)
        self.assertEqual((cov["mechanisms"]["biology"], cov["sources"]["pain"]), (3, 3))

    def test_pick_takes_the_thinnest_cells_problems_and_mechanisms_first(self):
        ms = self.ms
        probs = [{"text": "slow tests", "source": "pain"}, {"text": "late reviews", "source": "steer"}]
        with mock.patch.object(ms, "KINDS", ("bugs", "docs")), mock.patch.object(ms, "SCALES", ("file",)), \
                mock.patch.object(ms, "SURFACES", ("terminal",)), mock.patch.object(ms, "MECHANISMS", ("biology", "law")), \
                mock.patch.object(ms, "problems", return_value=probs), mock.patch.object(ms, "sampo_areas", return_value=[]):
            ms.add(self.p, [good()], self.cell(), "haiku", 1)
            got = ms.pick(self.p, 1, 0)[0]
            self.assertEqual((got["kind"], got["mechanism"], got["problem"], got["source"]),
                             ("docs", "law", "late reviews", "steer"))
            ms.add(self.p, [good(), good()], self.cell(kind="docs", mechanism="law", problem="late reviews"), "haiku", 2)
            self.assertEqual(ms.pick(self.p, 1, 0)[0]["kind"], "bugs")
            two = ms.pick(self.p, 2, 0)
            self.assertEqual([x["kind"] for x in two], ["bugs", "docs"])
            self.assertEqual(two[0]["mechanism"], "biology", "law holds 2 rows, biology 1")
        self.assertEqual(ms.pick(self.p, 6, 7), ms.pick(self.p, 6, 7), "a seed's draw is the same draw")
        self.assertEqual(len({(x["kind"], x["scale"], x["surface"]) for x in ms.pick(self.p, 40, 3)}), 40)
        untouched = set(ms.MECHANISMS) - {"biology", "law"}
        self.assertEqual({x["mechanism"] for x in ms.pick(self.p, len(untouched), 3)}, untouched,
                         "picks in one call spread over the mechanisms no row has used")

    def test_problems_come_from_the_records_and_the_constant_pains(self):
        with open(os.path.join(self.repo, "pyproject.toml"), "w") as f:
            f.write("[project]\nname = 'x'\n")
        with mock.patch.object(self.c, "steer_rules", return_value=[("2026-11-01", "make every reply shorter")]):
            probs = self.ms.problems(self.p)
        srcs = {x["source"] for x in probs}
        self.assertLessEqual({"pain", "steer", "project"}, srcs)
        self.assertGreaterEqual(sum(x["source"] == "pain" for x in probs), 25)
        proj = [x["text"] for x in probs if x["source"] == "project"]
        self.assertTrue(any(os.path.basename(self.repo) in t and "Python" in t for t in proj), proj)
        self.fm("sensitive", "on")
        self.assertFalse([x for x in self.ms.problems(self.p) if x["source"] == "project"
                          and os.path.basename(self.repo) in x["text"]], "a sensitive project's name stays home")


class Route(_Moons):
    PROFILE = {"name": "jarvis", "kind": "openai", "base_url": "https://127.0.0.1:9/v1", "model": "m",
               "key_env": "MOONS_TEST_KEY", "context": 8192, "tools": False}

    def ask(self, env, up=True):
        from sampo import ideas, models
        with mock.patch.dict(os.environ, {"MOONS_LOCAL": "jarvis", **env}), \
                mock.patch.object(models, "resolve", return_value=dict(self.PROFILE)), \
                mock.patch.object(self.ms, "_up", return_value=up), \
                mock.patch.object(models, "complete", return_value={"text": "local text"}) as comp, \
                mock.patch.object(ideas, "run_child", return_value="haiku text") as child:
            out = self.ms._ask(self.p, "sys", "prompt")
        return out, comp, child

    def test_the_local_model_answers_first(self):
        out, comp, child = self.ask({"MOONS_TEST_KEY": "k"})
        self.assertEqual(out, ("local text", "jarvis"))
        self.assertEqual(comp.call_args[0][0]["name"], "jarvis")
        child.assert_not_called()

    def test_haiku_when_the_key_is_unset_or_the_server_is_down(self):
        os.environ.pop("MOONS_TEST_KEY", None)
        for env, up in (({}, True), ({"MOONS_TEST_KEY": "k"}, False)):
            with self.subTest(env=env, up=up):
                out, comp, child = self.ask(env, up)
                self.assertEqual(out, ("haiku text", "haiku"))
                comp.assert_not_called()
                self.assertEqual((child.call_args[0][0], child.call_args[0][3]), ("moons-grow", "haiku"))

    def test_haiku_when_the_local_model_errs(self):
        from sampo import ideas, models
        with mock.patch.dict(os.environ, {"MOONS_TEST_KEY": "k", "MOONS_LOCAL": "jarvis"}), \
                mock.patch.object(models, "resolve", return_value=dict(self.PROFILE)), \
                mock.patch.object(self.ms, "_up", return_value=True), \
                mock.patch.object(models, "complete", side_effect=models.ModelError("down")), \
                mock.patch.object(ideas, "run_child", return_value="haiku text"):
            self.assertEqual(self.ms._ask(self.p, "sys", "prompt"), ("haiku text", "haiku"))

    def test_a_failing_local_model_falls_through_to_the_next_and_the_stop_names_each_error(self):  # T-3152
        from sampo import ideas, models
        first, second = dict(self.PROFILE), dict(self.PROFILE, name="gpu-mimo9", base_url="http://127.0.0.1:9/v1")

        def answer(prof, *a, **k):
            if prof["name"] == "jarvis":
                raise models.ModelError("timed out after 300 s")
            return {"text": "gpu text"} if gpu_answers else {"text": ""}
        os.environ.pop("MOONS_LOCAL", None)
        with mock.patch.dict(os.environ, {"MOONS_TEST_KEY": "k"}), \
                mock.patch.object(self.ms, "_generative", return_value=[first, second]), \
                mock.patch.object(self.ms, "_up", return_value=True), \
                mock.patch.object(self.ms, "_down", {}), \
                mock.patch.object(models, "complete", side_effect=answer) as comp, \
                mock.patch.object(ideas, "run_child") as child:
            gpu_answers = True
            self.assertEqual(self.ms._ask(self.p, "sys", "prompt"), ("gpu text", "gpu-mimo9"))
            self.ms._ask(self.p, "sys", "prompt")
            self.assertEqual([c[0][0]["name"] for c in comp.call_args_list], ["jarvis", "gpu-mimo9", "gpu-mimo9"],
                             "a model that just failed is skipped for a while, not waited on every call")
            gpu_answers = False
            with mock.patch.object(self.ms, "_LOCAL_ONLY", True), mock.patch.object(self.ms, "_down", {}):
                with self.assertRaises(self.ms.LocalOnly) as e:
                    self.ms._ask(self.p, "sys", "prompt")
            self.assertIn("jarvis: timed out after 300 s", str(e.exception))
            self.assertIn("gpu-mimo9: empty reply", str(e.exception))
            child.assert_not_called()

    def test_local_only_never_reaches_haiku_when_no_local_model_answers_or_it_errs(self):  # T-2916
        from sampo import ideas, models
        with mock.patch.object(self.ms, "_LOCAL_ONLY", True), mock.patch.object(ideas, "run_child") as child:
            with mock.patch.dict(os.environ, {"MOONS_LOCAL": ""}):
                with self.assertRaises(self.ms.LocalOnly) as e:
                    self.ms._ask(self.p, "sys", "prompt")
                self.assertIn("local-only", str(e.exception))
            with mock.patch.dict(os.environ, {"MOONS_TEST_KEY": "k", "MOONS_LOCAL": "jarvis"}), \
                    mock.patch.object(models, "resolve", return_value=dict(self.PROFILE)), \
                    mock.patch.object(self.ms, "_up", return_value=True), \
                    mock.patch.object(models, "complete", side_effect=models.ModelError("down")):
                with self.assertRaises(self.ms.LocalOnly):
                    self.ms._ask(self.p, "sys", "prompt")
            with self.assertRaises(self.ms.LocalOnly):  # audit: cheap=False (promote's strongest model) is paid too
                self.ms._ask(self.p, "sys", "prompt", cheap=False)
        child.assert_not_called()

    def test_grow_and_funnel_local_only_stop_with_the_reason_and_spend_nothing(self):  # T-2916
        from sampo import ideas
        with mock.patch.dict(os.environ, {"MOONS_LOCAL": ""}), mock.patch.object(ideas, "run_child") as child:
            r = self.ms.grow(self.p, 100, local_only=True)
            self.assertEqual((r["stopped"], r["calls"]), ("local", 1))
            self.assertIn("local-only", r["error"])
            cell = {"problem": "slow tests", "source": "pain", "kind": "bugs", "mechanism": "biology",
                    "scale": "file", "surface": "terminal"}
            self.ms.add(self.p, [good()], cell, "haiku", 1)
            with mock.patch.object(self.ms, "_embedder", return_value=None):  # the shingle fallback: no model
                r = self.ms.funnel(self.p, local_only=True)
            self.assertEqual(r["stopped"], "local")
            self.assertEqual((r["calls"], r["paid"]), (1, 0))
            self.assertIn("local-only", r["error"])
        child.assert_not_called()
        self.assertIn("(local-only", self.ms.funnel_line(r))

    def test_night_ready_wants_an_answering_local_model_a_plain_project_and_room_under_the_target(self):  # T-2916
        up = mock.patch.object(self.ms, "_local", return_value=dict(self.PROFILE))
        down = mock.patch.object(self.ms, "_local", return_value=None)
        with up:
            self.assertTrue(self.ms.night_ready(self.p))
            with mock.patch.object(self.ms, "TARGET", 0):
                self.assertFalse(self.ms.night_ready(self.p), "the target is met")
                self.ms.add(self.p, [good()], {"problem": "x", "source": "pain", "kind": "bugs", "mechanism": "law",
                                               "scale": "file", "surface": "terminal"}, "haiku", 1)
                self.assertEqual(self.ms.night_ready(self.p), {"funnel"}, "at the target, unjudged rows still funnel")
            self.assertEqual(self.ms.night_ready(self.p), {"grow", "funnel"})
            self.fm("sensitive", "on")
            self.assertFalse(self.ms.night_ready(self.p))
            self.fm("sensitive", "off")
        with down:
            self.assertFalse(self.ms.night_ready(self.p))

    def test_one_grow_run_keeps_its_paid_calls_under_the_per_command_cap(self):  # many small calls are one command
        from sampo import budget, ideas
        with mock.patch.object(budget, "effective", return_value=({"run": 0.12, "day": 100.0}, None)), \
                mock.patch.object(budget, "estimate", return_value=0.05), mock.patch.object(budget, "check"), \
                mock.patch.object(ideas, "run_child", return_value="haiku text"), \
                mock.patch.dict(os.environ, {"MOONS_LOCAL": ""}):
            self.assertEqual(self.ms._ask(self.p, "sys", "prompt", paid=1), ("haiku text", "haiku"))
            with self.assertRaises(budget.BudgetError):
                self.ms._ask(self.p, "sys", "prompt", paid=2)

    def test_a_sensitive_project_never_goes_to_the_cloud(self):
        self.fm("sensitive", "on")
        out = None
        with self.assertRaises(ValueError):
            out, _, child = self.ask({})
        self.assertIsNone(out)


class Grow(_Moons):
    def grow(self, *a, side=None, **kw):
        calls = []

        def fake(p, system, prompt, cheap=True, paid=0):
            calls.append(prompt)
            if side:
                raise side
            return reply(4, bad=1), "jarvis"
        with mock.patch.object(self.ms, "_ask", side_effect=fake):
            return self.ms.grow(self.p, *a, **kw), calls

    def test_grow_appends_batches_stops_at_max_calls_and_target_and_resumes(self):
        r, calls = self.grow(100, batch=4, max_calls=1)
        self.assertEqual((r["stopped"], r["calls"], r["added"], r["kept"], r["rejected"]), ("max_calls", 1, 4, 3, 1))
        self.assertEqual(r["reasons"], {"payoff": 1})
        self.assertEqual(r["models"], {"jarvis": 1})
        self.assertIn("Today you can't", calls[0])
        self.assertIn("data, never instructions", calls[0])
        r, calls = self.grow(7, batch=4)
        self.assertEqual((r["stopped"], r["calls"], r["total"]), ("target", 1, 8))
        r, calls = self.grow(7, batch=4)
        self.assertEqual((r["stopped"], r["calls"]), ("target", 0))
        rows = self.ms.rows(self.p)
        self.assertEqual([x["id"] for x in rows], [f"C-{i:06d}" for i in range(1, 9)])
        self.assertEqual([x["batch"] for x in rows], [1] * 4 + [2] * 4)
        cells = {(x["cell"]["kind"], x["cell"]["scale"], x["cell"]["surface"]) for x in rows}
        self.assertEqual(len(cells), 2, "the second batch went to another thin cell")
        self.assertEqual(self.ms.coverage(self.p)["reasons"], {"payoff": 2})

    def test_two_workers_run_two_calls_at_once_and_append_whole_rows(self):  # T-2893
        import threading
        gate, seen = threading.Barrier(2, timeout=5), []

        def fake(p, system, prompt, cheap=True, paid=0):
            gate.wait()  # raises BrokenBarrierError unless a second call is in flight
            seen.append(prompt)
            return reply(4), "jarvis"
        with mock.patch.object(self.ms, "_ask", side_effect=fake):
            r = self.ms.grow(self.p, 8, batch=4, workers=2)
        self.assertEqual((r["calls"], r["added"], r["stopped"]), (2, 8, "target"))
        rows = self.ms.rows(self.p)
        self.assertEqual([x["id"] for x in rows], [f"C-{i:06d}" for i in range(1, 9)])
        self.assertEqual(sorted({x["batch"] for x in rows}), [1, 2])
        self.assertEqual(len({x["title"] for x in rows[:4]}), 4)
        with self.assertRaises(ValueError):
            self.ms.grow(self.p, 8, workers=0)

    def test_the_line_counts_local_haiku_and_empty_by_length_per_model(self):
        from sampo import models
        n = []

        def fake(p, system, prompt, cheap=True, paid=0):
            n.append(1)
            if len(n) == 1:
                with self.ms._lock:
                    self.ms._cuts["jarvis"] += 1  # what _ask books when the local reply was cut empty
                return reply(2), "haiku"
            return reply(2), "jarvis"
        with mock.patch.object(self.ms, "_ask", side_effect=fake):
            r = self.ms.grow(self.p, 100, batch=2, max_calls=2)
        self.assertEqual((r["models"], r["cut"]), ({"haiku": 1, "jarvis": 1}, {"jarvis": 1}))
        self.assertIn("haiku 1, jarvis 1; empty by length (reasoning ate the budget): jarvis 1", self.ms.grow_line(r))
        prof = {"name": "jarvis", "kind": "openai", "base_url": "https://127.0.0.1:9/v1", "model": "m",
                "key_env": None, "context": 8192, "tools": False}
        err = models.ModelError("jarvis: reasoning used the whole budget: no text")
        with mock.patch.object(self.ms, "_locals", return_value=[prof]), mock.patch.object(models, "complete", side_effect=err), \
                mock.patch.object(self.ms.ideas, "run_child", return_value="h"):
            self.ms._cuts.clear()
            self.assertEqual(self.ms._ask(self.p, "s", "p"), ("h", "haiku"))
        self.assertEqual(dict(self.ms._cuts), {"jarvis": 1})

    def test_think_and_max_out_reach_the_local_call(self):
        from sampo import models
        prof = {"name": "jarvis", "kind": "openai", "base_url": "https://127.0.0.1:9/v1", "model": "m",
                "key_env": None, "context": 8192, "tools": False, "max_out": 1500}
        with mock.patch.object(self.ms, "_locals", return_value=[prof]), \
                mock.patch.object(models, "complete", return_value={"text": "t"}) as comp:
            self.ms._ask(self.p, "s", "p")
            self.assertEqual((comp.call_args.kwargs["max_tokens"], comp.call_args.kwargs["think"]), (1500, None))
            self.ms._THINK = False
            try:
                self.ms._ask(self.p, "s", "p")
            finally:
                self.ms._THINK = None
            self.assertIs(comp.call_args.kwargs["think"], False)
            self.ms._THINK = True
            try:
                self.assertEqual(self.ms._ask(self.p, "s", "p"), ("t", "jarvis+think"))
            finally:
                self.ms._THINK = None
        cell = {"problem": "slow tests", "source": "pain", "kind": "bugs", "mechanism": "biology", "scale": "file",
                "surface": "terminal"}
        self.ms.add(self.p, [good(), good(kill="")], cell, "jarvis+think", 1)
        self.ms.add(self.p, [good()], cell, "jarvis+nothink", 2)
        by = self.ms.coverage(self.p)["by_model"]
        self.assertEqual(by, {"jarvis+think": {"rows": 2, "kept": 1}, "jarvis+nothink": {"rows": 1, "kept": 1}})
        self.assertIn("Kept by model: jarvis+nothink 1 of 1, jarvis+think 1 of 2", self.ms.coverage_lines(self.ms.coverage(self.p)))

    def test_the_default_local_profile_is_the_first_own_or_loopback_generative_one(self):
        from sampo import models

        def prof(name, base, model, **kw):
            return {"name": name, "kind": "openai", "base_url": base, "model": model, "key_env": None, "context": 8192,
                    "tools": False, **kw}
        conf = {"cloud": prof("cloud", "https://api.example.com/v1", "big"),
                "embed": prof("embed", "http://127.0.0.1:11434/v1", "nomic", own=True),
                "decide": prof("decide", "http://127.0.0.1:11434/v1", "jev", own=True),
                "mine": prof("mine", "https://gpu.example.com/v1", "qwen", own=True),
                "loop": prof("loop", "http://127.0.0.1:8080/v1", "small")}
        env = {k: v for k, v in os.environ.items() if k != "MOONS_LOCAL"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(models, "configured", return_value=(None, conf)), \
                mock.patch.object(models, "_local", side_effect=lambda *a: iter([prof("ollama", "http://127.0.0.1:11434/v1", "gen")])), \
                mock.patch.object(models, "embedding_profiles", return_value=[prof("nomic", "", "nomic")]), \
                mock.patch.object(models, "systemone_profiles", return_value=[prof("jev", "", "jev")]), \
                mock.patch.object(self.ms, "_up", return_value=True):
            self.assertEqual(self.ms._local(False)["name"], "mine")
            self.assertEqual(self.ms._local(True)["name"], "loop", "a sensitive project's is loopback only")
        with mock.patch.dict(os.environ, {"MOONS_LOCAL": ""}), mock.patch.object(models, "configured", return_value=(None, conf)):
            self.assertIsNone(self.ms._local(False), "an empty MOONS_LOCAL turns the local model off")

    def test_grow_tells_each_call_how_many_paid_calls_came_before(self):
        seen = []

        def fake(p, system, prompt, cheap=True, paid=0):
            seen.append(paid)
            return reply(2), ("haiku" if len(seen) < 3 else "jarvis")
        with mock.patch.object(self.ms, "_ask", side_effect=fake):
            self.ms.grow(self.p, 100, batch=2, max_calls=4)
        self.assertEqual(seen, [0, 1, 2, 2], "a local answer costs nothing")

    def test_moons_inside_a_wrapper_object_are_found(self):
        self.assertEqual(len(self.ms._objects(json.dumps({"moons": [good(), good()]}), 8)), 2)
        self.assertEqual(len(self.ms._objects("[" + json.dumps(good()) + ", {\"note\": 1}]", 8)), 1)

    def test_a_batch_takes_no_more_than_it_asked_for(self):
        with mock.patch.object(self.ms, "_ask", return_value=(reply(30), "haiku")):
            r = self.ms.grow(self.p, 100, batch=3, max_calls=1)
        self.assertEqual(r["added"], 3)

    def test_grow_stops_cleanly_on_the_budget_and_after_three_failures(self):
        from sampo import budget
        r, calls = self.grow(50, side=budget.BudgetError("budget: over the cap"))
        self.assertEqual((r["stopped"], r["added"], len(calls)), ("budget", 0, 1))
        self.assertIn("over the cap", r["error"])
        r, calls = self.grow(50, side=ValueError("nothing came back"))
        self.assertEqual((r["stopped"], len(calls)), ("failed", 3))
        with mock.patch.object(self.ms, "_ask", return_value=("no json here", "haiku")):
            self.assertEqual(self.ms.grow(self.p, 50)["stopped"], "failed")
        self.assertEqual(self.ms.rows(self.p), [])

    def test_bad_arguments_are_refused(self):
        for kw in ({"target": -1}, {"target": 5, "batch": 0}, {"target": 5, "batch": 500}, {"target": 5, "max_calls": -2}):
            with self.subTest(kw), self.assertRaises(ValueError):
                self.ms.grow(self.p, **kw)


class Cli(_Moons):
    def test_coverage_and_grow_verbs(self):
        cov = self.fm_json("moons", "coverage")
        self.assertEqual((cov["total"], len(cov["thinnest"])), (0, 10))
        text = self.fm("moons", "coverage").stdout
        self.assertIn("0 candidates", text)
        r = self.fm_json("moons", "grow", "--target", "0")
        self.assertEqual((r["stopped"], r["calls"]), ("target", 0))
        bad = self.fm("moons", "grow", "--batch", "0", check=False)
        self.assertNotEqual(bad.returncode, 0)

    def test_funnel_families_and_promote_verbs(self):
        r = self.fm_json("moons", "funnel")
        self.assertEqual((r["calls"], r["stopped"]), (0, "done"))
        self.assertEqual(self.fm_json("moons", "families")["families"], [])
        self.assertEqual(self.fm_json("moons", "promote", "--max", "0")["blocks"], [])
        self.assertIn("Moons funnel", self.fm("moons", "funnel", "--stage", "kill").stdout)
        self.assertNotEqual(self.fm("moons", "funnel", "--stage", "bogus", check=False).returncode, 0)


# T-2869 -----------------------------------------------------------------------------------------------------

def moon(title, today, with_this, **kw):
    return good(title=title, today="Today you can't " + today, with_this="With this, you can " + with_this, **kw)


FLAKY = good()
DEPLOY = moon("Roll back a bad deploy before users notice", "undo a Friday deploy without waking the whole team.",
              "pull a broken release out of production in seconds.")
REVIEW = moon("Answer a waiting review within the hour", "get a pull request reviewed without nagging people for days.",
              "merge your code the same day it is ready.")
SECRET = moon("Stop a secret before it is pushed", "see a secret slip into a repo until a scanner emails you.",
              "keep every key out of your code before it leaves your machine.")
NUDGE = moon("Nudge whoever owes you a pull request", "tell who is sitting on your pull request.",
             "get your code merged without chasing anyone.")
FLAKY_SAME = good(title="Catch a FLAKY test, before it reaches anyone!")         # the exact key matches FLAKY's
FLAKY_NEAR = good(title="Catch a flaky test before it reaches anybody")         # one word: a shingle dup
FLAKY_ALSO = moon("Tell flaky tests from real failures", "trust a red build on a busy morning.",
                  "stop rerunning your tests until they pass.")                 # other words, the same idea: embed dup
# the fake embedding: the first keyword the text holds picks its vector (cosine: nudge~review 0.90, mutate~flaky 0.80)
VEC = (("nudge", (0, 0, 0.9, 0.436)), ("mutate", (0.8, 0.6, 0, 0)), ("leak", (0, 0, 0.6, 0.8)), ("flaky", (1, 0, 0, 0)),
       ("deploy", (0, 1, 0, 0)), ("review", (0, 0, 1, 0)), ("secret", (0, 0, 0, 1)))
PROFILE = {"name": "jarvis", "kind": "openai", "base_url": "https://127.0.0.1:9/v1", "model": "m",
           "key_env": "MOONS_TEST_KEY", "context": 8192, "tools": False}


class Judge:
    """A fake _ask for the funnel's judges, routed on the system prompt; answers by title, a default otherwise."""

    def __init__(self, ms, kill=None, tier=None, done=None, side=None, repair=None, fixed=None, wilder=None):
        from sampo import moons
        self.route = {ms.KILL_SYSTEM: ("kill", kill or {}, "yes | one person, one day, a clear bar"),
                      moons.RATE_SYSTEM: ("tier", tier or {}, "high | wild 3 | a new capability"),
                      ms.NOVEL_SYSTEM: ("novelty", done or {}, "no | nothing does this"),
                      ms.REPAIR_SYSTEM: ("repair", repair or {}, "none"),
                      ms.WILDER_SYSTEM: ("wilder", wilder or {}, "none")}
        self.systems, self.prompts = [], []
        self.calls, self.side, self.fixed = [], side, fixed  # fixed: a kill test holding this passes the kill judge

    def __call__(self, p, system, prompt, cheap=True, paid=0, **kw):
        stage, said, default = self.route[next(k for k in self.route if system.startswith(k))]  # T-3310: the kill judge's note follows its rule
        self.calls.append(stage)
        self.systems.append(system)
        self.prompts.append(prompt)
        if self.side:
            raise self.side
        out = []
        for chunk in prompt.split("<moon n=")[1:]:
            n, t = chunk.split(">")[0], re.search(r"Title: (.*)", chunk).group(1)
            out.append(f"{n}: {default if stage == 'kill' and self.fixed and self.fixed in chunk else said.get(t, default)}")
        return "\n".join(x for x in out if not x.endswith(": skip")), "jarvis"


class _Funnel(_Moons):
    def setUp(self):
        super().setUp()
        self.embeds = []

    def seed(self, *objs):
        cell = {"problem": "slow tests", "source": "pain", "kind": "bugs", "mechanism": "biology", "scale": "file",
                "surface": "terminal"}
        return [i for i, _, _ in self.ms.add(self.p, list(objs), cell, "haiku", 1)]

    def embed(self, profile, texts, timeout=120):
        self.embeds.append(list(texts))
        return [list(next((v for k, v in VEC if k in t.lower()), (0.5, 0.5, 0.5, 0.5))) for t in texts]

    def go(self, judge=None, local=True, **kw):
        from sampo import models
        judge = judge or Judge(self.ms)
        with mock.patch.object(self.ms, "_ask", side_effect=judge), \
                mock.patch.object(self.ms, "_embedder", return_value=dict(PROFILE) if local else None), \
                mock.patch.object(models, "embed", side_effect=self.embed):
            return self.ms.funnel(self.p, **kw), judge

    def fates(self):
        return {k: (e["stage"], e["verdict"]) for k, e in self.ms.fate(self.p).items()}

    def events(self):
        with open(os.path.join(self.ms._dir(self.p), self.ms.FUNNEL)) as f:
            return f.read().splitlines()


class EmbedStage(_Funnel):
    def test_the_embed_stage_has_its_own_profile_the_best_ranked_embedding_model(self):  # T-2893
        import http.server
        import threading
        from sampo import models
        seen = []

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _a(self):
                n = int(self.headers.get("Content-Length") or 0)
                seen.append((self.path, json.loads(self.rfile.read(n)) if n else None))
                if self.path == "/api/tags":
                    out = {"models": [{"name": "weak-embed", "capabilities": ["embedding"]},
                                      {"name": "bge-m3:567m", "capabilities": ["embedding"]},
                                      {"name": "gen", "capabilities": ["completion"]}]}
                elif self.path == "/v1/models":
                    out = {"data": [{"id": "bge-m3:567m"}]}
                else:
                    out = {"data": [{"index": 0, "embedding": [1.0, 0.0]}]}
                data = json.dumps(out).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            do_GET = do_POST = _a
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, args=(0.02,), daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        url, real = f"http://127.0.0.1:{srv.server_address[1]}", models.embedding_profiles
        bench = os.path.join(self.p.dir, "research", "models")
        os.makedirs(bench)
        with open(os.path.join(bench, "bench.jsonl"), "w") as f:
            for name, rec in (("weak-embed", 0.5), ("bge-m3:567m", 0.9)):
                f.write(json.dumps({"arm": "embedding", "use": "title-to-brief", "backend": name, "recall_at_1": rec}) + "\n")
        env = {k: v for k, v in os.environ.items() if k not in ("MOONS_EMBED", "FOREMAN_NO_LOCAL_MODELS")}  # its own server
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(models, "embedding_profiles", side_effect=lambda *a: real(url)), \
                mock.patch.object(models, "_local", side_effect=lambda *a: iter(())):
            prof = self.ms._embedder(self.p)
            self.assertEqual(prof["model"], "bge-m3:567m", "the model the bench ranks best")
            self.assertEqual(models.embed(prof, ["a"]), [[1.0, 0.0]])
            self.assertEqual(seen[-1], ("/v1/embeddings", {"model": "bge-m3:567m", "input": ["a"]}))
            self.fm("sensitive", "on")
            self.assertEqual(self.ms._embedder(self.p)["model"], "bge-m3:567m", "loopback is fine for a sensitive project")
        with mock.patch.dict(os.environ, {"MOONS_EMBED": ""}):
            self.assertIsNone(self.ms._embedder(self.p), "an empty MOONS_EMBED turns it off")

class Embed(_Moons):
    def test_embed_posts_the_openai_shape_and_orders_by_index(self):
        from sampo import models
        prof = dict(PROFILE, embed_model="e5")
        with mock.patch.dict(os.environ, {"MOONS_TEST_KEY": "k"}), mock.patch.object(models, "_post", return_value={
                "data": [{"index": 1, "embedding": [0, 1.5]}, {"index": 0, "embedding": [1, 0]}]}) as post:
            self.assertEqual(models.embed(prof, ["a", "b"]), [[1, 0], [0, 1.5]])
        url, body, headers = post.call_args[0][:3]
        self.assertEqual((url, body, headers), ("https://127.0.0.1:9/v1/embeddings", {"model": "e5", "input": ["a", "b"]},
                                                {"Authorization": "Bearer k"}))
        with mock.patch.dict(os.environ, {"MOONS_TEST_KEY": "k"}), \
                mock.patch.object(models, "_post", return_value={"data": [{"embedding": [1]}]}) as post:
            models.embed(dict(PROFILE), ["a"])
        self.assertEqual(post.call_args[0][1]["model"], "m", "no embed_model: the profile's model")

    def test_a_bad_reply_or_profile_is_a_model_error(self):
        from sampo import models
        for reply in ({"data": []}, {"data": [{"embedding": ["x"]}]}, {"data": [{"embedding": [True]}]}, {"nope": 1},
                      {"data": [{"embedding": []}]}):
            with self.subTest(reply), mock.patch.dict(os.environ, {"MOONS_TEST_KEY": "k"}), \
                    mock.patch.object(models, "_post", return_value=reply), self.assertRaises(models.ModelError):
                models.embed(dict(PROFILE), ["a"])
        os.environ.pop("MOONS_TEST_KEY", None)
        with self.assertRaises(models.ModelError):
            models.embed(dict(PROFILE), ["a"])  # the key unset
        with self.assertRaises(models.ModelError):
            models.embed(dict(PROFILE, kind="anthropic", key_env=None), ["a"])
        self.assertEqual(models._profile("x", {"model": "m", "embed_model": "e"}, "w")["embed_model"], "e")
        self.assertNotIn("embed_model", models._profile("x", {"model": "m"}, "w"))
        with self.assertRaises(models.ModelError):
            models._profile("x", {"model": "m", "embed_model": 3}, "w")


class Funnel(_Funnel):
    def test_kill_batches_run_at_once_up_to_the_local_slots(self):  # T-2953: 3 min a batch, one at a time
        import threading
        import time
        ids = self.seed(*[moon(f"Moon {i} for case {chr(97 + i % 26)}{i}", f"do thing {i}.", f"do thing {i} today.")
                          for i in range(2 * self.ms.JUDGE_BATCH + 1)])
        judge, live, peak, lock = Judge(self.ms), [0], [0], threading.Lock()

        def slow(*a, **kw):
            with lock:
                live[0] += 1
                peak[0] = max(peak[0], live[0])
            time.sleep(0.2)
            with lock:
                live[0] -= 1
            return judge(*a, **kw)
        rs = self.ms.rows(self.p)
        for stage, run in (("kill", self.ms._kill), ("tier", self.ms._tier)):  # T-2959: the tier stage too
            r = {"stages": {stage: {}}, "calls": 0, "models": {}, "stopped": "", "error": "", "paid": 0, "fails": 0}
            peak[0] = 0
            with mock.patch.object(self.ms, "_ask", side_effect=slow), \
                    mock.patch.object(self.ms, "_local", return_value={"name": "jarvis", "slots": 2}):
                run(self.p, r, rs, self.ms.fate(self.p), rs, None)
            self.assertEqual((stage, peak[0], r["calls"]), (stage, 2, 3))
            self.assertEqual({i: self.fates()[i][0] for i in ids}, {i: stage for i in ids})

    def test_each_candidate_gets_its_fate_and_reason_in_cost_order(self):
        ids = self.seed(FLAKY, DEPLOY, REVIEW, SECRET, NUDGE, FLAKY_SAME, FLAKY_ALSO, good(kill=""))
        judge = Judge(self.ms, kill={DEPLOY["title"]: "no | it needs live production traffic"},
                      tier={FLAKY["title"]: "extreme | wild 5 | new", SECRET["title"]: "low | wild 1 | a check",
                            NUDGE["title"]: "medium | wild 2 | a reminder"},
                      done={REVIEW["title"]: "yes | GitHub's review reminders"})
        r, _ = self.go(judge)
        self.assertEqual(judge.calls, ["kill", "tier", "novelty", "repair"], "cheapest first, one batch each")
        self.assertEqual((r["stopped"], r["calls"], r["method"]), ("done", 4, "embed"))
        f = self.ms.fate(self.p)
        self.assertEqual(self.fates(), {
            ids[0]: ("novelty", "kept"), ids[1]: ("kill", "rejected"), ids[2]: ("novelty", "rejected"),
            ids[3]: ("tier", "pooled"), ids[4]: ("tier", "merged"), ids[5]: ("exact", "dup"),
            ids[6]: ("embed", "dup"), ids[7]: ("write", "rejected")})
        self.assertEqual((f[ids[5]]["dup_of"], f[ids[5]]["reason"]), (ids[0], "exact"))
        self.assertEqual((f[ids[6]]["dup_of"], f[ids[6]]["reason"]), (ids[0], "embed"))
        self.assertEqual(f[ids[1]]["reason"], "kill: it needs live production traffic")
        self.assertEqual(f[ids[3]]["reason"], "tier: low, kept in the pool")  # T-3310: unique, so pooled, not thrown away
        self.assertEqual(f[ids[4]]["merged_into"], ids[2])
        self.assertEqual(f[ids[2]]["reason"], "novelty: already done: GitHub's review reminders")
        self.assertEqual((f[ids[0]]["tier"], f[ids[0]]["wild"]), ("extreme", 5))
        self.assertEqual(len(self.embeds), 1, "one embed call for the six rows that passed the exact check")
        self.assertEqual(len(self.embeds[0]), 6)
        with open(os.path.join(self.ms._dir(self.p), self.ms.VECTORS)) as fh:
            self.assertEqual(len(fh.read().splitlines()), 6)
        # export: the funnel's counts, and the one survivor's family
        x = self.ms.export(self.p)["funnel"]
        self.assertEqual((x["generated"], x["write"], x["dups"]), (8, {"kill test": 1}, {"exact": 1, "embed": 1,
                                                                                           "shingle": 0}))
        self.assertEqual((x["rejected"], x["merged"], x["kept"], x["pending"], x["pooled"]),
                         ({"kill": 1, "tier": 0, "novelty": 1}, 1, 1, 0, 1))
        fam = self.ms.export(self.p)
        self.assertEqual((fam["families_total"], fam["families"][0]["lead"], fam["families"][0]["tier"]),
                         (1, FLAKY["title"], "extreme"))
        # resumable: a second run judges nothing, embeds nothing and appends nothing
        before, n = self.events(), len(self.embeds)
        r, judge = self.go()
        self.assertEqual((r["calls"], judge.calls, len(self.embeds), self.events()), (0, [], n, before))
        # a new candidate: only its vector is fetched; the rest come from vectors.jsonl
        new = self.seed(moon("Mutate the tests that never fail", "know which of your tests would catch a real bug.",
                             "delete the tests that guard nothing in your code."))
        self.go(stage="exact")
        r, _ = self.go(stage="embed")
        self.assertEqual(self.embeds[-1], [self.ms._text(self.ms.rows(self.p)[-1])])
        self.assertEqual(self.fates()[new[0]], ("embed", "kept"))
        self.assertEqual(list(r["stages"]), ["embed"], "--stage runs that stage alone")

    def test_the_shingle_fallback_when_no_model_embeds(self):
        from sampo import models
        ids = self.seed(FLAKY, FLAKY_NEAR, FLAKY_ALSO, DEPLOY)
        r, _ = self.go(local=False, stage="exact")
        self.assertEqual(list(r["stages"]), ["exact"])
        r, _ = self.go(local=False, stage="embed")
        self.assertEqual(r["method"], "shingle")
        f = self.ms.fate(self.p)
        self.assertEqual((f[ids[1]]["verdict"], f[ids[1]]["reason"], f[ids[1]]["dup_of"]), ("dup", "shingle", ids[0]))
        self.assertEqual([f[i]["verdict"] for i in (ids[0], ids[2], ids[3])], ["kept"] * 3)
        self.assertEqual(f[ids[2]]["reason"], "shingle")
        self.assertEqual(self.embeds, [])
        with mock.patch.object(self.ms, "_embedder", return_value=dict(PROFILE)), \
                mock.patch.object(models, "embed", side_effect=models.ModelError("no /embeddings here")):
            self.assertEqual(self.ms._reps(self.p, self.ms.rows(self.p))[0], "shingle")

    def test_a_kill_test_that_needs_a_team_is_rejected_with_why_and_a_missed_answer_waits(self):
        ids = self.seed(FLAKY, DEPLOY, REVIEW)
        judge = Judge(self.ms, kill={DEPLOY["title"]: "no | it needs ten engineers for a month",
                                     REVIEW["title"]: "skip"})
        self.go(judge, stage="exact")
        self.go(judge, stage="embed")
        r, _ = self.go(judge, stage="kill")
        self.assertEqual(self.fates(), {ids[0]: ("kill", "kept"), ids[1]: ("kill", "rejected"),
                                        ids[2]: ("embed", "kept")})
        self.assertEqual(self.ms.fate(self.p)[ids[1]]["reason"], "kill: it needs ten engineers for a month")
        r, judge = self.go(stage="kill")
        self.assertEqual((judge.calls, self.fates()[ids[2]]), (["kill"], ("kill", "kept")))  # T-3045: the reject's repair was asked once, in the run before

    def test_the_novelty_stage_rejects_a_near_catalogue_title_unasked(self):
        ids = self.seed(FLAKY, DEPLOY)
        os.makedirs(self.ms.rune.home(self.p), exist_ok=True)
        with open(os.path.join(self.ms.rune.home(self.p), "moonshot.md"), "w") as f:
            f.write("# Moonshot catalogue\n\n### M-01 · Catch flaky tests early\n- Tier: high\n")
        r, judge = self.go()
        f = self.ms.fate(self.p)
        self.assertEqual((f[ids[0]]["stage"], f[ids[0]]["verdict"]), ("novelty", "rejected"))
        self.assertTrue(f[ids[0]]["reason"].startswith("novelty: near M-01"), f[ids[0]]["reason"])
        self.assertEqual(self.fates()[ids[1]], ("novelty", "kept"))

    def test_max_calls_and_the_budget_stop_a_run_that_resumes(self):
        from sampo import budget
        ids = self.seed(FLAKY, DEPLOY)
        r, judge = self.go(max_calls=1)
        self.assertEqual((r["stopped"], judge.calls), ("max_calls", ["kill"]))
        self.assertEqual(self.fates()[ids[0]], ("kill", "kept"))
        r, judge = self.go(Judge(self.ms, side=budget.BudgetError("budget: over the cap")))
        self.assertEqual((r["stopped"], r["calls"]), ("budget", 1))
        self.assertIn("over the cap", r["error"])
        self.assertEqual(self.fates()[ids[0]], ("kill", "kept"))
        r, judge = self.go(Judge(self.ms, side=ValueError("down")))
        self.assertEqual((r["stopped"], len(judge.calls)), ("failed", 3))
        r, judge = self.go()
        self.assertEqual((r["stopped"], judge.calls), ("done", ["tier", "novelty"]))
        self.assertEqual(self.fates()[ids[1]], ("novelty", "kept"))
        with self.assertRaises(ValueError):
            self.ms.funnel(self.p, stage="bogus")


class Families(_Funnel):
    def survive(self, *rows):
        """Each (moon, tier, wild) stored and marked kept at novelty, as the funnel would."""
        ids = self.seed(*(m for m, _, _ in rows))
        lines = [json.dumps({"id": i, "at": "2026-11-03T00:00:00Z", "stage": "novelty", "verdict": "kept",
                             "reason": "", "model": "jarvis", "tier": t, "wild": w}) for i, (_, t, w) in zip(ids, rows)]
        self.c.append_line(os.path.join(self.ms._dir(self.p), self.ms.FUNNEL), "\n".join(lines))
        return ids

    def build(self, local=True):
        from sampo import models
        with mock.patch.object(self.ms, "_local", return_value=dict(PROFILE) if local else None), \
                mock.patch.object(self.ms, "_embedder", return_value=dict(PROFILE) if local else None), \
                mock.patch.object(models, "embed", side_effect=self.embed):  # T-3220: never the box's own embedders
            return self.ms.families(self.p)

    MUTATE = moon("Mutate the tests that never fail", "know which of your tests would catch a real bug.",
                  "delete the tests that guard nothing in your code.")
    LEAK = moon("Trace where a leak started", "find which commit let a key out of your repo.",
                "rotate only the keys that were exposed in your code.")

    def test_families_are_stable_and_led_by_tier_then_wild_then_the_earliest(self):
        ids = self.survive((FLAKY, "high", 3), (DEPLOY, "extreme", 4), (self.MUTATE, "high", 5), (SECRET, "xhigh", 2),
                           (self.LEAK, "xhigh", 2))
        r = self.build()
        got = [(f["id"], f["lead"], f["variants"], f["size"]) for f in r["families"]]
        self.assertEqual(got, [("F-000002", ids[1], [], 1), ("F-000004", ids[3], [ids[4]], 2),
                               ("F-000003", ids[2], [ids[0]], 2)])
        self.assertEqual(r["families"][0]["kinds"], ["bugs"])
        self.assertEqual(r["families"][0]["mechanisms"], ["biology"])
        path = os.path.join(self.ms._dir(self.p), self.ms.FAMILIES)
        with open(path) as f:
            first = f.read()
        self.assertEqual(self.build()["families"], r["families"])
        self.assertEqual(self.build(local=False)["families"], r["families"], "the server down: the cached vectors")
        with open(path) as f:
            self.assertEqual(f.read(), first, "a rerun writes the same file")
        self.assertEqual(self.ms.scale_problems(self.p), [])


class Promote(_Funnel):
    def setUp(self):
        super().setUp()
        from sampo import rune
        self.rune = rune
        rune.init(self.p, GEN0_CAPS.encode())
        self.doc = os.path.join(rune.home(self.p), "moonshot.md")
        with open(self.doc, "w") as f:
            f.write("# Moonshot catalogue\n\n### M-01 · Market for lane time\n- Tier: high\n\n"
                    "### M-02 · Grow a test garden\n- Tier: xhigh\n")

    def test_leads_land_in_moonshot_md_through_the_cold_read_once(self):
        from sampo import ideas
        ids = Families.survive(self, (FLAKY, "high", 3), (DEPLOY, "extreme", 4), (Families.MUTATE, "massive", 5),
                               (SECRET, "xhigh", 2))
        self.build_families()
        seen = []

        def child(feature, system, prompt, model, timeout, project=None, **kw):
            seen.append((feature, model))
            if feature == "genesis-read":
                return "CLEAN"
            if feature == "moonshot-rate":
                return "\n".join(f"{i}: massive | wild 5 | bold" for i in range(1, 30))
            return ("Kill: Run it on 10 repos for one day; if it catches fewer than 3 real bugs, it is dead.\n"
                    "Better by: no tool mutates and deletes tests on its own\nNovelty: 5\nNeeds: none\n")
        with mock.patch.object(self.rune, "REVIEW_MODELS", ("haiku", "opus-x")), \
                mock.patch.object(ideas, "run_child", side_effect=child), \
                mock.patch.object(self.rune, "put", wraps=self.rune.put) as put:
            r = self.ms.promote(self.p)
            self.assertEqual(r["blocks"], ["M-03", "M-04", "M-05"], r)
            self.assertEqual(put.call_args[0][1], "moonshot.md")
            self.assertIn(("moons-promote", "opus-x"), seen, "the strongest model designs the kill test")
            self.assertIn("genesis-read", [f for f, _ in seen], "the cold read passed it")
            again = self.ms.promote(self.p)
            third = self.ms.promote(self.p)
        self.assertEqual(again["promoted"], ["F-000003"], "the family's variant is the one moon left")
        self.assertEqual(third["blocks"], [], "a promoted family isn't promoted twice")
        with open(self.doc) as f:
            doc = f.read()
        block = doc[doc.index("### M-03"):doc.index("### M-04")]
        for want in (f"### M-03 · {Families.MUTATE['title']}", "- Tier: massive", "- Domain: bugs", "- Lens: biology",
                     "- What: Today you can't", "- Prior art: flaky-test dashboards", "- Better by: no tool mutates",
                     "- Novelty: 5", "- Needs: none", "- Unblocks: none", "- Kill: Run it on 10 repos", "- Stage: idea",
                     "- Family: F-000003 (1 variant)"):
            self.assertIn(want, block)
        self.assertIn(f"### M-04 · {DEPLOY['title']}", doc)
        self.assertLess(doc.index(DEPLOY["title"]), doc.index(SECRET["title"]), "an xhigh lead is drawn after the big tiers")
        from sampo import moons
        rated = {x["id"]: x["reviewer"] for x in moons.rows(self.p)}
        self.assertEqual(rated["M-03"], "massive", "moons.rate gave the cold tier")
        self.assertFalse([x for x in moons.check(self.p) if "family" in x.lower()])
        out = os.path.join(self.tmp, "site")
        self.rune.export(self.p, out)
        with open(os.path.join(out, "genesis.json")) as f:
            sc = json.load(f)["moons_scale"]
        self.assertEqual(sc["funnel"]["generated"], 4)
        lead = next(x for x in sc["families"] if x["id"] == "F-000003")
        self.assertEqual((lead["cold"], lead["stage"], lead["size"]), ("massive", "idea", 2))

    def build_families(self):
        return Families.build(self)

    def test_check_names_a_family_line_with_no_family_unknown_ids_and_stale_families(self):
        Families.survive(self, (FLAKY, "high", 3))
        bad = self.ms.scale_problems(self.p)
        self.assertTrue(any("families.json" in x for x in bad), bad)
        self.build_families()
        self.assertEqual(self.ms.scale_problems(self.p), [])
        with open(self.doc, "a") as f:
            f.write("\n### M-03 · Ghost\n- Tier: massive\n- Family: F-999999 (2 variants)\n")
        self.c.append_line(os.path.join(self.ms._dir(self.p), self.ms.FUNNEL),
                           json.dumps({"id": "C-999999", "stage": "kill", "verdict": "kept"}))
        bad = self.ms.scale_problems(self.p)
        self.assertTrue(any("F-999999" in x for x in bad), bad)
        self.assertTrue(any("C-999999" in x for x in bad), bad)
        from sampo import moons
        self.assertTrue(any("F-999999" in x for x in moons.check(self.p)))


class Catalogue(_Funnel):
    """T-3047: every kept moon exported beside genesis.json, and promotion drawing from all of them."""

    def build_families(self):
        return Families.build(self)

    def many(self, n):
        ids = self.seed(*(moon(f"Idea number {i}", f"do thing {i}.", f"get thing {i} done.") for i in range(n)))
        lines = [json.dumps({"id": i, "at": "2026-11-03T00:00:00Z", "stage": "novelty", "verdict": "kept", "reason": "",
                             "model": "m", "tier": "high" if k % 2 else "extreme", "wild": k % 5 + 1})
                 for k, i in enumerate(ids)]
        self.c.append_line(os.path.join(self.ms._dir(self.p), self.ms.FUNNEL), "\n".join(lines))
        return ids

    def load_cat(self, out):
        """The index, then each shard it names, joined in order."""
        with open(os.path.join(out, "moons-catalogue.json")) as f:
            idx = json.load(f)
        rows = []
        for s in idx["shards"]:
            with open(os.path.join(out, s["file"])) as f:
                rows += json.load(f)
        self.assertEqual(len(rows), idx["total"])
        return rows

    def test_the_catalogue_is_an_index_and_shards_a_visitor_fetches_a_page_at_a_time(self):  # T-3047 review 4
        ids = self.many(250)
        out = os.path.join(self.tmp, "site")
        with mock.patch.object(self.ms, "SHARD", 100):
            self.ms.export(self.p, out)
        with open(os.path.join(out, "moons-catalogue.json")) as f:
            idx = json.load(f)
        self.assertEqual((idx["total"], idx["per"], [s["n"] for s in idx["shards"]]), (250, 100, [100, 100, 50]))
        self.assertEqual(sorted(idx["tiers"].items()), [("extreme", 125), ("high", 125)])
        self.assertEqual([s["tiers"] for s in idx["shards"]], [["extreme"], ["extreme", "high"], ["high"]])
        self.assertLess(os.path.getsize(os.path.join(out, "moons-catalogue.json")), 2000, "the index stays small")
        self.assertEqual(sorted(r["id"] for r in self.load_cat(out)), sorted(ids))
        with mock.patch.object(self.ms, "SHARD", 200):  # fewer shards: no stale file is left behind
            self.ms.export(self.p, out)
        self.assertEqual(sorted(x for x in os.listdir(out) if x.startswith("moons-catalogue")),
                         ["moons-catalogue-001.json", "moons-catalogue-002.json", "moons-catalogue.json"])

    def test_the_public_catalogue_carries_no_problem_or_source_text_and_a_real_scrub_redacts_it(self):  # T-3047 review 6, 10
        ids = self.seed(moon("Secret title sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123", "do a thing.", "get a thing."))
        self.c.append_line(os.path.join(self.ms._dir(self.p), self.ms.FUNNEL), json.dumps(
            {"id": ids[0], "at": "2026-11-03T00:00:00Z", "stage": "novelty", "verdict": "kept", "reason": "",
             "model": "m", "tier": "high", "wild": 3}))
        rs = [dict(x, cell={"kind": "bugs", "scale": "team", "surface": "cli", "mechanism": "mutate",
                            "problem": "PRIVATE-PROBLEM", "source": "PRIVATE-SOURCE"}) for x in self.ms.rows(self.p)]
        out = os.path.join(self.tmp, "site")

        def scrub(x):
            if isinstance(x, str):
                return self.c.redact(x)
            if isinstance(x, list):
                return [scrub(v) for v in x]
            return {k: scrub(v) for k, v in x.items()} if isinstance(x, dict) else x
        with mock.patch.object(self.ms, "rows", return_value=rs):
            self.ms.export(self.p, out, scrub=scrub)
        raw = "".join(open(os.path.join(out, x)).read() for x in os.listdir(out))
        self.assertNotIn("PRIVATE", raw)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz0123", raw, "the scrub ran over every file")
        self.assertEqual(set(self.load_cat(out)[0]),
                         {"id", "title", "today", "with_this", "grid", "tier", "wild", "family", "lead", "drawn"})

    def test_export_writes_every_kept_moon_beside_the_page_data_not_a_top_slice(self):
        ids = self.many(250)
        out = os.path.join(self.tmp, "site")
        got = self.ms.export(self.p, out)
        self.assertEqual(got["catalogue"], {"file": "moons-catalogue.json", "total": 250})
        rows = self.load_cat(out)
        self.assertEqual(sorted(r["id"] for r in rows), sorted(ids), "all 250 survivors, EXPORT_TOP is for families")
        self.assertGreater(250, self.ms.EXPORT_TOP)
        r = rows[0]
        for k in ("id", "title", "today", "with_this", "grid", "tier", "wild", "family", "lead", "drawn"):
            self.assertIn(k, r)
        self.assertEqual({x["grid"] for x in rows}, {"user"}, "a row with no grid is the user's")
        self.assertEqual((rows[0]["tier"], rows[-1]["tier"]), ("extreme", "high"), "best first")
        self.assertEqual(self.ms.export(self.p, out)["catalogue"]["total"], 250, "a second run writes the same file")

    def test_a_grid_family_and_a_hostile_line_reach_the_catalogue(self):
        ids = Families.survive(self, (FLAKY, "high", 3), (Families.MUTATE, "massive", 5), (DEPLOY, "extreme", 4))
        self.build_families()
        real = self.ms.rows
        rs = [dict(x, grid="sampo") if x["id"] == ids[2] else x for x in real(self.p)]
        out = os.path.join(self.tmp, "site")
        with mock.patch.object(self.ms, "rows", return_value=rs):
            self.ms.export(self.p, out, scrub=lambda x: x)
        by = {r["id"]: r for r in self.load_cat(out)}
        self.assertEqual((by[ids[2]]["grid"], by[ids[0]]["grid"]), ("sampo", "user"))
        self.assertEqual((by[ids[1]]["lead"], by[ids[0]]["lead"]), (True, False))
        self.assertEqual(by[ids[0]]["family"], by[ids[1]]["family"])
        self.assertNotEqual(by[ids[2]]["family"], by[ids[1]]["family"])

    def test_rune_export_writes_the_catalogue_and_tells_the_page_where_it_is(self):
        from sampo import rune
        self.many(5)
        out = os.path.join(self.tmp, "site")
        rune.export(self.p, out)
        with open(os.path.join(out, "genesis.json")) as f:
            self.assertEqual(json.load(f)["moons_scale"]["catalogue"], {"file": "moons-catalogue.json", "total": 5})
        self.assertTrue(os.path.isfile(os.path.join(out, "moons-catalogue.json")))

    def promote(self, **kw):
        from sampo import ideas
        seen = []

        def child(feature, system, prompt, model, timeout, project=None, **k):
            seen.append(feature)
            if feature == "genesis-read":
                return "CLEAN"
            if feature == "moonshot-rate":
                return "\n".join(f"{i}: massive | wild 5 | bold" for i in range(1, 30))
            return self.reply
        with mock.patch.object(self.rune, "REVIEW_MODELS", ("haiku", "opus-x")), \
                mock.patch.object(ideas, "run_child", side_effect=child):
            r = self.ms.promote(self.p, **kw)
        return r, seen

    reply = ("Kill: Run it on 10 repos for one day; if it catches fewer than 3 real bugs, it is dead.\n"
             "Better by: no tool does it\nNovelty: 5\nNeeds: none\n")

    def setUp(self):
        super().setUp()
        from sampo import rune
        self.rune = rune
        rune.init(self.p, GEN0_CAPS.encode())
        self.doc = os.path.join(rune.home(self.p), "moonshot.md")
        with open(self.doc, "w") as f:
            f.write("# Moonshot catalogue\n\n### M-01 · Market for lane time\n- Tier: high\n")

    def test_promotion_draws_family_leads_first_then_variants_and_records_each_draw(self):
        ids = Families.survive(self, (FLAKY, "extreme", 3), (Families.MUTATE, "massive", 5), (DEPLOY, "extreme", 4),
                               (SECRET, "high", 2))
        self.build_families()
        r, _ = self.promote(max_n=2)
        self.assertEqual(r["blocks"], ["M-02", "M-03"], r)
        with open(self.doc) as f:
            doc = f.read()
        self.assertLess(doc.index(Families.MUTATE["title"]), doc.index(DEPLOY["title"]), "massive lead, then extreme")
        self.assertNotIn(FLAKY["title"], doc, "the variant waits behind every lead")
        r, _ = self.promote()
        self.assertEqual(len(r["blocks"]), 2, r)
        with open(self.doc) as f:
            doc = f.read()
        self.assertLess(doc.index(FLAKY["title"]), doc.index(SECRET["title"]), "a big variant before a lower tier's lead")
        self.assertEqual(self.promote()[0]["blocks"], [], "nothing is drawn twice")
        draws = [d for e in self.c.ledger_events(self.p, ["moons_drawn"]) for d in e["data"]["draws"]]
        self.assertEqual([d["id"] for d in draws], [ids[1], ids[2], ids[0], ids[3]], "every kept tier is drawn, big first")
        self.assertEqual([d["block"] for d in draws], ["M-02", "M-03", "M-04", "M-05"])

    def test_a_moved_catalogue_changes_what_live_publishing_compares(self):  # T-3047 review 3
        from sampo import rune
        self.many(5)
        out = os.path.join(self.tmp, "site")
        rune.export(self.p, out)
        gj = os.path.join(out, "genesis.json")
        before = rune._body(gj)
        self.assertEqual(rune._body(gj), before, "a second read is the same")
        with open(os.path.join(out, "moons-catalogue-001.json"), "a") as f:
            f.write("\n")
        self.assertNotEqual(rune._body(gj), before, "genesis.json is the same, the shard is not")

    def test_a_refused_put_and_a_title_already_in_the_catalogue_are_recorded_as_drawn(self):  # T-3047 review 2
        ids = Families.survive(self, (Families.MUTATE, "massive", 5), (moon("Market for lane time", "x a.", "y b."), "high", 2))
        self.build_families()
        with mock.patch.object(self.rune, "put", side_effect=ValueError("cold read refused")):
            r, seen = self.promote()
        self.assertIn("cold read refused", r["error"])
        r, seen = self.promote()
        self.assertEqual((r["blocks"], r["error"], seen), ([], "", []), "no second paid call")
        d = {x["id"]: x["block"] for e in self.c.ledger_events(self.p, ["moons_drawn"]) for x in e["data"]["draws"]}
        self.assertEqual(d, {ids[0]: "", ids[1]: ""}, "the refused draw and the duplicate title are both spent")

    def test_a_promote_run_while_another_holds_the_store_draws_nothing(self):  # T-3047 review 11
        Families.survive(self, (Families.MUTATE, "massive", 5))
        self.build_families()
        import threading
        held, done = threading.Event(), threading.Event()

        def other():  # another funnel holds the store from its own thread: the lock is re-entrant per thread only (T-3081)
            with self.ms.store_lock(self.p):
                held.set()
                done.wait(30)
        t = threading.Thread(target=other)
        t.start()
        try:
            held.wait(30)
            r, seen = self.promote()
        finally:
            done.set()
            t.join()
        self.assertEqual((r["blocks"], seen), ([], []))
        self.assertIn("another funnel", r["error"])

    def test_a_draw_that_gave_no_kill_test_is_recorded_and_not_paid_for_again(self):
        ids = Families.survive(self, (Families.MUTATE, "massive", 5))
        self.build_families()
        self.reply = "Kill: try it and see\n"
        r, seen = self.promote()
        self.assertEqual((r["blocks"], len(r["skipped"])), ([], 1))
        r, seen = self.promote()
        self.assertEqual((r["skipped"], seen), ([], []), "the second run draws nothing and calls no model")
        d = [d for e in self.c.ledger_events(self.p, ["moons_drawn"]) for d in e["data"]["draws"]]
        self.assertEqual([(x["id"], x["block"]) for x in d], [(ids[0], "")])

    def test_the_catalogue_marks_a_drawn_moon_with_its_block(self):
        ids = Families.survive(self, (Families.MUTATE, "massive", 5))
        self.build_families()
        self.promote()
        out = os.path.join(self.tmp, "site")
        self.ms.export(self.p, out)
        self.assertEqual(self.load_cat(out)[0]["drawn"], "M-02")


class PromoteDomain(_Moons):
    def test_a_lead_domain_reads_as_plain_words(self):
        # T-2940: promote wrote 'refactors, at ecosystem scale, in the phone' and the cold read refused the catalogue
        from sampo import moonscale as ms
        self.assertEqual(ms._domain({"kind": "refactors", "scale": "ecosystem", "surface": "phone"}),
                         "refactors across the open-source ecosystem, from a phone")
        for s in ms.SCALES:
            for u in ms.SURFACES:
                d = ms._domain({"kind": "bugs", "scale": s, "surface": u})
                self.assertNotRegex(d, r" scale,|in the phone|in the CI|in the chat")
                self.assertTrue(d.startswith("bugs "), d)


class StoreLock(_Funnel):
    def test_a_second_funnel_or_families_exits_naming_the_holder_and_judges_nothing(self):  # T-2986
        self.seed(moon("Lint the lint", "lint twice", "lint once"))
        with self.ms.store_lock(self.p):
            for verb, code in (("funnel", 75), ("families", 1)):  # T-3929: the night's funnel exits 75 (busy), not a failure
                r = self.fm("moons", verb, check=False)
                self.assertEqual(r.returncode, code, r.stderr)
                self.assertIn(f"another funnel holds the moons store (pid {os.getpid()})", r.stderr)
            self.assertEqual(self.fm("moons", "funnel", "--status").returncode, 0)  # read-only: no lock
        self.assertEqual(self.fm("moons", "funnel", "--max-calls", "0", check=False).returncode, 0)  # released

    def test_status_is_the_funnels_alone(self):  # T-2865 review: families --status wrote families.json
        r = self.fm("moons", "families", "--status", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("funnel --status", r.stderr)

    def test_status_counts_duplicates_and_merges(self):  # T-2865 review: a dup was in no bucket
        first, second = self.seed(moon("A", "a", "b"), moon("B", "c", "d"))
        self.ms._log(self.p, [self.ms._event(first, "exact", "kept"), self.ms._event(second, "exact", "dup")],
                     {"stages": {"exact": {}}}, "exact")
        s = self.ms.status(self.p)
        self.assertEqual(s["stages"]["exact"], {"kept": 1, "rejected": 0, "dropped": 1, "waiting": 0})
        self.assertIn("exact 1 kept, 0 rejected, 1 dup or merged, 0 waiting", self.ms.status_line(s))

    def test_status_prints_each_stages_counts_without_a_model_call(self):  # T-2986
        first, _ = self.seed(moon("A", "a", "b"), moon("B", "c", "d"))
        self.ms._log(self.p, [self.ms._event(first, "exact", "kept")], {"stages": {"exact": {}}}, "exact")
        s = self.fm_json("moons", "funnel", "--status")
        self.assertEqual(s["stages"]["exact"], {"kept": 1, "rejected": 0, "dropped": 0, "waiting": 1})
        self.assertEqual(s["stages"]["embed"]["waiting"], 1)
        self.assertEqual(s["total"], 2)
        self.assertIn("exact 1 kept", self.fm("moons", "funnel", "--status").stdout)


# T-3045: yield (normalise and re-admit, the Sampo grid, kill repair, data-fed prompts) -------------------------------

class Yield(_Funnel):
    def test_with_this_missing_only_the_opening_is_normalised_at_write(self):
        you = good(with_this="You can ship a release knowing which red tests in your code are noise.")
        get = good(title="Another title", with_this="With this, you get a release with no noisy tests in your code.")
        out = self.ms.add(self.p, [you, get, good(title="Third", with_this="Ship faster with your code.")],
                          {"kind": "bugs", "scale": "file", "surface": "terminal"}, "haiku", 1)
        self.assertEqual([v for _, v, _ in out], ["kept", "rejected", "rejected"])
        self.assertTrue(self.ms.rows(self.p)[0]["with_this"].startswith("With this, you can ship"))

    def test_old_write_rejects_that_now_pass_are_readmitted_by_a_new_event_and_a_rerun_adds_none(self):
        row = dict(good(with_this="You can see which red tests in your code are noise."), id="C-000001", at="t",
                   cell={"kind": "bugs", "scale": "file", "surface": "terminal"}, model="haiku", batch=1)
        bad = dict(good(title="Bad", kill=""), id="C-000002", at="t", cell={}, model="haiku", batch=1)
        d = self.ms._dir(self.p)
        os.makedirs(d)
        with open(os.path.join(d, self.ms.CANDIDATES), "w") as f:
            f.write(json.dumps(row) + "\n" + json.dumps(bad) + "\n")
        with open(os.path.join(d, self.ms.FUNNEL), "w") as f:
            for i, why in (("C-000001", 'line: with_this must start "With this, you can"'), ("C-000002", "kill test: missing")):
                f.write(json.dumps({"id": i, "at": "t", "stage": "write", "verdict": "rejected", "reason": why}) + "\n")
        before = len(self.events())
        self.assertEqual(self.ms.readmit(self.p, dry=True), 1)
        self.assertEqual(len(self.events()), before, "a dry count writes nothing")
        self.assertEqual(self.ms.readmit(self.p), 1)
        self.assertEqual(self.fates()["C-000001"], ("write", "kept"))
        self.assertEqual(self.fates()["C-000002"], ("write", "rejected"))
        self.assertEqual(len(self.events()), before + 1)
        self.assertEqual(self.ms.readmit(self.p), 0)
        self.go()
        self.assertEqual(self.fates()["C-000001"][0], "novelty", "the funnel takes it from there")

    def sampo_cell(self, **kw):
        return {"grid": "sampo", "kind": "memory", "scale": "one task", "surface": "", "mechanism": "biology",
                "problem": "slow tests", "source": "pain", **kw}

    def test_the_sampo_grid_is_coverage_headings_plus_three_read_thinnest_first_with_the_user_grid(self):
        os.makedirs(self.ms.rune.home(self.p))
        with open(os.path.join(self.ms.rune.home(self.p), "coverage.md"), "w") as f:
            f.write("# Coverage\n\n## 1. Initiative\n- a\n\n## Docs\n- b\n")
        self.assertEqual(self.ms.sampo_areas(self.p), ["initiative", "docs", "problem solving", "memory", "efficiency"])
        cell = {"kind": "bugs", "scale": "file", "surface": "terminal", "mechanism": "law", "problem": "x", "source": "pain"}
        with mock.patch.object(self.ms, "KINDS", ("bugs",)), mock.patch.object(self.ms, "SCALES", ("file",)), \
                mock.patch.object(self.ms, "SURFACES", ("terminal",)):
            self.ms.add(self.p, [good()], cell, "haiku", 1)
            self.assertTrue(all(x["grid"] == "sampo" for x in self.ms.pick(self.p, 5, 0)))
            last = self.ms.pick(self.p, 26, 0)
            self.assertEqual(len({(x["kind"], x["scale"]) for x in last[:25]}), 25)
            self.assertEqual(last[-1].get("grid"), None, "25 sampo cells, then the user cell that holds a row")

    def test_a_sampo_cell_accepts_sampos_own_parts_and_sampo_can(self):
        own = good(with_this="With this, Sampo can keep the ledger and the guard in step across every session.")
        self.assertFalse(self.ms.admit(own)[0])
        self.assertTrue(self.ms.admit(dict(own, cell={"grid": "sampo"}))[0])
        out = self.ms.add(self.p, [dict(own, with_this="Sampo can remember a lane across sessions.")],
                          self.sampo_cell(), "haiku", 1)
        self.assertEqual(out[0][1], "kept")
        self.assertTrue(self.ms.rows(self.p)[0]["with_this"].startswith("With this, Sampo can"))

    def test_a_sampo_cells_prompt_asks_about_sampo_itself(self):
        text = self.ms._prompt(self.sampo_cell(), 3)
        self.assertIn("Sampo itself", text)
        self.assertIn("With this, Sampo can", text)
        self.assertNotIn("never on the harness's own parts", text)
        self.assertIn("never on the harness's own parts", self.ms._prompt(dict(self.sampo_cell(), grid=""), 3))

    def kill_no(self):
        return Judge(self.ms, kill={DEPLOY["title"]: "no | needs real production data for weeks"}, fixed="synthetic",
                     repair={DEPLOY["title"]: "Run it on 10 synthetic deploy logs on one laptop in a day; if fewer than 8 "
                                              "are caught, it is dead."})

    def test_a_kill_rejected_moon_gets_one_rewritten_kill_test_as_a_child_row_judged_like_any_row(self):
        a, b = self.seed(DEPLOY, REVIEW)
        judge = self.kill_no()
        self.go(judge)
        rows = {r["id"]: r for r in self.ms.rows(self.p)}
        kids = [r for r in rows.values() if r.get("parent") == a]
        self.assertEqual(len(kids), 1)
        kid = kids[0]
        self.assertEqual((kid["repair"], kid["title"], kid["with_this"]), ("kill", DEPLOY["title"], DEPLOY["with_this"]))
        self.assertIn("synthetic deploy logs", kid["kill"])
        self.assertEqual(self.fates()[a], ("kill", "rejected"))
        self.assertFalse([r for r in rows.values() if r.get("parent") == b])
        self.assertEqual(self.ms.fate(self.p)[kid["id"]]["stage"], "write")
        self.go(judge)  # a rerun: the kid passes every stage like any row; nothing is repaired twice
        self.assertEqual(self.fates()[kid["id"]], ("novelty", "kept"), "dedupe does not call the child its parent's copy")
        self.assertEqual(len([r for r in self.ms.rows(self.p) if r.get("parent")]), 1)

    def test_repairs_show_their_own_bar_not_the_last_stage_at_n_of_n(self):  # T-3044 review
        from sampo import core as c
        self.seed(DEPLOY, REVIEW)
        judge, seen = self.kill_no(), []

        def ask(p, system, *a, **kw):
            if system == self.ms.REPAIR_SYSTEM:
                with open(c._progress_path()) as f:
                    seen.append(json.load(f))
            return judge(p, system, *a, **kw)
        self.go(ask)
        self.assertEqual([(x["done"], x["total"], x["unit"], x["note"]) for x in seen], [(0, 1, "rows", "repair")])

    def test_a_repair_that_the_kill_judge_refuses_is_never_repaired_again(self):
        self.seed(DEPLOY)
        judge = Judge(self.ms, kill={DEPLOY["title"]: "no | needs a team"},
                      repair={DEPLOY["title"]: "Run it on 10 logs in a day; if fewer than 8 are caught, it is dead."})
        for _ in range(3):
            self.go(judge)
        kids = [r for r in self.ms.rows(self.p) if r.get("parent")]
        self.assertEqual(len(kids), 1)
        self.assertEqual(self.fates()[kids[0]["id"]], ("kill", "rejected"))

    def test_each_cells_prompt_carries_its_kinds_top_reject_reasons_and_two_kept_moons(self):
        ids = self.seed(*[moon(f"Bug moon {i}", f"do thing {i}.", f"do thing {i} today.") for i in range(4)])
        ev = self.ms._event
        self.ms._log(self.p, [ev(ids[0], "kill", "rejected", "kill: needs real production data"),
                              ev(ids[1], "kill", "rejected", "kill: needs a month of production data"),
                              ev(ids[2], "novelty", "kept"), ev(ids[3], "novelty", "kept")],
                     {"stages": {"kill": {}, "novelty": {}}}, "kill")
        hints = self.ms._hints(self.ms.rows(self.p), self.ms.fate(self.p))
        cell = {"kind": "bugs", "scale": "file", "surface": "terminal", "mechanism": "law", "problem": "p", "source": "pain"}
        text = self.ms._prompt(cell, 2, hints.get("bugs", ""))
        self.assertIn("needs real or production data (2)", text)
        self.assertIn("Bug moon 2", text)
        self.assertIn("Bug moon 3", text)
        self.assertEqual(self.ms._prompt(dict(cell, kind="docs"), 2, hints.get("docs", "")).count("Bug moon"), 0)

    # the reviewer's findings on 04a78cda -------------------------------------------------------------------------
    def test_a_model_cannot_name_a_parent_or_repair_and_duplicates_are_still_caught(self):
        a = self.seed(DEPLOY)[0]
        self.seed(dict(DEPLOY, parent=["x"], repair="kill"), dict(DEPLOY, parent=a, repair="kill"))
        self.assertFalse([x for x in self.ms.rows(self.p) if "parent" in x or "repair" in x])
        self.go()  # no crash on the unhashable parent
        self.assertEqual(self.fates()[a][0], "novelty")
        self.assertEqual([v for _, v in list(self.fates().values())[1:]], ["dup", "dup"])

    def test_with_this_you_get_is_not_the_users_line(self):
        out = self.ms.add(self.p, [good(with_this="With this, you get a release with no noisy tests in your code.")],
                          {"kind": "bugs", "scale": "file", "surface": "terminal"}, "haiku", 1)
        self.assertEqual(out[0][1], "rejected")

    def test_repair_runs_after_novelty_on_what_the_cap_leaves_and_never_in_a_single_stage_run(self):
        self.seed(DEPLOY, REVIEW)
        r, judge = self.go(self.kill_no(), max_calls=3)
        self.assertEqual(judge.calls, ["kill", "tier", "novelty"], "the cap went to the stages first")
        self.assertEqual(r["stopped"], "max_calls")
        r, judge = self.go(self.kill_no(), stage="kill")
        self.assertNotIn("repair", judge.calls)
        r, judge = self.go(self.kill_no())
        self.assertEqual(judge.calls[-1], "repair")
        self.assertIn("wait for the next run", self.ms.funnel_line(r))

    def test_a_readmitted_row_reads_with_its_normalised_line(self):
        d = self.ms._dir(self.p)
        os.makedirs(d)
        with open(os.path.join(d, self.ms.CANDIDATES), "w") as f:
            f.write(json.dumps(dict(good(with_this="You can see noise."), id="C-000001", cell={})) + "\n")
        self.assertEqual(self.ms.rows(self.p)[0]["with_this"], "With this, you can see noise.")

    def test_coverage_reports_the_sampo_grid(self):
        self.ms.add(self.p, [good()], self.sampo_cell(), "haiku", 1)
        r = self.ms.coverage(self.p)
        n = len(self.ms.sampo_areas(self.p)) * len(self.ms.SAMPO_SCALES)
        self.assertEqual((r["sampo_grid"], r["sampo_touched"], len(r["sampo_thinnest"])), (n, 1, min(10, n)))
        self.assertIn(f"1 of {n} Sampo cells", self.ms.coverage_lines(r))

    def test_themes_match_whole_words_and_name_other_stages_by_their_head(self):
        t = self.ms._theme
        self.assertEqual(t("kill: needs real data"), "needs real or production data")
        self.assertNotEqual(t("kill: a realistic plan, delivered in a timeline"), "needs real or production data")
        self.assertEqual(t("novelty: near C-000123 (Some title)"), "novelty: near")
        self.assertEqual(t("tier: low or medium"), "tier: low or medium")

    def test_a_sampo_scale_reads_in_plain_words_in_a_leads_domain(self):
        self.assertEqual(self.ms._domain({"kind": "memory", "scale": "one task", "surface": ""}), "memory in one task")
        self.assertEqual(self.ms._domain(self.sampo_cell(scale="every Sampo user", surface="")),
                         "memory for every Sampo user")


class KeepGrow(_Moons):  # T-3046: the goal counts kept moons, so grow and the funnel take turns until N survive
    def run_keep(self, keep, per_funnel=3, grow_stop="target", fun_calls=2, grow_add=5, stops=None, **kw):
        st, seen = {"s": 0, "n": 0}, []

        def funnel(p, stage=None, max_calls=None, local_only=False):
            seen.append(("funnel", max_calls, local_only))
            st["s"] += per_funnel
            return {"stages": {"novelty": {"kept": per_funnel} if per_funnel else {}}, "calls": fun_calls,
                    "stopped": "done", "error": "", "models": {}}

        def grow(p, target=None, batch=8, seed=0, max_calls=None, workers=None, think=None, local_only=False):
            seen.append(("grow", max_calls, local_only))
            st["n"] += grow_add
            return {"calls": 4, "added": grow_add, "kept": grow_add, "rejected": 0,
                    "stopped": next(stops, grow_stop) if stops else grow_stop, "error": "",
                    "models": {"jarvis": 4}, "total": st["n"], "reasons": {}}
        with mock.patch.object(self.ms, "survivors", side_effect=lambda p: st["s"]), \
                mock.patch.object(self.ms, "funnel", funnel), mock.patch.object(self.ms, "grow", grow):
            return self.ms._keep(self.p, keep, **kw), seen

    def test_rounds_of_funnel_then_grow_run_until_the_survivors_reach_keep(self):
        r, seen = self.run_keep(10, local_only=True)
        self.assertEqual((r["stopped"], r["survivors"], r["rounds"]), ("keep", 12, 4))
        self.assertEqual([s[0] for s in seen], ["funnel", "grow"] * 3 + ["funnel"])
        self.assertTrue(all(s[2] for s in seen), "--local-only reaches both jobs")
        self.assertIn("12 kept of 10", self.ms.grow_line(r))

    def test_pace_gives_kept_per_hour_eta_and_the_stage_that_drops_most(self):  # T-3514
        st = {"total": 1000, "stages": {"exact": {"rejected": 10, "dropped": 5}, "kill": {"rejected": 600, "dropped": 0}}}
        r = {"survivors": 150, "keep": 1000}
        with mock.patch.object(self.ms, "status", return_value=st), mock.patch.object(self.ms.time, "time", return_value=7200):
            k = self.ms.keep_pace(self.p, r, 0, 50)
            self.assertEqual((k["per_hour"], k["eta_hours"], k["top_drop"], k["top_drop_share"]), (50, 17.0, "kill", 0.6))
            self.assertEqual(self.ms.keep_pace_text(k), "50 kept/h, ETA 17 h; most dropped at kill (60% of candidates)")
            k = self.ms.keep_pace(self.p, dict(r, survivors=50), 0, 50)
            self.assertIsNone(k["eta_hours"])
            self.assertIn("no ETA yet", self.ms.keep_pace_text(k))

    def test_a_keep_run_reports_its_pace_in_the_line(self):  # T-3514
        r, _ = self.run_keep(10)
        self.assertIn("kept/h", self.ms.grow_line(r))

    def test_max_calls_is_one_budget_for_both_jobs_and_a_failure_stops_the_loop(self):
        r, seen = self.run_keep(1000, max_calls=7)
        self.assertEqual(r["stopped"], "max_calls")
        self.assertEqual([s[1] for s in seen], [7, 5, 1], "each job gets what the run has left")
        for stop in ("budget", "failed"):
            r, _ = self.run_keep(1000, grow_stop=stop)
            self.assertEqual(r["stopped"], stop)

    def test_a_busy_local_model_is_waited_out_and_a_sustained_outage_stops_with_why(self):  # T-3096
        sleeps = []
        with mock.patch.object(self.ms, "_sleep", sleeps.append):
            r, seen = self.run_keep(1000, grow_stop="local")
            self.assertEqual((r["stopped"], sleeps), ("local", [30, 60, 120, 240]))
            self.assertEqual(r["waited"], 450)
            self.assertIn("after waiting 450 s", self.ms.grow_line(r))
            sleeps.clear()
            r, _ = self.run_keep(10, stops=iter(["local", "local"]))  # two failed rounds, then it answers
            self.assertEqual((r["stopped"], sleeps), ("keep", [30, 60]))

    def test_a_timed_out_default_profile_is_asked_again_after_the_wait(self):  # T-3096: a failure marks it down for DOWN_S
        from sampo import models
        st, sleeps, calls = {"s": 0}, [], []

        def complete(prof, *a, **kw):
            calls.append(1)
            if len(calls) == 1:
                raise models.ModelError("timed out")
            return {"text": reply(4)}

        def funnel(p, stage=None, max_calls=None, local_only=False):
            st["s"] = 1 if self.ms.rows(p) else 0
            return {"stages": {}, "calls": 0, "stopped": "done", "error": "", "models": {}}
        self.ms._down.clear()
        with mock.patch.dict(os.environ, {"MOONS_TEST_KEY": "k"}), mock.patch.object(self.ms, "_sleep", sleeps.append), \
                mock.patch.object(self.ms, "_generative", return_value=[dict(PROFILE, name="jarvis")]), \
                mock.patch.object(self.ms, "_up", return_value=True), mock.patch.object(models, "complete", complete), \
                mock.patch.object(self.ms, "survivors", side_effect=lambda p: st["s"]), mock.patch.object(self.ms, "funnel", funnel), \
                mock.patch.object(self.ms, "_embedder", return_value=None), mock.patch.object(models, "embed", side_effect=AssertionError), \
                mock.patch.object(self.ms, "sampo_areas", return_value=[]):
            os.environ.pop("MOONS_LOCAL", None)  # the default path: no named profile, so _down applies
            r = self.ms._keep(self.p, 1, local_only=True)
        self.ms._down.clear()
        self.assertEqual((r["stopped"], sleeps, len(calls) > 1), ("keep", [30], True))
        self.assertGreater(r["added"], 0)

    def test_a_wait_is_skipped_when_max_calls_is_spent(self):  # T-3096
        sleeps = []
        with mock.patch.object(self.ms, "_sleep", sleeps.append):
            r, _ = self.run_keep(1000, grow_stop="local", max_calls=6)
        self.assertEqual(sleeps, [])  # funnel 2 + grow 4 = 6 calls: nothing left to retry with, so no wait
        self.assertEqual(r["calls"], 6)

    def test_three_rounds_that_raise_no_survivor_stop_as_stalled(self):
        r, seen = self.run_keep(1000, per_funnel=0, grow_add=0)
        self.assertEqual((r["stopped"], len(seen)), ("stalled", 6))
        r, seen = self.run_keep(1000, per_funnel=0, grow_add=5)  # rows are added, none survive
        self.assertEqual((r["stopped"], r["rounds"]), ("stalled", 3))

    def test_a_model_whose_rows_all_fail_admit_stalls_with_no_max_calls(self):  # review: rows added but never kept looped forever
        calls = []

        def ask(p, system, prompt, cheap=True, paid=0, **kw):
            calls.append(1)
            self.assertLess(len(calls), 2000, "looped")  # 3 flat rounds of ROUND rows at 4 a call
            return reply(4, bad=4), "jarvis"
        from sampo import models
        with mock.patch.object(self.ms, "_ask", side_effect=ask), mock.patch.object(self.ms, "_embedder", return_value=None), \
                mock.patch.object(models, "embed", side_effect=AssertionError), mock.patch.object(self.ms, "sampo_areas", return_value=[]):  # T-3045: a Sampo cell accepts an internal payoff
            r = self.ms.grow(self.p, keep=100, local_only=True)
        self.assertEqual((r["stopped"], r["survivors"], r["rounds"]), ("stalled", 0, 3))

    def test_keep_with_target_is_refused(self):
        r = self.fm("moons", "grow", "--keep", "5", "--target", "9", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("give one", r.stderr)

    def test_one_real_round_funnels_and_grows_so_the_survivors_rise(self):
        from sampo import models
        judge = Judge(self.ms)

        def ask(p, system, prompt, cheap=True, paid=0, **kw):
            return (reply(3), "jarvis") if system == self.ms.SYSTEM else judge(p, system, prompt, cheap, paid)
        with mock.patch.object(self.ms, "_ask", side_effect=ask), \
                mock.patch.object(self.ms, "_embedder", return_value=dict(PROFILE)), \
                mock.patch.object(models, "embed", side_effect=lambda pr, texts, timeout=120: [[1.0, 0.0, 0.0, 0.0]] * len(texts)):
            r = self.ms.grow(self.p, keep=1, local_only=True)
        self.assertEqual(r["stopped"], "keep")
        self.assertGreaterEqual(self.ms.survivors(self.p), 1)
        self.assertGreater(r["added"], 0)

    def test_the_verb_takes_keep_and_counts_survivors_not_rows(self):
        self.assertEqual(self.fm_json("moons", "grow", "--keep", "0", "--local-only")["stopped"], "keep")
        r = self.fm("moons", "grow", "--keep", "-1", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--keep can't be negative", r.stderr)
        self.assertEqual(self.ms.survivors(self.p), 0)


class FamilyLinks(_Funnel):  # T-3046: the novelty scan stores each survivor's links; families unions them, no all-pairs
    def groups(self, r):
        return sorted(sorted([f["lead"]] + f["variants"]) for f in r["families"])

    def all_pairs(self, ids):
        """The old grouping: every pair, single link at the family bar, by the cached vectors."""
        from sampo import models
        rs, f = {x["id"]: x for x in self.ms.rows(self.p)}, self.ms.fate(self.p)
        ids = [i for i in ids if f.get(i, {}).get("stage") == "novelty" and f[i]["verdict"] == "kept"]
        self.assertGreater(len(ids), 4, "the fixture's survivors")
        with mock.patch.object(self.ms, "_embedder", return_value=dict(PROFILE)), \
                mock.patch.object(models, "embed", side_effect=self.embed):
            method, rep = self.ms._reps(self.p, [rs[i] for i in ids])
        up = {i: i for i in ids}

        def root(i):
            while up[i] != i:
                i = up[i]
            return i
        for a in ids:
            for b in ids:
                if b < a and self.ms._sim(method, rep[a], rep[b]) >= self.ms._bar("family", method):
                    up[root(a)] = root(b)
        out = {}
        for i in ids:
            out.setdefault(root(i), []).append(i)
        return sorted(sorted(g) for g in out.values())

    def fixture(self):
        return self.seed(FLAKY, DEPLOY, Families.MUTATE, SECRET, Families.LEAK, REVIEW, NUDGE)

    def test_links_stored_at_novelty_give_the_all_pairs_groups_with_no_similarity_call(self):
        from sampo import models
        ids = self.fixture()
        r, _ = self.go()
        self.assertEqual(r["stopped"], "done")
        want = self.all_pairs(ids)
        self.assertIn(sorted([ids[0], ids[2]]), want, "mutate links to flaky (0.80)")
        self.assertTrue(os.path.exists(os.path.join(self.ms._dir(self.p), self.ms.LINKS)))
        with mock.patch.object(self.ms, "_sim", side_effect=AssertionError("families scanned pairs")), \
                mock.patch.object(models, "embed", side_effect=AssertionError("families embedded")):
            got = self.ms.families(self.p)
        self.assertEqual(self.groups(got), want)

    def test_survivors_with_no_stored_links_are_backfilled_in_chunks_then_stay_stored(self):
        ids = self.fixture()
        evs = [self.ms._event(i, "novelty", "kept", "", "jarvis", tier="high", wild=3) for i in ids]
        self.c.append_line(os.path.join(self.ms._dir(self.p), self.ms.FUNNEL), "\n".join(json.dumps(e) for e in evs))
        with mock.patch.object(self.ms, "BACKFILL", 2):
            got = self.build_families()
        self.assertEqual(self.groups(got), self.all_pairs(ids))
        with open(os.path.join(self.ms._dir(self.p), self.ms.LINKS)) as f:
            self.assertEqual(len([line for line in f if line.strip()]), len(ids))
        with mock.patch.object(self.ms, "_sim", side_effect=AssertionError("backfilled again")):
            self.assertEqual(self.groups(self.ms.families(self.p)), self.groups(got))

    def test_a_down_embedder_backfills_nothing_and_a_changed_bar_counts_as_no_record(self):  # review: no second method's records
        ids = self.fixture()
        self.go()
        path = os.path.join(self.ms._dir(self.p), self.ms.LINKS)
        with open(path) as g:
            before = g.read()
        os.remove(os.path.join(self.ms._dir(self.p), self.ms.VECTORS))  # no cached vectors: a down embedder means shingles
        rs, f = self.ms.rows(self.p), self.ms.fate(self.p)
        surv = sorted(self.ms._at(rs, f, ("novelty",), ("kept",)), key=lambda x: self.ms._num(x["id"]))
        recs = [json.loads(x) for x in before.splitlines() if x.strip()]
        self.assertTrue(recs and all(x["method"] == "embed" and "bar" in x for x in recs), recs)
        gone = recs[-1]["id"]
        with open(path, "w") as g:  # one survivor lost its record
            g.write("".join(json.dumps(x) + "\n" for x in recs if x["id"] != gone))
        with mock.patch.object(self.ms, "_embedder", return_value=None):  # embedder down: shingles
            method, have = self.ms._links(self.p, surv)
        self.assertEqual(method, "embed")
        self.assertNotIn(gone, have)
        self.assertEqual(len(open(path).read().splitlines()), len(recs) - 1, "nothing written under shingles")
        with open(path, "w") as g:  # a record under another bar is no record
            g.write("".join(json.dumps(dict(x, bar=0.99)) + "\n" for x in recs))
        with mock.patch.object(self.ms, "_embedder", return_value=None):
            self.assertEqual(self.ms._links(self.p, surv)[0], "shingle")  # none counted, so it rescans afresh

    def build_families(self):
        from sampo import models
        with mock.patch.object(self.ms, "_embedder", return_value=dict(PROFILE)), \
                mock.patch.object(models, "embed", side_effect=self.embed):
            return self.ms.families(self.p)

    def test_ten_thousand_synthetic_survivors_group_in_under_ten_seconds(self):
        import time
        n, d = 10000, self.ms._dir(self.p)
        os.makedirs(d, exist_ok=True)
        cell = {"kind": "bugs", "mechanism": "law"}
        with open(os.path.join(d, self.ms.CANDIDATES), "w") as f:
            f.write("".join(json.dumps({"id": f"C-{i:06d}", "title": f"t{i}", "cell": cell}) + "\n" for i in range(1, n + 1)))
        with open(os.path.join(d, self.ms.FUNNEL), "w") as f:
            f.write("".join(json.dumps(self.ms._event(f"C-{i:06d}", "novelty", "kept", "", "m", tier="high", wild=i % 5))
                            + "\n" for i in range(1, n + 1)))
        with open(os.path.join(d, self.ms.LINKS), "w") as f:  # pairs 1-2, 3-4 ...; and a chain through every 100th
            f.write("".join(json.dumps({"id": f"C-{i:06d}", "method": "embed", "bar": self.ms._bar("family", "embed"),
                                        "links": [f"C-{i - 1:06d}"] * (i % 2 == 0) + [f"C-{i - 100:06d}"] * (i % 100 == 1 and i > 100)})
                            + "\n" for i in range(1, n + 1)))
        t = time.time()
        r = self.ms.families(self.p)
        took = time.time() - t
        self.assertLess(took, 10, f"{took:.1f}s")
        self.assertEqual(r["survivors"], n)
        self.assertEqual(len(r["families"]), 5000 - 99)  # 5000 pairs, the 100th-links join 99 of the later ones to the first


# T-3122 -----------------------------------------------------------------------------------------------------

WILDER_JSON = json.dumps({"title": "Roll back every deploy before it ships", "today": "Today you can't undo a release before it lands.",
                          "with_this": "With this, you can ship a release that undoes itself the moment it hurts a user.",
                          "what": "It replays the release against a shadow copy of traffic and reverts on a bad signal.",
                          "kill": "Run it on 10 synthetic deploys on one laptop; if fewer than 8 are caught, it is dead."})


class Wilder(_Funnel):
    """A medium-tier moon gets one 'go wilder' rewrite as a child row (T-3122)."""

    def medium(self, **kw):
        return Judge(self.ms, tier={DEPLOY["title"]: "medium | wild 2 | tame"}, wilder={DEPLOY["title"]: WILDER_JSON}, **kw)

    def test_a_medium_tier_moon_gets_one_rewrite_as_a_child_row_judged_by_tier_again(self):
        a, b = self.seed(DEPLOY, REVIEW)
        judge = self.medium()
        self.go(judge)
        rows = {r["id"]: r for r in self.ms.rows(self.p)}
        kids = [r for r in rows.values() if r.get("parent") == a]
        self.assertEqual(len(kids), 1)
        self.assertEqual((kids[0]["repair"], kids[0]["title"]), ("wilder", "Roll back every deploy before it ships"))
        self.assertEqual(self.fates()[a], ("tier", "pooled"))  # T-3310: a unique medium moon waits in the pool, rewrite or not
        self.assertFalse([r for r in rows.values() if r.get("parent") == b], "a high-tier moon is not rewritten")
        self.assertEqual(self.ms.fate(self.p)[kids[0]["id"]]["stage"], "write")
        self.go(judge)
        self.assertEqual(self.fates()[kids[0]["id"]], ("novelty", "kept"), "the rewrite is rated by tier like any row")
        self.assertEqual(judge.calls.count("wilder"), 1)
        self.assertIn("rewritten wilder", self.ms.funnel_line({**self.go(judge)[0], "wilder": 1}))

    def test_a_rewrite_is_never_rewritten_and_a_parent_never_twice(self):
        a = self.seed(DEPLOY)[0]
        judge = Judge(self.ms, tier={DEPLOY["title"]: "medium | wild 2 | tame",
                                     "Roll back every deploy before it ships": "medium | wild 2 | still tame"},
                      wilder={DEPLOY["title"]: WILDER_JSON})
        for _ in range(4):
            self.go(judge)
        kids = [r for r in self.ms.rows(self.p) if r.get("parent")]
        self.assertEqual([k["parent"] for k in kids], [a])
        self.assertEqual(self.fates()[kids[0]["id"]], ("tier", "merged"))  # T-3310: still medium, and its pooled parent is the same idea
        self.assertEqual(judge.calls.count("wilder"), 1)

    def test_only_a_medium_rejection_is_rewritten_and_a_refusal_is_asked_once(self):
        self.seed(DEPLOY, REVIEW)
        judge = Judge(self.ms, tier={DEPLOY["title"]: "low | wild 1 | weekend", REVIEW["title"]: "medium | wild 2 | tame"})
        for _ in range(3):
            self.go(judge)
        self.assertEqual(judge.calls.count("wilder"), 1, "the low one is not asked; the medium one answered none, once")
        self.assertFalse([r for r in self.ms.rows(self.p) if r.get("parent")])

    def test_the_rewrite_runs_after_the_stages_on_what_the_cap_leaves_and_never_in_a_single_stage_run(self):
        self.seed(DEPLOY)
        judge = self.medium()
        self.go(judge, stage="tier")
        self.assertNotIn("wilder", judge.calls)
        r, judge = self.go(self.medium(), max_calls=2)
        self.assertNotIn("wilder", judge.calls)

    def test_a_model_cannot_name_the_rewrites_parent(self):
        a, b = self.seed(DEPLOY, REVIEW)
        wild = dict(json.loads(WILDER_JSON), parent=b, repair="kill")
        self.go(Judge(self.ms, tier={DEPLOY["title"]: "medium | wild 2 | tame"}, wilder={DEPLOY["title"]: json.dumps(wild)}))
        kid = [r for r in self.ms.rows(self.p) if r.get("parent")]
        self.assertEqual([(k["parent"], k["repair"]) for k in kid], [(a, "wilder")])


class Exemplars(_Funnel):
    """A cell's prompt carries its kind's best kept moons, never a rejected one (T-3122)."""

    def hints(self, *kept, rejected=()):
        ids = self.seed(*[moon(f"Bug moon {i}", f"do thing {i}.", f"do thing {i} today.") for i in range(5)])
        ev, evs = self.ms._event, []
        for i, tier, wild in kept:
            evs.append(ev(ids[i], "novelty", "kept", tier=tier, wild=wild))
        for i in rejected:
            evs.append(ev(ids[i], "tier", "rejected", "tier: low or medium", tier="medium"))
        self.ms._log(self.p, evs, {"stages": {"tier": {}, "novelty": {}}}, "tier")
        return self.ms._hints(self.ms.rows(self.p), self.ms.fate(self.p)).get("bugs", "")

    def test_up_to_two_of_the_kinds_kept_high_tier_moons_are_exemplars_the_wildest_first(self):
        text = self.hints((0, "high", 1), (1, "massive", 3), (2, "high", 5), (3, "high", 2), rejected=(4,))
        self.assertEqual(text.count("Kept moon for reference"), 2)
        self.assertIn("Bug moon 1", text)
        self.assertIn("Bug moon 2", text)
        self.assertNotIn("Bug moon 0", text)
        self.assertNotIn("Bug moon 4", text, "a rejected moon is never an exemplar")

    def test_a_rejected_moon_is_no_exemplar_even_when_it_was_once_kept(self):
        ids = self.seed(DEPLOY, REVIEW)
        ev = self.ms._event
        self.ms._log(self.p, [ev(ids[0], "novelty", "kept", tier="high", wild=4),
                              ev(ids[1], "novelty", "kept", tier="high", wild=4),
                              ev(ids[1], "novelty", "rejected", "novelty: gone")],
                     {"stages": {"novelty": {}}}, "novelty")
        text = self.ms._hints(self.ms.rows(self.p), self.ms.fate(self.p))["bugs"]
        self.assertIn(DEPLOY["title"], text)
        self.assertNotIn(REVIEW["title"], text)

    def test_grow_sends_the_exemplars_in_each_cells_write_prompt(self):
        self.hints((0, "high", 4), (1, "high", 3))
        seen = []

        def ask(p, system, prompt, **kw):
            seen.append(prompt)
            return reply(2), "jarvis"
        with mock.patch.object(self.ms, "_ask", side_effect=ask), \
                mock.patch.object(self.ms, "_pick", side_effect=lambda cnt, probs, k, seed, areas: [
                    {"kind": "bugs", "scale": "file", "surface": "terminal", "mechanism": "law", "problem": "p",
                     "source": "pain"}] * k):
            self.ms.grow(self.p, target=99, batch=2, max_calls=1, workers=1)
        self.assertEqual(sum("Kept moon for reference" in x for x in seen), 1)
        self.assertIn("Bug moon 0", seen[0])


class Writers(_Funnel):
    """sampo moons bench-writers: kept per call per stage for each writer, the fill using the best (T-3122)."""

    A = {"label": "27b", "writer": "local", "profile": "p27"}
    B = {"label": "haiku", "writer": "haiku", "profile": None}

    def bench(self, tier_low=(), kill_no=(), bad=None, **kw):
        asked = self.asked = []
        ms = self.ms
        names = {ms.SYSTEM: "write", ms.KILL_SYSTEM: "kill", ms.NOVEL_SYSTEM: "novelty",
                 ms.REPAIR_SYSTEM: "repair", ms.WILDER_SYSTEM: "wilder", self.rate: "tier"}
        bad = bad or {}

        def ask(p, system, prompt, cheap=True, paid=0, feature=None, writer=None, profile=None):
            who = profile or writer
            stage = names[next(k for k in names if system.startswith(k))]  # T-3310: the kill judge's note follows its rule
            asked.append((stage, who))
            if stage == "write":
                objs = [DEPLOY, REVIEW, SECRET, NUDGE]
                objs = objs[:4 - bad.get(who, 0)] + [dict(x, kill="") for x in objs[4 - bad.get(who, 0):]]
                return "```json\n" + "\n".join(json.dumps(x) for x in objs) + "\n```", who
            out = []
            for chunk in prompt.split("<moon n=")[1:]:
                n = chunk.split(">")[0]
                if stage == "kill":
                    out.append(f"{n}: " + ("no | needs a team" if who in kill_no else "yes | one person, one day"))
                elif stage == "tier":
                    out.append(f"{n}: " + ("low | wild 1 | meh" if who in tier_low else "high | wild 3 | new"))
                elif stage == "novelty":
                    out.append(f"{n}: no | nothing does this")
                else:
                    out.append(f"{n}: none")
            return "\n".join(out), who
        from sampo import models
        with mock.patch.object(ms, "_ask", side_effect=ask), mock.patch.object(ms, "_embedder", return_value=None), \
                mock.patch.object(models, "embed", side_effect=self.embed):
            return ms.bench_writers(self.p, cells=1, batch=4, writers=[self.A, self.B], **kw)

    @property
    def rate(self):
        from sampo import moons
        return moons.RATE_SYSTEM

    def row(self, r, label, stage):
        return next(x for x in r["rows"] if (x["writer"], x["stage"]) == (label, stage))

    def test_each_writer_runs_the_same_cells_and_reports_kept_per_call_per_stage(self):
        r = self.bench(bad={"haiku": 2})
        w, h = self.row(r, "27b", "write"), self.row(r, "haiku", "write")
        self.assertEqual((w["calls"], w["rows"], w["kept"], w["per_call"]), (1, 4, 4, 4.0))
        self.assertEqual((h["calls"], h["rows"], h["kept"], h["per_call"]), (1, 4, 2, 2.0))
        self.assertEqual([x["stage"] for x in r["rows"] if x["writer"] == "27b"], ["write", "kill", "tier", "novelty"])
        self.assertEqual(self.row(r, "27b", "kill")["per_call"], 4.0)
        self.assertTrue(all(x["arm"] == "moons-writers" for x in r["rows"]))

    def test_each_stage_is_judged_by_the_writer_under_test_and_nothing_reaches_the_real_store(self):
        self.bench()
        by = {}
        for stage, who in self.asked:
            by.setdefault(who, []).append(stage)
        self.assertEqual(by["p27"], ["write", "kill", "tier", "novelty"])
        self.assertEqual(by["haiku"], ["write", "kill", "tier", "novelty"])
        self.assertEqual(self.ms.rows(self.p), [])
        self.assertEqual(self.ms.fate(self.p), {})

    def test_the_best_per_stage_wins_a_tie_goes_to_the_free_writer_and_the_numbers_land_in_research(self):
        r = self.bench(tier_low=("p27",))
        self.assertEqual({s: v["label"] for s, v in r["best"].items()}, {"write": "27b", "kill": "27b", "tier": "haiku",
                                                                         "novelty": "haiku"})
        with open(os.path.join(self.p.dir, "research", "moons-writers.jsonl")) as f:
            saved = [json.loads(x) for x in f]
        self.assertEqual(saved, r["rows"])
        # T-3122 close: only the writer is taken; a judge that keeps more is laxer, not better, so the fill's judges stay
        self.assertEqual(self.ms.load_picks(self.p), {"write": ("local", "p27")})

    def test_a_local_only_or_sensitive_run_never_picks_a_paid_writer(self):
        self.bench(tier_low=("p27",))
        self.assertEqual(self.ms.load_picks(self.p, local_only=True), {"write": ("local", "p27")})
        self.fm("sensitive", "on")
        self.assertEqual(set(self.ms.load_picks(self.p)), {"write"})

    def test_a_judge_stage_pick_never_reaches_the_fill(self):
        # kept per call rewards the laxest judge: a lenient tier judge would inflate kept moons, not improve them
        os.makedirs(self.ms._dir(self.p), exist_ok=True)
        with open(os.path.join(self.ms._dir(self.p), self.ms.WRITERS_FILE), "w") as f:
            json.dump({"stages": {"write": {"writer": "haiku"}, "tier": {"writer": "haiku"}, "kill": {"writer": "haiku"},
                                  "novelty": {"writer": "haiku"}}}, f)
        self.assertEqual(self.ms.load_picks(self.p), {"write": ("haiku", None)})

    def test_a_writer_that_fails_is_a_row_not_the_end_of_the_run(self):
        from sampo import models
        real = self.ms.LocalOnly

        def ask(p, system, prompt, writer=None, profile=None, **kw):
            if profile == "p27":
                raise real("down")
            return reply(1), "haiku"
        with mock.patch.object(self.ms, "_ask", side_effect=ask), mock.patch.object(self.ms, "_embedder", return_value=None), \
                mock.patch.object(models, "embed", side_effect=self.embed):
            r = self.ms.bench_writers(self.p, cells=1, batch=2, writers=[self.A, self.B])
        self.assertTrue(self.row(r, "27b", "write")["error"])
        self.assertEqual(self.row(r, "27b", "write")["kept"], 0)
        self.assertNotIn("27b", {v["label"] for v in r["best"].values()})

    def test_max_calls_caps_the_whole_bench(self):
        r = self.bench(max_calls=3)
        self.assertLessEqual(len(self.asked), 3)
        self.assertLessEqual(sum(x["calls"] for x in r["rows"]), 3)

    def test_the_fill_uses_the_picked_writer_and_keeps_its_own_judges(self):
        self.bench(tier_low=("p27",))
        seen = []

        def ask(p, system, prompt, cheap=True, paid=0, feature=None, writer=None, profile=None):
            seen.append((system, writer, profile))
            return reply(2), "jarvis"
        with mock.patch.object(self.ms, "_ask", side_effect=ask):
            self.ms.grow(self.p, target=99, batch=2, max_calls=1, workers=1)
        self.assertEqual(seen[0][1:], ("local", "p27"))
        judged = Judge(self.ms)
        kws = []

        def judge(p, system, prompt, **kw):
            kws.append((judged.route[next(k for k in judged.route if system.startswith(k))][0], kw.get("writer"), kw.get("profile")))
            return judged(p, system, prompt, **kw)
        self.seed(DEPLOY)
        self.go(judge)
        self.assertTrue(kws)
        self.assertTrue(all(k[1:] == (None, None) for k in kws), kws)  # T-3122 close: no bench pick reaches a judge
        kws.clear()
        self.go(judge, local_only=True)
        self.assertTrue(all(k[1] != "haiku" for k in kws))

    def test_the_verb_with_no_calls_allowed_runs_nothing(self):
        r = self.fm_json("moons", "bench-writers", "--max-calls", "0")
        self.assertEqual(r["rows"], [])


class Pace(_Moons):
    """The fill's kept per hour and the projected finish (T-3122)."""

    NOW = 1_800_000_000

    def log(self, hours_ago):
        d = self.ms._dir(self.p)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, self.ms.FUNNEL), "a") as f:
            for n, h in enumerate(hours_ago):
                e = {"id": f"C-{n + 1:06d}", "at": self.c.iso(self.NOW - h * 3600), "stage": "novelty", "verdict": "kept"}
                f.write(json.dumps(e) + "\n")

    def test_the_rate_per_window_and_a_range_of_finish_dates_while_the_rate_moves(self):
        # 10 kept in the last hour, 30 in six, 60 in a day: 10, 5 and 2.5 an hour
        self.log([0.5] * 10 + [3] * 20 + [12] * 30 + [23.9])
        r = self.ms.pace(self.p, now=self.NOW)
        self.assertEqual((r["kept"], r["target"], r["remaining"]), (61, 10000, 9939))
        self.assertEqual([r["per_hour"][k] for k in ("1h", "6h", "24h")], [10.0, 5.0, 2.6])
        self.assertFalse(r["steady"])
        lo, hi = r["finish"]["from"], r["finish"]["to"]
        self.assertLess(lo, hi)
        self.assertEqual(lo, self.c.iso(self.NOW + 9939 / 10 * 3600)[:10])
        self.assertEqual(hi, self.c.iso(self.NOW + 9939 / 2.6 * 3600)[:10])

    def test_one_date_once_the_windows_agree(self):
        self.log([h + 0.01 for h in range(24) for _ in range(5)])  # 5 an hour all day
        r = self.ms.pace(self.p, now=self.NOW)
        self.assertTrue(r["steady"])
        self.assertEqual(r["finish"]["from"], r["finish"]["to"])

    def test_no_kept_moons_no_projection_and_a_finished_fill_projects_nothing(self):
        r = self.ms.pace(self.p, now=self.NOW)
        self.assertEqual((r["kept"], r["finish"], r["per_hour"]["24h"]), (0, None, 0.0))
        with mock.patch.object(self.ms, "TARGET", 3):
            self.log([1, 2, 3, 4])
            self.assertIsNone(self.ms.pace(self.p, now=self.NOW)["finish"])
            self.assertEqual(self.ms.pace(self.p, now=self.NOW)["remaining"], 0)

    def test_a_young_fill_is_not_averaged_over_a_day_it_has_not_run(self):
        self.log([0.1, 0.2, 0.3, 0.4])  # four moons in the first 24 minutes
        r = self.ms.pace(self.p, now=self.NOW)
        self.assertGreater(r["per_hour"]["24h"], 4.0)


# T-3310: more and better ideas per hour -------------------------------------------------------------------------

CELL = {"problem": "slow tests", "source": "pain", "kind": "bugs", "mechanism": "biology", "scale": "file",
        "surface": "terminal"}


def profs(*named):
    """Fake local profiles, (name, slots) each; patched over _locals, so no endpoint is ever called."""
    return [dict(PROFILE, name=n, slots=s) for n, s in named]


class ConcurrentWrites(_Funnel):
    """The rival read's first test: two writers at once, the store lock held only around add(), still never a duplicate
    id or a torn row, and the exact stage drops the same-titled second."""

    def race(self, n_threads, per, title=lambda t, i: FLAKY["title"]):
        import threading
        gate, errs, out = threading.Barrier(n_threads, timeout=5), [], []

        def work(t):
            try:
                gate.wait()
                objs = [good(title=title(t, i)) for i in range(per)]
                out.extend(self.ms.add(self.p, objs, dict(CELL), "haiku", t + 1))
            except Exception as e:  # noqa: BLE001 - the test reports whatever the thread hit
                errs.append(repr(e))
        ts = [threading.Thread(target=work, args=(t,)) for t in range(n_threads)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(10)
        return out, errs

    def test_two_threads_adding_the_same_moon_both_land_whole_and_exact_drops_the_second(self):
        out, errs = self.race(2, 1)
        self.assertEqual(errs, [])
        self.assertEqual(sorted(i for i, _, _ in out), ["C-000001", "C-000002"])
        self.assertEqual([r["id"] for r in self.ms.rows(self.p)], ["C-000001", "C-000002"])
        self.go(stage="exact")
        self.assertEqual(sorted(v for _, v in self.fates().values()), ["dup", "kept"])

    def test_many_writers_get_unique_contiguous_ids_and_one_funnel_line_per_row(self):
        out, errs = self.race(6, 3, title=lambda t, i: f"Moon {t} number {i}")
        self.assertEqual(errs, [])
        want = [f"C-{n:06d}" for n in range(1, 19)]
        self.assertEqual([r["id"] for r in self.ms.rows(self.p)], want)
        self.assertEqual(sorted(i for i, _, _ in out), want)
        self.assertEqual(len(self.events()), 18)
        self.assertEqual({r["batch"] for r in self.ms.rows(self.p)}, set(range(1, 7)))

    def test_add_waits_for_a_store_another_holds_and_gives_the_row_when_it_frees(self):
        import threading
        done = []
        with self.ms.store_lock(self.p):  # another run holds it for a moment
            t = threading.Thread(target=lambda: done.append(self.ms.add(self.p, [FLAKY], dict(CELL), "haiku", 1, wait=3)))
            t.start()
            t.join(0.3)
            self.assertEqual((done, self.ms.rows(self.p)), ([], []), "the writer is waiting, nothing half-written")
        t.join(5)
        self.assertEqual([v for _, v, _ in done[0]], ["kept"])
        self.assertEqual(len(self.ms.rows(self.p)), 1)


class ParallelEndpoints(_Funnel):
    """T-3310: grow runs the local GPU model, Jarvis and Haiku at once, each at its own slot count."""

    def run_grow(self, local, fake, **kw):
        kw = {"batch": 2, "max_calls": 5, **kw}
        with mock.patch.object(self.ms, "_locals", side_effect=lambda s: iter(local)), \
                mock.patch.object(self.ms, "_ask", side_effect=fake):
            return self.ms.grow(self.p, 1000, **kw)

    @staticmethod
    def model_of(kw):
        return kw["profile"] if kw.get("writer") == "local" else "haiku"

    def test_every_endpoint_runs_at_once_each_at_its_own_slot_count(self):
        import threading
        gate, seen, paid = threading.Barrier(5, timeout=5), [], []

        def fake(p, system, prompt, cheap=True, paid=0, **kw):
            gate.wait()  # BrokenBarrierError unless all five calls are in flight together
            seen.append((kw.get("writer"), kw.get("profile")))
            if kw.get("writer") == "haiku":
                paids.append(paid)
            return reply(2), self.model_of(kw)
        paids = paid
        r = self.run_grow(profs(("gpu", 2), ("jarvis", 1)), fake)
        self.assertEqual(sorted(seen, key=str), sorted([("local", "gpu")] * 2 + [("local", "jarvis")] + [("haiku", None)] * 2,
                                                       key=str))
        self.assertEqual((r["calls"], r["added"], r["models"]), (5, 10, {"gpu": 2, "jarvis": 1, "haiku": 2}))
        self.assertEqual(r["workers"], 5, "the run's slots are the endpoints' slots together")
        self.assertEqual(sorted(paid), [0, 1], "each Haiku call is told how many paid calls were in flight before it")
        self.assertEqual([x["id"] for x in self.ms.rows(self.p)], [f"C-{n:06d}" for n in range(1, 11)])

    def test_the_store_lock_is_held_only_around_writes_not_while_a_batch_is_generated(self):
        import threading
        took, one = [], threading.Lock()  # the workers take turns, so only grow's own hold could make one Busy

        def fake(p, system, prompt, cheap=True, paid=0, **kw):
            with one, self.ms.store_lock(self.p):  # a worker thread: Busy under a lock grow held for the whole run
                took.append(1)
            return reply(2), "gpu"
        r = self.run_grow(profs(("gpu", 1)), fake, max_calls=2, workers=1)
        self.assertEqual((len(took), r["added"], r["stopped"]), (2, 4, "max_calls"))

    def test_grow_still_refuses_a_store_another_funnel_holds_at_the_start(self):
        import threading
        held, done = threading.Event(), threading.Event()

        def other():  # another funnel, from its own thread: the lock is re-entrant per thread only (T-3081)
            with self.ms.store_lock(self.p):
                held.set()
                done.wait(30)
        t = threading.Thread(target=other)
        t.start()
        try:
            held.wait(30)
            r = self.run_grow(profs(("gpu", 1)), lambda *a, **k: (reply(2), "gpu"), max_calls=1)
        finally:
            done.set()
            t.join()
        self.assertEqual((r["stopped"], r["calls"]), ("busy", 0))

    def test_an_endpoint_that_keeps_failing_retires_after_three_and_the_rest_carry_on(self):
        import threading
        spent, haiku = threading.Event(), []

        def fake(p, system, prompt, cheap=True, paid=0, **kw):
            if kw.get("writer") == "haiku":
                haiku.append(1)
                if len(haiku) >= 3:
                    spent.set()
                raise ValueError("haiku is down")
            spent.wait(5)  # the GPU holds its answer until Haiku has used its three tries
            return reply(2), "gpu"
        r = self.run_grow(profs(("gpu", 1)), fake, max_calls=8)
        self.assertEqual(len(haiku), 3, "an endpoint stops at FAILS failures in a row, the others don't")
        self.assertEqual((r["stopped"], r["models"], r["added"]), ("max_calls", {"gpu": 5}, 10))

    def test_the_run_stops_failed_once_every_endpoint_has_retired(self):
        def fake(p, system, prompt, cheap=True, paid=0, **kw):
            raise ValueError("down")
        r = self.run_grow(profs(("gpu", 1)), fake, max_calls=50)
        self.assertEqual((r["stopped"], r["added"]), ("failed", 0))
        self.assertEqual(r["calls"], 3 + 3, "three tries for the GPU endpoint, three for the Haiku one, however many slots it has")

    def test_local_only_and_a_sensitive_project_get_no_haiku_endpoint(self):
        for label in ("local-only", "sensitive"):
            with self.subTest(label):
                seen = []

                def fake(p, system, prompt, cheap=True, paid=0, **kw):
                    seen.append(kw.get("writer"))
                    return reply(2), "gpu"
                if label == "sensitive":
                    self.fm("sensitive", "on")
                self.run_grow(profs(("gpu", 1), ("jarvis", 1)), fake, max_calls=4, local_only=label == "local-only")
                self.assertEqual(set(seen), {"local"})
                self.assertEqual(len(seen), 4)

    def test_an_explicit_workers_count_keeps_the_one_pool_it_always_meant(self):
        seen = []

        def fake(p, system, prompt, cheap=True, paid=0, **kw):
            seen.append(kw)
            return reply(2), "gpu"
        r = self.run_grow(profs(("gpu", 1), ("jarvis", 1)), fake, max_calls=3, workers=2)
        self.assertEqual((seen, r["workers"]), ([{}, {}, {}], 2))

    def test_the_batch_time_is_booked_on_each_row(self):
        self.run_grow(profs(("gpu", 1)), lambda *a, **k: (reply(2), "gpu"), max_calls=1)
        for row in self.ms.rows(self.p):
            self.assertIsInstance(row["secs"], float)
            self.assertGreaterEqual(row["secs"], 0)
        self.assertNotIn("secs", self.ms.add(self.p, [good(title="Other")], dict(CELL), "haiku", 9) and
                         self.ms.rows(self.p)[-1], "a caller that timed nothing books nothing")


class Variety(_Funnel):
    """T-3310: each batch prompt names the kept set's over-used openers and phrases (learned, never a fixed list) and
    carries 3 kept moons from distant cells as anchors."""

    def put(self, title, **cell):
        c = dict(CELL, **cell)
        return self.ms.add(self.p, [good(title=title)], c, "haiku", 1)[0][0]

    def keep(self, *ids):
        self.ms._log(self.p, [self.ms._event(i, "novelty", "kept", tier="high", wild=3) for i in ids],
                     {"stages": {"novelty": {}}}, "novelty")

    def avoid(self):
        return self.ms._avoid(self.ms.rows(self.p), self.ms.fate(self.p))

    def test_the_avoid_list_comes_from_the_kept_titles(self):
        self.keep(*[self.put(t) for t in ("Catch a flaky test without waking anyone", "Catch a leaked secret without waking anyone",
                                          "Catch a slow build without waking anyone", "Sort every inbox by risk")])
        text = self.avoid()
        self.assertIn("catch (3)", text)
        self.assertIn("waking anyone (3)", text)
        self.assertNotIn("inbox", text, "a title used once is not over-used")
        self.assertIn("data", text)

    def test_other_titles_give_another_list_so_nothing_is_fixed(self):
        self.keep(*[self.put(t) for t in ("Zork the plan before you start", "Zork the build after it fails",
                                          "Zork the queue each morning")])
        text = self.avoid()
        self.assertIn("zork (3)", text)
        self.assertNotIn("catch", text)

    def test_nothing_repeated_nothing_to_avoid_and_only_kept_moons_count(self):
        self.assertEqual(self.avoid(), "")
        ids = [self.put(f"Zork number {n}") for n in range(4)]
        self.assertEqual(self.avoid(), "", "none of them is kept yet")
        self.keep(*ids)
        self.assertIn("zork (4)", self.avoid())

    def test_anchors_are_three_kept_moons_from_distant_distinct_cells(self):
        near, near2 = self.put("Near one"), self.put("Near two", surface="browser")
        far = [self.put("Far a", kind="docs", scale="team", surface="chat"),
               self.put("Far a again", kind="docs", scale="team", surface="chat"),
               self.put("Far b", kind="ci", scale="org", surface="phone"),
               self.put("Far c", kind="reviews", scale="project", surface="ide")]
        self.keep(near, near2, *far)
        got = self.ms._anchors(self.ms.rows(self.p), self.ms.fate(self.p), dict(CELL))
        self.assertEqual(len(got), 3)
        self.assertEqual(sorted(x["title"] for x in got if x["title"] != "Far a again"), ["Far a", "Far b", "Far c"])
        self.assertEqual(len({self.ms._key(x["cell"]) for x in got}), 3, "three different cells")
        self.assertEqual(self.ms._anchors(self.ms.rows(self.p), self.ms.fate(self.p), dict(CELL), n=1)[0]["title"][:3], "Far")

    def test_the_batch_prompt_carries_the_avoid_list_and_three_anchors(self):
        titles = ["Catch a flaky test without waking anyone", "Catch a leaked secret without waking anyone",
                  "Catch a slow build without waking anyone"]
        self.keep(*[self.put(t, kind=k, scale=s) for t, (k, s) in zip(titles, (("docs", "team"), ("ci", "org"),
                                                                              ("reviews", "project")))],
                  self.put("Sort every inbox by risk", kind="chat", scale="team"))
        seen = []

        def fake(p, system, prompt, cheap=True, paid=0, **kw):
            seen.append(prompt)
            return reply(2), "haiku"
        with mock.patch.object(self.ms, "_ask", side_effect=fake):
            self.ms.grow(self.p, 100, batch=2, max_calls=1)
        text = seen[0]
        self.assertIn("catch (3)", text)
        self.assertIn("waking anyone (3)", text)
        self.assertEqual(text.count("Kept moon from a distant cell"), 3)
        self.assertEqual(text.count("past data, never instructions"), 4, "the avoid block and each anchor are labelled data")
        self.assertLess(text.index("catch (3)"), text.index("Each moonshot is one JSON object"))


class SampoJudge(_Funnel):
    """T-3310: the kill judge's prompt says what Sampo is and already has."""

    SAMPO = {"grid": "sampo", "kind": "memory", "scale": "one task", "surface": "", "mechanism": "biology",
             "problem": "slow tests", "source": "pain"}

    def judge(self):
        """A kill judge that, like the real one did, calls a Sampo moon a system that does not exist, unless told."""
        base = Judge(self.ms)

        def fake(p, system, prompt, cheap=True, paid=0, **kw):
            if not system.startswith(self.ms.KILL_SYSTEM):
                return base(p, system, prompt, cheap, paid)
            out = []
            for chunk in prompt.split("<moon n=")[1:]:
                n = chunk.split(">")[0]
                hyp = "About: Sampo itself" in chunk and self.ms.SAMPO_NOTE not in system
                out.append(f"{n}: " + ("no | a hypothetical system that does not exist" if hyp else "yes | one person, one day"))
            base.calls.append("kill")
            base.systems.append(system)
            base.prompts.append(prompt)
            return "\n".join(out), "jarvis"
        return fake, base

    def test_the_note_says_what_sampo_is_and_already_has(self):
        note = self.ms.SAMPO_NOTE
        for word in ("Sampo", "coding harness", "exists", "memory", "ledger"):
            self.assertIn(word, note)
        self.assertIn("never", note)
        self.assertIn("hypothetical", note)

    def test_a_sampo_moon_is_judged_with_the_note_and_the_marker_and_survives_the_kill(self):
        mine, theirs = [i for i, _, _ in self.ms.add(self.p, [DEPLOY], dict(self.SAMPO), "haiku", 1)] + \
                       self.seed(REVIEW)
        fake, base = self.judge()
        with mock.patch.object(self.ms, "_ask", side_effect=fake), \
                mock.patch.object(self.ms, "_embedder", return_value=None):
            self.ms.funnel(self.p, stage="exact")
            self.ms.funnel(self.p, stage="embed")
            self.ms.funnel(self.p, stage="kill")
        self.assertEqual(self.fates()[mine], ("kill", "kept"))
        self.assertEqual(self.fates()[theirs], ("kill", "kept"))
        self.assertIn(self.ms.SAMPO_NOTE, base.systems[0])
        self.assertTrue(base.systems[0].startswith(self.ms.KILL_SYSTEM), "the rule comes first, the note after it")
        mine_chunk = next(c for c in base.prompts[0].split("<moon n=")[1:] if DEPLOY["title"] in c)
        their_chunk = next(c for c in base.prompts[0].split("<moon n=")[1:] if REVIEW["title"] in c)
        self.assertIn("About: Sampo itself", mine_chunk)
        self.assertNotIn("About: Sampo itself", their_chunk)

    def test_only_the_kill_judge_gets_the_note(self):
        self.seed(FLAKY)
        judge = Judge(self.ms)
        self.go(judge)
        for system, calls in zip(judge.systems, judge.calls):
            self.assertEqual(self.ms.SAMPO_NOTE in system, calls == "kill", calls)


class YieldRouting(_Funnel):
    """T-3310: `sampo moons yield` shows kept per hour and survival per generator model; grow routes more batches to the
    model with the best kept per hour."""

    n = 0

    def setUp(self):
        super().setUp()
        patch = mock.patch.object(self.ms, "YIELD_MIN", 1)  # these fixtures judge a handful of rows; GrowHeld tests the default
        patch.start()
        self.addCleanup(patch.stop)

    def fill(self, model, kept, dead, secs):
        """`kept + dead` rows from `model` whose batch took `secs`, judged: kept at novelty or rejected at kill."""
        YieldRouting.n += 1
        out = self.ms.add(self.p, [good(title=f"{model} moon {YieldRouting.n} {i}") for i in range(kept + dead)], dict(CELL),
                          model, YieldRouting.n, secs=secs)
        ids = [i for i, _, _ in out]
        self.ms._log(self.p, [self.ms._event(i, "novelty", "kept") for i in ids[:kept]], {"stages": {"novelty": {}}}, "novelty")
        self.ms._log(self.p, [self.ms._event(i, "kill", "rejected", "kill: no") for i in ids[kept:]],
                     {"stages": {"kill": {}}}, "kill")

    def test_kept_per_hour_and_survival_per_model_from_the_decided_rows_only(self):
        self.fill("gpu", 5, 5, 60)
        self.fill("haiku", 1, 3, 40)
        self.ms.add(self.p, [good(title="Pending a"), good(title="Pending b")], dict(CELL), "gpu", 99, secs=100)
        y = self.ms.model_yield(self.p)["models"]
        self.assertEqual((y["gpu"]["rows"], y["gpu"]["decided"], y["gpu"]["kept"]), (12, 10, 5))
        self.assertEqual((y["gpu"]["survival"], y["gpu"]["kept_per_hour"]), (0.5, 300.0))
        self.assertEqual((y["haiku"]["decided"], y["haiku"]["kept"], y["haiku"]["survival"], y["haiku"]["kept_per_hour"]),
                         (4, 1, 0.25, 90.0))
        self.assertEqual(self.ms.model_yield(self.p)["best"], "gpu")

    def test_a_think_variant_counts_toward_its_model_and_a_row_with_no_time_has_no_rate(self):
        self.fill("gpu+think", 2, 0, 20)
        self.fill("gpu+nothink", 1, 1, 20)
        self.fill("old", 1, 1, 0)
        y = self.ms.model_yield(self.p)["models"]
        self.assertEqual((y["gpu"]["kept"], y["gpu"]["decided"], y["gpu"]["kept_per_hour"]), (3, 4, 270.0))
        self.assertIsNone(y["old"]["kept_per_hour"])
        self.assertEqual(self.ms.model_yield(self.p)["best"], "gpu")

    def test_the_yield_verb_prints_each_model_through_the_real_parser(self):
        self.assertEqual(self.fm_json("moons", "yield")["models"], {})
        self.assertIn("no decided moons", self.fm("moons", "yield").stdout.lower())
        self.fill("gpu", 5, 5, 60)
        self.fill("haiku", 1, 3, 40)
        r = self.fm_json("moons", "yield")
        self.assertEqual((r["best"], r["models"]["gpu"]["kept_per_hour"], r["models"]["haiku"]["survival"]), ("gpu", 300.0, 0.25))
        text = self.fm("moons", "yield").stdout
        self.assertIn("gpu: 300.0 kept/hour", text)
        self.assertIn("50% survive", text)
        self.assertIn("haiku: 90.0 kept/hour", text)

    def test_grow_sends_more_batches_to_the_model_with_the_best_kept_per_hour(self):
        import threading

        def trio():
            gate, got = threading.Barrier(3, timeout=5), []

            def fake(p, system, prompt, cheap=True, paid=0, **kw):
                gate.wait()  # all three dispatched before any answers: the routing alone picks who gets them
                got.append(1)
                return reply(2), ParallelEndpoints.model_of(kw)
            with mock.patch.object(self.ms, "_locals", side_effect=lambda s: iter(profs(("gpu", 2), ("jarvis", 2)))), \
                    mock.patch.object(self.ms, "_ask", side_effect=fake):
                return self.ms.grow(self.p, 1000, batch=2, max_calls=3)["models"]
        self.assertEqual(trio(), {"gpu": 1, "jarvis": 1, "haiku": 1}, "no yield known: an even share, in order")
        self.fill("gpu", 5, 5, 60)      # 300 kept an hour
        self.fill("haiku", 1, 3, 40)    # 90
        self.fill("jarvis", 1, 9, 120)  # 30
        self.assertEqual(trio(), {"gpu": 2, "haiku": 1}, "the best model's two slots fill first, then the next best")


class LowPool(_Funnel):
    """T-3310: a low or medium idea unique among the kept moons is pooled, beside the wild ones; the 10,000 count stays
    the wild tier."""

    def test_a_unique_low_or_medium_moon_is_pooled_and_never_reaches_novelty(self):
        ids = self.seed(FLAKY, SECRET, DEPLOY)
        judge = Judge(self.ms, tier={SECRET["title"]: "low | wild 1 | a check", DEPLOY["title"]: "medium | wild 2 | a rollback"})
        self.go(judge)
        f = self.ms.fate(self.p)
        self.assertEqual(self.fates(), {ids[0]: ("novelty", "kept"), ids[1]: ("tier", "pooled"), ids[2]: ("tier", "pooled")})
        self.assertEqual((f[ids[1]]["tier"], f[ids[1]]["wild"], f[ids[1]]["reason"]), ("low", 1, "tier: low, kept in the pool"))
        self.assertEqual(f[ids[2]]["tier"], "medium")
        novelty = [pr for s, pr in zip(judge.systems, judge.prompts) if s.startswith(self.ms.NOVEL_SYSTEM)]
        self.assertEqual(len(novelty), 1)
        self.assertNotIn(SECRET["title"], novelty[0])
        self.assertNotIn(DEPLOY["title"], novelty[0])

    def test_the_10000_count_stays_the_wild_tier_and_the_pool_is_its_own_list(self):
        ids = self.seed(FLAKY, SECRET, DEPLOY)
        self.go(Judge(self.ms, tier={SECRET["title"]: "low | wild 1 | a check", DEPLOY["title"]: "medium | wild 2 | a rollback"}))
        self.assertEqual(self.ms.survivors(self.p), 1)
        self.assertEqual([r["id"] for r in self.ms.pool(self.p)], ids[1:])
        s = self.ms.status(self.p)
        self.assertEqual(s["lowpool"], 2)
        self.assertIn("2 in the low pool", self.ms.status_line(s))
        self.assertEqual(self.ms.coverage(self.p)["pooled"], 2)
        self.assertEqual(self.ms.export(self.p)["funnel"]["pooled"], 2)
        self.assertEqual(self.ms.export(self.p)["families_total"], 1, "families are the wild survivors")

    def test_a_low_moon_near_a_kept_one_is_still_merged_into_it(self):
        ids = self.seed(REVIEW, NUDGE)
        self.go(Judge(self.ms, tier={NUDGE["title"]: "low | wild 1 | a reminder"}))
        f = self.ms.fate(self.p)
        self.assertEqual((f[ids[1]]["verdict"], f[ids[1]]["merged_into"]), ("merged", ids[0]))

    def test_two_look_alike_lows_in_one_batch_pool_the_first_and_merge_the_second(self):
        ids = self.seed(REVIEW, NUDGE)
        self.go(Judge(self.ms, tier={REVIEW["title"]: "low | wild 1 | a check", NUDGE["title"]: "low | wild 1 | a reminder"}))
        f = self.ms.fate(self.p)
        self.assertEqual((f[ids[0]]["verdict"], f[ids[1]]["verdict"], f[ids[1]]["merged_into"]), ("pooled", "merged", ids[0]))

    def test_a_pooled_moon_is_not_judged_again_on_a_rerun(self):
        self.seed(FLAKY, SECRET)
        self.go(Judge(self.ms, tier={SECRET["title"]: "low | wild 1 | a check"}))
        before = self.events()
        r, judge = self.go()
        self.assertEqual((r["calls"], judge.calls, self.events()), (0, [], before))

    def test_the_pool_and_kept_are_embedded_once_per_run_not_once_per_batch(self):
        seeds = [FLAKY, DEPLOY, REVIEW, SECRET]  # four orthogonal fake vectors: none is a dup of another
        self.seed(*seeds)
        for stage in ("exact", "embed", "kill"):
            self.go(stage=stage)
        calls = []
        real = self.ms._reps

        def count(p, rs, extra=None):
            calls.append(len(rs))
            return real(p, rs, extra)
        low = {x["title"]: "low | wild 1 | a check" for x in seeds}
        with mock.patch.object(self.ms, "JUDGE_BATCH", 1), mock.patch.object(self.ms, "_reps", side_effect=count):
            self.go(Judge(self.ms, tier=low), stage="tier")
        self.assertEqual(len(calls), 1, "one representation build for the four one-row batches")
        self.assertEqual(calls[0], 4)


class GrowHeld(_Funnel):
    """T-3310 review: a store held past the write wait loses no generated batch that can still be written; a second grow
    on the same store refuses; a model with few judged rows is unknown, not worst."""

    def hold(self, release):
        """Another funnel, from its own thread, holding the store until `release` is set; returns once it holds it."""
        import threading
        held = threading.Event()

        def other():
            with self.ms.store_lock(self.p):
                held.set()
                release.wait(30)
        t = threading.Thread(target=other)
        t.start()
        held.wait(30)
        return t

    def grow_three(self, release, hold_for=None, **kw):
        """grow with 3 calls at once; the store is taken by another thread when the first call starts, and freed after
        `hold_for` seconds (None: when grow returns)."""
        import threading
        gate, taken, box = threading.Barrier(3, timeout=10), threading.Lock(), []

        def fake(p, system, prompt, cheap=True, paid=0, **kw):
            with taken:
                if not box:
                    box.append(self.hold(release))
                    if hold_for is not None:
                        threading.Timer(hold_for, release.set).start()
            gate.wait()
            return reply(2), "gpu"
        with mock.patch.object(self.ms, "_locals", side_effect=lambda s: iter(profs(("gpu", 3)))), \
                mock.patch.object(self.ms, "_ask", side_effect=fake), \
                mock.patch.object(self.ms, "WRITE_WAIT", 0.2), mock.patch.object(self.ms, "DRAIN_WAIT", 0.2):
            r = self.ms.grow(self.p, 1000, batch=2, max_calls=3, **kw)
        release.set()
        for t in box:
            t.join(10)
        return r

    def test_a_store_held_past_the_write_wait_loses_no_batch_when_it_frees_within_the_deadline(self):
        import threading
        with mock.patch.object(self.ms, "WRITE_DEADLINE", 30.0):
            r = self.grow_three(threading.Event(), hold_for=1.0)
        self.assertEqual((r["stopped"], r["added"], r["calls"], r["lost"]), ("max_calls", 6, 3, 0))
        self.assertEqual(len(self.ms.rows(self.p)), 6)

    def test_a_store_that_stays_held_stops_the_run_and_counts_every_batch_not_written(self):
        import threading
        with mock.patch.object(self.ms, "WRITE_DEADLINE", 0.5):
            r = self.grow_three(threading.Event())
        self.assertEqual((r["stopped"], r["lost"], r["added"]), ("busy", 3, 0))
        self.assertIn("3 batch(es) not written", self.ms.grow_line(r))
        self.assertEqual(self.ms.rows(self.p), [])

    def test_a_second_grow_on_the_same_store_says_one_is_running_and_calls_nothing(self):
        import threading
        release, calls = threading.Event(), []
        held, started = threading.Event(), threading.Event()

        def first():
            with self.ms.grow_lock(self.p):
                held.set()
                release.wait(30)
        t = threading.Thread(target=first)
        t.start()
        held.wait(30)
        try:
            r = self.ms.grow(self.p, 10, batch=2, max_calls=1)
        finally:
            release.set()
            t.join()
        self.assertEqual((r["stopped"], r["calls"]), ("busy", 0))
        self.assertIn("grow is running", r["error"])
        with mock.patch.object(self.ms, "_ask", side_effect=lambda *a, **k: (calls.append(1), (reply(2), "gpu"))[1]):
            self.assertEqual(self.ms.grow(self.p, 10, batch=2, max_calls=1)["added"], 2, "the lock is released at the end")
        self.assertEqual(calls, [1])

    def test_a_model_with_few_judged_rows_is_unknown_and_gets_the_mean_not_the_floor(self):
        y = YieldRouting
        for model, kept, dead, secs in (("gpu", 5, 5, 60), ("new", 0, 3, 30)):
            y.fill(self, model, kept, dead, secs)
        r = self.ms.model_yield(self.p)
        self.assertEqual((r["models"]["new"]["kept_per_hour"], r["models"]["new"]["rated"]), (0.0, False),
                         "its own numbers stay visible")
        self.assertEqual(r["best"], "gpu")
        eps = [{"name": "gpu"}, {"name": "new"}]
        self.ms._weigh(eps, r["models"])
        self.assertEqual([e["weight"] for e in eps], [300.0, 300.0], "three rejected rows say nothing yet")


NETM = moon("Watch your projects as one network of links", "see how your projects depend on each other.",
            "spot the project that blocks five others.")
PROGM = moon("Watch the progress of every running task as it happens", "see what each running task is doing.",
             "know which task is stuck before it stalls.")
# the fake embedding for the misses: the first keyword picks the axis, anything else the fourth axis (cosine 0 apart)
MVEC = (("kalevala", (0, 0, 0, 1)), ("network", (1, 0, 0, 0)), ("progress", (0, 1, 0, 0)))
T_3034 = "A network view of every project and how they link"
T_3044 = "A progress view of what every task is doing right now"
T_3050 = "Kalevala: many Rune towers at once on every aspect of Sampo, overseen by Vainamoinen, and evolving its own setup"


class Misses(_Funnel):
    """T-3051: the user's feature asks, matched against the kept moons: an ask no earlier moon came near is a miss, and a
    miss feeds the generator's prompt, the Sampo grid and a rate per generation."""

    def embed(self, profile, texts, timeout=120):
        self.embeds.append(list(texts))
        return [list(next((v for k, v in MVEC if k in t.lower()), (0, 0, 1, 0))) for t in texts]

    def setUp(self):
        super().setUp()
        from sampo import cli
        self.cli = cli
        self.ask(T_3034, "2026-11-02T00:00:00Z")
        self.keep(NETM, "2026-11-01T00:00:00Z")
        self.keep(PROGM, "2026-11-04T00:00:00Z")  # written after the progress ask: learned, not predicted
        self.ask(T_3044, "2026-11-03T00:00:00Z")
        self.ask(T_3050, "2026-11-05T00:00:00Z")

    def ask(self, title, at, type="FEATURE", source="user"):
        return self.cli._create(self.p, title, type, "S", "planned", raw=title, source=source, net=False,
                                meta={"created": at}).id

    def keep(self, obj, at):
        cell = {"problem": "x", "source": "pain", "kind": "bugs", "mechanism": "law", "scale": "file", "surface": "terminal"}
        with mock.patch.object(self.ms.c, "now", return_value=at):
            mid = self.ms.add(self.p, [obj], cell, "haiku", 1)[0][0]
        self.ms._log(self.p, [self.ms._event(mid, "novelty", "kept", tier="high", wild=3)],
                     {"stages": {"novelty": {}}}, "novelty")
        return mid

    def see(self, fn, local=True, **kw):
        from sampo import models
        with mock.patch.object(self.ms, "_embedder", return_value=dict(PROFILE) if local else None), \
                mock.patch.object(models, "embed", side_effect=self.embed):
            return fn(self.p, **kw)

    def test_the_asks_are_the_users_feature_briefs_since_the_moons_began(self):
        self.ask("A feature asked before any moon", "2026-10-01T00:00:00Z")
        self.ask("A fix the user asked for", "2026-11-05T00:00:00Z", type="FIX")
        self.ask("A feature Sampo captured itself", "2026-11-05T00:00:00Z", source="capture")
        self.assertEqual([a["title"] for a in self.ms.asks(self.p)], [T_3034, T_3044, T_3050])

    def test_an_ask_no_earlier_kept_moon_came_near_is_a_miss_with_its_nearest_moon_and_similarity(self):
        r = self.see(self.ms.misses)
        self.assertEqual((r["method"], r["asks"]), ("embed", 3))
        self.assertEqual([x["title"] for x in r["predicted"]], [T_3034])
        self.assertEqual(r["predicted"][0]["moon_title"], NETM["title"], "the moon that predicted it")
        self.assertEqual(r["predicted"][0]["sim"], 1.0)
        miss = {x["title"]: x for x in r["missed"]}
        self.assertEqual(set(miss), {T_3044, T_3050})
        self.assertEqual((miss[T_3050]["moon_title"], miss[T_3050]["sim"]), (NETM["title"], 0.0),
                         "no moon is near the Kalevala; the nearest is named with its similarity")
        self.assertEqual((miss[T_3044]["moon_title"], miss[T_3044]["sim"], miss[T_3044]["late"]),
                         (PROGM["title"], 1.0, True), "a moon written after the ask learned it, it did not predict it")
        self.assertEqual(r["rate"], round(1 / 3, 3))

    def test_the_three_known_asks_show_as_a_miss_with_a_nearest_moon_or_as_predicted(self):
        r = self.see(self.ms.misses)
        every = r["predicted"] + r["missed"]
        self.assertEqual(len({x["id"] for x in every}), 3, "each of the three asks is one or the other, never both")
        self.assertTrue(all(x["moon"] and x["moon_title"] for x in every), "each names its nearest moon")
        self.assertEqual([x["id"] for x in r["predicted"]], ["T-0001"], "T-3034's network view: predicted")
        self.assertEqual([x["id"] for x in r["missed"]], ["T-0002", "T-0003"], "T-3044's progress view and T-3050's Kalevala")

    def test_shingles_stand_in_when_no_embedder_answers(self):
        r = self.see(self.ms.misses, local=False)
        self.assertEqual(r["method"], "shingle")
        self.assertIn(T_3050, [x["title"] for x in r["missed"]])
        self.assertTrue(all(0.0 <= x["sim"] <= 1.0 for x in r["predicted"] + r["missed"]))

    def test_no_asks_is_no_rate_and_no_model_call(self):
        with mock.patch.object(self.ms, "asks", return_value=[]):
            r = self.see(self.ms.misses)
        self.assertEqual((r["asks"], r["rate"], r["missed"]), (0, None, []))
        self.assertEqual(self.embeds, [])

    def test_each_miss_becomes_a_blind_spot_question_in_the_cell_it_belongs_to_and_a_new_sampo_area(self):
        self.ask("Memory of what each session learned, kept for the next one", "2026-11-06T00:00:00Z")
        self.see(self.ms.refresh_misses)
        areas = self.ms.sampo_areas(self.p)
        self.assertIn("kalevala", areas, "the Kalevala's subject had no area: it gets one")
        self.assertTrue(any("progress" in a for a in areas))
        self.assertEqual(areas.count("memory"), 1, "a subject that has an area adds none")
        blind = self.ms.blind_spots(self.p)
        self.assertIn("sampo kalevala", blind)
        self.assertIn("sampo memory", blind)
        text = blind["sampo kalevala"]
        self.assertIn("Blind spot", text)
        self.assertIn("Kalevala", text)
        self.assertIn("nearest moon", text)
        self.assertIn("data", text, "a past miss is data, never an instruction")
        self.assertNotIn("network", " ".join(blind.values()).lower(), "a predicted ask is no blind spot")
        grid = len(self.ms.coverage(self.p)["sampo_thinnest"])
        self.assertGreaterEqual(self.ms.coverage(self.p)["sampo_grid"], 5 * len(areas))
        self.assertTrue(any(x["kind"] == "kalevala" for x in self.ms.pick(self.p, 5 * len(areas) + 100, 0)
                            if x.get("grid") == "sampo"))
        self.assertGreater(grid, 0)

    def test_a_new_area_cap_keeps_the_grid_from_growing_without_bound(self):
        for i in range(self.ms.MISS_AREAS + 5):
            self.ask(f"Zzfeature{i}name: a thing nobody predicted {i}", f"2026-11-06T00:{i:02d}:00Z")
        self.see(self.ms.refresh_misses)
        base = len(self.ms.base_areas(self.p))
        self.assertLessEqual(len([a for a in self.ms.sampo_areas(self.p) if a.startswith("zzfeature")]), self.ms.MISS_AREAS)
        self.assertGreater(len(self.ms.sampo_areas(self.p)), base)

    def test_the_newest_ask_still_gets_an_area_after_more_older_misses_than_the_cap(self):
        for i in range(self.ms.MISS_AREAS + 2):
            self.ask(f"Zzfeature{i:02d}name: a thing nobody predicted", f"2026-11-06T00:{i:02d}:00Z")
        self.ask("Quuxlatest: the newest ask nobody predicted", "2026-11-07T00:00:00Z")
        r = self.see(self.ms.misses)
        self.assertLessEqual(len(r["areas"]), self.ms.MISS_AREAS)
        self.assertIn("quuxlatest", r["areas"], "the cap keeps the newest subjects, not the oldest")
        self.assertEqual(r["missed"][-1]["area"], "quuxlatest")

    def test_a_shingle_match_predicts_the_network_ask_and_the_method_is_in_the_digest(self):
        r = self.see(self.ms.misses, local=False)
        self.assertEqual(r["method"], "shingle")
        self.assertIn("T-0001", [x["id"] for x in r["predicted"]], "a short ask contained in a long moon is predicted")
        self.assertGreater(r["rate"], 0)
        self.see(self.ms.refresh_misses, local=False)
        d1 = self.ms._stored(self.p)["digest"]
        self.see(self.ms.refresh_misses)
        self.assertNotEqual(self.ms._stored(self.p)["digest"], d1, "an embed run does not reuse a shingle result")
        self.assertEqual(self.ms._stored(self.p)["method"], "embed")

    def test_common_english_alone_never_predicts_an_ask_in_the_shingle_fallback(self):
        # T-3051 review: many moons share filler phrases; a long ask made of them reached the bar against any one moon
        filler = ("you can see what the user has done in the repo and then run the tool to get the result of the "
                  "work in the file so that the team can read it ")
        for n in range(30):
            self.keep(moon(f"Zebra{n} quartz{n} lantern{n}", filler + f"qx{n}wv zk{n}jp.", filler), "2026-11-01T01:00:00Z")
        self.ask(filler + "and then the user can see the result", "2026-11-06T00:00:00Z")
        r = self.see(self.ms.misses, local=False)
        self.assertEqual(r["method"], "shingle")
        self.assertEqual(len(r["predicted"]), 1, "only the network ask is predicted; the filler ask is a miss")
        self.assertTrue(r["approximate"])
        self.assertIn("approximate", self.ms.miss_lines(r))

    def test_a_down_embed_server_is_retried_at_most_once_in_the_back_off(self):
        from sampo import models
        calls = []

        def down(profile, texts, timeout=120):
            calls.append(1)
            raise models.ModelError("down")
        with mock.patch.object(self.ms, "_embedder", return_value=dict(PROFILE)), \
                mock.patch.object(models, "embed", side_effect=down):
            self.ms.refresh_misses(self.p)
            n = len(calls)
            self.assertGreater(n, 0)
            self.ms.refresh_misses(self.p)
            self.ms.refresh_misses(self.p)
        self.assertEqual(len(calls), n, "the stored shingle result stands for the back-off")

    def test_the_status_reads_the_stored_result_and_never_refreshes(self):
        self.assertFalse(os.path.exists(os.path.join(self.ms._dir(self.p), self.ms.MISSES)))
        self.assertIn("Moon misses", self.fm("genesis", "moonshot", "--status").stdout)
        self.assertFalse(os.path.exists(os.path.join(self.ms._dir(self.p), self.ms.MISSES)), "--status wrote misses.json")

    def test_a_refresh_loads_the_briefs_once(self):
        with mock.patch.object(self.ms, "asks", wraps=self.ms.asks) as m:
            self.see(self.ms.refresh_misses, local=False)
        self.assertEqual(m.call_count, 1)

    def test_the_misses_verb_falls_back_to_the_stored_result_on_a_busy_lock(self):
        from sampo import core
        self.see(self.ms.refresh_misses, local=False)
        with mock.patch.object(self.ms, "refresh_misses", side_effect=core.LockTimeout("held")):
            r = self.ms.misses_or_stored(self.p)
        self.assertEqual(r["asks"], 3)

    def test_a_busy_lock_never_aborts_a_best_effort_refresh(self):
        from sampo import core
        with mock.patch.object(self.ms.c, "lock", side_effect=core.LockTimeout("held")):
            self.assertIsNone(self.see(lambda p: self.ms.best_effort(self.ms.refresh_misses, p)))
            self.assertIsNone(self.see(lambda p: self.ms.best_effort(self.ms.record_misses, p, 3)))

    def test_grow_puts_the_blind_spot_into_the_prompt_of_a_cell_of_that_area(self):
        seen = []

        def ask(p, system, prompt, **kw):
            seen.append(prompt)
            return reply(1), "jarvis"
        cell = {"kind": "kalevala", "scale": "one task", "surface": "", "mechanism": "law", "problem": "x",
                "source": "pain", "grid": "sampo"}
        from sampo import models
        with mock.patch.object(self.ms, "_ask", side_effect=ask), mock.patch.object(self.ms, "_pick", return_value=[cell]), \
                mock.patch.object(self.ms, "_embedder", return_value=dict(PROFILE)), \
                mock.patch.object(models, "embed", side_effect=self.embed), \
                mock.patch.object(self.ms, "_locals", side_effect=lambda s: iter([])):
            self.ms.grow(self.p, 1000, batch=1, max_calls=1)
        self.assertEqual(len(seen), 1)
        self.assertIn("Blind spot", seen[0])
        self.assertIn("Kalevala", seen[0])
        self.assertNotIn(T_3034, seen[0])

    def test_a_refresh_with_nothing_new_asks_the_embedder_nothing(self):
        self.see(self.ms.refresh_misses)
        n = len(self.embeds)
        self.see(self.ms.refresh_misses)
        self.assertEqual(len(self.embeds), n, "the stored result stands while the asks and the kept moons are the same")
        self.keep(NETM | {"title": "Another network moon"}, "2026-11-07T00:00:00Z")
        self.see(self.ms.refresh_misses)
        self.assertGreater(len(self.embeds), n)

    def test_the_miss_rate_is_recorded_for_each_generation_and_shown_in_the_status_line(self):
        self.see(self.ms.record_misses, gen=3)
        self.ask("A second miss", "2026-11-08T00:00:00Z")
        self.see(self.ms.record_misses, gen=4)
        self.see(self.ms.record_misses, gen=4)  # a second run of a generation replaces its line
        h = self.ms.miss_history(self.p)
        self.assertEqual([(x["gen"], x["asks"], x["predicted"], x["missed"]) for x in h], [(3, 3, 1, 2), (4, 4, 1, 3)])
        self.assertEqual(h[0]["rate"], round(1 / 3, 3))
        self.assertEqual(h[1]["rate"], 0.25)
        line = self.ms.miss_line(self.p)
        self.assertIn("1 of 4 asks", line)
        self.assertIn("25%", line)
        self.assertIn("gen-3 33%", line)
        self.assertIn("gen-4 25%", line)
        self.assertIn("miss", line.lower())

    def test_the_status_verb_and_the_misses_verb_show_it(self):
        out = self.fm("genesis", "moonshot", "--status").stdout
        self.assertIn("Moon misses", out)
        out = self.fm("genesis", "moonshot", "misses").stdout
        self.assertIn("Moon misses", out)
        self.assertIn("Kalevala", out)
        self.assertIn("nearest", out)
        js = self.fm_json("genesis", "moonshot", "misses")
        self.assertEqual(js["asks"], 3)
