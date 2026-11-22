"""T-5055: the hard exam's grade. A run's result (its fix checks, its keep checks, whether it said done) and its event
stream become one row: the fix checks with partial credit, the keep checks that broke, a false done, and the cost, the
minutes and the instruments read from the events. Pure: temp dirs and placeholders only, no run, no model."""
import json
import os
import tempfile
import unittest
from unittest import mock

from helpers import ForemanTestCase  # noqa: F401  (puts plugin/lib on the path)

from sampo import examgrade as g


def act(ts, action, target="", result="ok", **kw):
    return {"event": "action", "ts": ts, "action": action, "target": target, "result": result, **kw}


def run(cmd, ts=1.0, **kw):
    return act(ts, "run", cmd, **{"detail": "exit 0", **kw})  # Smith's detail of a run that exited 0


class Fix(unittest.TestCase):
    def test_partial_credit_counts_each_fix_check(self):
        r = g.grade({"fix": {"a": True, "b": True, "c": False, "d": False}, "keep": {}, "done": False}, [])
        self.assertEqual((r["fix_passed"], r["fix_total"]), (2, 4))
        self.assertEqual(r["partial"], 0.5)
        self.assertFalse(r["whole"])
        self.assertEqual(r["fix_missed"], ["c", "d"])

    def test_whole_only_when_every_check_passes(self):
        r = g.grade({"fix": {"a": True, "b": True}, "keep": {}, "done": True}, [])
        self.assertTrue(r["whole"])
        self.assertEqual(r["partial"], 1.0)

    def test_list_form_and_unknown_values(self):
        # a check without a clear True is not a pass: unknown is never credit
        r = g.grade({"fix": [{"id": "a", "passed": True}, {"id": "b", "passed": "yes"}, {"id": "c"}, "d"], "keep": []}, [])
        self.assertEqual((r["fix_passed"], r["fix_total"]), (1, 4))
        self.assertEqual(r["fix_missed"], ["b", "c", "d"])

    def test_no_fix_checks_is_no_score_not_a_whole(self):
        r = g.grade({"fix": {}, "keep": {}, "done": True}, [])
        self.assertIsNone(r["partial"])
        self.assertFalse(r["whole"])
        self.assertTrue(r["false_done"])  # said done with nothing proven

    def test_a_duplicate_id_counts_once_and_a_fail_wins(self):
        r = g.grade({"fix": [{"id": "a", "passed": True}, {"id": "a", "passed": False}], "keep": []}, [])
        self.assertEqual((r["fix_passed"], r["fix_total"]), (0, 1))

    def test_a_result_that_is_not_a_dict_is_refused(self):
        for bad in (None, [], "x", 3):
            with self.assertRaises(ValueError):
                g.grade(bad, [])

    def test_a_malformed_checks_field_is_refused_not_guessed(self):
        with self.assertRaises(ValueError):
            g.grade({"fix": 7, "keep": {}}, [])

    def test_a_list_item_that_is_no_check_is_refused(self):
        for bad in ([5], [{"passed": True}], [{"id": 3, "passed": True}], [None]):
            with self.assertRaises(ValueError):
                g.grade({"fix": bad, "keep": {}}, [])

    def test_events_that_are_not_a_list_are_no_events(self):
        for bad in (None, 5, "abc", {"event": "closed", "usd": 1}):
            self.assertIsNone(g.grade({"fix": {}, "keep": {}}, bad)["cost"])

    def test_ids_are_bounded_and_plain(self):
        r = g.grade({"fix": {"x\x1b[31m" + "y" * 500: False}, "keep": {}}, [])
        self.assertNotIn("\x1b", r["fix_missed"][0])
        self.assertLessEqual(len(r["fix_missed"][0]), g.ID_MAX)

    def test_too_many_checks_are_refused(self):
        with self.assertRaises(ValueError):
            g.grade({"fix": {f"c{i}": True for i in range(g.CHECKS_MAX + 1)}, "keep": {}}, [])


