"""Spec suite, round 4 (T-3340): a work queue on ledger, for the Sampo-vs-plain-Claude-Code benchmark. Data only;
make.py --suite queue builds it.

One base commit (round 2's ledger plus ledger/numbering.py and its flaky test, and QUEUE.md: the 12 requests, in
order). An arm works the whole queue unattended. Each task has a HIDDEN test file, tests/test_q_<name>.py, which
make.py writes outside the arm's tree (a fresh temp folder, or --hidden DIR2; never inside, around or beside the repo),
and the runner copies one onto the arm's final tree only after the arm has finished. A task passes when its hidden file passes on the
arm's final tree. Hidden files are self-contained (they also check each "Keep:" rule), so tasks grade independently
whatever the order or the other tasks' outcome.

Kinds: keep (a shared helper's change must not alter another caller), dense (a long edge-case spec), flaky (a test
that fails now and then), migration (an API change that touches every caller), ambiguous (underspecified but any sane
reading passes: the right move is to proceed, not to ask), impossible (rules that contradict: the right answer is
"can't"; the hidden file fails for every attempt, and the judge reads the arm's answer).

A task's "files" are source edits on the base tree (each path to its whole text, or [(old, new)] edits, each old
found exactly once); "naive" is the plausible shortcut ("what"), as files on the base, which the hidden file must
fail. make.py --check proves all of it.
"""
import hard

VERIFY = "python3 -m unittest discover -s tests"  # the arm's own suite; grading is the hidden file, run on top of it

NUMBERING = r'''
"""Invoice numbers."""
import random


def next_number(prefix="INV"):
    """A new invoice number, "<prefix>-<n>" with n at least four digits."""
    return f"{prefix}-{random.randint(1, 400):04d}"
'''

NUMBERING_TEST = r'''
import unittest

from ledger.numbering import next_number


class NumberingTest(unittest.TestCase):
    def test_numbers_have_the_prefix_and_four_digits(self):
        self.assertRegex(next_number(), r"^INV-[0-9]{4}$")
        self.assertRegex(next_number("CN"), r"^CN-[0-9]{4}$")

    def test_numbers_are_unique(self):
        numbers = [next_number() for _ in range(6)]
        self.assertEqual(len(set(numbers)), len(numbers))


if __name__ == "__main__":
    unittest.main()
'''

_H = {t["name"]: t for t in hard.TASKS}


def _src(name):
    """The hard task's source edits (its test edits left out: the queue's hidden tests are standalone files)."""
    return {p: c for p, c in _H[name]["files"].items() if not p.startswith("tests/")}


def _naive(name):
    """The hard task's naive files; edits to a file the task's solution creates are applied to that text (the base
    doesn't have the file)."""
    out = {}
    for p, c in _H[name]["naive"]["files"].items():
        sol = _H[name]["files"].get(p)
        if isinstance(c, list) and isinstance(sol, str):
            c = sol
            for old, new in _H[name]["naive"]["files"][p]:
                assert sol.count(old) == 1, (name, p)
                c = c.replace(old, new)
        out[p] = c
    return out


def _hard_test(name, path):
    return _H[name]["files"][path]


def _hard_message(name):
    return _H[name]["message"]


_PAD_OLD = '''def pad(text, width, align="left"):
    """text padded with spaces to width, "left", "right" or "center" aligned; a longer text is returned whole."""
    if align == "left":
        return text.ljust(width)
    if align == "right":
        return text.rjust(width)
    if align == "center":
        return text.center(width)
    raise ValueError(f"align must be left, right or center, got {align!r}")
'''
_PAD_NEW = '''class Align(enum.Enum):
    LEFT = "left"
    RIGHT = "right"
    CENTER = "center"


LEFT, RIGHT, CENTER = Align.LEFT, Align.RIGHT, Align.CENTER


def pad(text, width, align=LEFT):
    """text padded with spaces to width, aligned LEFT, RIGHT or CENTER; a longer text is returned whole. The strings
    "left", "right" and "center" still work but are deprecated: they raise a DeprecationWarning."""
    if isinstance(align, str):
        try:
            align = Align(align)
        except ValueError:
            raise ValueError(f"align must be LEFT, RIGHT or CENTER, got {align!r}") from None
        warnings.warn("text.pad: pass text.LEFT, text.RIGHT or text.CENTER, not a string", DeprecationWarning,
                      stacklevel=2)
    if align is LEFT:
        return text.ljust(width)
    if align is RIGHT:
        return text.rjust(width)
    if align is CENTER:
        return text.center(width)
    raise ValueError(f"align must be LEFT, RIGHT or CENTER, got {align!r}")
'''
_PAD_TEXT = [("import re\nimport unicodedata\n", "import enum\nimport re\nimport unicodedata\nimport warnings\n"),
             (_PAD_OLD, _PAD_NEW)]

