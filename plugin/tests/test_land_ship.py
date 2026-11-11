"""T-4239: a land never waits for a ship. After a train lands, sampo lane land starts one detached sampo ship of the head
it just read (or tells the running ship to go again when it finishes) and goes on to the next train; the ship tests and
pushes the snapshot at that head, never a newer one. Every land.log line carries its time; the ledger has each train's
and each ship's duration."""
# map-links: plugin/lib/sampo/train.py
import json
import os
import re
import signal
import statistics
import subprocess
import sys
import time
import unittest

from test_lane_land import OLD, LandCase

from sampo import core as c
from sampo import train


def until(cond, secs=90, what="the condition"):
    end = time.monotonic() + secs
    while time.monotonic() < end:
        got = cond()
        if got:
            return got
        time.sleep(0.1)
    raise AssertionError(f"{what} didn't hold within {secs}s")


class ShipCase(LandCase):
    """A land whose ships are real detached `sampo ship` processes (the other suites run them in line, FOREMAN_NO_BACKGROUND),
    pushing to an origin whose pre-receive hook logs each push and holds push N until the file go-N exists."""

    def setUp(self):
        super().setUp()
        os.environ.pop("FOREMAN_NO_BACKGROUND", None)  # SampoTestCase restores it
        self.origin = os.path.join(self.tmp, "origin.git")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", self.origin], check=True)
        self.git("remote", "add", "origin", self.origin)
        self.git("push", "-q", "origin", "HEAD:main")
        self.pushlog = os.path.join(self.tmp, "pushes")
        hook = os.path.join(self.origin, "hooks", "pre-receive")
        with open(hook, "w") as f:
            f.write(f'#!/bin/sh\nwhile read old new ref; do echo "$old $new $ref" >> {self.pushlog}; done\n'
                    f'n=$(wc -l < {self.pushlog} | tr -d " ")\n'
                    f'while [ ! -e {self.tmp}/go-$n ]; do sleep 0.05; done\n')
        os.chmod(hook, 0o755)
        self.p = c.find_project(self.repo)

    def tearDown(self):
        self.open(9)
        try:
            until(lambda: not train._ship_alive(self.p), 60, "the ship to finish")
        finally:
            super().tearDown()

    def open(self, n):
        for i in range(1, n + 1):
            open(os.path.join(self.tmp, f"go-{i}"), "a").close()

    def pushes(self):
        try:
            with open(self.pushlog) as f:
                return [ln.split() for ln in f.read().splitlines()]
        except OSError:
            return []

    def files(self):
        return set(subprocess.run(["git", "-C", self.origin, "ls-tree", "-r", "--name-only", "main"],
                                  capture_output=True, text=True).stdout.split())

    def lanes_ready(self, n, grant=True):
        ids = [self.lane(i, f"{'abcdefg'[i - 1]}.txt", when=OLD) for i in range(1, n + 1)]
        for t in ids:
            self.audited(t)
            self.returned(t, f"Lane {t}")
        if grant:
            b = self.brief(ids[0])
            b.meta["allow"] = ["publish"]
            c.save_brief(self.p, b)
        return ids

    def land_blocked(self, n=3):
        """A land of n lanes, one per train, whose first ship's push is held: the land ends while that ship still runs."""
        ids = self.lanes_ready(n)
        r = self.fm("lane", "land", "--batch", "1", env={"SAMPO_LAND_SHIP_WAIT_S": "4"}, timeout=240)
        return ids, r

    def events(self, kind):
        return [e for e in c.ledger_tail(self.p, 400, [kind]) if e.get("event") == kind]


