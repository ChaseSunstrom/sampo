#!/usr/bin/env python3
"""T-3272 (Moon C-001150): one table of speed promises for the hot CLI verbs and the hook budgets, and the check that holds them.

A verb's promise is a max ratio of its child CPU time to bare python's, measured in the same run. CPU, not wall time: on a
busy box a 100-300 ms verb queues behind the runnable tasks while bare python barely does, so wall ratios flake; the CPU
a child burns moves with the work it does (more imports, more code) and barely with the load. The hook half is
bench_hooks' own budgets (BUDGET_MS), read at call time, never copied here.

Two windows decide. The first round of each check is warmup and is dropped. A row over its promise is measured again in
a window nine runs long, and fails only if it is still over there.

Usage: speed_promises.py [--runs N]. Exits 1 when a promise breaks in both windows, printing its numbers.
"""
import contextlib
import os
import resource
import statistics
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import bench_hooks  # noqa: E402

BARE = "bare python"  # the key of the same-run baseline (a bare interpreter start)
BARE_ARGV = [sys.executable, "-c", "pass"]
SCRATCH_DROP = ("FOREMAN_PROJECT", "FOREMAN_STATE", "FOREMAN_SESSION_ID", "CLAUDE_CODE_SESSION_ID")
SECOND_RUNS = 9  # the second window, for the rows over their promise in the first

# argv follows `fm` in the scratch project bench_hooks builds (T-0001 is focused there). Each max_ratio is ~2x the worst
# CPU ratio of three measured runs on the loaded shared box (load 100-250): next 8.6-9.7x, task show 8.2-8.3x,
# state 8.9-9.6x, which 21.1-21.8x.
PROMISES = {
    "sampo next": {"argv": ["next"], "max_ratio": 20},
    "sampo task show": {"argv": ["task", "show", "T-0001"], "max_ratio": 17},
    "sampo state": {"argv": ["state"], "max_ratio": 20},
    "sampo which": {"argv": ["which", "check"], "max_ratio": 44},
}
# path -> the bench_hooks fixture it times; "Stop" takes bench_hooks' default budget, "MessageDisplay" its own
HOOK_PROMISES = {"hook default": "Stop", "hook MessageDisplay": "MessageDisplay"}
HOOK_BUDGETS = ("default", "MessageDisplay")  # keys of bench_hooks.BUDGET_MS the hook promises read


def hook_budgets():
    """The hook half of the table: bench_hooks' budgets in ms, read now (a change there moves these)."""
    return {f"hook {k}": bench_hooks.BUDGET_MS[k] for k in HOOK_BUDGETS}


def scratch_env(env, home):
    """The env a verb runs in: the developer's session and project pointers dropped, detached writers off (they outlive
    the temp dir), HOME at the scratch so `which` reads no ~/.claude.json and no skills."""
    return dict({k: v for k, v in env.items() if k not in SCRATCH_DROP}, HOME=home, FOREMAN_NO_BACKGROUND="1")


@contextlib.contextmanager
def scrubbed_environ(home):
    """os.environ replaced by scratch_env for the block; bench_hooks' children inherit it."""
    saved = dict(os.environ)
    os.environ.clear()
    os.environ.update(scratch_env(saved, home))
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def scratch_project(tmp):
    """The scratch project the verbs run in. Its setup runs scrubbed too, so the developer's project is never touched."""
    with scrubbed_environ(tmp):
        repo, env = bench_hooks._scratch_project(tmp)
    return repo, scratch_env(env, tmp)


def run_cpu(argv, cwd, env, timeout=120):
    """Run argv to the end; return (the child's CPU ms, CompletedProcess). RUSAGE_CHILDREN counts only the children this
    process has waited for, so a detached writer that outlives the call is not charged to the verb."""
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    p = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    return (after.ru_utime - before.ru_utime + after.ru_stime - before.ru_stime) * 1000, p


