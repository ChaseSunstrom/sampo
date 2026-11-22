"""T-4239: a land never waits for a ship. After a train lands, sampo lane land starts one detached sampo ship of the head
it just read (or tells the running ship to go again when it finishes) and goes on to the next train; the ship tests and
pushes the snapshot at that head, never a newer one. Every land.log line carries its time; the ledger has each train's
and each ship's duration."""
# map-links: plugin/lib/sampo/train.py
import json
import os
import re
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
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


def hold_hook(tmp, pushlog):
    """The origin's pre-receive hook: logs each push and holds push N until the file go-N exists in tmp. The hold ends
    by itself, refusing the push, once tmp is gone (the test died before its tearDown) or after 6000 rounds (T-4719)."""
    return (f'#!/bin/sh\nwhile read old new ref; do echo "$old $new $ref" >> {pushlog}; done\n'
            f'n=$(wc -l < {pushlog} | tr -d " ")\n'
            f'i=0\nwhile [ ! -e {tmp}/go-$n ]; do\n'
            f'  [ -d {tmp} ] && [ $i -lt 6000 ] || exit 1\n'
            f'  sleep 0.05; i=$((i+1))\ndone\n')


class HoldHook(unittest.TestCase):
    """T-4719: 325 of these hooks were found still looping on one machine, most over a day old, in test folders long
    deleted: a test that died before its tearDown left the hook waiting for a go file that could never appear."""

    def held(self, tmp):
        hook = os.path.join(tmp, "hook")
        with open(hook, "w") as f:
            f.write(hold_hook(tmp, os.path.join(tmp, "pushes")))
        os.chmod(hook, 0o755)
        p = subprocess.Popen([hook], stdin=subprocess.PIPE, text=True, cwd="/")
        p.stdin.write("a b refs/heads/main\n")
        p.stdin.close()
        self.addCleanup(lambda: (p.poll() is None and p.kill(), p.wait()))
        until(lambda: os.path.exists(os.path.join(tmp, "pushes")), 30, "the hook to log its push")
        return p

    def test_the_hook_ends_by_itself_once_its_test_folder_is_gone(self):
        tmp = tempfile.mkdtemp(prefix="fmtest-hold-")
        self.addCleanup(shutil.rmtree, tmp, True)
        p = self.held(tmp)
        time.sleep(0.3)
        self.assertIsNone(p.poll(), "the push is held while the folder is there and no go file is")
        shutil.rmtree(tmp)
        try:
            code = p.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.fail("the hook still loops after its test folder was removed: it leaks past the test")
        self.assertNotEqual(code, 0, "a hold that ends because the folder went refuses the push")

    def test_the_hook_lets_the_push_through_on_its_go_file_and_counts_its_rounds(self):
        with tempfile.TemporaryDirectory(prefix="fmtest-hold-") as tmp:
            p = self.held(tmp)
            open(os.path.join(tmp, "go-1"), "a").close()
            self.assertEqual(p.wait(timeout=15), 0)
            self.assertRegex(hold_hook(tmp, "x"), r"-lt \d+", "the loop counts its rounds: bounded even if the folder stays")


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
            f.write(hold_hook(self.tmp, self.pushlog))
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

    def term_child(self, out, tid=None, want="a" * 40, during=None):
        """_ship_child here over a marker of this process: its ship mock (no process is started: no pid reaches a kill)
        runs `during()` (what happens to the remote while the ship runs), then raises the ship's end by SIGTERM carrying
        `out` (what the ship had printed). (the lane's id, what the child printed, its return)."""
        import contextlib
        import io
        from unittest import mock
        from sampo import release
        tid = tid or self.lanes_ready(1)[0]
        if tid != c.STANDING_SHIP:  # the standing grant is never spent
            release._spend(self.p, tid, "a ship")
            self.assertNotIn("publish", self.brief(tid).meta.get("allow") or [])
        path = train._ship_marker(self.p)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump({"pid": os.getpid(), "ticks": train._ticks(os.getpid()), "started": time.time(), "want": want,
                       "ship_head": "", "again": False, "final": True}, f)
        err = train._ShipTerm(signal.SIGTERM)
        err.ship_out = out

        def ended(*a, **kw):
            if during:
                during()
            raise err

        buf = io.StringIO()
        with mock.patch.object(train, "_ship_run", side_effect=ended), contextlib.redirect_stdout(buf):
            rc = train._ship_child({"project": [self.p.slug, self.p.root, self.p.dir, self.p.lane], "main": self.repo,
                                    "grant": tid})
        return tid, buf.getvalue(), rc

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
        # T-4455: the detached ship no longer carries the PYTHONPATH entry its start added, so its environment is the
        # train's and it reuses the train's gate pass on the same code tree: no second run of the suite per ship
        self.assertEqual(self.gate_runs(), 3, "one gate run per train; the held ship reuses its train's pass")
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


    def child(self, **patches):
        """_ship_child run here over a marker of this process, its ship mocked: (the ship mock, what it printed)."""
        import contextlib
        import io
        from unittest import mock
        path = train._ship_marker(self.p)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump({"pid": os.getpid(), "ticks": train._ticks(os.getpid()), "started": time.time(), "want": "a" * 40,
                       "ship_head": "", "again": False, "final": True}, f)

        def ship(*a, **k):  # a later train asks for another ship while the first one runs
            if ship.mock.call_count == 1:
                m = train._read_marker(self.p)
                m["again"] = True
                c.write_atomic(path, json.dumps(m))
            return 0, "shipped 1 to origin main at " + "b" * 40 + " (the publish grant is spent)."
        out = io.StringIO()
        with mock.patch.object(train, "_ship_run", side_effect=ship) as ship.mock, \
                mock.patch.object(train, "_settle", **patches), contextlib.redirect_stdout(out):
            train._ship_child({"project": [self.p.slug, self.p.root, self.p.dir, self.p.lane], "main": self.repo,
                               "grant": "T-0001"})
        return ship.mock, out.getvalue()

    def test_behind_a_failed_settle_never_drops_the_ship_a_later_train_asked_for(self):
        ran, out = self.child(side_effect=[RuntimeError("lock busy"), None])
        self.assertEqual(ran.call_count, 2, "the queued ship ran although the first settle raised")
        self.assertRegex(out, r"(?m)^\d\d:\d\d:\d\d .*lock busy", "the failure is a timed line in the log")
        self.assertFalse(os.path.exists(train._ship_marker(self.p)), "the marker is cleared with the last ship")

    def test_behind_a_settle_that_cannot_take_the_lock_still_goes_again(self):
        from unittest import mock
        real, n = c.lock, []

        def lock(path, *a, **k):  # the second acquire (after the first ship) times out
            n.append(path)
            if len(n) == 2:
                raise c.LockTimeout("busy")
            return real(path, *a, **k)
        with mock.patch.object(c, "lock", side_effect=lock):
            ran, out = self.child(return_value=None)
        self.assertEqual(ran.call_count, 2, out)
        self.assertRegex(out, r"(?m)^\d\d:\d\d:\d\d .*busy")


