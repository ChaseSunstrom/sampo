"""T-4720: processes that outlive a test's temp folder. 325 pre-receive hooks of test_land_ship were found still looping
in deleted /tmp/fmtest-* folders (most over a day old), each forking sleep 20 times a second: about 40 on the load
average, which Sampo's own load gate, slot waits and lane briefs read as a loaded host. run.py ends what lives in a
test folder when it removes it, at its end and in the next run's sweep; sampo doctor counts what lives in a deleted test
folder, sampo tidy --apply ends it, and a removed lane takes its processes with it. Only processes of the same user
whose working folder (or open file, on Linux) is in such a folder qualify, never one matched by name, never this
process or its ancestors. The tests start their own looping children in temp folders; a real process reader is used
only where a child of the test is the one looked for."""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock

from helpers import ForemanTestCase

sys.path.insert(0, os.path.dirname(__file__))
import run  # noqa: E402

from sampo import doctor  # noqa: E402
from sampo import lanes as slanes  # noqa: E402
from sampo import tidy  # noqa: E402

LOOP = "import time\nwhile True: time.sleep(0.05)"
STUBBORN = ("import os, signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "open('ready', 'w').close()\nwhile True: time.sleep(0.05)")  # ready: SIGTERM is ignored from here on
TTY = ("import fcntl, sys, termios, time\nfcntl.ioctl(0, termios.TIOCSCTTY, 0)\n"
       "open(sys.argv[1], 'w').close()\nwhile True: time.sleep(0.05)")  # its stdin is a pty: it takes it as its terminal
HAS_PROC = os.path.isdir("/proc/self")
NOBODY = lambda pids: {}  # noqa: E731  a table of made-up pids: none of them is a live process to read facts of


def fake_proc(root, pid, tty=0, fds=None, ns="mnt:[1]", comm="sleep", stat=None):
    """One /proc/<pid> as proc_facts reads it: stat (tty_nr is the 5th field after the command), fd links, ns/mnt."""
    d = os.path.join(root, str(pid))
    os.makedirs(os.path.join(d, "fd"))
    os.makedirs(os.path.join(d, "ns"))
    with open(os.path.join(d, "stat"), "w") as f:
        f.write(stat if stat is not None else f"{pid} ({comm}) S 1 {pid} {pid} {tty} -1 4194304 1 0 0 0 0 0\n")
    for fd, target in (fds or {}).items():
        os.symlink(target, os.path.join(d, "fd", str(fd)))
    if ns:
        os.symlink(ns, os.path.join(d, "ns", "mnt"))


def have_table():
    return run.proc_table() is not None


class Children(unittest.TestCase):
    """Starts looping children and kills what a test left of them."""

    def setUp(self):
        self.kids = []
        self.addCleanup(self.reap)

    def reap(self):
        for p in self.kids:
            if p.poll() is None:
                p.kill()
            p.wait()

    def loop(self, folder, code=LOOP, detached=True):
        p = subprocess.Popen([sys.executable, "-c", code], cwd=folder, start_new_session=detached,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.kids.append(p)
        until(lambda: p.poll() is not None or cwd_of(p.pid) == os.path.realpath(folder), 15, "the child to start")
        return p

    def loop_tty(self, folder):
        """A looping child that has a controlling terminal, as a person's shell or editor has: a pty's slave is its stdin,
        and it takes the pty as its controlling terminal. The master stays open here until the test is cleaned up."""
        import pty
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        ready = os.path.join(tempfile.mkdtemp(), "ready")
        self.addCleanup(shutil.rmtree, os.path.dirname(ready), True)
        p = subprocess.Popen([sys.executable, "-c", TTY, ready], cwd=folder, start_new_session=True, stdin=slave,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.close(slave)
        self.kids.append(p)
        until(lambda: p.poll() is not None or os.path.exists(ready), 15, "the child to take its terminal")
        return p

    def clear(self, d, **kw):
        """T-4778: run.clear_folder as the test needs it: it ends processes on its own only where /proc is (a scan of about
        5 ms); on macOS every read is an lsof over all processes, so a class's clear reads nothing there (CostPerClass) and
        the end of the run does. A test of the ending asks for the read, as end_run does."""
        kw.setdefault("deep", not HAS_PROC)
        return run.clear_folder(d, **kw)

    def folder(self, prefix=None):
        d = tempfile.mkdtemp(prefix=prefix or f"{run.TMP_PREFIX}t4720-")
        self.addCleanup(shutil.rmtree, d, True)
        return d

    def assertEnds(self, p, secs=15):
        try:
            p.wait(timeout=secs)
        except subprocess.TimeoutExpired:
            self.fail(f"pid {p.pid} still runs {secs}s after its folder was cleared")

    def assertAlive(self, p):
        time.sleep(0.3)
        self.assertIsNone(p.poll(), "a process outside the folder was ended")


def cwd_of(pid):
    try:
        if HAS_PROC:
            return os.readlink(f"/proc/{pid}/cwd")
        out = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], capture_output=True, text=True).stdout
        names = [line[1:] for line in out.splitlines() if line.startswith("n")]
        return os.path.realpath(names[0]) if names else None  # no n line: the process is gone
    except OSError:
        return None


def until(cond, secs, what):
    end = time.time() + secs
    while time.time() < end:
        if cond():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


class RunClearsFolder(Children):
    """The sweep run.py does when it removes a test folder."""

    @unittest.skipUnless(have_table(), "no process table here (no /proc, no lsof)")
    def test_a_detached_child_looping_in_a_test_folder_is_gone_once_the_folder_is_cleared(self):
        d = self.folder()
        kid = self.loop(d)  # its own session: the class's process group never reached it
        self.clear(d)
        self.assertEnds(kid)
        self.assertFalse(os.path.exists(d))

    @unittest.skipUnless(have_table(), "no process table here (no /proc, no lsof)")
    def test_a_child_in_another_folder_is_left_alone(self):
        d, other = self.folder(), self.folder()
        mine, theirs = self.loop(d), self.loop(other)
        self.clear(d)
        self.assertEnds(mine)
        self.assertAlive(theirs)
        self.assertTrue(os.path.isdir(other))

    @unittest.skipUnless(have_table(), "no process table here (no /proc, no lsof)")
    def test_a_folder_whose_name_only_starts_like_another_is_not_inside_it(self):
        d = self.folder()
        sibling = d + "x"  # /tmp/fmtest-t4720-abc and /tmp/fmtest-t4720-abcx
        os.mkdir(sibling)
        self.addCleanup(shutil.rmtree, sibling, True)
        theirs = self.loop(sibling)
        self.clear(d)
        self.assertAlive(theirs)

    @unittest.skipUnless(have_table(), "no process table here (no /proc, no lsof)")
    def test_a_child_in_a_sub_folder_goes_too(self):
        d = self.folder()
        sub = os.path.join(d, "a", "b")
        os.makedirs(sub)
        kid = self.loop(sub)
        self.clear(d)
        self.assertEnds(kid)

    @unittest.skipUnless(have_table(), "no process table here (no /proc, no lsof)")
    def test_a_child_that_ignores_terminate_is_killed_after_the_grace(self):
        d = self.folder()
        kid = self.loop(d, STUBBORN)
        until(lambda: os.path.exists(os.path.join(d, "ready")), 15, "the child to ignore SIGTERM")
        t0 = time.time()
        run.end_procs(lambda: run.holders(run.under(d)), grace=0.4)
        self.assertEnds(kid, 10)
        self.assertEqual(kid.returncode, -signal.SIGKILL)
        self.assertLess(time.time() - t0, 10)

    @unittest.skipUnless(HAS_PROC, "open files are read from /proc")
    def test_a_child_that_left_the_folder_but_holds_a_file_in_it_goes_on_a_deep_read(self):
        d = self.folder()
        held = os.path.join(d, "held")
        kid = self.loop("/", f"import os, time\nf = open({held!r}, 'w')\nwhile True: time.sleep(0.05)")
        until(lambda: os.path.exists(held), 15, "the child to open its file")
        run.clear_folder(d)  # the working-folder read alone does not see it
        self.assertAlive(kid)
        run.clear_folder(d, deep=True)
        self.assertEnds(kid)

    def test_this_process_and_its_ancestors_are_never_listed(self):
        d = self.folder()
        table = [(os.getpid(), d), (os.getppid(), d), (2 ** 22 + 7, d)]
        self.assertEqual(run.holders(run.under(d), table=table), [(2 ** 22 + 7, d)])

    def test_an_unreadable_table_ends_nothing(self):
        sent = []
        self.assertEqual(run.end_procs(lambda: None, kill=lambda *a: sent.append(a)), set())
        self.assertEqual(sent, [])

    def test_only_this_users_processes_are_read(self):
        with tempfile.TemporaryDirectory() as proc:
            os.makedirs(os.path.join(proc, "4242"))
            os.symlink("/somewhere/inside", os.path.join(proc, "4242", "cwd"))
            os.makedirs(os.path.join(proc, "not-a-pid"))
            mine = getattr(os, "getuid", lambda: 0)()
            self.assertEqual(run.proc_table(proc, uid=mine), [(4242, "/somewhere/inside")])
            self.assertEqual(run.proc_table(proc, uid=mine + 1), [])

    def test_a_deleted_working_folder_reads_as_its_path(self):
        with tempfile.TemporaryDirectory() as proc:
            os.makedirs(os.path.join(proc, "7"))
            os.symlink("/tmp/fmtest-1-abc/sub (deleted)", os.path.join(proc, "7", "cwd"))
            mine = getattr(os, "getuid", lambda: 0)()
            self.assertEqual(run.proc_table(proc, uid=mine), [(7, "/tmp/fmtest-1-abc/sub")])


class RunSweeps(Children):
    @unittest.skipUnless(have_table(), "no process table here (no /proc, no lsof)")
    def test_a_dead_runs_folder_is_emptied_of_processes_before_it_is_removed(self):
        gone = subprocess.Popen(["true"])
        gone.wait()  # its pid names a dead run
        with tempfile.TemporaryDirectory() as base:
            dead = os.path.join(base, f"{run.TMP_PREFIX}{gone.pid}-abc")
            live = os.path.join(base, f"{run.TMP_PREFIX}{os.getpid()}-def")
            for x in (dead, live):
                os.mkdir(x)
            hook, mine = self.loop(dead), self.loop(live)  # what a SIGKILLed run left behind, and a live run's
            run.sweep(base)
            self.assertEnds(hook)
            self.assertAlive(mine)
            self.assertEqual(os.listdir(base), [os.path.basename(live)])

    @unittest.skipUnless(have_table(), "no process table here (no /proc, no lsof)")
    def test_the_end_of_a_run_ends_what_its_own_folders_still_hold(self):
        with tempfile.TemporaryDirectory() as base:
            mine = os.path.join(base, f"{run.TMP_PREFIX}{os.getpid()}-abc")
            other = os.path.join(base, f"{run.TMP_PREFIX}{os.getpid() + 1}-abc")  # another run's, live or not
            for x in (mine, other):
                os.mkdir(x)
            kid, theirs = self.loop(mine), self.loop(other)
            run.end_run(base)
            self.assertEnds(kid)
            self.assertAlive(theirs)

    @unittest.skipUnless(have_table(), "no process table here (no /proc, no lsof)")
    def test_a_class_that_leaves_a_detached_child_leaves_no_process_in_its_folder(self):
        with tempfile.TemporaryDirectory() as d:
            pidf = os.path.join(d, "pid")
            with open(os.path.join(d, "test_leaky.py"), "w") as f:
                f.write("import os, subprocess, sys, tempfile, unittest\nclass S(unittest.TestCase):\n"
                        "    def test_s(self):\n"
                        f"        p = subprocess.Popen([sys.executable, '-c', {LOOP!r}], start_new_session=True,\n"
                        "                             cwd=tempfile.mkdtemp(), stdin=subprocess.DEVNULL)\n"
                        f"        open({pidf!r}, 'w').write(str(p.pid))\n")
            from unittest import mock
            with mock.patch.object(run, "HERE", d):
                _, code, err, _, _ = run.run_one("test_leaky.S", [])
            self.assertEqual(code, 0, err)
            with open(pidf) as f:
                pid = int(f.read())
        until(lambda: not os.path.exists(f"/proc/{pid}") if HAS_PROC else cwd_of(pid) is None, 15,
              "the class's detached child to end")


HOLDER = ("import fcntl, os, sys\n"
          "f = open(os.path.join(sys.argv[1], '.runlock'), 'w')\n"
          "fcntl.flock(f, fcntl.LOCK_EX)\n"
          "print('ready', flush=True)\n"
          "sys.stdin.read()\n")  # another run (maybe in another pid namespace) holding its folder's lock


@unittest.skipUnless(have_table() and run.fcntl is not None, "needs a process table and flock")
class HeldFolders(Children):
    """T-4728 with T-4720: a folder a live run holds is its own, whatever its pid reads as; nothing in it is ended."""

    def held_folder(self, base, pid):
        d = os.path.join(base, f"{run.TMP_PREFIX}{pid}-abc")
        os.mkdir(d)
        holder = subprocess.Popen([sys.executable, "-c", HOLDER, d], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  text=True)
        self.addCleanup(lambda: (holder.stdin.close(), holder.wait(30)))
        self.assertEqual(holder.stdout.readline().strip(), "ready")
        return d

    def test_a_sweep_ends_nothing_in_a_dead_pids_folder_another_run_holds(self):
        gone = subprocess.Popen(["true"])
        gone.wait()
        with tempfile.TemporaryDirectory() as base:
            held = self.held_folder(base, gone.pid)
            free = os.path.join(base, f"{run.TMP_PREFIX}{gone.pid}-free")
            os.mkdir(free)
            theirs, leftover = self.loop(held), self.loop(free)
            run.sweep(base)
            self.assertEnds(leftover)
            self.assertAlive(theirs)
            self.assertTrue(os.path.isdir(held))
            self.assertFalse(os.path.exists(free))

    def test_the_end_of_a_run_leaves_a_folder_another_run_holds_under_this_pid_prefix(self):
        with tempfile.TemporaryDirectory() as base:
            held = self.held_folder(base, os.getpid())  # a run in another pid namespace that shares this pid
            free = os.path.join(base, f"{run.TMP_PREFIX}{os.getpid()}-free")
            os.mkdir(free)
            theirs, mine = self.loop(held), self.loop(free)
            run.end_run(base)
            self.assertEnds(mine)
            self.assertAlive(theirs)

    def test_the_end_of_a_run_ends_what_its_own_held_folder_holds(self):
        with tempfile.TemporaryDirectory() as base:
            own = os.path.join(base, f"{run.TMP_PREFIX}{os.getpid()}-own")
            os.mkdir(own)
            lock = run.hold(own)  # this run's lock on its own class folder
            self.assertIsNotNone(lock)
            self.addCleanup(lock.close)
            run.OWN_FOLDERS.add(own)
            self.addCleanup(run.OWN_FOLDERS.discard, own)
            kid = self.loop(own)
            run.end_run(base)
            self.assertEnds(kid)

    def test_clearing_a_class_folder_ends_its_processes_then_closes_the_lock_then_removes_it(self):
        with tempfile.TemporaryDirectory() as base:
            own = os.path.join(base, f"{run.TMP_PREFIX}{os.getpid()}-cls")
            os.mkdir(own)
            lock = run.hold(own)
            kid = self.loop(own)
            self.clear(own, lock=lock)
            self.assertEnds(kid)
            self.assertTrue(lock.closed)
            self.assertFalse(os.path.exists(own))


class TempRoot(Children):
    """A run's own temp folder is /tmp/fmtest-<pid>-*, so the folders a test makes sit under another test folder: the
    tests name the temp root they mean (the outermost test folder under /tmp is the live class folder)."""

    def setUp(self):
        super().setUp()
        from unittest import mock
        patch = mock.patch.object(slanes, "_temp_roots", lambda: [tempfile.gettempdir()])
        patch.start()
        self.addCleanup(patch.stop)


class LibReadsAndEnds(TempRoot):
    """The same read and end in the lib (lanes.py), for doctor, tidy and lane removal."""

    @unittest.skipUnless(have_table(), "no process table here (no /proc, no lsof)")
    def test_the_lib_ends_a_looping_child_in_a_folder_and_leaves_another(self):
        d, other = self.folder(), self.folder()
        mine, theirs = self.loop(d), self.loop(other)
        slanes.end_procs(lambda: slanes.holders(slanes.under(d)), grace=0.4)
        self.assertEnds(mine)
        self.assertAlive(theirs)

    def test_the_lib_never_lists_this_process_or_its_ancestors(self):
        d = self.folder()
        table = [(os.getpid(), d), (os.getppid(), d)]
        self.assertEqual(slanes.holders(slanes.under(d), table=table), [])

    def test_a_deleted_test_folder_is_named_by_its_prefix_under_a_temp_root_and_by_being_gone(self):
        root = tempfile.gettempdir()
        gone = os.path.join(root, f"{run.TMP_PREFIX}9-gone")
        live = self.folder()
        sub = os.path.join(gone, "sub")
        table = [(11, gone), (12, sub), (13, live), (14, os.path.join(live, "sub")),
                 (15, os.path.join(root, "keep-me-gone")), (16, "/home/someone/fmtest-1-x"),
                 (17, os.path.join(gone, f"{run.TMP_PREFIX}2-in")), (18, "/")]
        self.assertEqual([pid for pid, _ in slanes.deleted_test_procs(table=table, facts=NOBODY)], [11, 12, 17])

    def test_the_test_base_switches_the_machine_read_off(self):
        gone = os.path.join(tempfile.gettempdir(), f"{run.TMP_PREFIX}9-gone")
        with unittest.mock.patch.dict(os.environ, {"FOREMAN_NO_LEFTOVER_SCAN": "1"}):
            self.assertEqual(slanes.deleted_test_procs(table=[(11, gone)], facts=NOBODY), [])
            self.assertEqual(slanes.deleted_test_procs(), [])
        with unittest.mock.patch.dict(os.environ):
            os.environ.pop("FOREMAN_NO_LEFTOVER_SCAN", None)
            self.assertEqual(len(slanes.deleted_test_procs(table=[(11, gone)], facts=NOBODY)), 1)

    def test_a_recreated_folder_is_not_a_deleted_one(self):
        d = self.folder()
        self.assertEqual(slanes.deleted_test_procs(table=[(5, d)], facts=NOBODY), [])

    @unittest.skipUnless(HAS_PROC, "lsof may not name a deleted working folder: /proc does")
    def test_a_real_child_in_a_deleted_test_folder_is_found(self):
        d = self.folder()
        kid = self.loop(d)
        shutil.rmtree(d)
        found = [pid for pid, _ in slanes.deleted_test_procs()]
        self.assertIn(kid.pid, found)


class PersonsAreKept(TempRoot):
    """A process with a controlling terminal is a person's (a shell, an editor, a claude session): never ended by the
    sweep, the lane removal, tidy or doctor, only listed. A process in another mount namespace is another sandbox's:
    its /tmp is not ours, so a path that reads as a deleted test folder here may be a live one there."""

    def test_a_process_with_a_terminal_is_never_named_and_is_noted(self):
        d = self.folder()
        facts = lambda pids: {11: (True, True, "bash"), 12: (False, True, "sleep")}  # noqa: E731
        for mod in (run, slanes):
            note = {}
            got = mod.holders(mod.under(d), table=[(11, d), (12, d)], facts=facts, note=note)
            self.assertEqual(got, [(12, d)])
            self.assertEqual(note, {11: (d, "bash", True), 12: (d, "sleep", False)})

    def test_a_table_the_facts_cannot_read_names_nothing(self):
        d = self.folder()
        for mod in (run, slanes):
            self.assertEqual(mod.holders(mod.under(d), table=[(11, d)], facts=lambda pids: None), [])

    def test_the_deleted_folder_read_skips_terminals_and_another_mount_namespace(self):
        gone = os.path.join(tempfile.gettempdir(), f"{run.TMP_PREFIX}9-gone")
        table = [(11, gone), (12, gone), (13, gone), (14, gone)]
        facts = lambda pids: {11: (True, True, "bash"), 12: (False, False, "x"), 13: (False, True, "sleep")}  # noqa: E731
        note = {}  # 14 has no entry: gone since the table was read, still named, and ending it is harmless
        got = slanes.deleted_test_procs(table=table, facts=facts, note=note)
        self.assertEqual([pid for pid, _ in got], [13, 14])
        self.assertEqual(sorted(pid for pid, v in note.items() if v[2]), [11], "the person's one is listed apart")
        self.assertNotIn(12, note, "another namespace's is not ours to judge at all")

    def test_facts_read_the_terminal_and_the_mount_namespace_the_same_in_both_copies(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "self", "ns"))
            os.symlink("mnt:[1]", os.path.join(root, "self", "ns", "mnt"))
            fake_proc(root, 1, tty=34816, comm="bash")  # a controlling terminal
            fake_proc(root, 2, fds={0: "/dev/pts/3", 1: "/dev/null"}, comm="vim")  # a terminal as stdin only
            fake_proc(root, 3, fds={0: "/dev/null", 1: "/dev/null", 2: "/dev/null"}, comm="python3")
            fake_proc(root, 4, ns="mnt:[2]")  # another mount namespace
            fake_proc(root, 5, ns=None)  # a namespace that cannot be read counts as another
            fake_proc(root, 6, stat="junk")  # a stat that cannot be read: a person's until shown otherwise
            fake_proc(root, 8, comm="(sd-pam)", fds={2: "/dev/tty"})
            pids = [1, 2, 3, 4, 5, 6, 7, 8]  # 7: gone
            got = slanes.proc_facts(pids, root)
            self.assertEqual(got, run.proc_facts(pids, root))
            self.assertEqual(got[1], (True, True, "bash"))
            self.assertEqual(got[2], (True, True, "vim"))
            self.assertEqual(got[3], (False, True, "python3"))
            self.assertEqual(got[4][:2], (False, False))
            self.assertEqual(got[5][:2], (False, False))
            self.assertTrue(got[6][0])
            self.assertEqual(got[8], (True, True, "(sd-pam)"))
            self.assertNotIn(7, got)

    def test_facts_without_a_proc_come_from_one_ps_for_all_of_them(self):
        d = self.folder()
        plain, person = self.loop(d), self.loop_tty(d)
        with unittest.mock.patch.object(run, "HAS_PROC", False), \
                unittest.mock.patch.object(slanes, "PROC", "/no-such-proc"):
            for got in (run.proc_facts([plain.pid, person.pid]), slanes.proc_facts([plain.pid, person.pid])):
                self.assertEqual(got[plain.pid][:2], (False, True))
                self.assertEqual(got[person.pid][:2], (True, True))
                self.assertTrue(got[plain.pid][2].lower().startswith("python"), got)  # macOS names it Python

    def test_the_ancestors_without_a_proc_come_from_one_ps_and_equal_the_proc_ones(self):
        with_proc = (run._family(), slanes._family())
        with unittest.mock.patch.object(run, "HAS_PROC", False), unittest.mock.patch.object(slanes, "PROC", "/no-such-proc"):
            no_proc = (run._family(), slanes._family())
        self.assertEqual(with_proc[0], with_proc[1])
        self.assertEqual(no_proc[0], with_proc[0])
        self.assertIn(os.getppid(), no_proc[1])

    @unittest.skipUnless(have_table(), "no process table here (no /proc, no lsof)")
    def test_a_dead_runs_folder_sweep_leaves_a_terminal_process_and_another_namespaces(self):
        gone = subprocess.Popen(["true"])
        gone.wait()
        with tempfile.TemporaryDirectory() as base:
            dead = os.path.join(base, f"{run.TMP_PREFIX}{gone.pid}-abc")
            os.mkdir(dead)
            person, other_ns, hook = self.loop_tty(dead), self.loop(dead), self.loop(dead)
            facts = {person.pid: (True, True, "bash"), other_ns.pid: (False, False, "x")}
            with unittest.mock.patch.object(run, "proc_facts", lambda pids, proc=None: {
                    p: facts.get(p, (False, True, "python3")) for p in pids}):
                run.sweep(base)
            self.assertEnds(hook)
            self.assertAlive(person)
            self.assertAlive(other_ns)
            self.assertEqual(os.listdir(base), [], "the dead folder itself still goes")

    @unittest.skipUnless(have_table(), "no process table here (no /proc, no lsof)")
    def test_a_class_folder_clear_leaves_a_terminal_process(self):
        d = self.folder()
        person, hook = self.loop_tty(d), self.loop(d)
        self.clear(d)
        self.assertEnds(hook)
        self.assertAlive(person)


