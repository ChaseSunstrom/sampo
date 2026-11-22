"""T-5494: the user's own asks go first. A task records whether the user said it (`asked: verbatim`, set where the
user's words become a task) or Sampo derived it; the queue puts asked tasks ahead of every derived one, the longest
waiting first; `sampo queue` shows the split. An old brief with no field is read by the source heuristic. Nothing here
calls a model or the network."""
import unittest

from helpers import ForemanTestCase

from sampo import core as c


def _b(id, type="FIX", created="2026-01-01T00:00:00Z", asked=None, status="planned", priority="normal", **meta):
    b = c.Brief.new(id, f"task {id}", type, "S", now=created)
    b.meta["status"] = status
    b.meta["priority"] = priority
    if asked is not None:
        b.meta["asked"] = asked
    b.meta.update(meta)
    return b


def ids(q):
    return [b.id for b in q[0]]


class Order(unittest.TestCase):
    def test_order_asked_before_derived_oldest_first(self):
        """An asked task made later still beats a derived one made earlier; of two asked, the older first."""
        briefs = [_b("T-0001", "CLEAN", "2026-01-01T00:00:00Z", asked="derived"),
                  _b("T-0002", "FEATURE", "2026-01-03T00:00:00Z", asked="verbatim"),
                  _b("T-0003", "FIX", "2026-01-02T00:00:00Z", asked="verbatim"),
                  _b("T-0004", "RESEARCH", "2026-01-01T12:00:00Z", asked="derived")]
        self.assertEqual(ids(c.order_queue(briefs)), ["T-0003", "T-0002", "T-0004", "T-0001"],
                         "asked by age (not by type), then derived in today's type order")

    def test_order_the_hoists_still_come_first(self):
        briefs = [_b("T-0001", asked="verbatim"), _b("T-0002", asked="derived", priority="urgent"),
                  _b("T-0003", asked="derived", status="active")]
        self.assertEqual(ids(c.order_queue(briefs)), ["T-0003", "T-0002", "T-0001"])

    def test_order_a_dependency_still_precedes_the_asked_task_that_waits(self):
        briefs = [_b("T-0001", asked="verbatim", depends_on=["T-0002"]), _b("T-0002", asked="derived")]
        self.assertEqual(ids(c.order_queue(briefs)), ["T-0002", "T-0001"])

    def test_order_an_asked_task_breaks_a_group_of_derived_ones(self):
        briefs = [_b("T-0001", asked="derived", group="g"), _b("T-0002", asked="derived", group="g"),
                  _b("T-0003", asked="verbatim")]
        self.assertEqual(ids(c.order_queue(briefs))[0], "T-0003")

    def test_order_a_concern_still_breaks_the_tie_below_asked(self):
        briefs = [_b("T-0001", asked="verbatim"), _b("T-0002", asked="derived"), _b("T-0003", asked="derived")]
        link = {"id": "K-AAAAAA", "said": 3, "opened": "2026-11-10T10:00:00Z", "text": "x"}
        self.assertEqual(ids(c.order_queue(briefs, concerns={"T-0003": link})), ["T-0001", "T-0003", "T-0002"])

    def test_order_with_nothing_asked_is_todays_order(self):
        briefs = [_b("T-0001", "FEATURE", asked="derived"), _b("T-0002", "FIX", asked="derived"),
                  _b("T-0003", "CLEAN", asked="derived")]
        self.assertEqual(ids(c.order_queue(briefs)), ["T-0003", "T-0002", "T-0001"])

    def test_order_the_inbox_puts_asked_first_too(self):
        briefs = [_b("T-0001", "CLEAN", status="captured", asked="derived"),
                  _b("T-0002", "FEATURE", "2026-01-05T00:00:00Z", status="captured", asked="verbatim")]
        self.assertEqual([b.id for b in c.rank_inbox(briefs)], ["T-0002", "T-0001"])

    def test_order_an_inbox_ask_waits_oldest_first(self):
        briefs = [_b("T-0001", "FEATURE", "2026-01-05T00:00:00Z", status="captured", asked="verbatim"),
                  _b("T-0002", "FEATURE", "2026-01-02T00:00:00Z", status="captured", asked="verbatim")]
        self.assertEqual([b.id for b in c.rank_inbox(briefs)], ["T-0002", "T-0001"])


