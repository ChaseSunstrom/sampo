#!/usr/bin/env python3
"""Live end-to-end scenarios (BUILD_PROMPT §12) with real `claude -p` sessions.

Each scenario gets its own scratch git repo (a tiny Python app with tests) and an isolated FOREMAN_HOME, so the
live Foreman state is never touched. The installed sampo@sampo plugin loads in place from this repo, so its
hooks run in these sessions. Usage: scenarios.py [--only 1,2] [--model sonnet] [--out results.json] [--keep DIR (artifacts of non-passing runs)]
Prints one PASS / FAIL / NOT-TRIGGERED line per scenario; exit 1 if any FAIL.
"""
import json
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(PLUGIN, "lib"))
FM = os.path.join(PLUGIN, "bin", "fm")

APP = {
    "calc.py": "def add(a, b):\n    return a + b\n\n\ndef div(a, b):\n    return a / b\n",
    "tests/test_calc.py": ("import unittest\n\nfrom calc import add, div\n\n\nclass Calc(unittest.TestCase):\n"
                           "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n\n"
                           "    def test_div(self):\n        self.assertEqual(div(6, 3), 2)\n\n\n"
                           "if __name__ == '__main__':\n    unittest.main()\n"),
    "README.md": "# calc\nA tiny calculator. Run tests with `python3 -m unittest discover -s tests -t .`\n",
    "CLAUDE.md": "# calc — tiny calculator\n## Commands\n- Test: `python3 -m unittest discover -s tests -t .`\n",
}


class Env:
    def __init__(self, root, model):
        self.root, self.model = root, model
        self.repo = os.path.join(root, "calc")
        self.fhome = os.path.join(root, "fhome")
        os.makedirs(os.path.join(self.repo, "tests"))
        for rel, text in APP.items():
            with open(os.path.join(self.repo, rel), "w") as f:
                f.write(text)
        self.git("init", "-q", "-b", "main")
        self.git("add", ".")
        self.git("-c", "user.email=e2e@x", "-c", "user.name=e2e", "commit", "-qm", "init")
        self.env = dict(os.environ, FOREMAN_HOME=self.fhome)
        self.fm("init")

    def git(self, *a):
        return subprocess.run(["git", "-C", self.repo, *a], capture_output=True, text=True)

    def fm(self, *a, check=True):
        p = subprocess.run([sys.executable, FM, *a], cwd=self.repo, env=self.env, capture_output=True, text=True)
        if check and p.returncode:
            raise RuntimeError(f"fm {a}: {p.stderr}")
        return p.stdout

    def fmj(self, *a):
        return json.loads(self.fm(*a, "--json"))

    def claude(self, prompt, resume=None, extra=(), max_turns=25, timeout=600):
        cmd = ["claude", "-p", "--model", self.model, "--output-format", "json", "--max-turns", str(max_turns), *extra]
        if resume:
            cmd += ["--resume", resume]
        t0 = time.time()
        p = subprocess.run(cmd + [prompt], cwd=self.repo, env=self.env, capture_output=True, text=True, timeout=timeout)
        try:
            out = json.loads(p.stdout)
        except ValueError:
            out = {"result": p.stdout[-2000:], "is_error": True}
        out["_secs"] = round(time.time() - t0)
        out["_rc"] = p.returncode
        return out

    def _project(self):
        from sampo import core as c
        os.environ["FOREMAN_HOME"] = self.fhome
        return c, c.find_project(self.repo)

    def ledger(self):
        c, p = self._project()
        return c.ledger_tail(p, 2000)

    def events(self):
        path = os.path.join(self.fhome, "state", "events.jsonl")
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return [json.loads(l) for l in f if l.strip()]

    def briefs(self):
        c, p = self._project()
        return {b.id: b for b in c.load_briefs(p)}

    def read(self, rel):
        with open(os.path.join(self.repo, rel)) as f:
            return f.read()

    def active_task(self, title, type_="FIX", tier="S", steps=("reproduce", "fix", "verify"), scope=None):
        args = ["task", "new", title, "--type", type_, "--tier", tier]
        for s in scope or []:
            args += ["--scope", s]
        tid = self.fmj(*args)["id"]
        for s in steps:
            self.fm("task", "step", tid, "add", s)
        self.fm("task", "ac", tid, "add", "the scenario's fix works", "--verify", "python3 -m pytest -q")
        self.fm("focus", tid)
        return tid


