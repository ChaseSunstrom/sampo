"""T-4920 (T-4838 part B): cardmine turns Sampo's own records of repeated work into card requests.

Four sources, one reader each: pick misses (callcards' requests.jsonl rows that carry the task text), failed bench
scenarios (the bench's rows and the misses it filed), closed FIX briefs whose titles repeat, and tool-call sequences
that repeat across sessions (the event log's tool events). One writer appends the requests to that same requests.jsonl,
each {at, source, title, why, count, examples}.

A request holds aggregates only: counts, verbs and file kinds drawn from the fixed lists below, tool names that look
like names, failure classes and tiers. No word of a prompt, a brief, a file, a path or a secret is copied: a word
outside VERBS and KINDS is counted and dropped, so the raw text never leaves its record (a test plants a secret and a
prompt in every source and reads each request back for them).

A candidate is dropped when a card covers it (callcards.rank above LEXICAL_FLOOR) or a request is already open for it
(the same normalized title, or a token set at least NEAR alike), so a second run over unchanged records adds nothing.
Each source offers its TOP most-repeated candidates before that check, so the cap never lets run two reach further down.
Models advise, proof decides: nothing here gates a close or a merge; a card writer takes the requests."""
import collections
import hashlib
import json
import os
import re

from sampo import callcards
from sampo import core as c

MIN_MISSES = 2  # pick misses of one verb and file kind before it is a request
MIN_FIXES = 3  # closed FIX tasks with alike titles
MIN_SESSIONS = 5  # sessions one tool sequence shows up in
GRAMS = (3, 4)  # tool-sequence lengths
TOP = 10  # candidates a source offers per run
NEAR = 0.8  # token-set overlap (Jaccard) at which a candidate is an open request already
FIX_ALIKE = 0.6  # title token-set overlap that puts two FIX tasks in one cluster
WINDOW_DAYS = 30  # how far back the tool events are read
MAX_ROWS = 20000  # rows read per file: the newest
MAX_CALLS = 2000  # tool calls read per session

VERBS = ("add", "remove", "delete", "rename", "move", "extract", "inline", "sort", "format", "convert", "parse",
         "write", "read", "update", "replace", "split", "merge", "list", "count", "find", "generate", "create",
         "refactor", "test", "document", "wrap", "fix", "implement", "change", "make", "check", "run", "build",
         "install", "deploy", "migrate", "validate", "optimize", "debug", "explain", "summarize", "compare", "diff",
         "search", "replace", "sync", "log", "export", "import", "clean", "simplify", "patch", "revert", "review")
# file extensions and language names -> the kind they name; only these words ever appear in a request
KINDS = {"py": "python", "python": "python", "js": "javascript", "javascript": "javascript", "ts": "typescript",
         "typescript": "typescript", "tsx": "typescript", "jsx": "javascript", "go": "go", "golang": "go",
         "rs": "rust", "rust": "rust", "java": "java", "kt": "kotlin", "c": "c", "h": "c", "cpp": "cpp", "cc": "cpp",
         "rb": "ruby", "ruby": "ruby", "php": "php", "sh": "shell", "bash": "shell", "shell": "shell", "md": "markdown",
         "markdown": "markdown", "json": "json", "yml": "yaml", "yaml": "yaml", "toml": "toml", "html": "html",
         "css": "css", "svg": "svg", "sql": "sql", "txt": "text", "csv": "csv", "xml": "xml", "swift": "swift"}
COUNTED = ("verb", "verbs", "kinds", "kind", "class", "misses", "nearest_score_max", "scenarios", "tasks", "tiers",
           "tools", "sessions")  # the keys an example may carry
SOURCES = ("pick_miss", "bench_fail", "fix_repeat", "tool_sequence")

_WORD = re.compile(r"[a-z]+")
_EXT = re.compile(r"[A-Za-z0-9_)\]]\.([A-Za-z]{1,10})(?![A-Za-z0-9_])")
_TOOL = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,39}")
_CLASS = re.compile(r"[a-z_]{1,30}")
_TIER = re.compile(r"[SML]")


def _text_words(text):
    return _WORD.findall(str(text).lower())


def _verbs(text):
    """The VERBS a text names, in the order it names them (each once)."""
    seen = []
    for w in _text_words(text):
        if w in VERBS and w not in seen:
            seen.append(w)
    return seen


