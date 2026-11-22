"""T-5025 (slice of T-4337, a generation is a GIANT task): when kalevala select judges gen N it writes a Landed section into
gen-N/select.md and into lineage.md: each child of the gen's GIANT task with its status and the tower score delta from
the scores select already holds (no exam is re-run)."""
# map-links: plugin/lib/sampo/kalevala.py plugin/lib/sampo/big.py
import json
import os
from unittest import mock

from helpers import CLEAN_READER, ForemanTestCase, read_text
from sampo import core as c


def setUpModule():
    os.environ["PATH"] = CLEAN_READER + os.pathsep + os.environ["PATH"]


def tearDownModule():
    os.environ["PATH"] = os.environ["PATH"].replace(CLEAN_READER + os.pathsep, "", 1)


class Landed(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        gen0 = os.path.join(self.tmp, "gen0.md")
        with open(gen0, "w") as f:
            f.write("root charter\n")
        self.fm("genesis", "init", "--file", gen0)
        self.fm("kalevala", "start")  # efficiency, memory, problem-solving

    def project(self):
        return c.find_project(self.repo, create=False)

    def grade(self, tower, run, train):
        from sampo import rune
        rune.set_tower(tower)
        try:
            with open(os.path.join(rune.home(self.project()), "exam.jsonl"), "a") as f:
                f.write(json.dumps({"agent": "foreman", "run": run, "model": "sonnet", "split": "train",
                                    "closed": {"train": list(train), "holdout": []}}) + "\n")
        finally:
            rune.set_tower(None)

    def task(self, title):
        self.fm("task", "new", title, "--type", "FEATURE", "--tier", "S", "--ac", "x :: true", "--step", "do it")
        return "T-%04d" % max(c.id_num(b.id) for b in c.load_briefs(self.project()))

    def giant(self, kids):
        """A GIANT task whose Children record names `kids`: [(title, tower or None, status)]. Returns its id."""
        p = self.project()
        gid = self.task("Gen giant")
        rows = []
        for i, (title, tower, status) in enumerate(kids, 1):
            kid = c.find_brief(p, self.task(title))
            kid.meta.update(parent=gid, big_key=f"m{i}", status=status)
            if tower:
                kid.meta["tower"] = tower
            c.save_brief(p, kid)
            rows.append(f"m{i} → {kid.id} (block {i:08x})")
        big = c.find_brief(p, gid)
        big.meta["tier"] = "GIANT"
        big.set_section("Children", "\n".join(rows) + "\n")
        c.save_brief(p, big)
        return gid

    def record(self, n, gid):
        from sampo import kalevala
        m = kalevala.load(self.project())
        m["gens"][str(n)]["giant"] = gid
        kalevala._save(self.project(), m)

    def two_gens(self, kids, before=(("memory", ["a"]), ("efficiency", ["a", "b"])),
                 after=(("memory", ["a", "b", "c"]), ("efficiency", ["a", "b"]))):
        """Gen 1 judged (the baseline), then gen 2 with a GIANT carrying `kids`, its scores graded, not yet selected."""
        from sampo import kalevala
        p = self.project()
        for t, s in before:
            self.grade(t, "r1", s)
        kalevala.gen(p)
        kalevala.select(p, 1)
        kalevala.gen(p)
        self.record(2, self.giant(kids))
        for t, s in after:
            self.grade(t, "r2", s)

    def select_md(self, n):
        from sampo import kalevala
        return read_text(os.path.join(kalevala.home(self.project()), f"gen-{n}", "select.md"))

    def lineage(self):
        from sampo import kalevala
        return read_text(os.path.join(kalevala.home(self.project()), "lineage.md"))

    def test_select_lists_each_child_with_its_status_and_its_towers_delta_in_select_and_lineage(self):
        from sampo import kalevala
        self.two_gens([("Memory recall", "memory", "done"), ("Cheaper turns", "efficiency", "active")])
        kalevala.select(self.project(), 2)
        sel = self.select_md(2)
        self.assertIn("## Landed", sel)
        self.assertRegex(sel, r"T-\d+ \[done\] Memory recall: memory 1 -> 3 \(\+2\)")
        self.assertRegex(sel, r"T-\d+ \[active\] Cheaper turns: efficiency 2 -> 2 \(\+0\)")
        lin = self.lineage()
        self.assertIn("landed", lin.split("## gen-2")[1])
        self.assertRegex(lin, r"T-\d+ \[done\] Memory recall: memory 1 -> 3 \(\+2\)")

    def test_a_child_naming_no_tower_gets_no_delta_and_a_tower_in_its_title_is_found(self):
        from sampo import kalevala
        self.two_gens([("Tidy the plan view", None, "done"), ("Raise memory recall", None, "done")])
        kalevala.select(self.project(), 2)
        sel = self.select_md(2)
        self.assertRegex(sel, r"Tidy the plan view: no delta \(no tower named\)")
        self.assertRegex(sel, r"Raise memory recall: memory 1 -> 3 \(\+2\)")

    def test_a_tower_that_did_not_score_both_gens_has_no_delta(self):
        from sampo import kalevala
        self.two_gens([("Think harder", "problem-solving", "done")])  # no tower graded problem-solving
        kalevala.select(self.project(), 2)
        self.assertRegex(self.select_md(2), r"Think harder: no delta \(problem-solving did not score both gens\)")

    def test_gen_one_has_no_parent_so_each_child_reads_no_delta(self):
        from sampo import kalevala
        p = self.project()
        self.grade("memory", "r1", ["a"])
        kalevala.gen(p)
        self.record(1, self.giant([("First", "memory", "done")]))
        kalevala.select(p, 1)
        self.assertRegex(self.select_md(1), r"First: no delta \(no earlier gen to compare\)")

    def test_a_gen_with_no_giant_task_says_so_and_still_selects(self):
        # T-5552: gen always creates its GIANT now (T-5014), so the no-GIANT case is a gen whose creation failed
        from sampo import kalevala, kalevala_giant
        p = self.project()
        self.grade("memory", "r1", ["a"])
        with mock.patch.object(kalevala_giant, "giant_task", side_effect=RuntimeError("no disk")):
            kalevala.gen(p)
        r = kalevala.select(p, 1)
        self.assertEqual(r["verdict"], "baseline")
        self.assertIn("Landed: no GIANT task recorded for gen 1", self.select_md(1))

    def test_a_missing_giant_or_missing_child_brief_is_named_and_never_fails_select(self):
        from sampo import kalevala
        p = self.project()
        self.grade("memory", "r1", ["a"])
        kalevala.gen(p)
        self.record(1, "T-9999")
        kalevala.select(p, 1)
        self.assertIn("GIANT T-9999 has no brief", self.select_md(1))

    def test_a_child_whose_brief_is_gone_reads_missing(self):
        from sampo import kalevala
        self.two_gens([("Memory recall", "memory", "done")])
        kid = [b for b in c.load_briefs(self.project()) if b.meta.get("big_key") == "m1"][0]
        os.remove(kid.path)
        kalevala.select(self.project(), 2)
        self.assertRegex(self.select_md(2), rf"{kid.id} \[missing\]")

    def test_a_second_select_and_a_lineage_rewrite_keep_one_landed_section(self):
        from sampo import kalevala
        self.two_gens([("Memory recall", "memory", "done")])
        kalevala.select(self.project(), 2)
        kalevala.select(self.project(), 2)
        self.assertEqual(self.select_md(2).count("## Landed"), 1)
        self.assertEqual(self.lineage().count("Memory recall"), 1)

    def test_landed_is_a_pure_reading_of_the_scores_it_is_given(self):
        from sampo import kalevala, kalevala_landed
        self.two_gens([("Memory recall", "memory", "done")])
        p = self.project()
        g = kalevala.load(p)["gens"]["2"]
        prev = kalevala.load(p)["gens"]["1"]
        rows = kalevala_landed.landed(p, g, {"memory": 9, "efficiency": None}, prev)["rows"]
        self.assertEqual([(r["status"], r["tower"], r["delta"]) for r in rows], [("done", "memory", 8)])
        self.assertEqual(kalevala.load(p)["gens"]["2"].get("verdict"), None)  # reading judged nothing

    def _kid_id(self, gid):
        big = c.find_brief(self.project(), gid)
        return [ln for ln in big.section("Children").splitlines() if ln.strip()][0].split("→")[1].split()[0]

    def test_a_landed_that_raises_never_stops_the_verdict_and_select_says_why(self):
        from sampo import kalevala, kalevala_landed
        self.two_gens([("Memory recall", "memory", "done")])
        real = kalevala_landed.landed

        def boom(*a, **k):
            raise RuntimeError("unreadable")
        kalevala_landed.landed = boom
        try:
            kalevala.select(self.project(), 2)
        finally:
            kalevala_landed.landed = real
        sel = self.select_md(2)
        self.assertIn("Verdict: kept", sel)
        self.assertIn("could not read the GIANT task (RuntimeError)", sel)

    def test_more_than_the_row_cap_of_children_lists_the_cap_then_counts_the_rest(self):
        from sampo import kalevala, kalevala_landed
        self.two_gens([("Memory recall", "memory", "done")])
        p = self.project()
        gid = kalevala.load(p)["gens"]["2"]["giant"]
        kid = self._kid_id(gid)
        big = c.find_brief(p, gid)
        rows = [f"m{i} → {kid} (block {i:08x})" for i in range(1, kalevala_landed.MAX_ROWS + 2)]
        big.set_section("Children", "\n".join(rows) + "\n")
        c.save_brief(p, big)
        kalevala.select(p, 2)
        sel = self.select_md(2)
        self.assertEqual(sel.count(f"{kid} [done]"), kalevala_landed.MAX_ROWS)
        self.assertIn("... and 1 more", sel)

    def test_a_hostile_title_becomes_one_clean_line_of_at_most_eighty_characters(self):
        from sampo import kalevala, kalevala_landed
        self.two_gens([("Memory recall", "memory", "done")])
        p = self.project()
        gens = kalevala.load(p)["gens"]
        b = c.find_brief(p, self._kid_id(gens["2"]["giant"]))
        b.preamble = "# Memory\x1b[31m recall\t second line " + "x" * 300 + "\n"
        c.save_brief(p, b)
        res = kalevala_landed.landed(p, gens["2"], {"memory": 3, "efficiency": 2}, gens["1"])
        title = res["rows"][0]["title"]
        self.assertLessEqual(len(title), 80)
        self.assertNotIn("\x1b", title)
        self.assertNotIn("\n", title)
        self.assertTrue(title.startswith("Memory"))