class CostPerClass(TempRoot):
    """run.py clears a folder after every class: on Linux that is one /proc scan (about 5 ms) and a stat read for each
    hit; without /proc (macOS) it reads nothing per class, since each read is an lsof over every process. The end of the
    run and the next run's sweep do that one read."""

    def test_without_a_proc_a_class_folder_is_removed_without_reading_the_table(self):
        d = self.folder()
        with unittest.mock.patch.object(run, "HAS_PROC", False), \
                unittest.mock.patch.object(run, "proc_table", side_effect=AssertionError("read")) as table:
            run.clear_folder(d)
        table.assert_not_called()
        self.assertFalse(os.path.exists(d))

    def test_without_a_proc_the_end_of_the_run_and_the_sweep_read_it_once_each(self):
        with unittest.mock.patch.object(run, "HAS_PROC", False), \
                unittest.mock.patch.object(run, "proc_table", return_value=[]) as table:
            run.clear_folder(self.folder(), deep=True)
            self.assertEqual(table.call_count, 1)
            run.end_run(tempfile.gettempdir())
            self.assertEqual(table.call_count, 2)
            with tempfile.TemporaryDirectory() as base:
                gone = subprocess.Popen(["true"])
                gone.wait()
                os.mkdir(os.path.join(base, f"{run.TMP_PREFIX}{gone.pid}-x"))
                run.sweep(base)
            self.assertEqual(table.call_count, 3)

    def test_a_test_of_the_ending_asks_for_the_read_where_there_is_no_proc(self):
        # T-4778: macOS's clear_folder reads nothing per class (above), so the tests that need a process ended asked for
        # nothing there and waited 15 s for a child that was never signalled; on Linux they stay on the cheap scan
        d = self.folder()
        for has_proc, deep in ((False, True), (True, False)):
            with unittest.mock.patch(f"{__name__}.HAS_PROC", has_proc), \
                    unittest.mock.patch.object(run, "clear_folder") as clear:
                self.clear(d)
                self.clear(d, deep=not deep)
            self.assertEqual([c.kwargs["deep"] for c in clear.call_args_list], [deep, not deep])

    def test_with_a_proc_a_class_folder_with_nothing_in_it_costs_one_scan_and_no_fact_reads(self):
        d = self.folder()
        with unittest.mock.patch.object(run, "HAS_PROC", True), \
                unittest.mock.patch.object(run, "proc_table", return_value=[]) as table, \
                unittest.mock.patch.object(run, "proc_facts", side_effect=AssertionError("read")) as facts:
            run.clear_folder(d)
        self.assertEqual(table.call_count, 1)
        facts.assert_not_called()


