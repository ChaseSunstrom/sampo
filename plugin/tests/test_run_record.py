"""T-4268: a run of tests/run.py leaves one record in the repo's shared run-cache folder (what ran, on which tree,
which classes failed and each one's first failure line), `run.py --last` prints the newest one written from this
worktree without running anything or reading the tree (--same-tree: of this content, --any-tree: of the repo),
`sampo check` names the failed classes from the record stamped with its gate's own run id when its output lost them,
and the per-class times are one file for every worktree of the repo."""
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import run  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))
from sampo import core as c  # noqa: E402

NESTED = ("SAMPO_SLOT", "FOREMAN_SLOT", "SAMPO_SLOTS", "FOREMAN_SLOTS", "SAMPO_NO_TEST_CACHE", "FOREMAN_NO_TEST_CACHE",
          "SAMPO_CACHE_FULL", "FOREMAN_CACHE_FULL", "FM_TEST_TIMES", "FM_RUN_DEADLINE", "SAMPO_TEST_RUN_ID",
          "FOREMAN_TEST_RUN_ID")
LIB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib")
RED = ("import unittest\n\n\nclass Red(unittest.TestCase):\n    def test_bad(self):\n"
       "        with open({count!r}, 'a') as f:\n            f.write('ran\\n')\n"
       "        self.assertEqual(1, 2, 'one is not two')\n\n    def test_fine(self):\n        pass\n")
OK = "import unittest\n\n\nclass Ok(unittest.TestCase):\n    def test_a(self):\n        pass\n"
LEAK = ("import unittest\n\n\nclass Leak(unittest.TestCase):\n    def test_leak(self):\n"
        "        self.assertEqual(1, 2, 'key ' + 'gh' + 'p_' + 'A1' * 15)\n")
STATE = ("import os\nimport unittest\n\n\nclass State(unittest.TestCase):\n    def test_state(self):\n"
         "        root = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '.sampo')\n"
         "        os.makedirs(root, exist_ok=True)\n        with open(os.path.join(root, 'state.json'), 'w') as f:\n"
         "            f.write('rewritten')\n")
LEFT = ("import os\nimport unittest\n\n\nclass Left(unittest.TestCase):\n    def test_left(self):\n"
        "        root = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..')\n"
        "        with open(os.path.join(root, 'leftover.txt'), 'w') as f:\n            f.write('left behind')\n"
        "        self.fail('red, with a file left behind')\n")
SEEN = ("import os\nimport unittest\n\n\nclass Seen(unittest.TestCase):\n    def test_seen(self):\n"
        "        with open({out!r}, 'w') as f:\n            f.write(repr(os.environ.get('SAMPO_TEST_RUN_ID')))\n")
MANY = "import unittest\n\n\n" + "".join(f"class C{i}(unittest.TestCase):\n    def test_a(self):\n        pass\n\n\n"
                                         for i in range(60))


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
                          check=True, capture_output=True, text=True).stdout


