"""T-2342: Sampo stops only at an irreversible, ambiguous step. Each plan step and verify command is sorted by door
when the task is focused: a command the guard puts in a category (or can't read) is the guard's, with its run-time
stop and grant path untouched; a step naming a path git doesn't track is a local one-way step; the rest are two-way.
A local one-way step gets a verified snapshot and an Undo line, or runs only when the request asked for it and the
brief has one reading, or parks the task for the final report. Never changes fmguard: it only reads its categories."""
import glob
import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import tempfile

from sampo import core as c

CAP = 20 * 1024 * 1024  # bytes a snapshot may hold (FM_DOOR_CAP overrides); state grows by at most this per step
FILES = 2000  # entries a snapshot may hold: the copy and the hash stay bounded in time too
CONFIDENCE = 90  # the percent a verified snapshot's step is not reversed: the Undo line restores what it names
_TICK = re.compile(r"`([^`\n]+)`")
# ponytail: a verb gates which untracked paths a step "names" (reading /etc/hosts isn't a door); a step that changes a
# file with another word is read as two-way here and still met by the guard when its command runs
_DESTROY = re.compile(r"(?i)\b(delet\w*|remov\w*|rm|eras\w*|wip\w*|purg\w*|drop\w*|truncat\w*|overwr\w*|rewrit\w*|replac\w*|"
                      r"renam\w*|mv|mov(?:e|es|ed|ing)|clean\w*|reset|prun\w*|migrat\w*|convert\w*|regenerat\w*|clobber\w*|"
                      r"discard\w*|edit\w*|updat\w*|modif\w*|writ\w*|shred\w*|compress\w*|gzip|sed|tee)\b")
_NEGATE = re.compile(r"(?i)\b(?:don'?t|do not|dont|never|not|no|without|avoid|keep|leave|preserve|except|skip|stop)\b")
# commands whose every non-flag word is a file they change; the others (cp, mv, redirects, ...) come from the guard's own
# write-target parse, fmguard.write_targets
_ALLARGS = {"rm", "rmdir", "unlink", "shred", "truncate", "gzip", "gunzip", "bzip2", "xz", "sed", "tee", "dd"}
_REGEN = {"build", "dist", "node_modules", "__pycache__", ".venv", "target", ".cache"}  # ignored AND rebuilt by a command
_PATHS = 50  # paths one step may name: the snapshot, its copy and the hash stay bounded


def _cap():
    try:
        return int(os.environ.get("FM_DOOR_CAP", CAP))
    except ValueError:
        return CAP


def _ctx(p, b):
    import fmguard
    home = os.path.expanduser("~")
    return fmguard.Ctx(cwd=p.root, project_root=fmguard.project_root_for(p.root, home), home=home,
                       foreman_home=c.foreman_home(), state_dir=c.state_dir(), state_fallbacks=c.state_fallbacks(),
                       scratch=[x for x in ("/tmp", "/var/tmp", os.environ.get("TMPDIR")) if x],
                       task_id=b.id if b else None)


def _cats(cmds, ctx):
    """The guard's own category for each command, whichever it puts one on; a command it can't read counts too
    (fail closed: a stop the guard would make at run time is never Sampo's to decide)."""
    import fmguard
    out = []
    for cmd in cmds:
        try:
            out += [cat for cat, _ in fmguard.check_bash(cmd, ctx)]
        except Exception:  # noqa: BLE001 — any failure to classify is the guard's call
            out.append("unclassified")
    return sorted(set(out))


def _tokens(text):
    """Path-like words of a step: one with a '/' or a '.', no flag or URL."""
    for w in re.split(r"[\s`]+", text):
        w = w.strip("\"'()[]{},;:").rstrip(".")
        if w and "://" not in w and not w.startswith("-") and ("/" in w or "." in w) and w not in (".", ".."):
            yield w


def _full(root, tok):
    return os.path.join(root, os.path.expandvars(os.path.expanduser(tok)))


def _inside(path, root):
    return os.path.commonpath([path, root]) == root


def _expand(tok, root):
    """The existing paths a word names: $VAR and ~ expanded, a glob matched; each repo-relative when inside it, else absolute."""
    full = _full(root, tok)
    hits = sorted(glob.glob(full)) if any(ch in full for ch in "*?[") else [full] if os.path.lexists(full) else []
    rroot = os.path.realpath(root)
    out = []
    for h in hits[:_PATHS]:
        real = os.path.join(os.path.realpath(os.path.dirname(h)), os.path.basename(h))  # the leaf is never followed
        out.append(os.path.relpath(real, rroot) if _inside(real, rroot) or real == rroot else real)
    return out


