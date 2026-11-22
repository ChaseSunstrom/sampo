"""fm batch (T-0257): related small requests worked as one host task — one plan, one gate run, one review, one commit —
each still closed honestly, done in the host."""
import json

from helpers import ForemanTestCase, valid_m_plan


class Batch(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.ids = [self.capture(f"FEATURE: small thing {i}\nDONE-WHEN: thing {i} works") for i in range(3)]

    def capture(self, block):
        out = self.fm("intake", block).stdout
        return next(w for w in out.split() if w.startswith("T-"))

    def state(self):
        return json.loads(self.fm("state", "--json").stdout)

    def test_a_batch_is_one_host_with_every_members_criteria(self):
        res = json.loads(self.fm("batch", *self.ids, "--json").stdout)
        host = res["id"]
        show = self.fm("task", "show", host).stdout
        for i, tid in enumerate(self.ids):
            self.assertIn(f"{tid}: thing {i} works", show)
            self.assertIn(f"{tid}: Small thing {i}", show.split("## Steps", 1)[1])
        inbox = [x["id"] for x in self.state()["inbox"]]
        self.assertFalse(set(self.ids) & set(inbox), "members leave the inbox while batched")
        self.assertIn(host, [x["id"] for x in self.state()["queue"]] + inbox)
        self.assertEqual(res["tier"], "M", "three small ones are one medium task")

    def test_a_host_held_by_a_lane_keeps_its_members_out_of_the_inbox(self):
        # T-2317: once a builder lane held the host, lane_view hid it and its members came back as inbox items
        from sampo import core as c
        host = json.loads(self.fm("batch", *self.ids, "--json").stdout)["id"]
        p = c.find_project(self.repo)
        b = c.find_brief(p, host)
        b.meta["lane"] = "/elsewhere/a-builder-worktree"
        c.save_brief(p, b)
        inbox = [x["id"] for x in self.state()["inbox"]]
        self.assertFalse(set(self.ids) & set(inbox), inbox)
        self.assertNotIn(self.ids[0], self.fm("next").stdout)

    def test_a_done_when_verify_command_survives_the_batch(self):
        # T-1976: the DONE-WHEN fallback kept "x :: cmd" as text with no verify, so fm focus refused the batch
        ids = [self.capture(f"FEATURE: other thing {i}\nDONE-WHEN: other {i} works :: true") for i in range(2)]
        show = self.fm("task", "show", json.loads(self.fm("batch", *ids, "--json").stdout)["id"]).stdout
        self.assertIn(f"{ids[0]}: other 0 works — verify with `true`", show)

    def test_done_when_splits_on_the_last_separator_and_takes_both_forms(self):  # T-2789
        i = self.capture("FEATURE: x\nDONE-WHEN: a :: b :: true")
        j = self.capture("FEATURE: y\nDONE-WHEN: c — verify with `true`")
        show = self.fm("task", "show", json.loads(self.fm("batch", i, j, "--json").stdout)["id"]).stdout
        self.assertIn(f"{i}: a :: b — verify with `true`", show)
        self.assertIn(f"{j}: c — verify with `true`", show)

    def test_the_host_closing_closes_its_members(self):
        host = json.loads(self.fm("batch", *self.ids, "--json").stdout)["id"]
        from sampo import core as c
        gaps = c.plan_gaps(c.find_brief(c.find_project(self.repo), host), "full")
        self.assertFalse([g for g in gaps if "eading" in g], gaps)  # T-2335: the generator writes the readings itself
        self.fm("task", "set", host, "--section", "Approach (options → choice → why)", "--text", valid_m_plan()[1])
        self.fm("task", "set", host, "--section", "Acceptance criteria", "--text", "".join(
            f"- [ ] {tid}: thing {i} works — verify with `true`\n" for i, tid in enumerate(self.ids)))
        self.fm("focus", host)
        for n in range(1, 4):
            self.fm("task", "ac", host, "check", str(n), "--evidence", "true", "ok")
        for n in range(1, 4):
            self.fm("task", "step", host, "done", str(n), "--evidence", "true", "ok")
        for lens in ("self", "intent", "edge"):
            self.fm("task", "audit", host, lens, "x", "ok")
        self.fm("task", "set", host, "--section", "Docs impact", "--text", "none: test")
        self.fm("task", "set", host, "--section", "Regression test", "--text", "none: fixture")
        self.fm("task", "done", host, "--lesson", "batched")
        for tid in self.ids:
            show = json.loads(self.fm("task", "show", tid, "--json").stdout)
            self.assertEqual((show["meta"]["status"], show["meta"].get("done_in")), ("done", host))

    def test_started_or_single_items_are_refused(self):
        self.assertNotEqual(self.fm("batch", self.ids[0], check=False).returncode, 0, "one item isn't a batch")
        self.fm("task", "set", self.ids[0], "tier=S")
        self.fm("task", "set", self.ids[0], "--section", "Interpretation", "--text", "x")
        self.fm("task", "ac", self.ids[0], "add", "x :: true")
        self.fm("task", "step", self.ids[0], "add", "x")
        self.fm("focus", self.ids[0])
        out = self.fm("batch", *self.ids, check=False)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn(self.ids[0], out.stdout + out.stderr)

    def test_review_findings_hold(self):
        # T-0257 review: explore items need the user first; urgency carries; a host gone any way frees its members
        explore = self.capture("FEATURE?: maybe a thing")
        self.assertNotEqual(self.fm("batch", self.ids[0], explore, check=False).returncode, 0)
        urgent = self.capture("FEATURE!: urgent thing")
        self.fm("task", "step", self.ids[1], "add", "write it")
        self.fm("task", "step", self.ids[1], "add", "test it")
        host = json.loads(self.fm("batch", self.ids[1], urgent, "--json").stdout)["id"]
        show = json.loads(self.fm("task", "show", host, "--json").stdout)
        self.assertEqual(show["meta"]["priority"], "urgent")
        steps = self.fm("task", "show", host).stdout.split("## Steps", 1)[1]
        self.assertIn(f"{self.ids[1]}: write it", steps)
        self.assertIn(f"{self.ids[1]}: test it", steps)
        self.fm("task", "drop", host, "abandoned")  # a host that ends without closing them frees its members
        inbox = [x["id"] for x in self.state()["inbox"]]
        self.assertIn(self.ids[1], inbox)
        self.assertIn(urgent, inbox)

    def test_next_suggests_a_batch_of_small_ones(self):
        for tid in self.ids:
            self.fm("task", "set", tid, "tier=S")
        self.assertIn(f"sampo batch {' '.join(self.ids)}", self.fm("next").stdout)


class BriefReadyBatches(ForemanTestCase):
    """T-5478: `lane brief --ready` hands related small tasks (the same files) to one builder as one batch."""
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.fm("lane", "max", "5")

    def mk(self, title, scope, type_="FEATURE", tier="S"):
        tid = json.loads(self.fm("task", "new", title, "--type", type_, "--tier", tier, "--ac", "ok :: true",
                                 "--step", "s", "--json").stdout)["id"]
        self.fm("task", "set", tid, f"scope={scope}")
        return tid

    def ready(self):
        return json.loads(self.fm("lane", "brief", "--ready", "--json").stdout)

    def test_brief_ready_batches_related_s(self):
        import os
        from sampo import core as c
        ids = [self.mk(f"Alpha {w}", "plugin/lib/sampo/lanes.py") for w in ("one", "two", "three")]
        out = self.ready()
        self.assertEqual(len(out["briefed"]), 1, out)
        host = out["briefed"][0]["id"]
        self.assertNotIn(host, ids)
        self.assertEqual(out["batched"], [{"host": host, "members": ids}])
        self.assertEqual(out["skipped"], [], "the members are not listed as skipped: the host replaced them")
        audits = os.path.join(c.find_project(self.repo).dir, "audits")
        self.assertTrue(os.path.exists(os.path.join(audits, f"{host}.builder.md")))
        for tid in ids:
            self.assertFalse(os.path.exists(os.path.join(audits, f"{tid}.builder.md")), "no per-member brief")

    def test_brief_ready_text_says_what_it_batched(self):
        ids = [self.mk(f"Beta {w}", "docs/x.md") for w in ("one", "two")]
        text = self.fm("lane", "brief", "--ready").stdout
        host = next(w for w in text.split() if w.startswith("T-") and w.rstrip(":") not in ids).rstrip(":")
        self.assertIn(f"batched 2 into {host} ({', '.join(ids)})", text)

    def test_brief_ready_leaves_unrelated(self):
        ids = [self.mk(f"Gamma {i}", f"plugin/lib/sampo/mod{i}.py") for i in range(3)]
        out = self.ready()
        self.assertEqual({x["id"] for x in out["briefed"]}, set(ids))
        self.assertEqual(out["batched"], [])

    def test_brief_ready_batch_is_capped_at_four(self):
        ids = [self.mk(f"Delta {i}", "plugin/a.py") for i in range(5)]
        out = self.ready()
        self.assertEqual(len(out["batched"]), 1, out)
        self.assertEqual(len(out["batched"][0]["members"]), 4)
        host = out["batched"][0]["host"]
        self.assertEqual([x["id"] for x in out["briefed"]], [host])
        self.assertEqual({x["id"]: x["reason"] for x in out["skipped"]}, {ids[4]: f"scope overlaps {host}"})

    def test_brief_ready_batch_of_tasks_with_no_verify_is_not_made(self):
        a = json.loads(self.fm("task", "new", "Eps a", "--type", "FEATURE", "--tier", "S", "--ac", "ok",
                               "--step", "s", "--json").stdout)["id"]
        b = json.loads(self.fm("task", "new", "Eps b", "--type", "FEATURE", "--tier", "S", "--ac", "ok",
                               "--step", "s", "--json").stdout)["id"]
        for t in (a, b):
            self.fm("task", "set", t, "scope=plugin/e.py")
        out = self.ready()
        self.assertEqual(out["batched"], [])
        self.assertNotIn("batched_in", json.loads(self.fm("task", "show", a, "--json").stdout)["meta"])


    def ready_in_process(self, patches):
        import argparse
        import contextlib
        import io
        import unittest.mock
        from sampo import cli, core as c, lanes as slanes
        buf, stack = io.StringIO(), contextlib.ExitStack()
        for (mod, name), fn in patches.items():
            stack.enter_context(unittest.mock.patch.object({"cli": cli, "c": c, "lanes": slanes}[mod], name, side_effect=fn))
        with stack, contextlib.redirect_stdout(buf):
            slanes.brief_ready(c.find_project(self.repo), argparse.Namespace(limit=None, json=True))
        return json.loads(buf.getvalue())

    def test_brief_ready_batch_does_not_take_a_file_from_an_urgent_task(self):  # T-5478 review: a host sits at its members' rank
        a, b = self.mk("Zeta a", "plugin/z.py"), self.mk("Zeta b", "plugin/z.py")
        urgent = self.mk("Zeta urgent", "plugin/z.py", type_="FIX")
        self.fm("task", "set", urgent, "priority=urgent")
        out = self.ready()
        self.assertEqual([x["id"] for x in out["briefed"]], [urgent], out)
        self.assertEqual(out["batched"], [], "the host that lost the file is not reported as batched")
        self.assertEqual({x["id"] for x in out["skipped"]} & {a, b}, set(), "its members are under the host")
        self.assertTrue([x for x in out["skipped"] if x["reason"] == f"scope overlaps {urgent}"], out)

    def test_brief_ready_no_batch_holds_the_second_task_back_as_before(self):
        a, b = self.mk("Eta a", "plugin/h.py"), self.mk("Eta b", "plugin/h.py")
        out = json.loads(self.fm("lane", "brief", "--ready", "--no-batch", "--json").stdout)
        self.assertEqual([x["id"] for x in out["briefed"]], [a])
        self.assertEqual(out["batched"], [])
        self.assertEqual({x["id"]: x["reason"] for x in out["skipped"]}, {b: f"scope overlaps {a}"})

    def test_brief_ready_a_host_the_plan_gate_refuses_is_never_made_and_burns_no_id(self):
        from sampo import core as c
        a, b = self.mk("Theta a", "plugin/t.py"), self.mk("Theta b", "plugin/t.py")
        p = c.find_project(self.repo)
        real, before = c.plan_gaps, c.read_meta(p)["next_id"]
        out = self.ready_in_process({("c", "plan_gaps"): lambda t, *a, **k: ["a gap"] if t.id == "T-0000" else real(t, *a, **k)})
        self.assertEqual(out["batched"], [])
        self.assertEqual([x["id"] for x in out["briefed"]], [a], "the tasks stay tasks of their own")
        self.assertEqual(c.read_meta(p)["next_id"], before, "no task id was taken for the refused host")
        self.assertEqual(len(c.load_briefs(p)), 2)
        self.assertNotIn("batched_in", c.find_brief(p, b).meta)

    def test_brief_ready_a_group_make_batch_refuses_or_crashes_leaves_the_other_groups(self):
        from sampo import cli
        a = [self.mk(f"Iota {i}", "plugin/i.py") for i in range(2)]
        b = [self.mk(f"Kappa {i}", "plugin/k.py") for i in range(2)]
        real, calls = cli._make_batch, []

        def flaky(*args, **kw):
            calls.append(args[1])
            if len(calls) == 1:
                raise RuntimeError("lock timeout")  # a LockTimeout or any crash: this group is skipped, not the run
            return real(*args, **kw)
        out = self.ready_in_process({("cli", "_make_batch"): flaky})
        self.assertEqual([x["members"] for x in out["batched"]], [b])
        self.assertEqual({x["id"] for x in out["briefed"]} & set(a), {a[0]}, "the failed group is briefed one by one")

    def test_brief_ready_a_host_builder_brief_refuses_is_not_reported_as_batched(self):
        from sampo import core as c, lanes as slanes
        a, b = self.mk("Lambda a", "plugin/l.py"), self.mk("Lambda b", "plugin/l.py")
        real = slanes.builder_brief

        def refuse(p, t, args, *rest, **kw):
            if t.meta.get("batch"):
                raise c.PolicyError("no")
            return real(p, t, args, *rest, **kw)
        out = self.ready_in_process({("lanes", "builder_brief"): refuse})
        self.assertEqual((out["batched"], out["briefed"]), ([], []))
        self.assertEqual([x["reason"] for x in out["skipped"]], ["no"])


class Related(ForemanTestCase):
    """T-0670: related items of any size are offered as one batch; the brief says how to verify it cheaply."""
    def setUp(self):
        super().setUp()
        self.fm("init")
        cap = lambda block: next(w for w in self.fm("intake", block).stdout.split() if w.startswith("T-"))
        tail = "\nCONTEXT: from the frontier brainstorm, first version and value noted"
        self.guard = [cap(f"SECURITY: guard escape: a heredoc inside backticks hides rm {i}{tail}") for i in range(3)]
        self.other = cap(f"FEATURE: dark mode toggle for the settings page{tail}")
        for t in self.guard + [self.other]:
            self.fm("task", "set", t, "tier=M")

    def test_suggest_groups_related_items_only(self):
        out = self.fm("batch", "--suggest").stdout
        self.assertIn(f"sampo batch {' '.join(self.guard)}", out)
        self.assertNotIn(self.other, out)

    def test_next_offers_the_related_group_for_any_tier(self):
        self.assertIn(f"sampo batch {' '.join(self.guard)}", self.fm("next").stdout)

    def test_apply_creates_the_batches_and_their_brief_says_verify_once(self):
        res = self.fm("batch", "--suggest", "--apply").stdout
        host = next(w for w in res.split() if w.startswith("T-") and w not in self.guard + [self.other])
        show = self.fm("task", "show", host).stdout
        self.assertIn("full gates", show)
        self.assertIn("own new tests", show)