class Behind(ShipCase):
    def test_behind_a_train_lands_while_the_previous_ships_push_is_held_and_trains_cost_one_more_ship(self):
        ids, r = self.land_blocked(3)
        out = r.stdout + r.stderr
        for name in ("a.txt", "b.txt", "c.txt"):
            self.assertTrue(os.path.exists(os.path.join(self.repo, name)), out)  # all three trains landed
        self.assertEqual(self.gate_runs(), 3 + 1, "one gate run per train, and the held ship's own test run of its snapshot")
        self.assertEqual(len(self.pushes()), 1, "one ship is at its push; the later trains started none beside it")
        self.assertTrue(train._ship_alive(self.p), "the ship is still running when the land ends")
        self.assertIn("ship still running", out)
        self.assertIn(os.path.join(self.p.dir, "ship.log"), out)
        self.assertEqual(self.files() & {"b.txt", "c.txt"}, set())  # the held push is the first train's alone
        self.open(1)  # the first ship pushes; the trains meanwhile cost exactly one more ship, not one each
        until(lambda: len(self.pushes()) == 2, what="the follow-up ship to push")
        self.assertEqual(len(self.pushes()), 2)
        self.open(2)
        until(lambda: not train._ship_alive(self.p), what="the ships to finish")
        self.assertEqual(len(self.pushes()), 2, "three trains during one ship: one more ship")
        self.assertEqual(self.files() & {"a.txt", "b.txt", "c.txt"}, {"a.txt", "b.txt", "c.txt"})
        self.assertEqual([e["data"]["exit"] for e in self.events("ship_done")], [0, 0])
        self.assertNotIn("publish", self.brief(ids[0]).meta.get("allow") or [], "the land's last ship spends the grant")
        self.assertIn("grant kept", self.brief(ids[0]).section("Log"), "and the first one gave it back for the next")

    def test_behind_a_refused_ship_is_a_line_and_an_event_never_a_stopped_land(self):
        ids = self.lanes_ready(2)
        self.git("remote", "set-url", "origin", os.path.join(self.tmp, "nowhere.git"))  # every ship's fetch fails
        r = self.fm("lane", "land", "--batch", "1", env={"SAMPO_LAND_SHIP_WAIT_S": "60"}, timeout=240)
        out = r.stdout + r.stderr
        for name in ("a.txt", "b.txt"):
            self.assertTrue(os.path.exists(os.path.join(self.repo, name)), out)  # both trains landed
        self.assertEqual([x.id for x in c.load_briefs(self.p) if x.status in c.CLOSED and x.id in ids], ids, out)
        done = self.events("ship_done")
        self.assertTrue(done and all(e["data"]["exit"] != 0 for e in done), done)
        self.assertRegex(out, r"(?i)refused")


