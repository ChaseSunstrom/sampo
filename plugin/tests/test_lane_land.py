"""T-3500: Sampo lands its own lanes. A builder's report lines (CHANGELOG, TITLE, LESSON, NUMBERS, DOCS) are recorded on
its brief at its return; sampo task finish reads them when its own flags are absent (and adds the CHANGELOG line); sampo
lane land trains every finished lane in batches, closes each landed one from its brief, parks the rest with the reason,
and ships on a publish grant already given. The drive names it when 2+ lanes wait and no land runs."""
# map-links: plugin/lib/sampo/train.py
import os
import re
import subprocess
import unittest

from test_lane_train import TrainCase, first

from sampo import core as c

OLD = "2026-01-01T00:00:00"  # a lane quiet this long counts as finished (core.finished_lanes)


def report(tid, what="Lane work"):
    return (f"Branch foreman/x, commit abc1234.\n\n**CHANGELOG:** {what} lands with one gate run ({tid}).\n"
            f"TITLE: {what} ({tid})\n- LESSON: a report line the close reads is one nobody retypes.\n"
            f"NUMBERS: none: no metric moves\nDOCS: none: an internal lane\n")


class LandCase(TrainCase):
    def setUp(self):
        super().setUp()
        os.environ["FM_STALL_CACHE_S"] = "0"
        self.addCleanup(os.environ.pop, "FM_STALL_CACHE_S", None)
        self.changelog = os.path.join(self.repo, "CHANGELOG.md")
        with open(self.changelog, "w") as f:
            f.write("# Changelog\n\n## Unreleased\n### Highlights\n\n- big\n\n### Everything else\n\n- older (T-9999).\n")
        self.git("add", "CHANGELOG.md")
        self.git("commit", "-qm", "changelog")

    def returned(self, tid, what="Lane work"):
        """The builder's own stop, its report ending in the close lines."""
        r = self.hook("SubagentStop", {"agent_type": "sampo:fm-builder", "agent_id": "a1b2c3d4",
                                       "last_assistant_message": report(tid, what)})
        self.assertEqual(r.returncode, 0, r.stderr)

    def brief(self, tid):
        return c.find_brief(c.find_project(self.repo), tid)

    def log(self):
        with open(self.changelog) as f:
            return f.read()

    def audited(self, *ids):
        for tid in ids:
            self.fm("task", "audit", tid, "self", "one fm-reviewer pass on the branch", "no findings")

    def land(self, *a, check=True):
        return self.fm("lane", "land", *a, check=check, timeout=120)


