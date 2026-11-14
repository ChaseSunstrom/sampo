"""T-4645: a ship that starts while another ship of the project is alive runs no test command: it says what it waits on,
waits for the other to end (bounded), then takes its snapshot from the moved origin main and tests that; when the running
ship already carries every commit this one would ship it ends at once; a dead or stale holder never blocks; the publish
grant is not spent by the wait. Fake remotes only: each test pushes to a bare repo in its own temp folder.
reruns: none: a standalone fixture (it copies test_ship's setUp, it does not inherit its tests)."""
# map-links: plugin/lib/sampo/release.py
import json
import os
import subprocess
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


class ShipWaits(ForemanTestCase):
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

    def test_cmd(self):
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

    def test_a_ship_beside_a_running_one_waits_runs_no_test_then_tests_the_moved_main(self):
        # the observed waste: this ship would have tested a snapshot the running ship is certain to make stale
        self.grant()
        holder = self.holder()
        proc, out = self.start("--test", self.test_cmd())
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
        r = self.fm("ship", "--remote", "origin", "--test", self.test_cmd(), check=False, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertRegex(r.stdout + r.stderr, rf"running ship \(pid {holder.pid},.*every commit")
        self.assertEqual(self.run_count(), 0)
        self.assertEqual(self.main_log(), before, "nothing was pushed")
        self.assertTrue(self.granted(), "the running ship spends the grant, not this one")
        self.assertEqual(c.read_meta(self.p)["ship_claim"]["pid"], holder.pid, "the holder's claim is untouched")

    def ships_at_once(self):
        self.grant()
        r = self.fm("ship", "--remote", "origin", "--test", self.test_cmd(), check=False, timeout=120)
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
        r = self.fm("ship", "--remote", "origin", "--test", self.test_cmd(), check=False, timeout=120,
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
