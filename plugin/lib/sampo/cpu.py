"""T-3592: this session's CPU, measured. The user's goal (2026-11-06): ~100 subagents plus the merge queue at most 10%
of the machine, so the PC stays usable. 10% is what every efficiency change is measured against, not a limit.

`sampo cpu [--window S]` reads the session scope's cgroup v2 cpu.stat (usage_usec: every process the session ran,
short-lived hooks and tests included) at both ends of the window: cores used and % of the machine's cores. Sampling
/proc for the scope's processes every quarter second splits it by kind; what no scan saw is shown as unsampled. It
shows the scope's cpu.max too. The self-check (selfcheck.cpu_goal) keeps (time, usage) samples per scope in
state/cpu.json and reports how far the average since the newest sample 10+ minutes old is over the goal (default 10%;
`sampo cpu goal PCT` per project), with the top consumer from a half-second scan made only then.

`sampo cpu cap PCT|off` (optional; Sampo never sets it) puts CPUQuota on the session's own scope with
`systemctl --user set-property --runtime` (gone when the scope ends) and records it; the guard files it under system,
as it does a typed set-property. Linux with systemd user sessions only, and only a .scope (or a .service other than
user@N) under the user manager. FOREMAN_CGROUP names the folder the meter reads (tests); the cap never reads it."""
import json
import os
import re
import shutil
import subprocess
import time

from sampo import core as c

TICK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
GOAL = 10.0  # % of the machine's cores
WINDOW_S, KEEP_S, EVERY_S = 600, 1800, 60  # the average's span; samples kept; at most one sample a minute
SCAN_S = 0.5  # ponytail: the top consumer is what runs in this half second, not over the 10 minutes
# Kinds that own their descendants, matched on the nearest process up the parent chain; then the process's own kind
OWNERS = (("tests", re.compile(r"plugin/tests/|\bunittest\b|\bpytest\b|\bplugin test\b|\b(?:fm|sampo) check\b")),
          ("hooks", re.compile(r"/hooks/hook\b")),
          ("fm ui", re.compile(r"/hooks/(?:subagent-)?statusline\b|\b(?:fm|sampo) (?:ui|watch)\b")),
          ("merge queue", re.compile(r"\b(?:fm|sampo) (?:lane (?:train|merge|land)|ship)\b")))


def ncores():
    return os.cpu_count() or 1


def scope():
    """The scope folder the meter reads: FOREMAN_CGROUP (tests), else this process's own."""
    return os.environ.get("FOREMAN_CGROUP") or own_scope()


def own_scope():
    """The cgroup v2 folder this process runs in, or None (no cgroup v2: not Linux, or v1 only)."""
    try:
        with open("/proc/self/cgroup") as f:
            for line in f:
                if line.startswith("0::"):
                    return "/sys/fs/cgroup" + line[3:].strip()
    except OSError:
        pass
    return None


def _read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def usage(d):
    """cpu.stat usage_usec, or None."""
    m = re.search(r"^usage_usec (\d+)", _read(os.path.join(d, "cpu.stat")) or "", re.M)
    return int(m.group(1)) if m else None


def limit(text):
    """Cores cpu.max allows ("quota period"), None for max or unreadable."""
    try:
        quota, period = text.split()
        return int(quota) / int(period)
    except (AttributeError, ValueError, ZeroDivisionError):
        return None


def _scan(d, proc, facts, pids=None):
    """{(pid, start): (own ticks, reaped children's ticks)} for the scope's processes now (or for pids: T-3659's Board
    reads the whole machine); facts gets what each is."""
    out = {}
    for pid in (_read(os.path.join(d, "cgroup.procs")) or "").split() if pids is None else pids:
        s = _read(os.path.join(proc, pid, "stat"))
        if not s:
            continue  # gone
        comm, rest = s[s.find("(") + 1:s.rfind(")")], s[s.rfind(")") + 2:].split()
        key = (pid, rest[19])
        out[key] = (int(rest[11]) + int(rest[12]), int(rest[13]) + int(rest[14]))
        if key not in facts:
            info = {"ppid": rest[1], "comm": comm,
                    "cmd": " ".join((_read(os.path.join(proc, pid, "cmdline")) or "").split("\0")).strip()}
            for link in ("cwd", "exe"):
                try:
                    info[link] = os.readlink(os.path.join(proc, pid, link))
                except OSError:
                    info[link] = ""
            facts[key] = info
    return out


def kind(pid, table):
    """The nearest owner kind up the parent chain, else this process's own: Claude itself, git, builders' commands
    (run in a lane), other."""
    q, seen = pid, set()
    while q in table and q not in seen:
        seen.add(q)
        for name, rx in OWNERS:
            if rx.search(table[q]["cmd"]):
                return name
        q = table[q]["ppid"]
    me = table[pid]
    if me["comm"] == "claude" or "/claude/versions/" in me["exe"]:
        return "Claude itself"
    if me["comm"] == "git":
        return "git"
    return "builders' commands" if ".lanes/" in me["cwd"] + " " + me["cmd"] else "other"


