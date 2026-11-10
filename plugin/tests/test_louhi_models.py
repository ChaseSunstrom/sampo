"""T-2887: sampo louhi models, the decision arm (intake type and tier from held-out closed tasks, through systemone
backends) and the embedding arm (title to brief recall). Fake servers only; no live model."""
import collections
import http.server
import json
import os
import tempfile
import threading

from helpers import PRIVATE_TERMS, ForemanTestCase, read_text

from sampo import core as c
from sampo import louhi
from sampo import models

TASKS = [("the parser crashes on an empty file %d", "FIX", "S"), ("add a csv export to the report %d", "FEATURE", "M")]


class Server:
    """Answers /v1/systemone with `choice` at 0.9 (and records every state it saw) and /v1/embeddings with letter counts."""

    def __init__(self, choice="FIX"):
        self.states, self.crits, self.busy, srv = [], [], 0, self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path.endswith("/systemone") and srv.busy > 0:
                    srv.busy -= 1
                    self.send_response(529)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.path.endswith("/systemone"):
                    srv.states.append(body["state"])
                    srv.crits.append(next(iter(body["questions"].values()))["criteria"])
                    crit = list(srv.crits[-1])
                    pick = choice if choice in crit else crit[0]
                    out = {"answers": {"type": {"type": "choice", "choice": pick, "confidence": 0.9,
                                                "probabilities": {k: (0.9 if k == pick else 0.05) for k in crit}}}}
                else:
                    vec = lambda t: [float(t.lower().count(ch)) for ch in "abcdefghijklmnopqrstuvwxyz0123456789"]  # noqa: E731
                    xs = body["input"] if isinstance(body["input"], list) else [body["input"]]
                    out = {"data": [{"index": i, "embedding": vec(t)} for i, t in enumerate(xs)]}
                data = json.dumps(out).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/v1"
        threading.Thread(target=self.srv.serve_forever, args=(0.02,), daemon=True).start()

    def profile(self, name, kind):
        return {"name": name, "kind": kind, "base_url": self.url, "model": name, "key_env": None, "context": 8192,
                "tools": False}

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class ExamChildNoCore(ForemanTestCase):
    def test_the_exam_runs_its_children_with_no_core_dump_limit(self):  # T-3503
        probe = "python3 -c 'import resource,sys; sys.exit(resource.getrlimit(resource.RLIMIT_CORE)[0] != 0)'"
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(louhi._run({"commands": {"exec": probe}}, d, "x"), (True, ""))
            self.assertTrue(louhi._sh(probe, d))