class Fixture(unittest.TestCase):
    """A scratch git repo holding run.py, a red test module and a green one; its cache is its own folder."""

    def setUp(self):
        self.top = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.top, True)
        self.cache = os.path.join(self.top, "cache")
        self.count = os.path.join(self.top, "count")
        self.repo = os.path.join(self.top, "repo")
        self.tests = os.path.join(self.repo, "plugin", "tests")
        os.makedirs(self.tests)
        for name in ("run.py", "runcover.py"):
            shutil.copy(os.path.join(run.HERE, name), os.path.join(self.tests, name))
        os.symlink(LIB, os.path.join(self.repo, "plugin", "lib"))  # Sampo's redactor, as in a real checkout
        with open(os.path.join(self.repo, ".gitignore"), "w") as f:
            f.write("__pycache__/\n")
        for name, text in (("test_red.py", RED.format(count=self.count)), ("test_ok.py", OK)):
            with open(os.path.join(self.tests, name), "w") as f:
                f.write(text)
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "init")
        self.env = {k: v for k, v in os.environ.items() if k not in NESTED}
        # T-4442: these count runs: a red class is not rerun because this host happens to be loaded
        self.env.update(XDG_CACHE_HOME=self.cache, FOREMAN_SLOTS="0", FOREMAN_LOAD_RERUN="0")

    def go(self, *argv, repo=None, **env):
        repo = repo or self.repo
        return subprocess.run([sys.executable, os.path.join(repo, "plugin", "tests", "run.py"), *argv],
                              capture_output=True, text=True, timeout=300, env={**self.env, **env}, cwd=repo)

    def write(self, name, text):
        with open(os.path.join(self.tests, name), "w") as f:
            f.write(text)

    def rec(self, *argv, repo=None):
        got = self.go("--last", "--json", *argv, repo=repo)
        self.assertEqual(got.returncode, 0, got.stderr)
        return json.loads(got.stdout)

    def ran(self):
        try:
            with open(self.count) as f:
                return len(f.read().splitlines())
        except OSError:
            return 0

    def worktree(self, name="second"):
        path = os.path.join(self.top, name)
        git(self.repo, "worktree", "add", "-q", "-b", name, path)
        return path


class Last(Fixture):
    def test_last_prints_a_red_run_s_class_test_and_assertion_and_runs_nothing(self):
        r = self.go("test_red")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertEqual(self.ran(), 1)
        got = self.go("--last")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertRegex(got.stdout, r"(?m)^red at \d{4}-\d\d-\d\dT[\d:]+Z: test_red\.Red\.test_bad: "
                                     r"AssertionError: 1 != 2 : one is not two$")
        self.assertEqual(self.ran(), 1, "--last ran the tests again")

    def test_last_json_holds_the_counts_the_tree_and_the_traceback(self):
        self.go("test_red", "test_ok")
        rec = self.rec()
        self.assertFalse(rec["ok"])
        self.assertEqual(os.path.realpath(rec["worktree"]), self.repo)
        self.assertEqual(rec["argv"], ["test_red", "test_ok"])
        self.assertEqual((rec["ran"], rec["cached"]), (3, 0))
        (bad,) = rec["failed"]
        self.assertEqual((bad["class"], bad["test"]), ("test_red.Red", "test_bad"))
        self.assertIn("one is not two", bad["text"])
        self.assertIn("Traceback", bad["text"])
        self.assertLessEqual(len(bad["text"]), 2000)
        self.assertTrue(rec["tree"])

    def test_last_of_the_newest_run_wins(self):
        self.go("test_red")
        self.go("test_ok")
        out = self.go("--last").stdout
        self.assertRegex(out, r"^green at ")
        self.assertNotIn("red at", out)

    def test_last_shows_the_short_tree_id_of_the_record(self):  # finding 3b
        self.go("test_red")
        out = self.go("--last").stdout
        tree = self.rec()["tree"]
        self.assertRegex(out, rf"(?m)^tree {tree[:12]}\b")