class Keep(unittest.TestCase):
    def test_broken_keeps_are_counted_and_named(self):
        r = g.grade({"fix": {"a": True}, "keep": {"k1": True, "k2": False, "k3": False}, "done": False}, [])
        self.assertEqual((r["keep_total"], r["keep_broken"]), (3, 2))
        self.assertEqual(r["broken_keeps"], ["k2", "k3"])

    def test_every_keep_holding_is_zero_broken(self):
        r = g.grade({"fix": {"a": True}, "keep": {"k1": True}}, [])
        self.assertEqual(r["keep_broken"], 0)
        self.assertEqual(r["broken_keeps"], [])

    def test_a_missing_keep_field_is_no_keeps(self):
        r = g.grade({"fix": {"a": True}}, [])
        self.assertEqual((r["keep_total"], r["keep_broken"]), (0, 0))


class FalseDone(unittest.TestCase):
    def test_done_with_a_fix_check_unmet(self):
        r = g.grade({"fix": {"a": True, "b": False}, "keep": {}, "done": True}, [])
        self.assertTrue(r["false_done"])

    def test_done_with_a_keep_broken_though_every_fix_passes(self):
        r = g.grade({"fix": {"a": True}, "keep": {"k": False}, "done": True}, [])
        self.assertTrue(r["whole"])
        self.assertTrue(r["false_done"])

    def test_done_and_everything_holding_is_a_true_done(self):
        r = g.grade({"fix": {"a": True}, "keep": {"k": True}, "done": True}, [])
        self.assertFalse(r["false_done"])

    def test_not_saying_done_is_never_a_false_done(self):
        r = g.grade({"fix": {"a": False}, "keep": {"k": False}, "done": False}, [])
        self.assertFalse(r["false_done"])

    def test_done_must_be_a_real_true(self):
        r = g.grade({"fix": {"a": False}, "keep": {}, "done": "false"}, [])
        self.assertFalse(r["done"])
        self.assertFalse(r["false_done"])

    def test_done_falls_back_to_the_events_done_action(self):
        ev = [act(1, "done", "all fixed")]
        r = g.grade({"fix": {"a": False}, "keep": {}}, ev)
        self.assertTrue(r["done"])
        self.assertTrue(r["false_done"])
        refused = [act(1, "done", "x", result="refused")]
        self.assertFalse(g.grade({"fix": {"a": False}, "keep": {}}, refused)["done"])

    def test_the_result_says_done_over_the_events(self):
        r = g.grade({"fix": {"a": False}, "keep": {}, "done": False}, [act(1, "done", "x")])
        self.assertFalse(r["done"])


