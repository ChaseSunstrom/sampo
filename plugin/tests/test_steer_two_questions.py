"""T-4637: a message is read first, alone, by a short neutral classifier (steer.KIND_ASK) that answers two independent
questions apart in its last lines, RULE (do the words state how to work on future requests) and WORK (do they ask for
work now: yes, urge, none); the kind is derived in code: rule yes is a steer (even when it also urges); else work yes is
a request (an urge that also asks for work included); else urge; else other. Only a steer runs the long expander, so a
request, an urge or chat costs one short call, and a one-off change worded with a universal scope ('every date', 'all
the configs'), read as a steer in 5 of 18 live runs under the expander's own "turn it into a rule" framing, no longer
files a standing-rule brief. An unparseable classifier reply falls back to the expander's own old "KIND: x" line; the
exam's expansion (classify off) is the one call it always was; EXPAND is the text T-2627 measured. No word list over the
user's words: the regexes parse only the reader's own last lines. The reader is a stub claude on PATH, never a real call.
reruns: the parser, the call counts, the filing rule and the prompt lengths; the live counts are measured by hand."""
import json
import os
import unittest
from unittest import mock

from helpers import ForemanTestCase

STUB = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
sys.stdin.read()
system = args[args.index("--append-system-prompt") + 1] if "--append-system-prompt" in args else ""
asking = "two questions" in system
with open(os.environ["STUB_LOG"], "a") as f:
    f.write(("classifier" if asking else "expander") + "\n")
