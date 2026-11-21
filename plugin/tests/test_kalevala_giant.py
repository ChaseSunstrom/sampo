"""T-5014 (a slice of T-4337): kalevala gen also writes one GIANT task for the generation: its Design names the gen's
setup, its Breakdown has one milestone per tower the gen sets, its plan passes sampo big check, and the gen record
keeps its id."""
import os

from helpers import ForemanTestCase, read_text
from sampo import core as c


class KalevalaGiant(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        path = os.path.join(self.tmp, "gen0.md")
        with open(path, "w") as f:
            f.write("root charter\n")
        self.fm("genesis", "init", "--file", path)
        self.fm("kalevala", "start")  # efficiency, memory, problem-solving

    def project(self):
        return c.find_project(self.repo, create=False)

    def giant(self, n=1):
        from sampo import kalevala
        gid = kalevala.load(self.project())["gens"][str(n)]["giant"]
        return gid, c.find_brief(self.project(), gid)

    def test_gen_creates_a_giant_whose_design_names_the_setup_and_whose_breakdown_has_a_milestone_per_tower(self):
        from sampo import big, kalevala
        r = kalevala.gen(self.project())
        gid, b = self.giant()
        self.assertEqual(r["giant"], gid)
        self.assertEqual(b.tier, "GIANT")
        self.assertIn("gen-1/setup.md", b.section("Design"))
        self.assertIn("Gen: 1", b.section("Design"))
        blocks, problems = big.parse_blocks(b.section("Breakdown"))
        self.assertEqual(problems, [])
        self.assertEqual(len(blocks), len(r["set"]))
        for x, name in zip(blocks, r["set"]):
            self.assertIn(name, x["title"])
            self.assertEqual(x["tier"], "XL")
            self.assertTrue(x["outcome"])

    def test_the_plan_skeleton_is_filled_and_passes_big_check(self):
        from sampo import big, kalevala
        kalevala.gen(self.project())
        gid, b = self.giant()
        res = big.analyse(b, self.project().root)
        self.assertEqual(res["problems"], [])
        self.assertTrue(res["ok"])
        self.assertEqual(big.gate_gaps(b), [])
        self.assertEqual(c.plan_gaps(b, "full"), [])  # nothing else stands between the task and its start
        self.assertEqual(self.fm("big", "check", gid, check=False).returncode, 0)

    def test_the_giant_is_marked_with_its_gen_and_a_second_giant_is_not_made_for_one_gen(self):
        from sampo import kalevala, kalevala_giant
        p = self.project()
        kalevala.gen(p)
        gid, b = self.giant()
        self.assertEqual(str(b.meta.get("kalevala_gen")), "1")
        rows = [{"name": n, "aspect": n} for n in kalevala.load(p)["gens"]["1"]["set"]]
        self.assertEqual(kalevala_giant.giant_task(p, 1, rows).id, gid)  # found by its gen mark, not made again
        self.assertEqual(len([x for x in c.load_briefs(p) if str(x.meta.get("kalevala_gen")) == "1"]), 1)
        with self.assertRaises(ValueError):
            kalevala_giant.giant_task(p, 9, [])  # no tower, no milestone

    def test_a_failing_giant_leaves_the_recorded_gen_and_says_why(self):
        from unittest import mock
        from sampo import kalevala, kalevala_giant
        p = self.project()
        with mock.patch.object(kalevala_giant, "giant_task", side_effect=RuntimeError("no disk")):
            r = kalevala.gen(p)
        self.assertIsNone(r["giant"])
        self.assertIn("no disk", r["giant_error"])
        self.assertNotIn("giant", kalevala.load(p)["gens"]["1"])
        self.assertTrue(os.path.exists(os.path.join(kalevala.home(p), "gen-1", "setup.md")))

    def test_gen_again_retries_the_missing_giant_instead_of_refusing(self):
        from unittest import mock
        from sampo import kalevala, kalevala_giant
        p = self.project()
        with mock.patch.object(kalevala_giant, "giant_task", side_effect=RuntimeError("no disk")):
            kalevala.gen(p)
        r = kalevala.gen(p)  # parent unselected, but gen 1 has no GIANT: retry it, not a gen 2
        self.assertEqual(r["gen"], 1)
        self.assertTrue(r["giant"])
        self.assertEqual(kalevala.load(p)["gens"]["1"]["giant"], r["giant"])
        self.assertNotIn("2", kalevala.load(p)["gens"])

    def test_the_next_gen_gets_its_own_giant(self):
        from sampo import kalevala
        p = self.project()
        kalevala.gen(p)
        g1 = self.giant(1)[0]
        for t in ("memory",):  # a graded line so select can judge gen 1
            from sampo import rune
            import json
            rune.set_tower(t)
            try:
                with open(os.path.join(rune.home(p), "exam.jsonl"), "a") as f:
                    f.write(json.dumps({"agent": "foreman", "run": "r1", "model": "sonnet", "split": "train",
                                        "closed": {"train": ["a"], "holdout": []}}) + "\n")
            finally:
                rune.set_tower(None)
        kalevala.select(p, 1)
        kalevala.gen(p)
        g2 = self.giant(2)[0]
        self.assertNotEqual(g1, g2)
        self.assertIn("gen-2/setup.md", self.giant(2)[1].section("Design"))
