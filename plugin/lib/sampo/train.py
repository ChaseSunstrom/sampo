"""T-3380: sampo lane train, a merge queue. Reviewed lanes merge one after another in a scratch integration worktree
(never the main checkout); the project's gates run once on the result; a red run names its culprit lanes from its own
output (T-3602: _blame), bisecting by prefix only what it can't pin on one lane, each probe the red gate alone on the
red classes alone; the culprits are dropped and a narrowed confirm run checks the rest; the main checkout's branch then
fast-forwards to the gated tip.
The train's check_run carries the code tree (core.code_tree: no CHANGELOG.md, no sync mirror), so `sampo check` after
each close reuses the pass while the code is unchanged. T-3544: the full-suite gate runs only the tests the batch's
changes reach (_affected_gate); the full suite then runs on the landed merges in the background (lanes.pm_run)."""
import concurrent.futures
import contextlib
import fcntl
import functools
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
import time

from sampo import core as c
from sampo.lanes import CLOSE_MARK, CriterionRed, _git, _ref_ok, _verify, clear_git_locks, cpu_env, git_lock_lines, \
    pm_after_train, pm_record, pm_rerun_cmd, pm_wait, train_lock, train_lock_closing

FLAKY_ROUNDS = 8  # T-4676: a hard bound on the rounds of flaky classes one train goes on past (the cap proper is per class)
CUT_RERUNS = 1  # T-4889: a suite gate cut at its deadline with nothing failed runs once more; cut again, the train stops
WT_GONE = "the train's scratch worktree was removed by something else"  # T-4728: said by the first loss and the second
SKIPPED = "probe skipped"  # the note a held green run leaves; the report counts by it (T-3596)
PROBE_JOBS = 3 # T-3531: pre-filter probes at once
_FAILED = re.compile(r"^(?:FAIL|ERROR): (\S+) \(([\w.]+)\)", re.M)  # unittest's red line: test (where)
_LISTED = re.compile(r"^  failed class: (\S+)", re.M)  # run.py's every red class, one per line, never-started ones too
_CUT = re.compile(r"^  not run class: (\S+)", re.M)  # T-3936: run.py's classes the deadline cut (the train runs them too)
_COUNT = re.compile(r"^FAILED \((\d+) classes", re.M)  # run.py's summary count
_ANY_RED = re.compile(r"^(?:FAIL|ERROR): |^FAILED \(|^  failed class: ", re.M)  # T-4889: a run with any of these failed something
_FIXTURES = ("setUpModule", "tearDownModule", "setUpClass", "tearDownClass")  # unittest names the module or class
_FRAME = re.compile(r'^\s*File ".*", line \d+.*$', re.M)  # a traceback frame: the test and the code on its stack
_stem = lambda t: os.path.basename(t).rsplit(".", 1)[0]  # noqa: E731  a test file's module name


class _Lost(Exception):
    """T-4728: a scratch worktree the train made is no longer a worktree: something else removed it."""


def _lost_check(wt):
    """T-4728: raise _Lost when `wt`'s folder is gone or no longer holds a worktree (its .git file goes with it). A run in
    a vanished folder ends 127, which reads as a red base or a guilty lane: it is checked before and after each run."""
    if not os.path.exists(os.path.join(wt, ".git")):
        raise _Lost(wt)


def _rebuilding(p, what, attempt):
    """T-4728: run `attempt()`, a whole piece of work on its own scratch tree that raises _Lost when the tree goes. The
    first loss says so and runs it again on a new tree; a second raises PolicyError naming the loss, so what it was
    judging (a lane, the base) is never called red for a loss. `attempt` removes its own tree (try/finally)."""
    for n in (1, 2):
        try:
            return attempt()
        except _Lost as e:
            with contextlib.suppress(Exception):
                c.log_event(p, "lane_train_wt_gone", data={"n": n, "what": what, "tree": os.path.basename(str(e)),
                                                           **c.scratch_loss(str(e))})  # T-4735: folder, time, age, root
            if n == 1:
                print(f"sampo lane train: {WT_GONE} ({what}); rebuilding it once", file=sys.stderr)
    raise c.PolicyError(f"{WT_GONE} again ({what}): the second loss, so it stops here with nothing landed, no lane blamed "
                        "and the base not called red; run it again")


def _mod(k):
    """A red name's module: the part before its class (mod.Class.test, pkg.mod.Class), else its last part (mod)."""
    parts = k.split(".")
    return parts[max(next((n for n, x in enumerate(parts) if x[:1].isupper()), len(parts)) - 1, 0)]


def _affected_gate(p, wt, base, checks):
    """T-3544: (the full-suite gate, the affected template, the tests the merged batch reaches) or None to run every
    gate as configured. The full suite is the gate equal to the affected template's runner with no names (`python3
    plugin/tests/run.py`); the tests are cli.affected's since the base on the merged tree, as the probes pick them,
    the rule tests among them. None: FOREMAN_FULL_GATE=1, no such gate, or nothing picked (the full suite decides)."""
    from sampo import cli
    meta = c.read_meta(p)
    tpl = meta.get("affected") or ""
    full = cli.suite_gate(p.root, tpl, checks)  # T-3566: a close checks its linked tests against this part the same way
    if os.environ.get("FOREMAN_FULL_GATE") or not full:
        return None
    try:
        _, tests, _ = cli.affected(c.Project(p.slug, wt, p.dir, p.lane), since=base)
    except Exception:  # no affected command or no map: the full suite decides
        return None
    tests = [t for t in tests if not t.endswith(tuple(meta.get("affected_by") or ()))]  # another runner's: its own gate
    return (full, tpl, tests) if tests else None


# the operator switches (tests' run.OPERATOR_SWITCHES) that change what a test or gate does; per-caller identity
# (DRIVE_TASK, QUIET, INHIBITED, GATE_HELD) and slot variables are not among them
_DIGEST_ENV = {"LOAD_GATE", "FULL_GATE", "SENTINEL", "SENTINEL_RUN", "SENTINEL_TREE", "NO_BACKGROUND", "NO_SUITE_SLOT",
               "SLOW_GATE", "NO_NET", "NO_LINK", "CONFINE", "SMOKE_RUNNER"}


def _env_digest():
    """A digest of the gate-changing switches in the FOREMAN_/SAMPO_ environment."""
    import hashlib
    import json
    env = sorted((k.split("_", 1)[1], v) for k, v in os.environ.items()
                 if k.startswith(("FOREMAN_", "SAMPO_")) and k.split("_", 1)[1] in _DIGEST_ENV)
    return hashlib.sha1(json.dumps(env).encode()).hexdigest()


def _green_path(p):
    return os.path.join(p.dir, "train_green_env.json")


def _mark_green(p):
    """Record the env of a train whose gate went green: the next train with the same env needs no base check."""
    import contextlib
    import json
    path = _green_path(p)
    with contextlib.suppress(OSError):  # a failed record only costs the next train one base check
        with open(path + ".tmp", "w") as f:
            json.dump(_env_digest(), f)
        os.replace(path + ".tmp", path)


def _base_red(p, main, base, sub):
    """T-3625: why the base's own affected tests (the batch's picks that exist on it) fail, or ''. A red base then
    stops the train in minutes, not after its full gate and bisection. It runs only when the FOREMAN_/SAMPO_
    environment differs from that of the last train whose gate went green (_mark_green), whatever its base: in
    steady state it costs nothing, and an env change that turns the base red is caught. A genuinely red base is
    still the per-lane probe's and the bisection's to name."""
    import json
    try:
        with open(_green_path(p)) as f:
            if json.load(f) == _env_digest():
                return ""
    except (OSError, ValueError):
        pass
    return _rebuilding(p, "the base probe", lambda: _base_red_once(main, base, sub))  # T-4728


def _base_red_once(main, base, sub):
    """_base_red's run on one scratch tree; raises _Lost when something else removes the tree (the caller reruns it once)."""
    wt = c.scratch_tree("sampo-base-", main)  # T-4735: in Sampo's own scratch root, locked while this process lives
    try:
        if _git(main, "worktree", "add", "-q", "--detach", wt, base).returncode:
            return ""
        _lost_check(wt)
        ts = [t for t in sub[2] if os.path.exists(os.path.join(wt, t))]
        if not ts:
            return ""
        run = sub[1].replace("{tests}", shlex.join(ts)).replace("{names}", shlex.join(map(_stem, ts)))
        def go(cmd):  # yields to the machine's load and to other work, as _probe's runs do
            c.wait_for_load("sampo lane train")
            if shutil.which("nice"):
                cmd = "nice -n 10 bash -c " + shlex.quote(cmd)
            _lost_check(wt)
            got = c.run_command(wt, cmd, c.load_scaled(1800))
            _lost_check(wt)  # a run in a vanished folder ends 127: that is the loss, not a red base
            return got

        code, out = go(run)
        if code not in (0, 5, 124):  # one flake rerun of the same affected tests
            code, out = go(run)
        if code in (0, 5, 124):  # a run of no tests or a timeout proves nothing: the full gates decide
            return ""
        return _why_red({"cmd": sub[1], "affected": ts, "failing": _failed(out),
                         "tail": out.rstrip().splitlines()[-15:]})
    finally:
        _git(main, "worktree", "remove", "--force", wt)
        shutil.rmtree(wt, ignore_errors=True)
        c.scratch_release(wt)


def _why_red(r, blame="", tail=None):
    """T-3544: a red gate run as a reason: the gate (on how many affected modules), the classes it names failing, what
    blamed the lane (T-3602) and the last 15 lines (`tail`: the blamed class's), so nobody reruns a suite to learn them."""
    s = r["cmd"] + (f" on {len(r['affected'])} affected test module(s)" if r.get("affected") else "")
    s += f", failing {c.joinmore(r['failing'], 8)}" if r.get("failing") else ""
    s += f"; blamed: {blame}" if blame else ""
    return s + "".join("\n    " + ln for ln in (r.get("tail") if tail is None else tail) or ())


def _red(out):
    """Every test name a red run fails, or [] when it can't be sure it has them all (then nothing narrows to them and
    the gate reruns whole). unittest's FAIL/ERROR lines give the class (mod.Class.test), the module or class a fixture
    failed in (setUpModule (mod)) and a module that won't import (unittest.loader._FailedTest.mod: by its name);
    run.py's `failed class:` lines add the ones that failed whole and its `not run class:` lines the ones that never started. [] when a line names nothing runnable, or
    run.py's count disagrees with its lines. A name inside another (mod.Class.test in mod.Class) is dropped."""
    got, lines = set(), _FAILED.findall(out)
    for test, where in lines:
        got.add(test if where.startswith("unittest.loader._FailedTest") else where if test in _FIXTURES
                else where.rsplit(".", 1)[0] if "." in where else "")
    listed = set(_LISTED.findall(out))
    cut = set(_CUT.findall(out))  # T-3936: classes the deadline cut: run, but not in FAILED's count
    count = _COUNT.findall(out)
    whole = len(lines) == len(re.findall(r"^(?:FAIL|ERROR): ", out, re.M)) and "" not in got  # every line read
    if (count and int(count[-1]) != len(listed)) or not (listed and count or whole):
        return []
    names = {n for n in got | listed | cut if n and not n.startswith("unittest.")}
    if any(n.startswith("unittest.") for n in listed) and not any(w.startswith("unittest.") for _, w in lines):
        return []  # run.py says a module failed to import but no line says which
    return sorted(n for n in names if not any(n.startswith(m + ".") for m in names))


def _cut(out):
    """T-4889: the classes a run's deadline cut (run.py's `not run class:` lines). They never ran, so they failed nothing:
    a train runs them again, it never probes a base on them or blames a lane for them."""
    return set(_CUT.findall(out or ""))


def _failed(out):
    """T-4889: _red's names without the ones the deadline cut: what really failed ([] when _red can't be sure)."""
    cut = _cut(out)
    return [n for n in _red(out) if n not in cut]


def _nothing_failed(row):
    """T-4889: a red row that only ran out of time: ended 124 or with cut classes, and no line says anything failed."""
    out = row.get("out") or ""
    return (row["exit"] == 124 or bool(_cut(out))) and not _ANY_RED.search(out)


def _defines(path):
    """T-4889: the names a test module defines at its top level (classes, assignments, imports, also under if/try), or
    None when that can't be told (unreadable, a syntax error, `import *`, classes made at run time: globals(), vars(),
    setattr(, exec(). A probe names
    only classes its tree has: the base lacks a class a lane adds to an old module, and tests/run.py fails a whole run
    on a name it can't match ('no test matches'), which stopped three lands on 2026-11-17."""
    import ast
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
        tree = ast.parse(text)
    except (OSError, SyntaxError, ValueError):
        return None
    if any(k in text for k in ("globals()", "locals()", "vars()", "setattr(", "exec(")):  # classes made at run time
        return None
    got = set()

    def walk(body):
        for node in body:
            if isinstance(node, ast.ClassDef):
                got.add(node.name)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                for t in node.targets if isinstance(node, ast.Assign) else [node.target]:
                    got.update(n.id for n in ast.walk(t) if isinstance(n, ast.Name))
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for a in node.names:
                    if a.name == "*":
                        raise LookupError
                    got.add((a.asname or a.name).split(".")[0])
            elif isinstance(node, (ast.If, ast.Try, getattr(ast, "TryStar", ast.Try), ast.With, ast.For, ast.While,
                                   ast.Match)):
                for part in ("body", "orelse", "finalbody"):
                    walk(getattr(node, part, None) or [])
                for h in [*(getattr(node, "handlers", None) or []), *(getattr(node, "cases", None) or [])]:
                    walk(h.body)
    try:
        walk(tree.body)
    except LookupError:
        return None
    return got


@functools.lru_cache(maxsize=None)
def _machine_patterns():
    """T-4658: (doctor's disk FAIL line, any other failing line), both built from doctor's own rendering (doctor.result_line
    and its DISK name), so the late matcher can't drift from the line doctor prints. T-4652: doctor's disk line is the
    machine's fail line. doctor loads here, not at import: a train reads it only at a red gate."""
    from sampo import doctor
    head = doctor.result_line(doctor.Result(doctor.DISK, "FAIL", "")).rstrip()
    fail = re.compile(rf"^{re.escape(head)}\b.*$", re.M)
    other = re.compile(rf"^(?:FAIL|ERROR)(?::|[ \t]+(?![ \t]*{re.escape(doctor.DISK)}\b))", re.M)
    return fail, other


def _machine_only(out):
    """T-4652: True when a red run's only failing line is doctor's disk FAIL: a machine condition, the same whatever the
    code is, never a red base. Any other failing line (a test, another doctor check) makes it a code failure."""
    fail, other = _machine_patterns()
    return bool(fail.search(out)) and not other.search(out)


def _machine_refusal(p, out):
    """T-4652: the refusal for a gate red only on the machine: it names the disk line, and that no base or lane is
    blamed (nothing is dropped for it; the train stops and the gate can run again once the disk has room)."""
    line = c.fit(_machine_patterns()[0].search(out).group(0).strip(), 200)
    c.log_event(p, "lane_train_machine", data={"line": line})
    return c.PolicyError(f"refused: the machine, not the base or a lane, fails the gate: {line}; no lane dropped for it, "
                         "nothing landed: free disk space (sampo tidy shows what Sampo could free) and run it again")


def machine_stop(p):
    """T-4652: the refusal a train gives before it merges or tests anything when the machine fails every gate whatever
    the code is: the disk at doctor's fail level (under 5 GB free, lanes.disk_level). The line is doctor's own (the free
    space, what Sampo could free and its verb). '' at warn or ok, and when the disk can't be read: those trains run."""
    from sampo import doctor, lanes
    try:
        u = lanes.disk_usage(c.state_dir())
    except OSError:
        return ""
    if lanes.disk_level(u) != "fail":
        return ""
    r = doctor.check_disk(verdict=True)  # T-4658: the refusal needs the free space, not what could be freed (sized: 10 s)
    if r.status != "FAIL":
        return ""
    detail = r.detail if "sampo tidy" in r.detail else r.detail + "; sampo tidy shows what Sampo could free"
    c.log_event(p, "lane_train_machine", data={"free": int(u.free)})
    return f"refused before any merge or test: the machine, not the code, fails every gate: {detail}"


def _texts(out, names):
    """T-3602: {red name: its failure text}. unittest's and run.py's ==== rules split the output; a block belongs to
    the names in its first two lines (`==== mod.Class`, `FAIL: test (mod.Class.test)`); a name heading none gets it all."""
    blocks = re.split(r"(?m)^(?====)", out)
    heads = lambda k, b: re.search(rf"(?<![\w.]){re.escape(k)}(?!\w)", "\n".join(b.splitlines()[:2]))  # noqa: E731  # not a list: a block's two head lines
    return {k: "".join(b for b in blocks if heads(k, b)) or out for k in names}


def _blame(out, names, ids, diff, reach):
    """T-3602: who broke the gate, from its red run alone: {lane id: (why, red name)} for each red name (the whole
    output when it names none) one lane alone explains. Rules, strongest first, each narrowing the lanes the one
    before matched: (a) the failure text names a file the lane's diff changed (traceback frames aside: they name the
    test and the code on the stack, not the fault); (c) the lane changed the failing test; (b) its diff reaches the
    failing test (cli.affected's map, as the probes pick tests). A name matching no lane or several blames nobody."""
    blamed = {}
    for k, text in (_texts(out, names) if names else {"": out}).items():
        text, cand = _FRAME.sub("", text), list(ids)
        for n in range(3 if k else 1):
            hits = [(i, w) for i in cand if (w := _rule(n, k, text, diff(i), reach(i) if n == 2 else ()))]
            if len(hits) == 1:
                blamed.setdefault(hits[0][0], (hits[0][1], k))
                break
            cand = [i for i, _ in hits] or cand
    return blamed


def _rule(n, k, text, files, tests):
    """_blame's rule n for one lane (the files its diff changed, the tests that diff reaches) and red name k: why the
    lane is to blame, or ''."""
    if n == 0:
        return next((f"the failure output names {f}" for f in sorted(files)
                     if re.search(rf"(?<![\w.-]){re.escape(f)}(?![\w-])", text)), "")
    if n == 1:
        return next((f"it changed {f}, the failing test" for f in sorted(files) if _stem(f) == _mod(k)), "")
    return f"{_WEAK} what {_mod(k)} tests" if any(_stem(t) == _mod(k) for t in tests) else ""


_WEAK = "its diff touches"  # T-4289: rule (b)'s wording: a map link, no proof; a base red in the same class is checked first


def _same(a, b):
    """T-4289: test names a and b are one class's (equal, or one inside the other: mod.Class, mod.Class.test)."""
    return a == b or a.startswith(b + ".") or b.startswith(a + ".")


def _alone(row, names, out):
    """T-4289: (the names that fail on their own, the ones that don't) from `row`, the red `names` run alone, or None
    when that can't be told. A class red in a gate that passes alone is flaky under load (the gate's other classes run
    beside it), and tests/run.py's `failed class:` lines are what say each ran in a process of its own: for a gate that
    doesn't print them, or a run that names no failure, nothing here is proven and the old rules decide."""
    if row is None or (row["exit"] and not row.get("failing")):
        return None
    listed = set(_LISTED.findall(out))
    real = [n for n in names if row["exit"] and any(_same(f, n) for f in row["failing"])]
    flaky = [n for n in names if n not in real]
    return (real, flaky) if all(n in listed for n in flaky) else None


def _note_flaky(p, cmd, tree, out, names):
    """T-4289: the tests of the flaky `names` in flakes.json, as a red then a green run of `cmd` on one tree (what
    core.note_flakes turns into a flake); sampo doctor lists them by rate. The test ids it wrote ([]: the output named no
    failing test, nothing was recorded). A failure to record never costs the train."""
    ids = [i for i in c.failing_tests(out) if any(_same(i, n) for n in names)]
    text = "".join(f"FAIL: t ({i})\n" for i in ids)
    if ids:
        with contextlib.suppress(Exception):
            c.note_flakes(p, [(cmd, 1, text)], tree)
            c.note_flakes(p, [(cmd, 0, "")], tree)
    return ids


def _inconclusive(row, names):
    """T-4289 review: a red base probe that proves nothing (a timeout, no shell, or, when classes were asked for, a run
    that names none failing): it is neither red nor green on the base, so nothing is dropped, put back or called the
    base's on its account. A gate with no classes (a plain command) has none to name: its exit is its whole answer."""
    return row["exit"] in (124, 127) or bool(names and not row.get("failing"))


def _left_for(k, names):
    """T-4289 review: what a lane is left out for, to be put back if the base is red on it: the class blamed on it (k),
    or, for a bisection (k ''), the classes that failed alone (`names`), never a whole gate's failing set: a bisection
    proves only that the lane turns the gate red, not for which of its classes."""
    return {k} if k else set(names)


def _neighbours(root, mods, cap=8):
    """T-4676: the modules a flaky class's narrowed confirm runs: `mods` whole, then up to `cap` other test modules in the
    tree at `root` whose `# map-links:` header names a file one of `mods` names (the runner keeps no record of which
    classes shared a job, so the files they guard are what ties them: shared state shows in neighbours)."""
    from sampo import map as smap
    files = [f for f in _git(root, "ls-files", "-z").stdout.split("\0") if f]
    tests = {_stem(f): f for f in files if f.endswith(".py") and _stem(f).startswith("test_")}
    links = {w for m in mods if m in tests for w in smap._marked(root, tests[m])}
    near = sorted(s for s, f in tests.items() if s not in mods and links & set(smap._marked(root, f))) if links else []
    return sorted(mods) + near[:cap]


_MAP_FILE = re.compile(r"docs/map/[^/\0]+\.md")
_MARKER = re.compile(r"^(?:<{7}|>{7})(?: .*)?$|^={7}$", re.M)  # a conflict marker line


def _map_only(files):
    """T-4375: True when there are conflicted files and every one is a docs/map/*.md entry: two lanes that each add a
    sentence to one long entry clash textually though nothing disagrees. One code file among them: False."""
    return bool(files) and all(_MAP_FILE.fullmatch(f) for f in files)


def _union_map(wt, files):
    """T-4375: resolve the merge in `wt`, conflicted only in map files `files`, keeping both sides' lines: each file's
    three index stages through `git merge-file --union` (no base stage, both sides added it: an empty base). All results
    are read first and written only when every one is clean (no marker line, both sides present, a plain file); False
    leaves the merge as it was, for the caller to abort."""
    stages = {}  # path: {stage: mode}
    r = _git(wt, "ls-files", "-u", "-z")
    for rec in r.stdout.split("\0"):
        head, _, path = rec.partition("\t")
        parts = head.split()
        if len(parts) == 3 and path in files:
            stages.setdefault(path, {})[parts[2]] = parts[0]
    done = {}
    with tempfile.TemporaryDirectory(prefix="sampo-union-") as tmp:
        for f in files:
            got = stages.get(f, {})
            if "2" not in got or "3" not in got or any(m not in ("100644", "100755") for m in got.values()) \
                    or os.path.islink(os.path.join(wt, f)):
                return False  # deleted on one side, or not a plain file: a real disagreement
            paths = []
            for n, name in (("2", "ours"), ("1", "base"), ("3", "theirs")):
                out = subprocess.run(["git", "-C", wt, "show", f":{n}:{f}"], capture_output=True) if n in got else None
                if out is not None and out.returncode:
                    return False
                with open(os.path.join(tmp, name), "wb") as fh:
                    fh.write(out.stdout if out is not None else b"")
                paths.append(os.path.join(tmp, name))
            m = subprocess.run(["git", "merge-file", "--union", "-p", *paths], capture_output=True)
            text = m.stdout.decode("utf-8", "replace")
            if m.returncode or _MARKER.search(text):
                return False
            done[f] = m.stdout
    for f, data in done.items():
        with open(os.path.join(wt, f), "wb") as fh:
            fh.write(data)
        if _git(wt, "add", "--", f).returncode:
            return False
    return True


def _merge_in(wt, b, tip, log, kept=None):
    """Merge `tip` into the scratch tree; a conflict is aborted and logged (id, files). The new head sha or None.
    T-4375: a conflict only in docs/map/*.md entries is resolved keeping both sides' lines, the files recorded in
    `kept` (lane id: files) for the train to say and follow up."""
    ident = ("-c", "user.name=sampo", "-c", "user.email=sampo@localhost")
    msg = f"Merge {b.id}: {b.title}"
    if kept is not None:
        kept.pop(b.id, None)
    r = _git(wt, *ident, "merge", "--no-ff", "-m", msg, tip)
    if not r.returncode:
        return _git(wt, "rev-parse", "HEAD").stdout.strip()
    files = [f for f in _git(wt, "diff", "--name-only", "--diff-filter=U", "-z").stdout.split("\0") if f]
    if kept is not None and _map_only(files) and _union_map(wt, files) and \
            not _git(wt, *ident, "commit", "-m", msg).returncode:
        kept[b.id] = sorted(files)
        return _git(wt, "rev-parse", "HEAD").stdout.strip()
    _git(wt, "merge", "--abort")
    log.append((b.id, "conflicts with an earlier lane in the train" + (f": {c.joinmore(files, 8)}" if files else "")))
    return None


