"""T-3500: Sampo lands its own lanes. A builder's report lines (CHANGELOG, TITLE, LESSON, NUMBERS, DOCS) are recorded on
its brief at its return; sampo task finish reads them when its own flags are absent (and adds the CHANGELOG line); sampo
lane land trains every finished lane in batches, closes each landed one from its brief, parks the rest with the reason,
and ships on a publish grant already given. The drive names it when 2+ lanes wait and no land runs."""
# map-links: plugin/lib/sampo/train.py
import contextlib
import os
import re
import subprocess
import sys
import time
import unittest
from unittest import mock

from test_lane_train import TrainCase, first

from sampo import cli
from sampo import core as c
from sampo import lanes
from sampo import train

OLD = "2026-01-01T00:00:00"  # a lane quiet this long counts as finished (core.finished_lanes)
CLEAN_CMD = "python3 plugin/tests/run.py " + " ".join(c.repo_shape_classes())  # a --hold run's command (train._held_classes)


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

    def lane(self, n, name, text=None, ac="true", when=None, tier="S", checked=True):
        """T-4767: a lane the land trains: the land holds one with no passing affected run at its tip, so each lane
        carries a clean one (the repo-shape classes held, run just now) unless `checked=False` (the hold's own tests)."""
        tid = super().lane(n, name, text=text, ac=ac, when=when, tier=tier)
        if checked:
            self.record_clean_run(tid)
        return tid

    def record_clean_run(self, tid, ts=None):
        p = c.find_project(self.repo)
        b = c.find_brief(p, tid)
        b.add_evidence(c.AFFECTED_LABEL + CLEAN_CMD, "✓ 10 tests", step=1, ran=True,
                       ts=ts or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        c.save_brief(p, b)

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

    def trains(self, r):
        """The lanes each train of a land took, one string per train, in order."""
        return re.findall(r"(?m)^sampo lane land: train \d+: (.+)$", r.stderr)

    @contextlib.contextmanager
    def in_project(self):
        """T-5368: code run in this process whose sampo verbs (train._sampo spawns them) find this test's project: its
        cwd and state folder, as fm() gives its own subprocesses."""
        env = {"FOREMAN_HOME": self.home, "FOREMAN_INHIBITED": "1", "FOREMAN_DOCTOR_BENCH": "0", "FOREMAN_DOCTOR_BUILD": "0"}
        here = os.getcwd()
        os.chdir(self.repo)
        try:
            with mock.patch.dict(os.environ, env):
                yield
        finally:
            os.chdir(here)


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
                      "sampo:fm-debugger", "sampo:fm-reproducer", "sampo:fm-fixer", "sampo:fm-resolver",
                      "code-reviewer"):
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

    def test_a_changelog_field_of_none_adds_no_line(self):  # T-4075: T-4073's 'none: …' became a CHANGELOG bullet
        for i, none in enumerate(("none: a repair, no code change", "None")):
            tid = self.task()
            self.fm("task", "set", tid, "--section", "Close fields", "--text", f"CHANGELOG: {none}\nDOCS: none: x")
            with open(os.path.join(self.repo, f"w{i}.txt"), "w") as f:
                f.write("w\n")
            self.fm("task", "finish", tid, "--audit", "self review of the diff", "--commit", "work")
            self.assertEqual(self.brief(tid).status, "done")
            self.assertNotIn(f"({tid})", self.log(), none)

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

    def test_a_red_base_stop_keeps_the_gates_own_lines(self):  # T-4029: six stops in a day named only the bench
        self.fm("check", "add", "test ! -e SLOW || { echo 'PreToolUse:Bash  172.7  150  OVER BUDGET'; false; }")
        with open(os.path.join(self.repo, "SLOW"), "w") as f:
            f.write("x\n")
        self.git("add", "SLOW")
        self.git("commit", "-qm", "red base")
        a = self.lane(1, "a.txt", when=OLD)
        self.audited(a)
        self.returned(a, "Lane one")
        out = self.land(check=False).stdout
        self.assertIn("Stopped: the base", out)
        self.assertRegex(out, r"Stopped: the base .*\n(    .*\n)*    .*OVER BUDGET",
                         "the red gate's own lines in the stop, so nobody reruns it to learn which hook")

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

    def test_a_first_lane_parked_on_a_conflict_says_so_and_lands_once_its_tip_moves_past_it(self):
        # T-4013: T-3989 was named --first, parked on a conflict, read "not a finished lane" and stayed parked all land
        with open(os.path.join(self.repo, "same.txt"), "w") as f:
            f.write("main\n")
        a = self.lane(1, "same.txt", "lane\n", when=OLD)
        self.git("add", "same.txt")
        self.git("commit", "-qm", "main moves")
        one, fix = os.path.join(self.tmp, "lane1"), os.path.join(self.tmp, "resolve.sh")
        with open(fix, "w") as f:  # b's gate resolves a's conflict while the land runs; its close runs it again.
            # The resolving commit is dated like the lane's own: a commit newer than the lane's audit makes the land
            # leave it for a fresh review (rightly), which this test met whenever the clock's second turned between
            # the audit and the gate (red beside its heavy neighbours and at a load of 45, green alone)
            f.write(f"printf 'main\\n' > {one}/same.txt; GIT_AUTHOR_DATE={OLD} GIT_COMMITTER_DATE={OLD} "
                    f"git -C {one} commit -qam resolve; true\n")
        b = self.lane(2, "b.txt", ac=f"sh {fix}", when=OLD)
        self.audited(a, b)
        for tid in (a, b):
            self.returned(tid, f"Lane {tid}")
        r = self.land("--first", a, "--batch", "1")
        self.assertRegex(r.stderr, rf"--first {a}: parked: conflicts with .*same.txt")
        self.assertEqual(self.trains(r), [b, a], r.stderr)
        self.assertEqual(self.brief(a).status, "done", r.stdout + r.stderr)

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

    def test_a_lane_merged_into_another_lane_is_judged_on_its_own_work(self):  # T-4062
        a = self.lane(1, "a.txt", when=OLD)
        self.lane(2, "b.txt", when=OLD)
        with open(os.path.join(self.repo, "risky.py"), "w") as f:  # another task's exec code lands on main first
            f.write("import subprocess\nsubprocess.run('x', shell=True)\n")
        self.git("add", "risky.py")
        self.git("commit", "-qm", "another task's exec code")
        main = self.git("rev-parse", "--abbrev-ref", "HEAD")
        self.git("merge", "-q", "--no-edit", main, cwd=os.path.join(self.tmp, "lane1"))  # lane1 catches up
        self.git("merge", "-q", "--no-edit", "foreman/lane1", cwd=os.path.join(self.tmp, "lane2"))  # lane2 takes it in
        self.git("merge", "-q", "--no-ff", "--no-edit", "foreman/lane2")  # only lane2's merge is on main's line
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

    def test_a_lane_whose_affected_run_predates_its_tip_is_held_and_the_fresh_ones_train(self):
        # T-3963: 2026-11-08's train left out three lanes whose builders' affected runs came before their last commit;
        # T-4767: such a lane is no longer trained last but held, with the line to rerun its check
        stale, fresh, none = (self.lane(1, "a.txt", when=OLD, checked=False), self.lane(2, "b.txt", when=OLD),
                              self.lane(3, "d.txt", when=OLD))
        self.run_stale(stale)
        r = self.land("--batch", "1")
        self.assertRegex(r.stderr, rf"(?m)^sampo lane land: {stale}: held: no passing affected check at its tip "
                                   rf"\(its newest affected run is older than its last commit\).*"
                                   rf"sampo lane accept {stale}")
        order = re.findall(r"train \d+: (T-\d+)", r.stderr)
        self.assertEqual(order, [fresh, none], "the stale lane does not train")
        self.assertFalse(os.path.exists(os.path.join(self.repo, "a.txt")), "held")
        self.assertTrue(os.path.exists(os.path.join(self.repo, "b.txt")), "landed")

    def run_stale(self, tid):
        p = c.find_project(self.repo)
        b = c.find_brief(p, tid)
        b.add_evidence(c.AFFECTED_LABEL + "python3 -m pytest -q x", "✓ ok", step=1, ts="2025-06-01T00:00:00Z", ran=True)
        c.save_brief(p, b)

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
        # T-3884: leftovers after the train's own closes; T-3969: at the first boundary, not after the last train.
        # T-5368: the closes run on their own worker, so train 2 need not wait for them: only each close's own order stands
        self.assertEqual(sorted(steps), sorted([f"train 1: {a}", f"closed {a}", f"closed {early}", f"train 2: {b}",
                                                f"closed {b}"]), r.stderr)
        at = {s: i for i, s in enumerate(steps)}
        self.assertEqual(steps[0], f"train 1: {a}", r.stderr)
        self.assertLess(at[f"closed {a}"], at[f"closed {early}"], "a train's own lanes close before the leftovers")
        self.assertLess(at[f"train 2: {b}"], at[f"closed {b}"], r.stderr)

    def test_a_refused_leftover_is_tried_once_per_land_until_its_brief_changes(self):
        # T-3969: 2026-11-08, 63 merged-open tasks prepped for their closes waited for the land run's end (hours)
        early, a, b = (self.lane(n, f"{n}.txt", when=OLD) for n in (1, 2, 3))
        self.audited(early, a, b)
        self.fm("task", "set", early, "tier=M")
        r = self.hook("SubagentStop", {"agent_type": "sampo:fm-builder", "agent_id": "a1b2c3d4",
                                       "last_assistant_message": report(early, "Early").replace(
                                           "DOCS: none: an internal lane\n", "")})
        self.assertEqual(r.returncode, 0, r.stderr)
        for tid in (a, b):
            self.returned(tid, f"Lane {tid}")
        self.train(early)
        r = self.land("--batch", "1")
        tried = re.findall(rf"(?m)^sampo lane land: left for a hand close {early}: .*$", r.stderr)
        self.assertEqual(len(tried), 1, r.stderr)
        self.assertIn("docs impact missing", tried[0])
        self.assertGreater(r.stderr.index(tried[0]), r.stderr.index("train 1:"), "tried after the first train's boundary")

    def test_a_live_projects_close_check_skips_classes_green_at_the_same_code(self):
        # T-5483: the hourly tick's land --merged ran all 12,000 tests uncached, queued behind the behind verify on the
        # same code; a live project's verify already runs cached, so the closes' one check does too
        seen = os.path.join(self.tmp, "cache-env")
        self.fm("check", "add", f'echo "${{FOREMAN_CACHE_FULL:-${{SAMPO_CACHE_FULL:-unset}}}}" >> {seen}')
        outs = []
        for n, mode in ((1, "train"), (2, "live")):
            p = c.find_project(self.repo)
            c.write_meta(p, {**c.read_meta(p), "train_mode": mode})
            tid = self.lane(n, f"{n}.txt", when=OLD)
            self.audited(tid)
            self.returned(tid, f"Lane {n}")
            self.git("merge", "--no-ff", "-q", "-m", f"merge lane {n}", f"foreman/lane{n}")
            r = self.fm("lane", "land", "--merged", check=False, timeout=120)
            outs.append(r.stdout + r.stderr)
            self.assertTrue(os.path.exists(seen), outs[-1])
        with open(seen) as f:
            self.assertEqual(f.read().split(), ["unset", "1"], "\n".join(outs))

    def test_a_closed_tasks_merged_lane_is_removed_by_the_land(self):
        # T-3969: 19 lanes of done, merged tasks stood on disk until removed by hand (2026-11-08)
        x, y = self.lane(1, "x.txt", when=OLD), self.lane(2, "y.txt", when=OLD)
        self.audited(x, y)
        self.returned(y)
        self.train(x)
        p = c.find_project(self.repo)
        b = c.find_brief(p, x)
        b.meta["status"] = "done"
        c.save_brief(p, b)
        self.assertTrue(os.path.isdir(os.path.join(self.tmp, "lane1")))
        r = self.land()
        self.assertFalse(os.path.isdir(os.path.join(self.tmp, "lane1")), r.stdout + r.stderr)
        self.assertFalse(self.brief(x).meta.get("lane"), r.stderr)
        self.assertEqual(self.brief(x).status, "done")

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

    def test_a_missing_proof_gap_is_not_told_to_go_in_the_close_fields(self):
        # T-4209: the FIX proof is recorded in the lane (sampo task prove), not a Close field
        d = self.lane(2, "d.txt", when=OLD)
        self.audited(d)
        self.fm("task", "set", d, "type=FIX")
        r = self.hook("SubagentStop", {"agent_type": "sampo:fm-builder", "agent_id": "a1b2c3d4",
                                       "last_assistant_message": report(d, "Lane two")})
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.land("--batch", "1")
        line = re.search(rf"(?m)^sampo lane land: {d} lands but its close will be refused: .*$", r.stderr)
        self.assertTrue(line, r.stderr)
        self.assertIn("no red→green proof", line.group(0))
        self.assertNotRegex(line.group(0), r"no red→green proof[^;]*\(add the lines to its Close fields")

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
        self.assertRegex(self.land().stderr, rf"left for a hand close {d}: .*no retry")
        b = c.find_brief(p, d)  # T-4080: Sampo's own code changed since (T-4061 fixed T-3671's cause; it waited 6 h)
        b.set_section("Log", re.sub(r"(?m)^(- \S+ land: close refused: .* @ \w+ \w*) \w+$", r"\1 0000beef",
                                    b.section("Log")))
        c.save_brief(p, b)
        self.assertNotIn("no retry", self.land().stderr)
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
        with open(os.path.join(self.repo, "a.txt"), "w") as f:  # T-4692: held by uncommitted work in a lane's file
            f.write("mine\n")
        r = self.land(check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(busy, r.stderr)
        self.assertEqual(open(os.path.join(self.repo, "a.txt")).read(), "mine\n", "the focused task's file is kept")
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


PROBE = ("import fcntl, os, sys\n"
         "d, out = sys.argv[1], sys.argv[2]\n"
         "if os.path.basename(os.getcwd()).startswith('lane'):\n"  # the land's check in the lane's folder, under its train
         "    sys.exit(0)\n"
         "fd = os.open(os.path.join(d, '.lock'), os.O_CREAT | os.O_RDWR)\n"
         "try:\n"
         "    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
         "    state = 'free'\n"
         "except BlockingIOError:\n"
         "    state = 'held'\n"
         "if os.path.exists(os.path.join(d, 'closing')):\n"
         "    state = 'closing'\n"
         "open(out, 'a').write(state + '\\n')\n"
         "sys.exit(0 if state == 'free' else 1)\n")


class CloseLock(LandCase):
    """T-5368: a land's close ran its criteria (and the changelog commit) with the train lock held, so a train waited,
    and a hand close waited on a train, for up to 50 minutes of someone else's tests. The lock is held for the commit."""

    def test_a_lands_close_holds_the_train_lock_only_around_its_commit_not_while_its_criteria_run(self):  # lock
        p = c.find_project(self.repo)
        probe, out = os.path.join(self.tmp, "probe.py"), os.path.join(self.tmp, "probed")
        with open(probe, "w") as f:
            f.write(PROBE)
        tid = self.lane(1, "a.txt", when=OLD, ac=f"python3 {probe} {os.path.join(p.dir, 'train')} {out}")
        self.audited(tid)
        self.returned(tid, "Lane 1")
        r = self.land()
        states = []
        if os.path.exists(out):
            with open(out) as f:
                states = f.read().split()
        self.assertTrue(states, "the criterion's command ran at the close: " + r.stdout + r.stderr)
        self.assertEqual(set(states), {"free"}, f"the lock was not free while the criteria ran: {states}\n{r.stdout}")
        self.assertEqual(self.brief(tid).status, "done", r.stdout + r.stderr)
        self.assertIn("Lane 1 lands with one gate run", self.log(), "the changelog line is committed under the lock")
        self.assertEqual(self.git("status", "--porcelain", "--untracked-files=no"), "")
        self.assertFalse(os.path.exists(os.path.join(p.dir, "train", "closing")), "its mark is cleared")

    def test_a_close_whose_commit_waits_for_the_train_lock_keeps_its_proof_and_the_next_land_commits_it(self):
        p = c.find_project(self.repo)
        tid = self.lane(1, "a.txt", when=OLD)
        self.audited(tid)
        self.returned(tid, "Lane 1")
        self.train(tid)
        with c.lock(os.path.join(p.dir, "train"), timeout=1), mock.patch.object(lanes, "TRUNK_WAIT_S", 0.2), \
                mock.patch.object(train, "_sampo", wraps=train._sampo) as sampo, self.in_project():
            why, title = train._close(p, self.repo, tid)
        finishes = [call.args[0] for call in sampo.call_args_list if call.args and call.args[0][:2] == ["task", "finish"]]
        self.assertIn("train", why, why)
        self.assertEqual(title, "")
        self.assertEqual(len(finishes), 1, finishes)
        self.assertNotIn("--commit", finishes[0], "the criteria ran with no commit, so no lock")
        self.assertEqual(self.brief(tid).status, "done", "its proof is kept")
        self.assertNotIn("Lane 1 lands with one gate run", self.log(), "nothing written to trunk beside the train")
        self.assertEqual(self.git("status", "--porcelain", "--untracked-files=no"), "", "nothing half-written on trunk")
        with self.in_project():
            why, title = train._close(p, self.repo, tid)  # the lock is free: the next land commits what was proved
        self.assertEqual(why, "")
        self.assertTrue(title)
        self.assertIn("Lane 1 lands with one gate run", self.log())
        self.assertEqual(self.git("status", "--porcelain", "--untracked-files=no"), "")
        self.assertFalse(os.path.isdir(os.path.join(self.tmp, "lane1")), "its lane goes with the commit")

    def landed(self):
        tid = self.lane(1, "a.txt", when=OLD)
        self.audited(tid)
        self.returned(tid, "Lane 1")
        self.train(tid)
        return tid

    def test_a_crash_right_after_the_proof_leaves_the_commit_owed_and_the_merged_sweep_commits_it(self):  # T-5368 review
        p = c.find_project(self.repo)
        tid = self.landed()
        real = train._sampo

        def dies_after_finish(args, *a, **k):  # the process is gone once finish --defer-changelog has returned
            out = real(args, *a, **k)
            if args[:2] == ["task", "finish"] and "--defer-changelog" in args:
                raise RuntimeError("killed")
            return out
        with mock.patch.object(train, "_sampo", dies_after_finish), self.in_project():
            with self.assertRaises(RuntimeError):
                train._close(p, self.repo, tid)
        b = self.brief(tid)
        self.assertEqual(b.status, "done")
        self.assertTrue(train._commit_pending(b), "the finish wrote the owed commit with the done: " + b.section("Log"))
        self.assertNotIn("Lane 1 lands with one gate run", self.log())
        self.assertEqual([t for t, _ in train._merged_lanes(p, self.repo)[0]], [tid], "a done task owing its commit is swept")
        r = self.land("--merged")
        self.assertIn(f"closed {tid}", r.stdout + r.stderr)
        self.assertIn("Lane 1 lands with one gate run", self.log())
        self.assertFalse(train._commit_pending(self.brief(tid)))

    def test_a_close_whose_trunk_moved_mid_run_runs_once_more(self):  # T-5368 review
        p = c.find_project(self.repo)
        tid = self.landed()
        real, calls = train._sampo, []

        def moves_once(args, *a, **k):
            if args[:2] == ["task", "finish"] and "--defer-changelog" in args:
                calls.append(1)
                if len(calls) == 1:
                    return 1, "", f"sampo: refused: {tid} not finished: {cli.TREE_MOVED}; nothing recorded"
            return real(args, *a, **k)
        with mock.patch.object(train, "_sampo", moves_once), self.in_project():
            why, title = train._close(p, self.repo, tid)
        self.assertEqual((why, len(calls)), ("", 2), why)
        self.assertEqual(self.brief(tid).status, "done")

    def test_a_close_whose_trunk_keeps_moving_is_left_for_the_next_land_with_no_refusal_logged(self):  # T-5368 review
        p = c.find_project(self.repo)
        tid = self.landed()
        real, calls = train._sampo, []

        def always_moves(args, *a, **k):
            if args[:2] == ["task", "finish"] and "--defer-changelog" in args:
                calls.append(1)
                return 1, "", f"sampo: refused: {tid} not finished: {cli.TREE_MOVED}; nothing recorded"
            return real(args, *a, **k)
        with mock.patch.object(train, "_sampo", always_moves), self.in_project():
            why, title = train._close(p, self.repo, tid)
        # one retry layer: the first run and at most CLOSE_RETRIES (2) reruns, so a moving trunk costs the criteria 3 runs
        self.assertEqual(len(calls), 1 + train.CLOSE_RETRIES, "reruns are bounded in all, not per layer")
        self.assertEqual(train.CLOSE_RETRIES, 2)
        self.assertIn(cli.TREE_MOVED, why)
        self.assertNotIn("close refused", self.brief(tid).section("Log"), "no verdict on the task: the next land retries")
        self.assertNotEqual(self.brief(tid).status, "done")

    def test_the_pending_mark_is_the_whole_log_text_not_a_line_that_merely_ends_with_it(self):  # T-5368 review
        from types import SimpleNamespace
        def brief(*lines):
            return SimpleNamespace(section=lambda name: "\n".join(lines))
        mark = c.CLOSE_PENDING
        self.assertTrue(train._commit_pending(brief(f"- 2026-01-01T00:00:00Z {mark}")))
        self.assertFalse(train._commit_pending(brief(f"- 2026-01-01T00:00:00Z the note said {mark}")))
        self.assertFalse(train._commit_pending(brief(f"- 2026-01-01T00:00:00Z {mark}", f"- 2026-01-02T00:00:00Z {c.CLOSE_COMMITTED}")))


class FreshProcess(unittest.TestCase):
    def test_the_lands_closes_run_the_checkouts_code_not_its_own(self):
        # T-3970: 2026-11-08, a land that predated two new verbs refused four closes for docs naming them
        from unittest import mock
        from sampo import train
        with mock.patch("sampo.cli.main", side_effect=AssertionError("ran in the land's own process")):
            rc, out, err = train._sampo(["--version"])
        self.assertEqual(rc, 0, err)
        self.assertRegex(out + err, r"\d+\.\d+")


class Restart(LandCase):
    """T-3973: a land that merged a change to Sampo's own modules goes on as a fresh process on the new code."""
    def two(self):
        ids = [self.lane(n, f"{n}.txt", when=OLD) for n in (1, 2)]
        self.audited(*ids)
        for tid in ids:
            self.returned(tid, f"Lane {tid}")
        return ids

    def stamps(self, *seq):  # the code stamp as _land reads it: one per call, the last one repeated
        from unittest import mock
        n = []
        return mock.patch("sampo.train._code_stamp", side_effect=lambda: seq[min(len(n.append(0) or n) - 1, len(seq) - 1)])

    def cmd(self, background=True):
        """cmd_land run here with os.execv patched: (the execv mock, what it printed)."""
        import argparse
        import contextlib
        import io
        from unittest import mock

        from sampo import train
        if background:
            os.environ.pop("FOREMAN_NO_BACKGROUND", None)
            self.addCleanup(os.environ.__setitem__, "FOREMAN_NO_BACKGROUND", "1")
        out = io.StringIO()
        with mock.patch("os.execv") as ex, contextlib.redirect_stdout(out):
            train.cmd_land(self.p, argparse.Namespace(batch=1, first=None, bg=False, json=False), self.repo)
        return ex, out.getvalue()

    def setUp(self):
        super().setUp()
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.repo)  # the close runs `sampo` from the cwd: the test's repo, not this checkout
        self.p = c.find_project(self.repo)

    def test_changed_modules_with_lanes_still_ready_restart_the_land_and_exec_a_fresh_one(self):
        from sampo import train
        a, b = self.two()
        x = self.brief(b)
        x.meta["allow"] = ["publish"]
        c.save_brief(self.p, x)
        from unittest import mock
        with self.stamps("old", "new"), mock.patch("sampo.train._sampo", return_value=(0, "shipped", "")):
            got = train.land(self.p, self.repo, 1)
        self.assertTrue(got["restart"])
        self.assertIn("publish", self.brief(b).meta.get("allow") or [], "a restart never spends the grant: its successor ships")
        self.assertEqual((got["trains"], got["closed"]), (1, [a]))
        self.assertFalse(os.path.exists(os.path.join(self.repo, "2.txt")), "the second lane waits for the new code")
        self.assertEqual(train.first_ids(self.p), [b], "the lanes still ready keep their order for the restarted land")
        d = self.lane(3, "3.txt", when=OLD)  # b still waits, d too: the next land's cmd_land execs after its train
        self.audited(d)
        self.returned(d, "Lane 3")
        with self.stamps("old", "new"):
            ex, out = self.cmd()
        self.assertIn("Restarting on new code", out)
        argv = ex.call_args.args[1]
        self.assertEqual(argv[-4:], ["lane", "land", "--batch", "1"], "the same land, its batch kept")
        self.assertNotIn("--first", argv)
        self.assertEqual((self.brief(b).status, self.brief(d).status == "done"), ("done", False), "d waits for the exec")

    def test_no_change_or_nothing_left_never_restarts_and_no_background_skips_the_exec(self):
        from sampo import train
        a, b = self.two()
        with self.stamps("old"):  # unchanged: both lanes land, no restart
            got = train.land(self.p, self.repo, 1)
        self.assertEqual((got["restart"], got["trains"]), (False, 2))
        c_ = self.lane(3, "3.txt", when=OLD)
        self.audited(c_)
        self.returned(c_, "Lane 3")
        with self.stamps("old", "new"):  # changed by the last train: nothing ready, so nothing to restart for
            self.assertFalse(train.land(self.p, self.repo, 1)["restart"])
        d = self.lane(4, "4.txt", when=OLD)
        e = self.lane(5, "5.txt", when=OLD)
        self.audited(d, e)
        for tid in (d, e):
            self.returned(tid, f"Lane {tid}")
        with self.stamps("old", "new"):  # FOREMAN_NO_BACKGROUND is set (the tests'): said, never exec'd
            ex, out = self.cmd(background=False)
        ex.assert_not_called()
        self.assertIn("restarts on new code", out)