class LouhiModels(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)
        self.truth = {}
        for i in range(12):
            title, typ, tier = TASKS[i % 2]
            tid = json.loads(self.fm("capture", title % i, "--json").stdout)["id"]
            b = c.find_brief(self.p, tid)
            b.meta.update(type=typ, tier=tier, status="done")
            c.save_brief(self.p, b)
            self.truth[tid] = (typ, tier)
        self.held = sorted(t for t in self.truth if louhi.holdout(t))
        self.srv = Server("FIX")
        self.addCleanup(self.srv.close)

    def run_bench(self, **kw):
        return louhi.bench_models(self.p, decision=[self.srv.profile("kev", "systemone")],
                                  embedders=[self.srv.profile("nomic", "openai")], **kw)

    def rows(self):
        with open(os.path.join(self.p.dir, louhi.BENCH)) as f:
            return [json.loads(x) for x in f]

    def test_run_writes_one_row_per_backend_and_use_from_held_out_tasks_only(self):
        self.assertTrue(self.held and len(self.held) < len(self.truth))
        res = self.run_bench()
        rows = self.rows()
        self.assertEqual(len(rows), len(res["rows"]))
        by = {(r["backend"], r["use"]): r for r in rows}
        for use in ("intake-type", "intake-tier"):
            r = by[("kev", use)]
            self.assertEqual((r["arm"], r["n"], r["errors"]), ("decision", len(self.held), 0))
            for k in ("accuracy", "brier", "p50_ms", "p95_ms", "run", "held"):
                self.assertIn(k, r)
            for base in ("majority", "orchestrator"):  # T-2894: two honest baselines, labelled
                self.assertEqual(by[(base, use)]["arm"], "decision")
            self.assertIn("orchestrator's own", by[("orchestrator", use)]["note"])
            self.assertNotIn(("without", use), by)
        train = [self.truth[t][0] for t in self.truth if t not in self.held]
        top = max(sorted(set(train)), key=train.count)
        self.assertEqual(by[("majority", "intake-type")]["accuracy"],
                         round(sum(self.truth[t][0] == top for t in self.held) / len(self.held), 3))
        typ = by[("kev", "intake-type")]
        want = sum(self.truth[t][0] == "FIX" for t in self.held) / len(self.held)
        self.assertAlmostEqual(typ["accuracy"], round(want, 3), places=3)
        self.assertAlmostEqual(typ["brier"], round(sum((0.9 - (self.truth[t][0] == "FIX")) ** 2 for t in self.held)
                                                   / len(self.held), 3), places=3)
        emb = by[("nomic", "title-to-brief")]
        self.assertEqual(emb["arm"], "embedding")
        self.assertEqual(emb["n"], len(self.held))
        self.assertTrue(0 <= emb["recall_at_1"] <= emb["recall_at_10"] <= 1)
        self.assertEqual(emb["recall_at_10"], 1.0)  # fewer than ten held-out briefs: the right one is always in the top ten
        # no held-out label reaches a prompt, and no train task is asked about
        inputs = {louhi._raw(c.find_brief(self.p, t)) for t in self.held}  # T-2894: the briefs' raw requests, no shadow row
        self.assertTrue(self.srv.states and set(self.srv.states) <= inputs)
        self.assertFalse([s for s in self.srv.states if "FEATURE" in s or "FIX" in s.split()])
        # every option carries its description, not its own label
        crits = [x for x in self.srv.crits if "FIX" in x]
        self.assertTrue(crits)
        for crit in self.srv.crits:
            self.assertTrue(all(v != k and len(v) > 12 for k, v in crit.items()), crit)

    def test_retrieval_rows_vote_from_the_nearest_train_requests_and_abstain_when_split(self):
        self.run_bench()
        by = {(r["backend"], r["use"]): r for r in self.rows()}
        for name in ("bm25", "retrieval:nomic"):
            r = by[(name, "intake-type")]
            self.assertEqual((r["arm"], r["n"]), ("retrieval", len(self.held)))
            self.assertEqual(r["answered"] + r["abstained"], r["n"])
            self.assertEqual(r["coverage"], round(r["answered"] / r["n"], 3))
            # T-3068: accuracy has the same denominator as the majority baseline (every brief, an abstain is wrong)
            self.assertLessEqual(r["accuracy"], r["coverage"])
            if name == "bm25":  # the two kinds of request share no words (the fake embedder's letter counts blur them)
                self.assertEqual((r["accuracy"], r["accuracy_answered"]), (r["coverage"], 1.0))
            self.assertIn((name, "intake-tier"), by)
        self.assertEqual(louhi._vote(["FIX"] * 4 + ["FEATURE"] * 3), "FIX")
        self.assertIsNone(louhi._vote(["FIX"] * 3 + ["FEATURE"] * 3))  # split: abstain
        self.run_bench()  # a second run embeds nothing new: the vectors are cached by model and text hash
        with open(os.path.join(self.p.dir, louhi.RETRIEVAL_VECTORS)) as f:
            ids = [(json.loads(x)["id"]) for x in f]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertFalse(os.path.exists(os.path.join(self.p.root, louhi.RETRIEVAL_VECTORS)), "the cache stays out of the repo")

    def test_a_busy_529_is_retried_then_counted(self):
        self.srv.busy = 4  # models._post retries 3 times; the bench retries after it
        louhi.BUSY_WAIT, was = 0, models._sleep
        models._sleep = lambda s: None
        self.addCleanup(setattr, louhi, "BUSY_WAIT", 2)
        self.addCleanup(setattr, models, "_sleep", was)
        self.run_bench()
        r = {(x["backend"], x["use"]): x for x in self.rows()}[("kev", "intake-type")]
        self.assertEqual((r["n"], r["errors"]), (len(self.held), 0))

    def test_the_generation_arm_writes_yield_and_pass_rate_one_endpoint_at_a_time(self):
        a, b = "http://127.0.0.1:1/v1", "http://127.0.0.1:2/v1"
        mk = lambda n, u: {"name": n, "kind": "openai", "base_url": u, "model": n, "key_env": None, "context": 8192}  # noqa: E731
        profs = [mk("b1", b), mk("a1", a), mk("b2", b), mk("a2", a)]
        calls = []

        def grow(prof, k):
            calls.append(("grow", prof["name"], k))
            return {"added": k, "kept": k // 2, "seconds": 30.0, "fallback": 0}

        def funnel(prof, sample):
            calls.append(("funnel", prof["name"], sample))
            return {"kill": {"in": 8, "kept": 6}, "tier": {"in": sample, "kept": 2}}

        def smith(prof):
            calls.append(("smith", prof["name"]))
            return {"closed": 3, "seeds": 4}

        louhi.bench_models(self.p, decision=[], embedders=[], generators=profs, k=16, grow_fn=grow, funnel_fn=funnel,
                           smith_fn=smith)
        order = [x[1] for x in calls]
        self.assertEqual(order, ["a1"] * 3 + ["a2"] * 3 + ["b1"] * 3 + ["b2"] * 3)  # an endpoint's models never alternate
        rows = {(x["backend"], x["use"]): x for x in self.rows() if x["arm"] == "generation"}
        y = rows[("a1", "moons-yield")]
        self.assertEqual((y["k"], y["added"], y["admitted"], y["kill_kept"], y["tier_in"], y["tier_kept"], y["moons_per_min"]),
                         (16, 16, 8, 6, 8, 2, 32.0))  # 16 moons in 30 s
        self.assertIn("sample", y["note"])  # the tier stage takes a sample and says so
        s = rows[("b2", "smith-seeds")]
        self.assertEqual((s["pass_rate"], s["n"]), (0.75, 4))
        self.assertIn("b2", self.fm("exam", "models", "show").stdout)

    def test_no_endpoint_is_named_in_the_code(self):
        src = os.path.join(os.path.dirname(louhi.__file__))
        for f in ("louhi.py", "models.py"):
            with open(os.path.join(src, f)) as fh:
                self.assertFalse(c.has_private(fh.read(), PRIVATE_TERMS), f"{f} names a private term")
        self.assertFalse(hasattr(louhi, "SERVING"))
        profs = [{"base_url": "http://x/v1", "model": "first"}, {"base_url": "http://x/v1", "model": "second"}]
        self.assertEqual(louhi._serving(profs), {"http://x/v1": "first"})

    def test_cap_leaves_rows_out_and_show_says_how_many(self):
        self.run_bench(cap=1)
        r = {(x["backend"], x["use"]): x for x in self.rows()}[("kev", "intake-type")]
        self.assertEqual((r["n"], r["held"]), (1, len(self.held)))
        out = self.fm("exam", "models", "show").stdout
        self.assertIn("kev", out)
        self.assertIn(f"{len(self.held) - 1} held-out left out", out)

    def test_backends_run_grouped_by_endpoint_the_served_model_first(self):
        # a llama-swap endpoint takes ~3 minutes a switch and can't be asked what it holds: ordering, never alternation
        r, o = "https://jarvis.example/llm/v1", "http://127.0.0.1:11434/v1"
        mk = lambda base, model: ("decision", {"base_url": base, "model": model})  # noqa: E731
        jobs = [mk(r, "big-35b"), mk(o, "kev"), mk(r, "qwen3.8-27b"), mk(o, "d3"), mk(r, "big-35b")[0:1] + (
            {"base_url": r, "model": "aaa"},)]
        got = [(j[1]["base_url"], j[1]["model"]) for j in louhi._bench_order(jobs, {r: "qwen3.8-27b"})]
        self.assertEqual(got, [(o, "d3"), (o, "kev"), (r, "qwen3.8-27b"), (r, "aaa"), (r, "big-35b")])

    def test_a_failing_backend_counts_errors_and_a_second_run_appends_a_new_run(self):
        dead = {"name": "gone", "kind": "systemone", "base_url": "http://127.0.0.1:1/v1", "model": "gone", "key_env": None}
        louhi.bench_models(self.p, decision=[dead], embedders=[])
        r = {(x["backend"], x["use"]): x for x in self.rows()}[("gone", "intake-type")]
        self.assertEqual((r["n"], r["errors"], r["accuracy"]), (0, len(self.held), None))
        self.run_bench()
        self.assertEqual(len({x["run"] for x in self.rows()}), 2)
        self.assertNotIn("gone", self.fm("exam", "models", "show").stdout)  # show prints the latest run only


def _pool(n=12, lose_on=None):
    """Three fake projects a, b, c of n rows each; today's path is wrong on every row (right on `lose_on`'s)."""
    rows, truth = [], {}
    for pr in "abc":
        for i in range(n):
            t, typ = f"request {pr} {i}", ("FIX", "FEATURE")[i % 2]
            rows.append({"project": pr, "text": t, "type": typ, "tier": "S",
                         "path": {"intake-type": typ if pr == lose_on else "RESEARCH", "intake-tier": "S"}})
            truth[t] = typ
    return rows, truth


def _model(truth, lose_on=None, conf=0.9):
    """A contestant that knows every answer, except on project `lose_on`, where it answers wrong."""
    def make(train, use):
        return lambda text: ((truth[text] if f" {lose_on} " not in text else "RESEARCH") if use == "intake-type" else "S", conf)
    return make


class LouhiLoo(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)

    def loo(self, rows, **cons):
        return louhi.loo_models(self.p, rows=rows, contestants=cons)

    def rows(self):
        with open(os.path.join(self.p.dir, louhi.BENCH)) as f:
            return [json.loads(x) for x in f]

    def test_each_project_is_held_out_whole_and_rows_carry_coverage_and_a_wilson_bound_per_contestant(self):
        rows, truth = _pool()
        seen = []

        def make(train, use):
            seen.append(sorted({r["project"] for r in train}))  # what a held-out project's contestant trained on
            return _model(truth)(train, use)
        res = self.loo(rows, good=make)
        self.assertEqual(sorted(set(map(tuple, seen))), [("a", "b"), ("a", "c"), ("b", "c")])  # never the held-out one
        rs = [r for r in res["rows"] if r["backend"] == "good" and r["use"] == "intake-type"]
        self.assertEqual({r["project"] for r in rs} - {"pooled"}, {louhi._lake_h(x) for x in "abc"})
        pooled = {r["thr"]: r for r in rs if r["project"] == "pooled"}
        self.assertEqual(set(pooled), set(louhi.LOO_THRS))
        self.assertEqual((pooled[0.5]["n"], pooled[0.5]["coverage"], pooled[0.5]["accuracy"]), (36, 1.0, 1.0))
        self.assertAlmostEqual(pooled[0.5]["wilson_lb"], models.wilson_lower(36, 36), places=3)
        self.assertLess(pooled[0.5]["wilson_lb"], 1.0)
        self.assertEqual(pooled[0.95]["coverage"], 0.0)  # conf 0.9 abstains over 0.95
        self.assertEqual(self.rows()[-1]["arm"], "loo")  # written to the bench file

    def test_a_contestant_that_is_down_gets_no_rows_and_is_reported_down_after_three_straight_failures(self):
        # T-4111: jeff-serve was not running; its 159/159 errors were written as coverage 0, over the last real numbers
        rows, truth = _pool()
        calls = collections.Counter()

        def dead(train, use):
            def predict(text):
                calls[use] += 1
                raise models.ModelError("connection refused")
            return predict

        def flaky(train, use):  # fails on some rows only: still scored, its errors counted
            good = _model(truth)(train, use)
            return lambda text: (_ for _ in ()).throw(models.ModelError("x")) if text.endswith(" 3") else good(text)
        res = self.loo(rows, good=_model(truth), dead=dead, flaky=flaky)
        self.assertEqual(res["down"], ["dead"])
        self.assertNotIn("dead", {r["backend"] for r in res["rows"]})
        self.assertEqual(dict(calls), {"intake-type": 3})  # stops calling it for the run: no 120 s timeout per row
        fl = [r for r in res["rows"] if r["backend"] == "flaky" and r["project"] == "pooled" and r["use"] == "intake-type"]
        self.assertTrue(fl and all(r["errors"] == 3 for r in fl), fl)

    def test_majority_share_is_the_held_out_projects_own_top_class_and_a_one_project_result_says_so(self):
        # T-3071: type is 50/50 here (k=2), tier all S (k=1): the baseline is never under 1/k, never impossible for a majority class
        rows, truth = _pool()
        res = self.loo(rows, good=_model(truth))
        for r in res["rows"]:
            self.assertEqual(r["majority_share"], 0.5 if r["use"] == "intake-type" else 1.0, r)
            self.assertGreaterEqual(r["majority_share"], 1 / r["classes"])
            if r["project"] == "pooled":
                self.assertEqual(r["scope"], "3 projects")
        one = [r for r in rows if r["project"] != "c"]  # two projects: one held out at a time, still two
        self.assertEqual({r["scope"] for r in self.loo(one, good=_model(truth))["rows"] if r["project"] == "pooled"}, {"2 projects"})
        skew = [{**r, "type": "FIX"} if r["project"] == "a" else r for r in rows]  # a is all FIX: its own share is 1.0
        a = [r for r in self.loo(skew, good=_model(truth))["rows"] if r["project"] == louhi._lake_h("a") and r["use"] == "intake-type"]
        self.assertEqual({r["majority_share"] for r in a}, {1.0})
        self.assertEqual(louhi._scope(1), "one project")

    def test_a_contestant_that_wins_on_two_projects_and_loses_the_held_out_third_is_not_adopted(self):
        rows, truth = _pool(lose_on="c")
        res = self.loo(rows, mixed=_model(truth, lose_on="c"))
        a = res["adopt"]["intake-type"]
        self.assertFalse(a["adopt"], a)
        self.assertLessEqual(a["delta_lb"], 0)
        self.assertEqual(louhi.adopt_models(self.p, res)["intake-type"][0], "shadow")
        self.assertFalse(models.live(self.p, "intake-type"))

    def test_a_contestant_ahead_on_every_project_is_adopted_with_the_run_cited_and_abstains_fall_back_to_the_path(self):
        rows, truth = _pool()
        res = self.loo(rows, good=_model(truth))
        a = res["adopt"]["intake-type"]
        self.assertTrue(a["adopt"] and a["delta_lb"] > 0 and a["projects"] == 3, a)
        self.assertEqual(louhi.adopt_models(self.p, res)["intake-type"][0], "live")
        self.assertTrue(models.live(self.p, "intake-type"))  # the row cites the bench run: m...
        self.assertIn(res["run"], open(os.path.join(self.p.dir, "decisions.md")).read())
        path = lambda: "RESEARCH"  # noqa: E731
        self.assertEqual(models.resolve_use("intake-type", "x", lambda t: ("FIX", 0.9), path, 0.5), ("FIX", "model"))
        self.assertEqual(models.resolve_use("intake-type", "x", lambda t: ("FIX", 0.4), path, 0.5), ("RESEARCH", "path"))
        self.assertEqual(models.resolve_use("intake-type", "x", lambda t: None, path), ("RESEARCH", "path"))

    def test_one_project_the_model_loses_on_blocks_adoption_even_when_the_bootstrap_bound_is_positive(self):
        lb, ok = louhi._gate([0.3, 0.3, -0.05])
        self.assertGreater(lb, 0)  # the 5% quantile of 3 projects misses the worst one...
        self.assertFalse(ok)  # ...so the worst project's own gain must be >= 0 too
        self.assertTrue(louhi._gate([0.3, 0.3, 0.0])[1])
        self.assertFalse(louhi._gate([0.0, 0.0, 0.0])[1])  # a tie is not a win, whatever the float noise

    def test_a_confidence_of_none_falls_back_to_the_path(self):
        self.assertEqual(models.resolve_use("u", "x", lambda t: ("FIX", None), lambda: "FEATURE"), ("FEATURE", "path"))

    def test_rows_and_decision_name_the_baseline_actually_used(self):
        rows, truth = _pool()  # every row has a path
        res = self.loo(rows, good=_model(truth))
        self.assertEqual({r["baseline"] for r in res["rows"]}, {"path"})
        louhi.adopt_models(self.p, res)
        self.assertIn("beat today's path", open(os.path.join(self.p.dir, "decisions.md")).read())
        nop = [{k: v for k, v in r.items() if k != "path"} for r in rows]
        res = louhi.loo_models(self.p, rows=nop, contestants={"good": _model(truth)})
        self.assertEqual({r["baseline"] for r in res["rows"]}, {"majority"})
        self.assertEqual(res["adopt"]["intake-type"]["baseline"], "majority")

    def test_under_three_projects_nothing_is_adopted(self):
        rows, truth = _pool()
        res = self.loo([r for r in rows if r["project"] != "c"], good=_model(truth))
        a = res["adopt"]["intake-type"]
        self.assertFalse(a["adopt"])
        self.assertIn("3 projects", a["why"])

    def test_the_bootstrap_is_seeded(self):
        rows, truth = _pool(lose_on="c")
        a = self.loo(rows, mixed=_model(truth, lose_on="c"))["adopt"]["intake-type"]["delta_lb"]
        self.assertEqual(self.loo(rows, mixed=_model(truth, lose_on="c"))["adopt"]["intake-type"]["delta_lb"], a)

    def test_a_live_use_whose_live_agreement_falls_under_the_floor_goes_back_to_shadow_once(self):
        rows, truth = _pool()
        louhi.adopt_models(self.p, self.loo(rows, good=_model(truth)))
        self.assertTrue(models.live(self.p, "intake-type"))
        self.assertEqual(models.demote(self.p), [])  # no booked answers: nothing to judge
        # the floor is the path's accuracy on the booked rows (12/20), not on every shadow row (n=40, 36 hits)
        fake = {"intake-type": {"n": 40, "path_hits": 36, "model_n": 20, "model_hits": 8, "model_path_hits": 12}}
        orig = louhi.replay_models
        louhi.replay_models = lambda p: fake
        try:
            self.assertEqual(models.demote(self.p)[0][:3], ("intake-type", 8, 20))
            self.assertFalse(models.live(self.p, "intake-type"))
            self.assertEqual(models.demote(self.p), [])  # a second run: already shadow
        finally:
            louhi.replay_models = orig

    def test_the_flip_report_names_each_change_between_two_runs(self):
        rows, truth = _pool(lose_on="c")
        r1 = self.loo(rows, m=_model(truth, lose_on="c"))["run"]
        rows2, _ = _pool()
        r2 = louhi.loo_models(self.p, rows=rows2, contestants={"m": _model(truth)})["run"]
        text = louhi.flip_report(self.p, r1, r2)
        self.assertRegex(text, r"FLIP intake-type m: shadow -> live")
        self.assertIn("no change", louhi.flip_report(self.p, r2, r2))


def _row(run, backend, use="intake-type", gen=None, acc=0.6, n=48, err=0, ok=None, **kw):
    r = {"run": run, "at": "2026-11-04T00:00:00Z", "arm": "decision", "use": use, "backend": backend, "accuracy": acc,
         "brier": 0.2, "p50_ms": 10, "p95_ms": 20, "n": n, "errors": err, "held": n, **kw}
    if gen is not None:
        r["gen"] = gen
    if ok is not None:
        r["ok"] = ok
    return r


def _oks(hits, ids, p, flat=False, right=None):
    """{task: [hit, probability]}: the first `hits` of `ids` (or `right`) right, said with p when right and 1-p when wrong
    (every answer at p when `flat`)."""
    right = set(right if right is not None else ids[:hits])
    return {t: [int(t in right), None if p is None else p if flat or t in right else round(1 - p, 3)] for t in ids}


class BenchMigrates(ForemanTestCase):  # T-3064: the bench moved to the state dir; an old copy is carried over, once
    ROW = '{"use": "x", "n": 1}\n'

    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)
        self.root_copy = os.path.join(self.p.root, c.BENCH_REL)
        self.state_copy = os.path.join(self.p.dir, c.BENCH_REL)

    def git(self, *a):
        import subprocess
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=self.repo, capture_output=True, check=True)

    def test_a_root_copy_is_copied_and_kept(self):
        os.makedirs(os.path.dirname(self.root_copy), exist_ok=True)
        with open(self.root_copy, "w") as f:
            f.write(self.ROW)
        self.assertEqual(c.bench_path(self.p), self.state_copy)
        self.assertEqual(read_text(self.state_copy), self.ROW)
        self.assertTrue(os.path.exists(self.root_copy))

    def test_a_copy_deleted_in_history_is_restored_from_the_commit_before(self):
        os.makedirs(os.path.dirname(self.root_copy), exist_ok=True)
        with open(self.root_copy, "w") as f:
            f.write(self.ROW)
        self.git("add", "-f", c.BENCH_REL)
        self.git("commit", "-qm", "add bench")
        self.git("rm", "-q", c.BENCH_REL)
        self.git("commit", "-qm", "drop bench")
        self.assertFalse(os.path.exists(self.root_copy))
        c.bench_path(self.p)
        self.assertEqual(read_text(self.state_copy), self.ROW)

    def test_neither_gives_an_empty_read_and_an_existing_state_copy_is_never_overwritten(self):
        self.assertFalse(os.path.exists(c.bench_path(self.p)))
        self.assertEqual(louhi.bench_show(self.p).count('"use"'), 0)
        os.makedirs(os.path.dirname(self.state_copy), exist_ok=True)
        with open(self.state_copy, "w") as f:
            f.write("mine\n")
        os.makedirs(os.path.dirname(self.root_copy), exist_ok=True)
        with open(self.root_copy, "w") as f:
            f.write(self.ROW)
        c.bench_path(self.p)
        self.assertEqual(read_text(self.state_copy), "mine\n")


