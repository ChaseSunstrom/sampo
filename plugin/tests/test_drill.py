"""T-1669: fm drill runs a scripted day in a temp home with faults planted mid-day (a torn ledger and journal, a stale
lock, a broken lane, a corrupt state file, an unreadable journal) and passes only when every fault is found by fm fsck
or fm verify-ledger, or healed, and nothing crashed. And the fixes its first runs forced: a corrupt or unreadable
state file is named in one line, never a traceback, and an append after a torn tail starts on a line of its own."""
import json
import os
import shutil
import subprocess
import unittest
from unittest import mock

from helpers import ForemanTestCase, read_text

from sampo import core as c
from sampo import drill


def tree(root):
    return sorted(os.path.relpath(os.path.join(d, n), root) for d, _, ns in os.walk(root) for n in ns)


class Drill(ForemanTestCase):
    def test_a_dropped_ledger_class_passes_a_reader_that_names_it_and_fails_one_that_prints_a_zero(self):
        """T-2648: every task_done line gone; fm outcomes and fm digest must say the class is missing."""
        plans = [{"kind": "dropped class", "at": 3}, {"kind": "stale lock", "at": 1}, {"kind": "torn journal", "at": 2}]
        with mock.patch.object(drill, "plan", return_value=[dict(f) for f in plans]):
            r = drill.drill(1, 3)
        got = {f["kind"]: f for f in r["faults"]}
        self.assertEqual(got["dropped class"]["verdict"], "found", got)
        self.assertEqual(r["crashes"], [])
        self.assertTrue(r["ok"], r)
        self.assertIn("dropped class", drill.KINDS)
        zero = subprocess.CompletedProcess([], 0, "0 finished task(s): 0 held\nLast 7 day(s): 0 task(s) done", "")
        real = drill.Day.fm
        fake = lambda self, *a: zero if a[0] in ("outcomes", "digest") else real(self, *a)  # noqa: E731
        with mock.patch.object(drill, "plan", return_value=[dict(f) for f in plans]), \
                mock.patch.object(drill.Day, "fm", fake):
            bad = drill.drill(1, 3)
        self.assertEqual({f["kind"]: f["verdict"] for f in bad["faults"]}["dropped class"], "missed")
        self.assertFalse(bad["ok"])
        shutil.rmtree(bad["home"], ignore_errors=True)

    def test_a_seed_replays_the_same_plan(self):
        self.assertEqual(drill.plan(7, 3), drill.plan(7, 3))
        plans = {json.dumps(drill.plan(s, 3)) for s in range(8)}
        self.assertGreater(len(plans), 1, "seeds differ")
        kinds = [f["kind"] for f in drill.plan(7, 3)]
        self.assertEqual(len(set(kinds)), 3)
        self.assertTrue(set(kinds) <= set(drill.KINDS))
        with self.assertRaises(ValueError):
            drill.plan(1, 2)

    def test_a_drill_passes_names_its_faults_and_leaves_the_home_alone(self):
        before = tree(self.home)
        p = self.fm("drill", "--seed", "5", "--json", check=False)
        got = json.loads(p.stdout)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue(got["ok"])
        self.assertEqual(got["seed"], 5)
        self.assertEqual(len(got["faults"]), 3)
        for f in got["faults"]:
            self.assertIn(f["verdict"], ("found", "healed", "skipped"), f)
        self.assertEqual(got["crashes"], [])
        self.assertEqual(tree(self.home), before)
        self.assertFalse(os.path.exists(got["home"]), "a passing drill removes its temp home")

    def test_every_kind_at_once_still_passes(self):
        p = self.fm("drill", "--seed", "11", "--faults", "7", check=False)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        for kind in drill.KINDS:
            self.assertIn(kind, p.stdout)
        self.assertIn("fm drill --seed 11 --faults 7", p.stdout, "the replay command")

    def test_a_missed_fault_fails_and_keeps_the_home(self):
        with mock.patch.object(drill, "_verdict", return_value=("missed", "planted, not found")):
            r = drill.drill(5, 3)
        self.addCleanup(shutil.rmtree, r["home"], True)
        self.assertFalse(r["ok"])
        self.assertTrue(os.path.isdir(r["home"]), "a failed drill keeps its home to look at")
        self.assertIn("✗", drill.render(r))
        self.assertIn("FAILED", drill.render(r))

    def test_a_traceback_is_a_crash(self):
        self.assertIsNone(drill.crash("fm status", 4, "", "fm: state looks corrupt: x"))
        self.assertIn("fm status", drill.crash("fm status", 1, "", "Traceback (most recent call last):\n  x\n"))


class BriefWrites(unittest.TestCase):
    def test_no_sampo_module_opens_a_brief_for_writing_directly(self):  # T-3811 mtime gate: write_atomic only
        import glob
        import re
        pat = re.compile(r"""open\(\s*(brief|bpath|brief_path)\w*\s*,\s*["']w""")
        lib = os.path.join(os.path.dirname(os.path.abspath(drill.__file__)), "*.py")
        bad = [f"{os.path.basename(p)}:{n}" for p in glob.glob(lib)
               for n, ln in enumerate(open(p, encoding="utf-8"), 1) if pat.search(ln)]
        self.assertEqual(bad, [])


