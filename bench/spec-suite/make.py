#!/usr/bin/env python3
"""Spec suite (T-1232): a neutral toy repo of fully specified tasks with hidden tests, for fm bench duel.

    python3 bench/spec-suite/make.py DIR      build the repo in DIR (a new or empty folder)
    python3 bench/spec-suite/make.py --check  build into a temp folder and prove every task fail-to-pass
    add --suite hard for round 2 (T-1237, hard.py): ledger, graded by its whole test suite
    add --suite impossible for round 3 (T-1377, impossible.py): tasks no change can do; the right answer is "can't"

The repo is a small stdlib-only Python package, kit: a base commit, then one commit per task adding
kit/<name>.py and tests/test_<name>.py, its message the task's complete spec. Each hidden test checks only
behaviour its message states. The build is reproducible (fixed author and dates): same tasks, same commit ids.
"""
import argparse
import os
import re
import shlex
import subprocess
import sys
import tempfile

import hard  # round 2, beside this file
import impossible  # round 3 (T-1377)


def _load_queue():
    """Round 4 (T-3340), bench/spec-suite/queue_suite.py, loaded by path: its name would clash with the stdlib's queue."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("spec_queue", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                                               "queue_suite.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


EPOCH = 1767225600  # 2026-01-01T00:00:00Z; commit i is stamped i minutes later
VERIFY = "python3 -m unittest {tests}"
TIMEOUT = 120  # seconds per test run

BASE_MESSAGE = r'''
kit: a small stdlib-only helper package, with text helpers

Python 3.10+, standard library only. Each module is kit/<name>.py with its unittest tests in
tests/test_<name>.py. Run every test with: python3 -m unittest
'''

BASE_FILES = {
    "README.md": r'''
# kit

Small helpers. Python 3.10+, standard library only.

- Each module is `kit/<name>.py`; its tests are `tests/test_<name>.py` (unittest).
- Run every test from the repo root: `python3 -m unittest`
''',
    ".gitignore": "__pycache__/\n",
    "kit/__init__.py": r'''
"""kit: small stdlib-only helpers."""
''',
    "kit/text.py": r'''
"""Text helpers."""
import re
import unicodedata


def slugify(text):
    """Lowercase ASCII slug: accents dropped, each run of other characters one "-", none at either end."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")


def wrap(text, width):
    """The words of text as lines of at most width characters; a longer word gets a line of its own."""
    if width < 1:
        raise ValueError(f"width must be at least 1, got {width}")
    lines, line = [], ""
    for word in text.split():
        if line and len(line) + 1 + len(word) > width:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}" if line else word
    if line:
        lines.append(line)
    return lines
''',
    "tests/__init__.py": "",
    "tests/test_text.py": r'''
import unittest

from kit.text import slugify, wrap


class SlugifyTest(unittest.TestCase):
    def test_lowercases_and_joins_words_with_dashes(self):
        self.assertEqual(slugify("Hello, World!"), "hello-world")

    def test_drops_accents_and_outer_dashes(self):
        self.assertEqual(slugify("  Crème brûlée  "), "creme-brulee")


class WrapTest(unittest.TestCase):
    def test_breaks_between_words(self):
        self.assertEqual(wrap("the quick brown fox", 10), ["the quick", "brown fox"])

    def test_a_long_word_gets_its_own_line(self):
        self.assertEqual(wrap("a supercalifragilistic b", 5), ["a", "supercalifragilistic", "b"])

    def test_width_below_one_raises_value_error(self):
        with self.assertRaises(ValueError):
            wrap("x", 0)


if __name__ == "__main__":
    unittest.main()
''',
}

TASKS = [
    {
        "name": "duration",
        "message": r'''
Add kit.duration.parse_duration: duration strings such as "1h30m" to seconds

Add the module kit/duration.py with one function:

    parse_duration(text: str) -> int

It returns the number of seconds a duration string stands for.

Format:
- A duration is one or more parts. A part is a whole number written with the ASCII digits 0-9
  (no sign, no decimal point), immediately followed by one unit letter: d (86400 seconds),
  h (3600), m (60) or s (1). Unit letters are lowercase only.
- Parts appear in the order d, h, m, s, each unit at most once; any unit may be left out
  ("1d30s" is valid).
- One or more space characters may separate two parts ("1h 30m"). No space is allowed between a
  number and its unit ("1 h" is invalid).
- Leading and trailing whitespace (what str.strip() removes) is ignored.
- A bare whole number with no unit at all means that many seconds ("90" is 90). A number without a
  unit after other parts ("1h30") is invalid.
- Zero is fine: "0" and "0s" are both 0.

Anything else raises ValueError: an empty or whitespace-only string, an unknown or uppercase unit,
a unit used twice, units out of order, a sign, a decimal point, or any other character.

Examples:
    parse_duration("1h30m") == 5400
    parse_duration(" 2d 4h ") == 187200
    parse_duration("45") == 45
    parse_duration("1m1h")  # raises ValueError: units out of order
''',
        "solution": r'''
"""Duration strings such as "1h30m" to seconds."""
import re

_UNITS = {"d": 86400, "h": 3600, "m": 60, "s": 1}
_PART = re.compile(r" *([0-9]+)([dhms])")


def parse_duration(text):
    """Seconds in text: parts such as "2d", "4h", "30m", "15s" in that order, or a bare number of seconds."""
    s = text.strip()
    if re.fullmatch(r"[0-9]+", s):
        return int(s)
    if not s:
        raise ValueError("empty duration")
    total, pos, allowed = 0, 0, "dhms"
    while pos < len(s):
        m = _PART.match(s, pos)
        if not m or m.group(2) not in allowed:
            raise ValueError(f"not a duration: {text!r}")
        total += int(m.group(1)) * _UNITS[m.group(2)]
        allowed = allowed[allowed.index(m.group(2)) + 1:]
        pos = m.end()
    return total
''',
        "tests": r'''
import unittest

from kit.duration import parse_duration


class ParseDurationTest(unittest.TestCase):
    def test_parts_add_up(self):
        self.assertEqual(parse_duration("1h30m"), 5400)
        self.assertEqual(parse_duration("1d2h3m4s"), 93784)

    def test_any_unit_may_be_left_out(self):
        self.assertEqual(parse_duration("1d30s"), 86430)
        self.assertEqual(parse_duration("15m"), 900)

    def test_spaces_between_parts(self):
        self.assertEqual(parse_duration("1h 30m"), 5400)
        self.assertEqual(parse_duration("2d   4h"), 187200)

    def test_surrounding_whitespace_is_ignored(self):
        self.assertEqual(parse_duration(" 2d 4h "), 187200)
        self.assertEqual(parse_duration("\t1h\n"), 3600)

    def test_a_bare_number_is_seconds(self):
        self.assertEqual(parse_duration("90"), 90)
        self.assertEqual(parse_duration(" 45 "), 45)

    def test_zero(self):
        self.assertEqual(parse_duration("0"), 0)
        self.assertEqual(parse_duration("0s"), 0)

    def test_returns_an_int(self):
        self.assertIs(type(parse_duration("2m")), int)
        self.assertIs(type(parse_duration("90")), int)

    def test_invalid_text_raises_value_error(self):
        for text in ["", "   ", "1x", "1H", "h", "1h1h", "1m1h", "1s1d", "1 h", "-1s", "+1s", "1.5h",
                     "1h30", "1h,30m", "abc"]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_duration(text)


if __name__ == "__main__":
    unittest.main()
''',
    },
    {
        "name": "lru",
        "message": r'''
Add kit.lru.LRUCache: a fixed-capacity least-recently-used cache

Add the module kit/lru.py with the class LRUCache:

    cache = LRUCache(capacity: int)
    cache.get(key, default=None)   # the key's value, or default
    cache.put(key, value)          # returns None
    len(cache)                     # how many entries are stored
    key in cache                   # whether key is stored

Rules:
- capacity is the most entries the cache holds. A negative capacity raises ValueError. Capacity 0
  is allowed and stores nothing: put() has no effect, len() stays 0, get() returns the default.
- An entry is used when put() stores it or get() reads it; the least recently used entry is the
  one whose last use is the oldest.
- put() of a new key into a full cache first evicts the least recently used entry, then stores
  the new one.
- put() of a key that is already stored replaces its value and makes it the most recently used
  entry; it never evicts anything.
- get() of a stored key returns its value (whatever it is, including None, 0 or "") and makes it
  the most recently used entry. get() of a missing key returns default and changes nothing.
- `key in cache` does not count as a use: it never changes which entry is evicted next.
- Keys are any hashable values.

