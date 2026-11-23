"""T-5835 (part 1 of T-5768): the spawn table. One pure module says which model each agent type runs on, fills a spawn
that named none, and refuses a lookup sent to a general agent; the hook, the headless builders and the cost view read it."""
import os
import tempfile
import unittest
from unittest import mock

import helpers  # noqa: F401  (puts plugin/lib on the import path)
from sampo import core as c
from sampo import spawn

AGENTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agents")
HAIKU = ("fm-scout", "fm-resolver", "Explore", "claude-code-guide", "Plan")  # fm-resolver follows its agent file (haiku)
# The fm-* agents and Plan all default to haiku now (T-6101): their files and the TABLE say so.
AGENT_FILES = ("fm-builder", "fm-reviewer", "fm-recon", "fm-fixer", "fm-reproducer")


def spawn_of(type_=None, prompt="Where is cmd_x defined?", **more):
    d = {"description": "look", "prompt": prompt}
    if type_ is not None:
        d["subagent_type"] = type_
    d.update(more)
    return d


class Fill(unittest.TestCase):
    def setUp(self):
        env = {k: v for k, v in os.environ.items() if k != "SAMPO_SPAWN_TABLE"}
        p = mock.patch.dict(os.environ, env, clear=True)
        p.start()
        self.addCleanup(p.stop)

    def test_fill_gives_each_type_its_table_model(self):
        for t in HAIKU:
            with self.subTest(t=t):
                self.assertEqual(spawn.fill(spawn_of(t))["model"], "haiku")
        for t in AGENT_FILES:
            with self.subTest(t=t):
                self.assertEqual(spawn.fill(spawn_of(t))["model"], "haiku")

    def test_fill_reads_sampo_and_foreman_namespaced_types(self):
        for ns in ("sampo", "foreman"):
            self.assertEqual(spawn.fill(spawn_of(ns + ":fm-scout"))["model"], "haiku")
            self.assertEqual(spawn.fill(spawn_of(ns + ":fm-builder"))["model"], "haiku")
        self.assertEqual(spawn.model_for("sampo:fm-scout"), "haiku")
        self.assertEqual(spawn.model_for("fm-scout"), "haiku")

    def test_fill_leaves_an_explicit_model_alone(self):
        for model in ("opus", "sonnet", "haiku", "claude-opus-4-1"):
            tin = spawn_of("fm-scout", model=model)
            out = spawn.fill(tin)
            self.assertEqual(out["model"], model)
            self.assertEqual(out, tin)

    def test_fill_keeps_the_other_fields_and_never_changes_its_input(self):
        tin = spawn_of("fm-scout", run_in_background=True)
        before = dict(tin)
        out = spawn.fill(tin)
        self.assertEqual(tin, before, "the input stays as it came")
        self.assertEqual({k: v for k, v in out.items() if k != "model"}, before)

    def test_fill_an_unknown_or_untyped_spawn_is_returned_untouched(self):
        for tin in (spawn_of("general-purpose"), spawn_of(), spawn_of("some-plugin:thing"), spawn_of("")):
            with self.subTest(tin=tin):
                self.assertIs(spawn.fill(tin), tin)
                self.assertNotIn("model", tin)

    def test_fill_a_blank_model_counts_as_none(self):
        self.assertEqual(spawn.fill(spawn_of("fm-scout", model=""))["model"], "haiku")
        self.assertEqual(spawn.fill(spawn_of("fm-scout", model=None))["model"], "haiku")

    def test_fill_off_switch_returns_the_input_untouched(self):
        for off in ("off", "OFF", "0", "false"):
            with mock.patch.dict(os.environ, {"SAMPO_SPAWN_TABLE": off}):
                tin = spawn_of("fm-scout")
                self.assertFalse(spawn.enabled())
                self.assertIs(spawn.fill(tin), tin)
                self.assertNotIn("model", tin)
        with mock.patch.dict(os.environ, {"SAMPO_SPAWN_TABLE": "on"}):
            self.assertTrue(spawn.enabled())
            self.assertEqual(spawn.fill(spawn_of("fm-scout"))["model"], "haiku")
        self.assertTrue(spawn.enabled(), "unset: on")

    def test_fill_malformed_input_never_raises(self):
        for bad in (None, "x", 7, [], [1], {}, {"subagent_type": 5}, {"subagent_type": None}, {"subagent_type": ["fm-scout"]},
                    {"subagent_type": "fm-scout", "model": 5}, {"subagent_type": "a:b:c:fm-scout"}):
            with self.subTest(bad=bad):
                out = spawn.fill(bad)
                if isinstance(bad, dict) and bad.get("subagent_type") == "fm-scout":
                    self.assertEqual(out["model"], 5, "a model the caller set stands, odd as it is")
                else:
                    self.assertIs(out, bad)

    def test_the_table_names_only_models_a_spawn_accepts(self):
        for t, m in spawn.TABLE.items():
            self.assertIn(m, ("haiku", "sonnet", "opus"), t)
        self.assertIsNone(spawn.model_for("general-purpose"))
        self.assertIsNone(spawn.model_for(None))
        self.assertIsNone(spawn.model_for(["fm-scout"]))


