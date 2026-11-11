"""T-4206: in a guided run (a small or own-endpoint model) a step can no longer be spent looking. A `run` that changed no
file and ran no test counts as a look, judged by its effect; the budget keeps running after an edit as a progress
budget (the forced turn is 'run the test', then 'edit the source'); and when the turns run out with edits in the tree
the run carries on once by itself. T-4151 is the shape: two test edits, then thirteen runs that were greps and ls."""
import json
import os

from test_code import CodeCase, call, plan

SMALL_ASK = "do fix add"  # opens with a verb, not a question word: no read-only look before the plan
TEST_EDIT = call("edit", file="test_calc.py", old="print('ok')", new="assert calc.add(0, 0) == 0\nprint('ok')")
SRC_EDIT = call("edit", file="calc.py", old="a - b", new="a + b")
VERIFY = call("run", cmd="python3 test_calc.py")  # the plan's verify command: red until calc.py is fixed
LOOKS = ("ls", "cat calc.py", "grep -n add calc.py", "pwd", "ls -a", "head -3 calc.py", "grep -c def calc.py",
         "python3 -c \"print(1)\"", "echo a", "echo b", "echo c", "echo d", "echo e", "echo f", "echo g", "echo h")


def looks(n, start=0):
    return [call("run", cmd=LOOKS[(start + i) % len(LOOKS)] + ("" if start + i < len(LOOKS) else f" #{start + i}"))
            for i in range(n)]


def names(body):
    return {t["name"] for t in body.get("tools") or []}


def events(r):
    return [json.loads(l) for l in r.stdout.splitlines() if l.startswith("{")]


def said(f, i):
    return json.dumps(f.seen[i]["messages"][-1])


class Budget(CodeCase):
    def cloud(self, name):
        """The fake's profile moved to 0.0.0.0, which connects here but is no loopback address: a remote host."""
        path = os.path.join(self.home, "models.json")
        with open(path) as fh:
            conf = json.load(fh)
        conf["profiles"][name]["base_url"] = conf["profiles"][name]["base_url"].replace("127.0.0.1", "0.0.0.0")
        with open(path, "w") as fh:
            json.dump(conf, fh)

    def go(self, *script, extra=(), name="small", cloud=False):
        f = self.serve("anthropic", plan(), *script, name=name)
        if cloud:
            self.cloud(name)
        r = self.code(SMALL_ASK, "--model", name, *(() if cloud else ("--small",)), "--json", *extra, check=False)
        return f, r

    def forced(self, r):
        return [e for e in events(r) if e["event"] == "forced"]

    def stats(self, r):
        got = [e for e in events(r) if e["event"] == "stats"]
        self.assertEqual(len(got), 1, r.stdout + r.stderr)
        return got[0]


class Progress(Budget):
    def test_progress_the_t4151_shape_is_forced_to_run_the_test_then_to_edit_the_source(self):
        # one test edit, eight runs that only look (the budget of an S plan), a forced turn, a red run, eight more
        # looks, a forced turn, the source edit, the verify that passes
        f, r = self.go(TEST_EDIT, *looks(8), VERIFY, *looks(8, 8), SRC_EDIT, VERIFY)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("run", names(f.seen[9]), "eight looks are within the budget")
        due = f.seen[10]  # plan, the test edit, eight looks, then the tenth request
        self.assertEqual(names(due), {"run"}, "after the edit the turn is the test run, nothing to look with")
        self.assertIn("python3 test_calc.py", said(f, 10), "the verify command to use is named")
        self.assertIn("is the test run", said(f, 10).lower())
        self.assertIn("run", names(f.seen[18]), "a test run is progress: the count starts again")
        src = f.seen[19]
        self.assertTrue({"edit", "write"} <= names(src))
        self.assertFalse(names(src) & {"read", "search", "run", "card"}, names(src))
        self.assertIn("edit of the source", said(f, 19).lower())
        self.assertIn("return a + b", self.calc())
        self.assertEqual(self.task()["status"], "done")
        got = [e["n"] for e in self.forced(r)]
        self.assertEqual(len(got), 2, "one forced turn each: run the test, edit the source")
        s = self.stats(r)
        self.assertEqual((s["forced_test"], s["forced_source"], s["forced_edit"], s["forced"]), (1, 1, 0, 2))

    def test_progress_a_failed_forced_turn_gives_half_the_budget_back_so_a_diagnosis_is_not_cut(self):
        # three forced turns that make no progress (they look again): half the budget is given back
        f, r = self.go(TEST_EDIT, *looks(8), *looks(3, 8), *looks(4, 11), SRC_EDIT, VERIFY)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(self.forced(r)), 3 + 1, "three forced turns, four looks of the half budget, a forced turn")
        self.assertEqual(names(f.seen[12]), {"run"}, "the third forced turn")
        self.assertIn("search", names(f.seen[13]), "the half budget is a real chance to look")
        self.assertEqual(names(f.seen[17]), {"run"}, "four looks spent it again")

    def test_progress_a_pre_edit_step_is_forced_to_edit_as_before(self):
        f, r = self.go(*looks(8), SRC_EDIT, VERIFY)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(f.seen), 1 + 8 + 2)
        self.assertTrue({"edit", "write"} <= names(f.seen[9]))
        self.assertFalse(names(f.seen[9]) & {"read", "search", "run"}, "no verify to run before an edit: the edit")
        self.assertEqual(self.stats(r)["forced_edit"], 1)


