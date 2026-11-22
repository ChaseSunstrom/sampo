"""T-4739: a ship refused on its full run says what failed. Train 1115g (2026-11-15) was refused with only "the test failed
in the scratch tree (baseline task T-4440)": the classes were not named, T-4440 was three days old, and ship.log held
nothing of that ship, so a second full run was the only way to learn them. Now the refusal names the failed classes (at
most five, then a count), says whether the rerun alone also failed, names the file holding the run's output, and names a
baseline task as "recorded in" only when this run filed it (an older one says how old it is); a land's own in-line ship
writes the same ship.log block the detached ship writes: a start line (time, head, grant), its output and an end line."""
# map-links: plugin/lib/sampo/release.py, plugin/lib/sampo/train.py
import json
import os
import re
import subprocess
import time
import unittest
from unittest import mock

from helpers import ForemanTestCase

from sampo import core as c
from sampo import release, train


def sh(root, *a):
    return subprocess.run(["git", "-C", root, *a], capture_output=True, text=True, check=True).stdout


def red_out(*classes):
    """What run.py prints for red classes: unittest's FAIL line per class, then its summary and one `failed class:` each."""
    return ("".join(f"FAIL: test_it ({k}.test_it)\nAssertionError: False is not true\n\n" for k in classes)
            + f"Ran {len(classes)} tests in 1.0s\n\nFAILED ({len(classes)} classes)\n"
            + "".join(f"  failed class: {k}\n" for k in classes))


