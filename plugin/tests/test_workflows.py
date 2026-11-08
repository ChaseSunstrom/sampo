"""T-0787: the workflows Foreman ships (plugin/workflows/*.js, run as /sampo:<name>) load: a pure-literal meta first,
its name the file's, every phase() title declared in meta.phases, valid ES module syntax, and nothing that breaks resume
(Date.now, Math.random, an argless new Date)."""
import glob
import os
import re
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
FLOWS = sorted(glob.glob(os.path.join(HERE, "..", "workflows", "*.js")))


class Workflows(unittest.TestCase):
    def test_review_and_ideas_ship(self):
        self.assertEqual({os.path.basename(f) for f in FLOWS} >= {"review.js", "ideas.js"}, True, FLOWS)

    def test_each_loads(self):
        for path in FLOWS:
            with self.subTest(path=os.path.basename(path)), open(path, encoding="utf-8") as f:
                text = f.read()
                self.assertTrue(text.startswith("export const meta = {"), "meta must be the first statement")
                meta = text[:text.index("\n}\n") + 2]
                self.assertNotRegex(meta, r"\$\{|\.\.\.|\w\(", "meta is a pure literal: no interpolation, spreads or calls")
                name = re.search(r"\bname:\s*'([\w-]+)'", meta)
                self.assertEqual(name and name.group(1), os.path.basename(path)[:-3])
                self.assertRegex(meta, r"\bdescription:\s*'[^']+'")
                declared = set(re.findall(r"\{\s*title:\s*'([^']+)'", meta))
                used = set(re.findall(r"\bphase\(\s*'([^']+)'", text)) | set(re.findall(r"phase:\s*'([^']+)'", text))
                self.assertTrue(used, "phases are named")
                self.assertLessEqual(used, declared, "every phase used is declared in meta.phases")
                self.assertNotRegex(text, r"Date\.now|Math\.random|new Date\(\s*\)", "breaks resume")

    @unittest.skipUnless(shutil.which("node"), "node isn't installed")
    def test_each_parses(self):
        # the runner executes the body as an async function (top-level await and return), so check it that way
        for path in FLOWS:
            with self.subTest(path=os.path.basename(path)), tempfile.TemporaryDirectory() as d, \
                    open(path, encoding="utf-8") as f:
                body = f.read().replace("export const meta", "const meta", 1)
                wrapped = os.path.join(d, "flow.js")
                with open(wrapped, "w", encoding="utf-8") as out:
                    out.write("async function workflow(agent, parallel, pipeline, phase, log, args, budget) {\n"
                              + body + "\n}\n")
                r = subprocess.run(["node", "--check", wrapped], capture_output=True, text=True, timeout=60)
                self.assertEqual(r.returncode, 0, r.stderr)


# T-1245: the repo's own CI workflows (.github/workflows/*.yml), read as text (stdlib only): every action is pinned to a
# 40-hex commit SHA, and pages.yml builds read-only while only its deploy job holds pages/id-token write.
CI = os.path.join(HERE, "..", "..", ".github", "workflows")


def ci_text(name):
    with open(os.path.join(CI, name), encoding="utf-8") as f:
        return f.read()


def ci_jobs(text):
    """(the text before `jobs:`, {job name: its text})."""
    top, _, rest = text.partition("\njobs:\n")
    parts = re.split(r"(?m)^  ([\w-]+):\s*$", rest)
    return top, {parts[i]: parts[i + 1] for i in range(1, len(parts), 2)}


class CiWorkflows(unittest.TestCase):
    def test_every_uses_is_pinned(self):
        for name in sorted(os.listdir(CI)):
            if not name.endswith((".yml", ".yaml")):
                continue
            lines = [l for l in ci_text(name).splitlines() if re.match(r"\s*-?\s*uses:", l)]
            self.assertTrue(lines, name)
            for l in lines:
                self.assertRegex(l, r"uses:\s*\S+@[0-9a-f]{40}\s+#\s*\S+", f"{name}: {l.strip()}")

    def test_pages_permissions_split(self):
        top, js = ci_jobs(ci_text("pages.yml"))
        self.assertTrue({"build", "deploy"} <= set(js), list(js))
        self.assertRegex(js["deploy"], r"(?m)^    needs:\s*build\s*$")
        for w in ("pages: write", "id-token: write"):
            self.assertNotIn(w, top)
            self.assertNotIn(w, js["build"])
            self.assertIn(w, js["deploy"])
        self.assertIn("contents: read", js["build"])
        self.assertNotRegex(top, r"(?m)^\s*\w[\w-]*:\s*write\b")


if __name__ == "__main__":
    unittest.main()
