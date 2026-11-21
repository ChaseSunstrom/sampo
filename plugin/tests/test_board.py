"""T-3659: the Board, the live page's modern default: fourteen mechanism panels (nine at T-3659, five more at
T-3731) drawn from Sampo's own state. The server side (sampo.board) collects each panel's facts cheaply and redacted;
the page side (board.js boardModel) maps any snapshot, empty or oversized, to one plain model of fourteen panels, run
here under node."""
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import unittest.mock as mock

from helpers import SampoTestCase, read_text

from sampo import board
from sampo import core as c
from sampo import live

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WEB = os.path.join(ROOT, "plugin", "web", "live")
FIX = os.path.join(ROOT, "plugin", "tests", "fixtures", "live")
PARTS = {"lanes", "train", "cpu", "loop", "library", "commands", "deps", "state", "tests", "bigs", "bigs_more",
         "sessions", "asks", "ship", "usage", "guard"}  # T-3731: the five pictures of the machine and the user's asks
KEYS = ["lanes", "train", "cpu", "loop", "library", "models", "sampo", "state", "tests",
        "asks", "sessions", "ship", "usage", "guard"]


class Brief:
    """Just what the lanes and asks parts read of a brief."""

    def __init__(self, id_, status="active", **meta):
        self.id, self.title, self.status = id_, meta.pop("title", f"Task {id_}"), status
        self.meta = meta

    def steps(self):
        return []

    def section(self, name):
        return self.meta.get("raw", "") if name == "Raw request" else ""


def iso(ago):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - ago))


