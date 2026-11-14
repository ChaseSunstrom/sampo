"""T-4612: a message that only urges the session on (a sentence meaning go on, longer than the bare word) is a steer
with no task, not a new request. Nothing in the prompt hook decides it (the rules text leaves 'new request or not' to
the session), so the reading is the one the hook already starts for a steer-shaped message, the background steer
expansion: its KIND line gains `urge`. An urge books a one-shot carry-on note the session's next tool result carries;
a request (a mixed urge-plus-request included), a standing rule, or no judge reachable books nothing, as before. The
fixtures are neutral sentences and the model is a stub claude on PATH, never a real call."""
import json
import os
import unittest

from helpers import ForemanTestCase, read_text

STUB = r'''#!/usr/bin/env python3
import json, os, sys
sys.stdin.read()
if os.environ.get("STUB_DOWN"):
    sys.exit(1)
with open(os.environ["STUB_LOG"], "a") as f:
    f.write("call\n")
print(json.dumps({"result": os.environ["STUB_EXPAND"], "total_cost_usd": 0.01}))
'''

URGE = "please take up the next queued item now and do not stop while work remains"
MIXED = "take up the next queued item and also add a retry limit to the upload client"
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _Urge(ForemanTestCase):
    def setUp(self):
        super().setUp()
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir)
        with open(os.path.join(bindir, "claude"), "w") as f:
            f.write(STUB)
        os.chmod(os.path.join(bindir, "claude"), 0o755)
        self.log = os.path.join(self.tmp, "stub.log")
        self.env = {"PATH": bindir + os.pathsep + os.environ["PATH"], "STUB_LOG": self.log,
                    "FOREMAN_NO_BACKGROUND": "1"}
        self.fm("init")
        self.fm("task", "new", "Work in hand", "--type", "FIX", "--tier", "S", "--ac", "it works :: true",
                "--step", "do it", "--focus")

    def calls(self):
        return len(read_text(self.log).splitlines()) if os.path.exists(self.log) else 0

    def said(self, text, kind, sid="u1", down=False):
        """The message reaches the hook; the background expansion (run here in the foreground) reads it."""
        self.hook("UserPromptSubmit", {"prompt": text, "session_id": sid}, env=self.env)
        env = dict(self.env, STUB_EXPAND=kind)
        if down:
            env["STUB_DOWN"] = "1"
        return self.fm("exam", "steer", "expand", sid, env=env, check=not down).stdout

    def after_a_tool(self, sid="u1"):
        r = self.hook("PostToolUse", {"session_id": sid, "tool_name": "Read", "tool_input": {"file_path": "x"},
                                      "tool_response": {}}, env=self.env)
        out = (r.stdout or "").strip()
        return (json.loads(out).get("hookSpecificOutput") or {}).get("additionalContext", "") if out else ""

    def queue_titles(self):
        st = json.loads(self.fm("state", "--json").stdout)
        return [t.get("title", "") for t in st.get("inbox", []) + st.get("queue", [])]


class Urge(_Urge):
    def test_an_urge_files_no_task_and_the_next_tool_result_says_carry_on_once(self):
        out = self.said(URGE, "KIND: urge")
        self.assertIn("urge", out)
        self.assertEqual(self.queue_titles(), [])  # no capture of any kind
        note = self.after_a_tool()
        self.assertIn("urged", note)
        self.assertIn("capture nothing", note)
        self.assertIn("carry on", note)
        self.assertLess(len(note), 300)
        self.assertEqual(self.after_a_tool(), "")  # once: a second call hears nothing
        self.assertEqual(self.calls(), 1)  # the one reading the hook already starts; no new call

    def test_the_note_is_for_the_session_that_was_urged(self):
        self.said(URGE, "KIND: urge", sid="u1")
        self.assertEqual(self.after_a_tool(sid="other"), "")
        self.assertIn("urged", self.after_a_tool(sid="u1"))

    def test_the_note_rides_beside_another_posttool_note_without_dropping_either(self):
        from sampo import hooks
        ctx = lambda o: o["hookSpecificOutput"]["additionalContext"]
        other = {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "a file note"}}
        both = ctx(hooks._with_urge(other, "carry on"))
        self.assertIn("a file note", both)
        self.assertIn("carry on", both)
        self.assertEqual(ctx(hooks._with_urge(None, "carry on")), "carry on")
        self.assertIsNone(hooks._with_urge(None, ""))
        self.assertIs(hooks._with_urge(other, ""), other)

    def test_a_mixed_urge_and_request_is_still_a_request_and_a_rule_still_files(self):
        out = self.said(MIXED, "## Rule\n- Add a retry limit to the upload client.\nKIND: request")
        self.assertIn("request", out)
        self.assertEqual(self.after_a_tool(), "")
        self.said("from now on every upload client call carries a retry limit", "## Rule\n- Cap retries.\nKIND: steer",
                  sid="u2")
        self.assertTrue(any("Cap retries" in t for t in self.queue_titles()))
        self.assertEqual(self.after_a_tool(sid="u2"), "")

    def test_with_no_judge_reachable_nothing_changes(self):
        out = self.said(URGE, "", down=True)
        self.assertNotIn("urge", out.replace("urged", ""))
        self.assertEqual(self.queue_titles(), [])
        self.assertEqual(self.after_a_tool(), "")

    def test_a_question_or_a_sensitive_project_books_no_note(self):
        self.hook("UserPromptSubmit", {"prompt": "what is the next queued item right now?", "session_id": "u3"},
                  env=self.env)
        self.fm("sensitive", "on")
        self.fm("exam", "steer", "expand", "u3", env=dict(self.env, STUB_EXPAND="KIND: urge"))
        self.assertEqual(self.after_a_tool(sid="u3"), "")
        self.assertEqual(self.calls(), 0)


class Text(unittest.TestCase):
    def test_the_expander_may_answer_urge_and_the_rules_say_an_urge_is_no_request(self):
        from sampo import steer
        self.assertIn("KIND: urge", steer.EXPAND)
        self.assertIn("urge", steer.EXPAND.split("KIND: urge", 1)[1][:200])
        rules = read_text(os.path.join(ROOT, "plugin", "rules", "foreman.md"))
        line = next(x for x in rules.splitlines() if "New request" in x)
        self.assertIn("urge", line)
        self.assertIn("no capture", line)
        self.assertIn("next queued", line)
