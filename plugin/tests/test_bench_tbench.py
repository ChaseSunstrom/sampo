"""T-3747: sampo bench tbench runs Terminal-Bench tasks through Sampo's own agent in a container: the image built from the
task's Dockerfile, the agent acting through docker exec inside the task's time limit, the task's own tests grading it.
A fake docker (a script that keeps each "container" as a folder) and a two-task fixture set stand in for both."""
import json
import os
import shutil
import sys

from helpers import ForemanTestCase

from sampo import benchtb, core, dockershim, models

FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, shutil, subprocess, sys
D = os.environ["FAKE_DOCKER_DIR"]
a = sys.argv[1:]
with open(os.path.join(D, "log"), "a") as f:
    f.write(json.dumps(a) + "\n")
def fs(n): return os.path.join(D, "c", n, "fs")
def mapp(s, n):  # the container's /app, /tests, /logs live under its folder
    for top in ("/app", "/tests", "/logs"):
        s = s.replace(top, fs(n) + top)
    return s
cmd = a[0] if a else ""
if cmd in ("version", "info"):
    sys.exit(0)
if cmd == "build":
    tag, ctx = a[a.index("-t") + 1], a[-1]
    os.makedirs(os.path.join(D, "i"), exist_ok=True)
    shutil.copytree(ctx, os.path.join(D, "i", tag.replace(":", "_")), dirs_exist_ok=True)
    sys.exit(0)
if cmd == "run":
    n = a[a.index("--name") + 1]
    os.makedirs(os.path.join(fs(n), "app")); os.makedirs(os.path.join(fs(n), "logs"))
    print("cid"); sys.exit(0)
if cmd == "exec":
    rest = a[1:]; wd = "/"
    while rest[0] in ("-i", "-w"):
        if rest[0] == "-w": wd = rest[1]; rest = rest[2:]
        else: rest = rest[1:]
    n, argv = rest[0], rest[1:]
    os.makedirs(fs(n) + wd, exist_ok=True)
    os.chdir(fs(n) + wd)
    os.execvp(argv[0], [argv[0]] + [mapp(x, n) for x in argv[1:]])
if cmd == "cp":
    src, dst = a[1], a[2]
    if ":" in src and not src.startswith("/"):
        n, p = src.split(":", 1); src = mapp(p, n)
    if ":" in dst and not dst.startswith("/"):
        n, p = dst.split(":", 1); dst = mapp(p, n); os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.isdir(src): shutil.copytree(src, dst, dirs_exist_ok=True)
    else: shutil.copy(src, dst)
    sys.exit(0)
if cmd == "rm":
    shutil.rmtree(os.path.join(D, "c", a[-1]), ignore_errors=True); sys.exit(0)
if cmd == "rmi":
    shutil.rmtree(os.path.join(D, "i", a[-1].replace(":", "_")), ignore_errors=True); sys.exit(0)
if cmd == "ps":
    d = os.path.join(D, "c"); print("\n".join(sorted(os.listdir(d)) if os.path.isdir(d) else [])); sys.exit(0)
