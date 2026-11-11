"""T-4200: the concerns ledger (T-4188 part A): what the user has raised, held until it is shown to them. A concern is
folded from the steer meter's booked rows (steermeter.rows): a message whose repeat_of names an earlier one (its uuid,
its words or its concern) joins that one's concern, so one concern is said as many times as its chain has messages.
The meter decides which message repeats which (its repeat_of, a K- id of an open concern, or new); `sampo concerns
fold` asks it for the booked rows that have no decision yet, and records each answer as a fold event. A split (`sampo
concerns split`) cuts the links where the two asks say different things, for a model that linked unrelated asks.
Linked tasks are the captures made from a message (the capture's session, within LINK_S after it) and the tasks whose
brief names the concern id; a task's close changes nothing here. Only `show` with a proof that checks (a git ref on a
remote-tracking branch, a URL that answers, a path that exists, a number its command prints) marks a concern shown; a
message said again after that reopens it. `answer` takes a pure question out of the open list.

The events (show, answer, split, fold) are write-once lines in the project's concerns.jsonl; every read folds them with
the rows, so no state is edited in place and two sessions can't lose each other's write. Nothing here calls a model
except fold (the meter's reader, under its lock, never on a hook), and nothing gates a close or a merge: the ledger
tells the agent what the user is still waiting on."""
import hashlib
import json
import os
import re
import shlex
import subprocess

from sampo import core as c

CONCERNS = "concerns.jsonl"
LINK_S = 1800      # a capture links to the newest message of its session before it, within this many seconds
CHUNK = 256 * 1024  # the ledger is read backwards in pieces of this size, until a line older than the window
HELD_N = 40        # the open concerns a message may be said to repeat: the newest this many
PROOF_S = 120      # a num proof's command gets this long
URL_S = 10         # a url proof gets this long to answer
TEXT_N = 400       # an answer or a proof is kept to this many characters
PROOF_HELP = ("a proof is git:<ref> (a commit on a remote-tracking branch), url:<address> (it answers), path:<file> "
              "(it exists) or num:<value> via <command> (the command exits 0 and prints the value)")
_ID = re.compile(r"K-[0-9A-F]{6}")
_NUM = re.compile(r"^\s*([-+]?\d+(?:[.,]\d+)*)\s+via\s+(.+)$", re.S)


class Refused(ValueError):
    """A concern verb's refusal, with the reason a person reads."""


def concern_id(session, uuid):
    """K- and six hex of the first message's (session, uuid): the same id on every read, and it names the concern in a
    brief's raw request."""
    return "K-" + hashlib.sha1(f"{session}\0{uuid}".encode("utf-8")).hexdigest()[:6].upper()


def _norm(s):
    return " ".join(str(s or "").lower().split())


def _epoch(ts):
    t = c.parse_ts(ts) if isinstance(ts, str) else None
    return t.timestamp() if t else None


