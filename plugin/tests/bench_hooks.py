#!/usr/bin/env python3
"""Hook latency benchmark: runs every fixture payload through hooks/hook N times in a scratch project.

Usage: bench_hooks.py [--runs N] [--json]. Exits 1 if any hook's p95 exceeds its budget (BUILD_PROMPT §2.8).
Also imported by `fm doctor`.
"""
import json
import os
import resource
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
HOOK = os.path.join(PLUGIN, "hooks", "hook")
FM = os.path.join(PLUGIN, "bin", "fm")


def script_budget():
    """T-3132: the guard's script box, read by a child (-I), so the tree's code never runs in this timing process
    (a branch's fmguard could patch resource.getrusage or statistics.median and fake its own timings)."""
    code = "import sys; sys.path.insert(0, sys.argv[1]); import fmguard; print(fmguard._script_budget())"
    try:
        r = subprocess.run([sys.executable, "-I", "-c", code, os.path.join(PLUGIN, "lib")],
                           capture_output=True, text=True, timeout=30)
        v = float(r.stdout.strip().splitlines()[-1])
        return min(v, 5 * 0.2) if 0 < v < float("inf") else 0.2  # clamped: a branch can't loosen its own limit (as the guard's: T-4824)
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return 0.2  # the guard's default box


# T-1777: the script case is a hook plus the guard's CPU time box for one cold script read (an upper bound: T-4758, the
# bench's script now finishes inside it), so its budget is a hook's plus the box (300 left ~85 ms for startup, which load ate)
BUDGET_MS = {"default": 150, "MessageDisplay": 100}
BUDGET_MS["PreToolUse:Bash:script"] = BUDGET_MS["default"] + round(script_budget() * 1000)
# T-1192: a real-shaped build script (functions, case, quoting, substitutions, a heredoc) the guard reads when a command
# runs it (T-0275), rewritten before every run, so each run is a cold read, not a cached one. T-4758: a script the guard
# cannot read to the end inside its budgets is refused (exit 2); its read costs ~40 ms CPU a block (0.6 KB; it was
# quadratic: 8 blocks 1.9 s, 64 blocks 44 s, now 0.3 s and 2.5 s), and the case's latency budget is a hook's plus the
# guard's first budget (script_budget(), 0.2 s), so the case is the 4 blocks that fit in it: a script the guard reads
# to the end and allows, which is what doctor's "hook exit codes" expects (exit 0)
SCRIPT_BLOCKS = 4
SCRIPT_BLOCK = r'''
build_{n}() {{
  local target="${{1:-all}}" out="$BUILD_DIR/{n}"
  case "$target" in
    all|"") echo "building {n} into $out" ;;
    clean) rm -rf "$out" ;;
    *) printf 'unknown target: %s\n' "$target" >&2; return 1 ;;
  esac
  mkdir -p "$out" && cp -r "src/mod{n}" "$out/" 2>/dev/null || true
  for f in $(ls "$out" 2>/dev/null); do [ -f "$out/$f" ] && sha256sum "$out/$f" >> "$out/SUMS"; done
  cat > "$out/meta.json" <<EOF
{{"name": "mod{n}", "built": "$(date -u +%FT%TZ)", "target": "$target"}}
EOF
  VERSION="$(git describe --tags 2>/dev/null || echo 0.0.{n})" && export VERSION
}}
'''


def load_per_core(cgroup="/proc/self/cgroup", root="/sys/fs/cgroup"):
    """The machine's load over the cores this process may use: T-4018, a cgroup capped at 4 of 16 cores runs
    throttled at a machine load of 0.46 a core, so its cpu.max (the tightest up the tree) is the divisor."""
    cores = os.cpu_count() or 1
    try:
        with open(cgroup, encoding="utf-8") as f:
            path = f.read().strip().partition("::")[2] or "/"  # cgroup v2's one line
    except OSError:
        path = None
    while path:
        try:
            with open(os.path.join(root, path.lstrip("/"), "cpu.max"), encoding="utf-8") as f:
                quota, period = f.read().split()
            cores = min(cores, int(quota) / int(period)) if quota != "max" else cores
        except (OSError, ValueError, ZeroDivisionError):
            pass
        path = None if path == "/" else os.path.dirname(path)
    try:
        return os.getloadavg()[0] / max(cores, 0.01)
    except OSError:
        return 0.0


