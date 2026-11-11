"""T-0854: a newcomer's first fm commands make sense (a clean-HOME walkthrough, 2026-10-15)."""
import unittest

from helpers import ForemanTestCase


class FirstRun(ForemanTestCase):
    def test_root_help_is_short_and_points_to_all(self):  # T-0845
        out = self.fm("--help").stdout
        self.assertLess(len(out.splitlines()), 30, out)
        self.assertNotIn("{help,init", out)
        for verb in ("sampo init", "sampo capture", "sampo task new", "sampo next", "sampo status", "sampo check",
                     "sampo task finish"):
            self.assertIn(verb, out)
        self.assertIn("sampo help --all", out)
        self.assertIn("Every task", self.fm("help", "--all").stdout)

    def test_task_new_help_lists_values_and_an_example(self):  # T-0846
        out = self.fm("task", "new", "--help").stdout
        for v in ("RESEARCH", "CLEAN", "PERFORMANCE", "SECURITY", "FIX", "FEATURE"):
            self.assertIn(v, out)
        self.assertRegex(out, r"\{S,M,L,XL,HUGE,GIANT\}")
        last = out.strip().splitlines()[-1]
        self.assertIn('sampo task new "Add login timeout" --type FIX --tier S', last)
        self.assertIn("--focus", last)
        self.fm("task", "new", "an alias", "--type", "bug", "--tier", "S")  # aliases and lowercase still work
        self.fm("task", "new", "lowercase", "--type", "fix", "--tier", "S")

    def test_init_says_where_and_next_and_empty_status_says_how_to_start(self):  # T-0847
        p = self.fm("init")
        said = p.stdout + p.stderr
        self.assertIn(self.repo, said)
        self.assertIn(self.home, said)  # where state lives
        self.assertIn("sampo capture", said)
        status = self.fm("status").stdout
        self.assertEqual(len(status.strip().splitlines()), 1, status)
        self.assertIn("sampo capture", status)
        self.fm("capture", "add a hello command")
        self.assertGreater(len(self.fm("status").stdout.strip().splitlines()), 1)

    def test_task_new_reuses_a_same_titled_capture(self):  # T-0848
        cap = self.fm_json("capture", "add a hello command")["id"]
        out = self.fm("task", "new", "Add a  hello command", "--type", "FEATURE", "--tier", "S").stdout
        self.assertIn(cap, out.split()[0])
        self.assertIn(f"capture {cap}", out)
        st = self.fm_json("state")
        self.assertEqual(st["inbox"], [])
        self.assertEqual([q["id"] for q in st["queue"]], [cap])

    def test_done_refusal_leads_with_finish(self):  # T-0849
        tid = self.fm_json("task", "new", "a fix", "--type", "FIX", "--tier", "S", "--step", "do it")["id"]
        p = self.fm("task", "done", tid, check=False)
        self.assertEqual(p.returncode, 2, p.stderr)
        first = p.stderr.strip().splitlines()[0]
        self.assertIn(f"sampo task finish {tid} --run", first)
        self.assertIn("--audit", first)


if __name__ == "__main__":
    unittest.main()