print(json.dumps({"result": os.environ["STUB_KIND" if asking else "STUB_EXPAND"], "total_cost_usd": 0.01}))
'''

EXPAND_LEN = 880  # len(steer.EXPAND) at T-4637's base (6d97c1e11): the text its held-out coverage was measured on
ONE_OFF = "carry on, and make every date in the reports use the ISO format across all the reports"
RULE = "from now on never push to main without running the full suite first"
BULLETS = "## Rule\n- Run the full suite before any push to main.\n"


class Parser(unittest.TestCase):
    def kind(self, text):
        from sampo import steer
        return steer.kind_of(text)

    def test_rule_yes_is_a_steer_whatever_the_work_line_says(self):
        for work in ("yes", "urge", "none", "no"):
            self.assertEqual(self.kind(f"RULE: yes\nWORK: {work}"), "steer", work)

    def test_no_rule_then_work_decides(self):
        self.assertEqual(self.kind("RULE: no\nWORK: yes"), "request")
        self.assertEqual(self.kind("RULE: no\nWORK: urge"), "urge")
        self.assertEqual(self.kind("RULE: no\nWORK: none"), "other")
        self.assertEqual(self.kind("RULE: no\nWORK: no"), "other")

    def test_a_scope_word_in_the_bullets_changes_nothing_only_the_last_lines_are_read(self):
        self.assertEqual(self.kind("## Rule\n- Every date in all the reports uses ISO.\nRULE: no\nWORK: yes"), "request")

    def test_case_bullets_and_decoration_are_tolerated(self):
        self.assertEqual(self.kind("- x\n**rule: No**\n* work: Yes"), "request")
        self.assertEqual(self.kind("Rule:  yes\nWork:  none"), "steer")

    def test_one_field_alone_still_gives_a_kind(self):
        self.assertEqual(self.kind("RULE: yes"), "steer")
        self.assertEqual(self.kind("RULE: no"), "other")
        self.assertEqual(self.kind("WORK: yes"), "request")
        self.assertEqual(self.kind("WORK: urge"), "urge")

    def test_an_old_kind_line_still_parses_and_the_new_fields_win_over_it(self):
        for k in ("steer", "request", "urge", "other"):
            self.assertEqual(self.kind(f"- x\nKIND: {k}"), k)
        self.assertEqual(self.kind("KIND: steer\nRULE: no\nWORK: yes"), "request")

    def test_no_answer_or_nonsense_is_other(self):
        for t in ("", None, "## Rule\n- x", "RULE: maybe\nWORK: perhaps", "my RULE: yes is inline"):
            self.assertEqual(self.kind(t), "other", t)

    def test_the_expander_is_the_measured_text_and_the_classifier_is_short_and_neutral(self):
        from sampo import steer
        self.assertEqual(len(steer.EXPAND), EXPAND_LEN)  # not a character longer: its held-out coverage stands
        self.assertIn('"KIND: steer"', steer.EXPAND)
        self.assertNotIn("RULE:", steer.EXPAND)
        self.assertLess(len(steer.KIND_ASK), 500)
        self.assertIn("RULE: yes", steer.KIND_ASK)
        self.assertIn("WORK: urge", steer.KIND_ASK)
        self.assertNotIn("standing rule", steer.KIND_ASK)  # no "turn it into a rule" framing in the question


class Calls(unittest.TestCase):
    """steer.expand with the reader faked at its one seam: which children run, and what comes back."""

    def run_expand(self, replies, classify=True):
        from sampo import steer
        seen = []

        def child(p, feature, system, prompt, model):
            seen.append(feature)
            return replies.get(feature)

        with mock.patch.object(steer, "_child", child):
            return steer.expand(None, "the words", "sonnet", classify=classify), seen

    def test_no_rule_means_the_expander_never_runs(self):
        for reply, kind in (("RULE: no\nWORK: yes", "request"), ("RULE: no\nWORK: urge", "urge"),
                            ("RULE: no\nWORK: none", "other")):
            (bullets, got), seen = self.run_expand({"steer-kind": reply, "steer-expand": BULLETS + "KIND: steer"})
            self.assertEqual((bullets, got, seen), ([], kind, ["steer-kind"]), reply)

    def test_a_rule_runs_the_expander_once_and_its_own_kind_line_is_ignored(self):
        (bullets, kind), seen = self.run_expand({"steer-kind": "RULE: yes\nWORK: none",
                                                 "steer-expand": BULLETS + "KIND: request"})
        self.assertEqual((kind, seen), ("steer", ["steer-kind", "steer-expand"]))
        self.assertEqual(bullets, ["Run the full suite before any push to main."])

    def test_an_unparseable_classifier_falls_back_to_the_expanders_kind_line(self):
        for bad in (None, "", "I cannot tell", "RULE: maybe"):
            (_, kind), seen = self.run_expand({"steer-kind": bad, "steer-expand": BULLETS + "KIND: steer"})
            self.assertEqual((kind, seen), ("steer", ["steer-kind", "steer-expand"]), bad)
            (_, kind), _ = self.run_expand({"steer-kind": bad, "steer-expand": "KIND: urge"})
            self.assertEqual(kind, "urge", bad)

    def test_the_exam_expands_without_the_classifier_as_it_always_did(self):
        (bullets, kind), seen = self.run_expand({"steer-kind": "RULE: no\nWORK: yes",
                                                 "steer-expand": BULLETS + "KIND: steer"}, classify=False)
        self.assertEqual((kind, seen), ("steer", ["steer-expand"]))
        self.assertEqual(len(bullets), 1)


class Filing(ForemanTestCase):
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

    def said(self, text, kind, expand, sid):
        """The hook starts the reading; it runs here in the foreground. Returns its output and the children it ran."""
        self.hook("UserPromptSubmit", {"prompt": text, "session_id": sid}, env=self.env)
        before = self.children()
        out = self.fm("exam", "steer", "expand", sid, env=dict(self.env, STUB_KIND=kind, STUB_EXPAND=expand)).stdout
        return out, self.children()[len(before):]

    def children(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            return f.read().split()

    def titles(self):
        st = json.loads(self.fm("state", "--json").stdout)
        return [t.get("title", "") for t in st.get("inbox", []) + st.get("queue", [])]

    def test_a_one_off_change_read_rule_no_files_no_brief_and_costs_one_short_call(self):
        out, ran = self.said(ONE_OFF, "RULE: no\nWORK: yes", BULLETS + "KIND: steer", "q1")
        self.assertIn("request", out)
        self.assertEqual((self.titles(), ran), ([], ["classifier"]))

    def test_a_stated_rule_files_one_and_a_rule_that_also_urges_is_still_a_steer(self):
        out, ran = self.said(RULE, "RULE: yes\nWORK: none", BULLETS + "KIND: other", "q2")
        self.assertEqual(ran, ["classifier", "expander"])
        self.assertTrue(any("full suite" in t for t in self.titles()))
        _, ran = self.said("keep going, and " + RULE + " or merge", "RULE: yes\nWORK: urge",
                           "## Rule\n- Never merge unproven work.\nKIND: urge", "q3")
        self.assertTrue(any("merge unproven" in t for t in self.titles()))

    def test_an_unparseable_classifier_and_an_old_kind_steer_reply_still_file(self):
        _, ran = self.said(RULE, "no idea", BULLETS + "KIND: steer", "q4")
        self.assertEqual(ran, ["classifier", "expander"])
        self.assertTrue(any("full suite" in t for t in self.titles()))

    def test_an_urge_books_the_note_with_no_expansion_and_urge_plus_work_files_nothing(self):
        out, ran = self.said("please take up the next queued item", "RULE: no\nWORK: urge", "x", "q5")
        self.assertIn("urge", out)
        self.assertEqual((self.titles(), ran), ([], ["classifier"]))
        out, ran = self.said("take the next item and add a retry limit to the client", "RULE: no\nWORK: yes", "x", "q6")
        self.assertIn("request", out)
        self.assertEqual((self.titles(), ran), ([], ["classifier"]))


if __name__ == "__main__":
    unittest.main()