class Collect(SampoTestCase):
    def setUp(self):
        super().setUp()
        self.fm("task", "new", "Fix /home/alice/notes.md parsing", "--type", "FIX", "--tier", "S",
                "--ac", "it parses :: true", "--step", "parse it", "--focus")
        self.p = c.find_project(self.repo)
        live._CACHE.clear()
        board.cpu_reset()

    def test_every_part_is_there_json_and_without_the_home_folder(self):
        got = board.collect(self.p)
        self.assertEqual(set(got), PARTS)
        text = json.dumps(got)
        self.assertNotIn(os.path.expanduser("~"), text)
        self.assertNotIn("/home/alice", text)
        self.assertIn("~/notes.md", got["loop"]["words"])
        self.assertEqual([s["text"] for s in got["loop"]["steps"]], ["parse it"])
        self.assertEqual(got["loop"]["criteria"][0]["ok"], False)

    def test_a_failing_part_leaves_the_rest(self):
        def boom(p):
            raise RuntimeError("down")
        with mock.patch.dict(board.PARTS, {"lanes": boom}):
            got = board.collect(self.p)
        self.assertIsNone(got["lanes"])
        self.assertIsNotNone(got["state"])

    def test_the_snapshot_carries_the_board(self):
        s = live.snapshot(self.p)
        self.assertEqual(set(s["board"]), PARTS | {"layout"})  # T-3739: the parts and the layout
        json.dumps(s)

    def test_lanes_have_four_states_and_their_leased_files_capped_with_hot_ones_marked(self):
        lane = os.path.join(self.tmp, "lane")
        os.makedirs(os.path.join(lane, "plugin"))
        for i in range(30):
            path = os.path.join(lane, "plugin", f"f{i:02}.py")
            open(path, "w").close()
            os.utime(path, (time.time() - 3600,) * 2)
        os.utime(os.path.join(lane, "plugin", "f00.py"))  # touched just now
        briefs = [Brief("T-1", lane=lane, lane_stop="conflict 2026-11-07T10:00:00Z"),
                  Brief("T-2", lane=lane),
                  Brief("T-3", status="done", lane=lane, lane_stop=f"merged {c.now()}"),
                  Brief("T-4", lane=lane),
                  Brief("T-5", lane=lane, builder_returned=True),
                  Brief("T-6", status="done", lane=lane, updated="2020-01-01T00:00:00Z"),
                  Brief("T-7")]
        leases = {f"k{i}": {"task": "T-4", "project": self.p.slug, "file": f"plugin/f{i:02}.py", "until": time.time() + 60}
                  for i in range(30)}
        leases["other"] = {"task": "T-4", "project": "elsewhere", "file": "x.py", "until": time.time() + 60}
        with mock.patch.object(live, "_briefs", return_value=briefs), \
                mock.patch.object(c, "finished_lanes", return_value=["T-2"]), \
                mock.patch("sampo.bus._leases", return_value=leases):
            got = {x["task"]: x for x in board.part_lanes(self.p)}
        self.assertEqual({k: v["state"] for k, v in got.items()},
                         {"T-1": "parked", "T-2": "waiting", "T-3": "merged", "T-4": "building", "T-5": "waiting"})
        files = got["T-4"]["files"]
        self.assertEqual((len(files), got["T-4"]["more"]), (board.FILES_MAX, 30 - board.FILES_MAX))
        self.assertEqual([f["path"] for f in files if f["hot"]], ["plugin/f00.py"])
        self.assertNotIn("x.py", [f["path"] for f in files])

    def test_the_cpu_ring_splits_the_machine_into_kinds_each_minute(self):
        proc = os.path.join(self.tmp, "proc")
        tick = board.cpu.TICK

        def write(busy, procs):
            os.makedirs(proc, exist_ok=True)
            with open(os.path.join(proc, "stat"), "w") as f:
                f.write(f"cpu  {busy} 0 0 9000 0 0 0 0 0 0\ncpu0 0 0 0 0 0 0 0 0 0 0\n")
            for pid, (comm, ppid, cmd, ut) in procs.items():
                d = os.path.join(proc, str(pid))
                os.makedirs(d, exist_ok=True)
                with open(os.path.join(d, "stat"), "w") as f:
                    f.write(f"{pid} ({comm}) S {ppid} " + "0 " * 9 + f"{ut} 0 0 0 " + "0 " * 4 + f"{1000 + pid} 0 0")
                with open(os.path.join(d, "cmdline"), "w") as f:
                    f.write(cmd.replace(" ", "\0"))
        procs = {10: ("ollama", 1, "ollama serve", 0), 11: ("python3", 1, "python3 plugin/tests/run.py test_x", 0),
                 12: ("claude", 1, "claude --resume", 0), 13: ("python3", 12, "python3 /x/hooks/hook pre", 0)}
        write(0, procs)
        board.cpu_tick(now=6000, proc=proc)
        cores = {10: 1.0, 11: 0.5, 12: 2.0, 13: 0.1}
        write(int(5 * 60 * tick), {k: v[:3] + (int(cores[k] * 60 * tick),) for k, v in procs.items()})
        board.cpu_tick(now=6060, proc=proc)
        got = board.cpu_part()
        self.assertEqual(got["kinds"], list(board.KINDS))
        self.assertEqual(got["goal_pct"], 10)
        now = got["now"]
        for kind, want in (("models", 1.0), ("tests", 0.5), ("claude", 2.0), ("hooks", 0.1), ("other", 1.4), ("busy", 5.0)):
            self.assertAlmostEqual(now[kind], want, places=1, msg=kind)
        self.assertEqual(len(got["minutes"]), 1)
        self.assertEqual(len(got["minutes"][0]), 1 + len(board.KINDS))
        board.cpu_tick(now=6062, proc=proc)  # inside the 10 s throttle: nothing read
        self.assertEqual(board.cpu_part()["now"], now)
        for i in range(40):
            board.cpu_tick(now=6120 + 60 * i, proc=proc)
        self.assertLessEqual(len(board.cpu_part()["minutes"]), board.MINUTES_MAX)
        json.dumps(board.cpu_part())

    def test_commands_are_this_projects_sampo_calls_redacted_newest_twelve(self):
        rows = [{"ts": f"2026-11-07T10:{i:02}:00Z", "kind": "tool", "tool": "Bash", "project": self.p.slug,
                 "target": f"nice -n 19 sampo shot /home/alice/site/p{i}.html", "ok": True} for i in range(20)]
        rows += [{"ts": "2026-11-07T11:00:00Z", "kind": "tool_fail", "tool": "Bash", "project": self.p.slug,
                  "target": "python3 plugin/bin/sampo check --affected", "ok": False},
                 {"ts": "2026-11-07T11:01:00Z", "kind": "tool", "tool": "Bash", "project": "elsewhere", "target": "sampo next"},
                 {"ts": "2026-11-07T11:02:00Z", "kind": "tool", "tool": "Read", "project": self.p.slug, "target": "sampo x"},
                 {"ts": "2026-11-07T11:03:00Z", "kind": "tool", "tool": "Bash", "project": self.p.slug, "target": "ls -la"}]
        with open(os.path.join(c.state_dir(), "events.jsonl"), "a") as f:
            f.write("".join(json.dumps(r) + "\n" for r in rows))
        got = board.part_commands(self.p)
        self.assertEqual(len(got), board.COMMANDS_MAX)
        self.assertEqual(got[-1], {"at": "2026-11-07T11:00:00Z", "cmd": "sampo check --affected", "ok": False})
        self.assertEqual(got[-2]["cmd"], "sampo shot ~/site/p19.html")
        self.assertTrue(got[-2]["ok"])

    def test_tests_are_a_cell_per_module_running_failing_or_cached(self):
        tests = os.path.join(self.repo, "plugin", "tests")
        os.makedirs(tests)
        for name in ("test_a.py", "test_b.py", "test_c.py", "test_d.py", "helpers.py"):
            open(os.path.join(tests, name), "w").close()
        cache = os.path.join(self.tmp, "cache")
        path = os.path.join(cache, "sampo", "run-cache", hashlib.sha1(os.fsencode(self.p.root)).hexdigest()[:16] + ".json")
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as f:
            json.dump({"gen": 1, "paths": [], "classes": {"test_a.K": [["d", 1, []]]}}, f)
        c.log_event(self.p, "check_run", data={"results": [{"cmd": "run.py", "exit": 1, "failing": ["test_b.Foo.test_x"]},
                                                            {"cmd": "lint", "exit": 0}]})
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": cache}), \
                mock.patch.object(board, "_procs", return_value=[(99, "python3 -X faulthandler -c BOOT 0 -q test_c.Bar")]):
            got = board.part_tests(self.p)
        self.assertEqual({m["name"]: m["state"] for m in got["modules"]},
                         {"test_a": "cached", "test_b": "fail", "test_c": "running", "test_d": "idle"})
        self.assertEqual((got["done"], got["total"], got["running"]), (1, 2, 1))

    def test_train_library_state_and_deps_read_the_ledger_and_the_state_folder(self):
        now = time.time()
        ts = lambda ago: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - ago))
        with open(os.path.join(self.p.dir, "ledger.jsonl"), "a") as f:
            for e in ({"ts": ts(1260), "event": "lane_builder", "task": "T-9", "data": {}},
                      {"ts": ts(60), "event": "lane_merge", "task": "T-9", "data": {"train": ["T-9"]}},
                      {"ts": ts(30), "event": "cards_grow", "data": {"kept": 3}},
                      {"ts": ts(20), "event": "moons_grow", "data": {"kept": 5}}):
                f.write(json.dumps(e) + "\n")
        with open(os.path.join(self.p.dir, "decisions.md"), "a") as f:
            f.write("| 2026-11-07 | [costly] Draw the board on canvas | crisp at /home/alice/x | SVG |\n")
        train = board.part_train(self.p)
        self.assertEqual((train["landed_24h"], train["trains_24h"], train["minutes_per_lane"]), (1, 1, 20))
        self.assertEqual(train["landed"][-1]["task"], "T-9")
        with mock.patch("sampo.callcards.cards", return_value=({"a": {}, "b": {}}, {})):
            lib = board.part_library(self.p)
        self.assertEqual((lib["calling"], lib["hourly"][-1], len(lib["hourly"])), (2, 8, 24))
        self.assertEqual([r["grid"] for r in lib["recent"]], ["cards", "moons"])
        st = board.part_state(self.p)
        self.assertIn("tasks", [e["name"] for e in st["tree"]])
        self.assertTrue(all("/" not in e["name"] for e in st["tree"]))
        self.assertEqual((st["doc"]["title"], st["doc"]["text"]), ("Draw the board on canvas", "crisp at ~/x"))
        a = c.find_brief(self.p, c.active_brief(c.load_briefs(self.p)).id)
        self.fm("task", "new", "Second", "--type", "FIX", "--tier", "S", "--ac", "x :: true", "--step", "y",
                "--depends", a.id)
        live._CACHE.clear()
        self.assertIn([a.id], [d[1:] for d in board.part_deps(self.p)])

    # ---------------------------------------------------------------- T-3731: the five new pictures' parts

    def session(self, sid, **kw):
        d = os.path.join(c.state_dir(), "sessions")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"{sid}.json"), "w") as f:
            json.dump(dict({"ts": c.now(), "session_id": sid, "project": self.p.slug, "cwd": "/home/alice/gone",
                            "context_pct": 30}, **kw), f)

    def test_sessions_are_the_live_ones_with_a_clamped_context_and_a_private_project_only_counted(self):
        self.session("aaaaaaaa-1111", context_pct=140, model={"id": "claude-x", "display_name": "Opus /home/alice"})
        self.session("bbbbbbbb-2222", project="shop-api", context_pct="12", model="sonnet")
        self.session("cccccccc-3333", project="vault-1", model="opus")
        self.session("dddddddd-4444", ts="2020-01-01T00:00:00Z")  # not seen for years: gone
        for i in range(board.SESSIONS_MAX + 3):
            self.session(f"e{i:07}-5555", project="shop-api", ts=iso(60 + i), context_pct=None)
        with mock.patch.object(board, "_hidden", return_value={"vault-1"}):
            got = board.part_sessions(self.p)
        text = json.dumps(got)
        self.assertNotIn("/home/alice", text)
        self.assertNotIn("vault", text, "a sensitive project's session is a count, never a name")
        self.assertEqual(got["private"], 1)
        rows = {r["id"]: r for r in got["rows"]}
        self.assertEqual(len(got["rows"]), board.SESSIONS_MAX)
        self.assertEqual(got["more"], 2 + 3)
        mine = rows["aaaaaaaa"]
        self.assertEqual((mine["context_pct"], mine["mine"], mine["project"]), (100, True, self.p.slug))
        self.assertTrue(mine["model"].startswith("Opus"))
        self.assertEqual((rows["bbbbbbbb"]["context_pct"], rows["bbbbbbbb"]["mine"]), (12, False))
        self.assertNotIn("dddddddd", rows)
        self.assertTrue(all(isinstance(r["age_s"], int) and r["age_s"] >= 0 for r in got["rows"]))

    def test_asks_are_the_users_own_from_the_last_week_by_stage_oldest_first(self):
        H = 3600
        briefs = [Brief("T-1", "captured", source="user", created=iso(1 * H), title="Read /home/alice/a.md"),
                  Brief("T-2", "planned", source="user", created=iso(48 * H)),
                  Brief("T-3", "active", source="user", created=iso(30 * H), builder_returned=True),
                  Brief("T-4", "active", source="user", created=iso(20 * H)),
                  Brief("T-5", "verifying", source="user", created=iso(10 * H)),
                  Brief("T-6", "blocked", source="user", created=iso(5 * H)),
                  Brief("T-7", "done", source="user", created=iso(40 * H), updated=iso(2 * H)),
                  Brief("T-8", "done", source="user", created=iso(90 * H), updated=iso(72 * H)),
                  Brief("T-9", "active", source="agent", created=iso(H)),
                  Brief("T-10", "active", source="user", created=iso(10 * 86400)),
                  Brief("T-11", "captured", source="user", created=iso(H), raw="(noticed by an agent in T-1) x"),
                  Brief("T-12", "dropped", source="user", created=iso(H)),
                  Brief("T-13", "deferred", source="user", created=iso(3 * H))]
        with mock.patch.object(live, "_briefs", return_value=briefs):
            got = board.part_asks(self.p)
        self.assertEqual(got["days"], 7)
        self.assertEqual([(r["id"], r["stage"]) for r in got["rows"]],
                         [("T-2", "planned"), ("T-7", "landed"), ("T-3", "merging"), ("T-4", "building"),
                          ("T-5", "verifying"), ("T-6", "blocked"), ("T-13", "blocked"), ("T-1", "asked")])
        self.assertEqual(set(r["stage"] for r in got["rows"]) - set(board.ASK_STAGES), set())
        self.assertEqual(got["rows"][0]["age_h"], 48)
        self.assertEqual(got["rows"][-1]["title"], "Read ~/a.md")
        many = [Brief(f"T-{i}", "captured", source="user", created=iso(H + i)) for i in range(board.ASKS_MAX + 9)]
        with mock.patch.object(live, "_briefs", return_value=many):
            got = board.part_asks(self.p)
        self.assertEqual((len(got["rows"]), got["more"]), (board.ASKS_MAX, 9))
        ids = [r["id"] for r in got["rows"]]  # review: the cut keeps the newest, the drawer's "+9 older" are the dropped
        self.assertEqual(ids, [f"T-{i}" for i in reversed(range(board.ASKS_MAX))], "the newest kept, still oldest first")

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, capture_output=True, text=True, check=True).stdout.strip()

    def test_ship_is_the_last_push_and_its_ci_jobs_read_in_the_background_and_kept_thirty_seconds(self):
        self.assertIsNone(board.part_ship(self.p), "nothing pushed and no origin: no ship to draw")
        self.git("remote", "add", "origin", "https://github.com/acme/shop.git")
        self.git("commit", "-q", "--allow-empty", "-m", "Ship the cart page from /home/alice/cart")
        sha = self.git("rev-parse", "HEAD")
        self.git("update-ref", "refs/foreman/shipped-tip", sha)
        self.git("commit", "-q", "--allow-empty", "-m", "not shipped yet")
        calls = []

        def ci_now(p, at):
            calls.append(at)
            return {"runs": [{"workflow": "tests", "status": "in_progress", "conclusion": None}],
                    "jobs": [{"workflow": "tests", "name": "shard 1", "status": "completed", "conclusion": "success",
                              "steps": [5, 5], "secs": 40},
                             {"workflow": "tests", "name": "shard 2", "status": "in_progress", "conclusion": None,
                              "steps": [2, 6], "secs": 12}]}

        def settle():
            for t in list(live._STALE.values()):
                t.join(10)

        with mock.patch.object(board, "_ci_now", side_effect=ci_now), mock.patch.dict(os.environ, {"FOREMAN_NO_NET": ""}):
            first = board.part_ship(self.p)
            settle()
            second = board.part_ship(self.p)
            settle()
            self.assertEqual(len(calls), 1, "kept while fresh: one gh read")
            key = ("ci-jobs", self.p.dir, sha)
            with live._LOCK:
                at, val = live._CACHE[key]
                live._CACHE[key] = (at - board.CI_RUNNING_S - 1, val)
            board.part_ship(self.p)
            settle()
        self.assertEqual(len(calls), 2, "a running CI read again after thirty seconds")
        self.assertEqual(calls[0], sha)
        self.assertEqual((first["sha"], first["ref"], first["unshipped"]), (sha[:7], "shipped", 1))
        self.assertEqual(first["subject"], "Ship the cart page from ~/cart")
        self.assertEqual(first["ci"]["state"], "reading")
        self.assertEqual(second["ci"]["state"], "running")
        self.assertEqual([j["name"] for j in second["ci"]["jobs"]], ["shard 1", "shard 2"])
        with mock.patch.object(board, "_ci_now", side_effect=AssertionError("no network")), \
                mock.patch.dict(os.environ, {"FOREMAN_NO_NET": "1"}):
            live._CACHE.clear()
            off = board.part_ship(self.p)
        self.assertEqual(off["ci"]["state"], "offline")

    def test_ci_now_reads_the_runs_of_one_commit_and_their_jobs_through_gh(self):
        out = {("run", "list"): json.dumps([
                   {"databaseId": 7, "status": "completed", "conclusion": "failure", "workflowName": "tests"},
                   {"databaseId": 8, "status": "in_progress", "conclusion": "", "workflowName": "pages"}]),
               ("run", "view"): json.dumps({"jobs": [
                   {"name": "shard 1", "status": "completed", "conclusion": "failure", "startedAt": "2026-11-07T10:00:00Z",
                    "completedAt": "2026-11-07T10:01:30Z", "steps": [{"status": "completed"}, {"status": "queued"}]}]})}
        seen = []

        def gh(root, *argv):
            seen.append(argv)
            return out[argv[:2]]
        with mock.patch.object(board, "_gh", side_effect=gh):
            got = board._ci_now(self.p, "a" * 40)
        self.assertEqual([r["workflow"] for r in got["runs"]], ["tests", "pages"])
        self.assertEqual(got["jobs"][0], {"workflow": "tests", "name": "shard 1", "status": "completed",
                                          "conclusion": "failure", "steps": [1, 2], "secs": 90})
        self.assertIn("--commit", seen[0])
        self.assertEqual(len([a for a in seen if a[:2] == ("run", "view")]), 2)

    def test_ci_now_skips_run_view_while_a_run_is_unchanged(self):  # review: up to 4 gh calls every 30 s per project
        listed = [{"databaseId": 7, "status": "in_progress", "conclusion": "", "workflowName": "tests",
                   "updatedAt": "2026-11-07T10:00:00Z"}]
        view = json.dumps({"jobs": [{"name": "shard 1", "status": "in_progress", "conclusion": "",
                                     "startedAt": "2026-11-07T10:00:00Z", "steps": [{"status": "completed"}]}]})
        seen = []

        def gh(root, *argv):
            seen.append(argv[:2])
            return json.dumps(listed) if argv[:2] == ("run", "list") else view
        with mock.patch.object(board, "_gh", side_effect=gh):
            first = board._ci_now(self.p, "a" * 40)
            again = board._ci_now(self.p, "a" * 40)
            self.assertEqual(seen.count(("run", "view")), 1, "same id, status and updated time: no second view")
            self.assertEqual([j["name"] for j in again["jobs"]], ["shard 1"])
            self.assertGreaterEqual(again["jobs"][0]["secs"], first["jobs"][0]["secs"], "a running job's clock still moves")
            listed[0]["updatedAt"] = "2026-11-07T10:00:40Z"
            board._ci_now(self.p, "a" * 40)
        self.assertEqual(seen.count(("run", "view")), 2, "an updated run is viewed again")
        self.assertEqual(seen.count(("run", "list")), 3)

    def test_ci_now_names_a_missing_signed_out_or_slow_gh_instead_of_reading_forever(self):  # review: "reading CI…"
        for err, state in ((FileNotFoundError("gh"), "nogh"), (RuntimeError("gh exit 4"), "ghfail"),
                           (ValueError("not json"), "ghfail"), (subprocess.TimeoutExpired("gh", 20), "offline")):
            with mock.patch.object(board, "_gh", side_effect=err):
                got = board._ci_now(self.p, "b" * 40)
            self.assertEqual((got["error"], got["runs"], got["jobs"]), (state, [], []), state)
            self.assertEqual(board._ci_state(got), state)
        self.git("remote", "add", "origin", "https://github.com/acme/shop.git")
        self.git("commit", "-q", "--allow-empty", "-m", "x")
        self.git("update-ref", "refs/foreman/shipped-tip", self.git("rev-parse", "HEAD"))
        with mock.patch.object(board, "_gh", side_effect=FileNotFoundError("gh")), \
                mock.patch.dict(os.environ, {"FOREMAN_NO_NET": ""}):
            board.part_ship(self.p)
            for t in list(live._STALE.values()):
                t.join(10)
            ship = board.part_ship(self.p)
        self.assertEqual(ship["ci"]["state"], "nogh", "the panel says gh is missing, not 'reading CI…' forever")

    def test_usage_is_the_two_windows_the_days_spend_against_its_cap_and_the_weeks_samples(self):
        now = time.time()
        fc = {"five_hour": {"used": 40, "resets_at": now + 3600, "projected": 52.5, "full_at": None}}
        week = [{"ts": now - 7200, "used": 20, "resets": now + 86400}, {"ts": now - 3600, "used": 22, "resets": now + 86400},
                {"ts": now - 9 * 86400, "used": 90, "resets": now - 2 * 86400}]  # the last week's: not drawn
        with mock.patch("sampo.budget.forecast", return_value=fc), mock.patch("sampo.budget.samples", return_value=week), \
                mock.patch("sampo.budget.unknown_why", return_value="last read 9 h ago"), \
                mock.patch.object(live, "spend_today", return_value=[0.5, 1.25]):
            got = board.part_usage(self.p)
        five, seven = got["windows"]["five_hour"], got["windows"]["seven_day"]
        self.assertEqual((five["used"], five["span"], five["projected"], five["full_in"]), (40, 18000, 52.5, None))
        self.assertAlmostEqual(five["resets_in"], 3600, delta=5)
        self.assertEqual((seven["used"], seven["span"], seven["why"]), (None, 604800, "last read 9 h ago"))
        self.assertEqual(got["spend"], {"today": 1.75, "cap": 15.0, "hourly": [0.5, 1.25]})
        self.assertEqual([u for _, u in got["week"]], [20, 22])
        self.assertAlmostEqual(got["week"][0][0], 2, delta=0.1)

    def test_guard_counts_the_last_hours_commands_allowed_and_refused_by_rule(self):
        rows = [{"ts": iso(600 + i), "kind": "tool", "tool": "Bash", "project": self.p.slug, "target": "ls", "ok": True}
                for i in range(5)]
        rows += [{"ts": iso(300), "kind": "tool_fail", "tool": "Edit", "project": self.p.slug, "target": "x.py", "ok": False},
                 {"ts": iso(200), "kind": "guard_block", "rule": "rm-rf", "category": "destructive", "tool": "Bash",
                  "project": self.p.slug, "cmd": "rm -rf /home/alice/site"},
                 {"ts": iso(100), "kind": "guard_block", "rule": "rm-rf", "category": "destructive", "tool": "Bash",
                  "project": self.p.slug, "cmd": "rm -rf build"},
                 {"ts": iso(50), "kind": "guard_block", "rule": None, "category": "secrets", "tool": "Read",
                  "project": self.p.slug, "target": ".env"},
                 {"ts": iso(40), "kind": "guard_block", "rule": "x", "project": "elsewhere", "cmd": "nope"},
                 {"ts": iso(7200), "kind": "guard_block", "rule": "old", "project": self.p.slug, "cmd": "old"},
                 {"ts": iso(30), "kind": "prompt", "project": self.p.slug}]
        with open(os.path.join(c.state_dir(), "events.jsonl"), "a") as f:
            f.write("".join(json.dumps(r) + "\n" for r in rows) + "not json\n")
        got = board.part_guard(self.p)
        self.assertNotIn("/home/alice", json.dumps(got))
        self.assertEqual((got["allowed"], got["refused"]), (6, 3))
        self.assertEqual(got["rules"], [{"rule": "rm-rf", "n": 2}, {"rule": "secrets", "n": 1}])
        self.assertEqual([r["rule"] for r in got["recent"]], ["secrets", "rm-rf", "rm-rf"], "newest first")
        self.assertEqual(got["recent"][-1]["cmd"], "rm -rf ~/site")
        self.assertEqual(got["recent"][0]["cmd"], ".env")
        self.assertEqual(len(got["minutes"]), 60)
        self.assertEqual([sum(m[0] for m in got["minutes"]), sum(m[1] for m in got["minutes"])], [6, 3])
        self.assertEqual(got["tools"][0], {"tool": "Bash", "n": 7})
        both = board.guard_all([self.p.slug, "elsewhere"])
        self.assertEqual(both["refused"], 4)
        self.assertEqual({r["project"] for r in both["recent"]}, {self.p.slug, "elsewhere"})
        mine = both["by"][self.p.slug]  # review: a hidden chip takes its project out of the counts, not only the list
        self.assertEqual((mine["allowed"], mine["refused"], both["by"]["elsewhere"]["refused"]), (6, 3, 1))
        self.assertEqual(both["by"]["elsewhere"]["rules"], [{"rule": "x", "n": 1}])
        self.assertEqual(len(both["by"]["elsewhere"]["minutes"]), 60)

    def test_a_refused_command_carrying_a_secret_or_a_home_path_is_never_drawn(self):  # review: up to 120 chars reach the page
        token, opaque = "ghp_" + "Fake0Token1" * 3, "Zx9Qw8Er7Ty6Ui5Op4As3Df2Gh1"
        cmds = [f"curl -H 'Authorization: token {token}' https://api.example.com",
                f"deploy --key {opaque} --to prod", "cat /home/alice/.ssh/id_rsa", f"echo {token}"]
        with open(os.path.join(c.state_dir(), "events.jsonl"), "a") as f:
            f.write("".join(json.dumps({"ts": iso(10 + i), "kind": "guard_block", "rule": "secrets", "tool": "Bash",
                                        "project": self.p.slug, "cmd": cmd}) + "\n" for i, cmd in enumerate(cmds)))
        got = board.part_guard(self.p)
        text = json.dumps(got)
        for secret in (token, opaque, "/home/alice", "Fake0Token"):
            self.assertNotIn(secret, text)
        self.assertIn("cat ~/.ssh/id_rsa", [r["cmd"] for r in got["recent"]])
        self.assertIn("deploy --key [REDACTED] --to prod", [r["cmd"] for r in got["recent"]], "an opaque token too")
        self.assertIn("git reset --hard HEAD~2", board._guard_cmd("git reset --hard HEAD~2"), "a plain command stays")
        self.assertEqual(board._guard_cmd("pytest plugin/tests/test_board_live_page_2026.py"),
                         "pytest plugin/tests/test_board_live_page_2026.py", "a path is not a token")

    def test_guard_cmd_masks_url_path_and_query_secrets(self):
        hook = "T0AAAA/B0BBBB/Xk9Qm2Zr7Lp4Vw8Nc3Hd6Jt1"
        got = board._guard_cmd(f"curl -X POST https://hooks.example.test/services/{hook} -d x")
        self.assertNotIn("Xk9Qm2Zr7Lp4Vw8Nc3Hd6Jt1", got, "a webhook path token is masked")
        self.assertIn("https://hooks.example.test/services/", got, "the rest of the URL stays")
        got = board._guard_cmd("curl 'https://api.example.test/v1/items?id=7&token=q8W3e9R2t7Y4u1I6o5P0a2S8d'")
        self.assertNotIn("q8W3e9R2t7Y4u1I6o5P0a2S8d", got, "an opaque query value is masked")
        self.assertIn("api.example.test/v1/items?id=7&token=", got)
        for ok in ("cat plugin/lib/sampo/board.py", "python3.12 -m pip install pkg==3.12.1",
                   "git checkout my-long-feature-branch-name-here", "curl https://example.test/docs/getting-started-guide?id=42",
                   "open https://example.test/item/a1b2c3"):
            self.assertEqual(board._guard_cmd(ok), ok, ok)


