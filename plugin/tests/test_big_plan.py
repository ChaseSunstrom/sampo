"""T-4221 (big tiers part B, T-4219): a big task (XL, HUGE, GIANT) cannot start until its brief holds a full plan whose
structure code checks (sampo big check), and sampo big expand turns the Breakdown into child tasks, idempotently."""
import json

from helpers import ForemanTestCase

from sampo import big
from sampo import core as c

DESIGN = ("The parser reads blocks. interface: parse(text) -> list of dicts, in lib/parser.py\n"
          "The runner calls parse. interface: run(blocks) -> int, in lib/runner.py\n"
          "Nothing outside lib/ may change.\n")
C1 = ("[c1] Parser\ntype: FEATURE\ntier: M\nfiles: lib/parser.py, tests/test_parser.py\ncovers: 1\n"
      "criterion: the parser reads one block :: grep -q parse lib/parser.py\nstep: write parse() in lib/parser.py\n")
C2 = ("[c2] Runner\ntype: FEATURE\ntier: S\nfiles: lib/runner.py\ncovers: 2\n"
      "criterion: the runner returns a status :: grep -q run lib/runner.py\n")
C3 = ("[c3] Wire them\ntype: FEATURE\ntier: S\nfiles: lib/parser.py\nafter: c1, c2\ncovers: 3\n"
      "criterion: wired :: grep -q wire lib/parser.py\nstep: wire parse into run\n")
INTEGRATION = "criterion: parse and run work together :: grep -q together lib/runner.py\n"


def breakdown(*blocks):
    return "\n".join(blocks)


class Base(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)

    def make(self, tier="XL", acs=3, title="Rework everything"):
        args = ["task", "new", title, "--type", "FEATURE", "--tier", "L", "--ac", "first thing :: true",
                "--step", "write the plan, expand it, land the children"]
        r = self.fm(*args)
        tid = "T-%04d" % max(c.id_num(b.id) for b in c.load_briefs(self.p))
        for n in range(2, acs + 1):
            self.fm("task", "ac", tid, "add", f"criterion {n} :: true")
        b = c.find_brief(self.p, tid)
        b.meta["tier"] = tier  # part A owns the tier literals the CLI checks
        c.save_brief(self.p, b)
        return tid

    def plan(self, tid, bd=None, design=DESIGN, integration=INTEGRATION):
        for name, text in (("Design", design), ("Breakdown", bd), ("Integration", integration)):
            if text is not None:
                self.fm("task", "set", tid, "--section", name, "--text", text)

    def good(self, tid, **kw):
        self.plan(tid, breakdown(C1, C2, C3), **kw)

    def check(self, tid):
        r = self.fm("big", "check", tid, "--json", check=False)
        return r.returncode, json.loads(r.stdout)

    def problems(self, tid):
        return [f"{x['block']}: {x['problem']}" for x in self.check(tid)[1]["problems"]]

    def kids(self, tid):
        return sorted((b for b in c.load_briefs(self.p) if b.meta.get("parent") == tid), key=lambda b: b.id)


