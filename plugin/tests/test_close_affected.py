"""T-3526: a close whose linked tests are a gate's runner plus module names, after the gates' full pass on the same
code tree (a merge train's), runs nothing: the suite already ran them. No pass, or another runner: they run once."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import unittest
from unittest import mock

from helpers import ForemanTestCase

from sampo import cli
from sampo import core as c

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = "def add(a, b):\n    return a + b\n"


class CloseAffectedReuse(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.count = os.path.join(self.tmp, "count")  # outside the repo: a run must not change the tree it's judged on
        os.makedirs(os.path.join(self.repo, "tests"))
        shutil.copy(os.path.join(HERE, "run.py"), os.path.join(self.repo, "tests", "run.py"))
        self.write("calc.py", SRC)
        self.write("tests/test_calc.py",
                   "import os, sys, unittest\nsys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))\n"
                   "import calc\n\n\nclass Calc(unittest.TestCase):\n    def test_add(self):\n"
                   f"        open({self.count!r}, 'a').write('x\\n')\n        self.assertEqual(calc.add(1, 2), 3)\n")
        self.write(".gitignore", "__pycache__/\n")
        subprocess.run(["git", "-C", self.repo, "add", "-A"], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", "suite"], check=True)
        self.fm("check", "add", "python3 tests/run.py")
        self.fm("check", "affected", "python3 tests/run.py {names}")
        self.p = c.find_project(self.repo)

    def write(self, rel, text):
        with open(os.path.join(self.repo, rel), "w") as f:
            f.write(text)

    def runs(self):
        try:
            with open(self.count) as f:
                return len(f.read().split())
        except OSError:
            return 0

    def close(self):
        tid = self.fm_json("task", "new", "Change add", "--type", "FEATURE", "--tier", "S", "--ac", "works :: true",
                           "--step", "s", "--scope", "calc.py")["id"]
        self.fm("focus", tid)
        self.write("calc.py", SRC + "# tidy\n")
        return tid

    def finish(self, tid):
        r = self.fm("task", "finish", tid, "--audit", "self", "--run", "true", check=False,
                    env={"FOREMAN_SENTINEL": "off"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout, c.ledger_events(self.p, ("close_affected",))[-1]["data"]

    def test_a_green_full_pass_on_the_same_tree_covers_the_linked_tests(self):
        tid = self.close()
        self.fm("check", env={"FOREMAN_SENTINEL": "off"})  # the train's full pass, on the merged code
        before = self.runs()
        out, ev = self.finish(tid)
        self.assertEqual(self.runs(), before, "the linked tests ran again on code the gates just passed")
        self.assertIn("linked tests: covered by the gates' pass (run ", out)
        self.assertEqual(ev["exit"], 0)
        self.assertIn("code_tree", ev["reused"])

    def test_no_pass_runs_them_once(self):
        out, ev = self.finish(self.close())
        self.assertEqual(self.runs(), 1)
        self.assertNotIn("reused", ev)
        self.assertNotIn("covered by the gates", out)

    def test_a_missed_reuse_names_the_files_that_differ(self):
        # T-3548: an untracked scripts/ kept every close off the train's pass, and each reran its tests silently
        tid = self.close()
        self.fm("check", env={"FOREMAN_SENTINEL": "off"})
        self.write("calc.py", SRC + "# tidy\n# and again, after the pass\n")  # T-4822: a file the linked test reads
        out, ev = self.finish(tid)
        self.assertEqual(self.runs(), 2, "the code the test reads moved: the linked tests run")
        self.assertIn("linked tests: no reusable gate pass: the newest full gate run (", out)
        self.assertIn("was on another code tree: 1 file(s) differ: calc.py", out)
        self.assertIn("calc.py", ev["miss"])

    def test_a_stray_file_no_test_links_leaves_the_pass_standing(self):
        # T-4822: it differs from the pass's tree, but what could it break? The close judges it as it judges the task's
        # own change: by the tests linked to it, here none
        tid = self.close()
        self.fm("check", env={"FOREMAN_SENTINEL": "off"})
        os.makedirs(os.path.join(self.repo, "scripts"))
        self.write("scripts/stray.py", "x = 1\n")
        self.fm("task", "log", tid, "scope: a stray untracked file, not this task's")
        out, ev = self.finish(tid)
        self.assertEqual(self.runs(), 1, "nothing depends on the stray file: the pass's run stands")
        self.assertIn("covered by the gates' pass", out)
        self.assertEqual(ev["residual"]["ran"], [])

    def test_no_pass_at_all_says_so(self):
        out, _ = self.finish(self.close())
        self.assertIn("linked tests: no reusable gate pass: no full gate run is recorded", out)

    def again(self, rows=lambda r: r, **data):
        """The last check_run logged again as a newer run, each result row through `rows`, fields replaced by `data`."""
        d = c.ledger_events(self.p, ("check_run",))[-1]["data"]
        c.log_event(self.p, "check_run", data=dict(d, results=[rows(r) for r in d["results"]], **data))

    def ui_close(self, gated):
        """A task that changed calc.py and its .tsx test (another runner's) after another close committed a mirror .md
        file, then a train's pass on that code that ran test_calc only. `gated`: a gate runs the .tsx runner."""
        os.makedirs(os.path.join(self.repo, "ui"))
        os.makedirs(os.path.join(self.repo, c.MIRROR), exist_ok=True)
        self.write("ui/calc.test.tsx", "test('adds', () => {})\n")
        self.write(f"{c.MIRROR}/T-0099.md", "# another task\n")
        self.write("tests/test_mirror.py", "import unittest\n\n\nclass Mirror(unittest.TestCase):\n"
                   f"    def test_dir(self):\n        self.assertTrue({c.MIRROR!r})\n")  # links the mirror folder
        subprocess.run(["git", "-C", self.repo, "add", "-A"], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", "ui and mirror"], check=True)
        if gated:  # spelled as this repo's are: the gate `claude plugin test mods/sampo-ui`, the runner `cd … && … .`
            self.fm("check", "add", f"echo ui >> {self.count}")
        self.fm("check", "affected", "--for", ".tsx", "--literal", f"cd ui && echo . >> {self.count}")
        tid = self.close()
        self.write("ui/calc.test.tsx", "test('adds two', () => {})\n")
        self.fm("task", "log", tid, "scope: ui/calc.test.tsx, the test of what it changed")
        self.write(f"{c.MIRROR}/T-0099.md", "# another task, closed\n")  # another close's commit
        subprocess.run(["git", "-C", self.repo, "commit", "-qam", "T-0099 closed"], check=True)
        self.fm("check", env={"FOREMAN_SENTINEL": "off"})
        self.again(lambda r: dict(r, affected=["test_calc"]) if "run.py" in r["cmd"] else r, train=[tid])  # its part
        return tid

    def test_a_train_pass_covers_another_runners_tests_and_the_mirrors_noise(self):
        # T-3566: a train's pass reused 0 of 6 closes: each close command carried the .tsx runner's part, and the
        # mirror .md files other closes had committed linked tests the train never selected
        tid = self.ui_close(gated=True)
        before = self.runs()
        out, ev = self.finish(tid)
        self.assertEqual(self.runs(), before, "a test the train's selection covers ran again")
        self.assertIn("the suite on 1 affected module(s)", ev.get("reused", ""), out)

    def test_a_whole_suite_gates_test_the_difference_reaches_still_runs(self):
        # T-4822 review: a test whose extension a gate runs in full is no module name, so the residual never asked
        # whether the files differing since the pass reach it: it must run again, the one module the pass held must not
        tid = self.ui_close(gated=True)
        self.write("ui/calc.test.tsx", "test('adds three', () => {})\n")  # after the train's pass
        self.fm("task", "log", tid, "scope: ui/calc.test.tsx, the test of what it changed, edited after the pass")
        before = self.runs()
        out, ev = self.finish(tid)
        self.assertEqual(self.runs(), before + 1, "only the .tsx runner: test_calc's pass stands: " + out)
        self.assertIn("ui/calc.test.tsx", ev["residual"]["ran"])
        self.assertNotIn("test_calc", ev["residual"]["ran"])

    def test_another_runners_tests_no_gate_runs_are_run(self):
        # T-3566 review: with no gate for the .tsx runner no pass ran those tests, so the close runs them
        tid = self.ui_close(gated=False)
        before = self.runs()
        out, ev = self.finish(tid)
        self.assertEqual(self.runs(), before + 2, out)  # test_calc and the .tsx runner
        self.assertIn("linked tests: no reusable gate pass: no gate runs the .tsx tests", out)
        self.assertNotIn("reused", ev)

    def test_a_runner_is_the_gate_it_spells_differently(self):
        os.makedirs(os.path.join(self.repo, "mods", "ui"))
        self.write("tool", "#!/bin/sh\n")
        os.chmod(os.path.join(self.repo, "tool"), 0o755)
        gates = ["./tool mods/ui", "./tool"]
        self.assertEqual(cli.suite_gate(self.repo, "cd mods/ui && ../../tool .", gates), "./tool mods/ui")
        self.assertEqual(cli.suite_gate(self.repo, os.path.join(self.repo, "tool") + " {names}", gates), "./tool")
        self.assertIsNone(cli.suite_gate(self.repo, "cd mods/ui && ../../tool", gates), "another directory's suite")
        self.assertIsNone(cli.suite_gate(self.repo, "./tool mods/ui && ./tool", gates), "two commands")

    def test_a_newer_run_on_other_code_leaves_the_pass_on_this_code_standing(self):
        # T-3566: a lane's sentinel run logged after the train's hid its pass from five closes
        tid = self.close()
        self.fm("check", env={"FOREMAN_SENTINEL": "off"})
        self.again(tree="0" * 12, code_tree="0" * 40)
        before = self.runs()
        out, ev = self.finish(tid)
        self.assertEqual(self.runs(), before, out)
        self.assertIn("covered by the gates' pass", out)

    def test_a_newer_red_run_on_this_code_still_misses(self):
        self.fm("check", env={"FOREMAN_SENTINEL": "off"})
        self.again(lambda r: dict(r, exit=1))
        self.again(tree="0" * 12, code_tree="0" * 40)  # and a newer one on other code: skipped, the red one decides
        self.assertNotIn("cached", self.fm("check", env={"FOREMAN_SENTINEL": "off"}).stdout)

    def test_another_runner_runs_them_once_despite_the_pass(self):
        self.fm("check", "affected", "python3 -B tests/run.py {names}")
        tid = self.close()
        self.fm("check", env={"FOREMAN_SENTINEL": "off"})
        before = self.runs()
        out, ev = self.finish(tid)
        self.assertEqual(self.runs(), before + 1)
        self.assertNotIn("reused", ev)


class CloseSized(ForemanTestCase):
    """T-4832: the close sizes its linked-test run from the recorded seconds, as ship's affected stage does: a set over
    the ceiling is refused at once (no test runs), one under it runs with the sized limit. The layout is Sampo's own
    (plugin/tests/run.py with the suite beside it), so the record is read the way it is for this repo."""
    def setUp(self):
        super().setUp()
        self.fm("init")
        tests = os.path.join(self.repo, "plugin", "tests")
        os.makedirs(tests)
        shutil.copy(os.path.join(HERE, "run.py"), os.path.join(tests, "run.py"))
        self.count = os.path.join(self.tmp, "count")  # outside the repo: a run must not change the tree it's judged on
        self.times = os.path.join(self.tmp, "times.json")  # the run record the close reads (FM_TEST_TIMES)
        self.write("calc.py", SRC)
        self.write("plugin/tests/test_calc.py",
                   "import os, sys, unittest\nsys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname("
                   "os.path.abspath(__file__)))))\nimport calc\n\n\nclass Calc(unittest.TestCase):\n    def test_add(self):\n"
                   f"        open({self.count!r}, 'a').write('x\\n')\n        self.assertEqual(calc.add(1, 2), 3)\n")
        for i in range(4):  # the rest of the suite: linked to nothing the close changes, so never run here
            self.write(f"plugin/tests/test_other{i}.py", "import unittest\n\n\nclass Other(unittest.TestCase):\n"
                       "    def test_it(self):\n        pass\n")
        self.write(".gitignore", "__pycache__/\n")
        subprocess.run(["git", "-C", self.repo, "add", "-A"], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", "suite"], check=True)
        self.fm("check", "add", "python3 plugin/tests/run.py")
        self.fm("check", "affected", "python3 plugin/tests/run.py {names}")
        self.p = c.find_project(self.repo)

    def write(self, rel, text):
        with open(os.path.join(self.repo, rel), "w") as f:
            f.write(text)

    def record(self, calc, others, cpus=8):
        """The run record: test_calc.Calc took `calc` s, each other module `others` s; the host has `cpus` CPUs."""
        d = {"test_calc.Calc": calc, **{f"test_other{i}.Other": others for i in range(4)}}
        with open(self.times, "w") as f:
            json.dump(d, f)
        self.host = {"FM_TEST_TIMES": self.times, "FOREMAN_HOST": f"load=0,cpus={cpus}"}

    def runs(self):
        try:
            with open(self.count) as f:
                return len(f.read().split())
        except OSError:
            return 0

    def close(self):
        tid = self.fm_json("task", "new", "Change add", "--type", "FEATURE", "--tier", "S", "--ac", "works :: true",
                           "--step", "s", "--scope", "calc.py")["id"]
        self.fm("focus", tid)
        self.write("calc.py", SRC + "# tidy\n")
        return tid

    def finish(self, tid, check=False):
        r = self.fm("task", "finish", tid, "--audit", "self", "--run", "true", check=False,
                    env=dict({"FOREMAN_SENTINEL": "off"}, **self.host))
        if not check:
            return r
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def test_sized_under_the_ceiling_runs_with_the_sized_limit(self):
        self.record(calc=2.0, others=100.0)  # 2 of 402 recorded s: a small set, a limit of the floor
        tid = self.close()
        before = self.runs()
        r = self.finish(tid, check=True)
        self.assertEqual(self.runs(), before + 1, "the linked test runs once, under its sized limit")
        self.assertIn("limit 300 s", r.stdout)
        ev = c.ledger_events(self.p, ("close_affected",))[-1]["data"]
        self.assertEqual(ev["sized_s"], 300, "the record's sized limit is kept beside the one that ran")
        self.assertEqual(ev["limit"], 600, "the run gets the fixed 600 s when that is more: never sooner than before")

    def test_sized_over_the_ceiling_refuses_at_once_and_runs_nothing(self):
        self.record(calc=1000.0, others=600.0, cpus=1)  # 1000 s on one worker: about 1560 s, over the 1500 s ceiling
        tid = self.close()
        before = self.runs()
        r = self.finish(tid)
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.runs(), before, "a set that cannot finish under the ceiling runs no test")
        out = r.stdout + r.stderr
        self.assertIn("the next full gate (a train or sampo check) covers this tree", out)
        self.assertIn("finish again after it", out)
        self.assertIn("--skip-affected", out)
        self.assertIn("over the 1500 s limit", out)

    def finish_inproc(self, tid, linked=(0, ""), timeout=None):
        """The close in this process, so c.run_command can be stubbed for the linked tests (the run.py command): it
        answers `linked` (code, output) and records the limit it was given. Returns (exit code, text, limits seen)."""
        seen = []
        real = c.run_command

        def stub(root, cmd, timeout_s, env=None, **kw):
            if "run.py" not in cmd:
                return real(root, cmd, timeout_s, env=env, **kw)
            seen.append(timeout_s)
            return linked
        argv = ["task", "finish", tid, "--audit", "self", "--run", "true"] + (["--timeout", str(timeout)] if timeout else [])
        out, err, cwd = io.StringIO(), io.StringIO(), os.getcwd()
        os.chdir(self.repo)
        try:
            with mock.patch.dict(os.environ, dict({"FOREMAN_SENTINEL": "off"}, **self.host)), \
                    mock.patch.object(c, "run_command", side_effect=stub), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    code = cli.main(argv)
                except SystemExit as e:
                    code = e.code
        finally:
            os.chdir(cwd)
        return code, out.getvalue() + err.getvalue(), seen

    def last_close(self):
        return c.ledger_events(self.p, ("close_affected",))[-1]["data"]

    def test_sized_timeout_refuses_with_the_next_gate_advice(self):
        self.record(calc=2.0, others=100.0)  # sized 300 s; the run gets the fixed 600 s, which is more
        tid = self.close()
        code, out, seen = self.finish_inproc(tid, linked=(124, ""))
        self.assertNotEqual(code, 0, out)
        self.assertEqual(seen, [600], "the run gets the fixed limit, not the sized one, when that is more")
        self.assertIn("timed out at 600 s", out)
        self.assertIn("The next full gate (a train or sampo check) covers this tree", out)
        self.assertIn("--skip-affected", out)
        ev = self.last_close()
        self.assertEqual((ev["exit"], ev["limit"], ev["sized_s"]), (124, 600, 300))

    def test_explicit_timeout_is_the_limit_and_nothing_is_sized(self):
        self.record(calc=1000.0, others=600.0, cpus=1)  # over the ceiling: sized, this set would be refused
        tid = self.close()
        code, out, seen = self.finish_inproc(tid, timeout=700)
        self.assertEqual(code, 0, out)
        self.assertEqual(seen, [700], "the explicit --timeout is the limit")
        self.assertNotIn("too many to run", out)
        ev = self.last_close()
        self.assertEqual(ev["limit"], 700)
        self.assertNotIn("sized_s", ev, "no sizing was done")

    def test_empty_record_keeps_the_old_timeout_and_refuses_nothing(self):
        with open(self.times, "w") as f:  # a record with nothing in it: no seconds to size by
            json.dump({}, f)
        self.host = {"FM_TEST_TIMES": self.times, "FOREMAN_HOST": "load=0,cpus=8"}
        tid = self.close()
        code, out, seen = self.finish_inproc(tid, linked=(0, ""))
        self.assertEqual(code, 0, out)
        self.assertEqual(seen, [600], "the old timeout applies")
        self.assertNotIn("too many to run", out)
        self.assertNotIn("sized_s", self.last_close())

    def test_sized_refusal_is_covered_by_the_next_full_gate(self):
        # the refusal's own advice: a full check covers the tree, and the close finishes after it, running nothing
        self.record(calc=1000.0, others=600.0, cpus=1)
        tid = self.close()
        self.assertNotEqual(self.finish(tid).returncode, 0)
        self.fm("check", env=dict({"FOREMAN_SENTINEL": "off"}, **self.host))
        before = self.runs()
        r = self.finish(tid, check=True)
        self.assertEqual(self.runs(), before, "the full check's pass covers the linked test: nothing runs again")
        self.assertIn("covered by the gates' pass", r.stdout)


if __name__ == "__main__":
    unittest.main()