class StateErrors(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)
        self.fm("task", "new", "A thing", "--type", "FIX", "--tier", "S", "--ac", "ok :: true", "--step", "s", "--focus")
        self.log = os.path.join(self.home, "state", "logs", "hooks.log")

    def test_only_a_state_file_permission_error_is_a_state_error(self):
        from sampo import hooks
        self.assertIsNone(hooks._state_error(PermissionError(13, "denied", "/etc/shadow")), "not state: the breaker")
        self.assertIn("permission denied", hooks._state_error(
            PermissionError(13, "denied", os.path.join(c.state_dir(), "events.jsonl"))))

    def test_a_corrupt_meta_json_is_named_never_a_traceback(self):
        meta = os.path.join(self.p.dir, "meta.json")
        with open(meta, "w") as f:
            f.write('{"cut')
        p = self.fm("status", check=False)
        self.assertEqual(p.returncode, 4, p.stderr)
        self.assertIn(meta, p.stderr)
        self.assertIn("sampo fsck", p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        out = self.hook("UserPromptSubmit", {"prompt": "fix the thing"}).stdout
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        self.assertIn(meta, ctx)
        self.assertNotIn("Traceback", read_text(self.log))
        self.assertIn("meta.json", read_text(self.log))
        found = json.loads(self.fm("fsck", "--json", check=False).stdout)
        self.assertIn(("corrupt state", meta), [(f["kind"], f["path"]) for f in found["findings"]])

    @unittest.skipIf(os.geteuid() == 0, "root reads any file")
    def test_an_unreadable_ledger_is_named_never_a_traceback(self):
        ledger = os.path.join(self.p.dir, "ledger.jsonl")
        os.chmod(ledger, 0)
        try:
            p = self.fm("task", "log", "T-0001", "x", check=False)
            self.hook("UserPromptSubmit", {"prompt": "fix the thing"})
        finally:
            os.chmod(ledger, 0o644)
        self.assertEqual(p.returncode, 4, p.stderr)
        self.assertIn(ledger, p.stderr)
        self.assertIn("permission denied", p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertNotIn("Traceback", read_text(self.log) if os.path.exists(self.log) else "")

    def test_an_append_after_a_torn_tail_starts_its_own_line(self):
        events = os.path.join(self.home, "state", "events.jsonl")
        with open(events, "a") as f:
            f.write('{"ev": "cut')
        self.hook("UserPromptSubmit", {"prompt": "fix the thing"})
        lines = read_text(events).splitlines()
        i = lines.index('{"ev": "cut')
        self.assertGreater(len(lines), i + 1)
        for ln in lines[i + 1:]:
            json.loads(ln)
        path = os.path.join(self.tmp, "x.jsonl")
        c.append_line(path, '{"a": 1}\n')
        c.append_line(path, '{"b": 2}\n')
        self.assertEqual(read_text(path), '{"a": 1}\n{"b": 2}\n', "a whole tail gets no blank line")
        # T-2731: a caller that forgets the newline (fmgenesis's prediction score) still leaves whole lines
        c.append_line(path, '{"c": 3}')
        self.assertEqual(read_text(path), '{"a": 1}\n{"b": 2}\n{"c": 3}\n')
        c.append_line(path, "")  # fmdrill's own use: whole lines before a cut, nothing added to a whole tail
        self.assertEqual(read_text(path), '{"a": 1}\n{"b": 2}\n{"c": 3}\n')


if __name__ == "__main__":
    unittest.main()


class RestoreDrill(ForemanTestCase):
    """T-2652: fm drill --restore lays an upgrade snapshot into a temp home and proves each project's record reads
    back; the live state is never touched."""

    def setUp(self):
        super().setUp()
        self.fm("init")
        self.fm("capture", "a thing to keep")
        self.state = os.path.join(self.home, "state")

    def snap(self, name):
        self.fm("rollback", "--snapshot", name)

    def test_a_good_snapshot_restores_and_reads_back_without_touching_the_live_state(self):
        self.snap("1.0.0")
        before = {f: read_text(os.path.join(self.state, f)) for f in tree(self.state) if f.endswith((".json", ".md", ".jsonl"))
                  and not f.startswith(("snapshots/", "logs/"))}
        r = self.fm("drill", "--restore", check=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("1.0.0-", r.stdout)
        self.assertRegex(r.stdout, r"✓ \S+: ledger chain, fsck, readers")
        after = {f: read_text(os.path.join(self.state, f)) for f in before if os.path.exists(os.path.join(self.state, f))}
        self.assertEqual({f for f in before if before[f] != after.get(f)} - {f for f in before if f.endswith("ledger.jsonl")},
                         set())  # nothing but this run's own ledger lines moved

    def test_a_snapshot_whose_ledger_chain_is_broken_fails_naming_its_project(self):
        p = c.project_by_slug(os.listdir(os.path.join(self.state, "projects"))[0])
        led = os.path.join(p.dir, "ledger.jsonl")
        lines = read_text(led).splitlines(True)
        lines[0] = lines[0].replace('"capture"', '"captured"') if '"capture"' in lines[0] else lines[0][:-3] + 'x"}\n'
        with open(led, "w") as f:
            f.writelines(lines)
        self.snap("1.0.1")
        r = self.fm("drill", "--restore", check=False)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertRegex(r.stdout, rf"✗ {os.path.basename(p.dir)}: ledger")

    def test_no_snapshot_is_a_usage_error_naming_how_one_is_made(self):
        r = self.fm("drill", "--restore", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("snapshot", r.stderr + r.stdout)


class DrillLoadGate(unittest.TestCase):
    # T-2822: a drill's day runs its fm with the load gate off, so a loaded host neither stalls nor refuses its lane brief
    def test_a_drill_day_turns_the_load_gate_off_and_drops_the_host_stand_in(self):
        with mock.patch.dict(os.environ, {"FOREMAN_HOST": "load=99,cpus=1"}):
            day = drill.Day()
        self.addCleanup(day.remove)
        self.assertEqual(day.env["FOREMAN_LOAD_GATE"], "0")
        self.assertNotIn("FOREMAN_HOST", day.env)
