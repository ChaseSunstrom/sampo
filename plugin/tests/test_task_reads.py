"""T-4288: what making a task reads. The test runner records a stat of a tracked file as a dependency on the whole
file, and two cache stamps on that path stat'ed Sampo's own sources: the "Already covered?" note's (recall.verb_docs,
every file of the package) and recall's kept briefs' (recall.py and core.py). So 630 of 1,667 recorded classes re-ran
on any edit under plugin/lib/sampo and sat in every lane's affected set (273 test modules for a 7-line change). The
tests' default turns the note off (FOREMAN_NO_COVERED=1, set in helpers; test_nudges.Covered turns it on), and the
briefs' stamp says what it is (a "sampo.stamp" audit event the recorder reads)."""
import os
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from helpers import ForemanTestCase

from sampo import core as c

TEXT = "re-run the checks that recently finished tasks passed and report which ones fail now"
TOP = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class CoveredSwitch(ForemanTestCase):
    def setUp(self):
        super().setUp()
        # the marker run.py puts in every class's environment (SAMPO_SLOT, read as FOREMAN_SLOT once core mirrors it)
        patch = mock.patch.dict(os.environ, {"FOREMAN_SLOT": "none"})
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_session_outside_the_test_runner_gets_the_note_whatever_the_switch(self):
        os.environ.pop("FOREMAN_SLOT")  # T-4288: the switch is honoured only under run.py's marker, never in a real session
        self.fm("init")
        self.assertEqual("1", os.environ["FOREMAN_NO_COVERED"])
        self.assertIn("already covered?", self.fm("capture", TEXT, "--anyway").stdout.lower())

    def test_the_tests_default_makes_a_task_without_the_note(self):
        self.assertEqual("1", os.environ.get("FOREMAN_NO_COVERED"), "helpers sets it for every test")
        self.fm("init")
        out = self.fm("task", "new", TEXT, "--type", "FEATURE", "--tier", "S", "--ac", "x :: true", "--step", "x")
        self.assertNotIn("already covered?", out.stdout.lower())
        self.assertNotIn("already covered?", self.fm("capture", TEXT, "--anyway").stdout.lower())

    def test_without_the_switch_the_note_is_there_and_any_other_value_leaves_it_on(self):
        self.fm("init")
        for value in (None, "", "0"):
            os.environ.pop("FOREMAN_NO_COVERED", None)
            if value is not None:
                os.environ["FOREMAN_NO_COVERED"] = value
            self.assertIn("already covered?", self.fm("capture", TEXT, "--anyway").stdout.lower(), repr(value))

    def test_with_the_switch_no_file_of_the_package_is_stat_ed(self):
        from sampo import cli, recall
        seen, real = [], os.stat

        def stat(path, *a, **kw):
            if isinstance(path, str) and path.startswith(recall.SOURCES + os.sep):
                seen.append(path)
            return real(path, *a, **kw)

        with mock.patch.object(os, "stat", stat), mock.patch.object(recall, "covered", wraps=recall.covered) as asked:
            self.assertEqual("", cli._covered_note(None, "T-0001", TEXT))
            self.assertEqual([], seen, "the switch returns before recall is asked")
            self.assertEqual(0, asked.call_count)
            os.environ.pop("FOREMAN_NO_COVERED")
            note = cli._covered_note(None, "T-0001", TEXT)
        self.assertIn('sampo task drop T-0001 "covered by fm sentinel"', note)
        self.assertGreater(len({s for s in seen if s.endswith(".py")}), 100,
                           "what the switch saves: the note's cache check stats each source file of the package")


class OwnStamp(ForemanTestCase):
    def test_the_kept_briefs_stamp_says_what_it_is_before_it_stats_its_own_sources(self):
        from sampo import recall
        self.fm("init")
        calls, real = [], os.stat

        def stat(path, *a, **kw):
            if path in (recall.__file__, c.__file__):
                calls.append(("stat", path))
            return real(path, *a, **kw)

        with mock.patch.object(os, "stat", stat), \
                mock.patch.object(recall.sys, "audit", lambda ev, *a: calls.append((ev, *a))):
            recall._brief_docs(c.find_project(self.repo))
        own = (recall.__file__, c.__file__)
        self.assertEqual([("sampo.stamp", own), ("stat", own[0]), ("stat", own[1])],
                         [x for x in calls if x[0] in ("sampo.stamp", "stat")])
        self.assertEqual([[f, os.stat(f).st_mtime_ns, os.stat(f).st_size] for f in own], recall._own_stamp(*own),
                         "the stamp itself is as before: a kept cache stays valid")


