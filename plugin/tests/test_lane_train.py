"""T-3380: sampo lane train merges N reviewed lanes in a scratch integration worktree, gates once, bisects a red set to
the culprit lanes, fast-forwards the main checkout's branch, and records a gate pass `sampo check` reuses while the code
tree (minus CHANGELOG.md and the sync mirror) is the same."""
# map-links: plugin/lib/sampo/train.py
import contextlib
import json
import math
import os
import shutil
import subprocess
import time
import types
import unittest
from unittest import mock

from helpers import ForemanTestCase

from sampo import core as c
from sampo import train

first = lambda text: json.JSONDecoder().raw_decode(text)[0]  # noqa: E731


class TrainCase(ForemanTestCase):
    """The fixtures: an initialised repo with one gate that counts its runs and fails when BREAK exists."""
    def setUp(self):
        super().setUp()
        self.unreviewed_ids = set()  # T-5968: lanes made with reviewed=False: train() leaves them as they are
        self.fm("init")
        self.runs = os.path.join(self.tmp, "gate-runs")
        self.fm("check", "add", f"echo x >> {self.runs}; test ! -e BREAK -o -e FIX.txt")

    def git(self, *args, cwd=None):
        return subprocess.run(["git", "-C", cwd or self.repo, *args], capture_output=True, text=True).stdout.strip()

    def gate_runs(self):
        return len(open(self.runs).read().split()) if os.path.exists(self.runs) else 0

    def stamp_accepted(self, tid, path=None):
        """T-5968: the lane as `sampo lane accept` leaves it: its accepted_tip stamped at the tip it has now (every path to a
        trunk holds a lane with no review at its tip, lanes.review_hold)."""
        p = c.find_project(self.repo)
        b = c.find_brief(p, tid)
        tip = self.git("rev-parse", "HEAD", cwd=path or b.meta["lane"])
        b.meta["accepted_tip"] = tip
        c.save_brief(p, b)
        self.unreviewed_ids.discard(tid)
        return tip

    def lane(self, n, name, text=None, ac="true", when=None, tier="S", reviewed=True):
        """A lane worktree with a task and one commit writing `name` (dated `when`, else now); its task id. T-5968: it is
        accepted at that commit unless `reviewed=False` (the review hold's own tests)."""
        path = os.path.join(self.tmp, f"lane{n}")
        subprocess.run(["git", "-C", self.repo, "worktree", "add", "-q", "-b", f"foreman/lane{n}", path], check=True)
        plan = (["--interpretation", "Reading 1: the lane's change. Reading 2: none.",
                 "--approach", "1) use what exists: the close's own rule. fails if: the rule needs state the merge "
                               "makes. 2) do less: say it in the help. fails if: the close still refuses. 3) relax the "
                               "close. fails if: an unaudited change lands. Choice 1 (80%): the rule is already code."]
                if tier != "S" else [])  # M/L plan gate
        tid = first(self.fm("task", "new", f"Lane {n}", "--type", "FEATURE", "--tier", tier, "--ac", f"ok :: {ac}",
                            *plan, "--step", "s", "--json", "--focus", cwd=path).stdout)["id"]
        os.makedirs(os.path.dirname(os.path.join(path, name)), exist_ok=True)  # T-5735: a lane may write a nested path
        with open(os.path.join(path, name), "w") as f:
            f.write(text or f"{n}\n")
        self.git("add", name, cwd=path)
        subprocess.run(["git", "-C", path, "commit", "-qm", f"lane {n}"], check=True, capture_output=True,
                       env=dict(os.environ, GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when) if when else None)
        if reviewed:
            self.stamp_accepted(tid, path)
        else:
            self.unreviewed_ids.add(tid)
        return tid

    def train(self, *ids, check=True, env=None):
        for tid in ids:  # T-5968: a test that commits to a lane after making it still trains a lane its review covers
            if tid not in self.unreviewed_ids:
                self.stamp_accepted(tid)
        return self.fm("lane", "train", ",".join(ids), check=check, env=env)

    def meta(self, tid):
        return json.loads(self.fm("task", "show", tid, "--json").stdout)["meta"]


