"""T-4635: one view of a task's own change at a close. Ten close-time readers in cli.py judge a task the trunk already holds
by its own landing (core.landed_change through cli._own_view: the files its landing commits brought in, what is uncommitted,
what it touched or claims) and not by base..tree, which also holds every lane that landed since (T-4625 the linked tests,
T-4628 the scope check, T-4632 the Landed line and the owed companions, and now _self_docs_gaps, _proved_gaps,
_file_map_gaps, _tier_drift, _deleted and the debug-scaffold scan). A task nothing landed, and a lane, read base..tree as
before. A guard parses cli.py with ast and fails when a function other than the shared view (or one named, with its
reason, in ALLOWED) calls a base-diff primitive: a new reader cannot repeat the fault.
reruns: python3 plugin/tests/run.py test_close_one_view"""
# map-links: plugin/lib/sampo/cli.py, plugin/lib/sampo/core.py
import ast
import os
import subprocess
import unittest
from unittest import mock

from helpers import ForemanTestCase

from sampo import cli
from sampo import core as c
from sampo import doctor

HERE = os.path.dirname(os.path.abspath(__file__))
CLI = os.path.join(os.path.dirname(HERE), "lib", "sampo", "cli.py")

MASTER = ("# M\n## 3. File map\n| Path | Purpose |\n|---|---|\n| `plugin/bin/` | cli |\n"
          "| `docs/` | docs |\n## 4. Next\n")


class Base(ForemanTestCase):
    """A repo with a task that started, another lane's code landing as plain commits around the task's own merge."""

    def setUp(self):
        super().setUp()
        self.fm("init")
        self.put({"a.py": "VALUE = 1\n", "b.py": "VALUE = 1\n", "old.py": "VALUE = 1\n", "own.py": "VALUE = 1\n",
                  "plugin/bin/fm": "x\n", ".gitignore": "__pycache__/\n", "MASTER.md": MASTER,
                  ".fmproved": "secret.py\n", "secret.py": "SCORE = 1\n"})
        self.git("add", "-A")
        self.git("commit", "-qm", "suite")
        self.main = self.git("rev-parse", "--abbrev-ref", "HEAD").strip()
        self.p = c.find_project(self.repo)

    def git(self, *args):
        return subprocess.run(["git", "-C", self.repo, "-c", "user.name=t", "-c", "user.email=t@localhost", *args],
                              check=True, capture_output=True, text=True).stdout

    def put(self, files):
        for rel, text in files.items():
            os.makedirs(os.path.dirname(os.path.join(self.repo, rel)), exist_ok=True)
            with open(os.path.join(self.repo, rel), "w") as f:
                f.write(text)

    def head(self):
        return self.git("rev-parse", "HEAD").strip()

    def task(self, scope="a.py", tier="S", type_="FEATURE"):
        tid = self.fm_json("task", "new", "Change a", "--type", type_, "--tier", tier, "--ac", "works :: true",
                           "--step", "s", "--scope", scope)["id"]
        self.fm("focus", tid)
        return tid, self.head()

    def land(self, tid, files, base, gone=()):
        """The task's files on a branch from its base, merged the way a lane merge does."""
        self.git("checkout", "-q", "-b", f"lane-{tid}", base)
        self.put(files)
        for g in gone:
            os.remove(os.path.join(self.repo, g))
        self.git("add", "-A")
        self.git("commit", "-qm", f"{tid} work")
        self.git("checkout", "-q", self.main)
        self.git("merge", "--no-ff", "-qm", f"Merge {tid}: change", f"lane-{tid}")

    def direct(self, tid, files, gone=()):
        """Another lane's files committed straight onto the trunk, the title ending (T-ID)."""
        self.put(files)
        for g in gone:
            os.remove(os.path.join(self.repo, g))
        self.git("add", "-A")
        self.git("commit", "-qm", f"Other work ({tid})")

    def brief(self, tid):
        return c.find_brief(self.p, tid)

    def around(self, own, other_before, other_after, base_task=None, gone=(), other_gone=(), **kw):
        """Task starts; another lane lands `other_before`; the task lands `own`; another lane lands `other_after`."""
        tid, base = self.task(**kw)
        if other_before:
            self.direct("T-0998", other_before, other_gone)
        self.land(tid, own, base, gone)
        if other_after:
            self.direct("T-0999", other_after)
        return tid


