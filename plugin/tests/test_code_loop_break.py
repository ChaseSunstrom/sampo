"""T-2966: the third identical action in a step goes once to the planner (else escalate) profile, else the actor gets a
note naming the repetition; a journal event records each break; a run with no repeats sends nothing extra."""
import json
import os

from test_code import CLOSE, FIX, M_PLAN, CodeCase, call, journal_text, plan

from sampo import smith

READ = call("read", path="calc.py")


def breaks(case):
    return [json.loads(l) for l in journal_text(case).splitlines() if '"loop_break"' in l]


class LoopBreak(CodeCase):
    def stuck(self, flag):
        fix = call("edit", file="calc.py", old="a - b", new="a + b")
        pl = flag == "--planner"  # escalate isn't asked the plan or the close notes: the actor is
        big = self.serve("anthropic", *([plan(M_PLAN), fix, CLOSE] if pl else [fix]), name="big")
        small = self.serve("anthropic", *([] if pl else [plan(M_PLAN)]), READ, READ, READ, call("done", summary="x"),
                           *([] if pl else [CLOSE]), name="small")
        return self.code("fix add", "--model", "small", flag, "big", check=False), big, small

    def test_the_third_repeat_goes_once_to_the_planner_and_the_actor_carries_on(self):
        self.check_break("--planner")

    def test_escalate_takes_it_when_no_planner_is_set(self):
        self.check_break("--escalate")

    def check_break(self, flag):
        r, big, small = self.stuck(flag)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        pl = flag == "--planner"
        self.assertEqual(len(small.seen), 4 if pl else 6, "three reads, then the actor carries on")
        self.assertEqual(len(big.seen), 3 if pl else 1, "plan+handoff+close, or the handoff only")
        self.assertIn("same action", big.said(1 if pl else 0))
        self.assertIn("same action", small.said(3 if pl else 4), "the actor is told what the stronger model did")
        self.assertEqual([b["action"] for b in breaks(self)], ["read"])

    def test_with_neither_profile_the_actor_gets_a_note(self):
        small = self.serve("anthropic", plan(M_PLAN), READ, READ, READ, *FIX, CLOSE, name="small")
        r = self.code("fix add", "--model", "small")
        self.assertEqual(len(small.seen), 7, r.stdout)
        self.assertIn("same action", small.said(4))
        self.assertNotIn("same action", small.said(3))
        self.assertEqual(len(breaks(self)), 1)

    def test_a_run_with_no_repeats_sends_nothing_extra(self):
        small = self.serve("anthropic", plan(M_PLAN), READ, *FIX, name="small")
        big = self.serve("anthropic", plan(M_PLAN), CLOSE, name="big")
        self.code("fix add", "--model", "small", "--planner", "big")
        self.assertEqual(len(big.seen), 2, "only the plan and the close notes")
        self.assertEqual(breaks(self), [])


# T-4177: a local model explored 40 turns (23 reads of 6 files, 12 searches) and never edited. The loop keeps what the
# model was shown (a repeat gets a note, never the text again), caps exploration per step and forces the edit.
CODE = "return a - b"  # a line only the file's own text holds: the note must not carry it
EXPLORE = ("def", "assert", "print", "import", "calc", "add", "ok", "test")  # eight searches, each a new one
SEARCHES = [call("search", pattern=p) for p in EXPLORE]
EDIT = call("edit", file="calc.py", old="a - b", new="a + b")
# small-model mode looks read-only before it plans a request that asks for a change; a request that doesn't ask for one
# (it opens with a question word) goes straight to the plan, so the scripts below keep their turn numbers
SMALL_ASK = "do fix add"


def tool_names(body):
    return {t["name"] for t in body.get("tools") or []}


def last(f, i):
    """What the model was last told in its request i: the newest message only (earlier ones may hold the text)."""
    return json.dumps(f.seen[i]["messages"][-1])