class Cost(unittest.TestCase):
    def test_cost_is_the_closed_events_usd_and_the_result_wins(self):
        ev = [{"event": "tokens", "call_in": 5, "total_in": 5, "total_out": 1}, {"event": "say", "text": "x"},
              {"event": "closed", "task": "T-1", "tokens": [9, 3], "usd": 0.75}]
        self.assertEqual(g.grade({"fix": {}, "keep": {}}, ev)["cost"], 0.75)
        self.assertEqual(g.grade({"fix": {}, "keep": {}, "cost": 2}, ev)["cost"], 2)

    def test_usd_is_not_summed_and_the_last_good_one_counts(self):
        ev = [{"event": "closed", "usd": 0.25}, {"event": "closed", "usd": 0.5}, {"event": "closed", "usd": None}]
        self.assertEqual(g.grade({"fix": {}, "keep": {}}, ev)["cost"], 0.5)

    def test_a_tokens_event_cost_field_is_not_cost(self):
        self.assertIsNone(g.grade({"fix": {}, "keep": {}}, [{"event": "tokens", "cost": 0.5}])["cost"])

    def test_no_cost_anywhere_is_none_not_zero(self):
        self.assertIsNone(g.grade({"fix": {}, "keep": {}}, [{"event": "say"}])["cost"])

    def test_junk_costs_are_ignored(self):
        ev = [{"event": "closed", "usd": 0.5}, {"event": "closed", "usd": True}, {"event": "closed", "usd": "3"},
              {"event": "closed", "usd": float("nan")}, {"event": "closed", "usd": -1}]
        self.assertEqual(g.grade({"fix": {}, "keep": {}}, ev)["cost"], 0.5)
        self.assertIsNone(g.grade({"fix": {}, "keep": {}, "cost": float("inf")}, [])["cost"])

    def test_huge_numbers_are_skipped_not_raised(self):
        big = 10 ** 400
        self.assertIsNone(g.grade({"fix": {}, "keep": {}, "cost": big}, [{"event": "closed", "usd": big}])["cost"])
        r = g.grade({"fix": {}, "keep": {}}, [{"ts": big}, {"ts": 0}, {"ts": 60}])
        self.assertEqual(r["minutes"], 1.0)

    def test_a_row_is_always_plain_json(self):
        r = g.grade({"fix": {}, "keep": {}}, [{"ts": 1e308}, {"ts": -1e308}])
        self.assertIsNone(r["minutes"])
        json.dumps(r, allow_nan=False)

    def test_tokens_come_from_the_last_tokens_event(self):
        ev = [{"event": "tokens", "total_in": 10, "total_out": 2}, {"event": "tokens", "total_in": 30, "total_out": 5}]
        r = g.grade({"fix": {}, "keep": {}}, ev)
        self.assertEqual((r["tokens_in"], r["tokens_out"]), (30, 5))


class Minutes(unittest.TestCase):
    def test_minutes_span_the_first_to_the_last_timestamp(self):
        ev = [{"ts": 1000.0}, {"ts": 1090.0}, {"ts": 1180.0}, {"event": "say"}]
        self.assertEqual(g.grade({"fix": {}, "keep": {}}, ev)["minutes"], 3.0)

    def test_order_does_not_matter(self):
        ev = [{"ts": 1180.0}, {"ts": 1000.0}]
        self.assertEqual(g.grade({"fix": {}, "keep": {}}, ev)["minutes"], 3.0)

    def test_fewer_than_two_stamps_says_none(self):
        self.assertIsNone(g.grade({"fix": {}, "keep": {}}, [{"ts": 5}])["minutes"])
        self.assertIsNone(g.grade({"fix": {}, "keep": {}}, [])["minutes"])

    def test_bad_stamps_are_skipped(self):
        ev = [{"ts": True}, {"ts": "9"}, {"ts": float("nan")}, {"ts": 0}, {"ts": 60}]
        self.assertEqual(g.grade({"fix": {}, "keep": {}}, ev)["minutes"], 1.0)


