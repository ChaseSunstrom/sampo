"""fm tidy: seeded rot is found in a dry run; --apply archives without losing anything (§12 scenario 9)."""
import json
import os
import re
import shutil
import time

from helpers import ForemanTestCase, read_text

from sampo import core as c


def age(p, tid, days, status=None):
    """Backdate a brief (and optionally set its status) directly — test setup only."""
    b = c.find_brief(p, tid)
    old = c.datetime.datetime.now(c.datetime.timezone.utc) - c.datetime.timedelta(days=days)
    b.meta["updated"] = b.meta["created"] = old.strftime("%Y-%m-%dT%H:%M:%SZ")
    if status:
        b.meta["status"] = status
    c.save_brief(p, b, touch=False)


class TidyCase(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fake_home = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(self.fake_home, ".claude"))
        self.fm("init")
        self.p = c.find_project(self.repo)
        encoded = re.sub(r"[^A-Za-z0-9]", "-", self.repo)
        self.mem = os.path.join(self.fake_home, ".claude", "projects", encoded, "memory")
        os.makedirs(self.mem)

    def tidy(self, *args):
        return json.loads(self.fm("tidy", "--json", *args, env={"HOME": self.fake_home}).stdout)

    def kinds(self, report):
        return {f["kind"] for f in report["findings"]}

    def seed_rot(self):
        with open(os.path.join(self.repo, "CLAUDE.md"), "w") as f:
            f.write("# app\n## Map\n- `src/api/` — handlers\n- `docs/missing-guide.md` — guide\n"
                    "## Commands\n- Test: `npm run test:unit`\n- Plan with `/sampo:intake`, graph with `/graphify`\n")
        with open(os.path.join(self.repo, "package.json"), "w") as f:
            json.dump({"scripts": {"test": "jest"}}, f)
        os.makedirs(os.path.join(self.repo, "src", "api"))
        with open(os.path.join(self.mem, "MEMORY.md"), "w") as f:
            f.write("- Use pnpm, not npm\n- Build with `make all`\n- Use pnpm, not npm\n- Config lives in `config/old.yaml`\n")
        self.fm("capture", "old idea nobody touched")
        age(self.p, "T-0001", 40)
        self.fm("task", "new", "a", "--type", "FIX", "--tier", "S", "--depends", "T-0003")
        self.fm("task", "new", "b", "--type", "FIX", "--tier", "S", "--depends", "T-0002")
        self.fm("task", "new", "old finished work", "--type", "CLEAN", "--tier", "S")
        age(self.p, "T-0004", 20, status="done")


class DryRun(TidyCase):
    def test_finds_all_seeded_rot_and_changes_nothing(self):
        self.seed_rot()
        before = sorted(os.listdir(os.path.join(self.p.dir, "tasks")))
        mem_before = read_text(os.path.join(self.mem, "MEMORY.md"))
        report = self.tidy()
        kinds = self.kinds(report)
        for k in ("dead_path", "stale_command", "memory_duplicate", "memory_dead_path", "inbox_stale", "cycle", "archive_task"):
            self.assertIn(k, kinds)
        dead = [f for f in report["findings"] if f["kind"] == "dead_path"]
        self.assertTrue(any("docs/missing-guide.md" in f["detail"] for f in dead))
        self.assertFalse(any("src/api" in f["detail"] for f in dead), "existing paths are not dead")
        self.assertFalse(any("/sampo:intake" in f["detail"] or "/graphify" in f["detail"] for f in dead),
                         "slash commands are not paths")
        self.assertEqual(sorted(os.listdir(os.path.join(self.p.dir, "tasks"))), before)
        self.assertEqual(read_text(os.path.join(self.mem, "MEMORY.md")), mem_before)
        self.assertFalse(report["applied"])

    def test_reports_doc_drift_in_project_docs(self):
        os.makedirs(os.path.join(self.repo, "src"))
        open(os.path.join(self.repo, "src", "main.py"), "w").close()  # src/ is repo content
        with open(os.path.join(self.repo, "README.md"), "w") as f:
            f.write("Entry point: `src/gone.py`\n")
        drift = [f for f in self.tidy()["findings"] if f["kind"] == "docs_drift"]
        self.assertEqual(len(drift), 1)
        self.assertIn("src/gone.py", drift[0]["detail"])

    def test_clean_project_has_no_findings_that_need_action(self):
        report = self.tidy()
        self.assertFalse([f for f in report["findings"] if f["severity"] != "info"])