def _probe(p, main, base, b, tip, notes, tests=True):
    """T-3474: why this lane can't land, known before any full gate run, or ''. `git merge-tree` names a conflict with
    the base in milliseconds; the tests affected by the lane's change (cli.affected, as `sampo ship` runs them first)
    run on base+lane in a throwaway tree, naming a red lane in a minute instead of a bisection of full gate runs. A
    timeout (124) or a run of no tests (5) proves nothing, so the lane goes on to the gates. Before blaming, the red
    classes rerun once on base+lane (green: a flake) and then on the bare base (red there too: the base's); each goes
    on to the gates with a line in `notes`."""
    from sampo import cli
    r = _git(main, "merge-tree", "--write-tree", "--name-only", "--no-messages", base, tip)
    if r.returncode == 1:  # 1 is a conflict; any other failure (an old git) leaves it to the train's own merge
        files = [f for f in r.stdout.splitlines()[1:] if f]  # the first line is the merged tree
        if not _map_only(files):  # T-4375: a clash only in map entries is merged keeping both sides, by the train
            return "conflicts with the base" + (f": {c.joinmore(files, 8)}" if files else "")
    if not tests:  # T-3770: the gate runs this lane's affected tests itself, and a red gate names its lane (T-3602)
        return ""

    def attempt():  # T-4728: the tree can be removed under it; a second loss is the train's to stop, not this lane's fault
        seen = []
        return _probe_once(p, main, base, b, tip, seen, cli), seen
    try:
        why, seen = _rebuilding(p, f"the probe of {b.id}", attempt)
    except c.PolicyError as e:  # starts with WT_GONE: the train raises it (a child's result names a lane's reason)
        return str(e)
    notes.extend(seen)
    return why


def _probe_once(p, main, base, b, tip, notes, cli):
    """_probe's work on one scratch tree (base + the lane merged); raises _Lost when something else removes the tree."""
    wt = c.scratch_tree("sampo-probe-", main)  # T-4735
    try:
        if _git(main, "worktree", "add", "-q", "--detach", wt, base).returncode or _git(
                wt, "-c", "user.name=sampo", "-c", "user.email=sampo@localhost", "merge", "--no-ff", "--no-edit",
                tip).returncode:
            _lost_check(wt)  # a merge git could not run in a vanished folder is no refusal of the lane
            return ""
        _lost_check(wt)
        pw = c.Project(p.slug, wt, p.dir, p.lane)
        try:
            cmd, tests, changed = cli.affected(pw, since=base)
        except Exception:  # no affected command configured, or no map: the full gates still decide
            return ""
        if not cmd:
            return ""
        held = _ran_green(pw, b, tests, changed)
        if held:
            notes.append((b.id, f"{SKIPPED}: its own green run of these {len(tests)} test(s) on tree {held[:9]} "
                                "still holds (nothing they cover differs from base+lane)"))
            return ""
        def run(cmd):  # (exit, the classes it names red)
            cwd, cmd, _ = c.normalize_verify(wt, cmd)  # T-3411's cwd fix first: the nice wrapper would hide it
            env, _ = cli._venv_env(wt, cpu_env(p), cmd)  # T-4150: the project's own interpreter, as the lane ran it
            if shutil.which("nice"):  # the probe yields to whatever else the machine runs
                cmd = "nice -n 10 bash -c " + shlex.quote(cmd)
            _lost_check(wt)
            code, out = c.run_command(cwd, cmd, c.load_scaled(900), priority=True,  # T-3614: ahead of lane runs
                                      env=env)  # T-3757: a CPU rise is red here too
            _lost_check(wt)  # a run in a vanished folder ends 127: that is the loss, not a red lane
            return code, set(_failed(out))

        def again(classes):  # the red classes alone where the runner names them (pm_rerun_cmd), else cmd again
            return run(pm_rerun_cmd(wt, sorted(classes)) or cmd)

        code, bad = run(cmd)
        if code in (0, 5, 124):
            return ""
        if not bad:
            return f"its affected tests fail with the lane merged onto the base: exit {code}"
        code, red = again(bad)
        still = ((bad & red) or bad) if code else set()  # a flake is green on the rerun; a red naming none stays red
        if bad - still:
            notes.append((b.id, f"flaky (red once, green on a rerun): {c.joinmore(sorted(bad - still), 6)}"))
        _git(wt, "reset", "-q", "--hard", base)
        based = (still & again(still)[1]) if still else set()  # red without the lane too: the base's, not the lane's
        if based:
            notes.append((b.id, f"its affected tests are red on the base itself: {c.joinmore(sorted(based), 6)}; "
                                "left to the gates"))
        blame = still - based
        return "its affected tests fail with the lane merged onto the base: " + c.joinmore(sorted(blame), 6) if blame else ""
    finally:
        _git(main, "worktree", "remove", "--force", wt)
        shutil.rmtree(wt, ignore_errors=True)
        c.scratch_release(wt)
        _git(main, "worktree", "prune")


def _ran_green(p, b, tests, changed):
    """T-3531: a tree the lane's own evidence ran every one of `tests` green on (ran, exit 0, each named by path or
    bare name), when its diff to base+lane (p.root's HEAD) touches none of the tests, the sources the map links them to
    or the lane's changed files: that run still holds, so the probe needn't repeat it; '' when none does. Evidence keeps
    no base, but its tree is base+lane as it ran, which is the base's diff and the lane's later edits in one. A wrong
    skip costs a bisection, never a red landing: the full gates still run."""
    from sampo import map as smap
    links = smap.load(p).get("tests") or {}
    cover = set(tests) | set(changed) | {s for t in tests for s in links.get(t, [])}
    dirs = tuple(s for s in cover if s.endswith("/"))  # a folder link (site/) covers what's under it
    now = _git(p.root, "rev-parse", "HEAD^{tree}").stdout.strip()
    holds, named, last = {}, set(), ""
    for line in b.evidence():
        m, t = re.match(r"- (?:\((?:step|ac) \d+\) )?`(.*?)` → exit 0 ", line), c._TREE_MARK.search(line)
        if not (m and t and c._RAN_MARK in line) or c.NEVER_RAN in line:
            continue
        tree = t.group(1)
        if tree not in holds:
            d = _git(p.root, "diff", "--name-only", tree, now)
            holds[tree] = not d.returncode and not any(f in cover or f.startswith(dirs) for f in d.stdout.splitlines())
        if holds[tree]:
            named |= set(m.group(1).split())
            last = tree
    return last if last and all(t in named or os.path.basename(t).rsplit(".", 1)[0] in named for t in tests) else ""


MOVED = "the lane moved while merging: run it again"  # T-4756: its tip is not the one `lane merge` judged


def _held(p, b, main, tip):
    """The reason `sampo lane merge` would refuse this lane (private term, guard, self-change gate), or ''. T-4783: the
    private-term check is lanes.private_refusal, the function `merge` calls, first: a lane that carries a term never
    reaches a probe or a gate (the train once landed one that only ship met). Its reason names commit, file and line.
    T-4785: the body is lanes.land_holds, shared with fm serve's integrate and the pull-request train.
    T-4805 exempt: a hold with no landing of its own; _run records the landing (lanes.log_landed) after it fast-forwards."""
    from sampo import lanes
    return lanes.land_holds(p, b, main, tip)


def _none(head, left):
    """The refusal when no lane lands, carrying `left` [(id, why)] for sampo lane land to park each (T-3500)."""
    e = c.PolicyError(head + "; ".join(f"{i}: {w}" for i, w in left))
    e.left = list(left)
    return e


def _check(p, main, base, b, tip, probe_tests, judged=False):
    """(why this lane can't land or '', notes): the lane's own acceptance commands re-run in its worktree, as `lane merge`
    does, then conflicts and red tests against the base (T-3474), all before any gate run. T-4756: `judged` is a lane
    `lane merge` already ran the criteria of (or waived them, logged) at this tip: only the probe runs."""
    from sampo import cli
    seen = []
    try:
        try:
            if not judged:
                _verify(b)
        except CriterionRed as e:  # T-3629: red once, green on a rerun is a load flake, as _probe treats it
            _verify(b, only=e.n)  # that criterion alone: the others passed
            seen.append((b.id, f"flaky (red once, green on a rerun): {str(e).splitlines()[0][:160]}"))
    except (c.PolicyError, cli.UsageError) as e:
        return str(e).splitlines()[0], seen
    return _probe(p, main, base, b, tip, seen, probe_tests), seen


MARK = "SAMPO-CHECK "  # the line a lane's check process prints its result on (its stdout holds the criteria's own too)


def _child_argv(p, main, base, tid, tip, probe_tests, judged=False):
    """T-3119: the command line of one lane's check process: `python -m sampo.train JSON`, run in the main checkout."""
    arg = json.dumps({"project": [p.slug, p.root, p.dir, p.lane], "main": main, "base": base, "id": tid, "tip": tip,
                      "tests": bool(probe_tests), "judged": bool(judged)})
    return [sys.executable, "-m", "sampo.train", arg]


def _fan(argvs, cwd, jobs, timeout=7200):
    """T-3119: run each command line in its own process, `jobs` at once; the finished processes in the order given.
    The threads here only wait on their processes, so no check's work holds this interpreter's GIL. A process over
    `timeout` seconds is killed (rc 124)."""
    lib = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ, PYTHONPATH=lib + os.pathsep + os.environ.get("PYTHONPATH", ""))

    def one(argv):
        try:
            return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, errors="replace",
                                  timeout=timeout)
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(argv, 124, "", f"timed out after {timeout}s")
    with concurrent.futures.ThreadPoolExecutor(max(1, jobs)) as ex:
        return list(ex.map(one, argvs))


def _child_result(r):
    """(why or '', notes) from a lane's finished check process; its stderr passes on (the criteria's reuse lines)."""
    if r.stderr:
        sys.stderr.write(r.stderr)
    for line in reversed(r.stdout.splitlines()):
        if line.startswith(MARK):
            got = json.loads(line[len(MARK):])
            return got["why"], [tuple(x) for x in got["seen"]]
    tail = (r.stderr.strip().splitlines() or ["no output"])[-1]
    return f"its check process ended with exit {r.returncode} and no result: {c.fit(tail, 200)}", []


def _own_path_off():
    """T-4455: drop this lib's folder from PYTHONPATH once the child has imported sampo. Each parent (_fan, the ship's
    start, _spawn_check) adds it so `-m sampo.train` resolves; left in place it reached every command the child ran:
    a lane's check ran the lane's tests with the trunk's lib first on the path of each Python they started, and
    test_cli_speed.ForkServer and test_hooks.NoTaskGateKinds were red in every lane check that selected them."""
    lib = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    rest = [x for x in os.environ.get("PYTHONPATH", "").split(os.pathsep) if x and os.path.abspath(x) != lib]
    if rest:
        os.environ["PYTHONPATH"] = os.pathsep.join(rest)
    else:
        os.environ.pop("PYTHONPATH", None)


def _main(arg):
    """T-3119: the lane's check process: read the lane's task here, check it, print the result on one MARK line."""
    from sampo import cli
    _own_path_off()
    a = json.loads(arg)
    if "probe_pythonpath" in a:  # T-4455: what a command this child runs would inherit
        print(os.environ.get("PYTHONPATH"))
        return 0
    if "ship" in a:  # T-4239: the land's detached ship
        return _ship_child(a["ship"])
    if "accept" in a:  # T-4286: sampo lane accept's detached check of one lane
        return _accept_check_child(a["accept"])
    p = c.Project(*a["project"])
    why, seen = _check(p, a["main"], a["base"], cli.need_brief(p, a["id"]), a["tip"], a["tests"],
                    bool(a.get("judged")))
    print(MARK + json.dumps({"why": why, "seen": seen}))
    return 0


CLOSE_WAIT_S = 900.0  # T-4540: a hand close holds trunk across its finish (gates can run minutes): a train waits this long


def _closing(p):
    """T-4540: True while a live hand close holds the train's lock (lanes.train_lock_closing names it by pid)."""
    try:
        with open(os.path.join(p.dir, "train", CLOSE_MARK)) as f:
            pid = int(f.read().strip() or 0)
        if pid <= 0:
            return False
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return True


@contextlib.contextmanager
def _train_run_lock(p):
    """The train's lock for one run: a second train is refused after 1 s, but a hand close's commit is waited for."""
    with contextlib.ExitStack() as st:
        try:
            st.enter_context(train_lock(p, timeout=1))  # a second train, not a lane merge, waits on this one
        except c.LockTimeout:
            if not _closing(p):
                raise
            st.enter_context(train_lock(p, timeout=CLOSE_WAIT_S))  # LockTimeout again if the close outlasts the wait
        yield


def _after_train_run(p, main):
    """T-4540: after a train that held the lock: a red merge whose revert waited for it is reverted now. It never
    raises: the train's trunk is already fast-forwarded, so its return value or error must stand."""
    try:
        pm_after_train(p, main)
    except Exception as e:
        with contextlib.suppress(Exception):
            c.log_event(p, "postmerge_spawn_failed", data={"why": c.fit(str(e), 200)})


def train(p, main, ids=None, defer_verify=False, judged=None):
    """Land the named `ids` (a bare train would land lanes nobody picked) through one gate run, one train at a time.
    T-4756: `judged` {id: tip}: lanes whose criteria, guard and self-change gate `lane merge` already ran at that tip (it
    sends a single id here, as a train of one): the train runs no second judgment of a lane still at that tip, only its
    probe and gates; a lane whose branch moved since is refused (the new commits were judged by nothing).
    Returns a dict: merged, left_out [(id, why)], gate_runs, probes, tip, branch. Raises PolicyError when nothing can
    land, the base is red or the branch moved. T-4718: `defer_verify` leaves the background verify (and the revert pass)
    to the caller, who ships next and starts it after the ship was asked for (`verify_owed` says one is wanted): the
    verify and the ship's affected run compete for the same test slots."""
    from sampo import cli
    ids = list(dict.fromkeys(ids or []))
    if not ids:
        raise cli.UsageError("name the lanes to land: sampo lane train T-0001,T-0002 (it never lands a lane nobody picked)")
    why = machine_stop(p)  # T-4652: a full disk fails every gate: known in a second, before any merge or test
    if why:
        raise c.PolicyError(why)
    took = deferred = False
    try:
        with _train_run_lock(p):
            took = True
            for line in git_lock_lines(clear_git_locks(main)):  # T-4616: a stale lock would stop a merge or a builder's commit
                print(f"sampo: train: {line}", file=sys.stderr, flush=True)
            got = _run(p, main, ids, defer_verify, judged=dict(judged or {}))
            if defer_verify and not got["verify_owed"]:  # a red merge waiting for its revert is the verify's work too
                from sampo import lanes
                got["verify_owed"] = any(r["state"] == "red" for r in lanes.pm_records(p).values())
            deferred = defer_verify
            return got
    finally:  # T-4540: only a train that held the lock runs the pass: a refused one spawns nothing
        if took and not deferred:
            _after_train_run(p, main)


TRUNK_POLL_S = 30.0  # T-4540: a running gate checks trunk this often (a commit there ends the gate at once)