def _events(p):
    out = []
    try:
        with open(os.path.join(p.dir, CONCERNS), encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if isinstance(e, dict) and isinstance(e.get("concern"), str) and isinstance(e.get("event"), str) \
                        and isinstance(e.get("ts"), str):
                    out.append(e)
    except OSError:
        pass
    return out


def _rows(p):
    """The meter's rows that carry a concern (steer or new-request), oldest first; the meter's own rules apply. A row
    with no repeat_of key (booked before the meter decided it) takes its fold event's decision, when it has one."""
    from sampo import steermeter as sm
    rs = [r for r in sm._read_rows(sm.rows(p)) if r.get("kind") in sm.NEEDS_FIELDS
          and isinstance(r.get("concern"), str) and r["concern"].strip()]
    folded = {e["concern"]: e["same_as"] for e in _events(p) if e["event"] == "fold" and isinstance(e.get("same_as"), str)}
    for r in rs:
        if "repeat_of" not in r and concern_id(r.get("session"), r.get("uuid")) in folded:
            same = folded[concern_id(r.get("session"), r.get("uuid"))]
            r["repeat_of"] = same if same.startswith("K-") else None  # new and unread are both a concern of its own
    return sorted(rs, key=lambda r: r["at"])  # stable: equal stamps keep the book's order


def _parents(rows):
    """Per row, the newest earlier row its repeat_of names (a K- id of its concern, its uuid, its words or its concern);
    None when none."""
    keys = [(_norm(r.get("uuid")), _norm(r.get("text")), _norm(r.get("concern")),
             _norm(concern_id(r.get("session"), r.get("uuid")))) for r in rows]
    par = []
    for i, r in enumerate(rows):
        rep, hit = _norm(r.get("repeat_of")), None
        if rep:
            for j in range(i - 1, -1, -1):
                u, t, k, kid = keys[j]
                if rep in (u, t, k, kid) or (len(rep) >= 16 and rep in t) or (len(t) >= 16 and t in rep):
                    hit = j
                    break
        par.append(hit)
    return par


def _components(rows, par, cuts):
    """The root row index of each row's concern: a row joins its parent's unless that concern was split (cuts: its id)
    and the two asks say different things."""
    root = []
    for i, r in enumerate(rows):
        j = par[i]
        if j is None:
            root.append(i)
            continue
        rid = root[j]
        cut = concern_id(rows[rid]["session"], rows[rid]["uuid"]) in cuts \
            and _norm(rows[j]["concern"]) != _norm(r["concern"])
        root.append(i if cut else rid)
    return root


def _briefs_naming(p, briefs):
    """{concern id: [task ids whose raw request names it]} over the briefs (loaded when not given)."""
    out = {}
    for b in (c.load_briefs(p) if briefs is None else briefs):
        try:
            raw = b.section("Raw request") or ""
        except (AttributeError, TypeError):
            continue
        for cid in set(_ID.findall(raw.upper())):
            out.setdefault(cid, []).append(b.id)
    return out


_TS = re.compile(rb'"ts": "([^"]+)"')


def _captures_since(p, floor):
    """The capture events at or after floor (epoch). The ledger is read backwards from its end in CHUNK pieces and
    stops at the first line older than floor, so the window is the time a link needs, not a count of lines: a count
    of the newest lines dropped the older captures of a long project (T-4200 review: 157 of 1280 read)."""
    try:
        f = open(os.path.join(p.dir, "ledger.jsonl"), "rb")
    except OSError:
        return []
    out, rest, done = [], b"", False
    with f:
        pos = f.seek(0, os.SEEK_END)
        while not done:
            if pos > 0:
                step = min(CHUNK, pos)
                pos -= step
                f.seek(pos)
                parts = (f.read(step) + rest).split(b"\n")
                rest, parts = parts[0], parts[1:]  # the first piece may start mid-line: it is read with the next chunk
            else:
                parts, rest, done = [rest], b"", True
            for raw in reversed(parts):
                m = _TS.search(raw)
                ts = _epoch(m.group(1).decode("ascii", "replace")) if m else None
                if ts is not None and ts < floor:
                    return out
                if m is None or b'"capture"' not in raw:
                    continue
                try:
                    e = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(e, dict) and e.get("event") == "capture":
                    out.append(e)
    return out


def _captured_from(p, rows, root):
    """{root row index: [task ids]}: a capture (ledger event) links to the newest message of its session before it,
    within LINK_S. The ledger is read back to LINK_S before the oldest message, once."""
    if not rows:
        return {}
    by_sess = {}
    for i, r in enumerate(rows):
        by_sess.setdefault(r["session"], []).append(i)
    out = {}
    floor = min(t for t in (_epoch(r["at"]) for r in rows) if t is not None) - LINK_S
    for e in _captures_since(p, floor):
        sess, ts, tid = e.get("session_id"), _epoch(e.get("ts")), e.get("task")
        if not sess or ts is None or not tid:
            continue
        best = None
        for i in by_sess.get(sess, ()):
            t = _epoch(rows[i]["at"])
            if t is not None and t <= ts and ts - t <= LINK_S:
                best = i  # rows are oldest first: the last one that fits is the nearest
        if best is not None:
            out.setdefault(root[best], []).append(tid)
    return out


def fold(p, briefs=None):
    """The concerns of this project, oldest first: {id, text, surface, opened, last_said, said, tasks, state ("open",
    "shown" or "answered"), reopened (said after its last show or answer), proof, checked}. Empty with no rows."""
    rows = _rows(p)
    if not rows:
        return []
    events = _events(p)
    cuts = {e["concern"] for e in events if e["event"] == "split"}
    par = _parents(rows)
    root = _components(rows, par, cuts)
    groups = {}
    for i in range(len(rows)):
        groups.setdefault(root[i], []).append(i)
    by_root = _captured_from(p, rows, root)
    named = _briefs_naming(p, briefs)
    out = []
    for r0, members in groups.items():
        first, last = rows[r0], rows[members[-1]]
        cid = concern_id(first["session"], first["uuid"])
        tasks = set(by_root.get(r0, ())) | set(named.get(cid, ()))
        state, reopened, proof, checked = "open", False, None, None
        ev = [e for e in events if e["concern"] == cid and e["event"] in ("show", "answer")]
        if ev:
            e = ev[-1]
            state = "shown" if e["event"] == "show" else "answered"
            proof, checked = e.get("proof"), e.get("checked")
            if last["at"] > e["ts"]:  # said again after the proof or the answer: it is open again
                state, reopened = "open", True
        out.append({"id": cid, "text": first["concern"], "surface": last.get("surface"), "opened": first["at"],
                    "last_said": last["at"], "said": len(members), "tasks": sorted(tasks), "state": state,
                    "reopened": reopened, "proof": proof, "checked": checked})
    return out


def open_concerns(p, briefs=None):
    return [x for x in fold(p, briefs) if x["state"] == "open"]


def held(p):
    """{id, text} of the open concerns, oldest first, the newest HELD_N: the ones a message may be said to repeat."""
    return [{"id": x["id"], "text": x["text"]} for x in open_concerns(p)][-HELD_N:]


def record_fold(p, cid, same_as):
    """A fold event: the meter's decision for a row that had none (same_as: a K- id it repeats, 'new', or 'unread' when
    no reply could be read twice). Write-once; _rows reads it as the row's repeat_of."""
    _write(p, {"event": "fold", "concern": cid, "same_as": same_as, "ts": c.now()})


def _exists(p, cid):
    cid = (cid or "").strip().upper()
    if cid not in {x["id"] for x in fold(p)}:
        raise Refused(f"no concern {cid or '(none)'}: sampo concerns lists them")
    return cid


def _write(p, event):
    c.append_line(os.path.join(p.dir, CONCERNS), json.dumps(event, ensure_ascii=False))


# ---------------------------------------------------------------- proofs

def check_proof(p, proof):
    """(True, what was checked) or (False, why). Only a proof that can be checked here is one."""
    kind, sep, rest = (proof or "").strip().partition(":")
    kind, rest = kind.strip().lower(), rest.strip()
    if not sep or not rest:
        return False, PROOF_HELP
    if kind == "git":
        return _git_proof(p, rest)
    if kind == "url":
        if not re.match(r"https?://\S+$", rest):
            return False, "url: needs an http or https address"
        return (True, f"{rest} answers") if _url_answers(rest) else (False, f"{rest} does not answer")
    if kind == "path":
        path = rest if os.path.isabs(rest) else os.path.join(p.root, rest)
        return (True, f"{rest} exists") if os.path.exists(path) else (False, f"no such path: {rest}")
    if kind == "num":
        return _num_proof(p, rest)
    return False, PROOF_HELP


def _git_proof(p, ref):
    if ref.startswith("-"):
        return False, f"git:{ref} is not a ref"
    sha = c._git(p.root, "rev-parse", "--verify", "--quiet", ref + "^{commit}", proc=True, timeout=10)
    if sha.returncode:
        return False, f"git:{ref} does not resolve to a commit"
    names = [x for x in c._git(p.root, "branch", "-r", "--contains", sha.stdout.strip(), timeout=10).split()
             if x not in ("->",) and not x.startswith("origin/HEAD")]
    if not names:
        return False, f"git:{ref} is on no remote-tracking branch: push it first"
    on = ", ".join(names[:3]) + (f" and {len(names) - 3} more" if len(names) > 3 else "")
    return True, f"git:{ref} is on {on}"


def _url_answers(url):
    import urllib.request
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": "sampo"})
    try:
        with urllib.request.urlopen(req, timeout=URL_S) as r:
            return r.status < 400
    except (OSError, ValueError):
        return False


def _num_proof(p, rest):
    m = _NUM.match(rest)
    if not m:
        return False, "a number must come with its command: num:<value> via <command>, the command printing the value"
    value, cmd = m.group(1), m.group(2).strip()
    try:
        argv = shlex.split(cmd)
    except ValueError as e:
        return False, f"the command does not parse: {e}"
    if not argv:
        return False, "a number must come with its command"
    try:  # no shell: the command is argv, run in the project's root, bounded in time
        run = subprocess.run(argv, cwd=p.root, capture_output=True, text=True, timeout=PROOF_S,
                             stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"the command did not run: {e}"
    if run.returncode:
        return False, f"the command exited {run.returncode}"
    if not re.search(r"(?<![\w.,])" + re.escape(value) + r"(?![\w.,])", run.stdout or ""):
        return False, f"the command does not print {value}"
    return True, f"{value} printed by {argv[0]}"


# ---------------------------------------------------------------- the verbs

def show(p, cid, proof):
    """Marks a concern shown when its proof checks; a refused proof writes nothing."""
    cid = _exists(p, cid)
    ok, why = check_proof(p, proof)
    if not ok:
        raise Refused(f"not shown: {why}")
    _write(p, {"event": "show", "concern": cid, "proof": c.fit(c.redact(proof), TEXT_N), "checked": why,
               "ts": c.now()})
    return {"id": cid, "state": "shown", "checked": why}


def answer(p, cid, text):
    """A pure question's answer: the concern leaves the open list (state answered), no task or proof needed."""
    cid = _exists(p, cid)
    text = " ".join((text or "").split())
    if not text:
        raise Refused("an answer needs its text: sampo concerns answer ID \"<text>\"")
    _write(p, {"event": "answer", "concern": cid, "text": c.fit(c.redact(text), TEXT_N), "ts": c.now()})
    return {"id": cid, "state": "answered"}


def split(p, cid):
    """Cuts the links in a concern where its asks say different things (unrelated asks the model merged). Refused when
    the cut would leave it whole; returns the ids the concern becomes."""
    cid = _exists(p, cid)
    rows = _rows(p)
    cuts = {e["concern"] for e in _events(p) if e["event"] == "split"}
    par = _parents(rows)
    now_root = _components(rows, par, cuts)
    r0 = next(i for i in range(len(rows)) if concern_id(rows[i]["session"], rows[i]["uuid"]) == cid
              and now_root[i] == i)
    members = [i for i in range(len(rows)) if now_root[i] == now_root[r0]]
    after = _components(rows, par, cuts | {cid})
    pieces = sorted({after[i] for i in members})
    if len(pieces) < 2:
        raise Refused(f"nothing to split in {cid}: every ask in it says the same thing")
    _write(p, {"event": "split", "concern": cid, "ts": c.now()})
    return {"id": cid, "into": [concern_id(rows[k]["session"], rows[k]["uuid"]) for k in pieces]}


def _hours(opened):
    t = _epoch(opened)
    return None if t is None else max(0, int((c.clock().timestamp() - t) // 3600))


def cmd_concerns(args):
    from sampo import cli
    p = cli.resolve(args)
    action = getattr(args, "action", None) or "open"
    try:
        if action == "show":
            if not args.id or args.proof is None:
                raise cli.UsageError('sampo concerns show ID --proof "<what the user can now see>" (' + PROOF_HELP + ")")
            got = show(p, args.id, args.proof)
            return cli.out(args, got, f"{got['id']} shown: {got['checked']}")
        if action == "answer":
            got = answer(p, args.id, args.text)
            return cli.out(args, got, f"{got['id']} answered")
        if action == "split":
            got = split(p, args.id)
            return cli.out(args, got, f"{got['id']} split into " + ", ".join(got["into"]))
        if action == "fold":
            from sampo import steermeter
            got = steermeter.fold_rows(p)
            return cli.out(args, got, f"fold: {got['asked']} asked: {got['same']} said again, {got['new']} new, "
                           f"{got['unread']} unread, {got['left']} left" + ("" if got["model"] else " (no local model up)"))
    except Refused as e:
        raise cli.UsageError(str(e))
    rows = fold(p)
    shown = rows if getattr(args, "all", False) else [x for x in rows if x["state"] == "open"]
    lines = []
    for x in shown:
        task = ", ".join(x["tasks"]) or "no task"
        lines.append(f"{x['id']}  said {x['said']}x  open {_hours(x['opened'])} h  surface: {x['surface'] or 'none'}"
                     f"  {task}  {x['state']}{' (said again)' if x['reopened'] else ''}  {x['text']}")
    cli.out(args, {"concerns": shown}, "\n".join(lines) if lines else "No open concerns.")
