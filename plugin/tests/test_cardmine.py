"""T-4920 (T-4838 part B): cardmine turns Sampo's own records of repeated work into card requests: pick misses, failed
bench scenarios, closed FIX tasks whose fixes repeat and tool-call sequences that repeat across sessions. A request
carries counts, verbs, tool names and file kinds only, never the words of a prompt, a file or a secret; a request
already covered by a card, or already open, is not filed again, so a second run adds nothing."""
import datetime
import json
import os
import sys
import unittest
from unittest import mock

from helpers import SampoTestCase
from test_callcards import CHECK_UPPER, SCHEMA, UPPER

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
from sampo import callcards, cardmine, repeats  # noqa: E402
from sampo import core as c  # noqa: E402

SECRET = "sk-live-4f9a8b7c6d5e4f3a2b1c0d9e8f7a6b5c"
WORDS = ("fooBarQuux", "billing", "ledger", "jane@example.test", "hunter2")  # prompt, path and file words that stay home
KEYS = {"at", "source", "title", "key", "why", "count", "examples"}


def prompt(verb, rel="src/billing/ledger.py"):
    return f"{verb} the fooBarQuux helper in {rel} and mail jane@example.test, password hunter2, key {SECRET}"


class Base(SampoTestCase):
    def setUp(self):
        super().setUp()
        self.cards_dir = os.path.join(self.tmp, "cards")
        os.makedirs(self.cards_dir)
        env = mock.patch.dict(os.environ, {"SAMPO_CALLCARDS": self.cards_dir, "FOREMAN_NO_LOCAL_MODELS": "1"})
        env.start()
        self.addCleanup(env.stop)
        # only this test's cards: the shipped set would cover a request by one word it shares (the result then moves
        # with every card added to the plugin)
        dirs = mock.patch.object(callcards, "card_dirs", lambda root=None: [self.cards_dir])
        dirs.start()
        self.addCleanup(dirs.stop)
        self.p =c._project("mine", self.repo)
        os.makedirs(os.path.join(self.p.dir, "tasks"))
        self.bench = os.path.join(self.tmp, "bench.jsonl")
        self.seqs = []  # the routines `sampo repeats --sequences` finds: none unless a test sets some (no transcript walk)
        seq = mock.patch.object(repeats, "sequences", lambda p, days=repeats.SEQ_DAYS: {
            "sequences": self.seqs, "days": days, "truncated": False})
        seq.start()
        self.addCleanup(seq.stop)

    # --- fixtures, one per source
    def card(self, name, purpose):
        d = os.path.join(self.cards_dir, name)
        os.makedirs(d)
        with open(os.path.join(d, "card.json"), "w") as f:
            json.dump({"name": name, "purpose": purpose, "examples": [], "kind": "py", "inputs": SCHEMA,
                       "files": ["$path"], "deterministic": False}, f)
        for fn, text in (("gen.py", UPPER), ("check.py", CHECK_UPPER)):
            with open(os.path.join(d, fn), "w") as f:
                f.write(text)

    def rows(self, path, rows):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

    def misses(self, n, verb="rename", rel="src/billing/ledger.py"):
        self.rows(callcards.requests_path(), [{"at": "2026-11-01T00:00:00Z", "text": prompt(verb, rel),
                                               "top": [["shout", 0.04 + i / 100]]} for i in range(n)])

    def failures(self, n, cls="loop", run="r1"):
        self.rows(self.bench, [{"run": run, "scenario": f"s{i}", "kind": "bug", "model": "m", "arm": arm,
                                "passed": False, "failure_class": cls, "error": prompt("fix")}
                               for i in range(n) for arm in ("off", "on")]
                  + [{"run": run, "scenario": "ok", "kind": "bug", "model": "m", "arm": "on", "passed": True,
                      "failure_class": None}])

    def fixes(self, n, title="Fix the flaky parse of {n} in fooBarQuux", scope=("src/billing/ledger.py",),
              type_="FIX", status="done", first=100):
        for i in range(n):
            b = c.Brief.new(f"T-{first + i:04d}", title.format(n="x" * (i + 1)), type_, "S", scope=scope,
                            raw=prompt("fix"), status=status)
            with open(os.path.join(self.p.dir, "tasks", f"T-{first + i:04d}.md"), "w") as f:
                f.write(b.render())

    def calls(self, sessions, names=("Read", "Edit", "Bash", "Edit"), project="mine", start=0):
        rows, day = [], (c.clock() - datetime.timedelta(days=1)).strftime("%Y-%m-%dT")
        for s in range(sessions):
            for i, n in enumerate(names):
                rows.append({"ts": f"{day}00:{s:02d}:{i:02d}Z", "session_id": f"sess-{start + s}", "kind": "tool",
                             "tool": n, "target": prompt("fix"), "ok": True, "project": project})
        self.rows(os.path.join(c.state_dir(), "events.jsonl"), sorted(rows, key=lambda r: r["ts"]))

    def routines(self, *rows):
        """What `sampo repeats --sequences` returns (T-3921), as cardmine reads it: rows are (calls, count, sessions)."""
        self.seqs = [{"calls": list(calls), "count": n, "sessions": s, "saved": n * (len(calls) - 1),
                      "example": [prompt("fix")] * len(calls)} for calls, n, s in rows]

    def refusals(self, n, gaps=("step",), tasks=2, where="focus", event="plan_gate_refused"):
        """n plan-gate refusals on `tasks` different tasks, each naming the gaps."""
        for i in range(n):
            c.log_event(self.p, event, task=f"T-{100 + i % tasks:04d}",
                        data={"kinds": sorted({c.gap_kind(g) for g in gaps}), "where": where})

    def mine(self, **kw):
        return cardmine.mine(self.p, bench=self.bench, **kw)

    def filed(self):
        return [r for r in callcards.bench_rows(callcards.requests_path()) if "source" in r]

    def by(self, source):
        return [r for r in self.filed() if r["source"] == source]


