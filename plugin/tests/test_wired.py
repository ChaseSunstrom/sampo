"""T-5016 (a slice of T-4322): `sampo wired` lists the public functions in plugin/lib/sampo that no verb and no hook reaches,
as file:line rows. A static ast scan over a temp plugin tree: a function counts as wired when a chain of references (a name, a
module attribute, a lazy("sampo.x", "cmd_y") pair, a from-import) leads to it from cli.main, a hook or bin script, or a module's
`__main__` block; tests never count as callers. No test runs a model or the network."""
import json
import os
import tempfile
import unittest

from helpers import SampoTestCase

from sampo import wired

FILES = {
    "lib/sampo/cli.py": (
        "def lazy(module, func):\n"
        "    def run(args):\n"
        "        return None\n"
        "    return run\n"
        "\n"
        "def cmd_help(args):\n"
        "    return 1\n"
        "\n"
        "def main(argv=None):\n"
        "    from sampo import util\n"
        "    add('x', lazy('sampo.feat', 'cmd_x'))\n"
        "    add('help', cmd_help)\n"
        "    util.helper()\n"
        "    add('t', TABLE['t'])\n"
    ),
    "lib/sampo/feat.py": (
        "from sampo import util as u\n"
        "\n"
        "TABLE = {'k': table_target}\n"
        "\n"
        "def cmd_x(args):\n"
        "    return _inner() + u.second()\n"
        "\n"
        "def _inner():\n"
        "    return used()\n"
        "\n"
        "def used():\n"
        "    return 1\n"
        "\n"
        "def table_target():\n"
        "    return 2\n"
        "\n"
        "def cmd_nobody(args):\n"
        "    return 3\n"
        "\n"
        "def orphan():\n"
        "    return 4\n"
        "\n"
        "def chain_a():\n"
        "    return chain_b()\n"
        "\n"
        "def chain_b():\n"
        "    return 5\n"
        "\n"
        "def _private_unused():\n"
        "    return 6\n"
        "\n"
        "class Box:\n"
        "    def method(self):\n"
        "        return by_method()\n"
        "\n"
        "def by_method():\n"
        "    return 7\n"
        "\n"
        "def made_by_class():\n"
        "    return Box()\n"
    ),
    "lib/sampo/util.py": (
        "def helper():\n"
        "    return 1\n"
        "\n"
        "def second():\n"
        "    return 2\n"
        "\n"
        "def other_unused():\n"
        "    return 3\n"
        "\n"
        "def tool():\n"
        "    return 4\n"
        "\n"
        "if __name__ == '__main__':\n"
        "    tool()\n"
    ),
    "lib/sampo/hooks.py": (
        "def run(event):\n"
        "    return _go()\n"
        "\n"
        "def _go():\n"
        "    return shipped()\n"
        "\n"
        "def shipped():\n"
        "    return 1\n"
        "\n"
        "def hook_dead():\n"
        "    return 2\n"
    ),
    "hooks/hook": (
        "#!/usr/bin/env python3\n"
        "from sampo import hooks\n"
        "import sampo.util\n"
        "hooks.run('Stop')\n"
        "sampo.util.second()\n"
    ),
    "bin/sampo": (
        "#!/usr/bin/env python3\n"
        "from sampo import cli\n"
        "cli.main()\n"
    ),
    "tests/test_x.py": (
        "from sampo import feat, util\n"
        "feat.orphan()\n"
        "util.other_unused()\n"
        "feat.cmd_nobody(None)\n"
    ),
    "lib/fmfeat.py": (
        '"""Old import path, kept for two releases (T-2742): the module is sampo.feat."""\n'
        "import sys\nimport sampo.feat\nsys.modules[__name__] = sys.modules[\"sampo.feat\"]\n"
    ),
}


def build(root, files=FILES):
    for rel, text in files.items():
        path = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)