class ParkLines(LandCase):
    """T-4252: a lane a train leaves out is said as it happens, and --first names the state a lane is in."""
    def test_parked_line_prints_mid_land_before_the_next_train_with_its_reason(self):
        # 2026-11-11: T-4221 left train 1 at 04:55 and its line came only in the summary, an hour later
        a, b, d = self.lane(1, "a.txt", when=OLD), self.lane(2, "BREAK", when=OLD), self.lane(3, "d.txt", when=OLD)
        self.audited(a, b, d)
        for tid, what in ((a, "Lane one"), (b, "Lane two"), (d, "Lane three")):
            self.returned(tid, what)
        r = self.land("--batch", "1")
        park = re.search(rf"(?m)^sampo lane land: parked {b}: its merge turns the gates red: .+$", r.stderr)
        self.assertTrue(park, r.stderr)
        self.assertLess(r.stderr.index(f"train 2: {b}"), park.start(), "after the train that left it out")
        self.assertLess(park.start(), r.stderr.index(f"train 3: {d}"), "before the next train starts")
        self.assertRegex(r.stdout, rf"(?m)^  parked {b}: ", "the summary keeps its list")
        self.assertEqual(self.brief(d).status, "done", r.stdout + r.stderr)

    def test_parked_line_prints_for_a_lane_left_out_of_a_train_of_two(self):
        a, b = self.lane(1, "a.txt", when=OLD), self.lane(2, "BREAK", when=OLD)
        self.audited(a, b)
        self.returned(a, "Lane one")
        self.returned(b, "Lane two")
        r = self.land()
        self.assertRegex(r.stderr, rf"(?m)^sampo lane land: parked {b}: its merge turns the gates red")
        self.assertEqual(self.brief(a).status, "done", r.stdout + r.stderr)

    def test_first_names_a_lane_parked_in_this_land_with_its_why(self):
        with open(os.path.join(self.repo, "same.txt"), "w") as f:
            f.write("main\n")
        a = self.lane(1, "same.txt", "lane\n", when=OLD)
        self.git("add", "same.txt")
        self.git("commit", "-qm", "main moves")
        self.audited(a)
        self.returned(a, "Lane one")
        r = self.land("--first", a)
        self.assertRegex(r.stderr, rf"--first {a}: parked: conflicts with .*same.txt", r.stdout + r.stderr)
        self.assertNotIn("can't be told", r.stderr)

    def test_first_names_the_state_of_a_lane_not_waiting_on_a_merge(self):
        # 2026-11-11: --first on T-4221 said only "merged, parked or still building": three states, none chosen
        a = self.lane(1, "a.txt", when=OLD)
        self.audited(a)
        self.returned(a, "Lane one")
        self.land()
        self.assertRegex(self.land("--first", a).stderr, rf"--first {a}: merged already")
        b = self.lane(2, "BREAK", when=OLD)
        self.audited(b)
        self.returned(b, "Lane two")
        self.land()
        self.assertRegex(self.land("--first", b).stderr, rf"--first {b}: parked by an earlier land \(its last merge: refused\)")
        c_ = self.lane(3, "c.txt")  # committed just now: not quiet yet, and its builder runs
        p = c.find_project(self.repo)
        from sampo import bus
        x = c.find_brief(p, c_)
        x.meta["builder_proc"] = f"{bus.host_id()}|{os.getpid()}|{bus._proc(os.getpid())[1]}"
        c.save_brief(p, x)
        self.assertRegex(self.land("--first", c_).stderr, rf"--first {c_}: still building")
        t = first(self.fm("task", "new", "Plain", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true",
                          "--step", "s", "--json").stdout)["id"]
        self.assertRegex(self.land("--first", t).stderr, rf"--first {t}: not a lane")

    def test_first_a_workflow_lane_accepted_after_its_last_commit_is_not_still_building(self):
        # T-5344: builder_proc is the launching session, alive for days, with no builder_returned: the accept is what
        # finishes the lane (seven sat "still building" on 2026-11-19); a commit after the accept makes it wait again
        a = self.lane(1, "a.txt")  # committed just now: not quiet
        p = c.find_project(self.repo)
        from sampo import bus
        x = c.find_brief(p, a)
        x.meta["builder_proc"] = f"{bus.host_id()}|{os.getpid()}|{bus._proc(os.getpid())[1]}"
        x.meta["accepted_at"] = round(time.time() - 3600, 1)  # an hour before its commit
        c.save_brief(p, x)
        self.assertRegex(self.land("--first", a).stderr, rf"--first {a}: still building")
        x = c.find_brief(p, a)
        x.meta["accepted_at"] = round(time.time() + 5, 1)
        c.save_brief(p, x)
        self.assertNotRegex(self.land("--first", a).stderr, "still building")


