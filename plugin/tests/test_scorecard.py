"""T-3845: sampo scorecard: the direction's measures (research/direction.md section 8), each a number with a bar, read
model-free from what Sampo records; the hourly tick (night.tick) turns a measure past its bar on two ticks in a row into
one open task, nothing once it recovers, nothing more while that task is open."""
import datetime
import json
import os
import subprocess
import time
import unittest.mock

from helpers import ForemanTestCase

from sampo import core as c
from sampo import scorecard

first = lambda text: json.JSONDecoder().raw_decode(text)[0]  # noqa: E731


def ago(hours):
    t = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


NAMES = ["asks", "shipped", "lanes", "cpu", "tokens", "steers", "towers"]


class Scorecard(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)

    def git(self, *a, env=None):
        return subprocess.run(["git", "-C", self.repo, *a], check=True, capture_output=True, text=True,
                              env=env).stdout.strip()

    def land(self, name, hours):
        with open(os.path.join(self.repo, name), "w") as f:
            f.write(name)
        env = {**os.environ, "GIT_AUTHOR_DATE": ago(hours), "GIT_COMMITTER_DATE": ago(hours)}
        self.git("add", "-A")
        self.git("commit", "-qm", name, env=env)

    def ask(self, words, hours):
        tid = first(self.fm("capture", words, "--json").stdout)["id"]
        b = c.find_brief(self.p, tid)
        b.meta["created"] = ago(hours)
        c.save_brief(self.p, b)
        return tid

    def rows(self):
        return {r["name"]: r for r in scorecard.compute(self.p, time.time())}

    # ---- the verb: each measure, its bar and a mark

    def test_every_measure_names_its_value_bar_and_mark(self):
        rows = scorecard.compute(self.p, time.time())
        self.assertEqual([r["name"] for r in rows], NAMES)
        for r in rows:
            self.assertEqual(set(r), {"name", "label", "value", "bar", "op", "unit", "ok", "detail"} | ({"count"} if r["name"] == "asks" else set()), r)
            self.assertIn(r["ok"], (True, False, None))
            self.assertIn(r["op"], ("<=", ">="))
            self.assertIsInstance(r["bar"], (int, float))

    def test_the_verb_prints_each_measure_with_a_bar_and_a_mark_and_json_too(self):
        self.ask("remake the rune page as kalevala", 30)
        self.land("a.txt", 30)
        self.git("update-ref", "refs/foreman/shipped", "HEAD")
        self.land("b.txt", 5)
        out = self.fm("scorecard").stdout
        for label in ("user asks not landed", "landed not shipped", "lanes waiting", "agent CPU share",
                      "tokens per closed task", "steers per day", "Kalevala towers climbing"):
            self.assertIn(label, out)
        self.assertIn("bar", out)
        self.assertRegex(out, r"✗ user asks not landed: 1 ask\(s\), the oldest \d+ h")  # 30 h, over the 24 h bar
        self.assertRegex(out, r"✗ landed not shipped: 1 commit\(s\), the oldest 5 h")
        data = first(self.fm("scorecard", "--json").stdout)
        self.assertEqual([r["name"] for r in data["measures"]], NAMES)
        by = {r["name"]: r for r in data["measures"]}
        self.assertFalse(by["asks"]["ok"])
        self.assertEqual(by["asks"]["count"], 1)
        self.assertGreaterEqual(by["asks"]["value"], 29)
        self.assertEqual(by["shipped"]["bar"], scorecard.SHIP_OLDEST_H)
        self.assertEqual(data["failing"], [n for n in NAMES if by[n]["ok"] is False])

    def test_asks_pass_when_none_is_old_and_count_only_the_users_open_ones(self):
        self.assertTrue(self.rows()["asks"]["ok"])
        self.assertEqual(self.rows()["asks"]["count"], 0)
        self.ask("an ask from five hours ago", 5)
        r = self.rows()["asks"]
        self.assertEqual((r["count"], r["ok"]), (1, True), "open, but within the bar")
        self.assertIn("1 ask(s)", r["detail"])

    def test_shipped_says_n_a_for_a_project_that_never_shipped_and_passes_when_shipped(self):
        self.land("a.txt", 30)
        self.assertIsNone(self.rows()["shipped"]["ok"], "no ship ref: nothing to be behind")
        self.git("update-ref", "refs/foreman/shipped", "HEAD")
        r = self.rows()["shipped"]
        self.assertEqual((r["value"], r["ok"]), (0, True))
        self.land("b.txt", 1)
        r = self.rows()["shipped"]
        self.assertTrue(r["ok"], "an hour old is inside the bar")
        self.assertEqual(r["count"] if "count" in r else 1, 1)

    def test_lanes_waiting_count_the_lanes_past_the_bar(self):
        with unittest.mock.patch("sampo.lanes.awaiting_merge", return_value=["T-0002", "T-0003"]):
            r = self.rows()["lanes"]
        self.assertEqual((r["value"], r["ok"]), (2, True))
        many = [f"T-00{n:02d}" for n in range(10, 10 + scorecard.LANES_WAITING_MAX + 1)]
        with unittest.mock.patch("sampo.lanes.awaiting_merge", return_value=many):
            r = self.rows()["lanes"]
        self.assertEqual((r["value"], r["ok"]), (len(many), False))

    def test_cpu_share_is_read_from_the_samples_against_the_projects_goal(self):
        with unittest.mock.patch("sampo.cpu.series", return_value=None):
            self.assertIsNone(self.rows()["cpu"]["ok"], "no samples: not measured")
        with unittest.mock.patch("sampo.cpu.series", return_value={"now": 4.0, "goal": 10.0}):
            r = self.rows()["cpu"]
        self.assertEqual((r["value"], r["ok"], r["bar"]), (4.0, True, 10.0))
        with unittest.mock.patch("sampo.cpu.series", return_value={"now": 31.5, "goal": 10.0}):
            self.assertFalse(self.rows()["cpu"]["ok"])

    def test_tokens_per_closed_task_is_the_windows_spend_over_the_tasks_closed(self):
        t = self.ask("a task that closes", 3)
        b = c.find_brief(self.p, t)
        b.meta["status"], b.meta["updated"] = "done", ago(1)
        c.save_brief(self.p, b)
        costs = {t: 2_000_000, "(no task)": 1_000_000}
        with unittest.mock.patch("sampo.cost.task_costs", return_value=costs):
            r = self.rows()["tokens"]
        self.assertEqual(r["value"], 3_000_000)
        self.assertEqual(r["ok"], 3_000_000 <= scorecard.TOKENS_PER_TASK_MAX)

    def test_asks_older_than_the_selfcheck_cap_still_count_and_fail_the_bar(self):
        self.ask("an ask left for five days", 120)
        r = self.rows()["asks"]
        self.assertEqual((r["count"], r["ok"]), (1, False), "past ASK_DAYS is neglect, not recovery")
        self.assertGreaterEqual(r["value"], 119)

    def test_tokens_spend_is_cached_for_a_day_so_the_tick_does_not_rescan(self):
        t = self.ask("a task that closes", 3)
        b = c.find_brief(self.p, t)
        b.meta["status"], b.meta["updated"] = "done", ago(1)
        c.save_brief(self.p, b)
        with unittest.mock.patch("sampo.cost.task_costs", return_value={t: 2_000_000}) as m:
            self.assertEqual(self.rows()["tokens"]["value"], 2_000_000)
            self.assertEqual(self.rows()["tokens"]["value"], 2_000_000)
        self.assertEqual(m.call_count, 1, "the second read came from the cache")
        with unittest.mock.patch("sampo.cost.task_costs", return_value={t: 4_000_000}) as m:
            self.assertEqual(self.rows()["tokens"]["value"], 2_000_000)
            self.assertEqual(m.call_count, 0)
            fresh = {r["name"]: r for r in scorecard.compute(self.p, time.time(), fresh=True)}
            self.assertEqual(fresh["tokens"]["value"], 4_000_000, "the verb reads fresh")
        with unittest.mock.patch("sampo.cost.task_costs", return_value={t: 6_000_000}):
            later = {r["name"]: r for r in scorecard.compute(self.p, time.time() + 90000)}
        self.assertEqual(later["tokens"]["value"], 6_000_000, "a day on, it is read again")

    def test_tokens_are_n_a_when_no_task_closed_in_the_window(self):
        with unittest.mock.patch("sampo.cost.task_costs", return_value={"T-9999": 5_000_000}):
            r = self.rows()["tokens"]
        self.assertIsNone(r["ok"])
        self.assertIsNone(r["value"])

    def test_steers_per_day_are_the_weeks_avoidable_ones_over_seven(self):
        with unittest.mock.patch("sampo.steermeter.rate", return_value=(0, 0)):
            self.assertIsNone(self.rows()["steers"]["ok"], "nothing read: not measured")
        with unittest.mock.patch("sampo.steermeter.rate", return_value=(7, 40)):
            r = self.rows()["steers"]
        self.assertEqual((r["value"], r["ok"]), (1.0, True))
        with unittest.mock.patch("sampo.steermeter.rate", return_value=(7 * 30, 400)):
            r = self.rows()["steers"]
        self.assertEqual((r["value"], r["ok"]), (30.0, False))

    def test_towers_climbing_are_the_ones_past_their_first_generation(self):
        with unittest.mock.patch("sampo.rune.towers", return_value=[]):
            self.assertIsNone(self.rows()["towers"]["ok"], "no tower opened: nothing to climb")
        rows = [{"name": "root", "generation": 3}] + [{"name": f"t{n}", "generation": 0} for n in range(4)]
        with unittest.mock.patch("sampo.rune.towers", return_value=rows):
            r = self.rows()["towers"]
        self.assertEqual((r["value"], r["ok"]), (0, False), "the root is not a tower; none of four climbed")
        self.assertIn("0 of 4", r["detail"])
        rows[1]["generation"] = 2
        rows[2]["generation"] = 1
        with unittest.mock.patch("sampo.rune.towers", return_value=rows):
            r = self.rows()["towers"]
        self.assertEqual((r["value"], r["ok"]), (50.0, True))

    def test_a_measure_that_cannot_be_read_is_n_a_and_never_stops_the_rest(self):
        with unittest.mock.patch("sampo.rune.towers", side_effect=RuntimeError("boom")):
            rows = {r["name"]: r for r in scorecard.compute(self.p, time.time())}
        self.assertIsNone(rows["towers"]["ok"])
        self.assertIn("boom", rows["towers"]["detail"])
        self.assertEqual(len(rows), len(NAMES))

    # ---- drift: a miss on two ticks in a row is one task

    def miss(self, name="shipped", value=9.0):
        return {"name": name, "label": "landed not shipped", "value": value, "bar": 2, "op": "<=", "unit": "h",
                "ok": False, "detail": "4 commit(s), the oldest 9 h"}

    def good(self, name="shipped"):
        return dict(self.miss(name, 0.5), ok=True)

    def tasks(self):
        return [b for b in c.load_briefs(self.p) if b.meta.get("trigger") == "scorecard"]

    def test_drift_one_miss_files_nothing_two_in_a_row_file_one_task_naming_value_and_bar(self):
        self.assertEqual(scorecard.drift(self.p, [self.miss()], 1000), [])
        self.assertEqual(self.tasks(), [])
        got = scorecard.drift(self.p, [self.miss()], 4600)
        self.assertEqual(len(got), 1)
        (b,) = self.tasks()
        self.assertEqual(got[0], (b.id, "shipped"))
        self.assertEqual(b.status, "captured")
        text = b.section("Raw request")
        self.assertIn("landed not shipped", b.title)
        self.assertIn("9", text)
        self.assertIn("<= 2 h", text, "the bar is named")
        self.assertIn("2 ticks in a row", text)

    def test_drift_a_recovery_between_two_misses_starts_the_count_again(self):
        scorecard.drift(self.p, [self.miss()], 1000)
        scorecard.drift(self.p, [self.good()], 4600)
        scorecard.drift(self.p, [self.miss()], 8200)
        self.assertEqual(self.tasks(), [], "miss, recover, miss: never two in a row")
        scorecard.drift(self.p, [self.miss()], 11800)
        self.assertEqual(len(self.tasks()), 1)

    def test_drift_an_unreadable_measure_breaks_the_run_and_files_nothing(self):
        scorecard.drift(self.p, [self.miss()], 1000)
        scorecard.drift(self.p, [dict(self.miss(), ok=None)], 4600)
        scorecard.drift(self.p, [self.miss()], 8200)
        self.assertEqual(self.tasks(), [])

    def test_drift_no_second_task_while_one_is_open_and_nothing_when_it_recovers(self):
        for t in (1000, 4600, 8200, 11800, 15400):
            scorecard.drift(self.p, [self.miss()], t)
        self.assertEqual(len(self.tasks()), 1, "still failing, the task still open: one task")
        scorecard.drift(self.p, [self.good()], 19000)
        self.assertEqual(len(self.tasks()), 1, "recovery files nothing and closes nothing")
        self.assertEqual(scorecard.drift(self.p, [self.good()], 22600), [])

    def test_drift_measures_count_apart(self):
        a, b = self.miss("shipped"), self.miss("lanes", 9)
        scorecard.drift(self.p, [a, b], 1000)
        got = scorecard.drift(self.p, [self.good("shipped"), b], 4600)
        self.assertEqual([n for _, n in got], ["lanes"], "lanes missed twice running; shipped recovered in between")
        self.assertEqual(scorecard.drift(self.p, [a, b], 8200), [], "shipped is back to one miss")

    def test_drift_a_closed_task_whose_measure_still_fails_is_not_refiled_every_tick(self):
        scorecard.drift(self.p, [self.miss()], 1000)
        scorecard.drift(self.p, [self.miss()], 4600)
        (b,) = self.tasks()
        self.fm("task", "drop", b.id, "not now")
        scorecard.drift(self.p, [self.miss()], 8200)
        self.assertEqual([x for x in self.tasks() if x.status not in c.CLOSED], [], "the next tick leaves it alone")

    def test_drift_state_is_a_small_file_and_a_damaged_one_starts_again(self):
        scorecard.drift(self.p, [self.miss()], 1000)
        path = os.path.join(self.p.dir, "scorecard.json")
        with open(path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["misses"], {"shipped": 1})
        with open(path, "w") as f:
            f.write("{not json")
        self.assertEqual(scorecard.drift(self.p, [self.miss()], 4600), [], "a torn file counts from one again")

    def age_state(self):
        path = os.path.join(self.p.dir, "scorecard.json")
        with open(path, encoding="utf-8") as f:
            st = json.load(f)
        st["at"] -= 2 * scorecard.GAP_S
        with open(path, "w", encoding="utf-8") as f:
            json.dump(st, f)

    def test_ticks_closer_than_the_gap_count_once_the_stop_hook_sweep_runs_ticks_too(self):
        scorecard.drift(self.p, [self.miss()], 1000)
        scorecard.drift(self.p, [self.miss()], 1010)  # ten seconds on: the same tick's view
        self.assertEqual(self.tasks(), [])
        self.assertFalse(scorecard.due(self.p, 1010))
        self.assertTrue(scorecard.due(self.p, 1000 + scorecard.GAP_S))

    def test_the_tick_runs_the_drift_pass_and_reports_it(self):
        from sampo import night
        miss = [self.miss()]
        with unittest.mock.patch("sampo.scorecard.compute", return_value=miss) as comp:
            night.tick(self.p, fetch_ci=False)
            night.tick(self.p, fetch_ci=False)
            self.assertEqual(comp.call_count, 1, "a second tick inside the gap reads nothing")
            self.age_state()
            res = night.tick(self.p, fetch_ci=False)
        self.assertEqual(len(self.tasks()), 1, "two ticks with a miss in a row: a task")
        self.assertEqual(res["drift"], [{"task": self.tasks()[0].id, "measure": "shipped"}])

    def test_a_scorecard_that_raises_never_costs_the_tick(self):
        from sampo import night
        with unittest.mock.patch("sampo.scorecard.compute", side_effect=RuntimeError("boom")):
            res = night.tick(self.p, fetch_ci=False)
        self.assertTrue(any("scorecard" in n and "boom" in n for n in res["notes"]), res["notes"])