class IsAsked(unittest.TestCase):
    def test_field_wins_over_the_source(self):
        self.assertTrue(c.is_asked(_b("T-0001", asked="verbatim", source="discovered")))
        self.assertFalse(c.is_asked(_b("T-0001", asked="derived", source="user")))

    def test_old_brief_a_plain_user_task_is_asked(self):
        self.assertTrue(c.is_asked(_b("T-0001")))
        self.assertTrue(c.is_asked(_b("T-0001", source="intake")))

    def test_old_brief_a_derived_marker_or_another_source_is_derived(self):
        self.assertFalse(c.is_asked(_b("T-0001", carried_from="T-0009")), "a carried follow-up has the user source by default")
        self.assertFalse(c.is_asked(_b("T-0001", inferred_why="T-0002: same file")))
        self.assertFalse(c.is_asked(_b("T-0001", source="discovered")))
        self.assertFalse(c.is_asked(_b("T-0001", source="followup")))
        self.assertFalse(c.is_asked(_b("T-0001", source="genesis")))
        noted = _b("T-0001")
        noted.set_section("Raw request", "> x (noticed by an agent in T-0002)\n")
        self.assertFalse(c.is_asked(noted))

    def test_old_brief_ordering_uses_the_fallback(self):
        briefs = [_b("T-0001", "CLEAN", source="discovered"), _b("T-0002", "FEATURE", "2026-01-03T00:00:00Z")]
        self.assertEqual(ids(c.order_queue(briefs)), ["T-0002", "T-0001"])


class Verbs(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)

    def plan(self, title, **meta):
        tid = self.fm_json("task", "new", title, "--type", "FIX", "--tier", "S", "--ac", "ok :: true", "--step", "a")["id"]
        if meta:
            b = c.find_brief(self.p, tid)
            b.meta.update(meta)
            c.save_brief(self.p, b)
        return tid

    def test_queue_verb_shows_the_split(self):
        derived = self.plan("derived by a builder")
        carried = self.plan("left out of another task", carried_from="T-0099")
        asked = self.plan("what the user said", asked="verbatim")
        data = self.fm_json("queue")
        self.assertEqual(data["split"], {"asked": 1, "derived": 2})
        self.assertEqual([x["id"] for x in data["order"]], [asked, derived, carried])
        flags = {x["id"]: x["asked"] for x in data["order"]}
        self.assertEqual(flags, {asked: True, derived: False, carried: False}, "a carried_from task is derived")
        text = self.fm("queue").stdout
        self.assertIn("Asked by you (1)", text)
        self.assertIn("Derived (2)", text)
        self.assertTrue(text.splitlines()[0].startswith(f"1. {asked}"), "the ask is the first row")

    def test_queue_verb_says_nothing_extra_when_empty(self):
        self.assertNotIn("Asked by you", self.fm("queue").stdout)

    def test_capture_and_intake_mark_the_users_words_and_task_new_does_not(self):
        cap = self.fm_json("capture", "publish the thing", "--type", "FEATURE", "--tier", "S")["id"]
        found = self.fm_json("capture", "an unrelated bug", "--type", "FIX", "--tier", "S", "--source", "discovered")["id"]
        made = self.plan("a builder's own task")
        took = self.fm_json("intake", "FIX: the build breaks on friday")["created"][0]["id"]
        marks = {t: c.find_brief(self.p, t).meta.get("asked") for t in (cap, found, made, took)}
        self.assertEqual(marks, {cap: "verbatim", found: "derived", made: "derived", took: "verbatim"})


if __name__ == "__main__":
    unittest.main()