class SelfDocs(Base):
    def gaps(self, tid):
        with mock.patch("sampo.doctor.check_self_docs", return_value=doctor.Result("self docs", "FAIL", "stale")):
            return cli._self_docs_gaps(self.p, self.brief(tid))

    def test_a_skill_another_lane_landed_is_not_this_tasks_change(self):
        tid = self.around({"a.py": "VALUE = 2\n"}, {"plugin/skills/x/SKILL.md": "x\n"}, {"plugin/skills/y/SKILL.md": "y\n"})
        self.assertEqual(self.gaps(tid), [])

    def test_its_own_skill_still_asks_for_current_docs(self):
        tid = self.around({"plugin/skills/z/SKILL.md": "z\n"}, {"b.py": "VALUE = 2\n"}, None)
        self.assertEqual(self.gaps(tid), ["self docs: stale"])

    def test_a_task_nothing_landed_reads_its_base_as_before(self):
        tid, _ = self.task()
        self.direct("T-0999", {"plugin/skills/x/SKILL.md": "x\n"})
        self.assertEqual(self.gaps(tid), ["self docs: stale"])


class Proved(Base):
    def test_a_listed_file_another_lane_landed_asks_this_task_for_no_proof(self):
        tid = self.around({"a.py": "VALUE = 2\n"}, {"secret.py": "SCORE = 2\n"}, {"secret.py": "SCORE = 3\n"})
        self.assertEqual(cli._proved_gaps(self.p, self.brief(tid)), [])

    def test_its_own_listed_file_still_asks_for_a_proof(self):
        tid = self.around({"secret.py": "SCORE = 9\n"}, {"b.py": "VALUE = 2\n"}, {"b.py": "VALUE = 3\n"})
        (gap,) = cli._proved_gaps(self.p, self.brief(tid))
        self.assertIn("secret.py is listed in .fmproved", gap)

    def test_a_task_nothing_landed_reads_its_base_as_before(self):
        tid, _ = self.task()
        self.direct("T-0999", {"secret.py": "SCORE = 2\n"})
        self.assertEqual(len(cli._proved_gaps(self.p, self.brief(tid))), 1)


