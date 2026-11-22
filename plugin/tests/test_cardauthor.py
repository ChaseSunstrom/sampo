"""T-4838 (T-3157 part 4): the calling-card authoring loop: a writer drafts a card for a request, local models first,
and the draft joins the store only when its own tests pass in the confined runner."""
import json
import os
import sys
import unittest
from unittest import mock

from helpers import SampoTestCase

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
from sampo import callcards, cardauthor  # noqa: E402
from sampo import core as c  # noqa: E402

confined = unittest.skipUnless(callcards.confinement()[0], "no confinement on this machine: cards don't run")

GEN = ("from cardkit import emit, read_input, source\n"
       "inp = read_input()[\"inputs\"]\n"
       "emit({\"path\": inp[\"path\"], \"words\": len(source(inp[\"path\"]).split())})\n")
CHECK = ("from cardkit import fail, read_input, source\n"
         "inp = read_input()\n"
         "f = inp[\"facts\"]\n"
         "if not isinstance(f, dict) or f.get(\"words\") != len(source(inp[\"inputs\"][\"path\"]).split()):\n"
         "    fail(\"the word count is wrong: \" + repr(f)[:80])\n")
CARD = {"name": "count-words", "kind": "py", "read": True, "deterministic": True,
        "purpose": "Count the words in a text file, split on whitespace.",
        "examples": ["how many words are in notes.txt", "count the words of a file", "word count of the readme"],
        "inputs": {"type": "object", "required": ["path"],
                   "properties": {"path": {"type": "string", "format": "path", "description": "the text file"}}},
        "files": ["$path"]}
TESTS = [{"name": "counts", "files": {"a.txt": "one two three\n"}, "inputs": {"path": "a.txt"}, "expect": "facts",
          "facts": {"path": "a.txt", "words": 3}},
         {"name": "a wrong count is refused", "files": {"a.txt": "one two\n"}, "inputs": {"path": "a.txt"},
          "inject_facts": {"path": "a.txt", "words": 9}, "expect": "checker_failed"}]


def reply(card=None, tests=None, **files):
    """A writer's reply: the card's files as one JSON object, in a fence as models often send it."""
    body = {"card.json": card or CARD, "gen.py": GEN, "check.py": CHECK, "tests.json": TESTS if tests is None else tests}
    body.update(files)
    return "Here is the card:\n```json\n" + json.dumps({"files": body}) + "\n```\n"


class Writer:
    """Stands in for the writers: hands out replies in order and keeps each (writer, prompt) it was asked."""

    def __init__(self, *replies):
        self.replies, self.asked = list(replies), []

    def __call__(self, system, prompt, writer, paid):
        self.asked.append((writer, prompt))
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r, f"{writer}-model"


class Base(SampoTestCase):
    def setUp(self):
        super().setUp()
        self.p = c.init_project(self.repo)
        env = mock.patch.dict(os.environ, {"FOREMAN_NO_LOCAL_MODELS": "1"})
        env.start()
        self.addCleanup(env.stop)

    def request(self, text, **kw):
        os.makedirs(os.path.dirname(callcards.requests_path()), exist_ok=True)
        c.append_line(callcards.requests_path(), json.dumps(dict({"at": c.now(), "text": text}, **kw)))


