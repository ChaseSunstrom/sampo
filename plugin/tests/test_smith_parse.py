"""T-4426: sampo code never leaves a project worse than it found it without saying so. An edit or write that leaves a
Python, JSON, JS, shell or TOML file unparsable gets the parser's line and message in its own result; a done is refused
while such a file stands; a run that stops unfinished with one puts the files it changed back to the last step-end
snapshot (the start when none ended) and keeps the dropped difference as a patch. The fake model answers turn by turn."""
import glob
import json
import os
import shutil

from test_code import PLAN, CodeCase, call, journal_text, plan

TWO = dict(PLAN, steps=["write the helper", "fix calc.add"])
FIX = call("edit", file="calc.py", old="a - b", new="a + b")
DONE = call("done", summary="done")


def events(r):
    return [json.loads(l) for l in r.stdout.splitlines() if l.startswith("{")]


class Parse(CodeCase):
    def run_script(self, *script, extra=()):
        f = self.serve("openai", plan(PLAN), *script)
        r = self.code("fix add", "--model", "fake", "--json", *extra, check=False)
        return f, r

    def details(self, r):
        return [e["detail"] for e in events(r) if e["event"] == "action"]

    def test_parse_a_write_that_does_not_parse_gets_the_parser_message_per_kind(self):
        kinds = [("a.py", "def f(:\n    pass\n", "line 1"), ("a.json", '{"a": }\n', "line 1"),
                 ("a.sh", "if true\nfi\n", "line"), ("a.toml", "a = = 1\n", "line 1")]
        if shutil.which("node"):
            kinds.append(("a.js", "function (\n", "line 1"))
        _, r = self.run_script(*[call("write", file=n, content=c) for n, c, _ in kinds], extra=("--max-steps", "9"))
        got = self.details(r)
        for (name, _, line), detail in zip(kinds, got):
            self.assertIn(name, detail)
            self.assertIn("parse", detail, detail)
            self.assertIn(line, detail, detail)
        self.assertIn("SyntaxError", got[0])

    def test_parse_the_indentation_error_of_an_edit_names_its_line(self):
        _, r = self.run_script(call("edit", file="calc.py", old="    return a - b", new="    return a + b\n  x = 1"),
                               extra=("--max-steps", "3"))
        detail = self.details(r)[0]
        self.assertIn("no longer parses", detail)
        self.assertIn("IndentationError", detail)
        self.assertIn("line 3", detail)

    def test_parse_an_edit_that_keeps_it_parsable_returns_what_it_returned_before(self):
        _, r = self.run_script(call("write", file="ok.py", content="x = 1\n"),
                               call("edit", file="ok.py", old="x = 1", new="x = 2"), extra=("--max-steps", "3"))
        for detail in self.details(r):
            self.assertNotIn("parse", detail)

    def test_parse_a_file_that_did_not_parse_before_is_not_held_against_the_run(self):
        with open(os.path.join(self.repo, "old.py"), "w") as fh:
            fh.write("def broken(:\n")
        _, r = self.run_script(call("edit", file="old.py", old="broken(:", new="still_broken(:"),
                               call("write", file="old.py", content="def other(:\n"), extra=("--max-steps", "3"))
        for detail in self.details(r):
            self.assertNotIn("parse", detail)

    def test_parse_a_second_edit_that_leaves_it_broken_says_nothing_more(self):
        _, r = self.run_script(call("write", file="h.py", content="x = (\n"), call("write", file="h.py", content="y = (\n"),
                               extra=("--max-steps", "3"))
        got = self.details(r)
        self.assertIn("parse", got[0])
        self.assertNotIn("parse", got[1], "the model is mid-fix: no repeated warning")

    def test_parse_the_count_is_in_the_stats(self):
        _, r = self.run_script(call("write", file="h.py", content="x = (\n"), extra=("--max-steps", "2"))
        stats = [e for e in events(r) if e["event"] == "stats"][0]
        self.assertEqual(stats["parse_breaks"], 1)


