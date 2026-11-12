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
from sampo.lanes import CriterionRed, _git, _ref_ok, _verify, cpu_env, pm_record, pm_rerun_cmd, pm_wait

SKIPPED = "probe skipped"  # the note a held green run leaves; the report counts by it (T-3596)
PROBE_JOBS = 3 # T-3531: pre-filter probes at once
_FAILED = re.compile(r"^(?:FAIL|ERROR): (\S+) \(([\w.]+)\)", re.M)  # unittest's red line: test (where)
_LISTED = re.compile(r"^  failed class: (\S+)", re.M)  # run.py's every red class, one per line, never-started ones too
_CUT = re.compile(r"^  not run class: (\S+)", re.M)  # T-3936: run.py's classes the deadline cut (the train runs them too)
_COUNT = re.compile(r"^FAILED \((\d+) classes", re.M)  # run.py's summary count
_FIXTURES = ("setUpModule", "tearDownModule", "setUpClass", "tearDownClass")  # unittest names the module or class
_FRAME = re.compile(r'^\s*File ".*", line \d+.*$', re.M)  # a traceback frame: the test and the code on its stack
_stem = lambda t: os.path.basename(t).rsplit(".", 1)[0]  # noqa: E731  a test file's module name


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
    wt = tempfile.mkdtemp(prefix="sampo-base-")
    try:
        if _git(main, "worktree", "add", "-q", "--detach", wt, base).returncode:
            return ""
        ts = [t for t in sub[2] if os.path.exists(os.path.join(wt, t))]
        if not ts:
            return ""
        run = sub[1].replace("{tests}", shlex.join(ts)).replace("{names}", shlex.join(map(_stem, ts)))
        def go(cmd):  # yields to the machine's load and to other work, as _probe's runs do
            c.wait_for_load("sampo lane train")
            if shutil.which("nice"):
                cmd = "nice -n 10 bash -c " + shlex.quote(cmd)
            return c.run_command(wt, cmd, c.load_scaled(1800))

        code, out = go(run)
        if code not in (0, 5, 124):  # one flake rerun of the same affected tests
            code, out = go(run)
        if code in (0, 5, 124):  # a run of no tests or a timeout proves nothing: the full gates decide
            return ""
        return _why_red({"cmd": sub[1], "affected": ts, "failing": _red(out),
                         "tail": out.rstrip().splitlines()[-15:]})
    finally:
        _git(main, "worktree", "remove", "--force", wt)
        shutil.rmtree(wt, ignore_errors=True)


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
    return f"its diff touches what {_mod(k)} tests" if any(_stem(t) == _mod(k) for t in tests) else ""


def _merge_in(wt, b, tip, log):
    """Merge `tip` into the scratch tree; a conflict is aborted and logged (id, files). The new head sha or None."""
    r = _git(wt, "-c", "user.name=sampo", "-c", "user.email=sampo@localhost", "merge", "--no-ff", "-m",
             f"Merge {b.id}: {b.title}", tip)
    if not r.returncode:
        return _git(wt, "rev-parse", "HEAD").stdout.strip()
    files = [f for f in _git(wt, "diff", "--name-only", "--diff-filter=U", "-z").stdout.split("\0") if f]
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
        files = r.stdout.splitlines()[1:]  # the first line is the merged tree
        return "conflicts with the base" + (f": {c.joinmore(files, 8)}" if files else "")
    if not tests:  # T-3770: the gate runs this lane's affected tests itself, and a red gate names its lane (T-3602)
        return ""
    wt = tempfile.mkdtemp(prefix="sampo-probe-")
    try:
        if _git(main, "worktree", "add", "-q", "--detach", wt, base).returncode or _git(
                wt, "-c", "user.name=sampo", "-c", "user.email=sampo@localhost", "merge", "--no-ff", "--no-edit",
                tip).returncode:
            return ""
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
            if shutil.which("nice"):  # the probe yields to whatever else the machine runs
                cmd = "nice -n 10 bash -c " + shlex.quote(cmd)
            code, out = c.run_command(cwd, cmd, c.load_scaled(900), priority=True,  # T-3614: ahead of lane runs
                                      env=cpu_env(p))  # T-3757: a CPU rise is red here too
            return code, set(_red(out))

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


def _held(p, b, main, tip):
    """The reason `sampo lane merge` would refuse this lane's files (guard, self-change gate), or ''."""
    import fmguard
    from sampo import selfgate
    meta = c.read_meta(p)
    ctx = fmguard.Ctx(cwd=main, project_root=main, home=os.path.expanduser("~"), foreman_home=c.foreman_home(),
                      state_dir=c.state_dir(), state_fallbacks=c.state_fallbacks(), scratch=[],
                      allow=set(b.meta.get("allow") or []), task_id=b.id, standing=set(meta.get("standing") or {}),
                      trusted=bool(c.trusted()))
    files = [f for f in _git(main, "diff", "--name-only", "--no-renames", "-z", f"HEAD...{tip}").stdout.split("\0") if f]
    for f in files:
        blk = fmguard.check("Write", {"file_path": os.path.join(main, f), "content": ""}, ctx)
        if blk:
            return f"the guard refuses {f} ({blk.category})"
    held = selfgate.gate(p, b, main, tip, files)
    return "the self-change gate holds it: " + c.joinmore(held, 4, "; ") if held else ""