class Sha(ShipCase):
    def test_sha_the_ship_pushes_the_head_it_read_while_later_trains_land(self):
        self.lanes_ready(3)
        before = self.git("rev-parse", "HEAD")
        self.fm("lane", "land", "--batch", "1", env={"SAMPO_LAND_SHIP_WAIT_S": "4"}, timeout=240)
        first = self.pushes()[0]
        self.assertEqual(self.files() & {"a.txt", "b.txt", "c.txt"}, set(), "held: nothing yet")
        self.open(1)
        until(lambda: len(self.pushes()) == 2, what="the second ship to push")
        self.assertEqual(self.files() & {"a.txt", "b.txt", "c.txt"}, {"a.txt"},
                         "the first ship pushed the first train's head: the later trains' files are not in it")
        self.assertEqual(self.git("rev-parse", "HEAD"), self.git("rev-parse", "refs/heads/main"))
        self.assertNotEqual(self.git("rev-parse", "HEAD"), before)
        self.open(2)
        until(lambda: not train._ship_alive(self.p), what="the ships to finish")
        done = self.events("ship_done")
        self.assertEqual(done[0]["data"]["sha"], first[1], "the event names the sha pushed")
        self.assertEqual(done[1]["data"]["sha"], self.pushes()[1][1])
        self.assertRegex(done[0]["data"]["head"], r"^[0-9a-f]{40}$")
        self.assertNotEqual(done[0]["data"]["head"], done[1]["data"]["head"])

    def test_sha_a_pinned_ship_tests_and_pushes_a_snapshot_at_that_head(self):
        tid = self.lanes_ready(1)[0]
        for n in ("one", "two"):
            with open(os.path.join(self.repo, f"{n}.txt"), "w") as f:
                f.write(n)
            self.git("add", f"{n}.txt")
            self.git("commit", "-qm", n)
        pin = self.git("rev-parse", "HEAD~1")
        self.open(9)
        r = self.fm("ship", "--task", tid, "--test", "true", env={"SAMPO_SHIP_AT": pin}, timeout=240)
        self.assertEqual(self.files() & {"one.txt", "two.txt"}, {"one.txt"}, r.stdout)
        self.assertIn(f"at {pin[:9]}", r.stdout, "the ship's log says which snapshot it tests")
        bad = self.fm("ship", "--task", tid, "--test", "true", env={"SAMPO_SHIP_AT": "nonsense"}, check=False)
        self.assertNotEqual(bad.returncode, 0)

    def test_sha_the_pin_never_reaches_the_ships_own_test_run(self):
        # review: the suite a ship runs runs `fm ship` in scratch repos with the inherited env; a pin left in it names a
        # commit those repos lack, so every pinned ship (the land's own) went red
        tid = self.lanes_ready(1)[0]
        seen = os.path.join(self.tmp, "seen-env.txt")
        probe = os.path.join(self.tmp, "probe.py")
        with open(probe, "w") as f:
            f.write("import os, sys\n"
                    f"open({seen!r}, 'w').write(repr(sorted(k for k in os.environ if k.endswith('SHIP_AT'))))\n"
                    "sys.exit(1 if any(k.endswith('SHIP_AT') for k in os.environ) else 0)\n")
        with open(os.path.join(self.repo, "one.txt"), "w") as f:
            f.write("one")
        self.git("add", "one.txt")
        self.git("commit", "-qm", "one")
        pin = self.git("rev-parse", "HEAD")
        self.open(9)
        r = self.fm("ship", "--task", tid, "--test", f"{sys.executable} {probe}", env={"SAMPO_SHIP_AT": pin}, timeout=240)
        self.assertTrue(os.path.exists(seen), "the test command ran\n" + r.stdout)
        with open(seen) as f:
            self.assertEqual(f.read(), "[]", "the test command saw no SHIP_AT name\n" + r.stdout)

    def test_sha_a_dead_ships_marker_never_blocks_the_next(self):
        os.makedirs(os.path.dirname(train._ship_marker(self.p)), exist_ok=True)
        dead = subprocess.Popen(["true"])
        dead.wait()
        for pid, ticks in ((dead.pid, 1), (os.getpid(), 12345)):  # a dead pid; a live one with another start time
            with open(train._ship_marker(self.p), "w") as f:
                json.dump({"pid": pid, "ticks": ticks, "started": time.time(), "want": "x", "ship_head": "x"}, f)
            self.assertFalse(train._ship_alive(self.p), (pid, ticks))
        self.open(9)
        self.lanes_ready(1)
        r = self.fm("lane", "land", env={"SAMPO_LAND_SHIP_WAIT_S": "90"}, timeout=240)
        self.assertEqual(self.files() & {"a.txt"}, {"a.txt"}, r.stdout + r.stderr)
        self.assertFalse(train._ship_alive(self.p))
        self.assertFalse(os.path.exists(train._ship_marker(self.p)), "the marker is cleared with the ship")

    def test_sha_the_marker_holds_pid_and_start_time_of_the_running_ship(self):
        self.lanes_ready(1)
        self.fm("lane", "land", env={"SAMPO_LAND_SHIP_WAIT_S": "2"}, timeout=240)
        m = train._ship_alive(self.p)
        self.assertTrue(m, "the ship is held at its push")
        self.assertEqual(m["ticks"], train._ticks(m["pid"]))
        self.assertGreater(m["started"], 0)
        os.kill(m["pid"], signal.SIGKILL)  # a killed ship: its marker reads dead, a land after it starts a fresh one
        until(lambda: not train._ship_alive(self.p), 10, "the killed ship to read dead")