class ParseDone(CodeCase):
    def test_done_is_refused_while_a_file_the_run_changed_does_not_parse_and_accepted_once_it_does(self):
        f = self.serve("openai", plan(PLAN), call("write", file="helper.py", content="x = (\n"), FIX, DONE,
                       call("write", file="helper.py", content="x = (1,)\n"), DONE)
        r = self.code("fix add", "--model", "fake", check=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        said = f.said(4)  # the turn after the refused done
        self.assertIn("helper.py", said)
        self.assertIn("line 2", said)
        self.assertIn("no longer parse", said)
        self.assertEqual(self.task()["status"], "done")
        with open(os.path.join(self.repo, "helper.py")) as fh:
            self.assertEqual(fh.read(), "x = (1,)\n")

    def test_done_stays_refused_while_the_file_does_not_parse_and_the_task_is_not_closed(self):
        # the fixing edit never comes: each done is refused (the script then runs dry, which stops the run)
        self.serve("openai", plan(PLAN), call("write", file="helper.py", content="x = (\n"), FIX, DONE, DONE)
        r = self.code("fix add", "--model", "fake", check=False)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(journal_text(self).count('"parse_refused"'), 2)
        self.assertNotEqual(self.task()["status"], "done")


class RestoreCase(CodeCase):
    def run_script(self, *script, steps=TWO, max_steps="4"):
        f = self.serve("openai", plan(steps), *script)
        r = self.code("fix add", "--model", "fake", "--json", "--max-steps", max_steps, check=False)
        return f, r

    def patches(self):
        return glob.glob(os.path.join(os.path.dirname(journal_path(self)), "T-0001.dropped*.patch"))

    def ended(self, r):
        return [e for e in events(r) if e["event"] in ("error", "blocked")][-1]

    def stats(self, r):
        return [e for e in events(r) if e["event"] == "stats"][-1]


class Restore(RestoreCase):
    def test_restore_a_turn_limit_stop_with_an_unparsable_file_goes_back_to_the_last_step_end(self):
        # turns: plan 1, write v1 2, done 3 (step 1 ends, snapshot), write broken 4, the cap at the 5th
        _, r = self.run_script(call("write", file="helper.py", content="V = 1\n"), DONE,
                               call("write", file="helper.py", content="V = (\n"))
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(open(os.path.join(self.repo, "helper.py")).read(), "V = 1\n")
        patch = self.patches()
        self.assertEqual(len(patch), 1)
        text = open(patch[0]).read()
        self.assertIn("+V = (", text)
        self.assertIn("-V = 1", text)
        for ev in (self.ended(r), self.stats(r)):
            self.assertEqual(ev["restored"], ["helper.py"])
            self.assertEqual(ev["patch"], patch[0])
        self.assertIn("helper.py", r.stderr)

    def test_restore_with_no_step_ended_the_files_go_back_to_the_start(self):
        _, r = self.run_script(call("edit", file="calc.py", old="a - b", new="a + b\n  x = 1"),
                               call("write", file="new.py", content="y = (\n"), max_steps="3")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("a - b", self.calc())
        self.assertFalse(os.path.exists(os.path.join(self.repo, "new.py")), "a file the run made is gone")
        self.assertEqual(sorted(self.ended(r)["restored"]), ["calc.py", "new.py"])
        self.assertEqual(len(self.patches()), 1)

    def test_restore_a_stop_with_everything_parsable_restores_nothing(self):
        _, r = self.run_script(call("write", file="helper.py", content="V = 1\n"), DONE,
                               call("write", file="helper.py", content="V = 2\n"))
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(open(os.path.join(self.repo, "helper.py")).read(), "V = 2\n")
        self.assertEqual(self.patches(), [])
        self.assertNotIn("restored", self.ended(r))
        self.assertEqual(self.stats(r)["restored"], [])

    def test_restore_a_file_the_run_did_not_change_is_left_alone(self):
        with open(os.path.join(self.repo, "mine.py"), "w") as fh:
            fh.write("keep = (\n")  # the user's own work in progress, broken before the run began
        _, r = self.run_script(call("write", file="helper.py", content="V = (\n"), max_steps="2")
        self.assertEqual(open(os.path.join(self.repo, "mine.py")).read(), "keep = (\n")
        self.assertEqual(self.ended(r)["restored"], ["helper.py"])

    def test_restore_a_run_that_finished_its_steps_restores_nothing(self):
        # the step ends by a green verify with an unrelated file left broken: the verify and the close decide, not a restore
        self.serve("openai", plan(PLAN), call("write", file="helper.py", content="V = (\n"), FIX,
                   call("run", cmd="python3 test_calc.py"))
        r = self.code("fix add", "--model", "fake", "--json", check=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.patches(), [])
        self.assertEqual(open(os.path.join(self.repo, "helper.py")).read(), "V = (\n")


class RestoreKeep(RestoreCase):
    """T-4445: a run that stops unfinished with the keep check red (the project's own tests, green before the work) puts
    its files back as it does for an unparsable file: on the hard exam a run out of turns left 7 of 7 keep checks
    broken, and one ended by a model error left 2."""
    repo_seed = ("seed with a green suite", (
        ("calc.py", "def add(a, b):\n    return a - b\n"),
        ("test_calc.py", "import unittest\nimport calc\n\n\nclass T(unittest.TestCase):\n    def test_zero(self):\n"
                         "        self.assertEqual(calc.add(1, 0), 1)\n")))
    P = dict(PLAN, criteria=[{"text": "calc.add adds", "verify": "python3 -c 'import calc; assert calc.add(2, 3) == 5'"}])

    def test_keep_red_at_a_stop_puts_the_files_back_and_keeps_the_patch(self):
        _, r = self.run_script(call("edit", file="calc.py", old="a - b", new="a * b"), steps=self.P, max_steps="2")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("a - b", self.calc(), "back to the start: no step had ended")
        patch = self.patches()
        self.assertEqual(len(patch), 1)
        self.assertIn("+    return a * b", open(patch[0]).read())
        for ev in (self.ended(r), self.stats(r)):
            self.assertEqual(ev["restored"], ["calc.py"])
        self.assertIn("keep check", self.ended(r)["text"])
        self.assertIn("keep check", r.stderr)

    def test_keep_red_is_not_claimed_when_the_check_is_green_at_the_stop(self):
        _, r = self.run_script(call("edit", file="calc.py", old="a - b", new="a + b"), steps=self.P, max_steps="2")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("a + b", self.calc(), "unfinished work that keeps the tests green stays")
        self.assertEqual(self.patches(), [])
        self.assertEqual(self.stats(r)["restored"], [])


def journal_path(case):
    return os.path.join(os.path.dirname(os.path.dirname(case.task("T-0001")["path"])), "code", "T-0001.jsonl")
