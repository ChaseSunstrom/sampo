"""T-5035: a number the site's pages claim (stack cards, areas, tests, tasks, lanes, moonshots, feature pages, ...) is the
number site/data/stats.json, genesis.json and PAGES hold, after a build; no page types such a count by hand.

The build goes to a temp folder once; each page's visible text (script, style and svg stripped, the meta description
kept) is searched for a named list of number+noun claims. Each claim names the pages it must appear on, so a reworded
page fails here rather than passing for having nothing to compare. A second sweep finds any count of a noun the build
knows (stack cards, areas, feature pages, moonshots, ...) that no named claim covers: a hand-typed "Over a thousand
stack cards" is one. The release history and the benchmarks keep their own old numbers: only the listed nouns count."""
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

from helpers import PLUGIN, ForemanTestCase, read_text
from test_site_build import site

ROOT = os.path.dirname(PLUGIN)
NUM = r"(\d[\d,]*)"
SWEPT = ("index.html", "cards.html", "genesis/index.html", "features")  # the pages whose counts the build knows


def visible(page: str) -> str:
    """The reader's text: no script, style, svg or comments; the meta description is read too (a search result shows it)."""
    meta = re.findall(r'<meta (?:name|property)="(?:og:)?description" content="([^"]*)"', page)
    page = re.sub(r"(?is)<(script|style|svg|noscript)\b.*?</\1>|<!--.*?-->", " ", page)
    text = html.unescape(re.sub(r"<[^>]+>", " ", page) + " " + " ".join(meta))
    return re.sub(r"\s+", " ", text)


WORDS = {w: i for i, w in enumerate(
    "one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen "
    "nineteen twenty".split(), 1)}
WORDS["dozen"] = 12
WORD = r"(?i:" + "|".join(sorted(WORDS, key=len, reverse=True)) + r")"  # a count in words: "Five", "twenty", "a dozen"


def num(s: str) -> int:
    return WORDS[s.lower()] if s.lower() in WORDS else int(s.replace(",", ""))


