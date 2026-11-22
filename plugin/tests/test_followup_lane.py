"""T-5717: the follow-ups left on a closed task are drained in one batch. Each open follow-up is filed as a task of its own as
before and scored with the process tier from the paths its text names; the small ones (trivial or small, at most 6) become
ONE batch host (tier S, harness plan drain, one criterion with a verify command) that one haiku builder works, the rest
stay filed alone. The land settle and `sampo task follow ID --drain` both call it; the settle holds the project lock."""
# map-links: plugin/lib/sampo/triggers.py
import os
import unittest

from helpers import ForemanTestCase
from test_land_settle import Settle

from sampo import core as c
from sampo import triggers

DOC_ITEM = "FIX: docs/guide.md never says what --drain does"
TEST_ITEM = "FIX: plugin/tests/test_x.py asserts nothing about the second run"
CODE_ITEM = ("FEATURE: plugin/lib/sampo/core.py should read the map once per run and share it between every caller that "
             "asks for the linked tests of a file")


class Drain(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)

    def host(self, items):
        """A task with one open follow-up per item, as the fold writes them."""
        tid = self.fm_json("task", "new", "the host", "--type", "FIX", "--tier", "S")["id"]
        b = c.find_brief(self.p, tid)
        b.set_section(triggers.FOLLOW, "".join(f"- [ ] {t} (f-{i:08x} discovered)\n" for i, t in enumerate(items, 1)))
        c.save_brief(self.p, b)
        return tid

    def drain(self, tid):
        with c.lock(self.p.dir):  # the land settle holds it: the drain must not take it again
            return triggers.drain_follow_ups(self.p, c.find_brief(self.p, tid))

    def tasks(self):
        return c.load_briefs(self.p)

    def marks(self, tid):
        return [x["mark"] for x in triggers.follow_items(c.find_brief(self.p, tid))]


class Batching(Drain):
    def test_two_small_follow_ups_make_one_host_and_the_third_is_filed_alone(self):
        tid = self.host([DOC_ITEM, CODE_ITEM, TEST_ITEM])
        before = {b.id for b in self.tasks()}
        got = self.drain(tid)
        new = [b for b in self.tasks() if b.id not in before]
        hosts = [b for b in new if b.meta.get("batch")]
        self.assertEqual(len(hosts), 1, [b.title for b in new])
        h = hosts[0]
        self.assertEqual(h.tier, "S")
        self.assertEqual(h.meta.get("harness_plan"), "drain")
        acs = h.acceptance()  # each member's own criterion, then the drain's check with its verify command
        self.assertEqual(len(acs), 3)
        self.assertEqual([a.text.split(":")[0] for a in acs[:2]], h.meta["batch"])
        self.assertIn("sampo check --affected", c.verify_of(acs[-1].text))
        self.assertIn("member criterion not met keeps the host open", h.section("Interpretation"))
        self.assertEqual(c.plan_gaps(h), [], "a host a builder can start at once")
        members = [c.find_brief(self.p, m) for m in h.meta["batch"]]
        self.assertEqual(sorted(m.title for m in members), sorted(
            [DOC_ITEM.split(": ", 1)[1], TEST_ITEM.split(": ", 1)[1]]))
        self.assertTrue(all(m.meta.get("batched_in") == h.id for m in members))
        alone = [b for b in new if b is not h and b.id not in h.meta["batch"]]
        self.assertEqual(len(alone), 1)
        self.assertIn("core.py", alone[0].title)
        self.assertFalse(alone[0].meta.get("batched_in"), "the larger one is no member")
        self.assertEqual(got["batch"], h.id)
        self.assertIn(f"sampo lane brief {h.id} --model haiku", got["line"])
        self.assertEqual(len(got["filed"]), 3)

    def test_every_line_is_marked_captured_and_a_second_drain_makes_nothing_new(self):
        tid = self.host([DOC_ITEM, CODE_ITEM, TEST_ITEM])
        self.drain(tid)
        self.assertEqual(len(self.marks(tid)), 3)
        self.assertTrue(all(m.startswith("captured T-") for m in self.marks(tid)), self.marks(tid))
        again = len(self.tasks())
        got = self.drain(tid)
        self.assertEqual(len(self.tasks()), again)
        self.assertIsNone(got["batch"])
        self.assertEqual(got["filed"], [])
        self.assertIn("follow-ups drained", c.find_brief(self.p, tid).section("Log"))

    def test_one_small_one_is_filed_alone_a_batch_is_two_or_more(self):
        tid = self.host([DOC_ITEM, CODE_ITEM])
        got = self.drain(tid)
        self.assertIsNone(got["batch"])
        self.assertFalse([b for b in self.tasks() if b.meta.get("batch")])
        self.assertEqual(len(got["filed"]), 2)

    def test_a_follow_up_naming_no_path_or_a_risky_one_is_filed_alone(self):
        tid = self.host([DOC_ITEM, TEST_ITEM, "FIX: the retry loop forgets to back off",
                         "FIX: plugin/hooks/guard.py lets a doc through"])
        got = self.drain(tid)
        h = c.find_brief(self.p, got["batch"])
        self.assertEqual(len(h.meta["batch"]), 2)
        self.assertEqual(len(got["filed"]), 4)

    def test_at_most_six_go_into_the_host_the_rest_are_filed_alone(self):
        tid = self.host([f"FIX: docs/part{i}.md has a typo" for i in range(8)])
        got = self.drain(tid)
        self.assertEqual(len(c.find_brief(self.p, got["batch"]).meta["batch"]), 6)
        self.assertEqual(len(got["filed"]), 8)
        self.assertEqual(len([b for b in self.tasks() if b.status == "captured" and not b.meta.get("batched_in")
                              and not b.meta.get("batch") and b.title.endswith("has a typo")]), 2)

    def test_a_dropped_host_hands_its_members_back(self):
        tid = self.host([DOC_ITEM, TEST_ITEM])
        h = c.find_brief(self.p, self.drain(tid)["batch"])
        self.fm("task", "drop", h.id, "not wanted")
        self.assertFalse([b for b in self.tasks() if b.meta.get("batched_in")])


    def test_a_host_with_an_unmet_member_criterion_cannot_close_its_members_done(self):
        tid = self.host([DOC_ITEM, TEST_ITEM])
        h = c.find_brief(self.p, self.drain(tid)["batch"])
        self.fm("focus", h.id)
        self.fm("task", "ac", h.id, "check", str(len(h.acceptance())), "--evidence", "true", "ok")  # only the shared check
        for n in range(1, len(h.steps()) + 1):
            self.fm("task", "step", h.id, "done", str(n), "--evidence", "true", "ok")
        self.fm("task", "audit", h.id, "self", "x", "ok")
        self.fm("task", "set", h.id, "--section", "Docs impact", "--text", "none: test")
        r = self.fm("task", "done", h.id, "--lesson", "x", check=False)
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertRegex(r.stdout + r.stderr, r"(?i)criteri")
        self.assertIn(h.meta["batch"][0], r.stdout + r.stderr, "the refusal names the member's unmet criterion")
        self.assertEqual([m.status for m in map(lambda i: c.find_brief(self.p, i), h.meta["batch"])],
                         ["captured", "captured"], "no member was closed done")

    def test_a_failure_after_the_batch_is_made_leaves_no_half_host_and_never_raises(self):
        from unittest import mock
        tid = self.host([DOC_ITEM, TEST_ITEM])
        real = c.save_brief

        def save(p, b, *a, **k):
            if b.meta.get("harness_plan") == "drain":
                raise OSError("disk full")
            return real(p, b, *a, **k)
        with mock.patch.object(c, "save_brief", save):
            got = self.drain(tid)  # no exception
        self.assertIsNone(got["batch"])
        self.assertEqual(len(got["filed"]), 2)
        self.assertTrue(all(m.startswith("captured T-") for m in self.marks(tid)))
        self.assertFalse([b for b in self.tasks() if b.meta.get("batched_in")], "no member is left batched")
        self.assertFalse([b for b in self.tasks() if b.meta.get("batch") and b.status != "dropped"], "no live half host")
        self.assertIn("no batch (disk full)", c.find_brief(self.p, tid).section("Log"))

    def test_the_close_gate_names_the_drain_flag(self):
        tid = self.host([DOC_ITEM])
        self.assertIn("--drain", triggers.follow_gaps(self.p, c.find_brief(self.p, tid))[0])


