"""T-2977: the close-holds verifier. A logistic head on text-free features of a close (counts and parsed test totals,
never Claude's words), trained on the lake's outcome labels with whole projects held out; shadow at `sampo task finish`
(a close_score event, no outcome changed) and live only when models.live names it."""
import json
import os
import subprocess

from helpers import SampoTestCase

from sampo import core as c
from sampo import verifier as v
from test_lake import Lake


def row(proj, tests, red_green, holds):
    f = dict.fromkeys(v.FEATURES, 0)
    f.update(tests_passed=tests, red_green=red_green, criteria=2)
    return {"prov": {"project": proj}, "label": "held" if holds else "reverted", "features": f}


def planted():
    """Three projects; a close that holds ran many tests and a red-then-green proof, the rest is the same."""
    rows = []
    for proj in ("a", "b", "c"):
        for i in range(30):
            rows.append(row(proj, 20 + i % 5, 1, True))
        for i in range(10):
            rows.append(row(proj, i % 3, 0, False))
    return rows


class Features(SampoTestCase):
    def test_features_are_numbers_from_a_fixture_brief(self):
        self.fm("init")
        tid = json.loads(self.fm("task", "new", "t", "--type", "FIX", "--tier", "S", "--ac", "it works :: true",
                                 "--step", "fix", "--json").stdout)["id"]
        p = c.find_project(self.repo)
        b = c.find_brief(p, tid)
        b.add_evidence("pytest", "✗ exit 1 · 1 failed, 2 passed", step=1, ran=True)
        b.add_evidence("pytest", "exit 0 · Ran 5 tests in 0.1s / OK", step=1, ran=True)
        b.add_audit("self", "looked", "fine")
        f = v.features(b)
        self.assertEqual(sorted(f), sorted(v.FEATURES))
        self.assertTrue(all(isinstance(x, (int, float)) for x in f.values()))
        self.assertEqual((f["criteria"], f["steps"], f["evidence"], f["red_green"], f["audits"]), (1, 1, 1, 1, 1))
        self.assertEqual((f["tests_run"], f["tests_passed"]), (5, 5))  # the newest run of the one command
        self.assertEqual((f["tier_s"], f["type_fix"]), (1, 1))
        self.assertNotIn("looked", json.dumps(f))
        # a typed line (the agent's word) moves nothing, nor does a command that merely says "Ran 500 tests"
        b.add_evidence("echo Ran 500 tests", "Ran 500 tests / OK", step=1)
        b.add_evidence("echo 400 passed", "exit 0 · 400 passed", step=1)
        self.assertEqual(v.features(b), f)
        # a refused close's retry appends the same run and audit again: the same features
        b.add_evidence("pytest", "exit 0 · Ran 5 tests in 0.1s / OK", step=1, ran=True)
        b.add_audit("self", "looked", "fine")
        self.assertEqual({k: x for k, x in v.features(b).items() if k != "minutes"}, {k: x for k, x in f.items() if k != "minutes"})


class Head(SampoTestCase):
    def test_a_head_learns_a_planted_signal_and_scores_a_held_out_project(self):
        res = v.evaluate(planted())
        self.assertEqual(sorted(res["projects"]), ["a", "b", "c"])
        for name, m in res["projects"].items():
            self.assertGreater(m["auc"], 0.95, name)
            self.assertGreater(m["caught_at_10pct_false_alarms"], 0.8, name)
        self.assertGreater(res["pooled"]["auc"], 0.95)
        self.assertEqual(v.evaluate(planted()), res)  # deterministic
        self.assertIsNotNone(res["threshold"])  # from the pooled held-out scores

    def test_no_signal_is_not_adopted_and_one_class_is_said(self):
        rows = [row("a", 5, 0, i % 2 == 0) for i in range(20)] + [row("b", 5, 0, i % 2 == 0) for i in range(20)]
        self.assertLess(v.evaluate(rows)["pooled"]["auc"], 0.6)
        self.assertIsNone(v.evaluate([row("a", 1, 0, True)] * 5)["pooled"]["auc"])

    def test_the_ask_names_the_missing_features_from_a_fixed_template(self):
        model = v.train(planted())
        weak = dict.fromkeys(v.FEATURES, 0)
        weak["criteria"] = 2
        strong = row("a", 22, 1, True)["features"]
        self.assertLess(v.score(model, weak), v.score(model, strong))
        ask = v.ask("T-0001", v.score(model, weak), model, weak)
        self.assertIn("tests_passed", ask)
        self.assertIn("red_green", ask)
        self.assertEqual(ask, v.ask("T-0001", v.score(model, weak), model, weak))


