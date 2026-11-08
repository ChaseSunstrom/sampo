"""T-3333 (Fleet): Kalevala across machines. Towers are claims (tower:<name>); only the holder steps a tower; exam.jsonl,
the acts log and steers merge by line, each line once. The claims remote is a file:// bare repo."""
import json
import os
import subprocess
import tempfile
from unittest import mock

from helpers import CLEAN_READER, ForemanTestCase, read_text
from sampo import core as c


def setUpModule():
    os.environ["PATH"] = CLEAN_READER + os.pathsep + os.environ["PATH"]


def tearDownModule():
    os.environ["PATH"] = os.environ["PATH"].replace(CLEAN_READER + os.pathsep, "", 1)


def git(cwd, *a):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, check=True, capture_output=True)


class KalevalaFleet(ForemanTestCase):
    def setUp(self):
        super().setUp()
        from sampo import budget, claims
        self.fm("init")
        root = "root charter\n\n## 2. Capabilities\n1. **Bugs.** finds and fixes broken code. Exam: bug.\n"
        path = os.path.join(self.tmp, "gen0.md")
        with open(path, "w") as f:
            f.write(root)
        self.fm("genesis", "init", "--file", path)
        self.fm("kalevala", "start")
        budget.RUN["usd"] = 0.0
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        bare = os.path.join(t.name, "r.git")
        git(t.name, "init", "--bare", bare)
        src = os.path.join(t.name, "src")
        git(t.name, "init", src)
        git(src, "commit", "--allow-empty", "-m", "root")
        self.clones = {}
        for n in ("a", "b"):
            d = os.path.join(t.name, n)
            git(t.name, "clone", "-q", src, d)
            self.clones[n] = claims.Claims(d, remote="file://" + bare, session=n)

    def project(self):
        return c.find_project(self.repo, create=False)

    def step_as(self, who):
        from sampo import kalevala
        calls = []
        with mock.patch.object(kalevala, "_claims", lambda p: self.clones[who]):
            res = kalevala.step(self.project(), k=1, runner=lambda argv, cwd, env: calls.append(argv[-1]) or 0)
        return res, calls

    def test_each_machine_steps_a_different_tower_and_names_the_holder_it_skips(self):
        from sampo import kalevala
        out = {}

        def run_a(argv, cwd, env):  # b's whole round happens while a's job runs, so a still holds its claim
            out["b"] = self.step_as("b")
            return 0
        with mock.patch.object(kalevala, "_claims", lambda p: self.clones["a"]):
            kalevala.step(self.project(), k=1, runner=run_a)
        rb, cb = out["b"]
        self.assertEqual(len(cb), 1)
        held = [r for r in rb["results"] if r["status"] == "held"]
        self.assertEqual(len(held), 1)
        self.assertNotEqual(cb[0], held[0]["tower"])
        self.assertEqual({r["holder"] for r in held}, {self.clones["a"].me["id"]})

    def test_no_claims_remote_behaves_as_before(self):
        from sampo import kalevala
        calls = []
        with mock.patch.object(kalevala, "_claims", lambda p: None):
            res = kalevala.step(self.project(), runner=lambda a, cw, e: calls.append(a) or 0)
        self.assertEqual(len([r for r in res["results"] if r["status"] == "ran"]), len(calls))
        self.assertFalse([r for r in res["results"] if r["status"] in ("held", "offline")])

    def test_offline_a_machine_steps_only_the_towers_it_holds(self):
        import time
        from sampo import kalevala
        p = self.project()
        mine = kalevala.tower_rows(p)[0]["name"]
        self.assertEqual(self.clones["a"].take(f"tower:{mine}")["result"], "taken")
        kalevala._remember(p, mine, time.time() + 600)
        self.clones["a"].remote = "file:///nonexistent/x.git"
        res, calls = self.step_as("a")
        self.assertEqual(calls, [mine])
        self.assertTrue([r for r in res["results"] if r["status"] == "offline"])

    def test_a_job_longer_than_the_ttl_keeps_its_claim_and_it_is_released_after(self):
        import time
        from sampo import claims, kalevala
        p = self.project()
        seen = []

        def run(argv, cwd, env):
            time.sleep(3.5)  # past the 1 s TTL and 1 s slack: only a renewal keeps the claim
            seen.append(self.clones["b"].take(f"tower:{argv[-1]}")["result"])
            return 0
        with mock.patch.object(claims, "TTL", 1), mock.patch.object(claims, "SLACK", 1), \
                mock.patch.object(kalevala, "RENEW_TICK", 0.2), mock.patch.object(kalevala, "_claims", lambda p: self.clones["a"]):
            kalevala.step(p, k=1, runner=run)
        self.assertEqual(seen, ["held"])
        self.assertEqual([x for x in self.clones["b"].list() if x["key"].startswith("tower:")], [])

    def test_fleet_mode_needs_the_env_var(self):
        from sampo import kalevala
        os.environ.pop("FOREMAN_CLAIMS_REMOTE", None)
        self.assertIsNone(kalevala._claims(self.project()))

    def test_steers_append_missing_lines_in_local_order_and_big_logs_import(self):
        from sampo import sync
        self.fm("sync", "on")
        p = self.project()
        root = sync.mirror(p)
        local = os.path.join(p.dir, "research/genesis/towers/memory/steers.md")
        os.makedirs(os.path.dirname(local), exist_ok=True)
        with open(local, "w") as f:
            f.write("- zeta\n- alpha\n")
        sync.export(p)
        with open(os.path.join(root, "research/genesis/towers/memory/steers.md"), "a") as f:
            f.write("- beta\n")
        big = os.path.join(root, "research/genesis/exam.jsonl")
        with open(big, "w") as f:
            f.write("".join('{"at": "2026-01-01T00:00:%02dZ", "n": %d, "pad": "%s"}\n' % (i % 60, i, "x" * 3000) for i in range(500)))
        self.fm("sync", "import")
        self.assertEqual(read_text(local), "- zeta\n- alpha\n- beta\n")
        self.assertEqual(len(read_text(os.path.join(p.dir, "research/genesis/exam.jsonl")).splitlines()), 500)
        self.assertIn("*.jsonl merge=union", read_text(os.path.join(root, ".gitattributes")))

    def test_lines_merge_once_in_one_order_whatever_the_side(self):
        from sampo import sync
        x = '{"at": "2026-01-02T00:00:00Z", "n": 1}\n{"at": "2026-01-01T00:00:00Z", "n": 0}\n'
        y = '{"at": "2026-01-01T00:00:00Z", "n": 0}\n{"at": "2026-01-03T00:00:00Z", "n": 2}\n'
        m = sync.merge_lines(x, y)
        self.assertEqual(m, sync.merge_lines(y, x))
        self.assertEqual(len(m.splitlines()), 3)
        self.assertEqual(sync.merge_lines(m, x), m)

    def test_exam_and_acts_travel_and_merge_on_import(self):
        from sampo import kalevala, rune, sync
        self.fm("sync", "on")
        p = self.project()
        kalevala._act(p, "slot", "memory", "mine", undo="none")
        with open(os.path.join(rune.root(p), "exam.jsonl"), "w") as f:
            f.write('{"at": "2026-01-01T00:00:00Z", "agent": "x"}\n')
        sync.export(p)
        root = sync.mirror(p)
        theirs = {"id": "other-1", "act": "slot", "tower": "ahti", "reason": "theirs", "undo": "none",
                  "at": "2026-01-02T00:00:00Z"}
        with open(os.path.join(root, "research/genesis/kalevala/acts.jsonl"), "a") as f:
            f.write(json.dumps(theirs) + "\n")
        with open(os.path.join(root, "research/genesis/exam.jsonl"), "a") as f:
            f.write('{"at": "2026-01-02T00:00:00Z", "agent": "y"}\n')
        self.fm("sync", "import")
        self.fm("sync", "import")  # a second import changes nothing
        self.assertEqual(sorted(a["tower"] for a in kalevala.acts(p) if a["act"] == "slot"), ["ahti", "memory"])
        self.assertEqual(len(read_text(os.path.join(rune.root(p), "exam.jsonl")).splitlines()), 2)

    def test_a_redacted_line_reads_as_ours_on_import(self):  # T-3508 x T-3333: the mirror shows home as ~
        from sampo import rune, sync
        self.fm("sync", "on")
        p = self.project()
        local = os.path.join(rune.root(p), "exam.jsonl")
        mirrored = os.path.join(sync.mirror(p), "research/genesis/exam.jsonl")
        with mock.patch.dict(os.environ, {"HOME": "/srv/u/alice"}):
            with open(local, "w") as f:
                f.write('{"at": "2026-01-01T00:00:00Z", "cwd": "/srv/u/alice/proj"}\n')
            sync.export(p)
            self.assertNotIn("/srv/u/alice", read_text(mirrored))
            with open(mirrored, "a") as f:
                f.write('{"at": "2026-01-02T00:00:00Z", "agent": "y"}\n')
            sync.import_(p)
        self.assertEqual(len(read_text(local).splitlines()), 2, "our line once, theirs once")