def _run(p, main, ids, defer_verify=False, judged=None):
    judged = judged or {}
    from sampo import cli
    checks = list(c.read_meta(p).get("checks") or [])
    if not checks:
        raise cli.UsageError("no checks configured: sampo check add '<cmd>'")
    branch = _git(main, "symbolic-ref", "--short", "-q", "HEAD").stdout.strip()
    base = _git(main, "rev-parse", "HEAD").stdout.strip()
    if not branch or not base:
        raise c.PolicyError(f"{main} is on no branch: check one out for the train to fast-forward")
    pm_wait(p, main)
    pre_tree = c.worktree_tree(main)
    left, notes = [], []  # notes: (id, what a probe saw that still let the lane on)
    kept_maps = {}  # T-4375: lane id: the docs/map files its merge resolved keeping both sides' lines
    cands, rows = [], []  # (brief, tip) in order; (brief, tip, why so far) in order
    from sampo import lanes, release
    try:  # T-4804: unreadable is not unset: the merge scan refuses each lane on it (held, nothing pushed); this only says so
        c.private_terms()
    except c.PrivateTermsUnreadable:
        print("sampo: train: " + c.PRIVATE_UNREADABLE + ": every lane is held by the private-term scan until it is repaired "
              "(sampo doctor)", file=sys.stderr, flush=True)
    else:  # T-4783: an unset list holds nothing: say so, never the path (T-4785: an empty one is said apart)
        release.say_no_list("train")
    for tid in ids:
        b = cli.need_brief(p, tid)
        br = b.meta.get("lane_branch") or f"foreman/{b.id}"
        if b.meta.get("lane_spike"):
            rows.append((b, None, "a spike is never merged"))
        elif not _ref_ok(main, br):
            rows.append((b, None, f"no branch {br}"))
        else:
            tip = _git(main, "rev-parse", "--verify", f"{br}^{{commit}}").stdout.strip()
            rows.append((b, tip, MOVED if b.id in judged and judged[b.id] != tip else
                         "" if judged.get(b.id) == tip else _held(p, b, main, tip)))

    # T-3770: when the gate is the suite the affected template runs (T-3544), it runs every lane's affected tests on the
    # merged tree, and a red one bisects on its red classes alone (T-3602): a probe per lane ran them all twice (the
    # 2026-11-07 trains: probes 55.8 and 71.8 min against gates of 14.9 and 10.1). The probes' test runs stay for a
    # gate that runs no affected tests, or with FOREMAN_TRAIN_PROBES=1.
    probe_tests = os.environ.get("FOREMAN_TRAIN_PROBES", "") not in ("", "0") or not cli.suite_gate(
        p.root, c.read_meta(p).get("affected") or "", checks)

    # T-3531: PROBE_JOBS lanes at once, each in its own scratch tree (one on a host over 2x its CPUs); results keep the
    # candidate order, so the left-out list and notes read as a serial run's. T-3119: each lane's check is its own
    # process (its own interpreter, no GIL shared), the lane's criteria and probe tests under it
    t0 = time.monotonic()  # T-3596: the phases' minutes, in the result and the report line
    outs = iter(_fan([_child_argv(p, main, base, b.id, tip, probe_tests, judged.get(b.id) == tip)
                      for b, tip, why in rows if not why], main,
                     1 if c.host_strain(2.0) else PROBE_JOBS))
    for b, tip, why in rows:
        seen = []
        if not why:
            why, seen = _child_result(next(outs))
            if why.startswith(WT_GONE):  # T-4728: a probe's tree lost twice: the train stops, no lane is left out for it
                raise c.PolicyError(why)
        notes += seen
        left.append((b.id, why)) if why else cands.append((b, tip))
    t1 = time.monotonic()
    if not cands:
        raise _none("nothing to merge: ", left)
    wt = c.scratch_tree("sampo-train-", main)  # outside the main checkout; git worktree add takes the empty dir (T-4735: in the scratch root)
    r = _git(main, "worktree", "add", "-q", "--detach", wt, base)
    if r.returncode:
        shutil.rmtree(wt, ignore_errors=True)  # T-4735: before the try below: nothing else would remove the tree and its lock
        c.scratch_release(wt)
        raise c.PolicyError(f"git worktree add failed: {r.stderr.strip()[:300]}")
    runs, probes, results, sub = [0], [0], [], None
    olds, head, lost = [wt], [base], [0]  # T-4728: the folders the train made; the commit the tree is at; losses so far
    tips ={b.id: t for b, t in cands}
    meta = c.read_meta(p)
    tpl, other = meta.get("affected") or "", tuple(meta.get("affected_by") or ())
    bare = lambda s: " ".join(s.split())  # noqa: E731
    # T-3602: the gate a run narrows to named tests: the affected template's runner with no names
    suite = next((g for g in checks if "{names}" in tpl and bare(g) == bare(tpl.replace("{names}", ""))), None)
    passed, diffs, reaches = {}, {}, {}  # (tree, command line): its passing row; lane id: its files; lane id: its tests
    dropped = {}  # T-4289: lane id: the red classes it was left out for (put back when the base proves red on them)
    base_green, base_bad, confirm_s = set(), set(), [0.0]  # T-4676: classes a run of their own proved green on the bare
    # base (T-4700: red, base_bad); confirm seconds

    def diff(i):
        """The files lane i's branch changed since it left the base."""
        if i not in diffs:
            diffs[i] = {f for f in _git(main, "diff", "--name-only", "--no-renames", "-z", f"{base}...{tips[i]}")
                        .stdout.split("\0") if f}
        return diffs[i]

    def reach(i):
        """The tests lane i's diff reaches, as the probes pick them (cli.affected on the scratch tree); none without
        an affected command or a map."""
        if i not in reaches:
            ensure_wt()  # T-4728: a tree removed since the last step is rebuilt, not read as "reaches nothing"
            try:
                ts = cli.affected(c.Project(p.slug, wt, p.dir, p.lane), since=base, only=sorted(diff(i)))[1]
            except Exception:
                return set()  # not cached: a later call, on a sound tree, may know
            reaches[i] = {t for t in ts if not t.endswith(other)}  # another runner's tests: its own gate
        return reaches[i]

    def form(cmd, names=None):
        """(the command line gate `cmd` runs in the scratch tree, the test modules it names or None). T-3602: with
        `names` the suite runs only those whose module is in the tree (None, []: none is), a class left out when its
        module runs whole; the suite otherwise runs the batch's affected tests found there (T-3544: a prefix or the
        base lacks a later lane's new test file); any other gate runs as configured. T-4889: a class name needs its
        class in that tree's module too (a later lane's new class in an old module is no test of the base)."""
        ensure_wt()  # T-4728
        if names and cmd == suite:
            have = {}
            for f in _git(wt, "ls-files", "-z").stdout.split("\0"):
                if f:
                    have.setdefault(_stem(f), []).append(f)
            tops = {}

            def defined(n):  # the class of mod.Class(.test) is in a file of that module's name, or that can't be told
                parts, m = n.split("."), _mod(n)
                k = parts.index(m) + 1 if m in parts else len(parts)
                if k >= len(parts):
                    return True
                for f in (f for f in have[m] if f.endswith(".py")):
                    if f not in tops:
                        tops[f] = _defines(os.path.join(wt, f))
                    if tops[f] is None or parts[k] in tops[f]:
                        return True
                return not any(f.endswith(".py") for f in have[m])
            ns = sorted(n for n in set(names) if _mod(n) in have and ("." not in n or _mod(n) not in names) and defined(n))
            return (tpl.replace("{names}", shlex.join(ns)), ns) if ns else (None, [])
        ts = [t for t in sub[2] if os.path.exists(os.path.join(wt, t))] if sub and cmd == sub[0] else []
        if ts:
            return sub[1].replace("{tests}", shlex.join(ts)).replace("{names}", shlex.join(map(_stem, ts))), \
                [_stem(t) for t in ts]
        return cmd, None

    def wt_gone():
        """T-4728: True when the scratch tree's folder is no longer a worktree (its .git file is gone with it)."""
        return not os.path.exists(os.path.join(wt, ".git"))

    def ensure_wt():
        """T-4728: the scratch tree is the train's own, yet something else removed it (2026-11-15: a probe then ran in a folder
        that was not there and the train stopped on "base probe inconclusive ... exit 127"). Said by what it is: the first loss
        rebuilds the tree once, in a new folder at the commit it had reached (every state of it is a commit, kept by the
        repo), and the train goes on; a second loss stops it, nothing landed. Only the train's own folders are ever removed."""
        nonlocal wt
        if not wt_gone():
            return
        lost[0] += 1
        c.log_event(p, "lane_train_wt_gone", data={"n": lost[0], "at": head[0][:9], **c.scratch_loss(wt)})  # T-4735
        if lost[0] > 1:
            raise c.PolicyError(f"{WT_GONE} again, at {head[0][:9]}: the second loss in this train, so it stops here with "
                                "nothing landed and no lane blamed; run it again")
        new = c.scratch_tree("sampo-train-", main)  # a fresh path: the dead registration is the finally's to prune
        olds.append(new)  # the finally removes each folder the train made, and only those
        r = _git(main, "worktree", "add", "-q", "--detach", new, head[0])
        if r.returncode:
            raise c.PolicyError(f"{WT_GONE}, and it could not be rebuilt at {head[0][:9]}: {r.stderr.strip()[:300]}")
        wt = new
        notes.append(("gate", f"{WT_GONE}; rebuilt it once at {head[0][:9]} and went on"))

    def here():
        ensure_wt()
        return _git(wt, "rev-parse", "HEAD").stdout.strip()

    def reset(sha):
        ensure_wt()
        _git(wt, "reset", "-q", "--hard", sha)
        head[0] = sha

    def run1(cmd, names=None, fresh=False):
        """(row, ran): gate `cmd` on the scratch tree as it stands, worded by form(); a passing run of the same line on
        this very tree is reused (ran False) unless `fresh` (T-4676: a base proven green earlier may be red now); row
        None when nothing named is left. `affected` lists the modules a
        narrowed run named (cli._cached_pass reads it); a red row keeps the names it fails, its output and last lines.
        T-4728: a run the tree was removed under is run again on the rebuilt one (a second loss stops the train)."""
        ensure_wt()
        line, mods = form(cmd, names)
        key = (here(), line)
        if line is None or (key in passed and not fresh):
            return passed.get(key), False
        t0 = time.monotonic()
        while True:
            ensure_wt()
            code, out = c.run_command(wt, line, c.load_scaled(1800), priority=True,  # T-3614, T-3757; T-4289: no test override
                                      env=c.scrubbed_env(cpu_env(p)), watch=trunk_watch, watch_every=TRUNK_POLL_S)
            if not wt_gone():
                break
        row = {"cmd": cmd, "exit": code, "s": round(time.monotonic() - t0, 1), **({"affected": mods} if mods else {})}
        if code:
            if _machine_only(out):  # T-4652: the disk, not the code: no base is called red and no lane blamed for it
                raise _machine_refusal(p, out)
            row.update(failing=_failed(out), cut=sorted(_cut(out)), tail=out.rstrip().splitlines()[-15:], out=out)
        else:
            passed[key] = row
        return row, True

    def build(start, lanes):
        """Reset the scratch tree to `start`, merge `lanes`; [(brief, sha after)] of those that merged."""
        reset(start)
        out = []
        for b, tip in lanes:
            n = len(left)
            ensure_wt()
            sha = _merge_in(wt, b, tip, left, kept_maps)
            if not sha and wt_gone():  # T-4728: the tree went under the merge: that is no conflict, and no lane is left out for it
                del left[n:]
                ensure_wt()  # a second loss stops the train here
                sha = _merge_in(wt, b, tip, left, kept_maps)
                if not sha and wt_gone():
                    ensure_wt()
            if sha:
                out.append((b, sha))
                head[0] = sha
        return out

    def moved():
        return _git(main, "rev-parse", "HEAD").stdout.strip() != base or \
            _git(main, "symbolic-ref", "--short", "-q", "HEAD").stdout.strip() != branch

    def moved_why(when):
        """The refusal for a trunk that moved, naming the commit that moved it (T-4540: a stop is traced at once)."""
        tip = _git(main, "rev-parse", "HEAD").stdout.strip()
        if tip == base:
            return f"{branch} changed {when} (HEAD is no longer on it): nothing landed; run it again"
        by = c.fit(_git(main, "log", "-1", "--format=%s", tip).stdout.strip(), 80)
        return f"{branch} moved {when} (was {base[:9]}; now {tip[:9]} {by}): nothing landed; run it again"

    def trunk_watch():
        """T-4540: a gate's result is thrown away once trunk moved, so the gate ends at its next TRUNK_POLL_S check."""
        if moved():
            raise c.PolicyError(moved_why("while its gate ran"))

    def gate():
        """True when every gate passes in the scratch tree as it stands, each as form() words it."""
        c.wait_for_load("sampo lane train")
        runs[0] += 1
        results.clear()
        for cmd in checks:
            results.append(run1(cmd)[0])
            if results[-1]["exit"]:
                return False
        return True

    def probe(sha, cmd, names, fresh=False):
        """T-3602: the red gate alone on the tree at `sha`, the suite on only its red names: its row (None: nothing
        named is in that tree, so nothing there can fail). `fresh`: never a held pass."""
        reset(sha)
        row, ran = run1(cmd, names, fresh)
        probes[0] += ran
        return row

    def bisect(cmd, names, check=True):
        """T-3602: the lane (index + 1) that turns gate `cmd` red, by prefix: lo passes (0 = the base), hi fails. Each
        probe runs only that gate, the suite only on `names` (proven to fail alone on the whole set) or, with none, as
        the train runs it: seconds or the suite's part, where a whole gate run took minutes."""
        lo, hi = 0, len(merged)
        while hi - lo > 1:
            mid = (lo + hi) // 2
            row = probe(merged[mid - 1][1], cmd, names)
            lo, hi = (lo, mid) if row and row["exit"] else (mid, hi)
        if hi == 1 and check:  # the first lane is blamed on the strength of a base nobody probed: probe it before blaming
            base_check(cmd, names)
        return hi

    def base_check(cmd, names, fresh=False):
        """T-4289: the base probed in gate `cmd`, on `names` alone (all of it with none) in this very environment, before
        a lane is blamed on what a base may fail too. Red in them: base_red's refusal; a probe that says nothing (a timeout,
        a crash) is no proof either way: the train stops and drops nothing on its account. The scratch tree is put back.
        T-4676 `fresh`: a run of its own, never a pass the train held for this base."""
        row, bad = base_split(cmd, names, fresh)
        if row and row["exit"] and (not names or bad):
            raise base_red(row, names)

    def base_split(cmd, names, fresh=False):
        """T-4700: (the base's row for gate `cmd` on `names` alone, the names it is red in): base_check's probe, the
        verdict left to the caller. A row red in none of `names` says nothing of them; an inconclusive one stops the train."""
        was = here()
        row = probe(base, cmd, names, fresh)
        reset(was)
        if not row or not row["exit"]:
            return row, []
        if _inconclusive(row, names):
            c.log_event(p, "lane_train_base_inconclusive", data={"base": base[:9], "exit": row["exit"], "names": list(names)})
            raise c.PolicyError(f"base probe inconclusive: the base {base[:9]} of {branch} ended {row['exit']} with no failing "
                                f"class named, so it is not proven red or green ({c.joinmore(list(names), 4) or 'the gate'}); "
                                f"no lane dropped on its account: {_why_red(row)}")
        return row, [n for n in names if any(_same(f, n) for f in row["failing"])]

    def base_red(row, names):
        """T-4289: the refusal for a base found red: the classes of `names` red on it (the whole row's when none is
        named), and every lane already left out for one of them put back (a log line on its brief and the event), as the
        class was never theirs. No lane is dropped for it and the train stops: nothing can be blamed on a red base."""
        bad = [n for n in names if any(_same(f, n) for f in row.get("failing") or ())] or list(row.get("failing") or ())
        back = []
        for i, why in list(dropped.items()):
            if bad and any(_same(n, k) for n in bad for k in why):
                back.append(i)
                left[:] = [(j, w) for j, w in left if j != i]
                cli.mutate(p, i, lambda x: x.append_log(f"put back: {c.joinmore(bad, 4)} is red on the base "
                                                        f"{base[:9]}, not this lane's"), "lane_train_back", {"classes": bad})
        c.log_event(p, "lane_train_base_red", data={"base": base[:9], "classes": bad, "restored": back})
        note = "no lane dropped for it" + (f", {', '.join(back)} put back" if back else "")
        return c.PolicyError(f"the base {base[:9]} of {branch} is red; fix it before a train can blame a lane: red on "
                             f"the base ({note}): {_why_red(row)}")

    def confirm(need):
        """T-3602: True when the gates in `need` pass on the tree as it stands: the suite on only its names (the red
        ones, and what the left-out lanes' diffs reach: a lane may have needed them), every other gate whole (a lane
        left out can turn any of them red). A probe's pass on this very tree counts. T-4676: its minutes are the
        result's `confirm`."""
        c.wait_for_load("sampo lane train")
        results.clear()
        ran, t0c = False, time.monotonic()
        for cmd in (g for g in checks if g in need):
            row, fresh = run1(cmd, need[cmd])
            if row is None:  # nothing it named is left in the tree: the gate as the train runs it
                row, fresh = run1(cmd)
            ran |= fresh
            results.append(row)
            if row["exit"]:
                break
        runs[0] += ran
        confirm_s[0] += time.monotonic() - t0c
        return not results[-1]["exit"]

    try:
        merged = build(base, cands)
        ensure_wt()
        sub = _affected_gate(p, wt, base, checks)
        if moved():  # a lane merged meanwhile: don't spend a gate run on a base that is gone
            raise c.PolicyError(moved_why("since the train started"))
        why = _base_red(p, main, base, sub) if sub and merged else ""
        if why:  # T-3625: the base's affected tests first: a red base is known in minutes
            raise c.PolicyError(f"the base {base[:9]} of {branch} is red; fix it before a train can blame a lane: red on "
                                f"the base (no lane dropped for it): {why}")
        ok = not merged or gate()
        first = list(results)
        last = {r["cmd"]: r for r in results if not r["exit"]}  # each gate's newest passing row
        # T-3602: what each confirm reruns: every gate but the suite whole; the suite whole only if the red cut it off
        need = {g: None for g in checks if g != suite or g in checks[len(results):]}
        # T-4289: flaky classes (red in the gate, green alone on the merged tree) are never taken on that word: a lane's
        # shared state looks the same. pending: (classes, gate, tree, output) cleared so far; a train whose reds were
        # all cleared that way runs one narrowed confirm (T-4676: their modules and the modules map-linked to them, never
        # a second whole gate) on the merged tree, uncached, before it lands, and records a flake only when a confirm is
        # green. notflaky: classes red again in the confirm right after, judged as a lane's as before. cleared: classes
        # green in a confirm that was red for another: red and green alone again, they are the same class twice, and the
        # cap (the only way a flaky class stops a train) is reached.
        flaked, said, pending, notflaky, cleared = 0, set(), [], set(), set()
        cutoffs = 0  # T-4889: suite gates cut at their deadline with nothing failed, each run once more
        while not ok or pending:  # T-3602: blame from the red run itself; bisect only what it can't pin on one lane
            if ok:
                passed.clear()
                reset(merged[-1][1])
                if need.get(suite, ()) is None:  # the whole suite is owed: no narrowing it
                    t0c = time.monotonic()
                    ok = gate()
                    confirm_s[0] += time.monotonic() - t0c
                else:
                    mods = {_mod(n) for fl, *_ in pending for n in fl}
                    nd = dict(need)
                    ensure_wt()  # T-4728
                    need[suite] = nd[suite] = (need.get(suite) or set()) | set(_neighbours(wt, sorted(mods)))
                    ok = confirm(nd)
                last.update((r["cmd"], r) for r in results if not r["exit"])
                if ok:
                    for fl, fg, ftree, fout in pending:
                        if new := [n for n in fl if n not in said and n not in notflaky]:
                            said.update(new)
                            ids = _note_flaky(p, fg, ftree, fout, new)
                            notes.append(("gate", f"flaky under load: {c.joinmore(new, 6)} (red in the gate, green alone on "
                                                  "the merged tree and green in a narrowed confirm of their modules and "
                                                  "the modules linked to them; "
                                                  + (f"recorded in flakes.json: {c.joinmore(ids, 4)}" if ids else
                                                     "nothing recorded: the failure named no test") + ", no lane blamed)"))
                            c.log_event(p, "lane_train_flaky", data={"classes": new, "lanes": [b.id for b, _ in merged],
                                                                      "cmd": fg[:200]})
                    pending = []
                    break
            if pending and results[-1]["cmd"] == suite and results[-1].get("failing"):
                now = results[-1]["failing"]  # the run after the pending classes were cleared: who is red again
                for n in (n for fl, *_ in pending for n in fl):
                    if not any(_same(f, n) for f in now):
                        cleared.add(n)
                    elif n not in cleared:
                        notflaky.add(n)
                # judged as a lane's now: nothing left to confirm for them
                pending = [([n for n in fl if n not in notflaky], *more) for fl, *more in pending
                           if any(n not in notflaky for n in fl)]
            red, g = results[-1], results[-1]["cmd"]
            cut = set(red.get("cut") or ()) if g == suite else set()  # T-4889: run1 keeps them out of `failing`
            owed = bool(cut) or (g == suite and red["exit"] == 124)  # the deadline cut it: what never ran failed nothing
            if owed:
                need[suite] = None  # the cut classes run in a whole gate before anything lands
                if _nothing_failed(red):  # no red, so nothing to probe on the base or blame on a lane
                    if cutoffs >= CUT_RERUNS:
                        c.log_event(p, "lane_train_cut_off", data={"cut": len(cut), "runs": cutoffs + 1, "cmd": g[:200]})
                        raise c.PolicyError(f"the gate was cut off at its deadline {cutoffs + 1}x ({len(cut)} classes not "
                                            "run, none failed): the box is too loaded for it; nothing landed and no lane "
                                            "blamed; run the train again")
                    cutoffs += 1
                    notes.append(("gate", f"cut off at its deadline ({len(cut)} classes not run, none failed): ran it "
                                          "once more, no lane blamed"))
                    passed.clear()
                    reset(merged[-1][1])
                    ok = gate()
                    last.update((r["cmd"], r) for r in results if not r["exit"])
                    continue
            names, flaky, ftree = (red["failing"] if g == suite else []), [], ""
            if names:  # a narrowed run sees them only if they fail alone (not by order or a flake): on this tree first
                row = probe(here(), g, names)
                ftree = _git(wt, "rev-parse", "HEAD^{tree}").stdout.strip()
                split = _alone(row, names, red["out"])
                names, flaky = split or ([], [])
                names += [n for n in flaky if n in notflaky]  # red again in the rerun: no flake, judged as a lane's
                flaky = [n for n in flaky if n not in notflaky]
            if flaky:
                again = [n for n in flaky if n in cleared]  # T-4676: the same class twice: red, green alone, green, red again
                if again or flaked >= FLAKY_ROUNDS:  # nothing is learnt from yet another run: stop, drop no lane for them
                    flaky = again or flaky
                    c.log_event(p, "lane_train_flaky_cap", data={"classes": flaky, "rounds": flaked, "alone": names,
                                                                  "cmd": g[:200]})
                    raise c.PolicyError(f"flaky, cap reached: {c.joinmore(flaky, 6)} red in the gate and green alone on the "
                                        f"merged tree, round {flaked + 1} of them"
                                        + (", red again after a confirm that was green" if again else "") +
                                        "; nothing landed and no lane dropped for them"
                                        + (f" (red alone too: {c.joinmore(names, 4)})" if names else "") +
                                        "; the box is too loaded for this gate: run the train again")
                flaked += 1
                pending.append((flaky, g, ftree, red["out"]))
                if not names:  # nothing else is red: on to the whole gate again, before anything lands
                    ok = True
                    continue
            # T-4676/T-4700: before a lane is blamed or dropped for these classes they run once, uncached, on the bare base in
            # this very environment: a class red there is the base's or the machine's (a test that turned red with time), never
            # a lane's. All of them red, or one a lane was already left out for, or none narrowed: the train stops and drops
            # no lane (the lanes put back). Some of them: they leave the blame (named in the result, one FIX capture) and the
            # lanes are judged on the rest. A class a run proved either way is not run again in this train.
            cls, shown = list(names or (red["failing"] if g == suite else [])), red
            brow, based = None, []
            if todo := [n for n in cls if n not in base_bad and (n not in base_green or
                                                                  any(_same(n, y) for s in dropped.values() for y in s))]:
                brow, based = base_split(g, todo, fresh=True)
                base_green.update(n for n in todo if n not in based)
                base_bad.update(based)
            if bad := [n for n in cls if n in base_bad]:
                have = list((brow or {}).get("failing") or [])
                row = dict(brow or {"cmd": g, "tail": []},
                           failing=have + [n for n in bad if not any(_same(f, n) for f in have)])
                rest = [n for n in names if n not in bad]
                if not rest or any(_same(n, y) for s in dropped.values() for y in s for n in bad):
                    _base_red_capture(p, bad, base)
                    raise base_red(row, bad)
                names, cls = rest, [n for n in cls if n in rest]
                shown = dict(red, failing=[f for f in red["failing"] if not any(_same(f, n) for n in bad)])
                if based:  # said once: the memo holds them red for the rest of the train
                    made = _base_red_capture(p, based, base)
                    notes.append(("gate", f"red on the bare base too, so not any lane's: {c.joinmore(based, 6)}"
                                          + (f" (captured {made})" if made else "")))
                    c.log_event(p, "lane_train_base_red_kept", data={"base": base[:9], "classes": based, "lanes_judged_on": rest})
            need[g] = (need.get(g) or set()) | set(names) | set(flaky) if names and not owed else None  # the suite's other tests passed here (T-4889: not the cut ones)
            ids = [b.id for b, _ in merged]
            blamed = _blame(red["out"], names or red["failing"], ids, diff, reach)
            if not blamed:
                n, at = probes[0], bisect(g, names, not cls)
                only = "only " + c.joinmore(names, 4) if names else "the whole red gate"
                blamed = {ids[at - 1]: (f"bisection, {probes[0] - n} probe(s) of {only}", "")}
            texts = _texts(red["out"], red.get("failing") or [])
            for i in (i for i in ids if i in blamed):
                rule, k = blamed[i]
                why = "its merge turns the gates red: " + _why_red(shown, rule, texts[k].rstrip().splitlines()[-15:]
                                                                   if k else None)
                left.append((i, why))
                dropped[i] = _left_for(k, names)  # T-4289: what it was left out for (put back if the base is red on it)
                cli.mutate(p, i, lambda x, w=why: x.append_log("left out of a merge train: " + w), "lane_train_red",
                           {"cmd": red["cmd"], "failing": red.get("failing") or [], "blamed": rule})
                gone = {_stem(t) for t in reach(i)} if suite else set()  # before the rebuild: its links need its files
                if gone and need.get(suite, ()) is not None:  # a lane may have needed it: what its diff reaches reruns
                    need[suite] = (need.get(suite) or set()) | gone
            at = next(n for n, i in enumerate(ids) if i in blamed)
            rest = [(b, tips[b.id]) for b, _ in merged[at:] if b.id not in blamed]
            merged = merged[:at] + build(merged[at - 1][1] if at else base, rest)
            if not merged:
                break
            ok = confirm(need)
            last.update((r["cmd"], r) for r in results if not r["exit"])
        top = next((r for r in first if r["cmd"] == suite and r.get("affected")), None)
        if merged and top and suite in need:  # what the train landed on: the batch's tests with every lane in, then the
            # red ones and the left-out lanes' reach here; a close reuses it for those (cli._cached_pass)
            last[suite] = dict(last[suite], affected=sorted(set(top["affected"]) | set(last[suite].get("affected") or ())))
        green = [last[g] for g in checks] if merged else []
        if not merged:
            raise _none("nothing landed: ", left)
        _mark_green(p)  # T-3625: this env's gate went green
        t2 = time.monotonic()
        tip = here()  # T-4728: a tree lost since the last gate is rebuilt, never read as an empty tip
        if moved():
            raise c.PolicyError(moved_why("while the train ran"))
        ids_in = [b.id for b, _ in merged]
        for b, _ in merged:  # marked before the fast-forward, so a crash can't leave landed lanes unmarked
            def done(x, b=b):
                for k in ("base", "base_tree", "paused_tree"):
                    x.meta.pop(k, None)
                x.meta.update({"base": base}, **({"base_tree": pre_tree} if pre_tree else {}))
                x.append_log(f"merged {b.meta.get('lane_branch') or 'foreman/' + b.id} in merge train")
                x.meta.update(lane_stop=f"merged {c.now()}")
            cli.mutate(p, b.id, done, "lane_merge", {"train": ids_in})
        ff, kept = _fast_forward(main, tip)
        if kept:
            notes.append((branch, kept))
        if ff.returncode:
            for b, _ in merged:
                cli.mutate(p, b.id, lambda x: x.meta.update(lane_stop=f"refused {c.now()}: the train's fast-forward failed"),
                           "lane_merge", {"train": ids_in, "undone": True})
            raise c.PolicyError(f"fast-forward of {branch} to {tip[:9]} refused: {(ff.stderr or ff.stdout).strip()[:300]}")
        lanes.log_landed(p, ",".join(ids_in), base, tip)  # T-4805: only now it landed; ship and release read it to skip these
        maps =[(i, kept_maps[i]) for i in ids_in if i in kept_maps]  # T-4375: only the lanes that landed this way
        map_task = _map_follow_up(p, maps) if maps else ""
        part = next((r["affected"] for r in green if r.get("affected")), None)
        if part:  # T-3544: the full suite the gate skipped verifies each landed merge in the background, newest first
            for i, (b, sha) in enumerate(merged):  # T-4718: a caller that ships next starts the verify after the ship asked
                pm_record(p, sha, b.id, main, spawn=not defer_verify and i == len(merged) - 1)
        tree = _git(main, "rev-parse", f"{tip}^{{tree}}").stdout.strip()
        ct = c.code_tree(main, tree)
        c.log_event(p, "check_run", data={"tree": tree[:12], "code_tree": ct, "env": c.env_id(), "train": ids_in,
                                          "results": green})
        t3 = time.monotonic()
        mins = {"probe": round((t1 - t0) / 60, 1), "gate": round(max(0.0, t2 - t1 - confirm_s[0]) / 60, 1),
                "confirm": round(confirm_s[0] / 60, 1), "land": round((t3 - t2) / 60, 1)}  # T-4676: gate + confirm = the gates
        return {"merged": ids_in, "left_out": left, "notes": notes, "kept": kept, "map_merged": maps,
                "map_follow_up": map_task, "gate_runs": runs[0], "verify_owed": bool(part and defer_verify),
                "probes": probes[0], "tip": tip, "branch": branch, "affected": part, "minutes": mins, "probes_skipped": sum(
                    w.startswith(SKIPPED) for _, w in notes), "lanes_probed": len(rows)}
    finally:
        for folder in olds:  # T-4728: each one the train made (the first, and one rebuilt after a loss), nothing else
            _git(main, "worktree", "remove", "--force", folder)
            shutil.rmtree(folder, ignore_errors=True)
            c.scratch_release(folder)
        _git(main, "worktree", "prune")


def _map_follow_up(p, maps):
    """T-4375: one captured follow-up (source followup) for a train that merged lanes keeping both sides' lines in docs/map
    entries, naming each lane and file: a line both sides rewrote is kept twice, so the stale one must go. One ledger
    event says what was merged. Returns the new task's id, '' when the capture failed (the landing never waits on it)."""
    from sampo import cli
    what = "; ".join(f"{i}: {', '.join(fs)}" for i, fs in maps)
    text = (f"Read the docs/map entries the train merged keeping both sides' lines ({what})\n\nThe lane train merged "
            "these lanes with `git merge-file --union` because they clashed only in docs/map entries. A line both sides "
            "rewrote is kept twice: read each merged entry and drop the stale line.")
    made = ""
    try:
        with c.lock(p.dir, timeout=5):
            n = cli._create(p, cli._title(text), "CLEAN", "S", "captured", raw=text, source="followup", net=False)
            c.log_event(p, "capture", task=n.id, data={"source": "followup", "type": "CLEAN", "from": "map_union"})
            c.regen_views(p)
            made = n.id
    except Exception:  # noqa: BLE001 — a busy lock or a full disk costs the note, never the landed lanes
        pass
    c.log_event(p, "map_union", data={"lanes": {i: fs for i, fs in maps}, "followup": made})
    return made


def _base_red_capture(p, classes, base):
    """T-4700: one captured FIX (source discovered) for test classes red on the bare base a train found at the moment a
    lane would be left out for them (a test that turned red with time: outputs ageing, a date rolling over), unless one
    with that title is open. Returns the task's id, '' when none was made (the train never waits on it)."""
    from sampo import cli
    title = f"Base test red on its own: {c.joinmore(sorted(classes), 4)} (red on the bare base)"
    text = (f"{title}\n\nA lane train ran {c.joinmore(sorted(classes), 8)} alone on the bare base {base[:9]} when a lane was to "
            "be left out for it, and it failed there: no lane broke it, so none was left out for it. Find why the base "
            "turns red with time (the machine's old outputs, a date) and make the test hold on any day.")
    try:
        with c.lock(p.dir, timeout=5):
            if any(b.title == title for b in c.load_briefs(p) if b.status not in c.CLOSED):
                return ""
            n = cli._create(p, title, "FIX", "S", "captured", raw=text, source="discovered", net=False)
            c.log_event(p, "capture", task=n.id, data={"source": "discovered", "type": "FIX", "from": "train_base_red"})
            c.regen_views(p)
            return n.id
    except Exception:  # noqa: BLE001 — a busy lock or a full disk costs the capture, never the train's verdict
        return ""