class Refused(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.origin = os.path.join(self.tmp, "origin.git")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", self.origin], check=True)
        sh(self.repo, "remote", "add", "origin", self.origin)
        sh(self.repo, "push", "-q", "origin", "HEAD:main")
        sh(self.repo, "checkout", "-q", "-b", "work")
        with open(os.path.join(self.repo, "a.txt"), "w") as f:
            f.write("a\n")
        sh(self.repo, "add", "a.txt")
        sh(self.repo, "commit", "-qm", "add a")
        self.tid = json.loads(self.fm("task", "new", "land", "--type", "FEATURE", "--tier", "S", "--json").stdout)["id"]
        self.fm("task", "step", self.tid, "add", "land it")
        self.fm("task", "ac", self.tid, "add", "landed", "--verify", "true")
        self.fm("focus", self.tid)
        self.p = c.find_project(self.repo)
        b = c.find_brief(self.p, self.tid)
        b.meta["allow"] = ["publish"]
        c.save_brief(self.p, b)

    def red_ship(self, *classes, env=None):
        script = os.path.join(self.tmp, "red.sh")
        with open(script, "w") as f:
            f.write("cat <<'EOF'\n" + red_out(*classes) + "EOF\nexit 1\n")
        return self.fm("ship", "--remote", "origin", "--test", f"sh {script}", check=False, env=env)

    def pushed(self):
        return "add a" in sh(self.origin, "log", "--format=%s", "main")

    def test_names_the_failed_classes_the_rerun_and_the_file_holding_the_output(self):
        r = self.red_ship("test_bad.Bad", "test_worse.Worse", env={"FM_POSTMERGE_RERUN": "false"})
        said = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 0, said)
        refusal = next(ln for ln in said.splitlines() if "failed in the scratch tree" in ln)
        self.assertIn("test_bad.Bad", refusal)
        self.assertIn("test_worse.Worse", refusal)
        self.assertIn("rerun alone also failed", refusal)
        log = os.path.join(self.p.dir, "ship-test.log")
        self.assertIn(log, refusal, "the refusal says where the full output is")
        with open(log) as f:
            body = f.read()
        self.assertIn("failed class: test_bad.Bad", body)
        self.assertIn("failed class: test_worse.Worse", body)
        self.assertIn("nothing was pushed", refusal)
        self.assertFalse(self.pushed())

    def test_a_class_that_cannot_run_alone_is_said_not_called_a_failed_rerun(self):
        r = self.red_ship("test_bad.Bad")  # this scratch repo has no runner to rerun a class with
        refusal = next(ln for ln in (r.stdout + r.stderr).splitlines() if "failed in the scratch tree" in ln)
        self.assertIn("test_bad.Bad", refusal)
        self.assertRegex(refusal, r"(?i)could not be rerun alone")
        self.assertNotIn("also failed", refusal)

    def test_more_than_five_classes_are_five_and_a_count(self):
        names = [f"test_m{i}.M{i}" for i in range(8)]
        r = self.red_ship(*names, env={"FM_POSTMERGE_RERUN": "false"})
        refusal = next(ln for ln in (r.stdout + r.stderr).splitlines() if "failed in the scratch tree" in ln)
        self.assertEqual(sum(n in refusal for n in names), 5, refusal)
        self.assertIn("3 left out", refusal)
        with open(os.path.join(self.p.dir, "ship-test.log")) as f:
            body = f.read()
        self.assertTrue(all(f"failed class: {n}" in body for n in names), "the file holds all of them")

    def test_a_baseline_this_run_filed_is_recorded_in(self):
        r = self.red_ship("test_bad.Bad", env={"FM_POSTMERGE_RERUN": "false"})
        refusal = next(ln for ln in (r.stdout + r.stderr).splitlines() if "failed in the scratch tree" in ln)
        self.assertRegex(refusal, r"recorded in T-\d+")
        self.assertNotIn("baseline task", refusal)

    def test_a_baseline_older_than_a_day_says_how_old(self):
        b = c.find_brief(self.p, self.tid)
        b.meta["created"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 3 * 86400 - 60))
        c.save_brief(self.p, b)
        old = release._baseline_note(self.p, [f"baseline still red: {self.tid}"])
        self.assertRegex(old, rf"{self.tid}.*\d+ days old")
        self.assertNotIn("recorded in", old, "this run filed nothing")
        new = release._baseline_note(self.p, [f"baseline red: captured {self.tid} (urgent): x"])
        self.assertIn(f"recorded in {self.tid}", new)
        self.assertEqual(release._baseline_note(self.p, []), "")

    def test_a_class_less_failure_says_so_and_still_names_the_file(self):
        script = os.path.join(self.tmp, "bare.sh")
        with open(script, "w") as f:
            f.write("echo boom\nexit 3\n")
        r = self.fm("ship", "--remote", "origin", "--test", f"sh {script}", check=False)
        refusal = next(ln for ln in (r.stdout + r.stderr).splitlines() if "failed in the scratch tree" in ln)
        self.assertIn("no failing class named", refusal)
        self.assertIn("exit 3", refusal)
        self.assertIn(os.path.join(self.p.dir, "ship-test.log"), refusal)

    def test_names_the_failing_test_and_its_exception_from_the_first_runs_output(self):
        """T-5046: no second run to learn which test failed and how: the refusal carries class.test and the exception."""
        r = self.red_ship("test_bad.Bad", env={"FM_POSTMERGE_RERUN": "false"})
        refusal = next(ln for ln in (r.stdout + r.stderr).splitlines() if "failed in the scratch tree" in ln)
        self.assertIn("test_bad.Bad.test_it", refusal)
        self.assertIn("AssertionError: False is not true", refusal)

    def test_at_most_three_failing_tests_are_named_then_a_count(self):
        r = self.red_ship(*[f"test_m{i}.M{i}" for i in range(8)], env={"FM_POSTMERGE_RERUN": "false"})
        refusal = next(ln for ln in (r.stdout + r.stderr).splitlines() if "failed in the scratch tree" in ln)
        self.assertEqual(len(re.findall(r"\.test_it\b", refusal)), 3, refusal)
        self.assertIn("5 more", refusal)

    def test_the_ships_run_of_run_py_is_stamped_with_an_id_for_its_record(self):
        os.makedirs(os.path.join(self.tmp, "tests"))
        script = os.path.join(self.tmp, "tests", "run.py")  # a command that names tests/run.py is stamped with a run id
        with open(script, "w") as f:
            f.write("echo \"stamped=$SAMPO_TEST_RUN_ID\"\nexit 1\n")
        self.fm("ship", "--remote", "origin", "--test", f"sh {script}", check=False, env={"FM_POSTMERGE_RERUN": "false"})
        with open(os.path.join(self.p.dir, "ship-test.log")) as f:
            self.assertRegex(f.read(), r"stamped=[0-9a-f]{16}\b")


