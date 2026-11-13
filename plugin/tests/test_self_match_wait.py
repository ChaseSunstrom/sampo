"""T-4527: a wait loop on a process listing whose pattern also occurs in the launching command's own text never ends
(the launcher's command line holds the pattern). JARVIS 2026-11-12: a background wait looped on `ps -eo args | grep`
for a script name that its own launcher's text held, for 2.4 h. Ward adds one advisory line (never a refusal): the
loop may be right when the name is someone else's process."""
import unittest

from test_hooks import HookCase

from sampo import hooks

# a script body written with a heredoc holding the wait, and a later line that runs the same name
JARVIS = ("cat > wait.sh <<'EOF'\nwhile ps -eo args | grep gate.sh; do sleep 60; done\necho waited\nEOF\n"
          "nohup bash wait.sh > w.log 2>&1 &\nbash gate.sh")


def note(cmd):
    return hooks._self_wait_note({"tool_name": "Bash", "tool_input": {"command": cmd}})


class SelfMatchWaitNote(unittest.TestCase):
    def test_a_listing_wait_whose_pattern_occurs_elsewhere_gets_one_line(self):
        for cmd in (JARVIS,
                    "while ps -eo pid,args | grep -q run_train.sh; do sleep 30; done; bash run_train.sh",
                    "until ! ps -eo args | grep gate.sh >/dev/null; do sleep 5; done; nohup bash gate.sh &",
                    "while ps -eo args | grep -v grep | grep gate.sh; do sleep 9; done; bash gate.sh",  # a second grep
                    "while true; do\n  ps -eo args | grep gate.sh | grep -v grep >/dev/null || break\n  sleep 30\n"
                    "done\nbash gate.sh",  # a loop over several lines
                    "while ps -eo args | grep '[g]ate.sh'; do sleep 9; done; echo gate.sh starts",  # plain text elsewhere
                    "bash -c 'while ps -eo args | grep gate.sh; do sleep 9; done'; bash gate.sh",  # a script run
                    "while pgrep -f gate.sh; do sleep 30; done; bash gate.sh",  # pgrep -f, the same trap
                    'nohup bash -c "while ps -eo args | grep wait.sh; do sleep 5; done" & bash wait.sh'):
            with self.subTest(cmd=cmd[:50]):
                msg = note(cmd)
                self.assertTrue(msg, cmd)
                self.assertNotIn("\n", msg)
                self.assertIn("never ends", msg)
                self.assertIn("PID", msg)  # the fix: wait on the job's pid or a file it writes

    def test_a_bracketed_pattern_or_a_name_nowhere_else_or_a_pid_wait_gets_nothing(self):
        for cmd in ("while ps -eo args | grep '[g]ate.sh'; do sleep 60; done; nohup bash job.sh &",  # bracket, alone
                    "while ps -eo args | grep someproc; do sleep 60; done",  # a name that occurs nowhere else
                    "while pgrep -f someproc; do sleep 60; done",
                    "while kill -0 4242 2>/dev/null; do sleep 20; done; bash gate.sh",  # a pid wait
                    "tail --pid=4242 -f /dev/null; bash gate.sh", "wait 4242; bash gate.sh",
                    "while [ ! -f done.flag ]; do sleep 20; done; bash gate.sh",  # a file wait
                    "ps -eo args | grep gate.sh; bash gate.sh",  # a listing outside any loop
                    "pkill -f gate.sh; bash gate.sh",  # a kill, not a wait
                    "while sleep 1; do :; done; ps -eo args | grep gate.sh; bash gate.sh",  # after the loop closed                    'git commit -m "while ps -eo args | grep gate.sh; do sleep 5; done; bash gate.sh"',  # quoted data
                    "while ps -eo args | grep -v grep | grep someproc; do sleep 9; done; bash gate.sh"):
            with self.subTest(cmd=cmd[:50]):
                self.assertIsNone(note(cmd))

    def test_only_a_bash_call_is_read(self):
        self.assertIsNone(hooks._self_wait_note({"tool_name": "Write", "tool_input": {"content": JARVIS}}))


class SelfMatchWaitHook(HookCase):
    def test_the_jarvis_shape_is_advised_and_not_refused(self):
        self.fm("init")
        self.task()
        p = self.hook("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": JARVIS},
                                     "session_id": "sess-wait"})
        self.assertEqual(p.returncode, 0, p.stdout)  # advisory: the loop may be right for someone else's process
        self.assertNotIn("permissionDecision", p.stdout)
        self.assertIn("never ends", self.ctx_of(p))
        q = self.hook("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": "bash gate.sh"},
                                     "session_id": "sess-quiet"})
        self.assertNotIn("never ends", self.ctx_of(q))