def within(wall_p95, cpu_p95, budget, load, cpu_p50=None, pressure=0.0):
    """T-0805: a quiet machine judges wall time (what the user waits); a busy one (load per core above 1) judges the
    hook's own CPU time, which a code regression (more imports, more work) raises. T-3821: by its median when given:
    load inflates CPU time's tail too (one commit at load 2.3 per core: SessionStart CPU p95 404, then 106 ms).
    T-4029: CPU pressure (the share of the last 10 s some task waited for a CPU) at PRESSURE_BUSY or more is busy too:
    the 1-minute load lags, and read under 1 a core at 51% pressure while the land stopped on a red base bench."""
    busy = cpu_p50 if cpu_p50 is not None else cpu_p95
    return (busy if load > 1 or pressure >= PRESSURE_BUSY else wall_p95) <= budget


PRESSURE_BUSY = 10.0  # percent; an idle desktop reads 0-2


def stage_ok(res, budget, sum_p50=None):
    """T-4950: (ok, why) for a stage_bench result. The stage clock is the hook's own wall time, so on a busy machine
    (within()'s rule: load per core over 1 or CPU pressure at PRESSURE_BUSY) it counts the waits for a CPU too: at load
    40-76 on 16 cores it turned lane checks red with no code cause. There the hook process's CPU p99 is judged instead,
    which a code regression still raises; a quiet machine judges the stage clock's total p99. `sum_p50` bounds state +
    parse + check at p50, judged only when quiet."""
    busy = res["load"] > 1 or res["pressure"] >= PRESSURE_BUSY
    total, cpu = res["stages"]["total"]["p99"], res["cpu"]["p99"]
    if not within(total, cpu, budget, res["load"], pressure=res["pressure"]):
        return False, (f"hook CPU p99 {cpu} ms over {budget} ms (busy machine: load {res['load']}, pressure "
                       f"{res['pressure']}%)" if busy else f"stage clock total p99 {total} ms over {budget} ms")
    if sum_p50 is not None and not busy:
        got = sum(res["stages"][k]["p50"] for k in ("state", "parse", "check"))
        if got >= sum_p50:
            return False, f"state + parse + check stage p50 {got:.1f} ms, not under {sum_p50} ms"
    return True, ""


def cpu_pressure(cgroup="/proc/self/cgroup", root="/sys/fs/cgroup", system="/proc/pressure/cpu"):
    """T-4029: PSI's 'some avg10' for CPU, the higher of this process's cgroup and the machine's; 0 without PSI."""
    files = [system]
    try:
        with open(cgroup, encoding="utf-8") as f:
            files.append(os.path.join(root, (f.read().strip().partition("::")[2] or "/").lstrip("/"), "cpu.pressure"))
    except OSError:
        pass
    got = 0.0
    for path in files:
        try:
            with open(path, encoding="utf-8") as f:
                some = next(ln for ln in f if ln.startswith("some "))
            got = max(got, float(some.split("avg10=")[1].split()[0]))
        except (OSError, StopIteration, IndexError, ValueError):
            pass
    return got


def _scratch_project(tmp):
    repo = os.path.join(tmp, "app")
    os.makedirs(os.path.join(repo, "src"))
    # T-4344: no background work, whatever the caller's environment: the measured UserPromptSubmit prompt is long
    # enough to start `fm exam steer expand` (a paid model call, recorded only in this scratch state, which is deleted)
    env = dict(os.environ, FOREMAN_HOME=os.path.join(tmp, "fhome"), FOREMAN_NO_BACKGROUND="1")
    git_env = dict(env, GIT_AUTHOR_NAME="b", GIT_AUTHOR_EMAIL="b@x", GIT_COMMITTER_NAME="b", GIT_COMMITTER_EMAIL="b@x")
    for cmd in (["git", "init", "-q", "-b", "main", repo], ["git", "-C", repo, "commit", "-q", "--allow-empty", "-m", "i"]):
        subprocess.run(cmd, check=True, env=git_env)

    def fm(*a):
        subprocess.run([sys.executable, FM, *a], cwd=repo, env=env, check=True, capture_output=True)
    fm("task", "new", "Benchmark task", "--type", "FIX", "--tier", "S", "--scope", "src/**")
    fm("task", "step", "T-0001", "add", "reproduce")
    fm("task", "step", "T-0001", "add", "fix")
    fm("task", "ac", "T-0001", "add", "works")
    fm("focus", "T-0001")
    for i in range(10):
        fm("task", "new", f"queued {i}", "--type", "CLEAN", "--tier", "S")
        fm("capture", f"idea {i}")
    _write_script(repo, 0)
    return repo, env