class Train(TrainCase):
    def test_live_land_merges_every_lane_with_no_front_gate_and_records_each_for_the_behind_verify(self):
        # T-5444: 83 finished lanes waited at 2 per 2 h behind 2-5 h front gates; live lands them at once
        from sampo import lanes
        self.fm("lane", "mode", "live")
        ids = [self.lane(1, "a.txt"), self.lane(2, "b.txt"), self.lane(3, "BREAK")]
        r = self.train(*ids, env={"FM_POSTMERGE_CMD": "true", "FOREMAN_NO_BACKGROUND": "1"})
        self.assertEqual(self.gate_runs(), 0, "no gate runs in front of a live train")
        for name in ("a.txt", "b.txt", "BREAK"):
            self.assertTrue(os.path.exists(os.path.join(self.repo, name)), f"{name} landed: {r.stdout}{r.stderr}")
        p = c.find_project(self.repo)
        self.assertEqual(sorted(r["task"] for r in lanes.pm_records(p).values()), sorted(ids),
                         "each merge waits for the behind verify, which reverts a red one")

    def test_live_land_never_lands_a_tree_that_does_not_compile(self):
        from sampo import train
        self.fm("lane", "mode", "live")
        os.makedirs(os.path.join(self.repo, "plugin", "lib", "sampo"))
        with open(os.path.join(self.repo, "plugin", "lib", "sampo", "ok.py"), "w") as f:
            f.write("x = 1\n")
        self.git("add", "plugin")
        self.git("commit", "-qm", "a Sampo-shaped tree")
        bad = os.path.join("plugin", "lib", "sampo", "broken.py")
        tid = self.lane(1, bad, "def f(:\n")
        self.assertIn("SyntaxError", train._live_smoke(os.path.join(self.tmp, "lane1")))
        self.assertEqual(train._live_smoke(self.repo), "")
        r = self.train(tid, check=False, env={"FM_POSTMERGE_CMD": "true", "FOREMAN_NO_BACKGROUND": "1"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("does not compile", r.stderr + r.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.repo, bad)))

    def test_live_shape_runs_the_repo_shape_classes_once_and_only_on_a_sampo_tree(self):
        # T-5590: the live train's one repo-shape run on the merged tree: '' for green or a non-Sampo tree, else the why
        from sampo import train
        plain = os.path.join(self.tmp, "plain")
        shape = os.path.join(self.tmp, "shape")
        os.makedirs(os.path.join(shape, "plugin", "tests"))
        with open(os.path.join(shape, "plugin", "tests", "run.py"), "w") as f:
            f.write("# a stand-in runner\n")
        with mock.patch.object(train, "_shape_run", return_value=("green", "")) as run:
            self.assertEqual(train._live_shape(plain).verdict, "green", "another project's tree is not held to the classes")
            run.assert_not_called()
            self.assertEqual(train._live_shape(shape).verdict, "green")
            run.assert_called_once()
        with mock.patch.object(train, "_shape_run", return_value=("red", "test_docs, test_run")):
            got = train._live_shape(shape)
            self.assertEqual((got.verdict, got.names), ("red", "test_docs, test_run"))
            self.assertIn("breaks the repo-shape classes: test_docs, test_run", got.why)
        with mock.patch.object(train, "_shape_run", return_value=("cut", "")):
            got = train._live_shape(shape)
            self.assertEqual(got.verdict, "cut")
            self.assertIn("was cut", got.why)
            self.assertIn("SAMPO_SHAPE_BATCH_WALL_S", got.why)

    def test_live_shape_base_red_classes_do_not_stop_a_train_only_new_ones_do(self):
        # T-5805: a class red on the base is trunk's, not the batch's: the train lands, and one express FIX names the set
        from sampo import train
        p = c.find_project(self.repo)
        os.makedirs(os.path.join(self.repo, "plugin", "tests"))
        with open(os.path.join(self.repo, "plugin", "tests", "run.py"), "w") as f:
            f.write("# the trunk's stand-in runner\n")
        self.git("add", "plugin")
        self.git("commit", "-qm", "a Sampo tree with its runner")
        base = self.git("rev-parse", "HEAD").strip()
        shape = os.path.join(self.tmp, "shape")
        os.makedirs(os.path.join(shape, "plugin", "tests"))
        with open(os.path.join(shape, "plugin", "tests", "run.py"), "w") as f:
            f.write("# a stand-in runner\n")
        runs = lambda merged, basev: mock.patch.object(train, "_shape_run", side_effect=[merged, basev])
        with runs(("red", "test_docs, test_run"), ("red", "test_docs, test_run")) as run:
            self.assertEqual(train._live_shape(shape, p, self.repo, base).verdict, "green", "all red on the base: nothing new")
            self.assertEqual(run.call_count, 2)
        with runs(("red", "test_docs"), ("red", "test_docs")) as run:  # the base's verdict is cached per sha
            self.assertEqual(train._live_shape(shape, p, self.repo, base).verdict, "green")
            self.assertEqual(run.call_count, 1, "the base ran once for its sha")
        trunk = [b for b in c.load_briefs(p) if "Trunk red" in b.title]
        self.assertEqual(len(trunk), 1, "one trunk-red FIX per class set")
        self.assertEqual(trunk[0].priority, "urgent")
        self.assertIn("test_docs", trunk[0].title)
        c.update_meta(p, live_shape_base=None)
        with runs(("red", "test_docs, test_new"), ("red", "test_docs")):
            got = train._live_shape(shape, p, self.repo, base)
            self.assertEqual(got.verdict, "red")
            self.assertIn("breaks the repo-shape classes: test_new", got.why)
            self.assertNotIn("test_docs", got.why)
        c.update_meta(p, live_shape_base=None)
        with runs(("red", "test_docs"), ("green", "")):
            self.assertIn("breaks the repo-shape classes: test_docs", train._live_shape(shape, p, self.repo, base).why)
        c.update_meta(p, live_shape_base=None)
        with runs(("red", "test_docs"), ("cut", "")):
            self.assertIn("breaks the repo-shape classes: test_docs", train._live_shape(shape, p, self.repo, base).why,
                          "a base with no verdict forgives nothing")

    def test_live_land_shape_base_red_lands_a_batch_red_only_where_its_base_is_and_refuses_a_new_class(self):
        # T-5805: the whole land, with a trunk whose runner is red on test_docs
        self.fm("lane", "mode", "live")
        os.makedirs(os.path.join(self.repo, "plugin", "tests"))
        with open(os.path.join(self.repo, "plugin", "tests", "run.py"), "w") as f:
            f.write("import sys\nprint('  failed class: test_docs')\nsys.exit(1)\n")
        self.git("add", "plugin")
        self.git("commit", "-qm", "a Sampo tree red on test_docs")
        env = {"FM_POSTMERGE_CMD": "true", "FOREMAN_NO_BACKGROUND": "1"}
        head = self.git("rev-parse", "HEAD")
        worse = "import sys\nprint('  failed class: test_docs')\nprint('  failed class: test_run')\nsys.exit(1)\n"
        bad = self.lane(1, os.path.join("plugin", "tests", "run.py"), worse)
        r = self.train(bad, check=False, env=env)
        self.assertNotEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("test_run", r.stderr + r.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD"), head, "a new red class lands nothing")
        ok = self.lane(2, "b.txt")
        r = self.train(ok, check=False, env=env)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertNotEqual(self.git("rev-parse", "HEAD"), head, "a batch red only where its base is red landed")

    def test_live_land_refuses_a_batch_whose_merged_tree_breaks_or_cuts_the_repo_shape_classes(self):
        # T-5590: red or cut on the merged tree lands nothing; green lands. Each lane's stand-in runner is its own change.
        self.fm("lane", "mode", "live")
        os.makedirs(os.path.join(self.repo, "plugin", "tests"))
        with open(os.path.join(self.repo, "plugin", "tests", "run.py"), "w") as f:
            f.write("# the trunk's stand-in runner\nimport sys\nsys.exit(0)\n")
        self.git("add", "plugin")
        self.git("commit", "-qm", "a Sampo tree with its runner")
        stubs = {"red": "import sys\nprint('  failed class: test_docs')\nsys.exit(1)\n",
                 "cut": "import sys\nsys.exit(1)\n", "green": "import sys\nsys.exit(0)\n"}
        env = {"FM_POSTMERGE_CMD": "true", "FOREMAN_NO_BACKGROUND": "1"}
        for n, verdict in enumerate(("red", "cut", "green"), 1):
            with self.subTest(verdict=verdict):
                head = self.git("rev-parse", "HEAD")
                tid = self.lane(n, os.path.join("plugin", "tests", "run.py"), stubs[verdict])
                r = self.train(tid, check=False, env=env)
                out = r.stderr + r.stdout
                if verdict == "green":
                    self.assertEqual(r.returncode, 0, out)
                    self.assertNotEqual(self.git("rev-parse", "HEAD"), head, "the green batch landed")
                    continue
                self.assertNotEqual(r.returncode, 0, out)
                self.assertIn("nothing landed", out)
                self.assertEqual(self.git("rev-parse", "HEAD"), head, "a refused batch lands nothing")
                if verdict == "red":
                    self.assertIn("test_docs", out)
                else:
                    self.assertIn("was cut", out)
                    self.assertIn("SAMPO_SHAPE_BATCH_WALL_S", out)

    def shape_trunk(self, red_when):
        """T-5806: a live project whose trunk has a stand-in shape runner: red (class test_docs) when `red_when(files)` holds
        of the data files present in its tree. Returns the file that counts each run."""
        self.fm("lane", "mode", "live")
        count = os.path.join(self.tmp, "shape-runs")
        os.makedirs(os.path.join(self.repo, "plugin", "tests"))
        with open(os.path.join(self.repo, "plugin", "tests", "run.py"), "w") as f:
            f.write("import os, sys\n"
                    f"open({count!r}, 'a').write(os.getcwd() + ' ' + ','.join(sorted(os.listdir('.'))) + '\\n')\n"
                    f"if {red_when}:\n    print('  failed class: test_docs')\n    sys.exit(1)\n")
        self.git("add", "plugin")
        self.git("commit", "-qm", "a Sampo tree with its runner")
        return count

    def test_live_shape_bisect_parks_the_one_lane_that_adds_the_red_class_and_lands_the_rest(self):
        # T-5806: a merged batch with one lane that adds a new repo-shape red stopped the whole land
        count = self.shape_trunk("os.path.exists('RED.txt')")
        a, bad, b = self.lane(1, "a.txt"), self.lane(2, "RED.txt"), self.lane(3, "b.txt")
        r = self.train(a, bad, b, env={"FM_POSTMERGE_CMD": "true", "FOREMAN_NO_BACKGROUND": "1"})
        out = r.stdout + r.stderr
        for name in ("a.txt", "b.txt"):
            self.assertTrue(os.path.exists(os.path.join(self.repo, name)), f"{name} landed: {out}")
        self.assertFalse(os.path.exists(os.path.join(self.repo, "RED.txt")), "the culprit is not landed")
        self.assertRegex(out, rf"left out {bad}: .*test_docs")
        self.assertNotRegex(out, rf"left out ({a}|{b})")
        with open(count) as f:  # a red run reruns its failures: so count the judged trees
            trees = {line.split(" ", 1)[1] for line in f.read().splitlines()}
        # the batch, the base (T-5805: once, for the red), then the bisect: 6 trees at most, never one twice
        self.assertLessEqual(len(trees), 6, "a bisect of three lanes judges a few trees, each once: " + str(sorted(trees)))

    def test_live_shape_pair_parks_both_lanes_of_a_red_that_needs_them_together_and_lands_the_third(self):
        # T-5806: no lane is red alone: a lane-by-lane check would land both and turn trunk red
        self.shape_trunk("os.path.exists('P1.txt') and os.path.exists('P2.txt')")
        p1, c3, p2 = self.lane(1, "P1.txt"), self.lane(2, "c.txt"), self.lane(3, "P2.txt")
        r = self.train(p1, c3, p2, env={"FM_POSTMERGE_CMD": "true", "FOREMAN_NO_BACKGROUND": "1"})
        out = r.stdout + r.stderr
        self.assertTrue(os.path.exists(os.path.join(self.repo, "c.txt")), out)
        for name in ("P1.txt", "P2.txt"):
            self.assertFalse(os.path.exists(os.path.join(self.repo, name)), f"{name} parked: {out}")
        self.assertRegex(out, rf"left out {p1}: .*test_docs")
        self.assertRegex(out, rf"left out {p2}: .*test_docs")
        self.assertNotRegex(out, rf"left out {c3}")

    def test_shape_cull_tries_the_lanes_the_red_classes_name_first_and_stops_on_a_cut(self):
        # T-5806: the culprit-first split costs one run beyond the red one, and the batch's red is run once more (a rerun)
        from sampo import train
        red = lambda *names: train.ShapeVerdict("red", "test_docs") if set(names) & {"b"} else \
            train.ShapeVerdict("green", "")  # noqa: E731
        seen = []
        got = train._shape_cull(["a", "b", "c"], lambda s, **kw: seen.append(list(s)) or red(*s), suspects={"b"})
        self.assertEqual(list(got), ["b"])
        self.assertEqual(seen, [["a", "c"], ["a", "b", "c"], ["a", "b", "c"]],
                         "the rest of the batch first, then the suspect on it, then the batch's red once more")
        self.assertIn("test_docs", got["b"][0])
        self.assertEqual(got["b"][1], "test_docs")
        cut = lambda s, **kw: train.ShapeVerdict("cut", "")  # noqa: E731
        with self.assertRaises(c.PolicyError):
            train._shape_cull(["a", "b"], cut)

    def test_shape_cull_lands_a_batch_whose_red_no_prefix_reproduces_and_a_rerun_clears(self):
        # T-5806 review: a transient red on the batch run (every prefix green) parked the last two lanes
        from sampo import train
        calls = []

        def shape(sel, fresh=False):
            calls.append((list(sel), fresh))
            return train.ShapeVerdict("red", "test_docs") if len(sel) == 2 and not fresh else train.ShapeVerdict("green")
        self.assertEqual(train._shape_cull(["a", "b"], shape), {}, "nothing parked: the batch is green on its rerun")
        self.assertEqual(calls[-1], (["a", "b"], True), "the rerun is a fresh run, not the cached verdict")
        whole = lambda s, fresh=False: train.ShapeVerdict("green" if fresh or len(s) < 3 else "red", "test_docs")  # noqa: E731
        self.assertEqual(train._shape_cull(["a", "b", "c"], whole), {}, "three lanes, a transient red on the whole batch")
        self.assertEqual(train._shape_cull(["a", "b", "c"], whole, suspects={"c"}), {}, "and with a suspect named")

    def test_shape_cull_records_each_parked_lanes_own_classes(self):
        # T-5806 review: the lane_train_red event named the batch's classes for every parked lane
        from sampo import train
        cls = {"a": "test_x", "c": "test_y"}

        def shape(sel, fresh=False):
            names = [cls[i] for i in sel if i in cls]
            return train.ShapeVerdict("red", ", ".join(names)) if names else train.ShapeVerdict("green")
        got = train._shape_cull(["a", "b", "c"], shape)
        self.assertEqual({i: w[1] for i, w in got.items()}, {"a": "test_x", "c": "test_y"})

    def test_shape_cull_caps_the_fresh_seconds_it_spends_and_counts_no_cache_hit(self):
        # T-5806 review: 24 runs of a 1800 s wall held the train lock 12 h: the cap is summed fresh seconds
        from sampo import train
        every = lambda s, fresh=False: train.ShapeVerdict("red", "test_docs", secs=100.0)  # noqa: E731
        with self.assertRaises(c.PolicyError) as e:
            train._shape_cull(["a", "b", "c", "d", "e"], every, budget=250)
        self.assertRegex(str(e.exception), r"no lane parked")
        self.assertIn("250", str(e.exception))
        cached = lambda s, fresh=False: train.ShapeVerdict("red" if "f" in s else "green", "test_docs")  # noqa: E731
        got = train._shape_cull(list("abcdef"), cached, budget=1)
        self.assertEqual(list(got), ["f"], "runs from the cache cost no seconds")

    def narrow_trunk(self):
        """T-5853: a live project whose stand-in shape runner logs the classes each run was given (one line a run; a run
        naming `module::Class` runs only those) and is red on up to three classes: test_docs.Cls (RED.txt is in the tree),
        test_run.Other (OTH.txt, RED.txt not: a red the first run masks) and test_run.Wide (W.txt, neither of the others:
        again masked, and only a whole run sees it). Returns the log's path."""
        self.fm("lane", "mode", "live")
        log = os.path.join(self.tmp, "shape-args")
        os.makedirs(os.path.join(self.repo, "plugin", "tests"))
        with open(os.path.join(self.repo, "plugin", "tests", "run.py"), "w") as f:
            f.write("import os, sys\n"
                    "args = sys.argv[1:]\n"
                    "if '--last' in args:\n    sys.exit(3)\n"
                    f"open({log!r}, 'a').write(' '.join(args) + '\\n')\n"
                    "want = lambda k: not any('::' in a for a in args) or k.replace('.', '::') in args\n"
                    "has = os.path.exists\n"
                    "red = [k for k, on in (('test_docs.Cls', has('RED.txt')),\n"
                    "                       ('test_run.Other', has('OTH.txt') and not has('RED.txt')),\n"
                    "                       ('test_run.Wide', has('W.txt') and not has('RED.txt') and not has('OTH.txt'))) "
                    "if on and want(k)]\n"
                    "for k in red:\n    print('  failed class: ' + k)\n"
                    "sys.exit(1 if red else 0)\n")
        self.git("add", "plugin")
        self.git("commit", "-qm", "a Sampo tree with its runner")
        return log

    def shape_runs(self, log):
        with open(log) as f:
            return [line.split() for line in f.read().splitlines()]

    def test_live_shape_cull_runs_only_the_red_classes_on_each_subset_tree_and_the_kept_build_once_in_full(self):
        # T-5853: 11 subset trees at about 4 min each held a train for 40 min; the red classes alone run in seconds
        log = self.narrow_trunk()
        full = list(c.repo_shape_modules())
        ids = [self.lane(1, "a.txt"), self.lane(2, "RED.txt"), self.lane(3, "b.txt"), self.lane(4, "d.txt")]
        r = self.train(*ids, env={"FM_POSTMERGE_CMD": "true", "FOREMAN_NO_BACKGROUND": "1"})
        out = r.stdout + r.stderr
        for name in ("a.txt", "b.txt", "d.txt"):
            self.assertTrue(os.path.exists(os.path.join(self.repo, name)), f"{name} landed: {out}")
        self.assertFalse(os.path.exists(os.path.join(self.repo, "RED.txt")), "the culprit is not landed")
        runs = self.shape_runs(log)
        narrow = [x for x in runs if x != full]
        self.assertTrue(narrow, f"the cull ran subset trees: {runs}")
        self.assertTrue(all(x == ["test_docs::Cls"] for x in narrow), f"a subset tree runs only the red class: {narrow}")
        self.assertEqual(len(runs) - len(narrow), 3, f"whole runs: the batch, the base, the kept build once: {runs}")

    def test_live_shape_confirm_a_class_red_only_on_the_kept_builds_full_run_gets_one_more_split_on_it(self):
        # T-5853: the narrowed subsets never ran test_run.Other: the kept build's whole run does, and splits it once
        log = self.narrow_trunk()
        full = list(c.repo_shape_modules())
        ids = [self.lane(1, "a.txt"), self.lane(2, "RED.txt"), self.lane(3, "OTH.txt"), self.lane(4, "d.txt")]
        r = self.train(*ids, env={"FM_POSTMERGE_CMD": "true", "FOREMAN_NO_BACKGROUND": "1"})
        out = r.stdout + r.stderr
        for name in ("a.txt", "d.txt"):
            self.assertTrue(os.path.exists(os.path.join(self.repo, name)), f"{name} landed: {out}")
        for name in ("RED.txt", "OTH.txt"):
            self.assertFalse(os.path.exists(os.path.join(self.repo, name)), f"{name} parked: {out}")
        self.assertRegex(out, rf"left out {ids[1]}: .*test_docs.Cls")
        self.assertRegex(out, rf"left out {ids[2]}: .*test_run.Other")
        runs = self.shape_runs(log)
        self.assertEqual({tuple(x) for x in runs if x != full}, {("test_docs::Cls",), ("test_run::Other",)},
                         f"each split narrows to its own classes: {runs}")
        self.assertEqual(runs.count(full), 4, f"whole runs: the batch, the base, the kept build of each split: {runs}")

    def test_live_shape_cull_all_red_a_class_red_in_every_subset_tree_parks_no_lane_and_the_land_says_so(self):
        # T-5880: 16 of 16 lanes parked at 10:05 for a class that flaked under load: the base is run on it once, alone
        self.fm("lane", "mode", "live")
        log = os.path.join(self.tmp, "shape-args")
        os.makedirs(os.path.join(self.repo, "plugin", "tests"))
        with open(os.path.join(self.repo, "plugin", "tests", "run.py"), "w") as f:
            f.write("import os, sys\n"
                    "args = sys.argv[1:]\n"
                    "if '--last' in args:\n    sys.exit(3)\n"
                    f"open({log!r}, 'a').write(' '.join(args) + '\\n')\n"
                    "lanes = any(os.path.exists(n) for n in ('a.txt', 'b.txt', 'd.txt'))\n"
                    # the base is green on its whole run (the first) and red on the narrowed rerun
                    "red = lanes or any('::' in a for a in args)\n"
                    "if red:\n    print('  failed class: test_docs.Cls')\n"
                    "sys.exit(1 if red else 0)\n")
        self.git("add", "plugin")
        self.git("commit", "-qm", "a Sampo tree with its runner")
        ids = [self.lane(1, "a.txt"), self.lane(2, "b.txt"), self.lane(3, "d.txt")]
        r = self.train(*ids, env={"FM_POSTMERGE_CMD": "true", "FOREMAN_NO_BACKGROUND": "1"})
        out = r.stdout + r.stderr
        for name in ("a.txt", "b.txt", "d.txt"):
            self.assertTrue(os.path.exists(os.path.join(self.repo, name)), f"{name} landed: {out}")
        self.assertNotRegex(out, r"left out ")
        self.assertIn("repo-shape class test_docs.Cls red in all 3 subset trees: base rerun red, no lane parked for it", out)
        for tid in ids:
            self.assertNotIn("left out of a merge train", json.dumps(json.loads(
                self.fm("task", "show", tid, "--json").stdout).get("log", "")), f"{tid} is not parked")

    def test_live_shape_confirm_a_second_new_red_on_the_kept_build_stops_the_land_with_nothing_parked(self):
        # T-5853: one more split, then the land stops: a lane parked in the first round is not parked for a refused land
        self.narrow_trunk()
        ids = [self.lane(1, "a.txt"), self.lane(2, "RED.txt"), self.lane(3, "OTH.txt"), self.lane(4, "W.txt")]
        head = self.git("rev-parse", "HEAD")
        r = self.train(*ids, check=False, env={"FM_POSTMERGE_CMD": "true", "FOREMAN_NO_BACKGROUND": "1"})
        out = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 0, out)
        self.assertEqual(self.git("rev-parse", "HEAD"), head, "nothing landed")
        self.assertIn("test_run.Wide", out)
        self.assertIn("nothing landed", out)
        self.assertNotRegex(out, r"left out ")
        for tid in ids:
            self.assertNotIn("left out of a merge train", json.dumps(json.loads(
                self.fm("task", "show", tid, "--json").stdout).get("log", "")), f"{tid} is not parked")

    def test_shape_run_names_the_classes_it_is_given_and_a_class_the_tree_lacks_is_not_red(self):
        # T-5853: `module.Class` runs as run.py's `module::Class`; a name no test matches fails the whole run there
        from sampo import train
        cmds = []

        def run(path, cmd, wall, **kw):
            cmds.append(cmd)
            if "test_gone::Cls" in cmd:
                return 1, "run.py: no test matches test_gone::Cls\n"
            return 0, ""
        with mock.patch.object(c, "run_command", side_effect=run), mock.patch.object(c, "run_record", return_value=None):
            self.assertEqual(train._shape_run("/t", 5, "test_docs.Cls, test_run.Other"), ("green", ""))
            self.assertTrue(cmds[0].endswith("run.py test_docs::Cls test_run::Other"), cmds[0])
            cmds.clear()
            self.assertEqual(train._shape_run("/t", 5, "test_gone.Cls, test_run.Other"), ("green", ""))
            self.assertEqual(len(cmds), 2, "the name no test matches is dropped and the rest run")
            self.assertTrue(cmds[1].endswith("run.py test_run::Other"), cmds[1])
            cmds.clear()
            self.assertEqual(train._shape_run("/t", 5, "test_gone.Cls"), ("green", ""), "a class the tree lacks: no red")
            self.assertEqual(len(cmds), 1)
            cmds.clear()
            train._shape_run("/t", 5, "bad name; rm -rf x, a.b.c")
            self.assertEqual(len(cmds), 1)
            self.assertIn(" ".join(c.repo_shape_modules()), cmds[0], "a name that is no class id runs the whole set")
            self.assertNotIn("rm", cmds[0])

    def test_shape_run_a_red_class_that_prints_the_no_match_phrase_stays_red(self):
        # T-5853 review: the phrase is only run.py's own early exit (no test ran); a class's output may echo it
        from sampo import train
        cmds = []

        def run(path, cmd, wall, **kw):
            cmds.append(cmd)
            return 1, ("run.py: no test matches test_run::Other\n.F\nRan 3 tests in 0.1s\nFAILED (failures=1)\n"
                       "  failed class: test_run.Other\n")
        with mock.patch.object(c, "run_command", side_effect=run), mock.patch.object(c, "run_record", return_value=None):
            self.assertEqual(train._shape_run("/t", 5, "test_run.Other"), ("red", "test_run.Other"))
        self.assertEqual(len(cmds), 1, "the name was not dropped")

    def test_shape_run_a_module_that_fails_to_import_is_run_bare_not_dropped_as_green(self):
        # T-5853 review: run.py names a failed import `unittest.loader._FailedTest.<module>`, so `module::Class` matches
        # nothing there: the module exists in the tree, so it runs whole and its import failure is the red
        from sampo import train
        tree = os.path.join(self.tmp, "tree")
        os.makedirs(os.path.join(tree, "plugin", "tests"))
        with open(os.path.join(tree, "plugin", "tests", "test_bad.py"), "w") as f:
            f.write("import nothing_here\n")
        cmds = []

        def run(path, cmd, wall, **kw):
            cmds.append(cmd)
            if "::" in cmd:
                return 1, "run.py: no test matches " + ", ".join(w for w in cmd.split() if "::" in w) + "\n"
            return 1, "  failed class: unittest.loader._FailedTest\n"
        with mock.patch.object(c, "run_command", side_effect=run), mock.patch.object(c, "run_record", return_value=None):
            got = train._shape_run(tree, 5, "test_bad.Cls, test_gone.Cls")
        self.assertEqual(got[0], "red", got)
        self.assertEqual(len(cmds), 2, cmds)
        self.assertTrue(cmds[1].endswith("run.py test_bad"), "the bare module stays, the class of no module is dropped")

    def test_live_shape_refuses_a_batch_when_the_base_has_no_shape_verdict_and_parks_no_lane(self):
        # T-5806 review: a base cut says so (no lane is blamed for a class the base may carry), and names no wall it was not cut at
        self.shape_trunk("os.path.exists('RED.txt') or (not os.path.exists('a.txt') and sys.exit(1))")  # the bare base: no class named
        a, bad = self.lane(1, "a.txt"), self.lane(2, "RED.txt")
        head = self.git("rev-parse", "HEAD")
        r = self.train(a, bad, check=False, env={"FM_POSTMERGE_CMD": "true", "FOREMAN_NO_BACKGROUND": "1"})
        out = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 0, out)
        self.assertEqual(self.git("rev-parse", "HEAD"), head, "nothing landed")
        self.assertRegex(out, r"base .*no verdict")
        self.assertNotRegex(out, rf"left out {bad}")

    def test_live_mode_is_a_project_switch_and_full_gate_forces_gated(self):
        from sampo import train
        p = c.find_project(self.repo)
        self.assertFalse(train.live_mode(p), "a project is gated until it says live")
        self.assertIn("gated", self.fm("lane", "mode").stdout)
        self.fm("lane", "mode", "live")
        self.assertTrue(train.live_mode(p))
        with mock.patch.dict(os.environ, {"FOREMAN_FULL_GATE": "1"}):
            self.assertFalse(train.live_mode(p))
        self.assertNotEqual(self.fm("lane", "mode", "fast", check=False).returncode, 0)
        self.fm("lane", "mode", "gated")
        self.assertFalse(train.live_mode(p))

    def test_live_mode_behind_verify_runs_the_projects_gates_when_the_repo_has_no_sampo_runner(self):
        from sampo import lanes
        p = c.find_project(self.repo)
        c.update_meta(p, checks=["cargo fmt --check", "cargo test"])
        with mock.patch.dict(os.environ, {"FM_POSTMERGE_CMD": ""}):
            self.assertEqual(lanes.pm_cmd(self.repo, p=p), "", "a gated project with no runner lands unverified as before")
            self.fm("lane", "mode", "live")
            self.assertEqual(lanes.pm_cmd(self.repo, p=p), "( cargo fmt --check ) && ( cargo test )")
            self.assertEqual(lanes.pm_cmd(self.repo), "", "no project, no fallback")

    def test_live_selfgate_times_each_lane_only_after_the_merged_batch_was_held(self):
        # T-5446: the paired hook bench ran 5-12 min per lane; a live train times the merged batch once
        from sampo import lanes, selfgate, train
        p = c.find_project(self.repo)
        b = mock.Mock(id="T-1", meta={})
        seen = []
        with mock.patch.object(lanes, "land_holds", lambda *a, **k: seen.append(k.get("timing")) or ""):
            train._held(p, b, self.repo, "HEAD")
            self.fm("lane", "mode", "live")
            train._held(p, b, self.repo, "HEAD")
            base = self.git("rev-parse", "HEAD").strip()
            with mock.patch.object(selfgate, "gate", lambda *a, **k: ["hook Stop p95 100.0 -> 140.0 ms"]):
                self.assertTrue(train._live_timing(p, self.repo, self.repo, base, ["plugin/lib/sampo/x.py"]))  # T-5935: a benched file
            self.assertTrue(c.read_meta(p).get(train.LIVE_TIMING_HOLD))
            train._held(p, b, self.repo, "HEAD")
            with mock.patch.object(selfgate, "gate", lambda *a, **k: []):
                self.assertEqual(train._live_timing(p, self.repo, self.repo, base, ["plugin/lib/sampo/x.py"]), [])
            self.assertFalse(c.read_meta(p).get(train.LIVE_TIMING_HOLD))
            train._held(p, b, self.repo, "HEAD")
        self.assertEqual(seen, [True, False, True, False],
                         "gated times each lane; live times none until a merged hold, then each until one lands")

    def test_holds_run_four_at_once_and_keep_the_candidate_order(self):
        # T-5465: 55 lanes' holds one by one took about 18 min at about 20 s a lane
        import threading
        import time as _time
        from sampo import train
        p = c.find_project(self.repo)
        live, peak, lock = [0], [0], threading.Lock()

        def held(p_, b, main, tip):
            with lock:
                live[0] += 1
                peak[0] = max(peak[0], live[0])
            _time.sleep(0.2)
            with lock:
                live[0] -= 1
            return f"held {b.id}" if b.id in ("T-2", "T-5") else ""
        lanes_tips = [(mock.Mock(id=f"T-{i}"), f"tip{i}") for i in range(1, 9)]
        with mock.patch.object(train, "_held", held):
            t0 = _time.monotonic()
            got = train._holds(p, self.repo, lanes_tips)
            took = _time.monotonic() - t0
        self.assertEqual(got, ["", "held T-2", "", "", "held T-5", "", "", ""])
        self.assertEqual(peak[0], 4)
        self.assertLess(took, 1.2, "8 holds of 0.2 s, 4 at a time: about 0.4 s, not 1.6")
        with mock.patch.object(train, "_held", side_effect=c.PolicyError("boom")), self.assertRaises(c.PolicyError):
            train._holds(p, self.repo, lanes_tips[:3])

    def test_live_mode_holds_only_a_red_check(self):
        from sampo import train
        p = c.find_project(self.repo)
        b = mock.Mock(meta={"accepted_tip": "sha"})  # T-5968: reviewed at that tip
        with mock.patch.object(train, "live_mode", return_value=True):
            for line, held in (("held: check queued behind 2 running at most (pid 1)", False),
                               ("held: check running (pid 1); it joins the land when green", False),
                               ("held: check red: test_x.X; the checker's code has changed since it ran: accept", False),
                               ("held: check red: test_x.X; left out, no retry until the tip moves", True)):
                with mock.patch.object(train, "_check_hold", return_value=line):
                    self.assertEqual(bool(train._lane_hold(p, b, "sha", self.repo)), held, line)
            with mock.patch.object(train, "_check_hold", return_value=None), \
                    mock.patch.object(train, "_unchecked", return_value="held: no passing affected check"):
                self.assertIsNone(train._lane_hold(p, b, "sha", self.repo), "no check on record: the behind suite judges it")

    def test_gate_runs_hold_tests_to_the_projects_cpu_baseline(self):  # T-3757
        seen = os.path.join(self.tmp, "cpu-env")
        self.fm("check", "add", f'echo "$SAMPO_CPU_BASE $SAMPO_CPU_GATE" >> {seen}')
        self.train(self.lane(1, "a.txt"))
        base, gate = open(seen).read().split()[:2]
        self.assertTrue(base.endswith(os.sep + "test-cpu.json") and os.path.isabs(base), base)
        self.assertEqual(gate, "1")

    def test_a_breaking_lane_is_named_and_left_out_within_the_gate_budget(self):
        ids = [self.lane(1, "a.txt"), self.lane(2, "b.txt"), self.lane(3, "c.txt"), self.lane(4, "BREAK")]
        r = self.train(*ids)
        self.assertIn(ids[3], r.stdout)
        self.assertLessEqual(self.gate_runs(), 1 + math.ceil(math.log2(4)), r.stdout)
        for name in ("a.txt", "b.txt", "c.txt"):
            self.assertTrue(os.path.exists(os.path.join(self.repo, name)), name)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "BREAK")))
        for tid in ids[:3]:
            self.assertTrue(self.meta(tid)["lane_stop"].startswith("merged"), self.meta(tid))
        self.assertFalse(self.meta(ids[3]).get("lane_stop", "").startswith("merged"))
        self.assertNotIn("train", self.git("worktree", "list"), "the scratch worktree is gone")

    def test_a_middle_culprit_costs_one_regate_more(self):
        ids = [self.lane(1, "a.txt"), self.lane(2, "BREAK"), self.lane(3, "c.txt"), self.lane(4, "d.txt")]
        r = self.train(*ids)
        self.assertLessEqual(self.gate_runs(), 2 + math.ceil(math.log2(4)), r.stdout)
        self.assertTrue(os.path.exists(os.path.join(self.repo, "d.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.repo, "BREAK")))

    def test_check_reuses_the_train_pass_when_only_changelog_and_mirror_changed(self):
        self.train(self.lane(1, "a.txt"), self.lane(2, "b.txt"))
        self.assertEqual(self.gate_runs(), 1)
        with open(os.path.join(self.repo, "CHANGELOG.md"), "w") as f:
            f.write("- a line the close adds\n")
        os.makedirs(os.path.join(self.repo, ".sampo"), exist_ok=True)
        with open(os.path.join(self.repo, ".sampo", "STATE.md"), "w") as f:
            f.write("mirror\n")
        self.assertIn("cached", self.fm("check").stdout)
        self.assertEqual(self.gate_runs(), 1, "no gate ran")
        with open(os.path.join(self.repo, "a.txt"), "w") as f:
            f.write("changed code\n")
        self.fm("check")
        self.assertEqual(self.gate_runs(), 2, "a code change reruns the gates")

    def test_a_conflicting_lane_is_skipped_with_its_files_and_main_only_fast_forwards(self):
        a, b, d = self.lane(1, "same.txt", "one\n"), self.lane(2, "same.txt", "two\n"), self.lane(3, "d.txt")
        r = self.train(a, b, d)
        self.assertIn(b, r.stdout)
        self.assertIn("same.txt", r.stdout)
        self.assertEqual(open(os.path.join(self.repo, "same.txt")).read(), "one\n")
        self.assertTrue(os.path.exists(os.path.join(self.repo, "d.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".git", "MERGE_HEAD")))
        self.assertEqual(self.git("reflog", "-n", "1", "--format=%gs").split()[0], "merge", "one fast-forward")
        self.assertFalse(self.meta(b).get("lane_stop", "").startswith("merged"))

    def test_a_local_change_to_a_merged_file_is_set_aside_and_put_back(self):  # T-4007
        lines = "".join(f"{i}\n" for i in range(10))
        with open(os.path.join(self.repo, "log.txt"), "w") as f:
            f.write(lines)
        self.git("add", "log.txt")
        self.git("commit", "-qm", "log")
        a = self.lane(1, "log.txt", lines.replace("0\n", "zero\n", 1))
        with open(os.path.join(self.repo, "log.txt"), "w") as f:  # someone's own stash, which the train never touches
            f.write("theirs\n")
        self.git("stash", "push", "-q", "-m", "someone else's")
        with open(os.path.join(self.repo, "log.txt"), "w") as f:  # uncommitted, far from the lane's line
            f.write(lines.replace("9\n", "nine\n"))
        r = self.train(a)
        self.assertIn(a, r.stdout)
        self.assertEqual(open(os.path.join(self.repo, "log.txt")).read().split()[::9], ["zero", "nine"])
        self.assertEqual(self.git("diff", "--name-only"), "log.txt", "the local change is back, still uncommitted")
        self.assertEqual(self.git("stash", "list", "--format=%gs"), "On main: someone else's")
        self.assertTrue(self.meta(a)["lane_stop"].startswith("merged"))

    def test_a_local_change_the_train_conflicts_with_is_kept_as_a_named_stash(self):  # T-4007
        with open(os.path.join(self.repo, "same.txt"), "w") as f:
            f.write("one\n")
        self.git("add", "same.txt")
        self.git("commit", "-qm", "same")
        a = self.lane(1, "same.txt", "two\n")
        with open(os.path.join(self.repo, "same.txt"), "w") as f:
            f.write("mine\n")
        r = self.train(a)
        self.assertTrue(self.meta(a)["lane_stop"].startswith("merged"), r.stdout + r.stderr)
        self.assertEqual(open(os.path.join(self.repo, "same.txt")).read(), "two\n", "the landed file, no markers")
        self.assertEqual(self.git("ls-files", "-u"), "")
        self.assertEqual(self.git("status", "--porcelain", "--untracked-files=no"), "")
        self.assertIn("sampo train", self.git("stash", "list"))
        self.assertIn("mine", self.git("stash", "show", "-p"))
        self.assertIn("kept as stash", r.stdout)
        self.assertIn("same.txt", r.stdout)

    def test_a_lane_conflicting_with_the_base_is_left_out_before_any_gate_run(self):
        """T-3474: git merge-tree names a conflict with the base in milliseconds, not as 'an earlier lane'."""
        a = self.lane(1, "same.txt", "lane\n")
        with open(os.path.join(self.repo, "same.txt"), "w") as f:
            f.write("base\n")
        self.git("add", "same.txt")
        self.git("commit", "-qm", "base moves")
        r = self.train(a, check=False)
        self.assertIn("conflicts with the base: same.txt", r.stdout + r.stderr)
        self.assertEqual(self.gate_runs(), 0, "no gate run on a lane merge-tree rules out")

    MAP = "docs/map/ops.md"
    ENTRY = "- `ops.py`: does the work.\n"

    def map_base(self, name=MAP, text=ENTRY):
        """T-4375: a committed map entry (one long line) two lanes can each add a sentence to."""
        os.makedirs(os.path.dirname(os.path.join(self.repo, name)), exist_ok=True)
        with open(os.path.join(self.repo, name), "w") as f:
            f.write(text)
        self.git("add", name)
        self.git("commit", "-qm", "map entry")

    def read(self, name):
        with open(os.path.join(self.repo, name)) as f:
            return f.read()

    def followups(self):
        p = c.find_project(self.repo)
        return [b for b in c.load_briefs(p) if b.meta.get("source") == "followup"]

    TESTF = "plugin/tests/test_thing.py"
    TBASE = "import unittest\n\n\nclass Old(unittest.TestCase):\n    def test_old(self):\n        self.assertTrue(True)\n"

    @staticmethod
    def tcls(name, body="True"):
        return f"\n\nclass {name}(unittest.TestCase):\n    def test_it(self):\n        self.assertTrue({body})\n"

    def tbase(self, name=TESTF, text=TBASE):
        """T-5824: a committed test file two lanes can each append a class to."""
        self.map_base(name, text)

    def test_insertion_union_lands_two_lanes_that_each_append_a_class_at_one_spot(self):
        """T-5824: the 2026-11-22 shape (T-5497, T-5725, T-5793): both lanes only added lines at the end of one test
        file, so the train keeps ours then theirs and the gate judges; no park, no builder round."""
        self.tbase()
        a = self.lane(1, self.TESTF, self.TBASE + self.tcls("LaneOne"))
        b = self.lane(2, self.TESTF, self.TBASE + self.tcls("LaneTwo"))
        r = self.train(a, b)
        self.assertNotIn("left out", r.stdout)
        for tid in (a, b):
            self.assertTrue(self.meta(tid)["lane_stop"].startswith("merged"), r.stdout)
        text = self.read(self.TESTF)
        self.assertEqual(text, self.TBASE + self.tcls("LaneOne") + self.tcls("LaneTwo"))
        self.assertIn(f"merged {b} keeping both sides' added lines in {self.TESTF}", r.stdout)
        self.assertEqual(self.git("ls-files", "-u"), "")
        self.assertEqual(self.followups(), [], "a union of added lines needs no read")

    def test_insertion_union_parks_a_lane_that_edits_a_line_the_other_changed(self):
        """T-5824: a hunk with base lines in it is a disagreement, not an addition: the later lane parks as before."""
        self.tbase()
        a = self.lane(1, self.TESTF, self.TBASE.replace("assertTrue(True)", "assertTrue(1)"))
        b = self.lane(2, self.TESTF, self.TBASE.replace("assertTrue(True)", "assertTrue(2)"))
        r = self.train(a, b, check=False)
        self.assertIn(f"left out {b}: conflicts with an earlier lane in the train: {self.TESTF}", r.stdout)
        self.assertTrue(self.meta(a)["lane_stop"].startswith("merged"), r.stdout)
        self.assertIn("assertTrue(1)", self.read(self.TESTF))

    def test_insertion_union_parks_an_insertion_beside_a_changed_line(self):
        """T-5824: one hunk that both added a block and changed a base line stays a conflict."""
        self.tbase()
        a = self.lane(1, self.TESTF, self.TBASE.replace("assertTrue(True)", "assertTrue(1)") + self.tcls("LaneOne", "'one'"))
        b = self.lane(2, self.TESTF, self.TBASE.replace("assertTrue(True)", "assertTrue(2)") + self.tcls("LaneTwo", "'two'"))
        r = self.train(a, b, check=False)
        self.assertIn(f"left out {b}: conflicts with an earlier lane", r.stdout)

    def test_insertion_union_parks_two_appended_classes_with_one_name(self):
        """T-5824: the second class would shadow the first, so the union is refused and the lane parks."""
        self.tbase()
        a = self.lane(1, self.TESTF, self.TBASE + self.tcls("Same", "1"))
        b = self.lane(2, self.TESTF, self.TBASE + self.tcls("Same", "2"))
        r = self.train(a, b, check=False)
        self.assertIn(f"left out {b}: conflicts with an earlier lane in the train: {self.TESTF}", r.stdout)
        self.assertEqual(self.read(self.TESTF).count("class Same"), 1)

    def test_insertion_union_parks_a_conflict_in_a_file_that_is_not_python(self):
        """T-5824, T-5937: the same append-append clash in a non-python file parks: only .py and map files are unioned."""
        code = "plugin/lib/thing.txt"
        self.map_base(code, "x = 1\n")
        a = self.lane(1, code, "x = 1\ny = 2\n")
        b = self.lane(2, code, "x = 1\nz = 3\n")
        r = self.train(a, b, check=False)
        self.assertIn(f"left out {b}: conflicts with an earlier lane in the train: {code}", r.stdout)

    def test_insertion_union_parks_a_class_beside_a_method_added_to_the_last_class(self):
        """T-5824 review: one lane appends a class at indent 0, the other a method at indent 4 to the last class; both
        land at the end of the file, and the union would make the method a member of the first lane's class."""
        self.tbase()
        meth = "\n    def test_two(self):\n        self.assertTrue('two')\n"
        a = self.lane(1, self.TESTF, self.TBASE + self.tcls("LaneOne", "'one'"))
        b = self.lane(2, self.TESTF, self.TBASE + meth)
        r = self.train(a, b, check=False)
        self.assertIn(f"left out {b}: conflicts with an earlier lane in the train: {self.TESTF}", r.stdout)
        self.assertNotIn("test_two", self.read(self.TESTF))

    def test_insertion_union_keeps_two_methods_added_at_one_indent_to_the_last_class(self):
        """T-5824 review: the indent rule parks a mismatch, not two blocks that start at the same indent."""
        self.tbase()
        m1 = "\n    def test_one(self):\n        self.assertTrue('one')\n"
        m2 = "\n    def test_two(self):\n        self.assertTrue('two')\n"
        a = self.lane(1, self.TESTF, self.TBASE + m1)
        b = self.lane(2, self.TESTF, self.TBASE + m2)
        r = self.train(a, b)
        self.assertNotIn("left out", r.stdout)
        self.assertEqual(self.read(self.TESTF), self.TBASE + m1 + m2)

    def test_insertion_union_parks_what_it_cannot_prove_safe(self):
        """T-5824 review: a merge-file that times out, a name bound twice (a top-level assignment, an import, a tab-indented
        def), and a union that does not parse each return None, never a raise."""
        from sampo import train
        base = b"import unittest\n"
        two = lambda x, y: (base + x, base, base + y)  # noqa: E731
        self.assertEqual(train._insertion_union(*two(b"A = 1\n", b"B = 2\n")), b"import unittest\nA = 1\nB = 2\n")
        for name, pair in {
                "assignment": two(b"FIXTURE = 1\n", b"FIXTURE = 2\n"),
                "import": two(b"from os import path as p\n", b"from sys import path as p\n"),
                "tab def": (b"class C:\n\tdef a(self): pass\n\tdef f(self): pass\n", b"class C:\n\tdef a(self): pass\n",
                            b"class C:\n\tdef a(self): pass\n\tdef f(self): return 2\n"),
                "annotated": two(b"X: int = 1\n", b"X: int = 2\n"),
                "does not parse": two(b"def broken(:\n", b"Z = 1\n")}.items():
            self.assertIsNone(train._insertion_union(*pair), name)
        with mock.patch.object(train.subprocess, "run", side_effect=subprocess.TimeoutExpired("git", 60)):
            self.assertIsNone(train._insertion_union(*two(b"A = 1\n", b"B = 2\n")))

    def test_insertion_union_parks_when_one_test_file_is_unioned_and_another_is_not(self):
        """T-5824: every conflicted file must be a pure-insertion test file, or the merge is aborted whole."""
        self.tbase()
        other = "plugin/tests/test_other.py"
        self.map_base(other, "a = 1\n")
        a = self.lane(1, self.TESTF, self.TBASE + self.tcls("LaneOne"))
        b = self.lane(2, self.TESTF, self.TBASE + self.tcls("LaneTwo"))
        with open(os.path.join(self.tmp, "lane1", other), "w") as f:
            f.write("a = 2\n")
        self.git("commit", "-qam", "edit other", cwd=os.path.join(self.tmp, "lane1"))
        with open(os.path.join(self.tmp, "lane2", other), "w") as f:
            f.write("a = 3\n")
        self.git("commit", "-qam", "edit other", cwd=os.path.join(self.tmp, "lane2"))
        r = self.train(a, b, check=False)
        self.assertIn(f"left out {b}: conflicts with an earlier lane in the train", r.stdout)
        self.assertEqual(self.git("ls-files", "-u"), "")

    def test_map_only_conflict_keeps_both(self):
        """T-4375: two lanes that each add a sentence to the same docs/map entry both land; T-5472: the key-aware map
        merge puts both sentences on the one entry line, so there is no copy to read and no follow-up."""
        self.map_base()
        a = self.lane(1, self.MAP, self.ENTRY.rstrip("\n") + " Lane one adds this.\n")
        b = self.lane(2, self.MAP, self.ENTRY.rstrip("\n") + " Lane two adds that.\n")
        r = self.train(a, b)
        self.assertNotIn("left out", r.stdout)
        for tid in (a, b):
            self.assertTrue(self.meta(tid)["lane_stop"].startswith("merged"), r.stdout)
        self.assertEqual(self.read(self.MAP), self.ENTRY.rstrip("\n") + " Lane one adds this. Lane two adds that.\n")
        self.assertNotIn("keeping both", r.stdout)
        self.assertEqual(self.followups(), [])
        self.assertEqual(self.git("ls-files", "-u"), "")
        self.assertEqual(self.git("status", "--porcelain", "--untracked-files=no"), "")

    def test_map_rewrite_on_both_sides_keeps_both_lines_and_says_so(self):
        """T-4375, T-5472: two lanes that rewrite the same words of one entry both land, both versions kept (the lane's
        after trunk's), and the train names the lane and file for a read."""
        self.map_base()
        a = self.lane(1, self.MAP, "- `ops.py`: Lane one's words.\n")
        b = self.lane(2, self.MAP, "- `ops.py`: Lane two's words.\n")
        r = self.train(a, b)
        self.assertNotIn("left out", r.stdout)
        self.assertEqual(self.read(self.MAP), "- `ops.py`: Lane one's words.\n- `ops.py`: Lane two's words.\n")
        self.assertIn(f"merged {b} keeping both sides' lines in {self.MAP}", r.stdout)
        self.assertNotIn(f"merged {a} keeping", r.stdout, "the first lane merged cleanly")
        self.assertEqual(self.git("ls-files", "-u"), "")

    def test_a_lane_beside_a_master_line_main_repointed_keeps_no_stale_copy(self):
        """T-5472: the 2026-11-21 case: main repointed a module's MASTER.md line, the lane (made before) added its own
        module's line beside the old one. A union kept both copies; the train's map driver keeps main's line once."""
        self.map_base(".gitattributes", "MASTER.md merge=sampo-map\n")
        self.map_base("MASTER.md", "- `a.py` one [→ old](docs/map/old.md)\n- `b.py` two\n")
        a = self.lane(1, "MASTER.md", "- `a.py` one [→ old](docs/map/old.md)\n- `n.py` the lane's\n- `b.py` two\n")
        self.map_base("MASTER.md", "- `a.py` one [→ new](docs/map/new.md)\n- `b.py` two\n")
        r = self.train(a)
        self.assertTrue(self.meta(a)["lane_stop"].startswith("merged"), r.stdout)
        self.assertEqual(self.read("MASTER.md"), "- `a.py` one [→ new](docs/map/new.md)\n- `n.py` the lane's\n- `b.py` two\n")
        self.assertNotIn("keeping both", r.stdout)
        self.assertIn("mapmerge.py", self.git("config", "--get", "merge.sampo-map.driver"),
                      "the train wrote the driver its merges use")

    def test_map_only_conflict_with_the_base_keeps_both(self):
        """T-4375: the 2026-11-11 case: main moved and added a line to the same entry; the lane is not parked."""
        self.map_base()
        a = self.lane(1, self.MAP, self.ENTRY.rstrip("\n") + " Lane adds this.\n")
        with open(os.path.join(self.repo, self.MAP), "w") as f:
            f.write(self.ENTRY.rstrip("\n") + " Main adds that.\n")
        self.git("add", self.MAP)
        self.git("commit", "-qm", "main moves")
        r = self.train(a)
        self.assertNotIn("left out", r.stdout)
        self.assertTrue(self.meta(a)["lane_stop"].startswith("merged"), r.stdout)
        text = self.read(self.MAP)
        self.assertIn("Lane adds this.", text)
        self.assertIn("Main adds that.", text)
        self.assertNotRegex(text, r"(?m)^(<<<<<<<|=======|>>>>>>>)")
        self.assertEqual(text.count("`ops.py`"), 1, "one entry line carries both (T-5472)")

    def union_conflict(self, merged=True):
        """T-5836: a scratch repo whose merge of two lanes is conflicted in MAP alone (the key-aware union's case, both
        sides rewrote the entry), plus a folder outside it holding a file a write through a link would change.
        (the scratch tree, the outside file)."""
        wt = os.path.join(self.tmp, "union-wt")
        os.makedirs(wt)
        ident = ("-c", "user.name=t", "-c", "user.email=t@localhost")

        def git(*a):
            return subprocess.run(["git", "-C", wt, *ident, *a], capture_output=True, text=True)
        git("init", "-q", "-b", "main")
        os.makedirs(os.path.join(wt, "docs", "map"))
        for branch, text in (("main", self.ENTRY), ("one", "- `ops.py`: one.\n"), ("two", "- `ops.py`: two.\n")):
            git(*(("checkout", "-q", "-b", branch, "main") if branch != "main" else ("checkout", "-q", "main")))
            with open(os.path.join(wt, self.MAP), "w") as f:
                f.write(text)
            git("add", self.MAP)
            git("commit", "-qm", branch)
        git("checkout", "-q", "one")
        if merged:
            self.assertEqual(git("merge", "--no-ff", "-m", "m", "two").returncode, 1)
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        target = os.path.join(outside, "ops.md")
        with open(target, "w") as f:
            f.write("outside\n")
        return wt, target

    def test_union_symlink_parent_folder_is_refused_and_the_target_unchanged(self):
        """T-5836: the leaf is no link but its folder is (docs/map -> a folder outside the scratch tree): a write
        through it changed a file outside. The union parks (None) and the file there stays as it was."""
        from sampo import train
        wt, target = self.union_conflict()
        shutil.rmtree(os.path.join(wt, "docs", "map"))
        os.symlink(os.path.dirname(target), os.path.join(wt, "docs", "map"))
        self.assertIsNone(train._union_map(wt, [self.MAP]))
        self.assertEqual(open(target).read(), "outside\n")

    def test_union_symlink_leaf_is_refused_and_the_target_unchanged(self):
        """T-5836: the conflicted map file itself replaced by a link to a file outside the scratch tree."""
        from sampo import train
        wt, target = self.union_conflict()
        os.remove(os.path.join(wt, self.MAP))
        os.symlink(target, os.path.join(wt, self.MAP))
        self.assertIsNone(train._union_map(wt, [self.MAP]))
        self.assertEqual(open(target).read(), "outside\n")

    def test_union_symlink_non_regular_target_is_refused(self):
        """T-5836: a folder where the file was parks the lane; it never raises out of the train's merge."""
        from sampo import train
        wt, _ = self.union_conflict()
        os.remove(os.path.join(wt, self.MAP))
        os.mkdir(os.path.join(wt, self.MAP))
        self.assertIsNone(train._union_map(wt, [self.MAP]))

    def test_union_symlink_write_helper_never_follows_a_link_swapped_in_after_the_check(self):
        """T-5836: the open itself refuses a link (O_NOFOLLOW), so a swap between check and write changes nothing."""
        from sampo import train
        wt, target = self.union_conflict()
        real = train._plain_file

        def swap(root, rel):
            got = real(root, rel)  # the check passes on the real file ...
            os.remove(os.path.join(root, rel))  # ... then a link takes its place
            os.symlink(target, os.path.join(root, rel))
            return got
        with mock.patch.object(train, "_plain_file", swap):
            self.assertFalse(train._write_plain(wt, self.MAP, b"changed\n"))
        self.assertEqual(open(target).read(), "outside\n")

    def test_union_symlink_hard_linked_target_is_refused_and_the_target_unchanged(self):
        """T-5836 review: the conflicted file made a hard link of a file outside the scratch tree: a write through it
        changes that file too, so it is refused."""
        from sampo import train
        wt, target = self.union_conflict()
        os.remove(os.path.join(wt, self.MAP))
        os.link(target, os.path.join(wt, self.MAP))
        self.assertIsNone(train._union_map(wt, [self.MAP]))
        self.assertEqual(open(target).read(), "outside\n")

    def test_union_symlink_a_hard_link_made_after_the_check_is_refused_at_the_write(self):
        """T-5836 review: the same inode gets a second link between check and open: only the opened file's link count
        sees it, and nothing is written."""
        from sampo import train
        wt, target = self.union_conflict()
        real = train._plain_file

        def link(root, rel):
            got = real(root, rel)
            os.link(os.path.join(root, rel), target + ".link")
            return got
        before = open(os.path.join(wt, self.MAP)).read()
        with mock.patch.object(train, "_plain_file", link):
            self.assertFalse(train._write_plain(wt, self.MAP, b"changed\n"))
        self.assertEqual(open(os.path.join(wt, self.MAP)).read(), before)

    def test_union_symlink_a_folder_swapped_for_a_link_after_the_check_is_refused_by_the_inode_compare(self):
        """T-5836 review: docs/map becomes a link to a folder outside once the check has passed. The leaf is no link,
        so O_NOFOLLOW lets the open through; the file it got is not the one checked, and nothing is written."""
        from sampo import train
        wt, target = self.union_conflict()
        real = train._plain_file

        def swap(root, rel):
            got = real(root, rel)
            shutil.rmtree(os.path.join(root, "docs", "map"))
            os.symlink(os.path.dirname(target), os.path.join(root, "docs", "map"))
            return got
        with mock.patch.object(train, "_plain_file", swap):
            self.assertFalse(train._write_plain(wt, self.MAP, b"changed\n"))
        self.assertEqual(open(target).read(), "outside\n")

    def test_union_symlink_a_failing_write_parks_the_lane_and_aborts_the_merge(self):
        """T-5836 review: no space (or any OSError) on the write is a refusal, not an exception out of the train's
        merge: _merge_in logs the conflict, aborts, and leaves no half-written file or merge behind."""
        from sampo import train
        wt, _ = self.union_conflict(merged=False)
        log = []
        b = types.SimpleNamespace(id="T-1", title="lane")

        def full(*a, **k):
            raise OSError(28, "No space left on device")
        with mock.patch.object(train.os, "fdopen", full):
            self.assertIsNone(train._merge_in(wt, b, "two", log, kept={}))
        self.assertEqual(log[0][0], "T-1")
        self.assertIn("conflicts", log[0][1])
        self.assertEqual(self.git("ls-files", "-u", cwd=wt), "")
        self.assertEqual(self.git("status", "--porcelain", "--untracked-files=no", cwd=wt), "")
        self.assertEqual(open(os.path.join(wt, self.MAP)).read(), "- `ops.py`: one.\n")

    def test_union_symlink_a_lane_whose_map_file_is_a_link_parks_and_the_target_is_unchanged(self):
        """T-5836: two lanes that each turn the map entry into a link to a file outside the repo conflict; the train
        parks the second (a link is no plain file) and writes through neither."""
        self.map_base()
        outs, ids = [], []
        for n in (1, 2):
            outs.append(os.path.join(self.tmp, f"outside-ops{n}.md"))
            with open(outs[-1], "w") as f:
                f.write("outside\n")
            tid = self.lane(n, self.MAP, f"lane {n}\n")
            path = os.path.join(self.tmp, f"lane{n}")
            os.remove(os.path.join(path, self.MAP))
            os.symlink(outs[-1], os.path.join(path, self.MAP))
            self.git("add", self.MAP, cwd=path)
            self.git("commit", "-qm", "link", cwd=path)
            ids.append(tid)
        r = self.train(*ids, check=False)
        for outside in outs:
            self.assertEqual(open(outside).read(), "outside\n")
        self.assertFalse((self.meta(ids[1]).get("lane_stop") or "").startswith("merged"), r.stdout)
        self.assertIn("conflicts", r.stdout + r.stderr)

    def cap_base(self, left):
        """T-4980: a map file `left` bytes under its cap: two entries with a filler line between, so two lanes each
        growing one entry merge cleanly."""
        filler = "x" * (c.MAP_FILE_BYTES - left - len("- `a.py`: a.\n- `b.py`: b.\n") - 1)
        self.map_base(text=f"- `a.py`: a.\n{filler}\n- `b.py`: b.\n")
        return filler

    def test_two_lanes_that_together_take_a_map_file_over_its_cap_leave_the_second_out_before_a_gate(self):
        """T-4980: on 2026-11-15 two trains each spent about 40 minutes on a gate and its bisection to find one map
        line that took docs/map/files-lib-4.md over its cap once merged with another lane's line."""
        filler = self.cap_base(300)
        a = self.lane(1, self.MAP, f"- `a.py`: a. {'A' * 200}\n{filler}\n- `b.py`: b.\n")
        b = self.lane(2, self.MAP, f"- `a.py`: a.\n{filler}\n- `b.py`: b. {'B' * 200}\n")
        r = self.train(a, b, check=False)
        self.assertIn(f"left out {b}: takes {self.MAP} (", r.stdout)
        self.assertIn(f"over its {c.MAP_FILE_BYTES}-byte cap", r.stdout)
        self.assertTrue(self.meta(a)["lane_stop"].startswith("merged"), r.stdout)
        self.assertFalse((self.meta(b).get("lane_stop") or "").startswith("merged"), r.stdout)
        self.assertNotIn("B" * 200, self.read(self.MAP))
        self.assertEqual(self.gate_runs(), 1, "one gate run, on the lanes that fit; no bisection")

    def test_two_lanes_that_each_add_a_module_bullet_to_a_full_shard_both_land_the_second_one_moved(self):
        """T-5381: the 2026-11-19 case in small: each lane fits alone, together they take files-lib-1 over its cap; the
        second lane's new bullet moves to files-lib-2 with its MASTER.md index link, the lane lands, the report says so."""
        head = ("# Sampo map: File map, plugin/lib modules\n\nIntro.\n\n### plugin/lib modules, a to z\n\n<details>\n"
                "<summary>every module, one line each</summary>\n\n")
        foot = "\n</details>\n"

        def shard(*bullets):
            return head + "".join(bullets) + foot

        def master(*names):
            return "# M\n\n" + "".join(f"- `{n}` (words…) [→ files-lib-{s}](docs/map/files-lib-{s}.md)\n" for n, s in names)

        old = [("fmold", 1), ("fmother", 2)]
        fill = c.MAP_FILE_BYTES - 300 - len(shard("- `fmold` (\n", ")\n"))
        self.map_base("docs/map/files-lib-1.md", shard(f"- `fmold` ({'x' * fill})\n"))
        self.map_base("docs/map/files-lib-2.md", shard("- `fmother` (y)\n"))
        self.map_base("MASTER.md", master(*old))

        def add(n, name, size):
            tid = self.lane(n, "docs/map/files-lib-1.md", shard(f"- `fmold` ({'x' * fill})\n", f"- `{name}` ({'z' * size})\n"))
            path = os.path.join(self.tmp, f"lane{n}")
            with open(os.path.join(path, "MASTER.md"), "w") as f:
                f.write(master(*old, (name, 1)))
            self.git("add", "MASTER.md", cwd=path)
            subprocess.run(["git", "-C", path, "commit", "-qm", f"index {name}"], check=True, capture_output=True)
            return tid

        a, b = add(1, "fmnewa", 200), add(2, "fmnewb", 200)
        r = self.train(a, b)
        self.assertNotIn("left out", r.stdout)
        self.assertIn(f"moved {b}: 1 new map entry (fmnewb) moved from docs/map/files-lib-1.md to docs/map/files-lib-2.md", r.stdout)
        self.assertTrue(self.meta(b)["lane_stop"].startswith("merged"), r.stdout)
        self.assertIn(f"`fmnewa` ({'z' * 200})", self.read("docs/map/files-lib-1.md"))
        self.assertNotIn("fmnewb", self.read("docs/map/files-lib-1.md"))
        self.assertIn(f"`fmnewb` ({'z' * 200})", self.read("docs/map/files-lib-2.md"))
        self.assertIn("`fmnewb` (words…) [→ files-lib-2](docs/map/files-lib-2.md)", self.read("MASTER.md"))
        self.assertLessEqual(os.path.getsize(os.path.join(self.repo, "docs/map/files-lib-1.md")), c.MAP_FILE_BYTES)

    def test_a_map_file_the_base_has_over_its_cap_blames_no_lane_at_its_merge(self):
        """T-4980: the base's own breach is the gate's to name, never a lane's leave-out."""
        filler = self.cap_base(-100)
        a = self.lane(1, self.MAP, f"- `a.py`: a. more\n{filler}\n- `b.py`: b.\n")
        r = self.train(a, check=False)
        self.assertNotIn("cap", r.stdout.split("left out")[-1] if "left out" in r.stdout else "")
        self.assertNotIn(f"left out {a}: takes", r.stdout)

    def test_map_and_code_conflict_is_left_out(self):
        """T-4375: a lane that conflicts in a map file and in a code file is left out as before, its files named, the
        train's tree holds nothing of it, and nothing is captured."""
        self.map_base()
        with open(os.path.join(self.repo, "same.txt"), "w") as f:
            f.write("base\n")
        self.git("add", "same.txt")
        self.git("commit", "-qm", "code")
        a = self.lane(1, self.MAP, self.ENTRY.rstrip("\n") + " Lane one adds this.\n")
        with open(os.path.join(self.tmp, "lane1", "same.txt"), "w") as f:
            f.write("one\n")
        self.git("add", "same.txt", cwd=os.path.join(self.tmp, "lane1"))
        subprocess.run(["git", "-C", os.path.join(self.tmp, "lane1"), "commit", "-qm", "code one"], check=True)
        b = self.lane(2, self.MAP, self.ENTRY.rstrip("\n") + " Lane two adds that.\n")
        with open(os.path.join(self.tmp, "lane2", "same.txt"), "w") as f:
            f.write("two\n")
        self.git("add", "same.txt", cwd=os.path.join(self.tmp, "lane2"))
        subprocess.run(["git", "-C", os.path.join(self.tmp, "lane2"), "commit", "-qm", "code two"], check=True)
        r = self.train(a, b)
        self.assertIn(f"left out {b}: conflicts with an earlier lane in the train: ", r.stdout)
        self.assertIn(self.MAP, r.stdout.split(f"left out {b}")[1])
        self.assertIn("same.txt", r.stdout.split(f"left out {b}")[1])
        self.assertNotIn("both sides", r.stdout)
        self.assertEqual(self.read("same.txt"), "one\n")
        text = self.read(self.MAP)
        self.assertIn("Lane one adds this.", text)
        self.assertNotIn("Lane two adds that.", text)
        self.assertNotRegex(text, r"(?m)^(<<<<<<<|=======|>>>>>>>)")
        self.assertFalse(self.meta(b).get("lane_stop", "").startswith("merged"))
        self.assertEqual(self.followups(), [], "nothing was merged that way: nothing to read")
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".git", "MERGE_HEAD")))

    def test_map_union_follow_up(self):
        """T-4375: one follow-up per train that merged this way, naming each lane and file; a train with no such merge
        captures none."""
        self.map_base()
        self.map_base("docs/map/files.md", "- `f.py`: files.\n")
        a = self.lane(1, self.MAP, "- `ops.py`: One.\n")  # T-5472: rewrites of one span, so both are kept
        b = self.lane(2, self.MAP, "- `ops.py`: Two.\n")
        d = self.lane(3, self.MAP, "- `ops.py`: Three.\n")
        r = self.train(a, b, d)
        got = self.followups()
        self.assertEqual(len(got), 1, [x.title for x in got])
        raw = got[0].section("Raw request")
        for tid in (b, d):
            self.assertIn(tid, raw)
        self.assertIn(self.MAP, raw)
        self.assertNotIn(a, raw, "the first lane merged cleanly")
        self.assertIn(got[0].id, r.stdout)
        # a second train with only clean lanes captures nothing more
        e = self.lane(4, "e.txt")
        self.train(e)
        self.assertEqual(len(self.followups()), 1)

    def test_a_lane_whose_affected_test_fails_is_left_out_before_the_gate_run(self):
        """T-3474: each lane's affected tests run on base+lane first; a red one is named by class, a green one lands
        through one gate run (the gate itself never runs these tests)."""
        self.fm("check", "affected", "python3 -m unittest {tests}")
        test = "import unittest\nclass {0}(unittest.TestCase):\n    def test_it(self):\n        self.assertTrue({1})\n"
        good = self.lane(1, "test_good.py", test.format("Good", "True"))
        bad = self.lane(2, "test_bad.py", test.format("Bad", "False"))
        r = self.train(good, bad)
        self.assertIn(f"left out {bad}: its affected tests fail with the lane merged onto the base: test_bad.Bad", r.stdout)
        self.assertEqual(self.gate_runs(), 1, r.stdout)
        self.assertTrue(os.path.exists(os.path.join(self.repo, "test_good.py")))
        self.assertFalse(os.path.exists(os.path.join(self.repo, "test_bad.py")))
        self.assertNotIn("probe", self.git("worktree", "list"), "the probe worktrees are gone")

    def test_an_affected_test_the_lane_edits_that_is_red_on_the_base_holds_the_lane(self):
        """T-5982: a red the base has is excused only for a test the lane did not touch: the lane that edits the red test
        file owns it (a red base would otherwise let any lane through by editing a red test)."""
        self.fm("check", "affected", "python3 -m unittest {tests}")
        red = "import unittest\nclass Old(unittest.TestCase):\n    def test_it(self):\n        self.assertTrue(False)\n"
        with open(os.path.join(self.repo, "test_old.py"), "w") as f:
            f.write(red)
        self.git("add", "test_old.py")
        self.git("commit", "-qm", "a red test on the base")
        a = self.lane(1, "test_old.py", red + "# touched\n")
        r = self.train(a, check=False)
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn("test_old.Old", r.stdout + r.stderr)
        self.assertNotIn("left to the gates", r.stdout)
        self.assertFalse(self.meta(a).get("lane_stop", "").startswith("merged"), "the lane is not let through")

    def test_an_affected_test_red_on_the_base_itself_is_left_to_the_gates(self):
        """T-3474 review: a class red on the bare base too isn't the lane's fault: an info line, and the lane goes on.
        T-5982: for a test the lane did not touch (a source it changes links the test)."""
        self.fm("check", "affected", "python3 -m unittest {tests}")
        red = "# map-links: lib.py\nimport unittest\nclass Old(unittest.TestCase):\n    def test_it(self):\n        self.assertTrue(False)\n"
        with open(os.path.join(self.repo, "test_old.py"), "w") as f:
            f.write(red)
        with open(os.path.join(self.repo, "lib.py"), "w") as f:
            f.write("N = 1\n")
        self.git("add", "test_old.py", "lib.py")
        self.git("commit", "-qm", "a red test on the base")
        a = self.lane(1, "lib.py", "N = 2\n")
        r = self.train(a)
        self.assertIn("its affected tests are red on the base itself: test_old.Old; left to the gates", r.stdout)
        self.assertNotIn(f"left out {a}", r.stdout)
        self.assertEqual(self.gate_runs(), 1, r.stdout)
        self.assertTrue(self.meta(a)["lane_stop"].startswith("merged"))

    def test_a_flaky_affected_test_is_rerun_once_and_the_lane_goes_on(self):
        """T-3474 review: red once, green on the rerun of its class: named as flaky, never blamed on the lane."""
        self.fm("check", "affected", "python3 -m unittest {tests}")
        count = os.path.join(self.tmp, "flaky-runs")
        flaky = ("import os, unittest\nclass Flaky(unittest.TestCase):\n    def test_it(self):\n"
                 f"        with open({count!r}, 'a') as f:\n            f.write('x')\n"
                 f"        self.assertGreater(os.path.getsize({count!r}), 1)\n")
        a = self.lane(1, "test_flaky.py", flaky)
        r = self.train(a)
        self.assertIn("flaky", r.stdout)
        self.assertIn("test_flaky.Flaky", r.stdout)
        self.assertNotIn(f"left out {a}", r.stdout)
        self.assertEqual(self.gate_runs(), 1, r.stdout)
        self.assertTrue(os.path.exists(os.path.join(self.repo, "test_flaky.py")))

    def test_a_criterion_red_once_and_green_on_a_rerun_does_not_leave_the_lane_out(self):
        """T-3629: the acceptance re-run is retried once, like a probe: a load flake gets a note, not a left-out lane."""
        count = os.path.join(self.tmp, "ac-runs")
        a = self.lane(1, "a.txt", ac=f"echo x >> {count}; test $(wc -c < {count}) -gt 2")
        r = self.train(a)
        self.assertIn("flaky (red once, green on a rerun)", r.stdout)
        self.assertNotIn(f"left out {a}", r.stdout)
        self.assertTrue(self.meta(a)["lane_stop"].startswith("merged"), r.stdout)

    def test_the_rerun_of_a_red_criterion_runs_that_criterion_alone(self):
        """T-3629 review: criterion 1 passed, so the rerun of criterion 2 doesn't run it (or wait for load) again."""
        first, count = os.path.join(self.tmp, "first-runs"), os.path.join(self.tmp, "second-runs")
        a = self.lane(1, "a.txt", ac=f"echo x >> {first}")
        self.fm("task", "ac", a, "add", "second", "--verify", f"echo x >> {count}; test $(wc -c < {count}) -gt 2")
        r = self.train(a)
        self.assertIn("flaky (red once, green on a rerun)", r.stdout)
        self.assertEqual(len(open(first).read().split()), 1, "criterion 1 ran once")
        self.assertEqual(len(open(count).read().split()), 2, "criterion 2 ran, then once more")

    def test_probes_run_three_at_once_and_leave_out_what_a_serial_run_does(self):
        """T-3531: up to 3 probes at once, each in its own scratch tree (a barrier only 3 concurrent runs pass); the
        left-out list stays in candidate order, as a serial run (a host over 2x its CPUs) gives it, though the first
        lane finishes last."""
        self.fm("check", "affected", "python3 -m unittest {tests}")
        arrive, log, wait = (os.path.join(self.tmp, n) for n in ("arrive", "probe-log", "barrier-wait"))
        os.makedirs(arrive)

        def red(n, name, after):  # touches arrive/<name>, waits for 3 there (or the barrier-wait file's seconds)
            src = ("import os, time, unittest\nclass {0}(unittest.TestCase):\n    def test_it(self):\n"
                   "        open(os.path.join({1!r}, {0!r}), 'w').close()\n        end = time.time() + float(open({2!r}).read())\n"
                   "        while len(os.listdir({1!r})) < 3 and time.time() < end:\n            time.sleep(0.02)\n"
                   "        with open({3!r}, 'a') as f:\n            f.write('%s %d\\n' % ({0!r}, len(os.listdir({1!r}))))\n"
                   "        time.sleep({4})\n        self.assertTrue(False)\n").format(name, arrive, wait, log, after)
            return self.lane(n, f"test_{name.lower()}.py", src)

        ids = [red(1, "One", 0.6), self.lane(2, "same.txt", "lane\n"), red(3, "Two", 0.3), red(4, "Three", 0)]
        with open(os.path.join(self.repo, "same.txt"), "w") as f:
            f.write("base\n")
        self.git("add", "same.txt")
        self.git("commit", "-qm", "base moves")

        def run(load, secs):  # (the left-out line, what each lane's first run saw at the barrier)
            for f in os.listdir(arrive):
                os.remove(os.path.join(arrive, f))
            open(log, "w").close()
            with open(wait, "w") as f:
                f.write(str(secs))
            r = self.train(*ids, check=False, env={"FOREMAN_HOST": f"load={load},cpus=4,mem_mb=8192",
                                                   "FOREMAN_LOAD_WAIT": "0"})
            seen = {}
            for line in open(log).read().split("\n"):
                if line:
                    seen.setdefault(*line.split())
            return next(l for l in (r.stdout + r.stderr).splitlines() if "nothing to merge" in l), seen

        serial, alone = run(99, 0.3)
        both, seen = run(0.1, 30)
        self.assertEqual(alone["One"], "1", "a host over 2x its CPUs probes one lane at a time")
        self.assertEqual(seen, {"One": "3", "Two": "3", "Three": "3"}, "three probes ran at once")
        self.assertEqual(both, serial)
        self.assertLess(both.index(ids[0]), both.index(ids[1]))
        self.assertLess(both.index(ids[2]), both.index(ids[3]))
        self.assertIn("conflicts with the base: same.txt", both)
        self.assertNotIn("probe", self.git("worktree", "list"), "every probe's scratch tree is gone")

    def _green_lane(self, base_change, links=""):
        """A lane adding test_x.py (counting its runs), its green run recorded as evidence in the lane, then the base
        moving `base_change`; (task id, the run count after the evidence, the train's result, the count file)."""
        self.fm("check", "affected", "python3 -m unittest {tests}")
        count = os.path.join(self.tmp, "x-runs")
        src = (f"{links}import unittest\nclass X(unittest.TestCase):\n    def test_it(self):\n"
               f"        open({count!r}, 'a').write('x')\n")
        a = self.lane(1, "test_x.py", src)
        self.fm("task", "evidence", a, "--step", "1", "--run", "python3 -m unittest test_x.py",
                cwd=os.path.join(self.tmp, "lane1"))
        ran = os.path.getsize(count)
        with open(os.path.join(self.repo, base_change), "a") as f:
            f.write("moved\n")
        self.git("add", base_change)
        self.git("commit", "-qm", "the base moves")
        return a, ran, self.train(a), count

    def test_a_lane_whose_own_green_run_still_holds_skips_its_probe(self):
        """T-3531: the lane's evidence ran its affected tests green on a tree whose diff to base+lane touches none of
        them nor the sources they cover: the probe's test run is skipped, one note line, and the lane lands."""
        a, ran, r, count = self._green_lane("other.txt")
        self.assertIn(f"note {a}: probe skipped", r.stdout)
        self.assertEqual(os.path.getsize(count), ran, "the probe ran no test")
        self.assertTrue(self.meta(a)["lane_stop"].startswith("merged"))

    def test_the_report_line_names_the_minutes_of_each_phase(self):
        """T-3596: the probe, gate and land minutes of a train are in its result and its report, so the slow phase shows."""
        a, ran, r, count = self._green_lane("other.txt")
        self.assertRegex(r.stdout, r"Minutes: probe \d+\.\d, criteria \d+\.\d min, covered \d+ of \d+, gate \d+\.\d, confirm \d+\.\d, "
                                    r"land \d+\.\d; 1 of 1 probe\(s\) skipped")

    def test_a_lane_whose_tested_source_moved_on_the_base_is_probed(self):
        """T-3531: the base changed a source the affected test covers (map-links): its green run no longer holds."""
        with open(os.path.join(self.repo, "lib.py"), "w") as f:
            f.write("X = 1\n")
        self.git("add", "lib.py")
        self.git("commit", "-qm", "a source")
        a, ran, r, count = self._green_lane("lib.py", "# map-links: lib.py\n")
        self.assertNotIn("probe skipped", r.stdout)
        self.assertGreater(os.path.getsize(count), ran, "the probe ran the test again")
        self.assertTrue(self.meta(a)["lane_stop"].startswith("merged"))

    def test_a_lane_whose_own_criterion_is_red_is_left_out_named(self):
        a, b = self.lane(1, "a.txt"), self.lane(2, "b.txt", ac="false")
        r = self.train(a, b)
        self.assertIn(b, r.stdout)
        self.assertIn("criterion", r.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "b.txt")))
        self.assertTrue(os.path.exists(os.path.join(self.repo, "a.txt")))

    def test_a_bare_train_names_no_lane_and_lands_nothing(self):
        self.lane(1, "a.txt")
        r = self.fm("lane", "train", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "a.txt")))

    def test_a_repeated_id_merges_once(self):
        a = self.lane(1, "a.txt")
        self.train(a, a)
        self.assertEqual(self.gate_runs(), 1)
        self.assertTrue(os.path.exists(os.path.join(self.repo, "a.txt")))

    def test_space_separated_ids_all_train(self):
        # T-4833: `sampo lane train T-1 T-2` read only the first id and landed T-1 alone, naming no other
        a, b = self.lane(1, "a.txt"), self.lane(2, "b.txt")
        r = self.fm("lane", "train", a, b)
        self.assertIn(a, r.stdout)
        self.assertIn(b, r.stdout)
        for name in ("a.txt", "b.txt"):
            self.assertTrue(os.path.exists(os.path.join(self.repo, name)), name)
        self.assertEqual(self.gate_runs(), 1)

    def test_a_red_base_stops_the_train_instead_of_blaming_a_lane(self):
        with open(os.path.join(self.repo, "BREAK"), "w") as f:
            f.write("x\n")
        self.git("add", "BREAK")
        self.git("commit", "-qm", "red base")
        a = self.lane(1, "a.txt")
        r = self.train(a, check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("base", r.stderr + r.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "a.txt")))
        self.assertFalse(self.meta(a).get("lane_stop", "").startswith("merged"))