def _kinds(text="", paths=()):
    """The KINDS a text or a list of paths names (an extension or a language word), sorted."""
    found = {KINDS[e.lower()] for e in _EXT.findall(str(text)) if e.lower() in KINDS}
    found |= {KINDS[w] for w in _text_words(text) if w in KINDS and len(w) > 2}  # not "c" or "go" as a plain word
    for path in paths:
        ext = os.path.splitext(str(path))[1][1:].lower()
        if ext in KINDS:
            found.add(KINDS[ext])
    return sorted(found)


def _read(path):
    """The newest MAX_ROWS dict rows of a jsonl file, [] when there is none; a torn or foreign line is skipped."""
    rows = collections.deque(maxlen=MAX_ROWS)
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    x = json.loads(line)
                except (ValueError, RecursionError):
                    continue
                if isinstance(x, dict):
                    rows.append(x)
    except OSError:
        pass
    return list(rows)


def _requests(path):
    """The newest MAX_ROWS dict rows of requests.jsonl (core's bounded tail: a torn line is skipped)."""
    return [x for x in c.tail_jsonl(path, MAX_ROWS) if isinstance(x, dict)]


def _score(x):
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) else None


# --- one reader per source ---------------------------------------------------------------------------------------------
# each returns [candidate]: {source, title, subject, why, count, examples}; subject is what a card would be asked about
# (the card check ranks it; it is not written)

def _pick_misses(rows):
    """Misses pick() filed (a row with the task's text and no source, scenario or failure class), grouped by the verb
    and file kinds the text names. The text itself is read for those and for nothing else."""
    groups = collections.defaultdict(lambda: {"n": 0, "best": None})
    for r in rows:
        if "source" in r or "scenario" in r or "failure_class" in r or not isinstance(r.get("text"), str):
            continue
        verbs, kinds = _verbs(r["text"]), _kinds(r["text"])
        if not verbs and not kinds:
            continue
        g = groups[(verbs[0] if verbs else "", tuple(kinds[:3]))]
        g["n"] += 1
        top = r.get("top")
        s = _score(top[0][1]) if isinstance(top, list) and top and isinstance(top[0], list) and len(top[0]) > 1 else None
        if s is not None and (g["best"] is None or s > g["best"]):
            g["best"] = s
    out = []
    for (verb, kinds), g in groups.items():
        if g["n"] < MIN_MISSES:
            continue
        subject = " ".join(x for x in (verb or "work", "in " + "/".join(kinds) if kinds else "") if x)
        ex = {"verb": verb or "unknown", "kinds": list(kinds), "misses": g["n"]}
        if g["best"] is not None:
            ex["nearest_score_max"] = round(g["best"], 2)
        out.append({"source": "pick_miss", "title": f"pick misses: {subject}", "subject": subject, "count": g["n"],
                    "key": f"pick:{verb}:{'/'.join(kinds)}",
                    "why": f"{g['n']} tasks asking to {verb or 'work'}{' on ' + '/'.join(kinds) + ' files' if kinds else ''}"
                           f" were left without a card (nothing ranked above {callcards.LEXICAL_FLOOR})",
                    "examples": [ex]})
    return out


def _bench_fails(bench_rows, request_rows):
    """Failed bench scenarios by failure class and scenario kind: a scenario counts once however many models failed it,
    and once when the bench's own request for it is also in requests.jsonl; only its cards-on arm counts (a scenario that
    failed with cards off alone is one the cards answered). The class and kind are written only when they are in
    louhi's FAILURE_CLASSES (plus other) and KINDS: bench.jsonl lives in the repo tree, so any other word is unknown."""
    from sampo import louhi
    classes, kinds = set(louhi.FAILURE_CLASSES) | {"other"}, set(louhi.KINDS)
    seen = {}
    for r in bench_rows:
        if r.get("passed") is False and r.get("arm") == "on":
            seen[(str(r.get("run")), str(r.get("scenario")))] = (r.get("failure_class"), r.get("kind"))
    for r in request_rows:
        if "source" not in r and r.get("scenario") is not None and r.get("failure_class") and r.get("arm") == "on":
            seen.setdefault((str(r.get("run")), str(r["scenario"])), (r.get("failure_class"), r.get("kind")))
    groups = collections.Counter()
    for cls, kind in seen.values():
        cls = cls if isinstance(cls, str) and cls in classes else "unknown"
        kind = kind if isinstance(kind, str) and kind in kinds else "unknown"
        groups[(cls, kind)] += 1
    return [{"source": "bench_fail", "title": f"bench failures: {cls} on {kind} scenarios", "key": f"bench:{cls}:{kind}",
             "subject": f"{kind} {cls}", "count": n,
             "why": f"{n} bench scenario(s) of kind {kind} failed with class {cls}",
             "examples": [{"class": cls, "kind": kind, "scenarios": n}]} for (cls, kind), n in groups.items()]


