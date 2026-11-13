"""T-4222 (big tiers part C1, T-4219): a big task has its own integration branch (big/<ID>, cut once from the build head,
never pushed), its children's lanes are cut from it, and `sampo big run` hands out every ready child at once as a wave,
capped by the free lane slots and the load gate, none twice. A task with no big parent gets the lane it gets today."""
import argparse
import contextlib
import io
import json
import os
import subprocess
import unittest.mock

import test_big_plan as tbp

from sampo import big
from sampo import core as c
from sampo import lanes


class Base(tbp.Base):
    def git(self, *args, cwd=None, check=True):
        r = subprocess.run(["git", "-C", cwd or self.repo, "-c", "user.email=t@t", "-c", "user.name=t", *args],
                           capture_output=True, text=True)
        if check and r.returncode:
            raise AssertionError(f"git {' '.join(args)}: {r.stderr}")
        return r.stdout.strip()

    def commit(self, name, text="x\n", msg=None):
        with open(os.path.join(self.repo, name), "w") as f:
            f.write(text)
        self.git("add", name)
        self.git("commit", "-qm", msg or f"add {name}")
        return self.git("rev-parse", "HEAD")

    def big(self, slots=5):
        """An expanded XL task: c1 and c2 in the first wave, c3 after both. -> (parent id, {key: child id})"""
        tid = self.make()
        self.good(tid)
        self.fm("big", "expand", tid)
        self.fm("lane", "max", str(slots))
        got = big._children(c.find_brief(self.p, tid))
        return tid, {k: i for k, (i, _) in got.items()}

    def run_json(self, tid, *more, env=None):
        return json.loads(self.fm("big", "run", tid, "--json", *more, env=env).stdout)

    def tip(self, tid):
        return self.git("rev-parse", f"big/{tid}")

    def land(self, tid, kid):
        """What a lane's merge onto the integration branch leaves: a merge commit whose subject names the child."""
        title = c.find_brief(self.p, kid).title
        branch = f"big/{tid}"
        new = self.git("commit-tree", f"{branch}^{{tree}}", "-p", branch, "-m", f"Merge {kid}: {title}")
        self.git("update-ref", f"refs/heads/{branch}", new)

    def meta(self, tid):
        return c.find_brief(self.p, tid).meta

    def brief_text(self, kid):
        with open(os.path.join(self.p.dir, "audits", f"{kid}.builder.md"), encoding="utf-8") as f:
            return f.read()


