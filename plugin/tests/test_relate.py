"""fm relate (T-0383): dependencies between open tasks inferred, not typed — an id one task mentions, and a tool-less
child's 'T-B -> T-A: why' and 'group: …' lines — stored as inferred_deps and group, honoured by the queue and the
inbox, and undone by --clear. Runs on its own once a day when 3+ open tasks are new."""
import json
import os
import time

from helpers import ForemanTestCase

STUB = r'''#!/usr/bin/env python3
import json, os, sys
stdin = sys.stdin.read()
with open(os.environ["STUB_LOG"], "a") as f:
    f.write(json.dumps({"args": sys.argv[1:], "stdin": stdin, "ppid": os.getppid()}) + "\n")
if os.environ.get("STUB_EXIT"):
    sys.exit(int(os.environ["STUB_EXIT"]))
print(json.dumps({"result": open(os.environ["STUB_REPLY"]).read(), "total_cost_usd": 0.01}))
'''

REPLY = """T-0001 -> T-0004: the export reads what the parser writes
- T-0007 -> T-0006: the timeout fix needs the client
T-0004 -> T-0001: a cycle, dropped
T-0003 -> T-0042: an unknown id, dropped
T-0003 -> T-0003: a self edge, dropped
group: T-0002 T-0005 — the settings page
"""


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:  # someone else's: a reused pid, so ours is gone
        return False
    return True


class _Stubbed(ForemanTestCase):
    def setUp(self):
        super().setUp()
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir)
        with open(os.path.join(bindir, "claude"), "w") as f:
            f.write(STUB)
        os.chmod(os.path.join(bindir, "claude"), 0o755)
        self.log, reply = os.path.join(self.tmp, "stub.log"), os.path.join(self.tmp, "reply.txt")
        with open(reply, "w") as f:
            f.write(REPLY)
        self.env = {"PATH": bindir + os.pathsep + os.environ["PATH"], "STUB_LOG": self.log, "STUB_REPLY": reply,
                    "CLAUDE_CONFIG_DIR": self.safe_config_dir()}  # T-6020: not under /tmp
        self.fm("init")

    def calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            return [json.loads(x) for x in f]

    def inbox(self):
        return [q["id"] for q in json.loads(self.fm("state", "--json").stdout)["inbox"]]

    def queue(self):
        return [q["id"] for q in json.loads(self.fm("queue", "--json").stdout)["order"]]

    def seven(self, gap=False):
        for n, (title, typ) in enumerate((("export the report as CSV", "FEATURE"), ("settings page layout", "FEATURE"),
                                          ("rename the CLI", "FEATURE"), ("parse the report", "FEATURE"),
                                          ("settings page crashes on save", "FIX"), ("add the API client", "FEATURE"),
                                          ("the API client times out", "FIX"))):
            if gap and n == 1:
                time.sleep(1.1)  # the next capture lands in a later second than T-0001's
            # derived, not the user's asks: those sort by their one-second capture stamp (T-5494), so a run that
            # crosses a second boundary reorders them; derived tasks keep the type order these tests assert
            self.fm("capture", title, "--type", typ, "--tier", "M", "--source", "discovered")
        for tid in ("T-0006", "T-0007"):
            self.fm("task", "set", tid, "status=planned")


class Mentions(_Stubbed):
    def test_an_id_one_task_mentions_goes_first(self):
        self.fm("capture", "export the report as CSV, reading what T-0003 parses", "--tier", "M")
        self.fm("capture", "rename the CLI", "--tier", "M")
        self.fm("capture", "parse the report", "--tier", "M")
        self.assertEqual(self.inbox(), ["T-0001", "T-0002", "T-0003"])
        self.fm("relate", "--no-child", env=self.env)
        self.assertEqual(self.inbox(), ["T-0003", "T-0001", "T-0002"])
        self.assertIn("inferred_deps: [T-0003]", self.fm("task", "show", "T-0001").stdout)
        self.assertEqual(self.calls(), [])  # mentions are free: no child