class TestsOnlyClaim(Base):
    """T-4712: a plan let through as test-only despite a one-way word (recorded at focus: meta tests_only) is held to its
    change at the close: a file outside the tests asks for the rival read the gate would have asked at plan time."""
    ON = {"FOREMAN_NO_RIVAL": "0"}
    # T-4719's step: tests only; T-1290's: really a non-test edit (each names its test file: the plan's positive evidence)
    RELEASE = "failing test in plugin/tests/test_land_ship.py, bound the hook's loop, release held pushes in tearDown"
    DELETE = "failing test in plugin/tests/test_land_ship.py, then delete the copy of the rules"

    def plan(self, step):
        tid = self.fm_json("task", "new", "Fix a test", "--type", "FEATURE", "--tier", "S", "--ac",
                           "it works :: python3 plugin/tests/run.py test_land_ship", "--step", step, env=self.ON)["id"]
        self.fm("focus", tid, env=self.ON)
        return tid, self.head()

    def gaps(self, tid):
        with mock.patch.dict(os.environ, self.ON):
            return cli._tests_only_gaps(self.p, self.brief(tid))

    def test_the_gate_lets_the_plan_through_and_records_why(self):
        tid, _ = self.plan(self.DELETE)
        self.assertEqual(self.brief(tid).meta.get("tests_only"), "delete")

    def test_a_plan_with_no_one_way_word_records_nothing_and_never_asks(self):
        tid, base = self.plan("failing test, then fix the loop")
        self.assertIsNone(self.brief(tid).meta.get("tests_only"))
        self.land(tid, {"a.py": "VALUE = 2\n"}, base)
        self.assertEqual(self.gaps(tid), [])

    def test_a_change_of_test_files_alone_closes_as_today(self):  # T-4719
        tid, base = self.plan(self.RELEASE)
        self.land(tid, {"plugin/tests/test_land_ship.py": "X = 1\n"}, base)
        self.assertEqual(self.gaps(tid), [])

    def test_a_change_that_touches_a_non_test_file_is_refused_naming_it(self):  # T-1290
        tid, base = self.plan(self.DELETE)
        self.land(tid, {"plugin/tests/test_land_ship.py": "X = 1\n", "a.py": "VALUE = 2\n"}, base)
        (gap,) = self.gaps(tid)
        self.assertIn("a.py", gap)
        self.assertNotIn("test_land_ship", gap)
        self.assertIn(f"sampo second rival {tid}", gap)

    def test_the_rival_read_clears_it_and_so_does_the_gate_being_off(self):
        tid, base = self.plan(self.DELETE)
        self.land(tid, {"a.py": "VALUE = 2\n"}, base)
        self.assertEqual(len(self.gaps(tid)), 1)
        with mock.patch.dict(os.environ, {"FOREMAN_NO_RIVAL": "1"}):
            self.assertEqual(cli._tests_only_gaps(self.p, self.brief(tid)), [])
        b = self.brief(tid)
        b.meta["rival_at"] = "2026-01-01T00:00:00Z"
        with mock.patch.dict(os.environ, self.ON):
            self.assertEqual(cli._tests_only_gaps(self.p, b), [])

    def test_a_file_another_lane_landed_is_not_this_tasks_change(self):
        tid, base = self.plan(self.DELETE)
        self.direct("T-0998", {"b.py": "VALUE = 2\n"})
        self.land(tid, {"plugin/tests/test_land_ship.py": "X = 1\n"}, base)
        self.direct("T-0999", {"own.py": "VALUE = 2\n"})
        self.assertEqual(self.gaps(tid), [])

    def test_the_finish_refuses_it_before_anything_runs(self):
        tid, base = self.plan(self.DELETE)
        self.land(tid, {"a.py": "VALUE = 2\n"}, base)
        r = self.fm("task", "finish", tid, "--audit", "self", check=False, env=self.ON)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("let through as test-only", r.stdout + r.stderr)
        self.assertIn(f"sampo second rival {tid}", r.stdout + r.stderr)

    def test_task_done_is_no_way_round_it(self):  # review: done skipped finish's pre-checks
        tid, base = self.plan(self.DELETE)
        self.land(tid, {"a.py": "VALUE = 2\n"}, base)
        r = self.fm("task", "done", tid, check=False, env=self.ON)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("let through as test-only", r.stdout + r.stderr)
        self.assertIn(f"sampo second rival {tid}", r.stdout + r.stderr)

    def test_a_refocus_after_the_plan_changed_keeps_the_record(self):  # review: the re-focus popped it
        tid, base = self.plan(self.DELETE)
        self.fm("task", "step", tid, "add", "release plugin/lib/x.py to users", env=self.ON)
        self.fm("focus", tid, env=self.ON, check=False)
        self.assertEqual(self.brief(tid).meta.get("tests_only"), "delete")
        self.land(tid, {"a.py": "VALUE = 2\n"}, base)
        self.assertEqual(len(self.gaps(tid)), 1)

    def test_a_record_it_cannot_check_refuses_and_says_so(self):  # review: it failed open with no git root or base
        tid, base = self.plan(self.DELETE)
        for what in ("git_root", "task_base"):
            with mock.patch.object(c, what, return_value=None):
                (gap,) = self.gaps(tid)
            self.assertIn("could not read what it changed", gap)
            self.assertIn(f"sampo second rival {tid}", gap)
        b = self.brief(tid)
        b.meta["rival_at"] = "2026-01-01T00:00:00Z"
        with mock.patch.object(c, "task_base", return_value=None), mock.patch.dict(os.environ, self.ON):
            self.assertEqual(cli._tests_only_gaps(self.p, b), [])

    def test_the_ledger_of_other_lanes_is_read_once_not_once_per_file(self):  # review: a set rebuilt per file
        tid, base = self.plan(self.DELETE)
        self.land(tid, {"a.py": "VALUE = 2\n", "b.py": "VALUE = 2\n", "own.py": "VALUE = 2\n"}, base)
        with mock.patch.object(cli, "_theirs", wraps=cli._theirs) as theirs:
            (gap,) = self.gaps(tid)
        self.assertEqual(theirs.call_count, 1)
        self.assertIn("a.py, b.py, own.py", gap)