def _write_script(repo, run):
    """The build script, new bytes each run (a comment carries the run number), so the guard reads it cold."""
    with open(os.path.join(repo, "src", "build.sh"), "w") as f:
        f.write(f"#!/bin/bash\n# run {run}\nset -euo pipefail\nBUILD_DIR=${{BUILD_DIR:-build}}\n"
                + "".join(SCRIPT_BLOCK.format(n=n) for n in range(SCRIPT_BLOCKS)) + 'build_0 "$@"\n')


def run_bench(runs=30, only=None):  # only: a predicate on fixture names (T-2359 times the guard's alone)
    with open(os.path.join(HERE, "fixtures", "hook_payloads.json")) as f:
        fixtures = json.load(f)
    results = {}
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        repo, env = _scratch_project(tmp)
        env["CLAUDE_ENV_FILE"] = os.path.join(tmp, "envfile")
        for name, payload in fixtures.items():
            if only and not only(name):
                continue
            event = name.split(":")[0]
            body = json.dumps(dict({"session_id": "bench", "cwd": repo, "hook_event_name": event},
                                   **json.loads(json.dumps(payload).replace("{cwd}", repo))))
            times, cpus, codes = [], [], set()
            for i in range(runs):
                if name.endswith(":script"):
                    _write_script(repo, i)
                t0, r0 = time.perf_counter(), resource.getrusage(resource.RUSAGE_CHILDREN)
                p = subprocess.run([HOOK, event], input=body, capture_output=True, text=True, env=env, cwd=repo)
                r1 = resource.getrusage(resource.RUSAGE_CHILDREN)
                times.append((time.perf_counter() - t0) * 1000)
                cpus.append((r1.ru_utime - r0.ru_utime + r1.ru_stime - r0.ru_stime) * 1000)
                codes.add(p.returncode)
            times.sort()
            cpus.sort()
            budget = BUDGET_MS.get(name, BUDGET_MS.get(event, BUDGET_MS["default"]))
            at = min(len(times) - 1, int(len(times) * 0.95))
            p95, cpu95, load, pressure = times[at], cpus[at], load_per_core(), cpu_pressure()
            results[name] = {"p50": round(statistics.median(times), 1), "p95": round(p95, 1), "cpu_p95": round(cpu95, 1),
                             "cpu_p50": round(statistics.median(cpus), 1),  # T-3131: the gate's steady basis
                             "budget": budget, "load": round(load, 2), "pressure": pressure,
                             "ok": within(p95, cpu95, budget, load, statistics.median(cpus), pressure),
                             "exit_codes": sorted(codes)}
    return results


ONE_LINE = "cd {repo} && df -h / | tail -n 1"  # T-4780: the short command whose PreToolUse hook the harness timed out


def _pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * q))] if xs else 0.0


STAGES = ("start", "imports", "state", "parse", "check", "rules", "asks", "snapshot", "after", "total")


def stage_summary(rows):
    """T-4824: {stage: {p50, p99, max}} over the hook's stage rows (ms). A stage no row carries is "n/a", not 0: a 0.0
    reads as a measured time, and the stage was not measured at all."""
    stages = {}
    for k in STAGES:
        xs = [r[k] for r in rows if isinstance(r.get(k), (int, float))]
        if xs:
            stages[k] = {"p50": round(statistics.median(xs), 1), "p99": round(_pct(xs, 0.99), 1), "max": round(max(xs), 1)}
        else:
            stages[k] = {"p50": "n/a", "p99": "n/a", "max": "n/a"}
    return stages


def fixed_tmp_dir():
    """T-4780: a fresh folder under /tmp: the hook writes its bench files only under fixed temp roots (never TMPDIR)."""
    return tempfile.mkdtemp(prefix="sampo-bench-", dir="/tmp" if os.path.isdir("/tmp") else None)