class Bigs(SampoTestCase):
    """T-5027: board.bigs is the big-task tree (T-4223) in the snapshot: each big task with done/total, wave and children."""

    def mk(self, title, tier="S"):
        out = self.fm("task", "new", title, "--type", "FEATURE", "--tier", tier, "--ac", "x :: true", "--step", "y", "--json")
        return json.loads(out.stdout)["id"]

    def briefs(self, tree):
        """The project's briefs with each id in tree {parent: [children]} hung under its parent in memory."""
        got = c.load_briefs(self.p)
        for parent, kids in tree.items():
            for b in got:
                if b.id in kids:
                    b.meta["parent"] = parent
        return got

    def setUp(self):
        super().setUp()
        self.fm("task", "new", "Seed", "--type", "FIX", "--tier", "S", "--ac", "x :: true", "--step", "y")
        self.p = c.find_project(self.repo)
        live._CACHE.clear()

    def test_bigs_is_a_snapshot_part_listing_each_big_task_with_done_total_wave_and_children(self):
        big, k1, k2, k3 = self.mk("Rework /home/alice/x", "XL"), self.mk("One"), self.mk("Two"), self.mk("Three")
        self.mk("Plain")
        briefs = self.briefs({big: {k1, k2, k3}})
        by = {b.id: b for b in briefs}
        by[k1].meta["status"] = "done"
        by[k3].meta["depends_on"] = [k2]
        with mock.patch.object(live, "_briefs", return_value=briefs):
            got = board.part_bigs(self.p)
            snap = board.collect(self.p)
        self.assertEqual([t["id"] for t in got], [big])
        t = got[0]
        self.assertEqual((t["done"], t["total"], t["wave"], t["tier"]), (1, 3, 1, "XL"))
        self.assertEqual({k["id"]: k["wave"] for k in t["children"]}, {k1: 1, k2: 1, k3: 2})
        self.assertEqual({k["id"]: k["status"] for k in t["children"]}[k1], "done")
        self.assertNotIn("/home/alice", json.dumps(got))
        self.assertEqual(snap["bigs"], got)
        json.dumps(got)

    def test_bigs_more_is_kept_like_bigs_not_recounted_every_snapshot(self):
        self.assertIn("bigs_more", board.SLOW)

    def test_bigs_is_empty_with_no_big_task_and_a_tree_not_yet_expanded_has_no_children(self):
        self.assertEqual(board.part_bigs(self.p), [])
        big = self.mk("Alone", "XL")
        with mock.patch.object(live, "_briefs", return_value=c.load_briefs(self.p)):
            got = board.part_bigs(self.p)
        self.assertEqual([(t["id"], t["total"], t["wave"], t["children"]) for t in got], [(big, 0, None, [])])

    def test_bigs_keeps_twelve_trees_open_ones_first_and_forty_children_saying_how_many_more(self):
        bigs = [self.mk(f"Big {i}", "XL") for i in range(14)]
        kids = [self.mk(f"Kid {i}") for i in range(45)]
        briefs = self.briefs({bigs[-1]: set(kids)})
        for b in briefs:
            if b.id in bigs[:2]:
                b.meta["status"] = "done"
        with mock.patch.object(live, "_briefs", return_value=briefs):
            got = board.part_bigs(self.p)
        self.assertEqual(len(got), board.BIGS_MAX)
        self.assertNotIn(bigs[0], [t["id"] for t in got])  # the closed ones go first when the cap bites
        last = next(t for t in got if t["id"] == bigs[-1])
        self.assertEqual((last["total"], len(last["children"]), last["more"]), (45, board.BIG_KIDS_MAX, 5))

    def test_bigs_with_fourteen_open_keeps_the_newest_twelve_builds_only_those_trees_and_counts_the_cut(self):
        from sampo import bigview
        bigs = [self.mk(f"Big {i}", "XL") for i in range(14)]
        briefs = self.briefs({})
        built = []
        real = bigview._tree
        with mock.patch.object(live, "_briefs", return_value=briefs), \
                mock.patch.object(bigview, "_tree", side_effect=lambda p, b, *a: built.append(b.id) or real(p, b, *a)):
            got = board.part_bigs(self.p)
            more = board.part_bigs_more(self.p)
        newest = sorted(bigs, key=c.id_num, reverse=True)[:board.BIGS_MAX]
        self.assertEqual([t["id"] for t in got], newest)
        self.assertEqual(sorted(built), sorted(newest))
        self.assertEqual(more, 2)

    def test_bigs_never_raises_it_reads_none_in_the_snapshot_when_the_tree_read_breaks(self):
        with mock.patch("sampo.bigview.tree_rows", side_effect=RuntimeError("down")):
            self.assertIsNone(board.collect(self.p)["bigs"])
        self.assertIn("bigs", board.SLOW)