sys.exit(2)
'''

LEGACY = {
    "hello": ("software-engineering", 30, "Create hello.txt in /app holding the word hello."),
    "stuck": ("system-administration", 1, "Do something slow."),
}
RUN_TESTS = 'test "$(cat hello.txt)" = hello\n'  # the fake maps only argv: scripts use paths relative to /app


def make_tasks(root):
    """A legacy-layout fixture set (task.yaml, Dockerfile, run-tests.sh, tests/) and one 2.0-layout task."""
    for name, (cat, secs, text) in LEGACY.items():
        d = os.path.join(root, name)
        os.makedirs(os.path.join(d, "tests"))
        with open(os.path.join(d, "task.yaml"), "w") as f:
            f.write(f"instruction: |-\n  {text}\n\nauthor_name: x\ndifficulty: easy\ncategory: {cat}\n"
                    f"max_agent_timeout_sec: {secs}\nmax_test_timeout_sec: 20\n")
        with open(os.path.join(d, "Dockerfile"), "w") as f:
            f.write("FROM alpine\nWORKDIR /app\n")
        with open(os.path.join(d, "run-tests.sh"), "w") as f:
            f.write(RUN_TESTS)
        with open(os.path.join(d, "tests", "test_outputs.py"), "w") as f:
            f.write("# graded by run-tests.sh in this fixture\n")
    d = os.path.join(root, "v2")  # Terminal-Bench 2.0 layout: task.toml, instruction.md, environment/, tests/test.sh
    os.makedirs(os.path.join(d, "environment"))
    os.makedirs(os.path.join(d, "tests"))
    with open(os.path.join(d, "task.toml"), "w") as f:
        f.write('[metadata]\ncategory = "file-operations"\n[agent]\ntimeout_sec = 30.0\n[verifier]\ntimeout_sec = 20.0\n')
    with open(os.path.join(d, "instruction.md"), "w") as f:
        f.write("Create hello.txt in /app holding the word hello.\n")
    with open(os.path.join(d, "environment", "Dockerfile"), "w") as f:
        f.write("FROM alpine\nWORKDIR /app\n")
    with open(os.path.join(d, "tests", "test.sh"), "w") as f:
        f.write('if [ "$(cat hello.txt)" = hello ]; then echo 1 > ../logs/verifier/reward.txt; '
                'else echo 0 > ../logs/verifier/reward.txt; fi\n')


def call(name, **args):
    return {"text": "", "tool_calls": [{"id": "c1", "name": name, "args": args}], "usage": {"in": 5, "out": 3},
            "stop": "tool_use"}


def fake_model(profile, messages, tools=None, **kw):
    """Solves a hello task in two turns (run, done); on 'slow' just sleeps past the limit."""
    first = next(m["content"] for m in messages if m["role"] == "user")
    turns = sum(m["role"] == "assistant" for m in messages)
    if "slow" in first:
        return call("run", cmd="sleep 5")
    if turns == 0:
        return call("run", cmd="echo hello > hello.txt")
    if all(t["name"] != "done" for t in tools or []):  # the bare control has only `run`: it ends by answering
        return {"text": "made hello.txt", "tool_calls": [], "usage": {"in": 5, "out": 3}, "stop": "end"}
    return call("done", summary="made hello.txt")


class Tbench(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fake = os.path.join(self.tmp, "fake")
        bindir = os.path.join(self.tmp, "fakebin")
        os.makedirs(bindir)
        os.makedirs(self.fake)
        with open(os.path.join(bindir, "docker"), "w") as f:
            f.write(FAKE_DOCKER)
        os.chmod(os.path.join(bindir, "docker"), 0o755)
        self.tasks = os.path.join(self.tmp, "tasks")
        make_tasks(self.tasks)
        self.out = os.path.join(self.tmp, "out")
        self.saved = (os.environ.copy(), models.complete, models.resolve)
        os.environ["PATH"] = bindir + os.pathsep + os.environ["PATH"]
        os.environ["FAKE_DOCKER_DIR"] = self.fake
        os.environ["SAMPO_BENCH_MIN_FREE_GB"] = "0"
        models.complete = fake_model
        models.resolve = lambda name=None: {"name": name or "fake", "kind": "openai", "model": "fake", "tools": True,
                                            "context": 8192}

    def tearDown(self):
        env, models.complete, models.resolve = self.saved
        os.environ.clear()
        os.environ.update(env)
        super().tearDown()

    def log(self):
        with open(os.path.join(self.fake, "log")) as f:
            return [json.loads(x) for x in f]

    def test_the_fixture_set_loads_both_layouts_and_subsets_by_category(self):
        ts = benchtb.load_tasks(self.tasks)
        self.assertEqual([t["id"] for t in ts], ["hello", "stuck", "v2"])
        self.assertEqual(ts[1]["agent_sec"], 1)
        self.assertEqual(ts[2]["agent_sec"], 30)
        self.assertIn("hello", ts[2]["instruction"])
        self.assertEqual([t["id"] for t in benchtb.pick(ts, category="file-operations")], ["v2"])
        self.assertEqual(len(benchtb.pick(ts, n=2)), 2)
        self.assertEqual(len(benchtb.pick(ts, n=None)), 3)

    def test_two_tasks_run_end_to_end_one_solved_one_out_of_time(self):
        res = benchtb.run_bench("fake", self.tasks, self.out, n=2, only=["hello", "stuck"])  # both harnesses by default
        by = {(r["id"], r["harness"]): r for r in res["tasks"]}
        self.assertEqual(len(by), 4)
        for h in ("bare", "sampo"):
            with open(os.path.join(self.out, f"hello.{h}.jsonl")) as f:
                self.assertTrue(by["hello", h]["passed"], f.read())
            self.assertEqual(by["hello", h]["ended"], "done")
            self.assertFalse(by["stuck", h]["passed"])
            self.assertEqual(by["stuck", h]["ended"], "agent_timeout")
        self.assertEqual(res["passed"], {"bare": 1, "sampo": 1})
        self.assertEqual(res["n"], 2)
        for tid in ("hello", "stuck"):
            for h in ("bare", "sampo"):
                with open(os.path.join(self.out, f"{tid}.{h}.jsonl")) as f:
                    events = [json.loads(x) for x in f]
                self.assertEqual((events[0]["event"], events[0]["harness"]), ("start", h))
                self.assertTrue(any(e["event"] == "action" for e in events))
        with open(os.path.join(self.out, "results.json")) as f:
            self.assertEqual(json.load(f)["passed"], {"bare": 1, "sampo": 1})
        # every container and image was Sampo's own and is gone after use
        log = self.log()
        names = [x[x.index("--name") + 1] for x in log if x[0] == "run"]
        self.assertTrue(names and all(n.startswith("sampo-bench-") for n in names))
        tags = [x[x.index("-t") + 1] for x in log if x[0] == "build"]
        self.assertTrue(tags and all(t.startswith("sampo-bench-") for t in tags))
        self.assertEqual(sorted(x[-1] for x in log if x[0] == "rm"), sorted(names))
        self.assertEqual(sorted(x[-1] for x in log if x[0] == "rmi"), sorted(tags))
        self.assertFalse(os.listdir(os.path.join(self.fake, "c")))

    def test_a_2_0_layout_task_is_graded_by_its_reward_file(self):
        res = benchtb.run_bench("fake", self.tasks, self.out, only=["v2"])
        self.assertTrue(res["tasks"][0]["passed"], res)

    def test_one_harness_can_be_asked_for_and_runs_lists_both_side_by_side_with_the_delta(self):
        res = benchtb.run_bench("fake", self.tasks, self.out, only=["hello"], harnesses=("bare",))
        self.assertEqual([r["harness"] for r in res["tasks"]], ["bare"])
        os.makedirs(os.path.join(self.tmp, "state"), exist_ok=True)
        run_dir = os.path.join(core.state_dir(), "bench", "tbench", "20260101-000000")
        both = benchtb.run_bench("fake", self.tasks, run_dir, only=["hello"])
        self.assertIn("delta +0", benchtb.table(both))
        r = self.fm("bench", "runs", "--json")
        row = json.loads(r.stdout)["runs"][0]
        self.assertEqual((row["passed"], row["harnesses"]), ({"bare": 1, "sampo": 1}, ["bare", "sampo"]))
        self.assertIn("bare 1/1  sampo 1/1  delta +0", row["table"])

    def test_docker_absent_stops_with_a_reason(self):
        os.environ["PATH"] = os.path.join(self.tmp, "empty")
        with self.assertRaises(benchtb.Stop) as cm:
            benchtb.run_bench("fake", self.tasks, self.out, n=1)
        self.assertIn("docker", str(cm.exception))
        self.assertFalse(os.path.exists(os.path.join(self.out, "results.json")))

    def test_too_little_free_disk_stops_before_any_build(self):
        os.environ["SAMPO_BENCH_MIN_FREE_GB"] = "99999999"
        with self.assertRaises(benchtb.Stop) as cm:
            benchtb.run_bench("fake", self.tasks, self.out, n=1)
        self.assertIn("free", str(cm.exception))
        self.assertFalse([x for x in self.log() if x[0] == "build"] if os.path.exists(os.path.join(self.fake, "log")) else [])

    def test_the_shim_runs_reads_and_writes_in_a_named_container(self):
        shutil.copytree(os.path.join(self.tasks, "hello"), os.path.join(self.tmp, "ctx"))
        dockershim.build("sampo-bench-t", os.path.join(self.tmp, "ctx"))
        with dockershim.Container("sampo-bench-shim", "sampo-bench-t", workdir="/app", prefix="") as box:
            self.assertEqual(box.store("/app/a/b.txt", "héllo\n")[0], True)
            self.assertEqual(box.fetch("/app/a/b.txt"), "héllo\n")
            code, out = box.exec("pwd; cat a/b.txt", seconds=10)
            self.assertEqual(code, 0)
            self.assertIn("héllo", out)
            code, out = box.exec("sleep 5", seconds=1)
            self.assertIsNone(code)
        self.assertFalse(os.listdir(os.path.join(self.fake, "c")), "removed on exit")
        with self.assertRaises(ValueError):
            dockershim.build("not-ours", os.path.join(self.tmp, "ctx"))
        self.assertTrue(dockershim.Container("not-ours", "sampo-bench-t").name.startswith(dockershim.PREFIX))

    def test_the_cli_refuses_with_a_reason_when_docker_is_absent(self):
        r = self.fm("bench", "tbench", "--model", "fake", "--n", "1", "--tasks", self.tasks, check=False,
                    env={"PATH": os.path.join(self.tmp, "empty")})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("docker", r.stdout + r.stderr)
