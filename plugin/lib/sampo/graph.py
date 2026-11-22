"""T-0707 (Frontier 07, first slice): one local graph joining code, tasks and commits, with time on every edge that has
one, so a query can be asked as of any moment (and backtested). sqlite in the project's Sampo folder; nothing leaves
the machine. Code edges come from graphify's graph.json when the project has one, else from the name-stem mentions
fm impact uses; test links from fm map; task→file edges from Foreman-Task commit trailers and touched events;
co-change from commits. fm graph blast: what a change reaches. fm graph pack: a task's ranked read-set (personalized
PageRank seeded by its words)."""
import collections
import json
import os
import re
import sqlite3
import subprocess
import time

from sampo import core as c

COMMITS = 3000      # newest commits read for co-change and trailers
CO_MAX = 20         # a commit touching more files than this says nothing about which belong together
WORD = re.compile(r"[a-z][a-z0-9]{2,}")
STOP = {"the", "and", "for", "with", "from", "this", "that", "into", "when", "not", "are", "was", "add", "fix", "make",
        "use", "new", "all", "its", "out", "one", "has", "can", "but", "now", "get", "set", "run", "too"}


def _words(text):
    return {w for w in WORD.findall(text.lower()) if w not in STOP}


def _stamp(p):
    from sampo import map as smap
    head = c._git(p.root, "rev-parse", "HEAD", timeout=10).strip()
    gj = os.path.join(p.root, "graphify-out", "graph.json")
    try:
        led = os.path.getsize(os.path.join(p.dir, "ledger.jsonl"))
    except OSError:
        led = 0
    return f"{head}:{led}:{os.path.getmtime(gj) if os.path.exists(gj) else 0}:{smap.__file__}"


def db(p):
    """The graph, rebuilt when HEAD, the ledger or graphify's graph moved since it was built."""
    con = sqlite3.connect(os.path.join(p.dir, "graph.sqlite"))
    con.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
    stamp = _stamp(p)
    if (con.execute("SELECT v FROM meta WHERE k='stamp'").fetchone() or [None])[0] != stamp:
        _build(p, con)
        con.execute("INSERT OR REPLACE INTO meta VALUES ('stamp', ?)", (stamp,))
        con.commit()
    return con


def _worktree_copy(f):
    """T-5834: a builder's copy of the repo under .claude/worktrees/ is no file of the project (same rule as split's)."""
    return "/.claude/worktrees/" in "/" + f


def _build(p, con):
    """ponytail: a full rebuild whenever anything moved; incremental (new commits and ledger lines) if it gets slow."""
    from sampo import map as smap
    con.executescript("DROP TABLE IF EXISTS edges; DROP TABLE IF EXISTS tasks;"
                      "CREATE TABLE edges (src TEXT, dst TEXT, kind TEXT, t TEXT, w REAL);"
                      "CREATE TABLE tasks (id TEXT PRIMARY KEY, type TEXT, title TEXT, created TEXT)")
    rows = []
    tasks = {b.id: b for b in c.load_briefs(p)}
    con.executemany("INSERT INTO tasks VALUES (?,?,?,?)", [(b.id, b.type, b.title, str(b.meta.get("created") or ""))
                                                          for b in tasks.values()])
    log = c._git(p.root, "log", f"-{COMMITS}", "--no-renames", "--format=%x01%cI%x00%B%x00", "--name-only",
                 timeout=120) or ""
    files = {f for f in c._git(p.root, "ls-files", timeout=60).splitlines() if not _worktree_copy(f)}
    for entry in log.split("\x01")[1:]:
        when, body, names = (entry.split("\x00") + ["", ""])[:3]
        when = _utc(when)
        changed = [f for f in names.split("\n") if f.strip() and f in files]
        for tid in set(c.task_trailers(body)):
            rows += [(f"task:{tid}", f, "touched", when, 1.0) for f in changed]
        if 2 <= len(changed) <= CO_MAX:
            w = 1.0 / (len(changed) - 1)
            rows += [(a, b, "cochange", when, w) for a in changed for b in changed if a != b]
    for e in c.ledger_tail(p, 50000, kinds=("touched",)):
        f = (e.get("data") or {}).get("file") if e.get("event") == "touched" and e.get("task") else None
        if f and f.startswith(p.root.rstrip("/") + "/") and not _worktree_copy(os.path.relpath(f, p.root)):
            rows.append((f"task:{e['task']}", os.path.relpath(f, p.root), "touched", _utc(str(e.get("ts", ""))), 1.0))
    m = smap.load(p)
    rows += [(t, s, "tests", None, 0.5) for t, srcs in m["tests"].items() for s in srcs
             if not _worktree_copy(t) and not _worktree_copy(s)]
    rows += _code_edges(p, files)
    con.executemany("INSERT INTO edges VALUES (?,?,?,?,?)", rows)
    con.execute("CREATE INDEX IF NOT EXISTS e_src ON edges (src)")
    con.execute("CREATE INDEX IF NOT EXISTS e_dst ON edges (dst)")


