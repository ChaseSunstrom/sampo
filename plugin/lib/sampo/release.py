"""fm release VERSION (T-1188): the release ritual as named steps with a checkpoint after each (state/release.json),
so `--resume` continues from the first unfinished one and `--dry-run` only prints the plan. Sampo's own repo only
(plugin/.claude-plugin/plugin.json at its root; release.py is a thin wrapper over this). --push merges into the
remote's main (origin by default) from a temporary worktree behind the active task's publish grant, which it spends
before the push. The first run's flags (--ui, --push, --remote, --no-check) are kept in release.json for a resume;
a skipped step is never recorded as done, and a release with skipped gates can't push."""
import codecs
import contextlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import types

if __name__ == "__main__":  # the canary worker runs this file: lib/sampo first on the path would shadow the stdlib (secrets)
    _here = os.path.dirname(os.path.realpath(__file__))
    sys.path[:] = [os.path.dirname(_here)] + [p for p in sys.path if os.path.realpath(p or ".") != _here]

from sampo import core as c
from sampo import killsafe

# FOREMAN_RELEASE_FM: tests point the gates and the canary at a stub fm
FM = [sys.executable, os.environ.get("FOREMAN_RELEASE_FM") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(os.path.dirname(__file__)))), "bin", "fm")]
STEPS = [("tree", "the working tree is clean"),
         ("check", "fm check passes (the full gates)"),
         ("bump", "plugin.json, both marketplace.json entries, and sampo-ui's version when it changed"),
         ("changelog", "CHANGELOG.md's Unreleased entries are headed VERSION"),
         ("commit", "commit 'Foreman VERSION' with the Sampo-Task trailer"),
         ("release-check", "fm release-check: doctor and demo in a temporary HOME"),
         ("push", "--push: merge into the remote's main (--no-ff) from a temporary worktree, push; spends the publish grant"),
         ("round", "fm round close: tag HEAD round-N"),
         # T-1199: never fatal, so it never holds the push; 12 projects one by one held 1.2.34's for ~35 min
         ("canary", "fm sentinel in every local project, 4 at a time, started detached (report: fm release --canary-status)")]
GATES = ("check", "release-check", "canary")
MARK = r"^(Sampo|Foreman) [0-9]+\.[0-9]+\.[0-9]+"  # a release commit's subject
SEMVER = r"\d+\.\d+\.\d+"


def _err(msg):
    from sampo import cli
    raise cli.UsageError(msg)


