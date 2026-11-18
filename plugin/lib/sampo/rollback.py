"""T-1085: a snapshot of every project's record before a new Sampo version runs on it, and fm rollback to restore it.
The ledger is append-only and never restored; consent the user has since changed (standing, approvals, autonomy,
briefs' allow/approved) stays as it is now, so a rollback can't bring back what was revoked."""
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

from sampo import core as c
from sampo.tidy import BACKUP_PARTS

KEEP = 3  # per pool: upgrade snapshots, and the copies a rollback takes of the state it replaces
PRE = "pre-rollback-"
STALE_PART_S = 600
LIVE_META = ("standing", "pending_approvals", "autonomy", "sensitive", "paused", "drive", "trust_seen")


def _dir():
    return os.path.join(c.state_dir(), "snapshots")


def _stamp():
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def _never_restored(rel):  # the ledger and its monthly archives: restoring them breaks the hash chain and loses events
    return rel == "ledger.jsonl" or (rel.startswith("archive/") and os.path.basename(rel).startswith("ledger-"))


def _tar_record(path):
    """Every project's BACKUP_PARTS as <slug>/<part>, plus MANIFEST.json (file -> sha256); .part, renamed at the end."""
    files = {}
    with tarfile.open(path + ".part", "w:gz") as t:
        def add(arc, data):
            ti = tarfile.TarInfo(arc)
            ti.size, ti.mtime = len(data), time.time()
            t.addfile(ti, io.BytesIO(data))
        for p, _ in c.all_projects():
            slug = os.path.basename(p.dir)
            for part in BACKUP_PARTS:
                top = os.path.join(p.dir, part)
                walk = [top] if os.path.isfile(top) else [os.path.join(dp, f) for dp, _, fs in os.walk(top) for f in fs]
                for full in walk:
                    try:
                        with open(full, "rb") as f:
                            data = f.read()
                    except OSError:
                        continue
                    arc = f"{slug}/{os.path.relpath(full, p.dir)}"
                    files[arc] = hashlib.sha256(data).hexdigest()
                    add(arc, data)
        add("MANIFEST.json", json.dumps({"files": files}).encode())
    os.replace(path + ".part", path)


def _prune(d, upgrade):
    pool = [f for f in os.listdir(d) if f.endswith(".tar.gz") and f.startswith(PRE) != upgrade]
    for f in sorted(pool, key=lambda f: os.path.getmtime(os.path.join(d, f)))[:-KEEP]:
        os.remove(os.path.join(d, f))


def _snapshot_now(old):
    d = _dir()
    os.makedirs(d, exist_ok=True)
    _tar_record(os.path.join(d, f"{old}-{_stamp()}.tar.gz"))
    _prune(d, True)


def snapshot_upgrade(old):
    """The hook's call: skip when a finished snapshot for this old version exists, sweep stale .part files, then tar in
    a detached child (never on the hook's 5 s path); FOREMAN_NO_BACKGROUND runs it inline."""
    d = _dir()
    os.makedirs(d, exist_ok=True)
    for f in os.listdir(d):
        if f.endswith(".part") and time.time() - os.path.getmtime(os.path.join(d, f)) > STALE_PART_S:
            os.remove(os.path.join(d, f))
    if any(f.startswith(f"{old}-") and f.endswith(".tar.gz") for f in os.listdir(d)):
        return
    if os.environ.get("FOREMAN_NO_BACKGROUND"):
        return _snapshot_now(old)
    subprocess.Popen([sys.executable, os.path.join(c.PLUGIN_ROOT, "bin", "fm"), "rollback", "--snapshot", old],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


def _failed(old, err):
    try:
        from sampo import hooks  # T-3431: rotation and repeat collapse; lazy, the failure path only
        hooks.log_error("snapshot_failed", f"{old}: {err}")
    except Exception:
        pass


def _listing():
    d = _dir()
    names = [f for f in os.listdir(d) if f.endswith(".tar.gz")] if os.path.isdir(d) else []
    return sorted(names, key=lambda f: os.path.getmtime(os.path.join(d, f)), reverse=True)


def _stage(path, tmp, explicit):
    """Extract into tmp and check it against the manifest; returns {slug: [relative files]} or raises ValueError."""
    with tarfile.open(path) as t:
        t.extractall(tmp, filter="data")
    try:
        with open(os.path.join(tmp, "MANIFEST.json"), encoding="utf-8") as f:
            want = json.load(f)["files"]
    except (OSError, ValueError, KeyError):
        raise ValueError("no readable manifest: a snapshot from before manifests, or a damaged one")
    have = {os.path.relpath(os.path.join(dp, f), tmp) for dp, _, fs in os.walk(tmp) for f in fs} - {"MANIFEST.json"}
    if have != set(want):
        raise ValueError("the files in it don't match its manifest")
    for rel, sha in want.items():
        with open(os.path.join(tmp, rel), "rb") as f:
            if hashlib.sha256(f.read()).hexdigest() != sha:
                raise ValueError(f"{rel} doesn't match its checksum")
    slugs = {}
    for rel in want:
        slugs.setdefault(rel.split("/", 1)[0], []).append(rel.split("/", 1)[1])
    gone = [s for s in slugs if not os.path.isdir(os.path.join(c.projects_dir(), s))]
    if gone and not explicit:
        raise ValueError(f"project(s) {', '.join(gone)} don't exist now; name the snapshot with --to to restore them")
    return slugs


def _copy(src, dst, data=None):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if data is None:
        with open(src, "rb") as f:
            data = f.read()
    tmp = dst + ".rollback"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, dst)