def _local(path, root):
    """'root' for the repo itself or a folder above it; 'local' for an existing path git doesn't track (outside the repo,
    untracked, or ignored but not a rebuilt folder: .env, *.db); else None (tracked, or regenerable)."""
    rroot = os.path.realpath(root)
    real = os.path.normpath(os.path.join(rroot, path))
    if os.path.commonpath([real, rroot]) == real:
        return "root"
    if not _inside(real, rroot):
        return "local"
    if _REGEN & set(os.path.relpath(real, rroot).split(os.sep)):
        return None
    if not (stat.S_ISREG(m := os.lstat(real).st_mode) or stat.S_ISLNK(m) or stat.S_ISDIR(m)):
        return "local"  # a FIFO, socket or device: git never lists it, and nothing can copy it back
    try:
        ign = subprocess.run(["git", "-C", rroot, "check-ignore", "-q", "--", real], capture_output=True, timeout=10).returncode
        if ign != 1:
            return "local"  # ignored (0), or git can't say (128): untracked until it says otherwise
        r = subprocess.run(["git", "-C", rroot, "ls-files", "--others", "--exclude-standard", "-z", "--", real],
                           capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return "local"
    return "local" if r.returncode != 0 or r.stdout else None


def _bare(text):
    """Plain words of a step that could be a file or folder name (checked against the disk by _expand)."""
    for w in re.split(r"[\s`]+", text):
        w = w.strip("\"'()[]{},;:").rstrip(".")
        if re.fullmatch(r"\w[\w.-]*", w) and len(w) > 1 and not w.isdigit():  # a word with a '/' is _tokens' (and its parent isn't named)
            yield w


def _cmd_targets(cmd):
    """Words a shell command changes: the guard's own write targets, plus every file word of the destroying commands."""
    import fmguard
    out = []
    for seg in re.split(r"&&|\|\||[;|\n]", cmd):
        try:
            words = shlex.split(seg)
        except ValueError:
            out += list(fmguard.write_targets(seg))
            continue
        while words and (re.fullmatch(r"\w+=\S*", words[0]) or words[0] in ("then", "do", "else", "elif", "if", "!", "{",
                                                                             "(", "time", "while", "until")):
            words = words[1:]  # assignments and shell keywords: the program is the next word
        # T-2435: touch and mkdir write a path but destroy nothing in it (a redirect on the same segment still counts)
        if not (words and os.path.basename(words[0]) in ("touch", "mkdir") and not re.search(r"[<>]", seg)):
            out += list(fmguard.write_targets(seg))
        if words and os.path.basename(words[0]) in _ALLARGS:
            out += [w[3:] if w.startswith("of=") else w for w in words[1:] if not w.startswith("-") and not re.fullmatch(r"s/.*/.*/\w*|\d+", w)]
    return out


def _one(where, text, shell, ctx_of, root, local_only=False):
    head = text.split("Undo:", 1)[0]
    cmds = [text] if shell else _TICK.findall(head)
    toks = [t for cmd in cmds for t in _cmd_targets(cmd)]
    if not shell and _DESTROY.search(head):
        toks += [*_tokens(head), *_bare(head)]
    # T-3677: a local door needs a path, so the gate skips the guard (~9 ms of its patterns compiled) when none is named
    cats = _cats(cmds, ctx_of()) if cmds and (toks or not local_only) else []
    paths, named, flag = [], [], None
    if not cats:
        for t in dict.fromkeys(toks):
            hits = [(h, _local(h, root)) for h in _expand(t, root)]
            hits = [(h, k) for h, k in hits if k]
            if hits:
                named.append(t)
                flag = flag or any(k == "root" for _, k in hits)
                paths += [h for h, _ in hits if h not in paths]
    return {"where": where, "door": "guard" if cats else "local" if paths else "two-way", "cats": cats, "paths": paths[:_PATHS],
            "named": named, "root": True if flag else None}


def classify(p, b, local_only=False):
    """[{where: 'step N' | 'verify N', door: guard | local | two-way, cats, paths}] for every step and verify command;
    local_only (the gate's) reads a guard's door that names no path as two-way, every local door unchanged."""
    box = []

    def ctx_of():
        if not box:
            box.append(_ctx(p, b))
        return box[0]
    rows = [_one(f"step {s.n}", s.text, False, ctx_of, p.root, local_only) for s in b.steps()]
    for a in b.acceptance():
        cmd = c.verify_of(a.text)
        if cmd:
            rows.append(_one(f"verify {a.n}", cmd, True, ctx_of, p.root, local_only))
    return rows


def reply_door(text, p):
    """'one-way' when a reply's question is about a command the guard stops (a backticked one it categorizes, or a word
    of its categories: fmcli's list), else 'two-way'."""
    text = text or ""
    if _cats(_TICK.findall(text), _ctx(p, None)) if _TICK.search(text) else False:
        return "one-way"
    from sampo import cli
    return "one-way" if cli._guard_named(text) else "two-way"


# ---------------------------------------------------------------- snapshot

def _digest(path):
    h = hashlib.sha256()
    if os.path.isdir(path) and not os.path.islink(path):
        walk = []
        for dp, dn, fn in os.walk(path):
            dn.sort()
            walk.append((dp, [*fn, *(d for d in dn if os.path.islink(os.path.join(dp, d)))]))
    else:
        walk = [(os.path.dirname(path), [os.path.basename(path)])]
    for dp, names in walk:
        for name in sorted(names):
            q = os.path.join(dp, name)
            h.update(os.path.relpath(q, path).encode("utf-8", "surrogateescape") + b"\0")
            if os.path.islink(q):
                h.update(os.readlink(q).encode("utf-8", "surrogateescape"))
                continue
            if not stat.S_ISREG(os.lstat(q).st_mode):  # a FIFO would block open() forever
                raise OSError(f"{q} is not a regular file")
            with open(q, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
    return h.hexdigest()


def _same(root, snap, rels):
    """The restore command run into a temp folder gives back what the repo holds now, byte for byte (by hash)."""
    with tempfile.TemporaryDirectory() as tmp:
        r = subprocess.run(["cp", "-a", "--", snap + "/.", tmp + "/"], capture_output=True, timeout=120)
        return r.returncode == 0 and all(os.path.lexists(os.path.join(tmp, rel)) and os.path.lexists(os.path.join(root, rel))
                                         and _digest(os.path.join(tmp, rel)) == _digest(os.path.join(root, rel))
                                         for rel in rels)


def _size(path, cap):
    """(bytes, entries, special): special is true when anything is a FIFO, socket or device (not copied: reading it can block)."""
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode):
        return st.st_size, 1, not (stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode))
    total, n = st.st_size, 0
    for dp, dn, fn in os.walk(path):
        for name in [*fn, *dn]:
            n += 1
            m = os.lstat(os.path.join(dp, name))
            total += m.st_size
            if not (stat.S_ISREG(m.st_mode) or stat.S_ISLNK(m.st_mode) or stat.S_ISDIR(m.st_mode)):
                return total, n, True
            if total > cap or n > FILES:
                return total, n, False
    return total, n, False


