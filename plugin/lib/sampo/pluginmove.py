"""T-3707: sampo migrate plugins moves Claude Code's record of Sampo from an old id (c.old_plugin_ids: the old plugin
names @foreman, or sampo@ the old marketplace once its renames map ran) to sampo@sampo and sampo-ui@sampo.

A configured marketplace keeps the name it was added under (research T-2879-probe), so renaming marketplace.json never
reaches an install: the marketplace has to be added again as sampo. Claude Code's own CLI does that one change per
write (marketplace add, install, uninstall), and a session started between two of them loads both ids (every hook
twice) or neither. This rewrites each of the three files Claude Code keeps it in once, atomically, after a byte copy of
each; --undo puts the copies back. Only a marketplace read from a folder (install.sh's) is moved: its new record points
at the same folder, where Claude Code reads a directory marketplace's plugins in place."""
import datetime
import hashlib
import json
import os

from sampo import core as c

FILES = ("settings.json", os.path.join("plugins", "installed_plugins.json"),
         os.path.join("plugins", "known_marketplaces.json"))
BACKUPS = "sampo-migrate-plugins"  # in Claude Code's config folder, beside what it restores
PARTS = ("", "ui", "design")


def config_dir():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")


def _read(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return None


def _json(raw, path):
    if raw is None:
        return {}
    try:
        data = json.loads(raw)
    except ValueError as e:
        raise ValueError(f"{path} isn't JSON ({e}); nothing was changed") from None
    if not isinstance(data, dict):
        raise ValueError(f"{path} isn't a JSON object; nothing was changed")
    return data


def _dump(data, like):
    """Claude Code's own layout (two-space indent, non-ASCII as is), ending as the file did."""
    text = json.dumps(data, indent=2, ensure_ascii=False)
    return (text + "\n" if like and like.endswith(b"\n") else text).encode("utf-8")


def _sha(raw):
    return hashlib.sha256(raw).hexdigest() if raw is not None else None


def _table(data, key):
    v = data.get(key)
    return v if isinstance(v, dict) else {}


def _folder(entry):
    """A marketplace entry's folder (its real path) when it is read from one, else None."""
    s = entry.get("source") if isinstance(entry, dict) else None
    ok = isinstance(s, dict) and s.get("source") == "directory" and isinstance(s.get("path"), str)
    return os.path.realpath(s["path"]) if ok else None


def _rekey(table, moves, value):
    """table with each old key (moves: old -> new) replaced by its new one, in the first old one's place; value(new,
    vals) gives the new key's value from the old ones' values (and its own last, when it was there already)."""
    vals = {}
    for k, v in table.items():
        if k in moves:
            vals.setdefault(moves[k], []).append(v)
    out = {}
    for k, v in table.items():
        new = moves.get(k, k)
        if new in out or (k != new and new in table):
            continue  # a later old id, or an old id whose new one is in the table: that one keeps its own place
        out[new] = value(new, vals[new] + ([v] if k == new else [])) if new in vals else v
    return out


def _options(_new, vals):
    """The mod's saved options under every id it had, the current id's last so they win (settings._our_options)."""
    merged = {}
    for v in vals:
        if isinstance(v, dict):
            merged = {**merged, **v, "options": {**merged.get("options", {}), **(v.get("options") or {})}}
    return merged


def _plan(cfg):
    """(the files as they are, the move) where the move holds each file's new bytes, or is None when nothing old is
    left. Raises ValueError with the reason when the move can't be made here; nothing is written either way."""
    raws = {f: _read(os.path.join(cfg, f)) for f in FILES}
    settings, installed, known = (_json(raws[f], os.path.join(cfg, f)) for f in FILES)
    enabled, configs, extra = (_table(settings, k) for k in ("enabledPlugins", "pluginConfigs", "extraKnownMarketplaces"))
    recorded = _table(installed, "plugins")
    moves = {old: c.plugin_ids(part)[0] for part in PARTS for old in c.old_plugin_ids(part)}
    have = [k for k in moves if k in enabled or k in recorded or k in configs]
    old = known.get(c.OLD_MARKET) or extra.get(c.OLD_MARKET)
    if not have and not old:
        return raws, None
    src = old.get("source") if isinstance(old, dict) else None
    if not _folder(old):
        s = src if isinstance(src, dict) else {}
        where = s.get("repo") or s.get("url") or "ChaseSunstrom/sampo"
        raise ValueError(f"the {c.OLD_MARKET} marketplace isn't a folder on this machine ({where}), so it isn't moved "
                         f"here: claude plugin marketplace add {where} (it is named {c.MARKET} now), claude plugin "
                         f"install {c.plugin_ids()[0]}, then claude plugin uninstall each old id "
                         f"({', '.join(have) or c.old_plugin_ids()[-1]}). Until then it keeps working under its old id")
    folder = src["path"]
    mk = _json(_read(os.path.join(folder, ".claude-plugin", "marketplace.json")), folder)
    if mk.get("name") != c.MARKET:
        raise ValueError(f"the marketplace at {folder} still names itself {mk.get('name')!r}, not {c.MARKET!r}: update "
                         "that checkout first; nothing was changed")
    entry = {"source": {"source": "directory", "path": folder}}
    if known.get(c.MARKET) and _folder(known[c.MARKET]) != os.path.realpath(folder):
        raise ValueError(f"a {c.MARKET} marketplace from elsewhere is configured already "
                         f"({known[c.MARKET].get('source')}); nothing was changed")
    sources = {e.get("name"): e.get("source") for e in mk.get("plugins") or [] if isinstance(e, dict)}
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    def record(new, entries):  # the install record names the folder Claude Code reads the plugin from in place
        path = os.path.normpath(os.path.join(folder, sources.get(new.split("@")[0]) or ""))
        version = _json(_read(os.path.join(path, ".claude-plugin", "plugin.json")), path).get("version")
        out = []
        for e in (x for es in entries for x in (es if isinstance(es, list) else [es]) if isinstance(x, dict)):
            if not any((o.get("scope"), o.get("projectPath")) == (e.get("scope"), e.get("projectPath")) for o in out):
                out.append({**{k: v for k, v in e.items() if k != "gitCommitSha"}, "installPath": path,
                            "lastUpdated": now, **({"version": version} if version else {})})
        return out

    s = dict(settings)
    if enabled:
        s["enabledPlugins"] = _rekey(enabled, moves, lambda _n, vals: any(v is True for v in vals))
    if configs:
        s["pluginConfigs"] = _rekey(configs, moves, _options)
    if c.OLD_MARKET in extra:  # declared in settings too (this machine's): declared under the new name instead
        s["extraKnownMarketplaces"] = {**{n: e for n, e in extra.items() if n != c.OLD_MARKET}, c.MARKET: entry}
    i = {**installed, "plugins": _rekey(recorded, moves, record)} if recorded else installed
    k = {n: e for n, e in known.items() if n != c.OLD_MARKET}
    k[c.MARKET] = known.get(c.MARKET) or {**entry, "installLocation": folder, "lastUpdated": now}
    new = {f: _dump(d, raws[f]) if raws[f] is not None or d else None for f, d in zip(FILES, (s, i, k))}
    return raws, {"new": new, "moves": {o: moves[o] for o in have}, "folder": folder,
                  "dropped": [c.OLD_MARKET] if old else []}


def _manifest(cfg):
    path = os.path.join(cfg, BACKUPS, "manifest.json")
    try:
        with open(path) as f:
            return path, json.load(f)
    except (OSError, ValueError):
        return path, None


def migrate(undo=False, dry=False):
    cfg = config_dir()
    mpath, man = _manifest(cfg)
    if undo:
        return _undo(cfg, mpath, man, dry)
    raws, plan = _plan(cfg)
    if plan is None:
        return {"status": "done", "changed": False, "config": cfg}
    steps = {"moves": plan["moves"], "folder": plan["folder"], "dropped": plan["dropped"],
             "files": [f for f in FILES if plan["new"][f] is not None and plan["new"][f] != raws[f]]}
    if dry:
        return {"status": "plan", "config": cfg, **steps}
    bdir = os.path.dirname(mpath)
    files = {}
    for f in steps["files"]:  # every copy is on disk before the first file changes
        if raws[f] is not None:
            c.write_atomic(os.path.join(bdir, os.path.basename(f)), raws[f])
        files[f] = {"existed": raws[f] is not None, "before": _sha(raws[f]), "after": _sha(plan["new"][f])}
    c.write_atomic(mpath, json.dumps({**steps, "status": "moving", "files": files}, indent=2))
    for f in steps["files"]:  # realpath: a settings.json that is a link to a dotfiles repo stays a link
        c.write_atomic(os.path.realpath(os.path.join(cfg, f)), plan["new"][f])
    c.write_atomic(mpath, json.dumps({**steps, "status": "done", "files": files}, indent=2))
    return {"status": "done", "changed": True, "config": cfg, "backups": bdir, **steps}


def _undo(cfg, mpath, man, dry):
    if not man or man.get("status") not in ("done", "moving"):
        raise ValueError("nothing to undo: no plugin move recorded here, or it was already undone")
    files = man.get("files") or {}
    if dry:
        return {"status": "plan-undo", "config": cfg, "files": sorted(files)}
    bdir = os.path.dirname(mpath)
    olds = {}
    for f, rec in files.items():  # every backup checked before anything is restored
        if rec.get("existed"):
            olds[f] = _read(os.path.join(bdir, os.path.basename(f)))
            if _sha(olds[f]) != rec.get("before"):
                raise ValueError(f"the backup of {f} in {bdir} is missing or doesn't match the hash recorded at the "
                                 "move; nothing was restored")
    changed = []
    for f, rec in files.items():
        path = os.path.realpath(os.path.join(cfg, f))
        now = _read(path)
        if now is not None and _sha(now) not in (rec.get("after"), rec.get("before")):
            changed.append(f)  # written since the move (Claude Code, a person): kept beside the backup, then restored
            c.write_atomic(os.path.join(bdir, os.path.basename(f) + ".replaced"), now)
        if f in olds:
            c.write_atomic(path, olds[f])
        elif now is not None:
            os.remove(path)  # the move made it
    man["status"] = "undone"
    c.write_atomic(mpath, json.dumps(man, indent=2))
    r = {"status": "undone", "config": cfg, "files": sorted(files)}
    if changed:
        r["warning"] = (f"changed since the move, and those changes are undone too: {', '.join(changed)} (each kept "
                        f"as {bdir}/<name>.replaced)")
    return r


def render(r):
    cfg = r.get("config", "")
    if r["status"] == "plan-undo":
        return "\n".join(f"  restore {os.path.join(cfg, f)}" for f in r["files"]) + "\n(dry run: nothing changed)"
    if r["status"] == "undone":
        return (f"undone: {', '.join(r['files'])} in {cfg} are back as they were before the move"
                + (f"\nwarning: {r['warning']}" if r.get("warning") else "")
                + "\nRestart Claude Code sessions to load the old ids.")
    if not r.get("moves") and r["status"] == "done" and not r["changed"]:
        return f"nothing to move: Claude Code ({cfg}) has no old Sampo id or marketplace left"
    lines = [f"  {o} -> {n}" for o, n in r["moves"].items()]
    lines.append(f"  marketplace + {c.MARKET} (folder {r['folder']})")
    lines += [f"  marketplace - {m} (the same folder)" for m in r["dropped"]]
    lines.append("  one write each: " + ", ".join(os.path.join(cfg, f) for f in r["files"]))
    if r["status"] == "plan":
        return "\n".join(lines) + f"\n(dry run: nothing changed; a copy of each goes to {os.path.join(cfg, BACKUPS)} first)"
    return ("moved to " + ", ".join(dict.fromkeys(r["moves"].values())) + ":\n" + "\n".join(lines)
            + f"\nbackups: {r['backups']} (sampo migrate plugins --undo restores them)"
            + "\nRestart Claude Code sessions: until then each keeps the ids it started with.")