class Sources(Base):
    def test_repeated_pick_misses_become_one_request_with_a_verb_and_file_kinds(self):
        self.misses(3)
        self.misses(2, verb="sort", rel="data/ids.json")
        self.misses(1, verb="inline", rel="x.rs")  # once is not a repeat
        r = self.mine()
        got = self.by("pick_miss")
        self.assertEqual(len(got), 2, got)
        top = max(got, key=lambda x: x["count"])
        self.assertEqual((top["count"], set(top), top["examples"][0]["verb"], top["examples"][0]["kinds"]),
                         (3, KEYS, "rename", ["python"]))
        self.assertEqual(len(r["added"]), 2)
        self.assertIn("rename", top["title"])
        self.assertIn("python", top["title"])

    def test_a_failed_bench_scenario_becomes_a_request_by_its_class(self):
        self.failures(2)
        self.failures(1, cls="timeout", run="r2")
        self.mine()
        got = {r["title"]: r for r in self.by("bench_fail")}
        self.assertEqual(len(got), 2, got)
        loop = next(r for r in got.values() if "loop" in r["title"])
        self.assertEqual(loop["count"], 2, "two scenarios, each failed on both arms")
        self.assertEqual(loop["examples"][0]["class"], "loop")
        self.assertEqual(loop["examples"][0]["kind"], "bug")

    def test_a_scenario_that_failed_with_cards_off_alone_is_not_a_request(self):
        self.rows(self.bench, [{"run": "r1", "scenario": "s0", "kind": "bug", "model": "m", "arm": "off",
                                "passed": False, "failure_class": "loop"},
                               {"run": "r1", "scenario": "s0", "kind": "bug", "model": "m", "arm": "on",
                                "passed": True, "failure_class": None}])
        self.mine()
        self.assertEqual(self.by("bench_fail"), [], "the cards answered it")

    def test_a_class_or_kind_outside_louhis_lists_is_unknown(self):
        self.rows(self.bench, [{"run": "r1", "scenario": f"s{i}", "kind": "ledger-billing", "model": "m", "arm": "on",
                                "passed": False, "failure_class": "hunter2_fooBarQuux"} for i in range(2)])
        self.mine()
        got = self.by("bench_fail")
        self.assertEqual([(r["examples"][0]["class"], r["examples"][0]["kind"], r["count"]) for r in got],
                         [("unknown", "unknown", 2)])
        with open(callcards.requests_path()) as f:
            text = f.read()
        for bad in ("hunter2", "fooBar", "billing", "ledger"):
            self.assertNotIn(bad, text)

    def test_bench_misses_filed_by_the_bench_itself_count_with_its_rows_once(self):
        self.failures(1)
        self.rows(callcards.requests_path(), [{"at": "x", "text": prompt("fix"), "top": [], "scenario": "s0",
                                               "run": "r1", "model": "m", "arm": "off", "failure_class": "loop"}])
        self.mine()
        self.assertEqual([(r["source"], r["count"]) for r in self.filed()], [("bench_fail", 1)])

    def test_repeated_closed_fixes_become_a_request(self):
        self.fixes(4)
        self.fixes(1, title="Add a CSV export", type_="FEATURE", first=200)  # not a FIX
        self.fixes(3, title="Fix the flaky parse of {n} in fooBarQuux", status="active", first=300)  # not closed
        self.mine()
        got = self.by("fix_repeat")
        self.assertEqual(len(got), 1, got)
        self.assertEqual(got[0]["count"], 4)
        ex = got[0]["examples"][0]
        self.assertEqual((ex["verbs"], ex["kinds"], ex["tiers"]), (["fix"], ["python"], {"S": 4}))

    def test_two_fixes_are_not_a_repeat(self):
        self.fixes(2)
        self.mine()
        self.assertEqual(self.by("fix_repeat"), [])

    def test_a_tool_sequence_repeated_across_sessions_becomes_a_request(self):
        self.calls(6)
        self.calls(2, names=("Grep", "Glob", "Read"), start=50)  # only two sessions
        self.calls(9, project="other", start=80)  # another project's sessions are not this one's
        self.mine()
        got = self.by("tool_sequence")
        self.assertTrue(got)
        best = max(got, key=lambda r: r["count"])
        self.assertEqual(best["count"], 6)
        self.assertEqual(best["examples"][0]["tools"], ["Read", "Edit", "Bash", "Edit"])
        self.assertEqual(best["examples"][0]["sessions"], 6)
        self.assertFalse(any("Grep" in r["title"] for r in got))

    def test_a_repeat_of_one_tool_is_not_a_sequence(self):
        self.calls(8, names=("Bash", "Bash", "Bash", "Bash"))
        self.mine()
        self.assertEqual(self.by("tool_sequence"), [])

    def test_nothing_recorded_files_nothing(self):
        self.assertEqual(self.mine()["added"], [])
        self.assertFalse(os.path.exists(callcards.requests_path()))

    def test_a_torn_line_and_a_foreign_row_are_skipped(self):
        self.misses(2)
        with open(callcards.requests_path(), "a") as f:
            f.write('{"text": "cut\n[1, 2]\n"just text"\n')
        self.assertEqual(len(self.mine()["added"]), 1)

    def test_a_dry_run_writes_nothing(self):
        self.misses(3)
        r = cardmine.mine(self.p, bench=self.bench, write=False)
        self.assertEqual(len(r["added"]), 1)
        self.assertEqual(self.filed(), [])


