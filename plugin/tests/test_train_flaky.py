"""T-4289: a merge train drops no lane for a test that is red without any of them. A class red in the gate that passes
alone on the merged tree is flaky under load (recorded, the train goes on); one red on the base itself, in the gate's own
environment, stops the train and puts back the lanes already dropped for it; a gate's test classes never inherit the
caller's FM_STALL_ or similar overrides, and a detached land drops them and says so."""
# map-links: plugin/lib/sampo/train.py, plugin/tests/run.py
import contextlib
import io
import json
import os
import unittest
from unittest import mock

import run
import test_lane_train as tlt

# the suite runner's output as tests/run.py words it: each class in its own process, then `failed class:` lines and a count
SUITE_PY = '''import importlib, os, subprocess, sys, unittest
names = sys.argv[1:] or sorted(f[:-3] for f in os.listdir("plugin/tests") if f.startswith("test_") and f.endswith(".py"))
if os.path.exists("crash.flag") and sys.argv[1:] and not os.path.exists("b.txt"):
    sys.exit(124)  # a run that names no class and says nothing: a timeout or a crash
jobs = []
for n in names:
    if "." in n:
        jobs.append(n)
        continue
    mod = importlib.import_module(n)
    jobs += [n + "." + k for k, v in vars(mod).items() if isinstance(v, type) and issubclass(v, unittest.TestCase)]
bad = []
for j in jobs:
    r = subprocess.run([sys.executable, "-m", "unittest", j], capture_output=True, text=True)
    if r.returncode:
        print(r.stderr)
        bad.append(j)
for j in bad:
    print("  failed class: " + j)
if bad:
    print("FAILED (%d classes: %s)" % (len(bad), ", ".join(bad)))
    sys.exit(1)
'''
OK = tlt.OK
FLAKY = ("import os, unittest\n\n\nclass Flaky(unittest.TestCase):\n    def test_it(self):\n"
         "        first = not os.path.exists({0!r})\n        open({0!r}, 'w').close()\n"
         "        self.assertFalse(first, 'a load flake')\n")
STRONG = "import unittest\n\n\nclass Strong(unittest.TestCase):\n    def test_it(self):\n        self.fail('this lane breaks it')\n"
ENV = ("# map-links: a.txt c.txt\nimport os, unittest\n\n\nclass Env(unittest.TestCase):\n    def test_it(self):\n"
       "        self.assertFalse(os.environ.get('GATE_ENV_RED'), 'red in this environment{0}')\n")


# a lane's own pollution: red in a whole gate run (module names, no class), green alone (a class name): the shape of a
# class a lane's new test breaks by shared state, which a rerun of the whole gate is the only thing to see
VICTIM = ("# map-links: poll.txt\nimport os, unittest\n\nSUITE = {0!r}\n\n\nclass Victim(unittest.TestCase):\n"
          "    def test_it(self):\n        last = open(SUITE).read().splitlines()[-1].split(' ', 1)[1]\n"
          "        polluted = os.path.exists('poll.txt') and '.' not in last\n"
          "        self.assertFalse(polluted, 'state left by another test')\n")
# class Fk is red in the k-th whole gate run of the train and green alone: a different one each round
CAP = ("# map-links: a.txt\nimport unittest\n\nSUITE = {0!r}\n\n\ndef red_in(k):\n"
       "    lines = open(SUITE).read().splitlines()\n    whole = [x for x in lines if x.startswith('sampo-train-') and '.' not in x.split(' ', 1)[1]]\n"
       "    return '.' not in lines[-1].split(' ', 1)[1] and len(whole) == k\n\n\n"
       + "".join(f"class F{k}(unittest.TestCase):\n    def test_it(self):\n        self.assertFalse(red_in({k}), 'load')\n\n\n"
                 for k in range(1, 5)))
# red only with lane b's file in the tree, and says nothing of it: no rule names the lane, the map link only touches
XCLASS = ("# map-links: b.txt\nimport os, unittest\n\n\nclass X(unittest.TestCase):\n    def test_it(self):\n"
          "        self.assertFalse(os.path.exists('b' + '.txt'), 'boom')\n")  # never the file's name in the failure text
