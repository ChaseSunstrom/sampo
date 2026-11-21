"""T-4246 (big tiers part E, T-4219): sampo big adopt makes an open, planned task a child of a big task (a block in the
Breakdown, a Children line, meta parent, a dependency), sampo big fold merges an unplanned item into a big task and closes
it, and sampo big check refuses a plan that names a folded item nowhere."""
import json

from helpers import ForemanTestCase

from sampo import big
from sampo import core as c

DESIGN = "interface: parse(text) -> list of dicts, in lib/parser.py\n"
C1 = ("[c1] Parser\ntype: FEATURE\ntier: M\nfiles: lib/parser.py\ncovers: 1\n"
      "criterion: the parser reads one block :: grep -q parse lib/parser.py\nstep: write parse()\n")
C2 = ("[c2] Runner\ntype: FEATURE\ntier: S\nfiles: lib/runner.py\ncovers: 2\n"
      "criterion: the runner returns a status :: grep -q run lib/runner.py\n")
INTEGRATION = "criterion: parse and run work together :: grep -q together lib/runner.py\ncovers: 3\n"


class Base(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)

    def newid(self):
        return "T-%04d" % max(c.id_num(b.id) for b in c.load_briefs(self.p))

    def brief(self, tid):
        return c.find_brief(self.p, tid)

    def parent(self, tier="XL", plan=True, acs=3):
        self.fm("task", "new", "Rework everything", "--type", "FEATURE", "--tier", "L", "--ac", "first thing :: true",
                "--step", "write the plan")
        tid = self.newid()
        for n in range(2, acs + 1):
            self.fm("task", "ac", tid, "add", f"criterion {n} :: true")
        b = self.brief(tid)
        b.meta["tier"] = tier
        c.save_brief(self.p, b)
        if plan:
            for name, text in (("Design", DESIGN), ("Breakdown", C1 + "\n" + C2), ("Integration", INTEGRATION)):
                self.fm("task", "set", tid, "--section", name, "--text", text)
        return tid

    def task(self, title="Adopt me", tier="S", scope="lib/adopted.py", ac=True, **meta):
        args = ["task", "new", title, "--type", "FEATURE", "--tier", "S"]
        if scope:
            args += ["--scope", scope]
        if ac:
            args += ["--ac", "it works :: grep -q works lib/adopted.py", "--step", "make it work"]
        self.fm(*args)
        tid = self.newid()
        b = self.brief(tid)
        b.meta["tier"] = tier
        b.meta.update(meta)
        c.save_brief(self.p, b)
        return tid

    def adopt(self, parent, child, *more, check=True):
        return self.fm("big", "adopt", parent, child, *more, check=check)

    def refused(self, *args):
        r = self.fm("big", *args, check=False)
        self.assertNotEqual(r.returncode, 0, r.stdout)
        return r.stdout + r.stderr

    def problems(self, tid):
        r = self.fm("big", "check", tid, "--json", check=False)
        return [f"{x['block']}: {x['problem']}" for x in json.loads(r.stdout)["problems"]]

    def events(self, kind):
        return c.ledger_events(self.p, kinds=[kind])


