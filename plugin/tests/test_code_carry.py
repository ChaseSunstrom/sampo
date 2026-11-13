"""T-4493: sampo code hands a step the files earlier steps read. On the hard exam 15 to 24 reads a run were the first
read in a step of a file an earlier step had already read: each step is its own conversation, and the notes carried
between them hold outlines, not text. On an unpriced profile with a known context the next step's first message now
carries those files as they are at that moment, the newest read first, inside a quarter of the context's room; a read
of one that has not changed since is answered with the you-have-this note. A priced profile keeps the outlines only."""
import json
import os

from test_code import CodeCase, call, plan
from test_code_loop_break import CODE, EDIT, READ, last

from sampo import smith

FREE = {"name": "free", "kind": "openai", "context": 64000}
ROOM = 64000 - smith.MAX_TOKENS
TWO = {"type": "FIX", "tier": "S", "title": "Make calc.add add",
       "criteria": [{"text": "calc.add adds", "verify": "python3 test_calc.py"}],
       "steps": ["note what calc.py holds", "fix calc.add"]}
NOTE = call("write", file="notes.txt", content="calc.add subtracts\n")
DONE = call("done", summary="x")


class Carry(CodeCase):
    def run_object(self):
        run = smith.Run(type("A", (), {})(), self.repo, self.tmp)
        run.emit = lambda *a, **k: None
        run.own_endpoint = True
        run.cur = "step:1"
        return run

    def put(self, name, text):
        with open(os.path.join(self.repo, name), "w") as f:
            f.write(text)

    def test_carry_the_files_read_come_back_as_they_are_now_newest_read_first(self):
        run = self.run_object()
        self.put("b.py", "def b():\n    return 2\n")
        run.seen_read("calc.py", 1, 200)
        run.seen_read("b.py", 1, 200)
        self.put("calc.py", "def add(a, b):\n    return a + b\n")  # changed after it was read
        text = run.carried(2, FREE)
        self.assertIn("return a + b", text, "the text is the file's now, not what was read")
        self.assertNotIn(CODE, text)
        self.assertLess(text.index("b.py:"), text.index("calc.py:"), "the newest read first")
        self.assertIn("    1  def b():", text, "numbered as read numbers them")

    def test_carry_a_file_over_what_is_left_of_the_share_is_left_out_and_smaller_ones_still_fit(self):
        run = self.run_object()
        share = int(ROOM * smith.CARRY_SHARE) * 4
        self.put("big.py", "".join(f"x{i} = {'9' * 400}\n" for i in range(smith.READ_MAX)))  # 400 lines of 400+ chars
        self.assertGreater(os.path.getsize(os.path.join(self.repo, "big.py")), share)
        run.seen_read("calc.py", 1, 200)
        run.seen_read("big.py", 1, 200)
        text = run.carried(2, FREE)
        self.assertNotIn("big.py:", text)
        self.assertIn(CODE, text)
        self.assertEqual([os.path.basename(p) for p in run.seed], ["calc.py"])

    def test_carry_nothing_for_a_priced_profile_one_with_no_room_or_a_run_that_is_not_guided(self):
        for profile in (dict(FREE, price={"in": 1.0, "out": 2.0}), {"name": "bare", "kind": "openai"}, None):
            run = self.run_object()
            run.seen_read("calc.py", 1, 200)
            self.assertNotIn(CODE, run.carried(2, profile))
            self.assertEqual(run.seed, {})
        run = self.run_object()
        run.own_endpoint = False
        run.seen_read("calc.py", 1, 200)
        self.assertEqual(run.carried(2, FREE), "")

    def test_carry_a_deleted_file_a_folder_and_a_refused_path_are_passed_over(self):
        run = self.run_object()
        self.put("gone.py", "x = 1\n")
        run.seen_read("gone.py", 1, 200)
        run.seen_read(".", 1, 200)
        run.seen_read("calc.py", 1, 200)
        os.unlink(os.path.join(self.repo, "gone.py"))
        text = run.carried(2, FREE)
        self.assertNotIn("gone.py", text.split("The files earlier steps read")[1])
        self.assertEqual([os.path.basename(p) for p in run.seed], ["calc.py"])

    def test_carry_a_document_the_step_gets_as_the_specification_is_not_sent_twice(self):
        run = self.run_object()
        self.put("SPEC.md", "calc.add returns the sum.\n")
        run.spec = ["SPEC.md"]
        run.seen_read("SPEC.md", 1, 200)
        run.seen_read("calc.py", 1, 200)
        text = run.carried(2, FREE)
        self.assertNotIn("returns the sum", text)
        self.assertIn(CODE, text)

    def test_carry_what_the_survey_read_is_not_carried(self):
        run = self.run_object()
        run.cur = "survey"
        run.seen_read("calc.py", 1, 200)
        self.assertEqual(run.read_at, {})
        self.assertNotIn(CODE, run.carried(1, FREE))


class SeenCarried(CodeCase):
    def run_script(self, *script, price=None, context=64000):
        f = self.serve("anthropic", plan(TWO), *script, name="small")
        path = os.path.join(self.home, "models.json")
        with open(path) as fh:
            conf = json.load(fh)
        conf["profiles"]["small"]["context"] = context
        if price:
            conf["profiles"]["small"]["price"] = price
        with open(path, "w") as fh:
            json.dump(conf, fh)
        return f, self.code("fix add", "--model", "small", check=False)

    def test_seen_carried_the_second_step_starts_with_the_file_and_a_read_of_it_is_a_note(self):
        f, r = self.run_script(READ, NOTE, DONE, READ, EDIT, DONE)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        first = json.dumps(f.seen[4]["messages"][:2])
        self.assertIn("The files earlier steps read", first)
        self.assertIn(CODE, first, "step 2's first message holds calc.py")
        self.assertIn("you have this", last(f, 5))
        self.assertNotIn(CODE, last(f, 5))

    def test_seen_carried_a_file_edited_in_the_step_is_read_afresh(self):
        f, r = self.run_script(READ, NOTE, DONE, EDIT, READ, DONE)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("return a + b", last(f, 6), "changed since it was carried: the text, not a note")
        self.assertNotIn("you have this", last(f, 6))

    def test_seen_carried_a_priced_model_gets_the_outlines_only_and_reads_the_file_itself(self):
        f, r = self.run_script(READ, NOTE, DONE, READ, EDIT, DONE, price={"in": 3.0, "out": 15.0})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        first = json.dumps(f.seen[4]["messages"][:2])
        self.assertNotIn("The files earlier steps read", first)
        self.assertNotIn(CODE, first)
        self.assertIn(CODE, last(f, 5))
