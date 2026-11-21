"""T-3855: the category skills (skill-writing, problem-solving, coverage) load at the moment they fit, once a session:
on the prompt's own words, at the plan stage of an M or L task, and when a step's second evidence run fails."""
import json
import os

import test_hooks  # the module, not its classes: a class imported by name would run twice
from helpers import ForemanTestCase, read_json

from sampo import core as c

PS = "Skill sampo:problem-solving"
COV = "Skill sampo:coverage"
SW = "Skill sampo:skill-writing"
ALL = (PS, COV, SW)


class Routing(ForemanTestCase):
    def test_the_think_table_is_sound_and_every_hint_names_a_skill_the_model_may_load(self):
        r = read_json(os.path.join(c.PLUGIN_ROOT, "skills", "routing.json"))
        self.assertEqual(set(r["think_words"]), {"problem-solving", "coverage", "skill-writing"})
        self.assertIsNone(c.routing_problem(r))
        self.assertIn("think_words must", c.routing_problem(dict(r, think_words={"coverage": "all"})))
        self.assertIn("think_words must", c.routing_problem(dict(r, think_words=["why is"])))
        self.assertEqual(set(c.THINK_FOR), set(r["think_words"]))
        for key, hint in c.THINK_FOR.items():
            name = hint.split("sampo:")[1]
            self.assertEqual(name, key)
            with open(os.path.join(c.PLUGIN_ROOT, "skills", name, "SKILL.md"), encoding="utf-8") as f:
                self.assertNotIn("\ndisable-model-invocation: true", f.read())

    def test_words_pick_the_skill_and_near_misses_pick_none(self):
        for text, want in (("why does the cache miss on every second call?", "problem-solving"),
                           ("Why won't the login redirect work", "problem-solving"),
                           ("no idea why this keeps failing", "problem-solving"),
                           ("bisect it, it passed on Monday", "problem-solving"),
                           ("how come the export drops a column", "problem-solving"),
                           ("make it comprehensive: cover the whole space", "coverage"),
                           ("handle every edge case, nothing missed", "coverage"),
                           ("add a ton of benchmarks", "coverage"),
                           ("make it fully featured", "coverage"),
                           ("what did I leave unasked? list the unasked needs", "coverage"),
                           ("write a skill for release notes", "skill-writing"),
                           ("review the skill's description and SKILL.md", "skill-writing"),
                           ("create a skill that checks changelogs", "skill-writing")):
            with self.subTest(text=text):
                self.assertEqual(c.think_request(text), want)
        for text in ("", "fix the build", "add a --verbose flag", "that is why we kept the old name",
                     "why not add a flag?", "learn a new skill this year", "her skills are strong",
                     "the whole thing is slow", "a lot more readable", "everything is fine", "a cover letter",
                     "the test coverage report is green", "no big deal", "explain the bisection of the tree in the docs",
                     "add a test for the empty edge case", "write a comprehensive changelog entry",
                     "the hypothesis section of the paper is long", "I saw the same error message in the logs",
                     "change the skill level setting to expert"):
            with self.subTest(text=text):
                self.assertEqual(c.think_request(text), "", text)
        self.assertEqual(c.think_request(None), "")
        self.assertEqual(c.step_fails(None), 0)