def _none(head, left):
    """The refusal when no lane lands, carrying `left` [(id, why)] for sampo lane land to park each (T-3500)."""
    e = c.PolicyError(head + "; ".join(f"{i}: {w}" for i, w in left))
    e.left = list(left)
    return e


def _check(p, main, base, b, tip, probe_tests):
    """(why this lane can't land or '', notes): the lane's own acceptance commands re-run in its worktree, as `lane merge`
    does, then conflicts and red tests against the base (T-3474), all before any gate run."""
    from sampo import cli
    seen = []
    try:
        try:
            _verify(b)
        except CriterionRed as e:  # T-3629: red once, green on a rerun is a load flake, as _probe treats it
            _verify(b, only=e.n)  # that criterion alone: the others passed
            seen.append((b.id, f"flaky (red once, green on a rerun): {str(e).splitlines()[0][:160]}"))
    except (c.PolicyError, cli.UsageError) as e:
        return str(e).splitlines()[0], seen
    return _probe(p, main, base, b, tip, seen, probe_tests), seen


MARK = "SAMPO-CHECK "  # the line a lane's check process prints its result on (its stdout holds the criteria's own too)


def _child_argv(p, main, base, tid, tip, probe_tests):
    """T-3119: the command line of one lane's check process: `python -m sampo.train JSON`, run in the main checkout."""
    arg = json.dumps({"project": [p.slug, p.root, p.dir, p.lane], "main": main, "base": base, "id": tid, "tip": tip,
                      "tests": bool(probe_tests)})
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
    why, seen = _check(p, a["main"], a["base"], cli.need_brief(p, a["id"]), a["tip"], a["tests"])
    print(MARK + json.dumps({"why": why, "seen": seen}))
    return 0


def train(p, main, ids=None):
    """Land the named `ids` (a bare train would land lanes nobody picked) through one gate run, one train at a time.
    Returns a dict: merged, left_out [(id, why)], gate_runs, probes, tip, branch. Raises PolicyError when nothing can
    land, the base is red or the branch moved."""
    from sampo import cli
    ids = list(dict.fromkeys(ids or []))
    if not ids:
        raise cli.UsageError("name the lanes to land: sampo lane train T-0001,T-0002 (it never lands a lane nobody picked)")
    with c.lock(os.path.join(p.dir, "train"), timeout=1):  # a second train, not a lane merge, waits on this one
        return _run(p, main, ids)


