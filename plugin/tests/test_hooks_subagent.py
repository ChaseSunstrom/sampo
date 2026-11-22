"""T-5516: a stopped fm-reviewer's report records its own lens audits. An approve (or approve with nits) report records one
audit per `## lens:` heading for the task it names: how = the reviewer and its agent id, result = its verdict and the
finding count under that lens. A 'changes needed' report records none and marks the task, so the lead is told the
builder must act. A report with no lens heading records nothing (the lead records audits by hand, as before)."""
import json
import os
import unittest
from unittest import mock

from helpers import ForemanTestCase

from sampo import core as c
from sampo import lanes as slanes

APPROVE = """**Verdict** — approve with nits.

## lens: intent
**Findings**
- LOW — `plugin/lib/fmhooks.py:10` — a name is vague — rename it — low

## lens: adversary
**Findings**
No findings.

**Acceptance criteria** — met.
**Noticed:** none
"""

CHANGES = """**Verdict** — changes needed.

## lens: adversary
- HIGH — `plugin/lib/fmhooks.py:12` — trusts the payload text — cap it — high

**Noticed:** none
"""

NO_HEADINGS = "**Verdict** — approve. Nothing found in the diff.\n"


class ReviewerAudits(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.tid = json.loads(self.fm("task", "new", "Fix login", "--type", "FIX", "--tier", "S", "--json").stdout)["id"]
        self.fm("task", "step", self.tid, "add", "fix")
        self.fm("task", "ac", self.tid, "add", "works", "--verify", "pytest")
        self.fm("focus", self.tid)
        self.fm("audit", "prep", self.tid)  # the review is briefed on this tree: the stop checks it still is

    def edit_repo(self):
        with open(os.path.join(self.repo, "moved.txt"), "w", encoding="utf-8") as f:
            f.write("changed after the brief\n")

    def review_prompt(self, tid):
        return f"Read {os.path.join(self.tmp, 'audits', f'{tid}.review.md')} and do the review it describes."

    def stop(self, msg, agent_id="a1", prompt=None, transcript=True):
        """A reviewer's stop. Its transcript's first user turn is the prompt it was briefed with (default: self.tid's brief)."""
        pl = {"agent_id": agent_id, "agent_type": "sampo:fm-reviewer", "last_assistant_message": msg}
        if transcript:
            path = os.path.join(self.tmp, f"{agent_id}.jsonl")
            text = prompt if prompt is not None else self.review_prompt(self.tid)
            with open(path, "w", encoding="utf-8") as f:
                f.write(json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n")
            pl["agent_transcript_path"] = path
        self.hook("SubagentStop", pl)

    def other_task(self):
        return json.loads(self.fm("task", "new", "Fix logout", "--type", "FIX", "--tier", "S", "--json").stdout)["id"]

    def brief_of(self, tid):
        return c.find_brief(c.find_project(self.repo), tid)

    def brief(self):
        return self.brief_of(self.tid)

    def audit_rows(self):
        return [x for x in self.brief().evidence() if c._AUDIT_RE.match(x)]

    def test_reviewer_audits_one_per_lens_heading_on_approve(self):
        self.stop(APPROVE)
        self.assertEqual([lens for lens, _, _ in self.brief().audits()], ["intent", "adversary"])
        rows = self.audit_rows()
        self.assertIn("sampo:fm-reviewer a1", rows[0])  # how: the reviewer and its agent id
        self.assertIn("approve with nits, 1 finding", rows[0])  # result: the verdict and the finding count
        self.assertIn("approve with nits, 0 findings", rows[1])

    def test_reviewer_audits_are_not_recorded_twice_by_a_second_stop(self):
        self.stop(APPROVE)
        self.stop(APPROVE)  # the same agent id: the same audit, once
        self.assertEqual(len(self.audit_rows()), 2)

    def test_reviewer_audits_changes_needed_records_none_and_marks_the_task(self):
        self.stop(CHANGES)
        self.assertEqual(self.brief().audits(), [], "a changes-needed review is not an audit that passes")
        self.assertEqual(self.brief().meta.get("changes_asked"), "a1", "the task is marked so sampo next names the relay")
        self.assertTrue(self.brief().meta.get("changes_asked_at"))
        findings = c.ledger_events(c.find_project(self.repo), kinds=["finding"])  # the findings travel as their events
        self.assertTrue(any("trusts the payload text" in json.dumps(e) for e in findings))

    def test_reviewer_audits_the_same_reviewers_approve_clears_its_mark(self):
        self.stop(CHANGES, agent_id="a1")
        self.stop(APPROVE, agent_id="a1")
        self.assertFalse(self.brief().meta.get("changes_asked"))

    def test_reviewer_audits_another_reviewers_approve_keeps_the_mark(self):
        self.stop(CHANGES, agent_id="a1")
        self.stop(APPROVE, agent_id="a2")  # the same tree: the builder has not acted on the changes yet
        self.assertEqual(self.brief().meta.get("changes_asked"), "a1")

    def test_reviewer_audits_a_moved_tree_lets_any_reviewer_clear_the_mark(self):
        self.stop(CHANGES, agent_id="a1")
        self.edit_repo()  # the builder acted on the changes; the lead briefs the next review
        self.fm("audit", "prep", self.tid)
        self.stop(APPROVE, agent_id="a2")
        self.assertFalse(self.brief().meta.get("changes_asked"))
        self.assertEqual(len(self.brief().audits()), 2)

    # T-5516 item 3: the audit is judged on the tree the reviewer was briefed on, and only that tree
    def test_reviewer_audits_record_the_tree_the_reviewer_was_briefed_on(self):
        self.stop(APPROVE)
        trees = {tree for _, _, tree in self.brief().audits()}
        self.assertEqual(trees, {self.brief().meta.get("review_tree")})
        self.assertTrue(all(trees), "the audits are stamped with a tree, so a later change can't pass them")

    def test_reviewer_audits_nothing_when_the_lane_moved_after_the_brief(self):
        self.edit_repo()  # changed between the brief and the stop
        self.stop(APPROVE)
        self.assertEqual(self.brief().audits(), [], "the review judged an earlier tree: it records nothing")

    def test_reviewer_audits_nothing_without_a_briefed_tree(self):
        b = self.brief()
        b.meta.pop("review_tree", None)  # a review no prep briefed on: nothing to check it against
        c.save_brief(c.find_project(self.repo), b)
        self.stop(APPROVE)
        self.assertEqual(self.brief().audits(), [])

    # T-5516 item 5: sampo next names the relay, the findings inline, until a reviewer's approve clears the mark
    def test_reviewer_audits_next_names_the_relay_with_its_findings_inline(self):
        self.stop(CHANGES, agent_id="a1")
        out = self.fm("next").stdout
        self.assertIn("a1 asked for changes on " + self.tid, out)
        self.assertIn("trusts the payload text", out, "the findings travel with the relay, not in a separate command")

    def test_reviewer_audits_next_names_no_relay_once_cleared(self):
        self.stop(CHANGES, agent_id="a1")
        self.stop(APPROVE, agent_id="a1")
        self.assertNotIn("asked for changes", self.fm("next").stdout)

    # T-5516 review: a heading that only starts with a lens name ("## Edge cases", "## Adversary model") is no lens, so an
    # approve records no audit for a lens the reviewer never reviewed
    def test_reviewer_audits_a_heading_that_only_starts_with_a_lens_name_records_no_audit(self):
        msg = ("**Verdict** — approve.\n\n## lens: intent\nNo findings.\n\n## Edge cases\nNo findings.\n\n"
               "## Adversary model\nNo findings.\n\n## Design notes\nNo findings.\n")
        self.stop(msg)
        self.assertEqual([lens for lens, _, _ in self.brief().audits()], ["intent"],
                         "'## Edge cases' is not the edge lens, nor '## Adversary model' the adversary's")

    def test_reviewer_audits_a_suffix_that_does_not_say_it_passed_records_no_audit(self):
        msg = ("**Verdict** — approve.\n\n## lens: intent\nNo findings.\n\n## lens: edge — not  checked\nNo findings.\n\n"
               "## lens: adversary: skipped\nNo findings.\n\n## Self-review\nNo findings.\n")
        self.stop(msg)
        self.assertEqual([lens for lens, _, _ in self.brief().audits()], ["intent"])

    # T-5516 review: the mark is stamped before the findings are logged, so a second boundary crossed while the tree is
    # built can't leave the findings older than the mark (sampo next filters them by it)
    def test_reviewer_audits_findings_stay_in_the_relay_when_the_tree_build_crosses_a_second(self):
        from sampo import hooks
        real = slanes.review_tree

        def slow_tree(p, b):  # the tree build takes long enough that the clock reads a later second after it
            os.environ["FOREMAN_NOW"] = "2030-01-01T00:00:05Z"
            return real(p, b)
        path = os.path.join(self.tmp, "slow.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "user", "message": {"role": "user", "content": self.review_prompt(self.tid)}}) + "\n")
        pl = {"agent_id": "a1", "agent_type": "sampo:fm-reviewer", "last_assistant_message": CHANGES,
              "agent_transcript_path": path, "cwd": self.repo, "session_id": "sess-1"}
        with mock.patch.dict(os.environ, {"FOREMAN_NOW": "2030-01-01T00:00:00Z"}), \
                mock.patch.object(slanes, "review_tree", slow_tree), mock.patch("sampo.tripwire.start"):
            hooks.subagent_stop(pl)
        out = self.fm("next").stdout
        self.assertIn("trusts the payload text", out)
        self.assertNotIn("no finding text on record", out)

    # T-5516 review: reviewer text is data: no control character of it reaches the terminal through sampo next
    def test_reviewer_audits_next_prints_the_relay_findings_as_plain_text(self):
        self.stop(CHANGES.replace("trusts the payload text", "trusts the \x1b[31mpayload\x07 text"))
        out = self.fm("next").stdout
        self.assertIn("payload", out)
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\x07", out)

    def test_reviewer_audits_no_lens_heading_records_nothing(self):
        self.stop(NO_HEADINGS)
        self.assertEqual(self.brief().audits(), [], "no heading, no audit: the lead records it as before")

    # T-5516 item 1: the task is the one the reviewer's prompt names, never the focused task by default
    def test_reviewer_audits_report_naming_another_task_than_its_prompt_binds_the_prompts(self):
        other = self.other_task()
        self.stop(f"{other} reviewed.\n" + APPROVE)  # T-5913: the prompt's review brief decides, not the report's ids
        self.assertEqual([lens for lens, _, _ in self.brief().audits()], ["intent", "adversary"])
        self.assertEqual(self.brief_of(other).audits(), [])

    def test_reviewer_audits_report_and_focused_task_disagreeing_without_a_prompt_record_nothing(self):
        other = self.other_task()
        self.stop(f"{other} reviewed.\n" + APPROVE, transcript=False)  # no brief path: the old rule
        self.assertEqual(self.brief().audits(), [])
        self.assertEqual(self.brief_of(other).audits(), [])
        unbound = c.ledger_events(c.find_project(self.repo), kinds=["reviewer_unbound"])
        self.assertTrue(any(other in json.dumps(e) and self.tid in json.dumps(e) for e in unbound),
                        "the disagreement is logged with the tasks it names")

    def test_reviewer_audits_prompt_naming_another_task_than_the_focused_one_binds_the_prompts(self):
        other = self.other_task()
        self.stop(APPROVE, prompt=self.review_prompt(other))  # T-5913: the focus is only a guess; `other` was never briefed
        self.assertEqual(self.brief().audits(), [])
        self.assertEqual(self.brief_of(other).audits(), [], "no tree was briefed on it: nothing is recorded")

    def test_reviewer_audits_without_a_transcript_or_a_report_task_binds_nothing(self):
        self.stop(APPROVE, transcript=False)  # the focused task is only a guess: no audit lands on it
        self.assertEqual(self.brief().audits(), [])

    def test_reviewer_audits_a_report_naming_the_task_binds_it_without_a_prompt(self):
        self.stop(f"{self.tid} reviewed.\n" + APPROVE, transcript=False)
        self.assertEqual([lens for lens, _, _ in self.brief().audits()], ["intent", "adversary"])


class ReviewerVerdictAndLenses(unittest.TestCase):
    """T-5516 item 2: only an exact approve (or approve with nits) records audits; a lens that is not clean does not."""

    def verdict(self, line):
        return slanes.reviewer_verdict(f"{line}\n\n## lens: intent\nNo findings.\n")

    def test_exact_approve_words_record_as_approve(self):
        self.assertEqual(self.verdict("**Verdict** — approve.")[0], "approve")
        self.assertEqual(self.verdict("**Verdict** — approve with nits.")[0], "approve")
        self.assertEqual(self.verdict("**Verdict**: APPROVE WITH NITS")[0], "approve", "case does not matter")

    def test_a_conditional_approve_records_nothing(self):
        self.assertIsNone(self.verdict("**Verdict** — approve only once the HIGH is fixed."))
        self.assertIsNone(self.verdict("**Verdict** — approve, but the HIGH is open."))
        self.assertIsNone(self.verdict("**Verdict** — approve after the fix."))

    def test_changes_needed_only_as_exactly_those_words(self):
        self.assertEqual(self.verdict("**Verdict** — changes needed (HIGH open).")[0], "changes")
        self.assertIsNone(self.verdict("**Verdict** — changes needed, approve after the fix."))

    def test_a_lens_whose_heading_says_not_checked_is_not_recorded(self):
        msg = ("**Verdict** — approve with nits.\n\n## lens: intent\n- LOW — `f.py:1` — a name — rename — low\n\n"
               "## lens: adversary — not checked (no runtime here)\n- LOW — `f.py:2` — a gap — test it — low\n")
        self.assertEqual(slanes.reviewer_lenses(msg), {"intent": 1})

    def test_a_lens_whose_heading_says_changes_needed_is_not_recorded(self):
        msg = ("**Verdict** — approve with nits.\n\n## lens: intent\nNo findings.\n\n"
               "## lens: adversary — changes needed\n- LOW — `f.py:2` — a gap — test it — low\n")
        self.assertEqual(slanes.reviewer_lenses(msg), {"intent": 0})

    def test_a_lens_with_a_high_finding_is_not_recorded(self):
        msg = ("**Verdict** — approve with nits.\n\n## lens: intent\n- HIGH — `f.py:1` — a fault — fix — high\n\n"
               "## lens: adversary\n- LOW — `f.py:2` — a gap — test it — low\n")
        self.assertEqual(slanes.reviewer_lenses(msg), {"adversary": 1}, "the HIGH lens is left for the lead to record")

    def test_a_clean_lens_with_low_findings_is_recorded(self):
        msg = "**Verdict** — approve.\n\n## lens: intent\n- LOW — `f.py:1` — a name — rename — low\n"
        self.assertEqual(slanes.reviewer_lenses(msg), {"intent": 1})

    def test_a_finding_under_a_suffixed_heading_is_its_own_lens_not_the_previous_one(self):
        # T-5516 item 2: a heading's suffix ("— changes needed") does not end its lens: its findings stay with it
        msg = ("**Verdict** — changes needed.\n\n## lens: intent\nNo findings.\n\n"
               "## lens: adversary — changes needed\n- HIGH — `f.py:2` — a fault — fix — high\n")
        self.assertEqual(slanes.reviewer_findings(msg), [("adversary", "HIGH", "f.py:2", "a fault")])
        self.assertEqual(slanes.reviewer_lenses(msg), {"intent": 0})

    def test_a_heading_that_is_only_a_word_starting_with_a_lens_name_is_no_lens(self):
        msg = "## lens: intent\n- LOW — `f.py:1` — a name — rename — low\n\n## Edge cases\n- LOW — `f.py:2` — a gap — test — low\n"
        self.assertEqual(slanes.reviewer_findings(msg), [("intent", "LOW", "f.py:1", "a name"),
                                                         ("intent", "LOW", "f.py:2", "a gap")],
                         "'## Edge cases' is not the edge lens: its finding stays with intent")

    def test_a_heading_that_only_starts_with_a_lens_name_records_no_lens(self):
        # T-5516 review: the lens recorder reads the headings the finding reader reads ("## Edge cases" is no lens)
        msg = ("**Verdict** — approve.\n\n## lens: intent\nNo findings.\n\n## Edge cases\nNo findings.\n\n"
               "## Adversary model\nNo findings.\n\n## Design notes\nNo findings.\n")
        self.assertEqual(slanes.reviewer_lenses(msg), {"intent": 0})
        self.assertEqual(slanes.reviewer_lenses("**Verdict** — approve.\n\n## lens: adversary — changes needed\nx\n"), {})

    def lenses_of(self, head, body="No findings.\n"):
        return slanes.reviewer_lenses(f"**Verdict** — approve.\n\n## lens: intent\nNo findings.\n\n{head}\n{body}")

    # T-5516 round 2, item 1: a separator is a colon or a dash with its own space (or a long dash), never a hyphen inside a word
    def test_a_hyphen_or_dash_inside_a_word_makes_no_lens_heading(self):
        for head in ("## Self-review", "## Edge-cases", "## Design-notes", "## Edge—cases", "## edge–cases", "## Edge -cases"):
            self.assertEqual(self.lenses_of(head), {"intent": 0}, head)

    def test_a_hyphen_inside_a_word_does_not_switch_the_finding_lens(self):
        for head in ("## Self-review", "## Edge-cases", "## Design-notes"):
            msg = f"## lens: intent\n{head}\n- LOW — `f.py:2` — a gap — test — low\n"
            self.assertEqual(slanes.reviewer_findings(msg), [("intent", "LOW", "f.py:2", "a gap")], head)

    # T-5516 round 2, item 2: only a suffix that says it passed records; any other suffix keeps the lens out
    def test_a_suffix_that_does_not_say_it_passed_records_no_lens(self):
        for suffix in ("not checked", "not  checked", "not-checked", "Not Checked (no runtime here)", "skipped",
                       "not reviewed", "n/a", "changes required", "needs changes", "changes needed", "changes  needed",
                       "TODO", "approve once the HIGH is fixed", "approved?", "no  findings left to fix soon", "x findings"):
            for sep in (" — ", ": ", " - ", " – ", "—"):
                self.assertEqual(self.lenses_of(f"## lens: edge{sep}{suffix}"), {"intent": 0}, f"{sep}{suffix}")

    def test_a_suffix_that_says_it_passed_records_the_lens(self):
        for head in ("## edge", "## lens: edge", "## edge: ok", "## Lens: Edge - pass", "## Edge: approve with nits",
                     "## Edge — 2 findings", "## Edge — 0 findings", "## edge: 1 finding", "## Edge — 3 findings: all low",
                     "## Edge: clean", "## Edge: met", "## Edge: no findings", "## Edge: approve.", "## Edge: Approve  With Nits",
                     "## Edge: no findings", "## Edge: ｏｋ", "## `edge`: **ok**"):
            self.assertEqual(self.lenses_of(head), {"intent": 0, "edge": 0}, head)

    def test_a_clean_suffix_does_not_clean_a_lens_that_has_a_high_finding(self):
        self.assertEqual(self.lenses_of("## Edge: approve", "- HIGH — `f.py:1` — a fault — fix — high\n"), {"intent": 0})

    # T-5516 round 2, LOW: a heading in a code fence or an indented block is quoted text, not the report's own
    def test_a_heading_in_a_code_fence_or_an_indented_block_records_nothing(self):
        for body in ("```\n## lens: adversary: approve\n```\n", "```md\n## Adversary: approve\n```\n",
                     "    ## lens: adversary: approve\n", "        ## Adversary: approve\n"):
            self.assertEqual(self.lenses_of("## lens: edge: ok", body), {"intent": 0, "edge": 0}, body)
        after = self.lenses_of("```\n## Adversary: approve\n```\n## lens: edge", "")
        self.assertEqual(after, {"intent": 0, "edge": 0}, "the fence closes: a heading after it reads as one")

    def test_a_finding_under_a_quoted_heading_stays_with_the_real_lens(self):
        msg = ("## lens: intent\n```\n## lens: adversary\n```\n- LOW — `f.py:2` — a gap — test — low\n"
               "    ## lens: edge\n- LOW — `f.py:3` — a nit — test — low\n")
        self.assertEqual([x[0] for x in slanes.reviewer_findings(msg)], ["intent", "intent"])
