"""T-3659: the Board's facts, the live page's fourteen panels. collect(p) is the snapshot's "board" field: one part per
mechanism (lanes and their leased files, the merge train, the machine's CPU by kind, the task in focus, the card and
moon library, the sampo commands run here, the depends_on graph, the state folder, the test modules; since T-3731 the
live sessions, the week's asks, the last push and its CI, the usage tanks and the guard's gate). Each part is
cheap (a slow one is cached), soft (one that raises reads None, the rest stand) and plain: every string it returns
passes redact, the home-folder and private-term redaction, and a width cap, so the page shows no secret and no
machine layout. board.js maps the field (with the rest of the snapshot) to the panels."""
import hashlib
import json
import os
import re
import threading
import time

from sampo import core as c
from sampo import cpu
from sampo import live
from sampo.watch import COMMANDS_MAX, commands as part_commands  # T-4008: the pane's poll reads it too

FILES_MAX, MINUTES_MAX, LANDED_MAX, TREE_MAX = 24, 30, 8, 12
KINDS = ("tests", "claude", "models", "hooks", "other")
HOT_S = 600  # a leased file written in the last ten minutes is hot
TICK_S = 10  # the CPU ring reads /proc at most this often
DAY_S = 86400
PARKED = {"conflict", "refused"}  # lane_stop kinds that park a lane until someone acts
GROWS = {"cards_grow": "cards", "moons_grow": "moons"}
MODELS = re.compile(r"ollama|llama-server|llama_server|\bvllm\b|\bjeff\b", re.I)  # local model servers and their runners
SLOW = {"train": 30, "library": 30, "state": 10, "deps": 5, "tests": 10, "commands": 3,
        "sessions": 2, "asks": 10, "ship": 10, "usage": 10, "guard": 5}  # seconds a part is kept


def _terms():
    return live._cached(("private_terms",), live.SLOW_S, c.private_terms_lenient)  # T-4804: a display, never a refusal


def _t(s, n):
    """s as one plain, redacted line of at most n characters."""
    return c.fit(c.redact_local(c.redact(c.plain(" ".join(str(s or "").split()))), _terms()), n)


def _procs():
    """[(pid, command line)] as the CPU ring last saw them (at most TICK_S old): its scan reads each process's command
    line once in its life, so the Board adds no /proc read of its own."""
    with _RING_LOCK:
        return [(int(k[0]), f["cmd"]) for k, f in _RING["facts"].items()]


def _ls(d):
    try:
        return os.listdir(d)
    except OSError:
        return []


def _epoch(ts):
    at = c.parse_ts(ts)
    return at.timestamp() if at else None


# ---------------------------------------------------------------- the parts: each takes the project

def part_lanes(p):
    """Each builder lane (open, or merged in the last day): its state and the files its leases hold, hot ones (written
    in the last HOT_S) first, at most FILES_MAX of them."""
    from sampo import bus
    from sampo import lanes as lanes_mod
    briefs, now = live._briefs(p), time.time()
    try:
        done = set(c.finished_lanes(p, briefs, quiet_min=0))
    except Exception:
        done = set()
    held = {}
    for v in bus._leases().values():
        if isinstance(v, dict) and v.get("project") == p.slug and v.get("file"):
            held.setdefault(v.get("task"), set()).add(str(v["file"]).partition("::")[0])
    out = []
    for b in briefs:
        lane = b.meta.get("lane")
        if not lane:
            continue
        kind, at, _ = lanes_mod._stop(b)
        if b.status in c.CLOSED and not (kind == "merged" and now - at < DAY_S):
            continue
        state = ("merged" if kind == "merged" else "parked" if kind in PARKED
                 else "waiting" if b.id in done or b.meta.get("builder_returned") else "building")
        files = []
        for path in held.get(b.id, ()):
            try:
                hot = now - os.stat(os.path.join(str(lane), path)).st_mtime < HOT_S
            except (OSError, ValueError):
                hot = False
            files.append({"path": _t(path, 160), "hot": hot})
        files.sort(key=lambda f: (not f["hot"], f["path"]))
        steps = b.steps()
        out.append({"task": b.id, "title": _t(b.title, 80), "state": state, "files": files[:FILES_MAX],
                    "more": max(0, len(files) - FILES_MAX),
                    "steps": {"done": sum(s.done for s in steps), "total": len(steps)}})
    return out