class Apply(TidyCase):
    def test_old_reverted_copies_are_aged_out(self):  # T-1442
        rev = os.path.join(self.p.dir, "code", "reverted")
        os.makedirs(rev)
        outside = os.path.join(self.tmp, "outside")
        open(outside, "w").close()
        old, new, link = (os.path.join(rev, n) for n in ("old", "new", "link"))
        for f in (old, new):
            open(f, "w").close()
        os.symlink(outside, link)
        for f in (old, outside):
            os.utime(f, (time.time() - 31 * 86400,) * 2)
        os.utime(link, (time.time() - 31 * 86400,) * 2, follow_symlinks=False)
        self.assertIn("old_reverted", self.kinds(self.tidy()))
        self.assertTrue(os.path.exists(old), "a dry run removes nothing")
        self.tidy("--apply")
        self.assertEqual(sorted(os.listdir(rev)), ["link", "new"])
        self.assertTrue(os.path.exists(outside))

    def test_door_snapshots_of_long_closed_tasks_are_aged_out(self):  # T-2342
        for t in ("old closed", "recent closed", "still open"):
            self.fm("task", "new", t, "--type", "FIX", "--tier", "S")
        age(self.p, "T-0001", 15, status="done")
        age(self.p, "T-0002", 3, status="done")
        snaps = os.path.join(self.p.dir, "door-snapshots")
        for tid in ("T-0001", "T-0002", "T-0003", "T-9999"):
            for n in (f"{tid}-step1", f"{tid}-verify1"):
                os.makedirs(os.path.join(snaps, n))
                open(os.path.join(snaps, n + ".json"), "w").close()
        self.assertIn("old_door_snapshots", self.kinds(self.tidy()))
        self.assertEqual(len(os.listdir(snaps)), 16, "a dry run removes nothing")
        self.tidy("--apply")
        self.assertEqual(sorted(n for n in os.listdir(snaps) if not n.endswith(".json")),
                         ["T-0002-step1", "T-0002-verify1", "T-0003-step1", "T-0003-verify1", "T-9999-step1", "T-9999-verify1"])
        self.assertFalse(os.path.exists(os.path.join(snaps, "T-0001-step1.json")))

    def test_tidy_outside_copies_of_a_long_closed_task_go_with_its_other_snapshots(self):  # T-4377
        for t in ("old closed", "still open"):
            self.fm("task", "new", t, "--type", "FIX", "--tier", "S")
        age(self.p, "T-0001", 15, status="done")
        snaps = os.path.join(self.p.dir, "door-snapshots")
        for tid in ("T-0001", "T-0002"):
            os.makedirs(os.path.join(snaps, f"{tid}-verify1.out", "tmp"))
            with open(os.path.join(snaps, f"{tid}-verify1.out", "tmp", "app_test"), "w") as f:
                f.write("x")
        os.makedirs(os.path.join(snaps, "T-0001-verify1.outer"))  # not a name the gate makes: left alone
        self.assertIn("old_door_snapshots", self.kinds(self.tidy()))
        self.tidy("--apply")
        self.assertEqual(sorted(os.listdir(snaps)), ["T-0001-verify1.outer", "T-0002-verify1.out"])

    def test_apply_archives_and_loses_nothing(self):
        self.seed_rot()
        done_text = read_text(c.find_brief(self.p, "T-0004").path)
        report = self.tidy("--apply")
        self.assertTrue(report["applied"])
        self.assertIsNone(c.find_brief(self.p, "T-0004"), "archived out of tasks/")
        archived = [os.path.join(d, f) for d, _, fs in os.walk(os.path.join(self.p.dir, "archive")) for f in fs
                    if f.startswith("T-0004")]
        self.assertEqual(len(archived), 1)
        self.assertEqual(read_text(archived[0]), done_text)
        self.assertIsNotNone(c.find_brief(self.p, "T-0001"), "user requests are never auto-deleted")
        self.assertEqual(read_text(os.path.join(self.mem, "MEMORY.md")).count("Use pnpm, not npm"), 1)
        backups = [f for f in os.listdir(os.path.join(self.p.dir, "archive")) if f.startswith("memory-")]
        self.assertTrue(backups, "memory archived before dedupe")
        self.assertIsNotNone(c.read_meta(self.p)["last_tidy"])
        self.assertIn("tidy", [e["event"] for e in c.ledger_tail(self.p)])
        again = self.kinds(self.tidy())
        self.assertNotIn("archive_task", again)
        self.assertNotIn("memory_duplicate", again)
        self.assertIn("cycle", again, "judgment calls stay reported")

    def test_rotates_large_event_log(self):
        events = os.path.join(self.home, "state", "events.jsonl")
        with open(events, "w") as f:
            f.write((json.dumps({"ts": c.now(), "kind": "tool", "target": "x" * 200}) + "\n") * 30000)
        self.assertIn("rotate_events", self.kinds(self.tidy()))
        self.tidy("--apply")
        self.assertLess(os.path.getsize(events), 1024)
        rotated = [f for f in os.listdir(os.path.join(self.home, "state", "logs")) if f.startswith("events-")]
        self.assertEqual(len(rotated), 1)

    def test_prunes_old_full_outputs_and_snapshots_but_not_the_pid_map(self):
        # T-0811: compact output's full texts (T-0783) piled up; sessions/by-pid/ (T-0777) is a folder, not a snapshot
        state = os.path.join(self.home, "state")
        old, new = os.path.join(state, "outputs", "old-sess"), os.path.join(state, "outputs", "new-sess")
        by_pid, snap = os.path.join(state, "sessions", "by-pid"), os.path.join(state, "sessions", "old.json")
        for d in (old, new, by_pid):
            os.makedirs(d)
            open(os.path.join(d, "x.txt"), "w").close()
        open(snap, "w").close()
        os.makedirs(os.path.join(state, "bus"), exist_ok=True)
        beat, mail = os.path.join(state, "bus", "watch-dead"), os.path.join(state, "bus", "mail.jsonl")
        seen = os.path.join(state, "bus", "seen-dead")
        for x in (beat, mail, seen):  # T-0819: a crashed session's watcher heartbeat and read mark
            open(x, "w").close()
        week_ago = time.time() - 8 * 86400
        for x in (old, by_pid, snap, beat, mail, seen):
            os.utime(x, (week_ago, week_ago))
        self.assertTrue({"old_outputs", "old_heartbeats"} <= self.kinds(self.tidy()))
        self.tidy("--apply")
        self.assertEqual(sorted(os.listdir(os.path.join(state, "outputs"))), ["new-sess"])
        self.assertEqual(os.listdir(os.path.join(state, "sessions")), ["by-pid"])
        self.assertEqual(sorted(os.listdir(os.path.join(state, "bus"))), ["mail.jsonl", "seen-dead"],
                         "only heartbeats go: a read mark keeps a resumed session from re-reading old mail (T-0839)")

    def test_prunes_stack_dumps_older_than_two_weeks(self):
        # T-1405: a hung long verb's stack dump is evidence for a while, not forever
        stacks = os.path.join(self.home, "state", "logs", "stacks")
        os.makedirs(stacks)
        old, recent = os.path.join(stacks, "check-1.txt"), os.path.join(stacks, "code-2.txt")
        for x, days in ((old, 15), (recent, 10)):
            with open(x, "w") as f:
                f.write("Thread 0x1\n")
            os.utime(x, (time.time() - days * 86400,) * 2)
        self.assertIn("old_stacks", self.kinds(self.tidy()))
        self.tidy("--apply")
        self.assertEqual(os.listdir(stacks), ["code-2.txt"])

    def test_sweeps_the_empty_project_shells_foremans_children_left(self):
        # T-0856: 521 empty ~/.claude/projects folders, one per child run in a fresh temp dir
        projects = os.path.join(self.fake_home, ".claude", "projects")
        shell = os.path.join(projects, "-run-user-1000-fm-relate-ab12cd34", "memory")
        kept = os.path.join(projects, "-run-user-1000-fm-second-session-x1y2z3w4")
        mine = os.path.join(projects, "-home-me-app", "memory")
        for d in (shell, kept, mine):
            os.makedirs(d)
        open(os.path.join(kept, "a.jsonl"), "w").close()  # a transcript: not swept
        self.assertIn("child_shells", self.kinds(self.tidy()))
        self.tidy("--apply")
        self.assertFalse(os.path.exists(os.path.dirname(shell)))
        self.assertTrue(os.path.exists(kept))
        self.assertTrue(os.path.exists(mine), "the user's own project folders are never touched")

    def test_archives_a_temp_projects_state_once_its_folder_is_gone(self):
        # T-2091: throwaway trials under /tmp left 11 state dirs behind; a root elsewhere may only be unplugged
        temp = os.path.join(self.tmp, "trial")
        os.makedirs(temp)
        self.fm("init", cwd=temp)
        dead = c.find_project(temp, create=False).slug
        shutil.rmtree(temp)
        away = os.path.join(c.projects_dir(), "away-1a2b3c")
        os.makedirs(away)
        with open(os.path.join(away, "meta.json"), "w") as f:
            json.dump({"path": "/nonexistent-fm-disk/app"}, f)
        found = {f["detail"].split()[0]: f for f in self.tidy()["findings"] if f["kind"] == "dead_project"}
        self.assertEqual((found[dead]["severity"], found["away-1a2b3c"]["severity"]), ("action", "warn"))
        self.assertNotIn(self.p.slug, found)
        self.tidy("--apply")
        self.assertFalse(os.path.exists(os.path.join(c.projects_dir(), dead)))
        self.assertTrue(os.path.isfile(os.path.join(c.state_dir(), "archive", "projects", dead, "meta.json")))
        self.assertTrue(os.path.isdir(away), "a root outside temp is only reported")
        self.assertTrue(os.path.isdir(self.p.dir))

    def test_rolls_large_ledger_by_month(self):
        ledger = os.path.join(self.p.dir, "ledger.jsonl")
        old = {"ts": "2026-01-15T00:00:00Z", "session_id": None, "project": self.p.slug, "task": None,
               "event": "note", "data": {"pad": "y" * 300}}
        live = read_text(ledger)
        with open(ledger, "w") as f:  # past months before this month's lines, as a ledger lands (T-1099)
            f.write((json.dumps(old) + "\n") * 4000 + live)
        self.assertIn("roll_ledger", self.kinds(self.tidy()))
        self.tidy("--apply")
        monthly = read_text(os.path.join(self.p.dir, "archive", "ledger-2026-01.jsonl"))
        self.assertEqual(len(monthly.splitlines()), 4000)
        kept = [json.loads(l) for l in read_text(ledger).splitlines()]
        self.assertTrue(all(not e["ts"].startswith("2026-01") for e in kept if e["event"] != "ledger_rolled"))
        self.assertIn("ledger_rolled", [e["event"] for e in kept])

    def test_a_roll_keeps_a_nul_line_byte_for_byte_and_ends_an_unterminated_one(self):  # T-1457
        ledger = os.path.join(self.p.dir, "ledger.jsonl")
        old = json.dumps({"ts": "2026-01-15T00:00:00Z", "session_id": None, "project": self.p.slug, "task": None,
                          "event": "note", "data": {"pad": "y" * 300}}).encode()
        torn = b"\x00" * 40 + b'{"ts": "2026-01-15T00:00:01Z", "event": "no' + b"\n"  # a crash's NULs, then half a line
        with open(ledger, "wb") as f:
            f.write((old + b"\n") * 2000 + torn + (old + b"\n") * 1999 + old)  # all past months; the last unended
        self.tidy("--apply")
        with open(os.path.join(self.p.dir, "archive", "ledger-2026-01.jsonl"), "rb") as f:
            got = f.read()
        self.assertIn(torn, got, "the NUL line, byte for byte")
        self.assertTrue(got.endswith(old + b"\n"), "the unended last line is ended in the archive")
        self.assertEqual(got.count(b"\n"), 4001)
        self.assertEqual(self.fm("verify-ledger", check=False).returncode, 0)

    def test_all_covers_every_registered_project(self):
        other = os.path.join(self.tmp, "other")
        os.makedirs(other)
        self.fm("init", other)
        self.assertEqual(len(self.tidy("--all")["projects"]), 2)


