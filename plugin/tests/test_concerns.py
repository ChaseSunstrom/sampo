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

    def test_proof_a_number_comes_with_its_command_and_the_command_must_print_it(self):
        py = sys.executable
        self.assertFalse(cn.check_proof(self.p, "num:3")[0], "a number with no command")
        self.assertFalse(cn.check_proof(self.p, f"num:4 via {py} -c 'print(3)'")[0], "the command prints another number")
        self.assertFalse(cn.check_proof(self.p, f"num:3 via {py} -c 'raise SystemExit(2)'")[0], "a command that fails")
        cn.show(self.p, self.cid, f"num:3 via {py} -c 'print(3)'")
        self.assertEqual(self.state(), "shown")

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
        path = os.path.join(self.p.dir, "ledger.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(self.capture_line(at(60), "sess-1", "T-9001") + "\n")
            for i in range(5000):  # later lines of other events, all inside the link window
                f.write(json.dumps({"prev": "0" * 64, "ts": at(61 + i // 100), "session_id": "sess-2",
                                    "project": self.p.slug, "task": None, "event": "note", "data": {}}) + "\n")
        got = cn.fold(self.p)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["tasks"], ["T-9001"])

    def test_link_a_capture_outside_the_window_is_not_linked(self):
        t0 = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=2)
        at = lambda s: (t0 + datetime.timedelta(seconds=s)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.book("u1", "why isn't it pushed", at(0), "the work reaches GitHub")
        path = os.path.join(self.p.dir, "ledger.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(self.capture_line(at(cn.LINK_S + 600), "sess-1", "T-9002") + "\n")
        self.assertEqual(cn.fold(self.p)[0]["tasks"], [])
