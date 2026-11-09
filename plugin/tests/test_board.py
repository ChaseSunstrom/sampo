"""T-3659: the Board, the live page's modern default: nine mechanism panels drawn from Sampo's own state. The server
side (sampo.board) collects each panel's facts cheaply and redacted; the page side (board.js boardModel) maps any
snapshot, empty or oversized, to one plain model of nine panels, run here under node."""
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
PARTS = {"lanes", "train", "cpu", "loop", "library", "commands", "deps", "state", "tests"}
KEYS = ["lanes", "train", "cpu", "loop", "library", "models", "sampo", "state", "tests"]


class Brief:
    """Just what the lanes part reads of a brief."""

    def __init__(self, id_, status="active", **meta):
        self.id, self.title, self.status, self.meta = id_, f"Task {id_}", status, meta

    def steps(self):
        return []


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
        self.assertEqual(set(s["board"]), PARTS)
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


NODE = r"""
const B = require(process.argv[1]);
const fs = require("fs");
const out = JSON.parse(fs.readFileSync(process.argv[2], "utf8")).map(([snap, hist]) => {
  const m = B.boardModel(snap, hist);
  let bad = 0;
  const text = JSON.stringify(m, (k, v) => { if (typeof v === "number" && !isFinite(v)) bad++; return v; });
  return {m: JSON.parse(text), bad};
});
console.log(JSON.stringify(out));
"""


CTX = r"""
// a 2D context that records what a real canvas would throw on (negative radii, stops off [0, 1]) or draw nowhere (NaN)
function ctx() {
  const st = {ops: 0, bad: [], texts: []}, o = {};
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
      if (k === "fillText") return (t, ...a) => { st.ops++; check(k, a); st.texts.push(String(t)); };
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


def big_snapshot():
    s = json.loads(read_text(os.path.join(FIX, "snap-5.json")))
    b = s["board"]
    b["lanes"] = [{"task": f"T-{i}", "title": "x" * 300, "state": "building", "more": 0,
                   "files": [{"path": f"plugin/f{j}.py", "hot": j % 7 == 0} for j in range(100)]} for i in range(50)]
    b["commands"] = [{"at": "2026-11-07T10:00:00Z", "cmd": "sampo next", "ok": True}] * 500
    b["cpu"]["minutes"] = [[1_800_000_000 + 60 * i, 1, 1, 1, 1, 1] for i in range(500)]
    b["tests"]["modules"] = [{"name": f"test_{i}", "state": "cached"} for i in range(900)]
    b["deps"] = [[f"T-{i}", f"T-{i + 1}"] for i in range(400)]
    s["stats"]["cards"]["general"], s["stats"]["moons"]["candidates"] = 10 ** 7, 10 ** 6
    s["queue"] = s["queue"] * 100
    s["flows"] = s["flows"] * 300
    return s


class Model(SampoTestCase):
    """boardModel under node: the page's one mapping from a snapshot (and the ones before it) to nine panels."""

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

    def test_empty_typical_and_oversized_snapshots_make_nine_bounded_panels(self):
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
        self.assertLessEqual(len(json.dumps(big["m"])), 200_000, "the model stays small whatever the snapshot")
        t = by(typ)
        self.assertGreater(sum(t["library"]["dots"].values()), 0)
        self.assertTrue(t["lanes"]["clusters"])
        self.assertTrue(t["models"]["links"])
        self.assertTrue(t["sampo"]["commands"])
        self.assertEqual(by(empty)["loop"], None)

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
