"""Behavioural tests for the fm CLI (run as a subprocess against an isolated FOREMAN_HOME)."""
import argparse
import json
import subprocess
import os
import re
import shutil
import sys
import threading
import time
import unittest

from helpers import CALM_HOST, ForemanTestCase, read_text, read_json

from sampo import core as c
from sampo import cli


class HelpRegistrations(unittest.TestCase):
    """fmcli.py's command registrations: here, not in test_help, so every fmcli.py change runs them (T-1776: fm map links
    tests by name and drops fmcli.py's co-change pairs, so T-1608 never ran test_help and shipped a shared position)."""
    def test_tiers_come_from_registrations(self):
        # T-1144: each command declares its tier where it is registered; no shared table to conflict on
        sub = next(a for a in cli.build_parser()._actions if isinstance(a, argparse._SubParsersAction))
        listed = [w for _, names in cli.HELP_TIERS for w in names.split()]
        self.assertEqual(sorted(listed), sorted(sub.choices))
        self.assertEqual([t for t, _ in cli.HELP_TIERS][0], "Every task")
        src = open(os.path.join(os.path.dirname(__file__), "..", "lib", "sampo", "cli.py")).read()
        self.assertNotIn('("Every task", "next', src, "no hand-kept name list")

    def test_each_command_has_its_own_help_position(self):
        # T-1686: img, shot and critique all took 93; a tie silently falls back to registration order. One position per
        # registration site: the instruments loop (fminstr.TOOLS) is one site, its tools in the registry's order
        from sampo import instr
        sub = next(a for a in cli.build_parser()._actions if isinstance(a, argparse._SubParsersAction))
        with open(os.path.join(os.path.dirname(__file__), "..", "lib", "sampo", "cli.py"), encoding="utf-8") as f:
            sites = re.findall(r'\btier=\("[^"]+", (\d+)\)', f.read())
        commands = len({id(p) for p in sub.choices.values()})  # an alias (status) is its command's parser
        self.assertEqual(len(sites), commands - len(instr.TOOLS) + 1, "every registration site read")
        self.assertEqual(sorted({p for p in sites if sites.count(p) > 1}, key=int), [], "positions shared")


class OutJson(unittest.TestCase):
    """T-1589: --json output takes any value a TOML file can hold, and puts no raw control character on screen."""
    def test_a_datetime_prints_as_text_and_controls_are_escaped(self):
        import argparse, contextlib, datetime, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.out(argparse.Namespace(json=True),
                      {"when": datetime.datetime(2026, 10, 22, 12, 0), "name": "a\x7fb\x9bc"}, None)
        text = buf.getvalue()
        self.assertIn("2026-10-22 12:00:00", text)
        self.assertNotIn("\x7f", text)
        self.assertNotIn("\x9b", text)
        self.assertEqual(json.loads(text)["name"], "a\x7fb\x9bc")


class VerifyFreshLane(ForemanTestCase):  # T-3189: T-3113's `cd mods/x && npx vitest run` passed only in a lane with node_modules
    def test_warns_when_a_verify_cannot_run_in_a_fresh_lane(self):
        import tempfile
        with tempfile.TemporaryDirectory(dir=self.home) as d:
            git = lambda *a: subprocess.run(["git", "-C", d, *a], check=True, capture_output=True)  # noqa: E731
            git("init", "-q")
            os.makedirs(os.path.join(d, "ok"))
            os.makedirs(os.path.join(d, "mods", "x"))
            for f in ("ok/package.json", "mods/x/package.json"):
                open(os.path.join(d, f), "w").write("{}")
            git("add", "ok/package.json")  # mods/x/package.json stays untracked
            warn = lambda cmd: " ".join(c.lint_verify(cmd, d))  # noqa: E731
            self.assertIn("committed package.json", warn("cd mods/x && npx vitest run"))
            self.assertIn("committed package.json", warn("cd nowhere && npm test"))
            self.assertNotIn("package.json", warn("cd ok && npx vitest run"))
            self.assertNotIn("package.json", warn("cd mods/x && python3 -m pytest"))
        repo = os.path.join(os.path.dirname(__file__), "..", "..")
        lint = lambda cmd: " ".join(c.lint_verify(cmd, repo))  # noqa: E731
        self.assertIn("names no test", lint("python3 plugin/tests/run.py test_nope"))
        self.assertIn("nosuchtool", lint("nosuchtool --x"))
        self.assertEqual(lint("python3 plugin/tests/run.py test_cli"), "")