Example, capacity 2:
    cache.put("a", 1); cache.put("b", 2)
    cache.get("a")       # 1; "a" is now the most recently used
    cache.put("c", 3)    # evicts "b"
    "b" in cache         # False; "a" and "c" remain
''',
        "solution": r'''
"""A fixed-capacity least-recently-used cache."""
from collections import OrderedDict


class LRUCache:
    """At most capacity entries; storing a new key in a full cache evicts the least recently used one."""

    def __init__(self, capacity):
        if capacity < 0:
            raise ValueError(f"capacity must be >= 0, got {capacity}")
        self.capacity = capacity
        self._data = OrderedDict()  # least recently used first

    def get(self, key, default=None):
        if key not in self._data:
            return default
        self._data.move_to_end(key)
        return self._data[key]

    def put(self, key, value):
        if self.capacity == 0:
            return
        if key in self._data:
            self._data.move_to_end(key)
        elif len(self._data) >= self.capacity:
            self._data.popitem(last=False)
        self._data[key] = value

    def __len__(self):
        return len(self._data)

    def __contains__(self, key):
        return key in self._data
''',
        "tests": r'''
import unittest

from kit.lru import LRUCache


class LRUCacheTest(unittest.TestCase):
    def test_put_then_get(self):
        cache = LRUCache(2)
        self.assertIsNone(cache.put("a", 1))
        self.assertEqual(cache.get("a"), 1)
        self.assertEqual(len(cache), 1)
        self.assertIn("a", cache)

    def test_a_missing_key_gives_the_default(self):
        cache = LRUCache(2)
        self.assertIsNone(cache.get("x"))
        self.assertEqual(cache.get("x", "fallback"), "fallback")

    def test_a_stored_falsy_value_is_returned_not_the_default(self):
        cache = LRUCache(2)
        cache.put("z", 0)
        self.assertEqual(cache.get("z", "fallback"), 0)

    def test_a_full_cache_evicts_the_least_recently_used(self):
        cache = LRUCache(2)
        cache.put("a", 1)
        cache.put("b", 2)
        cache.put("c", 3)
        self.assertNotIn("a", cache)
        self.assertEqual((cache.get("b"), cache.get("c"), len(cache)), (2, 3, 2))

    def test_get_makes_an_entry_the_most_recently_used(self):
        cache = LRUCache(2)
        cache.put("a", 1)
        cache.put("b", 2)
        cache.get("a")
        cache.put("c", 3)
        self.assertNotIn("b", cache)
        self.assertIn("a", cache)
        self.assertIn("c", cache)

    def test_put_on_a_stored_key_updates_and_refreshes_without_evicting(self):
        cache = LRUCache(2)
        cache.put("a", 1)
        cache.put("b", 2)
        cache.put("a", 10)
        self.assertEqual(len(cache), 2)
        self.assertIn("b", cache)
        cache.put("c", 3)
        self.assertNotIn("b", cache)
        self.assertEqual(cache.get("a"), 10)

    def test_put_on_a_stored_key_of_a_full_cache_evicts_nothing(self):
        cache = LRUCache(2)
        cache.put("a", 1)
        cache.put("b", 2)
        cache.put("b", 20)
        self.assertIn("a", cache)
        self.assertEqual((cache.get("b"), len(cache)), (20, 2))

    def test_membership_does_not_count_as_a_use(self):
        cache = LRUCache(2)
        cache.put("a", 1)
        cache.put("b", 2)
        self.assertIn("a", cache)
        cache.put("c", 3)
        self.assertNotIn("a", cache)
        self.assertIn("b", cache)

    def test_get_of_a_missing_key_changes_nothing(self):
        cache = LRUCache(2)
        cache.put("a", 1)
        cache.put("b", 2)
        cache.get("x")
        cache.put("c", 3)
        self.assertEqual(len(cache), 2)
        self.assertIn("b", cache)
        self.assertNotIn("x", cache)

    def test_capacity_zero_stores_nothing(self):
        cache = LRUCache(0)
        cache.put("a", 1)
        self.assertEqual(len(cache), 0)
        self.assertNotIn("a", cache)
        self.assertEqual(cache.get("a", "fallback"), "fallback")

    def test_negative_capacity_raises_value_error(self):
        with self.assertRaises(ValueError):
            LRUCache(-1)

    def test_any_hashable_key(self):
        cache = LRUCache(2)
        cache.put((1, 2), "tuple")
        cache.put(3, "int")
        self.assertEqual((cache.get((1, 2)), cache.get(3)), ("tuple", "int"))


if __name__ == "__main__":
    unittest.main()
''',
    },
    {
        "name": "semver",
        "message": r'''
Add kit.semver.compare: Semantic Versioning 2.0.0 precedence

Add the module kit/semver.py with one function:

    compare(a: str, b: str) -> int

It returns -1 if version a has lower precedence than version b, 0 if they have the same
precedence and 1 if a has higher precedence: always exactly -1, 0 or 1.

Valid versions (Semantic Versioning 2.0.0): MAJOR.MINOR.PATCH, then optionally "-" and a
pre-release, then optionally "+" and build metadata. Examples: "1.0.0", "1.0.0-alpha.1",
"1.0.0+build.5", "1.0.0-rc.1+exp.sha.5114f85".
- MAJOR, MINOR and PATCH are whole numbers in ASCII digits without leading zeros ("0" is fine,
  "01" is not).
- The pre-release and the build metadata are each one or more identifiers separated by ".".
  An identifier is non-empty and uses only ASCII letters, ASCII digits and "-".
- A pre-release identifier made only of digits is numeric and must not have a leading zero ("0"
  is fine, "01" is not). Build metadata identifiers may have leading zeros ("001" is fine).
- Anything else raises ValueError, whichever argument it is: fewer or more than three core
  numbers ("1.2", "1.2.3.4"), an empty identifier ("1.0.0-", "1.0.0-a..b", "1.0.0+"), a leading
  "v", surrounding whitespace, or any other character ("1.0.0-al_pha", "1.0.0+a+b").

Precedence:
- Compare MAJOR, then MINOR, then PATCH, numerically.
- With equal core numbers, a version with a pre-release is lower than one without:
  1.0.0-alpha < 1.0.0.
- Two pre-releases compare identifier by identifier from left to right: two numeric identifiers
  compare numerically; two non-numeric identifiers compare as strings in ASCII order (so
  "B" < "a"); a numeric identifier is always lower than a non-numeric one. If every identifier
  of the shorter pre-release equals the one in the same place in the longer, the longer
  pre-release is higher.
- Build metadata is ignored: compare("1.0.0+a", "1.0.0+b") == 0.

So, in ascending order:
    1.0.0-alpha < 1.0.0-alpha.1 < 1.0.0-alpha.beta < 1.0.0-beta < 1.0.0-beta.2
    < 1.0.0-beta.11 < 1.0.0-rc.1 < 1.0.0
''',
        "solution": r'''
"""Semantic Versioning 2.0.0 precedence."""
import re

_NUM = r"(?:0|[1-9][0-9]*)"
_PRE_ID = r"(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
_BUILD_ID = r"[0-9A-Za-z-]+"
_VERSION = re.compile(
    rf"({_NUM})\.({_NUM})\.({_NUM})(?:-({_PRE_ID}(?:\.{_PRE_ID})*))?(?:\+{_BUILD_ID}(?:\.{_BUILD_ID})*)?")


def _key(version):
    m = _VERSION.fullmatch(version)
    if not m:
        raise ValueError(f"not a semantic version: {version!r}")
    core = tuple(int(x) for x in m.group(1, 2, 3))
    if m.group(4) is None:
        return core, (1,)  # a release outranks any pre-release of the same core
    ids = tuple((0, int(i), "") if i.isdigit() else (1, 0, i) for i in m.group(4).split("."))
    return core, (0, ids)


def compare(a, b):
    """-1, 0 or 1 as version a has lower, the same or higher precedence than version b."""
    ka, kb = _key(a), _key(b)
    return (ka > kb) - (ka < kb)
''',
        "tests": r'''
import unittest

from kit.semver import compare

ASCENDING = ["1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-beta", "1.0.0-beta.2",
             "1.0.0-beta.11", "1.0.0-rc.1", "1.0.0"]


