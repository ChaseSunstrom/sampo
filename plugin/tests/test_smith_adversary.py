"""T-4381: a run whose change touches a security-sensitive file (a manifest) closes: sampo task finish asks for the
adversary lens, a tier S plan has no close notes to give it, so the run asks the model once, shown the diff of those
files, and passes the answer as --lens adversary (fakes only; a temp repo whose Cargo.toml the run edits)."""
import json
import os
import unittest

from test_code import CLOSE, M_PLAN, PLAN, CodeCase, Resume, call, plan
from helpers import read_text

CARGO = '[package]\nname = "calc"\nversion = "0.1.0"\n\n[dependencies]\n'
EDITS = (call("edit", file="calc.py", old="a - b", new="a + b"),
         call("edit", file="Cargo.toml", old='version = "0.1.0"', new='version = "0.1.1"'),
         call("done", summary="x"))
LENS = ("an attacker with a crafted manifest could pull a hostile version; only the patch number changed, no new "
        "dependency, and the pinned lock still holds")


def lens(text=LENS):
    return {"text": json.dumps({"adversary": text})}


class Adversary(CodeCase):
    repo_seed = ("seed the bug", (("calc.py", "def add(a, b):\n    return a - b\n"),
                                  ("test_calc.py", "import calc\nassert calc.add(2, 3) == 5, calc.add(2, 3)\nprint('ok')\n"),
                                  ("Cargo.toml", CARGO)))

    def closes(self, *script, p=PLAN, edits=EDITS):
        f = self.serve("anthropic", plan(p), *edits, *script)
        r = self.code("fix add", "--model", "fake")
        self.assertEqual(self.task()["status"], "done", r.stdout + r.stderr)
        return f

    def test_manifest_closes_a_tier_s_run_with_the_adversary_lens_it_was_asked_for(self):
        f = self.closes(lens())
        self.assertEqual(len(f.seen), 5)  # plan, two edits, done, the one adversary question
        asked = f.said(4)
        self.assertIn("Cargo.toml", asked)
        self.assertIn("0.1.1", asked)  # the diff of the sensitive file
        self.assertIn("<data-", asked)  # fenced
        self.assertIn("attacker", asked)
        self.assertIn("hostile version", read_text(self.task()["path"]))

    def test_manifest_closes_with_the_audit_worded_as_the_runs_own_look(self):
        self.closes(lens())
        text = read_text(self.task()["path"])
        self.assertIn("fm code (", text)
        self.assertIn("not an independent review", text)  # said plainly, never passed off as one

    def test_manifest_closes_with_the_diff_since_the_base_even_when_the_edit_is_staged(self):
        f = self.closes(lens(), edits=(EDITS[0], EDITS[1], call("run", cmd="git add Cargo.toml"), EDITS[2]))
        self.assertIn("+version = \\\"0.1.1\\\"", f.said(5))

    def test_manifest_closes_with_a_bounded_diff(self):
        big = "".join(f'dep{i} = "1.{i}"\n' for i in range(2000))
        f = self.serve("anthropic", plan(), EDITS[0], call("write", file="Cargo.toml", content=CARGO + big), EDITS[2],
                       lens())
        self.code("fix add", "--model", "fake")
        self.assertEqual(self.task()["status"], "done")
        self.assertLess(len(f.said(4)), 12000)
        self.assertNotIn("dep1999", f.said(4))

    def test_manifest_closes_after_a_copied_example_is_asked_again(self):
        from sampo import smith
        copied = smith.GAP_FIELDS[-1][1].split("(for example: ", 1)[1][:-1]
        f = self.closes(lens(copied), lens())
        self.assertIn("copies the example", f.said(5))
        self.assertIn("hostile version", read_text(self.task()["path"]))

    def test_not_asked_when_no_sensitive_file_is_touched(self):
        f = self.closes(edits=(EDITS[0], EDITS[2]))
        self.assertEqual(len(f.seen), 3)  # plan, edit, done: nothing more

    def test_not_asked_by_a_modelless_run(self):
        r = self.code("fix add", "--worker", "true", "--verify", "python3 test_calc.py", check=False)
        self.assertNotIn("adversary lens", r.stdout)  # nobody to ask: finish refuses with its own reason as before

    def test_once_a_tier_m_run_gives_its_lens_in_the_close_notes_and_is_not_asked_twice(self):
        f = self.closes(CLOSE, p=M_PLAN)
        self.assertEqual(len(f.seen), 5)  # plan, two edits, done, the close notes: no second adversary question
        self.assertIn("negative numbers add too", read_text(self.task()["path"]))

    def test_resume_reuses_the_journaled_answer_and_does_not_ask_again(self):
        Resume.kill_mid_run(self, PLAN)
        jr = os.path.join(os.path.dirname(os.path.dirname(self.task()["path"])), "code", "T-0001.jsonl")
        with open(jr, "a") as fh:
            fh.write(json.dumps({"kind": "gap_notes", "notes": {"adversary": LENS}}) + "\n")
        f = self.serve("openai", *EDITS)  # no lens reply scripted: asking again would find the script empty
        r = self.code("--resume", "--model", "fake")
        self.assertEqual(self.task()["status"], "done", r.stdout + r.stderr)
        self.assertEqual(len(f.seen), 3)
        self.assertIn("hostile version", read_text(self.task()["path"]))

    def test_resume_journals_the_answer_it_was_asked(self):
        self.closes(lens())
        jr = os.path.join(os.path.dirname(os.path.dirname(self.task()["path"])), "code", "T-0001.jsonl")
        notes = [json.loads(x) for x in read_text(jr).splitlines() if '"gap_notes"' in x]
        self.assertEqual(notes[-1]["notes"], {"adversary": LENS})


if __name__ == "__main__":
    unittest.main()
