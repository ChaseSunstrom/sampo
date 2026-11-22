"""T-1068: lanes claim the files and symbols they will change up front; an overlapping claim is refused naming the
holder and its task."""
# map-links: plugin/agents/fm-builder.md
import json
import os
import subprocess

from helpers import read_text
from test_bus import Base

from sampo import core as c

A = {"CLAUDE_CODE_SESSION_ID": "sess-a"}
B = {"CLAUDE_CODE_SESSION_ID": "sess-b"}


class Claims(Base):
    def test_a_second_claim_on_the_same_symbol_fails_naming_the_holder(self):
        first = self.fm("lease", "take", "a.py::f", env=A)
        self.assertEqual(first.returncode, 0, first.stderr)
        r = self.fm("lease", "take", "a.py::f", env=B, check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("sess-a", r.stderr + r.stdout)
        self.assertIn("T-", r.stderr + r.stdout, "names its task")
        self.assertEqual(self.fm("lease", "take", "a.py::g", env=B).returncode, 0, "another symbol is free")

    def test_a_file_wide_claim_overlaps_any_symbol_and_warn_takes_it(self):
        self.fm("lease", "take", "a.py", env=A)
        r = self.fm("lease", "take", "a.py::f", "b.py::h", env=B, check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("b.py", self.fm("lease", "list").stdout, "a refused claim takes none of its targets")
        r = self.fm("lease", "take", "a.py::f", "--warn", env=B)
        self.assertIn("overlaps", r.stdout + r.stderr)
        self.assertIn("a.py::f", self.fm("lease", "list").stdout)

    def test_a_bare_drop_keeps_another_tasks_leases_in_the_same_session(self):
        # T-1216: builder lanes share the main session's id; a builder's bare drop released the main thread's leases
        self.fm("task", "new", "One", "--type", "FIX", "--tier", "S", "--ac", "x :: true", "--step", "s", "--focus", env=A)
        self.fm("lease", "take", "a.py", env=A)
        self.fm("task", "new", "Two", "--type", "FIX", "--tier", "S", "--ac", "x :: true", "--step", "s", "--focus", env=A)
        self.fm("lease", "take", "b.py", env=A)
        self.assertIn("Dropped 1 lease(s)", self.fm("lease", "drop", env=A).stdout)
        held = self.fm("lease", "list").stdout
        self.assertIn("a.py", held, "the first task's lease stays")
        self.assertNotIn("b.py", held)

    def test_a_bare_drop_with_no_active_task_frees_only_closed_or_taskless_leases(self):
        # T-2870: T-2868's builder, between steps, dropped 16 leases of the shared session, the main thread's included
        one = self.fm_json("task", "new", "One", "--type", "FIX", "--tier", "S", "--ac", "x :: true", "--step", "s",
                           "--focus", env=A)["id"]
        self.fm("lease", "take", "a.py", env=A)
        two = self.fm_json("task", "new", "Two", "--type", "FIX", "--tier", "S", "--ac", "x :: true", "--step", "s",
                           "--focus", env=A)["id"]
        self.fm("lease", "take", "b.py", env=A)
        self.fm("task", "block", two, "waits", env=A)  # nothing active now
        self.assertIn("Dropped 0 lease(s)", self.fm("lease", "drop", env=A).stdout, "open tasks keep theirs")
        held = self.fm("lease", "list").stdout
        self.assertIn("a.py", held)
        self.assertIn("b.py", held)
        self.fm("task", "drop", one, "not needed", env=A)
        self.fm("lease", "drop", env=A)
        held = self.fm("lease", "list").stdout
        self.assertNotIn("a.py", held, "a closed task's lease is freed")
        self.assertIn("b.py", held)

    def test_a_file_named_drop_keeps_another_tasks_leases_in_the_same_session(self):
        # T-1416: a builder lane's `fm lease drop plugin/lib/fmcode.py` freed two other lanes' leases on that file
        self.fm("task", "new", "One", "--type", "FIX", "--tier", "S", "--ac", "x :: true", "--step", "s", "--focus", env=A)
        self.fm("lease", "take", "a.py::f", env=A)
        self.fm("task", "new", "Two", "--type", "FIX", "--tier", "S", "--ac", "x :: true", "--step", "s", "--focus", env=A)
        self.fm("lease", "take", "a.py::g", env=A)
        self.assertIn("Dropped 1 lease(s)", self.fm("lease", "drop", "a.py", env=A).stdout)
        held = self.fm("lease", "list").stdout
        self.assertIn("a.py::f", held, "the first task's lease stays")
        self.assertNotIn("a.py::g", held)

    def test_another_task_of_the_same_session_is_refused_and_warn_keeps_both(self):
        # T-1588: builder lanes share the main session's id; T-1510's take handed T-1509's live lease to itself
        one = self.fm_json("task", "new", "One", "--type", "FIX", "--tier", "S", "--ac", "x :: true", "--step", "s",
                           env=A)["id"]
        self.fm("focus", one, env=A)
        self.fm("lease", "take", "a.py", env=A)
        self.fm("task", "new", "Two", "--type", "FIX", "--tier", "S", "--ac", "x :: true", "--step", "s", "--focus", env=A)
        r = self.fm("lease", "take", "a.py", env=A, check=False)
        self.assertNotEqual(r.returncode, 0, "another task's lease isn't mine because the session is")
        self.assertIn(one, r.stderr + r.stdout)
        self.fm("lease", "take", "a.py", "--warn", env=A)
        rows = self.fm_json("lease", "list")["leases"]
        self.assertEqual(len([v for v in rows if v["file"] == "a.py"]), 2, "both tasks' claims stay")
        self.assertEqual(self.fm("lease", "take", "b.py", env=A).returncode, 0)
        self.assertEqual(self.fm("lease", "take", "b.py", env=A).returncode, 0, "a task retakes its own lease")
        from sampo import bus
        bus.release(self.project(), one)  # the lane is removed: its task's leases go with it
        self.assertEqual(len([v for v in self.fm_json("lease", "list")["leases"] if v["file"] == "a.py"]), 1)

    def test_drop_releases_only_the_callers_leases_and_force_names_a_foreign_one(self):
        # T-3371: a lane's drop took leases of tasks it couldn't see; another lane's bare drop said "Dropped 0"
        from sampo import bus
        self.fm("task", "new", "One", "--type", "FIX", "--tier", "S", "--ac", "x :: true", "--step", "s", "--focus", env=A)
        self.fm("lease", "take", "a.py", env=A)
        bus.preclaim(self.project(), self.fm_json("status")["active"]["id"], ["pre.py"])
        self.fm("lease", "take", "b.py", env=B)
        out = self.fm("lease", "drop", env=A).stdout
        self.assertIn("Dropped 1 lease(s)", out, "its own take only")
        held = self.fm("lease", "list").stdout
        self.assertIn("b.py", held, "another session's lease stays")
        self.assertIn("pre.py", held, "the main checkout never drops the lane's pre-claim")
        named = self.fm("lease", "drop", "b.py", env=A)
        self.assertIn("Dropped 0 lease(s)", named.stdout)
        self.assertIn("--force", named.stderr)
        self.assertIn("Dropped 1 lease(s)", self.fm("lease", "drop", "b.py", "--force", env=A).stdout)
        self.assertIn("lease.forced_drop", read_text(os.path.join(self.project().dir, "ledger.jsonl")))
        self.assertIn("dropped with --force", read_text(os.path.join(bus._dir(), "mail.jsonl")), "the holder is told")
        for bad in (["drop", "--force"], ["list", "--force"], ["take", "c.py", "--force"]):
            self.assertNotEqual(self.fm("lease", *bad, env=A, check=False).returncode, 0)

    def test_a_bare_drop_with_no_active_task_keeps_a_lease_of_a_task_it_cannot_see(self):
        from sampo import bus
        data = bus._leases()
        bus._claim(data, self.project(), "sess-a", "T-9999", "z.py", "*", 600)
        bus._write_leases(data)
        self.fm("lease", "take", "q.py", env=A)
        self.assertIn("Dropped 1 lease(s)", self.fm("lease", "drop", env=A).stdout)
        self.assertIn("z.py", self.fm("lease", "list").stdout)

    def test_dropping_a_file_releases_its_symbol_leases(self):
        self.fm("lease", "take", "a.py::f", "a.py::g", "c.py::h", env=A)
        self.assertIn("Dropped 2 lease(s)", self.fm("lease", "drop", "a.py", env=A).stdout)
        held = self.fm("lease", "list").stdout
        self.assertNotIn("a.py", held)
        self.assertIn("c.py::h", held)

    def test_drop_and_expiry_free_the_claim(self):
        self.fm("lease", "take", "a.py::f", env=A)
        self.fm("lease", "drop", "a.py::f", env=A)
        self.assertEqual(self.fm("lease", "take", "a.py::f", env=B).returncode, 0)
        self.fm("lease", "take", "c.py", "--minutes", "-1", env=A)
        self.assertEqual(self.fm("lease", "take", "c.py", env=B).returncode, 0, "expired")

    def test_a_bare_drop_releases_every_lease_this_session_holds(self):
        # T-1134: the brief says "fm lease drop when done"; five builders ran it bare and got only usage
        self.fm("lease", "take", "a.py::f", "b.py", env=A)
        self.fm("lease", "take", "c.py", env=B)
        r = self.fm("lease", "drop", env=A)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Dropped 2 lease(s)", r.stdout)  # T-1205: not just "No leases held."
        self.assertIn("Dropped 0 lease(s)", self.fm("lease", "drop", "z.py", env=A).stdout)
        held = self.fm("lease", "list").stdout
        self.assertNotIn("a.py", held)
        self.assertNotIn("b.py", held)
        self.assertIn("c.py", held, "another session's lease stays")

    def test_a_root_relative_path_from_a_subdirectory_is_leased_as_itself(self):
        # T-1148: from mods/ui, 'fm lease take mods/ui/x.tsx' was recorded doubled
        sub = os.path.join(self.repo, "mods", "ui")
        os.makedirs(sub)
        for f in (os.path.join(sub, "x.tsx"), os.path.join(sub, "y.tsx")):
            open(f, "w").close()
        self.fm("lease", "take", "mods/ui/x.tsx", cwd=sub, env=A)
        self.fm("lease", "take", "y.tsx", "new.tsx", cwd=sub, env=A)
        held = self.fm("lease", "list").stdout
        self.assertIn("mods/ui/x.tsx::*", held)
        self.assertNotIn("mods/ui/mods", held)
        self.assertIn("mods/ui/y.tsx::*", held, "cwd-relative as before")
        self.assertIn("mods/ui/new.tsx::*", held, "a new file keeps the cwd meaning")

    def test_a_lane_brief_preclaims_its_concrete_scope_and_names_the_command(self):
        tid = self.task(title="Lane work", tier="S", focus=False, scope=["x.py", "lib/*.py"])
        path = json.loads(self.fm("lane", "brief", tid, "--json").stdout)["path"]
        with open(path) as f:
            self.assertRegex(f.read(), r"(?:fm|sampo) lease take")  # T-2855: the contract teaches sampo since T-2778
        out = self.fm("lease", "list").stdout
        self.assertIn("x.py::*", out)
        self.assertIn(tid, out)
        self.assertNotIn("lib/*.py", out, "a glob isn't claimed")
        r = self.fm("lease", "take", "x.py::f", env=B, check=False)
        self.assertEqual(r.returncode, 0, "a lane's pre-claim is advisory: the take notes it (T-3283)")
        self.assertIn(tid, r.stderr + r.stdout)

    def test_a_lane_take_over_a_sibling_lanes_hold_notes_it_and_takes_the_lease(self):
        # T-5720: parallel builders share the lead's session id, so a sibling lane's hold reads as the same session and
        # stalled T-5591, T-5709 and T-5712 at a take that refused; another session's hold still refuses
        lane = os.path.join(self.tmp, "lane")
        subprocess.run(["git", "-C", self.repo, "worktree", "add", "-q", "-b", "foreman/lane", lane], check=True)
        self.fm("task", "new", "One", "--type", "FIX", "--tier", "S", "--ac", "x :: true", "--step", "s", "--focus", env=A)
        self.assertEqual(self.fm("lease", "take", "a.py::f", env=A).returncode, 0)
        self.fm("task", "new", "Two", "--type", "FIX", "--tier", "S", "--ac", "x :: true", "--step", "s", "--focus",
                env=A, cwd=lane)
        r = self.fm("lease", "take", "a.py", env=A, cwd=lane, check=False)  # file-wide: overlaps the main task's a.py::f
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Took 1 lease(s)", r.stdout)
        self.assertIn("sibling lane", r.stderr, "the take names the sibling's hold it went over")
        self.assertEqual(len([v for v in self.fm_json("lease", "list")["leases"] if v["file"] == "a.py"]), 2,
                         "both tasks' claims stay: the main checkout's a.py::f and the lane's a.py::*")
        self.fm("lease", "drop", "a.py::f", env=A)  # only the lane's take holds a.py now
        with open(os.path.join(self.repo, "a.py"), "w") as f:
            f.write("def f():\n    return 1\n")
        _, out = self.note("sess-b", "Edit", {"file_path": os.path.join(self.repo, "a.py"),
                                              "old_string": "return 1", "new_string": "return 2"})
        self.assertEqual(out.get("permissionDecision"), "deny", "the sibling's take is a real hold, not advisory")
        r = self.fm("lease", "take", "a.py::f", env=B, cwd=lane, check=False)
        self.assertEqual(r.returncode, 1, "another session's explicit hold still refuses")
        self.assertIn("overlaps session sess-a", r.stderr)


class ClaimsReview(Base):
    def lane_task(self, scope):
        return self.task(title="Lane work", tier="S", focus=False, scope=scope)

    def test_a_brief_never_displaces_a_live_lease_and_says_so(self):
        path = os.path.join(self.repo, "a.py")
        with open(path, "w") as f:
            f.write("def f():\n    return 1\n")
        self.fm("lease", "take", "a.py", env=A)
        tid = self.lane_task(["a.py"])
        res = json.loads(self.fm("lane", "brief", tid, "--json").stdout)
        with open(res["path"]) as f:
            self.assertIn("a.py", f.read().split("## Claims")[1].split("## Your contract")[0])
        _, out = self.note("sess-a", "Edit", {"file_path": path, "old_string": "return 1", "new_string": "return 2"})
        self.assertNotEqual(out.get("permissionDecision"), "deny", "the holder keeps its lease")

    def test_warn_coexists_with_the_other_lease(self):
        path = os.path.join(self.repo, "a.py")
        with open(path, "w") as f:
            f.write("def f():\n    return 1\n")
        self.fm("lease", "take", "a.py::f", env=A)
        r = self.fm("lease", "take", "a.py::f", "--warn", env=B)
        self.assertIn("overlaps", r.stderr)
        self.assertEqual(r.stdout.count("overlaps"), 0, "warnings go to stderr")
        _, out = self.note("sess-a", "Edit", {"file_path": path, "old_string": "return 1", "new_string": "return 2"})
        self.assertNotEqual(out.get("permissionDecision"), "deny", "A's claim survived B's --warn")

    def test_release_frees_a_lane_claim_and_the_full_id_is_printed(self):
        from sampo import bus
        p = self.project()
        bus.preclaim(p, "T-1068", ["x.py"])
        r = self.fm("lease", "take", "x.py", env=B, check=False)
        self.assertIn("lane-T-1068", r.stderr + r.stdout)
        bus.release(p, "T-1068")
        self.assertEqual(self.fm("lease", "take", "x.py", env=B).returncode, 0)

    def test_no_lease_means_no_brief_load(self):
        from sampo import bus
        p = self.project()

        def boom(*a, **k):
            raise AssertionError("loaded briefs")
        real, c.load_briefs = c.load_briefs, boom
        try:
            self.assertIsNone(bus.conflict({"session_id": "s", "tool_name": "Edit", "tool_input": {}}, p,
                                             os.path.join(p.root, "free.py")))
        finally:
            c.load_briefs = real

    def test_json_refusal_is_structured_and_paths_are_normalised(self):
        self.fm("lease", "take", "new.py", env=A)
        r = self.fm("lease", "take", "./new.py::f", "--json", env=B, check=False)
        self.assertNotEqual(r.returncode, 0)
        refused = json.loads(r.stdout)["refused"]
        self.assertEqual(refused[0]["target"], "new.py::f")
        self.assertEqual(refused[0]["session"], "sess-a")
        self.assertIn("task", refused[0])
        r = self.fm("lease", "take", os.path.join(self.repo, "new.py"), env=B, check=False)
        self.assertNotEqual(r.returncode, 0, "an absolute path is the same file")

    def test_an_edits_lease_leaves_another_sessions_lease_intact(self):
        from sampo import bus
        p = self.project()
        path = os.path.join(self.repo, "a.py")
        with open(path, "w") as f:
            f.write("def f():\n    return 2\n")
        self.fm("lease", "take", "a.py::f", env=B)
        bus.after_edit({"session_id": "sess-a", "tool_name": "Write", "tool_input": {}}, p, path)
        held = {(v["session"], v["symbol"]) for v in bus._leases().values() if v["file"] == "a.py"}
        self.assertEqual(held, {("sess-b", "f"), ("sess-a", "*")})

    def test_a_lane_preclaim_never_denies_another_worktrees_edit(self):
        # T-1138: lane-T-1135's file-wide pre-claim of fmcli.py stalled T-1137's builder in its own worktree; lanes
        # edit separate checkouts and the merge settles overlaps, so a pre-claim is advisory (fm lease list shows it)
        from sampo import bus
        p = self.project()
        act = c.active_brief(c.load_briefs(p), p.lane)
        path = os.path.join(self.repo, "x.py")
        with open(path, "w") as f:
            f.write("x = 1\n")
        pl = {"session_id": "s", "tool_name": "Edit", "tool_input": {"old_string": "x = 1", "new_string": "x = 2"}}
        bus.preclaim(p, "T-9999", ["x.py"])
        self.assertIsNone(bus.conflict(pl, p, path), "another lane's pre-claim is advisory")
        self.assertIn("lane-T-9999", self.fm("lease", "list").stdout, "and still listed")
        bus.release(p, "T-9999")
        bus.preclaim(p, act.id, ["x.py"])
        self.assertIsNone(bus.conflict(pl, p, path), "the lane's own task edits its claim")
