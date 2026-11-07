"""fm second (T-0276): one primitive for an independent second read — a plan on another model (T-0233), a review's
findings rebutted (T-0232), the last session read for what was missed (T-0235), installed review skills per lens
(T-0230)."""
import json
import os
import re
import unittest

from helpers import ForemanTestCase

STUB = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
stdin = sys.stdin.read()
with open(os.environ["STUB_LOG"], "a") as f:
    f.write(json.dumps({"args": args, "stdin": stdin}) + "\n")
system = args[args.index("--append-system-prompt") + 1] if "--append-system-prompt" in args else ""
if "plan" in system.lower():
    text = ("## Objections\n- HIGH: the cache has no invalidation step — stale tokens survive logout\n"
            "- LOW: step 3 duplicates step 2\nVerdict: revise — add invalidation before building")
else:
    text = os.environ.get("STUB_MISSED") or "## Missed\n- export the report as CSV — asked twice, no task or answer followed\n"
print(json.dumps({"result": text, "total_cost_usd": 0.01}))
'''


class _Stubbed(ForemanTestCase):
    def setUp(self):
        super().setUp()
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir)
        with open(os.path.join(bindir, "claude"), "w") as f:
            f.write(STUB)
        os.chmod(os.path.join(bindir, "claude"), 0o755)
        self.log = os.path.join(self.tmp, "stub.log")
        self.cc = os.path.join(self.tmp, "cc")
        self.env = {"PATH": bindir + os.pathsep + os.environ["PATH"], "STUB_LOG": self.log, "CLAUDE_CONFIG_DIR": self.cc}
        self.fm("init")

    def calls(self):
        with open(self.log) as f:
            return [json.loads(x) for x in f]


class Plan(_Stubbed):
    def test_a_rival_model_reviews_the_plan(self):
        tid = json.loads(self.fm("task", "new", "Cache the session token", "--type", "FEATURE", "--tier", "L",
                                 "--ac", "tokens are cached :: true", "--step", "add the cache", "--interpretation",
                                 "keep tokens in memory", "--approach", "1) dict 2) redis 3) file Choice 1 (80%): dict", "--json").stdout)["id"]
        out = self.fm("second", "plan", tid, env=self.env).stdout
        self.assertIn("Verdict: revise", out)
        brief = self.fm("task", "show", tid).stdout
        self.assertIn("## Plan review", brief)
        self.assertIn("no invalidation step", brief)
        call = self.calls()[0]
        self.assertIn("2) redis", call["stdin"])
        self.assertEqual(call["args"][call["args"].index("--model") + 1], "sonnet")  # not the main model
        self.assertIn("--tools", call["args"])


RIVAL_STUB = r'''#!/usr/bin/env python3
import json, os, sys
stdin = sys.stdin.read()
with open(os.environ["STUB_LOG"], "a") as f:
    f.write(json.dumps({"args": sys.argv[1:], "stdin": stdin}) + "\n")
with open(os.environ["STUB_REPLY"]) as f:
    print(json.dumps({"result": f.read(), "total_cost_usd": 0.01}))