def _run(p, main, ids):
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
    cands, rows = [], []  # (brief, tip) in order; (brief, tip, why so far) in order
    for tid in ids:
        b = cli.need_brief(p, tid)
        br = b.meta.get("lane_branch") or f"foreman/{b.id}"
        if b.meta.get("lane_spike"):
            rows.append((b, None, "a spike is never merged"))
        elif not _ref_ok(main, br):
            rows.append((b, None, f"no branch {br}"))
        else:
            tip = _git(main, "rev-parse", "--verify", f"{br}^{{commit}}").stdout.strip()
            rows.append((b, tip, _held(p, b, main, tip)))

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
    outs = iter(_fan([_child_argv(p, main, base, b.id, tip, probe_tests) for b, tip, why in rows if not why], main,
                     1 if c.host_strain(2.0) else PROBE_JOBS))
    for b, tip, why in rows:
        seen = []
        if not why:
            why, seen = _child_result(next(outs))
        notes += seen
        left.append((b.id, why)) if why else cands.append((b, tip))
    t1 = time.monotonic()
    if not cands:
        raise _none("nothing to merge: ", left)
    wt = tempfile.mkdtemp(prefix="sampo-train-")  # outside the main checkout; git worktree add takes the empty dir
    r = _git(main, "worktree", "add", "-q", "--detach", wt, base)
    if r.returncode:
        raise c.PolicyError(f"git worktree add failed: {r.stderr.strip()[:300]}")
    runs, probes, results, sub = [0], [0], [], None
    tips = {b.id: t for b, t in cands}
    meta = c.read_meta(p)
    tpl, other = meta.get("affected") or "", tuple(meta.get("affected_by") or ())
    bare = lambda s: " ".join(s.split())  # noqa: E731
    # T-3602: the gate a run narrows to named tests: the affected template's runner with no names
    suite = next((g for g in checks if "{names}" in tpl and bare(g) == bare(tpl.replace("{names}", ""))), None)
    passed, diffs, reaches = {}, {}, {}  # (tree, command line): its passing row; lane id: its files; lane id: its tests

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
            try:
                ts = cli.affected(c.Project(p.slug, wt, p.dir, p.lane), since=base, only=sorted(diff(i)))[1]
            except Exception:
                ts = []
            reaches[i] = {t for t in ts if not t.endswith(other)}  # another runner's tests: its own gate
        return reaches[i]

    def form(cmd, names=None):
        """(the command line gate `cmd` runs in the scratch tree, the test modules it names or None). T-3602: with
        `names` the suite runs only those whose module is in the tree (None, []: none is), a class left out when its
        module runs whole; the suite otherwise runs the batch's affected tests found there (T-3544: a prefix or the
        base lacks a later lane's new test file); any other gate runs as configured."""
        if names and cmd == suite:
            have = {_stem(f) for f in _git(wt, "ls-files", "-z").stdout.split("\0") if f}
            ns = sorted(n for n in set(names) if _mod(n) in have and ("." not in n or _mod(n) not in names))
            return (tpl.replace("{names}", shlex.join(ns)), ns) if ns else (None, [])
        ts = [t for t in sub[2] if os.path.exists(os.path.join(wt, t))] if sub and cmd == sub[0] else []
        if ts:
            return sub[1].replace("{tests}", shlex.join(ts)).replace("{names}", shlex.join(map(_stem, ts))), \
                [_stem(t) for t in ts]
        return cmd, None

    def run1(cmd, names=None):
        """(row, ran): gate `cmd` on the scratch tree as it stands, worded by form(); a passing run of the same line on
        this very tree is reused (ran False); row None when nothing named is left. `affected` lists the modules a
        narrowed run named (cli._cached_pass reads it); a red row keeps the names it fails, its output and last lines."""
        line, mods = form(cmd, names)
        key = (_git(wt, "rev-parse", "HEAD").stdout.strip(), line)
        if line is None or key in passed:
            return passed.get(key), False
        t0 = time.monotonic()
        code, out = c.run_command(wt, line, c.load_scaled(1800), priority=True, env=cpu_env(p))  # T-3614, T-3757
        row = {"cmd": cmd, "exit": code, "s": round(time.monotonic() - t0, 1), **({"affected": mods} if mods else {})}
        if code:
            row.update(failing=_red(out), tail=out.rstrip().splitlines()[-15:], out=out)
        else:
            passed[key] = row
        return row, True

    def build(start, lanes):
        """Reset the scratch tree to `start`, merge `lanes`; [(brief, sha after)] of those that merged."""
        _git(wt, "reset", "-q", "--hard", start)
        out = []
        for b, tip in lanes:
            sha = _merge_in(wt, b, tip, left)
            if sha:
                out.append((b, sha))
        return out

    def moved():
        return _git(main, "rev-parse", "HEAD").stdout.strip() != base or \
            _git(main, "symbolic-ref", "--short", "-q", "HEAD").stdout.strip() != branch

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

    def probe(sha, cmd, names):
        """T-3602: the red gate alone on the tree at `sha`, the suite on only its red names: its row (None: nothing
        named is in that tree, so nothing there can fail)."""
        _git(wt, "reset", "-q", "--hard", sha)
        row, ran = run1(cmd, names)
        probes[0] += ran
        return row

    def bisect(cmd, names):
        """T-3602: the lane (index + 1) that turns gate `cmd` red, by prefix: lo passes (0 = the base), hi fails. Each
        probe runs only that gate, the suite only on `names` (proven to fail alone on the whole set) or, with none, as
        the train runs it: seconds or the suite's part, where a whole gate run took minutes."""
        lo, hi = 0, len(merged)
        while hi - lo > 1:
            mid = (lo + hi) // 2
            row = probe(merged[mid - 1][1], cmd, names)
            lo, hi = (lo, mid) if row and row["exit"] else (mid, hi)
        if hi == 1:  # the first lane is blamed on the strength of a base nobody probed: probe it before blaming
            row = probe(base, cmd, names)
            if row and row["exit"]:
                raise c.PolicyError(f"the base {base[:9]} of {branch} is red; fix it before a train can blame a "
                                    f"lane: {_why_red(row)}")
        return hi

    def confirm(need):
        """T-3602: True when the gates in `need` pass on the tree as it stands: the suite on only its names (the red
        ones, and what the left-out lanes' diffs reach: a lane may have needed them), every other gate whole (a lane
        left out can turn any of them red). A probe's pass on this very tree counts."""
        c.wait_for_load("sampo lane train")
        results.clear()
        ran = False
        for cmd in (g for g in checks if g in need):
            row, fresh = run1(cmd, need[cmd])
            if row is None:  # nothing it named is left in the tree: the gate as the train runs it
                row, fresh = run1(cmd)
            ran |= fresh
            results.append(row)
            if row["exit"]:
                break
        runs[0] += ran
        return not results[-1]["exit"]

    try:
        merged = build(base, cands)
        sub = _affected_gate(p, wt, base, checks)
        if moved():  # a lane merged meanwhile: don't spend a gate run on a base that is gone
            raise c.PolicyError(f"{branch} moved since the train started (was {base[:9]}): nothing landed; run it again")
        why = _base_red(p, main, base, sub) if sub and merged else ""
        if why:  # T-3625: the base's affected tests first: a red base is known in minutes
            raise c.PolicyError(f"the base {base[:9]} of {branch} is red; fix it before a train can blame a lane: {why}")
        ok = not merged or gate()
        first = list(results)
        last = {r["cmd"]: r for r in results if not r["exit"]}  # each gate's newest passing row
        # T-3602: what each confirm reruns: every gate but the suite whole; the suite whole only if the red cut it off
        need = {g: None for g in checks if g != suite or g in checks[len(results):]}
        while not ok:  # T-3602: blame from the red run itself; bisect only what it can't pin on one lane
            red, g = results[-1], results[-1]["cmd"]
            names = red["failing"] if g == suite else []
            if names:  # a narrowed run sees them only if they fail alone (not by order or a flake): on this tree first
                row = probe(_git(wt, "rev-parse", "HEAD").stdout.strip(), g, names)
                names = names if row and row["exit"] and all(any(f == n or f.startswith(n + ".") or n.startswith(
                    f + ".") for f in row["failing"]) for n in names) else []
            need[g] = (need.get(g) or set()) | set(names) if names else None  # the suite's other tests passed here
            ids = [b.id for b, _ in merged]
            blamed = _blame(red["out"], red["failing"], ids, diff, reach)
            if not blamed:
                n, at = probes[0], bisect(g, names)
                only = "only " + c.joinmore(names, 4) if names else "the whole red gate"
                blamed = {ids[at - 1]: (f"bisection, {probes[0] - n} probe(s) of {only}", "")}
            texts = _texts(red["out"], red.get("failing") or [])
            for i in (i for i in ids if i in blamed):
                rule, k = blamed[i]
                why = "its merge turns the gates red: " + _why_red(red, rule, texts[k].rstrip().splitlines()[-15:]
                                                                   if k else None)
                left.append((i, why))
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
        tip =_git(wt, "rev-parse", "HEAD").stdout.strip()
        if moved():
            raise c.PolicyError(f"{branch} moved while the train ran (was {base[:9]}): nothing landed; run it again")
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
        part = next((r["affected"] for r in green if r.get("affected")), None)
        if part:  # T-3544: the full suite the gate skipped verifies each landed merge in the background, newest first
            for i, (b, sha) in enumerate(merged):
                pm_record(p, sha, b.id, main, spawn=i == len(merged) - 1)
        tree = _git(main, "rev-parse", f"{tip}^{{tree}}").stdout.strip()
        ct = c.code_tree(main, tree)
        c.log_event(p, "check_run", data={"tree": tree[:12], "code_tree": ct, "env": c.env_id(), "train": ids_in,
                                          "results": green})
        t3 = time.monotonic()
        mins = {"probe": round((t1 - t0) / 60, 1), "gate": round((t2 - t1) / 60, 1), "land": round((t3 - t2) / 60, 1)}
        return {"merged": ids_in, "left_out": left, "notes": notes, "kept": kept, "gate_runs": runs[0],
                "probes": probes[0], "tip": tip, "branch": branch, "affected": part, "minutes": mins, "probes_skipped": sum(
                    w.startswith(SKIPPED) for _, w in notes), "lanes_probed": len(rows)}
    finally:
        _git(main, "worktree", "remove", "--force", wt)
        shutil.rmtree(wt, ignore_errors=True)
        _git(main, "worktree", "prune")


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