def _snapshot(p, tid, key, toks):
    """(Undo command, note) after a copy of the named paths that restores byte for byte, else (None, why not).
    key: 'step3' or 'verify1'. A sidecar <snapshot>.json keeps each path's digest for drift()."""
    import shutil
    rroot, cap, rels = os.path.realpath(p.root), _cap(), []
    try:
        for t in toks:
            full = _full(p.root, t)
            real = os.path.join(os.path.realpath(os.path.dirname(full)), os.path.basename(full))
            if not _inside(real, rroot):
                return None, f"{t} is outside the repo"
            rels.append(os.path.relpath(real, rroot))
        total = count = 0
        for rel in rels:
            s, k, special = _size(os.path.join(rroot, rel), cap)
            if special:
                return None, f"{rel} holds a FIFO, socket or device, which a copy can't restore"
            total, count = total + s, count + k
        if total > cap or count > FILES:
            return None, f"over the snapshot cap ({total // 1024} KB+, {count}+ entries; FM_DOOR_CAP bytes)"
        snap = os.path.join(p.dir, "door-snapshots", f"{tid}-{key}")
        shutil.rmtree(snap, ignore_errors=True)
        for rel in rels:
            os.makedirs(os.path.dirname(os.path.join(snap, rel)), exist_ok=True)
            r = subprocess.run(["cp", "-a", "--", os.path.join(rroot, rel), os.path.join(snap, rel)],
                               capture_output=True, timeout=120)
            if r.returncode:
                raise OSError("cp failed")
        if not _same(rroot, snap, rels):
            shutil.rmtree(snap, ignore_errors=True)
            return None, "the snapshot did not verify (restored hashes differ)"
        with open(snap + ".json", "w") as f:
            json.dump({"digests": {rel: _digest(os.path.join(rroot, rel)) for rel in rels}}, f)
        return f"cp -a {shlex.quote(snap + '/.')} {shlex.quote(rroot + '/')}", \
            f"{', '.join(toks)}: {total} bytes in {count} entries copied to {snap} and restored into a temp folder with matching hashes"
    except (OSError, subprocess.SubprocessError) as e:
        return None, f"no snapshot ({type(e).__name__})"