@confined
class Author(Base):
    def test_author_admits_a_draft_whose_own_tests_pass(self):
        w = Writer(reply())
        r = cardauthor.author(self.p, {"text": "count the words in a text file"}, ask=w, root=self.repo)
        self.assertEqual((r["verdict"], r["card"], r["tries"]), ("admitted", "count-words", 1), r)
        self.assertEqual([x for x, _ in w.asked], ["local"])
        self.assertTrue(os.path.isfile(os.path.join(cardauthor.store(), "count-words", "gen.py")))
        self.assertIn("count-words", callcards.cards(self.repo)[0])  # pick and call see it at once
        with open(os.path.join(self.repo, "notes.txt"), "w") as f:
            f.write("a b c d\n")
        got = callcards.call("count-words", {"path": "notes.txt"}, self.repo, cache=False)
        self.assertEqual(got["facts"], {"path": "notes.txt", "words": 4}, got)
        row = cardauthor.outcomes()[cardauthor.request_id("count the words in a text file")]
        self.assertEqual((row["verdict"], row["card"]), ("admitted", "count-words"))

    def test_author_repairs_from_each_failure_then_falls_back_to_sonnet(self):
        w = Writer("not json at all", reply(tests=[TESTS[0], dict(TESTS[0], name="again")]), reply())
        r = cardauthor.author(self.p, {"text": "count the words in a text file"}, ask=w, root=self.repo)
        self.assertEqual(r["verdict"], "admitted", r)
        self.assertEqual([x for x, _ in w.asked], ["local", "local", "sonnet"])
        self.assertIn("no JSON object", w.asked[1][1])  # each retry reads why the draft before it failed
        self.assertIn("must refuse", w.asked[2][1])

    def test_author_keeps_a_rejected_request_with_its_reason_and_last_draft(self):
        bad = [dict(TESTS[0], facts={"path": "a.txt", "words": 4}), TESTS[1]]  # the draft's own test is wrong
        w = Writer(reply(tests=bad), reply(tests=bad), reply(tests=bad))
        r = cardauthor.author(self.p, {"text": "count the words in a text file"}, ask=w, root=self.repo)
        self.assertEqual(r["verdict"], "rejected", r)
        self.assertIn("tests: counts", r["reason"])
        self.assertNotIn("count-words", callcards.cards(self.repo)[0])
        self.assertFalse(os.path.exists(os.path.join(cardauthor.store(), "count-words")))
        kept = os.path.join(cardauthor.base(), "rejected", cardauthor.request_id("count the words in a text file"))
        self.assertTrue(os.path.isfile(os.path.join(kept, "card.json")))
        self.assertEqual(cardauthor.outcomes()[r["request"]]["reason"], r["reason"])

    def test_author_takes_a_writers_unfit_verdict_and_keeps_the_last_reply(self):
        w = Writer('{"unfit": "a change to Sampo, not a tool over files"}')
        r = cardauthor.author(self.p, {"text": "FIX: the gate denied subagents"}, ask=w, root=self.repo)
        self.assertEqual((r["verdict"], r["tries"]), ("unfit", 1), r)  # no paid writer asked to confirm it
        self.assertIn("not a tool over files", r["reason"])
        self.request("FIX: the gate denied subagents")
        self.assertEqual(cardauthor.pending(self.repo), [])
        self.assertEqual(cardauthor.pending(self.repo, retry=True), [])  # --retry re-asks rejected drafts, not these
        w = Writer('{"files": {"card.json": null, "gen.py": null}}', "still nothing")
        r = cardauthor.author(self.p, {"text": "count the words"}, ask=w, root=self.repo, writer="local")
        self.assertIn("card.json is empty", w.asked[1][1])
        kept = os.path.join(cardauthor.base(), "rejected", r["request"], "reply.txt")
        with open(kept, encoding="utf-8") as f:
            self.assertEqual(f.read(), "still nothing")  # the raw reply, to read why a writer fails

    def test_author_takes_a_writers_covered_verdict_naming_a_card_that_does_it(self):
        cardauthor.author(self.p, {"text": "count the words in a text file"}, ask=Writer(reply()), root=self.repo)
        w = Writer("=== covered\ncount-words\n")
        r = cardauthor.author(self.p, {"text": "how long is this essay in words"}, ask=w, root=self.repo)
        self.assertEqual((r["verdict"], r["card"], r["tries"]), ("covered", "count-words", 1), r)
        self.assertIn("count-words: Count the words", w.asked[0][1])  # the writer sees the catalogue
        self.request("how long is this essay in words")
        self.assertEqual(cardauthor.pending(self.repo, retry=True), [])
        w = Writer("=== covered\nno-such-card\n", "nope")
        r = cardauthor.author(self.p, {"text": "sum a column"}, ask=w, root=self.repo, writer="local")
        self.assertEqual(r["verdict"], "rejected")  # a card that isn't there covers nothing
        self.assertIn("no card named 'no-such-card'", w.asked[1][1])  # and the retry is told so

    def test_author_asks_the_named_local_model_first(self):
        from sampo import moonscale
        with mock.patch.object(moonscale, "_ask", return_value=("=== unfit\nno\n", "m")) as a:
            cardauthor.author(self.p, {"text": "count the words"}, root=self.repo, model="big-coder")
        self.assertEqual(a.call_args.kwargs["profile"], "big-coder")

    def test_author_local_only_never_asks_a_paid_writer(self):
        w = Writer("nope", "nope")
        r = cardauthor.author(self.p, {"text": "count the words"}, ask=w, root=self.repo, writer="local")
        self.assertEqual([x for x, _ in w.asked], ["local", "local"])
        self.assertEqual(r["verdict"], "rejected")


