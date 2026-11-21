"""T-2387 (Mission 7, #10): fm prove --data. A data command (a backfill, an import, a cleanup) runs on a throwaway copy of
the tree and its sqlite or CSV data; each declared invariant is a query that returns the violating rows, run before and
after. Pass: the command exited 0 and every check returns no rows after. The checks only read (an authorizer allows
SELECT and nothing else); the command runs with no connection variable or credential, HOME and its working tree in the
throwaway folder, and no network where unshare allows; a database path that resolves outside the copy is refused. A CSV
is loaded into a throwaway sqlite table named after the file (an empty cell is NULL). fm task finish runs it when the
task touched data files in a trusted tree and its brief has an Invariants section: the data command, then a check a line."""
import csv
import os
import re
import shutil
import sqlite3
import time

from sampo import core as c

SECTION = "Invariants"
DATA = re.compile(r"(?i)\.(db|sqlite3?|csv)$")
RUN_T, FINISH_T, ROWS, CAP = 120, 60, 5, 10000  # seconds; violating rows shown; rows counted at most
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "target"}
READ_OPS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, getattr(sqlite3, "SQLITE_RECURSIVE", 33)}


class Refused(Exception):
    pass


class Nothing(Exception):
    pass


def _db(work, name):
    """The data file in the copy: --db's (inside the copy, or refused), else the only sqlite file, else the only CSV."""
    if name:
        path = os.path.realpath(os.path.join(work, name))
        if not path.startswith(work + os.sep):
            raise Refused(f"{name} resolves outside the throwaway copy: only the tree's own data is proved")
        if not os.path.isfile(path):
            raise Nothing(f"{name}: no such file in the tree")
        return path
    found = {"db": [], "csv": []}
    for d, dirs, files in os.walk(work):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
        for f in files:
            m = DATA.search(f)
            if m:
                found["csv" if m.group(1).lower() == "csv" else "db"].append(os.path.join(d, f))
    for kind in ("db", "csv"):
        if len(found[kind]) == 1:
            return found[kind][0]
        if found[kind]:
            raise Nothing(f"{len(found[kind])} {kind} files here: name one with --db FILE")
    raise Nothing("no sqlite or CSV file in the tree: name one with --db FILE")


def _ident(name):
    return '"' + name.replace('"', '""') + '"'


def _open(db, end):
    """A read-only connection to db: the sqlite file itself (mode=ro), or the CSV loaded into memory."""
    if db.lower().endswith(".csv"):
        conn = sqlite3.connect(":memory:")
        with open(db, newline="", encoding="utf-8", errors="replace") as f:
            rows = list(csv.reader(f))
        if rows:
            head = [h or f"c{i}" for i, h in enumerate(rows[0], 1)]
            table = os.path.splitext(os.path.basename(db))[0]
            conn.execute(f"CREATE TABLE {_ident(table)} ({', '.join(_ident(h) for h in head)})")
            conn.executemany(f"INSERT INTO {_ident(table)} VALUES ({', '.join('?' * len(head))})",
                             [[v if v != "" else None for v in (r + [""] * len(head))[:len(head)]] for r in rows[1:]])
    else:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.set_authorizer(lambda op, *a: sqlite3.SQLITE_OK if op in READ_OPS else sqlite3.SQLITE_DENY)
    conn.set_progress_handler(lambda: time.time() > end, 10000)
    return conn


def _look(db, checks, end):
    """({table: rows}, [(violating count, first rows)]) of db now. A check that can't run raises Nothing."""
    conn = _open(db, end)
    try:
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
        tables = {n: conn.execute(f"SELECT count(*) FROM {_ident(n)}").fetchone()[0] for n in names}
        out = []
        for i, sql in enumerate(checks, 1):
            try:
                rows = conn.execute(sql).fetchmany(CAP)
            except sqlite3.Error as e:
                raise Nothing(f"check {i} doesn't run here ({c.fit(c.plain(str(e)), 120)}): a check is one query that "
                              "reads and returns the violating rows")
            out.append((len(rows), [[v if isinstance(v, (int, float)) or v is None else c.fit(c.plain(str(v)), 80)
                                     for v in r] for r in rows[:ROWS]]))
        return tables, out
    finally:
        conn.close()