class Hooks(test_hooks.HookCase):
    def ups(self, prompt, sid="s1", **kw):
        return self.ctx_of(self.hook("UserPromptSubmit", dict({"prompt": prompt, "session_id": sid}, **kw)))

    def fail(self, tid, step=1, cmd="false"):
        """One failed fm evidence run on a step: the ledger line the CLI writes, then the hook the failure fires."""
        self.fm("task", "evidence", tid, "--step", str(step), "--run", cmd, check=False)

    def failure_note(self, tid, sid="s1", **kw):
        return self.ctx_of(self.hook("PostToolUseFailure", dict(
            {"tool_name": "Bash", "tool_input": {"command": f'sampo task evidence {tid} --step 1 --run "false"'},
             "error": "Exit code 1", "session_id": sid}, **kw)))

    def test_prompt_words_load_the_fitting_skill_once_a_session(self):
        self.fm("init")
        for prompt, skill in (("why does the cache miss on every second call?", PS),
                              ("make it comprehensive, cover every edge case", COV),
                              ("write a skill for release notes", SW)):
            with self.subTest(prompt=prompt):
                sid = "w-" + skill.split(":")[1]
                ctx = self.ups(prompt, sid)
                self.assertIn(skill, ctx)
                self.assertLessEqual(len(ctx), 400)
                self.assertNotIn(skill, self.ups(prompt, sid), "said once a session")
                self.assertIn(skill, self.ups(prompt, sid + "-new"), "a new session hears it again")
                for other in ALL:
                    if other != skill:
                        self.assertNotIn(other, ctx)

    def test_near_miss_words_load_nothing(self):
        self.fm("init")
        for i, prompt in enumerate(("fix the build", "that is why we kept the old name", "why not add a flag?",
                                    "learn a new skill this year", "a lot more readable", "the test coverage is green")):
            ctx = self.ups(prompt, f"nm-{i}")
            for skill in ALL:
                self.assertNotIn(skill, ctx, prompt)

    def test_a_note_that_does_not_fit_waits_rather_than_cutting_the_next_line(self):
        self.fm("init")
        self.task(title="x" * 150, steps=("s" * 150,))
        ctx = self.ups("why does it fail", "room")
        self.assertLessEqual(len(ctx), 400)
        self.assertIn("Next: ", ctx)
        self.assertIn('--run "<verify cmd>"', ctx, "the Next line stays whole")

    def plan_needed(self, tier):
        tid = json.loads(self.fm("task", "new", f"Plan a {tier} thing", "--type", "FEATURE", "--tier", tier,
                                 "--json").stdout)["id"]
        return tid

    def test_the_plan_stage_of_an_m_or_l_task_loads_coverage_once_and_s_does_not(self):
        self.fm("init")
        tid = self.plan_needed("M")
        nxt = json.loads(self.fm("next", "--json").stdout)
        self.assertEqual(nxt["stage"], "planning", nxt)
        self.assertEqual(nxt["skill"], c.THINK_FOR["coverage"])
        ctx = self.ups("carry on", "p1")
        self.assertIn(COV, ctx)
        self.assertNotIn(COV, self.ups("carry on again", "p1"), "once a session")
        self.assertIn(COV, self.ups("carry on", "p2"), "a new session")
        self.fm("task", "drop", tid, "not needed", "--force")
        self.plan_needed("S")
        nxt = json.loads(self.fm("next", "--json").stdout)
        self.assertEqual(nxt["stage"], "planning", nxt)
        self.assertFalse(nxt["skill"], "a small task needs no space map")
        self.assertNotIn(COV, self.ups("carry on", "p3"))

    def test_a_steps_second_failed_run_loads_problem_solving_once_and_the_first_loads_nothing(self):
        self.fm("init")
        tid = self.task(steps=("reproduce", "fix"))
        self.fail(tid)
        self.assertNotIn(PS, self.failure_note(tid), "one failed run is a try, not a stall")
        self.assertNotIn(PS, json.loads(self.fm("next", "--json").stdout)["skill"] or "")
        self.fail(tid)
        note = self.failure_note(tid)
        self.assertIn(PS, note)
        self.assertLessEqual(len(note), 300)
        self.assertEqual(json.loads(self.fm("next", "--json").stdout)["skill"], c.THINK_FOR["problem-solving"])
        self.fail(tid)
        self.assertNotIn(PS, self.failure_note(tid), "once a session")
        self.assertIn(PS, self.failure_note(tid, sid="s2"), "counts from the ledger: a new session still hears it")
        self.assertNotIn(PS, self.failure_note(tid, sid="s3", agent_id="a1"), "a subagent's failure is its own")

    def test_failures_on_another_step_or_a_pass_since_do_not_add_up(self):
        self.fm("init")
        tid = self.task(steps=("reproduce", "fix"))
        self.fail(tid)
        self.fm("task", "evidence", tid, "--step", "1", "--run", "true")  # a pass resets the step's count
        self.fail(tid)
        self.assertNotIn(PS, self.failure_note(tid, sid="reset"))
        self.assertEqual(c.step_fails(c.find_brief(self.project(), tid)), 1)

    def test_the_next_line_names_problem_solving_when_the_second_failure_is_already_in_the_ledger(self):
        self.fm("init")
        tid = self.task(steps=("reproduce", "fix"))
        self.fail(tid)
        self.fail(tid)
        ctx = self.ups("carry on", "next-line")
        self.assertIn(PS, ctx)
        self.assertLessEqual(len(ctx), 400)
        self.assertNotIn(PS, self.ups("carry on", "next-line"), "once a session")