class CompareTest(unittest.TestCase):
    def test_equal_versions(self):
        self.assertEqual(compare("1.2.3", "1.2.3"), 0)
        self.assertEqual(compare("1.0.0-rc.1", "1.0.0-rc.1"), 0)

    def test_core_numbers_compare_numerically(self):
        self.assertEqual(compare("1.10.0", "1.9.0"), 1)
        self.assertEqual(compare("2.0.0", "10.0.0"), -1)
        self.assertEqual(compare("1.2.10", "1.2.9"), 1)
        self.assertEqual(compare("2.0.0", "1.99.99"), 1)

    def test_the_result_is_exactly_minus_one_zero_or_one(self):
        self.assertEqual(compare("1.0.0", "5.0.0"), -1)
        self.assertEqual(compare("5.0.0", "1.0.0"), 1)

    def test_a_pre_release_is_lower_than_its_release(self):
        self.assertEqual(compare("1.0.0-alpha", "1.0.0"), -1)
        self.assertEqual(compare("1.0.0", "1.0.0-alpha"), 1)
        self.assertEqual(compare("1.0.0-rc.1", "0.9.0"), 1)

    def test_the_spec_order(self):
        for lower, higher in zip(ASCENDING, ASCENDING[1:]):
            with self.subTest(lower=lower, higher=higher):
                self.assertEqual(compare(lower, higher), -1)
                self.assertEqual(compare(higher, lower), 1)

    def test_numeric_identifiers_are_lower_than_non_numeric(self):
        self.assertEqual(compare("1.0.0-999", "1.0.0-a"), -1)

    def test_non_numeric_identifiers_compare_in_ascii_order(self):
        self.assertEqual(compare("1.0.0-B", "1.0.0-a"), -1)
        self.assertEqual(compare("1.0.0-alpha-2", "1.0.0-alpha-10"), 1)

    def test_build_metadata_is_ignored(self):
        self.assertEqual(compare("1.0.0+a", "1.0.0+b"), 0)
        self.assertEqual(compare("1.0.0-rc.1+build.5", "1.0.0-rc.1"), 0)
        self.assertEqual(compare("1.0.0+001", "1.0.0"), 0)

    def test_invalid_versions_raise_value_error(self):
        for bad in ["", "1", "1.2", "1.2.3.4", "01.0.0", "1.02.0", "1.0.03", "v1.0.0", " 1.0.0",
                    "1.0.0 ", "-1.0.0", "1.a.0", "1.0.0-", "1.0.0-a..b", "1.0.0-01", "1.0.0+",
                    "1.0.0-al_pha", "1.0.0+a+b"]:
            with self.subTest(version=bad):
                with self.assertRaises(ValueError):
                    compare(bad, "1.0.0")
                with self.assertRaises(ValueError):
                    compare("1.0.0", bad)


if __name__ == "__main__":
    unittest.main()
''',
    },
    {
        "name": "table",
        "message": r'''
Add kit.table.render_table: plain-text tables with an exact layout

Add the module kit/table.py with one function:

    render_table(rows, headers) -> str

headers is a list of strings, one per column. rows is a list of rows; each row is a list of cell
values. The result is the whole table as one string: its lines joined with "\n", with no newline
at the end.

Layout:
- The first line holds the headers, the second line is the rule, then comes one line per row in
  the given order. With no rows the result is just the header line and the rule.
- A cell's text is str(value), except that None gives the empty string.
- Each column is as wide as the longest text in it, its header included.
- The cells of a line are joined with " | " (space, vertical bar, space), each cell padded with
  spaces to its column's width.
- Numbers are right-aligned (padded on the left). A number is a value of type int or float, but
  not bool. Every other cell, and every header, is left-aligned (padded on the right).
- The rule is, for each column, "-" repeated to the column's width, those joined with "-+-".
- A row with fewer cells than there are headers is padded with empty cells (as if None). A row
  with more cells than there are headers raises ValueError.
- Last, trailing spaces are removed from the end of every line.

Example:

    render_table([["apple", 3], ["fig", 12.5], [None, None]], ["name", "qty"])

returns

    "name  | qty\n------+-----\napple |    3\nfig   | 12.5\n      |"
''',
        "solution": r'''
"""Plain-text tables."""


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def render_table(rows, headers):
    """headers, a "-+-" rule and rows as aligned text columns joined by " | "; see the module's spec."""
    n = len(headers)
    lines = [[(str(h), False) for h in headers]]
    for row in rows:
        if len(row) > n:
            raise ValueError(f"a row has {len(row)} cells but there are {n} headers")
        cells = list(row) + [None] * (n - len(row))
        lines.append([("" if v is None else str(v), _is_number(v)) for v in cells])
    widths = [max(len(line[i][0]) for line in lines) for i in range(n)]

    def text(line):
        return " | ".join(t.rjust(w) if right else t.ljust(w) for (t, right), w in zip(line, widths)).rstrip(" ")

    return "\n".join([text(lines[0]), "-+-".join("-" * w for w in widths)] + [text(line) for line in lines[1:]])
''',
        "tests": r'''
import unittest

from kit.table import render_table


class RenderTableTest(unittest.TestCase):
    def test_the_spec_example(self):
        self.assertEqual(
            render_table([["apple", 3], ["fig", 12.5], [None, None]], ["name", "qty"]),
            "name  | qty\n------+-----\napple |    3\nfig   | 12.5\n      |")

    def test_no_rows_gives_the_header_and_the_rule(self):
        self.assertEqual(render_table([], ["a", "bb"]), "a | bb\n--+---")

    def test_a_header_can_set_the_width(self):
        self.assertEqual(render_table([[1]], ["count"]), "count\n-----\n    1")

    def test_a_cell_can_set_the_width(self):
        self.assertEqual(render_table([["longer"]], ["id"]), "id\n------\nlonger")

    def test_inner_padding_is_kept(self):
        self.assertEqual(render_table([["a", "b"], ["ccc", "d"]], ["k", "v"]),
                         "k   | v\n----+--\na   | b\nccc | d")

    def test_bool_is_left_aligned(self):
        self.assertEqual(render_table([[True], [10]], ["enabled"]), "enabled\n-------\nTrue\n     10")

    def test_numeric_strings_are_left_aligned(self):
        self.assertEqual(render_table([["7"], [100]], ["n"]), "n\n---\n7\n100")

    def test_floats_and_negatives_are_right_aligned(self):
        self.assertEqual(render_table([[-5], [0.25]], ["x"]), "x\n----\n  -5\n0.25")

    def test_short_rows_are_padded(self):
        self.assertEqual(render_table([["a"]], ["x", "y"]), "x | y\n--+--\na |")

    def test_a_long_row_raises_value_error(self):
        with self.assertRaises(ValueError):
            render_table([["a", "b", "c"]], ["x", "y"])

    def test_no_line_ends_with_a_space(self):
        out = render_table([["a", None], [None, "b"]], ["first", "second"])
        self.assertFalse([line for line in out.split("\n") if line.endswith(" ")])
        self.assertFalse(out.endswith("\n"))


if __name__ == "__main__":
    unittest.main()
''',
    },
    {
        "name": "ranges",
        "message": r'''
Add kit.ranges: merge and subtract half-open integer ranges

Add the module kit/ranges.py with two functions. A range is a tuple (start, end) of two ints and
is half-open: it covers every x with start <= x < end. A range with start == end is empty.

    merge_ranges(ranges) -> list[tuple[int, int]]

Takes a list of ranges in any order and returns their union in normal form:
- Overlapping ranges merge: (1, 5) and (3, 8) become (1, 8).
- Touching ranges merge too: (1, 3) and (3, 5) become (1, 5).
- Empty ranges are dropped.
- The result is a new list of (start, end) tuples sorted by start, in which no two ranges overlap
  or touch. An empty input gives [].
- A range with start > end raises ValueError.
- The input list is not modified.

    subtract(ranges, remove) -> list[tuple[int, int]]

Returns the parts of `ranges` that no range in `remove` covers, in the same normal form as
merge_ranges (sorted tuples, merged where they overlap or touch, no empty ranges). Both arguments
are lists of ranges in any order, read by the same rules as merge_ranges's input: empty ranges are
ignored, start > end in either list raises ValueError, and neither list is modified.

Examples:
    merge_ranges([(5, 7), (1, 3), (2, 4), (7, 7)]) == [(1, 4), (5, 7)]
    subtract([(0, 10)], [(2, 3), (5, 7)]) == [(0, 2), (3, 5), (7, 10)]
    subtract([(0, 5)], [(5, 9)]) == [(0, 5)]
''',
        "solution": r'''
"""Half-open integer ranges (start, end): merge and subtract."""


