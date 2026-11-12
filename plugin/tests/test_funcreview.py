"""T-4388: before fm code plans a request for the code as a whole made right, on a free model, the harness reviews each
function alone (one thinking-off call: the function, its comment, the README, the test lines that name it), quote-checks
each claim, looks a second time at each one left, and hands the survivors, the untested functions and the README's
undefined names to the planner as fenced data. Models advise, proof decides: nothing here gates anything."""
import json
import os
import tempfile
import types
import unittest

from test_code import CLOSE, CodeCase, call, journal_text, plan
from test_smith_small import DONE, FIX_EDIT

from sampo import funcreview, smith
from sampo import models as m

README = """# pager

`page(items, size)` splits items into pages of size items; the last page may be shorter.
`slug(text)` lowercases text and joins its words with a dash.
`total(rows)` adds the amounts of the rows.
`export_csv(rows)` writes the rows as CSV.
"""
PAGER = '''def page(items, size):
    # the last page may be shorter
    return [items[i:i + size] for i in range(0, len(items) - size + 1, size)]


def slug(text):
    return "-".join(text.lower().split())


def total(rows):
    return sum(r["amount"] for r in rows)
'''
TEST_PAGER = "import pager\nassert pager.slug('A B') == 'a-b'\nprint('ok')\n"
SEED = ("seed review", CodeCase.repo_seed[1] + (("README.md", README), ("pager.py", PAGER),
                                                ("test_pager.py", TEST_PAGER)))
WHOLE = "Get these helpers to a finished state I could release; find what is wrong and fix it."
PROMISE = "the last page may be shorter"


def judge(wants=True, whole=True, find_fix=True):
    """The one judgment's reply (T-4388: its find_fix is the request's kind, a model's judgment; find_fix=None leaves the
    key out, as an older reply did)."""
    v = {"wants_change": wants, "whole": whole, "find_fix": find_fix, "why": "it asks for a release"}
    return {"text": json.dumps({k: x for k, x in v.items() if x is not None})}


def claim(promise=PROMISE, call_="page([1, 2, 3], 2)", expected="[[1, 2], [3]]", actual="[[1, 2]]",
          why="the last page is lost"):
    return {"promise": promise, "call": call_, "expected": expected, "actual": actual, "why": why}


def defects(*xs):
    return {"text": json.dumps({"defects": list(xs)})}


CLEAN = defects()
HOLDS = {"text": json.dumps({"trace": "range(0, 2, 2) is [0]", "returns": "[[1, 2]]", "holds": True})}
NOT_SO = {"text": json.dumps({"trace": "it keeps the rest", "returns": "[[1, 2], [3]]", "holds": False})}


def reviews(f):
    return [i for i, s in enumerate(f.seen) if funcreview.REVIEW_PROMPT[:40] in json.dumps(s["messages"][0])]


def looks(f):
    return [i for i, s in enumerate(f.seen) if funcreview.CHECK_PROMPT[:40] in json.dumps(s["messages"][0])]


def judged(f):
    return [i for i, s in enumerate(f.seen) if "wants_change" in json.dumps(s["messages"][0])]


# T-4425: the project holds a README, so a change request's plan is preceded by the survey (the judgment, the survey's
# turns: it opens the one document, then reports); T-4388's review comes after it, so the scripts carry it
SURVEYED = (call("read", path="README.md"), call("done", summary="DOCS: README.md says page(items, size)\nCODE: pager.py\n"
                                                                  "TESTS: none"))
AT = 1 + len(SURVEYED)  # the first call after the judgment and the survey


def surveyed(script):
    """The script with the survey's turns after the judgment (the first reply), when it is one."""
    if script and "wants_change" in json.dumps(script[0]):
        return (script[0], *SURVEYED, *script[1:])
    return script


class Pager(CodeCase):
    repo_seed = SEED

    def launch(self, request, *script, args=("--dry-plan",)):
        f = self.serve("openai", *surveyed(script))
        r = self.code(request, "--model", "fake", "--json", *args, check=False)
        return f, r, [json.loads(ln) for ln in r.stdout.splitlines() if ln.startswith("{")]