# red on the base (GATE_ENV_RED); the failure names the first of c.txt, d.txt in the tree: one lane at a time
ENV2 = ("# map-links: a.txt c.txt d.txt\nimport os, unittest\n\nNAMES = ('c.txt', 'd.txt')\n\n\nclass Env(unittest.TestCase):\n    def test_it(self):\n"
        "        msg = 'red in this environment: ' + ' '.join([n for n in NAMES if os.path.exists(n)][:1])\n"
        "        self.assertFalse(os.environ.get('GATE_ENV_RED'), msg)\n")


class Gate(tlt.TrainCase):
    """The train's suite gate on a runner worded as tests/run.py words its output; one rule test at the base."""
    def setUp(self):
        super().setUp()
        self.suite = os.path.join(self.tmp, "suite-runs")
        self.marker = os.path.join(self.tmp, "flaked")
        os.makedirs(os.path.join(self.repo, "plugin", "tests"))
        files = {"suite.sh": f'echo "${{PWD##*/}} $*" >> {self.suite}\nenv | grep "^FM_" >> {self.suite}.env\n'
                             'PYTHONPATH=plugin/tests exec python3 suite.py "$@"\n',
                 "suite.py": SUITE_PY, "plugin/tests/test_doctor.py": OK.format("Doctor"),
                 "plugin/tests/test_rename_aliases.py": OK.format("Aliases")}
        for rel, text in files.items():
            with open(os.path.join(self.repo, rel), "w") as f:
                f.write(text)
        self.git("add", "-A")
        self.git("commit", "-qm", "a suite")
        self.fm("check", "add", "sh suite.sh")
        self.fm("check", "affected", "sh suite.sh {names}")
        from sampo import core as c
        self.p = c.find_project(self.repo)

    def base(self, rel, text):
        with open(os.path.join(self.repo, rel), "w") as f:
            f.write(text)
        self.git("add", rel)
        self.git("commit", "-qm", f"base: {rel}")

    def gate_args(self):
        with open(self.suite) as f:
            return [ln.split(" ", 1)[1].strip() for ln in f if ln.startswith("sampo-train-")]

    def events(self, kind):
        from sampo import core as c
        return [e["data"] for e in c.ledger_events(self.p, (kind,))]

    def landed(self, name):
        return os.path.exists(os.path.join(self.repo, name))

    def green_env(self):
        """The last train's gate went green in this environment: no base check at the start, as in steady state."""
        from sampo import train
        train._mark_green(self.p)