class OneAnswer(TempRoot):
    """run.py must stay importable alone, so the lib keeps its own copy of the process reading and ending: both give the
    same answers on the same inputs."""

    def test_the_two_copies_answer_alike(self):
        d = self.folder()
        with tempfile.TemporaryDirectory() as root:
            for pid, cwd in ((21, d), (22, d + "/sub (deleted)"), (23, "/elsewhere"), (24, d + "x")):
                os.makedirs(os.path.join(root, str(pid)))
                os.symlink(cwd, os.path.join(root, str(pid), "cwd"))
            uid = os.getuid()
            self.assertEqual(sorted(run.proc_table(root, uid)), sorted(slanes.proc_table(root, uid)))
            self.assertEqual(run.proc_table(root, uid + 1), slanes.proc_table(root, uid + 1))
        paths = [d, d + "/a/b", d + "x", "/", "/elsewhere", os.path.dirname(d), d + "/"]
        for folders in ((d,), (d, "/elsewhere"), ("/",), ("",), (os.path.dirname(d) + os.sep,)):
            self.assertEqual([run.under(*folders)(x) for x in paths], [slanes.under(*folders)(x) for x in paths])
        table = [(os.getpid(), d), (os.getppid(), d), (31, d), (32, d + "/z"), (33, "/elsewhere"), (34, d + "x"), (35, d)]
        facts = lambda pids: {31: (True, True, "bash"), 32: (False, True, "sleep"), 35: (False, False, "x")}  # noqa: E731
        for ns in (False, True):
            a, b = {}, {}
            self.assertEqual(run.holders(run.under(d), table=table, facts=facts, note=a, ns=ns),
                             slanes.holders(slanes.under(d), table=table, facts=facts, note=b, ns=ns))
            self.assertEqual(a, b)
        for find in (lambda: [(5, "x")], lambda: [], lambda: None):
            sent = {}
            for name, mod in (("run", run), ("lib", slanes)):
                sent[name] = []
                got = mod.end_procs(find, grace=0.01, poll=0.001, sleep=lambda s: None,
                                    kill=lambda pid, sig, s=sent[name]: s.append((pid, sig)))
                sent[name] = (sorted(got), sent[name])
            self.assertEqual(sent["run"], sent["lib"])


