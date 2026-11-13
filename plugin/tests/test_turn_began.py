"""T-4547: hooks._turn_began with no turn_at (no prompt hook ran this turn) reads the session's seen stamp, a whole second,
and the drive's `worked` reads each brief's `updated` stamp, also a whole second. Both sides are on that one clock: a
task stamped in the second the session was seen is this turn's work (the fallback start is that whole second, so the
comparison is inclusive). The cost: work from the previous turn finished in that same second counts once more."""
from test_hooks import HookCase

from sampo import core as c
from sampo import hooks

S = "2026-11-13T10:00:00Z"  # the seen stamp: the whole second the session was last seen
NEXT = "the queue is empty"  # the drive's line when a turn worked and the queue drained


def set_session(p, seen=S, sid="sess-1"):
    meta = c.read_meta(p)
    meta["session"] = {"id": sid, "seen": seen}
    c.write_meta(p, meta)


def stamp_brief(p, tid, updated):
    """A brief's `updated` set to a chosen whole second (save_brief(touch=False) keeps it)."""
    b = c.find_brief(p, tid)
    b.meta.update(status="done", updated=updated)
    c.save_brief(p, b, touch=False)


class TurnBegan(HookCase):
    def test_no_turn_at_is_the_second_the_session_was_seen(self):
        """The fallback is the seen stamp on the stamps' clock: exactly its whole second, not a time.time() estimate."""
        self.fm("init")
        p = self.project()
        set_session(p)
        self.assertEqual(hooks._turn_began(p, "sess-1", {}), c.parse_ts(S).timestamp())

    def test_turn_at_wins_when_the_prompt_hook_set_it(self):
        self.fm("init")
        p = self.project()
        set_session(p)
        self.assertEqual(hooks._turn_began(p, "sess-1", {"turn_at": 1234.5}), 1234.5)

    def test_another_sessions_seen_stamp_is_no_turn_start(self):
        self.fm("init")
        p = self.project()
        set_session(p, sid="sess-2")
        self.assertIsNone(hooks._turn_began(p, "sess-1", {}))


class SameSecondWork(HookCase):
    """The drive's push at Stop with no prompt hook: the session was seen at S and one done task's stamp decides
    whether this turn worked (the drive's `worked`), so the queue drain says its line. The fallback start is the
    seen second itself, so a stamp in that second is this turn's and a stamp in the second before is not."""

    def stop_after(self, updated):
        self.fm("init")
        self.fm("autonomy", "full")
        p = self.project()
        set_session(p)
        # no active task: a dropped one would carry today's stamp and count as this turn's work
        done = self.task("Built the store", focus=False)
        stamp_brief(p, done, updated)
        return self.hook("Stop", {"stop_hook_active": False, "last_assistant_message": "Working on it.",
                                  "session_id": "sess-1"}).stdout

    def test_work_stamped_in_the_second_the_session_was_seen_is_this_turns(self):
        self.assertIn(NEXT, self.stop_after(S))

    def test_work_stamped_in_a_later_second_is_this_turns(self):
        self.assertIn(NEXT, self.stop_after("2026-11-13T10:00:01Z"))

    def test_work_stamped_in_the_second_before_is_not_this_turns(self):
        self.assertNotIn(NEXT, self.stop_after("2026-11-13T09:59:59Z"))
