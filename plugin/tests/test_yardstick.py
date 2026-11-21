"""T-5006 (slice of T-4314): the yardstick reads the three bench result stores (bench cases, SWE results.jsonl,
Terminal-Bench results.json) into one row shape, tags each row with a kind and an arm (local or claude), and says per
kind how far the local model trails Claude on the tasks both arms ran. No run happens here: temp-dir fixtures of the
stored shapes."""
import json
import os
import sys
from unittest import mock

from helpers import SampoTestCase

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
from sampo import yardstick  # noqa: E402

PROFILES = {"gemma": {"name": "gemma", "kind": "openai", "base_url": "http://127.0.0.1:11434/v1", "model": "g"},
            "lanbox": {"name": "lanbox", "kind": "openai", "base_url": "http://192.168.1.20:11434/v1", "model": "l"},
            "hosted": {"name": "hosted", "kind": "openai", "base_url": "https://api.example.test/v1", "model": "h"},
            "sonnet-api": {"name": "sonnet-api", "kind": "anthropic", "base_url": "https://api.anthropic.com/v1",
                           "model": "claude-sonnet-5-5"}}


def put(path, data, lines=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        if lines:
            f.write("".join(x if isinstance(x, str) else json.dumps(x) + "\n" for x in data))
        else:
            json.dump(data, f)


class Fixture(SampoTestCase):
    def setUp(self):
        super().setUp()
        self.state = os.path.join(self.tmp, "state")

    def cases(self, label, model, passes, slug="proj", types=None, **meta):
        """A bench run file: passes is {case id: bool}; the project's cases.json types them (FIX unless `types`);
        `meta` is what run_arm stores beside the cases (at, bare, ablate, extra, head, ...)."""
        base = os.path.join(self.state, "projects", slug, "bench")
        types = types or {}
        put(os.path.join(base, "cases.json"), [{"id": i, "type": types.get(i, "FIX")} for i in passes])
        put(os.path.join(base, "results", label + ".json"),
            {"label": label, "model": model, "cases": [{"id": i, "pass": ok} for i, ok in passes.items()], **meta})

    def swe(self, run, model, rows):
        """rows: (repo, id, harness, resolved[, extra fields])."""
        put(os.path.join(self.state, "bench-swe", run, "results.jsonl"),
            [{"instance_id": f"{x[0]}__{x[1]}", "repo": x[0], "model": model, "harness": x[2], "resolved": x[3],
              "patch": "x", **(x[4] if len(x) > 4 else {})} for x in rows], lines=True)

    def tb(self, run, model, tasks):
        """tasks: (category, id, harness, passed[, ended])."""
        put(os.path.join(self.state, "bench", "tbench", run, "results.json"),
            {"model": model, "harnesses": ["sampo"], "n": len(tasks),
             "tasks": [{"id": x[1], "harness": x[2], "category": x[0], "passed": x[3],
                        "ended": x[4] if len(x) > 4 else "done"} for x in tasks]})

    def rows(self):
        return yardstick.rows(self.state, PROFILES)


class Rows(Fixture):
    def test_each_store_becomes_rows_with_kind_and_arm(self):
        self.cases("c1", "sonnet", {"T-1": True, "T-2": False}, types={"T-2": "FEATURE"})
        self.swe("20261101-gemma", "gemma", [("django/django", "1", "sampo", True)])
        self.tb("20261102-gemma", "gemma", [("software-engineering", "t1", "sampo", False)])
        rows = {(r["suite"], r["id"]): r for r in self.rows()}
        self.assertEqual(len(rows), 4)
        self.assertEqual((rows["cases", "T-1"]["kind"], rows["cases", "T-1"]["arm"], rows["cases", "T-1"]["passed"]),
                         ("cases:FIX", "claude", True))
        self.assertEqual(rows["cases", "T-2"]["kind"], "cases:FEATURE")
        self.assertFalse(rows["cases", "T-2"]["passed"])
        swe = rows["swe", "django/django__1"]
        self.assertEqual((swe["kind"], swe["arm"], swe["passed"]), ("swe:django/django", "local", True))
        tb = rows["tbench", "t1"]
        self.assertEqual((tb["kind"], tb["arm"], tb["passed"]), ("tbench:software-engineering", "local", False))

    def test_no_stores_no_rows(self):
        self.assertEqual(self.rows(), [])
        self.assertEqual(yardstick.gap([]), {})

    def test_state_dir_defaults_to_the_running_state(self):
        self.state = os.path.join(self.home, "state")
        self.swe("r", "gemma", [("a/b", "1", "sampo", True)])
        self.assertEqual(len(yardstick.rows(profiles=PROFILES)), 1)

    def test_arm_comes_from_the_model_profile(self):
        arm = yardstick.arm_of
        self.assertEqual(arm("gemma", PROFILES), "local")  # a loopback base_url
        self.assertEqual(arm("sonnet-api", PROFILES), "claude")  # kind anthropic
        self.assertEqual(arm("hosted", PROFILES), "other")  # a remote model that isn't Claude: no side of the gap
        for name in ("sonnet", "opus", "haiku", "claude-sonnet-5-5", None, ""):  # Claude Code's own aliases, default
            self.assertEqual(arm(name, {}), "claude", name)
        for name in ("ollama", "lmstudio", "llamacpp", "vllm", "gemma3:12b"):  # the built-in local servers' names
            self.assertEqual(arm(name, {}), "local", name)
        self.assertEqual(arm("mystery", {}), "other")

    def test_a_private_network_host_is_a_local_model_too(self):
        def prof(url):
            return {"m": {"name": "m", "kind": "openai", "base_url": url, "model": "m"}}
        for url in ("http://192.168.1.20:11434/v1", "http://10.0.0.5/v1", "http://172.16.3.4:8000/v1",
                    "http://gpubox.local:11434/v1", "http://gpubox:11434/v1", "http://[fd00::5]:1234/v1",
                    "http://localhost:1/v1"):
            self.assertEqual(yardstick.arm_of("m", prof(url)), "local", url)
        for url in ("https://api.example.test/v1", "http://8.8.8.8/v1", "http://172.32.0.1/v1", "", "http://[::1/v1"):
            self.assertEqual(yardstick.arm_of("m", prof(url)), "other", url)

    def test_models_json_profiles_are_read_when_none_given(self):
        put(os.path.join(self.home, "models.json"),
            {"profiles": {"mine": {"kind": "openai", "base_url": "http://localhost:9/v1", "model": "m"}}})
        self.swe("r", "mine", [("a/b", "1", "sampo", True)])
        self.assertEqual([r["arm"] for r in yardstick.rows(self.state)], ["local"])

    def test_unreadable_models_json_does_not_stop_the_rows(self):
        with open(os.path.join(self.home, "models.json"), "w") as f:
            f.write("{not json")
        self.swe("r", "ollama", [("a/b", "1", "sampo", True)])
        self.assertEqual([r["arm"] for r in yardstick.rows(self.state)], ["local"])

    def test_case_missing_from_cases_json_has_an_unknown_kind(self):
        self.cases("c1", "sonnet", {"T-1": True})
        put(os.path.join(self.state, "projects", "proj", "bench", "cases.json"), [])
        self.assertEqual([r["kind"] for r in self.rows()], ["cases:unknown"])

    def test_broken_files_and_lines_are_skipped_not_fatal(self):
        self.swe("good", "gemma", [("a/b", "1", "sampo", True)])
        put(os.path.join(self.state, "bench-swe", "torn", "results.jsonl"),
            ['{"instance_id": "x__1", "repo": "a/b", "model": "gemma", "resolved": true}\n', "{torn\n", "[1]\n",
             '{"instance_id": 5}\n', '"text"\n'], lines=True)
        put(os.path.join(self.state, "bench", "tbench", "bad", "results.json"), ["not", "a", "dict"])
        put(os.path.join(self.state, "bench", "tbench", "worse", "results.json"), {"model": "gemma", "tasks": "x"})
        os.makedirs(os.path.join(self.state, "projects", "empty", "bench", "results"))
        put(os.path.join(self.state, "projects", "odd", "bench", "results", "r.json"), {"cases": [1, {"id": "a"}]})
        put(os.path.join(self.state, "projects", "odd", "bench", "results", "s.json"), {"cases": [], "at": 5})
        rows = self.rows()
        self.assertEqual(sorted(r["id"] for r in rows if r["suite"] == "swe"), ["a/b__1", "x__1"])
        self.assertEqual([r for r in rows if r["suite"] != "swe"][0]["id"], "a")

    def test_the_newest_run_of_a_task_counts_once_per_arm_whatever_the_model(self):
        self.swe("old", "gemma", [("a/b", "1", "sampo", False)])
        os.utime(os.path.join(self.state, "bench-swe", "old", "results.jsonl"), (1, 1))
        self.swe("new", "ollama", [("a/b", "1", "sampo", True)])  # another local model: the same arm, the newer run
        rows = self.rows()
        self.assertEqual([(r["model"], r["passed"]) for r in rows], [("ollama", True)])

    def test_a_default_model_and_a_named_alias_are_one_arm_row_per_task(self):
        self.cases("a", None, {"T-1": False}, at="2026-11-01T00:00:00Z")
        self.cases("b", "sonnet", {"T-1": True}, at="2026-11-02T00:00:00Z")
        self.assertEqual([r["passed"] for r in self.rows()], [True])

    def test_recency_is_the_stored_at_before_the_file_time(self):
        self.cases("late-at", "sonnet", {"T-1": True}, at="2026-11-10T00:00:00Z")
        self.cases("early-at", "sonnet", {"T-1": False}, at="2026-11-01T00:00:00Z")
        os.utime(os.path.join(self.state, "projects", "proj", "bench", "results", "late-at.json"), (1, 1))
        self.assertEqual([r["passed"] for r in self.rows()], [True])  # the file written last holds the older run

    def test_a_kind_is_one_line_without_control_characters(self):
        self.swe("r", "gemma", [("a/b\x1b[31m\nx", "1", "sampo", True)])
        kind = self.rows()[0]["kind"]
        self.assertTrue(kind.isprintable(), repr(kind))
        self.assertLessEqual(len(kind), 90)

    def test_a_rival_plugin_a_version_duel_and_an_ablation_are_not_plain_sampo(self):
        self.cases("plain", "sonnet", {"T-1": True})
        self.cases("rival", "sonnet", {"T-1": False}, extra=["/plug/rival-one"])
        self.cases("old", "sonnet", {"T-1": False}, head="0123456789abcdef", plugin="/wt/plugin")
        self.cases("abl", "sonnet", {"T-1": False}, ablate="drive")
        self.cases("bare", "sonnet", {"T-1": False}, bare=True)
        self.cases("snip", "sonnet", {"T-1": False}, bare="snippet")
        self.cases("mara", "sonnet", {"T-1": False}, marathon=3, compact_at=50000)
        by = {r["harness"]: r["passed"] for r in self.rows()}
        self.assertEqual(by["sampo"], True)  # none of the others replaced plain Sampo's row
        self.assertEqual(len(by), 7, by)
        self.assertIn("ablate:drive", by)
        self.assertIn("bare", by)
        self.assertTrue(any(h.startswith("extra:") and "rival-one" in h for h in by), by)
        self.assertTrue(any(h.startswith("head:01234567") for h in by), by)

    def test_infrastructure_failures_are_marked_not_counted_as_model_failures(self):
        self.swe("r", "gemma", [("a/b", "1", "sampo", False, {"failure": "pull", "reason": "pull failed: x"}),
                                ("a/b", "2", "sampo", False, {"failure": "docker", "reason": "docker: gone"}),
                                ("a/b", "3", "sampo", False, {"failure": "other", "reason": "harness error: KeyError"}),
                                ("a/b", "4", "sampo", False, {"failure": "wrong_fix", "reason": "F2P 0/2"})])
        self.tb("t", "gemma", [("sys", "b", "sampo", False, "build_failed"), ("sys", "g", "sampo", False, "grade_failed"),
                               ("sys", "s", "sampo", False, "steps")])
        marked = {r["id"]: r["excluded"] for r in self.rows()}
        self.assertEqual(sorted(i for i, e in marked.items() if e), ["a/b__1", "a/b__2", "a/b__3", "b", "g"])
        self.assertFalse(marked["a/b__4"] or marked["s"])

    def test_an_infrastructure_failure_does_not_replace_an_earlier_real_result(self):
        self.swe("old", "gemma", [("a/b", "1", "sampo", True)])
        os.utime(os.path.join(self.state, "bench-swe", "old", "results.jsonl"), (1, 1))
        self.swe("new", "gemma", [("a/b", "1", "sampo", False, {"failure": "docker", "reason": "docker: gone"})])
        self.assertEqual([(r["passed"], r["excluded"]) for r in self.rows()], [(True, "")])

    def test_a_store_past_the_row_cap_loses_its_oldest_rows(self):
        self.swe("a-old", "gemma", [("a/b", "old", "sampo", True)])  # sorts first by name: the cap must go by time
        os.utime(os.path.join(self.state, "bench-swe", "a-old", "results.jsonl"), (1, 1))
        self.swe("b-new", "gemma", [("a/b", "new", "sampo", True)])
        with mock.patch.object(yardstick, "MAX_ROWS", 1):
            self.assertEqual([r["id"] for r in self.rows()], ["a/b__new"])


class Gap(Fixture):
    def test_gap_is_claude_pass_rate_minus_local_pass_rate_per_kind(self):
        self.swe("c", "sonnet-api", [("a/b", "1", "sampo", True), ("a/b", "2", "sampo", True),
                                     ("a/b", "3", "sampo", True), ("a/b", "4", "sampo", False)])
        self.swe("l", "gemma", [("a/b", "1", "sampo", True), ("a/b", "2", "sampo", False),
                                ("a/b", "3", "sampo", False), ("a/b", "4", "sampo", False)])
        g = yardstick.gap(self.rows())["swe:a/b"]
        self.assertEqual((g["claude_pass"], g["claude_n"], g["local_pass"], g["local_n"]), (3, 4, 1, 4))
        self.assertEqual(g["paired"], 4)
        self.assertAlmostEqual(g["claude"], 0.75)
        self.assertAlmostEqual(g["local"], 0.25)
        self.assertAlmostEqual(g["gap"], 0.5)

    def test_gap_is_negative_when_local_leads(self):
        self.tb("c", "sonnet", [("sys", "t1", "sampo", False)])
        self.tb("l", "gemma", [("sys", "t1", "sampo", True)])
        self.assertAlmostEqual(yardstick.gap(self.rows())["tbench:sys"]["gap"], -1.0)

    def test_only_tasks_both_arms_ran_are_compared(self):
        self.swe("c", "sonnet-api", [("a/b", "1", "sampo", True)])
        self.swe("l", "gemma", [("a/b", "1", "sampo", True), ("a/b", "2", "sampo", False)])
        g = yardstick.gap(self.rows())["swe:a/b"]
        self.assertEqual((g["paired"], g["claude_n"], g["local_n"]), (1, 1, 1))
        self.assertAlmostEqual(g["gap"], 0.0)  # not +50 pts: claude never ran task 2
        self.assertEqual((g["claude_only"], g["local_only"]), (0, 1))  # the unpaired row is a count, not a rate

    def test_a_kind_with_one_arm_has_no_gap(self):
        self.tb("c", "sonnet", [("sys", "t1", "sampo", True)])
        g = yardstick.gap(self.rows())["tbench:sys"]
        self.assertIsNone(g["gap"])
        self.assertIsNone(g["local"])
        self.assertIsNone(g["claude"])  # no paired task: no rate to quote
        self.assertEqual((g["paired"], g["claude_only"], g["local_only"]), (0, 1, 0))

    def test_other_models_are_counted_but_on_neither_side(self):
        self.tb("h", "hosted", [("sys", "t1", "sampo", True)])
        g = yardstick.gap(self.rows())
        self.assertEqual(g["tbench:sys"]["other"], 1)
        self.assertIsNone(g["tbench:sys"]["gap"])
        self.assertEqual((g["tbench:sys"]["claude_only"], g["tbench:sys"]["local_only"]), (0, 0))

    def test_a_lan_box_model_is_the_local_arm(self):
        self.swe("c", "sonnet-api", [("a/b", "1", "sampo", True)])
        self.swe("l", "lanbox", [("a/b", "1", "sampo", False)])
        self.assertAlmostEqual(yardstick.gap(self.rows())["swe:a/b"]["gap"], 1.0)

    def test_infrastructure_failures_are_excluded_and_counted_per_arm(self):
        self.swe("c", "sonnet-api", [("a/b", "1", "sampo", True), ("a/b", "2", "sampo", True)])
        self.swe("l", "gemma", [("a/b", "1", "sampo", True),
                                ("a/b", "2", "sampo", False, {"failure": "docker", "reason": "docker: gone"})])
        g = yardstick.gap(self.rows())["swe:a/b"]
        self.assertEqual((g["paired"], g["excluded_local"], g["excluded_claude"]), (1, 1, 0))
        self.assertAlmostEqual(g["gap"], 0.0)

    def test_kinds_of_different_suites_stay_apart_and_all_pools_the_paired_tasks(self):
        self.cases("c", "sonnet", {"T-1": True, "T-2": True})
        self.cases("l", "gemma", {"T-1": False, "T-2": True})
        self.swe("c", "sonnet-api", [("a/b", "1", "sampo", True)])
        self.swe("l", "gemma", [("a/b", "1", "sampo", True)])
        g = yardstick.gap(self.rows())
        self.assertEqual(sorted(g), ["all", "cases:FIX", "swe:a/b"])
        self.assertAlmostEqual(g["cases:FIX"]["gap"], 0.5)
        self.assertAlmostEqual(g["swe:a/b"]["gap"], 0.0)
        self.assertEqual((g["all"]["paired"], g["all"]["claude_n"], g["all"]["local_n"]), (3, 3, 3))
        self.assertAlmostEqual(g["all"]["gap"], 1 / 3)

    def test_all_leaves_out_a_kind_only_one_arm_ran(self):
        self.cases("c", "sonnet", {"T-1": True, "T-2": True, "T-3": True})  # bench cases drive Claude Code: claude only
        self.swe("c", "sonnet-api", [("a/b", "1", "sampo", False)])
        self.swe("l", "gemma", [("a/b", "1", "sampo", False)])
        g = yardstick.gap(self.rows())
        self.assertAlmostEqual(g["all"]["gap"], 0.0)  # not skewed by three claude passes nobody paired
        self.assertEqual((g["all"]["paired"], g["all"]["claude_only"], g["all"]["local_only"]), (1, 3, 0))

    def test_harness_filter_compares_like_with_like(self):
        self.swe("c", "sonnet-api", [("a/b", "1", "sampo", True), ("a/b", "1", "bare", False)])
        self.swe("l", "gemma", [("a/b", "1", "sampo", False), ("a/b", "1", "bare", False)])
        rows = self.rows()
        self.assertAlmostEqual(yardstick.gap(rows)["swe:a/b"]["gap"], 0.5)
        self.assertAlmostEqual(yardstick.gap(rows, harness="sampo")["swe:a/b"]["gap"], 1.0)
        self.assertAlmostEqual(yardstick.gap(rows, harness="bare")["swe:a/b"]["gap"], 0.0)

    def test_a_harness_pairs_only_with_the_same_harness(self):
        self.swe("c", "sonnet-api", [("a/b", "1", "sampo", True)])
        self.swe("l", "gemma", [("a/b", "1", "bare", False)])
        g = yardstick.gap(self.rows())["swe:a/b"]
        self.assertEqual((g["paired"], g["claude_only"], g["local_only"]), (0, 1, 1))

    def test_ablations_and_rival_runs_stay_out_of_an_unfiltered_gap(self):
        self.cases("c", "sonnet", {"T-1": True})
        self.cases("abl", "sonnet", {"T-1": False}, ablate="drive")
        self.cases("riv", "sonnet", {"T-1": False}, extra=["/p/rival"])
        self.cases("l", "gemma", {"T-1": True})
        rows = self.rows()
        g = yardstick.gap(rows)["cases:FIX"]
        self.assertEqual((g["paired"], g["claude_only"]), (1, 0))
        self.assertAlmostEqual(g["gap"], 0.0)
        ab = yardstick.gap(rows, harness="ablate:drive")["cases:FIX"]
        self.assertEqual((ab["paired"], ab["claude_only"]), (0, 1))  # asked for by name, it is there

    def test_table_lists_the_widest_gap_first_with_counts(self):
        self.swe("c", "sonnet-api", [("a/b", "1", "sampo", True), ("c/d", "1", "sampo", True)])
        self.swe("l", "gemma", [("a/b", "1", "sampo", True), ("c/d", "1", "sampo", False)])
        self.tb("c", "sonnet", [("sys", "t1", "sampo", True)])  # no local row: no gap, listed after the gaps
        text = yardstick.table(yardstick.gap(self.rows()), min_paired=1)
        lines = text.splitlines()
        self.assertIn("kind", lines[0])
        body = [l for l in lines[1:] if l.strip()]
        self.assertTrue(body[0].startswith("swe:c/d"), body)
        self.assertIn("+100", body[0])
        self.assertIn("1/1", body[0])
        self.assertIn("0/1", body[0])
        self.assertTrue(body[-1].startswith("all"), body)  # the pooled line is the footer
        self.assertTrue(body[-2].startswith("tbench:sys") and "n/a" in body[-2], body)
        self.assertIn("unpaired", body[-2])  # the claude-only task is said, not dropped

    def test_a_kind_under_the_floor_is_marked_and_sorts_after_the_ones_over_it(self):
        ids = [str(i) for i in range(1, 5)]
        self.swe("c", "sonnet-api", [("big/repo", i, "sampo", True) for i in ids] + [("tiny/repo", "1", "sampo", True)])
        self.swe("l", "gemma", [("big/repo", i, "sampo", i == "1") for i in ids] + [("tiny/repo", "1", "sampo", False)])
        body = yardstick.table(yardstick.gap(self.rows())).splitlines()[1:]
        self.assertTrue(body[0].startswith("swe:big/repo") and "low n" not in body[0], body)
        self.assertTrue(body[1].startswith("swe:tiny/repo") and "low n" in body[1], body)  # +100 pts on one task
        self.assertEqual(yardstick.MIN_PAIRED, 3)

    def test_empty_table_says_so(self):
        self.assertIn("No bench rows", yardstick.table({}))