def cmd(p, args, main):
    from sampo import cli
    ids = [x for x in (args.id or "").replace(",", " ").split() if x]  # one positional: T-1,T-2
    got = train(p, main, ids)
    lines = [f"Train: {len(got['merged'])} lane(s) landed on {got['branch']} ({got['tip'][:9]}) in "
             f"{got['gate_runs']} gate run(s)" + (f" and {got['probes']} probe(s) of only the failing tests"
                                                  if got["probes"] else "") + f": {', '.join(got['merged'])}"]
    lines += [f"  left out {i}: {w}" for i, w in got["left_out"]] + [f"  note {i}: {w}" for i, w in got["notes"]]
    m = got["minutes"]  # T-3596: where the time went; the serial closes after the train are not in it
    lines.append(f"Minutes: probe {m['probe']}, gate {m['gate']}, land {m['land']}; "
                 f"{got['probes_skipped']} of {got['lanes_probed']} probe(s) skipped (a held green run)")
    if got["affected"]:
        lines.append(f"The test gate ran {len(got['affected'])} affected test module(s), not the full suite "
                     "(FOREMAN_FULL_GATE=1 runs it); the full suite verifies the landed merges in the background.")
    lines.append("sampo check now reuses this pass while only CHANGELOG.md and the sync mirror change.")
    if got["merged"] and (sh := _ship(p, False, main)):  # T-3849: a train by hand ships too, on a grant already given
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
    red = _red(out) if rc else []
    line = ((out.strip().splitlines() or ["shipped"])[-1] if not rc else
            "refused: " + _why(out) + (f"; failing {c.joinmore(red, 8)}" if red else ""))
    data = {"head": head, "sha": sha, "seconds": round(t1 - t0, 1), "exit": rc, "start": round(t0, 1),
            "end": round(t1, 1), "red": red[:20]}
    try:
        c.log_event(p, "ship_done", task=tid, data=data)
        c.write_atomic(os.path.join(p.dir, "ship-last.json"), json.dumps({**data, "grant": tid, "seen": False,
                                                                           "line": c.fit(c.plain(line), 400)}))
    except Exception:  # noqa: BLE001 — a record that can't be written never undoes a ship
        pass
    return line, data


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
    try:
        rc, out, err = _sampo(["ship", "--task", grant.id], env=dict(os.environ, FOREMAN_SHIP_AT=head) if head else None)
    except Exception as e:  # noqa: BLE001 — a failed ship is said, never stops the land
        rc, out, err = 1, "", f"{type(e).__name__}: {e}"
    line = (out.strip().splitlines() or ["shipped"])[-1] if not rc else "refused: " + _why(err or out)
    _ship_end(p, grant.id, head, rc, out + err, t0, time.time())
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
    isn't running, is a zombie, or this host has no /proc."""
    try:
        with open(f"/proc/{int(pid)}/stat") as f:
            rest = f.read().rsplit(")", 1)[1].split()
        return 0 if rest[0] == "Z" else int(rest[19])
    except (OSError, IndexError, ValueError):
        return 0


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
    env = dict(os.environ, FOREMAN_SHIP_AT=head)
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
    gate = float((got.get("minutes") or {}).get("gate") or 0)
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
    """A refusal's own lines (its Next: hint aside) as one line."""
    lines = (ln.strip().removeprefix("fm: ").removeprefix("sampo: ").removeprefix("refused: ").removeprefix("- ") for ln in text.splitlines())
    return c.fit("; ".join(ln for ln in lines if ln and not ln.startswith("Next:")), 400) or "no reason given"


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


