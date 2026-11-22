"""T-5484: what the whole suite costs. cachecost.py reads the run cache (read only) and lists the classes whose record
holds "*" (the whole tree: every change reruns them) with their CPU s and the cause run.py recorded for them; the
suite's CPU s and its whole-tree share come from the same records. The cut classes keep a file list and still rerun
when a file they read changes."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import cachecost  # noqa: E402
import run  # noqa: E402


def fake_cache(folder, classes, why=None):
    """A cache file as run.save_cache writes it: classes {id: [[digest, tests, [paths], [cpu s, jobs]?], ...]}."""
    paths = sorted({p for recs in classes.values() for r in recs for p in r[2]})
    index = {p: i for i, p in enumerate(paths)}
    body = {"gen": 1, "paths": paths, "classes": {
        c: [[r[0], r[1], [index[p] for p in r[2]], *r[3:]] for r in recs] for c, recs in classes.items()}}
    if why:
        body["why"] = why
    path = os.path.join(folder, "cache.json")
    with open(path, "w") as f:
        json.dump(body, f)
    return path


class Wholes(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="cachecost-")
        self.addCleanup(shutil.rmtree, self._tmp, True)

    def test_wholes_listed_with_cause(self):
        path = fake_cache(self._tmp, {
            "test_a.Whole": [["d1", 3, ["*", "plugin/x.py"], [12.5, 1]]],
            "test_b.Narrow": [["d2", 4, ["plugin/y.py", "plugin/z.py"], [30.0, 1]]],
            "test_c.Costly": [["d3", 2, ["*"], [40.0, 2]]],
        }, why={"test_a.Whole": "git git status --porcelain", "test_c.Costly": "browser"})
        rows = cachecost.wholes(path)
        self.assertEqual([r["class"] for r in rows], ["test_c.Costly", "test_a.Whole"], rows)  # by CPU s, costliest first
        self.assertEqual(rows[0]["cpu"], 40.0)
        self.assertEqual(rows[0]["cause"], "browser")
        self.assertEqual(rows[1]["cpu"], 12.5)
        self.assertEqual(rows[1]["cause"], "git git status --porcelain")
        self.assertNotIn("test_b.Narrow", [r["class"] for r in rows])

    def test_a_record_without_a_cause_says_so(self):
        path = fake_cache(self._tmp, {"test_a.Whole": [["d1", 3, ["*"], [1.0, 1]]]})
        rows = cachecost.wholes(path)
        self.assertEqual(rows[0]["cause"], cachecost.UNRECORDED)

    def test_report_counts_match_cache(self):
        path = fake_cache(self._tmp, {
            "test_a.Whole": [["d1", 3, ["*", "plugin/x.py"], [12.5, 1]]],
            "test_b.Narrow": [["d2", 4, ["plugin/y.py"], [30.0, 1]], ["old", 9, ["*"], [99.0, 1]]],
            "test_c.Unmeasured": [["d3", 2, ["plugin/w.py"]]],
        }, why={"test_a.Whole": "git git status --porcelain"})
        s = cachecost.summary(path)
        self.assertEqual(s["classes"], 3)  # the newest record of each class counts, the older one does not
        self.assertEqual(s["whole"], 1)
        self.assertEqual(s["tests"], 3 + 4 + 2)
        self.assertEqual(s["whole_tests"], 3)
        self.assertAlmostEqual(s["cpu"], 42.5)  # the classes' newest CPU s, the unmeasured one adds none
        self.assertAlmostEqual(s["whole_cpu"], 12.5)
        text = cachecost.report(path)
        self.assertIn("1 of 3 classes", text)
        self.assertIn("test_a.Whole", text)
        self.assertNotIn("test_b.Narrow", text)
        self.assertRegex(text, r"\d+\.\d s\s+1 classes\s+git\n")  # grouped by the cause's first word
        self.assertRegex(text, r"\d+\.\d s\s+3 tests  test_a\.Whole  \[git git status --porcelain\]\n")


class Cause(unittest.TestCase):
    """run.py keeps the cause of each whole-tree record beside the records, so the report can say why it reruns."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="cachecost-")
        self.addCleanup(shutil.rmtree, self._tmp, True)

    def test_a_cause_is_the_first_whole_line_of_the_runs_cover(self):
        self.assertEqual(run.why_of(["f plugin/x.py\n* git git status --porcelain\n* browser chrome\n", None]),
                         "git git status --porcelain")
        self.assertIsNone(run.why_of(["f plugin/x.py\n", None]))
        self.assertEqual(len(run.why_of(["* " + "x" * 300])), 120)

    def test_causes_go_only_to_the_whole_tree_records(self):
        cov = {"test_a.Whole": ["* browser chrome"], "test_b.Narrow": ["* browser chrome"]}
        new = {"test_a.Whole": ["d", 1, ["*"]], "test_b.Narrow": ["d", 1, ["plugin/y.py"]]}
        self.assertEqual(run.causes(cov, new), {"test_a.Whole": "browser chrome"})

    def test_cut_class_keeps_narrow_record(self):
        # T-5484: a class whose only blind read is a HEAD of a repo the test built in the temp folder is not whole-tree
        repo = os.path.join(self._tmp, "repo")
        os.makedirs(os.path.join(repo, "plugin"))
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        with open(os.path.join(repo, "plugin", "x.py"), "w") as f:
            f.write("X = 1\n")
        tmp = os.path.join(self._tmp, "tmp")  # the temp folder: the test's own repo sits in a class's fmtest- folder of it
        scratch = os.path.join(tmp, "fmtest-5484-run", "scratch-repo")  # its HEAD is the blind read
        os.makedirs(scratch)
        subprocess.run(["git", "init", "-q"], cwd=scratch, check=True)
        texts = [f"f {os.path.join(repo, 'plugin', 'x.py')}\n", f"h {scratch}\0HEAD"]
        with mock.patch.object(tempfile, "gettempdir", return_value=tmp):
            got = run.run_paths(set(), texts, os.path.realpath(repo), {"plugin/x.py": "blob"}, None)
        self.assertNotIn("*", got, got)
        self.assertEqual(got, {"plugin/x.py"})

    def test_save_keeps_the_cause_beside_its_record(self):
        path = os.path.join(self._tmp, "run-cache.json")
        run.save_cache(path, {"test_a.Whole": ["d1", 3, ["*"], [1.0, 1]], "test_b.Narrow": ["d2", 2, ["plugin/y.py"]]},
                       0, {"test_a.Whole": "browser"})
        self.assertEqual(run.load_why(path), {"test_a.Whole": "browser"})
        self.assertEqual(run.load_cache(path)[1]["test_a.Whole"][0][0], "d1")  # the records load as before
        run.save_cache(path, {"test_c.Other": ["d3", 1, ["plugin/w.py"]]}, 0)
        self.assertEqual(run.load_why(path), {"test_a.Whole": "browser"}, "a save with no cause keeps the others'")
        run.save_cache(path, {"test_a.Whole": ["d4", 3, ["plugin/x.py"], [2.0, 1]]}, 0)
        self.assertEqual(run.load_why(path), {}, "a replaced record with no cause drops its old cause")
