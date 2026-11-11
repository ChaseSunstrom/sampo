"""T-2643: fm doctor probes each pinned model name once a day with a one-line claude -p call."""
import os
import stat
import unittest

from helpers import ForemanTestCase

from sampo import doctor as d


class ModelProbe(ForemanTestCase):
    def stub(self, dead=()):
        """A fake claude on PATH: refuses the models in `dead`, logs every call."""
        bin_ = os.path.join(self.tmp, "bin")
        os.makedirs(bin_, exist_ok=True)
        log = os.path.join(self.tmp, "calls")
        path = os.path.join(bin_, "claude")
        with open(path, "w") as f:
            f.write("#!/bin/sh\necho \"$@\" >> %s\ncase \"$*\" in\n" % log)
            for m in dead:
                f.write("  *\"--model %s \"*) echo 'model not found' >&2; exit 1;;\n" % m)
            f.write("esac\ncat >/dev/null\necho '{\"type\": \"result\", \"is_error\": false, \"total_cost_usd\": 0, \"result\": \"OK\"}'\n")
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        self._path = os.environ["PATH"]
        os.environ["PATH"] = bin_ + os.pathsep + self._path
        self.addCleanup(lambda: os.environ.__setitem__("PATH", self._path))
        return log

    def calls(self, log):
        return len(open(log).read().splitlines()) if os.path.exists(log) else 0

    def test_dead_model_warns_names_module_and_caches(self):
        log = self.stub(dead=["opus"])
        r = d.check_model_pins()
        self.assertEqual(r.status, "WARN")
        self.assertIn("opus", r.detail)
        self.assertIn("rune", r.detail)
        self.assertNotIn("haiku", r.detail)
        n = self.calls(log)
        self.assertGreater(n, 0)
        r2 = d.check_model_pins()  # same day: from the cache, no new calls
        self.assertEqual(r2.status, "WARN")
        self.assertEqual(self.calls(log), n)

    def test_quiet_when_all_answer(self):
        self.stub()
        self.assertEqual(d.check_model_pins().status, "PASS")

    def test_paused_skips_the_probe_and_is_not_cached(self):
        """T-4196: the probe starts a claude child, which a pause refuses: the doctor died of it, every gate run with it."""
        from sampo import core as c
        log = self.stub(dead=["opus"])
        c.set_panic(True)  # in this test's own home; no cleanup: one registered here runs after tearDown put the real
        r = d.check_model_pins()  # FOREMAN_HOME back, and would lift the machine's real pause
        self.assertEqual(r.status, "PASS")
        self.assertIn("paused", r.detail)
        self.assertEqual(self.calls(log), 0)
        c.set_panic(False)
        self.assertEqual(d.check_model_pins().status, "WARN")  # the skip was not cached for the day

    def test_quiet_without_claude(self):
        old = os.environ["PATH"]
        os.environ["PATH"] = os.path.join(self.tmp, "empty")
        self.addCleanup(lambda: os.environ.__setitem__("PATH", old))
        self.assertEqual(d.check_model_pins().status, "PASS")


if __name__ == "__main__":
    unittest.main()
