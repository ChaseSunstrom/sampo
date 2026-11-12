"""T-1212: fm code, the harness-run loop. A scripted fake model server (OpenAI or Anthropic wire shape) answers every
request in order; fm code runs as a real subprocess on a throwaway repo with a seeded bug. No real model, no network."""
import http.server
import json
import os
import subprocess
import sys
import threading
import time

import forkcli
from helpers import FM, PLUGIN, ForemanTestCase, busy_factor, read_json, read_text, stub_corpus_replay

PLAN = {"type": "FIX", "tier": "S", "title": "Make calc.add add",
        "criteria": [{"text": "calc.add adds", "verify": "python3 test_calc.py"}], "steps": ["fix calc.add"]}
HANG = object()
NOT_A_FIX = {"calls": [("write", {"file": "notes.txt", "content": "x\n"})]}  # T-3180: an edit, so done isn't refused
M_PLAN = dict(PLAN, tier="M",  # T-2344: an M plan's readings, options with failure modes, and a choice confidence
              readings=["calc.add subtracts by mistake, so it should add", "callers want a difference, so rename it"],
              options=[{"kind": "build", "text": "change the operator in calc.add", "fails_if": "a caller relies on it"},
                       {"kind": "exists", "text": "call the builtin sum over both numbers", "fails_if": "inputs aren't numbers"},
                       {"kind": "less", "text": "document that add subtracts", "fails_if": "callers keep wrong totals"}],
              choice=1, confidence=85, why="the failing test names the operator")


ASIDES = {  # T-4362: a harness question about one check; the words are in its prompt. Answered with its default
    "check command's own failure": '{"own": []}',  # (a red of the project's own) without taking a scripted reply or
    "keeps failing the same way": '{"keep": true}'}  # showing in .seen, unless a test scripts one in Fake.asides[marker]


class Fake:
    """Answers each POST with the next scripted reply (a dict: text and/or calls; in: the prompt tokens it reports, 5 unless given) in the profile's wire shape; HANG
    holds the request until release(); past the script it answers 400. Records every request body."""

    def __init__(self, kind, script):
        self.kind, self.script, self.seen = kind, list(script), []
        self.asides, self.asked = {k: [] for k in ASIDES}, {k: [] for k in ASIDES}
        self.hung, self.release_ = threading.Event(), threading.Event()
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                aside = next((k for k in ASIDES if k in json.dumps(body)), None)
                if aside:
                    fake.asked[aside].append(body)
                    step = {"text": fake.asides[aside].pop(0) if fake.asides[aside] else ASIDES[aside]}
                else:
                    fake.seen.append(body)
                    step = fake.script.pop(0) if fake.script else None
                if step is HANG:
                    fake.hung.set()
                    fake.release_.wait(10)
                    step = None
                data = json.dumps(fake.reply(step) if step else {"error": "script exhausted"}).encode()
                self.send_response(200 if step else 400)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/v1"
        threading.Thread(target=self.srv.serve_forever, args=(0.02,), daemon=True).start()

    def reply(self, step):
        calls = [(f"c{len(self.seen)}_{i}", n, a) for i, (n, a) in enumerate(step.get("calls") or [])]
        if self.kind == "anthropic":
            content = ([{"type": "text", "text": step["text"]}] if step.get("text") else []) + [
                {"type": "tool_use", "id": i, "name": n, "input": a} for i, n, a in calls]
            stop = "max_tokens" if step.get("cut") else "tool_use" if calls else "end_turn"
            return {"content": content, "stop_reason": stop, "usage": {"input_tokens": step.get("in", 5), "output_tokens": 2}}
        msg = {"role": "assistant", "content": step.get("text")}
        if calls:
            msg["tool_calls"] = [{"id": i, "type": "function", "function": {"name": n, "arguments": json.dumps(a)}}
                                 for i, n, a in calls]
        stop = "length" if step.get("cut") else "tool_calls" if calls else "stop"
        return {"choices": [{"message": msg, "finish_reason": stop}],
                "usage": {"prompt_tokens": step.get("in", 5), "completion_tokens": 2}}

    def close(self):
        self.release_.set()
        self.srv.shutdown()
        self.srv.server_close()

    def said(self, i):
        """Everything request i sent, flattened to text: what the model was shown."""
        return json.dumps(self.seen[i])


def plan(p=None, prose="Here is the plan:\n"):
    return {"text": prose + "```json\n" + json.dumps(p or PLAN) + "\n```"}


def call(name, **args):
    return {"calls": [(name, args)]}


def text_action(name, **args):
    return {"text": "I'll do this:\n" + json.dumps(dict(args, action=name))}


CLOSE = {"text": json.dumps({"intent": "calc.add adds, as asked", "adversary": "negative numbers add too",
                             "docs": "none: internal fix", "lesson": "none: a one-character bug",
                             "why_not_caught": "none: test_calc.py is the test that catches it"})}


class CodeCase(ForemanTestCase):
    replayed = True  # T-1401: a fresh green guard corpus replay on record, so unattended runs aren't refused
    # T-3678: the bug, committed in each test's repo by git_repo from a per-process template (2 git processes fewer a test)
    repo_seed = ("seed the bug", (("calc.py", "def add(a, b):\n    return a - b\n"),
                                  ("test_calc.py", "import calc\nassert calc.add(2, 3) == 5, calc.add(2, 3)\nprint('ok')\n")))

    def setUp(self):
        super().setUp()
        if self.replayed:
            stub_corpus_replay(self.repo)
        self.fake_home = os.path.join(self.tmp, "home")  # rm -rf ~ in a script can only ever reach this
        os.makedirs(self.fake_home)
        with open(os.path.join(self.fake_home, "keep"), "w") as f:
            f.write("x")
        self.fakes = []

    def tearDown(self):
        for f in self.fakes:
            f.close()
        super().tearDown()

    def serve(self, kind, *script, tools=True, name="fake"):
        f = Fake(kind, script)
        self.fakes.append(f)
        path = os.path.join(self.home, "models.json")
        conf = read_json(path) if os.path.exists(path) else {"profiles": {}}
        conf["profiles"][name] = {"kind": kind, "base_url": f.url, "model": "m1", "tools": tools}
        with open(path, "w") as fh:
            json.dump(conf, fh)
        return f

    def env(self, extra=None):
        xdg = {k: os.path.join(self.fake_home, d) for k, d in (  # T-1442: no ~/.config default can reach the real one
            ("XDG_CONFIG_HOME", ".config"), ("XDG_DATA_HOME", ".local/share"), ("XDG_STATE_HOME", ".local/state"),
            ("XDG_CACHE_HOME", ".cache"))}
        e = dict(os.environ, FOREMAN_HOME=self.home, FOREMAN_INHIBITED="1", FOREMAN_NO_LOCAL_MODELS="1", HOME=self.fake_home, **{**xdg, **(extra or {})})
        e.pop("CLAUDE_CODE_SESSION_ID", None)
        return e

    def code(self, *args, check=True, env=None, input=None):
        # T-3678: a fork of the warm server, not a fresh interpreter; no input reads /dev/null (T-1374)
        p = forkcli.run(FM, ["code", *args], cwd=self.repo, env=self.env(env), input=input,
                        timeout=60 * busy_factor())  # T-1721: a hang guard; six builders' load tripped it
        if check and p.returncode:
            raise AssertionError(f"fm code exited {p.returncode}\nstdout:{p.stdout}\nstderr:{p.stderr}")
        return p

    def calc(self):
        return read_text(os.path.join(self.repo, "calc.py"))

    def task(self, tid="T-0001"):
        return self.fm_json("task", "show", tid)

    def last_commit(self):
        return subprocess.run(["git", "-C", self.repo, "show", "--stat", "--format=%s", "HEAD"], capture_output=True,
                              text=True).stdout

    def status(self):
        return subprocess.run(["git", "-C", self.repo, "status", "--porcelain"], capture_output=True, text=True).stdout


class CodeEnv(CodeCase):
    def test_xdg_dirs_stay_inside_the_temp_home(self):  # T-1442
        e = self.env()
        for k in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
            self.assertTrue(e[k].startswith(self.fake_home + os.sep), (k, e[k]))
        outside = self.env({"XDG_CONFIG_HOME": "/elsewhere"})  # a test's own extra still wins
        self.assertEqual(outside["XDG_CONFIG_HOME"], "/elsewhere")

    def test_fm_code_runs_as_a_fork_of_the_warm_server_in_a_repo_seeded_before_the_test(self):
        # T-3678: ~600 tests' fm code calls each paid a fresh interpreter, and their seed 2 git processes
        from unittest import mock
        with mock.patch.object(forkcli, "run", wraps=forkcli.run) as run:
            self.assertIn("usage", self.code("--help").stdout)
        self.assertEqual(run.call_args.args[:2], (FM, ["code", "--help"]))
        self.assertEqual(run.call_args.kwargs["cwd"], self.repo)
        log = subprocess.run(["git", "-C", self.repo, "log", "--format=%s", "--name-only"], capture_output=True,
                             text=True).stdout.splitlines()
        self.assertEqual([x for x in log if x], ["seed the bug", "calc.py", "test_calc.py", "init", "README.md"])
        self.assertEqual(self.status(), "")


class EndToEnd(CodeCase):
    def run_fix(self, kind):
        f = self.serve(kind, plan(),
                       call("read", path="calc.py"),
                       call("run", cmd="rm -rf ~"),
                       call("edit", file="calc.py", old="a - b", new="a + b"),
                       call("done", summary="calc.add adds now"))
        r = self.code("make calc.add add instead of subtract", "--model", "fake")
        self.assertIn("return a + b", self.calc())
        self.assertEqual(self.task()["status"], "done", r.stdout)
        brief = read_text(self.task()["path"])
        self.assertRegex(brief, r"`python3 test_calc.py` → exit 0")
        self.assertIn("fm code (fake)", brief)  # the audit line names the model
        shown = self.last_commit()
        self.assertIn("T-0001", shown)
        self.assertIn("calc.py", shown)
        # the read's result went back: the model saw the file
        self.assertIn("return a - b", f.said(2))
        # rm -rf ~ never ran, and the guard's refusal is what the model read next
        self.assertTrue(os.path.exists(os.path.join(self.fake_home, "keep")))
        self.assertIn("Ward: blocked", f.said(3))
        self.assertIn("refused", f.said(3))
        self.assertIn("calc.py", r.stdout)
        journal = os.path.join(os.path.dirname(os.path.dirname(self.task()["path"])), "code", "T-0001.jsonl")
        kinds = [json.loads(line)["kind"] for line in read_text(journal).splitlines()]
        self.assertEqual((kinds[0], kinds[-1]), ("start", "end"))
        return f

    def test_a_green_verify_after_the_steps_edits_closes_the_step_and_the_prompts_say_so(self):
        # T-3124: no done call; the red run before the edit leaves the step open, the green one after it closes it
        # T-3125: the system text bars importing Sampo's modules in a one-liner
        f = self.serve("openai", plan(),
                       call("run", cmd="python3 test_calc.py"),
                       call("edit", file="calc.py", old="a - b", new="a + b"),
                       call("run", cmd="python3 test_calc.py"))
        r = self.code("make calc.add add instead of subtract", "--model", "fake", "--no-commit")
        self.assertIn("return a + b", self.calc())
        self.assertEqual(self.task()["status"], "done", r.stdout)
        self.assertEqual(len(f.seen), 4, "plan, red run, edit, green run: the step closed itself, no done turn")
        self.assertIn("The step is done when you call done, or when a verify command passes after your edits", f.said(1))
        self.assertIn("never import Sampo's modules in a python3 -c one-liner", f.said(1))

    def test_a_green_verify_before_any_edit_leaves_the_step_open(self):  # T-3124
        green = dict(PLAN, criteria=[{"text": "it holds", "verify": "true"}])
        f = self.serve("openai", plan(green), call("run", cmd="true"),
                       call("write", file="notes.txt", content="x\n"), call("done", summary="noted"))
        self.code("note it", "--model", "fake", "--no-commit", check=False)
        self.assertEqual(f.script, [], "the green run before the write didn't end the step: the write and done were asked for")

    def test_openai_shape_fixes_the_seeded_bug(self):
        self.run_fix("openai")

    def test_anthropic_shape_works_the_same(self):
        f = self.run_fix("anthropic")
        self.assertIn("tool_result", f.said(3))  # it really spoke the Anthropic wire shape

    def test_json_event_stream_and_no_api_key_reaches_a_command(self):
        f = self.serve("openai", plan(), call("run", cmd="echo key=${OPENAI_API_KEY:-unset}"),
                       call("edit", file="calc.py", old="a - b", new="a + b"), call("done", summary="x"))
        r = self.code("fix add", "--model", "fake", "--json", "--no-commit", env={"OPENAI_API_KEY": "sk-SECRET"})
        self.assertIn("key=unset", f.said(2))
        self.assertNotIn("sk-SECRET", json.dumps(f.seen))
        events = [json.loads(line) for line in r.stdout.splitlines()]
        self.assertTrue(all("event" in e for e in events))
        self.assertEqual(events[-1]["event"], "closed")
        self.assertIn("seed the bug", self.last_commit())  # --no-commit: closed, nothing committed
        self.assertIn("calc.py", self.status())