class Callers(Drain):
    def test_the_flag_on_task_follow_drains_without_a_new_verb(self):
        tid = self.host([DOC_ITEM, TEST_ITEM, CODE_ITEM])
        r = self.fm("task", "follow", tid, "--drain")
        self.assertTrue(all(m.startswith("captured T-") for m in self.marks(tid)), self.marks(tid))
        self.assertEqual(len([b for b in self.tasks() if b.meta.get("batch")]), 1)
        self.assertRegex(r.stdout, r"sampo lane brief T-\d+ --model haiku")
        self.assertEqual(self.fm("task", "follow", tid, "--drain").stdout.count("--model haiku"), 0, "nothing left to drain")

    def test_the_drain_flag_refuses_a_settling_flag_beside_it(self):
        tid = self.host([DOC_ITEM])
        self.assertNotEqual(self.fm("task", "follow", tid, "--drain", "--done", check=False).returncode, 0)

    def test_the_land_settle_calls_it_under_the_lock(self):
        tid = self.host([DOC_ITEM, TEST_ITEM, CODE_ITEM])
        b = c.find_brief(self.p, tid)
        with c.lock(self.p.dir, timeout=2):  # as _settle_mechanical holds it around the call
            self.assertEqual(len(triggers.settle_follow_ups(self.p, b)), 3)
        self.assertEqual(len([x for x in self.tasks() if x.meta.get("batch")]), 1)


class LandDrain(Settle):
    def test_a_land_that_closes_a_task_with_three_follow_ups_drains_them_in_one_batch(self):
        tid = self.lane(1, "1.txt", when="2026-01-01T00:00:00")
        self.fm("task", "set", tid, "scope=1.txt")
        for i, text in enumerate((DOC_ITEM, CODE_ITEM, TEST_ITEM), 1):
            self.follow(tid, text, key=f"{i:08x}")
        self.audited(tid)
        self.returned(tid)
        r = self.land()
        self.assertEqual(self.brief(tid).status, "done", r.stdout + r.stderr)
        self.assertEqual(self.open_follows(tid), [], r.stdout + r.stderr)
        self.assertEqual(len([b for b in self.tasks() if b.meta.get("batch")]), 1, r.stdout + r.stderr)
        self.assertRegex(self.brief(tid).section("Log"), r"follow-ups drained \(land\): batch T-\d+")


if __name__ == "__main__":
    unittest.main()