class Seen(CodeCase):
    def run_script(self, *script, extra=()):
        f = self.serve("anthropic", plan(), *script, name="small")
        r = self.code("fix add", "--model", "small", *extra, check=False)
        return f, r

    def test_seen_a_second_read_of_an_unchanged_file_is_a_note_not_the_text(self):
        f, r = self.run_script(READ, READ, EDIT, call("done", summary="x"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(CODE, last(f, 2), "the first read sends the text")
        self.assertNotIn(CODE, last(f, 3), "the second is answered with a note")
        self.assertIn("you have this", last(f, 3))
        self.assertIn("def add", last(f, 3), "the note outlines what was shown")
        self.assertIn("return a + b", self.calc())

    def test_seen_a_range_inside_what_was_shown_is_covered_one_outside_is_not(self):
        part = call("read", path="calc.py", start=1, lines=1)
        inner = call("read", path="calc.py", start=2, lines=1)
        f, r = self.run_script(part, READ, inner, EDIT, call("done", summary="x"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn(CODE, last(f, 2), "line 1 only was shown")
        self.assertIn(CODE, last(f, 3), "the whole file wasn't covered by line 1: sent")
        self.assertNotIn(CODE, last(f, 4), "line 2 is inside the whole file just shown")
        self.assertIn("you have this", last(f, 4))

    def test_seen_a_file_that_changed_is_sent_again(self):
        f, r = self.run_script(READ, EDIT, READ, call("done", summary="x"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("return a + b", last(f, 4), "the edit changed it: the new text, not a note")
        self.assertNotIn("you have this", last(f, 4))

    def test_seen_text_the_model_no_longer_holds_is_sent_again(self):
        # the first read's result is cut from the context after KEEP_RESULTS more: a repeat then needs the text
        f, r = self.run_script(READ, *SEARCHES[:6], READ, EDIT, call("done", summary="x"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(CODE, last(f, 9), "cut from the conversation: sent whole")
        self.assertNotIn("you have this", last(f, 9))

    def test_seen_a_reworded_search_with_the_same_hits_is_a_note(self):
        a, b, c = (call("search", pattern=p) for p in ("def add", "def a", "assert"))
        f, r = self.run_script(a, b, c, EDIT, call("done", summary="x"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("calc.py", last(f, 2))
        self.assertIn("you have this", last(f, 3), "'def a' hits the same line as 'def add'")
        self.assertNotIn("you have this", last(f, 4), "'assert' has other hits: shown")
        self.assertIn("test_calc.py", last(f, 4))

    def test_seen_a_search_with_no_match_twice_is_not_a_repeat(self):
        a, b = call("search", pattern="zzz1"), call("search", pattern="zzz2")
        f, r = self.run_script(a, b, EDIT, call("done", summary="x"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("you have this", last(f, 3), "nothing was found: a new pattern is a new question")

    def test_seen_a_frontier_run_loses_nothing_it_has_not_re_read(self):
        f, r = self.run_script(READ, EDIT, call("done", summary="x"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(sum("you have this" in json.dumps(b) for b in f.seen), 0)


def events(r):
    return [json.loads(l) for l in r.stdout.splitlines() if l.startswith("{")]


class ForcedEdit(CodeCase):
    def run_script(self, *script, extra=(), plan_=None):
        f = self.serve("anthropic", plan(plan_), *script, name="small")
        return f, self.code(SMALL_ASK, "--model", "small", "--small", *extra, check=False)

    def test_forced_edit_after_the_explore_budget_the_turn_is_an_edit_and_exploration_is_withheld(self):
        f, r = self.run_script(*SEARCHES, EDIT, call("done", summary="x"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("return a + b", self.calc())
        self.assertLessEqual(tool_names(f.seen[1]), {"read", "search", "run", "edit", "write", "done"})
        self.assertIn("search", tool_names(f.seen[8]), "eight explorations are within the budget")
        forced = f.seen[9]  # plan, eight searches, then the ninth turn
        self.assertTrue({"edit", "write"} <= tool_names(forced))
        self.assertFalse(tool_names(forced) & {"read", "search", "run", "card"}, tool_names(forced))
        self.assertIn("explor", last(f, 9), "the turn says why, and what was read is summarised")
        self.assertIn("calc.py", last(f, 9))

    def test_forced_edit_a_model_that_would_explore_forever_still_ends_with_an_edit(self):
        # a fake that answers every turn with a new search: only a forced turn can change what it does, and the
        # fake answers an edit when the tools it is offered have no search (as a model must)
        f, r = self.run_script(*SEARCHES, EDIT, call("done", summary="x"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(f.seen), 1 + 8 + 2)
        self.assertEqual(self.task()["status"], "done")

    def test_forced_edit_prose_on_the_forced_turn_asks_again_for_the_edit(self):
        f, r = self.run_script(*SEARCHES, {"text": "Let me look at it once more."}, EDIT, call("done", summary="x"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(tool_names(f.seen[10]) & {"read", "search"}, "still forced after prose")
        self.assertIn("return a + b", self.calc())

    def test_forced_edit_an_edit_that_does_not_apply_is_told_and_the_loop_goes_on(self):
        bad = call("edit", file="calc.py", old="a * b", new="a + b")
        f, r = self.run_script(*SEARCHES, bad, EDIT, call("done", summary="x"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("error", last(f, 10).lower(), "the failed edit's error goes back")
        self.assertIn("return a + b", self.calc())

    def test_forced_edit_one_named_missing_fact_gets_one_targeted_look(self):
        miss = call("missing", fact="what test_calc.py asserts")
        look = call("read", path="test_calc.py")
        f, r = self.run_script(*SEARCHES, miss, look, EDIT, call("done", summary="x"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("missing", tool_names(f.seen[9]))
        self.assertTrue(tool_names(f.seen[10]) <= {"read", "search"}, "one targeted look")
        again = f.seen[11]
        self.assertFalse(tool_names(again) & {"read", "search", "missing"}, "then the edit is forced, no second ask")
        self.assertIn("return a + b", self.calc())

    def test_forced_edit_a_research_step_with_nothing_to_edit_is_never_forced(self):
        ask = self.serve("anthropic", {"text": json.dumps({"answer": "it subtracts"})}, *SEARCHES,
                         call("search", pattern="x"), call("done", summary="calc.add subtracts"), name="small")
        r = self.code("what does calc.add do?", "--ask", "--model", "small", "--small", check=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(ask.seen), 1 + 9 + 1, "nine looks and the done: no turn was spent on a forced edit")
        self.assertTrue(all("search" in tool_names(b) for b in ask.seen[1:]), "a question's run is never forced to edit")

    def test_forced_edit_a_step_that_already_edited_is_not_forced(self):
        # T-4206: the budget restarts after an edit (it is a progress budget now); the first nine turns are free
        f, r = self.run_script(EDIT, *SEARCHES, call("done", summary="x"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(all("search" in tool_names(b) for b in f.seen[1:10]), "after an edit the budget restarts")


class Carry(CodeCase):
    """T-4177 review: what step 1 was shown is in step 2's prompt, bounded, the oldest facts dropped first."""
    def steps(self, n, *scripts):
        p = {"type": "FIX", "tier": "S", "title": "Make calc.add add",
             "criteria": [{"text": "calc.add adds", "verify": "python3 test_calc.py"}],
             "steps": [f"step {i}" for i in range(1, n + 1)]}
        f = self.serve("anthropic", plan(p), *scripts, name="small")
        return f, self.code(SMALL_ASK, "--model", "small", "--small", check=False)

    def prompt(self, f, i):
        return json.dumps(f.seen[i]["messages"][0])

    def test_carry_a_fact_noted_in_step_one_is_in_step_twos_prompt(self):
        f, r = self.steps(2, call("search", pattern="return"), EDIT, call("done", summary="x"),
                          call("done", summary="y"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("earlier steps", self.prompt(f, 1), "step 1 has nothing carried")
        step2 = self.prompt(f, 4)
        self.assertIn("earlier steps", step2)
        self.assertIn("search 'return'", step2)

    def test_carry_it_is_bounded_and_the_oldest_facts_go_first(self):
        pad = "x" * 100
        script = []
        for i in range(1, 5):  # four steps of six searches each, then a file written so the step may be done
            script += [call("search", pattern=f"q{i}{j}{pad}") for j in range(6)]
            script += [EDIT if i == 1 else call("write", file=f"n{i}.txt", content="x\n"), call("done", summary="x")]
        f, r = self.steps(5, *script, call("done", summary="x"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        seen = [i for i, b in enumerate(f.seen) if "earlier steps" in json.dumps(b["messages"][0])]
        last = self.prompt(f, seen[-1])
        self.assertIn(f"q45{pad}", last, "the newest fact is kept")
        self.assertNotIn(f"q10{pad}", last, "the oldest is dropped first")
        note = last.split("earlier steps", 1)[1]
        self.assertLessEqual(len(note), 1600 + 600, "about 400 tokens, not the whole history")


class Frontier(CodeCase):
    """T-4177 review 2: the explore budget, the forced edit, the `missing` tool and the carried notes are for a guided
    run: --small, a profile marked small, an own profile, or a local (loopback) endpoint, which is where the read-forever
    loop was seen (jarvis, ollama: small is unset there). A cloud profile explores freely and only gets the seen-set note."""
    def cloud(self, name):
        """The fake's profile moved to 0.0.0.0, which connects here but is no loopback address: a remote host to the gate."""
        path = os.path.join(self.home, "models.json")
        with open(path) as fh:
            conf = json.load(fh)
        conf["profiles"][name]["base_url"] = conf["profiles"][name]["base_url"].replace("127.0.0.1", "0.0.0.0")
        with open(path, "w") as fh:
            json.dump(conf, fh)

    def test_guided_a_local_profile_without_small_is_forced_after_the_budget(self):
        f = self.serve("anthropic", plan(), *SEARCHES, EDIT, call("done", summary="x"), name="jarvis")
        r = self.code(SMALL_ASK, "--model", "jarvis", "--json", check=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(tool_names(f.seen[9]) & {"read", "search"}, "the budget ran out on a local endpoint")
        self.assertIn("missing", tool_names(f.seen[9]))
        self.assertTrue([e for e in events(r) if e["event"] == "forced"])
        self.assertIn("return a + b", self.calc())

    def test_guided_an_own_profile_is_guided_whatever_its_host(self):
        self.assertTrue(smith.guided_profile({"kind": "openai", "base_url": "https://llm.example.net/v1", "own": True}))
        self.assertTrue(smith.guided_profile({"kind": "openai", "base_url": "http://localhost:11434/v1"}))
        self.assertTrue(smith.guided_profile({"kind": "openai", "base_url": "https://x.example.net", "small": True}))

    def test_guided_a_cloud_profile_is_not(self):
        self.assertFalse(smith.guided_profile({"kind": "anthropic", "base_url": "https://api.example.net/v1"}))
        self.assertFalse(smith.guided_profile({"kind": "cli"}))
        self.assertFalse(smith.guided_profile({"kind": "cli", "base_url": "http://127.0.0.1:1/v1"}), "the CLI is cloud")
        self.assertFalse(smith.guided_profile({"kind": "anthropic"}), "no host is no local endpoint")

    def test_frontier_a_run_that_explores_twenty_times_is_never_forced(self):
        looks = [call("search", pattern=f"zz{i}") for i in range(20)]
        f = self.serve("anthropic", plan(), *looks, EDIT, call("done", summary="x"), name="big")
        self.cloud("big")
        r = self.code("fix add", "--model", "big", "--json", check=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(f.seen), 1 + 20 + 2)
        self.assertTrue(all("search" in tool_names(b) for b in f.seen[1:22]), "search was offered on every turn")
        self.assertFalse(any("Exploring is over" in json.dumps(b) for b in f.seen))
        self.assertEqual([e for e in events(r) if e["event"] == "forced"], [])
        self.assertIn("return a + b", self.calc())

    def test_frontier_a_profile_marked_small_is_forced(self):
        f = self.serve("anthropic", plan(), *SEARCHES, EDIT, call("done", summary="x"), name="big")
        path = os.path.join(self.home, "models.json")
        with open(path) as fh:
            conf = json.load(fh)
        conf["profiles"]["big"]["small"] = True
        with open(path, "w") as fh:
            json.dump(conf, fh)
        self.cloud("big")
        r = self.code(SMALL_ASK, "--model", "big", check=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(tool_names(f.seen[9]) & {"read", "search"}, "the profile's small turned the budget on")

    def test_frontier_a_repeat_still_gets_the_seen_note(self):
        f = self.serve("anthropic", plan(), READ, READ, EDIT, call("done", summary="x"), name="big")
        self.cloud("big")
        r = self.code("fix add", "--model", "big", check=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertGreaterEqual(sum("you have this" in json.dumps(b) for b in f.seen), 1)


class Scaffold(CodeCase):
    def test_forced_edit_scaffold_a_red_test_step_gets_the_verify_command_and_the_nearest_test(self):
        p = {"type": "FIX", "tier": "S", "title": "Make calc.add add",
             "criteria": [{"text": "calc.add adds", "verify": "python3 test_calc.py"}],
             "steps": ["write the failing test for calc.add", "fix calc.add"]}
        f = self.serve("anthropic", plan(p), call("write", file="test_new.py", content="print('x')\n"),
                       call("done", summary="x"), EDIT, call("done", summary="x"), name="small")
        r = self.code(SMALL_ASK, "--model", "small", "--small", check=False)
        first = json.dumps(f.seen[1])
        self.assertIn("Scaffold for the failing test", first)
        self.assertIn("python3 test_calc.py", first)
        self.assertIn("assert calc.add(2, 3) == 5", first, "the nearest test's lines are the template")
        self.assertNotIn("Scaffold", json.dumps(f.seen[3]), "only the red-test step is scaffolded")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class Stats(CodeCase):
    def stats(self, *script):
        self.serve("anthropic", plan(), *script, name="small")
        r = self.code("fix add", "--model", "small", "--json", check=False)
        got = [e for e in events(r) if e["event"] == "stats"]
        self.assertEqual(len(got), 1, r.stdout + r.stderr)
        return got[0]

    def test_stats_reread_ratio_and_turns_to_first_edit(self):
        s = self.stats(READ, READ, call("read", path="test_calc.py"), EDIT, call("done", summary="x"))
        self.assertEqual((s["reads"], s["rereads"]), (3, 1))
        self.assertAlmostEqual(s["reread_ratio"], 1 / 3, places=2)
        self.assertEqual(s["turns_to_first_edit"], 4)
        self.assertEqual(s["notes"], 1, "one repeat was answered with a note")

    def test_stats_a_resumed_run_adds_up_what_the_first_run_counted(self):
        self.serve("anthropic", plan(), READ, READ, name="small")
        r = self.code(SMALL_ASK, "--model", "small", "--small", "--max-steps", "2", check=False)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertNotIn('"end"', journal_text(self))
        self.serve("anthropic", EDIT, call("done", summary="x"), name="small")
        r = self.code("--resume", "--max-steps", "6", "--json", check=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        got = [e for e in events(r) if e["event"] == "stats"]
        self.assertEqual(len(got), 1)
        s = got[0]
        self.assertEqual((s["reads"], s["rereads"], s["notes"]), (2, 1, 1), "the first run's reads are not lost")
        self.assertEqual((s["turns"], s["turns_to_first_edit"]), (4, 3))

    def test_stats_a_run_that_never_edits_says_so(self):
        s = self.stats(READ, call("done", summary="nothing to change"), call("done", summary="x"), call("done", summary="x"))
        self.assertIsNone(s["turns_to_first_edit"])
