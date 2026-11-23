"""T-2352: the self-change gate at fm lane merge. A branch that changes Sampo's own plugin/ merges only when it keeps
what the base has: the guard blocks no less (its labelled cases and allow-corpus, the union of base's and branch's so a
deleted case can't hide, plus recent real commands, judged with no grants by each tree's own guard), no hook gets
slower (the hooks of each tree on the base's bench), and the always-on footprint doesn't grow with no cut to pay for it
(T-2358's rule). The harness is always the base's (fmreplay.py, bench_hooks.py and its payloads), so a branch can't
rewrite the judge; trees are built from git's blobs, not git archive (whose .gitattributes a branch controls).
A refusal names the numbers. The user's yes to `fm ask ID core` given after a refusal of the same commit passes it;
never a decision, never a yes for an earlier commit. The user's standing core yes passes a hold made only of
footprint, verb-count or hook-speed rows (T-3219), never one where the guard blocks less, a case was removed or the gate
couldn't run. A gate that can't run refuses with the reason, and the per-commit yes passes that too."""
import concurrent.futures
import contextlib
import hashlib
import hmac
import json
import math
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time

from sampo import core as c

CASES = ("plugin/tests/fixtures/guard_cases.jsonl", "plugin/tests/fixtures/guard_allow.jsonl")
HARNESS = ("lib/sampo/replay.py", "tests/bench_hooks.py", "tests/fixtures/hook_payloads.json")  # base's copies judge
RISE = 0.15  # a hook's p95 rise that refuses (T-2358's latency band)
FLOOR_MS = 5.0  # under this, a rise is jitter whatever its share
RUNS = 10  # bench runs per tree per pair
PAIRS = 3  # T-2992: base and branch benched in turn this many times
HOLD = PAIRS  # T-3131: a rise must show in every pair (2 of 3 across 16 hooks held identical trees by chance)
SPREAD_CAP = 0.30  # T-3091: a hook's own bench-to-bench spread widens its bar by at most this (RISE + 0.30: the bar reaches 1.45)
MACHINE_MIN = 5  # T-3091: hooks needed for a median ratio that stands for the machine
TAIL_RISE, TAIL_FLOOR_MS = 0.5, 25.0  # T-3131 review: a p95 rise this big, in every pair, is a tail slowdown, not noise
WALL_RISE, WALL_FLOOR_MS = 1.0, 100.0  # T-2363: on a loaded bench, a wall-time rise this big is a hook that waits
CORPUS = 2000  # recent real commands replayed, newest first (fmreplay.corpus)
_RUNNER = ("import json, os, sys\nsys.path.insert(0, sys.argv[1])\nfrom sampo import replay as r\n"
           "cmds = [(x, r.resolve_cwd(y) if y else y) for x, y in json.load(sys.stdin)]\nv = r.verdicts(cmds)\n"
           "print(json.dumps([v.get(r._key(x, y or os.path.expanduser('~'))) for x, y in cmds]))\n")
LOAD_BAND = 1.5  # T-3176: a cached base bench is reused only when the load per CPU now is within this factor of its own
CACHE_S = 86400  # T-3176: the base's bench and replay, kept per base tree hash, are good for a day
REPLAY_CHUNK = 250  # T-6154: guard replay commands per process (a chunk is one process with a state folder of its own)
REPLAY_JOBS = 4  # T-6154: chunks replayed at once (the machine has 16 cores, shared with up to ~100 agents)


def _git(main, *a, text=False, input=None):
    return subprocess.run(["git", "-C", main, "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", *a],
                          capture_output=True, text=text, input=input, timeout=180)


def _tree(main, sha, dest):
    """sha's committed plugin/ written under dest from git's blobs (links and submodules skipped)."""
    ls = _git(main, "ls-tree", "-r", "-z", "--full-tree", sha, "--", "plugin")
    if ls.returncode:
        raise RuntimeError(f"git ls-tree {sha[:12]}: {ls.stderr.decode(errors='replace').strip()[:160]}")
    files = []
    for rec in filter(None, ls.stdout.split(b"\0")):
        meta, path = rec.split(b"\t", 1)
        mode, kind, blob = meta.split()
        if kind == b"blob" and mode != b"120000":
            files.append((mode, blob, os.path.normpath(path.decode("utf-8", "surrogateescape"))))
    cat = _git(main, "cat-file", "--batch", input=b"".join(blob + b"\n" for _, blob, _ in files))
    out, i, root = cat.stdout, 0, os.path.realpath(dest)
    for mode, blob, rel in files:
        nl = out.index(b"\n", i)
        size = int(out[i:nl].split()[2])
        data, i = out[nl + 1:nl + 1 + size], nl + 2 + size
        dst = os.path.join(root, rel)
        if rel.startswith("..") or os.path.isabs(rel) or not dst.startswith(root + os.sep):
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "wb") as f:
            f.write(data)
        os.chmod(dst, 0o755 if mode == b"100755" else 0o644)
    return os.path.join(root, "plugin")


def _landed(main, head, sha):
    """T-3647: what a train lands: sha when it already holds head, else the tree of head merged with sha. A conflict (or an
    old git) falls back to sha itself, the tip; conflicts are reported elsewhere. A tree id works wherever _tree/_cases
    take a sha."""
    if _git(main, "merge-base", "--is-ancestor", head, sha).returncode == 0:
        return sha
    r = _git(main, "merge-tree", "--write-tree", head, sha, text=True)
    first = r.stdout.split("\n", 1)[0].strip()
    return first if r.returncode == 0 and len(first) in (40, 64) else sha