def _git(root, *a, ok=False):
    r = c._git(root, *a, timeout=300, proc=True)
    if r.returncode and not ok:
        _err(f"git {' '.join(a)} failed: {(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout


def _json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _dump(path, data):
    c.write_atomic(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


DESIGN_PJ = "plugins/foreman-design/.claude-plugin/plugin.json"  # T-1652: links to plugin/skills, so foreman's version


def _setver(path, v):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    c.write_atomic(path, re.sub(r'"version": "[^"]*"', lambda _: f'"version": "{v}"', text, 1))


def _grant(p):
    """The active task, when it holds the publish grant; else None."""
    b = c.active_brief_fast(p, p.lane)
    return b if b and "publish" in (b.meta.get("allow") or []) else None


def _land_marker():
    """T-5938: whether the land's marker (core.LAND_SHIP_ENV, or its SAMPO_ mirror) is set; both spellings are taken out of
    this process's environment either way (a plain `or` popped only the first that was set), so the ship's own test run
    inherits no claim to the land's standing grant."""
    got = [os.environ.pop(name, "") for name in (c.LAND_SHIP_ENV, "SAMPO_" + c.LAND_SHIP_ENV.partition("_")[2])]
    return any(got)


def _named_grant(p, tid, land=False):
    """T-2007: fm ship --task ID: that task (closed or active) when it holds the publish grant. T-5938: the standing
    ship's id (sampo ship auto on: a grant no push spends) only for the land's own ship (`land`: the marker the land puts
    on its detached and in-line ship, core.LAND_SHIP_ENV) and only while it is on; any other caller naming it is refused."""
    if tid == c.STANDING_SHIP:
        if not land:
            _err("the standing ship is the land's own ship's: it ships trunk after a train that merged, and no other "
                 "caller names it (a task's publish grant ships by sampo ship --task ID); nothing was pushed")
        return types.SimpleNamespace(id=tid, meta={"standing": True}) if c.standing_ship(p) else _err(
            "the standing ship is off (sampo ship auto on); nothing was pushed")
    b = c.find_brief(p, tid) or _err(f"no task {tid.upper()}; nothing was pushed")
    return b if "publish" in (b.meta.get("allow") or []) else _err(
        f"{b.id} has no publish grant (fm task set {b.id} --allow publish); nothing was pushed")


def _spend(p, tid, by):
    if tid == c.STANDING_SHIP:  # T-5938: a standing ship is never spent (sampo ship auto off ends it)
        return
    with c.lock(p.dir):
        b = c.find_brief(p, tid)
        if "publish" not in (b.meta.get("allow") or []):  # another release spent it since _grant looked
            _err("the publish grant was already spent; nothing was pushed")
        b.meta["allow"] = [x for x in b.meta.get("allow") or [] if x != "publish"]
        b.append_log(f"publish grant used by {by}")
        c.save_brief(p, b)
        c.log_event(p, "grant_spent", task=tid, data={"allow": "publish", "by": by})


def _gate(root, verb):
    if subprocess.run([*FM, verb], cwd=root).returncode:
        _err(f"fm {verb} failed")


def _ui_changed(root):
    last = _git(root, "log", "-1", "--format=%H", "-E", f"--grep={MARK}").strip()
    # T-5059: both folders (the link is one tracked entry, the move shows as changes under each): a pre-move last release asks for --ui once
    return bool(_git(root, "diff", "--name-only", f"{last}..HEAD" if last else "HEAD", "--",
                     *(f"mods/{n}" for n in _mod_names())).strip())


def _mod_names():
    from sampo import settings
    return settings.MOD_DIRS


def _mod_rel(root):
    """The UI mod's folder relative to the checkout, the real one (git refuses a path through a link)."""
    from sampo import settings
    return os.path.relpath(settings.mod_dir(root), root)


def bad_hour(events, hour, least=5):
    """T-1549: the user's historically bad hours: (failed, runs, overall share) when gates started at this local hour
    failed at least 40% of the time and at least twice as often as overall (5 runs in the hour and 20 in all at least),
    else None."""
    import datetime
    by, fails, total = {}, 0, 0
    for e in events:
        res = (e.get("data") or {}).get("results") if isinstance(e.get("data"), dict) else None
        try:
            h = datetime.datetime.fromisoformat(str(e.get("ts")).replace("Z", "+00:00")).astimezone().hour
        except ValueError:
            continue
        if not isinstance(res, list) or not res:
            continue
        bad = any(isinstance(r, dict) and r.get("exit") not in (0, None) for r in res)
        n, f = by.get(h, (0, 0))
        by[h], total, fails = (n + 1, f + bad), total + 1, fails + bad
    n, f = by.get(hour, (0, 0))
    if total < 20 or n < least:
        return None
    return (f, n, fails / total) if f / n >= 0.4 and f / n >= 2 * fails / total else None


def _bump(ctx):
    root, v, ui, old = ctx["root"], ctx["version"], ctx["ui"], ctx["old"]
    if ui is None and _ui_changed(root):
        _err("mods/sampo-ui changed since the last release: pass --ui VERSION")
    pj = os.path.join(root, "plugin", ".claude-plugin", "plugin.json")
    mk = os.path.join(root, ".claude-plugin", "marketplace.json")
    uj = os.path.join(root, _mod_rel(root), ".claude-plugin", "plugin.json")
    dj = os.path.join(root, DESIGN_PJ)
    data = _json(mk)  # everything read before anything is written
    if ui and not os.path.isfile(uj):
        _err("no mods/sampo-ui to give --ui to")
    meta = data.get("metadata") or {}
    if meta.get("version") == old:  # only what is at the old version moves, as release.py always did
        meta["version"] = v
    for e in data.get("plugins", []):
        name = str(e.get("name"))  # T-2879: sampo, sampo-design, sampo-ui (the foreman- names before)
        if name in {n + s for n in c.NAMESPACES for s in ("", "-design")} and e.get("version") == old:  # T-1652
            e["version"] = v
        elif name in {n + "-ui" for n in c.NAMESPACES} and ui:
            e["version"] = ui
    _setver(pj, v)
    if os.path.isfile(dj):
        _setver(dj, v)
    _dump(mk, data)
    if ui:
        _setver(uj, ui)


def _changelog(ctx):
    import datetime
    path = os.path.join(ctx["root"], "CHANGELOG.md")
    with open(path, encoding="utf-8") as f:
        text = f.read()
    if re.search(rf"(?m)^## {re.escape(ctx['version'])} ", text):
        return  # a resumed run
    if "## Unreleased\n" not in text:
        _err("CHANGELOG.md has no '## Unreleased' heading")
    from sampo import settings  # T-1549: a default this release changes is named in its notes
    snap = os.path.join(ctx["root"], "plugin", "defaults.json")
    try:
        old = _json(snap) if os.path.isfile(snap) else {}
    except ValueError:
        old = {}
    moved = [f"- Default changed: {m['key']} {json.dumps(m['old'])} → {json.dumps(m['new'])} (if you never set it, "
             "this changes what you see)\n" for m in settings.changed_defaults(old, mine=())]
    head = f"## Unreleased\n\n## {ctx['version']} — {datetime.date.today().isoformat()}\n" + "".join(moved)
    c.write_atomic(path, text.replace("## Unreleased\n", head, 1).replace(head + "\n", head, 1))
    _dump(snap, settings.defaults())  # what the next release compares with


def _commit(ctx):
    root, v = ctx["root"], ctx["version"]
    if _git(root, "log", "-1", "--format=%s").strip() != f"Foreman {v}":  # the subject is a protocol: resumes compare it, so it keeps the name old releases carry
        files = ["plugin/.claude-plugin/plugin.json", ".claude-plugin/marketplace.json", "CHANGELOG.md",
                 "plugin/defaults.json"]  # T-1549: the defaults this release ships, for the next one to compare
        if ctx["ui"]:
            files.append(os.path.join(_mod_rel(root), ".claude-plugin", "plugin.json"))
        if os.path.isfile(os.path.join(root, DESIGN_PJ)):
            files.append(DESIGN_PJ)
        b = c.active_brief_fast(ctx["p"], ctx["p"].lane)
        if os.path.isfile(os.path.join(root, "plugin", "defaults.json")):
            _git(root, "add", "--", "plugin/defaults.json")  # the first snapshot is a new file: commit -- needs it known
        # only these files: whatever else was staged between a stop and the resume stays out of the release
        _git(root, "commit", "-qm", f"Foreman {v}", *(["--trailer", f"{c.TRAILER}: {b.id}"] if b else []), "--", *files)
    ctx["state"]["sha"] = _git(root, "rev-parse", "HEAD").strip()  # what a later --push merges, not a moved HEAD


def _canary_log(p, v):
    return os.path.join(p.dir, "logs", f"canary-{v}.txt")


def _canary(ctx):
    """T-1362: the canary starts detached (own session, stdin closed, output in the project's logs/) so the release
    returns once pushed; the worker captures failures to each project's inbox and calls fm notify when it ends."""
    log = _canary_log(ctx["p"], ctx["version"])
    os.makedirs(os.path.dirname(log), exist_ok=True)
    with open(log, "wb") as f:
        proc = subprocess.Popen([sys.executable, os.path.abspath(__file__), "--canary-worker", ctx["version"], ctx["root"]],
                                stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.STDOUT, start_new_session=True)
    if os.environ.get("FOREMAN_NO_BACKGROUND"):  # tests: a detached child writing into a test's folder races its cleanup
        proc.wait()
    print(f"  canary (fm sentinel in every local project) started in the background; its report: {log} (fm release --canary-status)")


def _canary_status(p):
    logs = os.path.join(p.dir, "logs")
    found = [os.path.join(logs, f) for f in os.listdir(logs) if re.fullmatch(r"canary-.*\.txt", f)] if os.path.isdir(logs) else []
    if not found:
        print("no canary run yet (fm release --push starts one)")
        return
    last = max(found, key=os.path.getmtime)
    with open(last, encoding="utf-8", errors="replace") as f:
        text = f.read()
    print(f"{last}" + ("" if "canary done" in text else " (still running)") + "\n" + text.rstrip())


RERUN_S = 600  # T-1518: all the canary's load-flap reruns together


def _canary_worker(v, root):
    """`fm sentinel` in every local project fm knows: what an earlier task proved that fails there now. Never fatal."""
    try:
        listed = json.loads(subprocess.run([*FM, "projects", "--json"], capture_output=True, text=True,
                                           errors="replace", timeout=120).stdout or "{}").get("projects", [])
    except (ValueError, OSError, subprocess.TimeoutExpired):
        listed = []
    print(f"canary: fm sentinel in {len(listed)} project(s)", flush=True)

    def one(x, timeout=1800):
        """(the sentinel's exit code, None when it couldn't run or timed out; the report line)"""
        try:
            r = subprocess.run([*FM, "sentinel"], cwd=x["root"], capture_output=True, text=True, errors="replace",
                               timeout=timeout)
            first = (r.stdout.strip().splitlines() or ["(no output)"])[0]
            return r.returncode, f"  {'✗' if r.returncode else '✓'} {x['project']}: {first}"
        except (OSError, subprocess.TimeoutExpired) as e:
            return None, f"  ✗ {x['project']}: sentinel couldn't run ({type(e).__name__})"

    def load(hl):
        return f"load {hl[0]:.1f} on {hl[1]} CPUs" if hl else "load unknown"

    from concurrent.futures import ThreadPoolExecutor
    failed = []
    todo = [x for x in listed if x.get("exists")]
    with ThreadPoolExecutor(4) as pool:
        for x, (code, line) in zip(todo, pool.map(one, todo)):  # in project order, as each is ready
            print(line, flush=True)
            if code != 0:
                failed.append((x, line.strip(), code, c.host_load()))
    # T-1518: a check that failed (a real exit, not a timeout) while the host was loaded (load at least its CPUs) gets
    # one rerun alone after the pool, all reruns within RERUN_S; passing there is a load flap, reported with both
    # results and captured like a failure, so an intermittent regression still lands in the inbox. ponytail: "alone"
    # is the canary's own load gone, not an idle machine; wait for the load to fall first if flaps hide real failures.
    still, flaps, skipped, end = [], [], [], time.monotonic() + RERUN_S
    for x, line, code, hl in failed:
        if code is None or not hl or hl[0] < hl[1]:
            still.append((x, line))
            continue
        if end - time.monotonic() <= 0:
            still.append((x, line))
            skipped.append(x["project"])
            continue
        again_code, again = one(x, timeout=end - time.monotonic())
        again = f"{again.strip().split(': ', 1)[-1]} ({load(c.host_load())})"
        if again_code == 0:
            flaps.append((x, line))
            print(f"  ~ {x['project']}: load flap: failed beside the others ({line.split(': ', 1)[-1]}) ({load(hl)}), "
                  f"passed alone ({again})", flush=True)
        else:
            still.append((x, line))
            print(f"  ✗ {x['project']}: failed alone too: {again}", flush=True)
    if skipped:
        print(f"  (no rerun for {', '.join(skipped)}: the reruns' {RERUN_S:g} s were spent)", flush=True)
    failed = still
    from sampo import eco
    for x, title in [(x, f"Canary after Sampo {v}: {line[:200]}") for x, line in failed] + [
            (x, f"Canary load flap after Sampo {v}: {line[:200]}") for x, line in flaps]:
        try:  # a project's past check failing now, or only under load: an item in that project's inbox
            q = c.find_project(x["root"])
            if q:
                eco.capture_once(q, title, "FIX", "discovered")
        except Exception as e:  # never fatal: the report above still names it
            print(f"  (couldn't capture for {x['project']}: {type(e).__name__})", flush=True)
    flapped = f"{len(flaps)} load flap(s): {', '.join(x['project'] for x, _ in flaps)}" if flaps else ""
    print(f"canary done: {len(failed)} of {len(todo)} project(s) failing" + (f", {flapped}" if flaps else ""), flush=True)
    if failed or flaps:
        try:
            from sampo import serve
            p = c.find_project(root)
            serve._notify(p, f"Sampo {v} canary: " + "; ".join(
                ([f"{len(failed)} project(s) failing: " + ", ".join(x["project"] for x, _ in failed)] if failed else [])
                + ([flapped] if flaps else [])))
        except Exception:
            pass


_NEED_GRANT = ("--push needs the active task's publish grant: fm ask ID publish (the user's yes), or, where the user "
               "has given standing permission to release, fm task set ID --allow publish")


def _direct(p, root, commits):
    """T-4805: of commits, the ones no recorded lane landing carried: a lane_landed event (written by `sampo lane merge` and
    the merge train after the landing, never read from a commit message) names the base and tip it moved the trunk
    between, and every commit in base..tip, the lane's own included, was judged by that landing's gate. Events older than the
    oldest commit (less a day for clock skew) can't name it, so only newer ones are walked."""
    if not commits:
        return []
    stamps = [int(x) for x in _git(root, "log", "--no-walk", "--format=%ct", *commits, ok=True).split() if x.isdigit()]
    since = c.iso(min(stamps) - 86400) if stamps else ""
    landed = set()
    for e in c.ledger_events(p, ("lane_landed",), since=since):
        d = e.get("data") or {}
        if isinstance(d.get("base"), str) and isinstance(d.get("tip"), str):
            landed.update(_git(root, "rev-list", f"{d['base']}..{d['tip']}", ok=True).split())
    return [s for s in commits if s not in landed]


PUSH_CACHE = "selfgate-base-push.json"  # T-4805: the hold's base cache, apart from the lane train's selfgate-base.json
LANDED_BY = "def log_landed("  # T-4805: the line whose arrival in lanes.py began the lane_landed records


def _listed(root, s):
    """(first parent, the files s changed against it, renames listed as the delete and the add: a moved guard is seen) or None
    when git can't say (no first parent, as a root commit, or a failed command): the caller holds, as lanes.lane_files
    returning None does (T-4783)."""
    parent = c._git(root, "rev-parse", "--verify", "-q", f"{s}^1", timeout=60, proc=True)
    if parent.returncode or not parent.stdout.strip():
        return None
    parent = parent.stdout.strip()
    got = c._git(root, "diff", "--name-only", "--no-renames", "-z", parent, s, timeout=120, proc=True)
    if got.returncode:
        return None
    return parent, [f for f in got.stdout.split("\0") if f]


def _before_records(root, commits):
    """T-4805: the commits reachable from the first-parent commit that added lanes.py's log_landed (the landing of the record
    this hold reads): lanes landed before it left no record, and re-judging all of them at the first ship after would be
    unbounded; they count as judged. Nothing when the history doesn't show it (then every direct commit is judged).
    The cutoff is a trust boundary: it is the landing of T-4805 itself (the train's merge of this lane, judged by
    land_holds); commits from before it are trusted as landed through the gates of their day."""
    first = _git(root, "log", "--first-parent", "--reverse", "--format=%H", f"-S{LANDED_BY}", commits[-1], "--",
                 "plugin/lib/sampo/lanes.py", ok=True).split()
    return set(_git(root, "rev-list", first[0], ok=True).split()) if first else set()


def _passed(p, root, shas):
    """T-6031: of shas, the ones with a signed self_change_passed record under today's gate key (selfgate.passed): a hold
    judged them and the gate, the user's yes or the standing yes (while it stands) passed them. A refusal leaves no such
    record, a changed gate matches none, and a line written without the user's key is not read. The window starts a day
    before the oldest commit (a pass is recorded after its commit is made)."""
    from sampo import selfgate
    if not shas:
        return set()
    stamps = [int(x) for x in _git(root, "log", "--no-walk", "--format=%ct", *shas, ok=True).split() if x.isdigit()]
    if not stamps:  # git can't date them: nothing is skipped
        return set()
    return selfgate.passed(p, shas, since=c.iso(min(stamps) - 86400))


def _judge(p, b, root, todo):
    """T-6031: [(sha, rows)] for the commits of todo (sha, parent, files) the gate holds, judged up to train.hold_jobs() at
    a time (SAMPO_HOLD_JOBS, the lane train's knob): each worker has a scratch worktree of its own (checked out at a
    commit's parent in turn) and all share the one PUSH_CACHE file (selfgate._save writes it atomically, a file per
    thread). The hook bench inside a judgement is serialised machine-wide by selfgate._paired's lock and runs with no
    other worker's replay beside it (selfgate.QUIET, so the timings aren't skewed); the rest overlaps.
    A judge that can't run, a checkout that fails or a commit no worker reached is a hold, never a pass."""
    import queue
    import threading
    from sampo import selfgate, train
    if not todo:
        return []
    work, rows_of = queue.Queue(), {}
    for t in todo:
        work.put(t)

    def worker(i, wt):
        cache = PUSH_CACHE
        while True:
            try:
                s, parent, files = work.get_nowait()
            except queue.Empty:
                return
            try:
                _git(wt, "checkout", "-q", "--detach", parent)
                rows = selfgate.gate(p, b, wt, s, files, cache, record=True)
            except Exception as e:  # noqa: BLE001 — a judge that can't run is a hold, never a pass
                rows = [f"the self-change gate couldn't run ({type(e).__name__}: {c.fit(str(e), 160)})"]
            rows_of[s] = rows
    with c.scratch_dir("sampo-hold-", root) as tmp:  # T-4735: the owned scratch root
        made = []
        try:
            for i in range(min(train.hold_jobs(), len(todo))):  # added one at a time: git's worktree admin is not for concurrent adds
                wt = os.path.join(tmp, f"hold{i}")
                _git(root, "worktree", "add", "-q", "--detach", wt, todo[i][1])
                made.append(wt)
            threads = [threading.Thread(target=worker, args=(i, wt), daemon=True) for i, wt in enumerate(made)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        finally:
            for wt in made:
                _git(root, "worktree", "remove", "--force", wt, ok=True)
    out = []
    for s, _, _ in todo:
        rows = rows_of.get(s, ["no judge reached it: held rather than passed"])
        if rows:
            out.append((s, rows))
    return out


def _push_hold(p, b, root, commits, by):
    """T-4805: refuse the push when a commit in it was made straight on the trunk (no recorded lane landing carried it)
    and touches a file the self-change gate judges: each such commit is judged against its first parent, as `fm lane
    merge` judges a lane, up to train.hold_jobs() at a time, each in a scratch worktree at its parent. The same ask-and-yes
    passes it (fm ask ID core, after the refusal of this commit; one yes covers every commit refused before it), the
    standing yes only quality rows. A gate that can't run or a listing git can't give holds. A direct commit touching no
    judged file runs nothing; a lane-landed one was judged when it landed, and one from before landings were recorded
    counts as judged. Nothing is pushed and the grant is kept. T-6031: a commit that passed (the gate's pass, a yes, or the
    standing yes while it stands) under today's gate is recorded signed and not judged by a later hold: the hold's push_hold_judged
    event says how many it judged and skipped and its seconds. The base half of the judge is cached in its own file (a
    different parent per commit would miss the train's)."""
    from sampo import selfgate
    todo, held = [], []
    direct = _direct(p, root, commits)
    if direct:
        old = _before_records(root, commits)
        direct = [s for s in direct if s not in old]
    for s in direct:
        got = _listed(root, s)
        if got is None:
            held.append((s, ["couldn't list the files it changed (no first parent, or git failed): held rather than passed"]))
        elif any(selfgate._judged(f) or f in selfgate.CASES for f in got[1]):
            todo.append((s, got[0], got[1]))
    t0, skipped = time.monotonic(), 0
    if todo:  # T-6031: a commit the gate passed under today's gate is not judged again
        done = _passed(p, root, [s for s, _, _ in todo])
        skipped = sum(s in done for s, _, _ in todo)
        todo = [t for t in todo if t[0] not in done]
        held += _judge(p, b, root, todo)
    if direct:
        c.log_event(p, "push_hold_judged", task=b.id, data={"by": by, "judged": len(todo), "skipped": skipped,
                                                            "held": len(held), "seconds": round(time.monotonic() - t0, 1)})
    if held:
        what = "; ".join(f"{s[:8]} {c.fit(c.plain(_git(root, 'log', '-1', '--format=%s', s, ok=True).strip()), 60)}: "
                         + c.joinmore(rows, 3, "; ") for s, rows in held[:3])
        _err(f"the self-change gate holds {c.joinmore([s[:8] for s, _ in held], 5)}, made straight on the trunk with no lane "
             f"landing them: {what}; nothing was pushed and the grant is kept. The user's yes passes it (a guard row, a "
             f"removed case or a gate that couldn't run is theirs alone; the standing yes covers only footprint, verbs "
             f"and hook rows): fm ask {b.id} core --why \"…\", then {by} again")


def _release_commits(root, main, sha):
    """T-4805: the first-parent commits of the release sha that main lacks and whose patch main doesn't already have
    (git cherry): fm ship lands its picks as cherry-picks under other shas, and those were held at the ship. A commit a
    later `fm ship <commit>` left out is not one of them, so the release's merge pushing it is judged here."""
    mine = _git(root, "rev-list", "--first-parent", "--reverse", sha, "--not", main, ok=True).split()
    if not mine:
        return []
    have = {ln[2:].strip() for ln in _git(root, "cherry", main, sha, ok=True).splitlines() if ln.startswith("- ")}
    return [s for s in mine if s not in have]


def _push(ctx):
    root, v, p, remote = ctx["root"], ctx["version"], ctx["p"], ctx["remote"]
    b = _grant(p)
    if not b:
        _err(_NEED_GRANT)
    sha = ctx["state"].get("sha") or _err(f"no release commit recorded for {v}: fm release --abandon, then release again")
    _git(root, "fetch", "-q", remote, "main")
    with c.scratch_dir("sampo-rel-", root) as tmp:  # T-4735
        wt = os.path.join(tmp, "rel")
        _git(root, "worktree", "add", "-q", "--detach", wt, "FETCH_HEAD")
        main = _git(wt, "rev-parse", "HEAD").strip()
        try:
            if c._git(wt, "merge", "-q", "--no-ff", sha, "-m", f"Foreman {v}", timeout=300, proc=True).returncode:
                _git(wt, "merge", "--abort", ok=True)
                _land_over_picks(wt, sha, v)  # T-2598: ship cherry-picked the branch onto main
            landed = _git(wt, "rev-parse", "HEAD").strip()
            say_no_list("release")  # T-4785
            _refuse_private(wt, _git(wt, "rev-list", f"{main}..{landed}").split(), landed)  # T-3529: as fm ship does
            _push_hold(p, b, root, _release_commits(root, main, sha), "fm release --push")  # T-4805: a direct commit is judged
            _push_main(p, b.id, landed, remote, "fm release --push")
        finally:
            _git(root, "worktree", "remove", "--force", wt, ok=True)
    print(f"  pushed Sampo {v} to {remote} main (the publish grant is spent)")
    clean = not _git(root, "status", "--porcelain", ok=True).strip()
    if clean and not c._git(root, "merge-base", "--is-ancestor", "HEAD", landed, timeout=60, proc=True).returncode:
        if not c._git(root, "merge", "-q", "--ff-only", landed, timeout=300, proc=True).returncode:
            print("  the branch follows the landed release, so the next one merges from main's history")


def _land_over_picks(wt, sha, v):
    """T-2598: fm ship lands the branch's commits on main as cherry-picks, so their histories split while their content
    matches, and a --no-ff merge conflicts. When main's version of every file it differs in, outside its CI-ignored
    data, is one the branch once had, commit the release tree with main's ignored files laid over it, parents (main,
    release), in the worktree wt (at main); otherwise refuse, naming the file."""
    main = _git(wt, "rev-parse", "HEAD").strip()
    ignored = _ci_ignored(wt, main)
    diff = [x for x in _git(wt, "diff", "--name-only", main, sha).splitlines() if x]
    data = [x for x in diff if any(g.match(x) for g in ignored)]
    for f in (x for x in diff if x not in data):
        blob = c._git(wt, "rev-parse", "-q", "--verify", f"{main}:{f}", timeout=30, proc=True).stdout.strip()
        if not blob:
            continue  # the branch added it: nothing of main's is lost
        seen = re.findall(r"(?m)^:\d+ \d+ \w+ (\w+) ", _git(wt, "log", "--no-abbrev", "--format=", "--raw", sha, "--", f))
        if blob not in seen:
            _err(f"main has a change to {f} the release branch never had; nothing was pushed: bring it into the "
                 f"branch, then fm release {v} --resume")
    _git(wt, "read-tree", "--reset", "-u", sha)
    for f in data:
        if c._git(wt, "rev-parse", "-q", "--verify", f"{main}:{f}", timeout=30, proc=True).returncode:
            _git(wt, "rm", "-q", "--ignore-unmatch", "--", f)  # main removed its data file
        else:
            _git(wt, "checkout", main, "--", f)  # main's newer data wins
    new = _git(wt, "commit-tree", _git(wt, "write-tree").strip(), "-p", main, "-p", sha, "-m", f"Foreman {v}").strip()
    _git(wt, "reset", "-q", "--hard", new)
    print(f"  {v}: main held the branch's commits as cherry-picks; landed the release tree over it with main's data")


def private_where(commits, files):
    """Where _private_hits found a term, by commit and file, never the term (ship's refusal, and lane merge's: T-3999)."""
    return " and ".join(([f"commit{'s' if len(commits) > 1 else ''} " + c.joinmore([s[:8] for s in commits], 5)]
                         if commits else []) + ([f"file{'s' if len(files) > 1 else ''} " + c.joinmore(files, 5)] if files else []))


def _refuse_private(root, picks, tip, hits=None):
    """T-3529: refuse when the commits picks (messages, added lines against the first parent) or tip's tree hold one of
    the user's private terms (sampo private) or a value from their secrets file, naming the commits (the user's own
    shas: a scratch tree's re-picks differ) and files, never the term. hits: _private_hits' answer, already asked."""
    commits, files = hits or _private_hits(root, picks, tip)
    if commits or files:
        _err(f"a private term (sampo private list) or a secrets-file value is in {private_where(commits, files)}; nothing "
             "was pushed: redact it (sampo sync export for .sampo/; amend or rebase a commit that carries it), then ship again")


def say_no_list(who):
    """T-4785 (d): one stderr line when the user has set no private terms (T-4783 said it only in the train): the scan
    of `who` looks for the secrets-file values at most. Silent when a list is set; never the path or a term. A list that
    is there with no usable term (empty, blank lines, every term too short) is said apart, as a notice too."""
    status = c.private_list()[1]
    if status not in ("none", "empty"):
        return
    import sys
    print(f"sampo: {who}: the private-term check "
          + ("had no term list (none is set)" if status == "none" else "had a term list with no usable term "
             "(it is empty, or every term in it is too short or has a space)")
          + (", so only the secrets-file values were scanned" if private_values() else "")
          + ": sampo private add TERM sets one", file=sys.stderr, flush=True)


def private_values():
    """T-4783: the secrets-file values the private-term scan looks for besides the terms: a secret-named line only (a base
    URL or a port there is no secret), eight characters or more. Empty when the file is unset, open to others or unreadable."""
    from sampo import models, sandbox
    return [v for k, v in models.secret_pairs() if len(v) >= 8 and sandbox.secret(k)]


def _private_hits(root, picks, tip, paths=None):
    """(commits, files) of _refuse_private's scan, files named redacted; paths: only those files of tip's tree (T-3551:
    the tower checks the files its commit touches, so a file already on main can't hold its pushes back)."""
    try:
        terms, values = c.private_terms(), private_values()
    except c.PrivateTermsUnreadable:  # T-4804: 'no terms' from a list that can't be read would push unscanned
        _err(c.PRIVATE_UNREADABLE + "; nothing was pushed: repair or remove the list in the state folder, then ship")
    if not terms and not values:
        return [], []
    def bad(text):
        return c.has_private(text, terms) or any(v in text for v in values)
    commits = [s for s in picks
               if bad(_git(root, "log", "-1", "--format=%B", s)) or bad("\n".join(
                   ln for ln in _git(root, "diff", "--no-color", "--no-ext-diff", "-U0", f"{s}^", s).splitlines()
                   if ln.startswith("+") and not ln.startswith("+++ ")))]
    needles = [max(t.removeprefix("*.").split("*"), key=len) for t in terms] + values  # fixed strings; bad() decides
    # paths: those files only, unless one holds a needle (a pathspec is argv too: then the whole tree, filtered below)
    spec = [] if paths is None or any(n.lower() in f.lower() for f in paths for n in needles) \
        else [":(literal)" + f for f in sorted(paths)]
    tmp = tempfile.mkdtemp(prefix="sampo-private-")  # argv shows in ps: the needles go in a 0600 file in a 0700 dir
    try:
        fd, pats = tempfile.mkstemp(dir=tmp)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(needles) + "\n")
        r = c._git(root, "grep", "-l", "-z", "-I", "-i", "-F", "-f", pats, tip, "--", *spec, timeout=300, proc=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if r.returncode not in (0, 1):
        # T-4785 review: the exit status only; git's stderr may quote a path, a pathspec or a line of a blob (a term)
        _err(f"git grep failed (exit {r.returncode}), so the private-term scan did not run; nothing was pushed")
    blobs = dict((ln.partition("\t")[2], ln.split()[2]) for ln in _git(root, "ls-tree", "-r", "-z", tip).split("\0") if ln)
    blobs = {f: b for f, b in blobs.items() if paths is None or f in paths}
    hits = {x.split(":", 1)[1] for x in r.stdout.split("\0") if ":" in x}  # by blob id: a path may hold a term too
    files = sorted({f for f in hits if f in blobs and bad(_git(root, "cat-file", "blob", blobs[f]))} | set(filter(bad, blobs)))
    def shown(f):  # a path that holds a term or a value is named redacted
        for v in values:
            f = f.replace(v, "[REDACTED]")
        return c.redact_local(f, terms)
    return commits, [shown(f) for f in files]


def private_lines(root, tip, files, cap=5):
    """T-4783: {file: [line numbers]} of the lines of tip's `files` that hold a private term or a secrets-file value:
    numbers only, never the text (the files come from _private_hits, a path that holds a term already redacted: it
    reads no blob, so it has no lines). At most `cap` files and `cap` numbers each."""
    from sampo import models, sandbox
    try:
        terms = c.private_terms()
    except c.PrivateTermsUnreadable:  # T-4804: a local diagnostic: no line numbers from a list that can't be read
        return {}
    values = [v for k, v in models.secret_pairs() if len(v) >= 8 and sandbox.secret(k)]
    out = {}
    for f in list(files)[:cap]:
        r = c._git(root, "show", f"{tip}:{f}", timeout=60, proc=True)
        if r.returncode or "\0" in r.stdout:  # no such path (a redacted name) or a binary
            continue
        nums = [i for i, ln in enumerate(r.stdout.splitlines(), 1) if c.has_private(ln, terms) or any(v in ln for v in values)]
        if nums:
            out[f] = nums[:cap]
    return out


def _main_at(p, remote):
    """T-5980: the sha `remote`'s main is at (git ls-remote, 15s): "" when the remote has no main, None when it can't be
    told (unreachable, a timeout, git missing). The one question every give-back asks the remote."""
    try:
        got = c._git(p.root, "ls-remote", remote, "refs/heads/main", timeout=15, proc=True)
    except Exception:  # noqa: BLE001 — unknown is None, never a guess
        return None
    if got.returncode != 0:
        return None
    return (got.stdout.split() or [""])[0]


def _push_landed(p, remote, sha):
    """T-5938 review: whether `remote`'s main is at `sha` now (git ls-remote, 15s). False when it is not; True too when it
    can't be told (an unreachable remote): after an interrupted push the grant is then left spent, never doubled."""
    at = _main_at(p, remote)
    return at is None or at == sha


def _push_main(p, tid, sha, remote, by):
    """Push sha to remote's main from the repo (a relative remote URL resolves there). The grant is spent first, so one
    yes is one push even for two racing callers, and given back if git push fails (T-1943).
    T-4805 exempt: the primitive; its two callers, ship and _push, hold first (_push_hold), and a test scans for that."""
    _spend(p, tid, by)
    try:
        _git(p.root, "push", "-q", remote, f"{sha}:refs/heads/main")
    except BaseException as e:  # T-5938 review: an interrupt (Ctrl-C) between the spend and the push ends here too
        if tid == c.STANDING_SHIP:  # T-5938: nothing was spent
            raise
        # an interrupt, or a push git's runner ended on its timeout (T-5980: its child died, so nothing says the push did not
        # land), can come after the push: one yes, one push. A push git itself refused landed nothing: given back
        if (not isinstance(e, Exception) or "timed out after" in str(e)) and _push_landed(p, remote, sha):
            raise
        with c.lock(p.dir):
            b = c.find_brief(p, tid)
            b.meta["allow"] = [*(b.meta.get("allow") or []), "publish"]
            b.append_log(f"publish grant given back: {by}'s push failed")
            c.save_brief(p, b)
        raise


_MOVED = re.compile(r"cannot lock ref|incorrect old value|fetch first|non-fast-forward|stale info")  # T-2555
SHIPPED = "refs/foreman/shipped"  # T-1945: the last commit fm ship landed; default picks start after it
SHIPPED_TIP = "refs/foreman/shipped-tip"  # T-2527: the main commit that ship pushed (its sha differs once re-picked)


def _glob_re(g):
    """A GitHub paths glob as a regex: ** spans folders, * stays inside one."""
    return re.compile("".join(".*" if x == "**" else "[^/]*" if x == "*" else re.escape(x)
                              for x in re.split(r"(\*\*|\*)", g.strip().strip("'\""))) + r"\Z")


def _ci_ignored(root, rev):
    """T-2572: the paths-ignore globs in rev's .github/workflows: files CI never tests, so main moving only in them
    can't change what a retest would show. A repo without any gives [] (every other move refuses)."""
    names = c._git(root, "ls-tree", "--name-only", rev, ".github/workflows/", timeout=30, proc=True)
    out = []
    for name in names.stdout.split() if names.returncode == 0 else []:
        if name.endswith((".yml", ".yaml")):
            text = c._git(root, "show", f"{rev}:{name}", timeout=30, proc=True).stdout or ""
            for m in re.finditer(r"(?m)^([ \t]*)paths-ignore:[^\n]*\n((?:\1[ \t]+-[^\n]*\n?)+)", text):
                out += [x.split("#")[0].strip() for x in re.findall(r"-([^\n]*)", m.group(2))]
    return [_glob_re(g) for g in out if g]


def _suite(cmd):
    """T-3666: a test command's words without its job count: -j 6 runs the same tests as -j 2 or none."""
    import shlex
    try:
        words = shlex.split(str(cmd or ""))
    except ValueError:
        return str(cmd)
    out, skip = [], False
    for w in words:
        if skip:
            skip = False
        elif w in ("-j", "--jobs"):
            skip = True
        elif not re.fullmatch(r"-j\d+|--jobs=\d+", w):
            out.append(w)
    return shlex.join(out)


def _verified(p, root, said, base, last, at="HEAD"):
    """T-3666: the newest commit on HEAD's first-parent line that main and the last ship lack, whose tree the post-merge
    verify passed in full (lanes.pm_full_pass) with this ship's suite, in this environment, within the run cache's
    window: (sha, how to say the pass) or (None, None). The default fm check runs lint gates too: never the suite."""
    from sampo import cli, lanes
    if not said:
        return None, None
    fresh = lambda at: (lambda d: d is not None and d < cli.CACHE_DAYS)(c.age_days(str(at)))  # noqa: E731
    passes = {str(x.get("tree")): x for x in lanes.pm_full_passes(p)
              if _suite(x.get("cmd")) == _suite(said) and x.get("env") == c.env_id() and fresh(x.get("at"))}
    if not passes:
        return None, None
    line = _git(root, "log", "--first-parent", "-n", "500", "--format=%H %T", at, f"^{base}",
                *([f"^{last}"] if last else []), ok=True).split()
    for sha, tree in zip(line[::2], line[1::2]):
        x = passes.get(tree)
        if x and not (last and c._git(root, "merge-base", "--is-ancestor", last, sha, timeout=60, proc=True).returncode):
            return sha, f"the post-merge verify's full run on {str(x.get('sha'))[:9]} ({x.get('at')})"
    return None, None


def _settle_verify(p, root, target, tested, test, hit, code, tail):
    """T-6012: the ship's own suite, run on `tested` (the tree it ran on), settles the post-merge verify's pending records
    (lanes.pm_ship_settle) when it is a full run on the very tree of `target`: the project's own suite command (not the gates'
    fm check), no reuse (`hit`: it ran nothing), the tree equal to target's (the ship tests main's tree plus the picks: when
    main holds more, the run says nothing of target). `code` is the run's final exit. Returns the shas covered; never
    raises: the ship stands, the verify runs what is left."""
    from sampo import lanes
    try:
        if hit or not test:
            return []
        import shlex
        if _suite(shlex.join(test)) != _suite(lanes.pm_cmd(root, uncached=True, p=p)):
            return []
        if not tested or _git(root, "rev-parse", f"{target}^{{tree}}", ok=True).strip() != tested:
            return []
        return lanes.pm_ship_settle(p, root, target, code, tail)
    except Exception as e:  # a convenience
        print(f"post-merge verify settle skipped: {c.fit(c.plain(str(e)), 120)}", flush=True)
        return []


def _gated_pass(p, root, wt, base, said, target="HEAD", full=None):
    """T-3468: the gates' pass (a train's or a full fm check's) that already covers the tree about to be pushed, or
    None. It holds when the ship's test is the gates' own (the default fm check, or one of the configured checks),
    HEAD's code tree passed every configured gate, the shipped commits change no CI workflow (they supply the ignore
    globs), and the pushed tree differs from HEAD's in no CI workflow and no Python file under plugin/. After a train
    the ship ran the whole suite again on the same code (14-20 min at load). Returns (the pass or None, the differing
    files besides CI-skipped ones (paths-ignore: the tower's shards) and the sync mirror's notes): T-3533, the tower's
    stats or a CHANGELOG.md main changed, whose affected tests the caller runs instead of the suite (17-25 min).
    T-3666: `target`, the commit shipped (HEAD or a verified one); `full`, the post-merge verify's pass on its tree
    (_verified matched the suite), which stands in for the gates'."""
    import shlex
    from sampo import cli
    checks = list(c.read_meta(p).get("checks") or [])
    norm = lambda s: shlex.join(shlex.split(s))  # noqa: E731
    head = _git(root, "rev-parse", f"{target}^{{tree}}").strip()
    pushed = _git(wt, "rev-parse", "HEAD^{tree}").strip()
    if not head or not pushed or not full and (not checks or (said and norm(said) not in {norm(x) for x in checks})):
        return None, []
    if _git(wt, "diff", "--name-only", base, "HEAD", "--", ".github").strip():
        return None, []
    ignored = _ci_ignored(wt, "HEAD")
    rest = [f for f in _git(root, "diff", "--name-only", "--no-renames", head, pushed).splitlines()
            if not ((f.startswith(c.MIRROR_DIRS) and f.endswith(".md")) or any(g.match(f) for g in ignored))]
    if any(f.startswith(".github/") or (f.startswith("plugin/") and f.endswith(".py")) for f in rest):
        return None, []
    if full:
        return full, rest
    hit = cli._cached_pass(p, checks, head[:12], (), c.code_tree(root, head))
    return (f"the gates' pass ({hit})" if hit else None), rest


def _run_test(p, wt, base, env, test, log, run_id=None):
    """The ship's own run in the scratch tree, streamed: the affected tests first, then `test`. (exit code, the last
    1 MB of its output). T-5046: `run_id` is stamped on the record its tests/run.py leaves (this run's alone, not the
    affected stage's), so a refusal reads the failing tests there."""
    _affected_first(p, wt, base, env)  # T-2323: a red linked test stops the ship in minutes, not after the suite
    env = {**env, c.RUN_ID_ENV: run_id} if run_id else env
    env = {**env, c.ORIGIN_ENV: "ship"}  # T-6006: the `sampo check` it runs is metered as the ship's; its run stays full (no reuse)
    with open(log, "wb") as out, open(log, "rb") as rd:
        try:
            proc = subprocess.Popen(c.nice_argv(test), cwd=wt, stdin=subprocess.DEVNULL, env=env, stdout=out,
                                    stderr=subprocess.STDOUT)  # T-4197: a ship's run yields to the user's session
        except OSError as e:
            _err(f"--test {test[0]!r} couldn't run ({e.strerror}); nothing was pushed")
        dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while True:
            done = proc.poll() is not None
            chunk = rd.read()
            if chunk:
                print(dec.decode(chunk), end="", flush=True)
            if done:
                break
            time.sleep(0.1)
        rd.seek(max(0, os.path.getsize(log) - 1_000_000))  # the last 1 MB, for the baseline capture
        return proc.returncode, rd.read().decode("utf-8", errors="replace")


RED_RERUN_S = 900  # the red classes alone, on a quiet machine
LOAD_WAIT_S = 60  # c.wait_for_load never waits over this (T-3144)


def _rerun_timeout():
    """T-5985: the rerun alone's run timeout: RED_RERUN_S scaled by the host's load (T-4197), but never past what the land
    allows the "rerun" phase (it kills the ship at that bound) less the load wait before it and a margin: a heavy load
    must end the rerun itself, with its own line, before the land ends the whole ship."""
    room = ship_caps()["rerun"] - LOAD_WAIT_S - 30
    return max(1.0, min(c.load_scaled(RED_RERUN_S), room))


def _rerun_red(wt, out, code, env, said=None):
    """T-3435: the classes a red run names, rerun alone after a load wait (the post-merge suite's helpers, T-3216).
    Returns the flaky classes when every one is green alone, None when any is still red (or can't run alone).
    T-4739: `said` (a list) gets what happened to the rerun, for the refusal that follows a None."""
    from sampo import lanes
    said = [] if said is None else said
    if code == 124:
        said.append("the run timed out, so it was not rerun alone")
        return None
    classes = lanes.pm_classes(out, code)
    again = lanes.pm_rerun_cmd(wt, classes)
    if not again:
        said.append("the failure could not be rerun alone (no class named, or no runner for one)")
        return None
    c.wait_for_load("sampo ship", out=sys.stdout)
    print(f"{', '.join(classes)} failed in the full run; rerunning alone", flush=True)
    if c.run_command(wt, again, _rerun_timeout(), env=env, niced=True, origin="ship")[0]:  # T-4197, T-5985
        said.append("the rerun alone also failed")
        return None
    print(f"flaky (red in the full run, green alone): {', '.join(classes)}", flush=True)
    return classes


def _red_names(out):
    """T-4739: the classes a red full run names (run.py's `failed class:` block, else the failing tests' classes)."""
    listed = re.findall(r"(?m)^  failed class: ([\w.]+)\s*$", out or "")
    if listed:
        return sorted(set(listed))
    return sorted({t.rpartition(".")[0] or t for t in c.failing_tests(out)})  # unittest's mod.Class.test, else the name


def _keep_output(p, tail, cmd):
    """T-4739: the last 1 MB of a refused ship's test run, in the project's state folder (the scratch tree it ran in is
    gone when the ship ends): its path, or "" when it can't be written. One file, the newest refused run's."""
    path = os.path.join(p.dir, "ship-test.log")
    try:
        c.write_atomic(path, f"{c.now()} the test run of a refused ship: {c.fit(c.plain(cmd), 300)}\n{tail}")
    except OSError:
        return ""
    return path


def _baseline_note(p, got):
    """T-4739: what the baseline capture said, as a clause for the refusal. A task this run filed is 'recorded in T-N';
    one already open (the capture only found it) is named 'already open as T-N' with its age when over a day: it was
    never this run's doing, and a three-day-old task's text can list other tests."""
    for ln in got:
        if m := re.search(r"\bbaseline red: captured (T-\d+)\b", ln):
            return f"baseline recorded in {m.group(1)}"
        if m := re.search(r"\bbaseline still red: (T-\d+)\b", ln):
            b = c.find_brief(p, m.group(1))
            days = c.age_days(b.meta.get("created")) if b else None
            return f"baseline already open as {m.group(1)}" + (f" ({int(days)} days old)" if days and days >= 1 else "")
    return ""


SHOW_TESTS, EXC_WIDTH = 3, 100  # T-5046: the failing tests a refusal names with their exception, and an exception's width


def _red_tests(wt, tail, cmd, run_id):
    """T-5046: [(class, test, its exception line)] a red full run failed, so a refusal names them without a second run: the
    record the run's own tests/run.py left under `run_id` (it survives a cut or piped output), else the FAIL:/ERROR:
    blocks of the output's tail (unittest's `FAIL: t (mod.Class.t)` and the traceback's last exception line). A class
    the record says failed whole has no test. Sorted, so the first ones match the classes a refusal lists first."""
    rec = None
    if run_id:
        try:
            rec = c.run_record(wt, cmd, run_id)
        except Exception:  # noqa: BLE001 — a record that can't be read leaves the output's own blocks
            rec = None
    entries = rec.get("failed") if isinstance(rec, dict) and not rec.get("ok") else None
    found = [(str(f["class"]), str(f.get("test") or ""), str(f.get("first") or ""))
             for f in entries or [] if isinstance(f, dict) and f.get("class")]
    first = {}
    for k, t, e in found or _block_failures(tail):
        first.setdefault((k, t), e)  # a subTest fails once per value under one test: the test counts once, with its first exception
    return sorted((k, t, e) for (k, t), e in first.items())


_FAIL_HEAD = re.compile(r"(?m)^(?:FAIL|ERROR): \S+ \(([A-Za-z_]\w*(?:\.\w+)+)\)[^\n]*$")  # a subTest adds ` (i=1)`
_EXC_LINE = re.compile(r"(?:[\w.]+\.)?\w*(?:Error|Exception|Failure|Interrupt|Exit)\b")


def _block_failures(tail):
    """T-5046: [(mod.Class, test, exception line)] from unittest's FAIL:/ERROR: blocks in `tail`: a block runs to the next
    header, a ===== rule or the summary; its exception is the last unindented line that reads as one, else its last line."""
    heads = list(_FAIL_HEAD.finditer(tail or ""))
    out = []
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(tail)
        lines = []
        for ln in tail[m.end():end].splitlines():
            if ln.startswith("====") or ln.startswith("Ran "):
                break
            if ln.strip() and not set(ln.strip()) <= set("-"):  # unittest's ---- rule under the header is no content
                lines.append(ln.rstrip())
        exc = next((x for x in reversed(lines) if not x[:1].isspace() and _EXC_LINE.match(x)), lines[-1] if lines else "")
        cls, _, test = m.group(1).rpartition(".")
        out.append((cls, test, exc.strip()))
    return out[:200]


def _tests_clause(tests, room=None):
    """T-5046: `first failures: a.A.t: ValueError: x; …` for the first SHOW_TESTS tests, then a count; '' for none. `room`
    is the characters the clause may take: each exception is cut to fit it (not below 30), else fewer tests are named
    (one at least), the count always says how many are left out."""
    if not tests:
        return ""
    text = ""
    for n in range(min(SHOW_TESTS, len(tests)), 0, -1):
        more = len(tests) - n
        tail = f" (+{more} more)" if more > 0 else ""
        for width in range(EXC_WIDTH, 29, -10):
            shown = [(f"{k}.{t}" if t else k) + (f": {c.fit(c.redact(c.plain(e)), width)}" if e else "") for k, t, e in tests[:n]]  # plain first: it joins a token a zero-width character split
            text = "first failures: " + "; ".join(shown) + tail
            if room is None or len(text) <= room:
                return text
    return text


def _full_refusal(p, tail, failed, said, got, cmd, wt=None, run_id=None):
    """T-4739: the one line a refused full run says: the failed classes (five, then a count), what the rerun alone did, the
    baseline task and the file holding the run's output. Never the whole output. T-5046: and the first three failing tests
    with their exception, from the first run's record (`run_id`, read in `wt`) or its output, so no rerun is needed."""
    tests = _red_tests(wt, tail, cmd, run_id)
    names = _red_names(tail) or sorted({k for k, _, _ in tests})  # a cut or piped output names none: the record does
    what = (f"failing {c.joinmore(names, 5)}" if names else f"no failing class named (exit {failed})")
    path = _keep_output(p, tail, cmd)
    fixed = [what + (f" ({'; '.join(said)})" if said else ""), _baseline_note(p, got), f"full output: {path}" if path else ""]
    from sampo import train
    head, end = "the test failed in the scratch tree: ", "; nothing was pushed"
    room = train.REFUSAL_ROOM - len(head) - len(end) - len("; ".join(x for x in fixed if x)) - 2  # the width train keeps of a refusal
    clause = _tests_clause(tests, room)
    parts = [fixed[0], clause, *fixed[1:]]
    return head + "; ".join(x for x in parts if x) + end


SLOTS_CUT = "ran out of time waiting for test slots"  # T-4718: a ship's affected run cut by its limit, no test failed
_NOT_RUN = re.compile(r"^  not run class: ([\w.]+)\s*$", re.M)
_TEST_RED = re.compile(r"^(?:FAIL|ERROR): |^FAILED \(|^  failed class: ", re.M)


def _cut_only(code, out):
    """T-4718: the classes a run.py run cut at its deadline never started, when that is all that happened (exit 124, a
    class named, no FAIL/ERROR/failed class in the output): [] otherwise, so a real red is never taken for a wait. A
    class run.py killed "still running at the run's deadline" started and never ended: hung (or too slow), not starved
    of slots: not a cut either (a retry would hang again)."""
    if code != 124 or _TEST_RED.search(out) or _hung(out):
        return []
    return _NOT_RUN.findall(out)


def _hung(out):
    """The classes run.py killed at the run's deadline while they ran ('==== class' blocks naming it): they started."""
    return [b.split("\n", 1)[0].strip() for b in re.split(r"^==== ", out, flags=re.M)[1:]
            if "still running at the run's deadline" in b]


# T-4788: the affected stage is an early warning, not a second full run. Ship-1116a (2026-11-16): 208 affected files
# (6429 tests, 3846 CPU s) at load 13-16 could not finish in 900 s; two healthy classes 10 s in were blamed as hung.
AFFECTED_OLD, AFFECTED_FLOOR, AFFECTED_CEIL = 900, 300, 1500  # the limit with no record; the scaled limit's bounds (s)
AFFECTED_SHARE = 1 / 3  # an affected set over this share of the suite is the full check's work
AFFECTED_MARGIN, AFFECTED_SETUP, AFFECTED_FREE_MIN = 1.5, 60, 2.0  # x the estimate, plus startup s; at least 2 free CPUs
HUNG_TIMES, HUNG_PLUS, HUNG_NO_RECORD = 3, 120, 300  # a killed class is hung past x its record + s (no record: s in)


_READ_TIMES = ("import sys, json; sys.path.insert(0, sys.argv[1]); import run\n"
               "try:\n    jobs = min(run.slot_count(), run.usable_cpus(run._cgroup_dir())) or run.usable_cpus(run._cgroup_dir())\n"
               "except Exception:\n    jobs = 0\n"
               "print(json.dumps([run.load_times(), jobs]))\n")


def _run_times(wt, cmd, env=None, wait=30):
    """T-4788: ({class or job id: recorded wall seconds}, the suite's module names, the -j the run will use or 0) for a
    command that runs the tree's own tests/run.py: its timing record and default -j, read by that run.py in a child
    (never imported here; `env`, the ship's own without GIT_*, else this process's without them; its own session, killed
    with everything it started after `wait` s). The record keeps only finite non-negative numbers. ({}, set(), 0) for any
    other runner; the record {} and -j 0 when they can't be read, so the caller falls back to the file count."""
    tests = os.path.join(wt, "plugin", "tests")
    if "run.py" not in cmd:
        return {}, set(), 0
    try:
        suite = {f[:-3] for f in os.listdir(tests) if f.startswith("test_") and f.endswith(".py")}
    except OSError:
        return {}, set(), 0
    if env is None:
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    got, jobs, proc = {}, 0, None
    try:
        proc = subprocess.Popen([sys.executable, "-I", "-c", _READ_TIMES, tests], stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, cwd=wt, stdin=subprocess.DEVNULL, env=env,
                                start_new_session=True)
        out, _ = proc.communicate(timeout=wait)
        if proc.returncode == 0 and out.strip():
            read = json.loads(out.strip().splitlines()[-1])  # NaN and Infinity parse: sifted below
            got, jobs = read[0], read[1]
    except (OSError, ValueError, IndexError, KeyError, TypeError, subprocess.SubprocessError):
        got, jobs = {}, 0
    finally:  # every exit (clean, too slow, interrupted): the reader's session ends with whatever it left running
        if proc is not None:
            with contextlib.suppress(OSError):
                killsafe.killpg(proc.pid, signal.SIGKILL)
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                proc.wait(timeout=5)  # reaped, so the child is no zombie
    if not isinstance(got, dict):
        got = {}
    good = {k: float(v) for k, v in got.items() if isinstance(k, str) and isinstance(v, (int, float))
            and not isinstance(v, bool) and math.isfinite(v) and v >= 0}
    if m := re.search(r"(?:^|\s)-j\s*(\d+)(?!\S)", cmd):  # the command's own -j beats run.py's default
        jobs = int(m.group(1))
    return good, suite, jobs if isinstance(jobs, int) and not isinstance(jobs, bool) and jobs > 0 else 0


def _affected_limit(tests, times, suite, host, jobs=0):
    """T-4788: (the affected stage's limit in s, a line to say) or (None, the line) to skip the stage for the full check.
    The set's recorded seconds (run.py's own record: wall seconds per class, split jobs not counted twice) over the
    workers it can use: the CPUs the machine has free (its CPU count less its 1-minute load, at least AFFECTED_FREE_MIN),
    never more than `jobs`, the -j the run will use (0: unknown), times AFFECTED_MARGIN: that limit, between
    AFFECTED_FLOOR and AFFECTED_CEIL. Skipped when the set is over AFFECTED_SHARE of the suite (by recorded seconds,
    else by file count) or would not finish under the ceiling. No suite or no usable record (none, or all zero): the
    file count, then the old limit. A host with no CPU count is this machine's."""
    mods = sorted({os.path.basename(t).rsplit(".", 1)[0] for t in tests})
    if not suite or not mods:
        return AFFECTED_OLD, ""
    per = {}
    for k, v in times.items():
        mod, _, rest = k.partition(".")
        if mod in suite and rest and "." not in rest:  # a class's own time, not its split jobs'
            per[mod] = per.get(mod, 0.0) + v
    mine = [m for m in mods if m in per]
    named = f"affected set is {len(mods)} of {len(suite)} test files"
    if len(per) * 2 < len(suite) or len(mine) * 2 < len(mods) or sum(per.values()) <= 0:  # too little of a record
        if len(mods) > len(suite) * AFFECTED_SHARE:
            return None, f"{named} (by file count: no usable run times): the full check covers it"
        return AFFECTED_OLD, ""
    mean = sum(per.values()) / len(per)
    secs = sum(per[m] for m in mine) + (len(mods) - len(mine)) * mean
    all_secs = sum(per.values()) + (len(suite) - len(per)) * mean
    load, cpus = (host or (0, 0))[0], (host or (0, 0))[1] or os.cpu_count() or 1
    free = max(1.0, min(cpus, jobs or cpus, max(AFFECTED_FREE_MIN, cpus - load)))
    want = secs / free * AFFECTED_MARGIN + AFFECTED_SETUP
    figures = f"{secs:.0f} of {all_secs:.0f} recorded s, {secs / all_secs:.0%}"
    if secs > all_secs * AFFECTED_SHARE:
        return None, f"{named} ({figures}): the full check covers it"
    if want > AFFECTED_CEIL:
        return None, (f"{named} ({figures}) would take about {want:.0f} s on {free:.0f} workers, over the "
                      f"{AFFECTED_CEIL} s limit: the full check covers it")
    limit = int(max(AFFECTED_FLOOR, want))
    return limit, f"limit {limit} s: {secs:.0f} recorded s over {free:.0f} workers"


def _killed(out):
    """T-4788: {class or job: s into its own run} of the classes run.py killed at the run's deadline."""
    got = {}
    for b in re.split(r"^==== ", out or "", flags=re.M)[1:]:
        m = re.search(r"still running at the run's deadline \((\d+)s in\)", b)
        if m:
            got[b.split("\n", 1)[0].strip()] = int(m.group(1))
    return got


def _cut_totals(out, times):
    """T-4788: (the classes truly hung, the line a run cut at its limit says) from run.py's own totals. A class is hung
    only when its time in the run is far over its recorded one (HUNG_TIMES x it plus HUNG_PLUS s; no record: over
    HUNG_NO_RECORD s); the others were healthy and unfinished."""
    killed = _killed(out)
    notrun = set(_NOT_RUN.findall(out or ""))
    hung, notes = [], []
    for name, secs in sorted(killed.items()):
        rec = times.get(name) or times.get(name.rpartition(".")[0])
        if secs > (max(HUNG_TIMES * rec, rec + HUNG_PLUS) if rec else HUNG_NO_RECORD):
            hung.append(name)
        notes.append(f"{name} {secs} s in" + (f", recorded {rec:.0f} s" if rec else ", no record"))
    jobs = re.search(r"\((\d+) jobs", out or "")
    done = f"{int(jobs.group(1)) - len(notrun)} of {jobs.group(1)} classes done, " if jobs and notrun else ""
    left = len(notrun - set(killed))
    line = (f"the affected tests ran out of time: {done}{left} not started, {len(killed)} running when it ended "
            f"({c.joinmore(notes, 4)}); no test failed")
    return hung, line


def _affected_first(p, wt, base, env, only=None):
    """T-2323: fm check's affected-test selection over the range (main..the cherry-picked tip) in the scratch tree, run
    before the full suite; red stops the ship naming the failing classes. No linked test or no affected command: goes on.
    `only`: exactly these files instead of the range (T-3533). Returns the test files that ran green; [] when none is
    linked, and also (T-4788) when the stage was skipped as too big for an early warning or cut at its limit with no
    test failed and none hung: the full check that follows judges those, so [] is not "these passed". None when no
    selection could be made, and under `only` also when the stage was skipped, cut, or a failure beside a killed or
    not-run class left some of the files unrun: the caller then runs the suite instead of reusing a pass."""
    from sampo import cli
    try:
        cmd, tests, _ = cli.affected(c.Project(p.slug, wt, p.dir, p.lane), since=base, only=only)
    except Exception as e:  # a convenience: the full suite still runs
        print(f"affected tests skipped: {c.fit(c.plain(str(e)), 120)}", flush=True)
        return None
    if not cmd:
        return []
    try:  # T-4788: the stage's limit follows the set's recorded time and the free CPUs; a ship never crashes from sizing
        times, suite, jobs = _run_times(wt, cmd, env)
        limit, said = _affected_limit(tests, times, suite, c.host_load(), jobs)
    except Exception as e:
        times, limit, said = {}, AFFECTED_OLD, ""
        print(f"affected stage not sized ({c.fit(c.plain(str(e)), 120)}): the old {AFFECTED_OLD} s limit", flush=True)
    if limit is None:  # too big to be an early warning: the full check (or, for `only`, the suite) covers it
        print(said, flush=True)
        return None if only else []
    print(f"testing the {len(tests)} affected test file(s) " + (f"of {c.joinmore(only, 5)}" if only else "first")
          + (f" ({said})" if said else ""), flush=True)
    run_env = dict(cli._skip_env({}), **{k: v for k, v in env.items() if k != "FOREMAN_SKIP_TESTS"})
    run_env, venv = cli._venv_env(wt, run_env, cmd)  # T-4150: the project's own interpreter, as the close runs it
    if venv:
        print(cli._venv_note(venv), flush=True)
    code, out = c.run_command(wt, cmd, limit, niced=True, env=run_env, origin="ship")  # T-4197
    truly = []  # the killed classes far over their recorded time
    if code == 124 and not _TEST_RED.search(out) and _killed(out):
        # T-4788: cut at its limit with classes running and no test failed: said from the run's own totals. Only a class
        # far over its recorded time is hung; with none, the full check judges (the affected run only warns early); a
        # files-only run (`only`) has no full check after it, so its caller runs the suite
        truly, line = _cut_totals(out, times)
        if not truly:
            print(line + ("; the full check follows" if not only else "; the suite follows"), flush=True)
            return None if only else []
    if names := _cut_only(code, out):
        # T-4718: cut at its limit with only classes not run (train 1115d: 98% of the wall waiting for slots a verify
        # held) is a wait, not a failure: said so, then the classes not run go once more after a load wait
        print(f"{SLOTS_CUT} ({len(names)} classes not run): running them once more", flush=True)
        from sampo import lanes
        c.wait_for_load("sampo ship", out=sys.stdout)
        code, out = c.run_command(wt, lanes.pm_rerun_cmd(wt, names) or cmd, 900, niced=True, env=run_env,
                                    origin="ship")
        if again := _cut_only(code, out):
            print(out[-4000:], flush=True)
            _err(f"the affected tests {SLOTS_CUT} ({len(again)} classes not run), again after one retry; no test failed; "
                 "nothing was pushed: ship again when the test slots are free")
    if code and _rerun_red(wt, out, code, env) is None:
        print(out[-4000:], flush=True)
        bad = sorted({m.group(1) for m in re.finditer(r"^(?:FAIL|ERROR): \S+ \(([\w.]+?)\.\w+\)", out, re.M)})
        if (hung := truly or _hung(out)) and not bad:  # T-4718: killed while running at the deadline: not a failed test, not a wait
            _err(f"the affected tests did not finish in the scratch tree: {', '.join(hung)} still running at the run's "
                 "deadline, far over its recorded time (hung or too slow, not waiting for slots); nothing was pushed")
        _err("the affected tests failed in the scratch tree" + (f": {', '.join(bad)}" if bad else "") + "; nothing was pushed")
    if only and code and (_killed(out) or _NOT_RUN.findall(out or "")):
        # a failed class beside killed or not-run ones that passed alone: only the failed class is known green, so a
        # files-only run reuses no pass (the range run's full check still follows): the suite runs
        print("some affected classes never ran (killed or not started); the suite runs", flush=True)
        return None
    return tests


def _reuse_after_affected(p, wt, base, env, hit, rest):
    """T-3533, T-4788: the pass a ship may reuse after the files main changed beyond it (`rest`) had their affected tests
    run: (hit, the test files that ran). `hit` becomes None when that run could not vouch for them (skipped, cut,
    unselectable, or failed beside classes that never ran): the ship then runs the suite."""
    ran = _affected_first(p, wt, base, env, only=rest) if hit and rest else []
    return (hit if ran is not None else None), ran


def _tower_files(root, last, base):
    """T-2955: the files main moved since the last ship (by content) inside fm genesis live's write set (rune.live adds
    site/genesis and the stats file): the tower publishes them to main behind every branch, so main's copy is the one
    to keep. Everything else reaches main only through a ship, so a move there is someone else's and still stops it."""
    from sampo import rune
    moved = _git(root, "diff", "--name-only", last, base).splitlines()
    return {f for f in moved if f.startswith("site/genesis/") or _data_file(f)}


def _data_file(f):
    """T-4868: the daily stats and the card export, which publish pushes to main even while a ship tests (the stats
    would otherwise skip every day a ship ran through the quiet window): data only, so a move in them while the suite
    runs changes nothing a re-test could learn, and the ship re-picks over it instead of refusing."""
    from sampo import rune
    return f == rune.STATS or f.startswith(rune.CARDS + "/")


def _pick(root, wt, sha, tower):
    """Cherry-pick sha onto wt's HEAD (a merge against its first parent); a conflict only in the live tower's files keeps
    main's copy (T-2955), in the first pick and the re-pick after main moved while testing (T-2963). A failed pick is
    aborted; the result's returncode says which."""
    merge = len(_git(root, "rev-list", "--parents", "-n1", sha).split()) > 2
    r = c._git(wt, "cherry-pick", *(["-m", "1"] if merge else []), sha, timeout=300, proc=True)
    bad = set(_git(wt, "diff", "--name-only", "--diff-filter=U", ok=True).split()) if r.returncode else set()
    if bad and bad <= tower:
        _git(wt, "checkout", "--ours", "--", *bad)
        _git(wt, "add", "--", *bad)
        r = c._git(wt, "-c", "core.editor=true", "commit", "-q", "--no-edit", "--allow-empty", timeout=60, proc=True)
    if r.returncode and not bad and "is now empty" in r.stderr + r.stdout:
        # T-4087: a pick main already holds (or a merge that changed nothing) adds nothing: skipped, not a refusal
        r = c._git(wt, "cherry-pick", "--skip", timeout=60, proc=True)
    if r.returncode:
        _git(wt, "cherry-pick", "--abort", ok=True)
    return r


def _ship_cap():
    return float(os.environ.get("FOREMAN_SHIP_TIMEOUT_S") or 7200)  # SAMPO_SHIP_TIMEOUT_S, mirrored by core


# T-5985: the file a land's ship appends the name of each phase it enters to. A report channel, not a secret: the env is
# popped before the suite runs, so the suite and its output never see the path, but a same-user process can still read it
# from /proc/PID/environ of the ship and append a name, which restarts a clock. Accepted: that process runs as the same
# user and could signal the ship or edit the project's state outright; the channel only keeps a suite's own output and
# a nested `fm ship` from forging a phase by accident or by a hostile test name.
PHASE_ENV = "FOREMAN_SHIP_PHASE_FILE"
PHASES = ("prepare", "wait", "hold", "gates", "suite", "rerun", "finish")
WAIT_SLACK = 120  # the wait ends itself at its cap (_ship_gate); the land's backstop on it comes this much later
GATES_WAIT = 600  # c.wait_while's bound on a running fm check or ambient run: the ship's own, then it goes ahead beside it


def _env_secs(name, default):
    """A positive number of seconds from the environment, else `default` (an unset, empty, bad or non-positive value)."""
    try:
        v = float(os.environ.get(name) or default)
    except ValueError:
        return float(default)
    return v if v > 0 else float(default)


def ship_caps(limit=None):
    """T-5985: seconds each phase of a ship may take, each on its own clock that starts when the phase does: `prepare`
    (fetch, picks, the scratch tree), `rerun` (a red run's classes alone) and `finish` (the baseline, the footprint's hook
    bench, the push) FOREMAN_SHIP_STEP_TIMEOUT_S (1800); `wait` for an earlier ship (the cap, FOREMAN_SHIP_TIMEOUT_S, and a slack: the wait ends itself first);
    `hold` the self-change gate's hook bench FOREMAN_SHIP_HOLD_TIMEOUT_S (3600); `gates` the wait for a running check (600
    and a slack); `suite` the ship's own test run, the cap (7200): the time it waited for CPU slots is in it, the time it
    waited for another ship or the self-gate is not. `limit` overrides the suite's (a caller that read the cap already)."""
    suite = _ship_cap() if limit is None else limit
    step = _env_secs("FOREMAN_SHIP_STEP_TIMEOUT_S", 1800)
    return {"prepare": step, "wait": suite + WAIT_SLACK, "hold": _env_secs("FOREMAN_SHIP_HOLD_TIMEOUT_S", 3600),
            "gates": GATES_WAIT + WAIT_SLACK, "suite": suite, "rerun": step, "finish": step}


def ship_span():
    """T-5985: the longest a ship that holds its claim may run: every phase after the wait (the claim is taken once the
    wait is over). A claim older than this (and five minutes) is stale: the ship ended itself at each phase's bound."""
    return sum(v for k, v in ship_caps().items() if k != "wait")


def ship_life():
    """T-5985: the longest a land's detached ship lives in all: its wait for an earlier ship and every phase after it.
    What a verify that yields to a ship waits at most: the suite's cap alone ended it while the ship still waited."""
    return sum(ship_caps().values())


def _phase(args, name):
    """T-5985: say which phase this ship entered, to the file the land's ship run named (PHASE_ENV): the land starts that
    phase's own clock. No file (a hand ship) or one that can't be written says nothing: a phase is a report, never a
    condition. Not stdout: a suite's own output could say the same words, a file the suite never sees can't be forged."""
    path = getattr(args, "_phase_file", None)
    if not path or name not in PHASES:
        return
    with contextlib.suppress(OSError):
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0))  # never a link to another file
        try:
            os.write(fd, (name + "\n").encode())
        finally:
            os.close(fd)


def _claim_alive(g):
    """T-4645: a ship's claim names a process that is still running: the pid began at the recorded start time (a reused
    pid has another), or, on a host where that time can't be read, the pid answers; and the claim is no older than a
    ship may run (ship_span: every phase after the wait at its bound, plus five minutes: a ship ends itself at each)."""
    pid = g.get("pid") if isinstance(g, dict) else None
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        if time.time() - float(g.get("started") or 0) > ship_span() + 300:
            return False
    except (TypeError, ValueError):
        return False
    return c.same_process(pid, g.get("ticks") or 0)  # a marker without start ticks: the pid answering


def _ship_claim(p, at, picks, task):
    """T-4645: under the project lock, either this ship's claim is recorded (meta ship_claim: pid, start ticks, time,
    the head it tests, the commits it ships) and None comes back, or the claim of another live ship does. The claim is
    not ship_testing: that key is the test phase only, and `fm check` waits on it."""
    from sampo import train
    with c.lock(p.dir):
        meta = c.read_meta(p)
        held = meta.get("ship_claim")
        if held and held.get("pid") != os.getpid() and _claim_alive(held):
            return held
        if held and held.get("pid") != os.getpid():  # T-5938: a ship that died holding its claim leaves a line, then this goes ahead
            train._ship_log(p, train._gone_line(held.get("pid"), held.get("task"), held.get("started"), "gone"))
        meta["ship_claim"] = _claim_record(at, picks, task)
        c.write_meta(p, meta)
    return None


def _claim_record(at, picks, task):
    return {"pid": os.getpid(), "ticks": c.proc_ticks(os.getpid()), "started": round(time.time(), 1),
            "at": c.now(), "head": at, "picks": list(picks), "task": task}


def _ship_takeover(p, from_pid, at, picks, task):
    """T-5985: the claim passes to this ship, atomically (under the project lock, and only while `from_pid` still holds it):
    the one claim decides which ship tests. True when this ship holds it now."""
    with c.lock(p.dir):
        meta = c.read_meta(p)
        if (meta.get("ship_claim") or {}).get("pid") != from_pid:
            return False
        meta["ship_claim"] = _claim_record(at, picks, task)
        c.write_meta(p, meta)
    return True


def _wait_set(p, rec):
    """T-5985: this ship's entry in meta ship_waits (who waits on whom: pid, start ticks, the pid it waits on, its parents,
    its head's commit time), or none when `rec` is None; entries of ships that are gone are dropped with it."""
    me = str(os.getpid())
    with c.lock(p.dir):
        meta = c.read_meta(p)
        waits = {k: w for k, w in (meta.get("ship_waits") or {}).items()
                 if k != me and isinstance(w, dict) and c.same_process(w.get("pid"), w.get("ticks") or 0)}
        if rec:
            waits[me] = rec
        if waits:
            meta["ship_waits"] = waits
        else:
            meta.pop("ship_waits", None)
        c.write_meta(p, meta)


def _int(x):
    return x if isinstance(x, int) and not isinstance(x, bool) else None


def _wait_cycle(me, held, waits):
    """T-5985: the ship that waits, through the holder, on this one (a dict with its pid), or None. A holder waits on what
    its own children wait on (a standing-ship loop waits for its ship): from the holder follow every live waiter that is
    the holder or runs under it to the pid that one waits on, and so on (at most eight steps); a waiter on this ship (or
    on a parent of it) closes a cycle. A holder that is this ship's own parent is the shortest one: {"own": True}."""
    mine = {me["pid"], *(x for x in map(_int, me.get("ppids") or []) if x is not None)}
    if held in (me.get("ppids") or []):
        return {"pid": held, "own": True}
    seen, frontier = set(), [held]
    for _ in range(8):
        nxt = []
        for h in frontier:
            if h in seen:
                continue
            seen.add(h)
            for w in waits.values():
                if not isinstance(w, dict) or _int(w.get("pid")) in (None, me["pid"]):
                    continue
                if not c.same_process(w["pid"], w.get("ticks") or 0):
                    continue
                if h == w["pid"] or h in (w.get("ppids") or []):
                    on = _int(w.get("on"))
                    if on is not None and on in mine:
                        return w
                    nxt.append(on)
        frontier = [x for x in nxt if x is not None]
        if not frontier:
            break
    return None


def _age_key(w):
    """The order of two ships: the older head (its commit's time) first, then the one that started waiting first, then
    the lower pid: one total order, so the two ships of a cycle never both think the other is the older."""
    try:
        return (float(w.get("htime") or 0), float(w.get("since") or 0), int(w.get("pid") or 0))
    except (TypeError, ValueError):
        return (0.0, 0.0, 0)


class _Claimed:
    """The claim ends with the ship, whatever ends it (only the process that took it clears it)."""

    def __init__(self, p):
        self.p = p

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        try:
            with c.lock(self.p.dir):
                meta = c.read_meta(self.p)
                if (meta.get("ship_claim") or {}).get("pid") == os.getpid():
                    meta.pop("ship_claim")
                    c.write_meta(self.p, meta)
        except Exception:  # noqa: BLE001 — a claim that can't be cleared is stale at the next look (its pid is gone)
            pass


def _ship_gate(p, at, picks, task, args):
    """T-4645: a ship that starts while another ship of this project runs tests nothing yet. "go": no other ship is
    alive and this one's claim is taken. "covered": the running ship's picks include every one of ours (a line says so,
    nothing runs). "waited": the other ship ended (the caller reads the moved origin main and plans again). The wait is
    bounded by what a ship may run (FOREMAN_SHIP_TIMEOUT_S); past it the ship is refused, its grant untouched."""
    held = _ship_claim(p, at, picks, task)
    if held is None:
        return "go"
    mine, theirs = set(picks), set(held.get("picks") or [])
    who = f"the running ship (pid {held['pid']}, {held.get('task') or 'a ship'}, testing {str(held.get('head'))[:9]})"
    if mine <= theirs:
        print(f"nothing to ship here: {who} already carries every commit this one would ship "
              f"({', '.join(x[:8] for x in picks)}); no tests run. If it is refused, run fm ship again", flush=True)
        return "covered"
    t0 = getattr(args, "_ship_wait_from", None) or time.monotonic()
    args._ship_wait_from = t0
    _phase(args, "wait")  # T-5985: the land's clock for this wait is its own; the suite's starts after it
    end = t0 + _ship_cap()
    print(f"fm ship: waiting for {who} to end before testing: {len(picks)} commit(s) wait, no test runs until then "
          f"(at most {_ship_cap():g}s); then it tests the moved {args.remote or 'origin'} main", flush=True)
    try:
        htime = float(_git(p.root, "log", "-1", "--format=%ct", at, ok=True).strip() or 0)
    except ValueError:
        htime = 0.0
    me = {"pid": os.getpid(), "ticks": c.proc_ticks(os.getpid()), "on": held.get("pid"), "task": task, "head": at,
          "htime": htime, "since": round(time.time(), 1), "ppids": c.proc_ancestors(os.getpid())}
    with contextlib.suppress(Exception):  # a registry that can't be written only loses the cycle check, never the wait
        _wait_set(p, me)
    try:
        while True:
            meta = c.read_meta(p)
            g = (meta.get("ship_claim") or {})
            if not (g.get("pid") != os.getpid() and _claim_alive(g)):
                return "waited"
            if g.get("pid") != me["on"]:  # the claim passed to another ship: this one waits on that
                me["on"] = g.get("pid")
                with contextlib.suppress(Exception):
                    _wait_set(p, me)
            other = _wait_cycle(me, g["pid"], meta.get("ship_waits") or {})
            if other:
                # T-5985: the holder waits for a ship that waits on this one (its own child, or this ship's parent): neither
                # would ever end. The older head yields and ends, the newer takes the claim (atomic: one ship tests).
                if other.get("own") or _age_key(me) < _age_key(other):
                    _err(f"superseded by {other['pid']}" + (f" ({other.get('task')})" if other.get("task") else "")
                         + f": {who} and that ship wait on each other; this ship's head {at[:9]} is the older, so it ends "
                         "and the other goes ahead; nothing was pushed: run fm ship again after it")
                if _ship_takeover(p, g["pid"], at, picks, task):
                    print(f"fm ship: {who} waits on a ship that waits on this one: this head is the newer, so it tests "
                          f"now and the other goes after it (pid {other['pid']})", flush=True)
                    return "go"
            if time.monotonic() >= end:
                _err(f"{who} is still running after {_ship_cap():g}s; nothing was pushed: run fm ship again after it")
            time.sleep(1)
    finally:
        with contextlib.suppress(Exception):
            _wait_set(p, None)


def ship(p, args):
    """T-1943: fm ship — HEAD's commits the remote's main lacks (git cherry, by patch) or the named ones, cherry-picked
    onto the fetched main in a scratch worktree, their files compared, tested there, main re-checked, then pushed
    behind the publish grant. The working tree is never touched; a failure pushes nothing and keeps the grant.
    T-4239: HEAD is read once (SAMPO_SHIP_AT names the head instead): the ship tests and pushes that snapshot."""
    import shlex
    root, remote = p.root, args.remote or "origin"
    chance = getattr(args, "chance", None)
    if chance is not None:  # T-2496: refused before anything is pushed
        from sampo import outcomes
        try:
            outcomes._stated(chance)
        except ValueError as e:
            _err(f"--chance: {e}")
    if remote.startswith("-"):
        _err(f"--remote {remote!r} reads as a git option; name a remote or a URL")
    # T-5938 round 2: the marker is read once and gone from the env (the ship's own test run inherits no claim to it); the
    # standing ship's refusals come before anything is fetched: a caller other than the land's own ship, and an override
    land = _land_marker()
    if getattr(args, "task", None) == c.STANDING_SHIP:
        _named_grant(p, c.STANDING_SHIP, land=land)  # refuses a caller that isn't the land's, and a ship that is off
        if args.test or args.remote:
            _err("a standing ship takes no --test or --remote: the land's ship tests with the project's own check and pushes "
                 "to origin; nothing was pushed")
    # T-4239: the head this ship reads is fixed once (SAMPO_SHIP_AT: the head a land's detached ship was started on, else
    # HEAD now): a land goes on merging while its ship runs, and every pick, ancestry and tree below reads this sha, so the
    # ship tests and pushes the snapshot at it (a cherry-picked scratch tree), never a newer head
    pin = os.environ.get("FOREMAN_SHIP_AT", "").strip()  # core.mirror_env: SAMPO_SHIP_AT arrives under this name
    # review: read once, then gone from the env: the ship's own test run (os.environ minus GIT_*) runs suites that run
    # `fm ship` in scratch repos, where this sha isn't a commit
    os.environ.pop("FOREMAN_SHIP_AT", None)
    os.environ.pop("SAMPO_SHIP_AT", None)
    # T-5985: the land's phase file, read once and gone from the env for the same reason: a suite that runs `fm ship` in a
    # scratch repo must not write the land's phases. The replan below (the same args) keeps it.
    from_land = os.environ.pop(PHASE_ENV, None)
    os.environ.pop("SAMPO_SHIP_PHASE_FILE", None)
    args._phase_file = getattr(args, "_phase_file", None) or from_land
    _phase(args, "prepare")
    at = _git(root, "rev-parse", "--verify", "-q", "--end-of-options", f"{pin or 'HEAD'}^{{commit}}", ok=True).strip()
    if not at:
        _err(f"SAMPO_SHIP_AT {pin!r} isn't a commit here; nothing was pushed")
    _git(root, "fetch", "-q", remote, "main")
    base = _git(root, "rev-parse", "FETCH_HEAD").strip()
    picks = []
    for x in args.commits or []:
        sha = "" if x.startswith("-") else _git(root, "rev-parse", "--verify", "-q", "--end-of-options", f"{x}^{{commit}}", ok=True).strip()
        picks.append(sha or _err(f"{x!r} isn't a commit here"))
    last = _git(root, "rev-parse", "--verify", "-q", SHIPPED, ok=True).strip()  # T-1945: main may hold these under another patch
    if last and c._git(root, "merge-base", "--is-ancestor", last, at, timeout=60, proc=True).returncode:
        last = ""  # rebased or another branch: git cherry alone
    def _files(a, b):
        return set(_git(root, "diff", "--name-only", a, b).splitlines())
    tower = _tower_files(root, last, base) if last else set()  # T-2955: main's copy of these is the one to keep
    def _picks(upto):
        if last and not ((_files(last, base) - tower) & _files(last, upto)):
            # T-2054: main is where the last ship left it (T-2212: or moved only in files the branch left alone), so
            # replay the branch's own history: a merged lane's commit may not apply alone (its merge settled a
            # conflict), but each first-parent step applies in turn
            got = _git(root, "rev-list", "--first-parent", "--reverse", f"{last}..{upto}").split()
            if got:
                return got
        return [ln[2:] for ln in _git(root, "cherry", base, upto, *([last] if last else [])).splitlines() if ln.startswith("+ ")]
    said = args.test or _git(root, "config", "--local", "--get", "foreman.shiptest", ok=True).strip()
    target, full = at, None
    if not picks and not args.check and not getattr(args, "head", False):
        # T-3666: the newest commit the post-merge verify ran the whole suite on, so the ship needn't run it (~80 CPU-min)
        try:
            target, full = _verified(p, root, said, base, last, at)
        except Exception as e:  # a convenience: HEAD ships, tested, as before
            target, full = None, None
            print(f"post-merge verify reuse skipped: {c.fit(c.plain(str(e)), 120)}", flush=True)
        picks = _picks(target) if target else []
        target, full = (target, full) if picks else (at, None)
    picks = picks or _picks(at)
    if not picks:
        print(f"nothing to ship: {remote} main already has HEAD's changes" + (f" after the last shipped commit {last[:8]}" if last else ""))
        return 0
    if args.check:  # T-1960: a ship verifies without a grant: which of these main lacks, by patch
        missing = [x for x in picks if not args.commits or _git(root, "cherry", base, x, f"{x}^").startswith("+")]
        for x in missing:
            print(f"{remote} main lacks {x[:8]} {c.fit(c.plain(_git(root, 'log', '-1', '--format=%s', x).strip()), 72)}")
        if not missing:
            print(f"{remote} main has the changes of {', '.join(x[:8] for x in picks)}")
        return 1 if missing else 0
    b = (_named_grant(p, args.task, land=land) if getattr(args, "task", None)
         else _grant(p) or _err(_NEED_GRANT.replace("--push", "fm ship", 1) + "; nothing was pushed"))
    if args.test:  # T-1950: remembered per repo, so the oldest runtime's suite isn't retyped each ship
        _git(root, "config", "--local", "foreman.shiptest", args.test, ok=True)
    test = shlex.split(said) if said else [*FM, "check"]
    if full:
        k = int(_git(root, "rev-list", "--count", "--first-parent", f"{target}..{at}").strip() or 0)
        print(f"shipping {target[:9]}, the newest commit the post-merge verify passed in full; " + (
            f"{k} newer commit{'s' if k > 1 else ''} on the branch wait{'' if k > 1 else 's'} for the next verified one"
            if k else "it is HEAD"), flush=True)
    files = sorted({f for s in picks for f in _git(root, "diff-tree", "--no-commit-id", "--name-only", "-r", "-m", s).splitlines() if f})
    n = f"{len(picks)} commit{'s' if len(picks) > 1 else ''}"
    gate = _ship_gate(p, at, picks, getattr(b, "id", None) or getattr(args, "task", None), args)
    if gate == "covered":
        return 0
    if gate == "waited":  # the other ship ended: plan again from the moved main (it may carry these commits by now)
        # T-5985: the replan below opens with _phase(args, "prepare"): its fetch is never ended as the "wait" that just ended
        os.environ["FOREMAN_SHIP_AT"] = at
        if land:
            os.environ[c.LAND_SHIP_ENV] = "land"  # the replan is still the land's own ship
        return ship(p, args)
    with _Claimed(p), c.scratch_dir("sampo-ship-", root) as tmp:  # T-4735
        wt = os.path.join(tmp, "ship")
        _git(root, "worktree", "add", "-q", "--detach", wt, base)
        try:
            for s in picks:
                r = _pick(root, wt, s, tower)
                if r.returncode:
                    hint = "" if args.commits else " (main may hold it under another patch: name the commits to ship)"
                    _err(f"{s[:8]} didn't cherry-pick onto {remote} main{hint}: {c.fit(c.plain(r.stderr.strip()), 200)}; nothing was pushed")
            mine = [f for f in files if f not in tower]  # T-2955: the tower's files are main's, not the branch's
            if mine and c._git(wt, "diff", "--quiet", "HEAD", picks[-1], "--", *mine, timeout=300, proc=True).returncode:
                _err(f"the shipped files differ from {picks[-1][:8]}'s: a commit is missing or extra, or main changed "
                     "these files since the branch left it (rebase onto main); nothing was pushed")
            head = _git(wt, "rev-parse", "HEAD").strip()
            say_no_list("ship")  # T-4785: an unset list is said, not passed in silence (an unreadable one refuses just below)
            hits = _private_hits(root, picks, head)  # T-3529: before the suite runs
            sent = picks  # what lands on main, one cherry-pick each
            if hits[0] and not hits[1]:
                # T-3999: a later commit took the term out (or only a message held it), so main gets the clean tree as one
                # commit: no commit pushed holds it, and the branch's history needs no rewrite (every lane based on it would conflict)
                subjects = "\n".join(f"- {_git(root, 'log', '-1', '--format=%s', s).strip()}" for s in picks if s not in hits[0])
                head = _git(wt, "commit-tree", f"{head}^{{tree}}", "-p", base, "-m",
                            f"Sampo: {n} in one commit (a private term in {len(hits[0])} of them, left out)\n\n{subjects}").strip()
                _git(wt, "reset", "-q", "--hard", head)
                sent = [head]
                print(f"{private_where(hits[0], [])} held a private term the tree to push no longer has: main gets {n} in "
                      "one commit, without it", flush=True)
                hits = _private_hits(root, sent, head)  # the one commit main gets: its message, its lines, its tree
            _refuse_private(root, sent, head, hits)
            _phase(args, "hold")  # T-5985: the self-gate's hook bench has its own bound, outside the suite's
            _push_hold(p, b, root, picks, "fm ship")  # T-4805: a commit made straight on the trunk is judged like a lane's
            from sampo import ambient
            _phase(args, "gates")
            c.wait_while(lambda: c.gates_live(p) or ambient.running(p),  # T-1978: never two suites at once
                         "a running fm check or ambient test run", "fm ship", limit=GATES_WAIT, out=sys.stdout)
            _phase(args, "suite")  # T-5985: the suite's own clock starts here: its cap counts this run alone
            print(f"testing {n} on {remote} main, a snapshot of the head at {at[:9]} (a later head is never in it): "
                  f"{shlex.join(test)}", flush=True)
            env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}  # GIT_DIR from a hook: not the user's repo
            tested = _git(wt, "rev-parse", "HEAD^{tree}").strip()  # T-2642: the tree the suite runs on is the tree pushed
            c.update_meta(p, ship_testing={"pid": os.getpid(), "ticks": c.proc_ticks(os.getpid()), "at": c.now(),
                                           "root": os.path.realpath(wt)})
            t0 = time.monotonic()
            env.setdefault("PYTHONUNBUFFERED", "1")
            env[c.SLOT_PRIORITY] = env.get(c.SLOT_PRIORITY) or "1"  # T-3614: its runs take freed CPU slots first
            log = os.path.join(tmp, "test.log")  # T-2255: a file, not a pipe: a background process holding it can't hang the ship
            failed, tail, hit, rest = 0, "", None, []
            run_id = c.new_run_id(shlex.join(test))  # T-5046: the id its tests/run.py record carries, for the refusal
            try:  # a full fm check here waits for this run; the ship's own fm check (in wt) doesn't
                try:
                    hit, rest = _gated_pass(p, root, wt, base, said, target, full)  # T-3468
                except Exception as e:  # review: a check that can't run falls back to the suite
                    print(f"gates pass reuse skipped: {c.fit(c.plain(str(e)), 120)}", flush=True)
                # T-3533: main moved files tests read (the tower's daily stats): only their tests run, red stops the ship
                hit, ran = _reuse_after_affected(p, wt, base, env, hit, rest)
                if hit:
                    top = (f"; in {c.joinmore(rest, 5)} its affected tests passed "
                           f"({c.joinmore([os.path.basename(t) for t in ran], 8) or 'none linked'})") if rest else ""
                    print(f"reusing {hit}: the pushed tree differs from it only in files its CI "
                          f"skips or the sync mirror{top}, so the suite isn't run again", flush=True)
                else:
                    failed, tail = _run_test(p, wt, base, env, test, log, run_id)
            finally:
                if (c.read_meta(p).get("ship_testing") or {}).get("pid") == os.getpid():
                    c.update_meta(p, ship_testing=None)
            # T-3482: its typical time, for selfcheck.slow_jobs while a later ship's run is live
            c.log_event(p, "ship_test", data={"cmd": c.fit(shlex.join(test), 200), "exit": failed,
                                              "s": round(time.monotonic() - t0, 1)})
            rerun = []  # T-4739: what the rerun alone did, for the refusal
            if failed:
                _phase(args, "rerun")  # T-5985: a load wait and the red classes alone: their own bound, not the suite's
            ran = failed  # T-6012: what the full suite itself said; a flake's green rerun is no full green
            if failed and _rerun_red(wt, tail, failed, env, rerun) is not None:
                failed = 0  # T-3435: green alone is a flake; the baseline capture and the verdict get the rerun's
            covered = _settle_verify(p, root, target, tested, test, hit, ran, tail)  # T-6012: the suite is not run twice
            if covered:
                print(f"the post-merge verify's {len(covered)} pending merge{'s' if len(covered) > 1 else ''} in {target[:9]}'s "
                      "history are covered by this run", flush=True)
            _phase(args, "finish")  # the baseline, the footprint's hook bench, the push
            from sampo import cli  # the shipped tree is vouchable, so the run files (or clears) what fm check would
            try:
                got = [] if hit else cli._baseline(p, [(shlex.join(test), failed, tail, None)])  # a reuse ran nothing
            except Exception as e:  # the verdict stands; the capture is a convenience
                got = []
                print(f"baseline capture failed: {e}")
            if failed:
                for ln in got:  # T-4739: the capture's lines are in the log too, not only in the refusal
                    print(ln)
                _err(_full_refusal(p, tail, failed, rerun, got, shlex.join(test), wt, run_id))
            for ln in got:
                print(ln)
            rules = c.rules_path(os.path.join(wt, "plugin"))
            try:  # T-2281: Foreman's own footprint, the lean ratchet's baseline; before the push, so a failed push keeps it
                from sampo import doctor
                if os.path.isfile(rules):  # any other project's tree holds no Foreman plugin
                    doctor.record_footprint(p, rules, os.path.join(wt, "plugin"))
            except Exception as e:  # the ship stands; the record is a convenience
                print(f"footprint record failed: {e}")
            tip = _git(wt, "rev-parse", "HEAD").strip()
            pushing = _git(wt, "rev-parse", "HEAD^{tree}").strip()
            if pushing != tested:
                _err(f"the tree to push ({pushing}) isn't the tree the tests ran on ({tested}): something changed it "
                     "after the gates; nothing was pushed")
            for attempt in range(3):  # T-2555: a writer that lands between this fetch and the push is re-picked over too
                _git(root, "fetch", "-q", remote, "main")
                if c._git(root, "merge-base", "--is-ancestor", "FETCH_HEAD", tip, timeout=60, proc=True).returncode:
                    # T-2511: a move in files the shipped commits never touch (fm genesis live's tower publishes) can't
                    # change what was tested: re-pick onto the new main and push; any shared file refuses as before
                    new = _git(root, "rev-parse", "FETCH_HEAD").strip()
                    moved = {f for f in _files(base, new) if not _data_file(f)}  # T-4868: the daily data move needs no re-test
                    if moved & _files(base, tip):
                        _err(f"{remote} main moved while testing; nothing was pushed: run fm ship again")
                    ignored = _ci_ignored(root, tip)  # T-3439: GitHub runs the pushed commit's workflow, not main's
                    if not all(any(g.match(f) for g in ignored) for f in moved):  # T-2572: an untested tree never ships
                        _err(f"{remote} main moved while testing in files CI tests ({c.joinmore(sorted(moved), 3)}); "
                             "nothing was pushed: run fm ship again (it tests the new main)")
                    _git(wt, "checkout", "-q", "--detach", new)
                    again = _tower_files(root, last, new) if last else tower  # T-2963:the tower may have moved again
                    for sha in sent:  # T-3999: the one squashed commit, when the picks were squashed
                        if _pick(root, wt, sha, again).returncode:
                            _err(f"{remote} main moved while testing and re-picking onto it conflicted; nothing was pushed")
                    print(f"{remote} main moved while testing, only in files these commits don't touch: re-picked onto it")
                    tip = _git(wt, "rev-parse", "HEAD").strip()
                try:
                    _push_main(p, b.id, tip, remote, "fm ship")
                    break
                except Exception as e:  # the grant came back with the failed push
                    if attempt == 2 or not _MOVED.search(str(e)):
                        raise
                    print(f"{remote} main moved before the push landed: fetching it again", flush=True)
            if c._git(root, "update-ref", SHIPPED, picks[-1], timeout=60, proc=True).returncode:  # T-1951: main has them
                print(f"warning: couldn't move {SHIPPED} to {picks[-1][:8]}; the next plain fm ship may offer these again")
            if c._git(root, "update-ref", SHIPPED_TIP, tip, timeout=60, proc=True).returncode:  # T-2527: --last-ship
                print(f"warning: couldn't move {SHIPPED_TIP} to {tip[:8]}; fm ci-watch --last-ship would watch an older ship")
        finally:
            _git(root, "worktree", "remove", "--force", wt, ok=True)
    spent = "the standing ship stays on" if b.id == c.STANDING_SHIP else "the publish grant is spent"  # T-5938
    print(f"shipped {n} to {remote} main at {tip} ({spent})."
          + ("" if args.watch else f" Watch CI in the background: fm ci-watch --commit {tip}"), flush=True)
    if chance is not None:  # T-2496: the bet is booked once the commit is on main
        from sampo import outcomes
        if outcomes.ship_bet(p, chance, b.id, tip):
            print(f"Ship bet: {outcomes._stated(chance)}% that CI's blocking jobs pass on {tip[:12]}; fm ci-watch settles it.")
            note = outcomes.ship_note(p, chance)  # T-2611: the record meets the number where it is typed
            if note:
                print(note)
    if not args.watch:
        return 0
    from sampo import ci  # T-1946
    code, msg = ci.watch(p, "main", 3, 30, commit=tip)
    print(msg)
    return code


