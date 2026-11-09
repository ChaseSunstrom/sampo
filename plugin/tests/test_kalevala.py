"""Kalevala part 1 (T-3053): many Rune towers in one project. Each tower is its own home (research/genesis/towers/NAME);
the root tower behaves as before; moons, exam scenarios and the user's steers stay at the root for every tower."""
# map-links: plugin/lib/sampo/night.py plugin/lib/sampo/moonscale.py
import contextlib
import io
import json
import os
import threading
import time
from unittest import mock

from helpers import CLEAN_READER, ForemanTestCase, read_text
from sampo import core as c


def setUpModule():
    os.environ["PATH"] = CLEAN_READER + os.pathsep + os.environ["PATH"]


def tearDownModule():
    os.environ["PATH"] = os.environ["PATH"].replace(CLEAN_READER + os.pathsep, "", 1)


class Kalevala(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")

    def gen0(self, name, text):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as f:
            f.write(text)
        return path

    def project(self):
        return c.find_project(self.repo, create=False)

    def test_a_tower_seals_its_own_gen0_and_leaves_the_root_alone(self):
        res = self.fm_json("genesis", "--tower", "ahti", "init", "--file", self.gen0("a.md", "water tower\n"))
        self.assertTrue(res["dir"].endswith(os.path.join("research", "genesis", "towers", "ahti")), res["dir"])
        self.assertEqual(read_text(os.path.join(res["dir"], "gen-0.md")), "water tower\n")
        root = self.fm_json("genesis", "status")
        self.assertFalse(root["gen0"])  # the root has no gen-0 yet
        self.assertTrue(self.fm_json("genesis", "--tower", "ahti", "status")["gen0"])
        # the root seals a different gen-0: a tower's seal (ledger) never stands for it
        self.fm("genesis", "init", "--file", self.gen0("r.md", "root tower\n"))
        self.assertEqual(self.fm_json("genesis", "--tower", "ahti", "status")["gen0"], True)
        import hashlib
        self.assertEqual(self.fm_json("genesis", "status")["seal"], hashlib.sha256(b"root tower\n").hexdigest()[:8])
        self.assertEqual(self.fm_json("genesis", "--tower", "ahti", "status")["seal"],
                         hashlib.sha256(b"water tower\n").hexdigest()[:8])
        # a second tower init is refused on that tower only
        again = self.fm("genesis", "--tower", "ahti", "init", "--file", self.gen0("b.md", "other\n"), check=False)
        self.assertNotEqual(again.returncode, 0)
        self.fm("genesis", "--tower", "tapio", "init", "--file", self.gen0("c.md", "forest tower\n"))

    def test_a_tower_files_docs_and_steps_in_its_own_home(self):
        self.fm("genesis", "--tower", "ahti", "init", "--file", self.gen0("a.md", "water tower\n"))
        step = self.fm_json("genesis", "--tower", "ahti", "step")
        self.assertIn("seed", json.dumps(step))
        res = self.fm_json("genesis", "--tower", "ahti", "put", "gap-map.md", "--file", self.gen0("g.md", "# Gap map\n"))
        self.assertIn(os.path.join("towers", "ahti", "gap-map.md"), res["path"])
        root = os.path.join(self.project().dir, "research", "genesis")
        self.assertFalse(os.path.exists(os.path.join(root, "gap-map.md")))
        self.assertFalse(os.path.exists(os.path.join(root, "gen-0.md")))

    def test_the_shared_stores_stay_at_the_root_and_a_tower_steer_file_adds(self):
        from sampo import moonscale, rune
        p = self.project()
        rune.set_tower("ahti")
        try:
            self.assertEqual(rune.root(p), os.path.join(p.dir, "research", "genesis"))
            self.assertEqual(rune.home(p), os.path.join(rune.root(p), "towers", "ahti"))
            self.assertEqual(moonscale._dir(p), os.path.join(rune.root(p), "moons"))  # moons: shared
            from sampo import steer
            self.assertEqual(steer._path(p, "steer.jsonl"), os.path.join(rune.root(p), "steer.jsonl"))
            os.makedirs(rune.home(p))
            with open(os.path.join(rune.home(p), "steers.md"), "w") as f:
                f.write("- keep it watery\n")
            self.assertIn("keep it watery", " ".join(rune.tower_steers(p)))
        finally:
            rune.set_tower(None)
        self.assertEqual(rune.home(p), rune.root(p))
        self.assertNotIn("keep it watery", " ".join(rune.tower_steers(p)))  # the root's file isn't the tower's

    def test_the_towers_list_and_bad_names(self):
        self.fm("genesis", "--tower", "ahti", "init", "--file", self.gen0("a.md", "water tower\nmore\n"))
        self.fm("genesis", "--tower", "tapio", "init", "--file", self.gen0("c.md", "forest tower\n"))
        rows = self.fm_json("genesis", "towers")["towers"]
        by = {r["name"]: r for r in rows}
        self.assertEqual({"ahti", "tapio"}, set(by))
        self.assertEqual(by["ahti"]["aspect"], "water tower")
        self.assertEqual(by["ahti"]["generation"], 0)
        self.assertIsNone(by["ahti"]["select"])
        self.assertIsNone(by["ahti"]["drift"])
        os.makedirs(os.path.join(self.project().dir, "research", "genesis", "towers", "x-"))  # stray folders and files
        open(os.path.join(self.project().dir, "research", "genesis", "towers", "note"), "w").close()
        os.makedirs(os.path.join(self.project().dir, "research", "genesis", "towers", "root"))
        self.assertEqual({"ahti", "tapio"}, {r["name"] for r in self.fm_json("genesis", "towers")["towers"]})

    def test_a_bad_tower_name_is_refused_and_makes_no_folder(self):
        for bad in ("../x", "Root", "a/b", "root", "", "-x", "x-", "x" * 40):
            r = self.fm("genesis", "--tower", bad, "init", "--file", self.gen0("a.md", "x\n"), check=False)
            self.assertNotEqual(r.returncode, 0, bad)
        self.assertFalse(os.path.exists(os.path.join(self.project().dir, "research", "genesis", "towers")))

    def test_events_are_tagged_and_towers_shows_their_verdicts(self):
        from sampo import rune
        p = self.project()
        rune.set_tower("ahti")
        try:
            os.makedirs(os.path.join(rune.home(p), "gen-2"))
            rune._log(p, "genesis_select", data={"gen": 2, "verdict": "kept"})
            rune._log(p, "genesis_drift", data={"gen": 2, "verdict": "clean"})
        finally:
            rune.set_tower(None)
        c.log_event(p, "genesis_select", data={"gen": 1, "verdict": "reverted"})
        evs = [e for e in c.ledger_events(p, kinds=["genesis_select", "genesis_drift"])]
        self.assertEqual([(e["event"], (e["data"]).get("tower")) for e in evs],
                         [("genesis_select", "ahti"), ("genesis_drift", "ahti"), ("genesis_select", None)])
        self.assertEqual(rune._events(p, ["genesis_select"])[0]["data"]["verdict"], "reverted")  # root sees only its own
        os.makedirs(os.path.join(rune.root(p), "towers", "ahti"), exist_ok=True)
        got = {r["name"]: r for r in rune.towers(p)}["ahti"]
        self.assertEqual((got["generation"], got["select"], got["drift"]), (2, "kept", "clean"))
        self.assertIsNone(rune._TOWER)

    def test_a_towers_steers_reach_its_prompts_and_the_roots_never_do(self):
        from sampo import rune
        p = self.project()
        rune.set_tower("ahti")
        try:
            os.makedirs(rune.home(p))
            with open(os.path.join(rune.home(p), "steers.md"), "w") as f:
                f.write("- keep it watery\n")
            self.assertIn("keep it watery", rune._charter(p, "goal\nMUST: x.\n"))
            self.assertIn("keep it watery", rune._breakable(p, "goal\n"))
        finally:
            rune.set_tower(None)
        self.assertNotIn("keep it watery", rune._charter(p, "goal\nMUST: x.\n"))

    def test_publish_export_and_live_are_refused_under_a_tower(self):
        self.fm("genesis", "--tower", "ahti", "init", "--file", self.gen0("a.md", "water\n"))
        for verb, extra in (("publish", []), ("export", [os.path.join(self.tmp, "out")]), ("live", ["on"])):
            r = self.fm("genesis", "--tower", "ahti", verb, *extra, check=False)
            self.assertNotEqual(r.returncode, 0, verb)
            self.assertIn("root tower", r.stderr + r.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "out")))

    def test_exam_lines_are_per_tower_and_the_lean_record_is_keyed_by_tower(self):
        from sampo import louhi, rune
        p = self.project()
        self.fm("genesis", "--tower", "ahti", "init", "--file", self.gen0("a.md", "water\n"))
        self.fm("genesis", "init", "--file", self.gen0("r.md", "root\n"))
        with open(os.path.join(rune.home(p), "exam.jsonl"), "w") as f:
            f.write('{"run": "r1"}\n')
        self.assertEqual(self.fm_json("genesis", "status")["exam_lines"], 1)
        self.assertEqual(self.fm_json("genesis", "--tower", "ahti", "status")["exam_lines"], 0)  # its own lines, none yet
        ov = {k: 1 for k in louhi.LEAN_METRICS}
        louhi._lean_grew(p, "r", "agent", "s1", ov)
        rune.set_tower("ahti")
        try:
            louhi._lean_grew(p, "r", "agent", "s1", ov)
        finally:
            rune.set_tower(None)
        with open(os.path.join(louhi.root(p), "lean-start.json")) as f:
            book = json.load(f)
        self.assertEqual(sorted(book), ["0", "ahti/0"])