class Wired(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.plugin = os.path.join(os.path.realpath(self._tmp.name), "plugin")  # a repo root above it, as in the real tree
        build(self.plugin)

    def rows(self):
        return {(r["module"], r["name"]): r for r in wired.scan(self.plugin)}

    def names(self):
        return sorted(f"{m}.{n}" for m, n in self.rows())

    def test_unwired_functions_listed_wired_ones_not(self):
        got = self.names()
        for gone in ("feat.orphan", "feat.cmd_nobody", "feat.chain_a", "feat.chain_b", "util.other_unused", "hooks.hook_dead"):
            self.assertIn(gone, got)
        for kept in ("cli.main", "cli.cmd_help", "feat.cmd_x", "feat.used", "feat.table_target", "util.helper",
                     "util.second", "util.tool", "hooks.run", "hooks.shipped", "cli.lazy"):
            self.assertNotIn(kept, got, f"{kept} is reached from a verb or hook")

    def test_private_functions_and_methods_are_never_rows(self):
        got = self.names()
        self.assertNotIn("feat._private_unused", got)
        self.assertNotIn("feat.method", got)
        self.assertNotIn("feat.Box", got)

    def test_a_function_only_a_method_calls_follows_the_class(self):
        # Box is never made on a wired path, so its method's callee is unwired; made_by_class is itself unwired
        self.assertIn("feat.by_method", self.names())
        self.assertIn("feat.made_by_class", self.names())

    def test_tests_never_count_as_callers(self):
        self.assertIn(("feat", "orphan"), self.rows())
        self.assertIn(("util", "other_unused"), self.rows())

    def test_a_chain_names_who_calls_it_and_a_lone_one_has_no_callers(self):
        rows = self.rows()
        self.assertEqual(rows[("feat", "chain_b")]["callers"], ["feat.chain_a"])
        self.assertEqual(rows[("feat", "chain_a")]["callers"], [])
        self.assertEqual(rows[("feat", "orphan")]["callers"], [])

    def test_rows_are_file_line_and_kind(self):
        r = self.rows()[("feat", "cmd_nobody")]
        self.assertEqual(r["file"], "plugin/lib/sampo/feat.py")
        self.assertEqual(r["line"], 17)
        self.assertEqual(r["kind"], "cmd")
        self.assertEqual(self.rows()[("feat", "orphan")]["kind"], "function")

    def test_a_lazy_pair_wires_its_function_and_a_new_caller_unwires_the_row(self):
        self.assertNotIn(("feat", "cmd_x"), self.rows())
        build(self.plugin, {"lib/sampo/cli.py": FILES["lib/sampo/cli.py"] + "    lazy('sampo.feat', 'orphan')\n"})
        self.assertNotIn(("feat", "orphan"), self.rows())

    def test_a_lazy_pair_with_a_built_name_wires_every_def_with_that_prefix(self):
        self.assertIn(("feat", "cmd_nobody"), self.rows())
        build(self.plugin, {"lib/sampo/cli.py": FILES["lib/sampo/cli.py"] + "    lazy('sampo.feat', 'cmd_' + name)\n"})
        self.assertNotIn(("feat", "cmd_nobody"), self.rows())
        self.assertIn(("feat", "orphan"), self.rows())

    def test_a_hook_only_script_reaches_through_its_module_attribute(self):
        build(self.plugin, {"hooks/hook": FILES["hooks/hook"] + "hooks.hook_dead()\n"})
        self.assertNotIn(("hooks", "hook_dead"), self.rows())

    def test_a_module_level_dispatch_table_in_a_reached_module_wires_its_entries(self):
        self.assertNotIn(("feat", "table_target"), self.rows())

    def test_a_syntax_error_file_is_skipped_not_fatal(self):
        build(self.plugin, {"lib/sampo/broken.py": "def oops(:\n"})
        self.assertIn(("feat", "orphan"), self.rows())
        self.assertNotIn(("broken", "oops"), self.rows())

    def test_empty_tree_has_no_rows(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(wired.scan(d), [])

    def test_text_gives_file_line_rows(self):
        text = wired.render(wired.scan(self.plugin))
        self.assertIn("plugin/lib/sampo/feat.py:20 orphan", text)
        self.assertIn("called only by feat.chain_a", text)

    def test_the_import_idiom_chain_wires_the_def(self):
        build(self.plugin, {"lib/sampo/hooks.py": FILES["lib/sampo/hooks.py"]
                            + "\ndef via_import():\n    return __import__('sampo.util').util.other_unused()\n"
                            + "\ndef run2():\n    return via_import()\n",
                            "hooks/hook": FILES["hooks/hook"] + "hooks.run2()\n"})
        self.assertNotIn(("util", "other_unused"), self.rows())

    def test_a_shell_script_python_string_seeds_the_library(self):
        build(self.plugin, {"../setup-x.sh": "python3 -c 'from sampo import util; util.other_unused()' x\n"})
        self.assertNotIn(("util", "other_unused"), self.rows())

    def test_site_and_template_python_seed_the_library(self):
        build(self.plugin, {"../site/x.py": "from sampo import feat\nfeat.orphan()\n",
                            "templates/tui/panel.py": "from sampo import util\nutil.other_unused()\n"})
        got = self.rows()
        self.assertNotIn(("feat", "orphan"), got)
        self.assertNotIn(("util", "other_unused"), got)

    def test_getattr_with_a_built_name_wires_every_public_def(self):
        build(self.plugin, {"hooks/hook": FILES["hooks/hook"] + "from sampo import feat\ngetattr(feat, argv[1])()\n"})
        got = self.rows()
        self.assertNotIn(("feat", "orphan"), got)
        self.assertNotIn(("feat", "cmd_nobody"), got)

    def test_a_string_literal_import_and_call_wires_the_def(self):
        build(self.plugin, {"hooks/hook": FILES["hooks/hook"] + "run('python3 -c \"from sampo import feat; feat.orphan()\"')\n"})
        self.assertNotIn(("feat", "orphan"), self.rows())

    def test_skipped_files_are_reported_and_the_header_says_so(self):
        build(self.plugin, {"lib/sampo/broken.py": "def oops(:\n"})
        rows, skipped = wired.scan_full(self.plugin)
        self.assertEqual([s["file"] for s in skipped], ["plugin/lib/sampo/broken.py"])
        self.assertIn("syntax", skipped[0]["reason"].lower())
        self.assertIn("skipped 1 file", wired.render(rows, skipped=skipped))
        self.assertFalse(wired.fatal(skipped))

    def test_a_skipped_cli_or_seed_is_fatal(self):
        build(self.plugin, {"lib/sampo/cli.py": "def main(:\n"})
        self.assertTrue(wired.fatal(wired.scan_full(self.plugin)[1]))
        build(self.plugin, {"lib/sampo/cli.py": FILES["lib/sampo/cli.py"], "hooks/hook": "def (:\n"})
        self.assertTrue(wired.fatal(wired.scan_full(self.plugin)[1]))

    def test_header_carries_the_gap_caveat(self):
        self.assertIn("not seen:", wired.render(wired.scan(self.plugin)))


class WiredVerb(SampoTestCase):
    def test_verb_runs_on_the_real_tree_and_every_row_is_a_real_definition(self):
        data = json.loads(self.fm("wired", "--json").stdout)
        self.assertIsInstance(data["rows"], list)
        self.assertEqual(data["count"], len(data["rows"]))
        for r in data["rows"][:20]:
            self.assertRegex(r["file"], r"^plugin/lib/sampo/\w+\.py$")
            self.assertGreater(r["line"], 0)
        self.assertNotIn("cli.main", [f"{r['module']}.{r['name']}" for r in data["rows"]])

    def test_plain_output_one_row_per_line(self):
        out = self.fm("wired").stdout
        self.assertIn("wired", out.splitlines()[0])

    def test_an_unknown_module_is_an_error_not_an_empty_list(self):
        """a typo in --module printed "0 public functions … no verb or hook reaches", the same as a fully wired module"""
        p = self.fm("wired", "--module", "no_such_mod", check=False)
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("no_such_mod", p.stderr)
        self.assertNotIn("0 public functions", p.stdout)
        self.assertEqual(self.fm("wired", "--module", "wired.py").returncode, 0)  # a real one, with or without .py


if __name__ == "__main__":
    unittest.main()