class Flaky_(Gate):
    def test_flaky_alone_a_class_red_in_the_gate_and_green_alone_drops_no_lane(self):
        a = self.lane(1, "test_flaky.py", FLAKY.format(self.marker))
        b = self.lane(2, "test_b.py", OK.format("B"))
        r = self.train(a, b)
        self.assertIn("flaky under load", r.stdout)
        self.assertIn("test_flaky.Flaky", r.stdout)
        self.assertNotIn("left out", r.stdout)
        for name in ("test_flaky.py", "test_b.py"):
            self.assertTrue(self.landed(name), name)
        for tid in (a, b):
            self.assertTrue(self.meta(tid)["lane_stop"].startswith("merged"), tid)
        args = self.gate_args()
        self.assertEqual(args[1], "test_flaky.Flaky", "one probe of the red class alone")
        self.assertEqual(len(args), 3, args)
        self.assertNotIn(".", args[2], "then the whole gate again, uncached, before anything lands (T-4289 review)")
        self.assertIn("recorded in flakes.json: test_flaky.Flaky.test_it", r.stdout)
        got = self.events("lane_train_flaky")
        self.assertEqual([(e["classes"], sorted(e["lanes"])) for e in got], [(["test_flaky.Flaky"], sorted([a, b]))])
        with open(os.path.join(self.p.dir, "flakes.json")) as f:
            self.assertIn("test_flaky.Flaky.test_it", json.load(f)["flaky"], "the flaky record doctor's rates read")

    def test_a_lane_that_really_breaks_a_class_is_still_dropped_beside_a_flaky_one(self):
        a = self.lane(1, "test_flaky.py", FLAKY.format(self.marker))
        b = self.lane(2, "test_strong.py", STRONG)
        r = self.train(a, b)
        self.assertIn(f"left out {b}: its merge turns the gates red", r.stdout)
        self.assertIn("it changed test_strong.py, the failing test", r.stdout)
        self.assertNotIn(f"left out {a}", r.stdout)
        self.assertIn("flaky under load: test_flaky.Flaky", r.stdout)
        self.assertTrue(self.landed("test_flaky.py"))
        self.assertFalse(self.landed("test_strong.py"))
        self.assertEqual([e["classes"] for e in self.events("lane_train_flaky")], [["test_flaky.Flaky"]])

    def test_a_lane_whose_pollution_passes_alone_is_not_called_flaky_when_the_whole_gate_is_red_again(self):
        """T-4289 review: a class red in the whole gate that passes alone could be a lane's shared state: before anything
        lands the whole gate runs again, uncached, and a class red again is no flake: the lane is left out, no flake is
        recorded for it."""
        self.green_env()
        self.base("plugin/tests/test_victim.py", VICTIM.format(self.suite))
        a, b = self.lane(1, "a.txt"), self.lane(2, "poll.txt")
        r = self.train(a, b)
        self.assertIn(f"left out {b}: its merge turns the gates red", r.stdout)
        self.assertNotIn(f"left out {a}", r.stdout)
        self.assertTrue(self.landed("a.txt"))
        self.assertFalse(self.landed("poll.txt"))
        full = [x for x in self.gate_args() if "." not in x]
        self.assertGreaterEqual(len(full), 2, f"a whole gate ran again after the flaky clearing: {self.gate_args()}")
        flakes = os.path.join(self.p.dir, "flakes.json")
        if os.path.exists(flakes):
            with open(flakes) as f:
                self.assertNotIn("test_victim.Victim.test_it", json.load(f).get("flaky", {}))

    def test_flaky_classes_past_the_cap_stop_the_train_and_drop_no_lane(self):
        """T-4289 review: a different class red in each whole run, each green alone: after FLAKY_ROUNDS the train stops
        ('flaky, cap reached'), nothing is judged as a lane's, nothing lands."""
        from sampo import train
        self.green_env()
        self.base("plugin/tests/test_cap.py", CAP.format(self.suite))
        r = self.train(self.lane(1, "a.txt"), check=False)
        out = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 0, out)
        self.assertIn("flaky, cap reached", out)
        self.assertIn(f"test_cap.F{train.FLAKY_ROUNDS + 1}", out)
        self.assertNotIn("left out", out)
        self.assertFalse(self.landed("a.txt"))
        self.assertEqual([e["classes"] for e in self.events("lane_train_flaky_cap")], [[f"test_cap.F{train.FLAKY_ROUNDS + 1}"]])

    def test_a_note_says_recorded_only_when_the_failure_named_a_test(self):
        """T-4289 review: _note_flaky returns the ids it wrote: none for output that names no failing test."""
        from sampo import train
        self.assertEqual(train._note_flaky(self.p, "sh suite.sh", "tree1", "timed out after 9s\n", ["test_x.X"]), [])
        got = train._note_flaky(self.p, "sh suite.sh", "tree1", "FAIL: test_it (test_x.X.test_it)\n", ["test_x.X"])
        self.assertEqual(got, ["test_x.X.test_it"])

    def test_a_class_that_fails_alone_too_is_no_flake(self):
        """Pins behaviour that must not change (it passes on the code before T-4289 too): a class that fails alone on the
        merged tree as well is the lane's, never flaky, and the old rules drop the lane."""
        b = self.lane(1, "test_strong.py", STRONG)
        r = self.train(b, check=False)
        self.assertNotIn("flaky under load", r.stdout + r.stderr)
        self.assertEqual(self.events("lane_train_flaky"), [])
        self.assertFalse(self.landed("test_strong.py"))


