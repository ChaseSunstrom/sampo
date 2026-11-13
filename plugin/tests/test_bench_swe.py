"""T-3746: sampo bench swe runs SWE-bench Verified through Smith inside a per-instance docker container. Here the docker
CLI is a fake script whose "container" is a local folder, the model is scripted, and the dataset is two fixture rows."""
import json
import os
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from helpers import ForemanTestCase  # noqa: F401  (puts plugin/lib on sys.path)

from sampo import bench_swe, dockershim, models as m

FAKE = r'''#!/usr/bin/env python3
import os, subprocess, sys
root, log, a = os.environ["FAKE_ROOT"], os.environ["FAKE_LOG"], sys.argv[1:]
with open(log, "a") as f:
    f.write(" ".join(a[:4]) + "\n")
if a[0] == "pull" and os.environ.get("FAKE_PULL_FAIL"):
    print("manifest unknown", file=sys.stderr); sys.exit(1)
if a[0] == "image" and a[1] == "inspect":
    sys.exit(0 if os.environ.get("FAKE_HAVE") else 1)
if a[0] == "run":
    subprocess.run("git reset -q --hard && git clean -fdq", shell=True, cwd=root)
    print("cid123"); sys.exit(0)
if a[0] == "exec":
    i = a.index("-w"); cmd = a[i + 3:]
    secs = None
    if cmd[0] == "timeout":  # GNU timeout is the container's: the host (macOS) may have none, so the fake keeps the limit
        secs, cmd = float(cmd[3]), cmd[4:]
    try:
        p = subprocess.run(cmd, cwd=root, stdin=sys.stdin, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=secs)
    except subprocess.TimeoutExpired:
        sys.exit(124)
    sys.stdout.buffer.write(p.stdout); sys.exit(p.returncode)
sys.exit(0)
'''
ROWS = [{"instance_id": f"fake__calc-{i}", "repo": r, "base_commit": "HEAD", "version": "1.0",
         "problem_statement": "calc.add subtracts instead of adding",
         "test_patch": "diff --git a/test_calc.py b/test_calc.py\nnew file mode 100644\n--- /dev/null\n+++ b/test_calc.py\n"
                       "@@ -0,0 +1,9 @@\n+import unittest\n+import calc\n+\n+\n+class T(unittest.TestCase):\n"
                       "+    def test_add(self):\n+        self.assertEqual(calc.add(2, 3), 5)\n"
                       "+    def test_zero(self):\n+        self.assertEqual(calc.add(0, 0), 0)\n",
         "FAIL_TO_PASS": json.dumps(["test_add (test_calc.T)"]), "PASS_TO_PASS": json.dumps(["test_zero (test_calc.T)"])}
        for i, r in enumerate(["fake/calc", "fake/calc", "other/lib"])]


def tc(name, **args):
    return {"id": f"c{name}{len(args)}", "name": name, "args": args}


def reply(*calls):
    return {"text": "", "tool_calls": list(calls), "stop": "tool_calls", "usage": {"in": 10, "out": 5}}


def unittest_cmd(inst, files):
    return "python3 -m unittest -v test_calc"