def cmd_ship(args):
    from sampo import cli
    commits = list(args.commits or [])
    # T-5938: `auto` is the subcommand only as exactly `auto on|off|status`; a branch named auto still ships
    auto = len(commits) == 2 and commits[0] == "auto" and commits[1] in ("on", "off", "status")
    if getattr(args, "why", None) and commits != ["auto", "on"]:
        _err("--why is for `sampo ship auto on` alone (the user's own words that turn the standing ship on); nothing was pushed")
    if auto:
        return cli.cmd_ship_auto(args)
    return ship(cli.resolve(args), args)


def _round(ctx):
    import argparse
    from sampo import cli
    if any(t.startswith("round-") for t in _git(ctx["root"], "tag", "--points-at", "HEAD").split()):
        return  # a resume after the tag was made but before its checkpoint
    cli.cmd_round(argparse.Namespace(**{**vars(ctx["args"]), "action": "close"}))


def _unfinished(state):
    return [n for n, _ in STEPS if n != "push" and n not in state["done"] and n not in state["skipped"]]


def _changed(state):
    """The tree was written to (a bump, changelog or commit step is done): another version can't start over it."""
    return bool({"bump", "changelog", "commit"} & set(state["done"]))


# T-2390: any other repo: the generic ritual, the same checkpoint file, resume and grant path
ANY_STEPS = [("tree", "the working tree is clean"),
             ("check", "fm check passes (the full gates)"),
             ("bump", "the one manifest's version (pyproject.toml, package.json, Cargo.toml, a gemspec or VERSION)"),
             ("changelog", "CHANGELOG.md's Unreleased entries are headed VERSION"),
             ("prove", "fm prove --release: the artifact built offline and run from a throwaway prefix"),
             ("commit", "commit 'Release VERSION' (the manifest and CHANGELOG.md only)"),
             ("tag", "a local annotated tag vVERSION"),
             ("push", "--push: the branch and the tag to the remote; spends this repo's active task's publish grant")]