class RedOnBase(Gate):
    def setUp(self):
        super().setUp()
        self.red = {"GATE_ENV_RED": "1"}  # the gate's own environment: red on the base too
        self.green_env()

    def test_red_on_base_drops_no_lane_and_stops_the_train(self):
        self.base("plugin/tests/test_env.py", ENV.format(""))
        a, b, d = self.lane(1, "a.txt"), self.lane(2, "b.txt"), self.lane(3, "d.txt")
        r = self.train(a, b, d, check=False, env=self.red)
        out = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 0, out)
        self.assertIn("red on the base", out)
        self.assertIn("test_env.Env", out)
        self.assertNotIn("left out", out)
        for name in ("a.txt", "b.txt", "d.txt"):
            self.assertFalse(self.landed(name), name)
        for tid in (a, b, d):
            self.assertNotIn("left out of a merge train", self.fm("task", "show", tid).stdout, tid)
        self.assertEqual([e["classes"] for e in self.events("lane_train_base_red")], [["test_env.Env"]])

    def test_red_on_base_puts_back_the_lanes_already_dropped_for_it(self):
        """Round one blames lane b for the class on the strength of a file its failure names (c.txt); the confirm is red
        again with no lane left to name, the bisection ends at the base, and b goes back: the class was never its."""
        self.base("plugin/tests/test_env.py", ENV.format(": c.txt"))
        a, b, d = self.lane(1, "a.txt"), self.lane(2, "c.txt"), self.lane(3, "d.txt")
        r = self.train(a, b, d, check=False, env=self.red)
        out = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 0, out)
        self.assertIn("red on the base", out)
        for name in ("a.txt", "c.txt", "d.txt"):
            self.assertFalse(self.landed(name), name)
        log = self.fm("task", "show", b).stdout
        self.assertIn("left out of a merge train", log, "it was dropped in round one")
        self.assertIn("put back: test_env.Env is red on the base", log, "and put back when the base was found red")
        self.assertNotIn("put back", self.fm("task", "show", a).stdout, "a lane never dropped has nothing to put back")
        got = self.events("lane_train_base_red")
        self.assertEqual([(e["classes"], e["restored"]) for e in got], [(["test_env.Env"], [b])])

    def test_a_second_lane_dropped_for_a_class_already_dropped_for_is_checked_against_the_base(self):
        """T-4289 review (the incident): strong blame names lane b for the class, then, with b out, lane d for the same
        class, and so on while the base is red: the second drop probes the base first, finds it red, stops the train and
        puts b back; d is never dropped."""
        self.base("plugin/tests/test_env.py", ENV2)
        a, b, d = self.lane(1, "a.txt"), self.lane(2, "c.txt"), self.lane(3, "d.txt")
        r = self.train(a, b, d, check=False, env=self.red)
        out = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 0, out)
        self.assertIn("red on the base", out)
        for name in ("a.txt", "c.txt", "d.txt"):
            self.assertFalse(self.landed(name), name)
        self.assertIn("put back: test_env.Env is red on the base", self.fm("task", "show", b).stdout)
        self.assertNotIn("left out of a merge train", self.fm("task", "show", d).stdout, "d was never dropped for it")
        self.assertEqual([(e["classes"], e["restored"]) for e in self.events("lane_train_base_red")],
                         [(["test_env.Env"], [b])])

    def test_a_class_a_lane_breaks_and_the_base_passes_leaves_the_lane_out_not_the_train(self):
        """T-4289 review: the lane's own break (X red only with its file in) where the base is green: dropped as before,
        with the base probed and found green (a weak blame: a map link only)."""
        self.base("plugin/tests/test_x.py", XCLASS)
        a, b = self.lane(1, "a.txt"), self.lane(2, "b.txt")
        r = self.train(a, b)
        self.assertIn(f"left out {b}: its merge turns the gates red", r.stdout)
        self.assertNotIn("red on the base", r.stdout)
        self.assertTrue(self.landed("a.txt"))
        self.assertFalse(self.landed("b.txt"))
        self.assertEqual(self.events("lane_train_base_red"), [])

    def test_a_base_probe_that_names_no_class_is_inconclusive_and_drops_no_lane(self):
        """T-4289 review: the base probe ends with a timeout's code and says nothing: not 'red on the base' (no class),
        not a reason to drop or keep a lane: the train stops and says the probe was inconclusive."""
        self.base("plugin/tests/test_x.py", XCLASS)
        self.base("crash.flag", "")
        r = self.train(self.lane(1, "b.txt"), check=False)
        out = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 0, out)
        self.assertIn("base probe inconclusive", out)
        self.assertNotIn("red on the base", out)
        self.assertNotIn("left out", out)
        self.assertFalse(self.landed("b.txt"))
        self.assertEqual(self.events("lane_train_base_red"), [])

    def test_a_lane_left_out_by_a_bisect_is_recorded_for_the_classes_proven_alone_only(self):
        """T-4289 review: a whole-gate bisect proves no class alone: nothing is recorded for the lane to be put back by."""
        from sampo import train
        self.assertEqual(train._left_for("", []), set(), "a bisection with no class proven alone: nothing to be put back by")
        self.assertEqual(train._left_for("", ["A"]), {"A"})
        self.assertEqual(train._left_for("B", ["A"]), {"B"}, "a blamed class is the lane's own")


