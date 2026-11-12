"""T-4223 (big tiers part D, T-4219): a big task runs as one turn and one report. While its tree has a ready or
landed-unreviewed child, the Stop hook holds the session once per tree state; a tree only the user can unblock ends
in one report line. Drive off, paused, or no big task in focus: quiet. sampo big status, estimate and report show the
tree (n of m, time, turns, cost against an estimate), and the status line carries the segment."""
import copy
import json
import os
import sys
import time
from unittest import mock

from helpers import ForemanTestCase

from sampo import core as c
from sampo import cost, hooks

DESIGN = ("The parser reads blocks. interface: parse(text) -> list of dicts, in lib/parser.py\n"
          "The runner calls parse. interface: run(blocks) -> int, in lib/runner.py\n"
          "Nothing outside lib/ may change.\n")
C1 = ("[c1] Parser\ntype: FEATURE\ntier: M\nfiles: lib/parser.py, tests/test_parser.py\ncovers: 1\n"
      "criterion: the parser reads one block :: grep -q parse lib/parser.py\nstep: write parse() in lib/parser.py\n")
C2 = ("[c2] Runner\ntype: FEATURE\ntier: S\nfiles: lib/runner.py\ncovers: 2\n"
      "criterion: the runner returns a status :: grep -q run lib/runner.py\n")
C3 = ("[c3] Wire them\ntype: FEATURE\ntier: S\nfiles: lib/parser.py\nafter: c1, c2\ncovers: 3\n"
      "criterion: wired :: grep -q wire lib/parser.py\nstep: wire parse into run\n")
INTEGRATION = "criterion: parse and run work together :: grep -q together lib/runner.py\n"