class StalePlans(TidyCase):
    # T-1077: a queued brief whose scope file changed (or went, or whose dependency was dropped) since it was planned
    def plan(self, title, *args):
        out = self.fm("task", "new", title, "--type", "FIX", "--tier", "S", *args).stdout
        tid = re.search(r"T-\d{4}", out).group(0)
        age(self.p, tid, 1)  # planned a day ago: what changes from now on came after it
        return tid

    def write(self, rel, epoch=None, root=None):
        path = os.path.join(root or self.repo, rel)
        with open(path, "a") as f:
            f.write("x\n")
        if epoch:
            os.utime(path, (epoch, epoch))

    def commit(self, *paths, days=0):
        import subprocess
        t = f"@{int(time.time() - days * 86400)} +0000"
        env = dict(os.environ, GIT_AUTHOR_DATE=t, GIT_COMMITTER_DATE=t)
        subprocess.run(["git", "-C", self.repo, "add", "-A", "--", *paths], check=True, env=env)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", "x"], check=True, env=env)

    def stale(self, cwd=None):
        out = self.fm("tidy", "--json", cwd=cwd, env={"HOME": self.fake_home}).stdout
        return [f["detail"] for f in json.loads(out)["findings"] if f["kind"] == "stale_plan"]

    def test_a_changelog_in_scope_moving_is_no_replan(self):
        # T-1890: every finished task appends to CHANGELOG.md, so each queued plan scoped to it looked stale
        tid = self.plan("change a", "--scope", "CHANGELOG.md", "--scope", "a.py")
        self.write("CHANGELOG.md", time.time() - 60)
        self.assertEqual(self.stale(), [], "only the changelog moved")
        self.write("a.py", time.time() - 60)
        self.assertTrue(any(tid in d and "a.py changed since it was planned" in d for d in self.stale()), self.stale())

    def test_an_untracked_scope_file_newer_than_the_plan_flags_a_replan_until_a_log_line_says_it_was_reread(self):
        tid = self.plan("change a", "--scope", "a.py")
        self.assertEqual(c.find_brief(self.p, tid).status, "planned")
        self.write("a.py", time.time() - 2 * 86400)
        self.assertEqual(self.stale(), [], "a scope file older than the plan is fine")
        self.write("a.py", time.time() - 60)
        self.assertTrue(any(tid in d and "a.py changed since it was planned" in d for d in self.stale()), self.stale())
        text = self.fm("tidy", env={"HOME": self.fake_home}).stdout
        self.assertIn("stale_plan", text)
        self.assertIn(f'fm task log {tid} "plan re-read: ', text, "the fix is a command someone can run")
        self.assertIn(tid, json.loads(self.fm("digest", "--json").stdout)["stale_plans"])
        self.assertIn("Plans to re-plan", self.fm("digest").stdout)
        self.fm("task", "log", tid, "plan re-read: still valid")
        self.assertEqual(self.stale(), [], "the log line bumps updated, so the item clears")
        age(self.p, tid, 1, status="done")
        self.assertEqual(self.stale(), [], "only queued plans")
        self.assertNotIn(tid, json.loads(self.fm("digest", "--json").stdout)["stale_plans"])

    def test_a_tracked_file_counts_by_its_commit_not_its_checkout_mtime_and_a_lane_sees_the_main_checkout(self):
        import subprocess
        self.write("b.py")
        self.commit("b.py", days=2)
        tid = self.plan("change b", "--scope", "b.py")
        os.utime(os.path.join(self.repo, "b.py"))  # what a fresh clone or a checkout leaves: mtime now
        self.assertEqual(self.stale(), [], "a clean file committed before the plan isn't stale, whatever its mtime")
        lane = os.path.join(self.tmp, "lane")
        subprocess.run(["git", "-C", self.repo, "worktree", "add", "-q", "-b", "lane", lane], check=True)
        self.write("b.py", root=lane)  # the lane's own uncommitted edit, its files all with checkout mtimes
        self.assertEqual(self.stale(cwd=lane), [], "run from a lane, the main checkout is what the plan is against")
        self.write("b.py")
        self.commit("b.py")
        for cwd in (None, lane):
            self.assertTrue(any(tid in d and "b.py changed since it was planned" in d for d in self.stale(cwd)), cwd)

    def test_a_scope_file_gone_or_a_dependency_dropped_flags_it_but_a_file_still_to_create_doesnt(self):
        self.write("b.py")
        self.write("d.py")
        self.commit("b.py", "d.py", days=2)
        gone = self.plan("change b", "--scope", "b.py")
        deleted = self.plan("change d", "--scope", "d.py")
        new = self.plan("create c", "--scope", "c.py", "--scope", "src/**/*.py")
        dep = self.plan("dependency")
        after = self.plan("after it", "--depends", dep)
        self.assertEqual(self.stale(), [])
        os.remove(os.path.join(self.repo, "b.py"))  # tracked in HEAD, absent
        os.remove(os.path.join(self.repo, "d.py"))
        self.commit("d.py")  # deleted in a commit after the plan
        self.fm("task", "drop", dep, "not needed")
        found = self.stale()
        self.assertTrue(any(gone in d and "b.py is gone" in d for d in found), found)
        self.assertTrue(any(deleted in d and "d.py is gone" in d for d in found), found)
        self.assertTrue(any(after in d and f"{dep} was dropped" in d for d in found), found)
        self.assertFalse(any(new in d for d in found), "a scope file the plan creates isn't missing")