class Selection(Fixture):
    def test_a_green_selection_says_so_and_a_whole_green_run_does_not(self):  # finding 5
        self.assertEqual(self.go("test_ok").returncode, 0)
        out = self.go("--last").stdout
        self.assertRegex(out, r"^green at \d{4}-\d\d-\d\dT[\d:]+Z \(1 tests; selection: test_ok\)")
        self.assertTrue(self.rec()["ok"])
        self.write("test_red.py", OK.replace("Ok", "Fine"))  # the whole suite green now
        self.assertEqual(self.go().returncode, 0)
        whole = self.go("--last").stdout
        self.assertRegex(whole, r"^green at \S+ \(2 tests\)")
        self.assertNotIn("selection", whole)
        self.assertTrue(self.rec()["whole"])

    def test_a_green_selection_never_hides_a_red_whole_suite_run(self):  # finding 5
        self.assertEqual(self.go().returncode, 1)
        self.assertEqual(self.go("test_ok").returncode, 0)
        lines = self.go("--last").stdout.splitlines()
        self.assertRegex(lines[0], r"^green at .*selection: test_ok")
        named = [x for x in lines[1:] if "whole-suite" in x]
        self.assertEqual(len(named), 1, lines)
        self.assertIn("test_red.Red.test_bad", named[0])

    def test_a_newer_green_whole_suite_run_clears_it(self):
        self.assertEqual(self.go().returncode, 1)
        self.write("test_red.py", OK.replace("Ok", "Fine"))
        self.assertEqual(self.go().returncode, 0)
        self.assertEqual(self.go("test_ok").returncode, 0)
        self.assertNotIn("whole-suite", self.go("--last").stdout)

    def test_a_k_selection_and_a_skip_list_are_selections_too(self):
        self.go("-k", "test_a")
        self.assertIn("selection: -k test_a", self.go("--last").stdout)
        self.go(FOREMAN_SKIP_TESTS="test_red")
        out = self.go("--last").stdout
        self.assertIn("selection:", out)
        self.assertIn("test_red", out)
        self.assertFalse(self.rec()["whole"])


class Scope(Fixture):
    def test_last_is_the_newest_of_this_worktree_without_reading_the_tree(self):  # finding 3b
        self.go("test_red")
        out = io.StringIO()
        with mock.patch.object(run, "HERE", self.tests), mock.patch.dict(os.environ, self.env), \
                mock.patch.object(run, "tree", side_effect=AssertionError("--last read the tree")), \
                contextlib.redirect_stdout(out):
            self.assertEqual(run.show_last(run.parse(["--last"])), 0)
        self.assertIn("test_red.Red.test_bad", out.getvalue())

    def test_an_edited_tree_still_finds_the_record_by_path_but_not_by_content(self):  # finding 3b
        self.go("test_red")
        with open(os.path.join(self.tests, "test_ok.py"), "a") as f:
            f.write("# edited after the run\n")
        self.assertIn("test_red.Red.test_bad", self.go("--last").stdout)
        none = self.go("--last", "--same-tree")
        self.assertEqual(none.returncode, 3, none.stdout + none.stderr)
        self.assertEqual(len((none.stdout + none.stderr).strip().splitlines()), 1)

    def test_another_worktree_has_no_record_of_its_own_but_the_same_content_finds_it(self):
        self.go("test_red")
        same, other = self.worktree("same"), self.worktree("other")
        with open(os.path.join(other, "plugin", "tests", "test_ok.py"), "a") as f:
            f.write("# other content\n")
        none = self.go("--last", repo=same)
        self.assertEqual(none.returncode, 3, none.stdout + none.stderr)  # a record of the main worktree, not this one
        found = self.go("--last", "--same-tree", repo=same)
        self.assertEqual(found.returncode, 0, found.stderr)
        self.assertIn("test_red.Red.test_bad", found.stdout)
        self.assertEqual(self.go("--last", "--same-tree", repo=other).returncode, 3)
        any_tree = self.go("--last", "--any-tree", repo=other)
        self.assertEqual(any_tree.returncode, 0)
        self.assertIn("test_red.Red.test_bad", any_tree.stdout)

    def test_a_run_that_never_read_the_tree_stores_the_path_and_no_content_id(self):  # finding 3c
        self.go("-k", "test_a")  # -k: no cache, so no tree read
        rec = self.rec()
        self.assertFalse(rec["tree"])
        self.assertEqual(os.path.realpath(rec["worktree"]), self.repo)
        self.assertIn("green at", self.go("--last").stdout)
        self.assertEqual(self.go("--last", "--same-tree").returncode, 3)

    def test_a_sampo_file_rewritten_by_the_run_does_not_hide_its_record(self):  # finding 2
        self.write("test_state.py", STATE)
        self.assertEqual(self.go("test_state").returncode, 0)
        self.assertTrue(os.path.exists(os.path.join(self.repo, ".sampo", "state.json")))
        found = self.go("--last", "--same-tree")
        self.assertEqual(found.returncode, 0, found.stdout + found.stderr)

    def test_a_file_a_test_leaves_behind_still_matches_by_the_end_of_run_id(self):  # finding 2
        self.write("test_left.py", LEFT)
        self.assertEqual(self.go("test_left").returncode, 1)
        rec = self.rec()
        self.assertTrue(rec["tree"] and rec["tree_end"] and rec["tree"] != rec["tree_end"])
        found = self.go("--last", "--same-tree")
        self.assertEqual(found.returncode, 0, found.stdout + found.stderr)
        self.assertIn("test_left.Left", found.stdout)

    def test_tree_id_leaves_sampo_s_state_out(self):  # finding 2
        base = {"a.py": "100644 aa", "\0git": "x"}
        self.assertEqual(run.tree_id({**base, ".sampo/t.json": "100644 11"}),
                         run.tree_id({**base, ".sampo/t.json": "100644 22", ".sampo/new": "100644 33"}))
        self.assertEqual(run.tree_id(base), run.tree_id({**base, ".sampo/t.json": "100644 11"}))
        self.assertNotEqual(run.tree_id(base), run.tree_id({**base, "b.py": "100644 11"}))

    def test_other_tree_content_lookup_in_the_unit(self):
        common = os.path.join(self.repo, ".git")
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": self.cache}):
            run.record_run(common, {"tree": "start", "tree_end": "end", "worktree": "/w", "t": 1})
            self.assertEqual(run.last_record(common, tree="end")["tree"], "start")
            self.assertEqual(run.last_record(common, tree="start")["tree_end"], "end")
            self.assertIsNone(run.last_record(common, tree="other"))
            self.assertIsNone(run.last_record(common, worktree="/elsewhere"))


