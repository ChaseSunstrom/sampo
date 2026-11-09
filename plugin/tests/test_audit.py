"""T-0723: the diff a reviewer reads (fm audit prep) leaves out what it needn't read in full: a change repeated in
synced copies, the far end of long context lines, fixtures and lockfiles. Changed lines always stay whole."""
import json
import os
import subprocess

from helpers import ForemanTestCase
from test_hooks import HookCase

from sampo import cli
from sampo import core as c


def block(path, body):
    return f"diff --git a/{path} b/{path}\nindex 1111111..2222222 100644\n--- a/{path}\n+++ b/{path}\n{body}"


RULE = "@@ -1,3 +1,3 @@\n same\n-old rule\n+new rule\n"


class CompactDiff(ForemanTestCase):
    def test_copies_long_context_and_fixtures_shrink_and_changes_stay_whole(self):
        long_ctx, long_add = " " + "c" * 500, "+" + "a" * 500
        diff = (block("plugin/rules/foreman.md", RULE) + block("plugin/evals/x/prompt.md", RULE)
                + block("plugin/evals/y/prompt.md", RULE)
                + block("MASTER.md", f"@@ -1,2 +1,2 @@\n{long_ctx}\n-a\n{long_add}\n")
                + block("plugin/tests/fixtures/run.txt", "@@ -0,0 +1,3 @@\n+f1\n+f2\n+f3\n")
                + block("package-lock.json", "@@ -1 +1 @@\n-x\n+y\n"))
        out = cli._compact_diff(diff)
        self.assertEqual(out.count("+new rule"), 1, "one copy of a repeated change")
        self.assertIn("plugin/evals/x/prompt.md: the same change as plugin/rules/foreman.md", out)
        self.assertIn("plugin/evals/y/prompt.md: the same change as plugin/rules/foreman.md", out)
        self.assertNotIn("c" * 300, out, "long context is cut")
        self.assertIn(long_add, out, "a changed line stays whole")
        self.assertNotIn("+f2", out)
        self.assertIn("plugin/tests/fixtures/run.txt: a fixture or lockfile, +3 −0 lines, left out", out)
        self.assertIn("package-lock.json: a fixture or lockfile, +1 −1 lines, left out", out)
        self.assertLess(len(out), len(diff))

    def test_pre_audit_flags_a_hotspot_file_only(self):
        from sampo import map as smap
        found = [x for x in smap.pre_audit(self.repo, "", ["a.py", "b.py"], {"fixy": {"a.py": 4}}) if "hotspot" in x]
        self.assertEqual(len(found), 1)
        self.assertIn("a.py", found[0])

    def test_a_file_with_a_changed_line_over_4000_chars_is_left_out(self):
        long = "x" * 20000
        out = cli._compact_diff(block("demo/live.cast", f"@@ -0,0 +1,2 @@\n+{long}\n+short\n")
                                  + block("a.py", "@@ -1 +1 @@\n-x = 1\n+x = 2\n"))
        self.assertIn("demo/live.cast: generated or minified (a changed line over 4000 characters), +2 −0 lines, left out", out)
        self.assertNotIn("x" * 100, out)
        self.assertIn("+x = 2", out)

    def test_a_plain_diff_is_unchanged(self):
        diff = block("a.py", "@@ -1 +1 @@\n-x = 1\n+x = 2\n")
        self.assertEqual(cli._compact_diff(diff), diff)


PERSONAS = ("art director", "accessibility reviewer", "first-time user")