class Adopt(Base):
    def test_adopt_writes_the_block_the_children_line_the_parent_and_the_dependency(self):
        tid, kid = self.parent(), self.task()
        r = self.adopt(tid, kid)
        self.assertIn("a1", r.stdout)
        p, k = self.brief(tid), self.brief(kid)
        blocks, errs = big.parse_blocks(p.section("Breakdown"))
        self.assertEqual(errs, [])
        a1 = next(x for x in blocks if x["key"] == "a1")
        self.assertEqual((a1["title"], a1["tier"], a1["files"]), ("Adopt me", "S", ["lib/adopted.py"]))
        self.assertEqual(a1["criteria"], [("it works", "grep -q works lib/adopted.py")])
        self.assertEqual(big._children(p)["a1"][0], kid)
        self.assertEqual(big._children(p)["a1"][1], big._sig(a1))
        self.assertIn(kid, p.meta["depends_on"])
        self.assertEqual(k.meta["parent"], tid)
        self.assertEqual(k.meta["big_key"], "a1")
        self.assertEqual([e["data"]["child"] for e in self.events("big_adopt")], [kid])

    def test_an_adopted_child_is_a_child_like_an_expanded_one(self):
        tid, kid = self.parent(), self.task()
        self.adopt(tid, kid)
        r = self.fm("big", "check", tid, "--json")
        data = json.loads(r.stdout)
        self.assertEqual((data["ok"], data["problems"]), (True, []), data)
        self.assertEqual(data["waves"], [["c1", "c2", "a1"]])
        out = json.loads(self.fm("big", "expand", tid, "--json").stdout)
        self.assertEqual(sorted(out["created"]), ["c1", "c2"])  # the adopted one is not made again
        self.assertEqual(out["existing"]["a1"], kid)
        self.assertEqual(out["changed"], {})
        self.assertEqual(len([b for b in c.load_briefs(self.p) if b.meta.get("parent") == tid]), 3)

    def test_adopt_names_the_next_free_key_or_takes_one(self):
        tid, k1, k2, k3 = self.parent(), self.task("One"), self.task("Two", scope="lib/two.py"), self.task("Three", scope="lib/three.py")
        self.adopt(tid, k1)
        self.adopt(tid, k2)
        self.assertEqual(self.brief(k2).meta["big_key"], "a2")
        self.adopt(tid, k3, "--key", "docs")
        self.assertEqual(self.brief(k3).meta["big_key"], "docs")

    def test_adopt_after_takes_block_keys_and_task_ids(self):
        tid, k1, k2, k3 = self.parent(), self.task("One"), self.task("Two", scope="lib/two.py"), self.task("Three", scope="lib/three.py")
        self.adopt(tid, k1)
        self.adopt(tid, k2, "--after", "a1,c1")
        self.adopt(tid, k3, "--after", f"{k1}, c2")
        blocks = {x["key"]: x for x in big.parse_blocks(self.brief(tid).section("Breakdown"))[0]}
        self.assertEqual(blocks["a2"]["after"], ["a1", "c1"])
        self.assertEqual(blocks["a3"]["after"], ["a1", "c2"])  # the id became its key
        self.assertIn(k1, self.brief(k2).meta["depends_on"])
        self.assertIn("a2, a3", self.fm("big", "check", tid).stdout.split("wave 2")[1])  # a1 is in wave 1

    def test_adopt_after_a_block_not_made_yet_is_wired_when_expand_makes_it(self):
        tid, kid = self.parent(), self.task()
        r = self.adopt(tid, kid, "--after", "c1")
        self.assertIn("c1", r.stdout)
        self.assertEqual(self.brief(kid).meta.get("depends_on") or [], [])
        out = json.loads(self.fm("big", "expand", tid, "--json").stdout)
        c1 = out["created"]["c1"]
        self.assertIn(c1, self.brief(kid).meta["depends_on"])
        self.assertEqual(out["late"], {"a1": [c1]})

    def test_adopt_after_an_unknown_key_is_refused_with_the_keys_there(self):
        tid, kid = self.parent(), self.task()
        out = self.refused("adopt", tid, kid, "--after", "zz")
        self.assertIn("zz", out)
        self.assertIn("c1", out)
        self.assertEqual(self.brief(kid).meta.get("parent"), None)

    def test_adopt_twice_says_already_a_child_and_changes_nothing(self):
        tid, kid = self.parent(), self.task()
        self.adopt(tid, kid)
        before = (self.brief(tid).render(), self.brief(kid).render())
        r = self.adopt(tid, kid)
        self.assertIn("already a child", r.stdout)
        self.assertEqual((self.brief(tid).render(), self.brief(kid).render()), before)

    def test_adopt_finishes_a_half_done_adoption(self):
        tid, kid = self.parent(), self.task()
        self.adopt(tid, kid)
        k = self.brief(kid)  # the kill came after the parent was written, before the child was
        for key in ("parent", "big_key", "big_sig"):
            k.meta.pop(key)
        c.save_brief(self.p, k)
        self.adopt(tid, kid)
        self.assertEqual((self.brief(kid).meta["parent"], self.brief(kid).meta["big_key"]), (tid, "a1"))
        self.assertEqual(self.brief(tid).section("Breakdown").count("[a1]"), 1)
        self.assertEqual(self.brief(tid).section("Children").count(kid), 1)

    def test_adopt_finishing_a_half_done_adoption_restores_the_wait_on_siblings(self):
        tid, kid = self.parent(), self.task()
        self.fm("big", "expand", tid)
        sib = next(k.id for k in c.load_briefs(self.p) if k.meta.get("parent") == tid and k.meta.get("big_key") == "c1")
        self.adopt(tid, kid, "--after", "c1")
        self.assertIn(sib, self.brief(kid).meta.get("depends_on") or [])
        k = self.brief(kid)  # the kill came after the parent was written, before the child was
        for key in ("parent", "big_key", "big_sig", "depends_on"):
            k.meta.pop(key, None)
        c.save_brief(self.p, k)
        self.adopt(tid, kid)
        self.assertIn(sib, self.brief(kid).meta.get("depends_on") or [])

    def test_adopt_a_started_parent_takes_a_child(self):
        tid, kid = self.parent(), self.task()
        self.fm("big", "expand", tid)
        b = self.brief(tid)
        b.meta["status"] = "active"
        c.save_brief(self.p, b)
        self.adopt(tid, kid)
        self.assertEqual(self.brief(kid).meta["parent"], tid)

    def test_adopt_records_a_ledger_event_and_the_log(self):
        tid, kid = self.parent(), self.task()
        self.adopt(tid, kid)
        self.assertTrue(self.events("big_adopt"))
        self.assertIn(kid, self.brief(tid).section("Log"))

    def test_adopt_is_a_refusal_for_a_parent_that_is_not_big_or_is_closed(self):
        kid = self.task()
        small = self.parent(tier="M", plan=False)
        self.assertIn("XL", self.refused("adopt", small, kid))
        tid = self.parent()
        self.fm("task", "drop", tid, "no longer wanted")
        self.assertIn("dropped", self.refused("adopt", tid, kid))

    def test_adopt_refuses_a_closed_child_the_parent_itself_and_an_ancestor(self):
        tid, kid = self.parent(), self.task()
        self.fm("task", "drop", kid, "gone")
        self.assertIn("dropped", self.refused("adopt", tid, kid))
        self.assertIn("itself", self.refused("adopt", tid, tid))
        top = self.parent(tier="GIANT", plan=False)
        mid = self.parent()
        b = self.brief(mid)
        b.meta["parent"] = top
        c.save_brief(self.p, b)
        out = self.refused("adopt", mid, top)
        self.assertIn("ancestor", out)

    def test_adopt_refuses_a_child_with_another_parent_and_names_it(self):
        one, two, kid = self.parent(), self.parent(), self.task()
        self.adopt(one, kid)
        out = self.refused("adopt", two, kid)
        self.assertIn(one, out)
        self.assertIn("--move", out)
        self.assertEqual(self.brief(kid).meta["parent"], one)

    def test_adopt_move_takes_the_child_from_its_old_parent(self):
        one, two, kid = self.parent(), self.parent(), self.task()
        self.adopt(one, kid)
        self.adopt(two, kid, "--move")
        self.assertEqual(self.brief(kid).meta["parent"], two)
        self.assertNotIn("[a1]", self.brief(one).section("Breakdown"))
        self.assertIn("[c1]", self.brief(one).section("Breakdown"))  # its other blocks stay
        self.assertNotIn(kid, self.brief(one).section("Children"))
        self.assertNotIn(kid, self.brief(one).meta.get("depends_on") or [])
        self.assertIn(kid, self.brief(two).meta["depends_on"])
        self.assertEqual(self.problems(one), [])

    def test_adopt_move_refuses_a_child_that_has_started(self):
        one, two, kid = self.parent(), self.parent(), self.task()
        self.adopt(one, kid)
        k = self.brief(kid)
        k.meta["plan0_at"] = "2026-11-01T00:00:00"
        c.save_brief(self.p, k)
        out = self.refused("adopt", two, kid, "--move")
        self.assertIn("started", out)
        self.assertEqual(self.brief(kid).meta["parent"], one)

    def test_adopt_a_tier_the_check_would_refuse_says_to_split_or_retier(self):
        tid = self.parent()
        kid = self.task(tier="L")
        out = self.refused("adopt", tid, kid)
        self.assertIn("split it or re-tier it", out)
        self.assertNotIn("[a1]", self.brief(tid).section("Breakdown"))

    def test_adopt_under_a_giant_needs_an_xl_or_huge_child_and_writes_an_outcome(self):
        tid = self.parent(tier="GIANT", plan=False)
        small = self.task("Small")
        self.assertIn("XL or HUGE", self.refused("adopt", tid, small))
        kid = self.task("Big milestone", tier="XL", scope="")
        k = self.brief(kid)
        k.set_section("Interpretation", "The whole parser lands.\nMore words.\n")
        c.save_brief(self.p, k)
        self.adopt(tid, kid)
        a1 = big.parse_blocks(self.brief(tid).section("Breakdown"))[0][0]
        self.assertEqual((a1["tier"], a1["outcome"]), ("XL", "The whole parser lands."))
        self.assertEqual(a1["files"] + a1["criteria"], [])
        other = self.task("No reading", tier="HUGE", scope="")
        self.adopt(tid, other)
        a2 = big.parse_blocks(self.brief(tid).section("Breakdown"))[0][1]
        self.assertEqual(a2["outcome"], "No reading")  # the title when there is no Interpretation

    def test_adopt_a_child_with_no_criterion_or_no_scope_is_sent_to_plan_or_fold(self):
        tid = self.parent()
        no_ac = self.task("No criteria", ac=False)
        out = self.refused("adopt", tid, no_ac)
        self.assertIn("plan it first", out)
        self.assertIn("sampo big fold", out)
        no_scope = self.task("No scope", scope="")
        self.assertIn("scope", self.refused("adopt", tid, no_scope))

    def test_adopt_a_key_already_used_is_refused(self):
        tid, kid = self.parent(), self.task()
        self.assertIn("c1", self.refused("adopt", tid, kid, "--key", "c1"))
        self.assertIn("key", self.refused("adopt", tid, kid, "--key", "no good"))

    def test_adopt_a_child_that_waits_on_the_parent_is_refused(self):
        tid = self.parent()
        kid = self.task(depends_on=[tid])
        self.assertIn("waits on", self.refused("adopt", tid, kid))

    def test_adopt_reports_what_the_check_now_says_about_the_new_block(self):
        tid = self.parent()
        kid = self.task(scope="lib/parser.py")  # the file c1 owns
        out = self.adopt(tid, kid).stdout
        self.assertIn("lib/parser.py", out)
        self.assertIn("after", out)

    def test_adopt_json(self):
        tid, kid = self.parent(), self.task()
        data = json.loads(self.fm("big", "adopt", tid, kid, "--json").stdout)
        self.assertEqual((data["key"], data["child"], data["adopted"]), ("a1", kid, True))