def _fast_forward(main, tip):
    """T-4007: (git merge --ff-only tip in the main checkout, a note or ''). git refuses to overwrite an uncommitted
    change to a file the train merged, and that threw a green train away; so those files are stashed first and put back
    after. One that no longer applies stays a named stash with the landed files in place: never conflict markers in the
    checkout the Ward runs from (the JARVIS box's outage, 2026-11-08)."""
    st = _git(main, "status", "--porcelain", "-z", "-uall", "--no-renames").stdout
    merged = set(_git(main, "diff", "--name-only", "-z", "HEAD", tip).stdout.split("\0"))
    dirty = sorted({e[3:] for e in st.split("\0") if len(e) > 3} & merged)
    tag = f"sampo train {time.strftime('%Y-%m-%d %H:%M:%S')} ({os.getpid()}): local changes to files it merged"
    if not dirty or _git(main, "stash", "push", "-q", "-u", "-m", tag, "--", *dirty).returncode:
        return _git(main, "merge", "-q", "--ff-only", tip), ""  # nothing to set aside, or git's own refusal stands
    ff = _git(main, "merge", "-q", "--ff-only", tip)
    # every worktree and session shares the stash stack: ours by its tag, never whatever stash@{0} is by now
    ours = lambda: next((r for r, _, s in (x.partition(" ") for x in _git(  # noqa: E731
        main, "stash", "list", "--format=%gd %gs").stdout.splitlines()) if s.endswith(tag)), "")
    if (ref := ours()) and not _git(main, "stash", "apply", "-q", ref).returncode:
        _git(main, "stash", "drop", "-q", ours() or ref)
        return ff, f"set aside and put back local changes to {c.joinmore(dirty, 4, ', ')}"
    known = set(_git(main, "ls-tree", "-r", "-z", "--name-only", "HEAD", "--", *dirty).stdout.split("\0"))
    _git(main, "reset", "-q", "--", *dirty)  # the apply's unmerged entries
    if known & set(dirty):
        _git(main, "checkout", "HEAD", "--", *sorted(known & set(dirty)))
    for f in set(dirty) - known:  # gone at the tip: the stash holds its local version
        with contextlib.suppress(OSError):
            os.remove(os.path.join(main, f))
    return ff, (f"local changes to {c.joinmore(dirty, 4, ', ')} no longer apply over the train: kept as stash "
                f"'{tag}' (git stash list)")


def _ids(args):
    """The ids a train was named: the first positional and every one after it (T-4833: `train T-1 T-2` read only T-1
    and landed it alone, naming no other), each split on commas too (T-1,T-2)."""
    words = [args.id or ""] + list(getattr(args, "more", None) or [])
    return [x for w in words for x in w.replace(",", " ").split() if x]


def cmd(p, args, main, close=False):
    """sampo lane train IDS; `close` (T-4596: the multi-id sampo lane merge) also closes each task it landed, through
    close_landed: the train's gate pass serves every close, so the merge costs no gate run beyond the train's."""
    ids = _ids(args)
    owed = [False]
    try:  # T-4718: the verify starts after the ship below (or at the end); a train that raises leaves records: it starts too
        got = train(p, main, ids, defer_verify=True)
        owed[0] = bool(got.get("verify_owed"))
        return _cmd_report(p, args, main, close, got, owed)
    finally:
        start_verify(p, main, owed)


def _cmd_report(p, args, main, close, got, owed):
    from sampo import cli
    lines = [f"Train: {len(got['merged'])} lane(s) landed on {got['branch']} ({got['tip'][:9]}) in "
             f"{got['gate_runs']} gate run(s)" + (f" and {got['probes']} probe(s) of only the failing tests"
                                                  if got["probes"] else "") + f": {', '.join(got['merged'])}"]
    lines += [f"  left out {i}: {w}" for i, w in got["left_out"]] + [f"  note {i}: {w}" for i, w in got["notes"]]
    lines += [f"  merged {i} keeping both sides' lines in {', '.join(fs)} (clashed only in docs/map)"
              for i, fs in got.get("map_merged") or []]  # T-4375
    if got.get("map_follow_up"):
        lines.append(f"  follow-up {got['map_follow_up']}: read the merged map entries (a line both sides rewrote is "
                     "kept twice)")
    m = got["minutes"]  # T-3596: where the time went; the serial closes after the train are not in it
    lines.append(f"Minutes: probe {m['probe']}, gate {m['gate']}, confirm {m.get('confirm', 0.0)}, land {m['land']}; "
                 f"{got['probes_skipped']} of {got['lanes_probed']} probe(s) skipped (a held green run)")
    if got["affected"]:
        lines.append(f"The test gate ran {len(got['affected'])} affected test module(s), not the full suite "
                     "(FOREMAN_FULL_GATE=1 runs it); the full suite verifies the landed merges in the background.")
    lines.append("sampo check now reuses this pass while only CHANGELOG.md and the sync mirror change.")
    if close and got["merged"]:  # T-4596: what it landed closes through the same close a land makes
        try:
            with c.lock(os.path.join(p.dir, "land"), timeout=1):  # a running land closes them itself
                done, left = close_landed(p, main, got["merged"], who="sampo lane merge")
        except c.LockTimeout:
            done, left = [], []
            lines.append("A sampo lane land is running: it closes the lanes this merge landed.")
        got["closed"], got["hand"] = [t for t, _ in done], left
        lines += [f"  closed {t}: {c.fit(c.plain(x), 160)}" for t, x in done]
        lines += [f"  hand close {t}: {w} (then sampo focus {t}; sampo task finish {t})" for t, w in left]
    try:
        sh = _ship(p, False, main) if got["merged"] else None  # T-3849: a train by hand ships too, on a grant already given
    finally:
        start_verify(p, main, owed)  # T-4718: in line, so the ship has ended: the verify's suite runs take the slots after
    if sh:
        got["ship"] = sh[1]
        lines.append(f"Ship ({sh[0]}'s grant): {c.fit(c.plain(sh[1]), 300)}")
    return cli.out(args, got, "\n".join(lines))


# ---------------------------------------------------------------- sampo lane land (T-3500)

LAND_AUDIT = ("sampo lane land: the review recorded on the brief before the merge, re-recorded at the close; the train "
              "merged the lane's branch as reviewed, re-ran its criteria in its worktree and passed the gates once on the "
              "merged tree")


def landing(p):
    """T-3500: True while a sampo lane land holds its lock (the drive then names no merge)."""
    try:
        with c.lock(os.path.join(p.dir, "land"), timeout=0):
            return False
    except c.LockTimeout:
        return True


def train_live(p):
    """T-4692: True while a train of this project runs: it holds the train's own lock (a whole run, the kernel drops it with
    the process, so a dead train leaves nothing behind) and the holder is not a hand close's trunk commit (the close's
    marker names a live pid: _closing). Read from the lock, never from a process name."""
    try:
        with train_lock(p, timeout=0):
            return False
    except c.LockTimeout:
        return not _closing(p)
    except OSError:
        return False


def held_by_active(p, main, ids=None, briefs=None):
    """T-4692: why a task active in the main checkout holds the land, or '' when it does not. The land closes each lane's
    task by its id, so an unrelated focus holds nothing; what it holds is uncommitted work in the files the land writes
    there: a lane's changed files (the fast-forward would overwrite them) and the changelog every close adds a line to.
    Those are named. The focused task being one of the lanes itself holds too (its close is its own). `ids` are the
    lanes to land (default: the finished ones waiting on a merge). 2026-11-14 (T-4681): a focus made during a land left
    two lanes for hand closes; the blanket hold had made every focus a lost lane or a refusal."""
    from sampo import lanes
    busy = c.active_brief_fast(p)
    if not busy:
        return ""
    head = f"{busy.id} is active in the main checkout"
    tail = f"finish {busy.id} (or sampo task set {busy.id} status=planned), then land"
    try:
        briefs = briefs if briefs is not None else c.load_briefs(p)
        ids = list(ids) if ids is not None else lanes.awaiting_merge(p, briefs)
        if busy.id in ids:
            return f"{head} and is one of the lanes to land: {tail}"
        by = {b.id: b for b in briefs}
        watch = {"CHANGELOG.md"}
        for i in ids:
            b = by.get(i)
            tip = (b.meta.get("lane_branch") or f"foreman/{i}") if b else ""
            if tip:
                watch |= {f for f in _git(main, "diff", "--name-only", "-z", f"HEAD...{tip}").stdout.split("\0") if f}
        st = _git(main, "status", "--porcelain", "-z", "-uall", "--no-renames").stdout
        hit = sorted({e[3:] for e in st.split("\0") if len(e) > 3} & watch)
    except Exception:  # noqa: BLE001 — what can't be read can't be shown clear: the old hold stands
        return f"{head}, where sampo lane land closes each lane's task: {tail}"
    return (f"{head} with uncommitted changes to {c.joinmore(hit, 4, ', ')}, which the land writes: {tail}"
            if hit else "")


def _sampo(argv, env=None):
    """(exit code, stdout, stderr) of one sampo verb, the checks its command line runs, in a fresh process: a land
    outlives the trains it merges, so its own code would judge their verbs and docs as unknown (T-3970)."""
    r = subprocess.run([sys.executable, os.path.join(c.PLUGIN_ROOT, "bin", "sampo"), *argv], capture_output=True,
                       text=True, env=env)
    return r.returncode, r.stdout, r.stderr


SHIPPED_AT = re.compile(r"\bmain at ([0-9a-f]{40})\b")  # release.ship's closing line: the commit it pushed


def _grant_of(p):
    """The task holding a publish grant, or None."""
    return next((b for b in c.load_briefs(p) if "publish" in (b.meta.get("allow") or [])), None)


def _give_back(p, tid):
    """The grant a ship spent, held again for the land's next ship (a Log line says so); True when it was re-added."""
    with c.lock(p.dir):
        b = c.find_brief(p, tid)
        if b is None or "publish" in (b.meta.get("allow") or []):
            return False
        b.meta["allow"] = [*(b.meta.get("allow") or []), "publish"]
        b.append_log("publish grant kept for the land's next train (spent at its last ship)")
        c.save_brief(p, b)
    return True


def _ship_end(p, tid, head, rc, out, t0, t1):
    """T-4239: the record of one finished ship, as a ledger event (ship_done: head it read, sha it pushed, seconds, exit,
    the failing tests a red run names from its own output) and the line a land or sampo status tells. (line, data)."""
    sha = (SHIPPED_AT.findall(out) or [head])[-1]
    from sampo import release
    red = _failed(out) if rc and release.SLOTS_CUT not in out else []  # T-4718: a wait for slots names no failing test; T-4889: nor a cut
    lines = out.splitlines()
    mine = [i for i, ln in enumerate(lines) if ln.startswith(("fm: ", "sampo: "))]  # T-4739: the refusal, not the run's start
    why = _why("\n".join(lines[mine[-1]:]) if mine else out)
    line = ((out.strip().splitlines() or ["shipped"])[-1] if not rc else
            "refused: " + why + (f"; failing {c.joinmore(red, 8)}" if red and not any(k in why for k in red) else ""))
    data = {"head": head, "sha": sha, "seconds": round(t1 - t0, 1), "exit": rc, "start": round(t0, 1),
            "end": round(t1, 1), "red": red[:20]}
    try:
        c.log_event(p, "ship_done", task=tid, data=data)
        c.write_atomic(os.path.join(p.dir, "ship-last.json"), json.dumps({**data, "grant": tid, "seen": False,
                                                                           "line": c.fit(c.plain(line), 400)}))
    except Exception:  # noqa: BLE001 — a record that can't be written never undoes a ship
        pass
    return line, data


def _ship_log(p, text):
    """T-4739: `text` appended to the project's ship.log, where the detached ship writes its own output: a ship run in line
    (a train by hand, a land where nothing may run in the background) leaves the same block, so the newest ship is always
    the log's last. A log that can't be written never undoes a ship."""
    try:
        with open(os.path.join(p.dir, "ship.log"), "a", encoding="utf-8", errors="replace") as f:
            f.write(text)
    except OSError:
        pass


def _ship(p, keep, main=None):
    """T-3849: sampo ship on a publish grant someone already gave (never granted here): (grant id, one line) or None.
    keep=True (between a land's trains) gives a spent grant back with a Log line so the next train ships too; keep=False
    (the land's or a train's last ship) leaves it spent, spending it when nothing was left to push. A refusal is a
    line, never an error: it never stops the land. T-4239: in line, for a hand train and where nothing may run in the
    background; a land starts _ship_request's detached ship instead. `main`: the head is read once and the ship
    tests and pushes that snapshot."""
    from sampo import release
    grant = _grant_of(p)
    if not grant:
        return None
    running = _ship_alive(p)
    if running:  # review: never beside a detached ship (two pushes of one grant, a spent grant given back): join it, bounded
        secs = float(os.environ.get("FOREMAN_LAND_SHIP_WAIT_S") or 1800)
        print(f"sampo lane land: a ship is running (pid {running['pid']}); waiting up to {secs:g}s for it", file=sys.stderr,
              flush=True)
        if not _ship_wait(p, secs):
            return grant.id, f"a ship is already running (pid {running['pid']}): not shipping beside it"
        grant = _grant_of(p)  # the ship that ended may have spent it
        if not grant:
            return None
    print(f"sampo lane land: shipping on {grant.id}'s publish grant", file=sys.stderr, flush=True)
    head, t0 = (c._git(main, "rev-parse", "HEAD", timeout=10).strip() if main else ""), time.time()
    _ship_log(p, f"{_hms()} ship of {head[:9]} on {grant.id}'s publish grant (in line, from the land)\n")  # T-4739
    try:
        rc, out, err = _sampo(["ship", "--task", grant.id],  # T-4289: its gate's tests never take a test override
                              env=c.scrubbed_env(dict(os.environ, FOREMAN_SHIP_AT=head)) if head else None)
    except Exception as e:  # noqa: BLE001 — a failed ship is said, never stops the land
        rc, out, err = 1, "", f"{type(e).__name__}: {e}"
    line = (out.strip().splitlines() or ["shipped"])[-1] if not rc else "refused: " + _why(err or out)
    _, data = _ship_end(p, grant.id, head, rc, out + err, t0, time.time())
    if rc and data["red"] and not any(k in line for k in data["red"]):  # T-4739: the summary carries the classes
        line += f"; failing {c.joinmore(data['red'], 8)}"
    shown = (out + err).splitlines()[-5000:]
    _ship_log(p, "".join(ln + "\n" for ln in shown) + f"{_hms()} {c.fit(c.plain(line), 300)}\n")
    if rc:
        return grant.id, line
    if keep:  # review: only this ship's own push (its own "main at" line) spent it; else someone else's spend stands
        if SHIPPED_AT.search(out):
            _give_back(p, grant.id)
    elif "publish" in ((c.find_brief(p, grant.id) or grant).meta.get("allow") or []):  # nothing was left to push
        release._spend(p, grant.id, "sampo lane land")
    return grant.id, line


# ---------------------------------------------------------------- the land's ship, detached (T-4239)

def _ship_marker(p):
    """The file holding the running ship's pid and start time: its presence with a live pid is "a ship is running"."""
    return os.path.join(p.dir, "ship", "marker.json")


def _ticks(pid):
    """A process's start time in clock ticks (field 22 of /proc/PID/stat), the id a reused pid can't share; 0 when it
    isn't running or is a zombie. T-4633: a host with no /proc (macOS) gets the start time in epoch seconds from ps,
    which every liveness check here read as 'not running' (0) before."""
    try:
        with open(f"/proc/{int(pid)}/stat") as f:
            rest = f.read().rsplit(")", 1)[1].split()
        return 0 if rest[0] == "Z" else int(rest[19])
    except (IndexError, ValueError):
        return 0
    except OSError:
        pass
    try:
        with open("/proc/self/stat"):  # a /proc that answers for this process: the pid just isn't there
            return 0
    except OSError:
        pass
    return c.ps_start(pid) or 0  # T-4672: one reading of ps's start time for every reader


def _read_marker(p):
    try:
        with open(_ship_marker(p)) as f:
            m = json.load(f)
        return m if isinstance(m, dict) else None
    except (OSError, ValueError):
        return None


def _ship_alive(p):
    """The running ship's marker (pid, ticks, started, want, ship_head, again, final, log), or None: no marker, or its
    pid is gone, a zombie, or another process now (a dead ship's marker never blocks the next)."""
    m = _read_marker(p)
    if not m or not isinstance(m.get("pid"), int):
        return None
    if m.get("ticks"):
        return m if _ticks(m["pid"]) == m["ticks"] else None
    try:  # a host without /proc: the pid alone
        os.kill(m["pid"], 0)
        return m
    except OSError:
        return None