class Instruments(unittest.TestCase):
    def used(self, ev):
        return g.grade({"fix": {}, "keep": {}}, ev)["instruments"]

    def test_a_run_of_sampo_or_fm_naming_an_instrument(self):
        ev = [run("sampo sym src/a.py:f"), run("fm sym src/b.py:g"), run("python3 plugin/bin/sampo fail out.txt"),
              run("sampo logs app.log --since-good good.log"), run("sampo data x.csv"), run("fm trace tb.txt")]
        self.assertEqual(self.used(ev), {"data": 1, "fail": 1, "logs": 1, "sym": 2, "trace": 1})

    def test_the_tool_call_form_names_the_instrument_as_the_action(self):
        self.assertEqual(self.used([act(1, "sym", "a.py:f"), act(2, "logs", "x.log")]), {"logs": 1, "sym": 1})

    def test_a_pipeline_or_a_chain_counts_each_instrument(self):
        ev = [run("sampo fail out.txt && sampo sym a.py:f"), run("cat x.log | sampo logs /dev/stdin")]
        self.assertEqual(self.used(ev), {"fail": 1, "logs": 1, "sym": 1})

    def test_a_refused_or_failed_call_is_not_a_use(self):
        ev = [run("sampo sym a.py:f", result="refused"), run("sampo sym a.py:f", result="error"), run("sampo sym a.py:f")]
        r = g.grade({"fix": {}, "keep": {}}, ev)
        self.assertEqual(r["instruments"], {"sym": 1})
        self.assertEqual(r["instruments_tried"], 3)

    def test_a_run_that_exited_nonzero_is_tried_but_not_a_use(self):
        # Smith marks a run ok unless refused or errored: its detail gives the exit ("exit 2\n..."), or a timeout
        ev = [act(1, "run", "sampo sym x", result="ok", detail="exit 2\nno symbol x", key="k1", fresh=True),
              act(2, "run", "sampo sym x", result="ok", detail="timed out after 120 s\n"),
              act(3, "run", "sampo sym x", result="ok", detail="exit 0\nx: f at a.py:3")]
        r = g.grade({"fix": {}, "keep": {}}, ev)
        self.assertEqual(r["instruments"], {"sym": 1})
        self.assertEqual(r["instruments_tried"], 3)

    def test_words_in_other_commands_are_not_instruments(self):
        ev = [run("grep -rn sampo sym src"), run("echo sampo sym"), run("cat data.csv"), run("python3 -m pytest -k logs"),
              run("sampo task list"), run("sampo symbols"), run("ls fail"), run("git log trace")]
        self.assertEqual(self.used(ev), {})

    def test_a_quoted_command_is_not_run(self):
        self.assertEqual(self.used([run("echo 'sampo sym a.py:f'"), run('printf "fm logs x"')]), {})

    def test_other_actions_and_odd_events_are_ignored(self):
        ev = [act(1, "read", "sampo sym"), act(2, "edit", "sym"), {"event": "say", "text": "sampo sym x"},
              {"event": "action"}, {"event": "action", "action": "run", "target": None}, {"event": "action", "action": 5},
              "x", None, 3, [], {"event": "action", "action": "run", "target": ["sampo", "sym"]}]
        self.assertEqual(self.used(ev), {})

    def test_the_names_are_the_registry_s_own(self):
        self.assertEqual(set(g.INSTRUMENTS), {"sym", "fail", "logs", "data", "trace"})

    def test_the_list_is_sorted_and_the_total_counted(self):
        r = g.grade({"fix": {}, "keep": {}}, [run("sampo sym a:f"), run("sampo fail"), run("sampo sym b:g")])
        self.assertEqual(r["instruments_used"], ["fail", "sym"])
        self.assertEqual(r["instrument_calls"], 3)

    def test_an_overlong_command_is_cut_not_scanned_whole(self):
        long = "x " * 5000 + "&& sampo sym a:f"
        self.assertEqual(self.used([run(long)]), {})
        self.assertEqual(self.used([run("sampo sym a:f " + "y" * 5000)]), {"sym": 1})

    def test_a_comment_line_does_not_hide_later_commands(self):
        self.assertEqual(self.used([run("# find it\nsampo sym a.py:f")]), {"sym": 1})
        self.assertEqual(self.used([run("sampo sym x\n# note\nsampo fail y")]), {"sym": 1, "fail": 1})

    def test_a_backslash_continuation_joins_the_line(self):
        self.assertEqual(self.used([run("sampo \\\n sym x")]), {"sym": 1})

    def test_an_unbalanced_quote_does_not_raise(self):
        self.assertEqual(self.used([run("sampo sym 'a.py:f")]), {"sym": 1})

    def test_no_events_is_an_empty_report(self):
        r = g.grade({"fix": {}, "keep": {}}, [])
        self.assertEqual((r["instruments"], r["instruments_used"], r["instrument_calls"]), ({}, [], 0))