class DeferredTriage(TidyCase):
    # T-1048: each deferred brief gets drop / do / ask proposed; nothing is applied
    def defer(self, title, days, revisit=None, scope=None):
        out = self.fm("task", "new", title, "--type", "FIX", "--tier", "S", *(["--scope", scope] if scope else [])).stdout
        tid = re.search(r"T-\d{4}", out).group(0)
        age(self.p, tid, days, status="deferred")
        if revisit:
            b = c.find_brief(self.p, tid)
            b.meta["revisit"] = revisit
            c.save_brief(self.p, b, touch=False)
        return tid

    def verdicts(self):
        return {t: v for t, v, _ in json.loads(self.fm("digest", "--json").stdout)["deferred_triage"]}

    def touch(self, rel, commit=False):
        import subprocess
        with open(os.path.join(self.repo, rel), "w") as f:
            f.write("x\n")
        if commit:
            subprocess.run(["git", "-C", self.repo, "add", rel], check=True)
            subprocess.run(["git", "-C", self.repo, "commit", "-qm", rel], check=True)

    def test_edge_cases(self):
        self.touch("live.py")
        self.touch("src_a.py")
        new = self.defer("creates a file", 120, scope="plugin/lib/fmnew.py")  # never existed: the plan creates it
        noscope = self.defer("no scope", 120)
        future = self.defer("future", 120, revisit="2999-01-01", scope="nothere.py")
        glob = self.defer("glob", 120, scope="src_*.py")  # matches something: live
        deadglob = self.defer("dead glob", 120, scope="zzz_*.py")  # matches nothing, never did
        edge89 = self.defer("89", 89)
        edge91 = self.defer("91", 91)
        self.touch("w.py", commit=True)
        os.remove(os.path.join(self.repo, "w.py"))
        gone = self.defer("was tracked", 120, scope="w.py")
        changed = self.defer("file changed", 5, revisit="when live.py changes @" + "0" * 8)
        same = self.defer("file same", 5, revisit="when live.py changes @" + c.file_digest(os.path.join(self.repo, "live.py")))
        self.assertEqual(self.verdicts(), {new: "ask", noscope: "drop", future: "ask", glob: "ask", deadglob: "ask",
                                           edge89: "ask", edge91: "drop", gone: "drop", changed: "do", same: "ask"})
        text = self.fm("tidy", env={"HOME": self.fake_home}).stdout
        self.assertEqual(text.count("propose ask"), 0, "asks collapse into one line")
        self.assertRegex(text, r"\d+ deferred briefs to decide: T-")
        d = self.fm("digest").stdout
        self.assertLess(d.index(f"Deferred: {changed} → do"), d.index("→ drop"), "do first, then drop, then ask")
        self.assertIn("more: fm tidy", d)

    def test_old_with_a_dead_scope_is_drop_a_due_one_is_do_and_the_rest_ask_and_nothing_changes(self):
        with open(os.path.join(self.repo, "live.py"), "w") as f:
            f.write("x\n")
        self.touch("gone.py", commit=True)
        os.remove(os.path.join(self.repo, "gone.py"))
        dead = self.defer("dead", 120, scope="gone.py")
        due = self.defer("due", 10, revisit="2020-01-01")
        rest = self.defer("rest", 50, scope="live.py")
        young = self.defer("young", 1, scope="live.py")  # nobody touched live.py, but nothing says it is ready
        got = {t: v for t, v, _ in json.loads(self.fm("digest", "--json").stdout)["deferred_triage"]}
        self.assertEqual(got, {dead: "drop", due: "do", rest: "ask", young: "ask"})
        text = self.fm("tidy", env={"HOME": self.fake_home}).stdout
        self.assertIn("deferred_triage", text)
        self.assertIn(f'fm task drop {dead} "', text)
        self.assertIn(f"Deferred: {dead} → drop", self.fm("digest").stdout)
        for t in (dead, due, rest, young):
            self.assertEqual(c.find_brief(self.p, t).status, "deferred")


