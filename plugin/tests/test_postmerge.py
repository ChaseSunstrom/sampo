"""T-5791: the live landing's behind verify must not stop silently. 2026-11-22: 133 merge records sat pending from 02:21
with no runner (a spawn that died with its caller was never retried), two trunk reds (T-5774, T-5782: docs, left_out,
EnvClassified and more) were found by builders hours later, about 20 closes were blocked on them, and a red between two
lanes that were each green alone had no one lane to revert. So: a stale pending record always gets a runner (pm_ensure,
asked by the land loop and the close sweep), the repo-shape modules run first and a shape red is recorded within that
short pass, and a shape red between two lanes green alone becomes one express FIX naming both, with the closes that fail
only on its classes queued to close again when it lands. All in temp repos with shell stand-ins for the suite."""
import json
import os
import subprocess
import time
import unittest.mock

from helpers import ForemanTestCase

from sampo import core as c
from sampo import lanes as slanes
from sampo import train

first = lambda text: json.JSONDecoder().raw_decode(text)[0]
OLD = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 7200))
RED_BOTH = "[ -f a.txt ] && [ -f b.txt ] && { echo 'FAIL: t (m.B.t)'; exit 1; }; exit 0"  # red only when both lanes' files meet


class PmCase(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)
        self.mark = os.path.join(self.tmp, "order.txt")  # what ran, in order: a command writes to it by absolute path
        self.base = self.git("rev-parse", "HEAD")
        self.trunk = self.git("rev-parse", "--abbrev-ref", "HEAD")

    def env(self, **kv):
        for k, v in kv.items():
            os.environ[k] = v
            self.addCleanup(os.environ.pop, k, None)

    def git(self, *args):
        return subprocess.run(["git", "-C", self.repo, *args], capture_output=True, text=True).stdout.strip()

    def task(self, title="Landed"):
        return first(self.fm("task", "new", title, "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true",
                             "--step", "s", "--json").stdout)["id"]

    def lane(self, name, text="x\n"):
        """A branch off the base holding one file: a lane that never saw what landed after the base."""
        self.git("checkout", "-qb", f"b-{name}", self.base)
        with open(os.path.join(self.repo, name), "w") as f:
            f.write(text)
        self.git("add", "-A")
        self.git("commit", "-qm", name)
        self.git("checkout", "-q", self.trunk)

    def merge(self, name, tid):
        """A --no-ff merge of the lane, recorded pending like merge() does (no runner spawned)."""
        self.git("merge", "-q", "--no-ff", "-m", f"Merge {name} ({tid})", f"b-{name}")
        sha = self.git("rev-parse", "HEAD")
        with unittest.mock.patch.object(slanes, "pm_spawn"):
            slanes.pm_record(self.p, sha, tid, self.repo)
        return sha

    def rec(self, sha):
        return slanes.pm_records(self.p)[sha]

    def ran(self):
        try:
            with open(self.mark) as f:
                return f.read().split()
        except OSError:
            return []