class ModelBenchVisible(ForemanTestCase):  # T-3093: the Models section, the per-generation rerun, the promotion rule
    IDS = [f"T-{i:04d}" for i in range(1, 261)]

    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)

    def write(self, rows, frozen=None):
        path = os.path.join(self.p.dir, louhi.BENCH)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.writelines(json.dumps(r) + "\n" for r in rows)
        if frozen:
            os.makedirs(os.path.join(self.p.root, os.path.dirname(louhi.FROZEN)), exist_ok=True)
            with open(os.path.join(self.p.root, louhi.FROZEN), "w") as f:
                json.dump({"gen": 4, "ids": frozen}, f)

    def runs(self, model_hits=150, path_hits=100, maj_hits=90, run="m20261104-000000001", gen=5, ids=None, p=0.8, flat=False,
             right=None):
        ids = ids or self.IDS[:200]
        return [_row(run, "orchestrator", gen=gen, n=len(ids), acc=path_hits / len(ids), ok=_oks(path_hits, ids, None)),
                _row(run, "majority", gen=gen, n=len(ids), acc=maj_hits / len(ids), ok=_oks(maj_hits, ids, None), ceiling=0.9),
                _row(run, "kev", gen=gen, n=len(ids), acc=model_hits / len(ids), ok=_oks(model_hits, ids, p, flat, right))]

    def decisions(self):
        return read_text(os.path.join(self.p.dir, "decisions.md")) if os.path.exists(os.path.join(self.p.dir, "decisions.md")) else ""

    def test_the_section_keeps_the_last_complete_run_and_says_stale_since(self):
        rows = [_row("r1", "kev", gen=4, acc=0.7), _row("r1", "majority", gen=4, acc=0.5), _row("r1", "orchestrator", gen=4, acc=0.6),
                _row("r1", "d3", gen=4, acc=0.45),
                _row("r2", "kev", gen=5, acc=None, n=0, err=48),  # a failed run
                _row("r2", "d3", gen=5, acc=0.4, n=20, err=3)]  # a partial run
        v = louhi.models_view(rows)["uses"]["intake-type"]
        by = {b["backend"]: b for b in v["backends"]}
        self.assertEqual((by["kev"]["accuracy"], by["kev"]["run"], by["kev"]["stale_since"]), (0.7, "r1", "gen 5"))
        self.assertEqual(by["kev"]["p95_ms"], 20)
        self.assertEqual(by["d3"]["accuracy"], 0.45, "the partial run never replaces the complete one")
        self.assertEqual((v["path"]["accuracy"], v["majority"]["accuracy"]), (0.6, 0.5))
        skip = {"run": "r3", "at": "x", "arm": "skipped", "use": "*", "backend": "*", "why": "busy", "gen": 6}
        by = {b["backend"]: b for b in louhi.models_view(rows[:4] + [skip])["uses"]["intake-type"]["backends"]}
        self.assertEqual(by["kev"]["stale_since"], "gen 6", "a skipped run marks every backend stale")
        by = {b["backend"]: b for b in louhi.models_view(rows[:4])["uses"]["intake-type"]["backends"]}
        self.assertIsNone(by["kev"]["stale_since"])
        self.assertEqual(louhi.models_view([])["uses"], {})

    def test_a_backend_goes_live_only_on_200_frozen_rows_beating_both_baselines_by_5_points(self):
        ids = self.IDS[:200]
        rows = self.runs()
        base = {"path": rows[0], "majority": rows[1]}
        v = louhi.promote_verdict(rows[2], base, ids)
        self.assertTrue(v["pass"], v)
        self.assertLess(v["p_path"], 0.05)
        self.assertFalse(louhi.promote_verdict(self.runs(model_hits=104)[2], base, ids)["pass"], "4 points over the path")
        short = louhi.promote_verdict(rows[2], base, ids[:199])
        self.assertFalse(short["pass"])
        self.assertIn("199", short["why"])
        loud = louhi.promote_verdict(self.runs(p=0.99, flat=True)[2], base, ids)
        self.assertFalse(loud["pass"], "no sense of which answers are right: Brier worse than a flat guess")
        self.assertIn("Brier", loud["why"])
        # 5 points over the path (110 v 100 right) by winning 60 tasks and losing 50: not significant
        noisy = louhi.promote_verdict(self.runs(right=ids[50:160])[2], base, ids)
        self.assertFalse(noisy["pass"])
        self.assertGreaterEqual(noisy["p_path"], 0.05)
        self.assertEqual(noisy["gain_path"], 0.05)
        wide = {t: [0, 0.8] for t in self.IDS[200:]}  # rows closed since are rolling only
        r = dict(rows[2], ok={**rows[2]["ok"], **wide})
        self.assertEqual(louhi.promote_verdict(r, base, ids), v)

    def test_the_rule_writes_a_decision_row_citing_the_run_and_demotes_on_a_later_fall(self):
        from sampo import models
        ids = self.IDS[:200]
        self.write(self.runs(), frozen={"intake-type": ids})
        self.assertEqual(louhi.apply_promotion(self.p)["intake-type"][0], "live")
        self.assertTrue(models.live(self.p, "intake-type"))
        text = self.decisions()
        self.assertIn("m20261104-000000001", text)
        self.assertIn("backend kev", text)
        self.assertEqual(louhi.apply_promotion(self.p)["intake-type"][0], "live")
        self.assertEqual(self.decisions(), text, "a second call writes nothing")
        later = self.runs(model_hits=101, run="m20261105-000000001", gen=6)
        self.write(self.runs() + later, frozen={"intake-type": ids})
        self.assertEqual(louhi.apply_promotion(self.p)["intake-type"][0], "shadow")
        self.assertFalse(models.live(self.p, "intake-type"))
        self.assertIn("m20261105-000000001", self.decisions())

    def test_a_later_run_too_thin_to_judge_leaves_a_live_backend_alone(self):
        from sampo import models
        ids = self.IDS[:200]
        self.write(self.runs(), frozen={"intake-type": ids})
        louhi.apply_promotion(self.p)
        thin = self.runs(model_hits=10, run="m20261105-000000001", gen=6, ids=ids[:50])
        self.write(self.runs() + thin, frozen={"intake-type": ids})
        louhi.apply_promotion(self.p)
        self.assertTrue(models.live(self.p, "intake-type"))

    def test_no_frozen_set_no_promotion(self):
        from sampo import models
        self.write(self.runs())
        louhi.apply_promotion(self.p)
        self.assertFalse(models.live(self.p, "intake-type"))

    def test_a_held_moons_store_does_not_stop_a_bench_round(self):  # T-3487: a grow --keep holds the moons store for days
        from sampo import moonscale, rune
        os.makedirs(os.path.join(rune.home(self.p), "gen-4"))
        import threading  # the store lock is re-entrant per thread: the grow holds it from its own thread
        held, done = threading.Event(), threading.Event()

        def grow():
            with moonscale.store_lock(self.p):
                held.set()
                done.wait(30)
        t = threading.Thread(target=grow)
        t.start()
        held.wait(30)
        try:
            res = louhi.bench_night(self.p, decision=[], embedders=[])
        finally:
            done.set()
            t.join()
        self.assertNotIn("skipped", res)
        rows = [json.loads(x) for x in open(os.path.join(self.p.dir, louhi.BENCH))]
        self.assertTrue(rows and all(r["arm"] != "skipped" for r in rows), "the round ran beside the grow")

    def test_a_busy_exam_lock_is_a_skipped_row_and_the_generation_stays_due(self):
        from sampo import rune
        os.makedirs(os.path.join(rune.home(self.p), "gen-4"))
        self.assertEqual(louhi.current_gen(self.p), 4)
        self.assertTrue(louhi.bench_due(self.p))
        with louhi._exam_lock(self.p):  # another bench run holds it (flock is per open file, so this thread is refused too)
            res = louhi.bench_night(self.p, decision=[], embedders=[])
        self.assertIn("another bench run", res["skipped"])
        rows = [json.loads(x) for x in open(os.path.join(self.p.dir, louhi.BENCH))]
        self.assertEqual([(r["arm"], r["gen"]) for r in rows], [("skipped", 4)])
        self.assertTrue(louhi.bench_due(self.p), "a skip leaves the generation due")
        self.assertEqual(louhi.models_view(rows)["uses"], {})
        self.assertNotIn("skipped", louhi.bench_show(self.p).lower().replace("no model bench", ""))

    def test_the_exam_lock_names_its_holder_and_is_free_again_after_release(self):  # T-3487: the deadline and the pid
        with louhi._exam_lock(self.p):
            with self.assertRaises(louhi.ExamBusy) as e:
                with louhi._exam_lock(self.p, wait=0.1):
                    pass
            self.assertIn(f"pid {os.getpid()}", str(e.exception))
        with louhi._exam_lock(self.p, wait=0.1):
            pass

    def test_a_rerun_stamps_its_gen_and_per_task_hits_and_pins_the_frozen_set_once(self):
        from sampo import rune
        for i in range(12):
            title, typ, tier = TASKS[i % 2]
            b = c.find_brief(self.p, json.loads(self.fm("capture", title % i, "--json").stdout)["id"])
            b.meta.update(type=typ, tier=tier, status="done")
            c.save_brief(self.p, b)
        os.makedirs(os.path.join(rune.home(self.p), "gen-3"))
        srv = Server("FIX")
        self.addCleanup(srv.close)
        prof = srv.profile("kev", "systemone")
        res = louhi.bench_night(self.p, decision=[prof], embedders=[])
        self.assertFalse(os.path.exists(os.path.join(self.p.root, louhi.FROZEN)), "nothing is pinned before gen 4")
        os.makedirs(os.path.join(rune.home(self.p), "gen-4"))
        res = louhi.bench_night(self.p, decision=[prof], embedders=[])
        self.assertNotIn("skipped", res)
        kev = next(r for r in res["rows"] if r["backend"] == "kev" and r["use"] == "intake-type")
        self.assertEqual(kev["gen"], 4)
        self.assertEqual(len(kev["ok"]), kev["n"])
        self.assertTrue(all(len(v) == 2 for v in kev["ok"].values()))
        fz = json.load(open(os.path.join(self.p.root, louhi.FROZEN)))
        self.assertEqual(fz["gen"], 4)
        self.assertEqual(sorted(fz["ids"]["intake-type"]), sorted(kev["ok"]))
        maj = next(r for r in res["rows"] if r["backend"] == "majority" and r["use"] == "intake-type")
        self.assertEqual(maj["ceiling"], 1.0)
        self.assertFalse(louhi.bench_due(self.p), "this generation has its run")
        os.makedirs(os.path.join(rune.home(self.p), "gen-5"))
        self.assertTrue(louhi.bench_due(self.p))
        self.fm("capture", "a new held out thing 99")
        louhi.bench_night(self.p, decision=[], embedders=[])
        self.assertEqual(json.load(open(os.path.join(self.p.root, louhi.FROZEN))), fz, "pinned once, never rewritten")

    def test_a_backend_that_keeps_failing_is_cut_and_the_rest_count_as_errors(self):
        from unittest import mock
        rows = [("T-%d" % i, "text", "FIX", None) for i in range(10)]
        with mock.patch.object(models, "systemone", side_effect=models.ModelError("down")) as m:
            got = louhi._bench_decide({"name": "x"}, rows, "intake-type", 1)
        self.assertEqual((m.call_count, got["errors"], got["n"]), (louhi.BENCH_GIVE_UP, 10, 0))

    def test_the_noise_ceiling_is_the_share_of_tasks_never_re_tiered(self):
        for t in ("T-1", "T-2"):
            c.log_event(self.p, "task_set", task=t, data={"changes": {"tier": "M"}})
        c.log_event(self.p, "task_set", task="T-3", data={"changes": {"cost_cap": "5"}})
        self.assertEqual(louhi.noise_ceiling(self.p, "intake-tier", ["T-1", "T-2", "T-3", "T-4"]), 0.5)
        self.assertEqual(louhi.noise_ceiling(self.p, "intake-type", ["T-1"]), 1.0)
        self.assertIsNone(louhi.noise_ceiling(self.p, "intake-tier", []))
