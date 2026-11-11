"""T-4206: an edit never leaves Python running the old code. Python trusts a .pyc whose recorded source mtime (whole
seconds) and size still match, so a same-size edit ('a - b' to 'a + b') inside the second the file was last written
ran the old bytecode: the fixed test read red, and test_smith_budget.Carry failed one run in three. The edited file's
cached bytecode goes with the write. Each test puts the old mtime back after the edit: the same second, every run."""
import os
import subprocess
import sys

from test_batch_edit import _Files
from test_code import call
from test_smith_budget import Budget, VERIFY


def clean_env():
    return {k: v for k, v in os.environ.items() if k not in ("PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX")}


class EditBytecode(_Files):
    def value(self):
        return subprocess.run([sys.executable, "-c", "import a; print(a.x)"], cwd=self.repo, capture_output=True,
                              text=True, env=clean_env()).stdout.strip()

    def test_bytecode_a_same_size_edit_in_the_same_second_is_what_python_runs(self):
        path = os.path.join(self.repo, "a.py")
        was = os.stat(path)
        self.assertEqual(self.value(), "1")
        self.assertTrue(os.listdir(os.path.join(self.repo, "__pycache__")), "the import cached its bytecode")
        self.edit([{"file": "a.py", "old": "x = 1", "new": "x = 7"}])
        os.utime(path, ns=(was.st_atime_ns, was.st_mtime_ns))
        self.assertEqual(self.value(), "7")

    def test_bytecode_another_files_cache_stays(self):
        with open(os.path.join(self.repo, "a2.py"), "w") as f:
            f.write("x = 3\n")
        subprocess.run([sys.executable, "-c", "import a, a2"], cwd=self.repo, check=True, env=clean_env())
        self.edit([{"file": "a.py", "old": "x = 1", "new": "x = 7"}])
        left = os.listdir(os.path.join(self.repo, "__pycache__"))
        self.assertEqual([n.split(".")[0] for n in left], ["a2"], left)


class CodeBytecode(Budget):
    def back(self):
        """A run that puts calc.py's mtime back to what it was before the model's write."""
        was = os.stat(os.path.join(self.repo, "calc.py")).st_mtime_ns
        return call("run", cmd=f"python3 -c \"import os; os.utime('calc.py', ns=({was}, {was}))\"")

    def test_bytecode_the_edit_tool_drops_the_files_cache(self):
        f, r = self.go(call("edit", file="calc.py", old="a - b", new="a + b"), self.back(), VERIFY)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.task()["status"], "done")

    def test_bytecode_the_write_tool_drops_the_files_cache(self):
        with open(os.path.join(self.repo, "calc.py")) as fh:
            fixed = fh.read().replace("a - b", "a + b")
        f, r = self.go(call("write", file="calc.py", content=fixed), self.back(), VERIFY)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.task()["status"], "done")