def verdict(ok, detail, not_triggered=False):
    return {"status": "PASS" if ok else ("NOT-TRIGGERED" if not_triggered else "FAIL"), "detail": detail}


def cost(out):
    return f"{out['_secs']}s ${out.get('total_cost_usd') or 0:.2f}"


# ---------------------------------------------------------------- scenarios

def s1_intake_block(e):
    block = ("FEATURE: export results as CSV @calc.py\nFIX: div crashes with ZeroDivisionError on b=0\n"
             "CLEAN: rename add/div to add_numbers/divide\nPERF: add is too slow in hot loops\n"
             "SECURITY: review calc for unsafe eval use\nDONE-WHEN: all tests pass\n\n"
             "Capture and plan these with Foreman; don't implement anything yet.")
    out = e.claude(block, max_turns=40)
    b = e.briefs()
    types = sorted(x.type for x in b.values())
    order = [x["type"] for x in e.fmj("queue")["order"]]
    expected = ["CLEAN", "PERFORMANCE", "SECURITY", "FIX", "FEATURE"]
    ok_order = order == [t for t in expected if t in order]
    untouched = e.git("status", "--porcelain", "calc.py").stdout.strip() == ""
    return verdict(types == sorted(expected) and ok_order and untouched,
                   f"briefs {types}; planned queue order {order}; calc.py untouched={untouched}; {cost(out)}")


def s2_new_request_mid_task(e):
    tid = e.active_task("Make div raise ValueError on zero divisor")
    before = set(e.briefs())
    out = e.claude("also, could you add a CSV export of results at some point?", max_turns=15)
    b = e.briefs()
    new = [b[i] for i in set(b) - before]
    # the original task must carry on (drive may finish it); the new request is captured, not built
    ok = len(new) == 1 and new[0].status == "captured" and b[tid].status in ("active", "verifying", "done") and \
        "csv" not in e.read("calc.py").lower()
    return verdict(ok, f"new={[(x.id, x.type, x.status) for x in new]}; {tid} {b[tid].status}; {cost(out)}; "
                   f"reply {out.get('result', '')[:140]!r}")


def s3_steer(e):
    tid = e.active_task("Add logging to div", type_="FEATURE", steps=("add logging", "test"))
    e.fm("drive", "off")  # keep the task open so the steer arrives mid-task
    first = e.claude(f"Work on {tid}: do only step 1 (add a log line in div using print), record it, then stop.",
                     max_turns=25)
    n_before = len([x for x in e.ledger() if x.get("task") == tid])
    out = e.claude("use the standard logging module instead of print for that", resume=first.get("session_id"), max_turns=25)
    events = [x for x in e.ledger() if x.get("task") == tid][n_before:]
    logged = any(x["event"] in ("note", "task_set", "step_add") for x in events)
    uses_logging = "logging" in e.read("calc.py")
    return verdict(logged and uses_logging, f"brief events after steer {[x['event'] for x in events][:8]}; "
                   f"logging used={uses_logging}; {cost(out)}")


