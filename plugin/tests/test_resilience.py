"""T-0679: resilience, state and simplicity — first versions: a chaos test over the task lifecycle, fm usage --prune,
fm wiring, a doctor check that the views match the briefs, and a mutation script for the guard's checks."""
import datetime
import json
import os
import random
import subprocess
import sys
from unittest import mock

from helpers import PLUGIN, ForemanTestCase, read_text

from sampo import cli
from sampo import core as c


class Chaos(ForemanTestCase):
    def test_random_lifecycles_crashed_at_a_random_write_stay_consistent(self):
        self.fm("init")
        p = c.find_project(self.repo)
        rng, here = random.Random(int(os.environ.get("FOREMAN_CHAOS_SEED", "20261009"))), os.getcwd()  # other seeds: an env var
        os.chdir(self.repo)
        seen = set()
        try:
            for i in range(80):
                ids = [b.id for b in c.load_briefs(p)]
                tid = rng.choice(ids) if ids else None
                argv = rng.choice([
                    ["task", "new", f"t{i}", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true", "--step", "s"],
                    ["capture", f"idea {i}"],
                    *([["focus", tid], ["task", "evidence", tid, "--step", "1", "--run", "true"],
                       ["task", "finish", tid, "--run", "true", "--audit", "self check"], ["task", "block", tid, "x"],
                       ["task", "defer", tid, "later"], ["task", "drop", tid, "no"],
                       ["task", "set", tid, "status=planned"]] if tid else [])])
                before = {b.id: b.status for b in c.load_briefs(p)}
                left = [rng.randint(1, 8) if rng.random() < 0.4 else 10 ** 6]
                real = c.write_atomic

                def crashing(path, text):
                    left[0] -= 1
                    if left[0] <= 0:
                        raise OSError("chaos: the write never happened")
                    return real(path, text)
                with mock.patch.object(c, "write_atomic", crashing), \
                        mock.patch("sys.stdout"), mock.patch("sys.stderr"):
                    try:
                        cli.main(argv)
                    except BaseException:
                        pass
                after = {b.id: b.status for b in c.load_briefs(p)}  # always loads
                for t, st in after.items():
                    if t in before and before[t] != st:
                        seen.add((before[t], st))
                        self.assertIn(st, c.TRANSITIONS[before[t]], f"{t}: {before[t]} → {st} via {argv}")
        finally:
            os.chdir(here)
        c.regen_views(p)
        self.assertTrue(seen, "the run changed some statuses")


class Prune(ForemanTestCase):
    def test_commands_never_run_are_listed_as_trim_candidates(self):
        self.fm("init")
        slug = c.find_project(self.repo).slug
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(os.path.join(c.state_dir(), "events.jsonl"), "a") as f:
            f.write(json.dumps({"ts": ts, "kind": "tool", "tool": "Bash", "target": "fm queue", "project": slug}) + "\n")
        before = sorted(os.listdir(os.path.join(PLUGIN, "lib")))
        out = self.fm("usage", "--prune").stdout
        self.assertIn("orders", out)
        self.assertNotIn(" queue,", out.split("Not run")[1] if "Not run" in out else out)
        self.assertIn("fm capture", out)
        self.assertIn("CLEAN", out)
        self.assertEqual(sorted(os.listdir(os.path.join(PLUGIN, "lib"))), before, "nothing is deleted")


class Wiring(ForemanTestCase):
    def test_one_screen_of_what_is_wired(self):
        self.fm("init")
        self.fm("autonomy", "full")
        out = self.fm("wiring").stdout
        for want in ("version", "hooks", "PreToolUse", "autonomy full", "drive", "standing", "trust", "pause", "budget"):
            self.assertIn(want, out, want)


class ViewsRepair(ForemanTestCase):
    def test_doctor_sees_stale_views_and_repair_regenerates_them(self):
        self.fm("init")
        self.fm("task", "new", "One", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true", "--step", "s")
        p = c.find_project(self.repo)
        line = os.path.join(p.dir, "state.line")
        with open(line, "w") as f:
            f.write("garbage\n")
        self.assertIn("views", self.fm("doctor", check=False).stdout)
        self.fm("doctor", "--repair", check=False)
        self.assertNotEqual(read_text(line), "garbage\n")
        self.assertNotIn("views don't match", self.fm("doctor", check=False).stdout)


class RepairStaysInTheTest(ForemanTestCase):
    """T-4935: a test's fm doctor --repair ran fastbuild on the tree under test with the account's cargo (a fresh worktree
    has no binary and a merge leaves it stale: a cargo build, ViewsRepair's 101 s timeouts under load, and in the main
    checkout the live hooks' binary replaced), and its model fix on the real ~/.claude/settings.json and ~/.codex."""

    def test_a_tests_doctor_gets_the_switch_and_a_repair_a_home_of_its_own(self):
        import forkcli
        seen = []

        def run(script, args, **kw):
            seen.append(kw["env"])
            return subprocess.CompletedProcess(args, 0, "", "")
        with mock.patch.object(forkcli, "run", side_effect=run):
            for args in (("doctor", "--repair"), ("doctor", "--fix"), ("doctor",)):
                self.fm(*args)
            self.fm("doctor", "--repair", env={"HOME": self.tmp})
        repair, fix, plain, given = seen
        self.assertEqual({e.get("FOREMAN_DOCTOR_BUILD") for e in seen}, {"0"})
        for e in (repair, fix):
            self.assertTrue(e["HOME"].startswith(self.tmp + os.sep), e["HOME"])
        self.assertEqual(plain["HOME"], os.environ["HOME"])  # a plain doctor reads; only a repair writes the model
        self.assertEqual(given["HOME"], self.tmp)  # a test's own HOME wins

    def test_the_switch_stops_a_build_with_a_cargo_at_hand(self):
        from sampo import fastbuild
        plug = os.path.join(self.tmp, "plug")
        os.makedirs(os.path.join(plug, "native"))
        with mock.patch.dict(os.environ, {"FOREMAN_DOCTOR_BUILD": "0"}), \
                mock.patch.object(fastbuild, "build_fast", side_effect=AssertionError("built")):
            ok, line = fastbuild.repair(plug, cargo="/somewhere/cargo")
        self.assertFalse(ok)
        self.assertIn("not built (FOREMAN_DOCTOR_BUILD=0", line)

    def test_a_tests_repair_writes_the_tests_settings_only(self):
        settings = os.path.join(self.tmp, "nohome", ".claude", "settings.json")
        os.makedirs(os.path.dirname(settings))
        with open(settings, "w") as f:
            json.dump({"model": "claude-opus-4-8"}, f)
        self.fm("init")
        out = self.fm("doctor", "--repair", check=False).stdout
        self.assertIn("Native fast path: not built", out)
        with open(settings) as f:
            self.assertEqual(json.load(f).get("model"), "best", out)  # the model fix read the test's HOME


class Mutation(ForemanTestCase):
    def test_the_real_guard_has_flip_and_drop_sites(self):
        # T-0496's script became fm bench guard --mutate --flip (T-1121); judging is tested on fixtures (test_replay)
        import fmguard
        from sampo import mutate
        with open(os.path.join(PLUGIN, "lib", "fmguard.py"), encoding="utf-8") as f:
            src = f.read()
        flips = mutate.flips(src)
        self.assertGreater(len(flips), 50)
        self.assertTrue(all(m["src"] != src for m in flips[:20]))
        self.assertGreater(len(mutate.mutants(src, fmguard.CATEGORIES)), 30)