def prove(root, cmd, checks, allowed, db=None, limit=RUN_T):
    """The result dict: verdict pass, fail, refused (untrusted, a path outside the copy) or nothing (no check, no data,
    a check that can't run); the real tree and its data are never opened for writing."""
    from sampo import migrate
    from sampo import prove as sprove
    r = {"root": root, "cmd": cmd, "checks": [], "tables": {}}
    if not allowed:
        return r | {"verdict": "refused", "why": sprove.no_run()}
    if not checks or not (cmd or "").strip():
        return r | {"verdict": "nothing", "why": "a data proof needs the data command (--run CMD) and one or more "
                                                 "--check SQL (each returns the violating rows)"}
    tmp, work = sprove._copy(root)
    end = time.time() + limit
    try:
        work = os.path.realpath(work)
        path = _db(work, db)
        r["db"] = os.path.relpath(path, work)
        before_t, before = _look(path, checks, end)
        env = migrate._env(root, tmp)  # no connection variable or credential; HOME the throwaway folder
        argv = migrate._sandbox(env, root, work, end) + ["bash", "-c", cmd]
        out, err, rc, why = migrate._exec(argv, work, env, end)
        if not os.path.isfile(path):
            return r | {"verdict": "fail", "exit": rc, "why": f"the command removed {r['db']}"}
        after_t, after = _look(path, checks, end)
        r["tables"] = {n: [before_t.get(n, 0), after_t.get(n, 0)] for n in sorted(set(before_t) | set(after_t))}
        r["checks"] = [{"sql": c.fit(c.plain(sql), 200), "before": b[0], "after": a[0], "rows": a[1]}
                       for sql, b, a in zip(checks, before, after)]
        r["exit"] = rc
        bad = [i for i, x in enumerate(r["checks"], 1) if x["after"]]
        if rc or why:
            said = migrate._say(why or err or out or f"exit {rc}", tmp)
            return r | {"verdict": "fail", "why": f"the command failed: {said}"}
        if bad:
            return r | {"verdict": "fail", "why": "; ".join(f"check {i} still finds {r['checks'][i - 1]['after']} row(s)"
                                                         for i in bad)}
        return r | {"verdict": "pass", "why": f"the command passed and all {len(checks)} check(s) find no rows after"}
    except Refused as e:
        return r | {"verdict": "refused", "why": str(e)}
    except Nothing as e:
        return r | {"verdict": "nothing", "why": str(e)}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def lines(r):
    out = [f"{r['verdict']}: {r['why']}"]
    out += [f"  check {i}: {x['before']} row(s) before, {x['after']} after" + (f"; e.g. {x['rows'][:3]}" if x["rows"] else "")
            for i, x in enumerate(r["checks"], 1)]
    out += [f"  {n}: {b} row(s) before, {a} after" for n, (b, a) in r["tables"].items()]
    return out


def cmd_data(args, root, allowed):
    from sampo import cli
    if args.run and args.run.strip():
        p = c.find_project(os.getcwd())  # the command runs: the guard judges it first, with the task's grants
        cli.guard_stored(p, c.active_brief_fast(p, p.lane) if p else None, args.run)
    r = prove(root, args.run, args.check or [], allowed, db=args.db)
    cli.out(args, r, c.plain_lines("\n".join(lines(r))))
    return {"pass": 0, "fail": 1}.get(r["verdict"], 2)


def invariants(b):
    """(the data command, [checks]) from the brief's Invariants section, or None."""
    rows = [re.sub(r"^\s*(?:[-*]\s+|\d+[.)]\s+)", "", x).strip().strip("`") for x in b.section(SECTION).splitlines()]
    rows = [x for x in rows if x]
    return (rows[0], rows[1:]) if len(rows) > 1 else None


def for_task(p, b, say=print):
    """(code, text) for fm task finish, or None: the brief's invariants proved when the task touched data files in a
    trusted tree. An untrusted tree or a proof that can't run is said, never a failure and never evidence."""
    from sampo import infra
    from sampo import prove as sprove
    import shlex
    inv = invariants(b)
    if not inv:
        return None
    try:
        words = {os.path.normpath(w) for w in shlex.split(inv[0])}
    except ValueError:
        words = set()
    if not any(DATA.search(f) or os.path.normpath(f) in words for f in infra.touched(p, b)):  # data, or the command's file
        return None
    if not (sprove.trusted(p.root) or sprove._lane_trusted(p, p.root)):
        say(sprove.words("data: this task has invariants; proving them runs its data command, so it needs your yes "
                         "once per tree: sampo prove PATH --yes-run"))
        return None
    r = prove(p.root, inv[0], inv[1], True, limit=FINISH_T)
    if r["verdict"] in ("pass", "fail"):
        return {"pass": 0, "fail": 1}[r["verdict"]], "\n".join(lines(r))
    say("data: " + r["verdict"] + ": " + r["why"])
    return None