class TidyStateBackup(TidyCase):
    # T-0922: the briefs and the ledger are the record; tidy --apply keeps dated copies of them, the newest five
    def test_apply_keeps_dated_backups_of_the_record_and_only_five(self):
        import tarfile
        self.fm("capture", "keep me")
        self.tidy()  # a dry run writes nothing
        d = os.path.join(self.p.dir, "backups")
        self.assertFalse(os.path.isdir(d) and os.listdir(d))
        self.tidy("--apply")
        made = sorted(os.listdir(d))
        self.assertEqual(len(made), 1)
        with tarfile.open(os.path.join(d, made[0])) as t:
            names = t.getnames()
        self.assertTrue(any(n.startswith("tasks/T-0001") for n in names), names)
        self.assertIn("ledger.jsonl", names)
        self.assertIn("meta.json", names)
        self.assertFalse(any(n.startswith(("research", "backups", "graph")) for n in names), names)
        for i in range(6):  # older copies with earlier stamps
            with open(os.path.join(d, f"20200101T00000{i}Z.tar.gz"), "wb") as f:
                f.write(b"")
        self.tidy("--apply")
        left = sorted(os.listdir(d))
        self.assertEqual(len(left), 5)
        self.assertNotIn("20200101T000000Z.tar.gz", left)
        self.assertTrue(all(n.endswith(".tar.gz") for n in left))