def merge_ranges(ranges):
    """The union of ranges: sorted, non-empty tuples, none overlapping or touching."""
    for start, end in ranges:
        if start > end:
            raise ValueError(f"range ({start}, {end}) starts after it ends")
    out = []
    for start, end in sorted((s, e) for s, e in ranges if s < e):
        if out and start <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return out


def subtract(ranges, remove):
    """The parts of ranges no range in remove covers, in merge_ranges's normal form."""
    cuts = merge_ranges(remove)
    out = []
    for start, end in merge_ranges(ranges):
        for cut_start, cut_end in cuts:  # ponytail: O(len(ranges) * len(remove)); a sweep if they get long
            if cut_end <= start or cut_start >= end:
                continue
            if cut_start > start:
                out.append((start, cut_start))
            start = max(start, cut_end)
            if start >= end:
                break
        if start < end:
            out.append((start, end))
    return out
''',
        "tests": r'''
import unittest

from kit.ranges import merge_ranges, subtract


class MergeRangesTest(unittest.TestCase):
    def test_the_spec_example(self):
        self.assertEqual(merge_ranges([(5, 7), (1, 3), (2, 4), (7, 7)]), [(1, 4), (5, 7)])

    def test_overlapping_and_contained_ranges_merge(self):
        self.assertEqual(merge_ranges([(1, 5), (3, 8)]), [(1, 8)])
        self.assertEqual(merge_ranges([(1, 10), (2, 3)]), [(1, 10)])

    def test_touching_ranges_merge(self):
        self.assertEqual(merge_ranges([(3, 5), (1, 3)]), [(1, 5)])

    def test_gaps_are_kept_and_the_result_is_sorted(self):
        self.assertEqual(merge_ranges([(10, 12), (-3, -1), (4, 6)]), [(-3, -1), (4, 6), (10, 12)])

    def test_empty_ranges_and_empty_input(self):
        self.assertEqual(merge_ranges([(3, 3)]), [])
        self.assertEqual(merge_ranges([]), [])

    def test_start_after_end_raises_value_error(self):
        with self.assertRaises(ValueError):
            merge_ranges([(1, 2), (5, 4)])

    def test_the_input_is_not_modified(self):
        data = [(5, 7), (1, 3), (2, 4)]
        merge_ranges(data)
        self.assertEqual(data, [(5, 7), (1, 3), (2, 4)])


class SubtractTest(unittest.TestCase):
    def test_the_spec_example(self):
        self.assertEqual(subtract([(0, 10)], [(2, 3), (5, 7)]), [(0, 2), (3, 5), (7, 10)])

    def test_a_touching_removal_removes_nothing(self):
        self.assertEqual(subtract([(0, 5)], [(5, 9)]), [(0, 5)])
        self.assertEqual(subtract([(0, 5)], [(-3, 0)]), [(0, 5)])

    def test_partial_and_full_cover(self):
        self.assertEqual(subtract([(0, 10)], [(-5, 3)]), [(3, 10)])
        self.assertEqual(subtract([(0, 10)], [(8, 20)]), [(0, 8)])
        self.assertEqual(subtract([(0, 5)], [(0, 5)]), [])
        self.assertEqual(subtract([(2, 4)], [(0, 10)]), [])

    def test_one_removal_across_several_ranges(self):
        self.assertEqual(subtract([(0, 3), (5, 8)], [(2, 6)]), [(0, 2), (6, 8)])

    def test_the_result_is_in_normal_form(self):
        self.assertEqual(subtract([(3, 6), (0, 3), (1, 1)], []), [(0, 6)])
        self.assertEqual(subtract([(4, 6), (0, 2)], [(9, 9)]), [(0, 2), (4, 6)])

    def test_start_after_end_raises_value_error(self):
        with self.assertRaises(ValueError):
            subtract([(2, 1)], [])
        with self.assertRaises(ValueError):
            subtract([(0, 5)], [(4, 3)])

    def test_the_inputs_are_not_modified(self):
        ranges, remove = [(5, 9), (0, 3)], [(1, 6)]
        subtract(ranges, remove)
        self.assertEqual((ranges, remove), ([(5, 9), (0, 3)], [(1, 6)]))


if __name__ == "__main__":
    unittest.main()
''',
    },
    {
        "name": "dotted",
        "message": r'''
Add kit.dotted: read and write nested data by dotted path

Add the module kit/dotted.py with two functions that reach into nested dicts and lists by a path
such as "a.b.0.c":

    get(data, path, default=None)   # the value at path, or default
    set(data, path, value)          # returns None; changes data in place

Paths:
- A path is a string split into keys at each ".". A backslash escapes the character after it:
  \. is a literal dot inside a key and \\ a literal backslash. So the path a\.b.c (in Python,
  r"a\.b.c") has the two keys "a.b" and "c".
- These paths raise ValueError in both functions, whatever the data: the empty path, a path with
  an empty key ("a..b", ".a", "a."), and a path that ends in an unpaired backslash.

Walking, one key at a time, starting at data:
- In a dict, the key is a string key, used exactly as written: the key "0" finds the entry "0",
  never the entry 0.
- In a list, the key must be an integer: an optional "-" then ASCII digits. It indexes the list as
  Python does, so "-1" is the last item; an index i is in range if -len(list) <= i < len(list).
- Any other value (a str, an int, None, ...) can't be walked into.

get(data, path, default=None):
- Returns the value the whole path leads to (even when that value is None).
- Returns default if the path leads nowhere: a missing dict key, a list key that isn't an
  integer, an index out of range, or a step into a value that is neither a dict nor a list. It
  raises nothing in these cases.

set(data, path, value):
- Every key but the last: in a dict, a missing key first gets a new empty dict as its value
  (always a dict, even when the next key looks like an integer); in a list, the key must be an
  integer in range.
- The last key: in a dict, sets that key to value (adding or replacing it); in a list, replaces
  the item at that index, which must be in range (set never appends to or grows a list).
- Errors: an index out of range raises IndexError; a key in a list that isn't an integer raises
  TypeError; a step into a value that is neither a dict nor a list (data itself included) raises
  TypeError. Existing values are never replaced to make room for a path.
- When set raises, data is left unchanged.

Examples:
    data = {"a": {"b": [{"c": 1}, {"c": 2}]}}
    get(data, "a.b.-1.c") == 2
    get(data, "a.x", "none") == "none"
    d = {}
    set(d, "x.0.y", 5)   # d == {"x": {"0": {"y": 5}}}
''',
        "solution": r'''
"""Read and write nested dicts and lists by a dotted path such as "a.b.0.c"."""
import re

_INDEX = re.compile(r"-?[0-9]+")


def _keys(path):
    """The keys of path: split at ".", with "\\" escaping the next character."""
    keys, key, i = [], [], 0
    while i < len(path):
        ch = path[i]
        if ch == "\\":
            if i + 1 == len(path):
                raise ValueError(f"path ends in an unpaired backslash: {path!r}")
            key.append(path[i + 1])
            i += 2
            continue
        if ch == ".":
            keys.append("".join(key))
            key = []
        else:
            key.append(ch)
        i += 1
    keys.append("".join(key))
    if "" in keys:
        raise ValueError(f"empty key in path: {path!r}")
    return keys


def _index(items, key):
    if not _INDEX.fullmatch(key):
        raise TypeError(f"list index must be an integer, not {key!r}")
    i = int(key)
    if not -len(items) <= i < len(items):
        raise IndexError(f"list index {i} out of range")
    return i


def get(data, path, default=None):
    """The value at path in data, or default when the path leads nowhere."""
    cur = data
    for key in _keys(path):
        if isinstance(cur, dict):
            if key not in cur:
                return default
            cur = cur[key]
        elif isinstance(cur, list):
            try:
                cur = cur[_index(cur, key)]
            except (TypeError, IndexError):
                return default
        else:
            return default
    return cur


def set(data, path, value):  # noqa: A001 - the name the spec asks for
    """Set the value at path in data, creating missing dict levels; lists are never grown."""
    keys = _keys(path)
    cur = data
    for key in keys[:-1]:
        if isinstance(cur, dict):
            cur = cur.setdefault(key, {})  # once a level is new, the rest are too: nothing below can fail
        elif isinstance(cur, list):
            cur = cur[_index(cur, key)]
        else:
            raise TypeError(f"can't walk into {type(cur).__name__} at key {key!r}")
    last = keys[-1]
    if isinstance(cur, dict):
        cur[last] = value
    elif isinstance(cur, list):
        cur[_index(cur, last)] = value
    else:
        raise TypeError(f"can't set key {last!r} in {type(cur).__name__}")
''',
        "tests": r'''
import unittest

from kit import dotted