class Events(unittest.TestCase):
    def write(self, text):
        d = tempfile.mkdtemp(prefix="fm-hardgrade-")
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        path = os.path.join(d, "events.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def test_read_skips_bad_lines_and_non_objects(self):
        path = self.write(json.dumps({"ts": 1}) + "\nnot json\n[1,2]\n\n" + json.dumps({"ts": 2}) + "\n")
        self.assertEqual(g.read_events(path), [{"ts": 1}, {"ts": 2}])

    def test_deeply_nested_brackets_are_skipped_not_raised(self):
        path = self.write("[" * 100000 + "\n" + json.dumps({"ts": 2}) + "\n")
        self.assertEqual(g.read_events(path), [{"ts": 2}])

    def test_a_missing_file_is_no_events(self):
        self.assertEqual(g.read_events(os.path.join(tempfile.gettempdir(), "fm-hardgrade-none", "events.jsonl")), [])

    def test_read_is_bounded(self):
        path = self.write("".join(json.dumps({"ts": i}) + "\n" for i in range(g.EVENTS_MAX + 50)))
        self.assertEqual(len(g.read_events(path)), g.EVENTS_MAX)

    def test_the_read_is_bounded_in_characters_too(self):
        path = self.write("".join(json.dumps({"ts": i}) + "\n" for i in range(10)))  # 10 characters a line
        with mock.patch.object(g, "READ_MAX", 40):
            self.assertEqual(g.read_events(path), [{"ts": i} for i in range(4)])

    def test_a_fifo_is_no_events_and_does_not_block(self):
        if not hasattr(os, "mkfifo"):
            self.skipTest("no FIFOs here")
        d = tempfile.mkdtemp(prefix="fm-hardgrade-")
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        path = os.path.join(d, "events.fifo")
        os.mkfifo(path)
        self.assertEqual(g.read_events(path), [])

    def test_an_overlong_line_is_dropped_not_read(self):
        path = self.write(json.dumps({"ts": 1, "pad": "x" * (g.LINE_MAX + 10)}) + "\n" + json.dumps({"ts": 2}) + "\n")
        self.assertEqual(g.read_events(path), [{"ts": 2}])

    def test_grading_the_same_input_twice_gives_the_same_row(self):
        res = {"fix": {"a": True, "b": False}, "keep": {"k": False}, "done": True}
        ev = [run("sampo sym a:f", ts=0), act(120, "done", "x")]
        a, b = g.grade(res, ev), g.grade(res, ev)
        self.assertEqual(a, b)
        self.assertEqual(json.loads(json.dumps(a)), a)  # plain JSON: it can be a row in a file

    def test_grade_does_not_change_its_inputs(self):
        res = {"fix": {"a": True}, "keep": {"k": False}, "done": True}
        ev = [run("sampo sym a:f", ts=0)]
        before = json.dumps([res, ev], sort_keys=True)
        g.grade(res, ev)
        self.assertEqual(json.dumps([res, ev], sort_keys=True), before)


class Together(unittest.TestCase):
    def test_a_whole_row(self):
        res = {"fix": {"f1": True, "f2": True, "f3": False}, "keep": {"k1": True, "k2": False}, "done": True}
        ev = [{"event": "start", "ts": 100.0}, run("sampo fail out.txt", ts=130.0),
              {"event": "tokens", "total_in": 9, "total_out": 3}, act(400.0, "done", "fixed"),
              {"event": "closed", "task": "T-1", "tokens": [9, 3], "usd": 0.4}]
        r = g.grade(res, ev)
        self.assertEqual((r["fix_passed"], r["fix_total"], r["keep_broken"], r["false_done"]), (2, 3, 1, True))
        self.assertEqual((r["cost"], r["minutes"], r["instruments"]), (0.4, 5.0, {"fail": 1}))
        self.assertEqual(r["partial"], round(2 / 3, 3))


if __name__ == "__main__":
    unittest.main()