class RunId(Fixture):
    CMD = "python3 plugin/tests/run.py test_red"

    def test_a_record_carries_the_run_id_of_its_gate(self):  # finding 1
        self.go("test_red", SAMPO_TEST_RUN_ID="gate-a")
        self.go("test_ok", SAMPO_TEST_RUN_ID="gate-b")
        with mock.patch.dict(os.environ, self.env):
            lines = c.record_lines(self.repo, self.CMD, "gate-a")
            self.assertEqual(len(lines), 1, lines)
            self.assertRegex(lines[0], r"red at .*Z: test_red\.Red\.test_bad: AssertionError: 1 != 2")
            self.assertEqual(c.record_lines(self.repo, self.CMD, "gate-b"), [], "its own record is green")
            self.assertEqual(c.record_lines(self.repo, self.CMD, None), [], "no id: nothing, never the newest's")
            self.assertEqual(c.record_lines(self.repo, self.CMD, "gate-c"), [], "an id no run wrote")
            self.assertEqual(c.record_lines(self.repo, "python3 -m pytest -q", "gate-a"), [], "another runner's check")

    def test_another_gate_s_newer_red_record_is_never_printed(self):  # finding 1
        self.go("test_ok", SAMPO_TEST_RUN_ID="mine")
        self.go("test_red", SAMPO_TEST_RUN_ID="theirs")  # newer, red, a different gate's
        with mock.patch.dict(os.environ, self.env):
            self.assertEqual(c.record_lines(self.repo, self.CMD, "mine"), [])

    def test_run_command_stamps_the_id_and_the_run_pops_it(self):  # finding 1
        code, out = c.run_command(self.repo, "echo id=$SAMPO_TEST_RUN_ID", timeout=30, run_id="abc123")
        self.assertIn("id=abc123", out)
        self.write("test_seen.py", SEEN.format(out=os.path.join(self.top, "seen")))
        self.assertEqual(self.go("test_seen", SAMPO_TEST_RUN_ID="abc123").returncode, 0)
        with open(os.path.join(self.top, "seen")) as f:
            self.assertEqual(f.read(), "None", "a nested run in a class would inherit the id")
        self.assertEqual(self.rec()["run_id"], "abc123")

    def test_the_run_id_is_an_operator_switch(self):
        self.assertIn("TEST_RUN_ID", run.OPERATOR_SWITCHES)
        with mock.patch.dict(os.environ, {"SAMPO_TEST_RUN_ID": "x", "FOREMAN_TEST_RUN_ID": "x"}):
            kid = run.child_env("/t")
        self.assertNotIn("SAMPO_TEST_RUN_ID", kid)
        self.assertNotIn("FOREMAN_TEST_RUN_ID", kid)

    def test_new_run_id_only_for_a_gate_that_runs_tests_run_py(self):  # finding 9
        for cmd in ("python3 plugin/tests/run.py", "python3 plugin/tests/run.py test_cli -k x", "python3 tests/run.py"):
            self.assertTrue(c.new_run_id(cmd), cmd)
        for cmd in ("python3 -m pytest plugin/tests/test_run.py", "pytest tests/test_run.py", "pytest -q",
                    "python3 plugin/tests/run.pyc", "", None):
            self.assertIsNone(c.new_run_id(cmd), cmd)
        with mock.patch.dict(os.environ, self.env):
            self.assertEqual(c.record_lines(self.repo, "pytest plugin/tests/test_run.py", "any"), [])
        self.assertNotEqual(c.new_run_id("tests/run.py"), c.new_run_id("tests/run.py"))

    def test_the_named_classes_come_from_the_record_only_when_the_output_has_none(self):  # finding 3a
        from sampo import cli
        with mock.patch.object(cli, "_record_lines", return_value=["    red at T: x"]) as rl:
            self.assertEqual(cli._named("/r", "tests/run.py", "id", 1, "boom\n"), ["    red at T: x"])
            rl.assert_called_once_with("/r", "tests/run.py", "id")
            rl.reset_mock()
            self.assertEqual(cli._named("/r", "tests/run.py", "id", 1, "\n  failed class: test_x.A\n"), [])
            self.assertEqual(cli._named("/r", "tests/run.py", "id", 0, "boom\n"), [])
            rl.assert_not_called()

    def test_check_names_are_indented_and_cut_for_the_printed_gate(self):
        from sampo import cli
        many = [f"red at T: test_x.C.test_{i}: AssertionError" for i in range(20)]
        with mock.patch.object(c, "record_lines", return_value=many):
            got = cli._record_lines("/r", "tests/run.py", "id", n=12)
        self.assertEqual(len(got), 13)
        self.assertTrue(all(x.startswith("    ") for x in got))
        self.assertIn("8 more", got[-1])