OK = "import unittest\nclass {0}(unittest.TestCase):\n    def test_it(self):\n        pass\n"
# T-3602's fixtures: a scan red when a lane bans a word another lane's .txt file holds (its message names that file);
# a rule test red when two lanes each add an x-file (its message names none); a test only lane a's a.txt keeps green
# once b.txt exists (map-linked to a.txt, so a's diff reaches it)
SCAN = ("import os, unittest\n\n\ndef hits(word):\n"
        "    return sorted(f for f in os.listdir('.') if f.endswith('.txt') and word in open(f).read().split())\n\n\n"
        "class Foo(unittest.TestCase):\n    def test_it(self):\n        ban = os.path.exists('ban-foo')\n"
        "        self.assertEqual(hits('foo') if ban else [], [])\n\n\n"
        "class Bar(unittest.TestCase):\n    def test_it(self):\n        ban = os.path.exists('ban-bar')\n"
        "        self.assertEqual(hits('bar') if ban else [], [])\n")
PAIR = ("import os, unittest\n\n\nclass Aliases(unittest.TestCase):\n    def test_it(self):\n"
        "        self.assertLess(len([f for f in os.listdir('.') if f.startswith('x')]), 2)\n")
TWO_PAIRS = PAIR + ("\n\nclass Pairs(unittest.TestCase):\n    def test_it(self):\n"
                    "        self.assertLess(len([f for f in os.listdir('.') if f.startswith('y')]), 2)\n")
