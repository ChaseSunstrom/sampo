"""T-4490: sampo code keeps what the model read. On the hard exam a local model's step read one test file 22 times in
an hour: only the last six tool results were sent whole, so what it read seven actions ago was a one-line stub, and each
turn's new stub moved the prefix the model server had cached. An unpriced profile now keeps every result whole while
the sent text is under 70% of the context's room and then cuts one batch, stale results first, down to 45%; what is cut
stays cut. A priced profile keeps the last-six rule."""
import json
import os

from test_code import CodeCase, call, plan
from test_code_loop_break import CODE, EDIT, READ, SEARCHES, last

from sampo import smith

FREE = {"name": "free", "kind": "openai", "context": 64000}  # room: 64000 less the 8192 output reserve = 55808
ROOM = 64000 - smith.MAX_TOKENS
PRICED = dict(FREE, price={"in": 1.0, "out": 2.0})


def conv(n, tokens=1000, kind="read"):
    """A phase's conversation with n results of about `tokens` tokens each (chars / 4, as the harness counts)."""
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "plan"}]
    for i in range(n):
        msgs += [{"role": "assistant", "content": f"action {i}"},
                 {"role": "user", "content": f"Result of {kind}:\nresult {i}\n" + "x" * (tokens * 4)}]
    return msgs


def results(view):
    return [x["content"] for x in view if smith._is_result(x) or x["content"].startswith("(older result cut")]


def whole(view):
    return [int(t.split("result ")[1].split("\n")[0]) for t in results(view) if not t.startswith("(older result cut")]