class Robust(Fixture):
    def main(self, modules, fake, argv, **patches):
        """run.main() in this process on the fixture's repo, run_one faked; (exit code, stderr)."""
        for name, text in modules.items():
            self.write(name + ".py", text)
        before = set(sys.modules)
        self.addCleanup(lambda: [sys.modules.pop(m) for m in set(sys.modules) - before if m.startswith("test_")])
        err = io.StringIO()
        env = {"FOREMAN_FORK_CLI": "0", "XDG_CACHE_HOME": self.cache, "FOREMAN_NO_TEST_CACHE": "1",
               "FM_TEST_TIMES": os.path.join(self.top, "times.json")}
        with contextlib.ExitStack() as st:
            st.enter_context(mock.patch.dict(os.environ, env))
            for k in NESTED:
                if k not in env:
                    os.environ.pop(k, None)
            for k, v in {"HERE": self.tests, **patches}.items():
                st.enter_context(mock.patch.object(run, k, v))
            st.enter_context(mock.patch.object(run, "skipped", return_value=set()))
            st.enter_context(mock.patch.object(run, "run_one", side_effect=fake))
            st.enter_context(mock.patch.object(sys, "argv", ["run.py", "-j", "1", *argv]))
            st.enter_context(contextlib.redirect_stdout(io.StringIO()))
            st.enter_context(contextlib.redirect_stderr(err))
            code = run.main()
        return code, err.getvalue()

    def test_a_record_that_cannot_be_written_never_changes_the_run_s_result(self):  # finding 4
        ok = lambda cid, k, *rest: (cid,0, "", 1, 0.1)  # noqa: E731
        with mock.patch.object(run, "record_run", side_effect=RuntimeError("boom")):
            code, err = self.main({"test_boom": OK.replace("Ok", "Boom")}, ok, ["test_boom"])
        self.assertEqual(code, 0, err)
        self.assertRegex(err, r"(?m)^Ran 1 tests in [\d.]+s .*\n\nOK\n")
        said = [x for x in err.splitlines() if "record" in x and "boom" in x]
        self.assertEqual(len(said), 1, err)

    def test_a_record_block_that_raises_before_writing_is_guarded_too(self):  # finding 4
        ok = lambda cid, k, *rest: (cid,1, "FAIL: test_a (x.Y)\n" + "-" * 70 + "\nAssertionError: no\n", 1, 0.1)  # noqa: E731
        with mock.patch.object(run, "fail_entries", side_effect=ValueError("bad entries")):
            code, err = self.main({"test_boom": OK.replace("Ok", "Boom")}, ok, ["test_boom"])
        self.assertEqual(code, 1, err)
        self.assertIn("FAILED (1 classes: test_boom.Boom)", err)
        self.assertEqual(len([x for x in err.splitlines() if "record" in x and "bad entries" in x]), 1, err)

    def test_not_run_keeps_its_true_count_beside_the_capped_list(self):  # finding 7
        cut = lambda cid, k, *rest: (cid,1, run.NOT_STARTED, 0, 0.0)  # noqa: E731
        code, err = self.main({"test_many": MANY}, cut, ["test_many"])
        self.assertEqual(code, 124, err)
        rec = self.rec()
        self.assertEqual(rec["n_not_run"], 60)
        self.assertEqual(len(rec["not_run"]), 50)
        self.assertIn("60 classes not run", self.go("--last").stdout)
        self.assertEqual(rec["lines"], run.record_lines(rec))

    def test_a_subprocess_with_a_slot_marker_writes_no_record(self):
        self.go("test_ok", SAMPO_SLOT="2")
        self.assertEqual(self.go("--last").returncode, 3)