def _restore_project(p, slug, rels, tmp):
    live_briefs = {b.id: b for b in c.load_briefs(p)}
    archived = {c._BRIEF_FILE.match(os.path.basename(x)).group(1) for x in c.brief_paths(p, include_archive=True)
                if os.sep + "archive" + os.sep in x}
    for rel in sorted(rels):
        src, dst = os.path.join(tmp, slug, rel), os.path.join(p.dir, rel)
        if _never_restored(rel):
            continue
        base = os.path.basename(rel)
        if rel.startswith("tasks/") and c._BRIEF_FILE.match(base):
            tid = c._BRIEF_FILE.match(base).group(1)
            if tid in archived:
                continue  # finished or archived since: it must not come back twice
            with open(src, encoding="utf-8") as f:
                b = c.Brief.parse(f.read(), dst)
            live = live_briefs.get(tid)
            for k in ("allow", "approved"):
                if live is not None and k in live.meta:
                    b.meta[k] = live.meta[k]
                else:
                    b.meta.pop(k, None)
            if live is not None and live.path != dst:
                os.remove(live.path)  # same brief under a renamed file: the snapshot's name wins, one copy
            _copy(src, dst, b.render().encode("utf-8"))
        elif rel == "meta.json":
            with open(src, encoding="utf-8") as f:
                meta = json.load(f)
            live = c.read_meta(p)
            for k in LIVE_META:
                if k in live:
                    meta[k] = live[k]
                else:
                    meta.pop(k, None)
            _copy(src, dst, json.dumps(meta, indent=2, ensure_ascii=False).encode("utf-8"))
        else:
            _copy(src, dst)


def cmd_rollback(args):
    if args.snapshot:  # the upgrade hook's detached child
        try:
            _snapshot_now(args.snapshot)
        except Exception as e:
            _failed(args.snapshot, e)
            return 1
        return 0
    d = _dir()
    snaps = _listing()
    if args.list or not snaps:
        if args.json:
            print(json.dumps([{"name": s, "bytes": os.path.getsize(os.path.join(d, s)),
                               "pre_rollback": s.startswith(PRE)} for s in snaps], indent=2))
        else:
            print("\n".join(snaps) if snaps else "no snapshots yet: one is taken when Sampo's version changes")
        return 0 if snaps or args.list else 1
    name = args.to or next((s for s in snaps if not s.startswith(PRE)), None)
    if name not in snaps:
        print(f"no snapshot named {name}; fm rollback --list shows them")
        return 1
    if not args.yes:
        print(f"would restore {name}: briefs, meta and decisions go back as it had them, but the ledger is kept and "
              "standing, approvals, autonomy and each brief's allow/approved stay as they are now. Pass --yes to do it")
        return 1
    with tempfile.TemporaryDirectory(dir=d) as tmp:
        try:
            slugs = _stage(os.path.join(d, name), tmp, bool(args.to))
        except (ValueError, OSError, tarfile.TarError) as e:
            print(f"refusing to restore {name}: {e}")
            return 1
        _tar_record(os.path.join(d, f"{PRE}{_stamp()}.tar.gz"))
        _prune(d, False)
        by_slug = {os.path.basename(p.dir): p for p, _ in c.all_projects()}
        for slug, rels in slugs.items():
            p = by_slug.get(slug)
            if p is None:
                shutil.rmtree(os.path.join(tmp, slug), ignore_errors=True)
                print(f"skipped {slug}: no such project now")
                continue
            with c.lock(p.dir):
                _restore_project(p, slug, rels, tmp)
            c.log_event(p, "rollback", data={"snapshot": name})
    print(f"restored {name} (the state it replaced is in snapshots/{PRE}*)")
    return 0