class PmRespawn(PmCase):
    def setUp(self):
        super().setUp()
        self.env(FM_POSTMERGE_CMD="exit 0", FM_POSTMERGE_RERUN="exit 0")
        self.tid = self.task()
        self.lane("a.txt")
        self.sha = self.merge("a.txt", self.tid)
        os.environ.pop("FOREMAN_NO_BACKGROUND", None)  # the test base sets it: pm_spawn is mocked below, never run

    def test_respawn_with_no_background_set_asks_for_nothing_and_logs_nothing(self):
        slanes.pm_set(self.p, self.sha, at=OLD)
        os.environ["FOREMAN_NO_BACKGROUND"] = "1"
        with unittest.mock.patch.object(slanes, "pm_spawn") as spawn:
            self.assertFalse(slanes.pm_ensure(self.p, self.repo))
        self.assertEqual(spawn.call_count, 0)
        self.assertEqual([e for e in c.ledger_tail(self.p, 200) if e.get("event") == "postmerge_respawn"], [],
                         "no spawn was made, so none is said to be")
        self.assertFalse(os.path.exists(os.path.join(slanes._pm_dir(self.p), ".ensure")), "and no gap is started")

    def test_respawn_the_close_wait_asks_again_at_each_poll_not_once(self):
        with open(train.__file__) as f:
            src = f.read()
        body = src.split("    def wait_closes():", 1)[1].split("    def to_close", 1)[0]
        loop = body.split("while not closer.idle():", 1)[1]
        self.assertIn("ensure_pm()", loop, "a close that waits for hours is asked about at every poll")
        self.assertNotIn("ensure_pm()", body.split("while not closer.idle():", 1)[0])

    def test_respawn_the_post_merge_switches_are_test_overrides(self):
        for name in ("FM_POSTMERGE_CMD", "FM_POSTMERGE_RERUN", "FM_POSTMERGE_SHAPE"):
            self.assertIn(name, c.TEST_OVERRIDES)

    def ensure(self):
        with unittest.mock.patch.object(slanes, "pm_spawn") as spawn:
            slanes.pm_ensure(self.p, self.repo)
        return spawn.call_count

    def test_respawn_a_pending_record_older_than_20_minutes_with_no_runner_gets_one(self):
        slanes.pm_set(self.p, self.sha, at=OLD)
        self.assertEqual(self.ensure(), 1)

    def test_respawn_leaves_a_fresh_record_alone(self):
        self.assertEqual(self.ensure(), 0, "just landed: its own spawn is on the way")

    def test_respawn_leaves_a_record_a_live_runner_holds_alone(self):
        slanes.pm_set(self.p, self.sha, at=OLD, state="running", pid=os.getpid())
        self.assertEqual(self.ensure(), 0)
        other = self.task("Other")
        self.lane("b.txt")
        sha2 = self.merge("b.txt", other)
        slanes.pm_set(self.p, sha2, at=OLD)  # pending behind it: the live runner takes it next
        self.assertEqual(self.ensure(), 0)

    def test_respawn_a_record_whose_runner_died_gets_one(self):
        slanes.pm_set(self.p, self.sha, at=OLD, state="running", pid=2 ** 22 + 12345)
        self.assertEqual(self.ensure(), 1)

    def test_respawn_asks_once_per_gap_not_at_every_tick(self):
        slanes.pm_set(self.p, self.sha, at=OLD)
        with unittest.mock.patch.object(slanes, "pm_spawn") as spawn:
            slanes.pm_ensure(self.p, self.repo)
            slanes.pm_ensure(self.p, self.repo)
        self.assertEqual(spawn.call_count, 1, "a runner that dies at once is not respawned every few seconds")
        with open(os.path.join(slanes._pm_dir(self.p), ".ensure"), "w") as f:
            json.dump({"t": time.time() - 3600}, f)
        self.assertEqual(self.ensure(), 1, "a gap later it tries again")

    def test_respawn_waits_for_nothing_when_a_conflict_already_holds_the_queue(self):
        slanes.pm_set(self.p, self.sha, at=OLD)
        other = self.task("Other")
        self.lane("b.txt")
        sha2 = self.merge("b.txt", other)
        slanes.pm_set(self.p, sha2, state="conflict", settle_head=self.git("rev-parse", "HEAD"))
        self.assertEqual(self.ensure(), 0, "a runner could do nothing: the conflict was checked at this HEAD")

    def test_respawn_the_land_loop_and_the_close_sweep_both_ask_for_it(self):
        with unittest.mock.patch.object(slanes, "pm_ensure") as ens:
            train.land_merged(self.p, self.repo)
        self.assertEqual(ens.call_count, 1, "the close sweep")
        with unittest.mock.patch.object(slanes, "pm_ensure") as ens, unittest.mock.patch.object(train, "start_verify"):
            train._land_trains(self.p, self.repo, 1, [False])
        self.assertGreaterEqual(ens.call_count, 1, "the land loop")

    def test_respawn_none_without_a_suite_command(self):
        os.environ.pop("FM_POSTMERGE_CMD")
        slanes.pm_set(self.p, self.sha, at=OLD)
        self.assertEqual(self.ensure(), 0, "no suite, no verify to run")


