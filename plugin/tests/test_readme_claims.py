"""T-5032 (slice of T-4331): the counts README.md states about Sampo (stack cards, areas, style looks, Board panels, deny
rules, the lite CLAUDE.md's length) equal the live ones, and a drifted number fails naming README.md:LINE. A table of
claim patterns, not a prose parser: a number that is not about one of these things is prose and is never read. A count
README starts to state about verbs, hooks, tests, skills or the like, with no row here, fails too, so a new claim must
come with its live source."""
import glob
import os
import re
import unittest

import helpers  # noqa: F401  (puts plugin/lib on the path)
from helpers import PLUGIN

from sampo import setup as sampo_setup

REPO = os.path.dirname(PLUGIN)
README = os.path.join(REPO, "README.md")
CARDS = os.path.join(PLUGIN, "skills", "library", "cards")
MOD = os.path.join(REPO, "mods", "sampo-ui", "hooks", "register.tsx")  # T-5552: foreman-ui is a symlink to this

_ONES = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
         "seventeen eighteen nineteen").split()
_TENS = "twenty thirty forty fifty sixty seventy eighty ninety".split()
WORDS = {w: i for i, w in enumerate(_ONES)}
WORDS.update({w: 20 + 10 * i for i, w in enumerate(_TENS)})
WORDS.update({f"{t}-{o}": 20 + 10 * i + j for i, t in enumerate(_TENS) for j, o in enumerate(_ONES[1:10], 1)})
WORDS["hundred"] = 100
NUM = r"(?:\d[\d,]*|" + "|".join(sorted(WORDS, key=len, reverse=True)) + r")"


def value(tok):
    tok = tok.lower().replace(",", "")
    return int(tok) if tok.isdigit() else WORDS[tok]


def live_cards():
    return len(glob.glob(os.path.join(CARDS, "*", "*.md")))


def live_areas():
    return len([d for d in glob.glob(os.path.join(CARDS, "*")) if os.path.isdir(d)])


def live_looks():
    return len(glob.glob(os.path.join(CARDS, "looks", "look-*.md")))


def live_panels():
    """The built-in Board panels: the names in the mod's BUILT list (ORDER is derived from it)."""
    with open(MOD, encoding="utf-8") as f:
        m = re.search(r"const BUILT = \[([^\]]+)\] as const", f.read())
    return len(re.findall(r"'[^']+'", m.group(1))) if m else 0


def live_deny_rules():
    return len(sampo_setup.DENY_RULES)


def live_lite_lines():
    with open(os.path.join(PLUGIN, "templates", "CLAUDE.lite.md"), encoding="utf-8") as f:
        return len(f.read().splitlines())


# (name, regex whose group 1 is the number, live count, how the claim reads: exact, floor "Over N" or ceiling "under N")
CLAIMS = (
    ("stack cards", re.compile(rf"\b[Oo]ver ({NUM}) stack cards"), live_cards, "floor"),
    ("areas", re.compile(rf"\bin ({NUM}) areas\b"), live_areas, "exact"),
    ("named looks", re.compile(rf"\b({NUM}) named looks\b"), live_looks, "exact"),
    ("panels", re.compile(rf"\b({NUM}) panels (?:sized to the dock|in a two-column grid)"), live_panels, "exact"),
    ("deny rules", re.compile(rf"\b({NUM}) deny rules\b"), live_deny_rules, "exact"),
    ("lite lines", re.compile(rf"\bunder ({NUM}) lines\b"), live_lite_lines, "ceiling"),
)
# what a count of these nouns could mean: each one README states needs a row above or the test names it
WATCHED = re.compile(rf"(?<![\w`/.-])({NUM}) (verbs|hooks|tests|test modules|subcommands|skills|playbooks|stack cards|"
                     rf"cards|looks|modules)\b(?![\w/`-])", re.I)
FLOOR_SLACK = 1.5  # "Over 1,000" while the shelf holds 1,500 is stale too: a floor within 1.5x of the live count