SEEN = "import os, unittest\n\n\nclass Doctor(unittest.TestCase):\n    def test_it(self):\n        os.environ['SEEN'] = '1'\n"
ORDER = ("import os, unittest\n\n\nclass Aliases(unittest.TestCase):\n    def test_it(self):\n"
         "        n = len([f for f in os.listdir('.') if f.startswith('x')])\n"
         "        self.assertLess(n if os.environ.get('SEEN') else 0, 2)\n")
DEP = ("# map-links: a.txt\nimport os, unittest\n\n\nclass Dep(unittest.TestCase):\n    def test_it(self):\n"
       "        self.assertTrue(os.path.exists('a.txt') or not os.path.exists('b.txt'))\n")


class OneId(TrainCase):
    """T-4756: `lane merge` with one id is a train of one: the same gate, named in its output. (T-4742 reached the trunk
    ungated through a helper that passed one id: the plain merge ran no gate.)"""
    def merged(self, name):
        return os.path.exists(os.path.join(self.repo, name))

    def test_a_lane_that_turns_the_gate_red_does_not_reach_the_trunk(self):
        before = self.git("rev-parse", "HEAD")
        tid = self.lane(1, "BREAK")
        r = self.fm("lane", "merge", tid, check=False)
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(self.merged("BREAK"))
        self.assertEqual(self.git("rev-parse", "HEAD"), before, "the trunk did not move")
        self.assertIn(tid, r.stdout + r.stderr)
        self.assertIn("gate", (r.stdout + r.stderr).lower())
        self.assertFalse(self.meta(tid).get("lane_stop", "").startswith("merged"))

    def test_a_lane_whose_affected_test_fails_is_refused_naming_the_test(self):
        self.fm("check", "affected", "python3 -m unittest {tests}")
        bad = self.lane(1, "test_bad.py", "import unittest\nclass Bad(unittest.TestCase):\n"
                                          "    def test_it(self):\n        self.assertTrue(False)\n")
        r = self.fm("lane", "merge", bad, check=False)
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("test_bad.Bad", r.stdout + r.stderr)
        self.assertFalse(self.merged("test_bad.py"))

    def test_a_green_lane_lands_through_one_gate_run_and_the_output_names_the_gate(self):
        tid = self.lane(1, "a.txt")
        r = self.fm("lane", "merge", tid)
        self.assertTrue(self.merged("a.txt"))
        self.assertEqual(self.gate_runs(), 1, r.stdout)
        self.assertIn("gate", r.stdout)
        self.assertIn("train of one", r.stdout)
        self.assertTrue(self.meta(tid)["lane_stop"].startswith("merged"), self.meta(tid))
        self.assertIn("fm lane rm", r.stdout, "the single-lane next steps stay")

    def test_an_unverified_waiver_does_not_waive_the_gate(self):
        tid = self.lane(1, "BREAK", ac="false")
        r = self.fm("lane", "merge", tid, "--unverified", "the criterion needs a service this box lacks", check=False)
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(self.merged("BREAK"))

    def test_a_commit_to_the_lane_between_the_judgment_and_the_train_does_not_land(self):
        """T-4756 review: `lane merge` judges the lane at one tip; a commit the criteria's own run adds to the branch
        (the gap between that judgment and the train's read of the tip) was judged by nothing: refused, not merged."""
        late = ("echo late > late.txt && git add late.txt && git -c user.name=t -c user.email=t@localhost "
                "commit -qm late")
        tid = self.lane(1, "a.txt", ac=late)
        r = self.fm("lane", "merge", tid, check=False)
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("the lane moved while merging: run it again", r.stdout + r.stderr)
        self.assertFalse(self.merged("late.txt"))
        self.assertFalse(self.merged("a.txt"))
        self.assertEqual(self.gate_runs(), 0, "no gate ran on commits nothing judged")
        self.assertFalse(self.meta(tid).get("lane_stop", "").startswith(("merged", "refused")), self.meta(tid))
        refused = [e for e in c.ledger_tail(c.find_project(self.repo), 300)
                   if e.get("task") == tid and (e.get("data") or {}).get("kind") == "merge_refused"]
        self.assertEqual(refused, [], "a moved lane is not a refusal: a plain retry must not count as one")

    def test_a_one_id_conflict_writes_the_conflict_event_and_brief(self):
        """T-4756 review: the plain merge's conflict leaves a lane_conflict event, the conflict brief and a conflict
        stop mark; the gated one-id merge leaves the same."""
        tid = self.lane(1, "same.txt", "lane\n")
        with open(os.path.join(self.repo, "same.txt"), "w") as f:
            f.write("trunk\n")
        self.git("add", "same.txt")
        self.git("-c", "user.name=t", "-c", "user.email=t@localhost", "commit", "-qm", "trunk same")
        before = self.git("rev-parse", "HEAD")
        r = self.fm("lane", "merge", tid, check=False)
        out = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 0, out)
        self.assertIn("same.txt", out)
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertTrue(self.meta(tid)["lane_stop"].startswith("conflict"), self.meta(tid))
        brief = out.split("Conflict brief: ")[1].split()[0]
        text = open(brief).read()
        self.assertIn("same.txt", text)
        self.assertIn("<<<<<<<", text, "the hunks the builder must resolve")
        [ev] = [e for e in c.ledger_tail(c.find_project(self.repo), 300) if e.get("event") == "lane_conflict"]
        self.assertEqual(ev["task"], tid)
        self.assertIn("same.txt", ev["data"]["files"])

    def test_the_report_names_the_gate_and_the_functions_changed_of_the_trains_merge(self):
        """T-4756 review: the train fast-forwards a merge commit built in a scratch tree: the report says so (no
        `--no-ff` claim), HEAD is that merge commit (first parent the old trunk, second the lane tip), and the
        functions-changed lines read it."""
        before = self.git("rev-parse", "HEAD")
        tid = self.lane(1, "mod.py", "def hello():\n    return 1\n")
        tip = self.git("rev-parse", f"foreman/lane{1}")
        r = self.fm("lane", "merge", tid)
        self.assertNotIn("--no-ff", r.stdout)
        self.assertIn("gate: a train of one", r.stdout)
        self.assertIn("Functions changed:", r.stdout)
        self.assertIn("hello", r.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD^1"), before)
        self.assertEqual(self.git("rev-parse", "HEAD^2"), tip)

    def test_a_project_with_no_checks_merges_plainly_and_says_no_gate_ran(self):
        self.fm("check", "rm", "1")
        tid = self.lane(1, "a.txt")
        r = self.fm("lane", "merge", tid)
        self.assertTrue(self.merged("a.txt"))
        self.assertIn("no gate ran", r.stdout)


class PrivateTerm(TrainCase):
    """T-4783: the private-term check `lane merge` runs (T-3999) runs for every lane of a train, in the hold step, before
    any probe or gate: a lane that adds a term is left out naming file and line (never the term); the others land.
    The list is an invented one, set the way test_release sets its own (sampo private add)."""
    TERM = "zorblat-private.example"

    def setUp(self):
        super().setUp()
        self.fm("private", "add", self.TERM)

    def dirty(self, n, name="remote.py"):
        return self.lane(n, name, f'A = 1\nHOST = "llm.{self.TERM}"\nB = 2\n')

    def counted(self):
        """(calls, patch): release._private_hits wrapped to count its calls (this process only)."""
        from unittest import mock
        from sampo import release
        calls, real = [], release._private_hits
        return calls, mock.patch.object(release, "_private_hits",
                                        side_effect=lambda *a, **k: calls.append(a[2]) or real(*a, **k))

    def test_a_train_leaves_out_the_lane_that_adds_a_term_naming_file_and_line_and_lands_the_other(self):
        bad, good = self.dirty(1), self.lane(2, "ok.txt")
        r = self.train(bad, good)
        out = r.stdout + r.stderr
        self.assertIn(bad, out)
        self.assertIn("remote.py", out)
        self.assertRegex(out, r"remote\.py:2\b", "the line, as the merge note names it")
        self.assertNotIn("zorblat", out, "the term itself is never printed")
        self.assertTrue(os.path.exists(os.path.join(self.repo, "ok.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.repo, "remote.py")))
        self.assertNotIn("zorblat", self.git("log", "-p", "--format=%B", "HEAD"), "it never reached the trunk")
        self.assertTrue(self.meta(good)["lane_stop"].startswith("merged"), self.meta(good))
        self.assertFalse(self.meta(bad).get("lane_stop", "").startswith("merged"))
        self.assertEqual(self.gate_runs(), 1, "one gate run, on the lane that stayed")

    def test_a_train_of_only_that_lane_lands_nothing_and_runs_no_gate(self):
        bad = self.dirty(1)
        r = self.train(bad, self.dirty(2, "other.py"), check=False)
        out = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 0, out)
        self.assertIn("remote.py", out)
        self.assertIn("other.py", out)
        self.assertNotIn("zorblat", out)
        self.assertEqual(self.gate_runs(), 0)

    def test_a_term_in_a_commit_the_lane_later_cleans_still_leaves_it_out(self):
        tid = self.dirty(1)
        path = os.path.join(self.tmp, "lane1")
        with open(os.path.join(path, "remote.py"), "w") as f:
            f.write("A = 1\n")
        self.git("commit", "-qam", "clean it", cwd=path)  # the first commit still carries the term
        r = self.train(tid, self.lane(2, "ok.txt"))
        out = r.stdout + r.stderr
        self.assertIn("private term", out)
        self.assertIn(self.git("rev-parse", "--short=8", "foreman/lane1~1"), out, "the commit that carries it")
        self.assertNotIn("zorblat", out)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "remote.py")))
        self.assertTrue(os.path.exists(os.path.join(self.repo, "ok.txt")))

    def test_a_one_id_merge_of_that_lane_is_refused_once_with_no_gate_run(self):
        bad = self.dirty(1)
        calls, patch = self.counted()
        from sampo import core as c, lanes
        p = c.find_project(self.repo)
        with patch, self.assertRaises(c.PolicyError) as ctx:
            lanes.merge(p, c.find_brief(p, bad), self.repo)
        self.assertEqual(len(calls), 1, "asked once: not twice, not never")
        msg = str(ctx.exception)
        self.assertIn("remote.py:2", msg)
        self.assertNotIn("zorblat", msg)
        self.assertEqual(self.gate_runs(), 0)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "remote.py")))

    def test_a_clean_one_id_merge_asks_once_and_the_train_of_one_does_not_ask_again(self):
        tid = self.lane(1, "a.txt")
        calls, patch = self.counted()
        from sampo import core as c, lanes
        p = c.find_project(self.repo)
        with patch:
            lanes.merge(p, c.find_brief(p, tid), self.repo)
        self.assertEqual(len(calls), 1, "merge asked; the judged tip is not asked again in the train")
        self.assertTrue(os.path.exists(os.path.join(self.repo, "a.txt")))

    def test_every_lane_of_a_multi_id_train_is_asked_in_the_hold_step(self):
        ids = [self.lane(1, "a.txt"), self.lane(2, "b.txt"), self.lane(3, "c.txt")]
        calls, patch = self.counted()
        from sampo import core as c, train
        with patch:
            train.train(c.find_project(self.repo), self.repo, ids)
        self.assertEqual(len(calls), 3)
        self.assertEqual(sorted(calls), sorted(self.git("rev-parse", f"foreman/lane{n}") for n in (1, 2, 3)))

    def refusal(self, tid, n=1):
        """lanes.private_refusal for the task tid's lane n: (kind, why)."""
        from sampo import core as c, lanes
        p = c.find_project(self.repo)
        return lanes.private_refusal(p, c.find_brief(p, tid), self.repo, self.git("rev-parse", f"foreman/lane{n}"))

    def failing(self, verb, rc=124):
        """lanes._git where only the `verb` call (rev-list, diff) fails as a timeout does: empty stdout, a return code."""
        import subprocess
        from unittest import mock
        from sampo import lanes
        real = lanes._git
        return mock.patch.object(lanes, "_git", side_effect=lambda root, *a, **k: subprocess.CompletedProcess(
            ["git", *a], rc, "", "boom: output text") if a[:1] == (verb,) else real(root, *a, **k))

    def failing_listing_again(self):
        """lanes._git where the file listing (diff --name-only) fails from its second call with the same arguments on:
        the private check's own listing passes, the guard's listing after it fails (T-4783)."""
        import subprocess
        from unittest import mock
        from sampo import lanes
        real, seen = lanes._git, {}

        def run(root, *a, **k):
            if "--name-only" in a and "--diff-filter=U" not in a:
                seen[a] = seen.get(a, 0) + 1
                if seen[a] > 1:
                    return subprocess.CompletedProcess(["git", *a], 128, "", "boom: output text")
            return real(root, *a, **k)
        return mock.patch.object(lanes, "_git", side_effect=run)

    def scan_events(self):
        from sampo import core as c
        return [e for e in c.ledger_tail(c.find_project(self.repo), 300) if e.get("event") == "private_scan_failed"]

    def test_a_failing_rev_list_holds_the_lane_and_says_the_scan_could_not_run(self):
        tid = self.lane(1, "ok.txt")
        with self.failing("rev-list"):
            kind, why = self.refusal(tid)
        self.assertEqual(kind, "scan", why)
        self.assertIn("could not run: run it again", why)
        self.assertNotIn("redact", why, "no advice to amend commits for a scan that did not run")
        self.assertNotIn("boom", why)
        [ev] = self.scan_events()
        self.assertEqual(ev["task"], tid)
        self.assertEqual(ev["data"].get("cause"), "git exit 124")
        self.assertNotIn("boom", json.dumps(ev), "git's output text is never logged")

    def test_a_failing_diff_holds_the_lane(self):
        tid = self.lane(1, "ok.txt")
        with self.failing("diff", rc=1):
            kind, why = self.refusal(tid)
        self.assertEqual(kind, "scan", why)
        self.assertEqual(self.scan_events()[0]["data"].get("cause"), "file listing failed")

    def holds(self, tid, n=1, base="HEAD"):
        """lanes.land_holds (T-4785) for the task tid's lane n: the reason, or ''."""
        from sampo import core as c, lanes
        p = c.find_project(self.repo)
        return lanes.land_holds(p, c.find_brief(p, tid), self.repo, self.git("rev-parse", f"foreman/lane{n}"), base=base)

    def test_land_holds_is_empty_for_a_clean_lane_and_names_a_term_without_printing_it(self):  # T-4785
        self.assertEqual(self.holds(self.lane(1, "ok.txt")), "")
        why = self.holds(self.dirty(2, "remote.py"), 2)
        self.assertIn("remote.py:2", why)
        self.assertNotIn("zorblat", why)
        self.assertIn("redact it in the lane", why, "the train's own wording")

    def test_land_holds_compares_against_the_base_it_is_given_not_only_the_checkouts_head(self):  # T-4785 (c): a PR's base
        tid = self.dirty(1, "remote.py")
        self.assertNotEqual(self.holds(tid), "")
        self.assertEqual(self.holds(tid, base=self.git("rev-parse", "foreman/lane1")), "", "nothing ahead of itself")
        self.git("branch", "pr-base", "main")
        self.assertIn("remote.py:2", self.holds(tid, base="pr-base"))

    def test_land_holds_holds_when_a_git_call_fails_and_when_the_guard_refuses_a_file(self):  # T-4785
        tid = self.lane(1, "ok.txt")
        for verb in ("rev-list", "diff"):
            with self.failing(verb, rc=128):
                self.assertIn("could not run", self.holds(tid), verb)
        bad = self.lane(2, ".git-credentials", "https://invented-user:invented-token@example.test\n")
        self.assertIn("the guard refuses .git-credentials", self.holds(bad, 2))

    def test_a_listing_that_fails_after_the_private_check_holds_the_one_id_merge(self):
        tid = self.lane(1, "ok.txt")
        from sampo import core as c, lanes
        p = c.find_project(self.repo)
        with self.failing_listing_again(), self.assertRaises(c.PolicyError) as ctx:
            lanes.merge(p, c.find_brief(p, tid), self.repo)
        self.assertIn("could not run: run it again", str(ctx.exception))
        self.assertNotIn("boom", str(ctx.exception))
        self.assertEqual(self.scan_events()[0]["data"].get("cause"), "file listing failed")
        self.assertFalse(os.path.exists(os.path.join(self.repo, "ok.txt")))
        self.assertEqual(self.gate_runs(), 0)

    def test_a_listing_that_fails_after_the_private_check_holds_the_lane_of_a_train_not_lands_it_unguarded(self):
        from sampo import core as c, train
        ids = [self.lane(1, "a.txt"), self.lane(2, "b.txt")]
        with self.failing_listing_again(), self.assertRaises(c.PolicyError) as ctx:
            train.train(c.find_project(self.repo), self.repo, ids)
        self.assertEqual([i for i, _ in ctx.exception.left], ids)
        for _, why in ctx.exception.left:
            self.assertIn("could not run: run it again", why)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "a.txt")))
        self.assertEqual(self.gate_runs(), 0)

    def test_a_listing_that_times_out_is_none_not_an_empty_list(self):
        from unittest import mock
        from sampo import cli, lanes
        tip = self.git("rev-parse", "HEAD")
        self.assertEqual(lanes.lane_files(self.repo, tip), [])
        with mock.patch.object(lanes, "_git", side_effect=cli.UsageError("git timed out")):
            self.assertIsNone(lanes.lane_files(self.repo, tip))

    def test_a_train_holds_both_lanes_when_the_files_cannot_be_listed(self):
        from sampo import core as c, train
        ids = [self.lane(1, "a.txt"), self.lane(2, "b.txt")]
        with self.failing("diff"), self.assertRaises(c.PolicyError) as ctx:
            train.train(c.find_project(self.repo), self.repo, ids)
        self.assertEqual([i for i, _ in ctx.exception.left], ids)
        for _, why in ctx.exception.left:
            self.assertIn("could not run: run it again", why)
            self.assertNotIn("redact", why)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "a.txt")))
        self.assertEqual(self.gate_runs(), 0)

    def test_an_os_error_in_the_scan_holds_the_lane_and_the_train_goes_on(self):
        from unittest import mock
        from sampo import core as c, release, train
        ids = [self.lane(1, "a.txt"), self.lane(2, "b.txt")]
        real, seen = release.tempfile.mkdtemp, []

        def flaky(*a, **k):
            if k.get("prefix") == "sampo-private-" and not seen:  # the scratch folder of the first lane's scan only
                seen.append(1)
                raise OSError(28, "no space left")
            return real(*a, **k)
        with mock.patch.object(release.tempfile, "mkdtemp", side_effect=flaky):
            got = train.train(c.find_project(self.repo), self.repo, ids)
        # T-5465 runs the holds four at a time, so either lane's scan may be the first (3 of 6 runs failed on order)
        out = [i for i, _ in got["left_out"]]
        self.assertEqual(len(out), 1, got)
        self.assertEqual(sorted(got["merged"] + out), sorted(ids))
        self.assertIn("could not run: run it again", got["left_out"][0][1])
        self.assertEqual(self.scan_events()[0]["data"].get("cause"), "OSError")

    def test_a_scan_that_fails_in_a_one_id_merge_is_refused_with_the_rerun_advice(self):
        tid = self.lane(1, "ok.txt")
        from sampo import core as c, lanes
        p = c.find_project(self.repo)
        with self.failing("rev-list"), self.assertRaises(c.PolicyError) as ctx:
            lanes.merge(p, c.find_brief(p, tid), self.repo)
        self.assertIn("could not run: run it again", str(ctx.exception))
        self.assertNotIn("amend", str(ctx.exception))
        self.assertFalse(os.path.exists(os.path.join(self.repo, "ok.txt")))

    def test_a_term_only_in_a_commit_message_leaves_the_lane_out_naming_the_commit(self):
        tid = self.lane(1, "ok.txt")
        path = os.path.join(self.tmp, "lane1")
        self.git("commit", "-q", "--amend", "-m", f"fix the host llm.{self.TERM}", cwd=path)
        kind, why = self.refusal(tid)
        self.assertEqual(kind, "hit", why)
        self.assertIn(self.git("rev-parse", "--short=8", "foreman/lane1"), why)
        self.assertNotIn(" file", why, "no file holds it")
        self.assertNotIn("zorblat", why)
        r = self.train(tid, self.lane(2, "b.txt"))
        self.assertNotIn("zorblat", r.stdout + r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "ok.txt")))
        self.assertTrue(os.path.exists(os.path.join(self.repo, "b.txt")))

    def test_a_merge_commit_inside_the_lane_that_brings_the_term_leaves_it_out(self):
        tid = self.lane(1, "ok.txt")
        path = os.path.join(self.tmp, "lane1")
        who = ("-c", "user.name=t", "-c", "user.email=t@localhost")
        self.git("checkout", "-q", "-b", "side", "HEAD~1", cwd=path)
        with open(os.path.join(path, "side.py"), "w") as f:
            f.write(f'HOST = "llm.{self.TERM}"\n')
        self.git("add", "side.py", cwd=path)
        self.git(*who, "commit", "-qm", "side", cwd=path)
        self.git("checkout", "-q", "foreman/lane1", cwd=path)
        self.git(*who, "merge", "-q", "--no-ff", "-m", "bring the side in", "side", cwd=path)
        self.assertEqual(len(self.git("rev-list", "--merges", "HEAD~1..foreman/lane1", cwd=path).split()), 1)
        self.git("branch", "-q", "-D", "side", cwd=path)
        r = self.train(tid, self.lane(2, "b.txt"))
        out = r.stdout + r.stderr
        self.assertIn("side.py:1", out)
        self.assertNotIn("zorblat", out)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "side.py")))
        self.assertTrue(os.path.exists(os.path.join(self.repo, "b.txt")))

    def test_unverified_on_a_one_id_merge_still_runs_the_private_check(self):
        tid = self.dirty(1, "remote.py")
        r = self.fm("lane", "merge", tid, "--unverified", "the criterion needs a service this box lacks", check=False)
        out = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 0, out)
        self.assertIn("remote.py:2", out)
        self.assertNotIn("zorblat", out)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "remote.py")))

    def test_no_term_list_asks_nothing_of_a_clean_lane_and_the_train_says_so(self):
        self.fm("private", "rm", self.TERM)
        tid = self.lane(1, "a.txt")
        self.assertEqual(self.refusal(tid), ("", ""))
        r = self.train(tid)
        out = r.stdout + r.stderr
        self.assertIn("the private-term check had a term list with no usable term", out)  # rm left an empty list, not none
        self.assertTrue(os.path.exists(os.path.join(self.repo, "a.txt")))

    def test_an_unreadable_term_list_holds_the_lane_and_says_so(self):  # T-4785 (d): it was passed, with a notice
        from sampo import core as c
        with open(os.path.join(c.state_dir(), c.PRIVATE_TERMS), "w") as f:
            f.write("{not json")
        tid = self.lane(1, "a.txt")
        self.assertEqual(self.refusal(tid)[0], "scan", "the check every path calls holds on it")
        self.assertIn("term list", self.refusal(tid)[1])
        r = self.train(tid, check=False)
        out = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 0, out)
        self.assertIn("term list", out)
        self.assertNotIn(c.PRIVATE_TERMS, out, "no path in the line")
        self.assertFalse(os.path.exists(os.path.join(self.repo, "a.txt")))
        self.assertEqual(self.gate_runs(), 0)
        self.assertTrue([e for e in self.scan_events() if e["data"].get("cause") == "term list unreadable"])

    def test_an_unreadable_term_list_holds_every_lane_and_the_train_says_so_once(self):
        # T-4804: unreadable is not unset: a scan that read no terms from a damaged list would merge what it can't judge
        from sampo import core as c
        with open(os.path.join(c.state_dir(), c.PRIVATE_TERMS), "w") as f:
            f.write("{not json")
        r = self.train(self.lane(1, "a.txt"), check=False)
        out = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(out.count("the private-term list exists but cannot be read"), 1, out)
        self.assertNotIn("had no term list", out, "unreadable is not 'none set'")
        self.assertNotIn(c.PRIVATE_TERMS, out, "no path in the line")
        self.assertFalse(os.path.exists(os.path.join(self.repo, "a.txt")), "held: nothing was merged")

    def test_no_term_list_but_a_secrets_file_says_that_only_its_values_were_scanned(self):
        self.fm("private", "rm", self.TERM)
        path = os.path.join(self.tmp, "invented-secrets.env")
        with open(path, "w") as f:
            f.write("INVENTED_API_KEY=invented-value-0123456789\n")
        os.chmod(path, 0o600)
        r = self.train(self.lane(1, "a.txt"), env={"SAMPO_SECRETS": path})
        out = r.stdout + r.stderr
        self.assertIn("had a term list with no usable term", out)
        self.assertIn("only the secrets-file values were scanned", out)
        self.assertNotIn("invented-value", out)

    def test_a_train_with_a_term_list_prints_no_such_line(self):
        r = self.train(self.lane(1, "a.txt"))
        self.assertNotIn("had no term list", r.stdout + r.stderr)
        self.assertNotIn("no usable term", r.stdout + r.stderr)  # T-4785 re-check: nor the empty-list wording


