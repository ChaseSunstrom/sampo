"""T-5472: a three-way merge for MASTER.md and docs/map/*.md that knows a map line by its module key (- `name`).

git's union merge kept every line of both sides, so a line a lane carried unchanged from its base came back beside the
version trunk had changed or moved: 23 stale MASTER.md bullets returned that way in one live land (2026-11-21). Here a
stretch both sides kept is taken as is, a stretch one side changed takes that side, and a stretch both changed is
settled line by line against the base:
  trunk kept the line          -> the lane's state of it: kept, changed or gone
  trunk changed it, lane kept  -> trunk's
  both changed it              -> a word-level three-way merge of the line (two appended clauses keep both); two
                                  rewrites of one span are a conflict
  trunk dropped it, lane kept  -> gone (the stale-line rule)
  trunk dropped it, lane changed -> the lane's line replaces trunk's copy wherever trunk moved it; none: a conflict
Lines both sides added at one place are all kept, trunk's first, a start they share once. A line each side holds at
most once never comes out twice (both moved it, to two places), and a module key the result would hold more often
than either side, with other words (both added it), is a conflict. Within a stretch both changed, lines come out in
the base's order, so trunk reordering lines the lane also changed is not kept. A conflict is written with
git's markers; keep_both=True keeps both versions instead (trunk's first) and still counts it, for the lane train,
which then asks for a read of the entry.

Runs as the git merge driver `sampo-map` (python3 -I mapmerge.py %O %A %B %P %L: the standard library only, no sampo
import) and is used by the lane train for a map conflict git left. ensure(repo) writes the driver into a repo's config,
which its worktrees share."""
import collections
import difflib
import os
import re
import shlex
import subprocess
import sys

NAME = "sampo-map"
LINE_CHARS = 2000  # T-6103: doctor.MAP_LINE_CHARS (the Read cuts a longer line; test_docs.MapShards pins it)
_KEY = re.compile(r"- `([^`]+)`")
_TOKEN = re.compile(r"\w+|\s+|[^\w\s]")


def _key(line):
    m = _KEY.match(line)
    return m.group(1) if m else None


def _lines(text):
    parts = text.split("\n")
    if parts[-1] == "":
        parts.pop()
    return parts


def _blocks(a, b):
    return [m for m in difflib.SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks() if m.size]


def _regions(base, ours, theirs):
    """The diff3 walk: ('same', lines) for a base stretch both sides kept, ('diff', base, ours, theirs) between."""
    ma, mb = _blocks(base, ours), _blocks(base, theirs)
    syncs, i, j = [], 0, 0
    while i < len(ma) and j < len(mb):
        a, b = ma[i], mb[j]
        lo, hi = max(a.a, b.a), min(a.a + a.size, b.a + b.size)
        if lo < hi:
            syncs.append((lo, hi, a.b + lo - a.a, b.b + lo - b.a))
        if a.a + a.size < b.a + b.size:
            i += 1
        else:
            j += 1
    syncs.append((len(base), len(base), len(ours), len(theirs)))
    bi = oi = ti = 0
    for lo, hi, so, st in syncs:
        if bi < lo or oi < so or ti < st:
            yield "diff", base[bi:lo], ours[oi:so], theirs[ti:st]
        if hi > lo:
            yield "same", base[lo:hi]
        bi, oi, ti = hi, so + hi - lo, st + hi - lo


def _merge_line(b, o, t):
    """A word-level three-way merge of one line both sides changed: the merged line, or None when both rewrote one
    span. Both inserting at one place keeps both insertions, trunk's first (two lanes each appending a clause)."""
    out = []
    for r in _regions(_TOKEN.findall(b), _TOKEN.findall(o), _TOKEN.findall(t)):
        if r[0] == "same":
            out += r[1]
            continue
        _, bs, os_, ts = r
        if os_ == ts or ts == bs:
            out += os_
        elif os_ == bs:
            out += ts
        elif not bs:
            out += os_ + ts
        else:
            return None
    return "".join(out)


def _alike(a, b):
    """difflib's ratio of two lines, 0 when its cheap upper bounds already say under 0.5."""
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    return sm.ratio() if sm.real_quick_ratio() >= 0.5 and sm.quick_ratio() >= 0.5 else 0.0


