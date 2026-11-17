"""T-3277 (Moon C-001115): each edit's replay is ruled keep / neutral / harmful at the edit, per file, in a ledger."""
import json
import os
import subprocess
import unittest

from test_hooks import HookCase, parse

from sampo import core as c
from sampo import editverdict as ev

SYNC = {"FOREMAN_AMBIENT_SYNC": "1"}
GOOD = "def add(a, b):\n    return a + b\n"
BAD = "def add(a, b):\n    return a - b\n"
TEST = "import unittest\nimport calc\n\n\nclass T(unittest.TestCase):\n    def test_add(self):\n" \
       "        self.assertEqual(calc.add(1, 2), 3)\n"


class Verdict(unittest.TestCase):
    def test_a_pass_that_turns_red_is_harmful(self):
        self.assertEqual(ev.verdict(0, 1, ["calc.py"], {"calc.py": ["test_calc.py"]}), "harmful")
        self.assertEqual(ev.verdict(None, 1, ["calc.py"], {"calc.py": ["test_calc.py"]}), "harmful", "no earlier run: a pass")

    def test_red_turning_green_or_a_linked_test_passing_is_keep(self):
        linked = {"calc.py": ["test_calc.py"]}
        self.assertEqual(ev.verdict(1, 0, ["calc.py"], linked), "keep")
        self.assertEqual(ev.verdict(0, 0, ["calc.py"], linked), "keep", "a linked test passed over the edit")
        self.assertEqual(ev.verdict(None, 0, ["calc.py"], ["test_calc.py"]), "keep", "a plain list links every file")

    def test_no_linked_test_or_no_change_in_a_red_is_neutral(self):
        self.assertEqual(ev.verdict(0, 0, ["notes.txt"], {"calc.py": ["test_calc.py"]}), "neutral")
        self.assertEqual(ev.verdict(0, 0, ["notes.txt"], {}), "neutral")
        self.assertEqual(ev.verdict(0, 0, ["notes.txt"], []), "neutral")
        self.assertEqual(ev.verdict(1, 1, ["calc.py"], {"calc.py": ["test_calc.py"]}), "neutral", "red stayed red")
        self.assertEqual(ev.verdict(0, None, ["calc.py"], {}), "neutral", "no run, no ruling")

    def test_a_hostile_input_never_raises(self):
        for linked in (None, 3, {"calc.py": None}, {"calc.py": 5}, "test_calc.py"):
            self.assertIn(ev.verdict(0, 0, ["calc.py"], linked), ("keep", "neutral", "harmful"))
        self.assertEqual(ev.verdict(0, 1, [], {}), "harmful")


