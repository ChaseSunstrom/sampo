"""T-5019 (slice of T-4315, the steward): `sampo steward dup` finds open tasks that say the same thing twice. Each open
task's title and acceptance criteria become one sparse vector of hashed words and word pairs (bigrams) weighted by
TF-IDF over the open tasks, and two tasks whose cosine passes DUP are a duplicate, passing FOLD a fold candidate (one
reads as part of the other: close the newer into the older). No training and no model: a pass over a few hundred
tasks takes milliseconds, pure Python, and the same tasks give the same rows. Models advise, proof decides: a row is a
suggestion, nothing here closes, edits or merges a task.

Bounded: at most MAX_TASKS tasks (the newest), MAX_TOKENS tokens per task, MAX_CLIP characters of each criterion, from 30
open tasks on a word in more than max(5, 34%) of them (boilerplate: "the", "sampo") is left out of both the vector and
the pairing (below 30, IDF alone weighs the words), a postings list is
capped at MAX_POST so a pass never goes quadratic on one common bucket, and the pairing stops at BUDGET_S seconds. Two
tasks where one depends on the other are a deliberate split, never a pair. A project with no open task gives no rows."""
import math
import re
import time
import zlib

from sampo import core as c

DUP = 0.8          # cosine at which two open tasks are a duplicate
FOLD = 0.45        # ... and at which one reads as part of the other (fold the newer into the older)
DIM = 1 << 16      # hash buckets: a collision of two words is rare at a few hundred tokens a task
MAX_TASKS = 1500   # open tasks read, the newest
MAX_TOKENS = 400   # tokens one task's text keeps (a pasted log is clipped, not read whole)
MAX_POST = 200     # a bucket shared by more tasks than this is boilerplate
COMMON = 0.34      # ... or by more than this share of the tasks, once there are MIN_COMMON of them
MIN_COMMON = 30    # fewer open tasks than this: IDF alone weighs the words (a small backlog's real cluster is no boilerplate)
TITLE_W = 2        # the title's words count this many times: it is the task's own summary
MAX_CLIP = 4000    # characters of one criterion read (a pasted log is clipped before any regex runs)
MAX_ROWS = 50      # rows one run returns, best first
BUDGET_S = 3.0     # the pairing's wall clock

_WORD = re.compile(r"[a-z0-9]+(?:[._'-][a-z0-9]+)*")
_CODE = re.compile(r"`[^`\n]*`")      # a verify command or a path in code quotes: its words are the test's, not the task's
_TID = re.compile(r"\bt-\d{3,6}\b", re.I)  # a task id names a neighbour, not the subject
STOP = frozenset("a an and are as at be by for from has have in is it its of on or that the this to with when "
                 "was were will not no do does should must can may if then than so but also into per via".split())


def _stem(w):
    """A crude suffix strip so expire, expires and expired meet: ing, ed, es, s (not ss), then a final e."""
    if len(w) > 4:
        for suf in ("ing", "ed", "es"):
            if w.endswith(suf) and len(w) - len(suf) > 2:
                w = w[:-len(suf)]
                break
        else:
            if w.endswith("s") and not w.endswith("ss"):
                w = w[:-1]
    return w[:-1] if len(w) > 3 and w.endswith("e") else w


def _tokens(text):
    out = []
    for m in _WORD.finditer(text.lower()):
        w = m.group(0)
        if w not in STOP and len(w) > 1:
            out.append(_stem(w))
    return out


def _bucket(gram):
    return zlib.crc32(gram.encode("utf-8", "replace")) & (DIM - 1)


def text_of(b):
    """A task's own words: its title (twice) and its acceptance criteria, without their verify commands, code spans
    and task ids."""
    parts = [] if b.title == "untitled" else [b.title] * TITLE_W  # core.Brief.new's placeholder says nothing
    for a in b.acceptance():
        t = c.strip_verify((a.text or "")[:MAX_CLIP])
        parts.append(_TID.sub(" ", _CODE.sub(" ", t)))
    return "\n".join(parts)


def counts(text):
    """{bucket: term count} of the words and the bigrams of consecutive words in text, clipped to MAX_TOKENS words."""
    words = _tokens(text)[:MAX_TOKENS]
    grams = words + [f"{a} {b}" for a, b in zip(words, words[1:])]
    got = {}
    for g in grams:
        k = _bucket(g)
        got[k] = got.get(k, 0) + 1
    return got


def vectors(texts):
    """[(unit TF-IDF vector as {bucket: weight})] for the texts, in order: sublinear tf, smoothed idf over these
    texts, boilerplate buckets dropped first so the norm and the dot agree."""
    tf = [counts(t) for t in texts]
    n = len(tf)
    df = {}
    for row in tf:
        for k in row:
            df[k] = df.get(k, 0) + 1
    cap = MAX_POST if n < MIN_COMMON else min(MAX_POST, max(5, int(n * COMMON)))
    out = []
    for row in tf:
        v = {}
        for k, t in row.items():
            if df[k] <= cap:
                v[k] = (1 + math.log(t)) * (math.log((1 + n) / (1 + df[k])) + 1)
        norm = sum(x * x for x in v.values()) ** 0.5
        out.append({k: x / norm for k, x in v.items()} if norm else {})
    return out


