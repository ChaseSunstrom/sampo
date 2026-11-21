"""T-4201: open concerns order the queue (T-4188 part B). A task linked to an open concern is worked first, the most
said first, then the oldest; the active task, an urgent one and a dependency still come first, and with no concerns the
order is exactly today's. `sampo next`, `sampo queue` and `sampo status` name the concern; nothing a hook runs reads
the ledger of concerns. No test here calls a model or the network."""
import datetime
import json
import os
import unittest
from unittest import mock

from helpers import ForemanTestCase

from sampo import concerns as cn
from sampo import core as c


def _b(id, type, status="planned", priority="normal", deps=()):
    b = c.Brief.new(id, f"task {id}", type, "S", now="2026-01-01T00:00:00Z")
    b.meta["status"] = status
    b.meta["priority"] = priority
    b.meta["depends_on"] = list(deps)
    return b


def link(cid, said, opened="2026-11-10T10:00:00Z", text="it reaches GitHub"):
    return {"id": cid, "said": said, "opened": opened, "text": text}


def ids(q):
    return [b.id for b in q[0]]


class OrderKey(unittest.TestCase):
    def test_order_linked_tasks_first_most_said_first(self):
        briefs = [_b("T-0001", "FEATURE"), _b("T-0002", "FIX"), _b("T-0003", "CLEAN"), _b("T-0004", "RESEARCH")]
        got = c.order_queue(briefs, concerns={"T-0001": link("K-AAAAAA", 5), "T-0003": link("K-BBBBBB", 2)})
        self.assertEqual(ids(got), ["T-0001", "T-0003", "T-0004", "T-0002"],
                         "five-times-said first, then twice-said, then today's order for the rest")

    def test_order_equal_said_takes_the_oldest_concern_first(self):
        briefs = [_b("T-0001", "FIX"), _b("T-0002", "FIX")]
        got = c.order_queue(briefs, concerns={"T-0001": link("K-AAAAAA", 2, opened="2026-11-10T12:00:00Z"),
                                              "T-0002": link("K-BBBBBB", 2, opened="2026-11-09T12:00:00Z")})
        self.assertEqual(ids(got), ["T-0002", "T-0001"])

    def test_order_a_concern_breaks_a_group_that_would_keep_its_members_together(self):
        briefs = [_b("T-0001", "FIX"), _b("T-0002", "FIX"), _b("T-0003", "FIX")]
        for b in briefs[:2]:
            b.meta["group"] = "g"
        got = c.order_queue(briefs, concerns={"T-0003": link("K-AAAAAA", 3)})
        self.assertEqual(ids(got)[0], "T-0003")

    def test_order_no_concerns_is_todays_order(self):
        briefs = [_b("T-0001", "FEATURE"), _b("T-0002", "FIX"), _b("T-0003", "CLEAN"),
                  _b("T-0004", "SECURITY"), _b("T-0005", "PERFORMANCE"), _b("T-0006", "RESEARCH")]
        today = ids(c.order_queue(briefs))
        self.assertEqual(today, ["T-0006", "T-0003", "T-0005", "T-0004", "T-0002", "T-0001"])
        self.assertEqual(ids(c.order_queue(briefs, concerns={})), today)
        self.assertEqual(ids(c.order_queue(briefs, concerns={"T-9999": link("K-AAAAAA", 4)})), today,
                         "a concern linked to a task that is not queued changes nothing")

    def test_hoist_active_and_urgent_still_come_first(self):
        briefs = [_b("T-0001", "FEATURE"), _b("T-0002", "FEATURE", priority="urgent"), _b("T-0003", "FIX", status="active")]
        got = c.order_queue(briefs, concerns={"T-0001": link("K-AAAAAA", 9)})
        self.assertEqual(ids(got), ["T-0003", "T-0002", "T-0001"])

    def test_hoist_a_dependency_still_comes_before_the_task_that_waits_on_it(self):
        briefs = [_b("T-0001", "FEATURE", deps=["T-0002"]), _b("T-0002", "CLEAN")]
        got = c.order_queue(briefs, concerns={"T-0001": link("K-AAAAAA", 9)})
        self.assertEqual(ids(got), ["T-0002", "T-0001"])

    def test_hoist_an_unlinked_dependency_is_not_pulled_ahead_of_other_linked_work(self):
        briefs = [_b("T-0001", "FEATURE", deps=["T-0002"]), _b("T-0002", "CLEAN"), _b("T-0003", "FIX")]
        got = c.order_queue(briefs, concerns={"T-0001": link("K-AAAAAA", 9), "T-0003": link("K-BBBBBB", 1)})
        self.assertEqual(ids(got), ["T-0002", "T-0001", "T-0003"], "the dependency, then the task it unblocks")