class DoctorAndTidy(TempRoot):
    def reader(self, p, d):
        return lambda: [(p.pid, d)] if p.poll() is None else []

    def test_doctor_passes_when_nothing_lives_in_a_deleted_test_folder(self):
        r = doctor.check_leftover_procs(found=[])
        self.assertEqual((r.name, r.status), ("test leftovers", "PASS"))

    def test_doctor_counts_and_names_them_and_says_what_ends_them(self):
        gone = os.path.join(tempfile.gettempdir(), f"{run.TMP_PREFIX}9-gone")
        found = [(100 + i, gone) for i in range(3)] + [(200, gone + "2")]
        r = doctor.check_leftover_procs(found=found, lines={100: "sh /x/hook", 200: "python3 loop.py"})
        self.assertEqual(r.status, "WARN")
        self.assertIn("4 processes", r.detail)
        self.assertIn("2 deleted test folders", r.detail)
        self.assertIn("100", r.detail)
        self.assertIn("sh /x/hook", r.detail)
        self.assertIn("sampo tidy --apply", r.detail)

    def test_doctor_with_no_reading_says_so_and_does_not_fail(self):
        r = doctor.check_leftover_procs(found=None)
        self.assertEqual(r.status, "PASS")
        self.assertIn("not read", r.detail)

    @unittest.skipUnless(HAS_PROC, "lsof may not name a deleted working folder: /proc does")
    def test_tidy_lists_a_leftover_in_a_dry_run_and_ends_it_with_apply(self):
        d = self.folder()
        kid = self.loop(d)
        shutil.rmtree(d)
        read = lambda: [(p, w) for p, w in self.reader(kid, d)() if True]  # noqa: E731  only the child under test
        actions = []
        out = tidy.leftover_findings(False, actions, reader=read)
        self.assertEqual([f["kind"] for f in out], ["leftover_procs"])
        self.assertTrue(out[0]["auto"])
        self.assertIn("1 process", out[0]["detail"])
        self.assertIsNone(kid.poll(), "a dry run ends nothing")
        self.assertEqual(actions, [])
        out = tidy.leftover_findings(True, actions, reader=read)
        self.assertEnds(kid)
        self.assertEqual(actions, [("global", "leftover_procs")])
        self.assertEqual(tidy.leftover_findings(True, [], reader=read), [], "a second run finds nothing")

    def test_doctor_lists_the_persons_processes_apart_and_never_counts_them_as_leftovers(self):
        gone = os.path.join(tempfile.gettempdir(), f"{run.TMP_PREFIX}9-gone")
        r = doctor.check_leftover_procs(found=[(100, gone)], lines={100: "sh /x/hook", 300: "-bash"},
                                        kept=[(300, gone, "bash")])
        self.assertEqual(r.status, "WARN")
        self.assertIn("1 process in", r.detail)
        self.assertIn("1 with a terminal left alone", r.detail)
        self.assertIn("300", r.detail)
        only = doctor.check_leftover_procs(found=[], kept=[(300, gone, "bash")])
        self.assertEqual(only.status, "PASS", "a person's shell in a removed test folder is not a problem")
        self.assertIn("left alone", only.detail)
        self.assertIn("300", only.detail)

    def test_tidy_lists_the_persons_processes_apart_and_ends_only_the_others(self):
        gone = os.path.join(tempfile.gettempdir(), f"{run.TMP_PREFIX}9-gone")
        d = self.folder()
        hook = self.loop(d)
        out = tidy.leftover_findings(True, [], reader=self.reader(hook, d), kept=[(300, gone, "bash")])
        self.assertEqual([f["kind"] for f in out], ["leftover_procs", "leftover_procs_kept"])
        self.assertFalse(out[1]["auto"])
        self.assertIn("300 bash", out[1]["detail"])
        self.assertEnds(hook)
        out = tidy.leftover_findings(True, [], reader=lambda: [], kept=[(300, gone, "bash")])
        self.assertEqual([f["kind"] for f in out], ["leftover_procs_kept"])

    def test_tidy_with_nothing_to_end_reports_nothing(self):
        self.assertEqual(tidy.leftover_findings(True, [], reader=lambda: []), [])

    def test_tidy_ends_only_what_its_reader_named(self):
        d = self.folder()
        bystander = self.loop(d)  # alive in a live folder: the injected reader does not name it
        self.assertEqual(tidy.leftover_findings(True, [], reader=lambda: []), [])
        self.assertAlive(bystander)