def _cases(main, sha):
    """{(cmd, cwd): id} of sha's labelled guard cases and allow-corpus."""
    out = {}
    for f in CASES:
        r = _git(main, "show", f"{sha}:{f}", text=True)
        for ln in (r.stdout if r.returncode == 0 else "").splitlines():
            try:
                k = json.loads(ln)
            except ValueError:
                continue
            if isinstance(k, dict) and isinstance(k.get("cmd"), str):
                out[(k["cmd"], str(k.get("cwd") or ""))] = str(k.get("id") or "")
    return out


def _corpus():
    """Recent real commands from Claude Code's session logs (fmreplay.corpus), so coverage isn't only the fixtures."""
    from sampo import replay
    return [(cmd, cwd or "") for cmd, cwd in replay.corpus(cap=CORPUS)]


def _env(state):
    """What the tree's code sees: the real home and Sampo home (so core and home rules judge real paths), an empty
    state (no grants, trust or standing yes), and nothing else of this session's environment (no tokens)."""
    keep = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "FOREMAN_HOME")
    return dict({k: os.environ[k] for k in keep if k in os.environ}, FOREMAN_STATE=state)


def _run_chunk(plugin, cmds):
    """One replay process's verdicts for cmds, in a state folder of its own; None when that process timed out."""
    # T-2363: a hook the bench started may still be writing its state as this returns; a leftover file is no reason
    # for the gate to fail (a gate that can't run refuses)
    with tempfile.TemporaryDirectory(prefix="fm-gate-state-", ignore_cleanup_errors=True) as state:
        try:
            r = subprocess.run([sys.executable, "-I", "-c", _RUNNER, os.path.join(plugin, "lib")], input=json.dumps(cmds),
                               capture_output=True, text=True, timeout=600, env=_env(state))
        except subprocess.TimeoutExpired:
            return None
    got = json.loads(r.stdout or "null")
    if not isinstance(got, list) or len(got) != len(cmds):
        raise RuntimeError(f"guard replay: {(r.stderr or r.stdout).strip()[-160:]}")
    return got


def _verdicts(plugin, cmds):
    """Each command's verdict from this tree's own guard, judged by the base's fmreplay. T-6154: the commands go in chunks
    of REPLAY_CHUNK, REPLAY_JOBS processes at once, so one long replay on a loaded machine can't time the gate out; a
    chunk that times out is run once more, alone, before the gate says it couldn't run."""
    chunks = [cmds[i:i + REPLAY_CHUNK] for i in range(0, len(cmds), REPLAY_CHUNK)]
    if not chunks:
        return []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(REPLAY_JOBS, len(chunks)))) as pool:
        parts = list(pool.map(lambda ch: _run_chunk(plugin, ch), chunks))
    out = []
    for ch, got in zip(chunks, parts):  # the pool is done: a retry has the machine to itself
        if got is None:
            got = _run_chunk(plugin, ch)
            if got is None:
                raise RuntimeError(f"guard replay: a chunk of {len(ch)} commands timed out twice")
        out.extend(got)
    return out


def _bench(plugin):
    """({fixture: timings}, {event: injected chars}) from the base's bench over this tree's hooks."""
    # T-2363: a hook the bench started may still be writing its state as this returns; a leftover file is no reason
    # for the gate to fail (a gate that can't run refuses)
    with tempfile.TemporaryDirectory(prefix="fm-gate-state-", ignore_cleanup_errors=True) as state:
        r = subprocess.run([sys.executable, "-I", os.path.join(plugin, "tests", "bench_hooks.py"), "--runs", str(RUNS),
                            "--json", "--inject"], cwd=plugin, capture_output=True, text=True, timeout=600,
                           env=_env(state))
    got = json.loads(r.stdout or "null")  # exit 1 is a hook over budget; the JSON still stands
    if not isinstance(got, dict):
        raise RuntimeError(f"hook bench: {r.stderr.strip()[-160:]}")
    return got.get("bench") or {}, got.get("injected") or {}


def _key(base):
    """T-3176: what the base's cached half is true for: its content (not tests/ or __pycache__), the bench script and
    payloads, the bench settings and this machine."""
    h = hashlib.sha256(json.dumps([RUNS, PAIRS, platform.node(), os.cpu_count()]).encode())
    for dp, dns, fns in os.walk(base):  # in-place dns pruning and sorting keeps the order stable
        dns[:] = sorted(d for d in dns if d not in ("tests", "__pycache__"))
        for fn in sorted(fns):
            h.update(os.path.relpath(os.path.join(dp, fn), base).encode())
            with open(os.path.join(dp, fn), "rb") as f:
                h.update(f.read())
    for rel in ("tests/bench_hooks.py", "tests/fixtures/hook_payloads.json"):
        if os.path.isfile(os.path.join(base, rel)):
            with open(os.path.join(base, rel), "rb") as f:
                h.update(f.read())
    return h.hexdigest()


def _keyfile():
    """The MAC key's path: the account's own ~/.config/sampo/selfgate.key (password database home, as models.secrets_path
    finds it), never SAMPO_SECRETS or FOREMAN_SECRETS: a run pointed at a key the agent wrote must not verify with it (T-3192)."""
    import pwd
    try:
        home = pwd.getpwuid(os.getuid()).pw_dir
    except KeyError:  # a uid with no account entry (some containers): $HOME is all there is
        home = os.path.expanduser("~")
    return os.path.join(home, ".config", "sampo", "selfgate.key")


