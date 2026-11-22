"""T-5769: the keeper in `sampo keeper` (the refresh of the lead sessions): what each pane state gets, and the 30-minute
limit per pane. The rig (a fake tmux that answers capture-pane from a file) is test_keeper's."""
import unittest

from test_hooks import HookCase
from test_keeper import KeeperRig


class KeeperPaneStates(KeeperRig, HookCase):
    PROMPTS = {
        "idle": ("❯ \n  ? for shortcuts\n", "sent"),
        "idle in a box": ("╭──────╮\n│ >    │\n╰──────╯\n  ? for shortcuts\n", "sent"),
        "typed": ("❯ fix the thing\n  ? for shortcuts\n", "typed"),
        "dialog": ("Allow this edit?\n ❯ 1. Yes\n   2. No\n Esc to cancel\n", "dialog"),
        "busy": ("✻ Thinking… (3s · esc to interrupt)\n❯ \n", "busy"),
        "no prompt": ("$ ls\nfile\n", "not a Claude Code screen"),  # T-5769 review: a shell, not Claude Code
    }

    def test_keeper_each_pane_state_gets_what_it_should(self):
        self.queued()
        for name, (screen, want) in self.PROMPTS.items():
            with self.subTest(name):
                import os
                for f in (self.log, self.state_path()):
                    if os.path.exists(f):
                        os.unlink(f)
                self.show_raw(screen)
                self.snap()
                row = self.keep()["sessions"][0]
                self.assertEqual(row["action"] if want == "sent" else row["why"], want, row)
                self.assertEqual(len(self.keys()), 2 if want == "sent" else 0)

    def test_keeper_thirty_minutes_per_pane_not_per_machine(self):
        self.queued()
        self.snap(sid="a", pane="%7")
        self.snap(sid="b", pane="%8")
        self.assertEqual([r["action"] for r in self.keep()["sessions"]], ["sent", "sent"])
        self.assertEqual(len(self.keys()), 4)
        self.assertEqual([r["action"] for r in self.keep()["sessions"]], ["skipped", "skipped"])
        self.assertEqual(len(self.keys()), 4)

    def test_keeper_a_dry_run_types_nothing_and_records_nothing(self):
        import os
        self.queued()
        self.snap()
        row = self.keep("--dry")["sessions"][0]
        self.assertEqual(row["action"], "would send")
        self.assertEqual(self.keys(), [])
        self.assertFalse(os.path.exists(self.state_path()))


if __name__ == "__main__":
    unittest.main()