class Child(_Stubbed):
    def test_child_edges_and_groups_reorder_the_queue_and_inbox(self):
        self.seven()
        self.assertEqual(self.inbox(), ["T-0005", "T-0001", "T-0002", "T-0003", "T-0004"])
        self.assertEqual(self.queue(), ["T-0007", "T-0006"])
        out = self.fm("relate", env=self.env).stdout
        self.assertIn("2 edge", out)
        self.assertEqual(self.inbox(), ["T-0005", "T-0002", "T-0004", "T-0001", "T-0003"])  # a group stays together
        self.assertEqual(self.queue(), ["T-0006", "T-0007"])
        call = self.calls()[0]
        self.assertIn("--tools", call["args"])  # tool-less
        self.assertIn("T-0004 [FEATURE M] Parse the report", call["stdin"])
        self.assertIn("the timeout fix needs the client", self.fm("queue").stdout)  # the reason, shown
        self.assertIn("inferred_deps: [T-0004]", self.fm("task", "show", "T-0001").stdout)
        for tid in ("T-0003", "T-0004"):  # unknown id, self edge and cycle dropped
            self.assertNotIn("inferred_deps", self.fm("task", "show", tid).stdout)
        self.assertIn("group: the settings page", self.fm("task", "show", "T-0005").stdout)

    def test_clear_undoes_it(self):
        self.seven()
        self.fm("relate", env=self.env)
        self.fm("relate", "--clear", env=self.env)
        self.assertEqual(self.inbox(), ["T-0005", "T-0001", "T-0002", "T-0003", "T-0004"])
        self.assertEqual(self.queue(), ["T-0007", "T-0006"])
        for tid in ("T-0001", "T-0005", "T-0007"):
            brief = self.fm("task", "show", tid).stdout
            self.assertNotIn("inferred_", brief)
            self.assertNotIn("group:", brief)

    def test_the_fixture_order_holds_when_its_captures_straddle_a_second(self):
        self.seven(gap=True)  # a loaded machine stamps T-0001 a second before T-0005: the order must not move
        self.assertEqual(self.inbox(), ["T-0005", "T-0001", "T-0002", "T-0003", "T-0004"])

    def test_a_sensitive_project_keeps_its_words_out_of_any_child(self):
        self.seven()
        self.fm("sensitive", "on")
        self.fm("relate", env=self.env)
        self.assertEqual(self.calls(), [])


class Due(_Stubbed):
    def test_once_a_day_and_only_with_three_new_open_tasks(self):
        self.fm("capture", "one", "--tier", "M")
        self.fm("capture", "two", "--tier", "M")
        self.assertIn("not due", self.fm("relate", "--if-due", env=self.env).stdout)
        self.fm("capture", "three", "--tier", "M")
        self.fm("relate", "--if-due", env=self.env)
        self.assertEqual(len(self.calls()), 1)
        for t in ("four", "five", "six"):
            self.fm("capture", t, "--tier", "M")
        self.assertIn("not due", self.fm("relate", "--if-due", env=self.env).stdout)  # once a day
        self.assertEqual(len(self.calls()), 1)
        from sampo import core as c
        from sampo import relate
        meta = c.read_meta(c.find_project(self.repo))
        self.assertFalse(relate.due(meta, ["T-0004", "T-0005", "T-0006"]))  # ran today
        meta["relate_day"] = "2000-01-01"
        self.assertTrue(relate.due(meta, ["T-0004", "T-0005", "T-0006"]))  # tomorrow: three new
        self.assertFalse(relate.due(meta, ["T-0001", "T-0005", "T-0006"]))  # T-0001 was seen

    def wait_for_run(self):
        for _ in range(100):  # detached: the command returned before the run did
            if self.calls() and "inferred_deps" in self.fm("task", "show", "T-0001").stdout:
                break
            time.sleep(0.1)
        else:
            self.fail("no background fm relate ran")
        # T-1284: and until it has exited (the stub's parent): it still writes after T-0001's deps, and a slow
        # machine's cleanup removed the folder under it
        end = time.time() + 30
        while time.time() < end:
            if not any(_alive(call["ppid"]) for call in self.calls()):
                return
            time.sleep(0.1)
        self.fail("the background fm relate never exited")

    def test_session_start_runs_it_in_the_background(self):
        self.seven()
        from sampo import core as c  # only fm relate in the background: the day's review and the canary would race the cleanup
        c.update_meta(c.find_project(self.repo), second_session=c.now()[:10])
        with open(os.path.join(c.state_dir(), "canary.at"), "a"):
            pass
        self.hook("SessionStart", {"source": "startup", "session_id": "new-session"},
                  env=dict(self.env, FOREMAN_NO_BACKGROUND=""))
        self.wait_for_run()
        self.assertEqual(len({x["ppid"] for x in self.calls()}), 1, "a claude call from more than fm relate")

    def test_an_intake_of_three_runs_it_in_the_background(self):
        self.fm("intake", input="FEATURE: export the report as CSV\nFEATURE: settings page layout\n"
                                "FEATURE: rename the CLI\nFEATURE: parse the report\n",
                env=dict(self.env, FOREMAN_NO_BACKGROUND=""))
        self.wait_for_run()