def land(p, main, batch=None):
    """T-3500: every finished lane waiting on a merge (lanes.awaiting_merge) through `train`, `batch` at a time (one
    that conflicts with the branch is parked before any criteria or gate run); each landed lane is removed and its task
    closed from its brief's Close fields through sampo task finish's own checks; the rest are parked (lane_stop and a
    Log line), which awaiting_merge leaves out until the lane has a new commit. Nothing is forced, reset or granted;
    a task already holding the publish grant ships after. One land at a time, never beside an active main task."""
    busy = c.active_brief_fast(p)
    if busy:
        raise c.PolicyError(f"{busy.id} is active in the main checkout, where sampo lane land focuses each lane's task to "
                            f"close it: finish {busy.id} (or sampo task set {busy.id} status=planned), then land")
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
    rerun would have reached. Such a lane trains after the fresh ones (a review-fix commit postdates a 15-minute run,
    and nobody is scheduled to rerun it); one that never recorded a run is not stale (no tests may link to its files)."""
    if _affected_state(b, sha, main)[0] == "stale":
        return (f"its newest affected run predates the lane tip {sha[:9]}: rerun sampo check --affected --evidence "
                f"{b.id} in its lane after the last commit")
    return None


def _code_stamp():
    """T-3973: path, mtime and size of every Sampo module on disk: a change means a train merged new code for this land."""
    d = os.path.join(c.PLUGIN_ROOT, "lib", "sampo")
    return tuple((f, s.st_mtime_ns, s.st_size) for f in sorted(os.listdir(d)) if f.endswith(".py")
                 for s in [os.stat(os.path.join(d, f))])


def _land(p, main, batch):
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
        for tid in ids:
            try:
                why, last = _close(p, main, tid)
            except Exception as e:  # noqa: BLE001 — one lane's close never costs the others theirs
                why, last = f"{type(e).__name__}: {e}", ""
            (hand if why else closed).append((tid, why or last))
            print(f"sampo lane land: left for a hand close {tid}: {c.fit(why, 400)}" if why else
                  f"sampo lane land: closed {tid}", file=sys.stderr, flush=True)  # T-3819: the reason, not hours later

    def sort(bs):  # a conflict never costs a criteria or gate run (an old git without merge-tree: no check)
        for b in sorted(bs, key=land_key):
            if why := _check_hold(b, tip(b), main):  # T-4286: its check is running, red or cut off at this tip: left out
                print(f"sampo lane land: {b.id}: {why}", file=sys.stderr, flush=True)
                continue
            by[b.id], tested[b.id] = b, tip(b)
            r = _git(main, "merge-tree", "--write-tree", "--name-only", "--no-messages", "HEAD", tested[b.id] or "HEAD")
            if r.returncode == 1:
                files = [f for f in r.stdout.splitlines()[1:] if f]
                parked.append((b.id, f"conflicts with {branch}" + (f": {c.joinmore(files, 8)}" if files else "")))
            else:
                ready.append(b.id)
                if why := _stale_affected(b, tip(b), main):  # T-3963: trains after the fresh ones, still lands
                    stale.add(b.id)
                    print(f"sampo lane land: {b.id}: {why}", file=sys.stderr, flush=True)
                if gaps := _close_gaps(b):  # T-3819: said before the trains, so the lines are added while they run
                    print(f"sampo lane land: {b.id} lands but its close will be refused: {'; '.join(gaps)} (add the "
                          "lines to its Close fields before its train ends)", file=sys.stderr, flush=True)
        ready.sort(key=lambda t: t in stale)  # stable: fresh lanes first, each group in land order
    left = list(landed)  # merged and open (a train by hand, a land cut short): closed after the trains (T-3884)
    waiting = set(lanes.awaiting_merge(p, briefs)) - set(landed)
    sort(x for x in briefs if x.id in waiting)
    seen = set(waiting) | set(landed)
    while True:
        jump = first_ids(p)  # T-3796: asked for meanwhile go next, a lane finished since the land began too
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
            for t in (t for t in jump if t not in front):
                print(f"sampo lane land: --first {t}: not a finished lane waiting on a merge here (merged, parked or "
                      "still building)", file=sys.stderr, flush=True)
        if not ready:
            break
        ids, ready = ready[:batch], ready[batch:]
        print(f"sampo lane land: train {trains + 1}: {', '.join(ids)}", file=sys.stderr, flush=True)
        tested.update({i: tip(by[i]) for i in ids})  # train() reads the same heads as it starts
        head0 = c._git(main, "rev-parse", "HEAD", timeout=10).strip()  # T-3851: the skills this train adds
        t_train = time.monotonic()
        try:
            got = train(p, main, ids)
        except c.PolicyError as e:
            if not hasattr(e, "left"):  # a red base, a moved branch: no later batch would land either
                stopped = str(e)  # T-4029: with the red gate's own lines, so nobody reruns it to learn them
                break
            got = {"merged": [], "left_out": e.left}
        except c.LockTimeout:
            stopped = "a sampo lane train is running: land again after it"
            break
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
        if got["merged"]:  # T-3849: reaches the remote per train, the grant kept for the next
            shipped = ship(False) or shipped  # T-4239: asked for, never waited on: the next train starts at once
        if ready and _code_stamp() != stamp:  # T-3973: the train changed Sampo itself: the rest runs on the new code
            restart = True
            first_ids(p, ready)  # the restarted land trains them in this order, not re-sorted
            break
    if not restart:  # a restart's leftovers close under the new code (it finds them merged and open again)
        close(left)
    for tid, why in parked:
        kind, arg = _park_kind(why)
        lanes.stop_mark(p, tid, kind, arg, log=f"land: parked: {c.fit(why, 300)}", tip=tested.get(tid, ""))
    last = ship(True) if landed and not restart else None  # the land's last ship spends the grant: a restart's successor does
    shipped = last if last and not last[1].startswith("nothing to ship") else shipped or last
    grant, said = shipped or (None, "")
    return {"landed": landed, "closed": [t for t, _ in closed], "hand": hand, "parked": parked, "trains": trains,
            "stopped": stopped, "restart": restart, "ship": said, "ship_grant": grant, "lines": closed,
            "train_times": times, "ship_asked": asked, "ship_failed": ship_failed}


def _close_gaps(b):
    """T-3819: what a close would refuse for a line the brief lacks (CHANGELOG, DOCS, NUMBERS, LESSON, why not caught)."""
    from types import SimpleNamespace

    from sampo import cli
    from sampo import lanes
    f = lanes.close_fields(b.section(lanes.CLOSE_FIELDS))
    if not f.get("changelog"):
        return ["no CHANGELOG line in its Close fields"]
    return cli._field_gaps(b, SimpleNamespace(docs=f.get("docs"), numbers=f.get("numbers"), lesson=f.get("lesson")))


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
    busy = c.active_brief_fast(p)
    if busy:
        return f"{busy.id} became active in the main checkout, so land closes nothing more", ""
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
        return "no close fields on its brief (its builder's report had no CHANGELOG line)", ""
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
    rc, out, err = _sampo(["task", "finish", tid, "--audit", f"{LAND_AUDIT} (lane tip {sha})", "--result", seen,
                           *[x for lens in sorted(set(fresh) - {"self"}) for x in ("--lens", f"{lens}: {seen}")],
                           "--commit", title])
    if rc:
        why = _why(err or out) + ("; its lane is kept" if path else "")
        nb = cli.need_brief(p, tid)  # after the refusal: it may have touched the brief, and the hash must match then
        nb.append_log(f"land: close refused: {c.fit(' '.join(why.split()), 400)} @ {_brief_hash(nb)} {sha} {_code_id()}")
        c.save_brief(p, nb)
        return why, ""
    done = cli.need_brief(p, tid)
    if path and done.meta.get("lane"):
        try:
            lanes.remove(p, done, main)
        except (c.PolicyError, cli.UsageError) as e:  # closed; only the folder is left: fm tidy offers it
            out += f"\nclosed; its lane stays: {str(e).splitlines()[0]}"
    return "", f"{title} — {(out.strip().splitlines() or ['closed'])[-1]}"


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
    with open(log, "a", encoding="utf-8") as out:  # ponytail: the log only appends; rotate it when it grows
        proc = subprocess.Popen([os.path.join(c.PLUGIN_ROOT, "bin", "fm"), "lane", "land"], cwd=main,
                                stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, start_new_session=True,
                                env={**os.environ, "FOREMAN_LAND_STAMP": "1"})  # T-4239: every log line starts with its time
    return proc.pid, log


# ---------------------------------------------------------------- the check accept starts (T-4286)

CHECK_TRIES = 2  # a check cut off at its deadline is started once more (its green classes are cached), then reported
CHECK_WALL_S = 4 * 3600  # a check process still running after this is killed and counts as cut off
_SCRUBBED = ("FM_STALL", c.RUN_ID_ENV, "FOREMAN_TEST_RUN_ID")  # env prefixes a lane's check never inherits: a test's override, a nested run's id (the legacy name too)
_CHECK_KEYS = ("check_state", "check_tip", "check_pid", "check_ticks", "check_log", "check_tries", "check_failed")


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
    return all(n in cmd for n in c.REPO_SHAPE_CLASSES)


def _run_passed(line):
    """T-4286: whether an affected run's evidence line is a passing one (a failed run's result starts with the cross)."""
    return not line.partition("` → ")[2].startswith("✗")


def _clean_run(b, sha, main):
    """T-4286, the one rule for what lets a lane skip its check: its newest affected run is at its tip (not before its
    own last commit), passed, and its command held the four repo-shape classes. A failing line (a red check records
    one too), or a run that did not hold the classes, is not it. Accept and the land's hold both read this."""
    state, line = _affected_state(b, sha, main)
    return state == "fresh" and _run_passed(line) and _held_classes(line)