def _stop_group(proc):
    """End a child started with start_new_session and everything it started (a run.py's class subprocesses)."""
    import killguard  # T-5900: a stubbed Popen's pid must never reach killpg(1)
    killguard.install()
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except OSError:
            break
        try:
            proc.wait(timeout=30 if sig == signal.SIGTERM else 10)
            break
        except subprocess.TimeoutExpired:
            continue


def stage_bench(runs=200, command=ONE_LINE, entry="fast", beside=None):
    """T-4780: per-stage wall time (ms) of the PreToolUse hook for one short command, `runs` calls one after the other
    through the entry Claude Code runs (hooks/fast), in a scratch project. The hook writes its own stage clock rows
    (SAMPO_HOOK_STAGES); wall is timed here around the process. `beside`: test module names; a real
    `python3 plugin/tests/run.py <those>` runs while the calls are made (started here, stopped here when the calls are
    done; only the calls made while it ran are counted, so the figures are the hook's beside a test run and nothing
    else: no load of our own is made up). Returns {"wall", "cpu": {p50, p99, max}, "stages": {name: {p50, p99, max}},
    "runs" (counted), "load", "pressure"}."""
    test = None
    sink_dir = fixed_tmp_dir()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        repo, env = _scratch_project(tmp)
        sink = os.path.join(sink_dir, "stages.jsonl")
        env["SAMPO_HOOK_STAGES"] = sink
        body = json.dumps({"session_id": "bench", "cwd": repo, "hook_event_name": "PreToolUse", "tool_name": "Bash",
                           "tool_input": {"command": command.format(repo=repo)}})
        if beside:
            # its own session, so the whole group can be stopped; SAMPO_SLOT=1 makes it a nested run (run.py never
            # waits on, or takes, the machine's real test slots for it)
            test = subprocess.Popen([sys.executable, os.path.join(HERE, "run.py"), *beside], cwd=os.path.dirname(PLUGIN),
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                                    env=dict(os.environ, SAMPO_SLOT="1"))
        wall, cpu, counted, load, pressure = [], [], [], 0.0, 0.0
        try:
            for _ in range(runs):
                t0, r0 = time.perf_counter(), resource.getrusage(resource.RUSAGE_CHILDREN)
                subprocess.run([os.path.join(PLUGIN, "hooks", entry), "PreToolUse"], input=body, capture_output=True, text=True,
                               env=env, cwd=repo)
                r1 = resource.getrusage(resource.RUSAGE_CHILDREN)
                wall.append((time.perf_counter() - t0) * 1000)
                cpu.append((r1.ru_utime - r0.ru_utime + r1.ru_stime - r0.ru_stime) * 1000)
                counted.append(test is None or test.poll() is None)
                if len(wall) % 20 == 0:
                    load, pressure = max(load, load_per_core()), max(pressure, cpu_pressure())
        finally:
            if test is not None:
                _stop_group(test)  # also when it had finished: its leftover children
        try:
            with open(sink, encoding="utf-8") as f:
                rows = [json.loads(ln) for ln in f if ln.strip()]
        except OSError:
            rows = []
        shutil.rmtree(sink_dir, ignore_errors=True)
    if len(rows) == len(counted):  # one row per call, in order
        rows = [r for r, c in zip(rows, counted) if c]
    wall, cpu = [x for x, c in zip(wall, counted) if c], [x for x, c in zip(cpu, counted) if c]
    out = {"runs": len(wall), "stage_rows": len(rows), "load": round(max(load, load_per_core()), 2),
           "pressure": max(pressure, cpu_pressure()), "stages": stage_summary(rows)}
    for name, xs in (("wall", wall), ("cpu", cpu)):
        out[name] = {"p50": round(statistics.median(xs), 1) if xs else 0.0, "p99": round(_pct(xs, 0.99), 1),
                     "max": round(max(xs), 1) if xs else 0.0}
    return out