class Reviewed(Pager):

    def full(self, request, *script, args=()):
        return self.launch(request, *script, args=("--no-commit", *args))

    def test_per_function_each_function_gets_one_thinking_off_call(self):
        f, r, ev = self.launch(WHOLE, judge(), CLEAN, CLEAN, CLEAN, CLEAN, plan())
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(reviews(f), list(range(AT, AT + 4)), "add, page, slug, total: one call each, test files left out")
        for i, name in zip(reviews(f), ("add", "page", "slug", "total")):
            said = f.said(i)
            self.assertIn(f"function {name}:", said)
            self.assertIs(f.seen[i]["chat_template_kwargs"]["enable_thinking"], False)
            self.assertIn("the last page may be shorter", said, "the README rides every call")
        self.assertIn("pager.slug('A B')", f.said(reviews(f)[2]), "the test lines that name it")
        self.assertIn("# the last page may be shorter", f.said(reviews(f)[1]), "the comment above rides with the function")
        self.assertNotIn("File test_pager.py", json.dumps([s["messages"] for s in f.seen]))

    def test_per_function_the_note_says_how_many_of_how_many(self):
        f, r, ev = self.launch(WHOLE, judge(), CLEAN, CLEAN, CLEAN, CLEAN, plan())
        note = [e for e in ev if e["event"] == "note" and "review" in e]
        self.assertEqual(len(note), 1, r.stdout)
        self.assertIn("read 4 of 4 functions", note[0]["text"] if "text" in note[0] else json.dumps(note[0]))
        self.assertEqual((note[0]["review"]["read"], note[0]["review"]["of"]), (4, 4))

    def test_per_function_what_is_found_reaches_the_planner_fenced(self):
        f, r, ev = self.launch(WHOLE, judge(), CLEAN, defects(claim()), CLEAN, CLEAN, HOLDS, plan())
        said = f.said(len(f.seen) - 1)
        self.assertIn("page([1, 2, 3], 2)", said)
        self.assertIn("<data-", said)
        self.assertIn("export_csv", said, "a README name no code has")
        self.assertIn("fails now", said, "a criterion each, with a check that fails now: the test first")

    def test_quote_a_promise_not_in_the_readme_or_the_function_is_dropped(self):
        invented = claim(promise="page never returns an empty list", call_="page([], 2)")
        f, r, ev = self.launch(WHOLE, judge(), CLEAN, defects(invented, claim()), CLEAN, CLEAN, HOLDS, plan())
        said = f.said(len(f.seen) - 1)
        self.assertNotIn("page([], 2)", said)
        self.assertIn("page([1, 2, 3], 2)", said)
        self.assertEqual(len(looks(f)), 1, "the dropped claim is never looked at again")

    def test_quote_a_promise_in_the_function_s_own_comment_counts(self):
        mine = claim(promise="# the last page may be shorter")
        f, r, ev = self.launch(WHOLE, judge(), CLEAN, defects(mine), CLEAN, CLEAN, HOLDS, plan())
        self.assertIn("page([1, 2, 3], 2)", f.said(len(f.seen) - 1))

    def test_quote_a_claim_with_no_promise_is_kept_as_unsafe_input(self):
        unsafe = claim(promise="", call_="slug('a; rm -rf /')", expected="refuse the input", actual="passes it on")
        f, r, ev = self.launch(WHOLE, judge(), CLEAN, CLEAN, defects(unsafe), CLEAN, HOLDS, plan())
        said = f.said(len(f.seen) - 1)
        self.assertIn("slug('a; rm -rf /')", said)
        self.assertIn("unsafe input", said)

    def test_cut_reply_is_asked_once_more_with_more_room(self):
        cut = {"text": '{"defects": [{"promise": "the last', "cut": True}
        f, r, ev = self.launch(WHOLE, judge(), CLEAN, cut, defects(claim()), CLEAN, CLEAN, HOLDS, plan())
        calls = reviews(f)
        self.assertEqual(len(calls), 5, "page was asked twice")
        first, again = (f.seen[i] for i in calls[1:3])
        self.assertIn("function page:", json.dumps(again))
        self.assertGreater(again["max_tokens"], first["max_tokens"])
        self.assertIn("page([1, 2, 3], 2)", f.said(len(f.seen) - 1), "the second reply's claim is read")

    def test_cut_reply_cut_twice_is_not_asked_a_third_time(self):
        cut = {"text": '{"defects": [{"promise": "the', "cut": True}
        f, r, ev = self.launch(WHOLE, judge(), CLEAN, cut, cut, CLEAN, CLEAN, plan())
        self.assertEqual(len(reviews(f)), 5)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_second_look_each_kept_claim_goes_back_alone_with_its_function(self):
        other = claim(call_="page([1, 2, 3, 4, 5], 2)", actual="[[1, 2], [3, 4]]")
        f, r, ev = self.launch(WHOLE, judge(), CLEAN, defects(claim(), other), CLEAN, CLEAN, HOLDS, NOT_SO, plan())
        asked = looks(f)
        self.assertEqual(len(asked), 2)
        a, b = f.said(asked[0]), f.said(asked[1])
        self.assertIn("page([1, 2, 3], 2)", a)
        self.assertNotIn("[1, 2, 3, 4, 5]", a, "alone")
        self.assertIn("[1, 2, 3, 4, 5]", b)
        self.assertIn("def page", a, "with its function")
        self.assertIs(f.seen[asked[0]]["chat_template_kwargs"]["enable_thinking"], False)
        said = f.said(len(f.seen) - 1)
        self.assertIn("page([1, 2, 3], 2)", said)
        self.assertNotIn("page([1, 2, 3, 4, 5], 2)", said, "the claim judged not to hold is dropped")

    def test_second_look_a_claim_that_does_not_hold_is_dropped_and_journaled(self):
        f, r, ev = self.full(WHOLE, judge(), CLEAN, defects(claim()), CLEAN, CLEAN, NOT_SO, plan(), FIX_EDIT, DONE, CLOSE)
        recs = [json.loads(x) for x in journal_text(self).splitlines() if '"review"' in x]
        recs = [x for x in recs if x.get("kind") == "review"]
        self.assertEqual(len(recs), 1, journal_text(self))
        self.assertEqual((len(recs[0]["kept"]), len(recs[0]["dropped"])), (0, 1))
        self.assertIn("did not hold", recs[0]["dropped"][0]["reason"])

    def test_second_look_a_failed_or_invalid_second_look_keeps_the_claim(self):
        smith_run = smith.Run(types.SimpleNamespace(), self.repo, self.tmp)
        for what in ("garbage", "error"):
            with self.subTest(what):
                def ask(run, profile, messages, why, **kw):
                    if funcreview.REVIEW_PROMPT[:40] in messages[0]["content"]:
                        if "function page:" in messages[1]["content"]:
                            return {"text": json.dumps({"defects": [claim()]}), "stop": "stop"}
                        return {"text": json.dumps({"defects": []}), "stop": "stop"}
                    if what == "error":
                        raise m.ModelError("the server fell over")
                    return {"text": "I think it is fine, honestly", "stop": "stop"}
                rec = funcreview.review(smith_run, {"name": "x"}, ask=ask)
                self.assertEqual((len(rec["kept"]), len(rec["dropped"])), (1, 0), rec)

    def test_cap_at_most_review_funcs_are_read_and_the_note_says_how_many_of_how_many(self):
        root = tempfile.mkdtemp(dir=self.tmp)
        with open(os.path.join(root, "big.py"), "w") as f:
            f.write("\n\n".join(f"def f{i}(x):\n    return x + {i}" for i in range(funcreview.REVIEW_FUNCS + 2)) + "\n")
        run = smith.Run(types.SimpleNamespace(), root, self.tmp)
        asked = []

        def ask(run_, profile, messages, why, **kw):
            asked.append(why)
            return {"text": json.dumps({"defects": []}), "stop": "stop"}
        rec = funcreview.review(run, {"name": "x"}, ask=ask)
        self.assertEqual((rec["read"], rec["of"]), (funcreview.REVIEW_FUNCS, funcreview.REVIEW_FUNCS + 2))
        self.assertEqual(len(asked), funcreview.REVIEW_FUNCS)
        self.assertEqual(run.count["review_calls"], funcreview.REVIEW_FUNCS)

    def test_cap_failed_calls_in_a_row_end_the_review(self):
        root = tempfile.mkdtemp(dir=self.tmp)
        with open(os.path.join(root, "big.py"), "w") as f:
            f.write("\n\n".join(f"def f{i}(x):\n    return x + {i}" for i in range(10)) + "\n")
        run = smith.Run(types.SimpleNamespace(), root, self.tmp)
        tries = []

        def ask(run_, profile, messages, why, **kw):
            tries.append(why)
            raise m.ModelError("down")
        rec = funcreview.review(run, {"name": "x"}, ask=ask)
        self.assertEqual(len(tries), funcreview.MAX_FAILS)
        self.assertEqual(rec["read"], 0)

    def test_cap_what_a_model_says_is_printable_redacted_and_bounded(self):
        root = tempfile.mkdtemp(dir=self.tmp)
        with open(os.path.join(root, "a.py"), "w") as f:
            f.write("def f(x):\n    return x\n\n\ndef g(x):\n    return x\n")
        run = smith.Run(types.SimpleNamespace(), root, self.tmp)
        wild = claim(promise="", call_="f('\x1b[31m" + "x" * 5000 + "')", actual="line\none\n\x07bell")

        def ask(run_, profile, messages, why, **kw):
            if funcreview.REVIEW_PROMPT[:40] in messages[0]["content"]:
                return {"text": json.dumps({"defects": [wild] * 9}), "stop": "stop"}
            return {"text": json.dumps({"holds": True}), "stop": "stop"}
        rec = funcreview.review(run, {"name": "x"}, ask=ask)
        for c in rec["kept"]:
            self.assertLessEqual(len(c["call"]), 200)
            self.assertTrue(all(ch.isprintable() for v in c.values() for ch in v), c)
        self.assertLessEqual(len(rec["kept"]), funcreview.REVIEW_CLAIMS)
        self.assertLessEqual(len(funcreview.planner_text(run, rec)), funcreview.TEXT_CHARS + 2000)

    def test_before_the_plan_the_review_runs_after_the_judgment_and_before_the_plan(self):
        f, r, ev = self.launch(WHOLE, judge(), CLEAN, CLEAN, CLEAN, CLEAN, plan())
        self.assertEqual(judged(f), [0])
        self.assertEqual(reviews(f), list(range(AT, AT + 4)))
        self.assertEqual(len(f.seen), AT + 5, "the judgment, the survey, four reviews, the plan")
        self.assertNotIn("Before this plan each function", f.said(0))

    def test_before_the_plan_the_run_goes_on_to_do_the_work(self):
        f, r, ev = self.full(WHOLE, judge(), CLEAN, CLEAN, CLEAN, CLEAN, plan(), FIX_EDIT, DONE, CLOSE)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.task()["status"], "done")
        self.assertIn("a + b", self.calc())

    def test_before_the_plan_the_judgment_is_made_once(self):
        f, r, ev = self.full(WHOLE, judge(), CLEAN, CLEAN, CLEAN, CLEAN, plan(), FIX_EDIT, DONE, CLOSE)
        self.assertEqual(judged(f), [0], "the plan's own check reads the cached judgment")

    def test_named_change_no_review(self):
        f, r, ev = self.launch("Fix page() so the last page is kept", judge(True, False), plan())
        self.assertEqual(reviews(f), [], r.stdout + r.stderr)
        self.assertEqual(judged(f), [0])
        self.assertEqual(len(f.seen), AT + 1, "the judgment, the survey, the plan")

    def test_named_change_no_review_a_reply_without_a_verdict_on_the_whole_reviews_nothing(self):
        f, r, ev = self.launch(WHOLE, {"text": json.dumps({"wants_change": True, "why": "x"}), }, plan())
        self.assertEqual(reviews(f), [])

    def test_paid_no_review(self):
        f = self.serve("openai", *surveyed((judge(), plan())))
        path = os.path.join(self.home, "models.json")
        with open(path) as fh:
            conf = json.load(fh)
        conf["profiles"]["fake"]["price"] = {"in": 1.0, "out": 2.0}
        with open(path, "w") as fh:
            json.dump(conf, fh)
        r = self.code(WHOLE, "--model", "fake", "--json", "--dry-plan", check=False)
        self.assertEqual(reviews(f), [], r.stdout + r.stderr)
        self.assertEqual(len(f.seen), AT + 1, "the judgment, the survey, the plan")

    def test_paid_no_review_a_free_priced_model_still_reviews(self):
        f = self.serve("openai", *surveyed((judge(), CLEAN, CLEAN, CLEAN, CLEAN, plan())))
        path = os.path.join(self.home, "models.json")
        with open(path) as fh:
            conf = json.load(fh)
        conf["profiles"]["fake"]["price"] = {"in": 0, "out": 0}
        with open(path, "w") as fh:
            json.dump(conf, fh)
        self.code(WHOLE, "--model", "fake", "--json", "--dry-plan", check=False)
        self.assertEqual(len(reviews(f)), 4)

    def test_before_the_plan_no_review_flag_asks_nothing(self):
        f, r, ev = self.launch(WHOLE, judge(), plan(), args=("--dry-plan", "--no-review"))
        self.assertEqual(reviews(f), [], r.stdout + r.stderr)
        self.assertEqual(len(f.seen), AT + 1, "the judgment and the survey are the plan's own; no review")

    def test_before_the_plan_ask_makes_no_review(self):
        f, r, ev = self.launch(WHOLE, {"text": json.dumps({"answer": "it is fine"})}, call("done", summary="fine"),
                           args=("--ask",))
        self.assertEqual(reviews(f), [], r.stdout + r.stderr)

    def test_before_the_plan_a_verify_run_makes_no_review(self):
        f, r, ev = self.launch(WHOLE, FIX_EDIT, DONE, args=("--no-commit", "--verify", "python3 test_calc.py"))
        self.assertEqual(reviews(f), [], r.stdout + r.stderr)

    def test_skip_reason_names_each_refusal(self):
        run = smith.Run(types.SimpleNamespace(), self.repo, self.tmp)
        free = {"name": "x", "kind": "openai", "base_url": "http://127.0.0.1:9/v1", "model": "m"}
        self.assertEqual(funcreview.skip_reason(run, free), "")
        self.assertIn("paid", funcreview.skip_reason(run, dict(free, price={"in": 1, "out": 2})))
        self.assertIn("CLI", funcreview.skip_reason(run, dict(free, kind="cli")))
        self.assertIn("off this machine", funcreview.skip_reason(run, dict(free, base_url="https://api.example.com/v1")))
        self.assertEqual(funcreview.skip_reason(run, dict(free, base_url="https://gpu.lan/v1", own=True)), "")
        self.assertIn("no model", funcreview.skip_reason(run, None))
        run.worker = "claude"
        self.assertIn("worker", funcreview.skip_reason(run, free))
        run.worker, run.own_plan = None, True
        self.assertIn("own", funcreview.skip_reason(run, free))
        run.own_plan, run.planner = False, dict(free, price={"in": 1, "out": 2})
        self.assertIn("paid", funcreview.skip_reason(run, free), "the planner is asked too")

    def test_journal_one_review_record_with_what_was_read_kept_dropped_and_the_seconds(self):
        f, r, ev = self.full(WHOLE, judge(), CLEAN, defects(claim()), CLEAN, CLEAN, HOLDS, plan(), FIX_EDIT, DONE, CLOSE)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        recs = [x for x in (json.loads(ln) for ln in journal_text(self).splitlines()) if x.get("kind") == "review"]
        self.assertEqual(len(recs), 1)
        rec = recs[0]
        self.assertEqual((rec["read"], rec["of"], len(rec["kept"]), rec["dropped_n"]), (4, 4, 1, 0))
        self.assertIsInstance(rec["seconds"], float)
        self.assertEqual(rec["kept"][0]["function"], "page")

    def test_journal_stats_count_the_review_s_calls(self):
        f, r, ev = self.full(WHOLE, judge(), CLEAN, defects(claim()), CLEAN, CLEAN, HOLDS, plan(), FIX_EDIT, DONE, CLOSE)
        got = [e for e in ev if e["event"] == "stats"]
        self.assertEqual(len(got), 1, r.stdout)
        self.assertEqual(got[0]["review_calls"], 5, "four reviews and one second look")

    def test_resume_makes_no_second_review(self):
        f, r, ev = self.full(WHOLE, judge(), CLEAN, defects(claim()), CLEAN, CLEAN, HOLDS, plan(),
                             call("read", path="calc.py"), call("read", path="calc.py"), args=("--max-steps", "2"))
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        g = self.serve("openai", FIX_EDIT, DONE, CLOSE)
        r = self.code("--resume", check=False)
        self.assertEqual(self.task()["status"], "done", r.stdout + r.stderr)
        self.assertEqual((reviews(g), judged(g), looks(g)), ([], [], []))
        recs = [x for x in (json.loads(ln) for ln in journal_text(self).splitlines()) if x.get("kind") == "review"]
        self.assertEqual(len(recs), 1)
        self.assertIn("page([1, 2, 3], 2)", g.said(0), "the review rides the first step's prompt on a resume too")

    def test_resume_stats_go_on_from_the_review_s_calls(self):
        self.full(WHOLE, judge(), CLEAN, defects(claim()), CLEAN, CLEAN, HOLDS, plan(), call("read", path="calc.py"),
                  call("read", path="calc.py"), args=("--max-steps", "2"))
        self.serve("openai", FIX_EDIT, DONE, CLOSE)
        r = self.code("--resume", "--json", check=False)
        got = [json.loads(ln) for ln in r.stdout.splitlines() if ln.startswith("{")]
        stats = [e for e in got if e["event"] == "stats"]
        self.assertEqual(stats[-1]["review_calls"], 5, r.stdout)

    def test_the_first_step_carries_the_review(self):
        f, r, ev = self.full(WHOLE, judge(), CLEAN, defects(claim()), CLEAN, CLEAN, HOLDS, plan(), FIX_EDIT, DONE, CLOSE)
        step = f.said(len(f.seen) - 3)
        self.assertIn("page([1, 2, 3], 2)", step, "the first step's prompt holds the claim")


