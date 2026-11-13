"""T-4189: a repeated report means the cause model is wrong. A concern said again after its linked task closed (or after
a shown proof) reopens that task, or captures a linked FIX, with a forced first step: look at the real surface before
any change. The close of such a task refuses on unit-test evidence alone, and a shown number that is worse a day later
reopens the concern. Rows are written as the meter writes them (steers.jsonl); whether two reports are one concern is
what concerns.fold already decides. No test here calls a model or the network."""
import datetime
import json
import os
import subprocess
import sys
from unittest import mock

from helpers import ForemanTestCase

from sampo import concerns as cn
from sampo import core as c


def stamp(hours=0.0):
    t = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


class Repeat(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)
        self.enterContext(mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": os.path.join(self.tmp, "cfg")}))

    def book(self, uuid, text, at, concern="the work reaches GitHub", surface="the push", repeat_of=None):
        r = {"session": "sess-1", "uuid": uuid, "at": at, "model": "fake", "text": text, "kind": "steer",
             "avoidable": True, "root": "R7", "missed_thought": "check the live page", "why": "it said done",
             "concern": concern, "surface": surface}
        if repeat_of is not None:
            r["repeat_of"] = repeat_of
        c.append_line(os.path.join(self.p.dir, "steers.jsonl"), json.dumps(r, ensure_ascii=False))

    def linked_task(self, cid, closed_hours_ago=None, title="push it"):
        """A task whose raw request names the concern, closed (status done and a task_done event) that long ago."""
        tid = json.loads(self.fm("capture", f"{title} ({cid})", "--type", "FEATURE", "--tier", "S", "--json").stdout)["id"]
        if closed_hours_ago is not None:
            b = c.find_brief(self.p, tid)
            b.meta["status"] = "done"
            c.save_brief(self.p, b)
            with mock.patch.dict(os.environ, {"FOREMAN_NOW": stamp(closed_hours_ago)}):
                c.log_event(self.p, "task_done", task=tid, data={})
        return tid

    def chain(self, first_h=30, again_h=1, **kw):
        """Said first_h hours ago, said again again_h hours ago: one concern, its id."""
        self.book("u1", "why isn't it pushed", stamp(first_h), **kw)
        self.book("u2", "it still isn't pushed", stamp(again_h), repeat_of="why isn't it pushed", **kw)
        return cn.fold(self.p)[0]["id"]

    def age(self, hours):
        """Move every event of the concerns file back to that many hours ago."""
        path = os.path.join(self.p.dir, cn.CONCERNS)
        rows = [json.loads(x) for x in open(path)]
        for r in rows:
            r["ts"] = stamp(hours)
        with open(path, "w") as f:
            f.write("".join(json.dumps(r) + "\n" for r in rows))

    def brief(self, tid):
        return c.find_brief(self.p, tid)

    def surface_step(self, b):
        return next((s for s in b.steps() if s.text.startswith(cn.SURFACE_STEP)), None)

    def commit(self, msg):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "--allow-empty", "-q", "-m", msg],
                       cwd=self.repo, check=True)
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=self.repo, capture_output=True,
                              text=True, check=True).stdout.strip()