def _check_alive(m):
    """T-4286: whether the check process a lane's meta names still runs (its pid and start time: a reused pid is not it)."""
    pid, ticks = m.get("check_pid"), m.get("check_ticks")
    return bool(pid) and (_ticks(pid) == int(ticks) if ticks else _ticks(pid) > 0)  # meta holds strings


def _restartable(m):
    """T-4286: a check that ended without a result, with tries left: sampo lane accept starts it again."""
    return m.get("check_state") == "running" and not _check_alive(m) and int(m.get("check_tries") or 1) < CHECK_TRIES


def _check_hold(b, sha, main):
    """T-4286: why a lane waits on its check (running, red, cut off) at tip `sha`, else None: a check that never ran, or
    one for an older tip, holds nothing, and a newer green run at the tip that held the repo-shape classes (_clean_run)
    clears a red, cut-off or dead check. One rule: the land leaves such a lane out and accept says the same."""
    m = b.meta
    st = m.get("check_state")
    if not st or m.get("check_tip") != sha or st == "green":
        return None
    if st == "running" and _check_alive(m):
        return f"held: check running (pid {m.get('check_pid')}); it joins the land when green"
    if _clean_run(b, sha, main):
        return None
    mine = f"sampo check --affected --hold --evidence {b.id}"
    if st == "running":
        return (f"held: its check ended without a result: sampo lane accept {b.id} starts it again" if _restartable(m) else
                f"held: its check ended without a result twice: run {mine} in the lane")
    if st == "red":
        return (f"held: check red: {m.get('check_failed') or 'no failing class recorded'}; left out, no retry until the "
                f"tip moves or a newer green run holds the repo-shape classes ({mine} in the lane)")
    return f"held: its check was cut off twice at its deadline: run {mine} in the lane"


