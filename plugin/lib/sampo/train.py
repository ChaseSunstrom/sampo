"""T-3380: sampo lane train, a merge queue. Reviewed lanes merge one after another in a scratch integration worktree
(never the main checkout); the project's gates run once on the result; a red run names its culprit lanes from its own
output (T-3602: _blame), bisecting by prefix only what it can't pin on one lane, each probe the red gate alone on the
red classes alone; the culprits are dropped and a narrowed confirm run checks the rest; the main checkout's branch then
fast-forwards to the gated tip.
The train's check_run carries the code tree (core.code_tree: no CHANGELOG.md, no sync mirror), so `sampo check` after
each close reuses the pass while the code is unchanged. T-3544: the full-suite gate runs only the tests the batch's
changes reach (_affected_gate); the full suite then runs on the landed merges in the background (lanes.pm_run)."""
import ast
import collections
import concurrent.futures
import contextlib
import fcntl
import functools
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import signal
import stat
import statistics
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import typing

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
        firsts = {}  # T-5047: class -> [(test, first exception line)] from the last run's record, when it left one

        def go(cmd):  # yields to the machine's load and to other work, as _probe's runs do
            c.wait_for_load("sampo lane train")
            rid = c.new_run_id(cmd)  # T-5047: stamped, so the record read below is this run's and no other's
            raw = cmd
            if shutil.which("nice"):
                cmd = "nice -n 10 bash -c " + shlex.quote(cmd)
            _lost_check(wt)
            got = c.run_command(wt, cmd, c.load_scaled(1800), run_id=rid)
            _lost_check(wt)  # a run in a vanished folder ends 127: that is the loss, not a red base
            firsts.clear()
            if rid and got[0] not in (0, 5, 124):
                firsts.update(_first_failures(wt, raw, rid))
            return got

        code, out = go(run)
        if code not in (0, 5, 124):  # one flake rerun of the same affected tests
            code, out = go(run)
        if code in (0, 5, 124):  # a run of no tests or a timeout proves nothing: the full gates decide
            return ""
        return _why_red({"cmd": sub[1], "affected": ts, "failing": _failed(out), "firsts": firsts,
                         "tail": out.rstrip().splitlines()[-15:]})
    finally:
        _git(main, "worktree", "remove", "--force", wt)
        shutil.rmtree(wt, ignore_errors=True)
        c.scratch_release(wt)


def _first_failures(root, cmd, run_id):
    """T-5047: {class: [(test, its first exception line)]} from the record the run stamped `run_id` left (run.py --last,
    T-4268; redacted and bounded), {} when `cmd` is not tests/run.py, it left no record or the record is unreadable."""
    rec = c.run_record(root, cmd, run_id)
    got = {}
    for f in (rec.get("failed") if rec else None) or []:
        if isinstance(f, dict) and f.get("class") and f.get("first"):
            line = " ".join(c.redact(str(f["first"])).split())  # one line, the redactor's
            got.setdefault(str(f["class"]), []).append((str(f.get("test") or ""), line[:160]))
    return got


def _with_first(names, firsts, n=6, per=2):
    """T-5047: `names` joined as c.joinmore does, each followed by `(test: first exception line)` of its failures in
    `firsts` (_first_failures') when the record has them, at most `per` tests a name; a name it lacks reads as before."""
    out = []
    for k in names:
        hits = [x for cls, xs in sorted((firsts or {}).items()) if _same(cls, k) for x in xs]
        shown = [f"{t}: {f}" if t else f for t, f in hits[:per]] + ([f"+{len(hits) - per} more"] if len(hits) > per else [])
        out.append(k + (f" ({'; '.join(shown)})" if shown else ""))
    return c.joinmore(out, n)


def _why_red(r, blame="", tail=None):
    """T-3544: a red gate run as a reason: the gate (on how many affected modules), the classes it names failing (T-5047:
    each with its failing tests and first exception lines, when the row carries `firsts`), what blamed the lane (T-3602)
    and the last 15 lines (`tail`: the blamed class's), so nobody reruns a suite to learn them."""
    s = r["cmd"] + (f" on {len(r['affected'])} affected test module(s)" if r.get("affected") else "")
    s += f", failing {_with_first(r['failing'], r.get('firsts'), 8)}" if r.get("failing") else ""
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


def paused_stop(p):
    """T-5044: the refusal a train or a land gives before it merges or tests anything while Sampo is paused (the
    machine-wide pause or this project's own: ahead.paused). A pause refuses the gate runs, and the train reads a refused
    gate as a red base, so it stops here, saying the pause did it. Nothing is merged, parked or dropped for it, and the
    pause is the user's hold: it is named, never bypassed. '' when not paused."""
    from sampo import ahead
    if not ahead.paused(p):
        return ""
    c.log_event(p, "lane_train_paused", data={})
    if c.panicked():
        return ("refused before any merge or test: Sampo is paused, and a pause stops the gates, so no base or lane can "
                "be judged; nothing was merged, parked or dropped: resume first (sampo pause off; sampo pause says whose "
                "it is), then run it again")
    return ("refused before any merge or test: this project is paused, the user's hold on it (the gates themselves "
            "would run); nothing was merged, parked or dropped: resume first (sampo pause off; sampo pause says whose "
            "it is), then run it again")


def _texts(out, names):
    """T-3602: {red name: its failure text}. unittest's and run.py's ==== rules split the output; a block belongs to
    the names in its first two lines (`==== mod.Class`, `FAIL: test (mod.Class.test)`); a name heading none gets it all."""
    blocks = re.split(r"(?m)^(?====)", out)
    heads = lambda k, b: re.search(rf"(?<![\w.]){re.escape(k)}(?!\w)", "\n".join(b.splitlines()[:2]))  # noqa: E731  # not a list: a block's two head lines
    return {k: "".join(b for b in blocks if heads(k, b)) or out for k in names}


def _blame(out, names, ids, diff, reach, explained=None, who=None):
    """T-3602: who broke the gate, from its red run alone: {lane id: (why, red name)} for each red name (the whole
    output when it names none) one lane alone explains. Rules, strongest first, each narrowing the lanes the one
    before matched: (a) the failure text names a file the lane's diff changed (traceback frames aside: they name the
    test and the code on the stack, not the fault); (c) the lane changed the failing test; (b) its diff reaches the
    failing test (cli.affected's map, as the probes pick tests). A name matching no lane or several blames nobody.
    T-4986: each red name a lane was found for is added to the set `explained` (one lane may explain several; the
    result keeps its first), so the caller bisects the rest in the same round. T-3809: `who` (a dict) takes {red name:
    the lane that explains it}, so a caller that moves a lane's blame can hand its other names back."""
    blamed = {}
    for k, text in (_texts(out, names) if names else {"": out}).items():
        text, cand = _FRAME.sub("", text), list(ids)
        for n in range(3 if k else 1):
            hits = [(i, w) for i in cand if (w := _rule(n, k, text, diff(i), reach(i) if n == 2 else ()))]
            if len(hits) == 1:
                blamed.setdefault(hits[0][0], (hits[0][1], k))
                if explained is not None:
                    explained.add(k)
                if who is not None:
                    who[k] = hits[0][0]
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


_MAP_FILE = re.compile(r"MASTER\.md|docs/map/[^/\0]+\.md")  # T-5472: MASTER.md too
_MARKER = re.compile(r"^(?:<{7}|>{7})(?: .*)?$|^={7}$", re.M)  # a conflict marker line


def _map_only(files):
    """T-4375: True when there are conflicted files and every one is MASTER.md or a docs/map/*.md entry: two lanes
    that each add a sentence to one long entry clash textually though nothing disagrees. One code file among them:
    False."""
    return bool(files) and all(_MAP_FILE.fullmatch(f) for f in files)


def _plain_file(wt, rel):
    """T-5836: the lstat of `rel` in the scratch tree `wt` when it is a plain file the train may write: a regular file
    (no link, folder, fifo or device), with one hard link, whose real path is `wt`/`rel` itself (no folder on the way
    is a link, inside the tree or out of it). Else None. A lane's tree is the lane's text: a map or test file it made
    a link, or a folder it made a link, would have the union's write change a file outside the scratch tree."""
    path = os.path.join(wt, rel)
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
        return None
    if os.path.realpath(path) != os.path.join(os.path.realpath(wt), os.path.normpath(rel)):
        return None
    return st


def _write_plain(wt, rel, data):
    """T-5836: write `data` over the plain file `rel` in `wt` (_plain_file), True when written, False when refused:
    the one write every train union goes through. The open refuses a link (O_NOFOLLOW) and never waits on a fifo
    (O_NONBLOCK), and the file it got must be the one the check saw (device and inode), so a swap between the check
    and the write writes nothing; it still has one hard link then too. It truncates only after that. A failing write
    (no space, say) is a refusal as well: False, never an exception, so the caller parks the lane and aborts the
    merge (the file may then be half written, which the abort undoes)."""
    st = _plain_file(wt, rel)
    if st is None:
        return False
    try:
        fd = os.open(os.path.join(wt, rel), os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return False
    try:
        now = os.fstat(fd)
        if not stat.S_ISREG(now.st_mode) or now.st_nlink != 1 or (now.st_dev, now.st_ino) != (st.st_dev, st.st_ino):
            return False
        os.ftruncate(fd, 0)
        with os.fdopen(os.dup(fd), "wb") as fh:
            fh.write(data)
    except OSError:
        return False
    finally:
        os.close(fd)
    return True


def _union_map(wt, files):
    """T-4375: resolve the merge in `wt`, conflicted only in map files `files`: each file's three index stages through
    the key-aware map merge (T-5472, sampo.mapmerge, keep_both: a line both sides rewrote is kept twice, never a line
    the lane still had at its base after trunk changed or moved it; no base stage, both sides added it: an empty base).
    All results are read first and written only when every one is clean (no marker line, a plain file). The files
    where a line was kept twice ([] for none), or None leaving the merge as it was, for the caller to abort."""
    from sampo import mapmerge
    stages = {}  # path: {stage: mode}
    r = _git(wt, "ls-files", "-u", "-z")
    for rec in r.stdout.split("\0"):
        head, _, path = rec.partition("\t")
        parts = head.split()
        if len(parts) == 3 and path in files:
            stages.setdefault(path, {})[parts[2]] = parts[0]
    done, doubled = {}, []
    for f in files:
        got = stages.get(f, {})
        if "2" not in got or "3" not in got or any(m not in ("100644", "100755") for m in got.values()) \
                or _plain_file(wt, f) is None:
            return None  # deleted on one side, or not a plain file (T-5836: a link, a link on the way): a real disagreement
        texts = []
        for n in ("1", "2", "3"):
            out = subprocess.run(["git", "-C", wt, "show", f":{n}:{f}"], capture_output=True) if n in got else None
            if out is not None and out.returncode:
                return None
            texts.append(out.stdout.decode("utf-8", "surrogateescape") if out is not None else "")
        text, twice = mapmerge.merge(*texts, keep_both=True, path=f)
        if _MARKER.search(text):
            return None
        done[f] = text.encode("utf-8", "surrogateescape")
        if twice:
            doubled.append(f)
    for f, data in done.items():
        if not _write_plain(wt, f, data) or _git(wt, "add", "--", f).returncode:
            return None
    return doubled


_TEST_FILE = re.compile(r"plugin/tests/[^/\0]+\.py")
_MARK_LINE = re.compile(rb"(?:<{7}|\|{7}|={7}|>{7})(?: |$)")  # a conflict marker line (after its newline is cut)
TESTS_KEY = "+tests"  # T-5824: kept[lane id + TESTS_KEY] = the test files the lane's merge resolved by keeping both insertions


def _tests_only(files):
    """T-5824: True when there are conflicted files and every one is a plugin/tests/*.py file."""
    return bool(files) and all(_TEST_FILE.fullmatch(f) for f in files)


def _bound(source):
    """The names the top-level statements of python `source` (text) bind, one entry per binding: a def, a class, an
    assignment target (also annotated), an imported name (the `as` name, else the first dotted part). None when it does
    not parse."""
    try:
        body = ast.parse(source).body
    except (SyntaxError, ValueError):
        return None
    got = []

    def target(t):
        if isinstance(t, ast.Name):
            got.append(t.id)
        elif isinstance(t, (ast.Tuple, ast.List)):
            for e in t.elts:
                target(e)
        elif isinstance(t, ast.Starred):
            target(t.value)
    for s in body:
        if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            got.append(s.name)
        elif isinstance(s, ast.Assign):
            for t in s.targets:
                target(t)
        elif isinstance(s, ast.AnnAssign):
            target(s.target)
        elif isinstance(s, (ast.Import, ast.ImportFrom)):
            got += [a.asname or a.name.split(".")[0] for a in s.names if a.name != "*"]
    return got


def _indent(line):
    """Width of a line's leading whitespace, a tab counting to the next multiple of 8."""
    s = line.expandtabs()
    return len(s) - len(s.lstrip())


def _block(lines):
    """(indent, names bound) of an inserted block (list of byte lines): the smallest indent of its non-blank lines (None
    for a blank block) and the names its statements bind once it is dedented. None when it is not utf-8 or does not
    parse on its own."""
    try:
        text = b"".join(lines).decode("utf-8")
    except UnicodeDecodeError:
        return None
    rows = [x for x in text.splitlines() if x.strip()]
    low = min((_indent(x) for x in rows), default=None)
    names = _bound(textwrap.dedent(text)) if rows else []
    return None if names is None else (low, names)


def _insertion_union(ours, base, theirs):
    """T-5824: the three versions (bytes) of one file merged with `git merge-file --diff3`; a conflict hunk whose base part
    is empty (both sides only added lines at that spot) becomes ours then theirs. The merged bytes, or None to leave the
    merge as it was: a hunk that held a base line (a changed line is a disagreement), a clean run (git's own merge saw a
    conflict this did not), a marker line already in an input, a merge-file that timed out, or a union that could change
    what a line means or shadow a name: the two inserted blocks start at different indents (a method added to the last
    class beside a class would join that class), or one starts deeper than the line before the hunk, or one does not
    parse on its own, or both bind one name (a def, a class, an assignment target, an imported name), or the result
    does not parse or binds a top-level name more times than either side does."""
    if any(_MARK_LINE.match(ln.rstrip(b"\r\n")) for t in (ours, base, theirs) for ln in t.splitlines()):
        return None
    try:
        with tempfile.TemporaryDirectory(prefix="sampo-union-") as d:
            paths = []
            for n, t in (("ours", ours), ("base", base), ("theirs", theirs)):
                paths.append(os.path.join(d, n))
                with open(paths[-1], "wb") as fh:
                    fh.write(t)
            r = subprocess.run(["git", "merge-file", "-p", "--diff3", "-L", "ours", "-L", "base", "-L", "theirs", *paths],
                               capture_output=True, timeout=60)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode < 1 or r.returncode > 127:
        return None
    out, state, mine, old, other, hunks = [], 0, [], [], [], 0
    for ln in r.stdout.splitlines(keepends=True):
        bare = ln.rstrip(b"\r\n")
        if state == 0 and bare.startswith(b"<<<<<<< "):
            state, mine, old, other = 1, [], [], []
        elif state == 1 and bare.startswith(b"||||||| "):
            state = 2
        elif state == 2 and bare == b"=======":
            state = 3
        elif state == 3 and bare.startswith(b">>>>>>> "):
            if old:
                return None  # a base line sits in this hunk: someone changed it
            m1, m2 = _block(mine), _block(other)
            if m1 is None or m2 is None or set(m1[1]) & set(m2[1]):
                return None
            if m1[0] is not None and m2[0] is not None:
                before = next((_indent(x.decode("utf-8", "replace")) for x in reversed(out) if x.strip()), None)
                if m1[0] != m2[0] or (before is not None and m1[0] > before):
                    return None
            out += mine + other
            state, hunks = 0, hunks + 1
        elif state == 0:
            out.append(ln)
        elif state == 1:
            mine.append(ln)
        elif state == 2:
            old.append(ln)
        else:
            other.append(ln)
    if state or not hunks:
        return None
    merged = b"".join(out)
    try:
        have, a, z = (_bound(t.decode("utf-8")) for t in (merged, ours, theirs))
    except UnicodeDecodeError:
        return None
    if have is None or a is None or z is None:
        return None
    if any(have.count(n) > max(a.count(n), z.count(n)) for n in set(have)):
        return None
    return merged


def _union_tests(wt, files):
    """T-5824: resolve the merge in `wt`, conflicted only in test files `files`: each file's three index stages through
    _insertion_union. All results are read first and written only when every one is a union (a plain file, all three
    stages present). The list of files, or None leaving the merge as it was, for the caller to abort."""
    stages = {}  # path: {stage: mode}
    for rec in _git(wt, "ls-files", "-u", "-z").stdout.split("\0"):
        head, _, path = rec.partition("\t")
        parts = head.split()
        if len(parts) == 3 and path in files:
            stages.setdefault(path, {})[parts[2]] = parts[0]
    done = {}
    for f in files:
        got = stages.get(f, {})
        if sorted(got) != ["1", "2", "3"] or any(m not in ("100644", "100755") for m in got.values()) \
                or os.path.islink(os.path.join(wt, f)):
            return None
        texts = []
        for n in ("2", "1", "3"):
            out = subprocess.run(["git", "-C", wt, "show", f":{n}:{f}"], capture_output=True)
            if out.returncode:
                return None
            texts.append(out.stdout)
        text = _insertion_union(*texts)
        if text is None:
            return None
        done[f] = text
    for f, data in done.items():
        with open(os.path.join(wt, f), "wb") as fh:
            fh.write(data)
        if _git(wt, "add", "--", f).returncode:
            return None
    return sorted(done)


def _merge_in(wt, b, tip, log, kept=None, moved=None):
    """Merge `tip` into the scratch tree; a conflict is aborted and logged (id, files). The new head sha or None.
    T-4375: a conflict only in docs/map/*.md entries is resolved keeping both sides' lines, the files recorded in
    `kept` (lane id: files) for the train to say and follow up. T-5824: a conflict only in plugin/tests/*.py files where
    every hunk is a pure insertion (both sides only added lines at that spot) keeps ours then theirs, the files recorded
    in `kept` under the lane id + TESTS_KEY; the train's gate judges the result as it does any merge. T-5381: map entries
    moved to keep a file under its cap are recorded in `moved` (lane id: [str])."""
    ident = ("-c", "user.name=sampo", "-c", "user.email=sampo@localhost")
    msg = f"Merge {b.id}: {b.title}"
    if kept is not None:
        kept.pop(b.id, None)
        kept.pop(b.id + TESTS_KEY, None)
    before = _git(wt, "rev-parse", "HEAD").stdout.strip()
    r = _git(wt, *ident, "merge", "--no-ff", "-m", msg, tip)
    if not r.returncode:
        return _over_cap(wt, b, before, log, moved)
    files = [f for f in _git(wt, "diff", "--name-only", "--diff-filter=U", "-z").stdout.split("\0") if f]
    doubled = _union_map(wt, files) if kept is not None and _map_only(files) else None
    unioned = _union_tests(wt, files) if kept is not None and doubled is None and _tests_only(files) else None
    if unioned is not None and not _git(wt, *ident, "commit", "-m", msg).returncode:
        kept[b.id + TESTS_KEY] = unioned
        return _over_cap(wt, b, before, log, moved)
    if doubled is not None and not _git(wt, *ident, "commit", "-m", msg).returncode:
        if doubled:  # T-5472: only a line kept twice needs a read; a clean key-aware merge needs none
            kept[b.id] = sorted(doubled)
        return _over_cap(wt, b, before, log, moved)
    _git(wt, "merge", "--abort")
    log.append((b.id, "conflicts with an earlier lane in the train" + (f": {c.joinmore(files, 8)}" if files else "")))
    return None


_LIB_SHARD = re.compile(r"docs/map/(files-lib-(\d+))\.md")  # T-5381: the shards of independent module bullets
_BULLET_KEY = re.compile(r"- `([^`\n]+)`")
_INDEX_LINE = re.compile(r"(- `[^`\n]+`)\s.*?(\s?\[→ [^\]\n]+\]\(docs/map/[^)\n]+\))$")  # name, the words, the link


def _bullet_blocks(lines):
    """T-5381: [(key or None, first, end)] for each bullet of a shard: a `- ` line and the indented lines after it."""
    out, i = [], 0
    while i < len(lines):
        if not lines[i].startswith("- "):
            i += 1
            continue
        j = i + 1
        while j < len(lines) and lines[j].strip() and lines[j][0] in " \t":
            j += 1
        m = _BULLET_KEY.match(lines[i])
        out.append((m.group(1) if m else None, i, j))
        i = j
    return out


def _pure_additions(before, now):
    """T-5381: [(key, lines of the whole bullet)] a shard `now` added over `before`, or None unless that is all it did:
    each new bullet has a key `before` lacks and `now` minus those bullets is `before` word for word (an edit of an
    existing bullet, a changed header or a line anywhere else is no pure addition)."""
    was, here = before.split("\n"), now.split("\n")
    known = {k for k, _, _ in _bullet_blocks(was)}
    added = [(k, i, j) for k, i, j in _bullet_blocks(here) if k is not None and k not in known]
    if not added or len({k for k, _, _ in added}) != len(added):
        return None
    gone = {n for _, i, j in added for n in range(i, j)}
    if [ln for n, ln in enumerate(here) if n not in gone] != was or any("```" in here[n] for n in gone):
        return None
    return [(k, here[i:j]) for k, i, j in added]


def _new_shard(source, moves, cap):
    """T-5381: (name, text) of a new files-lib shard that holds `moves` and has the shape of `source`'s header, or None
    when they do not fit one Read."""
    n = max(int(m.group(2)) for f in source["all"] if (m := _LIB_SHARD.fullmatch(f))) + 1
    name = f"docs/map/files-lib-{n}.md"
    keys = sorted(k for k, _ in moves)
    body = "\n".join(ln for _, lines in moves for ln in lines)
    stem = _LIB_SHARD.fullmatch(source["name"]).group(1)
    text = (f"{source['title']}\n\n"
            f"Full entries for [MASTER.md](../../MASTER.md) section 3 (File map), split out of [{stem}]({stem}.md) when a "
            f"train's lanes together took it over its {cap}-byte cap (T-5381): {len(keys)} module(s), {keys[0]} to "
            f"{keys[-1]}.\n\n### plugin/lib modules, {keys[0]} to {keys[-1]}\n\n<details>\n"
            f"<summary>every module, one line each</summary>\n\n{body}\n\n</details>\n")
    return (name, text) if len(text.encode("utf-8", "surrogateescape")) <= cap else None


def _append_bullets(text, moves):
    """T-5381: `text` (a files-lib shard) with the bullets `moves` after its last bullet, or None when it has none."""
    lines = text.split("\n")
    blocks = _bullet_blocks(lines)
    if not blocks:
        return None
    end = blocks[-1][2]
    return "\n".join(lines[:end] + [ln for _, bl in moves for ln in bl] + lines[end:])


def _repoint(master, key, old, new):
    """T-5381: `master` with the one index line of `key` that links `old` (a shard's stem) linking `new`, or None when
    there is not exactly one such line."""
    lines = master.split("\n")
    hits = [n for n, ln in enumerate(lines) if ln.startswith(f"- `{key}`") and ln.endswith(f"](docs/map/{old}.md)")
            and "[→ " in ln]
    if len(hits) != 1:
        return None
    ln = lines[hits[0]]
    lines[hits[0]] = f"{ln[:ln.rfind('[→ ')]}[→ {new}](docs/map/{new}.md)"
    return "\n".join(lines)


def _make_room(over, before, now, cap):
    """T-5381: (the map files to write {path: text}, what moved [str]) that bring the files in `over` (paths this merge
    took over `cap`) back under it, or None when that needs more than moving the lane's own pure additions: a files-lib
    shard's new whole bullets go to the files-lib shard with the most room (or a new one) and their MASTER.md index lines
    follow; MASTER.md over its cap has the lane's new index lines cut to the name and the link, the longest saving first,
    only as many as it takes. `before` {path: text} holds each file in `over` as the merge's base had it; `now` {path:
    text} MASTER.md and every shard as they are. Pure: it reads no file and runs no git."""
    files, notes = dict(now), []
    size = lambda t: len(t.encode("utf-8", "surrogateescape"))  # noqa: E731
    for f in sorted(over):
        if f == "MASTER.md":
            continue
        m = _LIB_SHARD.fullmatch(f)
        moves = _pure_additions(before[f], now[f]) if m else None
        if not moves or "MASTER.md" not in files:
            return None
        master = files["MASTER.md"]
        need = size("".join(ln + "\n" for _, bl in moves for ln in bl))
        room = {p: cap - size(t) for p, t in files.items() if p != f and _LIB_SHARD.fullmatch(p)}
        target = max((p for p, r in room.items() if r >= need), key=lambda p: room[p], default=None)
        made = None
        if target:
            added = _append_bullets(files[target], moves)
        else:
            title = now[f].split("\n", 1)[0]
            made = _new_shard({"name": f, "title": title, "all": list(files)}, moves, cap)
            target, added = made if made else (None, None)
        if not target or added is None:
            return None
        stem, there = _LIB_SHARD.fullmatch(f).group(1), _LIB_SHARD.fullmatch(target).group(1)
        for k, _ in moves:
            master = _repoint(master, k, stem, there)
            if master is None:
                return None
        files["MASTER.md"], files[target], files[f] = master, added, before[f]
        keys = [k for k, _ in moves]
        notes.append(f"{len(keys)} new map entr{'y' if len(keys) == 1 else 'ies'} ({c.joinmore(keys, 4)}) moved from {f} to "
                     f"{target}{' (a new shard)' if made else ''}, their MASTER.md index links repointed")
    if "MASTER.md" in over:
        master, mine = files["MASTER.md"], before["MASTER.md"].split("\n")
        extra = collections.Counter(mine)
        lines, cuts = master.split("\n"), []
        for n, ln in enumerate(lines):
            if extra[ln] > 0:
                extra[ln] -= 1
                continue
            if (m := _INDEX_LINE.fullmatch(ln)) and m.group(1) + " " + m.group(2).strip() != ln:
                cuts.append((len(ln) - len(m.group(1) + " " + m.group(2).strip()), n, m.group(1) + " " + m.group(2).strip()))
        total, shortened = size(master), []
        for gain, n, short in sorted(cuts, reverse=True):
            if total <= cap:
                break
            lines[n], total = short, total - gain
            shortened.append(n)
        if total > cap:
            return None
        files["MASTER.md"] = "\n".join(lines)
        notes.append(f"{len(shortened)} new MASTER.md index line(s) cut to the name and the link")
    return {p: t for p, t in files.items() if t != now.get(p)}, notes


def _over_files(wt, before):
    """T-4980: [(path, size)] of MASTER.md and docs/map files the merge in `wt` took over the cap a Read opens whole
    (MAP_FILE_BYTES), alone or with the lanes merged before it. A file already over it before the merge is the base's, so
    no lane is blamed for it."""
    over = []
    for f in _git(wt, "diff", "--name-only", "-z", before, "HEAD").stdout.split("\0"):
        if f != "MASTER.md" and not (f.startswith("docs/map/") and f.endswith(".md")):
            continue
        try:
            size = os.path.getsize(os.path.join(wt, f))
        except OSError:
            continue
        was = _git(wt, "cat-file", "-s", f"{before}:{f}").stdout.strip()
        if size > c.MAP_FILE_BYTES and not (was.isdigit() and int(was) > c.MAP_FILE_BYTES):
            over.append((f, size))
    return over


def _map_path(wt, f):
    """T-5381: the path of the map file `f` in `wt`, or OSError when it is not a plain file the lane's own tree holds:
    a symlink (a lane commits one to a file outside the scratch tree, and a write through it lands as the user), a
    directory or device, or a path whose parents resolve anywhere but under `wt` (docs/map itself a link). A file that
    does not exist yet is fine: it is a new shard."""
    root = os.path.realpath(wt)
    path = os.path.join(root, f)
    if os.path.realpath(os.path.dirname(path)) != os.path.dirname(path):
        raise OSError(f"{f}: a parent directory is a link or leaves the tree")
    if os.path.lexists(path) and (os.path.islink(path) or not os.path.isfile(path)):
        raise OSError(f"{f}: not a regular file")
    return path


def _read_map(wt, paths):
    """T-5381: {path: text} of the files `paths` in `wt` (none for one that does not exist), as bytes kept apart from the
    platform's newline and encoding. OSError for a link or a file that is not a plain one (_map_path)."""
    out = {}
    for f in paths:
        path = _map_path(wt, f)
        if not os.path.lexists(path):
            continue
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as fh:
            out[f] = fh.read().decode("utf-8", "surrogateescape")
    return out


def _write_map(wt, f, text):
    """T-5381: write `text` to the map file `f` in `wt` without following a link: _map_path first, then O_NOFOLLOW so a
    link made between the check and the open fails too (OSError)."""
    path = _map_path(wt, f)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "wb") as fh:
        fh.write(text.encode("utf-8", "surrogateescape"))


