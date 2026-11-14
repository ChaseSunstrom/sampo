"""T-1667: fm fsck reports what a crash or a hand edit left broken in Foreman's own state (a lane whose folder or branch
is gone, a *.pid lock whose process is dead, a journal whose last line is torn) and --fix removes only the stale locks.
Each test runs in its own temp FOREMAN_HOME."""
import argparse
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import time
import unittest
from unittest import mock

from helpers import ForemanTestCase, git_repo, read_text

from sampo import core as c
from sampo import doctor as d

first = lambda text: json.JSONDecoder().raw_decode(text)[0]  # --json prints the task first


def dead_pid():
    proc = subprocess.Popen(["true"])
    proc.wait()  # reaped: the pid names no process now
    return proc.pid


def write(path, text, age=0):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    if age:
        os.utime(path, (time.time() - age, time.time() - age))
    return path


class Fsck(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)
        self.tid = self.task(self.p, "Gone lane", lane=os.path.join(self.tmp, "gone-lane"), lane_branch="foreman/gone")
        self.stale = write(os.path.join(self.p.dir, "sentinel.pid"), f"{dead_pid()}\n", age=7200)
        self.live = write(os.path.join(self.p.dir, "ambient", "run.pid"), str(os.getpid()), age=7200)
        self.ledger = os.path.join(self.p.dir, "ledger.jsonl")
        with open(self.ledger, "a") as f:
            f.write('{"event": "cut sho')  # a crash mid-append
        self.ledger_bytes = read_text(self.ledger)

    def task(self, p, title, **meta):
        tid = first(self.fm("task", "new", title, "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true",
                            "--step", "s", "--json", cwd=p.root).stdout)["id"]
        b = next(x for x in c.load_briefs(p) if x.id == tid)
        b.meta.update(meta)
        c.save_brief(p, b)
        return tid

    def ns(self, inode):
        """This process's PID namespace, as --fix reads it: a link whose text is pid:[inode]."""
        link = os.path.join(self.tmp, f"ns-{inode}")
        if not os.path.lexists(link):
            os.symlink(f"pid:[{inode}]", link)
        return mock.patch.object(d, "PID_NS", link)

    def fix(self, *args):
        """fm fsck --fix as the CLI runs it, in this process, in the initial PID namespace."""
        out = io.StringIO()
        with self.ns(4026531836), contextlib.redirect_stdout(out):
            rc = d.cmd_fsck(argparse.Namespace(fix=True, json=True, project=self.p.slug, **dict(args)))
        return rc, json.loads(out.getvalue())

    def kinds(self, res):
        return sorted((f["kind"], f.get("task") or os.path.basename(f["path"])) for f in res["findings"])

    def test_reports_the_broken_lane_the_stale_lock_and_the_torn_journal(self):
        r = self.fm("fsck", "--json", check=False)
        self.assertEqual(r.returncode, 1, r.stderr)
        res = json.loads(r.stdout)
        self.assertFalse(res["ok"])
        self.assertEqual(self.kinds(res), [("broken lane", self.tid), ("stale lock", "sentinel.pid"),
                                           ("torn journal", "ledger.jsonl")])
        self.assertTrue(all(f["project"] == self.p.slug for f in res["findings"]))
        self.assertTrue(os.path.exists(self.stale), "the report alone removes nothing")
        text = self.fm("fsck", check=False).stdout
        self.assertIn(f"{self.tid}: its folder is gone", text)
        self.assertIn(f"fm lane rm {self.tid}", text)
        self.assertIn("fm fsck --fix", text)
        self.assertTrue(all(self.p.slug in ln for ln in text.splitlines() if ln.startswith(d.FSCK_KINDS)))

    def test_record_leaves_lanes_to_the_machine_they_were_on(self):
        # T-4677: a lane's folder and branch belong to the machine the record was written on; a restore rehearsal
        # judges the record alone, or it goes red when a folder on the live machine is removed
        r = self.fm("fsck", "--record", "--json", check=False)
        self.assertEqual(self.kinds(json.loads(r.stdout)), [("stale lock", "sentinel.pid"), ("torn journal", "ledger.jsonl")])
        self.assertNotIn("its folder is gone", self.fm("fsck", "--record", check=False).stdout)

    def test_fix_removes_only_the_stale_lock(self):
        b = next(x for x in c.load_briefs(self.p) if x.id == self.tid)
        rc, res = self.fix()
        self.assertEqual(rc, 1, "the lane and the journal are left for a person")
        self.assertEqual(res["fixed"], [self.stale])
        self.assertEqual(self.kinds(res), [("broken lane", self.tid), ("torn journal", "ledger.jsonl")])
        self.assertFalse(os.path.exists(self.stale))
        self.assertTrue(os.path.exists(self.live), "a lock whose process lives is kept")
        self.assertEqual(read_text(self.ledger), self.ledger_bytes, "the hash-chained ledger is never rewritten")
        self.assertEqual(next(x for x in c.load_briefs(self.p) if x.id == self.tid).meta, b.meta)
        rc, again = self.fix()  # a second run: nothing more to fix
        self.assertEqual(again["fixed"], [])
        self.assertEqual(len(again["findings"]), 2)

    def test_fix_ends_a_whole_record_that_lost_only_its_newline(self):  # T-2731: gen-3/prediction-score.jsonl
        whole = write(os.path.join(self.p.dir, "research", "score.jsonl"), '{"a": 1}\n{"b": 2}')
        cut = write(os.path.join(self.p.dir, "cut.jsonl"), '{"a": 1}\n{"b": ')
        rc, res = self.fix()
        self.assertIn(whole, res["fixed"])
        self.assertEqual(read_text(whole), '{"a": 1}\n{"b": 2}\n', "only the newline is added")
        self.assertEqual(read_text(cut), '{"a": 1}\n{"b": ', "a record cut mid-way is left for a person")
        self.assertEqual(read_text(self.ledger), self.ledger_bytes, "the hash-chained ledger is never rewritten")
        self.assertIn(("torn journal", "cut.jsonl"), self.kinds(res))
        write(whole, '{"c": 3}')
        p = self.fm("fsck", "--fix", check=False)
        text = p.stdout
        from sampo import doctor
        if doctor._pid_ns_doubt():  # T-3222: in a sandbox (run.py --sandbox) --fix refuses by design: say so instead
            self.assertIn("PID namespace", p.stderr)
            return
        self.assertIn("its whole last record got its newline", text, p.stderr[-600:])
        self.assertNotIn("score.jsonl: a stale lock", text)

    def test_fix_keeps_a_lock_younger_than_an_hour(self):
        # review: a holder in another PID namespace (a sandbox) looks dead to os.kill; an hour is the sentinel's own limit
        os.utime(self.stale, (time.time() - 1800, time.time() - 1800))
        self.assertNotIn("stale lock", [f["kind"] for f in d.fsck()])
        self.fix()
        self.assertTrue(os.path.exists(self.stale))

    @unittest.skipUnless(sys.platform.startswith("linux"), "PID namespaces are Linux's")
    def test_fix_is_refused_where_it_cannot_see_every_process(self):
        for patch in (self.ns(4026532999), mock.patch.object(d, "PID_NS", os.path.join(self.tmp, "none"))):
            with patch, self.assertRaises(c.PolicyError) as e:
                d.fsck_fix(d.fsck())
            self.assertIn("PID namespace", str(e.exception))
        self.assertTrue(os.path.exists(self.stale))
        with self.ns(4026532999):  # fm doctor --fix says so instead of crashing
            fixed, left = d.fix([d.check_fsck()])
        self.assertEqual(fixed, [])
        self.assertIn("PID namespace", left[0])
        self.assertTrue(os.path.exists(self.stale))

    def test_doctor_fix_removes_the_stale_locks_too(self):
        with self.ns(4026531836):
            fixed, left = d.fix([d.check_fsck()])
        self.assertFalse(os.path.exists(self.stale))
        self.assertIn("removed 1 stale lock", " ".join(fixed + left))
        self.assertIn("broken lane", " ".join(left), "the lane and the journal are still findings")

    def test_a_healthy_lane_passes_and_one_whose_branch_or_git_record_is_gone_is_broken(self):
        tree = os.path.join(self.tmp, "lane")
        subprocess.run(["git", "-C", self.repo, "worktree", "add", "-q", "-b", "foreman/lane", tree], check=True)
        tid = self.task(self.p, "Live lane", lane=tree, lane_branch="foreman/lane")
        detail = lambda t: next((f["detail"] for f in d.fsck() if f.get("task") == t), None)  # noqa: E731
        self.assertIsNone(detail(tid))
        pruned = os.path.join(self.tmp, "pruned")
        subprocess.run(["git", "-C", self.repo, "worktree", "add", "-q", "-b", "foreman/pruned", pruned], check=True)
        ptid = self.task(self.p, "Pruned lane", lane=pruned, lane_branch="foreman/pruned")
        shutil.rmtree(os.path.join(self.repo, ".git", "worktrees", "pruned"))  # what git worktree prune leaves
        self.assertIn("git record is gone", detail(ptid))
        subprocess.run(["git", "-C", tree, "checkout", "-q", "--detach"], check=True)
        subprocess.run(["git", "-C", self.repo, "branch", "-q", "-D", "foreman/lane"], check=True)
        self.assertIn("its branch foreman/lane is gone", detail(tid))

    def test_another_projects_lane_names_its_project_and_no_lane_rm_from_here(self):
        other = git_repo(self.tmp, "other")
        self.fm("init", cwd=other)
        op = c.find_project(other)
        otid = self.task(op, "Their lane", lane=os.path.join(self.tmp, "their-gone-lane"))
        lines = [ln for ln in self.fm("fsck", check=False).stdout.splitlines() if "their-gone-lane" in ln]
        self.assertEqual(len(lines), 1)
        self.assertIn(op.slug, lines[0])
        self.assertIn(f"{otid}: its folder is gone", lines[0])
        self.assertNotIn("fm lane rm", lines[0], "an id from another project names a different task here")

    def test_a_closed_task_and_a_young_or_odd_lock_are_not_findings(self):
        b = next(x for x in c.load_briefs(self.p) if x.id == self.tid)
        b.meta["status"] = "dropped"  # a closed task's lane is fm tidy's to sweep
        c.save_brief(self.p, b)
        young = write(os.path.join(self.p.dir, "young.pid"), "")  # an empty pid file may still be getting its pid
        zero = write(os.path.join(self.p.dir, "zero.pid"), "0", age=7200)  # never os.kill(0): no process, stale
        big = write(os.path.join(self.p.dir, "big.pid"), "x" * 100, age=7200)  # not a pid file of ours: kept
        outside = write(os.path.join(self.tmp, "outside"), "1", age=7200)
        os.symlink(outside, os.path.join(self.p.dir, "link.pid"))  # a symlink is never followed nor removed
        os.mkfifo(os.path.join(self.p.dir, "pipe.jsonl"))  # a FIFO would block a plain open forever
        found = {os.path.basename(f["path"]): f for f in d.fsck() if f["kind"] == "stale lock"}
        self.assertEqual(sorted(found), ["sentinel.pid", "zero.pid"])
        self.assertNotIn("broken lane", [f["kind"] for f in d.fsck()])
        self.fix()
        self.assertTrue(all(os.path.lexists(x) for x in (young, big, outside, os.path.join(self.p.dir, "link.pid"))))
        self.assertFalse(os.path.exists(zero))

    def test_a_journal_whose_last_line_is_whole_passes(self):
        with open(self.ledger, "a") as f:
            f.write('rt"}\n')  # the line completed
        self.assertNotIn("torn journal", [f["kind"] for f in d.fsck()])
        write(os.path.join(self.p.dir, "mail.jsonl"), '{"a": 1}\nnot json\n')
        deep = write(os.path.join(self.p.dir, "deep.jsonl"), '{"a": 1}\n' + "[" * 60000 + "\n")  # RecursionError
        torn = sorted(os.path.basename(f["path"]) for f in d.fsck() if f["kind"] == "torn journal")
        self.assertEqual(torn, ["deep.jsonl", "mail.jsonl"])
        self.assertTrue(os.path.exists(deep))

    def test_a_line_that_lands_while_it_is_read_is_not_torn(self):
        # events.jsonl is appended all the time: a tail without its newline is read once more before it is reported
        def finish(_):
            if not read_text(self.ledger).endswith("\n"):  # once, whoever else sleeps meanwhile
                with open(self.ledger, "a") as f:
                    f.write('rt"}\n')
        with mock.patch.object(d.time, "sleep", side_effect=finish):
            self.assertNotIn("torn journal", [f["kind"] for f in d.fsck()])

    @unittest.skipIf(os.geteuid() == 0, "root reads any file")
    def test_an_unreadable_lock_or_journal_is_reported_not_skipped(self):
        lock = write(os.path.join(self.p.dir, "shut.pid"), "1", age=7200)
        journal = write(os.path.join(self.p.dir, "shut.jsonl"), "{}\n")
        for x in (lock, journal):
            os.chmod(x, 0)
        found = {os.path.basename(f["path"]): f["kind"] for f in d.fsck()}
        self.assertEqual((found.get("shut.pid"), found.get("shut.jsonl")), ("unreadable", "unreadable"))
        self.fix()
        self.assertTrue(os.path.exists(lock), "an unreadable lock is never removed")

    def test_a_state_file_that_isnt_json_is_corrupt_state(self):
        # T-1669: a corrupt meta.json stopped every fm verb, fm doctor included, and nothing named it
        gate = write(os.path.join(self.p.dir, "gate.json"), '{"cut')
        write(os.path.join(self.p.dir, "meta.json.bak"), "not json, not judged")
        write(os.path.join(c.state_dir(), "sessions", "s.tripfile-1.json"), "")  # an empty marker in a folder
        write(os.path.join(self.p.dir, "audits", "T-0001.dirty.json"), "{")  # only a project's or state's own files
        found = [(os.path.basename(f["path"]), f["detail"]) for f in d.fsck() if f["kind"] == "corrupt state"]
        self.assertEqual([n for n, _ in found], ["gate.json"])
        self.assertIn("isn't valid JSON", found[0][1])
        self.assertIn("1 corrupt state", d.check_fsck().detail)
        self.assertEqual(gate, next(f["path"] for f in d.fsck() if f["kind"] == "corrupt state"))

    def test_doctor_reports_the_one_line_summary(self):
        r = d.check_fsck()
        self.assertEqual(r.status, "WARN")
        self.assertIn("1 broken lane, 1 stale lock, 1 torn journal", r.detail)
        self.assertIn("fm fsck", r.detail)
        os.remove(self.stale)
        with open(self.ledger, "a") as f:
            f.write('rt"}\n')
        b = next(x for x in c.load_briefs(self.p) if x.id == self.tid)
        b.meta.pop("lane")
        c.save_brief(self.p, b)
        self.assertEqual(d.check_fsck().status, "PASS")