class Fold(Base):
    def item(self, title="Add a dark mode", **kw):
        self.fm("task", "new", title, "--type", "FEATURE", "--tier", "S")
        tid = self.newid()
        b = self.brief(tid)
        b.meta["status"] = "captured"
        b.meta.update(kw)
        c.save_brief(self.p, b)
        return tid

    def fold(self, parent, *items, check=True):
        return self.fm("big", "fold", parent, *items, check=check)

    def test_fold_writes_the_folded_line_and_closes_the_item_with_where_it_went(self):
        tid, it = self.parent(), self.item()
        r = self.fold(tid, it, "--why", "it belongs with the runner")
        p, i = self.brief(tid), self.brief(it)
        line = next(l for l in p.section("Folded").splitlines() if it in l)
        self.assertEqual(line, f"- {it}: Add a dark mode (it belongs with the runner)")
        self.assertEqual(i.status, "dropped")
        self.assertIn(f"folded into {tid}", i.section("Log"))
        self.assertEqual(i.meta.get("folded_into"), tid)
        self.assertIn(it, r.stdout)
        self.assertTrue(self.events("big_fold"))
        self.assertTrue(self.brief(it).path)  # nothing is deleted

    def test_fold_many_in_one_call_and_json(self):
        tid, a, b = self.parent(), self.item("First ask"), self.item("Second ask")
        data = json.loads(self.fm("big", "fold", tid, a, b, "--json").stdout)
        self.assertEqual(sorted(data["folded"]), sorted([a, b]))
        folded = self.brief(tid).section("Folded")
        self.assertIn(a, folded)
        self.assertIn(b, folded)
        self.assertEqual({self.brief(a).status, self.brief(b).status}, {"dropped"})

    def test_fold_twice_changes_nothing(self):
        tid, it = self.parent(), self.item()
        self.fold(tid, it)
        before = self.brief(tid).render()
        r = self.fold(tid, it)
        self.assertIn("already folded", r.stdout)
        self.assertEqual(self.brief(tid).render(), before)

    def test_fold_finishes_when_the_item_was_not_closed(self):
        tid, it = self.parent(), self.item()
        self.fold(tid, it)
        i = self.brief(it)  # the kill came after the parent's line, before the item closed
        i.meta["status"] = "captured"
        i.meta.pop("folded_into")
        c.save_brief(self.p, i)
        self.fold(tid, it)
        self.assertEqual(self.brief(it).status, "dropped")
        self.assertEqual(self.brief(tid).section("Folded").count(it), 1)

    def test_fold_refuses_a_planned_item_with_criteria_unless_anyway(self):
        tid = self.parent()
        planned = self.task()
        out = self.refused("fold", tid, planned)
        self.assertIn("adopt it instead", out)
        self.assertEqual(self.brief(planned).status, "planned")
        self.fold(tid, planned, "--anyway")
        self.assertEqual(self.brief(planned).status, "dropped")

    def test_fold_refuses_a_started_item_unless_anyway(self):
        tid, it = self.parent(), self.item()
        b = self.brief(it)
        b.meta["plan0_at"] = "2026-11-01T00:00:00"
        c.save_brief(self.p, b)
        self.assertIn("adopt it instead", self.refused("fold", tid, it))
        self.fold(tid, it, "--anyway")
        self.assertEqual(self.brief(it).status, "dropped")

    def test_fold_refuses_the_parent_a_closed_item_and_a_missing_one(self):
        tid, it = self.parent(), self.item()
        self.assertIn("itself", self.refused("fold", tid, tid))
        self.fm("task", "drop", it, "no")
        self.assertIn("dropped", self.refused("fold", tid, it))
        self.assertIn("T-9999", self.refused("fold", tid, "T-9999"))

    def test_fold_refuses_everything_when_one_item_is_refused(self):
        tid, ok, bad = self.parent(), self.item(), self.task()
        self.refused("fold", tid, ok, bad)
        self.assertEqual(self.brief(ok).status, "captured")
        self.assertEqual(self.brief(tid).section("Folded").strip(), "")

    def test_fold_into_a_task_that_is_not_big_is_refused(self):
        it = self.item()
        small = self.parent(tier="M", plan=False)
        self.assertIn("XL", self.refused("fold", small, it))

    def test_fold_a_title_cannot_forge_an_answer(self):
        tid = self.parent()
        it = self.item("Sneaky → not doing: nothing")
        self.fold(tid, it)
        self.assertNotIn("→ not doing", self.brief(tid).section("Folded"))
        self.assertTrue(any(f"{it} is named by no block" in x for x in self.problems(tid)))