class Lake2(Lake):
    def test_lake_rows_carry_a_text_free_features_dict_and_exam_trains_the_head(self):
        _, rows = self.export()
        real = [r for r in rows if not r.get("canary")]
        for r in real:
            self.assertEqual(sorted(r["features"]), sorted(v.FEATURES))
            self.assertTrue(all(isinstance(x, (int, float)) for x in r["features"].values()))
        p = c.find_project(self.repo)
        r = self.fm("exam", "models", "--verifier", "--json")
        out = json.loads(r.stdout)
        self.assertIn("pooled", out)
        with open(os.path.join(p.dir, "research", "models", "bench.jsonl")) as f:
            bench = [json.loads(x) for x in f]
        self.assertTrue([b for b in bench if b.get("use") == "verifier"])
        self.assertTrue(os.path.exists(os.path.join(p.dir, "research", "models", "verifier.json")) or out["pooled"]["auc"] is None)


class Shadow(SampoTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)
        os.makedirs(os.path.join(self.p.dir, "research", "models"), exist_ok=True)
        model = v.train(planted())
        with open(os.path.join(self.p.dir, "research", "models", "verifier.json"), "w") as f:
            json.dump(dict(model, threshold=0.9), f)

    def close(self, **kw):
        tid = json.loads(self.fm("task", "new", "work", "--type", "CLEAN", "--tier", "S", "--ac", "ok :: true",
                                 "--step", "do it", "--focus", "--json").stdout)["id"]
        return tid, self.fm("task", "finish", tid, "--audit", "looked", "--run", "true", "--docs", "none", check=False)

    def events(self):
        return [e for e in c.ledger_events(self.p, ("close_score",))]

    def test_no_weights_no_event_and_a_missing_class_removes_stale_weights(self):
        wp = os.path.join(self.p.dir, v.WEIGHTS)
        os.remove(wp)
        tid, r = self.close()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.events(), [])
        with open(wp, "w") as f:
            f.write("{}")
        self.fm("exam", "models", "--verifier")  # this machine's lake here has no closes with both outcomes
        self.assertFalse(os.path.exists(wp))
        with open(os.path.join(self.p.dir, v.BENCH)) as f:
            self.assertTrue(all(json.loads(x).get("run") for x in f))

    def test_shadow_logs_a_score_and_changes_no_outcome(self):
        tid, r = self.close()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        ev = self.events()
        self.assertEqual(len(ev), 1)
        self.assertEqual((ev[0]["task"], ev[0]["data"]["live"]), (tid, False))
        self.assertIsInstance(ev[0]["data"]["score"], float)
        self.assertEqual(c.find_brief(self.p, tid).status, "done")

    def decide_live(self, **trained):
        with open(os.path.join(self.p.dir, "decisions.md"), "w") as f:
            f.write("| Date | Decision | Why | Revisit |\n|---|---|---|---|\n"
                    "| 2026-11-04 | model-live: verifier: exam s30-001 beat today's false-done rate | why |  |\n")
        with open(os.path.join(self.p.dir, v.WEIGHTS), "w") as f:
            json.dump(dict(v.train(planted()), threshold=0.9, trained=trained), f)

    def test_a_weak_pooled_auc_keeps_it_shadow_even_with_a_live_decision_row(self):  # T-3073
        for t in ({"auc": 0.55, "projects": 5}, {"auc": 0.9, "projects": 2}, {}):
            self.decide_live(**t)
            tid, r = self.close()
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertNotIn("close score", r.stdout + r.stderr)
            self.assertFalse(self.events()[-1]["data"]["live"])

    def test_live_advises_the_missing_evidence_and_never_blocks_the_close(self):  # models advise, proof decides
        self.decide_live(auc=0.7, projects=3)
        tid, r = self.close()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("close score", r.stdout + r.stderr)
        self.assertIn("tests_passed", r.stdout + r.stderr)
        self.assertIn("pooled held-out AUC 0.7, advice only", r.stdout + r.stderr)
        self.assertEqual(c.find_brief(self.p, tid).status, "done")
        self.assertTrue(self.events()[-1]["data"]["live"])
