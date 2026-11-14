"""T-4648: a CLEAN task built in a lane records its behaviour lock with a bounded run. The close wants the task's tests run
before its first edit (sampo check --evidence ID --step 1), and the builder hook refuses a builder that run (T-4514: the
full check doubled a 30 to 50 minute run), so every lane-built CLEAN task ended in a refused close and a hand-written
"none: …" section. `sampo check --evidence ID --step 1 --lock` runs only the tests linked to the files the brief's scope
names (capped in modules), through the same runner, and records them as the lock when the tree is still the lane's start
tree; the close counts that record and nothing run after an edit. The full check stays refused for a builder, and its
refusal names the bounded form.
reruns: nothing from another module; one fixture of its own (a repo with a calc module, its test and a lane)."""
# map-links: plugin/lib/sampo/cli.py, plugin/lib/sampo/hooks.py, plugin/lib/sampo/core.py, plugin/lib/sampo/lanes.py
import json
import os
import shutil
import subprocess
import unittest

from helpers import ForemanTestCase

from sampo import cli
from sampo import core as c

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = "def add(a, b):\n    return a + b\n"
BUILDER = {"agent_id": "a42cb51b932d1111a", "agent_type": "sampo:fm-builder"}


class LaneLock(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.count = os.path.join(self.tmp, "count")  # outside the repo: a run must not change the tree it is judged on
        os.makedirs(os.path.join(self.repo, "tests"))
        shutil.copy(os.path.join(HERE, "run.py"), os.path.join(self.repo, "tests", "run.py"))
        self.put(self.repo, "calc.py", SRC)
        self.put(self.repo, "tests/test_calc.py", self.calc_test())
        self.put(self.repo, ".gitignore", "__pycache__/\n.fm-scratch/\n")
        subprocess.run(["git", "-C", self.repo, "add", "-A"], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", "suite"], check=True, capture_output=True)
        self.fm("check", "add", "python3 tests/run.py")
        self.fm("check", "affected", "python3 tests/run.py {names}")
        self.lane = os.path.join(self.tmp, "lane1")
        subprocess.run(["git", "-C", self.repo, "worktree", "add", "-q", "-b", "foreman/lane1", self.lane], check=True)
        self.p = c.find_project(self.repo)
        self.lp = c.find_project(self.lane)  # the lane's view: its root is the worktree

    def calc_test(self, name="Calc"):
        return ("import os, sys, unittest\nsys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))\nimport calc\n\n\n"
                f"class {name}(unittest.TestCase):\n    def test_add(self):\n"
                f"        open({self.count!r}, 'a').write('x\\n')\n        self.assertEqual(calc.add(1, 2), 3)\n")

    def put(self, root, rel, text):
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def runs(self):
        try:
            with open(self.count) as f:
                return len(f.read().split())
        except OSError:
            return 0

    def task(self, *scope, type_="CLEAN"):
        """A task in the lane, focused there (its start tree taken); its id."""
        args = [x for s in scope for x in ("--scope", s)]
        tid = self.fm_json("task", "new", "Tidy calc", "--type", type_, "--tier", "S", "--ac", "ok :: true",
                           "--step", "s", *args, "--focus", cwd=self.lane)["id"]
        return tid

    def lock(self, tid, cwd=None, check=False, **kw):
        return self.fm("check", "--evidence", tid, "--step", "1", "--lock", cwd=cwd or self.lane, check=check, **kw)

    def edit_time(self, tid, ts="2000-01-01T00:00:00Z"):
        """The ledger line the edit hook writes for the task's first edit in the lane."""
        with open(os.path.join(self.p.dir, "ledger.jsonl"), "a") as f:
            f.write(json.dumps({"ts": ts, "task": tid, "event": "touched",
                                "data": {"file": os.path.join(self.lane, "calc.py"), "tool": "Edit"}}) + "\n")

    def gaps(self, tid, first_edit):
        return cli._kind_gaps(c.find_brief(self.p, tid), first_edit)

    def evidence(self, tid):
        return c.find_brief(self.p, tid).evidence()

    # 1 + 4: the bounded form records the lock, and the close counts it
    def test_the_bounded_form_runs_the_linked_tests_once_and_records_them_as_the_lock(self):
        tid = self.task("calc.py")
        r = self.lock(tid)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.runs(), 1, "the linked test module ran, once")
        self.assertIn("test_calc", r.stdout)
        line = [e for e in self.evidence(tid) if c.LOCK_LABEL in e]
        self.assertEqual(len(line), 1, self.evidence(tid))
        self.assertIn("(step 1)", line[0])
        self.assertIn("[ran]", line[0])
        self.assertRegex(line[0], r"\[tree [0-9a-f]{12}\]")
        self.assertTrue(c.find_brief(self.p, tid).lock_at_start())

    def test_a_lane_built_clean_close_with_the_record_passes_the_lock_gate_without_a_written_section(self):
        tid = self.task("calc.py")
        self.assertEqual(self.lock(tid).returncode, 0)
        self.put(self.lane, "calc.py", SRC + "# tidy\n")
        self.edit_time(tid, c.now())  # the first edit comes after the lock run
        self.assertEqual(self.gaps(tid, c.first_touch(self.lp, tid)), [])
        self.assertEqual(c.find_brief(self.p, tid).section("Behaviour lock").strip(), "")

    def test_without_the_record_the_close_refuses_as_before(self):
        tid = self.task("calc.py")
        self.edit_time(tid)
        got = self.gaps(tid, c.first_touch(self.lp, tid))
        self.assertEqual(len(got), 1, got)
        self.assertIn("no behaviour lock", got[0])
        self.assertIn(f"sampo check --evidence {tid} --step 1", got[0])

    def test_a_lock_run_after_the_first_edit_does_not_count(self):
        tid = self.task("calc.py")
        self.put(self.lane, "calc.py", SRC + "# tidy\n")  # the first edit, before any lock run
        self.edit_time(tid)
        r = self.lock(tid)
        self.assertIn("after an edit", r.stdout + r.stderr)
        self.assertFalse(c.find_brief(self.p, tid).lock_at_start())
        got = self.gaps(tid, c.first_touch(self.lp, tid))
        self.assertEqual(len(got), 1, got)
        self.assertIn("no behaviour lock", got[0])
        # an edit made by Bash is seen late by the hook: a first-edit time after the run must not rescue the late run
        self.assertEqual(len(self.gaps(tid, "2099-01-01T00:00:00Z")), 1)

    def test_a_failing_lock_run_is_no_lock(self):
        self.put(self.lane, "tests/test_calc.py", self.calc_test().replace(", 3)", ", 4)"))
        subprocess.run(["git", "-C", self.lane, "commit", "-qam", "break"], check=True, capture_output=True)
        tid = self.task("calc.py")  # the lane's start tree has the red test on it
        r = self.lock(tid)
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertFalse(c.find_brief(self.p, tid).lock_at_start())

    # 5: nothing to run
    def test_no_scope_says_so_and_records_nothing(self):
        tid = self.task()
        r = self.lock(tid)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no scope", r.stdout + r.stderr)
        self.assertEqual(self.runs(), 0)
        self.assertFalse([e for e in self.evidence(tid) if "sampo check" in e], self.evidence(tid))

    def test_a_scope_with_no_linked_test_says_so_and_records_nothing(self):
        self.put(self.lane, "notes.txt", "x\n")
        subprocess.run(["git", "-C", self.lane, "add", "notes.txt"], check=True)
        subprocess.run(["git", "-C", self.lane, "commit", "-qm", "notes"], check=True, capture_output=True)
        tid = self.task("notes.txt")
        r = self.lock(tid)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no tests linked", r.stdout + r.stderr)
        self.assertFalse([e for e in self.evidence(tid) if "sampo check" in e], self.evidence(tid))

    # bounded
    def test_the_run_is_capped_in_modules_and_says_how_many_were_left_out(self):
        for n in range(cli.LOCK_MODULES + 3):
            self.put(self.lane, f"tests/test_zz_{n:02d}.py", "# map-links: calc.py\n" + self.calc_test(f"C{n}"))  # not named for calc.py, linked by a marker
        subprocess.run(["git", "-C", self.lane, "add", "-A"], check=True)
        subprocess.run(["git", "-C", self.lane, "commit", "-qm", "more tests"], check=True, capture_output=True)
        tid = self.task("calc.py")
        r = self.lock(tid)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.runs(), cli.LOCK_MODULES, "the cap, not every linked module")
        self.assertIn(f"{cli.LOCK_MODULES} of {cli.LOCK_MODULES + 4}", r.stdout)
        self.assertIn(c.LOCK_LABEL, "".join(self.evidence(tid)))

    # not for the main checkout, and it records nothing there
    def test_the_bounded_form_is_a_lane_builders_not_the_main_checkouts(self):
        self.fm("task", "new", "Tidy calc", "--type", "CLEAN", "--tier", "S", "--ac", "ok :: true", "--step", "s",
                "--scope", "calc.py", "--focus")
        r = self.lock("T-0001", cwd=self.repo)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("lane", r.stdout + r.stderr)
        self.assertEqual(self.runs(), 0)
        self.assertFalse(c.find_brief(self.p, "T-0001").lock_at_start())

    def test_a_hand_typed_evidence_line_is_no_lock(self):
        tid = self.task("calc.py")
        self.edit_time(tid)
        self.fm("task", "evidence", tid, "--step", "1", c.LOCK_LABEL + "x", "exit 0", cwd=self.lane)
        self.assertIn(c.LOCK_LABEL, "".join(self.evidence(tid)), "recorded, typed")
        self.assertFalse(c.find_brief(self.p, tid).lock_at_start(), "typed evidence carries no ran mark")
        self.assertEqual(len(self.gaps(tid, c.first_touch(self.lp, tid))), 1)

    # 2 + the hook: a builder may run the bounded form, never the wide ones
    def pre(self, cmd, **ids):
        return self.hook("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": self.lane,
                                        "session_id": "sess-b", **ids})

    def test_the_builder_hook_allows_the_bounded_form_and_the_full_form_refusal_names_it(self):
        ok = self.pre("sampo check --evidence T-0001 --step 1 --lock", **BUILDER)
        self.assertNotEqual(ok.returncode, 2, ok.stdout + ok.stderr)
        self.assertNotIn("sampo lane accept", ok.stdout + ok.stderr)
        for cmd in ("sampo check --evidence T-0001 --step 1", "sampo check", "sampo check --affected --lock",
                    "sampo quiet -- sampo check --affected"):
            r = self.pre(cmd, **BUILDER)
            self.assertEqual(r.returncode, 2, (cmd, r.stdout, r.stderr))
            why = json.loads(r.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
            self.assertEqual(len(why.splitlines()), 1, why)
            self.assertIn("--lock", why, cmd)
            self.assertIn("sampo lane accept", why, cmd)

    # 3: the brief
    def test_the_builder_brief_of_a_clean_task_names_the_bounded_form_in_one_line(self):
        tid = self.fm_json("task", "new", "Tidy", "--type", "CLEAN", "--tier", "S", "--ac", "ok :: true", "--step", "s",
                           "--scope", "calc.py")["id"]
        with open(self.fm_json("lane", "brief", tid)["path"]) as f:
            text = f.read()
        lines = [ln for ln in text.splitlines() if f"fm check --evidence {tid} --step 1 --lock" in ln]
        self.assertEqual(len(lines), 1, text)
        self.assertIn("before your first edit", lines[0])
        feature = self.fm_json("task", "new", "Add", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true", "--step", "s")["id"]
        with open(self.fm_json("lane", "brief", feature)["path"]) as f:
            self.assertNotIn("--lock", f.read())


if __name__ == "__main__":
    unittest.main()
