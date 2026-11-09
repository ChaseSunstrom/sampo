#!/usr/bin/env python3
"""Sampo's CPU bench (T-3252): what a CLI call costs and how test runs share the machine, before and after a change.

Usage: perf_bench.py [--runners N] [--modules M ...] [--calls N] [--note TEXT] [--out FILE]

Measures (a JSON line appended to research/perf/bench.jsonl unless --out names another file):
  cli_ms       median wall ms of each verb in a scratch FOREMAN_HOME (n = --calls), and bare python for scale
  alone_s      one run.py over the modules
  together_s   N run.py over the same modules at once (each its own -j default), wall time of the slowest
  load_peak    the highest 1-minute load seen while they ran, and the CPU count
Compare rows from the same machine and a similar starting load (load_start is recorded).
"""
import argparse
import datetime
import json
import os
import statistics
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
FM = os.path.join(REPO, "plugin", "bin", "fm")
VERBS = (("task", "show", "T-0001"), ("which", "commit"), ("state",), ("capture", "bench idea", "--type", "FIX",
                                                                           "--tier", "S"))
MODULES = ("test_core", "test_guard", "test_capture", "test_hooks")


def load1():
    return os.getloadavg()[0]  # portable: macOS has no /proc/loadavg


def cli_ms(calls):
    home = tempfile.mkdtemp(prefix="perf-bench-")  # a throwaway project: the verbs do their real work, on nothing real
    env = dict(os.environ, FOREMAN_HOME=os.path.join(home, "fhome"), FOREMAN_INHIBITED="1")
    repo = os.path.join(home, "repo")
    os.makedirs(repo)
    subprocess.run(["git", "init", "-q", repo], check=True)
    subprocess.run([sys.executable, FM, "init"], cwd=repo, env=env, capture_output=True)
    subprocess.run([sys.executable, FM, "task", "new", "Seed", "--type", "FIX", "--tier", "S", "--ac", "ok :: true",
                    "--step", "s"], cwd=repo, env=env, capture_output=True)
    out = {}
    for verb in [("-c", "pass")] + list(VERBS):
        argv = [sys.executable, *verb] if verb[0] == "-c" else [sys.executable, FM, *verb]
        times = []
        for _ in range(calls):
            t = time.perf_counter()
            subprocess.run(argv, capture_output=True, env=env, cwd=repo)
            times.append((time.perf_counter() - t) * 1000)
        out["python" if verb[0] == "-c" else " ".join(verb[:2])] = round(statistics.median(times), 1)
    return out


def suite(modules, runners):
    """Wall seconds of the slowest of `runners` run.py over the modules, all started together, and the peak load."""
    peak, done = [load1()], threading.Event()

    def watch():
        while not done.wait(0.5):
            peak.append(load1())

    w = threading.Thread(target=watch, daemon=True)
    w.start()
    t = time.perf_counter()
    procs = [subprocess.Popen([sys.executable, os.path.join(HERE, "run.py"), *modules], cwd=HERE,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for _ in range(runners)]
    codes = [p.wait() for p in procs]
    wall = time.perf_counter() - t
    done.set()
    return round(wall, 1), max(peak), codes


def group_cpu(pgid):
    """CPU seconds (user + system) every live process of one process group has used so far."""
    tick, total = os.sysconf("SC_CLK_TCK"), 0
    for d in os.listdir("/proc"):
        if d.isdigit():
            try:
                with open(f"/proc/{d}/stat") as f:
                    rest = f.read().rsplit(")", 1)[1].split()  # after "pid (comm)": state is rest[0]
            except OSError:
                continue
            if int(rest[2]) == pgid:
                total += int(rest[11]) + int(rest[12])
    return total / tick


def browser_arm(shared, pages, tmp):
    """CPU seconds `pages` page loads cost: A (shared False) launches a browser per page, B shares one."""
    sys.path.insert(0, os.path.join(REPO, "plugin", "lib"))
    from sampo import shot
    path = shot.chrome()
    page = "file://" + os.path.join(tmp, "p.html")
    with open(os.path.join(tmp, "p.html"), "w") as f:
        f.write("<h1>bench</h1>")
    cpu, b = 0.0, None
    try:
        for _ in range(pages):
            b = shot.shared(path, shot.SIZE) if shared else shot.Browser(path, shot.SIZE)
            shot.load(b, page)
            if not shared:
                cpu += group_cpu(b.pid)
            b.close()
        if shared:
            cpu = group_cpu(shot._SHARED[path].pid)
    finally:
        shot.close_shared()
    return round(cpu, 3)


def abab(pages, reps, bar=10):
    """Browser per page (A) against one shared browser (B), run A B A B ... `reps` times each: both lists and a
    verdict by the medians: won when B costs `bar` % less CPU, lost when `bar` % more, noise between."""
    tmp, a, b = tempfile.mkdtemp(prefix="perf-abab-"), [], []
    for _ in range(reps):
        a.append(browser_arm(False, pages, tmp))
        b.append(browser_arm(True, pages, tmp))
    ma, mb = statistics.median(a), statistics.median(b)
    verdict = "won" if mb <= ma * (1 - bar / 100) else "lost" if mb >= ma * (1 + bar / 100) else "noise"
    return {"kind": "abab browser", "order": "ABAB", "pages": pages, "bar_pct": bar, "a": "a browser per page",
            "b": "one shared browser, a fresh context per page", "a_cpu_s": a, "b_cpu_s": b,
            "saved_pct": round(100 * (ma - mb) / ma, 1) if ma else 0.0, "verdict": verdict}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--runners", type=int, default=4)
    p.add_argument("--modules", nargs="+", default=list(MODULES))
    p.add_argument("--calls", type=int, default=9)
    p.add_argument("--note", default="")
    p.add_argument("--out", default=os.path.join(REPO, "research", "perf", "bench.jsonl"))
    p.add_argument("--abab", choices=["browser"], help="run that A/B change ABAB instead of the suite bench")
    p.add_argument("--pages", type=int, default=20)
    p.add_argument("--reps", type=int, default=3)
    a = p.parse_args(argv)
    if a.abab:
        row = {"at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "note": a.note,
               "cpus": os.cpu_count(), "load_start": load1(), **abab(a.pages, a.reps)}
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
        print(json.dumps(row, indent=1))
        return 0
    row = {"at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "note": a.note,
           "cpus": os.cpu_count(), "load_start": load1(), "cli_ms": cli_ms(a.calls), "modules": a.modules}
    row["alone_s"], row["alone_load_peak"], codes1 = suite(a.modules, 1)
    row["together_s"], row["load_peak"], codes = suite(a.modules, a.runners)
    row["runners"], row["green"] = a.runners, not any(codes1 + codes)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
    print(json.dumps(row, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
