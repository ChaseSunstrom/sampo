"""T-5516: `fm task dissent ID resolve '1,2,4' 'how'` answers several open objections in one call. The list is all or
nothing: one number that names no open objection refuses the whole call and changes nothing. One call records one
dissent event for the batch, not one per number."""
import json

from helpers import ForemanTestCase

from sampo import core as c


class DissentSeveral(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.tid = json.loads(self.fm("task", "new", "Fix login", "--type", "FIX", "--tier", "S", "--json").stdout)["id"]
        for text in ("cache is not cleared on logout", "token lacks expiry", "tests skip the timeout path", "typo in docs"):
            self.fm("task", "dissent", self.tid, "add", text)

    def open_numbers(self):
        out = json.loads(self.fm("task", "dissent", self.tid, "--json").stdout)
        return [x["n"] for x in out["open"]]

    def test_resolve_several_numbers_in_one_call(self):
        self.fm("task", "dissent", self.tid, "resolve", "1,2,4", "adopted: noted in the plan")
        self.assertEqual(self.open_numbers(), [3])  # numbers are stable: the objection left open is still 3
        brief = self.fm("task", "show", self.tid).stdout
        self.assertEqual(brief.count("answered: adopted: noted in the plan"), 3)

    def test_several_numbers_record_one_dissent_event(self):
        self.fm("task", "dissent", self.tid, "resolve", "1,2", "rejected: out of scope")
        events = [e for e in c.ledger_events(c.find_project(self.repo), kinds=["dissent"]) if self.tid in json.dumps(e)]
        self.assertEqual(len(events), 5, "four adds and one batch resolve")

    def test_one_unknown_number_refuses_the_whole_list(self):
        p = self.fm("task", "dissent", self.tid, "resolve", "1,9", "rejected: out of scope", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(self.open_numbers(), [1, 2, 3, 4], "nothing was answered by the refused call")

    def test_a_single_number_still_works(self):
        self.fm("task", "dissent", self.tid, "resolve", "3", "rejected: out of scope")
        self.assertEqual(self.open_numbers(), [1, 2, 4])

    # T-5516 item 9: an answered objection is not answered twice: the whole call is refused, and nothing stacks
    def test_an_answered_objection_is_refused_not_answered_again(self):
        self.fm("task", "dissent", self.tid, "resolve", "2", "adopted: noted")
        p = self.fm("task", "dissent", self.tid, "resolve", "2,3", "rejected: out of scope", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("already answered", p.stderr + p.stdout, "the refusal names the answered objection")
        brief = self.fm("task", "show", self.tid).stdout
        self.assertEqual(brief.count("— answered:"), 1, "no second answer stacked on the first")
        self.assertEqual(self.open_numbers(), [1, 3, 4], "the refused call changed nothing: 3 is still open")

    def test_an_answered_objection_alone_is_refused_too(self):
        self.fm("task", "dissent", self.tid, "resolve", "1", "adopted: noted")
        p = self.fm("task", "dissent", self.tid, "resolve", "1", "adopted: again", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(self.fm("task", "show", self.tid).stdout.count("— answered:"), 1)