def _jaccard(a, b):
    return len(a & b) / len(a | b) if a | b else 0.0


def _title_tokens(text):
    return {w for w in _text_words(text) if len(w) > 2 and w not in callcards._STOP}


def _fix_repeats(briefs):
    """Closed FIX tasks clustered by the overlap of their normalized title tokens (letters only, ids and numbers gone,
    stop words dropped); the cluster is named by a hash of its first task's tokens, never by them."""
    clusters, index = [], collections.defaultdict(list)  # [{"tokens", "tasks": [brief]}], token -> cluster numbers
    for b in sorted((b for b in briefs if b.type == "FIX" and b.status == "done"), key=lambda b: b.id):
        toks = _title_tokens(b.title)
        if not toks:
            continue
        best, top = -1, 0.0
        for i in {i for t in toks for i in index[t]}:
            j = _jaccard(toks, clusters[i]["tokens"])
            if j > top or (j == top and best >= 0 and i < best):
                best, top = i, j
        if best >= 0 and top >= FIX_ALIKE:
            clusters[best]["tasks"].append(b)
            continue
        clusters.append({"tokens": toks, "tasks": [b]})
        for t in toks:
            index[t].append(len(clusters) - 1)
    out = []
    for cl in clusters:
        n = len(cl["tasks"])
        if n < MIN_FIXES:
            continue
        verbs, kinds, tiers = collections.Counter(), set(), collections.Counter()
        for b in cl["tasks"]:
            verbs.update(_verbs(b.title)[:1])
            kinds.update(_kinds(b.title, b.meta.get("scope") or []))
            tiers[b.tier if isinstance(b.tier, str) and _TIER.fullmatch(b.tier) else "unknown"] += 1
        vs, ks = [v for v, _ in verbs.most_common(3)], sorted(kinds)[:3]
        key = hashlib.sha256(" ".join(sorted(cl["tokens"])).encode()).hexdigest()[:6]
        subject = " ".join(vs[:1] + (["in " + "/".join(ks)] if ks else [])) or "fix"
        out.append({"source": "fix_repeat", "title": f"repeated fix: {subject} #{key}", "subject": subject, "count": n,
                    "key": f"fix:{key}",
                    "why": f"{n} closed FIX tasks with near-identical titles ({', '.join(vs) or 'no known verb'}"
                           f"{' in ' + '/'.join(ks) if ks else ''}): a fix worth a card",
                    "examples": [{"verbs": vs, "kinds": ks, "tasks": n, "tiers": dict(sorted(tiers.items()))}]})
    return out


def _tool_sequences(events, slug):
    """Runs of 3 or 4 different tools in a row (a tool repeated back to back counts once) that show up in at least
    MIN_SESSIONS sessions of this project; a run inside a longer one in as many sessions is the longer one's."""
    by = collections.defaultdict(list)
    for e in events:
        t = e.get("tool")
        if e.get("kind") != "tool" or e.get("ok") is False or e.get("project") != slug or not e.get("session_id"):
            continue
        if not isinstance(t, str) or not _TOOL.fullmatch(t):
            continue
        calls = by[str(e["session_id"])]
        if len(calls) < MAX_CALLS and (not calls or calls[-1] != t):
            calls.append(t)
    sessions = collections.defaultdict(set)
    for sid, calls in by.items():
        for n in GRAMS:
            for i in range(len(calls) - n + 1):
                gram = tuple(calls[i:i + n])
                if len(set(gram)) >= 2:
                    sessions[gram].add(sid)
    kept = {g: len(s) for g, s in sessions.items() if len(s) >= MIN_SESSIONS}
    for g in list(kept):  # a shorter run that never shows up without a longer one is not its own request
        if any(len(h) > len(g) and kept[h] >= kept[g] and any(h[i:i + len(g)] == g for i in range(len(h) - len(g) + 1))
               for h in kept):
            del kept[g]
    return [{"source": "tool_sequence", "title": "tool sequence: " + " ".join(g), "subject": " ".join(g).lower(),
             "key": "tool:" + ">".join(g),
             "count": n, "why": f"{n} sessions ran {' then '.join(g)} in a row",
             "examples": [{"tools": list(g), "sessions": n}]} for g, n in kept.items()]