class Sha(ShipCase):
    def land_here(self):
        """cmd_land run here: what it printed to stdout and stderr."""
        import argparse
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.repo)  # the close runs `sampo` from the cwd: the test's repo
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            train.cmd_land(self.p, argparse.Namespace(batch=1, first=None, bg=False, json=False), self.repo)
        return out.getvalue() + err.getvalue()

    def old_record(self):
        c.write_atomic(os.path.join(self.p.dir, "ship-last.json"), json.dumps(
            {"head": "0" * 40, "sha": "0" * 40, "seconds": 1.0, "exit": 0, "start": time.time() - 5000,
             "end": time.time() - 4990, "red": [], "grant": "T-0001", "seen": True, "line": "STALE earlier ship fine"}))

    def test_sha_a_ship_that_could_not_start_is_said_and_no_earlier_record_stands_in_for_it(self):
        from unittest import mock
        self.lanes_ready(1)
        self.old_record()
        with mock.patch.object(train, "_ship_request", return_value=("failed", "OSError: no fork left")):
            out = self.land_here()
        self.assertRegex(out, r"(?m)^Ship not started: .*no fork left.*next land", "the land's own summary names it")
        self.assertNotIn("STALE", out)
        self.assertNotIn("Ship (", out, "no ship result line: none ran")

    def test_sha_a_ship_that_ended_without_its_record_never_reports_the_earlier_ones_line(self):
        from unittest import mock
        self.lanes_ready(1)
        self.old_record()
        real = train._ship_request  # the ship child dies before writing its record

        def request(p, main, final):
            got = real(p, main, final)
            m = train._read_marker(p)
            if m and m.get("pid") != os.getpid():
                os.kill(m["pid"], signal.SIGKILL)  # the ship child dies at once: its marker reads dead
            return got
        with mock.patch.object(train, "_ship_request", side_effect=request):
            out = self.land_here()
        # T-5938: the killed ship's marker is read (and cleared) with a line of its own, which the summary now carries
        self.assertRegex(out, r"(?m)^(Ship ended without a result.*next land|Ship \(.*\): ship pid \d+ .* is gone: )")
        self.assertNotIn("STALE", out)

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