class EndToEnd(TempRoot):
    """Tidy's finding and doctor's row with their default reader, against real children the test starts in deleted test
    folders. Only the table is narrowed to those children: the rest of this machine's processes are never read as
    leftovers by a test, let alone ended. FOREMAN_NO_LEFTOVER_SCAN (set by the test base) is off for this test only."""

    def narrowed(self, *kids):
        real, pids = slanes.proc_table, {k.pid for k in kids}
        return unittest.mock.patch.object(slanes, "proc_table",
                                          lambda *a, **k: [(p, w) for p, w in real(*a, **k) if p in pids])

    @unittest.skipUnless(HAS_PROC, "lsof may not name a deleted working folder: /proc does")
    def test_a_detached_child_in_a_deleted_test_folder_is_found_and_ended_and_a_terminal_one_is_listed_and_left(self):
        d1, d2 = self.folder(), self.folder()
        plain, person = self.loop(d1), self.loop_tty(d2)
        shutil.rmtree(d1)
        shutil.rmtree(d2)
        with unittest.mock.patch.dict(os.environ), self.narrowed(plain, person):
            os.environ.pop("FOREMAN_NO_LEFTOVER_SCAN", None)
            row = doctor.check_leftover_procs()
            self.assertEqual(row.status, "WARN", row.detail)
            self.assertIn("1 process in 1 deleted test folder", row.detail)
            self.assertIn(str(plain.pid), row.detail)
            self.assertIn("1 with a terminal left alone", row.detail)
            self.assertIn(str(person.pid), row.detail)
            actions = []
            out = tidy.leftover_findings(False, actions)
            self.assertEqual([f["kind"] for f in out], ["leftover_procs", "leftover_procs_kept"])
            self.assertIn("1 process", out[0]["detail"])
            self.assertIsNone(plain.poll(), "a dry run ends nothing")
            tidy.leftover_findings(True, actions)
            self.assertEnds(plain)
            self.assertAlive(person)
            self.assertEqual(actions, [("global", "leftover_procs")])
            self.assertEqual([f["kind"] for f in tidy.leftover_findings(True, [])], ["leftover_procs_kept"])
            self.assertEqual(doctor.check_leftover_procs().status, "PASS")