class Review(_Stubbed):
    """The T-0383 review's fixes, one test each."""

    def meta(self):
        from sampo import core as c
        return c.read_meta(c.find_project(self.repo))

    def set_meta(self, **kw):
        from sampo import core as c
        c.update_meta(c.find_project(self.repo), **kw)

    def deps(self, tid):
        show = self.fm("task", "show", tid).stdout
        line = next((x for x in show.splitlines() if x.startswith("inferred_deps:")), "")
        return line.split(":", 1)[1].strip() if line else None

    def planned(self, title, scope):
        self.fm("task", "new", title, "--type", "FEATURE", "--tier", "S", "--scope", scope, "--ac", "ok :: true",
                "--step", "a")

    def test_a_failed_child_leaves_the_day_unclaimed_and_is_logged(self):
        from sampo import core as c
        self.seven()
        self.fm("relate", "--if-due", env=dict(self.env, STUB_EXIT="1"))
        meta = self.meta()
        self.assertNotIn("relate_day", meta)
        self.assertNotIn("relate_claim", meta)
        self.assertTrue(any(e.get("event") == "relate" and "error" in (e.get("data") or {})
                            for e in c.ledger_tail(c.find_project(self.repo), 50)))
        self.fm("relate", "--if-due", env=self.env)  # the next trigger tries again
        self.assertEqual(len(self.calls()), 2)
        self.assertEqual(self.deps("T-0001"), "[T-0004]")

    def test_a_fresh_claim_dedupes_a_concurrent_trigger(self):
        from sampo import core as c
        self.seven()
        self.set_meta(relate_claim=c.now())
        self.assertIn("already running", self.fm("relate", "--if-due", env=self.env).stdout)
        self.assertEqual(self.calls(), [])
        self.set_meta(relate_claim="2000-01-01T00:00:00Z")  # a claim left by a run that died
        self.fm("relate", "--if-due", env=self.env)
        self.assertEqual(len(self.calls()), 1)

    def test_clear_turns_the_automatic_runs_off_until_on(self):
        from sampo import relate
        self.seven()
        self.fm("relate", "--clear", env=self.env)
        self.assertIn("off", self.fm("relate", "--if-due", env=self.env).stdout)
        self.assertEqual(self.calls(), [])
        self.assertFalse(relate.due(self.meta(), ["T-0001", "T-0002", "T-0003"]))
        self.fm("relate", "--on", env=self.env)
        self.fm("relate", "--if-due", env=self.env)
        self.assertEqual(len(self.calls()), 1)

    def test_a_dropped_edge_stays_dropped(self):
        self.seven()
        self.fm("relate", env=self.env)
        self.fm("relate", "--drop", "T-0001", "T-0004", env=self.env)
        self.assertIsNone(self.deps("T-0001"))
        self.fm("relate", env=self.env)
        self.assertIsNone(self.deps("T-0001"))
        self.assertEqual(self.deps("T-0007"), "[T-0006]")

    def test_no_child_and_a_sensitive_project_keep_the_childs_edges(self):
        self.seven()
        self.fm("relate", env=self.env)
        self.fm("relate", "--no-child", env=self.env)
        self.assertEqual(self.deps("T-0001"), "[T-0004]")
        self.fm("sensitive", "on")
        self.fm("relate", env=self.env)
        self.assertEqual(self.deps("T-0001"), "[T-0004]")

    def test_batched_tasks_never_reach_the_child(self):
        self.seven()
        host = json.loads(self.fm("batch", "T-0002", "T-0003", "--json").stdout)["id"]
        self.fm("relate", env=self.env)
        stdin = self.calls()[0]["stdin"]
        self.assertNotIn("T-0002 [", stdin)
        self.assertNotIn("T-0003 [", stdin)
        self.assertIn(f"{host} [", stdin)

    def test_at_most_five_inferred_edges_per_task(self):
        for i in range(8):
            self.fm("capture", f"task {i}", "--tier", "M")
        with open(self.env["STUB_REPLY"], "w") as f:
            f.write("".join(f"T-0001 -> T-{i:04d}: needs it\n" for i in range(2, 9)))
        self.fm("relate", env=self.env)
        self.assertEqual(len(self.deps("T-0001").strip("[]").split(",")), 5)

    def test_fm_run_never_batches_a_task_with_what_it_inferred_it_needs(self):
        from sampo import core as c
        from sampo import serve
        self.planned("base", "a.txt")                 # T-0001
        self.planned("builds on T-0001", "b.txt")     # T-0002: a mention, so an inferred edge
        self.planned("free", "c.txt")                 # T-0003
        self.fm("relate", "--no-child", env=self.env)
        self.assertEqual(self.deps("T-0002"), "[T-0001]")
        p = c.find_project(self.repo)
        self.assertEqual([b.id for b in serve._batch(p, c.find_brief(p, "T-0001"), set(), 3)], ["T-0001", "T-0003"])

    def test_a_cycle_through_an_inferred_edge_names_fm_relate(self):
        self.planned("builds on T-0002", "a.txt")     # T-0001
        self.planned("base", "b.txt")                 # T-0002
        self.fm("relate", "--no-child", env=self.env)
        self.fm("task", "set", "T-0002", "depends_on=T-0001")
        out = self.fm("tidy").stdout
        self.assertIn("fm relate --drop T-0001 T-0002", out)