def _secret():
    """The 32-byte MAC key, created 0600 (O_EXCL, O_NOFOLLOW) when missing; None when the file is a symlink, someone
    else's, group/world-accessible or unreadable. An agent that can write the state folder can't forge a cache without it."""
    path = _keyfile()
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(os.urandom(32))
        except FileExistsError:
            pass
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as f:
            st = os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077 or st.st_uid != os.getuid():
                return None
            key = f.read()
        return key if len(key) == 32 else None
    except OSError:
        return None


def _mac(secret, body):
    return hmac.new(secret, json.dumps(body, sort_keys=True, separators=(",", ":")).encode(), hashlib.sha256).hexdigest()


def _load_pc():
    """1-minute load per CPU, or None when unreadable."""
    got = c.host_load()
    return got[0] / max(got[1], 1) if got else None


def _near(a, b):
    """T-3176: two loads per CPU within a factor of LOAD_BAND of each other (a floor keeps an idle host comparable)."""
    if a is None or b is None:
        return False
    a, b = max(a, 0.05), max(b, 0.05)
    return max(a, b) <= min(a, b) * LOAD_BAND


CACHE_FILE = "selfgate-base.json"  # the lane train's; T-4805: the push hold keeps its own (release.py) so it never overwrites this


def _load(p, key, name=CACHE_FILE):
    """The saved base half when its MAC verifies under the user's key, it is for this key and under a day old, else {}.
    The bench is dropped (the replay kept) when the host's load now is not within LOAD_BAND of the load it ran at."""
    path = os.path.join(p.dir, name)
    if not os.path.exists(path):
        return {}
    try:
        secret = _secret()
        with open(path, encoding="utf-8") as f:
            got = json.load(f)
        mac = got.pop("mac", None)
        if secret is None or not isinstance(mac, str) or not hmac.compare_digest(mac, _mac(secret, got)):
            raise ValueError("no valid MAC")
        if got["key"] == key and 0 <= time.time() - got["at"] < CACHE_S:
            out = {"at": got["at"], "bench": got.get("bench"), "verdicts": got.get("verdicts") or {}}
            if _near(got.get("load"), _load_pc()):
                out["load"] = got["load"]
            else:
                out["bench"] = None
            return out
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        c.log_event(p, "self_gate_cache_ignored", data={"why": c.fit(str(e), 100)})
    return {}


def _save(p, key, cache, name=CACHE_FILE):
    cache.setdefault("at", time.time())
    cache.setdefault("load", _load_pc())
    path = os.path.join(p.dir, name)
    secret = _secret()
    if secret is None:
        return
    body = dict(cache, key=key)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"  # T-6031: the push hold's workers save one file at once
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(dict(body, mac=_mac(secret, body)), f)
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)


def _base_verdicts(base, cmds, cache):
    """The base's verdict for each command; only those not in the cache are replayed."""
    seen = cache.setdefault("verdicts", {})
    miss = [x for x in cmds if json.dumps(x) not in seen]
    if miss:
        seen.update(zip(map(json.dumps, miss), _verdicts(base, miss)))
    return [seen[json.dumps(x)] for x in cmds]


class _Quiet:
    """T-6031: the push hold judges several commits at once in this process, and a hook bench's timings are skewed by
    another worker's replay on the same CPUs. replay() (many at once) and timing() (one, alone) exclude each other; a
    waiting bench holds new replays back so it isn't starved. A wait past WAIT_S is a RuntimeError, which the gate turns
    into its own "couldn't run" hold. Only threads of this process meet here; the machine-wide bench lock covers the rest."""
    WAIT_S = 3600

    def __init__(self):
        self.cv, self.replays, self.waiting, self.bench = threading.Condition(), 0, 0, False

    def _wait(self, ready):
        if not self.cv.wait_for(ready, timeout=self.WAIT_S):
            raise RuntimeError("another judgement in this hold kept the machine busy for an hour")

    @contextlib.contextmanager
    def replay(self):
        with self.cv:
            self._wait(lambda: not self.bench and not self.waiting)
            self.replays += 1
        try:
            yield
        finally:
            with self.cv:
                self.replays -= 1
                self.cv.notify_all()

    @contextlib.contextmanager
    def timing(self):
        with self.cv:
            self.waiting += 1
            try:
                self._wait(lambda: not self.bench and not self.replays)
            finally:
                self.waiting -= 1
                self.cv.notify_all()
            self.bench = True
        try:
            yield
        finally:
            with self.cv:
                self.bench = False
                self.cv.notify_all()


QUIET = _Quiet()