class Check(Base):
    def test_check_a_good_plan_passes_and_gives_the_waves(self):
        tid = self.make()
        self.good(tid)
        code, data = self.check(tid)
        self.assertEqual((code, data["problems"], data["ok"]), (0, [], True))
        self.assertEqual(data["waves"], [["c1", "c2"], ["c3"]])

    def test_check_an_uncovered_criterion_names_it(self):
        tid = self.make()
        self.plan(tid, breakdown(C1, C2, C3.replace("covers: 3\n", "")))
        code, _ = self.check(tid)
        self.assertEqual(code, 1)
        self.assertTrue(any("criterion 3" in x for x in self.problems(tid)), self.problems(tid))

    def test_check_integration_may_cover_a_criterion(self):
        tid = self.make()
        self.plan(tid, breakdown(C1, C2, C3.replace("covers: 3\n", "")), integration=INTEGRATION + "covers: 3\n")
        self.assertEqual(self.problems(tid), [])

    def test_check_a_child_above_m_is_named(self):
        tid = self.make()
        self.plan(tid, breakdown(C1.replace("tier: M", "tier: L"), C2, C3))
        self.assertTrue(any(x.startswith("c1:") and "above M" in x for x in self.problems(tid)), self.problems(tid))

    def test_check_a_cycle_and_an_unknown_after_key(self):
        tid = self.make()
        self.plan(tid, breakdown(C1 + "after: c3\n", C2, C3))
        self.assertTrue(any("cycle" in x and "c1" in x and "c3" in x for x in self.problems(tid)), self.problems(tid))
        self.plan(tid, breakdown(C1, C2, C3.replace("after: c1, c2", "after: c1, c9")))
        self.assertTrue(any(x.startswith("c3:") and "c9" in x for x in self.problems(tid)), self.problems(tid))

    def test_check_a_file_shared_in_one_wave_is_a_problem_and_ordering_fixes_it(self):
        tid = self.make()
        self.plan(tid, breakdown(C1, C2.replace("lib/runner.py", "lib/parser.py"), C3))
        got = self.problems(tid)
        self.assertTrue(any("lib/parser.py" in x and "c1" in x and "c2" in x for x in got), got)
        self.plan(tid, breakdown(C1, C2.replace("lib/runner.py", "lib/parser.py") + "after: c1\n", C3))
        self.assertEqual(self.problems(tid), [])

    def test_check_a_child_needs_a_criterion_that_can_fail(self):
        tid = self.make()
        self.plan(tid, breakdown(C1, C2.replace("criterion: the runner returns a status :: grep -q run lib/runner.py\n", ""), C3))
        self.assertTrue(any(x.startswith("c2:") and "no criterion" in x for x in self.problems(tid)), self.problems(tid))
        self.plan(tid, breakdown(C1, C2.replace("grep -q run lib/runner.py", "true"), C3))
        self.assertTrue(any(x.startswith("c2:") and "can't fail" in x for x in self.problems(tid)), self.problems(tid))
        self.plan(tid, breakdown(C1, C2.replace(" :: grep -q run lib/runner.py", ""), C3))
        self.assertTrue(any(x.startswith("c2:") and "verify" in x for x in self.problems(tid)), self.problems(tid))

    def test_check_design_and_integration_must_be_filled(self):
        tid = self.make()
        self.plan(tid, breakdown(C1, C2, C3), design="", integration="")
        got = self.problems(tid)
        self.assertTrue(any(x.startswith("Design:") for x in got), got)
        self.assertTrue(any(x.startswith("Integration:") for x in got), got)
        self.plan(tid, None, integration="covers: 3\n")  # a covers line is no criterion
        self.assertTrue(any(x.startswith("Integration:") and "criterion" in x for x in self.problems(tid)))

    def test_check_the_size_of_the_tier_is_a_warning_naming_the_tier_that_fits(self):
        tid = self.make()
        self.plan(tid, breakdown(C1, C2.replace("covers: 2", "covers: 2, 3"), ))
        code, data = self.check(tid)
        self.assertEqual(code, 0, data)
        self.assertTrue(any("2 children" in w["problem"] and "L" in w["fix"] for w in data["warnings"]), data["warnings"])

    def test_check_a_malformed_line_names_the_line(self):
        tid = self.make()
        self.plan(tid, breakdown(C1, C2 + "colour: red\n", C3))
        got = self.problems(tid)
        self.assertTrue(any("colour" in x and "c2" in x for x in got), got)
        self.plan(tid, breakdown("stray words\n" + C1, C2, C3))
        self.assertTrue(any("stray words" in x for x in self.problems(tid)))


