"""T-4671: every reader of the statusline's usage figure goes through budget's one age rule (T-4669's READ_STALE_S): a
reading older than the window is unknown (said with its age), never 0% and never the stale figure; the statusline's
countdown takes a reset in seconds or milliseconds as the reader does; the Rust mirror agrees with Python on the same
stale and fresh readings."""
import importlib.machinery
import importlib.util
import json
import os
import time

from helpers import HOOK, SampoTestCase
from test_fast_equiv import Base

from sampo import budget as bud
from sampo import core as c
from sampo import cost, doctor, lanes, watch

STATUSLINE = os.path.join(os.path.dirname(HOOK), "statusline")
STALE, FRESH = bud.READ_STALE_S + 600, 3600  # seconds since the snapshot was read


class Readers(SampoTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")

    def read(self, ago, ms=False, **windows):
        """A statusline snapshot read `ago` seconds back (its file's mtime): window=(used %, seconds until its reset)."""
        folder = os.path.join(c.state_dir(), "sessions")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, "s.json")
        mult = 1000 if ms else 1
        rl = {k: {"used_percentage": u, "resets_at": int((time.time() + s) * mult)} for k, (u, s) in windows.items()}
        with open(path, "w") as f:
            json.dump({"project": None, "rate_limits": rl}, f)
        os.utime(path, (time.time() - ago, time.time() - ago))

    # ---- forecast() and the readers of it: cost --forecast, the burn finding, doctor, lanes, --until reset

    def test_forecast_of_a_stale_reading_is_unknown(self):
        self.read(FRESH, seven_day=(90, 6 * 86400), five_hour=(60, 4 * 3600))
        self.assertEqual(sorted(bud.forecast()), ["five_hour", "seven_day"])
        self.read(STALE, seven_day=(90, 6 * 86400), five_hour=(60, 4 * 3600))
        self.assertEqual(bud.forecast(), {}, "a reading older than READ_STALE_S is not the usage now")

    def test_forecast_reads_a_millisecond_reset_as_the_reader_does(self):
        self.read(FRESH, seven_day=(90, 6 * 86400))
        seconds = bud.forecast()["seven_day"]
        self.read(FRESH, ms=True, seven_day=(90, 6 * 86400))
        got = bud.forecast()["seven_day"]
        self.assertAlmostEqual(got["resets_at"], seconds["resets_at"], delta=5)
        self.assertEqual(got["used"], 90)

    def test_unknown_says_how_old_the_reading_is(self):
        self.assertIsNone(bud.unknown_why(), "no snapshot at all: nothing to say")
        self.read(FRESH, seven_day=(90, 6 * 86400))
        self.assertIsNone(bud.unknown_why(), "a reading that stands is not unknown")
        self.read(STALE, seven_day=(90, 6 * 86400))
        self.assertRegex(bud.unknown_why(), r"last read 6 h ago, over the 6 h a reading stands")

    def test_cost_forecast_names_an_unknown_reading_with_its_age(self):
        self.read(STALE, seven_day=(90, 6 * 86400))
        out = self.fm("cost", "--forecast").stdout
        self.assertNotIn("90%", out)
        self.assertIn("unknown", out)
        self.assertIn("last read 6 h ago", out)
        self.read(FRESH, seven_day=(90, 6 * 86400))
        self.assertIn("weekly: 90% used", self.fm("cost", "--forecast").stdout)

    def test_the_burn_finding_needs_a_reading_that_stands(self):
        self.read(FRESH, seven_day=(90, 6 * 86400))
        self.assertIn("weekly usage is on pace to run out", "\n".join(a for a, _ in cost.burn_findings(self.tmp)))
        self.read(STALE, seven_day=(90, 6 * 86400))
        self.assertNotIn("weekly usage is on pace", "\n".join(a for a, _ in cost.burn_findings(self.tmp)))

    def test_doctor_does_not_warn_of_a_burn_a_stale_reading_shows(self):
        os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(self.tmp, "cc")  # no transcripts: only the weekly line can find
        self.addCleanup(os.environ.pop, "CLAUDE_CONFIG_DIR", None)
        self.read(FRESH, seven_day=(90, 6 * 86400))
        self.assertEqual(doctor.check_usage_burn().status, "WARN")
        self.read(STALE, seven_day=(90, 6 * 86400))
        self.assertEqual(doctor.check_usage_burn().status, "PASS")

    def test_the_lane_wall_needs_a_reading_that_stands(self):
        self.read(FRESH, five_hour=(100, 600))
        self.assertIsNotNone(lanes._usage_wall())
        self.read(STALE, five_hour=(100, 600))
        self.assertIsNone(lanes._usage_wall(), "a full window read 6 h ago is not a wall now")

    def test_until_reset_refuses_a_stale_reading_naming_its_age(self):
        self.fm("task", "new", "Waits", "--type", "FEATURE", "--tier", "S", "--ac", "ok :: true", "--step", "do it")
        self.read(STALE, seven_day=(90, 6 * 86400))
        p = self.fm("task", "defer", "T-0001", "waiting", "--until", "reset", check=False)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("last read 6 h ago", p.stderr + p.stdout)

    # ---- the weekly line of `sampo budget`, and watch's 5-hour figure

    def test_budget_says_unknown_with_the_age_not_no_reading(self):
        self.read(STALE, seven_day=(90, 6 * 86400))
        out = self.fm("budget").stdout
        self.assertIn("weekly: unknown", out)
        self.assertIn("last read 6 h ago", out)
        self.read(FRESH, seven_day=(90, 6 * 86400))
        self.assertIn("weekly: 90% used", self.fm("budget").stdout)

    def test_watch_shows_a_stale_five_hour_figure_as_unknown(self):
        p = c.find_project(self.repo)
        self.read(FRESH, five_hour=(55, 3600))
        self.assertIn("5h 55%", "\n".join(watch.render(watch.gather(p))))
        self.read(STALE, five_hour=(55, 3600))
        text = "\n".join(watch.render(watch.gather(p)))
        self.assertNotIn("55%", text)
        self.assertIn("5h unknown (read 6 h ago)", text)

    # ---- the statusline's countdown

    def test_until_takes_seconds_or_milliseconds(self):
        loader = importlib.machinery.SourceFileLoader("statusline_under_test", STATUSLINE)
        mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(mod)
        at = time.time() + 2 * 3600 + 5 * 60 + 30
        self.assertEqual(mod.until(at), "↻2h05m")
        self.assertEqual(mod.until(int(at * 1000)), "↻2h05m", "milliseconds are the same instant")
        self.assertEqual(mod.until(str(int(at * 1000))), "↻2h05m")
        self.assertEqual(mod.until(None), "")
        self.assertEqual(mod.until(float("inf")), "")