class ShipAndFirst(LandCase):
    """T-4252 review: a land that asks a detached ship after a train and also takes a --first id in the same run. The
    land's --first set and the ship request's flag were both named `asked` in _land(): the request replaced the set with
    True and the next loop's `asked.update(jump)` crashed the land."""
    def setUp(self):
        super().setUp()
        os.environ.pop("FOREMAN_NO_BACKGROUND", None)  # SampoTestCase restores it: the ship is a detached process here

    def tearDown(self):
        try:
            end = time.monotonic() + 60
            while train._ship_alive(c.find_project(self.repo)) and time.monotonic() < end:
                time.sleep(0.1)
        finally:
            super().tearDown()

    def test_a_ship_asked_after_a_train_and_a_first_id_taken_in_one_land_do_not_clash(self):
        a, b = self.lane(1, "a.txt", when=OLD), self.lane(2, "b.txt", when=OLD)
        self.audited(a, b)
        self.returned(a, "Lane one")
        self.returned(b, "Lane two")
        p = c.find_project(self.repo)
        x = self.brief(a)
        x.meta["allow"] = ["publish"]  # the grant a detached ship spends
        c.save_brief(p, x)
        r = self.fm("lane", "land", "--first", b, "--batch", "1", env={"SAMPO_LAND_SHIP_WAIT_S": "4"}, timeout=240)
        out = r.stdout + r.stderr
        self.assertNotIn("AttributeError", out)
        self.assertEqual(r.returncode, 0, out)
        self.assertIn("sampo lane land: ship ", out, "a ship was asked for after the first train")
        self.assertEqual(self.brief(a).status, "done", out)
        self.assertEqual(self.brief(b).status, "done", out)