class Plan(Base):
    def test_plan_writes_the_skeleton_once_and_never_over_a_filled_section(self):
        tid = self.make()
        r = self.fm("big", "plan", tid)
        b = c.find_brief(self.p, tid)
        for name in ("Design", "Breakdown", "Integration"):
            self.assertIn(name, [h.strip() for h, _ in b.sections])
        self.assertIn("[c1]", b.section("Breakdown"))  # the guide shows one block
        self.assertIn("sampo big check", r.stdout)
        self.assertIn("covered", r.stdout)  # the checks that will be applied
        self.plan(tid, breakdown(C1, C2, C3))
        self.fm("big", "plan", tid)
        self.assertIn("[c3]", c.find_brief(self.p, tid).section("Breakdown"))
        self.assertEqual(big.parse_blocks(c.find_brief(self.p, tid).section("Breakdown"))[1], [])

    def test_plan_of_an_unfilled_skeleton_is_still_a_problem(self):
        tid = self.make()
        self.fm("big", "plan", tid)
        got = self.problems(tid)
        self.assertTrue(any(x.startswith("Breakdown:") for x in got), got)
        self.assertTrue(any(x.startswith("Design:") for x in got), got)

    def test_plan_refuses_a_task_that_is_not_big(self):
        tid = self.make(tier="M")
        r = self.fm("big", "plan", tid, check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("XL", r.stdout + r.stderr)


class Gate(Base):
    def refused(self, tid):
        r = self.fm("focus", tid, check=False)
        self.assertNotEqual(r.returncode, 0, r.stdout)
        return r.stdout + r.stderr

    def test_gate_refuses_a_big_task_with_no_plan_naming_the_verbs(self):
        tid = self.make()
        out = self.refused(tid)
        self.assertIn(f"sampo big check {tid}", out)
        self.assertIn("Breakdown", out)

    def test_gate_refuses_an_uncovered_criterion(self):
        tid = self.make()
        self.plan(tid, breakdown(C1, C2, C3.replace("covers: 3\n", "")))
        self.assertIn("criterion 3", self.refused(tid))

    def test_gate_refuses_a_child_above_m(self):
        tid = self.make()
        self.plan(tid, breakdown(C1.replace("tier: M", "tier: L"), C2, C3))
        self.assertIn("above M", self.refused(tid))

    def test_gate_refuses_a_cycle(self):
        tid = self.make()
        self.plan(tid, breakdown(C1 + "after: c3\n", C2, C3))
        self.assertIn("cycle", self.refused(tid))

    def test_gate_refuses_a_file_shared_in_a_wave(self):
        tid = self.make()
        self.plan(tid, breakdown(C1, C2.replace("lib/runner.py", "lib/parser.py"), C3))
        self.assertIn("lib/parser.py", self.refused(tid))

    def test_gate_shows_three_problems_and_counts_the_rest(self):
        tid = self.make()
        self.plan(tid, breakdown(C1.replace("tier: M", "tier: L"), C2.replace("lib/runner.py", "lib/parser.py"), C3),
                  design="", integration="")
        out = self.refused(tid)
        self.assertRegex(out, r"\(\+\d+ more\)")

    def test_gate_passes_a_good_plan_and_next_says_focus(self):
        tid = self.make()
        self.good(tid)
        self.assertEqual([g for g in c.plan_gaps(c.find_brief(self.p, tid)) if "plan" in g], [])
        # a big tier is at least L (T-4220): the reading and the risks are still its own sections, and it waits for a yes;
        # its Design is its approach, never asked for twice
        gaps = c.plan_gaps(c.find_brief(self.p, tid))
        self.assertIn("Interpretation", gaps)
        self.assertNotIn("Approach", gaps)
        self.assertTrue(any(g.startswith("approval") for g in gaps), gaps)
        self.fm("task", "set", tid, "--section", "Interpretation", "--text",
                "Reading 1: rework all three parts. Reading 2: only the parser; ruled out, the criteria name all three.")
        self.fm("task", "set", tid, "--section", "Risks and rollback", "--text", "a child breaks main: revert its merge")
        self.fm("task", "set", tid, "--section", "Build vs reuse", "--text", "reuses the parser; nothing else fits")
        self.fm("task", "set", tid, "approved=true")
        self.fm("focus", tid)
        self.assertEqual(c.find_brief(self.p, tid).status, "active")

    def test_gate_next_says_to_write_the_full_plan(self):
        tid = self.make()
        out = c.next_action(c.find_brief(self.p, tid))
        self.assertIn(f"write the full plan: sampo big plan {tid}", out)

    def test_gate_leaves_s_m_and_l_alone(self):
        for tier in ("S", "M", "L"):
            tid = self.make(tier=tier)
            self.assertEqual([g for g in c.plan_gaps(c.find_brief(self.p, tid)) if "full plan" in g], [], tier)


class Expand(Base):
    def test_expand_makes_the_children_with_links_order_scope_and_criteria(self):
        tid = self.make()
        self.good(tid)
        data = json.loads(self.fm("big", "expand", tid, "--json").stdout)
        kids = self.kids(tid)
        self.assertEqual(len(kids), 3)
        by = {k.title: k for k in kids}
        k1, k2, k3 = by["Parser"], by["Runner"], by["Wire them"]
        self.assertEqual(data["created"], {"c1": k1.id, "c2": k2.id, "c3": k3.id})
        self.assertEqual((k1.meta["parent"], k1.tier, k1.type, k1.status), (tid, "M", "FEATURE", "planned"))
        self.assertEqual(k1.meta["scope"], ["lib/parser.py", "lib/runner.py"][:0] + ["lib/parser.py", "tests/test_parser.py"])
        self.assertEqual(k3.meta["depends_on"], sorted([k1.id, k2.id]))
        self.assertEqual(k1.meta["depends_on"], [])
        self.assertEqual([a.n for a in k1.acceptance()], [1])
        self.assertEqual(dict(k1.verify_cmds())[1], "grep -q parse lib/parser.py")
        self.assertEqual([s.text for s in k1.steps()], ["write parse() in lib/parser.py"])
        self.assertTrue(k2.steps(), "a block with no step lines still gets one")
        interp = k1.section("Interpretation")
        self.assertIn("parse(text) -> list of dicts", interp)  # the Design's interface line that names its file
        self.assertNotIn("run(blocks)", interp)
        parent = c.find_brief(self.p, tid)
        self.assertEqual(sorted(parent.meta["depends_on"]), sorted(k.id for k in kids))
        rec = parent.section("Children")
        self.assertIn(f"c1 → {k1.id}", rec)
        self.assertIn(f"c3 → {k3.id}", rec)

    def test_expand_children_can_be_focused_and_inherit_context_and_source(self):
        tid = self.make()
        self.good(tid)
        b = c.find_brief(self.p, tid)
        b.set_section("Raw request", "> FEATURE: rework\n> CONTEXT: the old way is gone\n> MUST: keep the API\n> NEVER: touch lib/old.py\n")
        c.save_brief(self.p, b)
        self.fm("big", "expand", tid)
        k1 = next(k for k in self.kids(tid) if k.title == "Parser")
        raw = k1.section("Raw request")
        for line in ("CONTEXT: the old way is gone", "MUST: keep the API", "NEVER: touch lib/old.py"):
            self.assertIn(line, raw)
        self.assertEqual(k1.meta["source"], c.find_brief(self.p, tid).meta["source"])
        self.assertEqual([g for g in c.plan_gaps(k1)], [])

    def test_expand_a_second_run_makes_nothing(self):
        tid = self.make()
        self.good(tid)
        self.fm("big", "expand", tid)
        before = [k.id for k in self.kids(tid)]
        data = json.loads(self.fm("big", "expand", tid, "--json").stdout)
        self.assertEqual(data["created"], {})
        self.assertEqual([k.id for k in self.kids(tid)], before)
        self.assertEqual(len([b for b in c.load_briefs(self.p)]), 4)

    def test_expand_dry_run_creates_nothing(self):
        tid = self.make()
        self.good(tid)
        r = self.fm("big", "expand", tid, "--dry-run")
        self.assertEqual(self.kids(tid), [])
        self.assertIn("c1", r.stdout)
        self.assertEqual(c.find_brief(self.p, tid).section("Children").strip(), "")

    def test_expand_an_edited_breakdown_adds_only_the_new_block(self):
        tid = self.make()
        self.good(tid)
        self.fm("big", "expand", tid)
        before = {k.id for k in self.kids(tid)}
        c4 = ("[c4] Docs\ntype: FEATURE\ntier: S\nfiles: docs/x.md\nafter: c3\ncovers: 1\n"
              "criterion: documented :: grep -q docs docs/x.md\n")
        self.plan(tid, breakdown(C1, C2, C3, c4))
        data = json.loads(self.fm("big", "expand", tid, "--json").stdout)
        self.assertEqual(list(data["created"]), ["c4"])
        new = [k for k in self.kids(tid) if k.id not in before]
        self.assertEqual(len(new), 1)
        k3 = next(k for k in self.kids(tid) if k.title == "Wire them")
        self.assertEqual(new[0].meta["depends_on"], [k3.id])
        self.assertIn(new[0].id, c.find_brief(self.p, tid).meta["depends_on"])

    def test_expand_refuses_to_drop_a_child_that_has_started(self):
        tid = self.make()
        self.good(tid)
        self.fm("big", "expand", tid)
        k2 = next(k for k in self.kids(tid) if k.title == "Runner")
        k2.meta["status"] = "active"
        c.save_brief(self.p, k2)
        self.plan(tid, breakdown(C1, C3.replace("after: c1, c2", "after: c1").replace("covers: 3", "covers: 2, 3")))
        r = self.fm("big", "expand", tid, check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(k2.id, r.stdout + r.stderr)
        self.assertIn("c2", r.stdout + r.stderr)

    def test_expand_a_dropped_child_that_never_started_is_named_not_destroyed(self):
        tid = self.make()
        self.good(tid)
        self.fm("big", "expand", tid)
        k2 = next(k for k in self.kids(tid) if k.title == "Runner")
        self.plan(tid, breakdown(C1, C3.replace("after: c1, c2", "after: c1").replace("covers: 3", "covers: 2, 3")))
        r = self.fm("big", "expand", tid)
        self.assertIn(k2.id, r.stdout)
        self.assertEqual(c.find_brief(self.p, k2.id).status, "planned")

    def test_expand_a_changed_block_of_an_existing_child_is_reported(self):
        tid = self.make()
        self.good(tid)
        self.fm("big", "expand", tid)
        self.plan(tid, breakdown(C1.replace("covers: 1", "covers: 1\ncriterion: more :: grep -q more lib/parser.py"), C2, C3))
        r = self.fm("big", "expand", tid)
        self.assertIn("changed", r.stdout)

    def test_expand_refuses_while_check_has_problems(self):
        tid = self.make()
        self.plan(tid, breakdown(C1, C2))
        r = self.fm("big", "expand", tid, check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("criterion 3", r.stdout + r.stderr)
        self.assertEqual(self.kids(tid), [])


MILESTONES = ("[m1] Parsing lands\ntier: XL\noutcome: the parser exists and reads every block\ncovers: 1\n\n"
              "[m2] Running lands\ntier: HUGE\noutcome: blocks run end to end\nafter: m1\ncovers: 2, 3\n")


class Giant(Base):
    def test_giant_check_passes_with_outline_only_milestones(self):
        tid = self.make(tier="GIANT")
        self.plan(tid, MILESTONES)
        code, data = self.check(tid)
        self.assertEqual((code, data["problems"]), (0, []), data)
        self.assertEqual(data["waves"], [["m1"], ["m2"]])

    def test_giant_a_milestone_must_be_xl_or_huge(self):
        tid = self.make(tier="GIANT")
        self.plan(tid, MILESTONES.replace("tier: XL", "tier: M"))
        self.assertTrue(any(x.startswith("m1:") and "XL or HUGE" in x for x in self.problems(tid)), self.problems(tid))

    def test_giant_expands_to_milestone_children_whose_plans_come_later(self):
        tid = self.make(tier="GIANT")
        self.plan(tid, MILESTONES)
        data = json.loads(self.fm("big", "expand", tid, "--json").stdout)
        kids = {k.title: k for k in self.kids(tid)}
        m1, m2 = kids["Parsing lands"], kids["Running lands"]
        self.assertEqual((m1.tier, m2.tier, m1.status, m2.status), ("XL", "HUGE", "planned", "planned"))
        self.assertEqual(m2.meta["depends_on"], [m1.id])
        self.assertEqual(data["created"], {"m1": m1.id, "m2": m2.id})
        self.assertIn("the parser exists and reads every block", m1.section("Interpretation") + m1.section("Raw request"))
        for m in (m1, m2):
            self.assertEqual(m.section("Breakdown").strip(), "")  # its own plan is written when it starts
            self.assertEqual(m.meta["parent"], tid)
        # not startable until its own full plan is written
        r = self.fm("focus", m1.id, check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(f"sampo big check {m1.id}", r.stdout + r.stderr)
        self.assertIn(f"write the full plan: sampo big plan {m1.id}", c.next_action(m1))
        # a second run makes none
        self.assertEqual(json.loads(self.fm("big", "expand", tid, "--json").stdout)["created"], {})


class Bounds(Base):
    def test_a_breakdown_past_the_block_cap_is_one_problem_not_a_quadratic_walk(self):
        tid = self.make()
        blocks = "".join(f"[k{i}] Block {i}\ntier: S\nfiles: f{i}.py\ncovers: 1\ncriterion: ok :: grep -q x f{i}.py\n\n"
                         for i in range(big.MAX_BLOCKS + 5))
        self.plan(tid, blocks)
        self.assertTrue(any("more than" in x for x in self.problems(tid)), self.problems(tid))