'''
REPLY = """## Readings
- expire every cached token on logout in session_cache.py, not only cache it
- keep tokens in memory in a dict in token_cache.py
- add tests for all the edge cases and error handling
## Approaches
- write tokens through to sqlite in store_db.py so they survive a restart
## Failure modes
- TokenCache.get hands back a token after logout
"""


class _RivalBase(_Stubbed):  # T-2341 (Mission 5, #2): a cold read of the request, before the plan is approved
    def setUp(self):
        super().setUp()
        with open(os.path.join(self.tmp, "bin", "claude"), "w") as f:
            f.write(RIVAL_STUB)
        self.reply = os.path.join(self.tmp, "reply.md")
        self.say(REPLY)
        self.env.update(STUB_REPLY=self.reply, FOREMAN_NO_RIVAL="")  # the suite's default turns the gate off

    def say(self, text):
        with open(self.reply, "w") as f:
            f.write(text)

    SURE = ("1) keep a dict in memory; fails if: a restart loses it 2) use what exists: lru_cache from the stdlib; "
            "fails if: it has no time limit 3) do less: skip the cache; fails if: logins stay slow Choice 1 (%s%%): dict")  # T-2335's gate

    def new(self, tier="L", approach=None, step="add the cache", raw="cache the session token so logins are fast",
            interp="Reading 1: keep tokens in memory in a dict in token_cache.py. Reading 2: share them through redis. "
                   "Reading 1."):
        approach = approach or self.SURE % 90
        args = ["task", "new", "Cache the session token", "--type", "FEATURE", "--tier", tier, "--ac",
                "cache the session token so logins are fast :: true", "--step", step, "--raw", raw, "--json"]
        if tier != "S":
            args += ["--interpretation", interp, "--approach", approach]
        tid = json.loads(self.fm(*args, env=self.env).stdout)["id"]
        if tier == "L":
            self.fm("task", "set", tid, "--section", "Risks and rollback", "--text", "stale tokens; drop the cache")
            self.fm("task", "set", tid, "--section", "Build vs reuse", "--text", "nothing in the repo caches tokens")
            self.fm("task", "set", tid, "approved=true")  # an L plan waits for approval at standard autonomy
        return tid

    def focus(self, tid, ok):
        p = self.fm("focus", tid, env=self.env, check=False)
        self.assertEqual(p.returncode == 0, ok, p.stdout + p.stderr)
        return p.stdout + p.stderr


class Rival(_RivalBase):
    def test_the_child_sees_only_the_request_and_the_map(self):
        tid = self.new()
        out = self.fm("second", "rival", tid, env=self.env).stdout
        call = self.calls()[0]
        self.assertIn("cache the session token", call["stdin"])
        self.assertIn("Project map", call["stdin"])
        for plan_only in ("2) redis", "Reading 2", "dict in token_cache.py", "tokens are cached", "add the cache"):
            self.assertNotIn(plan_only, call["stdin"])
        self.assertIn("--tools", call["args"])
        self.assertIn("Rival plan", out)
        self.assertIn("## Rival plan", self.fm("task", "show", tid).stdout)

    def test_a_rival_only_item_that_names_a_file_becomes_dissent_and_generic_ones_drop(self):
        tid = self.new()
        self.fm("second", "rival", tid, env=self.env)
        brief = self.fm("task", "show", tid).stdout
        self.assertIn("- [ ] rival reading: expire every cached token on logout in session_cache.py", brief)
        self.assertIn("- [ ] rival approach: write tokens through to sqlite in store_db.py", brief)
        self.assertNotIn("- [ ] rival reading: keep tokens in memory", brief)  # the plan has it
        self.assertNotIn("- [ ] rival reading: add tests", brief)  # generic: dropped
        self.assertIn("add tests for all the edge cases", brief.split("## Rival plan")[1].split("\n## ")[0])  # but kept in the section
        self.assertNotIn("TokenCache.get hands back", brief.split("## Dissent")[1].split("\n## ")[0])

    def test_focus_waits_for_an_unstarted_L_plan_until_the_rival_is_read_and_answered(self):
        tid = self.new()
        said = self.focus(tid, False)
        self.assertIn(f"sampo second rival {tid}", said)
        self.assertIn(f"sampo second rival {tid}", self.fm("next", env=self.env).stdout)
        self.fm("second", "rival", tid, env=self.env)
        self.assertIn("open", self.focus(tid, False).lower())  # the rival-only lines are still open
        for n in (2, 1):
            self.fm("task", "dissent", tid, "resolve", str(n), "adopted: noted in the plan", env=self.env)
        self.focus(tid, True)

    def test_a_changed_mind_reads_the_last_choice_percent(self):  # T-2427: the one parser the ledger and plan gate use
        changed = self.new("M", approach=self.SURE % 60 + " then, on a second look, Choice 2 (90%): lru_cache")
        self.focus(changed, True)

    def test_M_under_the_race_bar_or_with_one_reading_waits_and_others_never_do(self):
        low = self.new("M", approach=self.SURE % 60)
        self.assertIn(f"sampo second rival {low}", self.focus(low, False))
        self.fm("task", "drop", low, "not needed")
        one = self.new("M", interp="There is one honest reading: keep tokens in memory. Ruled out: sharing them through "
                                   "redis, because nothing else reads them.")
        self.assertIn(f"sampo second rival {one}", self.focus(one, False))
        self.fm("task", "drop", one, "not needed")
        sure = self.new("M")  # 90%, two readings
        self.focus(sure, True)
        self.assertFalse(os.path.exists(self.log))  # no child ran

    def test_no_other_honest_reading_is_the_ruled_out_form_and_needs_no_rival(self):
        tid = self.new("M", interp="Reading 1: keep tokens in memory. There is no other honest reading, because logins "
                                   "only ever need this process's own tokens.")
        self.focus(tid, True)

    def test_a_one_way_step_needs_it_even_on_an_S_brief(self):
        tid = self.new("S", step="publish the release notes")
        self.assertIn(f"sampo second rival {tid}", self.focus(tid, False))
        self.fm("task", "drop", tid, "not needed", "--force")
        self.focus(self.new("S", step="rename the cache key"), True)

    def test_an_S_push_or_a_verb_the_request_names_is_routine_and_waits_for_nothing(self):
        for kw in (dict(step="push the branch"), dict(step="send the report"),
                   dict(step="publish the release notes", raw="publish the release notes")):
            tid = self.new("S", **kw)
            self.focus(tid, True)
            self.fm("task", "drop", tid, "next", "--force")
        self.assertFalse(os.path.exists(self.log))

    def test_a_hand_typed_section_or_an_empty_reply_does_not_clear_the_gate(self):
        tid = self.new()
        self.fm("task", "set", tid, "--section", "Rival plan", "--text", "nothing to see")
        self.assertIn(f"sampo second rival {tid}", self.focus(tid, False))
        self.say("no bullets here\n")
        p = self.fm("second", "rival", tid, env=self.env)
        self.assertIn("no rival read", p.stdout)
        self.assertIn(f"sampo second rival {tid}", self.focus(tid, False))

    def test_each_list_keeps_four_items(self):
        self.say("## Readings\n" + "".join(f"- read file_{i}.py aloud\n" for i in range(7)))
        tid = self.new()
        self.fm("second", "rival", tid, env=self.env)
        sec = self.fm("task", "show", tid).stdout.split("## Rival plan")[1].split("\n## ")[0]
        self.assertEqual(sec.count("file_"), 4)

    def test_gate_on_an_ordinary_S_M_and_L_plan_reach_ready_after_the_rival_read(self):
        for tier, kw in (("S", dict(step="deploy the cache")), ("M", dict(approach=self.SURE % 60)), ("L", {})):
            tid = self.new(tier, **kw)
            self.assertIn(f"sampo second rival {tid}", self.fm("next", env=self.env).stdout)
            self.fm("second", "rival", tid, env=self.env)
            n = len(re.findall(r"- \[ \] rival", self.fm("task", "show", tid).stdout))
            for i in range(n, 0, -1):
                self.fm("task", "dissent", tid, "resolve", str(i), "adopted: noted", env=self.env)
            self.assertNotIn("second rival", self.fm("next", env=self.env).stdout)
            self.focus(tid, True)
            self.fm("task", "drop", tid, "next", "--force")

    def test_answering_every_rival_line_unlocks_focus(self):
        tid = self.new("M", approach=self.SURE % 60)
        self.fm("second", "rival", tid, env=self.env)
        self.fm("task", "dissent", tid, "resolve", "1", "rejected: out of scope", env=self.env)
        self.fm("task", "dissent", tid, "resolve", "2", "rejected: out of scope", env=self.env)
        self.focus(tid, True)

    def test_a_failed_child_is_noted_and_does_not_block(self):
        tid = self.new()
        p = self.fm("second", "rival", tid, env=dict(self.env, PATH="/nonexistent", HOME=self.tmp), check=False)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("no rival read", self.fm("task", "show", tid).stdout)
        self.focus(tid, True)

    def test_a_sensitive_project_sends_nothing(self):
        tid = self.new()
        self.fm("sensitive", "on")
        self.fm("second", "rival", tid, env=self.env)
        self.assertFalse(os.path.exists(self.log))
        self.focus(tid, True)


class RivalSplit(_RivalBase):
    def test_a_top_reading_the_plan_did_not_choose_marks_the_brief_split(self):
        tid = self.new()
        self.fm("second", "rival", tid, env=self.env)
        self.assertIn("rival_split: true", self.fm("task", "show", tid).stdout)

    def test_the_plans_own_reading_on_top_is_no_split(self):
        self.say(REPLY.replace("- expire every cached token on logout in session_cache.py, not only cache it\n", "")
                 .replace("- keep tokens in memory in a dict", "- keep the tokens in memory in a dict"))
        tid = self.new()
        self.fm("second", "rival", tid, env=self.env)
        self.assertNotIn("rival_split: true", self.fm("task", "show", tid).stdout)

    def test_an_S_brief_has_no_plan_reading_to_differ_from_so_it_is_never_split(self):
        tid = self.new("S", step="publish the release notes")
        self.fm("second", "rival", tid, env=self.env)
        self.assertNotIn("rival_split", self.fm("task", "show", tid).stdout)

    def test_FOREMAN_NO_RIVAL_zero_leaves_the_gate_on(self):
        tid = self.new()
        self.assertIn(f"sampo second rival {tid}", self.focus(tid, False))
        self.env["FOREMAN_NO_RIVAL"] = "0"
        self.assertIn(f"sampo second rival {tid}", self.focus(tid, False))
        self.env["FOREMAN_NO_RIVAL"] = "1"
        self.focus(tid, True)

    def test_the_M_panel_row_is_the_rival_trigger(self):
        from sampo import core as c
        with open(os.path.join(c.PLUGIN_ROOT, "protocols.json")) as f:
            rules = json.load(f)
        self.assertNotEqual(rules["M"]["panel"], "optional")
        self.assertIn("rival", rules["M"]["panel"])


class Debate(_Stubbed):
    def test_a_rebuttal_brief_embeds_the_review(self):
        tid = json.loads(self.fm("capture", "x", "--json").stdout)["id"]
        review = os.path.join(self.tmp, "review.md")
        with open(review, "w") as f:
            f.write("## adversary: changes needed\n- HIGH fmx.py:12 the token is logged\n")
        self.fm("research", "add", f"{tid}-review-1", "--file", review)
        out = self.fm("second", "debate", tid, "--review", f"{tid}-review-1").stdout
        path = next(w for w in out.split() if w.endswith(".debate.md"))
        with open(path) as f:
            brief = f.read()
        self.assertIn("the token is logged", brief)
        self.assertIn("CONFIRMED", brief)
        self.assertIn("foreman:fm-reviewer", out)

    def test_an_unknown_review_is_refused(self):
        tid = json.loads(self.fm("capture", "x", "--json").stdout)["id"]
        self.assertNotEqual(self.fm("second", "debate", tid, "--review", "nope", check=False).returncode, 0)


class Session(_Stubbed):
    def transcript(self, sid, messages, entrypoint=None):
        from sampo import cost
        d = cost.transcripts_dir(self.repo).replace(os.path.expanduser("~/.claude"), self.cc, 1)
        d = os.path.join(self.cc, "projects", os.path.basename(d))
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"{sid}.jsonl"), "w") as f:
            for m in messages:
                f.write(json.dumps(dict({"type": "user", "message": {"role": "user", "content": m}},
                                        **({"entrypoint": entrypoint} if entrypoint else {}))) + "\n")
        return os.path.join(d, f"{sid}.jsonl")

    def test_Interactive_sessions_only(self):
        # T-0307: a newer SDK or claude -p run (a background review, a child) isn't a session the user typed in
        import time
        self.transcript("old-session", ["export the report as CSV please"], entrypoint="cli")
        for i, ep in enumerate(("sdk-py", "sdk-cli")):
            path = self.transcript(f"robot-{i}", ["Review this change for security vulnerabilities"], entrypoint=ep)
            os.utime(path, (time.time() + 10 + i, time.time() + 10 + i))
        self.fm("second", "session", env=dict(self.env, FOREMAN_SESSION_ID="new-session"))
        self.assertIn("export the report as CSV please", self.calls()[0]["stdin"])
        self.assertNotIn("security vulnerabilities", self.calls()[0]["stdin"])

    def test_a_missed_request_is_captured_once(self):
        self.transcript("old-session", ["export the report as CSV please", "<task-notification>x</task-notification>",
                                        "also export the report as CSV"])
        out = self.fm("second", "session", "--if-due", env=dict(self.env, FOREMAN_SESSION_ID="new-session")).stdout
        self.assertIn("export the report as CSV", out)
        inbox = self.fm("state").stdout
        self.assertIn("Export the report as CSV", inbox)
        self.assertIn("export the report as CSV please", self.calls()[0]["stdin"])
        self.assertNotIn("task-notification", self.calls()[0]["stdin"])
        again = self.fm("second", "session", "--if-due", env=dict(self.env, FOREMAN_SESSION_ID="new-session")).stdout
        self.assertIn("already", again.lower())
        self.assertEqual(len(self.calls()), 1)  # once a day

    def test_another_sessions_message_is_not_a_request_and_pasted_html_is(self):
        # T-3687: a cross-session or agent message is a user-role turn the user never typed
        self.transcript("old-session", ['<cross-session-message from="uds:/a.sock" from-name="main">rename the '
                                        'billing tab</cross-session-message>', '<agent-message from="a1">drop the '
                                        'audit table</agent-message>', '<div class="row">the footer overlaps</div>'])
        self.fm("second", "session", env=dict(self.env, FOREMAN_SESSION_ID="new-session"))
        said = self.calls()[0]["stdin"]
        self.assertNotIn("billing tab", said)
        self.assertNotIn("audit table", said)
        self.assertIn("the footer overlaps", said)

    def test_session_start_runs_it_in_the_background(self):
        import time
        self.transcript("old-session", ["export the report as CSV please"])
        self.hook("SessionStart", {"source": "startup", "session_id": "new-session"},
                  env=dict(self.env, FOREMAN_NO_BACKGROUND=""))
        for _ in range(100):  # detached: the hook returned before the review ran
            if "Export the report as CSV" in self.fm("state").stdout:
                break
            time.sleep(0.1)
        self.assertIn("Export the report as CSV", self.fm("state").stdout)

    def test_secrets_stay_out_and_captures_ask_first(self):
        secret = "sk-ant-api03-" + "A" * 40  # pragma: allowlist secret (a fake key for the redaction test)
        self.transcript("old-session", [f"export the report as CSV please, the key is {secret}"])
        self.fm("second", "session", env=dict(self.env, FOREMAN_SESSION_ID="new-session"))
        self.assertNotIn(secret, self.calls()[0]["stdin"])
        brief = self.fm("task", "show", "T-0001").stdout
        self.assertIn("explore: true", brief)

    def test_a_credential_line_is_no_task_and_closed_work_is_shown(self):
        # T-2624: "try these: KEY=…" became an inbox task (redacted, still noise), and an ask a closed task already
        # settled read as unanswered because the child saw only open work
        self.transcript("old-session", ["try these: MODELS_API_KEY=sk-ant-api03-" + "B" * 40,  # pragma: allowlist secret
                                        "export the report as CSV please"])
        tid = json.loads(self.fm("task", "new", "Drop the print-timer flag", "--type", "FIX", "--tier", "S", "--json").stdout)["id"]
        self.fm("task", "drop", tid, "not needed")
        missed = ("## Missed\n- try these: MODELS_API_KEY=[REDACTED] — no follow-up\n"
                  "- export the report as CSV — asked, not done\n")
        out = self.fm("second", "session", env=dict(self.env, FOREMAN_SESSION_ID="new-session",
                                                    STUB_MISSED=missed)).stdout
        self.assertIn("export the report as CSV", out)
        self.assertNotIn("MODELS_API_KEY", self.fm("state").stdout)
        self.assertIn("Drop the print-timer flag", self.calls()[0]["stdin"])

    def test_a_failed_review_is_recorded_and_claims_the_day(self):
        self.transcript("old-session", ["export the report as CSV please"])
        p = self.fm("second", "session", "--if-due", env=dict(self.env, PATH="/nonexistent", HOME=self.tmp), check=False)
        self.assertNotEqual(p.returncode, 0)
        from sampo import core as c
        proj = c.find_project(self.repo)
        self.assertEqual(c.read_meta(proj).get("second_session"), c.now()[:10])
        self.assertTrue(any(e.get("event") == "second_session" and "error" in (e.get("data") or {})
                            for e in c.ledger_tail(proj, 50)))

    def test_a_sensitive_project_is_skipped(self):
        self.transcript("old-session", ["export the report as CSV please"])
        self.fm("sensitive", "on")
        out = self.fm("second", "session", "--if-due", env=self.env).stdout
        self.assertIn("sensitive", out.lower())
        self.assertFalse(os.path.exists(self.log))


class Cleanup(ForemanTestCase):
    def test_a_child_still_writing_into_state_does_not_fail_the_cleanup(self):
        # T-1901: the review session start ran detached wrote into state/ while tearDown removed the folder, so
        # test_session_start_runs_it_in_the_background failed with Errno 39 (directory not empty) on CI
        import tempfile
        import threading
        import time
        d = os.path.join(self.home, "state", "projects", "app")
        os.makedirs(d)
        until = time.monotonic() + 0.5

        def write():  # files into the folder until it's gone, as the child's writes fail once it is
            for i in range(10 ** 9):
                if time.monotonic() > until:
                    return
                try:
                    open(os.path.join(d, f"f{i}"), "w").close()
                except OSError:
                    return

        t = threading.Thread(target=write)
        t.start()
        try:
            ForemanTestCase.tearDown(self)
        finally:
            t.join()
            self._tmp = tempfile.TemporaryDirectory()  # the real tearDown cleans this one
        self.assertFalse(os.path.exists(self.tmp))


class Due(ForemanTestCase):
    def test_only_new_sessions_once_a_day_never_sensitive(self):
        from sampo import core as c
        from sampo import hooks
        today = c.now()[:10]
        self.assertTrue(hooks.second_due({"source": "startup"}, {}))
        self.assertFalse(hooks.second_due({"source": "compact"}, {}))
        self.assertFalse(hooks.second_due({"source": "startup"}, {"second_session": today}))
        self.assertFalse(hooks.second_due({"source": "startup"}, {"sensitive": True}))
        self.assertFalse(hooks.second_due({"source": "startup"}, {}, busy=True))


class LensSkills(ForemanTestCase):
    def test_audit_prep_names_installed_review_skills_per_lens(self):
        cc = os.path.join(self.tmp, "cc")
        path = os.path.join(cc, "plugins", "cache", "m", "sec", "1.0")
        os.makedirs(os.path.join(path, "skills", "sec-scan"))
        with open(os.path.join(path, "skills", "sec-scan", "SKILL.md"), "w") as f:
            f.write("---\nname: sec-scan\ndescription: Security vulnerability review of a diff.\n---\nbody\n")
        os.makedirs(os.path.join(cc, "plugins"), exist_ok=True)
        with open(os.path.join(cc, "plugins", "installed_plugins.json"), "w") as f:
            json.dump({"version": 2, "plugins": {"sec@m": [{"scope": "user", "installPath": path}]}}, f)
        with open(os.path.join(cc, "settings.json"), "w") as f:
            json.dump({"enabledPlugins": {"sec@m": True}}, f)
        self.fm("init")
        tid = json.loads(self.fm("task", "new", "x", "--type", "FIX", "--tier", "M", "--ac", "x :: true", "--step", "x",
                                 "--interpretation", "Reading 1: a. Reading 2: b.", "--approach", "1) a; fails if: x 2) use what exists: b; fails if: y 3) do less: c; fails if: z Choice 1 (80%): a", "--json").stdout)["id"]
        self.fm("focus", tid)
        with open(os.path.join(self.repo, "a.py"), "w") as f:
            f.write("x = 1\n")
        out = self.fm("audit", "prep", tid, env={"CLAUDE_CONFIG_DIR": cc}).stdout
        self.assertIn("adversary: /sec:sec-scan, /security-review", out)
        self.assertIn(f"sampo task audit {tid} adversary", out)
        data = json.loads(self.fm("audit", "prep", tid, "--json", env={"CLAUDE_CONFIG_DIR": cc}).stdout)
        self.assertEqual(data["lens_skills"]["adversary"][0], "/sec:sec-scan")

    def test_lens_tables_are_validated(self):
        from sampo.core import routing_problem
        good = {"lens_words": {"adversary": ["security"]}, "lens_builtin": {"adversary": ["security-review"]}}
        self.assertIsNone(routing_problem(good))
        self.assertIsNotNone(routing_problem(dict(good, lens_words={"adversary": "security"})))
        self.assertIsNotNone(routing_problem(dict(good, lens_builtin={"adversary": ["x; rm -rf /tmp/y"]})))


class OneWayVerb(unittest.TestCase):  # T-2521: deleting a code symbol in a tracked file is undone by git, not one-way
    def test_a_code_symbol_isn_t_a_one_way_object_and_data_still_is(self):
        from sampo import core as c

        class B:  # the two things _one_way reads
            tier = "M"

            def __init__(self, step):
                self.step = step

            def steps(self):
                return [type("S", (), {"text": self.step})()]

            def section(self, name):
                return "tidy the parser"
        for step in ("delete _CONF_RE", "drop the unused fmcore._x helper", "delete `old_name` and its test",
                     "delete parse_args()"):
            self.assertEqual(c._one_way(B(step)), "", step)
        for step, verb in (("delete data/old.csv", "delete"), ("drop the users table", "drop"),
                           ("delete the release branch", "delete"), ("delete old.csv", "delete"),
                           ("delete old_data.csv", "delete"), ("drop users.db", "drop")):
            self.assertEqual(c._one_way(B(step)), verb, step)