class Scrub(Gate):
    def test_run_py_children_never_inherit_a_callers_override(self):
        from sampo import core as c
        keep = {"FM_STALL_CACHE_S": "0", "FM_STALL_MIN": "0", "FM_HEARTBEAT": "0.1", "FM_STALL": "1", "FM_POSTMERGE_CMD": "x",
                "FM_PROMPT_VARIANT": "v", "FM_DOOR_CAP": "1", "FM_GUARD_SCRIPT_BUDGET": "0", "FM_ABLATE_BUDGET": "0",
                "FM_PROVE_ALLOW": "/x", "FM_RUN_DEADLINE": "99", "FM_TEST_TIMES": "/t", "PIP_QUIET": "1"}
        with mock.patch.dict(os.environ, keep):
            env = run.child_env("/tmp/x")
        self.assertEqual({k for k in keep if k in env}, {"FM_RUN_DEADLINE", "FM_TEST_TIMES", "PIP_QUIET"},
                         "a run's own switch stays; a test's override and the operator's post-merge setting go")
        self.assertEqual(run.override_prefixes(), c.TEST_OVERRIDES, "run.py reads the lib's list: one list, in one place")
        for name in ("FM_POSTMERGE_CMD", "FM_POSTMERGE_RERUN"):
            self.assertIn(name, c.TEST_OVERRIDES)
        self.assertEqual(c.override_names({"FM_STALL_MIN": "1", "FM_HEARTBEAT": "1", "FM_RUN_DEADLINE": "1", "PATH": "x"}),
                         ["FM_HEARTBEAT", "FM_STALL_MIN"])
        self.assertEqual(c.scrubbed_env({"FM_POSTMERGE_CMD": "x", "FM_STALL": "1", "PATH": "p"}, keep=("FM_POSTMERGE",)),
                         {"FM_POSTMERGE_CMD": "x", "PATH": "p"})

    def test_the_trains_own_gate_run_takes_no_override_from_its_caller(self):
        """T-4289 review: the gate the train runs itself (a foreground land has no spawn to scrub) passes its tests no
        FM_STALL_ or the like (the post-merge names go the same way: the scrubbed_env unit above and the ship below)."""
        self.green_env()
        a = self.lane(1, "a.txt")
        self.train(a, env={"FM_STALL_MIN": "0", "FM_HEARTBEAT": "0.1", "FM_MARK": "kept"})
        with open(self.suite + ".env") as f:
            seen = f.read()
        self.assertIn("FM_MARK=kept", seen, "the log of the gate's FM_ environment works: an ordinary one reaches it")
        self.assertNotIn("FM_STALL_MIN", seen)
        self.assertNotIn("FM_HEARTBEAT", seen)

    def test_a_ship_spawned_by_a_land_takes_no_override_from_its_caller(self):
        from sampo import train
        env = {"FM_STALL_MIN": "0", "FM_POSTMERGE_CMD": "true", "FOREMAN_SHIP_TIMEOUT_S": "5"}
        with mock.patch.dict(os.environ, env), mock.patch.object(train.subprocess, "Popen") as popen:
            popen.return_value.wait.return_value = 0
            popen.return_value.pid = 1
            with contextlib.suppress(Exception):
                train._ship_run(self.repo, "T-1", "abc123")
        given = popen.call_args.kwargs["env"]
        self.assertNotIn("FM_STALL_MIN", given)
        self.assertNotIn("FM_POSTMERGE_CMD", given)
        self.assertEqual(given["FOREMAN_SHIP_AT"], "abc123")

    def test_a_detached_land_drops_a_callers_overrides_and_says_so(self):
        from sampo import core as c, train
        p = c.find_project(self.repo)
        env = {"FM_STALL_CACHE_S": "0", "FM_HEARTBEAT": "0.1", "FOREMAN_HOME": os.environ.get("FOREMAN_HOME", "")}
        with mock.patch.dict(os.environ, env), mock.patch.object(train.subprocess, "Popen") as popen, \
                mock.patch.object(c, "panicked", return_value=False):
            os.environ.pop("FOREMAN_NO_BACKGROUND", None)
            popen.return_value.pid = 4242
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                pid, log = train.spawn_land(p, self.repo)
        self.assertEqual(pid, 4242)
        given = popen.call_args.kwargs["env"]
        self.assertNotIn("FM_STALL_CACHE_S", given)
        self.assertNotIn("FM_HEARTBEAT", given)
        self.assertEqual(given["FOREMAN_LAND_STAMP"], "1")
        with open(log) as f:
            said = f.read()
        self.assertIn("dropped FM_HEARTBEAT, FM_STALL_CACHE_S from the land's environment", said)
        self.assertIn("never reach a gate", said)

    def test_a_detached_land_with_no_override_says_nothing(self):
        """Pins behaviour that must not change (it passes on the code before T-4289 too): a land spawned with no test
        override in its environment says nothing of dropping any."""
        from sampo import core as c, train
        p = c.find_project(self.repo)
        with mock.patch.object(train.subprocess, "Popen") as popen, mock.patch.object(c, "panicked", return_value=False), \
                mock.patch.dict(os.environ):
            os.environ.pop("FOREMAN_NO_BACKGROUND", None)
            for k in [k for k in os.environ if k.startswith(c.TEST_OVERRIDES)]:
                os.environ.pop(k)
            popen.return_value.pid = 1
            _, log = train.spawn_land(p, self.repo)
        self.assertNotIn("dropped", open(log).read() if os.path.exists(log) else "")

    def test_a_detached_land_in_an_empty_environment_still_spawns_and_keeps_the_post_merge_settings(self):
        """T-4289 review: the old comprehension read `dropped` per item, unbound with an empty environ (a NameError); the
        operator's FM_POSTMERGE_ settings stay for the land's own process, a test override goes."""
        from sampo import core as c, train
        p = c.find_project(self.repo)
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(train.subprocess, "Popen") as popen, \
                mock.patch.object(c, "panicked", return_value=False):
            popen.return_value.pid = 7
            pid, _ = train.spawn_land(p, self.repo)
            self.assertEqual(pid, 7)
            self.assertEqual(popen.call_args.kwargs["env"], {"FOREMAN_LAND_STAMP": "1"})
        with mock.patch.dict(os.environ, {"FM_POSTMERGE_CMD": "x", "FM_POSTMERGE_RERUN": "y", "FM_STALL": "1"}, clear=True), \
                mock.patch.object(train.subprocess, "Popen") as popen, mock.patch.object(c, "panicked", return_value=False):
            popen.return_value.pid = 8
            train.spawn_land(p, self.repo)
            given = popen.call_args.kwargs["env"]
        self.assertEqual(given, {"FM_POSTMERGE_CMD": "x", "FM_POSTMERGE_RERUN": "y", "FOREMAN_LAND_STAMP": "1"})


if __name__ == "__main__":
    unittest.main()