class Verbs(ForemanTestCase):
    """The real flow: a message the meter booked, a capture made from it, and the verbs that read them."""

    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)
        self.enterContext(mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": os.path.join(self.tmp, "cfg")}))

    def plan(self, title, type="FEATURE"):
        return self.fm_json("task", "new", title, "--type", type, "--tier", "S", "--ac", "ok :: true", "--step", "a")["id"]

    def say(self, uuid, at_s_ago, concern="the work reaches GitHub", repeat_of=None, session="sess-1"):
        t = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=at_s_ago)
        r = {"session": session, "uuid": uuid, "at": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "model": "fake",
             "text": f"say {uuid}", "kind": "steer", "avoidable": True, "root": "R7", "missed_thought": "x",
             "why": "y", "concern": concern, "surface": "the push"}
        if repeat_of:
            r["repeat_of"] = repeat_of
        c.append_line(os.path.join(self.p.dir, "steers.jsonl"), json.dumps(r))

    def capture_from(self, tid, session="sess-1", source="user"):
        c.log_event(self.p, "capture", task=tid, data={"source": source, "type": "FEATURE"}, session=session)

    def test_order_an_agents_own_discovered_capture_is_not_the_user_waiting(self):
        """T-4663: a task the agent captured for an unrelated bug in the same session is not ranked first."""
        self.plan("plain fix", "FIX")
        found = self.plan("an unrelated bug the agent found", "FIX")
        self.say("u1", 300)
        self.capture_from(found, source="discovered")
        data = self.fm_json("queue")
        self.assertEqual([x["id"] for x in data["order"]], ["T-0001", found])
        self.assertNotIn("concern", data["order"][1])

    def test_order_a_dropped_task_is_not_ranked_by_its_concern(self):
        two, cid = self.setup_two()
        b = c.find_brief(self.p, two)
        b.meta["status"] = "dropped"
        c.save_brief(self.p, b)
        self.assertEqual(cn.queue_links(self.p), {})
        self.assertEqual(self.fm_json("concerns")["concerns"][0]["tasks"], [], "open for action again")

    def setup_two(self):
        """T-0001 is the older FIX; T-0002 a FEATURE linked to a concern said twice: it comes first."""
        self.plan("plain fix", "FIX")
        two = self.plan("publish it", "FEATURE")
        self.say("u1", 300)
        self.say("u2", 200, repeat_of="say u1")
        self.capture_from(two)
        return two, cn.concern_id("sess-1", "u1")

    def test_order_the_queue_verb_puts_the_linked_task_first_and_names_the_concern(self):
        two, cid = self.setup_two()
        data = self.fm_json("queue")
        self.assertEqual([x["id"] for x in data["order"]], [two, "T-0001"])
        self.assertEqual(data["order"][0]["concern"]["id"], cid)
        self.assertEqual(data["order"][0]["concern"]["said"], 2)
        self.assertNotIn("concern", data["order"][1])
        text = self.fm("queue").stdout.splitlines()
        self.assertTrue(text[0].startswith(f"1. {two}"), text)
        self.assertIn(f"{cid} said 2x", "\n".join(text), "the concern is named beside the task")

    def test_order_the_next_verb_names_the_top_concern_and_its_task(self):
        two, cid = self.setup_two()
        got = self.fm_json("next")
        self.assertEqual(got["task"], two)
        self.assertEqual(got["concern"]["id"], cid)
        self.assertIn(cid, got["action"])
        self.assertIn(two, got["action"])
        self.assertIn("said 2x", got["action"])

    def test_order_a_dependency_of_a_linked_task_comes_first_and_says_who_waits(self):
        self.plan("plain fix", "FIX")
        base = self.fm_json("task", "new", "groundwork", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true",
                            "--step", "a")["id"]
        top = self.fm_json("task", "new", "publish it", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true",
                           "--step", "a", "--depends", base)["id"]
        self.say("u1", 300)
        self.capture_from(top)
        got = self.fm_json("queue")["order"]
        self.assertEqual([x["id"] for x in got][:2], [base, top], "the groundwork, then the task the user waits on")
        self.assertEqual(got[0]["concern"]["via"], top)
        self.assertEqual(self.fm_json("next")["task"], base)
        self.assertIn(top, self.fm_json("next")["action"])

    def test_order_with_no_concerns_the_verbs_keep_todays_order_and_say_nothing(self):
        one = self.plan("plain fix", "FIX")
        two = self.plan("a feature", "FEATURE")
        data = self.fm_json("queue")
        self.assertEqual([x["id"] for x in data["order"]], [one, two])
        self.assertNotIn("concern", data["order"][0])
        self.assertNotIn("K-", self.fm("queue").stdout)
        got = self.fm_json("next")
        self.assertEqual(got["task"], one)
        self.assertIsNone(got["concern"])
        self.assertNotIn("K-", got["action"])

    def test_order_a_captured_task_linked_to_a_concern_comes_before_a_queued_one(self):
        self.plan("plain fix", "FIX")
        cap = self.fm_json("capture", "publish it", "--type", "FEATURE", "--tier", "S")
        cap = cap["id"] if isinstance(cap, dict) else cap
        self.say("u1", 300)
        self.capture_from(cap)
        got = self.fm_json("next")
        self.assertEqual(got["task"], cap, "a task the user's ask made waits in the inbox: it is not behind the queue")

    def test_order_status_has_one_line_and_json_carries_the_counts(self):
        self.setup_two()
        self.say("u3", 100, concern="the login page works")
        text = self.fm("status").stdout
        line = next(x for x in text.splitlines() if x.startswith("Open concerns"))
        self.assertIn("2 open", line)
        self.assertIn("1 said twice or more", line)
        self.assertIn("oldest K-", line)
        got = self.fm_json("status")
        self.assertEqual(got["concerns"]["open"], 2)
        self.assertEqual(got["concerns"]["twice"], 1)
        quiet = self.fm("status", "--line").stdout
        self.assertNotIn("concern", quiet.lower(), "the status bar's poll reads nothing new")

    def test_order_status_says_nothing_with_no_concerns(self):
        self.plan("plain fix", "FIX")
        self.assertNotIn("Open concerns", self.fm("status").stdout)

    def state_text(self):
        with open(os.path.join(self.p.dir, "STATE.md"), encoding="utf-8") as f:
            return f.read()

    def _next(self, links):
        return c.next_for(self.p, c.load_briefs(self.p), checks=False, concerns=links)

    def test_lead_the_top_concern_comes_before_a_finished_lane_waiting_on_a_merge(self):
        """T-4217: an idle session was told to merge first and the concern only showed in the task pick."""
        two, cid = self.setup_two()
        from sampo import lanes as slanes
        with mock.patch.object(slanes, "awaiting_merge", return_value=["T-0099"]):
            _, _, action = self._next(cn.queue_links(self.p))
            self.assertTrue(action.startswith("the user is waiting on "), action)
            self.assertLess(action.index(cid), action.index("lane merge"), "the concern, then the merge")
            self.assertLess(action.index(two), action.index("lane merge"), "its task is the pick, the merge follows")
            _, _, plain = self._next(None)
            self.assertTrue(plain.startswith("builder lane(s) finished"), "no concerns: today's order, unchanged")
            self.assertNotIn("the user is waiting", plain)

    def test_lead_the_top_concern_comes_before_the_self_improvement_pass(self):
        """T-4217: a due friction pass was returned before the queue was read, so the concern never showed."""
        from sampo import friction
        two, cid = self.setup_two()
        with mock.patch.object(friction, "due", return_value=True):
            b, st, action = self._next(cn.queue_links(self.p))
            self.assertEqual(b.id, two)
            self.assertNotEqual(st, "reflect")
            self.assertTrue(action.startswith("the user is waiting on "), action)
            self.assertIn(cid, action)
            self.assertNotIn(friction.ACTION, action)
            self.assertIn("self-improvement pass waits", action, "the pass is deferred in words, not dropped")
            b, st, action = self._next(None)
            self.assertEqual(st, "reflect", "no concerns: the pass comes as it did")
            self.assertEqual(action, friction.ACTION)

    def test_lead_a_concern_whose_task_the_lane_cannot_pick_leaves_the_pass_and_merge_first(self):
        from sampo import friction
        self.setup_two()
        with mock.patch.object(friction, "due", return_value=True):
            _, st, action = self._next({"T-9999": link("K-ZZZZZZ", 3)})
            self.assertEqual(st, "reflect", "a link to a task that is not here changes nothing")
            self.assertNotIn("the user is waiting", action)

    def test_lead_a_concern_task_held_by_a_lane_does_not_lead_through_its_dependency(self):
        """T-4217: the pick and the lead read one view: a lane-held concern task links nothing in the main checkout."""
        from sampo import friction
        two, cid = self.setup_two()
        briefs = c.load_briefs(self.p)
        held = next(x for x in briefs if x.id == two)
        held.meta["lane"] = "lane-x"
        held.meta["depends_on"] = ["T-0001"]
        links = cn.queue_links(self.p)
        with mock.patch.object(friction, "due", return_value=True):
            _, st, _ = c.next_for(self.p, briefs, checks=False, concerns=links)
        self.assertEqual(st, "reflect", "no link on the pick, so the friction pass is not deferred")
        from sampo import lanes as slanes
        with mock.patch.object(slanes, "awaiting_merge", return_value=["T-0099"]):
            b, _, action = c.next_for(self.p, briefs, checks=False, concerns=links)
        self.assertEqual(b.id, "T-0001")
        self.assertNotIn("the user is waiting", action, "the lead reads the same view as the pick")
        self.assertTrue(action.startswith("builder lane(s) finished"), action)

    def test_lead_the_state_and_a_replan_carry_the_concern_order(self):
        two, cid = self.setup_two()
        got = self.fm("queue", "--replan").stdout
        self.assertIn(cid, got)
        state = self.state_text()
        lines = state.splitlines()
        self.assertTrue(any("waiting" in x and cid in x and two in x for x in lines[:4]), "the first lines name it: " + state)
        queue = lines[lines.index(next(x for x in lines if x.startswith("## Queue"))) + 1:]
        self.assertTrue(queue[0].startswith(f"1. {two}"), queue[:3])

    def test_lead_next_writes_the_concern_order_into_the_state(self):
        two, cid = self.setup_two()
        self.fm("next")
        state = self.state_text()
        self.assertIn(cid, state)

    def test_lead_the_state_with_no_concerns_is_unchanged(self):
        self.plan("plain fix", "FIX")
        self.fm("queue", "--replan")
        state = self.state_text()
        self.assertNotIn("waiting", state)
        self.assertNotIn("K-", state)

    def test_lead_an_answered_concern_leaves_the_state_at_once(self):
        two, cid = self.setup_two()
        self.fm("next")
        self.assertIn(cid, self.state_text())
        self.fm("concerns", "answer", cid, "it is a question, answered")
        self.assertIsNone(c.state_dict(self.p)["waiting"], "no stale link survives the answer")
        self.assertNotIn(cid, self.state_text(), "the answer itself regenerates STATE.md")
        self.fm("queue", "--replan")
        self.assertNotIn(cid, self.state_text())

    def test_lead_a_stale_or_broken_link_file_costs_the_order_never_the_state(self):
        two, cid = self.setup_two()
        path = os.path.join(self.p.dir, "concern_links.json")
        for junk in ("not json", "[1, 2]", '{"links": 7}', '{"links": {"T-0002": 5}, "at": "2020-01-01T00:00:00Z"}'):
            with open(path, "w", encoding="utf-8") as f:
                f.write(junk)
            self.assertIsNone(c.state_dict(self.p)["waiting"], junk)
            self.assertEqual([q["id"] for q in c.state_dict(self.p)["queue"]], ["T-0001", two], junk)

    def test_order_a_hook_path_never_reads_the_concerns(self):
        self.setup_two()
        briefs = c.load_briefs(self.p)
        with mock.patch.object(cn, "fold", side_effect=AssertionError("a hook read the concerns")), \
                mock.patch.object(cn, "open_concerns", side_effect=AssertionError("a hook read the concerns")):
            c.order_queue(briefs)
            c.next_for(self.p, briefs, checks=False)
            c.state_dict(self.p)


if __name__ == "__main__":
    unittest.main()
