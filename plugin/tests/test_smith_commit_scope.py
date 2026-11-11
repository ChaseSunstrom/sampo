"""T-4387: fm code's close commit holds what the run changed, not what its commands built. A directory a command made
(30 small files from a shell loop here, cargo's target/ in the exam run) that none of the run's own write and edit tools
touched is listed in the repo's .git/info/exclude, left out of the changed event and the commit, and named with its
count; a lockfile, a tracked file a command rewrote and a file the run wrote itself stay."""
import json
import os
import subprocess

from helpers import read_text
from test_code import PLAN, CodeCase, call, plan

BUILD = "mkdir -p out && for i in $(seq 1 30); do echo $i > out/f$i.o; done"
FIX = call("edit", file="calc.py", old="a - b", new="a + b")


class CommitScope(CodeCase):
    def git(self, *a):
        return subprocess.run(["git", "-C", self.repo, *a], capture_output=True, text=True).stdout

    def committed(self, rev="HEAD"):
        return [x for x in self.git("show", "--name-only", "--format=", rev).splitlines() if x]

    def exclude(self):
        path = os.path.join(self.repo, ".git", "info", "exclude")
        lines = read_text(path).splitlines() if os.path.exists(path) else []
        return [x for x in lines if x and not x.startswith("#")]  # git's template comments aside

    def left(self):  # what git status shows beyond the __pycache__ a verify's python leaves (core.ARTEFACT's business)
        return "".join(x + "\n" for x in self.status().splitlines() if "__pycache__" not in x)

    def put(self, rel, text):
        full = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(full) or self.repo, exist_ok=True)
        with open(full, "w") as f:
            f.write(text)

    def test_build_output_left_out_of_the_commit_and_named(self):
        self.serve("openai", plan(), call("run", cmd=BUILD), FIX, call("done", summary="added"))
        r = self.code("make calc.add add instead of subtract", "--model", "fake")
        self.assertEqual(self.task()["status"], "done", r.stdout)
        self.assertEqual(self.committed(), ["calc.py"], r.stdout)
        self.assertEqual(self.left(), "", "the tree is clean afterwards")
        self.assertEqual(len(os.listdir(os.path.join(self.repo, "out"))), 30, "the files stay on disk")
        line = next((x for x in r.stdout.splitlines() if "left out out/" in x and "30" in x), "")
        self.assertIn(".gitignore", line, r.stdout)  # how to keep it
        self.assertIn("/out/", self.exclude())

    def test_build_output_left_out_of_the_changed_event(self):
        self.serve("openai", plan(), call("run", cmd=BUILD), FIX, call("done", summary="added"))
        r = self.code("make calc.add add instead of subtract", "--model", "fake", "--json", "--no-commit")
        events = [json.loads(x) for x in r.stdout.splitlines()]
        changed = [e for e in events if e["event"] == "changed"]
        self.assertTrue(changed)
        self.assertEqual(changed[-1]["files"], ["calc.py"])
        said = [e for e in events if e["event"] == "built_out"]
        self.assertEqual([(e["dir"], e["count"]) for e in said], [("out/", 30)])
        self.assertNotIn("out/", self.status())

    def test_lockfile_and_tracked_kept_beside_the_left_out_directory(self):
        # (a file named like a lockfile would make the close ask for a security audit: this is the same shape, one file)
        cmd = BUILD + " && echo pin > deps.pin && echo more >> README.md && mkdir -p few && echo a > few/a && echo b > few/b"
        self.serve("openai", plan(), call("run", cmd=cmd), FIX, call("done", summary="added"))
        r = self.code("make calc.add add instead of subtract", "--model", "fake")
        self.assertEqual(sorted(self.committed()), ["README.md", "calc.py", "deps.pin", "few/a", "few/b"], r.stdout)
        self.assertEqual(self.left(), "")

    def test_own_write_kept_in_a_large_new_directory(self):
        cmd = "mkdir -p gen && for i in $(seq 1 30); do echo $i > gen/f$i.txt; done"
        self.serve("openai", plan(), call("write", file="gen/own.txt", content="mine\n"), call("run", cmd=cmd), FIX,
                   call("done", summary="added"))
        r = self.code("make calc.add add instead of subtract", "--model", "fake")
        got = self.committed()
        self.assertEqual(len(got), 32, got)  # calc.py, the written file, and the 30 beside it
        self.assertIn("gen/own.txt", got)
        self.assertNotIn("/gen/", self.exclude())
        self.assertNotIn("left out", r.stdout)

    def test_own_write_kept_is_journaled_for_a_resume(self):
        self.serve("openai", plan(), call("write", file="gen/own.txt", content="mine\n"), FIX, call("done", summary="x"))
        self.code("make calc.add add instead of subtract", "--model", "fake", "--no-commit")
        journal = [p for p in self.journals() if p.endswith("T-0001.jsonl") and not p.endswith(".events.jsonl")]
        wrote = [json.loads(x) for p in journal for x in read_text(p).splitlines() if '"wrote"' in x]
        self.assertEqual([w["file"] for w in wrote], ["gen/own.txt", "calc.py"])

    def journals(self):
        out = []
        for d, _, names in os.walk(self.home):
            out += [os.path.join(d, n) for n in names if n.endswith(".jsonl")]
        return out

    def test_a_directory_in_a_unit_call_is_found_by_who_wrote_it(self):
        from sampo import smith
        os.makedirs(os.path.join(self.repo, "out"))
        for i in range(25):
            self.put(f"out/{i}", "x")
        self.put("mixed/mine", "x")
        for i in range(25):
            self.put(f"mixed/{i}", "x")
        for i in range(5):
            self.put(f"small/{i}", "x")
        got = smith._leave_out_built(self.repo, {"mixed/mine"})
        self.assertEqual(got, [("out/", 25)])
        self.assertEqual(smith._leave_out_built(self.repo, {"mixed/mine"}), [], "found once, written once")
        self.assertEqual(self.exclude(), ["/out/"])

    def test_exclude_is_local_and_once(self):
        self.serve("openai", plan(), call("run", cmd=BUILD), FIX, call("done", summary="added"))
        self.code("make calc.add add instead of subtract", "--model", "fake")
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".gitignore")), "never the user's .gitignore")
        self.assertEqual(self.exclude().count("/out/"), 1)
        noted = dict(PLAN, title="Note it", criteria=[{"text": "notes exist", "verify": "test -f notes.txt"}])
        self.serve("openai", plan(noted), call("run", cmd=BUILD), call("write", file="notes.txt", content="x\n"),
                   call("done", summary="noted"), name="again")
        self.code("note it", "--model", "again")
        self.assertEqual(self.exclude().count("/out/"), 1, "a second run writes it no second time")
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".gitignore")))
        self.assertEqual(self.left(), "")

    def test_exclude_is_local_and_once_leaves_a_directory_the_user_had_before_the_run_alone(self):
        for i in range(30):
            self.put(f"data/{i}.csv", "x")  # the user's own, untracked, before fm code starts
        self.serve("openai", plan(), FIX, call("done", summary="added"))
        r = self.code("make calc.add add instead of subtract", "--model", "fake")
        self.assertEqual(self.exclude(), [], r.stdout)
        self.assertIn("data/", self.status())
        self.assertNotIn("left out data/", r.stdout)

    def test_exclude_is_local_and_once_when_gitignore_already_covers_it(self):
        self.put(".gitignore", "out/\n")
        subprocess.run(["git", "-C", self.repo, "add", ".gitignore"], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", "ignore out"], check=True)
        self.serve("openai", plan(), call("run", cmd=BUILD), FIX, call("done", summary="added"))
        r = self.code("make calc.add add instead of subtract", "--model", "fake")
        self.assertEqual(read_text(os.path.join(self.repo, ".gitignore")), "out/\n")
        self.assertEqual(self.exclude().count("/out/"), 0)
        self.assertNotIn("left out", r.stdout)
        self.assertEqual(self.committed(), ["calc.py"])