def drift(p, tid, n):
    """Paths a step's snapshot covers whose live data exists and differs from the digest stored with the snapshot (a path
    the step removed, or left alone, is not drift). Step done warns on these: the Undo line restores the snapshot's copy."""
    try:
        with open(os.path.join(p.dir, "door-snapshots", f"{tid}-step{n}.json")) as f:
            digests = json.load(f)["digests"]
    except (OSError, ValueError, KeyError, TypeError):
        return []
    out = []
    for rel, was in digests.items():
        live = os.path.join(os.path.realpath(p.root), rel)
        try:
            if os.path.lexists(live) and _digest(live) != was:
                out.append(rel)
        except OSError:
            out.append(rel)
    return out


# ---------------------------------------------------------------- the gate

_h = c.door_hash  # what door_seen and door_undone store for a step or verify text


def _named(raw, tok):
    """The request asks for this path: its whole token (not a longer name or a word inside one), in a clause with a
    destroy verb and with no negation before it ('update the data model' doesn't name src/data; 'don't delete a' doesn't ask)."""
    want = re.escape(tok.lower().replace("\\", "/").removeprefix("./").rstrip("/"))
    pat = re.compile(r"(?<![\w/.~$-])(?:\./)?" + want + r"/?(?![\w/-]|\.\w)")
    for clause in re.split(r"(?<=[.!?;])\s+|[\n;]+", raw.lower()):
        m = pat.search(clause)
        if m and _DESTROY.search(clause) and not _NEGATE.search(clause[:m.start()]):
            return True
    return False


def _ambiguous(b):
    """M/L: the Interpretation holds two or more distinct readings (one ruled out with a reason doesn't count), or the
    cold rival read the request differently (rival_split, fmsecond; T-2424: T-2341's brief 8 reads it here)."""
    return b.tier in ("M", "L") and (bool(b.meta.get("rival_split"))
                                     or c._distinct(c._split_options(b.section("Interpretation"), bullets=False)) >= 2)


def _decide(p, tid, text, why, rejected, kind, confidence=None):
    from sampo import cli
    cli._write_decision(p, text, why, rejected, kind=kind, task=tid, confidence=confidence)
    if confidence is None:
        return
    try:  # a stated confidence is a bet in the calibration ledger, as fm decide --confidence makes it
        from sampo import outcomes
        tb = c.find_brief(p, tid)
        outcomes.bet(p, "decision", confidence, task=tid, key=text.lower(),
                       base=outcomes.base_rate(outcomes.outcomes(p), outcomes._first_finish(p), tb.type, tb.tier))
    except Exception:  # noqa: BLE001 — the ledger never loses the decision just written
        pass


