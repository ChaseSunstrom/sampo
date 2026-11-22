"""T-5133: carded task kinds. A card.json may carry "kinds", the yardstick task kinds the card serves, each suite:kind
(exam:bug, exam:vuln, exam:none, cases:TYPE); cardgap.carded_kinds(root) is their union over the loaded cards, Sampo's own
and the admitted store's and a SAMPO_CALLCARDS folder's. A card whose kinds entry is no suite:kind is left out, with why."""
import json
import os
import sys
from unittest import mock

from helpers import SampoTestCase

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
from sampo import callcards, cardgap  # noqa: E402

CODE_CARDS = ("failing-frame", "locate-function", "pick-function", "py-callers", "py-none-guard", "py-outline")
GEN = "import json, sys\njson.load(sys.stdin)\n"
SCHEMA = {"type": "object", "properties": {}}


class Kinds(SampoTestCase):
    def setUp(self):
        super().setUp()
        self.cards_dir = os.path.join(self.tmp, "extra")
        os.makedirs(self.cards_dir)
        env = mock.patch.dict(os.environ, {"SAMPO_CALLCARDS": self.cards_dir, "FOREMAN_NO_LOCAL_MODELS": "1"})
        env.start()
        self.addCleanup(env.stop)

    def card(self, name, base=None, **spec):
        d = os.path.join(base or self.cards_dir, name)
        os.makedirs(d)
        spec = {"name": name, "purpose": f"{name}: reads a file", "examples": ["read it"], "kind": "py", "read": True,
                "inputs": SCHEMA, "files": [], **spec}
        with open(os.path.join(d, "card.json"), "w") as f:
            json.dump(spec, f)
        for fn in ("gen.py", "check.py"):
            with open(os.path.join(d, fn), "w") as f:
                f.write(GEN)
        return d

    def test_union_over_the_temp_root(self):
        self.card("one-card", kinds=["cases:refactor", "exam:vuln"])
        self.card("two-card", kinds=["cases:refactor", "cases:docs"])
        got = cardgap.carded_kinds(self.repo)
        self.assertTrue({"cases:refactor", "exam:vuln", "cases:docs"} <= got, got)
        self.assertEqual(cardgap.report(self.repo)["by_kind"]["cases:refactor"], ["one-card", "two-card"])

    def test_the_admitted_store_counts(self):
        os.makedirs(callcards.authored_dir(), exist_ok=True)
        self.card("stored-card", base=callcards.authored_dir(), kinds=["exam:none"])
        self.assertIn("exam:none", cardgap.carded_kinds(self.repo))
        self.assertEqual(cardgap.report(self.repo)["by_kind"]["exam:none"], ["stored-card"])

    def test_a_card_without_kinds_adds_none(self):
        before = cardgap.carded_kinds(self.repo)
        self.card("plain-card")
        self.assertEqual(cardgap.carded_kinds(self.repo), before)
        self.assertNotIn("plain-card", cardgap.report(self.repo)["left_out"])

    def test_a_bad_entry_leaves_the_card_out_with_its_reason(self):
        for name, kinds in (("no-suite", ["bug"]), ("bad-suite", ["exam:bug", "lint:style"]), ("bad-exam", ["exam:mystery"]),
                            ("empty-type", ["cases:"]), ("caps-type", ["cases:Refactor"]), ("not-text", ["exam:bug", 7]),
                            ("not-list", "exam:bug"), ("two-colons", ["cases:a:b"])):
            self.card(name, kinds=kinds)
        got = cardgap.report(self.repo)
        for name in ("no-suite", "bad-suite", "bad-exam", "empty-type", "caps-type", "not-text", "not-list", "two-colons"):
            self.assertIn(name, got["left_out"], name)
            self.assertTrue(got["left_out"][name], name)
            for kind_cards in got["by_kind"].values():
                self.assertNotIn(name, kind_cards)  # all of a bad card's kinds go, the good one beside the bad too
        self.assertIn("lint:style", got["left_out"]["bad-suite"])
        self.assertIn("exam:mystery", got["left_out"]["bad-exam"])
        self.assertNotIn("cases:Refactor", cardgap.carded_kinds(self.repo))

    def test_a_card_that_does_not_load_adds_none(self):
        d = self.card("broken-card", kinds=["cases:ghost"])
        os.remove(os.path.join(d, "gen.py"))
        self.assertNotIn("cases:ghost", cardgap.carded_kinds(self.repo))

    def test_a_repo_folder_in_the_env_is_no_store(self):
        inner = os.path.join(self.repo, "cardsrc")
        os.makedirs(inner)
        self.card("inside-card", base=inner, kinds=["cases:inside"])
        with mock.patch.dict(os.environ, {"SAMPO_CALLCARDS": inner}):
            self.assertNotIn("cases:inside", cardgap.carded_kinds(self.repo))

    def test_the_six_code_cards_name_exam_bug(self):
        got = cardgap.report(None)
        for name in CODE_CARDS:
            self.assertIn(name, got["by_kind"].get("exam:bug", []), name)
            self.assertNotIn(name, got["left_out"], name)
        self.assertIn("exam:bug", cardgap.carded_kinds(None))

    def test_no_shipped_card_is_left_out(self):
        self.assertEqual(cardgap.report(None)["left_out"], {})