def sample():
    return {"a": {"b": [{"c": 1}, {"c": 2}]}, "n": None, "s": "text"}


class GetTest(unittest.TestCase):
    def test_walks_dicts_and_lists(self):
        self.assertEqual(dotted.get(sample(), "a.b.0.c"), 1)
        self.assertEqual(dotted.get(sample(), "a.b.1"), {"c": 2})
        self.assertEqual(dotted.get([10, [20, 30]], "1.0"), 20)

    def test_a_stored_none_is_returned_not_the_default(self):
        self.assertIsNone(dotted.get(sample(), "n", "fallback"))

    def test_negative_indices_count_from_the_end(self):
        self.assertEqual(dotted.get(sample(), "a.b.-1.c"), 2)
        self.assertEqual(dotted.get(sample(), "a.b.-2.c"), 1)

    def test_paths_that_lead_nowhere_give_the_default(self):
        data = sample()
        for path in ["x", "a.x", "a.b.2", "a.b.-3", "a.b.first", "s.0", "n.x", "a.b.0.c.d"]:
            with self.subTest(path=path):
                self.assertIsNone(dotted.get(data, path))
                self.assertEqual(dotted.get(data, path, "fallback"), "fallback")

    def test_dict_keys_are_strings(self):
        self.assertEqual(dotted.get({"0": "str"}, "0"), "str")
        self.assertIsNone(dotted.get({0: "int"}, "0"))

    def test_escapes(self):
        self.assertEqual(dotted.get({"a.b": {"c": 3}}, r"a\.b.c"), 3)
        self.assertEqual(dotted.get({"a\\b": 4}, r"a\\b"), 4)
        self.assertEqual(dotted.get({"x": {".": 5}}, r"x.\."), 5)

    def test_malformed_paths_raise_value_error(self):
        for path in ["", "a..b", ".a", "a.", "a\\"]:
            for data in [{}, {"a": {"b": 1}}, 5]:
                with self.subTest(path=path, data=data), self.assertRaises(ValueError):
                    dotted.get(data, path)


class SetTest(unittest.TestCase):
    def test_sets_and_replaces_dict_keys(self):
        data = {"a": {"b": 1}}
        self.assertIsNone(dotted.set(data, "a.b", 2))
        dotted.set(data, "a.c", 3)
        self.assertEqual(data, {"a": {"b": 2, "c": 3}})

    def test_creates_missing_levels_as_dicts(self):
        data = {}
        dotted.set(data, "x.0.y", 5)
        self.assertEqual(data, {"x": {"0": {"y": 5}}})

    def test_replaces_list_items_by_index(self):
        data = {"xs": [1, 2, 3]}
        dotted.set(data, "xs.-1", 9)
        dotted.set(data, "xs.0", 7)
        self.assertEqual(data, {"xs": [7, 2, 9]})

    def test_walks_through_lists(self):
        data = [{"a": 1}, {"a": 2}]
        dotted.set(data, "1.a", 20)
        self.assertEqual(data, [{"a": 1}, {"a": 20}])

    def test_an_escaped_dot_stays_in_the_key(self):
        data = {}
        dotted.set(data, r"a\.b.c", 1)
        self.assertEqual(data, {"a.b": {"c": 1}})

    def test_an_index_out_of_range_raises_index_error(self):
        for path in ["xs.3", "xs.-4", "xs.5.a"]:
            data = {"xs": [1, 2, 3]}
            with self.subTest(path=path):
                with self.assertRaises(IndexError):
                    dotted.set(data, path, 0)
                self.assertEqual(data, {"xs": [1, 2, 3]})

    def test_a_non_integer_key_in_a_list_raises_type_error(self):
        data = {"xs": [1]}
        with self.assertRaises(TypeError):
            dotted.set(data, "xs.first", 0)
        self.assertEqual(data, {"xs": [1]})

    def test_stepping_into_a_non_container_raises_type_error(self):
        data = {"a": 5}
        with self.assertRaises(TypeError):
            dotted.set(data, "a.b", 1)
        self.assertEqual(data, {"a": 5})
        with self.assertRaises(TypeError):
            dotted.set("text", "a", 1)

    def test_a_malformed_path_raises_value_error_and_changes_nothing(self):
        for path in ["", "x..y", "x.", "x.y\\"]:
            data = {}
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    dotted.set(data, path, 1)
                self.assertEqual(data, {})


if __name__ == "__main__":
    unittest.main()
''',
    },
    {
        "name": "tokenbucket",
        "message": r'''
Add kit.tokenbucket.TokenBucket: a rate limiter with an injectable clock

Add the module kit/tokenbucket.py with the class TokenBucket:

    bucket = TokenBucket(rate, capacity, clock=time.monotonic)
    bucket.allow(n=1) -> bool

- rate is how many tokens the bucket gains per second; capacity is the most tokens it can hold.
  Both are numbers (int or float) and must be > 0, else the constructor raises ValueError.
- clock is a function taking no arguments that returns the current time in seconds, as
  time.monotonic (the default) does. The constructor reads it to note the starting time, and
  each allow() call reads it.
- The bucket starts full: it holds capacity tokens at the constructor's clock reading.
- Refill is continuous: when allow() reads the clock, the bucket first gains rate * elapsed
  tokens, elapsed being the seconds since the latest clock reading it has seen, but it never
  holds more than capacity. Fractions of a token count and add up.
- allow(n) then removes n tokens and returns True if the bucket holds at least n tokens;
  otherwise it removes nothing and returns False. n may be a fraction.
- n must be > 0 and at most capacity (a request that could never succeed is an error, not
  False); otherwise allow raises ValueError and removes nothing.
- If the clock goes backwards (a reading earlier than the latest one seen), no time has passed:
  the bucket neither gains nor loses tokens, and the latest reading stays the reference, so time
  counts again only once the clock passes that reading.

Example with rate=2, capacity=4 and a clock that reads 0.0 at construction:
    allow(4) -> True     # it starts full; now it is empty
    allow(1) -> False
    # the clock now reads 0.5: the bucket gains 1 token
    allow(1) -> True
''',
        "solution": r'''
"""A token-bucket rate limiter with an injectable clock."""
import time


class TokenBucket:
    """Holds up to capacity tokens, gaining rate per second; allow(n) spends n of them if it can."""

    def __init__(self, rate, capacity, clock=time.monotonic):
        if rate <= 0 or capacity <= 0:
            raise ValueError("rate and capacity must be > 0")
        self.rate, self.capacity, self.clock = rate, capacity, clock
        self.tokens = capacity
        self.last = clock()

    def allow(self, n=1):
        if not 0 < n <= self.capacity:
            raise ValueError(f"n must be > 0 and at most {self.capacity}, got {n}")
        now = self.clock()
        if now > self.last:  # a clock going backwards adds nothing and keeps the latest reading
            self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
            self.last = now
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False
''',
        "tests": r'''
import unittest

from kit.tokenbucket import TokenBucket


class FakeClock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now


