"""T-4198: a test's addCleanup runs while the environment is still the test's own.

unittest runs tearDown first and the addCleanup functions after it, last added first. The old tearDown put the real
FOREMAN_HOME back, so an in-process cleanup (c.set_panic(False), any state write) landed in the machine's real state:
one such test lifted a real pause twice. The restore is now the first cleanup registered, so it runs last."""
import io
import os
import unittest

from helpers import BuiltOnce, SampoTestCase

KEYS = ("FOREMAN_HOME", "FOREMAN_NO_BACKGROUND", "XDG_STATE_HOME", "FOREMAN_GUARD_CASES")


def run_inner(case):
    """Run one test of `case` in its own runner, so its cleanups run before this test's own end."""
    result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(unittest.defaultTestLoader.loadTestsFromTestCase(case))
    return result


class CleanupSeesTheTestsHome(SampoTestCase):
    def test_a_later_addcleanup_still_sees_the_tests_own_home(self):
        seen = {}

        class Inner(SampoTestCase):
            def test_it(self):
                seen["home"] = self.home

                def late():
                    seen["env"] = {k: os.environ.get(k) for k in KEYS}
                    seen["after_late"] = True
                self.addCleanup(late)

        result = run_inner(Inner)
        self.assertTrue(result.wasSuccessful(), result.errors + result.failures)
        self.assertTrue(seen.get("after_late"))
        self.assertEqual(seen["env"]["FOREMAN_HOME"], seen["home"])
        self.assertEqual(seen["env"]["FOREMAN_NO_BACKGROUND"], "1")
        self.assertEqual(seen["env"]["XDG_STATE_HOME"], os.path.join(os.path.dirname(seen["home"]), "xdg"))

    def test_the_environment_is_put_back_once_every_cleanup_has_run(self):
        outer = {k: os.environ.get(k) for k in KEYS}

        class Inner(SampoTestCase):
            def test_it(self):
                pass

        self.assertTrue(run_inner(Inner).wasSuccessful())
        self.assertEqual({k: os.environ.get(k) for k in KEYS}, outer)

    def test_the_restore_runs_after_a_cleanup_that_raises(self):
        outer = {k: os.environ.get(k) for k in KEYS}

        class Inner(SampoTestCase):
            def test_it(self):
                def boom():
                    raise RuntimeError("cleanup failed")
                self.addCleanup(boom)

        self.assertFalse(run_inner(Inner).wasSuccessful())
        self.assertEqual({k: os.environ.get(k) for k in KEYS}, outer)


class BuiltOnceCleanupSeesTheTestsHome(BuiltOnce, SampoTestCase):
    """The same rule for the BuiltOnce setUp, which points FOREMAN_HOME at the class's fixed folder."""

    def build(self):
        return "fixture"

    def test_a_later_addcleanup_still_sees_the_built_home(self):
        seen = {}

        class Inner(BuiltOnce, SampoTestCase):
            def build(self):
                return "x"

            def test_it(self):
                seen["home"] = self.home
                self.addCleanup(lambda: seen.update(env=os.environ.get("FOREMAN_HOME")))

        self.assertTrue(run_inner(Inner).wasSuccessful())
        self.assertEqual(seen["env"], seen["home"])


class ResetupAndTmp(SampoTestCase):
    def test_setup_teardown_setup_then_cleanups_puts_back_the_original_env(self):
        outer = {k: os.environ.get(k) for k in KEYS + ("FOREMAN_DISK_FREE",)}

        class Inner(SampoTestCase):
            def test_it(self):
                pass

        case = Inner("test_it")
        case.setUp()
        case.tearDown()
        case.setUp()
        case.doCleanups()
        self.assertEqual({k: os.environ.get(k) for k in KEYS + ("FOREMAN_DISK_FREE",)}, outer)

    def test_a_cleanup_that_writes_state_leaves_no_folder_behind(self):
        seen = {}

        class Inner(SampoTestCase):
            def test_it(self):
                seen["tmp"] = self.tmp

                def late():
                    os.makedirs(os.path.join(os.environ["FOREMAN_HOME"], "state"), exist_ok=True)
                self.addCleanup(late)

        self.assertTrue(run_inner(Inner).wasSuccessful())
        self.assertFalse(os.path.exists(seen["tmp"]))


if __name__ == "__main__":
    unittest.main()