class SmallPlaybook(CodeCase):
    """T-2917: a loopback profile's phase prompt carries the kept small-model variant of the routed playbook."""
    def run_with(self, row):
        cards = os.path.join(self.home, "state", "cards")
        os.makedirs(cards)
        with open(os.path.join(cards, "cards.jsonl"), "w") as f:
            f.write(json.dumps(row) + "\n" if row else "")
        f = self.serve("openai", plan(), call("read", path="calc.py"),
                       call("edit", file="calc.py", old="a - b", new="a + b"), call("done", summary="adds now"))
        self.code("make calc.add add instead of subtract", "--model", "fake")
        return f

    def test_the_kept_variant_for_the_routed_playbook_reaches_the_phase_prompt_cut_to_context(self):
        row = {"id": "K-000001", "playbook": "references/verify/tdd-workflow.md", "profile": "fake", "lift": 0.5,
               "orig_lift": 0.25, "title": "Test first always", "when": "You fix a bug with a test.",
               "steps": [{"do": "Write the failing test first.", "cmd": ""}], "example": "x", "check": "y", "traps": ["z" * 5000]}
        f = self.run_with(row)
        self.assertNotIn("A short playbook", f.said(0), "the planner's prompt is unchanged")
        self.assertIn("A short playbook for this kind of task", f.said(1))
        self.assertIn("Write the failing test first", f.said(1))
        self.assertNotIn("z" * 3400, f.said(1), "cut to a tenth of the profile's context")

    def test_no_kept_variant_leaves_the_prompt_unchanged(self):
        f = self.run_with(None)
        self.assertNotIn("A short playbook", "".join(f.said(i) for i in range(len(f.seen))))