class LaneRemoval(ForemanTestCase, Children):
    def setUp(self):
        ForemanTestCase.setUp(self)
        Children.setUp(self)
        self.fm("init")

    def lane(self):
        self.fm("task", "new", "Laned", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true", "--step", "s")
        return "T-0001", json.loads(self.fm("lane", "new", "T-0001", "--json").stdout)["path"]

    @unittest.skipUnless(have_table(), "no process table here (no /proc, no lsof)")
    def test_removing_a_lane_ends_the_processes_living_in_it_and_only_those(self):
        tid, path = self.lane()
        elsewhere = os.path.join(self.tmp, "elsewhere")
        os.mkdir(elsewhere)
        inside, outside = self.loop(path), self.loop(elsewhere)
        self.fm("lane", "rm", tid)
        self.assertFalse(os.path.isdir(path))
        self.assertEnds(inside)
        self.assertAlive(outside)

    @unittest.skipUnless(have_table(), "no process table here (no /proc, no lsof)")
    def test_removing_a_lane_leaves_a_process_with_a_terminal_and_lists_it_and_says_what_it_ended(self):
        tid, path = self.lane()
        person, hook = self.loop_tty(path), self.loop(path)
        out = self.fm("lane", "rm", tid).stdout
        self.assertFalse(os.path.isdir(path))
        self.assertEnds(hook)
        self.assertAlive(person)
        self.assertRegex(out, r"ended 1 leftover process: [Pp]ython")  # macOS names it Python
        self.assertRegex(out, rf"1 process still stands in the removed lane: {person.pid} [Pp]ython")
        log = self.fm("task", "show", tid).stdout
        self.assertRegex(log, r"ended 1 leftover process: [Pp]ython")
        self.assertRegex(log, rf"1 process still stands in the removed lane: {person.pid} [Pp]ython")

    @unittest.skipUnless(have_table(), "no process table here (no /proc, no lsof)")
    def test_removing_a_lane_with_nothing_running_in_it_says_nothing_of_processes(self):
        tid, path = self.lane()
        out = self.fm("lane", "rm", tid).stdout
        self.assertNotIn("leftover", out)
        self.assertNotIn("still stand", out)

    @unittest.skipUnless(have_table(), "no process table here (no /proc, no lsof)")
    def test_tidy_does_not_remove_a_closed_tasks_lane_a_process_is_working_in(self):
        tid, path = self.lane()
        self.fm("task", "drop", tid, "not needed")  # closed, its clean lane remains: stale_lane's item
        env = {"HOME": self.tmp}
        person = self.loop_tty(path)
        out = self.fm("tidy", "--apply", env=env).stdout
        self.assertIn(f"stale_lane: {tid}", out)
        self.assertTrue(os.path.isdir(path), "a person's shell or editor is never pulled from under them")
        self.assertIn("a process is working in", out)
        self.assertAlive(person)
        person.kill()
        person.wait()
        self.fm("tidy", "--apply", env=env)
        self.assertFalse(os.path.isdir(path), "with nobody in it the lane goes")

    @unittest.skipUnless(have_table(), "no process table here (no /proc, no lsof)")
    def test_a_lane_that_cannot_be_removed_keeps_its_processes(self):
        tid, path = self.lane()
        with open(os.path.join(path, "wip.py"), "w") as f:
            f.write("x = 1\n")  # uncommitted: the removal is refused
        kid = self.loop(path)
        p = self.fm("lane", "rm", tid, check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertTrue(os.path.isdir(path))
        self.assertAlive(kid)


if __name__ == "__main__":
    unittest.main()
