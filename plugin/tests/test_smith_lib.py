"""T-4443: sampo code offers the library's stack cards to its model, as the Claude Code hooks already do. At the plan the
cards the project's own signals match (lib.suggest) and the ones the request names (lib.cards_named) are offered as a
short list of name and one-line summary; a read or edit of a file offers that file's cards (lib.cards_for), each once a
run; the model opens one with the lib tool, which returns it as fenced, bounded data. A lib event records what was
offered and opened and the stats count both. No extra model call, and a damaged card index never stops a run. The
scripted fake model is test_code's."""
import json
import os
import types
from unittest import mock

from test_code import CLOSE, PLAN, CodeCase, call, plan, read_text, text_action
from test_smith_small import DONE, FIX_EDIT

from sampo import lib as slib
from sampo import smith

API = "from fastapi import FastAPI\n\napp = FastAPI()\n"
FIX_AND_CLOSE = (plan(PLAN), FIX_EDIT, DONE, CLOSE)
OPEN_PYTHON = {"calls": [("lib", {"name": "python"})]}  # (test_code's call() takes the action as its own `name`)


class LibCase(CodeCase):
    def go(self, *script, request="fix add", args=(), env=None, tools=True):
        f = self.serve("openai", *script, tools=tools)
        r = self.code(request, "--model", "fake", "--json", *args, env=env, check=False)
        return f, r, [json.loads(ln) for ln in r.stdout.splitlines() if ln.startswith("{")]

    @staticmethod
    def libs(ev, where=None):
        return [e for e in ev if e["event"] == "lib" and (where is None or e.get("where") == where)]

    @staticmethod
    def stats(ev):
        return [e for e in ev if e["event"] == "stats"][-1]

    @staticmethod
    def tools(f, i):
        return [t["function"]["name"] for t in f.seen[i].get("tools") or []]

    def run_object(self, **kw):
        run = smith.Run(types.SimpleNamespace(), self.repo, os.path.join(self.tmp, "journal"))
        run.request = kw.pop("request", "fix add")
        run.__dict__.update(kw)
        return run


class Offer(LibCase):
    def test_offer_at_plan_names_the_card_the_projects_signals_match(self):
        f, r, ev = self.go(*FIX_AND_CLOSE)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        at_plan = self.libs(ev, "plan")
        self.assertEqual(len(at_plan), 1, "one offer event, before the plan")
        self.assertIn("python", at_plan[0]["offered"], "a .py project matches the python card")
        self.assertEqual(at_plan[0]["opened"], [])
        names = [e["event"] for e in ev]
        self.assertLess(names.index("lib"), names.index("plan"))
        said_plan = f.said(0)
        self.assertIn("Stack cards", said_plan, "the planner is told what the library holds")
        self.assertIn("python: ", said_plan, "by name, then its one-line summary")
        self.assertNotIn("## Verify", said_plan, "names and summaries, never card text")
        self.assertIn("lib", self.tools(f, 1), "the step can open one")
        self.assertIn("Stack cards", f.said(1))

    def test_offer_names_a_card_the_request_itself_names(self):
        f, r, ev = self.go(*FIX_AND_CLOSE, request="fix add, and keep the pytest suite green")
        offered = self.libs(ev, "plan")[0]["offered"]
        self.assertIn("pytest", offered, "named outright in the request")
        self.assertEqual(len(offered), len(set(offered)), "a card matched twice is offered once")
        self.assertLessEqual(len(offered), smith.LIB_OFFER_MAX)

    def test_offer_is_names_and_summaries_only_and_bounded(self):
        f, r, ev = self.go(*FIX_AND_CLOSE)
        run = self.run_object()
        note = run.lib_note()
        self.assertTrue(note)
        lines = [ln for ln in note.splitlines() if ln.startswith("  ")]
        self.assertTrue(0 < len(lines) <= smith.LIB_OFFER_MAX)
        for ln in lines:
            self.assertLessEqual(len(ln), 2 + smith.LIB_SUMMARY_MAX + 60, ln)
        self.assertLess(len(note), 1800, "a few hundred tokens a run")

    def test_offer_reaches_a_cli_worker_as_one_prompt_line(self):
        bin_ = os.path.join(self.tmp, "bin")
        os.makedirs(bin_)
        said = os.path.join(self.tmp, "prompts")
        with open(os.path.join(bin_, "rec"), "w") as f:
            f.write(f"#!/bin/sh\nprintf '%s\\n===\\n' \"$1\" >> {said}\ncase \"$1\" in *'fix round'*) exec python3 -c "
                    "'import pathlib; p = pathlib.Path(\"calc.py\"); p.write_text(p.read_text().replace(\"a - b\", "
                    "\"a + b\"))';; esac\n")
        os.chmod(os.path.join(bin_, "rec"), 0o755)
        self.serve("openai", plan(PLAN))
        self.code("FIX: make calc.add add", "--model", "fake", "--worker", "rec {prompt}", "--no-commit",
                  env={"PATH": bin_ + os.pathsep + os.environ["PATH"]}, check=False)
        prompts = read_text(said).split("\n===\n")[:-1]
        self.assertTrue(prompts)
        for p in prompts:
            self.assertIn("Stack cards", p)
            self.assertIn("python", p)
            self.assertIn("sampo lib show NAME", p, "a worker CLI opens a card with the library's own command")