def check(promises, runs, timer):
    """One row per promise. Each round times bare python, then every verb, so a load swing reaches them all alike. The
    first round is warmup and is dropped; the median of the `runs` measured calls is taken per path, and the ratio is the
    verb's median over the same run's bare median. timer(key) -> CPU ms for key BARE or a promise name."""
    samples = {key: [] for key in (BARE, *promises)}
    for rnd in range(runs + 1):
        for key in samples:
            ms = timer(key)
            if rnd:
                samples[key].append(ms)
    bare = statistics.median(samples[BARE])
    rows = []
    for name, p in promises.items():
        med = statistics.median(samples[name])
        ratio = med / bare if bare > 0 else float("inf")
        rows.append({"path": name, "median_cpu_ms": round(med, 1), "bare_cpu_ms": round(bare, 1), "ratio": ratio,
                     "max_ratio": p["max_ratio"], "ok": ratio <= p["max_ratio"], "window": 1})
    return rows


def hook_check(runs=5, labels=None):
    """Hook rows: bench_hooks.run_bench times each fixture in its own scratch project, and each row is judged against its
    budget here (bench_hooks.within: wall p95, or CPU p95 on a busy box). labels: the paths to time (default all)."""
    want = [p for p in HOOK_PROMISES if labels is None or p in labels]
    fixtures = {HOOK_PROMISES[p] for p in want}
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as home, scrubbed_environ(home):
        res = bench_hooks.run_bench(runs, only=lambda name: name in fixtures)
    budgets = hook_budgets()
    rows = []
    for p in want:
        r = res[HOOK_PROMISES[p]]
        budget = budgets[p]
        rows.append({"path": p, "p95_ms": r["p95"], "cpu_p95_ms": r["cpu_p95"], "budget_ms": budget, "load": r["load"],
                     "ok": bench_hooks.within(r["p95"], r["cpu_p95"], budget, r["load"]), "window": 1})
    return rows


def settle(rows, measure):
    """The two-window decision: the rows over their promise are measured again, SECOND_RUNS long, and fail only if they
    are still over. measure(names, runs) returns fresh rows for those paths; the rest keep the first window's."""
    failing = [r["path"] for r in rows if not r["ok"]]
    if not failing:
        return rows
    again = {r["path"]: dict(r, window=2) for r in measure(failing, SECOND_RUNS)}
    return [again.get(r["path"], r) for r in rows]


def report(rows):
    """The paths over their promise, each with its numbers; empty when every promise holds."""
    out = []
    for r in rows:
        if r["ok"]:
            continue
        if "budget_ms" in r:
            out.append(f"{r['path']}: p95 {r['p95_ms']:g} ms over its {r['budget_ms']:g} ms budget")
        else:
            out.append(f"{r['path']}: {r['ratio']:.1f}x bare python (promise {r['max_ratio']:g}x; "
                       f"median {r['median_cpu_ms']:g} ms CPU vs bare {r['bare_cpu_ms']:g} ms)")
    return "\n".join(out)


def live_timer(repo, env, promises):
    """A timer that runs the real verb in the scratch project. A verb that fails is an error, not a fast pass."""
    def timer(key):
        argv = BARE_ARGV if key == BARE else [sys.executable, bench_hooks.FM, *promises[key]["argv"]]
        cpu, p = run_cpu(argv, repo, env)
        if p.returncode:
            raise RuntimeError(f"{key} exited {p.returncode} in the scratch project: {p.stderr[-300:]}")
        return cpu
    return timer


def main():
    runs = max(1, int(sys.argv[sys.argv.index("--runs") + 1])) if "--runs" in sys.argv else 5
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        repo, env = scratch_project(tmp)
        timer = live_timer(repo, env, PROMISES)
        rows = settle(check(PROMISES, runs, timer), lambda names, n: check({k: PROMISES[k] for k in names}, n, timer))
    rows += settle(hook_check(runs), lambda labels, n: hook_check(n, labels))
    for r in rows:
        mark = "ok" if r["ok"] else "OVER"
        if "budget_ms" in r:
            print(f"{r['path']:<24}p95 {r['p95_ms']:>8.1f} ms  budget {r['budget_ms']:g} ms  {mark}  window {r['window']}")
        else:
            print(f"{r['path']:<24}{r['ratio']:>8.1f}x  promise {r['max_ratio']:g}x  {mark}  window {r['window']}")
    bad = report(rows)
    if bad:
        print(bad)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
