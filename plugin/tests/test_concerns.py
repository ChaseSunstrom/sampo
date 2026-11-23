"""T-4200: the concerns ledger (T-4188 part A). Concerns are folded from the steer meter's booked rows (a repeat_of chain
is one concern said N times), a task close never closes one, and only a checkable surface proof marks one shown. The
rows here are written as the meter writes them (steers.jsonl); no test calls a model or the network."""
import datetime
import json
import os
import subprocess
import sys
import time
from unittest import mock

from helpers import ForemanTestCase

from sampo import concerns as cn
from sampo import core as c
from sampo import selfcheck


def stamp(hours=0.0, seconds=0.0):
    t = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours, seconds=seconds)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


class Ledger(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)
        cfg = os.path.join(self.tmp, "cfg")
        self.enterContext(mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": cfg}))

    def book(self, uuid, text, at, concern, surface="the push", kind="steer", repeat_of=None, session="sess-1"):
        """One row as steermeter books it (read, not unread)."""
        r = {"session": session, "uuid": uuid, "at": at, "model": "fake", "text": text, "kind": kind,
             "avoidable": True, "root": "R7", "missed_thought": "check the live page", "why": "it said done",
             "concern": concern, "surface": surface}
        if repeat_of is not None:
            r["repeat_of"] = repeat_of
        c.append_line(os.path.join(self.p.dir, "steers.jsonl"), json.dumps(r, ensure_ascii=False))
        return at

    def task(self, title="publish it"):
        return self.fm("capture", title, "--type", "FEATURE", "--tier", "S", "--json").stdout

    def capture_from(self, session, tid):
        """The capture ledger event sampo capture writes for a task (session and time are what a link needs)."""
        c.log_event(self.p, "capture", task=tid, data={"source": "user", "type": "FEATURE"}, session=session)

    def ids(self, rows):
        return [x["id"] for x in rows]


class Fold(Ledger):
    def test_fold_a_repeat_chain_is_one_concern_said_three_times(self):
        first = self.book("u1", "why isn't it pushed", stamp(hours=5), "the work reaches GitHub")
        self.book("u2", "it still isn't pushed", stamp(hours=3), "the work reaches GitHub",
                  repeat_of="why isn't it pushed")
        last = self.book("u3", "where can I see it", stamp(hours=1), "the work reaches GitHub",
                         repeat_of="it still isn't pushed")
        self.book("u4", "the login page is blank", stamp(hours=2), "the login page works", surface="login page")
        got = cn.fold(self.p)
        self.assertEqual(len(got), 2, "one chain and one unrelated ask")
        one = next(x for x in got if x["text"] == "the work reaches GitHub")
        self.assertEqual(one["said"], 3)
        self.assertEqual(one["opened"], first)
        self.assertEqual(one["last_said"], last)
        self.assertEqual(one["state"], "open")
        self.assertEqual(one["id"], cn.concern_id("sess-1", "u1"), "the id is the first message's, stable")
        self.assertEqual(one["tasks"], [])

    def test_fold_a_repeat_of_nothing_earlier_starts_its_own_concern(self):
        self.book("u1", "a fresh ask", stamp(hours=3), "the page loads")
        self.book("u2", "a line never said", stamp(hours=2), "the cache works", repeat_of="a message never sent")
        self.assertEqual(len(cn.fold(self.p)), 2)

    def test_fold_a_task_close_does_not_close_the_concern(self):
        self.book("u1", "why isn't it pushed", stamp(seconds=120), "the work reaches GitHub")  # captured just after
        tid = self.task()
        self.capture_from("sess-1", json.loads(tid)["id"])
        got = cn.fold(self.p)
        self.assertEqual(len(got[0]["tasks"]), 1, "the capture from that message links the task")
        b = c.find_brief(self.p, json.loads(tid)["id"])
        b.meta["status"] = "done"
        c.save_brief(self.p, b)
        after = cn.fold(self.p)
        self.assertEqual(after[0]["state"], "open", "a closed task leaves the concern open")
        self.assertEqual(after[0]["tasks"], got[0]["tasks"])

    def test_fold_a_task_that_names_the_concern_id_links_to_it(self):
        self.book("u1", "where can I see it", stamp(hours=2), "the login page works", surface="login page")
        cid = cn.concern_id("sess-1", "u1")
        self.task(f"show the login page ({cid})")
        self.assertEqual(len(cn.fold(self.p)[0]["tasks"]), 1)

    def test_fold_an_empty_book_is_an_empty_list_and_a_read_writes_nothing(self):
        self.assertEqual(cn.fold(self.p), [])
        self.book("u1", "why isn't it pushed", stamp(hours=3), "the work reaches GitHub")
        cn.fold(self.p)
        cn.fold(self.p)
        self.assertFalse(os.path.exists(os.path.join(self.p.dir, cn.CONCERNS)), "fold only reads")

    def test_fold_answer_takes_a_pure_question_out_of_the_open_list(self):
        self.book("u1", "what does shown mean here", stamp(hours=3), "the meaning of shown")
        cid = cn.fold(self.p)[0]["id"]
        cn.answer(self.p, cid, "a concern is shown once its proof checks out")
        got = cn.fold(self.p)[0]
        self.assertEqual(got["state"], "answered")
        self.assertEqual(cn.open_concerns(self.p), [])

    def test_fold_split_cuts_the_links_between_unrelated_asks(self):
        self.book("u1", "why isn't it pushed", stamp(hours=4), "the work reaches GitHub")
        self.book("u2", "the login page is blank", stamp(hours=3), "the login page works", surface="login page",
                  repeat_of="why isn't it pushed")  # the model linked two unrelated asks
        self.assertEqual(len(cn.fold(self.p)), 1)
        made = cn.split(self.p, cn.fold(self.p)[0]["id"])
        self.assertEqual(len(made["into"]), 2)
        self.assertEqual(sorted(x["text"] for x in cn.fold(self.p)),
                         ["the login page works", "the work reaches GitHub"])

    def test_fold_asks_waiting_reads_the_concerns_when_any_exist(self):
        self.book("u1", "why isn't it pushed", stamp(hours=3), "the work reaches GitHub")
        self.book("u2", "why still not", stamp(hours=2), "the work reaches GitHub", repeat_of="why isn't it pushed")
        hit = selfcheck.asks_waiting(self.p, c.load_briefs(self.p), time.time())
        self.assertTrue(hit.startswith("1 of the user's concerns from the last 3 days aren't shown; oldest: "), hit)
        self.assertIn(cn.concern_id("sess-1", "u1"), hit)

    def test_fold_the_verb_lists_open_concerns_and_json_matches(self):
        self.book("u1", "why isn't it pushed", stamp(hours=3), "the work reaches GitHub")
        got = self.fm_json("concerns")
        self.assertEqual(self.ids(got["concerns"]), [cn.concern_id("sess-1", "u1")])
        self.assertIn("said 1", self.fm("concerns").stdout)
        refused = self.fm("concerns", "show", cn.concern_id("sess-1", "u1"), "--proof", "it works now", check=False)
        self.assertNotEqual(refused.returncode, 0, "free text is no proof: the verb refuses it")
        self.assertIn("git:", refused.stderr + refused.stdout)


class Proof(Ledger):
    def setUp(self):
        super().setUp()
        self.book("u1", "why isn't it pushed", stamp(hours=3), "the work reaches GitHub")
        self.cid = cn.concern_id("sess-1", "u1")

    def state(self):
        return next(x for x in cn.fold(self.p) if x["id"] == self.cid)["state"]

    def test_proof_free_text_is_refused_and_the_concern_stays_open(self):
        ok, why = cn.check_proof(self.p, "it works now")
        self.assertFalse(ok)
        self.assertIn("git:", why)
        with self.assertRaises(ValueError):
            cn.show(self.p, self.cid, "it works now")
        self.assertEqual(self.state(), "open")

    def test_proof_a_path_that_exists_shows_it_and_a_missing_one_is_refused(self):
        with open(os.path.join(self.repo, "page.html"), "w") as f:
            f.write("<p>pushed</p>")
        self.assertEqual(cn.check_proof(self.p, "path:page.html")[0], True)
        ok, why = cn.check_proof(self.p, "path:no/such/page.html")
        self.assertFalse(ok)
        self.assertIn("no such path", why)
        cn.show(self.p, self.cid, "path:page.html")
        self.assertEqual(self.state(), "shown")

    def test_proof_a_git_ref_is_checked_against_the_remote_tracking_branch(self):
        ok, why = cn.check_proof(self.p, "git:HEAD")
        self.assertFalse(ok, "no remote-tracking branch holds HEAD yet")
        self.assertIn("remote-tracking", why)
        self.assertFalse(cn.check_proof(self.p, "git:no-such-ref")[0])
        subprocess.run(["git", "-C", self.repo, "update-ref", "refs/remotes/origin/main", "HEAD"], check=True)
        self.assertTrue(cn.check_proof(self.p, "git:HEAD")[0])
        cn.show(self.p, self.cid, "git:HEAD")
        self.assertEqual(self.state(), "shown")

    def test_proof_a_url_must_answer(self):
        with mock.patch.object(cn, "_url_answers", return_value=False):
            self.assertFalse(cn.check_proof(self.p, "url:https://example.com/pushed")[0])
        with mock.patch.object(cn, "_url_answers", return_value=True):
            cn.show(self.p, self.cid, "url:https://example.com/pushed")
        self.assertEqual(self.state(), "shown")

    def surface_script(self, body="import sys\nprint(sys.argv[1])\n"):
        """A script in the project and the project's own surface command for it (meta surface_commands): the only kind of
        command a num proof may run (T-4663)."""
        path = os.path.join(self.repo, "measure.py")
        with open(path, "w") as f:
            f.write(body)
        m = c.read_meta(self.p)
        m["surface_commands"] = [f"{sys.executable} {path}"]
        c.write_meta(self.p, m)
        return f"{sys.executable} {path}"

    def test_proof_a_number_comes_with_its_command_and_the_command_must_print_it(self):
        run = self.surface_script("import sys\nif sys.argv[1] == 'fail':\n    raise SystemExit(2)\nprint(sys.argv[1])\n")
        self.assertFalse(cn.check_proof(self.p, "num:3")[0], "a number with no command")
        self.assertFalse(cn.check_proof(self.p, f"num:4 via {run} 3")[0], "the command prints another number")
        self.assertFalse(cn.check_proof(self.p, f"num:3 via {run} fail")[0], "a command that fails")
        cn.show(self.p, self.cid, f"num:3 via {run} 3")
        self.assertEqual(self.state(), "shown")

    def test_proof_a_number_from_a_command_that_is_no_surface_command_is_refused(self):
        """T-4663: any argv proved only that the number was typed (`python -c 'print(3)'`, `echo 3`)."""
        ok, why = cn.check_proof(self.p, f"num:3 via {sys.executable} -c 'print(3)'")
        self.assertFalse(ok)
        self.assertIn("surface", why)
        self.assertFalse(cn.check_proof(self.p, "num:3 via echo 3")[0])
        self.assertFalse(cn.check_proof(self.p, "num:3 via echo sampo smoke 3")[0],
                         "the surface command's words after another program do not make it a surface command")
        with self.assertRaises(ValueError):
            cn.show(self.p, self.cid, "num:3 via echo 3")
        self.assertEqual(self.state(), "open")

    def test_proof_what_a_surface_command_is(self):
        self.assertTrue(cn._surface_run(self.p, "sampo smoke --json"))
        self.assertTrue(cn._surface_run(self.p, "python3 plugin/bin/sampo shot http://localhost:1/"))
        self.assertTrue(cn._surface_run(self.p, "/usr/bin/curl -s https://example.com/n"))
        self.assertFalse(cn._surface_run(self.p, "python3 -c 'print(1)'"))
        self.assertFalse(cn._surface_run(self.p, "python3 plugin/tests/run.py test_x"))
        self.assertFalse(cn._surface_run(self.p, "sh -c 'sampo smoke'"))
        self.assertFalse(cn._surface_run(self.p, ""))
        self.assertFalse(cn._surface_run(self.p, "'unbalanced"))

    def test_proof_a_number_command_prints_a_bounded_amount(self):
        run = self.surface_script("print('x' * 5000)\nprint(3)\n")
        with mock.patch.object(cn, "OUT_MAX", 1000):
            ok, why = cn.check_proof(self.p, f"num:3 via {run}")
        self.assertFalse(ok)
        self.assertIn("more than", why)
        self.assertTrue(cn.check_proof(self.p, f"num:3 via {run}")[0], "within the cap it is read")

    def test_proof_a_path_outside_the_project_is_refused(self):
        outside = os.path.join(self.tmp, "outside.txt")
        with open(outside, "w") as f:
            f.write("x")
        ok, why = cn.check_proof(self.p, f"path:{outside}")
        self.assertFalse(ok)
        self.assertIn("inside the project", why)
        self.assertFalse(cn.check_proof(self.p, "path:../outside.txt")[0])
        self.assertFalse(cn.check_proof(self.p, "path:/etc/passwd")[0])
        os.symlink(outside, os.path.join(self.repo, "link.txt"))
        self.assertFalse(cn.check_proof(self.p, "path:link.txt")[0], "a link out of the project is outside it")
        inside = os.path.join(self.repo, "page.html")
        with open(inside, "w") as f:
            f.write("ok")
        self.assertTrue(cn.check_proof(self.p, f"path:{inside}")[0], "an absolute path inside the project is fine")

    def test_proof_a_git_ref_says_it_was_not_fetched(self):
        subprocess.run(["git", "-C", self.repo, "update-ref", "refs/remotes/origin/main", "HEAD"], check=True)
        ok, why = cn.check_proof(self.p, "git:HEAD")
        self.assertTrue(ok)
        self.assertIn("not fetched", why, "a remote-tracking ref is what the last fetch saw, not the remote now")

    def test_proof_a_url_follows_no_redirect_to_another_host(self):
        import urllib.request
        h = cn._SameHost()
        req = urllib.request.Request("https://example.com/a")
        self.assertIsNone(h.redirect_request(req, None, 302, "Found", {}, "https://elsewhere.example.org/b"))
        self.assertIsNotNone(h.redirect_request(req, None, 302, "Found", {}, "https://example.com/b"))
        self.assertIsNone(h.redirect_request(req, None, 302, "Found", {}, "http://example.com/b"), "https to http")

    def test_proof_a_closed_task_after_a_shown_concern_leaves_it_shown(self):
        with open(os.path.join(self.repo, "page.html"), "w") as f:
            f.write("ok")
        cn.show(self.p, self.cid, "path:page.html")
        tid = json.loads(self.task())["id"]
        self.capture_from("sess-1", tid)
        b = c.find_brief(self.p, tid)
        b.meta["status"] = "done"
        c.save_brief(self.p, b)
        self.assertEqual(self.state(), "shown")

    def test_proof_said_again_after_it_was_shown_reopens_it(self):
        with open(os.path.join(self.repo, "page.html"), "w") as f:
            f.write("ok")
        cn.show(self.p, self.cid, "path:page.html")
        self.book("u2", "still not pushed", stamp(seconds=-60), "the work reaches GitHub",
                  repeat_of="why isn't it pushed")  # said after the proof was shown
        self.assertEqual(self.state(), "open")

    def test_proof_show_an_unknown_concern_is_refused(self):
        with self.assertRaises(ValueError):
            cn.show(self.p, "K-000000", "path:page.html")


class SurfaceEvidence(ForemanTestCase):
    """T-6023: a step that tells a builder to record `sampo smoke` can never close in a project with no smoke target (the
    command exits 1: nothing to check yet). The advice, the step text and every message that lists the surface commands
    name `sampo smoke` only where a target is set, else a surface run that works here."""

    SMOKE = "sampo smoke"

    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)

    def target(self, smoke):
        meta = c.read_meta(self.p)
        meta["smoke"] = smoke
        c.write_meta(self.p, meta)

    def task_with(self, step=None, touched=None):
        self.made = getattr(self, "made", 0) + 1
        title = f"{'abcdefghij'[self.made % 10]}{self.made} {'zebra quartz mango violin'.split()[self.made % 4]} widget"
        tid = json.loads(self.fm("capture", title, "--type", "FEATURE", "--tier", "S", "--anyway", "--json").stdout)["id"]
        b = c.find_brief(self.p, tid)
        if step:
            b.add_step(step)
            c.save_brief(self.p, b)
        if touched:
            c.log_event(self.p, "touched", task=tid, data={"file": os.path.join(self.repo, touched)})
        return c.find_brief(self.p, tid)

    def advice(self):
        """Every writer of surface-evidence advice, as the text each gives for this project: the paths gap, the
        concern step's gap, the step a repeated concern writes, the proof help and the refusal of a non-surface number."""
        paths = cn.surface_gaps(self.p, self.task_with(touched="web/app.css"))
        steps = cn.surface_gaps(self.p, self.task_with(step=f"{cn.SURFACE_STEP} (K-ABCDEF): look"))
        refused = cn._run_argv(self.p, "echo 3")[1]
        return {"paths gap": paths[0], "step gap": steps[0], "step text": cn._step_text(self.p, {"id": "K-ABCDEF", "surface": "the page"}),
                "proof help": cn.proof_help(self.p), "number refusal": refused}

    def test_no_target_no_writer_names_sampo_smoke_and_each_names_a_surface_command_that_runs(self):
        for name, text in self.advice().items():
            self.assertNotIn(self.SMOKE, text, f"{name}: sampo smoke exits 1 with no target, so the step can never close")
            self.assertIn("sampo shot", text, f"{name} names the surface command that runs here")

    def test_no_target_advice_is_a_command_the_close_accepts(self):
        gap = cn.surface_gaps(self.p, self.task_with(touched="web/app.css"))[0]
        self.assertIn("--run \"sampo shot URL\"", gap)
        self.assertTrue(cn._surface_cmd(self.p, "sampo shot http://localhost:3000/"))

    def test_a_target_of_any_kind_names_sampo_smoke(self):
        for smoke in ({"web": "http://localhost:1/"}, {"http": {"url": "http://localhost:1/", "status": None, "key": None}},
                      {"cmd": {"run": "echo ok", "expect": None}}):
            self.target(smoke)
            self.assertTrue(cn.smoke_target(self.p), smoke)
            for name, text in self.advice().items():
                self.assertIn(self.SMOKE, text, f"{name} with {list(smoke)[0]} target")

    def test_a_target_set_through_the_verb_names_sampo_smoke(self):
        self.fm("smoke", "set", "web", "http://localhost:1/")
        self.assertIn("--run \"sampo smoke\"", cn.surface_gaps(self.p, self.task_with(touched="web/app.css"))[0])

    def test_smoke_off_or_an_empty_target_is_no_target(self):
        for smoke in ({"off": "no URL serves this UI"}, {}, {"web": ""}, {"web": "http://x/", "off": "why"}, "web", None):
            self.target(smoke)
            self.assertFalse(cn.smoke_target(self.p), smoke)
            self.assertNotIn(self.SMOKE, self.advice()["paths gap"], smoke)

    def test_a_listed_project_surface_command_is_named_first(self):
        meta = c.read_meta(self.p)
        meta["surface_commands"] = ["make deploy-staging"]
        c.write_meta(self.p, meta)
        gap = cn.surface_gaps(self.p, self.task_with(touched="web/app.css"))[0]
        self.assertIn("--run 'make deploy-staging'", gap)
        self.assertNotIn(self.SMOKE, gap)
        self.assertTrue(cn._surface_cmd(self.p, "make deploy-staging"))

    def test_a_listed_command_that_runs_sampo_smoke_is_never_named_without_a_target(self):
        """Review: meta surface_commands may list sampo smoke itself; with no target that is the command that exits 1."""
        meta = c.read_meta(self.p)
        meta["surface_commands"] = ["sampo smoke", "python3 plugin/bin/sampo smoke", "fm smoke --profile all",
                                    "env X=1 sampo smoke", "make smoke-site"]
        c.write_meta(self.p, meta)
        for name, text in self.advice().items():
            self.assertNotIn(self.SMOKE, text, name)
            self.assertNotIn("fm smoke", text, name)
        gap = cn.surface_gaps(self.p, self.task_with(touched="web/app.css"))[0]
        self.assertIn("--run 'make smoke-site'", gap, "a listed command that only has smoke in its name is kept")
        meta["surface_commands"] = ["sampo smoke"]
        c.write_meta(self.p, meta)
        self.assertIn("--run \"sampo shot URL\"", cn.surface_gaps(self.p, self.task_with(touched="web/app.css"))[0])
        self.fm("smoke", "set", "web", "http://localhost:1/")
        self.assertIn("--run \"sampo smoke\"", cn.surface_gaps(self.p, self.task_with(touched="web/app.css"))[0],
                      "with a target set it is named again")

    def test_a_listed_command_is_quoted_so_the_shell_runs_it_as_one_word(self):
        import shlex
        for own in ("make deploy-staging; touch /tmp/x", "make $(id) `id` $HOME", "make -d 'a b' \"c\"",
                    "make x '&' '|' '>' \"it's\" #"):
            meta = c.read_meta(self.p)
            meta["surface_commands"] = [own]
            c.write_meta(self.p, meta)
            rec, _ = cn.surface_how(self.p, "T-0001", 1)
            self.assertEqual(shlex.split(rec)[-1], own, "one shell word that is the command, expanded by nothing")
            self.assertEqual(shlex.split(rec)[:-1], ["sampo", "task", "evidence", "T-0001", "--step", "1", "--run"])
        meta["surface_commands"] = ["multi\nline", "tab\tsep\x1b[31m"]
        c.write_meta(self.p, meta)
        self.assertIn('"sampo shot URL"', cn.surface_how(self.p, "T-0001", 1)[0], "a command with a control char is skipped")

    def test_a_target_that_sampo_smoke_cannot_run_is_no_target(self):
        """Review: smoke_target must fail closed against smoke._other_checks, which runs a cmd only with a run string and
        an http check only with a url; each of these exits 1 'nothing to check yet', so none may advise sampo smoke."""
        for smoke in ({"cmd": {"run": "", "expect": None}}, {"cmd": {"run": None}}, {"cmd": {}}, {"cmd": "echo ok"},
                      {"cmd": {"run": ["echo"]}}, {"http": {"url": ""}}, {"http": {}}, {"http": "http://x/"},
                      {"http": {"url": None, "status": 200}}, {"web": None}, {"web": 0}, {"web": ["http://x/"]},
                      {"web": "   "}, {"web": {"url": "http://x/"}}):
            self.target(smoke)
            self.assertFalse(cn.smoke_target(self.p), smoke)
            self.assertNotIn(self.SMOKE, cn.surface_gaps(self.p, self.task_with(touched="web/app.css"))[0], smoke)
        for smoke in ({"cmd": {"run": "echo ok"}}, {"http": {"url": "http://localhost:1/"}}, {"web": "http://localhost:1/"}):
            self.target(smoke)
            self.assertTrue(cn.smoke_target(self.p), smoke)

    def test_every_meta_that_is_no_target_is_one_sampo_smoke_refuses_to_run(self):
        """The two agree: sampo smoke says 'nothing to check yet' exactly where smoke_target is false."""
        for smoke in ({}, {"off": "why"}, {"cmd": {"run": "", "expect": None}}, {"http": {"url": ""}}, {"cmd": {}}):
            self.target(smoke)
            self.assertFalse(cn.smoke_target(self.p), smoke)
            r = self.fm("smoke", check=False)
            self.assertEqual(r.returncode, 1, f"{smoke}: {r.stdout}{r.stderr}")
            self.assertIn("nothing to check yet", r.stderr)
        self.target({"cmd": {"run": "true", "expect": None}})
        self.assertTrue(cn.smoke_target(self.p))
        self.assertEqual(self.fm("smoke", check=False).returncode, 0)

    def test_the_gap_clears_with_the_command_it_named(self):
        b = self.task_with(step=f"{cn.SURFACE_STEP} (K-ABCDEF): look")
        self.assertEqual(len(cn.surface_gaps(self.p, b)), 1)
        self.assertEqual(cn.surface_gaps(self.p, b, then=[(1, "sampo shot http://localhost:1/")]), [])

    def test_a_walker_lists_every_writer_of_sampo_smoke_advice(self):
        """Every string in plugin/lib/sampo that says `sampo smoke`, by file and function: a new writer fails here until
        it is listed with the reason it is safe (project-aware, or not advice) and, if it advises, covered above."""
        import ast
        lib = os.path.join(os.path.dirname(os.path.abspath(cn.__file__)))
        found = set()
        for name in sorted(os.listdir(lib)):
            path = os.path.join(lib, name)
            if not name.endswith(".py"):
                continue
            with open(path, encoding="utf-8") as f:
                text = f.read()
            if "sampo smoke" not in text:
                continue
            tree = ast.parse(text)
            docs = {id(n.body[0].value) for n in ast.walk(tree)
                    if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.body
                    and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}

            def visit(node, where):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    where = node.name
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs \
                        and "sampo smoke" in node.value:
                    found.add((name, where))
                for ch in ast.iter_child_nodes(node):
                    visit(ch, where)
            visit(tree, "<module>")
        # the two that say it, each only where smoke_target is true; advice() above runs every caller of them (the paths
        # gap, the step gap, the repeat's step text, the proof help, the number refusal) for both kinds of project
        covered = {("concerns.py", "surface_how"), ("concerns.py", "surface_list")}
        gated = {("hooks.py", "_drive_push")}  # the drained-queue line says it only with meta smoke web set
        help_text = {("cli.py", "_build_parser")}  # argparse help of the smoke verb and shot: not a step or an advice
        self.assertEqual(found - covered - gated - help_text, set(), "a writer of sampo smoke advice that is not listed")
        self.assertEqual((covered | gated | help_text) - found, set(), "a listed writer that is gone: drop it from the list")


