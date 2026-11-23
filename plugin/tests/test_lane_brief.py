"""T-5774: lanes.lane_fn_overlap (T-5725) unpacked _defs as three values after T-5682 made it return four, so `sampo lane
brief` raised ValueError for any task with a code scope. A scoped Python file plus another lane changing one of its defs
gives the warning line."""
# map-links: plugin/lib/sampo/lanes.py
import contextlib
import io
import json
import os
import re
import subprocess
import types
import unittest

from helpers import ForemanTestCase

from sampo import core as c
from sampo import lanes

G = ["git", "-c", "user.name=t", "-c", "user.email=t@localhost", "-c", "commit.gpgsign=false"]


class FnOverlap(ForemanTestCase):
    def git(self, cwd, *a):
        return subprocess.run(G + list(a), cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()

    def test_fn_overlap_warns_for_a_def_another_lane_changes_instead_of_raising(self):
        main = os.path.join(self.tmp, "main")
        os.makedirs(main)
        self.git(main, "init", "-q")
        with open(os.path.join(main, "a.py"), "w") as f:
            f.write("def keep():\n    return 1\n\n\ndef frobnicate():\n    return 2\n")
        self.git(main, "add", "-A")
        self.git(main, "commit", "-qm", "base")
        base = self.git(main, "rev-parse", "HEAD")
        lane = os.path.join(self.tmp, "lane")
        self.git(main, "worktree", "add", "-q", "-b", "other", lane)
        with open(os.path.join(lane, "a.py"), "w") as f:
            f.write("def keep():\n    return 1\n\n\ndef frobnicate():\n    return 3\n")
        self.git(lane, "commit", "-qam", "change frobnicate")
        step = types.SimpleNamespace(text="make `frobnicate` faster")
        mine = types.SimpleNamespace(id="T-1", title="Speed up frobnicate", meta={"scope": ["a.py"]}, steps=lambda: [step])
        other = types.SimpleNamespace(id="T-2", title="Other work", status="active", meta={"lane": lane})
        out = lanes.lane_fn_overlap(None, mine, main, base, [mine, other])
        self.assertEqual(len(out), 1, out)
        self.assertIn("frobnicate in a.py", out[0])
        self.assertIn("T-2", out[0])


class SizeCap(ForemanTestCase):
    """T-6047: a builder brief whose task-owned text is past the cap is refused at plan time, with the split printed."""

    def setUp(self):
        super().setUp()
        self.fm("init")

    TITLE = 'Cap "probe" $HOME it\'s `x`'  # what a printed command must quote

    def mk(self, n_ac, width, title=None, steps=3):
        args = ["task", "new", title or self.TITLE, "--type", "FEATURE", "--tier", "S", "--json"]
        for i in range(steps):
            args += ["--step", f"do part {i + 1}"]
        for i in range(n_ac):
            args += ["--ac", f"criterion {i + 1} " + "word " * width + ":: true"]
        return json.loads(self.fm(*args).stdout)["id"]

    def project(self):
        return c.find_project(self.repo)

    def brief_path(self, tid):
        return os.path.join(self.project().dir, "audits", f"{tid}.builder.md")

    def refused(self, tid, *more):
        r = self.fm("lane", "brief", tid, *more, check=False)
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(self.brief_path(tid)), "a refused brief is not written")
        return r.stdout + r.stderr

    def test_over_the_cap_is_refused_with_the_split_and_the_measured_reason(self):
        tid = self.mk(8, 250)  # eight criteria of ~1.3k chars: ~10k of the task's own text
        out = self.refused(tid)
        self.assertIn("93%", out)
        self.assertIn("48%", out)
        self.assertIn("part 1/", out)
        self.assertIn("part 2/", out)
        self.assertIn("--depends", out, "the parts are chained")
        self.assertIn("--oversize", out, "the override is named")
        self.assertTrue(int(re.findall(r"part \d+/(\d+)", out)[0]) >= 2, out)
        for size in re.findall(r"~(\d+) chars", out):
            self.assertLessEqual(int(size), lanes.OWN_CAP, out)

    def test_the_printed_script_runs_and_makes_chained_parts_that_each_fit(self):
        tid = self.mk(8, 250)
        out = self.refused(tid)
        script = re.search(r"sh (\S+)", out).group(1).strip("'")
        env = dict(os.environ, FOREMAN_HOME=self.home, FOREMAN_INHIBITED="1", FOREMAN_DOCTOR_BENCH="0", FOREMAN_DOCTOR_BUILD="0")
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        r = subprocess.run(["sh", script], cwd=self.repo, env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        p = self.project()
        made = [b for b in c.load_briefs(p) if b.id != tid]
        self.assertGreaterEqual(len(made), 2, r.stdout)
        made.sort(key=lambda b: b.id)
        self.assertEqual([b.title for b in made], [f"{self.TITLE} (part {k}/{len(made)})" for k in range(1, len(made) + 1)])
        self.assertEqual(made[1].meta.get("depends_on"), [made[0].id], "part 2 is chained to part 1")
        self.assertEqual(sum(len(b.acceptance()) for b in made), 8, "every criterion lands once")
        for b in made:  # what the builder is given, not the function's own estimate
            self.assertLessEqual(lanes.own_chars(b), lanes.OWN_CAP, b.id)
        brief = self.fm("lane", "brief", made[0].id)  # part 1 renders: no refusal
        self.assertTrue(os.path.exists(self.brief_path(made[0].id)), brief.stdout)

    def test_why_does_not_lift_the_cap(self):
        tid = self.mk(8, 250)
        self.assertIn("--oversize", self.refused(tid, "--why", "land backlog"))

    def test_a_reason_overrides_the_refusal_and_is_logged_on_one_line(self):
        tid = self.mk(8, 250)
        r = self.fm("lane", "brief", tid, "--oversize", "one indivisible\nmigration\n- [x] forged")
        self.assertTrue(os.path.exists(self.brief_path(tid)), r.stdout + r.stderr)
        self.assertIn("past the 8000 cap: briefed anyway", r.stdout)
        self.assertNotIn("split it, or keep it in the main thread", r.stdout, "the old advice contradicts the override")
        p = self.project()
        hits = [e for e in c.ledger_events(p, ("lane_brief_oversize",)) if e.get("task") == tid]
        self.assertEqual(len(hits), 1, hits)
        self.assertEqual(hits[0]["data"]["why"], "one indivisible migration - [x] forged")
        log = [ln for ln in c.find_brief(p, tid).section("Log").splitlines() if "briefed anyway" in ln]
        self.assertEqual(len(log), 1, log)
        self.assertIn("one indivisible migration - [x] forged", log[0])

    def test_a_batch_refusal_is_one_line_and_oversize_does_not_lift_it(self):
        tid = self.mk(8, 250)
        r = self.fm("lane", "brief", "--ready", "--oversize", "all of them", "--json")
        skipped = json.loads(r.stdout)["skipped"]
        row = next(x for x in skipped if x["id"] == tid)
        self.assertNotIn("\n", row["reason"])
        self.assertIn(f"sampo lane brief {tid}", row["reason"])
        self.assertFalse(os.path.exists(self.brief_path(tid)))
        self.assertFalse(os.path.exists(os.path.join(self.project().dir, "audits", f"{tid}.split.sh")), "no file from a batch")

    def test_a_resume_of_a_built_lane_is_not_refused_and_the_skip_is_logged(self):
        tid = self.mk(8, 250)
        p = self.project()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            lanes.builder_brief(p, c.find_brief(p, tid), types.SimpleNamespace(json=True, model=None), resume_branch="lane/x")
        self.assertTrue(os.path.exists(self.brief_path(tid)))
        hits = [e for e in c.ledger_events(p, ("lane_brief_oversize",)) if e.get("task") == tid]
        self.assertEqual(len(hits), 1, hits)
        self.assertIn("resume", hits[0]["data"]["why"])

    def test_under_the_cap_is_unchanged(self):
        tid = self.mk(2, 40)
        r = self.fm("lane", "brief", tid)
        self.assertTrue(os.path.exists(self.brief_path(tid)))
        self.assertNotIn("93%", r.stdout + r.stderr)
        self.assertNotIn("part 1/", r.stdout + r.stderr)
        self.assertEqual(c.ledger_events(self.project(), ("lane_brief_oversize",)), [])

    def test_the_split_packs_criteria_and_steps_in_order_each_part_under_the_cap(self):
        tid = self.mk(8, 250, steps=5)
        parts = lanes.split_criteria(c.find_brief(self.project(), tid), lanes.OWN_CAP)
        self.assertGreaterEqual(len(parts), 2)
        self.assertEqual([a.n for g in parts for a in g["acs"]], list(range(1, 9)), "every criterion once, in order")
        self.assertEqual([s.n for g in parts for s in g["steps"]], list(range(1, 6)), "every step once, in order")
        for g in parts:
            self.assertLessEqual(g["chars"], lanes.OWN_CAP)

    def test_no_split_names_what_is_too_big(self):
        one = self.mk(1, 1700)  # one criterion of ~8.5k chars: nothing to split by
        self.assertIn("nothing to split by", self.refused(one))
        shared = self.mk(3, 250, title="Shared heavy")
        self.fm("task", "set", shared, "--section", "Interpretation", "--text", "reading " * 1000)
        out = self.refused(shared)
        self.assertIn("every part repeats them", out)
        self.assertNotIn("nothing to split by", out)


if __name__ == "__main__":
    unittest.main()