def frontmatter_model(path):
    """The `model:` line of a file's frontmatter, read independently of spawn.py."""
    with open(path) as f:
        lines = f.read().split("\n")
    if lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        if line.strip() == "---":
            return None
        if line.startswith("model:"):
            return line.split(":", 1)[1].strip().strip("\"'")
    return None


class AgentFiles(unittest.TestCase):
    """T-5835: plugin/agents/*.md frontmatter is the one source for Sampo's own agent types; the table holds built-ins."""

    def setUp(self):
        env = {k: v for k, v in os.environ.items() if k != "SAMPO_SPAWN_TABLE"}
        p = mock.patch.dict(os.environ, env, clear=True)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(spawn.reset)
        spawn.reset()

    def test_fill_every_agent_file_with_a_model_line_resolves_to_that_model(self):
        names = sorted(f for f in os.listdir(AGENTS) if f.endswith(".md"))
        self.assertGreaterEqual(len(names), 8)
        seen = 0
        for f in names:
            model = frontmatter_model(os.path.join(AGENTS, f))
            if not model:
                continue
            seen += 1
            for t in (f[:-3], "sampo:" + f[:-3], "foreman:" + f[:-3]):
                with self.subTest(t=t):
                    self.assertEqual(spawn.fill(spawn_of(t))["model"], model)
                    self.assertEqual(spawn.model_for(t), model)
        self.assertGreaterEqual(seen, 8)

    def test_fill_the_table_holds_no_row_for_a_sampo_agent(self):
        self.assertEqual({t for t in spawn.TABLE if t.startswith("fm-")}, set())

    def test_fill_a_changed_agent_file_changes_the_model_after_a_reset(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "fm-x.md"), "w") as f:
                f.write("---\nname: fm-x\ndescription: d\nmodel: opus\n---\nbody\n")
            with open(os.path.join(d, "fm-nomodel.md"), "w") as f:
                f.write("---\nname: fm-nomodel\ndescription: d\n---\nbody model: haiku\n")
            with mock.patch.object(spawn, "AGENTS_DIR", d):
                spawn.reset()
                self.assertEqual(spawn.fill(spawn_of("fm-x"))["model"], "opus")
                tin = spawn_of("fm-nomodel")
                self.assertIs(spawn.fill(tin), tin, "a body line is no frontmatter")
                tin = spawn_of("fm-scout")
                self.assertIs(spawn.fill(tin), tin, "an agent the folder lacks has no row")
            spawn.reset()

    def test_fill_a_missing_or_unreadable_agents_dir_falls_back_to_the_table_without_raising(self):
        with tempfile.TemporaryDirectory() as d:
            afile = os.path.join(d, "afile")
            with open(afile, "w") as f:
                f.write("x")
            for bad in (os.path.join(d, "nope"), afile, None, ""):
                with self.subTest(bad=bad), mock.patch.object(spawn, "AGENTS_DIR", bad):
                    spawn.reset()
                    self.assertEqual(spawn.fill(spawn_of("Explore"))["model"], "haiku")
                    self.assertEqual(spawn.fill(spawn_of("Plan"))["model"], "haiku")
                    tin = spawn_of("fm-scout")
                    self.assertIs(spawn.fill(tin), tin)
                    self.assertIsNone(spawn.model_for("fm-builder"))
                    self.assertIsNone(spawn.lookup_refusal(spawn_of("fm-scout")))
                    self.assertIn("fm-scout", spawn.lookup_refusal(spawn_of(None, prompt="Where is x?")))

    def test_fill_a_binary_or_odd_agent_file_never_raises(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "bin.md"), "wb") as f:
                f.write(b"---\n\xff\xfe\nmodel: haiku\n---\n")
            with open(os.path.join(d, "empty.md"), "w"):
                pass
            os.mkdir(os.path.join(d, "dir.md"))
            with open(os.path.join(d, "ok.md"), "w") as f:
                f.write("---\nname: ok\nmodel: 'sonnet'\n---\n")
            with mock.patch.object(spawn, "AGENTS_DIR", d):
                spawn.reset()
                self.assertEqual(spawn.fill(spawn_of("ok"))["model"], "sonnet")
                self.assertEqual(spawn.fill(spawn_of("bin"))["model"], "haiku")
            spawn.reset()

    def test_fill_reads_the_agents_dir_once_per_process(self):
        real = os.listdir
        calls = []

        def counting(p="."):
            calls.append(p)
            return real(p)
        with mock.patch.object(os, "listdir", counting):
            spawn.reset()
            for _ in range(5):
                spawn.fill(spawn_of("fm-scout"))
                spawn.lookup_refusal(spawn_of(None))
                spawn.model_for("fm-builder")
        self.assertEqual(len([c for c in calls if c == spawn.AGENTS_DIR]), 1)


