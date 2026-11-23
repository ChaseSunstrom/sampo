"""T-4645: a ship that starts while another ship of the project is alive runs no test command: it says what it waits on,
waits for the other to end (bounded), then takes its snapshot from the moved origin main and tests that; when the running
ship already carries every commit this one would ship it ends at once; a dead or stale holder never blocks; the publish
grant is not spent by the wait. Fake remotes only: each test pushes to a bare repo in its own temp folder.
reruns: none: a standalone fixture (it copies test_ship's setUp, it does not inherit its tests)."""
# map-links: plugin/lib/sampo/release.py
import json
import os
import subprocess
import sys
import time

from helpers import FM, ForemanTestCase, busy_factor

from sampo import core as c
from sampo import train


def sh(root, *a):
    return subprocess.run(["git", "-C", root, *a], capture_output=True, text=True, check=True).stdout


def until(cond, secs=60, what="the condition"):
    end = time.monotonic() + secs * busy_factor()
    while time.monotonic() < end:
        got = cond()
        if got:
            return got
        time.sleep(0.1)
    raise AssertionError(f"{what} didn't hold within {secs}s")


class ShipFixture(ForemanTestCase):
    """The fixture alone (no test of its own): ShipWaits, DeadWait and Phases each add theirs."""

    def setUp(self):
        super().setUp()
        self.fm("init")
        self.origin = os.path.join(self.tmp, "origin.git")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", self.origin], check=True)
        sh(self.repo, "remote", "add", "origin", self.origin)
        sh(self.repo, "push", "-q", "origin", "HEAD:main")
        sh(self.repo, "checkout", "-q", "-b", "work")
        for name in ("a", "b"):
            with open(os.path.join(self.repo, f"{name}.txt"), "w") as f:
                f.write(name + "\n")
            sh(self.repo, "add", f"{name}.txt")
            sh(self.repo, "commit", "-qm", f"add {name}")
        self.p = c.find_project(self.repo)
        self.tid = self.task("land")
        self.fm("focus", self.tid)  # the active task is the one whose grant a bare `fm ship` reads
        self.runs = os.path.join(self.tmp, "runs")
        self.seen = os.path.join(self.tmp, "seen")
        self.procs = []

    def tearDown(self):
        for x in self.procs:
            if x.poll() is None:
                x.kill()
            x.wait()
        super().tearDown()

    def task(self, title):
        tid = json.loads(self.fm("task", "new", title, "--type", "FEATURE", "--tier", "S", "--json").stdout)["id"]
        self.fm("task", "step", tid, "add", "land it")
        self.fm("task", "ac", tid, "add", "landed", "--verify", "true")
        return tid

    def grant(self, tid=None):
        b = c.find_brief(self.p, tid or self.tid)
        b.meta["allow"] = ["publish"]
        c.save_brief(self.p, b)

    def granted(self, tid=None):
        return "publish" in (c.find_brief(self.p, tid or self.tid).meta.get("allow") or [])

    def main_log(self):
        return sh(self.origin, "log", "--format=%s", "main").split("\n")

    def suite_cmd(self):  # not test_*: a helper named so reads as a test the subclasses inherit (T-3815)
        """A suite command that records each run and what the tested tree's history shows."""
        return f"sh -c 'echo x >> {self.runs}; git log --format=%s > {self.seen}'"

    def run_count(self):
        try:
            with open(self.runs) as f:
                return f.read().count("x")
        except OSError:
            return 0

    def start(self, *a, env=None):
        """`sampo ship` in the background: (process, its output file)."""
        out = os.path.join(self.tmp, f"out{len(self.procs)}.txt")
        e = dict(os.environ, FOREMAN_HOME=self.home, FOREMAN_INHIBITED="1", FOREMAN_DOCTOR_BENCH="0", **(env or {}))
        for k in ("FOREMAN_SHIP_AT", "SAMPO_SHIP_AT", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_BRIDGE_SESSION_ID"):
            e.pop(k, None)
        with open(out, "w") as f:
            proc = subprocess.Popen([FM, "ship", "--remote", "origin", *a], cwd=self.repo, env=e, stdin=subprocess.DEVNULL,
                                    stdout=f, stderr=subprocess.STDOUT)
        self.procs.append(proc)
        return proc, out

    @staticmethod
    def text(path):
        with open(path) as f:
            return f.read()

    def holder(self, picks=None, started=None, ticks=True, alive=True):
        """A fake running ship: the claim a ship writes, held by a process that is alive (a sleeper) or gone."""
        proc = subprocess.Popen(["sleep", "600"] if alive else ["true"])
        self.procs.append(proc)
        if not alive:
            proc.wait()
        claim = {"pid": proc.pid, "ticks": (train._ticks(proc.pid) or 12345) if ticks else 0,
                 "started": started or time.time(), "at": c.now(), "head": "0" * 40,
                 "picks": picks if picks is not None else ["f" * 40], "task": "T-0000"}
        c.update_meta(self.p, ship_claim=claim)
        return proc

    def ends(self, holder):
        holder.kill()
        holder.wait()


class ShipWaits(ShipFixture):
    def test_a_ship_beside_a_running_one_waits_runs_no_test_then_tests_the_moved_main(self):
        # the observed waste: this ship would have tested a snapshot the running ship is certain to make stale
        self.grant()
        holder = self.holder()
        proc, out = self.start("--test", self.suite_cmd())
        until(lambda: "waiting for" in self.text(out), 60, "the waiting line")
        line = next(ln for ln in self.text(out).splitlines() if "waiting for" in ln)
        self.assertIn(str(holder.pid), line, "the line names what it waits on")
        time.sleep(2)  # the wait polls: give it time to start a test it must not start
        self.assertEqual(self.run_count(), 0, "no test command runs while another ship is alive")
        self.assertIsNone(proc.poll(), "it waits, it doesn't end")
        self.assertTrue(self.granted(), "the wait spends nothing")
        other = os.path.join(self.tmp, "other")  # the running ship's push: origin main moves while this one waits
        subprocess.run(["git", "clone", "-q", self.origin, other], check=True)
        with open(os.path.join(other, "c.txt"), "w") as f:
            f.write("c\n")
        sh(other, "add", "c.txt")
        sh(other, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "someone else")
        sh(other, "push", "-q", "origin", "HEAD:main")
        self.ends(holder)
        self.assertEqual(proc.wait(timeout=120 * busy_factor()), 0, self.text(out))
        self.assertEqual(self.run_count(), 1)
        self.assertIn("someone else", self.text(self.seen).split("\n"), "the test ran on the moved origin main")
        self.assertEqual(self.main_log()[:3], ["add b", "add a", "someone else"])
        self.assertFalse(self.granted(), "its own push spent the grant")
        self.assertFalse(c.read_meta(self.p).get("ship_claim"), "the claim ends with the ship")

    def test_a_ship_whose_commits_the_running_ship_already_carries_ends_at_once_without_tests(self):
        self.grant()
        mine = sh(self.repo, "rev-list", "--reverse", "HEAD~2..HEAD").split()
        holder = self.holder(picks=[*mine, "e" * 40])
        before = self.main_log()
        r = self.fm("ship", "--remote", "origin", "--test", self.suite_cmd(), check=False, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertRegex(r.stdout + r.stderr, rf"running ship \(pid {holder.pid},.*every commit")
        self.assertEqual(self.run_count(), 0)
        self.assertEqual(self.main_log(), before, "nothing was pushed")
        self.assertTrue(self.granted(), "the running ship spends the grant, not this one")
        self.assertEqual(c.read_meta(self.p)["ship_claim"]["pid"], holder.pid, "the holder's claim is untouched")

    def ships_at_once(self):
        self.grant()
        r = self.fm("ship", "--remote", "origin", "--test", self.suite_cmd(), check=False, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("waiting for", r.stdout + r.stderr)
        self.assertEqual(self.run_count(), 1, "it tested at once")
        self.assertEqual(self.main_log()[:2], ["add b", "add a"])

    def test_a_holder_that_is_gone_never_blocks(self):
        self.holder(alive=False)
        self.ships_at_once()

    def test_a_live_pid_that_began_at_another_time_is_a_reused_pid_and_never_blocks(self):
        self.holder()
        claim = c.read_meta(self.p)["ship_claim"]
        c.update_meta(self.p, ship_claim=dict(claim, ticks=claim["ticks"] + 1))
        self.ships_at_once()

    def test_a_claim_older_than_a_ship_may_run_never_blocks(self):
        self.holder(started=time.time() - 10 * 86400)  # alive, but a ship ends itself at its cap
        self.ships_at_once()

    def test_the_wait_is_bounded_and_the_grant_survives_it(self):
        self.grant()
        holder = self.holder()
        t0 = time.monotonic()
        r = self.fm("ship", "--remote", "origin", "--test", self.suite_cmd(), check=False, timeout=120,
                    env={"FOREMAN_SHIP_TIMEOUT_S": "2"})
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("still running", r.stdout + r.stderr)
        self.assertIn("nothing was pushed", r.stdout + r.stderr)
        self.assertLess(time.monotonic() - t0, 60 * busy_factor())
        self.assertEqual(self.run_count(), 0)
        self.assertTrue(self.granted(), "a refused wait keeps the grant")
        self.assertEqual(c.read_meta(self.p)["ship_claim"]["pid"], holder.pid)

    def test_a_ship_is_claimed_while_it_runs_and_a_red_one_clears_its_claim(self):
        self.grant()
        r = self.fm("ship", "--remote", "origin", "--test", f"sh -c 'cp {os.path.join(self.p.dir, 'meta.json')} {self.seen}; exit 1'",
                    check=False, timeout=120)
        self.assertNotEqual(r.returncode, 0)
        with open(self.seen) as f:
            claim = json.load(f)["ship_claim"]
        self.assertGreater(claim["pid"], 0)
        self.assertEqual(len(claim["picks"]), 2, "it names the commits it ships")
        self.assertFalse(c.read_meta(self.p).get("ship_claim"), "the claim is gone with the refused ship")
        self.assertTrue(self.granted())

    def test_two_real_ships_the_second_waits_for_the_first_then_tests_the_moved_main(self):
        # end to end, the observed order: ship A is testing (a hand ship: no train marker anywhere), ship B starts beside it
        second = self.task("second")
        self.grant()
        self.grant(second)
        go = os.path.join(self.tmp, "go")
        add_a = sh(self.repo, "rev-parse", "HEAD~1").strip()
        a, out_a = self.start("--task", self.tid, "--test",
                              f"sh -c 'echo A >> {self.runs}; while [ ! -e {go} ]; do sleep 0.05; done'", add_a)
        until(lambda: "A" in (open(self.runs).read() if os.path.exists(self.runs) else ""), 120, "ship A's test")
        self.assertFalse(os.path.exists(train._ship_marker(self.p)),
                         "why the land's wait never saw it: a hand ship leaves no ship/marker.json, only its claim")
        self.assertIsNone(train._ship_alive(self.p))
        self.assertEqual(c.read_meta(self.p)["ship_claim"]["pid"], a.pid)
        b, out_b = self.start("--task", second, "--test", f"sh -c 'echo B >> {self.runs}; git log --format=%s > {self.seen}'")
        until(lambda: "waiting for" in self.text(out_b), 120, "ship B's waiting line")
        time.sleep(2)
        self.assertEqual(self.text(self.runs).strip(), "A", "B runs no test while A is alive")
        open(go, "w").close()
        self.assertEqual(a.wait(timeout=120 * busy_factor()), 0, self.text(out_a))
        self.assertEqual(b.wait(timeout=120 * busy_factor()), 0, self.text(out_b))
        self.assertEqual(self.text(self.runs).split(), ["A", "B"])
        self.assertIn("add a", self.text(self.seen).split("\n"), "B tested on the main A moved")
        self.assertEqual(self.main_log()[:2], ["add b", "add a"])
        self.assertEqual(self.main_log().count("add a"), 1)
        self.assertFalse(self.granted(second) or self.granted(), "each ship's own push spent its grant")


class DeadWait(ShipFixture):
    """T-5985: a waiting ship goes ahead the moment the earlier ship's pid is gone, and a ship the earlier one's own
    phase bounds still allow to run is still waited on. Real pids only: a killed sleeper, an exited `true`."""

    def test_a_waiting_ship_goes_ahead_at_once_when_its_holder_is_killed(self):
        self.grant()
        holder = self.holder()
        proc, out = self.start("--test", self.suite_cmd())
        until(lambda: "waiting for" in self.text(out), 60, "the waiting line")
        self.assertIsNone(proc.poll())
        self.assertEqual(self.run_count(), 0)
        t0 = time.monotonic()
        holder.kill()  # SIGKILL: no handler runs, the claim stays in meta.json naming a pid that is gone
        holder.wait()
        self.assertEqual(proc.wait(timeout=60 * busy_factor()), 0, self.text(out))
        self.assertLess(time.monotonic() - t0, 60 * busy_factor(), "it went ahead, it didn't sit out a bound")
        self.assertEqual(self.run_count(), 1)
        self.assertEqual(self.main_log()[:2], ["add b", "add a"], "it pushed")
        self.assertNotIn("still running", self.text(out))

    def test_an_exited_pid_is_not_alive_to_the_wait(self):
        from sampo import release
        holder = self.holder(alive=False)
        self.assertIsNotNone(holder.returncode, "a real pid that has exited")
        self.assertFalse(release._claim_alive(c.read_meta(self.p)["ship_claim"]))

    def test_a_claim_is_alive_for_as_long_as_every_phase_may_take_and_not_after(self):
        from sampo import release
        self.assertGreater(release.ship_span(), 7200 + 300, "the span is every phase, not the suite alone")
        holder = self.holder(started=time.time() - (7200 + 300 + 600))  # past the old suite-only bound, inside the span
        self.assertTrue(release._claim_alive(c.read_meta(self.p)["ship_claim"]),
                        "a ship in its later phases is not declared stale")
        self.ends(holder)
        self.holder(started=time.time() - (release.ship_span() + 300 + 600))
        self.assertFalse(release._claim_alive(c.read_meta(self.p)["ship_claim"]), "past every bound it is stale")


class Phases(ShipFixture):
    def test_a_real_ship_names_each_phase_to_the_file_the_land_gave_it_and_its_suite_never_sees_it(self):
        from sampo import release
        self.grant()
        pf = os.path.join(self.tmp, "phases")
        open(pf, "w").close()
        suite = f"sh -c 'echo \"${{{release.PHASE_ENV}-unset}}\" > {self.seen}; echo x >> {self.runs}'"
        proc, out = self.start("--test", suite, env={release.PHASE_ENV: pf})
        self.assertEqual(proc.wait(timeout=120 * busy_factor()), 0, self.text(out))
        names = self.text(pf).split()
        self.assertEqual(names, ["prepare", "hold", "gates", "suite", "finish"], names)
        self.assertEqual(self.text(self.seen).strip(), "unset", "the suite's env has no phase file to write to")
        self.assertNotIn("ship phase", self.text(out), "a hand ship prints no phase line: only the land's run does")

    def test_a_ship_that_waited_names_prepare_again_before_it_replans_so_a_slow_fetch_is_not_a_wait(self):
        from sampo import release
        self.grant()
        holder = self.holder()
        pf = os.path.join(self.tmp, "phases")
        open(pf, "w").close()
        proc, out = self.start("--test", self.suite_cmd(), env={release.PHASE_ENV: pf})
        until(lambda: "waiting for" in self.text(out), 60, "the waiting line")
        until(lambda: "wait" in self.text(pf).split(), 60, "the wait phase")
        self.ends(holder)
        self.assertEqual(proc.wait(timeout=120 * busy_factor()), 0, self.text(out))
        names = self.text(pf).split()
        at = names.index("wait")
        self.assertEqual(names[at + 1], "prepare", f"the replan's fetch runs under prepare, not wait: {names}")
        self.assertEqual(names[-3:], ["gates", "suite", "finish"], names)


GATE = """
import json, sys, types
from sampo import core as c, release
slug, root, d, lane = json.loads(sys.argv[1])
p = c.Project(slug, root, d, lane)
try:
    got = release._ship_gate(p, sys.argv[2], ["e" * 40], sys.argv[3], types.SimpleNamespace(remote="origin"))
    print("GATE", got, "CLAIM", (c.read_meta(p).get("ship_claim") or {}).get("pid"), flush=True)
except Exception as e:
    print("ERR", type(e).__name__, e, flush=True)
"""

PARENT = """
import json, os, subprocess, sys
from sampo import core as c, release
slug, root, d, lane = json.loads(sys.argv[1])
p = c.Project(slug, root, d, lane)
assert release._ship_claim(p, sys.argv[2], ["f" * 40], "T-9001") is None
print("PID", os.getpid(), flush=True)
r = subprocess.run([sys.executable, "-c", sys.argv[4], sys.argv[1], sys.argv[2], sys.argv[3]], capture_output=True, text=True)
print(r.stdout, r.stderr, flush=True)
"""

FAMILY = """
import subprocess, sys, time
y = subprocess.Popen(["sleep", "600"])
print(y.pid, flush=True)
time.sleep(600)
"""


class Cycle(ShipFixture):
    """T-5985: two ships that wait on each other never end: the older head yields ("superseded by <pid>"), the newer takes
    the one claim and tests. Real processes only (their own pids, parents and start ticks), no stubbed Popen."""

    def py(self, code, *argv):
        from helpers import LIB
        out = os.path.join(self.tmp, f"py{len(self.procs)}.txt")
        env = dict(os.environ, FOREMAN_HOME=self.home, PYTHONPATH=LIB, FOREMAN_INHIBITED="1")
        with open(out, "w") as f:
            proc = subprocess.Popen([sys.executable, "-c", code, *argv], cwd=self.repo, env=env, stdin=subprocess.DEVNULL,
                                    stdout=f, stderr=subprocess.STDOUT)
        self.procs.append(proc)
        return proc, out

    def where(self):
        return json.dumps([self.p.slug, self.p.root, self.p.dir, self.p.lane])

    def head(self):
        return sh(self.repo, "rev-parse", "HEAD").strip()

    def seeded(self, y, on, htime, task="T-9002"):
        """The registry entry a real ship Y (a real process) would write while it waits on pid `on`."""
        rec = {"pid": y, "ticks": c.proc_ticks(y), "on": on, "task": task, "head": "9" * 40, "htime": htime,
               "since": round(time.time(), 1), "ppids": c.proc_ancestors(y)}
        with c.lock(self.p.dir):
            meta = c.read_meta(self.p)
            meta["ship_waits"] = dict(meta.get("ship_waits") or {}, **{str(y): rec})
            c.write_meta(self.p, meta)

    def family(self):
        """A claim held by a real process X whose real child Y exists: (X, Y's pid)."""
        x, xout = self.py(FAMILY)
        y = int(until(lambda: self.text(xout).strip(), 60, "the child's pid"))
        c.update_meta(self.p, ship_claim={"pid": x.pid, "ticks": c.proc_ticks(x.pid), "started": time.time(), "at": c.now(),
                                          "head": "0" * 40, "picks": ["f" * 40], "task": "T-9001"})
        return x, y

    def waiter(self):
        a, out = self.py(GATE, self.where(), self.head(), "T-9003")
        until(lambda: str(a.pid) in (c.read_meta(self.p).get("ship_waits") or {}), 60, "A in the wait registry")
        return a, out

    def verdict(self, a, out):
        a.wait(timeout=60 * busy_factor())
        return self.text(out)

    def test_a_ship_whose_claim_holder_is_its_own_parent_yields_at_once(self):
        g, out = self.py(PARENT, self.where(), self.head(), "T-9004", GATE)
        until(lambda: g.poll() is not None, 60, "the parent and its child ship to end")
        text = self.text(out)
        pid = text.split("PID", 1)[1].split()[0]
        self.assertIn(f"ERR UsageError superseded by {pid}", text, "the child waited on its own parent: it ends, never waits")
        self.assertIn("nothing was pushed", text)

    def test_the_older_head_yields_to_a_holders_child_that_waits_on_it(self):
        x, y = self.family()
        a, out = self.waiter()
        self.seeded(y, a.pid, htime=time.time() + 10_000)  # Y's head is the newer: A, the older, yields
        text = self.verdict(a, out)
        self.assertIn(f"ERR UsageError superseded by {y} (T-9002)", text)
        self.assertEqual((c.read_meta(self.p).get("ship_claim") or {}).get("pid"), x.pid, "the claim did not move")

    def test_the_newer_head_takes_the_claim_when_the_holders_child_waits_on_it(self):
        x, y = self.family()
        a, out = self.waiter()
        self.seeded(y, a.pid, htime=1)  # Y's head is the older: A, the newer, tests now
        text = self.verdict(a, out)
        self.assertIn(f"GATE go CLAIM {a.pid}", text, text)
        self.assertIn("this head is the newer", text)

    def test_a_holders_child_that_waits_on_somebody_else_is_no_cycle(self):
        x, y = self.family()
        z = subprocess.Popen(["sleep", "600"])
        self.procs.append(z)
        a, out = self.waiter()
        self.seeded(y, z.pid, htime=1)
        time.sleep(3.5)  # three looks of A's wait
        self.assertIsNone(a.poll(), self.text(out))
        self.ends(x)  # the holder ends: A goes ahead (waited), not superseded
        text = self.verdict(a, out)
        self.assertIn("GATE waited", text, text)


class Bounds(ForemanTestCase):
    """T-5985: a verify that yields to a ship waits for the ship's whole life, a rerun is bounded under its own phase."""

    def test_a_verify_that_yields_waits_for_a_ships_whole_life_not_its_suite_alone(self):
        from unittest import mock
        from sampo import lanes, release
        small = {"prepare": 0.2, "wait": 0.6, "hold": 0.2, "gates": 0.2, "suite": 0.2, "rerun": 0.2, "finish": 0.2}
        with mock.patch.object(release, "ship_caps", lambda limit=None: dict(small)), \
                mock.patch.object(lanes, "SHIP_YIELD_POLL", 0.05), \
                mock.patch.object(train, "_ship_alive", lambda p: {"pid": os.getpid()}):
            t0 = time.monotonic()
            waited = lanes.ship_yield(None)
        self.assertGreater(waited, small["suite"] + 0.8, "it waited past the suite's cap, to the ship's whole life")
        self.assertLess(time.monotonic() - t0, release.ship_life() + 5)
        self.assertGreater(release.ship_life(), release.ship_span())

    def test_a_rerun_is_bounded_under_its_phase_whatever_the_load(self):
        from unittest import mock
        from sampo import release
        cap = release.ship_caps()["rerun"]
        with mock.patch.object(c, "load_scaled", lambda s: s * 1000):
            self.assertLessEqual(release._rerun_timeout(), cap - 90, "the load wait and a margin stay inside the phase")
        with mock.patch.object(c, "load_scaled", lambda s: s):
            self.assertEqual(release._rerun_timeout(), release.RED_RERUN_S)
        with mock.patch.object(release, "ship_caps", lambda limit=None: {"rerun": 10}):
            self.assertEqual(release._rerun_timeout(), 1.0, "a floor, never zero or negative")


# T-5985: every `os.kill(pid, 0)` in plugin/lib/sampo is a pid-only answer. A wait on a ship, a verify or a check must
# not use one (a reused pid then reads as the ship still running): those compare start ticks (core.same_process). The
# rest are listed here, each with why a pid alone is enough (key: file, enclosing top-level function).
PID_ONLY = {
    ("core.py", "_alive"): "the one pid-only answer: same_process falls back to it for a marker that recorded no start time",
    ("board.py", "_train_runs"): "a display of runs, never a wait",
    ("bus.py", "record_pid"): "a record of who listens, not a wait on one",
    ("bus.py", "watch"): "the watcher's own parent: it ends with it",
    ("cli.py", "_sentinel"): "sentinel.pid: a second sentinel is refused, nothing waits on it",
    ("cli.py", "_stack_dumps"): "a diagnostic's listing of processes",
    ("cpu.py", "_release_main"): "a cpu slot's holder: the slot's own file records who",
    ("doctor.py", "_stale_pid"): "a *.pid file's age and pid: a verdict for fsck, it waits on nothing",
    ("editverdict.py", "_alive"): "the edit verdict's own worker: a stale one only repeats its work",
    ("hooks.py", "_live_close"): "a close marker (pid only): not a ship or verify wait",
    ("hooks.py", "_closing"): "a close marker (pid only): not a ship or verify wait",
    ("jobsreg.py", "_alive"): "a job's registry line, for display",
    ("lanes.py", "agent_lock_dead"): "a start-time check's fallback for a host that can't read /proc",
    ("lanes.py", "_pid_alive"): "a gate folder's leftover check: a wrong guess keeps a folder one pass longer",
    ("lib.py", "_recover_sources"): "which source tree a dead process left, not a wait",
    ("models.py", "_alive"): "a model server's pid file: a wrong guess restarts it",
    ("pilotsig.py", "alive"): "a pilot signal's sender, a display",
    ("second.py", "_alive"): "a second opinion's worker: a wrong guess repeats its work",
    ("session.py", "_alive"): "a session marker, a display",
    ("smithtui.py", "_alive"): "a terminal display",
    ("train.py", "_closing"): "a close marker (pid only): not a ship or verify wait",
}


class Liveness(ForemanTestCase):
    """T-5985: a wait on a ship, a verify or a check never takes a living pid for the process a marker names."""

    @staticmethod
    def kill_zero_sites():
        import ast
        import glob
        from helpers import LIB
        found = {}
        for path in sorted(glob.glob(os.path.join(LIB, "sampo", "*.py"))):
            with open(path, encoding="utf-8") as f:
                tree = ast.parse(f.read())
            for top in ast.walk(tree):
                if not isinstance(top, ast.Module):
                    continue
                for fn in top.body:
                    for node in ast.walk(fn):
                        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "kill"
                                and isinstance(node.func.value, ast.Name) and node.func.value.id == "os"
                                and len(node.args) == 2 and isinstance(node.args[1], ast.Constant) and node.args[1].value == 0):
                            name = getattr(fn, "name", "<module>")
                            if isinstance(fn, ast.ClassDef):  # a method: the class's own method
                                name = next((m.name for m in ast.walk(fn) if isinstance(m, ast.FunctionDef)
                                             and m.lineno <= node.lineno <= m.end_lineno), fn.name)
                            found.setdefault((os.path.basename(path), name), []).append(node.lineno)
        return found

    def test_no_pid_only_check_is_left_outside_the_listed_ones(self):
        found = self.kill_zero_sites()
        new = sorted(set(found) - set(PID_ONLY))
        self.assertEqual(new, [], "a pid-only liveness check (os.kill(pid, 0)) outside the list: a ship, verify or check "
                         "wait must use core.same_process (pid and start ticks); another use is listed in PID_ONLY with "
                         "its reason")
        self.assertEqual(sorted(set(PID_ONLY) - set(found)), [], "listed, but no such check any more: drop it from PID_ONLY")

    def test_the_converted_waits_do_not_use_a_pid_alone(self):
        from sampo import lanes, release
        found = self.kill_zero_sites()
        for site in (("release.py", "_claim_alive"), ("train.py", "_ship_alive"), ("lanes.py", "_pm_dead"),
                     ("lanes.py", "ship_yield"), ("core.py", "gates_live"), ("ambient.py", "running")):
            self.assertNotIn(site, found, site)
        self.assertTrue(callable(lanes._pm_dead) and callable(release._claim_alive))

    def test_same_process_wants_the_start_ticks_a_marker_recorded(self):
        me = os.getpid()
        ticks = c.proc_ticks(me)
        self.assertTrue(ticks, "this host reads start ticks")
        self.assertTrue(c.same_process(me, ticks))
        self.assertFalse(c.same_process(me, ticks + 1), "a live pid that began at another time is a reused pid")
        self.assertTrue(c.same_process(me), "no ticks recorded: the pid answering")
        gone = subprocess.Popen(["true"])
        gone.wait()
        self.assertFalse(c.same_process(gone.pid, 12345))
        self.assertFalse(c.same_process(gone.pid))
        for bad in (0, 1, -5, None, True, "7"):
            self.assertFalse(c.same_process(bad, ticks), bad)

    def test_a_verify_record_whose_pid_was_reused_is_dead_and_a_live_one_is_not(self):
        from sampo import lanes
        me = os.getpid()
        self.assertFalse(lanes._pm_dead({"pid": me, "ticks": c.proc_ticks(me)}))
        self.assertTrue(lanes._pm_dead({"pid": me, "ticks": c.proc_ticks(me) + 1}), "reused pid")
        self.assertFalse(lanes._pm_dead({"pid": me}), "a record with no ticks: the pid answering")
        self.assertTrue(lanes._pm_dead({"pid": 0}), "no pid is no verify")
        self.assertTrue(lanes._pm_dead({"pid": "x"}))
        self.assertTrue(lanes._pm_dead({}))

    def test_a_verify_claim_records_the_claimers_start_ticks(self):
        import inspect
        from sampo import lanes
        self.assertIn("ticks=c.proc_ticks(os.getpid())", inspect.getsource(lanes._pm_claim))

    def test_a_check_marker_whose_pid_was_reused_is_not_live(self):
        p = c.Project("p", self.tmp, os.path.join(self.tmp, "pdir"))
        os.makedirs(p.dir)
        me = os.getpid()
        c.write_meta(p, {"gates_running": {"pid": me, "ticks": c.proc_ticks(me) + 1, "root": self.tmp}})
        self.assertIsNone(c.gates_live(p), "the pid lives, it is another process")
        c.write_meta(p, {"gates_running": {"pid": me, "ticks": c.proc_ticks(me), "root": self.tmp}})
        self.assertEqual(c.gates_live(p)["pid"], me)
        c.write_meta(p, {"gates_running": {"pid": me, "root": self.tmp}})
        self.assertEqual(c.gates_live(p)["pid"], me, "an older marker with no ticks: the pid answering")

    def test_the_train_marker_of_a_reused_pid_is_not_a_running_ship(self):
        p = c.Project("p", self.tmp, os.path.join(self.tmp, "pdir"))
        me = os.getpid()
        m = {"pid": me, "ticks": c.proc_ticks(me) + 1}
        os.makedirs(os.path.join(p.dir, "ship"))
        with open(os.path.join(p.dir, "ship", "marker.json"), "w") as f:
            json.dump(m, f)
        self.assertFalse(train._ship_alive(p))
        m["ticks"] = c.proc_ticks(me)
        with open(os.path.join(p.dir, "ship", "marker.json"), "w") as f:
            json.dump(m, f)
        self.assertTrue(train._ship_alive(p))

    def test_an_ambient_run_file_with_pid_and_ticks_is_read_and_a_reused_pid_is_not_a_run(self):
        from sampo import ambient, doctor
        p = c.Project("p", self.tmp, os.path.join(self.tmp, "pdir"))
        path = os.path.join(ambient._dir(p), "run.pid")
        me = os.getpid()
        for body, live in ((f"{me} {c.proc_ticks(me)}", True), (f"{me} {c.proc_ticks(me) + 1}", False), (f"{me}", True)):
            with open(path, "w") as f:
                f.write(body)
            self.assertEqual(ambient.running(p), live, body)
            old = os.stat(path).st_mtime - 10_000
            os.utime(path, (old, old))
            self.assertEqual(doctor._stale_pid(path), "", f"fsck: a live run is never stale ({body})")
        gone = subprocess.Popen(["true"])
        gone.wait()
        with open(path, "w") as f:
            f.write(f"{gone.pid} 12345")
        old = os.stat(path).st_mtime - 10_000
        os.utime(path, (old, old))
        self.assertIn("is gone", doctor._stale_pid(path))