class Init(ForemanTestCase):
    def test_init_prints_slug_and_state_is_empty(self):
        slug = self.fm("init").stdout.strip()
        self.assertEqual(slug, c.slug_for(self.repo))
        st = self.fm_json("state")
        self.assertEqual(st["project"], slug)
        self.assertIsNone(st["active"])
        self.assertEqual((st["queue"], st["inbox"]), ([], []))

    def test_an_unknown_task_id_names_the_ones_that_exist(self):
        # T-0987 (T-0978: a model took a task id from another checkout): the refusal says which ids are here
        self.fm("init")
        self.fm("task", "new", "One", "--type", "FIX", "--tier", "S", "--ac", "x :: true", "--step", "x", "--focus")
        self.fm("capture", "Two")
        err = self.fm("task", "show", "T-0983", check=False).stderr
        self.assertIn("no task T-0983", err)
        self.assertIn("active T-0001", err)
        self.assertIn("T-0002", err)

    def test_commands_auto_register_git_repo(self):
        self.fm("capture", "something")
        self.assertIsNotNone(c.find_project(self.repo))

    def test_project_flag_and_env_select_project_outside_it(self):
        slug = self.fm("init").stdout.strip()
        elsewhere = os.path.join(self.tmp, "elsewhere")
        os.makedirs(elsewhere)
        self.assertEqual(self.fm_json("state", "-p", slug, cwd=elsewhere)["project"], slug)
        self.assertEqual(self.fm_json("state", cwd=elsewhere, env={"FOREMAN_PROJECT": slug})["project"], slug)
        p = self.fm("state", cwd=elsewhere, check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("sampo init", p.stderr)


class ShadowRows(ForemanTestCase):  # T-2488: each model use logs the path's own decision, no model called
    def test_capture_and_intake_log_type_and_tier_and_a_refused_near_duplicate_logs_dedupe(self):
        import json
        self.fm("init")
        p = c.find_project(self.repo)
        self.fm("capture", "FIX: the parser crashes on empty input")
        self.fm("intake", "FEATURE: an export button for the report page")
        self.fm("capture", "FIX: the parser crashes on empty input files", check=False)  # near-duplicate: refused
        rows = [json.loads(x) for x in open(os.path.join(p.dir, "shadow.jsonl"))]
        got = [(r["use"], r["path"]) for r in rows]
        for want in (("intake-type", "FIX"), ("intake-type", "FEATURE"), ("dedupe", "same")):
            self.assertIn(want, got)
        self.assertEqual(sum(u == "intake-tier" for u, _ in got), 2)


class FlagsAmongPaths(unittest.TestCase):  # T-1671
    def test_a_flag_among_path_words_moves_in_front_and_free_text_is_left_as_typed(self):
        p = cli.build_parser()
        first = lambda argv: cli._options_first(p, argv)  # noqa: E731
        a = p.parse_args(first(["new", "check", "--run", "x.py"]))
        self.assertEqual((a.words, a.run), (["check", "x.py"], True))
        a = p.parse_args(first(["-p", "proj", "img", "diff", "a.png", "--tol", "3", "b.png"]))
        self.assertEqual((a.paths, a.tol, a.project), (["a.png", "b.png"], 3, "proj"))
        a = p.parse_args(first(["img", "diff", "a.png", "--tol=3", "b.png"]))
        self.assertEqual((a.paths, a.tol), (["a.png", "b.png"], 3))
        for argv in (["new", "check", "--bogus", "x.py"],  # an option it doesn't know: as typed, argparse says so
                     ["code", "fix", "--json", "it"],  # a request is free text: a flag inside stays an error
                     ["session", "send", "ID", "please", "--title", "to"],
                     ["new", "check", "x.py", "--", "--run"]):  # past -- nothing moves
            self.assertEqual(first(list(argv)), argv)


class ParserOnce(unittest.TestCase):
    def test_the_parser_is_built_once_per_process(self):
        # T-1378: the hook queues a prompt's requests with in-process fm calls; each rebuilt the parser twice (its own
        # and fmrecall's similar-command search), ~0.15 s a request, so at load only 2-3 of 16 fit the hook's time
        from unittest import mock
        with mock.patch.object(cli, "_PARSER", None), \
                mock.patch.object(cli, "_build_parser", wraps=cli._build_parser) as built:
            a, b = cli.build_parser(), cli.build_parser()
            from sampo import recall
            recall.covered("list every task in the queue with its status")
        self.assertIs(a, b)
        self.assertEqual(built.call_count, 1)
        self.assertEqual(a.parse_args(["state"]).cmd, "state")
        self.assertEqual(a.parse_args(["state", "--json"]).json, True, "a second parse starts from the defaults")
        self.assertFalse(a.parse_args(["state"]).json)

    def test_a_run_builds_only_its_command_and_each_matches_the_full_parser(self):
        # T-2016: building all 214 subparsers was ~80 ms of every fm call; a command whose arguments leaned on another
        # command's block would lose them silently, so every partial parser is compared with the full one
        def subs(ap):
            return next((a for a in ap._actions if isinstance(a, argparse._SubParsersAction)), None)

        def shape(ap):
            s = subs(ap)
            return ([(a.option_strings, a.dest, a.nargs, a.choices if not isinstance(a, argparse._SubParsersAction)
                      else None, a.default, a.required) for a in ap._actions]
                    + [(k, shape(v)) for k, v in (s.choices.items() if s else ())])

        full = subs(cli.build_parser()).choices
        tiers = list(cli.HELP_TIERS)
        for name in full:
            part = subs(cli._build_parser(only=name)).choices
            self.assertEqual(set(part), {name, *(k for k, v in full.items() if v is full[name])}, name)
            self.assertEqual(shape(part[name]), shape(full[name]), name)
        self.assertEqual(cli.HELP_TIERS, tiers, "sampo help's tiers stay whole")
        from unittest import mock
        with mock.patch.object(cli, "_PARSER", None), mock.patch.dict(cli._PARTS, clear=True):
            self.assertEqual(len(subs(cli._parser_for(["-p", "x", "next"])).choices), 1)
            self.assertEqual(len(subs(cli._parser_for(["nxet"])).choices), len(full), "a typo gets the whole parser")


class CaptureAndIntake(ForemanTestCase):
    def test_a_missing_file_argument_is_a_usage_error(self):
        # T-1189: fm intake --file on a path that isn't there printed a Python traceback
        self.fm("init")
        for argv in (["intake", "--file", "nope.txt"], ["research", "add", "x", "--file", "nope.md"]):
            r = self.fm(*argv, check=False)
            self.assertNotIn("Traceback", r.stderr, argv)
            self.assertIn("nope.", r.stderr, argv)
            self.assertNotEqual(r.returncode, 0)

    def test_code_print_help_says_it_makes_a_task_and_names_the_probe_flags(self):
        # T-3089: -p looked like a harmless print but creates a task
        h = " ".join(self.fm("code", "--help").stdout.split())
        i = h.index("claude's -p: ")
        seg = h[i:i + 400]
        for want in ("creates a task", "--ask", "--dry-plan"):
            self.assertIn(want, seg)

    def test_capture_creates_captured_brief_and_inbox_view(self):
        out = self.fm("capture", "export report as CSV", "--type", "FEATURE", "--source", "discovered").stdout
        self.assertIn("T-0001", out)
        b = c.find_brief(c.find_project(self.repo), "T-0001")
        self.assertEqual((b.status, b.type, b.meta["source"]), ("captured", "FEATURE", "discovered"))
        inbox = read_text(os.path.join(c.find_project(self.repo).dir, "INBOX.md"))
        self.assertIn("T-0001", inbox)
        self.assertEqual([i["id"] for i in self.fm_json("state")["inbox"]], ["T-0001"])

    def test_capture_reask_dropped(self):
        # T-0438: a request like one already dropped is said at capture, with why it was dropped
        self.fm("capture", "export the weekly report as a CSV spreadsheet")
        self.fm("task", "drop", "T-0001", "not wanted: reports live on the dashboard")
        out = self.fm("capture", "export weekly report to CSV spreadsheet").stdout
        self.assertIn("Dropped before: T-0001", out)
        self.assertIn("reports live on the dashboard", out)
        self.assertNotIn("Dropped before", self.fm("capture", "fix the login timeout on slow wifi").stdout)

    def test_capture_reask_ignores_a_dropped_batchs_huge_request(self):
        # T-0726: a dropped batch quoting every member's request shared half the words of any new request
        import itertools
        words = " ".join("q" + "".join(x) for x in itertools.product("abcd", repeat=4))  # 256 distinct stems
        self.fm("capture", f"rebuild members: audit diffs lane locks commit retry evidence quality {words}")
        self.fm("task", "drop", "T-0001", "rebuilt")
        out = self.fm("capture", "audit diffs skip lane locks").stdout
        self.assertNotIn("Dropped before", out)

    def test_capture_veto_contradiction(self):
        # T-0438: a request a standing veto covers is said at capture
        self.fm("capture", "tidy the readme")
        c.add_veto(c.find_project(self.repo), "never push to main")
        self.assertIn("Vetoed: \"never push to main\"", self.fm("capture", "push the release to main").stdout)
        self.assertNotIn("Vetoed", self.fm("capture", "write the release notes").stdout)

    def test_a_lone_word_no_action_resembles_is_a_step_or_criterion_to_add(self):
        # T-1903: `fm task step T-1 CHANGELOG` was "invalid choice"; a word near an action stays a typo
        self.fm("task", "new", "x", "--type", "FIX", "--tier", "S")
        self.fm("task", "step", "T-0001", "CHANGELOG")
        self.fm("task", "ac", "T-0001", "add", "works")  # T-3372: a bare `ac ID works` is refused (verb-like)
        b = c.find_brief(c.find_project(self.repo), "T-0001")
        self.assertEqual([s.text for s in b.steps()], ["CHANGELOG"])
        self.assertIn("works", b.section("Acceptance criteria"))
        p = self.fm("task", "step", "T-0001", "dnoe", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("invalid choice", p.stderr)
        self.fm("task", "ac", "T-0001", "add", "fast", "--verify", "true")  # review: an option after the word
        self.assertIn("verify with `true`", c.find_brief(c.find_project(self.repo), "T-0001").section("Acceptance criteria"))
        for typo in ("DONE", "3", "--json"):  # review: an action in capitals, a number, an option alone stay errors
            self.assertNotEqual(self.fm("task", "step", "T-0001", typo, check=False).returncode, 0, typo)
        self.assertEqual(len(c.find_brief(c.find_project(self.repo), "T-0001").steps()), 1)

    def test_an_action_synonym_is_its_action_and_two_bare_words_refuse(self):  # T-2540
        # `fm task step T-1 remove 4` added a step named "remove" and dropped the 4
        self.fm("task", "new", "x", "--type", "FIX", "--tier", "S", "--step", "one", "--step", "two")
        steps = lambda: [s.text for s in c.find_brief(c.find_project(self.repo), "T-0001").steps()]  # noqa: E731
        self.fm("task", "step", "T-0001", "remove", "2")
        self.assertEqual(steps(), ["one"])
        p = self.fm("task", "step", "T-0001", "fix", "it", check=False)
        self.assertNotEqual(p.returncode, 0, "two bare words: quote the text")
        self.assertEqual(steps(), ["one"])
        self.fm("task", "step", "T-0001", "CHANGELOG")  # T-1903's lone word still adds
        self.assertEqual(steps(), ["one", "CHANGELOG"])

    def test_intake_source_files_the_briefs_under_it(self):  # T-2554: the tower's briefs aren't the user's asks
        made = json.loads(self.fm("intake", "--source", "genesis", "FEATURE: a tower brief", "--json").stdout)
        tid = (made["created"] if isinstance(made, dict) else made)[0]["id"]
        self.assertEqual(c.find_brief(c.find_project(self.repo), tid).meta.get("source"), "genesis")

    def test_capture_reads_a_leading_tag_as_the_type(self):  # T-1904: "FIX: …" was filed as a FEATURE
        self.fm("capture", "FIX: the send button stays grey")
        self.fm("capture", "bug: login loops", "--type", "CLEAN")  # --type wins; the words stay
        self.fm("capture", "Note: tags are case-blind")
        self.fm("capture", "PERF?: slow startup")  # explore, as fm intake reads it
        self.fm("capture", "fix!: crash on save")  # urgent
        p = c.find_project(self.repo)
        one, two, three, four, five = (c.find_brief(p, f"T-000{i}") for i in range(1, 6))
        self.assertEqual((one.type, one.title, one.priority), ("FIX", "The send button stays grey", "normal"))
        self.assertEqual((two.type, two.title), ("CLEAN", "Bug: login loops"))
        self.assertEqual((three.type, three.title), ("FEATURE", "Note: tags are case-blind"))
        self.assertEqual((four.type, four.title, bool(four.meta.get("explore"))), ("PERFORMANCE", "Slow startup", True))
        self.assertEqual((five.type, five.title, five.priority), ("FIX", "Crash on save", "urgent"))
        self.assertFalse(one.meta.get("explore"))
        self.fm("capture", "FIX: the save hangs\nrepro: save twice")  # review: a tag on the first of several lines
        six = c.find_brief(p, "T-0006")
        self.assertEqual(six.type, "FIX")
        self.assertNotIn("FIX:", six.title)

    def test_capture_keeps_a_mid_text_ref_and_records_refs_as_depends(self):  # T-0334
        self.fm("capture", "first")
        self.fm("capture", "fix the #T-0001 regression in send")
        self.fm("capture", "#T-0001 follow-up: docs")
        p = c.find_project(self.repo)
        two, three = c.find_brief(p, "T-0002"), c.find_brief(p, "T-0003")
        self.assertEqual(two.title, "Fix the T-0001 regression in send")
        self.assertEqual(three.title, "Follow-up: docs")
        self.assertEqual((two.meta["depends_on"], three.meta["depends_on"]), (["T-0001"], ["T-0001"]))

    def test_intake_block_creates_briefs_in_canonical_order(self):
        block = ("FEATURE: export CSV @src/reports\nFIX: login timeout\nCLEAN!: dedupe date helpers\n"
                 "PERF?: first paint 4s\nSECURITY: review upload\nCONTEXT: Django app\nDONE-WHEN: tests pass\n")
        res = json.loads(self.fm("intake", "--json", input=block).stdout)
        self.assertEqual([i["type"] for i in res["created"]], ["FEATURE", "FIX", "CLEAN", "PERFORMANCE", "SECURITY"])
        self.assertEqual([i["type"] for i in res["order"]], ["CLEAN", "PERFORMANCE", "SECURITY", "FIX", "FEATURE"])
        p = c.find_project(self.repo)
        feat = c.find_brief(p, res["created"][0]["id"])
        self.assertEqual(feat.meta["scope"], ["src/reports"])
        self.assertIn("Django app", feat.section("Raw request"))
        self.assertIn("tests pass", feat.section("Raw request"))
        clean = c.find_brief(p, res["created"][2]["id"])
        self.assertEqual(clean.priority, "urgent")
        self.assertTrue(c.find_brief(p, res["created"][3]["id"]).meta.get("explore"))

    def test_lines_between_items_belong_to_the_item_above(self):
        # T-0259: a 30-item block gave every item every item's CONTEXT and DONE-WHEN
        block = ("CONTEXT: shared repo note\nFEATURE: export CSV\nCONTEXT: reports.py\nDONE-WHEN: the CSV opens\n"
                 "FIX: login timeout\nCONTEXT: auth/session.py\nDONE-WHEN: login works on 3G\nSKIP: OAuth\n")
        res = json.loads(self.fm("intake", "--json", input=block).stdout)
        p = c.find_project(self.repo)
        feat, fix = (c.find_brief(p, x["id"]) for x in res["created"])
        for b in (feat, fix):
            self.assertIn("shared repo note", b.section("Raw request"), "lines before the first item are for all")
        self.assertIn("the CSV opens", feat.section("Raw request"))
        self.assertNotIn("login works", feat.section("Raw request"))
        self.assertNotIn("auth/session.py", feat.section("Raw request"))
        self.assertIn("login works on 3G", fix.section("Raw request"))
        self.assertNotIn("reports.py", fix.section("Raw request"))
        self.assertIn("OAuth", fix.section("Non-goals"))
        self.assertNotIn("OAuth", feat.section("Non-goals"))

    def test_intake_ref_becomes_dependency(self):
        self.fm("task", "new", "Base work", "--type", "CLEAN", "--tier", "S")
        res = json.loads(self.fm("intake", "--json", input="FEATURE: build on it #T-0001\n").stdout)
        b = c.find_brief(c.find_project(self.repo), res["created"][0]["id"])
        self.assertEqual(b.meta["depends_on"], ["T-0001"])

    def test_a_long_title_is_cut_at_a_word_and_a_command_keeps_its_case(self):
        # T-1985: "…the DONE-WHEN fallbac" and "Fm batch drops …"
        self.fm("capture", "FIX: fm batch drops a member's verify command when its criteria come from the DONE-WHEN "
                           "fallback of the raw request text")
        self.fm("capture", "FIX: c.first_touch counts a Write outside the project")
        self.fm("capture", "FIX: the band is slow")
        t = {b.id: b.title for b in c.load_briefs(c.find_project(self.repo))}
        self.assertTrue(t["T-0001"].startswith("fm batch drops"), t["T-0001"])
        self.assertTrue(t["T-0001"].endswith(" DONE-WHEN…"), t["T-0001"])
        self.assertLessEqual(len(t["T-0001"]), 90)
        self.assertTrue(t["T-0002"].startswith("c.first_touch"), t["T-0002"])
        self.assertEqual(t["T-0003"], "The band is slow")

    def test_a_mid_sentence_scope_keeps_its_text_and_an_end_one_is_dropped(self):
        # T-1991: "an intake line made only of @path tokens …" became "made only of  tokens"
        from sampo import cli
        self.assertEqual(cli._title("a line of @path tokens is dropped @plugin/lib/fmcore.py"),
                         "A line of path tokens is dropped")
        self.assertEqual(cli._title("@src/app.py the totals row is wrong"), "The totals row is wrong")
        self.assertEqual(cli._title("mail a@b.com bounces @src/mail.py"), "Mail a@b.com bounces")

    def test_titles_cannot_carry_terminal_control_sequences(self):
        # round 4 (brainstorm, security): titles are printed by the dashboard, statusline and terminal title
        self.fm("capture", "evil\x1b]0;pwned\x07 title‮ here")
        self.fm("task", "new", "also\x1b[2J bad", "--type", "FIX", "--tier", "S")
        self.fm("task", "new", "planned\x1b]0;again\x07 one", "--type", "FIX", "--tier", "S", "--from", "T-0001")
        self.fm("capture", "\x1b[2J")
        for b in c.load_briefs(c.find_project(self.repo)):
            self.assertNotRegex(b.title, r"[\x00-\x1f\x7f‮]")
            self.assertTrue(b.title, "a title of only control characters falls back to 'untitled'")
        self.assertNotIn("\x1b]0;pwned", self.fm("watch", "--once").stdout)

    def test_planning_a_capture_replaces_its_criteria_and_steps(self):
        # T-1364: a capture that already has criteria and steps, planned with --from, keeps only the ones given
        # (T-1315 and T-1331 each ended up with every step twice)
        for tid in ("T-0001", "T-0002"):
            self.fm("capture", f"make it so {tid}")
            self.fm("task", "ac", tid, "add", "old check", "--verify", "true")
            self.fm("task", "step", tid, "add", "old step")
        self.fm("task", "new", "make it so", "--type", "FIX", "--tier", "S", "--from", "T-0001",
                "--ac", "new check :: true", "--step", "new step", "--replan")  # T-3087: ac add made it planned
        got = json.loads(self.fm("task", "show", "T-0001", "--json").stdout)
        self.assertEqual([x["text"] for x in got["criteria"]], ["new check"])
        self.assertEqual([x["text"] for x in got["steps"]], ["new step"])
        self.fm("task", "new", "keep mine", "--type", "FIX", "--tier", "S", "--from", "T-0002", "--replan")
        got = json.loads(self.fm("task", "show", "T-0002", "--json").stdout)
        self.assertEqual([x["text"] for x in got["criteria"]], ["old check"], "no --ac: the capture's own stay")
        self.assertEqual([x["text"] for x in got["steps"]], ["old step"])

    def test_a_capture_given_criteria_and_steps_is_planned(self):
        # T-1886: filled by fm task set, ac or step it stayed captured, so the drive kept offering it for planning
        def status(tid):
            return json.loads(self.fm("task", "show", tid, "--json").stdout)["status"]
        self.fm("capture", "one")
        self.fm("capture", "two")
        self.fm("task", "set", "T-0001", "--section", "Acceptance criteria",
                "--text", "- [ ] works — verify with `true`")
        self.assertEqual(status("T-0001"), "captured")  # criteria alone: not yet
        self.fm("task", "step", "T-0001", "add", "do it")
        self.assertEqual(status("T-0001"), "planned")
        self.fm("task", "set", "T-0002", "--section", "Steps", "--text", "1. [ ] go")
        self.fm("task", "ac", "T-0002", "add", "works :: true")
        self.assertEqual(status("T-0002"), "planned")
        self.fm("task", "set", "T-0002", "status=captured")
        self.assertEqual(status("T-0002"), "captured")  # set by hand: kept

    def test_task_set_scope_plus_and_minus_edit_the_list(self):  # T-3377
        def scope():
            return json.loads(self.fm("task", "show", "T-0001", "--json").stdout)["scope"]
        self.fm("capture", "one")
        self.fm("task", "set", "T-0001", "scope=[a, b]")
        self.fm("task", "set", "T-0001", "scope+=c,a")
        self.assertEqual(scope(), ["a", "b", "c"])
        self.fm("task", "set", "T-0001", "scope-=b")
        self.assertEqual(scope(), ["a", "c"])
        self.fm("task", "set", "T-0001", "scope-=zzz")
        self.assertEqual(scope(), ["a", "c"])
        p = self.fm("task", "set", "T-0001", "title+=x", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("can't set 'title+'", p.stderr)

    def test_task_set_list_ops_no_scope_depends_glob_chain_and_race(self):  # T-3377
        def get(k):
            return json.loads(self.fm("task", "show", "T-0001", "--json").stdout)[k]
        self.fm("capture", "one")
        self.fm("capture", "two")
        self.fm("task", "set", "T-0001", "scope+=src/**/*.py")  # no scope yet; a glob stays whole
        self.assertEqual(get("scope"), ["src/**/*.py"])
        self.fm("task", "set", "T-0001", "depends_on+=T-0002")
        self.assertEqual(get("depends"), ["T-0002"])
        self.fm("task", "set", "T-0001", "depends_on-=T-0002")
        self.assertEqual(get("depends"), [])
        self.fm("task", "set", "T-0001", "scope=[a]", "scope+=b", "scope-=a")  # in order, in one call
        self.assertEqual(get("scope"), ["b"])
        self.fm("task", "set", "T-0001", "scope=[]")
        ts = [threading.Thread(target=self.fm, args=("task", "set", "T-0001", f"scope+=f{i}")) for i in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(sorted(get("scope")), ["f0", "f1", "f2", "f3"])

    def test_ac_edit_text_carrying_a_verify_phrase_sets_the_verify(self):
        # T-2786: --text 'X — verify with `cmd`' stored the phrase as text and kept the old verify: two verifies
        self.fm("capture", "one")
        self.fm("task", "ac", "T-0001", "add", "old", "--verify", "true")
        for text, want in (("X — verify with `python3 -c pass", "- [ ] X — verify with `python3 -c pass`"),
                           ("Y — verify with `python3 -c pass`", "- [ ] Y — verify with `python3 -c pass`"),
                           ("Z :: python3 -c pass", "- [ ] Z — verify with `python3 -c pass`")):
            self.fm("task", "ac", "T-0001", "edit", "1", "--text", text)
            shown = self.fm("task", "show", "T-0001").stdout
            self.assertIn(want, shown)
            self.assertEqual(shown.count("verify with"), 1)
        self.fm("task", "ac", "T-0001", "edit", "1", "--text", "W — verify with `false`", "--verify", "true")
        shown = self.fm("task", "show", "T-0001").stdout  # --verify wins, the phrase leaves the text
        self.assertIn("- [ ] W — verify with `true`", shown)
        self.assertNotIn("false", shown)

    def test_section_criteria_and_steps_in_the_task_new_forms_are_parsed(self):
        # T-1894: "- [ ] X :: cmd" and "1. go" given with --section stayed plain text: no verify command, no steps,
        # so fm focus refused the task as not planned
        self.fm("capture", "one")
        self.fm("task", "set", "T-0001", "--section", "Acceptance criteria", "--text", "- [ ] works :: test -e a",
                "--section", "Steps", "--text", "1. go\n2. [x] went")
        shown = self.fm("task", "show", "T-0001").stdout
        self.assertIn("- [ ] works — verify with `test -e a`", shown)
        self.assertIn("1. [ ] go\n2. [x] went", shown)
        self.assertEqual(json.loads(self.fm("task", "show", "T-0001", "--json").stdout)["status"], "planned")
        kept = "- [ ] kept — verify with `echo a :: b`"  # saved again as shown: a command holding " :: " stays
        self.fm("task", "set", "T-0001", "--section", "Acceptance criteria", "--text", kept)
        self.assertIn(kept + "\n", self.fm("task", "show", "T-0001").stdout)

    def test_section_criteria_and_steps_in_the_ac_and_step_forms_are_parsed(self):
        # T-1900: "works :: cmd" and bare step lines, the forms --ac and --step take, stayed plain text
        self.fm("capture", "one")
        self.fm("task", "set", "T-0001", "--section", "Acceptance criteria", "--text", "works :: test -e a",
                "--section", "Steps", "--text", "go\nwent")
        shown = self.fm("task", "show", "T-0001").stdout
        self.assertIn("- [ ] works — verify with `test -e a`", shown)
        self.assertIn("1. [ ] go\n2. [ ] went", shown)
        self.assertEqual(json.loads(self.fm("task", "show", "T-0001", "--json").stdout)["status"], "planned")

    def test_a_planned_task_never_started_can_be_planned_again(self):
        # T-1862: an M task whose --focus was refused stays planned; --from refused it, so only status=captured got out
        self.fm("task", "new", "big one", "--type", "FIX", "--tier", "S", "--ac", "old :: true", "--step", "old")
        r = self.fm("task", "new", "big one", "--type", "FIX", "--tier", "S", "--from", "T-0001", "--ac", "new :: true",
                    check=False)  # T-3087: a planned task with criteria is not re-planned by accident
        self.assertEqual((r.returncode, "--replan" in r.stderr), (1, True), r.stderr)
        got = json.loads(self.fm("task", "show", "T-0001", "--json").stdout)
        self.assertEqual([x["text"] for x in got["criteria"]], ["old"])
        self.fm("task", "new", "big one", "--type", "FIX", "--tier", "S", "--from", "T-0001", "--ac", "new :: true",
                "--step", "new", "--focus", "--replan")
        got = json.loads(self.fm("task", "show", "T-0001", "--json").stdout)
        self.assertEqual(([x["text"] for x in got["criteria"]], got["status"]), (["new"], "active"))
        r = self.fm("task", "new", "big one", "--type", "FIX", "--tier", "S", "--from", "T-0001", check=False)
        self.assertEqual((r.returncode, "T-0001 is active" in r.stderr), (1, True), r.stderr)

    def test_parallel_captures_get_unique_ids(self):
        outs = []

        def run(i):
            for _ in range(20):  # the 5 s lock can run out under load (exit 3): try again
                r = self.fm("capture", f"item {i}", check=False)
                if r.returncode != 3:
                    break
            outs.append(r.stdout if r.returncode == 0 else f"exit {r.returncode}: {r.stderr}")

        self.fm("init")
        threads = [threading.Thread(target=run, args=(i,)) for i in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(len(outs), 8, outs)
        ids = sorted(b.id for b in c.load_briefs(c.find_project(self.repo)))
        self.assertEqual(ids, [f"T-{i:04d}" for i in range(1, 9)])


class TaskLifecycle(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("task", "new", "Fix login timeout", "--type", "FIX", "--tier", "S", "--scope", "src/auth/**")
        self.fm("task", "set", "T-0001", "--section", "Regression test", "--text", "none: fixture (T-0045 has its own)")
        self.p = c.find_project(self.repo)

    def brief(self, tid="T-0001"):
        return c.find_brief(self.p, tid)

    def test_new_task_is_planned_with_scope(self):
        b = self.brief()
        self.assertEqual((b.status, b.type, b.tier, b.meta["scope"]), ("planned", "FIX", "S", ["src/auth/**"]))
        self.assertEqual(self.fm_json("task", "show", "T-0001")["id"], "T-0001")

    def test_step_done_without_evidence_is_refused(self):
        self.fm("task", "step", "T-0001", "add", "write failing test")
        p = self.fm("task", "step", "T-0001", "done", "1", check=False)
        self.assertEqual(p.returncode, 2)
        self.assertIn("evidence", p.stderr)
        self.fm("task", "step", "T-0001", "done", "1", "--evidence", "pytest -k slow", "1 failed as expected")
        self.assertTrue(self.brief().steps()[0].done)

    def test_evidence_then_step_done(self):
        self.fm("task", "step", "T-0001", "add", "a")
        self.fm("task", "step", "T-0001", "add", "b")
        self.fm("task", "evidence", "T-0001", "--step", "1", "pytest", "3 passed")
        self.fm("task", "step", "T-0001", "done", "1")
        steps = self.brief().steps()
        self.assertEqual([(s.done, s.current) for s in steps], [(True, False), (False, True)])

    def test_step_evidence_moves_the_current_step(self):
        # T-0406 (the user, watching JARVIS in Claude Code): steps 3-5 of T-0278 had passing runs while the band still
        # read "step 1/5, 0 done": evidence never ticked a step or moved its CURRENT marker, only fm task finish did
        for s in ("a", "b", "c"):
            self.fm("task", "step", "T-0001", "add", s)
        state = lambda: [(s.done, s.current) for s in self.brief().steps()]  # noqa: E731
        self.fm("task", "evidence", "T-0001", "--step", "2", "--run", "true")
        self.assertEqual(state(), [(False, False), (True, False), (False, True)], "2 ticked, the work moves to 3")
        self.fm("task", "evidence", "T-0001", "--step", "3", "--run", "false", check=False)
        self.assertEqual(state(), [(False, False), (True, False), (False, True)], "a failed run keeps 3 open, current")
        self.fm("task", "evidence", "T-0001", "--step", "2", "--run", "false", check=False)
        self.assertEqual(state(), [(False, False), (False, True), (False, False)], "a failure reopens 2, current")
        self.fm("task", "evidence", "T-0001", "--step", "2", "--run", "true")
        self.fm("task", "evidence", "T-0001", "--step", "3", "--run", "true")
        self.assertEqual(state(), [(False, True), (True, False), (True, False)], "the open step left is current")
        self.assertEqual(self.fm_json("task", "show", "T-0001")["step"]["n"], 1)

    def test_task_done_requires_everything(self):
        self.fm("task", "step", "T-0001", "add", "a")
        self.fm("task", "ac", "T-0001", "add", "login works on 3G", "--verify", "pytest -k slow")
        p = self.fm("task", "done", "T-0001", check=False)
        self.assertEqual(p.returncode, 2)
        self.assertIn("step 1", p.stderr)
        self.assertIn("criterion 1", p.stderr)
        self.fm("task", "step", "T-0001", "done", "1", "--evidence", "pytest", "ok")
        self.fm("task", "ac", "T-0001", "check", "1", "--evidence", "pytest -k slow", "1 passed")
        p = self.fm("task", "done", "T-0001", check=False)
        self.assertEqual(p.returncode, 2)
        self.assertIn("audit missing: self", p.stderr)
        self.fm("task", "audit", "T-0001", "self", "lens checklist", "no findings")
        self.fm("task", "done", "T-0001")
        self.assertEqual(self.brief().status, "done")
        events = [e["event"] for e in c.ledger_tail(self.p)]
        self.assertIn("task_done", events)

    def test_remote_is_granted_only_through_fm_ask(self):
        p = self.fm("task", "set", "T-0001", "--allow", "remote", check=False)
        self.assertEqual(p.returncode, 2)
        self.assertIn("sampo ask", p.stderr)
        self.fm_ask("T-0001", "remote")
        self.assertEqual(c.read_meta(self.p)["pending_approvals"][0]["allow"], ["remote"])

    def test_docs_named_in_docs_impact_must_exist_and_be_current(self):
        self.fm("task", "new", "Add a flag", "--type", "FEATURE", "--tier", "M")
        self.fm("task", "step", "T-0002", "add", "a")
        self.fm("task", "step", "T-0002", "done", "1", "--evidence", "pytest", "ok")
        for name, text in (("README.md", "# app\nRun `./cli.py` with --verbose.\n"), ("NOTES.md", "See `./gone.py`.\n")):
            with open(os.path.join(self.repo, name), "w") as f:
                f.write(text)
        self.fm("task", "set", "T-0002", "--section", "Docs impact", "--text", "README.md: --verbose; docs/usage.md")

        def audited_done(check):
            for lens in ("intent", "edge"):
                self.fm("task", "audit", "T-0002", lens, "lens prompt", "ok")
            return self.fm("task", "done", "T-0002", "--lesson", "none: test", check=check)
        p = audited_done(False)
        self.assertEqual(p.returncode, 2)
        self.assertIn("docs/usage.md, which doesn't exist", p.stderr)
        self.assertIn("README.md: path ./cli.py", p.stderr)
        self.assertNotIn("gone.py", p.stderr, "drift in docs the task didn't name is reported, not a blocker")
        open(os.path.join(self.repo, "cli.py"), "w").close()
        self.fm("task", "set", "T-0002", "--section", "Docs impact", "--text", "README.md: documents --verbose")
        self.assertIn("NOTES.md: path ./gone.py", audited_done(True).stdout)

    def test_audit_older_than_the_last_edit_is_stale(self):
        self.fm("task", "step", "T-0001", "add", "a")
        self.fm("task", "step", "T-0001", "done", "1", "--evidence", "pytest", "ok")
        self.fm("task", "audit", "T-0001", "self", "checklist", "ok")
        with open(os.path.join(self.repo, "x.py"), "w") as f:  # an edit after the audit…
            f.write("x = 2\n")
        with open(os.path.join(self.p.dir, "ledger.jsonl"), "a") as f:  # …as the hook records it
            f.write(json.dumps({"ts": "2999-01-01T00:00:00Z", "task": "T-0001", "event": "touched",
                                "data": {"file": "x.py", "tool": "Edit"}}) + "\n")
        p = self.fm("task", "done", "T-0001", check=False)
        self.assertEqual(p.returncode, 2)
        self.assertIn("re-audit", p.stderr)

    def test_evidence_recorded_after_the_audit_does_not_stale_it(self):
        # T-0020: the audit covered these exact files; recording more evidence (merge/push results) changes nothing
        self.fm("task", "step", "T-0001", "add", "a")
        self.fm("task", "step", "T-0001", "done", "1", "--evidence", "pytest", "ok")
        self.fm("task", "audit", "T-0001", "self", "checklist", "ok")
        time.sleep(1.1)  # timestamps have one-second resolution: the evidence must come later than the audit
        self.fm("task", "evidence", "T-0001", "--step", "1", "git push origin main", "pushed")
        self.fm("task", "done", "T-0001")
        self.assertEqual(self.brief().status, "done")
        self.assertEqual(self.fm("task", "audit", "T-0001", "vibes", "x", "y", check=False).returncode, 1)

    def test_next_agrees_with_the_done_gate_after_evidence_only_updates(self):
        # T-0028: fm next / the statusline can't afford a worktree id; evidence carries the one it was recorded at
        self.fm("task", "step", "T-0001", "add", "a")
        self.fm("task", "ac", "T-0001", "add", "works")
        self.fm("focus", "T-0001")
        self.fm("task", "step", "T-0001", "done", "1", "--evidence", "pytest", "ok")
        self.fm("task", "ac", "T-0001", "check", "1", "--evidence", "pytest", "ok")
        self.fm("task", "audit", "T-0001", "self", "checklist", "ok")
        time.sleep(1.1)
        self.fm("task", "evidence", "T-0001", "--step", "1", "git push origin main", "pushed")
        self.assertEqual(self.fm_json("next")["stage"], "closing")
        with open(os.path.join(self.repo, "x.py"), "w") as f:
            f.write("x = 2\n")
        time.sleep(1.1)
        self.fm("task", "evidence", "T-0001", "--step", "1", "pytest", "ok")  # files changed since the audit
        self.assertEqual(self.fm_json("next")["stage"], "auditing")

    def test_evidence_run_records_the_real_exit_and_output(self):
        # T-0024: typed results went wrong ("272 tests" for 267); run the command and record what it printed
        self.fm("task", "step", "T-0001", "add", "a")
        out = self.fm("task", "evidence", "T-0001", "--step", "1", "--run", "echo '3 passed'").stdout
        self.assertIn("3 passed", out)
        line = self.brief().evidence()[-1]
        for needle in ("echo '3 passed'", "exit 0", "3 passed"):
            self.assertIn(needle, line)
        p = self.fm("task", "evidence", "T-0001", "--step", "1", "--run", "echo '1 failed'; exit 3", check=False)
        self.assertEqual(p.returncode, 3)
        self.assertIn("exit 3", self.brief().evidence()[-1])
        p = self.fm("task", "step", "T-0001", "done", "1", check=False)
        self.assertNotEqual(p.returncode, 0, "the newest evidence for the step is a failed run")
        self.assertIn("failed", p.stderr)
        self.fm("task", "evidence", "T-0001", "--step", "1", "--run", "true")
        self.fm("task", "step", "T-0001", "done", "1")
        self.assertEqual(self.fm("task", "evidence", "T-0001", "--step", "1", "pytest", check=False).returncode, 1,
                         "a typed result is still required without --run")

    def test_targeted_runs_never_wait_for_load_and_a_full_wait_is_capped(self):
        # T-3144: evidence and check --affected ran at once; a load wait is at most 60 s whatever FOREMAN_LOAD_WAIT says
        env = {"FOREMAN_HOST": "load=24,cpus=8,mem_mb=8192", "FOREMAN_LOAD_WAIT": "0"}
        self.fm("task", "step", "T-0001", "add", "a")
        r = self.fm("task", "evidence", "T-0001", "--step", "1", "--run", "true", env=env)
        self.assertNotIn("waiting for the machine's load", r.stderr)
        r = self.fm("check", "--affected", check=False, env=env)
        self.assertNotIn("waiting for the machine's load", r.stderr)
        slept = []
        old = (c.time.sleep, c.time.time)
        t = [0.0]
        c.time.sleep = lambda s: (slept.append(s), t.__setitem__(0, t[0] + s))
        c.time.time = lambda: t[0]
        os.environ["FOREMAN_HOST"] = "load=24,cpus=8,mem_mb=8192"
        os.environ["FOREMAN_LOAD_WAIT"] = "900"
        try:
            c.wait_for_load("fm check", out=open(os.devnull, "w"))
        finally:
            c.time.sleep, c.time.time = old
            os.environ.pop("FOREMAN_HOST", None)
            os.environ.pop("FOREMAN_LOAD_WAIT", None)
        self.assertLessEqual(sum(slept), 60)

    def test_evidence_run_never_reads_bytecode_older_than_its_source(self):
        # T-0876: a fix that kept the file's size and second of mtime ran the old code from __pycache__
        import py_compile
        self.fm("task", "step", "T-0001", "add", "a")
        mod = os.path.join(self.repo, "mod.py")
        with open(mod, "w") as f:
            f.write("X = 1\n")
        t = int(time.time()) - 60
        os.utime(mod, ns=(t * 10**9 + 100, t * 10**9 + 100))
        pyc = py_compile.compile(mod, doraise=True)
        os.utime(pyc, ns=(t * 10**9 + 300, t * 10**9 + 300))  # compiled between the two edits of that second
        with open(mod, "w") as f:
            f.write("X = 2\n")
        os.utime(mod, ns=(t * 10**9 + 800, t * 10**9 + 800))
        self.fm("task", "evidence", "T-0001", "--step", "1", "--run", "python3 -c 'import mod; assert mod.X == 2'")

    def test_focus_requires_a_plan(self):
        p = self.fm("focus", "T-0001", check=False)
        self.assertEqual(p.returncode, 2)
        self.assertIn("acceptance criterion", p.stderr)
        self.assertIn("step", p.stderr)
        self.fm("task", "ac", "T-0001", "add", "login works on 3G")
        self.fm("task", "step", "T-0001", "add", "reproduce")
        self.fm("focus", "T-0001")
        self.assertEqual(self.brief().status, "active")

    def test_a_paused_started_task_is_not_regated(self):  # T-2269 review: pausing sets status back to planned
        ap = "Approach (options → choice → why)"
        self.fm("task", "new", "Old plan", "--type", "FEATURE", "--tier", "M")
        for sec, text in (("Interpretation", "Reading 1: a. Reading 2: b."), (ap, "1) a; fails if: x 2) use what exists: b; fails if: y 3) do less: c; fails if: z Choice 1 (80%): a")):
            self.fm("task", "set", "T-0002", "--section", sec, "--text", text)
        self.fm("task", "ac", "T-0002", "add", "works", "--verify", "true")
        self.fm("task", "step", "T-0002", "add", "build")
        self.fm("focus", "T-0002")
        self.fm("task", "set", "T-0002", "--section", ap, "--text", "just do it")  # the plan now fails the content gate
        for tid in ("T-0001",):
            self.fm("task", "ac", tid, "add", "works")
            self.fm("task", "step", tid, "add", "do it")
        self.fm("focus", "T-0001")
        self.assertEqual(self.brief("T-0002").status, "planned")
        self.fm("focus", "T-0002")  # started before, paused: not refused
        self.assertEqual(self.brief("T-0002").status, "active")

    def test_unapproved_L_focus_depends_on_autonomy(self):
        self.fm("task", "new", "Big one", "--type", "FEATURE", "--tier", "L")
        for sec, text in (("Interpretation", "Reading 1: x. Reading 2: z"), ("Risks and rollback", "revert"),
                          ("Approach (options → choice → why)", "1) y; fails if: p 2) use what exists: w; fails if: q 3) do less: v; fails if: r Choice 1 (80%): y")):
            self.fm("task", "set", "T-0002", "--section", sec, "--text", text)
        self.fm("task", "ac", "T-0002", "add", "works", "--verify", "pytest")
        self.fm("task", "step", "T-0002", "add", "build")
        p = self.fm("focus", "T-0002", check=False)
        self.assertEqual(p.returncode, 2)
        self.assertIn("approval", p.stderr)
        self.fm("autonomy", "full")
        self.fm("focus", "T-0002")

    def test_focus_is_exclusive(self):
        self.fm("task", "new", "Second", "--type", "CLEAN", "--tier", "S")
        for tid in ("T-0001", "T-0002"):
            self.fm("task", "ac", tid, "add", "works")
            self.fm("task", "step", tid, "add", "do it")
        self.fm("focus", "T-0001")
        self.fm("focus", "T-0002")
        self.assertEqual((self.brief("T-0001").status, self.brief("T-0002").status), ("planned", "active"))
        self.assertEqual(self.fm_json("state")["active"]["id"], "T-0002")

    def test_set_fields_and_allow(self):
        self.fm("task", "set", "T-0001", "tier=M", "priority=urgent", "--allow", "publish")
        b = self.brief()
        self.assertEqual((b.tier, b.priority, b.meta["allow"]), ("M", "urgent", ["publish"]))
        self.fm("task", "set", "T-0001", "--section", "Execution prompt", "--text", "Do X then Y.")
        self.assertIn("Do X then Y.", self.brief().section("Execution prompt"))

    def test_cli_never_grants_core_or_unauthorizable_categories(self):
        # However fm is reached (renamed binary, symlink, python -m), core only comes from the user's reply to fm ask.
        for cat in ("core", "state-direct", "self-authorize", "bogus"):
            p = self.fm("task", "set", "T-0001", "--allow", cat, check=False)
            self.assertNotEqual(p.returncode, 0, cat)
            self.assertIn("sampo ask", p.stderr + p.stdout if cat == "core" else "sampo ask")
        self.assertEqual(self.brief().meta.get("allow") or [], [])

    def test_abbreviated_options_are_rejected(self):
        # The guard matches `--allow core` literally; an abbreviation must not reach the same code path (T-0010).
        for spelling in (["--allo", "core"], ["--al=core"], ["--all", "core"]):
            p = self.fm("task", "set", "T-0001", *spelling, check=False)
            self.assertEqual(p.returncode, 2, spelling)
        self.assertEqual(self.brief().meta.get("allow") or [], [])
        self.assertEqual(self.fm("capture", "x", "--sour", "self", check=False).returncode, 2)

    def test_ask_records_a_pending_approval_without_granting(self):
        p = self.fm_ask("T-0001", "core", "publish", why="edit the guard", session="s1")
        self.assertIn("yes", p.stdout.lower())
        pend = c.read_meta(self.p)["pending_approvals"]
        self.assertEqual([(a["task"], a["allow"], a["why"], a["session"]) for a in pend],
                         [("T-0001", ["core", "publish"], "edit the guard", "s1")])
        self.assertEqual(self.brief().meta.get("allow") or [], [])
        self.fm_ask("T-0001", "core", why="again", session="s1")
        self.assertEqual(len(c.read_meta(self.p)["pending_approvals"]), 1, "one pending request per task")

    def test_ask_not_seen_by_the_hook_is_refused(self):
        # Obfuscated or scripted fm ask: no trusted session, so nothing may be recorded (T-0009 adversary audit).
        p = self.fm("ask", "T-0001", "core", env={"CLAUDE_CODE_SESSION_ID": "victim"}, check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("session", p.stderr)
        self.assertNotIn("pending_approvals", c.read_meta(self.p))

    def test_ask_session_comes_from_the_hook_not_the_environment(self):
        self.fm_ask("T-0001", "core", session="mine", env={"CLAUDE_CODE_SESSION_ID": "victim"})
        self.assertEqual(c.read_meta(self.p)["pending_approvals"][0]["session"], "mine")

    def test_audit_goes_stale_when_files_change_however_they_were_edited(self):
        self.fm("task", "step", "T-0001", "add", "a")
        self.fm("task", "step", "T-0001", "done", "1", "--evidence", "pytest", "ok")
        self.fm("task", "audit", "T-0001", "self", "checklist", "ok")
        path = os.path.join(self.repo, "README.md")
        original = read_text(path)
        with open(path, "a") as f:  # e.g. sed -i through Bash: no touched event
            f.write("changed after the audit\n")
        p = self.fm("task", "done", "T-0001", check=False)
        self.assertEqual(p.returncode, 2)
        self.assertIn("changed since", p.stderr)
        with open(path, "w") as f:
            f.write(original)
        self.fm("task", "done", "T-0001")

    def test_tier_cannot_be_lowered_once_work_has_evidence(self):
        self.fm("task", "set", "T-0001", "tier=M")
        self.fm("task", "step", "T-0001", "add", "a")
        self.fm("task", "evidence", "T-0001", "--step", "1", "pytest", "red")
        p = self.fm("task", "set", "T-0001", "tier=S", check=False)
        self.assertEqual(p.returncode, 2)
        self.assertIn("tier", p.stderr)
        self.fm("task", "set", "T-0001", "tier=L")
        self.assertEqual(self.brief().tier, "L")

    def test_ask_rejects_unknown_and_unauthorizable_categories(self):
        for cat in ("bogus", "state-direct", "self-authorize"):
            self.assertEqual(self.fm_ask("T-0001", cat, check=False).returncode, 1, cat)
        self.assertEqual(self.fm_ask("T-0099", "core", check=False).returncode, 1)

    def test_set_rejects_unknown_status_and_protected_fields(self):
        self.assertEqual(self.fm("task", "set", "T-0001", "status=weird", check=False).returncode, 1)
        self.assertEqual(self.fm("task", "set", "T-0001", "id=T-9999", check=False).returncode, 1)
        self.assertEqual(self.fm("task", "set", "T-0001", "status=done", check=False).returncode, 1)

    def test_block_drop_defer(self):
        self.fm("task", "block", "T-0001", "waiting on API key")
        b = self.brief()
        self.assertEqual(b.status, "blocked")
        self.assertIn("waiting on API key", b.section("Log"))
        self.assertEqual(self.fm_json("state")["blocked"][0]["id"], "T-0001")
        self.fm("task", "defer", "T-0001")
        self.assertEqual(self.brief().status, "deferred")
        self.fm("task", "drop", "T-0001", "not needed")
        self.assertEqual(self.brief().status, "dropped")

    def test_drop_refuses_the_task_in_hand_unless_forced(self):
        # T-3088: a cleanup loop must not drop an active task silently
        self.fm("task", "ac", "T-0001", "add", "works", "--verify", "true")
        self.fm("task", "step", "T-0001", "add", "go")
        self.fm("focus", "T-0001")
        self.assertEqual(self.brief().status, "active")
        r = self.fm("task", "drop", "T-0001", "tidy", check=False)
        self.assertEqual((r.returncode, "--force" in r.stderr, self.brief().status), (1, True, "active"), r.stderr)
        self.fm("task", "drop", "T-0001", "tidy", "--force")
        self.assertEqual(self.brief().status, "dropped")

    def test_log_appends_a_note_without_replacing_the_section(self):
        self.fm("task", "log", "T-0001", "steer: use the existing logger instead of print")
        self.fm("task", "log", "T-0001", "second note")
        log = self.brief().section("Log")
        self.assertIn("steer: use the existing logger instead of print", log)
        self.assertIn("second note", log)
        self.assertIn("created", log, "earlier lines are kept")
        self.assertIn("note", [e["event"] for e in c.ledger_tail(self.p)])

    def test_unknown_task_exit_1(self):
        self.assertEqual(self.fm("task", "show", "T-0404", check=False).returncode, 1)

    def test_checkpoint_and_resume(self):
        self.fm("task", "step", "T-0001", "add", "reproduce")
        self.fm("task", "ac", "T-0001", "add", "works")
        self.fm("focus", "T-0001")
        with open(os.path.join(self.repo, "x.py"), "w") as f:
            f.write("x = 1\n")
        self.fm("checkpoint", "--note", "Half-way through the retry helper")
        sec = self.brief().section("Resume here")
        self.assertIn("Half-way through the retry helper", sec)
        self.assertIn("<!-- auto -->", sec)
        self.assertIn("x.py", sec)
        r = self.fm_json("resume")
        self.assertEqual((r["id"], r["step"]["n"]), ("T-0001", 1))
        self.assertIn("Half-way", r["resume"])
        self.assertIn("checkpoint", [e["event"] for e in c.ledger_tail(self.p)])

    def test_resume_with_nothing_active(self):
        self.assertIsNone(self.fm_json("resume")["id"])


class StateViews(ForemanTestCase):
    def test_state_md_is_bounded(self):
        self.fm("init")
        for i in range(30):
            self.fm("task", "new", f"task {i}", "--type", "FIX", "--tier", "S")
            self.fm("capture", f"idea {i}")
        text = read_text(os.path.join(c.find_project(self.repo).dir, "STATE.md"))
        self.assertLessEqual(len(text.splitlines()), 60)

    def test_state_line(self):
        self.fm("task", "new", "Fix it", "--type", "FIX", "--tier", "S")
        self.fm("task", "step", "T-0001", "add", "one")
        self.fm("task", "step", "T-0001", "add", "two")
        self.fm("task", "ac", "T-0001", "add", "works")
        self.fm("focus", "T-0001")
        line = self.fm("state", "--line").stdout.strip()
        self.assertIn("T-0001 FIX 1/2", line)
        self.assertEqual(len(line.splitlines()), 1)

    def test_queue_json_with_cycle(self):
        self.fm("task", "new", "a", "--type", "FIX", "--tier", "S", "--depends", "T-0002")
        self.fm("task", "new", "b", "--type", "FIX", "--tier", "S", "--depends", "T-0001")
        self.fm("task", "new", "c", "--type", "CLEAN", "--tier", "S")
        q = self.fm_json("queue")
        self.assertEqual(q["order"][0]["id"], "T-0003")
        self.assertEqual(q["cycles"], [["T-0001", "T-0002"]])

    def test_log_event(self):
        self.fm("init")
        self.fm("log", "baseline", '{"tests": "40 passed"}')
        e = c.ledger_tail(c.find_project(self.repo))[-1]
        self.assertEqual((e["event"], e["data"]), ("log", {"event": "baseline", "tests": "40 passed"}))  # T-2244


class LogNamespace(ForemanTestCase):  # T-2244: fm log can't write a Foreman event's name
    def test_forged_check_run_is_a_note(self):
        self.fm("init")
        p = c.find_project(self.repo)
        forged = json.dumps({"cmd": "x", "rc": 0, "tree": c.worktree_id(p.root), "results": [], "event": "other"})
        self.fm("log", "check_run", forged)
        e = c.ledger_tail(p)[-1]
        self.assertEqual((e["event"], e["data"]["event"]), ("log", "check_run"))
        self.assertFalse(any(x["event"] == "check_run" for x in c.ledger_tail(p, 3000)))
        note = cli._preflight(p, argparse.Namespace(meta={}))
        self.assertIn("no sampo check on this tree yet", note)


class SensitiveAndDrive(ForemanTestCase):
    def test_sensitive_on_off_preserves_other_settings(self):
        os.makedirs(os.path.join(self.repo, ".claude"))
        path = os.path.join(self.repo, ".claude", "settings.local.json")
        with open(path, "w") as f:
            json.dump({"env": {"A": "1"}}, f)
        self.fm("sensitive", "on")
        data = read_json(path)
        self.assertEqual(data["permissions"]["defaultMode"], "default")
        self.assertEqual(data["env"], {"A": "1"})
        self.assertTrue(c.read_meta(c.find_project(self.repo))["sensitive"])
        self.fm("sensitive", "off")
        data = read_json(path)
        self.assertNotIn("defaultMode", data.get("permissions", {}))
        self.assertEqual(data["env"], {"A": "1"})
        self.assertFalse(c.read_meta(c.find_project(self.repo))["sensitive"])

    def test_drive_toggle(self):
        self.fm("init")
        self.fm("drive", "off")
        self.assertFalse(c.read_meta(c.find_project(self.repo))["drive"])
        self.fm("drive", "on")
        self.assertTrue(c.read_meta(c.find_project(self.repo))["drive"])


class OneCommandTask(ForemanTestCase):
    def test_task_new_with_ac_step_and_focus(self):
        self.fm("init")
        self.fm("task", "new", "Fix typo in README", "--type", "FIX", "--tier", "S",
                "--ac", "typo gone", "--step", "fix it", "--focus")
        b = c.find_brief(c.find_project(self.repo), "T-0001")
        self.assertEqual((b.status, len(b.acceptance()), len(b.steps())), ("active", 1, 1))


class DecideRows(ForemanTestCase):
    def test_list_json_has_structured_rows(self):  # T-0341: the desktop app's Decisions tab
        self.fm("decide", "use sqlite | for the cache", "--why", "one file, no server")
        self.fm("decide", "ship without the review", "--why", "cap hit", "--kind", "costly")
        rows = self.fm_json("decide", "--list")["rows"]
        self.assertEqual([(r["kind"], r["text"], r["why"]) for r in rows],
                         [(None, "use sqlite | for the cache", "one file, no server"),
                          ("costly", "ship without the review", "cap hit")])
        self.assertRegex(rows[0]["date"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(len(self.fm_json("decide", "--review")["rows"]), 1)


class EditCriterion(ForemanTestCase):
    def test_edit_sets_the_verify_tail_or_the_text_and_keeps_the_rest(self):
        self.fm("task", "new", "A", "--type", "FIX", "--tier", "S", "--ac", "typo gone", "--ac", "old :: false")
        self.fm("task", "ac", "T-0001", "edit", "1", "--verify", "true")
        self.fm("task", "ac", "T-0001", "edit", "2", "--verify", "grep -q x README")
        self.fm("task", "ac", "T-0001", "edit", "2", "--text", "README says x")
        b = c.find_brief(c.find_project(self.repo), "T-0001")
        self.assertEqual(b.verify_cmds(), [(1, "true"), (2, "grep -q x README")])
        self.assertIn("README says x", b.section("Acceptance criteria"))
        self.assertNotIn("old", b.section("Acceptance criteria"))
        p = self.fm("task", "ac", "T-0001", "edit", "1", "--verify", "typed: screenshot review")
        self.assertIn("typed", p.stderr)  # a typed check isn't a command: finish would run it
        self.assertNotEqual(self.fm("task", "ac", "T-0001", "edit", "3", "--verify", "true", check=False).returncode, 0)
        self.assertNotEqual(self.fm("task", "ac", "T-0001", "edit", "1", check=False).returncode, 0)
        log = c.find_brief(c.find_project(self.repo), "T-0001").section("Log")
        self.assertIn("criterion 2 verify: `false` → `grep -q x README`", log)  # every edit leaves a visible trail
        # T-0342: once a criterion has evidence its check is fixed: an edit could weaken a failing one until it passes
        self.fm("task", "evidence", "T-0001", "--ac", "2", "--run", "false", check=False)
        p = self.fm("task", "ac", "T-0001", "edit", "2", "--verify", "true", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("evidence", p.stderr)
        self.assertIn("grep -q x README", c.find_brief(c.find_project(self.repo), "T-0001").section("Acceptance criteria"))


class Checks(ForemanTestCase):
    def test_every_failing_class_is_printed_and_ledgered(self):  # T-3397: the summary line cut the list at ~200 chars
        self.fm("init")
        names = [f"test_mod_{i}.LongClassNameNumber{i}" for i in range(12)]
        with open(os.path.join(self.repo, "run.py"), "w") as f:
            f.write("import sys\nprint(''.join('  failed class: ' + n + '\\n' for n in " + repr(names) + "))\n"
                    "print('Ran 9 tests\\n\\nFAILED (12 classes: x, +7 more)')\nsys.exit(1)\n")
        self.fm("check", "add", "python3 run.py")
        p = self.fm("check", "--fresh", check=False)
        for n in names:
            self.assertIn(f"\n    {n}\n", p.stdout)
        ev = [e for e in c.ledger_tail(c.find_project(self.repo), 50) if e["event"] == "check_run"][-1]
        self.assertEqual(sorted(ev["data"]["failed"]), sorted(names))
        self.assertEqual(c.failed_classes("  failed class: a.B\n  failed class: c.D\n"), ["a.B", "c.D"])
        self.assertEqual(c.failed_classes("  failed class: x.Fake\nnoise\n  failed class: a.B\r\n  failed class: c.D\r\n"),
                         ["a.B", "c.D"], "only the last contiguous block, CRLF too")
        many = c.class_lines([f"m.C{i}" for i in range(80)])
        self.assertEqual((len(many), many[-1]), (51, "    (+30 left out (sampo check --json))"))

    def test_a_red_gate_names_its_classes_in_the_finish_refusal_and_the_affected_print(self):  # T-3397
        self.fm("init")
        names = [f"test_mod_{i}.Class{i}" for i in range(60)]
        with open(os.path.join(self.repo, "run.py"), "w") as f:
            f.write("import sys\nprint(''.join('  failed class: ' + n + '\\n' for n in " + repr(names) + "))\nsys.exit(1)\n")
        tid = self.fm_json("task", "new", "T", "--type", "FIX", "--tier", "S", "--ac", "a :: true", "--step", "s",
                           "--focus")["id"]
        self.fm("task", "set", tid, "--section", "Regression test", "--text", "none: fixture")
        p = self.fm("task", "finish", tid, "--run", "python3 run.py", "--audit", "self check", check=False)
        self.assertIn("      test_mod_0.Class0\n", p.stderr)
        self.assertIn("      test_mod_49.Class49\n", p.stderr)
        self.assertNotIn("test_mod_50.Class50", p.stderr)
        self.assertIn("(+10 left out (sampo check --json))", p.stderr)
        for rel in ("src/parser.py", "tests/test_parser.py"):
            os.makedirs(os.path.join(self.repo, os.path.dirname(rel)), exist_ok=True)
            with open(os.path.join(self.repo, rel), "w") as f:
                f.write("x = 1\n")
        subprocess.run(["git", "-C", self.repo, "add", "-A"], check=True)
        subprocess.run(["git", "-C", self.repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x"],
                       check=True)
        self.fm("check", "affected", "python3 run.py {tests}")
        with open(os.path.join(self.repo, "src/parser.py"), "a") as f:
            f.write("y = 2\n")
        out = self.fm("check", "--affected", check=False).stdout
        self.assertIn("\n    test_mod_0.Class0\n", out)
        self.assertIn("(+10 left out (sampo check --json))", out)

    def test_a_slow_gate_reruns_only_its_failing_tests(self):  # T-1678
        self.fm("init")
        with open(os.path.join(self.repo, "run.py"), "w") as f:
            f.write("import sys\nif sys.argv[1:] == ['test_m.C.test_x']:\n    print('Ran 1 test\\n\\nOK')\n"
                    "    sys.exit(0)\nprint('FAIL: test_x (test_m.C.test_x)\\nRan 9 tests\\n\\nFAILED (failures=1)')\n"
                    "sys.exit(1)\n")
        self.fm("check", "add", "python3 run.py")
        slow = {"FOREMAN_SLOW_GATE": "0"}  # every gate counts as slow
        p = self.fm("check", "--fresh", env=slow, check=False)
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertIn("passed on a rerun of just them", p.stdout)
        self.assertEqual(c.load_briefs(c.find_project(self.repo)), [], "no baseline task for a load-spike failure")
        from sampo import cli
        self.assertIsNone(cli._narrow_rerun("python3 run.py two", "FAIL: t (m.C.t)\nFAILED (failures=2)\n"),
                          "a failure the names don't cover: the whole gate stands")
        self.assertIsNone(cli._narrow_rerun("go test ./...", "--- FAIL: TestX\n"))
        self.assertEqual(cli._narrow_rerun("pytest -q", "FAILED a.py::t - x\n=== 1 failed, 3 passed ===\n"),
                         "pytest -q a.py::t")
        self.assertIsNone(cli._narrow_rerun("pytest -q", "FAILED a.py::t - x\n=== 1 failed, 1 error ===\n"))

    def test_the_suite_gate_deadline_scales_with_load_and_a_timeout_names_both(self):  # T-3174
        from sampo import cli
        suite = "python3 plugin/tests/run.py"
        for load, want in ((1, 1800), (16, 1800), (32, 3600), (400, 7200)):  # per 16 CPUs; capped at 7200
            os.environ["FOREMAN_HOST"] = f"load={load},cpus=16"
            self.addCleanup(os.environ.__setitem__, "FOREMAN_HOST", CALM_HOST)
            self.assertEqual(cli._gate_deadline(suite, 600), want)
            self.assertEqual(cli._gate_deadline("python3 plugin/tests/run.py test_cli", 600), min(7200, 600 * max(1, load / 16)))
        self.fm("init")
        self.fm("check", "add", "sleep 5")
        p = self.fm("check", "--fresh", "--timeout", "1", check=False, env={"FOREMAN_HOST": "load=6,cpus=4"})  # 1.5: at the bar, no wait
        self.assertEqual(p.returncode, 1)
        self.assertIn("load 1.5 per CPU, deadline 1.5s", p.stdout)

    def test_a_gate_flaky_again_and_again_fails(self):
        # final review: a racy bug that passes half the time mustn't keep passing as "flaky"
        self.fm("init")
        flag = os.path.join(self.tmp, "odd")
        self.fm("check", "add", f"if [ -e {flag} ]; then rm {flag}; exit 0; else touch {flag}; exit 1; fi")
        codes = [self.fm("check", "--fresh", check=False) for _ in range(3)]
        self.assertEqual([p.returncode for p in codes], [0, 0, 1])
        self.assertIn("flaky again", codes[2].stdout)

    def test_a_failed_gate_names_its_failing_test_and_how(self):  # T-1884: not the summary's last 10 lines
        self.fm("init")
        with open(os.path.join(self.repo, "run.py"), "w") as f:
            f.write("import sys\nprint('FAIL: test_x (test_m.C.test_x)\\n---\\nAssertionError: the playbook grew')\n"
                    "print('\\n' * 12 + 'Ran 9 tests\\n\\nFAILED (failures=1)')\nsys.exit(1)\n")
        self.fm("check", "add", "python3 run.py")
        p = self.fm("check", "--fresh", check=False)
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("    failing: test_m.C.test_x\n", p.stdout)
        self.assertIn("    AssertionError: the playbook grew", p.stdout)

    def test_a_pass_on_the_same_tree_is_reused_and_affected_runs_only_linked_tests(self):
        # round 5 (T-0063): the ~100 s suite ran twice per tree (step and criterion evidence); iterate on linked tests
        self.fm("init")
        log = os.path.join(self.tmp, "runs.log")
        self.fm("check", "add", f"echo full >> {log}")
        self.fm("check")
        out = self.fm("check").stdout
        self.assertIn("cached", out)
        self.assertEqual(read_text(log).count("full"), 1)
        with open(os.path.join(self.repo, "new.txt"), "w") as f:
            f.write("x\n")
        self.fm("check")
        self.fm("check", "--fresh")
        self.assertEqual(read_text(log).count("full"), 3)
        for rel in ("src/parser.py", "tests/test_parser.py", "tests/test_report.py", "src/report.py"):
            os.makedirs(os.path.join(self.repo, os.path.dirname(rel)), exist_ok=True)
            with open(os.path.join(self.repo, rel), "w") as f:
                f.write("x = 1\n")
        subprocess.run(["git", "-C", self.repo, "add", "-A"], check=True)
        subprocess.run(["git", "-C", self.repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x"],
                       check=True)
        self.fm("check", "affected", f"echo {{tests}} >> {log}")
        with open(os.path.join(self.repo, "src/parser.py"), "a") as f:
            f.write("y = 2\n")
        out = self.fm("check", "--affected").stdout
        self.assertIn("affected", out)
        self.assertIn("tests/test_parser.py", read_text(log))
        self.assertNotIn("tests/test_report.py", read_text(log))
        for rel in ("src/demo.py", "tests/test_demo.py"):  # T-0875: new and untracked, so the map has no link for it
            with open(os.path.join(self.repo, rel), "w") as f:
                f.write("x = 1\n")
        self.fm("check", "--affected")
        self.assertIn("tests/test_demo.py", read_text(log))

    def test_a_changed_test_the_template_cannot_run_is_named(self):
        # T-1131: a .tsx test under a Python template was dropped and the result read "No tests linked"
        self.fm("init")
        self.fm("check", "affected", "python3 -m unittest {names}")
        os.makedirs(os.path.join(self.repo, "web"), exist_ok=True)
        with open(os.path.join(self.repo, "web", "pane.test.tsx"), "w") as f:
            f.write("test('x', () => {})\n")
        out = self.fm("check", "--affected", check=False).stdout
        self.assertIn("web/pane.test.tsx", out)
        self.assertIn("can't run", out)

    def test_each_test_extension_gets_its_own_runner(self):
        # T-1135: a Python default plus `--for .tsx`: a changed .py and .tsx test run both commands, once each
        from sampo import cli
        self.fm("init")
        self.fm("check", "affected", "echo py {names}")
        before = cli.affected(c.find_project(self.repo, create=False))
        self.fm("check", "affected", "--for", ".tsx", "echo tsx")
        os.makedirs(os.path.join(self.repo, "web"), exist_ok=True)
        for n in ("test_a.py", "pane.test.tsx"):
            with open(os.path.join(self.repo, "web", n), "w") as f:
                f.write("x\n")
        cmd, tests, _ = cli.affected(c.find_project(self.repo, create=False))
        self.assertEqual(cmd, "echo py test_a && echo tsx")
        self.assertEqual(sorted(tests), ["web/pane.test.tsx", "web/test_a.py"])
        out = self.fm("check", "--affected", check=False).stdout
        self.assertNotIn("can't run", out)
        self.assertEqual(before[0], None)

    def test_bare_check_affected_shows_and_only_clear_unsets(self):
        # T-3671: a bare `check affected` (a look) wiped the template; now it shows, --clear unsets, and it's logged
        self.fm("init")
        self.fm("check", "affected", "echo py {names}")
        self.fm("check", "affected", "--for", ".tsx", "echo tsx")
        shown = self.fm("check", "affected").stdout
        self.assertIn("echo py {names}", shown)
        self.assertIn("echo tsx", self.fm("check", "affected", "--for", ".tsx").stdout)
        p = c.find_project(self.repo, create=False)
        self.assertEqual(c.read_meta(p)["affected"], "echo py {names}")
        self.assertEqual(c.read_meta(p)["affected_by"], {".tsx": "echo tsx"})
        self.fm("check", "affected", "--clear", "--for", "tsx")
        self.assertEqual(c.read_meta(p)["affected_by"], {})
        self.fm("check", "affected", "--clear")
        self.assertEqual(c.read_meta(p)["affected"], "")
        with open(os.path.join(p.dir, "ledger.jsonl")) as f:
            self.assertIn("affected_template", f.read())

    def test_a_flaky_gate_is_rerun_and_labelled_and_old_failures_are_told_apart(self):
        # T-0047: a failure that passes on a rerun is flaky; one that already failed before the task is pre-existing
        self.fm("init")
        flag = os.path.join(self.tmp, "ran-once")
        self.fm("check", "add", f"test -e {flag} || {{ touch {flag}; exit 1; }}")
        p = self.fm("check", check=False)
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertIn("flaky", p.stdout)
        self.assertRegex(p.stdout, r"\d+\.\d s")
        self.fm("check", "rm", "1")
        self.fm("check", "add", "exit 3")
        self.assertNotIn("pre-existing", self.fm("check", check=False).stdout)
        self.fm("task", "new", "Other work", "--type", "CLEAN", "--tier", "S", "--ac", "ok", "--step", "do", "--focus")
        p = self.fm("check", check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("pre-existing", p.stdout)

    """T-0025: the project's gates in one command that can't be masked by a pipe."""

    def test_gates_run_together_and_any_failure_fails(self):
        self.fm("init")
        self.assertNotEqual(self.fm("check", check=False).returncode, 0, "no gates configured is not a pass")
        self.fm("check", "add", "echo suite ok")
        self.fm("check", "add", "echo 'lint: 2 errors'; exit 1")
        self.assertIn("echo suite ok", self.fm("check", "list").stdout)
        p = self.fm("check", check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("✓ echo suite ok", p.stdout)
        self.assertIn("✗", p.stdout)
        self.assertIn("lint: 2 errors", p.stdout)
        self.fm("check", "rm", "2")
        self.assertIn("✓ echo suite ok", self.fm("check").stdout)

    def test_odd_output_missing_bash_and_no_timeout_are_handled(self):
        # round-1 edge audit: binary output, no bash on PATH, --timeout 0 ("no limit"), concurrent adds
        self.fm("init")
        self.fm("task", "new", "Fix it", "--type", "FIX", "--tier", "S", "--step", "fix", "--ac", "works")
        p = self.fm("task", "evidence", "T-0001", "--step", "1", "--run", r"printf 'ok \377\376\n'", check=False)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.fm("task", "evidence", "T-0001", "--step", "1", "--run", "true", "--timeout", "0",
                                 check=False).returncode, 0)
        p = self.fm("task", "evidence", "T-0001", "--step", "1", "--run", "true", env={"PATH": self.tmp}, check=False)
        self.assertNotIn("Traceback", p.stderr)
        self.assertIn("exit 127", c.find_brief(c.find_project(self.repo), "T-0001").evidence()[-1])
        threads = [threading.Thread(target=self.fm, args=("check", "add", f"echo {i}")) for i in range(6)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(len(c.read_meta(c.find_project(self.repo)).get("checks")), 6)

    def test_a_failing_gate_is_not_hidden_by_a_later_passing_one_or_by_typed_evidence(self):
        # round-1 adversary audit: only runs decide, and one fm check run is one verdict
        self.fm("init")
        self.fm("check", "add", "false")
        self.fm("check", "add", "true")
        self.fm("task", "new", "Fix it", "--type", "FIX", "--tier", "S", "--step", "fix", "--ac", "works")
        self.assertEqual(self.fm("check", "--evidence", "T-0001", "--step", "1", check=False).returncode, 1)
        self.assertNotEqual(self.fm("task", "step", "T-0001", "done", "1", check=False).returncode, 0)
        self.fm("task", "evidence", "T-0001", "--step", "1", "pytest", "exit 0 · 5 passed [ran] [tree deadbeef0000]")
        self.assertNotEqual(self.fm("task", "step", "T-0001", "done", "1", check=False).returncode, 0,
                            "typed evidence can't clear a failed run")
        b = c.find_brief(c.find_project(self.repo), "T-0001")
        self.assertNotEqual(b.last_work_tree()[1], "deadbeef0000", "typed text can't forge a worktree id")
        self.fm("check", "rm", "1")
        self.fm("check", "--evidence", "T-0001", "--step", "1")
        self.fm("task", "step", "T-0001", "done", "1")

    def test_results_become_evidence(self):
        self.fm("init")
        self.fm("check", "add", "echo suite ok")
        self.fm("task", "new", "Fix it", "--type", "FIX", "--tier", "S", "--step", "fix", "--ac", "works")
        self.fm("check", "--evidence", "T-0001", "--step", "1")
        line = c.find_brief(c.find_project(self.repo), "T-0001").evidence()[-1]
        self.assertIn("echo suite ok", line)
        self.assertIn("exit 0", line)
        self.fm("task", "step", "T-0001", "done", "1")


class NewTaskCriteria(ForemanTestCase):
    def test_ac_can_carry_its_verify_command(self):
        # T-0042: an M task made in one command must be focusable (every criterion needs a verify command)
        self.fm("task", "new", "Add export", "--type", "FEATURE", "--tier", "M", "--ac", "exports CSV :: pytest -k csv",
                "--step", "build it")
        for sec in ("Interpretation", "Approach (options → choice → why)", "Risks and rollback"):
            self.fm("task", "set", "T-0001", "--section", sec, "--text", "Reading 1: a. Reading 2: b. 1) a; fails if: x 2) use what exists: b; fails if: y 3) do less: c; fails if: z Choice 1 (80%): a")
        self.fm("focus", "T-0001")
        ac = c.find_brief(c.find_project(self.repo), "T-0001").acceptance()[0]
        self.assertEqual(ac.text, "exports CSV — verify with `pytest -k csv`")
        self.fm("task", "ac", "T-0001", "add", "quotes fields :: pytest -k quote")  # same form on ac add
        ac = c.find_brief(c.find_project(self.repo), "T-0001").acceptance()[1]
        self.assertEqual(ac.text, "quotes fields — verify with `pytest -k quote`")


class AuditPrep(ForemanTestCase):
    """T-0026: the diff since the task started, frozen, plus one ready reviewer brief per lens."""

    def test_the_brief_names_the_review_checklists_resolved_for_this_project(self):
        # T-1681: the reviewer read playbooks by path, skipping {{fact}} fill-ins and the project's overlays
        self.fm("init")
        self.fm("task", "new", "Edit", "--type", "FIX", "--tier", "S", "--ac", "works :: true", "--step", "do it",
                "--focus")
        overlay = os.path.join(self.repo, ".claude", "playbooks", "review")
        os.makedirs(overlay)
        with open(os.path.join(overlay, "python.md"), "w") as f:
            f.write("Our rule: no bare except in handlers.\n")
        with open(os.path.join(self.repo, "app.py"), "w") as f:
            f.write("X = 1\n")
        self.fm("audit", "prep", "T-0001", "--lens", "adversary")
        p = c.find_project(self.repo)
        brief = read_text(os.path.join(p.dir, "audits", "T-0001.review.md"))
        m = re.search(r"(?m)^Checklists[^:]*: (\S+)$", brief)
        self.assertTrue(m, brief[-400:])
        text = read_text(m.group(1))
        self.assertIn("review/code-reviewer", text)
        self.assertIn("review/python", text)
        self.assertIn("Our rule: no bare except in handlers.", text, "the project's overlay")
        self.assertNotIn("review/rust", text, "only what the diff's languages call for")

    def test_split_writes_parallel_briefs_by_lens_group(self):
        # T-0216: an L review in up to three fresh contexts (claudekit-style), opt-in for usage
        self.fm("init")
        self.fm("task", "new", "Big change", "--type", "FEATURE", "--tier", "L", "--step", "do it")
        self.fm("task", "ac", "T-0001", "add", "works", "--verify", "true")
        for sec in ("Interpretation", "Approach (options → choice → why)", "Risks and rollback"):
            self.fm("task", "set", "T-0001", "--section", sec, "--text", "Reading 1: a. Reading 2: b. 1) a; fails if: x 2) use what exists: b; fails if: y 3) do less: c; fails if: z Choice 1 (80%): a")
        self.fm("task", "set", "T-0001", "approved=true", "--section", "Build vs reuse", "--text", "nothing fits")
        self.fm("focus", "T-0001")
        with open(os.path.join(self.repo, "big.py"), "w") as f:
            f.write("".join(f"X{i} = {i}\n" for i in range(900)))
        one = self.fm("audit", "prep", "T-0001").stdout
        self.assertIn("Run one sampo:fm-reviewer", one)
        self.assertIn("--split", one, "a big L diff gets the suggestion")
        out = self.fm("audit", "prep", "T-0001", "--split").stdout
        briefs = sorted(set(re.findall(r"\S+T-0001\.review-\d\.md", out)))
        self.assertEqual(len(briefs), 3, out)
        self.assertIn("in parallel", out)
        lenses = [re.findall(r"(?m)^## (\w+)$", read_text(b)) for b in briefs]
        self.assertEqual(sorted(sum(lenses, [])), sorted(["intent", "adversary", "edge", "operator", "maintainer"]))
        self.assertTrue(all(lenses), "no empty group")

    def test_long_compacted_diff_tells_the_reviewer_to_read_in_parts(self):
        # T-0983 (written by the recorded demo session, T-0979): a diff over ~1500 compacted lines doesn't fit one Read
        self.fm("init")
        self.fm("task", "new", "Add it", "--type", "FEATURE", "--tier", "M", "--step", "add it")
        self.fm("task", "ac", "T-0001", "add", "works", "--verify", "true")
        for sec in ("Interpretation", "Approach (options → choice → why)", "Risks and rollback"):
            self.fm("task", "set", "T-0001", "--section", sec, "--text", "Reading 1: a. Reading 2: b. 1) a; fails if: x 2) use what exists: b; fails if: y 3) do less: c; fails if: z Choice 1 (80%): a")
        self.fm("focus", "T-0001")
        with open(os.path.join(self.repo, "big.py"), "w") as f:
            f.write("".join(f"X{i} = {i}\n" for i in range(1600)))
        self.assertIn("read it in parts", self.fm("audit", "prep", "--print", "T-0001").stdout)
        with open(os.path.join(self.repo, "big.py"), "w") as f:
            f.write("X = 1\n")
        self.assertNotIn("read it in parts", self.fm("audit", "prep", "--print", "T-0001").stdout)

    def test_brief_asks_for_report_findings_fields_and_prep_names_the_tool(self):
        # T-0780: findings reach the host review UI; the ranked lines still parse (_FINDING)
        self.fm("init")
        self.fm("task", "new", "Add it", "--type", "FEATURE", "--tier", "M", "--step", "add it")
        self.fm("task", "ac", "T-0001", "add", "works", "--verify", "true")
        for sec in ("Interpretation", "Approach (options → choice → why)", "Risks and rollback"):
            self.fm("task", "set", "T-0001", "--section", sec, "--text", "Reading 1: a. Reading 2: b. 1) a; fails if: x 2) use what exists: b; fails if: y 3) do less: c; fails if: z Choice 1 (80%): a")
        self.fm("focus", "T-0001")
        with open(os.path.join(self.repo, "x.py"), "w") as f:
            f.write("X = 1\n")
        out = self.fm("audit", "prep", "T-0001").stdout
        self.assertIn("ReportFindings", out)
        for field in ("file", "line", "short_summary", "failure_scenario", "category"):
            self.assertIn(field, cli._REVIEW_OUT)
        self.assertIn("HIGH/MEDIUM/LOW", cli._REVIEW_OUT)
        self.assertTrue(cli._FINDING.search("- HIGH a.py:3 boom"))

    def test_prep_freezes_the_diff_and_prints_lens_briefs(self):
        self.fm("init")
        self.fm("task", "new", "Add --verbose", "--type", "FEATURE", "--tier", "L", "--step", "add it")
        self.fm("task", "ac", "T-0001", "add", "prints more with --verbose", "--verify", "pytest")
        for sec in ("Interpretation", "Approach (options → choice → why)", "Risks and rollback"):
            self.fm("task", "set", "T-0001", "--section", sec, "--text", "Reading 1: a. Reading 2: b. 1) a; fails if: x 2) use what exists: b; fails if: y 3) do less: c; fails if: z Choice 1 (80%): a")
        self.fm("task", "set", "T-0001", "approved=true", "--section", "Build vs reuse", "--text", "nothing fits")
        self.fm("focus", "T-0001")
        with open(os.path.join(self.repo, "cli.py"), "w") as f:
            f.write("VERBOSE = True\n")  # new, uncommitted and untracked
        out = self.fm("audit", "prep", "--print", "T-0001").stdout
        diff = next(w for w in out.split() if w.endswith("T-0001.diff"))
        self.assertIn("VERBOSE = True", read_text(diff))
        for lens, needle in (("intent", "prints more with --verbose"), ("adversary", "trying to break"),
                             ("edge", "environment"), ("operator", "real machine"), ("maintainer", "next year")):
            self.assertIn(needle, out, lens)
        only = self.fm("audit", "prep", "--print", "T-0001", "--lens", "adversary").stdout
        self.assertIn("trying to break", only)
        self.assertNotIn("next year", only)
        with open(os.path.join(self.repo, "blob.bin"), "wb") as f:
            f.write(b"\xff\xfe binary \x00\n")  # a diff that isn't UTF-8
        self.assertIn("trying to break", self.fm("audit", "prep", "--print", "T-0001", "--lens", "adversary").stdout)

    def test_audit_diff_leaves_out_secrets(self):
        # T-0413 (JARVIS): an untracked .env backup was frozen into the audit diff, secrets and all
        self.fm("init")
        self.fm("task", "new", "Fix it", "--type", "FIX", "--tier", "S", "--ac", "works", "--step", "fix", "--focus")
        with open(os.path.join(self.repo, ".env.bak-20261009-ai-container"), "w") as f:
            f.write("DB_PASS_PHRASE=correct-horse-battery-staple\n")  # pragma: allowlist secret
        with open(os.path.join(self.repo, "cfg.py"), "w") as f:
            f.write('password = "hunter2hunter2"\nDEBUG = True\n')  # pragma: allowlist secret
        out = self.fm("audit", "prep", "--print", "T-0001").stdout
        diff = read_text(next(w for w in out.split() if w.endswith("T-0001.diff")))
        self.assertNotIn("correct-horse-battery-staple", diff)
        self.assertNotIn("hunter2hunter2", diff)
        self.assertIn("diff --git a/.env.bak-20261009-ai-container b/.env.bak-20261009-ai-container", diff)
        self.assertIn("contents left out", diff)
        self.assertIn("DEBUG = True", diff)
        self.assertIn("secret-looking value added in cfg.py", out, "the pre-audit still reads the raw diff")

    def test_lens_briefs_carry_this_projects_past_findings_for_that_lens(self):
        # round 9 (T-0018): a reviewer starts from the weak spots earlier reviews of this project found
        self.fm("init")
        self.fm("task", "new", "Fix it", "--type", "FIX", "--tier", "S", "--ac", "works", "--step", "fix", "--focus")
        self.fm("research", "add", "t0005-adversary", input=(
            "## Verdict: changes needed\n**HIGH — fmguard.py:10 — tree writes bypass the core check**\n"
            "**LOW — a nit**\n- **MEDIUM — fmsync.py:3** — imported titles reach the terminal \x1b[31munsanitized\n"))
        report = json.dumps({"message": {"role": "assistant", "content": [
            {"type": "text", "text": "Verdict: changes needed\n1 MED stale grants survive a restart\n"}]}})
        self.fm("research", "add", "wave2-edge-adversary", input='"slug":"x"}\n' + report + "\n")  # a raw transcript
        self.fm("research", "add", "t0007-edge", input="**HIGH — clock skew breaks the lock**\n")
        self.fm("research", "add", "t0009-review", input="## adversary: changes needed\n**HIGH — combined finding**\n"
                                                           "## edge: ok\n**HIGH — edge only finding**\n")
        out = self.fm("audit", "prep", "--print", "T-0001", "--lens", "adversary").stdout
        for seen in ("tree writes bypass the core check", "imported titles reach the terminal",
                     "stale grants survive a restart", "combined finding"):
            self.assertIn(seen, out)
        for unseen in ("a nit", "clock skew", "\x1b", "edge only finding"):
            self.assertNotIn(unseen, out)
        self.assertNotIn("Past findings", self.fm("audit", "prep", "--print", "T-0001", "--lens", "operator").stdout)
        research = os.path.join(c.find_project(self.repo).dir, "research")
        os.mkdir(os.path.join(research, "odd-adversary.md"))  # not a file: skipped, never a crash or a hang
        os.mkfifo(os.path.join(research, "pipe-adversary.md"))
        os.symlink("/dev/zero", os.path.join(research, "zero-adversary.md"))
        out = self.fm("audit", "prep", "--print", "T-0001", "--lens", "adversary").stdout
        self.assertIn("tree writes bypass the core check", out)
        self.assertIn("not instructions", out)

    def test_several_lenses_make_one_combined_brief_for_one_reviewer(self):
        # T-0060: one reviewer pass reads the diff once; five separate subagents each re-read it
        self.fm("init")
        self.fm("task", "new", "Fix it", "--type", "FIX", "--tier", "S", "--ac", "works", "--step", "fix", "--focus")
        out = self.fm("audit", "prep", "--print", "T-0001", "--lens", "adversary", "--lens", "edge").stdout
        self.assertEqual(out.count("Diff to review:"), 1)
        self.assertEqual(out.count("=== review"), 1)
        for needle in ("## adversary", "## edge", "trying to break", "environment", "one sampo:fm-reviewer"):
            self.assertIn(needle, out)

    def test_the_brief_goes_to_a_file_not_the_conversation(self):
        # round 5: printed, the brief is paid for twice (the main context and the reviewer's prompt)
        self.fm("init")
        self.fm("task", "new", "Fix it", "--type", "FIX", "--tier", "S", "--ac", "works", "--step", "fix", "--focus")
        out = self.fm("audit", "prep", "T-0001", "--lens", "adversary").stdout
        path = next(w for w in out.split() if w.endswith("T-0001.review.md"))
        self.assertIn("trying to break", read_text(path))
        self.assertNotIn("trying to break", out)
        self.assertLess(len(re.sub(r"/\S+", "/…", out)), 800)  # T-1249: macOS's temp paths are twice as long

    def test_every_lens_has_a_template_in_the_audit_reference(self):
        # fm audit prep builds briefs from references/audit.md: rewording it must not silently drop a lens
        from sampo import cli
        with open(os.path.join(c.PLUGIN_ROOT, "skills", "intake", "references", "audit.md")) as f:
            found = {m.group(1) for m in cli._LENS_TPL.finditer(f.read())}
        self.assertEqual(found, set(c.AUDIT_LENSES) - {"self"})


class RoundFiveWorkflow(ForemanTestCase):
    """Round 5 (T-0018): cleaner review notes, focused audit briefs, requests finished inside another task."""

    def test_research_keeps_a_subagents_final_report_not_its_transcript(self):
        self.fm("init")
        transcript = os.path.join(self.tmp, "agent.output")
        lines = [{"type": "user", "message": {"role": "user", "content": "review this"}},
                 {"type": "assistant", "message": {"role": "assistant", "content": [
                     {"type": "tool_use", "name": "Read", "input": {"file_path": "x"}}]}},
                 {"type": "assistant", "message": {"role": "assistant", "content": [
                     {"type": "text", "text": "## Verdict: changes needed\n\n**HIGH** — x.py:3 — broken"}]}}]
        with open(transcript, "w") as f:
            f.write("\n".join(json.dumps(l) for l in lines) + "\n")
        path = self.fm_json("research", "add", "r1-edge", "--from-agent", transcript)["path"]
        text = read_text(path)
        self.assertTrue(text.startswith("## Verdict: changes needed"), text[:80])
        self.assertNotIn("tool_use", text)
        plain = os.path.join(self.tmp, "report.md")
        with open(plain, "w") as f:
            f.write("## Verdict: ok\n")
        self.assertIn("## Verdict: ok", read_text(self.fm_json("research", "add", "r2", "--from-agent", plain)["path"]))
        # final maintainer audit: a FIFO or device would hang the read, a directory would crash it
        os.mkfifo(os.path.join(self.tmp, "fifo"))
        for bad in ("/dev/zero", os.path.join(self.tmp, "fifo"), self.tmp):
            for flag in ("--from-agent", "--file"):
                with self.subTest(bad=bad, flag=flag):
                    p = self.fm("research", "add", "r3", flag, bad, check=False)
                    self.assertEqual(p.returncode, 1)
                    self.assertIn("regular file", p.stderr)

    def test_audit_prep_note_reaches_the_review_brief(self):
        self.fm("init")
        self.fm("task", "new", "Add sync", "--type", "FEATURE", "--tier", "L", "--step", "build it")
        self.fm("task", "ac", "T-0001", "add", "syncs", "--verify", "pytest")
        for sec in ("Interpretation", "Approach (options → choice → why)", "Risks and rollback"):
            self.fm("task", "set", "T-0001", "--section", sec, "--text", "Reading 1: a. Reading 2: b. 1) a; fails if: x 2) use what exists: b; fails if: y 3) do less: c; fails if: z Choice 1 (80%): a")
        self.fm("task", "set", "T-0001", "--section", "Risks and rollback", "--text", "revert the commit")
        self.fm("task", "set", "T-0001", "approved=true", "--section", "Build vs reuse", "--text", "nothing fits")
        self.fm("focus", "T-0001")
        out = self.fm("audit", "prep", "--print", "T-0001", "--note", "threat: a pulled .foreman/ is untrusted",
                      "--note", "round 3 only").stdout
        self.assertEqual(out.count("threat: a pulled .foreman/ is untrusted"), 1)  # one combined brief (T-0060)
        self.assertEqual(out.count("round 3 only"), 1)

    def test_a_request_done_inside_another_task_is_done_there(self):
        self.fm("init")
        self.fm("task", "new", "Umbrella", "--type", "FEATURE", "--tier", "S", "--step", "all of it", "--ac", "works")
        self.fm("capture", "export as CSV")
        p0 = self.fm("task", "drop", "T-0002", "--done-in", "T-0001", check=False)
        self.assertEqual(p0.returncode, 2, "the host must have done work (evidence), or nothing was done anywhere")
        self.fm("task", "evidence", "T-0001", "--step", "1", "--run", "true")
        # T-0364: "clean up all docs, code, UI, settings, environment" closed inside a docs-and-dead-code task, and UI,
        # settings and environment were never looked at: the user's words need a reason and travel to the host
        p1 = self.fm("task", "drop", "T-0002", "--done-in", "T-0001", check=False)
        self.assertEqual(p1.returncode, 2)
        self.assertIn("reason", p1.stderr)
        out = self.fm("task", "drop", "T-0002", "covered by the export step", "--done-in", "T-0001").stdout
        self.assertIn("done in T-0001", out)
        p = c.find_project(self.repo)
        folded, host = c.find_brief(p, "T-0002"), c.find_brief(p, "T-0001")
        self.assertEqual((folded.status, folded.meta.get("done_in")), ("done", "T-0001"))
        self.assertIn("includes T-0002", host.section("Log"))
        self.assertIn("export as CSV", host.section("Raw request"), "the host's intent audit sees every folded ask")
        self.assertNotEqual(self.fm("task", "drop", "T-0001", "--done-in", "T-0999", check=False).returncode, 0)
        self.assertNotEqual(self.fm("task", "drop", "T-0001", "--done-in", "T-0001", check=False).returncode, 0)
        # work that was started keeps its own gates: --done-in isn't a way around evidence and audits
        self.fm("capture", "another")
        p2 = self.fm("task", "drop", "T-0001", "--done-in", "T-0003", check=False)
        self.assertEqual(p2.returncode, 2)
        self.assertIn("started", p2.stderr)


class NextAction(ForemanTestCase):
    def test_next_names_the_one_required_action(self):
        self.fm("init")
        self.assertIn("queue is empty", self.fm("next").stdout)
        self.fm("task", "new", "Fix login", "--type", "FIX", "--tier", "S")
        self.assertIn("missing acceptance criterion, step", self.fm("next").stdout)
        self.fm("task", "ac", "T-0001", "add", "works", "--verify", "pytest")
        self.fm("task", "step", "T-0001", "add", "reproduce")
        self.assertIn("sampo focus T-0001", self.fm("next").stdout)
        res = self.fm_json("next")
        self.assertEqual((res["task"], res["stage"]), ("T-0001", "ready"))


class Autonomy(ForemanTestCase):
    def test_autonomy_defaults_to_standard_and_switches(self):
        self.fm("init")
        self.assertEqual(self.fm_json("state")["autonomy"], "standard")
        self.assertIn("standard", self.fm("autonomy").stdout)
        self.fm("autonomy", "full")
        self.assertEqual(self.fm_json("state")["autonomy"], "full")
        self.assertEqual(self.fm("autonomy", "reckless", check=False).returncode, 2)


class DecisionsResearchSelf(ForemanTestCase):
    def test_decide_appends_a_table_row_and_logs(self):
        self.fm("init")
        self.fm("decide", "Use Python stdlib for fm", "--why", "jq missing; python on PATH", "--rejected", "sh+jq")
        text = read_text(os.path.join(c.find_project(self.repo).dir, "decisions.md"))
        self.assertRegex(text, r"\| \d{4}-\d\d-\d\d \| Use Python stdlib for fm \| jq missing; python on PATH \| sh\+jq \|")
        self.assertIn("decision", [e["event"] for e in c.ledger_tail(c.find_project(self.repo))])

    def test_decide_escapes_pipes(self):
        self.fm("init")
        self.fm("decide", "a | b", "--why", "c|d")
        text = read_text(os.path.join(c.find_project(self.repo).dir, "decisions.md"))
        self.assertIn("a \\| b", text)

    def test_research_add_from_stdin_and_file(self):
        self.fm("init")
        self.fm("research", "add", "auth-recon", input="## Findings\n- src/auth.py:12 retries missing\n")
        path = os.path.join(c.find_project(self.repo).dir, "research", "auth-recon.md")
        self.assertIn("retries missing", read_text(path))
        src = os.path.join(self.tmp, "notes.md")
        with open(src, "w") as f:
            f.write("token=abcd1234efgh leaked\n")
        self.fm("research", "add", "notes", "--file", src)
        saved = read_text(os.path.join(c.find_project(self.repo).dir, "research", "notes.md"))
        self.assertNotIn("abcd1234efgh", saved)

    def test_research_closes_with_a_decision_that_reaches_decisions_md(self):
        # T-0896: research ends in a choice the project keeps; a warning at close was easy to walk past
        self.fm("init")
        tid = json.loads(self.fm("task", "new", "Which TOML parser", "--type", "RESEARCH", "--tier", "S", "--ac",
                                 "compared :: true", "--step", "compare", "--json").stdout)["id"]
        self.fm("focus", tid)
        p = self.fm("task", "finish", tid, "--run", "true", "--audit", "self check", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("Decision", p.stderr)
        self.fm("task", "set", tid, "--section", "Decision", "--text", "Use tomllib; would change if we need writes")
        self.fm("task", "finish", tid, "--run", "true", "--audit", "self check")
        text = read_text(os.path.join(c.find_project(self.repo).dir, "decisions.md"))
        self.assertIn("Use tomllib; would change if we need writes", text)
        self.assertIn(tid, text)

    def test_a_close_names_what_only_a_person_can_check(self):
        # T-0900: "the CSV opens in Excel" had no command of its own; a generic --run closed it as if proven
        self.fm("init")
        self.fm("task", "new", "CSV export", "--type", "FEATURE", "--tier", "S", "--ac", "it writes rows :: true",
                "--ac", "the CSV opens in Excel", "--step", "build it", "--focus")
        out = self.fm("task", "finish", "T-0001", "--run", "true", "--audit", "self check").stdout
        card = out.split("Not verified", 1)[1] if "Not verified" in out else ""
        self.assertIn("the CSV opens in Excel", card)
        self.assertNotIn("it writes rows", card)

    def test_a_stored_verify_the_guard_refuses_is_not_run(self):
        # T-0901: a verify command that never passed the hook (stored before the guard read them, or a brief another
        # clone synced in) gets the guard's checks before fm runs it; fm itself is run directly here, as such a brief is
        self.fm("init")
        gone = os.path.join(os.path.expanduser("~"), "fm-t0901-never-there")
        self.fm("task", "new", "tidy the parser", "--type", "FEATURE", "--tier", "S", "--ac", f"it parses :: rm -rf {gone}",
                "--step", "fix it", "--focus")
        p = self.fm("task", "finish", "T-0001", "--run", "true", "--audit", "self check", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("rm-outside", p.stderr)
        self.assertIn("won't run", p.stderr)
        self.assertFalse([e for e in c.ledger_tail(c.find_project(self.repo)) if e.get("event") == "evidence"],
                         "refused before anything ran")
        self.fm("check", "add", f"rm -rf {gone}")  # its review: a stored gate is run by fm check
        p = self.fm("check", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("won't run", p.stderr)

    def test_research_name_must_be_safe(self):
        self.fm("init")
        self.assertEqual(self.fm("research", "add", "../escape", input="x", check=False).returncode, 1)

    def test_capture_self_targets_the_foreman_repo_project(self):
        own = os.path.join(self.home)  # FOREMAN_HOME is itself a git repo in this test
        import subprocess
        subprocess.run(["git", "init", "-q", own], check=True)
        out = self.fm("capture", "stop gate too chatty", "--source", "self", "--self").stdout
        self.assertIn("T-0001", out)
        p = c.find_project(own)
        self.assertEqual(c.find_brief(p, "T-0001").meta["source"], "self")
        self.assertIsNone(c.find_project(self.repo))


class ResearchListShow(ForemanTestCase):
    def test_list_newest_first_and_show_prints(self):
        self.fm("init")
        self.fm("research", "add", "older", input="# Old notes\nbody one\n")
        folder = os.path.join(c.find_project(self.repo).dir, "research")
        os.utime(os.path.join(folder, "older.md"), (1, 1))
        self.fm("research", "add", "newer", input="plain first line\n")
        os.makedirs(os.path.join(folder, "brainstorm-0102"))
        with open(os.path.join(folder, "brainstorm-0102", "ideas.md"), "w") as f:
            f.write("# Ideas\nidea-zebra\n")
        os.utime(os.path.join(folder, "brainstorm-0102", "ideas.md"), (2, 2))
        out = self.fm("research", "list").stdout
        self.assertLess(out.index("newer"), out.index("brainstorm-0102"))
        self.assertLess(out.index("brainstorm-0102"), out.index("older"))
        self.assertIn("Old notes", out)
        self.assertIn("body one", self.fm("research", "show", "older.md").stdout)
        self.assertIn("idea-zebra", self.fm("research", "show", "brainstorm-0102").stdout)

    def test_show_unknown_names_closest(self):
        self.fm("init")
        self.fm("research", "add", "auth-recon", input="x\n")
        r = self.fm("research", "show", "auth-recn", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("auth-recon", r.stderr + r.stdout)


class AskClosed(ForemanTestCase):
    """T-0302: a grant only works while its task is active, so a yes on a closed task would be a yes for nothing."""

    def test_a_closed_task_gets_no_dialog_and_no_request(self):
        self.fm("init")
        tid = json.JSONDecoder().raw_decode(self.fm("task", "new", "Release", "--type", "CLEAN", "--tier", "S", "--ac",
                                                    "ok :: true", "--step", "a", "--focus", "--json").stdout)[0]["id"]
        self.fm("task", "finish", tid, "--run", "true", "--audit", "self check")
        cmd = f"fm ask {tid} plugin --why 'install it'"
        out = json.loads(self.hook("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": cmd}}).stdout)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("closed", out["hookSpecificOutput"]["permissionDecisionReason"])
        r = self.fm("ask", tid, "plugin", "--why", "install it", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("closed", r.stderr)
        self.assertFalse(c.read_meta(c.find_project(self.repo)).get("pending_approvals"))
        cmd = f"fm ask {tid} core --standing --why 'every task'"  # standing covers later tasks: any id will do
        out = json.loads(self.hook("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": cmd}}).stdout)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "ask")


class SlowVerifyNote(ForemanTestCase):  # T-3418
    CMD = "python3 plugin/tests/run.py test_lanes test_cli.Foo"

    def ran(self, cmd, s):
        self.fm("init")
        p = c.find_project(self.repo)
        c.log_event(p, "check_run", data={"results": [{"cmd": cmd, "exit": 0, "s": s}]})
        return p

    def test_a_slow_whole_module_run_is_noted_with_class_scoped_advice(self):
        note = cli._slow_verify_note(self.ran(self.CMD, 300), self.CMD)
        self.assertIn("test_lanes", note)
        self.assertIn("300", note)
        self.assertNotIn("test_cli", note)  # a class-scoped name is already narrow

    def test_a_timed_evidence_run_counts(self):
        self.fm("init")
        p = c.find_project(self.repo)
        c.log_event(p, "evidence", data={"cmd": self.CMD, "s": 200.0})
        self.assertIn("200", cli._slow_verify_note(p, self.CMD))

    def test_evidence_run_records_its_seconds(self):
        self.fm("init")
        tid = json.loads(self.fm("task", "new", "X", "--type", "FIX", "--tier", "S", "--json",
                                 "--ac", "ok :: true").stdout)["id"]
        self.fm("task", "evidence", tid, "--run", "true")
        ev = [e for e in c.ledger_tail(c.find_project(self.repo), 50) if e.get("event") == "evidence"]
        self.assertIsInstance(ev[-1]["data"].get("s"), float)

    def test_no_note_for_a_fast_run_a_scoped_command_or_no_timing(self):
        self.assertIsNone(cli._slow_verify_note(self.ran(self.CMD, 30), self.CMD))
        scoped = "python3 plugin/tests/run.py test_cli.Foo"
        self.assertIsNone(cli._slow_verify_note(self.ran(scoped, 300), scoped))
        self.assertIsNone(cli._slow_verify_note(self.ran("true", 300), self.CMD))


if __name__ == "__main__":
    unittest.main()


class VerifyLint(ForemanTestCase):
    def test_a_verify_that_cannot_run_is_refused_before_anything_is_written(self):
        # T-0765: 'python3 -m pytest' (not installed) and 'fm research nosuch' (no such action; research list exists since T-0802) were accepted; once a
        # criterion has evidence it can't be edited, so each task was dropped and recreated
        self.fm("init")
        for verify in ("python3 -m no_such_module_t0765 -q", "fm research nosuch", "cd plugin && fm tsk show T-0001"):
            r = self.fm("task", "new", "X", "--type", "FIX", "--tier", "S", "--ac", f"works :: {verify}", check=False)
            self.assertNotEqual(r.returncode, 0, verify)
            self.assertIn("can't run", r.stderr, verify)
        self.assertEqual(json.loads(self.fm("state", "--json").stdout).get("inbox") or [], [], "nothing was written")
        tid = json.loads(self.fm("task", "new", "Y", "--type", "FIX", "--tier", "S", "--json",
                                 "--ac", "works :: python3 -m json.tool --help && fm task show T-0001").stdout)["id"]
        r = self.fm("task", "ac", tid, "add", "more :: fm research nosuch", check=False)
        self.assertIn("can't run", r.stderr)
        self.assertEqual(len(c.find_brief(c.find_project(self.repo), tid).acceptance()), 1)
        self.fm("task", "ac", tid, "add", "more :: fm state")


class Redo(ForemanTestCase):
    def test_a_recreated_task_keeps_the_dropped_tasks_diff(self):
        # T-0767: T-0761 was dropped (a locked criterion named a check that couldn't run) and recreated as T-0766;
        # the new task's snapshot was taken after the edits, so finish --commit said "nothing to commit"
        from sampo import map as smap
        self.fm("init")
        new = lambda *a: json.JSONDecoder().raw_decode(self.fm(
            "task", "new", *a, "--type", "FIX", "--tier", "S", "--json", "--ac", "works :: fm state",
            "--step", "do it").stdout)[0]["id"]
        old = new("First try", "--focus")
        with open(os.path.join(self.repo, "fix.py"), "w") as f:
            f.write("x = 1\n")
        live = new("Not dropped")
        r = self.fm("task", "new", "Again", "--type", "FIX", "--tier", "S", "--redo", live, check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("dropped", r.stderr)
        self.fm("task", "drop", old, "wrong check", "--force")
        again = new("Second try", "--redo", old, "--focus")
        p = c.find_project(self.repo)
        b = c.find_brief(p, again)
        self.assertIn("fix.py", smap.changed(p.root, c.task_base(p.root, b)))
        self.assertIn(old, read_text(b.path))


class SetLists(ForemanTestCase):
    def test_list_fields_accept_brackets(self):
        # T-0810: scope=[a,b] stored "[a" and "b]" as globs
        self.fm("init")
        tid = json.loads(self.fm("task", "new", "X", "--type", "FIX", "--tier", "S", "--json").stdout)["id"]
        for value in ("[plugin/a.py, plugin/b.py]", '["plugin/a.py","plugin/b.py"]', "plugin/a.py,plugin/b.py"):
            self.fm("task", "set", tid, f"scope={value}")
            self.assertEqual(c.find_brief(c.find_project(self.repo), tid).meta["scope"], ["plugin/a.py", "plugin/b.py"])


    def test_depends_on_refuses_unknown_and_self_accepts_archived(self):
        # T-3420
        self.fm("init")
        a, b = (json.loads(self.fm("task", "new", n, "--type", "FIX", "--tier", "S", "--json").stdout)["id"]
                for n in ("A", "B"))
        r = self.fm("task", "set", a, "depends_on=T-9999", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("unknown task T-9999", r.stdout + r.stderr)
        r = self.fm("task", "set", a, f"depends_on={a}", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("a task can't depend on itself", r.stdout + r.stderr)
        p = c.find_project(self.repo)
        old = c.find_brief(p, b).path
        os.makedirs(os.path.join(p.dir, "archive"), exist_ok=True)
        os.rename(old, os.path.join(p.dir, "archive", os.path.basename(old)))
        self.fm("task", "set", a, f"depends_on={b}")
        self.assertEqual(c.find_brief(p, a).meta["depends_on"], [b])
        r = self.fm("task", "set", a, f"depends_on=[{b},T-9999]", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("unknown task T-9999", r.stdout + r.stderr)
        r = self.fm("task", "set", a, "depends_on+=T-9999", check=False)  # the T-3377 append form too
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("unknown task T-9999", r.stdout + r.stderr)
        self.fm("task", "set", a, f"depends_on={b.lower()}")
        self.assertEqual(c.find_brief(p, a).meta["depends_on"], [b])

    def test_a_verify_warning_reaches_the_last_line(self):
        # T-3813: the warning sat on stderr above 'criterion added.', and a caller reading 2>&1 | tail -1 never saw
        # it; two criteria naming tests that don't exist then reached merge trains (2026-11-08)
        self.fm("init")
        os.makedirs(os.path.join(self.repo, "plugin", "tests"), exist_ok=True)
        with open(os.path.join(self.repo, "plugin", "tests", "test_real.py"), "w") as f:
            f.write("import unittest\nclass Real(unittest.TestCase):\n    def test_it(self):\n        pass\n")
        open(os.path.join(self.repo, "plugin", "tests", "run.py"), "w").close()
        bad = "python3 plugin/tests/run.py test_missing"
        last = lambda r: r.stdout.strip().splitlines()[-1]  # noqa: E731 — the line a 2>&1 | tail -1 shows (stderr went first)
        r = self.fm("task", "new", "W", "--type", "FIX", "--tier", "S", "--ac", f"ok :: {bad}", "--step", "s",
                    "--focus")
        self.assertIn("names no test", last(r))
        tid = c.active_brief(c.load_briefs(c.find_project(self.repo))).id
        self.assertIn("names no test", last(self.fm("task", "ac", tid, "add", f"two :: {bad}")))
        self.assertIn("matches -k Nope", last(self.fm("task", "ac", tid, "edit", "1", "--verify",
                                                       "python3 plugin/tests/run.py test_real -k Nope")))
        self.assertEqual(last(self.fm("task", "ac", tid, "edit", "1", "--verify",
                                      "python3 plugin/tests/run.py test_real -k Real")), f"{tid}: criterion 1 edited.")

    def test_several_sections_in_one_call_all_land(self):
        # T-1124: only the last --section/--text pair was applied, silently
        self.fm("init")
        tid = json.loads(self.fm("task", "new", "X", "--type", "FIX", "--tier", "S", "--json").stdout)["id"]
        self.fm("task", "set", tid, "--section", "Interpretation", "--text", "first", "--section", "Non-goals",
                "--text", "second")
        b = c.find_brief(c.find_project(self.repo), tid)
        self.assertEqual((b.section("Interpretation").strip(), b.section("Non-goals").strip()), ("first", "second"))
        r = self.fm("task", "set", tid, "--section", "Interpretation", "--section", "Approach", "--text", "x", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--text", r.stdout + r.stderr)
        # T-3805: --file pairs the same way (its help said so; four pairs failed with '0 text(s)')
        one, two = os.path.join(self.repo, "one.txt"), os.path.join(self.repo, "two.txt")
        for path, text in ((one, "third"), (two, "fourth")):
            with open(path, "w") as f:
                f.write(text)
        self.fm("task", "set", tid, "--section", "Interpretation", "--file", one, "--section", "Non-goals", "--file", two)
        b = c.find_brief(c.find_project(self.repo), tid)
        self.assertEqual((b.section("Interpretation").strip(), b.section("Non-goals").strip()), ("third", "fourth"))


class BareFm(ForemanTestCase):
    # T-0917: bare fm inside a project answers "what now?"; outside one it is the help it always was
    def test_bare_fm_in_a_project_says_where_things_stand_and_what_is_next(self):
        self.fm("init")
        self.fm("capture", "Add a dark theme")
        out = self.fm().stdout
        self.assertIn("Next:", out)
        self.assertRegex(out, r"in1\b")  # the status line
        self.assertIn("sampo help", out)

    def test_bare_fm_outside_a_project_is_the_help(self):
        import tempfile
        with tempfile.TemporaryDirectory(dir=self.home) as d:
            out = self.fm(cwd=d).stdout
        self.assertIn("fm <command> -h for its options.", out)
        self.assertNotIn("Next:", out)


class Completion(ForemanTestCase):
    # T-0924: tab completion from the real parser and the live briefs
    def test_completes_verbs_subcommands_options_and_live_task_ids(self):
        self.fm("init")
        self.fm("capture", "Add a dark theme")
        words = lambda *w: self.fm("complete", "--", *w).stdout.split()  # noqa: E731
        self.assertIn("capture", words("cap"))
        self.assertNotIn("init", words("cap"))
        self.assertIn("finish", words("task", ""))
        self.assertIn("evidence", words("task", "ev"))
        self.assertEqual(words("focus", "T-"), ["T-0001"])
        self.assertIn("T-0001", words("task", "show", ""))
        self.assertIn("--type", words("task", "new", "x", "--ty"))

    def test_prints_a_hook_for_bash_and_zsh(self):
        bash = self.fm("complete", "--shell", "bash").stdout
        self.assertIn("complete -F _fm fm", bash)
        self.assertIn("sampo complete --", bash)
        self.assertIn("bashcompinit", self.fm("complete", "--shell", "zsh").stdout)
        r = subprocess.run(["bash", "-n"], input=bash, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


class InboxList(ForemanTestCase):
    # product check: fm --help lists "fm inbox — captured requests waiting", but bare fm inbox demanded a gh source
    def test_bare_fm_inbox_lists_the_captured_requests(self):
        self.fm("init")
        self.fm("capture", "Add a dark theme")
        r = self.fm("inbox")
        self.assertIn("T-0001", r.stdout)
        self.assertIn("Add a dark theme", r.stdout)
        self.assertEqual(json.loads(self.fm("inbox", "--json").stdout)["inbox"][0]["id"], "T-0001")
        self.assertIn("{gh,spec,",self.fm("inbox", "--help").stdout)  # the tracker source is still there


class AffectedPairs(ForemanTestCase):
    """T-0952: a test that historically changes with a file runs when only that file changes, so a hooks.json edit
    runs the wiring tests even though no test is named after it."""

    def test_a_co_changed_test_is_affected(self):
        from sampo import cli
        self.fm("init")
        self.fm("check", "affected", "python3 -m pytest -q {tests}")
        os.makedirs(os.path.join(self.repo, "hooks"))
        os.makedirs(os.path.join(self.repo, "tests"))
        for i in range(3):
            for rel, body in (("hooks/hooks.json", f'{{"n": {i}}}\n'), ("tests/test_wiring.py", f"N = {i}\n")):
                with open(os.path.join(self.repo, rel), "w") as f:
                    f.write(body)
            for args in (["add", "-A"], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", f"wire {i}"]):
                subprocess.run(["git", "-C", self.repo, *args], check=True)
        self.fm("task", "new", "edit the hooks", "--type", "FIX", "--tier", "S", "--ac", "a :: true", "--step", "s",
                "--focus")
        with open(os.path.join(self.repo, "hooks", "hooks.json"), "w") as f:
            f.write('{"n": 9}\n')
        _, tests, _ = cli.affected(c.find_project(self.repo, create=False))
        self.assertIn("tests/test_wiring.py", tests)

    def red_lane(self, edits):
        """Task B (checkpointed) recorded its new tests/test_red.py failing at step 1; A is active and the files in
        edits (under tests/) changed since A's focus. B's id."""
        self.fm("init")
        os.makedirs(os.path.join(self.repo, "tests"))
        # T-3417: a stand-in for tests/run.py, which cost 6-34 s per nested run here (interpreter, CPU-slot wait, a
        # process per class) while every assertion only needs "runs the named modules; a name it can't find or was told
        # to skip fails 'no test matches'" (T-1999)
        with open(os.path.join(self.repo, "tests", "run.py"), "w") as f:
            f.write("import os, sys, unittest\n"
                    "here = os.path.dirname(os.path.abspath(__file__))\n"
                    "sys.path.insert(0, here)\n"
                    "skip = os.environ.get('FOREMAN_SKIP_TESTS', '').split(',')\n"
                    "gone = [n for n in sys.argv[1:] if n in skip or not os.path.isfile(os.path.join(here, n + '.py'))]\n"
                    "if gone:\n"
                    "    sys.exit('run.py: no test matches ' + ', '.join(gone))\n"
                    "r = unittest.TextTestRunner().run(unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:]))\n"
                    "sys.exit(not r.wasSuccessful())\n")
        green = "import unittest\n\n\nclass G(unittest.TestCase):\n    def test_g(self):\n        pass\n"
        with open(os.path.join(self.repo, "tests", "test_green.py"), "w") as f:
            f.write(green)
        for args in (["add", "-A"], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "suite"]):
            subprocess.run(["git", "-C", self.repo, *args], check=True)
        self.fm("check", "affected", "python3 tests/run.py {names}")
        self.fm("task", "new", "A", "--type", "FIX", "--tier", "S", "--ac", "a :: true", "--step", "s", "--focus")
        b = self.fm_json("task", "new", "B", "--type", "FIX", "--tier", "S", "--ac", "a :: true", "--step", "s")["id"]
        self.fm("focus", b)
        with open(os.path.join(self.repo, "tests", "test_red.py"), "w") as f:
            f.write(green.replace("pass", "self.fail('red')"))
        self.fm("task", "evidence", b, "--step", "1", "--run", "python3 tests/run.py test_red", check=False)
        self.fm("checkpoint")
        self.fm("focus", "T-0001")
        for name in edits:
            with open(os.path.join(self.repo, "tests", name), "a") as f:
                f.write("# edited\n")
        return b

    def test_a_skipped_red_first_module_isnt_named_to_run_py(self):
        # T-1718: --affected said it skipped another lane's red-first test_red, then named it to run.py, which failed
        # "no test matches test_red"
        b = self.red_lane(["test_green.py", "test_red.py"])  # A's edit, and B's (its lane goes on) since A's focus
        r = self.fm("check", "--affected", check=False, timeout=300)  # T-3390/T-3404: a nested test run; T-3417: ~0.2 s now, the 300 s is a hang backstop
        self.assertIn(f"only task {b}'s red-first tests: test_red", r.stdout)
        self.assertNotIn("no test matches", r.stdout + r.stderr)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_only_the_red_first_file_itself_is_left_out(self):
        # T-1731 review: a same-stem test under another runner (web/test_red.js) was dropped by its name alone
        self.red_lane(["test_green.py", "test_red.py"])
        self.fm("check", "affected", "echo {tests}", "--for", ".js")
        os.makedirs(os.path.join(self.repo, "web"))
        with open(os.path.join(self.repo, "web", "test_red.js"), "w") as f:
            f.write("// A's own test\n")
        r = self.fm("check", "--affected", check=False, timeout=300)
        self.assertIn("echo web/test_red.js", r.stdout)
        self.assertNotIn("run.py test_green test_red", r.stdout)

    def test_an_affected_run_with_evidence_records_it(self):  # T-1747: the builder contract relies on it
        self.red_lane(["test_green.py"])
        r = self.fm("check", "--affected", "--evidence", "T-0001", "--step", "1", check=False, timeout=300)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        ev = c.find_brief(c.find_project(self.repo), "T-0001").section("Verification evidence")
        self.assertIn("affected tests only", ev)
        self.assertIn("test_green", ev)

    def test_every_linked_test_skipped_is_said_so(self):
        # T-1731 review: "No tests linked" when the one linked test was another lane's red-first file
        self.red_lane(["test_red.py"])
        r = self.fm("check", "--affected", check=False)
        self.assertIn("all 1 linked test module(s) skipped (other lanes' red-first tests)", r.stdout)
        self.assertNotIn("No tests linked", r.stdout)


class SelfRunningLine(ForemanTestCase):  # T-2364: what ran by itself, in fm state and the pane's band
    def test_fm_state_and_the_band_carry_one_self_running_line(self):
        self.fm("init")
        self.fm("task", "new", "t", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true", "--step", "s", "--focus")
        p = c.find_project(self.repo)
        c.log_event(p, "capture", task="T-0001", data={"source": "discovered", "type": "FIX", "trigger": "ci-red"})
        self.fm("ask", "T-0001", "plugin", "--later", "--why", "needs a linter")
        out = self.fm("state").stdout
        line = next((x for x in out.splitlines() if x.startswith("Self-running:")), "")
        self.assertIn("1 trigger fired today (ci-red → inbox)", line)
        self.assertIn("1 install waiting", line)
        sd = json.loads(self.fm("state", "--json").stdout)
        self.assertEqual(sd["self_running"]["installs"], 1)
        self.assertEqual(sd["self_running"]["fired"], {"ci-red": 1})
        view = json.loads(self.fm("ui", "--json").stdout)
        self.assertTrue(any(s["text"].startswith("Self-running:") for s in view["signals"]), view["signals"])


class EmptyModule(unittest.TestCase):
    """T-3727: a crash can leave one of Sampo's own modules empty on disk; bin/sampo heals it from HEAD or names the
    file and the one fix, never an AttributeError traceback."""
    def setUp(self):
        import tempfile
        from helpers import PLUGIN, git_repo
        self._tmp = tempfile.TemporaryDirectory()
        self.root = git_repo(os.path.realpath(self._tmp.name), "inst")
        self.cli = os.path.join(self.root, "plugin", "lib", "sampo", "cli.py")
        os.makedirs(os.path.dirname(self.cli))
        os.makedirs(os.path.join(self.root, "plugin", "bin"))
        shutil.copy(os.path.join(PLUGIN, "lib", "sampo", "py.py"), os.path.dirname(self.cli))
        shutil.copy(os.path.join(PLUGIN, "bin", "sampo"), os.path.join(self.root, "plugin", "bin"))
        with open(self.cli, "w") as f:
            f.write("def main():\n    print('ran')\n    return 0\n")
        open(os.path.join(os.path.dirname(self.cli), "__init__.py"), "w").close()
        subprocess.run(["git", "-C", self.root, "add", "-A"], check=True)
        subprocess.run(["git", "-C", self.root, "commit", "-qm", "install"], check=True)
        self.bin = os.path.join(self.root, "plugin", "bin", "sampo")

    def tearDown(self):
        self._tmp.cleanup()

    def run_bin(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "FOREMAN_", "SAMPO_"))}
        return subprocess.run([sys.executable, self.bin], capture_output=True, text=True, env=env, timeout=60)

    def test_an_empty_module_is_restored_and_named(self):
        open(self.cli, "w").close()
        r = self.run_bin()
        self.assertEqual((r.returncode, r.stdout), (0, "ran\n"), r.stderr)
        self.assertIn("plugin/lib/sampo/cli.py", r.stderr)
        self.assertIn("restored", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_a_module_empty_in_head_too_is_named_with_the_one_fix(self):
        with open(self.cli, "w"):
            pass
        subprocess.run(["git", "-C", self.root, "add", "-A"], check=True)
        subprocess.run(["git", "-C", self.root, "commit", "-qm", "empty"], check=True)
        r = self.run_bin()
        self.assertEqual(r.returncode, 1)
        self.assertIn("plugin/lib/sampo/cli.py is empty", r.stderr)
        self.assertIn("git -C", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