class CodeStampAtLoad(unittest.TestCase):
    """T-6869: importing core stamps no module file (c6d0aff74 stat'ed all of them in every fm process, so every class
    that made a task depended on the whole package). A wake asks whether a module moved since this process loaded its
    code: the folder or a .py modified after LOADED_NS is a change, a file gone is a change."""

    def test_importing_core_stamps_no_module_file(self):
        self.assertIsNone(c.CODE_START)

    def test_a_module_moved_after_the_load_is_a_change_and_one_before_it_is_not(self):
        with mock.patch.object(c, "LOADED_NS", 0):
            self.assertTrue(c.code_changed(None), "every file is newer than a load at time zero")
        with mock.patch.object(c, "LOADED_NS", 1 << 62):
            self.assertFalse(c.code_changed(None), "nothing on disk moved after a load in the far future")

    def test_a_package_folder_that_cannot_be_read_is_a_change(self):
        with mock.patch.object(c, "__file__", os.path.join(TOP, "no-such-folder", "core.py")):
            self.assertTrue(c.code_changed(None))

    def test_a_module_restored_with_an_old_mtime_after_the_load_is_still_a_change(self):
        """T-6869: cp -p or rsync sets an old mtime back over a file the load already read: the ctime says when it moved."""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "mod.py")
            with open(path, "w") as fh:
                fh.write("X = 1\n")
            loaded = time.time_ns()
            time.sleep(0.02)  # ctime has a tick: the restore must land after the load's stamp
            os.utime(path, ns=(1, 1))
            self.assertGreater(os.stat(path).st_ctime_ns, loaded, "precondition: the restore is after the load")
            with mock.patch.object(c, "__file__", os.path.join(d, "core.py")), mock.patch.object(c, "LOADED_NS", loaded):
                self.assertTrue(c.code_changed(None), "mtime 1970 says unchanged; ctime says it moved after the load")


class TaskReads(ForemanTestCase):
    def test_making_and_focusing_a_task_reads_no_file_of_the_package_whole(self):
        """The tripwire: a new stat, open or listing of Sampo's own sources on the path of task new, focus or capture
        fails here, by name, before it puts a third of the suite in every lane's affected set again."""
        import run
        py = run.cover_python()
        if not py:
            self.skipTest("no recording python here (run.cover_python)")
        out = os.path.join(self.tmp, "cover")
        env = dict(os.environ, FOREMAN_HOME=self.home, FOREMAN_INHIBITED="1", FOREMAN_SLOT="none", RUNPY_COVER=out,
                   RUNPY_COVER_ROOTS=os.pathsep.join(run._roots(TOP)))
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        fm = os.path.join(TOP, "plugin", "bin", "fm")
        for args in (["init"],
                     ["task", "new", TEXT, "--type", "FIX", "--tier", "S", "--ac", "a :: true", "--step", "s", "--focus"],
                     ["capture", "cache the session token per request"],
                     ["focus", "T-0001"]):
            r = subprocess.run([py, fm, *args], cwd=self.repo, env=env, capture_output=True, text=True, timeout=120)
            self.assertEqual(0, r.returncode, f"fm {' '.join(args)}: {r.stderr[-800:]}")
        with open(out) as f:
            lines = f.read().splitlines()
        lib = os.path.join(os.path.realpath(TOP), "plugin", "lib", "sampo")
        self.assertTrue([x for x in lines if x.startswith(f"c {lib}{os.sep}cli.py ")], "the recorder was on")
        whole = sorted({x[2:][len(lib) + 1:] for x in lines
                        if x[:2] in ("s ", "f ") and x[2:].startswith(lib + os.sep) and x.endswith(".py")})
        self.assertEqual([], whole, "stat'ed or opened whole: every class that makes a task would depend on all of each")
        self.assertNotIn(f"d {lib}", lines, "the package's folder listed: a new module would rerun every such class")


if __name__ == "__main__":
    unittest.main()
