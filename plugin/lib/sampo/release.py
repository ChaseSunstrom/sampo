"""fm release VERSION (T-1188): the release ritual as named steps with a checkpoint after each (state/release.json),
so `--resume` continues from the first unfinished one and `--dry-run` only prints the plan. Sampo's own repo only
(plugin/.claude-plugin/plugin.json at its root; release.py is a thin wrapper over this). --push merges into the
remote's main (origin by default) from a temporary worktree behind the active task's publish grant, which it spends
before the push. The first run's flags (--ui, --push, --remote, --no-check) are kept in release.json for a resume;
a skipped step is never recorded as done, and a release with skipped gates can't push."""
import codecs
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

if __name__ == "__main__":  # the canary worker runs this file: lib/sampo first on the path would shadow the stdlib (secrets)
    _here = os.path.dirname(os.path.realpath(__file__))
    sys.path[:] = [os.path.dirname(_here)] + [p for p in sys.path if os.path.realpath(p or ".") != _here]

from sampo import core as c

# FOREMAN_RELEASE_FM: tests point the gates and the canary at a stub fm
FM = [sys.executable, os.environ.get("FOREMAN_RELEASE_FM") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(os.path.dirname(__file__)))), "bin", "fm")]
STEPS = [("tree", "the working tree is clean"),
         ("check", "fm check passes (the full gates)"),
         ("bump", "plugin.json, both marketplace.json entries, and foreman-ui's version when it changed"),
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


def _named_grant(p, tid):
    """T-2007: fm ship --task ID: that task (closed or active) when it holds the publish grant."""
    b = c.find_brief(p, tid) or _err(f"no task {tid.upper()}; nothing was pushed")
    return b if "publish" in (b.meta.get("allow") or []) else _err(
        f"{b.id} has no publish grant (fm task set {b.id} --allow publish); nothing was pushed")


def _spend(p, tid, by):
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
    return bool(_git(root, "diff", "--name-only", f"{last}..HEAD" if last else "HEAD", "--", "mods/foreman-ui").strip())


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
        _err("mods/foreman-ui changed since the last release: pass --ui VERSION")
    pj = os.path.join(root, "plugin", ".claude-plugin", "plugin.json")
    mk = os.path.join(root, ".claude-plugin", "marketplace.json")
    uj = os.path.join(root, "mods", "foreman-ui", ".claude-plugin", "plugin.json")
    dj = os.path.join(root, DESIGN_PJ)
    data = _json(mk)  # everything read before anything is written
    if ui and not os.path.isfile(uj):
        _err("no mods/foreman-ui to give --ui to")
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
            files.append("mods/foreman-ui/.claude-plugin/plugin.json")
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


def _push(ctx):
    root, v, p, remote = ctx["root"], ctx["version"], ctx["p"], ctx["remote"]
    b = _grant(p)
    if not b:
        _err(_NEED_GRANT)
    sha = ctx["state"].get("sha") or _err(f"no release commit recorded for {v}: fm release --abandon, then release again")
    _git(root, "fetch", "-q", remote, "main")
    with tempfile.TemporaryDirectory() as tmp:
        wt = os.path.join(tmp, "rel")
        _git(root, "worktree", "add", "-q", "--detach", wt, "FETCH_HEAD")
        main = _git(wt, "rev-parse", "HEAD").strip()
        try:
            if c._git(wt, "merge", "-q", "--no-ff", sha, "-m", f"Foreman {v}", timeout=300, proc=True).returncode:
                _git(wt, "merge", "--abort", ok=True)
                _land_over_picks(wt, sha, v)  # T-2598: ship cherry-picked the branch onto main
            landed = _git(wt, "rev-parse", "HEAD").strip()
            _refuse_private(wt, _git(wt, "rev-list", f"{main}..{landed}").split(), landed)  # T-3529: as fm ship does
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


def _private_hits(root, picks, tip, paths=None):
    """(commits, files) of _refuse_private's scan, files named redacted; paths: only those files of tip's tree (T-3551:
    the tower checks the files its commit touches, so a file already on main can't hold its pushes back)."""
    from sampo import models, sandbox  # a secret-named line only: a base URL or a port there is no secret
    terms, values = c.private_terms(), [v for k, v in models.secret_pairs() if len(v) >= 8 and sandbox.secret(k)]
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
        _err(f"git grep failed: {c.plain(r.stderr.strip())[:200]}; nothing was pushed")
    blobs = dict((ln.partition("\t")[2], ln.split()[2]) for ln in _git(root, "ls-tree", "-r", "-z", tip).split("\0") if ln)
    blobs = {f: b for f, b in blobs.items() if paths is None or f in paths}
    hits = {x.split(":", 1)[1] for x in r.stdout.split("\0") if ":" in x}  # by blob id: a path may hold a term too
    files = sorted({f for f in hits if f in blobs and bad(_git(root, "cat-file", "blob", blobs[f]))} | set(filter(bad, blobs)))
    def shown(f):  # a path that holds a term or a value is named redacted
        for v in values:
            f = f.replace(v, "[REDACTED]")
        return c.redact_local(f)
    return commits, [shown(f) for f in files]


def _push_main(p, tid, sha, remote, by):
    """Push sha to remote's main from the repo (a relative remote URL resolves there). The grant is spent first, so one
    yes is one push even for two racing callers, and given back if git push fails (T-1943)."""
    _spend(p, tid, by)
    try:
        _git(p.root, "push", "-q", remote, f"{sha}:refs/heads/main")
    except Exception:
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


def _verified(p, root, said, base, last):
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
    line = _git(root, "log", "--first-parent", "-n", "500", "--format=%H %T", "HEAD", f"^{base}",
                *([f"^{last}"] if last else []), ok=True).split()
    for sha, tree in zip(line[::2], line[1::2]):
        x = passes.get(tree)
        if x and not (last and c._git(root, "merge-base", "--is-ancestor", last, sha, timeout=60, proc=True).returncode):
            return sha, f"the post-merge verify's full run on {str(x.get('sha'))[:9]} ({x.get('at')})"
    return None, None


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


def _run_test(p, wt, base, env, test, log):
    """The ship's own run in the scratch tree, streamed: the affected tests first, then `test`. (exit code, the last
    1 MB of its output)."""
    _affected_first(p, wt, base, env)  # T-2323: a red linked test stops the ship in minutes, not after the suite
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


def _rerun_red(wt, out, code, env):
    """T-3435: the classes a red run names, rerun alone after a load wait (the post-merge suite's helpers, T-3216).
    Returns the flaky classes when every one is green alone, None when any is still red (or can't run alone)."""
    from sampo import lanes
    if code == 124:
        return None
    classes = lanes.pm_classes(out, code)
    again = lanes.pm_rerun_cmd(wt, classes)
    if not again:
        return None
    c.wait_for_load("sampo ship", out=sys.stdout)
    print(f"{', '.join(classes)} failed in the full run; rerunning alone", flush=True)
    if c.run_command(wt, again, c.load_scaled(900), env=env, niced=True)[0]:  # T-4197
        return None
    print(f"flaky (red in the full run, green alone): {', '.join(classes)}", flush=True)
    return classes


def _affected_first(p, wt, base, env, only=None):
    """T-2323: fm check's affected-test selection over the range (main..the cherry-picked tip) in the scratch tree, run
    before the full suite; red stops the ship naming the failing classes. No linked test or no affected command: goes on.
    `only`: exactly these files instead of the range (T-3533). The tests run green ([] when none is linked), or None
    when no selection could be made."""
    from sampo import cli
    try:
        cmd, tests, _ = cli.affected(c.Project(p.slug, wt, p.dir, p.lane), since=base, only=only)
    except Exception as e:  # a convenience: the full suite still runs
        print(f"affected tests skipped: {c.fit(c.plain(str(e)), 120)}", flush=True)
        return None
    if not cmd:
        return []
    print(f"testing the {len(tests)} affected test file(s) " + (f"of {c.joinmore(only, 5)}" if only else "first"), flush=True)
    code, out = c.run_command(wt, cmd, 900, niced=True,  # T-4197
                              env=dict(cli._skip_env({}), **{k: v for k, v in env.items() if k != "FOREMAN_SKIP_TESTS"}))
    if code and _rerun_red(wt, out, code, env) is None:
        print(out[-4000:], flush=True)
        bad = sorted({m.group(1) for m in re.finditer(r"^(?:FAIL|ERROR): \S+ \(([\w.]+?)\.\w+\)", out, re.M)})
        _err("the affected tests failed in the scratch tree" + (f": {', '.join(bad)}" if bad else "") + "; nothing was pushed")
    return tests


def _tower_files(root, last, base):
    """T-2955: the files main moved since the last ship (by content) inside fm genesis live's write set (rune.live adds
    site/genesis and the stats file): the tower publishes them to main behind every branch, so main's copy is the one
    to keep. Everything else reaches main only through a ship, so a move there is someone else's and still stops it."""
    from sampo import rune
    moved = _git(root, "diff", "--name-only", last, base).splitlines()
    return {f for f in moved if f.startswith("site/genesis/") or f == rune.STATS}


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


def ship(p, args):
    """T-1943: fm ship — HEAD's commits the remote's main lacks (git cherry, by patch) or the named ones, cherry-picked
    onto the fetched main in a scratch worktree, their files compared, tested there, main re-checked, then pushed
    behind the publish grant. The working tree is never touched; a failure pushes nothing and keeps the grant."""
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
    _git(root, "fetch", "-q", remote, "main")
    base = _git(root, "rev-parse", "FETCH_HEAD").strip()
    picks = []
    for x in args.commits or []:
        sha = "" if x.startswith("-") else _git(root, "rev-parse", "--verify", "-q", "--end-of-options", f"{x}^{{commit}}", ok=True).strip()
        picks.append(sha or _err(f"{x!r} isn't a commit here"))
    last = _git(root, "rev-parse", "--verify", "-q", SHIPPED, ok=True).strip()  # T-1945: main may hold these under another patch
    if last and c._git(root, "merge-base", "--is-ancestor", last, "HEAD", timeout=60, proc=True).returncode:
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
    target, full = "HEAD", None
    if not picks and not args.check and not getattr(args, "head", False):
        # T-3666: the newest commit the post-merge verify ran the whole suite on, so the ship needn't run it (~80 CPU-min)
        try:
            target, full = _verified(p, root, said, base, last)
        except Exception as e:  # a convenience: HEAD ships, tested, as before
            target, full = None, None
            print(f"post-merge verify reuse skipped: {c.fit(c.plain(str(e)), 120)}", flush=True)
        picks = _picks(target) if target else []
        target, full = (target, full) if picks else ("HEAD", None)
    picks = picks or _picks("HEAD")
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
    b = (_named_grant(p, args.task) if getattr(args, "task", None)
         else _grant(p) or _err(_NEED_GRANT.replace("--push", "fm ship", 1) + "; nothing was pushed"))
    if args.test:  # T-1950: remembered per repo, so the oldest runtime's suite isn't retyped each ship
        _git(root, "config", "--local", "foreman.shiptest", args.test, ok=True)
    test = shlex.split(said) if said else [*FM, "check"]
    if full:
        k = int(_git(root, "rev-list", "--count", "--first-parent", f"{target}..HEAD").strip() or 0)
        print(f"shipping {target[:9]}, the newest commit the post-merge verify passed in full; " + (
            f"{k} newer commit{'s' if k > 1 else ''} on the branch wait{'' if k > 1 else 's'} for the next verified one"
            if k else "it is HEAD"), flush=True)
    files = sorted({f for s in picks for f in _git(root, "diff-tree", "--no-commit-id", "--name-only", "-r", "-m", s).splitlines() if f})
    n = f"{len(picks)} commit{'s' if len(picks) > 1 else ''}"
    with tempfile.TemporaryDirectory() as tmp:
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
            from sampo import ambient
            c.wait_while(lambda: c.gates_live(p) or ambient.running(p),  # T-1978: never two suites at once
                         "a running fm check or ambient test run", "fm ship", out=sys.stdout)
            print(f"testing {n} on {remote} main: {shlex.join(test)}", flush=True)
            env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}  # GIT_DIR from a hook: not the user's repo
            tested = _git(wt, "rev-parse", "HEAD^{tree}").strip()  # T-2642: the tree the suite runs on is the tree pushed
            c.update_meta(p, ship_testing={"pid": os.getpid(), "at": c.now(), "root": os.path.realpath(wt)})
            t0 = time.monotonic()
            env.setdefault("PYTHONUNBUFFERED", "1")
            env[c.SLOT_PRIORITY] = env.get(c.SLOT_PRIORITY) or "1"  # T-3614: its runs take freed CPU slots first
            log = os.path.join(tmp, "test.log")  # T-2255: a file, not a pipe: a background process holding it can't hang the ship
            failed, tail, hit, rest = 0, "", None, []
            try:  # a full fm check here waits for this run; the ship's own fm check (in wt) doesn't
                try:
                    hit, rest = _gated_pass(p, root, wt, base, said, target, full)  # T-3468
                except Exception as e:  # review: a check that can't run falls back to the suite
                    print(f"gates pass reuse skipped: {c.fit(c.plain(str(e)), 120)}", flush=True)
                # T-3533: main moved files tests read (the tower's daily stats): only their tests run, red stops the ship
                ran = _affected_first(p, wt, base, env, only=rest) if hit and rest else []
                hit = hit if ran is not None else None
                if hit:
                    top = (f"; in {c.joinmore(rest, 5)} its affected tests passed "
                           f"({c.joinmore([os.path.basename(t) for t in ran], 8) or 'none linked'})") if rest else ""
                    print(f"reusing {hit}: the pushed tree differs from it only in files its CI "
                          f"skips or the sync mirror{top}, so the suite isn't run again", flush=True)
                else:
                    failed, tail = _run_test(p, wt, base, env, test, log)
            finally:
                if (c.read_meta(p).get("ship_testing") or {}).get("pid") == os.getpid():
                    c.update_meta(p, ship_testing=None)
            # T-3482: its typical time, for selfcheck.slow_jobs while a later ship's run is live
            c.log_event(p, "ship_test", data={"cmd": c.fit(shlex.join(test), 200), "exit": failed,
                                              "s": round(time.monotonic() - t0, 1)})
            if failed and _rerun_red(wt, tail, failed, env) is not None:
                failed = 0  # T-3435: green alone is a flake; the baseline capture and the verdict get the rerun's
            from sampo import cli  # the shipped tree is vouchable, so the run files (or clears) what fm check would
            try:
                got = [] if hit else cli._baseline(p, [(shlex.join(test), failed, tail, None)])  # a reuse ran nothing
            except Exception as e:  # the verdict stands; the capture is a convenience
                got = []
                print(f"baseline capture failed: {e}")
            m = re.search(r"\bT-\d+\b", " ".join(got))
            if failed:
                _err("the test failed in the scratch tree" + (f" (baseline task {m.group(0)})" if m else "")
                     + "; nothing was pushed")
            for ln in got:
                print(ln)
            rules = os.path.join(wt, "plugin", "rules", "foreman.md")
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
                    moved = _files(base, new)
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
    print(f"shipped {n} to {remote} main at {tip} (the publish grant is spent)."
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
    task's publish grant stops after the local tag and names the push."""
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
