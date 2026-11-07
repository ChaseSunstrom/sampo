"""T-0094: fm help lists every command once, in tiers (the everyday ones first); fm alone prints it."""
import argparse
import re
import os
import sys
import unittest

from helpers import ForemanTestCase

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))


class Help(ForemanTestCase):
    def test_every_command_once_in_tiers_everyday_first(self):
        from sampo import cli
        sub = next(a for a in cli.build_parser()._actions if isinstance(a, argparse._SubParsersAction))
        out = self.fm("help").stdout
        tiers = out.split("\nSampo names")[0]  # T-2775: the Sampo-names block lists aliases, not commands
        lines = [ln.split()[0] for ln in tiers.splitlines() if ln.startswith("  ")]
        self.assertEqual(sorted(lines), sorted(sub.choices), "every command, each once (a new one needs a tier)")
        self.assertTrue(out.startswith("Every task"), out[:80])
        self.assertLess(out.index("  next "), out.index("  sentinel "))
        self.assertEqual(self.fm().stdout, out, "fm alone prints it")

    # the registration checks (tiers, one position each) live in test_cli.HelpRegistrations (T-1776)

    def test_help_text_carries_no_ticket_ids(self):
        # T-0873 (round 4): "(T-0594)" in help reads as noise to anyone who isn't Foreman's maintainer
        from sampo import cli
        sub = next(a for a in cli.build_parser()._actions if isinstance(a, argparse._SubParsersAction))
        texts = {"fm help --all": self.fm("help", "--all").stdout, "fm --help": self.fm("--help").stdout}
        texts.update({f"fm {name} -h": p.format_help() for name, p in sub.choices.items()})
        leaks = {where: re.findall(r"\bT-\d{3,4}\b", t) for where, t in texts.items() if re.search(r"\bT-\d{3,4}\b", t)}
        self.assertEqual(leaks, {})

    def test_parser_builds_skip_argparse_translation_lookups_and_help_reads_the_same(self):
        # T-3677: with no catalog, each argparse message cost a gettext find() for nothing (~17 ms a full build);
        # the build binds it to identity, so every command's help must read byte for byte as before
        import gettext
        from unittest import mock
        from sampo import cli

        def helps(ap, name="fm"):
            out = {name: ap.format_help()}
            for a in ap._actions:
                if isinstance(a, argparse._SubParsersAction):
                    for sub, p in a.choices.items():
                        out.update(helps(p, f"{name} {sub}"))
            return out

        with mock.patch.object(gettext, "translation", wraps=gettext.translation) as looked:
            raw = helps(cli._build_parser())
            self.assertTrue(looked.called, "the unbound build looks a catalog up per message")
            looked.reset_mock()
            with mock.patch.object(cli, "_PARSER", None), mock.patch.dict(cli._PARTS, clear=True):
                built = cli.build_parser()
                cli._parser_for(["task", "new", "x"])
                self.assertEqual(looked.call_count, 0, "no lookup while building")
                self.assertEqual(helps(built), raw)
        self.assertIs(argparse._, gettext.gettext, "restored after the build")
        self.assertGreater(len(raw), 200, "every command and subcommand compared")

    def test_a_catalog_keeps_argparse_translating(self):
        import gettext
        from unittest import mock
        from sampo import cli
        bound = lambda: (argparse._, argparse.ngettext)  # noqa: E731
        plain = cli._untranslated(bound)
        self.assertEqual((plain[0]("x"), plain[1]("a", "as", 1), plain[1]("a", "as", 2)), ("x", "a", "as"))
        with mock.patch.object(gettext, "find", return_value="/x/messages.mo"):
            self.assertEqual(cli._untranslated(bound), (gettext.gettext, gettext.ngettext), "a catalog: translated")
        self.assertEqual(bound(), (gettext.gettext, gettext.ngettext), "restored")
        with self.assertRaises(ZeroDivisionError):
            cli._untranslated(lambda: 1 / 0)
        self.assertEqual(bound(), (gettext.gettext, gettext.ngettext), "restored after a failed build")

    def test_a_command_name_prints_its_help(self):  # T-1909: "task" is a stop word, so fm help task was refused
        self.assertIn("drop a task, with why", self.fm("help", "task").stdout)  # T-1913: 3.12's usage line differs
        self.assertIn("--done-in ID", self.fm("help", "task", "drop").stdout)
        self.assertNotIn("usage:", self.fm("help", "notify", "me", "when", "done").stdout, "words past a name: a question")

    def test_status_is_state(self):
        # T-0393: "status" is the word the rules and /foreman:status use; `fm status` was an invalid choice
        self.fm("init")
        self.fm("capture", "something")  # T-0847: an empty project's status is one line on how to start
        p = self.fm("status", check=False)
        self.assertEqual(p.returncode, 0, p.stderr[-300:])
        stamp = lambda t: re.sub(r"_Generated \S+", "", t)  # noqa: E731 — the two calls may straddle a second
        self.assertEqual(stamp(p.stdout), stamp(self.fm("state").stdout))


if __name__ == "__main__":
    unittest.main()
