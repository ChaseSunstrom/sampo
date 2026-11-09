"""T-3930: the frontier-gap scoreboard: each local profile's held-out pass rate beside a Claude reference arm's on the
same exam run, rows saved with the git revision, a 14-run trend with the commits between, and a paired credit rule."""
import datetime
import json
import os
import shutil
import subprocess
from unittest import mock

from helpers import ForemanTestCase, read_text

from sampo import core as c
from sampo import louhi, rune


def ago(days):
    return (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


class Gap(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        g = self.fm_json("exam", "gen", "--seed", "8", "--per-kind", "3", "--lang", "python", "--kind", "bug,vuln,slow")
        self.run_id = g["run"]
        self.p = c.find_project(self.repo)
        sc = louhi.load(self.p, self.run_id)["scenarios"]
        self.held = [s["id"] for s in sc if s["split"] == "holdout"]
        self.kind = {s["id"]: s["kind"] for s in sc}
        self.assertEqual(len(self.held), 3)

    def line(self, agent, model, closed, at=None, **kw):
        rune.exam_line(self.p, dict({"run": self.run_id, "agent": agent, "model": model, "at": at or ago(0), "split": "holdout",
                                     "closed": {"train": [], "holdout": closed}, "answered": {"holdout": self.held, "child": []},
                                     "found": len(closed), "keyed": 3, "fixed": len(closed), "fixable": 3}, **kw))

    def commit(self, name):
        with open(os.path.join(self.repo, name), "w") as f:
            f.write(name)
        subprocess.run(["git", "-C", self.repo, "add", name], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", f"add {name}"], check=True)
        return subprocess.run(["git", "-C", self.repo, "rev-parse", "HEAD"], capture_output=True, text=True,
                              check=True).stdout.strip()

    def test_the_table_sets_each_profile_beside_the_reference_per_kind_with_its_failure_classes(self):
        self.line("smith", "qwen", self.held[:1])
        self.line("foreman", "haiku", list(self.held))
        ans = louhi._answers(self.p, self.run_id, "smith")
        os.makedirs(ans)
        with open(os.path.join(ans, "model"), "w") as f:
            f.write("qwen")
        with open(os.path.join(ans, "x.events.jsonl"), "w") as f:
            f.write(json.dumps({"event": "error", "text": "fm code: the model gave no valid plan after 2 repairs"}) + "\n")
        (row,) = louhi.gap_rows(self.p)
        self.assertEqual((row["profile"], row["run"], row["ref_model"]), ("qwen", self.run_id, "haiku"))
        self.assertEqual((row["pass"], row["ref_pass"], row["gap"]), (33.3, 100.0, 66.7))
        self.assertEqual((row["found"], row["keyed"], row["fixed"], row["fixable"]), (1, 3, 1, 3))
        self.assertEqual(set(row["kinds"]), {"bug", "vuln", "slow"})
        self.assertEqual(row["kinds"][self.kind[self.held[0]]]["gap"], 0.0)
        self.assertEqual(row["classes"][0][0], "no_plan")
        out = self.fm("exam", "gap", self.run_id).stdout
        self.assertIn("qwen", out)
        self.assertIn("66.7", out)

    def test_no_reference_arm_is_said_and_costs_nothing(self):
        self.line("smith", "qwen", self.held[:1])
        self.assertEqual(louhi.gap_rows(self.p), [])
        self.assertIn("no reference", self.fm("exam", "gap", self.run_id).stdout)

    def test_the_reference_is_reused_within_a_week_and_rerun_after(self):
        self.line("foreman", "haiku", list(self.held), at=ago(2))
        self.assertFalse(louhi.ref_due(self.p, self.run_id, "haiku"))
        self.assertTrue(louhi.ref_due(self.p, self.run_id, "sonnet"))
        calls = []
        self.assertTrue(louhi.gap_ref(self.p, self.run_id, "haiku", runner=lambda *a, **k: calls.append(a))["reused"])
        self.assertEqual(calls, [])
        self.line("foreman", "sonnet", [], at=ago(9))
        self.assertTrue(louhi.ref_due(self.p, self.run_id, "sonnet"))
        got = louhi.gap_ref(self.p, self.run_id, "sonnet", budget=1.5, runner=lambda *a, **k: calls.append((a, k)))
        self.assertFalse(got["reused"])
        self.assertEqual(calls[0][1]["budget"], 1.5)
        self.assertEqual(calls[0][1]["split"], "holdout")

    def test_rows_are_saved_once_with_the_git_revision_and_the_trend_names_the_commits_between(self):
        rev1 = self.commit("one.txt")
        self.line("foreman", "haiku", list(self.held))
        self.line("smith", "qwen", [], at=ago(2))
        self.assertEqual(len(louhi.gap_save(self.p)), 1)
        rev2 = self.commit("two.txt")
        self.line("smith", "qwen", self.held[:2], at=ago(1))
        new = louhi.gap_save(self.p)
        self.assertEqual(len(new), 1)
        self.assertEqual(louhi.gap_save(self.p), [])  # a second run adds nothing
        saved = [json.loads(x) for x in read_text(os.path.join(self.p.root, louhi.GAP_FILE)).splitlines()]
        self.assertEqual([r["rev"] for r in saved], [rev1[:12], rev2[:12]])
        tr = louhi.gap_trend(self.p)["qwen"]
        self.assertEqual([r["gap"] for r in tr["rows"]], [100.0, 33.3])
        self.assertEqual(tr["rows"][1]["commits"], ["add two.txt"])
        self.assertIn("not credited", tr["rows"][1]["credit"])  # two wins, no loss: p = 0.5, one run is no proof
        self.assertIn("add two.txt", self.fm("exam", "gap", "--trend").stdout)

    def test_the_trend_keeps_the_last_14_runs(self):
        for i in range(20):
            louhi.gap_append(self.p, [{"at": ago(20 - i), "run": "s1-001", "profile": "qwen", "rev": "", "gap": float(i)}])
        self.assertEqual(len(louhi.gap_trend(self.p)["qwen"]["rows"]), 14)

    def test_credit_needs_the_same_run_and_enough_paired_wins(self):
        a = {"run": "s1-001", "closed": [], "answered": [f"x{i}" for i in range(10)]}
        b = dict(a, closed=[f"x{i}" for i in range(10)])
        self.assertTrue(louhi.gap_credit(a, b).startswith("credited"))
        self.assertTrue(louhi.gap_credit(b, a).startswith("hurt"))
        self.assertTrue(louhi.gap_credit(a, dict(b, run="s2-001")).startswith("unpaired"))
        self.assertTrue(louhi.gap_credit(a, dict(a)).startswith("not credited"))

    def test_the_live_page_carries_the_gap_trend_and_draws_a_line_chart(self):
        from sampo import live
        louhi.gap_append(self.p, [{"at": ago(3 - i), "run": "s1-001", "profile": "qwen", "rev": "abc", "gap": 60.0 - 10 * i}
                                  for i in range(3)])
        live._CACHE.clear()
        with mock.patch.object(c, "_git", side_effect=AssertionError("the live page runs no git")):
            self.assertEqual([x["gap"] for x in live.stats(self.p)["gap"]["qwen"]], [60.0, 50.0, 40.0])
        louhi.gap_append(self.p, [{"at": ago(0.5 + i / 100), "run": "s1-001", "profile": "qwen", "gap": float(i)} for i in range(20)])
        self.assertEqual(len(louhi.gap_series(self.p)["qwen"]), 14)

    def test_the_chart_draws_empty_one_point_and_negative_gaps_inside_its_box(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("no node to run the page's drawer")
        out = subprocess.run([node, "-e", DRAW, os.path.join(os.path.dirname(louhi.__file__), "..", "..", "web", "live", "board.js")],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr[-1500:])
        got = json.loads(out.stdout)
        self.assertTrue(any("no gap yet" in t for t in got["empty"]["text"]), got["empty"]["text"])
        self.assertTrue(any(t.startswith("p 4") for t in got["one"]["text"]), got["one"]["text"])
        self.assertGreater(got["one"]["dots"], 0)
        neg = got["neg"]
        self.assertEqual(neg["bad"], [])
        self.assertLessEqual(max(neg["ys"]), neg["bot"] + 0.01, "a gap below zero stays above the chart's floor")
        self.assertGreaterEqual(min(neg["ys"]), neg["top"] - 0.01)
        self.assertGreater(neg["ys"][1], neg["zero"], "-5 is drawn below the zero line: ahead of Claude, not off the chart")

    def test_both_arms_are_compared_on_the_scenarios_both_answered_across_rounds(self):
        g = self.fm_json("exam", "gen", "--seed", "9", "--per-kind", "6", "--lang", "python", "--kind", "bug,vuln,slow")
        run = g["run"]
        held = [s["id"] for s in louhi.load(self.p, run)["scenarios"] if s["split"] == "holdout"]
        self.assertGreater(len(held), 3)
        r1, r2 = held[:3], held[3:6]

        def put(agent, model, ids, closed, at):
            rune.exam_line(self.p, {"run": run, "agent": agent, "model": model, "at": at, "split": "holdout",
                                    "closed": {"train": [], "holdout": closed}, "answered": {"holdout": ids, "child": []},
                                    "found": 0, "keyed": 3, "fixed": 0, "fixable": 3})
        put("smith", "qwen", r1, r1[:1], ago(3))
        put("smith", "qwen", r2, [], ago(1))  # round two: the profile's newest line
        put("foreman", "haiku", r1, r1, ago(0))  # the reference answered round one only
        self.assertEqual(louhi.gap_rows(self.p, run), [], "no scenario both answered: no comparison")
        put("foreman", "haiku", r1 + r2[:1], r1 + r2[:1], ago(0))
        (row,) = louhi.gap_rows(self.p, run)
        self.assertEqual((row["n"], row["pass"], row["ref_pass"], row["gap"]), (1, 0.0, 100.0, 100.0))
        self.assertEqual(row["answered"], r2[:1])
        calls = []
        louhi.gap_ref(self.p, run, "sonnet", runner=lambda *a, **k: calls.append(k), ids=r2)
        self.assertEqual((calls[0]["ids"], calls[0]["limit"]), (set(r2), 3))
        self.assertEqual(louhi.local_ids(self.p, run), r2)

    def test_the_reference_filter_counts_only_claude_lines_that_answered_held_out(self):
        self.line("smith", "haiku-tuned", list(self.held), at=ago(1))  # a local profile whose name says haiku is no reference
        self.line("foreman", "haiku", [], at=ago(1), answered={"holdout": [], "child": []})
        self.assertTrue(louhi.ref_due(self.p, self.run_id, "haiku"))
        path = os.path.join(rune.home(self.p), "exam.jsonl")
        with open(path, "a") as f:
            f.write(json.dumps({"run": self.run_id, "model": "x", "closed": {}, "answered": {}}) + "\n")  # no agent: skipped
        self.assertEqual(louhi.gap_rows(self.p), [])

    def test_the_night_runs_the_paid_reference_weekly_across_runs_unless_sensitive_or_degraded(self):
        from unittest import mock as m
        from sampo import budget, night
        step = lambda: [(n, a, u) for n, a, u in night.jobs(self.p) if n == "frontier gap"]  # noqa: E731
        self.assertEqual(louhi.gap_pending(self.p), None, "no local profile answered yet")
        self.line("smith", "qwen", self.held[:1], at=ago(1))
        self.assertEqual(louhi.gap_pending(self.p), self.run_id)
        (name, argv, usd), = step()
        self.assertEqual(argv, ["exam", "gap", self.run_id, "--ref"])
        self.assertEqual(usd, budget.estimate("exam-arm", 3, 0.5))
        with m.patch.object(budget, "degrade", return_value=True):
            self.assertEqual(step(), [], "a degraded night spends nothing")
        c.write_meta(self.p, {**c.read_meta(self.p), "sensitive": True})
        self.assertEqual(step(), [])
        c.write_meta(self.p, {**c.read_meta(self.p), "sensitive": False})
        self.line("foreman", "haiku", list(self.held), at=ago(2))
        self.assertEqual(louhi.gap_pending(self.p), None, "reused inside the week")
        g = self.fm_json("exam", "gen", "--seed", "10", "--per-kind", "3", "--lang", "python", "--kind", "bug")
        rune.exam_line(self.p, {"run": g["run"], "agent": "smith", "model": "qwen", "at": ago(0), "split": "holdout",
                                "closed": {"train": [], "holdout": []}, "answered": {"holdout": ["x"], "child": []}})
        self.assertEqual(louhi.gap_pending(self.p), None, "a newer run does not buy a second reference inside the week")
        os.remove(os.path.join(rune.home(self.p), "exam.jsonl"))
        self.line("smith", "qwen", self.held[:1], at=ago(1))
        self.line("foreman", "haiku", list(self.held), at=ago(9))
        self.assertEqual(louhi.gap_pending(self.p), self.run_id, "due again after a week")


DRAW = r"""
const B = require(process.argv[1]);
function ctx() {
  const st = {text: [], ys: [], dots: 0, bad: [], path: []}, o = {};
  const g = new Proxy(o, {
    get(_, k) {
      if (k in o) return o[k];
      if (k === "measureText") return (s) => ({width: String(s).length * 5});
      if (k === "moveTo" || k === "lineTo") return (x, y) => { if (![x, y].every(isFinite)) st.bad.push(k + " non-finite"); st.path.push([x, y]); };
      if (k === "stroke") return () => { if (o.strokeStyle !== "grid") st.ys.push(...st.path.map((q) => q[1])); st.path = []; };
      if (k === "beginPath") return () => { st.path = []; };
      if (k === "fillText") return (s, x, y) => st.text.push(String(s));
      if (k === "arc") return (x, y, r) => { st.dots++; if (![x, y, r].every(isFinite) || r < 0) st.bad.push("arc"); };
      return () => {};
    },
    set(_, k, v) { o[k] = v; return true; },
  });
  return {g, st};
}
const T = new Proxy({}, {get: (_, k) => (k === "grid" ? "grid" : "#abcdef")});
const base = {agents: [], models: [], links: [], gpu: null, cpm: 0, loaded: 0};
function run(gap, w = 360, h = 230) {
  const {g, st} = ctx(), d = Object.assign({}, base, {gap});
  B.DRAW.models.base(g, d, w, h, T, {});
  return st;
}
const empty = run([]), one = run([{name: "p", pts: [4]}]);
const neg = run([{name: "n", pts: [10, -5, 3]}]);
const h = 230, pad = 14, gh = 54, top = h - pad - gh + 12, bot = top + gh - 22, hi = 10, lo = -5;
const zero = bot - (bot - top) * (0 - lo) / (hi - lo);
console.log(JSON.stringify({empty: {text: empty.text}, one: {text: one.text, dots: one.dots},
  neg: {ys: neg.ys, top, bot, zero, bad: neg.bad}}));
"""
