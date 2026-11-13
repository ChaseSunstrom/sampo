"""T-4531: the CPUs Sampo counts are the CPUs this process may use. A cgroup v2 cap (cpu.max, quota over period, rounded up,
on this process's folder or any ancestor up to the cgroup root) lowers os.cpu_count(); the scheduler affinity can lower
it too. The cgroup files are read through a folder argument, so a temp folder stands in for /sys/fs/cgroup."""
import os
from unittest import mock

from helpers import ForemanTestCase

import run
from sampo import core as c


class CpuCapCount(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.cg = os.path.join(self.tmp, "cgroup")  # stands in for /sys/fs/cgroup
        self.scope = os.path.join(self.cg, "user.slice", "session.scope")
        os.makedirs(self.scope)
        self.proc = os.path.join(self.tmp, "proc-self-cgroup")  # stands in for /proc/self/cgroup
        self.put_proc("0::/user.slice/session.scope\n")

    def put_proc(self, text):
        with open(self.proc, "w", encoding="utf-8") as f:
            f.write(text)

    def put(self, folder, text):
        with open(os.path.join(folder, "cpu.max"), "w", encoding="utf-8") as f:
            f.write(text)

    def count(self, cpus=16, affinity=16):
        """c.usable_cpus over the temp cgroup tree on a host of `cpus` CPUs with `affinity` of them allowed."""
        with mock.patch.object(os, "cpu_count", lambda: cpus), \
                mock.patch.object(os, "sched_getaffinity", lambda pid: set(range(affinity))):
            return c.usable_cpus(cgroup_root=self.cg, proc_cgroup=self.proc)

    def capped(self):
        """Patch the no-argument count every reader calls so it reads the temp cgroup tree instead of the real one."""
        real = c.usable_cpus
        return mock.patch.object(c, "usable_cpus", lambda: real(cgroup_root=self.cg, proc_cgroup=self.proc))

    def test_a_quota_caps_the_count(self):
        self.put(self.scope, "800000 100000\n")
        self.assertEqual(self.count(cpus=16), 8)

    def test_an_unlimited_or_unreadable_cpu_max_leaves_the_cpu_count(self):
        self.assertEqual(self.count(cpus=16), 16, "no cpu.max file")
        self.put(self.scope, "max 100000\n")
        self.assertEqual(self.count(cpus=16), 16, "max is no limit")
        self.put(self.scope, "garbage\n")
        self.assertEqual(self.count(cpus=16), 16, "one token: unreadable")
        self.put(self.scope, "800000 0\n")
        self.assertEqual(self.count(cpus=16), 16, "a zero period is unreadable")
        self.put(self.scope, "800000 xyz\n")
        self.assertEqual(self.count(cpus=16), 16, "a non-number period is unreadable")
        self.put(self.scope, "-800000 100000\n")
        self.assertEqual(self.count(cpus=16), 16, "a negative quota is unreadable, not one CPU")

    def test_no_cgroup_v2_line_or_no_proc_file_leaves_the_cpu_count(self):
        self.put(self.scope, "800000 100000\n")
        self.put_proc("1:cpu,cpuacct:/user.slice\n")  # a v1 line only
        self.assertEqual(self.count(cpus=16), 16)
        os.remove(self.proc)
        self.assertEqual(self.count(cpus=16), 16)

    def test_the_tightest_folder_up_the_tree_counts(self):
        parent = os.path.join(self.cg, "user.slice")
        self.put(parent, "400000 100000\n")
        self.put(self.scope, "max 100000\n")
        self.assertEqual(self.count(cpus=16), 4, "an ancestor's cap, the leaf unlimited")
        self.put(self.scope, "800000 100000\n")
        self.assertEqual(self.count(cpus=16), 4, "the tighter of leaf and ancestor")
        self.put(parent, "1600000 100000\n")
        self.assertEqual(self.count(cpus=16), 8, "a loose ancestor leaves the leaf's cap")

    def test_quota_rounds_up_and_is_at_least_one(self):
        self.put(self.scope, "250000 100000\n")
        self.assertEqual(self.count(cpus=16), 3, "2.5 CPUs rounds up to 3")
        self.put(self.scope, "1000 100000\n")
        self.assertEqual(self.count(cpus=16), 1, "a tiny quota still leaves one CPU")

    def test_a_quota_above_the_host_does_not_raise_the_count(self):
        self.put(self.scope, "3200000 100000\n")
        self.assertEqual(self.count(cpus=16), 16)

    def test_the_scheduler_affinity_counts_when_it_is_smaller(self):
        self.assertEqual(self.count(cpus=16, affinity=4), 4)
        self.put(self.scope, "800000 100000\n")
        self.assertEqual(self.count(cpus=16, affinity=6), 6, "the smaller of affinity and quota")

    def test_at_least_one_cpu(self):
        self.put(self.scope, "1 100000\n")
        self.assertEqual(self.count(cpus=1), 1)
        with mock.patch.object(os, "cpu_count", lambda: None):
            self.assertGreaterEqual(c.usable_cpus(cgroup_root=self.cg, proc_cgroup=self.proc), 1)

    def test_load_factor_for_the_same_load_is_twice_as_large_under_the_cap(self):
        with mock.patch.object(os, "cpu_count", lambda: 16):
            self.put(self.scope, "max 100000\n")
            with self.capped():
                uncapped = c.load_factor(host="load=24")
            self.put(self.scope, "800000 100000\n")
            with self.capped():
                capped = c.load_factor(host="load=24")
        self.assertEqual((uncapped, capped), (1.5, 3.0))

    def test_host_load_is_the_host_cpus_under_a_cap(self):
        # T-4531: the host-wide load is compared with the host's own CPUs; a session's cap doesn't shrink that count
        self.put(self.scope, "800000 100000\n")
        with mock.patch.object(os, "cpu_count", lambda: 16), self.capped(), \
                mock.patch.dict(os.environ, {"FOREMAN_HOST": "load=2,mem_mb=8192"}):
            self.assertEqual(c.host_load(), (2.0, 16))
            self.assertIsNone(c.host_strain(), "16 CPUs, load 2: far under the bar, the gate ignores the cap")

    def test_a_foreman_host_cpus_stand_in_still_wins(self):
        self.put(self.scope, "800000 100000\n")
        with mock.patch.object(os, "cpu_count", lambda: 16), self.capped(), \
                mock.patch.dict(os.environ, {"FOREMAN_HOST": "load=1,cpus=3"}):
            self.assertEqual(c.host_load(), (1.0, 3))
            self.assertEqual(c.load_factor(host="load=24,cpus=3"), 8.0)

    def test_the_runner_counts_the_same_as_core(self):
        self.put(self.scope, "800000 100000\n")
        with mock.patch.object(os, "cpu_count", lambda: 16), \
                mock.patch.object(os, "sched_getaffinity", lambda pid: set(range(16))), \
                mock.patch.object(run, "CGROUP_ROOT", self.cg):
            self.assertEqual(run.usable_cpus(self.scope), c.usable_cpus(cgroup_root=self.cg, proc_cgroup=self.proc))
            self.assertEqual(run.usable_cpus(self.scope), 8)
            self.assertEqual(run.usable_cpus(None), 16, "no cgroup folder: the cpu count and affinity only")

    def test_the_default_pool_of_this_run_is_the_cpus_it_may_use(self):
        # -j: the slot pool (slot_count) counts the cgroup cap but not the scheduler affinity; this run's workers take the
        # smaller of the two, so a run pinned to 4 CPUs by taskset starts 4 workers, not the machine's 16
        with mock.patch.object(os, "cpu_count", lambda: 16), \
                mock.patch.object(os, "sched_getaffinity", lambda pid: set(range(4))), \
                mock.patch.object(run, "CGROUP_ROOT", self.cg), mock.patch.object(run, "_cgroup_dir", lambda: None), \
                mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SAMPO_SLOTS", None)
            self.assertEqual(run.parse([]).j, 4)