class AffectedBase(TrainCase):
    """T-3544's fixture (T-4705 shares it): a suite whose runs are logged, set as the gate and the affected template."""
    def setUp(self):
        super().setUp()
        self.suite = os.path.join(self.tmp, "suite-runs")  # one line per suite run: its folder and its arguments
        os.makedirs(os.path.join(self.repo, "plugin", "tests"))
        files = {"suite.sh": f'echo "${{PWD##*/}} $*" >> {self.suite}\nPYTHONPATH=plugin/tests exec python3 -m unittest "$@"\n',
                 "plugin/tests/test_doctor.py": OK.format("Doctor"),  # rule tests (cli.RULE_TESTS)
                 "plugin/tests/test_rename_aliases.py": OK.format("Aliases"), "test_other.py": OK.format("Other")}
        for rel, text in files.items():
            with open(os.path.join(self.repo, rel), "w") as f:
                f.write(text)
        self.git("add", "-A")
        self.git("commit", "-qm", "a suite")
        self.fm("check", "add", "sh suite.sh")
        self.fm("check", "affected", "sh suite.sh {names}")
        from sampo import core as c
        self.p = c.find_project(self.repo)

    def gate_args(self):
        """The suite's arguments on each run in the train's scratch tree (the probes run in their own)."""
        with open(self.suite) as f:
            return [ln.split(" ", 1)[1].strip() for ln in f if ln.startswith("sampo-train-")]

    def probe_runs(self):
        """The suite's runs in a probe's scratch tree (T-3474's pre-filter), not the train's."""
        with open(self.suite) as f:
            return [ln for ln in f if ln.startswith("sampo-probe-")]


