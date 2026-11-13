"""T-4590: a Smith fix run wrote four regression tests for three defects, three of which pass on the unfixed source, and
every criterion's verify went from red (the class did not exist) to green, so the run said done. Now a criterion whose plan
names proof files (the files the run writes and the verify needs) is also run on the before-work copy of the tree with only
those files laid over it: a pass there means the proof does not reproduce the defect. It is told to the worker at the step's
end, and at the close the criterion is not proven (every criterion not proven: not done). A proof that fails there passes the
check; a criterion with no proof files is untouched. The fake model answers from a script; the repo is a real git repo."""
import json
import os
import subprocess
import sys

from helpers import FM, read_text
from test_code import HANG, PLAN, CodeCase, call, plan
from test_smith_record import events, sent

from sampo import smith

PROOF = "proof_add.py"
WEAK = "import calc\nassert calc.add(2, 0) == 2, calc.add(2, 0)\nprint('ok')\n"  # 2 - 0 is 2: passes on the unfixed code
STRONG = "import calc\nassert calc.add(2, 3) == 5, calc.add(2, 3)\nprint('ok')\n"  # 2 - 3 is -1: fails there
ASKED = "calc.add adds when the second number is not zero"


def one(proof=(PROOF,), **kw):
    """A plan whose one criterion is the proof file's run; proof: the files it names (none: the key is left out)."""
    cr = {"text": ASKED, "verify": f"python3 {PROOF}"}
    if proof is not None:
        cr["proof"] = list(proof)
    return dict(PLAN, criteria=[cr], steps=["write the proof, then fix calc.add"], **kw)


def write(content):
    return call("write", file=PROOF, content=content)


FIX_CALC = call("edit", file="calc.py", old="a - b", new="a + b")  # the same size: a stale .pyc would hide the change
DONE = call("done", summary="proof written and calc.add fixed")


class Unit(CodeCase):
    def test_prompt_asks_for_proof_files_in_plain_meaning(self):
        self.assertIn('"proof"', smith.PLAN_PROMPT)
        self.assertRegex(smith.PLAN_PROMPT, r"(?i)files? (?:the run|you) writes?")

    def test_a_plan_without_proof_is_accepted_and_unchanged(self):
        self.assertIsNone(smith.check_plan(PLAN))
        self.assertEqual(smith.fit_proof(PLAN), (PLAN, None))

    def test_fit_proof_keeps_repo_relative_files_and_drops_the_rest(self):
        v = dict(PLAN, criteria=[
            {"text": "a", "verify": "x", "proof": ["tests/test_a.py", "/etc/passwd", "../out.py", "t/../../x.py", 7, "",
                                                   "tests/test_a.py", "./tests/test_b.py", "dir/"]},
            {"text": "b", "verify": "y", "proof": "only.py"},
            {"text": "c", "verify": "z", "proof": 5},
            {"text": "d", "verify": "w", "proof": []}])
        got, _ = smith.fit_proof(v)
        self.assertEqual([cr.get("proof") for cr in got["criteria"]],
                         [["tests/test_a.py", "tests/test_b.py"], ["only.py"], None, None])
        self.assertEqual(v["criteria"][0]["proof"][1], "/etc/passwd", "the reply itself is never changed")

    def test_fit_proof_is_bounded(self):
        v = dict(PLAN, criteria=[{"text": "a", "verify": "x", "proof": [f"t{i}.py" for i in range(40)]}])
        got, _ = smith.fit_proof(v)
        self.assertEqual(len(got["criteria"][0]["proof"]), smith.PROOF_FILES)

    def test_check_plan_accepts_a_plan_that_names_proof_files(self):
        self.assertIsNone(smith.check_plan(one()))