class SweBench(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sweb-")
        self.root, self.log = os.path.join(self.tmp, "ctr"), os.path.join(self.tmp, "docker.log")
        os.makedirs(self.root)
        open(self.log, "w").close()
        g = lambda *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@x", *a], cwd=self.root,
                                      check=True, capture_output=True)
        with open(os.path.join(self.root, "calc.py"), "w") as f:
            f.write("def add(a, b):\n    return a - b\n")
        g("init", "-q"); g("add", "."); g("commit", "-qm", "base")
        self.docker = os.path.join(self.tmp, "docker")
        with open(self.docker, "w") as f:
            f.write(FAKE)
        os.chmod(self.docker, os.stat(self.docker).st_mode | stat.S_IEXEC)
        self.out = os.path.join(self.tmp, "out")
        env = {"SAMPO_DOCKER": self.docker, "FAKE_ROOT": self.root, "FAKE_LOG": self.log}
        p = mock.patch.dict(os.environ, env)
        p.start(); self.addCleanup(p.stop)
        # T-4541: the machine's real free disk is not this test's subject (36 GB free turned all of it red)
        d = mock.patch.object(dockershim, "free_gb", lambda path=None: 500.0)
        d.start(); self.addCleanup(d.stop)
        self.profile ={"name": "fake", "kind": "openai", "model": "m1", "tools": True, "context": 32000}
        p = mock.patch.object(bench_swe, "profile_for", lambda name: self.profile)
        p.start(); self.addCleanup(p.stop)

    def logged(self):
        with open(self.log) as f:
            return f.read()

    def run_swe(self, script, rows=None, **kw):
        replies = iter(script)
        with mock.patch.object(m, "complete", lambda *a, **k: next(replies)):
            kw.setdefault("harnesses", ("sampo",))
            return bench_swe.run(model="fake", n=kw.pop("n", 2), rows=rows or ROWS, out_dir=self.out,
                                 runner=unittest_cmd, **kw)

    FIX = reply(tc("edit", file="calc.py", old="a - b", new="a + b"))
    DONE = reply(tc("done", summary="fixed"))

    def test_a_fixed_instance_is_resolved_and_a_do_nothing_one_is_not(self):
        res = self.run_swe([self.FIX, self.DONE, self.DONE, self.DONE, self.DONE], n=2)
        by = {r["instance_id"]: r for r in res["results"]}
        self.assertEqual(len(by), 2, res)
        fixed = [r for r in by.values() if r["resolved"]]
        self.assertEqual(len(fixed), 1, res)
        self.assertEqual((fixed[0]["f2p"], fixed[0]["p2p"]), ([1, 1], [1, 1]))
        self.assertEqual(res["resolved"], 1)
        bad = next(r for r in by.values() if not r["resolved"])
        self.assertEqual(bad["f2p"], [0, 1])  # the F2P test still fails: graded by the instance's own tests
        self.assertIn("+    return a + b", fixed[0]["patch"])

    def test_results_and_trajectories_are_kept(self):
        res = self.run_swe([self.FIX, self.DONE], n=1)
        self.assertTrue(os.path.isfile(os.path.join(self.out, "results.jsonl")))
        with open(os.path.join(self.out, "results.jsonl")) as f:
            self.assertEqual(json.loads(f.readline())["instance_id"], res["results"][0]["instance_id"])
        traj = os.path.join(self.out, "trajectories", res["results"][0]["instance_id"] + ".sampo.jsonl")
        with open(traj) as f:
            text = f.read()
        self.assertEqual(res["results"][0]["harness"], "sampo")
        self.assertIn('"role": "tool"', text)
        self.assertIn("edit", text)

    def test_pulls_an_image_it_lacks_and_removes_it_after_but_keeps_one_it_found(self):
        self.run_swe([self.DONE], n=1)
        log = self.logged()
        self.assertIn("pull swebench/sweb.eval.x86_64.fake_1776_calc-0:latest", log)
        self.assertIn("rmi", log)
        self.assertIn("rm -f sampo-bench-", log)
        open(self.log, "w").close()
        os.environ["FAKE_HAVE"] = "1"
        self.addCleanup(os.environ.pop, "FAKE_HAVE", None)
        self.run_swe([self.DONE], n=1)
        self.assertNotIn("pull", self.logged())
        self.assertNotIn("rmi", self.logged())  # the user's own image is not ours to remove

    def test_the_default_pick_spans_repos(self):
        picked = bench_swe.pick(ROWS, 2)
        self.assertEqual({r["repo"] for r in picked}, {"fake/calc", "other/lib"})
        self.assertEqual(picked, bench_swe.pick(ROWS, 2))  # deterministic
        self.assertEqual(len(bench_swe.pick(ROWS, 99)), 3)

    def test_no_docker_stops_and_says_why(self):
        with mock.patch.dict(os.environ, {"SAMPO_DOCKER": os.path.join(self.tmp, "nope")}):
            with self.assertRaises(bench_swe.Stop) as e:
                self.run_swe([], n=1)
        self.assertIn("docker", str(e.exception))

    def test_the_disk_budget_stops_before_a_pull(self):
        with mock.patch.object(dockershim, "free_gb", lambda path=None: 12.0):
            res = self.run_swe([self.DONE], n=2)
        self.assertEqual(res["results"], [])
        self.assertIn("disk", res["stopped"])
        self.assertNotIn("pull", self.logged())

    def test_a_failed_pull_is_an_unresolved_instance_not_a_crash(self):
        os.environ["FAKE_PULL_FAIL"] = "1"
        self.addCleanup(os.environ.pop, "FAKE_PULL_FAIL", None)
        res = self.run_swe([], n=1)
        self.assertFalse(res["results"][0]["resolved"])
        self.assertIn("pull", res["results"][0]["reason"])

    def test_the_shim_edit_wants_one_match_and_refuses_paths_outside(self):
        with dockershim.Container("sampo-bench-t", "img", workdir="/testbed") as c:
            self.assertTrue(c.edit("calc.py", "return", "return  ").startswith("edited"))
            self.assertTrue(c.edit("calc.py", "zzz", "y").startswith("refused"))
            self.assertTrue(c.read("../etc/passwd", 1, 5).startswith("refused"))
            self.assertTrue(c.write("/etc/x", "y").startswith("refused"))
            self.assertIn("return", c.read("calc.py", 1, 5))
            self.assertIn("calc.py", c.search("def add", None))
            self.assertTrue(c.run("exit 3").startswith("exit 3"))

    def test_the_log_parser_reads_pytest_unittest_and_sympy_lines(self):
        out = ("PASSED t/a.py::test_a\nFAILED t/a.py::test_b - boom\n"
               "test_x (m.C.test_x) ... ok\ntest_y (m.C) ... FAIL\ntest_sym ok\ntest_sym2 F\n")
        got = bench_swe.parse_log(out)
        self.assertEqual(got["t/a.py::test_a"], "PASSED")
        self.assertEqual(got["t/a.py::test_b"], "FAILED")
        self.assertEqual(got["test_x (m.C)"], "PASSED")
        self.assertEqual(got["test_y (m.C)"], "FAILED")
        self.assertEqual((got["test_sym"], got["test_sym2"]), ("PASSED", "FAILED"))

    BARE_EDIT = reply(tc("edit", file="calc.py", old="a - b", new="a + b"))
    STOP = {"text": "fixed", "tool_calls": [], "stop": "stop", "usage": {"in": 10, "out": 5}}

    def test_both_harnesses_run_per_instance_on_one_pull_and_are_tagged(self):
        seen = []
        replies = iter([self.BARE_EDIT, self.STOP, self.FIX, self.DONE])

        def complete(profile, msgs, **k):
            seen.append((msgs, k.get("tools")))
            return next(replies)
        with mock.patch.object(m, "complete", complete):
            res = bench_swe.run(model="fake", n=1, rows=ROWS, out_dir=self.out, runner=unittest_cmd)
        by = {r["harness"]: r for r in res["results"]}
        self.assertEqual(set(by), {"bare", "sampo"})
        self.assertTrue(by["bare"]["resolved"] and by["sampo"]["resolved"], res)
        self.assertEqual(self.logged().count("pull "), 1)
        for h in by:
            self.assertTrue(os.path.isfile(os.path.join(self.out, "trajectories", f"fake__calc-0.{h}.jsonl")))
        bare_msgs, bare_tools = seen[0]  # the control: three tools and no Smith prompt
        self.assertEqual({t["name"] for t in bare_tools}, {"read", "edit", "run"})
        self.assertNotIn("one action at a time", json.dumps(bare_msgs))
        self.assertIn("subtracts instead of adding", json.dumps(bare_msgs))
        rows = bench_swe.compare(res["results"])
        self.assertEqual(rows["delta"]["resolved_pct"], 0)
        self.assertEqual(rows["bare"]["n"], 1)
        self.assertIn("steps", rows["sampo"])

    def test_a_reasoning_budget_failure_is_its_own_class(self):
        def complete(profile, msgs, **k):
            raise m.ModelError("fake: reasoning used the whole budget: no text in 8192 tokens (think: false)")
        with mock.patch.object(m, "complete", complete):
            res = bench_swe.run(model="fake", n=1, rows=ROWS, out_dir=self.out, runner=unittest_cmd)
        self.assertEqual({r["failure"] for r in res["results"]}, {"reasoning_budget"})

    def test_the_prompt_goes_through_smiths_own_phase(self):
        seen = []
        replies = iter([self.DONE])

        def complete(profile, msgs, **k):
            seen.append(msgs)
            return next(replies)
        with mock.patch.object(m, "complete", complete):
            bench_swe.run(model="fake", n=1, rows=ROWS, out_dir=self.out, runner=unittest_cmd, harnesses=("sampo",))
        self.assertIn("subtracts instead of adding", json.dumps(seen[0]))
        self.assertIn("one action at a time", seen[0][0]["content"])  # Smith's ACT_PROMPT


if __name__ == "__main__":
    unittest.main()