def part_train(p):
    """The last day's merges: the lanes landed (newest LANDED_MAX), how many trains carried them, the minutes from a
    builder's launch to its merge, the lanes a red train left out, and whether a train runs now."""
    titles = {b.id: b.title for b in live._briefs(p)}
    built, waits, trains, landed, left = {}, [], set(), [], 0
    for e in c.ledger_events(p, ["lane_builder", "lane_merge", "lane_train_red"], c.iso(time.time() - DAY_S)):
        t, task, kind = _epoch(e.get("ts")), e.get("task"), e.get("event")
        if t is None:
            continue
        if kind == "lane_builder":
            built[task] = t
        elif kind == "lane_train_red":
            left += 1
        elif kind == "lane_merge":
            landed.append({"task": _t(task, 12), "title": _t(titles.get(task, ""), 80), "at": e["ts"]})
            train = (e.get("data") or {}).get("train") if isinstance(e.get("data"), dict) else None
            trains.add(tuple(train) if isinstance(train, list) else (task,))
            if task in built:
                waits.append(t - built.pop(task))
    return {"landed": landed[-LANDED_MAX:], "landed_24h": len(landed), "trains_24h": len(trains),
            "minutes_per_lane": round(sum(waits) / len(waits) / 60) if waits else None, "left_out": left,
            "running": any(re.search(r"\b(?:fm|sampo) lane train\b", cmd) for _, cmd in _procs())}


def part_loop(p):
    """The task in focus, from the words asked to the brief: criteria (met or not), steps, the stated choice's
    confidence and the stage. None with nothing in focus."""
    b = c.active_brief(live._briefs(p), p.lane)
    if not b:
        return None
    return {"id": b.id, "title": _t(b.title, 120), "words": _t(b.section("Raw request") or b.title, 240),
            "criteria": [{"text": _t(a.text.partition(" :: ")[0], 90), "ok": a.checked} for a in b.acceptance()[:6]],
            "steps": [{"text": _t(s.text, 70), "done": s.done, "current": s.current} for s in b.steps()[:9]],
            "choice": c.stated_confidence(b.section("Approach (options → choice → why)")),
            "stage": c.stage(b, c.effective_autonomy(p))}