class TidyHygiene(TidyCase):
    # T-1072: fixes unused for 60 days (or losing) are listed and retired on --apply; unused cards are only listed
    def seed_fixes(self):
        from sampo import fixes
        self.fm("fix", "add", "stale-sig: boom", "--do", "run the old thing")
        self.fm("fix", "add", "fresh-sig: bang", "--do", "run the new thing")
        self.fm("fix", "add", "losing-sig: nope", "--do", "run the wrong thing")
        ids = {f["sig"]: f["id"] for f in fixes.known(self.p)}
        stamp = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
        with open(fixes._stats_path(), "w") as f:
            json.dump({"fixes": {ids["stale-sig: boom"]: {"worked": 1, "last": stamp(time.time() - 90 * 86400)},
                                 ids["fresh-sig: bang"]: {"worked": 1, "last": stamp(time.time())},
                                 ids["losing-sig: nope"]: {"misses": 3, "last": stamp(time.time())}}}, f)

    def sigs(self):
        return sorted(f["sig"] for f in self.fm_json("fix", "list"))

    def test_stale_by_age_is_listed_never_deleted_and_a_losing_fix_is_removed_with_a_copy(self):
        self.seed_fixes()
        found = {f["kind"] + ":" + f["detail"] for f in self.tidy()["findings"] if f["kind"] in ("stale_fix", "losing_fix")}
        self.assertEqual(len(found), 2, found)
        self.assertTrue(any("stale-sig" in d for d in found) and any("losing-sig" in d for d in found))
        self.assertEqual(len(self.sigs()), 3)  # a dry run changes nothing
        self.tidy("--apply")
        self.assertEqual(self.sigs(), ["fresh-sig: bang", "stale-sig: boom"])
        kept = read_text(os.path.join(self.p.dir, "archive", "fixes-removed.md"))
        self.assertIn("losing-sig: nope", kept)
        self.assertRegex(kept, r"\d{4}-\d\d-\d\d")

    def test_quiet_rules_and_ignored_hints_are_listed_not_removed(self):
        # T-1115: a rule no call matched in 60 days, and hints shown again and again without use, are retire candidates
        self.fm("rule", "add", "dropdb", "--why", "never drop the database")
        self.fm("rule", "add", "npm publish", "--why", "releases go through CI", "--warn")
        path = os.path.join(self.p.dir, "rules.json")
        with open(path) as f:
            rules = json.load(f)
        old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 90 * 86400))
        for r in rules:
            r["at"] = old
        with open(path, "w") as f:
            json.dump(rules, f)
        self.hook("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": "npm publish --dry-run"}})
        with open(os.path.join(self.p.dir, "hints.json"), "w") as f:
            json.dump({"ignored": {"batch": 9, "skills": 1}}, f)
        found = {f["kind"]: f["detail"] for f in self.tidy("--apply")["findings"]}
        self.assertIn("dropdb", found.get("quiet_rule", ""))
        self.assertNotIn("npm publish", found.get("quiet_rule", ""))  # it matched a call just now
        self.assertIn("batch", found.get("ignored_hint", ""))
        self.assertNotIn("skills", found.get("ignored_hint", ""))
        self.assertEqual(len(self.fm_json("rule", "list")), 2, "only the user removes a rule")

    def test_a_fix_synced_from_the_repo_is_not_touched(self):
        from sampo import fixes
        rec = {"sig": "synced-sig: x", "do": "do it"}
        with open(os.path.join(self.p.dir, "fixes.md"), "w") as f:
            f.write(fixes._line(rec))
        fid = fixes._fid(rec)
        with open(fixes._stats_path(), "w") as f:
            json.dump({"fixes": {fid: {"misses": 5}}}, f)
        found = [x for x in self.tidy("--apply")["findings"] if "synced-sig" in x["detail"]]
        self.assertTrue(found and "shared" in found[0]["detail"])
        self.assertEqual(self.sigs(), ["synced-sig: x"])

    def test_unused_cards_are_those_the_map_picked_for_this_project(self):
        def unused():
            return [x for x in self.tidy()["findings"] if x["kind"] == "unused_card"]
        self.assertEqual(unused(), [])  # no map, no claim
        with open(os.path.join(self.p.dir, "map.json"), "w") as f:
            json.dump({"lib_cards": [{"name": "alpha", "detect": []}, {"name": "beta", "detect": []}]}, f)
        found = unused()
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0]["detail"].startswith("2 of 2"), found[0]["detail"])
        c.log_event(self.p, "enforce", data={"kind": "card_injected", "card": "alpha"})
        found = unused()
        self.assertTrue(found[0]["detail"].startswith("1 of 2") and "beta" in found[0]["detail"]
                        and "alpha" not in found[0]["detail"], found[0]["detail"])
        self.tidy("--apply")  # listed only