class Ledger(HookCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.write("calc.py", GOOD)
        self.write("test_calc.py", TEST)
        self.write("notes.txt", "n\n")
        self.write(".gitignore", "__pycache__/\n")
        subprocess.run(["git", "-C", self.repo, "add", "-A"], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", "calc"], check=True)
        self.fm("check", "affected", "python3 -m unittest -q {names}")
        self.task(type_="FEATURE", steps=("change add", "wire it"))
        self.fm("check", "ambient", "on")

    def write(self, rel, text):
        with open(os.path.join(self.repo, rel), "w") as f:
            f.write(text)

    def edit(self, rel, text):
        self.write(rel, text)
        self.touched(rel)

    def touched(self, rel):
        self.hook("PostToolUse", {"tool_name": "Edit", "tool_input": {"file_path": os.path.join(self.repo, rel)},
                                  "tool_response": {}}, env=SYNC, timeout=60)

    def note(self):
        r = parse(self.hook("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": "ls"}})) or {}
        return (r.get("hookSpecificOutput") or {}).get("additionalContext") or ""

    def rows(self):
        try:
            with open(os.path.join(self.project().dir, "ambient", "edits.jsonl")) as f:
                return [json.loads(l) for l in f if l.strip()]
        except OSError:
            return []

    def test_one_line_per_edited_file_and_a_harmful_note_names_it(self):
        self.edit("calc.py", GOOD + "# fine\n")
        self.assertEqual([(r["file"], r["verdict"]) for r in self.rows()], [("calc.py", "keep")])
        self.note()
        self.edit("calc.py", BAD)
        bad = self.rows()[-1]
        self.assertEqual((bad["file"], bad["verdict"], bad["exit"]), ("calc.py", "harmful", 1))
        text = self.note()
        self.assertIn("now fail", text)
        self.assertIn("calc.py", text, "the harmful note names the edited file")
        self.edit("calc.py", GOOD)
        self.assertEqual(self.rows()[-1]["verdict"], "keep", "red turning green")

    def test_a_file_no_test_links_is_neutral(self):
        self.edit("notes.txt", "changed\n")
        rows = self.rows()
        self.assertEqual([(r["file"], r["verdict"]) for r in rows], [("notes.txt", "neutral")], rows)

    def test_edits_that_land_before_one_run_each_get_a_line(self):
        env = {"FOREMAN_NO_BACKGROUND": "1"}  # queued, no run
        self.write("calc.py", GOOD + "# a\n")
        self.hook("PostToolUse", {"tool_name": "Edit", "tool_input": {"file_path": os.path.join(self.repo, "calc.py")},
                                  "tool_response": {}}, env=env)
        self.assertEqual(self.rows(), [], "no run yet, no ruling")
        self.edit("notes.txt", "b\n")
        self.assertEqual(sorted(r["file"] for r in self.rows()), ["calc.py", "notes.txt"])
        self.edit("notes.txt", "c\n")
        self.assertEqual(len(self.rows()), 3, "a taken edit is not ruled twice")

    def test_an_edit_outside_the_project_is_not_queued(self):
        self.hook("PostToolUse", {"tool_name": "Edit", "tool_input": {"file_path": "/etc/hostname"},
                                  "tool_response": {}}, env=SYNC, timeout=60)
        self.assertEqual(self.rows(), [])

    def test_check_ambient_edits_lists_the_last_verdict_per_file(self):
        self.edit("calc.py", GOOD + "# fine\n")
        self.edit("calc.py", BAD)
        self.edit("notes.txt", "changed\n")
        out = self.fm("check", "ambient", "edits").stdout
        self.assertIn("calc.py", out)
        self.assertIn("harmful", out)
        self.assertIn("notes.txt", out)
        self.assertEqual(out.count("calc.py"), 1, "the last verdict per file, not the history")
        data = self.fm_json("check", "ambient", "edits")
        files = {r["file"]: r["verdict"] for r in data["edits"]}
        self.assertEqual(files, {"calc.py": "harmful", "notes.txt": "neutral"})

    def test_the_ledger_and_the_queue_stay_bounded(self):
        p = self.project()
        for i in range(ev.LEDGER_MAX + 50):
            ev.record(p, [f"f{i}.py"], 0, 0, {}, None)
        self.assertLessEqual(len(self.rows()), ev.LEDGER_MAX)
        self.assertEqual(self.rows()[-1]["file"], f"f{ev.LEDGER_MAX + 49}.py")
        for i in range(ev.BATCH_MAX + 20):
            ev.queue(p, os.path.join(p.root, f"q{i}.py"))
        taken = ev.take(p)
        self.assertEqual(len(taken), ev.BATCH_MAX)
        ev.done(p)
        self.assertEqual(len(ev.take(p)), 20, "what a run's batch left over waits for the next one")
        ev.done(p)
        self.assertEqual(ev.take(p), [])

    def test_a_run_that_cannot_rule_a_batch_leaves_it_queued(self):  # T-3277 review 1
        self.fm("check", "affected", "--clear")  # no template: cli.affected raises inside the run
        self.edit("notes.txt", "changed\n")
        self.assertEqual(self.rows(), [])
        self.assertEqual(ev.take(self.project()), ["notes.txt"], "the edit is still queued")

    def test_a_batch_a_dead_run_took_is_queued_again(self):  # T-3277 review 1: a SIGTERM skips every finally
        p = self.project()
        d = os.path.join(p.dir, "ambient")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "pending.4194999"), "w") as f:  # no such process
            f.write(json.dumps({"r": p.root, "f": "calc.py"}) + "\n")
        self.assertEqual(ev.take(p), ["calc.py"])

    def _ambient_dir(self):
        d = os.path.join(self.project().dir, "ambient")
        os.makedirs(d, exist_ok=True)
        return d

    def _plant(self, name, rel="calc.py"):
        with open(os.path.join(self._ambient_dir(), name), "w") as f:
            f.write(json.dumps({"r": self.project().root, "f": rel}) + "\n")

    def test_a_batch_stranded_in_an_adopting_file_is_adopted_too(self):  # T-3277 review 2 LOW 1
        self._plant("adopting.4194999.1.x")  # its owner died between the rename and the unlink
        self.assertEqual(ev.take(self.project()), ["calc.py"])
        self.assertEqual([n for n in os.listdir(self._ambient_dir()) if n.startswith("adopting.")], [])

    def test_a_pid_a_new_process_reuses_does_not_hold_a_batch(self):  # T-3277 review 2 LOW 4
        ppid = os.getppid()  # alive, but not the process that took the batch
        self._plant(f"pending.{ppid}.1")
        self.assertEqual(ev.take(self.project()), ["calc.py"], "a mismatched start time reads as dead")
        ev.done(self.project())
        self._plant(f"pending.{ppid}.{ev._start(ppid)}", "notes.txt")
        self.assertEqual(ev.take(self.project()), [], "the same process still holds its batch")

    def test_take_and_release_leave_no_temp_file_and_keep_other_roots(self):  # T-3277 review 2 LOW 2, 3
        p = self.project()
        lane = os.path.join(self.tmp, "lane")
        other = c.Project(p.slug, lane, p.dir, lane)
        ev.queue(other, os.path.join(lane, "b.py"))
        for i in range(ev.BATCH_MAX + 1):
            ev.queue(p, os.path.join(p.root, f"q{i}.py"))
        self.assertEqual(len(ev.take(p)), ev.BATCH_MAX)
        ev.release(p)  # not ruled: the batch is queued again
        names = os.listdir(self._ambient_dir())
        self.assertEqual([n for n in names if n.startswith(("pending.", "adopting."))], [], names)
        self.assertEqual(len(ev.take(p)), ev.BATCH_MAX)
        ev.done(p)
        self.assertEqual(len(ev.take(p)), 1, "the one past the batch cap waits for the next run")
        ev.done(p)
        self.assertEqual(ev.take(p), [])
        self.assertEqual(ev.take(other), ["b.py"])

    def test_json_rows_carry_no_absolute_root(self):  # T-3277 review 2 LOW 5
        self.edit("notes.txt", "changed\n")
        out = self.fm("check", "ambient", "edits", "--json").stdout
        self.assertNotIn(self.project().root, out)
        self.assertTrue(all("root" not in r for r in json.loads(out)["edits"]))

    def test_a_run_rules_only_the_edits_of_its_own_root(self):  # T-3277 review 2
        p = self.project()
        lane = os.path.join(self.tmp, "lane")
        other = c.Project(p.slug, lane, p.dir, lane)
        ev.queue(p, os.path.join(p.root, "a.py"))
        ev.queue(other, os.path.join(other.root, "b.py"))
        self.assertEqual(ev.take(p), ["a.py"])
        ev.done(p)
        self.assertEqual(ev.take(p), [], "the other tree's edit is not this root's to rule")
        self.assertEqual(ev.take(other), ["b.py"], "it waits for a run in its own root")
        ev.done(other)
        ev.record(p, ["a.py"], 0, 0, {}, None)
        ev.record(other, ["b.py"], 0, 0, {}, None)
        self.assertEqual([r["file"] for r in ev.last(p)], ["a.py"])
        self.assertEqual([r["file"] for r in ev.last(other)], ["b.py"])
        self.assertEqual({r["root"] for r in self.rows()}, {p.root, other.root})

    def test_a_mixed_batch_says_which_file_links_a_test(self):  # T-3277 review 3
        env = {"FOREMAN_NO_BACKGROUND": "1"}
        self.write("calc.py", BAD)
        self.hook("PostToolUse", {"tool_name": "Edit", "tool_input": {"file_path": os.path.join(self.repo, "calc.py")},
                                  "tool_response": {}}, env=env)
        self.edit("notes.txt", "changed\n")  # the run: calc.py (linked) and notes.txt (not) in one batch, pass to red
        rows = {r["file"]: r for r in self.rows()}
        self.assertEqual((rows["calc.py"]["verdict"], rows["calc.py"]["linked"], rows["calc.py"]["batch"]),
                         ("harmful", True, 2))
        self.assertEqual((rows["notes.txt"]["verdict"], rows["notes.txt"]["linked"], rows["notes.txt"]["batch"]),
                         ("harmful", False, 2))
        out = self.fm("check", "ambient", "edits").stdout
        line = next(l for l in out.splitlines() if "notes.txt" in l)
        self.assertIn("harmful, one of 2 in the batch, not linked", line)
        self.assertIn("harmful, one of 2 in the batch, linked", next(l for l in out.splitlines() if "calc.py" in l))
        self.assertIn("after editing calc.py, notes.txt", self.note(), "the linked file is named first")

    def test_queue_rejects_a_carriage_return_and_resolves_dots(self):  # T-3277 review LOW
        p = self.project()
        ev.queue(p, os.path.join(p.root, "a\rb.py"))
        ev.queue(p, os.path.join(p.root, "a\nb.py"))
        ev.queue(p, os.path.join(p.root, "..", "outside.py"))
        ev.queue(p, os.path.join(p.root, "sub", "..", ".git", "config"))
        ev.queue(p, os.path.join(p.root, "sub", "..", "ok.py"))
        self.assertEqual(ev.take(p), ["ok.py"])

    def test_names_and_render_strip_control_text(self):  # T-3277 review LOW
        self.assertNotIn("\x1b", ev.names(["a\x1b[31m.py"]))
        row = {"file": "a\x1b[2J.py", "verdict": "keep", "ts": "t", "batch": 1, "linked": True}
        self.assertNotIn("\x1b", ev.render([row]))