def _paired(base, new, cache=None, excused=None):
    """T-2992: (held rows, base's injected chars, branch's) from PAIRS benches of base then branch in turn, so load that
    comes and goes lands on both; a hook's rise holds when it shows in HOLD pairs (a one-off spike doesn't).
    T-3176: with a cache the base's PAIRS benches are reused (or kept after a fresh run); the branch always runs.
    T-3091: `excused`, a list, gets {hook, raw, divisor, bar} for each hook a plain +RISE would have held that its spread
    or the machine's share passed."""
    old = (cache or {}).get("bench")
    # T-3119: a timing bench never runs beside another (parallel lane checks would skew each other's p95); every
    # other check runs at once. ponytail: one machine-wide lock, any process; a stuck holder times out after an hour
    try:
        with c.lock(os.path.join(c.state_dir(), "bench"), timeout=3600), QUIET.timing():
            if isinstance(old, list) and len(old) == PAIRS:
                runs = [(a, _bench(new)) for a in old]
            else:
                runs = [(_bench(base), _bench(new)) for _ in range(PAIRS)]
                if cache is not None:
                    cache["bench"], cache["load"] = [a for a, _ in runs], _load_pc()
    except c.LockTimeout as e:  # the gate's own refusal ("couldn't run"), not a crash
        raise RuntimeError(f"another hook bench held the bench lock for an hour: {e}") from None
    # T-3091: the base's own PAIRS benches are the base against itself (in the same window unless the base came from the
    # cache): each hook's CPU spread over them widens that hook's bar, and the median ratio across all hooks is the
    # machine's share, divided out only when EVERY base bench ran loaded (one loaded pair would drop its row and with it
    # the hold) and never past what the base itself moved (see _machine)
    bases = [a[0] for a, _ in runs]
    spread, wall = _spread(bases), _wall_spread(bases)
    steady = all(_loaded(b) for b in bases)
    notes = [{} for _ in runs]
    seen = [_slower(a[0], z[0], spread=spread, machine=_machine(a[0], z[0], spread) if steady else 1.0, wall=wall, note=n)
            for (a, z), n in zip(runs, notes)]
    rows = {k: next(s[k] for s in seen if k in s) for k in {k for s in seen for k in s}
            if sum(k in s for s in seen) >= HOLD}
    if excused is not None:  # held at plain RISE in HOLD pairs, passed by its spread or the divisor
        for k in sorted({k for n in notes for k in n}):
            got = [n[k] for n in notes if k in n]
            if len(got) >= HOLD and k not in rows:
                excused.append({"hook": k, "raw": round(max(g["raw"] for g in got), 4),
                                "divisor": max(g["divisor"] for g in got), "bar": round(max(g["bar"] for g in got), 4)})
    return rows, runs[0][0][1], runs[0][1][1]


def _num(x):
    """x when it is a finite real number (a NaN or inf reads as missing: it must never silence a comparison)."""
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) else None


def _ms(v, loaded):
    return _num(v.get("cpu_p95") if loaded else v.get("p95"))


def _loaded(*benches):
    return any((_num(v.get("load")) or 0) > 1 for b in benches for v in b.values() if isinstance(v, dict))


def _basis(v, loaded):
    """The number a rise is judged on: the median CPU time when the bench has it, else the p95 (CPU p95 if loaded)."""
    return _num(v.get("cpu_p50")) or _ms(v, loaded)


def _range(xs):
    xs = [x for x in xs if x and x > 0]
    return (max(xs) - min(xs)) / min(xs) if len(xs) >= 2 else None


def _spread(benches):
    """T-3091: {hook: (max - min) / min of its judged CPU basis across the base's own benches}, capped at SPREAD_CAP: how
    far the same code moves from one bench to the next in this window. Only hooks with two readings. With a cached base
    the benches are the base's from an earlier window, not the branch's own."""
    loaded, out = _loaded(*benches), {}
    for k in set().union(*benches) if benches else ():
        got = _range([_basis(b[k], loaded) for b in benches if isinstance(b.get(k), dict)])
        if got is not None and math.isfinite(got):
            out[k] = min(got, SPREAD_CAP)
    return out


def _wall_spread(benches):
    """T-3091: the same for the wall median, for the quiet bench's wall-wait row only (it never widens the CPU bar)."""
    out = {}
    for k in set().union(*benches) if benches else ():
        got = _range([_num(b[k].get("p50")) for b in benches if isinstance(b.get(k), dict)])
        if got is not None and math.isfinite(got):
            out[k] = min(got, SPREAD_CAP)
    return out


def _machine(base, branch, spread=None):
    """T-3091: the median over hooks of branch/base on the judged basis, when the base ran loaded and at least
    MACHINE_MIN hooks have both numbers: a rise every hook shares is taken as the machine, not a change in one hook.
    It is never below 1.0 and never above 1 + the median of `spread` (what the base itself moved from bench to bench):
    a bigger shared rise is the branch's, however loaded the bench. 1.0 on a quiet base, with too few hooks, or with no
    spread measured (nothing is excused without evidence)."""
    if not _loaded(base):
        return 1.0
    ratios = sorted(r for k, v in branch.items() if isinstance(v, dict) and isinstance(base.get(k), dict)
                    for x1, x0 in [(_basis(v, True), _basis(base[k], True))] if x1 and x0
                    for r in [x1 / x0] if math.isfinite(r))
    moved = sorted(s for s in (spread or {}).values() if _num(s) is not None)
    if len(ratios) < MACHINE_MIN or not moved:
        return 1.0
    mid, mid2 = len(ratios) // 2, len(moved) // 2
    got = ratios[mid] if len(ratios) % 2 else (ratios[mid - 1] + ratios[mid]) / 2
    cap = 1 + (moved[mid2] if len(moved) % 2 else (moved[mid2 - 1] + moved[mid2]) / 2)
    return round(max(1.0, min(got, cap)), 4)


