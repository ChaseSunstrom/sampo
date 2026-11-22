"""Kalevala depth (T-5018, a slice of T-3312): start --depth N stores the depth in the manifest, refuses N below 1, depth 1
opens one genesis and depth 2 is today's start; status prints a depth line. No level logic here."""
import os

from helpers import CLEAN_READER, ForemanTestCase
from sampo import core as c
from sampo import kalevala


def setUpModule():
    os.environ["PATH"] = CLEAN_READER + os.pathsep + os.environ["PATH"]


def tearDownModule():
    os.environ["PATH"] = os.environ["PATH"].replace(CLEAN_READER + os.pathsep, "", 1)


class KalevalaDepth(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        path = os.path.join(self.tmp, "gen0.md")
        with open(path, "w") as f:
            f.write("placeholder root charter\n")
        self.fm("genesis", "init", "--file", path)

    def project(self):
        return c.find_project(self.repo, create=False)

    def test_default_depth_is_two_and_is_recorded_in_the_manifest(self):
        res = self.fm_json("kalevala", "start")
        self.assertEqual(kalevala.load(self.project())["depth"], 2)
        self.assertEqual(len(res["opened"]), 3)  # the fixed three: today's behaviour
        self.assertEqual(self.fm_json("kalevala", "status")["depth"], 2)

    def test_depth_flag_is_recorded_and_status_prints_a_depth_line(self):
        self.fm("kalevala", "start", "--depth", "3")
        self.assertEqual(kalevala.load(self.project())["depth"], 3)
        out = self.fm("kalevala", "status").stdout
        self.assertIn("depth 3", out.splitlines())

    def test_depth_one_opens_one_genesis(self):
        res = self.fm_json("kalevala", "start", "--depth", "1")
        self.assertEqual(len(res["opened"]), 1)
        self.assertEqual(len(res["towers"]), 1)
        self.assertEqual(kalevala.load(self.project())["depth"], 1)

    def test_depth_below_one_is_refused_and_writes_nothing(self):
        for bad in ("0", "-1"):
            r = self.fm("kalevala", "start", "--depth", bad, check=False)
            self.assertNotEqual(r.returncode, 0, bad)
        self.assertEqual(kalevala.load(self.project()), {"towers": {}})
        with self.assertRaises(ValueError):
            kalevala.start(self.project(), depth=0)

    def test_a_status_before_any_start_has_no_depth_line(self):
        out = self.fm("kalevala", "status").stdout
        self.assertNotIn("depth", out)


class KalevalaDepthRerun(KalevalaDepth):
    """The rerun cases of depth.

    reruns: the base's tests run again beside these, under the same generated tower fixture."""

    def test_a_bare_rerun_keeps_the_recorded_depth(self):
        self.fm("kalevala", "start", "--depth", "3")
        self.fm("kalevala", "start")
        self.assertEqual(kalevala.load(self.project())["depth"], 3)

    def test_depth_one_opens_the_first_aspect(self):
        res = self.fm_json("kalevala", "start", "--depth", "1")
        self.assertEqual(res["towers"][0]["aspect"], kalevala.aspects(self.project())[0][0])

    def test_lowering_depth_over_open_towers_is_refused_and_changes_nothing(self):
        self.fm("kalevala", "start")  # depth 2, three towers
        r = self.fm("kalevala", "start", "--depth", "1", check=False)
        self.assertNotEqual(r.returncode, 0)
        m = kalevala.load(self.project())
        self.assertEqual(m["depth"], 2)
        self.assertEqual(len(m["towers"]), 3)
        with self.assertRaises(ValueError):
            kalevala.start(self.project(), depth=1)

    def test_depth_one_rerun_over_its_one_tower_is_fine(self):
        self.fm("kalevala", "start", "--depth", "1")
        self.fm("kalevala", "start", "--depth", "1")
        self.assertEqual(len(kalevala.load(self.project())["towers"]), 1)
