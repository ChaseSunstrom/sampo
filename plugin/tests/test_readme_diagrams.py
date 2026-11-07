"""T-3248: the README's hand-drawn diagrams (loop, guard, architecture) come in Sampo's modern tokens, dark and light,
say Sampo (never Foreman or the old fm), and pass sampo works check's source gates. NAME.svg stays the dark one so
links to it keep working."""
# map-links: site/assets/diagrams/loop.svg site/assets/diagrams/guard.svg site/assets/diagrams/architecture.svg
import os
import re
import unittest

import helpers  # noqa: F401  (puts plugin/lib on the path)
from sampo import works

DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "site", "assets", "diagrams")
NAMES = ("loop", "guard", "architecture")
NIGHT = {"dark": "#0B0E14", "light": "#F6F7F9"}  # live.css [data-theme="modern"] --night, each scheme
KIND = "card"  # pinned: a type that appears later must not silently change what these diagrams are gated as


def read(name):
    with open(os.path.join(DIR, name), encoding="utf-8") as f:
        return f.read()


class ReadmeDiagrams(unittest.TestCase):
    def test_both_themes_and_the_old_name_is_the_dark_one(self):
        for name in NAMES:
            for theme, night in NIGHT.items():
                with self.subTest(name=name, theme=theme):
                    self.assertIn(night, read(f"{name}-{theme}.svg"))
            with self.subTest(name=name):
                self.assertEqual(read(f"{name}.svg"), read(f"{name}-dark.svg"))

    def test_says_sampo_never_foreman(self):
        svgs = sorted(f for f in os.listdir(DIR) if f.endswith(".svg"))
        self.assertGreaterEqual(len(svgs), 9)
        for f in svgs:
            with self.subTest(f=f):
                text = read(f)
                self.assertNotRegex(text, re.compile("foreman", re.I))
                self.assertNotRegex(text, r"\bfm\b")
                self.assertIn("Sampo", text)

    def test_source_gates_pass(self):
        for name in NAMES:
            for theme in NIGHT:
                with self.subTest(name=name, theme=theme):
                    data = read(f"{name}-{theme}.svg").encode("utf-8")
                    bad = [g for g in works.inspect(data, KIND)["gates"] if not g["ok"]]
                    self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
