"""T-2746: fm migrate moves the Sampo home and its siblings (foreman.draft, .evolve, .lanes) to the sampo names, once.

Each folder is renamed (never copied), and a symlink is left at the old path so every absolute path in settings, links
and worktree gitdir files still resolves. The record is backed up first and a manifest (with a hash of each tree) is
written beside the folders, outside them, so --undo can restore the old layout and prove it byte for byte. The steps
are decided from what is on disk, not from a counter, so a run killed anywhere resumes on the next one."""
import hashlib
import json
import os
import tempfile

from sampo import core as c

SUFFIXES = ("", ".draft", ".evolve", ".lanes")
MANIFEST, BACKUP = "sampo-migrate.json", "sampo-migrate-backup.tar.gz"


def tree_hash(path):
    """sha256 over every entry's relative path, kind and content (a symlink by its target); the same wherever the
    folder sits. ponytail: reads every byte, so a 3 GB home takes seconds; sample if it ever matters."""
    h = hashlib.sha256()
    for dp, dns, fns in os.walk(path):
        dns.sort()
        for n in sorted(fns + [d for d in dns if os.path.islink(os.path.join(dp, d))]):
            full = os.path.join(dp, n)
            h.update(os.path.relpath(full, path).encode() + b"\0")
            if os.path.islink(full):
                h.update(b"L" + os.readlink(full).encode() + b"\0")
                continue
            h.update(b"F")
            try:
                with open(full, "rb") as f:
                    for blk in iter(lambda: f.read(1 << 20), b""):
                        h.update(blk)
            except OSError:
                h.update(b"unreadable")
    return h.hexdigest()


def _parent():
    h = c.foreman_home()
    h = os.path.abspath(os.path.expanduser(h))
    if os.path.basename(h) not in ("foreman", "sampo"):
        raise ValueError(f"{h} isn't named foreman or sampo; this only moves the standard layout")
    return os.path.dirname(h)


def _link(old, new):
    os.symlink(new, old)


def _write(path, data):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".mig-")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def _is_link_to(p, target):
    return os.path.islink(p) and os.path.realpath(p) == os.path.realpath(target)


def _plan(parent):
    rows = []
    for s in SUFFIXES:
        old, new = os.path.join(parent, "foreman" + s), os.path.join(parent, "sampo" + s)
        if _is_link_to(old, new):
            rows.append((old, new, "linked"))
        elif os.path.isdir(new) and not os.path.lexists(old):
            rows.append((old, new, "moved"))  # killed between the rename and the symlink
        elif os.path.isdir(old) and not os.path.islink(old) and not os.path.lexists(new):
            rows.append((old, new, "todo"))
        elif os.path.lexists(old) or os.path.lexists(new):
            raise ValueError(f"{old} and {new} are both in the way (not a plain folder with a free sampo name); "
                             "nothing was changed")
    return rows


def migrate(undo=False, dry=False):
    parent = _parent()
    mpath = os.path.join(parent, MANIFEST)
    man = None
    if os.path.isfile(mpath):
        with open(mpath) as f:
            man = json.load(f)
    if undo:
        return _undo(parent, mpath, man, dry)
    if man and man.get("status") == "done":
        return {"status": "done", "manifest": mpath, "changed": False}
    rows = _plan(parent)
    if man and man.get("status") == "undone":
        man = None
    if not rows:
        raise ValueError(f"no foreman home under {parent} to move")
    if dry:
        return {"status": "plan", "steps": [{"old": o, "new": n, "state": s} for o, n, s in rows]}
    if not man:  # first run: back up, hash, then write the manifest before any rename
        _backup(os.path.join(parent, BACKUP))
        man = {"status": "moving", "parent": parent,
               "folders": [{"old": o, "new": n, "hash": tree_hash(o if s == "todo" else n)} for o, n, s in rows]}
        _write(mpath, man)
    for o, n, s in rows:
        try:
            if s == "todo":
                os.rename(o, n)
            if s != "linked":
                _link(o, n)
        except OSError as e:
            raise ValueError(f"couldn't move {o}: {e} (a mount point or another filesystem isn't moved by copying)") from e
    man["status"] = "done"
    _write(mpath, man)
    return {"status": "done", "manifest": mpath, "changed": True}


def _backup(path):
    from sampo import rollback
    if not os.path.isdir(c.projects_dir()):
        _write(path + ".none", {})  # nothing to back up: say so rather than leave a missing file
        return
    rollback._tar_record(path)


def _undo(parent, mpath, man, dry):
    if not man or man.get("status") not in ("done", "moving"):
        raise ValueError("nothing to undo: no migration manifest, or it is already undone")
    if dry:
        return {"status": "plan", "steps": [{"old": f["old"], "new": f["new"], "state": "undo"} for f in man["folders"]]}
    for f in man["folders"]:
        o, n = f["old"], f["new"]
        if _is_link_to(o, n):
            os.unlink(o)
        if os.path.isdir(n) and not os.path.lexists(o):
            os.rename(n, o)
    man["status"] = "undone"  # recorded first: a used home (state written since the move) must not strand the undo
    _write(mpath, man)
    bad = [f["old"] for f in man["folders"] if not os.path.isdir(f["old"]) or tree_hash(f["old"]) != f["hash"]]
    r = {"status": "undone", "manifest": mpath, "changed": True}
    if bad:
        r["warning"] = f"moved back, but these differ from the hashes in the manifest: {', '.join(bad)}"
    return r


def render(r):
    if r["status"] == "plan":
        return "\n".join(f"  {x['state']:<6} {x['old']} -> {x['new']}" for x in r["steps"]) + "\n(dry run: nothing changed)"
    if r["status"] == "undone":
        if r.get("warning"):
            return f"undone: the old layout is back ({r['manifest']})\nwarning: {r['warning']}"
        return f"undone: the old layout is back and matches the manifest ({r['manifest']})"
    return (f"migrated: home and siblings now live under the sampo names, symlinks at the old paths ({r['manifest']})"
            if r["changed"] else "already migrated; nothing changed")


def cmd_migrate(args):
    from sampo import cli
    run, show = migrate, render
    if getattr(args, "what", "home") == "plugins":  # T-3707: Claude Code's plugin ids, not the folders
        from sampo import pluginmove
        run, show = pluginmove.migrate, pluginmove.render
    try:
        r = run(args.undo, args.dry_run)
    except ValueError as e:
        raise cli.UsageError(str(e)) from None
    cli.out(args, r, show(r))
    return 0
