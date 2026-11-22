"""T-5680: a Workflow script's agent() stage names an agentType (a sampo:fm-* type, which carries a maxTurns) or a model
(haiku or sonnet). A stage with neither inherits the session's Opus with no turn cap, so the PreToolUse hook refuses the
Workflow call and names the fix. The check reads the script inline, from a file (scriptPath), or from a shipped workflow
(name); comments and strings are not calls; a stage whose options are not an object literal is refused (fail closed)."""
import contextlib
import glob
import io
import json
import os
import re
import tempfile
import unittest
from unittest import mock

from helpers import ForemanTestCase  # noqa: F401  (puts plugin/lib on sys.path)

from sampo import budget, hooks

PLUGIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
SHIPPED = os.path.join(PLUGIN, "workflows")

UNTYPED = """const r = await agent('look at the code', { label: 'lens:intent', phase: 'Review', schema: S })
return r
"""


def probs(script):
    return budget.workflow_problems({"script": script})


class StageCheck(unittest.TestCase):
    def test_an_untyped_opus_stage_is_refused_by_its_label(self):
        found = probs(UNTYPED)
        self.assertEqual([label for label, _ in found], ["lens:intent"])
        self.assertIn("sampo:fm-", found[0][1])  # the fix names a type
        self.assertIn("haiku", found[0][1])  # and a model

    def test_a_stage_with_no_options_is_refused_by_its_line(self):
        found = probs("const a = 1\nawait agent('just a prompt')\n")
        self.assertEqual([label for label, _ in found], ["line 2"])

    def test_an_sampo_fm_type_passes_and_the_old_foreman_name_too(self):
        self.assertEqual(probs("await agent('p', { agentType: 'sampo:fm-scout' })"), [])
        self.assertEqual(probs("await agent('p', { label: 'x', agentType: 'foreman:fm-reviewer', schema: S })"), [])

    def test_a_builtin_or_general_type_with_no_model_is_refused(self):
        for t in ("Explore", "general-purpose", "Plan"):
            with self.subTest(t=t):
                self.assertEqual(len(probs(f"await agent('p', {{ agentType: '{t}', label: 'L' }})")), 1)

    def test_a_haiku_or_sonnet_model_passes_without_a_type(self):
        self.assertEqual(probs("await agent('p', { model: 'haiku' })"), [])
        self.assertEqual(probs("await agent('p', { label: 'm', model: 'sonnet', effort: 'medium' })"), [])

    def test_another_model_is_refused_even_with_a_builtin_type(self):
        found = probs("await agent('p', { agentType: 'Explore', model: 'opus', label: 'big' })")
        self.assertEqual([label for label, _ in found], ["big"])

    def test_a_spread_of_unknown_options_is_refused_even_beside_a_type(self):
        self.assertEqual(len(probs("await agent(p, { ...opts, label: 'x' })")), 1)
        self.assertEqual(len(probs("await agent(p, { agentType: 'sampo:fm-x', ...opts })")), 1)
        self.assertEqual(len(probs("await agent(p, { ...opts, agentType: 'sampo:fm-reviewer' })")), 1)
        self.assertEqual(len(probs("const reviewer = (p, opts) => agent(p, { ...opts, agentType: 'sampo:fm-x' })")), 1)

    def test_a_spread_of_an_object_literal_is_read_through(self):
        self.assertEqual(probs("await agent(p, { ...{ agentType: 'sampo:fm-x' } })"), [])
        found = probs("await agent(p, { ...{ label: 'lit' }, agentType: 'Explore' })")
        self.assertEqual([label for label, _ in found], ["lit"])

    def test_a_dynamic_type_counts_only_with_an_fm_literal_in_it(self):
        self.assertEqual(len(probs("await agent(p, { agentType: REVIEWERS[0], label: 'a' })")), 1)
        self.assertEqual(probs("await agent(p, { agentType: pick ? 'sampo:fm-a' : 'sampo:fm-b', label: 'a' })"), [])
        self.assertEqual(len(probs("await agent(p, makeOpts())")), 1)
        self.assertEqual(len(probs("await agent(p, opts)")), 1)
        self.assertEqual(len(probs("await agent(p)")), 1)

    def test_a_regex_literal_holding_a_quote_backtick_or_slash_does_not_open_a_string(self):
        after = "await agent('p', { label: 'after' })\n"
        for regex in ("/`/g", "/[/*]/g", "/'/g", "/\"/g", "/\\/\\*/"):
            with self.subTest(regex=regex):
                self.assertEqual([label for label, _ in probs(f"const re = {regex}\n" + after)], ["after"])
        self.assertEqual([label for label, _ in probs("return /x/.test(s) ? 1 : 0\n" + after)], ["after"])
        self.assertEqual([label for label, _ in probs("const h = (a + b) / 2 / c\n" + after)], ["after"])

    def test_an_open_string_template_or_comment_at_the_end_is_refused(self):
        typed = "await agent('p', { model: 'sonnet' })\n"
        for tail in ("const s = 'open", "const t = `abc ${", "/* open"):
            with self.subTest(tail=tail):
                found = probs(typed + tail)
                self.assertEqual(len(found), 1)
                self.assertEqual(found[0][0], "script")

    def test_a_label_is_echoed_in_printable_ascii(self):
        found = probs("await agent('p', { label: 'a\x1b[31mb\x07c' })")
        self.assertEqual(len(found), 1)
        self.assertTrue(all(" " <= c <= "~" for c in found[0][0]), repr(found[0][0]))

    def test_a_model_name_matches_haiku_or_sonnet_as_a_substring(self):
        self.assertEqual(probs("await agent('p', { model: 'claude-sonnet-4-5' })"), [])
        self.assertEqual(probs("await agent('p', { model: 'Haiku' })"), [])
        self.assertEqual(len(probs("await agent('p', { model: 'claude-opus-4' })")), 1)

    def test_a_dynamic_model_alone_is_refused(self):
        self.assertEqual(len(probs("await agent(p, { model: MODEL })")), 1)

    def test_an_escaped_identifier_in_code_is_refused_not_read_as_a_name(self):
        # agent( is agent( to JavaScript: a backslash in code can only be an identifier escape
        found = probs("await \\u0061gent('p', { model: 'sonnet' })\n")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0][0], "script")

    def test_comments_and_strings_are_not_calls(self):
        script = ("// agent() matches a type exactly, so the old name is the fallback\n"
                  "/* await agent('x') */\n"
                  "const doc = 'call agent(prompt) with a type'\n"
                  "const t = \"agent(x)\"\n"
                  "await agent('p', { model: 'sonnet' })\n")
        self.assertEqual(probs(script), [])

    def test_a_call_inside_a_template_expression_is_checked(self):
        found = probs("const s = `lens ${ await agent('p', { label: 'inner' }) }`\n")
        self.assertEqual([label for label, _ in found], ["inner"])

    def test_nested_braces_in_the_options_do_not_hide_the_model(self):
        script = "await agent('p', { label: 'n', schema: { type: 'object', properties: { x: {} } }, model: 'haiku' })"
        self.assertEqual(probs(script), [])

    def test_a_label_that_is_not_a_literal_falls_back_to_the_line(self):
        found = probs("await agent('p', { label: `lens:${x}` })")
        self.assertEqual([label for label, _ in found], ["line 1"])

    def test_a_long_label_is_shortened_and_kept_on_one_line(self):
        found = probs("await agent('p', { label: '" + "x" * 200 + "\\n' })")
        self.assertEqual(len(found), 1)
        self.assertLessEqual(len(found[0][0]), 60)
        self.assertNotIn("\n", found[0][0])

    def test_no_script_names_no_problem(self):
        self.assertEqual(budget.workflow_problems({}), [])
        self.assertEqual(budget.workflow_problems({"script": ""}), [])
        self.assertEqual(budget.workflow_problems({"script": 42}), [])

    def test_a_shipped_name_is_read_from_the_plugin_and_checked(self):
        self.assertEqual(budget.workflow_problems({"name": "review"}), [])
        self.assertEqual(budget.workflow_problems({"name": "ideas"}), [])

    def test_an_unknown_or_unsafe_name_is_refused_and_nothing_outside_is_read(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"HOME": d}):
            self.assertEqual(len(budget.workflow_problems({"name": "no-such-workflow"}, d)), 1)
            self.assertEqual(len(budget.workflow_problems({"name": "../../../etc/passwd"}, d)), 1)

    def test_a_name_in_the_project_or_the_home_workflows_is_checked(self):
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as home, \
                mock.patch.dict(os.environ, {"HOME": home}):
            for base in (os.path.join(project, ".claude", "workflows"), os.path.join(home, ".claude", "workflows")):
                os.makedirs(base)
            with open(os.path.join(project, ".claude", "workflows", "mine.js"), "w", encoding="utf-8") as f:
                f.write(UNTYPED)
            with open(os.path.join(home, ".claude", "workflows", "own.js"), "w", encoding="utf-8") as f:
                f.write("await agent('p', { model: 'haiku' })\n")
            self.assertEqual([label for label, _ in budget.workflow_problems({"name": "mine"}, project)], ["lens:intent"])
            self.assertEqual(budget.workflow_problems({"name": "own"}, project), [])

    def test_a_script_path_is_read_and_checked(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ad-hoc.js")
            with open(path, "w", encoding="utf-8") as f:
                f.write(UNTYPED)
            self.assertEqual([label for label, _ in budget.workflow_problems({"scriptPath": path})], ["lens:intent"])

    def test_a_relative_script_path_is_resolved_against_the_payload_cwd(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "wf"))
            with open(os.path.join(d, "wf", "ad.js"), "w", encoding="utf-8") as f:
                f.write(UNTYPED)
            self.assertEqual([label for label, _ in budget.workflow_problems({"scriptPath": "wf/ad.js"}, d)],
                             ["lens:intent"])

    def test_a_tilde_script_path_is_expanded_to_the_home_directory(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"HOME": home}):
            with open(os.path.join(home, "ad.js"), "w", encoding="utf-8") as f:
                f.write(UNTYPED)
            self.assertEqual([label for label, _ in budget.workflow_problems({"scriptPath": "~/ad.js"}, "/")],
                             ["lens:intent"])

    def test_an_unreadable_script_path_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            found = budget.workflow_problems({"scriptPath": os.path.join(d, "missing.js")})
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0][0], "script")

    def test_an_inline_script_over_the_cap_is_refused(self):
        found = probs("// filler\n" * (budget.WORKFLOW_MAX // 10 + 10))
        self.assertEqual(len(found), 1)
        self.assertIn("too large", found[0][1])

    def test_an_oversize_script_path_is_refused_not_read_whole(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "big.js")
            with open(path, "w", encoding="utf-8") as f:
                f.write("// filler\n" * (budget.WORKFLOW_MAX // 8 + 10))
            found = budget.workflow_problems({"scriptPath": path})
            self.assertEqual(len(found), 1)
            self.assertIn("too large", found[0][1])


class ShippedScripts(unittest.TestCase):
    def test_every_shipped_script_passes(self):
        paths = sorted(glob.glob(os.path.join(SHIPPED, "*.js")))
        self.assertTrue(paths)
        for path in paths:
            with self.subTest(path=os.path.basename(path)), open(path, encoding="utf-8") as f:
                self.assertEqual(probs(f.read()), [])

    def test_the_shipped_scripts_name_no_builtin_type_without_a_model(self):
        for name in ("review.js", "ideas.js"):
            with self.subTest(name=name), open(os.path.join(SHIPPED, name), encoding="utf-8") as f:
                text = f.read()
            self.assertNotRegex(text, r"agentType: '(?!sampo:fm-|foreman:fm-)")


class AgentTypes(unittest.TestCase):
    def test_every_fm_agent_type_has_a_turn_cap(self):
        paths = sorted(glob.glob(os.path.join(PLUGIN, "agents", "fm-*.md")))
        self.assertGreaterEqual(len(paths), 6)
        for path in paths:
            with self.subTest(path=os.path.basename(path)), open(path, encoding="utf-8") as f:
                self.assertRegex(f.read(), r"(?m)^maxTurns: [1-9][0-9]*$")


class Hooks(unittest.TestCase):
    def pre(self, tool_input, tool="Workflow", cwd=None):
        payload = {"tool_name": tool, "tool_input": tool_input}
        if cwd is not None:
            payload["cwd"] = cwd
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = hooks._pre_tool_use(json.dumps(payload))
        return code, out.getvalue()

    def test_a_relative_script_path_is_checked_against_the_payload_cwd(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "wf"))
            with open(os.path.join(d, "wf", "ad.js"), "w", encoding="utf-8") as f:
                f.write(UNTYPED)
            code, out = self.pre({"scriptPath": "wf/ad.js"}, cwd=d)
        self.assertEqual(code, 0)
        self.assertIn("lens:intent", json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"])

    def test_a_missing_script_path_is_denied(self):
        with tempfile.TemporaryDirectory() as d:
            code, out = self.pre({"scriptPath": "nope.js"}, cwd=d)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_the_matcher_routes_workflow_to_the_guard(self):
        with open(os.path.join(PLUGIN, "hooks", "hooks.json"), encoding="utf-8") as f:
            cfg = json.load(f)
        pre = [h for h in cfg["hooks"]["PreToolUse"] if "fast" in json.dumps(h)]
        self.assertTrue(pre)
        self.assertIn("Workflow", pre[0]["matcher"].split("|"))

    def test_an_untyped_stage_is_denied_with_the_fix_named(self):
        code, out = self.pre({"script": UNTYPED})
        self.assertEqual(code, 0)
        deny = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(deny["permissionDecision"], "deny")
        self.assertEqual(deny["hookEventName"], "PreToolUse")
        self.assertIn("lens:intent", deny["permissionDecisionReason"])
        self.assertIn("sampo:fm-", deny["permissionDecisionReason"])

    def test_a_typed_workflow_passes_silently(self):
        self.assertEqual(self.pre({"script": "await agent('p', { agentType: 'sampo:fm-scout' })"}), (0, ""))
        self.assertEqual(self.pre({"name": "review"}), (0, ""))

    def test_a_broken_check_fails_closed(self):
        with mock.patch.object(budget, "workflow_problems", side_effect=RuntimeError("boom")), \
                mock.patch.object(hooks, "log_error"):
            code, out = self.pre({"script": "await agent('p')"})
        self.assertEqual(code, 0)
        reason = json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("refused until", reason)

    def test_a_workflow_call_with_no_input_is_allowed(self):
        self.assertEqual(self.pre(None), (0, ""))
        self.assertEqual(self.pre("text"), (0, ""))


class AgentFiles(unittest.TestCase):
    def test_doctor_still_accepts_every_agent_file(self):
        from sampo import doctor
        r = doctor.check_frontmatter(os.path.normpath(PLUGIN))
        self.assertEqual(r.status, "PASS", r.detail if hasattr(r, "detail") else r)

    def test_the_builder_turn_cap_covers_the_observed_maximum(self):
        # T-5680 review: builder turns reached 315 in the automation data (p90 161), so 150 cut real builds short
        with open(os.path.join(PLUGIN, "agents", "fm-builder.md"), encoding="utf-8") as f:
            m = re.search(r"(?m)^maxTurns: ([1-9][0-9]*)$", f.read())
        self.assertIsNotNone(m)
        self.assertGreaterEqual(int(m.group(1)), 320)