def ship_last(p):
    """The record of the newest finished land ship (head, sha, seconds, exit, end, line, seen), or None."""
    try:
        with open(os.path.join(p.dir, "ship-last.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _ship_request(p, main, final):
    """T-4239: a land asks for a ship of the head it just landed, never waiting for it. A running ship (its marker holds a
    live pid) is told to go again when it is done (N requests during one ship cost one more ship); else, with a publish
    grant held, one detached niced `sampo ship` starts on its own tree. `final`: no train follows this request, so the
    grant is spent at the last ship (the newest request decides). ("started"|"joined"|"failed", detail) or None (no
    grant, nothing started)."""
    head = c._git(main, "rev-parse", "HEAD", timeout=10).strip()
    path = _ship_marker(p)
    me = {"land_pid": os.getpid(), "land_ticks": _ticks(os.getpid())}  # the newest asker: the ship gives the grant back only while it lives
    with c.lock(os.path.dirname(path), timeout=30):
        m = _ship_alive(p)
        if m:  # no grant check: the running ship spent it, and gives it back when it ends
            m.update(want=head, again=head != m.get("ship_head"), final=final, **me)
            c.write_atomic(path, json.dumps(m))
            return "joined", m
        with contextlib.suppress(OSError):  # a dead ship's marker
            os.remove(path)
        grant = _grant_of(p)
        if not grant:
            return None
        log = os.path.join(p.dir, "ship.log")
        lib = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = dict(os.environ, PYTHONPATH=lib + os.pathsep + os.environ.get("PYTHONPATH", ""))
        arg = json.dumps({"ship": {"project": [p.slug, p.root, p.dir, p.lane], "main": main, "grant": grant.id}})
        try:
            with open(log, "a", encoding="utf-8") as out:
                proc = subprocess.Popen(c.nice_argv([sys.executable, "-m", "sampo.train", arg]), cwd=main, env=env,
                                        stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                        start_new_session=True)
        except OSError as e:
            return "failed", f"{type(e).__name__}: {e}"
        threading.Thread(target=proc.wait, daemon=True).start()  # no zombie while this land lives
        m = {"pid": proc.pid, "ticks": _ticks(proc.pid), "started": round(time.time(), 1), "want": head,
             "ship_head": "", "again": False, "final": final, "grant": grant.id, "log": log, **me}
        c.write_atomic(path, json.dumps(m))
        return "started", m


def _hms():
    return time.strftime("%H:%M:%S")


def _ship_run(main, grant, head, argv=None):
    """(exit code, output) of one `sampo ship --task GRANT` pinned to `head` (SAMPO_SHIP_AT: it tests and pushes that
    snapshot), every output line stamped into this process's own log; ended after SAMPO_SHIP_TIMEOUT_S (7200).
    Review: the output goes to a file, never a pipe: a process that left the group and holds a pipe's write end keeps
    the read loop waiting for an EOF that never comes (T-2255), so the cap would never fire. The deadline is
    wait(timeout) then a group kill; whoever still holds the file writes into an unlinked file."""
    env = c.scrubbed_env(dict(os.environ, FOREMAN_SHIP_AT=head))  # T-4289: the ship's tests take no test override
    limit = float(os.environ.get("FOREMAN_SHIP_TIMEOUT_S") or 7200)  # SAMPO_SHIP_TIMEOUT_S, mirrored by core
    fd, path = tempfile.mkstemp(prefix="ship-run-", suffix=".log")
    out, part, capped = [], "", False
    try:
        with os.fdopen(fd, "ab") as w, open(path, encoding="utf-8", errors="replace") as r:
            proc = subprocess.Popen(argv or [sys.executable, os.path.join(c.PLUGIN_ROOT, "bin", "sampo"), "ship",
                                             "--task", grant],
                                    cwd=main, env=env, stdin=subprocess.DEVNULL, stdout=w, stderr=subprocess.STDOUT,
                                    start_new_session=True)

            def drain(last=False):
                nonlocal part
                part += r.read()
                *lines, part = part.split("\n")
                if last and part:
                    lines.append(part)
                    part = ""
                for ln in lines:
                    out.append(ln + "\n")
                    del out[:-5000]
                    print(f"{_hms()} {ln.rstrip()}", flush=True)
            deadline = time.monotonic() + limit
            rc = None
            while rc is None:
                try:
                    rc = proc.wait(timeout=max(0.0, min(0.4, deadline - time.monotonic())))
                except subprocess.TimeoutExpired:
                    if time.monotonic() >= deadline:
                        capped = True
                        with contextlib.suppress(OSError):
                            os.killpg(proc.pid, signal.SIGKILL)
                        rc = proc.wait()
                drain()
            drain(last=True)
    finally:
        with contextlib.suppress(OSError):
            os.remove(path)
    if capped:
        out.append(f"timed out: the ship was ended after {limit:g}s\n")
        rc = 124
    return rc, "".join(out)


def _land_alive(m):
    """False only when the marker names the land that requested the ship and that process is gone (or its pid is another
    process now: another start time); a marker without the land's identity reads alive."""
    pid = m.get("land_pid")
    return True if not pid else bool(m.get("land_ticks")) and _ticks(pid) == m.get("land_ticks")


def _settle(p, grant, rc, out, m):
    """Review: the grant after one detached ship (called under the marker lock). A ship that exited 0: the land's last
    ship (final, and none to follow) spends a grant still held (nothing was pushed); any other gives it back, but only
    when THIS ship's own push spent it (its output has its own "main at <sha>" line: a ship with no push of its own never
    undoes a hand ship's spend) and only while a request is waiting (`again`) or the land that asked is still alive to
    ask again (else it stays spent)."""
    from sampo import release
    if rc:
        return
    b = c.find_brief(p, grant)
    if bool(m.get("final")) and not m.get("again"):
        if b is not None and "publish" in (b.meta.get("allow") or []):
            with contextlib.suppress(Exception):
                release._spend(p, grant, "sampo lane land")
    elif SHIPPED_AT.search(out) and (m.get("again") or _land_alive(m)):  # a request is waiting, or its land can still ask
        _give_back(p, grant)


def _ship_child(a):
    """T-4239: the detached ship process (python -m sampo.train {"ship": ...}): ships the head the marker wants, writes
    the ship_done event, settles the grant, and goes again when a later train asked, else clears the marker. The grant
    is given back after a ship that is not the last (or that a later request will follow); the land's last ship leaves
    it spent. A refusal or a red run is a line and an event, never an exception."""
    p = c.Project(*a["project"])
    main, grant = a["main"], a["grant"]
    path = _ship_marker(p)
    try:
        while True:
            with c.lock(os.path.dirname(path), timeout=30):
                m = _read_marker(p) or {}
                head = m.get("want") or c._git(main, "rev-parse", "HEAD", timeout=10).strip()
                m.update(ship_head=head, again=False)
                c.write_atomic(path, json.dumps(m))
            print(f"{_hms()} ship of {head[:9]} on {grant}'s publish grant", flush=True)
            t0 = time.time()
            try:
                rc, out = _ship_run(main, grant, head)
            except Exception as e:  # noqa: BLE001 — never an exception out of the loop
                rc, out = 1, f"{type(e).__name__}: {e}"
            line, _ = _ship_end(p, grant, head, rc, out, t0, time.time())
            print(f"{_hms()} {c.fit(c.plain(line), 300)}", flush=True)
            def said(e):  # a failure here is a timed line in the log, never the loss of a queued ship
                print(f"{_hms()} grant not settled after the ship: {type(e).__name__}: {c.fit(c.plain(str(e)), 200)}",
                      flush=True)
            try:
                with c.lock(os.path.dirname(path), timeout=30):
                    m = _read_marker(p) or {}
                    again = bool(m.get("again"))
                    try:
                        _settle(p, grant, rc, out, m)
                    except Exception as e:  # noqa: BLE001 — a hook holding the project lock, a brief that won't save
                        said(e)
                    if not again:
                        with contextlib.suppress(OSError):
                            os.remove(path)
                        return 0
            except Exception as e:  # noqa: BLE001 — the marker lock itself: the request is read without it
                said(e)
                if not (_read_marker(p) or {}).get("again"):
                    return 0  # the finally clears the marker
    finally:
        m = _read_marker(p)
        if m and m.get("pid") == os.getpid():  # an escaped exception: the marker never outlives its ship
            with contextlib.suppress(OSError):
                os.remove(path)


def _accepted(b):
    """The epoch sampo lane accept queued a lane at (its brief's accepted_at), or None."""
    try:
        return float(b.meta.get("accepted_at")) if b is not None and b.meta.get("accepted_at") else None
    except (TypeError, ValueError):
        return None


def _train_times(p, land_id, n, got, by, wall):
    """T-4239: one train's times: the summary row (lanes landed, minutes in the gate, minutes waiting: the train's wall
    time minus its gate run, the probes, the merge and the queue before them) and, for a train that landed lanes, a
    land_train ledger event (lanes, seconds, gate_s, wait_s, at, each lane's accept time)."""
    gate = round(float((got.get("minutes") or {}).get("gate") or 0) + float((got.get("minutes") or {}).get("confirm") or 0), 1)
    wait = max(0.0, round(wall / 60 - gate, 1))
    row = {"train": n, "lanes": list(got["merged"]), "gate": gate, "waiting": wait, "seconds": round(wall, 1)}
    if got["merged"]:
        acc = {t: a for t in got["merged"] if (a := _accepted(by.get(t)))}
        try:
            c.log_event(p, "land_train", data={"land": land_id, "train": n, "lanes": list(got["merged"]),
                                               "seconds": round(wall, 1), "gate_s": round(gate * 60, 1),
                                               "wait_s": round(max(0.0, wall - gate * 60), 1),
                                               "at": round(time.time(), 3), "accepted": acc})
        except Exception:  # noqa: BLE001 — a record that can't be written never stops the land
            pass
    return row


def last_land(p):
    """T-4239: the newest land's trains from the ledger: {lanes, trains, gate, wait (minutes), live}: `live` is each
    accepted lane's seconds from its accept to the end of the first successful ship that started after its train. None
    when no land has recorded a train."""
    evs = c.ledger_tail(p, 3000, ["land_train", "ship_done"])
    trains = [e for e in evs if e.get("event") == "land_train"]
    if not trains:
        return None
    lid = (trains[-1].get("data") or {}).get("land")
    mine = [e["data"] for e in trains if (e.get("data") or {}).get("land") == lid]
    ships = [e["data"] for e in evs if e.get("event") == "ship_done" and (e.get("data") or {}).get("exit") == 0]
    live = []
    for d in mine:
        for acc in (d.get("accepted") or {}).values():
            done = next((s for s in ships if s.get("start", 0) >= d.get("at", 0)), None)
            if done and acc:
                live.append(max(0.0, done["end"] - acc))
    return {"land": lid, "lanes": sum(len(d.get("lanes") or []) for d in mine), "trains": len(mine),
            "gate": round(sum(d.get("gate_s", 0) for d in mine) / 60, 1),
            "wait": round(sum(d.get("wait_s", 0) for d in mine) / 60, 1), "live": live}


def land_line(p):
    """T-4239: sampo status's line about the newest land, or ''."""
    got = last_land(p)
    if not got:
        return ""
    tail = (f"accept-to-live median {statistics.median(got['live']) / 60:.1f} min" if got["live"] else
            f"gate {got['gate']} min, waiting {got['wait']} min")
    return f"Last land: {got['lanes']} lane(s) in {got['trains']} train(s); {tail}"


def ship_line(p):
    """T-4239: sampo status's line about a land ship that outlived its land: running (with its log), or how it ended
    when no land has told it yet; '' otherwise."""
    m = _ship_alive(p)
    if m:
        return f"Land ship running since {time.strftime('%H:%M:%S', time.localtime(m['started']))}: {m['log']}"
    rec = ship_last(p)
    if rec and not rec.get("seen"):
        return (f"Land ship ended at {time.strftime('%H:%M:%S', time.localtime(rec['end']))} ({rec['grant']}'s grant): "
                f"{rec['line']}")
    return ""


def _ship_wait(p, secs):
    """True once no ship runs (waits at most `secs`: a land's last wait is bounded)."""
    end = time.monotonic() + secs
    while _ship_alive(p):
        if time.monotonic() >= end:
            return False
        time.sleep(0.25)
    return True


def _why(text):
    """A refusal's own lines (its Next: hint aside) as one line. T-4147: a finish refusal numbers its missing lines, each
    with a `fix:` line under it that repeats the item's own fix; the reason keeps the items and drops the repeats and the
    numbers, so the 400-character cut still reaches the last item (the docs line) as the bullets did."""
    lines = []
    for ln in text.splitlines():
        s = ln.strip().removeprefix("fm: ").removeprefix("sampo: ").removeprefix("refused: ").removeprefix("- ")
        if s.startswith("fix: ") or s.startswith("Next:"):  # the fix under a numbered item: the item already says it
            continue
        s = re.sub(r"^\d+\. ", "", s)  # a numbered item of the list
        lines.append(re.sub(r" missing:$", " missing", s))  # the count line's colon: the list follows it
    return c.fit("; ".join(ln for ln in lines if ln), 400) or "no reason given"


def _park_kind(why):
    """The lane_stop (kind, arg) a train's reason maps to, as `lane merge` marks the same refusals (lanes.STOP_CAUSES)."""
    if why.startswith("conflicts with"):
        return "conflict", ""
    if why.startswith("the self-change gate"):
        return "guard", "core"
    if why.startswith("the guard refuses"):
        return "guard", why.rpartition("(")[2].rstrip(")")
    return "refused", ""


def announce_skills(root, before, after):
    """T-3851: a session lists its skills when it starts, so each SKILL.md a train added between `before` and `after`
    is told to every live session once (the bus), with its description and the file to Read. The names sent."""
    from sampo import bus, plugins
    added = (c._git(root, "diff", "--name-only", "--no-renames", "--diff-filter=A", before, after, "--", "plugin/skills", timeout=30,
                    fail="") or "").split("\n") if before and after and before != after else []
    sent = []
    for rel in (f for f in added if re.fullmatch(r"plugin/skills/[\w-]+/SKILL\.md", f)):
        path, name = os.path.join(root, rel), rel.split("/")[2]
        desc = c.fit(c.plain(plugins._frontmatter(path).get("description") or ""), 200)
        bus.send("all", f"New Sampo skill {c.NS}:{name} landed: {desc} Sessions started before it can't load it by "
                        f"name: Read {path}")
        sent.append(name)
    return sent


def _first_state(b, tid, why, landed):
    """T-4252: what `sampo lane land --first` finds for a lane not waiting on a merge: parked in this land (its why),
    merged, parked by an earlier land, not a lane, or still building (its builder runs). The catch-all only when none
    of those can be told from the brief."""
    from sampo import bus
    from sampo import lanes
    if why:
        return f"parked: {why}"
    if b is None:
        return "not a task: no brief has that id"
    if tid in landed or lanes._stop(b)[0] == "merged":
        return "merged already"
    if lanes.parked(b):
        return f"parked by an earlier land (its last merge: {lanes._stop(b)[0]})"
    if not b.meta.get("lane"):
        return "not a lane: its task has no lane to land"
    if b.status not in c.CLOSED and bus.proc_state(b.meta.get("builder_proc")) == "alive":
        return "still building: its builder is running and its lane is not yet quiet"
    return ("not a finished lane waiting on a merge here (its state can't be told from here: merged, parked or "
            "still building)")


def _note_refusal(p, why):
    """T-4526: a refused land's reason as one stamped line of the state's land.log. A detached land's stderr is that log
    already (its refusal is printed there), so it adds nothing. 2026-11-12: a refusal for an active task went unread for
    2 h because nothing wrote it down where the land's output went."""
    if isinstance(sys.stderr, _Stamped):
        return
    with contextlib.suppress(OSError):
        with open(os.path.join(p.dir, "land.log"), "a", encoding="utf-8") as f:
            f.write(f"{_hms()} refused: {why}\n")


def land(p, main, batch=None):
    """T-3500: every finished lane waiting on a merge (lanes.awaiting_merge) through `train`, `batch` at a time (one
    that conflicts with the branch is parked before any criteria or gate run); each landed lane is removed and its task
    closed from its brief's Close fields through sampo task finish's own checks; the rest are parked (lane_stop and a
    Log line), which awaiting_merge leaves out until the lane has a new commit. Nothing is forced, reset or granted;
    a task already holding the publish grant ships after. One land at a time; an active main task holds it only by uncommitted changes to a lane's file or the changelog."""
    why = machine_stop(p)  # T-4652: the disk refuses the land at once, before a lane is parked or a batch trains
    if why:
        raise c.PolicyError(why)
    if why := held_by_active(p, main):  # T-4692: an unrelated focus holds nothing; uncommitted lane files do
        _note_refusal(p, why)
        raise c.PolicyError(why)
    with contextlib.ExitStack() as held:
        try:
            held.enter_context(c.lock(os.path.join(p.dir, "land"), timeout=1))
        except c.LockTimeout:
            raise c.PolicyError("another sampo lane land is running; it lands these lanes too") from None
        return _land(p, main, batch or c.TRAIN_BATCH)


def first_ids(p, add=()):
    """T-3796: the ids sampo lane land --first put ahead of the rest: `add` appends to them (returning them all);
    with none, they are taken (returned and emptied) at a train boundary."""
    d = os.path.join(p.dir, "land-first")
    path = os.path.join(d, "ids")
    with c.lock(d):
        try:
            with open(path) as f:
                have = f.read().split()
        except OSError:
            have = []
        keep = list(dict.fromkeys([*have, *add])) if add else []
        if keep or have:
            c.write_atomic(path, " ".join(keep))
    return keep if add else have


def land_key(b):
    """T-3796: the order lanes train in: urgent, then performance and security fixes, then the user's own asks, then
    the rest (discovered work, follow-ups), oldest first in each."""
    kind = 0 if b.type in ("PERFORMANCE", "SECURITY") else 1 if b.meta.get("source") == "user" else 2
    return (b.priority != "urgent", kind, c.id_num(b.id))


def _stale_affected(b, sha, main):
    """T-3963: the note for a lane whose newest `sampo check --affected` run came before its own last commit (a merge of
    main's branch is not its work), else None. 2026-11-08 left out three lanes whose scans and exit-code tests only the
    rerun would have reached. T-4767: the land holds such a lane unless its check is green at the tip (_unchecked), so
    this note only reaches one whose green check postdates its stale evidence."""
    if _affected_state(b, sha, main)[0] == "stale":
        return (f"its newest affected run predates the lane tip {sha[:9]}: rerun sampo check --affected --evidence "
                f"{b.id} in its lane after the last commit")
    return None


def _code_stamp():
    """T-3973: path, mtime and size of every Sampo module on disk: a change means a train merged new code for this land."""
    d = os.path.join(c.PLUGIN_ROOT, "lib", "sampo")
    return tuple((f, s.st_mtime_ns, s.st_size) for f in sorted(os.listdir(d)) if f.endswith(".py")
                 for s in [os.stat(os.path.join(d, f))])


def start_verify(p, main, owed):
    """T-4718: the background verify a train deferred, started now (once) when the caller owes one: after the ship was
    asked for or ran in line, so the verify's suite runs never take the slots the ship's affected run waits for. `owed`
    is the caller's one-item list, cleared here. It starts when the caller owes one OR a record is still pending/red
    on disk (a land that raised or was killed during its closes never set the flag, but its records stand with no
    runner: a young pending record even holds off the sentinel's own full run). The runner is single (it takes the
    queue's lock), so a second start is harmless. It never raises: the landed lanes and the ship's result must stand."""
    from sampo import lanes
    try:
        if not owed[0] and not any(r.get("state") in ("pending", "red") for r in lanes.pm_records(p).values()):
            return
        owed[0] = False
        lanes.pm_spawn(p, main)
    except Exception as e:  # noqa: BLE001 — a failed spawn leaves the records pending: the next train or verify takes them
        with contextlib.suppress(Exception):
            c.log_event(p, "postmerge_spawn_failed", data={"why": c.fit(str(e), 200)})


def _land(p, main, batch):
    """T-4718: the trains defer their background verify; it starts after the ship (or, whatever happens, at the end)."""
    owed = [False]
    try:
        return _land_trains(p, main, batch, owed)
    finally:
        start_verify(p, main, owed)


def _land_trains(p, main, batch, owed):
    from sampo import lanes
    stamp = _code_stamp()  # the code this process loaded
    briefs = c.load_briefs(p)
    branch = _git(main, "symbolic-ref", "--short", "-q", "HEAD").stdout.strip() or "HEAD"

    def tip(b):
        return _git(main, "rev-parse", "--verify", "-q",
                    f"{b.meta.get('lane_branch') or 'foreman/' + b.id}^{{commit}}").stdout.strip()
    # a land cut short (or a train by hand) left these merged and open: they only need their close
    landed = [b.id for b in briefs if b.status not in c.CLOSED and b.meta.get("lane") and lanes._stop(b)[0] == "merged"
              and (t := tip(b)) and not _git(main, "merge-base", "--is-ancestor", t, "HEAD").returncode]
    parked, ready, trains, stopped, stale, restart = [], [], 0, "", set(), False
    closed, hand, shipped = [], [], None
    by, tested = {}, {}  # T-3994: each lane's brief, and the tip each verdict judged (a park holds only that tip)
    times, background, asked = [], not os.environ.get("FOREMAN_NO_BACKGROUND") and not c.panicked(), False
    ship_failed = None  # the newest request's failure to start a ship (None once a later one started or joined)
    land_id = f"{os.getpid()}-{int(time.time())}"
    if (rec := ship_last(p)) and not rec.get("seen"):  # T-4239: the end of a ship that outlived its land
        print(f"sampo lane land: the earlier ship ended at {time.strftime('%H:%M:%S', time.localtime(rec['end']))} "
              f"({rec['grant']}'s grant): {rec['line']}", file=sys.stderr, flush=True)
        with contextlib.suppress(OSError):
            c.write_atomic(os.path.join(p.dir, "ship-last.json"), json.dumps({**rec, "seen": True}))

    def ship(final):  # T-4239: where nothing runs detached the ship is in line, as before; else it is asked for
        nonlocal asked, ship_failed
        if not background:
            return _ship(p, not final, main)
        got = _ship_request(p, main, final)
        if got:
            kind, m = got
            asked, ship_failed = kind in ("started", "joined"), (None if kind in ("started", "joined") else str(m))
            print("sampo lane land: " + (f"ship started (pid {m['pid']}, log {m['log']})" if kind == "started" else
                  f"ship joined for {m.get('want', '')[:9]}: the running ship goes again when it is done"
                  if kind == "joined" else f"ship not started: {m}"), file=sys.stderr, flush=True)
        return None

    def close(ids):  # T-3796: each lane closes once its train lands it, not after the whole pile has trained
        done, left = close_landed(p, main, ids)
        closed.extend(done)
        hand.extend(left)

    def note_park(pairs):  # T-4252: one line per lane left out, as it happens; plain() keeps a gate's output to one line
        for i, w in pairs:
            print(f"sampo lane land: parked {i}: {c.fit(c.plain(w), 400)}", file=sys.stderr, flush=True)

    held = set()  # T-4767: lanes the check hold left out: read again when a train starts or one is named --first

    def sort(bs):  # a conflict never costs a criteria or gate run (an old git without merge-tree: no check)
        for b in sorted(bs, key=land_key):
            if why := _check_hold(b, tip(b), main) or _unchecked(b, tip(b), main):  # T-4286: its check is running, red or cut off at this tip: left out; T-4767: no passing check at the tip at all
                print(f"sampo lane land: {b.id}: {why}", file=sys.stderr, flush=True)
                held.add(b.id)
                continue
            held.discard(b.id)
            by[b.id], tested[b.id] = b, tip(b)
            r = _git(main, "merge-tree", "--write-tree", "--name-only", "--no-messages", "HEAD", tested[b.id] or "HEAD")
            files = [f for f in r.stdout.splitlines()[1:] if f] if r.returncode == 1 else []
            if r.returncode == 1 and not _map_only(files):  # T-4375: a map-only clash is the train's to merge
                parked.append((b.id, f"conflicts with {branch}" + (f": {c.joinmore(files, 8)}" if files else "")))
                note_park(parked[-1:])  # T-4252: said now, not at the land's end
            else:
                ready.append(b.id)
                if why := _stale_affected(b, tip(b), main):  # T-3963: trains after the fresh ones, still lands
                    stale.add(b.id)
                    print(f"sampo lane land: {b.id}: {why}", file=sys.stderr, flush=True)
                if gaps := _close_gaps(b):  # T-3819: said before the trains, so the lines are added while they run
                    print(f"sampo lane land: {b.id} lands but its close will be refused: {'; '.join(gaps)} (add the "
                          "lines to its Close fields before its train ends)", file=sys.stderr, flush=True)
        ready.sort(key=lambda t: t in stale)  # stable: fresh lanes first, each group in land order
    def recheck(ids):
        """T-4767: `ids` without the lanes the hold now catches: sort read it at the land's start, and a lane that waits
        for a later train (each one is minutes of gate) may since have a new commit with no check at it, or a check that
        ended red. A dropped lane is said, not parked: it joins again once its check is clean (readmit), else the next land."""
        now, keep = {b.id: b for b in c.load_briefs(p)}, []
        for t in ids:
            b = now.get(t)
            if b and (why := _check_hold(b, tip(b), main) or _unchecked(b, tip(b), main)):
                print(f"sampo lane land: {t}: {why}", file=sys.stderr, flush=True)
                held.add(t)
            else:
                keep.append(t)
        return keep

    def readmit():
        """T-4767 review: the held lanes whose check is clean now (a hand check or an accept finished while a train ran)
        are sorted in, as the held line promises. Said once: a lane still held is not printed again."""
        now = c.load_briefs(p)
        back = {b.id for b in now if b.id in held and not (_check_hold(b, tip(b), main) or _unchecked(b, tip(b), main))}
        back &= set(lanes.awaiting_merge(p, now, 0, fresh=True))
        sort(b for b in now if b.id in back)
    left = list(landed)  # merged and open (a train by hand, a land cut short): closed after the trains (T-3884)
    waiting = set(lanes.awaiting_merge(p, briefs)) - set(landed)
    sort(x for x in briefs if x.id in waiting)
    seen = set(waiting) | set(landed)
    first_taken = set()  # T-4013: every --first id this land took (`asked` is the ship request's flag, not this)
    while True:
        jump = first_ids(p)  # T-3796: asked for meanwhile go next, a lane finished since the land began too
        first_taken.update(jump)
        moved = {t for t, _ in parked if t in first_taken and tested.get(t) != tip(by[t])}
        if moved:  # T-4013: a --first lane parked on a conflict its tip has since moved past is judged again
            now = c.load_briefs(p)
            again = set(lanes.awaiting_merge(p, now, 0, fresh=True)) & moved  # T-4265: its new tip is seconds old
            parked[:] = [x for x in parked if x[0] not in again]
            sort(b for b in now if b.id in again)
            jump = jump + sorted(again - set(jump))
        if held and (trains or held & set(jump)):  # T-4767: a held lane whose check has turned green since is sorted in
            readmit()
        if jump:
            late = set()
            if set(jump) - seen:
                now = c.load_briefs(p)
                # T-3824: named, so finished; T-4265: read fresh, its fix may be seconds old
                late = (set(lanes.awaiting_merge(p, now, 0, fresh=True)) - seen) & set(jump)
                sort(b for b in now if b.id in late)
                seen |= late
            front = [t for t in jump if t in ready]
            ready = front + [t for t in ready if t not in front]
            if any(t not in front for t in jump):  # (`now` above is a list of briefs; this one a map by id)
                by_id = {b.id: b for b in c.load_briefs(p)}
            for t in (t for t in jump if t not in front):
                why = next((w for i, w in parked if i == t), "")
                print(f"sampo lane land: --first {t}: " + _first_state(by_id.get(t), t, why, landed),
                      file=sys.stderr, flush=True)
        if ready and trains:  # T-4767: a lane waits out the trains before it: its check is read again at this one's start
            ready = recheck(ready)
        if not ready:
            break
        ids, ready = ready[:batch], ready[batch:]
        print(f"sampo lane land: train {trains + 1}: {', '.join(ids)}", file=sys.stderr, flush=True)
        tested.update({i: tip(by[i]) for i in ids})  # train() reads the same heads as it starts
        head0 = c._git(main, "rev-parse", "HEAD", timeout=10).strip()  # T-3851: the skills this train adds
        t_train = time.monotonic()
        try:
            got = train(p, main, ids, defer_verify=True)
            owed[0] = owed[0] or bool(got.get("verify_owed"))
        except c.PolicyError as e:
            if not hasattr(e, "left"):  # a red base, a moved branch: no later batch would land either
                stopped = str(e)  # T-4029: with the red gate's own lines, so nobody reruns it to learn them
                break
            got = {"merged": [], "left_out": e.left}
        except c.LockTimeout:
            stopped = "a sampo lane train is running: land again after it"
            break
        note_park(got["left_out"])  # T-4252: before the next train starts, not in the summary
        if got.get("kept"):  # T-4007: the main checkout's local changes the fast-forward set aside
            print(f"sampo lane land: {got['kept']}", file=sys.stderr, flush=True)
        trains += 1
        landed += got["merged"]
        parked += got["left_out"]
        times.append(_train_times(p, land_id, trains, got, by, time.monotonic() - t_train))
        close(got["merged"])
        if got["merged"]:
            try:  # a note to the sessions never costs the land
                announce_skills(main, head0, c._git(main, "rev-parse", "HEAD", timeout=10).strip())
            except Exception as e:
                print(f"sampo lane land: new skills not announced: {c.fit(c.plain(str(e)), 160)}", file=sys.stderr)
        # T-4718: the verify starts after the ship asked, never beside it, and (its suite run is one long run with no
        # per-class boundary to yield at) only once no further train of this land is queued: else it would starve the
        # next train's ship again. What is owed waits in `owed`; the land's end starts it whatever happened.
        if got["merged"]:  # T-3849: reaches the remote per train, the grant kept for the next
            try:
                shipped = ship(False) or shipped  # T-4239: asked for, never waited on: the next train starts at once
            finally:
                if not ready:
                    start_verify(p, main, owed)
        elif not ready:
            start_verify(p, main, owed)  # a red merge's revert pass owed by a train that landed nothing
        if ready and _code_stamp() != stamp:  # T-3973: the train changed Sampo itself: the rest runs on the new code
            restart = True
            first_ids(p, ready)  # the restarted land trains them in this order, not re-sorted
            break
    if not restart:  # a restart's leftovers close under the new code (it finds them merged and open again)
        close(left)
    for tid, why in parked:
        kind, arg = _park_kind(why)
        lanes.stop_mark(p, tid, kind, arg, log=f"land: parked: {c.fit(why, 300)}", tip=tested.get(tid, ""))
    try:
        last = ship(True) if landed and not restart else None  # the land's last ship spends the grant: a restart's successor does
    finally:
        start_verify(p, main, owed)
    shipped = last if last and not last[1].startswith("nothing to ship") else shipped or last
    grant, said = shipped or (None, "")
    return {"landed": landed, "closed": [t for t, _ in closed], "hand": hand, "parked": parked, "trains": trains,
            "stopped": stopped, "restart": restart, "ship": said, "ship_grant": grant, "lines": closed,
            "train_times": times, "ship_asked": asked, "ship_failed": ship_failed}


def close_landed(p, main, ids, who="sampo lane land"):
    """T-4596: the one close every landing path makes (a land's trains, a multi-id sampo lane merge, sampo lane land
    --merged): each id's task closes through `_close` (sampo task finish's own checks, re-run on the trunk; nothing
    forced) and its lane goes with it. ([(id, the close's last line)], [(id, why it is left for a hand close)]); one
    lane's refusal or error never costs the others theirs. The gates need no run of their own here: finish reuses the
    full pass on record for the trunk's code tree, which the CHANGELOG line each close commits leaves valid (the code
    tree leaves CHANGELOG.md out), so N closes cost one gate run at most, the first, and none after a train."""
    closed, hand = [], []
    for tid in ids:
        try:
            why, last = _close(p, main, tid)
        except Exception as e:  # noqa: BLE001 — one lane's close never costs the others theirs
            why, last = f"{type(e).__name__}: {e}", ""
        (hand if why else closed).append((tid, why or last))
        steps = close_steps(last) if not why else ""  # T-4600: a slow close names its step in the land's own log
        print(f"{who}: left for a hand close {tid}: {c.fit(why, 400)}" if why else f"{who}: closed {tid}",
              file=sys.stderr, flush=True)  # T-3819: the reason, not hours later
        if steps:  # its own line: the 'closed T-N' line stays what land logs and tests read
            print(f"{who}: {tid} {c.fit(steps, 300)}", file=sys.stderr, flush=True)
    return closed, hand


def _worktree_dirty(path):
    """What `git status` lists in a lane's folder (uncommitted or untracked files; ignored ones are not work), '' when
    clean; None when the folder is not a worktree git can read."""
    if not os.path.isdir(path):
        return None
    r = _git(path, "status", "--porcelain")
    return None if r.returncode else r.stdout.strip()


def _names_task(main, tid):
    """T-4603: True when a commit on the trunk names the task in its subject ('Merge T-0007', 'Fix it (T-0007)'): the id
    ends at a non-digit, so T-0007 is not found in T-00071."""
    r = _git(main, "log", "HEAD", "-E", f"--grep={re.escape(tid)}([^0-9]|$)", "--format=%s")
    return any(re.search(rf"{re.escape(tid)}(?!\d)", s) for s in r.stdout.splitlines())


def _empty_lane(main, first_parent, tid, tips):
    """T-4603: a lane in which nothing was ever built. Its tip (and its worktree's HEAD) is an ancestor of the trunk
    only because it is an old commit of the trunk's own first-parent line (a merged lane's commits are on a side line,
    reached through a merge), and no trunk commit names its task (a lane fast-forwarded in has its own commits on that
    line, and its subject names the task). `first_parent` is the trunk's first-parent line, read once for the sweep."""
    return all(t in first_parent for t in tips) and not _names_task(main, tid)


def _merged_lanes(p, main):
    """T-4596: ([(task id, its lane folder)] for every open task whose lane is wholly on the trunk, ordered by id;
    [(id, why)] for the ones the sweep leaves; T-4603: [(id, its lane folder, why it is kept or '')] for the lanes
    holding nothing, whose task was never built). On the trunk: the lane's branch tip and its worktree's own HEAD are
    both ancestors of the trunk's HEAD, so no commit of the lane's is ever lost; a lane with any other commit is not here."""
    on, left, empty = [], [], []
    first_parent = None  # the trunk's first-parent line, read at most once
    for b in sorted(c.load_briefs(p), key=lambda x: c.id_num(x.id)):
        path = b.meta.get("lane") or ""
        if b.status in c.CLOSED or not path:
            continue
        branch = b.meta.get("lane_branch") or f"foreman/{b.id}"
        tip = _git(main, "rev-parse", "--verify", "-q", f"{branch}^{{commit}}").stdout.strip()
        head = _git(path, "rev-parse", "--verify", "-q", "HEAD^{commit}").stdout.strip() if os.path.isdir(path) else ""
        if not tip or any(_git(main, "merge-base", "--is-ancestor", t, "HEAD").returncode for t in (tip, head) if t):
            continue  # not merged (or no branch left to judge): the sweep's business ends here
        if first_parent is None:
            first_parent = set(_git(main, "rev-list", "--first-parent", "HEAD").stdout.split())
        dirty = _worktree_dirty(path)
        if _empty_lane(main, first_parent, b.id, [t for t in (tip, head) if t]):
            why = ("" if dirty == "" else f"its folder {path} is gone or not a worktree git can read" if dirty is None
                   else f"its folder has uncommitted work ({dirty.splitlines()[0][3:]}): nothing is discarded")
            empty.append((b.id, path, why))
        elif dirty is None:
            left.append((b.id, f"its lane folder {path} is gone or not a worktree git can read"))
        elif dirty:
            first = dirty.splitlines()[0][3:]
            left.append((b.id, f"its lane has uncommitted work ({first}{' and more' if len(dirty.splitlines()) > 1 else ''})"
                               ": commit it or remove it, nothing is discarded"))
        else:
            on.append((b.id, path))
    return on, left, empty


def _drop_empty(p, main, empty):
    """T-4603: `sampo lane land --merged --drop-empty`: each clean empty lane goes through the lane remove verb
    (lanes.remove: its folder, registration and the branch it never added a commit to; its task returns to the queue,
    never closed). Returns ([ids dropped], [(id, path, why kept)]); a refusal keeps that lane and costs no other."""
    from sampo import cli
    from sampo import lanes
    dropped, kept = [], []
    for tid, path, why in empty:
        if why:
            kept.append((tid, path, why))
            continue
        try:
            lanes.remove(p, cli.need_brief(p, tid), main)
            dropped.append(tid)
        except Exception as e:  # noqa: BLE001 — one lane's refusal never costs the others theirs
            kept.append((tid, path, f"not dropped: {c.fit(c.plain(str(e)), 200)}"))
    return dropped, kept


def land_merged(p, main, drop_empty=False):
    """T-4596: close the tasks of every lane whose tip is already on the trunk (a hand train, `sampo lane merge`, or a
    land cut short landed them and closed nothing): the lane goes and the task closes through `close_landed`. One gate
    pass serves them all: the one on record for the trunk's code tree, else a single `sampo check` first, whose red
    result leaves every task active. A dirty lane, a task with no close fields or audits and one whose own verify fails
    on the trunk stay active, the reason in the output and on the task's Log. T-4603: a lane in which nothing was built
    is not landed work: it is named empty (its task never built, left open and queued), and with drop_empty a clean
    one is removed, its task returned to the queue, never closed. {"closed", "left": [(id, why)], "gate",
    "empty": [(id, note)], "dropped": [ids]}."""
    from sampo import cli
    if why := held_by_active(p, main, ids=[]):  # T-4692: merged lanes are on the trunk: only the changelog is left to hold
        raise c.PolicyError(why)
    with contextlib.ExitStack() as held:
        try:
            held.enter_context(c.lock(os.path.join(p.dir, "land"), timeout=1))
        except c.LockTimeout:
            raise c.PolicyError("another sampo lane land is running; it closes the lanes it lands") from None
        on, left, empty = _merged_lanes(p, main)
        if on:  # T-4658: the machine before any gate or close, as land() and train() check it. Nothing is checked when no
            # merged lane waits for its close: the empty lanes' removal below frees disk, it takes none.
            why = machine_stop(p)
            if why:
                raise c.PolicyError(why)
        dropped, kept = _drop_empty(p, main, empty) if drop_empty else ([], [(t, x, w) for t, x, w in empty])
        gate = "no merged lane waits for a close" if not on else ""
        if on and cli._full_pass(p):
            gate = "reused the gate pass on record for the trunk's code tree"
        elif on:  # one run for all of them; finish then reuses it for each close (a red one closes nothing)
            print(f"sampo lane land --merged: no gate pass on record for the trunk: one sampo check for the "
                  f"{len(on)} lane(s)", file=sys.stderr, flush=True)
            rc, out, err = _sampo(["check"])
            gate = "ran one sampo check on the trunk" if not rc else ""
            if rc:
                why = f"the trunk's gates are red, so no close rides on them: {c.fit(_why(err or out), 300)}"
                left.extend((t, why) for t, _ in on)
                on = []
                gate = "one sampo check on the trunk, red"
        closed, hand = close_landed(p, main, [t for t, _ in on], who="sampo lane land --merged") if on else ([], [])
        left.extend(hand)
        for tid, why in left:
            try:
                _note_left(p, tid, why)
            except Exception as e:  # noqa: BLE001 — the Log line is a note: it never costs the sweep its result
                print(f"sampo lane land --merged: no note on {tid}: {type(e).__name__}", file=sys.stderr)
    return {"closed": [t for t, _ in closed], "left": left, "lines": closed, "gate": gate,
            "empty": [(t, w) for t, _, w in kept], "dropped": dropped}


def _note_left(p, tid, why):
    """T-4596: the reason a sweep left a task active, on its Log once (the same reason twice in a row is not logged
    again: a sweep run every hour must not grow every brief it leaves)."""
    from sampo import cli
    b = cli.need_brief(p, tid)
    line = f"land --merged: left active: {c.fit(' '.join(why.split()), 300)}"
    log = [x for x in b.section("Log").splitlines() if x.strip()]
    if log and log[-1].endswith(line):
        return
    b.append_log(line)
    c.save_brief(p, b)


def _close_gaps(b):
    """T-3819: what a close would refuse for a line the brief lacks (CHANGELOG, DOCS, NUMBERS, LESSON, why not caught;
    T-4515: the WHY-NOT-CAUGHT line of its Close fields counts for the last)."""
    from types import SimpleNamespace

    from sampo import cli
    from sampo import lanes
    f = lanes.close_fields(b.section(lanes.CLOSE_FIELDS))
    if not f.get("changelog"):
        return ["no CHANGELOG line in its Close fields"]
    return cli._field_gaps(b, SimpleNamespace(docs=f.get("docs"), numbers=f.get("numbers"), lesson=f.get("lesson"),
                                              why_not_caught=f.get("why_not_caught")))


def _brief_hash(b):
    """T-3884: the brief's text without its Log (the refusal record lives there)."""
    return hashlib.sha1("\0".join(f"{h}\1{t}" for h, t in b.sections if h.strip() != "Log").encode()).hexdigest()[:12]


# ponytail: a refusal caused outside the brief, the tip and Sampo's own code (a tool on PATH, the machine) is retried
# after this long
REFUSAL_TTL_S = 6 * 3600


def _code_id():
    """T-4080: the committed tree of the Sampo code running this land, '' outside git: a gate rule fixed on main
    changes a close's answer (T-3701 and T-3671 waited 6 h after T-4061 and T-4062 fixed their refusals)."""
    lib = os.path.dirname(os.path.dirname(os.path.abspath(c.__file__)))
    return (c._git(lib, "rev-parse", "HEAD:./", timeout=10, fail="") or "").strip()[:12]


def _refused_before(b, tip):
    """The reason the last land's close was refused when the brief, the lane tip and Sampo's code are as they were
    then, else None."""
    for line in reversed(b.section("Log").splitlines()):
        if m := re.search(r"^- (\S+) land: close refused: (.*) @ (\w+) (\w*)(?: (\w+))? ?$", line):
            at = c.parse_ts(m.group(1))
            if not at or time.time() - at.timestamp() > REFUSAL_TTL_S:
                return None
            return m.group(2) if (m.group(3), m.group(4), m.group(5) or "") == (_brief_hash(b), tip, _code_id()) else None
    return None


def _close(p, main, tid):
    """('', 'TITLE — the close's last line'), or (why it's left for a hand close, ''). The task closes from the main
    checkout with its lane still in place, which goes only once the close is done: a refused close keeps it for a fix.
    Only audits made after the lane's tip commit count, and the close's lenses name that tip."""
    from sampo import cli
    from sampo import lanes
    if why := held_by_active(p, main, ids=[]):  # T-4692: a focus that came meanwhile holds only by an uncommitted changelog
        return f"{why.split(': finish')[0]}, so land closes nothing more", ""
    b = cli.need_brief(p, tid)
    path = b.meta.get("lane") or ""
    if b.status == "done":  # T-3907: closed inside its lane; the merge was all it lacked
        try:
            lanes.remove(p, b, main)
        except (c.PolicyError, cli.UsageError) as e:
            return f"merged; its lane stays: {str(e).splitlines()[0]}", ""
        return "", f"{b.title} — already closed in its lane"
    sha = _git(path, "rev-parse", "--short", "HEAD").stdout.strip() if path else ""
    last = float(_git(path, "log", "-1", "--format=%ct").stdout.strip() or 0) if path else 0.0
    f = lanes.close_fields(b.section(lanes.CLOSE_FIELDS))
    if not f.get("changelog"):
        if "close lines not stored" in b.section("Log"):  # T-4599: the stop hook saw lines the tail rule refused
            return ("no close fields on its brief (its builder's report had close lines, not at its end: "
                    "fill them with sampo task set ID --section 'Close fields')", "")
        return "no close fields on its brief (none came from a builder's report: it held no CHANGELOG line, or none stopped here)", ""
    line, why = lanes.close_line(f["changelog"], "changelog", tid)
    if why:
        return why, ""
    title = lanes.close_line(f.get("title"), "title", tid)[0] or lanes.close_line(b.title, "title", tid)[0]
    newest = {}
    for (lens, ts, _), text in zip(b.audits(), [x for x in b.evidence() if c._AUDIT_RE.match(x)]):
        newest[lens] = (ts, text)
    if not newest:
        return "no audit recorded: review the merged diff and record it (sampo task audit), then finish it", ""
    fresh = {lens: text for lens, (ts, text) in newest.items() if (at := c.parse_ts(ts)) and at.timestamp() >= last}
    if not fresh:
        return f"its audits predate the lane tip {sha}: review it again", ""
    asks = sorted(lens for lens, text in fresh.items() if "→ changes needed" in text)
    if asks:
        return f"its {', '.join(asks)} audit asks for changes", ""
    seen = f"as recorded before the merge, after lane tip {sha}"
    if (was := _refused_before(b, sha)) is not None:  # T-3884: same brief, same tip: the answer can't have changed
        return was + " (unchanged since the land that refused it: no retry)", ""
    # T-4515: the builder's WHY-NOT-CAUGHT line goes on as the close's why-not-caught; the brief's own section wins
    own = b.section("Why not caught").strip() or not f.get("why_not_caught")
    why_arg = [] if own else [f"--why-not-caught={f['why_not_caught']}"]
    t_finish = time.monotonic()  # T-4600: the close's wall time, lock wait included, beside the steps the finish itself reports
    try:
        with train_lock_closing(p):  # T-4540: the close commits on trunk: it waits for a running train (bounded), not beside it
            rc, out, err = _sampo(["task", "finish", tid, "--audit", f"{LAND_AUDIT} (lane tip {sha})", "--result", seen,
                                   *[x for lens in sorted(set(fresh) - {"self"}) for x in ("--lens", f"{lens}: {seen}")],
                                   *why_arg, "--commit", title])
    except c.LockTimeout:
        return ("a running train holds trunk: the close waits for it, and the next land retries it"
                + ("; its lane is kept" if path else ""), "")
    if rc:
        why = _why(err or out) + ("; its lane is kept" if path else "")
        nb = cli.need_brief(p, tid)  # after the refusal: it may have touched the brief, and the hash must match then
        nb.append_log(f"land: close refused: {c.fit(' '.join(why.split()), 400)} @ {_brief_hash(nb)} {sha} {_code_id()}")
        c.save_brief(p, nb)
        return why, ""
    finish_s = time.monotonic() - t_finish
    done = cli.need_brief(p, tid)
    t_lane = time.monotonic()
    if path and done.meta.get("lane"):
        try:
            lanes.remove(p, done, main)
        except (c.PolicyError, cli.UsageError) as e:  # closed; only the folder is left: fm tidy offers it
            out += f"\nclosed; its lane stays: {str(e).splitlines()[0]}"
    steps = close_steps(out)  # T-4600: where this close's seconds went, so a slow one in a land's log names its step
    note = (f" [{steps}; finish {finish_s:.0f} s wall, lane removal {time.monotonic() - t_lane:.0f} s]" if steps else "")
    return "", f"{title} — {(out.strip().splitlines() or ['closed'])[-1]}{note}"


CLOSE_STEPS = re.compile(r"Close steps \([^)]*\): [^\]\n]*")


def close_steps(text):
    """T-4600: the `Close steps (N s): verify …, affected … (ran), …` line a `sampo task finish` printed, '' when it
    printed none (an older sampo, a refusal)."""
    m = CLOSE_STEPS.search(text or "")
    return m.group(0).strip() if m else ""


class _Stamped:
    """T-4239: a text stream that starts every line with its local time (HH:MM:SS), so land.log says when each thing
    happened. Whole lines go to the stream in one write each; a partial line waits for its newline (flush ends it)."""

    def __init__(self, raw):
        self.raw, self.part = raw, ""

    def write(self, s):
        self.part += s
        *whole, self.part = self.part.split("\n")
        if whole:
            self.raw.write("".join(f"{_hms()} {ln}\n" for ln in whole))
            self.raw.flush()  # stdout and stderr share one log: their lines stay in the order they were said
        return len(s)

    def flush(self):
        if self.part:
            self.raw.write(f"{_hms()} {self.part}\n")
            self.part = ""
        self.raw.flush()

    def __getattr__(self, name):
        return getattr(self.raw, name)


def _stamp_output(args):
    """T-4239: SAMPO_LAND_STAMP=1 (a detached land sets it) puts the time at the start of every line the land prints,
    its traceback and the summary too; never on --json output, which a script reads."""
    if os.environ.get("FOREMAN_LAND_STAMP") and not getattr(args, "json", False) and not isinstance(sys.stdout, _Stamped):
        sys.stdout, sys.stderr = _Stamped(sys.stdout), _Stamped(sys.stderr)


def cmd_land(p, args, main):
    from sampo import cli
    _stamp_output(args)
    if args.batch is not None and args.batch < 1:
        raise cli.UsageError("--batch takes a whole number of lanes per train, 1 or more")
    if getattr(args, "merged", False):  # T-4596: close the tasks of lanes already on the trunk; no train, no merge
        if args.batch is not None or getattr(args, "first", None) or getattr(args, "bg", False):
            raise cli.UsageError("sampo lane land --merged takes none of --batch, --first or --bg: it trains nothing")
        got = land_merged(p, main, drop_empty=getattr(args, "drop_empty", False))
        lines = [f"Land --merged: {len(got['closed']) + len(got['left'])} lane(s) already on the trunk: "
                 f"{len(got['closed'])} closed, {len(got['left'])} left active ({got['gate']})."
                 if got["closed"] or got["left"] else "Land --merged: no lane with an open task is on the trunk."
                 if not got["empty"] and not got["dropped"] else "Land --merged: no lane with landed work is on the trunk."]
        lines += [f"  closed {t}: {c.fit(c.plain(x), 160)}" for t, x in got.pop("lines")]
        lines += [f"  left {t}: {c.fit(c.plain(w), 400)}" for t, w in got["left"]]
        lines += [f"  dropped {t}: its lane was empty, the task was never built; the lane is removed and the task is back "
                  "in the queue, not closed" for t in got["dropped"]]
        lines += [f"  empty {t}: its lane is empty, the task was never built; it stays open"
                  + (f" ({w})" if w else " (sampo lane land --merged --drop-empty removes the lane and returns the task "
                                         "to the queue)") for t, w in got["empty"]]
        return cli.out(args, got, "\n".join(lines))
    if getattr(args, "drop_empty", False):
        raise cli.UsageError("sampo lane land --drop-empty goes with --merged: it removes the empty lanes that sweep finds")
    first = [x.upper() for x in (getattr(args, "first", None) or "").replace(",", " ").split() if x]
    if first:  # T-3796: ahead of the rest; into a running land at its next train boundary
        for t in first:
            cli.need_brief(p, t)
        first_ids(p, first)
        if landing(p):
            return cli.out(args, {"queued": first}, f"Land: {', '.join(first)} train next, at the running land's next "
                                                    "train boundary.")
    if getattr(args, "bg", False):  # T-3922: started detached, returned at once; a land takes hours
        if landing(p):
            return cli.out(args, {"land": "running"}, "A land is already running: it trains the finished lanes at its "
                                                      "next train boundary.")
        started = spawn_land(p, main)
        if started is None:
            return cli.out(args, {"land": "not started"}, "Land not started in the background (FOREMAN_NO_BACKGROUND or "
                                                          "paused): sampo lane land runs it here.")
        pid, log = started
        return cli.out(args, {"land": "started", "pid": pid, "log": log},
                       f"land started in the background (pid {pid}, log {log}); it trains the finished lanes.")
    t_land = time.time()
    got = land(p, main, args.batch)
    go = got["restart"] and not os.environ.get("FOREMAN_NO_BACKGROUND") and not c.panicked()
    wait = ""
    if got.get("ship_failed"):  # the newest request never started a ship: no record of an earlier one stands in for it
        wait = f"Ship not started: {c.fit(c.plain(got['ship_failed']), 300)} (the next land ships the head)"
        got["ship"] = wait
    elif got.get("ship_asked") and not got["restart"]:  # T-4239: the land's last ship, awaited (bounded) with the lock released
        secs = float(os.environ.get("FOREMAN_LAND_SHIP_WAIT_S") or 1800)  # SAMPO_LAND_SHIP_WAIT_S, mirrored by core
        if _ship_wait(p, secs):
            rec = ship_last(p)
            if rec and float(rec.get("start") or 0) >= t_land - 0.2:  # this land's ship (start is rounded to 0.1 s)
                got["ship_grant"], got["ship"] = rec["grant"], rec["line"]
                with contextlib.suppress(OSError):
                    c.write_atomic(os.path.join(p.dir, "ship-last.json"), json.dumps({**rec, "seen": True}))
            else:  # the ship died before its record, or it was never this land's: the earlier line is not its result
                wait = "Ship ended without a result (the next land ships the head)"
                got["ship"] = wait
        else:
            wait = f"ship still running: {os.path.join(p.dir, 'ship.log')}"
            got["ship"], got["ship_log"] = wait, os.path.join(p.dir, "ship.log")
    lines = [f"Land: {len(got['landed'])} lane(s) landed in {got['trains']} train(s): {len(got['closed'])} closed, "
             f"{len(got['hand'])} left for a hand close, {len(got['parked'])} parked."
             if got["landed"] or got["parked"] or got["stopped"] else "Land: no finished lane waits on a merge."]
    lines += [f"  Train {t['train']}: {len(t['lanes'])} lane(s), gate {t['gate']} min, waiting {t['waiting']} min"
              for t in got.get("train_times", [])]
    skipped = sum("no retry)" in w for _, w in got["hand"])
    lines += [f"  {skipped} refused close(s) not retried: brief and lane tip unchanged since the last land"] * bool(skipped)
    lines += [f"  closed {t}: {c.fit(c.plain(x), 160)}" for t, x in got.pop("lines")]
    lines += [f"  hand close {t}: {w} (then sampo focus {t}; sampo task finish {t})" for t, w in got["hand"]]
    lines += [f"  parked {t}: {w}" for t, w in got["parked"]]
    lines += [f"Stopped: {got['stopped']}"] if got["stopped"] else []
    lines += ([wait] if wait else [f"Ship ({got['ship_grant']}'s grant): {c.fit(c.plain(got['ship']), 300)}"]
              if got["ship_grant"] else [])
    lines += ["Restarting on new code: a train merged changes to Sampo's modules; the lanes still waiting land under them."
              if go else "The land restarts on new code: a train merged changes to Sampo's modules (run sampo lane land "
                         "again for the lanes still waiting)."] * bool(got["restart"])
    rv = cli.out(args, got, "\n".join(lines))
    if go:  # the lock is released: the same pid and log carry on under the new code (--first ids persist in first_ids)
        sys.stdout.flush()
        sys.stderr.flush()
        os.execv(sys.executable, [sys.executable, os.path.join(c.PLUGIN_ROOT, "bin", "sampo"), "lane", "land",
                                  *(["--batch", str(args.batch)] if args.batch else [])])
    return rv


def spawn_land(p, main):
    """T-3922: a detached `fm lane land` in its own session, its output appended to the state's land.log; never waited on.
    None when FOREMAN_NO_BACKGROUND is set (the tests') or sampo is paused (T-0591: paused, it waits)."""
    if os.environ.get("FOREMAN_NO_BACKGROUND") or c.panicked():
        return None
    log = os.path.join(p.dir, "land.log")
    keep = ("FM_POSTMERGE",)  # the operator's post-merge settings: the land's own process reads them (lanes.py)
    dropped = c.override_names(os.environ, keep)
    env = c.scrubbed_env(os.environ, keep)
    with open(log, "a", encoding="utf-8") as out:  # ponytail: the log only appends; rotate it when it grows
        if dropped:  # T-4289: the caller's test overrides never reach the land's gate; say which went
            out.write(f"{_hms()} sampo: dropped {', '.join(dropped)} from the land's environment: test overrides never reach a gate\n")  # stamped like the land's lines
        proc = subprocess.Popen([os.path.join(c.PLUGIN_ROOT, "bin", "fm"), "lane", "land"], cwd=main,
                                stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, start_new_session=True,
                                env={**env, "FOREMAN_LAND_STAMP": "1"})  # T-4239: every log line starts with its time
    return proc.pid, log


# ---------------------------------------------------------------- the check accept starts (T-4286)

CHECK_TRIES = 2  # a check cut off at its deadline is started once more (its green classes are cached), then reported
CHECK_WALL_S = 4 * 3600  # a check process still running after this is killed and counts as cut off
# T-4892: past CHECK_TRIES, a cut-off try that still ran classes is started again (run.py runs only what is left), up to
# this many tries and CHECK_WALL_S from the first one's start (check_first); a try that ran none is reported
CHECK_TRIES_MAX = 8
LANE_CHECKS_ENV = "SAMPO_LANE_CHECKS"
LANE_CHECKS_MAX = 64
TURN_POLL_S = 0.5  # T-4491: a queued check looks for a free slot this often


def lane_checks():
    """T-4491: how many lane checks run at once in a project (SAMPO_LANE_CHECKS, default 2, at least 1, at most
    LANE_CHECKS_MAX: each slot is an open lock file). A further one waits as 'check queued' until a running one ends."""
    try:
        return min(LANE_CHECKS_MAX, max(1, int(os.environ.get(LANE_CHECKS_ENV) or 2)))
    except ValueError:
        return 2
_SCRUBBED = ("FM_STALL", c.RUN_ID_ENV, "FOREMAN_TEST_RUN_ID")  # env prefixes a lane's check never inherits: a test's override, a nested run's id (the legacy name too)
_CHECK_KEYS = ("check_state", "check_tip", "check_pid", "check_ticks", "check_log", "check_tries", "check_failed",
               "check_queued", "check_solo", "check_shape",  # T-4563: the tip the repo-shape modules ran green at
               "check_first",  # T-4892: when this tip's first try started (the wall budget over all its tries)
               "check_code",  # T-4404: the checker's code stamp at the start
               "check_cut_timeout")  # T-4767: the --timeout a cut-off check's deadline line advised
BATCH_MAX = 8  # T-4528: lanes (the turn's own included) checked as one candidate; the rest wait their own turn


def _scrub_env(env):
    """T-4286: `env` without the FM_STALL-style overrides (a session that ran tests set them) and a run id."""
    return {k: v for k, v in env.items() if not k.startswith(_SCRUBBED)}


def _check_argv(b, since):
    """T-4286: the lane's own check: the affected tests plus the repo-shape classes, its evidence on the task, its
    result (exit, failed classes, cut_off) as JSON on stdout. `since`: a merge-commit tip's whole branch."""
    return [os.path.join(c.PLUGIN_ROOT, "bin", "sampo"), "check", "--affected", "--hold", "--evidence", b.id, "--json",
            *(["--since", since] if since else [])]


def _lane_tip(main, b):
    return _git(main, "rev-parse", "--verify", "-q",
                f"{b.meta.get('lane_branch') or 'foreman/' + b.id}^{{commit}}").stdout.strip()


def _spawn_check(p, b, main, *, tip, since, tries):
    """T-4286: the detached, niced process that runs the lane's check (see _accept_check_child) in its own session,
    its output in the state's check/ID.log; never waited on. (pid, log), or None when FOREMAN_NO_BACKGROUND is set
    (the tests') or sampo is paused. The one function the tests replace: none starts a real check."""
    if os.environ.get("FOREMAN_NO_BACKGROUND") or c.panicked():
        return None
    d = os.path.join(p.dir, "check")
    os.makedirs(d, exist_ok=True)
    log = os.path.join(d, f"{b.id}.log")
    arg = json.dumps({"accept": {"project": [p.slug, p.root, p.dir, p.lane], "main": main, "id": b.id, "tip": tip,
                                 "tries": tries, "since": since, "argv": _check_argv(b, since)}})
    lib = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = _scrub_env({**os.environ, "PYTHONPATH": lib + os.pathsep + os.environ.get("PYTHONPATH", "")})
    with open(log, "w", encoding="utf-8") as out:  # one start's output: a retry replaces it
        proc = subprocess.Popen(c.nice_argv([sys.executable, "-m", "sampo.train", arg]), cwd=b.meta.get("lane") or main,
                                stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, start_new_session=True,
                                env=env)
    return proc.pid, log


def _affected_state(b, sha, main):
    """T-4286: ('none' | 'stale' | 'fresh', the newest affected run's evidence line): a lane with no run, one whose
    newest run came before its own last commit (a merge of main's branch is not its work), one with a run at its tip.
    T-3963's stale test, and what sampo lane accept starts a check on."""
    runs = [(t, l) for l in b.evidence() if "--affected (affected tests only): " in l  # the label, in its sampo and older fm forms
            and (m := c._TS_TAIL.search(l)) and (t := c.parse_ts(m.group(1)))]
    if not runs or not sha:
        return "none", ""
    t, line = max(runs, key=lambda x: x[0])
    last = _git(main, "log", "-1", "--no-merges", "--format=%ct", sha).stdout.strip()
    return ("stale" if last and t.timestamp() < float(last) else "fresh"), line


def _held_classes(line):
    """T-4286: whether an affected run's evidence line names every repo-shape class in its command (a --hold run's does)."""
    cmd = line.split("` → ", 1)[0]
    return all(n in cmd for n in c.repo_shape_classes())


def _run_passed(line):
    """T-4286: whether an affected run's evidence line is a passing one (a failed run's result starts with the cross)."""
    return not line.partition("` → ")[2].startswith("✗")


def _clean_run(b, sha, main):
    """T-4286, the one rule for what lets a lane skip its check: its newest affected run is at its tip (not before its
    own last commit), passed, and its command held the repo-shape classes, or the repo-shape modules ran green at this
    tip (T-4563: check_shape, which the modules hold the classes by). A failing line (a red check records one too), or a
    run that held neither, is not it. Accept and the land's hold both read this."""
    state, line = _affected_state(b, sha, main)
    return state == "fresh" and _run_passed(line) and (_held_classes(line) or b.meta.get("check_shape") == sha)


def _check_alive(m):
    """T-4286: whether the check process a lane's meta names still runs (its pid and start time: a reused pid is not it)."""
    pid, ticks = m.get("check_pid"), m.get("check_ticks")
    return bool(pid) and (_ticks(pid) == int(ticks) if ticks else _ticks(pid) > 0)  # meta holds strings


def _restartable(m):
    """T-4286: a check that ended without a result, with tries left: sampo lane accept starts it again."""
    return m.get("check_state") == "running" and not _check_alive(m) and int(m.get("check_tries") or 1) < CHECK_TRIES


CHECKER_FILES = (("lib", "sampo", "cli.py"), ("lib", "sampo", "train.py"), ("lib", "sampo", "core.py"),
                 ("lib", "sampo", "map.py"))  # T-4404: the trunk's code a check runs (cli.affected picks tests through map.py;
# core.py has run_command, run_record, REPO_SHAPE, check_claim, kill_tree). Not tests/run.py: the check runs the lane's own.
RESTART_MAX = 2  # T-4404: explicit accept restarts of a held check at one tip, then it is run by hand in the lane
_STAMPS = {}  # trunk plugin root -> ((mtime_ns, size) of each file, the digest): a file read once until it changes


def _trunk_plugin(main):
    """The trunk's plugin folder: the `main` checkout's own when it has one, else this process's. A lane_reexec'd caller
    (accept typed inside a lane) has the lane's PLUGIN_ROOT, which must not flip the stamp."""
    mine = os.path.join(main or "", "plugin")
    return mine if main and os.path.isfile(os.path.join(mine, *CHECKER_FILES[0])) else c.PLUGIN_ROOT


def _checker_stamp(main=None):
    """T-4404: a short hash of the trunk's checker code (CHECKER_FILES, by content: a checkout's mtimes move with every
    trunk commit, a commit that leaves these alone must not look like a fix). Recorded by the check's process before its
    run; a red check is worth running again only when it differs, since the same code gives the same answer. Memoized
    by each file's (mtime_ns, size): about 1.2 MB is read once per change, not per call."""
    root = _trunk_plugin(main)
    paths = [os.path.join(root, *parts) for parts in CHECKER_FILES]
    sig = []
    for path in paths:
        try:
            s = os.stat(path)
            sig.append((s.st_mtime_ns, s.st_size))
        except OSError:
            sig.append(None)
    hit = _STAMPS.get(root)
    if hit and hit[0] == sig:
        return hit[1]
    h = hashlib.sha1(usedforsecurity=False)  # a content fingerprint to compare two stamps, not a security control
    for path in paths:
        with contextlib.suppress(OSError), open(path, "rb") as f:
            h.update(f.read())
        h.update(b"\0")
    _STAMPS[root] = (sig, h.hexdigest()[:12])
    return _STAMPS[root][1]


def _rerun_reason(m, main=None):
    """T-4404: why an explicit accept may start a held check again (as try 1; the land never does), else None: cut off or
    without a result twice (a process that died on its second try too: a reboot, an OOM kill), or red with the checker's
    code differing from the stamp recorded when it ran (none recorded: it ran before the stamp existed)."""
    st = m.get("check_state")
    if st == "cut_off":
        return "its check was cut off or ended without a result twice"
    if st == "running" and not _check_alive(m) and int(m.get("check_tries") or 1) >= CHECK_TRIES:
        return "its check ended without a result twice"
    if st == "red" and m.get("check_code") != _checker_stamp(main):
        return f"it was red: {m.get('check_failed') or 'no failing class recorded'}; the checker's code has changed since"
    return None


def _restart_count(m, tip):
    """T-4404: the explicit restarts of this lane's held check at `tip`: check_restarts is "<tip>:<n>", so a record wipe
    (a paused start's give-back, a discarded run) that drops check_tip does not reset the cap, and another tip's is 0."""
    head, _, n = str(m.get("check_restarts") or "").rpartition(":")
    return int(n) if head == tip and n.isdigit() else 0


def _rerunnable(m, main=None):
    return _rerun_reason(m, main) is not None


def _check_hold(b, sha, main):
    """T-4286: why a lane waits on its check (running, red, cut off) at tip `sha`, else None: a check that never ran, or
    one for an older tip, holds nothing, and a newer green run at the tip that held the repo-shape classes (_clean_run)
    clears a red, cut-off or dead check. One rule: the land leaves such a lane out and accept says the same."""
    m = b.meta
    st = m.get("check_state")
    if not st or m.get("check_tip") != sha or st == "green":
        return None
    if st == "running" and _check_alive(m):
        if m.get("check_queued"):  # T-4491: its process waits for one of the project's lane_checks() slots
            return (f"held: check queued behind {lane_checks()} running at most (pid {m.get('check_pid')}); it starts "
                    "when one ends and joins the land when green")
        return f"held: check running (pid {m.get('check_pid')}); it joins the land when green"
    if _clean_run(b, sha, main):
        return None
    mine = f"sampo check --affected --hold --evidence {b.id}"
    n = int(m.get("check_tries") or CHECK_TRIES)  # T-4892: tries that still ran classes went on past two
    times = "twice" if n == 2 else f"{n}x"
    seen = f" (its output: {m['check_log']})" if m.get("check_log") else ""  # T-4851: what it printed before it ended
    if st == "running":
        return (f"held: its check ended without a result: sampo lane accept {b.id} starts it again" if _restartable(m) else
                f"held: its check ended without a result {times}: sampo lane accept {b.id} starts it again (or run {mine} "
                f"in the lane){seen}")
    if st == "red":
        if _rerunnable(m, main):  # T-4404: the checker's code is not the code that said red
            return (f"held: check red: {m.get('check_failed') or 'no failing class recorded'}; the checker's code has "
                    f"changed since it ran: sampo lane accept {b.id} starts it again")
        return (f"held: check red: {m.get('check_failed') or 'no failing class recorded'}; left out, no retry until the "
                f"tip moves or a newer green run holds the repo-shape classes ({mine} in the lane)")
    big = m.get("check_cut_timeout") or 1200  # T-4767: the hand run's way back is a larger limit than the one that cut it
    return (f"held: its check was cut off or ended without a result {times}: sampo lane accept {b.id} starts it again "
            f"(T-4404), or run sampo check --affected --timeout {big} --hold --evidence {b.id} in the lane, in the "
            f"background (a foreground run is cut off at about 10 minutes){seen}")


def _unchecked(b, sha, main):
    """T-4767: why the land leaves a lane out for having no check at its tip, else None. Train 1115k took four lanes that
    had no check on record at all (_check_hold holds 'a check that never ran' nothing), each carried a red its builder
    never saw, and the gate ran 100 minutes for nothing. A lane trains when its check is green at this tip or its
    newest affected run is a clean one (_clean_run: at the tip, passed, held the repo-shape classes); any other is held
    with the reason and the two ways to a check, the lane's own accept and the run by hand."""
    m = b.meta
    if (m.get("check_state") == "green" and m.get("check_tip") == sha) or _clean_run(b, sha, main):
        return None
    state, line = _affected_state(b, sha, main)
    why = ("it has no affected run on record" if state == "none" else
           "its newest affected run is older than its last commit" if state == "stale" else
           "its newest affected run failed" if not _run_passed(line) else
           "its newest affected run did not hold the repo-shape classes")
    return (f"held: no passing affected check at its tip ({why}): run sampo check --affected --hold --evidence {b.id} "
            f"in the lane, or sampo lane accept {b.id} --audit LENS HOW RESULT; it joins the land when its check is green")


def check_notes(briefs, main, ids=None):
    """T-4286: {task id: 'checking' | 'check queued' | 'check red: CLASSES' | 'check cut off: accept again' | 'check ended:
    accept again'} for the lanes whose own check holds them at their current tip (sampo lane list, sampo next): one git call
    per lane that has a check on record. T-4404: a red one says `accept again` when the checker's code changed since it ran
    (the stamp is hashed only for a red one, and memoized by file stat)."""
    out = {}
    for b in briefs:
        if (ids is not None and b.id not in ids) or b.meta.get("check_state") in (None, "", "green"):
            continue
        why = _check_hold(b, _lane_tip(main, b), main)
        if why:
            st = b.meta["check_state"]
            out[b.id] = ("check queued" if st == "running" and _check_alive(b.meta) and b.meta.get("check_queued") else
                         "checking" if st == "running" and _check_alive(b.meta) else
                         f"check red: {b.meta.get('check_failed') or '?'}"
                         + (" (checker changed: accept again)" if st == "red" and _rerunnable(b.meta, main) else "")
                         if st == "red" else
                         "check cut off: accept again" if st == "cut_off" else
                         "check ended: accept again" if _restartable(b.meta) or _rerunnable(b.meta, main) else
                         "check ended: run it in the lane")
    return out


def stop_check(b):
    """T-4286: end the check process a lane's meta names (sampo lane rm): SIGTERM to that pid alone, once its start time
    says it is still that process; its handler (_run_check) ends the check's own session first. Whether one was signalled."""
    m = b.meta
    if not _check_alive(m) or str(m.get("check_pid")) == str(os.getpid()):  # never this process
        return False
    with contextlib.suppress(OSError, ValueError):
        os.kill(int(m["check_pid"]), signal.SIGTERM)
        return True
    return False


def _add_audit_once(x, lens, how, result, tree):
    """T-4286: one audit row per (lens, how, result, tree): accepting the same review twice records it once. Whether a
    row was added. The text is cleaned the way Brief.add_audit cleans it, so the comparison is on what is stored."""
    how_s = c.defang(c._unmarked(c.redact(str(how)).replace("`", "'").strip()))
    res_s = c.defang(c._unmarked(c.redact(str(result)).replace("\n", " ").strip()))
    head, mark = f"- (audit {lens}) `{how_s}` → {res_s}", (f" [tree {tree}]" if tree else "")
    for line in x.evidence():
        if line.startswith(head) and (line[len(head):].startswith(mark + " (") if mark else
                                      not line[len(head):].startswith(" [tree ")):
            return False
    x.add_audit(lens, how, result, tree=tree)
    return True


def _queue(p, main, tid):
    """T-3922: the lane queued first: ('running', None) into a running land at its next train boundary; else a detached
    land starts and trains it first, ('started', (pid, log)), or ('not started', None) in the tests and when paused.
    T-4528: `tid` may be a list (a batch's members, in order): one call, so one land is started for all of them."""
    first_ids(p, [tid] if isinstance(tid, str) else list(tid))
    if landing(p):
        return "running", None
    started = spawn_land(p, main)  # a land takes hours: never waited on
    return ("started", started) if started else ("not started", None)


def _set_check(p, tid, note=None, **meta):
    """T-4286: the lane's check record in its task's meta (flat scalars; None removes one), `note` on its Log."""
    def go(x):
        for k, v in meta.items():
            if v is None:
                x.meta.pop(k, None)
            else:
                x.meta[k] = v
        if note:
            x.append_log(note)
    from sampo import cli
    cli.mutate(p, tid, go, "check", {k: v for k, v in meta.items() if k == "check_state"} | ({"note": note[:200]} if note else {}))


class _ClaimLost(Exception):
    """Another process claimed the lane's check between what the caller read and its own claim."""

    def __init__(self, m):
        super().__init__(f"check is running (pid {m.get('check_pid')}, log {m.get('check_log')})")
        self.meta = dict(m)


def _check_seen(m):
    """What a caller saw of a lane's check: (state, tip, pid) as strings (meta holds strings once saved)."""
    return tuple(str(m.get(k) or "") for k in ("check_state", "check_tip", "check_pid"))


def _start_check(p, b, main, tip, since, tries, restarts=None):
    """T-4286: start the lane's check (try number `tries`) and record it on the task; the started pid and log, or None.
    The claim comes first, inside the project lock: check_state=running with this process's own pid, only if the task's
    check is still what `b` showed (else _ClaimLost: another accept or the check's own retry got there), so two accepts
    at once start one check. The spawn follows; a start that does not happen (or raises) gives the claim back.
    T-4404: `restarts` (an explicit accept's restart of a held check, the count at this tip) is kept in check_restarts as
    "<tip>:<n>", outside _CHECK_KEYS so it survives a restart's own clearing, and counts only for that tip (a record
    wipe that drops check_tip does not reset it: _restart_count). A start that does not happen writes the record back
    as it was (nothing, for a first start), so a held lane stays held: a failed try-2 start used to wipe check_tip and
    the cut-off written after it had none."""
    from sampo import cli
    seen, me, prior = _check_seen(b.meta), os.getpid(), {}

    def claim(x):
        if _check_seen(x.meta) != seen:
            raise _ClaimLost(x.meta)
        prior.update({k: x.meta.get(k) for k in (*_CHECK_KEYS, "check_restarts")})
        x.meta.update(check_state="running", check_tip=tip, check_pid=me, check_ticks=_ticks(me), check_tries=tries,
                      check_first=x.meta.get("check_first") if tries > 1 and x.meta.get("check_first") else
                      round(time.time()))  # T-4892: a retry keeps the first try's start
        for k in ("check_failed", "check_log", "check_queued"):
            x.meta.pop(k, None)
        if restarts is not None:
            x.meta["check_restarts"] = f"{tip}:{restarts}"
        elif _restart_count(x.meta, tip) == 0:
            x.meta.pop("check_restarts", None)  # another tip's count: gone

    def give_back():
        _set_check(p, b.id, **prior)
    cli.mutate(p, b.id, claim, "check", {"check_state": "running"})
    try:
        started = _spawn_check(p, b, main, tip=tip, since=since, tries=tries)
    except BaseException:
        give_back()
        raise
    if not started:
        give_back()
        return None
    pid, log = started
    _set_check(p, b.id, f"check started on {tip[:9]} (pid {pid}, try {tries}, log {log})", check_pid=pid,
               check_ticks=_ticks(pid), check_log=log)
    return started


def _result_of(r):
    """The result the lane's `sampo check --affected --json` printed, or None: the last JSON object in the output that
    starts a line and carries "exit". T-4418: cli.out prints it over many lines (indent 2) and this read one line, so
    from T-4286's landing no check ever had a result: each ran its suite twice and ended 'cut off 2x at its
    deadline' (10 of 10 on 2026-11-11). Decoded from where it starts, so other output before and after it (the
    lane hand-over's line, a hook's) is passed over, and an object of a test's own is not taken for it."""
    text, decoder = r.stdout or "", json.JSONDecoder()
    for m in reversed(list(re.finditer(r"(?m)^\{", text))):
        try:
            got, _ = decoder.raw_decode(text, m.start())
        except ValueError:
            continue
        if isinstance(got, dict) and "exit" in got:
            return got
    return None


def _run_check(argv, cwd):
    """(CompletedProcess, timed_out): the check in its own session, killed whole past CHECK_WALL_S. T-4410: with
    SAMPO_LANE_CHILD set, so the trunk's sampo checks the lane's tree itself (py.lane_reexec would hand over to the
    lane's own copy, which may not know --hold or may carry a check bug the trunk has fixed); the tests it runs are
    the lane's either way."""
    # T-4514: SAMPO_CHECK_LOG is the file this process writes to, which a second check of the kind is told to read
    log = c.stdout_log()
    env = {**_scrub_env(os.environ), "SAMPO_LANE_CHILD": "1"}
    if log:
        env["SAMPO_CHECK_LOG"] = log
    proc = subprocess.Popen(argv, cwd=cwd, env=env,
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, errors="replace", start_new_session=True)
    def stopped(signum, frame):  # sampo lane rm (stop_check): everything the check started ends with this process
        c.kill_tree(proc.pid)  # T-4417: its commands and their test classes run in sessions of their own
        os._exit(128 + signum)
    try:
        before = signal.signal(signal.SIGTERM, stopped)
    except ValueError:  # not the main thread: no handler, the deadline still ends it
        before = None
    # T-4851: the check's lines are copied to this process's stdout (the log 'check started ... log ...' names) as they
    # arrive, and kept whole for _result_of: a check cut off at its deadline leaves what it printed before the kill
    chunks, to_log = [], [True]

    def pump():
        for line in iter(proc.stdout.readline, ""):
            chunks.append(line)
            if to_log[0]:
                try:
                    sys.stdout.write(line)
                    sys.stdout.flush()
                except (OSError, ValueError):  # a log that cannot be written (disk full, closed) must not stall the check's pipe
                    to_log[0] = False
    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    try:
        start = time.monotonic()
        proc.wait(timeout=CHECK_WALL_S)
        reader.join(max(CHECK_WALL_S - (time.monotonic() - start), 0))  # a survivor holding the pipe open past the deadline
        if reader.is_alive():
            raise subprocess.TimeoutExpired(argv, CHECK_WALL_S)
        return subprocess.CompletedProcess(argv, proc.returncode, "".join(chunks), ""), False
    except subprocess.TimeoutExpired:
        c.kill_tree(proc.pid)  # T-4417: the whole tree, or a survivor keeps its test slots and this pipe open
        reader.join(30)  # something outside the tree holding the pipe: what came before is kept, the rest not waited for
        to_log[0] = False
        return subprocess.CompletedProcess(argv, 124, "".join(chunks), ""), True
    finally:
        if before is not None:
            signal.signal(signal.SIGTERM, before)
        if not reader.is_alive():
            proc.stdout.close()


def _take_slot(slots):
    """T-4491: an flock on the first free slot of `slots` (open files), True; False when every one is held."""
    for f in slots:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            continue
    return False


@contextlib.contextmanager
def _check_turn(p, waits=None, started=None):
    """T-4416, T-4491: at most lane_checks() lane checks at a time in a project. Five started together (2026-11-11: 1,275
    test modules for 16 slots) were each still running after 15 to 25 minutes and a 59-module one was cut off at its
    run's deadline; in turn the work is the same and the first result comes after one run. One flock per slot
    (check/turn-N.lock), held by the check's own process around its run and released by the kernel however that process
    ends. A free slot is taken at once. With none free, `waits()` once (the task is told: check queued), then the process
    polls for one every TURN_POLL_S, with no session open; `started()` once it has one, if it waited. No signal handler
    is set here, so the SIGTERM of sampo lane rm (stop_check) ends a waiting one. The land's gate and ship do not take it."""
    d = os.path.join(p.dir, "check")
    os.makedirs(d, exist_ok=True)
    slots = [open(os.path.join(d, f"turn-{i}.lock"), "a") for i in range(lane_checks())]
    try:
        waited = False
        while not _take_slot(slots):
            if not waited:
                waited = True
                if waits:
                    waits()
            time.sleep(TURN_POLL_S)
        if waited and started:
            started()
        yield
    finally:
        for f in slots:
            f.close()


def _outcome(r, timed_out):
    """T-4528: ('green' | 'red' | 'cut', the check's JSON or None) for a finished `sampo check --affected --json`: green
    is an exit 0 with a result inside the deadline; red is a result that names failures; anything else (the deadline, a
    kill, a crash, no result at all) is cut. One reading for a lane's own check and for a batch's."""
    got = _result_of(r)
    if got is not None and r.returncode == 0 and not timed_out:
        return "green", got
    if got is not None and not got.get("cut_off") and not timed_out:
        return "red", got
    return "cut", got


def _batch_peers(p, main, me, tip):
    """T-4528: the lanes whose check waits its turn behind this one, as (brief, tip), oldest task first, at most
    BATCH_MAX - 1: its process alive and queued (T-4491: with several slots, a check running in another one is no peer:
    marking it green mid-run would send it to a redundant solo check), claimed at the lane's current tip on its first
    try, and not marked for a solo run
    (a batch that went red or was cut off marks its members). T-4563: and whose repo-shape modules ran green at that tip
    (_shape_clear: a batch's run holds the classes, not the other modules). One git call per lane that passes the task's
    own record."""
    out = []
    for x in c.load_briefs(p):
        m = x.meta
        if (x.id == me or m.get("check_state") != "running" or not m.get("check_queued") or not _check_alive(m)
                or int(m.get("check_tries") or 1) != 1):
            continue
        t = _lane_tip(main, x)
        if t and m.get("check_tip") == t and m.get("check_solo") != t and _shape_clear(x, t, main):
            out.append((x, t))
    return sorted(out, key=lambda r: c.id_num(r[0].id))[:max(BATCH_MAX - 1, 0)]


def _batch_argv(base):
    """T-4528: the candidate's check: the lane check's command over everything changed since the trunk it sits on, and
    no --evidence (the run is no one lane's own)."""
    return [os.path.join(c.PLUGIN_ROOT, "bin", "sampo"), "check", "--affected", "--hold", "--json", "--since", base]


def _mark_members(p, main, members, note, solo=False, **meta):
    """T-4528: write `meta` (None removes a key), `check_solo` = the member's tip when `solo`, and `note` on each member
    (brief, tip) whose check still waits at that tip, and only those: a lane whose tip moved or whose check ended
    meanwhile keeps whatever it has. The ids written."""
    from sampo import cli
    wrote = []
    for x, t in members:
        def go(y, t=t):
            if y.meta.get("check_state") != "running" or y.meta.get("check_tip") != t or _lane_tip(main, y) != t:
                return
            for k, v in meta.items():
                if v is None:
                    y.meta.pop(k, None)
                else:
                    y.meta[k] = v
            if solo:
                y.meta["check_solo"] = t
            y.append_log(note.replace("{tip}", t[:9]))
            wrote.append(y.id)
        cli.mutate(p, x.id, go, "check", {k: v for k, v in meta.items() if k == "check_state"})
    return wrote


def _batch_check(p, main, b, tip, peers, run=None):
    """T-4528: this lane and the lanes waiting behind it as one candidate: a scratch copy of the trunk with their branches
    merged (a lane that conflicts with the others is left out, as the train leaves it; this lane conflicting means no
    batch), one affected run over the union of their changes. Green: every member that still waits at its tip is marked
    green and queued, in order; the ids covered. Red or cut off: nothing is written for the lanes but a solo mark, so
    each takes its own turn and a red lane is named by its own check (the cost of a miss is this one run). [] when no
    batch ran (a single lane, a conflict with the trunk): the caller checks this lane alone."""
    run = run or _run_check
    base = _git(main, "rev-parse", "HEAD").stdout.strip()
    if not base:
        return []
    try:  # T-4728: a tree removed under the batch is rebuilt once; lost twice, no batch: each lane is checked alone
        return _rebuilding(p, "the batch check", lambda: _batch_once(p, main, b, tip, peers, run, base))
    except c.PolicyError as e:
        print(f"sampo lane accept: {e}", file=sys.stderr)
        return []


def _batch_once(p, main, b, tip, peers, run, base):
    """_batch_check's work on one scratch tree; raises _Lost when something else removes the tree."""
    wt = c.scratch_tree("sampo-batch-", main)  # T-4735
    try:
        if _git(main, "worktree", "add", "-q", "--detach", wt, base).returncode:
            return []
        merged, log = [], []
        for x, t in [(b, tip), *peers]:
            _lost_check(wt)
            if _merge_in(wt, x, t, log, kept={}):
                merged.append((x, t))
            elif x.id == b.id:
                _lost_check(wt)
                return []
        if len(merged) < 2:
            return []
        got = run(_batch_argv(base), wt)
        _lost_check(wt)  # a run in a vanished folder is no red batch
        what, _ = _outcome(*got)
        if what != "green":
            _mark_members(p, main, merged, f"batch check of {len(merged)} lanes was {'red' if what == 'red' else 'cut off'} "
                                           "on {tip}: each lane is checked alone in turn", solo=True)
            return []
        wrote = _mark_members(p, main, merged, f"check green on {{tip}} as one of a batch of {len(merged)} lanes checked "
                                               f"together on {base[:9]} (one run)", check_state="green", check_pid=None,
                              check_ticks=None, check_failed=None, check_solo=None, check_queued=None,
                              accepted_at=round(time.time(), 1))
        if wrote:
            _queue(p, main, wrote)
        return wrote
    finally:
        _git(main, "worktree", "remove", "--force", wt)
        shutil.rmtree(wt, ignore_errors=True)
        c.scratch_release(wt)
        _git(main, "worktree", "prune")


def _went_on(got, m, tries):
    """T-4892: whether cut-off try `tries` earns one more past CHECK_TRIES: its check's progress record (cli's
    {classes, not_run}) says it ran at least one class, so the next try, run.py's green classes cached, starts further
    on; within CHECK_TRIES_MAX tries and CHECK_WALL_S of the first try's start (check_first). No record: no."""
    prog = (got or {}).get("progress") or {}
    try:
        ran = int(prog["classes"]) - int(prog["not_run"])
        first = float(m.get("check_first") or 0)
    except (KeyError, TypeError, ValueError):
        return False
    return ran > 0 and tries < CHECK_TRIES_MAX and 0 < first and time.time() - first < CHECK_WALL_S


def _accept_check_child(a):
    """T-4286: the lane's check process: run the check in the lane, then act on its result. Green: the lane is queued
    first, the same call accept makes (a land not running is started). Red: left out, its failing classes (from the
    run's record, via the check's JSON) on the task and no retry until the tip moves. Cut off (the deadline, a kill, no
    result at all): started once more, then reported. A lane whose tip moved meanwhile is judged by none of it.
    T-4528: taking the turn, a lane whose check a batch already marked green at its tip runs nothing, and one with
    other lanes waiting behind it checks them with itself as one candidate (_batch_check); a batch that did not go
    green falls through to this lane's own check."""
    from sampo import cli
    p = c.Project(*a["project"])
    b = cli.need_brief(p, a["id"])
    main, tip, tries = a["main"], a["tip"], int(a.get("tries") or 1)
    if tries == 1 and _shape_wanted(b, main, tip):  # T-4563: the modules first, before the turn: lanes run theirs together
        verdict = _shape_stage(p, b, main, tip)
        if verdict == "red":
            return 1  # refused at its tip: recorded on the lane, and no long check runs
        if verdict == "moved":
            return 0
    r, timed_out = None, False
    with _check_turn(p, lambda: _set_check(p, b.id, f"check queued: {lane_checks()} lane check(s) run at once in this "
                                                    "project (T-4491); it starts when one ends", check_queued="1"),
                     lambda: _set_check(p, b.id, check_queued=None)):
        cur = cli.need_brief(p, b.id)
        if _lane_tip(main, cur) == tip:  # a tip that moved during the wait: no suite for nothing
            if cur.meta.get("check_state") == "green" and cur.meta.get("check_tip") == tip:
                return 0  # T-4528: a batch checked it while it waited, and queued it
            if tries == 1 and cur.meta.get("check_solo") != tip and _shape_clear(cur, tip, main):
                try:
                    peers = _batch_peers(p, main, b.id, tip)
                    if peers and b.id in _batch_check(p, main, cur, tip, peers):
                        return 0
                except Exception as e:  # a batch that cannot be built costs nothing: the lane's own check follows
                    print(f"batch check skipped: {type(e).__name__}: {c.redact(str(e))[:200]}", file=sys.stderr)
            # T-4563: the modules ran green at this tip (they hold the classes): the hold check runs the rest without them
            argv = [*a["argv"], "--shape-green"] if cur.meta.get("check_shape") == tip else a["argv"]
            # T-4404: the code this run starts with, taken after the wait for a slot: a red one is held against this stamp
            _set_check(p, b.id, check_code=_checker_stamp(main))
            r, timed_out = _run_check(argv, b.meta.get("lane") or main)
    got = _result_of(r) if r is not None else None
    b = cli.need_brief(p, b.id)
    if r is None or _lane_tip(main, b) != tip:
        _set_check(p, b.id, f"check on {tip[:9]} discarded: the lane tip moved; sampo lane accept {b.id} checks the new one",
                   **dict.fromkeys(_CHECK_KEYS))  # every key: a leftover tries or failed list would mislead the next one
        return 0
    what, got = _outcome(r, timed_out)
    if what == "green":
        _set_check(p, b.id, f"check green on {tip[:9]}", check_state="green", check_pid=None, check_ticks=None,
                   check_failed=None, check_queued=None, accepted_at=round(time.time(), 1))
        _queue(p, main, b.id)
        return 0
    if what == "red":
        failed = " ".join(str(x) for x in got.get("failed") or [])[:500]
        _set_check(p, b.id, f"its lane's check is red: {failed or 'no failing class recorded'} (tip {tip[:9]}; no retry "
                            "until the tip moves)", check_state="red", check_failed=failed, check_pid=None, check_ticks=None,
                   check_shape=None, check_queued=None)  # T-4563: a red check no longer rests on the shape modules' green
        return 1
    try:
        if (tries < CHECK_TRIES or _went_on(got, b.meta, tries)) and _start_check(p, b, main, tip, a.get("since"), tries + 1):
            return 1
    except _ClaimLost:  # another process started this lane's check meanwhile: it reports
        return 1
    rerun = None  # T-4767: the --timeout the deadline line advised, kept for the hold line (a hand run is the way back)
    if got is None and not timed_out:  # T-4410: it ended on its own (a usage error, a crash): nothing was cut off
        last = next((x.strip() for x in reversed((r.stdout or "").splitlines()) if x.strip()), "no output")
        what = f"check gave no result {tries}x on {tip[:9]} (exit {r.returncode}): {c.redact(last)[:200]}"
        tail = f"run sampo check --affected --hold --evidence {b.id} in the lane"
    else:  # T-4491: the deadline line cli's check printed (its limit, the classes that ran, the larger rerun), when it did
        dl = c.redact(str((got or {}).get("deadline") or ""))[:400]
        what = f"check cut off {tries}x on {tip[:9]}" if dl else f"check cut off {tries}x at its deadline on {tip[:9]}"
        tail = dl or f"run sampo check --affected --hold --evidence {b.id} in the lane"
        rerun = (re.search(r"--timeout (\d+(?:\.\d+)?)\b", dl) or [None, None])[1]
    log = b.meta.get("check_log")  # T-4851: the check's lines, as far as it got
    _set_check(p, b.id, f"{what}: {tail}" + (f" (its output: {log})" if log else ""), check_state="cut_off",
               check_pid=None, check_ticks=None, check_queued=None, check_cut_timeout=rerun)
    return 1


# ---------------------------------------------------------------- sampo lane accept (T-3922)

AUDIT_FORM = "--audit LENS HOW RESULT"


def _audit_rows(args):
    """T-4391: the audits typed to `lane accept`, every one read before anything is recorded or started. Python 3.14's
    argparse lets an option taking three values take the next option as its last value when one is short, so
    `--audit intent TEXT --audit adversary TEXT` was one audit whose RESULT was "--audit"; the words after it fell into the
    command's task-id list, and the second audit was dropped without a word. A refused audit names the lens it could
    not read and shows the form; a known lens, a non-empty HOW and RESULT, and no value that is one of the command's own
    options are all it takes."""
    from sampo import cli
    rows = [tuple(x) for x in (getattr(args, "audit", None) or [])]
    if not rows:
        raise cli.UsageError(f"sampo lane accept needs {AUDIT_FORM} (one per lens reviewed, three values each)")
    lane = next(a for a in cli.build_parser()._actions if isinstance(a, cli.argparse._SubParsersAction)).choices["lane"]
    flags = set(lane._option_string_actions)
    form = f"{AUDIT_FORM}: a lens ({', '.join(c.AUDIT_LENSES)}), then how it was done, then the result, each one word or quoted"
    for lens, how, result in rows:
        for name, value in (("HOW", how), ("RESULT", result)):
            if value.startswith("--") and value.partition("=")[0] in flags:
                raise cli.UsageError(f"could not read the {lens} audit: its {name} is the option {value} (an audit takes "
                                     f"three values, repeat --audit for each lens); {form}")
            if not value.strip():
                raise cli.UsageError(f"could not read the {lens} audit: its {name} is empty; {form}")
    bad = sorted({lens for lens, _, _ in rows if lens not in c.AUDIT_LENSES})
    if bad:
        raise cli.UsageError(f"unknown lens {', '.join(bad)}; {form}")
    more = list(getattr(args, "more", None) or [])
    if more:  # words after the audits that no audit took: a dropped audit's, never a task id (accept takes one lane)
        raise cli.UsageError(f"could not read {', '.join(repr(w) for w in more)} after the {rows[-1][0]} audit; {form}")
    return rows


def _accept_gaps(b, path, main, rows):
    """T-4487: what the close would refuse for the lane at `path` once these audits (rows) are on its brief: the lens
    groups its tier needs (cli._audit_gaps, the close's own rule), and adversary when the lane's own diff is
    security-sensitive (the close's _adversary_gaps). Judged against the lane's tree, before anything is recorded."""
    from sampo import cli
    tree = c.worktree_id(path)
    base = c.task_base(main, b)  # the close reads the same start point: a task with none is judged on its audits alone
    names = c.task_diff(path, base, "--name-only", "--no-renames") if base and tree else None
    if not base:
        risky = []
    elif names is None:  # unreadable: counted as sensitive, as the close does
        risky = ["diff unavailable, so its content wasn't checked"]
    else:
        risky = c.sensitive(sorted(set(names.splitlines())), c.task_diff(path, base) or "")
    gaps = cli._audit_gaps(b, [(lens, how) for lens, how, _ in rows], b.audit_blockers(tree=tree))
    if risky and not any(lens == "adversary" for lens, _, _ in rows):
        gaps += [r + cli._adversary_ask(risky) for r in b.audit_blockers(tree=tree, need=("adversary",))
                 if r.startswith("audit missing: adversary")]
    return gaps


SHAPE_WALL_S = 900  # T-4542: the repo-shape modules in a lane before its long check; past it, no verdict (the check runs them)


def _shape_tree(path):
    """T-4542: whether a lane's tree is a Sampo tree with the repo-shape modules (another project's is not held to them)."""
    return os.path.isfile(os.path.join(path, "plugin", "tests", "run.py"))


def _shape_run(path):
    """T-4542: ('green' | 'red' | 'cut', the failing classes named) for the repo-shape modules run once in the lane's tree,
    niced. Red names the classes its run record carries (else the classes its output names). Cut is no verdict (the
    deadline, a run that left no record and named no class): the lane's own check runs the same classes with --hold."""
    cmd = "python3 plugin/tests/run.py " + " ".join(c.repo_shape_modules())
    rid = c.new_run_id(cmd)
    code, out = c.run_command(path, cmd, SHAPE_WALL_S, niced=True, run_id=rid)
    if code == 0:
        return "green", ""
    rec = c.run_record(path, cmd, rid) if code else None
    if code == 124 or (rec or {}).get("not_run"):
        return "cut", ""
    names = sorted({f["class"] for f in (rec or {}).get("failed") or [] if f.get("class")}) or c.failed_classes(out)
    return ("red", ", ".join(names)) if names else ("cut", "")


def _shape_wanted(b, main, tip):
    """T-4563: whether a lane's check child runs its repo-shape modules first: a Sampo tree with the modules, and no green
    at this tip yet (a green one, or a batch's, needs no run)."""
    m = b.meta
    if m.get("check_shape") == tip or (m.get("check_state") == "green" and m.get("check_tip") == tip):
        return False
    return _shape_tree(m.get("lane") or main)


def _shape_clear(b, tip, main):
    """T-4563: whether a lane's repo-shape modules are clear at `tip` for a batch to queue it: they ran green there, or the
    lane's tree has none (another project's)."""
    return b.meta.get("check_shape") == tip or not _shape_tree(b.meta.get("lane") or main)


def _shape_stage(p, b, main, tip):
    """T-4563: the lane's repo-shape modules, run first in its check child, before the turn (so lanes run theirs at once).
    'red': the lane's tip is recorded red with the failing classes (no retry until it moves) and no long check runs.
    'green': check_shape = tip, so the hold check runs without the classes. 'cut': no verdict, the long check runs them.
    'moved': the tip changed meanwhile; nothing is written, the newer tip's check decides."""
    from sampo import cli
    code = _checker_stamp(main)  # T-4404: taken before the run, as the long check's is: this red is held against it too
    verdict, names = _shape_run(b.meta.get("lane") or main)
    if _lane_tip(main, cli.need_brief(p, b.id)) != tip:
        return "moved"
    if verdict == "red":
        failed = " ".join(names.split(", "))[:500]
        _set_check(p, b.id, f"its repo-shape modules are red in its lane: {failed} (tip {tip[:9]}; no long check was run; "
                            "fix them, commit, and accept again)", check_state="red", check_tip=tip, check_failed=failed,
                   check_pid=None, check_ticks=None, check_shape=None, check_code=code)
    elif verdict == "green":
        _set_check(p, b.id, f"repo-shape modules green on {tip[:9]}: the check runs the rest without the classes",
                   check_shape=tip)
    return verdict


def accept(p, b, main, args):
    """T-3922: a reviewed lane in one call: each --audit (lens, how, result) recorded on its brief from the lane's tree,
    the close's missing lines named, and the lane queued first (first_ids): into a running land at its next train
    boundary, or with none running a detached land starts now and trains it first (accept never waits on it). Only a lane awaiting a merge is queued (quiet 0:
    the review just happened); any other lane says why. Nothing is forced, granted or closed beyond land's own close."""
    from sampo import cli
    from sampo import lanes
    rows = _audit_rows(args)  # T-4391: refused before anything is recorded or started
    path = b.meta.get("lane") or ""
    if not path or not os.path.isdir(path):
        raise c.PolicyError(f"{b.id} has no lane to accept (fm lane brief {b.id} starts a builder)")
    tip = _lane_tip(main, b)
    clean = _clean_run(b, tip, main)  # T-4286: a passing run at its tip that held the repo-shape classes (see there)
    skip_check = bool(getattr(args, "no_check", False))
    if skip_check and not clean:  # refused before anything is recorded
        raise c.PolicyError(
            f"{b.id} has no passing affected run at its tip that held the repo-shape classes "
            f"({', '.join(c.repo_shape_classes())}): --no-check would queue it without them. Run "
            f"`sampo check --affected --hold --evidence {b.id}` in the lane (or drop --no-check: accept starts that check)")
    gaps = _accept_gaps(b, path, main, rows)
    if gaps:  # T-4487: refused before anything is recorded or started: the close would refuse the lane for these
        raise c.PolicyError(f"{b.id} not accepted, nothing recorded: the close would refuse this lane: {'; '.join(gaps)}. "
                            f"Accept again with the audits it names ({AUDIT_FORM}, one per lens)")
    tree = c.worktree_id(path)
    added = 0
    for lens, how, result in rows:
        _, new = cli.mutate(p, b.id, lambda x, lens=lens, how=how, result=result: _add_audit_once(x, lens, how, result, tree),
                            "audit", {"lens": lens, "how": how[:200], "result": result[:300]})
        added += bool(new)
    b = cli.need_brief(p, b.id)
    lines = [f"accept {b.id}: {added} audit(s) recorded ({', '.join(sorted({lens for lens, _, _ in rows}))})"
             + (f", {len(rows) - added} already on the brief" if added < len(rows) else "") + "."]
    lines += [f"  the close will be refused: {g} (add it to the brief's Close fields before the land closes it)"
              for g in _close_gaps(b)]
    if b.id not in lanes.awaiting_merge(p, c.load_briefs(p), 0, fresh=True):  # T-4265: the review just saw its tip
        lines.append(f"  not queued: {b.id} is not a finished lane waiting on a merge (commits, a clean tree); "
                     "fm lane list says what it waits on")
        return cli.out(args, {"id": b.id, "queued": False, "audits": len(rows)}, "\n".join(lines))
    if not skip_check and (not clean or _check_hold(b, tip, main)):
        # T-4286: no clean run at its tip, or its check holds it (running, red, cut off: what the land would skip it for):
        # the lane's own check decides, then it queues; a hold is said as "held", never as queued
        return _accept_check(p, b, main, args, tip, lines)

    def accepted(x):  # T-4239: the start of its accept-to-live time; a check a newer green run cleared is green now
        x.meta["accepted_at"] = round(time.time(), 1)
        if x.meta.get("check_state") in ("running", "red", "cut_off") and x.meta.get("check_tip") == tip:
            x.meta["check_state"] = "green"
            for k in ("check_failed", "check_pid", "check_ticks"):
                x.meta.pop(k, None)
    cli.mutate(p, b.id, accepted, "accepted", {"at": round(time.time(), 1)})
    how, started = _queue(p, main, b.id)
    if how == "running":
        lines.append(f"  {b.id} queued first in the running land: it trains at the land's next train boundary.")
        return cli.out(args, {"id": b.id, "queued": True, "land": "running"}, "\n".join(lines))
    if started is None:
        lines.append("  land not started in the background (FOREMAN_NO_BACKGROUND or paused): sampo lane land runs it here")
        return cli.out(args, {"id": b.id, "queued": True, "land": "not started"}, "\n".join(lines))
    pid, log = started
    lines.append(f"  land started in the background (pid {pid}, log {log}); it trains {b.id} first.")
    return cli.out(args, {"id": b.id, "queued": True, "land": "started", "pid": pid, "log": log}, "\n".join(lines))


def _accept_check(p, b, main, args, tip, lines):
    """T-4286: accept for a lane with no clean run at its tip (or one its check holds): the check running (said), held
    red at this tip (said as `held: …`, the land's own words), else started detached (a dead one once more, claimed
    first so two accepts start one); the lane queues when it is green. T-4404: a held check _rerun_reason names (cut off
    or without a result twice; red with the checker's code changed) starts again as try 1, at most RESTART_MAX times per
    tip (then the hand command) and not while other lanes' checks wait for a slot (`restarts when the check queue is
    empty`); a restart that does not start leaves the held record as it was."""
    from sampo import cli
    m = b.meta
    st = m.get("check_state") if m.get("check_tip") == tip else None
    tries = int(m.get("check_tries") or 1)
    restarts = None

    def running(meta):
        lines.append(f"  {b.id}: check is running (pid {meta.get('check_pid')}, log {meta.get('check_log')}); the lane "
                     "joins the land when it is green")
        return cli.out(args, {"id": b.id, "queued": False, "check": "running", "pid": meta.get("check_pid")}, "\n".join(lines))
    if st == "running" and _check_alive(m):
        return running(m)
    def held(why):
        lines.append(f"  {b.id}: {why}")
        return cli.out(args, {"id": b.id, "queued": False, "check": st, "held": why,
                              "failed": m.get("check_failed") or ""}, "\n".join(lines))
    if why := _check_hold(b, tip, main):
        if reason := _rerun_reason(m, main):  # T-4404: asked again
            done, mine = _restart_count(m, tip), f"sampo check --affected --hold --evidence {b.id}"
            if done >= RESTART_MAX:
                return held(f"held: {reason}; accept restarted it {done} times at this tip already: run {mine} in the lane")
            if any(x.id != b.id and x.meta.get("check_state") == "running" and x.meta.get("check_queued")
                   and _check_alive(x.meta) for x in c.load_briefs(p)):
                return held(f"held: {reason}; sampo lane accept {b.id} restarts it when the check queue is empty "
                            "(other lanes' checks wait for a slot)")
            restarts = done + 1
            lines.append(f"  {b.id}: starting its check again as try 1 ({reason}; restart {restarts} of {RESTART_MAX})")
        elif not _restartable(m):
            return held(why)
    since = None
    if len(_git(main, "rev-list", "--parents", "-n", "1", tip).stdout.split()) > 2:  # T-4355: a merge tip has no diff of its own
        since = m.get("base") or None
    try:
        started = _start_check(p, b, main, tip, since, tries + 1 if st == "running" and restarts is None else 1, restarts)
    except _ClaimLost as e:  # another accept claimed it after this one read the task
        return running(e.meta)
    if not started:
        lines.append(f"  check not started in the background (FOREMAN_NO_BACKGROUND or paused): run `sampo check --affected "
                     f"--hold --evidence {b.id}` in the lane, then accept again")
        return cli.out(args, {"id": b.id, "queued": False, "check": "not started"}, "\n".join(lines))
    pid, log = started
    lines.append(f"  check started in the background (pid {pid}, log {log}): the lane joins the land when it is green; a red "
                 "one is left out with its failing classes named on the task")
    return cli.out(args, {"id": b.id, "queued": False, "check": "started", "pid": pid, "log": log}, "\n".join(lines))


if __name__ == "__main__":  # T-3119: a lane's check process (see _child_argv)
    sys.exit(_main(sys.argv[1]))

