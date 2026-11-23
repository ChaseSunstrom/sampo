"""T-5030: every Rust decision (keep, port, rejected) is recorded in plugin/native/DECISIONS.json with the python and
native milliseconds it rests on; a row missing a number, or one whose verdict the numbers contradict, fails here."""
import copy
import json
import math
import os
import re
import unittest

from helpers import PLUGIN

LEDGER = os.path.join(PLUGIN, "native", "DECISIONS.json")
DECISIONS = ("keep", "port", "rejected")
BASES = ("measured", "estimate")
UNITS = ("cpu", "wall", "compile")
NEED = ("hot_path", "python_ms", "native_ms", "unit", "decision", "basis", "task", "source")


def problems(doc):
    """Every reason the ledger is not trustworthy, one string each; empty when it is."""
    if not isinstance(doc, dict) or not isinstance(doc.get("decisions"), list) or not doc["decisions"]:
        return ["no decisions list"]
    out, seen = [], set()
    for i, row in enumerate(doc["decisions"]):
        if not isinstance(row, dict):
            out.append(f"row {i}: not an object")
            continue
        name = row.get("hot_path") if isinstance(row.get("hot_path"), str) and row.get("hot_path").strip() else f"row {i}"
        for key in NEED:
            if row.get(key) in (None, ""):
                out.append(f"{name}: missing {key}")
        for key in ("python_ms", "native_ms"):
            v = row.get(key)
            if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not v > 0):
                out.append(f"{name}: {key} must be a finite number above 0, got {v!r}")
        ident = " ".join(name.split()).casefold()
        if ident in seen:
            out.append(f"{name}: listed twice")
        seen.add(ident)
        if row.get("decision") not in DECISIONS and row.get("decision") not in (None, ""):
            out.append(f"{name}: decision {row['decision']!r} is not one of {DECISIONS}")
        if row.get("unit") not in UNITS and row.get("unit") not in (None, ""):
            out.append(f"{name}: unit {row['unit']!r} is not one of {UNITS}")
        if row.get("basis") not in BASES and row.get("basis") not in (None, ""):
            out.append(f"{name}: basis {row['basis']!r} is not one of {BASES}")
        if isinstance(row.get("task"), str) and not re.fullmatch(r"T-\d+", row["task"]):
            out.append(f"{name}: task {row['task']!r} is not a task id")
        py, nat, dec = row.get("python_ms"), row.get("native_ms"), row.get("decision")
        if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (py, nat)):
            if dec in ("keep", "port") and not nat < py:
                out.append(f"{name}: {dec} but native {nat} ms is not under python {py} ms")
            if dec == "rejected" and not nat >= py:
                out.append(f"{name}: rejected but native {nat} ms beats python {py} ms")
        if dec in ("keep", "rejected") and row.get("basis") == "estimate":
            out.append(f"{name}: a {dec} path needs a measured number, not an estimate")
    return out