class Repairs(CodeCase):
    def test_malformed_plan_is_repaired_and_dry_plan_changes_nothing(self):
        bad = dict(PLAN, type="BUGFIX", criteria=[{"text": "adds"}])
        f = self.serve("openai", {"text": "Sure! " + json.dumps(bad)}, {"text": "no json at all"}, plan())
        r = self.code("fix add", "--model", "fake", "--dry-plan")
        self.assertIn("Make calc.add add", r.stdout)
        self.assertEqual(len(f.seen), 3)
        self.assertIn("BUGFIX", f.said(1))  # the error quoted back
        self.assertIn("no JSON object", f.said(2))
        self.assertEqual(self.status(), "")
        self.assertNotEqual(self.fm("task", "show", "T-0001", check=False).returncode, 0)  # no task made
        self.assertIn("return a - b", self.calc())

    def test_a_plan_never_repaired_stops_before_any_task(self):
        self.serve("openai", *[{"text": "nope"}] * 8)  # three JSON tries, then five field questions (T-1765)
        r = self.code("fix add", "--model", "fake", check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("no valid plan after 2 repairs, nor field by field", r.stderr + r.stdout)
        self.assertIn("Next: fm code models", r.stderr + r.stdout)
        self.assertEqual(self.status(), "")

    def test_a_model_that_cant_write_json_gives_its_plan_field_by_field(self):  # T-1765
        from sampo import smith
        f = self.serve("openai", *[{"text": "nope"}] * 3, {"text": "Type: FIX."}, {"text": "S"},
                       {"text": "Make calc.add add"}, {"text": "- calc.add adds :: `python3 test_calc.py`\nno sep"},
                       {"text": "```\n1. fix calc.add\n```"})
        r = self.code("fix add", "--model", "fake", "--dry-plan")
        self.assertIn("Make calc.add add", r.stdout)
        self.assertIn("field by field", r.stdout)
        self.assertEqual(len(f.seen), 8)
        self.assertIn("one word of FIX", f.said(3))
        self.assertNotIn("corrected JSON", json.dumps(f.seen[3]["messages"][-1]))
        self.assertEqual(smith._criteria("1. a :: `x`\nno sep"), [{"text": "a", "verify": "x"}])
        self.assertEqual(smith._line(smith._unlabel("Why not caught: none: new", "why_not_caught")), "none: new")

    def test_failing_verify_goes_back_and_a_second_fix_passes(self):
        # a model without native tools: every action is a JSON object in its text
        f = self.serve("openai", plan(),
                       text_action("edit", file="calc.py", old="a - b", new="a * b"),
                       text_action("done", summary="fixed?"),
                       text_action("edit", file="calc.py", old="a * b", new="a + b"),
                       text_action("done", summary="fixed"), tools=False)
        r = self.code("fix add", "--model", "fake")
        self.assertIn("return a + b", self.calc())
        self.assertEqual(self.task()["status"], "done", r.stdout)
        self.assertNotIn("tools", f.seen[1])
        self.assertIn("AssertionError: 6", f.said(3))  # the failing verify's output (a * b) went back to the model

    def test_verify_never_passing_blocks_the_task(self):
        self.serve("openai", plan(), NOT_A_FIX, *[call("done", summary="nothing") for _ in range(4)])
        r = self.code("fix add", "--model", "fake", check=False)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.task()["status"], "blocked")

    def test_recheck_reruns_a_blocked_runs_checks_through_the_guard(self):  # T-1341
        self.serve("openai", plan(), NOT_A_FIX, *[call("done", summary="nothing") for _ in range(4)])
        self.code("fix add", "--model", "fake", check=False)
        self.assertEqual(self.task()["status"], "blocked")
        with open(os.path.join(self.repo, "calc.py"), "w") as f:  # a person fixed it in a live CLI
            f.write("def add(a, b):\n    return a + b\n")
        r = self.code("--recheck", "T-0001", "--json", check=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        events = [json.loads(x) for x in r.stdout.splitlines() if x.strip()]
        self.assertEqual([e["event"] for e in events][:3], ["start", "plan", "task"])
        self.assertEqual([(e["label"], e["exit"]) for e in events if e["event"] == "verify"], [("recheck", 0)])
        self.assertIn("fm task finish T-0001", events[-1]["text"])
        self.assertIn("python3 test_calc.py", self.fm("task", "show", "T-0001").stdout.split("## Verification")[1])
        with open(os.path.join(self.repo, "calc.py"), "w") as f:
            f.write("def add(a, b):\n    return a * b\n")
        r = self.code("--recheck", "T-0001", "--json", check=False)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.code("--recheck", "T-0099", check=False).returncode, 1)

    def test_escalate_takes_over_after_three_failed_rounds(self):
        self.serve("openai", plan(), NOT_A_FIX, *[call("done", summary="nothing") for _ in range(4)])
        strong = self.serve("anthropic", call("edit", file="calc.py", old="a - b", new="a + b"),
                            call("done", summary="fixed"), name="strong")
        r = self.code("fix add", "--model", "fake", "--escalate", "strong")
        self.assertEqual(self.task()["status"], "done", r.stdout)
        self.assertIn("fix round 4 (strong)", r.stdout)
        self.assertEqual(len(strong.seen), 2)

    def test_a_sensitive_repo_never_reaches_an_offsite_model(self):
        self.fm("init")
        self.fm("sensitive", "on")
        with open(os.path.join(self.home, "models.json"), "w") as fh:
            json.dump({"profiles": {"far": {"base_url": "https://models.example.invalid/v1", "model": "x"}}}, fh)
        r = self.code("fix add", "--model", "far", check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("sensitive", r.stderr)

    def test_m_tier_asks_the_model_for_its_close_notes(self):
        f = self.serve("anthropic", plan(M_PLAN),
                       call("edit", file="calc.py", old="a - b", new="a + b"), call("done", summary="x"), CLOSE)
        r = self.code("fix add", "--model", "fake")
        self.assertEqual(self.task()["status"], "done", r.stdout)
        self.assertIn("negative numbers add too", read_text(self.task()["path"]))
        self.assertEqual(len(f.seen), 4)

    def test_the_harness_written_plan_is_exempt_from_the_plan_gate_content_checks(self):
        # T-2335: an S plan is the harness's, not a reading/option plan the model wrote; it says so, no boilerplate labels
        # (T-2344: an M plan carries the model's readings and options and meets the gate: PlanReadings)
        self.serve("anthropic", plan(PLAN),
                   call("edit", file="calc.py", old="a - b", new="a + b"), call("done", summary="x"), CLOSE)
        self.code("fix add", "--model", "fake")
        from sampo import core
        b = core.find_brief(core.find_project(self.repo), self.task()["id"])
        self.assertEqual(b.meta.get("harness_plan"), "code")
        self.assertNotIn("use what exists", b.section("Approach (options → choice → why)"))


class PlanReadings(CodeCase):
    """T-2344 (Mission 5, #2 and #8 in fm code): an M plan names its readings, 3+ options each with a failure mode (one
    use-what-exists, one do-less) and 'Choice N (NN%)'; check_plan runs the plan gate's own content check on it, and the
    task fm code files passes fm focus's gate unchanged (no harness exemption for M)."""

    def test_check_plan_asks_an_m_plan_for_readings_options_and_a_choice_confidence(self):
        from sampo import smith
        self.assertIsNone(smith.check_plan(M_PLAN))
        self.assertIsNone(smith.check_plan(PLAN))  # S is unchanged
        for k in ("readings", "options", "confidence"):
            with self.subTest(drop=k):
                self.assertIn(k, smith.check_plan({x: v for x, v in M_PLAN.items() if x != k}) or "")
        bare = dict(M_PLAN, options=[dict(o, fails_if="") for o in M_PLAN["options"]])
        self.assertIn("fails_if", smith.check_plan(bare) or "")
        no_less = dict(M_PLAN, options=[dict(o, kind="build") if o["kind"] == "less" else o for o in M_PLAN["options"]])
        self.assertIn("do less", smith.check_plan(no_less) or "")
        one = dict(M_PLAN, readings=M_PLAN["readings"][:1])
        self.assertIn("ruled_out", smith.check_plan(one) or "")
        self.assertIsNone(smith.check_plan(dict(one, ruled_out="renaming it: the request says the sum is wrong")))
        same = dict(M_PLAN, options=[dict(o, text="change the operator in calc.add to a plus sign",
                                          fails_if="a caller relies on the old result") for o in M_PLAN["options"]])
        self.assertIn("options", smith.check_plan(same) or "")  # the gate's own distinctness check, quoted back
        for word in ("readings", "fails_if", "Choice", "confidence"):
            self.assertIn(word, smith.PLAN_PROMPT)

    def test_the_m_task_fm_code_files_passes_the_plan_gate_unchanged(self):
        self.serve("anthropic", plan(M_PLAN), call("edit", file="calc.py", old="a - b", new="a + b"),
                   call("done", summary="x"), CLOSE)
        self.code("fix add", "--model", "fake")
        from sampo import core
        b = core.find_brief(core.find_project(self.repo), self.task()["id"])
        self.assertIsNone(b.meta.get("harness_plan"))
        self.assertEqual(core._content_gaps(b), [])
        self.assertIn("Reading 2:", b.section("Interpretation"))
        ap = b.section("Approach (options → choice → why)")
        self.assertIn("Choice 1 (85%)", ap)
        self.assertIn("use what exists:", ap)

    def test_an_m_plan_under_the_race_bar_gets_its_rival_read_before_focus(self):
        os.environ["FOREMAN_NO_RIVAL"] = "0"  # the suite skips the read; here the stubbed child fails, which never blocks
        self.serve("anthropic", plan(dict(M_PLAN, confidence=60)), call("edit", file="calc.py", old="a - b", new="a + b"),
                   call("done", summary="x"), CLOSE)
        r = self.code("fix add", "--model", "fake")
        from sampo import core
        b = core.find_brief(core.find_project(self.repo), self.task()["id"])
        self.assertTrue(b.meta.get("rival_at") or b.section("Rival plan").strip(), r.stdout)
        self.assertEqual(self.task()["status"], "done", r.stdout)


class MissNote(CodeCase):
    """T-2378 (#5 in fm code): a shell run that fails on a missing tool goes through Claude Code's own failure hook, so
    the model reads the same miss note and the task can't finish past the miss."""

    def test_a_missing_tool_in_a_run_gets_the_miss_note_and_the_finish_check(self):
        f = self.serve("openai", plan(), call("run", cmd="nosuchtool-q7 --version"),
                       call("edit", file="calc.py", old="a - b", new="a + b"), call("done", summary="x"))
        r = self.code("fix add", "--model", "fake", check=False)
        self.assertIn("`nosuchtool-q7` is missing", f.said(2))
        self.assertIn('Find it: sampo which', f.said(2))
        from sampo import core
        p = core.find_project(self.repo)
        misses = [e for e in core.ledger_events(p, ("capability_miss",))
                  if (e.get("data") or {}).get("name") == "nosuchtool-q7"]
        self.assertEqual([e["task"] for e in misses], [self.task()["id"]])
        self.assertNotEqual(self.task()["status"], "done", r.stdout)  # fm task finish refuses past an open miss


class SelfRunningNote(CodeCase):  # T-2364: fm code shows what runs by itself, as fm state does
    def test_the_first_line_names_the_project_and_its_root(self):  # T-2741: a -p run shows where it resolved
        self.serve("openai", plan(), call("edit", file="calc.py", old="a - b", new="a + b"), call("done", summary="x"))
        r = self.code("fix calc.add so it adds", "--model", "fake")
        slug = json.load(open(os.path.join(self.repo, ".fm", "project.json"))).get("slug") if os.path.exists(
            os.path.join(self.repo, ".fm", "project.json")) else None
        first = r.stdout.splitlines()[0]
        self.assertTrue(first.startswith("fm code in "), first)
        self.assertIn(f"({os.path.realpath(self.repo)})", first)
        if slug:
            self.assertIn(slug, first)
        r = self.code("fix calc.add so it adds", "--model", "fake", "--json", check=False)
        start = next(json.loads(x) for x in r.stdout.splitlines() if x.startswith("{") and '"start"' in x)
        self.assertEqual((bool(start.get("project")), start.get("root")), (True, os.path.realpath(self.repo)))

    def test_fm_code_prints_the_self_running_line_at_its_start(self):
        self.fm("task", "new", "waiting", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true", "--step", "s")
        self.fm("ask", "T-0001", "plugin", "--later", "--why", "needs a linter")
        self.serve("openai", plan(), call("edit", file="calc.py", old="a - b", new="a + b"), call("done", summary="x"))
        r = self.code("fix calc.add so it adds", "--model", "fake")
        self.assertIn("Self-running:", r.stdout)
        self.assertIn("1 install waiting", r.stdout)


class CloseLines(CodeCase):  # T-2395: fm code's close prints the Not verified and meaning lines fm task finish does
    def test_unproven_and_meaning_lines_reach_fm_code_s_close_output(self):
        self.serve("openai", plan(), call("edit", file="calc.py", old="a - b", new="a + b"), call("done", summary="x"))
        r = self.code("fix calc.add so it adds. Also email the weekly digest to every admin on Mondays.", "--model", "fake")
        self.assertEqual(self.task()["status"], "done", r.stdout)
        self.assertIn("meaning unproven", r.stdout)
        self.assertIn("email the weekly digest", r.stdout)


class AnyRequest(CodeCase):
    """T-1320: fm code takes any request: a question gets an answer (no task, nothing changed), and a plan whose check
    can't run here goes back to the planner instead of ending the run."""

    def test_a_question_is_answered_from_the_files_and_changes_nothing(self):
        f = self.serve("openai", {"text": json.dumps({"answer": "it subtracts"})},
                       {"text": json.dumps({"wants_change": False, "why": "it asks what the code does"})},  # T-4284
                       call("read", path="calc.py"),
                       call("edit", file="calc.py", old="a - b", new="a + b"),
                       call("done", summary="calc.add returns a - b (calc.py line 2): it subtracts, it doesn't add"))
        r = self.code("what does calc.add do?", "--model", "fake", "--json")
        events = [json.loads(line) for line in r.stdout.splitlines() if line.startswith("{")]
        answer = [e for e in events if e["event"] == "answer"]
        self.assertEqual(len(answer), 1, r.stdout)
        self.assertIn("calc.py line 2", answer[0]["text"])
        self.assertIn("return a - b", self.calc(), "a question changes nothing")
        self.assertIn("refused", f.said(4), "the edit was refused: this run only answers")
        self.assertNotEqual(self.fm("task", "show", "T-0001", check=False).returncode, 0, "no task for a question")

    def test_a_plan_that_also_says_answer_is_a_plan(self):
        both = dict(PLAN, answer="it subtracts")
        self.serve("openai", plan(both), call("edit", file="calc.py", old="a - b", new="a + b"),
                   call("done", summary="adds"))
        r = self.code("fix add", "--model", "fake", check=False)
        self.assertEqual(self.task()["status"], "done", r.stdout + r.stderr)

    def test_a_check_that_cant_run_here_goes_back_to_the_planner(self):
        bad = dict(PLAN, criteria=[{"text": "calc.add adds", "verify": "python3 -m nosuchmod_t1320 -q"}])
        f = self.serve("openai", plan(bad), plan(),
                       call("edit", file="calc.py", old="a - b", new="a + b"), call("done", summary="adds"))
        r = self.code("fix add", "--model", "fake", check=False)
        self.assertEqual(self.task()["status"], "done", r.stdout + r.stderr)
        self.assertIn("nosuchmod_t1320", f.said(1), "why it can't run was quoted back")


class Resume(CodeCase):
    def kill_mid_run(self, p=None):
        asked = [{"text": json.dumps({"wants_change": False, "why": "findings"})}] \
            if (p or {}).get("type") == "RESEARCH" else []  # T-4284: a RESEARCH plan's request is judged once
        first = self.serve("openai", plan(p), *asked, call("read", path="calc.py"), HANG)
        p = subprocess.Popen([sys.executable, FM, "code", "fix add", "--model", "fake"], cwd=self.repo, env=self.env(),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertTrue(first.hung.wait(30), "fm code never reached the hanging request")
            p.kill()
        finally:
            p.communicate(timeout=30)
        self.assertEqual(self.task()["status"], "active")

    def test_resume_after_a_killed_run_finishes_the_task(self):
        self.kill_mid_run()
        second = self.serve("openai", call("edit", file="calc.py", old="a - b", new="a + b"),
                            call("done", summary="fixed"))
        r = self.code("--resume", "--model", "fake")
        self.assertEqual(self.task()["status"], "done", r.stdout + r.stderr)
        self.assertIn("return a + b", self.calc())
        self.assertIn("return a - b", second.said(0))  # it picked up the conversation, read result and all
        self.assertEqual(len(second.seen), 2)  # no second plan, no second task
        self.assertNotEqual(self.fm("task", "show", "T-0002", check=False).returncode, 0)

    def test_a_kill_between_two_calls_answers_every_unanswered_one(self):
        marker = os.path.join(self.repo, "started")
        first = self.serve("openai", plan(), {"calls": [("read", {"path": "calc.py"}),
                                                        ("run", {"cmd": "touch started; sleep 3"}),
                                                        ("read", {"path": "test_calc.py"})]})
        p = subprocess.Popen([sys.executable, FM, "code", "fix add", "--model", "fake"], cwd=self.repo, env=self.env(),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            for _ in range(300):
                if os.path.exists(marker) or p.poll() is not None:
                    break
                threading.Event().wait(0.05)
            p.kill()
        finally:
            p.communicate(timeout=30)
        self.assertTrue(os.path.exists(marker))
        second = self.serve("openai", *FIX)
        self.code("--resume", "--model", "fake")
        sent = second.seen[0]["messages"]
        results = {m["tool_call_id"]: m["content"] for m in sent if m["role"] == "tool"}
        self.assertEqual(len(results), 3, sent)
        self.assertIn("return a - b", results["c2_0"])
        self.assertIn("\nnot run (interrupted)", results["c2_1"])
        self.assertIn("\nnot run (interrupted)", results["c2_2"])
        self.assertEqual(self.task()["status"], "done")

    def test_resume_with_nothing_to_resume_says_so(self):
        r = self.code("--resume", check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("nothing to resume", r.stderr)

    def test_claudes_continue_resumes_the_newest_unfinished_run(self):  # T-1399: claude -c
        self.kill_mid_run()
        self.serve("openai", *FIX)
        r = self.code("-c", "--model", "fake")
        self.assertEqual(self.task()["status"], "done", r.stdout + r.stderr)
        self.assertIn("resuming T-0001", r.stdout)


class ClaudeFlags(CodeCase):
    """T-1399: claude's own flags where the meaning matches: -p/--print is --plain, -c/--continue is --resume with no
    id, --output-format json is --json. fm's -p PROJECT still works before the verb (and --project after it)."""

    def test_print_is_plain_and_a_project_before_the_verb_stays_the_project(self):
        from sampo import cli
        parse = cli.build_parser().parse_args
        for flag in ("-p", "--print"):
            a = parse(["code", flag, "x", "--dry-plan"])
            self.assertEqual((a.plain, a.request, a.project, a.dry_plan), (True, ["x"], None, True), flag)
        for flag in ("-c", "--continue"):
            self.assertEqual(parse(["code", flag]).resume, "", flag)
        a = parse(["-p", "SLUG", "code", "x"])
        self.assertEqual((a.project, a.plain), ("SLUG", False))
        self.assertEqual(parse(["code", "--project", "SLUG", "x"]).project, "SLUG")
        slug = self.fm("init").stdout.strip()
        elsewhere = os.path.join(self.tmp, "elsewhere")
        os.makedirs(elsewhere)
        self.serve("openai", plan())
        r = forkcli.run(FM, ["-p", slug, "code", "-p", "fix add", "--model", "fake", "--dry-plan"], cwd=elsewhere,
                        env=self.env(), timeout=60 * busy_factor())
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Make calc.add add", r.stdout)
        self.assertEqual(self.status(), "")

    def test_output_format_json_is_json_and_any_other_is_refused(self):
        self.serve("openai", plan())
        r = self.code("fix add", "--model", "fake", "--output-format", "json", "--dry-plan")
        events = [json.loads(line) for line in r.stdout.splitlines()]
        self.assertEqual(events[-1]["event"], "plan", r.stdout)
        for value in ("stream-json", "text"):
            r = self.code("fix add", "--output-format", value, check=False)
            self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
            self.assertIn("--json", r.stderr)


def journal_text(case, tid="T-0001"):
    return read_text(os.path.join(os.path.dirname(os.path.dirname(case.task(tid)["path"])), "code", f"{tid}.jsonl"))


FIX = [call("edit", file="calc.py", old="a - b", new="a + b"), call("done", summary="fixed")]


class Review(CodeCase):
    def test_credentials_git_and_fifos_are_never_read_or_searched(self):
        with open(os.path.join(self.repo, ".env"), "w") as f:
            f.write("TOKEN_VALUE=hunter2hunter2\n")
        os.mkfifo(os.path.join(self.repo, "pipe"))
        f = self.serve("openai", plan(), call("read", path=".env"), call("read", path=".git/config"),
                       call("read", path="pipe"), call("search", pattern="hunter2"), *FIX)
        self.code("fix add", "--model", "fake")
        sent = json.dumps(f.seen)
        self.assertNotIn("hunter2hunter2", sent)
        self.assertNotIn("repositoryformatversion", sent)
        self.assertIn("pipe", f.said(4))  # the FIFO read answered (refused), it didn't hang

    def test_tool_results_and_the_journal_are_redacted(self):
        with open(os.path.join(self.repo, "notes.txt"), "w") as f:
            f.write("plain plainvalue12345 and ghp_abcdefghijklmnopqrstuvwxyz0123\n")
        f = self.serve("openai", plan(), call("read", path="notes.txt"), *FIX)
        self.code("fix add", "--model", "fake", env={"GROQ_API_KEY": "plainvalue12345"})
        for text in (json.dumps(f.seen), journal_text(self)):
            self.assertNotIn("plainvalue12345", text)
            self.assertNotIn("ghp_abcdefghijklmnopqrstuvwxyz0123", text)
        self.assertIn("plain", f.said(2))

    def test_a_reply_cut_at_the_output_limit_is_not_run(self):
        f = self.serve("openai", plan(), dict(call("write", file="calc.py", content="garbage"), cut=True), *FIX)
        self.code("fix add", "--model", "fake")
        self.assertIn("return a + b", self.calc())
        self.assertIn("cut off", f.said(2))
        self.assertEqual(f.seen[1]["max_tokens"], 8192)

    def test_a_verify_that_already_passes_is_sent_back_once(self):
        weak = dict(PLAN, criteria=[{"text": "it runs", "verify": "true"}])
        f = self.serve("openai", plan(weak), plan(), *FIX)
        r = self.code("fix add", "--model", "fake")
        self.assertIn("proves nothing", f.said(1))
        self.assertEqual(self.task()["status"], "done", r.stdout)

    def test_a_refused_verify_gets_one_replan_then_stops_before_any_task(self):
        bad = dict(PLAN, criteria=[{"text": "clean", "verify": "rm -rf ~"}])
        f = self.serve("openai", plan(bad), plan(bad))
        r = self.code("fix add", "--model", "fake", check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("refused", f.said(1))
        self.assertTrue(os.path.exists(os.path.join(self.fake_home, "keep")))
        self.assertNotEqual(self.fm("task", "show", "T-0001", check=False).returncode, 0)

    def test_a_verify_that_writes_inside_the_repo_waits_for_its_task_and_the_run_closes(self):
        # T-4371 (exam s8-047 goal-c-e61e7e, `rm -f test_app && sh ./test.sh`): asked before any task exists, the guard
        # refuses every write inside the repo for want of a task; that is no refusal of the verify itself
        own = dict(PLAN, criteria=[{"text": "calc.add adds", "verify": "rm -f built.tmp && python3 test_calc.py"}])
        f = self.serve("openai", plan(own), *FIX)
        r = self.code("fix add", "--model", "fake", check=False)
        self.assertEqual(self.task()["status"], "done", r.stdout + r.stderr)
        self.assertNotIn("refused", f.said(1))

    def test_a_verify_that_waits_for_its_task_is_not_run_before_the_task_exists(self):  # T-4371
        from unittest import mock
        sys.path.insert(0, os.path.join(PLUGIN, "lib"))
        from sampo import smith
        run = mock.Mock(plan=dict(PLAN, criteria=[{"text": "x", "verify": "rm -f data.db && python3 test_calc.py"}]),
                        root=self.repo, small=False)
        run.guard.return_value = ("Sampo: no active task in this project, so data.db can't be edited yet. One command "
                                  "starts a small task: sampo task new …")
        with mock.patch.object(smith, "run_bounded", return_value=(1, "")) as ran:
            self.assertIsNone(smith._plan_problem(run))
            ran.assert_not_called()  # focus's door gate snapshots what it deletes, and the guard meets it, first
            run.guard.return_value = "Ward: blocked rm-outside: ~ is outside the project"
            self.assertIn("refused by Smith's guard", smith._plan_problem(run))
            ran.assert_not_called()

    def test_a_plan_focus_refuses_goes_back_to_the_planner_once(self):
        # T-3016: 3 of 15 local exam runs ended here (an M plan with no criterion for a clause of the request)
        kept = dict(M_PLAN, criteria=M_PLAN["criteria"] + [
            {"text": "keep the tests as they are", "verify": "python3 test_calc.py"}])
        # T-4363: the mapping question comes first; two replies that fill nothing leave the whole re-plan to do it
        f = self.serve("anthropic", plan(M_PLAN), {"text": "hmm"}, {"text": "nope"}, plan(kept),
                       call("edit", file="calc.py", old="a - b", new="a + b"), call("done", summary="x"), CLOSE)
        r = self.code("fix add, and keep the tests as they are", "--model", "fake", check=False)
        self.assertIn("isn't planned enough", f.said(3))
        self.assertEqual(self.task("T-0001")["status"], "dropped", r.stdout + r.stderr)
        self.assertEqual(self.task("T-0002")["status"], "done", r.stdout + r.stderr)

    def test_a_plan_refused_twice_ends_the_run_with_the_refusal(self):
        self.serve("anthropic", plan(M_PLAN), {"text": "hmm"}, {"text": "nope"}, plan(M_PLAN), {"text": "hmm"}, {"text": "nope"})
        r = self.code("fix add, and keep the tests as they are", "--model", "fake", check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("refused the plan", r.stderr)
        self.assertEqual(self.task("T-0001")["status"], "dropped")  # the first gave way to the re-plan
        self.assertEqual(self.task("T-0002")["status"], "planned")  # the second stays to be looked at
        self.assertIn("fm task show T-0002", r.stderr)

    def test_a_re_plan_that_cannot_stand_keeps_the_refused_task(self):
        # T-3016 review: the refused task is dropped only once its replacement passes the plan's checks
        weak = dict(M_PLAN, criteria=[{"text": "keep the tests as they are", "verify": "true"}])
        self.serve("anthropic", plan(M_PLAN), {"text": "hmm"}, {"text": "nope"}, plan(weak), plan(weak))  # T-4363: one more round
        r = self.code("fix add, and keep the tests as they are", "--model", "fake", check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("proves nothing", r.stderr)
        self.assertEqual(self.task("T-0001")["status"], "planned", r.stderr)

    def test_max_steps_stop_resumes_with_its_options(self):
        f = self.serve("openai", plan(), call("read", path="calc.py"), call("read", path="calc.py"))
        r = self.code("fix add", "--model", "fake", "--no-commit", "--max-steps", "2", check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("--resume", r.stdout + r.stderr)
        self.assertEqual(self.task()["status"], "active")
        self.assertNotIn('"end"', journal_text(self))
        self.serve("openai", *FIX)
        r = self.code("--resume", "--max-steps", "6")
        self.assertEqual(self.task()["status"], "done", r.stdout)
        self.assertIn("seed the bug", self.last_commit())  # --no-commit came back from the journal
        self.assertEqual(len(f.seen), 3)

    def test_a_deadline_stops_the_run_itself_with_a_resume_hint(self):
        # T-4083: a slow profile hit the exam's wall clock and was killed with no trail (louhi's 'timeout' class)
        f = self.serve("openai", plan(), *[call("run", cmd="sleep 1")] * 8)
        t0 = time.monotonic()
        r = self.code("fix add", "--model", "fake", "--no-commit", "--deadline", "2", check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("out of time", r.stdout + r.stderr)
        self.assertIn("--resume", r.stdout + r.stderr)
        self.assertLess(time.monotonic() - t0, 8, "it stops itself, long before the script runs out")
        self.assertLess(len(f.seen), 9)
        self.assertIn("Time left:", json.dumps(f.seen[-1]), "each turn carries the time left")
        self.assertEqual(self.task()["status"], "active", "resumable")

    def test_trim_keeps_the_last_six_results_whole(self):
        from sampo import smith
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "plan"}]
        for i in range(9):
            msgs += [{"role": "assistant", "content": "", "tool_calls": [{"id": f"c{i}", "name": "read", "args": {}}]},
                     {"role": "tool", "tool_call_id": f"c{i}", "content": f"result {i}\n" + "x" * 500}]
        view = smith.trim(msgs)
        self.assertEqual(len(view), len(msgs))
        self.assertEqual(view[:2], msgs[:2])
        tools = [m["content"] for m in view if m["role"] == "tool"]
        self.assertTrue(all(len(t) < 120 and t.startswith("(older result cut") for t in tools[:3]), tools[:3])
        self.assertEqual(tools[3:], [m["content"] for m in msgs if m["role"] == "tool"][3:])
        self.assertIn("x" * 500, msgs[3]["content"])  # the stored conversation is untouched


class Models(CodeCase):
    def test_models_lists_the_profiles(self):
        self.serve("openai")
        r = self.code("models")
        self.assertIn("fake", r.stdout)
        self.assertIn("m1", r.stdout)

    def test_unknown_model_is_a_plain_error(self):
        r = self.code("fix it", "--model", "nope", check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("nope", r.stderr)
        self.assertNotIn("Traceback", r.stderr)


class TaskRun(CodeCase):
    """T-1330: fm code --task ID works a planned brief: its criteria and steps are the plan, so no planner call."""
    def brief(self, verify="python3 test_calc.py"):
        a = ["task", "new", "make calc.add add", "--type", "FIX", "--tier", "S", "--json", "--step", "fix calc.add"]
        a += ["--ac", f"calc.add adds :: {verify}"] if verify else []
        return json.loads(self.fm(*a).stdout)["id"]

    def test_a_planned_brief_is_worked_without_a_plan_call(self):
        tid = self.brief()
        f = self.serve("openai", call("edit", file="calc.py", old="a - b", new="a + b"), call("done", summary="adds"))
        r = self.code("--task", tid, "--model", "fake", "--json", check=False)
        self.assertEqual(self.task(tid)["status"], "done", r.stdout + r.stderr)
        self.assertIn("return a + b", self.calc())
        self.assertIn("step 1", f.said(0), "the first call is the step, not a plan")
        ev = [json.loads(x) for x in r.stdout.splitlines() if x.startswith("{")]
        self.assertEqual([e.get("task") for e in ev if e["event"] == "start"], [tid])

    def test_a_task_waiting_for_the_users_yes_runs_nothing_until_it_is_given(self):  # T-1369
        from sampo import core
        marker = os.path.join(self.tmp, "ran")
        tid = self.brief(verify=f"touch {marker}; python3 test_calc.py")
        p = core.find_project(self.repo)
        with core.lock(p.dir):
            b = core.find_brief(p, tid)
            b.meta.update(confirm=True, confirm_why="it arrived from the repo's .foreman/ (fm sync)")
            core.save_brief(p, b)
        self.serve("openai", *FIX)  # a model that would work it: only the refusal stops it
        r = self.code("--task", tid, "--model", "fake", check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("waits for your own yes", r.stderr)
        self.assertEqual(r.stderr.strip().splitlines()[-1], f"Next: fm ask {tid} confirm")
        self.assertFalse(os.path.exists(marker), "its verify never ran")
        with core.lock(p.dir):
            b = core.find_brief(p, tid)
            b.meta["allow"] = ["confirm"]  # what the user's yes (fm ask ID confirm) records
            core.save_brief(p, b)
        self.serve("openai", *FIX)
        self.code("--task", tid, "--model", "fake")
        self.assertEqual(self.task(tid)["status"], "done")

    def test_it_refuses_a_task_it_cant_work_and_says_why(self):
        self.serve("openai", call("done", summary="x"))
        no_check = self.brief(verify=None)
        r = self.code("--task", no_check, "--model", "fake", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no criterion", r.stderr)
        dropped = self.brief()
        self.fm("task", "drop", dropped, "not needed")
        r = self.code("--task", dropped, "--model", "fake", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("dropped", r.stderr)
        r = self.code("--task", "T-9999", "--model", "fake", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("T-9999", r.stderr)
        r = self.code("fix it too", "--task", no_check, "--model", "fake", check=False)
        self.assertNotEqual(r.returncode, 0, "a request and --task can't both be the plan")

    def test_it_refuses_checks_it_cant_run_and_a_criterion_without_one(self):
        self.serve("openai", call("done", summary="x"))
        ran = []
        for verify, want in (("python3 -m no_such_module_xyz", "can't run here"), ("rm -rf ~", "refused")):
            tid = self.brief()  # fm task new refuses such a check; an edit or a synced brief can still hold one
            self.fm("task", "ac", tid, "edit", "1", "--verify", verify, check=False)
            if self.task(tid)["criteria"][0]["verify"] != verify:
                continue  # refused at the edit too: nothing for fm code to catch
            r = self.code("--task", tid, "--model", "fake", check=False)
            self.assertNotEqual(r.returncode, 0, verify)
            self.assertIn(want, r.stderr, verify)
            ran.append(verify)
        self.assertTrue(ran, "at least one bad check reached fm code")
        tid = self.brief()
        self.fm("task", "ac", tid, "add", "the README says so")  # no check: evidence would land on the wrong number
        r = self.code("--task", tid, "--model", "fake", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("criterion 2", r.stderr)

    def test_an_earlier_unfinished_run_of_the_task_is_resumed_not_restarted(self):
        tid = self.brief()
        self.serve("openai", call("done", summary="x"))
        code = os.path.join(os.path.dirname(os.path.dirname(self.task(tid)["path"])), "code")
        os.makedirs(code, exist_ok=True)
        with open(os.path.join(code, f"{tid}.jsonl"), "w") as f:
            f.write(json.dumps({"kind": "start", "task": tid}) + "\n")
        r = self.code("--task", tid.lower(), "--model", "fake", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(f"--resume {tid}", r.stderr, "the id is matched whatever its case")

    def test_it_refuses_a_task_another_run_is_working(self):
        import fcntl
        tid = self.brief()
        self.serve("openai", call("done", summary="x"))
        code = os.path.join(os.path.dirname(os.path.dirname(self.task(tid)["path"])), "code")
        os.makedirs(code, exist_ok=True)
        with open(os.path.join(code, f"{tid}.lock"), "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)  # as a running fm code holds it
            r = self.code("--task", tid, "--model", "fake", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("another fm code run", r.stderr)


def run_friction_lines(case):
    """T-1339: the 'run friction:' log lines on Foreman's own Improve fm code task (None when there's no task)."""
    from sampo import core
    sp = core.find_project(case.home)
    b = sp and next((b for b in core.load_briefs(sp) if b.title.startswith("Improve fm code")), None)
    return b and [x.split("run friction: ", 1)[1] for x in b.section("Log").splitlines() if "run friction: " in x]


class TreeLock(CodeCase):  # T-1342
    def test_a_second_run_in_the_same_tree_names_the_one_holding_it(self):
        import fcntl
        import hashlib
        jdir = os.path.join(self.home, "state", "projects")
        self.serve("openai", plan(), *FIX)
        self.code("fix add", "--model", "fake")  # makes the project's code folder
        code = next(os.path.join(jdir, d, "code") for d in os.listdir(jdir))
        path = os.path.join(code, f"tree-{hashlib.sha1(os.fsencode(os.path.realpath(self.repo))).hexdigest()[:12]}.lock")
        with open(path, "a+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fh.seek(0)
            fh.truncate()
            fh.write(json.dumps({"pid": 4242, "task": "T-0009", "since": "2026-10-23T10:15:00Z"}))
            fh.flush()
            self.serve("openai", plan())
            r = self.code("fix it again", "--model", "fake", check=False)
            self.assertEqual(r.returncode, 1)
            self.assertIn("another fm code run is working in this tree: T-0009 (pid 4242, since 10:15 UTC)", r.stderr)
            self.assertIn("Next: fm code tui", r.stderr)
            self.serve("openai", plan())
            self.code("fix it again", "--model", "fake", "--dry-plan")  # a dry plan changes nothing: no lock

    def test_close_and_block_reach_fm_notify(self):
        said = os.path.join(self.tmp, "said.txt")
        self.fm("notify", f'printf "%s\\n" "$1" >> {said}')
        self.serve("openai", plan(), *FIX)
        self.code("fix add", "--model", "fake")
        with open(said) as f:
            self.assertIn("fm code T-0001 closed: Make calc.add add", f.read())


class AskBeside(CodeCase):  # T-1770
    def test_ask_answers_beside_a_run_holding_the_tree_and_never_plans_a_change(self):
        import fcntl
        import hashlib
        jdir = os.path.join(self.home, "state", "projects")
        self.serve("openai", plan(), *FIX)
        self.code("fix add", "--model", "fake")
        code = next(os.path.join(jdir, d, "code") for d in os.listdir(jdir))
        path = os.path.join(code, f"tree-{hashlib.sha1(os.fsencode(os.path.realpath(self.repo))).hexdigest()[:12]}.lock")
        with open(path, "a+") as fh:  # another run is working in this tree
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fh.seek(0)
            fh.truncate()
            fh.write(json.dumps({"pid": 4242, "task": "T-0009", "since": "2026-10-23T10:15:00Z"}))
            fh.flush()
            f = self.serve("openai", {"text": json.dumps({"answer": "it adds"})}, call("read", path="calc.py"),
                           call("done", summary="calc.add returns a + b (calc.py line 2)"))
            r = self.code("what does calc.add do?", "--ask", "--model", "fake", "--json")
            answer = [json.loads(x) for x in r.stdout.splitlines() if x.startswith("{") and '"answer"' in x]
            self.assertIn("calc.py line 2", answer[-1]["text"], r.stdout)
            self.assertIn("plan no change", json.dumps(f.seen[0]), "the planner is told it's a question")
            self.serve("openai", plan())
            r = self.code("fix it again", "--ask", "--model", "fake", check=False)
            self.assertEqual(r.returncode, 1)
            self.assertIn("--ask answers questions only", r.stderr)
        self.assertNotEqual(self.fm("task", "show", "T-0002", check=False).returncode, 0, "no task from --ask")
        r = self.code("--ask", "--task", "T-0001", check=False)
        self.assertIn("--ask answers a question", r.stderr)


class FollowUp(CodeCase):  # T-1340
    def test_a_follow_up_carries_the_earlier_runs_request_plan_and_files(self):
        self.serve("openai", plan(), *FIX)
        self.code("fix add", "--model", "fake")
        f = self.serve("openai", plan(dict(PLAN, title="Add a docstring to calc.add")))
        out = self.code("now document it", "--after", "T-0001", "--model", "fake", "--dry-plan").stdout
        self.assertIn("Add a docstring", out)
        sent = json.dumps(f.seen[0])
        for want in ("follow-up to T-0001 (closed", "Its request: fix add", "Its plan: Make calc.add add",
                     "Files it changed: calc.py", "now document it"):
            self.assertIn(want, sent)
        r = self.code("x", "--after", "T-0099", "--model", "fake", "--dry-plan", check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("no fm code run of that task", r.stderr)

    def test_the_new_task_names_the_earlier_one(self):
        self.serve("openai", plan(), *FIX)
        self.code("fix add", "--model", "fake")
        with open(os.path.join(self.repo, "calc.py"), "w") as fh:
            fh.write("def add(a, b):\n    return a - b  # broken again\n")  # a new size: no stale .pyc
        subprocess.run(["git", "-C", self.repo, "commit", "-qam", "break it again"], check=True)
        self.serve("openai", plan(), *FIX)
        self.code("and again", "--after", "T-0001", "--model", "fake")
        self.assertIn("(a follow-up to T-0001)", self.fm("task", "show", "T-0002").stdout)


class SkillOffer(CodeCase):  # T-1397
    def test_a_matching_indexed_skill_is_offered_once_and_nothing_else_is(self):
        kit = os.path.join(self.tmp, "kit")
        for rel, body in {"skills/pdf/SKILL.md": "---\nname: pdf\ndescription: Extract text and tables from PDF "
                                                  "files, fill forms\n---\n" + "body\n" * 200,
                          "skills/docx/SKILL.md": "---\nname: docx\ndescription: Word documents and their tables\n---\n",
                          "LICENSE": "MIT License\n\nCopyright\n"}.items():
            os.makedirs(os.path.dirname(os.path.join(kit, rel)), exist_ok=True)
            with open(os.path.join(kit, rel), "w") as f:
                f.write(body)
        for a in (["init", "-q"], ["add", "-A"], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "kit"]):
            subprocess.run(["git", "-C", kit, *a], check=True)
        p = forkcli.run(FM, ["lib", "source", "add", kit, "--name", "kit"], cwd=self.repo, env=self.env())
        self.assertEqual(p.returncode, 0, p.stderr)
        self.serve("openai", plan())
        out = self.code("extract tables from this pdf", "--model", "fake", "--dry-plan").stdout
        offers = [x for x in out.splitlines() if "skill: " in x]
        self.assertEqual(len(offers), 1, out)
        self.assertIn("skill: kit:pdf matches (open, ~", offers[0])
        self.assertIn("nothing is installed", out)
        self.serve("openai", plan())
        self.assertNotIn("skill:", self.code("fix add", "--model", "fake", "--dry-plan").stdout)


class Toolchain(CodeCase):  # T-1407
    def test_pins_are_read_as_data(self):
        from sampo import smith
        with open(os.path.join(self.repo, ".tool-versions"), "w") as f:
            f.write("# pinned\npython 3.12.1 3.11.0\nnodejs ref:main\nruby system\n\ngolang 1.22.0 # go\n")
        with open(os.path.join(self.repo, ".nvmrc"), "w") as f:
            f.write("lts/*\n")
        with open(os.path.join(self.repo, ".python-version"), "w") as f:
            f.write("3.9.0\n")
        self.assertEqual(smith.pins(self.repo), [("python", "3.12.1"), ("golang", "1.22.0")])

    def test_checks_run_on_the_pinned_interpreter_and_an_envrc_never_runs(self):
        bin_, pin = os.path.join(self.tmp, "fakebin"), os.path.join(self.tmp, "pin", "bin")
        os.makedirs(bin_)
        os.makedirs(pin)
        mark, envrc = os.path.join(self.tmp, "pinned-ran"), os.path.join(self.tmp, "envrc-ran")
        with open(os.path.join(bin_, "mise"), "w") as f:
            f.write(f'#!/bin/sh\n[ "$1 $2" = "where python@3.12.1" ] && echo {os.path.dirname(pin)} && exit 0\nexit 1\n')
        with open(os.path.join(pin, "python3"), "w") as f:
            f.write(f'#!/bin/sh\ntouch {mark}\nexec {sys.executable} "$@"\n')
        for x in (os.path.join(bin_, "mise"), os.path.join(pin, "python3")):
            os.chmod(x, 0o755)
        with open(os.path.join(self.repo, ".tool-versions"), "w") as f:
            f.write("python 3.12.1\nnodejs 99.0.0\n")
        with open(os.path.join(self.repo, ".envrc"), "w") as f:
            f.write(f"touch {envrc}\n")
        subprocess.run(["git", "-C", self.repo, "add", "."], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", "pins"], check=True)
        self.serve("openai", plan(), *FIX)
        r = self.code("fix add", "--model", "fake", env={"PATH": bin_ + os.pathsep + os.environ["PATH"]})
        self.assertIn("toolchain: python 3.12.1 (mise) first on PATH", r.stdout)
        self.assertIn("not installed by mise, asdf or pyenv here: nodejs 99.0.0", r.stdout)
        self.assertTrue(os.path.exists(mark), "the verify ran on the pinned python3")
        self.assertFalse(os.path.exists(envrc), "a repo's .envrc is never sourced")
        self.assertEqual(self.task()["status"], "done")


class SlotIntegrity(CodeCase):  # T-1398
    def test_elided_code_is_found_only_when_the_old_text_lacks_it(self):
        from sampo import smith
        self.assertEqual(smith.elided("def f():\n    # ... rest unchanged\n    return 1\n"), "# ...")
        self.assertIsNotNone(smith.elided("x = 1\n// ... existing code ...\n"))
        self.assertIsNotNone(smith.elided("the rest of the file is unchanged"))
        self.assertIsNone(smith.elided("class P(Protocol):\n    def f(self): ...\n    ...\n"), "an Ellipsis is code")
        self.assertIsNone(smith.elided("# ... rest unchanged\n", have="x\n# ... rest unchanged\n"), "already there")

    def test_an_edit_or_write_that_elides_code_is_refused_and_re_asked(self):
        f = self.serve("openai", plan(),
                       call("edit", file="calc.py", old="def add(a, b):\n    return a - b",
                            new="def add(a, b):\n    # ... rest unchanged\n    return a + b"),
                       call("write", file="calc.py", content="def add(a, b):\n    return a + b\n# ... existing code ...\n"),
                       *FIX)
        self.code("fix add", "--model", "fake")
        self.assertIn("elides code", f.said(2))
        self.assertIn("elides code", f.said(3))
        self.assertNotIn("...", self.calc())
        self.assertEqual(self.task()["status"], "done")

    def test_a_near_miss_anchor_is_applied_once_and_an_ambiguous_one_refused(self):
        from sampo import smith
        text = "def a():\n    x = 1\n\ndef b():\n  x  =  1\n"
        self.assertEqual(smith.near_anchor("def add(a, b):\n    return a - b\n", "def add(a, b):\n  return a - b"),
                         (0, 31))
        self.assertEqual(smith.near_anchor(text, "x = 1"), "ambiguous")
        self.assertIsNone(smith.near_anchor(text, "y = 2"))
        f = self.serve("openai", plan(), call("edit", file="calc.py", old="def add(a, b):\n  return a - b",
                                              new="def add(a, b):\n    return a + b"), call("done", summary="x"))
        self.code("fix add", "--model", "fake")
        self.assertIn("near match", f.said(2))
        self.assertIn("return a + b", self.calc())

    def test_a_fix_round_names_where_the_check_fails(self):
        from sampo import smith
        out = ('Traceback (most recent call last):\n  File "/usr/lib/python3/x.py", line 9, in f\n'
               f'  File "{os.path.join(self.repo, "test_calc.py")}", line 2, in <module>\n'
               'calc.py:2:5: E999 x\ncalc.py(2): error\n')
        self.assertEqual(smith.places(out, os.path.realpath(self.repo)), ["test_calc.py:2", "calc.py:2"])
        f = self.serve("openai", plan(), NOT_A_FIX, call("done", summary="nothing yet"), *FIX)
        self.code("fix add", "--model", "fake")
        self.assertIn("Where it fails: test_calc.py:2", json.dumps(f.seen[3]))


class Leases(CodeCase):  # T-1764
    def test_a_model_runs_leases_end_with_it(self):
        self.serve("openai", plan(), *FIX)
        self.code("fix add", "--model", "fake")
        with open(os.path.join(self.repo, "calc.py"), "w") as f:  # a new size: a .pyc from this second can't pass
            f.write("def add(a, b):\n    return a - b  # broken again\n")
        self.serve("openai", plan(), call("edit", file="calc.py", old="a - b", new="a + b"), call("done", summary="x"))
        r = self.code("fix add again", "--model", "fake", "--json", check=False)
        self.assertNotIn("is leased by session", r.stdout, "the first run's lease ended with it")
        self.assertEqual(self.task("T-0002")["status"], "done", r.stdout[-600:])


class FirstClose(CodeCase):  # T-1349
    def test_only_a_projects_first_closed_run_says_so(self):
        self.serve("openai", plan(), *FIX)
        first = json.loads(self.code("fix add", "--model", "fake", "--json").stdout.splitlines()[-1])
        self.assertEqual((first["event"], first.get("first_in")), ("closed", "app"))
        with open(os.path.join(self.repo, "calc.py"), "w") as f:  # a new size: a .pyc from this second can't pass
            f.write("def add(a, b):\n    return a - b  # broken again\n")
        self.serve("openai", plan(), call("edit", file="calc.py", old="a - b", new="a + b"), call("done", summary="x"))
        again = json.loads(self.code("fix add again", "--model", "fake", "--json").stdout.splitlines()[-1])
        self.assertEqual(again["event"], "closed")
        self.assertNotIn("first_in", again)


class Why(CodeCase):  # T-1760 (T-1373)
    def test_why_explains_a_closed_run_and_names_the_next_command(self):
        self.serve("openai", plan(), *FIX)
        self.code("fix add", "--model", "fake")
        out = self.code("why", "T-0001").stdout
        self.assertIn("T-0001: closed (fix add)", out)
        self.assertIn("done: step 1", out)
        self.assertTrue(out.rstrip().endswith("Next: fm task show T-0001"), out)
        self.assertEqual(self.code("why").stdout, out, "no ID: the newest run")

    def test_why_explains_a_blocked_run_with_its_reason(self):
        self.serve("openai", plan(), NOT_A_FIX, *[call("done", summary="nothing") for _ in range(4)])
        self.code("fix add", "--model", "fake", check=False)
        out = self.code("why").stdout
        self.assertIn("T-0001: blocked (fix add)", out)
        self.assertIn("the verify still fails", out)
        self.assertIn("Next: fm task show T-0001", out)

    def test_why_explains_a_run_a_kill_stopped(self):
        first = self.serve("openai", plan(), call("read", path="calc.py"), HANG)
        p = Durable.start(self, "fix add", "--model", "fake")
        Durable.kill_when(self, p, first.hung.is_set)
        out = self.code("why", "T-0001").stdout
        self.assertIn("T-0001: stopped by a kill or a crash (fix add)", out)
        self.assertIn("was working on: step 1", out)
        self.assertIn("Next: fm code --resume T-0001", out)

    def test_why_with_no_such_run_says_so(self):
        r = self.code("why", "T-0099", check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("no fm code run T-0099", r.stderr)


class PipedContext(CodeCase):  # T-1760 (T-1374)
    def test_piped_stdin_is_the_requests_context(self):
        f = self.serve("openai", plan(), *FIX)
        self.code("fix add", "--model", "fake", input="the add test fails with -1\n")
        self.assertIn("the add test fails with -1", json.dumps(f.seen[0]), "the plan call sees it")
        self.assertIn("the add test fails with -1", json.dumps(f.seen[1]), "a work call too")
        self.assertEqual(self.task()["status"], "done")

    def test_nothing_piped_adds_nothing(self):
        f = self.serve("openai", plan(), *FIX)
        self.code("fix add", "--model", "fake")
        self.assertNotIn("piped on stdin", json.dumps(f.seen[0]))


class RunFriction(CodeCase):  # T-1339
    def test_each_new_cause_is_logged_once_onto_foremans_own_friction_task(self):
        for _ in range(2):  # blocked twice for the same reason: one line
            self.serve("openai", plan(), NOT_A_FIX, *[call("done", summary="nothing") for _ in range(4)])
            self.code("fix add", "--model", "fake", check=False)
        self.code("fix it", "--model", "nope", check=False)  # fails before any task
        lines = run_friction_lines(self)
        self.assertEqual([x.split(":")[0] for x in lines], ["blocked", "preflight"], lines)
        here = [b.title for b in __import__("sampo.core").core.load_briefs(__import__("sampo.core").core.find_project(self.repo))]
        self.assertFalse(any(t.startswith("Improve fm code") for t in here), "never in the user's own project")

    def test_a_run_a_kill_left_unfinished_is_friction_at_the_next_start(self):
        first = self.serve("openai", plan(), HANG)
        p = Durable.start(self, "fix add", "--model", "fake")
        Durable.kill_when(self, p, first.hung.is_set)
        self.serve("openai", plan())
        self.code("something else", "--model", "fake", "--dry-plan")
        self.assertIn("killed", [x.split(":")[0] for x in run_friction_lines(self)])


class Durable(CodeCase):  # T-1338
    def start(self, *args):
        return subprocess.Popen([sys.executable, FM, "code", *args], cwd=self.repo, env=self.env(),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def kill_when(self, p, ready):
        try:
            for _ in range(600):
                if ready() or p.poll() is not None:
                    break
                time.sleep(0.05)
            self.assertTrue(ready(), "fm code never reached the point to kill it at")
        finally:
            p.kill()
            p.communicate(timeout=30)

    def test_a_new_run_names_each_unfinished_run_with_its_resume_command(self):
        first = self.serve("openai", plan(), HANG)
        self.kill_when(self.start("fix add", "--model", "fake"), first.hung.is_set)
        self.serve("openai", plan(), plan())
        r = self.code("fix add again", "--model", "fake", "--dry-plan")
        self.assertIn("unfinished run T-0001 (fix add): fm code --resume T-0001", r.stdout)
        events = [json.loads(x) for x in self.code("--dry-plan", "x", "--model", "fake", "--json",
                                                    check=False).stdout.splitlines() if x.startswith("{")]
        self.assertIn("T-0001", [e.get("unfinished") for e in events])

    def test_a_kill_while_planning_leaves_nothing_to_resume_and_the_next_run_starts_clean(self):
        first = self.serve("openai", HANG)
        self.kill_when(self.start("fix add", "--model", "fake"), first.hung.is_set)
        self.assertNotEqual(self.fm("task", "show", "T-0001", check=False).returncode, 0, "no task yet")
        self.assertIn("nothing to resume", self.code("--resume", check=False).stderr)
        self.serve("openai", plan(), *FIX)
        self.code("fix add", "--model", "fake")
        self.assertEqual(self.task()["status"], "done")

    def test_a_kill_during_the_check_resumes_at_the_check_without_redoing_the_step(self):
        waiting, ok = (os.path.join(self.tmp, n) for n in ("waiting", "ok"))
        check = (f"if grep -q 'a + b' calc.py && [ ! -f {ok} ]; then touch {waiting}; sleep 30; fi; "  # after the step
                 "python3 test_calc.py")
        pl = dict(PLAN, criteria=[{"text": "calc.add adds", "verify": check}])
        self.serve("openai", plan(pl), *FIX)
        self.kill_when(self.start("fix add", "--model", "fake"), lambda: os.path.exists(waiting))
        self.assertEqual(self.task()["status"], "active")
        open(ok, "w").close()
        again = self.serve("openai")
        r = self.code("--resume", "--model", "fake")
        self.assertEqual(self.task()["status"], "done", r.stdout + r.stderr)
        self.assertEqual(again.seen, [], "the step was done: no model call on resume")


class RunHooks(CodeCase):  # T-1351
    def on(self, **cmds):
        with open(os.path.join(self.home, "code.toml"), "w") as f:
            f.write("[on]\n" + "".join(f'{k} = {json.dumps(v)}\n' for k, v in cmds.items()))

    def test_a_closed_run_hands_its_event_to_the_on_closed_command(self):
        got = os.path.join(self.tmp, "got.json")
        self.on(closed=f"cat > {got}", **{"needs-you": f"touch {self.tmp}/wrong"})
        self.serve("openai", plan(), *FIX)
        r = self.code("fix add", "--model", "fake")
        self.assertTrue(os.path.exists(got), r.stdout + r.stderr)
        with open(got) as f:
            e = json.load(f)
        self.assertEqual((e["event"], e["task"]), ("closed", "T-0001"))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "wrong")))
        self.assertRegex(self.fm("state", "--line").stdout, r"code T-0001 closed\s*$")

    def test_a_block_runs_blocked_and_needs_you_and_a_refused_or_failing_one_is_a_note(self):
        self.on(blocked=f"touch {self.tmp}/blocked; exit 3", **{"needs-you": "rm -rf ~"})
        self.serve("openai", plan(), NOT_A_FIX, *[call("done", summary="nothing") for _ in range(4)])
        r = self.code("fix add", "--model", "fake", check=False)
        self.assertEqual(r.returncode, 1)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "blocked")))
        self.assertIn("on blocked: exit 3", r.stdout + r.stderr)
        self.assertIn("on needs-you: not run", r.stdout + r.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.fake_home, "keep")), "the guard stopped it")
        self.assertRegex(self.fm("state", "--line").stdout, r"code T-0001 blocked\s*$")

    def test_the_state_line_tells_a_live_run_from_one_a_kill_stopped(self):
        import fcntl
        from sampo import core
        self.fm("init")
        p = core.find_project(self.repo)
        d = os.path.join(p.dir, "code")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "T-0009.events.jsonl"), "w") as f:
            f.write(json.dumps({"event": "start", "request": "x"}) + "\n")
        self.assertEqual(core.code_line(p), "code T-0009 stopped")
        with open(os.path.join(d, "T-0009.lock"), "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            self.assertEqual(core.code_line(p), "code T-0009 running")


class Cost(CodeCase):  # T-1357
    def priced(self, per_m=10000):  # $0.01 a token: a fake call (5 in, 2 out) costs $0.07
        path = os.path.join(self.home, "models.json")
        conf = read_json(path)
        conf["profiles"]["fake"]["price"] = {"in": per_m, "out": per_m}
        with open(path, "w") as fh:
            json.dump(conf, fh)

    def spent(self):
        path = os.path.join(self.home, "state", "spend.jsonl")
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return [e for e in map(json.loads, f) if e["feature"] == "fm code"]

    def closed(self, r):
        return [json.loads(line) for line in r.stdout.splitlines()][-1]

    def test_each_priced_call_goes_to_the_ledger_and_the_close_line_shows_the_total(self):
        self.serve("openai", plan(), *FIX)
        self.priced()
        closed = self.closed(self.code("fix add", "--model", "fake", "--json"))
        rows = self.spent()
        self.assertEqual([round(e["usd"], 4) for e in rows], [0.07] * 3)  # the plan, the edit, done: a priced profile is judged nothing (T-4362)
        self.assertEqual({e["project"] for e in rows}, {self.fm_json("status")["project"]})
        self.assertEqual((closed["event"], closed["usd"]), ("closed", 0.21))
        self.assertIn("$0.21", closed["text"])

    def test_an_unpriced_model_records_nothing_and_shows_no_dollars(self):
        self.serve("openai", plan(), *FIX)
        closed = self.closed(self.code("fix add", "--model", "fake", "--json"))
        self.assertEqual(self.spent(), [])
        self.assertNotIn("$", closed["text"])
        self.assertIsNone(closed.get("usd"))

    def test_the_cap_stops_a_run_and_a_resume_keeps_its_spend_and_cap(self):
        self.serve("openai", plan(), call("read", path="calc.py"), call("read", path="calc.py"))
        self.priced()
        r = self.code("fix add", "--model", "fake", "--max-usd", "0.15", check=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("spent $0.21 of its $0.15 cap", r.stderr)
        self.assertEqual(r.stderr.strip().splitlines()[-1], "Next: fm code --resume T-0001 --max-usd 0.3")
        self.assertEqual(self.task()["status"], "active")
        r = self.code("--resume", check=False)  # the cap came back from the journal, and $0.21 is past it
        self.assertIn("spent $0.21 of its $0.15 cap", r.stderr)
        self.serve("openai", *FIX)
        self.priced()  # serve wrote the profile afresh
        closed = self.closed(self.code("--resume", "--max-usd", "1", "--json"))
        self.assertEqual(self.task()["status"], "done")
        self.assertEqual(closed["usd"], 0.35)


class Failures(CodeCase):  # T-1754 (T-1334, T-1355)
    def test_every_failure_ends_with_one_next_line(self):
        last = lambda r: r.stderr.strip().splitlines()[-1]  # noqa: E731
        self.assertEqual(last(self.code("fix it", "--model", "nope", check=False)), "Next: fm code models")
        self.assertEqual(last(self.code(check=False)), 'Next: fm code "<what to do>"')
        self.serve("openai", plan(), call("read", path="calc.py"))  # the third call runs past the script: a 400
        r = self.code("fix add", "--model", "fake", "--json", check=False)
        self.assertEqual(last(r), "Next: fm code --resume T-0001")
        end = [json.loads(line) for line in r.stdout.splitlines()][-1]
        self.assertEqual((end["event"], end["next"]), ("error", "fm code --resume T-0001"))
        self.serve("openai", plan(), NOT_A_FIX, *[call("done", summary="nothing") for _ in range(4)])
        r = self.code("fix add", "--model", "fake", "--json", check=False)
        self.assertEqual(last(r), "Next: fm task show T-0002")
        self.assertEqual([json.loads(line) for line in r.stdout.splitlines()][-1]["next"], "fm task show T-0002")

    def test_the_start_event_names_the_foreman_running_it(self):
        self.serve("openai", plan(), *FIX)
        start = json.loads(self.code("fix add", "--model", "fake", "--json").stdout.splitlines()[0])
        with open(os.path.join(PLUGIN, ".claude-plugin", "plugin.json")) as f:
            version = json.load(f)["version"]
        self.assertEqual(start["foreman"], {"root": PLUGIN, "version": version})

    def test_two_installs_or_a_doubled_hook_are_said_once_at_start(self):  # T-1757
        import unittest.mock
        from sampo import smith
        from sampo import doctor
        warn = doctor.Result("installs", "WARN", "two Foreman installs: 1.2.0 from x and 1.2.50 in y")
        ok = doctor.Result("hook state writers", "PASS", "fine")
        with unittest.mock.patch.object(doctor, "check_dual_install", return_value=warn), \
                unittest.mock.patch.object(doctor, "check_hook_writers", return_value=ok):
            first, again = smith.install_warning(), smith.install_warning()
        self.assertEqual(first, "fm doctor: two Foreman installs: 1.2.0 from x and 1.2.50 in y")
        self.assertIsNone(again, "said once")
        with unittest.mock.patch.object(doctor, "check_dual_install", return_value=ok), \
                unittest.mock.patch.object(doctor, "check_hook_writers", return_value=ok):
            self.assertIsNone(smith.install_warning())

    def test_the_start_event_names_the_runs_containment(self):  # T-1760 (T-1402)
        self.serve("openai", plan(), *FIX)
        start = json.loads(self.code("fix add", "--model", "fake", "--json",
                                     env={"FOREMAN_CONFINE": "0"}).stdout.splitlines()[0])
        self.assertEqual(start["containment"], {"kind": "guard", "label": "guard only"})


def cmdline(pid):  # T-1902: ps where there's no /proc (macOS CI never saw the runner)
    from sampo import ideas
    try:
        return b"\0".join(ideas._cmdline(pid))
    except OSError:
        return b""


def alive(pid):  # running, not a zombie: /proc's state, else ps's
    from sampo import ideas
    try:
        if os.path.isdir("/proc"):
            with open(f"/proc/{pid}/stat") as f:
                return f.read().rsplit(")", 1)[1].split()[0] not in "ZX"
        return not ideas._ps(pid, "stat").startswith("Z")
    except OSError:
        return False


class Survivors(CodeCase):  # T-1754 (T-1337)
    def test_a_runner_a_hard_kill_left_is_stopped_by_the_next_start(self):
        self.serve("openai", plan(), call("run", cmd="sleep 120"))
        reg = os.path.join(self.home, "state", "code-procs")
        p = subprocess.Popen([sys.executable, FM, "code", "fix add", "--model", "fake"], cwd=self.repo, env=self.env(),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(1200):  # the run tool's runner, not the baseline check that runs before it
                names = [n for n in os.listdir(reg) if n.isdigit()] if os.path.isdir(reg) else []
                names = [n for n in names if b"sleep" in cmdline(n)]
                if names:
                    break
                time.sleep(0.05)
            self.assertTrue(names, "the runner was never registered")
            pid = int(names[0])
        finally:
            p.kill()
            p.wait(30)
        self.assertTrue(alive(pid), "the hard kill left the runner going")
        r = self.code("fix it", "--model", "nope", check=False)
        self.assertIn(f"stopped 1 runner(s) a killed fm code left running (pid {pid})", r.stdout + r.stderr)
        for _ in range(100):
            if not alive(pid):
                break
            time.sleep(0.05)
        self.assertFalse(alive(pid))
        self.assertEqual(os.listdir(reg), [])

    def test_a_live_runs_runner_and_a_reused_pid_are_left_alone(self):
        from sampo import smith
        from sampo import ideas
        live = subprocess.Popen(["sleep", "60"], start_new_session=True)
        other = subprocess.Popen(["sleep", "60"], start_new_session=True)
        try:
            me = os.getpid()
            smith._register(live.pid)  # its owner (this test) is alive
            os.makedirs(os.path.join(self.home, "state", "code-procs"), exist_ok=True)
            with open(os.path.join(self.home, "state", "code-procs", str(other.pid)), "w") as f:
                f.write(f"1 {me} 1")  # an owner and runner that aren't the processes these pids now are
            for garbled in ("0", "1"):  # killpg(0) would be this test's own group
                with open(os.path.join(self.home, "state", "code-procs", garbled), "w") as f:
                    f.write("0 999999999 0")
            self.assertEqual(smith.stop_survivors(), [])
            self.assertTrue(alive(live.pid) and alive(other.pid))
            self.assertEqual(os.listdir(os.path.join(self.home, "state", "code-procs")), [str(live.pid)])
            self.assertEqual(ideas._starttime(me), open(os.path.join(  # noqa: SIM115
                self.home, "state", "code-procs", str(live.pid))).read().split()[2])
        finally:
            for x in (live, other):
                x.kill()
                x.wait()


FEAT = dict(PLAN, type="FEATURE")  # T-2739: a FEATURE close needs no red-green proof, so the seeds can be quick


class OutcomeEscalation(CodeCase):
    """T-2739: each close records the harness that made it; a type and tier whose fm code closes keep needing later
    fixes starts on the --escalate model, and the journal says why."""

    def seed(self, n, bad, harness="fm-code"):
        """n closed FEATURE S tasks made under `harness`, their 30-day windows over, the first `bad` fixed later."""
        import re
        from sampo import core
        p = core.find_project(self.repo)
        old = core.iso(time.time() - 31 * 86400)
        ids = []
        for i in range(n):
            self.fm("task", "new", f"Seed {i}", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true", "--step", "s",
                    "--focus", env={"FOREMAN_HARNESS": harness})
            tid = core.load_briefs(p)[-1].id
            self.fm("task", "finish", tid, "--audit", "self", "--run", "true", env={"FOREMAN_HARNESS": harness})
            path = core.find_brief(p, tid).path
            text = read_text(path)
            text = re.sub(r"(?m)^updated: .*$", f"updated: {old}", text)
            text = re.sub(r"(?m)^- \d{4}-\d\d-\d\dT[\d:]+Z done$", f"- {old} done", text)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            ids.append(tid)
        for tid in ids[:bad]:
            self.fm("capture", f"Crash in what {tid} added", "--type", "FIX", "--tier", "S")
        return ids

    def run_fix(self, *flags):
        """The base model plans, then (if it still has the work) does nothing; the strong one fixes calc.py."""
        self.serve("anthropic", plan(FEAT), call("done", summary="nothing"), name="fake")
        strong = self.serve("anthropic", *FIX, name="strong")
        return self.code("fix add", "--model", "fake", *flags), strong

    def test_a_close_records_the_harness_that_made_it(self):
        from sampo import core
        self.serve("anthropic", plan(), *FIX, name="fake")
        self.code("fix add", "--model", "fake")
        done = [e for e in core.ledger_events(core.find_project(self.repo), ("task_done",)) if e["task"] == "T-0001"]
        self.assertEqual([(e.get("data") or {}).get("harness") for e in done], ["fm-code"])

    def test_five_settled_closes_with_30_percent_fixed_later_start_on_the_escalate_model(self):
        self.seed(5, 2)  # 2 of 5 = 40%
        r, strong = self.run_fix("--escalate", "strong")
        tid = "T-0008"  # five seeds, the two captured fixes, then fm code's own
        self.assertEqual(self.task(tid)["status"], "done", r.stdout)
        self.assertEqual(len(strong.seen), 2, "the work ran on the escalate model")
        self.assertIn("2 of 5", r.stdout)
        self.assertIn('"kind": "escalate"', journal_text(self, tid))

    def under_the_bar(self, n, bad, harness):
        tid = f"T-{n + bad + 1:04d}"
        self.seed(n, bad, harness)
        self.serve("anthropic", plan(FEAT), *FIX, name="fake")
        strong = self.serve("anthropic", call("done", summary="x"), name="strong")
        r = self.code("fix add", "--model", "fake", "--escalate", "strong")
        self.assertEqual(self.task(tid)["status"], "done", r.stdout)
        self.assertEqual(strong.seen, [], "the base model did the work")
        self.assertNotIn("kind\": \"escalate", journal_text(self, tid))

    def test_under_30_percent_it_starts_on_the_base_model(self):
        self.under_the_bar(5, 1, "fm-code")

    def test_under_5_settled_closes_it_starts_on_the_base_model(self):
        self.under_the_bar(4, 4, "fm-code")

    def test_closes_made_under_another_harness_do_not_count(self):
        self.under_the_bar(5, 5, "claude-code")

    def test_with_no_escalate_model_set_the_journal_says_the_record_calls_for_one(self):
        self.seed(5, 2)
        self.serve("anthropic", plan(FEAT), *FIX, name="fake")
        r = self.code("fix add", "--model", "fake")
        self.assertEqual(self.task("T-0008")["status"], "done", r.stdout)
        self.assertIn("none is set", r.stdout)


class TrueRepeat(CodeCase):
    """T-4357: a repeat is the same action with the same arguments while nothing it could see has changed since it was
    last sent. An edit of one file, a run of one command and a read of one path are no repeat when the arguments, an
    applied edit or the output between them differ."""
    RUN = call("run", cmd="python3 test_calc.py")

    def go(self, *script):
        f = self.serve("anthropic", plan(), *script, name="small")
        r = self.code("fix add", "--model", "small", "--json", check=False)
        evs = [json.loads(x) for x in r.stdout.splitlines() if x.startswith("{")]
        return f, r, evs, journal_text(self).count('"loop_break"')

    def test_true_repeat_edit_run_cycles_of_one_file_are_no_loop(self):
        e = [call("edit", file="calc.py", old=o, new=n) for o, n in (("a - b", "a * b"), ("a * b", "a / b"),
                                                                    ("a / b", "a + b"))]
        f, r, evs, breaks = self.go(e[0], self.RUN, e[1], self.RUN, e[2], self.RUN)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(breaks, 0, "three edits and three runs, each pair with a change between")
        self.assertNotIn("same action", json.dumps(f.seen))
        self.assertIn("return a + b", self.calc())

    def test_true_repeat_three_reads_of_different_ranges_are_no_loop(self):
        reads = [call("read", path="calc.py", start=1, lines=1), call("read", path="calc.py", start=2, lines=1),
                 call("read", path="calc.py", start=1, lines=2)]
        f, r, evs, breaks = self.go(*reads, *FIX)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(breaks, 0)
        self.assertNotIn("same action", json.dumps(f.seen))

    def test_true_repeat_the_same_run_with_the_same_output_names_what_was_the_same(self):
        f, r, evs, breaks = self.go(self.RUN, self.RUN, self.RUN, *FIX)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(breaks, 1, [(e["action"], e["key"], e["fresh"], e["detail"][-80:]) for e in evs if e["event"] == "action"])
        note = f.said(4)  # the plan, three runs, then the turn that is told
        self.assertIn("same action", note)
        self.assertIn("python3 test_calc.py", note)
        self.assertIn("turns 1, 2 and 3", note, "the turns it was sent")
        self.assertIn("same exit code", note)
        self.assertIn("Its last result ended", note, "the failing output's last lines, the fact the model lacked")
        self.assertIn("Traceback", note)  # test_calc.py fails on the unfixed calc.py: its output is quoted
        self.assertIn("AssertionError", note)

    def test_true_repeat_a_failing_run_whose_output_differs_only_by_noise_is_still_a_loop(self):
        # pytest's "failed in 0.03s", a tmp-path counter, a timestamp: the same command, the same exit code and no change
        # to the tree is a repeat whatever the bytes it printed (the review of T-4357)
        run = call("run", cmd="python3 -c \"import time; print(time.time_ns()); raise SystemExit(1)\"")
        f, r, evs, breaks = self.go(run, run, run, *FIX)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(breaks, 1, [(e["action"], e["fresh"]) for e in evs if e["event"] == "action"])
        self.assertIn("same exit code", f.said(4))

    def test_true_repeat_an_edit_between_two_sends_of_a_failing_run_is_no_loop(self):
        fail = call("run", cmd="python3 -c \"print('t=0.01s'); raise SystemExit(1)\"")
        f, r, evs, breaks = self.go(fail, fail, call("edit", file="calc.py", old="a - b", new="a + b"), fail, *FIX)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(breaks, 0, "two sends, an applied edit, one send: never three with nothing between")

    def fake(self, trees, results):
        """A Run whose act() and tree() are scripted, to drive do() without a model: trees[i] is the tree after action i."""
        from sampo import smith as s
        seq = iter(results)
        class Fake(s.Run):
            guided = False  # a class attribute over Run's property: a cloud profile, so a run is judged by the tree

            def __init__(me):
                me.root, me.turns, me.last_act, me.code = os.path.realpath(self.repo), 0, None, None
                me.sent, me.third, me.visited, me.world, me.tree_now = {}, [], set(), None, trees[0]
                me.events = []
            def act(me, name, a):
                me.turns += 1
                me.tree_now = trees[min(me.turns, len(trees) - 1)]
                return next(seq), False
            def tree(me):
                return me.tree_now
            def redact(me, t):
                return t
            def emit(me, event, text, quiet=False, **d):
                me.events.append(d)
            def log(me, **d):
                pass
            def offered(me):
                return [{"name": "fixcard"}]
        return Fake()

    def test_true_repeat_a_card_that_changes_the_tree_clears_the_count(self):
        # read, an applied card, read, read: the card changed files between the first read and the rest
        read = ("read", {"path": "calc.py"})
        card = ("card", {"name": "fixcard", "inputs": {}})
        changed = self.fake(["t0", "t0", "t1", "t1", "t1"], ["text", "applied", "text", "text"])
        for name, a in (read, card, read, read):
            changed.do(name, a)
        self.assertEqual(changed.third, [], "the card changed the tree: the later reads are new")
        same = self.fake(["t0"] * 5, ["text", "no change", "text", "text"])
        for name, a in (read, card, read, read):
            same.do(name, a)
        self.assertEqual(len(same.third), 1, "a card that changed nothing is no change")
        own = self.fake(["t0", "t0", "t1", "t1", "t1"], ["text", "applied", "text", "text"])  # called by its own name
        for name, a in (read, ("fixcard", {}), read, read):
            own.do(name, a)
        self.assertEqual(own.third, [])
        self.assertEqual([e["fresh"] for e in changed.events], [True, True, True, False])

    def test_true_repeat_an_edit_and_its_exact_reverse_forever_is_a_loop(self):
        fwd = call("edit", file="calc.py", old="a - b", new="a + b")
        back = call("edit", file="calc.py", old="a + b", new="a - b")
        f, r, evs, breaks = self.go(fwd, back, fwd, back, fwd, back, call("done", summary="x"))
        self.assertGreaterEqual(breaks, 1, "A to B, B to A: the third return to a state this step has been in is a repeat")
        self.assertIn("same action", json.dumps(f.seen))

    def test_true_repeat_three_different_edits_of_one_file_are_no_loop(self):
        e = [call("edit", file="calc.py", old=o, new=n) for o, n in (("a - b", "a * b"), ("a * b", "a / b"),
                                                                    ("a / b", "a ** b"))]
        f, r, evs, breaks = self.go(*e, call("done", summary="x"))
        self.assertEqual(breaks, 0)

    def test_true_repeat_the_key_is_the_actions_canonical_arguments(self):
        from sampo import smith as s
        run = s.Run.__new__(s.Run)
        run.root = os.path.realpath(self.repo)
        key = lambda name, **a: run.repeat_key(name, a)
        self.assertEqual(key("read", path="calc.py"), key("read", path="./calc.py"))
        self.assertEqual(key("read", path="calc.py"), key("read", path="calc.py", start=1))
        self.assertEqual(key("read", path="calc.py"), key("read", path="calc.py", why="to see", thought="x"))
        self.assertEqual(key("run", cmd="python3  test_calc.py"), key("run", cmd="python3 test_calc.py "))
        self.assertNotEqual(key("read", path="calc.py"), key("read", path="calc.py", start=2))
        self.assertNotEqual(key("read", path="calc.py"), key("read", path="test_calc.py"))
        self.assertEqual(key("edit", file="calc.py", old="a", new="b"), key("edit", file="./calc.py", old="a", new="b"))

    def test_true_repeat_two_keys_reaching_three_in_one_turn_are_both_named(self):
        from sampo import smith as s
        run = s.Run.__new__(s.Run)
        run.root = os.path.realpath(self.repo)
        run.sent, run.third, run.turns, run.visited, run.code = {}, [], 0, set(), None
        for i in range(3):
            run.turns = i + 1
            run.repeat("read", {"path": "a.py"}, "x", False)
            run.repeat("read", {"path": "b.py"}, "y", False)
        self.assertEqual(len(run.third), 2, "the first key's third send is not lost to the second's")
        self.assertEqual([e["action"] for e in run.third], ["read", "read"])
        self.assertTrue("a.py" in run.third[0]["args"] and "b.py" in run.third[1]["args"])

    def test_true_repeat_the_same_read_three_times_names_since_which_turn(self):
        read = call("read", path="calc.py")
        f, r, evs, breaks = self.go(read, read, read, *FIX)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(breaks, 1)
        self.assertIn("same action", f.said(4))
        self.assertIn("nothing changed since turn 1", f.said(4))
        self.assertIn("calc.py", f.said(4))
        self.assertIn("The file has not changed since turn 1", f.said(4), "a read's fact: its text is what it was")

    def test_true_repeat_the_same_edit_sent_a_third_time_with_nothing_changed_is_one(self):
        edit = call("edit", file="calc.py", old="a - b", new="a + b")
        f, r, evs, breaks = self.go(edit, edit, edit, call("done", summary="x"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(breaks, 1, "the first applied; the second and third found nothing to change")
        self.assertIn("nothing changed since turn 1", f.said(4))

    def test_true_repeat_an_edit_between_two_reads_of_a_file_clears_the_count(self):
        read = call("read", path="calc.py")
        edit = call("edit", file="calc.py", old="a - b", new="a + b")
        f, r, evs, breaks = self.go(read, read, edit, read, read, call("done", summary="x"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(breaks, 0, "two reads, an edit, two reads: never three with nothing between")

    def test_true_repeat_each_action_event_carries_its_key_and_whether_it_is_new(self):
        read = call("read", path="calc.py")
        f, r, evs, breaks = self.go(read, read, call("read", path="test_calc.py"),
                                    call("edit", file="calc.py", old="a - b", new="a + b"), read, call("done", summary="x"))
        acts = [e for e in evs if e["event"] == "action"]
        self.assertEqual([e["action"] for e in acts], ["read", "read", "read", "edit", "read", "done"])
        self.assertTrue(all(len(e["key"]) == 12 for e in acts))
        self.assertEqual([e["fresh"] for e in acts], [True, False, True, True, True, True],
                         "the second read repeats the first; the read after the edit is new")
        self.assertEqual(acts[0]["key"], acts[1]["key"])
        self.assertNotEqual(acts[0]["key"], acts[2]["key"], "another path is another key")


if __name__ == "__main__":
    import unittest
    unittest.main()