def similar(vecs, floor=FOLD, deadline=None, info=None):
    """[(i, j, cosine)] for i < j whose vectors' cosine is at least floor, through an inverted index: only tasks that
    share a bucket are ever compared. deadline: a time.monotonic() after which the pairing stops.
    info: a dict that gets timed_out (bool) and skipped (buckets over MAX_POST left out), so a cut is never silent."""
    post = {}
    for i, v in enumerate(vecs):
        for k in v:
            post.setdefault(k, []).append(i)
    acc = {}
    skipped, timed_out = 0, False
    for k, ids in post.items():
        if len(ids) < 2:
            continue
        if len(ids) > MAX_POST:
            skipped += 1
            continue
        if deadline is not None and time.monotonic() > deadline:
            timed_out = True
            break
        for a in range(len(ids)):
            i = ids[a]
            wi = vecs[i][k]
            for b in range(a + 1, len(ids)):
                j = ids[b]
                acc[(i, j)] = acc.get((i, j), 0.0) + wi * vecs[j][k]
    if info is not None:
        info["timed_out"], info["skipped"] = timed_out, skipped
    return [(i, j, s) for (i, j), s in acc.items() if s >= floor]


def _num(tid):
    m = re.search(r"(\d+)\s*$", tid or "")
    return int(m.group(1)) if m else 0


def _linked(a, b):
    """True when one task names the other as a dependency: a split the user chose, not a duplicate."""
    da, db = a.meta.get("depends_on") or [], b.meta.get("depends_on") or []
    da, db = [da] if isinstance(da, str) else da, [db] if isinstance(db, str) else db
    return b.id in da or a.id in db


def find(briefs, dup=DUP, fold=FOLD, limit=MAX_ROWS, budget=BUDGET_S, info=None):
    """Duplicate and fold rows over the open briefs among these, best first: {"kind": "duplicate"|"fold", "keep": the
    older task's id, "fold": the newer's, "score", "keep_title", "fold_title"}. A fold row names the newer task as the
    one to fold into the older.
    info: a dict that gets left_out (rows cut by limit), timed_out, skipped (buckets over MAX_POST) and clipped (open
    tasks beyond MAX_TASKS), so a partial answer says so."""
    info = info if info is not None else {}
    info.update(left_out=0, timed_out=False, skipped=0, clipped=0)
    open_ = [b for b in briefs if b.status in c.OPEN and b.id]
    open_.sort(key=lambda b: _num(b.id), reverse=True)
    info["clipped"] = max(0, len(open_) - MAX_TASKS)
    open_ = sorted(open_[:MAX_TASKS], key=lambda b: _num(b.id))
    if len(open_) < 2:
        return []
    vecs = vectors([text_of(b) for b in open_])
    rows = []
    for i, j, s in similar(vecs, min(fold, dup), time.monotonic() + budget, info):
        a, b = open_[i], open_[j]
        if _linked(a, b):
            continue
        rows.append({"kind": "duplicate" if s >= dup else "fold", "keep": a.id, "fold": b.id, "score": round(s, 3),
                     "keep_title": a.title, "fold_title": b.title})
    rows.sort(key=lambda r: (-r["score"], _num(r["keep"]), _num(r["fold"])))
    info["left_out"] = max(0, len(rows) - max(0, limit))
    return rows[:max(0, limit)]


def run(p, args=None, info=None):
    """The rows for p's open tasks. args may carry dup, fold (thresholds, 0 to 1) and limit; each is validated."""
    from sampo.cli import UsageError
    gdup, gfold = _threshold(args, "dup", None), _threshold(args, "fold", None)
    if gdup is not None and gfold is not None and gfold > gdup:
        raise UsageError(f"the fold threshold ({gfold}) is above the duplicate one ({gdup})")
    dup, fold = gdup if gdup is not None else DUP, gfold if gfold is not None else FOLD
    if gfold is None:
        fold = min(fold, dup)
    elif gdup is None:
        dup = max(dup, fold)
    limit = getattr(args, "limit", None)
    if limit is None:
        limit = MAX_ROWS
    else:
        try:
            limit = max(0, int(limit))
        except (TypeError, ValueError):
            raise UsageError(f"--limit wants a whole number, got {limit!r}") from None
    try:
        briefs = c.load_briefs(p)
    except (OSError, ValueError, c.CorruptState):
        return []
    return find(briefs, dup=dup, fold=fold, limit=limit, info=info)


def _threshold(args, name, default):
    from sampo.cli import UsageError
    v = getattr(args, name, None)
    if v is None:
        return default
    try:
        v = float(v)
    except (TypeError, ValueError):
        raise UsageError(f"--{name} wants a number from 0 to 1, got {v!r}") from None
    if not 0 < v <= 1:  # also false for nan
        raise UsageError(f"--{name} wants a number above 0 and up to 1, got {v}")
    return v


def line(r):
    return (f"  {r['kind']:<9} {r['score']:.2f}  {r['fold']} into {r['keep']}: "
            f"{r['fold_title'][:60]} / {r['keep_title'][:60]}")


def cmd_steward(args):
    """`sampo steward ACTION`: dup is the only action so far."""
    return cmd_dup(args)


def cmd_dup(args):
    """`sampo steward dup`: the duplicate and fold pairs among the open tasks, best first."""
    from sampo import cli
    p = cli.resolve(args)
    info = {}
    rows = run(p, args, info)
    notes = []
    if info.get("left_out"):
        notes.append(c.more(rows + [None] * info["left_out"], len(rows), "--limit")[1])
    if info.get("timed_out"):
        notes.append("stopped at the time budget: the list may be incomplete")
    if info.get("clipped"):
        notes.append(f"{info['clipped']} older open task(s) beyond {MAX_TASKS} not read")
    if info.get("skipped"):
        notes.append(f"{info['skipped']} very common word(s) not compared")
    head = f"steward dup: {sum(r['kind'] == 'duplicate' for r in rows)} duplicate, " \
           f"{sum(r['kind'] == 'fold' for r in rows)} fold candidate(s) among the open tasks"
    text = ("\n".join([head] + [line(r) for r in rows]) if rows else
            "steward dup: no duplicate or foldable open tasks")
    if notes:
        text += "\n  " + "; ".join(notes)
    cli.out(args, {"rows": rows, **info}, text)
