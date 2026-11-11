"""T-3127: self-checks: what the user would otherwise have to point out, named with its numbers before they ask. On
2026-11-06 they asked, one message each: why landed work hadn't reached GitHub in 22 hours, why a retired .foreman/
was tracked beside .sampo/, and why pages they had asked for weren't done, then "I shouldn't even need to send any of
the messages". Each check reads state and at most one git call; lines() caches the set CACHE_S per project, and
next_for (sampo next and the drive's line) shows it in every state, not only when idle."""
import calendar
import collections
import json
import os
import re
import time

from sampo import core as c

CACHE_S = 300
SHIP_H = 2  # landed work unshipped this long (its oldest commit since the last ship)
ASK_DAYS, ASK_H = 3, 2  # the user's asks from the last ASK_DAYS days, open ASK_H hours or more
OPEN = ("captured", "planned", "active", "verifying")  # deferred: the user said later; blocked: its own line says why


class Hit(str):
    """T-4118: a check's line that also acts (action: argv run detached, at the lowest priority) or tells the user
    (tell; level: told again the same day only when it rises). A plain str only prints, as before."""
    def __new__(cls, text, action=None, tell=False, level=0):
        s = super().__new__(cls, text)
        s.action, s.tell, s.level = list(action) if action else None, bool(tell), level
        return s


def _fm(*args):
    return [os.path.join(c.PLUGIN_ROOT, "bin", "fm"), *args]


def _age(secs):
    h = secs / 3600
    return f"{h:.0f} h" if h >= 1 else f"{secs / 60:.0f} min"


