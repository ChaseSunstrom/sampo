"""T-5769: the context keeper for the lead. An idle lead pane at a task boundary with its context above 60% is typed one
`/compact <focus>` (focus: `sampo resume --line`), at most once per 30 minutes per session, never over typed text or an open
dialog; a snapshot with no context percentage is read from the session transcript. No tmux pane: the Stop hook prints the
same line instead. A fake tmux answers capture-pane from a file and logs every call: no test touches a real pane."""
import datetime
import json
import os
import subprocess
import time
import unittest

from test_code import CodeCase, call, plan
from test_code import journal_text
from test_hooks import HookCase


def live_child(case):
    """A real process (a sleep) that is this test's child: the stand-in for the claude under a tmux pane's shell. Its
    (pid, start time) is what the statusline records as claude_pid and claude_start."""
    from sampo import bus
    proc = subprocess.Popen(["sleep", "600"])
    case.addCleanup(proc.wait)
    case.addCleanup(proc.kill)
    return proc, bus._proc(proc.pid)[1]


class KeeperRig:
    """Mixin for a HookCase: a fake tmux, a lead's statusline snapshot, the keeper's own state file. The fake tmux answers
    capture-pane from a file (the first `busy_for` reads from busy.txt) and display-message from another (the pane's
    command and pid); a pane's pid is this test, the parent of the stand-in claude the snapshot names."""
    PANE = "%7"

    def setUp(self):
        super().setUp()
        self.fm("init")
        self.bin = os.path.join(self.tmp, "fakebin")
        os.makedirs(self.bin)
        self.log, self.pane_file = os.path.join(self.tmp, "tmux.log"), os.path.join(self.tmp, "pane.txt")
        self.cmd_file, self.busy_file = os.path.join(self.tmp, "pane_cmd.txt"), os.path.join(self.tmp, "busy.txt")
        self.count_file, self.busy_for = os.path.join(self.tmp, "reads"), os.path.join(self.tmp, "busy_for")
        with open(os.path.join(self.bin, "tmux"), "w") as f:
            f.write(f'#!/bin/sh\necho "$@" >> "{self.log}"\n'
                    f'if [ "$1" = display-message ]; then cat "{self.cmd_file}"; fi\n'
                    f'if [ "$1" = capture-pane ]; then\n'
                    f'  n=$(cat "{self.count_file}" 2>/dev/null || echo 0); n=$((n+1)); echo $n > "{self.count_file}"\n'
                    f'  if [ -f "{self.busy_for}" ] && [ "$n" -le "$(cat "{self.busy_for}")" ]; then cat "{self.busy_file}"\n'
                    f'  else cat "{self.pane_file}"; fi\n'
                    f'fi\n')
        os.chmod(os.path.join(self.bin, "tmux"), 0o755)
        self.proc, self.start = live_child(self)
        self.runs("claude")
        self.cfg = os.path.join(self.tmp, "cfg")
        self.env = {"PATH": self.bin + os.pathsep + os.environ["PATH"], "CLAUDE_CONFIG_DIR": self.cfg}
        self.sessions = os.path.join(self.home, "state", "sessions")
        os.makedirs(self.sessions, exist_ok=True)
        self.show()

    def show(self, prompt="❯ "):
        with open(self.pane_file, "w") as f:
            f.write(f"some output\n\n{prompt}\n  ? for shortcuts\n")

    def show_raw(self, text):
        with open(self.pane_file, "w") as f:
            f.write(text)

    def runs(self, command, pid=None):
        """What `tmux display-message` says of the pane: its foreground command and the pid of its first process."""
        with open(self.cmd_file, "w") as f:
            f.write(f"{command}\t{pid or os.getpid()}\n")

    def snap(self, sid="lead-1", pct=72, pane=PANE, age_s=5, cwd=None, size=200000, pid="mine", **extra):
        ts = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=age_s)).strftime("%Y-%m-%dT%H:%M:%SZ")
        body = {"ts": ts, "session_id": sid, "cwd": cwd or self.repo, "context_pct": pct, "context_size": size,
                "tmux_pane": pane, **extra}
        if pid == "mine":
            body.update(claude_pid=self.proc.pid, claude_start=self.start)
        elif pid:
            body["claude_pid"] = pid
        if pct is None:
            body.pop("context_pct")
        body.update(extra)  # after the pid's own fields: a test may overwrite claude_start
        if pane is None:
            body.pop("tmux_pane")
        with open(os.path.join(self.sessions, f"{sid}.json"), "w") as f:
            json.dump(body, f)

    def keys(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            return [ln.rstrip("\n") for ln in f if ln.startswith("send-keys")]

    def keep(self, *args):
        return self.fm_json("keeper", *args, env=self.env)

    def state_path(self):
        return os.path.join(self.home, "state", "keeper.json")

    def age_record(self, sid, seconds):
        with open(self.state_path()) as f:
            st = json.load(f)
        st["panes"][sid]["at"] -= seconds
        with open(self.state_path(), "w") as f:
            json.dump(st, f)

    def queued(self, title="Fix the login"):
        return self.task(title, focus=False)


class KeeperCompact(KeeperRig, HookCase):
    def test_compact_is_typed_once_with_the_resume_focus(self):
        tid = self.queued()
        self.snap()
        r = self.keep()
        self.assertEqual([x["action"] for x in r["sessions"]], ["sent"])
        keys = self.keys()
        self.assertEqual(len(keys), 2, keys)
        self.assertTrue(keys[0].startswith(f"send-keys -t {self.PANE} -l /compact "), keys[0])
        self.assertIn(tid, keys[0], "the focus comes from the resume line: the next task in the queue")
        self.assertEqual(keys[1], f"send-keys -t {self.PANE} Enter")

    def test_compact_focus_is_one_clean_line(self):
        self.queued("Fix the login\n/exit\x1b[31m now")
        self.snap()
        self.keep()
        typed = self.keys()[0]
        self.assertNotIn("\x1b", typed)
        self.assertLessEqual(len(typed), 400)

    def test_compact_resume_line_names_the_active_task_and_step(self):
        tid = self.task("Fix the login")  # focused
        out = self.fm("resume", "--line", "--no-check").stdout.strip()
        self.assertEqual(len(out.splitlines()), 1, out)
        self.assertIn(tid, out)
        self.assertIn("step 1/2", out)

    def test_compact_resume_line_with_no_task(self):
        out = self.fm("resume", "--line").stdout.strip()
        self.assertEqual(len(out.splitlines()), 1, out)
        self.assertIn("no active task", out)

    def test_compact_not_while_a_task_is_in_hand(self):
        self.task("Fix the login")  # focused: mid-task, not a boundary
        self.snap()
        r = self.keep()
        self.assertEqual(self.keys(), [])
        self.assertEqual(r["sessions"][0]["why"], "mid-task")

    def test_compact_only_above_sixty_percent(self):
        self.queued()
        for pct, want in ((None, 0), (30, 0), (60, 0), (61, 2)):
            with self.subTest(pct=pct):
                if os.path.exists(self.state_path()):
                    os.unlink(self.state_path())
                if os.path.exists(self.log):
                    os.unlink(self.log)
                self.snap(pct=pct)
                self.keep()
                self.assertEqual(len(self.keys()), want)

    def test_compact_never_over_typed_text(self):
        self.queued()
        self.show("❯ fix the thing")
        self.snap()
        r = self.keep()
        self.assertEqual(self.keys(), [])
        self.assertEqual(r["sessions"][0]["why"], "typed")
        self.show()
        self.keep()
        self.assertEqual(len(self.keys()), 2, "an empty box again: it goes")

    def test_compact_never_with_a_dialog_open(self):
        self.queued()
        self.show_raw("Do you want to proceed?\n ❯ 1. Yes\n   2. No\n\n Esc to cancel · Tab to amend\n")
        self.snap()
        r = self.keep()
        self.assertEqual(self.keys(), [])
        self.assertEqual(r["sessions"][0]["why"], "dialog")

    def test_compact_never_while_the_turn_runs(self):
        self.queued()
        self.show_raw("✻ Working… (12s · esc to interrupt)\n\n❯ \n")
        self.snap()
        r = self.keep()
        self.assertEqual(self.keys(), [])
        self.assertEqual(r["sessions"][0]["why"], "busy")

    def test_compact_once_per_thirty_minutes_and_after_a_fresh_snapshot(self):
        self.queued()
        self.snap()
        self.keep()
        self.assertEqual(len(self.keys()), 2)
        self.keep()
        self.assertEqual(len(self.keys()), 2, "a second run at once: nothing")
        self.age_record("lead-1", 31 * 60)
        r = self.keep()
        self.assertEqual(len(self.keys()), 2, "30 min passed but the snapshot is the old one: the compact hasn't shown yet")
        self.assertIn("same snapshot", r["sessions"][0]["why"])
        self.snap()  # the statusline wrote again: the context is still over 60%
        self.keep()
        self.assertEqual(len(self.keys()), 4)
        self.age_record("lead-1", 10 * 60)
        self.snap()
        self.keep()
        self.assertEqual(len(self.keys()), 4, "10 min on: inside the 30")

    def test_compact_reads_the_context_from_the_transcript_when_the_snapshot_has_none(self):
        self.queued()
        self.snap(pct=None)
        folder = os.path.join(self.cfg, "projects", "".join(ch if ch.isalnum() else "-" for ch in self.repo))
        os.makedirs(folder, exist_ok=True)
        now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        use = lambda n: {"type": "assistant", "timestamp": now, "sessionId": "lead-1", "message": {
            "id": f"m{n}", "usage": {"input_tokens": 10, "cache_read_input_tokens": n, "cache_creation_input_tokens": 0,
                                     "output_tokens": 5}, "content": []}}
        with open(os.path.join(folder, "lead-1.jsonl"), "w") as f:
            f.write(json.dumps(use(30000)) + "\n" + json.dumps(use(150000)) + "\n")
        env_cwd = self.keep()
        self.assertEqual(len(self.keys()), 2, env_cwd)
        self.assertEqual(env_cwd["sessions"][0]["pct"], 75)

    def test_compact_ignores_a_session_of_another_project_and_a_stale_snapshot(self):
        self.queued()
        self.snap(sid="elsewhere", cwd=self.tmp)
        self.snap(sid="old", age_s=9 * 3600)
        r = self.keep()
        self.assertEqual(self.keys(), [])
        self.assertEqual(r["sessions"], [])

    def test_compact_ignores_a_pane_that_is_not_a_pane_id(self):
        self.queued()
        self.snap(pane="%7; rm -rf /")
        self.keep()
        self.assertEqual(self.keys(), [])

    def test_compact_records_the_first_one_for_the_cost_comparison(self):
        self.queued()
        self.snap()
        self.keep()
        with open(self.state_path()) as f:
            first = json.load(f)["first"]
        self.assertLess(abs(first - time.time()), 60)
        self.age_record("lead-1", 31 * 60)
        self.snap()
        self.keep()
        with open(self.state_path()) as f:
            self.assertEqual(json.load(f)["first"], first, "the first stays the first")


class KeeperSafety(KeeperRig, HookCase):
    """T-5769 review: a pane is typed into only when it is the live Claude Code session the snapshot names; the focus is
    plain words; a locked state file is a row, not a crash."""

    def go(self, **kw):
        self.queued()
        self.snap(**kw)
        return self.keep()["sessions"][0]

    def test_compact_never_into_a_pane_running_bash_with_a_prompt(self):
        self.runs("bash")
        self.show("> ")  # a shell's PS2 or a here-doc prompt: the same glyph as Claude Code's
        row = self.go()
        self.assertEqual(self.keys(), [])
        self.assertEqual(row["action"], "skipped")
        self.assertIn("bash", row["why"])

    def test_compact_never_on_a_pid_mismatch(self):
        other = subprocess.Popen(["sleep", "600"])  # a pane whose first process is not an ancestor of the claude
        self.addCleanup(other.wait)
        self.addCleanup(other.kill)
        self.runs("claude", pid=other.pid)
        row = self.go()
        self.assertEqual(self.keys(), [])
        self.assertIn("pid", row["why"])

    def test_compact_never_when_the_snapshot_names_no_claude_pid(self):
        row = self.go(pid=None)
        self.assertEqual(self.keys(), [])
        self.assertIn("pid", row["why"])

    def test_compact_never_when_the_pid_was_reused(self):
        row = self.go(claude_start="1")  # the pid is live, but it is not the process that wrote the snapshot
        self.assertEqual(self.keys(), [])
        self.assertIn("pid", row["why"])

    def test_compact_never_on_a_bare_prompt_with_no_claude_code_screen(self):
        self.show_raw("some output\n\n❯ \n")  # the claude command, the right pid, but no Claude Code footer: not its screen
        row = self.go()
        self.assertEqual(self.keys(), [])
        self.assertIn("not a Claude Code screen", row["why"])

    def test_compact_a_node_pane_with_the_right_pid_is_fine(self):
        self.runs("node")
        self.assertEqual(self.go()["action"], "sent")

    def test_compact_focus_is_plain_words(self):
        self.queued("Fix $(touch x); `id` | tee > f & rm \"a\" 'b' <c> {d} [e] ~ ! * ? #x \\ now")
        self.snap()
        self.keep()
        typed = self.keys()[0]
        text = typed.split(" -l ", 1)[1]
        self.assertRegex(text, r"^/compact [\w .,:()/'+%=@-]+$")
        for ch in "$`;|&<>\"{}[]~!*?#\\":
            self.assertNotIn(ch, text)
        self.assertIn("Fix", text)
        self.assertLessEqual(len(text), 9 + 300)

    def test_compact_a_locked_state_is_a_row_not_a_crash(self):
        import fcntl
        self.queued()
        self.snap()
        folder = os.path.join(self.home, "state", "keeper")
        os.makedirs(folder, exist_ok=True)
        fd = os.open(os.path.join(folder, ".lock"), os.O_CREAT | os.O_RDWR)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        r = self.keep()
        self.assertEqual(self.keys(), [])
        self.assertEqual(r["sessions"], [{"session": "-", "pct": None, "action": "skipped", "why": "locked"}])


class KeeperDecide(unittest.TestCase):
    """The decision is one pure function: the harness adapters (Claude Code, `sampo code`, headless) feed it a percent and
    whether the session is at a task boundary, and act on its answer."""

    def test_decide_is_a_pure_function_of_the_reading(self):
        from sampo import keeper
        now = 100000.0
        self.assertEqual(keeper.decide(72, True, None, {"now": now}), (True, ""))
        self.assertEqual(keeper.decide(60, True, None, {"now": now})[0], False)
        self.assertIn("not over", keeper.decide(60, True, None, {"now": now})[1])
        self.assertIn("no context", keeper.decide(None, True, None, {"now": now})[1])
        self.assertEqual(keeper.decide(90, False, None, {"now": now}), (False, "mid-task"))
        self.assertIn("30 minutes", keeper.decide(90, True, now - 600, {"now": now})[1])
        self.assertEqual(keeper.decide(90, True, now - 3600, {"now": now})[0], True)
        same = keeper.decide(90, True, now - 3600, {"now": now, "snap": 5, "last_snap": 5})
        self.assertEqual(same[0], False)
        self.assertIn("same snapshot", same[1])
        self.assertEqual(keeper.decide(90, True, now - 3600, {"now": now, "snap": 6, "last_snap": 5})[0], True)


class KeeperCodeAdapter(unittest.TestCase):
    """`sampo code` reads its own conversation's size against its context room; its sink is the run's own context trim
    (Run.sight cuts old results past 70% of the room, trim() always for a priced profile): the keeper adds a journal row."""

    class Run:
        keeper_at = None

        def __init__(self):
            self.rows = []

        def log(self, **rec):
            self.rows.append(rec)

    def test_code_reader_measures_the_conversation_against_the_room(self):
        from sampo import keeper
        profile = {"name": "local", "context": 1000}  # room: 1000 less a quarter, 750 tokens, chars over 4
        self.assertEqual(keeper.read_code([{"content": "x" * 2400}], profile), 80)
        self.assertIsNone(keeper.read_code([{"content": "x" * 2400}], {"name": "api", "context": 1000, "price": [1, 2]}),
                          "a priced profile is trimmed on every send: no reading")
        self.assertIsNone(keeper.read_code([{"content": "x"}], {"name": "unknown"}), "no known context: no reading")

    def test_code_sink_notes_a_row_once_per_thirty_minutes(self):
        from sampo import keeper
        run, profile, msgs = self.Run(), {"name": "local", "context": 1000}, [{"content": "x" * 2400}]
        row = keeper.code_tick(run, "step-1", msgs, profile, now=1000.0)
        self.assertEqual((row["how"], row["pct"], row["action"]), ("code", 80, "trim"))
        self.assertEqual(run.rows[0]["kind"], "keeper")
        self.assertIsNone(keeper.code_tick(run, "step-1", msgs, profile, now=1600.0), "inside the 30 minutes")
        self.assertIsNotNone(keeper.code_tick(run, "step-1", msgs, profile, now=1000.0 + 1801))
        self.assertIsNone(keeper.code_tick(run, "step-1", [{"content": "x" * 100}], profile, now=9000.0), "under 60%")


class KeeperSpawn(KeeperRig, HookCase):
    """T-5769 review: the Stop hook fires while the pane still shows the spinner, so the keeper runs from a detached
    child a few seconds later, one per session at a time, and waits out a busy pane."""

    def test_stop_spawns_one_detached_delayed_child_per_session(self):
        import unittest.mock
        from sampo import keeper
        self.queued()
        self.snap()
        calls = []

        real = subprocess.Popen

        class Popen(real):  # only the detached keeper is recorded; tmux and ps still run (subprocess.run builds on Popen)
            def __init__(self, argv, **kw):
                if isinstance(argv, list) and "keeper" in argv:
                    calls.append((argv, kw))
                    argv, kw = ["true"], {}
                super().__init__(argv, **kw)

        env = {k: v for k, v in os.environ.items() if k != "FOREMAN_NO_BACKGROUND"}
        env.update(self.env)  # the fake tmux first on PATH
        with unittest.mock.patch.dict(os.environ, env, clear=True), unittest.mock.patch.object(keeper.subprocess, "Popen", Popen):
            self.assertIsNone(keeper.after_stop({"session_id": "lead-1", "cwd": self.repo}))
            self.assertEqual(len(calls), 1, calls)
            argv, kw = calls[0]
            self.assertEqual(argv[1:3], ["keeper", "--session"])
            self.assertEqual(argv[3], "lead-1")
            self.assertIn("--delay", argv)
            self.assertTrue(kw.get("start_new_session"))
            self.assertEqual(self.keys(), [], "the hook itself types nothing: the pane is still on its spinner")
            keeper.after_stop({"session_id": "lead-1", "cwd": self.repo})
            self.assertEqual(len(calls), 1, "single flight: the one before is still to run")
            for stamp in os.listdir(os.path.join(self.home, "state")):
                if stamp.startswith("keeper-run-"):
                    path = os.path.join(self.home, "state", stamp)
                    os.utime(path, (time.time() - 300, time.time() - 300))
            keeper.after_stop({"session_id": "lead-1", "cwd": self.repo})
            self.assertEqual(len(calls), 2, "a child that died leaves no lock for good")

    def test_the_child_waits_out_a_busy_pane_then_types(self):
        self.queued()
        self.snap()
        with open(self.busy_file, "w") as f:
            f.write("✻ Thinking… (3s · esc to interrupt)\n❯ \n")
        with open(self.busy_for, "w") as f:
            f.write("2")  # the first two reads show the spinner
        r = self.keep("--session", "lead-1", "--delay", "0", "--poll", "0.1")
        self.assertEqual([x["action"] for x in r["sessions"]], ["sent"], r)
        self.assertEqual(len(self.keys()), 2)

    def test_the_child_gives_up_on_a_pane_that_stays_busy(self):
        self.queued()
        self.snap()
        with open(self.busy_file, "w") as f:
            f.write("✻ Thinking… (3s · esc to interrupt)\n❯ \n")
        with open(self.busy_for, "w") as f:
            f.write("1000")
        r = self.keep("--session", "lead-1", "--delay", "0", "--poll", "0.1", "--tries", "3")
        self.assertEqual(r["sessions"][0]["why"], "busy")
        self.assertEqual(self.keys(), [])
        self.assertEqual(open(self.count_file).read().strip(), "3", "three looks, no more")


class KeeperPhase(CodeCase):
    """T-5794: Run.phase calls keeper.code_tick in a try that swallows any error, so a broken call left no trace; this one
    drives a whole `fm code` run on an unpriced profile with a small context and reads the keeper row off its journal."""

    def run_small(self, context, *script, price=None):
        self.serve("anthropic", plan(), *script, name="small")
        path = os.path.join(self.home, "models.json")
        with open(path) as fh:
            conf = json.load(fh)
        conf["profiles"]["small"]["context"] = context
        if price:
            conf["profiles"]["small"]["price"] = price
        with open(path, "w") as fh:
            json.dump(conf, fh)
        return self.code("fix add", "--model", "small", check=False)

    def keeper_rows(self):
        return [r for r in (json.loads(x) for x in journal_text(self).splitlines()) if r.get("kind") == "keeper"]

    def test_phase_drives_code_tick_and_its_keeper_row_is_in_the_journal(self):
        r = self.run_small(1200, call("edit", file="calc.py", old="a - b", new="a + b"), call("done", summary="fixed"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        rows = self.keeper_rows()
        self.assertEqual(len(rows), 1, "once per 30 minutes: one row for the run (a 1200-token context: the plan is over 60%)")
        self.assertEqual((rows[0]["how"], rows[0]["action"]), ("code", "trim"))
        self.assertGreaterEqual(rows[0]["pct"], 60)

    def test_phase_drives_code_tick_and_a_priced_profile_has_no_row(self):
        r = self.run_small(1200, call("edit", file="calc.py", old="a - b", new="a + b"), call("done", summary="fixed"),
                           price={"in": 3.0, "out": 15.0})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.keeper_rows(), [], "trimmed on every send: no room to fill, no row")


if __name__ == "__main__":
    unittest.main()