class Order(ForemanTestCase):
    @staticmethod
    def brief(id, status="planned", priority="normal", inferred=(), group=None):
        from sampo import core as c
        b = c.Brief.new(id, f"task {id}", "FEATURE", "S", now="2026-01-01T00:00:00Z", status=status)
        b.meta.update(priority=priority, inferred_deps=list(inferred))
        if group:
            b.meta["group"] = group
        return b

    def test_an_inferred_edge_never_holds_back_an_urgent_task(self):
        from sampo import core as c
        for status in ("planned", "captured"):
            briefs = [self.brief("T-0001", status, "urgent", inferred=["T-0002"]), self.brief("T-0002", status)]
            q = c.order_queue(briefs)[0] if status == "planned" else c.rank_inbox(briefs)
            self.assertEqual([b.id for b in q], ["T-0001", "T-0002"], status)

    def test_keys_and_dependencies_are_read_once_per_brief(self):
        from unittest import mock
        from sampo import core as c
        briefs = [self.brief(f"T-{i:04d}", inferred=[f"T-{i - 1:04d}"] if i % 3 else [], group=f"g{i % 7}")
                  for i in range(1, 301)]
        inbox = [self.brief(b.id, "captured", inferred=b.meta["inferred_deps"], group=b.meta["group"]) for b in briefs]
        with mock.patch.object(c, "_key", wraps=c._key) as key, mock.patch.object(c, "_deps", wraps=c._deps) as deps:
            q = c.order_queue(briefs)[0]
            self.assertLessEqual(key.call_count, 300)
            self.assertLessEqual(deps.call_count, 300)
            deps.reset_mock()
            r = c.rank_inbox(inbox)
            self.assertLessEqual(deps.call_count, 300)
        for order in ([b.id for b in q], [b.id for b in r]):  # still after what each waits on
            pos = {i: n for n, i in enumerate(order)}
            self.assertTrue(all(pos[f"T-{i - 1:04d}"] < pos[f"T-{i:04d}"] for i in range(2, 301) if i % 3))