class AuditSurvives(LandCase):
    """T-4487: an audit taken at a lane's own tip still counts at the land's close after another lane's close has
    written to main (its CHANGELOG line, which no merge message names). What decides staleness is the lane's own files."""

    def audit_at_tip(self, tid, n, lenses):
        p = c.find_project(self.repo)
        x = c.find_brief(p, tid)
        for lens in lenses:  # what accept records: the lane's own working tree
            x.add_audit(lens, "read the diff", "no findings", tree=c.worktree_id(os.path.join(self.tmp, f"lane{n}")))
        c.save_brief(p, x)

    def blockers(self, tid):
        """The audit gaps the close judges b's audits by, against main as it stands now (the merge-aware tree)."""
        p = c.find_project(self.repo)
        x = c.find_brief(p, tid)
        return x.audit_blockers(tree=c.audit_tree_past_merges(p, x, c.worktree_id(self.repo)))

    def land_lane_one_then_lane_two(self):
        """Main as a's close leaves it (its merge, then its CHANGELOG line), then b's land merges b's audited tip."""
        self.git("merge", "-q", "--no-ff", "--no-edit", "foreman/lane1")
        with open(os.path.join(self.repo, "CHANGELOG.md"), "a") as f:
            f.write("- Lane one\n")
        self.git("commit", "-qam", "CHANGELOG: lane one")
        self.git("merge", "-q", "--no-ff", "--no-edit", "foreman/lane2")

    def test_audit_survives_another_lanes_close_on_main(self):  # T-4487
        a = self.lane(1, "a.txt", when=OLD)
        b = self.lane(2, "b.txt", when=OLD, tier="M")
        self.audit_at_tip(b, 2, ("intent", "edge"))
        self.land_lane_one_then_lane_two()
        self.assertEqual(self.blockers(b), [], "main gained a.txt and CHANGELOG.md, not b's files: b's audits still count")

    def test_audit_survives_is_not_granted_once_the_lanes_own_file_changes(self):  # T-4487
        a = self.lane(1, "a.txt", when=OLD)
        b = self.lane(2, "b.txt", when=OLD, tier="M")
        self.audit_at_tip(b, 2, ("intent", "edge"))
        with open(os.path.join(self.tmp, "lane2", "b.txt"), "w") as f:  # the lane's own file, edited after the audit
            f.write("edited after the audit\n")
        self.git("commit", "-qam", "edit after the audit", cwd=os.path.join(self.tmp, "lane2"))
        self.land_lane_one_then_lane_two()
        self.assertTrue(any("files changed since the audit" in x for x in self.blockers(b)), self.blockers(b))

    def test_a_new_file_the_lane_writes_after_its_audit_stales_it(self):  # T-4487: no touch was recorded for it
        a = self.lane(1, "a.txt", when=OLD)
        b = self.lane(2, "b.txt", when=OLD, tier="M")
        self.audit_at_tip(b, 2, ("intent", "edge"))
        lane2 = os.path.join(self.tmp, "lane2")
        with open(os.path.join(lane2, "new.txt"), "w") as f:  # a script's file in the lane, no Edit hook saw it
            f.write("written by a shell command\n")
        p = c.find_project(lane2)
        x = c.find_brief(p, b)
        blockers = x.audit_blockers(tree=c.audit_tree_past_merges(p, x, c.worktree_id(lane2)))
        self.assertTrue(any("files changed since the audit" in y for y in blockers), blockers)