def gate(p, tid):
    """Run when fm focus starts a task: each local one-way step gets a verified snapshot and its Undo line (a decision
    with a confidence), runs unsnapshotted only when the request names it and the brief has one reading (a costly
    decision), or parks the task (a marked step with no Undo line; c.waits_on_user reads it). Returns the park reason or None."""
    from sampo import cli
    b = c.find_brief(p, tid)
    if not b or b.status in c.CLOSED:
        return None
    todo, raw = {s.n: s for s in b.steps() if not s.done}, b.section("Raw request")
    acs = {a.n: c.verify_of(a.text) for a in b.acceptance() if not a.checked}
    old = {k: [str(x) for x in b.meta.get(k) or []] for k in ("door_seen", "door_undone")}
    seen, undone, edits, notes, doors, door_acs, park = list(old["door_seen"]), list(old["door_undone"]), {}, [], [], [], None
    for d in classify(p, b, local_only=True):
        kind, n = d["where"].split()[0], int(d["where"].split()[1])
        text = todo[n].text if kind == "step" and n in todo else acs.get(n) if kind == "verify" else None
        if d["door"] != "local" or not text:
            continue
        head = text.split("Undo:", 1)[0]
        (doors if kind == "step" else door_acs).append(n)
        if c.undo_ok(text) or (kind == "verify" and _h(text) in undone):  # a real way back is there already
            continue
        if d["root"]:
            undo, why = None, "it names the repo itself or a folder above it, which a copy can't cover"
        elif b.has_evidence(step=n) if kind == "step" else False:
            undo, why = None, "the step already has evidence, so its text is fixed"
        else:
            undo, why = _snapshot(p, tid, f"{kind}{n}", d["paths"])
        if undo:
            if kind == "step":
                edits[n] = (text, f"{head.rstrip().rstrip('.')}. Undo: {undo}")
            else:
                undone.append(_h(text))
            notes.append(("snap", head, why, d["paths"]))
        elif not d["root"] and all(_named(raw, t) for t in d["named"]) and not _ambiguous(b):
            (doors if kind == "step" else door_acs).remove(n)
            if _h(text) not in seen:
                notes.append(("ran", head, why, d["paths"]))
        else:
            park = park or f"{kind} {n}"
            if _h(text) not in seen:
                notes.append(("park", head, why, d["paths"]))
        if not undo and _h(text) not in seen:
            seen.append(_h(text))
    doors, door_acs = sorted(set(doors)), sorted(set(door_acs))
    if (doors, door_acs, seen, undone) != (c.door_marked(b), c.door_marked(b, "door_acs"), old["door_seen"], old["door_undone"]) or edits:
        def apply(x):
            for n, (was, new) in edits.items():  # a step edited since it was read keeps its text; door_gaps says so
                cur = next((s for s in x.steps() if s.n == n), None)
                if cur is not None and cur.text == was:
                    x.edit_step(n, new)
            for key, val in (("doors", doors), ("door_acs", door_acs), ("door_seen", seen), ("door_undone", undone)):
                if val:
                    x.meta[key] = val
                else:
                    x.meta.pop(key, None)
        cli.mutate(p, tid, apply, "door_gate", {"doors": doors, "door_acs": door_acs, "park": park})
    for kind, text, why, paths in notes:  # after the brief is saved, and outside every lock (each writer takes its own)
        if kind == "snap":
            _decide(p, tid, f"Snapshot before a one-way step: {text}", f"{why}; the step's Undo line restores it",
                    "ran the step with no copy", "reversible", CONFIDENCE)
        elif kind == "ran":
            _decide(p, tid, f"Ran one-way step the request asked for, without a snapshot: {text}",
                    f"{why}; the request names {', '.join(paths)} and the brief has one reading", "parked it for the final report", "costly")
        else:
            _decide(p, tid, f"Parked {tid} at a one-way, ambiguous step: {text}",
                    f"{why}; the request doesn't name {', '.join(paths)} or the brief has two readings: "
                    f"{c.fit(' '.join(b.section('Interpretation').split()) or 'one reading, not named by the request', 300)}",
                    "ran the step without a copy", "costly")
            c.log_event(p, "door_park", task=tid, data={"door": "one-way", "step": text[:120]}, session=cli.session())
    return c.door_park(c.find_brief(p, tid)) if park else None


def check(p, tid):
    """The gate for fm focus and for a step or criterion changed on an active task: an error in it is a PolicyError naming
    the cause (never a traceback), and a task left with a door that has no real Undo line is refused the way focus does."""
    b = c.find_brief(p, tid)
    if not b or b.status in c.CLOSED:
        return
    try:
        gate(p, tid)
    except c.PolicyError:
        raise
    except Exception as e:  # noqa: BLE001
        raise c.PolicyError(f"{tid}: the one-way door check failed ({type(e).__name__}: {e}); fix that, or write an "
                            f"'Undo: cp -a SNAPSHOT/. .' line on the step by hand")
    if b.status in ("active", "verifying") and (gaps := c.door_gaps(c.find_brief(p, tid))):
        raise c.PolicyError(f"{tid} has a one-way step on untracked data and needs {', '.join(gaps)}")


def tally(b):
    """T-2346: the finish receipt's one line on the plan's local one-way steps: each undone (its Undo line's restore)
    or parked (no way back, left for the final report); '' when the plan had none."""
    marked = c.door_marked(b)
    if not marked:
        return ""
    steps = {s.n: s.text for s in b.steps()}
    parts = [f"step {n} undone ({c.fit(steps[n].split('Undo:', 1)[1].strip(), 60)})" if c.undo_ok(steps.get(n, ""))
             else f"step {n} parked" for n in marked]
    return f"Doors: {len(marked)} one-way step{'' if len(marked) == 1 else 's'}: " + ", ".join(parts)