def check_notes(briefs, main, ids=None):
    """T-4286: {task id: 'checking' | 'check red: CLASSES' | 'check cut off'} for the lanes whose own check holds them at
    their current tip (sampo lane list, sampo next): one git call per lane that has a check on record."""
    out = {}
    for b in briefs:
        if (ids is not None and b.id not in ids) or b.meta.get("check_state") in (None, "", "green"):
            continue
        why = _check_hold(b, _lane_tip(main, b), main)
        if why:
            st = b.meta["check_state"]
            out[b.id] = ("checking" if st == "running" and _check_alive(b.meta) else
                         f"check red: {b.meta.get('check_failed') or '?'}" if st == "red" else
                         "check cut off" if st == "cut_off" else
                         "check ended: accept again" if _restartable(b.meta) else "check ended: run it in the lane")
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
    land starts and trains it first, ('started', (pid, log)), or ('not started', None) in the tests and when paused."""
    first_ids(p, [tid])
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


def _start_check(p, b, main, tip, since, tries):
    """T-4286: start the lane's check (try number `tries`) and record it on the task; the started pid and log, or None.
    The claim comes first, inside the project lock: check_state=running with this process's own pid, only if the task's
    check is still what `b` showed (else _ClaimLost: another accept or the check's own retry got there), so two accepts
    at once start one check. The spawn follows; a start that does not happen (or raises) gives the claim back."""
    from sampo import cli
    seen, me = _check_seen(b.meta), os.getpid()

    def claim(x):
        if _check_seen(x.meta) != seen:
            raise _ClaimLost(x.meta)
        x.meta.update(check_state="running", check_tip=tip, check_pid=me, check_ticks=_ticks(me), check_tries=tries)
        for k in ("check_failed", "check_log"):
            x.meta.pop(k, None)
    cli.mutate(p, b.id, claim, "check", {"check_state": "running"})
    try:
        started = _spawn_check(p, b, main, tip=tip, since=since, tries=tries)
    except BaseException:
        _set_check(p, b.id, **dict.fromkeys(_CHECK_KEYS))
        raise
    if not started:
        _set_check(p, b.id, **dict.fromkeys(_CHECK_KEYS))
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
    proc = subprocess.Popen(argv, cwd=cwd, env={**_scrub_env(os.environ), "SAMPO_LANE_CHILD": "1"},
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, errors="replace", start_new_session=True)
    def stopped(signum, frame):  # sampo lane rm (stop_check): the check's own session ends with this process
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
        os._exit(128 + signum)
    try:
        before = signal.signal(signal.SIGTERM, stopped)
    except ValueError:  # not the main thread: no handler, the deadline still ends it
        before = None
    try:
        out, _ = proc.communicate(timeout=CHECK_WALL_S)
        return subprocess.CompletedProcess(argv, proc.returncode, out, ""), False
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
        out, _ = proc.communicate()
        return subprocess.CompletedProcess(argv, 124, out, ""), True
    finally:
        if before is not None:
            signal.signal(signal.SIGTERM, before)


@contextlib.contextmanager
def _check_turn(p, waits=None):
    """T-4416: one lane's accept check at a time in a project. Five started together (2026-11-11: 1,275 test modules for
    16 slots) were each still running after 15 to 25 minutes and a 59-module one was cut off at its run's deadline; in
    turn the work is the same and the first result comes after one run. An flock on the state's check/turn.lock, held
    by the check's own process around its run and released by the kernel however that process ends. Free: taken at
    once. Held: `waits()` once (the task is told), then the process blocks; no signal handler is set here, so the
    SIGTERM of sampo lane rm (stop_check) ends a waiting one. The land's gate and ship do not take it."""
    d = os.path.join(p.dir, "check")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "turn.lock"), "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if waits:
                waits()
            fcntl.flock(f, fcntl.LOCK_EX)
        yield