class Branch(Base):
    def test_branch_is_cut_once_from_the_build_head_and_recorded(self):
        tid, _ = self.big()
        head = self.git("rev-parse", "HEAD")
        got = big.branch(self.p, c.find_brief(self.p, tid))
        self.assertEqual((got["branch"], got["sha"], got["created"]), (f"big/{tid}", head, True))
        self.assertEqual(self.tip(tid), head)
        m = self.meta(tid)
        self.assertEqual((m["big_branch"], m["big_cut"]), (f"big/{tid}", head), "the children read it from the meta")
        self.commit("later.txt")  # the build head moves on; the branch stays where it was cut
        again = big.branch(self.p, c.find_brief(self.p, tid))
        self.assertEqual((again["sha"], again["created"]), (head, False))
        self.assertEqual(self.meta(tid)["big_cut"], head)
        self.assertEqual(self.git("remote"), "", "nothing is pushed: there is nowhere it could go, and no call tries")

    def test_branch_of_a_milestone_is_cut_from_the_giants_branch(self):
        giant = self.make(tier="GIANT", title="Everything")
        mile = self.make(tier="XL", title="A milestone")
        b = c.find_brief(self.p, mile)
        b.meta["parent"] = giant
        c.save_brief(self.p, b)
        outer = big.branch(self.p, c.find_brief(self.p, giant))
        self.commit("g.txt")
        self.git("update-ref", f"refs/heads/big/{giant}", self.git("rev-parse", "HEAD"))  # the giant's branch moved on
        inner = big.branch(self.p, c.find_brief(self.p, mile))
        self.assertEqual(inner["sha"], self.tip(giant))
        self.assertNotEqual(inner["sha"], outer["sha"])
        self.assertEqual(self.meta(mile)["big_cut"], self.tip(giant))

    def test_sync_merges_the_build_branch_into_the_integration_branch(self):
        tid, _ = self.big()
        big.branch(self.p, c.find_brief(self.p, tid))
        head = self.commit("later.txt", msg="work on the build branch")
        self.assertNotEqual(self.tip(tid), head)
        out = self.fm("big", "sync", tid).stdout
        self.assertIn(f"big/{tid}", out)
        self.assertEqual(self.git("merge-base", "--is-ancestor", head, f"big/{tid}", check=False), "")
        self.assertTrue(self.git("show", f"big/{tid}:later.txt"))
        again = self.fm("big", "sync", tid).stdout
        self.assertIn("up to date", again)

    def test_sync_says_what_conflicted_and_changes_nothing(self):
        tid, _ = self.big()
        self.commit("clash.txt", "base\n")
        big.branch(self.p, c.find_brief(self.p, tid))
        self.git("checkout", "-q", "-b", "side", f"big/{tid}")  # a clashing edit on the integration branch
        with open(os.path.join(self.repo, "clash.txt"), "w") as f:
            f.write("on the branch\n")
        self.git("commit", "-qam", "branch edit")
        self.git("update-ref", f"refs/heads/big/{tid}", self.git("rev-parse", "HEAD"))
        self.git("checkout", "-q", "-")
        with open(os.path.join(self.repo, "clash.txt"), "w") as f:
            f.write("on the build branch\n")
        self.git("commit", "-qam", "build edit")
        before = self.tip(tid)
        r = self.fm("big", "sync", tid, check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("clash.txt", r.stdout + r.stderr)
        self.assertEqual(self.tip(tid), before, "a conflict leaves the integration branch as it was")

    def test_sync_refuses_a_branch_checked_out_somewhere(self):
        tid, _ = self.big()
        big.branch(self.p, c.find_brief(self.p, tid))
        self.git("checkout", "-q", f"big/{tid}")
        self.git("commit", "-q", "--allow-empty", "-m", "on the branch")
        self.git("checkout", "-q", "-b", "elsewhere")
        wt = os.path.join(self.tmp, "wt-big")
        self.git("worktree", "add", "-q", wt, f"big/{tid}")
        self.commit("later.txt")
        r = self.fm("big", "sync", tid, check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("checked out", r.stdout + r.stderr)


class Lane(Base):
    def test_a_childs_lane_is_cut_from_the_integration_branch(self):
        tid, kids = self.big()
        cut = self.git("rev-parse", "HEAD")
        big.branch(self.p, c.find_brief(self.p, tid))
        moved = self.commit("later.txt")  # main moves on: the lane's base must stay the branch tip
        self.assertNotEqual(cut, moved)
        out = json.loads(self.fm("lane", "brief", kids["c1"], "--json").stdout)
        text = open(out["path"], encoding="utf-8").read()
        self.assertIn(f"big/{tid}", text, "the brief says which branch the lane is cut from")
        self.assertIn(cut, text, "the contract's base is the branch tip")
        self.assertNotIn(moved, text, "not the build head")
        self.assertEqual(self.meta(kids["c1"]).get("big_base"), cut, "the lane's base is on the child's brief")

    def test_a_childs_lane_waits_for_the_children_it_comes_after(self):
        tid, kids = self.big()
        r = self.fm("lane", "brief", kids["c3"], check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(kids["c1"], r.stdout + r.stderr)
        self.assertNotIn("builder", self.meta(kids["c3"]))

    def test_a_nested_childs_lane_uses_the_nearest_big_ancestors_branch(self):
        giant = self.make(tier="GIANT", title="Everything")
        tid, kids = self.big()
        b = c.find_brief(self.p, tid)
        b.meta["parent"] = giant
        c.save_brief(self.p, b)
        self.commit("g.txt")
        text = open(json.loads(self.fm("lane", "brief", kids["c1"], "--json").stdout)["path"], encoding="utf-8").read()
        self.assertIn(f"big/{tid}", text)
        self.assertNotIn(f"big/{giant}`", text)
        self.assertEqual(self.meta(tid)["big_cut"], self.tip(giant), "the milestone's branch is cut from the giant's")


class Wave(Base):
    def test_wave_launches_every_ready_child_at_once_from_the_branch(self):
        tid, kids = self.big()
        out = self.run_json(tid)  # nothing is running: the first wave
        self.assertEqual([x["id"] for x in out["wave"]], [kids["c1"], kids["c2"]])
        self.assertEqual(out["branch"], f"big/{tid}")
        for x in out["wave"]:
            self.assertIn('subagent_type: "sampo:fm-builder"', x["launch"])
            self.assertIn('isolation: "worktree"', x["launch"])
            self.assertIn(self.tip(tid), self.brief_text(x["id"]))
            self.assertTrue(self.meta(x["id"]).get("builder"), "marked launched")
        self.assertEqual([x["id"] for x in out["waiting"]], [kids["c3"]])
        self.assertIn(kids["c1"], out["waiting"][0]["waits"])
        text = self.fm("big", "run", tid).stdout
        self.assertNotIn(kids["c1"] + ": Agent", text, "a second call hands out nothing again")

    def test_wave_is_capped_by_the_free_slots_and_the_rest_are_next_wave(self):
        tid, kids = self.big(slots=1)
        out = self.run_json(tid)
        self.assertEqual([x["id"] for x in out["wave"]], [kids["c1"]])
        self.assertEqual([x["id"] for x in out["next_wave"]], [kids["c2"]])
        self.assertIn("next wave", self.fm("big", "run", tid).stdout)
        again = self.run_json(tid)
        self.assertEqual(again["wave"], [], "the slot is taken, and c1 is not handed out twice")
        self.assertEqual([x["id"] for x in again["running"]], [kids["c1"]])
        self.fm("lane", "max", "2")
        third = self.run_json(tid)
        self.assertEqual([x["id"] for x in third["wave"]], [kids["c2"]], "only what is left")

    def test_wave_the_load_gate_is_not_bypassed(self):
        tid, kids = self.big()
        out = self.run_json(tid, env={"FOREMAN_HOST": "load=90,cpus=8,mem_mb=8192"})
        self.assertEqual(out["wave"], [])
        self.assertIn("load", out["held"])
        self.assertEqual(sorted(x["id"] for x in out["next_wave"]), sorted([kids["c1"], kids["c2"]]))
        self.assertFalse(self.meta(kids["c1"]).get("builder"))

    def test_wave_dry_run_changes_nothing(self):
        tid, kids = self.big()
        out = self.run_json(tid, "--dry-run")
        self.assertEqual([x["id"] for x in out["wave"]], [kids["c1"], kids["c2"]])
        self.assertTrue(out["dry_run"])
        self.assertFalse(self.meta(kids["c1"]).get("builder"))
        self.assertNotIn("big_branch", self.meta(tid))
        self.assertEqual(self.git("branch", "--list", "big/*"), "")
        self.assertFalse(os.path.exists(os.path.join(self.p.dir, "audits", f"{kids['c1']}.builder.md")))

    def test_wave_a_lane_that_fails_to_cut_is_reported_and_a_second_run_hands_out_the_rest(self):
        tid, kids = self.big()
        real = lanes.builder_brief

        def flaky(p, task, args, **kw):
            if task.id == kids["c1"]:
                raise RuntimeError("no worktree")
            return real(p, task, args, **kw)

        def run():
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                big.task_run(self.p, argparse.Namespace(id=tid, json=True, dry_run=False))
            return json.loads(buf.getvalue())
        with unittest.mock.patch.object(lanes, "builder_brief", side_effect=flaky):
            first = run()
        self.assertEqual([x["id"] for x in first["wave"]], [kids["c2"]])
        self.assertEqual([(x["id"], "no worktree" in x["reason"]) for x in first["cut_failed"]], [(kids["c1"], True)])
        second = run()
        self.assertEqual([x["id"] for x in second["wave"]], [kids["c1"]], "only what is left; c2 is not handed out twice")
        self.assertEqual(second["cut_failed"], [])

    def test_wave_a_launch_that_never_started_is_handed_out_again(self):
        tid, kids = self.big()
        self.run_json(tid)
        b = c.find_brief(self.p, kids["c1"])
        b.meta["builder"] = "2026-01-01T00:00:00Z"  # briefed long ago, no lane ever opened: the slot is released
        c.save_brief(self.p, b)
        again = self.run_json(tid)
        self.assertEqual([x["id"] for x in again["wave"]], [kids["c1"]])

    def test_wave_a_run_while_another_holds_the_wave_is_refused_and_hands_out_nothing(self):
        tid, kids = self.big()
        with unittest.mock.patch.object(big, "RUN_WAIT", 0.2), c.lock(os.path.join(self.p.dir, big.RUN_LOCK)):
            # another `big run` is handing out this wave right now: this one must not read the same ready children
            with self.assertRaises(c.LockTimeout):
                big.task_run(self.p, argparse.Namespace(id=tid, json=True, dry_run=False))
        self.assertFalse(self.meta(kids["c1"]).get("builder"), "nothing was briefed by the run that was refused")
        out = self.run_json(tid)  # once the lock is free the wave goes out as usual
        self.assertEqual([x["id"] for x in out["wave"]], [kids["c1"], kids["c2"]])

    def test_wave_all_landed_says_to_close_the_parent(self):
        tid, kids = self.big()
        big.branch(self.p, c.find_brief(self.p, tid))
        for k in ("c1", "c2", "c3"):
            self.land(tid, kids[k])
        out = self.run_json(tid)
        self.assertTrue(out["all_landed"])
        self.assertEqual(out["wave"], [])
        text = self.fm("big", "run", tid).stdout
        self.assertIn(f"close the parent, sampo big close {tid}", text, "the verb is part C2's: this line only names it")

    def test_wave_is_the_next_action_of_a_focused_expanded_big_task(self):
        tid, kids = self.big()
        self.fm("task", "set", tid, "--section", "Interpretation", "--text",
                "Reading 1: all three parts. Reading 2: only the parser; ruled out, the criteria name all three.")
        self.fm("task", "set", tid, "--section", "Risks and rollback", "--text", "a child breaks main: revert its merge")
        self.fm("task", "set", tid, "--section", "Build vs reuse", "--text", "reuses the parser; nothing else fits")
        self.fm("task", "set", tid, "approved=true")
        self.fm("focus", tid)
        nxt = self.fm("next").stdout
        self.assertIn("launch these 2 builders in one message", nxt)
        self.assertIn(f"sampo big run {tid}", nxt)

    def test_wave_needs_an_expanded_big_task(self):
        tid = self.make()
        self.good(tid)
        r = self.fm("big", "run", tid, check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(f"sampo big expand {tid}", r.stdout + r.stderr)
        small = self.fm("task", "new", "Tiny", "--type", "FIX", "--tier", "S", "--ac", "ok :: true", "--step", "s",
                        "--json").stdout
        sid = json.JSONDecoder().raw_decode(small)[0]["id"]
        r = self.fm("big", "run", sid, check=False)
        self.assertNotEqual(r.returncode, 0)


class Order(Base):
    def test_order_a_child_after_unlanded_children_is_not_ready_until_they_land(self):
        tid, kids = self.big()
        first = self.run_json(tid)
        self.assertNotIn(kids["c3"], [x["id"] for x in first["wave"]])
        self.land(tid, kids["c1"])
        mid = self.run_json(tid)
        self.assertEqual(mid["wave"], [], "c2 has not landed: c3 still waits")
        self.assertEqual([x["id"] for x in mid["waiting"]], [kids["c3"]])
        self.assertEqual(mid["waiting"][0]["waits"], [kids["c2"]])
        self.land(tid, kids["c2"])
        last = self.run_json(tid)
        self.assertEqual([x["id"] for x in last["wave"]], [kids["c3"]])
        self.assertIn(self.tip(tid), self.brief_text(kids["c3"]), "the third lane starts from the branch with both landed")

    def test_order_a_reverted_landing_is_not_landed_and_holds_its_dependent_again(self):
        tid, kids = self.big()
        big.branch(self.p, c.find_brief(self.p, tid))
        self.land(tid, kids["c1"])
        self.land(tid, kids["c2"])
        up = c.find_brief(self.p, tid)
        self.assertEqual(big.unmet(self.p, c.find_brief(self.p, kids["c3"]), up), [])
        branch = f"big/{tid}"  # the revert of c1's merge on the branch: the newest subject naming c1 decides
        title = c.find_brief(self.p, kids["c1"]).title
        new = self.git("commit-tree", f"{branch}^{{tree}}", "-p", branch, "-m", f'Revert "Merge {kids["c1"]}: {title}"')
        self.git("update-ref", f"refs/heads/{branch}", new)
        self.assertEqual(big.unmet(self.p, c.find_brief(self.p, kids["c3"]), up), [kids["c1"]])
        out = self.run_json(tid)  # c1 is not on the branch any more: it is ready again, and c3 waits for it again
        self.assertEqual([x["id"] for x in out["wave"]], [kids["c1"]])
        self.assertEqual([(x["id"], x["waits"]) for x in out["waiting"]], [(kids["c3"], [kids["c1"]])])

    def test_order_a_blocked_child_is_reported_with_its_choices_and_does_not_stop_its_siblings(self):
        tid, kids = self.big()
        self.fm("task", "block", kids["c1"], "the parser needs a decision")
        out = self.run_json(tid)
        self.assertEqual([x["id"] for x in out["wave"]], [kids["c2"]])
        bad = {x["id"]: x for x in out["problems"]}
        self.assertIn(kids["c1"], bad)
        self.assertIn("decision", bad[kids["c1"]]["reason"])
        text = self.fm("big", "run", tid).stdout
        for word in ("retry", "re-plan", "drop"):
            self.assertIn(word, text)
        self.assertEqual([x["id"] for x in out["waiting"]], [kids["c3"]], "c3 waits on the blocked c1")

    def test_order_a_parked_child_is_reported_not_handed_out(self):
        tid, kids = self.big()
        self.run_json(tid)
        b = c.find_brief(self.p, kids["c1"])
        lane = os.path.join(self.tmp, "lane-c1")
        os.makedirs(lane)
        b.meta["lane"] = lane
        b.meta["lane_stop"] = "conflict 2026-01-01T00:00:00Z"
        c.save_brief(self.p, b)
        out = self.run_json(tid)
        self.assertIn(kids["c1"], {x["id"] for x in out["problems"]})
        self.assertNotIn(kids["c1"], [x["id"] for x in out["wave"]])


class Unchanged(Base):
    def new(self, title="Plain", tier="S", parent=None):
        out = self.fm("task", "new", title, "--type", "FIX", "--tier", tier, "--ac", "ok :: true", "--step", "s",
                      "--json").stdout
        tid = json.JSONDecoder().raw_decode(out)[0]["id"]
        if parent:
            b = c.find_brief(self.p, tid)
            b.meta["parent"] = parent
            c.save_brief(self.p, b)
        return tid

    def test_unchanged_a_task_with_no_big_parent_gets_the_lane_it_gets_today(self):
        tid = self.new()
        out = json.loads(self.fm("lane", "brief", tid, "--json").stdout)
        text = open(out["path"], encoding="utf-8").read()
        self.assertIn(self.git("rev-parse", "HEAD"), text)
        self.assertNotIn("big/", text)
        self.assertNotIn("integration branch", text.lower())
        self.assertEqual(self.git("branch", "--list", "big/*"), "")
        self.assertNotIn("big_base", self.meta(tid))

    def test_unchanged_a_child_of_a_task_that_is_not_big_is_cut_from_the_build_head(self):
        parent = self.new("A long task", tier="L")
        kid = self.new("Its split child", parent=parent)
        text = open(json.loads(self.fm("lane", "brief", kid, "--json").stdout)["path"], encoding="utf-8").read()
        self.assertIn(self.git("rev-parse", "HEAD"), text)
        self.assertNotIn("big/", text)
        self.assertEqual(self.git("branch", "--list", "big/*"), "")

    def test_unchanged_a_sibling_dependency_of_a_plain_task_still_means_closed(self):
        a = self.new("First")
        b = self.new("Second")
        self.fm("task", "set", b, f"depends_on={a}")
        ready = json.loads(self.fm("lane", "brief", "--ready", "--json").stdout)
        self.assertEqual([x["id"] for x in ready["briefed"]], [a])
        self.assertIn(a, {x["id"]: x["reason"] for x in ready["skipped"]}[b])

    def test_unchanged_a_plain_next_action_is_not_a_wave(self):
        self.new()
        self.assertNotIn("launch these", self.fm("next").stdout)


class Review(Base):
    """T-4222's review: what a closed child is worth to its dependents, the reads `sampo next` costs, a lane run from a
    lane, old git, and the states a dead or stalled child can be in."""

    def close(self, kid, status="done"):
        b = c.find_brief(self.p, kid)
        b.meta["status"] = status
        c.save_brief(self.p, b)

    def lane_for(self, kid, name, **meta):
        """A lane folder for the child (a real directory, not a worktree), as `lane brief` and the harness leave it."""
        path = os.path.join(self.tmp, name)
        os.makedirs(path)
        b = c.find_brief(self.p, kid)
        b.meta.update({"builder": c.now(), "lane": path, **meta})
        c.save_brief(self.p, b)
        return path

    def stopped_proc(self):
        from sampo import bus
        return f"{bus.host_id()}|99999999|0"

    def test_order_a_closed_predecessor_not_on_the_branch_holds_its_dependent(self):
        tid, kids = self.big()
        big.branch(self.p, c.find_brief(self.p, tid))
        self.close(kids["c1"])
        self.close(kids["c2"])
        out = self.run_json(tid)
        self.assertEqual(out["wave"], [])
        self.assertEqual([(x["id"], sorted(x["waits"])) for x in out["waiting"]],
                         [(kids["c3"], sorted([kids["c1"], kids["c2"]]))])
        text = self.fm("big", "run", tid).stdout
        self.assertIn(f"{kids['c1']} is closed but not on big/{tid} yet", text)
        r = self.fm("lane", "brief", kids["c3"], check=False)
        self.assertNotEqual(r.returncode, 0, "no lane is cut from a tip without its predecessors")
        self.assertIn(kids["c1"], r.stdout + r.stderr)
        self.land(tid, kids["c1"])
        self.land(tid, kids["c2"])
        out = self.run_json(tid)
        self.assertEqual([x["id"] for x in out["wave"]], [kids["c3"]])
        brief = self.brief_text(kids["c3"])
        self.assertIn(f"{kids['c1']}, {kids['c2']}", brief, "the brief names what the check proved is on the tip")
        self.assertIn(self.tip(tid), brief)

    def test_order_the_brief_claims_only_what_the_check_proved(self):
        tid, kids = self.big()
        self.run_json(tid)
        first = self.brief_text(kids["c1"])
        self.assertIn(f"big/{tid}", first)
        self.assertNotIn("comes after", first, "c1 comes after nothing: the brief says nothing of predecessors")
        self.assertNotIn("already on it", first)

    def test_order_a_dropped_predecessor_frees_its_dependent_and_is_not_counted_landed(self):
        tid, kids = self.big()
        big.branch(self.p, c.find_brief(self.p, tid))
        self.close(kids["c1"], "dropped")
        self.land(tid, kids["c2"])
        self.assertEqual([x["id"] for x in self.run_json(tid)["wave"]], [kids["c3"]], "a dropped child is free of")
        self.land(tid, kids["c3"])
        out = self.run_json(tid)
        self.assertTrue(out["all_landed"])
        self.assertEqual([x["id"] for x in out["dropped"]], [kids["c1"]])
        self.assertEqual(sorted(x["id"] for x in out["landed"]), sorted([kids["c2"], kids["c3"]]))
        self.assertIn("dropped", self.fm("big", "run", tid).stdout)

    def test_order_a_parent_whose_children_are_all_dropped_is_not_all_landed(self):
        tid, kids = self.big()
        for k in kids.values():
            self.close(k, "dropped")
        out = big.wave_state(self.p, c.find_brief(self.p, tid))
        self.assertFalse(out["all_landed"])

    def test_wave_line_with_the_briefs_given_loads_none(self):
        tid, kids = self.big()
        parent = c.find_brief(self.p, tid)
        briefs = c.load_briefs(self.p)
        with unittest.mock.patch.object(c, "load_briefs", side_effect=AssertionError("loaded every brief again")):
            line = big.wave_line(self.p, parent, briefs)
        self.assertIn("launch these 2 builders in one message", line)

    def test_wave_next_hands_the_briefs_it_holds_to_the_wave(self):
        tid, kids = self.big()
        self.fm("task", "set", tid, "--section", "Interpretation", "--text",
                "Reading 1: all three parts. Reading 2: only the parser; ruled out, the criteria name all three.")
        self.fm("task", "set", tid, "--section", "Risks and rollback", "--text", "a child breaks main: revert its merge")
        self.fm("task", "set", tid, "--section", "Build vs reuse", "--text", "reuses the parser; nothing else fits")
        self.fm("task", "set", tid, "approved=true")
        self.fm("focus", tid)
        seen = []
        real = big.wave_line
        with unittest.mock.patch.object(big, "wave_line", side_effect=lambda p, b, briefs=None: (seen.append(briefs),
                                                                                              real(p, b, briefs))[1]):
            c._next_for(self.p)
        self.assertTrue(seen and seen[0], "_next_for passes its own list of briefs")

    def test_wave_run_and_sync_inside_a_lane_are_refused(self):
        from sampo import cli
        tid, _ = self.big()
        inside = c.Project(self.p.slug, self.p.root, self.p.dir, lane=os.path.join(self.tmp, "a-lane"))
        for fn in (big.task_run, big.task_sync):
            with self.assertRaises(cli.UsageError) as got:
                fn(inside, argparse.Namespace(id=tid, json=True, dry_run=False))
            self.assertIn("run it from the main checkout", str(got.exception))

    def diverge(self, tid):
        """The integration branch and the build head each have a commit the other lacks."""
        big.branch(self.p, c.find_brief(self.p, tid))
        new =self.git("commit-tree", f"big/{tid}^{{tree}}", "-p", f"big/{tid}", "-m", "an edit on the branch")
        self.git("update-ref", f"refs/heads/big/{tid}", new)
        self.commit("later.txt", msg="work on the build branch")

    def test_sync_on_a_git_without_merge_tree_write_tree_says_so_and_does_not_call_it_a_conflict(self):
        tid, _ = self.big()
        self.diverge(tid)
        real = big._g

        def old(p, *a):
            if a and a[0] == "merge-tree":
                return subprocess.CompletedProcess(a, 129, "", "error: unknown option `write-tree'\n")
            return real(p, *a)
        with unittest.mock.patch.object(big, "_g", side_effect=old):
            with self.assertRaises(c.PolicyError) as got:
                big.task_sync(self.p, argparse.Namespace(id=tid, json=False))
        self.assertIn("2.38", str(got.exception))
        self.assertNotIn("conflict", str(got.exception).lower())

    def test_sync_conflict_text_names_commands_that_exist(self):
        tid, _ = self.big()
        self.commit("clash.txt", "base\n")
        big.branch(self.p, c.find_brief(self.p, tid))
        self.git("checkout", "-q", "-b", "side", f"big/{tid}")
        self.commit("clash.txt", "on the branch\n", msg="branch edit")
        self.git("update-ref", f"refs/heads/big/{tid}", self.git("rev-parse", "HEAD"))
        self.git("checkout", "-q", "-")
        self.commit("clash.txt", "on the build branch\n", msg="build edit")
        r = self.fm("big", "sync", tid, check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("git worktree add", r.stdout)
        self.assertIn("git merge", r.stdout)
        self.assertNotIn("on a lane", r.stdout)

    def test_the_zero_id_follows_the_repositorys_object_format(self):
        self.assertEqual(big._zero(self.p), "0" * 40)
        real = big._g

        def sha256(p, *a):
            if a[:2] == ("rev-parse", "--show-object-format"):
                return subprocess.CompletedProcess(a, 0, "sha256\n", "")
            return real(p, *a)
        with unittest.mock.patch.object(big, "_g", side_effect=sha256):
            self.assertEqual(big._zero(self.p), "0" * 64)

    def test_wave_a_child_whose_builder_died_is_a_problem_with_its_choices(self):
        tid, kids = self.big()
        self.lane_for(kids["c1"], "lane-c1", builder_proc=self.stopped_proc())
        st = big.wave_state(self.p, c.find_brief(self.p, tid))
        bad = {x["id"]: x for x in st["problems"]}
        self.assertIn(kids["c1"], bad)
        self.assertIn("process ended", bad[kids["c1"]]["reason"])
        self.assertTrue(any("lane recover" in ch for ch in bad[kids["c1"]]["choices"]))
        self.assertNotIn(kids["c1"], [x["id"] for x in st["ready"] + st["running"]])
        self.assertEqual([x["id"] for x in st["ready"]], [kids["c2"]], "its sibling goes on")

    def test_wave_a_stalled_child_is_a_problem_and_not_handed_out_again(self):
        tid, kids = self.big()
        self.lane_for(kids["c1"], "lane-c1")
        with unittest.mock.patch.object(lanes, "_stalled_ids", side_effect=lambda p, live: [x.id for x in live]):
            st = big.wave_state(self.p, c.find_brief(self.p, tid))
        bad = {x["id"]: x for x in st["problems"]}
        self.assertIn("stalled", bad[kids["c1"]]["reason"])
        self.assertNotIn(kids["c1"], [x["id"] for x in st["ready"]])

    def test_wave_a_fresh_lane_cut_from_a_branch_ahead_of_main_is_not_finished(self):
        tid, kids = self.big()
        big.branch(self.p, c.find_brief(self.p, tid))
        self.land(tid, kids["c1"])  # the integration branch is now ahead of main
        self.run_json(tid)
        tip = self.tip(tid)
        self.assertEqual(self.meta(kids["c2"]).get("big_base"), tip)
        lane = os.path.join(self.tmp, "lane-c2")
        self.git("worktree", "add", "-q", "--detach", lane, f"big/{tid}")
        b = c.find_brief(self.p, kids["c2"])
        b.meta.update({"lane": lane, "builder_proc": self.stopped_proc()})
        c.save_brief(self.p, b)
        briefs = c.load_briefs(self.p)
        self.assertNotIn(kids["c2"], c.finished_lanes(self.p, briefs, quiet_min=0, fresh=True),
                         "a lane with no commit of its own is not a finished one")
        working, waiting, _ = lanes._slots(self.p, briefs)
        self.assertIn(kids["c2"], working, "it still holds its slot")
        self.assertNotIn(kids["c2"], waiting)
        self.assertEqual(lanes.stop_causes(self.p, briefs)[kids["c2"]][0], "uncommitted")
        self.git("commit", "-q", "--allow-empty", "-m", "its own work", cwd=lane)  # a commit of its own: now it is
        briefs = c.load_briefs(self.p)
        self.assertIn(kids["c2"], c.finished_lanes(self.p, briefs, quiet_min=0, fresh=True))
        self.assertEqual(lanes.stop_causes(self.p, briefs)[kids["c2"]][0], "no_report")