def part_library(p):
    """The calling cards there are, the cards and moons kept each hour of the last day (oldest first) and the newest
    grow runs."""
    from sampo import callcards
    now, hourly, recent = time.time(), [0] * 24, []
    for e in c.ledger_events(p, list(GROWS), c.iso(now - DAY_S)):
        t, data = _epoch(e.get("ts")), e.get("data") if isinstance(e.get("data"), dict) else {}
        kept = data.get("kept") if isinstance(data.get("kept"), int) else 0
        if t is None or e.get("event") not in GROWS:
            continue
        h = 23 - int((now - t) // 3600)
        if 0 <= h < 24:
            hourly[h] += kept
        recent.append({"grid": GROWS[e["event"]], "at": e["ts"], "kept": kept})
    return {"calling": len(callcards.cards()[0]), "hourly": hourly, "recent": recent[-6:]}


def part_deps(p):
    """[task, what it depends on...] for each open task that waits on another."""
    return [[b.id] + [str(d) for d in list(b.meta.get("depends_on") or [])[:3]]
            for b in live._briefs(p) if b.status not in c.CLOSED and b.meta.get("depends_on")][:40]


def part_state(p):
    """The state folder's top entries (the newest TREE_MAX, folders first) and the latest decision as a card."""
    from sampo import cli
    rows = []
    try:
        with os.scandir(p.dir) as it:
            for e in it:
                try:
                    st = e.stat()
                    d = e.is_dir()
                except OSError:
                    continue
                rows.append((st.st_mtime, e.name, d, len(_ls(e.path)) if d else 0, 0 if d else st.st_size // 1024))
    except OSError:
        pass
    rows.sort(reverse=True)
    newest = rows[0][1] if rows else ""
    tree = sorted(rows[:TREE_MAX], key=lambda r: (not r[2], r[1]))
    doc = None
    try:
        with open(os.path.join(p.dir, "decisions.md"), "rb") as f:
            f.seek(max(0, os.fstat(f.fileno()).st_size - 16384))
            lines = f.read().decode("utf-8", "replace").splitlines()
        last = next((l for l in reversed(lines) if re.match(r"\|\s*\d{4}-\d\d-\d\d\s*\|", l)), None)
        if last:
            row = cli._decision_row(last)
            doc = {"kind": "decision", "title": _t(row["text"], 80), "text": _t(row["why"], 240), "at": _t(row["date"], 32)}
    except OSError:
        pass
    out = {"tree": [{"name": _t(n, 32), "dir": d, "at": c.iso(t), "n": k, "kb": kb} for t, n, d, k, kb in tree],
           "newest": _t(newest, 32), "doc": doc}
    try:  # T-4665: the foresight numbers for the sampo panel's stat, only once predictions are booked
        from sampo import ahead
        got = ahead.caught(p)
        if got is not None:
            out["caught"] = got
    except Exception:  # noqa: BLE001 - a foresight read never breaks the state panel
        pass
    return out


def part_tests(p):
    """A cell per test module: running (a live process names it), failing (the latest check's run named it), cached
    (green in the run cache) or idle; and how many of the latest check's commands passed."""
    names = sorted({f[:-3] for d in ("plugin/tests", "tests") for f in _ls(os.path.join(p.root, d))
                    if f.startswith("test_") and f.endswith(".py")})
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    path = os.path.join(base, "sampo", "run-cache", hashlib.sha1(os.fsencode(p.root)).hexdigest()[:16] + ".json")
    try:
        with open(path, encoding="utf-8") as f:
            classes = json.load(f).get("classes") or {}
    except (OSError, ValueError, AttributeError):
        classes = {}
    cached = {str(k).split(".")[0] for k in classes}
    last = (c.ledger_tail(p, 2000, kinds=["check_run"]) or [{}])[-1]
    results = [r for r in ((last.get("data") or {}).get("results") or []) if isinstance(r, dict)]
    failing = {str(t).split(".")[0] for r in results for t in (r.get("failing") or [])}
    running = set()
    for _, cmd in _procs():
        if "python" in cmd:
            running.update(re.findall(r"\b(test_\w+)", cmd))
    state = lambda n: "running" if n in running else "fail" if n in failing else "cached" if n in cached else "idle"
    mods = [{"name": _t(n, 40), "state": state(n)} for n in names]
    ts = str(last.get("ts") or "")
    return {"modules": mods, "done": sum(1 for r in results if r.get("exit") == 0), "total": len(results),
            "running": sum(1 for m in mods if m["state"] == "running"), "source": f"last check {ts[11:16]}" if ts else ""}


# ---------------------------------------------------------------- the machine's CPU by kind, a minute at a time

_RING, _RING_LOCK = {}, threading.Lock()


def cpu_reset():
    with _RING_LOCK:
        _RING.clear()
        _RING.update(at=None, busy=None, procs=None, facts={}, minutes=[], now=None)


cpu_reset()


def _busy(proc):
    """The machine's busy clock ticks since boot: every /proc/stat cpu field but idle and iowait (guest is in user)."""
    head = (cpu._read(os.path.join(proc, "stat")) or "").split("\n", 1)[0].split()
    try:
        v = [int(x) for x in head[1:9]]
        return sum(v) - v[3] - v[4] if head[0] == "cpu" and len(v) == 8 else None
    except (IndexError, ValueError):
        return None


def _kind(pid, table):
    k = cpu.kind(pid, table)
    if k in ("tests", "hooks"):
        return k
    me = table[pid]
    if MODELS.search(me["cmd"] or me["comm"]):
        return "models"
    return "claude" if k in ("Claude itself", "short-lived under Claude") else "other"


def cpu_tick(now=None, proc="/proc", every=TICK_S):
    """Reads the machine (at most once each `every` seconds): the cores each kind used since the last read, into this
    minute's average. A process first seen now started since the last read, so all its ticks count; a child seen then and gone
    now is already in its parent's reaped ticks, so those are taken off (cpu.sample's rule). other is the busy rest."""
    now = time.time() if now is None else now
    with _RING_LOCK:
        if _RING["at"] is not None and 0 <= now - _RING["at"] < every:
            return
        facts, busy = _RING["facts"], _busy(proc)
        scan = cpu._scan(None, proc, facts, pids=[n for n in _ls(proc) if n.isdigit()])
        prev, at, busy0 = _RING["procs"], _RING["at"], _RING["busy"]
        _RING.update(at=now, busy=busy, procs=scan)
        if prev is not None and busy is not None and busy0 is not None and now > at:
            secs, used = now - at, dict.fromkeys(KINDS, 0)
            table = {k[0]: facts[k] for k in scan}
            gone = {}
            for k in prev.keys() - scan.keys():
                gone[facts[k]["ppid"]] = gone.get(facts[k]["ppid"], 0) + sum(prev[k])
            for k, (own, reaped) in scan.items():
                own0, reaped0 = prev.get(k, (0, 0))
                used[_kind(k[0], table)] += max(0, own - own0) + max(0, reaped - reaped0 - gone.get(k[0], 0))
            got = {k: v / cpu.TICK / secs for k, v in used.items() if k != "other"}
            got["busy"] = max(0, busy - busy0) / cpu.TICK / secs
            got["other"] = max(0.0, got["busy"] - sum(got[k] for k in KINDS if k != "other"))
            _RING["now"] = {k: round(v, 2) for k, v in got.items()}
            minute, rows = int(now // 60 * 60), _RING["minutes"]
            if not rows or rows[-1][0] != minute:
                rows.append([minute, 0, [0.0] * len(KINDS)])
            rows[-1][1] += 1
            rows[-1][2] = [s + got[k] for s, k in zip(rows[-1][2], KINDS)]
            _RING["minutes"] = [r for r in rows if now - r[0] < MINUTES_MAX * 60][-MINUTES_MAX:]
        for k in [k for k in facts if k not in scan]:
            del facts[k]


def cpu_part(p=None):
    """{kinds, goal_pct, cores, minutes: [[minute epoch, cores per kind...]], now: {kind: cores, busy}}."""
    with _RING_LOCK:
        rows = [[m] + [round(s / n, 2) for s in sums] for m, n, sums in _RING["minutes"]]
        return {"kinds": list(KINDS), "goal_pct": cpu.goal(p) if p else cpu.GOAL, "cores": cpu.ncores(),
                "minutes": rows, "now": dict(_RING["now"]) if _RING["now"] else None}


def part_cpu(p):
    cpu_tick()
    return cpu_part(p)


# ---------------------------------------------------------------- T-3731: the machine's sessions, the user's asks, the
# last push and its CI, the usage tanks and the guard's gate

SESSIONS_MAX, ASKS_MAX, ASK_DAYS, JOBS_MAX, GUARD_RECENT, GUARD_RULES = 24, 60, 7, 24, 12, 8
ASK_STAGES = ("asked", "planned", "building", "verifying", "merging", "blocked", "landed")
CI_RUNNING_S, CI_DONE_S, GH_TIMEOUT = 30, 300, 20  # a running CI is read again after 30 s, a finished one after 5 min
GUARD_LINES = 6000  # the events read back for the gate's last hour (a lower bound past it)
WEEK_MAX = 400


def _hidden():
    """The sensitive projects' slugs (their sessions are a count, never a name)."""
    return live._cached(("board-hidden",), live.SLOW_S,
                        lambda: {q.slug for q, meta in c.all_projects() if meta.get("sensitive")})


def _pct(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return int(round(min(100.0, max(0.0, v)))) if v == v else None


def part_sessions(p):
    """The live Claude sessions on this machine, newest first (at most SESSIONS_MAX): each one's project, task, model,
    context used (0 to 100) and seconds since it last reported; one in a sensitive project only counts in `private`."""
    from sampo import bus
    hidden, now = _hidden(), time.time()
    seen, private = [], 0
    for s in bus.live():
        if s.get("project") in hidden:
            private += 1
        else:
            seen.append(s)
    shown = seen[:SESSIONS_MAX]
    try:
        tasks = {r["session"]: r.get("task") for r in bus.rows(shown)}
    except Exception:  # noqa: BLE001 - the task names are a detail; the orbit stands without them
        tasks = {}
    rows = []
    for s in shown:
        model = s.get("model")
        if isinstance(model, dict):
            model = model.get("display_name") or model.get("id")
        t = _epoch(s.get("ts"))
        rows.append({"id": _t(str(s.get("session_id") or "")[:8], 8), "project": _t(s.get("project"), 40),
                     "task": _t(tasks.get(s.get("session_id")), 12) or None, "model": _t(model, 24) or None,
                     "context_pct": _pct(s.get("context_pct")), "age_s": int(max(0, now - t)) if t else 0,
                     "mine": s.get("project") == p.slug})
    return {"rows": rows, "more": len(seen) - len(shown), "private": private}


def _ask_stage(b, now):
    if b.status == "done":
        t = _epoch(b.meta.get("updated"))
        return "landed" if t and now - t < DAY_S else None
    if b.status == "captured":
        return "asked"
    if b.status in ("blocked", "deferred"):
        return "blocked"
    if b.status == "active":
        return "merging" if b.meta.get("builder_returned") else "building"
    return b.status if b.status in ASK_STAGES else None


def part_asks(p):
    """The user's own asks of the last ASK_DAYS days that haven't landed (and the ones landed in the last day), oldest
    first, each at its stage. Sampo's captures, a tower's Mission briefs and agent notices are not the user's."""
    from sampo import foresight
    now, rows = time.time(), []
    try:
        tower = foresight._tower_ids(p)
    except Exception:  # noqa: BLE001
        tower = set()
    for b in live._briefs(p):
        if b.meta.get("source") != "user" or b.id in tower:
            continue
        at = _epoch(b.meta.get("created"))
        if at is None or now - at > ASK_DAYS * DAY_S:
            continue
        stage = _ask_stage(b, now)
        if stage is None or c._NOTE_RAW.search(b.section("Raw request") or ""):
            continue
        rows.append((at, {"id": _t(b.id, 12), "title": _t(b.title, 90), "stage": stage,
                          "age_h": int(round(max(0, now - at) / 3600))}))
    rows.sort(key=lambda r: r[0])  # the newest ASKS_MAX kept, oldest first: the drawer's "+N older" are the dropped ones
    return {"rows": [r for _, r in rows[-ASKS_MAX:]], "days": ASK_DAYS, "more": max(0, len(rows) - ASKS_MAX)}


def _git(root, *argv):
    import subprocess
    r = subprocess.run(["git", *argv], cwd=root, capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL)
    return r.stdout.strip() if r.returncode == 0 else ""


def _gh(root, *argv):
    """gh's stdout for argv in root; raises when gh fails or runs past GH_TIMEOUT."""
    import subprocess
    r = subprocess.run(["gh", *argv], cwd=root, capture_output=True, text=True, timeout=GH_TIMEOUT,
                       stdin=subprocess.DEVNULL)
    if r.returncode:
        raise RuntimeError(f"gh exit {r.returncode}")
    return r.stdout


CI_ERRORS = ("nogh", "ghfail", "offline")  # gh missing; signed out, rate-limited or garbled; past GH_TIMEOUT
_RUN_JOBS, _RUN_JOBS_MAX = {}, 64  # (root, run id) -> (status, updatedAt, its jobs): run view only when a run moved
_RUN_JOBS_LOCK = threading.Lock()


def _run_jobs(p, r, wf):
    """One run's jobs, each with its start and end epochs: read through `gh run view` only when the run's status or
    updatedAt changed since the last read (a run listed without updatedAt is always read)."""
    key, mark = (p.root, int(r["databaseId"])), (r.get("status"), r.get("updatedAt"))
    with _RUN_JOBS_LOCK:
        hit = _RUN_JOBS.get(key)
    if hit and mark[1] and hit[0] == mark:
        return hit[1]
    got = json.loads(_gh(p.root, "run", "view", str(key[1]), "--json", "jobs") or "{}")
    jobs = []
    for j in (got.get("jobs") or []) if isinstance(got, dict) else []:
        if not isinstance(j, dict):
            continue
        steps = [s for s in j.get("steps") or [] if isinstance(s, dict)]
        jobs.append({"workflow": wf, "name": _t(j.get("name"), 60), "status": _t(j.get("status"), 20),
                     "conclusion": _t(j.get("conclusion"), 20) or None,
                     "steps": [sum(s.get("status") == "completed" for s in steps), len(steps)],
                     "start": _epoch(j.get("startedAt")), "end": _epoch(j.get("completedAt"))})
    with _RUN_JOBS_LOCK:
        _RUN_JOBS.pop(key, None)
        _RUN_JOBS[key] = (mark, jobs)
        while len(_RUN_JOBS) > _RUN_JOBS_MAX:
            _RUN_JOBS.pop(next(iter(_RUN_JOBS)))
    return jobs


def _ci_now(p, sha):
    """The CI runs of one commit (at most 3) and their jobs: each job's steps [done, total] and its seconds so far.
    A gh that's missing, fails or runs past GH_TIMEOUT gives no runs and an `error` (one of CI_ERRORS) the panel names."""
    import subprocess
    try:
        runs = [r for r in json.loads(_gh(p.root, "run", "list", "--commit", sha, "--limit", "10", "--json",
                                          "databaseId,status,conclusion,workflowName,updatedAt") or "[]")
                if isinstance(r, dict)][:3]
        out, jobs, now = [], [], time.time()
        for r in runs:
            wf = _t(r.get("workflowName"), 40)
            out.append({"workflow": wf, "status": _t(r.get("status"), 20),
                        "conclusion": _t(r.get("conclusion"), 20) or None})
            if isinstance(r.get("databaseId"), int):
                jobs += _run_jobs(p, r, wf)
    except FileNotFoundError:
        return {"runs": [], "jobs": [], "error": "nogh"}
    except subprocess.TimeoutExpired:
        return {"runs": [], "jobs": [], "error": "offline"}
    except (RuntimeError, ValueError, AttributeError, OSError):
        return {"runs": [], "jobs": [], "error": "ghfail"}

    def secs(j):
        start, end = j["start"], j["end"]
        return int(max(0, (end if end and j["status"] == "completed" else now) - start)) if start else 0
    return {"runs": out, "jobs": [{k: v for k, v in dict(j, secs=secs(j)).items() if k not in ("start", "end")}
                                  for j in jobs[:JOBS_MAX]]}


def _ci_state(got):
    if got.get("error") in CI_ERRORS:
        return got["error"]
    runs, jobs = got.get("runs") or [], got.get("jobs") or []
    if not runs:
        return "waiting"
    if any(r.get("status") != "completed" for r in runs):
        return "running"
    bad = ("failure", "timed_out", "startup_failure", "cancelled")
    return "red" if any(r.get("conclusion") in bad for r in runs) or any(j.get("conclusion") in bad for j in jobs) \
        else "green"


def part_ship(p):
    """The last push (the commit ship pushed, else origin's default branch): its subject, age and the commits on top
    of it; and its CI, read through gh in the background (kept CI_RUNNING_S while it runs, CI_DONE_S once done).
    None with nothing pushed."""
    from sampo import foresight, release
    sha, ref = _git(p.root, "rev-parse", "-q", "--verify", release.SHIPPED_TIP + "^{commit}"), "shipped"
    if not sha:
        if not _git(p.root, "remote"):
            return None
        sha, ref = _git(p.root, "rev-parse", "-q", "--verify",
                        f"refs/remotes/origin/{foresight.default_branch(p.root)}^{{commit}}"), "origin"
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        return None
    subject, _, ct = _git(p.root, "log", "-1", "--format=%s%x00%ct", sha).partition("\0")
    try:
        unshipped = int(_git(p.root, "rev-list", "--count", f"{sha}..HEAD") or 0)
    except ValueError:
        unshipped = 0
    age = int(max(0, time.time() - int(ct))) if ct.isdigit() else None
    out = {"sha": sha[:7], "subject": _t(subject, 120), "age_s": age, "ref": ref, "unshipped": unshipped}
    empty = {"state": "reading", "runs": [], "jobs": [], "failed": []}
    if not foresight.github_repo(p.root):
        ci = dict(empty, state="local")
    elif os.environ.get("FOREMAN_NO_NET") or p.slug in _hidden():
        ci = dict(empty, state="offline")
    else:
        key = ("ci-jobs", p.dir, sha)
        with live._LOCK:
            hit = live._CACHE.get(key)
        done = bool(hit and hit[1] and _ci_state(hit[1]) in ("green", "red", *CI_ERRORS))  # a failing gh: retried in 5 min
        got = live._stale(key, CI_DONE_S if done else CI_RUNNING_S, lambda: _ci_now(p, sha), None)
        ci = empty if got is None else {"state": _ci_state(got), "runs": got.get("runs") or [], "jobs": got.get("jobs") or [],
                                        "failed": [j["name"] for j in got.get("jobs") or []
                                                   if j.get("conclusion") in ("failure", "timed_out")]}
    return dict(out, ci=ci)


def part_usage(p):
    """The 5-hour and weekly usage windows (percent used, span, seconds to reset, projected percent, seconds until full;
    or why the figure is unknown), today's spend by hour against the daily cap and this week's samples as
    [hours ago, percent], oldest first."""
    from sampo import budget
    now = time.time()
    fc, windows = budget.forecast(now) or {}, {}
    for key, span in budget.WINDOWS.items():
        w = fc.get(key)
        if isinstance(w, dict) and isinstance(w.get("used"), (int, float)):
            full = w.get("full_at")
            windows[key] = {"used": w["used"], "span": span,
                            "resets_in": int(w["resets_at"] - now) if isinstance(w.get("resets_at"), (int, float)) else None,
                            "projected": w.get("projected"),
                            "full_in": int(full - now) if isinstance(full, (int, float)) else None}
        else:
            windows[key] = {"used": None, "span": span, "why": _t(budget.unknown_why(key, now), 80) or None}
    hourly = live.spend_today()
    rows = budget.samples()
    last = max((r["resets"] for r in rows), default=None)
    week = [[round((now - r["ts"]) / 3600, 2), r["used"]] for r in rows if r["resets"] == last][-WEEK_MAX:]
    return {"windows": windows, "spend": {"today": round(sum(hourly), 2), "cap": budget.caps().get("day"), "hourly": hourly},
            "week": week}


_OPAQUE = re.compile(r"(?<![\w/.~-])(?=[\w+=-]*\d)(?=[\w+=-]*[A-Za-z])[\w+=-]{20,}(?![\w/.~-])")


_URL = re.compile(r"https?://[^\s'\"<>]+")
_URL_TOKEN = re.compile(r"(?=[\w+-]*\d)(?=[\w+-]*[A-Za-z])[\w+-]{20,}")


def _mask_url(m):
    return _URL_TOKEN.sub("[REDACTED]", m.group(0))


def _guard_cmd(s):
    """A refused command as the page may show it: redacted (known token shapes, home paths, private terms) and any
    other opaque run of 20+ letters and digits outside a path masked, and inside a URL (path segment or query value,
    where a chat-webhook secret lives) too, since a refused command is often one carrying a secret the guard caught."""
    return c.fit(_URL.sub(_mask_url, _OPAQUE.sub("[REDACTED]", _t(s, 400))), 120)


def _tally():
    return {"allowed": 0, "refused": 0, "rules": {}, "tools": {}, "minutes": [[0, 0] for _ in range(60)]}


def _by_n(d, k, n):
    return [{k: name, "n": v} for name, v in sorted(d.items(), key=lambda kv: (-kv[1], kv[0]))][:n]


def _guard(slugs, by=False):
    """The gate's last hour for these projects; by: each project's own counts too (the All view's chips filter them)."""
    slugs, now = set(slugs), time.time()
    total, each, recent = _tally(), {}, []
    for e in c.tail_jsonl(os.path.join(c.state_dir(), "events.jsonl"), GUARD_LINES):
        if not isinstance(e, dict) or e.get("project") not in slugs:
            continue
        kind = e.get("kind")
        if kind not in ("tool", "tool_fail", "guard_block"):
            continue
        t = _epoch(e.get("ts"))
        if t is None or not 0 <= now - t < 3600:
            continue
        blocked, tool = kind == "guard_block", _t(e.get("tool"), 24)
        rule = _t(e.get("rule") or e.get("category") or "guard", 40) if blocked else None
        for k in [total] + ([each.setdefault(e["project"], _tally())] if by else []):
            k["minutes"][59 - int((now - t) // 60)][blocked] += 1
            if tool:
                k["tools"][tool] = k["tools"].get(tool, 0) + 1
            k["refused" if blocked else "allowed"] += 1
            if blocked:
                k["rules"][rule] = k["rules"].get(rule, 0) + 1
        if blocked:
            recent.append({"ago_s": int(now - t), "rule": rule, "tool": tool,
                           "cmd": _guard_cmd(e.get("cmd") or e.get("target")), "project": _t(e.get("project"), 40)})
    recent.sort(key=lambda r: r["ago_s"])
    shape = lambda k: dict(k, rules=_by_n(k["rules"], "rule", GUARD_RULES), tools=_by_n(k["tools"], "tool", 8))  # noqa: E731
    out = dict(shape(total), recent=recent[:GUARD_RECENT])
    if by:
        out["by"] = {_t(s, 40): shape(k) for s, k in each.items()}
    return out


def part_guard(p):
    """The last hour at the guard's gate for this project: the tool calls let through and the ones refused, by rule,
    the newest refusals, each minute's two counts and the tools called most."""
    return _guard([p.slug])


def guard_all(slugs):
    """The gate's last hour across these projects (the All view), each refusal tagged with its project, and `by` each
    project's own counts, rules, minutes and tools, so a hidden chip takes its project out of every figure."""
    return live._cached(("board-guard", tuple(sorted(slugs))), SLOW["guard"], lambda: _guard(slugs, by=True))


PARTS = {"cpu": part_cpu, "lanes": part_lanes, "train": part_train, "loop": part_loop, "library": part_library,
         "commands": part_commands, "deps": part_deps, "state": part_state, "tests": part_tests,
         "sessions": part_sessions, "asks": part_asks, "ship": part_ship, "usage": part_usage,
         "guard": part_guard}  # cpu first: its scan is _procs


def collect(p):
    """{part: its facts, or None when it raised}; the slow parts kept SLOW[part] seconds."""
    out = {}
    for name in list(PARTS):
        fn = PARTS[name]
        try:
            out[name] = live._cached(("board", name, p.dir), SLOW[name], lambda: fn(p)) if name in SLOW else fn(p)
        except Exception:  # one part down leaves the rest of the board
            out[name] = None
    return out


# T-3739: the Board's layout, which panels show, where, how big, in which style and how much they move. One file per
# scope (the global one in the state folder, a project's in its own), each partial; layout_view reads them field by
# field, project over global over the built-in one. `sampo board layout` and the page's edit mode (/api/act's
# board_layout) write them through layout_set; the pane and Sampo Code read layout_view's order and hidden panels.
PANEL_KEYS = ("lanes", "train", "cpu", "loop", "library", "models", "sampo", "state", "tests",
              "asks", "sessions", "ship", "usage", "guard")  # the page's order (T-3731 added the last five)
STYLES, MOTIONS = ("card", "flat"), ("full", "calm", "still")
SIZES = {"span": "columns", "height": "rows"}  # each 1 to 3
LAYOUT_FILE, LAYOUT_MAX = "board-layout.json", 4096  # bytes of a scope's file: fourteen panels need about 1000


def layout_builtin():
    return {"order": list(PANEL_KEYS), "panels": {k: {"span": 1, "height": 1, "hidden": False} for k in PANEL_KEYS},
            "style": "card", "motion": "full"}


def _name(k):
    return re.sub(r"[^\w.-]", "?", str(k))[:40] or "?"  # a field named in an error: plain, short


def layout_check(obj):
    """obj, one scope's layout (any field may be left out), as stored: {version, order?, panels?, style?, motion?}.
    ValueError whose text starts with the first bad field (panels.lanes.span, order[1], style)."""
    if not isinstance(obj, dict):
        raise ValueError("layout: an object with order, panels, style or motion")
    try:
        size = len(json.dumps(obj))
    except (TypeError, ValueError):
        raise ValueError("layout: plain JSON only") from None
    if size > LAYOUT_MAX:
        raise ValueError(f"layout: {size} bytes, over {LAYOUT_MAX}")
    out = {"version": 1}
    for k, v in obj.items():
        if k == "version":
            if v != 1 or isinstance(v, bool):
                raise ValueError("version: 1 is the only one")
        elif k == "order":
            if not isinstance(v, list):
                raise ValueError("order: a list of panels (" + ", ".join(PANEL_KEYS) + ")")
            for i, x in enumerate(v):
                if x not in PANEL_KEYS:
                    raise ValueError(f"order[{i}]: {_name(x)} is not a panel (" + ", ".join(PANEL_KEYS) + ")")
                if x in v[:i]:
                    raise ValueError(f"order: {x} twice")
            out["order"] = list(v)
        elif k == "panels":
            if not isinstance(v, dict):
                raise ValueError("panels: an object of panel: {span, height, hidden}")
            out["panels"] = {}
            for key, fields in v.items():
                if key not in PANEL_KEYS:
                    raise ValueError(f"panels.{_name(key)}: not a panel (" + ", ".join(PANEL_KEYS) + ")")
                if not isinstance(fields, dict):
                    raise ValueError(f"panels.{key}: an object of span, height and hidden")
                for f, x in fields.items():
                    if f in SIZES:
                        if not isinstance(x, int) or isinstance(x, bool) or not 1 <= x <= 3:
                            raise ValueError(f"panels.{key}.{f}: 1 to 3 {SIZES[f]}")
                    elif f == "hidden":
                        if not isinstance(x, bool):
                            raise ValueError(f"panels.{key}.hidden: true or false")
                    else:
                        raise ValueError(f"panels.{key}.{_name(f)}: not a panel field (span, height, hidden)")
                out["panels"][key] = dict(fields)
        elif k == "style":
            if v not in STYLES:
                raise ValueError("style: " + " or ".join(STYLES))
            out["style"] = v
        elif k == "motion":
            if v not in MOTIONS:
                raise ValueError("motion: full, calm or still")
            out["motion"] = v
        else:
            raise ValueError(f"{_name(k)}: not a layout field (order, panels, style, motion)")
    return out


def layout_path(scope, p=None):
    if scope == "global":
        return os.path.join(c.state_dir(), LAYOUT_FILE)
    if scope == "project" and p is not None:
        return os.path.join(p.dir, LAYOUT_FILE)
    raise ValueError("scope: global, or project with a project")


def _layout_read(path):
    """(the scope's checked layout or None when there is no file, the error that skipped it or None)."""
    try:
        with open(path, "rb") as f:
            raw = f.read(LAYOUT_MAX * 4 + 1)
    except FileNotFoundError:
        return None, None
    except OSError as e:
        return None, f"unreadable ({e.strerror or e})"
    if len(raw) > LAYOUT_MAX * 4:
        return None, "layout: too big"
    try:
        return layout_check(json.loads(raw.decode("utf-8"))), None
    except UnicodeDecodeError:
        return None, "layout: not UTF-8"
    except RecursionError:  # a few thousand '[' fit the byte cap
        return None, "layout: nested too deep"
    except ValueError as e:  # json's own error is a ValueError too: "Expecting value: line 1 ..."
        return None, _t(str(e), 200)


def _layout_union(old, new):
    """new's fields over old's: order and style whole, each panel's fields one by one."""
    out = dict(old, **{k: v for k, v in new.items() if k != "panels"})
    panels = {k: dict(v) for k, v in (old.get("panels") or {}).items()}
    for k, v in (new.get("panels") or {}).items():
        panels[k] = dict(panels.get(k) or {}, **v)
    if panels:
        out["panels"] = panels
    return out


def layout_view(p=None):
    """The layout in force for p (None: the global one, as the All view and a pane outside a project read it):
    {order: every panel (PANEL_KEYS), panels: {key: {span, height, hidden}}, style, motion, from: the top scope that had a
    file, errors: each scope's file that was skipped, and why}."""
    eff, src, errors = {}, "builtin", []
    for scope, q in (("global", None), ("project", p)):
        if scope == "project" and p is None:
            continue
        part, err = _layout_read(layout_path(scope, q))
        if err:
            errors.append(f"{scope} layout skipped: {err}")
        if part is not None:
            eff, src = _layout_union(eff, part), scope
    out = layout_builtin()
    order = eff.get("order") or []
    out["order"] = list(order) + [k for k in PANEL_KEYS if k not in order]
    for k, v in (eff.get("panels") or {}).items():
        out["panels"][k].update(v)
    out["style"], out["motion"] = eff.get("style", out["style"]), eff.get("motion", out["motion"])
    out["from"], out["errors"] = src, errors
    return out


def layout_short(p=None):
    """What a pane reads (sampo ui's board_layout, Sampo Code): the whole order, the hidden panels in that order, each
    panel's span and height, the scope it came from, and the files skipped."""
    v = layout_view(p)
    ps = v["panels"]
    return {"order": v["order"], "hidden": [k for k in v["order"] if ps[k]["hidden"]],
            "span": {k: ps[k]["span"] for k in v["order"]}, "height": {k: ps[k]["height"] for k in v["order"]},
            "from": v["from"], "errors": v["errors"]}


def _layout_lock():
    return c.lock(os.path.join(c.state_dir(), "board-layout.lock"))


def layout_set(scope, p, changes, replace=False):
    """Check changes, then merge them into the scope's file (replace: the file becomes them, as the page sends its
    whole layout). ValueError naming the field, and nothing written, when they are bad. Returns what was written."""
    path = layout_path(scope, p)
    new = layout_check(changes)
    with _layout_lock():
        if not replace:
            old, _ = _layout_read(path)  # a broken file is replaced by the changes
            new = layout_check(_layout_union(old or {}, new))
        c.write_atomic(path, json.dumps(new, indent=1, sort_keys=True) + "\n")
    return new


def layout_reset(scope, p=None):
    """Remove the scope's file, so the scope below shows; True when there was one."""
    path = layout_path(scope, p)
    with _layout_lock():
        try:
            os.unlink(path)
        except FileNotFoundError:
            return False
    return True
