"""T-0705 (Frontier 05, first slice): ambient verification. Affected tests run after each edit, out of the model's
turns; only a flip reaches it, once; a pass never closes a step (T-0994)."""
import json
import os
import subprocess

from test_hooks import HookCase, parse

from sampo import core as c

SYNC = {"FOREMAN_AMBIENT_SYNC": "1"}
GOOD = "def add(a, b):\n    return a + b\n"
TEST = "import unittest\nimport calc\n\n\nclass T(unittest.TestCase):\n    def test_add(self):\n" \
       "        self.assertEqual(calc.add(1, 2), 3)\n"


class Ambient(HookCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.write("calc.py", GOOD)
        self.write("test_calc.py", TEST)
        self.write(".gitignore", "__pycache__/\n")  # a test run's bytecode is not a change to the tree
        subprocess.run(["git", "-C", self.repo, "add", "-A"], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", "calc"], check=True)
        self.fm("check", "affected", "python3 -m unittest -q {names}")
        self.tid = self.task(type_="FEATURE", steps=("change add", "wire it"))

    def write(self, rel, text):
        with open(os.path.join(self.repo, rel), "w") as f:
            f.write(text)

    def edit(self, text):
        self.write("calc.py", text)
        self.hook("PostToolUse", {"tool_name": "Edit", "tool_input": {"file_path": os.path.join(self.repo, "calc.py")},
                                  "tool_response": {}}, env=SYNC, timeout=60)

    def note(self):
        r = parse(self.hook("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": "ls"}})) or {}
        return (r.get("hookSpecificOutput") or {}).get("additionalContext") or ""

    def test_off_by_default(self):
        self.edit("def add(a, b):\n    return a - b\n")
        self.assertNotIn("Ambient", self.note())

    def test_a_flip_is_reported_once_and_a_pass_never_closes_the_step(self):
        self.fm("check", "ambient", "on")
        self.edit("def add(a, b):\n    return a - b\n")
        first = self.note()
        self.assertIn("now fail", first)
        self.assertIn("test_calc", first)
        self.assertNotIn("Ambient", self.note(), "a flip is said once")
        self.edit("def add(a, b):\n    return a - b  # still wrong\n")
        self.assertNotIn("Ambient", self.note(), "still failing is not a flip")
        self.edit(GOOD)
        back = self.note()
        self.assertIn("pass again", back)
        # T-0994: a pass mid-step says nothing about the step being done (it closed T-0989's 'docs' step); the note
        # says how to record it when it is
        self.assertIn(f"fm task evidence {self.tid} --step 1 --run", back)
        b = c.find_brief(self.project(), self.tid)
        self.assertFalse(b.has_evidence(step=1), b.evidence())

    def verdict(self):
        try:
            with open(os.path.join(self.project().dir, "ambient", "verdict.json")) as f:
                return json.load(f)
        except OSError:
            return None

    def focus_task(self):
        """A task whose criterion passes, focused before any edit (its base is the tree it starts from)."""
        tid = self.fm_json("task", "new", "Add", "--type", "FEATURE", "--tier", "S", "--ac", "works :: true",
                           "--step", "s", "--scope", "calc.py")["id"]
        self.fm("focus", tid)
        return tid

    def close(self, tid):
        """The close runs (or reuses) the affected tests for the edits since the task's base."""
        return self.fm("task", "finish", tid, "--audit", "self", "--run", "true", check=False,
                       env={"FOREMAN_SENTINEL": "off"})

    def close_events(self):
        return c.ledger_events(self.project(), ("close_affected",))

    def test_a_clean_ambient_run_records_a_pass_for_the_tree_it_ran_on(self):
        # T-3270: the close reuses this verdict only for the same command on the same working tree
        self.fm("check", "ambient", "on")
        self.edit(GOOD + "# touched\n")
        v = self.verdict()
        self.assertIsNotNone(v)
        self.assertEqual(v["exit"], 0)
        self.assertIn("test_calc", v["cmd"])
        self.assertEqual(v["tree"], c.worktree_tree(self.repo))

    def test_a_run_that_moves_the_tree_records_no_verdict(self):
        # T-3270: a test that writes a file changes the tree during its run: that run proves nothing about any tree
        self.fm("check", "ambient", "on")
        self.write("test_calc.py", TEST + "\n    def test_writes(self):\n        open('scratch.txt', 'w').write('x')\n")
        self.edit(GOOD + "# touched\n")
        self.assertIsNone(self.verdict())

    def test_a_run_an_edit_landed_in_writes_no_pass(self):
        # review: an edit then a revert during the run leaves the tree id unchanged; the dirty marker still says it moved
        from sampo import ambient
        p = self.project()
        os.makedirs(os.path.join(p.dir, "ambient"), exist_ok=True)
        open(os.path.join(p.dir, "ambient", "dirty"), "w").close()
        ambient._write_pass(p, "python3 -m unittest -q test_calc", c.worktree_tree(self.repo), 0)
        self.assertIsNone(self.verdict())

    def stored(self, cmd, **over):
        """A pass for the tree as it is now, written the way _write_pass writes it, with some fields overridden."""
        v = {"cmd": cmd, "tree": c.worktree_tree(self.repo), "exit": 0, "env": c.env_id(), "ts": c.now()}
        v.update(over)
        os.makedirs(os.path.join(self.project().dir, "ambient"), exist_ok=True)
        with open(os.path.join(self.project().dir, "ambient", "verdict.json"), "w") as f:
            json.dump(v, f)

    def test_a_pass_from_another_environment_is_not_reused(self):
        from sampo import ambient
        cmd = "python3 -m unittest -q test_calc"
        self.stored(cmd, env="0badenv0")
        self.assertFalse(ambient.passed(self.project(), cmd))
        self.stored(cmd)  # control: the same pass in this environment is reused
        self.assertTrue(ambient.passed(self.project(), cmd))

    def test_a_pass_older_than_the_cache_window_is_not_reused(self):
        from sampo import ambient
        cmd = "python3 -m unittest -q test_calc"
        self.stored(cmd, ts="2020-01-01T00:00:00Z")
        self.assertFalse(ambient.passed(self.project(), cmd))
        self.stored(cmd)  # control: a fresh pass is reused
        self.assertTrue(ambient.passed(self.project(), cmd))

    def test_a_close_reuses_the_ambient_pass_for_the_same_command_and_tree(self):
        self.fm("check", "ambient", "on")
        tid = self.focus_task()
        self.edit(GOOD + "# touched\n")
        r = self.close(tid)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        last = self.close_events()[-1]["data"]
        self.assertIs(last.get("cached"), True, last)
        self.assertEqual(last.get("exit"), 0)

    def test_a_close_reruns_when_the_tree_moved_since_the_pass(self):
        self.fm("check", "ambient", "on")
        tid = self.focus_task()
        self.edit(GOOD + "# touched\n")
        self.write("calc.py", "def add(a, b):\n    return a - b\n")  # no ambient run: the tree moved and is red
        r = self.close(tid)
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("test_calc", r.stdout + r.stderr)
        self.assertIs(self.close_events()[-1]["data"].get("cached"), False)

    def test_a_close_reruns_after_a_red_ambient_run(self):
        self.fm("check", "ambient", "on")
        tid = self.focus_task()
        self.edit("def add(a, b):\n    return a - b\n")
        self.assertEqual(self.verdict()["exit"], 1)
        r = self.close(tid)
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIs(self.close_events()[-1]["data"].get("cached"), False)

    def test_a_full_check_waits_for_an_ambient_run(self):
        # T-1964: a full fm check started while an ambient run was going ran two suites at once
        import threading
        self.fm("check", "add", "true")
        busy = subprocess.Popen(["sleep", "2"])
        threading.Thread(target=busy.wait, daemon=True).start()  # reaped when it ends, so its pid dies
        d = os.path.join(self.project().dir, "ambient")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "run.pid"), "w") as f:
            f.write(str(busy.pid))
        r = self.fm("check")
        self.assertIn("waiting for the ambient test run", r.stderr)
        self.assertIsNotNone(busy.poll(), "it ran only after the ambient run ended")

    def test_a_full_check_waits_for_another_on_this_tree(self):
        # T-1971: a second full fm check ran a suite beside the first one and cleared its gates_running marker
        import threading
        self.fm("check", "add", "true")
        busy = subprocess.Popen(["sleep", "2"])
        threading.Thread(target=busy.wait, daemon=True).start()
        p = self.project()
        c.update_meta(p, gates_running={"task": None, "pid": busy.pid, "at": c.now(), "root": p.root})
        r = self.fm("check")
        self.assertIn("waiting for another sampo check", r.stderr)
        self.assertIsNotNone(busy.poll(), "it ran only after the other check ended")

    def test_a_full_check_waits_for_fm_ships_test_run_but_not_inside_it(self):
        # T-1978: fm ship's suite in its scratch tree had no marker, so a full check here ran a second suite beside it
        import threading
        self.fm("check", "add", "true")
        p = self.project()
        busy = subprocess.Popen(["sleep", "30"])
        try:  # the ship's own fm check runs in its scratch tree: it never waits for itself
            c.update_meta(p, ship_testing={"pid": busy.pid, "at": c.now(), "root": p.root})
            self.assertNotIn("fm ship", self.fm("check", "--fresh").stderr)
        finally:
            busy.kill()
            busy.wait()
        busy = subprocess.Popen(["sleep", "2"])
        threading.Thread(target=busy.wait, daemon=True).start()
        c.update_meta(p, ship_testing={"pid": busy.pid, "at": c.now(), "root": "/elsewhere/ship"})
        self.assertIn("waiting for sampo ship's test run", self.fm("check", "--fresh").stderr)
        self.assertIsNotNone(busy.poll(), "it ran only after the ship's test run ended")

    def test_a_check_whose_tree_changed_mid_run_starts_an_ambient_run(self):
        # T-1973: an edit made while full gates ran got no ambient run (T-1965) and no pass (T-1963): nothing tested it
        self.fm("check", "ambient", "on")
        self.write("brk.py", "open('calc.py', 'w').write('def add(a, b):\\n    return a - b\\n')\n")
        self.fm("check", "add", "python3 brk.py")  # stands in for an edit made while the gates ran
        self.fm("check", "--fresh", env=SYNC, check=False)
        self.assertIn("now fail", self.note())

    def test_an_edit_during_fm_ships_test_run_starts_no_ambient_run(self):
        # T-1984: fm ship's suite runs in its scratch tree; an ambient run beside it was two suites at once
        self.fm("check", "ambient", "on")
        p = self.project()
        c.update_meta(p, ship_testing={"pid": os.getpid(), "at": c.now(), "root": "/elsewhere/ship"})
        self.edit("def add(a, b):\n    return a - b\n")
        self.assertNotIn("Ambient", self.note(), "fm ship starts one after its run")
        c.update_meta(p, ship_testing=None)

    def test_an_edit_during_full_gates_starts_no_ambient_run(self):
        # T-1965: an edit while fm check ran started an ambient suite beside it
        self.fm("check", "ambient", "on")
        p = self.project()
        c.update_meta(p, gates_running={"task": None, "pid": os.getpid(), "at": c.now(), "root": p.root})
        self.edit("def add(a, b):\n    return a - b\n")
        self.assertNotIn("Ambient", self.note(), "the gates answer for this edit")
        c.update_meta(p, gates_running={})
        self.edit("def add(a, b):\n    return a - b\n")
        self.assertIn("now fail", self.note())

    def test_a_failing_note_names_the_failing_tests_not_the_runners_timing(self):
        # T-1275: 'Ran 44 tests in 1.6s (12 classes …)' made these notes 23 KB over 92 edits
        self.fm("check", "ambient", "on")
        self.write("test_calc.py", TEST + "\n    def test_sub(self):\n        self.assertEqual(calc.add(3, 1), 4)\n")
        self.edit("def add(a, b):\n    return a - b\n")
        first = self.note()
        self.assertIn("test_add", first)
        self.assertIn("test_sub", first)
        self.assertNotIn("Ran ", first)
        self.assertNotIn("classes", first)
        self.assertLessEqual(len(first.split("Ambient tests:")[1]) + len("Ambient tests:"), 220, first)
        self.edit(GOOD)
        back = self.note()
        self.assertIn("pass again", back)
        self.assertLessEqual(len(back), 200, back)
        from sampo import ambient  # pytest's summary lines name the failures too
        out = "FAILED tests/test_a.py::test_one - AssertionError: x\nFAILED tests/test_b.py::test_two\n"
        self.assertEqual([m.group(1) or m.group(2) for m in ambient.FAILED.finditer(out)],
                         ["tests/test_a.py::test_one", "tests/test_b.py::test_two"])

    def test_a_test_the_runner_cant_run_is_left_out(self):
        # T-0994: a .tsx test went to 'python3 plugin/tests/run.py widget.test', which ran nothing and read as a pass
        import json
        self.write("widget.test.tsx", "test('x', () => {})\n")
        self.write("calc.py", "def add(a, b):\n    return a + b  # touched\n")
        out = json.loads(self.fm("check", "--affected", "--json").stdout)
        self.assertIn("test_calc.py", out["tests"])
        self.assertNotIn("widget.test.tsx", out["tests"])