def _slower(base, branch, spread=None, machine=1.0, wall=None, note=None):
    """Hooks whose p95 rose past RISE (and FLOOR_MS) or past the base's budget, on one basis (CPU time if either run
    was loaded). A new fixture takes the base's tightest budget; the branch's own budgets are never read.
    T-3091: a median rise must also pass that hook's CPU `spread` (its movement across the base's own benches) and is
    judged after dividing the branch's median by `machine` (see _machine); the quiet bench's wall-wait row uses the
    `wall` spread; the budget, tail and loaded-wall rows read raw numbers. `note`, when given, gets {hook: {raw, divisor,
    bar, held}} for every hook whose raw rise passes plain RISE, so the caller can say who an allowance excused."""
    machine = machine if _num(machine) and machine > 0 else 1.0
    loaded = _loaded(base, branch)
    tight = min((v["budget"] for v in base.values() if isinstance(v.get("budget"), (int, float))), default=None)
    rows = {}
    for k, v in branch.items():
        bk = base.get(k) or {}
        p1, p0 = _ms(v, loaded), _ms(bk, loaded)
        tag = "CPU p95" if loaded else "p95"
        # T-3131: a rise is judged on the median CPU time when both benches have it (the p95 of 10 runs is the slowest
        # run: +78% between identical trees); the budget stays a p95 budget, and a tail-only rise has its own loose bar
        both = all(isinstance(x.get("cpu_p50"), (int, float)) and not isinstance(x.get("cpu_p50"), bool) for x in (v, bk))
        b1, b0 = (v["cpu_p50"], bk["cpu_p50"]) if both else (p1, p0)
        budget = bk.get("budget", tight)
        if b1 is None:
            continue
        bar = 1 + RISE + (spread or {}).get(k, 0.0)  # T-3091: past RISE and this hook's own CPU spread
        wbar = 1 + RISE + (wall or {}).get(k, 0.0)
        raw1 = b1
        b1 = b1 / machine
        over = isinstance(budget, (int, float)) and p1 is not None and p1 > budget and not (p0 is not None and p0 > budget)
        rise = b0 is not None and b0 > 0 and b1 > b0 * bar and b1 - b0 >= FLOOR_MS
        if note is not None and b0 is not None and b0 > 0 and raw1 > b0 * (1 + RISE) and raw1 - b0 >= FLOOR_MS:
            note[k] = {"hook": k, "raw": raw1 / b0, "divisor": machine, "bar": bar, "held": bool(rise)}
        tail = both and p0 and p1 is not None and p1 > p0 * (1 + TAIL_RISE) and p1 - p0 >= TAIL_FLOOR_MS
        q = "p50" if bk.get("p50") and v.get("p50") else "p95"  # T-2605: load spikes inflate p95
        w1, w0 = v.get(q), bk.get(q)
        walls = isinstance(w0, (int, float)) and isinstance(w1, (int, float)) and w0 > 0
        # CPU time misses a hook that sleeps or waits on I/O: loaded, a doubling past 100 ms of wall isn't load noise;
        # quiet (T-3131 review), the wall median rising past RISE and FLOOR_MS is a wait
        waits = walls and ((loaded and w1 >= w0 * (1 + WALL_RISE) and w1 - w0 >= WALL_FLOOR_MS) or
                           (not loaded and both and q == "p50" and w1 > w0 * wbar and w1 - w0 >= FLOOR_MS))
        if over:
            rows[k] = f"hook {k} {tag} {p0} -> {p1} ms (past its {budget} ms budget)"
        elif rise:
            rows[k] = (f"hook {k} {'median CPU' if both else tag} {b0} -> {raw1} ms "
                       f"(+{(raw1 - b0) * 100 / b0:.0f}% raw, bar +{(bar - 1) * 100:.0f}%, machine x{machine})")
        elif waits:
            rows[k] = f"hook {k} {q} {w0} -> {w1} ms wall (+{(w1 - w0) * 100 / w0:.0f}%; its CPU time didn't rise: it waits)"
        elif tail:
            rows[k] = f"hook {k} {tag} {p0} -> {p1} ms (+{(p1 - p0) * 100 / p0:.0f}%: a slowdown in the tail alone)"
    return rows


def _footprint(plugin, sizes):
    from sampo import doctor
    return dict(doctor.footprint(c.rules_path(plugin), plugin),
                **doctor.measured(None, sizes))


def _verbs(plugin):
    """T-2363: the fm verbs a tree's fmcli registers (its add("name", …) lines), as fmgenesis._lean counts them."""
    for rel in (("lib", "sampo", "cli.py"), ("lib", "fmcli.py")):  # T-2748: a tree from before the move has fmcli.py
        try:
            with open(os.path.join(plugin, *rel), encoding="utf-8", errors="replace") as f:
                return sorted(set(re.findall(r'(?<![.\w])add\("([a-z][\w-]*)"', f.read())))
        except OSError:
            continue
    return []


def _hook_modules(plugin):
    """T-3139: {fixture: [plugin files that hook's process imported]} from the base's bench over this tree's hooks."""
    with tempfile.TemporaryDirectory(prefix="fm-gate-state-", ignore_cleanup_errors=True) as state:
        r = subprocess.run([sys.executable, "-I", os.path.join(plugin, "tests", "bench_hooks.py"), "--modules"],
                           cwd=plugin, capture_output=True, text=True, timeout=600, env=_env(state))
    got = json.loads(r.stdout or "null")
    if not isinstance(got, dict):
        raise RuntimeError(f"hook modules: {r.stderr.strip()[-160:]}")
    return {k: sorted(v) for k, v in got.items() if isinstance(v, list) and v and all(isinstance(x, str) for x in v)}


