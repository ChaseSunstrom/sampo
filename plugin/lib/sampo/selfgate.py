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
import hashlib
import hmac
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time

from sampo import core as c

CASES = ("plugin/tests/fixtures/guard_cases.jsonl", "plugin/tests/fixtures/guard_allow.jsonl")
HARNESS = ("lib/sampo/replay.py", "tests/bench_hooks.py", "tests/fixtures/hook_payloads.json")  # base's copies judge
RISE = 0.15  # a hook's p95 rise that refuses (T-2358's latency band)
FLOOR_MS = 5.0  # under this, a rise is jitter whatever its share
RUNS = 10  # bench runs per tree per pair
PAIRS = 3  # T-2992: base and branch benched in turn this many times
HOLD = PAIRS  # T-3131: a rise must show in every pair (2 of 3 across 16 hooks held identical trees by chance)
TAIL_RISE, TAIL_FLOOR_MS = 0.5, 25.0  # T-3131 review: a p95 rise this big, in every pair, is a tail slowdown, not noise
WALL_RISE, WALL_FLOOR_MS = 1.0, 100.0  # T-2363: on a loaded bench, a wall-time rise this big is a hook that waits
CORPUS = 2000  # recent real commands replayed, newest first (fmreplay.corpus)
_RUNNER = ("import json, os, sys\nsys.path.insert(0, sys.argv[1])\nfrom sampo import replay as r\n"
           "cmds = [(x, r.resolve_cwd(y) if y else y) for x, y in json.load(sys.stdin)]\nv = r.verdicts(cmds)\n"
           "print(json.dumps([v.get(r._key(x, y or os.path.expanduser('~'))) for x, y in cmds]))\n")
LOAD_BAND = 1.5  # T-3176: a cached base bench is reused only when the load per CPU now is within this factor of its own
CACHE_S = 86400  # T-3176: the base's bench and replay, kept per base tree hash, are good for a day


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


def _verdicts(plugin, cmds):
    """Each command's verdict from this tree's own guard, judged by the base's fmreplay."""
    # T-2363: a hook the bench started may still be writing its state as this returns; a leftover file is no reason
    # for the gate to fail (a gate that can't run refuses)
    with tempfile.TemporaryDirectory(prefix="fm-gate-state-", ignore_cleanup_errors=True) as state:
        r = subprocess.run([sys.executable, "-I", "-c", _RUNNER, os.path.join(plugin, "lib")], input=json.dumps(cmds),
                           capture_output=True, text=True, timeout=600, env=_env(state))
    got = json.loads(r.stdout or "null")
    if not isinstance(got, list) or len(got) != len(cmds):
        raise RuntimeError(f"guard replay: {(r.stderr or r.stdout).strip()[-160:]}")
    return got


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


def _load(p, key):
    """The saved base half when its MAC verifies under the user's key, it is for this key and under a day old, else {}.
    The bench is dropped (the replay kept) when the host's load now is not within LOAD_BAND of the load it ran at."""
    path = os.path.join(p.dir, "selfgate-base.json")
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


def _save(p, key, cache):
    cache.setdefault("at", time.time())
    cache.setdefault("load", _load_pc())
    path = os.path.join(p.dir, "selfgate-base.json")
    secret = _secret()
    if secret is None:
        return
    body = dict(cache, key=key)
    try:
        fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(dict(body, mac=_mac(secret, body)), f)
        os.replace(path + ".tmp", path)
    except OSError:
        pass


def _base_verdicts(base, cmds, cache):
    """The base's verdict for each command; only those not in the cache are replayed."""
    seen = cache.setdefault("verdicts", {})
    miss = [x for x in cmds if json.dumps(x) not in seen]
    if miss:
        seen.update(zip(map(json.dumps, miss), _verdicts(base, miss)))
    return [seen[json.dumps(x)] for x in cmds]


def _paired(base, new, cache=None):
    """T-2992: (held rows, base's injected chars, branch's) from PAIRS benches of base then branch in turn, so load that
    comes and goes lands on both; a hook's rise holds when it shows in HOLD pairs (a one-off spike doesn't).
    T-3176: with a cache the base's PAIRS benches are reused (or kept after a fresh run); the branch always runs."""
    old = (cache or {}).get("bench")
    # T-3119: a timing bench never runs beside another (parallel lane checks would skew each other's p95); every
    # other check runs at once. ponytail: one machine-wide lock, any process; a stuck holder times out after an hour
    try:
        with c.lock(os.path.join(c.state_dir(), "bench"), timeout=3600):
            if isinstance(old, list) and len(old) == PAIRS:
                runs = [(a, _bench(new)) for a in old]
            else:
                runs = [(_bench(base), _bench(new)) for _ in range(PAIRS)]
                if cache is not None:
                    cache["bench"], cache["load"] = [a for a, _ in runs], _load_pc()
    except c.LockTimeout as e:  # the gate's own refusal ("couldn't run"), not a crash
        raise RuntimeError(f"another hook bench held the bench lock for an hour: {e}") from None
    seen = [_slower(a[0], z[0]) for a, z in runs]
    rows = {k: next(s[k] for s in seen if k in s) for k in {k for s in seen for k in s}
            if sum(k in s for s in seen) >= HOLD}
    return rows, runs[0][0][1], runs[0][1][1]