@confined
class Gate(Base):
    def gate(self, text):
        return cardauthor.author(self.p, {"text": "count the words in a text file"}, ask=Writer(text), root=self.repo,
                                 writer="sonnet")

    def test_gate_wants_a_real_run_and_a_refusal(self):
        self.assertIn("must refuse", self.gate(reply(tests=[TESTS[0], dict(TESTS[0], name="again")]))["reason"])
        self.assertIn("runs gen.py for real", self.gate(reply(tests=[TESTS[1], dict(TESTS[1], name="again")]))["reason"])
        self.assertIn("2 to 12", self.gate(reply(tests=[TESTS[0]]))["reason"])

    def test_gate_refuses_a_taken_name_a_bad_file_name_and_a_chain(self):
        self.assertTrue(self.gate(reply())["verdict"] == "admitted")
        self.assertIn("exists", self.gate(reply())["reason"])
        self.assertIn("file name", self.gate(reply(**{"../x.py": "print(1)"}))["reason"])
        chain = dict(CARD, name="two-counts", kind="chain", steps=[{"card": "count-words", "inputs": {"path": "$path"}}])
        self.assertIn("kind", self.gate(reply(card=chain))["reason"])

    def test_gate_a_draft_that_does_not_load_names_why(self):
        r = self.gate(reply(card=dict(CARD, name="Bad Name")))
        self.assertEqual(r["verdict"], "rejected")
        self.assertIn("name", r["reason"])

    def test_gate_wants_the_real_run_to_check_something(self):
        loose = {k: v for k, v in TESTS[0].items() if k != "facts"}  # a card that emits anything would pass it
        self.assertIn("checks nothing", self.gate(reply(tests=[loose, TESTS[1]]))["reason"])

    def test_gate_never_reads_a_file_outside_the_case_folder(self):
        # review: a case's after keys name files to compare, and the mismatch quotes what was read; the reason goes to
        # the next writer (a paid one last), so a path out of the case's folder, or a link gen.py makes, reads nothing
        secret = os.path.join(self.tmp, "host-secret.txt")
        with open(secret, "w") as f:
            f.write("HOST-SECRET-TEXT\n")
        for after in ({secret: ""}, {"../host-secret.txt": ""}):
            with self.subTest(after=after):
                r = self.gate(reply(tests=[dict(TESTS[0], after=after), TESTS[1]]))
                self.assertEqual(r["verdict"], "rejected", r)
                self.assertNotIn("HOST-SECRET", r["reason"])
        link = GEN + "import os\nos.symlink(%r, 'out.txt')\n" % secret
        r = self.gate(reply(tests=[dict(TESTS[0], after={"out.txt": ""}), TESTS[1]], **{"gen.py": link}))
        self.assertEqual(r["verdict"], "rejected", r)
        self.assertNotIn("HOST-SECRET", r["reason"])

    def test_gate_survives_a_draft_nested_too_deep_to_parse(self):
        for name in ("tests.json", "card.json"):
            with self.subTest(name=name):
                r = self.gate(reply(**{name: "[" * 60000}))  # json's recursion limit: RecursionError, no ValueError
                self.assertEqual(r["verdict"], "rejected", r)

    def test_drafts_are_gated_one_at_a_time(self):
        # review: the staged folders calls can see are the process's; two drafts gated at once could each run the
        # other's gen.py (the same name), and one could be admitted on code its tests never ran
        import threading
        import time
        seen = []

        def gate(d, existing):
            seen.append(len(callcards._STAGED))
            time.sleep(0.2)
            return None, "stub"
        files = cardauthor.parse(reply())
        with mock.patch.object(cardauthor, "_gate", side_effect=gate):
            ts = [threading.Thread(target=cardauthor._try, args=(files, self.repo)) for _ in range(2)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
        self.assertEqual(seen, [1, 1])


@confined
class Grow(Base):
    def test_grow_authors_pending_requests_most_repeated_first_in_batches(self):
        self.request("count the words in a text file")
        self.request("count the words in a text file")
        self.request("list the TODO lines of a file", source="mine", title="list the TODO lines of a file", count=1)
        self.request("reverse the lines of a file")
        todo = [q["text"] for q in cardauthor.pending(self.repo)]
        self.assertEqual(todo[0], "count the words in a text file")  # said twice
        self.assertEqual(len(todo), 3)
        w = Writer(*[reply(card=dict(CARD, name=n, purpose=f"{n} alone", examples=[n])) for n in
                     ("count-words", "list-todos", "reverse-lines")])
        r = cardauthor.grow(self.p, batch=2, ask=w, root=self.repo, mine=False, ideas=0)
        self.assertEqual((r["admitted"], r["rejected"], r["remaining"]), (["count-words", "list-todos"], 0, 1), r)
        r = cardauthor.grow(self.p, batch=2, ask=w, root=self.repo, mine=False, ideas=0)
        self.assertEqual((r["admitted"], r["remaining"]), (["reverse-lines"], 0), r)
        self.assertEqual(cardauthor.grow(self.p, ask=w, root=self.repo, mine=False, ideas=0)["admitted"], [])

    def test_grow_authors_as_many_at_once_as_it_has_workers(self):
        self.request("count the words in a text file")
        self.request("list the TODO lines of a file")
        w = Writer(*[reply(card=dict(CARD, name=n, purpose=f"{n} alone", examples=[n])) for n in ("count-words", "list-todos")])
        r = cardauthor.grow(self.p, batch=2, ask=w, root=self.repo, mine=False, ideas=0, workers=2)
        self.assertEqual((sorted(r["admitted"]), r["calls"], r["stopped"]), (["count-words", "list-todos"], 2, "done"), r)

    def test_grow_tops_up_a_short_queue_with_proposals(self):
        idea = json.dumps({"requests": [{"request": "count the words in a text file", "why": "docs"}]})
        w = Writer(idea, reply())
        r = cardauthor.grow(self.p, batch=3, ask=w, root=self.repo, mine=False)
        self.assertEqual((r["proposed"], r["admitted"], r["calls"]), (1, ["count-words"], 2), r)
        self.assertIn("Propose", w.asked[0][1])
        self.assertEqual(cardauthor.grow(self.p, batch=3, ask=Writer(), root=self.repo, mine=False, ideas=0)["calls"], 0)

    def test_grow_mines_new_requests_first_unless_told_not_to(self):
        from sampo import cardmine

        def mined(p, **kw):
            self.request("count the words in a text file", source="mine", title="count the words in a text file")
            return {"added": [{"title": "count the words in a text file"}], "found": 1, "covered": 0, "open": 0}
        with mock.patch.object(cardmine, "mine", side_effect=mined) as m:
            r = cardauthor.grow(self.p, batch=1, ask=Writer(reply()), root=self.repo)
            self.assertEqual((r["mined"], r["admitted"]), (1, ["count-words"]), r)
            cardauthor.grow(self.p, ask=Writer(), root=self.repo, mine=False, ideas=0)
        self.assertEqual(m.call_count, 1)
        with mock.patch.object(cardmine, "mine", side_effect=c.LockTimeout("busy")):  # a busy store: author what's filed
            self.assertIn("mine: busy", cardauthor.grow(self.p, ask=Writer(), root=self.repo, ideas=0)["error"])

    def test_author_records_nothing_when_no_writer_answers(self):
        from sampo import models
        w = Writer(*[models.ModelError("connection refused")] * 3)  # an outage is no verdict on the request
        r = cardauthor.author(self.p, {"text": "count the words in a text file"}, ask=w, root=self.repo)
        self.assertEqual(r["verdict"], "error", r)
        self.assertNotIn(r["request"], cardauthor.outcomes())
        self.request("count the words in a text file")
        self.assertEqual(len(cardauthor.pending(self.repo)), 1)  # asked again on the next run

    def test_grow_stops_when_the_writers_keep_failing_to_answer(self):
        from sampo import models
        for t in ("first request here", "second request here", "third request here", "fourth request here"):
            self.request(t)
        w = Writer(*[models.ModelError("down")] * 20)
        r = cardauthor.grow(self.p, batch=4, ask=w, root=self.repo, writer="local", mine=False, ideas=0)
        self.assertEqual((r["stopped"], r["rejected"]), ("errors", 0), r)
        self.assertEqual(len(cardauthor.pending(self.repo)), 4)

    def test_grow_stops_at_its_call_budget(self):
        for t in ("first request here", "second request here", "third request here"):
            self.request(t)
        w = Writer(*["nope"] * 9)
        r = cardauthor.grow(self.p, batch=3, max_calls=4, ask=w, root=self.repo, writer="local", mine=False, ideas=0)
        self.assertEqual((r["stopped"], r["calls"], r["rejected"]), ("max_calls", 4, 2), r)

    def test_grow_skips_a_request_a_card_already_covers(self):
        w = Writer(reply())
        cardauthor.author(self.p, {"text": "count the words in a text file"}, ask=w, root=self.repo)
        self.request("count the words of a text file")  # count-words fits it now
        self.request("list the TODO lines of a text file")  # one shared word or two is no cover (the writer judges)
        self.assertEqual([q["text"] for q in cardauthor.pending(self.repo)], ["list the TODO lines of a text file"])


class Propose(Base):
    def test_propose_files_new_requests_from_the_catalogue_and_its_outcomes(self):
        self.request("count the words in a text file")  # filed already
        cardauthor._record({"at": c.now(), "request": "r1", "text": "reply pong", "verdict": "unfit", "card": None,
                            "reason": "unfit: a chat, not a tool", "tries": 1, "writers": ["local"]})
        w = Writer("Ideas:\n" + json.dumps({"requests": [
            {"request": "Count the words in a text file.", "why": "docs"},
            {"request": "list the imports of a Python file", "why": "dep tasks read them"},
            {"request": "x", "why": ""},
            "not an object",
            {"request": "find bare except clauses in a Python file", "why": "bug tasks"},
            {"request": "list the imports of a python file!", "why": "a repeat"}]}))
        r = cardauthor.propose(self.p, n=5, ask=w, root=self.repo)
        self.assertEqual(r["added"], ["list the imports of a Python file", "find bare except clauses in a Python file"])
        self.assertEqual([x for x, _ in w.asked], ["local"])  # proposing is bulk work: local models only
        prompt = w.asked[0][1]
        self.assertIn("py-outline", prompt)  # the catalogue it must not repeat
        self.assertIn("a chat, not a tool", prompt)  # and what was turned down, so it proposes better
        self.assertIn("count the words in a text file", prompt)  # what is filed already
        got = {q["text"]: q for q in cardauthor.requests()}
        self.assertEqual(got["find bare except clauses in a Python file"]["source"], "propose")
        self.assertEqual(got["find bare except clauses in a Python file"]["why"], "bug tasks")

    def test_propose_fit_rules_and_admitted_examples_are_in_the_prompt(self):
        """T-5829: tonight's local grow admitted 0 of 13 (10 unfit): the proposer must know what a card fits."""
        w = Writer(json.dumps({"requests": []}))
        cardauthor.propose(self.p, n=3, ask=w, root=self.repo)
        system = cardauthor.PROPOSE_SYSTEM
        for rule in ("ONE file kind", "deterministic", "no cross-file judgement"):
            self.assertIn(rule, system)
        for card in ("py-outline", "py-none-guard"):  # two admitted examples, read from the shipped cards
            self.assertIn(card, w.asked[0][1])
        self.assertIn("Fit examples", w.asked[0][1])
        for ex in cardauthor.FIT_EXAMPLES:
            self.assertTrue(ex in w.asked[0][1])
        self.assertEqual(len(cardauthor.FIT_EXAMPLES), 2)
        for card in ("py-outline", "py-none-guard"):  # the examples name cards that still ship
            self.assertTrue(os.path.isdir(os.path.join(c.PLUGIN_ROOT, "cards", card)))

    def test_propose_survives_a_writer_that_gives_nothing(self):
        r = cardauthor.propose(self.p, n=3, ask=Writer("no json here"), root=self.repo)
        self.assertEqual(r["added"], [])
        self.assertIn("no JSON", r["error"])


class Cli(Base):
    def test_card_grow_with_nothing_pending_calls_no_writer(self):
        r = json.loads(self.fm("card", "grow", "--json").stdout)
        self.assertEqual((r["admitted"], r["calls"], r["remaining"], r["stopped"]), ([], 0, 0, "done"))
        self.assertIn("0 request(s) left", self.fm("card", "grow").stdout)

    def test_card_author_needs_the_request_and_grow_a_batch(self):
        r = self.fm("card", "author", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("sampo card author", r.stderr)
        self.assertIn("--batch", self.fm("card", "grow", "--batch", "0", check=False).stderr)


class Parse(unittest.TestCase):
    def test_parse_takes_the_files_object_and_writes_json_files_as_text(self):
        files = cardauthor.parse(reply())
        self.assertEqual(json.loads(files["card.json"])["name"], "count-words")
        self.assertIsInstance(json.loads(files["tests.json"]), list)
        self.assertEqual(files["gen.py"], GEN)

    def test_parse_takes_files_as_blocks_so_code_needs_no_escaping(self):
        text = ("Here it is.\n=== card.json\n" + json.dumps(CARD) + "\n=== gen.py\n```python\n" + GEN + "```\n"
                "=== check.py\n" + CHECK + "\n=== tests.json\n```json\n" + json.dumps(TESTS) + "\n```\n")
        files = cardauthor.parse(text)
        self.assertEqual(sorted(files), ["card.json", "check.py", "gen.py", "tests.json"])
        self.assertEqual(files["gen.py"], GEN)  # the fence a model wraps code in is not part of the file
        self.assertEqual(files["check.py"], CHECK)
        self.assertEqual(json.loads(files["tests.json"]), TESTS)
        with self.assertRaises(cardauthor.Unfit) as e:
            cardauthor.parse("=== unfit\na chat, not a tool\n")
        self.assertEqual(str(e.exception), "a chat, not a tool")
        with self.assertRaises(ValueError) as e:
            cardauthor.parse("=== gen.py\nprint(1)\n")
        self.assertIn("card.json", str(e.exception))

    def test_parse_takes_python_only_as_gen_and_check(self):
        # review: the card's folder comes first on the runner's path, so a draft's cardkit.py or json.py would stand
        # in for the real module (a check that never fails)
        for name in ("cardkit.py", "json.py", "helpers.py"):
            with self.subTest(name=name), self.assertRaises(ValueError) as e:
                cardauthor.parse(json.dumps({"files": {"card.json": CARD, "gen.py": "x", name: "x"}}))
            self.assertIn("file name", str(e.exception))

    def test_parse_refuses_what_is_not_a_card(self):
        for text, why in (("no braces", "no JSON object"), ('{"files": {"gen.py": "x"}}', "card.json"),
                          (json.dumps({"files": dict({"card.json": CARD}, **{f"f{i}.py": "x" for i in range(9)})}),
                           "files at most"),
                          (json.dumps({"files": {"card.json": CARD, "gen.py": "x" * (65 << 10)}}), "too big"),
                          ('{"files": {"card.json": null}}', "card.json is empty"),
                          (json.dumps({"files": {"card.json": CARD, "gen.py": "  "}}), "gen.py is empty")):
            with self.assertRaises(ValueError) as e:
                cardauthor.parse(text)
            self.assertIn(why, str(e.exception))


if __name__ == "__main__":
    unittest.main()