NODE = r"""
const B = require(process.argv[1]);
const fs = require("fs");
const out = JSON.parse(fs.readFileSync(process.argv[2], "utf8")).map(([snap, hist, hidden]) => {
  const m = B.boardModel(snap, hist, hidden);
  let bad = 0;
  const text = JSON.stringify(m, (k, v) => { if (typeof v === "number" && !isFinite(v)) bad++; return v; });
  return {m: JSON.parse(text), bad};
});
console.log(JSON.stringify(out));
"""


CTX = r"""
// a 2D context that records what a real canvas would throw on (negative radii, stops off [0, 1]) or draw nowhere (NaN)
function ctx() {
  const st = {ops: 0, bad: [], texts: [], px: []}, o = {};
  let px = 10;
  const check = (k, a) => {
    if (a.some((x) => typeof x === "number" && !isFinite(x))) st.bad.push(k + " got a non-finite number");
    const neg = {arc: [2], ellipse: [2, 3], createRadialGradient: [2, 5], roundRect: [2, 3]}[k] || [];
    if (neg.some((i) => a[i] < 0)) st.bad.push(k + " got a negative size");
  };
  const g = new Proxy(o, {
    get(_, k) {
      if (k in o) return o[k];
      if (k === "measureText") return (s) => ({width: String(s).length * px * 0.6});
      if (k === "createLinearGradient" || k === "createRadialGradient") return (...a) => {
        check(k, a);
        return {addColorStop(u, c) { if (!(u >= 0 && u <= 1)) st.bad.push("a gradient stop off [0, 1]"); }};
      };
      if (k === "fillText") return (t, ...a) => { st.ops++; check(k, a); st.texts.push(String(t)); st.px.push([px, String(t)]); };
      return (...a) => { st.ops++; check(k, a); };
    },
    set(_, k, v) {
      if (k === "font") px = +((/([\d.]+)px/.exec(v) || [0, 10])[1]);
      if (k === "lineWidth" && !(v >= 0)) st.bad.push("lineWidth " + v);
      o[k] = v;
      return true;
    },
  });
  return {g, st};
}
"""
DRAWN = r"""
const B = require(process.argv[1]);
const fs = require("fs");
""" + CTX + r"""
const sizes = [[120, 80], [360, 230], [430, 260], [520, 300], [760, 360]];
const out = [];
JSON.parse(fs.readFileSync(process.argv[2], "utf8")).forEach(([snap, hist], ci) => {
  for (const p of B.boardModel(snap, hist).panels) {
    if (p.data == null) continue;
    const R = B.DRAW[p.key];
    if (!R) { out.push(`${p.key}: no drawer`); continue; }
    for (const [w, h] of sizes) {
      const where = `case ${ci} ${p.key} ${w}x${h}`, mem = {}, {g, st} = ctx();
      try {
        R.base(g, p.data, w, h, B.TOKENS, mem);
        if (!st.ops) out.push(where + ": base drew nothing");
        for (const t of [0, 3.3, 7.25, 1e5]) if (R.live) R.live(g, p.data, w, h, t, B.TOKENS, mem);
      } catch (e) { out.push(`${where}: ${e && e.stack ? e.stack.split("\n").slice(0, 2).join(" ") : e}`); }
      for (const b of new Set(st.bad)) out.push(`${where}: ${b}`);
    }
  }
});
console.log(JSON.stringify(out));
"""