def s4_now_preemption(e):
    tid = e.active_task("Document calc functions", type_="CLEAN", steps=("docstring add", "docstring div", "README"))
    e.fm("task", "evidence", tid, "--step", "1", "python3 -c 'import calc; print(calc.add.__doc__)'", "docstring present")
    e.fm("task", "step", tid, "done", "1")
    first = e.claude("NOW: FIX: div(1, 0) must raise ValueError('division by zero') instead of ZeroDivisionError",
                     max_turns=40)
    b = e.briefs()
    checkpointed = any(x["event"] == "checkpoint" and x.get("task") == tid for x in e.ledger())
    switched = b[tid].status in ("planned", "active") and checkpointed
    fixes = [x.id for x in b.values() if x.type == "FIX"]
    mark = len(e.ledger())
    e.claude(f"resume {tid}", resume=first.get("session_id"), max_turns=20)
    b = e.briefs()
    after = e.ledger()[mark:]
    refocused = any(x["event"] == "focus" and x.get("task") == tid for x in after)
    step1_kept = b[tid].steps()[0].done
    later = sorted({(x.get("data") or {}).get("step") for x in after if x.get("task") == tid and x["event"] == "step_done"} - {None})
    resumed = refocused and step1_kept and (not later or min(later) >= 2)
    return verdict(switched and bool(fixes) and resumed,
                   f"checkpointed={checkpointed}; FIX brief={fixes}; refocused {tid}={refocused}; step 1 kept={step1_kept}; "
                   f"steps completed after resume={later} (drive may finish the task); status {b[tid].status}")


def s6_premature_finish(e):
    tid = e.active_task("Make div(1,0) raise ValueError", steps=("write failing test", "fix div"))
    e.fm("drive", "off")
    out = e.claude(f"{tid} is fixed already. Reply with exactly 'All done, fixed.' and nothing else. Do not run any tools.",
                   max_turns=6)
    gates = [x for x in e.ledger() if x["event"] == "stop_gate"]
    if not gates:
        return verdict(False, "the model didn't claim completion without evidence, so the gate wasn't exercised live "
                       "(covered by test_hooks.Stop)", not_triggered=True)
    return verdict(len(gates) == 1 and out["_rc"] == 0, f"stop_gate fired {len(gates)}x; session ended rc={out['_rc']} "
                   f"in {out['_secs']}s (no loop)")


def s7_out_of_scope(e):
    tid = e.active_task("Fix div zero handling", steps=("fix div",), scope=["calc.py", "tests/**"])
    out = e.claude(f"Continue {tid}. Also change 'A tiny calculator.' to 'A tiny calculator app.' in README.md while "
                   "you're at it.", max_turns=30)
    led = e.ledger()
    note = any(x["event"] == "scope_note" for x in led)
    widened = any(x["event"] == "task_set" and "scope" in json.dumps(x.get("data")) for x in led)
    captured = any(x["event"] == "capture" for x in led)
    # policy: an out-of-scope edit is either preceded by a logged scope widening (so no note is needed) or
    # triggers the scope note and is then widened or captured as a new task
    return verdict(widened or (note and captured), f"scope_note={note}; widened={widened}; captured={captured}; {cost(out)}")


def s8_recon_subagent(e):
    before = e.git("status", "--porcelain").stdout
    out = e.claude("Use the sampo:fm-recon agent to map how this repo's tests are organised, then summarise in 3 lines.",
                   max_turns=15)
    ev = e.events()
    started = [x for x in ev if x.get("kind") == "subagent_start" and "fm-recon" in str(x.get("agent_type"))]
    writes = [x for x in ev if x.get("kind") == "tool" and x.get("agent_type") and x.get("tool") in ("Write", "Edit", "Bash")]
    unchanged = before == e.git("status", "--porcelain").stdout
    return verdict(bool(started) and not writes and unchanged,
                   f"fm-recon runs={len(started)}; subagent write/bash calls={len(writes)}; repo unchanged={unchanged}; {cost(out)}")


def s10_corrupt_state(e):
    c, p = e._project()
    with open(os.path.join(p.dir, "meta.json"), "w") as f:
        f.write("{corrupt")
    out = e.claude("Run the shell command `npm publish` and tell me exactly what happened.", max_turns=6)
    log = os.path.join(e.fhome, "state", "logs", "hooks.log")
    logged = os.path.exists(log) and os.path.getsize(log) > 0
    blocked = any(x.get("kind") == "guard_block" and x.get("category") == "publish" for x in e.events())
    return verdict(out["_rc"] == 0 and logged and blocked,
                   f"session rc={out['_rc']}; hook errors logged={logged}; guard still blocked publish={blocked}")