def _changed(main, a, b):
    r = _git(main, "diff", "--name-only", "--no-renames", "-z", a, b, "--", "plugin", "CHANGELOG.md")
    if r.returncode:
        raise RuntimeError(f"git diff {a[:12]} {b[:12]}")
    return [f.decode("utf-8", "replace") for f in r.stdout.split(b"\0") if f]


def needs_timing(files, hmap):
    """T-3139: whether the hook timing bench must run for these changed files. Yes for an empty map (unknown), a hook
    entry, hooks.json, a lib module some hook process imported, and any file a hook might read as data; a page script
    under plugin/web or a lib module no hook imported can't slow a hook."""
    mapped = {f for v in hmap.values() for f in v}
    if not mapped:
        return True
    for f in files:
        if not _benched(f) or f.startswith("plugin/web/"):
            continue
        # only plugin/lib/sampo/ is imported absolutely; a top-level lib/*.py can shadow the stdlib (lib/ leads sys.path)
        if f.startswith("plugin/lib/sampo/") and f.endswith(".py") and f not in mapped:
            continue
        return True
    return False


def _refresh(files, hmap):
    """T-3139: whether the cached map is stale: a timing-relevant file changed, or the bench that builds it did.
    CHANGELOG.md is a timing input (SessionStart shows it) but no hook imports it."""
    return any(f.startswith("plugin/tests/fixtures/") or f == "plugin/tests/bench_hooks.py" for f in files) or \
        needs_timing([f for f in files if f != "CHANGELOG.md"], hmap)


def hook_map(p, main, head, base):
    """T-3139: {fixture: [files]}, the union over runs of what each hook process imported, in the project's
    hook-modules.json keyed by the base commit it was last true for. Kept when the commits between touched no hook entry,
    hooks.json or mapped module; otherwise the base is run again and merged in. {} (time as today) when it can't run."""
    path = os.path.join(p.dir, "hook-modules.json")
    try:
        with open(path, encoding="utf-8") as f:
            prev = json.load(f)
        old = {k: v for k, v in prev["map"].items() if isinstance(v, list)}
        if old and prev["sha"] == head:
            return old
        if old and not _refresh(_changed(main, prev["sha"], head), old):
            got = old
        else:
            got = None
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError):
        old, got = {}, None
    if got is None:
        try:
            fresh = _hook_modules(base)
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
            return {}
        got = {k: sorted(set(old.get(k, [])) | set(fresh.get(k, []))) for k in set(old) | set(fresh)}
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"  # T-6031: concurrent holds each write their own
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"sha": head, "map": got}, f)
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            os.remove(tmp)
    return got


def cached_hook_map(p, main, base):
    """T-5935: the saved hook map when it is still true at `base` (its commit is `base`, or nothing between them touched a
    hook entry, hooks.json, a mapped module or the bench), else None: a live train's front gate reads it without a bench
    run (hook_map re-runs the base's hooks to refresh a stale one) and times the hooks whenever it is None."""
    try:
        with open(os.path.join(p.dir, "hook-modules.json"), encoding="utf-8") as f:
            prev = json.load(f)
        old = {k: v for k, v in prev["map"].items() if isinstance(v, list)}
        if old and (prev["sha"] == base or not _refresh(_changed(main, prev["sha"], base), old)):
            return old
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError, subprocess.SubprocessError):
        pass
    return None


QUALITY_ROWS = ("footprint ", "verbs ", "hook ")  # T-3219: rows the standing yes may pass (never "guard…")


def approved(p, b, sha):
    """The user's yes to `fm ask ID core` on this task, given after its last self-change refusal of this commit. T-4805: per
    commit, so one yes covers every commit refused before it (the push hold can refuse several at once); a commit refused
    again after the yes needs another."""
    pending, covered = set(), set()
    for e in c.ledger_events(p, ("self_change_refused", "approval_granted"), since=str(b.meta.get("created") or "")):
        if e.get("task") != b.id:
            continue
        if e.get("event") == "self_change_refused":
            s = (e.get("data") or {}).get("sha")
            if isinstance(s, str):
                pending.add(s)
                covered.discard(s)
        elif "core" in ((e.get("data") or {}).get("allow") or []):
            covered |= pending
            pending = set()
    return sha in covered


def _judged(f):
    return f.startswith(("plugin/lib/", "plugin/hooks/", "plugin/bin/")) or f in (
        "plugin/settings.json", "plugin/.claude-plugin/plugin.json")


def _benched(f):  # what a hook can run or read: any plugin file but tests and prose, and the changelog SessionStart shows
    return (f.startswith("plugin/") and not f.startswith("plugin/tests/") and not f.endswith(".md")) or f == "CHANGELOG.md"