class Aggregates(Base):
    def plant(self):
        self.misses(3)
        self.failures(2)
        self.fixes(4)
        self.calls(6)
        # a planted tool name that is not a name: it never reaches a request either
        self.rows(os.path.join(c.state_dir(), "events.jsonl"),
                  [{"ts": c.now(), "session_id": f"z{i}", "kind": "tool", "tool": prompt("fix"),
                    "ok": True, "project": "mine"} for i in range(8)])

    def test_no_secret_prompt_word_or_path_reaches_a_request(self):
        self.plant()
        self.mine()
        rows = self.filed()
        self.assertEqual({r["source"] for r in rows}, {"pick_miss", "bench_fail", "fix_repeat", "tool_sequence"})
        with open(callcards.requests_path()) as f:
            mined = "".join(line for line in f if '"source"' in line)
        for bad in (SECRET, "sk-live", *WORDS, "src/", ".py", "fooBar", "password", "mail"):
            self.assertNotIn(bad, mined)
        for r in rows:
            self.assertEqual(set(r), KEYS, r)
            self.assertIsInstance(r["count"], int)
            self.assertIsInstance(r["examples"], list)

    def test_every_example_value_is_a_number_a_vocabulary_word_or_a_tool_name(self):
        self.plant()
        self.mine()

        def leaves(x):
            if isinstance(x, dict):
                for k, v in x.items():
                    yield str(k)
                    yield from leaves(v)
            elif isinstance(x, list):
                for v in x:
                    yield from leaves(v)
            else:
                yield x
        allowed = set(cardmine.VERBS) | set(cardmine.KINDS) | set(cardmine.COUNTED) | {"S", "M", "L"}
        for r in self.filed():
            for leaf in leaves(r["examples"]):
                ok = isinstance(leaf, (int, float)) or leaf in allowed or leaf in {"Read", "Edit", "Bash"} \
                    or leaf in ("bug", "loop", "unknown")
                self.assertTrue(ok, f"{leaf!r} in {r['source']}")

    def test_a_planted_secret_in_the_ledger_target_never_leaves_it(self):
        self.calls(6)
        self.mine()
        with open(callcards.requests_path()) as f:
            self.assertNotIn(SECRET, f.read())

    def test_mined_rows_are_not_read_back_as_pick_misses(self):
        self.misses(3)
        self.mine()
        r = cardmine.mine(self.p, bench=self.bench, write=False)
        self.assertEqual(r["added"], [])
        self.assertEqual(r["found"], 1, "the one group, not the filed request as a miss")