def problems(text, live=None):
    """Every claim in README text that is stale, missing or unlisted, as 'README.md:LINE: ...' lines."""
    live = live or {}
    out, seen = [], set()
    lines = text.splitlines()
    for name, rx, fn, mode in CLAIMS:
        want = live[name] if name in live else fn()
        for n, line in enumerate(lines, 1):
            for m in rx.finditer(line):
                seen.add(name)
                got = value(m.group(1))
                if mode == "floor":
                    ok = got < want <= got * FLOOR_SLACK
                elif mode == "ceiling":
                    ok = want < got
                else:
                    ok = got == want
                if not ok:
                    out.append(f"README.md:{n}: says {m.group(0)!r} ({mode} {got}) but the live count of {name} is {want}")
    for name, *_ in CLAIMS:
        if name not in seen:
            out.append(f"README.md: no claim about {name} found: the sentence moved or was reworded; update CLAIMS")
    for n, line in enumerate(lines, 1):
        spans = [m.span() for _, rx, _, _ in CLAIMS for m in rx.finditer(line)]
        for m in WATCHED.finditer(line):
            if not any(a <= m.start() < b for a, b in spans):
                out.append(f"README.md:{n}: states {m.group(0)!r} with no live source: add a row to CLAIMS")
    return out


def readme():
    with open(README, encoding="utf-8") as f:
        return f.read()


SYNTHETIC = ("Over 1,000 stack cards in 29 areas.\ntwenty named looks.\nten panels sized to the dock.\n"
             "11 deny rules.\nunder 90 lines.\n")
LIVE = {"stack cards": 1200, "areas": 29, "named looks": 20, "panels": 10, "deny rules": 11, "lite lines": 80}


class ReadmeClaims(unittest.TestCase):
    def test_the_readme_counts_match_the_live_ones(self):
        self.assertEqual(problems(readme()), [])

    def test_live_sources_are_found(self):
        # a moved folder or a renamed constant would make every claim compare with 0 and a "floor" pass nothing
        for name, _, fn, _ in CLAIMS:
            with self.subTest(name=name):
                self.assertGreater(fn(), 0)

    def test_a_drifted_number_names_its_line(self):
        # synthetic text the test owns, checked against fixed live counts: independent of the README's wording
        self.assertEqual(problems(SYNTHETIC, live=LIVE), [])
        line = next(n for n, l in enumerate(SYNTHETIC.splitlines(), 1) if "stack cards in" in l)
        for old, new, name in (("Over 1,000 stack cards", "Over 5,000 stack cards", "stack cards"),
                               ("Over 1,000 stack cards", "Over 100 stack cards", "stack cards"),
                               (" in 29 areas", " in 99 areas", "areas"),
                               ("twenty named looks", "nineteen named looks", "named looks"),
                               ("ten panels", "nine panels", "panels"),
                               ("11 deny rules", "7 deny rules", "deny rules")):
            with self.subTest(change=new):
                self.assertIn(old, SYNTHETIC)
                found = problems(SYNTHETIC.replace(old, new), live=LIVE)
                self.assertTrue(found, f"{new!r} passed")
                self.assertTrue(all(re.match(r"README\.md:\d+: ", p) for p in found), found)
                self.assertTrue(any(name in p for p in found), found)
        bad = problems(SYNTHETIC.replace("Over 1,000 stack cards in", "Over 5,000 stack cards in"), live=LIVE)
        self.assertTrue(any(p.startswith(f"README.md:{line}: ") for p in bad), bad)

    def test_a_count_with_no_live_source_is_named(self):
        found = problems(SYNTHETIC + "Sampo ships 412 verbs and forty-two hooks.\n", live=LIVE)
        self.assertEqual(sum("with no live source" in p for p in found), 2, found)

    def test_a_claim_that_went_missing_is_named(self):
        found = problems(SYNTHETIC.replace("twenty named looks", "a shelf of looks"), live=LIVE)
        self.assertTrue(any("no claim about named looks" in p for p in found), found)

    def test_only_the_stack_card_sentence_states_the_area_count(self):
        # "N areas" in other prose is not the shelf's area count
        self.assertEqual(problems(SYNTHETIC + "Sampo has two areas of work left.\n", live=LIVE), [])

    def test_number_words(self):
        for tok, n in (("ten", 10), ("twenty", 20), ("twenty-five", 25), ("1,227", 1227), ("29", 29)):
            self.assertEqual(value(tok), n)


if __name__ == "__main__":
    unittest.main()