class UncheckedLane(LandCase):
    """T-4767: train 1115k (2026-11-15) took T-4154, T-4155 and T-4283 with no check on record at all (no lane_check, no
    accepted event: _check_hold held 'a check that never ran' nothing), so each carried a red its builder never saw and
    the gate ran 100 minutes for nothing. The land trains a lane only with a passing affected run at its tip that held the
    repo-shape classes (_clean_run) or a green check at that tip; any other lane is held with the line to run its check."""
    def run_at_tip(self, tid, cmd=None, result="✓ 10 tests", ts="2026-06-01T00:00:00Z"):
        p = c.find_project(self.repo)
        b = c.find_brief(p, tid)
        b.add_evidence(c.AFFECTED_LABEL + (cmd or CLEAN_CMD), result, step=1, ts=ts, ran=True)
        c.save_brief(p, b)

    def ready(self, n, name):
        tid = self.lane(n, name, when=OLD, checked=False)
        self.audited(tid)
        self.returned(tid, f"Lane {n}")
        return tid

    def landed(self, name):
        return os.path.exists(os.path.join(self.repo, name))

    def test_unchecked_lane_with_no_check_on_record_is_held_with_the_line_to_run_it(self):
        a = self.ready(1, "a.txt")
        r = self.land(check=False)
        self.assertFalse(self.landed("a.txt"), r.stdout + r.stderr)
        self.assertRegex(r.stderr, rf"sampo lane land: {a}: held: no passing affected check at its tip")
        self.assertIn(f"sampo lane accept {a} --audit LENS HOW RESULT", r.stderr)  # accept refuses without an audit
        self.assertIn(f"sampo check --affected --hold --evidence {a}", r.stderr)
        self.assertEqual(self.gate_runs(), 0, "a held lane costs no gate run")

    def test_unchecked_lane_whose_newest_run_names_only_its_own_modules_is_held(self):
        a = self.ready(1, "a.txt")
        self.run_at_tip(a, cmd="python3 plugin/tests/run.py test_one test_two")  # the builder's modules, no repo-shape classes
        r = self.land(check=False)
        self.assertFalse(self.landed("a.txt"), r.stdout + r.stderr)
        self.assertIn("did not hold the repo-shape classes", r.stderr)

    def test_unchecked_lane_with_a_failing_or_older_run_is_held_and_says_which(self):
        a, b = self.ready(1, "a.txt"), self.ready(2, "b.txt")
        self.run_at_tip(a, result="✗ 1 failed")
        self.run_at_tip(b, ts="2025-12-01T00:00:00Z")  # before the lane's last commit
        r = self.land(check=False)
        self.assertFalse(self.landed("a.txt") or self.landed("b.txt"), r.stdout + r.stderr)
        self.assertRegex(r.stderr, rf"{a}: held: no passing affected check at its tip \(its newest affected run failed")
        self.assertRegex(r.stderr, rf"{b}: held: no passing affected check at its tip \(its newest affected run is older")

    def test_unchecked_lane_a_full_passing_run_at_its_tip_lands_without_a_new_check(self):
        a = self.ready(1, "a.txt")
        self.run_at_tip(a)
        r = self.land()
        self.assertTrue(self.landed("a.txt"), r.stdout + r.stderr)
        self.assertNotIn("held:", r.stderr)

    def test_unchecked_lane_whose_check_is_running_or_was_cut_off_at_its_tip_is_held(self):
        # 2026-11-17: T-4139's check ran, was cut off twice, and the land's train had taken the lane before it began
        from sampo import cli
        a, b = self.ready(1, "a.txt"), self.ready(2, "b.txt")
        p = c.find_project(self.repo)
        for n, tid, meta in ((1, a, {"check_state": "running", "check_pid": str(os.getpid()),
                                      "check_ticks": str(train._ticks(os.getpid()))}),
                             (2, b, {"check_state": "cut_off", "check_cut_timeout": "1200"})):
            tip = self.git("rev-parse", "HEAD", cwd=os.path.join(self.tmp, f"lane{n}"))
            cli.mutate(p, tid, lambda x, m=meta, t=tip: x.meta.update(check_tip=t, **m), "x", {})
        r = self.land(check=False)
        self.assertFalse(self.landed("a.txt") or self.landed("b.txt"), r.stdout + r.stderr)
        self.assertIn(f"{a}: held: check running", r.stderr)
        self.assertIn(f"{b}: held: its check was cut off or ended without a result twice", r.stderr)  # T-4404's wording
        self.assertIn(f"sampo check --affected --timeout 1200 --hold --evidence {b} in the lane, in the background", r.stderr)

    def test_unchecked_lane_that_waits_for_a_second_train_is_read_again_when_its_turn_comes(self):
        # the land read the hold once at its start: a lane committed to while an earlier train's gate ran was taken unchecked
        a, b = self.ready(1, "a.txt"), self.ready(2, "b.txt")
        self.record_clean_run(a)
        self.record_clean_run(b, ts="2026-11-01T00:00:00Z")  # a clean run at its tip when the land starts: dated before the late commit
        late, marker = os.path.join(self.tmp, "lane2"), os.path.join(self.tmp, "late-commit")
        self.fm("check", "add", f"test -e {marker} || (touch {marker} && git -C {late} commit -q --allow-empty -m late)")
        r = self.land("--batch", "1", check=False)
        self.assertTrue(self.landed("a.txt"), r.stdout + r.stderr)
        self.assertFalse(self.landed("b.txt"), r.stdout + r.stderr)
        self.assertRegex(r.stderr, rf"{b}: held: no passing affected check at its tip \(its newest affected run is older")
        self.assertEqual(re.findall(r"train \d+: (T-\d+)", r.stderr), [a], "no second train for the held lane")

    def test_unchecked_lane_held_at_the_start_joins_the_next_train_when_its_check_turns_green(self):
        # T-4767 review: a lane the hold dropped at the land's start sat in `seen`, in neither ready nor parked, so a check
        # that finished while train 1 ran never brought it back ("it joins the land when its check is green")
        a, b = self.ready(1, "a.txt"), self.ready(2, "b.txt")
        self.record_clean_run(a)  # b has no run on record: held at the start
        script, marker = os.path.join(self.tmp, "green.py"), os.path.join(self.tmp, "went-green")
        with open(script, "w") as f:  # what a finished hand check leaves: a clean affected run at b's tip, dated now
            f.write("import sys\nsys.path.insert(0, %r)\nimport time\nfrom sampo import core as c\n"
                    "p = c.find_project(%r)\nb = c.find_brief(p, %r)\n"
                    "b.add_evidence(c.AFFECTED_LABEL + %r, '✓ 10 tests', step=1, ran=True, "
                    "ts=time.strftime('%%Y-%%m-%%dT%%H:%%M:%%SZ', time.gmtime()))\nc.save_brief(p, b)\n"
                    % (os.path.dirname(os.path.dirname(c.__file__)), self.repo, b, CLEAN_CMD))
        self.fm("check", "add", f"test -e {marker} || (touch {marker} && {sys.executable} {script})")
        r = self.land("--batch", "1", check=False)
        self.assertRegex(r.stderr, rf"{b}: held: no passing affected check at its tip \(it has no affected run")
        self.assertTrue(self.landed("a.txt") and self.landed("b.txt"), r.stdout + r.stderr)
        self.assertEqual(re.findall(r"train \d+: (T-\d+)", r.stderr), [a, b], "b rode the next train of the same land")

    def test_unchecked_lane_a_green_check_at_an_older_tip_does_not_count(self):
        from sampo import cli
        a = self.ready(1, "a.txt")
        p = c.find_project(self.repo)
        tip = self.git("rev-parse", "HEAD", cwd=os.path.join(self.tmp, "lane1"))
        cli.mutate(p, a, lambda x: x.meta.update(check_state="green", check_tip="0" * 40), "x", {})
        r = self.land(check=False)
        self.assertFalse(self.landed("a.txt"), r.stdout + r.stderr)
        self.assertIn("held: no passing affected check at its tip", r.stderr)
        cli.mutate(p, a, lambda x: x.meta.update(check_tip=tip), "x", {})
        self.land()
        self.assertTrue(self.landed("a.txt"), "the same lane lands when its green check is at its tip")