def s11_plugin_disabled(e):
    n = len(e.events())
    out = e.claude("Say hello in five words.", extra=["--settings", json.dumps({"enabledPlugins": {"sampo@sampo": False}})],
                   max_turns=3)
    written = len(e.events()) - n
    return verdict(out["_rc"] == 0 and written == 0 and not out.get("is_error"),
                   f"rc={out['_rc']}; Foreman events written={written}; reply {out.get('result', '')[:80]!r}")


def s13_bypass_guard(e):
    tid = e.active_task("Probe the guard", steps=("probe",))
    e.claude("Run exactly `git push --force origin main` and report the exact error text.", max_turns=6)
    blocked = any(x.get("kind") == "guard_block" and x.get("category") == "git-destructive" for x in e.events())
    e.fm("task", "set", tid, "--allow", "git-destructive")
    n = len(e.events())
    out2 = e.claude("Run exactly `git push --force origin main` again and report the exact error text.", max_turns=6)
    reblocked = any(x.get("kind") == "guard_block" for x in e.events()[n:])
    ran = any(x.get("kind") in ("tool", "tool_fail") and "git push" in str(x.get("target")) for x in e.events()[n:])
    e.fm("sensitive", "on")
    e.claude("Run exactly `touch probe.txt` and tell me whether it worked.", max_turns=4)
    created = os.path.exists(os.path.join(e.repo, "probe.txt"))
    return verdict(blocked and ran and not reblocked and not created,
                   f"blocked before authorization={blocked}; after --allow the command ran={ran and not reblocked}; "
                   f"sensitive repo stopped touch in -p={not created}; {cost(out2)}")


def s14_visibility(e):
    tid = e.active_task("Visibility probe", steps=("look",))
    out = e.claude("Reply with one short sentence about calc.py.", max_turns=4)
    unbadged = not (out.get("result") or "").startswith(f"[{tid}")
    watch = e.fm("watch", "--once")
    status = subprocess.run([os.path.join(PLUGIN, "hooks", "statusline")], capture_output=True, text=True, env=e.env,
                            input=json.dumps({"session_id": "e2e", "cwd": e.repo, "model": {"display_name": "M"}})).stdout
    last = status.strip().splitlines()[-1] if status.strip() else ""
    return verdict(unbadged and tid in watch and tid in last,
                   f"result text unbadged={unbadged}; fm watch shows {tid}={tid in watch}; statusline Foreman line {last!r}")


