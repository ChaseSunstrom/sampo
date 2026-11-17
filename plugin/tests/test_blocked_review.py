"""T-3303: blocked work never sits. The review frees a blocked task held only by tasks that all closed, and one held only
by capacity (a model reads the reason; no answer keeps the block), sampo task block refuses a capacity-only reason, and
a block over 48 h that can't move is named on the Next line with one proposed action, the user-only ones told once a
day. The model is replaced by a stub here: blocked._ask is the one place a model is asked."""
import contextlib
import io
import os
from unittest import mock

from helpers import ForemanTestCase

from sampo import blocked
from sampo import cli
from sampo import core as c

T0 = "2026-11-01T00:00:00Z"  # when the blocks are written
LATER = "2026-11-03T02:00:00Z"  # 50 hours on: past STALE_H


class Stub:
    """blocked._ask's stand-in: answers by kind; counts the questions. sure=False answers under the bar; down=True is a
    backend that gives no answer."""

    def __init__(self, holds=None, action=None, sure=True, down=False):
        self.answers = {"block-holds": holds, "block-stale": action}
        self.sure, self.down, self.asked = sure, down, []

    def __call__(self, p, kind, prompt, choices, **kw):
        self.asked.append(kind)
        got = self.answers.get(kind)
        got = got(prompt) if callable(got) else got
        if self.down or not got:
            return None
        return {"choice": got, "probability": 0.9 if self.sure else 0.5, "cleared": self.sure}


