"""T-0707 (Frontier 07, first slice): one local graph of code, tasks and commits with time on its edges, queried for a
change's blast radius and a task's ranked read-set, as of any moment."""
import json
import os
import subprocess

from helpers import ForemanTestCase

FILES = {"app/auth.py": "def check_token(t):\n    return bool(t)\n",
         "app/session.py": "import auth\n\n\ndef open_session(t):\n    return auth.check_token(t)\n",
         "app/billing.py": "def invoice(total):\n    return round(total, 2)\n",
         "tests/test_auth.py": "import auth\n"}


class Base(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.fm("task", "new", "Fix login token expiry", "--type", "FIX", "--tier", "S")
        self.fm("task", "new", "Add invoices", "--type", "FEATURE", "--tier", "S")
        for rel, text in FILES.items():
            self.write(rel, text)
        self.commit("base", "2026-01-01T10:00:00Z")
        self.write("app/auth.py", FILES["app/auth.py"] + "\n\nTTL = 60\n")
        self.write("app/session.py", FILES["app/session.py"] + "\n\nREFRESH = True\n")
        self.commit("Fix login token expiry\n\nForeman-Task: T-0001", "2026-02-01T10:00:00Z")
        self.write("app/billing.py", FILES["app/billing.py"] + "\n\nCURRENCY = 'EUR'\n")
        self.commit("Add invoices\n\nForeman-Task: T-0002", "2026-03-01T10:00:00Z")

    def write(self, rel, text):
        path = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def empty_commits(self, n):
        for i in range(n):
            subprocess.run(["git", "-C", self.repo, "commit", "-q", "--allow-empty", "-m", f"empty {i}"], check=True)

    def commit(self, msg, when):
        env = dict(os.environ, GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when)
        subprocess.run(["git", "-C", self.repo, "add", "-A"], check=True, env=env)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", msg], check=True, env=env)


class Blast(Base):
    def test_dependents_co_change_tests_and_past_fixes_as_of_a_moment(self):
        out = self.fm("graph", "blast", "app/auth.py").stdout
        self.assertIn("app/session.py", out)
        self.assertIn("tests/test_auth.py", out)
        self.assertIn("T-0001", out)
        self.assertIn("co-change", out)
        before = self.fm("graph", "blast", "app/auth.py", "--as-of", "2026-01-15T00:00:00Z").stdout
        self.assertNotIn("T-0001", before)
        self.assertNotIn("co-change: app/session.py", before)


class _BlastPlan(Base):
    """plan() and risks(), no tests of their own (T-3815): BlastInPlan and BlastReach share them without rerunning
    each other's tests."""
    AP ="Approach (options → choice → why)"

    def plan(self, tid_title, scope, tier="M"):
        self.fm("task", "new", tid_title, "--type", "FEATURE", "--tier", tier, *(["--scope", scope] if scope else []))
        tid = "T-0003"
        self.fm("task", "set", tid, "--section", self.AP, "--text", "1) a; fails if: x 2) use what exists: b; fails if: y 3) do less: c; fails if: z Choice 1 (80%): a")
        self.fm("task", "set", tid, "--section", "Interpretation", "--text", "Reading 1: a. Reading 2: b.")
        self.fm("task", "ac", tid, "add", "works", "--verify", "true")
        self.fm("task", "step", tid, "add", "build")
        return tid

    def risks(self):
        from sampo import core as c
        return c.find_brief(c.find_project(self.repo), "T-0003").section("Risks and rollback")


class BlastInPlan(_BlastPlan):
    """T-2277: fm focus writes the blast line into an M/L task's Risks once."""

    def test_names_neighbours_and_past_fixes_once(self):
        tid = self.plan("Touch the session", "app/session.py")
        self.fm("graph", "build")
        import time
        from sampo import core as c
        from sampo import graph
        t0 = time.monotonic()
        line = graph.blast_line(c.find_project(self.repo), ["app/session.py"])
        self.assertLess(time.monotonic() - t0, graph.BLAST_BUDGET)  # the seeded graph answers well inside the cap; wall clock: BLAST_BUDGET deadline (blast_line runs git)
        self.assertIn("app/auth.py", line)
        self.fm("focus", tid)
        r = self.risks()
        self.assertIn("Blast radius (sampo graph blast):", r)
        self.assertIn("app/auth.py", r)
        self.assertIn("T-0001", r)
        self.fm("focus", tid)
        self.assertEqual(self.risks().count("Blast radius"), 1)

    def test_a_brief_holding_the_old_fm_line_is_not_annotated_again(self):  # T-2847
        tid = self.plan("Touch the session", "app/session.py")
        self.fm("graph", "build")
        self.fm("task", "set", tid, "--section", "Risks and rollback", "--text", "Blast radius (fm graph blast): old line.")
        self.fm("focus", tid)
        self.assertEqual(self.risks().count("Blast radius"), 1)
        self.assertNotIn("sampo graph blast", self.risks())

    def test_empty_scope_glob_or_unbuilt_graph_skips(self):
        tid = self.plan("Touch the session", "app/*.py")
        self.fm("graph", "build")
        self.fm("focus", tid)
        self.assertNotIn("Blast radius", self.risks())

    def test_no_graph_skips_and_focus_does_not_build_one(self):
        tid = self.plan("Touch the session", "app/session.py")
        self.fm("focus", tid)
        self.assertNotIn("Blast radius", self.risks())


class BlastReach(_BlastPlan):
    """T-2336: the line reaches plans with no scope (files named in the text), a graph a few commits old, and says
    'unknown' for files with no history."""

    def named(self, text):
        tid = self.plan("Touch the session", None)
        self.fm("task", "set", tid, "--section", "Interpretation", "--text", text)
        return tid

    def line(self, text, scope=None):
        from sampo import core as c
        from sampo import graph
        return graph.blast_line(c.find_project(self.repo), scope, text=text)

    def test_files_named_in_the_text_get_the_line_once(self):
        tid = self.named("Reading 1: change `app/session.py::open_session`, not billing. Reading 2: skip.")
        self.fm("graph", "build")
        self.fm("focus", tid)
        r = self.risks()
        self.assertIn("Blast radius (sampo graph blast): co-changes app/auth.py", r)
        self.fm("focus", tid)
        self.assertEqual(self.risks().count("Blast radius"), 1)

    def test_a_scope_wins_over_the_text(self):
        self.write("app/lonely.py", "x = 1\n")
        self.commit("lonely", "2026-04-01T10:00:00Z")
        self.fm("graph", "build")
        line = self.line("touch app/lonely.py", ["app/session.py"])
        self.assertIn("app/auth.py", line)
        self.assertNotIn("lonely", line)

    def test_words_that_are_not_repo_files_say_nothing(self):
        self.fm("graph", "build")
        self.assertEqual(self.line("see ../../etc/passwd, nope/none.py, e.g. v1.2 and /etc/hostname"), "")

    def test_a_graph_10_commits_old_is_read_and_one_60_old_is_not(self):
        self.fm("graph", "build")
        self.empty_commits(10)
        self.assertIn("app/auth.py", self.line("app/session.py"))
        self.empty_commits(50)
        from unittest import mock
        with mock.patch.dict(os.environ, {"FOREMAN_NO_BACKGROUND": "1"}):
            self.assertEqual(self.line("app/session.py"), "")

    def test_a_graph_60_commits_old_starts_a_build(self):
        self.fm("graph", "build")
        self.empty_commits(60)
        line, pop = BlastBuild.run_line(self, True)
        self.assertEqual((line, pop.call_count), (["", ""], 1))

    def test_no_co_change_history_says_unknown(self):
        self.write("app/lonely.py", "x = 1\n")
        self.commit("lonely", "2026-04-01T10:00:00Z")
        self.fm("graph", "build")
        self.assertEqual(self.line("only app/lonely.py"),
                         "Blast radius (sampo graph blast): unknown, no co-change history for app/lonely.py.")


class BlastBuild(Base):
    """T-2277: a missing or stale graph at focus starts one detached fm graph build and writes no line."""

    def run_line(self, background):
        from unittest import mock
        from sampo import core as c
        from sampo import graph
        p = c.find_project(self.repo)
        env = {} if background else {"FOREMAN_NO_BACKGROUND": "1"}
        real = subprocess.Popen
        pop = mock.MagicMock()

        def fake(argv, *a, **k):  # git runs through Popen too: only the build is stubbed
            return pop(argv, *a, **k) if argv[-2:] == ["graph", "build"] else real(argv, *a, **k)
        with mock.patch.dict(os.environ, env), mock.patch("subprocess.Popen", fake):
            if background:
                os.environ.pop("FOREMAN_NO_BACKGROUND", None)
            line = [graph.blast_line(p, ["app/session.py"]) for _ in range(2)]
        return line, pop

    def test_a_missing_graph_starts_one_build_and_no_line(self):
        line, pop = self.run_line(True)
        self.assertEqual(line, ["", ""])
        self.assertEqual(pop.call_count, 1)  # the second call sees the build in flight
        args, kw = pop.call_args
        self.assertEqual(args[0][-2:], ["graph", "build"])
        self.assertTrue(kw["start_new_session"])

    def test_a_stale_graph_starts_a_build(self):
        self.fm("graph", "build")
        self.write("app/new.py", "x = 1\n")
        self.commit("move head", "2026-04-01T10:00:00Z")
        self.empty_commits(60)  # T-2336: a graph this far behind HEAD is stale
        line, pop = self.run_line(True)
        self.assertEqual((line, pop.call_count), (["", ""], 1))

    def test_a_ledger_append_alone_does_not_make_it_stale(self):  # review: hooks append events all the time
        from sampo import core as c
        from sampo import graph
        self.fm("graph", "build")
        p = c.find_project(self.repo)
        with open(os.path.join(p.dir, "ledger.jsonl"), "a") as f:
            f.write('{"event": "noise"}\n')
        line, pop = self.run_line(True)
        self.assertEqual(pop.call_count, 0)
        self.assertIn("app/auth.py", graph.blast_line(p, ["app/session.py"]))

    def test_the_build_removes_its_lock_and_a_future_lock_is_expired(self):
        from sampo import core as c
        from sampo import graph
        p = c.find_project(self.repo)
        lock = os.path.join(p.dir, "graph.building")
        with open(lock, "w") as f:
            f.write("1")
        self.fm("graph", "build")
        self.assertFalse(os.path.exists(lock))
        with open(lock, "w") as f:
            f.write("1")
        future = __import__("time").time() + 86400
        os.utime(lock, (future, future))
        self.write("app/new.py", "x = 1\n")
        self.commit("move head", "2026-04-01T10:00:00Z")
        self.empty_commits(60)  # the graph is stale again
        line, pop = self.run_line(True)
        self.assertEqual(pop.call_count, 1)

    def test_the_line_is_computed_before_the_project_lock(self):
        from sampo import core as c  # source order: git and sqlite waits must not sit inside the lock every verb queues on
        src = open(os.path.join(c.PLUGIN_ROOT, "lib", "sampo", "cli.py")).read()
        body = src.split("def cmd_focus")[1].split("\ndef ")[0]
        self.assertLess(body.index("blast_line"), body.index("with c.lock(p.dir)"))

    def test_no_background_starts_none(self):
        line, pop = self.run_line(False)
        self.assertEqual((line, pop.call_count), (["", ""], 0))


class Pack(Base):
    def test_a_tasks_words_reach_files_through_past_tasks(self):
        data = json.loads(self.fm("graph", "pack", "login token refresh is broken", "--json").stdout)
        files = [f["path"] for f in data["files"]]
        self.assertEqual(set(files[:2]), {"app/auth.py", "app/session.py"}, files)
        self.assertTrue("app/billing.py" not in files or files.index("app/billing.py") > 1)


class Graphify(Base):
    def test_graphify_edges_become_file_edges(self):
        self.write("graphify-out/graph.json", json.dumps({
            "nodes": [{"id": "inv", "source_file": os.path.join(self.repo, "app/billing.py")},
                      {"id": "sess", "source_file": "app/session.py"}],
            "links": [{"source": "inv", "target": "sess", "relation": "calls"}]}))
        out = self.fm("graph", "blast", "app/session.py").stdout
        self.assertIn("app/billing.py", out.split("co-change")[0])