class AffectedGate(AffectedBase):
    """T-3544: the train's full-suite gate (the affected template's runner with no names) runs only the tests the
    batch's merged changes reach plus the rule tests; the full suite moves to the post-merge verify."""

    def test_a_gate_that_runs_the_affected_tests_needs_no_probe_of_them(self):  # T-3770
        """The gate is the affected suite, so it runs each lane's tests on the merged tree once; no lane's tests run
        again in a probe of its own first."""
        a, b = self.lane(1, "test_a.py", OK.format("A")), self.lane(2, "test_b.py", OK.format("B"))
        r = self.train(a, b)
        self.assertEqual(self.probe_runs(), [], r.stdout)
        self.assertEqual(self.gate_args(), ["test_doctor test_rename_aliases test_a test_b"], r.stdout)
        self.assertTrue(self.meta(a)["lane_stop"].startswith("merged") and self.meta(b)["lane_stop"].startswith("merged"))

    def test_a_red_lane_is_still_left_out_by_the_gate_with_no_probe(self):  # T-3770
        bad = "import unittest\nclass Bad(unittest.TestCase):\n    def test_it(self):\n        self.assertTrue(False)\n"
        a, b = self.lane(1, "test_a.py", OK.format("A")), self.lane(2, "test_bad.py", bad)
        r = self.train(a, b)
        self.assertEqual(self.probe_runs(), [], r.stdout)
        self.assertIn(f"left out {b}: its merge turns the gates red", r.stdout)
        self.assertTrue(os.path.exists(os.path.join(self.repo, "test_a.py")))
        self.assertFalse(os.path.exists(os.path.join(self.repo, "test_bad.py")))

    def test_the_probes_come_back_with_their_switch(self):  # T-3770
        a = self.lane(1, "test_a.py", OK.format("A"))
        r = self.train(a, env={"FOREMAN_TRAIN_PROBES": "1"})
        self.assertNotEqual(self.probe_runs(), [], r.stdout)

    def test_the_gate_names_only_the_affected_modules_and_the_rule_tests(self):
        """A lane adding a module gets test_rename_aliases though no map link names it (T-3316's miss); the other
        gates run as configured, the pass records its modules, and each landed merge awaits the full suite."""
        from sampo import core as c, lanes
        a, b = self.lane(1, "test_a.py", OK.format("A")), self.lane(2, "plugin/newmod.py", "X = 1\n")
        r = self.train(a, b, env={"FM_POSTMERGE_CMD": "sh suite.sh"})
        self.assertEqual(self.gate_args(), ["test_doctor test_rename_aliases test_a"], r.stdout)
        self.assertEqual(self.gate_runs(), 1, "the other gate ran once, as configured")
        row = [e for e in c.ledger_events(self.p, ("check_run",)) if e["data"].get("train")][-1]["data"]["results"][1]
        self.assertEqual((row["cmd"], row["affected"]), ("sh suite.sh", ["test_doctor", "test_rename_aliases", "test_a"]))
        self.assertEqual(sorted(x["task"] for x in lanes.pm_records(self.p).values()), sorted([a, b]))
        self.assertIn("3 affected test module(s)", r.stdout)

    def test_a_red_gate_names_the_failing_class_on_the_left_out_lane(self):
        """Each lane is green alone (its probe passes); together they fail: the culprit's reason and brief name the
        failing class and the gate's last lines, so nobody reruns a full suite to learn them."""
        pair = "import os, unittest\nclass Pair(unittest.TestCase):\n    def test_it(self):\n        self.assertFalse(os.path.exists('b.txt'))\n"
        a, b = self.lane(1, "test_pair.py", pair), self.lane(2, "b.txt")
        r = self.train(a, b)
        self.assertIn(f"left out {b}: its merge turns the gates red: sh suite.sh on 3 affected test module(s), "
                      "failing test_pair.Pair; blamed: the failure output names b.txt", r.stdout)
        self.assertIn("FAILED (failures=1)", r.stdout, "the gate's last lines")
        log = self.fm("task", "show", b).stdout
        self.assertIn("test_pair.Pair", log)
        self.assertIn("FAILED (failures=1)", log)
        self.assertTrue(os.path.exists(os.path.join(self.repo, "test_pair.py")))
        self.assertFalse(os.path.exists(os.path.join(self.repo, "b.txt")))
        from sampo import core as c
        row = [e for e in c.ledger_events(self.p, ("check_run",)) if e["data"].get("train")][-1]["data"]["results"][1]
        self.assertEqual(row["affected"], ["test_doctor", "test_pair", "test_pair.Pair", "test_rename_aliases"],
                         "T-3602: the batch's modules (passed with every lane in) and what the confirm reran")

    def base(self, rel, text):
        with open(os.path.join(self.repo, rel), "w") as f:
            f.write(text)
        self.git("add", rel)
        self.git("commit", "-qm", f"base: {rel}")

    def test_a_culprit_the_failure_output_names_is_left_out_with_no_bisection(self):
        """T-3602: the red run's own failure text names b.txt, which only lane b changed: b is left out with no gate
        rerun on any prefix; one probe of the red class alone on the red tree (it fails alone, so a narrowed run can
        see it), then a confirm of that class's module plus what b's diff reaches (the rule tests) and the other gate."""
        self.base("plugin/tests/test_rename_scan.py", SCAN)
        z, a, b, d = (self.lane(1, "ban-foo"), self.lane(2, "a.txt", "fine\n"), self.lane(3, "b.txt", "foo\n"),
                      self.lane(4, "d.txt", "fine\n"))
        r = self.train(z, a, b, d)
        out = next(ln for ln in r.stdout.splitlines() if ln.startswith(f"  left out {b}:"))
        self.assertIn("failing test_rename_scan.Foo; blamed: the failure output names b.txt", out)
        self.assertEqual(self.gate_args()[1:], ["test_rename_scan.Foo"] * 2 + ["test_doctor test_rename_aliases test_rename_scan"],
                         r.stdout)  # T-4676: alone on the red tree, then alone on the bare base, then the confirm
        self.assertEqual(self.gate_runs(), 2, "the other gate: the full run and the confirm")
        self.assertIn("in 2 gate run(s) and 2 probe(s)", r.stdout)
        self.assertIn("the failure output names b.txt", self.fm("task", "show", b).stdout)
        for name, landed in (("ban-foo", True), ("a.txt", True), ("d.txt", True), ("b.txt", False)):
            self.assertEqual(os.path.exists(os.path.join(self.repo, name)), landed, name)

    def test_an_ambiguous_red_bisects_with_probes_of_only_the_failing_class(self):
        """T-3602: the failure names no lane's file and every lane reaches the rule test: a prefix bisection whose
        probes run only the failing class, then a confirm of that class's module and the left-out lane's reach."""
        self.base("plugin/tests/test_rename_aliases.py", PAIR)
        l1, l2, l3, l4 = self.lane(1, "x1"), self.lane(2, "c.txt"), self.lane(3, "x2"), self.lane(4, "d.txt")
        r = self.train(l1, l2, l3, l4)
        self.assertEqual(self.gate_args()[1:], ["test_rename_aliases.Aliases"] * 4 + ["test_doctor test_rename_aliases"],
                         r.stdout)
        self.assertIn(f"left out {l3}: its merge turns the gates red", r.stdout)
        self.assertIn("blamed: bisection, 2 probe(s) of only test_rename_aliases.Aliases", r.stdout)
        self.assertIn("4 probe(s)", r.stdout.splitlines()[0])  # alone, two bisection probes, the bare base
        self.assertEqual(self.gate_runs(), 2)
        for name, landed in (("x1", True), ("c.txt", True), ("d.txt", True), ("x2", False)):
            self.assertEqual(os.path.exists(os.path.join(self.repo, name)), landed, name)

    def test_two_ambiguous_culprits_go_from_one_red_run(self):
        """T-4986: two classes red, each by its own lane, and the failure names neither lane's file: the bisection blames
        the first, then bisects the class still green at its prefix over the lanes after it, in the same round. Both are
        left out with one confirm after (train 3b took one 40-100 minute round per culprit), each for its own class."""
        self.base("plugin/tests/test_rename_aliases.py", TWO_PAIRS)
        l1, l2, l3, l4, l5, l6 = (self.lane(1, "x1"), self.lane(2, "y1"), self.lane(3, "x2"), self.lane(4, "c.txt"),
                                  self.lane(5, "y2"), self.lane(6, "d.txt"))
        r = self.train(l1, l2, l3, l4, l5, l6)
        out = {t: next((ln for ln in r.stdout.splitlines() if ln.startswith(f"  left out {t}:")), "") for t in (l3, l5)}
        self.assertIn("blamed: bisection", out[l3], r.stdout)
        self.assertIn("of only test_rename_aliases.Aliases", out[l3], r.stdout)
        self.assertIn("blamed: bisection", out[l5], r.stdout)
        self.assertIn("of only test_rename_aliases.Pairs", out[l5], r.stdout)
        self.assertNotIn("Pairs", out[l3], "each lane is left out for its own class only")
        self.assertNotIn("Aliases", out[l5], "each lane is left out for its own class only")
        self.assertEqual(self.gate_runs(), 2, "the full run and one confirm: no second round for the second culprit")
        for name, landed in (("x1", True), ("y1", True), ("c.txt", True), ("d.txt", True), ("x2", False), ("y2", False)):
            self.assertEqual(os.path.exists(os.path.join(self.repo, name)), landed, name)

    def test_a_named_culprit_and_an_ambiguous_one_go_from_one_red_run(self):
        """T-4986 review: the failure names b.txt for one class and no file for the other: the named lane is left out by
        its name, and the class nobody's file explains is bisected in the same round, not left red for the next."""
        self.base("plugin/tests/test_rename_scan.py", SCAN)
        self.base("plugin/tests/test_rename_aliases.py", PAIR)
        z, l1, b, l3, d = (self.lane(1, "ban-foo"), self.lane(2, "x1"), self.lane(3, "b.txt", "foo\n"), self.lane(4, "x2"),
                           self.lane(5, "d.txt", "fine\n"))
        r = self.train(z, l1, b, l3, d)
        out = {t: next((ln for ln in r.stdout.splitlines() if ln.startswith(f"  left out {t}:")), "") for t in (b, l3)}
        self.assertIn("the failure output names b.txt", out[b], r.stdout)
        self.assertIn("blamed: bisection", out[l3], r.stdout)
        self.assertIn("of only test_rename_aliases.Aliases", out[l3], r.stdout)
        self.assertEqual(self.gate_runs(), 2, "the full run and one confirm")
        for name, landed in (("ban-foo", True), ("x1", True), ("d.txt", True), ("b.txt", False), ("x2", False)):
            self.assertEqual(os.path.exists(os.path.join(self.repo, name)), landed, name)

    def test_two_independent_culprits_go_from_one_red_run(self):
        """T-3602: two failing classes, each naming its own lane's file: both left out at once, no probe."""
        self.base("plugin/tests/test_rename_scan.py", SCAN)
        ids = [self.lane(1, "ban-foo"), self.lane(2, "ban-bar"), self.lane(3, "b.txt", "foo\n"),
               self.lane(4, "d.txt", "bar\n"), self.lane(5, "e.txt", "fine\n")]
        r = self.train(*ids)
        self.assertIn(f"left out {ids[2]}: its merge turns the gates red", r.stdout)
        self.assertIn("the failure output names b.txt", r.stdout)
        self.assertIn(f"left out {ids[3]}: its merge turns the gates red", r.stdout)
        self.assertIn("the failure output names d.txt", r.stdout)
        self.assertEqual(self.gate_args()[1:], ["test_rename_scan.Bar test_rename_scan.Foo"] * 2 + [
                                                "test_doctor test_rename_aliases test_rename_scan"], r.stdout)
        for name, landed in (("ban-foo", True), ("ban-bar", True), ("e.txt", True), ("b.txt", False), ("d.txt", False)):
            self.assertEqual(os.path.exists(os.path.join(self.repo, name)), landed, name)

    def test_a_lane_needing_a_left_out_lane_is_caught_by_the_confirm(self):
        """T-3602: lane a is named and left out; lane b only works with a (test_dep, which a's diff reaches): the confirm
        runs a's reach, goes red on test_dep, and b is left out by its own name in that failure; no full gate reruns."""
        self.base("plugin/tests/test_rename_scan.py", SCAN)
        self.base("plugin/tests/test_dep.py", DEP)
        z, a, b, e = (self.lane(1, "ban-foo"), self.lane(2, "a.txt", "foo\n"), self.lane(3, "b.txt", "fine\n"),
                      self.lane(4, "e.txt", "fine\n"))
        r = self.train(z, a, b, e)
        self.assertIn(f"left out {a}: its merge turns the gates red", r.stdout)
        self.assertIn("failing test_rename_scan.Foo; blamed: the failure output names a.txt", r.stdout)
        out = next(ln for ln in r.stdout.splitlines() if ln.startswith(f"  left out {b}:"))
        self.assertIn("failing test_dep.Dep; blamed: the failure output names b.txt", out)
        reach = "test_dep test_doctor test_rename_aliases test_rename_scan"  # a's: test_dep links a.txt
        self.assertEqual(self.gate_args()[1:], ["test_rename_scan.Foo"] * 2 + [reach] + ["test_dep.Dep"] * 2 + [reach],
                         r.stdout)
        self.assertEqual(self.gate_runs(), 3, "the full run and two confirms")
        for name, landed in (("ban-foo", True), ("e.txt", True), ("a.txt", False), ("b.txt", False)):
            self.assertEqual(os.path.exists(os.path.join(self.repo, name)), landed, name)

    def test_a_class_green_alone_bisects_with_the_whole_suite_gate(self):
        """T-3602 review: Aliases fails only after Doctor ran in the same process (order): run alone it passes on the
        red tree, so no narrowed probe or confirm could see it; the bisection and the confirm run the suite gate as
        the train does, and the lane that turns it red is left out, never landed."""
        self.base("plugin/tests/test_doctor.py", SEEN)
        self.base("plugin/tests/test_rename_aliases.py", ORDER)
        l1, l2, l3, l4 = self.lane(1, "x1"), self.lane(2, "c.txt"), self.lane(3, "x2"), self.lane(4, "d.txt")
        r = self.train(l1, l2, l3, l4)
        self.assertEqual(self.gate_args()[1:], ["test_rename_aliases.Aliases"] * 2 + ["test_doctor test_rename_aliases"] * 3,
                         r.stdout)  # T-4676: the bare base, T-4700: before the blame (the bisection) rather than after it
        self.assertIn(f"left out {l3}: its merge turns the gates red", r.stdout)
        self.assertIn("blamed: bisection, 2 probe(s) of the whole red gate", r.stdout)
        for name, landed in (("x1", True), ("c.txt", True), ("d.txt", True), ("x2", False)):
            self.assertEqual(os.path.exists(os.path.join(self.repo, name)), landed, name)

    def test_a_gate_that_passed_before_the_red_one_reruns_in_the_confirm(self):
        """T-3602 review: the first gate passes with every lane in (BREAK, but FIX.txt too); the suite names FIX.txt,
        so lane a goes; the confirm reruns the first gate, which is red without FIX.txt, and lane b (BREAK) goes too:
        the passing row from the full run is never recorded for a tree it didn't run on."""
        self.base("plugin/tests/test_rename_scan.py", SCAN)
        z, a, b, e = (self.lane(1, "ban-foo"), self.lane(2, "FIX.txt", "foo\n"), self.lane(3, "BREAK"),
                      self.lane(4, "e.txt", "fine\n"))
        r = self.train(z, a, b, e)
        self.assertIn("blamed: the failure output names FIX.txt", r.stdout)
        out = next(ln for ln in r.stdout.splitlines() if ln.startswith(f"  left out {b}:"))
        self.assertIn("test ! -e BREAK", out)
        self.assertIn("blamed: bisection", out)
        for name, landed in (("ban-foo", True), ("e.txt", True), ("FIX.txt", False), ("BREAK", False)):
            self.assertEqual(os.path.exists(os.path.join(self.repo, name)), landed, name)

    def test_full_gate_runs_the_bare_full_suite(self):
        from sampo import lanes
        self.train(self.lane(1, "test_a.py", OK.format("A")), env={"FOREMAN_FULL_GATE": "1", "FM_POSTMERGE_CMD": "sh suite.sh"})
        self.assertEqual(self.gate_args(), [""])
        self.assertEqual(lanes.pm_records(self.p), {}, "the full suite already ran: no post-merge verify")

    def test_a_close_after_the_train_reuses_its_affected_pass(self):
        """T-3526's close skips its linked tests when the train's pass ran every one of them."""
        a = self.lane(1, "test_a.py", OK.format("A"))
        self.train(a)
        with open(self.suite) as f:
            before = f.read()
        r = self.fm("task", "finish", a, "--audit", "self", "--run", "true", check=False, env={"FOREMAN_SENTINEL": "off"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("covered by the gates' pass", r.stdout)
        with open(self.suite) as f:
            self.assertEqual(f.read(), before, "the linked tests ran again")

    def red_base(self):
        with open(os.path.join(self.repo, "plugin", "tests", "test_doctor.py"), "w") as f:
            f.write("import unittest\nclass Doctor(unittest.TestCase):\n    def test_it(self):\n        self.assertTrue(False)\n")
        self.git("commit", "-qam", "red base")

    def test_a_red_base_is_reported_from_its_affected_classes_before_the_full_gate(self):  # T-3625
        self.red_base()
        r = self.train(self.lane(1, "test_a.py", OK.format("A")), check=False, env={"FM_POSTMERGE_CMD": "sh suite.sh"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("is red", r.stderr + r.stdout)
        self.assertIn("Doctor", r.stderr + r.stdout)
        self.assertEqual(self.gate_runs(), 0, "no gate run: the base's affected classes found it")
        self.assertEqual(self.gate_args(), [], "the scratch tree never ran the suite")

    def test_the_base_check_runs_only_when_the_env_differs_from_the_last_green_gate(self):  # T-3625
        from sampo import train
        base = self.git("rev-parse", "HEAD")
        sub = ("sh suite.sh", "sh suite.sh {names}", ["plugin/tests/test_doctor.py"])
        n = lambda: len(open(self.suite).read().splitlines()) if os.path.exists(self.suite) else 0  # noqa: E731
        self.assertEqual(train._base_red(self.p, self.repo, base, sub), "")
        self.assertEqual(n(), 1, "no green gate recorded yet: it runs")
        train._mark_green(self.p)
        self.git("commit", "--allow-empty", "-qm", "the base moved")
        self.assertEqual(train._base_red(self.p, self.repo, self.git("rev-parse", "HEAD"), sub), "")
        self.assertEqual(n(), 1, "same env, another base: not run again")
        os.environ["FOREMAN_LOAD_GATE"] = "0"
        try:
            self.assertEqual(train._base_red(self.p, self.repo, base, sub), "")
        finally:
            del os.environ["FOREMAN_LOAD_GATE"]
        self.assertEqual(n(), 2, "a different env runs again")

    def test_a_callers_identity_never_changes_the_env_digest(self):  # T-3625
        import run
        from sampo import train
        self.assertLessEqual(train._DIGEST_ENV, run.OPERATOR_SWITCHES)
        before = train._env_digest()
        os.environ["FOREMAN_DRIVE_TASK"] = "T-9"
        try:
            self.assertEqual(train._env_digest(), before)
        finally:
            del os.environ["FOREMAN_DRIVE_TASK"]

    def test_a_flaky_base_class_is_rerun_once_and_the_train_goes_on(self):  # T-3625
        from sampo import train
        flaky = os.path.join(self.tmp, "flaked")
        with open(os.path.join(self.repo, "plugin", "tests", "test_doctor.py"), "w") as f:
            f.write("import os, unittest\nclass Doctor(unittest.TestCase):\n    def test_it(self):\n"
                    f"        first = not os.path.exists({flaky!r})\n        open({flaky!r}, 'w').close()\n"
                    "        self.assertFalse(first)\n")
        self.git("commit", "-qam", "flaky")
        sub = ("sh suite.sh", "sh suite.sh {names}", ["plugin/tests/test_doctor.py"])
        self.assertEqual(train._base_red(self.p, self.repo, self.git("rev-parse", "HEAD"), sub), "")


class Covered(AffectedBase):
    """T-3809: a lane criterion that is the suite gate on named test modules is not run in the lane before the gate: the
    gate runs those modules on the merged tree, and a red one leaves out the lane that owns it as any red module does."""

    def lane_runs(self):
        """The suite's runs inside a lane's own folder (a criterion run there), not the train's or a probe's."""
        with open(self.suite) as f:
            return [ln for ln in f if ln.startswith("lane")]

    def test_a_suite_criterion_is_covered_by_the_gate_and_its_modules_join_the_gate(self):
        marker = os.path.join(self.tmp, "criterion-ran")
        a = self.lane(1, "x.txt", ac="sh suite.sh test_other")  # an old module no change reaches: only the criterion names it
        b = self.lane(2, "test_a.py", OK.format("A"), ac="sh suite.sh test_a")
        d = self.lane(3, "d.txt", ac=f"echo x >> {marker} && test -e d.txt")  # not the suite: still run in the lane
        r = self.train(a, b, d)
        self.assertEqual(self.lane_runs(), [], r.stdout)
        self.assertEqual(self.probe_runs(), [], r.stdout)
        self.assertEqual(sorted(self.gate_args()[0].split()), ["test_a", "test_doctor", "test_other", "test_rename_aliases"])
        self.assertEqual(open(marker).read().split(), ["x"], "the other criterion ran once, in the lane")
        self.assertIn(f"note {a}: criterion 1 covered by the gate", r.stdout)
        self.assertIn(f"note {b}: criterion 1 covered by the gate", r.stdout)
        self.assertNotIn(f"note {d}: criterion 1 covered", r.stdout)
        for tid in (a, b, d):
            self.assertTrue(self.meta(tid)["lane_stop"].startswith("merged"), r.stdout)

    def test_a_red_covered_module_still_leaves_out_its_lane(self):
        bad = "import unittest\nclass Other(unittest.TestCase):\n    def test_it(self):\n        self.assertTrue(False)\n"
        a = self.lane(1, "test_other.py", bad, ac="sh suite.sh test_other")
        b = self.lane(2, "b.txt", ac="sh suite.sh test_doctor")
        r = self.train(a, b)
        self.assertEqual(self.lane_runs(), [], r.stdout)
        self.assertIn(f"left out {a}: its merge turns the gates red", r.stdout)
        self.assertTrue(self.meta(b)["lane_stop"].startswith("merged"), r.stdout)
        self.assertFalse(self.meta(a).get("lane_stop", "").startswith("merged"))

    def test_a_landed_lane_records_the_gate_run_as_its_criterion_evidence(self):
        a = self.lane(1, "x.txt", ac="sh suite.sh test_other")
        b = self.lane(2, "y.txt", ac="sh suite.sh test_other")
        r = self.train(a, b)
        show = self.fm("task", "show", a).stdout
        self.assertRegex(show, r"- \[x\] ok")
        self.assertRegex(show, r"\(ac 1\) `sh suite\.sh test_other` → exit 0 · covered by the train's gate run: "
                               r"'sh suite\.sh' ran test_other(, \w+)* on tree [0-9a-f]{12}")
        self.assertIn("covered by the train's gate run", self.fm("task", "show", b).stdout, r.stdout)

    def test_a_lane_left_out_gets_no_gate_evidence(self):
        bad = "import unittest\nclass Other(unittest.TestCase):\n    def test_it(self):\n        self.assertTrue(False)\n"
        a = self.lane(1, "test_other.py", bad, ac="sh suite.sh test_other")
        b = self.lane(2, "b.txt")
        self.train(a, b)
        self.assertNotIn("covered by the train's gate run", self.fm("task", "show", a).stdout)

    def test_a_pattern_the_module_lacks_or_a_missing_module_is_run_in_the_lane(self):
        a = self.lane(1, "a.txt", ac="sh suite.sh test_other -k test_it")  # the pattern is in the module: covered
        b = self.lane(2, "b.txt", ac="sh suite.sh test_other -k nowhere_in_it")
        d = self.lane(3, "d.txt", ac="sh suite.sh test_nothing_here")
        r = self.train(a, b, d, check=False)
        self.assertIn(f"note {a}: criterion 1 covered by the gate", r.stdout)
        self.assertNotIn(f"note {b}: criterion 1 covered", r.stdout)
        self.assertNotIn(f"note {d}: criterion 1 covered", r.stdout)
        self.assertEqual(sorted({ln.split()[0] for ln in self.lane_runs()}), ["lane2", "lane3"])

    def test_the_minutes_line_reports_the_criteria_stage_apart(self):
        a = self.lane(1, "x.txt", ac="sh suite.sh test_other")
        b = self.lane(2, "y.txt", ac="sh suite.sh test_doctor")
        d = self.lane(3, "d.txt", ac="test -e d.txt")
        r = self.train(a, b, d)
        self.assertRegex(r.stdout, r"Minutes: probe \d+\.\d, criteria \d+\.\d min, covered 2 of 3, gate \d+\.\d, "
                                   r"confirm \d+\.\d, land \d+\.\d;")

    def test_full_gate_runs_every_module_so_it_covers_too(self):
        a = self.lane(1, "x.txt", ac="sh suite.sh test_other")
        r = self.train(a, env={"FOREMAN_FULL_GATE": "1"})
        self.assertEqual(self.lane_runs(), [], r.stdout)
        self.assertIn(f"note {a}: criterion 1 covered by the gate", r.stdout)
        # the whole suite ran (no module list on its row): the evidence says so
        self.assertRegex(self.fm("task", "show", a).stdout,
                         r"covered by the train's gate run: 'sh suite\.sh' ran the whole suite on tree [0-9a-f]{12}")

    XDEP = ("# map-links: b.txt\nimport os, unittest\n\n\nclass X(unittest.TestCase):\n    def test_it(self):\n"
            "        self.assertFalse(os.path.exists('bad' + '.txt'), 'boom')\n")  # the failure never names the file

    def base_file(self, rel, text):
        os.makedirs(os.path.dirname(os.path.join(self.repo, rel)), exist_ok=True)
        with open(os.path.join(self.repo, rel), "w") as f:
            f.write(text)
        self.git("add", rel)
        self.git("commit", "-qm", f"base: {rel}")

    MAP = Train.MAP
    ENTRY = Train.ENTRY

    def map_base(self):
        self.base_file(self.MAP, self.ENTRY)

    def followups(self):
        p = c.find_project(self.repo)
        return [b for b in c.load_briefs(p) if b.meta.get("source") == "followup"]

    def test_the_owner_of_a_red_covered_module_is_blamed_not_the_lane_the_map_links_to_it(self):
        """T-3809 review: lane b's file only touches test_xdep through the map; lane d changed only a source (bad.txt)
        and its criterion names the module. The gate is red in it: base + d alone is red, so d is left out, b lands."""
        self.base_file("test_xdep.py", self.XDEP)
        b = self.lane(1, "b.txt")
        d = self.lane(2, "bad.txt", ac="sh suite.sh test_xdep")
        r = self.train(b, d)
        self.assertIn(f"left out {d}: its merge turns the gates red", r.stdout)
        self.assertTrue(self.meta(b)["lane_stop"].startswith("merged"), r.stdout)
        self.assertFalse(self.meta(d).get("lane_stop", "").startswith("merged"), r.stdout)

    def test_a_module_the_covering_lane_leaves_green_alone_keeps_the_map_blame(self):
        """The same red, but the covering lane (d) adds nothing the module dislikes: the map-linked lane b is still the one."""
        self.base_file("test_xb.py", self.XDEP.replace("'bad' + '.txt'", "'b' + '.txt'"))  # red with b.txt in the tree
        b = self.lane(1, "b.txt")
        d = self.lane(2, "d.txt", ac="sh suite.sh test_xb")
        r = self.train(b, d)
        self.assertIn(f"left out {b}: its merge turns the gates red", r.stdout)
        self.assertTrue(self.meta(d)["lane_stop"].startswith("merged"), r.stdout)

    def test_a_green_owner_probe_keeps_the_map_follow_up_of_a_lane_merged_before_the_blamed_one(self):
        """T-3809 review: the probe merges the owner (d) alone on the base, which pops its kept-both-sides entry; it is
        put back, so the follow-up of the train still names d (it merged on a's line of the same entry)."""
        self.map_base()
        self.base_file("test_xb.py", self.XDEP.replace("'bad' + '.txt'", "'b' + '.txt'"))  # red with b.txt in the tree
        a = self.lane(1, self.MAP, "- `ops.py`: One.\n")  # T-5472: rewrites of one span, so both are kept
        d = self.lane(2, self.MAP, "- `ops.py`: Two.\n", ac="sh suite.sh test_xb")
        b = self.lane(3, "b.txt")
        r = self.train(a, d, b)
        self.assertIn(f"left out {b}: its merge turns the gates red", r.stdout)
        self.assertIn(f"merged {d} keeping both sides' lines in {self.MAP}", r.stdout)
        got = self.followups()
        self.assertEqual(len(got), 1, r.stdout)
        self.assertIn(d, got[0].section("Raw request"))

    def test_the_owner_swap_hands_the_blamed_lanes_other_names_back_to_the_bisection(self):
        """T-3809 review: lane b is the weak (map) blame for test_xdep and for test_xz2; lane d owns test_xdep and is
        red alone, so d takes that blame. test_xz2, which b's file turns red, is bisected in the same round (not left
        for a second red gate): d and b are both left out in one round, and e lands on a gate run that stays green."""
        self.base_file("test_xdep.py", self.XDEP)
        self.base_file("test_xz2.py", self.XDEP.replace("'bad' + '.txt'", "'b' + '.txt'"))
        b = self.lane(1, "b.txt")
        d = self.lane(2, "bad.txt", ac="sh suite.sh test_xdep")
        e = self.lane(3, "e.txt")
        r = self.train(b, d, e)
        self.assertIn(f"left out {d}: its merge turns the gates red", r.stdout)
        self.assertIn(f"left out {b}: its merge turns the gates red", r.stdout)
        self.assertRegex(r.stdout, r"landed on main \(\w+\) in 2 gate run\(s\)")

    def test_two_weak_blames_with_one_owner_leave_only_the_owner_out(self):
        """T-3809 review: lanes b1 and b2 are each map-linked to a module lane d carries and turns red alone: d is the
        one left out, and neither of them."""
        for n in (1, 2):
            self.base_file(f"test_xm{n}.py", self.XDEP.replace("# map-links: b.txt", f"# map-links: b{n}.txt"))
        b1, b2 = self.lane(1, "b1.txt"), self.lane(2, "b2.txt")
        d = self.lane(3, "bad.txt", ac="sh suite.sh test_xm1 test_xm2")
        r = self.train(b1, b2, d)
        self.assertIn(f"left out {d}: its merge turns the gates red", r.stdout)
        self.assertEqual(r.stdout.count("left out"), 1, r.stdout)
        for x in (b1, b2):
            self.assertTrue(self.meta(x)["lane_stop"].startswith("merged"), r.stdout)

    def test_a_pattern_is_a_test_or_class_name_not_a_word_in_the_file(self):
        with open(os.path.join(self.repo, "test_other.py"), "a") as f:
            f.write("# only_in_a_comment\n")
        self.git("commit", "-qam", "a comment")
        a = self.lane(1, "a.txt", ac="sh suite.sh test_other -k only_in_a_comment")
        b = self.lane(2, "b.txt", ac="sh suite.sh test_other -k Other")  # a class name
        d = self.lane(3, "d.txt", ac="sh suite.sh test_other -k test_it")  # a test name
        r = self.train(a, b, d, check=False)
        self.assertNotIn(f"note {a}: criterion 1 covered", r.stdout)
        self.assertIn(f"note {b}: criterion 1 covered by the gate", r.stdout)
        self.assertIn(f"note {d}: criterion 1 covered by the gate", r.stdout)
        self.assertEqual(sorted({ln.split()[0] for ln in self.lane_runs()}), ["lane1"])

    def test_only_a_python_file_of_the_module_name_joins_the_gate(self):
        a = self.lane(1, "test_other.json", "{}\n", ac="sh suite.sh test_other")
        self.train(a)
        self.assertEqual(self.gate_args()[0].split().count("test_other"), 1, self.gate_args())

    def test_a_criterion_whose_evidence_cannot_be_recorded_says_so_and_is_left_for_the_close(self):
        from unittest import mock

        from sampo import cli, train

        class Refusing:  # a brief that refuses the evidence line of criterion 1 and the box of criterion 2
            def add_evidence(self, cmd, result, ac=None, **kw):
                if ac == 1:
                    raise c.PolicyError("T-1 refuses this line")

            def check_ac(self, n):
                raise c.PolicyError(f"T-1 acceptance criterion {n} has no evidence")
        lane = types.SimpleNamespace(id="T-1")
        with mock.patch.object(cli, "mutate", lambda p, i, fn, ev, data: fn(Refusing())):
            notes = train._covered_evidence(None, lane, {1: ("sh suite.sh test_a", ["test_a.py"]),
                                                         2: ("sh suite.sh test_b", ["test_b.py"])},
                                            {"cmd": "s", "exit": 0}, "abc123")
        self.assertEqual([n[0] for n in notes], ["T-1", "T-1"], notes)
        self.assertIn("criterion 1: covered, but its evidence was not recorded: ", notes[0][1])
        self.assertIn("criterion 2: covered, but its evidence is recorded, but the box is not ticked: ", notes[1][1])

    def test_a_whole_suite_run_cut_to_the_red_names_leaves_the_close_the_criterion(self):
        """FOREMAN_FULL_GATE: the first gate (the whole suite) is red in lane d's module, the lane is left out, and the
        confirm no longer runs the whole suite: the landed lane b's criterion is proven by neither its row's module
        list nor a whole run, or is proven by the whole run it got; either way it is never ticked without a run naming it."""
        bad = "import unittest\nclass A(unittest.TestCase):\n    def test_it(self):\n        self.assertTrue(False)\n"
        b = self.lane(1, "b.txt", ac="sh suite.sh test_other")
        d = self.lane(2, "test_a.py", bad, ac="sh suite.sh test_a")
        r = self.train(b, d, env={"FOREMAN_FULL_GATE": "1"})
        self.assertIn(f"left out {d}", r.stdout)
        show = self.fm("task", "show", b).stdout
        self.assertTrue(self.meta(b)["lane_stop"].startswith("merged"), r.stdout)
        if "covered by the train's gate run" in show:
            self.assertRegex(show, r"ran (the whole suite|test_other\S*) on tree")
        self.assertNotIn("covered by the train's gate run", self.fm("task", "show", d).stdout)

    def test_a_red_first_gate_then_green_keeps_the_evidence_of_a_module_it_ran(self):
        """The first gate (a narrowed run) is red in lane d's module; d is left out; b's module ran green in that run, and
        the final row lists it: b's criterion has the gate's evidence."""
        bad = "import unittest\nclass A(unittest.TestCase):\n    def test_it(self):\n        self.assertTrue(False)\n"
        b = self.lane(1, "b.txt", ac="sh suite.sh test_other")
        d = self.lane(2, "test_a.py", bad, ac="sh suite.sh test_a")
        r = self.train(b, d)
        self.assertIn(f"left out {d}", r.stdout)
        self.assertRegex(self.fm("task", "show", b).stdout,
                         r"covered by the train's gate run: 'sh suite\.sh' ran test_other\S* on tree|ran the whole suite on tree")

    def test_a_failure_recording_the_evidence_after_the_landing_is_a_note_not_a_crash(self):
        from unittest import mock

        from sampo import train
        lane = types.SimpleNamespace(id="T-1")
        with mock.patch.object(train, "_covered_evidence", side_effect=OSError("disk full")):
            notes = train._record_covered(None, [(lane, "sha")], {"T-1": {1: ("cmd", ["test_a.py"])}}, {"cmd": "s", "exit": 0},
                                          "abc123")
        self.assertEqual(len(notes), 1, notes)
        self.assertEqual(notes[0][0], "T-1")
        self.assertIn("left for the close to run: OSError: disk full", notes[0][1])


class GateNames(unittest.TestCase):
    """T-3809: which criteria are the suite gate on named modules (train._gate_names): anything else runs in the lane."""
    SUITE = "python3 plugin/tests/run.py"

    def names(self, cmd, suite=None):
        from sampo import train
        return train._gate_names(cmd, suite or self.SUITE)

    def test_modules_classes_patterns_and_a_deadline_are_names(self):
        self.assertEqual(self.names("python3 plugin/tests/run.py test_a test_b.Cls -k pat --deadline 30"),
                         (["test_a", "test_b.Cls"], ["pat"]))
        self.assertEqual(self.names("python3 plugin/tests/run.py test_a.Cls.test_x"), (["test_a.Cls.test_x"], []))

    def test_anything_else_is_not_one(self):
        for cmd in ("python3 plugin/tests/run.py",  # the whole suite: no modules named
                    "python3 plugin/tests/run.py test_a && echo hi", "python3 plugin/tests/run.py test_a | tail",
                    "python3 plugin/tests/run.py test_a -j 4", "python3 plugin/tests/run.py -k pat",
                    "python3 plugin/tests/run.py test_a -k", "python3 plugin/tests/run.py test_a --deadline soon",
                    "python3 plugin/tests/run.py ../test_a", "python3 plugin/tests/run.py test_a $(id)",
                    "env X=1 python3 plugin/tests/run.py test_a", "python3 other/run.py test_a", "pytest test_a",
                    "python3 plugin/tests/run.py 'test a'", "python3 plugin/tests/run.py test_a 'it's",
                    "python3 plugin/tests/run.py test_a\ntouch x", "python3 plugin/tests/run.py test_a\rtouch x"):
            self.assertIsNone(self.names(cmd), repr(cmd))  # a newline is a second command to the shell


class RedNames(unittest.TestCase):
    """T-3602 review: the names a red run fails, complete or none (then the gate reruns whole, never narrowed)."""
    def red(self, out):
        from sampo import train
        return train._red(out)

    def test_each_unittest_red_line_gives_its_class_module_or_fixture(self):
        out = ("FAIL: test_a (test_x.X.test_a)\nERROR: setUpModule (test_m)\nERROR: setUpClass (test_c.C)\n"
               "ERROR: test_gone (unittest.loader._FailedTest.test_gone)\n")
        self.assertEqual(self.red(out), ["test_c.C", "test_gone", "test_m", "test_x.X"])

    def test_run_py_lines_add_classes_that_never_started_and_a_wrong_count_gives_none(self):
        listed = "\n  failed class: test_x.X\n  failed class: test_late.L\n"
        self.assertEqual(self.red("FAIL: test_a (test_x.X.test_a)" + listed + "\nFAILED (2 classes: …)\n"),
                         ["test_late.L", "test_x.X"])
        self.assertEqual(self.red("FAIL: test_a (test_x.X.test_a)" + listed + "\nFAILED (3 classes: …)\n"), [])

    def test_classes_the_deadline_cut_are_named_beside_the_failed_one(self):  # T-3936
        out = ("FAIL: test_a (test_x.X.test_a)\n\n  failed class: test_x.X\n\nFAILED (1 classes: test_x.X)\n"
               "not run (deadline): 2\n  not run class: test_late.L\n  not run class: test_later.M\n")
        self.assertEqual(self.red(out), ["test_late.L", "test_later.M", "test_x.X"])

    def test_a_red_line_naming_no_class_gives_none_and_a_split_job_folds_into_its_class(self):
        self.assertEqual(self.red("FAIL: test_a (test_x.X.test_a)\nERROR: oddity (nowhere)\n"), [])
        self.assertEqual(self.red("FAIL: test_a (test_x.X.test_a)\n  failed class: test_x.X.test_a\n"
                                  "\nFAILED (1 classes: test_x.X.test_a)\n"), ["test_x.X"])


class SlotPriority(TrainCase):
    def test_the_train_and_the_main_checks_runs_take_slots_first_and_a_lanes_do_not(self):
        """T-3614: tests/run.py gives freed CPU slots to a waiting run with SAMPO_SLOT_PRIORITY=1 first."""
        seen = os.path.join(self.tmp, "priority")
        self.fm("check", "add", f'echo "${{FOREMAN_SLOT_PRIORITY:-low}}" >> {seen}')
        unset = {"FOREMAN_SLOT_PRIORITY": ""}  # this test's own runner may run with it
        tid = self.lane(1, "a.txt")
        self.fm("check", "--fresh", cwd=os.path.join(self.tmp, "lane1"), env=unset)
        self.fm("check", "--fresh", env=unset)
        self.fm("check", "--fresh", env={"FOREMAN_SLOT_PRIORITY": "0"})  # an opt-out stays low
        self.train(tid, env=unset)
        self.assertEqual(open(seen).read().split(), ["low", "1", "0", "1"])


class TrainHold(TrainCase):
    """T-5077: a train leaves a record of itself while it runs (its process with its start time, the checkout it holds, its
    lanes), which the guard reads to refuse a trunk-moving git command there. Nothing here runs a real train but one whose
    gate looks at the record; the rest writes the record the way a train would, naming a process of the test's own."""
    def setUp(self):
        super().setUp()
        self.p = c.find_project(self.repo)
        self.record = os.path.join(self.p.dir, "train", "running.json")

    def child(self):
        """A live process of the test's own, and the record a train of that pid would write."""
        from sampo import bus
        proc = subprocess.Popen(["sleep", "60"])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        return proc, f"{bus.host_id()}|{proc.pid}|{bus._proc(proc.pid)[1]}"

    def write(self, proc, text, root=None, at=None):
        os.makedirs(os.path.dirname(self.record), exist_ok=True)
        with open(self.record, "w") as f:
            json.dump({"proc": text, "pid": proc.pid, "root": root or self.repo, "lanes": ["T-0001", "T-0002"],
                       "at": at or time.time() - 600}, f)

    def test_a_running_train_writes_its_record_and_takes_it_away(self):
        tid = self.lane(1, "a.txt")
        seen = os.path.join(self.tmp, "seen.json")
        self.fm("check", "add", f"cp {self.record} {seen}")  # the gate runs while the train holds its lock
        self.assertFalse(os.path.exists(self.record))
        self.train(tid)
        got = json.load(open(seen))
        self.assertEqual(got["root"], self.repo)
        self.assertEqual(got["lanes"], [tid])
        self.assertIn(f"|{got['pid']}|", got["proc"], "host|ns|pid|start: a reused pid is told by its start")
        self.assertNotEqual(got["pid"], os.getpid(), "the train is the sampo process, not the test")
        self.assertFalse(os.path.exists(self.record), "the train over: nothing holds trunk")

    def test_train_hold_reads_a_live_train_only(self):
        from sampo import train as strain
        self.assertIsNone(strain.train_hold(self.p), "no record")
        proc, text = self.child()
        self.write(proc, text)
        got = strain.train_hold(self.p)
        self.assertEqual((got["pid"], got["root"], got["lanes"]), (proc.pid, self.repo, ["T-0001", "T-0002"]))
        self.assertAlmostEqual(got["held"], 600, delta=60)
        self.write(proc, text.rsplit("|", 1)[0] + "|1")  # a reused pid: another start time
        self.assertIsNone(strain.train_hold(self.p))
        self.write(proc, text.replace(text.split("|")[0], "another-host"))  # another machine: nothing known of it
        self.assertIsNone(strain.train_hold(self.p))
        self.write(proc, text)
        proc.kill()
        proc.wait()
        self.assertIsNone(strain.train_hold(self.p), "a killed train's leftover record is no train")
        with open(self.record, "w") as f:
            f.write("{not json")
        self.assertIsNone(strain.train_hold(self.p))
        with open(self.record, "w") as f:
            f.write("[]")
        self.assertIsNone(strain.train_hold(self.p))

    def test_the_trains_own_process_tree_is_not_held(self):
        from sampo import train as strain
        proc, text = self.child()
        self.write(proc, text)
        self.assertIsNotNone(strain.train_hold(self.p))
        self.assertIsNone(strain.train_hold(self.p, chain=[os.getpid(), proc.pid]), "a child of the train")

    def test_a_trains_end_leaves_a_later_trains_record(self):
        from sampo import train as strain
        proc, text = self.child()
        with strain._train_running(self.p, self.repo, ["T-1"]):
            self.write(proc, text)  # another train's record, written over ours
        self.assertTrue(os.path.exists(self.record), "only the record of the ending train goes")

    def test_the_hook_hands_the_guard_the_reader_and_a_commit_on_trunk_is_refused(self):
        import fmguard
        from sampo import hooks
        ctx = hooks._guard_ctx({"cwd": self.repo}, fmguard)[0]
        self.assertIsNone(ctx.train_hold())
        self.assertIsNone(fmguard.check("Bash", {"command": "git commit -m x"}, ctx))
        proc, text = self.child()
        self.write(proc, text)
        blk = fmguard.check("Bash", {"command": "git commit -m x"}, ctx)
        self.assertEqual(blk.category, "confirm", blk)
        self.assertIn("merge train", fmguard.message(blk, ctx))
        lane = os.path.join(self.tmp, "lane-x")
        subprocess.run(["git", "-C", self.repo, "worktree", "add", "-q", "-b", "foreman/x", lane], check=True)
        ctx = hooks._guard_ctx({"cwd": lane}, fmguard)[0]
        self.assertIsNotNone(ctx.train_hold(), "a lane sees the same train")
        self.assertIsNone(fmguard.check("Bash", {"command": "git commit -m x"}, ctx), "and commits freely in its own tree")
        self.write(proc, text, root=os.path.join(self.tmp, "elsewhere"))
        ctx = hooks._guard_ctx({"cwd": self.repo}, fmguard)[0]
        self.assertIsNone(fmguard.check("Bash", {"command": "git commit -m x"}, ctx), "a train that holds another checkout")


OLD = "2026-01-01T00:00:00"  # a lane quiet this long counts as finished (core.finished_lanes)
CLEAN_CMD = "python3 plugin/tests/run.py " + " ".join(c.repo_shape_classes())  # a --hold run's command (train._held_classes)


def _report(tid, what="Lane work"):
    return (f"Branch foreman/x, commit abc1234.\n\n**CHANGELOG:** {what} lands with one gate run ({tid}).\n"
            f"TITLE: {what} ({tid})\n- LESSON: a report line the close reads is one nobody retypes.\n"
            f"NUMBERS: none: no metric moves\nDOCS: none: an internal lane\n")


class SelfResolve(TrainCase):
    """T-6102: a lane the land parked for a code conflict gets its headless resolve from the land (pilotsig.resolve_run, run
    in-process here under the claim the detached pilot takes), is re-queued into the same land's train once it is green,
    with no lead call; a resolve that fails twice leaves a hand row naming both sides' hunks; OVERLAP pairs train in
    successive passes with the merge between them. (A LandCase subclass would import test_lane_land, which imports this
    module: the few helpers it needs are here.)"""

    def setUp(self):
        super().setUp()
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.repo)
        self.p = c.find_project(self.repo)
        self.calls = []
        with open(os.path.join(self.repo, "CHANGELOG.md"), "w") as f:
            f.write("# Changelog\n\n## Unreleased\n### Highlights\n\n- big\n\n### Everything else\n\n- older (T-9999).\n")
        self.git("add", "CHANGELOG.md")
        self.git("commit", "-qm", "changelog")

    def lane(self, n, name, text=None, ac="true", when=None, tier="S", reviewed=True):
        tid = super().lane(n, name, text=text, ac=ac, when=when, tier=tier, reviewed=reviewed)
        self.record_clean_run(tid)  # the land holds a lane with no passing affected run at its tip
        return tid

    def record_clean_run(self, tid):
        p = c.find_project(self.repo)
        b = c.find_brief(p, tid)
        b.add_evidence(c.AFFECTED_LABEL + CLEAN_CMD, "✓ 10 tests", step=1, ran=True,
                       ts=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        c.save_brief(p, b)

    def returned(self, tid, what="Lane work"):
        r = self.hook("SubagentStop", {"agent_type": "sampo:fm-builder", "agent_id": "a1b2c3d4",
                                       "last_assistant_message": _report(tid, what)})
        self.assertEqual(r.returncode, 0, r.stderr)

    def audited(self, *ids):
        for tid in ids:
            self.fm("task", "audit", tid, "self", "one fm-reviewer pass on the branch", "no findings")

    def brief(self, tid):
        return c.find_brief(c.find_project(self.repo), tid)

    @contextlib.contextmanager
    def in_project(self):
        """T-5368: the sampo verbs train._sampo spawns find this test's project, as fm() gives its own subprocesses."""
        env = {"FOREMAN_HOME": self.home, "FOREMAN_INHIBITED": "1", "FOREMAN_DOCTOR_BENCH": "0", "FOREMAN_DOCTOR_BUILD": "0"}
        here = os.getcwd()
        os.chdir(self.repo)
        try:
            with mock.patch.dict(os.environ, env):
                yield
        finally:
            os.chdir(here)

    def head(self):
        return subprocess.run(["git", "-C", self.repo, "rev-parse", "HEAD"], capture_output=True, text=True,
                              check=True).stdout.strip()

    def conflict(self):
        """A lane and main that both add same.txt with other text: the conflict the land parks before any criteria run."""
        with open(os.path.join(self.repo, "same.txt"), "w") as f:
            f.write("main\n")
        a = self.lane(1, "same.txt", "lane\n", when=OLD)
        self.git("add", "same.txt")
        self.git("commit", "-qm", "main moves")
        return a

    def run_land(self, resolver, claim=None, on_held=None, watch=None, wait=None):
        """train.land in this process: the headless run is `resolver(tid, lane)`, the pilot's claim is taken as the detached
        pilot would take it, and the train calls are recorded (one list of ids per train, in order). claim(p, tid) replaces
        the claim-and-resolve (a resolve that never finishes); on_held runs once, the first time a claim reads as held (a
        lane accepted meanwhile); watch's claim is read at each train (self.held_at_train); wait replaces RESOLVE_WAIT_S."""
        from sampo import pilotsig
        seen, real_train, real_held = [], train.train, pilotsig.held
        pending = [on_held] if on_held else []
        self.held_at_train = []

        def trained(p, main, ids, *args, **kw):
            seen.append(list(ids))
            if watch:
                self.held_at_train.append(real_held(c.find_brief(p, watch)))
            return real_train(p, main, ids, *args, **kw)

        def held_watch(b):
            res = real_held(b)
            if res and pending:
                pending.pop()()
            return res

        def claim_and_resolve(p, tid):
            with c.lock(p.dir, timeout=5.0):
                b = c.find_brief(p, tid)
                b.meta["pilot_pid"] = str(os.getpid())
                b.meta["pilot_kind"] = pilotsig.KIND
                c.save_brief(p, b)
            return pilotsig.resolve_run(p, tid)

        def headless(p, tid, lane, agent, prompt, left):
            self.calls.append(tid)
            return resolver(tid, lane)
        bound = mock.patch.object(train, "RESOLVE_WAIT_S", wait, create=True) if wait is not None else contextlib.nullcontext()
        with bound, mock.patch.object(train, "train", trained), \
                mock.patch.object(pilotsig, "held", held_watch), \
                mock.patch.object(pilotsig, "resolve", side_effect=claim or claim_and_resolve), \
                mock.patch.object(pilotsig, "_headless", side_effect=headless), \
                mock.patch.object(pilotsig, "spawn", return_value=1), \
                mock.patch.object(pilotsig, "tick", return_value=[]), \
                mock.patch.object(pilotsig, "claude_found", return_value=True), \
                mock.patch.object(pilotsig, "budget_high", return_value=False), self.in_project():
            got = train.land(self.p, self.repo, None)
        return got, seen

    def test_a_conflict_park_is_resolved_and_merged_by_the_same_land_with_no_lead_call(self):
        a = self.conflict()
        b = self.lane(2, "b.txt", when=OLD)
        self.audited(a, b)
        self.returned(a, "Lane one")
        self.returned(b, "Lane two")
        main = self.head()

        def resolver(tid, lane):  # the resolver's run: trunk merged in, both sides kept, committed, its own checks run
            subprocess.run(["git", "-C", lane, "merge", "-q", "--no-edit", main], capture_output=True)
            with open(os.path.join(lane, "same.txt"), "w") as f:
                f.write("main\nlane\n")
            subprocess.run(["git", "-C", lane, "add", "same.txt"], check=True)
            subprocess.run(["git", "-C", lane, "commit", "-qm", "resolve the conflict"], check=True, capture_output=True)
            self.audited(tid)
            self.record_clean_run(tid)
            from sampo import pilotsig
            return pilotsig._Run(True, "")
        got, seen = self.run_land(resolver)
        self.assertEqual(self.calls, [a], "the resolver ran once, for the parked lane")
        self.assertEqual(seen, [[b], [a]], "the parked lane is re-queued into the same land's next train: " + str(seen))
        self.assertEqual([t for t, _ in got["parked"]], [], got)
        subjects = subprocess.run(["git", "-C", self.repo, "log", "--format=%s", "HEAD"], capture_output=True, text=True)
        self.assertIn("resolve the conflict", subjects.stdout, "the resolved lane's commit landed on trunk")

    def test_two_failed_tries_leave_a_hand_row_that_names_both_sides_hunks(self):
        a = self.conflict()
        self.audited(a)
        self.returned(a, "Lane one")
        from sampo import pilotsig
        got, _ = self.run_land(lambda tid, lane: pilotsig._Run(True, ""))  # a run that ends and changes nothing
        self.assertEqual(len(self.calls), 2, "two tries, then the pilot gives up: " + str(self.calls))
        self.assertEqual([t for t, _ in got["parked"]], [a], got)
        log = self.brief(a).section("Log")
        self.assertIn("gave up on the merge conflict after 2 tries", log)
        self.assertIn("hunks: same.txt (main L1; lane L1)", log, "the hand row names both sides' hunks: " + log)

    def test_overlapping_lanes_train_in_two_passes_with_the_merge_between(self):
        a = self.lane(1, "a1.py", "def shared_step():\n    return 1\n", when=OLD)
        b = self.lane(2, "b1.py", "def shared_step():\n    return 2\n", when=OLD)
        self.audited(a, b)
        self.returned(a, "Lane one")
        self.returned(b, "Lane two")
        got, seen = self.run_land(lambda tid, lane: self.fail("no resolve for a lane nothing parked"))
        self.assertEqual(seen, [[a], [b]], "an OVERLAP pair (both add shared_step) trains one after the other: " + str(seen))
        self.assertEqual([t for t, _ in got["parked"]], [], got)

    def test_the_resolve_wait_is_twenty_minutes_not_the_pilots_two_hours(self):  # T-6102 review
        self.assertEqual(train.RESOLVE_WAIT_S, 20 * 60, "a land waits for a running resolve 20 minutes at most")

    def test_a_lane_accepted_mid_wait_trains_without_waiting_for_a_resolve_that_never_finishes(self):  # T-6102 review
        """The resolve's claim stays held for the whole test (a stub that never finishes: its claim is held for 30 s more,
        far short of the pilot's cap), and the land waits for it only up to RESOLVE_WAIT_S (0.5 s here). A lane accepted
        while the land waits is trained at once, while the resolve still runs; the parked lane stays parked at the bound."""
        from sampo import pilotsig
        a = self.conflict()
        self.audited(a)
        self.returned(a, "Lane one")
        c_ = self.lane(3, "c.txt", "lane3\n", when=OLD)
        self.audited(c_)
        self.returned(c_, "Lane three")
        lane3 = os.path.join(self.tmp, "lane3")
        with open(os.path.join(lane3, "c.txt"), "w") as f:  # an edit not committed: not finished, so not queued at the start
            f.write("lane3 edited\n")

        def never_finishes(p, tid):
            with c.lock(p.dir, timeout=5.0):
                b = c.find_brief(p, tid)
                b.meta["pilot_kind"] = pilotsig.KIND
                b.meta["claude_starting"] = str(int(time.time() - pilotsig.VERB_WALL_S + 30))
                c.save_brief(p, b)

        def accept_c():  # `sampo lane accept` meanwhile: the edit is committed (the tree is clean), the lane is --first
            subprocess.run(["git", "-C", lane3, "checkout", "--", "c.txt"], check=True)
            train.first_ids(self.p, [c_])
        got, seen = self.run_land(lambda tid, lane: self.fail("no resolve runs: the claim never finishes"),
                                  claim=never_finishes, on_held=accept_c, watch=a, wait=0.5)
        self.assertEqual(seen, [[c_]], "the lane accepted mid-wait trains while the resolve still runs: " + str(seen))
        self.assertEqual(self.held_at_train, [True], "the resolve was still held at that train: " + str(self.held_at_train))
        self.assertEqual([t for t, _ in got["parked"]], [a], "the resolve that never finished stays parked at the bound: " + str(got))


if __name__ == "__main__":
    unittest.main()