def sample(d, secs, proc="/proc", every=0.25, sleep=time.sleep, clock=time.monotonic):
    """The scope's CPU over secs: {cores, pct, kinds: {kind: cores}, unsampled, secs}. A process first seen after the
    first scan started inside the window, so all its CPU counts. A child that lived between two scans shows up in its
    parent's reaped-children ticks (cutime + cstime), less the children seen and gone: counted as the parent's kind,
    Claude's own as short-lived under Claude (hooks, the status line, quick commands)."""
    facts, first, last, now, scans = {}, {}, {}, {}, 0
    t0, u0 = clock(), usage(d)
    end = t0 + secs
    while True:
        now = _scan(d, proc, facts)
        for k, v in now.items():
            first.setdefault(k, (0, 0) if scans else v)
        last.update(now)
        scans += 1
        if clock() >= end:
            break
        sleep(min(every, end - clock()))
    elapsed, u1 = clock() - t0, usage(d)
    if u0 is None or u1 is None:
        return None
    if elapsed <= 0:
        return {"cores": None, "pct": None, "kinds": {}, "unsampled": None, "secs": 0}
    bypid = {k[0]: info for k, info in facts.items()}  # a reused pid: its newest process
    gone = {}
    for k in last.keys() - now.keys():  # seen, then reaped: its whole life lands in the parent's reaped ticks
        gone[facts[k]["ppid"]] = gone.get(facts[k]["ppid"], 0) + sum(last[k])
    kinds = {}
    for k, (own, reaped) in last.items():
        name = kind(k[0], bypid)
        kinds[name] = kinds.get(name, 0) + own - first[k][0]
        rest = reaped - first[k][1] - gone.get(k[0], 0)
        if rest > 0:
            name = "short-lived under Claude" if name == "Claude itself" else name
            kinds[name] = kinds.get(name, 0) + rest
    kinds = dict(sorted(((k, v / TICK / elapsed) for k, v in kinds.items() if v > 0), key=lambda kv: -kv[1]))
    cores = (u1 - u0) / 1e6 / elapsed
    return {"cores": cores, "pct": cores / ncores() * 100, "kinds": kinds,
            "unsampled": max(0.0, cores - sum(kinds.values())), "secs": elapsed}


def top(got):
    """(kind, cores) using the most, the unsampled rest counted as one."""
    rows = dict(got["kinds"], **({"short-lived (unsampled)": got["unsampled"]} if got["unsampled"] else {}))
    return max(rows.items(), key=lambda kv: kv[1]) if rows else None


def _state():
    path = os.path.join(c.state_dir(), "cpu.json")
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return path, d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return path, {}


def remember(d, now, used):
    """Adds (now, used) to the scope's samples (one a minute at most); returns the newest one WINDOW_S or more old."""
    path, st = _state()
    every = st.get("samples") if isinstance(st.get("samples"), dict) else {}
    rows = [r for r in every.get(d) or [] if isinstance(r, list) and len(r) == 2 and 0 <= now - r[0] <= KEEP_S
            and r[1] <= used]  # a counter that went back is another scope's
    old = max((r for r in rows if now - r[0] >= WINDOW_S), default=None, key=lambda r: r[0])
    if not rows or now - rows[-1][0] >= EVERY_S:
        rows.append([now, used])
        st["samples"] = {k: v for k, v in every.items() if k != d and v and now - v[-1][0] <= KEEP_S}
        st["samples"][d] = rows
        try:
            c.write_atomic(path, json.dumps(st))
        except OSError:
            pass
    return old


def goal(p):
    try:
        return float(c.read_meta(p).get("cpu_goal") or GOAL)
    except (TypeError, ValueError, OSError):
        return GOAL


def goal_line(p, now=None):
    """The self-check: None under the goal or without 10 minutes of samples; else how far over, and the top consumer."""
    d = scope()
    used = usage(d) if d else None
    if used is None:
        return None
    now = time.time() if now is None else now
    old = remember(d, now, used)
    if not old:
        return None
    cores = (used - old[1]) / 1e6 / (now - old[0])
    pct, g = cores / ncores() * 100, goal(p)
    if pct <= g:
        return None
    got = sample(d, SCAN_S)
    t = got and top(got)
    return (f"CPU: this session averaged {cores:.1f} cores, {pct:.0f}% of the machine's {ncores()}, over "
            f"{(now - old[0]) / 60:.0f} min: {pct - g:.0f} points over the {g:g}% goal"
            + (f"; top now: {t[0]} {t[1]:.1f} cores" if t else "") + " → sampo cpu")


def _pct(text, what):
    from sampo import cli
    try:
        v = float(text)
    except (TypeError, ValueError):
        v = None
    if v is None or not 0 < v <= 100:
        raise cli.UsageError(f"{what} is a percent of the machine's {ncores()} cores, above 0 and at most 100 "
                             f"(sampo cpu {what} 10 is {ncores() / 10:g} cores)")
    return v