TEXTS = r"""
const B = require(process.argv[1]);
const fs = require("fs");
""" + CTX + r"""
const [snap, key, w, h] = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const p = B.boardModel(snap, []).panels.find((x) => x.key === key), {g, st} = ctx();
B.DRAW[key].base(g, p.data, w, h, B.TOKENS, {});
console.log(JSON.stringify(st.texts));
"""


SMALL = r"""
const B = require(process.argv[1]);
const fs = require("fs");
""" + CTX + r"""
// every text the named drawers draw, base and live, at a phone's and a desktop's panel sizes, below the floor in px
const [cases, keys, floor] = JSON.parse(fs.readFileSync(process.argv[2], "utf8")), out = new Set();
cases.forEach(([snap, hidden]) => {
  for (const p of B.boardModel(snap, [], hidden).panels) {
    if (!keys.includes(p.key) || p.data == null) continue;
    for (const [w, h] of [[340, 230], [430, 260], [520, 300], [760, 360]]) {
      const mem = {}, {g, st} = ctx();
      B.DRAW[p.key].base(g, p.data, w, h, B.TOKENS, mem);
      for (const t of [0, 3.3, 7.25]) if (B.DRAW[p.key].live) B.DRAW[p.key].live(g, p.data, w, h, t, B.TOKENS, mem);
      for (const [px, t] of st.px) if (px < floor && t.trim()) out.add(`${p.key} ${w}x${h}: ${px}px "${t}"`);
    }
  }
});
console.log(JSON.stringify([...out]));
"""


def big_snapshot():
    s = json.loads(read_text(os.path.join(FIX, "snap-5.json")))
    b = s["board"]
    b["lanes"] = [{"task": f"T-{i}", "title": "x" * 300, "state": "building", "more": 0,
                   "files": [{"path": f"plugin/f{j}.py", "hot": j % 7 == 0} for j in range(100)]} for i in range(50)]
    b["commands"] = [{"at": "2026-11-07T10:00:00Z", "cmd": "sampo next", "ok": True}] * 500
    b["cpu"]["minutes"] = [[1_800_000_000 + 60 * i, 1, 1, 1, 1, 1] for i in range(500)]
    b["tests"]["modules"] = [{"name": f"test_{i}", "state": "cached"} for i in range(900)]
    b["deps"] = [[f"T-{i}", f"T-{i + 1}"] for i in range(400)]
    b["sessions"] = {"rows": [{"id": f"s{i:07}", "project": "p" * 200, "task": "T-1", "model": "m" * 300,
                               "context_pct": i % 140, "age_s": i, "mine": i % 2 == 0} for i in range(500)],
                     "more": 10 ** 6, "private": 10 ** 6}
    b["asks"] = {"rows": [{"id": f"T-{i}", "title": "a" * 300, "stage": "building", "age_h": i % 170} for i in range(900)],
                 "days": 7, "more": 10 ** 5}
    b["ship"] = {"sha": "abc1234", "subject": "s" * 400, "age_s": 10 ** 9, "ref": "shipped", "unshipped": 10 ** 6,
                 "ci": {"state": "running", "runs": [{"workflow": "w" * 200, "status": "in_progress", "conclusion": None}] * 50,
                        "jobs": [{"workflow": "w", "name": "j" * 300, "status": "in_progress", "conclusion": None,
                                  "steps": [10 ** 6, 10], "secs": 10 ** 9}] * 300, "failed": ["x" * 300] * 40}}
    b["usage"] = {"windows": {"five_hour": {"used": 1e6, "span": 18000, "resets_in": -50, "projected": 1e9, "full_in": -1},
                              "seven_day": {"used": -20, "span": 604800, "resets_in": 1e12, "projected": None, "full_in": None}},
                  "spend": {"today": 1e9, "cap": 0, "hourly": [1e6] * 500}, "week": [[i / 10, i % 120] for i in range(5000)]}
    b["guard"] = {"allowed": 10 ** 7, "refused": 10 ** 6, "rules": [{"rule": "r" * 200, "n": i} for i in range(100)],
                  "recent": [{"ago_s": i, "rule": "r", "tool": "Bash", "cmd": "c" * 300} for i in range(500)],
                  "minutes": [[10 ** 5, 10 ** 4]] * 500, "tools": [{"tool": "t" * 100, "n": 1}] * 50}
    s["stats"]["cards"]["general"], s["stats"]["moons"]["candidates"] = 10 ** 7, 10 ** 6
    s["queue"] = s["queue"] * 100
    s["flows"] = s["flows"] * 300
    return s