def _relieve(wt, b, before, over, moved):
    """T-5381: True when the merge in `wt` was amended to fit the cap (_make_room), the notes in `moved` {lane id:
    [str]}; False with the tree reset to the merge as it was when that cannot be done. Anything that goes wrong
    (a file that can't be read or written, git refusing) is False: the caller leaves the lane out as before."""
    ident = ("-c", "user.name=sampo", "-c", "user.email=sampo@localhost")
    merged = _git(wt, "rev-parse", "HEAD").stdout.strip()
    try:
        names = ["MASTER.md"] + [os.path.relpath(p, wt).replace(os.sep, "/") for p in c.map_shards(wt)]
        now = _read_map(wt, names)
        old = {}
        for f in over:
            r = subprocess.run(["git", "-C", wt, "show", f"{before}:{f}"], capture_output=True, timeout=60)
            if r.returncode:
                return False
            old[f] = r.stdout.decode("utf-8", "surrogateescape")
        got = _make_room(over, old, now, c.MAP_FILE_BYTES)
        if got is None:
            return False
        for f in got[0]:  # every target checked before the first write: a link anywhere is a refusal, not a half move
            _map_path(wt, f)
        for f, text in got[0].items():
            _write_map(wt, f, text)
        if _git(wt, "add", "--", *got[0]).returncode or _git(wt, *ident, "commit", "-q", "--amend", "--no-edit").returncode:
            raise OSError("git refused the amend")
        if moved is not None:
            moved[b.id] = got[1]
        return True
    except (OSError, KeyError, ValueError, subprocess.SubprocessError):
        _git(wt, "reset", "-q", "--hard", merged)
        _git(wt, "clean", "-fdq", "--", "docs/map")
        return False


def _over_cap(wt, b, before, log, moved=None):
    """T-4980: the merge's head sha, or None with the merge undone and the lane left out when it took MASTER.md or a
    docs/map file over the cap a Read opens whole (MAP_FILE_BYTES), alone or with the lanes merged before it: a gate
    and its bisection found that in about 40 minutes (2026-11-15, twice). A file already over it before the merge is
    the base's, so no lane is blamed for it.
    T-5381: before it is left out, the merge is amended to fit when the lane only added (_make_room: a files-lib shard's
    new whole bullets move to a shard with room and their index links follow; MASTER.md's new index lines are cut to the
    short form): ~119 waiting lanes each fit alone and together took files-lib-4 from 76 to 119 KB. What moved goes in
    `moved` {lane id: [str]} for the train's report."""
    if moved is not None:
        moved.pop(b.id, None)
    over = _over_files(wt, before)
    if over and _relieve(wt, b, before, [f for f, _ in over], moved):
        over = _over_files(wt, before)
        if over and moved is not None:
            moved.pop(b.id, None)
    if not over:
        return _git(wt, "rev-parse", "HEAD").stdout.strip()
    _git(wt, "reset", "-q", "--hard", before)
    _git(wt, "clean", "-fdq", "--", "docs/map")
    log.append((b.id, f"takes {c.joinmore([f'{f} ({n} bytes)' for f, n in over], 4)} over its {c.MAP_FILE_BYTES}-byte cap "
                      "with the lanes merged before it: move its map line to a docs/map file with room (sampo selfcheck "
                      "names them)"))
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
        firsts = {}  # T-5047: class -> [(test, first exception line)] from the first run's record, when it left one

        def run(cmd, stamp=False):  # (exit, the classes it names red)
            cwd, cmd, _ = c.normalize_verify(wt, cmd)  # T-3411's cwd fix first: the nice wrapper would hide it
            env, _ = cli._venv_env(wt, cpu_env(p), cmd)  # T-4150: the project's own interpreter, as the lane ran it
            rid, raw = (c.new_run_id(cmd) if stamp else None), cmd  # T-5047: the first run's record names its failures
            if shutil.which("nice"):  # the probe yields to whatever else the machine runs
                cmd = "nice -n 10 bash -c " + shlex.quote(cmd)
            _lost_check(wt)
            code, out = c.run_command(cwd, cmd, c.load_scaled(900), priority=True,  # T-3614: ahead of lane runs
                                      env=env, run_id=rid)  # T-3757: a CPU rise is red here too
            _lost_check(wt)  # a run in a vanished folder ends 127: that is the loss, not a red lane
            if rid and code not in (0, 5, 124):
                firsts.update(_first_failures(wt, raw, rid))
            return code, set(_failed(out))

        def again(classes):  # the red classes alone where the runner names them (pm_rerun_cmd), else cmd again
            return run(pm_rerun_cmd(wt, sorted(classes)) or cmd)

        code, bad = run(cmd, stamp=True)
        if code in (0, 5, 124):
            return ""
        if not bad:
            return f"its affected tests fail with the lane merged onto the base: exit {code}"
        code, red = again(bad)
        still = ((bad & red) or bad) if code else set()  # a flake is green on the rerun; a red naming none stays red
        if bad - still:
            notes.append((b.id, f"flaky (red once, green on a rerun): {_with_first(sorted(bad - still), firsts)}"))
        _git(wt, "reset", "-q", "--hard", base)
        based = (still & again(still)[1]) if still else set()  # red without the lane too: the base's, not the lane's
        if based:
            notes.append((b.id, f"its affected tests are red on the base itself: {c.joinmore(sorted(based), 6)}; "
                                "left to the gates"))
        blame = still - based
        return "its affected tests fail with the lane merged onto the base: " + _with_first(sorted(blame), firsts) if blame else ""
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


def live_mode(p):
    """T-5444 (T-4308's mode, a project's meta train_mode=live): a lane lands at once on its own record, with no criteria stage, no front
    gate and no batch cap; the full suite runs behind on the merged head (lanes.pm_run, T-3141/T-3446/T-3800) and reverts
    the merge that turned it red. On 2026-11-21 83 finished lanes waited here at 2 landed per 2 h, each train's front gate
    2-5 h. FOREMAN_FULL_GATE=1 forces the gated train; unset, a project is gated."""
    if os.environ.get("FOREMAN_FULL_GATE", "") not in ("", "0"):
        return False
    return c.read_meta(p).get("train_mode") == "live"  # set per project: sampo lane mode live|gated


def _dep_hold(p, b, main):
    """T-5725 review: why the land leaves out a lane stacked on another lane (meta stacked_on: built on that lane's
    unlanded tip), or None. Its commits carry the dependency's, so landing it first would put them on main unreviewed and
    unchecked, whatever land_key's order says: it waits until the dependency's tip is an ancestor of main. A dependency
    with no brief or no branch (landed and cleaned up) holds nothing; so does a dropped one, whose unmerged branch survives
    `lane rm` and never lands; git unable to say holds nothing either."""
    dep_id = str((b.meta or {}).get("stacked_on") or "")
    if not dep_id:
        return None
    dep = next((x for x in c.load_briefs(p) if x.id == dep_id), None)
    tip = _lane_tip(main, dep) if dep and dep.status != "dropped" else ""
    if not tip or _git(main, "merge-base", "--is-ancestor", tip, "HEAD").returncode != 1:
        return None
    return f"held: built on {dep_id}, whose lane tip is not on main yet; it joins once {dep_id} has landed"


def _lane_hold(p, b, sha, main):
    """Why the land leaves this lane out at `sha`, or None. A lane stacked on an unlanded dependency waits for it
    (_dep_hold, T-5725). Gated: its own check running, queued, red or missing
    (_check_hold, _unchecked). Live (T-5444): only a red check that its current checker gave; the behind suite judges
    the rest after the merge."""
    if dep := _dep_hold(p, b, main):
        return dep
    why = _check_hold(b, sha, main) or (None if live_mode(p) else _unchecked(b, sha, main))
    if why and live_mode(p) and not (why.startswith("held: check red") and "has changed since it ran" not in why):
        return None
    return why


LIVE_SMOKE = ("-m", "compileall", "-q", "plugin/lib", "plugin/hooks")  # T-5444: a merged syntax error never reaches the hooks


def _live_smoke(wt):
    """T-5444: '' when the merged tree compiles (Sampo's own tree: its hooks run from trunk), else the error. Other
    projects (no plugin/lib/sampo) have nothing to brick: ''."""
    if not os.path.isdir(os.path.join(wt, "plugin", "lib", "sampo")):
        return ""
    r = subprocess.run([sys.executable, *LIVE_SMOKE], cwd=wt, capture_output=True, text=True, timeout=300)
    return "" if r.returncode == 0 else (r.stdout + r.stderr).strip()[-600:] or f"exit {r.returncode}"


class ShapeVerdict(typing.NamedTuple):
    """T-5806: a repo-shape run's result: verdict 'green' | 'red' | 'cut' (no verdict), names the red classes joined (for a
    cut built by the search: why there is no verdict), secs the seconds a fresh run took (0 for a verdict from the cache)."""
    verdict: str
    names: str = ""
    secs: float = 0.0

    @property
    def why(self):
        """What a refusal says ('' when green): red names the classes, a cut (no verdict) is refused too."""
        if self.verdict == "red":
            return f"the merged tree breaks the repo-shape classes: {self.names}"
        if self.verdict == "cut":
            return (f"its repo-shape run has no verdict: {self.names}" if self.names else
                    f"its repo-shape run was cut (no verdict) at {SHAPE_BATCH_WALL_ENV}={shape_batch_wall()} s")
        return ""


def _live_shape(wt, p=None, main=None, base=None, classes=None):
    """T-5590: the live train's one repo-shape run on the merged tree, before anything lands (a module, an env name or a
    help position a lane adds is red here, not on trunk). T-5806: a ShapeVerdict, so _run splits a red batch by lane:
    green when the tree is green or not a Sampo tree (_shape_tree).
    T-5805: given the project, main and the base sha, a red tree runs the base's shape too (once per base, cached) and is
    red only for the classes not red on the base: those are trunk's, filed once as an express FIX (the key is the class
    set) and never stop a train (a class red on the merged tree but green on the base still does). A base with no
    verdict forgives nothing.
    T-5853: `classes` (the red classes a batch run named, joined) runs only those: a subset tree of the cull."""
    if not _shape_tree(wt):
        return ShapeVerdict("green")
    t0 = time.monotonic()
    verdict, names = _shape_run(wt, shape_batch_wall(), classes) if classes else _shape_run(wt, shape_batch_wall())
    secs = time.monotonic() - t0
    if verdict == "red" and p is not None and main and base:
        try:
            old = _base_shape_red(p, main, base)
        except Exception:  # noqa: BLE001: no base verdict forgives nothing
            old = None
        if old:
            new = [n for n in (x.strip() for x in names.split(",")) if n and n not in old]
            if not new:
                _trunk_red_capture(p, base, old)
                return ShapeVerdict("green", "", secs)
            names = ", ".join(new)
    return ShapeVerdict(verdict, names, secs)


SHAPE_CULL_WALLS = 2  # T-5806: the fresh shape seconds one cull may spend, in batch walls (shape_batch_wall()); cache hits are free


def _shape_cull(ids, shape, suspects=(), budget=None, base_rerun=None, notes=None):
    """T-5806: {lane id: (why, its classes)} the lanes to park so the rest of `ids` (merge order) is green on the repo-shape
    classes, from `shape(ids subset, fresh=False) -> ShapeVerdict` (the caller merges that subset on a fresh tree and
    caches a verdict per tree; `fresh` skips the cache).
    The lanes the red classes name (`suspects`) go out first: the rest of the batch green means the culprits are among
    them, and only those are searched, on top of the rest (so a pair of which only one is named parks that one alone: the
    land stays safe, the other lane is judged on the next train). Else (or after that) the prefix bisect of _run's gate red:
    the lane whose merge turns the prefix red; one red alone with the lanes before it green is the culprit, one green alone
    is parked with the earlier lane it needs (a second bisect), both, as no lane is red alone there. The bisect takes a
    red to be monotonic: when no prefix of the whole batch is red, the batch's red is run once more, uncached, and a green
    rerun lands it all with nothing parked (a flake). A cut run (no verdict) raises, and so does a search that has spent
    `budget` fresh seconds (default SHAPE_CULL_WALLS batch walls): nothing is parked then.
    T-5880: when every lane ids is parked and a class is red in all their trees, no lane is to blame for it (a base red, or
    a flake under load, as 16 of 16 lanes parked for test_fast_equiv.Passthrough): `base_rerun(class) -> ShapeVerdict`
    runs it once on the base alone, the class is dropped from every park (a lane left with none is not parked) whatever the
    rerun says, and (class, the rerun's verdict, number of lanes) is appended to `notes`. A cut rerun raises."""
    parked, spent = {}, [0.0]
    cap = SHAPE_CULL_WALLS * shape_batch_wall() if budget is None else budget
    order = {i: n for n, i in enumerate(ids)}

    def run(sel, fresh=False):
        if spent[0] >= cap:
            raise c.PolicyError(f"the repo-shape search for the lane(s) that turn it red spent {int(spent[0])} s of fresh runs "
                                f"(cap {int(cap)} s, {SHAPE_CULL_WALLS} x {SHAPE_BATCH_WALL_ENV}): nothing landed and no lane "
                                "parked; fix the red classes or land fewer lanes")
        v = shape(sorted(sel, key=order.get), fresh=True) if fresh else shape(sorted(sel, key=order.get))
        spent[0] += v.secs
        if v.verdict == "cut":
            raise c.PolicyError(v.why + ": nothing landed and no lane parked")
        return v

    fixed, pool = [], list(ids)  # fixed: lanes proven green together, never parked; pool: where the culprits are
    if (sus := [i for i in ids if i in set(suspects)]) and len(sus) < len(ids):
        rest = [i for i in ids if i not in sus]
        if run(rest).verdict == "green":
            fixed, pool = rest, sus
    while pool:
        top = run(fixed + pool)
        if top.verdict == "green":
            break
        lo, hi, red = 0, len(pool), top
        while hi - lo > 1:
            mid = (lo + hi) // 2
            got = run(fixed + pool[:mid])
            lo, hi, red = (lo, mid, got) if got.verdict == "red" else (mid, hi, red)
        if hi == len(pool) and len(fixed) + len(pool) == len(ids):  # the whole batch, no prefix of it red
            if (again := run(fixed + pool, fresh=True)).verdict == "green":  # no prefix red: the batch's red was a flake
                break
            red = again
        h, group = pool[hi - 1], [pool[hi - 1]]
        if hi > 1 and run(fixed + [h]).verdict != "red":  # green alone: it needs an earlier lane of the pool
            lo2, hi2 = 0, hi - 1
            while hi2 - lo2 > 1:
                mid = (lo2 + hi2) // 2
                lo2, hi2 = (lo2, mid) if run(fixed + pool[:mid] + [h]).verdict == "red" else (mid, hi2)
            group = [pool[hi2 - 1], h]
        for i in group:
            other = [x for x in group if x != i]
            parked[i] = (f"its merge turns the repo-shape classes red ({red.names or 'unnamed'})" + (
                f" together with {other[0]}, parked with it: neither is red alone" if other else ""), red.names)
        pool = [i for i in pool if i not in group]
    if base_rerun and len(ids) > 1 and len(parked) == len(ids):  # T-5880: a class red in every lane's tree is no lane's
        sets = [{n for n in re.split(r"[,\s]+", w[1]) if n} for w in parked.values()]
        for k in sorted(set.intersection(*sets)):
            v = base_rerun(k)  # the class alone on the base, once, as the subset trees ran it
            if v.verdict == "cut":
                raise c.PolicyError(f"the repo-shape class {k} is red in all {len(ids)} subset trees and its rerun on the "
                                    f"base has no verdict ({v.why}): nothing landed and no lane parked")
            if notes is not None:
                notes.append((k, v.verdict, len(ids)))
            for i in list(parked):
                why, names = parked[i]
                left = [n for n in re.split(r"[,\s]+", names) if n and n != k]
                if left:
                    parked[i] = (why.replace(f"({names})", f"({', '.join(left)})", 1), ", ".join(left))
                else:
                    del parked[i]
    return parked


LIVE_SHAPE_BASE = "live_shape_base"  # T-5805: meta key: {"sha": base, "red": [classes red on it]} (one entry, the last base run)


def _trunk_red_capture(p, base, classes):
    """T-5805: one express (urgent) trunk-red FIX for the shape classes red on the train's base, keyed on the class set
    (the key lanes.pm_inherited files under too, so a set is asked once). A capture that fails never changes the verdict."""
    try:
        from sampo import cli, eco
        names = c.joinmore([c.fit(c.plain(k), 100) for k in classes], 8)
        key = "Trunk red: " + ", ".join(c.fit(c.plain(k), 100) for k in sorted(classes))
        b, new = eco.capture_once(p, f"Trunk red: {names}\n\nThe repo-shape classes fail on trunk at {base[:9]}; a live "
                                     "train lands past them and refuses only a class it turns red itself. Fix them or find "
                                     "what turned them red.", "FIX", "trunk-red", key=key)
        if new:
            cli.mutate(p, b.id, lambda x: x.meta.update(priority="urgent"), "trunk_red_shape",
                       {"classes": classes[:20], "left_out": max(0, len(classes) - 20)})
    except Exception as e:  # noqa: BLE001
        c.log_event(p, "live_shape_capture_failed", data={"error": c.fit(str(e), 200)})


def _base_shape_cached(p, base):
    """T-5806: the classes the last base run found red on `base` (a list, [] when green), or None: no run kept for it."""
    got = c.read_meta(p).get(LIVE_SHAPE_BASE)
    return got["red"] if isinstance(got, dict) and got.get("sha") == base and isinstance(got.get("red"), list) else None


def _base_shape_red(p, main, base):
    """T-5805: the repo-shape classes red on `base` (a list, [] when green), or None with no verdict (a cut, a tree that
    would not build). Cached in meta per base sha: a land retried on the same base runs the base once."""
    if (got := _base_shape_cached(p, base)) is not None:
        return got
    wt = c.scratch_tree("sampo-shape-base-", main)
    try:
        if _git(main, "worktree", "add", "-q", "--detach", wt, base).returncode or not _shape_tree(wt):
            return None
        verdict, names = _shape_run(wt, shape_batch_wall())
    finally:
        _git(main, "worktree", "remove", "--force", wt)
        shutil.rmtree(wt, ignore_errors=True)
        c.scratch_release(wt)
    if verdict == "cut":
        return None
    red = sorted(n.strip() for n in names.split(",") if n.strip()) if verdict == "red" else []
    c.update_meta(p, **{LIVE_SHAPE_BASE: {"sha": base, "red": red}})
    return red


def _base_shape_rerun(p, main, base, classes):
    """T-5880: a ShapeVerdict for the repo-shape `classes` (joined) run once more on the bare `base`, as a subset tree runs
    them (narrowed, niced, the same wall), never from the cache. A red run is kept as the base's (meta, with the classes
    already kept for it) and filed once as the trunk-red FIX (_trunk_red_capture never raises); a base that will not build
    is a cut (no verdict)."""
    wt = c.scratch_tree("sampo-shape-base-", main)
    t0 = time.monotonic()
    try:
        if _git(main, "worktree", "add", "-q", "--detach", wt, base).returncode or not _shape_tree(wt):
            return ShapeVerdict("cut", "the base tree would not build")
        verdict, names = _shape_run(wt, shape_batch_wall(), classes)
    finally:
        _git(main, "worktree", "remove", "--force", wt)
        shutil.rmtree(wt, ignore_errors=True)
        c.scratch_release(wt)
    secs = time.monotonic() - t0
    if verdict == "red":
        red = sorted({*(_base_shape_cached(p, base) or []), *(n.strip() for n in names.split(",") if n.strip())})
        c.update_meta(p, **{LIVE_SHAPE_BASE: {"sha": base, "red": red}})
        _trunk_red_capture(p, base, red)
    return ShapeVerdict(verdict, names, secs)