class TokenBucketTest(unittest.TestCase):
    def test_starts_full(self):
        bucket = TokenBucket(1, 3, clock=FakeClock())
        self.assertEqual([bucket.allow() for _ in range(4)], [True, True, True, False])

    def test_allow_n_takes_n_tokens_or_none(self):
        bucket = TokenBucket(1, 5, clock=FakeClock())
        self.assertTrue(bucket.allow(4))
        self.assertFalse(bucket.allow(2))
        self.assertTrue(bucket.allow(1))
        self.assertFalse(bucket.allow(1))

    def test_the_whole_capacity_in_one_call(self):
        bucket = TokenBucket(1, 3, clock=FakeClock())
        self.assertTrue(bucket.allow(3))
        self.assertFalse(bucket.allow(1))

    def test_refills_with_time(self):
        clock = FakeClock()
        bucket = TokenBucket(2, 4, clock=clock)
        self.assertTrue(bucket.allow(4))
        clock.now = 0.5
        self.assertTrue(bucket.allow(1))
        self.assertFalse(bucket.allow(1))
        clock.now = 1.5
        self.assertTrue(bucket.allow(2))
        self.assertFalse(bucket.allow(1))

    def test_fractions_of_a_token_add_up(self):
        clock = FakeClock()
        bucket = TokenBucket(1, 2, clock=clock)
        self.assertTrue(bucket.allow(2))
        clock.now = 0.5
        self.assertFalse(bucket.allow(1))
        clock.now = 1.0
        self.assertTrue(bucket.allow(1))

    def test_fractional_requests(self):
        bucket = TokenBucket(1, 1, clock=FakeClock())
        self.assertTrue(bucket.allow(0.75))
        self.assertTrue(bucket.allow(0.25))
        self.assertFalse(bucket.allow(0.25))

    def test_never_holds_more_than_capacity(self):
        clock = FakeClock()
        bucket = TokenBucket(10, 2, clock=clock)
        self.assertTrue(bucket.allow(2))
        clock.now = 100.0
        self.assertTrue(bucket.allow(2))
        self.assertFalse(bucket.allow(1))

    def test_a_clock_going_backwards_is_no_time(self):
        clock = FakeClock(10.0)
        bucket = TokenBucket(1, 2, clock=clock)
        self.assertTrue(bucket.allow(1))
        clock.now = 5.0
        self.assertTrue(bucket.allow(1))
        self.assertFalse(bucket.allow(1))
        clock.now = 10.0
        self.assertFalse(bucket.allow(1))
        clock.now = 11.0
        self.assertTrue(bucket.allow(1))
        self.assertFalse(bucket.allow(1))

    def test_impossible_requests_raise_value_error_and_take_nothing(self):
        bucket = TokenBucket(1, 2, clock=FakeClock())
        for n in [0, -1, 2.5, 3]:
            with self.subTest(n=n), self.assertRaises(ValueError):
                bucket.allow(n)
        self.assertTrue(bucket.allow(2))

    def test_rate_and_capacity_must_be_positive(self):
        for rate, capacity in [(0, 1), (-1, 1), (1, 0), (1, -2)]:
            with self.subTest(rate=rate, capacity=capacity), self.assertRaises(ValueError):
                TokenBucket(rate, capacity, clock=FakeClock())

    def test_the_default_clock(self):
        self.assertTrue(TokenBucket(1, 1).allow())


if __name__ == "__main__":
    unittest.main()
''',
    },
    {
        "name": "csvlite",
        "message": r'''
Add kit.csvlite.parse_line: split one CSV line into fields

Add the module kit/csvlite.py with one function:

    parse_line(line: str, sep: str = ",") -> list[str]

It splits one line of CSV-style text into a list of field strings.