class Model(SampoTestCase):
    """boardModel under node: the page's one mapping from a snapshot (and the ones before it) to fourteen panels."""

    def run_model(self, cases):
        node = shutil.which("node")
        if not node:
            self.skipTest("no node to run the page's model")
        src = os.path.join(self.tmp, "cases.json")
        with open(src, "w") as f:
            json.dump(cases, f)
        out = subprocess.run([node, "-e", NODE, os.path.join(WEB, "board.js"), src], capture_output=True, text=True,
                             timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        return json.loads(out.stdout)

    def test_empty_typical_and_oversized_snapshots_make_fourteen_bounded_panels(self):
        typical = json.loads(read_text(os.path.join(FIX, "snap-5.json")))
        hist = [json.loads(read_text(os.path.join(FIX, f"snap-{i}.json"))) for i in range(1, 5)]
        empty, typ, big = self.run_model([[{}, []], [typical, hist], [big_snapshot(), [big_snapshot()] * 3]])
        for name, got in (("empty", empty), ("typical", typ), ("big", big)):
            m = got["m"]
            self.assertEqual(got["bad"], 0, f"no NaN or Infinity in {name}")
            self.assertEqual([p["key"] for p in m["panels"]], KEYS, name)
            for i, p in enumerate(m["panels"]):
                where = f"{name} {p['key']}"
                self.assertEqual(p["n"], f"{i + 1:02}", where)
                self.assertTrue(p["name"] and p["what"] and p["accent"], where)
                self.assertIsInstance(p["label"], str, where)
                self.assertTrue(p["foot"]["text"], where)
                self.assertIsInstance(p["foot"]["stat"], str, where)
            self.assertNotIn("/home/", json.dumps(m), name)
        by = lambda got: {p["key"]: p["data"] for p in got["m"]["panels"]}
        b = by(big)
        self.assertLessEqual(len(b["lanes"]["clusters"]), 16)
        self.assertTrue(all(len(x["nodes"]) <= 24 for x in b["lanes"]["clusters"]))
        self.assertLessEqual(sum(b["library"]["dots"].values()), 1500)
        self.assertLessEqual(len(b["sampo"]["commands"]), 12)
        self.assertLessEqual(len(b["sampo"]["queue"]), 8)
        self.assertLessEqual(len(b["cpu"]["minutes"]), 30)
        self.assertLessEqual(len(b["tests"]["cells"]), 400)
        self.assertLessEqual(len(b["models"]["links"]), 24)
        self.assertLessEqual(len(b["sessions"]["bodies"]), 24)  # T-3731: the five new pictures stay bounded too
        self.assertTrue(all(0 <= x["ctx"] <= 100 for x in b["sessions"]["bodies"] if x["ctx"] is not None))
        self.assertLessEqual(len(b["asks"]["rows"]), 60)
        self.assertLessEqual(len(b["ship"]["ships"]), 6)
        self.assertTrue(all(len(s["jobs"]) <= 16 for s in b["ship"]["ships"]))
        self.assertTrue(all(0 <= j["done"] <= j["total"] for s in b["ship"]["ships"] for j in s["jobs"]))
        self.assertLessEqual(len(b["usage"]["week"]), 84)
        self.assertLessEqual(len(b["usage"]["spend"]["hourly"]), 24)
        self.assertTrue(all(0 <= t["used"] <= 100 for t in b["usage"]["tanks"] if t["used"] is not None))
        self.assertLessEqual(len(b["guard"]["rules"]), 6)
        self.assertLessEqual(len(b["guard"]["recent"]), 12)
        self.assertLessEqual(len(b["guard"]["minutes"]), 60)
        self.assertLessEqual(len(json.dumps(big["m"])), 200_000, "the model stays small whatever the snapshot")
        t = by(typ)
        self.assertGreater(sum(t["library"]["dots"].values()), 0)
        self.assertTrue(t["lanes"]["clusters"])
        self.assertTrue(t["models"]["links"])
        self.assertTrue(t["sampo"]["commands"])
        self.assertTrue(t["sessions"]["bodies"] and t["asks"]["rows"] and t["ship"]["ships"][0]["jobs"])
        self.assertTrue(t["guard"]["rules"] and t["usage"]["tanks"][0]["used"] is not None)
        self.assertEqual(by(empty)["loop"], None)
        for k in ("sessions", "asks", "ship", "usage", "guard"):
            self.assertFalse(by(empty)[k]["known"], f"{k}: an empty snapshot is not yet read")

    def test_the_lanes_footer_names_the_lanes_not_drawn_and_an_empty_hour_chart_says_so(self):  # T-4076
        lanes = next(p for p in self.run_model([[big_snapshot(), []]])[0]["m"]["panels"] if p["key"] == "lanes")
        self.assertEqual(len(lanes["data"]["clusters"]), 16)
        self.assertIn("+34 more", lanes["foot"]["text"], "50 lanes out, 16 drawn: the rest are named")
        snap = json.loads(read_text(os.path.join(FIX, "snap-5.json")))
        snap["board"]["library"]["hourly"] = [0] * 24
        src = os.path.join(self.tmp, "case.json")
        with open(src, "w") as f:
            json.dump([snap, "library", 520, 300], f)
        out = subprocess.run([shutil.which("node") or "node", "-e", TEXTS, os.path.join(WEB, "board.js"), src],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        texts = json.loads(out.stdout)
        self.assertIn("kept / hour · peak 0", texts, "nothing kept is peak 0, not 1")
        self.assertIn("nothing kept in 24 h", texts, "a blank chart says why it is blank")

    def test_a_fresh_servers_first_snapshot_has_no_cpu_reading_and_says_so(self):  # T-4076 QA round 2: read 0.0
        snap = json.loads(read_text(os.path.join(FIX, "snap-5.json")))
        snap["board"]["cpu"] = dict(snap["board"]["cpu"], now=None, minutes=[])  # cpu_part before its second read
        cpu = next(p for p in self.run_model([[snap, []]])[0]["m"]["panels"] if p["key"] == "cpu")
        self.assertEqual(cpu["foot"]["text"], "no CPU reading yet")
        self.assertFalse(cpu["data"]["known"])

    def test_the_sampo_footer_keeps_its_ok_count_and_adds_the_caught_figure_only_when_predictions_are_scored(self):
        # T-4665: foresight's "caught before you noticed" sits beside the command stat, never in its place
        base = json.loads(read_text(os.path.join(FIX, "snap-5.json")))
        base["board"]["commands"] = [{"at": "2026-11-07T10:00:00Z", "cmd": "sampo next", "ok": True},
                                     {"at": "2026-11-07T10:01:00Z", "cmd": "sampo state", "ok": False}]
        cases = []
        for caught in (None, {"caught": 1, "scored": 0}, {"caught": 2, "scored": 5}):
            s = json.loads(json.dumps(base))
            s["board"]["state"] = dict(s["board"].get("state") or {})
            s["board"]["state"].pop("caught", None)
            if caught:
                s["board"]["state"]["caught"] = caught
            cases.append([s, []])
        foots = [next(p for p in m["m"]["panels"] if p["key"] == "sampo")["foot"] for m in self.run_model(cases)]
        for f in foots:
            self.assertEqual(f["stat"], "1/2 ok", "the existing stat stays")
            self.assertTrue(f["text"].startswith("$ sampo state"))
        self.assertEqual(foots[0]["text"], "$ sampo state", "no rows: no caught figure")
        self.assertEqual(foots[1]["text"], "$ sampo state", "rows booked, none scored: no caught figure")
        self.assertEqual(foots[2]["text"], "$ sampo state · caught before you noticed: 2 of 5")

    def test_every_panel_draws_empty_typical_and_huge_data_at_every_size_without_a_fault(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("no node to run the page's drawers")
        typical = json.loads(read_text(os.path.join(FIX, "snap-5.json")))
        hist = [json.loads(read_text(os.path.join(FIX, f"snap-{i}.json"))) for i in range(1, 5)]
        src = os.path.join(self.tmp, "cases.json")
        with open(src, "w") as f:
            json.dump([[{}, []], [typical, hist], [big_snapshot(), [big_snapshot()] * 3]], f)
        out = subprocess.run([node, "-e", DRAWN, os.path.join(WEB, "board.js"), src], capture_output=True, text=True,
                             timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        self.assertEqual(json.loads(out.stdout), [])

    def test_all_mode_draws_every_projects_lanes_and_loops_coloured_by_project_and_the_chips_filter(self):  # T-3730
        base = json.loads(read_text(os.path.join(FIX, "snap-5.json")))
        lane = lambda slug, t: dict(base["board"]["lanes"][0], task=t, project=slug)
        loop = lambda slug, t: dict(base["board"]["loop"], id=t, project=slug)
        snap = dict(base, project={"slug": "all", "name": "All projects"}, all=True, projects=[
            {"slug": "shop-api", "name": "shop-api"}, {"slug": "notes-app", "name": "notes-app"},
            {"slug": "vault-1", "name": "vault", "sensitive": True}],
            board=dict(base["board"], lanes=[lane("shop-api", "T-1"), lane("notes-app", "T-2")],
                       trains=[], loops=[loop("shop-api", "T-1"), loop("notes-app", "T-2")]))
        lonely = dict(base)
        got = self.run_model([[snap, []], [lonely, []]])
        m = got[0]["m"]["panels"]
        lanes = next(p for p in m if p["key"] == "lanes")
        self.assertEqual(got[0]["bad"], 0)
        clusters = lanes["data"]["clusters"]
        self.assertEqual({c["proj"] for c in clusters}, {"shop-api", "notes-app"})
        self.assertEqual(len({c["pc"] for c in clusters}), 2, "one colour per project")
        self.assertTrue(all(c["tag"] for c in clusters))
        loopp = next(p for p in m if p["key"] == "loop")
        d = loopp["data"]
        self.assertTrue(d["all"])
        self.assertEqual({x["proj"] for x in d["loops"]}, {"shop-api", "notes-app"})
        self.assertNotIn("vault", json.dumps(m), "a sensitive project is never drawn")
        self.assertFalse((next(p for p in got[1]["m"]["panels"] if p["key"] == "loop")["data"] or {}).get("all"))

    def texts(self, snap, key, w=520, h=300):
        node = shutil.which("node") or self.skipTest("no node to run the page's drawers")
        src = os.path.join(self.tmp, "case.json")
        with open(src, "w") as f:
            json.dump([snap, key, w, h], f)
        out = subprocess.run([node, "-e", TEXTS, os.path.join(WEB, "board.js"), src], capture_output=True, text=True,
                             timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        return json.loads(out.stdout)

    def test_each_new_picture_says_what_it_waits_for_when_empty(self):  # T-3731
        want = {"sessions": "waiting for a Claude session to report", "asks": "waiting for your next ask",
                "ship": "waiting for the first push", "usage": "waiting for a usage reading from the statusline",
                "guard": "waiting for a command at the gate"}
        quiet = {"board": {"sessions": {"rows": [], "more": 0, "private": 0}, "asks": {"rows": [], "days": 7, "more": 0},
                           "ship": None, "usage": {"windows": {}, "spend": {"today": 0, "cap": 15, "hourly": []}, "week": []},
                           "guard": {"allowed": 0, "refused": 0, "rules": [], "recent": [], "minutes": [], "tools": []}}}
        for snap in ({}, quiet):
            for key, say in want.items():
                self.assertIn(say, self.texts(snap, key), f"{key} {'read' if snap else 'unread'}")
        foots = {p["key"]: p["foot"]["text"] for p in self.run_model([[quiet, []]])[0]["m"]["panels"]}
        self.assertEqual(foots["sessions"], "no live session")
        self.assertEqual(foots["guard"], "no command in the last hour")

    def test_the_new_pictures_name_what_they_draw_from_the_typical_snapshot(self):  # T-3731
        typical = json.loads(read_text(os.path.join(FIX, "snap-5.json")))
        sess = self.texts(typical, "sessions", 760, 360)
        self.assertIn("machine", sess)
        self.assertTrue(any(re.fullmatch(r"\d+%", t) for t in sess), "each session carries its context gauge")
        self.assertTrue({"asked", "building", "landed"} <= set(self.texts(typical, "asks", 760, 360)))
        ship = self.texts(typical, "ship", 760, 360)
        self.assertTrue(any(re.fullmatch(r"[0-9a-f]{7}", t) for t in ship), "the pushed commit's sha")
        usage = self.texts(typical, "usage", 760, 360)
        self.assertTrue({"5 h", "week"} <= set(usage), usage)
        guard = self.texts(typical, "guard", 760, 360)
        self.assertTrue({"allowed", "refused"} <= set(guard), guard)

    def test_all_mode_colours_the_new_pictures_by_project_and_keeps_a_sensitive_one_out(self):  # T-3731
        base = json.loads(read_text(os.path.join(FIX, "snap-5.json")))
        b = base["board"]
        ask = lambda slug, i: dict(b["asks"]["rows"][0], id=f"T-{i}", project=slug)
        ship = lambda slug: dict(b["ship"], project=slug)
        snap = dict(base, project={"slug": "all", "name": "All projects"}, all=True, projects=[
            {"slug": "shop-api", "name": "shop-api"}, {"slug": "notes-app", "name": "notes-app"},
            {"slug": "vault-1", "name": "vault", "sensitive": True}],
            board=dict(b, asks={"rows": [ask("shop-api", 1), ask("notes-app", 2)], "days": 7, "more": 0},
                       ships=[ship("shop-api"), ship("notes-app")],
                       sessions=dict(b["sessions"], rows=[dict(r, project=s) for r, s in
                                                          zip(b["sessions"]["rows"], ["shop-api", "notes-app"] * 9)]),
                       guard=dict(b["guard"], recent=[dict(r, project=s) for r, s in
                                                      zip(b["guard"]["recent"], ["shop-api", "notes-app"] * 9)])))
        got = self.run_model([[snap, []]])[0]
        self.assertEqual(got["bad"], 0)
        by = {p["key"]: p["data"] for p in got["m"]["panels"]}
        for key, rows in (("asks", by["asks"]["rows"]), ("ship", by["ship"]["ships"]),
                          ("sessions", by["sessions"]["bodies"]), ("guard", by["guard"]["recent"])):
            self.assertEqual({r["proj"] for r in rows}, {"shop-api", "notes-app"}, key)
            self.assertEqual(len({r["pc"] for r in rows}), 2, f"{key}: one colour per project")
            self.assertTrue(all(r["tag"] for r in rows), key)
        self.assertNotIn("vault", json.dumps(got["m"]))

    def all_snap(self, **board):
        base = json.loads(read_text(os.path.join(FIX, "snap-5.json")))
        return dict(base, project={"slug": "all", "name": "All projects"}, all=True, projects=[
            {"slug": "shop-api", "name": "shop-api"}, {"slug": "notes-app", "name": "notes-app"}],
            board=dict(base["board"], **board))

    def test_all_mode_chips_filter_the_gates_counts_ships_past_six_say_more_and_asks_keep_the_newest(self):  # review
        tally = lambda a, r, rule: {"allowed": a, "refused": r, "rules": [{"rule": rule, "n": r}], "tools": [{"tool": "Bash", "n": a + r}],
                                    "minutes": [[0, 0]] * 59 + [[a, r]]}
        guard = dict(tally(12, 5, "rm-rf"), recent=[], by={"shop-api": tally(10, 4, "rm-rf"), "notes-app": tally(2, 1, "secrets")})
        guard["rules"] = [{"rule": "rm-rf", "n": 4}, {"rule": "secrets", "n": 1}]
        ship = json.loads(read_text(os.path.join(FIX, "snap-5.json")))["board"]["ship"]
        ships = [dict(ship, project=["shop-api", "notes-app"][i % 2]) for i in range(8)]
        asks = [{"id": f"T-{p}{h}", "title": "x", "stage": "asked", "age_h": h, "project": p}
                for h in reversed(range(40)) for p in ("shop-api", "notes-app")]  # oldest first, as the server sends
        snap = self.all_snap(guard=guard, ships=ships, asks={"rows": asks, "days": 7, "more": 0})
        both, shop = self.run_model([[snap, [], []], [snap, [], ["notes-app"]]])
        P = lambda got: {p["key"]: p for p in got["m"]["panels"]}  # noqa: E731
        g = P(shop)["guard"]["data"]
        self.assertEqual((g["allowed"], g["refused"]), (10, 4), "a hidden chip takes its project out of the counts")
        self.assertEqual([r["rule"] for r in g["rules"]], ["rm-rf"])
        self.assertEqual(g["minutes"][-1], [10, 4])
        self.assertEqual(g["tools"], [{"tool": "Bash", "n": 14}])
        self.assertEqual((P(both)["guard"]["data"]["allowed"], P(both)["guard"]["data"]["refused"]), (12, 5))
        s = P(both)["ship"]
        self.assertEqual((len(s["data"]["ships"]), s["data"]["more"]), (6, 2))
        self.assertIn("+2 more", s["foot"]["text"])
        a = P(both)["asks"]["data"]
        self.assertEqual(len(a["rows"]), 60)
        self.assertEqual(max(r["age"] for r in a["rows"]), 29, "the newest kept in All mode too")
        self.assertEqual(a["more"], 20)
        self.assertIn("+2 more pushes not drawn", self.texts(snap, "ship", 760, 360), "the drawer names the pushes not drawn")

    def test_a_missing_or_failing_gh_is_named_on_the_ship_panel(self):  # review: not "reading CI…" forever
        base = json.loads(read_text(os.path.join(FIX, "snap-5.json")))
        for state, say in (("nogh", "gh isn't installed"), ("ghfail", "gh can't read CI")):
            snap = dict(base, board=dict(base["board"], ship=dict(base["board"]["ship"],
                                                                  ci={"state": state, "runs": [], "jobs": [], "failed": []})))
            ship = next(p for p in self.run_model([[snap, []]])[0]["m"]["panels"] if p["key"] == "ship")
            self.assertEqual(ship["data"]["ships"][0]["state"], state)
            self.assertTrue(any(say in t for t in self.texts(snap, "ship", 760, 360)), state)

    def test_the_new_pictures_never_draw_text_under_nine_px(self):  # review: 7.5-8 px labels were hard to read
        node = shutil.which("node") or self.skipTest("no node to run the page's drawers")
        typical = json.loads(read_text(os.path.join(FIX, "snap-5.json")))
        b = typical["board"]
        many = self.all_snap(ships=[dict(b["ship"], project=["shop-api", "notes-app"][i % 2]) for i in range(8)],
                             sessions=dict(b["sessions"], rows=[dict(b["sessions"]["rows"][i % len(b["sessions"]["rows"])],
                                                                     id=f"s{i:07}", project=["shop-api", "notes-app"][i % 2])
                                                                for i in range(15)]))
        src = os.path.join(self.tmp, "small.json")
        with open(src, "w") as f:
            json.dump([[[{}, []], [typical, []], [many, []]], ["sessions", "asks", "ship", "usage", "guard"], 9], f)
        out = subprocess.run([node, "-e", SMALL, os.path.join(WEB, "board.js"), src], capture_output=True, text=True,
                             timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        self.assertEqual(json.loads(out.stdout)[:40], [])

    def test_the_loop_panel_waits_until_the_first_snapshot_and_the_live_green_is_the_boards(self):
        # T-4078: on a cold server the loop panel said 'no task in focus' before anything was read (sampo shot, 1600
        # light, 2026-11-09); sampo critique found the near-duplicate greens #0A8656 and #0E8A5F
        node = shutil.which("node") or self.skipTest("no node to run the page's code")
        js = ("const B = require(process.argv[1]); "
              "console.log(JSON.stringify([B.idle(''), B.idle('null')]))")
        out = subprocess.run([node, "-e", js, os.path.join(WEB, "board.js")], capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        self.assertEqual(json.loads(out.stdout), ["waiting for the first snapshot", "no task in focus"],
                         "before any update (dk '') it waits; after one with no focus (dk 'null') it says so")
        css = read_text(os.path.join(WEB, "live.css"))
        self.assertIn("--green:#0a8656", css.lower())
        self.assertNotIn("#0e8a5f", css.lower())

    def test_the_tokens_file_and_the_stylesheet_agree(self):
        tokens = json.loads(read_text(os.path.join(WEB, "board-tokens.json")))
        css = read_text(os.path.join(WEB, "board.css"))
        dark = re.search(r"(?s)\n\.board \{(.*?)\n\}", css).group(1)
        light = re.search(r"(?s)prefers-color-scheme:\s*light\)\s*\{\s*\.board \{(.*?)\}", css).group(1)
        self.assertEqual(set(tokens), {"dark", "light"})
        self.assertGreaterEqual(len(tokens["dark"]), 12)
        for scheme, block in (("dark", dark), ("light", light)):
            for k, v in tokens[scheme].items():
                self.assertRegex(block, rf"--bd-{re.escape(k)}:\s*{re.escape(v)};", f"{scheme} {k}")

    def test_the_claude_code_mods_board_palette_is_generated_from_the_tokens(self):
        # T-3701: one source of truth; the mod ships alone, so its copy is generated and must match
        tool = os.path.join(ROOT, "mods", "foreman-ui", "tools", "board_palette.py")
        out = subprocess.run(["python3", tool, "--check"], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        ts = read_text(os.path.join(ROOT, "mods", "foreman-ui", "hooks", "board-palette.ts"))
        for k, v in json.loads(read_text(os.path.join(WEB, "board-tokens.json")))["dark"].items():
            self.assertIn(f"  {k}: 0x{v[1:].lower()},", ts)


class Layout(SampoTestCase):
    """T-3739: a Board layout (panel order, span, height, hidden, style, motion) is a file per scope, read project over
    global over the built-in one; `sampo board layout` and the page's edit mode write it, and a bad one names its field."""

    def setUp(self):
        super().setUp()
        self.p = c.find_project(self.repo, create=True)
        live._CACHE.clear()

    def test_the_built_in_layout_is_the_nine_panels_in_the_pages_order(self):
        got = board.layout_view(self.p)
        self.assertEqual(got["order"], KEYS)
        self.assertEqual(got["panels"]["lanes"], {"span": 1, "height": 1, "hidden": False})
        self.assertEqual((got["style"], got["motion"], got["from"]), ("card", "full", "builtin"))
        self.assertEqual(got["errors"], [])

    def test_a_layout_is_read_project_over_global_over_built_in_field_by_field(self):
        board.layout_set("global", None, {"style": "flat", "panels": {"lanes": {"span": 2}, "cpu": {"hidden": True}}})
        board.layout_set("project", self.p, {"order": ["tests", "lanes"], "panels": {"lanes": {"span": 3, "height": 2}}})
        got = board.layout_view(self.p)
        self.assertEqual(got["order"], ["tests", "lanes"] + [k for k in KEYS if k not in ("tests", "lanes")],
                         "a partial order puts the rest after it, in the page's order")
        self.assertEqual(got["panels"]["lanes"], {"span": 3, "height": 2, "hidden": False}, "the project's span wins")
        self.assertTrue(got["panels"]["cpu"]["hidden"], "the global hidden stands where the project says nothing")
        self.assertEqual((got["style"], got["from"]), ("flat", "project"))
        other = board.layout_view(None)
        self.assertEqual((other["panels"]["lanes"]["span"], other["order"], other["from"]), (2, KEYS, "global"))
        board.layout_set("project", self.p, {"panels": {"lanes": {"hidden": True}}})
        self.assertEqual(board.layout_view(self.p)["panels"]["lanes"], {"span": 3, "height": 2, "hidden": True},
                         "set merges into the scope's file")
        board.layout_reset("project", self.p)
        self.assertEqual(board.layout_view(self.p)["panels"]["lanes"]["span"], 2)
        board.layout_reset("global", None)
        self.assertEqual(board.layout_view(self.p)["from"], "builtin")
        board.layout_reset("global", None)  # a second reset is a no-op

    def test_replace_writes_the_whole_layout_the_page_sent(self):
        board.layout_set("project", self.p, {"panels": {"lanes": {"span": 3}}, "style": "flat"})
        board.layout_set("project", self.p, {"order": list(reversed(KEYS))}, replace=True)
        got = board.layout_view(self.p)
        self.assertEqual((got["order"], got["panels"]["lanes"]["span"], got["style"]), (list(reversed(KEYS)), 1, "card"))

    def test_a_bad_layout_is_refused_naming_the_field_and_nothing_is_written(self):
        bad = [({"panels": {"lanes": {"span": 4}}}, "panels.lanes.span"),
               ({"panels": {"lanes": {"span": True}}}, "panels.lanes.span"),
               ({"panels": {"lanes": {"height": 0}}}, "panels.lanes.height"),
               ({"panels": {"lanes": {"hidden": "yes"}}}, "panels.lanes.hidden"),
               ({"panels": {"lanes": {"width": 2}}}, "panels.lanes.width"),
               ({"panels": {"nope": {}}}, "panels.nope"),
               ({"panels": []}, "panels"),
               ({"order": ["lanes", "lanes"]}, "order"),
               ({"order": ["lanes", "x"]}, r"order\[1\]"),
               ({"order": "lanes"}, "order"),
               ({"style": "neon"}, "style"),
               ({"motion": 1}, "motion"),
               ({"version": 2}, "version"),
               ({"colour": "red"}, "colour"),
               ("lanes", "layout"),
               ({"order": KEYS, "panels": {k: {"span": 1} for k in KEYS}, "pad": "x" * 5000}, "layout")]
        for body, field in bad:
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, rf"^{field}(?![\w.\[])"):
                board.layout_set("project", self.p, body)
        self.assertFalse(os.path.exists(board.layout_path("project", self.p)), "a refused layout writes nothing")

    def test_a_hand_broken_file_is_skipped_and_named(self):
        path = board.layout_path("project", self.p)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write('{"panels": {"lanes": {"span": 7}}}')
        board.layout_set("global", None, {"style": "flat"})
        got = board.layout_view(self.p)
        self.assertEqual((got["style"], got["panels"]["lanes"]["span"], got["from"]), ("flat", 1, "global"))
        self.assertEqual(len(got["errors"]), 1)
        self.assertIn("project", got["errors"][0])
        self.assertIn("panels.lanes.span", got["errors"][0])
        with open(path, "w") as f:
            f.write("not json")
        self.assertIn("project", board.layout_view(self.p)["errors"][0])

    def test_a_deeply_nested_layout_file_is_skipped_not_a_crash(self):
        path = board.layout_path("project", self.p)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("[" * 3000)
        from unittest import mock  # this Python may parse 3000 deep: make json hit its limit as an older one does
        with mock.patch.object(board.json, "loads", side_effect=RecursionError):
            got = board.layout_view(self.p)
            self.assertIn("nested too deep", got["errors"][0])
            self.assertIn("layout", live.board(self.p))
        with mock.patch.object(board, "layout_view", side_effect=RecursionError):
            self.assertIn("errors", live.board(self.p)["layout"])

    def test_the_snapshot_carries_the_layout(self):
        board.layout_set("project", self.p, {"panels": {"tests": {"hidden": True}}})
        s = live.snapshot(self.p)
        self.assertEqual(set(s["board"]), PARTS | {"layout"})
        self.assertTrue(s["board"]["layout"]["panels"]["tests"]["hidden"])
        self.assertEqual(live.snapshot_all(self.p)["board"]["layout"]["from"], "builtin",
                         "the All view reads the global layout")

    def test_the_verb_shows_sets_and_resets_each_scope(self):
        self.fm("board", "layout", "set", "lanes.span=2", "cpu.hidden=true", "order=tests,lanes", "motion=still")
        self.fm("board", "layout", "set", "--project", "lanes.height=3", "style=flat")
        got = self.fm_json("board", "layout", "show")
        self.assertEqual(got["order"][:2], ["tests", "lanes"])
        self.assertEqual(got["panels"]["lanes"], {"span": 2, "height": 3, "hidden": False})
        self.assertEqual((got["motion"], got["style"], got["from"]), ("still", "flat", "project"))
        self.assertEqual(self.fm_json("board", "layout", "show", "--global")["style"], "card", "--global: that scope alone")
        text = self.fm("board", "layout", "show").stdout
        self.assertIn("tests", text)
        self.assertIn("hidden: cpu", text)
        bad = self.fm("board", "layout", "set", "lanes.span=9", check=False)
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("panels.lanes.span", bad.stderr)
        bad = self.fm("board", "layout", "set", "nope", check=False)
        self.assertNotEqual(bad.returncode, 0)
        self.fm("board", "layout", "set", "--layout", '{"panels": {"state": {"span": 3}}}')
        self.assertEqual(self.fm_json("board", "layout", "show")["panels"]["state"]["span"], 3)
        self.fm("board", "layout", "reset", "--project")
        self.assertEqual(self.fm_json("board", "layout", "show")["from"], "global")
        self.fm("board", "layout", "reset")
        self.assertEqual(self.fm_json("board", "layout", "show")["from"], "builtin")

    def test_a_bad_layout_json_beside_pairs_is_a_usage_error_not_a_traceback(self):
        for bad in ('{"panels": 5}', '{"panels": {"lanes": 5}}', '[1]'):
            got = self.fm("board", "layout", "set", "--layout", bad, "lanes.span=2", check=False)
            self.assertNotEqual(got.returncode, 0, bad)
            self.assertNotIn("Traceback", got.stderr, bad)
            self.assertIn("layout", got.stderr + got.stdout, bad)

    def test_the_wide_asks_rule_yields_to_a_span_of_three(self):
        css = read_text(os.path.join(WEB, "board.css"))
        self.assertRegex(css, r'section\.bd-p\[data-wide\]:not\(\[data-span="3"\]\)\{grid-column:span 2\}')
        self.assertNotRegex(css, r'section\.bd-p\[data-wide\]\{grid-column')

    def test_no_text_says_nine_panels(self):
        for rel in (("lib", "sampo", "board.py"), ("web", "live", "board.js")):
            self.assertNotRegex(read_text(os.path.join(ROOT, "plugin", *rel)).lower(), r"\ball nine|nine-panel")

    def test_the_pages_layout_reader_mends_anything_into_nine_panels(self):
        node = shutil.which("node") or self.skipTest("no node to run the page's code")
        js = ("const B = require(process.argv[1]); const out = [B.layoutOf(null), B.layoutOf({order: ['tests', 'x', "
              "'tests', 'cpu'], panels: {cpu: {span: 9, height: -1, hidden: 'y'}, tests: {span: 2, hidden: true}}, "
              "style: 'flat', motion: 'still'}), B.layoutOf('junk'), "
              "B.layoutMove(B.layoutOf(null), 'cpu', 0).order, B.layoutMove(B.layoutOf(null), 'tests', 99).order, "
              "[B.snapSpan(100, 400, 16, 3), B.snapSpan(700, 400, 16, 3), B.snapSpan(5000, 400, 16, 3), "
              "B.snapSpan(900, 400, 16, 2), B.snapSpan(-50, 400, 16, 3)], "
              "[B.snapHeight(100, 280), B.snapHeight(500, 280), B.snapHeight(9000, 280)]]; "
              "console.log(JSON.stringify(out))")
        out = subprocess.run([node, "-e", js, os.path.join(WEB, "board.js")], capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        empty, mixed, junk, first, last, spans, heights = json.loads(out.stdout)
        self.assertEqual((empty["order"], empty["style"], empty["motion"]), (KEYS, "card", "full"))
        self.assertEqual(junk, empty)
        self.assertEqual(mixed["order"], ["tests", "cpu"] + [k for k in KEYS if k not in ("tests", "cpu")])
        self.assertEqual(mixed["panels"]["cpu"], {"span": 3, "height": 1, "hidden": False}, "clamped; not-a-bool is shown")
        self.assertEqual(mixed["panels"]["tests"], {"span": 2, "height": 1, "hidden": True})
        self.assertEqual((mixed["style"], mixed["motion"]), ("flat", "still"))
        self.assertEqual(first[0], "cpu")
        self.assertEqual(last[-1], "tests")
        self.assertEqual(spans, [1, 2, 3, 2, 1], "a drag snaps to whole columns, at most the grid's")
        self.assertEqual(heights, [1, 2, 3])