FEATURE_ASKS = (
    "Implement team files so a route combines with its parent's rules; fix what is wrong along the way.",
    "Migrate the helpers to the new page format and find what is broken.",
    "Make the pager faster: it is slow on big lists, fix it.",
    "Add a feature that exports every total, and fix the bug in it.",
)
DEFECT_ASKS = (WHOLE, "Check the helpers are right.", "Review the code for bugs and fix them.",
               "Something is broken in the paging; find and fix it.")


def kind_note(ev):
    """The one wants_change note of a run: (its find_fix, its text)."""
    n = [e for e in ev if e["event"] == "note" and "find_fix" in e]
    return (n[0]["find_fix"], json.dumps(n[0])) if len(n) == 1 else (None, json.dumps(ev))


class DefectShaped(Pager):
    """T-4388 (the measurement in exam-review.md): the review's leads are for find-and-fix requests, never the plan of a
    feature, migration or performance request. The kind is the model's judgment (the one wants_change call's find_fix);
    the word lists (smith.defect_shaped) only stand in when the model gave no valid find_fix."""

    def test_defect_shaped_the_find_and_fix_kind_only(self):  # the fallback's own lists
        for r in DEFECT_ASKS + ("Find the bugs in the implementation of page().",):
            self.assertTrue(smith.defect_shaped(r), r)
        for r in FEATURE_ASKS + ("", None, "Does page() keep the last page, and is it broken?"):
            self.assertFalse(smith.defect_shaped(r), r)

    def test_defect_shaped_a_feature_request_makes_no_review_even_when_judged_whole(self):
        for ask in FEATURE_ASKS:
            f, r, ev = self.launch(ask, judge(True, True, False), plan())
            self.assertEqual(reviews(f), [], ask + r.stdout + r.stderr)
            self.assertEqual(len(f.seen), AT + 1, ask)
            self.assertNotIn("each function", json.dumps(f.seen[-1]["messages"][1]), ask)
            self.assertNotIn("export_csv", f.said(len(f.seen) - 1), "no lead of the review shapes the plan")
            kind, text = kind_note(ev)
            self.assertIs(kind, False, text)
            self.assertNotIn("fallback", text, "the model judged it")

    def test_defect_shaped_a_find_and_fix_request_is_still_reviewed(self):
        f, r, ev = self.launch(DEFECT_ASKS[2], judge(), CLEAN, CLEAN, CLEAN, CLEAN, plan())
        self.assertEqual(reviews(f), list(range(AT, AT + 4)), r.stdout + r.stderr)
        self.assertEqual(kind_note(ev)[0], True)
        self.assertEqual(len(judged(f)), 1, "the kind rides the one judgment: no second call")

    def test_defect_shaped_the_model_decides_not_the_words(self):
        f, r, ev = self.launch(FEATURE_ASKS[0], judge(True, True, True), CLEAN, CLEAN, CLEAN, CLEAN, plan())
        self.assertEqual(reviews(f), list(range(AT, AT + 4)), "feature words, but the model says find and fix")
        f, r, ev = self.launch(DEFECT_ASKS[1], judge(True, True, False), plan())
        self.assertEqual(reviews(f), [], "find words, but the model says it is not a find-and-fix request")

    def test_defect_shaped_a_reply_without_the_key_falls_back_to_the_word_lists_and_the_note_says_so(self):
        old = judge(True, True, None)  # an older verdict reply: no find_fix
        f, r, ev = self.launch(DEFECT_ASKS[2], old, CLEAN, CLEAN, CLEAN, CLEAN, plan())
        self.assertEqual(reviews(f), list(range(AT, AT + 4)), "the lists call it find-and-fix")
        kind, text = kind_note(ev)
        self.assertIs(kind, True, text)
        self.assertIn("fallback", text)
        f, r, ev = self.launch(FEATURE_ASKS[0], old, plan())
        self.assertEqual(reviews(f), [], "the lists call it a feature")
        self.assertIn("fallback", kind_note(ev)[1])

    def test_defect_shaped_a_reply_that_is_not_a_verdict_falls_back_too(self):
        f = self.serve("openai", {"text": "no idea"}, *SURVEYED, plan())
        r = self.code(DEFECT_ASKS[2], "--model", "fake", "--json", "--dry-plan", check=False)
        ev = [json.loads(ln) for ln in r.stdout.splitlines() if ln.startswith("{")]
        self.assertEqual(reviews(f), [], "a fallback verdict is never a whole one")
        self.assertIn("fallback", kind_note(ev)[1])


class OneFunction(CodeCase):
    def test_per_function_a_repo_of_one_function_is_not_reviewed(self):
        f = self.serve("openai", plan())
        r = self.code(WHOLE, "--model", "fake", "--json", "--dry-plan", check=False)
        self.assertEqual((reviews(f), judged(f)), ([], []), "no whole to review, and no judgment spent to find out")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