class Run(CodeCase):
    def go(self, p, *script, tmp=None):
        f = self.serve("openai", plan(p), *script)
        r = self.code("fix add", "--model", "fake", "--json", check=False, env={"TMPDIR": tmp} if tmp else None)
        return f, r

    def notes(self, r):
        return [e for e in events(r) if e["event"] == "note" and e.get("proof")]

    def test_a_proof_that_passes_on_the_unfixed_code_is_told_to_the_worker_at_the_steps_end(self):
        f, r = self.go(one(), write(WEAK), FIX_CALC, DONE, write(STRONG), DONE)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(f.script, [], "every scripted reply was asked for: the done was sent back once")
        told = sent(f, len(f.seen) - 2)  # the request after the first done
        self.assertIn("passes on the code as it was before your work", told)
        self.assertIn(PROOF, told)
        self.assertIn("not done", told.lower())
        self.assertEqual(self.task()["status"], "done")
        self.assertNotIn("not proven", events(r)[-1]["text"], "the mended proof is proven")

    def test_a_proof_that_fails_on_the_unfixed_code_passes_the_check(self):
        f, r = self.go(one(), write(STRONG), FIX_CALC, DONE)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(f.script, [])
        self.assertNotIn("before your work", "\n".join(sent(f, i) for i in range(len(f.seen))))
        self.assertEqual(self.task()["status"], "done")
        self.assertEqual({n["proof"] for n in self.notes(r)}, {"red"}, "run on the before copy, it failed: accepted")

    def test_a_proof_never_mended_closes_not_proven_and_not_done(self):
        f, r = self.go(one(), write(WEAK), FIX_CALC, DONE, DONE, DONE)
        self.assertNotEqual(r.returncode, 0, r.stdout)
        last = events(r)[-1]
        self.assertEqual(last["event"], "blocked", r.stdout[-600:])
        self.assertIn("not proven", last["reason"])
        self.assertIn(ASKED, last["reason"])
        self.assertIn("passes on the code as it was before", last["reason"])
        self.assertNotEqual(self.task()["status"], "done")
        self.assertIn("a + b", self.calc(), "the work stays in the tree")

    def test_one_proof_unproven_beside_a_proven_criterion_closes_and_says_not_proven(self):
        p = one()
        p["criteria"].append({"text": "the project's own test passes", "verify": "python3 test_calc.py"})
        f, r = self.go(p, write(WEAK), FIX_CALC, DONE, DONE, DONE)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        last = events(r)[-1]
        self.assertEqual(last["event"], "closed")
        self.assertIn(f"not proven: {ASKED}", last["text"])
        self.assertNotIn("not proven: the project's own test", last["text"], "a criterion with no proof files is untouched")
        self.assertTrue(any(x.startswith("not proven: " + ASKED) for x in last["unproven"]), last["unproven"])

    def test_a_criterion_with_no_proof_files_is_never_run_on_the_before_copy(self):
        f, r = self.go(one(proof=None), write(WEAK), FIX_CALC, DONE)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.notes(r), [])
        self.assertNotIn("not proven", events(r)[-1]["text"])

    def test_the_before_copy_is_the_start_state_whatever_the_edit_that_changed_it(self):
        fix = call("run", cmd="printf 'def add(a, b):\\n    return a + b\\n' > calc.py")  # a shell edit, no edit tool
        f, r = self.go(one(), write(WEAK), fix, DONE, write(STRONG), DONE)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("passes on the code as it was before your work", sent(f, len(f.seen) - 2),
                      "calc.py was restored from git, not from what the edit tool saw")

    def test_the_working_tree_is_never_touched_and_the_scratch_copy_is_removed(self):
        tmp = os.path.join(self.tmp, "scratch")
        os.makedirs(tmp)
        f, r = self.go(one(), write(WEAK), FIX_CALC, DONE, write(STRONG), DONE, tmp=tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(read_text(os.path.join(self.repo, PROOF)), STRONG)
        self.assertIn("return a + b", self.calc(), "the fix is in the tree the run ends with")
        self.assertEqual([x for x in os.listdir(tmp) if "proof" in x], [], "no scratch copy is left")

    def test_the_overlay_names_only_the_declared_files(self):
        """A file the run wrote that the plan did not name is not laid over: its test file fails there as the old tree does."""
        other = call("write", file="extra_note.py", content="X = 1\n")
        f, r = self.go(one(proof=("extra_note.py",)), write(WEAK), FIX_CALC, other, DONE)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual({n["proof"] for n in self.notes(r)}, {"red"},
                         "proof_add.py was not named, so the copy lacks it and its verify fails there: not a pass")

    def test_a_proof_file_not_written_yet_is_not_checked(self):
        f, r = self.go(one(), FIX_CALC, call("done", summary="fixed"), write(STRONG), DONE)
        self.assertEqual(f.script, [], str(events(r)[-1]))
        self.assertEqual(self.task()["status"], "done")
        self.assertNotIn("before your work", sent(f, 2))

    def test_a_check_that_cannot_run_is_no_pass(self):
        """The verify fails on the before copy for its own reasons (the file imports a module the fix adds): accepted."""
        needs = "import helper_new\nassert helper_new.ok() == 5\n"
        mk = call("write", file="helper_new.py", content="def ok():\n    return 5\n")
        f, r = self.go(one(), mk, write(needs), DONE)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(f.script, [])
        self.assertEqual(self.task()["status"], "done")


TOOL = "#!/bin/sh\npython3 -c 'import calc; assert calc.add(2, 0) == 2'\n"  # a weak proof as a script: passes on the old code
CHMOD = call("run", cmd="chmod +x proof_check.sh")


class Review(CodeCase):
    """T-4590 review: the holes found in the first cut."""
    go, notes = Run.go, Run.notes

    def test_a_proof_file_never_written_is_not_proven_at_the_close(self):
        p = dict(one(), criteria=[{"text": ASKED, "verify": "python3 test_calc.py", "proof": [PROOF]}])
        f, r = self.go(p, FIX_CALC, DONE)
        last = events(r)[-1]
        self.assertEqual(last["event"], "blocked", r.stdout[-600:])
        self.assertIn("not proven", last["reason"])
        self.assertIn(ASKED, last["reason"])
        self.assertIn("never written", last["reason"])
        self.assertNotEqual(self.task()["status"], "done")

    def test_one_of_two_named_proof_files_never_written_is_not_proven_beside_a_proven_criterion(self):
        p = dict(one(), criteria=[{"text": ASKED, "verify": f"python3 {PROOF}", "proof": [PROOF, "proof_two.py"]},
                                  {"text": "the project's own test passes", "verify": "python3 test_calc.py"}])
        f, r = self.go(p, write(STRONG), FIX_CALC, DONE)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        last = events(r)[-1]
        self.assertEqual(last["event"], "closed")
        self.assertIn(f"not proven: {ASKED}", last["text"])
        self.assertNotIn("not proven: the project's own test", last["text"])

    def test_an_executable_proof_script_keeps_its_mode_on_the_before_copy(self):
        """Without the mode the copy's ./proof_check.sh exits 126, reads as red, and the weak proof goes through."""
        caller = call("write", file=PROOF, content="import subprocess, sys\nsys.exit(subprocess.call(['./proof_check.sh']))\n")
        sh = call("write", file="proof_check.sh", content=TOOL)
        strong = call("run", cmd="printf '#!/bin/sh\\npython3 -c \"import calc; assert calc.add(2, 3) == 5\"\\n' > proof_check.sh")
        p = dict(one(), criteria=[{"text": ASKED, "verify": f"python3 {PROOF}", "proof": [PROOF, "proof_check.sh"]}])
        f, r = self.go(p, caller, sh, CHMOD, FIX_CALC, DONE, strong, DONE)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("passes on the code as it was before your work", sent(f, len(f.seen) - 2))

    def test_an_executable_file_restored_from_git_keeps_its_mode(self):
        """tool.sh is tracked and executable; the work edits it, so the before copy gets the HEAD blob and its mode."""
        body = "#!/bin/sh\npython3 -c 'import calc; assert calc.add(2, 3) == 5'\n"
        tool = os.path.join(self.repo, "tool.sh")
        with open(tool, "w") as fh:
            fh.write(body)
        os.chmod(tool, 0o755)
        for a in (("add", "tool.sh"), ("commit", "-q", "-m", "tool")):
            subprocess.run(["git", "-C", self.repo, *a], check=True)
        p = dict(one(), criteria=[{"text": ASKED, "verify": "./tool.sh", "proof": [PROOF]}])
        edit = call("run", cmd="echo '# edited' >> tool.sh")  # in place: the mode stays in the working tree
        f, r = self.go(p, write(WEAK), edit, FIX_CALC, DONE)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual({n["proof"] for n in self.notes(r)}, {"red"},
                         "./tool.sh ran in the copy (and failed on the old calc): not a 126, not skipped")

    def test_a_verify_that_cannot_run_on_the_before_copy_is_skipped_with_a_note(self):
        """The verify runs a script the work made and the plan did not name: 127 there is no proof failing."""
        mk = call("write", file="newtool.sh", content="#!/bin/sh\npython3 -c 'import calc'\n")
        mod = call("run", cmd="chmod +x newtool.sh")
        p = dict(one(), criteria=[{"text": ASKED, "verify": f"python3 {PROOF} && ./newtool.sh", "proof": [PROOF]}])
        f, r = self.go(p, write(WEAK), mk, mod, FIX_CALC, DONE)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        got = self.notes(r)
        self.assertEqual({n["proof"] for n in got}, {"skipped"}, got)
        self.assertIn("could not run", " ".join(n.get("why", "") for n in got))
        self.assertNotIn("not proven", events(r)[-1]["text"])

    def test_a_verify_naming_the_trees_absolute_path_is_skipped_with_a_note(self):
        """The copy would run the fixed tree: a pass there proves nothing, so the criterion is not checked."""
        p = dict(one(), criteria=[{"text": ASKED, "verify": f"python3 {self.repo}/{PROOF}", "proof": [PROOF]}])
        f, r = self.go(p, write(WEAK), FIX_CALC, DONE)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        got = self.notes(r)
        self.assertEqual({n["proof"] for n in got}, {"skipped"}, got)
        self.assertIn("path", " ".join(n.get("why", "") for n in got))
        self.assertNotIn("before your work", "\n".join(sent(f, i) for i in range(len(f.seen))))
        self.assertNotIn("not proven", events(r)[-1]["text"])


class Resume(CodeCase):
    def kill_after(self, p, *script):
        """A run that did its work and hangs at the next request, then killed."""
        first = self.serve("openai", plan(p), *script, HANG)
        proc = subprocess.Popen([sys.executable, FM, "code", "fix add", "--model", "fake"], cwd=self.repo, env=self.env(),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertTrue(first.hung.wait(60), "fm code never reached the hanging request")
            proc.kill()
        finally:
            proc.communicate(timeout=30)
        self.assertEqual(self.task()["status"], "active")

    def journal_path(self):
        return os.path.join(os.path.dirname(os.path.dirname(self.task()["path"])), "code", "T-0001.jsonl")

    def drop_base(self, replace=None):
        path = self.journal_path()
        kept = []
        for x in read_text(path).splitlines():
            if x.strip() and json.loads(x).get("kind") == "proof_base":
                if replace:
                    kept.append(json.dumps(replace))
                continue
            if x.strip():
                kept.append(x)
        with open(path, "w") as fh:
            fh.write("\n".join(kept) + "\n")

    def test_the_base_is_journalled_at_the_start(self):
        self.kill_after(one(), write(WEAK), FIX_CALC)
        recs = [json.loads(x) for x in read_text(self.journal_path()).splitlines() if x.strip()]
        got = [x for x in recs if x.get("kind") == "proof_base"]
        self.assertEqual(len(got), 1, got)
        self.assertIsInstance(got[0]["dirty"], dict)
        self.assertEqual(len(got[0]["head"]), 2)

    def test_a_resumed_run_still_tells_a_proof_that_passes_before_the_work(self):
        self.kill_after(one(), write(WEAK), FIX_CALC)
        f = self.serve("openai", DONE, write(STRONG), DONE)
        r = self.code("--resume", "--model", "fake", "--json", check=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(f.script, [])
        self.assertIn("passes on the code as it was before your work", sent(f, len(f.seen) - 2))
        self.assertEqual(self.task()["status"], "done")

    def test_a_resumed_run_with_a_weak_proof_never_mended_closes_not_proven(self):
        self.kill_after(one(), write(WEAK), FIX_CALC)
        self.serve("openai", DONE, DONE, DONE)
        r = self.code("--resume", "--model", "fake", "--json", check=False)
        last = events(r)[-1]
        self.assertEqual(last["event"], "blocked", r.stdout[-600:])
        self.assertIn("not proven", last["reason"])
        self.assertNotEqual(self.task()["status"], "done")

    def test_a_resume_that_cannot_restore_the_base_says_the_check_is_off_and_proves_nothing(self):
        self.kill_after(one(), write(WEAK), FIX_CALC)
        self.drop_base()  # a journal that holds no base (lost, or too large to keep)
        self.serve("openai", DONE)
        r = self.code("--resume", "--model", "fake", "--json", check=False)
        notes = [e for e in events(r) if e["event"] == "note" and e.get("proof") == "off"]
        self.assertEqual(len(notes), 1, r.stdout[-800:])
        last = events(r)[-1]
        self.assertEqual(last["event"], "blocked", r.stdout[-600:])
        self.assertIn("not proven", last["reason"])
        self.assertNotEqual(self.task()["status"], "done")

    def test_a_resume_of_a_run_whose_check_was_off_from_the_start_is_not_made_unproven(self):
        self.kill_after(one(), write(WEAK), FIX_CALC)
        self.drop_base({"kind": "proof_base", "off": "the folder is not the top of a git tree"})
        self.serve("openai", DONE)
        r = self.code("--resume", "--model", "fake", "--json", check=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("not proven", events(r)[-1]["text"])
        self.assertEqual(self.task()["status"], "done")

    def test_a_unproven_criterion_is_replayed_from_the_journal(self):
        from test_code import journal_text
        f = self.serve("openai", plan(dict(one(), criteria=[
            {"text": ASKED, "verify": f"python3 {PROOF}", "proof": [PROOF]},
            {"text": "the project's own test passes", "verify": "python3 test_calc.py"}])),
            write(WEAK), FIX_CALC, DONE, DONE, DONE)
        self.code("fix add", "--model", "fake", "--json")
        recs = [json.loads(x) for x in journal_text(self).splitlines() if x.strip()]
        got = [r for r in recs if r.get("kind") == "proof_unproven"]
        self.assertEqual(len(got), 1, got)
        self.assertEqual(got[0]["items"][0]["text"], ASKED)
