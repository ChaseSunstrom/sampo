"""T-5728: a plan refusal costs one round: every gap on its own short line, a PLANS.json skeleton with a TODO per gap that
one `sampo task plan --file` call fills, and a choice under the race bar gets its cold rival read from lane brief itself."""
import argparse
import json
import os
import re
import unittest
from unittest import mock

import helpers  # noqa: F401
from helpers import ForemanTestCase

from sampo import core as c
from sampo import lanes

AP = "Approach (options → choice → why)"
GOOD_INTERP = "Reading 1: the gate counts options. Reading 2: the gate reads the words under each option."
GOOD_AP = ("1) use what exists: the existing gap list. likely outcome: the data is there. fails if: a caller cuts it. "
           "2) do less: one gap per line. fails if: the skeleton still costs a round. "
           "3) print every gap and write the skeleton. likely outcome: one round. fails if: the file is stale. "
           "Choice 3 ({pct}%): every measured extra round is removed.")


def new_task(t, tier="M", **kw):
    args = ["task", "new", "Plan in one round", "--type", "FIX", "--tier", tier, "--raw", "plan in one round"]
    for k, v in kw.items():
        args += [f"--{k}", v]
    return json.loads(t.fm(*args, "--json").stdout)["id"]


class OneRound(ForemanTestCase):
    def bare(self):
        """An M task with nothing planned but one criterion with no check and one step."""
        return new_task(self, ac="it works", step="do it")

    def refusal(self, tid):
        r = self.fm("lane", "brief", tid, check=False)
        self.assertNotEqual(r.returncode, 0, r.stdout)
        return r.stderr + r.stdout

    def test_one_round_each_gap_is_one_short_line(self):
        tid = self.bare()
        text = self.refusal(tid)
        gaps = c.plan_gaps(c.find_brief(c.find_project(self.repo), tid), "standard")
        lines = [ln for ln in text.splitlines() if ln.startswith("- ")]
        self.assertGreaterEqual(len(lines), 3, text)
        self.assertTrue(all(len(ln) < 120 for ln in lines), [ln for ln in lines if len(ln) >= 120])
        self.assertLessEqual(len(lines), len(gaps), "no gap is printed twice")
        for want in ("Interpretation", "Approach", "verify command on criterion 1"):
            self.assertEqual(sum(want in ln for ln in lines), 1, f"{want}: one line\n{text}")
        self.assertFalse([ln for ln in text.splitlines() if ", Approach" in ln and "missing" in ln], "never one joined line")

    def test_one_round_the_skeleton_holds_the_sections_and_a_todo_per_gap(self):
        tid = self.bare()
        text = self.refusal(tid)
        path = re.search(r"(\S*PLANS\.json)", text).group(1)
        self.assertTrue(os.path.isfile(path), path)
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        e = data["plans"][0] if isinstance(data, dict) else data[0]
        self.assertEqual(e["id"], tid)
        self.assertIn("<TODO:", e["interpretation"])
        self.assertIn("<TODO:", e["approach"])
        self.assertEqual(len(e["criteria"]), 1)
        self.assertTrue(e["criteria"][0].startswith("it works") and "<TODO:" in e["criteria"][0], e["criteria"])
        self.assertEqual(e["steps"], ["do it"], "the current steps are kept")
        lines = [ln for ln in text.splitlines() if ln.startswith("- ")]
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read().count("<TODO:"), len(lines), "one TODO per gap")

    def test_one_round_the_skeleton_filled_is_one_plan_call(self):
        tid = self.bare()
        path = re.search(r"(\S*PLANS\.json)", self.refusal(tid)).group(1)
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        e = data["plans"][0] if isinstance(data, dict) else data[0]
        stale = self.fm("task", "plan", "--file", path, check=False)
        self.assertNotEqual(stale.returncode, 0)
        self.assertIn("TODO", stale.stderr + stale.stdout, "a skeleton not filled in is refused, nothing written")
        self.assertEqual(c.find_brief(c.find_project(self.repo), tid).section("Interpretation").strip(), "")
        e["interpretation"] = GOOD_INTERP
        e["approach"] = GOOD_AP.format(pct=90)
        e["criteria"] = ["it works :: true"]
        with open(path, "w", encoding="utf-8") as f:
            json.dump([e], f)
        self.fm("task", "plan", "--file", path)
        gaps = c._content_gaps(c.find_brief(c.find_project(self.repo), tid))
        self.assertEqual(gaps, [])
        self.assertNotIn("isn't planned enough", self.fm("lane", "brief", tid, check=False).stderr)

    def test_one_round_a_plan_call_that_leaves_gaps_prints_them_all_at_once(self):
        tid = self.bare()
        path = os.path.join(self.tmp, "mine.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump([{"id": tid, "interpretation": "just one reading", "approach": "do the thing", "replan": True,
                        "criteria": ["it works :: true"], "steps": ["do it"]}], f)
        r = self.fm("task", "plan", "--file", path)
        lines = [ln for ln in r.stdout.splitlines() if ln.lstrip().startswith("- ")]
        self.assertGreaterEqual(len(lines), 3, r.stdout)
        self.assertTrue(all(len(ln) < 120 for ln in lines))
        self.assertIn("PLANS.json", r.stdout)


