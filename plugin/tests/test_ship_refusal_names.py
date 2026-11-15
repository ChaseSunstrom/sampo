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
