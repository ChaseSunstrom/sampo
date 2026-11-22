"""T-5179 (child k3 of T-4338, Moons at scale): every kept moon graded new, partial or built against what Sampo already
has: the done tasks (built, with the task id), fm's verbs whose help the moon restates (built, with the verb), the open
tasks near it and the modules' map lines near it (partial, with that evidence), else new. Rows append to
moons/grounding.jsonl as {id, state, evidence, digest, mark, at}; a moon's grade is its last row. Model-free, and cheap
at 10,000 moons: the briefs, the verb docs and the map load once per run, a moon is graded again only when its text
digest or the newest closed task (the mark) changed, and the store lock is never taken (rows and fates are read as
they stand; a row is one appended line)."""
import collections
import hashlib
import json
import os
import re

from sampo import core as c
from sampo import moonscale, recall

GROUNDING = "grounding.jsonl"
STATES = ("new", "partial", "built")
MAP_LEAD = 300  # characters of a map line's own text matched against: its lead, not the long history behind it
MAP_SHARE = 0.6  # the share of a moon title's stems a module's lead must hold to be near it (and 3 at least)
_BULLET = re.compile(r"^- `([^`\n]+)`\s*(.*)")


def _path(p):
    return os.path.join(moonscale._dir(p), GROUNDING)


def digest(x):
    """A moon's text digest: its title, payoff and what it does (the words a grade reads), 12 hex digits."""
    text = "\n".join(str(x.get(k) or "") for k in ("title", "today", "with_this", "what"))
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:12]


def state(p):
    """{moon id: its last grounding row}: a torn or foreign line is skipped; no store lock is taken."""
    return {x["id"]: x for x in moonscale._read(_path(p)) if isinstance(x.get("id"), str)}


def mark(briefs):
    """The newest closed task: its update time and id, '' with none. A task closed since a moon was graded regrades it."""
    done = [(str(b.meta.get("updated") or ""), b.id) for b in briefs if b.status == "done"]
    return "|".join(max(done)) if done else ""


def _modules(p):
    """[(name, stems of the lead of its map line)] from MASTER.md and the files-* shards, one line per module."""
    text = c.master_text(p.root, "files") or ""
    out = []
    for line in text.splitlines():
        m = _BULLET.match(line)
        if m:
            lead = re.sub(r"[`*]", "", m.group(2))[:MAP_LEAD]
            out.append((m.group(1), set(recall._tokens(lead)), c.fit(" ".join(lead.split()), 120)))
    return out


def _near_module(title, mods, index):
    """(name, lead) of the module whose map line holds most of the title's stems (3 or more, MAP_SHARE of them)."""
    q = set(recall._tokens(title))
    hits = collections.Counter(i for w in q for i in index.get(w, ()))
    best = max(hits.items(), key=lambda kv: (kv[1], -kv[0]), default=None)
    if best and best[1] >= 3 and best[1] >= MAP_SHARE * len(q):
        return mods[best[0]][0], mods[best[0]][2]
    return None


def _grade(x, done, docs, opened, mods, index):
    """(state, evidence) of one moon against the loads of this run."""
    title = str(x.get("title") or "")
    if title in done:
        return "built", f"{done[title][0]}: {done[title][1]}"
    payoff = re.sub(r"(?i)^with this, you can\s*", "", str(x.get("with_this") or ""))
    for text in (title, payoff):
        for name, help_ in recall.covered(text, docs) if text else ():
            return "built", f"fm {name}: {help_}"
    q, names = recall._title_tokens(title)
    best = (0.0, None)
    for d, theirs, tid, ttitle in opened if len(q) >= 2 else ():
        j = recall.near_share(q, names, [], d, theirs, [])
        if j is not None and j > best[0]:
            best = (j, f"{tid} (open): {ttitle}")
    if best[1]:
        return "partial", best[1]
    near = _near_module(title, mods, index)
    if near:
        return "partial", f"module {near[0]}: {near[1]}"
    return "new", ""


def ground(p):
    """Grade every kept moon not yet graded at its current text digest and the current mark; append the rows, log
    moons_ground {kept, grounded, new, partial, built} (the counts over every kept moon now) and return that dict.
    No model, no store lock: the rows and fates are read as they stand."""
    kept = moonscale._kept(p)
    graded = state(p)
    todo = []
    briefs, now_mark = None, ""
    if kept:
        briefs = c.load_briefs(p, include_archive=True)  # once for the run: never once per moon
        now_mark = mark(briefs)
        todo = [x for x in kept if (g := graded.get(x.get("id"))) is None
                or g.get("digest") != digest(x) or g.get("mark") != now_mark]
    counts = collections.Counter(g.get("state") for x in kept if (g := graded.get(x.get("id"))))
    if todo:
        done = recall.nearest_done(p, sorted({str(x.get("title") or "") for x in todo}), briefs)
        docs = recall.verb_docs()
        opened = [(*recall._title_tokens(b.title), b.id, b.title) for b in briefs if b.status not in c.CLOSED]
        mods = _modules(p)
        index = collections.defaultdict(list)
        for i, (_, words, _) in enumerate(mods):
            for w in words:
                index[w].append(i)
        at, lines = c.now(), []
        for x in todo:
            st, why = _grade(x, done, docs, opened, mods, index)
            old = graded.get(x.get("id"))
            if old and old.get("state") in STATES:
                counts[old["state"]] -= 1
            counts[st] += 1
            lines.append(json.dumps({"id": x["id"], "state": st, "evidence": why, "digest": digest(x),
                                     "mark": now_mark, "at": at}, ensure_ascii=False))
        os.makedirs(moonscale._dir(p), exist_ok=True)
        c.append_line(_path(p), "\n".join(lines))
    out = {"kept": len(kept), "grounded": len(todo), **{s: counts[s] for s in STATES}}
    c.log_event(p, "moons_ground", data=out)
    return out