class DesignLens(HookCase):
    """T-1501: a UI diff gets a design read by three personas; any other diff costs nothing extra."""

    def write(self, rel, text="x\n"):
        path = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(path) or self.repo, exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def prep(self, tier="M"):
        self.fm("init")
        self.tid = self.task(tier=tier)

    def brief(self):
        data = json.loads(self.fm("audit", "prep", self.tid, "--json").stdout)
        with open(data["brief"]) as f:
            return data["lenses"], f.read()

    def test_ui_paths_finds_ui_files_and_leaves_the_rest(self):
        ui =["web/app.css", "web/x.scss", "a/b.less", "src/App.tsx", "src/c.jsx", "v/a.vue", "s/a.svelte",
              "p/index.astro", "site/index.html", "mods/foreman-ui/hooks/state.ts", "plugin/lib/fmcodetui.py",
              "plugin/templates/tui/cost_meter.py", "app/templates/home.jinja"]
        other = ["a.py", "plugin/templates/brief.md", "plugin/templates/mcp-server/server.py", "lib/intuition.py",
                 "plugin/tests/test_code_tui.py", "mods/foreman-ui/tests/pane.test.tsx",
                 "plugin/tests/fixtures/design/slop.css", "README.md", "mods/foreman-ui/tools/shot.py",
                 "mods/foreman-ui/hooks/hooks.json", "roles/web/templates/nginx.conf.j2", "demo/code-tui.mp4"]
        self.assertEqual(c.ui_paths(ui + other), ui)

    def test_ui_paths_skips_what_design_lint_skips(self):  # review: built, vendored and generated files aren't UI
        self.assertEqual(c.ui_paths(["dist/app.css", "node_modules/x/a.html", "src/app.css"]), ["src/app.css"])
        skipped = ["web/vendor/b.css", "build/i.html", "out/i.html", "target/site/i.html", "venv/lib/a.html",
                   "static/app.min.css", "htmlcov/index.html", "coverage/lcov-report/index.html"]
        self.assertEqual(c.ui_paths(skipped), [])
        self.assertEqual(c.ui_paths(["plugin/hooks/statusline", "plugin/hooks/subagent-statusline", "bin/status"]),
                         ["plugin/hooks/statusline", "plugin/hooks/subagent-statusline"], "Foreman's own UI")

    def test_a_screenshot_under_a_dot_directory_is_never_listed(self):  # review: ~/.ssh, .config and the like
        self.prep()
        self.write("web/app.css")
        self.write(".cache/shot.png", "png")
        self.write("shots/ok.png", "png")
        self.fm("task", "log", self.tid, "see .cache/shot.png and shots/ok.png")
        design = self.brief()[1].split("## design")[1]
        self.assertNotIn("shot.png", design)
        self.assertIn("ok.png", design)

    def test_only_a_lens_heading_switches_the_lens(self):  # review: "## Design notes" is not the design lens
        from sampo import lanes
        got = lanes.reviewer_findings("## maintainer\n- LOW — a.py:1 — x\n## Design notes\n- LOW — b.py:2 — y\n"
                                        "## Edge cases\n- LOW — c.py:3 — z\n## lens: design\n- LOW — d.py:4 — w\n"
                                        "## edge: changes needed\n- LOW — e.py:5 — v")
        self.assertEqual([x[0] for x in got], ["maintainer"] * 3 + ["design", "edge"])
        self.fm("init")
        self.task()
        self.fm("research", "add", "t0009-review", input="## design-system\n**HIGH — tokens drift**\n"
                                                         "## design: changes needed\n**HIGH — no focus ring**\n")
        out = self.fm("audit", "prep", "--print", "T-0001", "--lens", "design").stdout
        self.assertIn("no focus ring", out)
        self.assertNotIn("tokens drift", out)

    def test_the_reviewer_prompt_and_template_define_the_three_personas(self):
        for rel in (("agents", "fm-reviewer.md"), ("skills", "intake", "references", "audit.md")):
            with open(os.path.join(c.PLUGIN_ROOT, *rel)) as f:
                text = f.read().lower()
            for who in PERSONAS:
                with self.subTest(file=rel[-1], persona=who):
                    self.assertIn(who, text)
            self.assertIn("design", text)
        self.assertIn("design", c.AUDIT_LENSES, "recordable with fm task audit ID design")
        self.assertNotIn("design", c.DEFAULT_LENSES)
        self.assertFalse(any("design" in g for t in c.REQUIRED_AUDITS.values() for g in t), "advisory, not required")

    def test_prep_names_the_design_lens_for_a_ui_diff_with_its_screenshots(self):
        self.prep()
        self.write("web/app.css", "body { color: #333 }\n")
        self.write("shots/home.png", "png")
        self.fm("task", "log", self.tid, "screenshot shots/home.png and shots/gone.png")
        lenses, text = self.brief()
        self.assertEqual(lenses, list(c.DEFAULT_LENSES) + ["design"])
        self.assertIn("## design", text)
        for who in PERSONAS:
            self.assertIn(who, text.lower())
        self.assertIn("web/app.css", text.split("## design")[1])
        self.assertIn(os.path.join(self.repo, "shots", "home.png"), text, "a screenshot the task names, to read")
        self.assertNotIn("gone.png", text.split("## design")[1], "a named screenshot that isn't there is left out")

    def test_an_s_task_with_a_ui_diff_gets_the_design_lens_too(self):
        self.prep(tier="S")
        self.write("src/App.tsx", "export const A = () => null\n")
        lenses, text = self.brief()
        self.assertEqual(lenses, ["self", "design"])
        self.assertIn("No screenshots", text)

    def test_a_base_that_is_an_option_is_refused_and_writes_nothing(self):  # T-2318
        self.prep()
        out = os.path.join(self.repo, "pwned.txt")
        r = self.fm("audit", "prep", self.tid, f"--base=--output={out}", check=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not a revision", r.stdout + r.stderr)
        self.assertFalse(os.path.exists(out), "git never saw the value as an option")
        self.assertEqual(self.fm("audit", "prep", self.tid, "--base", "HEAD", "--json").returncode, 0)

    def test_the_diff_runs_with_the_safe_git_options(self):  # T-2318
        import subprocess
        self.prep()
        mark = os.path.join(self.repo, "ran.txt")
        drv = os.path.join(self.home, "drv.sh")
        with open(drv, "w") as f:
            f.write(f"#!/bin/sh\ntouch '{mark}'\n")
        os.chmod(drv, 0o755)
        subprocess.run(["git", "-C", self.repo, "config", "diff.external", drv], check=True)
        self.write("README.md", "# changed\n")
        self.fm("audit", "prep", self.tid, "--base", "HEAD", "--json")
        self.assertFalse(os.path.exists(mark), "the repo's diff driver never ran")

    def test_the_brief_lists_every_function_the_diff_changed(self):  # T-2312
        body = "".join(f"def {n}():\n    return 1\n\n\n" for n in ("f", "g", "h"))
        self.write("m.py", body)
        subprocess.run(["git", "-C", self.repo, "add", "m.py"], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", "m"], check=True)
        self.prep()
        self.write("m.py", body.replace("return 1", "return 2", 2))
        text = self.brief()[1]
        self.assertIn("Functions changed", text)
        self.assertIn("m.py: f, g", text)

    def test_hunk_header_text_is_plain_and_bounded(self):  # T-2312: repo text is data
        from sampo import lanes
        d = ("diff --git a/x.c b/x.c\n--- a/x.c\n+++ b/x.c\n@@ -1 +1 @@ int \x1b]0;pwn\x07evil" + "z" * 200 + "(void)\n"
             "@@ -9 +9 @@\n")
        got = lanes.functions_changed(d)["x.c"]
        self.assertEqual(len(got), 1)
        self.assertNotIn("\x1b", got[0])
        self.assertLessEqual(len(got[0]), 40)

    def test_function_names_credit_changed_defs_skip_quoted_files_and_say_how_many_more(self):  # T-2312 review
        from sampo import lanes
        d = ("diff --git a/m.py b/m.py\n--- a/m.py\n+++ b/m.py\n"
             "@@ -3 +3 @@ def prev():\n-def f(a):\n+def f(a, b):\n"
             "@@ -9,0 +10,2 @@ def prev():\n+async def added():\n+    pass\n"
             'diff --git "a/q\\303\\251.py" "b/q\\303\\251.py"\n@@ -1 +1 @@ def stray():\n'
             "diff --git a/n.py b/n.py\n" + "".join(f"@@ -{i} +{i} @@ def g{i}():\n" for i in range(1, 12)))
        got = lanes.functions_changed(d)
        self.assertEqual(got["m.py"], ["f", "added"], "a changed def is not credited to the function before it")
        self.assertNotIn("stray", str(got))
        self.assertEqual(got["n.py"][:2], ["g1", "g2"])
        self.assertIn("+3 more", lanes.functions_line(got))

    def test_a_hunk_names_git_context_and_its_defs_unless_it_starts_on_a_def_line(self):  # T-2312: never hide an edit
        from sampo import lanes
        both = ("diff --git a/m.py b/m.py\n@@ -4 +4,3 @@ def f():\n-    return 1\n+    return 2\n+\n+def g():\n")
        self.assertEqual(lanes.functions_changed(both)["m.py"], ["f", "g"])
        sig = "diff --git a/m.py b/m.py\n@@ -3 +3 @@ def prev():\n-def f(a):\n+def f(a, b):\n"
        self.assertEqual(lanes.functions_changed(sig)["m.py"], ["f"])

    def test_a_secret_in_a_hunk_header_reaches_neither_the_brief_nor_the_merge_output(self):  # T-2312 review
        from sampo import lanes
        key = "sk-live-9f8e7d6c5b4a39281706"
        self.assertNotIn(key, str(lanes.functions_changed(
            f'diff --git a/a.py b/a.py\n@@ -5 +5 @@ API_KEY = "{key}"\n')))
        self.write(".env", f"TOKEN={key}\nA=1\nB=2\nC=3\nD=4\n")
        self.write("a.py", f'API_KEY = "{key}"\nA=1\nB=2\nC=3\nD=4\n')
        subprocess.run(["git", "-C", self.repo, "add", "-f", ".env", "a.py"], check=True)
        subprocess.run(["git", "-C", self.repo, "commit", "-qm", "k"], check=True)
        self.prep()
        self.write(".env", f"TOKEN={key}\nA=1\nB=2\nC=3\nD=5\n")
        self.write("a.py", f'API_KEY = "{key}"\nA=1\nB=2\nC=3\nD=5\n')
        text = self.brief()[1]
        self.assertNotIn(key, text)

    def test_a_backend_diff_gets_no_design_lens(self):
        self.prep()
        self.write("a.py", "x = 1\n")
        lenses, text = self.brief()
        self.assertEqual(lenses, list(c.DEFAULT_LENSES))
        self.assertNotIn("## design", text)

    def test_a_merged_lanes_prep_diffs_its_own_merge_not_an_empty_one(self):  # T-3972: T-2878's diff was 0 lines
        self.prep()

        def git(*a, cwd=None):
            return subprocess.run(["git", "-C", cwd or self.repo, *a], check=True, capture_output=True, text=True).stdout
        lane = os.path.join(self.tmp, "lane-x")
        git("worktree", "add", "-q", "-b", "foreman/x", lane)
        with open(os.path.join(lane, "lane_work.py"), "w") as f:
            f.write("x = 1\n")
        git("add", "lane_work.py", cwd=lane)
        git("commit", "-qm", "lane work", cwd=lane)
        git("merge", "-q", "--no-ff", "--no-edit", "foreman/x")  # the train's merge
        self.write("later_work.py", "y = 2\n")  # another task's work after it
        git("add", "later_work.py")
        git("commit", "-qm", "later")
        p = c.find_project(self.repo)
        b = c.find_brief(p, self.tid)
        b.meta["lane_branch"] = "foreman/x"
        c.save_brief(p, b)
        self.fm("audit", "prep", self.tid, "--json")
        with open(os.path.join(p.dir, "audits", f"{self.tid}.diff")) as f:
            diff = f.read()
        self.assertIn("lane_work.py", diff, "the lane's own change, not merge-base HEAD..tip (empty once merged)")
        self.assertNotIn("later_work.py", diff, "not the work merged after it")