class Looking(Budget):
    def test_looking_runs_count_against_the_budget_before_any_edit(self):
        f, r = self.go(*looks(8), SRC_EDIT, VERIFY)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("run", names(f.seen[8]), "seven looks and the eighth: still within the budget")
        self.assertFalse(names(f.seen[9]) & {"read", "search", "run"}, "eight looking runs spent it")
        s = self.stats(r)
        self.assertEqual(s["look_runs"], 8)
        self.assertEqual(s["looks"], 8)

    def test_looking_a_run_that_changes_a_file_does_not_count(self):
        touches = [call("run", cmd=f"touch made{i}.txt") for i in range(12)]
        f, r = self.go(*touches, SRC_EDIT, VERIFY)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(all("run" in names(b) for b in f.seen[1:13]), "twelve runs that wrote a file: never forced")
        self.assertEqual(self.forced(r), [])
        self.assertEqual(self.stats(r)["look_runs"], 0)

    def test_looking_a_run_of_the_test_after_an_edit_resets_the_count(self):
        f, r = self.go(TEST_EDIT, *looks(7), VERIFY, *looks(7, 7), SRC_EDIT, VERIFY)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.forced(r), [], "seven looks, the test, seven looks: neither stretch reaches the budget")
        self.assertTrue(all("run" in names(b) for b in f.seen[1:17]))

    def test_looking_an_edit_that_applies_resets_the_count(self):
        f, r = self.go(*looks(7), TEST_EDIT, *looks(7, 7), SRC_EDIT, VERIFY)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.forced(r), [])

    def test_looking_the_same_test_run_again_with_nothing_edited_is_a_look(self):
        # a red run, then the same tests again and again: with no edit between them nothing was tested that was not
        again = [call("run", cmd="python3 test_calc.py" + tail) for tail in (" 2>&1", " 2>&1 | head -3", " -v", " -q",
                                                                            " -b", " -u", " -O", " -B")]
        f, r = self.go(TEST_EDIT, VERIFY, *again, SRC_EDIT, VERIFY)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(self.forced(r)), 1, "the first run was progress, the repeats were looks")
        self.assertEqual(self.stats(r)["look_runs"], 8)

    def test_looking_a_read_and_a_search_count_as_before(self):
        mix = [call("read", path="calc.py"), call("search", pattern="add")] * 4
        f, r = self.go(*mix, SRC_EDIT, VERIFY)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(names(f.seen[9]) & {"read", "search", "run"})


class Untouched(Budget):
    def test_untouched_a_cloud_run_explores_freely_after_an_edit(self):
        f, r = self.go(TEST_EDIT, *looks(14), SRC_EDIT, VERIFY, name="big", cloud=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.forced(r), [])
        self.assertTrue(all("run" in names(b) and "search" in names(b) for b in f.seen[1:17]))

    def test_untouched_a_plan_that_changes_nothing_is_not_forced(self):
        research = dict(type="RESEARCH", tier="S", title="Why add subtracts", criteria=[], steps=["find out"])
        f = self.serve("anthropic", plan(research), *looks(12), call("done", summary="a - b by mistake"), name="small")
        r = self.code(SMALL_ASK, "--model", "small", "--small", "--json", check=False)
        self.assertEqual(self.forced(r), [], r.stdout + r.stderr)


class Carry(Budget):
    def test_carry_turns_running_out_with_edits_in_the_tree_carries_on_once(self):
        # the plan is turn 1, the edit turn 2, and --max-steps 6 ends the step at turn 6 with the fix not yet checked
        f, r = self.go(SRC_EDIT, *looks(3), call("search", pattern="add"), call("read", path="calc.py"), VERIFY,
                       extra=("--max-steps", "6"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("carrying on", r.stdout + r.stderr)
        self.assertEqual(self.stats(r)["carried"], 1)
        self.assertEqual(self.task()["status"], "done")

    def test_carry_the_second_run_out_stops_as_before_and_says_what_is_left(self):
        f, r = self.go(SRC_EDIT, *looks(40), extra=("--max-steps", "6"))
        self.assertNotEqual(r.returncode, 0)
        out = r.stdout + r.stderr
        self.assertEqual(out.count("carrying on"), 1, "once, never twice")
        self.assertIn("used all", out)
        self.assertEqual(self.stats(r)["carried"], 1)
        self.assertIn("return a + b", self.calc(), "the edit stays in the tree for the resume")

    def test_carry_a_run_with_no_edit_stops_at_the_cap(self):
        f, r = self.go(*looks(8), extra=("--max-steps", "6"))
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("carrying on", r.stdout + r.stderr)
        self.assertEqual(self.stats(r)["carried"], 0)

    def test_carry_a_cloud_run_stops_at_the_cap_as_before(self):
        f, r = self.go(SRC_EDIT, *looks(10), name="big", cloud=True, extra=("--max-steps", "6"))
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("carrying on", r.stdout + r.stderr)