class HandCloseGroups(LandCase):
    def test_hand_close_groups_name_each_reason_once_with_its_clearing_command(self):
        """T-5516: the land's summary says how many lanes each reason leaves, once per reason, with one command that
        clears it (the per-lane lines stay as they were)."""
        a, b, d = (self.lane(n, f"{x}.txt", when=OLD) for n, x in ((1, "a"), (2, "b"), (3, "d")))
        p = c.find_project(self.repo)
        for tid in (a, b):  # an audit older than the lane's tip: predates it
            self.returned(tid)
            x = c.find_brief(p, tid)
            x.add_audit("self", "a review of an older tip", "no findings", ts="2025-06-01T00:00:00Z")
            c.save_brief(p, x)
        self.returned(d)  # no audit at all
        r = self.land()
        groups = [ln for ln in r.stdout.splitlines() if ln.startswith("  hand close group")]
        self.assertEqual(len(groups), 2, r.stdout)
        predate = [g for g in groups if "audits predate the lane tip" in g]
        self.assertEqual(len(predate), 1, r.stdout)
        self.assertIn("group (2) audits predate the lane tip: ", predate[0])
        self.assertIn(a, predate[0])
        self.assertIn(b, predate[0])
        self.assertIn(f"clear with: sampo task audit {a} ", predate[0])
        missing = [g for g in groups if "no audit recorded" in g]
        self.assertEqual(len(missing), 1, r.stdout)
        self.assertIn(f"hand close group (1) no audit recorded: {d} ", missing[0])


