"""T-4277: the lane load gate counts the load of runnable threads at nice 0, sampled from /proc (a fake tree here), and
the hook reads only a share cached in state for at most 30 s, never /proc itself."""
import os
import unittest
from unittest import mock

from helpers import SampoTestCase

from sampo import core as c


def fake_proc(root, states):
    """states: [(state, nice)] for one thread each: root/<pid>/task/<tid>/stat in the kernel's layout, with a command
    name that holds a space and parentheses (the fields are split after its last ')')."""
    for idx, (state, nice) in enumerate(states):
        pid, tid = 1000 + idx // 10, idx + 1
        d = os.path.join(root, str(pid), "task", str(tid))
        os.makedirs(d, exist_ok=True)
        rest = " ".join([state] + ["1"] * 15 + [str(nice)] + ["0"] * 10)  # nice is field 16 after the state
        with open(os.path.join(d, "stat"), "w") as f:
            f.write(f"{tid} (nightly drill (t) {tid}) {rest}\n")


class RunQueueShare(SampoTestCase):
    def setUp(self):
        super().setUp()
        self.proc = os.path.join(self.tmp, "proc")
        os.makedirs(self.proc)
        self.now, self.sleeps = 1000.0, []
        for p in (mock.patch.dict(os.environ, {"FOREMAN_STATE": os.path.join(self.tmp, "state"), "FOREMAN_HOST": ""}),
                  mock.patch.object(c, "PROC_ROOT", self.proc),
                  mock.patch.object(c, "_pause", self.sleeps.append),
                  mock.patch.object(c, "_wall", lambda: self.now),
                  mock.patch.object(c.os, "getloadavg", lambda: (60.0, 60.0, 60.0)),
                  mock.patch.object(c.os, "cpu_count", lambda: 16),
                  mock.patch.object(c, "_mem_available_mb", lambda: 8192.0)):
            p.start()
            self.addCleanup(p.stop)

    def test_runq_share_is_the_nice_0_share_of_the_runnable_threads_over_three_passes_200_ms_apart(self):
        fake_proc(self.proc, [("R", 19)] * 90 + [("R", 0)] * 10 + [("S", 0)] * 20)  # sleepers don't count
        self.assertAlmostEqual(c.run_queue_share(), 0.1)
        self.assertEqual(self.sleeps, [0.2, 0.2], "three passes, 200 ms apart")

    def test_runq_at_load_60_with_90_of_100_runnable_threads_niced_reads_about_6_and_a_lane_starts(self):
        fake_proc(self.proc, [("R", 19)] * 90 + [("R", 0)] * 10)
        self.assertIsNone(c.host_strain(nice_free=True), "60 x 0.1 = 6, far under the bar of 24")
        self.assertIn("6.0", c.host_strain(bar=0.3, nice_free=True), "a bar of 4.8 per CPU shows the 6.0 it read")

    def test_runq_with_every_runnable_thread_at_nice_0_still_refuses_at_load_60(self):
        fake_proc(self.proc, [("R", 0)] * 100)
        self.assertIn("loaded", c.host_strain(nice_free=True))
        self.assertIn("60.0", c.host_strain(nice_free=True))

    def test_runq_fake_host_runq0_stands_in_for_the_sample(self):
        os.environ["FOREMAN_HOST"] = "load=60,cpus=16,mem_mb=8192,runq0=0.1"
        self.assertIsNone(c.host_strain(nice_free=True))
        os.environ["FOREMAN_HOST"] = "load=60,cpus=16,mem_mb=8192,runq0=1"
        self.assertIn("loaded", c.host_strain(nice_free=True))
        os.environ["FOREMAN_HOST"] = "load=60,cpus=16,mem_mb=8192"  # no share at all: the whole load stands
        self.assertIn("loaded", c.host_strain(nice_free=True))

    def test_runq_falls_back_to_the_cpu_time_share_without_task_folders_or_over_the_cap(self):
        with mock.patch.object(c, "niced_share", lambda: 0.9):
            self.assertIsNone(c.run_queue_share(), "an empty /proc has no task folders")
            self.assertIsNone(c.host_strain(nice_free=True), "the CPU-time share (0.9 niced) takes the load down")
        fake_proc(self.proc, [("R", 19)] * 6)
        with mock.patch.object(c, "RUNQ_CAP", 5), mock.patch.object(c, "niced_share", lambda: None):
            self.assertIsNone(c.run_queue_share(), "over the cap of 5 threads: no sample")
            self.assertIn("loaded", c.host_strain(nice_free=True), "no share at all: the load stands")

    def test_runq_skips_a_stat_it_cannot_parse_and_a_thread_that_exited(self):
        fake_proc(self.proc, [("R", 19)] * 9 + [("R", 0)])
        bad = os.path.join(self.proc, "1000", "task")
        fields = lambda state, nice: " ".join([state] + ["1"] * 15 + [nice])
        for name, text in (("91", "91 no parenthesis R 1 1"), ("92", f"92 (x) {fields('R', 'x')}"),
                           ("93", f"93 (x) {fields('S', '0')}")):
            os.makedirs(os.path.join(bad, name))
            with open(os.path.join(bad, name, "stat"), "w") as f:
                f.write(text)
        os.makedirs(os.path.join(bad, "94"))  # exited between the listing and the read: no stat file
        self.assertAlmostEqual(c.run_queue_share(), 0.1, msg="only the 10 parsed runnable threads count")

    def test_runq_sample_is_cached_for_30_s_and_a_second_sampling_call_reuses_it(self):
        fake_proc(self.proc, [("R", 19)] * 90 + [("R", 0)] * 10)
        calls = []
        real = c._runq_pass
        with mock.patch.object(c, "_runq_pass", lambda p: calls.append(p) or real(p)):
            self.assertAlmostEqual(c.run_queue_share(), 0.1)
            self.now += 29
            self.assertAlmostEqual(c.run_queue_share(), 0.1)
        self.assertEqual(len(calls), 3, "the second call read the cache, not /proc")

    def test_hook_path_reads_a_cached_share_of_at_most_30_s_and_never_samples_proc(self):
        fake_proc(self.proc, [("R", 19)] * 90 + [("R", 0)] * 10)
        self.assertIsNone(c.host_strain(nice_free=True))  # a sampling caller (a lane gate) fills the cache
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "state", "runq_share.json")))

        def no_sampling(p):
            raise AssertionError("the hook sampled /proc")
        with mock.patch.object(c, "_runq_pass", no_sampling), mock.patch.object(c, "niced_share", lambda: 0.0):
            self.assertIsNone(c.host_strain(nice_free=True, sample=False), "the cached 0.1 share reads 6")
            self.now += 31  # stale: the hook falls back to the CPU-time share, here 0 niced, so the load stands
            self.assertIn("loaded", c.host_strain(nice_free=True, sample=False))

    def test_hook_path_with_no_cache_falls_back_to_the_cpu_time_share_and_never_samples_proc(self):
        fake_proc(self.proc, [("R", 19)] * 90 + [("R", 0)] * 10)

        def no_sampling(p):
            raise AssertionError("the hook sampled /proc")
        with mock.patch.object(c, "_runq_pass", no_sampling), mock.patch.object(c, "niced_share", lambda: 0.9):
            self.assertIsNone(c.host_strain(nice_free=True, sample=False))
        with mock.patch.object(c, "_runq_pass", no_sampling), mock.patch.object(c, "niced_share", lambda: None):
            self.assertIn("loaded", c.host_strain(nice_free=True, sample=False), "no share at all: the load stands")

    def test_hook_path_with_a_stale_cache_starts_one_detached_sampler_per_30_s(self):
        """Review: only the lane gates wrote the cache, and a hook that reads 'loaded' offers no lane, so no gate ran
        and the hook stayed on the CPU-time share for good."""
        started = []

        def no_sampling(p):
            raise AssertionError("the hook sampled /proc")
        with mock.patch.object(c, "_runq_pass", no_sampling), mock.patch.object(c, "niced_share", lambda: 0.0), \
                mock.patch.object(c.subprocess, "Popen", lambda argv, **kw: started.append((argv, kw))), \
                mock.patch.dict(os.environ, {"FOREMAN_NO_BACKGROUND": ""}):
            self.assertIn("loaded", c.host_strain(nice_free=True, sample=False))
            self.assertEqual(len(started), 1, "a stale cache starts the sampler")
            self.assertTrue(started[0][1].get("start_new_session"), "detached: never in the hook's time")
            self.now += 5
            c.host_strain(nice_free=True, sample=False)
            self.assertEqual(len(started), 1, "one per 30 s, however many hooks run")
            self.now += 31
            c.host_strain(nice_free=True, sample=False)
            self.assertEqual(len(started), 2)
        with mock.patch.object(c.subprocess, "Popen", lambda argv, **kw: started.append((argv, kw))), \
                mock.patch.object(c, "niced_share", lambda: 0.0), mock.patch.dict(os.environ, {"FOREMAN_NO_BACKGROUND": "1"}):
            self.now += 60
            c.host_strain(nice_free=True, sample=False)
        self.assertEqual(len(started), 2, "none where background work is off")

    def test_hook_path_sampler_fills_the_cache_the_next_hook_reads(self):
        fake_proc(self.proc, [("R", 19)] * 90 + [("R", 0)] * 10)
        c._runq_refresh_main()  # what the detached sampler runs
        with mock.patch.object(c, "_runq_pass", lambda p: self.fail("the hook sampled /proc")):
            self.assertIsNone(c.host_strain(nice_free=True, sample=False), "the sampled 0.1 share reads 6")

    def test_runq_cached_failure_is_not_sampled_again_for_30_s(self):
        """Review: a pass over RUNQ_CAP threads gives no share; uncached, every lane brief of a batch redid it."""
        fake_proc(self.proc, [("R", 0)] * 30)
        calls = []
        real = c._runq_pass
        with mock.patch.object(c, "RUNQ_CAP", 10), mock.patch.object(c, "_runq_pass", lambda p: calls.append(p) or real(p)):
            self.assertIsNone(c.run_queue_share())
            first = len(calls)
            self.assertIsNone(c.run_queue_share())
            self.assertEqual(len(calls), first, "the failure is cached")
            self.now += 31
            self.assertIsNone(c.run_queue_share())
            self.assertGreater(len(calls), first)

    def test_hook_lanes_free_never_samples_proc(self):
        """Review: nothing held hooks.lanes_free to sample=False; without it every hook read /proc for half a second."""
        from sampo import hooks

        def no_sampling(p):
            raise AssertionError("the hook sampled /proc")
        with mock.patch.object(c, "_runq_pass", no_sampling), mock.patch.object(c, "niced_share", lambda: 0.95), \
                mock.patch.dict(os.environ, {"FOREMAN_NO_BACKGROUND": "1"}):
            self.assertTrue(hooks.lanes_free([]))


if __name__ == "__main__":
    unittest.main()
