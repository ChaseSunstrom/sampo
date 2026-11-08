"""T-3934: a FEATURE close names its outcome signal; sampo outcomes --silent lists merged features whose signal never fired
1, 7 or 30 days on, sampo next surfaces the oldest, and --census gives every verb, skill and night job three levels."""
import json
import os

from test_outcomes import Base  # first: it puts plugin/lib on the path

from sampo import core as c
from sampo import outcomes


class Probes(Base):
    def feature(self, signal, days=2):
        tid = self.done("Add the widget", "FEATURE", "true", "--outcome", signal)
        self.age(tid, days)
        return tid

    def silent(self):
        return json.loads(self.fm("outcomes", "--silent", "--json").stdout)

    def test_the_close_records_the_signal_and_a_young_feature_is_not_yet_due(self):
        tid = self.feature("file:widget.out", days=0)
        self.assertIn("file:widget.out", c.find_brief(self.p, tid).section("Outcome"))
        self.assertEqual([r["task"] for r in self.silent()["silent"]], [])

    def test_silent_after_a_day_and_a_firing_clears_it(self):
        tid = self.feature("file:widget.out")
        row = self.silent()["silent"][0]
        self.assertEqual((row["task"], row["status"], row["stage"]), (tid, "silent", 1))
        self.assertTrue(row["checked"])  # every result is stamped with its check time
        self.assertIn(tid, self.fm("next").stdout)  # the oldest is named in sampo next
        with open(os.path.join(self.repo, "widget.out"), "w") as f:
            f.write("x\n")
        self.assertEqual(self.silent()["silent"], [])

    def test_a_signal_that_cannot_be_read_is_manual_never_passing(self):
        self.feature("metric:nothing-registered")
        self.feature("gibberish")
        self.assertEqual({r["status"] for r in self.silent()["silent"]}, {"manual"})

    def test_a_row_signal_fires_on_a_new_line_in_a_file(self):
        self.feature("row:rows.jsonl")
        with open(os.path.join(self.repo, "rows.jsonl"), "w") as f:
            f.write(json.dumps({"at": c.now()}) + "\n")
        self.assertEqual(self.silent()["silent"], [])

    def test_stages_are_1_7_and_30_days(self):
        self.assertEqual([outcomes.stage(d) for d in (0.5, 1, 8, 31)], [0, 1, 7, 30])

    def test_the_census_does_not_count_a_builders_run_as_use(self):
        ev = [{"ts": c.now(), "kind": "tool", "tool": "Bash", "target": "sampo scout --slow", "project": self.p.slug,
               "session_id": "s", **({"agent_type": "sampo:fm-builder"} if i else {})} for i in range(2)]
        used = outcomes.used_counts(ev, self.p.slug)
        self.assertEqual(used["verb:scout"], [("s", ev[0]["ts"])])

    def test_census_capture_files_at_most_ten_once_each(self):
        first = json.loads(self.fm("outcomes", "--census", "--capture", "--json").stdout)["captured"]
        again = json.loads(self.fm("outcomes", "--census", "--capture", "--json").stdout)["captured"]
        self.assertEqual(len(first), 10)
        self.assertFalse(set(first) & set(again))

    def test_doctor_names_a_silent_feature(self):
        from sampo import doctor
        self.assertEqual(doctor.check_outcomes([self.p]).status, "PASS")
        self.feature("file:never.out")
        outcomes.probes(self.p, fresh=True)
        self.assertEqual(doctor.check_outcomes([self.p]).status, "WARN")

    def test_a_stale_cache_returns_at_once_and_spawns_one_detached_refresh(self):
        from unittest import mock
        self.feature("file:never.out")
        with mock.patch.dict(os.environ), mock.patch.object(outcomes.subprocess, "Popen") as popen, \
                mock.patch.object(outcomes, "_evaluate", side_effect=AssertionError("probed inline")):
            os.environ.pop("FOREMAN_NO_BACKGROUND", None)
            self.assertEqual(outcomes.probes(self.p, cached=True), [])  # no cache yet: nothing shown, no inline probe
            self.assertEqual(outcomes.line(self.p), "")  # a second hook turn: still one refresh (10 minute mark)
            self.assertEqual(popen.call_count, 1)
            self.assertTrue(popen.call_args.kwargs["start_new_session"])
            os.environ["FOREMAN_NO_BACKGROUND"] = "1"
            os.remove(os.path.join(self.p.dir, "outcome-probes.refresh"))
            outcomes.probes(self.p, cached=True)
            self.assertEqual(popen.call_count, 1)

    def test_backfill_proposes_from_the_changelog_and_apply_writes_through_the_brief(self):
        tid = self.done("Add the widget", "FEATURE", "true")
        with open(os.path.join(self.repo, "CHANGELOG.md"), "w") as f:
            f.write(f"- `sampo scout` now lists parts ({tid})\n")
        none = self.done("Add the gadget", "FEATURE", "true")
        data = json.loads(self.fm("outcomes", "--backfill", "--json").stdout)
        self.assertEqual({r["task"]: r["signal"] for r in data["proposals"]}, {tid: "verb:scout", none: None})
        self.assertEqual(c.find_brief(self.p, tid).section("Outcome"), "")
        self.fm("outcomes", "--backfill", "--apply")
        self.assertTrue(c.find_brief(self.p, tid).section("Outcome").startswith("verb:scout"))
        self.assertEqual(json.loads(self.fm("outcomes", "--backfill", "--json").stdout)["proposals"][0]["task"], none)

    def test_the_census_lists_never_used_and_ineffective_items(self):
        data = json.loads(self.fm("outcomes", "--census", "--json").stdout)
        self.assertIn("verb:scout", {f"{i['kind']}:{i['name']}" for i in data["items"]})
        self.assertTrue(all({"exists", "used", "effective"} <= set(i) for i in data["items"]))