class FileMap(Base):
    def test_a_directory_another_lane_added_is_not_this_tasks_to_list(self):
        tid = self.around({"a.py": "VALUE = 2\n"}, {"plugin/newdir/x": "x\n"}, {"plugin/laterdir/x": "x\n"})
        self.assertEqual(cli._file_map_gaps(self.p, self.brief(tid)), [])

    def test_its_own_new_directory_is_named_and_only_that_one(self):
        tid = self.around({"plugin/owndir/x": "x\n"}, {"plugin/newdir/x": "x\n"}, {"plugin/laterdir/x": "x\n"})
        (gap,) = cli._file_map_gaps(self.p, self.brief(tid))
        self.assertIn("plugin/owndir", gap)
        self.assertNotIn("newdir", gap)
        self.assertNotIn("laterdir", gap)

    def test_a_task_nothing_landed_reads_its_base_as_before(self):
        tid, _ = self.task()
        self.direct("T-0999", {"plugin/newdir/x": "x\n"})
        (gap,) = cli._file_map_gaps(self.p, self.brief(tid))
        self.assertIn("plugin/newdir", gap)


class TierDrift(Base):
    BIG = "".join(f"line {n}\n" for n in range(200))

    def test_lines_another_lane_added_to_the_same_file_are_not_this_tasks_size(self):
        tid = self.around({"a.py": "VALUE = 2\n"}, None, {"a.py": "VALUE = 2\n" + self.BIG})
        self.assertIsNone(cli._tier_drift(self.p, self.brief(tid), ["a.py"]))

    def test_its_own_big_change_still_drifts(self):
        tid = self.around({"a.py": "VALUE = 2\n" + self.BIG}, None, {"b.py": "VALUE = 5\n"})
        self.assertIn("M-sized", cli._tier_drift(self.p, self.brief(tid), ["a.py"]))

    def test_a_task_nothing_landed_reads_its_base_as_before(self):
        tid, _ = self.task()
        self.direct("T-0999", {"a.py": "VALUE = 2\n" + self.BIG})
        self.assertIn("M-sized", cli._tier_drift(self.p, self.brief(tid), ["a.py"]))


class Deleted(Base):
    def test_a_file_another_lane_deleted_is_not_this_tasks_deletion(self):
        tid = self.around({"a.py": "VALUE = 2\n"}, None, None, other_gone=())
        self.git("rm", "-q", "old.py")
        self.git("commit", "-qm", "Other work (T-0999)")
        self.assertEqual(cli._deleted(self.p, self.brief(tid)), [])

    def test_its_own_deletion_is_still_named(self):
        tid = self.around({"a.py": "VALUE = 2\n"}, {"b.py": "VALUE = 2\n"}, None, gone=["own.py"])
        self.git("rm", "-q", "old.py")
        self.git("commit", "-qm", "Other work (T-0999)")
        self.assertEqual(cli._deleted(self.p, self.brief(tid)), ["own.py"])

    def test_an_uncommitted_deletion_after_the_landing_is_still_named(self):
        tid = self.around({"a.py": "VALUE = 2\n"}, None, None)
        os.remove(os.path.join(self.repo, "old.py"))
        self.assertEqual(cli._deleted(self.p, self.brief(tid)), ["old.py"])

    def test_a_task_nothing_landed_reads_its_base_as_before(self):
        tid, _ = self.task()
        self.git("rm", "-q", "old.py")
        self.git("commit", "-qm", "Other work (T-0999)")
        self.assertEqual(cli._deleted(self.p, self.brief(tid)), ["old.py"])


class Scaffolding(Base):
    DEBUG = "VALUE = 2\nbreakpoint()\n"

    def test_a_debug_call_another_lane_added_to_the_same_file_is_not_this_tasks(self):
        tid = self.around({"a.py": "VALUE = 2\n"}, None, {"a.py": self.DEBUG})
        self.assertEqual(cli._scaffolding(self.p, self.brief(tid), ["a.py"]), ([], []))

    def test_its_own_debug_call_is_still_named(self):
        tid = self.around({"a.py": self.DEBUG}, None, {"b.py": "VALUE = 5\n"})
        hits, conflicts = cli._scaffolding(self.p, self.brief(tid), ["a.py"])
        self.assertEqual([h.split(":")[0] for h in hits], ["a.py"])
        self.assertEqual(conflicts, [])

    def test_an_uncommitted_debug_call_after_the_landing_is_still_named(self):
        tid = self.around({"a.py": "VALUE = 2\n"}, None, {"b.py": "VALUE = 5\n"})
        self.put({"a.py": "VALUE = 2\nbreakpoint()\n"})
        self.assertEqual(len(cli._scaffolding(self.p, self.brief(tid), ["a.py"])[0]), 1)

    def test_a_task_nothing_landed_reads_its_base_as_before(self):
        tid, _ = self.task()
        self.direct("T-0999", {"a.py": self.DEBUG})
        self.assertEqual(len(cli._scaffolding(self.p, self.brief(tid), ["a.py"])[0]), 1)