class Lookup(unittest.TestCase):
    def setUp(self):
        env = {k: v for k, v in os.environ.items() if k != "SAMPO_SPAWN_TABLE"}
        p = mock.patch.dict(os.environ, env, clear=True)
        p.start()
        self.addCleanup(p.stop)

    LOOKUPS = ("Where is cmd_which defined?", "where is the ledger written", "Which files touch the budget gate?",
               "Find the file that defines HookCase", "find all callers of fm_edit", "Find where SAMPO_HOME is read.",
               "Which files use core.atomic_write? file:line only.")

    def test_lookup_a_lookup_prompt_to_a_general_or_untyped_agent_is_refused_naming_fm_scout(self):
        for p in self.LOOKUPS:
            for t in (None, "general-purpose", "", "sampo:general-purpose"):
                with self.subTest(p=p, t=t):
                    msg = spawn.lookup_refusal(spawn_of(t, prompt=p))
                    self.assertIsInstance(msg, str)
                    self.assertIn("fm-scout", msg)

    def test_lookup_a_build_or_research_prompt_passes(self):
        for p in ("Implement a retry in sampo/ci.py and add a test", "Find and fix the off-by-one in budget.py",
                  "Find out why the hook is slow and explain the cause", "Investigate why test_hooks flakes",
                  "Write a script that finds all dead imports", "Refactor the ledger and find a better name for it",
                  "Review the diff and say which files need more tests", "Design a cache: where should it live and why?",
                  "Research how other tools do this", "", "   "):
            with self.subTest(p=p):
                self.assertIsNone(spawn.lookup_refusal(spawn_of("general-purpose", prompt=p)))

    def test_lookup_a_long_prompt_is_not_a_lookup(self):
        long = "Where is cmd_x defined? " + "Context about the whole migration we are in. " * 30
        self.assertIsNone(spawn.lookup_refusal(spawn_of(None, prompt=long)))

    def test_lookup_a_typed_agent_passes(self):
        for t in ("fm-scout", "sampo:fm-scout", "fm-recon", "Explore", "Plan", "fm-builder", c.ns_names("fm-reviewer")[-1], "some:thing"):
            with self.subTest(t=t):
                self.assertIsNone(spawn.lookup_refusal(spawn_of(t, prompt="Where is cmd_x defined?")))

    def test_lookup_malformed_input_never_raises_and_gives_none(self):
        for bad in (None, "x", 7, [], {}, {"prompt": None}, {"prompt": 5}, {"prompt": ["where is x"]},
                    {"prompt": "Where is x?", "subagent_type": 5}, {"prompt": b"where is x"}, {"prompt": "\x00" * 50}):
            with self.subTest(bad=bad):
                self.assertIsNone(spawn.lookup_refusal(bad))

    def test_lookup_off_switch_refuses_nothing(self):
        with mock.patch.dict(os.environ, {"SAMPO_SPAWN_TABLE": "off"}):
            self.assertIsNone(spawn.lookup_refusal(spawn_of(None, prompt="Where is cmd_x defined?")))

    def test_lookup_the_refusal_names_the_namespaced_agent_to_call(self):
        msg = spawn.lookup_refusal(spawn_of("general-purpose", prompt="Which files touch the ledger?"))
        self.assertIn("sampo:fm-scout", msg)
        self.assertLess(len(msg), 600)


if __name__ == "__main__":
    unittest.main()