class Cap(unittest.TestCase):
    def test_cap_a_grandchild_holding_the_output_never_outlives_the_ships_time_limit(self):
        # review: `for ln in proc.stdout` on a pipe never saw EOF while a process that left the group held it, so the cap
        # was bypassed and the marker stayed alive for good (T-2255's hazard)
        import tempfile
        from unittest import mock
        script = "setsid sleep 40 & echo started; echo more; sleep 40"
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"FOREMAN_SHIP_TIMEOUT_S": "2"}), \
                mock.patch("sys.stdout", new=open(os.devnull, "w")):
            t0 = time.monotonic()
            rc, out = train._ship_run(tmp, "T-0001", "0" * 40, argv=["sh", "-c", script])
            took = time.monotonic() - t0
        subprocess.run(["pkill", "-x", "-f", "sleep 40"], check=False)  # the grandchild left the group on purpose
        self.assertEqual(rc, 124, out)
        self.assertLess(took, 15, "the cap ended the ship at its limit, not when the grandchild let go of the output")
        self.assertIn("started", out, "what the ship printed before the cap is kept")
        self.assertIn("timed out", out)


class Grant(ShipCase):
    """Review: who may give the publish grant back, and an in-line ship beside a detached one."""

    SHIPPED = "shipped 1 to origin main at " + "a" * 40 + " (the publish grant is spent)."

    def held(self, tid):
        return "publish" in (c.find_brief(self.p, tid).meta.get("allow") or [])

    def spent(self):
        from sampo import release
        tid = self.lanes_ready(1)[0]
        release._spend(self.p, tid, "a hand ship")
        self.assertFalse(self.held(tid))
        return tid

    def test_grant_a_ship_that_pushed_nothing_never_brings_back_a_grant_another_ship_spent(self):
        tid = self.spent()
        train._settle(self.p, tid, 0, "nothing to ship: origin's main already has these commits", {"final": False})
        self.assertFalse(self.held(tid), "someone else's push spent it: a ship with no push of its own leaves it spent")

    def test_grant_a_ships_own_push_gives_the_grant_back_for_the_next_train(self):
        tid = self.spent()
        train._settle(self.p, tid, 0, self.SHIPPED, {"final": False, "land_pid": os.getpid(),
                                                     "land_ticks": train._ticks(os.getpid())})
        self.assertTrue(self.held(tid), "its own push spent it and a later train will ask again")

    def test_grant_a_refused_ship_gives_nothing_back(self):
        tid = self.spent()
        train._settle(self.p, tid, 1, self.SHIPPED, {"final": False})
        self.assertFalse(self.held(tid))

    def test_grant_it_stays_spent_when_the_land_that_asked_for_the_ship_is_gone(self):
        tid = self.spent()
        gone = subprocess.Popen(["true"])
        gone.wait()
        train._settle(self.p, tid, 0, self.SHIPPED, {"final": False, "land_pid": gone.pid, "land_ticks": 7})
        self.assertFalse(self.held(tid), "no later train will ask: the grant isn't left waiting for one")
        train._settle(self.p, tid, 0, self.SHIPPED, {"final": False, "land_pid": os.getpid(), "land_ticks": 7})
        self.assertFalse(self.held(tid), "a live pid with another start time is another process")

    def test_grant_an_in_line_ship_never_runs_beside_a_detached_one(self):
        from unittest import mock
        tid = self.lanes_ready(1)[0]
        sleeper = subprocess.Popen(["sleep", "30"])
        self.addCleanup(sleeper.wait)
        self.addCleanup(sleeper.kill)
        os.makedirs(os.path.dirname(train._ship_marker(self.p)), exist_ok=True)
        with open(train._ship_marker(self.p), "w") as f:
            json.dump({"pid": sleeper.pid, "ticks": train._ticks(sleeper.pid), "started": time.time(), "want": "x",
                       "ship_head": "x"}, f)
        with mock.patch.dict(os.environ, {"FOREMAN_LAND_SHIP_WAIT_S": "1"}), \
                mock.patch.object(train, "_sampo", return_value=(0, "shipped", "")) as ran:
            got = train._ship(self.p, False, self.repo)
        ran.assert_not_called()
        self.assertEqual(got[0], tid)
        self.assertIn("already running", got[1])
        self.assertTrue(self.held(tid), "nothing was shipped: the grant is untouched")
        os.remove(train._ship_marker(self.p))

    def test_grant_an_in_line_ship_goes_on_once_the_running_ship_is_done(self):
        from unittest import mock
        self.lanes_ready(1)
        brief = subprocess.Popen(["sleep", "1"])
        self.addCleanup(brief.wait)
        os.makedirs(os.path.dirname(train._ship_marker(self.p)), exist_ok=True)
        with open(train._ship_marker(self.p), "w") as f:
            json.dump({"pid": brief.pid, "ticks": train._ticks(brief.pid), "started": time.time(), "want": "x",
                       "ship_head": "x"}, f)
        with mock.patch.dict(os.environ, {"FOREMAN_LAND_SHIP_WAIT_S": "30"}), \
                mock.patch.object(train, "_sampo", return_value=(0, "nothing to ship", "")) as ran:
            train._ship(self.p, True, self.repo)
        ran.assert_called_once()
        os.remove(train._ship_marker(self.p))