if __name__ == "__main__":
    unittest.main()


class GateLock(ForemanTestCase):
    """T-1668: one machine-wide gate lock; a second gate run waits, saying who holds it."""

    def test_only_a_full_check_run_takes_the_lock(self):  # T-1743: builders' --affected runs mustn't queue
        import argparse
        import contextlib
        from unittest import mock
        from sampo import cli
        took = []

        @contextlib.contextmanager
        def lock(label, **kw):
            took.append(label)
            yield
        with mock.patch.object(c, "gate_lock", lock), mock.patch.object(cli, "_cmd_check", return_value=0):
            for affected, repeat, want in ((True, 0, []), (False, 3, []), (False, 0, ["sampo check"])):
                took.clear()
                cli.cmd_check(argparse.Namespace(action="run", affected=affected, repeat=repeat))
                self.assertEqual(took, want, (affected, repeat))

    def test_a_long_held_lock_is_waited_on_only_so_long(self):  # T-1753: a hung check elsewhere held it 40+ min
        import argparse
        import threading
        from unittest import mock
        from sampo import cli
        held, release = threading.Event(), threading.Event()

        def hold():
            with c.gate_lock("hung check"):
                held.set()
                release.wait(10)
        t = threading.Thread(target=hold)
        t.start()
        held.wait(5)
        err = io.StringIO()
        try:
            with mock.patch.dict(os.environ, {"FOREMAN_GATE_WAIT": "0.3"}), mock.patch("sys.stderr", err), \
                    mock.patch.object(cli, "_cmd_check", return_value=0) as run:
                self.assertEqual(cli.cmd_check(argparse.Namespace(action="run", affected=False, repeat=0)), 0)
            run.assert_called_once()
            self.assertIn("hung check", err.getvalue())
            self.assertIn("running beside it", err.getvalue())
        finally:
            release.set()
            t.join()
        with mock.patch.object(cli, "_cmd_check", side_effect=c.LockTimeout("project lock busy")):
            with self.assertRaises(c.LockTimeout):  # the check's own lock timeout isn't mistaken for the gate's
                cli.cmd_check(argparse.Namespace(action="run", affected=False, repeat=0))

    def test_second_run_waits_and_names_holder(self):
        import threading
        said, order, held, release = [], [], threading.Event(), threading.Event()

        def first():
            with c.gate_lock("release check"):
                order.append("first in")
                held.set()
                release.wait(5)
                order.append("first out")

        def second():
            with c.gate_lock("canary", say=said.append):
                order.append("second in")

        t1 = threading.Thread(target=first)
        t1.start()
        held.wait(5)
        t2 = threading.Thread(target=second)
        t2.start()
        for _ in range(250):  # the waiter says so once it has found the lock busy: no fixed sleep
            if said:
                break
            time.sleep(0.02)
        self.assertEqual(order, ["first in"])  # the second still waits
        self.assertTrue(said and "release check" in said[0] and str(os.getpid()) in said[0])
        release.set()
        t1.join(5)
        t2.join(5)
        self.assertEqual(order, ["first in", "first out", "second in"])

    def test_nested_run_does_not_deadlock(self):
        code = "from sampo import core as c\nwith c.gate_lock('inner', timeout=1):\n    print('ok')"
        with c.gate_lock("outer"):
            r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=20,
                               env={**os.environ, "PYTHONPATH": os.path.dirname(os.path.dirname(c.__file__))})
        self.assertEqual(r.stdout.strip(), "ok", r.stderr)

    def test_stale_held_value_takes_the_lock(self):
        # FOREMAN_GATE_HELD=999999 with no holder: ignored, the lock is really taken, and a second process waits on us
        code = "from sampo import core as c\nwith c.gate_lock('probe', timeout=0.5):\n    print('ok')"
        env = {**os.environ, "PYTHONPATH": os.path.dirname(os.path.dirname(c.__file__)), "FOREMAN_GATE_HELD": "999999"}
        os.environ["FOREMAN_GATE_HELD"] = "999999"
        try:
            with c.gate_lock("stale"):
                self.assertEqual(os.environ["FOREMAN_GATE_HELD"], str(os.getpid()))  # overwritten
                os.environ["FOREMAN_GATE_HELD"] = "999999"  # a child with a wrong value is no child of ours
                r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=20, env=env)
        finally:
            os.environ.pop("FOREMAN_GATE_HELD", None)
        self.assertNotIn("ok", r.stdout)
        self.assertIn("LockTimeout", r.stderr)