class DecisionSunset(TidyCase):
    # T-1084: a decision can expire; the digest lists the week's decisions, not only costly ones
    def test_a_past_sunset_is_expired_in_tidy_and_the_digest_lists_every_decision(self):
        bad = self.fm("decide", "x", "--sunset", "2020-13-45", check=False)
        self.assertNotEqual(bad.returncode, 0)
        self.fm("decide", "use the old cache", "--why", "temporary", "--sunset", "2020-01-01")
        self.fm("decide", "use sqlite for state", "--why", "simple")
        self.assertIn("sunset", self.fm("tidy", env={"HOME": self.fake_home}).stdout)
        d = self.fm("digest").stdout
        self.assertIn("Decided without you:", d)
        self.assertIn("use sqlite for state", d)
        self.assertIn("use sqlite for state", " ".join(self.fm_json("digest")["decided"]))

    def test_a_later_row_settles_the_sunset_and_bad_or_misplaced_flags_are_refused(self):
        for bad in ("20200101", "2020-W01-1"):
            self.assertNotEqual(self.fm("decide", "x", "--sunset", bad, check=False).returncode, 0, bad)
        for extra in (["--list"], ["--digest"], ["--ask", "q?", "--default", "a"]):
            self.assertNotEqual(self.fm("decide", "x", "--sunset", "2030-01-01", *extra, check=False).returncode, 0, extra)
        self.assertNotEqual(self.fm("decide", "--sunset", "2030-01-01", check=False).returncode, 0)
        r = self.fm("decide", "use the old cache", "--sunset", "2020-01-01")
        self.assertIn("already expired", r.stdout)
        self.assertIn("sunset", self.fm("tidy", env={"HOME": self.fake_home}).stdout)
        self.fm("decide", "keep the old cache for now", "--revisited", "old cache")
        self.assertNotIn("sunset", self.fm("tidy", env={"HOME": self.fake_home}).stdout)

    def test_a_costly_decision_is_listed_once_in_the_digest(self):
        self.fm("decide", "ship without review", "--kind", "costly")
        self.assertEqual(self.fm("digest").stdout.count("ship without review"), 1)