class PmShapeFirst(PmCase):
    def setUp(self):
        super().setUp()
        self.shape_ish = f"echo shape >> {self.mark}; " + RED_BOTH
        self.full = f"echo full >> {self.mark}; " + RED_BOTH
        self.env(FM_POSTMERGE_SHAPE=self.shape_ish, FM_POSTMERGE_CMD=self.full, FM_POSTMERGE_RERUN=RED_BOTH)
        patcher = unittest.mock.patch.object(c, "REPO_SHAPE", ("m",))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tid = self.task()

    def both(self):
        self.lane("a.txt")
        self.lane("b.txt")
        return self.merge("a.txt", self.tid), self.merge("b.txt", self.task("Other"))

    def test_shape_first_the_default_command_is_the_repo_shape_modules_of_this_repos_runner(self):
        os.environ.pop("FM_POSTMERGE_SHAPE")
        self.assertEqual(slanes.pm_shape_cmd(self.repo), "", "no runner here: no shape pass")
        os.makedirs(os.path.join(self.repo, "plugin", "tests"))
        with open(os.path.join(self.repo, "plugin", "tests", "run.py"), "w") as f:
            f.write("")
        self.assertEqual(slanes.pm_shape_cmd(self.repo), "python3 plugin/tests/run.py m")
        self.env(FM_POSTMERGE_SHAPE="echo x")
        self.assertEqual(slanes.pm_shape_cmd(self.repo), "echo x")

    def test_shape_first_a_green_shape_pass_goes_on_to_the_full_suite(self):
        self.lane("a.txt")
        sha = self.merge("a.txt", self.tid)
        slanes.pm_run(self.p, self.repo)
        self.assertEqual(self.ran(), ["shape", "full"])
        self.assertEqual(self.rec(sha)["state"], "green")

    def test_shape_first_a_shape_red_is_recorded_before_the_full_suite_runs(self):
        a, b = self.both()
        slanes.pm_run(self.p, self.repo)
        red = [r for r in slanes.pm_records(self.p).values() if r["state"] in ("red", "reverted", "semantic")]
        self.assertEqual([r["classes"] for r in red], [["m.B"]])
        self.assertEqual(red[0]["sha"], b)
        # the HEAD probe stopped at the shape pass, so it stands for nothing else: A is green only by a full run of its own
        # tree (the one "full" below), never "the rest of the suite green on HEAD" (that run never happened)
        self.assertEqual(self.ran(), ["shape", "shape", "full", "shape"], "b's red came after one real full run, on a")
        ra = self.rec(a)
        self.assertEqual((ra["state"], ra.get("covered_by"), ra.get("narrowed")), ("green", a, False))

    def test_shape_first_a_shape_only_probe_never_covers_a_merge_it_did_not_run_the_suite_on(self):
        a, b = self.both()
        with unittest.mock.patch.object(slanes, "_pm_green_at", return_value=False) as probe, \
                unittest.mock.patch.object(slanes, "pm_verify", return_value=False), \
                unittest.mock.patch.object(slanes, "_pm_first_red", return_value=1), \
                unittest.mock.patch.object(slanes, "_pm_cmd_green_at", return_value=False), \
                unittest.mock.patch.object(slanes, "_pm_cover") as cover:
            def stopped(p, main, sha, reds=None, short=None, **_):  # T-5936: part, since, heads
                if reds is not None:
                    reds.append("m.B")
                if short is not None:
                    short.append(True)  # the probe ended at the shape pass
                return False
            probe.side_effect = stopped
            slanes.pm_run(self.p, self.repo)
        self.assertEqual(cover.call_count, 0, "no narrowing and no cover on a probe whose full suite never ran")

    def test_shape_first_a_shape_run_that_names_no_class_is_no_verdict_and_the_full_suite_decides(self):
        for shape in ("echo 'run.py: no test matches m.Gone'; exit 1", "exit 1", "echo 'run.py: m not started'; exit 1"):
            with self.subTest(shape=shape):
                self.env(FM_POSTMERGE_SHAPE=f"echo shape >> {self.mark}; " + shape, FM_POSTMERGE_CMD=f"echo full >> {self.mark}; exit 0")
                os.remove(self.mark) if os.path.exists(self.mark) else None
                self.lane(f"n{len(shape)}.txt")
                sha = self.merge(f"n{len(shape)}.txt", self.task())
                slanes.pm_run(self.p, self.repo)
                self.assertEqual(self.ran(), ["shape", "full"], "a module missing from an older tree is not a red")
                self.assertEqual(self.rec(sha)["state"], "green")

    def test_shape_first_a_shape_run_that_names_no_class_is_no_verdict_at_the_head_probe_too(self):
        self.env(FM_POSTMERGE_SHAPE=f"echo shape >> {self.mark}; echo 'run.py: no test matches m.Gone'; exit 1",
                 FM_POSTMERGE_CMD=f"echo full >> {self.mark}; exit 0")
        self.lane("a.txt")
        self.lane("c.txt")
        sa = self.merge("a.txt", self.tid)
        sc = self.merge("c.txt", self.task("Other"))
        slanes.pm_run(self.p, self.repo)
        self.assertEqual(self.ran(), ["shape", "full"])
        self.assertEqual((self.rec(sa)["state"], self.rec(sc)["state"]), ("green", "green"))

    def test_shape_first_a_shape_class_red_only_inside_the_full_run_is_flaky_and_the_full_suite_still_runs(self):
        self.lane("a.txt")
        self.lane("b.txt")
        self.env(FM_POSTMERGE_RERUN="exit 0")  # red in the shape pass, green alone
        self.merge("a.txt", self.tid)
        sha = self.merge("b.txt", self.task("Other"))
        slanes.pm_run(self.p, self.repo)
        self.assertIn("full", self.ran(), "a shape red that is no merge's does not stand in for the suite")
        self.assertEqual(self.rec(sha)["state"], "green")

    def test_shape_first_the_head_probe_of_several_pending_merges_runs_the_shape_modules_first_too(self):
        self.lane("a.txt")
        self.lane("c.txt")
        self.merge("a.txt", self.tid)
        self.merge("c.txt", self.task("Other"))
        slanes.pm_run(self.p, self.repo)
        self.assertEqual(self.ran(), ["shape", "full"], "one probe covers both merges: one shape pass, one full run")

    def test_shape_first_a_shape_pass_that_times_out_does_not_stand_for_a_red(self):
        self.env(FM_POSTMERGE_SHAPE="exit 124")
        self.lane("a.txt")
        sha = self.merge("a.txt", self.tid)
        slanes.pm_run(self.p, self.repo)
        self.assertEqual(self.ran(), ["full"])
        self.assertEqual(self.rec(sha)["state"], "green")