MANIFESTS = (("pyproject.toml", r'(?m)^(version\s*=\s*")([^"]+)(")'), ("package.json", r'("version"\s*:\s*")([^"]+)(")'),
             ("Cargo.toml", r'(?m)^(version\s*=\s*")([^"]+)(")'), ("*.gemspec", r"""(\.version\s*=\s*["'])([^"']+)(["'])"""),
             ("VERSION", r"\A(\s*)(\d[^\s]*)()"))


def _manifest(root):
    """(relative path, pattern, version) of the one manifest that carries a version; UsageError naming what it read."""
    import glob
    found = []
    for pat, rx in MANIFESTS:
        for path in sorted(glob.glob(os.path.join(root, pat))):
            with open(path, encoding="utf-8", errors="replace") as f:
                m = re.search(rx, f.read(1 << 20))
            if m:
                found.append((os.path.relpath(path, root), rx, m.group(2)))
    if len(found) != 1:
        _err(f"{'no manifest carries a version' if not found else str(len(found)) + ' manifests carry a version: ' + ', '.join(x[0] for x in found)}"
             f" (looked at {', '.join(p for p, _ in MANIFESTS).replace('*.gemspec', 'a gemspec')}); release one with exactly one")
    return found[0]


def run_any(p, args, root, state, state_path):
    """T-2390: fm release VERSION outside Sampo's repo, a checkpoint after each of ANY_STEPS (state/release.json, as
    Sampo's own). A red or unproven fm prove --release stops before the commit; --push without this repo's active
    task's publish grant stops after the local tag and names the push.
    T-4805 exempt: the self-change gate judges Sampo's own plugin/ (it returns nothing in a tree without
    plugin/lib/fmguard.py), and this runs only outside Sampo's repo, so a hold here would judge no file."""
    import datetime
    v = args.version
    rel, rx, cur = _manifest(root)
    if args.dry_run:
        print(f"fm release {v} ({rel}: {cur}): {len(ANY_STEPS)} steps, a checkpoint after each (--resume continues)")
        for i, (n, what) in enumerate(ANY_STEPS, 1):
            print(f"  {i}. {n}: {what}")
        return 0
    if args.resume or (state and state.get("version") == v and state.get("any")):
        if not state or state.get("version") != v:
            _err(f"no unfinished release of {v} to resume")
        state["push"] = bool(args.push or state.get("push"))
    else:
        if state and state.get("version") != v and _unfinished(state):
            _err(f"the release of {state['version']} is unfinished: fm release {state['version']} --resume, or fm release --abandon")
        if not re.fullmatch(SEMVER, cur) or tuple(map(int, v.split("."))) <= tuple(map(int, cur.split("."))):
            _err(f"{v} isn't newer than {rel}'s {cur}")
        state = {"version": v, "old": cur, "done": [], "skipped": [], "push": bool(args.push), "remote": args.remote or "origin",
                 "any": rel}
    remote = state.get("remote") or "origin"
    if remote.startswith("-"):
        _err(f"--remote {remote!r} reads as a git option; name a remote or a URL")

    def tree():
        if _git(root, "status", "--porcelain").strip():
            _err("the tree has uncommitted changes; commit or finish them first")

    def bump():
        path = os.path.join(root, state["any"])
        with open(path, encoding="utf-8") as f:
            text = f.read()
        c.write_atomic(path, re.sub(rx, lambda m: m.group(1) + v + m.group(3), text, count=1))

    def changelog():
        path = os.path.join(root, "CHANGELOG.md")
        text = open(path, encoding="utf-8").read() if os.path.isfile(path) else ""
        if re.search(rf"(?m)^## {re.escape(v)} ", text):
            return
        if "## Unreleased\n" not in text:
            _err("CHANGELOG.md has no '## Unreleased' heading to put under the version")
        c.write_atomic(path, text.replace("## Unreleased\n", f"## Unreleased\n\n## {v} — {datetime.date.today().isoformat()}\n", 1))

    def prove():
        r = subprocess.run([*FM, "prove", root, "--release"], cwd=root)
        if r.returncode:
            _err(f"fm prove --release {'failed' if r.returncode == 1 else 'is unproven or refused'} (exit {r.returncode}): "
                 "a release closes on its artifact working; nothing was committed" + (
                     "" if r.returncode == 1 else " (an untrusted tree needs the user's sampo prove PATH --yes-run once)"))

    def commit():
        if _git(root, "log", "-1", "--format=%s").strip() != f"Release {v}":
            b = c.active_brief_fast(p, p.lane)
            _git(root, "commit", "-qm", f"Release {v}", *(["--trailer", f"{c.TRAILER}: {b.id}"] if b else []), "--",
                 state["any"], *(["CHANGELOG.md"] if os.path.isfile(os.path.join(root, "CHANGELOG.md")) else []))
        state["sha"] = _git(root, "rev-parse", "HEAD").strip()

    def tag():
        if not _git(root, "tag", "-l", f"v{v}").strip():
            _git(root, "tag", "-a", f"v{v}", "-m", f"Release {v}", state.get("sha") or "HEAD")

    def push():
        b = _grant(p)
        if not b:
            print(f"  stopped after the local tag: pushing v{v} to {remote} needs this repo's task's publish grant (fm ask "
                  f"ID publish); for the final report: the push of {v} waits on it")
            c.log_event(p, "release_push_waiting", data={"version": v, "remote": remote})
            return False
        branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD").strip()
        _spend(p, b.id, f"fm release {v}")
        _git(root, "push", remote, f"{state.get('sha') or 'HEAD'}:refs/heads/{branch}")
        _git(root, "push", remote, f"refs/tags/v{v}")
        return True

    fns = {"tree": tree, "check": lambda: _gate(root, "check"), "bump": bump, "changelog": changelog, "prove": prove,
           "commit": commit, "tag": tag, "push": push}
    for i, (name, _) in enumerate(ANY_STEPS, 1):
        if name in state["done"] or (name == "push" and not state["push"]):
            continue
        print(f"step {i}/{len(ANY_STEPS)} {name}", flush=True)
        try:
            ok = fns[name]()
        except Exception as e:
            _dump(state_path, state)
            if type(e).__name__ == "UsageError":
                raise type(e)(f"step {i} {name}: {e}\nstopped at step {i} ({name}); fix it, then: fm release {v} --resume")
            raise
        if ok is not False:
            state["done"].append(name)
        _dump(state_path, state)
    print(f"Released {v} ({state['any']}): tagged v{v}" + (", pushed" if "push" in state["done"] else ", not pushed"))
    return 0