class OffTrain(LandCase):
    """T-5368: a land's closes ran between its trains, one by one (30 to 100 s each for an hour on 2026-11-21), and four
    lanes accepted meanwhile waited for the land's next train boundary. The closes run on their own worker now: a lane
    queued or accepted while they are pending trains within one gate, not after the last of them."""

    def setUp(self):
        super().setUp()
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.repo)  # the close runs `sampo` from the cwd: the test's repo, not this checkout
        self.p = c.find_project(self.repo)

    def run_land(self, slow, first_after, want):
        """A land (this process, its own thread) whose close of `slow` blocks until `go` is set; once that close has
        begun, `first_after()` runs, and the land is given 40 s to train `want`. The order of events:
        [('train', ids) | ('close', id), ('closed', id)]."""
        import threading
        order, begun, go, out = [], threading.Event(), threading.Event(), {}
        real_close, real_train = train._close, train.train

        def close(p, main, tid, *a, **k):
            if tid == slow:
                begun.set()
                go.wait(60)
            order.append(("close", tid))
            got = real_close(p, main, tid, *a, **k)
            order.append(("closed", tid))
            return got

        def trained(p, main, ids, *a, **k):
            order.append(("train", list(ids)))
            return real_train(p, main, ids, *a, **k)

        def land():
            try:
                with self.in_project():
                    out["got"] = train.land(self.p, self.repo, 1)
            except BaseException as e:  # noqa: BLE001 — reported by the test thread
                out["err"] = e
        with mock.patch.object(train, "_close", close), mock.patch.object(train, "train", trained):
            t = threading.Thread(target=land, daemon=True)
            t.start()
            try:
                self.assertTrue(begun.wait(60), "the slow close began: " + repr(order))
                first_after()
                deadline = time.monotonic() + 40
                while time.monotonic() < deadline and ("train", [want]) not in order:
                    time.sleep(0.2)
            finally:
                go.set()
                t.join(120)
        return order, out

    def test_a_first_lane_queued_while_closes_are_pending_trains_before_the_remaining_closes_finish(self):
        early, a = (self.lane(n, f"{n}.txt", when=OLD) for n in (1, 2))
        d = self.lane(3, "3.txt")  # committed just now: not finished until its builder returns
        self.audited(early, a)
        self.returned(early, "Lane 1")
        self.returned(a, "Lane 2")
        self.train(early)  # merged by a train of its own: open until a land closes it (its close is the slow one)

        def queue_d():  # d is accepted and named --first while the land is busy closing
            self.audited(d)
            self.returned(d, "Lane 3")
            train.first_ids(self.p, [d])
        order, out = self.run_land(early, queue_d, d)
        late = [i for i, e in enumerate(order) if e == ("train", [d])]
        self.assertTrue(late, f"the --first lane trained while a close was pending: {order} {out}")
        self.assertLess(late[0], order.index(("closed", early)), f"before the slow close finished: {order}")
        self.assertNotIn("err", out, out)
        self.assertEqual([self.brief(t).status for t in (early, a, d)], ["done"] * 3, f"{order} {out}")
        self.assertEqual(sorted(out["got"]["closed"]), sorted([early, a, d]), out["got"])

    def test_closes_still_pending_at_the_last_train_finish_before_the_land_returns(self):
        early, a = (self.lane(n, f"{n}.txt", when=OLD) for n in (1, 2))
        self.audited(early, a)
        self.returned(early, "Lane 1")
        self.returned(a, "Lane 2")
        self.train(early)
        order, out = self.run_land(early, lambda: None, a)
        self.assertNotIn("err", out, out)
        self.assertEqual(sorted(out["got"]["closed"]), sorted([early, a]), out["got"])
        self.assertEqual([self.brief(t).status for t in (early, a)], ["done", "done"], order)

    def test_a_train_waits_for_the_lands_own_closer_to_let_go_of_the_lock_not_one_second(self):
        """The closer removes its closing file and lets go of the lock a moment later: a train that asks in that gap found
        no close and was refused after 1 s, which stopped the land. Here the closer holds the lock 1.5 s with the file
        already gone; the train must get the lock when it lets go."""
        import threading
        held, got = threading.Event(), {}

        def finish(args):  # runs inside the close's commit, under the train's lock
            with contextlib.suppress(OSError):
                os.remove(os.path.join(self.p.dir, "train", lanes.CLOSE_MARK))
            held.set()
            time.sleep(1.5)
            return 1, "", "nothing to commit"

        def closer():
            with mock.patch.object(train, "_sampo", finish):
                got["commit"] = train._commit_close(self.p, "T-9999", "t", "")
        t = threading.Thread(target=closer, daemon=True)
        t.start()
        try:
            self.assertTrue(held.wait(30), "the closer holds the lock")
            t0 = time.monotonic()
            try:
                with train._train_run_lock(self.p):
                    got["waited"] = time.monotonic() - t0
            except c.LockTimeout as e:
                self.fail(f"the train was refused after {time.monotonic() - t0:.1f} s while the closer held the lock: {e}")
        finally:
            t.join(60)
        self.assertGreater(got["waited"], 0.3, "it waited for the closer, not slipped past it")
        self.assertLess(got["waited"], 10, got)