class Dedupe(Base):
    def test_dedupe_a_second_run_adds_nothing(self):
        self.misses(3)
        self.failures(2)
        self.fixes(4)
        self.calls(6)
        first = self.mine()
        n = len(self.filed())
        self.assertGreaterEqual(n, 4)
        self.assertEqual(len(first["added"]), n)
        second = self.mine()
        self.assertEqual(second["added"], [])
        self.assertEqual(len(self.filed()), n)
        self.assertEqual(second["open"], n)

    def test_dedupe_against_a_card_that_covers_the_request(self):
        self.card("rename-symbol", "rename a symbol in python files")
        self.misses(3)
        self.misses(3, verb="sort", rel="data/ids.json")
        r = self.mine()
        self.assertEqual([x["examples"][0]["verb"] for x in self.by("pick_miss")], ["sort"])
        self.assertEqual(r["covered"], 1)

    def test_dedupe_a_card_below_the_floor_does_not_cover(self):
        self.card("shout", "uppercase the text of one file")
        self.misses(3)
        self.assertEqual(len(self.mine()["added"]), 1)

    def test_dedupe_against_an_open_request_with_the_same_normalized_title(self):
        self.misses(3)
        self.mine()
        title = self.by("pick_miss")[0]["title"]
        os.remove(callcards.requests_path())
        self.misses(3)
        self.rows(callcards.requests_path(), [{"at": "x", "source": "hand", "title": title.upper() + "!!", "why": "w",
                                               "count": 1, "examples": []}])
        r = self.mine()
        self.assertEqual(r["added"], [])
        self.assertEqual(r["open"], 1)

    def test_dedupe_against_an_open_request_with_a_near_identical_token_set(self):
        self.misses(3)
        self.mine()
        title = self.by("pick_miss")[0]["title"]
        os.remove(callcards.requests_path())
        self.misses(3)
        self.rows(callcards.requests_path(), [{"at": "x", "source": "hand", "title": title + " again", "why": "w",
                                               "count": 1, "examples": []}])
        self.assertEqual(self.mine()["added"], [])

    def test_dedupe_a_different_request_is_not_taken_for_an_open_one(self):
        self.misses(3)
        self.misses(3, verb="sort", rel="data/ids.json")
        self.mine()
        self.assertEqual(len({r["title"] for r in self.by("pick_miss")}), 2)

    def test_dedupe_two_fix_clusters_keep_their_own_requests(self):
        self.fixes(3)
        self.fixes(3, title="Repair the broken export of {n} totals", scope=("web/app.js",), first=200)
        self.mine()
        self.assertEqual(len(self.by("fix_repeat")), 2)

    def test_dedupe_two_fix_clusters_of_one_verb_and_kind_are_told_apart_by_their_key(self):
        self.fixes(3)
        self.fixes(3, title="Fix the broken render of {n} widget", first=200)  # same verb, same python scope
        self.mine()
        got = self.by("fix_repeat")
        self.assertEqual(len(got), 2, got)
        self.assertEqual(len({r["key"] for r in got}), 2)
        self.assertTrue(all(r["key"].startswith("fix:") for r in got))
        self.assertEqual(self.mine()["added"], [], "a second run files neither again")
        self.fixes(3, title="Fix the stale cache of {n} session", first=300)  # a third one, in a later run
        self.assertEqual(len(self.mine()["added"]), 1)
        self.assertEqual(len(self.by("fix_repeat")), 3)

    def test_dedupe_a_request_without_a_key_still_matches_by_title_but_a_keyed_one_does_not(self):
        self.fixes(3)
        self.mine()
        row = self.by("fix_repeat")[0]
        os.remove(callcards.requests_path())
        self.rows(callcards.requests_path(), [{k: v for k, v in row.items() if k != "key"}])
        self.assertEqual(self.mine()["added"], [], "an old row with no key: its title holds")

    def test_dedupe_a_bench_failure_and_a_repeated_fix_are_covered_by_a_card_like_a_pick_miss(self):
        self.card("loop-bug", "a bug that loops")
        self.card("fix-python", "fix python")
        self.failures(2)
        self.fixes(3)
        r = self.mine()
        self.assertEqual((self.by("bench_fail"), self.by("fix_repeat")), ([], []))
        self.assertEqual(r["covered"], 2)

    def test_dedupe_a_card_does_not_cover_a_tool_sequence(self):
        self.card("read-edit", "read edit bash")
        self.calls(6)
        self.assertEqual(len(self.mine()["added"]), 1)

    def test_a_busy_lock_files_nothing_and_says_why(self):
        self.misses(3)
        with mock.patch.object(c, "lock", side_effect=c.LockTimeout("lock busy: x")):
            r = self.mine()
        self.assertEqual((r["added"], r["found"]), ([], 1))
        self.assertIn("busy", r["reason"])
        self.assertEqual(self.filed(), [])
        self.assertEqual(len(self.mine()["added"]), 1, "the next run files it")

    def test_dedupe_a_cap_per_source_keeps_a_second_run_empty(self):
        for i in range(cardmine.TOP + 3):
            self.misses(2, verb=cardmine.VERBS[i], rel=f"a{i}.py")
        first = self.mine()
        self.assertEqual(len(first["added"]), cardmine.TOP)
        second = self.mine()
        self.assertEqual(second["added"], [])
        self.assertEqual(second["found"], cardmine.TOP, "the three left out stay out: no reaching further down")

    def test_dedupe_writes_hold_while_two_runs_race(self):
        self.misses(3)
        import threading
        out = []
        ts = [threading.Thread(target=lambda: out.append(len(self.mine()["added"]))) for _ in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(sorted(out), [0, 0, 0, 1])
        self.assertEqual(len(self.filed()), 1)


class Repeats(Base):
    """T-5887: the sessions' own repeated chores are card requests too: routines `sampo repeats --sequences` ranks, the
    plan gate's refusals by the kind of gap, and the guard refusals Sampo forgave."""
    ROUTINE = ("sampo task evidence", "git status", "sampo lane accept")

    def test_a_routine_of_three_calls_in_two_sessions_becomes_a_request_naming_its_source_and_count(self):
        self.routines((self.ROUTINE, 5, 3))
        r = self.mine()
        got = self.by("routine")
        self.assertEqual(len(got), 1, got)
        row = got[0]
        self.assertEqual((set(row), row["count"], row["key"].startswith("routine:")), (KEYS, 5, True))
        self.assertEqual(row["examples"][0]["tools"], list(self.ROUTINE))
        self.assertEqual(row["examples"][0]["sessions"], 3)
        self.assertIn("sampo task evidence", row["title"])
        self.assertIn("5", row["why"])
        self.assertIn("sampo repeats", row["why"])
        self.assertEqual(len(r["added"]), 1)

    def test_a_routine_of_two_calls_or_one_session_is_not_a_request(self):
        self.routines((("git status", "git diff"), 9, 4), (self.ROUTINE, 6, 1))
        self.mine()
        self.assertEqual(self.by("routine"), [])

    def test_only_the_top_routines_are_requested_per_run_and_a_second_run_adds_nothing(self):
        self.routines(*[((f"sampo task {w}", f"git {w}", "Edit"), 10 - i, 2) for i, w in enumerate(
            ["alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta"])])
        first = self.mine()
        self.assertEqual(len(self.by("routine")), cardmine.NEW_TOP)
        self.assertEqual(self.by("routine")[0]["count"], 10)
        second = self.mine()
        self.assertEqual((second["added"], len(self.by("routine"))), ([], cardmine.NEW_TOP))
        self.assertEqual(len(first["added"]), cardmine.NEW_TOP)

    def test_a_routine_is_deduped_against_an_open_request_and_a_card(self):
        self.routines((self.ROUTINE, 5, 3))
        self.mine()
        title = self.by("routine")[0]["title"]
        os.remove(callcards.requests_path())
        self.rows(callcards.requests_path(), [{"at": "x", "source": "hand", "title": title, "why": "w", "count": 1,
                                               "examples": []}])
        self.assertEqual(self.mine()["added"], [], "an open hand-written request with the same title")
        os.remove(callcards.requests_path())
        self.card("sampo-evidence", "sampo task evidence git status sampo lane accept")
        r = self.mine()
        self.assertEqual((r["added"], r["covered"]), ([], 1), "a card that ranks for it covers it")

    def test_a_routine_keeps_command_names_and_drops_other_words(self):
        self.routines((("sampo task evidence", "grep fooBarQuux hunter2 " + SECRET, "Agent sampo:fm-builder"), 4, 2),
                      (("Read", f"curl https://jane@example.test/{SECRET}", "Edit"), 4, 2))
        self.mine()
        with open(callcards.requests_path()) as f:
            mined = f.read()
        for bad in (SECRET, "sk-live", "fooBar", "hunter2", "jane", "example.test"):
            self.assertNotIn(bad, mined)
        self.assertIn("grep", mined)

    def test_a_routine_the_repeats_scan_cannot_read_files_nothing(self):
        with mock.patch.object(repeats, "sequences", side_effect=OSError("no transcripts")):
            self.assertEqual(self.mine()["added"], [])

    def test_a_repeated_plan_gate_refusal_kind_becomes_a_request_naming_its_source_and_count(self):
        self.refusals(4, gaps=("Approach (options → choice → why): 3+ options", "step"))
        self.refusals(3, gaps=("acceptance criterion",), where="lane")
        self.mine()
        got = {r["key"]: r for r in self.by("plan_refusal")}
        self.assertEqual(set(got), {"plan:approach", "plan:steps", "plan:criteria"}, got)
        self.assertEqual((got["plan:approach"]["count"], got["plan:criteria"]["count"]), (4, 3))
        self.assertIn("plan gate", got["plan:approach"]["why"])
        self.assertIn("4", got["plan:approach"]["why"])
        self.assertEqual(set(got["plan:approach"]), KEYS)

    def test_a_plan_kind_refused_twice_or_on_one_task_is_not_a_repeat(self):
        self.refusals(2, gaps=("step",))
        self.refusals(5, gaps=("acceptance criterion",), tasks=1)  # one task asked again and again is that task's gap
        self.mine()
        self.assertEqual(self.by("plan_refusal"), [])

    def test_plan_refusals_are_deduped_on_a_second_run(self):
        self.refusals(4, gaps=("step",))
        self.assertEqual(len(self.mine()["added"]), 1)
        self.assertEqual(self.mine()["added"], [])

    def test_a_guard_refusal_sampo_forgave_three_times_becomes_a_request(self):
        for _ in range(3):
            c.log_event(self.p, "guard_forgiven", data={"category": "rewrite", "rule": "sed-in-place"})
        c.log_event(self.p, "guard_forgiven", data={"category": "other", "rule": "x"})
        self.mine()
        got = self.by("guard_false")
        self.assertEqual([(r["key"], r["count"]) for r in got], [("guard:rewrite:sed-in-place", 3)])
        self.assertIn("forgave", got[0]["why"])

    def test_the_new_sources_carry_no_secret_or_prompt_word(self):
        self.routines((("sampo task evidence", "git status", "Edit"), 4, 2))
        self.refusals(3, gaps=("step",))
        for _ in range(3):
            c.log_event(self.p, "guard_forgiven", data={"category": "rewrite", "rule": prompt("fix")})
        self.mine()
        self.assertEqual({r["source"] for r in self.filed()}, {"routine", "plan_refusal"})
        with open(callcards.requests_path()) as f:
            mined = f.read()
        for bad in (SECRET, "fooBar", "jane", "hunter2"):
            self.assertNotIn(bad, mined)

    def test_a_refused_plan_gate_logs_the_kinds_of_gap_it_named(self):
        c.log_plan_gate_refused(self.p, ["Interpretation: 2 readings", "step", "a cold rival read (x)",
                                         "approval (M tier needs it)", "an Undo: line on step 2", "something new"],
                                task="T-0001", where="lane")
        ev = c.ledger_tail(self.p, 5, kinds=["plan_gate_refused"])
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["task"], "T-0001")
        self.assertEqual(ev[0]["data"], {"kinds": ["approval", "door", "interpretation", "other", "rival", "steps"],
                                         "where": "lane"})

    def test_a_focus_refusal_for_a_thin_plan_is_logged_as_a_plan_gate_refusal(self):
        from sampo import cli
        b = c.Brief.new("T-0777", "Thin plan", "FIX", "S", raw="x")
        with mock.patch.object(c, "effective_autonomy", lambda p, meta=None: "standard"), \
                self.assertRaises(c.PolicyError):
            cli._focus_refusal(self.p, b)
        ev = c.ledger_tail(self.p, 5, kinds=["plan_gate_refused"])
        self.assertEqual([(e["task"], e["data"]["where"]) for e in ev], [("T-0777", "focus")])
        self.assertIn("criteria", ev[0]["data"]["kinds"])


if __name__ == "__main__":
    unittest.main()