class PmSemanticRed(PmCase):
    """Lane A and lane B each pass the shape modules alone; merged together they fail m.B (the first parent, with only
    B on it, is green). No one lane is to blame, so no revert: one express FIX names both."""

    def setUp(self):
        super().setUp()
        self.env(FM_POSTMERGE_SHAPE=RED_BOTH, FM_POSTMERGE_CMD=RED_BOTH, FM_POSTMERGE_RERUN=RED_BOTH)
        patcher = unittest.mock.patch.object(c, "REPO_SHAPE", ("m",))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.a, self.b = self.task("Lane A"), self.task("Lane B")

    def green_alone(self, *tids):
        """Mark the lanes as the accept does: check_shape is the tip the shape modules ran green at. The tip is the
        branch's own, so it is written when the lane exists (lane() below), and a lane merged later is that tip's sha^2."""
        self.alone = getattr(self, "alone", set()) | set(tids)
        self.mark_alone()

    def mark_alone(self):
        from sampo import cli
        for t in getattr(self, "alone", ()):
            name = {self.a: "a.txt", self.b: "b.txt"}.get(t) or getattr(self, "names", {}).get(t)
            tip = self.git("rev-parse", "--verify", "-q", f"b-{name}") if name else ""
            if tip:
                cli.mutate(self.p, t, lambda x, tip=tip: x.meta.update(check_shape=tip), "test_mark", {})

    def lane(self, name, text="x\n"):
        super().lane(name, text)
        self.mark_alone()

    def meet(self):
        """B lands first, A on top of it (A's lane never saw B): A's merge is red in m.B."""
        self.lane("a.txt")
        self.lane("b.txt")
        sb = self.merge("b.txt", self.b)
        sa = self.merge("a.txt", self.a)
        return sa, sb

    def fixes(self):
        return [b for b in c.load_briefs(self.p, include_archive=True)
                if b.id not in (self.a, self.b) and "m.B" in b.section("Raw request")]

    def test_semantic_red_two_lanes_each_green_alone_file_one_express_fix_and_no_revert(self):
        self.green_alone(self.a, self.b)
        sa, sb = self.meet()
        slanes.pm_run(self.p, self.repo)
        r = self.rec(sa)
        self.assertEqual((r["state"], r["classes"]), ("semantic", ["m.B"]))
        self.assertEqual(self.rec(sb)["state"], "green")
        self.assertTrue(os.path.exists(os.path.join(self.repo, "a.txt")), "nothing was reverted")
        (fix,) = self.fixes()
        self.assertEqual(fix.type, "FIX")
        self.assertTrue(train.is_express(fix), "urgent: it trains next and alone")
        text = fix.section("Raw request")
        self.assertIn(self.a, text)
        self.assertIn(self.b, text)
        self.assertIn("m.B", fix.title)
        self.assertEqual(fix.meta.get("trunk_red"), ["m.B"])
        slanes.pm_wait(self.p, self.repo)  # a semantic red holds no land: the FIX is the way out

    def test_semantic_red_is_filed_once(self):
        self.green_alone(self.a, self.b)
        sa, _ = self.meet()
        slanes.pm_run(self.p, self.repo)
        slanes.pm_set(self.p, sa, state="pending")
        slanes.pm_run(self.p, self.repo)
        self.assertEqual(len(self.fixes()), 1)

    def test_semantic_red_a_lane_not_green_alone_is_reverted_as_before(self):
        self.green_alone(self.b)  # A has no shape pass on record: it may be A's own fault
        sa, _ = self.meet()
        slanes.pm_run(self.p, self.repo)
        self.assertEqual(self.rec(sa)["state"], "reverted")
        self.assertEqual(self.fixes(), [])

    def test_semantic_red_a_stale_mark_is_no_green_alone_the_lane_tip_moved_since_the_shape_pass(self):
        from sampo import cli
        self.green_alone(self.a, self.b)
        sa, _ = self.meet()
        cli.mutate(self.p, self.a, lambda x: x.meta.update(check_shape="0" * 40), "test_mark", {})  # a tip since replaced
        slanes.pm_run(self.p, self.repo)
        self.assertEqual(self.rec(sa)["state"], "reverted", "the mark names another tip than the one merged")
        self.assertEqual(self.fixes(), [])

    def test_semantic_red_a_partner_with_a_stale_mark_is_no_partner(self):
        from sampo import cli
        self.green_alone(self.a, self.b)
        sa, _ = self.meet()
        cli.mutate(self.p, self.b, lambda x: x.meta.update(check_shape="0" * 40), "test_mark", {})
        slanes.pm_run(self.p, self.repo)
        self.assertEqual(self.rec(sa)["state"], "reverted")

    def test_semantic_red_partners_whose_files_the_red_class_reads_are_named_first(self):
        with open(os.path.join(self.repo, "m.py"), "w") as f:  # the red class's module, naming c.txt and not b.txt
            f.write("# reads c.txt\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "m")
        self.base = self.git("rev-parse", "HEAD")
        d = self.task("Lane C")
        self.names = {d: "c.txt"}
        self.green_alone(self.a, self.b, d)
        for n in ("a.txt", "b.txt", "c.txt"):
            self.lane(n)
        self.merge("b.txt", self.b)
        self.merge("c.txt", d)
        sa = self.merge("a.txt", self.a)
        slanes.pm_run(self.p, self.repo)
        self.assertEqual(self.rec(sa)["partners"], [d, self.b], "the lane whose file the class reads leads")

    def test_semantic_red_a_later_merge_red_on_the_same_classes_files_no_second_fix(self):
        """After the semantic red, the next merge fails the same classes on its first parent too: it is trunk's red, which
        the open semantic FIX already owns (by class subset), not a second FIX under another key."""
        self.green_alone(self.a, self.b)
        self.meet()
        slanes.pm_run(self.p, self.repo)
        (fix,) = self.fixes()
        e = self.task("Lane E")
        self.lane("e.txt")
        se = self.merge("e.txt", e)
        slanes.pm_run(self.p, self.repo)
        self.assertEqual(self.rec(se)["state"], "inherited")
        self.assertEqual([f.id for f in self.fixes()], [fix.id], "one FIX for the one red")
        self.assertEqual([b.id for b in c.load_briefs(self.p) if b.type == "FIX" and b.id != fix.id], [])

    def test_semantic_red_a_merge_with_no_lane_landed_since_its_base_is_reverted_as_before(self):
        self.green_alone(self.a)
        self.lane("a.txt")
        self.lane("b.txt")
        with open(os.path.join(self.repo, "b.txt"), "w") as f:  # b.txt on trunk by hand: no merge of a lane
            f.write("x\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "b by hand")
        sa = self.merge("a.txt", self.a)
        slanes.pm_run(self.p, self.repo)
        self.assertEqual(self.rec(sa)["state"], "reverted")

    def test_semantic_red_a_red_outside_the_shape_modules_is_reverted_as_before(self):
        self.green_alone(self.a, self.b)
        self.env(FM_POSTMERGE_SHAPE="exit 0", FM_POSTMERGE_CMD=RED_BOTH.replace("m.B", "other.C"),
                 FM_POSTMERGE_RERUN=RED_BOTH.replace("m.B", "other.C"))
        sa, _ = self.meet()
        slanes.pm_run(self.p, self.repo)
        self.assertEqual(self.rec(sa)["state"], "reverted", "only the shape modules' green alone is on record")

    # ---- the closes that fail only on the FIX's classes wait for it, and close again when it lands
    def fix_and_refusal(self, why="the gates are red: failing test_docs.Agents (t)"):
        self.green_alone(self.a, self.b)
        from sampo import cli
        fix, _ = __import__("sampo.eco", fromlist=["eco"]).capture_once(self.p, "Trunk red: test_docs.Agents\n\nx", "FIX",
                                                                          "trunk-red", key="Trunk red: test_docs.Agents")
        cli.mutate(self.p, fix.id, lambda x: x.meta.update(priority="urgent", trunk_red=["test_docs.Agents"]), "test_mark", {})
        return fix.id, why

    def test_semantic_red_a_close_failing_only_on_the_fixs_classes_waits_for_it(self):
        fid, why = self.fix_and_refusal()
        self.assertTrue(train._fix_wait(self.p, self.repo, self.a, why))
        b = c.find_brief(self.p, self.a)
        self.assertEqual(b.meta.get("close_waits_on"), fid)
        self.assertIn(fid, b.section("Log"))

    def test_semantic_red_a_close_failing_on_anything_else_is_left_as_before(self):
        fid, _ = self.fix_and_refusal()
        for why in ("the gates are red: failing test_docs.Agents and test_run.EnvClassified",
                    "no audit recorded: review the merged diff",
                    "the gates are red: failing test_other.Thing"):
            with self.subTest(why=why):
                self.assertFalse(train._fix_wait(self.p, self.repo, self.a, why))
        self.assertIsNone(c.find_brief(self.p, self.a).meta.get("close_waits_on"))

    def test_semantic_red_a_closed_fix_is_not_waited_for(self):
        fid, why = self.fix_and_refusal()
        from sampo import cli
        cli.mutate(self.p, fid, lambda x: x.meta.update(status="done"), "test_mark", {})
        self.assertFalse(train._fix_wait(self.p, self.repo, self.a, why))

    def test_semantic_red_the_close_is_queued_to_close_again_when_the_fix_lands(self):
        fid, why = self.fix_and_refusal()
        tid = self.a
        b = c.find_brief(self.p, tid)
        b.append_log(f"land: close refused: {why} @ {train._brief_hash(b)} abc1234 {train._code_id()}")
        c.save_brief(self.p, b)
        self.assertTrue(train._fix_wait(self.p, self.repo, tid, why))
        self.assertFalse(train._fix_released(self.p, self.repo, tid, landed=[]), "the FIX is not on trunk yet")
        b = c.find_brief(self.p, tid)
        self.assertIsNotNone(train._refused_before(b, "abc1234"), "the refusal memo stands while it waits")
        self.assertTrue(train._fix_released(self.p, self.repo, tid, landed=[fid]), "it landed in this land")
        b = c.find_brief(self.p, tid)
        self.assertIsNone(b.meta.get("close_waits_on"))
        self.assertIn("land: close queued again", b.section("Log"))
        self.assertIsNone(train._refused_before(b, "abc1234"), "the memo no longer says 'no retry': the close runs again")
        self.assertFalse(train._fix_released(self.p, self.repo, tid, landed=[fid]), "once: nothing left to release")

    def test_semantic_red_a_leftover_waiting_for_its_fix_is_not_closed_by_the_sweep(self):
        fid, why = self.fix_and_refusal()
        self.assertTrue(train._fix_wait(self.p, self.repo, self.a, why))
        self.assertEqual(train._fix_waiting(self.p, self.repo, [self.a, self.b]), {self.a: fid})