class Ledger(unittest.TestCase):
    def setUp(self):
        with open(LEDGER, encoding="utf-8") as f:
            self.doc = json.load(f)

    def test_the_ledger_is_clean(self):
        self.assertEqual(problems(self.doc), [])

    def test_it_records_a_kept_path_and_a_rejected_one(self):
        decisions = {r["decision"] for r in self.doc["decisions"]}
        self.assertIn("keep", decisions, "sampo-fast is kept: its row is the seed")
        self.assertIn("rejected", decisions, "the regex-at-start port was measured and turned down")

    def test_a_row_missing_a_number_fails(self):
        for key in ("python_ms", "native_ms"):
            for bad in (None, "", "fast", 0, -3, True):
                doc = copy.deepcopy(self.doc)
                if bad is None:
                    del doc["decisions"][0][key]
                else:
                    doc["decisions"][0][key] = bad
                self.assertTrue(problems(doc), f"{key}={bad!r} passed")

    def test_a_row_missing_any_other_field_fails(self):
        for key in NEED:
            doc = copy.deepcopy(self.doc)
            del doc["decisions"][0][key]
            self.assertTrue(problems(doc), f"no {key} passed")

    def test_a_verdict_the_numbers_contradict_fails(self):
        doc = copy.deepcopy(self.doc)
        row = next(r for r in doc["decisions"] if r["decision"] == "keep")
        row["native_ms"] = row["python_ms"] * 2
        self.assertTrue(problems(doc), "keep with a slower native passed")
        doc = copy.deepcopy(self.doc)
        row = next(r for r in doc["decisions"] if r["decision"] == "rejected")
        row["native_ms"] = row["python_ms"] / 2
        self.assertTrue(problems(doc), "rejected with a faster native passed")
        doc = copy.deepcopy(self.doc)
        next(r for r in doc["decisions"] if r["decision"] == "keep")["basis"] = "estimate"
        self.assertTrue(problems(doc), "keep on an estimate passed")

    def test_junk_documents_fail(self):
        for doc in (None, [], {}, {"decisions": []}, {"decisions": ["x"]}, {"decisions": [{}]}):
            self.assertTrue(problems(doc), f"{doc!r} passed")

    def test_a_hot_path_listed_twice_fails(self):
        doc = copy.deepcopy(self.doc)
        doc["decisions"].append(copy.deepcopy(doc["decisions"][0]))
        self.assertTrue(problems(doc))

    def test_a_hot_path_listed_twice_in_other_case_or_spacing_fails(self):
        doc = copy.deepcopy(self.doc)
        twin = copy.deepcopy(doc["decisions"][0])
        twin["hot_path"] = "  " + twin["hot_path"].upper().replace(" ", "   ") + "  "
        doc["decisions"].append(twin)
        self.assertTrue(problems(doc), "a case and spacing variant passed as a new hot path")

    def test_an_infinite_number_fails(self):
        for dec, key in (("keep", "python_ms"), ("rejected", "native_ms")):
            doc = copy.deepcopy(self.doc)
            next(r for r in doc["decisions"] if r["decision"] == dec)[key] = float("inf")
            self.assertTrue(problems(doc), f"{dec} with {key}=Infinity passed")

    def test_a_rejection_that_rests_on_an_estimate_fails(self):
        doc = copy.deepcopy(self.doc)
        next(r for r in doc["decisions"] if r["decision"] == "rejected")["basis"] = "estimate"
        self.assertTrue(problems(doc), "rejected on an estimate passed")

    def test_every_kept_path_has_a_binary_to_keep(self):
        # a "keep" row for a crate that is gone would be a number with nothing behind it
        if any(r["decision"] == "keep" for r in self.doc["decisions"]):
            self.assertTrue(os.path.isfile(os.path.join(PLUGIN, "native", "crates", "sampo-fast", "src", "main.rs")))


class AgentCalls(unittest.TestCase):
    """T-5837: hooks._spawn_rule fills a missing model and refuses a lookup sent to a general agent, in output sampo-fast
    cannot make, so agent_ok hands Python every Agent or Task call the rule could touch. The behaviour is run against
    the built binary in test_fast_equiv (Agents); this reads the source, so it holds on a machine with no cargo."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(PLUGIN, "native", "crates", "sampo-fast", "src", "main.rs"), encoding="utf-8") as f:
            src = f.read()
        start = src.index("fn agent_ok(")
        cls.body = src[start:src.index("\n}\n", start)]

    def test_agent_ok_defers_a_call_with_no_model(self):
        self.assertRegex(self.body, r'ti\.get\("model"\)')
        self.assertRegex(self.body, r'(?s)ti\.get\("model"\).{0,120}model\.is_empty\(\).{0,40}return None')

    def test_agent_ok_defers_a_sonnet_or_opus_call_of_any_type(self):  # T-6101: the spawn rule rewrites those to haiku
        self.assertRegex(self.body, r'(?s)model\.contains\("sonnet"\) \|\| model\.contains\("opus"\) \{\s*return None')
        self.assertLess(self.body.index('contains("sonnet")'), self.body.index('"general-purpose"'))

    def test_agent_ok_defers_a_call_with_no_type_or_a_general_purpose_one(self):
        # a missing type, a blank one (T-5497 trims it) and a general-purpose one (any case) all return None
        self.assertRegex(self.body, r'(?s)ti\.get\("subagent_type"\) else \{\s*return None;?\s*\}')
        self.assertRegex(self.body, r'(?s)t\.is_empty\(\) \|\| t\.to_lowercase\(\)\.contains\("general-purpose"\).{0,40}return None')

    def test_agent_ok_asks_before_it_reads_any_state(self):
        # the deferrals come before the budget reads: a call Python must decide never costs a snapshot read first
        self.assertLess(self.body.index('"general-purpose"'), self.body.index("rate_limits(state)"))
        self.assertLess(self.body.index('"model"'), self.body.index("budget.json"))

    def test_agent_ok_still_defers_a_fixer_or_resolver(self):
        self.assertIn('"fm-fixer"', self.body)
        self.assertIn('"fm-resolver"', self.body)

    def test_agent_calls_the_python_rule_logs_are_the_ones_the_native_path_leaves_to_it(self):
        # hooks._spawn_rule skips its row for a typed call with a model: the very calls agent_ok answers itself
        from sampo import hooks
        import inspect
        src = inspect.getsource(hooks._spawn_rule)
        self.assertIn('"general-purpose" not in kind', src)
        self.assertRegex(src, r"isinstance\(model, str\) and model")


if __name__ == "__main__":
    unittest.main()