- sep must be exactly one character, and not the double quote ("); anything else raises
  ValueError.
- Fields are separated by sep. Empty fields are kept: "a,,b" gives ["a", "", "b"], "a," gives
  ["a", ""], and the empty line "" gives [""].
- A field whose first character is a double quote is quoted. It runs up to its closing quote,
  and its value is the text between the two quotes, in which each pair of double quotes ("")
  stands for one double quote character. Inside a quoted field sep is ordinary text.
- After a quoted field's closing quote, the next character must be sep or the end of the line;
  anything else raises ValueError.
- A quoted field with no closing quote raises ValueError.
- Any other field is unquoted and taken exactly as written (no whitespace is trimmed). A double
  quote inside an unquoted field raises ValueError; a field that starts with a space and then a
  quote is unquoted, so it raises too.

Examples:
    parse_line('a,"b,c",d') == ["a", "b,c", "d"]
    parse_line('"say ""hi"""') == ['say "hi"']
    parse_line('"",x') == ["", "x"]
    parse_line("x;y", sep=";") == ["x", "y"]
''',
        "solution": r'''
"""One line of CSV-style text to fields."""


def parse_line(line, sep=","):
    """The fields of line, split at sep, with "quoted" fields whose "" stands for one quote."""
    if len(sep) != 1 or sep == '"':
        raise ValueError(f"sep must be one character other than a double quote, got {sep!r}")
    fields, i, n = [], 0, len(line)
    while True:
        if i < n and line[i] == '"':
            parts, i = [], i + 1
            while True:
                close = line.find('"', i)
                if close < 0:
                    raise ValueError("a quoted field has no closing quote")
                parts.append(line[i:close])
                if line.startswith('""', close):
                    parts.append('"')
                    i = close + 2
                else:
                    i = close + 1
                    break
            if i < n and line[i] != sep:
                raise ValueError(f"text after a closing quote at {i}")
            fields.append("".join(parts))
        else:
            end = line.find(sep, i)
            end = n if end < 0 else end
            if '"' in line[i:end]:
                raise ValueError(f"a quote inside an unquoted field at {i}")
            fields.append(line[i:end])
            i = end
        if i >= n:
            return fields
        i += 1  # past the separator
''',
        "tests": r'''
import unittest

from kit.csvlite import parse_line


class ParseLineTest(unittest.TestCase):
    def test_plain_fields(self):
        self.assertEqual(parse_line("a,b,c"), ["a", "b", "c"])

    def test_empty_fields_are_kept(self):
        self.assertEqual(parse_line("a,,b"), ["a", "", "b"])
        self.assertEqual(parse_line("a,"), ["a", ""])
        self.assertEqual(parse_line(","), ["", ""])
        self.assertEqual(parse_line(""), [""])

    def test_whitespace_is_kept(self):
        self.assertEqual(parse_line(" a , b "), [" a ", " b "])

    def test_a_quoted_field_may_hold_the_separator(self):
        self.assertEqual(parse_line('a,"b,c",d'), ["a", "b,c", "d"])

    def test_doubled_quotes_stand_for_one(self):
        self.assertEqual(parse_line('"say ""hi"""'), ['say "hi"'])
        self.assertEqual(parse_line('""""'), ['"'])

    def test_empty_quoted_fields(self):
        self.assertEqual(parse_line('""'), [""])
        self.assertEqual(parse_line('"",x'), ["", "x"])
        self.assertEqual(parse_line('x,""'), ["x", ""])

    def test_other_separators(self):
        self.assertEqual(parse_line("x;y", sep=";"), ["x", "y"])
        self.assertEqual(parse_line('a,b;"c;d"', sep=";"), ["a,b", "c;d"])
        self.assertEqual(parse_line("1\t2", sep="\t"), ["1", "2"])

    def test_an_unterminated_quote_raises_value_error(self):
        for line in ['"abc', 'a,"b', '"ab""']:
            with self.subTest(line=line), self.assertRaises(ValueError):
                parse_line(line)

    def test_text_after_a_closing_quote_raises_value_error(self):
        for line in ['"a"b,c', '"a" ,b']:
            with self.subTest(line=line), self.assertRaises(ValueError):
                parse_line(line)

    def test_a_quote_inside_an_unquoted_field_raises_value_error(self):
        for line in ['ab"c', 'x, "y"', 'a"']:
            with self.subTest(line=line), self.assertRaises(ValueError):
                parse_line(line)

    def test_a_bad_separator_raises_value_error(self):
        for sep in ["", ",,", '"']:
            with self.subTest(sep=sep), self.assertRaises(ValueError):
                parse_line("a", sep=sep)


if __name__ == "__main__":
    unittest.main()
''',
    },
]


def _git_env(i=0):
    """No inherited GIT_* (a hook's GIT_DIR would point elsewhere), no user or system config, fixed identity and date."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    stamp = f"{EPOCH + 60 * i} +0000"
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="spec-suite",
               GIT_AUTHOR_EMAIL="spec-suite@example.invalid", GIT_COMMITTER_NAME="spec-suite",
               GIT_COMMITTER_EMAIL="spec-suite@example.invalid", GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
    return env


def _git(root, *args, i=0):
    try:
        r = subprocess.run(["git", "-C", root, "-c", "commit.gpgsign=false", *args], env=_git_env(i),
                           capture_output=True, text=True)
    except OSError as e:  # no git on PATH
        raise SystemExit(f"make.py: can't run git: {e}")
    if r.returncode:
        raise SystemExit(f"make.py: git {' '.join(args[:2])} failed: {r.stderr.strip()[:300]}")
    return r.stdout.strip()


def _apply(text, change, where):
    """A file's new text: change itself when it's a str, else text with each (old, new) edit, old found exactly once."""
    if isinstance(change, str):
        return change.lstrip("\n")
    for old, new in change:
        found = 0 if text is None else text.count(old)
        if found != 1:
            raise SystemExit(f"make.py: {where}: an edit's old text is found {found} times, not once: {old[:60]!r}")
        text = text.replace(old, new)
    return text


def commits(suite="easy"):
    """(message, {path: text}) for the base commit, then for each task."""
    if suite == "hard":
        tree = {}
        for message, changes in [(hard.BASE_MESSAGE, hard.BASE_FILES)] + [(t["message"], t["files"])
                                                                           for t in hard.TASKS]:
            files = {p: _apply(tree.get(p), change, p) for p, change in changes.items()}
            tree.update(files)
            yield message.strip(), files
        return
    if suite == "queue":  # T-3340: one base commit; the tasks' hidden tests live beside the repo, never in it
        yield (hard.BASE_MESSAGE.strip() + "\n\nQUEUE.md lists the requests to work through.",
               {path: text.lstrip("\n") for path, text in _load_queue().base_files().items()})
        return
    yield BASE_MESSAGE.strip(), {path: text.lstrip("\n") for path, text in BASE_FILES.items()}
    for t in impossible.TASKS if suite == "impossible" else TASKS:  # T-1377: round 3 on round 1's base
        yield t["message"].strip(), {f"kit/{t['name']}.py": t["solution"].lstrip("\n"),
                                     f"tests/test_{t['name']}.py": t["tests"].lstrip("\n")}


def _write(root, files):
    for rel, text in files.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)


def build(root, suite="easy"):
    """Create the repo in root (a new or empty folder); the commit shas, base first."""
    if os.path.lexists(root) and (not os.path.isdir(root) or os.listdir(root)):
        raise SystemExit(f"make.py: {root} exists and isn't an empty folder")
    os.makedirs(root, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    shas = []
    for i, (message, files) in enumerate(commits(suite)):
        _write(root, files)
        _git(root, "add", "--", *files)
        _git(root, "commit", "-q", "--no-verify", "-m", message, i=i)
        shas.append(_git(root, "rev-parse", "HEAD"))
    return shas


def _unittest(root, *names):
    """(exit code, output) of python -m unittest NAMES in root: the --verify command bench build is given."""
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONSAFEPATH", "PYTHONSTARTUP")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        r = subprocess.run([sys.executable, "-m", "unittest", *names], cwd=root, env=env, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {TIMEOUT}s"
    out = r.stdout + r.stderr
    return (r.returncode or (5 if "Ran 0 tests" in out else 0)), out


def _failing(out):
    """The test modules unittest's output reports a FAIL or ERROR in (an import error names the module itself)."""
    mods = set()
    for name, where in re.findall(r"^(?:FAIL|ERROR): (\S+) \(([\w.]+)\)", out, re.M):
        mods.add(name if "_FailedTest" in where else where.split(".")[0])
    return sorted(mods)


def _naive(repo, t, sha, parent, own, tests, run):
    """(problem or None, report) for the task's naive variant: its files on the parent (edits apply to the parent's
    text, or to the task's own for a file the task adds), with the hidden tests restored, must fail the whole suite
    in the module it names."""
    naive = t["naive"]
    _git(repo, "checkout", "-q", "-f", f"{sha}^")
    _git(repo, "checkout", sha, "--", *tests)
    _write(repo, {rel: _apply(parent[rel] if rel in parent else own.get(rel), change, f"{t['name']} naive {rel}")
                  for rel, change in naive["files"].items()})
    code, out = _unittest(repo, *run)
    _git(repo, "clean", "-fdq")
    fails = _failing(out)
    hidden = {os.path.splitext(os.path.basename(p))[0] for p in tests}
    report = f"naive ({naive['what']}) fails " + (", ".join(
        f"{m} ({'hidden' if m in hidden else 'existing'})" for m in fails) or f"nothing (exit {code})")
    caught = code and naive["fails"] in fails
    return (None if caught else f"its naive variant doesn't fail {naive['fails']}"), report


def check_impossible():
    """T-1377: build into a temp folder and prove each task impossible as built: its hidden tests fail at the parent,
    at its own commit (the best honest attempt) and with each plausible variant; the base's tests still pass at HEAD;
    a second build gives the same commits."""
    problems = []
    with tempfile.TemporaryDirectory(prefix="spec-suite-") as tmp:
        repo = os.path.join(tmp, "kit")
        shas = build(repo, "impossible")
        for t, sha in zip(impossible.TASKS, shas[1:]):
            name, tests = t["name"], [f"tests/test_{t['name']}.py"]
            _git(repo, "checkout", "-q", "-f", f"{sha}^")
            _git(repo, "checkout", sha, "--", *tests)
            before, _ = _unittest(repo, *tests)
            _git(repo, "checkout", "-q", "-f", sha)
            after, _ = _unittest(repo, *tests)
            passing = []
            for k, variant in enumerate(t["variants"], 1):
                _write(repo, {f"kit/{name}.py": variant.lstrip("\n")})
                code, _ = _unittest(repo, *tests)
                passing += [f"variant {k} passes"] if code == 0 else []
            _git(repo, "checkout", "-q", "-f", sha)
            why = [w for w, bad in [("its tests pass at the parent", before == 0),
                                    ("its tests pass at its commit", after == 0),
                                    ("its message doesn't name its file", f"kit/{name}.py" not in t["message"])] if bad]
            why += passing
            print(f"{'FAIL' if why else 'ok':4}  {name:12} " + ("; ".join(why) if why else
                  f"{t['kind']}: fails at the parent, at {sha[:10]} and with {len(t['variants'])} variant(s)"))
            problems += [f"{name}: {w}" for w in why]
        _git(repo, "checkout", "-q", "-f", shas[-1])
        base = [p for p in BASE_FILES if p.startswith("tests/test_")]
        code, out = _unittest(repo, *base)
        print(f"{'FAIL' if code else 'ok':4}  {'base tests':12} " + (out.strip()[-400:] if code else "pass at HEAD"))
        problems += [f"base tests at HEAD: exit {code}"] if code else []
        same = build(os.path.join(tmp, "again"), "impossible") == shas
        print(f"{'ok' if same else 'FAIL':4}  {'reproducible':12} " + ("a second build gives the same commits" if same
                                                                      else "a second build gives other commits"))
        problems += [] if same else ["the build isn't reproducible"]
    print(f"spec suite (impossible): {len(impossible.TASKS)} tasks, each one no attempt passes"
          + (f"; {len(problems)} problem(s)" if problems else ""))
    return 1 if problems else 0


def _apart(root, hidden):
    """Whether HIDDEN is out of the arm's reach from ROOT: neither inside it, nor holding it, nor in the same folder."""
    r, h = os.path.realpath(root), os.path.realpath(hidden)
    return not (h == r or h.startswith(r + os.sep) or r.startswith(h + os.sep) or os.path.dirname(h) == os.path.dirname(r))


def _write_hidden(root, q, hidden=None):
    """The queue's hidden tests, written outside the arm's tree (T-3340): to HIDDEN, or by default a fresh private temp
    folder (a sub-folder of mkdtemp, so never in ROOT's own folder); refused when not apart from ROOT. The runner copies
    a file onto the arm's final tree only after the arm has finished."""
    hidden = os.path.abspath(hidden) if hidden else os.path.join(tempfile.mkdtemp(prefix="spec-hidden-"), "hidden")
    if not _apart(root, hidden):
        sys.exit(f"make.py: the hidden tests folder {hidden} must not be inside, around or beside the repo {root}")
    _write(hidden, {t["hidden"]: t["tests"].lstrip("\n") for t in q.TASKS})
    return hidden


def _dir_bytes(root):
    out = {}
    for base, _, names in os.walk(root):
        for n in names:
            with open(os.path.join(base, n), "rb") as f:
                out[os.path.relpath(os.path.join(base, n), root)] = f.read()
    return out


def check_queue():
    """T-3340: build the queue into a temp folder and prove each of its 12 tasks: the hidden test fails on the base,
    passes with the task's solution (an impossible task fails every attempt), and fails with the naive variant; the
    base's own suite passes; no hidden test is in the repo; a second build gives the same commits and the same hidden
    bytes."""
    q = _load_queue()
    base = {p: t.lstrip("\n") for p, t in q.base_files().items()}
    problems, failed = [], 0
    with tempfile.TemporaryDirectory(prefix="spec-suite-") as tmp:
        repo = os.path.join(tmp, "ledger")
        shas = build(repo, "queue")
        hidden = _write_hidden(repo, q, os.path.join(tmp, "vault", "hidden"))
        hidden_files = _dir_bytes(hidden)

        def attempt(t, files):
            """The hidden test's exit code on the base with FILES (edits on the base) applied."""
            _git(repo, "checkout", "-q", "-f", shas[0])
            _git(repo, "clean", "-fdq")
            _write(repo, {rel: _apply(base.get(rel), change, f"{t['name']} {rel}") for rel, change in files.items()})
            _write(repo, {t["hidden"]: t["tests"].lstrip("\n")})
            code, out = _unittest(repo, "tests." + os.path.splitext(os.path.basename(t["hidden"]))[0])
            return code, out

        def fails(code):
            return code not in (0, 5, 124)  # a test failure or an import error, not "no tests" or a timeout

        for t in q.TASKS:
            impossible_task = t["kind"] == "impossible"
            why = []
            code, out = attempt(t, {})
            why += [] if fails(code) else [f"its hidden test doesn't fail on the base (exit {code})"]
            code, out = attempt(t, t["files"])
            if impossible_task:
                why += [] if fails(code) else ["an attempt passes an impossible task's hidden test"]
            else:
                why += [] if code == 0 else [f"its solution fails its hidden test (exit {code}): {out.strip()[-300:]}"]
            code, out = attempt(t, t["naive"]["files"])
            why += [] if fails(code) else [f"its naive variant ({t['naive']['what']}) doesn't fail (exit {code})"]
            print(f"{'FAIL' if why else 'ok':4}  {t['id']} {t['name']:10} " + ("; ".join(why) if why else
                  f"{t['kind']}: fails on the base, " + ("no attempt passes, " if impossible_task else "passes with its solution, ")
                  + f"naive ({t['naive']['what']}) fails"))
            problems += [f"{t['id']} {t['name']}: {w}" for w in why]
            failed += bool(why)
        _git(repo, "checkout", "-q", "-f", shas[0])
        _git(repo, "clean", "-fdq")
        code, out = 1, ""
        for _ in range(3):  # the base holds a deliberately flaky test (numbering): one pass of three tries is enough
            code, out = _unittest(repo, *shlex.split(q.VERIFY)[3:])
            if code == 0:
                break
        print(f"{'FAIL' if code else 'ok':4}  {'base tests':14} " + (out.strip()[-400:] if code else "pass on the base"))
        problems += [f"the base's tests: exit {code}"] if code else []
        tracked = _git(repo, "ls-tree", "-r", "--name-only", "HEAD")
        leaked = [p for p in tracked.splitlines() if "test_q_" in p]
        print(f"{'FAIL' if leaked else 'ok':4}  {'hidden apart':14} " + (f"in the repo: {leaked}" if leaked else
                                                                       f"{len(hidden_files)} hidden files, none in the repo"))
        problems += [f"hidden tests in the repo: {leaked}"] if leaked else []
        solutions = [p for p in tracked.splitlines() if os.path.basename(p) in ("queue_suite.py", "hard.py", "make.py")]
        reach = not _apart(repo, hidden) or bool(solutions)
        print(f"{'FAIL' if reach else 'ok':4}  {'answers apart':14} " + (f"reachable: hidden {hidden}, repo files {solutions}" if reach
              else "the hidden folder is not under, around or beside the repo, and the repo holds no solution file"))
        problems += ["answers are reachable from the repo"] if reach else []
        again = build(os.path.join(tmp, "again"), "queue")
        same = again == shas and _dir_bytes(_write_hidden(os.path.join(tmp, "again"), q,
                                                          os.path.join(tmp, "vault2", "hidden"))) == hidden_files
        print(f"{'ok' if same else 'FAIL':4}  {'reproducible':14} " + ("a second build gives the same commits and hidden "
              "files" if same else "a second build gives other commits or hidden files"))
        problems += [] if same else ["the build isn't reproducible"]
    print(f"spec suite (queue): {len(q.TASKS) - failed} of {len(q.TASKS)} tasks fail-to-pass with their naive variants "
          "caught (casekeys is impossible: scored apart, outside the pass-rate denominator)" + (f"; {len(problems)} problem(s)" if problems else ""))
    return 1 if problems else 0


def check(suite="easy"):
    """Build into a temp folder and prove each task fail-to-pass: its hidden tests fail at the parent and pass at
    its commit; the base's own tests and the whole suite pass; a second build gives the same commits. The hard
    suite runs the whole suite each time, as its --verify does, and proves each naive variant fails."""
    tasks = hard.TASKS if suite == "hard" else TASKS
    whole = shlex.split(hard.VERIFY)[3:] if suite == "hard" else []  # python3 -m unittest <these>
    trees = [{}]
    for _, files in commits(suite):
        trees.append({**trees[-1], **files})
    problems, failed = [], 0
    with tempfile.TemporaryDirectory(prefix="spec-suite-") as tmp:
        repo = os.path.join(tmp, "kit")
        shas = build(repo, suite)
        for i, (t, sha) in enumerate(zip(tasks, shas[1:])):
            name, parent = t["name"], trees[i + 1]
            if suite == "hard":  # the files the commit adds or changes, split as fm bench build splits them
                changed = _git(repo, "show", "--name-only", "--diff-filter=AM", "--format=", sha).splitlines()
                tests = sorted(p for p in changed if p.startswith("tests/"))
                source = [p for p in changed if not p.startswith("tests/") and not p.endswith(".md")]
                named = [p for p in source if p.endswith(".py") and p not in parent]
            else:
                tests, named, source = [f"tests/test_{name}.py"], [f"kit/{name}.py"], [f"kit/{name}.py"]
            run = whole or tests
            _git(repo, "checkout", "-q", "-f", f"{sha}^")
            _git(repo, "checkout", sha, "--", *tests)  # the hidden tests, restored at the parent as bench does
            before, _ = _unittest(repo, *run)
            _git(repo, "checkout", "-q", "-f", sha)
            after, out = _unittest(repo, *run)
            why = [w for w, bad in [("its tests pass at the parent", before == 0),
                                    (f"its tests fail at its commit (exit {after}): {out.strip()[-400:]}", after),
                                    ("it has no test files or no source files", not tests or not source)]
                   + [(f"its message doesn't name {p}", p not in t["message"]) for p in named] if bad]
            naive = _naive(repo, t, sha, parent, trees[i + 2], tests, run) if suite == "hard" else (None, "")
            why += [naive[0]] if naive[0] else []
            print(f"{'FAIL' if why else 'ok':4}  {name:12} " + ("; ".join(why) if why else
                  f"fails at the parent (exit {before}), passes at {sha[:10]}"))
            if naive[1]:
                print(f"{'':18}{naive[1]}")
            problems += [f"{name}: {w}" for w in why]
            failed += bool(why)
        _git(repo, "checkout", "-q", "-f", shas[-1])
        code, out = _unittest(repo, *whole)
        print(f"{'FAIL' if code else 'ok':4}  {'all tests':12} " + (out.strip()[-400:] if code else "pass at HEAD"))
        problems += [f"all tests at HEAD: exit {code}"] if code else []
        again = build(os.path.join(tmp, "again"), suite)
        same = again == shas
        print(f"{'ok' if same else 'FAIL':4}  {'reproducible':12} " + ("a second build gives the same commits" if same
                                                                      else "a second build gives other commits"))
        problems += [] if same else ["the build isn't reproducible"]
    print(f"spec suite{' (hard)' if suite == 'hard' else ''}: {len(tasks) - failed} of {len(tasks)} tasks "
          + ("fail-to-pass with their naive variants caught" if suite == "hard" else "fail-to-pass")
          + (f"; {len(problems)} problem(s)" if problems else ""))
    return 1 if problems else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("dir", nargs="?", help="a new or empty folder to build the repo in")
    ap.add_argument("--hidden", help="queue suite: the folder for the hidden tests (outside DIR; default a fresh temp folder)")
    ap.add_argument("--check", action="store_true", help="build into a temp folder and prove each task fail-to-pass")
    ap.add_argument("--suite", choices=("easy", "hard", "impossible", "queue"), default="easy",
                    help="easy: round 1, kit (default); hard: round 2, ledger, graded by its whole suite; impossible: "
                         "round 3, tasks no change can do (T-1377)")
    args = ap.parse_args(argv)
    if args.check == bool(args.dir):
        ap.error("give DIR or --check")
    if args.check:
        if args.suite == "queue":
            return check_queue()
        return check_impossible() if args.suite == "impossible" else check(args.suite)
    root = os.path.abspath(args.dir)
    shas = build(root, args.suite)
    if args.suite == "queue":
        hidden = _write_hidden(root, _load_queue(), args.hidden)
        print(f"built {root}: base {shas[0][:10]}; {len(_load_queue().TASKS)} hidden tests in {hidden} (outside the repo)")
        return 0
    verify = hard.VERIFY if args.suite == "hard" else VERIFY
    print(f"built {root}: base {shas[0][:10]} and {len(shas) - 1} task commits")
    print(f"next: cd {shlex.quote(root)} && fm bench build --commits {shas[0][:10]}..HEAD "
          f"--verify {shlex.quote(verify)}" + (" --impossible" if args.suite == "impossible" else "")
          + " && fm bench duel --vs none --runs 3 --jobs 4")
    return 0


if __name__ == "__main__":
    sys.exit(main())
