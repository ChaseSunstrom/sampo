"""T-5794: modelup's switch-back types `/model best` into the session's tmux pane, so it asks what the keeper asks first: the
pane runs the Claude Code the snapshot names (tmuxpane.claude_here) and its screen is Claude Code's own and idle (state
strict). A fake tmux logs every call; nothing here touches a real pane."""
import os
import time
from unittest import mock

from test_hooks import HookCase
from test_keeper import KeeperRig, live_child

from sampo import modelup


class SwitchBack(KeeperRig, HookCase):
    def switch(self):
        """modelup._send in this process, its state and tmux the fake's; the channel it used (None: none)."""
        env = {"FOREMAN_HOME": self.home, "PATH": self.env["PATH"]}
        with mock.patch.dict(os.environ, env):
            for k in ("FOREMAN_STATE", "FOREMAN_DRIVE_TASK", "CLAUDE_CODE_ENTRYPOINT"):
                os.environ.pop(k, None)
            return modelup._send("lead-1")

    def test_the_switch_back_types_only_into_the_live_claude_pane_claude_here(self):
        self.snap()
        self.assertEqual(self.switch(), "tmux")
        keys = self.keys()
        self.assertEqual(len(keys), 2, keys)
        self.assertIn(modelup.WAY_BACK, keys[0])

    def test_the_switch_back_types_nothing_into_a_bash_pane_claude_here(self):
        self.snap()
        self.runs("bash")
        self.assertIsNone(self.switch())
        self.assertEqual(self.keys(), [])

    def test_the_switch_back_types_nothing_on_a_pid_mismatch_claude_here(self):
        other, _ = live_child(self)  # alive, but neither the snapshot's claude nor its parent
        self.snap()
        self.runs("claude", pid=other.pid)
        self.assertIsNone(self.switch())
        self.assertEqual(self.keys(), [])

    def test_the_switch_back_types_nothing_on_a_reused_pid_claude_here(self):
        self.snap(claude_start="1")
        self.assertIsNone(self.switch())
        self.assertEqual(self.keys(), [])

    def test_the_switch_back_types_nothing_without_a_claude_pid_claude_here(self):
        self.snap(pid=None)
        self.assertIsNone(self.switch())
        self.assertEqual(self.keys(), [])

    def test_the_switch_back_types_nothing_into_a_screen_that_is_no_claude_code_claude_here(self):
        self.snap()
        for screen in ("user@host:~$ \n", "some output\n\n❯ \n", "some output\n\n❯ half typed\n  ? for shortcuts\n",
                       "Do you want to proceed?\n  1. Yes\n  2. No\n  esc to cancel\n"):
            self.show_raw(screen)  # a shell, a bare prompt glyph, typed text, a dialog
            self.assertIsNone(self.switch(), screen)
        self.assertEqual(self.keys(), [])