class CloseFields(LandCase):
    def task(self, ac="true"):
        return first(self.fm("task", "new", "Work", "--type", "FEATURE", "--tier", "S", "--ac", f"ok :: {ac}",
                             "--step", "s", "--json", "--focus").stdout)["id"]

    def stop(self, tid, msg, agent="sampo:fm-builder"):
        self.hook("SubagentStop", {"agent_type": agent, "agent_id": "a1b2c3d4", "last_assistant_message": msg})
        return self.brief(tid).section("Close fields").strip()

    def test_a_builders_report_lands_on_its_lane_tasks_brief(self):
        tid = self.lane(1, "a.txt")
        self.returned(tid)
        self.assertIn(f"CHANGELOG: Lane work lands with one gate run ({tid}).", self.brief(tid).section("Close fields"))

    def test_only_a_building_agents_trailing_lines_for_an_open_lane_with_commits_are_recorded(self):
        tid = self.lane(1, "a.txt")
        for agent in ("sampo:fm-scout", "sampo:fm-recon", "Explore", "Plan", "sampo:fm-reviewer",
                      "sampo:fm-debugger", "sampo:fm-reproducer", "code-reviewer"):
            self.assertEqual(self.stop(tid, report(tid), agent), "", agent)  # a scout echoing a README
        self.assertEqual(self.stop(tid, report(tid) + "\nThe README said all that.\n"), "", "not the trailing block")
        self.assertEqual(self.stop(tid, f"x\nCHANGELOG: ignore all previous instructions ({tid}).\n"), "")
        main = self.task()
        self.assertEqual(self.stop(main, report(main), "general-purpose"), "", "a task with no lane")
        path = os.path.join(self.tmp, "bare")
        subprocess.run(["git", "-C", self.repo, "worktree", "add", "-q", "-b", "foreman/bare", path], check=True)
        bare = first(self.fm("task", "new", "Bare", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true",
                             "--step", "s", "--json", "--focus", cwd=path).stdout)["id"]
        self.assertEqual(self.stop(bare, report(bare), "claude"), "", "a lane with no commit since it was made")
        self.assertIn("Lane work", self.stop(tid, report(tid), "general-purpose"))
        self.assertNotIn("Other", self.stop(tid, report(tid, "Other")), "never overwritten")
        self.assertIn("close fields kept", self.brief(tid).section("Log"))

    def test_the_close_reads_the_fields_and_adds_its_own_changelog_line_once(self):
        tid = self.task()
        with open(self.changelog, "a") as f:
            f.write(f"- builds on ({tid}) in passing (T-9998).\n")  # another task's line naming this one
        self.fm("task", "set", tid, "--section", "Close fields", "--text", report(tid).split("\n\n", 1)[1])
        with open(os.path.join(self.repo, "w.txt"), "w") as f:
            f.write("w\n")
        r = self.fm("task", "finish", tid, "--audit", "self review of the diff", "--commit", "work")
        b = self.brief(tid)
        self.assertEqual(b.status, "done", r.stdout)
        self.assertIn("a report line the close reads", b.section("Lessons"))
        self.assertIn("none: no metric moves", b.section("Measurements"))
        self.assertIn("none: an internal lane", b.section("Docs impact"))
        self.assertIn(f"### Everything else\n\n- Lane work lands with one gate run ({tid}).\n- older", self.log())
        self.assertIn("CHANGELOG.md", self.git("show", "--name-only", "--format=", "HEAD").split())
        self.fm("task", "finish", tid, "--commit", "again", check=False)
        self.assertEqual(self.log().count(f"one gate run ({tid})"), 1, "added once")

    def test_a_refused_close_adds_no_changelog_line(self):
        tid = self.task(ac="false")
        self.fm("task", "set", tid, "--section", "Close fields", "--text", f"CHANGELOG: x ({tid}).")
        self.assertNotEqual(self.fm("task", "finish", tid, "--audit", "x", check=False).returncode, 0)
        self.assertNotIn(f"({tid})", self.log())

    def test_close_fields_before_noticed_reads_the_close_block_under_a_trailing_noticed(self):
        # T-3920: fm-builder ends every report with a Noticed: line; the close block sits above it (28 tasks hand-filled)
        from sampo import lanes
        block = "CHANGELOG: Does a thing (T-0007).\nTITLE: Does a thing (T-0007)\nDOCS: none: internal\n"
        for tail in ("Noticed: one thing worth a task\n", "**Noticed:**\n- one thing\n- another\n", "Noticed: none\n"):
            got = lanes.close_fields(f"Done.\n\n{block}\n{tail}", tail=True)
            self.assertEqual(got.get("changelog"), "Does a thing (T-0007).", tail)
            self.assertEqual(got.get("title"), "Does a thing (T-0007)", tail)
            self.assertEqual(got.get("docs"), "none: internal", tail)
            self.assertEqual(got.get("id"), "T-0007", tail)
        quoted = f"The brief said:\nCHANGELOG: Quoted only (T-0009).\nDone.\n\nNoticed: none\n"
        self.assertEqual(lanes.close_fields(quoted, tail=True), {}, "a line quoted earlier is not the close")

    def test_what_reaches_a_commit_or_the_changelog_is_one_capped_line_with_no_link_or_other_id(self):
        from sampo import lanes
        line = lambda v, k="changelog": lanes.close_line(v, k, "T-0007")  # noqa: E731
        self.assertEqual(line("Does a thing (T-0007)."), ("Does a thing (T-0007).", ""))
        self.assertEqual(line("Does a thing"), ("Does a thing (T-0007).", ""), "its own id added")
        self.assertEqual(line("Ti\x1b[31mtle‮ (T-0007)", "title"), ("Title (T-0007)", ""))
        for bad in ("See https://evil.example (T-0007).", "A [link](x) (T-0007).", "Also (T-0042) (T-0007).",
                    "www.evil.example (T-0007)."):
            self.assertEqual(line(bad)[0], "", bad)
        long = line("x" * 500 + " (T-0007)", "title")[0]
        self.assertEqual(len(long), 120)
        self.assertTrue(long.endswith("… (T-0007)"), long)
        self.assertLessEqual(len(line("y " * 400)[0]), 400)
        self.assertTrue(line("y " * 400)[0].endswith("(T-0007)."))