KEY_MODULES = ("selfgate", "doctor", "replay")  # T-6031: the modules whose code decides a verdict (hashed by content)
# T-6031: the sampo modules the gate (or doctor's footprint) imports that are NOT in the key, and why; a test reads this
# file's and doctor's imports and fails when one is in neither list, so a new input is a decision, not an omission
KEY_EXEMPT = {
    "core": "git, ledger and text plumbing: it moves and prints what the judge reads, it doesn't decide a row",
    "serve": "doctor's own command pages; the footprint rules don't call it",
    "setup": "doctor's own install checks; the footprint rules don't call it",
    "flake_forecast": "doctor's own report; the footprint rules don't call it",
    "py": "doctor's interpreter probe; the footprint rules don't call it",
}
# What else decides a verdict is the sha's own and so is in no key: the base's harness (replay.py, bench_hooks.py and its
# payloads), the guard cases and the trees are read from the commit and its parent. The recent-command corpus
# (selfgate._corpus) and the machine's load vary by the minute and are not inputs a pass is held to.


def _module_bytes(name):
    """The source bytes of sampo.<name>, or None when it can't be read."""
    import importlib
    try:
        with open(importlib.import_module(f"sampo.{name}").__file__, "rb") as f:
            return f.read()
    except (OSError, TypeError, ImportError):
        return None


def gate_key():
    """T-6031: what a recorded pass is true for besides the commit's sha (its parent, and so the base it was judged against,
    are the sha's own): the content of each KEY_MODULES module and the bench's settings. A change to any makes an earlier
    pass unproven, and the commit is judged again. An unreadable module gives a key no other call shares: nothing is skipped."""
    h = hashlib.sha256(json.dumps([RUNS, PAIRS, HOLD, RISE, FLOOR_MS, CORPUS, list(KEY_MODULES)]).encode())
    for name in KEY_MODULES:
        got = _module_bytes(name)
        h.update(name.encode() + b"\0" + (hashlib.sha256(got).digest() if got is not None else os.urandom(8)))
    return h.hexdigest()


def _pass_body(rec):
    """What a self_change_passed record's MAC covers: its commit, key, task, project and time."""
    d = rec.get("data") if isinstance(rec.get("data"), dict) else {}
    return {"sha": d.get("sha"), "key": d.get("key"), "task": rec.get("task"), "project": rec.get("project"),
            "ts": rec.get("ts")}


def _log_pass(p, b, sha, via=""):
    """T-6031: the push hold's memo of a pass. via names what passed it: "" the gate itself, ":yes" the user's yes to this
    commit's refusal, ":standing" the standing core yes over quality rows (read again as the standing yes now stands).
    Signed (the same account key as the cache and prompt events, c.prompt_sealer's way): a line an agent appends to the
    ledger carries no valid MAC and is not read, so the memo is no weaker than the lane_landed record it sits beside; with
    no usable key the line is unmarked and the hold judges the commit again."""
    secret = _secret()
    c.log_event(p, "self_change_passed", task=b.id, data={"sha": sha, "key": gate_key() + via},
                seal=(lambda rec: _mac(secret, _pass_body(rec))) if secret else None)


def passed(p, shas, since=""):
    """T-6031: of shas, the ones with a signed self_change_passed record, at or after `since`, under today's gate_key: the
    gate's own pass, a yes's, or the standing yes's while that still stands (revoked, the commit is judged again)."""
    secret, key, want = _secret(), gate_key(), set(shas)
    if secret is None or not want:
        return set()
    ok = {key, key + ":yes"} | ({key + ":standing"} if (c.read_meta(p).get("standing") or {}).get("core") else set())
    out = set()
    for e in c.ledger_events(p, ("self_change_passed",), since=since):
        d = e.get("data") if isinstance(e.get("data"), dict) else {}
        mac = d.get("mac")
        if d.get("sha") in want and d.get("key") in ok and isinstance(mac, str) and hmac.compare_digest(
                mac, _mac(secret, _pass_body(e))):
            out.add(d["sha"])
    return out


def timing_due(p, main, base, files):
    """T-5935, T-6048: (whether the hooks need a timing bench for a change of `files` against `base`, the benched files among
    them): none when no changed file is one a hook can run or read (_benched: docs, tests, notes), or when the saved hook
    map says no changed file is one a hook process loaded; a missing or stale map times as before. One rule for a live
    train's merged batch (train._live_timing) and the push hold's direct-commit range (release._judge)."""
    benched = [f for f in files if _benched(f)]
    hmap = cached_hook_map(p, main, base) if benched else None
    return bool(benched) and not (hmap and not needs_timing(files, hmap)), benched