def _ms(v, loaded):
    x = v.get("cpu_p95") if loaded else v.get("p95")
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def _slower(base, branch):
    """Hooks whose p95 rose past RISE (and FLOOR_MS) or past the base's budget, on one basis (CPU time if either run
    was loaded). A new fixture takes the base's tightest budget; the branch's own budgets are never read."""
    loaded = any(v.get("load", 0) > 1 for v in list(base.values()) + list(branch.values()))
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
        over = isinstance(budget, (int, float)) and p1 is not None and p1 > budget and not (p0 is not None and p0 > budget)
        rise = b0 is not None and b0 > 0 and b1 > b0 * (1 + RISE) and b1 - b0 >= FLOOR_MS
        tail = both and p0 and p1 is not None and p1 > p0 * (1 + TAIL_RISE) and p1 - p0 >= TAIL_FLOOR_MS
        q = "p50" if bk.get("p50") and v.get("p50") else "p95"  # T-2605: load spikes inflate p95
        w1, w0 = v.get(q), bk.get(q)
        walls = isinstance(w0, (int, float)) and isinstance(w1, (int, float)) and w0 > 0
        # CPU time misses a hook that sleeps or waits on I/O: loaded, a doubling past 100 ms of wall isn't load noise;
        # quiet (T-3131 review), the wall median rising past RISE and FLOOR_MS is a wait
        waits = walls and ((loaded and w1 >= w0 * (1 + WALL_RISE) and w1 - w0 >= WALL_FLOOR_MS) or
                           (not loaded and both and q == "p50" and w1 > w0 * (1 + RISE) and w1 - w0 >= FLOOR_MS))
        if over:
            rows[k] = f"hook {k} {tag} {p0} -> {p1} ms (past its {budget} ms budget)"
        elif rise:
            rows[k] = f"hook {k} {'median CPU' if both else tag} {b0} -> {b1} ms (+{(b1 - b0) * 100 / b0:.0f}%)"
        elif waits:
            rows[k] = f"hook {k} {q} {w0} -> {w1} ms wall (+{(w1 - w0) * 100 / w0:.0f}%; its CPU time didn't rise: it waits)"
        elif tail:
            rows[k] = f"hook {k} {tag} {p0} -> {p1} ms (+{(p1 - p0) * 100 / p0:.0f}%: a slowdown in the tail alone)"
    return rows


def _footprint(plugin, sizes):
    from sampo import doctor
    return dict(doctor.footprint(os.path.join(plugin, "rules", "foreman.md"), plugin),
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
    try:
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump({"sha": head, "map": got}, f)
        os.replace(path + ".tmp", path)
    except OSError:
        pass
    return got


QUALITY_ROWS = ("footprint ", "verbs ", "hook ")  # T-3219: rows the standing yes may pass (never "guard…")


def approved(p, b, sha):
    """The user's yes to `fm ask ID core` on this task, given after its last self-change refusal of this commit."""
    last, yes = None, False
    for e in c.ledger_events(p, ("self_change_refused", "approval_granted"), since=str(b.meta.get("created") or "")):
        if e.get("task") != b.id:
            continue
        if e.get("event") == "self_change_refused":
            last, yes = (e.get("data") or {}).get("sha"), False
        elif last is not None and "core" in ((e.get("data") or {}).get("allow") or []):
            yes = True
    return yes and last == sha


def _judged(f):
    return f.startswith(("plugin/lib/", "plugin/hooks/", "plugin/bin/")) or f in (
        "plugin/settings.json", "plugin/.claude-plugin/plugin.json")


def _benched(f):  # what a hook can run or read: any plugin file but tests and prose, and the changelog SessionStart shows
    return (f.startswith("plugin/") and not f.startswith("plugin/tests/") and not f.endswith(".md")) or f == "CHANGELOG.md"


def gate(p, b, main, ref, files):
    """[refusal lines] for a commit that changes Sampo's own plugin/, [] when it holds, isn't one, or the user said yes
    after its last refusal of this same commit. Each refusal is a self_change_refused ledger event with the sha."""
    if not os.path.isfile(os.path.join(main, "plugin", "lib", "fmguard.py")) or not any(
            f.startswith("plugin/") for f in files):
        return []
    sha = _git(main, "rev-parse", "--verify", f"{ref}^{{commit}}", text=True).stdout.strip()
    if sha and approved(p, b, sha):
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
        cache = _load(p, key)  # T-3176: the base half, rerun only when the base, settings, machine or a day changed
        was, now = _cases(main, head), _cases(main, judged)
        out += [f"guard case {i or '?'} removed or changed by the branch: {c.fit(c.redact(cmd), 100)}"
                for (cmd, cwd), i in was.items() if (cmd, cwd) not in now]
        if any(_judged(f) for f in files) or was != now:
            cmds = list(dict.fromkeys(list(was) + list(now) + _corpus()))
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
        timed = bool(grown) or (bool(benched) and needs_timing(files, hook_map(p, main, head, base)))
        if benched and not timed:
            c.log_event(p, "self_gate_timing_skipped", task=b.id, data={"sha": sha, "files": benched[:20]})
        if timed:  # timings, and the injected chars a cut needs
            slow, s0, s1 = _paired(base, new, cache)
            out += list(slow.values())
            f0, f1 = _footprint(base, s0), _footprint(new, s1)
        out += [f"footprint {k} {w} -> {cur} (+{int((cur - w) * 100 // w)}%) with no always-on cut to pay for it"
                for k, w, cur in doctor.grown(f0, f1) if not k.endswith(" ms")]
        if cache.get("bench") or cache.get("verdicts"):
            _save(p, key, cache)
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
        return []
    if out:
        c.log_event(p, "self_change_refused", task=b.id, data={"sha": sha, "rows": out[:20]})
    return out