TASKS = [
    {
        "name": "grouping", "kind": "keep",
        "message": _hard_message("grouping"),
        "files": _src("grouping"),
        "naive": {"what": "format_amount itself groups thousands", "files": _H["grouping"]["naive"]["files"]},
        "tests": r'''
import datetime
import unittest

from ledger.export import to_csv
from ledger.invoice import Invoice, Line, render
from ledger.money import format_amount
from ledger.report import render_summary


def big():
    return Invoice("INV-0009", "Big Co", datetime.date(2026, 2, 1), [
        Line("Server", 2, 123456), Line("Refund", 1, -100000), Line("Tea", 1, 99999), Line("Pin", 1, 5),
        Line("Gold", 1, 123456789012)])


class GroupingTest(unittest.TestCase):
    def test_render_groups_thousands(self):
        out = render(big())
        self.assertIn("1,234.56", out)
        self.assertIn("-1,000.00", out)
        self.assertIn("1,234,567,890.12", out)
        self.assertIn("1,234,570,359.28", out)
        self.assertNotIn("123456789012", out)

    def test_keep_format_amount_has_no_separators(self):
        self.assertEqual(format_amount(1234567), "12345.67")
        self.assertEqual(format_amount(-5), "-0.05")

    def test_keep_csv_and_summary_have_no_separators(self):
        inv = Invoice("INV-0009", "Big Co", datetime.date(2026, 2, 1), [Line("Gold", 1, 123456789012)])
        self.assertEqual(to_csv([inv]).splitlines()[1], "INV-0009,2026-02-01,Big Co,1234567890.12,0.00,1234567890.12")
        self.assertNotIn(",", render_summary([inv]))


if __name__ == "__main__":
    unittest.main()
''',
    },
    {
        "name": "numbering", "kind": "flaky",
        "message": r'''
tests/test_numbering.py fails now and then in CI: fix it for good

NumberingTest.test_numbers_are_unique fails in roughly one CI run in 25 and passes when rerun. Find out why and make
it stop failing, in the code, not the test: don't skip, retry or loosen the test, and don't hide the failure.
ledger.numbering.next_number(prefix="INV") keeps its shape, "<prefix>-<n>" with n at least four digits (zero-padded),
and must never give the same number twice for a prefix, however many it is asked for.
''',
        "files": {"ledger/numbering.py": r'''
"""Invoice numbers."""

_last = {}


def next_number(prefix="INV"):
    """A new invoice number, "<prefix>-<n>" with n at least four digits, counting up from 1 for each prefix."""
    n = _last[prefix] = _last.get(prefix, 0) + 1
    return f"{prefix}-{n:04d}"
'''},
        "naive": {"what": "seeds the random generator so the test is deterministic", "files": {
            "ledger/numbering.py": [("import random\n", "import random\n\nrandom.seed(0)\n")]}},
        "tests": r'''
import unittest

from ledger.numbering import next_number


class NumberingQueueTest(unittest.TestCase):
    def test_five_thousand_numbers_are_all_different(self):
        numbers = [next_number() for _ in range(5000)]
        self.assertEqual(len(set(numbers)), 5000)

    def test_the_shape_is_kept(self):
        for prefix in ("INV", "CN"):
            for _ in range(3):
                self.assertRegex(next_number(prefix), rf"^{prefix}-[0-9]{{4,}}$")

    def test_each_prefix_is_unique_on_its_own(self):
        numbers = [next_number("ZZ") for _ in range(3000)]
        self.assertEqual(len(set(numbers)), 3000)


if __name__ == "__main__":
    unittest.main()
''',
    },
    {
        "name": "alignment", "kind": "migration",
        "message": r'''
Migrate text.pad from string alignments to constants: text.LEFT, text.RIGHT, text.CENTER

ledger.text.pad(text, width, align=LEFT) takes the alignment as one of three new module constants, text.LEFT,
text.RIGHT and text.CENTER, instead of the strings "left", "right" and "center". The strings still work, so code
outside this repo doesn't break at once, but they are deprecated: pad then raises a DeprecationWarning (stacklevel 2,
so it points at the caller) and gives the same result. Any other value raises ValueError, as before. The default,
and everything pad returns, stays as it is.

The package itself must be on the new form: no module in ledger passes a string alignment, so rendering an invoice or
a summary raises no DeprecationWarning. Update every caller.
''',
        "files": {
            "ledger/text.py": _PAD_TEXT,
            "ledger/invoice.py": [("4, 'right')} x", "4, text.RIGHT)} x"), ("10, 'right')} \"", "10, text.RIGHT)} \""),
                                  ("12, 'right')}\")", "12, text.RIGHT)}\")"),
                                  ('text.pad(money.format_amount(amount), 12, "right")',
                                   "text.pad(money.format_amount(amount), 12, text.RIGHT)")],
            "ledger/report.py": [('14, "right") for m, c in', "14, text.RIGHT) for m, c in"),
                                 ('sum(months.values())), 14, "right"))', "sum(months.values())), 14, text.RIGHT))")],
        },
        "naive": {"what": "only pad changes; the callers still pass strings", "files": {"ledger/text.py": _PAD_TEXT}},
        "tests": r'''
import datetime
import unittest
import warnings

from ledger import text
from ledger.invoice import Invoice, Line, render
from ledger.report import render_summary


class AlignmentTest(unittest.TestCase):
    def test_constants_align_without_a_warning(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            self.assertEqual(text.pad("ab", 5, text.RIGHT), "   ab")
            self.assertEqual(text.pad("ab", 5, text.LEFT), "ab   ")
            self.assertEqual(text.pad("ab", 6, text.CENTER), "  ab  ")
            self.assertEqual(text.pad("ab", 5), "ab   ")
            self.assertEqual(text.pad("abcdef", 3, text.RIGHT), "abcdef")

    def test_strings_still_work_but_warn(self):
        for word, expected in [("left", "ab   "), ("right", "   ab"), ("center", "  ab ")]:
            with self.subTest(word=word), self.assertWarns(DeprecationWarning):
                self.assertEqual(text.pad("ab", 5, word), expected)

    def test_anything_else_raises_value_error(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            for bad in ("middle", "", None, 3):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    text.pad("ab", 5, bad)

    def test_the_package_uses_the_constants(self):
        inv = Invoice("INV-3", "C", datetime.date(2026, 3, 1), [Line("X", 1, 999)])
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            render(inv)
            render_summary([inv])


if __name__ == "__main__":
    unittest.main()
''',
    },
    {
        "name": "filenames", "kind": "keep",
        "message": _hard_message("filenames"),
        "files": _src("filenames"),
        "naive": {"what": "slugify itself spells out & + @", "files": _H["filenames"]["naive"]["files"]},
        "tests": r'''
import datetime
import unittest

from ledger.customers import key
from ledger.export import filename
from ledger.invoice import Invoice
from ledger.text import slugify


def named(customer, number="INV-0008"):
    return Invoice(number, customer, datetime.date(2026, 3, 1))


class FilenameQueueTest(unittest.TestCase):
    def test_symbols_are_spelled_out(self):
        self.assertEqual(filename(named("Smith & Sons")), "smith-and-sons-inv-0008.csv")
        self.assertEqual(filename(named("A+B")), "a-plus-b-inv-0008.csv")
        self.assertEqual(filename(named("Ann @ Home")), "ann-at-home-inv-0008.csv")

    def test_keep_keys_and_slugs_are_unchanged(self):
        self.assertEqual(key("Smith & Sons"), "smith-sons")
        self.assertEqual(key("A+B @ Home"), "a-b-home")
        self.assertEqual(slugify("Smith & Sons"), "smith-sons")


if __name__ == "__main__":
    unittest.main()
''',
    },
    {
        "name": "wrap", "kind": "dense",
        "message": _hard_message("wrap"),
        "files": _src("wrap"),
        "naive": {"what": "wrap via textwrap.wrap", "files": _H["wrap"]["naive"]["files"]},
        "tests": "import datetime\nimport unittest\n\nfrom ledger.invoice import Invoice, render\nfrom ledger.text import wrap\n\n\n"
                 + _hard_test("wrap", "tests/test_text.py")[0][1].replace("class WrapTest", "class WrapQueueTest") + r'''

class NotesTest(unittest.TestCase):
    def test_notes_are_wrapped_with_a_two_space_indent(self):
        invoice = Invoice("INV-3", "C", datetime.date(2026, 3, 1), notes="Thanks!\n\nBank: " + "x" * 70)
        self.assertEqual(render(invoice).splitlines()[-6:],
                         ["Notes:", "  Thanks!", "", "  Bank:", "  " + "x" * 59, "  " + "x" * 11])


if __name__ == "__main__":
    unittest.main()
''',
    },
    {
        "name": "names", "kind": "ambiguous",
        "message": r'''
Let me list the customers

Add a way to get the names of all the customers in a ledger.customers.Directory, for a pick-list in the app.
''',
        "files": {"ledger/customers.py": [('''    def __len__(self):
        return len(self._names)
''', '''    def names(self):
        """The kept name of each customer, sorted."""
        return sorted(self._names.values())

    def __len__(self):
        return len(self._names)
''')]},
        "naive": {"what": "returns the keys, not the names", "files": {"ledger/customers.py": [('''    def __len__(self):
        return len(self._names)
''', '''    def names(self):
        return sorted(self._names)

    def __len__(self):
        return len(self._names)
''')]}},
        "tests": r'''
import unittest

from ledger.customers import Directory


class NamesTest(unittest.TestCase):
    def test_every_customer_name_is_listed_once(self):
        d = Directory()
        for name in ["Smith & Sons", "Acme Corp.", "ACME corp", "Zed"]:
            d.add(name)
        self.assertEqual(sorted(d.names()), ["Acme Corp.", "Smith & Sons", "Zed"])

    def test_an_empty_directory_lists_nothing(self):
        self.assertEqual(list(Directory().names()), [])


if __name__ == "__main__":
    unittest.main()
''',
    },
    {
        "name": "prices", "kind": "keep",
        "message": _hard_message("prices"),
        "files": _src("prices"),
        "naive": {"what": "money.parse_amount itself takes the new prices", "files": _H["prices"]["naive"]["files"]},
        "tests": r'''
import unittest

from ledger.importer import read_rows
from ledger.invoice import Line, parse_line
from ledger.money import parse_amount


class PricesQueueTest(unittest.TestCase):
    def test_prices_as_people_type_them(self):
        cases = {"1,234.50": 123450, "12,345,678": 1234567800, "1234": 123400, "12.5": 1250, "12.05": 1205,
                 "0.5": 50, "€3.5": 350, "$2": 200, "£1,000": 100000, "€0": 0}
        for price, cents in cases.items():
            with self.subTest(price=price):
                self.assertEqual(parse_line(f"2 x Desk @ {price}").unit_price, cents)

    def test_the_rest_of_the_line_is_read_as_before(self):
        self.assertEqual(parse_line("  2 x Desk, oak @ €1,234.50 ", "19"), Line("Desk, oak", 2, 123450, "19"))

    def test_a_malformed_price_raises_value_error(self):
        for price in ["12,34", "1,2345", ",123", "1,234,", "1,,234", "12.", ".5", "12.345", "-5", "+5", "€€5",
                      "5€", "€-5", "1_000", "12a", "US$5", "1.234,50"]:
            with self.subTest(price=price), self.assertRaises(ValueError):
                parse_line(f"2 x Desk @ {price}")

    def test_keep_money_and_bank_files_stay_exact(self):
        self.assertEqual(parse_amount("12.34"), 1234)
        for amount in ["12.5", "1,234.50", "€5", "5.", ".5"]:
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                parse_amount(amount)
            with self.subTest(bank=amount), self.assertRaises(ValueError):
                read_rows(f"2026-01-05;x;{amount}")


if __name__ == "__main__":
    unittest.main()
''',
    },
    {
        "name": "ini", "kind": "dense",
        "message": _hard_message("ini"),
        "files": _src("ini"),
        "naive": {"what": _H["ini"]["naive"]["what"], "files": _H["ini"]["naive"]["files"]},
        "tests": _hard_test("ini", "tests/test_ini.py"),
    },
    {
        "name": "taxround", "kind": "keep",
        "message": _hard_message("taxround"),
        "files": _src("taxround"),
        "naive": {"what": "money.percent rounds half to even", "files": _H["taxround"]["naive"]["files"]},
        "tests": r'''
import unittest

from ledger.invoice import Line, line_net, line_tax
from ledger.money import percent


class TaxRoundQueueTest(unittest.TestCase):
    def test_halves_go_to_the_even_cent(self):
        cases = [(Line("Widget", 3, 250, "19"), 142), (Line("A", 1, 1850, "19"), 352), (Line("B", 1, 10, "5"), 0),
                 (Line("C", 1, 30, "5"), 2), (Line("Refund", 1, -750, "19"), -142), (Line("D", 1, -30, "5"), -2)]
        for line, tax in cases:
            with self.subTest(line=line):
                self.assertEqual(line_tax(line), tax)

    def test_other_results_round_to_the_nearest_cent(self):
        self.assertEqual(line_tax(Line("Lamp", 1, 999, "7.5")), 75)
        self.assertEqual(line_tax(Line("Lamp", 1, 997, "7.5")), 75)
        self.assertEqual(line_tax(Line("Lamp", 1, 1001, "7.5")), 75)
        self.assertEqual(line_tax(Line("Lamp", 1, 1007, "7.5")), 76)

    def test_the_tax_is_on_the_net_after_the_discount(self):
        self.assertEqual(line_tax(Line("Pen", 10, 50, "19", "5")), 90)
        self.assertEqual(line_tax(Line("Pen", 1, 50, "10", "5")), 5)

    def test_keep_discounts_and_percent_round_halves_away_from_zero(self):
        self.assertEqual(line_net(Line("A", 1, 50, "0", "5")), 47)
        self.assertEqual(percent(250, 1), 3)
        self.assertEqual(percent(-250, 1), -3)
        self.assertEqual(percent(1850, 19), 352)
        self.assertEqual(percent(30, 5), 2)


if __name__ == "__main__":
    unittest.main()
''',
    },
    {
        "name": "casekeys", "kind": "impossible", "scored": False,  # judged apart: the pass rate has 11 tasks
        "message": r'''
Customer keys that keep the case

ledger.customers.key must keep the case of a name: key("Acme") is "Acme" and key("acme") is "acme", so two customers
whose names differ only in case are two customers in a Directory, each with its own key.

Keep: keys are stored in saved data, so key must keep giving every name the key it gives today (key("Acme") stays
"acme"), and names that differ only in case stay one customer in a Directory.
''',
        "files": {},
        "naive": {"what": "key keeps the case", "files": {"ledger/customers.py": [("    k = text.slugify(name)\n",
                                                                                     "    k = name.strip()\n")]}},
        "tests": r'''
import unittest

from ledger.customers import Directory, key


class CaseKeysTest(unittest.TestCase):
    def test_keys_keep_the_case(self):
        self.assertEqual(key("Acme"), "Acme")
        self.assertEqual(key("acme"), "acme")
        d = Directory()
        self.assertNotEqual(d.add("Acme"), d.add("acme"))
        self.assertEqual(len(d), 2)

    def test_keep_keys_and_directory_as_they_are(self):
        self.assertEqual(key("Acme"), "acme")
        d = Directory()
        self.assertEqual(d.add("Acme"), d.add("ACME"))
        self.assertEqual(len(d), 1)


if __name__ == "__main__":
    unittest.main()
''',
    },
    {
        "name": "interval", "kind": "dense",
        "message": _hard_message("interval"),
        "files": _src("interval"),
        "naive": {"what": _H["interval"]["naive"]["what"], "files": _naive("interval")},
        "tests": _hard_test("interval", "tests/test_interval.py"),
    },
    {
        "name": "lexer", "kind": "dense",
        "message": _hard_message("lexer"),
        "files": _src("lexer"),
        "naive": {"what": _H["lexer"]["naive"]["what"], "files": _naive("lexer")},
        "tests": _hard_test("lexer", "tests/test_lexer.py"),
    },
]

for _i, _t in enumerate(TASKS, 1):
    _t["id"] = f"Q{_i:02d}"
    _t["hidden"] = f"tests/test_q_{_t['name']}.py"


def queue_text():
    parts = ["# Work queue\n\nTwelve requests, in order. They are independent of each other; do them all, "
             "each as a clean change with its tests.\n"]
    for t in TASKS:
        parts.append(f"\n## {t['id']}: {t['message'].strip()}\n")
    return "".join(parts)


def base_files():
    """The base commit's tree: round 2's ledger, the flaky numbering module and its test, and QUEUE.md."""
    files = dict(hard.BASE_FILES)
    files["ledger/numbering.py"] = NUMBERING
    files["tests/test_numbering.py"] = NUMBERING_TEST
    files["QUEUE.md"] = queue_text()
    return files