def _utc(iso):
    """An ISO time as UTC 'YYYY-MM-DDTHH:MM:SSZ' so times compare as strings."""
    import datetime
    try:
        return datetime.datetime.fromisoformat(iso.strip().replace("Z", "+00:00")).astimezone(
            datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return iso.strip()


def _code_edges(p, files):
    """(user, used, 'uses') file pairs: graphify's when it has a graph here, else name-stem mentions."""
    from sampo import map as smap
    gj = os.path.join(p.root, "graphify-out", "graph.json")
    if os.path.exists(gj):
        try:
            with open(gj) as f:
                g = json.load(f)
            where = {}
            for n in g.get("nodes") or []:
                sf = str(n.get("source_file") or "")
                rel = os.path.relpath(sf, p.root) if os.path.isabs(sf) else sf
                if rel in files:
                    where[n.get("id")] = rel
            out = {(where[e.get("source")], where[e.get("target")]) for e in (g.get("links") or g.get("edges") or [])
                   if e.get("source") in where and e.get("target") in where
                   and where[e.get("source")] != where[e.get("target")]}
            if out:
                return [(a, b, "uses", None, 1.0) for a, b in sorted(out)]
        except (OSError, ValueError, AttributeError):
            pass
    code = sorted(f for f in files if smap._CODE.search(f))[:3000]  # ponytail: O(files × stems) regex scan
    stems = {f: smap._name_stem(f) for f in code if not smap._TEST.search(f)}
    stems = {f: s for f, s in stems.items() if len(s) >= 4 and s.lower() not in smap._COMMON_STEMS}
    if not stems:
        return []
    pat = re.compile(r"\b(" + "|".join(sorted({re.escape(s) for s in stems.values()}, key=len, reverse=True)) + r")\b")
    by_stem = collections.defaultdict(list)
    for f, s in stems.items():
        by_stem[s].append(f)
    out = []
    for f in code:
        try:
            with open(os.path.join(p.root, f), encoding="utf-8", errors="replace") as fh:
                found = set(pat.findall(fh.read(200_000)))
        except OSError:
            continue
        out += [(f, g, "uses", None, 1.0) for s in found for g in by_stem[s] if g != f]
    return out


def _edges(con, as_of, where="", args=()):
    q = f"SELECT src, dst, kind, t, w FROM edges WHERE (t IS NULL OR t <= ?){where}"
    return con.execute(q, (as_of or "9999", *args)).fetchall()


BLAST_BUDGET = 1.0  # seconds fm focus gives the graph query (T-2277)


BUILD_LOCK_S = 600  # a build in flight this young is left alone; an older lock file is a dead build's


def _build_in_background(p):
    """A detached `fm graph build` like fmmap.compact's `fm map`, never two at once (the lock file's age)."""
    if os.environ.get("FOREMAN_NO_BACKGROUND"):
        return
    lock = os.path.join(p.dir, "graph.building")
    try:
        if 0 <= time.time() - os.path.getmtime(lock) < BUILD_LOCK_S:  # a future mtime (clock jump) is expired
            return
    except OSError:
        pass
    try:
        with open(lock, "w") as f:
            f.write(str(os.getpid()))
        subprocess.Popen([os.path.join(c.PLUGIN_ROOT, "bin", "fm"), "graph", "build"], cwd=p.root,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError:
        pass


FRESH_COMMITS = 50  # a graph built this many commits before HEAD still answers (T-2336)
TOKEN = re.compile(r"[\w][\w./-]*")


def _repo_files(p, names):
    """The distinct names that are real files inside the project (no globs, no escapes), in order."""
    root = os.path.realpath(p.root)
    paths = []
    for s in names:
        f = os.path.normpath(s)
        real = os.path.realpath(os.path.join(root, f))
        if not re.search(r"[*?\[\]{}]", s) and real.startswith(root + os.sep) and os.path.isfile(real) \
                and f not in paths:
            paths.append(f)
    return paths


def blast_line(p, scope, budget=BLAST_BUDGET, text=""):
    """T-2277: the plan's one-line blast radius for a task's literal scope files (T-2336: with no scope, the repo files
    its `text` names), or "" when there is nothing to say; "unknown" when those files have no co-change history.
    Reads the graph only if it is built and within FRESH_COMMITS of HEAD (focus never rebuilds it: a build runs git log
    over 3000 commits) and stops a slow query after `budget` seconds; any failure is silence."""
    if scope:
        paths = _repo_files(p, scope)
    else:  # ponytail: any word that is a file; ceiling 300 stats, upgrade is the brief's own file list
        words = {w.rstrip(".,-") for w in TOKEN.findall(text or "")[:2000] if "." in w or "/" in w}
        paths = _repo_files(p, sorted(words)[:300])
    db_path = os.path.join(p.dir, "graph.sqlite")
    if not paths:
        return ""
    try:
        con = sqlite3.connect(db_path, timeout=0.5) if os.path.exists(db_path) else None
        # HEAD alone: ledger events and graphify's file don't change co-change history (the stamp starts with HEAD)
        built = con and (con.execute("SELECT v FROM meta WHERE k='stamp'").fetchone() or [""])[0].split(":")[0]
        # commits HEAD has that the built one lacks; a built commit that is gone (rebased away) is an error: stale
        fresh = bool(built) and int(c._git(p.root, "rev-list", "--count", f"{built}..HEAD", timeout=2).strip() or "x") \
            < FRESH_COMMITS
    except Exception:
        fresh = False
    if not fresh:
        _build_in_background(p)  # the next focus has the line
        return ""
    try:
        end = time.monotonic() + budget
        con.set_progress_handler(lambda: int(time.monotonic() > end), 10000)
        r = blast(p, paths[:20], con=con)
    except Exception:  # a hint: it never stops a focus
        return ""
    bits = []
    if r["cochange"]:
        bits.append("co-changes " + ", ".join(r["cochange"][:5]))  # not a list: a sentence in a plan note
    if r["fixes"]:
        bits.append("past FIXes " + ", ".join(f["id"] for f in r["fixes"][:5]))  # not a list: a sentence in a plan note
    if not bits:  # a graph that was read and found nothing says so, so the plan shows the check ran
        return f"Blast radius (sampo graph blast): unknown, no co-change history for {c.joinmore(paths, 5)}."
    return "Blast radius (sampo graph blast): " + "; ".join(bits) + "."


def blast(p, paths, as_of=None, con=None):
    con = con or db(p)
    marks = ",".join("?" * len(paths))
    ins = _edges(con, as_of, f" AND dst IN ({marks})", paths)
    outs = _edges(con, as_of, f" AND src IN ({marks})", paths)
    co = collections.Counter()
    for s, d, k, t, w in outs:
        if k == "cochange" and d not in paths:
            co[d] += w
    titles = {r[0]: (r[1], r[2]) for r in con.execute("SELECT id, type, title FROM tasks")}
    fixes = sorted({s[5:] for s, d, k, t, w in ins if k == "touched" and titles.get(s[5:], ("",))[0] == "FIX"})
    return {"paths": paths,
            "dependents": sorted({s for s, d, k, t, w in ins if k == "uses" and s not in paths}),
            "tests": sorted({s for s, d, k, t, w in ins if k == "tests"}),
            "cochange": [f for f, _ in co.most_common(8)],
            "fixes": [{"id": t, "title": titles[t][1]} for t in fixes]}


def pack(p, text, as_of=None, top=15):
    """Files ranked for a task: personalized PageRank over the graph, restarting at the files whose paths and the past
    tasks whose titles share the text's words."""
    con = db(p)
    words = _words(text)
    nbr = collections.defaultdict(lambda: collections.defaultdict(float))
    for s, d, k, t, w in _edges(con, as_of):
        nbr[s][d] += w
        nbr[d][s] += w
    seeds = collections.Counter()
    for node in list(nbr):
        if not node.startswith("task:"):
            seeds[node] += len(words & _words(node.replace("/", " ").replace("_", " ").replace(".", " ")))
    for tid, title, created in con.execute("SELECT id, title, created FROM tasks"):
        if (not as_of or created < as_of) and f"task:{tid}" in nbr:
            seeds[f"task:{tid}"] += len(words & _words(title))
    seeds = {k: v for k, v in seeds.items() if v}
    if not seeds:
        return []
    total = sum(seeds.values())
    restart = {k: v / total for k, v in seeds.items()}
    rank = dict(restart)
    for _ in range(25):  # power iteration, restart probability 0.25
        nxt = collections.defaultdict(float, {k: 0.25 * v for k, v in restart.items()})
        for u, r in rank.items():
            out = nbr[u]
            tw = sum(out.values())
            for v, w in out.items():
                nxt[v] += 0.75 * r * w / tw
        rank = nxt
    files = [(f, s) for f, s in rank.items() if not f.startswith("task:") and os.path.exists(os.path.join(p.root, f))]
    return sorted(files, key=lambda x: -x[1])[:top]


def cmd_graph(args):
    from sampo import cli
    p = cli.resolve(args)
    if args.action == "build":
        try:
            con = db(p)
        finally:  # the lock only guards a build in flight (T-2277)
            try:
                os.remove(os.path.join(p.dir, "graph.building"))
            except OSError:
                pass
        n = con.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        return cli.out(args, {"edges": n}, f"Graph: {n} edges (code, tests, tasks, co-change).")
    if not args.words:
        raise cli.UsageError("fm graph blast PATH… | pack \"<task text>\" [--as-of ISO time]")
    as_of = _utc(args.as_of) if args.as_of else None
    if args.action == "blast":
        paths = [os.path.relpath(os.path.abspath(w), p.root) if os.path.exists(w) else w for w in args.words]
        b = blast(p, paths, as_of)
        dep, depn = c.more(b["dependents"], 15)
        tst, tstn = c.more(b["tests"], 15)
        text = (f"Blast radius of {', '.join(paths)}" + (f" as of {as_of}" if as_of else "") + ":\n"
                f"  dependents: {', '.join(dep) or 'none'}{f' ({depn})' if depn else ''}\n"
                f"  tests: {', '.join(tst) or 'none linked'}{f' ({tstn})' if tstn else ''}\n"
                f"  co-change: {', '.join(b['cochange']) or 'none'}\n"
                f"  fixes that touched it: " + (", ".join(f"{x['id']} {c.fit(x['title'], 50)}" for x in b["fixes"][-6:])
                                                or "none"))
        return cli.out(args, b, text)
    files = pack(p, " ".join(args.words), as_of, args.top)
    return cli.out(args, {"files": [{"path": f, "score": round(s, 4)} for f, s in files]},
                     ("Read first (personalized PageRank from the words, past tasks and the code graph):\n"
                      + "\n".join(f"  {i}. {f}" for i, (f, _) in enumerate(files, 1))) if files else
                     "Nothing in the graph shares those words.")
