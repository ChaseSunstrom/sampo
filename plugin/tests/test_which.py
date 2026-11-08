"""T-1699: fm which TASK ranks the skills (read from the skills folder at run time), the playbooks' headers and the
fm verbs' help against a task's words and prints what to load, each with the command that loads it."""
import json
import os

from helpers import ForemanTestCase

from sampo import core as c

SKILLS = os.path.join(os.path.dirname(__file__), "..", "skills")


class Which(ForemanTestCase):
    def skills(self, **found):
        """A skills folder of {name: (description, user_only)}: fm which reads whatever the folder holds."""
        root = os.path.join(self.tmp, "skills")
        for name, (desc, user_only) in found.items():
            os.makedirs(os.path.join(root, name))
            with open(os.path.join(root, name, "SKILL.md"), "w") as f:
                f.write(f"---\nname: {name}\ndescription: {desc}\n"
                        + ("disable-model-invocation: true\n" if user_only else "") + "---\n\n# body\n")
        return {"FOREMAN_SKILLS": root}

    def test_a_3d_hero_with_motion_gets_the_design_skill_and_the_scene_and_motion_playbooks(self):
        env = self.skills(**{"ui-design": ("Use whenever you build or restyle a UI: 3D scenes, motion, layout.", False),
                             "status": ("Use whenever you report where work stands.", False)})
        out = self.fm("which", "make a 3D hero with motion", env=env).stdout
        self.assertIn("Skill sampo:ui-design", out)
        self.assertNotIn("sampo:status", out, "a skill sharing no word isn't listed")
        self.assertIn("fm playbooks show ui/scene-3d", out)
        self.assertIn("fm playbooks show ui/motion", out)
        self.assertNotIn("INDEX", out, "procedures, not the index")
        got = json.loads(self.fm("which", "make a 3D hero with motion", "--json", env=env).stdout)
        self.assertIn("fm playbooks show ui/motion", [x["load"] for x in got["playbooks"]])

    def test_the_real_skills_folder_is_read_at_run_time(self):
        out = self.fm("which", "six style directions for a new look").stdout
        self.assertIn("Skill sampo:directions", out)
        got = json.loads(self.fm("which", "make a 3D hero with motion", "--json").stdout)
        self.assertIn("Skill sampo:ui-design", [x["load"] for x in got["skills"]])
        loads = [x["load"] for x in got["playbooks"]]
        self.assertIn("fm playbooks show ui/scene-3d", loads)
        self.assertIn("fm playbooks show ui/motion", loads)
        self.assertFalse(got["skills"][0]["summary"].startswith('"'), "a YAML-quoted description, unquoted")

    def test_a_user_only_skill_is_marked_as_typed(self):
        env = self.skills(tidy=("Use whenever the user asks to clean up Foreman memory and archives.", True))
        out = self.fm("which", "clean up the archives", env=env).stdout
        self.assertIn("(you type /sampo:tidy)", out)
        self.assertNotIn("Skill sampo:tidy", out)

    def test_a_verb_is_listed_with_its_command(self):
        out = self.fm("which", "notify me when a run finishes").stdout
        self.assertIn("fm notify", out)

    def test_the_merge_train_is_found_by_what_it_does(self):  # T-3467: "train [ID,ID…]" split at its comma
        self.assertIn("fm lane train", self.fm("which", "land several finished lanes at once").stdout)

    def test_a_task_subcommand_is_found_by_its_help(self):  # T-1908: only top-level commands were indexed
        self.assertIn("fm task drop", self.fm("which", "drop a task").stdout)
        self.assertIn("fm task evidence", self.fm("which", "record evidence for a step").stdout)
        self.assertIn("fm task defer", self.fm("which", "defer a task").stdout)
        self.assertIn("fm task drop", self.fm("help", "drop a task").stdout)
        self.assertIn("drop a task, with why", self.fm("task", "--help").stdout)

    def test_an_action_choice_is_found_as_its_own_command(self):  # T-1916: fm lane's actions are choices, not subparsers
        self.assertIn("fm lane merge", self.fm("which", "merge a lane").stdout)
        self.assertIn("fm lane resume", self.fm("which", "take back a stopped builder's work").stdout)
        self.assertIn("fm lane merge", self.fm("help", "merge a lane").stdout)
        self.assertNotIn("fm lane list", self.fm("which", "a git worktree beside the repo").stdout, "no echo per action")
        # review: actions listed in the positional's own help count; one that only restates its command doesn't
        self.assertIn("fm playbooks blame", self.fm("which", "blame each playbook line's task from git").stdout)
        self.assertNotIn("fm round close", self.fm("which", "close a round").stdout)

    def test_the_rules_plain_words_find_their_command_and_the_strongest_kind_prints_first(self):  # T-1908
        self.assertIn("fm drive", self.fm("which", "stop auto-continuing").stdout)
        self.assertIn("fm serve", self.fm("which", "serve this repo").stdout)  # review: the second verb on its line
        self.assertTrue(self.fm("which", "what did it cost").stdout.startswith("fm commands:"))

    def test_task_set_key_forms_are_found(self):
        # T-3108: 'widen task scope', 'approve a task' and 'allow a guard category' name sampo task set ID scope=[...]
        self.assertIn("fm task set", self.fm("which", "widen task scope").stdout)
        self.assertIn("scope=[", self.fm("which", "widen task scope").stdout)
        self.assertIn("approved=true", self.fm("which", "approve a task").stdout)
        self.assertIn("--allow", self.fm("which", "allow a guard category on a task").stdout)

    def test_one_common_word_is_no_match(self):
        # T-1699 review: "page" alone listed the UI playbooks and fm page for a login task nothing covers
        p = self.fm("which", "add a zebra page with quokka", check=False)
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("Nothing matches", p.stdout)
        # T-2371: the stack cards are a shelf now, so the login task finds the auth card (a real fit) but no UI playbook
        out = self.fm("which", "add a login page with oauth").stdout
        self.assertIn("fm lib show auth-providers", out)
        self.assertNotIn("fm playbooks show ui/", out)

    def test_foremans_own_release_tool_is_not_a_users_release(self):
        out = self.fm("which", "ship a release").stdout
        self.assertIn("fm playbooks show release/release-and-changelog", out)
        self.assertNotIn("fm release", out)

    def test_a_folder_word_is_not_a_name_match(self):
        # T-2248: release/ named every book in it, so all tied and the alphabet cut the generic release book
        from sampo import which
        books = [{"name": f"release/{n}", "load": "", "summary": "", "text": f"release {n.replace('-', ' ')} {t}"}
                 for n, t in (("demo", "Demo: ship a short demo, release day"), ("release-notes", "Release notes: ship it"))]
        got = which.rank("ship a release", {"playbooks": books})["playbooks"]
        self.assertEqual(got[0]["name"], "release/release-notes")
        self.assertGreater(got[0]["score"], got[1]["score"])

    def test_only_a_playbooks_header_is_indexed(self):
        # a playbook without a ## section was indexed whole, so its body's common words outranked real headers
        from sampo import playbooks
        from sampo import which
        root = os.path.join(self.tmp, "books")
        os.makedirs(os.path.join(root, "references", "ui"))
        with open(os.path.join(root, "references", "ui", "long.md"), "w") as f:
            f.write("# Long\n\nUse when a thing.\n\n" + "filler words here. " * 100 + "zebra crossing\n")
        old, playbooks.ROOT = playbooks.ROOT, root
        try:
            text = which.playbooks()[0]["text"]
        finally:
            playbooks.ROOT = old
        self.assertNotIn("zebra", text)
        self.assertLessEqual(len(text), which.HEADER_MAX + 20)

    def test_a_big_library_reads_only_the_headers_the_index_returns(self):
        # T-1835: past BIG books, fm which takes its candidates from fmplaybooks.find, not a read of every header
        from unittest import mock
        from sampo import playbooks
        from sampo import which
        from test_playbook_machinery import put
        root = os.path.join(self.tmp, "books")
        for i in range(12):
            put(root, f"references/misc/b{i}.md", f"# Book {i}\n\nUse when the gizmo{i} breaks.\n\n## Steps\n\n1. x\n")
        put(root, "references/fix/kiln.md", "# Kiln repair\n\nUse when the pottery kiln overheats.\n\n## Steps\n\n1. x\n")
        with mock.patch.object(playbooks, "ROOT", root), mock.patch.object(which, "BIG", 10), \
                mock.patch.object(which, "_head", wraps=which._head) as head:
            got = which.rank("repair the pottery kiln", {"playbooks": which.playbooks("repair the pottery kiln")})
        self.assertEqual([x["name"] for x in got["playbooks"]], ["fix/kiln"])
        self.assertEqual(head.call_count, 1, "only the index's hit is read")

    def test_the_stemmer_is_fmrecalls_and_collapses_doubled_consonants(self):
        from sampo import recall
        from sampo import which
        self.assertEqual(recall.stem("shipping"), recall.stem("ship"))
        self.assertEqual(recall.stem("stopped"), "stop")
        self.assertEqual(recall.stem("calling"), "call", "l, s and z stay doubled")
        self.assertEqual(which.words("Shipping 3D motions"), recall.words("ship 3d motion", cut=6))

    def test_an_unknown_task_says_so(self):
        p = self.fm("which", "xyzzy frobnicate quux", check=False)
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertIn("Nothing matches", p.stdout)
        p = self.fm("which", "the and a with", check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("nothing to look up", p.stderr, "only stop words: refused, saying so")

    def test_the_prompt_hook_mentions_it_once_when_no_skill_is_named(self):
        self.fm("init")

        def ctx(prompt, sid="sess-1"):
            out = self.hook("UserPromptSubmit", {"prompt": prompt, "session_id": sid}).stdout
            return json.loads(out).get("hookSpecificOutput", {}).get("additionalContext", "") if out.strip() else ""
        first = ctx("add a --verbose flag to the parser")
        self.assertIn('sampo which "', first)
        self.assertLessEqual(len(first), 400)
        self.assertNotIn("fm which", ctx("add a --quiet flag to the parser"), "once a session")
        self.assertNotIn("fm which", ctx("add a --quiet flag via /sampo:intake", "sess-2"), "a skill named already")
        self.assertNotIn("fm which", ctx("why is the sky blue?", "sess-3"), "no task in it")


class Shelves(ForemanTestCase):
    """T-2371 (Mission 6, #5): one find across every shelf: past fixes, then what this machine holds (project tools, MCP
    servers, installed skills, binaries), Foreman's own, stack cards, then marketplace plugins; each row says where it
    came from and whether it needs a grant; each search leaves a tool_search receipt. Local files only."""

    def setUp(self):
        super().setUp()
        self.fm("init")
        self.cc = os.path.join(self.tmp, "cc")
        self.hm = os.path.join(self.tmp, "home")
        self.bin = os.path.join(self.tmp, "bin")
        for d in (self.cc, os.path.join(self.hm, ".local", "bin"), self.bin):
            os.makedirs(d, exist_ok=True)

    def env(self):
        return {"CLAUDE_CONFIG_DIR": self.cc, "HOME": self.hm, "PATH": self.bin + os.pathsep + os.environ["PATH"]}

    def write(self, path, text="", mode=None):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        if mode:
            os.chmod(path, mode)

    def find(self, need):
        return json.loads(self.fm("which", need, "--json", env=self.env(), check=False).stdout)

    def rows(self, need, shelf):
        return self.find(need).get(shelf) or []

    def test_a_binary_on_path_or_in_a_bin_folder_is_found_without_a_grant(self):
        self.write(os.path.join(self.bin, "zorblat"), "#!/bin/sh\n", 0o755)
        self.write(os.path.join(self.hm, ".local", "bin", "frobnix"), "#!/bin/sh\n", 0o755)
        self.write(os.path.join(self.repo, "node_modules", ".bin", "quuxer"), "#!/bin/sh\n", 0o755)
        self.write(os.path.join(self.bin, "plainfile"), "not executable\n", 0o644)
        for name in ("zorblat", "frobnix", "quuxer"):
            (row,) = self.rows(name, "binaries")
            self.assertEqual((row["source"], row["grant"]), ("binary", ""), name)
            self.assertIn(name, row["load"])
        self.assertEqual(self.rows("plainfile", "binaries"), [], "a file that doesn't run isn't a tool")

    def test_a_project_tool_from_fm_new_tool_is_found_by_its_header_and_one_line(self):
        self.write(os.path.join(self.repo, "tools", "sheetsplit.py"),
                   '#!/usr/bin/env python3\n# fm new tool\n"""sheetsplit: split a spreadsheet into one file per tab."""\n')
        self.write(os.path.join(self.repo, "tools", "other.py"), '"""split spreadsheet tabs: no header."""\n')
        (row,) = self.rows("split a spreadsheet by tab", "tools")
        self.assertEqual(row["grant"], "")
        self.assertTrue(row["source"].startswith("project tool"), row["source"])
        self.assertIn("this repo's, unreviewed: read it first", row["source"])  # the repo wrote it, not this machine
        self.assertIn("tools/sheetsplit.py", row["load"])

    def test_an_installed_plugins_skill_is_found(self):
        tree = os.path.join(self.tmp, "plug")
        self.write(os.path.join(tree, "skills", "pdfsplit", "SKILL.md"),
                   "---\nname: pdfsplit\ndescription: Split a PDF into one file per page.\n---\n\n# body\n")
        self.write(os.path.join(self.cc, "plugins", "installed_plugins.json"),
                   json.dumps({"plugins": {"docs@mk": [{"installPath": tree}]}}))
        self.write(os.path.join(self.cc, "settings.json"), json.dumps({"enabledPlugins": {"docs@mk": True}}))
        (row,) = self.rows("split a pdf per page", "installed")
        self.assertEqual((row["source"], row["grant"]), ("installed skill", ""))
        self.assertIn("docs:pdfsplit", row["name"])

    def test_an_mcp_server_in_the_project_or_the_users_config_is_found(self):
        self.write(os.path.join(self.repo, ".mcp.json"), json.dumps({"mcpServers": {"gizmo-db": {"command": "gizmod"}}}))
        self.write(os.path.join(self.hm, ".claude.json"), json.dumps(
            {"mcpServers": {"wombat-search": {"command": "w"}},
             "projects": {self.repo: {"mcpServers": {"yak-cache": {"command": "g"}}}}}))
        (row,) = self.rows("gizmo-db", "mcp")
        self.assertTrue(row["source"].startswith(".mcp.json"), row["source"])
        self.assertIn("this repo's, unreviewed: read it first", row["source"])
        self.assertEqual([r["source"] for r in self.rows("wombat-search", "mcp")], ["~/.claude.json"])
        self.assertEqual([r["name"] for r in self.rows("yak-cache", "mcp")], ["yak-cache"])
        self.assertEqual(self.rows("gizmo-db", "mcp")[0]["grant"], "")

    def test_a_pipe_or_a_huge_file_where_a_config_belongs_is_skipped_not_read(self):
        os.mkfifo(os.path.join(self.repo, ".mcp.json"))  # a read would block forever
        self.write(os.path.join(self.hm, ".claude.json"), '{"mcpServers": {"gizmo": {}}, "pad": "' + "x" * 9_000_000 + '"}')
        p = self.fm("which", "gizmo", env=self.env(), check=False, timeout=15)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)

    def test_a_shelf_that_fails_is_named_in_the_output_and_the_receipt(self):
        from unittest import mock

        from sampo import which
        p = c.find_project(self.repo)
        failed = []
        with mock.patch.dict(os.environ, self.env()), mock.patch.object(which, "tools", side_effect=OSError("x")):
            got = which.search("record evidence for a step", p, failed)
        self.assertEqual((failed, got["tools"]), (["tools"], []))
        self.assertIn("fm task evidence", [r["load"] for r in got["commands"]], "the other shelves still answer")
        self.assertIn("skipped: tools", which.skipped_note(failed))
        self.assertEqual(which.skipped_note([]), "")

    def test_a_malformed_config_is_skipped_not_fatal(self):
        self.write(os.path.join(self.repo, ".mcp.json"), "{not json")
        self.write(os.path.join(self.hm, ".claude.json"), json.dumps({"mcpServers": ["a"], "projects": 3}))
        p = self.fm("which", "gizmo", env=self.env(), check=False)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("Nothing matches", p.stdout)

    def test_a_past_fix_is_found_and_leads_the_list(self):
        self.fm("fix", "add", "zorblat: command not found", "--do", "install zorblat with pip install zorblat-cli")
        self.write(os.path.join(self.bin, "zorblat"), "#!/bin/sh\n", 0o755)
        self.write(os.path.join(self.repo, ".mcp.json"), json.dumps({"mcpServers": {"zorblat-mcp": {"command": "z"}}}))
        out = self.fm("which", "zorblat", env=self.env()).stdout
        self.assertIn("pip install zorblat-cli", out)
        self.assertIn("learned here", out)
        self.assertLess(out.index("Solved before"), out.index("On this machine"), out)
        self.assertEqual(self.rows("zorblat", "fixes")[0]["grant"], "")

    def test_a_marketplace_plugin_is_last_and_says_an_install_needs_a_grant(self):
        self.write(os.path.join(self.cc, "plugins", "marketplaces", "mk", ".claude-plugin", "marketplace.json"),
                   json.dumps({"name": "mk", "plugins": [{"name": "zorblat-tools", "description": "zorblat helpers",
                                                          "source": "./zt"}]}))
        self.write(os.path.join(self.bin, "zorblat"), "#!/bin/sh\n", 0o755)
        out = self.fm("which", "zorblat", env=self.env()).stdout
        self.assertLess(out.index("On this machine"), out.index("Marketplace plugins"), out)
        (row,) = self.rows("zorblat", "plugins")
        self.assertEqual((row["source"], row["grant"]), ("marketplace mk", "fm ask ID plugin"))
        self.assertIn("fm ask ID plugin", out)

    def test_foremans_own_verbs_still_answer_and_a_stack_card_is_a_shelf(self):
        got = self.find("record evidence for a step")
        self.assertIn("fm task evidence", [r["load"] for r in got["commands"]])
        cards = self.find("fastapi")
        self.assertTrue(cards["library"], "stack cards are searched")
        self.assertEqual(cards["library"][0]["grant"], "")

    def test_each_search_leaves_a_receipt_of_what_it_searched_and_found(self):
        self.write(os.path.join(self.bin, "zorblat"), "#!/bin/sh\n", 0o755)
        self.fm("which", "zorblat", env=self.env())
        self.fm("which", "xyzzy frobnicate quux", env=self.env(), check=False)
        evs = c.ledger_events(c.find_project(self.repo), ["tool_search"])
        self.assertEqual([e["data"]["need"] for e in evs], ["zorblat", "xyzzy frobnicate quux"])
        first, second = evs[0]["data"], evs[1]["data"]
        self.assertEqual(first["hits"]["binaries"], 1)
        self.assertEqual(sum(second["hits"].values()), 0, "a search that found nothing is a receipt too")
        for shelf in ("fixes", "tools", "mcp", "installed", "binaries", "skills", "playbooks", "commands", "library",
                      "plugins"):
            self.assertIn(shelf, first["sources"])

    def test_a_search_makes_no_network_call_and_starts_no_process(self):
        import socket
        import subprocess
        from unittest import mock

        from sampo import which
        with mock.patch.dict(os.environ, self.env()), mock.patch("os.getcwd", return_value=self.repo), \
                mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network")), \
                mock.patch.object(socket, "getaddrinfo", side_effect=AssertionError("network")), \
                mock.patch.object(subprocess, "run", side_effect=AssertionError("process")), \
                mock.patch.object(subprocess, "Popen", side_effect=AssertionError("process")):
            got = which.search("record evidence for a step", c.find_project(self.repo))
        self.assertIn("fm task evidence", [r["load"] for r in got["commands"]])

    def test_the_cheap_local_hit_the_hook_reads_needs_no_plugin_index(self):
        from sampo import which
        self.write(os.path.join(self.hm, ".cargo", "bin", "rg2"), "#!/bin/sh\n", 0o755)
        self.write(os.path.join(self.repo, "tools", "sheetsplit.py"), '# fm new tool\n"""sheetsplit: split sheets."""\n')
        self.fm("fix", "add", "jq: command not found", "--do", "apt install jq")
        p = c.find_project(self.repo)
        from unittest import mock
        with mock.patch.dict(os.environ, {"HOME": self.hm}), mock.patch("sampo.plugins.index", side_effect=AssertionError):
            self.assertIn("rg2", which.local_hit(p, "rg2"))
            self.assertEqual(which.local_hit(p, "sheetsplit"), "", "a repo's own tool is never put in the hook's note")
            self.assertIn("apt install jq", which.local_hit(p, "jq"))
            self.assertEqual(which.local_hit(p, "nothing-here"), "")


if __name__ == "__main__":
    import unittest
    unittest.main()
