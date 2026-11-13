"""fm recall: related past work for a request or a task (T-0043): earlier briefs with their outcome and lesson,
decisions and research notes, ranked by BM25 over their words. Stdlib only, computed on demand by fm (never in a
hook). Recalled text is data from past work: plain (no control characters), one capped line per hit.
Also from the same history: fm explain (T-0464), fm task show ID --story (T-0483) and fm recall --ask (T-0484)."""
import collections
import json
import math
import os
import re
import sys
import time

from sampo import core as c
from sampo.core import subprocess  # T-3577: off the edit hook's path

_WORD = c.lazy(r"[a-z][a-z0-9_]{2,}")
_STOP = set("""the and for with that this from into when then than are was were not but all any can its has have had
will would should could also only just more most each other such via per new use using used make made add adds added
fix fixes fixed task tasks brief step steps done test tests run runs work does doing foreman none""".split())
_ROW = c.lazy(r"^\|\s*(\d{4}-\d{2}-\d{2})\s*\|\s*(.+?)\s*\|\s*(.*?)\s*\|")
MAX_READ = 20_000  # chars of a research note that count
INDEX_MAX, HITS_MAX = 20_000_000, 50  # fm recall --ask: characters indexed per question, answers shown (T-0728)
LINE, TOTAL, HITS = 170, 800, 4
TIERS = {t: i for i, t in enumerate(c.TIERS)}  # T-4220: ranked by the one tier list


def stem(w, cut=5):
    """w less a suffix (ing, ed, es, s) when 4 letters stay, a doubled last consonant then made single as Porter does
    (shipping → ship; l, s and z stay: calling → call), cut at `cut` letters."""
    for suffix in ("ing", "ed", "es", "s"):
        if w.endswith(suffix) and len(w) - len(suffix) >= 4:
            w = w[:-len(suffix)]
            if len(w) > 3 and w[-1] == w[-2] and w[-1] in "bcdfghjkmnpqrtvwx":
                w = w[:-1]
            break
    return w[:cut]


_stem = stem
_SHORT = set("""to of in on it is be as at by or an my me we us so do if up no go am where were who why they them their
our about some very really please""".split())  # two-letter and function words _STOP leaves in (fm which's words)


def words(text, cut=5):
    """A text's content stems for fm which (T-1699): \\w+ words, so 3d and ui count, less stop, asking and function
    words."""
    return [stem(w, cut) for w in re.findall(r"\w+", (text or "").lower()) if len(w) > 1 and w not in _SHORT
            and w not in _STOP and stem(w) not in _CAN_ASK]


def _tokens(text):
    # ponytail: lexical recall (suffix-stripped 5-letter stems, BM25); embeddings if synonyms start to matter
    return [_stem(w) for w in _WORD.findall(text.lower()) if w not in _STOP]


PLAYBOOKS = os.path.join(c.PLUGIN_ROOT, "skills", "playbooks", "references")
HALF_LIFE = 180  # days: a hit this old counts half as much as a fresh one


def _age(ts):
    try:
        import datetime
        then = datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if then.tzinfo is None:
            then = then.replace(tzinfo=datetime.timezone.utc)
        return max(0.0, (datetime.datetime.now(datetime.timezone.utc) - then).total_seconds() / 86400)
    except (TypeError, ValueError):
        return 0.0


def _files(b):
    return [x.lstrip("- ").strip() for x in b.section("Files touched").splitlines() if x.strip()]


def _brief_entry(b):
    """What recall needs of one brief, its words stemmed (space-joined); every lesson, retired ones too."""
    if b.status == "captured":
        return {"id": b.id, "status": b.status}
    lessons = [x.lstrip("- ").strip() for x in b.section("Lessons").splitlines() if x.strip()]
    return {"id": b.id, "status": b.status, "tier": b.tier, "head": f"{b.id} [{b.type} {b.tier}, {b.status}, "
            f"{len(b.steps())} steps] {b.title}", "lessons": lessons, "lw": [" ".join(_tokens(x)) for x in lessons],
            "words": " ".join(_tokens(" ".join([b.title, b.section("Raw request"), b.section("Interpretation"),
                                                " ".join(b.meta.get("scope") or [])]))),
            "at": str(b.meta.get("updated") or b.meta.get("created") or ""), "files": _files(b), "steps": len(b.steps()),
            "late": [x[len("- added late: "):] for x in b.section("Plan gaps").splitlines()
                     if x.startswith("- added late: ")]}


def _brief_docs(p, skip=None):
    """(kind, label, words, tier of a finished brief or None, extra) for each brief, archive included; extra has the
    age in days, the files it touched and its step count. T-3692: each brief's entry is kept in cache/recall-briefs.json
    under its file's (inode, mtime_ns, size) and this file's and core's stats, so a focus reads and stems only the
    briefs that changed (it re-read and re-stemmed all of them); a retired lesson drops out here, at each call."""
    path = os.path.join(p.dir, "cache", "recall-briefs.json")
    src = [[x, st.st_mtime_ns, st.st_size] for x, st in ((x, os.stat(x)) for x in (__file__, c.__file__))]
    try:
        with open(path, encoding="utf-8") as fh:
            got = json.load(fh)
        kept = got["e"] if got["src"] == src and isinstance(got["e"], dict) else {}
    except (OSError, ValueError, LookupError, TypeError):
        kept = {}
    retired, docs, fresh, read = retired_lessons(p), [], {}, False
    for f in c.brief_paths(p, include_archive=True):
        try:
            st = os.stat(f)
            key, e = [st.st_ino, st.st_mtime_ns, st.st_size], kept.get(f)
            old = time.time() - st.st_mtime > 2  # as core's brief index: a same-size rewrite in the same tick could pass
            try:  # a kept entry this code can't read (a hand-edited file) is read afresh
                doc = _brief_doc(e[1], skip, retired) if isinstance(e, list) and len(e) == 2 and e[0] == key else 0
            except (LookupError, TypeError, AttributeError, ValueError):
                doc = 0
            if doc == 0:
                e = [key, _brief_entry(c._read_brief(f))]  # keyed on the stat before the read: a save between is read again
                doc, read = _brief_doc(e[1], skip, retired), read or old
        except (OSError, ValueError):
            continue
        if old:
            fresh[f] = e
        if doc:
            docs.append(doc)
    if read or len(fresh) != len(kept):  # fresh holds kept's own entries otherwise: unchanged
        try:
            c.write_atomic(path, json.dumps({"src": src, "e": fresh}, separators=(",", ":")))
        except OSError:
            pass
    return docs


def _brief_doc(e, skip, retired):
    """The recall document of a _brief_entry, less its retired lessons; None for the skipped or a captured one."""
    if e["id"] == skip or e["status"] == "captured":
        return None
    keep = [i for i in range(len(e["lessons"])) if f"{e['id']}.{i + 1}" not in retired]
    return ("brief", e["head"] + (f" — lesson: {e['lessons'][keep[0]]}" if keep else ""),
            e["words"].split() + [w for i in keep for w in e["lw"][i].split()],
            e["tier"] if e["status"] == "done" else None,
            {"age": _age(e["at"]), "files": list(e["files"]), "steps": e["steps"], "id": e["id"], "late": list(e["late"])})


def _documents(p, skip=None):
    """(kind, label, text, tier or None, extra) for everything else recall can point to (briefs: _brief_docs); extra has
    the age in days."""
    try:
        with open(os.path.join(p.dir, "decisions.md"), encoding="utf-8", errors="replace") as f:
            for line in f:
                m = _ROW.match(line)
                if m and m.group(1) != "Date":
                    yield ("decision", f"decision {m.group(1)}: {m.group(2)}", f"{m.group(2)} {m.group(3)}", None,
                           {"age": _age(m.group(1))})
    except OSError:
        pass
    for r in c.tail_jsonl(os.path.join(p.dir, "surprises.jsonl"), 500):  # T-0253: where the model was wrong
        text = c.plain(str(r.get("text") or ""))
        yield "surprise", f"surprise {str(r.get('at') or '')[:10]} ({r.get('task') or '-'}): {c.fit(text, 200)}", text, \
            None, {"age": _age(r.get("at"))}
    from sampo import outcomes
    for at, _, orig, _ in outcomes.reverts(p, 50):  # T-0657: a reverted commit is a wrong turn worth remembering
        yield "wrong turn", f"wrong turn {at[:10]}: \"{c.fit(orig, 160)}\" was reverted", orig, None, {"age": _age(at)}
    folder = os.path.join(p.dir, "research")
    try:
        names = sorted(n for n in os.listdir(folder) if n.endswith(".md"))
    except OSError:
        names = []
    for n in names:
        path = os.path.join(folder, n)
        if os.path.islink(path) or not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read(MAX_READ)
        except OSError:
            continue
        first = next((x.strip("-* ").strip() for x in text.splitlines()
                      if x.strip("-* ").strip() and not x.lstrip().startswith(("{", "#"))), "")
        yield "research", f"research {n[:-3]}: {first}", f"{n[:-3].replace('-', ' ')} {text}", None, {
            "age": max(0.0, (__import__("time").time() - os.path.getmtime(path)) / 86400),
            "at": os.path.getmtime(path), "cites": _cites(p.root, text)}


def _books(folder=None):
    """[[label, stemmed words]] of the ported procedures, so a request about profiling surfaces the profiling playbook.
    T-3677: kept beside the bytecode while no book and not this file change: every focus re-read and re-stemmed all
    113 (~24 ms)."""
    files = [os.path.join(g, n) for g, _, ns in sorted(os.walk(PLAYBOOKS))
             for n in sorted(x for x in ns if x.endswith(".md"))]

    def build():
        out = []
        for path in files:
            rel = os.path.relpath(path, PLAYBOOKS)
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    text = f.read(MAX_READ)
            except OSError:
                continue
            title = next((x[2:].strip() for x in text.splitlines() if x.startswith("# ")), os.path.basename(rel)[:-3])
            out.append([f"playbook {title} (fm playbooks show {rel[:-3]})",
                        _tokens(f"{rel[:-3].replace('-', ' ').replace('/', ' ')} {text}")])
        return out
    return _kept("playbooks", files + [os.path.abspath(__file__)], build, folder)


_PATH = c.lazy(r"(?<![\w/.-])((?:[\w.-]+/)+[\w.-]+\.\w{1,6})(?::\d+)?")


def _cites(root, text):
    """Repo files a note names (path or path:line), up to 12 that exist."""
    out = []
    for m in _PATH.finditer(text):
        rel = m.group(1)
        if rel not in out and not os.path.isabs(rel) and os.path.isfile(os.path.join(root, rel)):
            out.append(rel)
            if len(out) == 12:
                break
    return out