class TheView(Base):
    def view(self, tid, **kw):
        b = self.brief(tid)
        return cli._own_view(self.p, b, c.task_base(self.p.root, b), **kw)

    def test_a_landed_task_is_its_landing_plus_what_is_uncommitted(self):
        tid = self.around({"a.py": "VALUE = 2\n"}, {"b.py": "VALUE = 3\n"}, {"old.py": "VALUE = 3\n"})
        self.put({"own.py": "VALUE = 2\n"})
        view = self.view(tid)
        self.assertEqual(view.files, ["a.py", "own.py"])  # own.py: uncommitted, nobody else's

    def test_a_task_nothing_landed_is_base_to_tree(self):
        tid, base = self.task()
        self.direct("T-0999", {"b.py": "VALUE = 3\n"})
        view = self.view(tid)
        self.assertEqual(view.files, ["b.py"])
        self.assertEqual(view.base, c.task_base(self.p.root, self.brief(tid)))  # the recorded base, never moved

    def test_a_lane_reads_base_to_tree_whatever_the_log_says(self):
        tid = self.around({"a.py": "VALUE = 2\n"}, {"b.py": "VALUE = 3\n"}, None)
        with mock.patch.object(self.p, "lane", "lane-x", create=True):
            view = self.view(tid)
            self.assertEqual(view.files, ["a.py", "b.py"])

    def test_the_diff_is_the_landings_own_text_and_the_uncommitted_edits(self):
        tid = self.around({"a.py": "VALUE = 2\n"}, None, {"a.py": "VALUE = 2\nLATER = 1\n"})
        view = self.view(tid)
        text = view.diff("-U0", paths=["a.py"])
        self.assertIn("+VALUE = 2", text)
        self.assertNotIn("LATER", text)
        self.assertNotIn("LATER", view.text())

    def test_the_four_older_readers_see_the_same_files(self):
        tid = self.around({"a.py": "VALUE = 2\n"}, {"b.py": "VALUE = 3\n"}, {"old.py": "VALUE = 3\n"})
        b = self.brief(tid)
        self.fm("check", "affected", "python3 tests/run.py {names}")
        _, _, changed = cli.affected(self.p, b)
        self.assertEqual(sorted(changed), ["a.py"])
        self.assertEqual(sorted(cli._task_files(self.p, b)[0]), ["a.py"])
        line = cli._landed(self.p, b)
        self.assertIn(self.git("log", "-1", "--format=%h", "--", "a.py").strip(), line)
        self.assertNotIn(self.git("log", "-1", "--format=%h", "--", "old.py").strip(), line)


# ---- the guard ----------------------------------------------------------------------------------------------------
# A function in cli.py that reads a task's change from its recorded base must go through _own_view. The primitives: the
# map's changed()/changed_symbols(), core's task_diff() and landed_change(), and a git diff run through _git or subprocess.
VIEW = {"_OwnView._landing", "_OwnView.files", "_OwnView.diff", "_OwnView.text"}  # the one shared reader (_own_view builds it)
PRIMITIVES = {"changed", "changed_symbols", "task_diff", "landed_change"}
ALLOWED = {  # name: why it reads a diff itself, and why that is no close-time reading of a task's change
    "_commit_task": "stages the files to commit, from the task's focus snapshot: the writer of the close's commit, whose "
                    "unlanded behaviour is locked and which a landed task has nothing left to stage in",
    "task_prove_hunks": "sampo task prove --hunks: the hunks of base..tree no check notices, an audit of the whole diff by design",
    "task_prove": "sampo task prove: runs a test on the start tree and the tree now; its changed tests are the whole range by design",
    "_cmd_check": "sampo check: the files a gate run wrote, one tree against the next",
    "_paths_unchanged": "a gate's cached pass: the tree it passed on against the tree now, no task involved",
    "_tree_diff": "files between two trees a caller names, no task base",
    "_residual": "T-4822: the files between a gate row's code tree and the code tree now (two trees, no task base)",
    "_hub_tests": "symbols of the files changed between a base and a tree the caller (affected) hands it, already the view's",
    "_audit_scan": "sampo audit scan: any diff (a branch, a PR checkout), no task needed",
    "cmd_audit": "the audit brief's diff is the whole range the reviewer reads, by design (a separate reader, not a gate)",
}