def suite_trend(path, n=7):
    """T-3757: [(day, CPU s)] the test suite costs, estimated per day from the baseline the merge train and the
    post-merge verify keep (every test class's usual, summed), the last n days; [] when there is none."""
    try:
        with open(path, encoding="utf-8") as f:
            days = json.load(f).get("days") or {}
        return [(str(k), round(float(v), 1)) for k, v in sorted(days.items())][-n:]
    except (OSError, ValueError, AttributeError, TypeError):
        return []


def cap(pct):
    """CPUQuota on this session's scope (pct of the machine; None lifts it), recorded in state/cpu.json."""
    d = own_scope()  # never FOREMAN_CGROUP: the cap is for the scope this process is in, nothing else
    unit = os.path.basename(d or "")
    # a scope (or a service that isn't the user manager): a slice or user@N.service would throttle every app
    if not d or "/user@" not in d or not (unit.endswith(".scope") or unit.endswith(".service")
                                          and not unit.startswith("user@")):
        raise c.PolicyError(f"sampo cpu cap needs a session scope that systemd user manages: this one's cgroup is "
                            f"{d or 'unknown (no cgroup v2 here)'}; nothing was set")
    exe = shutil.which("systemctl")
    if not exe:
        raise c.PolicyError("sampo cpu cap needs systemctl (systemd user sessions); nothing was set")
    quota = f"{max(1, round(pct * ncores()))}%" if pct else ""
    r = subprocess.run([exe, "--user", "set-property", "--runtime", unit, f"CPUQuota={quota}"],
                       capture_output=True, text=True, timeout=30)
    if r.returncode:
        raise c.PolicyError(f"systemctl --user set-property {unit} failed: {(r.stderr or r.stdout).strip()[:300]}")
    path, st = _state()
    st["cap"] = {"pct": pct, "unit": unit, "scope": d, "quota": quota, "at": c.now()} if pct else None
    c.write_atomic(path, json.dumps(st))
    return st["cap"]


def cmd_cpu(args):
    from sampo import cli
    if args.action == "goal":
        p = cli.resolve(args)
        if args.value is None:
            return cli.out(args, {"goal_pct": goal(p)}, f"goal: {goal(p):g}% of the machine")
        c.update_meta(p, cpu_goal=_pct(args.value, "goal"))
        return cli.out(args, {"goal_pct": goal(p)}, f"goal: {goal(p):g}% of the machine's {ncores()} cores")
    if args.action == "cap":
        if args.value is None:
            raise cli.UsageError("sampo cpu cap PCT (a percent of the machine) or sampo cpu cap off")
        rec = cap(None if args.value == "off" else _pct(args.value, "cap"))
        return cli.out(args, {"cap": rec}, f"cap: {rec['pct']:g}% of the machine (CPUQuota={rec['quota']} on "
                                           f"{rec['unit']}, until the session ends)" if rec else "cap: off")
    d = scope()
    got = sample(d, max(0.0, args.window)) if d else None
    if got is None:
        raise c.PolicyError(f"sampo cpu reads cgroup v2 CPU accounting, which this session hasn't "
                            f"({d or 'no /proc/self/cgroup'}): Linux with a systemd session only")
    try:
        p = cli.resolve(args, create=False)
        g, trend = goal(p), suite_trend(os.path.join(p.dir, "test-cpu.json"))
    except Exception:  # noqa: BLE001 — outside a project the default goal
        g, trend = GOAL, []
    cpu_max = (_read(os.path.join(d, "cpu.max")) or "").strip()
    rec = _state()[1].get("cap")
    rec = rec if isinstance(rec, dict) and rec.get("scope") == d else None
    data = dict(got, scope=d, machine_cores=ncores(), goal_pct=g, cpu_max=cpu_max, limit_cores=limit(cpu_max), cap=rec,
                tests_trend=[{"day": k, "cpu_s": v} for k, v in trend])
    lines = [f"{os.path.basename(d)}: " + (
        f"{got['cores']:.2f} cores, {got['pct']:.1f}% of the machine's {ncores()} over {got['secs']:.1f} s "
        f"({'over' if got['pct'] > g else 'within'} the {g:g}% goal)" if got["cores"] is not None
        else "not measured (--window 0)")]
    lines.append("limit: " + (f"{limit(cpu_max):g} cores (cpu.max {cpu_max})" if limit(cpu_max) else "none (cpu.max max)")
                 + (f", set by sampo cpu cap {rec['pct']:g} at {rec['at']}" if rec else ""))
    rows = list(got["kinds"].items()) + ([("short-lived (unsampled)", got["unsampled"])] if got["unsampled"] else [])
    lines += [f"  {k:<24} {v:5.2f} cores" for k, v in rows]
    if trend:  # T-3757: a cost that creeps in under the train's margins shows here day by day
        lines.append(f"test suite: about {trend[-1][1]:,.0f} CPU s a full run ("
                     + " · ".join(f"{k[5:]} {v:,.0f}" for k, v in trend) + ")")
    return cli.out(args, data, "\n".join(lines))
