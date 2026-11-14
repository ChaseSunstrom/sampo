"""T-4585: sampo task finish decides every refusal it can without running anything before it starts a gate or verify
command, and a --lens line given in the same call counts as that lens's audit at the tip for the staleness rule.
On T-4576 (2026-11-13) the gates ran about 45 minutes and only then was the close refused for audits that predate the
last change, although the call carried the intent, edge and adversary lines. A fake gate (--run 'touch MARKER')
proves nothing costly started: the marker file is never written by a refused close."""
import json
import os
import unittest

from helpers import ForemanTestCase, valid_m_plan

from sampo import core as c

first = lambda text: json.JSONDecoder().raw_decode(text)[0]  # noqa: E731  (--focus prints its own lines after the JSON)

LESSON = ("--lesson", "none: test")


class FinishCheapFirst(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.marker = os.path.join(self.tmp, "gate-ran")
        self.gate = f"touch {self.marker}"

    def write(self, name, text="x = 1\n"):
        os.makedirs(os.path.dirname(os.path.join(self.repo, name)), exist_ok=True)
        with open(os.path.join(self.repo, name), "w") as f:
            f.write(text)

    def m_task(self, type_="FEATURE", audits=("intent", "edge"), extra=()):
        """An M task whose one step and criterion are proved, with `audits` recorded, then its file changed: every
        recorded audit predates the last change (the T-4576 state)."""
        interpretation, approach = valid_m_plan()
        tid = first(self.fm("task", "new", "Mine", "--type", type_, "--tier", "M", "--interpretation", interpretation,
                            "--approach", approach, "--ac", "ok :: true", "--step", "s", "--scope", "src/**",
                            *extra, "--json", "--focus").stdout)["id"]
        self.write("src/a.py")
        self.fm("task", "evidence", tid, "--ac", "1", "pytest", "ok")
        self.fm("task", "ac", tid, "check", "1")
        self.fm("task", "step", tid, "done", "1", "--evidence", "pytest", "ok")
        for lens in audits:
            self.fm("task", "audit", tid, lens, "checked", "ok")
        self.write("src/a.py", "x = 2\n")  # after the audits: they predate the last change
        c.log_event(c.find_project(self.repo), "touched", task=tid, data={"file": os.path.join(self.repo, "src/a.py"),
                                                                           "tool": "Edit"})
        return tid

    def finish(self, tid, *args, docs="none: test"):
        return self.fm("task", "finish", tid, "--run", self.gate, "--audit", "re-read", *LESSON, "--docs", docs,
                       *args, check=False)

    def test_stale_audits_and_no_lens_refuse_before_the_gate_starts(self):
        tid = self.m_task()
        r = self.finish(tid)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("nothing ran", r.stderr)
        self.assertIn("audit missing", r.stderr)
        self.assertFalse(os.path.exists(self.marker), "the gate command started before the audits were checked")
        self.assertNotEqual(c.find_brief(c.find_project(self.repo), tid).status, "done")

    def test_the_calls_own_lens_lines_count_at_the_tip_and_the_gate_goes_on(self):
        tid = self.m_task()
        r = self.finish(tid, "--lens", "intent: it fixes the stated defect", "--lens", "edge: empty and repeated input held")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(self.marker), "with the lenses given the close goes on to its gate")
        self.assertEqual(c.find_brief(c.find_project(self.repo), tid).status, "done")

    def test_one_missing_lens_is_still_refused_before_the_gate(self):
        tid = self.m_task(audits=())
        r = self.finish(tid, "--lens", "edge: held")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("audit missing: --lens 'intent", r.stderr)
        self.assertFalse(os.path.exists(self.marker))

    def test_a_lens_line_with_no_result_is_refused_before_the_gate(self):
        tid = self.m_task()
        r = self.finish(tid, "--lens", "intent:   ", "--lens", "edge: held")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--lens takes", r.stderr)
        self.assertFalse(os.path.exists(self.marker))

    def test_a_docs_line_naming_a_missing_doc_is_refused_before_the_gate(self):
        tid = self.m_task()
        r = self.finish(tid, "--lens", "intent: ok", "--lens", "edge: ok", docs="updated nowhere/missing-doc.md")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("nowhere/missing-doc.md", r.stderr)
        self.assertIn("nothing ran", r.stderr)
        self.assertFalse(os.path.exists(self.marker), "docs drift was found only after the gate ran")

    def test_an_open_child_is_refused_before_the_gate(self):
        tid = self.m_task()
        child = first(self.fm("task", "new", "Child", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true",
                              "--step", "s", "--json").stdout)["id"]
        p = c.find_project(self.repo)
        kid = c.find_brief(p, child)
        kid.meta["parent"] = tid
        c.save_brief(p, kid)
        self.fm("focus", tid)
        r = self.finish(tid, "--lens", "intent: ok", "--lens", "edge: ok")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(child, r.stderr)
        self.assertFalse(os.path.exists(self.marker))

    def test_a_research_close_without_a_decision_is_refused_before_the_gate(self):
        tid = self.m_task(type_="RESEARCH")
        r = self.finish(tid, "--lens", "intent: ok", "--lens", "edge: ok")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no Decision", r.stderr)
        self.assertFalse(os.path.exists(self.marker))

    def test_a_credential_in_the_files_to_commit_is_refused_before_the_gate(self):
        tid = self.m_task()
        self.write("src/a.py", 'api_token = "k8Zr4mVx2Lw9pQ7s"\n')
        r = self.finish(tid, "--lens", "intent: ok", "--lens", "edge: ok", "--commit", "Mine")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("look like a credential", r.stderr)
        self.assertFalse(os.path.exists(self.marker), "the credential scan ran only after the gate")

    def test_the_lens_stays_counted_when_the_tree_moves_after_the_gates(self):
        # the affected-tests run (or anything after the first record) can leave other files: the call's lenses are
        # judged at the tip the close ends on, not at the one before that run
        tid = self.m_task()
        late = os.path.join(self.repo, "src", "late.py")
        self.fm("check", "affected", f"python3 -c \"open({late!r}, 'w').write('y = 1')\" {{tests}}")
        self.write("src/test_a.py", "import unittest\n")
        r = self.finish(tid, "--lens", "intent: ok", "--lens", "edge: ok")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(c.find_brief(c.find_project(self.repo), tid).status, "done")


if __name__ == "__main__":
    unittest.main()