def _held(p, b, main, tip):
    """The reason `sampo lane merge` would refuse this lane (private term, guard, self-change gate), or ''. T-4783: the
    private-term check is lanes.private_refusal, the function `merge` calls, first: a lane that carries a term never
    reaches a probe or a gate (the train once landed one that only ship met). Its reason names commit, file and line.
    T-4785: the body is lanes.land_holds, shared with fm serve's integrate and the pull-request train.
    T-4805 exempt: a hold with no landing of its own; _run records the landing (lanes.log_landed) after it fast-forwards.
    T-5446: a live train leaves the hook timing out of each lane's hold and times the merged batch once
    (_live_timing); after a merged timing hold the next land times each lane again, to find the one that slowed it."""
    from sampo import lanes
    return lanes.land_holds(p, b, main, tip, timing=not live_mode(p) or bool(c.read_meta(p).get(LIVE_TIMING_HOLD)))


HOLD_JOBS_ENV = "SAMPO_HOLD_JOBS"


def hold_jobs():
    """T-5465: how many lanes' holds run at once (SAMPO_HOLD_JOBS, default 4, 1 to 16). Each is a few git and replay
    subprocesses (about 20 s a lane on Sampo's own tree), so threads overlap them; 55 lanes one by one took about 18 min."""
    try:
        return min(16, max(1, int(os.environ.get(HOLD_JOBS_ENV) or 4)))
    except ValueError:
        return 4


def _holds(p, main, lanes_tips):
    """T-5465: _held for each (brief, tip), up to hold_jobs() at once; the reasons in the order given. A hold that raises
    raises here, as the one-by-one loop did (ex.map re-raises the first in order)."""
    def one(bt):
        return _held(p, bt[0], main, bt[1])
    if len(lanes_tips) < 2 or hold_jobs() == 1:
        return [one(bt) for bt in lanes_tips]
    with concurrent.futures.ThreadPoolExecutor(max_workers=hold_jobs()) as ex:
        return list(ex.map(one, lanes_tips))


LIVE_TIMING_HOLD = "live_timing_hold"  # T-5446: meta key: the merged batch a live train's hook timing held


def _live_timing(p, main, wt, base):
    """T-5446: the self-change gate with its hook timing on a live train's merged tree, once for the whole batch: [] when it
    holds, else its refusal rows (recorded in meta, so the next land times each lane and leaves out the one that slowed
    the hooks). Each lane's own hold already ran the guard replay, cases, footprint and verb rows."""
    import types
    from sampo import selfgate
    sha = _git(wt, "rev-parse", "HEAD").stdout.strip()
    files = [f for f in _git(main, "diff", "--name-only", base, sha).stdout.splitlines() if f.strip()]
    rows = selfgate.gate(p, types.SimpleNamespace(id="live-train", meta={}), main, sha, files)
    c.update_meta(p, **{LIVE_TIMING_HOLD: sha if rows else None})
    return rows


def _none(head, left):
    """The refusal when no lane lands, carrying `left` [(id, why)] for sampo lane land to park each (T-3500)."""
    e = c.PolicyError(head + "; ".join(f"{i}: {w}" for i, w in left))
    e.left = list(left)
    return e


_NAME_TOK, _PAT_TOK = re.compile(r"test_\w+(?:\.\w+){0,2}"), re.compile(r"[\w][\w.\-]*")


def _gate_names(cmd, suite):
    """T-3809: ([module names], [-k patterns]) of a criterion that is the suite gate `suite` on named modules
    (`python3 plugin/tests/run.py test_a test_b.Cls -k pat --deadline 30`), else None. Strict, as the shell would read
    it: the suite's own words first, then only `test_x[.Class[.test]]` names, `-k PATTERN` and `--deadline SECONDS`; an
    env prefix, `&&`, a pipe, a glob, `$(…)`, `-j`, a name-less run (the whole suite) or a quote that doesn't close is
    any other command, which runs in the lane as before."""
    if "\n" in cmd or "\r" in cmd:  # a line break ends a shell command: shlex reads what follows as more words
        return None
    try:
        want, got = shlex.split(suite), shlex.split(cmd)
    except ValueError:
        return None
    if not want or got[:len(want)] != want:
        return None
    names, pats, rest, i = [], [], got[len(want):], 0
    while i < len(rest):
        x = rest[i]
        if x in ("-k", "--deadline"):
            v = rest[i + 1] if i + 1 < len(rest) else ""
            if not (_PAT_TOK.fullmatch(v) if x == "-k" else v.isdigit()):
                return None
            pats += [v] if x == "-k" else []
            i += 2
        elif _NAME_TOK.fullmatch(x):
            names.append(x)
            i += 1
        else:
            return None
    return (names, pats) if names else None