class BlockedReview(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        for t in ("Build the parser", "Wire the parser in", "Ship the docs"):
            self.fm("task", "new", t, "--type", "FEATURE", "--tier", "S", "--ac", "works :: true", "--step", "do it")
        self.p = c.find_project(self.repo)

    def block(self, tid, reason, at=T0, *more):
        self.fm("task", "block", tid, reason, *more, env={"FOREMAN_NOW": at})

    def review(self, stub=None, now=LATER, **kw):
        with mock.patch.object(blocked, "_ask", stub or Stub()), mock.patch.dict(os.environ, {"FOREMAN_NOW": now}):
            return blocked.review(self.p, **kw)

    def status(self, tid):
        return c.find_brief(self.p, tid).status

    def test_closed_dependencies_and_no_reason_free_it_without_a_model(self):
        self.fm("task", "set", "T-0002", "depends_on+=T-0001")
        self.block("T-0002", "")
        stub = Stub()
        self.assertEqual(self.review(stub, now="2026-11-01T02:00:00Z")["freed"], [])  # T-0001 still open
        self.assertEqual(self.status("T-0002"), "blocked")
        self.fm("task", "drop", "T-0001", "done elsewhere")
        res = self.review(stub, now="2026-11-01T04:00:00Z")
        self.assertEqual([x["id"] for x in res["freed"]], ["T-0002"])
        self.assertEqual(self.status("T-0002"), "planned")
        self.assertIn("unblocked: blocked review: every task it waited on is closed (T-0001)",
                      c.find_brief(self.p, "T-0002").section("Log"))
        self.assertEqual(stub.asked, [])

    def test_a_reason_naming_closed_tasks_frees_it_when_read_as_tasks_only(self):
        self.block("T-0003", "waits for T-0001 to land")
        self.fm("task", "drop", "T-0001", "done elsewhere")
        res = self.review(Stub(holds="tasks"))
        self.assertEqual([x["id"] for x in res["freed"]], ["T-0003"])
        self.assertEqual(self.status("T-0003"), "planned")

    def test_closed_dependencies_with_a_real_reason_stay_blocked(self):
        self.fm("task", "set", "T-0002", "depends_on+=T-0001")
        self.fm("task", "drop", "T-0001", "done elsewhere")
        self.block("T-0002", "needs the user's API key")  # its dependency closed long ago: that isn't what holds it
        self.review(Stub(holds="real"))
        self.assertEqual(self.status("T-0002"), "blocked")
        self.review(Stub(), now="2026-11-03T04:00:00Z")  # nor with no answer
        self.assertEqual(self.status("T-0002"), "blocked")

    def test_every_named_id_must_close(self):
        self.block("T-0003", "waits for T-0001 and T-0002")
        self.fm("task", "drop", "T-0001", "done elsewhere")
        stub = Stub(holds="tasks")
        res = self.review(stub)
        self.assertEqual(self.status("T-0003"), "blocked")  # T-0002 is still open: a tasks reading can't free it
        self.assertEqual(res["stale"][0]["waits_on"], ["T-0002"])
        self.block("T-0002", "waits for T-0099")  # an id no task has is never read as closed
        self.review(Stub(holds="tasks"), now="2026-11-03T05:00:00Z")
        self.assertEqual(self.status("T-0002"), "blocked")

    def test_a_capacity_only_block_goes_back_to_planned(self):
        self.block("T-0003", "no free builder slot right now")
        res = self.review(Stub(holds="capacity"))
        self.assertEqual([x["id"] for x in res["freed"]], ["T-0003"])
        self.assertEqual(self.status("T-0003"), "planned")
        self.assertIn("capacity is never a block", c.find_brief(self.p, "T-0003").section("Log"))

    def test_capacity_frees_it_beside_an_open_task_it_names_and_tasks_does_not(self):
        self.block("T-0003", "no builder slot: T-0002 holds the lane")
        self.review(Stub(holds="tasks"))
        self.assertEqual(self.status("T-0003"), "blocked")  # T-0002 is open: a tasks reading can't free it
        self.block("T-0003", "no builder slot while T-0002 holds the lane", LATER)
        self.review(Stub(holds="capacity"), now="2026-11-03T03:00:00Z")
        self.assertEqual(self.status("T-0003"), "planned")

    def test_verdicts_of_tasks_a_pass_skips_or_never_reaches_are_kept(self):
        self.block("T-0001", "the vendor's API key hasn't arrived")
        self.block("T-0002", "the user hasn't said which domain to use")
        self.review(Stub(holds="real", action="ask"))
        before = blocked.load(self.p)["verdicts"]
        self.assertEqual(set(before), {"T-0001", "T-0002"})
        with mock.patch.object(blocked, "_action", side_effect=RuntimeError("boom")), \
                mock.patch.object(blocked, "_may_ask", return_value=True):
            with self.assertRaises(RuntimeError):
                self.review(Stub(holds="real", action="ask"), now="2026-11-03T05:00:00Z")
        after = blocked.load(self.p)
        self.assertEqual(after["verdicts"]["T-0002"], before["T-0002"])  # never reached: carried over
        self.assertEqual({x["id"] for x in after["stale"]}, {"T-0001", "T-0002"})

    def test_a_block_until_a_date_is_left_alone_until_then(self):
        self.block("T-0003", "no free builder slot right now", T0, "--until", "2026-12-01")
        stub = Stub(holds="capacity", action="ask")
        res = self.review(stub)
        self.assertEqual((self.status("T-0003"), res["stale"], stub.asked), ("blocked", [], []))
        self.review(stub, now="2026-12-01T09:00:00Z")
        self.assertEqual(self.status("T-0003"), "planned")

    def test_a_real_block_is_kept_and_its_verdict_is_not_asked_again(self):
        self.block("T-0003", "the vendor's API key hasn't arrived")
        stub = Stub(holds="real", action="ask")
        self.review(stub)
        self.review(stub, now="2026-11-03T04:00:00Z")
        self.assertEqual(self.status("T-0003"), "blocked")
        self.assertEqual(stub.asked.count("block-holds"), 1)  # kept per (task, reason)
        self.assertEqual(stub.asked.count("block-stale"), 1)

    def test_no_answer_keeps_the_block_and_ends_the_reviews_questions(self):
        for tid in ("T-0001", "T-0002", "T-0003"):
            self.block(tid, f"no free builder slot for {tid}")
        stub = Stub(down=True)
        self.review(stub, now="2026-11-01T02:00:00Z")
        self.assertEqual(len(stub.asked), 1)  # a backend that gives no answer isn't asked twice in one review
        self.review(stub, now="2026-11-01T04:00:00Z")
        self.assertEqual(len(stub.asked), 2)  # and is asked again next time
        self.assertEqual({self.status(t) for t in ("T-0001", "T-0002", "T-0003")}, {"blocked"})

    def test_an_answer_under_the_bar_keeps_the_block_and_waits_before_asking_again(self):
        self.block("T-0003", "no free builder slot right now")
        stub = Stub(holds="capacity", sure=False)
        self.review(stub, now="2026-11-01T02:00:00Z")
        self.review(stub, now="2026-11-01T04:00:00Z")
        self.assertEqual((self.status("T-0003"), stub.asked.count("block-holds")), ("blocked", 1))
        self.review(Stub(holds="capacity"), now="2026-11-01T09:00:00Z")
        self.assertEqual(self.status("T-0003"), "planned")

    def test_dry_run_changes_nothing_and_writes_no_record(self):
        self.block("T-0003", "the user hasn't said which domain to use")
        told = []
        with mock.patch("sampo.selfcheck.notify", lambda p, text: told.append(text)):
            res = self.review(Stub(holds="capacity"), apply=False)
        self.assertEqual([x["id"] for x in res["freed"]], ["T-0003"])
        self.assertEqual((self.status("T-0003"), told, blocked.load(self.p)), ("blocked", [], {}))
        with mock.patch.dict(os.environ, {"FOREMAN_NOW": LATER}):
            self.assertTrue(blocked.due(self.p))  # the hourly review still runs on time

    def test_a_task_blocked_again_after_the_review_freed_it_stays_blocked_a_day(self):
        self.block("T-0003", "no free builder slot right now")
        self.review(Stub(holds="capacity"))
        self.block("T-0003", "still no slot, holding it", LATER)
        self.review(Stub(holds="capacity"), now="2026-11-03T05:00:00Z")
        self.assertEqual(self.status("T-0003"), "blocked")
        self.review(Stub(holds="capacity"), now="2026-11-04T03:00:00Z")
        self.assertEqual(self.status("T-0003"), "planned")

    def test_a_failure_part_way_keeps_the_record(self):
        self.block("T-0002", "the vendor's API key hasn't arrived")
        self.block("T-0003", "no free builder slot right now")
        holds = Stub(holds=lambda prompt: "capacity" if "slot" in prompt else "real",  # the stub's own fixture rule
                     action="decide")
        with mock.patch.object(blocked, "_free", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.review(holds)
        self.assertEqual(blocked.load(self.p)["verdicts"]["T-0002"]["holds"], "real")

    def test_task_block_refuses_a_capacity_only_reason(self):
        err = io.StringIO()
        with mock.patch.object(blocked, "_ask", Stub(holds="capacity")), contextlib.chdir(self.repo), \
                contextlib.redirect_stderr(err):
            rc = cli.main(["task", "block", "T-0001", "waiting on the merge queue"])
        self.assertEqual(rc, 2)
        self.assertIn("capacity is never a block", err.getvalue())
        self.assertEqual(self.status("T-0001"), "planned")
        for holds in ("real", "tasks"):
            with mock.patch.object(blocked, "_ask", Stub(holds=holds)), contextlib.chdir(self.repo), \
                    contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["task", "block", "T-0001", "needs the user's API key"]), 0)
            self.assertEqual(self.status("T-0001"), "blocked")
            self.fm("task", "unblock", "T-0001")

    def test_task_block_asks_one_backend_briefly(self):
        seen = []
        with mock.patch.object(blocked, "_ask", lambda p, kind, prompt, choices, **kw: seen.append(kw)):
            blocked.capacity(self.p, "waiting on the merge queue")
        self.assertEqual(seen, [{"timeout": blocked.BLOCK_TIMEOUT, "n": 1}])

    def test_task_block_keeps_the_block_when_no_model_answers(self):
        self.block("T-0001", "waiting on the merge queue")  # the test machine has no model: no answer
        self.assertEqual(self.status("T-0001"), "blocked")

    def test_a_stale_block_is_named_with_one_action_and_the_user_told_once_a_day(self):
        self.block("T-0003", "the user hasn't said which domain to use")
        told = []
        with mock.patch("sampo.selfcheck.notify", lambda p, text: told.append(text)):
            res = self.review(Stub(holds="real", action="ask"))
            self.review(Stub(holds="real", action="ask"), now="2026-11-03T05:00:00Z")
        self.assertEqual([(x["id"], x["hours"], x["action"], x["user"]) for x in res["stale"]],
                         [("T-0003", 50, "ask", True)])
        self.assertEqual(len(told), 1)
        self.assertIn("T-0003", told[0])
        line = blocked.stale_line(self.p)  # the last review's record: 53 h at 05:00
        self.assertIn("T-0003 blocked 53 h (the user hasn't said which domain to use)", line)
        self.assertIn("the user's one ask", line)
        nxt = self.fm("next")
        self.assertIn("T-0003 blocked 53 h", nxt.stdout + nxt.stderr)

    def test_each_action_has_its_line_and_no_answer_proposes_decide(self):
        for act, words in (("drop", "drop it as superseded"), ("split", "sampo task split T-0003"),
                           ("decide", "sampo decide")):
            with self.subTest(act=act):
                self.setUp()
                self.block("T-0003", "the vendor's API key hasn't arrived")
                self.review(Stub(holds="real", action=act))
                self.assertIn(words, blocked.stale_line(self.p))
        self.setUp()
        self.block("T-0003", "the vendor's API key hasn't arrived")
        self.assertEqual(self.review(Stub())["stale"][0]["action"], "decide")

    def test_a_young_block_is_not_stale_and_no_action_is_asked(self):
        self.block("T-0003", "the vendor's API key hasn't arrived")
        stub = Stub(holds="real", action="ask")
        res = self.review(stub, now="2026-11-01T10:00:00Z")
        self.assertEqual(res["stale"], [])
        self.assertNotIn("block-stale", stub.asked)
        self.assertEqual(blocked.stale_line(self.p), "")

    def test_the_question_count_is_capped_per_review(self):
        for tid in ("T-0001", "T-0002", "T-0003"):
            self.block(tid, f"reason {tid}")
        stub = Stub(holds="real", action="decide")
        self.review(stub, asks=2)
        self.assertEqual(len(stub.asked), 2)

    def test_the_tick_runs_the_review_once_an_hour(self):
        from sampo import night
        self.block("T-0003", "no free builder slot right now")
        with mock.patch.object(blocked, "_ask", Stub(holds="capacity")), \
                mock.patch.dict(os.environ, {"FOREMAN_NOW": LATER}):
            res = night.tick(self.p, fetch_ci=False)
        self.assertEqual(res.get("unblocked"), ["T-0003"])
        self.assertEqual(self.status("T-0003"), "planned")
        with mock.patch.dict(os.environ, {"FOREMAN_NOW": "2026-11-03T02:30:00Z"}):
            self.assertFalse(blocked.due(self.p))
        with mock.patch.dict(os.environ, {"FOREMAN_NOW": "2026-11-03T03:01:00Z"}):
            self.assertTrue(blocked.due(self.p))

    def test_the_verb_reports_the_review(self):
        self.block("T-0003", "no free builder slot right now")
        out = self.fm("blocked", "review", "--dry-run").stdout
        self.assertIn("Would free", out)
        self.assertIn("0 freed, 1 kept", out)  # no model on the test machine: the block stands
        self.fm("blocked", "review")
        self.assertEqual(self.fm_json("blocked", "list")["kept"], ["T-0003"])