class Reopen(Repeat):
    def test_reopen_a_repeat_after_the_close_reopens_the_task_with_the_surface_step(self):
        cid = cn.concern_id("sess-1", "u1")
        tid = self.linked_task(cid, closed_hours_ago=10)
        sha = self.commit(f"push it ({tid})")
        self.chain(first_h=30, again_h=1)
        got = cn.repeated(self.p)
        self.assertEqual([(x["id"], x["trigger"], x["task"]) for x in got], [(cid, "closed", tid)])
        done = cn.act(self.p)
        self.assertEqual([x["action"] for x in done], ["reopened"])
        b = self.brief(tid)
        self.assertEqual(b.status, "planned", "the closed task is open again")
        s = self.surface_step(b)
        self.assertIsNotNone(s)
        self.assertTrue(s.current and not s.done, "the surface step is the one to do next")
        self.assertIn(cid, s.text)
        log = b.section("Log")
        self.assertIn("reported again on", log)
        self.assertIn("the earlier cause was wrong or incomplete", log)
        self.assertIn(f"earlier fix: {sha}", log)

    def test_reopen_acts_once_per_repeat_and_again_for_the_next(self):
        cid = cn.concern_id("sess-1", "u1")
        tid = self.linked_task(cid, closed_hours_ago=10)
        self.chain(first_h=30, again_h=1)
        self.assertEqual(len(cn.act(self.p)), 1)
        self.assertEqual(cn.act(self.p), [], "a second pass finds it handled")
        self.assertEqual(len([s for s in self.brief(tid).steps() if s.text.startswith(cn.SURFACE_STEP)]), 1)
        self.assertEqual(cn.repeated(self.p), [])
        b = self.brief(tid)  # closed again, then said again
        b.meta["status"] = "done"
        c.save_brief(self.p, b)
        with mock.patch.dict(os.environ, {"FOREMAN_NOW": stamp(0.5)}):
            c.log_event(self.p, "task_done", task=tid, data={})
        self.book("u3", "still not pushed", stamp(0.1), repeat_of="it still isn't pushed")
        self.assertEqual([x["action"] for x in cn.act(self.p)], ["reopened"])
        self.assertEqual(len([s for s in self.brief(tid).steps() if s.text.startswith(cn.SURFACE_STEP)]), 1,
                         "one surface step is kept, not one more per repeat")

    def test_reopen_a_close_older_than_a_week_captures_a_linked_fix(self):
        cid = cn.concern_id("sess-1", "u1")
        old = self.linked_task(cid, closed_hours_ago=24 * 10)
        self.chain(first_h=24 * 12, again_h=1)
        done = cn.act(self.p)
        self.assertEqual([x["action"] for x in done], ["captured"])
        self.assertEqual(self.brief(old).status, "done", "the old close stays closed")
        new = self.brief(done[0]["task"])
        self.assertEqual(new.type, "FIX")
        self.assertIn(cid, new.section("Raw request"), "named by id, so the ledger links it")
        self.assertIsNotNone(self.surface_step(new))
        self.assertIn(done[0]["task"], next(x for x in cn.fold(self.p) if x["id"] == cid)["tasks"])

    def test_reopen_said_before_the_close_is_not_a_repeat(self):
        cid = cn.concern_id("sess-1", "u1")
        self.linked_task(cid, closed_hours_ago=1)
        self.chain(first_h=30, again_h=5)  # both said before the close
        self.assertEqual(cn.repeated(self.p), [])
        self.assertEqual(cn.act(self.p), [])

    def test_reopen_after_a_shown_proof_marks_the_proof_refuted(self):
        self.book("u1", "why isn't it pushed", stamp(30))
        cid = cn.concern_id("sess-1", "u1")
        open(os.path.join(self.repo, "proof.txt"), "w").close()
        cn.show(self.p, cid, "path:proof.txt")
        self.age(2)
        self.book("u2", "it still isn't pushed", stamp(0), repeat_of="why isn't it pushed")
        got = cn.repeated(self.p)
        self.assertEqual([(x["id"], x["trigger"]) for x in got], [(cid, "proof")])
        self.assertEqual(got[0]["refuted"], "path:proof.txt", "the proof is kept, as refuted")
        x = cn.fold(self.p)[0]
        self.assertEqual((x["state"], x["refuted"]), ("open", "path:proof.txt"))
        self.assertEqual([y["action"] for y in cn.act(self.p)], ["captured"], "no linked task: a FIX is captured")

    def test_reopen_a_concern_with_no_surface_is_exempt_and_says_so(self):
        cid = cn.concern_id("sess-1", "u1")
        tid = self.linked_task(cid, closed_hours_ago=10)
        self.chain(first_h=30, again_h=1, surface="none")
        done = cn.act(self.p)
        self.assertEqual([x["action"] for x in done], ["reopened"])
        b = self.brief(tid)
        self.assertEqual(b.status, "planned")
        self.assertIsNone(self.surface_step(b), "nothing to look at: no forced step")
        self.assertIn("no reachable surface", b.section("Log"))
        self.assertEqual(cn.surface_gaps(self.p, b), [])

    def test_reopen_the_tick_acts_and_a_quiet_ledger_costs_nothing(self):
        cid = cn.concern_id("sess-1", "u1")
        tid = self.linked_task(cid, closed_hours_ago=10)
        self.chain(first_h=30, again_h=1)
        got = cn.tick(self.p)
        self.assertEqual([x["action"] for x in got["repeats"]], ["reopened"])
        with mock.patch.object(cn, "fold", side_effect=AssertionError("folded")):
            self.assertEqual(cn.tick(self.p)["repeats"], [], "nothing new since the last pass: the ledger is not folded")
        self.assertEqual(self.brief(tid).status, "planned")


