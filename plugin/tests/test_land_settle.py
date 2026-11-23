"""T-5535: a land's close settles the mechanical blockers of a merged, audited lane itself: a file edited outside scope gets
a scope reason naming the reviewed lane tip, each open follow-up is captured to the inbox, and a step with no verify
command of its own takes the first criterion's run as its evidence. Each settlement is one Log line; the land's summary
counts the settled closes. A lane with no fresh audit still goes to a hand close, with nothing settled."""
# map-links: plugin/lib/sampo/train.py
import os
import unittest

from test_lane_land import LandCase

from sampo import core as c
from sampo import triggers


class Settle(LandCase):
    def sha(self, n):
        """The short tip of lane n: what the close names as the reviewed tip."""
        return self.git("rev-parse", "--short", "HEAD", cwd=self.lane_path(n)).strip()

    def lane_path(self, n):
        return os.path.join(self.tmp, f"lane{n}")

    def follow(self, tid, text="FIX: 1.txt never flushes its last line", key="a1b2c3d4"):
        """An open follow-up on a lane's task, as the fold writes one (triggers._place): a lane's task never takes an
        appended capture itself, so the item goes on its brief directly."""
        p = c.find_project(self.repo)
        b = c.find_brief(p, tid)
        body = b.section(triggers.FOLLOW).strip("\n")
        b.set_section(triggers.FOLLOW, (body + "\n" if body else "") + f"- [ ] {text} (f-{key} discovered)\n")
        c.save_brief(p, b)

    def audited_m(self, tid):
        """An M lane's audits: the lenses its finish asks for (intent and adversary) besides the self pass."""
        self.audited(tid)
        self.fm("task", "audit", tid, "intent", "read the brief against the diff", "no findings")
        self.fm("task", "audit", tid, "adversary", "read the diff as an attacker", "no findings")

    def open_follows(self, tid):
        return triggers.follow_gaps(c.find_project(self.repo), self.brief(tid))

    def tasks(self):
        return c.load_briefs(c.find_project(self.repo))


class SettleScope(Settle):
    def test_an_out_of_scope_file_is_settled_with_a_reason_naming_the_lane_tip(self):
        tid = self.lane(1, "1.txt", when="2026-01-01T00:00:00")
        self.fm("task", "set", tid, "scope=other/**")  # 1.txt is outside it
        self.audited(tid)
        self.returned(tid)
        tip = self.sha(1)  # read before the land: its close removes the lane's folder
        r = self.land()
        self.assertEqual(self.brief(tid).status, "done", r.stdout + r.stderr)
        self.assertIn(f"scope: widened by the lane diff reviewed and merged at tip {tip} (land)",
                      self.brief(tid).section("Log"), r.stdout + r.stderr)
        self.assertIn("1 closed (1 settled)", r.stdout + r.stderr)


class SettleFollowUp(Settle):
    def test_an_open_follow_up_is_captured_to_the_inbox_and_named_in_the_log(self):
        tid = self.lane(1, "1.txt", when="2026-01-01T00:00:00")
        self.fm("task", "set", tid, "scope=1.txt")  # in scope: only the follow-up is open
        self.follow(tid)
        self.assertEqual(len(self.open_follows(tid)), 1, "one open follow-up on the lane's task")
        self.audited(tid)
        self.returned(tid)
        before = len(self.tasks())
        r = self.land()
        self.assertEqual(self.brief(tid).status, "done", r.stdout + r.stderr)
        self.assertEqual(self.open_follows(tid), [], r.stdout + r.stderr)
        self.assertRegex(self.brief(tid).section("Follow-ups captured"), r"\[captured T-\d+\]")
        self.assertEqual(len(self.tasks()), before + 1, "one task per captured follow-up")
        self.assertRegex(self.brief(tid).section("Log"), r"follow-ups settled \(land\): f-[0-9a-f]{8} captured T-\d+")
        self.assertIn("1 closed (1 settled)", r.stdout + r.stderr)


    def test_a_follow_up_folded_into_the_lane_is_filed_as_a_task_by_its_land(self):
        # T-6065: a capture folded into an unlanded lane that no round settled is never lost: the land files it
        tid = self.lane(1, "1.txt", when="2026-01-01T00:00:00")
        self.fm("task", "set", tid, "scope=1.txt")
        r = self.fm("capture", "FIX: 1.txt never flushes its last line when the buffer is empty", "--parent", tid,
                    "--source", "followup")
        self.assertIn("folded into", r.stdout)
        self.assertEqual(len(self.open_follows(tid)), 1)
        self.audited(tid)
        self.returned(tid)
        before = len(self.tasks())
        r = self.land()
        self.assertEqual(self.brief(tid).status, "done", r.stdout + r.stderr)
        self.assertEqual(self.open_follows(tid), [], r.stdout + r.stderr)
        self.assertEqual(len(self.tasks()), before + 1, "the folded item became a task of its own")
        filed = max(self.tasks(), key=lambda b: c.id_num(b.id))
        self.assertEqual(filed.meta.get("source"), "followup")


class SettleSteps(Settle):
    def test_a_step_with_no_verify_of_its_own_takes_the_first_criterion_as_its_run(self):
        tid = self.lane(1, "1.txt", ac="true", tier="M", when="2026-01-01T00:00:00")  # M: a step has no run of its own
        self.fm("task", "step", tid, "add", "check the output")  # step 1 has the lane's clean run; step 2 has none
        self.audited_m(tid)
        self.returned(tid)
        r = self.land()
        self.assertEqual(self.brief(tid).status, "done", r.stdout + r.stderr)
        self.assertRegex(self.brief(tid).section("Log"), r"steps settled \(land\): step 2 took criterion 1's run \(true\)")
        self.assertIn("1 closed (1 settled)", r.stdout + r.stderr)


class SettleNoAudit(Settle):
    def test_no_audit_a_lane_with_all_three_gaps_goes_to_a_hand_close_and_settles_nothing(self):
        tid = self.lane(1, "1.txt", ac="true", tier="M", when="2026-01-01T00:00:00")
        self.fm("task", "set", tid, "scope=other/**")
        self.follow(tid)
        self.returned(tid)  # no audit at all
        before = len(self.tasks())
        r = self.land()
        b = self.brief(tid)
        self.assertNotEqual(b.status, "done", r.stdout + r.stderr)
        self.assertIn("left for a hand close", r.stdout + r.stderr)
        self.assertNotIn("settled", b.section("Log"))
        self.assertNotIn("scope: widened", b.section("Log"))
        self.assertEqual(len(self.tasks()), before, "no follow-up captured")
        self.assertTrue(self.open_follows(tid), "its follow-up is still open")
        self.assertIn("0 closed, 1 left for a hand close", r.stdout + r.stderr)
        self.assertNotIn("settled", r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