class Died(ShipCase):
    """T-5938: a ship that dies or is killed leaves a line in ship.log (the project's one place to read what a ship did) and
    the next ship goes ahead: nothing waits on its dead pid. On 2026-11-22 the log held no line for a ship that was gone."""

    def log(self):
        try:
            with open(os.path.join(self.p.dir, "ship.log"), encoding="utf-8") as f:
                return f.read()
        except OSError:
            return ""

    def dead_marker(self, grant="T-0001"):
        gone = subprocess.Popen(["true"])  # a real pid that has ended and been reaped: never a stub's
        gone.wait()
        os.makedirs(os.path.dirname(train._ship_marker(self.p)), exist_ok=True)
        with open(train._ship_marker(self.p), "w") as f:
            json.dump({"pid": gone.pid, "ticks": 4242, "started": time.time() - 90, "want": "x", "ship_head": "x",
                       "grant": grant, "final": True, "log": "x"}, f)
        return gone.pid

    def test_died_a_marker_whose_ship_is_gone_leaves_one_line_and_is_cleared(self):
        pid = self.dead_marker()
        got = train._ship_reap(self.p)
        self.assertEqual(got["pid"], pid)
        self.assertIn(f"pid {pid}", self.log())
        self.assertRegex(self.log(), r"(?m)^\d\d:\d\d:\d\d ship pid \d+ .*gone")
        self.assertFalse(os.path.exists(train._ship_marker(self.p)), "the dead ship's marker is cleared")
        self.assertIsNone(train._ship_reap(self.p), "a second look finds nothing and says nothing more")
        self.assertEqual(self.log().count(f"pid {pid}"), 1)
        rec = train.ship_last(self.p)
        self.assertTrue(rec and rec["exit"] and not rec["seen"], "sampo status tells how it ended")

    def test_died_a_running_ship_is_never_reaped(self):
        sleeper = subprocess.Popen(["sleep", "30"])
        self.addCleanup(sleeper.wait)
        self.addCleanup(sleeper.kill)
        os.makedirs(os.path.dirname(train._ship_marker(self.p)), exist_ok=True)
        with open(train._ship_marker(self.p), "w") as f:
            json.dump({"pid": sleeper.pid, "ticks": train._ticks(sleeper.pid), "started": time.time(), "want": "x",
                       "ship_head": "x"}, f)
        self.assertIsNone(train._ship_reap(self.p))
        self.assertEqual(self.log(), "")
        self.assertTrue(os.path.exists(train._ship_marker(self.p)))
        os.remove(train._ship_marker(self.p))

    def test_died_the_next_ship_starts_at_once_and_the_log_says_why_it_did_not_wait(self):
        self.lanes_ready(1)
        self.open(9)
        pid = self.dead_marker()
        got = train._ship_request(self.p, self.repo, True)
        self.assertEqual(got[0], "started", got)
        self.assertIn(f"ship pid {pid}", self.log())
        until(lambda: not train._ship_alive(self.p), 90, "the fresh ship to end")

    def test_died_an_in_line_ship_goes_ahead_beside_a_dead_marker_and_says_so(self):
        from unittest import mock
        self.lanes_ready(1)
        pid = self.dead_marker()
        t0 = time.monotonic()
        with mock.patch.dict(os.environ, {"FOREMAN_LAND_SHIP_WAIT_S": "600"}), \
                mock.patch.object(train, "_sampo", return_value=(0, "nothing to ship", "")) as ran:
            train._ship(self.p, True, self.repo)
        ran.assert_called_once()
        self.assertLess(time.monotonic() - t0, 30, "no wait on a pid that is gone")
        self.assertIn(f"ship pid {pid}", self.log())

    @staticmethod
    def zombie(pid):
        """True when pid is dead but not yet reaped (or gone): read from /proc, nothing is signalled."""
        try:
            with open(f"/proc/{pid}/stat") as f:
                return f.read().rpartition(")")[2].split()[0] == "Z"
        except OSError:
            return True

    @staticmethod
    def group(pgid):
        """The live (not zombie) processes of process group pgid, read from /proc: nothing is signalled."""
        out = []
        for d in os.listdir("/proc"):
            if not d.isdigit():
                continue
            try:
                with open(f"/proc/{d}/stat") as f:
                    rest = f.read().rpartition(")")[2].split()
            except OSError:
                continue
            if rest and rest[0] != "Z" and int(rest[2]) == pgid:
                out.append(int(d))
        return out

    def children_of(self, pid):
        out = []
        for d in os.listdir("/proc"):
            try:
                with open(f"/proc/{d}/stat") as f:
                    if d.isdigit() and int(f.read().rpartition(")")[2].split()[1]) == pid:
                        out.append(int(d))
            except (OSError, ValueError, IndexError):
                continue
        return out

    def test_died_a_ship_killed_by_sigterm_says_so_clears_its_marker_and_takes_its_ship_with_it(self):
        ids = self.lanes_ready(1)
        self.fm("lane", "land", env={"SAMPO_LAND_SHIP_WAIT_S": "2"}, timeout=240)
        m = until(lambda: train._ship_alive(self.p), 30, "the ship to run")
        until(lambda: re.search(r"ship of [0-9a-f]{9} on ", self.log()), 60, "the ship to say it started")
        until(lambda: self.pushes(), 60, "the ship to reach its push (held by the origin: the grant is spent)")
        kids = until(lambda: self.children_of(m["pid"]), 30, "the ship's own process")
        mine = [k for k in kids if self.group(k)]
        self.assertTrue(mine, "the ship runs in a group of its own")
        self.assertNotIn("publish", self.brief(ids[0]).meta.get("allow") or [], "the push spent the grant")
        os.kill(m["pid"], signal.SIGTERM)
        until(lambda: not train._ship_alive(self.p), 30, "the ship to end")
        self.assertRegex(self.log(), r"(?m)^\d\d:\d\d:\d\d ship pid \d+ .*SIGTERM")
        self.assertFalse(os.path.exists(train._ship_marker(self.p)), "its marker never outlives it")
        rec = train.ship_last(self.p)
        self.assertTrue(rec and rec["exit"] == 128 + signal.SIGTERM, rec)
        for k in mine:  # the ship it ran, and the push it held, never go on without it
            until(lambda k=k: not self.group(k), 15, f"the ship's process group {k} to be gone")
        self.assertEqual(self.files() & {"a.txt"}, set(), "nothing was pushed")
        self.assertIn("publish", self.brief(ids[0]).meta.get("allow") or [], "its push never landed: the grant is back")
        self.assertRegex(self.log(), r"(?m)^\d\d:\d\d:\d\d ship pid \d+ .*SIGTERM.*given back")
        self.assertNotIn("untouched", self.log())

    def test_died_a_signal_before_the_push_landed_gives_the_spent_grant_back_and_says_so(self):
        tid, out, rc = self.term_child("tests passed\n")
        self.assertEqual(rc, 128 + signal.SIGTERM)
        self.assertIn("publish", self.brief(tid).meta.get("allow") or [], "no push was seen to land: the grant is held again")
        self.assertRegex(out, r"SIGTERM.*given back")
        self.assertNotIn("untouched", out)

    def test_died_a_signal_after_the_push_landed_leaves_the_grant_spent_and_says_so(self):
        tid, out, rc = self.term_child(f"shipped 1 to origin main at {'c' * 40} (the publish grant is spent).\n")
        self.assertNotIn("publish", self.brief(tid).meta.get("allow") or [], "its push landed: one yes is one push")
        self.assertRegex(out, r"SIGTERM.*push (landed|had landed).*spent")
        self.assertNotIn("given back", out)

    def test_died_a_signal_for_the_standing_grant_says_it_stays_on(self):
        c.set_standing_ship(self.p, True, "keep pushing to main", "test")
        tid, out, rc = self.term_child("tests passed\n", tid=c.STANDING_SHIP)
        self.assertEqual(rc, 128 + signal.SIGTERM)
        self.assertRegex(out, r"SIGTERM.*standing ship.*(stays on|never spent)")
        self.assertNotIn("given back", out)

    def test_died_a_hand_ships_claim_whose_pid_is_gone_is_taken_over_with_a_line(self):
        from sampo import release
        gone = subprocess.Popen(["true"])
        gone.wait()
        with c.lock(self.p.dir):
            meta = c.read_meta(self.p)
            meta["ship_claim"] = {"pid": gone.pid, "ticks": 4242, "started": round(time.time(), 1), "at": "", "head": "h",
                                  "picks": [], "task": "T-0007"}
            c.write_meta(self.p, meta)
        self.assertIsNone(release._ship_claim(self.p, "h2", [], "T-0008"), "the dead claim never holds the next ship")
        self.assertRegex(self.log(), rf"ship pid {gone.pid} .*T-0007.*gone")
        self.assertEqual(c.read_meta(self.p)["ship_claim"]["task"], "T-0008")
        with c.lock(self.p.dir):  # leave no claim behind for the teardown
            meta = c.read_meta(self.p)
            meta.pop("ship_claim")
            c.write_meta(self.p, meta)