class StackedDependent(LandCase):
    """T-5725 review: a lane built on another lane's tip carries that lane's commits; landed first, it puts them on main
    unreviewed and unchecked. The land holds it until the dependency's tip is on main, whatever land_key's order says."""

    def stacked(self):
        """Lane 1 (the older task: land_key sorts it first) is stacked on lane 2."""
        top = self.lane(1, "top.txt", when=OLD)  # the first made: T-0001
        dep = self.lane(2, "dep.txt", when=OLD)
        one = os.path.join(self.tmp, "lane1")
        subprocess.run(["git", "-C", one, "merge", "-q", "--no-edit", "foreman/lane2"], check=True, capture_output=True,
                       env=dict(os.environ, GIT_AUTHOR_DATE=OLD, GIT_COMMITTER_DATE=OLD))  # old: the lane is quiet
        self.record_clean_run(top)  # its tip moved by the merge
        p = c.find_project(self.repo)
        b = c.find_brief(p, top)
        b.meta["stacked_on"] = dep
        c.save_brief(p, b)
        return dep, top

    def test_the_dependent_sorts_ahead_of_its_dependency_and_the_land_holds_it_until_the_dependency_is_on_main(self):
        dep, top = self.stacked()
        p = c.find_project(self.repo)
        bd, bt = c.find_brief(p, dep), c.find_brief(p, top)
        self.assertLess(train.land_key(bt), train.land_key(bd), "the premise: the order puts the dependent first")
        tip = train._lane_tip(self.repo, bt)
        why = train._lane_hold(p, bt, tip, self.repo)
        self.assertIsNotNone(why)
        self.assertIn(dep, why)
        self.assertIsNone(train._lane_hold(p, bd, train._lane_tip(self.repo, bd), self.repo), "the dependency is not held")
        self.git("merge", "-q", "--no-edit", "foreman/lane2")
        self.assertIsNone(train._lane_hold(p, bt, tip, self.repo), "the dependency's tip is on main: no hold")

    def test_a_land_trains_the_dependency_before_the_dependent_that_sorts_ahead_of_it(self):
        dep, top = self.stacked()
        self.audited(dep, top)
        self.returned(dep, "Dep lane")
        self.returned(top, "Top lane")
        r = self.land()
        self.assertEqual(self.brief(dep).status, "done", r.stdout + r.stderr)
        self.assertEqual(self.brief(top).status, "done", r.stdout + r.stderr)
        subjects = self.git("log", "--format=%s").splitlines()  # newest first
        self.assertGreater(subjects.index(f"Dep lane ({dep})"), subjects.index(f"Top lane ({top})"), subjects)
        self.assertRegex(r.stderr, rf"{top}: held: built on {dep}")

    def test_a_dependency_that_is_gone_holds_nothing(self):
        dep, top = self.stacked()
        p = c.find_project(self.repo)
        bt = c.find_brief(p, top)
        bt.meta["stacked_on"] = "T-9876"
        c.save_brief(p, bt)
        self.assertIsNone(train._lane_hold(p, bt, train._lane_tip(self.repo, bt), self.repo))

    def test_a_dropped_dependency_holds_nothing_though_its_branch_survives(self):  # T-5725 review round 2
        dep, top = self.stacked()
        p = c.find_project(self.repo)
        bt = c.find_brief(p, top)
        self.assertIsNotNone(train._lane_hold(p, bt, train._lane_tip(self.repo, bt), self.repo), "the premise: held first")
        self.fm("task", "drop", dep, "--reason", "not wanted", "--force")  # its unmerged branch stays: the tip is not on main, and never will be
        self.assertEqual(self.brief(dep).status, "dropped")
        p = c.find_project(self.repo)
        bt = c.find_brief(p, top)
        self.assertIsNone(train._lane_hold(p, bt, train._lane_tip(self.repo, bt), self.repo),
                          "a dropped dependency never lands: holding for it holds forever")


class AcceptDuringMerge(LandCase):
    """T-5816: a land started while a `sampo lane merge` (a train) holds the train lock found it held and exited with a
    refusal in land.log. Accept queues the lane (first_ids) and starts no land, saying the merge is running."""

    def accept(self, tid):
        import io
        import types
        p = c.find_project(self.repo)
        args = types.SimpleNamespace(audit=[["self", "read the diff", "no findings"]], json=False, no_check=False)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.in_project():
            train.accept(p, c.find_brief(p, tid), self.repo, args)
        return out.getvalue()

    def test_accept_during_merge_queues_the_lane_and_starts_no_land(self):
        a = self.lane(1, "a.txt", when=OLD)
        p = c.find_project(self.repo)
        with mock.patch.object(train, "spawn_land", return_value=(4242, "land.log")) as spawn:
            with c.lock(os.path.join(p.dir, "train"), timeout=1):  # a lane merge's train holds it
                out = self.accept(a)
            spawn.assert_not_called()
            self.assertIn("merge is running", out)
            self.assertIn(a, train.first_ids(p), "queued first for the next land")
            self.assertNotIn("land started in the background", out)
            out = self.accept(a)  # the merge is over: the same accept starts the land
            spawn.assert_called_once()
            self.assertIn("land started in the background", out)


if __name__ == "__main__":
    unittest.main()