def _side(bs, xs):
    """How one side changed a region's base lines: fate[i] is base line i's text on that side (itself when kept) or
    None when it is gone; adds[g] are the lines it added before base line g (g == len(bs): at the end). A changed line
    is found by its module key anywhere in the region, a keyless one by its place in a replaced stretch."""
    ops = difflib.SequenceMatcher(None, bs, xs, autojunk=False).get_opcodes()
    pair = {}
    for tag, i1, i2, j1, _ in ops:
        if tag == "equal":
            pair.update((i1 + k, j1 + k) for k in range(i2 - i1))

    def once(idx, seq):
        seen = {}
        for k in idx:
            if (name := _key(seq[k])) is not None:
                seen.setdefault(name, []).append(k)
        return {name: ks[0] for name, ks in seen.items() if len(ks) == 1}

    used = set(pair.values())
    kx = once([j for j in range(len(xs)) if j not in used], xs)
    for name, i in once([i for i in range(len(bs)) if i not in pair], bs).items():
        if name in kx:
            pair[i] = kx[name]
            used.add(kx[name])
    for tag, i1, i2, j1, j2 in ops:
        if tag == "replace":  # a keyless line pairs with the likest new keyless line of its stretch, if alike enough
            for i in [i for i in range(i1, i2) if i not in pair and _key(bs[i]) is None]:
                right = [j for j in range(j1, j2) if j not in used and _key(xs[j]) is None]
                like = [(_alike(bs[i], xs[j]), -j) for j in right]
                if like and max(like)[0] >= 0.5:
                    j = -max(like)[1]
                    pair[i] = j
                    used.add(j)
    fate = [xs[pair[i]] if i in pair else None for i in range(len(bs))]
    adds, back, gap = [[] for _ in range(len(bs) + 1)], {j: i for i, j in pair.items()}, 0
    for j, line in enumerate(xs):
        if j in back:
            gap = back[j] + 1
        else:
            adds[gap].append(line)
    return fate, adds


class _Theirs(str):
    """A line the lane put where it stands: the copy the doubled-line check drops."""


class _Kept(list):
    """Both versions kept (keep_both) and already counted: the doubled-key check leaves them alone."""


class _Move:
    """A line trunk dropped from this place and the lane changed: settled once the whole file is known."""

    def __init__(self, base, theirs):
        self.base, self.theirs = base, theirs


def _added(ao, at):
    """Lines both sides added at one place: a start they share once (a new file's header), then trunk's, then the
    lane's (a repeated structural line such as a code fence is never shared between two blocks). (lines, clashes): a
    clash is one module key both added with different words."""
    at = [_Theirs(x) for x in at]
    if not ao or not at:
        return ao or at, 0
    p = 0
    while p < min(len(ao), len(at)) and ao[p] == at[p]:
        p += 1
    ko = {_key(x): x for x in ao if _key(x)}
    return ao + at[p:], sum(1 for x in at[p:] if _key(x) in ko and ko[_key(x)] != x)


def _resolve(bs, os_, ts, keep_both):
    """A region both sides changed: (lines, conflicts), or None for a conflict when keep_both is off. A line may be a
    _Move, settled by the caller."""
    fo, ao = _side(bs, os_)
    ft, at = _side(bs, ts)
    out, conflicts = [], 0
    for g in range(len(bs) + 1):
        lines, clash = _added(ao[g], at[g])
        if clash and not keep_both:
            return None
        out, conflicts = out + ([_Kept(lines)] if clash else lines), conflicts + clash
        if g == len(bs):
            break
        b, o, t = bs[g], fo[g], ft[g]
        if o == b:
            if t is not None:
                out.append(t if t == b else _Theirs(t))
        elif o is None:
            if t is not None and t != b:
                out.append(_Move(b, t))
        elif t == b or t == o:
            out.append(o)
        elif t is None and not b.strip():
            out.append(o)
        else:
            merged = _merge_line(b, o, t) if t is not None else None
            if merged is not None and len(merged) > LINE_CHARS:  # T-6103: both clauses would pass the limit: two bullets
                merged = None
            if merged is not None:
                out.append(merged)
            elif not keep_both:
                return None
            else:
                out.append(_Kept([o] if t is None else [o, t]))
                conflicts += 1
    return out, conflicts


def _markers(bs, os_, ts, size, path):
    return (["<" * size + f" ours:{path}", *os_, "|" * size + f" base:{path}", *bs, "=" * size, *ts,
             ">" * size + f" theirs:{path}"])


def _settle(out, keep_both, size, path):
    """Each _Move: the lane's line replaces trunk's one copy of the base line (or its one line of that module, merged
    word by word) elsewhere in the file; no such line is a conflict (keep_both: the lane's line stays here)."""
    conflicts = 0
    for at, m in [(k, x) for k, x in enumerate(out) if isinstance(x, _Move)]:
        plain = [k for k, x in enumerate(out) if isinstance(x, str)]
        same = [k for k in plain if out[k] == m.base]
        name = _key(m.base)
        if len(same) == 1:
            out[same[0]], out[at] = m.theirs, None
            continue
        kin = [k for k in plain if name is not None and _key(out[k]) == name] if not same else []
        merged = _merge_line(m.base, out[kin[0]], m.theirs) if len(kin) == 1 else None
        if merged is not None and len(merged) > LINE_CHARS:  # T-6103: a word merge past the limit is no merge
            merged = None
        if merged is not None:
            out[kin[0]], out[at] = merged, None
            continue
        conflicts += 1
        out[at] = m.theirs if keep_both else _markers([m.base], [], [m.theirs], size, path)
    return conflicts


