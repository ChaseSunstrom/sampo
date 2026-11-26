"""T-5239 (c3 of T-4322, Built means used, done means seen): the census holds the unwired public functions and verbs
(wired.scan_full rows, read by the night census run and never on a hook path), captures at most 10 a run and never a
title twice (an archived one included), saves census.json with this and the previous unwired count, and runs each night
as the census job."""
import contextlib
import io
import json
import os
import shutil
from types import SimpleNamespace
from unittest import mock

from helpers import ForemanTestCase

from sampo import core as c
from sampo import night
from sampo import outcomes
from sampo import wired

UNCALLED = "no caller on the default path"


def row(module, name, kind="function", line=10):
    return {"module": module, "name": name, "kind": kind, "file": f"plugin/lib/sampo/{module}.py", "line": line,
            "callers": []}


class CensusNight(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.p = c.find_project(self.repo)

    def scan(self, rows, skipped=()):
        """Stub the static scan (the one thing that reads the whole plugin): rows and the files it could not read."""
        patcher = mock.patch.object(wired, "scan_full", return_value=(list(rows), list(skipped)))
        self.scan_mock = patcher.start()
        self.addCleanup(patcher.stop)

    def census_run(self, **kw):
        """One `sampo outcomes --census` run in-process (so the scan stub holds), its JSON output parsed."""
        args = SimpleNamespace(census=True, capture=kw.get("capture", False), days=None, silent=False,
                               backfill=False, json=True)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            outcomes.cmd_probes(args, self.p)
        return json.loads(buf.getvalue())

    def census_json(self):
        with open(os.path.join(self.p.dir, "census.json"), encoding="utf-8") as f:
            return json.load(f)

    def test_census_rows_hold_the_unwired_functions_and_verbs(self):
        self.scan([row("outcomes", "lonely_helper"), row("night", "cmd_orphan", kind="cmd", line=40)])
        items = {(i["kind"], i["name"]): i for i in outcomes.census(self.p)}
        self.assertIn(("function", "outcomes.lonely_helper"), items)
        self.assertIn(("verb", "night.cmd_orphan"), items)
        for key in (("function", "outcomes.lonely_helper"), ("verb", "night.cmd_orphan")):
            self.assertEqual(items[key]["flag"], UNCALLED)

    def test_census_saves_census_json_with_this_and_the_previous_unwired_count(self):
        self.scan([row("outcomes", "a"), row("outcomes", "b")])
        outcomes.census(self.p, save=True)
        self.assertEqual(self.census_json()["unwired"], 2)
        self.assertIsNone(self.census_json()["previous"])
        self.scan([row("outcomes", "a"), row("outcomes", "b"), row("outcomes", "c")])
        outcomes.census(self.p, save=True)
        self.assertEqual(self.census_json()["unwired"], 3)
        self.assertEqual(self.census_json()["previous"], 2)

    def test_a_plain_census_query_writes_no_census_json(self):
        self.scan([row("outcomes", "a")])
        outcomes.census(self.p)
        self.census_run()  # `sampo outcomes --census` without --capture
        self.assertFalse(os.path.exists(os.path.join(self.p.dir, "census.json")))

    def test_a_partial_scan_gives_no_unwired_rows_and_no_census_json(self):
        """Any module the scan could not read (not only cli.py or a seed) leaves its callees looking unwired."""
        for skipped in ({"file": "plugin/lib/sampo/cli.py", "reason": "too big", "seed": False},
                        {"file": "plugin/lib/sampo/headroom.py", "reason": "symlink", "seed": False}):
            self.scan([row("outcomes", "a")], skipped=[skipped])
            items = outcomes.census(self.p, save=True)
            self.assertFalse([i for i in items if i.get("unwired")])
            self.assertFalse(os.path.exists(os.path.join(self.p.dir, "census.json")))

    def test_captures_stay_at_ten_a_run_and_never_repeat_a_title_archived_included(self):
        self.scan([row("outcomes", f"unused_{i:02d}") for i in range(12)])
        # only the unwired rows here: the runtime census's own never-used verbs would share the ten-a-run cap
        patcher = mock.patch.object(outcomes, "_census_items", side_effect=lambda p, days: [])  # a fresh list each call
        patcher.start()
        self.addCleanup(patcher.stop)
        first = self.census_run(capture=True)
        self.assertEqual(len(first["captured"]), 10)
        # one of the first ten is archived (done or dropped): its title still counts as on the board
        briefs = c.load_briefs(self.p)
        archived = os.path.join(self.p.dir, "archive", "2026-01")
        os.makedirs(archived)
        shutil.move(briefs[0].path, os.path.join(archived, os.path.basename(briefs[0].path)))
        second = self.census_run(capture=True)
        self.assertEqual(len(second["captured"]), 2)
        self.assertEqual(self.census_run(capture=True)["captured"], [])
        titles = [b.title for b in c.load_briefs(self.p, include_archive=True)]
        self.assertEqual(len(titles), len(set(titles)))
        self.assertEqual(sum(UNCALLED in t for t in titles), 12)

    def night_jobs(self, home):
        """night.jobs for this project as Sampo's home (its repo is FOREMAN_HOME) or as another project."""
        env = {"FOREMAN_HOME": self.repo if home else os.path.join(self.tmp, "elsewhere")}
        with mock.patch.dict(os.environ, env), mock.patch("sampo.moonscale._local", return_value=False), \
                mock.patch("sampo.models.claude_on", return_value=False):
            return {name: (argv, usd) for name, argv, usd in night.jobs(self.p)}

    def test_the_night_runs_the_census_job_as_a_free_outcomes_census_capture_at_home(self):
        self.assertIn("census", night.NAMES)
        self.scan([row("outcomes", "a")])
        self.assertEqual(self.night_jobs(home=True)["census"], (["outcomes", "--census", "--capture"], 0.0))

    def test_the_census_job_is_only_in_sampos_own_project(self):
        """The scan reads Sampo's modules, so another project's night must not run it."""
        self.assertNotIn("census", self.night_jobs(home=False))

    def test_captures_take_turns_across_kinds_so_one_kind_cannot_starve_the_rest(self):
        self.scan([row("outcomes", f"unused_{i:02d}") for i in range(11)])
        skill = {"kind": "skill", "name": "lonely-skill", "exists": True, "used": 0, "effective": None,
                 "flag": "never used"}
        with mock.patch.object(outcomes, "_census_items", side_effect=lambda p, days: [skill]):
            captured = self.census_run(capture=True)["captured"]
        titles = [b.title for b in c.load_briefs(self.p) if b.id in captured]
        self.assertEqual(len(captured), 10)
        self.assertTrue(any("skill lonely-skill" in t for t in titles))

    def test_an_item_already_on_the_board_is_not_captured_again_when_its_flag_changes(self):
        self.scan([])  # no unwired rows: only the runtime item under test
        item = {"kind": "skill", "name": "old-skill", "exists": True, "used": 0, "effective": None}
        with mock.patch.object(outcomes, "_census_items", side_effect=lambda p, days: [dict(item)]):
            self.assertEqual(len(self.census_run(capture=True)["captured"]), 1)
        later = dict(item, used=3, effective=0)  # now used, but never effective: a new flag, the same item
        with mock.patch.object(outcomes, "_census_items", side_effect=lambda p, days: [dict(later)]):
            self.assertEqual(self.census_run(capture=True)["captured"], [])