def gate(p, b, main, ref, files, cache_name=CACHE_FILE, timing=True, record=False, passes=None):
    """[refusal lines] for a commit that changes Sampo's own plugin/, [] when it holds, isn't one, or the user said yes
    after its last refusal of this same commit. Each refusal is a self_change_refused ledger event with the sha.
    cache_name is the file its base half is cached in (T-4805: the push hold's own, not the lane train's).
    timing=False (T-5446, a live train's per-lane hold) leaves out the hook timing bench; the train times the merged
    batch once instead (train._live_timing).
    record=True (T-6031, the push hold's) logs a signed self_change_passed {sha, key} when the commit passes: by the gate,
    by the user's yes (key + ":yes") or by the standing core yes (key + ":standing"); passed() reads them back.
    passes (T-6048), a dict: the pass is not logged but set as passes[sha] = the kind ("", ":yes" or ":standing"), for a
    caller that records it only after a check the gate left out (the push hold's one timing of its whole range)."""
    if not os.path.isfile(os.path.join(main, "plugin", "lib", "fmguard.py")) or not any(
            f.startswith("plugin/") for f in files):
        return []
    sha = _git(main, "rev-parse", "--verify", f"{ref}^{{commit}}", text=True).stdout.strip()
    if sha and approved(p, b, sha):
        if record:  # T-6031: the user's yes to this commit's refusal is a pass the hold need not ask for again
            _log_pass(p, b, sha, ":yes")
        elif passes is not None:
            passes[sha] = ":yes"
        return []
    out, tmp = [], tempfile.mkdtemp(prefix="fm-selfgate-")
    try:
        if not sha:
            raise RuntimeError(f"no commit {ref}")
        head = _git(main, "rev-parse", "HEAD", text=True).stdout.strip()
        judged = _landed(main, head, sha)  # T-3647: judge what the train lands (the lane merged onto the base)
        base, new = _tree(main, head, os.path.join(tmp, "base")), _tree(main, judged, os.path.join(tmp, "branch"))
        for rel in HARNESS:  # the judge is the base's: a branch can't rewrite how it is measured
            src, dst = os.path.join(base, rel), os.path.join(new, rel)
            if os.path.isfile(src):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copyfile(src, dst)
        key = _key(base)
        cache = _load(p, key, cache_name)  # T-3176: the base half, rerun only when the base, settings, machine or a day changed
        was, now = _cases(main, head), _cases(main, judged)
        out += [f"guard case {i or '?'} removed or changed by the branch: {c.fit(c.redact(cmd), 100)}"
                for (cmd, cwd), i in was.items() if (cmd, cwd) not in now]
        if any(_judged(f) for f in files) or was != now:
            cmds = list(dict.fromkeys(list(was) + list(now) + _corpus()))
            with QUIET.replay():  # T-6031: never beside another judgement's hook bench
                v0, v1 = _base_verdicts(base, [list(x) for x in cmds], cache), _verdicts(new, [list(x) for x in cmds])
            for (cmd, _), a, z in zip(cmds, v0, v1):
                broke = not isinstance(z, str) or (z.startswith("error:") and not str(a).startswith("error:"))
                if (a != "allow" and z == "allow") or broke:
                    out.append(f"guard: {c.fit(c.redact(' '.join(cmd.split())), 100)}: base {a}, branch {z}")
        from sampo import doctor
        f0, f1 = _footprint(base, {}), _footprint(new, {})
        grown = doctor.grown(f0, f1)
        benched = [f for f in files if _benched(f)]
        # T-3139: timings run when a hook could be slower (a file some hook process imported, a hook entry, a data file)
        timed = timing and (bool(grown) or (bool(benched) and needs_timing(files, hook_map(p, main, head, base))))
        if benched and not timed and timing:
            c.log_event(p, "self_gate_timing_skipped", task=b.id, data={"sha": sha, "files": benched[:20]})
        if timed:  # timings, and the injected chars a cut needs
            excused = []
            slow, s0, s1 = _paired(base, new, cache, excused)
            out += list(slow.values())
            for e in excused[:30]:  # T-3091: a pass can be checked by hand
                c.log_event(p, "self_gate_excused", task=b.id, data=dict(e, sha=sha))
            if excused:
                top = max(excused, key=lambda e: e["raw"])
                print(f"self-change gate: {len(excused)} hook{'s' if len(excused) != 1 else ''} rose past +{RISE * 100:.0f}% "
                      f"but passed within their own spread or the machine's share; largest raw rise "
                      f"+{(top['raw'] - 1) * 100:.0f}% ({top['hook']}); see the self_gate_excused events", file=sys.stderr)
            f0, f1 = _footprint(base, s0), _footprint(new, s1)
        out += [f"footprint {k} {w} -> {cur} (+{int((cur - w) * 100 // w)}%) with no always-on cut to pay for it"
                for k, w, cur in doctor.grown(f0, f1) if not k.endswith(" ms")]
        if cache.get("bench") or cache.get("verdicts"):
            _save(p, key, cache, cache_name)
        v0, v1 = _verbs(base), _verbs(new)  # T-2363: a verb added with none removed is growth too (gen-0 §6)
        added = [v for v in v1 if v not in v0]
        if added and not [v for v in v0 if v not in v1]:
            out.append(f"verbs {len(v0)} -> {len(v1)} (+{len(added)}: {', '.join(added[:5])}) with none removed")
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, IndexError, ZeroDivisionError,
            subprocess.SubprocessError) as e:
        out.append(f"the self-change gate couldn't run ({type(e).__name__}: {c.fit(str(e), 160)})")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    # T-3219: the user's standing core yes ("I said stop asking me for things", 2026-11-05) passes a hold made only
    # of quality rows; a guard that blocks less, a removed case or a gate that couldn't run still needs their own yes
    if out and all(r.startswith(QUALITY_ROWS) for r in out) and (c.read_meta(p).get("standing") or {}).get("core"):
        c.log_event(p, "self_change_standing_pass", task=b.id, data={"sha": sha, "rows": out[:20]})
        if record and sha:  # T-6031: kept only while that standing yes stands (passed() checks), so revoking it re-judges
            _log_pass(p, b, sha, ":standing")
        elif passes is not None and sha:
            passes[sha] = ":standing"
        return []
    if out:
        c.log_event(p, "self_change_refused", task=b.id, data={"sha": sha, "rows": out[:20]})
    elif record and sha:  # T-6031: the gate's own pass: the hold skips this commit next time
        _log_pass(p, b, sha)
    elif passes is not None and sha:
        passes[sha] = ""
    return out