def _doubled(out, ours, theirs, keep_both, size, path):
    """A merge never makes a line or a module key commoner than both sides have it. A line each side holds at most
    once that the result holds twice (both moved it, to two places, or both added it) keeps one copy, the lane's
    copy dropped. Then a module key still commoner (both added that module with other words): each of its lines that
    is not trunk's is a conflict (keep_both: kept and counted)."""
    co, ct = collections.Counter(ours), collections.Counter(theirs)
    cr = collections.Counter(x for x in out if isinstance(x, str))
    for i, x in enumerate(out):
        if isinstance(x, _Theirs) and x.strip() and cr[x] > 1 and co[x] <= 1 and ct[x] <= 1:
            out[i] = None
            cr[x] -= 1

    def count(lines):
        return collections.Counter(k for x in lines if isinstance(x, str) and (k := _key(x)) is not None)
    ko, kt, kr = count(ours), count(theirs), count(out)
    extra, mine, n = {k for k, v in kr.items() if v > max(ko[k], kt[k])}, set(ours), 0
    for i, x in enumerate(out):
        if isinstance(x, str) and extra and _key(x) in extra and x not in mine:
            n += 1
            if not keep_both:
                out[i] = _markers([], [], [x], size, path)
    return n


def merge(base, ours, theirs, keep_both=False, size=7, path=""):
    """Merge three versions of a map file: (text, conflicts). ours is trunk's (the merge's HEAD), theirs the lane's."""
    out, conflicts, ol, tl = [], 0, _lines(ours), _lines(theirs)
    for r in _regions(_lines(base), ol, tl):
        if r[0] == "same":
            out += r[1]
            continue
        _, bs, os_, ts = r
        if os_ == ts or ts == bs:
            out += os_
        elif os_ == bs:
            out += [_Theirs(x) for x in ts]
        elif (got := _resolve(bs, os_, ts, keep_both)) is None:
            out.append(_markers(bs, os_, ts, size, path))
            conflicts += 1
        else:
            out += got[0]
            conflicts += got[1]
    conflicts += _settle(out, keep_both, size, path)
    conflicts += _doubled(out, ol, tl, keep_both, size, path)
    lines = []
    for x in out:
        if isinstance(x, list):
            lines += x
        elif x is not None:
            lines.append(x)
    end = ours if ours else theirs
    return "\n".join(lines) + ("\n" if lines and (end.endswith("\n") or not end) else ""), conflicts


def command():
    """The driver line git runs (it quotes each %-path itself)."""
    return f"python3 -I {shlex.quote(os.path.abspath(__file__))} %O %A %B %P %L"


def ensure(repo):
    """Write the sampo-map driver into `repo`'s git config when it is missing or names another command (a moved
    install): True when it wrote. Unset, git merges merge=sampo-map files as plain text, a conflict per clash."""
    cmd, key = command(), f"merge.{NAME}.driver"
    try:
        got = subprocess.run(["git", "-C", repo, "config", "--get", key], capture_output=True, text=True, timeout=30)
        if got.returncode == 0 and got.stdout.strip() == cmd:
            return False
        subprocess.run(["git", "-C", repo, "config", f"merge.{NAME}.name",
                        "Sampo map merge: a map line by its module key (T-5472)"], capture_output=True, timeout=30)
        return subprocess.run(["git", "-C", repo, "config", key, cmd], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _read(path):
    with open(path, encoding="utf-8", errors="surrogateescape", newline="") as f:
        return f.read()


def main(argv):
    """git's call: %O %A %B [%P [%L]]; the result goes to %A. Exit 0 clean, 1 with conflict markers left, 2 when it
    could not run (%A untouched, so git reports the conflict itself)."""
    try:
        o, a, b = argv[1:4]
        path = argv[4] if len(argv) > 4 else a
        size = int(argv[5]) if len(argv) > 5 and argv[5].isdigit() else 7
        text, conflicts = merge(_read(o), _read(a), _read(b), size=size, path=path)
        with open(a, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
            f.write(text)
    except Exception as e:  # noqa: BLE001 — any failure leaves git's own conflict, never a half-written file
        print(f"{NAME}: {e}", file=sys.stderr)
        return 2
    if conflicts:
        print(f"{NAME}: {path}: {conflicts} line(s) both sides rewrote, left with conflict markers", file=sys.stderr)
    return 1 if conflicts else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