class Entries(unittest.TestCase):
    ERR = "FAIL: test_bad (test_x.Big.test_bad)\n" + "-" * 70 + "\nTraceback (most recent call last):\nAssertionError: x\n"

    def test_a_split_class_names_its_class_once(self):  # finding 6
        results = [("test_x.Big.test_bad", 1, self.ERR, 1, 0.1)]
        entries = run.fail_entries(results, lambda j: "test_x.Big" if j == "test_x.Big.test_bad" else j, [])
        self.assertEqual((entries[0]["class"], entries[0]["test"]), ("test_x.Big", "test_bad"))
        line = run.record_lines({"ok": False, "t": 1, "failed": entries})[0]
        self.assertTrue(line.endswith("test_x.Big.test_bad: AssertionError: x"), line)
        self.assertNotIn("test_bad.test_bad", line)

    def test_a_cpu_red_class_is_an_entry_without_a_test(self):
        (e,) = run.fail_entries([], lambda j: j, [("test_y.Slow", 31.0, 10.0, "6x its usual")])
        self.assertEqual((e["class"], e["test"], e["text"]), ("test_y.Slow", "", ""))
        self.assertIn("CPU over its margin", e["first"])

    def test_no_failure_text_but_the_names_when_the_redactor_is_missing(self):  # finding 8
        raw = [{"class": "a.B", "test": "t", "first": "AssertionError: key", "text": "Traceback ... key"}]
        self.assertEqual(run.safe_entries(raw, None), [{"class": "a.B", "test": "t", "first": "", "text": ""}])
        got = run.safe_entries(raw, lambda s: s.replace("key", "[R]"))
        self.assertEqual((got[0]["first"], got[0]["text"]), ("AssertionError: [R]", "Traceback ... [R]"))
        self.assertTrue(run.record_lines({"ok": False, "t": 1, "failed": run.safe_entries(raw, None)})[0].endswith("a.B.t"))

    def test_a_record_line_without_a_first_line_has_no_dangling_colon(self):
        line = run.record_lines({"ok": False, "t": 1, "failed": [{"class": "a.B", "test": "t", "first": ""}]})[0]
        self.assertTrue(line.endswith("a.B.t"), line)


