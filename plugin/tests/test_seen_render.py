"""T-5240 (T-4322 child c5): a task whose edits touch a page builder in the renderer table, a page script (plugin/web/**/*.js,
site/*.js) or a meta surface_paths glob closes only with a surface run made after its last edit. A module outside the
table is not gated. The close path is cn.safe_gaps, the call cli.py makes. No test here calls a model."""
import json
import os

from helpers import ForemanTestCase

from sampo import concerns as cn
from sampo import core as c


class Render(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)

    def task(self, title="tidy the page"):
        return json.loads(self.fm("capture", title, "--type", "FEATURE", "--tier", "S", "--json").stdout)["id"]

    def brief(self, tid):
        return c.find_brief(self.p, tid)

    def touch(self, tid, rel):
        c.log_event(self.p, "touched", task=tid, data={"file": os.path.join(self.repo, rel)})

    def run_line(self, tid, cmd, ok=True, ts=None):
        b = self.brief(tid)
        if not b.steps():
            b.add_step("do it")
        b.add_evidence(cmd, "exit 0" if ok else "✗ exit 1", step=1, ran=True, **({"ts": ts} if ts else {}))
        c.save_brief(self.p, b)

    def gated(self, tid, then=()):
        return cn.safe_gaps(self.p, self.brief(tid), then=list(then))

    def test_a_page_builder_with_unit_runs_only_is_refused_and_a_curl_after_the_edit_closes_it(self):
        tid = self.task()
        self.touch(tid, "plugin/lib/sampo/board.py")
        self.run_line(tid, "python3 plugin/tests/run.py test_x")
        got = self.gated(tid)
        self.assertEqual(len(got), 1)
        self.assertIn("plugin/lib/sampo/board.py", got[0])
        self.run_line(tid, "curl -sf http://localhost:1/")
        self.assertEqual(self.gated(tid), [])

    def test_every_named_page_builder_is_gated(self):
        for rel in ("plugin/lib/sampo/live.py", "plugin/lib/sampo/page.py", "plugin/lib/sampo/panels.py",
                    "plugin/lib/sampo/stories.py", "site/pages.py", "site/build.py"):
            tid = self.task(title=f"edit {rel}")
            self.touch(tid, rel)
            self.assertEqual(len(self.gated(tid)), 1, rel)

    def test_a_page_script_under_plugin_web_is_gated_by_its_glob(self):
        tid = self.task()
        self.touch(tid, "plugin/web/live/live.js")
        self.assertEqual(cn.user_facing(self.p, self.brief(tid)), ["plugin/web/live/live.js"])
        got = self.gated(tid)
        self.assertEqual(len(got), 1)
        self.assertIn("plugin/web/live/live.js", got[0])
        self.run_line(tid, "sampo smoke")
        self.assertEqual(self.gated(tid), [])

    def test_a_site_script_is_gated_one_level_deep_only(self):
        tid = self.task()
        self.touch(tid, "site/app.js")
        self.assertEqual(cn.user_facing(self.p, self.brief(tid)), ["site/app.js"])
        other = self.task(title="a nested site script")
        self.touch(other, "site/vendor/app.js")
        self.assertEqual(cn.user_facing(self.p, self.brief(other)), [], "site/*.js is one level, not a tree")

    def test_a_module_outside_the_table_is_not_gated(self):
        tid = self.task()
        self.touch(tid, "plugin/lib/sampo/core.py")
        self.touch(tid, "plugin/lib/sampo/cli.py")
        self.assertEqual(cn.user_facing(self.p, self.brief(tid)), [])
        self.assertEqual(self.gated(tid), [])

    def test_a_meta_surface_path_glob_gates_its_files(self):
        tid = self.task()
        meta = c.read_meta(self.p)
        meta["surface_paths"] = ["templates/**/*.j2"]
        c.write_meta(self.p, meta)
        self.touch(tid, "templates/mail/a/b.j2")
        self.touch(tid, "plugin/lib/sampo/core.py")
        self.assertEqual(cn.user_facing(self.p, self.brief(tid)), ["templates/mail/a/b.j2"])
        self.assertEqual(len(self.gated(tid)), 1)
        self.run_line(tid, "fm shot http://localhost:1/")
        self.assertEqual(self.gated(tid), [])

    def test_a_surface_run_before_the_last_page_edit_does_not_count(self):
        tid = self.task()
        self.touch(tid, "plugin/lib/sampo/board.py")
        self.run_line(tid, "curl -sf http://localhost:1/", ts="2000-01-01T00:00:00Z")
        self.assertEqual(len(self.gated(tid)), 1, "the curl is older than the edit")
        self.assertEqual(self.gated(tid, then=[(1, "curl -sf http://localhost:1/")]), [],
                         "the close's own run is new")

    def test_the_glob_matcher_is_exact_about_stars(self):
        self.assertTrue(cn._glob_match("plugin/web/**/*.js", "plugin/web/live/live.js"))
        self.assertTrue(cn._glob_match("plugin/web/**/*.js", "plugin/web/live.js"))
        self.assertFalse(cn._glob_match("site/*.js", "site/a/b.js"))
        self.assertFalse(cn._glob_match("plugin/web/**/*.js", "plugin/webx/a.js"))
        self.assertTrue(cn._glob_match("templates/**/*.j2", "templates/a/b.j2"))