class BigCase(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.fm("drive", "on")
        self.fm("autonomy", "full")
        self.p = c.find_project(self.repo)

    def big(self):
        """An XL task with a full plan, expanded into three children, and focused."""
        self.fm("task", "new", "Rework everything", "--type", "FEATURE", "--tier", "L",
                "--ac", "first thing :: true", "--step", "plan, expand, land the children")
        tid = "T-%04d" % max(c.id_num(b.id) for b in c.load_briefs(self.p))
        for n in (2, 3):
            self.fm("task", "ac", tid, "add", f"criterion {n} :: true")
        b = c.find_brief(self.p, tid)
        b.meta["tier"] = "XL"
        c.save_brief(self.p, b)
        options = ("Reading 1: a. Reading 2: b. 1) a; fails if: x 2) use what exists: b; fails if: y 3) do less: c; "
                   "fails if: z Choice 1 (80%): a")
        for name, text in (("Interpretation", "Reading 1: split the parser and runner. Reading 2: keep one module. "
                            "Ruled out: one module, too big to review."), ("Approach (options → choice → why)", options),
                           ("Risks and rollback", "A bad split: revert the children."),
                           ("Design", DESIGN), ("Breakdown", C1 + "\n" + C2 + "\n" + C3), ("Integration", INTEGRATION)):
            self.fm("task", "set", tid, "--section", name, "--text", text)
        self.fm("big", "expand", tid)
        self.fm("focus", tid)
        return tid

    def kids(self, tid):
        return sorted((b for b in c.load_briefs(self.p) if b.meta.get("parent") == tid), key=lambda b: b.id)

    def set_kid(self, kid, **fields):
        b = c.find_brief(self.p, kid.id)
        b.meta.update(fields)  # status lives in meta
        c.save_brief(self.p, b)

    def finish(self, tid):
        """Close a task through the real finish path, with every audit lens and the docs and lesson it demands."""
        lenses = [f"{n}: fine" for n in ("intent", "adversary", "edge", "operator", "maintainer")]
        args = [a for n in lenses for a in ("--lens", n)]
        return self.fm("task", "finish", tid, "--audit", "self", *args, "--docs", "none: test",
                       "--lesson", "none: test", "--run", "true")

    def stop(self, sid="sess-1", msg="Working on it."):
        return self.hook("Stop", {"stop_hook_active": False, "last_assistant_message": msg, "session_id": sid}).stdout

    def reason(self, out):
        data = json.loads(out) if out.strip() else {}
        return data.get("reason", "") if data.get("decision") == "block" else ""

    def holds(self, n=3):
        return [r for r in (self.reason(self.stop()) for _ in range(n)) if "big task" in r]


class BigTurn(BigCase):
    def test_a_focused_big_task_with_a_ready_child_holds_the_stop_once_per_tree_state_turn(self):
        tid = self.big()
        first = self.holds()
        self.assertEqual(len(first), 1, "one hold for the tree state, not one per Stop")
        self.assertIn(tid, first[0])
        self.assertIn(self.kids(tid)[0].id, first[0], "it names the child to take up next")
        self.set_kid(self.kids(tid)[0], status="done")  # the tree changed: a new state holds once more
        again = self.holds()
        self.assertEqual(len(again), 1)
        self.assertIn("1 of 3", again[0])

    def test_a_landed_unreviewed_child_continues_the_big_task_turn(self):
        tid = self.big()
        self.holds()  # the first state is said
        kid = self.kids(tid)[1]
        self.set_kid(kid, status="verifying", lane="lanes/a", lane_stop="merged 2026-11-11T10:00")
        got = self.holds()
        self.assertEqual(len(got), 1)
        self.assertIn(kid.id, got[0])
        self.assertIn("landed", got[0], "a merged child is reviewed and closed, not left landed")

    def test_only_the_user_can_unblock_ends_in_one_report_turn(self):
        tid = self.big()
        for kid in self.kids(tid):
            self.set_kid(kid, status="blocked", user_held="needs a decision on the parser API")
        got = self.holds()
        self.assertEqual(len(got), 1, "one report line, then the stop is released")
        self.assertIn("needs you", got[0])

    def test_the_hold_releases_after_its_time_budget_turn(self):
        self.big()
        env = {"FOREMAN_BIG_HOLD_S": "0"}  # a zero budget: the first hold is already past it
        got = [self.reason(self.hook("Stop", {"stop_hook_active": False, "last_assistant_message": "x",
                                              "session_id": "sess-1"}, env=env).stdout) for _ in range(3)]
        bigs = [r for r in got if "big task" in r]
        self.assertEqual(len(bigs), 1, "one report at the budget, then released")
        self.assertIn("time budget", bigs[0])

    def test_a_child_in_focus_is_not_offered_again_turn(self):
        tid = self.big()
        first, second = self.kids(tid)[0], self.kids(tid)[1]
        self.fm("focus", first.id)  # the child is being worked: the hold names the next ready one
        got = self.holds()
        self.assertEqual(len(got), 1)
        self.assertIn(f"take up {second.id}", got[0])
        self.assertNotIn(f"take up {first.id}", got[0])

    def test_a_dropped_child_counts_as_finished_turn(self):
        tid = self.big()
        self.set_kid(self.kids(tid)[0], status="dropped")
        got = self.holds()
        self.assertEqual(len(got), 1)
        self.assertIn("1 of 3 children done", got[0], "a dropped child is finished, as the status counts it")

    def test_a_tree_that_returns_to_a_said_state_says_nothing_turn(self):
        tid = self.big()
        kid = self.kids(tid)[0]
        before = kid.status
        self.assertEqual(len(self.holds()), 1)  # state A is said
        self.set_kid(kid, status="done")
        self.assertEqual(len(self.holds()), 1)  # state B is said
        self.set_kid(kid, status=before)
        self.assertEqual(self.holds(), [], "back in state A: already said, so quiet")

    def test_a_budget_gap_of_a_whole_budget_starts_a_new_streak_turn(self):
        tid = self.big()
        briefs = c.load_briefs(self.p)
        budget = hooks.BIG_HOLD_S
        now = time.time()
        g = {"big": {tid: {"first": now - 3 * budget, "last": now - 2 * budget, "seen": []}}}  # a streak long over
        text = hooks._big_hold(self.p, {"drive": True}, briefs, {"session_id": "sess-1"}, g)
        self.assertIn("take up", text, "a fresh streak after a quiet gap is not the budget")

    def test_the_budget_is_reported_once_then_released_turn(self):
        tid = self.big()
        now = time.time()
        g = {"big": {tid: {"first": now - hooks.BIG_HOLD_S - 60, "last": now - 1, "seen": []}}}
        pl, sd = {"session_id": "sess-1"}, {"drive": True}
        self.assertIn("time budget", hooks._big_hold(self.p, sd, c.load_briefs(self.p), pl, g))
        self.set_kid(self.kids(tid)[0], status="done")
        self.assertIsNone(hooks._big_hold(self.p, sd, c.load_briefs(self.p), pl, g), "the budget was said once")

    def test_a_non_numeric_budget_falls_back_to_the_default_turn(self):
        self.big()
        with mock.patch.dict(os.environ, {"FOREMAN_BIG_HOLD_S": "soon"}):
            text = hooks._big_hold(self.p, {"drive": True}, c.load_briefs(self.p), {"session_id": "sess-1"}, {})
        self.assertIn("take up", text, "a bad budget is the default, and the hold still runs")

    def test_children_across_the_digit_boundary_are_taken_in_number_order_turn(self):
        tid = self.big()
        parent, kid = c.find_brief(self.p, tid), self.kids(tid)[0]

        def child(n):  # the same plan, only the number differs: T-999 sorts before T-1000 by number, after it as text
            b = copy.deepcopy(kid)
            b.meta.update(id=f"T-{n}", parent=tid, status="planned")
            b.meta.pop("lane_stop", None)
            return b
        briefs = [parent, child(1000), child(999)]  # listed out of order: the hook sorts by number
        text = hooks._big_hold(self.p, {"drive": True}, briefs, {"session_id": "sess-1"}, {})
        self.assertIn("take up T-999", text, "a text sort would name T-1000 first")
        self.assertNotIn("take up T-1000", text)


class BigQuiet(BigCase):
    def test_drive_off_gives_no_big_hold_quiet(self):
        self.big()
        self.fm("drive", "off")
        self.assertEqual(self.holds(), [])

    def test_paused_gives_no_big_hold_quiet(self):
        self.big()
        self.fm("pause", "on")
        self.assertEqual(self.holds(), [])
        self.fm("pause", "off")

    def test_no_big_focus_reads_no_children_and_writes_no_hold_quiet(self):
        self.fm("task", "new", "Small fix", "--type", "FIX", "--tier", "S", "--ac", "works :: true",
                "--step", "fix", "--focus")
        self.assertEqual(self.holds(), [])
        self.assertEqual(list(c.ledger_events(self.p, kinds={"big_hold"})), [])

    def test_no_big_focus_reads_nothing_beyond_the_briefs_quiet(self):
        self.fm("task", "new", "Small fix", "--type", "FIX", "--tier", "S", "--ac", "works :: true",
                "--step", "fix", "--focus")
        briefs = c.load_briefs(self.p)
        with mock.patch.object(c, "ledger_tail", side_effect=AssertionError("read")), \
                mock.patch.object(c, "ledger_events", side_effect=AssertionError("read")), \
                mock.patch.object(cost, "_read_cache", side_effect=AssertionError("read")):
            self.assertIsNone(hooks._big_hold(self.p, {"drive": True}, briefs, {"session_id": "s"}, {}))

    def test_the_drive_task_worker_is_quiet_quiet(self):
        self.big()
        with mock.patch.dict(os.environ, {"FOREMAN_DRIVE_TASK": "T-0001"}):
            self.assertIsNone(hooks._big_hold(self.p, {"drive": True}, c.load_briefs(self.p),
                                              {"session_id": "sess-1"}, {}))

    def test_the_big_segment_loads_no_bigview_without_a_big_task_quiet(self):
        self.fm("task", "new", "Small fix", "--type", "FIX", "--tier", "S", "--ac", "works :: true",
                "--step", "fix", "--focus")
        sys.modules.pop("sampo.bigview", None)
        self.assertIsNone(c._big_segment(c.load_briefs(self.p), None))
        self.assertNotIn("sampo.bigview", sys.modules, "a status with no big task imports nothing of the big view")


class BigStatus(BigCase):
    def test_big_status_shows_n_of_m_time_turns_and_cost_against_estimate_status(self):
        tid = self.big()
        self.set_kid(self.kids(tid)[0], status="done")
        data = json.loads(self.fm("big", "status", tid, "--json").stdout)
        self.assertEqual((data["done"], data["total"]), (1, 3))
        for key in ("time_min", "attempts", "cost", "estimate"):
            self.assertIn(key, data)
        self.assertIn("1 of 3", self.fm("big", "status", tid).stdout)

    def test_a_child_closed_by_finish_reports_its_real_minutes_and_attempts_status(self):
        tid = self.big()
        for name, text in (("lib/parser.py", "def parse(text):\n    return []\n"),
                           ("lib/runner.py", "def run(blocks):\n    return 0\n")):  # the children's criteria grep these
            os.makedirs(os.path.join(self.repo, os.path.dirname(name)), exist_ok=True)
            with open(os.path.join(self.repo, name), "w", encoding="utf-8") as f:
                f.write(text)
        kid = self.kids(tid)[0]
        self.fm("focus", kid.id)
        self.finish(kid.id)
        data = json.loads(self.fm("big", "status", tid, "--json").stdout)
        row = {k["id"]: k for k in data["children"]}
        self.assertEqual(row[kid.id]["attempts"], 1, "one focus, no failed finish: one attempt, as the close logged")
        self.assertIsInstance(row[kid.id]["minutes"], int)
        self.assertIsNone(row[self.kids(tid)[1].id]["minutes"], "an open child has no minutes yet: unknown, not 0")
        self.assertEqual(data["attempts"], 1)
        self.assertEqual(data["time_min"], row[kid.id]["minutes"])
        self.assertEqual(data["done"], 1)

    def test_a_cost_newer_than_the_cache_is_unknown_status(self):
        tid = self.big()
        self.write_cost_cache(ts=1.0, costs={tid: 5})  # made before the tree existed
        data = json.loads(self.fm("big", "status", tid, "--json").stdout)
        self.assertEqual(data["cost_unknown"], 4, "the parent and its three children are all newer than the cache")
        self.assertIsNone(data["cost"], "unknown is not 0")
        self.assertIn("newer than the cost cache", self.fm("big", "status", tid).stdout)

    def test_a_cost_made_after_the_tree_is_known_status(self):
        tid = self.big()
        self.write_cost_cache(ts=time.time() + 3600, costs={tid: 5})
        data = json.loads(self.fm("big", "status", tid, "--json").stdout)
        self.assertEqual((data["cost"], data["cost_unknown"]), (5, 0))

    def write_cost_cache(self, ts, costs):
        path = cost._cache_path(self.p)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        c.write_atomic(path, json.dumps({"size": 0, "ts": ts, "costs": costs}))

    def test_big_status_all_lists_every_big_task_status(self):
        tid = self.big()
        self.assertIn(tid, self.fm("big", "status", "--all").stdout)

    def test_big_estimate_without_history_says_so_status(self):
        tid = self.big()
        self.assertIn("no history", self.fm("big", "estimate", tid).stdout)

    def test_the_status_line_carries_the_big_task_and_its_wave_status(self):
        tid = self.big()
        self.assertRegex(self.fm("status").stdout, rf"{tid} XL 0/3 · wave 1")
        for kid in self.kids(tid)[:2]:
            self.set_kid(kid, status="done")
        self.assertRegex(self.fm("status").stdout, rf"{tid} XL 2/3 · wave 2")

    def test_big_report_names_every_child_and_its_state_status(self):
        tid = self.big()
        self.set_kid(self.kids(tid)[0], status="done")
        out = self.fm("big", "report", tid).stdout
        for kid in self.kids(tid):
            self.assertIn(kid.id, out)
        self.assertIn("done", out)

    def test_closing_the_big_task_appends_one_close_row_status(self):
        from sampo import bigview
        tid = self.big()
        for kid in self.kids(tid):
            self.set_kid(kid, status="done")
        self.finish(tid)  # the close path writes the row
        rows = [json.loads(x) for x in open(bigview.jsonl_path(self.p), encoding="utf-8") if x.strip()]
        self.assertEqual([r["id"] for r in rows], [tid])
        self.assertEqual((rows[0]["children"], rows[0]["done"]), (3, 3))