class RivalRead(ForemanTestCase):
    """A choice under 80% gets its cold rival read from lane brief itself, in the background, never a bare refusal."""
    def setUp(self):
        super().setUp()
        saved = os.environ.pop("FOREMAN_NO_RIVAL", None)
        self.addCleanup(lambda: os.environ.__setitem__("FOREMAN_NO_RIVAL", saved) if saved is not None
                        else os.environ.pop("FOREMAN_NO_RIVAL", None))

    def planned(self, pct=70, **kw):
        return new_task(self, interpretation=GOOD_INTERP, approach=GOOD_AP.format(pct=pct), ac="it works :: true",
                        step="do it", **kw)

    def brief(self, tid):
        p = c.find_project(self.repo)
        return p, c.find_brief(p, tid)

    def test_one_round_a_choice_under_80_starts_the_rival_read_in_the_background(self):
        tid = self.planned(70)
        p, b = self.brief(tid)
        with mock.patch.object(lanes, "_rival_spawn", return_value=True) as spawn, mock.patch.object(lanes, "RIVAL_WAIT", 0):
            with self.assertRaises(c.PolicyError) as cm:
                lanes.builder_brief(p, b, argparse.Namespace())
        spawn.assert_called_once()
        self.assertEqual(spawn.call_args.args[1].id, tid)
        said = str(cm.exception)
        self.assertIn("rival", said)
        self.assertIn("pending", said, "the read is named pending when the wait ends")
        self.assertNotIn("sampo second rival", said.split("pending")[0].split("\n")[0], "never a refusal that hands the command back")

    def test_one_round_the_rival_read_starts_beside_the_other_gaps_without_waiting(self):
        tid = new_task(self, interpretation=GOOD_INTERP, approach=GOOD_AP.format(pct=70), ac="it works", step="do it")
        p, b = self.brief(tid)
        with mock.patch.object(lanes, "_rival_spawn", return_value=True) as spawn, \
                mock.patch.object(lanes, "_rival_wait", side_effect=AssertionError("waited with gaps left")):
            with self.assertRaises(c.PolicyError) as cm:
                lanes.builder_brief(p, b, argparse.Namespace())
        spawn.assert_called_once()
        said = str(cm.exception)
        self.assertIn("verify command on criterion 1", said)
        self.assertIn("rival read started in the background", said)
        self.assertEqual(len([ln for ln in said.splitlines() if ln.startswith("- ")]), 1, "the rival is no gap line of its own")

    def test_one_round_a_choice_at_80_or_more_starts_no_read(self):
        tid = self.planned(85)
        p, b = self.brief(tid)
        self.assertEqual(c.rival_gap(b), [])
        with mock.patch.object(lanes, "_rival_spawn") as spawn:
            try:
                lanes.builder_brief(p, b, argparse.Namespace())
            except Exception:  # whatever stops it later (load, slots) is not the plan gate
                pass
        spawn.assert_not_called()

    def test_one_round_the_read_lands_in_time_and_the_brief_goes_on(self):
        tid = self.planned(70)
        p, b = self.brief(tid)

        def land(p_, tid_, secs):
            def save(x):
                x.meta["rival_at"] = c.now()
            from sampo import cli
            cli.mutate(p_, tid_, save, "rival", {})
            return True
        with mock.patch.object(lanes, "_rival_spawn", return_value=True), mock.patch.object(lanes, "_rival_wait", side_effect=land):
            try:
                lanes.builder_brief(p, b, argparse.Namespace())
            except c.PolicyError as e:
                self.assertNotIn("rival", str(e))
            except Exception:
                pass

    def test_one_round_second_rival_takes_model_local(self):
        from sampo import second
        seen = {}

        def fake(p, system, prompt, **kw):
            seen["asked"] = True
            return "## Readings\n- the gate reads every option\n## Approaches\n- print the gaps\n## Failure modes\n- none\n", "qwen"
        p, b = self.brief(self.planned(70))
        from sampo import moonscale
        with mock.patch.object(moonscale, "_ask", fake):
            lines, _split, note = second.rival(p, b, model="local", timeout=5)
        self.assertTrue(seen.get("asked"))
        self.assertTrue(c.find_brief(p, b.id).meta.get("rival_at"), note)