class Redaction(Fixture):
    def test_a_secret_in_a_failure_is_redacted_before_it_is_written(self):  # finding 8
        self.write("test_leak.py", LEAK)
        self.assertEqual(self.go("test_leak").returncode, 1)
        got = self.go("--last", "--json").stdout
        self.assertNotIn("ghp_", got)
        self.assertNotIn("A1A1A1", got)
        self.assertIn("[REDACTED]", got)
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": self.cache}):
            folder = run.runs_dir(os.path.join(self.repo, ".git"))
            self.assertTrue(os.listdir(folder))
            for n in os.listdir(folder):
                with open(os.path.join(folder, n)) as f:
                    self.assertNotIn("A1A1A1", f.read())

    def test_a_lone_copy_of_run_py_stores_names_only(self):  # finding 8
        self.write("test_leak.py", LEAK)
        os.unlink(os.path.join(self.repo, "plugin", "lib"))  # no Sampo beside it: nothing to redact with
        self.assertEqual(self.go("test_leak").returncode, 1)
        rec = self.rec()
        (bad,) = rec["failed"]
        self.assertEqual((bad["class"], bad["test"], bad["first"], bad["text"]), ("test_leak.Leak", "test_leak", "", ""))
        self.assertNotIn("ghp_", json.dumps(rec))
        self.assertTrue(rec["lines"][0].endswith("test_leak.Leak.test_leak"), rec["lines"])


class Records(Fixture):
    def folder(self):
        return run.runs_dir(os.path.join(self.repo, ".git"))

    def test_cap_keeps_the_newest_200_records(self):
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": self.cache}):
            common = os.path.join(self.repo, ".git")
            for i in range(201):
                run.record_run(common, {"tree": "t", "n": i, "t": 1000 + i})
            names = sorted(os.listdir(self.folder()))
            self.assertEqual(len(names), 200)
            self.assertEqual(run.last_record(common, tree="t")["n"], 200)
            self.assertEqual(json.load(open(os.path.join(self.folder(), names[0])))["n"], 1, "the oldest went")

    def test_the_newest_whole_suite_record_of_each_worktree_outlives_the_cap(self):  # finding 12
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": self.cache}):
            common = os.path.join(self.repo, ".git")
            run.record_run(common, {"worktree": "/w1", "whole": True, "ok": False, "n": "w1-old", "t": 1})
            run.record_run(common, {"worktree": "/w1", "whole": True, "ok": False, "n": "w1-new", "t": 2})
            run.record_run(common, {"worktree": "/w2", "whole": True, "ok": False, "n": "w2", "t": 3})
            for i in range(210):  # many -k runs
                run.record_run(common, {"worktree": "/w1", "whole": False, "ok": True, "n": i, "t": 10 + i})
            seen = {}
            for n in os.listdir(self.folder()):
                with open(os.path.join(self.folder(), n)) as f:
                    r = json.load(f)
                seen[r["n"]] = r
            self.assertIn("w1-new", seen)
            self.assertIn("w2", seen)
            self.assertNotIn("w1-old", seen, "only the newest whole-suite record of a worktree is kept past the cap")
            self.assertEqual(len(seen), 202)
            got = run.last_record(common, worktree="/w1", whole=True)
            self.assertEqual(got["n"], "w1-new")

    def test_stale_temp_files_of_a_crashed_write_go_and_nothing_else_is_touched(self):  # finding 13
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": self.cache}):
            common = os.path.join(self.repo, ".git")
            run.record_run(common, {"n": 0, "t": 1})
            f = self.folder()
            old, fresh = "00000000000000000001-111.json.111", "00000000000000000002-222.json.222"
            keep = ("notes.txt", "x.events.jsonl", "00000000000000000003-333.json.tmp")
            for n in (old, fresh, *keep):
                with open(os.path.join(f, n), "w") as fh:
                    fh.write("x")
            os.makedirs(os.path.join(f, "running"))
            for n in (old, *keep):
                os.utime(os.path.join(f, n), (time.time() - 7200, time.time() - 7200))
            os.utime(os.path.join(f, "running"), (time.time() - 7200, time.time() - 7200))
            run.record_run(common, {"n": 1, "t": 2})
            left = set(os.listdir(f))
            self.assertNotIn(old, left)
            self.assertIn(fresh, left)
            self.assertTrue({*keep, "running"} <= left, left)