class Gate(Repeat):
    CID = "K-ABC123"

    def reopened(self):
        """A task carrying the surface step for the concern, as act leaves it."""
        tid = json.loads(self.fm("capture", "push it", "--type", "FIX", "--tier", "S", "--json").stdout)["id"]
        b = self.brief(tid)
        b.add_step(f"{cn.SURFACE_STEP} ({self.CID}): look at the real surface")
        c.save_brief(self.p, b)
        return tid, self.brief(tid)

    def step_n(self, b):
        return self.surface_step(b).n

    def test_gate_a_task_without_the_step_is_never_asked(self):
        tid = json.loads(self.fm("capture", "plain", "--type", "FIX", "--tier", "S", "--json").stdout)["id"]
        with mock.patch.object(cn, "_events", side_effect=AssertionError("read")):
            self.assertEqual(cn.surface_gaps(self.p, self.brief(tid)), [])

    def test_gate_no_evidence_refuses_and_says_what_counts(self):
        _, b = self.reopened()
        got = cn.surface_gaps(self.p, b)
        self.assertEqual(len(got), 1)
        self.assertIn(self.CID, got[0])
        self.assertIn("sampo concerns show", got[0])
        self.assertIn("sampo smoke", got[0])

    def test_gate_unit_test_evidence_alone_is_refused_and_named(self):
        tid, b = self.reopened()
        b.add_evidence("python3 plugin/tests/run.py test_x", "exit 0", step=self.step_n(b), ran=True)
        c.save_brief(self.p, b)
        got = cn.surface_gaps(self.p, self.brief(tid))
        self.assertEqual(len(got), 1)
        self.assertIn("not a surface run", got[0])
        self.assertIn("python3 plugin/tests/run.py test_x", got[0])

    def test_gate_a_surface_run_passes_and_a_failed_one_does_not(self):
        tid, b = self.reopened()
        n = self.step_n(b)
        b.add_evidence("sampo smoke", "✗ exit 1", step=n, ran=True)
        c.save_brief(self.p, b)
        self.assertEqual(len(cn.surface_gaps(self.p, self.brief(tid))), 1, "a red surface run proves nothing")
        b = self.brief(tid)
        b.add_evidence("python3 plugin/bin/sampo shot http://localhost:1/", "exit 0", step=n, ran=True)
        c.save_brief(self.p, b)
        self.assertEqual(cn.surface_gaps(self.p, self.brief(tid)), [])

    def test_gate_a_typed_line_is_not_a_run(self):
        tid, b = self.reopened()
        b.add_evidence("sampo smoke", "exit 0", step=self.step_n(b), ran=False)
        c.save_brief(self.p, b)
        self.assertEqual(len(cn.surface_gaps(self.p, self.brief(tid))), 1)

    def test_gate_the_command_the_close_is_about_to_run_counts(self):
        _, b = self.reopened()
        n = self.step_n(b)
        self.assertEqual(cn.surface_gaps(self.p, b, then=[(n, "curl -sf https://example.org/")]), [])
        self.assertEqual(len(cn.surface_gaps(self.p, b, then=[(n, "python3 -m unittest")])), 1)
        self.assertEqual(len(cn.surface_gaps(self.p, b, then=[(n + 1, "sampo smoke")])), 1, "another step's run is not it")

    def test_gate_a_project_names_its_own_surface_commands(self):
        tid, b = self.reopened()
        b.add_evidence("make deploy-staging", "exit 0", step=self.step_n(b), ran=True)
        c.save_brief(self.p, b)
        self.assertEqual(len(cn.surface_gaps(self.p, self.brief(tid))), 1)
        m = c.read_meta(self.p)
        m["surface_commands"] = ["make deploy-staging"]
        c.write_meta(self.p, m)
        self.assertEqual(cn.surface_gaps(self.p, self.brief(tid)), [])

    def test_gate_a_concern_shown_after_the_reopen_counts(self):
        self.book("u1", "why isn't it pushed", stamp(30))
        cid = cn.concern_id("sess-1", "u1")
        tid = json.loads(self.fm("capture", "push it", "--type", "FIX", "--tier", "S", "--json").stdout)["id"]
        b = self.brief(tid)
        b.add_step(f"{cn.SURFACE_STEP} ({cid}): look at the real surface")
        c.save_brief(self.p, b)
        self.assertEqual(len(cn.surface_gaps(self.p, self.brief(tid))), 1)
        open(os.path.join(self.repo, "proof.txt"), "w").close()
        cn.show(self.p, cid, "path:proof.txt")
        self.assertEqual(cn.surface_gaps(self.p, self.brief(tid)), [])

    def test_gate_finish_refuses_a_task_with_only_unit_test_evidence(self):
        tid, b = self.reopened()
        b.add_evidence("python3 plugin/tests/run.py test_x", "exit 0", step=self.step_n(b), ran=True)
        c.save_brief(self.p, b)
        r = self.fm("task", "finish", tid, "--audit", "self", "--run", "true", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not a surface run", r.stderr + r.stdout)
        self.assertEqual(self.brief(tid).status, c.find_brief(self.p, tid).status)
        self.assertNotEqual(self.brief(tid).status, "done")

    def test_gate_task_done_is_no_way_round_it(self):
        tid, b = self.reopened()
        r = self.fm("task", "done", tid, check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(self.CID, r.stderr + r.stdout)
        self.assertIn("surface", r.stderr + r.stdout)


class Remeasure(Repeat):
    def script(self, value, line="cpu {} percent"):
        path = os.path.join(self.repo, "measure.py")
        runs = os.path.join(self.repo, "measure.runs")
        with open(path, "w") as f:
            f.write(f"open({runs!r}, 'a').write('x')\nprint('header 7')\nprint({line!r}.format({value!r}))\n")
        return f"{sys.executable} {path}"

    def runs(self):
        try:
            return len(open(os.path.join(self.repo, "measure.runs")).read())
        except OSError:
            return 0

    def shown(self, value, direction="", age_h=25):
        self.book("u1", "why is the CPU at 100%", stamp(60), concern="the CPU is calm", surface="top")
        cid = cn.concern_id("sess-1", "u1")
        cn.show(self.p, cid, f"num:{value}{direction} via {self.script(value)}")
        self.age(age_h)
        return cid

    def test_remeasure_a_worse_number_a_day_later_reopens_the_concern(self):
        cid = self.shown(80)
        self.script(95)
        got = cn.remeasure(self.p)
        self.assertEqual([(x["id"], x["before"], x["after"], x["worse"]) for x in got], [(cid, 80.0, 95.0, True)])
        x = cn.fold(self.p)[0]
        self.assertEqual((x["state"], x["reopened"]), ("open", True))
        self.assertTrue(x["refuted"].startswith("num:80"), "the shown proof is kept, as refuted")
        self.assertEqual([(y["id"], y["trigger"]) for y in cn.repeated(self.p)], [(cid, "remeasure")])

    def test_remeasure_a_better_or_equal_number_keeps_it_shown_and_is_not_asked_again(self):
        self.shown(80)
        self.script(60)
        self.assertEqual([x["worse"] for x in cn.remeasure(self.p)], [False])
        self.assertEqual(cn.fold(self.p)[0]["state"], "shown")
        self.script(99)
        self.assertEqual(cn.remeasure(self.p), [], "once: a shown number is re-checked a single time")
        self.assertEqual(self.runs(), 2, "the show's run and the one re-check")

    def test_remeasure_waits_for_the_day(self):
        self.shown(80, age_h=2)
        self.script(95)
        self.assertEqual(cn.remeasure(self.p), [])
        self.assertEqual(self.runs(), 1, "only the show's own run")

    def test_remeasure_higher_is_better_when_the_proof_says_up(self):
        self.shown(80, direction=" up")
        self.script(60)
        self.assertEqual([x["worse"] for x in cn.remeasure(self.p)], [True])

    def test_remeasure_a_small_wobble_is_not_worse(self):
        self.shown(80)
        self.script(82)
        self.assertEqual([x["worse"] for x in cn.remeasure(self.p)], [False])

    def test_remeasure_a_reading_it_cannot_find_reopens_nothing(self):
        self.shown(80)
        self.script("n/a", line="cpu {}")  # the line's shape changed
        got = cn.remeasure(self.p)
        self.assertEqual([(x["worse"], x["after"]) for x in got], [(False, None)])
        self.assertEqual(cn.fold(self.p)[0]["state"], "shown")

    def test_remeasure_a_concern_shown_with_a_path_is_never_re_run(self):
        self.book("u1", "why isn't it pushed", stamp(60))
        open(os.path.join(self.repo, "proof.txt"), "w").close()
        cn.show(self.p, cn.concern_id("sess-1", "u1"), "path:proof.txt")
        self.age(30)
        self.assertEqual(cn.remeasure(self.p), [])

    def test_remeasure_the_tick_reopens_the_linked_task(self):
        cid = self.shown(80)
        tid = self.linked_task(cid, closed_hours_ago=40)
        self.script(95)
        got = cn.tick(self.p)
        self.assertEqual([x["id"] for x in got["remeasured"]], [cid])
        self.assertEqual([x["action"] for x in got["repeats"]], ["reopened"])
        self.assertEqual(self.brief(tid).status, "planned")
        self.assertIsNotNone(self.surface_step(self.brief(tid)))