def _accept_check_child(a):
    """T-4286: the lane's check process: run the check in the lane, then act on its result. Green: the lane is queued
    first, the same call accept makes (a land not running is started). Red: left out, its failing classes (from the
    run's record, via the check's JSON) on the task and no retry until the tip moves. Cut off (the deadline, a kill, no
    result at all): started once more, then reported. A lane whose tip moved meanwhile is judged by none of it."""
    from sampo import cli
    p = c.Project(*a["project"])
    b = cli.need_brief(p, a["id"])
    main, tip, tries = a["main"], a["tip"], int(a.get("tries") or 1)
    r, timed_out = None, False
    with _check_turn(p, lambda: _set_check(p, b.id, "check waits its turn: another lane's check is running (one at a "
                                                    "time in a project, T-4416)")):
        if _lane_tip(main, cli.need_brief(p, b.id)) == tip:  # a tip that moved during the wait: no suite for nothing
            r, timed_out = _run_check(a["argv"], b.meta.get("lane") or main)
    got = _result_of(r) if r is not None else None
    b = cli.need_brief(p, b.id)
    if r is None or _lane_tip(main, b) != tip:
        _set_check(p, b.id, f"check on {tip[:9]} discarded: the lane tip moved; sampo lane accept {b.id} checks the new one",
                   **dict.fromkeys(_CHECK_KEYS))  # every key: a leftover tries or failed list would mislead the next one
        return 0
    if got is not None and r.returncode == 0 and not timed_out:
        _set_check(p, b.id, f"check green on {tip[:9]}", check_state="green", check_pid=None, check_ticks=None,
                   check_failed=None, accepted_at=round(time.time(), 1))
        _queue(p, main, b.id)
        return 0
    if got is not None and not got.get("cut_off") and not timed_out:
        failed = " ".join(str(x) for x in got.get("failed") or [])[:500]
        _set_check(p, b.id, f"its lane's check is red: {failed or 'no failing class recorded'} (tip {tip[:9]}; no retry "
                            "until the tip moves)", check_state="red", check_failed=failed, check_pid=None, check_ticks=None)
        return 1
    try:
        if tries < CHECK_TRIES and _start_check(p, b, main, tip, a.get("since"), tries + 1):
            return 1
    except _ClaimLost:  # another process started this lane's check meanwhile: it reports
        return 1
    if got is None and not timed_out:  # T-4410: it ended on its own (a usage error, a crash): nothing was cut off
        last = next((x.strip() for x in reversed((r.stdout or "").splitlines()) if x.strip()), "no output")
        what = f"check gave no result {tries}x on {tip[:9]} (exit {r.returncode}): {c.redact(last)[:200]}"
    else:
        what = f"check cut off {tries}x at its deadline on {tip[:9]}"
    _set_check(p, b.id, f"{what}: run sampo check --affected --hold --evidence {b.id} in the lane",
               check_state="cut_off", check_pid=None, check_ticks=None)
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
            f"({', '.join(c.REPO_SHAPE_CLASSES)}): --no-check would queue it without them. Run "
            f"`sampo check --affected --hold --evidence {b.id}` in the lane (or drop --no-check: accept starts that check)")
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
    red or cut off at this tip (said as `held: …`, the land's own words, no retry), else started detached (a dead one
    once more, claimed first so two accepts start one); the lane queues when it is green."""
    from sampo import cli
    m = b.meta
    st = m.get("check_state") if m.get("check_tip") == tip else None
    tries = int(m.get("check_tries") or 1)

    def running(meta):
        lines.append(f"  {b.id}: check is running (pid {meta.get('check_pid')}, log {meta.get('check_log')}); the lane "
                     "joins the land when it is green")
        return cli.out(args, {"id": b.id, "queued": False, "check": "running", "pid": meta.get("check_pid")}, "\n".join(lines))
    if st == "running" and _check_alive(m):
        return running(m)
    if why := _check_hold(b, tip, main):
        if not _restartable(m):
            lines.append(f"  {b.id}: {why}")
            return cli.out(args, {"id": b.id, "queued": False, "check": st, "held": why,
                                  "failed": m.get("check_failed") or ""}, "\n".join(lines))
    since = None
    if len(_git(main, "rev-list", "--parents", "-n", "1", tip).stdout.split()) > 2:  # T-4355: a merge tip has no diff of its own
        since = m.get("base") or None
    try:
        started = _start_check(p, b, main, tip, since, tries + 1 if st == "running" else 1)
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

