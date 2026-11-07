"""T-2931: the smith arm asks the local profile once for findings from fm code's end text. Fakes only; the end text is
gemma4:12b's real vuln-js answer, the untested-rust file and symbol are the real ones."""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from helpers import ForemanTestCase  # noqa: F401  (puts plugin/lib on the path)

from sampo import louhi, models, smithbench

END = "SQL Injection: File app.js, Line 23, the find_user_sql function concatenates name"
FAKE_FM = "import json,sys\nfor e in json.loads(%r): print(json.dumps(e))\n"


SQL_JS = "function find_user_sql(name) {\n  return \"SELECT * FROM users WHERE name = '\" + name + \"'\";\n}\n"
UNIQ_TS = "export function uniq_keep_order(xs: number[]): number[] {\n  const out: number[] = [];\n  return out;\n}\n"
# T-3676: s28-027/slow-ts-5b5140 (jarvis-nothink, Nov 7): this step summary named the keyed defect, but the findings
# call saw only the close line, so it was missed
SAID = ('{"action": "done", "summary": "Recorded per-helper performance verdicts in T-0001-findings.md. Only offender: '
        'uniq_keep_order (app.ts:15) uses out.includes(x) per element -> O(n^2), hidden by tiny test inputs."}')
CLOSED = "T-0001 closed; committed 5260903 (3 files). Undo: git revert --no-edit 5260903 (tokens: 22274 in, 1796 out)"


def closed_run(verify_exit=0):
    return [{"event": "say", "text": SAID},
            {"event": "action", "action": "done", "target": SAID[30:230], "result": "ok"},
            {"event": "action", "action": "edit", "target": "app.ts", "result": "ok"},
            {"event": "action", "action": "write", "target": "notes.md", "result": "refused"},
            {"event": "verify", "label": "verify", "exit": verify_exit},
            {"event": "closed", "text": CLOSED}]