def run(p, args):
    if getattr(args, "canary_status", False):
        return _canary_status(p) or 0
    state_path = os.path.join(p.dir, "release.json")
    try:
        state = _json(state_path) if os.path.isfile(state_path) else None
        if state is not None and not (isinstance(state, dict) and isinstance(state.get("version"), str)):
            raise ValueError
    except ValueError:
        if not args.abandon:
            _err(f"{state_path} is unreadable: fm release --abandon forgets it")
        state = {"version": "(unreadable)"}
    if state:
        state.setdefault("skipped", [])
    if args.abandon:
        if state and args.dry_run:
            print(f"would abandon the release of {state['version']} (--dry-run: nothing changed)")
        elif state:
            os.remove(state_path)
        if not args.dry_run:
            print(f"abandoned the release of {state['version']}" if state else "no release in progress")
        return 0
    v = args.version
    if not v:
        _err("fm release VERSION (or --abandon)")
    for what, x in (("VERSION", v), ("--ui", args.ui)):
        if x and not re.fullmatch(SEMVER, x):
            _err(f"{what} is MAJOR.MINOR.PATCH, e.g. 1.2.20")
    root = c.git_root(p.root)
    pj = os.path.join(root or "", "plugin", ".claude-plugin", "plugin.json")
    if not root:
        _err("fm release runs in a git repo")
    if not os.path.isfile(pj):  # T-2390: any other repo gets the generic ritual
        return run_any(p, args, root, state, state_path)
    import datetime
    hour = datetime.datetime.now().hour
    bad = bad_hour(c.ledger_events(p, ["check_run"]), hour)
    if bad:  # T-1549: a warning, never a stop
        print(f"fm release: careful: gates started between {hour}:00 and {hour}:59 failed {bad[0]} of {bad[1]} times "
              f"in this project (overall {bad[2]:.0%}): by its record, a bad hour for a release", file=sys.stderr)
    if args.dry_run:
        print(f"fm release {v}: {len(STEPS)} steps, a checkpoint after each (--resume continues)")
        for i, (name, what) in enumerate(STEPS, 1):
            print(f"  {i}. {name}: {what}")
        return 0
    resume = args.resume
    if not resume and state:
        if state["version"] != v and _unfinished(state) and _changed(state):
            _err(f"the release of {state['version']} is unfinished: fm release {state['version']} --resume, or fm release --abandon")
        if state["version"] == v and not _unfinished(state) and args.push and "push" not in state["done"]:
            resume = True  # released but never pushed: only the push is left
        if state["version"] == v and _unfinished(state) and _changed(state):
            resume = True  # the same release stopped after its bump: starting over would say "isn't newer"
    if resume:
        if not state or state["version"] != v:
            _err(f"no unfinished release of {v} to resume" + (f" (the one in progress is {state['version']})" if state else ""))
        for k, new in (("ui", args.ui), ("remote", args.remote)):
            state[k] = new or state.get(k)
        state["push"] = bool(args.push or state.get("push"))
        state["no_check"] = bool(args.no_check or state.get("no_check"))
    else:
        cur = _json(pj).get("version", "")
        if not re.fullmatch(SEMVER, cur):
            _err(f"plugin.json's version {cur!r} isn't MAJOR.MINOR.PATCH; fix it first")
        done = []
        if v == cur and _git(root, "log", "-1", "--format=%s").strip() == f"Foreman {v}":
            done = ["tree", "check", "bump", "changelog", "commit"]  # T-2598: committed, checkpoints abandoned: go on
        elif tuple(map(int, v.split("."))) <= tuple(map(int, cur.split("."))):
            _err(f"{v} isn't newer than {cur}")
        state = {"version": v, "old": cur, "done": done, "skipped": [], "ui": args.ui, "push": bool(args.push),
                 "remote": args.remote, "no_check": bool(args.no_check)}
        if done:
            state["sha"] = _git(root, "rev-parse", "HEAD").strip()
    state["remote"] = state.get("remote") or "origin"
    if state["remote"].startswith("-"):
        _err(f"--remote {state['remote']!r} reads as a git option; name a remote or a URL")
    if getattr(args, "stale_ok", None) is not None and not state["push"]:
        _err("--stale-ok waives the safety gate of --push; without --push there is nothing to waive")
    if state["push"]:
        if state["no_check"] or state["skipped"]:
            _err("a release with skipped gates (--no-check) can't --push; nothing was changed")
        if not _grant(p):
            _err(_NEED_GRANT + "; nothing was changed")
        from sampo import replay  # T-1401: the guard's own corpus replay is green and recent, or the waiver is on the ledger
        note = replay.enforce(p, "fm release --push", getattr(args, "stale_ok", None))
        if note:
            print(f"  {note}")
        url = _git(root, "remote", "get-url", state["remote"], ok=True).strip() or state["remote"]
        if os.environ.get("FOREMAN_RELEASE_FM") and not os.path.isdir(url):
            _err("FOREMAN_RELEASE_FM (a stub fm for the gates, for tests) can't --push to a real remote; nothing was changed")
    ctx = {"root": root, "version": v, "ui": state["ui"], "push": state["push"], "remote": state["remote"],
           "old": state["old"], "p": p, "args": args, "state": state}

    def tree(_):
        if _git(root, "status", "--porcelain").strip():
            _err("the tree has uncommitted changes; commit or finish them first")

    fns = {"tree": tree, "check": lambda _: _gate(root, "check"), "bump": _bump, "changelog": _changelog,
           "commit": _commit, "release-check": lambda _: _gate(root, "release-check"), "canary": _canary,
           "push": _push, "round": _round}
    todo = [i for i, (n, _) in enumerate(STEPS)
            if n not in state["done"] and n not in state["skipped"] and (n != "push" or state["push"])]
    if not todo:
        print(f"Sampo {v} is already released: every step is done")
        return 0
    if resume:
        print(f"resuming at step {todo[0] + 1} ({STEPS[todo[0]][0]})")
    for i in range(len(STEPS)):
        name = STEPS[i][0]
        if i not in todo and not (name == "push" and not state["push"]):
            continue
        print(f"step {i + 1}/{len(STEPS)} {name}", flush=True)  # a log file shows where a long run is
        if name == "push" and not state["push"]:
            print("  (no --push: nothing published; fm release V --push does it later)")
            continue  # not recorded: a later --push still has it to do
        if state["no_check"] and name in GATES:
            print("  skipped (--no-check)")
            state["skipped"].append(name)
        else:
            try:
                fns[name](ctx)
            except Exception as e:
                _dump(state_path, state)
                if type(e).__name__ == "UsageError":
                    raise type(e)(f"step {i + 1} {name}: {e}\nstopped at step {i + 1} ({name}); fix it, then: "
                                  f"fm release {v} --resume")
                raise
            state["done"].append(name)
        _dump(state_path, state)
    print(f"Sampo {v} released: " + (f"{len(state['skipped'])} gate(s) skipped ({', '.join(state['skipped'])}); "
                                       if state["skipped"] else "") + ("pushed" if "push" in state["done"] else "not pushed"))
    return 0


if __name__ == "__main__" and len(sys.argv) == 4 and sys.argv[1] == "--canary-worker":  # _canary's detached child
    with c.gate_lock("canary", say=lambda m: print(m, flush=True)):  # T-1668: queues behind fm check and release gates
        _canary_worker(sys.argv[2], sys.argv[3])
