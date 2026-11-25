"""fm serve / fm run with stub systemctl, journalctl and claude on PATH: no real services, sessions or network."""
import json
import os
import sys

from helpers import FM, ForemanTestCase, git_repo, read_text

from sampo import core as c
from sampo import serve as sserve


class ServeCase(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.uhome = os.path.join(self.tmp, "uhome")  # HOME: systemd unit dir and ~/.claude.json live here
        self.bin = os.path.join(self.tmp, "bin")
        self.calls = os.path.join(self.tmp, "calls.log")
        os.makedirs(self.uhome)
        os.makedirs(self.bin)
        self.stub("systemctl", 'if [ "$2" = is-active ]; then echo active; fi\n')
        self.stub("journalctl", 'echo "Error: Workspace not trusted."\necho "session https://claude.ai/code/x?t=1"\n')
        self.stub("claude", "")
        self.stub("loginctl", 'if [ "$1" = show-user ]; then echo yes; fi\n')
        self.trust(self.repo)
        self.fm("init")
        self.slug = c.find_project(self.repo).slug
        self.unit = os.path.join(self.uhome, ".config", "systemd", "user", f"foreman-serve-{self.slug}.service")

    def stub(self, name, body):
        path = os.path.join(self.bin, name)
        with open(path, "w") as f:
            f.write(f'#!/usr/bin/env bash\necho "{name} $*" >> {self.calls}\n' + body)
        os.chmod(path, 0o755)

    def trust(self, path):
        with open(os.path.join(self.uhome, ".claude.json"), "w") as f:
            json.dump({"projects": {path: {"hasTrustDialogAccepted": True}}}, f)

    def env(self, **extra):
        return dict({"HOME": self.uhome, "XDG_CONFIG_HOME": os.path.join(self.uhome, ".config"),
                     "PATH": self.bin + os.pathsep + os.environ["PATH"]}, **extra)

    def serve(self, *args, check=True):
        return self.fm("serve", *args, check=check, env=self.env())

    def called(self):
        return read_text(self.calls) if os.path.exists(self.calls) else ""

    def meta(self):
        return c.read_meta(c.find_project(self.repo))


class Serve(ServeCase):
    def test_start_writes_and_enables_a_unit_and_sets_full_auto_drive(self):
        self.fm("autonomy", "standard")
        self.fm("drive", "off")
        self.serve()
        unit = read_text(self.unit)
        for needle in ("Managed by Sampo", f"WorkingDirectory={self.repo}", "Type=simple", "Restart=always",
                       "StartLimitBurst=", "env claude remote-control", "--spawn same-dir", "StandardOutput=null"):
            self.assertIn(needle, unit)
        self.assertNotIn("--permission-mode", unit, "the user's own default mode applies unless one is given")
        self.assertIn(f"systemctl --user enable --now foreman-serve-{self.slug}.service", self.called())
        self.assertNotIn("claude ", self.called(), "serve never runs claude itself (remote-control --help blocks)")
        m = self.meta()
        self.assertEqual((m["autonomy"], m["drive"]), ("full", True))
        self.assertEqual((m["serve"]["prev_autonomy"], m["serve"]["prev_drive"]), ("standard", False))

    def test_linger_is_enabled_so_the_unit_outlives_the_login(self):
        # without linger, systemd --user (and the unit) stops when the SSH session that ran fm serve ends
        self.serve()
        self.assertNotIn("enable-linger", self.called())
        self.stub("loginctl", 'if [ "$1" = show-user ]; then echo no; fi\n')
        out = self.serve().stdout
        self.assertIn("loginctl enable-linger", self.called())
        self.assertIn("linger is off", out)

    def test_stop_reverses_everything(self):
        self.fm("autonomy", "standard")
        self.serve()
        self.serve("stop")
        self.assertFalse(os.path.exists(self.unit))
        self.assertIn(f"systemctl --user disable --now foreman-serve-{self.slug}.service", self.called())
        m = self.meta()
        self.assertEqual((m["autonomy"], m["drive"]), ("standard", True))
        self.assertNotIn("serve", m)

    def test_untrusted_workspace_is_refused_with_the_reason(self):
        self.trust(os.path.join(self.tmp, "elsewhere"))
        p = self.serve(check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("trust", p.stderr)
        self.assertFalse(os.path.exists(self.unit))
        self.trust(self.tmp)  # Claude Code checks the repo root itself: a trusted parent folder doesn't count
        self.assertNotEqual(self.serve(check=False).returncode, 0)
        self.trust(self.repo)
        self.serve(os.path.join(self.repo, "."))

    def test_permission_mode_given_or_sensitive(self):
        self.serve("--permission-mode", "acceptEdits")
        self.assertIn("--permission-mode acceptEdits", read_text(self.unit))
        self.serve("stop")
        self.fm("sensitive", "on")
        self.serve()
        self.assertIn("--permission-mode default", read_text(self.unit), "sensitive repos approve from the phone")
        self.assertNotEqual(self.serve("--permission-mode", "yolo", check=False).returncode, 0)

    def test_status_names_the_unit_state_and_the_last_error_when_down(self):
        self.serve()
        out = self.serve("status").stdout
        for needle in (self.slug, "active", self.repo, "idle"):
            self.assertIn(needle, out)
        self.assertNotIn("Workspace not trusted", out)
        self.stub("systemctl", 'if [ "$2" = is-active ]; then echo failed; fi\n')
        out = self.serve("status").stdout
        self.assertIn("Workspace not trusted", out)
        self.assertNotIn("https://", out, "the session URL is never shown")
        self.assertIn("still full autonomy with drive on", out, "a dead unit leaves the project in serve mode")

    def test_status_json_lists_units_for_the_desktop_app(self):  # T-0328
        self.assertEqual(json.loads(self.serve("status", "--json").stdout), {"v": 1, "units": []})
        self.serve()
        units = json.loads(self.serve("status", "--json").stdout)["units"]
        self.assertEqual([(u["project"], u["state"], u["root"], u["active"]) for u in units],
                         [(self.slug, "active", self.repo, None)])
        self.stub("systemctl", 'if [ "$2" = is-active ]; then echo failed; fi\n')
        u = json.loads(self.serve("status", "--json").stdout)["units"][0]
        self.assertEqual(u["state"], "failed")
        self.assertIn("Workspace not trusted", " ".join(u["log"]))
        self.assertNotIn("https://", " ".join(u["log"]))

    def test_control_characters_never_reach_the_unit_file(self):
        # a newline in the repo path or PATH would end its line and start a new unit directive
        bad = type("P", (), {"root": "/srv/app\nExecStartPre=/bin/evil", "slug": "app-1"})()
        with self.assertRaises(c.PolicyError):
            sserve.unit_text(bad, None)
        p = c.find_project(self.repo)
        old = os.environ["PATH"]
        os.environ["PATH"] = old + ":/x\nExecStartPre=/bin/evil"
        try:
            text = sserve.unit_text(p, None)
        finally:
            os.environ["PATH"] = old
        self.assertNotIn("\nExecStartPre", text)

    def test_units_of_another_foreman_install_are_left_alone(self):
        self.serve()
        other = self.unit.replace(self.slug, "elsewhere-123456")
        with open(self.unit) as f:
            text = f.read().replace(self.home, "/other/foreman")
        with open(other, "w") as f:
            f.write(text)
        self.fm("uninstall-user", env=self.env())
        self.assertFalse(os.path.exists(self.unit))
        self.assertTrue(os.path.exists(other))

    def test_refuses_while_fm_run_works_here(self):
        import fcntl
        with open(os.path.join(c.find_project(self.repo).dir, "run.lock"), "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            p = self.serve(check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("fm run", p.stderr)

    def test_uninstall_user_stops_serve_units(self):
        self.serve()
        self.fm("uninstall-user", env=self.env())
        self.assertFalse(os.path.exists(self.unit))
        self.assertIn("disable --now", self.called())


class RunCase(ServeCase):
    def setUp(self):
        super().setUp()
        # a stub session that finishes whatever task it is scoped to, the way a real one would through fm
        fm = f"{sys.executable} {FM}"
        self.finisher = (f't=$FOREMAN_DRIVE_TASK\necho "session for $t"\n{fm} focus $t >/dev/null\n'
                         f'{fm} task step $t done 1 --evidence x ok >/dev/null\n'
                         f'{fm} task ac $t check 1 --evidence x ok >/dev/null\n'
                         f'{fm} task audit $t self x ok >/dev/null\n'
                         f'{fm} task set $t --section "Regression test" --text "none: stub" >/dev/null\n{fm} task done $t\n')

    def task(self, title):
        return json.loads(self.fm("task", "new", title, "--type", "FIX", "--tier", "S", "--ac", "works",
                                  "--step", "fix it", "--json").stdout)["id"]

    def run_fm(self, *args, check=True):
        return self.fm("run", *args, check=check, env=self.env())


class Run(RunCase):
    def test_a_usd_contract_counts_each_sessions_cost_without_max_usd(self):  # T-1158
        for t in ("one", "two", "three"):
            self.task(t)
        self.stub("claude", self.finisher + 'echo \'{"type":"result","total_cost_usd":0.30,"result":"ok"}\'\n')
        self.fm("contract", "--usd", "0.5")
        out = self.run_fm().stdout
        self.assertEqual(len([l for l in self.called().splitlines() if l.startswith("claude ")]), 2)
        self.assertIn("contract ended (spend bound)", out)
        self.assertIn("contract receipt: 2 task(s) closed", out)

    def test_a_contract_that_is_over_stops_fm_run_starting_tasks(self):  # T-1158
        self.task("one")
        self.stub("claude", self.finisher + 'echo \'{"type":"result","total_cost_usd":0.30,"result":"ok"}\'\n')
        self.fm("contract", "--usd", "0")  # a spend bound already reached
        out = self.run_fm().stdout
        self.assertIn("contract ended (spend bound)", out)
        self.assertIn("contract receipt: 0 task(s) closed", out)
        self.assertEqual([l for l in self.called().splitlines() if l.startswith("claude ")], [])

    def test_max_usd_stops_starting_tasks_once_spend_reaches_it(self):  # T-1046
        for t in ("one", "two", "three"):
            self.task(t)
        self.stub("claude", self.finisher + 'echo \'{"type":"result","total_cost_usd":0.30,"result":"ok"}\'\n')
        out = self.run_fm("--max-usd", "0.5").stdout
        sessions = [l for l in self.called().splitlines() if l.startswith("claude ")]
        self.assertEqual(len(sessions), 2)  # 0.30 < 0.5 starts a second; 0.60 >= 0.5 stops the third
        self.assertIn("spend cap reached", out)

    def test_stall_usd_stops_a_run_that_spends_without_closing(self):  # T-2669
        for t in ("one", "two", "three"):
            self.task(t)
        fm = f"{sys.executable} {FM}"  # moves the brief each time (progress) but never closes the task
        self.stub("claude", f't=$FOREMAN_DRIVE_TASK\n{fm} focus $t >/dev/null\n{fm} task evidence $t --run "echo $RANDOM" >/dev/null\n'
                  'echo \'{"type":"result","total_cost_usd":0.30,"result":"ok"}\'\n')
        r = self.run_fm("--stall-usd", "0.5", "--max", "5", check=False)
        out = r.stdout + r.stderr
        self.assertEqual(len([l for l in self.called().splitlines() if l.startswith("claude ")]), 2)
        self.assertIn("--stall-usd", out, out)

    def test_stall_usd_resets_on_each_close(self):  # T-2669
        for t in ("one", "two", "three"):
            self.task(t)
        self.stub("claude", self.finisher + 'echo \'{"type":"result","total_cost_usd":0.30,"result":"ok"}\'\n')
        out = self.run_fm("--stall-usd", "0.5").stdout
        self.assertEqual(len([l for l in self.called().splitlines() if l.startswith("claude ")]), 3)
        self.assertNotIn("--stall-usd", out)

    def test_all_projects_works_each_projects_queue_in_turn(self):  # T-1782
        a = self.task("one")
        other = git_repo(self.tmp, "other")
        self.fm("init", cwd=other)
        b = json.loads(self.fm("task", "new", "two", "--type", "FIX", "--tier", "S", "--ac", "works", "--step",
                               "fix it", "--json", cwd=other).stdout)["id"]
        idle = git_repo(self.tmp, "idle")
        self.fm("init", cwd=idle)  # nothing queued: skipped
        self.stub("claude", self.finisher)
        out = self.run_fm("--all-projects").stdout
        p1, p2 = c.find_project(self.repo), c.find_project(other)
        self.assertEqual((c.find_brief(p1, a).status, c.find_brief(p2, b).status), ("done", "done"))
        self.assertIn(f"== {p1.slug}: 1 queued", out)
        self.assertIn(f"== {p2.slug}: 1 queued", out)
        self.assertNotIn(c.find_project(idle).slug, out)
        self.assertIn("2 project(s) worked, 0 stopped early", out)
        r = self.run_fm("--all-projects", "-p", p1.slug, check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--all-projects works every project: drop -p", r.stderr)

    def test_a_runner_works_each_task_through_fm_code(self):  # T-1773
        import types
        t = self.task("one")
        p = c.find_project(self.repo)
        argv = sserve._session_cmd(p, c.find_brief(p, t), types.SimpleNamespace(runner="codex"), {})
        self.assertEqual(argv[1:], [os.path.join(c.PLUGIN_ROOT, "bin", "fm"), "code", "--task", t, "--worker", "codex"])
        before = c.find_brief(p, t).status
        r = self.run_fm("--runner", "nosuch", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--runner nosuch: not a runner fm code can run", r.stderr)
        self.assertEqual(c.find_brief(p, t).status, before, "nothing ran")

    def test_each_task_runs_in_its_own_fresh_session(self):
        a, b = self.task("one"), self.task("two")
        self.stub("claude", self.finisher)
        out = self.run_fm().stdout
        p = c.find_project(self.repo)
        self.assertEqual([c.find_brief(p, t).status for t in (a, b)], ["done", "done"])
        sessions = [l for l in self.called().splitlines() if l.startswith("claude ")]
        self.assertEqual(len(sessions), 2)
        self.assertTrue(all(" -p " in s for s in sessions))
        self.assertIn(f"{a} done", out)
        self.assertIn(f"session for {a}", read_text(os.path.join(c.state_dir(), "logs", f"run-{self.slug}.log")))

    def test_a_timed_out_session_is_logged_with_its_errors(self):
        self.task("one")
        self.stub("claude", 'echo "stuck on the login" >&2\nsleep 5\n')
        p = self.run_fm("--timeout", "0.02", check=False)  # minutes
        self.assertEqual(p.returncode, 1)
        self.assertIn("limit", p.stderr)
        self.assertIn("stuck on the login", read_text(os.path.join(c.state_dir(), "logs", f"run-{self.slug}.log")))

    def test_a_session_without_progress_stops_the_run(self):
        self.task("one")
        p = self.run_fm(check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("no progress", p.stderr)
        self.assertEqual(len([l for l in self.called().splitlines() if l.startswith("claude ")]), 1)

    def limited_then(self, body):
        """A stub session that hits the usage limit on its first call, then runs body."""
        n = os.path.join(self.tmp, "sessions")
        return (f'echo x >> {n}\nif [ "$(wc -l < {n})" -eq 1 ]; then\n'
                f'echo "You\'ve hit your session limit · resets 3pm (Europe/Berlin)" >&2; exit 1\nfi\n' + body)

    def test_a_usage_limit_is_waited_out_and_the_task_retried(self):
        a = self.task("one")
        self.stub("claude", self.limited_then(self.finisher))
        out = self.run_fm("--wait", "0.0003").stdout  # hours: ~1 s budget
        self.assertEqual(c.find_brief(c.find_project(self.repo), a).status, "done")
        self.assertIn("usage limit", out)
        self.assertIn("usage limit", read_text(os.path.join(c.state_dir(), "logs", f"run-{self.slug}.log")))

    def test_a_usage_limit_past_the_wait_budget_stops_the_run(self):
        self.task("one")
        self.stub("claude", 'echo "You\'ve hit your weekly limit" >&2\nexit 1\n')
        p = self.run_fm("--wait", "0.0003", check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("usage limit", p.stderr)
        self.assertEqual(len([l for l in self.called().splitlines() if l.startswith("claude ")]), 2)

    def test_wait_zero_and_other_failures_stop_at_once(self):
        self.task("one")
        self.stub("claude", self.limited_then(self.finisher))
        p = self.run_fm("--wait", "0", check=False)
        self.assertEqual((p.returncode, "usage limit" in p.stderr), (1, True))
        # a crash whose earlier output merely mentions a limit is a crash, not a limit (only the last line counts)
        self.stub("claude", 'echo "You\'ve reached your quota, the API said"\necho "Error: boom" >&2\nexit 1\n')
        p = self.run_fm("--wait", "1", check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("claude exited 1", p.stderr)
        for bad in ("nan", "-1", "inf"):
            self.assertIn("--wait", self.run_fm("--wait", bad, check=False).stderr)

    def test_run_heartbeat_goes_stale_and_notifies(self):
        # T-0434: each loop beats into the state dir; a clean stop removes it, a killed run leaves it, and doctor's
        # dead-man check tells the notify command once per stale episode
        import time
        from sampo import doctor
        p = c.find_project(self.repo)
        beat, seen, told = (os.path.join(p.dir, "heartbeat-run.json"), os.path.join(self.tmp, "seen"),
                            os.path.join(self.tmp, "told"))
        self.task("one")
        self.stub("claude", f"cat {beat} > {seen}\n" + self.finisher)
        self.run_fm()
        self.assertEqual(json.loads(read_text(seen))["doing"], "T-0001")
        self.assertFalse(os.path.exists(beat))  # a clean stop takes it away
        self.task("two")
        self.stub("claude", "kill -9 $PPID\n")  # the run dies mid-session
        self.run_fm(check=False)
        self.assertTrue(os.path.exists(beat))
        self.fm("notify", f'echo "$1" >> {told}')
        self.assertEqual(doctor.check_heartbeats([p]).status, "PASS")  # not overdue yet
        later = time.time() + 86400
        r = doctor.check_heartbeats([p], now=later)
        self.assertEqual(r.status, "WARN")
        self.assertIn("fm run", r.detail)
        doctor.check_heartbeats([p], now=later)
        self.assertEqual(len(read_text(told).splitlines()), 1)  # once per stale episode
        self.assertIn("T-0002", read_text(told))

    def test_blocked_tasks_dont_stop_the_run(self):
        # T-0434 review: a session that blocks its task says why and the run moves on (the user wants no idling);
        # only a crash, a login problem or no progress stops it
        for t in ("one", "two", "three"):
            self.task(t)
        self.stub("claude", f'{sys.executable} {FM} task block $FOREMAN_DRIVE_TASK "stub: cannot" >/dev/null\n')
        self.run_fm()
        self.assertEqual(len([l for l in self.called().splitlines() if l.startswith("claude ")]), 3)

    def test_tasks_waiting_on_the_user_are_skipped(self):
        a, b = self.task("one"), self.task("two")
        self.fm_ask(a, "publish")
        self.stub("claude", self.finisher)
        out = self.run_fm().stdout
        self.assertIn(f"{a} waits on the user", out)
        self.assertEqual(c.find_brief(c.find_project(self.repo), b).status, "done")

    def test_refuses_while_serve_runs_here(self):
        self.task("one")
        self.fm("serve", env=self.env())
        # T-6399: beside a serving session a run works in lanes (test_parallel); a measured run needs the main checkout
        p = self.run_fm("--max-usd", "5", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("fm serve", p.stderr)

    def test_beside_serve_a_task_no_lane_can_take_is_skipped_not_spun_on(self):
        # T-6409: an unscoped head made the lane pool pick nothing, return, and be called again for the same head forever
        a = self.task("one")
        self.fm("serve", env=self.env())
        self.stub("claude", self.finisher)
        p = self.run_fm(check=False)
        self.assertIn(f"{a}: no lane can take it", p.stdout)
        self.assertNotIn("claude ", self.called())


class RunRoute(RunCase):
    """T-2353: fm run with no --models and none kept gives each session the per-tier route's model, and says so."""
    def setUp(self):
        super().setUp()
        self.fm("models", "default", "route")  # T-6101: these pin the history route, not the Haiku default

    def claudes(self):
        return [l for l in self.called().splitlines() if l.startswith("claude ")]

    def test_no_models_and_none_kept_routes_each_session_and_prints_it(self):
        a, b = self.task("one"), self.task("two")
        self.stub("claude", self.finisher)
        out = self.run_fm().stdout
        self.assertEqual(len(self.claudes()), 2)
        self.assertTrue(all("--model sonnet" in l for l in self.claudes()), self.claudes())
        for t in (a, b):
            self.assertRegex(out, rf"{t}: model sonnet: ")  # one line per session
        self.assertEqual(c.find_brief(c.find_project(self.repo), a).meta["builder_model"], "sonnet")  # feeds the route

    def test_models_still_wins_and_prints_no_route(self):
        self.task("one")
        self.stub("claude", self.finisher)
        out = self.run_fm("--models", "S=opus").stdout
        self.assertTrue(all("--model opus" in l for l in self.claudes()), self.claudes())
        self.assertNotIn(": model sonnet", out)
        self.assertNotIn(": model opus: ", out)

    def test_a_kept_choice_wins_over_the_route(self):
        self.task("one")
        self.stub("claude", self.finisher)
        self.run_fm("--models", "S=haiku", "--save", "--max", "0", check=False)
        self.task("two")
        self.run_fm()
        self.assertTrue(self.claudes() and all("--model haiku" in l for l in self.claudes()), self.claudes())

    def test_a_probe_session_is_marked_and_runs_one_model_down(self):
        from sampo import lanes
        p = c.find_project(self.repo)
        for i in range(5):  # five closed sonnet S lanes that held
            t = self.task(f"old {i}")
            b = c.find_brief(p, t)
            b.meta["builder_model"] = "sonnet"
            b.meta["status"] = "done"
            c.save_brief(p, b)
        tid = self.task("probe me")
        while not lanes.is_probe(tid):  # tasks pile up until one's id is a probe by the hash; fm run works them all
            tid = self.task("probe me")
        self.stub("claude", self.finisher)
        out = self.run_fm().stdout
        self.assertIn(f"{tid}: model haiku: ", out)
        self.assertEqual(c.find_brief(p, tid).meta.get("route_probe"), "1")


class Failover(ServeCase):
    def task(self, title):
        return json.loads(self.fm("task", "new", title, "--type", "FIX", "--tier", "S", "--ac", "works",
                                  "--step", "fix it", "--json").stdout)["id"]

    def test_a_usage_limit_past_the_wait_hands_the_task_to_the_named_agent(self):
        # T-0731: opt-in per run (the packet goes to another provider), never by default
        import time
        a = self.task("one")
        self.stub("claude", 'echo "You\'ve hit your weekly limit" >&2\nexit 1\n')
        self.stub("codex", "cat > /dev/null\n")
        p = self.fm("run", "--wait", "0", check=False, env=self.env())
        self.assertNotIn("codex", self.called())  # without --failover nothing leaves
        p = self.fm("run", "--wait", "0", "--failover", "codex", check=False, env=self.env())
        self.assertIn(f"handed {a} to codex", p.stdout + p.stderr)
        for _ in range(50):
            if "codex " in self.called():
                break
            time.sleep(0.1)
        self.assertIn("codex ", self.called())
        self.assertIn(f"Handoff: {a}", read_text(os.path.join(c.find_project(self.repo).dir, "handoffs", f"{a}.md")))


class BoardRun(RunCase):
    """T-0955: a no-progress task is quarantined across runs, fm run holds a sleep lock, fm night prints a timer."""

    def test_a_no_progress_task_is_blocked_and_the_next_run_takes_the_next(self):
        a, b = self.task("one"), self.task("two")
        p = self.run_fm(check=False)  # the stub session does nothing
        self.assertEqual((p.returncode, "no progress" in p.stderr), (1, True))
        proj = c.find_project(self.repo)
        stuck, other = (a, b) if c.find_brief(proj, a).status == "blocked" else (b, a)
        self.assertEqual(c.find_brief(proj, stuck).status, "blocked")
        self.assertIn("no progress", c.find_brief(proj, stuck).section("Log"))
        self.assertNotEqual(c.find_brief(proj, other).status, "blocked")
        self.stub("claude", self.finisher)
        self.run_fm()
        self.assertEqual([c.find_brief(proj, t).status for t in (stuck, other)], ["blocked", "done"])

    def test_run_re_execs_once_under_systemd_inhibit(self):
        # a pass-through stub: --list answers, anything else drops its options and runs the command
        self.stub("systemd-inhibit", '[ "$1" = --list ] && exit 0\nwhile [ "${1#--}" != "$1" ]; do shift; done\n'
                                     'exec "$@"\n')
        p = self.fm("run", env=self.env(FOREMAN_INHIBITED=""))
        self.assertIn("nothing else runnable", p.stdout)
        held = [l for l in self.called().splitlines() if l.startswith("systemd-inhibit --what=sleep:idle")]
        self.assertEqual(len(held), 1, self.called())
        self.assertIn(" run", held[0])
        open(self.calls, "w").close()
        self.fm("run", env=self.env(FOREMAN_INHIBITED="1"))
        self.assertNotIn("--what", self.called(), "the guard stops a second re-exec")


class NightInstall(ServeCase):  # T-2357: night and tick timers staged, asked once, installed only after the user's yes
    def tree(self):
        return sorted(os.path.join(d, f) for d, _, fs in os.walk(self.uhome) for f in fs)

    def stage(self, *extra):
        self.fm("task", "new", "timers", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true", "--step", "s",
                "--focus", env=self.env())
        return self.fm("night", "--stage", *extra, env=self.env())

    def test_night_stage_writes_only_state_and_queues_one_plugin_grant_naming_each_line(self):
        before = self.tree()
        out = self.stage("--watch-issues", "octo/app", "--watch-ci").stdout
        self.assertEqual(self.tree(), before, "staging writes nothing outside state")
        self.assertEqual(self.called(), "")
        staged = os.path.join(c.find_project(self.repo).dir, "night", "staged")
        names = sorted(os.listdir(staged))
        for n in (f"foreman-night-{self.slug}.service", f"foreman-night-{self.slug}.timer",
                  f"foreman-tick-{self.slug}.service", f"foreman-tick-{self.slug}.timer", "crontab.txt", "plan.json"):
            self.assertIn(n, names)
        timer = read_text(os.path.join(staged, f"foreman-night-{self.slug}.timer"))
        self.assertIn("Persistent=true", timer)
        self.assertRegex(read_text(os.path.join(staged, f"foreman-tick-{self.slug}.service")), r"(?m)^ExecStart=.* night --tick$")
        for word in ("nightly", "hourly", "$", "fm night --uninstall", "octo/app", "CI"):
            self.assertIn(word, out)
        todo = self.fm("todo", "--json", env=self.env()).stdout
        self.assertEqual(todo.count('"kind": "grant"'), 1)
        self.assertIn("plugin", todo)
        self.assertIn("octo/app", todo)
        self.assertIn("fm night --uninstall", todo)
        self.fm("night", "--stage", "--watch-issues", "octo/app", "--watch-ci", env=self.env())  # the same plan: one ask
        self.assertEqual(self.fm("todo", "--json", env=self.env()).stdout.count('"kind": "grant"'), 1)

    def test_night_install_after_the_yes_enables_the_staged_timers_and_the_staged_watch_lines_only(self):
        self.stage("--watch-ci")
        r = self.fm("night", "--install", env=self.env())  # the user's own terminal: the guard isn't in the way
        udir = os.path.join(self.uhome, ".config", "systemd", "user")
        for n in (f"foreman-night-{self.slug}.timer", f"foreman-tick-{self.slug}.timer"):
            self.assertTrue(os.path.isfile(os.path.join(udir, n)), r.stdout)
            self.assertIn(f"systemctl --user enable --now {n}", self.called())
        self.assertIn(f"systemctl --user start foreman-tick-{self.slug}.service", self.called())  # T-2542
        self.assertIn("First tick", r.stdout, "the yes is followed by a run that is seen")
        meta = self.meta()
        self.assertEqual(meta.get("watch"), {"ci": True, "issues": []})  # issues weren't staged, so not watched
        self.assertFalse(os.path.exists(os.path.join(c.find_project(self.repo).dir, "night", "staged")))
        self.fm("night", "--uninstall", env=self.env())
        self.assertFalse(any(n.startswith(("foreman-night-", "foreman-tick-")) for n in os.listdir(udir)))
        self.assertIn(f"systemctl --user disable --now foreman-tick-{self.slug}.timer", self.called())
        self.assertNotIn("watch", self.meta())

    def test_night_install_with_nothing_staged_stages_first_and_the_print_timer_flag_is_gone(self):
        r = self.fm("night", "--print-timer", env=self.env(), check=False)
        self.assertNotEqual(r.returncode, 0)
        self.fm("night", "--install", env=self.env())
        self.assertIn(f"enable --now foreman-night-{self.slug}.timer", self.called())
        self.assertEqual(self.meta().get("watch"), {"ci": False, "issues": []})

    def test_night_doctor_names_staged_timers_waiting_and_units_of_another_home(self):
        self.stage()
        out = self.fm("doctor", env=self.env(), check=False).stdout
        self.assertIn("night timers", out)
        self.assertIn("waiting for your yes", out)
        udir = os.path.join(self.uhome, ".config", "systemd", "user")
        os.makedirs(udir, exist_ok=True)
        with open(os.path.join(udir, "foreman-night-old.service"), "w") as f:
            f.write('[Service]\nEnvironment="FOREMAN_HOME=/elsewhere/foreman"\nExecStart=/x/fm night\n')
        out = self.fm("doctor", env=self.env(), check=False).stdout
        self.assertIn("foreman-night-old.service", out)
        self.assertIn("/elsewhere/foreman", out)