class NoMatch(LibCase):
    repo_seed = ("seed a project of notes", (("notes.txt", "one line\n"),))

    def test_offer_none_when_no_card_matches(self):
        f, r, ev = self.go(call("edit", file="notes.txt", old="one line", new="two lines"), DONE,
                           request="reword the notes", args=("--verify", "grep -q 'two lines' notes.txt", "--no-commit"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.libs(ev), [], "no card matches: no lib event")
        self.assertNotIn("Stack cards", f.said(0))
        self.assertEqual(self.stats(ev)["cards_offered"], 0)
        self.assertEqual(self.stats(ev)["cards_opened"], 0)


class FileOffer(LibCase):
    repo_seed = ("seed a project with an api file", (
        ("calc.py", "def add(a, b):\n    return a - b\n"),
        ("test_calc.py", "import calc\nassert calc.add(2, 3) == 5, calc.add(2, 3)\nprint('ok')\n"),
        ("requirements.txt", "fastapi\n"),
        ("api.py", API)))

    def test_a_card_the_plan_offered_is_not_offered_again_at_its_file(self):
        f, r, ev = self.go(plan(PLAN), call("read", path="api.py"), call("read", path="api.py"), FIX_EDIT, DONE, CLOSE)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        planned = self.libs(ev, "plan")[0]["offered"]
        self.assertIn("fastapi", planned, "the project's requirements match it")
        offered = [n for e in self.libs(ev, "file") for n in e["offered"]]
        self.assertFalse(set(planned) & set(offered), "an offered card is not offered again at a file")
        self.assertEqual(len(offered), len(set(offered)), "each card is offered once a run")
        self.assertNotIn("Stack cards for api.py", f.said(2))

    def test_file_offer_comes_with_a_read_each_card_once_and_at_most_two(self):
        run = self.run_object()
        run.lib_offer("plan")
        seen = []
        with mock.patch.object(slib, "cards_for", return_value=["pytest", "docker", "git", "pytest"]):
            first = run.lib_file_note(os.path.join(self.repo, "api.py"))
            second = run.lib_file_note(os.path.join(self.repo, "api.py"))
            third = run.lib_file_note(os.path.join(self.repo, "api.py"))
        self.assertIn("Stack cards for api.py", first)
        names = [ln.split(":")[0].strip() for ln in (first + "\n" + second).splitlines() if ln.startswith("  ")]
        self.assertEqual(len(names), len(set(names)), "no card twice")
        self.assertEqual(len([ln for ln in first.splitlines() if ln.startswith("  ")]), smith.LIB_FILE_MAX)
        self.assertEqual(third, "", "everything the file carries was offered by then")
        self.assertIn("pytest", run.lib_state()["offered"])
        self.assertIn("lib", first, "and the way to open one")

    def test_file_offer_is_off_for_a_file_outside_the_project(self):
        run = self.run_object()
        self.assertEqual(run.lib_file_note(os.path.join(self.tmp, "elsewhere.py")), "")
        self.assertEqual(run.lib_file_note(""), "")


class LibTool(LibCase):
    def test_lib_tool_returns_the_card_as_fenced_data(self):
        f, r, ev = self.go(plan(PLAN), OPEN_PYTHON, FIX_EDIT, DONE, CLOSE)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        said = f.said(2)
        self.assertIn("Python baseline", said, "the card's text reaches the model")
        self.assertRegex(said, r"<data-[0-9a-f]{12}>")
        self.assertNotIn("name: python", said, "without its frontmatter")
        opened = self.libs(ev, "open")
        self.assertEqual([e["opened"] for e in opened], [["python"]])

    def test_lib_tool_card_is_bounded(self):
        run = self.run_object()
        text = run.lib_open("python")
        self.assertIn("Python baseline", text)
        self.assertLessEqual(len(text), smith.LIB_CARD_MAX + 200)
        with mock.patch.object(smith, "LIB_CARD_MAX", 120):
            cut = run.lib_open("python")
        self.assertLessEqual(len(cut), 120 + 200)
        self.assertIn("cut", cut.splitlines()[-1], "a cut card says so")

    def test_lib_tool_refuses_a_bad_or_unknown_name_and_names_the_near_one(self):
        run = self.run_object()
        for bad in ("../../etc/passwd", "py thon", "", None, 7):
            self.assertTrue(run.lib_open(bad).startswith("error"), bad)
        self.assertIn("did you mean 'python'", run.lib_open("pythn"))
        self.assertEqual(run.lib_state()["opened"], [], "a refused name opens nothing")

    def test_lib_tool_works_as_a_json_action_for_a_model_without_native_tools(self):
        f, r, ev = self.go(plan(PLAN), {"text": "I'll do this:\n" + json.dumps({"action": "lib", "name": "python"})},
                           text_action("edit", file="calc.py", old="a - b", new="a + b"),
                           text_action("done", summary="fixed"), CLOSE, tools=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('"action": "lib"', f.said(1).replace('\\"', '"'), "the text form is taught")
        self.assertIn("Python baseline", f.said(2))

    def test_lib_tool_is_read_only_so_a_question_may_use_it(self):
        run = self.run_object(read_only=True)
        res, fin = run.act("lib", {"name": "python"})
        self.assertIn("Python baseline", res)
        self.assertFalse(fin)


class Stats(LibCase):
    def test_stats_count_what_was_offered_and_opened(self):
        f, r, ev = self.go(plan(PLAN), OPEN_PYTHON, FIX_EDIT, DONE, CLOSE)
        s = self.stats(ev)
        offered = self.libs(ev, "plan")[0]["offered"]
        self.assertEqual(s["cards_offered"], len(offered))
        self.assertEqual(s["cards_opened"], 1)

    def test_stats_count_an_unopened_offer_as_offered_only(self):
        f, r, ev = self.go(*FIX_AND_CLOSE)
        s = self.stats(ev)
        self.assertGreaterEqual(s["cards_offered"], 1)
        self.assertEqual(s["cards_opened"], 0, "the exam's measure: an offered card nobody opened")

    def test_stats_event_is_documented_in_the_event_table(self):
        self.assertIn("lib", smith.EVENTS)
        for word in ("offered", "opened", "where"):
            self.assertIn(word, smith.EVENTS["lib"])
        for word in ("cards_offered", "cards_opened"):
            self.assertIn(word, smith.EVENTS["stats"])


class Damaged(LibCase):
    def test_damaged_index_never_stops_a_run(self):
        for broken in ("suggest", "cards_named", "cards_for", "cards"):
            with self.subTest(broken=broken), mock.patch.object(slib, broken, side_effect=OSError("index is damaged")):
                run = self.run_object()
                if broken != "cards_for":  # the plan's offer never asks for a file's cards
                    self.assertEqual(run.lib_offer("plan"), [])
                    self.assertEqual(run.lib_note(), "")
                self.assertEqual(run.lib_file_note(os.path.join(self.repo, "calc.py")) if broken == "cards_for" else "",
                                 "")
                self.assertIsInstance(run.lib_open("python"), str, "an answer, never a raise")

    def test_damaged_card_file_is_an_error_to_the_model_not_a_crash(self):
        run = self.run_object()
        with mock.patch.object(smith, "_read_card", side_effect=OSError("unreadable")) if hasattr(smith, "_read_card") \
                else mock.patch("builtins.open", side_effect=OSError("unreadable")):
            res = run.lib_open("python")
        self.assertTrue(res.startswith("error"), res)

    def test_damaged_names_index_on_disk_does_not_stop_a_cli_run(self):
        os.makedirs(os.path.join(self.home, "state"), exist_ok=True)
        for rel in ("state/lib-names.json", "lib-names.json"):
            with open(os.path.join(self.home, rel), "w") as fh:
                fh.write("{not json")
        f, r, ev = self.go(*FIX_AND_CLOSE)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.task()["status"], "done")


class NoExtraCall(LibCase):
    def test_no_extra_call_is_made_for_the_offer(self):
        f, r, ev = self.go(*FIX_AND_CLOSE)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(f.seen), 3, "the plan, the edit and the close: the offer is no call of its own")
        self.assertEqual(self.libs(ev, "plan")[0]["event"], "lib")

    def test_no_extra_call_when_the_model_opens_a_card(self):
        f, r, ev = self.go(plan(PLAN), OPEN_PYTHON, FIX_EDIT, DONE, CLOSE)
        self.assertEqual(len(f.seen), 4, "one turn for the lib call itself, no more")

    def test_no_extra_call_in_process(self):
        run = self.run_object()
        with mock.patch.object(smith, "complete", side_effect=AssertionError("a model call"), create=True):
            run.lib_offer("plan")
            run.lib_note()
            run.lib_open("python")
            run.lib_file_note(os.path.join(self.repo, "calc.py"))