def hook_modules(only=None):
    """T-3139: {fixture: [plugin files the hook's process imported]}, one untimed run per fixture, the hook entry
    dumping its sys.modules into the file SAMPO_HOOK_MODULES names (the gate's map of which diffs can slow a hook)."""
    with open(os.path.join(HERE, "fixtures", "hook_payloads.json")) as f:
        fixtures = json.load(f)
    got = {}
    sink_dir = fixed_tmp_dir()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        repo, env = _scratch_project(tmp)
        env["CLAUDE_ENV_FILE"] = os.path.join(tmp, "envfile")
        env["FOREMAN_NO_BACKGROUND"] = "1"  # T-3241: imports only; a late background writer raced the cleanup
        for name, payload in fixtures.items():
            if only and not only(name):
                continue
            event = name.split(":")[0]
            body = json.dumps(dict({"session_id": "bench", "cwd": repo, "hook_event_name": event},
                                   **json.loads(json.dumps(payload).replace("{cwd}", repo))))
            out = os.path.join(sink_dir, "modules.jsonl")
            if name.endswith(":script"):
                _write_script(repo, 0)
            for _ in range(2):  # state-gated lazy imports show on a later run
                subprocess.run([HOOK, event], input=body, capture_output=True, text=True, cwd=repo, timeout=120,
                               env=dict(env, SAMPO_HOOK_MODULES=out))
            files = set()
            if os.path.isfile(out):
                with open(out, encoding="utf-8") as fh:
                    for ln in fh:
                        files.update(json.loads(ln))
                os.remove(out)
            got[name] = sorted(files)
    shutil.rmtree(sink_dir, ignore_errors=True)
    return got


def injection_sizes():
    """Chars SessionStart and UserPromptSubmit inject in a scratch project (fm doctor's budget; T-2358: fm ship's
    record measures the shipped tree's hooks through --inject)."""
    import tempfile
    sizes = {}
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        repo, env = _scratch_project(tmp)
        env["CLAUDE_ENV_FILE"] = os.path.join(tmp, "envfile")
        for event, extra in (("SessionStart", {"source": "startup"}),
                             ("UserPromptSubmit", {"prompt": "FIX: a\nFEATURE!: b\nCLEAN?: c\nCONTEXT: d"})):
            payload = json.dumps(dict({"session_id": "doctor", "cwd": repo, "hook_event_name": event}, **extra))
            try:  # a hook that can't start or hangs measures 0, as fm doctor's own run had it
                out = subprocess.run([HOOK, event], input=payload, env=env, cwd=repo, capture_output=True, text=True,
                                     timeout=120).stdout
                sizes[event] = len(json.loads(out)["hookSpecificOutput"]["additionalContext"])
            except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
                sizes[event] = 0
    return sizes


def main():
    if "--modules" in sys.argv:  # T-3139
        print(json.dumps(hook_modules()))
        return 0
    if "--stages" in sys.argv:  # T-4780: the one-line command's per-stage wall time
        mods = sys.argv[sys.argv.index("--beside") + 1:] if "--beside" in sys.argv else None  # modules to run beside
        print(json.dumps(stage_bench(int(sys.argv[sys.argv.index("--runs") + 1]) if "--runs" in sys.argv else 200,
                                     beside=mods)))
        return 0
    runs = int(sys.argv[sys.argv.index("--runs") + 1]) if "--runs" in sys.argv else 30
    res = run_bench(runs)
    if "--inject" in sys.argv:  # T-2358: fm ship's footprint record, both in one JSON
        print(json.dumps({"bench": res, "injected": injection_sizes()}))
    elif "--json" in sys.argv:
        print(json.dumps(res, indent=2))
    else:
        print(f"{'hook':<24}{'p50 ms':>8}{'p95 ms':>8}{'budget':>8}  exit")
        for k, v in res.items():
            print(f"{k:<24}{v['p50']:>8}{v['p95']:>8}{v['budget']:>8}  {v['exit_codes']}{'' if v['ok'] else '  OVER BUDGET'}")
        busy = max((v["load"] for v in res.values()), default=0)
        psi = max((v["pressure"] for v in res.values()), default=0)
        if busy > 1 or psi >= PRESSURE_BUSY:
            print(f"machine busy (load {busy:.1f} per core, CPU pressure {psi:.0f}%): judged by each hook's CPU time, "
                  "not wall time (T-0805, T-4029)")
    return 0 if all(v["ok"] for v in res.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