class RivalSpawn(ForemanTestCase):
    """T-5728 review round 1: the real claim, spawn, deadline and release, with only Popen and the model stubbed."""
    def setUp(self):
        super().setUp()
        for k in ("FOREMAN_NO_RIVAL", "FOREMAN_NO_BACKGROUND"):
            saved = os.environ.pop(k, None)
            self.addCleanup(lambda k=k, saved=saved: os.environ.__setitem__(k, saved) if saved is not None
                            else os.environ.pop(k, None))
        tid = new_task(self, interpretation=GOOD_INTERP, approach=GOOD_AP.format(pct=70), ac="it works :: true",
                       step="do it")
        self.p = c.find_project(self.repo)
        self.b = c.find_brief(self.p, tid)

    def mark(self):
        from sampo import second
        return second.rival_mark(self.p, self.b.id)

    def held(self):
        with open(self.mark(), encoding="utf-8") as f:
            return f.read().strip()

    def popen(self, pid=None):
        proc = mock.Mock()
        proc.pid = os.getpid() if pid is None else pid
        return mock.patch.object(lanes.subprocess, "Popen", return_value=proc)

    def test_one_round_two_spawns_at_once_start_one_read(self):
        import threading
        res, go = [], threading.Barrier(2)

        def one():
            go.wait()
            res.append(lanes._rival_spawn(self.p, self.b))
        with self.popen() as popen:
            ts = [threading.Thread(target=one) for _ in range(2)]
            [t.start() for t in ts]
            [t.join() for t in ts]
        self.assertEqual(popen.call_count, 1, "only the creator of the claim spawns")
        self.assertEqual(res, [True, True])
        self.assertEqual(self.held(), str(os.getpid()), "the marker names the child's pid")
        self.assertEqual(popen.call_args.kwargs["env"]["FOREMAN_RIVAL_MARK"], self.mark())

    def test_one_round_a_dead_pids_marker_is_retaken(self):
        import subprocess
        import sys
        done = subprocess.Popen([sys.executable, "-c", "pass"])
        done.wait()
        os.makedirs(os.path.dirname(self.mark()), exist_ok=True)
        with open(self.mark(), "w") as f:
            f.write(f"{done.pid}\n")
        with self.popen() as popen:
            self.assertTrue(lanes._rival_spawn(self.p, self.b))
        self.assertEqual(popen.call_count, 1)
        self.assertEqual(self.held(), str(os.getpid()))

    def test_one_round_a_live_pids_marker_holds_the_next_spawn_off_however_old(self):
        os.makedirs(os.path.dirname(self.mark()), exist_ok=True)
        with open(self.mark(), "w") as f:
            f.write(f"{os.getpid()}\n")
        os.utime(self.mark(), (1_000_000_000, 1_000_000_000))  # an old file: age alone never frees it past the ceiling...
        with mock.patch("sampo.second.MARK_MAX_S", 10 ** 12), self.popen() as popen:
            self.assertTrue(lanes._rival_spawn(self.p, self.b))
        popen.assert_not_called()

    def test_one_round_no_background_spawns_nothing(self):
        os.environ["FOREMAN_NO_BACKGROUND"] = "1"
        with self.popen() as popen:
            self.assertFalse(lanes._rival_spawn(self.p, self.b))
        popen.assert_not_called()
        self.assertFalse(os.path.exists(self.mark()))

    def test_one_round_a_failed_spawn_leaves_no_marker(self):
        with mock.patch.object(lanes.subprocess, "Popen", side_effect=OSError("no")):
            self.assertFalse(lanes._rival_spawn(self.p, self.b))
        self.assertFalse(os.path.exists(self.mark()))

    def test_one_round_the_child_removes_its_marker_even_when_the_read_fails(self):
        from sampo import second
        os.makedirs(os.path.dirname(self.mark()), exist_ok=True)
        with open(self.mark(), "w") as f:
            f.write("1\n")
        args = argparse.Namespace(what="rival", id=self.b.id, model="local", timeout=5, json=False, project=self.p.slug)
        env = mock.patch.dict(os.environ, {"FOREMAN_RIVAL_MARK": self.mark(), "FOREMAN_PROJECT": self.p.slug})
        with env, mock.patch.object(second, "rival", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                second.cmd_second(args)
        self.assertFalse(os.path.exists(self.mark()))

    def test_one_round_a_mark_env_naming_another_file_is_never_removed(self):
        from sampo import second
        other = os.path.join(self.tmp, "keep.txt")
        with open(other, "w") as f:
            f.write("x")
        args = argparse.Namespace(what="rival", id=self.b.id, model="local", timeout=5, json=False, project=self.p.slug)
        with mock.patch.dict(os.environ, {"FOREMAN_RIVAL_MARK": other, "FOREMAN_PROJECT": self.p.slug}), \
                mock.patch.object(second, "rival", return_value=([], False, "")):
            second.cmd_second(args)
        self.assertTrue(os.path.exists(other))

    def test_one_round_a_sensitive_project_with_no_local_model_sends_nothing(self):
        from sampo import ideas, moonscale, second
        self.fm("sensitive", "on")
        with mock.patch.object(moonscale, "_locals", return_value=[]), \
                mock.patch.object(second, "_child", side_effect=AssertionError("a child was asked")) as child, \
                mock.patch.object(ideas, "run_child", side_effect=AssertionError("sent to a model")) as sent:
            _lines, _split, note = second.rival(self.p, self.b, model="local", timeout=5)
        child.assert_not_called()
        sent.assert_not_called()
        self.assertIn("no rival read", note)
        self.assertIn("local model", note)

    def test_one_round_the_read_has_one_deadline_split_between_local_and_the_child(self):
        from sampo import moonscale, second
        seen, t0 = {}, __import__("time").monotonic()

        def ask(p, system, prompt, **kw):
            seen["deadline"] = kw.get("deadline")
            raise ValueError("no local model answered")

        def child(p, feature, system, prompt, model, timeout):
            seen["child"] = timeout
            return "## Readings\n- a specific reading\n## Approaches\n- a specific approach\n## Failure modes\n- none\n"
        with mock.patch.object(moonscale, "_ask", ask), mock.patch.object(second, "_child", child):
            second.rival(self.p, self.b, model="local", timeout=100)
        self.assertLessEqual(seen["deadline"] - t0, 51,"the local model gets at most half")
        self.assertGreater(seen["child"], 40)
        self.assertLessEqual(seen["child"], 100 - 0, "the child gets what is left of the one deadline")

    def test_one_round_a_past_deadline_asks_no_further_local_model(self):
        import time
        from sampo import models, moonscale
        with mock.patch.object(moonscale, "_locals", return_value=[{"name": "x"}, {"name": "y"}]), \
                mock.patch.object(models, "complete", side_effect=AssertionError("asked past the deadline")) as comp:
            with self.assertRaises(Exception):
                moonscale._ask(self.p, "s", "u", writer="local", deadline=time.monotonic() - 1)
        comp.assert_not_called()

    def test_one_round_each_local_model_gets_what_is_left_of_the_deadline(self):
        import time
        from sampo import models, moonscale
        got = []

        def complete(prof, msgs, **kw):
            got.append(kw["timeout"])
            raise models.ModelError("down")
        with mock.patch.object(moonscale, "_locals", return_value=[{"name": "x"}, {"name": "y"}]), \
                mock.patch.object(models, "complete", complete):
            with self.assertRaises(Exception):
                moonscale._ask(self.p, "s", "u", writer="local", timeout=500, deadline=time.monotonic() + 30)
        self.assertTrue(got and all(0 < t <= 30.5 for t in got), got)


class SkeletonFiles(ForemanTestCase):
    def test_one_round_each_task_gets_its_own_skeleton_file(self):
        a, b = new_task(self, ac="it works", step="do it"), new_task(self, ac="it works", step="do it")
        path = os.path.join(self.tmp, "mine.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump([{"id": t, "interpretation": "just one reading", "approach": "do the thing", "replan": True,
                        "criteria": ["it works :: true"], "steps": ["do it"]} for t in (a, b)], f)
        out = self.fm("task", "plan", "--file", path).stdout
        paths = re.findall(r"(\S*\.PLANS\.json)", out)
        self.assertEqual(sorted(set(os.path.basename(x) for x in paths)), sorted(f"{t}.PLANS.json" for t in (a, b)), out)
        self.assertNotIn(" PLANS.json", out.replace("\n", " ").replace("/", " "), "no shared file")
        for x in set(paths):
            with open(x, encoding="utf-8") as f:
                self.assertEqual(len(json.load(f)), 1, "one task per file")


class WhichFindsIt(ForemanTestCase):
    def test_one_round_which_finds_task_plan_file_for_plan_in_one_call(self):
        for q in ("fill a whole plan in one call", "plan several tasks in one call"):
            out = self.fm("which", q).stdout
            self.assertIn("task plan --file", out, q)


if __name__ == "__main__":
    unittest.main()