class FullRefusalRecord(ForemanTestCase):
    """T-5046: _full_refusal reads the failing tests from the first run's record, else from the run's FAIL:/ERROR: blocks."""

    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)

    def refusal(self, tail, rec, **kw):
        with mock.patch.object(c, "run_record", return_value=rec) as rr:
            line = release._full_refusal(self.p, tail, 1, [], [], "python3 plugin/tests/run.py", wt="/wt", run_id="abc", **kw)
        return line, rr

    def test_the_record_read_by_run_id_gives_class_test_and_the_exception(self):
        rec = {"ok": False, "failed": [{"class": "test_a.A", "test": "test_one", "first": "KeyError: 'x'"},
                                       {"class": "test_b.B", "test": "", "first": "import failed: no module q"}]}
        line, rr = self.refusal("", rec)  # the run's own output lost every name (a pipe, a cut): the record has them
        rr.assert_called_once_with("/wt", "python3 plugin/tests/run.py", "abc")
        self.assertIn("test_a.A.test_one: KeyError: 'x'", line)
        self.assertIn("test_b.B: import failed: no module q", line, "a class that failed whole is named without a test")
        self.assertIn("test_a.A", line)

    def test_a_record_that_is_missing_falls_back_to_the_output_blocks(self):
        tail = ("ERROR: test_it (test_x.X.test_it)\n----------------------------------------------------------------------\n"
                "Traceback (most recent call last):\n  File \"a.py\", line 3, in test_it\n    f()\nValueError: boom\n\n"
                "======================================================================\n"
                "FAIL: test_two (test_x.X.test_two)\n----------------------------------------------------------------------\n"
                "Traceback (most recent call last):\n  File \"a.py\", line 9, in test_two\nAssertionError: 1 != 2\n\n"
                "----------------------------------------------------------------------\nRan 2 tests in 0.1s\n\nFAILED (failures=1, errors=1)\n")
        line, _ = self.refusal(tail, None)
        self.assertIn("test_x.X.test_it: ValueError: boom", line)
        self.assertIn("test_x.X.test_two: AssertionError: 1 != 2", line)
        self.assertNotIn("Traceback", line)

    def test_a_green_or_unreadable_record_changes_nothing_and_no_run_id_reads_nothing(self):
        line, _ = self.refusal(red_out("test_c.C"), {"ok": True, "failed": []})
        self.assertIn("test_c.C.test_it: AssertionError", line)
        with mock.patch.object(c, "run_record", side_effect=OSError("gone")):
            line = release._full_refusal(self.p, red_out("test_c.C"), 1, [], [], "x", wt="/wt", run_id="abc")
        self.assertIn("test_c.C.test_it", line, "a record that can't be read never hides the refusal")
        with mock.patch.object(c, "run_record") as rr:
            release._full_refusal(self.p, "", 1, [], [], "x", wt="/wt", run_id=None)
        rr.assert_not_called()

    def test_an_exception_line_is_cut_so_the_refusal_stays_one_short_line(self):
        rec = {"ok": False, "failed": [{"class": "test_a.A", "test": "t", "first": "ValueError: " + "x" * 500}]}
        line, _ = self.refusal("", rec)
        self.assertLess(len(line), 400, line)

    def test_a_secret_split_by_a_zero_width_character_is_redacted_in_the_exception(self):
        """T-5046: plain() strips zero-width characters, so a token split by one must be redacted after plain() joins it."""
        token = "ghp_abcdefghij​klmnopqrstuvwxyz0123"
        rec = {"ok": False, "failed": [{"class": "test_a.A", "test": "t", "first": f"ValueError: token {token} leaked"}]}
        line, _ = self.refusal("", rec)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz0123", line)
        self.assertIn("[REDACTED]", line)

    def test_a_subtest_failing_three_times_is_one_test_on_the_block_path_and_the_record_path(self):
        blocks = "".join(f"FAIL: test_it (m.C.test_it) (i={i})\n----------\nAssertionError: bad {i}\n\n" for i in (1, 2, 3))
        tail = blocks + "Ran 1 test in 1.0s\n\nFAILED (1 class)\n  failed class: m.C\n"
        got = release._red_tests(None, tail, "cmd", None)
        self.assertEqual([(k, t) for k, t, _ in got], [("m.C", "test_it")])
        rec = {"ok": False, "failed": [{"class": "m.C", "test": "test_it", "first": f"AssertionError: bad {i}"} for i in (1, 2, 3)]}
        with mock.patch.object(release.c, "run_record", return_value=rec):
            got = release._red_tests("/wt", "", "cmd", "r1")
        self.assertEqual([(k, t) for k, t, _ in got], [("m.C", "test_it")])
        self.assertEqual(release._tests_clause(got, 400).count("m.C.test_it"), 1)