class Sight(CodeCase):
    def run_object(self):
        run = smith.Run(type("A", (), {})(), self.repo, self.tmp)
        run.emit = lambda *a, **k: self.notes.append(a[1] if len(a) > 1 else "")
        return run

    def setUp(self):
        super().setUp()
        self.notes = []

    def test_sight_everything_is_whole_while_the_text_is_under_the_room(self):
        run, msgs = self.run_object(), conv(30)  # 30,000 tokens of 55,808: under 70%
        self.assertEqual(run.sight("build", msgs, FREE), msgs)
        self.assertEqual(self.notes, [])

    def test_sight_past_70_percent_one_batch_is_cut_oldest_first_down_to_45(self):
        run, msgs = self.run_object(), conv(45)  # 45,000 tokens: over 39,065
        view = run.sight("build", msgs, FREE)
        self.assertEqual(len(view), len(msgs))
        self.assertLessEqual(smith._tokens(view), ROOM * smith.SIGHT_LOW)
        kept = whole(view)
        self.assertEqual(kept, list(range(45 - len(kept), 45)), "the oldest went, the rest are whole")
        self.assertGreaterEqual(len(kept), 24, "no more is cut than the low mark asks")
        self.assertIn("x" * 4000, msgs[3]["content"], "the stored conversation is untouched")
        self.assertEqual(len(self.notes), 1)
        self.assertIn(f"{45 - len(kept)} older result(s) cut", self.notes[0])

    def test_sight_prefix_is_the_same_bytes_between_batches(self):
        run, msgs = self.run_object(), conv(45)
        first = run.sight("build", msgs, FREE)
        more = msgs + conv(5)[2:]  # five more results arrive: still under the high mark
        second = run.sight("build", more, FREE)
        self.assertEqual(json.dumps(second[:len(first)]), json.dumps(first), "what was sent before is sent again as it was")
        self.assertEqual(second[len(first):], more[len(first):], "and the new results are whole")
        self.assertEqual(len(self.notes), 1, "no second batch")

    def test_sight_prefix_cut_results_stay_cut_in_a_later_batch(self):
        run, msgs = self.run_object(), conv(45)
        first = run.sight("build", msgs, FREE)
        more = msgs + conv(20)[2:]
        second = run.sight("build", more, FREE)  # over the high mark again: a second batch
        self.assertEqual(len(self.notes), 2)
        stubs = [i for i, x in enumerate(first) if x["content"].startswith("(older result cut")]
        self.assertTrue(all(second[i] == first[i] for i in stubs))
        self.assertLessEqual(smith._tokens(second), ROOM * smith.SIGHT_LOW)

    def test_sight_the_newest_six_are_never_cut(self):
        run, msgs = self.run_object(), conv(8, tokens=20000)
        view = run.sight("build", msgs, FREE)
        self.assertEqual(whole(view), [2, 3, 4, 5, 6, 7], "over the room still: fit() is the backstop")

    def test_sight_stale_reads_go_first_then_other_results_then_live_reads(self):
        run, msgs = self.run_object(), conv(40)  # 40,000 tokens: just over the high mark
        with open(os.path.join(self.repo, "a.py"), "w") as f:
            f.write("new text\n")
        full = os.path.join(self.repo, "a.py")
        calc = os.path.join(self.repo, "calc.py")
        ran = 2 + 2 * 20 + 1  # result 20 is a run's output, not a read
        msgs[ran] = dict(msgs[ran], content=msgs[ran]["content"].replace("Result of read", "Result of run"))
        live = [msgs[2 + 2 * i + 1]["content"] for i in range(40) if i not in (20, 30)]
        run.shown["reads"] = {
            full: [{"digest": "what it held before the edit", "start": 1, "end": 9, "msg": msgs[2 + 2 * 30 + 1]["content"]}],
            calc: [{"digest": run.digest(calc), "start": 1, "end": 9, "msg": t} for t in live]}
        view = run.sight("build", msgs, FREE)
        gone = [i for i in range(40) if i not in whole(view)]
        self.assertEqual(gone[-2:], [20, 30], "the read of the changed file and the run's output are cut")
        self.assertEqual(gone[:-2], list(range(len(gone) - 2)), "then the live reads, oldest first")
        self.assertIn("1 of files changed since", self.notes[0])

    def test_sight_a_priced_profile_and_one_with_no_room_keep_the_last_six(self):
        for profile in (PRICED, {"name": "bare", "kind": "openai"}, {"name": "zero", "kind": "openai", "context": 0}):
            run, msgs = self.run_object(), conv(12)
            self.assertEqual(run.sight("build", msgs, profile), smith.trim(msgs), profile["name"])
            self.assertFalse(run.roomy)

    def test_sight_holds_reaches_as_far_as_the_text_is_sent(self):
        run, msgs = self.run_object(), conv(12)
        run.cur, run.msgs = "build", {"build": msgs}
        old = {"msg": msgs[3]["content"]}
        run.sight("build", msgs, FREE)
        self.assertTrue(run.holds(old), "eleven results back, still sent whole")
        run.cut = {msgs[3]["content"]}
        self.assertFalse(run.holds(old), "cut by the last send")
        run.cut = set()
        run.sight("build", msgs, PRICED)
        self.assertFalse(run.holds(old), "the last-six rule: past its reach")


class SeenFar(CodeCase):
    def run_script(self, *script, price=None):
        f = self.serve("anthropic", plan(), *script, name="small")
        if price:
            path = os.path.join(self.home, "models.json")
            with open(path) as fh:
                conf = json.load(fh)
            conf["profiles"]["small"]["price"] = price
            with open(path, "w") as fh:
                json.dump(conf, fh)
        return f, self.code("fix add", "--model", "small", check=False)

    def test_seen_far_a_reread_seven_actions_later_is_still_a_note(self):
        f, r = self.run_script(READ, *SEARCHES[:6], READ, EDIT, call("done", summary="x"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("you have this", last(f, 9), "the first read is still in what was sent")
        self.assertNotIn(CODE, last(f, 9))
        self.assertIn(CODE, json.dumps(f.seen[9]["messages"]), "whole, seven results back")

    def test_seen_far_a_priced_model_is_sent_the_text_again(self):
        f, r = self.run_script(READ, *SEARCHES[:6], READ, EDIT, call("done", summary="x"), price={"in": 3.0, "out": 15.0})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(CODE, last(f, 9), "cut from the conversation by the last-six rule: sent whole")
        self.assertNotIn("you have this", last(f, 9))