def coverage_md(areas):
    """coverage.md: each (area, open) as a '## <area>' section. An open area leaves one cell empty; a closed one fills
    all 64 stage × surface cells with its own N/A reasons."""
    from sampo import rune
    out = []
    for area, is_open in areas:
        out.append(f"## {area}\n")
        for st in rune.STAGES:
            for sf in rune.SURFACES:
                if is_open and (st, sf) != (rune.STAGES[0], rune.SURFACES[0]):
                    continue
                out.append(f"- {st} × {sf}: N/A: {area} on {sf} at {st} is out of scope\n")
    return "".join(out)


class KalevalaStart(ForemanTestCase):
    """Kalevala part 2, step 1 (T-3081): sampo kalevala start opens a tower per aspect with open coverage plus the
    fixed three, each sealed from the root gen-0 and its aspect; status lists them."""

    def setUp(self):
        super().setUp()
        self.fm("init")
        self.root_text = "Sampo gen-0: the root tower's charter\n"
        self.fm("genesis", "init", "--file", self.write("gen0.md", self.root_text))

    def write(self, name, text):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as f:
            f.write(text)
        return path

    def put_coverage(self, areas):
        self.fm("genesis", "put", "coverage.md", "--file", self.write("cov.md", coverage_md(areas)))

    def tower_dir(self, name):
        return os.path.join(c.find_project(self.repo, create=False).dir, "research", "genesis", "towers", name)

    def test_start_opens_a_tower_per_open_area_and_the_fixed_three(self):
        self.put_coverage([("Open area", True), ("Done area", False)])
        res = self.fm_json("kalevala", "start")
        self.assertEqual({t["aspect"] for t in res["towers"]}, {"Open area", "problem solving", "memory", "efficiency"})
        status = self.fm_json("kalevala", "status")
        self.assertEqual({t["aspect"] for t in status["towers"]}, {t["aspect"] for t in res["towers"]})
        self.assertTrue(all(t["sealed"] for t in status["towers"]))

    def test_each_tower_gen0_is_the_root_gen0_plus_its_aspect_and_sealed(self):
        import hashlib
        from sampo import rune
        self.put_coverage([("Open area", True)])
        self.fm_json("kalevala", "start")
        p = c.find_project(self.repo, create=False)
        for name, aspect in (("open-area", "Open area"), ("memory", "memory")):
            path = os.path.join(self.tower_dir(name), "gen-0.md")
            text = read_text(path)
            self.assertTrue(text.startswith(self.root_text), name)
            self.assertIn(aspect, text)
            self.assertEqual(self.fm_json("genesis", "--tower", name, "status")["seal"],
                             hashlib.sha256(text.encode()).hexdigest()[:8])
            self.assertEqual(os.stat(path).st_mode & 0o222, 0, f"{name} gen-0 is read-only")
            rune.set_tower(name)
            try:
                self.assertEqual(rune.sealed(p), "")
            finally:
                rune.set_tower(None)

    def test_a_second_start_opens_nothing_and_rewrites_no_gen0(self):
        self.put_coverage([("Open area", True)])
        first = self.fm_json("kalevala", "start")
        path = os.path.join(self.tower_dir("memory"), "gen-0.md")
        before = read_text(path)
        second = self.fm_json("kalevala", "start")
        self.assertEqual(second["opened"], [])
        self.assertEqual(len(second["towers"]), len(first["towers"]))
        self.assertEqual(len(self.fm_json("kalevala", "status")["towers"]), len(first["towers"]))
        self.assertEqual(read_text(path), before)

    def test_aspect_names_are_slugged_unique_and_never_root(self):
        from sampo import rune
        self.put_coverage([("Problem Solving", True), ("Root", True), ("Efficiency!!", True)])
        res = self.fm_json("kalevala", "start")
        names = [t["name"] for t in res["towers"]]
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(all(rune._valid(n) for n in names), names)
        self.assertEqual(set(names), {"problem-solving", "aspect-root", "efficiency", "memory"})
        self.assertIn("Efficiency!!", {t["aspect"] for t in res["towers"]})  # the coverage area keeps its own text