def _primitive_calls(call):
    """The base-diff primitive a Call node is, or None."""
    f = call.func
    name = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else ""
    if name in PRIMITIVES:
        return name
    if name in ("_git", "run", "check_output", "Popen"):
        for arg in call.args:
            if any(isinstance(n, ast.Constant) and n.value == "diff" for n in ast.walk(arg)):
                return f"{name}(… 'diff' …)"
    return None


def offenders(source):
    """{function: [primitives]} of every top-level function and method in `source` (a nested def counts for its outer
    function) that calls a base-diff primitive."""
    found = {}
    tree = ast.parse(source)
    funcs = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs.append((node.name, node))
        elif isinstance(node, ast.ClassDef):
            funcs += [(f"{node.name}.{m.name}", m) for m in node.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for name, fn in funcs:
        calls = sorted({hit for n in ast.walk(fn) if isinstance(n, ast.Call) and (hit := _primitive_calls(n))})
        if calls:
            found[name] = calls
    return found


READERS = ["affected", "_task_view", "_landed",  # T-5715: _task_files is _task_view's first three, the diff kept for the tier "_owed_companions", "_self_docs_gaps", "_proved_gaps", "_file_map_gaps",
           "_tier_drift", "_deleted", "_scaffolding", "_tests_only_gaps"]  # T-4712: the eleventh


class Guard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(CLI, encoding="utf-8") as f:
            cls.source = f.read()

    def test_no_function_but_the_view_and_the_named_ones_reads_a_base_diff_itself(self):
        bad = {k: v for k, v in offenders(self.source).items() if k not in VIEW and k not in ALLOWED}
        self.assertEqual(bad, {}, "a close-time reader diffs the task's base itself: go through cli._own_view (the "
                                  "landed task's own change), or name the function in ALLOWED with its reason")

    def test_each_of_the_ten_readers_goes_through_the_view(self):
        funcs = {n.name: n for n in ast.parse(self.source).body if isinstance(n, ast.FunctionDef)}
        for name in READERS:
            calls = {n.func.id for n in ast.walk(funcs[name]) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
            self.assertIn("_own_view", calls, f"{name} must read the task's change through _own_view")

    def test_the_view_does_read_the_primitives_and_every_allowed_name_still_does(self):
        found = offenders(self.source)  # a stale allow-list entry would hide a later offender under its name
        for name in sorted(VIEW | set(ALLOWED)):
            self.assertIn(name, found, f"{name} no longer calls a base-diff primitive: drop it from the guard's lists")

    def test_a_planted_reader_that_diffs_the_base_is_caught(self):
        planted = self.source + ("\n\ndef _eleventh_gap(p, b):\n    base = c.task_base(p.root, b)\n"
                                 "    return smap.changed(p.root, base)\n")
        bad = {k for k in offenders(planted) if k not in VIEW and k not in ALLOWED}
        self.assertEqual(bad, {"_eleventh_gap"})

    def test_a_planted_git_diff_in_a_method_or_a_nested_helper_is_caught(self):
        src = ("class K:\n    def m(self, p, base):\n        return c._git(p.root, 'diff', '--numstat', base)\n\n\n"
               "def outer(p, base):\n    def inner():\n        return subprocess.run(['git', 'diff', base])\n    return inner()\n\n\n"
               "def fine(p):\n    return c._git(p.root, 'log', '-1')\n")
        self.assertEqual(sorted(offenders(src)), ["K.m", "outer"])

    def test_a_planted_task_diff_or_landed_change_is_caught(self):
        src = "def a(p, b):\n    return c.task_diff(p.root, 'x')\n\n\ndef b(p, x):\n    return c.landed_change(p.root, x)\n"
        self.assertEqual(sorted(offenders(src)), ["a", "b"])


if __name__ == "__main__":
    unittest.main()