class Times(ShipCase):
    def test_times_land_lines_carry_their_time_and_the_summary_names_each_trains_minutes(self):
        self.open(9)
        self.lanes_ready(2)
        r = self.fm("lane", "land", "--batch", "1", env={"SAMPO_LAND_STAMP": "1", "SAMPO_LAND_SHIP_WAIT_S": "90"},
                    timeout=240)
        lines = [ln for ln in (r.stdout + r.stderr).splitlines() if ln.strip()]
        self.assertTrue(lines)
        for ln in lines:
            self.assertRegex(ln, r"^\d\d:\d\d:\d\d ", f"{ln!r} in:\nstdout:{r.stdout}\nstderr:{r.stderr}")
        out = r.stdout
        self.assertRegex(out, r"Train 1: 1 lane\(s\), gate [\d.]+ min, waiting [\d.]+ min")
        self.assertRegex(out, r"Train 2: 1 lane\(s\), gate [\d.]+ min, waiting [\d.]+ min")

    def test_times_a_detached_land_logs_every_line_with_its_time(self):
        self.open(9)
        self.lanes_ready(1)
        r = self.fm("lane", "land", "--bg")
        self.assertIn("started", r.stdout, r.stderr)
        log = os.path.join(self.p.dir, "land.log")
        until(lambda: os.path.exists(log) and "Land:" in open(log).read() and not train.landing(self.p), 120, "the land")
        lines = [ln for ln in open(log).read().splitlines() if ln.strip()]
        self.assertTrue(lines)
        for ln in lines:
            self.assertRegex(ln, r"^\d\d:\d\d:\d\d ", ln)

    def test_times_the_ledger_has_each_trains_and_each_ships_duration(self):
        self.open(9)
        self.lanes_ready(2)
        self.fm("lane", "land", "--batch", "1", env={"SAMPO_LAND_SHIP_WAIT_S": "90"}, timeout=240)
        trains = self.events("land_train")
        self.assertEqual(len(trains), 2)
        for e in trains:
            self.assertEqual(len(e["data"]["lanes"]), 1)
            self.assertGreaterEqual(e["data"]["seconds"], 0)
        ships = self.events("ship_done")
        self.assertTrue(ships)
        for e in ships:
            self.assertEqual(e["data"]["exit"], 0)
            self.assertGreaterEqual(e["data"]["seconds"], 0)
            self.assertRegex(e["data"]["sha"], r"^[0-9a-f]{40}$")

    def test_times_status_shows_the_last_lands_accept_to_live_median(self):
        self.open(9)
        ids = self.lanes_ready(2)
        for tid, ago in zip(ids, (600, 1200)):
            b = self.brief(tid)
            b.meta["accepted_at"] = time.time() - ago
            c.save_brief(self.p, b)
        self.fm("lane", "land", "--batch", "1", env={"SAMPO_LAND_SHIP_WAIT_S": "90"}, timeout=240)
        out = self.fm("status").stdout
        self.assertRegex(out, r"Last land: 2 lane\(s\) in 2 train\(s\); accept-to-live median [\d.]+ min", out)
        got = train.last_land(self.p)
        self.assertEqual(len(got["live"]), 2)
        self.assertGreaterEqual(statistics.median(got["live"]), 10 * 60 - 5)


if __name__ == "__main__":
    unittest.main()