class SiteClaims(ForemanTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.TemporaryDirectory()
        out = os.path.join(cls.tmp.name, "o")
        p = subprocess.run([sys.executable, os.path.join(ROOT, "site", "build.py"), out], capture_output=True, text=True,
                           timeout=300)
        if p.returncode != 0:  # not an assert: python -O would strip it and let a failed build pass
            raise AssertionError(p.stderr)
        cls.pages = {}
        for dp, _, names in os.walk(out):
            for n in names:
                rel = os.path.relpath(os.path.join(dp, n), out)
                if n.endswith(".html") and rel.startswith(SWEPT):
                    cls.pages[rel] = visible(read_text(os.path.join(dp, n)))
        with open(os.path.join(ROOT, "site", "data", "stats.json"), encoding="utf-8") as f:
            cls.stats = json.load(f)
        with open(os.path.join(ROOT, "site", "genesis", "genesis.json"), encoding="utf-8") as f:
            cls.moons = json.load(f)["moonshots"]["rows"]
        from sampo import core
        cls.lenses = core.DEFAULT_LENSES  # the lenses an M/L review runs by default
        cls.npages = len(site("pages").PAGES)
        cls.ngroups = len(site("build").GROUPS)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()
        super().tearDownClass()

    def claims(self):
        """(name, pattern, expected numbers, pages it must appear on): the pattern's groups are the numbers it reads."""
        s, t = self.stats, self.stats["totals"]
        cards, areas, tasks = sum(s["cards"].values()), len(s["cards"]), t["tasks_done"]
        wild = sum((r.get("wild") or 0) >= 4 for r in self.moons)
        ideas = sum(r["stage"] == "idea" for r in self.moons)
        tl = s.get("tools") or {}
        home, feats = ("index.html",), ("features/index.html",)
        with open(os.path.join(ROOT, "site", "index.html"), encoding="utf-8") as f:
            drawn = len(re.findall(r'<g class="moon ', f.read()))  # the star chart's marks: its caption's count is theirs
        return [
            ("stack cards", rf"{NUM} stack cards", [cards], home + feats + ("features/cards.html",)),
            ("cards in areas", rf"{NUM} cards in {NUM} areas", [cards, areas], ("features/cards.html",)),
            ("areas", rf"{NUM} areas", [areas], home + ("features/cards.html",)),
            ("tasks planned", rf"{NUM} tasks planned", [tasks], home),
            ("tasks done", rf"tasks done {NUM}", [tasks], home),
            ("tests", rf"{NUM} tests in the gate", [s["code"]["tests"]], home),
            ("lanes merged", rf"{NUM} lanes merged", [t["lanes_merged"]], home),
            ("blocked commands", rf"{NUM} commands blocked", [t["guard_blocks"]], home),
            ("moonshots", rf"{NUM} moonshots", [len(self.moons)], home),
            ("moons held", rf"It holds {NUM};", [len(self.moons)], home),
            ("moons rated wild", rf"these are the {NUM} rated 4 or 5", [wild], home),
            ("moons still ideas", rf"{NUM} of the {NUM} are still ideas", [ideas, len(self.moons)], home),
            ("feature pages", rf"All {NUM} feature pages", [self.npages], home),
            ("pages in groups", rf"{NUM} pages in {NUM} groups", [self.npages, self.ngroups], feats),
            ("record tiles", rf"tasks done {NUM} evidence runs {NUM} proofs {NUM} commits {NUM}",
             [tasks, t["evidence"], t["proofs"], t["commits"]], home),
            ("tools tiles", rf"commits {NUM} evidence runs {NUM} gate runs {NUM}",
             [t["commits"], t["evidence"], t["gate_runs"]], ("features/tools.html",)),
            ("runs over tasks", rf"{NUM} runs over {NUM} tasks", [t["evidence"], tasks], home),
            ("gate runs", rf"median full check, over {NUM} runs", [t["gate_runs"]], home),
            ("proofs per hundred", rf"{NUM} proofs for every hundred tasks", [round(100 * t["proofs"] / tasks)], home),
            ("tools missing", rf"tools found missing {NUM}", [tl.get("misses", 0)], ("features/lacks.html",)),
            ("audit lenses", rf"({WORD}) audit lenses", [len(self.lenses)], ("features/review.html",)),
            ("chart moons", rf"The ({WORD}) wildest of", [drawn], home),
            ("shelf searches", rf"shelf searches {NUM}", [tl.get("searches", 0)], ("features/lacks.html",)),
        ]

    def test_every_named_claim_equals_its_source(self):
        for name, pattern, want, where in self.claims():
            for rel in where:
                found = re.findall(pattern, self.pages.get(rel, ""), re.I)  # a missing page fails below, by name
                self.assertTrue(found, f"{rel} has no '{name}' claim to check ({pattern}); reworded or dropped?")
                for hit in found:
                    got = [num(x) for x in (hit if isinstance(hit, tuple) else (hit,))]
                    self.assertEqual(got, want, f"{rel}: '{name}' says {got}, the build's data says {want}")
            if not pattern.startswith(NUM):  # "commits 1,349" is a tile's label and number: read on the pages named only
                continue
            for rel, text in self.pages.items():  # a page that wasn't named but makes the claim is held to it too
                for hit in re.findall(pattern, text, re.I):
                    got = [num(x) for x in (hit if isinstance(hit, tuple) else (hit,))]
                    self.assertEqual(got, want, f"{rel}: '{name}' says {got}, the build's data says {want}")

    def test_no_count_the_build_knows_is_left_uncovered(self):
        """A count of a noun the build knows, in any wording, is one of the named claims above: a stale or typed-by-hand
        figure in a phrasing no claim reads ("Over a thousand stack cards") fails here."""
        spans = [re.compile(p) for _, p, _, _ in self.claims()]
        count = (rf"(?:{WORD}|\d[\d,]*\+?|[Oo]ver (?:a |one )?(?:thousand|hundred)|(?:a |one )?(?:thousand|hundred)s?|thousands|"
                 r"hundreds|[Mm]ore than \d[\d,]*|[Oo]ver \d[\d,]*|[Nn]early \d[\d,]*)")
        nouns = r"(?:stack cards|cards in \d+ areas|areas|feature pages|moonshots|tests in the gate|lanes merged|tasks planned|audit lenses)"
        loose = re.compile(rf"\b{count} {nouns}")
        for rel, text in self.pages.items():
            covered = [m.span() for p in spans for m in p.finditer(text)]
            for m in loose.finditer(text):
                inside = any(a < m.end() and m.start() < b for a, b in covered)  # the two ranges overlap
                self.assertTrue(inside, f"{rel}: '{m.group()}' is a count the build knows that no claim here checks")

    def test_no_source_types_a_count_by_hand(self):
        """pages.py and the two page sources hold no count of a noun stats.json, genesis.json or PAGES already gives."""
        typed = re.compile(r"(?i)\b(?:over |more than |nearly )?(?:\d[\d,]*\+?|a thousand|thousand|hundreds?|" + WORD + ") "
                           r"(?:stack cards|areas|feature pages|moonshots|tests in the gate|lanes merged|tasks planned)\b")
        for rel in ("site/pages.py", "site/index.html", "site/genesis/index.html"):
            for n, line in enumerate(read_text(os.path.join(ROOT, rel)).splitlines(), 1):
                self.assertIsNone(typed.search(line), f"{rel}:{n} types a count the build knows: {typed.search(line)}")

    def test_the_genesis_page_counts_its_levels_from_its_script(self):
        text = self.pages.get("genesis/index.html", "")
        said = re.search(r"\b(\w+) levels, one generation at a time", text, re.I)
        self.assertTrue(said, "the Genesis page lost its levels heading")
        drawn = set(re.findall(r"Level (\d+)", read_text(os.path.join(ROOT, "site", "genesis", "genesis.js"))))
        self.assertEqual(num(said.group(1)), len(drawn), f"the tower draws levels {sorted(drawn)}")


if __name__ == "__main__":
    unittest.main()