class Rollback(TidyCase):
    def test_an_upgrade_snapshots_the_state_and_rollback_restores_a_deleted_brief(self):
        self.fm("task", "new", "keep me", "--type", "FIX", "--tier", "S", "--ac", "x :: true", "--step", "s")
        with open(os.path.join(c.state_dir(), "seen-version"), "w") as f:
            f.write("0.1")
        self.hook("SessionStart", {"source": "startup"})
        names = os.listdir(os.path.join(c.state_dir(), "snapshots"))
        self.assertEqual(len(names), 1)
        self.assertTrue(names[0].startswith("0.1-"))
        self.assertIn(names[0], self.fm("rollback", "--list").stdout)
        brief = c.brief_paths(self.p)[0]
        self.assertTrue(os.path.exists(brief))
        os.remove(brief)
        r = self.fm("rollback", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--yes", r.stdout + r.stderr)
        self.assertFalse(os.path.exists(brief))
        self.fm("decide", "after the snapshot")
        self.fm("rollback", "--yes")
        self.assertTrue(os.path.exists(brief))
        self.assertIn("rollback", read_text(os.path.join(self.p.dir, "ledger.jsonl")))
        self.assertIn("after the snapshot", read_text(os.path.join(self.p.dir, "ledger.jsonl")))  # never restored
        self.fm("verify-ledger")
        listing = json.loads(self.fm("rollback", "--list", "--json").stdout)
        self.assertEqual(sorted(x["pre_rollback"] for x in listing), [False, True])

    def snap(self):
        with open(os.path.join(c.state_dir(), "seen-version"), "w") as f:
            f.write("0.1")
        self.hook("SessionStart", {"source": "startup"})

    def test_consent_revoked_since_the_snapshot_stays_revoked(self):
        meta = c.read_meta(self.p)
        meta["standing"] = {"core": {"at": "2026-01-01T00:00:00Z", "why": "t"}}
        c.write_meta(self.p, meta)
        self.fm("task", "new", "keep me", "--type", "FIX", "--tier", "S", "--ac", "x :: true", "--step", "s")
        self.snap()
        self.fm("standing", "off")
        self.fm("rollback", "--yes")
        self.assertNotIn("standing", c.read_meta(self.p))

    def test_a_brief_archived_since_does_not_come_back(self):
        self.fm("task", "new", "keep me", "--type", "FIX", "--tier", "S", "--ac", "x :: true", "--step", "s")
        self.snap()
        brief = c.brief_paths(self.p)[0]
        arch = os.path.join(self.p.dir, "archive", "2026-10")
        os.makedirs(arch)
        os.replace(brief, os.path.join(arch, os.path.basename(brief)))
        self.fm("rollback", "--yes")
        self.assertEqual(c.brief_paths(self.p), [])

    def test_an_abbreviated_yes_is_an_error(self):
        self.snap()
        self.assertNotEqual(self.fm("rollback", "--ye", check=False).returncode, 0)
        self.assertEqual(self.fm("rollback", "--list", check=False).returncode, 0)

    def test_a_tampered_snapshot_is_refused(self):
        self.snap()
        d = os.path.join(c.state_dir(), "snapshots")
        path = os.path.join(d, os.listdir(d)[0])
        with open(path, "wb") as f:
            f.write(b"not a tarball")
        r = self.fm("rollback", "--yes", check=False)
        self.assertNotEqual(r.returncode, 0)


class UnblockedByGit(TidyCase):
    def test_a_fresh_mtime_is_no_change_but_a_commit_is(self):
        # T-1106: a fresh clone or a builder's worktree gave every file a new mtime, so every blocked task read
        # "changed since it was blocked"
        import subprocess
        import time
        from sampo import tidy as stidy
        with open(os.path.join(self.repo, "a.py"), "w") as f:
            f.write("x = 1\n")
        subprocess.run(["git", "-C", self.repo, "add", "a.py"], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", "a"], check=True)
        tid = self.fm_json("task", "new", "Wait", "--type", "FIX", "--tier", "S", "--ac", "ok :: true", "--step", "s")["id"]
        self.fm("task", "block", tid, "waiting on a.py to settle")
        time.sleep(1.1)
        later = time.time() + 30
        os.utime(os.path.join(self.repo, "a.py"), (later, later))  # what a clone does
        b = c.find_brief(self.p, tid)
        self.assertIsNone(stidy.unblocked(self.p, b, {}))
        with open(os.path.join(self.repo, "a.py"), "w") as f:
            f.write("x = 2\n")
        subprocess.run(["git", "-C", self.repo, "commit", "-qam", "a changed"], check=True)
        self.assertIn("a.py changed", stidy.unblocked(self.p, c.find_brief(self.p, tid), {}) or "")


class ExamWork(TidyCase):
    """T-2568: fm tidy prunes a graded exam run's work copies once its newest grade is 14 days old and a newer run of
    the same tier was graded for that agent; answers, grades and ungraded runs stay."""
    def seed(self, rel, run, agent, at, tier=None):
        from sampo import rune
        base = os.path.join(self.p.dir, "exam", *rel, run)
        os.makedirs(os.path.join(base, "work", agent, "sc1"), exist_ok=True)
        os.makedirs(os.path.join(base, "answers", agent), exist_ok=True)
        with open(os.path.join(base, "work", agent, "sc1", "big.txt"), "w") as f:
            f.write("x" * 2_000_000)
        with open(os.path.join(base, "answers", agent, "sc1.json"), "w") as f:
            f.write("{}")
        home = rune.home(self.p)
        os.makedirs(home, exist_ok=True)
        with open(os.path.join(home, "exam.jsonl"), "a") as f:
            f.write(json.dumps({"run": run, "agent": agent, "at": at, **({"tier": tier} if tier else {})}) + "\n")
        return base

    def test_old_superseded_runs_lose_work_copies_only(self):
        old = self.seed((), "s1-001", "foreman", "2026-01-01T00:00:00Z")
        new = self.seed((), "s2-002", "foreman", "2026-01-20T00:00:00Z")
        lng = self.seed(("long",), "l001", "foreman", "2026-01-01T00:00:00Z", tier="long")  # newest of its tier
        ungraded = os.path.join(self.p.dir, "exam", "s3-003", "work", "foreman")
        os.makedirs(ungraded)
        dry = [f for f in self.tidy()["findings"] if f["kind"] == "exam_work"]
        self.assertEqual(len(dry), 1, dry)
        self.assertIn("s1-001", dry[0]["detail"])
        self.assertIn("2 MB", dry[0]["detail"])
        self.assertTrue(os.path.isdir(os.path.join(old, "work", "foreman", "sc1")), "a dry run removes nothing")
        self.tidy("--apply")
        self.assertFalse(os.path.exists(os.path.join(old, "work", "foreman", "sc1")))
        self.assertTrue(os.path.isfile(os.path.join(old, "work", "foreman", "PRUNED")))
        self.assertTrue(os.path.isfile(os.path.join(old, "answers", "foreman", "sc1.json")))
        for keep in (new, lng):
            self.assertTrue(os.path.isdir(os.path.join(keep, "work", "foreman", "sc1")), keep)
        self.assertTrue(os.path.isdir(ungraded))
        self.assertNotIn("exam_work", self.kinds(self.tidy()), "a pruned run is not listed again")

    def test_a_line_naming_a_path_is_ignored(self):
        self.seed((), "s2-002", "foreman", "2026-01-20T00:00:00Z")
        from sampo import rune
        with open(os.path.join(rune.home(self.p), "exam.jsonl"), "a") as f:
            f.write(json.dumps({"run": "../../x", "agent": "foreman", "at": "2026-01-01T00:00:00Z"}) + "\n")
        self.assertNotIn("exam_work", self.kinds(self.tidy()))

    def test_a_symlinked_work_dir_is_never_followed(self):
        old = self.seed((), "s1-001", "foreman", "2026-01-01T00:00:00Z")
        self.seed((), "s2-002", "foreman", "2026-01-20T00:00:00Z")
        outside = os.path.join(self.tmp, "outside")
        shutil.move(os.path.join(old, "work", "foreman"), outside)
        os.symlink(outside, os.path.join(old, "work", "foreman"))
        self.tidy("--apply")
        self.assertTrue(os.path.isfile(os.path.join(outside, "sc1", "big.txt")))
        self.assertFalse(os.path.exists(os.path.join(outside, "PRUNED")))