class KalevalaNoRootGen0(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")

    def test_start_refuses_without_a_sealed_root_gen0_and_status_lists_nothing(self):
        r = self.fm("kalevala", "start", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("genesis init", r.stderr + r.stdout)
        self.assertFalse(os.path.exists(os.path.join(c.find_project(self.repo, create=False).dir, "research", "genesis", "towers")))
        self.assertEqual(self.fm_json("kalevala", "status")["towers"], [])


class KalevalaStep(ForemanTestCase):
    """Kalevala part 2, step 2 (T-3081): sampo kalevala step runs each tower's next genesis step as a job, at most K
    at once, one step per tower at a time, inside Kalevala's own budget share; the moons store is locked and the exam
    scenarios are read-only for a tower job."""

    def setUp(self):
        super().setUp()
        self.fm("init")
        root = "root charter\n\n## 2. Capabilities\n1. **Bugs.** finds and fixes broken code. Exam: bug.\n"
        self.fm("genesis", "init", "--file", self.write("gen0.md", root))  # §2 gives each tower a spec job to run
        self.fm("kalevala", "start")  # the fixed three: efficiency, memory, problem-solving
        from sampo import budget
        budget.RUN["usd"] = 0.0  # the in-process runs' spend never leaks into the next test's per-command cap

    def write(self, name, text):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as f:
            f.write(text)
        return path

    def project(self):
        return c.find_project(self.repo, create=False)

    def fake(self, rc=0):
        calls = []

        def run(argv, cwd, env):
            calls.append((list(argv), dict(env)))
            return rc
        return run, calls

    def hold_store(self, release):
        """A thread holds the moons store until `release` is set; returns the thread once it holds the store."""
        from sampo import moonscale
        held = threading.Event()

        def hold():
            with moonscale.store_lock(self.project()):
                held.set()
                release.wait(10)
        t = threading.Thread(target=hold)
        t.start()
        held.wait(10)
        return t

    def test_each_tower_runs_its_next_step_as_one_job_with_an_allowed_verb(self):
        from sampo import kalevala
        run, calls = self.fake()
        res = kalevala.step(self.project(), runner=run)
        ran = [r for r in res["results"] if r["status"] == "ran"]
        self.assertTrue(ran, res)
        self.assertEqual(len(calls), len(ran))
        for argv, env in calls:  # genesis VERB [N] --tower NAME: seed and select take the generation the CLI needs
            i = argv.index("genesis")
            self.assertIn(argv[i + 1], {"spec", "seed", "select"})
            if argv[i + 1] != "spec":
                self.assertTrue(argv[i + 2].isdigit(), argv)
            self.assertEqual(argv[-2], "--tower")
            self.assertIn(argv[-1], {"problem-solving", "memory", "efficiency"})
            self.assertEqual(env["FOREMAN_TOWER_READONLY_EXAM"], "1")
            self.assertEqual(env["FOREMAN_NO_BACKGROUND"], "1")
        self.assertEqual(sorted(argv[-1] for argv, _ in calls), sorted(r["tower"] for r in ran))

    def test_a_stage_the_loop_does_not_run_is_left_to_the_main_thread(self):
        from sampo import kalevala
        self.assertIsNone(kalevala.job_argv("memory", {"stage": "build", "gen": 1, "command": "fm genesis build",
                                                        "kind": "ok"}))
        self.assertIsNone(kalevala.job_argv("memory", {"stage": "seed", "gen": 1, "kind": "fatal",
                                                        "command": "fm genesis put gen-1/seed.md --file -"}))

    def test_job_argv_takes_the_generation_the_command_names(self):
        from sampo import kalevala

        def argv(command, gen=3):  # the act's gen is the target: seed 3 writes gen-3/seed.md
            return kalevala.job_argv("memory", {"stage": command.split()[2], "gen": gen, "command": command,
                                                "kind": "ok"})
        self.assertEqual(argv("fm genesis seed 3")[-5:], ["genesis", "seed", "3", "--tower", "memory"])
        self.assertEqual(argv("fm genesis select 2", gen=2)[-5:], ["genesis", "select", "2", "--tower", "memory"])
        self.assertEqual(argv("fm genesis spec", gen=1)[-4:], ["genesis", "spec", "--tower", "memory"])

    def test_a_tower_spec_reuses_the_root_exam_and_writes_no_scenario(self):
        # T-3841: every tower's first step is genesis spec, and a tower job may not write exam scenarios (T-3081), so all
        # 26 towers failed it on 2026-11-06 and no Kalevala round ran after; the step tests' fake runner never ran one
        from sampo import louhi
        p = self.project()
        run = os.path.join(louhi.root(p), "s0-001")
        os.makedirs(run)
        scen = [{"id": f"b{i}", "kind": "bug", "split": "holdout" if i % 3 == 0 else "train"} for i in range(6)]
        with open(os.path.join(run, "run.json"), "w") as f:
            json.dump({"run": "s0-001", "seed": 0, "per_kind": 6, "langs": ["rust"], "scenarios": scen}, f)
        train = os.path.join(louhi.root(p), "s0-002")  # newer, same kinds, nothing held out: never the one graded
        os.makedirs(train)
        with open(os.path.join(train, "run.json"), "w") as f:
            json.dump({"run": "s0-002", "seed": 1, "scenarios": [dict(x, split="train") for x in scen]}, f)
        res = self.fm_json("genesis", "--tower", "memory", "spec", env={"FOREMAN_TOWER_READONLY_EXAM": "1"})
        self.assertEqual(res["run"], "s0-001", res)
        self.assertEqual((res["total"], res["held"]), (6, 2))
        self.assertIn(os.path.join("towers", "memory"), res["path"])
        self.assertEqual(louhi.runs(p), ["s0-001", "s0-002"], "no exam run written")
        self.assertEqual(res["langs"], ["rust"], "the run's own languages, which the coverage report parses back")
        spec = read_text(res["path"])
        self.assertIn("| Bugs | bug | 6 | 2 |", spec)
        self.assertIn("(the root's, read only; seed 0, 6 per kind, rust): a third", spec)
        from sampo import rune
        with mock.patch.dict(os.environ, {"FOREMAN_TOWER_READONLY_EXAM": "1"}), \
                mock.patch.object(rune, "_TOWER", "memory"), mock.patch.object(rune, "put", side_effect=ValueError("no")):
            with self.assertRaises(ValueError):
                rune.spec(p)
        self.assertTrue(os.path.isfile(os.path.join(run, "run.json")), "a refused put never removes the shared run")
        for r in (run, train):
            os.remove(os.path.join(r, "run.json"))  # no run covers its kinds: refused, and the shared folder stays
        r = self.fm("genesis", "--tower", "efficiency", "spec", env={"FOREMAN_TOWER_READONLY_EXAM": "1"}, check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no exam run", r.stderr + r.stdout)
        self.assertTrue(os.path.isdir(run))

    def test_a_failed_step_keeps_its_reason_and_status_shows_it(self):
        # T-3841: the manifest kept 26 failures as 'rc 1' and nothing else, so nobody learned the spec step's refusal
        from sampo import kalevala
        res = kalevala.step(self.project(), runner=lambda argv, cwd, env: (1, "fm: exam scenarios are read-only"))
        failed = [r for r in res["results"] if r["status"] == "failed"]
        self.assertTrue(failed, res)
        self.assertTrue(all(r["why"] == "fm: exam scenarios are read-only" for r in failed), failed)
        self.assertIn("last step: spec failed: fm: exam scenarios are read-only", self.fm("kalevala", "status").stdout)
        ok = kalevala.step(self.project(), runner=lambda argv, cwd, env: 0)  # an exit code alone still works
        self.assertTrue(all("why" not in r for r in ok["results"] if r["status"] == "ran"), ok)

    def test_a_round_where_every_job_fails_is_captured_once(self):
        # T-3841: 26 of 26 towers failed and nothing became work for two days
        from sampo import kalevala
        boom = lambda argv, cwd, env: (1, "fm: boom")  # noqa: E731
        kalevala.step(self.project(), runner=boom)
        kalevala.step(self.project(), runner=boom)
        caps = [b for b in c.load_briefs(self.project()) if b.title.startswith("Kalevala: every tower's step failed")]
        self.assertEqual(len(caps), 1, [b.title for b in c.load_briefs(self.project())])
        self.assertIn("fm: boom", caps[0].section("Raw request"))

    def test_at_most_k_towers_run_at_once(self):
        import threading
        import time
        from sampo import kalevala
        lock, active, peak = threading.Lock(), [0], [0]

        def slow(argv, cwd, env):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.3)
            with lock:
                active[0] -= 1
            return 0
        res = kalevala.step(self.project(), k=1, runner=slow)
        self.assertEqual(res["k"], 1)
        self.assertEqual(peak[0], 1)
        self.assertGreater(len([r for r in res["results"] if r["status"] == "ran"]), 1)

    def test_one_step_per_tower_at_a_time_and_a_held_tower_is_busy(self):
        from sampo import kalevala
        run, calls = self.fake()
        p = self.project()
        with kalevala.tower_lock(p, "memory"):
            res = kalevala.step(p, runner=run)
        self.assertEqual([r["tower"] for r in res["results"] if r["status"] == "busy"], ["memory"])
        self.assertNotIn("memory", [argv[-1] for argv, _ in calls])

    def test_plan_queues_the_rest_thinnest_first_and_refuses_with_no_share(self):
        from sampo import budget, kalevala
        rows = [{"name": "b", "generation": 3}, {"name": "a", "generation": 0}, {"name": "c", "generation": 1}]
        self.assertEqual(kalevala.order(rows), ["a", "c", "b"])
        self.assertEqual(kalevala.plan(["a", "c", "b"], slots=2, fit=2), (["a", "c"], ["b"], 2))
        self.assertEqual(kalevala.plan(["a", "c", "b"], slots=4, fit=5)[2], 3)
        with self.assertRaises(budget.BudgetError):
            kalevala.plan(["a"], slots=1, fit=0)

    def test_a_short_share_queues_the_thinnest_last(self):
        from sampo import budget
        budget.set_caps(day=1.0)  # Kalevala's share is half the day cap: two jobs at the fallback estimate
        res = self.fm_json("kalevala", "step", "--dry-run")
        self.assertEqual([r["tower"] for r in res["results"]], ["efficiency", "memory"])
        self.assertEqual(res["queued"], ["problem-solving"])

    def test_the_kalevala_share_is_its_own_and_refuses_past_it(self):
        from sampo import budget, kalevala
        p = self.project()
        budget.set_caps(day=1.0)
        budget.record(kalevala.FEATURE, usd=0.5, runs=1, project=p.slug, detail="an earlier round")
        run, calls = self.fake()
        with self.assertRaises(budget.BudgetError):
            kalevala.step(p, runner=run)
        self.assertEqual(calls, [])

    def test_a_tower_jobs_spend_is_charged_to_kalevala_once(self):
        from sampo import budget, kalevala
        p = self.project()
        envs = []

        def child(argv, cwd, env):  # a tower's own run: its spend is recorded in its own process, under the tag
            envs.append(dict(env))
            with mock.patch.dict(os.environ, env):
                budget.record("ideas", usd=0.2, runs=1, project=p.slug, detail="child run")
            return 0
        res = kalevala.step(p, runner=child)
        ran = [r for r in res["results"] if r["status"] == "ran"]
        self.assertTrue(ran)
        self.assertTrue(all(e.get("FOREMAN_SPEND_AS") == kalevala.FEATURE for e in envs), envs)
        self.assertEqual(kalevala.spent_today(), round(0.2 * len(ran), 4))

    def test_a_round_charges_each_tower_its_own_spend(self):
        """A real round: each tower's estimate is charged, settled, and its child's spend is tagged with its name, so
        the tower's spend is exactly its child's spend and the estimate and settlement net to zero per tower."""
        from sampo import budget, kalevala
        p = self.project()
        budget.set_caps(day=5.0)

        def child(argv, cwd, env):
            with mock.patch.dict(os.environ, env):
                budget.record("ideas", usd=0.2, runs=1, project=p.slug, detail="child run")
            return 0
        res = kalevala.step(p, runner=child)
        ran = [r["tower"] for r in res["results"] if r["status"] == "ran"]
        self.assertTrue(ran)
        rows = {t["name"]: t["spend"] for t in kalevala.watch(p)["towers"]}
        for n in rows:
            self.assertEqual(rows[n], 0.2 if n in ran else 0.0, (n, rows))
        self.assertEqual(kalevala.spent_today(), round(0.2 * len(ran), 4))

    def test_the_day_cap_bounds_the_towers_a_round_starts(self):
        from sampo import budget, kalevala
        p = self.project()
        budget.set_caps(day=1.0)
        budget.record("ideas", usd=0.6, runs=1, project=p.slug, detail="another feature today")
        res = kalevala.step(p, dry_run=True)  # 0.4 of the day is left: one job at the fallback estimate
        self.assertEqual([r["tower"] for r in res["results"] if r["status"] == "planned"], ["efficiency"])
        self.assertEqual(res["queued"], ["memory", "problem-solving"])

    def test_the_per_command_cap_bounds_the_towers_a_round_starts(self):
        from sampo import budget, kalevala
        budget.set_caps(run=0.3)  # one job at the fallback estimate fits the command's cap, two do not
        res = kalevala.step(self.project(), dry_run=True)
        self.assertEqual(len([r for r in res["results"] if r["status"] == "planned"]), 1)
        self.assertEqual(len(res["queued"]), 2)

    def test_a_high_usage_halves_kalevalas_share(self):
        from sampo import budget, kalevala
        budget.set_caps(day=1.0)  # share 0.5: two jobs fit
        self.assertEqual(len(kalevala.step(self.project(), dry_run=True)["queued"]), 1)
        with mock.patch.object(budget, "usage_high", return_value="weekly usage 85%"):
            res = kalevala.step(self.project(), dry_run=True)  # halved share 0.25: one job
        self.assertEqual(len([r for r in res["results"] if r["status"] == "planned"]), 1)
        self.assertEqual(len(res["queued"]), 2)

    def test_a_tower_is_locked_when_its_charge_is_taken(self):
        from sampo import budget, kalevala
        p = self.project()
        held = {}
        real = budget.record

        def spy(feature, **kw):
            if feature == kalevala.FEATURE and kw.get("usd", 0) > 0:
                held[kw["detail"].split(" ")[0]] = kalevala._held(p, kw["detail"].split(" ")[0])
            return real(feature, **kw)
        with mock.patch.object(budget, "record", side_effect=spy):
            res = kalevala.step(p, runner=self.fake()[0])
        ran = [r["tower"] for r in res["results"] if r["status"] == "ran"]
        self.assertTrue(ran)
        self.assertTrue(all(held.get(n) for n in ran), held)

    def test_the_stages_are_read_under_the_kalevala_lock(self):
        from sampo import core as c, kalevala
        p = self.project()
        seen = []
        real = kalevala._stage

        def spy(proj, name):  # held: another handle on the lock can't take it in the time a stage read needs
            try:
                with c.lock(proj.dir, timeout=0.05):
                    seen.append(False)
            except c.LockTimeout:
                seen.append(True)
            return real(proj, name)
        with mock.patch.object(kalevala, "_stage", side_effect=spy):
            kalevala.step(p, runner=self.fake()[0])
        self.assertTrue(seen and all(seen), seen)

    def test_kalevala_reads_the_exam_through_rune_public_wrappers(self):
        from sampo import kalevala, rune
        p = self.project()
        with mock.patch.object(rune, "exam_lines", return_value=[]) as lines:
            self.assertEqual(kalevala._tower_scores(p, ["memory"]), {"memory": None})
        lines.assert_called_once_with(p, kalevala.AGENT)
        self.assertEqual(rune.train_scenarios([]), [])

    def test_the_manifest_keeps_each_round_and_its_steps(self):
        from sampo import kalevala
        p = self.project()
        kalevala.step(p, runner=self.fake()[0])
        m = kalevala.load(p)
        self.assertEqual(m["rounds"], 1)
        self.assertTrue(m["steps"])
        self.assertTrue({"tower", "stage", "status", "rc", "at"}.issubset(m["steps"][0]), m["steps"][0])

    def test_the_steps_log_is_bounded(self):
        from sampo import kalevala
        p = self.project()
        old, kalevala.MAX_STEPS = kalevala.MAX_STEPS, 2
        try:
            kalevala.step(p, runner=self.fake()[0])
            kalevala.step(p, runner=self.fake()[0])
        finally:
            kalevala.MAX_STEPS = old
        self.assertLessEqual(len(kalevala.load(p)["steps"]), 2)

    def test_a_dry_run_names_the_jobs_and_runs_none(self):
        from sampo import kalevala
        res = self.fm_json("kalevala", "step", "--dry-run")
        self.assertTrue(res["dry_run"])
        self.assertTrue(res["results"])
        self.assertTrue(all(r["status"] == "planned" for r in res["results"]), res["results"])
        self.assertEqual(kalevala.load(self.project()).get("rounds", 0), 0)

    def test_exam_scenarios_are_read_only_for_a_tower_job(self):
        from sampo import louhi
        p = self.project()

        def listing():
            return sorted(os.listdir(louhi.root(p))) if os.path.isdir(louhi.root(p)) else []
        before = listing()
        os.environ["FOREMAN_TOWER_READONLY_EXAM"] = "1"
        try:
            with self.assertRaises(ValueError):
                louhi.gen(p)
        finally:
            os.environ.pop("FOREMAN_TOWER_READONLY_EXAM", None)
        self.assertEqual(listing(), before)

    def test_moons_writes_refuse_while_another_funnel_holds_the_store(self):
        from sampo import moonscale
        p = self.project()
        release = threading.Event()
        t = self.hold_store(release)
        try:
            with mock.patch.object(moonscale, "LOCK_WAIT", 0.2):  # the bounded wait, then a refusal
                r = moonscale.grow(p, target=3, max_calls=0)
            self.assertIn("another funnel", r["error"])
            with self.assertRaises(moonscale.Busy):
                moonscale.add(p, [{"title": "x"}], {}, "model", 1)
        finally:
            release.set()
            t.join()

    def test_grow_waits_a_bounded_time_for_a_store_that_frees_up(self):
        from sampo import moonscale
        p = self.project()
        release = threading.Event()
        t = self.hold_store(release)
        threading.Timer(0.3, release.set).start()
        try:
            with mock.patch.object(moonscale, "LOCK_WAIT", 5.0):
                r = moonscale.grow(p, target=3, max_calls=0)
        finally:
            release.set()
            t.join()
        self.assertFalse(r.get("error"))

    def test_a_grow_refused_by_the_store_exits_nonzero(self):
        from sampo import moonscale
        release = threading.Event()
        t = self.hold_store(release)
        try:
            r = self.fm("moons", "grow", "--max-calls", "0", check=False)  # waits LOCK_WAIT (5 s), then refuses
        finally:
            release.set()
            t.join()
        self.assertEqual(r.returncode, moonscale.BUSY_EXIT, r.stdout + r.stderr)  # T-3929: busy, not a failure; still nonzero
        self.assertIn("another funnel", r.stderr)


class CloseWithoutKalevala(ForemanTestCase):
    """Kalevala part 2 review (T-3081): the close's behaviour note is best effort. A Kalevala that cannot be imported
    must not block the close: the task still closes and the failure goes to stderr."""

    def test_a_close_still_runs_when_kalevala_cannot_be_imported(self):
        self.fm("init")
        with open(os.path.join(self.repo, "a.py"), "w") as f:
            f.write("x = 1\n")
        self.fm("task", "new", "t", "--type", "FEATURE", "--tier", "S", "--scope", "a.py", "--step", "do it",
                "--ac", "flag", "--focus")
        self.fm("task", "evidence", "T-0001", "--ac", "1", "--run", "true")
        self.fm("task", "ac", "T-0001", "check", "1")
        self.fm("task", "evidence", "T-0001", "--step", "1", "--run", "true")
        self.fm("task", "step", "T-0001", "done", "1")
        self.fm("task", "audit", "T-0001", "self", "checked", "ok")
        hide = os.path.join(self.tmp, "hide")
        os.makedirs(hide)
        with open(os.path.join(hide, "sitecustomize.py"), "w") as f:
            f.write("import sys\nsys.modules['sampo.kalevala'] = None  # a broken Kalevala import\n")
        path = os.pathsep.join([hide, os.environ.get("PYTHONPATH", "")])
        r = self.fm("task", "done", "T-0001", env={"PYTHONPATH": path}, check=False)
        self.assertEqual(r.returncode, 0, r.stderr)


class KalevalaGen(ForemanTestCase):
    """Kalevala part 2, step 3 (T-3081): Kalevala's own generations at research/genesis/kalevala/gen-N. Each gen picks
    the tower set and its setup; select judges it by the towers' combined exam results against the parent gen on the
    towers both scored, keeps it or reverts it, and writes Kalevala's own lineage.md (never a tower's)."""

    def setUp(self):
        super().setUp()
        self.fm("init")
        self.fm("genesis", "init", "--file", self.write("gen0.md", "root charter\n"))
        self.fm("kalevala", "start")  # efficiency, memory, problem-solving

    def write(self, name, text):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as f:
            f.write(text)
        return path

    def project(self):
        return c.find_project(self.repo, create=False)

    def grade(self, tower, run, train, holdout=()):
        """One graded exam line in the tower's own home, as fm exam grade writes it."""
        from sampo import rune
        rune.set_tower(tower)
        try:
            path = os.path.join(rune.home(self.project()), "exam.jsonl")
            with open(path, "a") as f:
                f.write(json.dumps({"agent": "foreman", "run": run, "model": "sonnet", "split": "train",
                                    "closed": {"train": list(train), "holdout": list(holdout)}}) + "\n")
        finally:
            rune.set_tower(None)

    def test_gen_one_picks_every_sealed_tower_and_writes_its_setup(self):
        from sampo import kalevala
        r = kalevala.gen(self.project())
        self.assertEqual(r["gen"], 1)
        self.assertEqual(r["set"], ["efficiency", "memory", "problem-solving"])
        gdir = os.path.join(kalevala.home(self.project()), "gen-1")
        setup = read_text(os.path.join(gdir, "setup.md"))
        for name in r["set"]:
            self.assertIn(name, setup)
        self.assertIn("Gen: 1", setup)
        self.assertFalse(os.path.exists(os.path.join(kalevala.home(self.project()), "lineage.md")))  # select writes it

    def test_the_gen_folders_live_under_kalevala_and_no_tower_gets_a_lineage(self):
        from sampo import kalevala
        self.fm("kalevala", "gen")
        p = self.project()
        self.assertTrue(os.path.isdir(os.path.join(p.dir, "research", "genesis", "kalevala", "gen-1")))
        for name in ("efficiency", "memory", "problem-solving"):
            self.assertFalse(os.path.exists(os.path.join(p.dir, "research", "genesis", "towers", name, "lineage.md")))
        self.assertEqual(kalevala.load(p)["gens"]["1"]["set"], ["efficiency", "memory", "problem-solving"])

    def test_the_next_gen_waits_for_the_parent_to_be_selected(self):
        from sampo import kalevala
        self.fm("kalevala", "gen")
        with self.assertRaisesRegex(ValueError, "select 1 first"):
            kalevala.gen(self.project())

    def test_select_refuses_a_gen_with_no_setup_and_a_gen_no_tower_graded(self):
        from sampo import kalevala
        p = self.project()
        with self.assertRaisesRegex(ValueError, "no kalevala gen 3"):
            kalevala.select(p, 3)
        kalevala.gen(p)
        with self.assertRaisesRegex(ValueError, "has a graded exam line"):
            kalevala.select(p, 1)

    def test_the_towers_combined_results_keep_a_gen_that_closes_more_and_revert_one_that_does_not(self):
        from sampo import kalevala
        p = self.project()
        self.grade("memory", "r1", ["a"])
        self.grade("efficiency", "r1", ["a", "b"])
        kalevala.gen(p)
        g1 = kalevala.select(p, 1)
        self.assertEqual((g1["verdict"], g1["base"]), ("baseline", "gen-1"))
        self.grade("memory", "r2", ["a", "b", "c"])
        self.grade("efficiency", "r2", ["a", "b"])
        kalevala.gen(p)
        g2 = kalevala.select(p, 2)
        self.assertEqual(g2["scores"]["memory"], 3)
        self.assertEqual((g2["verdict"], g2["base"]), ("kept", "gen-2"))  # 5 closed against gen 1's 3 on both towers
        self.grade("memory", "r3", ["a"])
        self.grade("efficiency", "r3", ["a"])
        kalevala.gen(p)
        g3 = kalevala.select(p, 3)
        self.assertEqual((g3["verdict"], g3["base"]), ("reverted", "gen-2"))  # 2 closed against 5: reverted onto gen 2

    def test_a_tower_that_never_graded_is_left_out_of_the_comparison(self):
        from sampo import kalevala
        p = self.project()
        self.grade("memory", "r1", ["a"])
        kalevala.gen(p)
        kalevala.select(p, 1)
        self.grade("memory", "r2", ["a", "b"])  # efficiency and problem-solving never graded
        kalevala.gen(p)
        g2 = kalevala.select(p, 2)
        self.assertIsNone(g2["scores"]["efficiency"])
        self.assertEqual(g2["verdict"], "kept")  # only memory is on both gens: 2 against 1

    def test_lineage_holds_one_section_per_gen_and_select_is_idempotent(self):
        from sampo import kalevala
        p = self.project()
        self.grade("memory", "r1", ["a"])
        kalevala.gen(p)
        kalevala.select(p, 1)
        again = kalevala.select(p, 1)
        self.assertEqual(again["verdict"], "baseline")
        lin = read_text(os.path.join(kalevala.home(p), "lineage.md"))
        self.assertEqual(lin.count("## gen-1"), 1)
        self.assertIn("- baseline:", lin)
        self.assertIn("- seed base: gen-1", lin)
        select_md = read_text(os.path.join(kalevala.home(p), "gen-1", "select.md"))
        self.assertIn("Verdict: baseline", select_md)

    def test_the_cli_runs_gen_and_select_and_status_lists_the_verdict(self):
        self.grade("memory", "r1", ["a"])
        self.assertEqual(self.fm_json("kalevala", "gen")["gen"], 1)
        r = self.fm_json("kalevala", "select", "1")
        self.assertEqual(r["verdict"], "baseline")


class KalevalaCapabilities(ForemanTestCase):
    """Kalevala part 2, step 4 (T-3081): a closed task that changes how Sampo behaves is a capability. The next
    Kalevala gen writes the registry into every set tower's capabilities.md, so the tower's next seed carries it; select
    records, per tower, whether the towers that got the last gen's new capabilities improved."""

    def setUp(self):
        super().setUp()
        self.fm("init")
        self.fm("genesis", "init", "--file", self.write("gen0.md", "root charter\n"))
        self.fm("kalevala", "start")  # efficiency, memory, problem-solving

    def write(self, name, text):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as f:
            f.write(text)
        return path

    def project(self):
        return c.find_project(self.repo, create=False)

    def closed(self, task, areas=(), title=""):
        """One task_done row, as cli's close logs it (areas and title only when the task changed behaviour)."""
        data = {"areas": list(areas), "title": title} if areas else {"tier": "S"}
        c.log_event(self.project(), "task_done", task=task, data=data)

    def grade(self, tower, run, train):
        from sampo import rune
        rune.set_tower(tower)
        try:
            with open(os.path.join(rune.home(self.project()), "exam.jsonl"), "a") as f:
                f.write(json.dumps({"agent": "foreman", "run": run, "model": "sonnet", "split": "train",
                                    "closed": {"train": list(train), "holdout": []}}) + "\n")
        finally:
            rune.set_tower(None)

    def caps_file(self, tower):
        return os.path.join(self.project().dir, "research", "genesis", "towers", tower, "capabilities.md")

    def test_a_task_names_the_behaviour_it_changed_and_tests_and_docs_name_none(self):
        from sampo import kalevala
        self.assertEqual(kalevala.behaviour(["plugin/lib/sampo/ideas.py", "plugin/tests/test_ideas.py",
                                             "docs/map/review.md", "plugin/skills/fm-plan/SKILL.md",
                                             "plugin/lib/sampo/cli.py"]),
                         ["ideas", "plan", "skills"])
        self.assertEqual(kalevala.behaviour(["plugin/lib/sampo/cli.py", "plugin/tests/test_cli.py"]), [])
        self.assertEqual(kalevala.behaviour(["plugin/lib/sampo/planet.py"]), [])  # a name that holds "plan" isn't plan
        self.assertEqual(kalevala.behaviour(["plugin/lib/sampo/prompts.py"]), ["prompt"])
        self.assertEqual(kalevala.behaviour(["plugin/lib/sampo/plan.py"]), ["plan"])

    def test_the_registry_holds_each_behaviour_task_once_in_the_order_closed(self):
        from sampo import kalevala
        self.closed("T-0001", ["ideas"], "rank ideas by novelty")
        self.closed("T-0002")  # changes no behaviour: not a capability
        self.closed("T-0001", ["ideas"], "rank ideas by novelty")  # a second close never doubles it
        self.closed("T-0003", ["review"], "review the diff")
        reg = kalevala.registry(self.project())
        self.assertEqual([x["task"] for x in reg], ["T-0001", "T-0003"])
        self.assertEqual((reg[1]["title"], reg[1]["areas"]), ("review the diff", ["review"]))

    def test_gen_writes_the_registry_into_every_set_towers_capabilities_and_the_manifest_keeps_it(self):
        from sampo import kalevala
        p = self.project()
        self.closed("T-0001", ["ideas"], "rank ideas")
        self.grade("memory", "r1", ["a"])
        r = kalevala.gen(p)
        for name in r["set"]:
            self.assertIn("T-0001 (rank ideas): ideas", read_text(self.caps_file(name)))
        m1 = kalevala.load(p)["gens"]["1"]
        self.assertEqual((m1["capabilities"], m1["new_capabilities"]), (["T-0001"], ["T-0001"]))
        kalevala.select(p, 1)
        self.closed("T-0002", ["memory"], "keep memory")
        kalevala.gen(p)
        m2 = kalevala.load(p)["gens"]["2"]
        self.assertEqual((m2["capabilities"], m2["new_capabilities"]), (["T-0001", "T-0002"], ["T-0002"]))
        self.assertIn("T-0002 (keep memory): memory", read_text(self.caps_file("memory")))

    def test_the_towers_capabilities_file_keeps_the_newest_thirty(self):
        from sampo import kalevala
        for k in range(1, 32):
            self.closed(f"T-{k:04d}", ["ideas"], f"idea {k}")
        self.grade("memory", "r1", ["a"])
        kalevala.gen(self.project())
        text = read_text(self.caps_file("memory"))
        self.assertEqual(text.count("\n- T-"), 30)
        self.assertNotIn("T-0001 (", text)
        self.assertIn("T-0031 (idea 31)", text)
        self.assertEqual(len(kalevala.registry(self.project())), 31)  # the registry keeps all of them

    def test_select_records_whether_the_last_gens_new_capabilities_helped_each_tower(self):
        from sampo import kalevala
        p = self.project()
        self.closed("T-0001", ["ideas"], "rank ideas")
        self.grade("memory", "r1", ["a"])
        self.grade("efficiency", "r1", ["a", "b"])
        kalevala.gen(p)
        self.assertEqual(kalevala.select(p, 1)["helped"], {})  # gen 1 has no parent: nothing to test yet
        self.closed("T-0002", ["review"], "review the diff")
        self.grade("memory", "r2", ["a", "b", "c"])  # memory 1 -> 3
        self.grade("efficiency", "r2", ["a"])  # efficiency 2 -> 1
        kalevala.gen(p)  # gen 2 writes T-0002 for the next seeds; select 2 tests what gen 1 wrote (T-0001)
        g2 = kalevala.select(p, 2)
        self.assertEqual(g2["helped"]["memory"], {"new": ["T-0001"], "rose": True})
        self.assertEqual(g2["helped"]["efficiency"], {"new": ["T-0001"], "rose": False})
        self.assertNotIn("problem solving", g2["helped"])  # never graded: not compared
        self.assertIn("Helped:", read_text(os.path.join(kalevala.home(p), "gen-2", "select.md")))

    def test_a_towers_next_seed_carries_the_capabilities_as_a_section(self):
        from sampo import kalevala, rune
        p = self.project()
        self.closed("T-0001", ["ideas"], "rank ideas")
        self.grade("memory", "r1", ["a"])
        kalevala.gen(p)
        rune.set_tower("memory")
        try:
            home = rune.home(p)
            os.makedirs(os.path.join(home, "gen-1"), exist_ok=True)
            with open(os.path.join(home, "gen-1", "seed.md"), "w") as f:
                f.write("# Seed — generation 1\n\n## Standing intent\n\nkeep it small\n")
            with open(os.path.join(home, "lineage.md"), "w") as f:
                f.write("# Lineage\n\n## gen-1\n- baseline: first\n- seed base: gen-1\n")
            rune.seed(p, 2)
            text = read_text(os.path.join(home, "gen-2", "seed.md"))
        finally:
            rune.set_tower(None)
        self.assertIn("## Capabilities built since gen-0", text)
        self.assertIn("T-0001 (rank ideas): ideas", text)
        self.assertEqual(text.count("## Capabilities built since gen-0"), 1)


class KalevalaWatch(ForemanTestCase):
    """Kalevala part 3, step 1 (T-3082): sampo kalevala watch reads each tower's generation, exam deltas, drift verdict,
    stall (hours since its newest generation), spend today and open Missions, and prints one line per tower with its
    state. Read-only: it writes nothing."""

    def setUp(self):
        super().setUp()
        self.fm("init")
        # a Capabilities section makes each tower's next stage runnable (the acts run jobs); KalevalaStep's root charter
        self.fm("genesis", "init", "--file", self.write(
            "gen0.md", "root charter\n\n## 2. Capabilities\n1. **Bugs.** finds and fixes broken code. Exam: bug.\n"))
        self.fm("kalevala", "start")  # efficiency, memory, problem-solving
        from sampo import budget
        budget.RUN["usd"] = 0.0

    def write(self, name, text):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as f:
            f.write(text)
        return path

    def project(self):
        return c.find_project(self.repo, create=False)

    def in_tower(self, tower, fn):
        from sampo import rune
        rune.set_tower(tower)
        try:
            return fn(rune.home(self.project()))
        finally:
            rune.set_tower(None)

    def grade(self, tower, run, train):
        def add(home):
            with open(os.path.join(home, "exam.jsonl"), "a") as f:
                f.write(json.dumps({"agent": "foreman", "run": run, "model": "sonnet", "split": "train",
                                    "closed": {"train": list(train), "holdout": []}}) + "\n")
        self.in_tower(tower, add)

    def make_gen(self, tower, n, when=None, missions=()):
        def add(home):
            d = os.path.join(home, f"gen-{n}")
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "seed.md"), "w") as f:
                f.write("# Seed\n")
            for m in missions:
                with open(os.path.join(d, f"mission-{m}.md"), "w") as f:
                    f.write("# Mission\n")
            if when is not None:
                os.utime(d, (when, when))
        self.in_tower(tower, add)

    def log_drift(self, tower, verdict):
        from sampo import rune
        rune.set_tower(tower)
        try:
            rune._log(self.project(), "genesis_drift", data={"verdict": verdict})
        finally:
            rune.set_tower(None)

    def test_watch_reads_each_tower_and_prints_one_line_each(self):
        from sampo import kalevala
        self.grade("memory", "r1", ["a"])
        self.grade("memory", "r2", ["a", "b"])
        self.make_gen("memory", 1, missions=("a", "b"))
        r = kalevala.watch(self.project())
        rows = {t["name"]: t for t in r["towers"]}
        self.assertEqual(sorted(rows), ["efficiency", "memory", "problem-solving"])
        m = rows["memory"]
        self.assertEqual((m["generation"], m["closed"], m["delta"], m["missions"]), (1, 2, 1, 2))
        self.assertEqual(m["state"], "improving")
        self.assertEqual(rows["efficiency"]["state"], "unscored")
        self.assertEqual(rows["efficiency"]["missions"], 0)
        lines = [x for x in self.fm("kalevala", "watch").stdout.splitlines() if x.split(":")[0] in rows]
        self.assertEqual(len(lines), 3, lines)
        self.assertTrue(any(x.startswith("memory: improving") and "2 open Mission" in x for x in lines), lines)

    def test_one_corrupt_tower_file_blanks_only_its_own_row(self):  # T-3376
        from sampo import kalevala
        self.grade("memory", "r1", ["a"])
        self.in_tower("efficiency", lambda h: open(os.path.join(h, "exam.jsonl"), "w").write(
            json.dumps({"agent": "foreman", "run": "r1", "split": "train", "closed": {"train": [["x"]]}}) + "\n"))
        r = kalevala.watch(self.project())
        rows = {t["name"]: t for t in r["towers"]}
        self.assertEqual(sorted(rows), ["efficiency", "memory", "problem-solving"])
        self.assertEqual(rows["efficiency"]["state"], "unreadable")
        self.assertTrue(rows["efficiency"]["error"])
        self.assertEqual(rows["memory"]["state"], "new")
        self.assertNotIn("error", rows["memory"])
        out = self.fm("kalevala", "watch").stdout
        self.assertIn("efficiency: unreadable", out)
        self.assertIn("memory: new", out)

    def test_a_round_lists_an_unreadable_tower_as_skipped_and_plans_the_rest(self):  # T-3376
        from sampo import kalevala
        self.in_tower("efficiency", lambda h: open(os.path.join(h, "exam.jsonl"), "w").write(
            json.dumps({"agent": "foreman", "run": "r1", "split": "train", "closed": {"train": [["x"]]}}) + "\n"))
        res = kalevala.step(self.project(), dry_run=True)
        by = {r["tower"]: r for r in res["results"]}
        self.assertEqual(by["efficiency"]["status"], "unreadable")
        self.assertTrue(by["efficiency"]["error"])
        planned = [t for t, r in by.items() if r["status"] == "planned"]
        self.assertTrue(planned)
        self.assertNotIn("efficiency", planned + res["queued"])

    def test_each_state_has_its_own_reading_in_priority_order(self):
        from sampo import kalevala
        p = self.project()
        self.log_drift("efficiency", "drifted")  # drift outranks the exam
        self.grade("efficiency", "r1", ["a"])
        self.in_tower("problem-solving", lambda h: (os.chmod(os.path.join(h, "gen-0.md"), 0o644),
                                                    open(os.path.join(h, "gen-0.md"), "a").write("edited\n")))
        self.grade("memory", "r1", ["a"])
        self.grade("memory", "r2", ["a", "b"])
        now = time.time()
        self.make_gen("memory", 1, when=now - 50 * 3600)  # no new generation for 50 hours
        rows = {t["name"]: t for t in kalevala.watch(p, now=now, stall_h=24)["towers"]}
        self.assertEqual(rows["efficiency"]["state"], "drifted")
        self.assertEqual(rows["efficiency"]["drift"], "drifted")
        self.assertEqual(rows["problem-solving"]["state"], "unsealed")
        self.assertFalse(rows["problem-solving"]["sealed"])
        self.assertEqual(rows["memory"]["state"], "stalled")
        self.assertGreaterEqual(rows["memory"]["idle_h"], 49.9)
        self.assertEqual(rows["memory"]["delta"], 1)  # the exam still reads: stalled is the verdict that counts

    def test_the_exam_states_compare_the_newest_run_with_the_one_before(self):
        from sampo import kalevala
        p = self.project()
        self.grade("efficiency", "r1", ["a", "b"])
        self.grade("efficiency", "r2", ["a", "b"])
        self.grade("memory", "r1", ["a", "b"])
        self.grade("memory", "r2", ["a"])
        self.grade("problem-solving", "r1", ["a"])
        rows = {t["name"]: t for t in kalevala.watch(p)["towers"]}
        self.assertEqual((rows["efficiency"]["state"], rows["efficiency"]["delta"]), ("flat", 0))
        self.assertEqual((rows["memory"]["state"], rows["memory"]["delta"]), ("falling", -1))
        self.assertEqual((rows["problem-solving"]["state"], rows["problem-solving"]["delta"]), ("new", None))
        self.assertEqual(rows["problem-solving"]["closed"], 1)

    def test_watch_writes_nothing(self):
        from sampo import kalevala
        p = self.project()
        self.grade("memory", "r1", ["a"])
        self.make_gen("memory", 1, missions=("a",))

        def snapshot():
            out = {}
            for root, _, files in os.walk(kalevala.home(p)):
                for f in files:
                    path = os.path.join(root, f)
                    out[path] = (os.path.getmtime(path), os.path.getsize(path))
            return out
        before = snapshot()
        kalevala.watch(p)
        self.fm("kalevala", "watch")
        self.assertEqual(snapshot(), before)

    def test_a_stall_window_must_be_a_positive_number_of_hours(self):
        for bad in ("nan", "0", "-3"):
            r = self.fm("kalevala", "watch", "--stall-hours", bad, check=False)
            self.assertNotEqual(r.returncode, 0, bad)
        self.assertEqual(self.fm("kalevala", "watch", "--stall-hours", "0.5").returncode, 0)

    # Kalevala part 3, step 2 (T-3082): the acts. Each is logged in the ledger with its reason and an undo line.

    def runner(self):
        calls = []

        def run(argv, cwd, env):
            calls.append(list(argv))
            return 0
        return run, calls

    def steers_of(self, tower):
        return self.in_tower(tower, lambda h: open(os.path.join(h, "steers.md")).read()
                             if os.path.exists(os.path.join(h, "steers.md")) else "")

    def test_a_drifted_tower_is_paused_and_sits_out_the_round(self):
        from sampo import budget, kalevala
        p = self.project()
        budget.set_caps(day=5.0)
        self.log_drift("memory", "drifted")
        run, calls = self.runner()
        res = kalevala.step(p, runner=run)
        self.assertEqual({x["tower"]: x["status"] for x in res["results"]}["memory"], "paused")
        self.assertFalse(any("memory" in " ".join(a) for a in calls), calls)
        act = [a for a in kalevala.acts(p) if a["act"] == "pause"]
        self.assertEqual([a["tower"] for a in act], ["memory"])
        self.assertIn("drift", act[0]["reason"])
        self.assertEqual(kalevala.load(p)["towers"]["memory"]["paused"], act[0]["reason"])

    def test_a_stalled_tower_is_snapshotted_before_its_restart_and_goes_first(self):
        import time
        from sampo import budget, kalevala
        p = self.project()
        budget.set_caps(day=5.0)
        self.grade("memory", "r1", ["a"])
        self.make_gen("memory", 1, when=time.time() - 30 * 3600)
        self.assertEqual(kalevala.order(kalevala.watch(p)["towers"])[0], "memory")  # the stalled tower ranks first
        run, _ = self.runner()
        res = kalevala.step(p, runner=run)
        act = [a for a in kalevala.acts(p) if a["act"] == "restart"]
        self.assertEqual([a["tower"] for a in act], ["memory"])
        self.assertTrue(os.path.isfile(os.path.join(act[0]["snapshot"], "gen-1", "seed.md")))
        self.assertIn(act[0]["snapshot"], act[0]["undo"])
        # ponytail: memory's stage is 'fatal' in this fixture, so it isn't run; the run-first order is the ranking above

    def test_two_concurrent_steps_snapshot_a_stalled_tower_and_log_its_restart_once(self):
        import shutil
        import time
        from sampo import budget, kalevala
        p = self.project()
        budget.set_caps(day=5.0)
        self.grade("memory", "r1", ["a"])
        self.make_gen("memory", 1, when=time.time() - 30 * 3600)
        real = shutil.copytree

        def slow(*a, **k):  # the snapshot is slow, so the second step reaches its check while the first copies
            time.sleep(0.4)
            return real(*a, **k)
        run, _ = self.runner()
        with mock.patch.object(kalevala.shutil, "copytree", slow):
            ts = [threading.Thread(target=kalevala.step, args=(p,), kwargs={"runner": run}) for _ in range(2)]
            for t in ts:
                t.start()
            for t in ts:
                t.join(30)
        self.assertEqual([a["tower"] for a in kalevala.acts(p) if a["act"] == "restart"], ["memory"])
        self.assertEqual(len(os.listdir(os.path.join(kalevala.home(p), "snapshots"))), 1)

    def test_a_held_decide_lock_makes_a_step_skip_that_tower_as_busy_within_the_deadline(self):
        import time
        from sampo import budget, kalevala
        p = self.project()
        budget.set_caps(day=5.0)
        run, calls = self.runner()
        held, release = threading.Event(), threading.Event()

        def hold():
            with kalevala._decide_lock(p, "memory"):
                held.set()
                release.wait(30)
        t = threading.Thread(target=hold)
        t.start()
        held.wait(10)
        try:
            with mock.patch.object(kalevala, "DECIDE_WAIT", 0.2):
                t0 = time.monotonic()
                res = kalevala.step(p, runner=run)
                took = time.monotonic() - t0
        finally:
            release.set()
            t.join(10)
        self.assertEqual({x["tower"]: x["status"] for x in res["results"]}["memory"], "busy")
        self.assertFalse(any("memory" in " ".join(a) for a in calls), calls)
        self.assertLess(took, 10)

    def test_a_steer_passes_the_drift_check_before_it_is_written(self):
        from sampo import kalevala
        p = self.project()
        self.assertIsNone(kalevala.steer(p, "memory", "MUST: drop gen-0's focus"))
        self.assertEqual(self.steers_of("memory"), "")
        self.assertEqual(kalevala.steer(p, "memory", "Keep gen-0's focus on this aspect.")["act"], "steer")
        self.assertIsNone(kalevala.steer(p, "memory", "Keep gen-0's focus on this aspect."))
        self.assertEqual(self.steers_of("memory").count("Keep gen-0"), 1)
        self.assertIn("steer_refused", [a["act"] for a in kalevala.acts(p)])

    def test_two_towers_on_the_same_files_merge_or_wait_and_a_merge_pauses(self):
        from sampo import budget, kalevala
        p = self.project()
        budget.set_caps(day=5.0)
        self.grade("efficiency", "r1", ["a"])
        self.grade("efficiency", "r2", ["a", "b"])
        self.grade("memory", "r1", ["a"])
        self.grade("memory", "r2", ["a"])
        for t in ("efficiency", "memory"):
            self.make_gen(t, 1, missions=("a",))
            self.in_tower(t, lambda h: open(os.path.join(h, "gen-1", "mission-a.md"), "w").write(
                "Touches plugin/lib/sampo/ideas.py\n"))
        run, _ = self.runner()
        res = kalevala.step(p, runner=run)
        merged = [a for a in kalevala.acts(p) if a["act"] == "merge"]
        self.assertEqual([a["tower"] for a in merged], ["memory"])
        self.assertIn("efficiency", merged[0]["reason"])
        self.assertTrue(kalevala.load(p)["towers"]["memory"]["paused"])
        self.assertEqual({x["tower"]: x["status"] for x in res["results"]}["memory"], "merged")
        kalevala.resume(p)
        self.grade("memory", "r3", ["a", "c"])  # memory improves now: the same files make one of them wait
        res = kalevala.step(p, runner=run)
        self.assertEqual({x["tower"]: x["status"] for x in res["results"]}["memory"], "waiting")
        self.assertEqual([a["tower"] for a in kalevala.acts(p) if a["act"] == "wait"], ["memory"])

    def test_a_tower_flat_two_generations_gives_its_slot_to_one_that_improves(self):
        from sampo import budget, kalevala
        p = self.project()
        budget.set_caps(day=0.6)  # the share is half the day (0.30): one job's 0.25 estimate fits, the second is queued
        for r, train in (("r1", ["a"]), ("r2", ["a"]), ("r3", ["a"])):
            self.grade("memory", r, train)
        for r, train in (("r1", ["a"]), ("r2", ["a", "b"]), ("r3", ["a", "b", "c"])):
            self.grade("efficiency", r, train)
        run, calls = self.runner()
        res = kalevala.step(p, runner=run)
        self.assertEqual(res["queued"][-1], "memory")  # the stuck tower waits last
        slot = [a for a in kalevala.acts(p) if a["act"] == "slot"]
        self.assertEqual([a["tower"] for a in slot], ["memory"])
        self.assertIn("improve", slot[0]["reason"])

    def test_pause_halts_every_round_until_resume_and_shows_in_status(self):
        from sampo import budget, kalevala
        p = self.project()
        budget.set_caps(day=5.0)
        kalevala.pause(p, "user stop")
        run, calls = self.runner()
        res = kalevala.step(p, runner=run)
        self.assertEqual(res["halted"], "user stop")
        self.assertEqual(calls, [])
        self.assertIn("pause", [a["act"] for a in self.fm_json("kalevala", "status")["acts"]])
        kalevala.resume(p)
        res = kalevala.step(p, runner=run)
        self.assertNotIn("halted", res)
        self.assertTrue(calls, res["results"])

    def test_the_loop_has_no_end_of_its_own_only_the_caller_user_or_a_halt_stops_it(self):  # T-3082 step 3
        from sampo import budget, kalevala
        p = self.project()
        budget.set_caps(day=5.0)
        run, calls = self.runner()
        seen = []

        def stop():  # the caller's limit: it's asked before each round, and only this one ends the loop
            seen.append(1)
            return "limit reached" if len(seen) > 3 else None
        res = kalevala.loop(p, runner=run, stop=stop)
        self.assertEqual(res, {"halted": "limit reached", "rounds": 3})
        self.assertTrue(calls)

    def test_a_user_pause_during_a_round_halts_the_next_one(self):
        from sampo import budget, kalevala
        p = self.project()
        budget.set_caps(day=5.0)

        def run_and_pause(argv, root, env):
            kalevala.pause(p, "user stop")
            return 0
        res = kalevala.loop(p, runner=run_and_pause, rounds=5)
        self.assertEqual(res, {"halted": "user stop", "rounds": 2})

    def test_every_tower_off_the_rules_is_a_drift_stop_that_resume_clears(self):
        from sampo import budget, kalevala
        p = self.project()
        budget.set_caps(day=5.0)
        for name in ("efficiency", "memory", "problem-solving"):
            self.log_drift(name, "drifted")
        run, calls = self.runner()
        res = kalevala.loop(p, runner=run, rounds=5)
        self.assertEqual(res, {"halted": kalevala.DRIFT_STOP, "rounds": 1})
        self.assertEqual(calls, [])
        self.assertEqual(kalevala.load(p)["halted"], kalevala.DRIFT_STOP)
        kalevala.resume(p)
        self.assertNotIn("halted", kalevala.step(p, runner=run))

    def test_a_round_that_can_run_nothing_is_idle_not_a_spin(self):
        from sampo import budget, kalevala
        p = self.project()
        budget.set_caps(day=5.0)
        for name in ("efficiency", "memory", "problem-solving"):
            kalevala.pause(p, "hold", name)
        run, calls = self.runner()
        res = kalevala.loop(p, runner=run, rounds=5)
        self.assertIn("idle", res)
        self.assertEqual(res["rounds"], 1)
        self.assertEqual(calls, [])

    def test_the_share_and_day_cap_stop_the_loop_with_an_error_not_a_hang(self):
        from sampo import budget, kalevala
        p = self.project()
        budget.set_caps(day=0.1)  # below one job's estimate: the cap refuses the round
        run, calls = self.runner()
        with self.assertRaises(budget.BudgetError):
            kalevala.loop(p, runner=run, rounds=5)
        self.assertEqual(calls, [])


class RestartUndoHasNoRm(ForemanTestCase):
    def test_the_restart_undo_hint_holds_no_rm(self):
        from sampo import kalevala
        src = open(kalevala.__file__, encoding="utf-8").read()
        self.assertNotRegex(src, r"undo=f?[\"']rm\b")
        self.assertNotIn("rm -rf {newest}", src)