class TimesShared(Fixture):
    def test_times_shared_by_every_worktree_of_the_repo(self):
        self.go("test_red", "test_ok")
        other = self.worktree()
        with mock.patch.object(run, "HERE", os.path.join(other, "plugin", "tests")), \
                mock.patch.dict(os.environ, self.env):
            self.assertIn("test_ok.Ok", run.load_times())
            self.assertTrue(run.times_path().startswith(os.path.join(self.cache, "sampo", "run-cache", "")))
        first = run.hashlib.sha1(os.fsencode(os.path.join(self.repo, ".git"))).hexdigest()[:16]
        self.assertTrue(os.path.exists(os.path.join(self.cache, "sampo", "run-cache", first + ".times.json")))

    def test_times_shared_reads_the_old_temp_file_once_and_never_writes_it(self):
        legacy = os.path.join(self.top, "old-times.json")
        with open(legacy, "w") as f:
            json.dump({"test_old.Slow": 12.5}, f)
        with mock.patch.object(run, "HERE", self.tests), mock.patch.dict(os.environ, self.env), \
                mock.patch.object(run, "legacy_times_path", lambda: legacy):
            self.assertEqual(run.load_times(), {"test_old.Slow": 12.5})
            before = os.stat(legacy).st_mtime_ns
            run.save_times({"test_new.Fast": 1.0})
            self.assertEqual(run.load_times(), {"test_old.Slow": 12.5, "test_new.Fast": 1.0})
            self.assertTrue(os.path.exists(run.times_path()))
            with open(legacy) as f:
                self.assertEqual(json.load(f), {"test_old.Slow": 12.5})
            self.assertEqual(os.stat(legacy).st_mtime_ns, before)
            os.unlink(run.times_path())  # the shared file gone again: the old one is read again, not copied over
            self.assertEqual(run.load_times(), {"test_old.Slow": 12.5})

    def test_save_times_holds_the_cache_lock_over_its_read_merge_write(self):  # finding 11
        held, seen = [], []
        real = run._cache_lock

        @contextlib.contextmanager
        def spy(path):
            held.append(path)
            with real(path):
                seen.append("in")
                yield
            seen.append("out")
        with mock.patch.object(run, "HERE", self.tests), mock.patch.dict(os.environ, self.env), \
                mock.patch.object(run, "_cache_lock", spy):
            run.save_times({"a.B": 1.0})
            self.assertEqual(held, [run.times_path()])
        self.assertEqual(seen, ["in", "out"])

    def test_parallel_saves_lose_no_update(self):  # finding 11
        code = ("import sys\nsys.path.insert(0, %r)\nimport run\nrun.save_times({sys.argv[1]: 1.0})\n" % run.HERE)
        env = {**self.env, "FM_TEST_TIMES": os.path.join(self.top, "shared-times.json")}
        procs = [subprocess.Popen([sys.executable, "-c", code, f"a.C{i}"], env=env, cwd=self.top) for i in range(6)]
        for p in procs:
            self.assertEqual(p.wait(timeout=120), 0)
        with mock.patch.dict(os.environ, env):
            self.assertEqual(sorted(run.load_times()), [f"a.C{i}" for i in range(6)])


if __name__ == "__main__":
    unittest.main()