def _test_names(path):
    """T-3809: the names a -k pattern (a substring of a test's id, as run.py reads it) can match in the test file `path`:
    its module stem, every class and every `test*` function. None when it can't be read or parsed."""
    import ast
    try:
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
    except (OSError, SyntaxError, ValueError):
        return None
    names = [_stem(path)] + [n.name for n in ast.walk(tree)
                             if isinstance(n, ast.ClassDef)
                             or isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test")]
    return set(names)


def _covered(p, b, checks):
    """T-3809: {criterion number: (its command, the test files it runs at the lane's tip)} for the criteria the train's
    gate runs on the merged tree: the suite gate (the affected template's runner) on named modules, each an existing
    test file of the lane's tip (its name the file's whole stem: run.py matches names as substrings) holding the class
    a `.Class` names and each -k pattern in at least one of the criterion's files. {} unless the lane sits clean on its
    tip (the files read are the tip's), or when no gate runs affected tests; every other criterion runs in the lane."""
    from sampo import cli, lanes
    tpl = c.read_meta(p).get("affected") or ""
    suite = cli.suite_gate(p.root, tpl, checks) if "{names}" in tpl else None
    wt, branch = b.meta.get("lane"), b.meta.get("lane_branch") or f"foreman/{b.id}"
    if not suite or not wt or not os.path.isdir(wt) or not lanes._lane_tree(wt, branch):
        return {}
    have = {}
    for f in _git(wt, "ls-files", "-z").stdout.split("\0"):
        if f.endswith(".py") and _stem(f).startswith("test_"):  # a test_a.json or .md fixture is no module of the gate
            have.setdefault(_stem(f), []).append(f)
    out = {}
    for n, cmd in b.verify_cmds():
        got = _gate_names(cmd, suite) if cmd else None
        if not got:
            continue
        files, ok = [], True
        for name in got[0]:
            mod, _, cls = name.partition(".")
            fs = have.get(mod) or []
            defs = [_defines(os.path.join(wt, f)) for f in fs]
            if not fs or (cls and (None in defs or not any(cls.split(".")[0] in d for d in defs))):
                ok = False  # no such module, or a class it can't be told to hold: run it here
                break
            files += [f for f in fs if f not in files]
        names = set()
        for f in files if ok and got[1] else ():
            found = _test_names(os.path.join(wt, f))
            if found is None:
                ok = False  # can't be parsed: run it here
                break
            names |= found
        if ok and all(any(k in x for x in names) for k in got[1]):  # a -k pattern is a part of a test or class name
            out[n] = (cmd, files)
    return out


def _covered_evidence(p, b, cover, row, tree):
    """T-3809: the evidence of lane b's covered criteria ({n: (cmd, files)}), once its train landed: the gate's passing
    `row` (cmd, exit, the modules a narrowed run named; none: the whole suite ran) on `tree`, named in each line, and the
    box ticked, so the close takes it as it takes a run of its own. A criterion whose modules the row doesn't list gets
    nothing here: the close runs it. Returns notes [(id, text)]: a criterion whose evidence or box the brief refuses (a
    red run of that command still stands as its newest) is told, not hidden, and the close runs it."""
    from sampo import cli
    if row.get("exit"):
        return []
    ran, notes = row.get("affected"), []

    def mark(x):
        for n, (cmd, files) in sorted(cover.items()):
            mods = sorted({_stem(f) for f in files})
            if ran is not None and not set(mods) <= set(ran):
                continue
            what = "its evidence was not recorded"  # then, once the line is in: the box
            try:
                x.add_evidence(cmd, f"exit 0 · covered by the train's gate run: '{row['cmd']}' ran "
                                    f"{c.joinmore(mods, 6) if ran is not None else 'the whole suite'} on tree {tree}",
                               ac=n, tree=tree, ran=True)
                what = "its evidence is recorded, but the box is not ticked"
                x.check_ac(n)
            except c.PolicyError as e:
                notes.append((b.id, f"criterion {n}: covered, but {what}: "
                                    f"{c.fit(str(e).splitlines()[0] if str(e) else 'refused', 120)}"))
    cli.mutate(p, b.id, mark, "lane_criteria_covered", {"tree": tree, "criteria": sorted(cover)})
    return notes


def _record_covered(p, merged, cov, row, tree):
    """T-3809: after the landing, each merged lane's covered criteria get their evidence (_covered_evidence); the notes
    [(id, text)] of what it left for the close. A failure of the recording (a disk, a lock) is a note too: the lane
    is already on trunk, and its close runs the criterion."""
    notes = []
    for b, _ in merged:
        if cov.get(b.id):
            try:
                notes += _covered_evidence(p, b, cov[b.id], row, tree)
            except Exception as e:
                notes.append((b.id, f"covered criteria left for the close to run: {type(e).__name__}: "
                                    f"{c.fit(str(e), 120)}"))
    return notes


def _check(p, main, base, b, tip, probe_tests, judged=False, info=None):
    """(why this lane can't land or '', notes): the lane's own acceptance commands re-run in its worktree, as `lane merge`
    does, then conflicts and red tests against the base (T-3474), all before any gate run. T-4756: `judged` is a lane
    `lane merge` already ran the criteria of (or waived them, logged) at this tip: only the probe runs. T-3809: the
    criteria the gate covers (_covered) don't run here; `info` takes them ("covered": {n: [cmd, files]}), how many
    criteria the lane has ("total") and the seconds the stage took ("s")."""
    from sampo import cli
    seen, info = [], info if info is not None else {}
    t0 = time.monotonic()
    try:
        try:
            if not judged:
                skip = _covered(p, b, list(c.read_meta(p).get("checks") or []))
                info.update(total=len([1 for _, v in b.verify_cmds() if v]), covered={n: list(v) for n, v in skip.items()})
                seen += [(b.id, f"criterion {n} covered by the gate: `{c.fit(v[0], 100)}` runs with the train's tests")
                         for n, v in sorted(skip.items())]
                _verify(b, skip=set(skip))
        except CriterionRed as e:  # T-3629: red once, green on a rerun is a load flake, as _probe treats it
            _verify(b, only=e.n)  # that criterion alone: the others passed
            seen.append((b.id, f"flaky (red once, green on a rerun): {str(e).splitlines()[0][:160]}"))
    except (c.PolicyError, cli.UsageError) as e:
        info["s"] = time.monotonic() - t0
        return str(e).splitlines()[0], seen
    info["s"] = time.monotonic() - t0
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
    """(why or '', notes, info) from a lane's finished check process; its stderr passes on (the criteria's reuse
    lines). T-3809: info is _check's ("covered", "total", "s"), {} when the process said none."""
    if r.stderr:
        sys.stderr.write(r.stderr)
    for line in reversed(r.stdout.splitlines()):
        if line.startswith(MARK):
            got = json.loads(line[len(MARK):])
            return got["why"], [tuple(x) for x in got["seen"]], got.get("info") or {}
    tail = (r.stderr.strip().splitlines() or ["no output"])[-1]
    return f"its check process ended with exit {r.returncode} and no result: {c.fit(tail, 200)}", [], {}


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
    info = {}
    why, seen = _check(p, a["main"], a["base"], cli.need_brief(p, a["id"]), a["tip"], a["tests"],
                    bool(a.get("judged")), info)
    print(MARK + json.dumps({"why": why, "seen": seen, "info": info}))
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


TRAIN_RUNNING = "running.json"  # T-5077: what a train writes under its lock, for the guard (fmguard's trunk-hold rule)


@contextlib.contextmanager
def _train_running(p, main, ids):
    """T-5077: while the train holds its lock, a record of it beside the lock: its process (host|ns|pid|start, so a reused
    pid is told), the checkout it holds, its lanes and when its gate began. The guard reads it to refuse a trunk-moving git
    command in that checkout (2026-11-18: a commit onto foreman/build voided train 4's 3 h 13 min gate). Best effort: a
    write that fails leaves the train running unguarded, as before; the file goes with the train, and a record a killed
    train leaves behind names a dead process, which holds nothing."""
    from sampo import bus
    path = os.path.join(p.dir, "train", TRAIN_RUNNING)
    me = os.getpid()
    info = bus._proc(me)
    if info:
        with contextlib.suppress(OSError):
            c.write_atomic(path, json.dumps({"proc": f"{bus.host_id()}|{me}|{info[1]}", "pid": me, "root": main,
                                             "lanes": list(ids), "at": time.time()}))
    try:
        yield
    finally:
        with contextlib.suppress(OSError):  # only our own record: a later train's is its own
            with open(path) as f:
                mine = json.load(f).get("pid") == me
            if mine:
                os.remove(path)


def train_hold(p, chain=None):
    """T-5077: {pid, root, lanes, at, held} of the train of this project that runs now (its record names a process that is
    alive and began when it said), else None. The train's own process tree (`chain`: pid and ancestors, default this
    process's) holds nothing against itself. Never touches the train's lock: a probe that took it would delay a train."""
    from sampo import bus
    try:
        with open(os.path.join(p.dir, "train", TRAIN_RUNNING)) as f:
            rec = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(rec, dict) or bus.proc_state(rec.get("proc")) != "alive":
        return None
    if chain is None:
        chain = [pid for pid, _ in bus._chain(os.getpid(), 12)]
    if rec.get("pid") in chain:
        return None
    at = rec.get("at")
    if not isinstance(at, (int, float)) or not -1e12 < at < 1e12:  # a damaged time (inf, nan, text) reads as now
        at = time.time()
    lanes = rec.get("lanes")
    return {"pid": rec.get("pid"), "root": rec.get("root"), "lanes": lanes if isinstance(lanes, list) else [], "at": at,
            "held": max(0.0, time.time() - at)}


OVERDUE_X = 2.0  # T-5793: a train running past this many times its typical time is overdue
OVERDUE_MIN_S = 300.0  # ...and past this many seconds: a typical of one minute never makes five overdue
OVERDUE_SAMPLES = 3  # the trains that must have been timed before a typical is claimed (the median of the last 20)


def overdue_train(p, now=None, chain=None):
    """T-5793: the train running now when it has run past twice its typical time, else None: {lanes, age, typical, pid,
    key} (seconds; key names this train, so a line about it is said once). Its age is from its record under the lock (when
    its gate began), the typical the median `seconds` of the last 20 land_train events (3 at least). Reads only: never
    the lock. The pulse and the hook's drive line say it (2026-11-22 a land stood an hour on one train, unremarked)."""
    hold = train_hold(p, chain)
    if not hold:
        return None
    now = time.time() if now is None else now
    age = now - hold["at"]
    if age < OVERDUE_MIN_S:
        return None
    secs = []
    for e in c.ledger_tail(p, 3000, ["land_train"])[-20:]:
        v = finite_num((e.get("data") or {}).get("seconds")) if isinstance(e.get("data"), dict) else None
        if v is not None and v > 0:
            secs.append(v)
    if len(secs) < OVERDUE_SAMPLES:
        return None
    typical = statistics.median(secs)
    if age <= OVERDUE_X * typical:
        return None
    return {"lanes": [str(t) for t in hold["lanes"]][:12], "age": round(age), "typical": round(typical),
            "pid": hold["pid"], "key": f"{hold['pid']}@{int(hold['at'])}"}


def overdue_text(o):
    """T-5793: `train overdue: T-1, T-2 running 90 min (typical 10 min)` for an overdue_train() result."""
    return (f"train overdue: {c.joinmore(o['lanes'], 4)} running {round(o['age'] / 60)} min "
            f"(typical {round(o['typical'] / 60)} min)")


OWN_COMMIT_WAIT_S = 10.0  # T-5368: a train waits this long, in all, for the land's own closer to let go of the lock
_own = {"n": 0, "end": 0.0}  # this process's closer commits: how many run now, and when the last one let go (monotonic)
_own_mu = threading.Lock()


@contextlib.contextmanager
def _own_commit():
    """T-5368: marks this process's close commit, from before it asks for the train's lock until after it lets go. The
    closing file only exists while the lock is held, so a train that asked in the instants before it was written, or
    after it was removed (the lock goes a moment later), found no close and was refused: it stopped the land."""
    with _own_mu:
        _own["n"] += 1
    try:
        yield
    finally:
        with _own_mu:
            _own["n"] -= 1
            _own["end"] = time.monotonic()


def _own_closer(grace=2.0):
    """True while this process's closer commits, or let go of the lock within `grace` s."""
    with _own_mu:
        return _own["n"] > 0 or time.monotonic() - _own["end"] < grace


@contextlib.contextmanager
def _train_run_lock(p, main=None, ids=()):
    """The train's lock for one run: a second train is refused after 1 s, but a hand close's commit is waited for, and so
    is this land's own closer (T-5368: for 10 s in all, however its closing file reads at that instant)."""
    with contextlib.ExitStack() as st:
        try:
            st.enter_context(train_lock(p, timeout=1))  # a second train, not a lane merge, waits on this one
        except c.LockTimeout:
            if _closing(p):
                st.enter_context(train_lock(p, timeout=CLOSE_WAIT_S))  # LockTimeout again if the close outlasts the wait
            elif _own_closer():
                end = time.monotonic() + OWN_COMMIT_WAIT_S
                while True:
                    try:
                        st.enter_context(train_lock(p, timeout=1))
                        break
                    except c.LockTimeout:
                        if _closing(p):
                            st.enter_context(train_lock(p, timeout=CLOSE_WAIT_S))
                            break
                        if time.monotonic() >= end or not _own_closer():
                            raise
            else:
                raise
        if main:
            st.enter_context(_train_running(p, main, ids))
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
    why = paused_stop(p) or machine_stop(p)  # T-5044: a pause refuses every gate; T-4652: a full disk fails every one
    if why:  # known in a second, before any merge or test
        raise c.PolicyError(why)
    took = deferred = False
    try:
        with _train_run_lock(p, main, ids):
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
    moved_maps = {}  # T-5381: lane id: the map entries its merge moved to keep a map file under its cap
    cands, rows = [], []  # (brief, tip) in order; (brief, tip, why so far) in order
    from sampo import lanes, release
    try:  # T-4804: unreadable is not unset: the merge scan refuses each lane on it (held, nothing pushed); this only says so
        c.private_terms()
    except c.PrivateTermsUnreadable:
        print("sampo: train: " + c.PRIVATE_UNREADABLE + ": every lane is held by the private-term scan until it is repaired "
              "(sampo doctor)", file=sys.stderr, flush=True)
    else:  # T-4783: an unset list holds nothing: say so, never the path (T-4785: an empty one is said apart)
        release.say_no_list("train")
    todo = []  # T-5465: (row index, brief, tip) whose holds run HOLD_JOBS at once; rows keep the candidate order
    for tid in ids:
        b = cli.need_brief(p, tid)
        br = b.meta.get("lane_branch") or f"foreman/{b.id}"
        if b.meta.get("lane_spike"):
            rows.append((b, None, "a spike is never merged"))
        elif not _ref_ok(main, br):
            rows.append((b, None, f"no branch {br}"))
        else:
            tip = _git(main, "rev-parse", "--verify", f"{br}^{{commit}}").stdout.strip()
            if b.id in judged and judged[b.id] != tip:
                rows.append((b, tip, MOVED))
            elif judged.get(b.id) == tip:
                rows.append((b, tip, ""))
            else:
                todo.append((len(rows), b, tip))
                rows.append((b, tip, None))
    for i, why in zip([i for i, _, _ in todo], _holds(p, main, [(b, tip) for _, b, tip in todo])):
        rows[i] = (rows[i][0], rows[i][1], why)

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
    jobs = 1 if c.host_strain(2.0) else PROBE_JOBS
    live = live_mode(p)  # T-5444: no criteria stage and no front gate: the behind suite judges the merged head
    outs = iter(_fan([_child_argv(p, main, base, b.id, tip, probe_tests, judged.get(b.id) == tip)
                      for b, tip, why in rows if not why], main, jobs)) if not live else iter(())
    infos = {}  # T-3809: lane id: what its check process said of the criteria stage (covered, total, s)
    for b, tip, why in rows:
        seen = []
        if not why and not live:
            why, seen, infos[b.id] = _child_result(next(outs))
            if why.startswith(WT_GONE):  # T-4728: a probe's tree lost twice: the train stops, no lane is left out for it
                raise c.PolicyError(why)
        notes += seen
        left.append((b.id, why)) if why else cands.append((b, tip))
    t1 = time.monotonic()
    if not cands:
        raise _none("nothing to merge: ", left)
    from sampo import mapmerge
    mapmerge.ensure(main)  # T-5472: the key-aware map driver .gitattributes names, in the config the tree shares
    wt = c.scratch_tree("sampo-train-", main)  # outside the main checkout; git worktree add takes the empty dir (T-4735: in the scratch root)
    r = _git(main, "worktree", "add", "-q", "--detach", wt, base)
    if r.returncode:
        shutil.rmtree(wt, ignore_errors=True)  # T-4735: before the try below: nothing else would remove the tree and its lock
        c.scratch_release(wt)
        raise c.PolicyError(f"git worktree add failed: {r.stderr.strip()[:300]}")
    runs, probes, results, sub = [0], [0], [], None
    olds, head, lost = [wt], [base], [0]  # T-4728: the folders the train made; the commit the tree is at; losses so far
    tips ={b.id: t for b, t in cands}
    cov = {i: {int(n): v for n, v in (x.get("covered") or {}).items()} for i, x in infos.items()}  # T-3809: JSON keys are strings

    def covers(i):
        """The test files lane i's covered criteria name (repo-relative paths at its tip)."""
        return [f for _, fs in cov.get(i, {}).values() for f in fs]
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
            sha = _merge_in(wt, b, tip, left, kept_maps, moved_maps)
            if not sha and wt_gone():  # T-4728: the tree went under the merge: that is no conflict, and no lane is left out for it
                del left[n:]
                ensure_wt()  # a second loss stops the train here
                sha = _merge_in(wt, b, tip, left, kept_maps, moved_maps)
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
        the train runs it: seconds or the suite's part, where a whole gate run took minutes. T-4986: every such lane
        of one red run, [(index + 1, the classes of `names` it turns red, its probes)]: the classes still green at a
        blamed lane's prefix are bisected again over the lanes after it, in this round (train 3b found one culprit per
        40-100 minute round). With no names, or a probe that names no failing class, one lane for all of them. A class
        red only with an earlier culprit and a later lane both in leaves the later one out too: one train's wait for
        that lane, where finding it a round later cost every lane that round."""
        found, todo, start = [], list(names), 0
        while True:
            lo, hi, red, n0 = start, len(merged), None, probes[0]
            while hi - lo > 1:
                mid = (lo + hi) // 2
                row = probe(merged[mid - 1][1], cmd, todo)
                if row and row["exit"]:
                    hi, red = mid, row
                else:
                    lo = mid
            if hi == 1 and check:  # the first lane is blamed on the strength of a base nobody probed: probe it before blaming
                base_check(cmd, todo)
            failing = (red or {}).get("failing") or ()
            mine = [n for n in todo if any(_same(f, n) for f in failing)] if failing else list(todo)
            found.append((hi, mine or list(todo), probes[0] - n0))
            todo = [n for n in todo if n not in mine] if mine else []
            if not todo:  # the rest were green at lane hi's prefix and red on the whole tree: a lane after it
                return found
            start = hi

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

    def owner_probe(blamed, g, explained, who):
        """T-3809 review: a module a lane's criterion named no longer ran on base + that lane before the gate (the gate
        runs it): when rule (b) blames a lane for such a red name only because the map links its diff to the module, the
        lane that carries the module (it covers it) is probed first, alone on the base, on that name. Red there: it is
        the one to blame, and the map-linked lane stays in; the other names that lane alone explained (`who`: name ->
        lane) leave `explained`, so this round bisects them. Green (or no owner): the weak blame stands, and the probe's
        build leaves `left` and `kept_maps` as they were."""
        out = dict(blamed)
        for i, (rule, k) in blamed.items():
            if not (rule.startswith(_WEAK) and k):
                continue
            for o in (x.id for x, _ in merged if x.id != i and _mod(k) in {_stem(f) for f in covers(x.id)}):
                n, was, kept = len(left), here(), {m: list(fs) for m, fs in kept_maps.items()}
                moves = {m: list(fs) for m, fs in moved_maps.items()}
                got = build(base, [(x, tips[x.id]) for x, _ in merged if x.id == o])
                del left[n:]
                kept_maps.clear()  # build's merge pops and re-adds the lane's entry: put the train's own back
                kept_maps.update(kept)
                moved_maps.clear()
                moved_maps.update(moves)
                row = probe(got[0][1], g, [k]) if got else None
                reset(was)
                if row and row["exit"] and any(_same(f, k) for f in row.get("failing") or ()):
                    del out[i]
                    out.setdefault(o, (f"it carries {_mod(k)}, red with it alone on the base", k))  # never over its first
                    explained -= {m for m, l in who.items() if l == i and m != k}
                    break
        return out

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

    def shape_split(first):
        """T-5806: the live batch whose merged tree is red on the repo-shape classes (`first`): the batch less the lane(s)
        _shape_cull blames, each left out with the classes named (its log, `left`), rebuilt on the base. Each subset is
        merged on a fresh tree and judged once per tree (a verdict is kept per tree hash, the batch's own included). A cut
        run is no verdict: the land stops with nothing parked, as before. The lanes whose diff (or the tests it reaches)
        touches a red class's module are tried first.
        T-5853: a subset tree runs only the red classes (`first.names`: seconds, where the whole set took minutes under
        load); the kept build then gets one whole run before it lands (none when it is the batch's own tree, whose
        narrowed rerun cleared its red). A class red only there (a subset never ran it) gets one more split on it; a
        red again, or a cut, ends the land with nothing parked: the lanes are marked only once the build is green."""
        if first.verdict != "red":
            raise c.PolicyError("the merged batch's repo-shape run did not pass, nothing landed: " + first.why)
        if _base_shape_cached(p, base) is None:  # _live_shape ran the base for this red and got no verdict
            raise c.PolicyError(f"the repo-shape run of the base {base[:9]} has no verdict (cut at {SHAPE_BATCH_WALL_ENV}="
                                f"{shape_batch_wall()} s, or its tree would not build), so no lane can be blamed for a class "
                                f"the base may carry: nothing landed and no lane parked ({first.why})")

        def split(first, pool):
            """(parked {id: (why, classes)}, the kept build, its whole-set ShapeVerdict, trees judged) for one red."""
            ids_m = [b.id for b, _ in pool]
            ensure_wt()
            tree0 = _git(wt, "rev-parse", "HEAD^{tree}").stdout.strip()
            seen = {tree0: first}  # narrowed verdicts per tree (the batch's own red names exactly those classes)

            def shape(sel, fresh=False):
                trunk_watch()  # a moved trunk ends the search: its verdicts are of a base that is gone
                n = len(left)
                build(base, [(b, tips[b.id]) for b, _ in pool if b.id in sel])
                conflicts, left[n:] = left[n:], []  # a merge conflict of a subset is the final build's to name
                if conflicts:  # the lane is judged absent there: no verdict, as a cut
                    return ShapeVerdict("cut", f"the subset does not merge ({conflicts[0][0]}: {conflicts[0][1]})")
                ensure_wt()
                key = _git(wt, "rev-parse", "HEAD^{tree}").stdout.strip()
                if key in seen and not fresh:
                    return seen[key]._replace(secs=0.0)  # a cache hit costs no shape seconds
                # base-relative: a class red on the base is no lane's; T-5853: only the red classes run
                seen[key] = _live_shape(wt, p, main, base, first.names)
                return seen[key]

            mods = {_mod(n) for n in re.split(r"[,\s]+", first.names) if n}
            sus = {i for i in ids_m if mods & ({_stem(f) for f in diff(i)} | {_stem(t) for t in reach(i)})}
            notes = []  # T-5880: (class, base rerun verdict, lanes) for a class red in every lane's subset tree
            parked = _shape_cull(ids_m, shape, sus, base_rerun=lambda k: _base_shape_rerun(p, main, base, k), notes=notes)
            for k, verdict, n in notes:
                print(f"sampo lane land: repo-shape class {c.fit(c.plain(k), 100)} red in all {n} subset trees: base rerun "
                      f"{verdict}, no lane parked for it", file=sys.stderr, flush=True)
                c.log_event(p, "lane_train_shape_all_red", data={"class": c.fit(c.plain(k), 100), "lanes": n, "base_rerun": verdict,
                                                                 "kind": "base red" if verdict == "red" else "flaky under load"})
            kept = build(base, [(b, tips[b.id]) for b, _ in pool if b.id not in parked])
            ensure_wt()
            if not kept or _git(wt, "rev-parse", "HEAD^{tree}").stdout.strip() == tree0:
                return parked, kept, ShapeVerdict("green"), len(seen)
            trunk_watch()
            return parked, kept, _live_shape(wt, p, main, base), len(seen)  # the one whole run of the kept build

        ids_m, pool, parked, trees, classes = [b.id for b, _ in merged], merged, {}, 0, []
        for rnd in (1, 2):
            got, pool, full, n = split(first, pool)
            parked.update(got)
            trees += n
            classes.append(first.names)
            if full.verdict == "green":
                break
            if full.verdict == "cut" or rnd == 2:
                raise c.PolicyError(full.why + (f" (the split of {classes[-1]} left it red)" if full.verdict == "red" else "")
                                    + ": nothing landed and no lane parked")
            first = full
        for i in (i for i in ids_m if i in parked):
            left.append((i, parked[i][0]))
            cli.mutate(p, i, lambda x, w=parked[i][0]: x.append_log("left out of a merge train: " + w), "lane_train_red",
                       {"cmd": "repo-shape", "failing": [n for n in re.split(r"[,\s]+", parked[i][1]) if n],
                        "blamed": "shape bisection"})
        c.log_event(p, "lane_train_shape_cull", data={"parked": sorted(parked), "kept": [i for i in ids_m if i not in parked],
                                                       "classes": ", ".join(classes), "trees": trees})
        return pool

    try:
        merged = build(base, cands)
        ensure_wt()
        sub = _affected_gate(p, wt, base, checks)
        if moved():  # a lane merged meanwhile: don't spend a gate run on a base that is gone
            raise c.PolicyError(moved_why("since the train started"))
        why = _base_red(p, main, base, sub) if sub and merged and not live else ""
        if why:  # T-3625: the base's affected tests first: a red base is known in minutes
            raise c.PolicyError(f"the base {base[:9]} of {branch} is red; fix it before a train can blame a lane: red on "
                                f"the base (no lane dropped for it): {why}")
        if sub:  # T-3809: the modules a merged lane's covered criteria name run in this gate (after the base probe: a new
            # lane's module is no test of the base); form() keeps those the tree has
            sub = (*sub[:2], list(dict.fromkeys([*sub[2], *(f for b, _ in merged for f in covers(b.id))])))
        if live and merged and (smoke := _live_smoke(wt)):  # T-5444: the one front check: the merged tree compiles
            raise c.PolicyError(f"the merged tree does not compile, nothing landed: {smoke}")
        if live and merged and (shape := _live_shape(wt, p, main, base)).verdict != "green":  # T-5590, T-5805: the batch's new repo-shape reds, once
            merged = shape_split(shape)  # T-5806: a red parks the lane(s) that bring it and the rest land; a cut stops the land
        if live and merged and (slow := _live_timing(p, main, wt, base)):  # T-5446: the hooks timed once per batch
            raise c.PolicyError("the self-change gate holds the merged batch, nothing landed: " + c.joinmore(slow, 4, "; ")
                                + "; the next land times each lane and leaves out the one that slowed the hooks")
        ok = not merged or live or gate()
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
            explained, who = set(), {}
            blamed = _blame(red["out"], names or red["failing"], ids, diff, reach, explained, who)
            if blamed and suite and g == suite and any(cov.values()):  # T-3809: the lane carrying a covered module first
                blamed = owner_probe(blamed, g, explained, who)
            mine = {}  # T-4986: a bisected lane's own classes: what it is left out for, and all its line names
            rest = [n for n in names if n not in explained] if blamed else list(names)
            if not blamed or rest:  # T-4986 review: what no lane's file explains is bisected in this round too
                for at, own, spent in bisect(g, rest, not cls):
                    only = "only " + c.joinmore(own, 4) if own else "the whole red gate"
                    blamed.setdefault(ids[at - 1], (f"bisection, {spent} probe(s) of {only}", ""))
                    mine.setdefault(ids[at - 1], own)
            texts = _texts(red["out"], red.get("failing") or [])
            for i in (i for i in ids if i in blamed):
                rule, k = blamed[i]
                own = dict(shown, failing=[f for f in shown.get("failing") or () if any(_same(f, n) for n in mine[i])]) \
                    if mine.get(i) and not k else shown
                why = "its merge turns the gates red: " + _why_red(own, rule, texts[k].rstrip().splitlines()[-15:]
                                                                   if k else None)
                left.append((i, why))
                # T-4289: what it was left out for (put back if the base is red on it); T-4986: named and bisected, both
                dropped[i] = _left_for(k, mine.get(i) or names) | (set(mine[i]) if k and mine.get(i) else set())
                cli.mutate(p, i, lambda x, w=why: x.append_log("left out of a merge train: " + w), "lane_train_red",
                           {"cmd": red["cmd"], "failing": (own if own is not shown else red).get("failing") or [],
                            "blamed": rule})
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
        green = [last[g] for g in checks] if merged and not live else []  # T-5444: live ran no gate: no pass to record
        if not merged:
            raise _none("nothing landed: ", left)
        if not live:
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
        unions = [(i, kept_maps[i + TESTS_KEY]) for i in ids_in if i + TESTS_KEY in kept_maps]  # T-5824
        if unions:
            c.log_event(p, "test_union", data={"lanes": {i: fs for i, fs in unions}})
        part = next((r["affected"] for r in green if r.get("affected")), None) or (live and "live")  # T-5444: verified behind
        if part:  # T-3544: the full suite the gate skipped verifies each landed merge in the background, newest first
            for i, (b, sha) in enumerate(merged):  # T-4718: a caller that ships next starts the verify after the ship asked
                pm_record(p, sha, b.id, main, spawn=not defer_verify and i == len(merged) - 1)
        tree = _git(main, "rev-parse", f"{tip}^{{tree}}").stdout.strip()
        ct = c.code_tree(main, tree)
        if not live:  # T-5444: a live train ran no gate; its merges' verify is the behind suite's record
            c.log_event(p, "check_run", data={"tree": tree[:12], "code_tree": ct, "env": c.env_id(), "train": ids_in,
                                              "results": green})
        if suite and last.get(suite):  # T-3809: each covered criterion's evidence is this gate's pass, on the tree that landed
            notes += _record_covered(p, merged, cov, last[suite], tree[:12])
        t3 = time.monotonic()
        # T-3809: the criteria stage on its own: the lanes' criteria seconds spread over the lanes run at once, no more
        # than the fan took; the probes' minutes are what is left of it
        ran_c = [x for x in infos.values() if "s" in x]
        crit = min(t1 - t0, sum(x["s"] for x in ran_c) / max(1, min(jobs, len(ran_c))))
        # "covered K of M" counts every lane the criteria stage checked, those later left out too: it says what the stage
        # skipped, not what the landed lanes' evidence took
        mins = {"probe": round(max(0.0, t1 - t0 - crit) / 60, 1), "criteria": round(crit / 60, 1),
                "covered": sum(len(x.get("covered") or ()) for x in ran_c), "criteria_total": sum(x.get("total", 0) for x in ran_c),
                "gate": round(max(0.0, t2 - t1 - confirm_s[0]) / 60, 1),
                "confirm": round(confirm_s[0] / 60, 1), "land": round((t3 - t2) / 60, 1)}  # T-4676: gate + confirm = the gates
        return {"merged": ids_in, "left_out": left, "notes": notes, "kept": kept, "map_merged": maps,
                "test_merged": unions, "map_follow_up": map_task, "gate_runs": runs[0], "verify_owed": bool(part and defer_verify),
                "map_moved": [(i, "; ".join(moved_maps[i])) for i in ids_in if moved_maps.get(i)],  # T-5381
                "probes": probes[0], "tip": tip, "branch": branch, "affected": part, "minutes": mins, "probes_skipped": sum(
                    w.startswith(SKIPPED) for _, w in notes), "lanes_probed": len(rows)}
    finally:
        for folder in olds:  # T-4728: each one the train made (the first, and one rebuilt after a loss), nothing else
            _git(main, "worktree", "remove", "--force", folder)
            shutil.rmtree(folder, ignore_errors=True)
            c.scratch_release(folder)
        _git(main, "worktree", "prune")


def _map_follow_up(p, maps):
    """T-4375: one captured follow-up (source followup) for a train that merged lanes keeping both versions of a map line
    both sides rewrote (T-5472: only those), naming each lane and file: one of the two must go. One ledger
    event says what was merged. Returns the new task's id, '' when the capture failed (the landing never waits on it)."""
    from sampo import cli
    what = "; ".join(f"{i}: {', '.join(fs)}" for i, fs in maps)
    text = (f"Read the docs/map entries the train merged keeping both sides' lines ({what})\n\nThe lane train merged "
            "these lanes keeping both versions of a map line both sides rewrote (sampo.mapmerge, T-5472): read each "
            "merged entry and keep one.")
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


def _move_lines(got):
    """T-5381: the report's line for each landed lane whose map entries the train moved to keep a map file under its cap."""
    return [f"  moved {i}: {what}" for i, what in got.get("map_moved") or []]


def _cmd_report(p, args, main, close, got, owed):
    from sampo import cli
    lines = [f"Train: {len(got['merged'])} lane(s) landed on {got['branch']} ({got['tip'][:9]}) in "
             f"{got['gate_runs']} gate run(s)" + (f" and {got['probes']} probe(s) of only the failing tests"
                                                  if got["probes"] else "") + f": {', '.join(got['merged'])}"]
    lines += [f"  left out {i}: {w}" for i, w in got["left_out"]] + [f"  note {i}: {w}" for i, w in got["notes"]]
    lines += [f"  merged {i} keeping both sides' lines in {', '.join(fs)} (both rewrote one map line)"
              for i, fs in got.get("map_merged") or []]  # T-4375
    lines += [f"  merged {i} keeping both sides' added lines in {', '.join(fs)} (both only added lines at one spot)"
              for i, fs in got.get("test_merged") or []]  # T-5824
    lines += _move_lines(got)
    if got.get("map_follow_up"):
        lines.append(f"  follow-up {got['map_follow_up']}: read the merged map entries (a line both sides rewrote is "
                     "kept twice)")
    m = got["minutes"]  # T-3596: where the time went; the serial closes after the train are not in it
    lines.append(f"Minutes: probe {m['probe']}, criteria {m.get('criteria', 0.0)} min, covered {m.get('covered', 0)} of "
                 f"{m.get('criteria_total', 0)}, gate {m['gate']}, confirm {m.get('confirm', 0.0)}, land {m['land']}; "
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
        lines += _hand_close_lines(left)  # T-5516: grouped by reason, then the per-lane lines
    try:
        sh = _ship(p, False, main) if got["merged"] else None  # T-3849: a train by hand ships too, on a grant already given
    finally:
        start_verify(p, main, owed)  # T-4718: in line, so the ship has ended: the verify's suite runs take the slots after
    if sh:
        got["ship"] = sh[1]
        lines.append(f"Ship ({sh[0]}'s grant): {c.fit(c.plain(sh[1]), REFUSAL_LINE)}")
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


REFUSAL_ROOM = 600  # T-5046: the width of a ship's refusal (release._full_refusal fits its clauses to it)
REFUSAL_LINE = REFUSAL_ROOM + 100  # and of the line that carries it wherever it is kept or shown: room for "refused: " and a "; failing …" tail
SHIPPED_AT = re.compile(r"\bmain at ([0-9a-f]{40})\b")  # release.ship's closing line: the commit it pushed
# T-5058: release._ship_gate's line when a running ship already carries every commit: that ship pushes them and spends
# the grant itself, so a land's last ship that printed it never spends it (twice on 2026-11-19 a land's last ship did,
# and the hand ship it named was refused at its push after its tests)
COVERED = re.compile(r"^nothing to ship here: .* already carries every commit", re.M)


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
    why = _why("\n".join(lines[mine[-1]:]) if mine else out, REFUSAL_ROOM)  # T-5046: a full refusal names its failing tests, then the path
    line = ((out.strip().splitlines() or ["shipped"])[-1] if not rc else
            "refused: " + why + (f"; failing {c.joinmore(red, 8)}" if red and not any(k in why for k in red) else ""))
    data = {"head": head, "sha": sha, "seconds": round(t1 - t0, 1), "exit": rc, "start": round(t0, 1),
            "end": round(t1, 1), "red": red[:20]}
    try:
        c.log_event(p, "ship_done", task=tid, data=data)
        c.write_atomic(os.path.join(p.dir, "ship-last.json"), json.dumps({**data, "grant": tid, "seen": False,
                                                                           "line": c.fit(c.plain(line), REFUSAL_LINE)}))
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
    line = (out.strip().splitlines() or ["shipped"])[-1] if not rc else "refused: " + _why(err or out, REFUSAL_ROOM)
    _, data = _ship_end(p, grant.id, head, rc, out + err, t0, time.time())
    if rc and data["red"] and not any(k in line for k in data["red"]):  # T-4739: the summary carries the classes
        line += f"; failing {c.joinmore(data['red'], 8)}"
    shown = (out + err).splitlines()[-5000:]
    _ship_log(p, "".join(ln + "\n" for ln in shown) + f"{_hms()} {c.fit(c.plain(line), REFUSAL_LINE)}\n")
    if rc:
        return grant.id, line
    if keep:  # review: only this ship's own push (its own "main at" line) spent it; else someone else's spend stands
        if SHIPPED_AT.search(out):
            _give_back(p, grant.id)
    elif COVERED.search(out):  # T-5058: the running ship it named carries these commits and spends the grant itself
        pass
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
        if b is not None and "publish" in (b.meta.get("allow") or []) and not COVERED.search(out or ""):  # T-5058
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
            print(f"{_hms()} {c.fit(c.plain(line), REFUSAL_LINE)}", flush=True)
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


def finite_num(v):
    """v as a finite float, or None: a ledger field is data; a bool, a string or a NaN is not a time."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return float(v)


def train_accepts(d):
    """(at, [accept epochs]) of one land_train event's data, or None when its `at` is not a time."""
    if not isinstance(d, dict):
        return None
    at = finite_num(d.get("at"))
    if at is None:
        return None
    acc = d.get("accepted")
    return at, ([a for a in map(finite_num, acc.values()) if a] if isinstance(acc, dict) else [])


def pushed_seconds(evs, trains):
    """The one accept-to-pushed rule (status and sampo measure): for each (at, accepts) train, each accept's seconds to the
    end of the first ship_done with exit 0 that started at or after the train's `at` (first by start, whatever order the
    ledger holds them); a train no ship has followed gives none."""
    ships = []
    for e in evs:
        d = e.get("data") if isinstance(e, dict) and e.get("event") == "ship_done" else None
        if isinstance(d, dict) and d.get("exit") == 0 and finite_num(d.get("start")) is not None \
                and finite_num(d.get("end")) is not None:
            ships.append((float(d["start"]), float(d["end"])))
    got = []
    for at, accepts in trains:
        later = [s for s in ships if s[0] >= at]
        if later:
            end = min(later)[1]
            got += [max(0.0, end - a) for a in accepts]
    return got


def last_land(p):
    """T-4239: the newest land's trains from the ledger: {lanes, trains, gate, wait (minutes), live}: `live` (accept-to-pushed) is each
    accepted lane's seconds from its accept to the end of the first successful ship that started after its train. None
    when no land has recorded a train."""
    evs = c.ledger_tail(p, 3000, ["land_train", "ship_done"])
    trains = [e for e in evs if e.get("event") == "land_train"]
    if not trains:
        return None
    lid = (trains[-1].get("data") or {}).get("land")
    mine = [e["data"] for e in trains if (e.get("data") or {}).get("land") == lid]
    live = pushed_seconds(evs, [x for x in map(train_accepts, mine) if x])
    return {"land": lid, "lanes": sum(len(d.get("lanes") or []) for d in mine), "trains": len(mine),
            "gate": round(sum(d.get("gate_s", 0) for d in mine) / 60, 1),
            "wait": round(sum(d.get("wait_s", 0) for d in mine) / 60, 1), "live": live}


def land_line(p):
    """T-4239: sampo status's line about the newest land, or ''."""
    got = last_land(p)
    if not got:
        return ""
    tail = (f"accept-to-pushed median {statistics.median(got['live']) / 60:.1f} min" if got["live"] else
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


def _why(text, limit=400):
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
    return c.fit("; ".join(ln for ln in lines if ln), limit) or "no reason given"


def _park_kind(why):
    """The lane_stop (kind, arg) a train's reason maps to, as `lane merge` marks the same refusals (lanes.STOP_CAUSES)."""
    if why.startswith("conflicts with"):
        return "conflict", ""
    if why.startswith("the self-change gate"):
        return "guard", "core"
    if why.startswith("the guard refuses"):
        return "guard", why.rpartition("(")[2].rstrip(")")
    return "refused", ""


def _pilot_parks(p, parked, tick=True):
    """T-5766: after the land marked its parks, the lane pilot is asked for each conflict park (a refusal or a guard's grant
    is a person's): one detached headless resolver, under the pilot's single-flight claim, 2 h cap and SAMPO_LANE_PILOT
    switch (pilotsig.resolve is quiet where none starts), then one pilotsig.tick for the lanes stopped before any merge.
    One lane's failure never costs the land or the other lanes."""
    from sampo import pilotsig
    for tid, why in parked:
        if _park_kind(why)[0] != "conflict":
            continue
        try:
            pilotsig.resolve(p, tid)
        except Exception as e:  # noqa: BLE001 - the land goes on; the lane waits as it did before the pilot
            print(f"lane pilot: {tid}: no resolver: {c.fit(str(e), 200)}", file=sys.stderr)
    if not tick:  # T-5876: a park's own ask (note_park) is for that lane; the land's end ticks for the rest
        return
    try:
        pilotsig.tick(p)  # the stopped (uncommitted) lanes: the one other start; the lead's reads start nothing
    except Exception as e:  # noqa: BLE001
        print(f"lane pilot: tick: {c.fit(str(e), 200)}", file=sys.stderr)


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
    why = paused_stop(p) or machine_stop(p)  # T-5044: a pause, T-4652: the disk, refuse the land at once, before a lane
    if why:  # is parked or a batch trains
        raise c.PolicyError(why)
    if why := held_by_active(p, main):  # T-4692: an unrelated focus holds nothing; uncommitted lane files do
        _note_refusal(p, why)
        raise c.PolicyError(why)
    with contextlib.ExitStack() as held:
        try:
            held.enter_context(c.lock(os.path.join(p.dir, "land"), timeout=1))
        except c.LockTimeout:
            raise c.PolicyError("another sampo lane land is running; it lands these lanes too") from None
        return _land(p, main, batch)  # T-5848: None stays None down to _land_trains: only an explicit --batch caps express lanes


def first_ids(p, add=(), peek=False):
    """T-3796: the ids sampo lane land --first put ahead of the rest: `add` appends to them (returning them all);
    with none, they are taken (returned and emptied) at a train boundary. T-5368: `peek` only reads them."""
    d = os.path.join(p.dir, "land-first")
    path = os.path.join(d, "ids")
    with c.lock(d):
        try:
            with open(path) as f:
                have = f.read().split()
        except OSError:
            have = []
        if peek:
            return have
        keep = list(dict.fromkeys([*have, *add])) if add else []
        if keep or have:
            c.write_atomic(path, " ".join(keep))
    return keep if add else have


def _restart_carry(p, ready=None, alone=()):
    """T-5023: what a restarting land hands its successor besides the --first ids: the lanes it left ready, and which
    of them were express from --first (`alone`). Given `ready`, it writes them; without, it takes them (read and
    emptied): the successor promotes an express lane named --first, never one that only sat in the restart queue."""
    d = os.path.join(p.dir, "land-first")
    path = os.path.join(d, "carry")
    with c.lock(d):
        if ready is not None:
            c.write_atomic(path, json.dumps({"ready": list(ready), "alone": [t for t in ready if t in set(alone)]}))
            return None
        try:
            with open(path) as f:
                got = json.load(f)
            os.unlink(path)
        except (OSError, ValueError):
            got = {}
    got = got if isinstance(got, dict) else {}  # a hand-edited file is read as far as it makes sense
    return {k: [t for t in got[k] if isinstance(t, str)] if isinstance(got.get(k), list) else [] for k in ("ready", "alone")}


# T-5735: the files the lane pipeline itself runs on (a path ending in / is a folder). A lane whose diff touches one is a
# fix to the pipeline: it trains first, so the next land (and every loop that re-execs at a unit boundary) runs it.
# The land and train, lane brief/accept/start/recover (train, lanes and their helpers), the ship and the affected
# selection (release), the check gates the merge runs (selfgate, hooksmoke), the hot hooks and guard (hooks.py, the
# hooks/ entries and hooks.json), the test runner (tests/run.py) and the bin entries.
PIPELINE_PATHS = tuple("plugin/lib/sampo/" + f for f in (
    "train.py", "lanes.py", "landlock.py", "lane_overlap.py", "lane_preview.py", "mapmerge.py", "bisecthunk.py",
    "recover.py", "reverify.py", "greenref.py", "pilotsig.py", "ahead.py", "release.py", "selfgate.py", "hooksmoke.py",
    "hooks.py")) + ("plugin/hooks/", "plugin/tests/run.py", "plugin/bin/")
# cli.py (12k lines) and core.py (10k) are touched by most lanes: a whole-file match would make nearly every lane express.
# Only a change inside one of these top-level functions counts: the close/finish and check paths, and `sampo next`.
PIPELINE_FUNCS = {
    "plugin/lib/sampo/cli.py": ("task_finish", "_close_affected", "_finish_gaps", "affected", "_check_affected", "cmd_check",
                                "_affected_why"),
    "plugin/lib/sampo/core.py": ("next_for", "lock", "scratch_tree"),
}
_PIPE_TIPS = {}  # tip -> whether its diff touches the pipeline: a lane's tip never changes its diff, so asked once


def touches_pipeline(files):
    """T-5735: whether any of these repo paths is one of PIPELINE_PATHS (a file of PIPELINE_FUNCS is not: pipeline_lane
    reads which functions changed)."""
    return any(f in PIPELINE_PATHS or any(f.startswith(x) for x in PIPELINE_PATHS if x.endswith("/")) for f in files)


def _touches_pipeline_funcs(main, tip, files):
    """T-5735: whether the lane's diff in a PIPELINE_FUNCS file lies in one of its named functions: git's hunk header
    (-U0) names the top-level def a change sits under, and a def the change adds or removes is named by its own line."""
    for f in (f for f in files if f in PIPELINE_FUNCS):
        r = _git(main, "diff", "-U0", f"HEAD...{tip}", "--", f, timeout=30)
        if r.returncode:
            continue
        names = set(PIPELINE_FUNCS[f])
        for ln in r.stdout.split("\n"):
            m = (re.match(r"@@ [^@]*@@ (?:async )?def (\w+)", ln) if ln.startswith("@@")
                 else re.match(r"[+-](?:async )?def (\w+)", ln))
            if m and m.group(1) in names:
                return True
    return False


def pipeline_lane(main, tip):
    """T-5735: whether the lane at `tip` changes a pipeline file or function (its diff from where it left trunk). Cached
    per tip: a land asks again at every sort, readmit and recheck, and an unchanged lane costs no git process. A tip
    git could not diff is no pipeline lane, and not cached (the next ask tries again)."""
    if not tip:
        return False
    if tip not in _PIPE_TIPS:
        r = _git(main, "diff", "--name-only", f"HEAD...{tip}", timeout=30)
        if r.returncode:
            return False
        if len(_PIPE_TIPS) > 4000:
            _PIPE_TIPS.clear()
        files = r.stdout.split("\n")
        _PIPE_TIPS[tip] = touches_pipeline(files) or _touches_pipeline_funcs(main, tip, files)
    return _PIPE_TIPS[tip]


def land_key(b, pipe=False):
    """T-3796: the order lanes train in: urgent, then performance and security fixes, then the user's own asks, then
    the rest (discovered work, follow-ups), oldest first in each. T-5735: a pipeline lane (`pipe`) leads them all."""
    kind = 0 if b.type in ("PERFORMANCE", "SECURITY") else 1 if b.meta.get("source") == "user" else 2
    return (not pipe, b.priority != "urgent", kind, c.id_num(b.id))


def is_express(b, pipe=False):
    """T-5023: a performance fix, a security fix or an urgent lane (the ones land_key sorts first): named --first, it
    trains next and alone, a batch of one, so no ordinary lane's gate holds it back. The gate itself is never skipped.
    T-5726: in live mode the express lanes at the head train together instead (one train; T-5848: all of them, an explicit --batch aside).
    Several express lanes train in the order they were named --first (FIFO), not re-sorted by land_key.
    T-5735: a lane that touches the pipeline (`pipe`, from pipeline_lane) is express whatever its type, named or not."""
    return pipe or b.type in ("PERFORMANCE", "SECURITY") or b.priority == "urgent"


def _stale_affected(b, sha, main):
    """T-3963: the note for a lane whose newest `sampo check --affected` run came before its own last commit (a merge of
    main's branch is not its work), else None. 2026-11-08 left out three lanes whose scans and exit-code tests only the
    rerun would have reached. T-4767: the land holds such a lane unless its check is green at the tip (_unchecked), so
    this note only reaches one whose green check postdates its stale evidence."""
    if _affected_state(b, sha, main)[0] == "stale":
        return (f"its newest affected run predates the lane tip {sha[:9]}: rerun sampo check --affected --evidence "
                f"{b.id} in its lane after the last commit")
    return None


LAND_CHECKS = 3  # T-5561: checks running at once (any lane's) past which the land starts no further stale lane's check


STALE_RETRY_S = 600.0  # T-5561 review: a lane whose check would not start is not tried again in this land for this long


def _live_checks(p, briefs=None):
    """T-5561: the lane checks running now in the project, started by an accept or a land alike (`briefs`: already loaded)."""
    return sum(1 for x in (c.load_briefs(p) if briefs is None else briefs)
               if x.meta.get("check_state") == "running" and _check_alive(x.meta))


def _stale_start(p, b, main, sha, failed=None, live=None):
    """T-5561: the land's own rerun of a stale lane's check. A lane whose newest affected run predates its tip `sha` (a
    fixer's commit after accept) waited for a hand-run `sampo check`: 11 lanes sat out a round on 2026-11-22. With no check
    on record at this tip, the lane's check starts as accept starts it (_start_check: in its lane, niced, behind the slot
    gate) and the lane is held while it runs (the returned `held: …` line); once LAND_CHECKS run it waits for one to end.
    None: not a stale lane, a check at this tip already on record (running, red, cut off or green: _check_hold's and the
    land's own rules apply), or none could start (paused, no background): the land holds or trains it as before.
    Review: `failed` (lane id -> when the land may try it again) notes a start that did not happen, so the land's 15 s
    polls do not start it again at once; `live` is a callable giving the running checks, counted once per poll."""
    m = b.meta
    if _affected_state(b, sha, main)[0] != "stale" or (m.get("check_state") and m.get("check_tip") == sha):
        return None
    note = f"its newest affected run predates the lane tip {sha[:9]}"
    if (n := live() if live else _live_checks(p)) >= LAND_CHECKS:
        return (f"held: {note}: {n} checks run already (the land runs at most {LAND_CHECKS}); its check starts when one ends "
                "and it joins the land when green")
    retry = time.monotonic() + STALE_RETRY_S
    try:  # priority 0 (low=True): the train gate's own runs, which wait on a slot at 1, keep first claim
        started = _start_check(p, b, main, sha, _check_since(main, b, sha), 1, low=True)
    except _ClaimLost:  # another process started it first: it runs
        return _check_hold(c.find_brief(p, b.id) or b, sha, main)
    except Exception as e:  # noqa: BLE001 — a check that cannot start leaves the lane as it was, never a land's crash
        print(f"sampo lane land: {b.id}: its check did not start: {type(e).__name__}: {c.fit(str(e), 200)}",
              file=sys.stderr, flush=True)
        if failed is not None:
            failed[b.id] = retry
        return None
    if not started:
        if failed is not None:
            failed[b.id] = retry
        return None
    return (f"held: {note}: the land started its check (pid {started[0]}, log {started[1]}); it joins the land when green")


def _code_stamp():
    """T-3973: path, mtime and size of every Sampo module on disk: a change means a train merged new code for this land."""
    d = os.path.join(c.PLUGIN_ROOT, "lib", "sampo")
    return tuple((f, s.st_mtime_ns, s.st_size) for f in sorted(os.listdir(d)) if f.endswith(".py")
                 for s in [os.stat(os.path.join(d, f))])


class CodeWatch:
    """T-5735: the code stamp a long-running loop started on, asked at each unit boundary whether Sampo's modules changed
    since. A stamp that can't be read (a module mid-rename) is no change: the next boundary asks again."""

    def __init__(self, stamp=None):
        self.stamp = stamp if stamp is not None else self._read()

    @staticmethod
    def _read():
        try:
            return _code_stamp()
        except OSError:
            return None

    def changed(self):
        now = self._read()
        return now is not None and self.stamp is not None and now != self.stamp


_BOOT_STAMP = CodeWatch._read()  # the code on disk when this process loaded train.py (what it runs, bar what it imports later)


def process_watch():
    """T-5735: the watch of a long-running loop: the stamp taken when train.py was imported, not when the loop began, so
    a merge that landed during the process's own start-up (imports, a first scan) is seen at the first boundary."""
    return CodeWatch(_BOOT_STAMP)


def reexec_on_new_code(watch, execv=None):
    """T-5735: at a unit boundary (never inside one), replace this process by a fresh run of the same command when Sampo's
    own modules changed since `watch` was taken: a pipeline fix lands and the loops already running (the verify pass,
    a watch follow) take it up at their next unit instead of running the old code for hours. The pid stays, so a record
    naming it still finds it alive; the new process takes its own stamp. Returns False (nothing done) when the code
    is unchanged, SAMPO_NO_REEXEC is set, the command can't be rebuilt or the exec fails (an OSError: said once on stderr;
    the watch takes the new stamp, so the loop goes on on the old code and is not retried until the code changes again);
    it only returns True from a stubbed execv."""
    if os.environ.get("SAMPO_NO_REEXEC") or not watch.changed():
        return False
    argv = list(getattr(sys, "orig_argv", None) or [sys.executable, *sys.argv])[1:]  # python's own name first: ours below
    if not argv:
        return False
    for f in (sys.stdout, sys.stderr):
        try:
            f.flush()
        except (OSError, ValueError):
            pass
    try:
        (execv or os.execv)(sys.executable, [sys.executable, *argv])
    except OSError as e:  # a refused exec leaves this process running: on the old code, and asking once, not every round
        watch.stamp = watch._read()
        print(f"sampo: could not restart on new code ({c.fit(c.plain(str(e)), 160)}): going on with the code this "
              f"process started on", file=sys.stderr)
        return False
    return True


def unit_boundary(watch, hold=False):
    """T-5735: the one call a long-running loop makes between its units (the verify pass between records, the watch
    follow between rounds): take up new Sampo code by re-exec, unless `hold` (the loop holds state a restart would lose
    and redo work for). True only from a stubbed exec."""
    return False if hold else reexec_on_new_code(watch)


def start_verify(p, main, owed):
    """T-4718: the background verify a train deferred, started now (once) when the caller owes one: after the ship was
    asked for or ran in line, so the verify's suite runs never take the slots the ship's affected run waits for. `owed`
    is the caller's one-item list, cleared here. It starts when the caller owes one OR a record is still pending/red
    on disk (a land that raised or was killed during its closes never set the flag, but its records stand with no
    runner: a young pending record even holds off the sentinel's own full run). The runner is single (it takes the
    queue's lock), so a second start is harmless. It never raises: the landed lanes and the ship's result must stand."""
    from sampo import lanes
    try:
        if (not owed[0] and not any(r.get("state") in ("pending", "red") for r in lanes.pm_records(p).values())
                and not lanes.pm_conflict_due(p, main)):  # T-5518: nor a conflict whose trunk moved since it was checked
            return
        owed[0] = False
        lanes.pm_spawn(p, main)
    except Exception as e:  # noqa: BLE001 — a failed spawn leaves the records pending: the next train or verify takes them
        with contextlib.suppress(Exception):
            c.log_event(p, "postmerge_spawn_failed", data={"why": c.fit(str(e), 200)})


CLOSE_POLL_S = 3.0  # T-5368: a land waiting on its closes looks for a lane named --first (one accepted is) this often
LOCK_RETRIES = 3  # T-5793: a train refused because a close held trunk is tried again this many times in a row, then the land stops
LOCK_FAST_S = 30.0  # T-5793: a refusal after a full close wait (CLOSE_WAIT_S) is tried again once, never three times: that was an hour
CLOSE_RETRIES = 2  # a close whose trunk moved under its run (a train's fast-forward) runs again this many times, then waits


class _Closer:
    """T-5368: a land's closes run on this thread, one at a time, so a train never waits for one: 2026-11-21 a land's
    closes took 30 to 100 s each for an hour, between its trains, and four lanes accepted meanwhile waited for them. The
    land puts ids on the queue and goes on training; its end waits for the queue (idle), looking for new lanes meanwhile.
    `work(ids)` is the land's close and never raises. A close whose commit waits for a running train (`holding`)
    goes to `later`, back on the queue when the train ends (hold(False))."""

    def __init__(self):
        self.cv = threading.Condition()
        self.work = None
        self.q, self.later, self.thread = [], [], None
        self.holding = self.stopped = False
        self.only = None  # trim(keep): the ids that may still close (None: any)

    def _wake(self):  # under cv
        if self.thread is None and self.q and not self.stopped:
            self.thread = threading.Thread(target=self._run, name="sampo-land-closes", daemon=True)
            self.thread.start()

    def put(self, ids, front=False):
        """ids close next (front: before what is queued, a train's own lanes before the leftovers) or last."""
        ids = [t for t in ids if self.only is None or t in self.only]
        if ids:
            with self.cv:
                self.q.insert(0, ids) if front else self.q.append(ids)
                self._wake()

    def hold(self, on):
        """A train is running (on): a close's commit waits for it in `later`; its end puts them back."""
        with self.cv:
            self.holding = on
            if not on and self.later:
                self.q[:0] = self.later  # T-5873: proven, waiting only for their commit: fast, so they go first
                self.later = []
                self._wake()

    def after_train(self, ids):
        with self.cv:
            ids = [t for t in ids if self.only is None or t in self.only]
            if not ids:
                return
            if self.holding:
                self.later.append(list(ids))
            else:  # the train ended meanwhile
                self.q.append(list(ids))
                self._wake()

    def _run(self):
        while True:
            with self.cv:
                if self.stopped or not self.q:
                    self.thread = None
                    self.cv.notify_all()
                    return
                ids = self.q.pop(0)
            try:
                self.work(ids)
            except Exception as e:  # noqa: BLE001 — the work reports its own; one batch never ends the worker
                print(f"sampo lane land: a close batch failed: {type(e).__name__}: {c.fit(str(e), 200)}", file=sys.stderr, flush=True)
            with self.cv:
                self.cv.notify_all()

    def idle(self):
        with self.cv:
            return self.thread is None and not self.q and not self.later

    def wait(self, timeout):
        with self.cv:
            if self.thread is not None:
                self.cv.wait(timeout)

    def trim(self, keep):
        """T-5793: the land stops: only the ids in `keep` (the lanes this land merged) close from now; the running batch
        finishes, and no other lane's close starts, not even one a train's end puts back. The ids dropped (queued, or
        held for the train's end), for the caller to name."""
        keep = set(keep)
        with self.cv:
            self.only = keep
            dropped = [t for ids in self.q + self.later for t in ids if t not in keep]
            self.q = [x for x in ([t for t in ids if t in keep] for ids in self.q) if x]
            self.later = [x for x in ([t for t in ids if t in keep] for ids in self.later) if x]
            return dropped

    def stop(self):
        """No batch begins from now (the running one finishes): what was queued, for the caller to say or leave."""
        with self.cv:
            self.stopped = True
            dropped = [t for ids in self.q + self.later for t in ids]
            self.q, self.later = [], []
            return dropped

    def drain(self, budget):
        """T-5873: the land restarts on new code: the closes queued and the ones that waited for the train (hold(False) put
        them first) run until the closer is idle or `budget` seconds pass, then it stops (the running batch finishes).
        What was still queued after the budget, for the caller to say: each is merged and open for the next land."""
        end = time.monotonic() + budget
        with self.cv:
            if self.later and not self.holding:
                self.q[:0], self.later = self.later, []
                self._wake()
            while not self.idle() and (left := end - time.monotonic()) > 0:
                self.cv.wait(min(left, 1.0))
        return self.stop()

    def join(self):
        with self.cv:
            while self.thread is not None:
                self.cv.wait(1.0)


def _land(p, main, batch):
    """T-4718: the trains defer their background verify; it starts after the ship (or, whatever happens, at the end)."""
    owed = [False]
    closer = _Closer()
    try:
        return _land_trains(p, main, batch, owed, closer)
    finally:
        closer.stop()  # T-5368: a land that raised leaves its queued closes to the next land (merged and open: it finds them)
        closer.join()
        start_verify(p, main, owed)


def _land_trains(p, main, batch, owed, closer=None):
    from sampo import cli
    from sampo import lanes
    closer = closer or _Closer()
    asked_batch = batch  # T-5848: the --batch the user gave (None: the default, which never caps a live express train)
    batch = batch or c.TRAIN_BATCH
    code = CodeWatch()  # the code this land started on: T-5735 asked at each train boundary like the loops' unit boundaries
    from sampo import measure
    measure.note_pipeline_used(p)  # T-5735: this land runs on every pipeline fix merged before it started
    briefs = c.load_briefs(p)
    branch = _git(main, "symbolic-ref", "--short", "-q", "HEAD").stdout.strip() or "HEAD"

    def tip(b):
        return _git(main, "rev-parse", "--verify", "-q",
                    f"{b.meta.get('lane_branch') or 'foreman/' + b.id}^{{commit}}").stdout.strip()
    def merged(b):
        # T-5861: a lane the accept reviewed at its tip (reviewed_tip) that a stacked lane's merge carried to trunk has no
        # lane_stop of its own and no commit past main (finished.json holds it with no commit time): it is merged too.
        # The lane's tip must be the reviewed one: a fresh lane's tip (the base) is on trunk as well.
        return (b.meta.get("lane") and (t := tip(b))
                and (lanes._stop(b)[0] == "merged" or b.meta.get("reviewed_tip") == t)
                and not _git(main, "merge-base", "--is-ancestor", t, "HEAD").returncode)
    # a land cut short (or a train by hand) left these merged and open: they only need their close
    landed = [b.id for b in briefs if b.status not in c.CLOSED and merged(b)]
    # T-3969: closed already, its merged lane still on disk: _close's done branch removes it
    gone = [b.id for b in briefs if b.status == "done" and os.path.isdir(b.meta.get("lane") or "") and merged(b)]
    parked, ready, trains, stopped, stale, restart = [], [], 0, "", set(), False
    closed, hand, shipped = [], [], None
    settled = []  # T-5535: the closes that settled a gap of their own (the summary's "N settled")
    by, tested = {}, {}  # T-3994: each lane's brief, and the tip each verdict judged (a park holds only that tip)
    pipes = set()  # T-5735: the sorted lanes whose diff touches the pipeline (express, whatever their type)
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

    queued = set()  # T-5368: the ids put on the closer (once each)
    fixwait, freed = {}, set()  # T-5791: {task: the express FIX its close waits for}, and the (task, FIX) pairs released

    def ensure_pm():  # T-5791: a pending behind verify with no runner gets one; never costs the land anything
        try:
            lanes.pm_ensure(p, main)
        except Exception as e:  # noqa: BLE001 — a safety net's own failure is said, never raised into the land
            print(f"sampo lane land: post-merge runner check failed: {type(e).__name__}: {c.fit(str(e), 160)}",
                  file=sys.stderr, flush=True)

    def retry(tid, why):  # on the closer's thread: a refusal that is no verdict on the task goes round again
        # (a moved trunk is _close's own retry, CLOSE_RETRIES reruns in all: one layer, not one on top of the other)
        if why.startswith(COMMIT_WAIT) and closer.holding:  # its proof stands: the train's end commits it
            closer.after_train([tid])
            return True
        # T-5791: refused only for the classes an open express trunk-red FIX owns: it waits for that FIX's landing,
        # once per FIX (a close that fails again after the FIX landed is a verdict, left for a hand close)
        if (fid := _fix_open(p, why, tid)) and (tid, fid) not in freed and _fix_wait(p, main, tid, why):
            fixwait[tid] = fid
            print(f"sampo lane land: {tid}: its close waits for {fid} (an express trunk-red FIX) to land", file=sys.stderr, flush=True)
            return True
        return False

    def close(ids):  # T-3796: each lane closes once its train lands it, not after the whole pile has trained
        for tid in ids:  # T-5368: on the closer's thread, whose queue the trains' boundaries and the land's end feed
            if closer.stopped:  # a restart: what is not closed yet is merged and open, the next land finds it
                break
            if closer.only is not None and tid not in closer.only:  # T-5793: a stopped land closes only its own lanes
                continue
            try:
                done, left_ = close_landed(p, main, [tid], settled=settled, again=retry,
                                           commit_wait=lambda: 1.0 if closer.holding else None)
            except Exception as e:  # noqa: BLE001 — close_landed keeps each lane's own; this is all it could not
                done, left_ = [], [(tid, f"{type(e).__name__}: {e}")]
            closed.extend(done)
            hand.extend(left_)
    closer.work = close

    def wait_closes():
        """T-5368: the trains are done and the closes are not: wait for them, but a lane named --first (a lane accepted
        meanwhile is) trains at once, its gate running beside the closes. True when one came, False once the closes are done."""
        while not closer.idle():
            ensure_pm()  # T-5791: the closes may wait for hours: each poll asks (pm_ensure's own gap limits the spawns)
            closer.wait(CLOSE_POLL_S)
            if first_ids(p, peek=True):
                return True
        return bool(first_ids(p, peek=True))

    def to_close(ids, front=False):
        todo = [t for t in ids if t not in queued and t not in fixwait]
        queued.update(todo)
        closer.put(todo, front)

    def release_waiting():
        """T-5791: the closes whose express FIX has landed (or closed) go on the closer again; True when any did."""
        out = [t for t in list(fixwait) if _fix_released(p, main, t, landed)]
        for t in out:
            freed.add((t, fixwait.pop(t)))
            queued.discard(t)
            print(f"sampo lane land: {t}: its FIX landed: the close is queued again", file=sys.stderr, flush=True)
        if out:
            to_close(out, front=True)
        return bool(out)

    marked = {}  # T-5852: {lane: (why, tip)} of each park already on the brief: the end pass marks only the rest

    def mark_park(i, w):
        """T-5852: the lane's lane_stop and Log line, written when the lane is parked, not at the land's end: a continuous
        land parked four lanes at 07:20 and they had no lane_stop at 07:54, so `sampo lane recover` and the lane pilot
        refused them as "builder may be working". A park already marked for the same reason at the same tip is not
        written again (no second Log line); a failed write is left for the end pass, which marks what `marked` lacks."""
        tip_ = tested.get(i, "")
        if marked.get(i) == (w, tip_):
            return
        kind, arg = _park_kind(w)
        lanes.stop_mark(p, i, kind, arg, log=f"land: parked: {c.fit(w, 300)}", tip=tip_)
        marked[i] = (w, tip_)

    def note_park(pairs):  # T-4252: one line per lane left out, as it happens; plain() keeps a gate's output to one line
        for i, w in pairs:
            print(f"sampo lane land: parked {i}: {c.fit(c.plain(w), 400)}", file=sys.stderr, flush=True)
            try:  # T-5852: marked at once; a lane that can't be marked never costs the land its other lanes
                mark_park(i, w)
            except Exception as e:
                print(f"sampo lane land: {i}: its park is not marked yet: {c.fit(c.plain(str(e)), 160)}", file=sys.stderr, flush=True)
                continue  # unmarked: the pilot would see no park; the end pass marks it and asks
            if _park_kind(w)[0] == "conflict":  # T-5876: the resolver starts now, not 30 to 60 minutes later at the land's end
                _pilot_parks(p, [(i, w)], tick=False)  # (never raises into the land; the end pass asks again and ticks)

    held = set()  # T-4767: lanes the check hold left out: read again when a train starts or one is named --first
    checking, mine = set(), set()  # T-5561: lanes whose check this land started, and those still running (or waiting for a
    # slot): a land with nothing else to train waits for the second set
    failed, census = {}, []  # T-5561 review: lane -> when a check that would not start may be tried again; the running checks, counted once per poll

    def running():
        if not census:
            census.append(_live_checks(p))
        return census[0]

    def hold(b):
        """T-4767's hold, and T-5561: a stale lane with no check at its tip gets its check started here (not in a no-background
        or paused land: those hold it as before); `checking` follows the lanes whose check is in flight."""
        sha = tip(b)
        why = _lane_hold(p, b, sha, main)
        # live mode trains a stale lane at once with its note (sort): the land does not wait hours for a niced check there
        if (background and not live_mode(p) and failed.get(b.id, 0) <= time.monotonic()
                and (why is None or why.startswith("held: no passing affected check"))):
            if got := _stale_start(p, b, main, sha, failed, running):
                mine.add(b.id)  # only the checks this land started are waited for: an accept's check joins by its own queueing
                if "the land started its check" in got:
                    census[:] = [running() + 1]
                why = got
        if why and b.id in mine and (why.startswith(("held: check running", "held: check queued", "held: shape queued"))
                                     or "predates the lane tip" in why):
            checking.add(b.id)
        else:
            checking.discard(b.id)
        return why

    def sort(bs):  # a conflict never costs a criteria or gate run (an old git without merge-tree: no check)
        for b in sorted(bs, key=lambda x: land_key(x, pipeline_lane(main, tip(x)))):  # T-5735: a pipeline fix leads
            if why := hold(b):  # T-4286: its check is running, red or cut off at this tip: left out; T-4767: no passing check at the tip at all; T-5561: a stale lane's check is started
                print(f"sampo lane land: {b.id}: {why}", file=sys.stderr, flush=True)
                held.add(b.id)
                continue
            held.discard(b.id)
            by[b.id], tested[b.id] = b, tip(b)
            pipes.discard(b.id)
            if pipeline_lane(main, tested[b.id]):
                pipes.add(b.id)
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
                    proof = [g for g in gaps if g.startswith("no red→green proof")]  # T-4209: recorded in the lane, not a Close field
                    fields = [g for g in gaps if g not in proof]
                    said = "; ".join(fields) + (" (add the lines to its Close fields before its train ends)" if fields else "")
                    print(f"sampo lane land: {b.id} lands but its close will be refused: "
                          + "; ".join(x for x in (said, *proof) if x), file=sys.stderr, flush=True)
        ready.sort(key=lambda t: t in stale)  # stable: fresh lanes first, each group in land order
    def recheck(ids):
        """T-4767: `ids` without the lanes the hold now catches: sort read it at the land's start, and a lane that waits
        for a later train (each one is minutes of gate) may since have a new commit with no check at it, or a check that
        ended red. A dropped lane is said, not parked: it joins again once its check is clean (readmit), else the next land."""
        now, keep = {b.id: b for b in c.load_briefs(p)}, []
        for t in ids:
            b = now.get(t)
            if b and (why := hold(b)):
                print(f"sampo lane land: {t}: {why}", file=sys.stderr, flush=True)
                held.add(t)
            else:
                keep.append(t)
        return keep

    def readmit():
        """T-4767 review: the held lanes whose check is clean now (a hand check or an accept finished while a train ran)
        are sorted in, as the held line promises. Said once: a lane still held is not printed again."""
        now = c.load_briefs(p)
        census[:] = [_live_checks(p, now)]  # once for the poll, not per stale lane
        back = set()
        for b in now:
            if b.id not in held:
                continue
            flying = b.id in checking
            if not (why := hold(b)):
                back.add(b.id)
            elif flying and b.id not in checking:  # T-5561: its check ended and still holds it (red, cut off): said once
                print(f"sampo lane land: {b.id}: {why}", file=sys.stderr, flush=True)
        back &= set(lanes.awaiting_merge(p, now, 0, fresh=True))
        sort(b for b in now if b.id in back)
        census.clear()

    def wait_checks():
        """T-5561: nothing is ready and held lanes' checks run (the land started them): wait, bounded by STALE_WAIT_S, for
        one to end green and join. True when a lane is ready, a close waiting for its FIX was released or a lane was named
        --first; False when none is in flight (or the wait ran out). The cheap looks come every CLOSE_POLL_S, the held
        lanes' read (every brief loaded) every STALE_POLL_S."""
        end = time.monotonic() + STALE_WAIT_S
        again = 0.0
        while checking and time.monotonic() < end and not c.panicked():
            ensure_pm()
            time.sleep(CLOSE_POLL_S)
            if release_waiting():
                return True
            if time.monotonic() >= again:
                again = time.monotonic() + STALE_POLL_S
                readmit()
            if ready or first_ids(p, peek=True):
                return True
        return False
    left = landed + gone  # merged and open (a train by hand, a land cut short): closed after each train's own (T-3884)
    fixwait.update(_fix_waiting(p, main, left))  # T-5791: an earlier land's closes that wait for their FIX stay out of the closes
    # T-5873: every merged-and-open leftover goes on the closer before the first train (not LEFT_PER_TRAIN after one: a
    # land that restarts after its first train never reached the rest); a close refused before answers from its memo, fast.
    # One batch each: a stop's trim and a restart's drain act between batches, so one batch of sixty would run to its end
    for t in left:
        to_close([t])
    waiting = set(lanes.awaiting_merge(p, briefs)) - set(landed)
    sort(x for x in briefs if x.id in waiting)
    seen = set(waiting) | set(landed)
    alone = set()  # T-5023: the express lanes taken from --first: each trains as a batch of one
    carry = _restart_carry(p)  # T-5023: a restarted land's queue: its lanes are promoted only if they were before
    first_taken = set()  # T-4013: every --first id this land took (`asked` is the ship request's flag, not this)
    retried, slow_retried = 0, False  # T-5793: the train's refusals by a close that held trunk, in a row; one after a long wait
    mine, shelved, said = set(), [], None  # T-5793: the lanes this land merged; the leftovers a stop left; the train line said

    def halt():
        """T-5793: the land stops: before the train's hold ends (no new close starts in the gap), only the lanes this land
        merged close on (the running batch finishes); the leftovers wait for the next land and are named."""
        shelved[:] = closer.trim(mine)
    while True:
        ensure_pm()  # T-5791: once per train and per idle wait: the safety net behind the landing
        release_waiting()
        jump = first_ids(p)  # T-3796: asked for meanwhile go next, a lane finished since the land began too
        carried, carry = carry, {"ready": [], "alone": []}  # T-5023: the restart's queue counts at the first read only
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
            # T-5023: express lanes lead the queue and train alone; a restart's queue (first iteration only) carries
            # the lanes that were express then, and no ordinary-by-name lane turns express because it waited there
            fast = [t for t in front if is_express(by[t], t in pipes) and (t not in carried["ready"] or t in carried["alone"])]
            for t in fast:
                if t not in alone:
                    print(f"sampo lane land: {t} is express (performance, security or urgent): it trains next, "
                          + ("with the other express lanes" if live_mode(p) else "alone"), file=sys.stderr, flush=True)
            alone.update(fast)
            front = fast + [t for t in front if t not in fast]
            ready = front + [t for t in ready if t not in front]
            if any(t not in front for t in jump):  # (`now` above is a list of briefs; this one a map by id)
                by_id = {b.id: b for b in c.load_briefs(p)}
            for t in (t for t in jump if t not in front):
                why = next((w for i, w in parked if i == t), "")
                print(f"sampo lane land: --first {t}: " + _first_state(by_id.get(t), t, why, landed),
                      file=sys.stderr, flush=True)
        for t in (t for t in ready if t in pipes and t not in alone):  # T-5735: named or not, a pipeline fix is express
            alone.add(t)
            print(f"sampo lane land: {t} touches the pipeline (its own modules): it trains first, "
                  + ("with the other express lanes" if live_mode(p) else "alone"), file=sys.stderr, flush=True)
        ready.sort(key=lambda t: t not in alone)  # T-5023: stable; a held express lane sorted back in leads again
        if ready and trains:  # T-4767: a lane waits out the trains before it: its check is read again at this one's start
            ready = recheck(ready)
        if not ready:
            to_close([t for t in left if t not in queued])  # T-5368: the leftovers close now, a lane named meanwhile trains
            if wait_closes() or release_waiting():  # T-5791: a close that waited meanwhile may have its FIX landed already
                continue
            if wait_checks():  # T-5561: a stale lane's check the land started: it trains when that check is green
                continue
            break
        # T-5444: live, one train for all. T-5023: an express lane at the head leaves the ordinary lanes' train; T-5726:
        # gated it trains alone, live the express lanes at the head (up to `batch`) train together: twenty performance
        # lanes were twenty trains, each with its own gate and bench; a red one is bisected as in any train
        live = live_mode(p)
        # T-5848: the default batch does not cap them (54 urgent lanes trained as seven gates); an explicit --batch does
        cut = ((max(1, min(asked_batch or len(ready), next((i for i, t in enumerate(ready) if t not in alone), len(ready))))
                if live else 1)
               if ready[0] in alone else len(ready) if live else batch)
        ids, ready = ready[:cut], ready[cut:]
        if said != (trains, tuple(ids)):  # T-5793: a train tried again is not announced again
            print(f"sampo lane land: train {trains + 1}: {', '.join(ids)}", file=sys.stderr, flush=True)
            said = (trains, tuple(ids))
        tested.update({i: tip(by[i]) for i in ids})  # train() reads the same heads as it starts
        head0 = c._git(main, "rev-parse", "HEAD", timeout=10).strip()  # T-3851: the skills this train adds
        t_train = time.monotonic()
        closer.hold(True)  # T-5368: the closes run on, only their commits wait for this train's lock
        try:
            got = train(p, main, ids, defer_verify=True)
            owed[0] = owed[0] or bool(got.get("verify_owed"))
        except c.PolicyError as e:
            if not hasattr(e, "left"):  # a red base, a moved branch: no later batch would land either
                stopped = str(e)  # T-4029: with the red gate's own lines, so nobody reruns it to learn them
                halt()
                break
            got = {"merged": [], "left_out": e.left}
        except c.LockTimeout:
            # T-5793: a close (a hand close's, or this land's own closer's) holding trunk is no other train: tried again
            # after it, a few times; a train that really runs ends the land as before (land again after it)
            # (each wait for a hand close is CLOSE_WAIT_S: a refusal after a long wait is tried again once, not three times)
            slow = time.monotonic() - t_train >= LOCK_FAST_S
            if retried < LOCK_RETRIES and not (slow and slow_retried) and (_closing(p) or _own_closer()):
                retried, slow_retried = retried + 1, slow_retried or slow
                ready = ids + ready
                print(f"sampo lane land: train {trains + 1} waits for a close that holds trunk: tried again "
                      f"({retried}/{LOCK_RETRIES})", file=sys.stderr, flush=True)
                closer.wait(CLOSE_POLL_S)
                continue
            stopped = "a sampo lane train is running: land again after it"
            halt()
            break
        finally:
            closer.hold(False)
        retried, slow_retried = 0, False
        mine.update(got["merged"])
        note_park(got["left_out"])  # T-4252: before the next train starts, not in the summary
        if got.get("kept"):  # T-4007: the main checkout's local changes the fast-forward set aside
            print(f"sampo lane land: {got['kept']}", file=sys.stderr, flush=True)
        trains += 1
        for t in got["merged"]:
            if t in pipes:  # T-5735: filed and merged times of a pipeline lane; its first use comes with the next process
                measure.note_pipeline_merged(p, by[t])
        landed += got["merged"]
        parked += got["left_out"]
        times.append(_train_times(p, land_id, trains, got, by, time.monotonic() - t_train))
        to_close(got["merged"], front=True)  # T-5368: on the closer's thread: the next train starts at once
        to_close([t for t in left if t not in queued and _retry_due(p, t)][:LEFT_PER_TRAIN])  # T-3969: a prepped one closes now, not hours on
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
        if ready and code.changed():  # T-3973: the train changed Sampo itself: the rest runs on the new code
            restart = True
            first_ids(p, ready)  # the restarted land trains them in this order, not re-sorted
            _restart_carry(p, ready, alone)
            break
    if stopped:  # T-5793: said before anything is waited for (the summary comes after the closes' join)
        print(f"sampo lane land: stopped: {stopped}", file=sys.stderr, flush=True)
    if stopped:  # a stopped land's leftovers close at the next land (2026-11-22 it queued every leftover and joined their
        # closes); the lanes this land merged close (halt kept them): the running and the queued ones finish
        rest = shelved + [t for t in left if t not in queued and t not in shelved]
        if rest:
            print(f"sampo lane land: {len(rest)} landed lane(s) left merged and open for the next land: "
                  f"{', '.join(rest)}", file=sys.stderr, flush=True)
    elif restart:  # T-5873: the closes queued (and the ones waiting for their commit) run first, bounded; the rest closes
        # under the new code (the next land finds it merged and open): stop() alone dropped them all, one close per land
        n0 = len(closed)
        dropped = closer.drain(RESTART_CLOSE_S)
        closer.join()
        print(f"sampo lane land: closed {len(closed) - n0} before restart ({len(dropped)} left for the next land)",
              file=sys.stderr, flush=True)
    else:
        to_close([t for t in left if t not in queued])  # one tried at a boundary was said there
    closer.join()  # T-5368: the land's last ship and its summary come after every close
    for tid, why in parked:  # T-5852: marked as each was parked (note_park); this pass writes only what that did not
        mark_park(tid, why)
    _pilot_parks(p, parked)  # T-5766: the marks are on the briefs; each parked conflict gets its headless resolver
    try:
        last = ship(True) if landed and not restart else None  # the land's last ship spends the grant: a restart's successor does
    finally:
        start_verify(p, main, owed)
    shipped = last if last and not last[1].startswith("nothing to ship") else shipped or last
    grant, said = shipped or (None, "")
    hand.extend((t, f"waits for {f} (an express trunk-red FIX) to land, then closes again") for t, f in fixwait.items())  # T-5791
    hand = list(dict(hand).items())  # a leftover retried after its brief changed: its newest answer
    return {"landed": landed, "closed": [t for t, _ in closed], "hand": hand, "settled": settled, "parked": parked, "trains": trains,
            "stopped": stopped, "restart": restart, "ship": said, "ship_grant": grant, "lines": closed,
            "train_times": times, "ship_asked": asked, "ship_failed": ship_failed}


def close_landed(p, main, ids, who="sampo lane land", settled=None, again=None, commit_wait=None):
    """T-4596: the one close every landing path makes (a land's trains, a multi-id sampo lane merge, sampo lane land
    --merged): each id's task closes through `_close` (sampo task finish's own checks, re-run on the trunk; nothing
    forced) and its lane goes with it. ([(id, the close's last line)], [(id, why it is left for a hand close)]); one
    lane's refusal or error never costs the others theirs. settled (T-5535) collects the ids whose close settled a gap.
    T-5368: `again(id, why)` is asked of a refusal and, True, takes the id over (it is queued to close again: neither
    closed nor left); `commit_wait` is the seconds (or a function giving them) a commit waits for a running train.
    The gates need no run of their own here: finish reuses the
    full pass on record for the trunk's code tree, which the CHANGELOG line each close commits leaves valid (the code
    tree leaves CHANGELOG.md out), so N closes cost one gate run at most, the first, and none after a train."""
    closed, hand = [], []
    for tid in ids:
        try:
            why, last = _close(p, main, tid, settled, commit_wait)
        except Exception as e:  # noqa: BLE001 — one lane's close never costs the others theirs
            why, last = f"{type(e).__name__}: {e}", ""
        if why and again and again(tid, why):
            print(f"{who}: {tid} goes round again: {c.fit(why, 200)}", file=sys.stderr, flush=True)
            continue
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
        owes = b.status == "done" and _commit_pending(b)  # T-5368: proved and done, its CHANGELOG commit never made
        if (b.status in c.CLOSED and not owes) or not path:
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


PM_POLL_S = 5.0  # T-5368: how often a sweep looks again at a post-merge verify it waits for
PM_WAIT_S = 1800.0  # T-5368: the longest it waits (a verify that outlives it is closed beside, its tests rerun then)


def _pm_unsettled(p, ids):
    """T-5368: [(task id, merge sha, state)] of the post-merge verifies of these tasks' merges that are on their way to a
    result: running with a live runner, or pending and just landed (SENTINEL_MIN: older is stuck, no runner took it).
    A finished one (green, red, reverted), a dead runner's and another task's are not waited for."""
    from sampo import cli
    from sampo import lanes
    want = set(ids)
    out = []
    for r in lanes.pm_records(p).values():
        fresh = (a := c.age_days(r.get("at"))) is not None and a < cli.SENTINEL_MIN / 1440
        if r.get("task") in want and (r.get("state") == "running" and not lanes._pm_dead(r)
                                      or r.get("state") == "pending" and fresh):
            out.append((r["task"], r.get("sha") or "", r["state"]))
    return sorted(out)


def _wait_pm(p, ids, who):
    """T-5368: a sweep's closes rerun criteria that the post-merge verify of the same merges is running (and caches by
    class once it passes): the sweep waits for it, up to PM_WAIT_S, and says so. '' when none was running, else what it
    did for the gate line ('waited 41 s for the post-merge verify', or that it was still running at the deadline)."""
    first = _pm_unsettled(p, ids)
    if not first:
        return ""
    t0 = time.monotonic()
    print(f"{who}: waiting for the post-merge verify of {', '.join(sorted({t for t, _, _ in first}))} "
          f"(up to {PM_WAIT_S:.0f} s) before the closes: its tests are theirs", file=sys.stderr, flush=True)
    while _pm_unsettled(p, ids):
        left = PM_WAIT_S - (time.monotonic() - t0)
        if left <= 0:
            s = f"the post-merge verify was still running after {time.monotonic() - t0:.0f} s"
            print(f"{who}: {s}: closing beside it", file=sys.stderr, flush=True)
            return s
        time.sleep(max(0.0, min(PM_POLL_S, left)))
    return f"waited {time.monotonic() - t0:.0f} s for the post-merge verify"


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
    from sampo import lanes
    if why := held_by_active(p, main, ids=[]):  # T-4692: merged lanes are on the trunk: only the changelog is left to hold
        raise c.PolicyError(why)
    # T-5368: the wait for a post-merge verify (up to PM_WAIT_S) comes before the land lock: a land that came meanwhile
    # is not refused for it. The lanes it waits for are read once here; the sweep below reads them again under the lock.
    try:  # a land that runs is said at once: no wait for a verify first (the lock is taken again below, after the wait)
        with c.lock(os.path.join(p.dir, "land"), timeout=1):
            pass
    except c.LockTimeout:
        raise c.PolicyError("another sampo lane land is running; it closes the lanes it lands") from None
    waiting = _merged_lanes(p, main)[0]
    waited =_wait_pm(p, [t for t, _ in waiting], "sampo lane land --merged") if waiting else ""
    with contextlib.ExitStack() as held:
        try:
            held.enter_context(c.lock(os.path.join(p.dir, "land"), timeout=1))
        except c.LockTimeout:
            raise c.PolicyError("another sampo lane land is running; it closes the lanes it lands") from None
        on, left, empty = _merged_lanes(p, main)
        settled = []  # T-5535: the closes below that settled a gap of their own
        if on:  # T-4658: the machine before any gate or close, as land() and train() check it. Nothing is checked when no
            # merged lane waits for its close: the empty lanes' removal below frees disk, it takes none.
            why = paused_stop(p) or machine_stop(p)  # T-5044: a pause refuses its gate too
            if why:
                raise c.PolicyError(why)
        dropped, kept = _drop_empty(p, main, empty) if drop_empty else ([], [(t, x, w) for t, x, w in empty])
        gate = "no merged lane waits for a close" if not on else ""
        if on and cli._full_pass(p):
            gate = "reused the gate pass on record for the trunk's code tree"
        elif on:  # one run for all of them; finish then reuses it for each close (a red one closes nothing)
            print(f"sampo lane land --merged: no gate pass on record for the trunk: one sampo check for the "
                  f"{len(on)} lane(s)", file=sys.stderr, flush=True)
            # T-5483: a live project lands with no front gate and its behind verify already runs cached, so this one
            # check skips the classes green at the same code too (1 in 10 rerun): the hourly tick's land ran all 12,000
            # tests uncached, queued behind the verify on the same code
            live = c.read_meta(p).get("train_mode") == "live"
            rc, out, err = _sampo(["check"], env={**os.environ, "SAMPO_CACHE_FULL": "1"} if live else None)
            gate = "ran one sampo check on the trunk" if not rc else ""
            if rc:
                why = f"the trunk's gates are red, so no close rides on them: {c.fit(_why(err or out), 300)}"
                left.extend((t, why) for t, _ in on)
                on = []
                gate = "one sampo check on the trunk, red"
        gate += f" ({waited})" if waited else ""
        try:  # T-5791: the sweep is one of the two ways a stopped behind verify is started again; its failure costs nothing
            lanes.pm_ensure(p, main)
        except Exception as e:  # noqa: BLE001
            print(f"sampo lane land --merged: post-merge runner check failed: {type(e).__name__}: {c.fit(str(e), 160)}",
                  file=sys.stderr, flush=True)
        # T-5791: a close that waits for its express trunk-red FIX stays open until the FIX is on the trunk (here: merged
        # or closed); one that fails only on a FIX's classes now waits too, said in `left` with the FIX's id
        ids = [t for t, _ in on]
        waits, freed = {}, set()
        for t in list(_fix_waiting(p, main, ids)):
            if _fix_released(p, main, t, landed=ids):
                freed.add(t)
            else:
                waits[t] = _fix_waiting(p, main, [t])[t]

        def wait_for_fix(tid, why):
            if (fid := _fix_open(p, why, tid)) and tid not in freed and _fix_wait(p, main, tid, why):
                waits[tid] = fid
                return True
            return False
        closed, hand = close_landed(p, main, [t for t in ids if t not in waits], who="sampo lane land --merged",
                                    settled=settled, again=wait_for_fix) if on else ([], [])
        left.extend(hand)
        left.extend((t, f"waits for {f} (an express trunk-red FIX) to land, then closes again") for t, f in waits.items())
        for tid, why in left:
            try:
                _note_left(p, tid, why)
            except Exception as e:  # noqa: BLE001 — the Log line is a note: it never costs the sweep its result
                print(f"sampo lane land --merged: no note on {tid}: {type(e).__name__}", file=sys.stderr)
    return {"closed": [t for t, _ in closed], "left": left, "lines": closed, "gate": gate,
            "empty": [(t, w) for t, _, w in kept], "dropped": dropped, "settled": settled}


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


def _close_gaps(b, ptier=None):
    """T-3819: what a close would refuse for a line the brief lacks (CHANGELOG, DOCS, NUMBERS, LESSON, why not caught;
    T-4515: the WHY-NOT-CAUGHT line of its Close fields counts for the last; T-4209: a FIX's missing red→green proof
    comes first, said as the close would (Brief.proof_gap), since no Close field gives it).
    T-5715: `ptier`, the process tier the caller measured, decides which of docs, lesson and why-not-caught are owed
    (none given: the brief's stamped or claimed tier)."""
    from types import SimpleNamespace

    from sampo import cli
    from sampo import lanes
    proof = [g] if (g := b.proof_gap()) else []
    f = lanes.close_fields(b.section(lanes.CLOSE_FIELDS)) or lanes.lane_trailers(
        b.meta.get("lane") or "", b.meta.get("base") or "", b.id)  # T-5496: the commits' trailers, if the brief has none
    if not f.get("changelog"):
        return proof + ["no CHANGELOG line in its Close fields"]
    return proof + cli._field_gaps(b, SimpleNamespace(docs=f.get("docs"), numbers=f.get("numbers"), lesson=f.get("lesson"),
                                                      why_not_caught=f.get("why_not_caught")), ptier)


def _brief_hash(b):
    """T-3884: the brief's text without its Log (the refusal record lives there)."""
    return hashlib.sha1("\0".join(f"{h}\1{t}" for h, t in b.sections if h.strip() != "Log").encode()).hexdigest()[:12]


# ponytail: a refusal caused outside the brief, the tip and Sampo's own code (a tool on PATH, the machine) is retried
# after this long
REFUSAL_TTL_S = 6 * 3600
# ponytail: leftovers retried per train boundary, so 50 prepped closes don't hold up the next train; raise it if they pile
LEFT_PER_TRAIN = 8
RESTART_CLOSE_S = 300  # T-5873: a land restarting on new code runs its queued closes this long (in all) before it execs


def _retry_due(p, tid):
    """T-3969: a leftover whose close may pass now: never refused, its brief or lane changed since, the refusal is
    older than its backoff (T-5823: a code-only change waits, doubling per identical refusal) or REFUSAL_TTL_S (a
    closed one: only a done task, whose lane is all that's left)."""
    from sampo import cli
    b = cli.need_brief(p, tid)
    if b.status in c.CLOSED and b.status != "done":
        return False
    path = b.meta.get("lane") or ""
    sha = _git(path, "rev-parse", "--short", "HEAD").stdout.strip() if os.path.isdir(path) else ""
    return _refused_before(b, sha) is None


def _code_id():
    """T-4080: the committed tree of the Sampo code running this land, '' outside git: a gate rule fixed on main
    changes a close's answer (T-3701 and T-3671 waited 6 h after T-4061 and T-4062 fixed their refusals)."""
    lib = os.path.dirname(os.path.dirname(os.path.abspath(c.__file__)))
    return (c._git(lib, "rev-parse", "HEAD:./", timeout=10, fail="") or "").strip()[:12]


REFUSAL_BACKOFF_S = 600  # T-5823: the wait after a refusal's first appearance when only Sampo's code changed; doubles per repeat


def _refused_before(b, tip):
    """The reason the last land's close was refused when the brief and the lane tip are as they were then and Sampo's
    code is too, else None. T-5823: every train changes the code, so a code-only change (same reason, brief, tip) holds
    the refusal for REFUSAL_BACKOFF_S, doubling per identical refusal in a row, capped at REFUSAL_TTL_S, instead of
    retrying at once (eight leftovers cost a minute each at every train boundary). A brief or tip change, a
    different reason or a released FIX (QUEUED_AGAIN) retries at once."""
    last, n = None, 0
    for line in reversed(b.section("Log").splitlines()):
        if QUEUED_AGAIN in line:  # T-5791: the FIX the close waited for landed: the refusal before it no longer holds
            break
        if not (m := re.search(r"^- (\S+) land: close refused: (.*) @ (\w+) (\w*)(?: (\w+))? ?$", line)):
            continue
        if last is None:
            last = m
        elif m.group(2, 3, 4) != last.group(2, 3, 4):  # another reason, brief or tip: the run of identical refusals ends
            break
        n += 1
    if last is None:
        return None
    at = c.parse_ts(last.group(1))
    if not at or last.group(3, 4) != (_brief_hash(b), tip):
        return None
    wait = (REFUSAL_TTL_S if (last.group(5) or "") == _code_id()
            else min(REFUSAL_TTL_S, REFUSAL_BACKOFF_S * 2 ** min(n - 1, 20)))
    return last.group(2) if time.time() - at.timestamp() <= wait else None


QUEUED_AGAIN = "land: close queued again"  # T-5791: the Log line of a close released when its express FIX landed
_CLASS_RE = re.compile(r"\b[A-Za-z_][A-Za-z_0-9]*\.[A-Z][A-Za-z_0-9]*\b")  # a class as the gates name it: test_docs.Agents


def _fix_open(p, why, tid=""):
    """T-5791: the id of an open FIX filed for a semantic trunk red (meta trunk_red = its classes) whose classes are
    every class this refusal names, else ''. A refusal naming no class, or one outside the FIX's, is a verdict on the lane."""
    named = set(_CLASS_RE.findall(why or ""))
    if not named:
        return ""
    for b in sorted(c.load_briefs(p), key=lambda x: c.id_num(x.id)):
        red = b.meta.get("trunk_red")
        if isinstance(red, list) and red and b.id != tid and b.status not in c.CLOSED and named <= set(red):  # never itself
            return b.id
    return ""


def _fix_wait(p, main, tid, why):
    """T-5791: a close refused only for the classes an open express trunk-red FIX owns waits for that FIX: the id goes on
    the brief (close_waits_on) and its Log, and True is returned (the close is not a verdict on the lane). False else."""
    from sampo import cli
    fid = _fix_open(p, why, tid)
    if not fid:
        return False
    line = f"land: close waits for {fid}: its refusal names only that FIX's classes ({c.fit(c.plain(why), 160)})"

    def mark(b):
        if b.meta.get("close_waits_on") != fid:
            b.meta.update(close_waits_on=fid)
            b.append_log(line)
    cli.mutate(p, tid, mark, "close_waits", {"fix": fid})
    return True


def _fix_waiting(p, main, ids):
    """T-5791: {task id: the FIX it waits for} among `ids`: the sweep leaves these to their FIX's landing."""
    out = {}
    for t in ids:
        b = c.find_brief(p, t)
        if b and (fid := b.meta.get("close_waits_on")) and b.status not in c.CLOSED:
            out[t] = fid
    return out


def _fix_released(p, main, tid, landed=()):
    """T-5791: True once, when the FIX a close waited for is merged (in `landed`) or closed: the wait is cleared, the Log
    says the close is queued again (which also ends the refusal's memo) and the caller puts the id on the closer."""
    from sampo import cli
    b = c.find_brief(p, tid)
    if not b or not (fid := b.meta.get("close_waits_on")):
        return False
    fix = c.find_brief(p, fid)
    if fid not in landed and fix is not None and fix.status not in c.CLOSED:
        return False

    def free(x):
        if x.meta.pop("close_waits_on", None):
            x.append_log(f"{QUEUED_AGAIN}: {fid} landed")
    cli.mutate(p, tid, free, "close_queued_again", {"fix": fid})
    return True


SETTLE_LOCK_S = 30  # T-5535: the wait for the brief's lock before a close settles its gaps (a timeout leaves them to finish)


def _settle_mechanical(p, tid, sha):
    """T-5535: settles the three mechanical blockers a merged, audited lane's close would otherwise leave to a hand close,
    and records each as one Log line: a file edited outside its scope gets a 'scope:' reason naming the reviewed tip;
    each open follow-up is captured to the inbox (triggers.settle_follow_ups); a step with no evidence of its own (an M
    or L task) takes the first criterion's command as its run. ([the kinds settled], that run for finish's --run or None).
    The caller only asks once the task's audits are fresh: a lane without one settles nothing."""
    from sampo import cli, triggers
    kinds, run = [], None
    try:
        with c.lock(p.dir, timeout=SETTLE_LOCK_S):  # released before finish, which takes the same lock itself
            b = cli.need_brief(p, tid)
            touches, _, _ = cli._task_files(p, b)
            if cli._scope_gaps(p, b, touches):
                b.append_log(f"scope: widened by the lane diff reviewed and merged at tip {sha} (land)")
                kinds.append("scope")
            first = next(((n, cmd) for n, cmd in b.verify_cmds() if cmd), None) if b.tier != "S" else None
            bare = [s for s in b.steps() if not s.done and not b.has_evidence(step=s.n)] if first else []
            for s in bare:  # a retried close finds its step's line already logged: one line per step, once
                if f"steps settled (land): step {s.n} " not in b.section("Log"):
                    b.append_log(f"steps settled (land): step {s.n} took criterion {first[0]}'s run "
                                 f"({c.fit(first[1], 60)})")
                run = first[1]
            if bare:
                kinds.append("steps")
            if kinds:
                c.save_brief(p, b)
            if triggers.settle_follow_ups(p, b):
                kinds.append("follow-ups")
    except c.LockTimeout:  # the close goes on: what it still lacks, finish names and a hand close settles
        return [], None
    return kinds, run


def _closed_text(got):
    """T-5535: 'N closed', and 'N closed (M settled)' when M of those closes settled a gap of their own."""
    m = len(got.get("settled") or [])
    return f"{len(got['closed'])} closed" + (f" ({m} settled)" if m else "")


PENDING_COMMIT, COMMITTED = c.CLOSE_PENDING, c.CLOSE_COMMITTED  # T-5368: the Log's marks of a close's two phases


def _wait(w):
    """T-5368: a commit's wait for the train's lock: a number, or a function asked when the commit is reached (a train
    may have started during the criteria), or None for the default."""
    return w() if callable(w) else w


def _close(p, main, tid, settled=None, commit_wait=None):
    """('', 'TITLE — the close's last line'), or (why it's left for a hand close, ''). The task closes from the main
    checkout with its lane still in place, which goes only once the close is done: a refused close keeps it for a fix.
    Only audits made after the lane's tip commit count, and the close's lenses name that tip. A close that settled a gap
    of its own (T-5535) appends its id to settled once finish has closed it."""
    from sampo import cli
    from sampo import lanes
    if why := held_by_active(p, main, ids=[]):  # T-4692: a focus that came meanwhile holds only by an uncommitted changelog
        return f"{why.split(': finish')[0]}, so land closes nothing more", ""
    b = cli.need_brief(p, tid)
    path = b.meta.get("lane") or ""
    if b.status == "done" and _commit_pending(b):  # T-5368: its criteria passed in an earlier land; the commit waited
        brief_fields = lanes.close_fields(b.section(lanes.CLOSE_FIELDS))
        f = brief_fields or lanes.lane_trailers(path, b.meta.get("base") or "", tid)  # T-5496: the lane's trailers, as at the close
        title = lanes.close_line(f.get("title"), "title", tid)[0] or lanes.close_line(b.title, "title", tid)[0]
        why, out = _commit_close(p, tid, title, path, _wait(commit_wait), "" if brief_fields else f.get("changelog", ""))
        if why:
            return why, ""
        return _finish_close(p, main, tid, path, title, out, 0.0)
    if b.status == "done":  # T-3907: closed inside its lane; the merge was all it lacked
        try:
            lanes.remove(p, b, main)
        except (c.PolicyError, cli.UsageError) as e:
            return f"merged; its lane stays: {str(e).splitlines()[0]}", ""
        return "", f"{b.title} — already closed in its lane"
    sha = _git(path, "rev-parse", "--short", "HEAD").stdout.strip() if path else ""
    last = float(_git(path, "log", "-1", "--format=%ct").stdout.strip() or 0) if path else 0.0
    brief_fields = lanes.close_fields(b.section(lanes.CLOSE_FIELDS))
    f = brief_fields or lanes.lane_trailers(path, b.meta.get("base") or "", tid)  # T-5496: no Close fields: trailers
    if not f.get("changelog"):
        if "close lines not stored" in b.section("Log"):  # T-4599: the stop hook saw lines the tail rule refused
            return ("no close fields on its brief (its builder's report had close lines, not at its end: "
                    "fill them with sampo task set ID --section 'Close fields')", "")
        return ("no close fields on its brief (none came from a builder's report or its commits' trailers: "
                "it held no CHANGELOG line, or none stopped here)"), ""
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
    # T-5496: finish reads only its brief's Close fields, so a trailer's lines go to it as flags
    keys = ("changelog", "lesson", "numbers", "docs")
    tip = [] if brief_fields else [f"--{k.replace('_', '-')}={f[k]}" for k in keys if f.get(k)]
    kinds, run = _settle_mechanical(p, tid, sha)  # T-5535: the audits are fresh, so the gaps a hand close would settle are settled here
    run_arg = ["--run", run] if run else []
    t_finish = time.monotonic()  # T-4600: the close's wall time, lock wait included, beside the steps the finish itself reports
    # T-5444: a live land's close re-runs no linked tests: the behind suite runs them all on the merged head and reverts
    live_skip = ["--skip-affected", "live landing: the behind suite verifies the merged head and reverts a red merge"] \
        if live_mode(p) else []
    # T-5368: the criteria (and the audits, the evidence, the task's done) run with no lock: a slow one froze every train
    # and every later close for the whole of its tests. Only the CHANGELOG line and the commit take trunk's lock, below.
    argv = ["task", "finish", tid, "--audit", f"{LAND_AUDIT} (lane tip {sha})", "--result", seen,
            *[x for lens in sorted(set(fresh) - {"self"}) for x in ("--lens", f"{lens}: {seen}")],
            *why_arg, *tip, *live_skip, *run_arg, "--defer-changelog"]
    rc, out, err = _sampo(argv)
    for _ in range(CLOSE_RETRIES):  # trunk moved under the run (a train's fast-forward): again, at most CLOSE_RETRIES reruns
        if not (rc and cli.TREE_MOVED in (err or out)):
            break
        rc, out, err = _sampo(argv)
    if rc and cli.TREE_MOVED in (err or out):  # the next land, then
        return _why(err or out) + ("; its lane is kept" if path else ""), ""  # no verdict on the task: nothing logged
    if rc:
        why = _why(err or out) + ("; its lane is kept" if path else "")
        nb = cli.need_brief(p, tid)  # after the refusal: it may have touched the brief, and the hash must match then
        nb.append_log(f"land: close refused: {c.fit(' '.join(why.split()), 400)} @ {_brief_hash(nb)} {sha} {_code_id()}")
        c.save_brief(p, nb)
        return why, ""
    # proved and done, and the finish wrote the mark that its commit is owed with the done itself (T-5368: a crash
    # between the two left the task done and its CHANGELOG line never committed); a land that can't take the lock now
    # leaves the commit to the next
    why, out2 = _commit_close(p, tid, title, path, _wait(commit_wait), "" if brief_fields else f["changelog"])
    if why:
        return why, ""
    if kinds and settled is not None:  # T-5535: counted once it has closed, not when it settled
        settled.append(tid)
    return _finish_close(p, main, tid, path, title, out + "\n" + out2, time.monotonic() - t_finish)


def _commit_pending(b):
    """T-5368: True when the task's newest close mark says its criteria passed and its commit is still owed. A mark is a
    Log line that is exactly `- <time> <mark>`: a note that merely ends with its text is none."""
    marks = [m for x in b.section("Log").splitlines()
             if (m := re.fullmatch(r"- \S+ (" + re.escape(PENDING_COMMIT) + "|" + re.escape(COMMITTED) + r")", x.rstrip()))]
    return bool(marks) and marks[-1].group(1) == PENDING_COMMIT


COMMIT_WAIT = "a running train holds trunk"  # T-5368: the start of the refusal a commit that timed out on a train's lock gives


def _commit_close(p, tid, title, path, timeout=None, changelog=""):
    """T-5368: the second half of a land's close: the CHANGELOG line and the commit of a done task, under the train's
    lock (T-4540: a running train is waited for, bounded, never committed beside; `timeout` seconds, default
    lanes.TRUNK_WAIT_S). ('', the finish's output), or (why it is left, ''): the mark stays, and the next land commits it.
    `changelog`: the line a lane's commit trailers gave (T-5496), which this finish can't read off the brief's Close fields."""
    from sampo import cli
    keep = "; its lane is kept" if path else ""
    try:
        with _own_commit(), train_lock_closing(p, timeout):
            rc, out, err = _sampo(["task", "finish", tid, "--commit", title,
                                   *([f"--changelog={changelog}"] if changelog else [])])
    except c.LockTimeout:
        return COMMIT_WAIT + ": the close's commit waits for it, and the next land retries it" + keep, ""
    if rc:
        return _why(err or out) + keep, ""
    nb = cli.need_brief(p, tid)
    nb.append_mark(COMMITTED)
    c.save_brief(p, nb)
    return "", out


def _finish_close(p, main, tid, path, title, out, finish_s):
    """The lane goes once its task is closed and committed ('', the close's last line)."""
    from sampo import cli
    from sampo import lanes
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
                 f"{_closed_text(got)}, {len(got['left'])} left active ({got['gate']})."
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
    lines = [f"Land: {len(got['landed'])} lane(s) landed in {got['trains']} train(s): {_closed_text(got)}, "
             f"{len(got['hand'])} left for a hand close, {len(got['parked'])} parked."
             if got["landed"] or got["parked"] or got["stopped"] else "Land: no finished lane waits on a merge."]
    lines += [f"  Train {t['train']}: {len(t['lanes'])} lane(s), gate {t['gate']} min, waiting {t['waiting']} min"
              for t in got.get("train_times", [])]
    skipped = sum("no retry)" in w for _, w in got["hand"])
    lines += [f"  {skipped} refused close(s) not retried: brief and lane tip unchanged since the last land"] * bool(skipped)
    lines += [f"  closed {t}: {c.fit(c.plain(x), 160)}" for t, x in got.pop("lines")]
    lines += _hand_close_lines(got["hand"])  # T-5516: grouped by reason, then the per-lane lines
    lines += [f"  parked {t}: {w}" for t, w in got["parked"]]
    lines += [f"Stopped: {got['stopped']}"] if got["stopped"] else []
    lines += ([wait] if wait else [f"Ship ({got['ship_grant']}'s grant): {c.fit(c.plain(got['ship']), REFUSAL_LINE)}"]
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
STALE_POLL_S = 15.0  # T-5561 review: how often a land waiting on the checks it started reads every lane's brief again
STALE_WAIT_S = CHECK_WALL_S + 600  # T-5561: the longest a land with nothing to train waits for the checks it started
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


SHAPE_RUNS_ENV = "SAMPO_SHAPE_RUNS"


def shape_runs():
    """T-4947: how many lanes' repo-shape stages run at once in a project (SAMPO_SHAPE_RUNS, default 3, at least 1, at
    most LANE_CHECKS_MAX). T-4563 ran them before the turn with no cap: 15 lanes accepted together ran 15 at once
    (2026-11-17: load 76 on 16 cores, every other run slowed ~5x, and a stage near SHAPE_WALL_S is cut, which also
    keeps its lane out of a batch check). A further one waits as 'shape queued'; the stage's wall starts with its slot."""
    try:
        return min(LANE_CHECKS_MAX, max(1, int(os.environ.get(SHAPE_RUNS_ENV) or 3)))
    except ValueError:
        return 3
_SCRUBBED = ("FM_STALL", c.RUN_ID_ENV, "FOREMAN_TEST_RUN_ID")  # env prefixes a lane's check never inherits: a test's override, a nested run's id (the legacy name too)
_CHECK_KEYS = ("check_state", "check_tip", "check_pid", "check_ticks", "check_log", "check_tries", "check_failed",
               "check_queued", "check_solo", "check_shape",  # T-4563: the tip the repo-shape modules ran green at
               "check_shape_solo",  # T-5380: the tip a shape batch went red or was cut at: its own stage runs alone
               "check_shape_lead",  # T-5380: "<leader id>:<pid>:<ticks>" while a live shape batch holds the lane
               "check_first",  # T-4892: when this tip's first try started (the wall budget over all its tries)
               "check_code",  # T-4404: the checker's code stamp at the start
               "check_cut_timeout")  # T-4767: the --timeout a cut-off check's deadline line advised
BATCH_MAX = 8  # T-4528, T-5380: lanes (the turn's own included) checked as one candidate (a long check's, a shape stage's); the rest wait their own turn


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


def _check_since(main, b, tip):
    """T-4355: the base a merge-commit tip's check diffs from (such a tip has no diff of its own), else None."""
    if len(_git(main, "rev-list", "--parents", "-n", "1", tip).stdout.split()) > 2:
        return b.meta.get("base") or None
    return None


def _spawn_check(p, b, main, *, tip, since, tries, low=False):
    """T-4286: the detached, niced process that runs the lane's check (see _accept_check_child) in its own session,
    its output in the state's check/ID.log; never waited on. (pid, log), or None when FOREMAN_NO_BACKGROUND is set
    (the tests') or sampo is paused. The one function the tests replace: none starts a real check.
    T-4862: try 1 starts the log afresh; a retry (tries > 1) appends, so the cut-off try's lines stay. Each try writes a
    header (try N, the tip, the UTC time) before its process starts, so the file reads as one block per try."""
    if os.environ.get("FOREMAN_NO_BACKGROUND") or c.panicked():
        return None
    d = os.path.join(p.dir, "check")
    os.makedirs(d, exist_ok=True)
    log = os.path.join(d, f"{b.id}.log")
    arg = json.dumps({"accept": {"project": [p.slug, p.root, p.dir, p.lane], "main": main, "id": b.id, "tip": tip,
                                 "tries": tries, "since": since, "argv": _check_argv(b, since)}})
    lib = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = _scrub_env({**os.environ, "PYTHONPATH": lib + os.pathsep + os.environ.get("PYTHONPATH", "")})
    if low:  # T-5561 review: the land's own start of a stale lane's check: its tests take freed slots after the train gate's
        env[c.SLOT_PRIORITY] = "0"
    with open(log, "a" if tries > 1 else "w", encoding="utf-8") as out:  # T-4862: a retry keeps the try before it
        out.write(f"sampo: try {tries} on {tip[:9]} at {c.now()}\n")
        out.flush()  # the header precedes the child's own lines, which it writes to this file as it runs
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
        if m.get("check_queued") == "shape":  # T-4947: its process waits for one of the shape_runs() slots
            return (f"held: shape queued: its repo-shape modules wait behind {shape_runs()} lanes' running at most (pid "
                    f"{m.get('check_pid')}); they start when one ends, then its check")
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
            queued = st == "running" and _check_alive(b.meta) and b.meta.get("check_queued")
            out[b.id] = ("shape queued" if queued == "shape" else "check queued" if queued else
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
    land starts and trains it first, ('started', (pid, log)), or ('not started', None) in the tests and when paused;
    T-5816: ('merging', None) while a lane merge holds the train lock (no land starts; the lane waits in the queue).
    T-4528: `tid` may be a list (a batch's members, in order): one call, so one land is started for all of them."""
    first_ids(p, [tid] if isinstance(tid, str) else list(tid))
    if landing(p):
        return "running", None
    if train_live(p):  # T-5816: a lane merge (a train by hand) holds the train lock: a land started now is refused at once
        return "merging", None  # and logs a refusal; the lane waits in first_ids for the next land
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


def _start_check(p, b, main, tip, since, tries, restarts=None, low=False):
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
        started = _spawn_check(p, b, main, tip=tip, since=since, tries=tries, **({"low": True} if low else {}))
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
    # T-5589: the check `sampo lane accept` starts is one someone waits on: its tests take freed slots ahead of the
    # background and ordinary runs (an explicit SAMPO_SLOT_PRIORITY=0 opts out, as c.run_command keeps it)
    env[c.SLOT_PRIORITY] = env.get(c.SLOT_PRIORITY) or "1"
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
def _check_turn(p, waits=None, started=None, name="turn", n=None, until=None):
    """T-4416, T-4491: at most lane_checks() lane checks at a time in a project. Five started together (2026-11-11: 1,275
    test modules for 16 slots) were each still running after 15 to 25 minutes and a 59-module one was cut off at its
    run's deadline; in turn the work is the same and the first result comes after one run. One flock per slot
    (check/turn-N.lock), held by the check's own process around its run and released by the kernel however that process
    ends. A free slot is taken at once. With none free, `waits()` once (the task is told: check queued), then the process
    polls for one every TURN_POLL_S, with no session open; `started()` once it has one, if it waited. No signal handler
    is set here, so the SIGTERM of sampo lane rm (stop_check) ends a waiting one. The land's gate and ship do not take it.
    T-4947: `name` and `n` make another pool of the same kind (check/shape-N.lock, shape_runs() of them).
    T-5380: `until`, asked at each poll (never when a slot is free), ends the wait with no slot: the turn yields False
    (True when it holds one), and `started()` is called if it had waited."""
    d = os.path.join(p.dir, "check")
    os.makedirs(d, exist_ok=True)
    slots = [open(os.path.join(d, f"{name}-{i}.lock"), "a") for i in range(n or lane_checks())]
    try:
        waited, got = False, True
        while not _take_slot(slots):
            if until and until():
                got = False
                break
            if not waited:
                waited = True
                if waits:
                    waits()
            time.sleep(TURN_POLL_S)
        if waited and started:
            started()
        yield got
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
        if (x.id == me or m.get("check_state") != "running" or m.get("check_queued") in (None, "", "shape")
                or not _check_alive(m) or int(m.get("check_tries") or 1) != 1):  # T-4947: a shape wait is no turn wait
            continue
        t = _lane_tip(main, x)
        if t and m.get("check_tip") == t and m.get("check_solo") != t and _shape_clear(x, t, main):
            out.append((x, t))
    return sorted(out, key=lambda r: c.id_num(r[0].id))[:max(BATCH_MAX - 1, 0)]


def _batch_argv(base):
    """T-4528: the candidate's check: the lane check's command over everything changed since the trunk it sits on, and
    no --evidence (the run is no one lane's own)."""
    return [os.path.join(c.PLUGIN_ROOT, "bin", "sampo"), "check", "--affected", "--hold", "--json", "--since", base]


def _mark_members(p, main, members, note, solo=False, solo_key="check_solo", queued=None, **meta):
    """T-4528: write `meta` (None removes a key), `solo_key` (check_solo; T-5380: the shape stage's own) = the member's
    tip when `solo`, and `note` on each member (brief, tip) whose check still waits at that tip, and only those: a lane
    whose tip moved or whose check ended meanwhile keeps whatever it has. T-5380: `queued` also requires its
    check_queued to be that still (a shape peer that took a slot meanwhile runs its own stage). The ids written."""
    from sampo import cli
    wrote = []
    for x, t in members:
        def go(y, t=t):
            if y.meta.get("check_state") != "running" or y.meta.get("check_tip") != t or _lane_tip(main, y) != t:
                return
            if queued and y.meta.get("check_queued") != queued:
                return
            for k, v in meta.items():
                if v is None:
                    y.meta.pop(k, None)
                else:
                    y.meta[k] = v
            if solo:
                y.meta[solo_key] = t
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
        with _check_turn(p, lambda: _set_check(p, b.id, f"shape queued: {shape_runs()} lanes' repo-shape modules run at once "
                                                        "in this project (T-4947); they start when one ends", check_queued="shape"),
                         lambda: _set_check(p, b.id, check_queued=None), name="shape", n=shape_runs(),
                         until=lambda: _shape_cleared(p, main, b.id, tip)) as slot:
            # T-4947: at most shape_runs() at once; a tip that moved during the wait runs nothing
            if not slot:  # T-5380: a batch cleared it while it waited: out of the shape queue at once, no slot taken
                _set_check(p, b.id, check_queued=None)
                verdict = "green"
            else:
                verdict = _shape_stage(p, b, main, tip) if _lane_tip(main, cli.need_brief(p, b.id)) == tip else "moved"
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
    rows = [tuple(x) for x in (getattr(args, "audit", None) or [])]  # T-5516: none typed is fine: audits on the brief count
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
        raise cli.UsageError(f"could not read {', '.join(repr(w) for w in more)} after the "
                             f"{rows[-1][0] + ' audit' if rows else 'audit options'}; {form}")
    return rows


# T-5516: the hand-close reasons with a known clearing command, in the order their groups print. The first text that holds
# one of them names the group. Any other reason clears the way its per-lane line says: focus, then finish.
_HAND_REASONS = [
    ("no audit recorded", 'sampo task audit {t} <lens> "<what you checked>" "<result>"'),
    ("audits predate the lane tip", 'sampo task audit {t} <lens> "<what you checked>" "<result>"'),
]
_HAND_FALLBACK = "sampo focus {t}; sampo task finish {t}"


def _hand_close_lines(left):
    """T-5516: the hand-close leftovers [(id, why)] as one line per reason, `  hand close group (N) REASON: IDS (clear with:
    COMMAND, once for each id)`, N the lanes it leaves and COMMAND filled for the first id, then the per-lane lines as
    they were. The known reasons come first in _HAND_REASONS order; any other reason groups by its text with shas and
    task ids blanked, in first-seen order."""
    groups = {}  # reason: (command, [ids])
    for t, w in left:
        text = " ".join(str(w).split())
        known = next(((lab, cmd) for lab, cmd in _HAND_REASONS if lab in text), None)
        label, cmd = known or (re.sub(r"\bT-\d+\b", "<id>", re.sub(r"\b[0-9a-f]{7,40}\b", "<sha>", text)), _HAND_FALLBACK)
        groups.setdefault(label, (cmd, []))[1].append(t)
    known_labels = [lab for lab, _ in _HAND_REASONS]
    order = [lab for lab in known_labels if lab in groups] + [lab for lab in groups if lab not in known_labels]
    lines = [f"  hand close group ({len(groups[lab][1])}) {lab}: {', '.join(groups[lab][1])} (clear with: "
             f"{groups[lab][0].format(t=groups[lab][1][0])}, once for each id)" for lab in order]
    return lines + [f"  hand close {t}: {w} (then sampo focus {t}; sampo task finish {t})" for t, w in left]


def _counted_lenses(b, path):
    """T-5516: the lenses with an audit on the brief recorded against the lane's current tree, whoever recorded it
    (sampo task audit, a reviewer's report, or accept's own rows). The tree decides, as audit_blockers judges it, not
    the clock: an audit with no tree, or one against another tree, counts none."""
    tree = c.worktree_id(path)
    return {lens for lens, _, t in b.audits() if tree and t == tree} if tree else set()


def _accept_gaps(b, path, main, rows, counted=(), p=None, tier_out=None):
    """T-4487: what the close would refuse for the lane at `path` once these audits (rows) are on its brief: the lens
    groups its process tier needs (cli._audit_gaps, the close's own rule), and adversary when the lane's own diff is
    security-sensitive (the close's _adversary_gaps). Judged against the lane's tree, before anything is recorded.
    T-5516: a lens in `counted` (an audit on the lane's tree, see _counted_lenses) is given without a row.
    T-5715: the lane's diff is read once, for the sensitive check and the process tier (core.ptier_now, linked tests
    from project `p`'s map); `tier_out`, a dict, gets the tier and its reasons for the caller to stamp."""
    from sampo import cli
    tree = c.worktree_id(path)
    base = c.task_base(main, b)  # the close reads the same start point: a task with none is judged on its audits alone
    names = c.task_diff(path, base, "--name-only", "--no-renames") if base and tree else None
    if not base:
        risky = []
        ptier, why = c.unmeasured_ptier(b), []
    elif names is None:  # unreadable: counted as sensitive, as the close does
        risky = ["diff unavailable, so its content wasn't checked"]
        ptier, why = c.unmeasured_ptier(b), []
    else:
        files, diff = sorted(set(names.splitlines())), c.task_diff(path, base) or ""
        risky = c.sensitive(files, diff)
        ptier, why = c.ptier_now(b, files, diff, cli._tests_linked(p, b, files) if p else {})
    if tier_out is not None:
        tier_out.update(ptier=ptier, why=why)
    given = [(lens, how) for lens, how, _ in rows] + [(lens, "") for lens in sorted(counted)]
    gaps = cli._audit_gaps(b, given, b.audit_blockers(tree=tree, groups=c.audit_groups(b.tier, ptier)), ptier)
    if risky and "adversary" not in {lens for lens, _ in given}:
        gaps += [r + cli._adversary_ask(risky) for r in b.audit_blockers(tree=tree, need=("adversary",))
                 if r.startswith("audit missing: adversary")]
    return gaps


SHAPE_WALL_S = 900  # T-4542: the repo-shape modules in a lane before its long check; past it, no verdict (the check runs them)


def _shape_tree(path):
    """T-4542: whether a lane's tree is a Sampo tree with the repo-shape modules (another project's is not held to them)."""
    return os.path.isfile(os.path.join(path, "plugin", "tests", "run.py"))


SHAPE_BATCH_WALL_S = 2 * SHAPE_WALL_S  # T-5380: a batch's run, over the merged tree of up to BATCH_MAX lanes; a cut one costs each lane its own run on top
SHAPE_BATCH_WALL_ENV = "SAMPO_SHAPE_BATCH_WALL_S"


def shape_batch_wall():
    """T-5380: the wall of a shape batch's run in seconds (SAMPO_SHAPE_BATCH_WALL_S, a positive whole number; else
    SHAPE_BATCH_WALL_S): a larger tree than one lane's, and a cut one makes every member run its own stage after it."""
    try:
        got = int(os.environ.get(SHAPE_BATCH_WALL_ENV) or SHAPE_BATCH_WALL_S)
    except ValueError:
        return SHAPE_BATCH_WALL_S
    return got if got > 0 else SHAPE_BATCH_WALL_S


_SHAPE_CLASS_ID = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?")  # a failing class as the run record names it: module[.Class]
_NO_TEST_MATCHES = re.compile(r"(?m)^run\.py: no test matches ([^\n;]*)")  # run.py's own line, as its early exit prints it
_TESTS_RAN = re.compile(r"(?m)^Ran [1-9]\d* tests?\b")  # a run that ran tests did not end for want of a match


def _shape_selectors(classes):
    """T-5853: run.py's names for `classes` (the red classes a run named, joined: `module.Class` becomes `module::Class`,
    a bare module stays), or None for the whole repo-shape set: no class, or a name that is no class id (never put
    into a command)."""
    names = [n for n in re.split(r"[,\s]+", classes or "") if n]
    if not names or not all(_SHAPE_CLASS_ID.fullmatch(n) for n in names):
        return None
    return list(dict.fromkeys(n.replace(".", "::") for n in names))


def _shape_run(path, wall=None, classes=None):
    """T-4542: ('green' | 'red' | 'cut', the failing classes named) for the repo-shape modules run once in the lane's tree,
    niced. Red names the classes its run record carries (else the classes its output names). Cut is no verdict (the
    deadline, a run that left no record and named no class): the lane's own check runs the same classes with --hold.
    T-5380: `wall` (seconds) replaces SHAPE_WALL_S: a batch's run has its own.
    T-5853: `classes` (the names a red run gave) narrows the run to them: a tree that has none of a class (a lane adds
    its module, and the subset leaves that lane out) is not red there, so run.py's "no test matches" for a name drops
    it and the rest run (none left: green). Only run.py's own line counts (at a line start, no "Ran N tests" line), and
    a class whose module file is in the tree runs as the bare module (it may fail to import)."""
    names = _shape_selectors(classes)
    while True:
        cmd = "python3 plugin/tests/run.py " + " ".join(names or c.repo_shape_modules())
        rid = c.new_run_id(cmd)
        code, out = c.run_command(path, cmd, wall or SHAPE_WALL_S, niced=True, run_id=rid)
        if code == 0:
            return "green", ""
        gone = {n.strip() for n in m.group(1).split(",")} & set(names) if names and code == 1 and (
            m := _NO_TEST_MATCHES.search(out or "")) and not _TESTS_RAN.search(out) else set()
        if not gone:
            break
        # a `module::Class` whose module file is in the tree matches nothing when that module fails to import (run.py
        # names it `unittest.loader._FailedTest.<module>`): the bare module runs, and its import failure is the red
        left = []
        for n in names:
            mod = n.partition("::")[0]
            if n not in gone:
                left.append(n)
            elif "::" in n and mod not in gone and os.path.isfile(os.path.join(path, "plugin", "tests", mod + ".py")):
                left.append(mod)  # a bare module is dropped when it matches nothing too, so this ends
        if not (names := list(dict.fromkeys(left))):
            return "green", ""
    rec = c.run_record(path, cmd, rid) if code else None
    if code == 124 or (rec or {}).get("not_run"):
        return "cut", ""
    got = sorted({f["class"] for f in (rec or {}).get("failed") or [] if f.get("class")}) or c.failed_classes(out)
    return ("red", ", ".join(got)) if got else ("cut", "")


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


def _shape_peers(p, main, me, tip):
    """T-5380: the lanes whose shape stage waits for a slot behind this one, as (brief, tip), oldest task first, at most
    BATCH_MAX - 1: their process alive and queued as 'shape' (one that took a slot is running its own stage), claimed
    at the lane's current tip on its first try, not already cleared there, not marked for a solo stage, and not held by
    another live leader's batch (check_shape_lead: a second slot's leader would run the same lanes again)."""
    out = []
    for x in c.load_briefs(p):
        m = x.meta
        if (x.id == me or m.get("check_state") != "running" or m.get("check_queued") != "shape" or not _check_alive(m)
                or int(m.get("check_tries") or 1) != 1 or _shape_held(m, me)):
            continue
        t = _lane_tip(main, x)
        if t and m.get("check_tip") == t and m.get("check_shape") != t and m.get("check_shape_solo") != t:
            out.append((x, t))
    return sorted(out, key=lambda r: c.id_num(r[0].id))[:max(BATCH_MAX - 1, 0)]


def _shape_held(m, me):
    """T-5380: whether a lane's meta carries a shape batch's claim ('<leader id>:<pid>:<ticks>') of a live process other
    than lane `me`'s (a dead leader's, a reused pid's, a malformed one is no claim)."""
    lead, _, rest = str(m.get("check_shape_lead") or "").partition(":")
    try:
        pid, ticks = (int(x) for x in rest.split(":"))
    except ValueError:
        return False
    return bool(lead) and lead != me and ticks > 0 and _ticks(pid) == ticks


def _shape_claim_id(lead):
    """T-5380: '<lead lane id>:<this pid>:<its start ticks>', the value a leader writes as check_shape_lead."""
    return f"{lead}:{os.getpid()}:{_ticks(os.getpid())}"


def _shape_claim(p, main, members, claim, held):
    """T-5380: write check_shape_lead = `claim` on each member (brief, tip) that still waits for a shape slot at that tip
    and that no other live leader holds, under the brief's lock (two leaders asking at once get disjoint lanes). Each
    one written is appended to `held` as it is (so a failure part way still leaves the caller what to drop): the list."""
    from sampo import cli
    lead = claim.partition(":")[0]
    for x, t in members:
        def go(y, t=t):
            m = y.meta
            if (m.get("check_state") != "running" or m.get("check_tip") != t or m.get("check_queued") != "shape"
                    or _lane_tip(main, y) != t or _shape_held(m, lead)):
                return
            m["check_shape_lead"] = claim
            held.append((y, t))
        cli.mutate(p, x.id, go, "check", {})
    return held


def _shape_unclaim(p, members, claim):
    """T-5380: drop `claim` from each member's brief (a claim another leader has since written stays)."""
    from sampo import cli
    for x, _ in members:
        def go(y):
            if y.meta.get("check_shape_lead") == claim:
                y.meta.pop("check_shape_lead", None)
        try:
            cli.mutate(p, x.id, go, "check", {})
        except Exception as e:  # a lane removed meanwhile; a dead leader's claim reads as none anyway
            print(f"shape claim not dropped on {x.id}: {type(e).__name__}", file=sys.stderr)


def _shape_cleared(p, main, me, tip):
    """T-5380: whether a batch has cleared lane `me`'s repo-shape modules at `tip` (its tip still that): the lane
    waiting for a shape slot leaves the queue at once."""
    from sampo import cli
    cur = cli.need_brief(p, me)
    return cur.meta.get("check_shape") == tip and _lane_tip(main, cur) == tip


def _shape_batch(p, main, b, tip, peers, run=None):
    """T-5380: this lane and the lanes waiting for a shape slot as one candidate: a scratch copy of the trunk with their
    branches merged (a lane that conflicts with the others is left out; this lane conflicting means no batch), the
    repo-shape modules run once on it. Green: every member that still waits at its tip is marked check_shape = tip (the
    peers also still queued as 'shape'), so each skips its own stage; True when this lane is one. Red or cut off: no one
    is cleared, each member gets a check_shape_solo mark and runs its own stage (the cost of a miss is this one run).
    False when no batch ran (a single lane, a conflict with the trunk): the caller runs this lane's stage alone.
    The peers are claimed first (check_shape_lead, dropped when the batch ends) so a second slot's leader batches others;
    the run has its own wall (shape_batch_wall())."""
    run = run or _shape_run
    base = _git(main, "rev-parse", "HEAD").stdout.strip()
    if not base:
        return False
    claim, held = _shape_claim_id(b.id), []
    try:
        peers = _shape_claim(p, main, peers, claim, held)
        if not peers:
            return False
        # T-4728: a tree removed under the batch is rebuilt once; lost twice, no batch
        return _rebuilding(p, "the shape batch", lambda: _shape_batch_once(p, main, b, tip, peers, run, base))
    except c.PolicyError as e:
        print(f"shape batch skipped: {c.redact(str(e))[:200]}", file=sys.stderr)  # the check child's own line
        return False
    finally:
        _shape_unclaim(p, held, claim)


def _shape_batch_once(p, main, b, tip, peers, run, base):
    """_shape_batch's work on one scratch tree; raises _Lost when something else removes the tree."""
    wt = c.scratch_tree("sampo-shape-", main)  # T-4735
    try:
        if _git(main, "worktree", "add", "-q", "--detach", wt, base).returncode:
            return False
        merged, log = [], []
        for x, t in [(b, tip), *peers]:
            _lost_check(wt)
            if _merge_in(wt, x, t, log, kept={}):
                merged.append((x, t))
            elif x.id == b.id:
                _lost_check(wt)
                return False
        if len(merged) < 2:
            return False
        what = run(wt, shape_batch_wall())[0]
        _lost_check(wt)  # a run in a vanished folder is no red batch
        n, mine, theirs = len(merged), merged[:1], merged[1:]
        if what != "green":
            _mark_members(p, main, merged, f"shape batch of {n} lanes was {'red' if what == 'red' else 'cut off'} on {{tip}}: "
                                           "each lane runs its own repo-shape stage", solo=True, solo_key="check_shape_solo")
            return False
        note = f"repo-shape modules green on {{tip}} as one of a batch of {n} lanes (one run on {base[:9]}): no stage of its own"
        got = _mark_members(p, main, mine, note, check_shape=tip)
        for x, t in theirs:  # check_shape is each member's own tip: one call each; only those still waiting for a slot
            _mark_members(p, main, [(x, t)], note, queued="shape", check_shape=t)
        return b.id in got
    finally:
        _git(main, "worktree", "remove", "--force", wt)
        shutil.rmtree(wt, ignore_errors=True)
        c.scratch_release(wt)
        _git(main, "worktree", "prune")


def _shape_stage(p, b, main, tip):
    """T-4563: the lane's repo-shape modules, run first in its check child, before the turn (so lanes run theirs at once).
    'red': the lane's tip is recorded red with the failing classes (no retry until it moves) and no long check runs.
    'green': check_shape = tip, so the hold check runs without the classes. 'cut': no verdict, the long check runs them.
    'moved': the tip changed meanwhile; nothing is written, the newer tip's check decides."""
    from sampo import cli
    cur = cli.need_brief(p, b.id)
    if cur.meta.get("check_shape") == tip and _lane_tip(main, cur) == tip:
        return "green"  # T-5380: a batch run by another lane's stage cleared it while it waited for its slot
    if cur.meta.get("check_shape_solo") != tip:
        try:
            peers = _shape_peers(p, main, b.id, tip)
            if peers and _shape_batch(p, main, cur, tip, peers):
                return "green"
        except Exception as e:  # a batch that cannot be built costs nothing: the lane's own stage follows
            print(f"shape batch skipped: {type(e).__name__}: {c.redact(str(e))[:200]}", file=sys.stderr)
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
    counted = _counted_lenses(b, path)  # T-5516: audits already on the brief for the lane's tree count, typed or not
    tier = {}  # T-5715: the process tier the lane's diff scored, filled by _accept_gaps
    gaps = _accept_gaps(b, path, main, rows, counted, p=p, tier_out=tier)
    if gaps:  # T-4487: refused before anything is recorded or started: the close would refuse the lane for these
        msg = (f"{b.id} not accepted, nothing recorded: the close would refuse this lane: {'; '.join(gaps)}. "
               f"Accept again with the audits it names ({AUDIT_FORM}, one per lens)")
        if not rows and not counted:  # T-5516: nothing typed and nothing on the brief after the tip: a usage refusal
            raise cli.UsageError(msg)
        raise c.PolicyError(msg)
    tree = c.worktree_id(path)
    added = 0
    for lens, how, result in rows:
        _, new = cli.mutate(p, b.id, lambda x, lens=lens, how=how, result=result: _add_audit_once(x, lens, how, result, tree),
                            "audit", {"lens": lens, "how": how[:200], "result": result[:300]})
        added += bool(new)
    if tip and (rows or counted):  # T-5359: the review saw this tip: for a lane whose launcher lives (no builder_returned)
        # that is its return, so the awaiting test below counts it; accepted_at is still stamped only when the lane really
        # queues (T-4239). T-5516 item 8: an accept that recorded and counted no audit saw nothing, so it stamps nothing
        cli.mutate(p, b.id, lambda x: x.meta.__setitem__("reviewed_tip", tip), "reviewed", {"tip": tip[:12]})
    ptier, ptier_why = tier.get("ptier") or "", tier.get("why") or []
    # T-5715: the tier the review judged the lane at, raise-only (the close reads it as a floor); written and logged
    # only when the stamp rises
    if ptier_why and c.ptier_rises(cli.need_brief(p, b.id), ptier):
        cli.mutate(p, b.id, lambda x: c.stamp_ptier(x, ptier, ptier_why), "ptier", {"ptier": ptier})
    b = cli.need_brief(p, b.id)
    lines = [f"accept {b.id}: {added} audit(s) recorded"
             + (f" ({', '.join(sorted({lens for lens, _, _ in rows}))})" if rows else "")
             + (f", {len(rows) - added} already on the brief" if added < len(rows) else "")
             + (f"; counted from audits after the lane tip: {', '.join(sorted(counted))}" if counted else "") + "."]
    if stale := lanes.stale_stack(p, b):  # T-5725 review: the dependency this lane is stacked on has a newer tip
        lines.append(f"  {stale}")
    lines += [f"  the close will be refused: {g}" + ("" if g.startswith("no red→green proof") else
                                                     " (add it to the brief's Close fields before the land closes it)")
              for g in _close_gaps(b, ptier or None)]  # T-4209: the proof is recorded in the lane (sampo task prove), not a Close field
    if b.id not in lanes.awaiting_merge(p, c.load_briefs(p), 0, fresh=True):  # T-4265: the review just saw its tip
        lines.append(f"  not queued: {b.id} is not a finished lane waiting on a merge (commits, a clean tree); "
                     "fm lane list says what it waits on")
        return cli.out(args, {"id": b.id, "queued": False, "audits": len(rows)}, "\n".join(lines))
    if not skip_check and not live_mode(p) and (not clean or _check_hold(b, tip, main)):
        # T-4286: no clean run at its tip, or its check holds it (running, red, cut off: what the land would skip it for):
        # the lane's own check decides, then it queues; a hold is said as "held", never as queued. T-5447: a live
        # project starts no lane check: the behind suite judges the merge and the land still holds a red check
        return _accept_check(p, b, main, args, tip, lines)

    def accepted(x):  # T-4239: the start of its accept-to-live time; a check a newer green run cleared is green now
        x.meta["accepted_at"] = round(time.time(), 1)
        if ptier_why:
            c.stamp_ptier(x, ptier, ptier_why)  # T-5715: raise-only, idempotent with the stamp above
        if x.meta.get("check_state") in ("running", "red", "cut_off") and x.meta.get("check_tip") == tip:
            x.meta["check_state"] = "green"
            for k in ("check_failed", "check_pid", "check_ticks"):
                x.meta.pop(k, None)
    cli.mutate(p, b.id, accepted, "accepted", {"at": round(time.time(), 1)})
    how, started = _queue(p, main, b.id)
    if how == "running":
        lines.append(f"  {b.id} queued first in the running land: it trains at the land's next train boundary.")
        return cli.out(args, {"id": b.id, "queued": True, "land": "running"}, "\n".join(lines))
    if how == "merging":  # T-5816
        lines.append(f"  {b.id} queued first, no land started: a sampo lane merge is running (it holds the train lock); "
                     "run sampo lane land once it ends and it trains this lane first.")
        return cli.out(args, {"id": b.id, "queued": True, "land": "merge running"}, "\n".join(lines))
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
    since = _check_since(main, b, tip)
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