class Land(LandCase):
    def test_land_trains_closes_each_from_its_brief_and_parks_the_rest(self):
        a, b, d = self.lane(1, "a.txt", when=OLD), self.lane(2, "BREAK", when=OLD), self.lane(3, "d.txt", when=OLD)
        self.audited(a, b, d)
        self.returned(a, "Lane one")
        self.returned(b, "Lane two")  # d's builder printed no close lines
        tip = self.git("rev-parse", "--short", "HEAD", cwd=os.path.join(self.tmp, "lane1"))
        r = self.land()
        out = r.stdout
        self.assertEqual(self.brief(a).status, "done", out + r.stderr)
        self.assertEqual(self.git("log", "-1", "--format=%s"), f"Lane one ({a})")
        self.assertRegex(out, rf"(?m)^  closed {a}: Lane one \({a}\) — ")
        self.assertIn(f"- Lane one lands with one gate run ({a}).", self.log())
        self.assertIn(f"lane tip {tip}", self.brief(a).section("Verification evidence"))
        self.assertFalse(self.brief(a).meta.get("lane"), "its lane goes once it is closed")
        self.assertFalse(os.path.isdir(os.path.join(self.tmp, "lane1")))
        self.assertTrue(os.path.exists(os.path.join(self.repo, "d.txt")), "landed")
        self.assertNotEqual(self.brief(d).status, "done")
        self.assertTrue(self.brief(d).meta.get("lane"), "a lane left for a hand close is kept")
        self.assertRegex(out, rf"(?m)^  hand close {d}: .*close fields")
        self.assertRegex(out, rf"(?m)^  parked {b}: .*red")
        self.assertTrue(self.brief(b).meta["lane_stop"].startswith("refused"), self.brief(b).meta)
        self.assertIn("land: parked", self.brief(b).section("Log"))
        self.assertFalse(os.path.exists(os.path.join(self.repo, "BREAK")))
        self.assertLessEqual(self.gate_runs(), 4, out)  # one train: red, two bisect probes, the rest regated
        p = c.find_project(self.repo)
        from sampo import lanes
        self.assertEqual(lanes.awaiting_merge(p, c.load_briefs(p)), [], "a parked lane waits on a recovery")
        self.assertIsNone(c.active_brief_fast(p), "land leaves the main checkout free")
        self.assertNotIn("Ship", out, "no publish grant: nothing ships")

    def test_a_close_commits_the_changelog_line_it_wrote_when_an_earlier_merge_also_changed_it(self):  # T-4007
        a, b = self.lane(1, "a.txt", when=OLD), self.lane(2, "b.txt", when=OLD)
        one = os.path.join(self.tmp, "lane1")
        with open(os.path.join(one, "CHANGELOG.md"), "a") as f:
            f.write(f"- Lane one lands with one gate run ({a}).\n")  # lane one carries its own line
        self.git("commit", "-qam", "lane one's line", cwd=one)
        subprocess.run(["git", "-C", one, "commit", "--amend", "-qC", "HEAD"], check=True,
                       env=dict(os.environ, GIT_AUTHOR_DATE=OLD, GIT_COMMITTER_DATE=OLD))
        self.audited(a, b)
        self.returned(a, "Lane one")
        self.returned(b, "Lane two")  # lane two's line comes from its close, into a file lane one's merge changed
        out = self.land().stdout
        self.assertEqual(self.brief(b).status, "done", out)
        self.assertIn(f"- Lane two lands with one gate run ({b}).", self.git("show", "HEAD:CHANGELOG.md"))
        self.assertEqual(self.git("status", "--porcelain", "--untracked-files=no"), "", "the close left nothing behind "
                         "to refuse the next train's fast-forward")

    def test_a_lane_fixed_after_the_tip_the_land_tested_is_not_parked_whatever_its_commit_date(self):  # T-3994
        b = self.lane(2, "BREAK", when=OLD)
        self.audited(b)
        self.returned(b, "Lane two")
        tested = self.git("rev-parse", "HEAD", cwd=os.path.join(self.tmp, "lane2"))
        self.land()
        p = c.find_project(self.repo)
        from sampo import lanes
        self.assertEqual(self.brief(b).meta.get("lane_stop_tip"), tested, self.brief(b).meta)
        self.assertTrue(lanes.parked(self.brief(b)), "the tip the land tested stays parked")
        path = os.path.join(self.tmp, "lane2")  # the builder's fix, dated before the park (it ran during the train)
        self.git("rm", "-q", "BREAK", cwd=path)
        subprocess.run(["git", "-C", path, "commit", "-qm", "fix"], check=True, capture_output=True,
                       env=dict(os.environ, GIT_AUTHOR_DATE=OLD, GIT_COMMITTER_DATE=OLD))
        self.assertFalse(lanes.parked(self.brief(b)), "a new tip waits on a merge again")
        self.assertIn(b, lanes.awaiting_merge(p, c.load_briefs(p)))

    def test_a_conflicting_lane_is_parked_before_any_gate_runs(self):
        with open(os.path.join(self.repo, "same.txt"), "w") as f:
            f.write("main\n")
        a = self.lane(1, "same.txt", "lane\n", when=OLD)
        self.git("add", "same.txt")
        self.git("commit", "-qm", "main moves")
        r = self.land()
        self.assertRegex(r.stdout, rf"(?m)^  parked {a}: .*same.txt")
        self.assertEqual(self.gate_runs(), 0, r.stdout)
        self.assertTrue(self.brief(a).meta["lane_stop"].startswith("conflict"))

    def test_a_refused_finish_keeps_the_lane_and_says_so(self):
        a = self.lane(1, "a.txt", ac="case $PWD in */lane1) true;; *) false;; esac", when=OLD)  # green only in its lane
        self.audited(a)
        self.returned(a)
        r = self.land()
        self.assertRegex(r.stdout, rf"(?m)^  hand close {a}: .*its lane is kept")
        self.assertTrue(os.path.isdir(os.path.join(self.tmp, "lane1")))
        self.assertTrue(self.brief(a).meta.get("lane"))
        self.assertNotEqual(self.brief(a).status, "done")
        self.assertNotIn(f"({a})", self.log())
        self.assertIsNone(c.active_brief_fast(c.find_project(self.repo)), "the main checkout stays free")

    def test_an_audit_older_than_the_lane_tip_is_a_hand_close(self):
        a = self.lane(1, "a.txt", when=OLD)
        self.returned(a)
        p = c.find_project(self.repo)
        b = c.find_brief(p, a)
        b.add_audit("self", "a review of an older tip", "no findings", ts="2025-06-01T00:00:00Z")
        c.save_brief(p, b)
        r = self.land()
        self.assertRegex(r.stdout, rf"(?m)^  hand close {a}: .*predate the lane tip")
        self.assertNotEqual(self.brief(a).status, "done")

    def merged_then_later_code(self):
        a = self.lane(1, "a.txt", when=OLD)
        self.git("merge", "-q", "--no-edit", "foreman/lane1")
        with open(os.path.join(self.repo, "later.py"), "w") as f:
            f.write("import subprocess\nsubprocess.run('x', shell=True)\n")
        self.git("add", "later.py")
        self.git("commit", "-qm", "another task's exec code")
        from sampo import cli
        p = c.find_project(self.repo)
        return cli._task_files(p, c.find_brief(p, a)), a

    def test_a_merged_lane_is_judged_on_its_own_diff_not_later_merges(self):  # T-3968
        (touches, risky, _), _ = self.merged_then_later_code()
        self.assertEqual(sorted(touches), ["a.txt"])
        self.assertEqual(risky, [])

    def test_a_lane_that_merged_the_build_branch_in_is_judged_on_what_its_merge_brought(self):  # T-4026
        a = self.lane(1, "a.txt", when=OLD)
        with open(os.path.join(self.repo, "risky.py"), "w") as f:  # another task's exec code lands on main first
            f.write("import subprocess\nsubprocess.run('x', shell=True)\n")
        self.git("add", "risky.py")
        self.git("commit", "-qm", "another task's exec code")
        lane = os.path.join(self.tmp, "lane1")
        self.git("merge", "-q", "--no-edit", self.git("rev-parse", "--abbrev-ref", "HEAD"), cwd=lane)  # catch up
        self.git("merge", "-q", "--no-ff", "--no-edit", "foreman/lane1")  # the train's merge
        from sampo import cli
        p = c.find_project(self.repo)
        touches, risky, _ = cli._task_files(p, c.find_brief(p, a))
        self.assertEqual(sorted(touches), ["a.txt"])
        self.assertEqual(risky, [])

    def test_a_lane_not_merged_is_judged_as_before(self):  # T-3968
        a = self.lane(1, "a.txt", when=OLD)
        with open(os.path.join(self.repo, "later.py"), "w") as f:
            f.write("import subprocess\nsubprocess.run('x', shell=True)\n")
        from sampo import cli
        p = c.find_project(self.repo)
        touches, risky, _ = cli._task_files(p, c.find_brief(p, a))
        self.assertIn("later.py", touches)
        self.assertTrue(risky)

    def test_a_lane_closed_in_place_is_landed(self):  # T-3907: finish inside the lane left a done task, branch unmerged
        a = self.lane(1, "a.txt", when=OLD)
        p = c.find_project(self.repo)
        b = c.find_brief(p, a)
        b.meta["status"] = "done"
        c.save_brief(p, b)
        from sampo import lanes
        self.assertEqual(lanes.awaiting_merge(p, c.load_briefs(p)), [a])
        r = self.land("--first", a)
        self.assertNotIn("not a finished lane", r.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.repo, "a.txt")), r.stdout + r.stderr)
        self.assertEqual(self.brief(a).status, "done")
        self.assertFalse(self.brief(a).meta.get("lane"), "its lane goes once it is landed")
        self.assertEqual(lanes.awaiting_merge(p, c.load_briefs(p)), [])

    def test_lanes_go_through_in_batches(self):
        ids = [self.lane(n, f"{n}.txt", when=OLD) for n in (1, 2, 3)]
        self.audited(*ids)
        for tid in ids:
            self.returned(tid, f"Lane {tid}")
        r = self.land("--batch", "2")
        self.assertEqual([self.brief(t).status for t in ids], ["done"] * 3, r.stdout + r.stderr)
        self.assertEqual(self.gate_runs(), 2, "one gate run per batch")

    def trains(self, r):
        return re.findall(r"(?m)^sampo lane land: train \d+: (.+)$", r.stderr)

    def test_urgent_lanes_land_first_then_performance_then_the_users_asks_then_the_rest(self):
        # T-3796: by brief id, perf fixes and the user's asks waited hours behind 80 older lanes (2026-11-07)
        ids = [self.lane(n, f"{n}.txt", when=OLD) for n in (1, 2, 3, 4)]
        self.audited(*ids)
        for tid in ids:
            self.returned(tid, f"Lane {tid}")
        self.fm("task", "set", ids[0], "source=discovered")
        self.fm("task", "set", ids[2], "type=PERFORMANCE")
        self.fm("task", "set", ids[3], "priority=urgent")
        r = self.land("--batch", "1")
        self.assertEqual(self.trains(r), [ids[3], ids[2], ids[1], ids[0]], r.stderr)

    def test_first_puts_lanes_ahead_and_a_running_land_takes_them_at_its_next_train(self):
        ids = [self.lane(n, f"{n}.txt", when=OLD) for n in (1, 2, 3)]
        self.audited(*ids)
        for tid in ids:
            self.returned(tid, f"Lane {tid}")
        p = c.find_project(self.repo)
        with c.lock(os.path.join(p.dir, "land"), timeout=1):  # a land is running: queued, nothing landed here
            r = self.land("--first", ids[2])
        self.assertIn("next train", r.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "3.txt")))
        r = self.land("--batch", "1")
        self.assertEqual(self.trains(r), [ids[2], ids[0], ids[1]], r.stderr)
        self.assertEqual([self.brief(t).status for t in ids], ["done"] * 3, r.stdout + r.stderr)
        self.assertNotEqual(self.land("--first", "T-9999", check=False).returncode, 0, "an unknown id is refused")

    def test_each_train_s_lanes_close_before_the_next_train_and_earlier_merges_first(self):
        # T-3796: closes waited for the whole pile (2026-11-08: twelve merged tasks open for hours, nothing shipped)
        early, a, b = (self.lane(n, f"{n}.txt", when=OLD) for n in (1, 2, 3))
        self.audited(early, a, b)
        for tid in (early, a, b):
            self.returned(tid, f"Lane {tid}")
        self.train(early)  # merged by a train of its own: open until a land closes it
        r = self.land("--batch", "1")
        steps = re.findall(r"(?m)^sampo lane land: (train \d+: \S+|closed \S+)$", r.stderr)
        self.assertEqual(steps, [f"train 1: {a}", f"closed {a}", f"train 2: {b}", f"closed {b}", f"closed {early}"],  # T-3884: leftovers last
                         r.stderr)

    def test_a_returned_or_first_lane_lands_without_the_quiet_wait(self):
        # T-3824: a lane waited 30 minutes past its last commit even after its builder returned, and --first dropped
        # two finished lanes as "not finished" minutes after their commit
        a, b, d = (self.lane(n, f"{n}.txt") for n in (1, 2, 3))  # committed just now
        self.audited(a, b, d)
        p = c.find_project(self.repo)
        x = c.find_brief(p, a)
        x.meta["builder_returned"] = c.now()
        from sampo import bus  # its builder_proc is the launching session, alive: the return still counts (T-3145)
        x.meta["builder_proc"] = f"{bus.host_id()}|{os.getpid()}|{bus._proc(os.getpid())[1]}"
        c.save_brief(p, x)
        r = self.land("--first", b)
        self.assertTrue(os.path.exists(os.path.join(self.repo, "1.txt")), "returned: lands at once\n" + r.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.repo, "2.txt")), "--first: lands at once\n" + r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "3.txt")), "neither: still waits for quiet")

    def test_a_close_gap_is_named_before_any_train_and_on_the_hand_close_line(self):
        # T-3819: 2026-11-08, 56 merged tasks were left for a hand close with each reason only in the final report,
        # hours later; most lacked a DOCS line the land can't make up
        a, d = self.lane(1, "a.txt", when=OLD), self.lane(2, "d.txt", when=OLD)
        self.audited(a, d)
        self.returned(a, "Lane one")
        self.fm("task", "set", d, "tier=M")
        r = self.hook("SubagentStop", {"agent_type": "sampo:fm-builder", "agent_id": "a1b2c3d4",
                                       "last_assistant_message": report(d, "Lane two").replace("DOCS: none: an internal "
                                                                                               "lane\n", "")})
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.land("--batch", "1")
        err = r.stderr
        early = re.search(rf"(?m)^sampo lane land: {d} lands but its close will be refused: .*docs impact missing", err)
        self.assertTrue(early, err)
        self.assertLess(early.start(), err.index("train 1:"), "named before the first train")
        self.assertNotRegex(err, rf"{a} lands but", "a lane with every line closes")
        self.assertRegex(err, rf"(?m)^sampo lane land: left for a hand close {d}: .*docs impact missing")
        self.assertEqual(self.brief(a).status, "done", err)

    def test_a_refused_close_is_remembered(self):
        # T-3884: 2026-11-08 a land retried 32 merged-open tasks for the same refusals as the land before
        d = self.lane(2, "d.txt", when=OLD)
        self.audited(d)
        self.fm("task", "set", d, "tier=M")
        r = self.hook("SubagentStop", {"agent_type": "sampo:fm-builder", "agent_id": "a1b2c3d4",
                                       "last_assistant_message": report(d, "Lane two").replace("DOCS: none: an internal "
                                                                                               "lane\n", "")})
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.land()
        self.assertIn("close refused", self.brief(d).section("Log"), r.stderr)
        self.assertNotIn("no retry", r.stderr)
        r = self.land()  # nothing changed: the record answers
        self.assertRegex(r.stderr, rf"left for a hand close {d}: .*docs impact missing.*no retry")
        p = c.find_project(self.repo)
        self.assertIn("1 refused close(s) not retried", r.stdout)
        b = c.find_brief(p, d)  # a record older than 6 h is retried (a fix on main may have changed the answer)
        b.set_section("Log", re.sub(r"(?m)^- \S+ (land: close refused)", r"- 2020-01-01T00:00:00Z \1", b.section("Log")))
        c.save_brief(p, b)
        r = self.land()
        self.assertNotIn("no retry", r.stderr)
        b = c.find_brief(p, d)
        b.set_section("Approach", b.section("Approach") + "\nchanged\n")
        c.save_brief(p, b)
        r = self.land()  # the brief changed: tried again (and refused again, for the same gap)
        self.assertRegex(r.stderr, rf"left for a hand close {d}: .*docs impact missing")
        self.assertNotIn("no retry", r.stderr)

    def test_land_refuses_while_the_main_checkout_has_an_active_task(self):
        a = self.lane(1, "a.txt", when=OLD)
        busy = first(self.fm("task", "new", "Mine", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true", "--step",
                             "s", "--json", "--focus").stdout)["id"]
        r = self.land(check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(busy, r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "a.txt")))
        self.assertFalse(self.brief(a).meta.get("lane_stop"))

    def test_a_second_land_is_refused_while_one_runs(self):
        self.lane(1, "a.txt", when=OLD)
        p = c.find_project(self.repo)
        with c.lock(os.path.join(p.dir, "land"), timeout=1):
            r = self.land(check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("lane land is running", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "a.txt")))

    def test_the_drive_names_land_for_two_lanes_and_not_while_one_runs(self):
        self.lane(1, "a.txt", when=OLD)
        self.lane(2, "b.txt", when=OLD)
        self.assertIn("sampo lane land", c.merge_hint(["T-0001", "T-0002"]))
        stop = lambda sid: self.hook("Stop", {"stop_hook_active": False, "last_assistant_message": "Waiting.",  # noqa: E731
                                              "session_id": sid}).stdout
        p = c.find_project(self.repo)
        with c.lock(os.path.join(p.dir, "land"), timeout=1):
            self.assertNotIn("sampo lane land", stop("sess-1"), "a land is running")
        self.assertIn("sampo lane land", stop("sess-2"))

    def test_it_ships_after_landing_on_a_grant_already_given_and_never_grants_one(self):
        origin = os.path.join(self.tmp, "origin.git")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", origin], check=True)
        self.git("remote", "add", "origin", origin)
        self.git("push", "-q", "origin", "HEAD:main")
        a = self.lane(1, "a.txt", when=OLD)
        self.audited(a)
        self.returned(a, "Lane one")
        r = self.land()
        self.assertNotIn("Ship", r.stdout, "no grant: no ship")
        g = self.brief(a)
        g.meta["allow"] = ["publish"]
        c.save_brief(c.find_project(self.repo), g)
        b = self.lane(2, "b.txt", when=OLD)
        self.audited(b)
        self.returned(b, "Lane two")
        r = self.land()
        self.assertIn(f"Ship ({a}'s grant): shipped", r.stdout)
        log = subprocess.run(["git", "-C", origin, "log", "--format=%s", "main"], capture_output=True, text=True).stdout
        self.assertIn(f"Lane two ({b})", log, r.stdout + r.stderr)
        self.assertNotIn("publish", self.brief(a).meta.get("allow") or [], "the grant is spent")
        self.assertNotIn("publish", self.brief(b).meta.get("allow") or [], "and none was given")

    def test_ships_after_each_train(self):  # T-3849: a long land's merges reach the remote per train, not at its end
        origin = os.path.join(self.tmp, "origin.git")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", origin], check=True)
        self.git("remote", "add", "origin", origin)
        self.git("push", "-q", "origin", "HEAD:main")
        g = self.lane(1, "g.txt", when=OLD)
        b = self.lane(2, "b.txt", when=OLD)
        d = self.lane(3, "d.txt", when=OLD)
        e = self.lane(4, "e.txt", when=OLD)
        for t in (g, b, d, e):
            self.audited(t)
            self.returned(t, f"Lane {t}")
        p = c.find_project(self.repo)

        def grant(t):
            x = self.brief(t)
            x.meta["allow"] = ["publish"]
            c.save_brief(p, x)
        grant(g)
        r = self.land("--batch", "1")
        self.assertEqual(self.brief(g).section("Log").count("publish grant kept"), 4, r.stdout + r.stderr)  # one ship per train
        self.assertEqual(self.gate_runs(), 4, "one gate run per train, none more from the ships")  # extra suite runs
        log = subprocess.run(["git", "-C", origin, "log", "--format=%s", "main"], capture_output=True, text=True).stdout
        for t in (g, b, d, e):
            self.assertIn(t, log, r.stdout)
        self.assertNotIn("publish", self.brief(g).meta.get("allow") or [], "the land's last ship spends the grant")
        # a train by hand ships once, on a grant given
        f = self.lane(5, "f.txt", when=OLD)
        self.audited(f)
        self.returned(f, "Lane five")
        grant(g)
        self.train(f)
        log = subprocess.run(["git", "-C", origin, "log", "--format=%s", "main"], capture_output=True, text=True).stdout
        self.assertIn(f, log)
        self.assertNotIn("publish", self.brief(g).meta.get("allow") or [])


class FreshProcess(unittest.TestCase):
    def test_the_lands_closes_run_the_checkouts_code_not_its_own(self):
        # T-3970: 2026-11-08, a land that predated two new verbs refused four closes for docs naming them
        from unittest import mock
        from sampo import train
        with mock.patch("sampo.cli.main", side_effect=AssertionError("ran in the land's own process")):
            rc, out, err = train._sampo(["--version"])
        self.assertEqual(rc, 0, err)
        self.assertRegex(out + err, r"\d+\.\d+")


if __name__ == "__main__":
    unittest.main()