class SmithFindings(unittest.TestCase):
    def arm(self, events, reply, files=("app.js",)):
        calls = []
        def complete(profile, messages, **kw):
            calls.append(messages)
            return {"text": reply, "tool_calls": [], "usage": {"in": 1, "out": 1}}
        files = files if isinstance(files, dict) else dict.fromkeys(files, "x = 1\n")
        with tempfile.TemporaryDirectory() as tmp:
            base, work = os.path.join(tmp, "base"), os.path.join(tmp, "work")
            os.makedirs(os.path.join(base, "repo"))
            for f, src in files.items():
                with open(os.path.join(base, "repo", f), "w") as h:
                    h.write(src)
            fm = os.path.join(tmp, "fm.py")
            with open(fm, "w") as h:
                h.write(FAKE_FM % json.dumps(events))
            log = os.path.join(tmp, "e.jsonl")
            with mock.patch.object(smithbench, "FM", fm), mock.patch.object(models, "resolve", lambda m: {"name": m}), \
                    mock.patch.object(models, "complete", complete):
                ans = louhi._smith_arm(base, {"request": "audit", "kind": "vuln"}, work, "fake", None, log)
            with open(log) as h:
                logged = h.read()
        return ans, calls, logged

    def test_the_end_text_becomes_findings_the_grade_can_credit(self):
        reply = 'Here: {"findings": [{"file": "app.js", "symbol": "find_user_sql", "says": "SQL injection"}]}'
        ans, calls, logged = self.arm([{"event": "answer", "text": END}], reply, files={"app.js": SQL_JS})
        self.assertEqual(len(calls), 1)
        self.assertIn(END, json.dumps(calls[0]))
        self.assertIn("app.js", json.dumps(calls[0]), "the file list goes with the text")
        self.assertEqual(ans["findings"], [{"file": "app.js", "symbol": "find_user_sql", "says": "SQL injection"}])
        self.assertIn("findings", logged, "the call is in the events log")
        key = {"defects": [{"files": ["app.js"], "symbols": ["find_user_sql"]}]}
        self.assertEqual(louhi._matches(ans["findings"][0], key["defects"][0]), True)

    def test_a_file_not_in_the_repo_is_dropped(self):
        reply = json.dumps({"findings": [{"file": "ghost.js", "symbol": "clamp", "says": "x"},
                                         {"file": "app.rs", "symbol": "clamp", "says": "uncovered"}]})
        ans, _, _ = self.arm([{"event": "answer", "text": "clamp in app.rs is uncovered"}], reply,
                             files={"app.rs": "pub fn clamp(x: i64) -> i64 { x }\n"})
        self.assertEqual([f["file"] for f in ans["findings"]], ["app.rs"])

    def test_a_closed_run_is_read_from_smiths_own_step_words(self):
        reply = json.dumps({"findings": [{"file": "app.ts", "symbol": "uniq_keep_order()", "says": "O(n^2)"}]})
        ans, calls, _ = self.arm(closed_run(), reply, files={"app.ts": UNIQ_TS})
        ask = calls[0][-1]["content"]
        self.assertIn("Only offender: uniq_keep_order (app.ts:15)", ask, "the step's own summary, whole, not the close only")
        self.assertIn(CLOSED, ask)
        self.assertIn("Files it changed: app.ts\n", ask, "an edit that went through, not a refused write")
        self.assertEqual([f["symbol"] for f in ans["findings"]], ["uniq_keep_order()"])

    def test_an_answer_run_sends_its_words_once(self):
        # T-3688: the done step's say JSON and the answer event carry the same summary; twice used half the budget
        said = "uniq_keep_order (app.ts:15) is O(n^2): out.includes(x) per element"
        evs = [{"event": "say", "text": json.dumps({"action": "done", "summary": said})},
               {"event": "action", "action": "done", "target": said[:40], "result": "ok"},
               {"event": "answer", "text": said}]
        _, calls, _ = self.arm(evs, json.dumps({"findings": []}), files={"app.ts": UNIQ_TS})
        ask = calls[0][-1]["content"]
        self.assertEqual(ask.count("out.includes(x)"), 1, ask)
        self.assertNotIn('"action"', ask, "the summary, not the JSON around it")

    def test_the_hidden_check_never_enters_smiths_tree_or_argv(self):
        # T-3688: what the grade checks (base/hidden, the key) is never where Smith runs, so no retry can quote it
        script = ("import json,os,sys\nseen=sys.argv+list(os.environ.values())\nfor d,ds,fs in os.walk('..'):\n"
                  "    ds[:]=[x for x in ds if x!='.git']\n    for f in fs:\n        p=os.path.join(d,f)\n"
                  "        try:\n            seen.append(p+':'+open(p,errors='replace').read())\n"
                  "        except OSError:\n            pass\n"
                  "print(json.dumps({'event':'answer','text':' '.join(seen)}))\n")
        with tempfile.TemporaryDirectory() as tmp:
            base, work = os.path.join(tmp, "base"), os.path.join(tmp, "work")
            for d, f, src in (("repo", "app.js", SQL_JS), ("hidden", "check.sh", "HIDDEN_SENTINEL\n")):
                os.makedirs(os.path.join(base, d))
                with open(os.path.join(base, d, f), "w") as h:
                    h.write(src)
            fm = os.path.join(tmp, "fm.py")
            with open(fm, "w") as h:
                h.write(script)
            with mock.patch.object(smithbench, "FM", fm), mock.patch.object(models, "resolve", lambda m: {"name": m}), \
                    mock.patch.object(models, "complete", lambda *a, **k: {"text": "{}", "tool_calls": [], "usage": {}}):
                louhi._smith_arm(base, {"request": "audit", "kind": "vuln", "defects": [{"says": "HIDDEN_KEY"}]},
                                 work, "fake", None, os.path.join(tmp, "e.jsonl"))
            with open(os.path.join(tmp, "e.jsonl")) as h:
                saw = "\n".join(json.loads(ln).get("text") or "" for ln in h if '"answer"' in ln)
        self.assertIn("find_user_sql", saw, "Smith's whole run directory was walked")
        self.assertNotIn("HIDDEN_SENTINEL", saw)
        self.assertNotIn("HIDDEN_KEY", saw)
        self.assertNotIn(base, saw)

    def test_a_finding_whose_name_is_not_in_its_file_is_dropped_and_logged(self):
        # T-3676: the Nov 7 false alarms' biggest grounded class: a name the file never holds (unknown, fix:1, step_4,
        # paging, T-0001, verify_command), or the same finding twice
        bad = [("app.ts", "unknown"), ("app.ts", "fix:1"), ("app.ts", "T-0001"), ("app.ts", "paging"),
               ("test_app.ts", "verify_command"), ("app.ts", "")]
        reply = json.dumps({"findings": [{"file": f, "symbol": s, "says": "x"} for f, s in bad]
                           + [{"file": "app.ts", "symbol": "uniq_keep_order", "says": "O(n^2)"}] * 2})
        ans, _, logged = self.arm(closed_run(), reply, files={"app.ts": UNIQ_TS, "test_app.ts": "test_uniq();\n"})
        self.assertEqual([(f["file"], f["symbol"]) for f in ans["findings"]], [("app.ts", "uniq_keep_order")])
        self.assertIn("not in its file", logged)
        self.assertIn("paging", logged, "each dropped finding is named in the events log")

    def test_a_name_the_fix_removed_is_still_grounded_in_the_scenario(self):
        fm_rm = [{"event": "say", "text": '{"action": "done", "summary": "Removed the unsafe find_user_sql in app.js"}'},
                 {"event": "action", "action": "done", "target": "Removed the unsafe find_user_sql", "result": "ok"},
                 {"event": "closed", "text": CLOSED}]
        reply = json.dumps({"findings": [{"file": "app.js", "symbol": "find_user_sql", "says": "SQL injection"}]})
        with mock.patch.object(louhi.shutil, "copytree", side_effect=self.copy_emptied):
            ans, _, _ = self.arm(fm_rm, reply, files={"app.js": SQL_JS})
        self.assertEqual([f["symbol"] for f in ans["findings"]], ["find_user_sql"])

    def test_a_doc_that_misnames_the_code_is_grounded_in_the_code(self):
        # T-3676 replay: s30-037/docs-rust-73139b's credited README.md::paginate (the README calls it page())
        reply = json.dumps({"findings": [{"file": "README.md", "symbol": "paginate", "says": "README calls it page()"},
                                         {"file": "README.md", "symbol": "T-0001", "says": "closed"}]})
        ans, _, _ = self.arm([{"event": "answer", "text": "README.md names paginate as page"}], reply,
                             files={"README.md": "- page(xs, n, size): page n from 1\n",
                                    "app.rs": "pub fn paginate(xs: &[i64], n: i64, size: i64) {}\n"})
        self.assertEqual([f["symbol"] for f in ans["findings"]], ["paginate"])

    def test_an_untested_function_named_on_its_test_file_is_grounded_in_the_code(self):
        # T-3676 live replay: s28-027/untested-c-2ef72d's right answer, test_app.c::limit_to; an untested function is
        # absent from its test file by definition
        reply = json.dumps({"findings": [{"file": "test_app.c", "symbol": "limit_to", "says": "no test exists"},
                                         {"file": "test_app.c", "symbol": "ghost_fn", "says": "x"}]})
        ans, _, _ = self.arm([{"event": "answer", "text": "limit_to has no test"}], reply,
                             files={"test_app.c": "static void test_total(void) {}\n",
                                    "app.c": "long limit_to(long x, long lo, long hi) { return x; }\n"})
        self.assertEqual([f["symbol"] for f in ans["findings"]], ["limit_to"])

    def test_only_the_most_serious_finding_is_kept(self):
        # T-3676 live replay, 352 Nov 7 answers: past the first finding, 11 found against 61 false alarms (vuln-c-5e1223:
        # lookup_sql first, then slice_page, dedupe, sum_prices, right_align, all real names in app.c)
        reply = json.dumps({"findings": [{"file": "app.js", "symbol": "find_user_sql", "says": "SQL injection"},
                                         {"file": "app.js", "symbol": "page_of", "says": "may overflow"}]})
        ans, _, logged = self.arm([{"event": "answer", "text": END}], reply,
                                  files={"app.js": SQL_JS + "function page_of(xs, n) { return xs; }\n"})
        self.assertEqual([f["symbol"] for f in ans["findings"]], ["find_user_sql"])
        self.assertIn("page_of: past the first", logged)

    def test_a_finding_outside_the_tree_is_never_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "repo"))
            with open(os.path.join(tmp, "secret.txt"), "w") as h:
                h.write("hunter_two\n")
            os.mkfifo(os.path.join(tmp, "repo", "pipe.md"))
            self.assertFalse(louhi._grounded({"file": "../secret.txt", "symbol": "hunter_two"}, [os.path.join(tmp, "repo")]))
            self.assertFalse(louhi._grounded({"file": "pipe.md", "symbol": "hunter_two"}, [os.path.join(tmp, "repo")]),
                             "a fifo is skipped, not opened (it would block)")

    @staticmethod
    def copy_emptied(src, dst, copy=shutil.copytree, **kw):  # the run's tree, with the function deleted from app.js
        copy(src, dst, **kw)
        if os.path.basename(dst) == "work":
            with open(os.path.join(dst, "app.js"), "w") as h:
                h.write("// gone\n")

    def test_the_answer_states_a_confidence_from_the_runs_own_evidence(self):
        reply = json.dumps({"findings": []})
        green, _, _ = self.arm(closed_run(0), reply, files={"app.ts": UNIQ_TS})
        red, _, _ = self.arm(closed_run(1), reply, files={"app.ts": UNIQ_TS})
        for ans in (green, red):
            self.assertIsNotNone(louhi._conf(ans), "a stated confidence, not one the grade scores as 50")
            self.assertLess(ans["confidence"], 50, "Nov 7: no smith run closed its scenario, green or not")
        self.assertGreaterEqual(green["confidence"], red["confidence"])

    def test_no_end_text_makes_no_call(self):
        ans, calls, _ = self.arm([{"event": "error", "text": ""}], "{}")
        self.assertEqual((calls, ans["findings"]), ([], []))

    def test_a_failing_model_leaves_findings_empty(self):
        ans, calls, _ = self.arm([{"event": "answer", "text": END}], "no json at all")
        self.assertEqual((len(calls), ans["findings"]), (1, []))


if __name__ == "__main__":
    unittest.main()
