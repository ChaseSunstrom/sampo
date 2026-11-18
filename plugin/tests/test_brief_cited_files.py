"""T-4928: `sampo lane brief` checks the files a brief names against the base the builder starts from. T-4861 cited
wardhead.py, which only T-2981's unlanded lane branch added: the builder spent a run finding the base lacked it. A file
another task's unlanded branch adds refuses the brief, naming that task and the depends_on to add; a repo path no branch
has is listed in the brief as one to create (a task may name what it makes); the task's own branch never counts."""
import json
import os
import subprocess

from helpers import ForemanTestCase

from sampo import core as c


class CitedFiles(ForemanTestCase):
    repo_seed = ("seed", (("lib/a.py", "x = 1\n"),))

    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)

    def git(self, *args):
        return subprocess.run(["git", "-C", self.repo, *args], capture_output=True, text=True, check=True).stdout

    def task(self, title, raw, scope=None):
        tid = json.loads(self.fm("task", "new", title, "--type", "FIX", "--tier", "S", "--raw", raw, "--ac",
                                 "ok :: true", "--step", "s", "--json").stdout)["id"]
        if scope:
            self.fm("task", "set", tid, "scope=" + ",".join(scope))
        return tid

    def branch_adding(self, tid, path):
        """A lane branch foreman/<tid> with one commit adding `path`, the main checkout left on its own branch."""
        head = self.git("rev-parse", "--abbrev-ref", "HEAD").strip()
        self.git("checkout", "-q", "-b", f"foreman/{tid}")
        full = os.path.join(self.repo, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write("y = 2\n")
        self.git("add", path)
        self.git("commit", "-q", "-m", f"add {path} ({tid})")
        self.git("checkout", "-q", head)

    def brief(self, tid):
        r = self.fm("lane", "brief", tid, "--json", check=False)
        return r.returncode, r.stdout + r.stderr

    def test_a_file_only_an_unlanded_branch_adds_refuses_the_brief_and_names_its_task(self):
        holder = self.task("Ward head", "Add the ward head module")
        self.branch_adding(holder, "lib/wardhead.py")
        tid = self.task("Breaker", "Ward head breaker (wardhead.py:53-54): a slow embed trips it")
        code, out = self.brief(tid)
        self.assertNotEqual(code, 0, out)
        self.assertIn("wardhead.py", out)
        self.assertIn(holder, out)
        self.assertIn(f"depends_on+={holder}", out, "the refusal names the dependency to add")
        self.fm("task", "set", tid, f"depends_on={holder}")
        code, out = self.brief(tid)
        self.assertNotEqual(code, 0, out)
        self.assertIn(f"waits on {holder}", out, "with the dependency set, it says it waits until that branch lands")
        scoped = self.task("Scoped", "Tune the breaker in wardhead.py", scope=["lib/wardhead.py"])
        code, out = self.brief(scoped)
        self.assertNotEqual(code, 0, "a scope file counts as cited")
        self.assertIn(holder, out)
        self.assertNotIn("(and", out, "the scope's path and the prose's bare name are one file")

    def test_once_the_branch_lands_the_brief_is_written(self):
        holder = self.task("Ward head", "Add the ward head module")
        self.branch_adding(holder, "lib/wardhead.py")
        self.git("merge", "-q", "--no-ff", "-m", f"Merge {holder}", f"foreman/{holder}")
        tid = self.task("Breaker", "Ward head breaker (lib/wardhead.py:53): a slow embed trips it")
        code, out = self.brief(tid)
        self.assertEqual(code, 0, out)
        self.assertNotIn("## Files the base lacks", open(json.loads(out[:out.rindex("}") + 1])["path"]).read())

    def test_a_path_no_branch_has_is_listed_to_create_not_refused(self):
        tid = self.task("New module", "Add lib/newthing.py beside lib/a.py; see docs at https://example.org/x/y.py")
        code, out = self.brief(tid)
        self.assertEqual(code, 0, out)
        text = open(json.loads(out[:out.rindex("}") + 1])["path"]).read()
        self.assertIn("## Files the base lacks", text)
        self.assertIn("lib/newthing.py", text)
        self.assertNotIn("x/y.py", text.split("## Files the base lacks")[1], "a URL's path is not a repo file")
        self.assertNotIn("- lib/a.py", text, "a file the base has is not listed")

    def test_the_task_s_own_branch_never_counts(self):
        tid = self.task("Own work", "Finish lib/own.py")
        self.branch_adding(tid, "lib/own.py")
        code, out = self.brief(tid)
        self.assertEqual(code, 0, out)