def unshipped(p, briefs, now):
    """Commits since the last sampo ship (its ref) whose oldest is SHIP_H hours old: ship_waiting says how many only
    when no task is open, so a busy session never saw 22 hours of work wait."""
    last = "refs/foreman/shipped"  # release.SHIPPED; re-picked or rebased past it, the range isn't what main lacks
    if c._git(p.root, "merge-base", "--is-ancestor", last, "HEAD", proc=True, timeout=10).returncode:
        return None
    times = c._git(p.root, "log", "--first-parent", "--format=%ct", f"{last}..HEAD", timeout=10).split()
    if not times:
        return None
    oldest = now - int(times[-1])
    if oldest < SHIP_H * 3600:
        return None
    return Hit(f"{len(times)} landed commit(s) unshipped, the oldest {_age(oldest)} → sampo ship", tell=True,
               level=int(oldest // (6 * 3600)))  # T-4118: told; again each 6 h it grows (T-4121 ships it)


def old_mirror(p, briefs, now):
    """The retired mirror folder tracked beside the current one: a process on pre-rename code (or a pull) brings it
    back, and a close commits it (T-3390's did, 6,233 files)."""
    old, new = c.MIRRORS[1], c.MIRROR
    if not os.path.isdir(os.path.join(p.root, old)) or not os.path.isdir(os.path.join(p.root, new)):
        return None
    if not c._git(p.root, "ls-files", "--", old, timeout=10).strip():
        return None
    return f"the retired {old}/ is tracked beside {new}/ → sampo sync export folds it in and commits it gone"


def _concerns_waiting(held):
    """T-4200: the open concerns from the last ASK_DAYS days, open ASK_H hours or more, oldest first. A shown or
    answered one is not waiting; a concern said again after its proof is open again (concerns.fold says so)."""
    rows = []
    for x in held:
        made = c.age_days(x["opened"])
        if x["state"] != "open" or made is None or made > ASK_DAYS or made * 24 < ASK_H:
            continue
        rows.append((made, x))
    if not rows:
        return None
    rows.sort(key=lambda r: -r[0])
    shown = [f"{x['id']} ({_age(made * 86400)}, said {x['said']}x, surface: {x['surface'] or 'none'})"
             for made, x in rows[:3]]
    return Hit(f"{len(rows)} of the user's concerns from the last {ASK_DAYS} days aren't shown; oldest: "
               + "; ".join(shown) + " → show the oldest with a proof, or answer it", tell=True,
               level=len(rows) // 5)


def asks_waiting(p, briefs, now):
    """The user's own asks from the last ASK_DAYS days that are open ASK_H hours or more, oldest first, each with
    where it sits: they ask "why isn't X done" next."""
    from sampo import concerns, foresight
    briefs = list(briefs)  # read twice below (the concerns' task links, then the old count): a generator would run dry
    held = concerns.fold(p, briefs)  # T-4200: once the steer meter has read any message, its concerns are the count
    if held:
        return _concerns_waiting(held)
    tower, rows = foresight._tower_ids(p), []  # review: the tower's own briefs and agent-noted ones aren't the user's
    for b in briefs:
        if b.meta.get("source") != "user" or b.status not in OPEN or b.id in tower \
                or c._NOTE_RAW.search(b.section("Raw request")):
            continue
        made = c.age_days(b.meta.get("created"))
        if made is None or made > ASK_DAYS or made * 24 < ASK_H:
            continue
        rows.append((made, b))
    if not rows:
        return None
    rows.sort(key=lambda r: -r[0])
    shown = []
    for made, b in rows[:3]:
        where = b.status + (": its lane waits on a merge" if b.meta.get("builder_returned") else "")
        shown.append(f"{b.id} ({_age(made * 86400)}, {where})")
    return Hit(f"{len(rows)} of the user's asks from the last {ASK_DAYS} days aren't landed; oldest: " + "; ".join(shown)
               + " → land or unblock the oldest first", tell=True, level=len(rows) // 5)


CHECKS = (unshipped, old_mirror, asks_waiting)


SLOW_X, SLOW_OVER_S, HISTORY, HISTORY_DAYS = 1.5, 60, 20, 7
# T-3482: Sampo's live-job markers in meta (pid + start "at"), each with the ledger event that records its finished runs
JOBS = (("gates_running", "the full gates run", "check_run"), ("ship_testing", "sampo ship's test run", "ship_test"))


def _secs(e):
    """A passing run's seconds (its own "s", or its gates' sum); a red run stops early and the train is another job."""
    d = e.get("data") or {}
    rs = d.get("results") or []
    if d.get("train") or d.get("exit") or any(r.get("exit") for r in rs):
        return None
    return d["s"] if "s" in d else sum(r.get("s") or 0 for r in rs)


def slow_jobs(p, briefs, now):
    """T-3482: "why is this stuff taking so long just to push?": a live job past SLOW_X its median of the last HISTORY
    passing runs, the ledger's own history (quiet under 3 runs)."""
    out = []
    for key, name, event in JOBS:
        start = c.parse_ts((c.gates_live(p, key=key) or {}).get("at"))
        if not start:
            continue
        ran = now - start.timestamp()
        past = [s for s in map(_secs, c.ledger_events(p, [event], since=c.iso(now - HISTORY_DAYS * 86400))) if s]
        past = sorted(past[-HISTORY:])
        typ = past[len(past) // 2] if len(past) >= 3 else None
        if typ and ran > SLOW_X * typ and ran - typ > SLOW_OVER_S:
            out.append(f"{name} has run {_age(ran)}, {ran / typ:.1f}x its typical {_age(typ)} → see what it waits on")
    return Hit(" · ".join(out), tell=True) if out else None


REPEAT_N, REPEAT_S = 3, 3600
_WRAP = c.lazy(r"^(?:cd \S+ && |nice (?:-n ?\d+ )?|(?:sampo|fm) quiet -- )+")


def repeats(p, briefs, now):
    """T-3482: the same command failing REPEAT_N+ times in the last hour, a retry storm. Failing runs only: plain 3+
    reruns are TDD's own (red, green, final) and fired in 77% of the real ledger's 10-minute windows."""
    runs = {}
    for e in c.ledger_events(p, ["evidence"], since=c.iso(now - REPEAT_S)):
        d = e.get("data") or {}
        if not d.get("cmd") or not isinstance(d.get("result"), str):
            continue
        cmd = _WRAP.sub("", " ".join(str(d["cmd"]).split()))
        r = runs.setdefault(re.sub(r"^(?:fm|sampo) ", "sampo ", cmd), [0, 0, set()])  # one key for either spelling
        r[0] += 1
        if d["result"].startswith("✗"):  # "red as expected" is a TDD red, not a failure
            r[1] += 1
            r[2].add(e.get("task") or "no task")
    hits = sorted(((r[1], r[0], cmd, r[2]) for cmd, r in runs.items() if r[1] >= REPEAT_N), reverse=True)
    if not hits:
        return None
    bad, n, cmd, tasks = hits[0]
    more = f"; {len(hits) - 1} more command(s) like it" if len(hits) > 1 else ""
    return (f"{bad} of {n} runs of `{c.fit(cmd, 80)}` in the last hour failed ({c.joinmore(sorted(tasks), 3)}){more} → "
            "read why it fails before running it again")


WAIT_N, WAIT_STALE_S = 3, 1800


def wait_turns(p, briefs, now):
    """T-3482: the agent's own wait-only replies in a row, counted by the Stop hook ([n, first, last] in gate.json's
    per-session drive state); a session quiet WAIT_STALE_S since says nothing. Read uncached: a small file."""
    try:
        with open(os.path.join(p.dir, "gate.json"), encoding="utf-8") as f:
            drive = json.load(f).get("drive") or {}
    except (OSError, ValueError, AttributeError):
        return None
    runs = [w for d in drive.values() if isinstance(d, dict) and isinstance(w := d.get("waits"), list) and len(w) == 3
            and now - w[2] < WAIT_STALE_S]
    n, first, _ = max(runs, default=(0, 0, 0))
    if n < WAIT_N:
        return None
    return (f"{n} wait-only replies in a row over {_age(now - first)} → name the slow job's elapsed vs its typical, "
            "or work on something else")


CHECKS += (slow_jobs, repeats)
LIVE = (wait_turns,)  # cheap reads, run past the cache: a wait shows on the next turn, not 5 minutes on


WAIT_MIN, RATE_H = 5, 2  # T-3478: lanes waiting before the rate is worth a line; the window it is measured over


def landing_rate(p, briefs, now):
    """T-3478: finished lanes waiting on a merge, the landings of the last RATE_H hours (minutes each) and the hours
    the pile takes to clear at that rate: 'why is this taking so long just to push?' was asked with ~50 waiting at
    ~15 min each, a number nobody had said."""
    from sampo import lanes as slanes
    waiting = slanes.awaiting_merge(p, briefs)
    if len(waiting) < WAIT_MIN:
        return None
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - RATE_H * 3600))
    landed = {e.get("task") for e in c.ledger_events(p, ("lane_merge",), since=since)
              if str(e.get("ts", "")) >= since and not (e.get("data") or {}).get("undone")}
    if not landed:
        # T-4118: starts a land (a second one returns at once: "a land is already running")
        return Hit(f"{len(waiting)} finished lanes wait and none landed in the last {RATE_H} h → {c.merge_hint(waiting)}",
                   action=_fm("lane", "land"))
    each = RATE_H * 60 / len(landed)
    return (f"{len(waiting)} finished lanes wait; {len(landed)} landed in the last {RATE_H} h (~{each:.0f} min each), "
            f"so the pile clears in ~{len(waiting) * each / 60:.0f} h at this rate → measure the slowest step and cut it")


CHECKS += (landing_rate,)


UNTRACKED_H, UNTRACKED_STATS = 1, 500  # untracked this long is left behind, not work in progress; files stat'ed


def _older(path, now):
    try:
        return now - os.path.getmtime(path) >= UNTRACKED_H * 3600
    except OSError:
        return False


def untracked(p, briefs, now):
    """T-3548: untracked, non-ignored files in the main checkout are part of its code tree (core.worktree_tree), so no
    close there matches the train's pass on the merged commit and each reruns its linked tests: an untracked scripts/
    did that all day (78-309 s a close). The mirror's .md files are left out, as worktree_tree leaves them. From a lane
    (p.root is its worktree, the cache is the project's) it reads the main checkout all the same. Only files untouched
    UNTRACKED_H hours count: one made a minute ago is work in progress."""
    main = c.main_worktree(p.root) or p.root
    out = c._git(main, "ls-files", "-z", "--others", "--exclude-standard", "--", ".",
                 *(f":(exclude,glob){m}/**/*.md" for m in c.MIRRORS), timeout=10)
    # ponytail: only the first UNTRACKED_STATS listed are stat'ed; a bigger pile is named by its first part
    files = [f for f in out.split("\0")[:UNTRACKED_STATS] if f and _older(os.path.join(main, f), now)]
    if not files:
        return None
    tops = collections.Counter(f.split("/")[0] + ("/" if "/" in f else "") for f in files)  # ls-files order on ties
    shown = [t + (f" ({n})" if t.endswith("/") else "") for t, n in tops.most_common()]
    return (f"{len(files)} untracked file(s) in the main checkout ({c.joinmore(shown, 4)}) keep every close off the "
            "train's pass, so each reruns its tests → commit, .gitignore or remove them")


CHECKS += (untracked,)

MAP_ROOM = 2000  # bytes: a few map lines; under this a split is due before the next close adds one


def map_room(p, briefs, now):
    """T-4057: MASTER.md or a docs/map shard within MAP_ROOM of the cap a Read opens whole (test_docs and doctor hold it):
    the next close that maps a module there goes red, so the split is named first."""
    out = []
    for path in [os.path.join(p.root, "MASTER.md")] + c.map_shards(p.root):
        try:
            left = c.MAP_FILE_BYTES - os.path.getsize(path)
        except OSError:
            continue
        if left < MAP_ROOM:
            out.append(f"{os.path.relpath(path, p.root)} has {left} bytes left of its {c.MAP_FILE_BYTES}")
    return (" · ".join(out) + " → split it: move a block of entries to a new docs/map shard and their MASTER.md index "
            "links with it") if out else None


CHECKS += (map_room,)


def cpu_goal(p, briefs, now):
    """T-3592: the session's 10-minute CPU average over the goal (10% of the machine), with the top consumer."""
    from sampo import cpu
    return cpu.goal_line(p, now)


CHECKS += (cpu_goal,)


def usage_burn(p, briefs, now):
    """T-4118: the usage-burn scan's run-out forecast (cost.burn_findings, state/burn.json, the last day): burn.json
    said 'on pace to run out 93 h before its reset' for hours and nothing told the user (the steer audit, T-4115)."""
    try:
        with open(os.path.join(c.state_dir(), "burn.json"), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    if now - float(d.get("ts") or 0) > 86400:
        return None
    for row in d.get("found") or []:
        if row and "run out" in str(row[0]):
            return Hit(f"{row[0]} → {row[1]}" if len(row) > 1 else str(row[0]), tell=True)
    return None


def stale_beats(p, briefs, now):
    """T-4118: a background job's heartbeat (fm run, the merge loop) past its next beat: it stopped without a word."""
    from sampo import serve
    late = serve.stale_beats(p, now)
    return Hit("stale heartbeat: " + ", ".join(n for n, _ in late) + " → see whether it died", tell=True) if late else None


def pause_lingers(p, briefs, now):
    """T-4190: a pause that has outlasted its welcome, with who set it. A session's own pause is told (it lifts it with
    sampo pause off: it was the one that set it); the user's is only named, after an hour, and stays theirs."""
    info = c.pause_info()
    if not info:
        return None
    try:
        age = now - calendar.timegm(time.strptime(info["at"], "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, OverflowError):
        return None
    if info["by"] == "agent":
        return Hit(f"{c.pause_line(info)}, {_age(age)} ago → lift it if it has done its job", tell=True) \
            if age >= PAUSE_AGENT_MIN * 60 else None
    return f"{c.pause_line(info)}, {_age(age)} ago" if age >= 3600 else None


PAUSE_AGENT_MIN = 10  # a session's own pause told after this many minutes
CHECKS += (usage_burn, stale_beats, pause_lingers)


def lines(p, briefs=None, now=None):
    """The checks' hits, cached CACHE_S (a hook calls this on every Stop). A check that can't read its source is
    skipped, never a crash."""
    now = time.time() if now is None else now
    return _cached(p, briefs, now) + _run(LIVE, p, briefs, now)


def _run(checks, p, briefs, now):
    out = []
    for check in checks:
        try:
            hit = check(p, briefs, now)
        except Exception:  # noqa: BLE001 — a check never costs the next action
            hit = None
        if hit:
            out.append(hit)
    return out


def _cached(p, briefs, now):
    path = os.path.join(p.dir, "selfcheck.json")
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        if 0 <= now - float(d.get("at", 0)) < CACHE_S:
            return list(d.get("lines") or [])
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    out = _run(CHECKS, p, c.load_briefs(p) if briefs is None else briefs, now)
    try:
        c.write_atomic(path, json.dumps({"at": now, "lines": out}))
    except OSError:
        pass
    return out


# T-4118: findings act or tell, with drive on or off (night.tick runs act: the Stop hook's sweep and the hourly timer).
# An action runs detached at most once per ACT_BACKOFF_S and ACT_PER_DAY times a day per check; a tell, an action that
# couldn't start, or one whose finding outlives ESCALATE_S reaches the user once a day per check, sooner only when its
# level rises. State per check in selfcheck-act.json; a resolved check keeps only when it was last told.
ACT_BACKOFF_S, ACT_PER_DAY, ESCALATE_S, TELL_EVERY_S = 1800, 3, 2 * 3600, 86400


def alerts(p, now=None, keep=5):
    """The last tells of the past day, oldest first, for the pane (fm ui --json alerts)."""
    cut = (now or time.time()) - TELL_EVERY_S
    rows = []
    try:
        with open(os.path.join(p.dir, "alerts.jsonl"), encoding="utf-8") as f:
            for ln in f.readlines()[-50:]:
                try:
                    r = json.loads(ln)
                    at = c.parse_ts(r["at"])
                    if at and at.timestamp() >= cut:
                        rows.append({"at": r["at"], "text": str(r["text"])[:300]})
                except (ValueError, KeyError, TypeError):
                    continue
    except OSError:
        return []
    return rows[-keep:]


def notify(p, text):
    """The user's notify command (sampo notify), else a desktop notification where a display exists; always a row in
    alerts.jsonl for the pane."""
    try:
        c.append_line(os.path.join(p.dir, "alerts.jsonl"), json.dumps({"at": c.now(), "text": c.plain(text)}) + "\n")
    except OSError:
        pass
    if c.read_meta(p).get("notify"):
        from sampo import serve
        serve._notify(p, text)
        return
    import shutil
    import subprocess
    if os.environ.get("FOREMAN_NO_BACKGROUND"):
        return  # tests: never a popup on the desktop running the suite
    if shutil.which("notify-send") and (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        try:
            subprocess.run(["notify-send", "Sampo", c.plain(text)[:300]], timeout=10, stdin=subprocess.DEVNULL,
                           capture_output=True)
        except (OSError, subprocess.SubprocessError):
            pass


def _spawn(p, argv):
    """argv detached at nice 19 (the desktop first), its pid, or None when it couldn't start."""
    if os.environ.get("FOREMAN_NO_BACKGROUND"):
        return None
    import subprocess
    try:
        return subprocess.Popen(["nice", "-n", "19", *argv], cwd=p.root, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True).pid
    except OSError:
        return None


def act(p, briefs=None, now=None, checks=None, spawn=_spawn, tell=notify):
    """Each hit's action run, or the user told, per the rules above: [(check, 'act' | 'tell', text)]."""
    now = time.time() if now is None else now
    briefs = c.load_briefs(p) if briefs is None else briefs
    path = os.path.join(p.dir, "selfcheck-act.json")
    try:
        with open(path, encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {}
    out, seen = [], set()
    for check in (CHECKS + LIVE if checks is None else checks):
        try:
            hit = check(p, briefs, now)
        except Exception:  # noqa: BLE001 — a check never costs the sweep
            hit = None
        if not hit:
            continue
        k = getattr(check, "__name__", "check")
        seen.add(k)
        s = st.setdefault(k, {})
        if s.get("first") is None:
            s["first"] = now
        say, action = getattr(hit, "tell", False), getattr(hit, "action", None)
        if action:
            day = [t for t in s.get("acted") or [] if now - t < 86400]
            if (not day or now - day[-1] >= ACT_BACKOFF_S) and len(day) < ACT_PER_DAY:
                pid = spawn(p, action)
                day.append(now)
                c.log_event(p, "self_act", data={"check": k, "cmd": c.plain(" ".join(action))[:200], "pid": pid})
                out.append((k, "act", str(hit)))
                say = say or pid is None
            s["acted"] = day
            say = say or (bool(day) and now - s["first"] >= ESCALATE_S)
        level = getattr(hit, "level", 0)
        if say and (now - (s.get("told") or 0) >= TELL_EVERY_S or level > s.get("level", -1)):
            tell(p, str(hit))
            s["told"], s["level"] = now, level
            c.log_event(p, "self_tell", data={"check": k, "text": c.plain(str(hit))[:300]})
            out.append((k, "tell", str(hit)))
    for k, s in st.items():
        if k not in seen:  # resolved: a new finding starts fresh; when it was last told still holds the day's limit
            st[k] = {key: s[key] for key in ("told", "level") if key in s}
    try:
        c.write_atomic(path, json.dumps(st))
    except OSError:
        pass
    return out