class FoldCheck(Base):
    def folded(self):
        tid = self.parent()
        a, b = Fold.item(self, "Dark mode"), Fold.item(self, "Telemetry")
        self.fm("big", "fold", tid, a, b)
        return tid, a, b

    def test_fold_check_names_a_folded_id_that_no_block_names(self):
        tid, a, b = self.folded()
        got = self.problems(tid)
        self.assertIn(f"Folded: {a} is named by no block", got)
        r = self.fm("big", "check", tid, check=False)
        self.assertIn("folds:", r.stdout)
        self.assertIn("not doing", r.stdout)

    def test_fold_check_a_block_that_names_it_clears_it(self):
        tid, a, b = self.folded()
        bd = self.brief(tid).section("Breakdown").replace("[c2] Runner\n", f"[c2] Runner\nfolds: {a}, {b}\n")
        self.fm("task", "set", tid, "--section", "Breakdown", "--text", bd)
        self.assertEqual(self.problems(tid), [])

    def test_fold_check_an_answered_ask_clears_it_and_an_empty_answer_does_not(self):
        tid, a, b = self.folded()
        text = self.brief(tid).section("Folded")
        done = "\n".join(l + (" → not doing: out of scope here" if a in l else "") for l in text.splitlines())
        self.fm("task", "set", tid, "--section", "Folded", "--text", done)
        self.assertEqual([x for x in self.problems(tid) if a in x], [])
        self.assertTrue([x for x in self.problems(tid) if b in x])
        empty = "\n".join(l + (" → not doing:" if b in l else "") for l in done.splitlines())
        self.fm("task", "set", tid, "--section", "Folded", "--text", empty)
        self.assertTrue([x for x in self.problems(tid) if b in x])

    def test_fold_check_a_fold_naming_a_task_that_is_not_folded_is_a_problem(self):
        tid = self.parent()
        bd = self.brief(tid).section("Breakdown").replace("[c2] Runner\n", "[c2] Runner\nfolds: T-0999\n")
        self.fm("task", "set", tid, "--section", "Breakdown", "--text", bd)
        self.assertTrue(any(x.startswith("c2:") and "T-0999" in x and "Folded" in x for x in self.problems(tid)), self.problems(tid))
        bd = bd.replace("folds: T-0999", "folds: banana")
        self.fm("task", "set", tid, "--section", "Breakdown", "--text", bd)
        self.assertTrue(any(x.startswith("c2:") and "banana" in x for x in self.problems(tid)), self.problems(tid))

    def test_fold_check_a_giant_milestone_carries_folds(self):
        tid = self.parent(tier="GIANT", plan=False)
        it = Fold.item(self, "Dark mode")
        self.fm("big", "fold", tid, it)
        ms = "[m1] One\ntier: XL\noutcome: one lands\ncovers: 1, 2, 3\n"
        for name, text in (("Design", DESIGN), ("Breakdown", ms + "\n[m2] Two\ntier: XL\noutcome: two\nafter: m1\n"),
                           ("Integration", INTEGRATION)):
            self.fm("task", "set", tid, "--section", name, "--text", text)
        self.assertIn(f"Folded: {it} is named by no block", self.problems(tid))
        self.fm("task", "set", tid, "--section", "Breakdown", "--text",
                ms + f"folds: {it}\n\n[m2] Two\ntier: XL\noutcome: two\nafter: m1\n")
        self.assertEqual(self.problems(tid), [])

    def test_fold_a_folds_line_added_after_a_child_was_made_leaves_it_unchanged(self):
        tid, a, b = self.folded()
        bd = self.brief(tid).section("Breakdown").replace("[c2] Runner\n", f"[c2] Runner\nfolds: {a}\n")
        self.fm("task", "set", tid, "--section", "Breakdown", "--text", bd)
        text = self.brief(tid).section("Folded")
        done = "\n".join(l + (" → not doing: out of scope" if b in l else "") for l in text.splitlines())
        self.fm("task", "set", tid, "--section", "Folded", "--text", done)
        self.fm("big", "expand", tid)
        self.fm("task", "set", tid, "--section", "Breakdown", "--text", bd.replace(f"folds: {a}\n", f"folds: {a}, {b}\n"))
        r = self.fm("big", "expand", tid, "--json")
        self.assertEqual(json.loads(r.stdout)["changed"], {})

    def test_fold_check_a_started_parent_still_names_every_folded_item(self):
        tid, a, b = self.folded()
        bd = self.brief(tid).section("Breakdown").replace("[c2] Runner\n", f"[c2] Runner\nfolds: {a}, {b}\n")
        self.fm("task", "set", tid, "--section", "Breakdown", "--text", bd)
        p = self.brief(tid)
        p.meta["plan0_at"] = "2026-01-01T00:00:00Z"  # started
        c.save_brief(self.p, p)
        self.assertEqual(big.gate_gaps(self.brief(tid), self.repo), [])
        self.fm("task", "set", tid, "--section", "Breakdown", "--text", bd.replace(f"folds: {a}, {b}\n", ""))
        self.assertTrue(big.gate_gaps(self.brief(tid), self.repo))

    def test_fold_check_the_signature_of_an_old_block_does_not_change(self):
        blocks, _ = big.parse_blocks(C1)
        self.assertEqual(big._sig(blocks[0]), big._sig({k: v for k, v in blocks[0].items() if k != "folds"}))

    def test_fold_check_the_guides_and_the_checks_show_folds(self):
        self.assertIn("folds:", big.GUIDE_BREAKDOWN)
        self.assertIn("folds:", big.GUIDE_GIANT)
        self.assertTrue(any("folded" in x.lower() for x in big.CHECKS))
        self.assertIn("folds", big.KEYS)

    def test_expand_copies_the_folded_asks_into_the_childs_raw_request(self):
        tid, a, b = self.folded()
        bd = self.brief(tid).section("Breakdown").replace("[c2] Runner\n", f"[c2] Runner\nfolds: {a}\n")
        self.fm("task", "set", tid, "--section", "Breakdown", "--text", bd)
        text = self.brief(tid).section("Folded")
        done = "\n".join(l + (" → not doing: out of scope" if b in l else "") for l in text.splitlines())
        self.fm("task", "set", tid, "--section", "Folded", "--text", done)
        self.fm("big", "expand", tid)
        kids = {k.title: k for k in c.load_briefs(self.p) if k.meta.get("parent") == tid}
        self.assertIn("Dark mode", kids["Runner"].section("Raw request"))
        self.assertIn(a, kids["Runner"].section("Raw request"))
        self.assertNotIn("Dark mode", kids["Parser"].section("Raw request"))
        self.assertNotIn("Telemetry", kids["Runner"].section("Raw request"))
