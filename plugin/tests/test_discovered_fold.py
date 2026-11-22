"""T-4743: a project's inbox isn't flooded by discovered and followup captures. An item that names a file in an open
task's scope goes on that task's Follow-ups; one that nearly repeats an open task is folded into it; the items of one
report that name the same file make one task, with the items as steps; each trigger has a daily filing allowance that
follows its own record, and what is over it waits in a held list (sampo triggers --held) that the next day files
best-first. The user's own captures and a red build are never folded or held."""
import datetime
import os
import re

from helpers import ForemanTestCase

from sampo import core as c
from sampo import triggers


class Fold(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)

    def tasks(self):
        return c.load_briefs(self.p)

    def new(self, title, scope=None, type_="FIX"):
        args = ["task", "new", title, "--type", type_, "--tier", "S"]
        return self.fm_json(*args, *(["--scope", scope] if scope else []))["id"]

    def noticed(self, lines):
        report = os.path.join(self.tmp, "report.md")
        with open(report, "w") as f:
            f.write("Verdict: approve\n" + "".join(f"Noticed: {x}\n" for x in lines))
        return self.fm("research", "add", "T-review", "--from-agent", report).stdout

    def history(self, trigger, done, dropped, open_=0):
        """Captures of a trigger with closed outcomes, as its record."""
        for status, n in (("done", done), ("dropped", dropped), (None, open_)):
            for _ in range(n):
                self.seq = getattr(self, "seq", 0) + 1
                b = c.find_brief(self.p, triggers.file_item(  # as a capture files: the fold's own filings are what a day counts
                    self.p, {"text": f"FIX: {trigger} finding number {self.seq} of its own", "type": "FIX",
                             "source": trigger, "tier": "S"}))
                if status:
                    b.meta["status"] = status
                    c.save_brief(self.p, b, touch=False)

    def crowd(self, trigger, n):
        """n tasks the trigger filed today through the capture path (what the allowance counts)."""
        for _ in range(n):
            self.seq = getattr(self, "seq", 0) + 1
            triggers.file_item(self.p, {"text": f"{trigger} finding {self.seq} of the day", "type": "FIX",
                                        "source": trigger, "tier": "S"})

    def rollover(self, days=1):
        return datetime.datetime.now().astimezone() + datetime.timedelta(days=days)

    # ---- criterion 1: appended, folded, per area, readable afterwards
    def test_an_item_naming_a_file_in_an_open_tasks_scope_is_appended_to_its_follow_ups(self):
        tid = self.new("Rework the retry loop", scope="plugin/net/retry.py")
        before = len(self.tasks())
        out = self.fm("capture", "FIX: the backoff in plugin/net/retry.py never grows", "--source", "discovered").stdout
        self.assertIn(f"appended to {tid}", out)
        self.assertEqual(len(self.tasks()), before, "no new task")
        b = c.find_brief(self.p, tid)
        self.assertIn("never grows", b.section("Follow-ups captured"))
        self.assertIn("never grows", self.fm("task", "show", tid).stdout)  # readable afterwards
        self.assertEqual(triggers.fold_counts(self.p)["appended"], 1)

    def lane_host(self, **meta):
        tid = self.new("Rework the retry loop", scope="plugin/net/retry.py")
        b = c.find_brief(self.p, tid)
        b.meta.update(meta)
        c.save_brief(self.p, b, touch=False)
        return tid

    def test_a_host_whose_lane_has_merged_gets_no_follow_up_the_item_files_as_its_own_task(self):  # T-5363, lane
        tid = self.lane_host(lane=os.path.join(self.tmp, "wt-merged"), lane_stop=f"merged {c.now()}")
        out = self.fm("capture", "FIX: the backoff in plugin/net/retry.py never grows", "--source", "discovered").stdout
        self.assertNotIn(f"appended to {tid}", out)
        self.assertEqual(c.find_brief(self.p, tid).section("Follow-ups captured").strip(), "")
        self.assertEqual(triggers.fold(self.p, "plugin/net/retry.py loses the first request", "FIX", "discovered"), (None, None))

    def test_a_host_a_builder_holds_in_a_lane_gets_no_follow_up_the_item_files_as_its_own_task(self):  # T-5363, lane
        tid = self.lane_host(lane=os.path.join(self.tmp, "wt-live"), status="active")
        self.assertEqual(triggers.fold(self.p, "plugin/net/retry.py never grows its backoff", "FIX", "discovered"), (None, None))
        self.assertEqual(triggers.fold(self.p, "plugin/net/retry.py never grows its backoff", "FIX", "discovered",
                                       under=tid), (None, None), "not even the task named as the one in hand")
        self.assertEqual(c.find_brief(self.p, tid).section("Follow-ups captured").strip(), "")
        other = self.new("Sweep the net package", scope="plugin/net/*")  # a lane-less host in scope still takes it
        self.assertEqual(triggers.fold(self.p, "plugin/net/retry.py never grows its backoff", "FIX", "discovered"),
                         ("appended", other))

    def test_a_host_with_no_lane_still_gets_the_follow_up(self):  # T-5363
        tid = self.lane_host()
        self.assertEqual(triggers.fold(self.p, "plugin/net/retry.py never grows its backoff", "FIX", "discovered"),
                         ("appended", tid))

    def test_a_near_repeat_of_an_open_task_is_folded_and_the_open_tasks_log_says_so(self):
        tid = self.new("fm lease take prints every lease in the project")
        before = len(self.tasks())
        out = self.fm("capture", "FIX: fm lease take prints every lease in the project, not just its own",
                      "--source", "discovered").stdout
        self.assertIn(f"folded into {tid}", out)
        self.assertEqual(len(self.tasks()), before)
        self.assertIn("not just its own", c.find_brief(self.p, tid).section("Log"))
        self.assertEqual(self.fm("capture", "FIX: fm lease take prints every lease in the project, not its own",
                                 "--source", "user", check=False).returncode, 5, "a user's request is still refused")

    def test_a_reports_items_naming_one_file_make_one_task_with_the_items_as_steps(self):
        out = self.noticed(["plugin/a/one.py drops a BOM line", "plugin/a/one.py never closes its file",
                            "plugin/b/two.py trusts the port it is given", "plugin/a/one.py leaks a handle on retry",
                            "plugin/b/two.py keeps no timeout"])
        found = [b for b in self.tasks() if b.meta.get("source") == "discovered"]
        self.assertEqual(len(found), 2, [b.title for b in found])
        self.assertIn("2 task(s)", out)
        one = next(b for b in found if "one.py" in b.title)
        self.assertEqual(len(one.steps()), 3)
        self.assertTrue(any("never closes" in s.text for s in one.steps()))  # each item readable as a step

    def test_a_report_item_naming_no_file_is_still_its_own_task(self):
        self.noticed(["the CSV parser drops a BOM line", "no test covers an empty export"])
        self.assertEqual(len([b for b in self.tasks() if b.meta.get("source") == "discovered"]), 2)

    # ---- criterion 2: the allowance follows the record; over it is held, shown, and filed first next day
    def test_the_allowance_follows_the_trigger_own_precision(self):
        self.history("discovered", done=2, dropped=18)
        low = triggers.allowance(self.p, "discovered")
        self.history("followup", done=18, dropped=2)
        high = triggers.allowance(self.p, "followup")
        self.assertLess(low, high)
        self.assertGreaterEqual(low, triggers.MIN_ALLOW)
        self.assertIsNone(triggers.allowance(self.p, "ci-red"), "a red build has no allowance")

    def test_items_over_the_allowance_are_held_shown_and_filed_best_first_next_day(self):
        self.history("discovered", done=1, dropped=19)  # precision 0.05: the allowance is 2 of 40 (T-5722); 3 items come
        left = triggers.allowance(self.p, "discovered")
        self.assertEqual(left, 2, "the learned allowance under the 40 a clean record gets")
        n = len(self.tasks())
        out = self.fm("capture", "FEATURE: a prettier banner for the pane", "--source", "discovered").stdout
        self.assertIn("held", out)
        self.fm("capture", "FIX: the lock leaks a descriptor on timeout", "--source", "discovered")
        self.fm("capture", "FIX: the digest crashes on an empty ledger", "--source", "discovered")
        self.assertEqual(len(self.tasks()), n, "nothing filed over the allowance")
        shown = self.fm("triggers", "--held").stdout
        for want in ("prettier banner", "leaks a descriptor", "empty ledger"):
            self.assertIn(want, shown)
        self.assertEqual(len(triggers.held(self.p)), 3)
        tomorrow = datetime.datetime.now().astimezone() + datetime.timedelta(days=1)
        filed = triggers.drain(self.p, now=tomorrow)
        self.assertEqual(len(filed), left, "the day's allowance, no more")
        titles = [c.find_brief(self.p, t).title for t in filed]
        self.assertFalse(any("banner" in t for t in titles), "the FEATURE waits behind the FIXes")
        self.assertEqual([x["text"] for x in triggers.held(self.p)].count("a prettier banner for the pane"), 1)

    def test_a_user_capture_and_a_red_build_are_never_held_or_folded(self):
        self.history("discovered", done=0, dropped=20)
        self.history("ci-red", done=0, dropped=20)
        tid = self.new("Rework the retry loop", scope="plugin/net/retry.py")
        n = len(self.tasks())
        self.fm("capture", "FIX: plugin/net/retry.py loses the first request", "--source", "user")
        self.assertEqual(len(self.tasks()), n + 1, "a user's request files, even inside a scope")
        self.assertEqual(c.find_brief(self.p, tid).section("Follow-ups captured").strip(), "")
        self.assertEqual(triggers.fold(self.p, "FIX: plugin/net/retry.py is red in CI", "FIX", "ci-red"), (None, None))
        self.assertEqual(triggers.fold(self.p, "FIX: anything at all about the lock", "FIX", "user"), (None, None))
        self.assertEqual(triggers.held(self.p), [])

    def test_the_first_firings_of_a_young_trigger_file_freely(self):
        self.assertEqual(triggers.allowance(self.p, "discovered"), triggers.DAILY_MAX)
        self.fm("capture", "FIX: one finding about the lock leak", "--source", "discovered")
        self.assertEqual(triggers.held(self.p), [])

    def test_a_clean_record_allows_forty_and_a_budget_the_user_sets_raises_it_to_sixty(self):  # T-5722
        self.assertEqual(triggers.allowance(self.p, "discovered"), 40, "a clean record files forty a day")
        self.fm("triggers", "set", "discovered", "--budget", "60")
        self.assertEqual(triggers.allowance(self.p, "discovered"), 60, "the user's budget raises the allowance")
        self.crowd("discovered", 41)  # past forty: only the raised budget lets the next one file
        self.fm("capture", "FIX: one more finding about the lock leak", "--source", "discovered")
        self.assertEqual(triggers.held(self.p), [], "under the raised budget nothing waits")

    def test_a_budget_the_user_sets_below_the_record_caps_the_allowance(self):  # T-5722
        self.fm("triggers", "set", "discovered", "--budget", "5")
        self.assertEqual(triggers.allowance(self.p, "discovered"), 5, "the user's budget caps it")
        self.crowd("discovered", 5)
        self.fm("capture", "FIX: one finding past the cap", "--source", "discovered")
        self.assertEqual(len(triggers.held(self.p)), 1, "the sixth waits behind the cap")

    def test_a_budget_the_user_sets_wins_over_a_learned_allowance_both_ways(self):  # T-5722
        self.history("discovered", done=2, dropped=18)  # precision 0.1: learned down to 4 of 40
        self.assertEqual(triggers.allowance(self.p, "discovered"), 4)
        self.fm("triggers", "set", "discovered", "--budget", "60")
        self.assertEqual(triggers.allowance(self.p, "discovered"), 60, "the budget raises the learned allowance too")
        self.fm("triggers", "set", "discovered", "--budget", "3")
        self.assertEqual(triggers.allowance(self.p, "discovered"), 3, "and caps it below the learned one")

    def test_the_replay_allows_the_budget_the_user_sets_both_ways(self):  # T-5722: the replay's own allowance line
        self.fm("triggers", "set", "discovered", "--budget", "60")
        self.crowd("discovered", 41)
        self.assertEqual(triggers.replay(self.p, 7)["held"], 0, "41 filed today under a budget of 60 hold nothing")
        self.fm("triggers", "set", "discovered", "--budget", "5")
        self.assertGreater(triggers.replay(self.p, 7)["held"], 0, "a budget of 5 holds what is over it")

    # ---- criterion 3: a followup goes through the same fold, append and allowance
    def test_a_followup_item_is_appended_folded_and_held_like_a_discovered_one(self):
        tid = self.new("Rework the retry loop", scope="plugin/net/retry.py")
        out = self.fm("capture", "FEATURE: retry.py should log each attempt in plugin/net/retry.py",
                      "--source", "followup").stdout
        self.assertIn(f"appended to {tid}", out)
        other = self.new("Document the export format in the guide", type_="FEATURE")
        out = self.fm("capture", "FEATURE: Document the export format in the guide, with an example",
                      "--source", "followup").stdout
        self.assertIn(f"folded into {other}", out)
        self.history("followup", done=1, dropped=19)
        n = len(self.tasks())
        out = self.fm("capture", "FEATURE: a follow-up nobody asked for", "--source", "followup").stdout
        self.assertIn("held", out)
        self.assertEqual(len(self.tasks()), n)
        self.assertEqual([x["source"] for x in triggers.held(self.p)], ["followup"])

    def test_an_item_folds_onto_a_near_task_of_another_type_but_prefers_one_of_its_own(self):  # T-1726, test_roles.Noticed
        fix = self.new("Lease take prints every lease in the project", type_="FIX")
        kind, tid = triggers.fold(self.p, "lease take prints every lease in the project, not just its own", "FEATURE",
                                  "discovered")
        self.assertEqual((kind, tid), ("folded", fix))
        same = self.new("Lease take prints every lease in the project and its own", type_="FEATURE")
        kind, tid = triggers.fold(self.p, "lease take prints every lease in the project, not just its own, again",
                                  "FEATURE", "discovered")
        self.assertEqual((kind, tid), ("folded", same))

    # ---- the edges: what names a file, a second run, the bounded list, the once-a-day drain
    def test_named_paths_judges_by_shape_only(self):
        self.assertEqual(triggers.named_paths("use and/or in v1.2 at 3.5"), [])
        self.assertEqual(triggers.named_paths("see `plugin/lib/x.py:12` and retry.py, then ./docs/a.md."),
                         ["plugin/lib/x.py", "retry.py", "docs/a.md"])
        self.assertEqual(triggers.named_paths(""), [])

    def test_a_second_run_of_the_same_report_adds_nothing(self):
        tid = self.new("Rework the retry loop", scope="plugin/net/retry.py")
        line = "plugin/net/retry.py never grows its backoff"
        self.noticed([line])
        n = len(self.tasks())
        self.noticed([line])
        self.assertEqual(len(self.tasks()), n)
        self.assertEqual(c.find_brief(self.p, tid).section("Follow-ups captured").count("never grows"), 1)

    # ---- T-4743 review: nothing a trigger found is lost
    def ago(self, days):
        return (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def hold(self, text, days=0, type_="FIX", tier="S", **more):
        return triggers._hold(self.p, dict({"text": text, "type": type_, "source": "discovered", "tier": tier,
                                            "held_at": self.ago(days)}, **more))

    def test_the_201st_held_item_is_still_readable_and_files_later(self):
        for i in range(200):
            self.hold(f"finding {i} about the cache layer of module m{i}", days=2)
        self.hold("the oldest finding of all, about the lock leak", days=30)  # the 201st
        self.assertFalse(hasattr(triggers, "HELD_MAX"), "no cap, no eviction")
        items = triggers.held(self.p)
        self.assertEqual(len(items), 201)
        self.assertEqual(items[-1]["text"], "the oldest finding of all, about the lock leak")
        self.assertEqual(triggers.fold_counts(self.p).get("evicted"), None)
        old = triggers.DRAIN_LOOKS
        triggers.DRAIN_LOOKS = 3
        try:
            filed = triggers.drain(self.p)
        finally:
            triggers.DRAIN_LOOKS = old
        self.assertEqual(c.find_brief(self.p, filed[0]).title.lower(), "the oldest finding of all, about the lock leak")
        self.assertEqual(len(triggers.held(self.p)), 201 - len(filed))

    def test_a_held_item_files_within_a_week_the_oldest_first_whatever_its_tier_or_the_days_room(self):
        self.history("discovered", done=2, dropped=18)  # room today: none
        self.hold("a redesign of the whole banner system", days=12, type_="FEATURE", tier="L")
        self.hold("the digest crashes on an empty ledger", days=9)
        self.hold("the lock leaks a descriptor on timeout", days=10)
        self.hold("the parser drops a BOM line", days=1)
        filed = triggers.drain(self.p)
        self.assertEqual([c.find_brief(self.p, t).title.lower() for t in filed],
                         ["a redesign of the whole banner system", "the lock leaks a descriptor on timeout",
                          "the digest crashes on an empty ledger"], "overdue: oldest first, past the allowance")
        self.assertEqual([x["text"] for x in triggers.held(self.p)], ["the parser drops a BOM line"])
        self.assertEqual(triggers.MAX_WAIT_DAYS, 7)

    def test_a_security_item_an_urgent_one_and_the_urgent_triggers_are_never_folded_or_held(self):
        self.history("discovered", done=0, dropped=20)
        host = self.new("the export endpoint skips the auth check", type_="SECURITY")
        n = len(self.tasks())
        self.assertEqual(triggers.fold(self.p, "SECURITY: the export endpoint skips the auth check, again", "SECURITY",
                                       "discovered"), (None, None))
        self.assertEqual(triggers.fold(self.p, "the export endpoint skips the auth check, again", "SECURITY", "followup"),
                         (None, None))
        self.assertEqual(triggers.fold(self.p, "a plain item with nothing to cover it zz", "FIX", "discovered",
                                       priority="urgent"), (None, None))
        self.assertTrue(all(triggers.exempt("FIX", t) for t in triggers.URGENT))
        self.assertEqual(triggers.held(self.p), [])
        self.assertEqual(c.find_brief(self.p, host).section("Log").count("folded"), 0)
        out = self.fm("capture", "SECURITY: the token check in plugin/a/auth.py is skippable", "--source", "discovered").stdout
        self.assertIn("Captured as", out)
        self.assertEqual(len(self.tasks()), n + 1)

    def test_a_report_item_tagged_security_or_urgent_files_at_once_past_the_allowance(self):
        self.history("discovered", done=0, dropped=20)
        out = self.noticed(["SECURITY: the token check in plugin/a/auth.py is skippable", "FIX!: the build breaks on a clean checkout",
                            "FEATURE: a prettier banner for the pane"])
        self.assertEqual(len(triggers.held(self.p)), 1, out)
        self.assertIn("banner", triggers.held(self.p)[0]["text"])
        found = {b.type: b for b in self.tasks() if b.meta.get("source") == "discovered" and "finding" not in b.title}
        self.assertIn("SECURITY", found)
        self.assertIn("token check", found["SECURITY"].title)
        self.assertEqual(next(b for b in self.tasks() if "breaks on a clean" in b.title).meta.get("priority"), "urgent")

    def test_the_whole_finding_is_kept_on_the_host_however_long_and_two_long_ones_with_one_start_both_stay(self):
        tid = self.new("Rework the retry loop", scope="plugin/net/retry.py")
        head = "FIX: the backoff in plugin/net/retry.py never grows because " + "the multiplier is reset on every call, " * 8
        one, two = head + "\nrepro: call it twice, step ONE", head + "\nrepro: call it thrice, step TWO"
        self.assertGreater(len(head), 240)
        self.fm("capture", one, "--source", "discovered")
        self.fm("capture", two, "--source", "discovered")
        self.fm("capture", one, "--source", "discovered")  # a second run of the same
        sec = c.find_brief(self.p, tid).section("Follow-ups captured")
        self.assertEqual(sec.count("- [ ] "), 2, sec)
        texts = []
        for line in sec.splitlines():
            rel = line.split("whole text: ")[1].strip()
            with open(os.path.join(self.p.dir, rel), encoding="utf-8") as f:
                texts.append(f.read())
        self.assertTrue(any("step ONE" in t for t in texts) and any("step TWO" in t for t in texts), texts)
        self.assertTrue(all(len(t) > 300 for t in texts))

    def test_held_text_and_raw_are_kept_uncut(self):
        self.history("discovered", done=0, dropped=20)
        text, raw = "alpha beta " * 140, "gamma delta " * 800
        self.assertEqual(triggers.fold(self.p, text, "FIX", "discovered", raw=raw)[0], "held")
        (e,) = triggers.held(self.p)
        self.assertEqual(e["text"], c.redact(text.strip()))
        self.assertEqual(e["raw"], c.redact(raw))
        (tid,) = triggers.drain(self.p, self.rollover())
        self.assertEqual(len(c.find_brief(self.p, tid).section("Raw request")) > 8000, True)

    def test_a_hosts_follow_ups_must_each_be_settled_before_it_closes(self):
        tid = self.new("Rework the retry loop", scope="plugin/net/retry.py")
        self.fm("capture", "FIX: the backoff in plugin/net/retry.py never grows", "--source", "discovered")
        self.fm("capture", "FIX: plugin/net/retry.py swallows the timeout error", "--source", "followup")
        b = c.find_brief(self.p, tid)
        self.assertEqual(len(triggers.follow_gaps(self.p, b)), 1)
        done = self.fm("task", "done", tid, check=False)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("follow-up", done.stdout + done.stderr)
        self.assertIn("sampo task follow", done.stdout + done.stderr)
        listed = self.fm("task", "follow", tid).stdout
        self.assertIn("never grows", listed)
        keys = re.findall(r"f-([0-9a-f]{8})", listed)
        self.assertEqual(len(keys), 2)
        self.assertNotEqual(self.fm("task", "follow", tid, keys[0], "--drop", check=False).returncode, 0, "a drop says why")
        self.fm("task", "follow", tid, keys[0], "--drop", "--why", "the timeout is by design")
        self.fm("task", "follow", tid, keys[1], "--capture")
        sec = c.find_brief(self.p, tid).section("Follow-ups captured")
        self.assertIn("[dropped: the timeout is by design]", sec)
        self.assertRegex(sec, r"\[captured T-\d+\]")
        new = [b for b in self.tasks() if b.id != tid]
        self.assertTrue(any("never grows" in b.title or "swallows" in b.title for b in new))
        self.assertEqual(triggers.follow_gaps(self.p, c.find_brief(self.p, tid)), [])
        self.fm("capture", "FIX: plugin/net/retry.py never closes its socket", "--source", "discovered")
        keys = re.findall(r"\[ \].*\(f-([0-9a-f]{8})", self.fm("task", "follow", tid).stdout)
        r = self.fm("task", "follow", tid, "--done", check=False)  # never every open one at once
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(len(triggers.follow_gaps(self.p, c.find_brief(self.p, tid))), 1)
        self.fm("task", "follow", tid, keys[0], "--done")
        self.assertEqual(triggers.follow_gaps(self.p, c.find_brief(self.p, tid)), [])
        self.assertIn("- [x] ", c.find_brief(self.p, tid).section("Follow-ups captured"))
        self.assertEqual(len(keys), 1)

    def test_a_dropped_or_deferred_host_files_its_appended_items_as_captures(self):
        for verb, extra in (("drop", []), ("defer", [])):
            tid = self.new(f"Rework the {verb} loop", scope=f"plugin/{verb}/retry.py")
            self.fm("capture", f"FIX: plugin/{verb}/retry.py loses the {verb} request", "--source", "discovered")
            self.assertEqual(len(triggers.follow_gaps(self.p, c.find_brief(self.p, tid))), 1)
            n = len(self.tasks())
            self.fm("task", verb, tid, "not needed", *extra)
            self.assertEqual(len(self.tasks()), n + 1, verb)
            new = max(self.tasks(), key=lambda b: c.id_num(b.id))
            self.assertIn(f"loses the {verb} request", new.title)
            self.assertEqual(new.status, "captured")
            self.assertIn(f"[captured {new.id}]", c.find_brief(self.p, tid).section("Follow-ups captured"))
        self.assertEqual(triggers.held(self.p), [])

    def host_with_item(self, name):
        tid = self.new(f"Rework the {name} loop", scope=f"plugin/{name}/retry.py")
        self.fm("capture", f"FIX: plugin/{name}/retry.py loses the {name} request", "--source", "discovered")
        self.assertEqual(len(triggers.follow_gaps(self.p, c.find_brief(self.p, tid))), 1)
        return tid

    def assert_refiled(self, tid, name, n):
        self.assertEqual(len(self.tasks()), n + 1, name)
        new = max(self.tasks(), key=lambda b: c.id_num(b.id))
        self.assertIn(f"loses the {name} request", new.title)
        self.assertEqual(new.status, "captured")
        self.assertIn(f"[captured {new.id}]", c.find_brief(self.p, tid).section("Follow-ups captured"))

    def test_every_way_a_host_is_closed_unread_files_its_items_as_captures(self):
        from sampo import cli, eco
        member = self.host_with_item("batched")  # a batch member closed done in its host
        host = self.new("The batch host")
        b, h = c.find_brief(self.p, member), c.find_brief(self.p, host)
        b.meta["batched_in"], h.meta["batch"] = host, [member]
        c.save_brief(self.p, b, touch=False)
        c.save_brief(self.p, h, touch=False)
        n = len(self.tasks())
        cli._settle_batch(self.p, c.find_brief(self.p, host), done=True)
        self.assertEqual(c.find_brief(self.p, member).status, "done")
        self.assert_refiled(member, "batched", n)
        tid = self.host_with_item("settable")  # task set status=deferred
        n = len(self.tasks())
        self.fm("task", "set", tid, "status=deferred")
        self.assert_refiled(tid, "settable", n)
        tid = self.host_with_item("swept")  # deferred before this release, swept to a drop
        b = c.find_brief(self.p, tid)
        b.meta["status"] = "deferred"
        c.save_brief(self.p, b, touch=False)
        n = len(self.tasks())
        eco.swipe_apply(self.p, f"{tid}:drop")
        self.assert_refiled(tid, "swept", n)
        again = len(self.tasks())
        self.fm("task", "set", tid, "status=planned")
        self.fm("task", "drop", tid, "no")
        self.assertEqual(len(self.tasks()), again, "a second drop files nothing twice")

    def test_a_close_refused_for_open_follow_ups_names_each_and_the_verb_that_settles_it(self):
        tid = self.host_with_item("closing")
        b = c.find_brief(self.p, tid)
        msgs = triggers.follow_gaps(self.p, b)
        self.assertEqual(len(msgs), 1)
        self.assertIn("loses the closing request", msgs[0], "the item itself is named")
        self.assertRegex(msgs[0], rf"sampo task follow {tid}")

    def test_an_anyway_capture_skipped_the_fold_and_does_not_count_against_the_allowance(self):
        before = triggers.filed_today(self.p, "discovered")
        self.fm("capture", "FIX: a finding about the lock leak", "--source", "discovered", "--anyway")
        self.assertEqual(triggers.filed_today(self.p, "discovered"), before)
        self.fm("capture", "FIX: another finding about the digest", "--source", "discovered")
        self.assertEqual(triggers.filed_today(self.p, "discovered"), before + 1)

    def test_a_fold_goes_only_onto_an_open_awake_host(self):
        text = "fm lease take prints every lease in the project, not just its own"
        host = self.new("fm lease take prints every lease in the project")
        for status in ("deferred", "blocked"):
            b = c.find_brief(self.p, host)
            b.meta["status"] = status
            c.save_brief(self.p, b, touch=False)
            self.assertEqual(triggers.fold(self.p, text, "FIX", "discovered"), (None, None), status)
        b = c.find_brief(self.p, host)
        b.meta["status"] = "planned"
        c.save_brief(self.p, b, touch=False)
        # T-1726: an agent's line is typed by a guess at its words, so another type's open task still hosts it
        self.assertEqual(triggers.fold(self.p, text, "FEATURE", "discovered"), ("folded", host))
        self.assertEqual(triggers.fold(self.p, text, "FIX", "discovered"), ("folded", host))

    def test_an_append_goes_to_the_narrowest_scope_then_the_active_task_never_a_catch_all(self):
        wide = self.new("Sweep the plugin", scope="plugin/**")
        directory = self.new("Rework the net package", scope="plugin/net/*")
        a = self.new("Rework the retry loop", scope="plugin/net/retry.py")
        b = self.new("Rework the retry timeouts", scope="plugin/net/retry.py")
        self.assertEqual(triggers.fold(self.p, "plugin/net/retry.py never grows its backoff", "FIX", "discovered"),
                         ("appended", a), f"the exact file over {wide} and {directory}; the oldest of equals")
        brief = c.find_brief(self.p, b)
        brief.meta["status"] = "active"
        c.save_brief(self.p, brief, touch=False)
        self.assertEqual(triggers.fold(self.p, "plugin/net/retry.py loses the first request", "FIX", "discovered"),
                         ("appended", b), "of equally narrow ones the active task")
        self.assertEqual(triggers.fold(self.p, "plugin/net/other.py loses the first request", "FIX", "discovered"),
                         ("appended", directory), "the directory over the catch-all")

    def test_the_state_line_and_triggers_show_everything_held_not_just_today(self):
        self.hold("the lock leaks a descriptor on timeout", days=3)
        self.hold("the digest crashes on an empty ledger", days=9)
        line = triggers.self_running(self.p)["line"]
        self.assertIn("2 held in all", line)
        self.assertIn("1 overdue", line)
        self.assertIn("2 held in all", self.fm("state").stdout)
        shown = self.fm("triggers", "--held").stdout
        self.assertIn("overdue", shown)
        self.assertIn("2 held", shown)

    def test_a_direct_creators_filings_neither_eat_the_allowance_nor_count_in_the_replay(self):
        from sampo import cli
        self.history("discovered", done=0, dropped=0)  # none; the fold's own filings are marked
        self.crowd("discovered", 1)
        for i in range(30):  # train, doctor, rune, find, outcomes…: a brief of their own, an event that says where from
            b = cli._create(self.p, f"direct finding {i} of a check", "FIX", "S", "captured", source="discovered", net=False)
            c.log_event(self.p, "capture", task=b.id, data={"source": "discovered", "type": "FIX", "from": "train_base_red"})
        self.assertEqual(triggers.filed_today(self.p, "discovered"), 1)
        self.assertEqual(triggers.fold(self.p, "a fresh finding about the digest crash zz", "FIX", "discovered"), (None, None))
        r = triggers.replay(self.p, 7)
        self.assertEqual((r["before"], r["direct"]), (1, 30))

    def test_the_replay_reads_events_from_before_the_mark_by_their_provenance(self):
        from sampo import cli
        for i, more in enumerate([{}, {}, {}, {"finder": "census"}, {"from": "outcomes --census"}]):
            b = cli._create(self.p, f"legacy finding {i} of its day", "FIX", "S", "captured", source="discovered", net=False)
            c.log_event(self.p, "capture", task=b.id, data=dict({"source": "discovered", "type": "FIX"}, **more))
        r = triggers.replay(self.p, 7)
        self.assertEqual((r["before"], r["direct"]), (3, 2))

    def test_a_held_duplicate_adds_no_event_and_a_rerun_report_keeps_its_new_steps(self):
        self.assertEqual(self.hold("plugin/a/one.py: 2 findings noticed in r", steps=["drops a BOM line"]), "added")
        self.assertEqual(self.hold("plugin/a/one.py: 2 findings noticed in r", steps=["drops a BOM line"]), "same")
        self.assertEqual(self.hold("plugin/a/one.py: 2 findings noticed in r", steps=["drops a BOM line", "leaks a handle"]),
                         "merged")
        (e,) = triggers.held(self.p)
        self.assertEqual(e["steps"], ["drops a BOM line", "leaks a handle"])
        self.history("discovered", done=0, dropped=20)
        ent = {"text": "plugin/b/two.py: 2 findings noticed in r", "source": "discovered", "type": "FIX", "tier": "S"}
        for _ in range(2):
            self.assertIsNone(triggers.file_or_hold(self.p, dict(ent)))
        self.assertEqual(triggers.fold_counts(self.p)["held"], 1)

    def break_held(self):
        path = os.path.join(self.p.dir, triggers.HELD_FILE)
        with open(path, "w", encoding="utf-8") as f:
            f.write('[{"text": "a half-writ')
        return path, open(path, "rb").read()

    def test_a_held_file_that_cannot_be_read_never_costs_a_finding_and_is_never_overwritten(self):
        path, before = self.break_held()
        with self.assertRaises(c.CorruptState):
            triggers.held(self.p)
        with self.assertRaises(c.CorruptState):
            self.hold("one more finding about the lock")
        n = len(self.tasks())
        r = self.fm("capture", "FIX: another finding about the digest", "--source", "discovered")
        self.assertIn(triggers.HELD_FILE, r.stderr, "it says why the fold was skipped")
        self.assertIn("Captured as", r.stdout)
        self.assertEqual(len(self.tasks()), n + 1, "the finding is an ordinary captured task")
        new = max(self.tasks(), key=lambda b: c.id_num(b.id))
        self.assertEqual(new.status, "captured")
        self.assertEqual(open(path, "rb").read(), before)
        # a research report: its noticed lines and the report's own log and views still happen
        r = self.noticed(["the lock leaks a descriptor on timeout", "plugin/net/retry.py never backs off",
                          "plugin/net/retry.py drops the timeout"])
        self.assertIn(triggers.HELD_FILE, r)
        self.assertEqual(len(self.tasks()), n + 1 + 3, "one task per line: without the held list there is no grouping")
        self.assertTrue(c.ledger_events(self.p, kinds={"research"}), "log_event still ran")
        self.assertEqual(open(path, "rb").read(), before)
        self.assertIn(triggers.HELD_FILE, triggers.self_running(self.p)["line"], "the state line says so, never crashes")

    def test_the_drain_runs_for_items_that_waited_past_midnight_only(self):
        self.history("discovered", done=2, dropped=18)
        self.fm("capture", "FEATURE: a prettier banner for the pane", "--source", "discovered")
        self.assertEqual(triggers.drain_due(self.p), [], "held today: a new day makes room, nothing else")
        items = triggers.held(self.p)
        items[0]["held_at"] = self.ago(1)  # before today's midnight, well inside its week
        triggers._write_held(self.p, items)
        n = len(self.tasks())
        self.assertEqual(triggers.drain_due(self.p), [], "today's allowance is already used up")
        self.assertEqual(len(self.tasks()), n)
        tomorrow = datetime.datetime.now().astimezone() + datetime.timedelta(days=1)
        self.assertEqual(len(triggers.drain_due(self.p, tomorrow)), 1)
        self.assertEqual(triggers.held(self.p), [])

    def test_a_group_over_the_allowance_is_held_whole_and_files_with_its_steps(self):
        self.history("discovered", done=2, dropped=18)
        out = self.noticed(["plugin/a/one.py drops a BOM line", "plugin/a/one.py never closes its file"])
        self.assertIn("held", out)
        (entry,) = triggers.held(self.p)
        self.assertEqual(len(entry["steps"]), 2)
        tomorrow = datetime.datetime.now().astimezone() + datetime.timedelta(days=1)
        (tid,) = triggers.drain(self.p, tomorrow)
        self.assertEqual(len(c.find_brief(self.p, tid).steps()), 2)

    # ---- the state line, and the replay that answers "why so many tasks"
    def test_the_state_line_counts_what_was_folded_appended_and_held_today(self):
        self.new("Rework the retry loop", scope="plugin/net/retry.py")
        self.fm("capture", "FIX: the backoff in plugin/net/retry.py never grows", "--source", "discovered")
        self.history("followup", done=1, dropped=19)
        self.fm("capture", "FEATURE: a follow-up nobody asked for", "--source", "followup")
        line = triggers.self_running(self.p)["line"]
        self.assertRegex(line, r"1 appended, 1 held today")
        self.assertIn("1 appended", self.fm("state").stdout)

    def test_the_replay_prints_tasks_filed_before_against_after(self):
        self.new("Rework the retry loop", scope="plugin/net/retry.py")
        self.history("discovered", done=2, dropped=18)
        for i in range(5):  # five items in the same file, one open task owning it: the replay appends them
            triggers.file_item(self.p, {"text": f"FIX: plugin/net/retry.py problem number {i} in its own words",
                                        "type": "FIX", "source": "discovered", "tier": "S"})
        r = self.fm_json("triggers", "--replay", "--days", "7")["replay"]
        self.assertEqual(r["before"], 25)
        self.assertLess(r["after"], r["before"])
        self.assertEqual(r["before"], r["after"] + r["folded"] + r["appended"] + r["held"])
        self.assertNotIn("evicted", r)
        self.assertIn("filed 25 before, ", self.fm("triggers", "--replay", "--days", "7").stdout)