# --- dedupe and write --------------------------------------------------------------------------------------------------

def _norm(title):
    return " ".join(sorted(_title_tokens(title)))


def _opened(rows):
    """What the open requests are known by: (key or None, normalized title, title tokens) for each row with a title or a
    key. A row mined here carries its key; a hand-written one has only a title."""
    out = []
    for r in rows:
        key = r.get("key") if isinstance(r.get("key"), str) else None
        title = r.get("title") if isinstance(r.get("title"), str) else ""
        if key or title:
            out.append((key, _norm(title), _title_tokens(title)))
    return out


def _is_open(cand, opened):
    """A candidate is open when a request holds its key; a request without a key is matched by its normalized title or
    a token set at least NEAR alike. A keyed request is never matched by title: two clusters can share a title once the
    words that tell them apart (a hash) are gone from its tokens."""
    toks = _title_tokens(cand["title"])
    return any(key == cand["key"] if key else (n == _norm(cand["title"]) or _jaccard(toks, t) >= NEAR)
               for key, n, t in opened)


def _covered(cand, root):
    """A card covers a candidate when it ranks at LEXICAL_FLOOR or above for the candidate's subject, as pick judges a
    task: a pick miss's verb and file kinds, a repeated fix's verb and kind, a bench failure's kind and class. A tool
    sequence is tool names, not words a card ranks on: only the open requests dedupe it."""
    if cand["source"] == "tool_sequence":
        return False
    ranked = callcards.rank(cand["subject"], k=1, root=root)
    return bool(ranked) and ranked[0][0] >= callcards.LEXICAL_FLOOR


def _offer(cands):
    """Each source's TOP most repeated, then by title: the order and the cap do not depend on what is already filed."""
    out = []
    for src in SOURCES:
        mine = sorted((x for x in cands if x["source"] == src), key=lambda x: (-x["count"], x["title"]))
        out += mine[:TOP]
    return out


def mine(p, write=True, bench=None, since=None):
    """The card requests Sampo's records of p ask for, each filed once: {"added": [request rows], "found": candidates
    offered, "covered": dropped for a card, "open": requests already in requests.jsonl}. bench: the bench rows file
    (default research/callcards/bench.jsonl of p's tree). write=False only reports."""
    bench = bench or os.path.join(p.root, "research", "callcards", "bench.jsonl")
    path = callcards.requests_path()
    since = since or c.iso(c.clock().timestamp() - WINDOW_DAYS * 86400)
    requests = _requests(path)
    try:
        briefs = c.load_briefs(p, include_archive=True)
    except (OSError, ValueError, c.CorruptState):
        briefs = []
    try:
        tools = _tool_sequences(c.global_events(since), p.slug)  # streamed: no list of the window's events
    except OSError:
        tools = []
    cands = _offer(_pick_misses(requests) + _bench_fails(_read(bench), requests) + _fix_repeats(briefs) + tools)
    # the card check is the slow part and needs no lock: it runs before the write holds one
    fresh = [x for x in cands if not _covered(x, p.root)]
    covered = len(cands) - len(fresh)

    def attempt(opened):
        added = []
        for cand in fresh:
            if _is_open(cand, opened):
                continue
            added.append({"at": c.now(), "source": cand["source"], "title": cand["title"], "key": cand["key"],
                          "why": cand["why"], "count": cand["count"], "examples": cand["examples"]})
            opened.append((cand["key"], "", set()))
        return added

    if not write:
        opened = _opened(requests)
        return {"added": attempt(list(opened)), "found": len(cands), "covered": covered, "open": len(opened)}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with c.lock(os.path.dirname(path)):  # two runs at once file one request, not two
            opened = _opened(_requests(path))
            n_open = len(opened)
            added = attempt(opened)
            for row in added:
                c.append_line(path, json.dumps(row))
    except c.LockTimeout as e:  # a busy lock files nothing: the next run finds the same candidates
        return {"added": [], "found": len(cands), "covered": covered, "open": len(_opened(requests)),
                "reason": str(e)}
    return {"added": added, "found": len(cands), "covered": covered, "open": n_open}