class Link(Ledger):
    """T-4200 review: a task captured from a message links to its concern even when the capture is far back in a
    long ledger. The lines are written as sampo writes them (prev, ts, session_id, project, task, event, data)."""

    def capture_line(self, ts, session, tid):
        rec = {"prev": "0" * 64, "ts": ts, "session_id": session, "project": self.p.slug, "task": tid,
               "event": "capture", "data": {"source": "user", "type": "FEATURE"}}
        return json.dumps(rec)

    def test_link_a_capture_older_than_five_thousand_later_lines_still_links_its_task(self):
        t0 = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=2)
        at = lambda s: (t0 + datetime.timedelta(seconds=s)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.book("u1", "why isn't it pushed", at(0), "the work reaches GitHub")
        tid = json.loads(self.task())["id"]
        path = os.path.join(self.p.dir, "ledger.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(self.capture_line(at(60), "sess-1", tid) + "\n")
            for i in range(5000):  # later lines of other events, all inside the link window
                f.write(json.dumps({"prev": "0" * 64, "ts": at(61 + i // 100), "session_id": "sess-2",
                                    "project": self.p.slug, "task": None, "event": "note", "data": {}}) + "\n")
        got = cn.fold(self.p)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["tasks"], [tid])

    def test_link_only_a_capture_made_from_the_users_words_links(self):
        """T-4663: an agent's own `capture --source discovered` for an unrelated bug, made in the same session within the
        window, is not the user's ask: it must not give the concern a task."""
        self.book("u1", "why isn't it pushed", stamp(seconds=300), "the work reaches GitHub")
        other = json.loads(self.task("an unrelated bug the agent found"))["id"]
        c.log_event(self.p, "capture", task=other, data={"source": "discovered", "type": "FIX"}, session="sess-1")
        self.assertEqual(cn.fold(self.p)[0]["tasks"], [], "a discovered capture is not made from the user's words")
        mine = json.loads(self.task("publish it"))["id"]
        c.log_event(self.p, "capture", task=mine, data={"source": "intake", "type": "FEATURE"}, session="sess-1")
        self.assertEqual(cn.fold(self.p)[0]["tasks"], [mine], "an intake capture is")

    def test_link_a_task_that_names_the_concern_links_whatever_its_source(self):
        self.book("u1", "where can I see it", stamp(hours=2), "the login page works", surface="login page")
        cid = cn.concern_id("sess-1", "u1")
        made = json.loads(self.fm("capture", f"show the login page ({cid})", "--source", "discovered", "--type", "FIX",
                                  "--tier", "S", "--json").stdout)["id"]
        self.assertEqual(cn.fold(self.p)[0]["tasks"], [made])

    def test_link_a_dropped_or_missing_task_counts_as_no_task(self):
        """T-4663: a concern whose only task was dropped (or is not a task any more) stayed open for ever with no hold
        and no place in the order; such a task is no task, so the concern is open for action again."""
        self.book("u1", "why isn't it pushed", stamp(seconds=300), "the work reaches GitHub")
        tid = json.loads(self.task())["id"]
        self.capture_from("sess-1", tid)
        self.capture_from("sess-1", "T-9003")  # a task that is not there
        self.assertEqual(cn.fold(self.p)[0]["tasks"], [tid], "the missing one is not a task; the real one is")
        b = c.find_brief(self.p, tid)
        b.meta["status"] = "dropped"
        c.save_brief(self.p, b)
        self.assertEqual(cn.fold(self.p)[0]["tasks"], [])
        self.assertEqual(cn.queue_links(self.p), {}, "nothing queued is ordered by it")
        b.meta["status"] = "planned"
        c.save_brief(self.p, b)
        self.assertEqual(cn.fold(self.p)[0]["tasks"], [tid], "a task that comes back counts again")

    def test_link_a_k_id_in_the_body_of_an_unrelated_brief_does_not_link_it(self):
        """T-4663: the id names the concern in a brief's first line or on a CONTEXT: line that opens with it (what the
        capture of a repeat writes), not anywhere in the text: a note that quotes an id is not the task for it."""
        self.book("u1", "where can I see it", stamp(hours=2), "the login page works", surface="login page")
        cid = cn.concern_id("sess-1", "u1")
        self.fm("capture", f"an unrelated fix\nthe log line said {cid} once, long ago", "--type", "FIX", "--tier", "S",
                "--source", "discovered")
        self.assertEqual(cn.fold(self.p)[0]["tasks"], [], "a mention in the body is not a link")
        named = json.loads(self.fm("capture", f"show it\nCONTEXT: {cid} reported again on 2026-11-14", "--type", "FIX",
                                   "--tier", "S", "--source", "discovered", "--json").stdout)["id"]
        self.assertEqual(cn.fold(self.p)[0]["tasks"], [named], "a CONTEXT line that opens with the id is a link")

    def test_link_a_capture_outside_the_window_is_not_linked(self):
        t0 = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=2)
        at = lambda s: (t0 + datetime.timedelta(seconds=s)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.book("u1", "why isn't it pushed", at(0), "the work reaches GitHub")
        path = os.path.join(self.p.dir, "ledger.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(self.capture_line(at(cn.LINK_S + 600), "sess-1", "T-9002") + "\n")
        self.assertEqual(cn.fold(self.p)[0]["tasks"], [])