class RustMirror(Base):
    """sampo-fast's usage reader against Python's on the same stale and fresh readings: the hook's answer (a subagent
    waits or runs) is the observable one, and the two must agree byte for byte (Base.pair)."""

    def read(self, ago, **limits):
        self.snapshot(**limits)
        path = os.path.join(self.home, "state", "sessions", "snap.json")
        os.utime(path, (time.time() - ago, time.time() - ago))

    def test_stale_and_fresh_readings_decide_alike(self):
        far = 9e9
        agent = {"payload": {"tool_name": "Agent", "tool_input": {"prompt": "look", "description": "x"}}}
        full5 = {"five_hour": {"used_percentage": 95, "resets_at": far}}
        over7 = {"seven_day": {"used_percentage": 92, "resets_at": far}}
        for name, ago, limits, native in [
            ("5-hour full, read just now: Python waits", 60, full5, False),
            ("5-hour full, read past the window: unknown, runs", STALE, full5, True),
            ("week over the bar, read just now: waits", 60, over7, False),
            ("week over the bar, read past the window: unknown, runs", STALE, over7, True),
            ("just inside the window still counts", bud.READ_STALE_S - 120, full5, False),
            ("no reset time, fresh: counts", 60, {"five_hour": {"used_percentage": 95}}, False),
            ("no reset time, stale: unknown", STALE, {"five_hour": {"used_percentage": 95}}, True),
            ("millisecond reset, fresh: counts", 60,
             {"five_hour": {"used_percentage": 95, "resets_at": far * 1000}}, False),
            ("a passed reset reads 0, stale or not", STALE, {"five_hour": {"used_percentage": 95, "resets_at": 1}}, True),
        ]:
            with self.subTest(name):
                self.read(ago, **limits)
                self.assertEqual(self.pair(**agent), native, f"{name}: sampo-fast should {'decide' if native else 'defer'}")