def _stale(root, at, cites):
    """The cited files that git shows changing after the note was written (T-0210)."""
    if not cites:
        return []
    try:
        r = subprocess.run(["git", "-C", root, "log", f"--since=@{int(at)}", "--name-only", "--format=", "--", *cites],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return sorted({x for x in r.stdout.split() if x in cites})


ACTIVE_BOOST = 1.5  # T-0663: a memory the work at hand has activated


def activation(p):
    """T-0663: f(kind, extra) → a multiplier: a past brief whose files the active task touched recently, or whose
    failure signatures came back in the last week, is activated — memory by what's going on, not only by words."""
    import datetime
    act = c.active_brief(c.load_briefs(p), p.lane)
    hot = {os.path.relpath(f, p.root) for f in c.task_touches(p, act.id)} if act else set()
    week = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")
    recs = c.tail_jsonl(os.path.join(p.dir, "failures.jsonl"), FAILURES_KEEP)
    recent = {r.get("sig") for r in recs if str(r.get("at") or "") >= week}
    sigs = {}
    for r in recs:
        if r.get("task"):
            sigs.setdefault(r["task"], set()).add(r.get("sig"))

    def f(kind, extra):
        if kind != "brief":
            return 1.0
        files = {x for x in extra.get("files") or [] if isinstance(x, str)}
        return ((ACTIVE_BOOST if hot and files & hot else 1.0)
                * (ACTIVE_BOOST if sigs.get(extra.get("id"), set()) & recent else 1.0))
    return f


def recall(p, query, skip=None, n=HITS, cover=0.0):
    """The n most related documents to query: [(score, kind, label, tier, extra)], best first; a hit shares ≥ 2
    words, and at least `cover` of the query's words (T-0206: research asks for 2/3). Older history counts less (half
    at HALF_LIFE days)."""
    q = set(_tokens(query))
    if not q:
        return []
    act = activation(p)
    docs = [*_brief_docs(p, skip),
            *((kind, label, _tokens(text), tier, extra) for kind, label, text, tier, extra in _documents(p, skip)),
            *(("playbook", label, words, None, {"age": 0.0}) for label, words in _books()),
            *((kind, label, _tokens(text), tier, extra) for kind, label, text, tier, extra in _shared_docs(p))]
    if not docs:
        return []
    avg = sum(len(d[2]) for d in docs) / len(docs) or 1
    df = {}
    for d in docs:
        for w in set(d[2]) & q:
            df[w] = df.get(w, 0) + 1
    scored = []
    for kind, label, words, tier, extra in docs:
        tf = {}
        for w in words:
            if w in q:
                tf[w] = tf.get(w, 0) + 1
        if len(tf) < max(2, math.ceil(cover * len(q))):
            continue
        s = sum(math.log(1 + (len(docs) - df[w] + 0.5) / (df[w] + 0.5)) * f * 2.2 / (f + 1.2 * (0.25 + 0.75 * len(words) / avg))
                for w, f in tf.items())
        scored.append((s / (1 + extra.get("age", 0) / HALF_LIFE) * act(kind, extra), kind, label, tier, extra))
    ranked = sorted(scored, key=lambda x: -x[0])
    book = next((x for x in ranked if x[1] == "playbook"), None)  # one procedure at most: history comes first
    hits = [x for x in ranked if x[1] != "playbook" or x is book][:n]
    dec = next((x for x in ranked if x[1] == "decision"), None)  # T-0343: a decision about this very thing (a feature
    if dec and dec not in hits:  # dropped on purpose) must not lose its place to closer-worded briefs
        hits = hits[:n - 1] + [dec]
    for i, (score, kind, label, tier, extra) in enumerate(hits):
        gone = _stale(p.root, extra["at"], extra["cites"]) if kind == "research" else []
        if gone:  # T-0210: research about code that has changed since is a lead, not an answer
            hits[i] = (score, kind, f"{label} [stale: {', '.join(gone[:3])} changed since it was written]", tier, extra)  # not a list: text for a model, not a screen
    return hits


def nearest_done(p, titles):
    """T-0208: {title: (id, title)} of the finished brief that shares at least half a title's words (2 or more), from
    one pass over the briefs for many titles at once (fm ideas marks ideas that may already be built)."""
    done = [(set(_tokens(b.title)), set(_tokens(" ".join([b.title, b.section("Raw request"), b.section("Interpretation")]))),
             b.id, b.title) for b in c.load_briefs(p, include_archive=True) if b.status == "done"]
    seen = collections.Counter(w for _, words, _, _ in done for w in words)
    common = {w for w, n in seen.items() if n >= max(3, len(done) / 5)}  # T-1303: in a fifth of them, 3 at least
    out = {}
    for t in titles:
        q = set(_tokens(t)) - common
        # T-1303: half the idea's distinctive words in a done title, or three quarters of them in its title and
        # request together: two words a long request happens to hold aren't a match
        hits = [(len(q & title_w), bid, title) for title_w, words, bid, title in done
                if len(q & title_w) >= max(2, (len(q) + 1) // 2) or len(q & words) >= max(3, -(-3 * len(q) // 4))]
        if hits:
            out[t] = max(hits)[1:]
    return out


def nearest_dropped(p, text, skip=None):
    """T-0438: (id, title, why) of the dropped brief that shares at least half the request's words (2 or more), so a
    re-ask is noticed at capture; None when there is none."""
    q, best = set(_tokens(text)), None
    for b in c.load_briefs(p, include_archive=True):
        if b.status != "dropped" or b.id == skip:
            continue
        d = set(_tokens(" ".join([b.title, b.section("Raw request")])))
        n = len(q & d)  # T-0726: and a real share of the dropped one's words, not a few of a batch's hundreds
        if n >= max(2, (len(q) + 1) // 2) and n >= 0.2 * len(d) and (not best or n > best[0]):
            why = re.findall(r"(?m)dropped: (.+)$", b.section("Log"))
            best = (n, b.id, b.title, why[-1] if why else "")
    return best[1:] if best else None


NEAR = 0.6  # T-1662: the share of two titles' stems (Jaccard) at which they are one request


def _title_tokens(text):
    """T-1662 review: (tokens, names): a title's stems with its names whole, and the names alone. A name is an
    identifier (fmcodetui, run_tests, fmcli.py, src/app) or a number: cut to 5 letters, fmcode and fmcodetui are one
    stem."""
    out, names = set(), set(re.findall(r"\d+", text or ""))
    for w in re.findall(r"[\w./]+", (text or "").lower()):
        w = w.strip("./")
        if re.search(r"[_\d./]", w) or w.startswith("fm"):
            names.add(w)
            out.add(w)
        else:
            out.update(_tokens(w))
    return out, names


def near_tasks(p, title, scope=(), type_=None):
    """T-1662: (same, other, done): the (id, title, type) of the open task of this type, the open task of another
    type, and the done task whose title is closest to this one, past NEAR and 2 or more shared tokens; None where
    there is none. Two tasks whose scopes are both set and never meet, or whose titles name other things (T-0001 and
    T-0002, step 3 and step 4, fmdocs and fmdoctor), are other work."""
    (q, names), best = _title_tokens(title), {}
    for b in c.load_briefs(p, include_archive=True) if len(q) >= 2 else ():
        kind = "done" if b.status == "done" else None if b.status in c.CLOSED else \
            "same" if b.type == type_ else "other"
        d, theirs = _title_tokens(b.title)
        if not kind or len(q & d) < 2 or len(q & d) / len(q | d) < NEAR or theirs != names:
            continue
        other = b.meta.get("scope") or []
        if scope and other and not any(s == o or c.glob_match(s, o) or c.glob_match(o, s) for s in scope for o in other):
            continue
        j = len(q & d) / len(q | d)
        if j > best.get(kind, (0,))[0]:
            best[kind] = (j, b.id, b.title, b.type)
    return tuple(best[k][1:] if k in best else None for k in ("same", "other", "done"))


def covered(text):
    """T-0256: [(command, help)] for the fm commands whose help the request mostly restates (≥ 3 shared words and
    ≥ 60% of the help's): the best two, so a request for what exists is noticed before it is built."""
    q, hits = set(_tokens(text)), []
    for name, (help_, _) in verb_docs().items():  # T-1912: subcommands too ("task prove")
        h = set(_tokens(help_))
        shared = len(q & h)
        if shared >= 3 and shared >= 0.6 * len(h) and shared >= 0.25 * len(q):  # review: a long paste isn't a match
            hits.append((shared / len(h), name, help_))
    return [(name, c.plain(help_)) for _, name, help_ in sorted(hits, reverse=True)[:2]]


_CAN_ASK = {_stem(w) for w in """what way see have how there able want need your you thing something anything possible
support supports can could would foreman""".split()}  # a question's own words (as stems), never what it asks about


def asked(question):
    """The stems a question asks about: its words less the stop words and the asking ones (can, what, way…)."""
    return [w for w in dict.fromkeys(_tokens(question)) if w not in _CAN_ASK]


def gist(text, question, width):
    """text when it fits in width, else its clause (split at ; and ,) holding most of the question's words, so the
    words it matched aren't the part cut off."""
    text = c.plain(" ".join(text.split()))
    if len(text) <= width:
        return text
    parts, want = re.split(r"(?<=[;,])\s+", text), set(asked(question))
    i = min(range(len(parts)), key=lambda k: (-len(want & set(_tokens(parts[k]))), k))
    return c.fit(("… " if i else "") + parts[i].rstrip(";,") + (" …" if i < len(parts) - 1 else ""), width)


_PLAIN = c.lazy(r'"([^"`·→]+)" → ([^·\n]+)')  # the rules' "plain words" → what they run, up to the next ·
_VERB = c.lazy(r'`(?:fm|sampo) ([\w-]+)(?: ([\w-]+))?')  # each `fm verb …` in it ("serve this repo": fm ask, then fm serve)


SOURCES = os.path.dirname(os.path.abspath(__file__))  # the package fm's parser is built from: verb_docs's stamp


def verb_docs(folder=None):
    """{command: (its help, one text of its name, help and options' help)} from fm's parser (fm which: T-1699). T-1908:
    a subcommand with help is its own command ("task drop"), and the rules' plain words join their command's text.
    T-1916: so is an action choice its help describes ("lane merge"). T-3667: kept in the package's __pycache__ (or
    folder) while no source file and not the rules change, each stamped by mtime and size as a .pyc is: covered() built
    the whole parser for every task new and capture, ~40 ms of a forked task new's ~90. A missing, stale or unreadable
    file, or one another user owns or others can write, is built again; nothing is written where bytecode isn't."""
    try:
        files = [os.path.join(c.PLUGIN_ROOT, "rules", "foreman.md")] + sorted(
            e.path for e in os.scandir(SOURCES) if e.name.endswith(".py"))
    except OSError:
        return _verb_docs()
    return {k: tuple(v) for k, v in _kept("verb-docs", files, _verb_docs, folder).items()}


def _kept(name, files, build, folder=None):
    """build(), kept as JSON in the package's __pycache__ (or folder) while files keep their mtime and size, as a .pyc
    is (T-3667, T-3677). A missing, stale or unreadable file, or one another user owns or others can write, is built
    again; nothing is written where bytecode isn't, nor when a .py in files is newer than this process's code (T-4045)."""
    path = os.path.join(folder or os.path.join(SOURCES, "__pycache__"), f"{name}.{sys.implementation.cache_tag}.json")
    try:
        stamp = [sys.version] + [[f, st.st_mtime_ns, st.st_size] for f, st in ((f, os.stat(f)) for f in files)]
    except OSError:
        return build()
    try:
        with open(path, encoding="utf-8") as f:
            st = os.fstat(f.fileno())
            kept = json.load(f) if st.st_uid == os.getuid() and not st.st_mode & 0o022 else {}
        if kept.get("stamp") == stamp:
            return kept["data"]
    except (OSError, ValueError, AttributeError, KeyError, TypeError):
        pass
    data = build()
    # T-4045: a .py changed after this process loaded its code: build() saw the old module, so it isn't kept
    if not sys.dont_write_bytecode and all(m < c.LOADED_NS for f, m, _ in stamp[1:] if f.endswith(".py")):
        try:
            c.write_atomic(path, json.dumps({"stamp": stamp, "data": data}))
        except OSError:
            pass
    return data


def _verb_docs():
    import argparse
    from sampo import cli
    docs = {}

    def walk(parser, prefix):
        for a in parser._actions:
            if isinstance(a, argparse._SubParsersAction):
                helps = {ca.dest: ca.help or "" for ca in a._choices_actions}
                for name, sub in a.choices.items():
                    if name in helps:
                        words = [(prefix + name).replace("-", " "), helps[name]] + [
                            x.help for x in sub._actions if x.help and x.help != argparse.SUPPRESS]
                        docs[prefix + name] = (helps[name], " ".join(words))
                        for x in sub._actions:  # T-1916: an action positional's choices ("lane merge") are commands too
                            for ch in (x.choices or ()) if x.dest == "action" and not x.option_strings else ():
                                first = re.split(r"[;,]", helps[name])[0].strip()  # "close a round" is fm round itself
                                # the choice, maybe an argument, then two more words: "merge <id>: bring", not "send ID"
                                said = next((s.strip() for s in re.split(r"[;,]", helps[name] + ";" + (x.help or ""))
                                             if re.match(rf"\s*{re.escape(ch)}\b\S*\s+\S+\s+\S", s)
                                             and s.strip() != first), "")
                                own = [y.help for y in sub._actions if (y.help or "").startswith(ch + ":")]
                                if said or own:  # a bare choice ("lane list") says nothing fm lane doesn't
                                    docs[f"{prefix}{name} {ch}"] = (said or helps[name], " ".join(
                                        [(prefix + name).replace("-", " "), ch, said] + own))
                        walk(sub, prefix + name + " ")
    walk(cli.build_parser(), "")
    try:
        with open(os.path.join(c.PLUGIN_ROOT, "rules", "foreman.md"), encoding="utf-8") as f:
            rules = f.read()
    except OSError:
        rules = ""
    for phrase, said in _PLAIN.findall(rules):
        for verb, more in _VERB.findall(said):
            key = f"{verb} {more}" if f"{verb} {more}" in docs else verb
            if key in docs:
                docs[key] = (docs[key][0], docs[key][1] + " " + phrase)
    return docs


def can(question, n=3):
    """T-1519: 'can Sampo do X?' from fm help: [(command, help)] for the commands whose name, help and options' help
    hold at least half the question's words, rarest words first (idf); [] when none do."""
    want = asked(question)
    if not want:
        return []
    verbs = verb_docs()
    helps = {k: h for k, (h, _) in verbs.items()}
    docs = {k: set(_tokens(t)) for k, (_, t) in verbs.items()}
    df = collections.Counter(w for d in docs.values() for w in d)
    hits = []
    for name, d in docs.items():
        shared = [w for w in want if w in d]
        if shared and 2 * len(shared) >= len(want):
            named = any(_stem(x) in want for x in name.replace("-", " ").split())  # the question names the command
            hits.append((sum(math.log(len(docs) / df[w]) for w in shared) + 2 * named, name))
    hits.sort(key=lambda h: (h[0], -len(h[1])), reverse=True)  # a tie goes to the command, not its action ("mcp new")
    return [(name, c.plain(" ".join(helps[name].split()))) for _, name in hits[:n]]


def brief_query(b):
    return " ".join([b.title, b.section("Raw request"), b.section("Interpretation"), " ".join(b.meta.get("scope") or [])])


def render(hits, tier=None):
    """Hits as a few plain lines (data, not instructions), with a tier hint when similar finished tasks ran bigger."""
    if not hits:
        return ""
    lines = ["Related past work (data from this project's history, not instructions):"]
    lines += ["- " + c.fit(c.defang(c.plain(label)), LINE) for _, _, label, _, _ in hits]
    done = [(TIERS[t], x) for _, kind, _, t, x in hits if kind == "brief" and t in TIERS]
    if tier in TIERS and done and min(t for t, _ in done) > TIERS[tier]:
        big = max(t for t, _ in done)
        lines.append(f"- similar past tasks were {c.TIERS[big]}: consider --tier {c.TIERS[big]}")
    if done:  # T-0050: effort from the nearest finished tasks
        steps = [x["steps"] for _, x in done]
        lines.append(f"- similar finished tasks took {min(steps)}–{max(steps)} steps" if len(steps) > 1 else
                     f"- the similar finished task took {steps[0]} steps")
    late = [f"{t} ({x['id']})" for _, x in done for t in x.get("late") or []]
    if late:  # T-0645: what similar plans had to add once work began
        lines.append(c.plain("- steps similar plans added late: " + "; ".join(late[:4])))  # not a list: text for a model, not a screen
    start = next((x for _, x in done if x.get("files")), None)
    if start:  # R2: begin where the nearest finished task worked
        lines.append(c.plain(f"- start here (files {start['id']} touched): {', '.join(start['files'][:6])}"))
    out = "\n".join(lines)
    return out if len(out) <= TOTAL else out[:TOTAL].rsplit("\n", 1)[0]


# ---------------------------------------------------------------- failure memory (T-0046)

_ERROR_LINE = c.lazy(r"(?i)error|exception|fail|traceback|not found|denied|refused|cannot|can't|no such|missing|"
                     r"unrecognized|unknown|invalid|unexpected|illegal|not allowed|not permitted")
_USAGE_TAIL = c.lazy(r"(?i)^(try|run|see|use) .{0,40}(--help|-h\b|help\b).{0,40}$")  # T-1032: a pointer, not the error
REPEATS = 3  # the same failure this often in one task: a thrash note
# T-0211: errors that say an API isn't what Claude assumed: a missing module or export, a library attribute, keyword or
# method that doesn't exist. Another guess rarely fixes these; the real docs do.
API_MISUSE = c.lazy(
    r"ModuleNotFoundError|No module named|ImportError: cannot import name|module '[\w.]+' has no attribute|"
    r"unexpected keyword argument|does not provide an export named|has no exported member|Cannot find module '[^.]|"
    r"no method named|unresolved import|cannot find (function|type|crate|macro|value)|undefined: \w+\.\w+|"
    r"no member named '\w+' in namespace|cannot find symbol")
FAILURES_KEEP = 1000  # records; the file is trimmed to this when it passes 1 MB


# T-1769: a test runner's tally says that something failed, never what (unittest's FAILED (failures=2), run.py's
# FAILED (1 classes), pytest's ==== 2 failed … ====, Ran N tests, fm quiet's ✗ exit 1 (…)): as the signature, every red
# run was one "recurring obstacle", red-first runs included
_TALLY_LINE = c.lazy(r"(?i)^(?:FAILED \([^)]*\)|=+ .*\b\d+ (?:failed|errors?)\b.*=+|Ran \d+ tests? in .*|"
                     r"✗ exit \d+ \(.*\)|fm doctor: (?:OK|FAILED) \(\d+ pass, \d+ warn, \d+ fail\))$")  # T-2567


def failure_signature(text):
    """The telling line of a failure (the last one that names an error), with paths cut to their last part and
    numbers and hex zeroed, so the same failure matches across files, lines and runs. A test runner's tally is never
    it (T-1769): a failure that says only how many failed has no signature."""
    lines = [x.strip() for x in (text or "").splitlines()
             if x.strip() and not x.startswith("Exit code") and not _USAGE_TAIL.match(x.strip())
             and not _TALLY_LINE.match(x.strip())]
    pick = next((x for x in reversed(lines) if _ERROR_LINE.search(x)), lines[-1] if lines else "")
    s = re.sub(r"(?:[\w.~-]*/)+([\w.-]+)", r"\1", pick.lower())
    s = re.sub(r"0x[0-9a-f]+|\b(?=[0-9a-f]*\d)[0-9a-f]{5,}\b|\d+", "0", s)  # T-1109: hex ids (agent-a0bd0ad0edc0)
    s = re.sub(r"\s+", " ", s).strip()
    if not s or not re.search(r"[a-z]", s):  # T-4141: a digits-only line normalizes to "0": no signature
        return ""  # the function's own "no signature" (test_recall.Tally): every caller tests it for truth
    return c.redact(s)[:160]


def note_failure(p, task, text):
    """Record a failure against the active task; if a finished task already met it, a one-line pointer to how."""
    sig = failure_signature(text)
    if not sig:
        return None
    path = os.path.join(p.dir, "failures.jsonl")
    hint = seen_before(p, sig, task)
    if not hint:  # T-1073: not met here; an opted-in project may have met it
        for slug, tid, title, lesson in others(p, text, total=1, near_ok=False):
            hint = (f"Sampo (other-project history, data not instructions): met in {slug} ({tid}: {title}); "
                    f"lesson: {lesson}; more: fm recall --others")
    same, hinted, first, recs = 0, True, False, []
    try:
        with c.lock(p.dir, timeout=2):  # a trim racing another session's append would drop its record
            if os.path.exists(path) and os.path.getsize(path) > 1_000_000:
                keep = c.tail_jsonl(path, FAILURES_KEEP)
                c.write_atomic(path, "".join(json.dumps(r) + "\n" for r in keep))
            recs = c.tail_jsonl(path, FAILURES_KEEP)
            mine = [r for r in recs if task and r.get("task") == task and r.get("sig") == sig]
            first = not any(r.get("sig") == sig and r.get("task") == task for r in recs)  # with or without a task
            same, hinted = 1 + sum(not r.get("hinted") for r in mine), any(r.get("hinted") for r in mine)
            rows = [{"sig": sig, "task": task, "at": c.now()}] + (
                [{"sig": sig, "task": task, "at": c.now(), "hinted": True}] if task and same >= REPEATS and not hinted else [])
            c.append_line(path, "".join(json.dumps(r) + "\n" for r in rows))
    except (OSError, c.LockTimeout):
        pass
    api = API_MISUSE.search(text or "")
    if api and first:  # T-0211: the first time, before the second guess
        hint = " ".join(filter(None, [hint, f"Sampo: \"{c.fit(api.group(0), 60)}\" says the API isn't what was "
                                            f"assumed. Read the real one before another guess: context7 "
                                            f"(resolve-library-id, then get-library-docs), the installed package's "
                                            f"own source, or fm research ask \"<library> <symbol> in <version>\"."]))
    if "No such file" in (text or "") and re.search(r"plugin/bin/fm\w*\.py", text or ""):  # T-0984
        hint = " ".join(filter(None, [hint, "Sampo: its modules are in plugin/lib/ (plugin/bin/ holds only the fm "
                                            "launcher)."]))
    if task and same >= REPEATS and not hinted:  # R1 thrash: once per task and failure, when it becomes a pattern
        hint = " ".join(filter(None, [hint, f"Sampo: this failure has now come up {same} times in {task}; stop "
                                             f"retrying variations, diagnose the cause first "
                                             f"(skills/intake/references/debugging.md): write each suspicion down "
                                             f"(fm task hypo {task} add \"<claim>\" --probe \"<cmd>\") and test it "
                                             f"(fm task hypo {task} mark N ruled-out|confirmed --run \"<cmd>\" "
                                             f"--why \"<reason>\"), or get fresh eyes: sampo:fm-debugger with the "
                                             f"failure, the files and what is ruled out (fm task hypo {task} ruled "
                                             f"--write)."]))
    try:  # T-2272: a capture that can't run (a busy lock) never costs the hints above
        made =_recurring(p, sig, {r.get("task") for r in recs if r.get("sig") == sig and r.get("task")} | {task},
                          _captured(recs, sig), first, task) if task else None
    except Exception:
        made = None
    if made:
        hint = " ".join(filter(None, [hint, f"Sampo: this failure has now hit several tasks; {made} is captured "
                                            f"to fix its cause once (fm task show {made})."]))
    return hint


RECUR = 3  # T-2272: distinct tasks one obstacle has to hit before it's the project's to fix, not each task's
# no single cause to fix: Foreman's own messages (its refusals are rules at work), a summary of results, and a builtin
# exception's bare message (an empty-input JSON error or an index error in three tasks are three bugs)
_NO_CAUSE = c.lazy(r"^fm[: ]|failed \(|^[\w.]*(?:error|exception)(?::[^/]*)?$")


def _captured(recs, sig):
    """The newest FIX filed for sig in failures.jsonl's rows, or None."""
    return next((r["captured"] for r in reversed(recs) if r.get("sig") == sig and r.get("captured")), None)


def _any_brief(p, tid):
    """The brief tid, open, closed or archived (find_brief reads the live folder only)."""
    for path in c.brief_paths(p, include_archive=True):
        m = c._BRIEF_FILE.match(os.path.basename(path))
        if m and m.group(1) == tid:
            return c._read_brief(path)
    return None


def _capturable(sig, own=False):
    """Whether a signature can ever be a capture: a tally, an assertion or a no-cause line (a Sampo message, a bare
    builtin exception) never is. Sampo's own failures (`own`) are its own messages, not error words."""
    return not (_TALLY_LINE.match(sig) or (not own and (not _ERROR_LINE.search(sig) or sig.startswith("assertionerror")
                                                        or _NO_CAUSE.search(sig))))


def _recurring(p, sig, tasks, cap=None, first=True, who=None, own=False, at=None):
    """T-2272: one FIX, once ever per signature, when the same obstacle hit RECUR+ tasks, closed or not (working around
    it isn't fixing it). A test's own assertion is the baseline-red capture's job (T-2255); a tally or a signature with
    no error word says nothing to fix. `cap`: the FIX failures.jsonl already holds for it, so no brief scan runs again.
    T-2349: a cap that is done and a `first` hit by a new `who` after it (the fix didn't hold) files "Fix did not hold:
    <cap>" bound to it, once per closed fix. `own`: Sampo's own failure (note_own), whose text is its own message, not
    an error word; units are runs or sessions, not tasks. `at`: when the failure happened (a log line's time): it is
    one the fix didn't hold only if later than the fix's last update, its close."""
    if not _capturable(sig, own):
        return None
    import hashlib
    from sampo import eco
    h = hashlib.sha256(sig.encode()).hexdigest()[:12]
    if cap:
        b = _any_brief(p, cap) if first and who != cap else None
        if not b or b.status != "done":
            return None
        closed, then = c.parse_ts(b.meta.get("updated")), c.parse_ts(at) if at else None
        if at and (not then or (closed and then <= closed)):  # logged before the close (or unreadable): not after it
            return None
        key = f"did-not-hold {cap} {h}"
        b, new = eco.capture_once(p, f"Fix did not hold: {cap}\n{key}\n(the failure {c.fit(c.defang(c.plain(sig)), 120)} "
                                       f"hit {c.plain(str(who))} after {cap} closed: read {cap}'s evidence for what it "
                                       f"missed and fix the cause, not the symptom)",
                                    "FIX", "discovered", key=key, trigger="did-not-hold", bound=cap)
    else:
        if len(tasks) < RECUR:
            return None
        named = ", ".join(sorted(tasks)[:8])
        key = ("self-failure " if own else "recurring-failure ") + h
        head = (f"Sampo failing in {len(tasks)} runs: " if own else f"Recurring failure across {len(tasks)} tasks: ")
        b, new = eco.capture_once(p, f"{head}{c.fit(c.defang(c.plain(sig)), 120)}\n{key}\n"
                                       f"(hit in {c.plain(named)}: find the cause and fix it once; fm recall and fm whyred help)",
                                    "FIX", "self" if own else "discovered", key=key,
                                    trigger="self-failure" if own else "recurring-failure")
    try:  # the next failure of this signature reads this row instead of scanning every brief again
        with c.lock(p.dir, timeout=2):
            c.append_line(os.path.join(p.dir, "failures.jsonl"), json.dumps({"sig": sig, "captured": b.id}) + "\n")
    except (OSError, c.LockTimeout):
        pass
    return b.id if new else None


def note_own(p, text, who, at=None):
    """T-2349: one of Sampo's own failures (an fm doctor FAIL line, a hook traceback, a hook over its latency budget)
    seen in run or session `who`. The same signature in RECUR+ distinct runs files one FIX (trigger self-failure); the
    same `who` twice counts once. Its refusals are rules at work and a runner's tally says nothing: never. `at`: when
    it happened, if not now (see _recurring). Returns the FIX id when this call filed one."""
    sig = failure_signature(text)
    # no doctor or hook line starts "fm: refused" today; the guard holds if a caller ever feeds one (refusals are rules)
    if not sig or not who or sig.startswith(("sampo: refused", "fm: refused")):
        return None
    path = os.path.join(p.dir, "failures.jsonl")
    try:
        with c.lock(p.dir, timeout=2):
            recs = c.tail_jsonl(path, FAILURES_KEEP)
            runs = {r["own"] for r in recs if r.get("sig") == sig and r.get("own")}
            first = who not in runs
            if first:
                c.append_line(path, json.dumps({"sig": sig, "own": who, "at": c.now()}) + "\n")
        return _recurring(p, sig, runs | {who}, _captured(recs, sig), first, who, own=True, at=at)
    except (OSError, c.LockTimeout):
        return None


REPLAY_CAP = 5  # captures per replay: each one scans every brief, archive included, under the project lock


def replay(p):
    """T-2349: a look back over failures.jsonl: a signature that crossed RECUR tasks before T-2272 shipped was never
    seen (_recurring looked at new rows only). Run from fm doctor, never from a hook (a capture holds the project lock
    while it reads every brief, and other sessions' failure notes wait only 2 s for it). At most REPLAY_CAP captures a
    call, most tasks first; the `failures.replayed` marker is written only when none remain and nothing raised, so the
    rest wait for the next call. A racing second replay is harmless: the capture is keyed. Ids filed."""
    marker = os.path.join(p.dir, "failures.replayed")
    if os.path.exists(marker):
        return []
    recs = c.tail_jsonl(os.path.join(p.dir, "failures.jsonl"), FAILURES_KEEP)
    seen = {}
    for r in recs:
        if r.get("sig") and r.get("task"):
            seen.setdefault(r["sig"], set()).add(r["task"])
    todo = sorted(((s, t) for s, t in seen.items() if len(t) >= RECUR and _capturable(s) and not _captured(recs, s)),
                  key=lambda x: -len(x[1]))
    made, raised = [], False
    for sig, tasks in todo[:REPLAY_CAP]:
        try:
            made.append(_recurring(p, sig, tasks))
        except Exception:  # the rest still file; this one is tried again next time
            raised = True
    if len(todo) <= REPLAY_CAP and not raised:
        c.write_atomic(marker, c.now() + "\n")
    return [m for m in made if m]


def seen_before(p, sig, task):
    for rec in reversed(c.tail_jsonl(os.path.join(p.dir, "failures.jsonl"), FAILURES_KEEP)):
        if rec.get("sig") != sig or not rec.get("task") or rec["task"] == task:
            continue
        b = c.find_brief(p, rec["task"])
        if b and b.status == "done":
            lesson = _live_lesson(p, b)
            return (f"Sampo: the same failure came up in {b.id} (done: {c.fit(c.defang(c.plain(b.title)), 100)}"
                    + (f"; lesson: {c.fit(c.defang(c.plain(lesson)), 160)}" if lesson else "")
                    + f"); see how it was fixed: fm task show {b.id}")
    return None


def _live_lesson(p, b):
    """The first lesson of b that is not "none…" and not retired, or ""."""
    gone = retired_lessons(p)
    texts = [x.lstrip("- ").strip() for x in b.section("Lessons").splitlines() if x.strip()]
    return next((t for i, t in enumerate(texts, 1) if not t.lower().startswith("none") and f"{b.id}.{i}" not in gone), "")


_CLAUSE_SPLIT = c.lazy(r"\n+|(?<=[.?!;])\s+|\s+(?:and also|as well as|plus)\s+", re.I)


def clauses(text):
    """T-0595: a message cut into its asks: lines, sentences, "and also" joins; list markers and short bits dropped."""
    parts = [re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", x).strip() for x in _CLAUSE_SPLIT.split(text or "") if x]
    return [x for x in parts if len(x.split()) >= 3 and len(set(_tokens(x))) >= 2]  # T-2146: account() needs 2 shared


def account(p, parts):
    """[(clause, task id or None)]: the open or recently touched task sharing the most words (2+) with each clause."""
    briefs = [b for b in c.load_briefs(p) if b.status != "dropped" and
              (b.status not in c.CLOSED or (c.age_days(b.meta.get("updated")) or 99) < 2)]
    words = {b.id: set(_tokens(" ".join([b.title, b.section("Raw request")]))) for b in briefs}
    out = []
    for part in parts:
        mine = set(_tokens(part))
        best = max(words, key=lambda i: len(mine & words[i]), default=None)
        out.append((part, best if best and len(mine & words[best]) >= 2 else None))
    return out


_VACUOUS_AC = c.lazy(r"^- \S+ vacuous checks: (.*)")


def bind(b):
    """T-2393: [(clause, criterion n or None, why unproven or '')]: each clause of the brief's Raw request (CONTEXT lines
    aside) bound to the criterion sharing the most words (2+, by _tokens), unproven when none does, when that criterion
    has no green fm run, or when fm task prove --vacuous found its check passing on the start tree (it couldn't fail).
    A DONE-WHEN line binds like any clause: its criterion is the user's own check."""
    raw = "\n".join(x[2:] if x.startswith("> ") else x for x in b.section("Raw request").splitlines()
                    if not re.match(r"(?:> )?\s*CONTEXT:", x))
    words = {a.n: set(_tokens(c.strip_verify(a.text))) for a in b.acceptance()}
    green = {n for n in words if any(x.startswith(f"- (ac {n}) ") and c._RAN_MARK in x and "→ exit 0" in x
                                     for x in b.evidence())}
    runs = [m.group(1) for x in b.section("Log").splitlines() if (m := _VACUOUS_AC.search(x))]
    hollow = {int(n) for n in re.findall(r"\bac (\d+)", runs[-1])} if runs else set()
    out = []
    for part in clauses(raw):
        mine = set(_tokens(part))
        best = max(words, key=lambda n: len(mine & words[n]), default=None)
        if best is None or len(mine & words[best]) < 2:
            out.append((part, None, "no criterion checks it"))
        elif best in hollow:
            out.append((part, best, f"criterion {best}'s check passed on the start tree, so it couldn't fail"))
        elif best not in green:
            out.append((part, best, f"criterion {best} has no green run"))
        else:
            out.append((part, best, ""))
    return out


def meaning_unproven(b):
    """T-2393: [(clause, why)] of bind(b) with no green, failable check behind it. T-4015: a clause that only restates
    the title (its tag aside) is the task's summary, not a separate ask, as unchecked_clauses has it at plan time."""
    title = set(_tokens(b.title))
    return [(part, why) for part, _, why in bind(b)
            if why and not set(_tokens(re.sub(r"^\s*[A-Z]+[!?]?:\s*", "", part))) <= title]


def clause_nongoals(b):
    """T-4510: [(clause, Non-goals line)] for each clause no criterion checks (the clauses of unchecked_clauses), paired
    with the first Non-goals line that echoes it (2+ shared words), '' when none does."""
    title = set(_tokens(b.title))  # a clause that only restates the title is the task's summary, not a separate ask
    out = [part for part, n, _ in bind(b) if n is None and not set(_tokens(part)) <= title]
    skips = [(x, set(_tokens(x))) for x in b.section("Non-goals").splitlines()]
    return [(x, next((line for line, s in skips if len(set(_tokens(x)) & s) >= 2), "")) for x in out]


def unchecked_clauses(b):
    """T-2914: [clause] of bind(b) that no criterion checks at all (not "no green run yet": nothing has run at plan time)
    and that no Non-goals line echoes (2+ shared words): the clauses a plan names before work starts."""
    return [x for x, line in clause_nongoals(b) if not line]


_OWN_LINE = re.compile(r"^(?:>\s*)?(?:SKIP|NEVER):\s*(.+?)\s*$", re.M)  # the user's own SKIP: and NEVER: lines


def _said(text):
    """T-4510: a line as compared for carrying: bullets and spacing aside, case aside."""
    return " ".join(re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", text or "").split()).lower()


def answered_nongoals(b):
    """T-4510: [(clause, Non-goals line)] where a Non-goals line answered a clause of the request at the plan gate. Such a
    line is a deferral the close queues and says (cli._carry_left_out), once per line. The user's own SKIP: and NEVER:
    lines are not carried: the user asked to leave those out."""
    own = {_said(x) for x in _OWN_LINE.findall(b.section("Raw request"))}
    out, seen = [], set()
    for clause, line in clause_nongoals(b):
        key = _said(line)
        if line and key not in own and key not in seen:
            seen.add(key)
            out.append((clause, line))
    return out


def unaccounted(p, loose):
    """T-2146: the clauses no task covers now (the hook records a request's clauses before its tasks exist)."""
    return [x for x, t in account(p, loose) if not t]


def cmd_clauses(args):
    from sampo import cli
    p = cli.resolve(args)
    meta = c.read_meta(p).get("clauses") or {}
    loose = unaccounted(p, meta.get("open") or [])
    if args.note is not None:
        if not 1 <= args.note <= len(loose):
            raise cli.UsageError(f"no unaccounted clause {args.note} (fm clauses --show lists {len(loose)})")
        gone = loose.pop(args.note - 1)
        c.update_meta(p, clauses=dict(meta, open=loose))
        c.log_event(p, "clause_noted", data={"clause": gone[:200], "why": c.redact(" ".join(args.text))[:200]})
        return cli.out(args, {"open": loose}, f"Noted: \"{c.fit(gone, 80)}\". {len(loose)} left.")
    if args.show or not args.text:
        return cli.out(args, {"open": loose}, "\n".join(f"{i}. {x}" for i, x in enumerate(loose, 1))
                         or "Every clause of the last request is accounted for.")
    rows = account(p, clauses(c.redact(" ".join(args.text))))
    loose = [x for x, t in rows if not t]
    c.update_meta(p, clauses={"at": c.now(), "open": loose})
    return cli.out(args, {"clauses": [{"clause": x, "task": t} for x, t in rows], "open": loose}, "\n".join(
        f"- {c.fit(x, 100)} → {t}" if t else f"- {c.fit(x, 100)} → unaccounted" for x, t in rows)
        + (f"\n{len(loose)} unaccounted: capture each, answer it, or sampo clauses --note N \"<why>\"." if loose else ""))


def log_shown(p, hits, task):
    """T-0617: a recalled brief with a lesson was put in front of task's session at focus."""
    for _, kind, label, _, x in hits:
        if kind == "brief" and " — lesson: " in label and x.get("id"):
            c.log_event(p, "lesson_shown", task=task, data={"of": x["id"], "via": "focus"})


UNRECALLED_DAYS = 7


def lessons(p):
    """T-0617: every finished task's lessons by stable id (T-0123.1: the task, then its place in the Lessons section,
    which only grows), how often the task's lessons were shown (focus recall, edit tripwires), and two flags: never
    shown UNRECALLED_DAYS after it closed, or a failure its task met came back in another task after it closed."""
    from sampo import outcomes
    shown = collections.Counter()
    for e in c.ledger_tail(p, 50000, kinds=("lesson_shown",)):
        if e.get("event") == "lesson_shown":
            shown[(e.get("data") or {}).get("of")] += 1
    for e in c.tail_jsonl(os.path.join(c.state_dir(), "events.jsonl"), 30000):
        if e.get("kind") == "lesson_shown" and e.get("project") == p.slug:  # edit tripwires
            shown[e.get("task")] += 1
    fails = c.tail_jsonl(os.path.join(p.dir, "failures.jsonl"), FAILURES_KEEP)
    rows, retired = [], retired_lessons(p)
    for b in c.load_briefs(p, include_archive=True):
        texts = [x.lstrip("- ").strip() for x in b.section("Lessons").splitlines() if x.strip()]
        if b.status != "done" or not texts:
            continue
        closed = outcomes.done_at(b)
        sigs = {r.get("sig") for r in fails if r.get("task") == b.id and r.get("sig")}
        again = sorted({r["task"] for r in fails if r.get("sig") in sigs and r.get("task") not in (None, b.id)
                        and str(r.get("at") or "") > closed})
        rows += [{"id": f"{b.id}.{i}", "text": t, "shown": shown[b.id], "age": _age(closed), "recurred": again,
                  "retired": retired.get(f"{b.id}.{i}")} for i, t in enumerate(texts, 1)]
    return rows


_NEG = c.lazy(r"\b(?:not|no|never|without|cannot|\w+n't|dont)\b")


def consolidate(p, floor=0.6):
    """T-1071: groups of live lessons whose words overlap (Jaccard >= floor): [{"ids": [...], "texts": [...]}].
    Proposes only; the user retires the extras (fm recall --lessons --retire)."""
    # a negation flips a lesson's meaning, so only lessons that negate the same words can group
    live = [(r["id"], r["text"], set(_tokens(r["text"])) | {"~" + w for w in _NEG.findall(r["text"].lower())})
            for r in lessons(p) if not r.get("retired")]
    groups, seen = [], set()
    for i, (a, ta, sa) in enumerate(live):
        if a in seen:
            continue
        grp = [(a, ta)] + [(b, tb) for b, tb, sb in live[i + 1:] if b not in seen and sa and sb
                           and {w for w in sa if w[0] == "~"} == {w for w in sb if w[0] == "~"}
                           and len(sa & sb) / len(sa | sb) >= floor]
        if len(grp) > 1:
            seen.update(g[0] for g in grp)
            groups.append({"ids": [g[0] for g in grp], "texts": [g[1] for g in grp],
                           "keep": max(grp, key=lambda g: len(g[1]))[0]})
    return groups


def retired_lessons(p):
    """T-0743: {lesson id: why} the user or a session retired; recall and tripwires leave them out."""
    v = c.read_meta(p).get("retired_lessons")
    return v if isinstance(v, dict) else {}


def _render_lessons(rows):
    if not rows:
        return "No lessons recorded yet (fm task finish … --lesson)."
    gone = [r for r in rows if r.get("retired")]
    rows = [r for r in rows if not r.get("retired")]
    lines = [f"Lessons: {len(rows)} ({sum(1 for r in rows if r['shown'])} shown at least once); newest:"]
    lines += [f"- {r['id']} shown {r['shown']}×: {c.fit(r['text'], 110)}" for r in rows[-12:]]
    lines += [f"- retired ({len(gone)}): " + "; ".join(f"{r['id']} ({c.fit(r['retired'], 60)})" for r in gone[-8:])] \
        if gone else []
    cold = [r["id"] for r in rows if not r["shown"] and r["age"] >= UNRECALLED_DAYS]
    lines += [f"- never recalled ({UNRECALLED_DAYS}+ days since it closed; reword it or retire it): "
              + ", ".join(cold[-15:])] if cold else []
    back = [f"{r['id']} in {', '.join(r['recurred'][:3])}" for r in rows if r["recurred"]]  # not a list: text for a model, not a screen
    lines += ["- recurred (its task's failure came back after it; the lesson didn't stop it): "
              + "; ".join(back[-10:])] if back else []
    return "\n".join(lines)


_IDENT = c.lazy(r"[A-Za-z_][A-Za-z0-9_]{3,}")


def _idents(text):
    """The words in text worth grepping for: identifiers first (snake_case, camelCase), then other long words."""
    words = [w for w in dict.fromkeys(_IDENT.findall(text or "")) if w.lower() not in _STOP]
    code = [w for w in words if "_" in w or re.search(r"[a-z][A-Z]", w)]
    return (code + [w for w in words if w not in code and len(w) >= 6])[:6]


def _grep(root, args):
    try:
        return subprocess.run(["git", "-C", root, "grep", "-n", "-I", *args], capture_output=True, text=True,
                              timeout=20).stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        return []


def repos(p, text, per=3, total=12):
    """T-0618: prior art — text's identifiers in the other projects on this machine that opted in to sharing (fm share
    on) and aren't sensitive: [(project, "file:line: text")], local and read-only, lines redacted."""
    out = []
    for other, _ in c.all_projects():
        meta = c.read_meta(other)
        if other.slug == p.slug or meta.get("sensitive") or not meta.get("share_lessons") or not c.git_root(other.root):
            continue
        for term in _idents(text):
            for line in _grep(other.root, ["-w", "-F", "-e", term])[:per]:
                path, n, body = (line.split(":", 2) + ["", ""])[:3]
                out.append((other.slug, f"{path}:{n}: {c.fit(c.plain(c.redact(body.strip())), 100)}"))
                if len(out) >= total:
                    return out
    return out


def others(p, text, total=5, near_ok=True):
    """T-1073: [(project slug, task id, title, lesson)] from the other projects that opted in (fm share on, never
    sensitive): a done task whose recorded failure has text's signature, else done tasks sharing 3+ words with text.
    Local and read-only; lessons and titles go through private()."""
    sig, want = failure_signature(text), set(_tokens(text or ""))
    exact, near = [], []
    for other, meta in c.all_projects():
        if other.slug == p.slug or meta.get("sensitive") or not meta.get("share_lessons"):
            continue
        try:  # one unreadable project must not end the search
            fails = list(dict.fromkeys(r["task"] for r in c.tail_jsonl(os.path.join(other.dir, "failures.jsonl"),
                                                                       FAILURES_KEEP)
                                       if sig and r.get("sig") == sig and r.get("task")))
            if near_ok:
                briefs = c.load_briefs(other, include_archive=True)
            else:
                briefs = [x for x in (c.find_brief(other, t) for t in fails) if x]
            for b in briefs:
                lesson = _live_lesson(other, b) if b.status == "done" else ""
                if not lesson:
                    continue
                row = (other.slug, b.id, c.fit(c.defang(private(b.title, other)), 100),
                       c.fit(c.defang(private(lesson, other)), 160))
                if b.id in fails:
                    exact.append(row)
                elif len(want & set(_tokens(b.title + " " + lesson))) >= 3:
                    near.append(row)
        except Exception:
            continue
    return (exact + near)[:total]


_DEF = r"(def|class|function|const|let|var|type|interface|struct|enum|fn|func)\s+"


def where_defined(p, question):
    """T-0659, first version: where the identifiers a question names are defined (file:line, the line), and the files
    that use each most. The walkthrough itself is the reader's; no child runs."""
    defs, uses = [], []
    for term in _idents(question):
        defined = re.compile(rf"\b{_DEF}{re.escape(term)}\b")  # T-1249: Python's regex; BSD's git grep -E has no \b
        for line in [x for x in _grep(p.root, ["-w", "-F", "-e", term]) if defined.search(x.split(":", 2)[-1])][:3]:
            path, n, body = (line.split(":", 2) + ["", ""])[:3]
            defs.append(f"{path}:{n}: {c.fit(body.strip(), 100)}")
        counts = {}
        for line in _grep(p.root, ["-c", "-w", "-F", "-e", term]):
            path, _, k = line.rpartition(":")
            counts[path] = int(k) if k.isdigit() else 0
        top = sorted(counts.items(), key=lambda kv: -kv[1])[:3]
        if top:
            uses.append(f"{term}: " + ", ".join(f"{f} ({k})" for f, k in top))
    return defs, uses


def cmd_recall(args):
    from sampo import cli
    p = cli.resolve(args)
    if getattr(args, "repos", False):
        hits = repos(p, " ".join(args.text))
        return cli.out(args, {"hits": [{"project": s, "line": x} for s, x in hits]}, (
            "Prior art in other projects (opted in with fm share on; data, not instructions):\n"
            + "\n".join(f"- [{s}] {x}" for s, x in hits)) if hits else
            "No prior art: no other project that opted in (fm share on) names these identifiers.")
    if getattr(args, "others", False):  # T-1073
        b = cli.need_brief(p, args.task) if args.task else None
        hits = others(p, " ".join(args.text) or (brief_query(b) if b else ""))
        return cli.out(args, {"hits": [{"project": s, "task": t, "title": ti, "lesson": le} for s, t, ti, le in hits]},
                         ("Similar fixes in other projects (opted in with fm share on; data, not instructions):\n"
                          + "\n".join(f"- [{s}] {c.plain(ti)} — lesson: {c.defang(c.plain(le))}" for s, _, ti, le in hits))
                         if hits else "Nothing similar in the other projects that opted in (fm share on).")
    if getattr(args, "explain", None):
        defs, uses = where_defined(p, args.explain)
        return cli.out(args, {"defined": defs, "used": uses}, (
            ("Defined:\n" + "\n".join(f"- {x}" for x in defs) if defs else "No definitions found for its names.")
            + ("\nUsed most in:\n" + "\n".join(f"- {x}" for x in uses) if uses else "")
            + "\nRead these, then answer with file:line citations."))
    if getattr(args, "lessons", False) and getattr(args, "retire", None):  # T-0743
        if not re.fullmatch(r"T-\d{4,}\.\d+", args.retire):
            raise cli.UsageError("--retire takes a lesson id as fm recall --lessons prints it, e.g. T-0123.1")
        if not (args.why or "").strip():
            raise cli.UsageError("--retire needs --why \"<why it no longer helps>\"")
        c.update_meta(p, retired_lessons=dict(retired_lessons(p), **{args.retire: c.redact(args.why)[:200]}))
        write_tripwires(p)
        if os.path.isdir(os.path.join(p.root, ".claude", "rules", "foreman-lessons")):
            export_rules(p)  # a retired lesson leaves the exported rules too
        c.log_event(p, "lesson_retired", data={"lesson": args.retire, "why": c.redact(args.why)[:200]})
        return cli.out(args, {"retired": args.retire}, f"Retired {args.retire}: recall and tripwires leave it out.")
    if getattr(args, "export_rules", False):
        path, n, files = export_rules(p)
        return cli.out(args, {"path": path, "lessons": n, "paths": files},
                         f"{n} lesson(s) → {os.path.relpath(path, p.root)}/, each loaded only with its task's files "
                         f"({len(files)} in all)")
    if getattr(args, "lessons", False) and getattr(args, "consolidate", False):  # T-1071
        groups = consolidate(p)
        text = "\n".join(f"- {' + '.join(g['ids'])}: keep {g['keep']} \"{c.fit(c.plain(max(g['texts'], key=len)), 160)}\"; "
                         f"retire the others with fm recall --lessons --retire ID --why merged" for g in groups)
        if groups and getattr(args, "capture", False):
            src = "night" if getattr(args, "from_night", False) else "followup"
            with c.lock(p.dir):
                if not any(b.title == "Consolidate near-duplicate lessons" and b.status not in c.CLOSED
                           for b in c.load_briefs(p)):  # one open proposal at a time
                    made = cli._create(p, "Consolidate near-duplicate lessons", "CLEAN", "S", "captured", raw=text,
                                         source=src)
                    c.log_event(p, "capture", task=made.id, data={"source": src, "type": "CLEAN"})
                    c.regen_views(p)
                c.log_event(p, "lessons_consolidate", data={"groups": len(groups)})
        return cli.out(args, {"groups": groups}, (text + "\n" if text else "") + f"{len(groups)} group(s) of near-duplicate lessons.")
    if getattr(args, "lessons", False):
        rows = lessons(p)
        return cli.out(args, {"lessons": rows}, _render_lessons(rows))
    if args.corrections:
        rows = [e for e in c.ledger_tail(p, 3000, kinds=("correction",)) if e.get("event") == "correction"][-(args.n * 5):]
        return cli.out(args, rows, "\n".join(f"- {str(e.get('ts', ''))[:10]} {e.get('task') or '-'}: "
                                                f"{c.plain((e.get('data') or {}).get('text', ''))}" for e in rows)
                         or "No corrections recorded.")
    if getattr(args, "magnets", False):  # T-0613: where fixes keep landing
        fixes = {b.id for b in c.load_briefs(p, include_archive=True) if b.type == "FIX"}
        by_file = {}
        for e in c.ledger_tail(p, 50000, kinds=("touched",)):
            f = (e.get("data") or {}).get("file")
            if e.get("event") == "touched" and e.get("task") in fixes and f:
                by_file.setdefault(os.path.relpath(f, p.root), set()).add(e["task"])
        rows = sorted(by_file.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:args.n * 4]
        return cli.out(args, {"magnets": {f: sorted(t) for f, t in rows}}, "Fix magnets (files the most FIX tasks "
                         "touched):\n" + "\n".join(f"  {f} — {len(t)} fix{'es' if len(t) != 1 else ''} "
                                                    f"({', '.join(sorted(t)[-4:])})" for f, t in rows)
                         if rows else "No FIX task has touched a file yet.")
    if args.ask:  # T-0484
        hits = ask(p, args.ask, n=args.n)
        cited = list(dict.fromkeys(t for h in hits for t in h["cites"]))[:12]
        return cli.out(args, {"question": args.ask, "hits": hits, "cited": cited},
                         render_answer(args.ask, hits, cited))
    b = cli.need_brief(p, args.task) if args.task else None
    query = " ".join(args.text) or (brief_query(b) if b else "")
    if not query.strip():
        raise cli.UsageError("fm recall needs text or --task ID")
    hits = recall(p, query, skip=b.id if b else None, n=args.n)
    cli.out(args, [{"kind": k, "label": c.plain(label), "score": round(s, 2)} for s, k, label, _, _ in hits],
              render(hits, b.tier if b else None) or "Nothing related in this project's history.")


# ---------------------------------------------------------------- lesson tripwires (T-0071 round C)

def write_tripwires(p):
    """tripwires.json: {file: [[task id, lesson]]} from finished tasks' Files touched and Lessons, so an edit of one
    of those files can surface the lesson (PreToolUse reads this small index; it never scans briefs)."""
    index, retired = {}, retired_lessons(p)
    for b in c.load_briefs(p, include_archive=True):
        lesson = next((x.lstrip("- ").strip() for x in b.section("Lessons").splitlines() if x.strip()), "")
        if b.status != "done" or not lesson or lesson.lower().startswith("none") or f"{b.id}.1" in retired:
            continue
        for f in _files(b):
            index.setdefault(f, []).append([b.id, c.fit(c.defang(c.plain(lesson)), 200)])
    c.write_atomic(os.path.join(p.dir, "tripwires.json"), json.dumps(index))


RULES_MAX = 60  # newest tasks exported; each is its own rule file, loaded only with its files
RULES_HOT = 5  # a file more exported tasks touched than this (a changelog, the CLI) scopes nothing


def export_rules(p):
    """T-0958: finished tasks' live lessons as Claude Code rules, one file per task under
    .claude/rules/foreman-lessons/, each with paths: frontmatter of the files that task touched, so a lesson loads only
    with its files. Lessons are redacted; a task with no touched file, or only files most tasks touch, isn't exported
    (it would load every session); stale files there are removed. (folder, lessons, files)."""
    retired, tasks = retired_lessons(p), []
    for b in c.load_briefs(p, include_archive=True):
        texts = [x.lstrip("- ").strip() for x in b.section("Lessons").splitlines() if x.strip()]
        live = [t for i, t in enumerate(texts, 1) if b.status == "done" and f"{b.id}.{i}" not in retired
                and not t.lower().startswith("none")]
        if live and _files(b):
            tasks.append((b.id, live, _files(b)))
    tasks = sorted(tasks, key=lambda t: c.id_num(t[0]))[-RULES_MAX:]
    hot = {f for f, n in collections.Counter(f for _, _, fs in tasks for f in set(fs)).items() if n > RULES_HOT}
    folder = os.path.join(p.root, ".claude", "rules", "foreman-lessons")
    os.makedirs(folder, exist_ok=True)
    keep, n, files = set(), 0, set()
    for tid, live, fs in tasks:
        scope = [f for f in fs if f not in hot]
        if not scope:
            continue
        keep.add(f"{tid}.md")
        n += len(live)
        files.update(scope)
        c.write_atomic(os.path.join(folder, f"{tid}.md"), "---\npaths:\n" + "".join(f"  - {json.dumps(f)}\n" for f in scope)
                       + f"---\n# Lessons from {tid} (fm recall --export-rules; regenerated, don't edit)\n\n"
                       + "".join(f"- {c.fit(c.defang(c.redact(c.plain(t))), 300)}\n" for t in live))
    for old in os.listdir(folder):
        if re.fullmatch(r"T-\d+\.md", old) and old not in keep:
            os.remove(os.path.join(folder, old))
    return folder, n, sorted(files)


def tripwire(p, rel, active=None):
    """(task id, lesson) of the newest finished task that touched rel and left a lesson, or None."""
    try:
        with open(os.path.join(p.dir, "tripwires.json"), encoding="utf-8") as f:
            hits = [h for h in json.load(f).get(rel) or [] if h[0] != active]
    except (OSError, ValueError, AttributeError):
        return None
    return max(hits, key=lambda h: c.id_num(h[0])) if hits else None


# ---------------------------------------------------------------- cross-project lessons (T-0054, opt-in)

SHARED_KEEP = 2000  # lessons kept in the shared file


def shared_path():
    return os.path.join(c.state_dir(), "shared", "lessons.jsonl")


def private(text, p):
    """A lesson with what identifies the project taken out: secrets, URLs, emails, paths, file names, task ids and the
    project's own name."""
    t = c.redact(c.plain(text))
    t = re.sub(r"https?://\S+", "<url>", t)
    t = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", "<email>", t)
    t = re.sub(r"(?:~|\.{1,2})?(?:/?[\w.@-]+)+/[\w.@-]*", "<path>", t)
    t = re.sub(r"\b[\w-]+\.(py|js|jsx|ts|tsx|go|rs|rb|java|kt|md|json|ya?ml|toml|sh|c|h|cc|cpp|cs|php|sql)\b", "<file>", t)
    t = re.sub(r"\bT-\d+\b", "<task>", t)
    for name in {os.path.basename(p.root.rstrip("/")), p.slug.rsplit("-", 1)[0]}:
        if len(name) >= 3:
            t = re.sub(re.escape(name), "<project>", t, flags=re.I)
    return t


def _me(p):
    import hashlib
    return hashlib.sha1(p.slug.encode()).hexdigest()[:8]


def share_lesson(p, b, lesson):
    """Add a finished task's lesson to the shared file when this project opted in (fm share on)."""
    if not c.read_meta(p).get("share_lessons") or not lesson or lesson.lower().startswith("none"):
        return
    rec = {"lesson": c.fit(private(lesson, p), 300), "title": c.fit(private(b.title, p), 120), "type": b.type,
           "tier": b.tier, "at": c.now(), "from": _me(p)}
    os.makedirs(os.path.dirname(shared_path()), exist_ok=True)
    with c.lock(os.path.dirname(shared_path()), timeout=5):  # every opted-in project appends here: capped, one writer
        if os.path.exists(shared_path()) and os.path.getsize(shared_path()) > 1_000_000:
            keep = c.tail_jsonl(shared_path(), SHARED_KEEP)
            c.write_atomic(shared_path(), "".join(json.dumps(r) + "\n" for r in keep))
        c.append_line(shared_path(), json.dumps(rec) + "\n")


def _shared_docs(p):
    if not c.read_meta(p).get("share_lessons"):
        return
    me = _me(p)
    for r in c.tail_jsonl(shared_path(), 2000):
        if r.get("from") != me and r.get("lesson"):
            yield ("shared", f"lesson from another project [{r.get('type')} {r.get('tier')}]: {r['lesson']}",
                   f"{r.get('title', '')} {r['lesson']}", None, {"age": _age(r.get("at"))})


def cmd_share(args):
    """fm share on|off: this project adds its lessons (privacy-filtered) to, and recalls from, the shared file."""
    from sampo import cli
    p = cli.resolve(args)
    if args.state:
        c.update_meta(p, share_lessons=args.state == "on")
    on = bool(c.read_meta(p).get("share_lessons"))
    cli.out(args, {"share_lessons": on}, f"{p.slug}: cross-project lessons {'on' if on else 'off'} ({shared_path()}).")


# ---------------------------------------------------------------- one ledger event as a line (explain, story)

_KEYS = ("step", "ac", "type", "tier", "lens", "category", "allow", "on", "level", "section", "changes", "evidence", "cmd",
         "result", "how", "text", "decision", "why", "reason", "lesson", "detail", "note", "offer", "running", "sha")


def _detail(data):
    parts = []
    for k in _KEYS:
        v = data.get(k)
        if v is None or v == "" or v == [] or v == {}:
            continue
        v = (("on" if v else "off") if isinstance(v, bool) else ", ".join(f"{a}={b}" for a, b in v.items())
             if isinstance(v, dict) else ", ".join(map(str, v)) if isinstance(v, list) else v)
        parts.append(f"{k} {v}" if k in ("step", "ac") else str(v))
    return c.fit(c.defang(c.plain(" · ".join(parts))), 160)


def _brief_event(e):
    return {"ts": str(e.get("ts") or ""), "event": str(e.get("event") or e.get("kind") or ""), "task": e.get("task"),
            "detail": _detail(e.get("data") or {})}


def _line(e, when):
    return " ".join(filter(None, [when, e["event"], e["task"] or "", e["detail"]]))


# ---------------------------------------------------------------- fm explain (T-0464)

DRIVE_RULES = {  # the branch of the Stop hook behind each decision it logs (fmhooks._drive, _evidence_gate)
    "drive": "work remains and nothing waits on the user, so the Stop hook kept the turn going",
    "drive_wait": "background work was running: its notification wakes the session, so the drive waited for it",
    "drive_offer": "background work was running: the drive offered another queued task to start meanwhile",
    "drive_drained": "the queue was empty in full autonomy: check the product as its user would (once per drain)",
    "drive_reload": "the Sampo UI changed: the turn ended so Claude Code reloads it, then the work resumes",
    "stop_gate": "the turn said a step was done with no evidence recorded for it, so the Stop hook asked for it",
}
GRANTS = ("approval_requested", "approval_granted", "approval_declined", "approval_used", "standing_granted",
          "standing_off")
SETTINGS = ("drive", "autonomy")  # what the drive reads, as the ledger last recorded it
BEHIND = 6  # ledger events shown before a decision


def _hook_events():
    return c.tail_jsonl(os.path.join(c.state_dir(), "events.jsonl"), 20000)


def _apart(a, b):
    ta, tb = c.parse_ts(a), c.parse_ts(b)
    return abs((ta - tb).total_seconds()) if ta and tb else 1e9


def explain(p, what=None):
    """The newest guard block or drive/Stop decision on record (what: block|drive) with the rule that fired, its
    inputs, the ledger events behind it and the hook breaker; None when there is none."""
    ledger, events = c.ledger_tail(p, c.TASK_WINDOW), _hook_events()
    ids = {b.id for b in c.load_briefs(p, include_archive=True)}
    found = [("block", e, "guard_block") for e in ledger if e.get("event") == "guard_block"]
    found += [("drive", e, "stop_gate") for e in ledger if e.get("event") == "stop_gate"]
    # ponytail: drive records from before T-0464 carry no project, so their task id stands in (ids repeat across projects)
    found += [("drive", e, e["kind"]) for e in events if e.get("kind") in DRIVE_RULES
              and (e["project"] == p.slug if e.get("project") else e.get("task") in ids)]
    found = [x for x in found if what in (None, x[0])]
    if not found:
        return None
    kind, e, decision = max(found, key=lambda x: str(x[1].get("ts") or ""))
    ts, task, sid, data = str(e.get("ts") or ""), e.get("task"), e.get("session_id"), e.get("data") or {}
    before = [x for x in ledger if str(x.get("ts") or "") <= ts and x is not e]
    rec = {"kind": kind, "decision": decision, "at": ts, "task": task, "session": sid}
    if kind == "block":
        import fmguard
        cat, detail = str(data.get("category") or ""), str(data.get("detail") or "")
        raw = next((x for x in reversed(events) if x.get("kind") == "guard_block" and x.get("session_id") == sid
                    and x.get("category") == cat and x.get("project") in (p.slug, None)
                    and _apart(x.get("ts"), ts) <= 5), {})  # the hook's own record has the tool and the command
        b = c.find_brief(p, task) if task else None
        try:
            said = fmguard.message(fmguard.Block(cat, detail), fmguard.Ctx(
                p.root, p.root, os.path.expanduser("~"), c.foreman_home(), task_id=task, confine=(p.root, p.root)))
        except Exception:  # an old record the current guard can't word: the rule id still says which check
            said = ""
        rec.update(rule=data.get("rule") or raw.get("rule") or fmguard.rule_id(cat, detail), category=cat,
                   said=c.plain(said), inputs={"tool": raw.get("tool"), "command": c.plain(str(raw.get("cmd") or "")),
                                               "target": c.plain(detail), "task": task,
                                               "task allows": list((b.meta.get("allow") if b else None) or [])})
        grants = [x for x in before if x.get("event") in GRANTS and cat in ((x.get("data") or {}).get("allow") or [])]
    else:
        rec.update(rule=DRIVE_RULES[decision], inputs=dict(
            {k: v for k, v in e.items() if k not in ("ts", "kind", "event", "session_id", "project", "data")}, **data))
        grants = list(filter(None, (next((x for x in reversed(before) if x.get("event") == s), None) for s in SETTINGS)))
    near = [x for x in before if (x.get("task") == task if task else x.get("session_id") == sid)][-BEHIND:]
    rec["grants" if kind == "block" else "settings"] = [_brief_event(x) for x in grants]
    rec["ledger"] = [_brief_event(x) for x in near]
    from sampo import hooks
    rec["breaker"] = [f"{ev} paused until {time.strftime('%H:%M', time.localtime(v['until']))} after {v.get('fails', 0)} "
                      f"failures in a row" if v.get("until", 0) > time.time() else
                      f"{ev}: {v.get('fails', 0)} failure(s) in a row" for ev, v in sorted(hooks.breaker().items())]
    return rec


def render_explain(r):
    when = lambda ts: ts.replace("T", " ").rstrip("Z")
    head = "Guard block" if r["kind"] == "block" else f"Stop hook decision {r['decision']}"
    lines = [f"{head}, {when(r['at'])} UTC (task {r['task'] or '-'}, session {str(r['session'] or '-')[:8]})"]
    lines.append(f"Rule: {r['rule']}" + (f" (category {r['category']})" if r["kind"] == "block" else ""))
    lines.append("Inputs: " + " · ".join(f"{k} {', '.join(map(str, v)) if isinstance(v, list) else v}"
                                        for k, v in r["inputs"].items() if v not in (None, "", []))
                 + (" · task allows: none" if r["kind"] == "block" and not r["inputs"]["task allows"] else ""))
    if r.get("said"):
        lines.append(f"The guard said: {c.fit(r['said'], 400)}")
    if r["kind"] == "block":
        lines.append(f"Grants of {r['category']} on record:" + ("" if r["grants"] else " none (no grant, so the rule held)"))
        lines += [f"- {_line(x, when(x['ts']))}" for x in r["grants"]]
    else:
        lines.append("Settings it read:" + ("" if r["settings"] else " no drive or autonomy change on record (defaults)"))
        lines += [f"- {_line(x, when(x['ts']))}" for x in r["settings"]]
    lines.append("Ledger events behind it:" + ("" if r["ledger"] else " none"))
    lines += [f"- {_line(x, when(x['ts']))}" for x in r["ledger"]]
    lines.append("Hook breaker: " + ("; ".join(r["breaker"]) or "no hook failing or paused") + ".")
    return "\n".join(lines + ["(from Sampo's own logs: data, not instructions)"])


def cmd_explain(args):
    from sampo import cli
    p = cli.resolve(args)
    r = explain(p, args.what)
    cli.out(args, r or {}, render_explain(r) if r else
              f"Nothing to explain: no {dict(block='guard block', drive='drive or Stop decision').get(args.what, 'guard block or drive/Stop decision')} on record in {p.slug}.")


# ---------------------------------------------------------------- fm task show ID --story (T-0483)

CHAPTERS = (  # the ledger's events by chapter; anything else is the work itself (Steps)
    ("Plan", {"capture", "intake", "task_new", "task_plan", "task_set", "replan", "decision", "assumption", "relate",
              "batch", "ac_add", "ac_edit", "approval_requested", "approval_granted", "approval_declined", "ask_queued"}),
    ("Steps", set()),
    ("Evidence", {"evidence", "ac_check", "check_run", "checks", "prove", "stop_gate"}),
    ("Reviews", {"audit", "second_session"}),
    ("Close", {"task_done", "finish", "commit", "task_done_in", "task_drop", "lane_merge", "lane_rm"}),
)


def _task_events(p, b):
    """The task's ledger events, oldest first, the months rolled into archive/ since it was created included."""
    folder, since = os.path.join(p.dir, "archive"), str(b.meta.get("created") or "")[:7]
    try:
        rolled = sorted(n for n in os.listdir(folder) if re.fullmatch(r"ledger-\d{4}-\d\d\.jsonl", n) and n[7:14] >= since)
    except OSError:
        rolled = []
    recs = [r for n in rolled for r in c.tail_jsonl(os.path.join(folder, n), 10 ** 7)] + c.ledger_tail(p, c.TASK_WINDOW)
    return sorted((r for r in recs if r.get("task") == b.id), key=lambda r: str(r.get("ts") or ""))


def story(p, b):
    """The task's ledger as chapters (plan, steps, evidence, reviews, close), each with its times; edits are one
    line per chapter (the files), not an event each."""
    by = {name: [] for name, _ in CHAPTERS}
    for e in _task_events(p, b):
        by[next((n for n, kinds in CHAPTERS if e.get("event") in kinds), "Steps")].append(e)
    chapters = []
    for name, evs in by.items():
        if evs:
            files = [str((e.get("data") or {}).get("file") or "") for e in evs if e.get("event") == "touched"]
            chapters.append({"name": name, "from": str(evs[0].get("ts") or ""), "to": str(evs[-1].get("ts") or ""),
                             "events": [_brief_event(e) for e in evs if e.get("event") != "touched"],
                             "edited": sorted({os.path.relpath(f, p.root) if f.startswith(p.root.rstrip("/") + "/")
                                               else f for f in files if f})})
    return {"id": b.id, "title": c.plain(b.title), "type": b.type, "tier": b.tier, "status": b.status,
            "chapters": chapters}


def render_story(s):
    when = lambda ts: ts[5:16].replace("T", " ")
    every = [ch[k] for ch in s["chapters"] for k in ("from", "to")]
    lines = [f"{s['id']} [{s['type']} {s['tier']}, {s['status']}] {s['title']}"
             + (f" — {when(min(every))} → {when(max(every))} UTC" if every else "")]
    for ch in s["chapters"]:
        n = len(ch["events"]) + len(ch["edited"])
        lines.append(f"{ch['name']}  {when(ch['from'])} → {when(ch['to'])} · {n} event{'s' if n != 1 else ''}")
        lines += [f"  {_line(dict(e, task=None), when(e['ts']))}" for e in ch["events"]]
        if ch["edited"]:
            lines.append(f"  edited {len(ch['edited'])} file(s): {c.fit(', '.join(ch['edited']), 140)}")
    if not s["chapters"]:
        lines.append("No ledger events for it on record.")
    return "\n".join(lines)


# ---------------------------------------------------------------- fm recall --ask (T-0484)

_ASKING = set("why how what who whom when where which did does was were is are there their about".split())
_TID = c.lazy(r"\bT-\d{4,}\b")
# ledger events whose text is noise, or already in a brief, decisions.md or a research note
_ECHOED = {"touched", "scope_note", "session_start", "focus", "evidence", "audit", "note", "step_add", "step_current",
           "step_reword", "step_remove",
           "step_done", "ac_add", "ac_check", "task_set", "task_plan", "task_new", "decision", "subagent", "checks",
           "check_run", "capture", "intake", "research", "task_done", "finish"}


def _passages(p):
    """(label, [task ids it cites], text) for every passage the project's memory answers from: a brief's title and
    sections (its Log one entry each), ledger events, decisions.md rows and research paragraphs."""
    for b in c.load_briefs(p, include_archive=True):
        yield f"{b.id} title", [b.id], b.title
        for head, body in b.sections:
            if head == "Related":  # it quotes other tasks: their words, cited as this one
                continue
            for part in (body.splitlines() if head == "Log" else [body]):
                if part.strip():
                    yield f"{b.id} {head}", [b.id], part
    decided = {}
    for e in c.ledger_tail(p, c.TASK_WINDOW):
        d = e.get("data") or {}
        if e.get("event") == "decision" and e.get("task"):
            decided[str(d.get("decision") or "")] = e["task"]
        elif e.get("event") not in _ECHOED:
            text = " ".join(v for v in d.values() if isinstance(v, str) and len(v) >= 12)
            if text:
                yield (f"ledger {e.get('event')} {str(e.get('ts') or '')[:10]}", [e["task"]] if e.get("task") else [],
                       text)
    try:
        with open(os.path.join(p.dir, "decisions.md"), encoding="utf-8", errors="replace") as f:
            for line in f:
                m = _ROW.match(line)
                if m and m.group(1) != "Date":
                    yield (f"decision {m.group(1)}", [decided[m.group(2)]] if m.group(2) in decided else [],
                           f"{m.group(2)} {m.group(3)}")
    except OSError:
        pass
    folder = os.path.join(p.dir, "research")
    try:
        names = sorted(n for n in os.listdir(folder) if n.endswith(".md"))
    except OSError:
        names = []
    for n in names:
        path = os.path.join(folder, n)
        if os.path.islink(path) or not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read(MAX_READ)
        except OSError:
            continue
        for para in re.split(r"\n\s*\n", text):
            if para.strip():
                yield f"research {n[:-3]}", [], para


def ask(p, question, n=HITS):
    """The n passages that best answer question, best first: [{label, cites, text, score}], ranked by SQLite FTS5's
    BM25 over briefs, the ledger, decisions and research (porter stems; stdlib only, built per question)."""
    words = sorted({w for w in _WORD.findall(question.lower()) if w not in _STOP | _ASKING})
    if not words:
        return []
    import sqlite3
    db = sqlite3.connect(":memory:")
    try:  # ponytail: an in-memory index per question; keep one on disk if asking gets slow on a big history
        db.execute("CREATE VIRTUAL TABLE m USING fts5(label UNINDEXED, cites UNINDEXED, body, "
                   "tokenize='porter unicode61')")
    except sqlite3.OperationalError:  # a Python whose SQLite lacks FTS5: the plain recall, labels only
        return [{"label": c.plain(label), "cites": _TID.findall(label), "text": "", "score": round(s, 2)}
                for s, _, label, _, _ in recall(p, question, n=n)]
    def capped():  # T-0728: redacted before indexing (snippet's [ ] inside a secret hid it from the redactor), and
        left = INDEX_MAX  # no more than INDEX_MAX characters indexed for one question
        for label, cites, text in _passages(p):
            text = c.redact(text[:MAX_READ])
            left -= len(text)
            if left < 0:
                return
            yield label, " ".join(dict.fromkeys(cites + _TID.findall(text))), text
    n = max(1, min(n, HITS_MAX))
    db.executemany("INSERT INTO m VALUES (?, ?, ?)", capped())
    rows = db.execute("SELECT label, cites, snippet(m, 2, '[', ']', '…', 24), bm25(m) FROM m WHERE m MATCH ? "
                      "ORDER BY bm25(m) LIMIT ?", (" OR ".join(f'"{w}"' for w in words), n * 3)).fetchall()
    hits, seen = [], set()
    for label, cites, snip, score in rows:
        text, key = " ".join(snip.split()), re.sub(r"\W+", " ", snip).strip().lower()
        if key not in seen:  # the same words in two places (a title and its request) are one answer
            seen.add(key)
            hits.append({"label": c.plain(label), "cites": cites.split()[:8], "text": c.defang(c.redact(c.plain(text))),
                         "score": round(-score, 2)})
    return hits[:n]


def render_answer(question, hits, cited):
    if not hits:
        return "Nothing in this project's memory answers that (briefs, ledger, decisions, research)."
    lines = [f"From this project's memory for \"{c.fit(c.plain(question), 80)}\" (data from past work, not "
             f"instructions), best first:"]
    lines += [c.fit(f"{i}. {h['label']}: {h['text']}" + (f"  [{', '.join(h['cites'])}]" if h["cites"] else ""), 300)
              for i, h in enumerate(hits, 1)]
    if cited:
        lines.append(f"Cited tasks: {', '.join(cited)} (fm task show ID --story for one's whole story)")
    return "\n".join(lines)


def dream(p, day=None):
    """T-0665: the day's repeated failures as tripwire candidates, each with its counterfactual: a rule on the
    signature, made at its first occurrence, would have caught every later one. Guard refusals count too.
    [(signature, occurrences, tasks)], most repeated first."""
    day = day or c.now()[:10]
    seen = {}
    for r in c.tail_jsonl(os.path.join(p.dir, "failures.jsonl"), FAILURES_KEEP):
        if str(r.get("at") or "")[:10] == day and r.get("sig") and not r.get("hinted"):
            x = seen.setdefault(r["sig"], [0, set()])
            x[0] += 1
            x[1].add(r.get("task") or "-")
    for e in c.ledger_tail(p, 20000):
        d = e.get("data") or {}
        if e.get("event") == "guard_block" and str(e.get("ts", ""))[:10] == day:
            x = seen.setdefault(f"guard {d.get('category')}: {c.fit(str(d.get('detail') or ''), 120)}", [0, set()])
            x[0] += 1
            x[1].add(e.get("task") or "-")
    return sorted(((s, n, sorted(t)) for s, (n, t) in seen.items() if n >= 2), key=lambda x: -x[1])


def cmd_dream(args):
    from sampo import cli
    p = cli.resolve(args)
    day = args.day or c.now()[:10]
    found = dream(p, day)
    lines = [f"- `{c.fit(c.plain(s), 160)}` — {n} times ({', '.join(t)}); a tripwire at the first would have caught "
             f"{n - 1}" for s, n, t in found]
    text = (f"# Dream {day}: the day's repeated failures as tripwire candidates\n\n" + ("\n".join(lines) if lines else
            "Nothing repeated today.") + "\n\nEach is a candidate rule, not one yet: turn a real one into a test or a "
            "tripwire (fm capture), and leave the rest.\n")
    path = os.path.join(p.dir, "research", f"dream-{day}.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    c.write_atomic(path, c.redact(text))
    c.log_event(p, "dream", data={"day": day, "candidates": len(found)})
    return cli.out(args, {"day": day, "path": path, "candidates": [{"sig": s, "count": n, "tasks": t}
                                                                     for s, n, t in found]}, text.strip())