class LandShipLog(ForemanTestCase):
    """The land's in-line ship (a train by hand, a land where nothing may run in the background) writes ship.log too."""

    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)
        self.tid = json.loads(self.fm("task", "new", "land", "--type", "FEATURE", "--tier", "S", "--json").stdout)["id"]
        b = c.find_brief(self.p, self.tid)
        b.meta["allow"] = ["publish"]
        c.save_brief(self.p, b)
        self.log = os.path.join(self.p.dir, "ship.log")

    def ship(self, rc, out, err):
        with mock.patch.object(train, "_sampo", return_value=(rc, out, err)):
            return train._ship(self.p, False, self.repo)

    def test_a_refused_ship_leaves_a_block_with_start_and_end_lines_and_the_classes(self):
        out = red_out("test_bad.Bad", "test_worse.Worse")
        refusal = ("fm: the test failed in the scratch tree: failing test_bad.Bad, test_worse.Worse (the rerun alone also "
                   "failed); full output: /x/ship-test.log; nothing was pushed")
        grant, line = self.ship(1, out, refusal + "\n")
        self.assertEqual(grant, self.tid)
        self.assertIn("test_bad.Bad", line, "the land's own summary line carries the classes")
        with open(self.log) as f:
            lines = f.read().splitlines()
        head = sh(self.repo, "rev-parse", "HEAD").strip()
        self.assertRegex(lines[0], rf"^\d\d:\d\d:\d\d ship of {head[:9]} on {self.tid}'s publish grant")
        self.assertIn("  failed class: test_bad.Bad", lines)
        self.assertRegex(lines[-1], r"^\d\d:\d\d:\d\d refused: .*test_bad\.Bad")

    def test_the_ship_last_line_carries_the_failing_tests_and_their_exception(self):
        """T-5046: the refusal's `first failures` clause comes before the output path, inside train.REFUSAL_ROOM characters."""
        out = red_out("test_bad.Bad", "test_worse.Worse", "test_c.C", "test_d.D", "test_e.E")
        refusal = release._full_refusal(self.p, out, 1, ["the rerun alone also failed"], [], "python3 plugin/tests/run.py")
        line, _ = train._ship_end(self.p, self.tid, "a" * 40, 1, "fm: " + refusal + "\n", 0.0, 1.0)
        self.assertIn("test_bad.Bad.test_it: AssertionError: False is not true", line)
        self.assertRegex(line, r"\(\+\d more\)", "the count of the tests left out is kept")

    def test_long_exceptions_still_leave_the_path_and_the_count_in_the_ship_last_line(self):
        """T-5046: three 100-character exceptions are cut to the room left, so the path and `nothing was pushed` survive."""
        refusal = self.long_refusal()
        line, _ = train._ship_end(self.p, self.tid, "a" * 40, 1, "fm: " + refusal + "\n", 0.0, 1.0)
        self.assertIn("full output: /x/state/outputs/ship-0123456789ab.txt", line)
        self.assertIn("nothing was pushed", line)
        self.assertRegex(line, r"\(\+\d more\)")

    def long_refusal(self):
        long = "AssertionError: " + "x" * 90
        keys = ("a.A", "b.B", "c.C", "d.D", "e.E")
        out = ("".join(f"FAIL: test_it ({k}.test_it)\n{long}\n\n" for k in keys)
               + "Ran 5 tests in 1.0s\n\nFAILED (5 classes)\n" + "".join(f"  failed class: {k}\n" for k in keys))
        with mock.patch.object(release, "_keep_output", return_value="/x/state/outputs/ship-0123456789ab.txt"):
            return release._full_refusal(self.p, out, 1, ["the rerun alone also failed"], [], "python3 plugin/tests/run.py")

    def test_a_refusal_of_the_full_width_keeps_its_path_and_clause_in_every_place_it_is_stored_or_shown(self):
        """T-5046: the refusal is as wide as train.REFUSAL_ROOM; the ship record, the in-line land's log line and its
        summary keep all of it, so `sampo status` and the next land still say where the output is."""
        refusal = self.long_refusal()
        self.assertGreater(len(refusal), 400, "a refusal wider than the old cut is the case")
        self.assertLessEqual(len(refusal), train.REFUSAL_ROOM)
        line, _ = train._ship_end(self.p, self.tid, "a" * 40, 1, "fm: " + refusal + "\n", 0.0, 1.0)
        with open(os.path.join(self.p.dir, "ship-last.json")) as f:
            kept = json.load(f)["line"]
        for text in (kept, line):
            self.assertIn("full output: /x/state/outputs/ship-0123456789ab.txt", text)
            self.assertIn("nothing was pushed", text)
        grant, said = self.ship(1, "", "fm: " + refusal + "\n")
        with open(self.log) as f:
            last = f.read().splitlines()[-1]
        for text in (last, said):
            self.assertIn("full output: /x/state/outputs/ship-0123456789ab.txt", text)
            self.assertIn("nothing was pushed", text)

    def test_a_second_ship_is_the_last_block_and_the_first_is_kept(self):
        self.ship(1, red_out("test_one.One"), "fm: the test failed in the scratch tree: failing test_one.One\n")
        self.ship(1, red_out("test_two.Two"), "fm: the test failed in the scratch tree: failing test_two.Two\n")
        with open(self.log) as f:
            text = f.read()
        starts = [m.start() for m in re.finditer(r"(?m)^\d\d:\d\d:\d\d ship of ", text)]
        self.assertEqual(len(starts), 2)
        self.assertIn("test_one.One", text[:starts[1]])
        self.assertNotIn("test_one.One", text[starts[1]:], "the newest ship is the log's last block")
        self.assertIn("test_two.Two", text[starts[1]:])

    def test_a_passing_ship_writes_its_block_with_the_result(self):
        self.ship(0, "testing 1 on origin main\nmain at " + "a" * 40 + "\n", "")
        with open(self.log) as f:
            lines = f.read().splitlines()
        self.assertRegex(lines[0], r"ship of .* publish grant")
        self.assertIn("a" * 40, lines[-1])

    def test_the_detached_ships_record_line_is_the_refusal_not_the_start_of_its_output(self):
        out = ("testing 1 on origin main, a snapshot of the head at abc: sh run.sh\n" + red_out("test_bad.Bad")
               + "fm: the test failed in the scratch tree: failing test_bad.Bad; nothing was pushed\n")
        line, data = train._ship_end(self.p, self.tid, "h", 1, out, 0.0, 1.0)
        self.assertEqual(line.count("test_bad.Bad"), 1, line)
        self.assertTrue(line.startswith("refused: the test failed in the scratch tree"), line)
        self.assertEqual(data["red"], ["test_bad.Bad"])
        bare = ("testing 1 on origin main\n" + red_out("test_bad.Bad") + "fm: the test failed in the scratch tree; "
                "nothing was pushed\n")
        self.assertIn("failing test_bad.Bad", train._ship_end(self.p, self.tid, "h", 1, bare, 0.0, 1.0)[0])

    def test_a_log_that_cannot_be_written_never_undoes_the_ship(self):
        os.makedirs(self.log)  # a directory where the file goes
        grant, line = self.ship(1, "", "fm: nope\n")
        self.assertEqual(grant, self.tid)
        self.assertIn("nope", line)


if __name__ == "__main__":
    unittest.main()