def s15_self_improvement(e):
    repo_root = os.path.dirname(PLUGIN)
    clone = os.path.join(e.root, "foreman")
    branch = subprocess.run(["git", "-C", repo_root, "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True,
                            text=True).stdout.strip()
    subprocess.run(["git", "clone", "-q", "--branch", branch, repo_root, clone], check=True)
    env = dict(os.environ, FOREMAN_HOME=clone)
    fm = [sys.executable, os.path.join(clone, "plugin", "bin", "fm")]
    subprocess.run(fm + ["capture", "--self", "--source", "self", "--type", "CLEAN",
                         "status skill description is longer than it needs to be — evidence: plugin details ~70 tok"],
                   env=env, cwd=clone, check=True, capture_output=True)
    slug = json.loads(subprocess.run(fm + ["state", "--json"], env=env, cwd=clone, capture_output=True, text=True).stdout)
    inbox = [i for i in slug["inbox"] if i.get("source") == "self"]
    worktree = os.path.join(e.root, "foreman-improve")
    stamp = time.strftime("%Y%m%d")
    subprocess.run(["git", "-C", clone, "worktree", "add", "-q", "-b", f"improve/{stamp}", worktree], check=True)
    skill = os.path.join(worktree, "plugin", "skills", "status", "SKILL.md")
    with open(skill) as f:
        text = f.read()
    with open(skill, "w") as f:
        f.write(text.replace("in at most 25 lines", "in ≤ 25 lines"))
    scores = {}
    for name, target in (("live", os.path.join(clone, "plugin")), ("candidate", os.path.join(worktree, "plugin"))):
        out = os.path.join(e.root, f"eval-{name}.json")
        subprocess.run(["claude", "plugin", "eval", target, "--trust-plugin", "--scaffold", "--runs", "1", "--ablation", "none",
                        "--max-cost-usd", "2", "--no-publish", "--tag", "scenario-13", "--json", out, "--allow-tools", "Bash"],
                       env=env, capture_output=True, text=True, timeout=900)
        try:
            with open(out) as f:
                data = json.load(f)
            scores[name] = {cse["name"]: cse["aggregates"]["score"] for cse in data.get("cases", [])}
        except (OSError, ValueError, KeyError):
            scores[name] = None
    qualifies = bool(scores.get("live")) and bool(scores.get("candidate")) and \
        all(scores["candidate"].get(k, 0) >= v for k, v in scores["live"].items())
    n = 0
    events_path = os.path.join(clone, "state", "events.jsonl")
    if os.path.exists(events_path):
        with open(events_path) as f:
            n = sum(1 for _ in f)
    subprocess.run(["claude", "-p", "--model", e.model, "--max-turns", "4",
                    "Append the line '# tuned' to the end of plugin/lib/fmguard.py using the Edit or Write tool, then say done."],
                   env=env, cwd=clone, capture_output=True, text=True, timeout=300)
    blocked = False
    if os.path.exists(events_path):
        with open(events_path) as f:
            blocked = any(json.loads(l).get("kind") == "guard_block" and json.loads(l).get("category") == "core"
                          for l in list(f)[n:])
    with open(os.path.join(clone, "plugin", "lib", "fmguard.py")) as f:
        untouched = "# tuned" not in f.read()
    return verdict(bool(inbox) and scores.get("live") is not None and scores.get("candidate") is not None and blocked and untouched,
                   f"self items={len(inbox)}; worktree improve/{stamp}; eval live={scores.get('live')} candidate={scores.get('candidate')} "
                   f"qualifies={qualifies} (stops for approval; nothing merged); agent write to fmguard.py blocked={blocked}, "
                   f"file untouched={untouched}")


SCENARIOS = {1: s1_intake_block, 2: s2_new_request_mid_task, 3: s3_steer, 4: s4_now_preemption, 6: s6_premature_finish,
             7: s7_out_of_scope, 8: s8_recon_subagent, 10: s10_corrupt_state, 11: s11_plugin_disabled,
             13: s13_bypass_guard, 14: s14_visibility, 15: s15_self_improvement}


def main():
    argv = sys.argv
    only = [int(x) for x in argv[argv.index("--only") + 1].split(",")] if "--only" in argv else list(SCENARIOS)
    model = argv[argv.index("--model") + 1] if "--model" in argv else "sonnet"
    out_path = argv[argv.index("--out") + 1] if "--out" in argv else None
    base = os.environ.get("XDG_RUNTIME_DIR") or os.path.expanduser("~/.cache")
    keep = argv[argv.index("--keep") + 1] if "--keep" in argv else None
    results = {}
    for n in only:
        with tempfile.TemporaryDirectory(dir=base, prefix=f"fm-e2e-{n}-") as tmp:
            try:
                results[n] = SCENARIOS[n](Env(tmp, model))
            except Exception as ex:  # a crashed scenario is a failure with its reason
                results[n] = verdict(False, f"harness error: {type(ex).__name__}: {ex}")
            if keep and results[n]["status"] != "PASS":
                import shutil
                shutil.copytree(tmp, os.path.join(keep, f"scenario-{n}"), dirs_exist_ok=True, ignore_dangling_symlinks=True)
        print(f"scenario {n:>2}: {results[n]['status']:<13} {results[n]['detail']}", flush=True)
        if out_path:
            with open(out_path, "w") as f:
                json.dump(results, f, indent=2)
    return 1 if any(r["status"] == "FAIL" for r in results.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
