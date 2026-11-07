"""T-1943: fm ship lands the unpushed commits on the remote's main from a scratch worktree, behind the publish grant."""
import json
import os
import shlex
import subprocess

from helpers import ForemanTestCase

from sampo import core as c


def sh(root, *a):
    return subprocess.run(["git", "-C", root, *a], capture_output=True, text=True, check=True).stdout


class Ship(ForemanTestCase):
    def setUp(self):
        super().setUp()
        self.fm("init")
        self.origin = os.path.join(self.tmp, "origin.git")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", self.origin], check=True)
        sh(self.repo, "remote", "add", "origin", self.origin)
        sh(self.repo, "push", "-q", "origin", "HEAD:main")
        sh(self.repo, "checkout", "-q", "-b", "work")
        for name in ("a", "b"):
            with open(os.path.join(self.repo, f"{name}.txt"), "w") as f:
                f.write(name + "\n")
            sh(self.repo, "add", f"{name}.txt")
            sh(self.repo, "commit", "-qm", f"add {name}")
        self.tid = json.loads(self.fm("task", "new", "land", "--type", "FEATURE", "--tier", "S", "--json").stdout)["id"]
        self.fm("task", "step", self.tid, "add", "land it")
        self.fm("task", "ac", self.tid, "add", "landed", "--verify", "true")
        self.fm("focus", self.tid)

    def grant(self):
        p = c.find_project(self.repo)
        b = c.find_brief(p, self.tid)
        b.meta["allow"] = ["publish"]
        c.save_brief(p, b)

    def granted(self):
        return "publish" in (c.find_brief(c.find_project(self.repo), self.tid).meta.get("allow") or [])

    def main_log(self):
        return sh(self.origin, "log", "--format=%s", "main").split("\n")

    def ship(self, *a):
        return self.fm("ship", "--remote", "origin", *a, check=False)

    def test_the_test_run_is_marked_while_it_runs_and_waits_for_a_live_check(self):
        # T-1978: the ship's suite had no marker (a full fm check ran beside it) and ran beside a live check here
        import threading
        self.grant()
        p = c.find_project(self.repo)
        seen = os.path.join(self.tmp, "seen.json")
        busy = subprocess.Popen(["sleep", "2"])
        threading.Thread(target=busy.wait, daemon=True).start()
        c.update_meta(p, gates_running={"task": None, "pid": busy.pid, "at": c.now(), "root": p.root})
        r = self.ship("--test", f"cp {os.path.join(p.dir, 'meta.json')} {seen}")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("waiting for a running fm check", r.stdout + r.stderr)
        with open(seen) as f:
            self.assertEqual(json.load(f)["ship_testing"]["pid"] > 0, True)
        self.assertFalse(c.read_meta(p).get("ship_testing"), "cleared after the run")

    def test_the_default_fm_check_in_the_scratch_tree_never_waits_for_its_own_ship(self):
        # T-1978: the ship's marker names its scratch tree; the fm check it runs there must not wait 10 minutes on it
        self.grant()
        self.fm("check", "add", "true")
        r = self.ship()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("waiting for fm ship", r.stdout + r.stderr)
        self.assertIn("add b", self.main_log())

    def test_without_the_grant_nothing_is_pushed(self):
        r = self.ship("--test", "true")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("publish", r.stdout + r.stderr)
        self.assertNotIn("add a", self.main_log())

    def test_ships_the_unpushed_commits_after_the_test_and_spends_the_grant(self):
        self.grant()
        status = sh(self.repo, "status", "--porcelain")
        r = self.ship("--test", "true")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.main_log()[:2], ["add b", "add a"])
        tip = sh(self.origin, "rev-parse", "main").strip()
        self.assertIn(tip, r.stdout, "the pushed sha, for fm ci-watch --commit")
        self.assertEqual(sh(self.repo, "rev-parse", "refs/foreman/shipped-tip").strip(), tip,
                         "T-2527: fm ci-watch --last-ship reads the pushed main tip")
        self.assertIn("ci-watch", r.stdout)
        self.assertFalse(self.granted(), "the yes covers one push")
        self.assertEqual(sh(self.repo, "worktree", "list").count("\n"), 1, "the scratch worktree is gone")
        self.assertEqual(sh(self.repo, "status", "--porcelain"), status, "the working tree is untouched")
        r = self.ship("--test", "true")  # main has these changes now (by patch, not sha)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("nothing to ship", r.stdout)

    def test_chance_books_one_ship_bet_on_the_pushed_commit(self):  # T-2496
        self.grant()
        self.assertNotEqual(self.ship("--test", "true", "--chance", "150").returncode, 0)  # refused before any push
        self.assertTrue(self.granted())
        r = self.ship("--test", "true", "--chance", "85")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        tip = sh(self.origin, "rev-parse", "main").strip()
        rows = [json.loads(x) for x in open(os.path.join(c.find_project(self.repo).dir, "calibration.jsonl"))]
        self.assertEqual([(x["kind"], x["stated"], x["key"], x["task"]) for x in rows], [("ship", 85, tip[:12], self.tid)])
        self.assertIn("85%", r.stdout)

    def test_chance_far_from_the_ship_record_names_it_after_the_bet(self):  # T-2611
        with open(os.path.join(c.find_project(self.repo).dir, "calibration.jsonl"), "w") as f:
            for i in range(20):  # 20 earlier ship bets, all settled green by CI
                f.write(json.dumps({"ts": c.now(), "id": f"s{i}", "kind": "ship", "stated": 85, "base": None,
                                    "task": self.tid, "key": f"{i:012x}"}) + "\n")
                f.write(json.dumps({"ts": c.now(), "of": f"s{i}", "outcome": "held", "by": "seeded", "src": "ci"}) + "\n")
        self.grant()
        r = self.ship("--test", "true", "--chance", "80")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertRegex(r.stdout, r"Ship bet: 80%.*\nShip record: 20 of 20 ship bets held \(100%\); 80% sits 20 points under")

    def mover(self, rel):  # T-2511: a script the ship's test runs: someone else pushes a commit to main meanwhile
        script, other = os.path.join(self.tmp, f"move-{rel.replace('/', '-')}.sh"), os.path.join(self.tmp, "other")
        with open(script, "w") as f:
            f.write(f"set -e\nrm -rf {other}\ngit clone -q {self.origin} {other}\ncd {other}\n"
                    f"mkdir -p $(dirname {rel})\necho moved > {rel}\ngit add -A\n"
                    "git -c user.name=t -c user.email=t@t -c commit.gpgsign=false commit -qm 'tower update'\n"
                    "git push -q origin HEAD:main\n")
        return script

    def ci_ignores(self, *globs):  # T-2572: main's workflow names what CI never tests
        other = os.path.join(self.tmp, "ci")
        subprocess.run(["git", "clone", "-q", self.origin, other], check=True)
        os.makedirs(os.path.join(other, ".github", "workflows"))
        with open(os.path.join(other, ".github", "workflows", "test.yml"), "w") as f:
            f.write("on:\n  push:\n    paths-ignore:  # data\n" + "".join(f"      - {g}\n" for g in globs) + "jobs: {}\n")
        sh(other, "add", "-A")
        sh(other, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "ci")
        sh(other, "push", "-q", "origin", "HEAD:main")

    def test_a_main_that_moved_only_in_other_files_is_re_picked_and_pushed(self):  # T-2511
        self.ci_ignores("site/genesis/genesis.json", "site/genesis/gen-*/**")
        self.grant()
        r = self.ship("--test", f"sh {self.mover('site/genesis/genesis.json')}")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.main_log()[:3], ["add b", "add a", "tower update"])
        self.assertFalse(self.granted())

    def test_a_move_in_a_file_ci_tests_refuses_though_no_shipped_file_moved(self):  # T-2572
        self.ci_ignores("site/genesis/genesis.json")
        self.grant()
        r = self.ship("--test", f"sh {self.mover('lib/other.py')}")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("files CI tests", r.stdout + r.stderr)
        self.assertTrue(self.granted())

    def test_without_a_workflow_every_file_move_refuses(self):  # T-2572
        self.grant()
        r = self.ship("--test", f"sh {self.mover('site/genesis/genesis.json')}")
        self.assertNotEqual(r.returncode, 0)
        self.assertTrue(self.granted())

    def test_ci_globs_follow_github_rules(self):  # T-2572
        from sampo import release
        g = release._glob_re
        self.assertTrue(g("site/genesis/gen-*/**").match("site/genesis/gen-2/a/b.md"))
        self.assertFalse(g("site/genesis/gen-*/**").match("site/x.md"))
        self.assertFalse(g("site/genesis/*.md").match("site/genesis/gen-2/x.md"))
        self.assertTrue(g("'site/genesis/*.md'").match("site/genesis/frame.md"))

    def test_the_repos_ci_skips_every_tower_data_file_but_tests_the_page_code(self):
        # T-3439: the tower began publishing moons-catalogue shards that test.yml still tested, so every ship's 20-40
        # min window met a tower push "in files CI tests" and nothing reached GitHub for a night
        import shutil
        from helpers import PLUGIN
        from sampo import release
        root = os.path.dirname(PLUGIN)
        os.makedirs(os.path.join(self.repo, ".github", "workflows"))
        shutil.copy(os.path.join(root, ".github", "workflows", "test.yml"), os.path.join(self.repo, ".github", "workflows"))
        sh(self.repo, "add", "-A")
        sh(self.repo, "commit", "-qm", "the real workflow")
        ignored = release._ci_ignored(self.repo, "HEAD")
        files = sh(root, "ls-files", "site/genesis").split() + ["site/genesis/moons-catalogue-999.json"]
        data = [f for f in files if f.endswith((".json", ".md"))]
        self.assertTrue(data and len(data) < len(files), files)
        for f in files:
            self.assertEqual(any(g.match(f) for g in ignored), f in data, f)

    def test_a_tower_move_ships_when_the_pushed_trees_ci_skips_it(self):
        # T-3439: GitHub runs the pushed commit's workflow, so its globs decide, not the moved main's (which had none)
        os.makedirs(os.path.join(self.repo, ".github", "workflows"))
        with open(os.path.join(self.repo, ".github", "workflows", "test.yml"), "w") as f:
            f.write("on:\n  push:\n    paths-ignore:\n      - site/genesis/moons-catalogue*.json\njobs: {}\n")
        sh(self.repo, "add", "-A")
        sh(self.repo, "commit", "-qm", "ci")
        self.grant()
        r = self.ship("--test", f"sh {self.mover('site/genesis/moons-catalogue-001.json')}")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.main_log()[:4], ["ci", "add b", "add a", "tower update"])

    FAILS = "sh -c 'exit 1'"  # T-3468: the gate on record passed; run again here it would fail, so a ship shows reuse

    def _gated_with_a_tower_move(self, rel, gated=True):
        """T-3468: main's CI (which this branch shares) skips the moons shards; main then took a tower move in rel;
        HEAD's code tree passed the gates (as a train records it) or not."""
        self.ci_ignores("site/genesis/moons-catalogue*.json")
        sh(self.repo, "fetch", "-q", "origin", "main")
        sh(self.repo, "rebase", "-q", "origin/main")  # the workflow is main's, not one of the shipped commits
        subprocess.run(["sh", self.mover(rel)], check=True, capture_output=True)
        self.fm("check", "add", self.FAILS)
        p = c.find_project(self.repo)
        tree = sh(self.repo, "rev-parse", "HEAD^{tree}").strip()
        if gated:
            c.log_event(p, "check_run", data={"tree": tree[:12], "code_tree": c.code_tree(self.repo, tree),
                                              "env": c.env_id(), "train": [self.tid],
                                              "results": [{"cmd": self.FAILS, "exit": 0, "s": 1.0}]})
        self.grant()

    def test_a_ship_reuses_the_gates_pass_when_only_ci_skipped_tower_files_differ(self):
        # T-3468: after a train gated these very files, the ship ran the whole suite again (14-20 min) on them
        self._gated_with_a_tower_move("site/genesis/moons-catalogue-002.json")
        r = self.ship("--test", self.FAILS)  # a run would fail: the reuse is what ships
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("reusing the gates' pass", r.stdout)
        self.assertEqual(self.main_log()[:3], ["add b", "add a", "tower update"])

    def test_no_pass_or_a_tested_file_moved_runs_the_suite(self):
        self._gated_with_a_tower_move("site/genesis/moons-catalogue-002.json", gated=False)
        r = self.ship("--test", self.FAILS)
        self.assertNotEqual(r.returncode, 0, "no gates pass on record: the suite runs and fails")
        self.assertNotIn("reusing the gates' pass", r.stdout)

    def test_a_tower_move_in_a_file_tests_read_runs_the_suite(self):
        self._gated_with_a_tower_move("site/genesis/stats.json")  # not CI-skipped: test_docs reads the stats
        r = self.ship("--test", self.FAILS)  # T-3533: no affected command here, so its tests can't be picked
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("reusing the gates' pass", r.stdout)

    def _gated_with_a_linked_test(self, rel, template):
        """T-3533: a test linked (map-links) to the tower's stats, an affected runner that logs what it ran."""
        with open(os.path.join(self.repo, "test_stats.py"), "w") as f:
            f.write("# map-links: site/genesis/stats.json\n")
        sh(self.repo, "add", "test_stats.py")
        sh(self.repo, "commit", "-qm", "add test_stats")
        self.ran = os.path.join(self.tmp, "affected-ran")
        self.fm("check", "affected", f"echo {{tests}} >> {self.ran}; {template}")
        self._gated_with_a_tower_move(rel)
        return self.ship("--test", self.FAILS)  # the suite would fail: only a reuse ships

    def test_a_stats_only_difference_runs_its_affected_tests_not_the_suite(self):
        # T-3533: the tower's daily stats.json (which tests read) made every ship rerun the whole suite (17-25 min)
        r = self._gated_with_a_linked_test("site/genesis/stats.json", "true")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("reusing the gates' pass", r.stdout)
        self.assertIn("site/genesis/stats.json", r.stdout)
        self.assertIn("test_stats.py", r.stdout)
        with open(self.ran) as f:
            self.assertEqual(f.read().split(), ["test_stats.py"], "the stats' tests ran once, not the range's")
        self.assertEqual(self.main_log()[:4], ["add test_stats", "add b", "add a", "tower update"])

    def test_a_red_affected_run_on_a_stats_difference_stops_the_ship(self):  # T-3533
        r = self._gated_with_a_linked_test("site/genesis/stats.json", "echo 'FAIL: test_x (test_stats.Cls.test_x)'; exit 1")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("affected tests failed", r.stdout + r.stderr)
        self.assertNotIn("reusing the gates' pass", r.stdout)
        self.assertNotIn("add a", self.main_log())
        self.assertTrue(self.granted())

    def test_a_code_difference_still_runs_the_suite(self):  # T-3533
        r = self._gated_with_a_linked_test("plugin/lib/sampo/stats.py", "true")
        self.assertNotEqual(r.returncode, 0, "a plugin .py moved: the suite runs and fails")
        self.assertNotIn("reusing the gates' pass", r.stdout)

    def test_a_ship_test_other_than_the_gates_still_runs(self):
        # review: a saved or given --test (another runtime's suite) is not what the gates ran
        self._gated_with_a_tower_move("site/genesis/moons-catalogue-002.json")
        r = self.ship("--test", "false")
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("reusing the gates' pass", r.stdout)

    def test_a_main_that_moved_in_a_shipped_file_still_refuses(self):  # T-2511
        self.grant()
        r = self.ship("--test", f"sh {self.mover('a.txt')}")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("moved while testing", r.stdout + r.stderr)
        self.assertTrue(self.granted())

    def test_ship_on_a_named_tasks_grant(self):
        # T-2007: a finished task ships on its own grant: no separate ship task focused to hold one
        r = self.ship("--task", self.tid.lower(), "--test", "true")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(f"{self.tid} has no publish grant", r.stdout + r.stderr)
        self.assertNotIn("add a", self.main_log())
        r = self.ship("--task", "T-9999", "--test", "true")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no task T-9999", r.stdout + r.stderr)
        self.grant()
        other = json.loads(self.fm("task", "new", "other", "--type", "CLEAN", "--tier", "S", "--ac", "x :: true",
                                    "--step", "x", "--json").stdout)["id"]
        self.fm("checkpoint")
        self.fm("focus", other)  # the active task has no grant; the named one does
        r = self.ship("--task", self.tid, "--test", "true")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.main_log()[:2], ["add b", "add a"])
        self.assertFalse(self.granted(), "the named task's yes is spent")

    def test_check_says_whether_main_has_the_commits_without_the_grant(self):
        # T-1960: a ship task's verify was git log -n3 | grep, which a later ship pushes out of view
        first = sh(self.repo, "rev-parse", "HEAD~1").strip()
        r = self.ship("--check")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("add a", r.stdout)
        self.grant()
        self.assertEqual(self.ship(first, "--test", "true").returncode, 0)
        self.assertTrue(self.granted() is False)
        self.assertEqual(self.ship("--check", first).returncode, 0)
        r = self.ship("--check")
        self.assertEqual(r.returncode, 1, "add b is still missing")
        self.assertIn("add b", r.stdout)
        self.assertNotRegex(r.stdout, r"\badd a\b", "a word: a hash ending in 'add' before 'add b' reads as 'add a'")

    def test_a_named_commit_ships_alone(self):
        self.grant()
        first = sh(self.repo, "rev-parse", "HEAD~1").strip()
        r = self.ship(first, "--test", "true")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.main_log()[0], "add a")
        self.assertNotIn("add b", self.main_log())

    def test_a_failed_test_pushes_nothing_and_keeps_the_grant(self):
        self.grant()
        r = self.ship("--test", "false")
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("add a", self.main_log())
        self.assertTrue(self.granted())
        self.assertEqual(sh(self.repo, "worktree", "list").count("\n"), 1)

    def verified(self, rev, cmd=None):
        """T-3666: the post-merge verify's clean uncached run on rev's tree, as pm_verify records it."""
        from sampo import lanes
        sha = sh(self.repo, "rev-parse", rev).strip()
        lanes.pm_full_pass(c.find_project(self.repo), self.repo, sha, cmd or self.FAILS)
        return sha

    def test_a_verified_commit_older_than_head_ships_without_the_suite(self):
        # T-3666: every ship ran the whole suite on HEAD (~80 CPU-min), a tree the verify had almost never run yet
        self.grant()
        first = self.verified("HEAD~1")
        r = self.ship("--test", self.FAILS + " -j 6")  # a run would fail; the job count runs the same tests
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.main_log()[0], "add a")
        self.assertNotIn("add b", self.main_log(), "the unverified commit waits")
        self.assertIn(f"shipping {first[:9]}", r.stdout)
        self.assertIn("1 newer commit", r.stdout)
        self.assertIn("reusing the post-merge verify's full run", r.stdout)
        self.assertEqual(sh(self.repo, "rev-parse", "refs/foreman/shipped").strip(), first)
        self.grant()  # nothing newer verified: the next ship tests HEAD and lands the rest
        r = self.ship("--test", "true")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.main_log()[:2], ["add b", "add a"])

    def test_a_verified_commit_at_head_ships_everything_without_the_suite(self):  # T-3666
        self.grant()
        self.verified("HEAD")
        r = self.ship("--test", self.FAILS)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.main_log()[:2], ["add b", "add a"])
        self.assertIn("reusing the post-merge verify's full run", r.stdout)

    def test_no_verified_commit_newer_than_main_tests_head(self):  # T-3666
        self.grant()
        self.verified("origin/main")
        r = self.ship("--test", self.FAILS)
        self.assertNotEqual(r.returncode, 0, "the suite ran on HEAD and failed")
        self.assertNotIn("add a", self.main_log())
        self.assertNotIn("post-merge verify", r.stdout)

    def test_head_flag_tests_head(self):  # T-3666
        self.grant()
        self.verified("HEAD~1")
        r = self.ship("--head", "--test", "true")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.main_log()[:2], ["add b", "add a"])
        self.assertNotIn("post-merge verify", r.stdout)

    def test_another_suite_command_or_env_or_a_stale_pass_tests_head(self):  # T-3666
        from sampo import lanes
        p = c.find_project(self.repo)
        self.grant()
        self.verified("HEAD~1", "python3 plugin/tests/run.py")  # another suite than the ship's
        self.verified("HEAD~1")
        self.verified("HEAD~1")
        path = os.path.join(p.dir, "postmerge", ".full.json")
        with open(path) as f:
            rows = json.load(f)
        rows[1]["env"], rows[2]["at"] = "another-python", "2026-01-01T00:00:00Z"
        c.write_atomic(path, json.dumps(rows))
        self.assertEqual(len(lanes.pm_full_passes(p)), 3)
        r = self.ship("--test", self.FAILS)
        self.assertNotEqual(r.returncode, 0, "no pass holds: the suite ran on HEAD and failed")
        self.assertNotIn("add a", self.main_log())
        self.assertNotIn("post-merge verify", r.stdout)

    def test_the_default_fm_check_tests_head(self):  # T-3666: its lint gates are no part of the verify's suite
        self.grant()
        self.fm("check", "add", "false")
        self.verified("HEAD~1", "")
        r = self.ship()
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("add a", self.main_log())
        self.assertNotIn("post-merge verify", r.stdout)

    def test_the_private_scan_still_runs_on_the_verified_commit(self):  # T-3666
        self.grant()
        self.fm("private", "add", "*.home.example-private.test")
        with open(os.path.join(self.repo, "c.txt"), "w") as f:
            f.write("url = http://llm.home.example-private.test/v1\n")
        sh(self.repo, "add", "c.txt")
        sh(self.repo, "commit", "-qm", "add c")
        leak = self.verified("HEAD")
        with open(os.path.join(self.repo, "d.txt"), "w") as f:
            f.write("d\n")
        sh(self.repo, "add", "d.txt")
        sh(self.repo, "commit", "-qm", "add d")
        r = self.ship("--test", self.FAILS)
        out = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 0, out)
        self.assertIn(f"shipping {leak[:9]}", out)
        self.assertIn("c.txt", out)
        self.assertIn("private term", out)
        self.assertNotIn("add a", self.main_log())
        self.assertTrue(self.granted())

    def test_a_private_term_or_a_secret_value_in_the_shipped_commits_or_tree_refuses_naming_where(self):  # T-3529
        self.grant()
        self.fm("private", "add", "*.home.example-private.test")
        with open(os.path.join(self.repo, "c.txt"), "w") as f:
            f.write("url = http://llm.home.example-private.test/v1\n")
        sh(self.repo, "add", "c.txt")
        sh(self.repo, "commit", "-qm", "add c")
        leak = sh(self.repo, "rev-parse", "HEAD").strip()
        ran = os.path.join(self.tmp, "ran")
        r = self.ship("--test", f"touch {ran}")
        out = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 0, out)
        self.assertIn("c.txt", out)
        self.assertIn(leak[:8], out, "the commit that added it")
        self.assertIn("private term", out)
        self.assertNotIn("example-private", out, "the term itself is never printed")
        self.assertIn("ship again", out, "how to fix")
        self.assertFalse(os.path.exists(ran), "refused before the tests run")
        self.assertNotIn("add a", self.main_log())
        self.assertTrue(self.granted())
        with open(os.path.join(self.repo, "c.txt"), "w") as f:
            f.write("url = http://<host>/v1\n")
        sh(self.repo, "commit", "-qam", "redact c")
        r = self.ship("--test", "true")
        out = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 0, "the commit that added it still carries it: amend or rebase it out")
        self.assertIn(f"private term (sampo private list) or a secrets-file value is in commit {leak[:8]};", out)
        self.assertNotIn("c.txt", out, "the tree is clean now")
        sh(self.repo, "reset", "-q", "--hard", "HEAD~2")
        secrets = os.path.join(self.tmp, "secrets.env")
        with open(secrets, "w") as f:
            f.write("export FIXTURE_TOKEN='fixture-value-0123456789'\nSHORT=abc\nBASE_URL=http://127.0.0.1:11434/v1\n")
        os.chmod(secrets, 0o600)
        def commit(name, text, msg):  # a file each: an empty commit doesn't cherry-pick
            with open(os.path.join(self.repo, name), "w") as f:
                f.write(text)
            sh(self.repo, "add", name)
            sh(self.repo, "commit", "-qm", msg)
        commit("d.txt", "base_url = http://127.0.0.1:11434/v1\n", "local model")
        env = {"FOREMAN_SECRETS": secrets}
        r = self.fm("ship", "--remote", "origin", "--test", "true", check=False, env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)  # a URL line names no secret
        self.grant()
        commit("e.txt", "e\n", "token fixture-value-0123456789")
        r = self.fm("ship", "--remote", "origin", "--test", "true", check=False, env=env)
        out = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 0, out)
        self.assertIn(sh(self.repo, "rev-parse", "HEAD").strip()[:8], out)
        self.assertIn("secrets-file value", out)
        self.assertNotIn("fixture-value", out)
        self.assertNotIn("token", self.main_log()[0])
        self.assertTrue(self.granted())

    def test_the_private_scan_never_puts_a_term_or_a_secret_on_gits_command_line(self):  # T-3529 review
        # argv shows in ps and /proc/<pid>/cmdline to anyone on the machine: needles go through a 0600 pattern file
        from unittest import mock
        from sampo import cli, release
        secrets = os.path.join(self.tmp, "secrets.env")
        with open(secrets, "w") as f:
            f.write("FIXTURE_TOKEN=fixture-value-0123456789\n")
        os.chmod(secrets, 0o600)
        with open(os.path.join(c.state_dir(), c.PRIVATE_TERMS), "w") as f:
            json.dump(["*.home.example-private.test"], f)
        os.makedirs(os.path.join(self.repo, "hosts", "llm.home.example-private.test"))
        for rel, text in (("c.txt", "key fixture-value-0123456789\n"), ("hosts/llm.home.example-private.test/notes.md", "n\n")):
            with open(os.path.join(self.repo, rel), "w") as f:
                f.write(text)
        sh(self.repo, "add", "-A")
        sh(self.repo, "commit", "-qm", "leak")
        tip, seen, real = sh(self.repo, "rev-parse", "HEAD").strip(), [], c._git
        def spy(root, *a, **kw):
            seen.append(a)
            return real(root, *a, **kw)
        with mock.patch.dict(os.environ, {"FOREMAN_SECRETS": secrets}), mock.patch.object(c, "_git", spy):
            with self.assertRaises(cli.UsageError) as e:
                release._refuse_private(self.repo, [tip], tip)
        msg = str(e.exception)
        self.assertIn("c.txt", msg)
        self.assertIn("hosts/<host>/notes.md", msg, "a path holding a term is named redacted")
        self.assertIn(tip[:8], msg)
        self.assertTrue(any("grep" in a for a in seen))
        argv = "\0".join(x for a in seen for x in a).lower()
        for needle in ("example-private", "fixture-value"):
            self.assertNotIn(needle, argv)
            self.assertNotIn(needle, msg)
        seen.clear()  # T-3551: the tower's touched files go to git grep as the pathspec, unless one holds a needle
        with mock.patch.dict(os.environ, {"FOREMAN_SECRETS": secrets}), mock.patch.object(c, "_git", spy):
            self.assertEqual(release._private_hits(self.repo, [], tip, {"c.txt"}), ([], ["c.txt"]))
            self.assertIn(":(literal)c.txt", [x for a in seen if "grep" in a for x in a])
            seen.clear()
            got = release._private_hits(self.repo, [], tip, {"c.txt", "hosts/llm.home.example-private.test/notes.md"})
        self.assertEqual(got, ([], ["c.txt", "hosts/<host>/notes.md"]))
        self.assertNotIn("example-private", "\0".join(x for a in seen for x in a).lower())

    def test_a_tree_changed_after_the_tests_ran_refuses_with_both_hashes(self):  # T-2642
        self.grant()
        r = self.ship("--test", "sh -c 'echo x > c.txt && git add c.txt && git -c user.name=t -c user.email=t@t commit -qm sneak'")
        out = r.stdout + r.stderr
        self.assertNotEqual(r.returncode, 0, out)
        self.assertIn("tree", out)
        self.assertRegex(out, r"[0-9a-f]{40}.*[0-9a-f]{40}")
        self.assertNotIn("add a", self.main_log())
        self.assertNotIn("sneak", self.main_log())
        self.assertTrue(self.granted())

    def affected_ship(self, template):
        # T-2323: a changed test file in the range, and a stubbed affected runner; the full suite writes a marker
        self.grant()
        with open(os.path.join(self.repo, "test_new.py"), "w") as f:
            f.write("x = 1\n")
        sh(self.repo, "add", "test_new.py")
        sh(self.repo, "commit", "-qm", "add test_new")
        self.fm("check", "affected", template)
        self.marker = os.path.join(self.tmp, "full-ran")
        return self.ship("--test", f"touch {self.marker}")

    def test_a_red_affected_run_stops_the_ship_before_the_full_suite_naming_the_class(self):
        r = self.affected_ship("echo {tests}; echo 'FAIL: test_x (test_new.Cls.test_x)'; exit 1")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("affected tests failed", r.stdout + r.stderr)
        self.assertIn("test_new.Cls", r.stdout + r.stderr)
        self.assertFalse(os.path.exists(self.marker), "the full suite never started")
        self.assertNotIn("add test_new", self.main_log())
        self.assertTrue(self.granted())

    def test_a_green_affected_run_goes_on_to_the_full_suite(self):
        r = self.affected_ship("echo {tests}")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(os.path.exists(self.marker))
        self.assertIn("add test_new", self.main_log())

    def test_a_red_test_captures_one_baseline_task_naming_the_failing_test(self):
        # T-2255: the ship's red run files what fm check would, once per failing set, and still pushes nothing
        self.grant()
        red = "sh -c 'echo \"FAIL: test_x (test_mod.Cls.test_x)\"; echo Ran 1 test; echo \"FAILED (failures=1)\"; exit 1'"
        r = self.ship("--test", red)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("FAIL: test_x", r.stdout)  # still streamed
        self.assertNotIn("add a", self.main_log())
        self.assertTrue(self.granted())
        p = c.find_project(self.repo)
        base = [b for b in c.load_briefs(p) if b.meta.get("baseline")]
        self.assertEqual(len(base), 1)
        self.assertEqual(base[0].meta.get("priority"), "urgent")
        self.assertIn("fails test_mod.Cls.test_x.", base[0].section("Raw request"))
        self.assertIn(base[0].id, r.stderr + r.stdout)
        self.ship("--test", red)
        self.assertEqual(len([b for b in c.load_briefs(p) if b.meta.get("baseline")]), 1)

    def base(self):
        return [b for b in c.load_briefs(c.find_project(self.repo)) if b.meta.get("baseline")]

    def test_a_background_process_holding_the_output_doesnt_hang_the_ship(self):
        import time
        self.grant()
        t = time.time()
        r = self.ship("--test", "sh -c 'sleep 30 & exit 1'")
        self.assertNotEqual(r.returncode, 0)
        self.assertLess(time.time() - t, 20)  # wall clock: a ship child must not wait out its background sleeper
        self.assertNotIn("add a", self.main_log())

    def test_a_failing_name_is_cleaned_and_cut_before_it_is_written(self):
        self.grant()
        out = os.path.join(self.tmp, "out.txt")  # a file, so the 10k name isn't also in the command
        with open(out, "w") as f:
            f.write("FAILED t.py::test_x_" + "y" * 10000 + "\x1b[31mz\n")
        red = f"sh -c 'cat {out}; exit 1'"
        r = self.ship("--test", red)
        self.assertTrue(self.base(), r.stdout[-300:] + r.stderr[-500:])
        text = self.base()[0].section("Raw request")
        self.assertNotIn("\x1b", text)
        self.assertLess(len(text), 1500)

    def test_an_environment_failure_captures_nothing(self):
        self.grant()
        red = "sh -c 'echo \"FAIL: test_x (m.C.test_x)\"; echo \"No space left on device\"; exit 1'"
        r = self.ship("--test", red)
        self.assertIn("the test failed in the scratch tree", r.stderr + r.stdout)
        self.assertFalse(self.base())

    def rerun_ship(self, alone, affected=None):
        # T-3435: a red class is rerun alone (FM_POSTMERGE_RERUN is the seam); green alone is a flake
        self.grant()
        if affected:
            with open(os.path.join(self.repo, "test_new.py"), "w") as f:
                f.write("x = 1\n")
            sh(self.repo, "add", "test_new.py")
            sh(self.repo, "commit", "-qm", "add test_new")
            self.fm("check", "affected", affected)
        red = "sh -c 'echo \"FAIL: test_x (test_mod.Cls.test_x)\"; exit 1'"
        return self.fm("ship", "--remote", "origin", "--test", red, check=False,
                       env={"FM_POSTMERGE_RERUN": alone, "FOREMAN_LOAD_GATE": "0"})

    def test_a_class_red_in_the_suite_but_green_alone_is_a_flake_that_ships(self):  # T-3435
        r = self.rerun_ship("true")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("flaky", r.stdout + r.stderr)
        self.assertIn("test_mod.Cls", r.stdout + r.stderr)
        self.assertIn("add a", self.main_log())
        self.assertFalse(self.base())  # the verdict is the rerun's: no baseline task

    def test_a_class_red_alone_too_still_refuses(self):  # T-3435
        r = self.rerun_ship("false")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("the test failed in the scratch tree", r.stdout + r.stderr)
        self.assertNotIn("add a", self.main_log())

    def test_a_flaky_affected_class_goes_on_to_the_suite(self):  # T-3435
        r = self.rerun_ship("true", affected="echo 'FAIL: test_y (test_aff.Cls.test_y)'; exit 1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("flaky", r.stdout + r.stderr)
        self.assertIn("test_aff.Cls", r.stdout + r.stderr)

    def test_an_affected_class_red_alone_too_refuses(self):  # T-3435
        r = self.rerun_ship("false", affected="echo 'FAIL: test_y (test_aff.Cls.test_y)'; exit 1")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("affected tests failed", r.stdout + r.stderr)

    def test_a_green_ship_records_the_footprint_and_a_red_one_does_not(self):  # T-2281
        self.grant()
        rec = os.path.join(c.find_project(self.repo).dir, "footprint.json")
        self.ship("--test", "false")
        self.assertFalse(os.path.exists(rec))
        self.assertEqual(self.ship("--test", "true").returncode, 0)
        self.assertFalse(os.path.exists(rec))  # no plugin/rules/foreman.md in this repo: not Foreman's own
        os.makedirs(os.path.join(self.repo, "plugin", "rules"))
        with open(os.path.join(self.repo, "plugin", "rules", "foreman.md"), "w") as f:
            f.write("a rule\n")
        sh(self.repo, "add", "plugin")
        sh(self.repo, "commit", "-qm", "rules")
        self.grant()
        self.assertEqual(self.ship("--test", "true").returncode, 0)
        with open(rec) as f:
            got = json.load(f)["metrics"]
        self.assertIn("rules chars", got)
        self.assertNotIn("SessionStart chars", got)  # no bench in this tree: not measured
        # T-2358: with the tree's own hook bench, the record carries injected chars and each hook's p95
        os.makedirs(os.path.join(self.repo, "plugin", "tests"))
        with open(os.path.join(self.repo, "plugin", "tests", "bench_hooks.py"), "w") as f:
            f.write("import json, sys\nassert '--inject' in sys.argv and '--json' in sys.argv\n"
                    "print(json.dumps({'bench': {'PreToolUse:Bash': {'p95': 40.0, 'cpu_p95': 30.0, 'load': 0.1}},"
                    " 'injected': {'SessionStart': 1234, 'UserPromptSubmit': 56}}))\n")
        sh(self.repo, "add", "plugin")
        sh(self.repo, "commit", "-qm", "bench")
        self.grant()
        r = self.fm("ship", "--remote", "origin", "--test", "true", check=False, env={"FOREMAN_DOCTOR_BENCH": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(rec) as f:
            got = json.load(f)["metrics"]
        self.assertEqual((got.get("SessionStart chars"), got.get("PreToolUse:Bash p95 ms")), (1234, 40.0))

    def test_a_green_ship_names_an_open_baseline_task_it_turned_green(self):
        self.grant()
        flag = os.path.join(self.tmp, "ok")
        cmd = f"sh -c 'test -f {flag} || {{ echo \"FAIL: test_x (m.C.test_x)\"; exit 1; }}'"
        self.ship("--test", cmd)
        tid = self.base()[0].id
        open(flag, "w").close()
        r = self.ship("--test", cmd)
        self.assertEqual(r.returncode, 0)
        self.assertIn(f"baseline is green again: {tid}", r.stdout)

    def test_a_red_test_naming_no_failing_test_captures_nothing(self):
        self.ship("--test", "false")
        self.assertFalse([b for b in c.load_briefs(c.find_project(self.repo)) if b.meta.get("baseline")])

    def test_main_moving_during_the_test_in_no_shipped_file_is_re_picked(self):
        # T-2511: an empty commit lands on main mid-test; it touches no file the ship does, so the tested result
        # still holds on the new main (a move in a shipped file refuses: test_a_main_that_moved_in_a_shipped_file...)
        self.grant()
        other = os.path.join(self.tmp, "other")
        subprocess.run(["git", "clone", "-q", self.origin, other], check=True)
        sh(other, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "someone else")
        r = self.ship("--test", f"git -C {other} push -q origin HEAD:main")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("re-picked onto it", r.stdout)
        self.assertEqual(self.main_log()[:3], ["add b", "add a", "someone else"])
        self.assertFalse(self.granted())

    def test_main_moving_between_the_last_check_and_the_push_is_re_picked_without_a_retest(self):
        # T-2555: fm genesis live's tower publish landed between ship's last fetch and its push: "cannot lock ref"
        hook = os.path.join(self.origin, "hooks", "pre-receive")
        with open(hook, "w") as f:  # the first push moves main itself (an empty commit), as a racing writer would
            f.write('#!/bin/sh\n[ -f "$GIT_DIR/moved" ] && exit 0\ntouch "$GIT_DIR/moved"\n'
                    "unset GIT_QUARANTINE_PATH GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES\n"
                    "old=$(git rev-parse refs/heads/main)\n"
                    'new=$(echo "tower publish" | GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t '
                    'GIT_COMMITTER_EMAIL=t@t git commit-tree "$old^{tree}" -p "$old")\n'
                    'git update-ref refs/heads/main "$new"\n')
        os.chmod(hook, 0o755)
        self.grant()
        runs = os.path.join(self.tmp, "runs")
        r = self.ship("--test", f"sh -c 'echo x >> {runs}'")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.main_log()[:3], ["add b", "add a", "tower publish"])
        with open(runs) as f:
            self.assertEqual(f.read().count("x"), 1, "the move touched no shipped file: no second test run")
        self.assertFalse(self.granted())

    def test_a_rejected_push_keeps_the_grant(self):
        # review: the grant was spent before git push, so a rejected push cost the yes
        hook = os.path.join(self.origin, "hooks", "pre-receive")
        with open(hook, "w") as f:
            f.write("#!/bin/sh\necho no >&2\nexit 1\n")
        os.chmod(hook, 0o755)
        self.grant()
        r = self.ship("--test", "true")
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("add a", self.main_log())
        self.assertTrue(self.granted(), "a push that failed gives the grant back")
        self.assertEqual(sh(self.repo, "worktree", "list").count("\n"), 1)

    def test_a_relative_remote_path_and_a_missing_test_program(self):
        self.grant()
        r = self.ship("--test", "no-such-program-1943")
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("Traceback", r.stderr)
        self.assertTrue(self.granted())
        sh(self.repo, "remote", "set-url", "origin", os.path.relpath(self.origin, self.repo))
        r = self.ship("--test", "true")  # fetched and pushed from the repo, not the scratch tree
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.main_log()[0], "add b")

    def test_a_plain_ship_starts_after_the_shipped_ref(self):
        # T-1945: main held shipped commits under another patch (a merge), so git cherry listed them every time
        self.grant()
        self.assertEqual(self.ship("--test", "true").returncode, 0)
        other = os.path.join(self.tmp, "other")
        subprocess.run(["git", "clone", "-q", self.origin, other], check=True)
        sh(other, "reset", "-q", "--hard", "HEAD~2")
        for name in ("a", "b"):
            with open(os.path.join(other, f"{name}.txt"), "w") as f:
                f.write(name + "\n")
        sh(other, "add", ".")
        sh(other, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "a and b squashed")
        sh(other, "push", "-qf", "origin", "HEAD:main")
        with open(os.path.join(self.repo, "c.txt"), "w") as f:
            f.write("c\n")
        sh(self.repo, "add", "c.txt")
        sh(self.repo, "commit", "-qm", "add c")
        self.grant()
        r = self.ship("--test", "true")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.main_log()[:2], ["add c", "a and b squashed"])
        self.assertEqual(sh(self.repo, "rev-parse", "refs/foreman/shipped"), sh(self.repo, "rev-parse", "HEAD"))

    def test_a_merged_lane_whose_merge_settled_a_conflict_ships_through_the_merge(self):
        # T-2054: a builder lane's own commit didn't cherry-pick onto main alone; its merge on the branch had settled it
        self.grant()
        self.assertEqual(self.ship("--test", "true").returncode, 0)
        shipped = sh(self.repo, "rev-parse", "HEAD").strip()
        with open(os.path.join(self.repo, "shared.txt"), "w") as f:
            f.write("one\n")
        sh(self.repo, "add", "shared.txt")
        sh(self.repo, "commit", "-qm", "branch writes shared")
        sh(self.repo, "checkout", "-q", "-b", "lane", shipped)
        with open(os.path.join(self.repo, "shared.txt"), "w") as f:
            f.write("two\n")
        sh(self.repo, "add", "shared.txt")
        sh(self.repo, "commit", "-qm", "lane writes shared")
        sh(self.repo, "checkout", "-q", "work")
        subprocess.run(["git", "-C", self.repo, "merge", "-q", "lane"], capture_output=True)
        with open(os.path.join(self.repo, "shared.txt"), "w") as f:
            f.write("one\ntwo\n")
        sh(self.repo, "add", "shared.txt")
        sh(self.repo, "commit", "-qm", "Merge lane")
        self.grant()
        r = self.ship("--test", "true")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(sh(self.origin, "show", "main:shared.txt"), "one\ntwo\n")
        self.assertEqual(self.main_log()[:2], ["Merge lane", "branch writes shared"])
        self.assertEqual(sh(self.repo, "rev-parse", "refs/foreman/shipped"), sh(self.repo, "rev-parse", "HEAD"))

    def _merged_lane_then_main_moves(self, name):
        # T-2212: the T-2054 setup, then someone pushes a commit to origin main touching `name` after the last ship
        self.grant()
        self.assertEqual(self.ship("--test", "true").returncode, 0)
        shipped = sh(self.repo, "rev-parse", "HEAD").strip()
        for branch, text, msg in (("work", "one\n", "branch writes shared"), ("lane", "two\n", "lane writes shared")):
            if branch == "lane":
                sh(self.repo, "checkout", "-q", "-b", "lane", shipped)
            with open(os.path.join(self.repo, "shared.txt"), "w") as f:
                f.write(text)
            sh(self.repo, "add", "shared.txt")
            sh(self.repo, "commit", "-qm", msg)
        sh(self.repo, "checkout", "-q", "work")
        subprocess.run(["git", "-C", self.repo, "merge", "-q", "lane"], capture_output=True)
        with open(os.path.join(self.repo, "shared.txt"), "w") as f:
            f.write("one\ntwo\n")
        sh(self.repo, "add", "shared.txt")
        sh(self.repo, "commit", "-qm", "Merge lane")
        other = os.path.join(self.tmp, "other")
        subprocess.run(["git", "clone", "-q", self.origin, other], check=True)
        os.makedirs(os.path.dirname(os.path.join(other, name)), exist_ok=True)
        with open(os.path.join(other, name), "w") as f:
            f.write("main moved\n")
        sh(other, "add", ".")
        sh(other, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "main moves")
        sh(other, "push", "-q", "origin", "HEAD:main")
        self.grant()

    def test_a_merged_lane_ships_when_main_moved_only_in_other_files(self):
        self._merged_lane_then_main_moves("site/genesis.json")
        r = self.ship("--test", "true")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(sh(self.origin, "show", "main:shared.txt"), "one\ntwo\n")
        self.assertEqual(sh(self.origin, "show", "main:site/genesis.json"), "main moved\n")

    def test_a_merged_lane_is_refused_when_main_touched_the_branchs_files(self):
        self._merged_lane_then_main_moves("shared.txt")
        before = sh(self.origin, "rev-parse", "main")
        r = self.ship("--test", "true")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(sh(self.origin, "rev-parse", "main"), before)

    def test_a_tower_update_pushed_while_the_ship_tests_is_re_picked_over(self):
        # T-2963: the live tower pushed genesis.json mid-test and the re-pick conflicted (ship 4)
        self.ci_ignores("site/genesis/**")  # as the real repo's CI does
        other = self._tower_setup()
        push = (f"cd {other} && echo tower2 > site/genesis/genesis.json && git -c user.name=t -c user.email=t@t "
                "commit -qam 'Genesis: tower update (fm genesis live)' && git push -q origin HEAD:main")
        r = self.ship("--test", f"sh -c {shlex.quote(push)}")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(sh(self.origin, "show", "main:shared.txt"), "one\ntwo\n")
        self.assertEqual(sh(self.origin, "show", "main:site/genesis/genesis.json"), "tower2\n")

    def _tower_setup(self):
        self._merged_lane_then_main_moves("site/genesis/genesis.json")
        sh(self.repo, "checkout", "-q", "work")
        os.makedirs(os.path.join(self.repo, "site", "genesis"), exist_ok=True)
        with open(os.path.join(self.repo, "site", "genesis", "genesis.json"), "w") as f:
            f.write("branch\n")
        sh(self.repo, "add", ".")
        sh(self.repo, "commit", "-qm", "branch touches the tower file")
        other = os.path.join(self.tmp, "other")
        with open(os.path.join(other, "site", "genesis", "genesis.json"), "w") as f:
            f.write("tower\n")
        sh(other, "add", ".")
        sh(other, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "Genesis: tower update (fm genesis live)")
        sh(other, "push", "-q", "origin", "HEAD:main")
        self.grant()
        return other

    def test_a_branch_that_touched_a_tower_file_ships_over_the_live_towers_commit(self):
        # T-2955: fm genesis live publishes site/genesis/* to main behind the branch; the branch touched genesis.json
        # too, so the replay fell back to git cherry and the pick conflicted: the tower's file keeps main's version
        self._tower_setup()
        r = self.ship("--test", "true")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(sh(self.origin, "show", "main:shared.txt"), "one\ntwo\n")
        self.assertEqual(sh(self.origin, "show", "main:site/genesis/genesis.json"), "tower\n")

    def test_a_remembered_test_runs_when_test_is_left_out(self):
        # T-1950: the oldest runtime's suite was retyped on every ship
        mark = os.path.join(self.tmp, "ran")
        self.grant()
        self.assertEqual(self.ship("--test", f"touch {mark}").returncode, 0)
        os.remove(mark)
        with open(os.path.join(self.repo, "c.txt"), "w") as f:
            f.write("c\n")
        sh(self.repo, "add", "c.txt")
        sh(self.repo, "commit", "-qm", "add c")
        self.grant()
        r = self.ship()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(os.path.exists(mark), "the remembered --test ran")
        self.assertIn(f"touch {mark}", r.stdout)

    def test_a_failed_ref_write_after_the_push_still_reports_the_ship(self):
        # T-1951: main got the commits, so a refs/foreman/shipped lock must not read as a failed ship
        lock = os.path.join(self.repo, ".git", "refs", "foreman", "shipped.lock")
        os.makedirs(os.path.dirname(lock))
        open(lock, "w").close()
        self.grant()
        r = self.ship("--test", "true")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.main_log()[:2], ["add b", "add a"])
        self.assertIn("shipped 2 commits", r.stdout)
        self.assertIn("refs/foreman/shipped", r.stdout + r.stderr)

    def test_watch_watches_the_pushed_commit(self):
        # T-1946: ship, then CI on the pushed sha, in one background job
        import sys
        bin_dir = os.path.join(self.tmp, "bin")
        os.makedirs(bin_dir)
        with open(os.path.join(bin_dir, "gh"), "w") as f:
            f.write(f"#!{sys.executable}\nimport json, subprocess, sys\n"
                    "if sys.argv[1:3] != ['run', 'list']: sys.exit(3)\n"
                    f"sha = subprocess.run(['git', '-C', {self.origin!r}, 'rev-parse', 'main'], capture_output=True, text=True).stdout.strip()\n"
                    "print(json.dumps([{'databaseId': 1, 'status': 'completed', 'conclusion': 'success', 'headSha': sha, "
                    "'url': 'https://github.com/o/r/actions/runs/1'}]))\n")
        os.chmod(os.path.join(bin_dir, "gh"), 0o755)
        self.grant()
        r = self.fm("ship", "--remote", "origin", "--test", "true", "--watch",
                    env={"PATH": bin_dir + os.pathsep + os.environ["PATH"]}, check=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        tip = sh(self.origin, "rev-parse", "main").strip()
        self.assertIn(f"watching {tip[:7]} on main", r.stdout)
        self.assertIn("CI green", r.stdout)

    def test_a_remote_or_commit_that_reads_as_an_option_is_refused(self):
        self.grant()
        for argv in (["--remote=--upload-pack=touch x"], ["--remote", "origin", "--", "--output=x"]):
            r = self.fm("ship", *argv, "--test", "true", check=False)
            self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("add a", self.main_log())
        self.assertTrue(self.granted())