class TermGrant(ShipCase):
    """T-5980: a killed ship's spent grant is given back only after origin shows its main lacks the ship's head. The ship
    here is a mock (no process starts, so no pid ever reaches a kill); the remote is the real bare origin."""

    def setUp(self):
        super().setUp()
        self.open(9)  # origin's pre-receive hook holds each push until its go file exists: this test's own pushes pass

    def held(self, tid):
        return "publish" in (c.find_brief(self.p, tid).meta.get("allow") or [])

    def commit(self):
        """A new local commit on the repo's HEAD: (its sha)."""
        self.git("commit", "--allow-empty", "-qm", "a head to ship")
        return self.git("rev-parse", "HEAD").strip()

    def origin_main(self):
        return subprocess.run(["git", "-C", self.origin, "rev-parse", "main"], capture_output=True, text=True).stdout.strip()

    def test_term_grant_a_remote_that_never_moved_gives_the_grant_back(self):
        head = self.commit()
        tid, out, rc = self.term_child("tests passed\n", want=head)
        self.assertTrue(self.held(tid), "origin's main is where it was and lacks the head: nothing landed")
        self.assertRegex(out, r"SIGTERM.*nothing was pushed.*given back")

    def test_term_grant_a_remote_that_has_the_head_keeps_the_grant_spent(self):
        """The kill came between the push and its print: the output shows nothing, origin has the head."""
        head = self.commit()
        tid, out, rc = self.term_child("tests passed\n", want=head,
                                       during=lambda: self.git("push", "-q", "origin", "HEAD:main"))
        self.assertEqual(self.origin_main(), head)
        self.assertFalse(self.held(tid), "the push landed: one yes is one push")
        self.assertNotIn("given back", out)
        self.assertRegex(out, r"SIGTERM.*(moved or has its head|spent)")

    def test_term_grant_a_remote_that_already_had_the_head_keeps_the_grant_spent(self):
        head = self.commit()
        self.git("push", "-q", "origin", "HEAD:main")
        tid, out, rc = self.term_child("tests passed\n", want=head)
        self.assertFalse(self.held(tid), "main is the head before and after: not a proof that nothing was pushed")
        self.assertNotIn("given back", out)

    def test_term_grant_a_remote_whose_main_moved_keeps_the_grant_spent(self):
        """Another push moved main (the ship's pushed sha is a cherry-pick, not the head): the ship may have been it."""
        head = self.commit()
        other = self.commit()
        self.git("reset", "-q", "--hard", head)
        tid, out, rc = self.term_child("tests passed\n", want=head,
                                       during=lambda: self.git("push", "-q", "origin", f"{other}:refs/heads/main"))
        self.assertEqual(self.origin_main(), other)
        self.assertFalse(self.held(tid))
        self.assertNotIn("given back", out)

    def test_term_grant_a_remote_that_cannot_be_reached_keeps_the_grant_spent(self):
        head = self.commit()
        gone = os.path.join(self.tmp, "no-such-origin.git")
        tid, out, rc = self.term_child("tests passed\n", want=head,
                                       during=lambda: self.git("remote", "set-url", "origin", gone))
        self.assertFalse(self.held(tid), "the remote could not say whether the push landed: spent, never doubled")
        self.assertNotIn("given back", out)
        self.assertRegex(out, r"SIGTERM.*could not be asked")

    def test_term_grant_a_remote_unreachable_before_the_ship_keeps_the_grant_spent(self):
        head = self.commit()
        self.git("remote", "set-url", "origin", os.path.join(self.tmp, "no-such-origin.git"))
        tid, out, rc = self.term_child("tests passed\n", want=head)
        self.assertFalse(self.held(tid), "where main was is unknown: the grant is not given back on a guess")
        self.assertNotIn("given back", out)

    def test_term_grant_the_ship_ending_in_an_exception_asks_the_remote_too(self):
        """_ship_child's own except branch (the ship's run raised) takes the same give-back path."""
        from unittest import mock
        head = self.commit()
        tid = self.lanes_ready(1)[0]
        from sampo import release
        release._spend(self.p, tid, "a ship")
        path = train._ship_marker(self.p)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump({"pid": os.getpid(), "ticks": train._ticks(os.getpid()), "started": time.time(), "want": head,
                       "ship_head": "", "again": False, "final": True}, f)

        def ran(*a, **kw):
            self.git("push", "-q", "origin", "HEAD:main")
            raise OSError("the ship's group was killed")

        import contextlib
        import io
        buf = io.StringIO()
        with mock.patch.object(train, "_ship_run", side_effect=ran), contextlib.redirect_stdout(buf):
            train._ship_child({"project": [self.p.slug, self.p.root, self.p.dir, self.p.lane], "main": self.repo,
                               "grant": tid})
        self.assertFalse(self.held(tid), "origin has the head: the grant stays spent")

    def test_term_grant_the_standing_ship_asks_no_remote(self):
        from unittest import mock
        from sampo import release
        c.set_standing_ship(self.p, True, "keep pushing to main", "test")
        with mock.patch.object(release, "_main_at", side_effect=AssertionError("the remote was asked")):
            self.assertIsNone(train._remote_main(self.p, c.STANDING_SHIP))
            tid, out, rc = self.term_child("tests passed\n", tid=c.STANDING_SHIP)
        self.assertRegex(out, r"standing ship")

    def test_term_grant_a_push_that_timed_out_gives_the_grant_back_only_when_main_lacks_the_sha(self):
        """release._push_main: git's runner ended the push on its timeout, so its child died and nothing says it did not
        land. Origin is asked; a refusal by git itself landed nothing."""
        from unittest import mock
        from sampo import release
        from sampo import cli
        head = self.commit()
        tid = self.lanes_ready(1)[0]
        timed_out = cli.UsageError("git push -q origin x:refs/heads/main failed: timed out after 300 s")
        with mock.patch.object(release, "_git", side_effect=timed_out):
            with self.assertRaises(cli.UsageError):
                release._push_main(self.p, tid, head, "origin", "a hand ship")
        self.assertTrue(self.held(tid), "origin's main lacks the sha: nothing landed, the grant is back")
        self.git("push", "-q", "origin", "HEAD:main")
        with mock.patch.object(release, "_git", side_effect=timed_out):
            with self.assertRaises(cli.UsageError):
                release._push_main(self.p, tid, head, "origin", "a hand ship")
        self.assertFalse(self.held(tid), "origin's main is at the sha: the push landed, the grant stays spent")
        refused = cli.UsageError("git push failed: ! [rejected] (non-fast-forward)")
        b = self.brief(tid)
        b.meta["allow"] = ["publish"]
        c.save_brief(self.p, b)
        with mock.patch.object(release, "_git", side_effect=refused):
            with self.assertRaises(cli.UsageError):
                release._push_main(self.p, tid, head, "origin", "a hand ship")
        self.assertTrue(self.held(tid), "a push git refused landed nothing: given back even though main has the sha")

    def test_term_grant_the_push_primitive_unreachable_remote_after_a_timeout_stays_spent(self):
        from unittest import mock
        from sampo import release
        from sampo import cli
        head = self.commit()
        tid = self.lanes_ready(1)[0]
        self.git("remote", "set-url", "origin", os.path.join(self.tmp, "no-such-origin.git"))
        timed_out = cli.UsageError("git push failed: timed out after 300 s")
        with mock.patch.object(release, "_git", side_effect=timed_out):
            with self.assertRaises(cli.UsageError):
                release._push_main(self.p, tid, head, "origin", "a hand ship")
        self.assertFalse(self.held(tid), "the remote can't say: spent, never doubled")


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

    def test_cap_a_signal_after_the_ship_exited_still_ends_what_it_left_in_its_group(self):
        """T-5938 review: the ship's own process was reaped, a process it left running in its group was not: the signal
        that ends the land's ship ends that too (the group is killed whether or not the leader is still there)."""
        import tempfile
        from unittest import mock
        calls = []

        def once(*a):  # the signal lands while the output is read, after the ship was waited for
            calls.append(1)
            if len(calls) == 1:
                raise train._ShipTerm(signal.SIGTERM)
            return "00:00:00"
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(train, "_hms", side_effect=once), \
                mock.patch("sys.stdout", new=open(os.devnull, "w")):
            with self.assertRaises(train._ShipTerm) as got:
                train._ship_run(tmp, "T-0001", "0" * 40, argv=["sh", "-c", "sleep 41 & echo $!"])
        pid = int(got.exception.ship_out.split()[0])  # a real pid: the one the script printed
        self.addCleanup(lambda: subprocess.run(["kill", "-9", str(pid)], capture_output=True))
        end = time.monotonic() + 10
        while time.monotonic() < end and os.path.exists(f"/proc/{pid}") and not Died.zombie(pid):
            time.sleep(0.1)
        self.assertTrue(not os.path.exists(f"/proc/{pid}") or Died.zombie(pid), "the process the ship left behind is gone")

    def test_cap_the_ships_environment_carries_neither_spelling_of_the_land_marker_for_a_task_grant(self):
        import tempfile
        from unittest import mock
        names = (c.LAND_SHIP_ENV, "SAMPO_" + c.LAND_SHIP_ENV.partition("_")[2])
        probe = [sys.executable, "-c", f"import os; print('seen=' + ','.join(k for k in {names!r} if os.environ.get(k)))"]
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {k: "land" for k in names}), \
                mock.patch("sys.stdout", new=open(os.devnull, "w")):
            rc, out = train._ship_run(tmp, "T-0001", "0" * 40, argv=probe)
            self.assertEqual((rc, out.strip()), (0, "seen="), "a task's grant gets no marker, in either spelling")
            rc, out = train._ship_run(tmp, c.STANDING_SHIP, "0" * 40, argv=probe)
            self.assertIn(c.LAND_SHIP_ENV, out, "the standing grant gets the marker")


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

    def test_grant_a_push_cut_short_by_an_interrupt_gives_the_spent_grant_back_unless_the_remote_has_it(self):
        """T-5938 review: the grant is spent before the push; a Ctrl-C between them skipped the give-back (it sat in an
        `except Exception`), leaving the grant spent for a push that never happened."""
        from unittest import mock
        from sampo import release
        tid = self.lanes_ready(1)[0]
        with mock.patch.object(release, "_git", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                release._push_main(self.p, tid, "b" * 40, "origin", "a hand ship")
        self.assertTrue(self.held(tid), "nothing reached origin's main: the grant is back")
        tip = subprocess.run(["git", "-C", self.origin, "rev-parse", "main"], capture_output=True, text=True).stdout.strip()
        with mock.patch.object(release, "_git", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                release._push_main(self.p, tid, tip, "origin", "a hand ship")
        self.assertFalse(self.held(tid), "origin's main is at the sha: the push landed, one yes is one push")

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

    COVERED = ("nothing to ship here: the running ship (pid 7, T-1, testing abcdef123) already carries every commit "
               "this one would ship (05eeeae6); no tests run. If it is refused, run fm ship again")

    def test_grant_a_final_ship_a_running_ship_covers_leaves_the_grant_for_that_ship(self):
        """T-5058: twice on 2026-11-19 a land's last ship, covered by a hand ship then testing, spent the grant; the hand
        ship's push then refused ('the publish grant was already spent; nothing was pushed') after its tests"""
        tid = self.lanes_ready(1)[0]
        train._settle(self.p, tid, 0, self.COVERED, {"final": True})
        self.assertTrue(self.held(tid), "the running ship pushes these commits and spends the grant itself")
        train._settle(self.p, tid, 0, "nothing to ship: origin main already has HEAD's changes", {"final": True})
        self.assertFalse(self.held(tid), "nothing left to push: the land's last ship still spends it")

    def test_grant_an_in_line_last_ship_a_running_ship_covers_leaves_the_grant(self):
        from unittest import mock
        tid = self.lanes_ready(1)[0]
        with mock.patch.object(train, "_sampo", return_value=(0, self.COVERED + "\n", "")):
            got = train._ship(self.p, False, self.repo)
        self.assertEqual(got[0], tid)
        self.assertTrue(self.held(tid), "covered by the running ship: not this ship's to spend")
        with mock.patch.object(train, "_sampo", return_value=(0, "nothing to ship: origin main already has HEAD's "
                                                                 "changes\n", "")):
            train._ship(self.p, False, self.repo)
        self.assertFalse(self.held(tid))

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

    def test_times_status_shows_the_last_lands_accept_to_pushed_median(self):
        self.open(9)
        ids = self.lanes_ready(2)
        for tid, ago in zip(ids, (600, 1200)):
            b = self.brief(tid)
            b.meta["accepted_at"] = time.time() - ago
            c.save_brief(self.p, b)
        self.fm("lane", "land", "--batch", "1", env={"SAMPO_LAND_SHIP_WAIT_S": "90"}, timeout=240)
        out = self.fm("status").stdout
        self.assertRegex(out, r"Last land: 2 lane\(s\) in 2 train\(s\); accept-to-pushed median [\d.]+ min", out)
        got = train.last_land(self.p)
        self.assertEqual(len(got["live"]), 2)
        self.assertGreaterEqual(statistics.median(got["live"]), 10 * 60 - 5)


if __name__ == "__main__":
    unittest.main()
