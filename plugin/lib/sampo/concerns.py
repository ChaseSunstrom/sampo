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

The events (show, answer, split, fold, repeat, remeasure) are write-once lines in the project's concerns.jsonl; every
read folds them with the rows, so no state is edited in place and two sessions can't lose each other's write. Nothing
here calls a model except fold (the meter's reader, under its lock, never on a hook). The ledger tells the agent what the
user is still waiting on, and gates one close (T-4189).

T-4189: a concern said again after its linked task closed, or after a proof was shown for it, or whose shown number is
worse a day later, was fixed on the wrong cause model. `repeated` finds those (the proof is kept, marked refuted), `act`
reopens the closed task (or captures a linked FIX when the close is old or there is none) with one first step, "look at
the real surface", and `surface_gaps` is the one gate: that task cannot close on unit-test evidence alone. The meaning
of "the same concern" is what fold already decides; nothing here reads the words of a message."""
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
PLANS = "plans.json"  # T-4524: the open concerns' plan documents, written by note_plans (no model) for the drive's hook path
PLAN_RE = re.compile(r"(?<![\w/.-])((?:[\w-]+/)*[\w-][\w.-]*\.md)(?![\w/-])")  # a path the user names: PLAN.md, docs/p.md
PHASE_RE = re.compile(r"^#{1,6}[ \t]*Phase[ \t]+(\d+[a-z]?)\b[ \t:.\-]*(.*)$", re.M | re.I)  # '## Phase 2: the page'
PLAN_MAX = 200 * 1024  # a plan document is read this far, at most, on the hook path
TEXT_N = 400       # an answer or a proof is kept to this many characters
REPEAT_DAYS = 7    # a close this recent is reopened; an older one is left closed and a linked FIX is captured
REMEASURE_S = 86400  # a shown number is read again once, this long after it was shown
WORSE_PCT = 10     # a re-read number is worse when it moved this far (of the shown value) the wrong way
SURFACE_STEP = "Look at the real surface first"
# the commands that look at what the user sees (a command's words, its program's name only); a project adds its own as
# meta.json "surface_commands" (["make deploy-staging"]). Anything else, a test run above all, is not a surface run.
SURFACE_CMDS = (("sampo", "smoke"), ("fm", "smoke"), ("sampo", "shot"), ("fm", "shot"), ("sampo", "concerns", "show"),
                ("fm", "concerns", "show"), ("curl",), ("wget",))
PROOF_HELP = ("a proof is git:<ref> (a commit on a remote-tracking branch), url:<address> (it answers), path:<file> "
              "(it exists) or num:<value> via <command> (the command exits 0 and prints the value; num:<value> up via "
              "<command> when a higher number is better)")
_ID = re.compile(r"K-[0-9A-F]{6}")
_NUM = re.compile(r"^\s*([-+]?\d+(?:[.,]\d+)*)(\s+up)?\s+via\s+(.+)$", re.S)
_MARK = "\0"      # where the shown number sat in the line the command printed it on (a show event's template)


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
    "shown" or "answered"), reopened (said after its last show or answer, or its shown number worse a day later),
    proof, checked, refuted (the shown proof that a later report or reading refuted, else None), refuted_by ("said" or
    "remeasure"), refuted_at, said_at (when each message was said)}. Empty with no rows."""
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
        refuted = refuted_by = refuted_at = None  # T-4189: the proof a later report or reading refuted
        ev = [e for e in events if e["concern"] == cid and (e["event"] in ("show", "answer")
                                                            or (e["event"] == "remeasure" and e.get("worse")))]
        if ev:
            e = ev[-1]
            if e["event"] == "remeasure":  # the shown number was worse a day later: open again, its proof refuted
                shows = [x for x in ev if x["event"] == "show"]
                proof = shows[-1].get("proof") if shows else None
                state, reopened = "open", True
                refuted, refuted_by, refuted_at = proof, "remeasure", e["ts"]
                if last["at"] > e["ts"]:
                    refuted_by, refuted_at = "said", last["at"]
            else:
                state = "shown" if e["event"] == "show" else "answered"
                proof, checked = e.get("proof"), e.get("checked")
                if last["at"] > e["ts"]:  # said again after the proof or the answer: it is open again
                    state, reopened = "open", True
                    if e["event"] == "show":
                        refuted, refuted_by, refuted_at = proof, "said", last["at"]
        docs = sorted({d for i in members for d in _plan_names(rows[i].get("text"))})  # T-4524: the user's words
        out.append({"id": cid, "text": first["concern"], "surface": last.get("surface"), "opened": first["at"],
                    "last_said": last["at"], "said": len(members), "tasks": sorted(tasks), "state": state,
                    "reopened": reopened, "proof": proof, "checked": checked, "refuted": refuted,
                    "refuted_by": refuted_by, "refuted_at": refuted_at, "said_at": [rows[i]["at"] for i in members],
                    "named": docs})
    return out


def _plan_names(text):
    """The relative paths of the .md documents a message names (no absolute path, no '..' out of the repo)."""
    out = []
    for m in PLAN_RE.finditer(text or ""):
        d = os.path.normpath(m.group(1))
        if not os.path.isabs(d) and d != ".." and not d.startswith(".." + os.sep):
            out.append(d)
    return out


def _brief_text(b):
    try:
        return f"{b.title} {b.section('Raw request') or ''}"
    except (AttributeError, TypeError):
        return b.title or ""


def phases(text):
    """[(label, part)] of a plan document's '## Phase N: title' headings, in document order."""
    return [(f"Phase {m.group(1)}", f"{m.group(2).strip()}") for m in PHASE_RE.finditer(text or "")]


def note_plans(p):
    """T-4524: the plan documents the open concerns name, that exist in the repo, into plans.json. steermeter.tick runs
    this each round, detached; the drive's hook path reads the file (plan_next) and never folds the ledger. No model."""
    found = [{"concern": x["id"], "doc": d} for x in open_concerns(p) for d in x.get("named") or ()
             if os.path.isfile(os.path.join(p.root, d))]
    path = os.path.join(p.dir, PLANS)
    text = json.dumps({"plans": found}, ensure_ascii=False, indent=1) + "\n"
    try:
        same = open(path, encoding="utf-8").read() == text
    except OSError:
        same = False
    if not same:
        c.write_atomic(path, text)
    return found


def plan_next(p, briefs=None):
    """T-4524: the first part of an open concern's plan document that no done task names: {doc, part}, or None. Reads
    plans.json (note_plans), the plan document and the briefs; no model, no fold. A part is built when a done task's
    title or Raw request names both the document and the part's label ('PLAN.md Phase 1')."""
    try:
        with open(os.path.join(p.dir, PLANS), encoding="utf-8") as f:
            plans = json.load(f).get("plans") or []
    except (OSError, ValueError, AttributeError):
        return None
    briefs = c.load_briefs(p) if briefs is None else briefs
    for x in plans if isinstance(plans, list) else []:
        doc = x.get("doc") if isinstance(x, dict) else None
        if not isinstance(doc, str) or doc != os.path.normpath(doc) or doc.startswith("..") or os.path.isabs(doc):
            continue  # a hand-edited or stale entry is never read outside the repo
        try:
            with open(os.path.join(p.root, doc), encoding="utf-8", errors="replace") as f:
                text = f.read(PLAN_MAX)
        except OSError:
            continue  # the document is gone: nothing is left to queue from it
        built = [_brief_text(b) for b in briefs if b.meta.get("status") == "done"]
        named = re.compile(rf"(?<![\w/.-]){re.escape(doc)}(?![\w/-])")  # PLAN.md, not NEWPLAN.md
        built = [t for t in built if named.search(t)]
        for label, part in phases(text):
            if not any(re.search(rf"\b{re.escape(label)}\b", t, re.I) for t in built):
                return {"doc": doc, "label": label, "part": f"{label}: {part}" if part else label}
    return None


def open_concerns(p, briefs=None):
    return [x for x in fold(p, briefs) if x["state"] == "open"]


def queue_links(p, briefs=None, rows=None):
    """T-4201: {task id: {id, said, opened, text}} for the tasks linked to an open concern: the concern said most
    (then the oldest) when a task has several. core.order_queue reads it. Only the verbs that show the queue (next,
    queue, status) call this: it folds the ledger, which no hook's path may."""
    out = {}
    for x in open_concerns(p, briefs) if rows is None else rows:
        for t in x["tasks"]:
            cur = out.get(t)
            if cur is None or (-x["said"], x["opened"]) < (-cur["said"], cur["opened"]):
                out[t] = {"id": x["id"], "said": x["said"], "opened": x["opened"], "text": x["text"]}
    return out


def summary(p, briefs=None, rows=None):
    """T-4201: {open, twice, oldest: {id, hours, text}} of the open concerns, or None with none (nothing to say)."""
    rows = open_concerns(p, briefs) if rows is None else rows
    if not rows:
        return None
    oldest = min(rows, key=lambda x: x["opened"])
    return {"open": len(rows), "twice": sum(1 for x in rows if x["said"] >= 2),
            "oldest": {"id": oldest["id"], "hours": _hours(oldest["opened"]), "text": oldest["text"]}}


def lead(link):
    """The words `sampo next` and `sampo queue` put beside a linked task."""
    via = f" (for {link['via']}, which waits on it)" if link.get("via") else ""
    return f"{link['id']} said {link['said']}x: {c.fit(c.plain(' '.join(str(link['text']).split())), 80)}{via}"


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
    return _check_full(p, proof)[:2]


def _check_full(p, proof):
    """(ok, what was checked or why not, extra): extra is what a show event keeps of a num proof (value, up, template),
    so the number can be read again a day later (remeasure)."""
    kind, sep, rest = (proof or "").strip().partition(":")
    kind, rest = kind.strip().lower(), rest.strip()
    if not sep or not rest:
        return False, PROOF_HELP, {}
    if kind == "git":
        return (*_git_proof(p, rest), {})
    if kind == "url":
        if not re.match(r"https?://\S+$", rest):
            return False, "url: needs an http or https address", {}
        return (True, f"{rest} answers", {}) if _url_answers(rest) else (False, f"{rest} does not answer", {})
    if kind == "path":
        path = rest if os.path.isabs(rest) else os.path.join(p.root, rest)
        return (True, f"{rest} exists", {}) if os.path.exists(path) else (False, f"no such path: {rest}", {})
    if kind == "num":
        return _num_proof(p, rest)
    return False, PROOF_HELP, {}


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


def _run_argv(p, cmd):
    """(stdout, None) of a command run as argv (no shell) in the project's root, bounded in time; (None, why) when it
    does not parse, run, or exit 0."""
    try:
        argv = shlex.split(cmd)
    except ValueError as e:
        return None, f"the command does not parse: {e}"
    if not argv:
        return None, "a number must come with its command"
    try:
        run = subprocess.run(argv, cwd=p.root, capture_output=True, text=True, timeout=PROOF_S,
                             stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, f"the command did not run: {e}"
    if run.returncode:
        return None, f"the command exited {run.returncode}"
    return run.stdout or "", None


def _number(s):
    """The float of a printed number ('1,234' thousands, '1,5' a decimal comma), None when it is neither."""
    s = str(s).strip()
    if "," in s and "." not in s and not re.fullmatch(r"[-+]?\d{1,3}(,\d{3})+", s):
        s = s.replace(",", ".")
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def _num_proof(p, rest):
    m = _NUM.match(rest)
    if not m:
        return False, "a number must come with its command: num:<value> via <command>, the command printing the value", {}
    value, up, cmd = m.group(1), bool(m.group(2)), m.group(3).strip()
    out, why = _run_argv(p, cmd)
    if out is None:
        return False, why, {}
    at = re.compile(r"(?<![\w.,])" + re.escape(value) + r"(?![\w.,])")
    line = next((x for x in out.splitlines() if at.search(x)), None)
    if line is None:
        return False, f"the command does not print {value}", {}
    extra = {}
    if _number(value) is not None:  # what a later reading looks for: the same line, its number in the shown one's place
        extra = {"value": _number(value), "up": up, "template": c.fit(c.redact(at.sub(_MARK, line.strip(), count=1)), TEXT_N)}
    return True, f"{value} printed by {shlex.split(cmd)[0]}", extra


# ---------------------------------------------------------------- the verbs

def show(p, cid, proof):
    """Marks a concern shown when its proof checks; a refused proof writes nothing."""
    cid = _exists(p, cid)
    ok, why, extra = _check_full(p, proof)
    if not ok:
        raise Refused(f"not shown: {why}")
    _write(p, {"event": "show", "concern": cid, "proof": c.fit(c.redact(proof), TEXT_N), "checked": why,
               "ts": c.now(), **extra})
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


# ---------------------------------------------------------------- T-4189: a repeated report

REMEASURE_N = 3    # at most this many shown numbers are read again in one pass (each command may run PROOF_S)
EXEMPT = ("", "none")  # a concern whose surface is none has nothing to look at: no forced step


def _reading(p, e):
    """The number a show event's command prints now, on the line shaped as it was shown (its template, the number in
    the shown one's place), as a float; None when the command fails or no line has that shape."""
    kind, _, rest = str(e.get("proof") or "").strip().partition(":")
    m = _NUM.match(rest) if kind.strip().lower() == "num" else None
    tpl = e.get("template")
    if not m or not isinstance(tpl, str) or _MARK not in tpl:
        return None
    out, _ = _run_argv(p, m.group(3).strip())
    if out is None:
        return None
    head, _, tail = tpl.partition(_MARK)
    rx = re.compile(re.escape(head) + r"(?<![\w.,])([-+]?\d+(?:[.,]\d+)*)(?![\w.,])" + re.escape(tail))
    for line in out.splitlines():
        hit = rx.fullmatch(line.strip())
        if hit:
            return _number(hit.group(1))
    return None


def remeasure(p):
    """A shown number is read again once, REMEASURE_S after it was shown: the same command, the same line. Worse (WORSE_PCT
    of the shown value the wrong way: higher, or lower when the proof says up) writes a remeasure event with worse true,
    which fold reads as the concern open again with its proof refuted. Any other reading (better, equal, or none found)
    is written too, so a number is read a single time. [{id, before, after, worse}]."""
    pending = {}
    for e in _events(p):
        if e["event"] == "show":
            pending[e["concern"]] = e
        elif e["event"] in ("answer", "remeasure"):
            pending.pop(e["concern"], None)
    cutoff, out = c.clock().timestamp() - REMEASURE_S, []
    for cid, e in pending.items():
        t = _epoch(e["ts"])
        if len(out) >= REMEASURE_N or t is None or t > cutoff or not e.get("template") \
                or not isinstance(e.get("value"), (int, float)):
            continue
        before, after = float(e["value"]), _reading(p, e)
        slack = abs(before) * WORSE_PCT / 100
        worse = after is not None and (after < before - slack if e.get("up") else after > before + slack)
        _write(p, {"event": "remeasure", "concern": cid, "before": before, "after": after, "worse": worse,
                   "ts": c.now()})
        out.append({"id": cid, "before": before, "after": after, "worse": worse})
    return out


def repeated(p, briefs=None):
    """The concerns reported again that nobody has acted on yet: [{id, text, surface, said, trigger, when, task,
    closed_at, refuted}]. trigger: "remeasure" (its shown number got worse), "closed" (said again after its linked task
    closed; task is that task) or "proof" (said again after a proof was shown; refuted is that proof). Nothing for a
    concern whose linked task is still open (the work is under way) or that was acted on at or after the report (a repeat
    event's since). Meaning is fold's: a message is the same concern when the meter said so."""
    bs = list(c.load_briefs(p) if briefs is None else briefs)
    by_id = {b.id: b for b in bs}
    xs = [x for x in fold(p, bs) if x["tasks"] or x["refuted"]]
    if not xs:
        return []
    done_at = {}
    for e in c.ledger_events(p, kinds=("task_done",), since=min(x["opened"] for x in xs)):
        if e.get("task"):
            done_at[e["task"]] = max(done_at.get(e["task"], ""), str(e.get("ts") or ""))
    handled = {}
    for e in _events(p):
        if e["event"] == "repeat":
            handled[e["concern"]] = max(handled.get(e["concern"], ""), str(e.get("since") or ""))
    out = []
    for x in xs:
        linked = [by_id[t] for t in x["tasks"] if t in by_id]
        if any(b.status not in c.CLOSED for b in linked):
            continue
        closed = [(done_at[b.id], b) for b in linked if b.status == "done" and done_at.get(b.id)]
        closed_at, task = max(closed, key=lambda z: z[0]) if closed else (None, None)
        if x["refuted_by"] == "remeasure":
            kind, when = "remeasure", x["refuted_at"]
        elif task is not None and any(a > closed_at for a in x["said_at"]) and any(a <= closed_at for a in x["said_at"]):
            kind, when = "closed", x["last_said"]
        elif x["refuted_by"] == "said":
            kind, when = "proof", x["refuted_at"]
        else:
            continue
        if when > handled.get(x["id"], ""):
            out.append({"id": x["id"], "text": x["text"], "surface": x["surface"], "said": x["said"], "trigger": kind,
                        "when": when, "task": task.id if task else None, "closed_at": closed_at,
                        "refuted": x["refuted"]})
    return out


def _step_text(r):
    surface = c.fit(c.plain(" ".join(str(r["surface"] or "").split())), 60)
    return (f"{SURFACE_STEP} ({r['id']}): open the real thing the user looks at ({surface}) and record what you see "
            f"(sampo smoke, sampo shot URL, curl); the earlier cause was wrong or incomplete, so find the cause there "
            f"before changing anything")


def _earlier_fix(p, tid):
    sha = c._git(p.root, "log", "-1", "--format=%h", "-F", f"--grep=({tid})")
    return sha.strip() or "none found in git log"


def _reopen(p, b, r):
    """A closed task open again with the surface step current (kept, not added again, when it is there and not done);
    a concern with no surface only gets the log line."""
    exempt = _norm(r["surface"]) in EXEMPT
    said = c.fit(c.plain(" ".join(str(r["text"]).split())), 100)
    head = (f"re-measured on {r['when']}: the shown number got worse" if r["trigger"] == "remeasure"
            else f"reported again on {r['when']}")
    b.append_log(f"{head} ({r['id']}, said {r['said']}x: {said}); the earlier cause was wrong or incomplete; earlier fix: "
                 f"{_earlier_fix(p, b.id)}" + (f"; its shown proof {r['refuted']} is refuted" if r["refuted"] else "")
                 + ("; no reachable surface for this concern, so no forced step" if exempt else ""))
    if b.status in c.CLOSED:
        b.meta["status"] = "planned"
    if not exempt:
        mine = [s for s in b.steps() if s.text.startswith(SURFACE_STEP) and r["id"] in s.text and not s.done]
        b.set_current(mine[-1].n if mine else b.add_step(_step_text(r)))
    c.save_brief(p, b)


def _capture(p, r):
    """(brief, action): a FIX linked to the concern by its id, with the surface step; a FIX this concern already has
    (found by that id, even closed or archived) is reopened or left, not filed again."""
    from sampo import eco
    said = c.fit(c.plain(" ".join(str(r["text"]).split())), 100)
    why = {"remeasure": "its shown number got worse when read again", "proof": "said again after its proof was shown",
           "closed": "said again after its linked task closed"}[r["trigger"]]
    text = (f"{said} ({r['id']}, said {r['said']}x)\nCONTEXT: {r['id']} reported again on {r['when']}: {why}; the earlier "
            f"cause was wrong or incomplete" + (f"; the proof {r['refuted']} is refuted" if r["refuted"] else "")
            + (f"; the close was {r['closed_at']}" if r["closed_at"] else ""))
    b, new = eco.capture_once(p, text, "FIX", "discovered", key=f"CONTEXT: {r['id']} reported again")
    if new:
        if _norm(r["surface"]) not in EXEMPT:
            b.set_current(b.add_step(_step_text(r)))
            c.save_brief(p, b)
        return b, "captured"
    if b.status in c.CLOSED:
        _reopen(p, b, r)
        return b, "reopened"
    return b, "joined"


def act(p, briefs=None):
    """What a repeat calls for, once each: the linked task closed within REPEAT_DAYS is reopened with the surface step,
    anything else (an older close, no task, a proof refuted) gets a linked FIX. Writes a repeat event per concern acted
    on (its since is the report's time, so the next report is acted on again). [{id, task, action, trigger}]."""
    bs = list(c.load_briefs(p) if briefs is None else briefs)
    by_id = {b.id: b for b in bs}
    out = []
    for r in repeated(p, bs):
        b = by_id.get(r["task"]) if r["task"] else None
        age = c.clock().timestamp() - (_epoch(r["closed_at"]) or 0) if r["closed_at"] else None
        if b is not None and age is not None and age <= REPEAT_DAYS * 86400:
            _reopen(p, b, r)
            action = "reopened"
        else:
            b, action = _capture(p, r)
        _write(p, {"event": "repeat", "concern": r["id"], "task": b.id, "action": action, "trigger": r["trigger"],
                   "since": r["when"], "ts": c.now()})
        out.append({"id": r["id"], "task": b.id, "action": action, "trigger": r["trigger"]})
    return out


def _sig(p):
    out = []
    for name in ("steers.jsonl", CONCERNS):
        try:
            st = os.stat(os.path.join(p.dir, name))
            out.append([st.st_size, st.st_mtime_ns])
        except OSError:
            out.append(None)
    return out


def tick(p):
    """The model-free work of a pass: read due numbers again, then act on repeats. Only when the steer rows or this
    ledger changed since the last pass is the ledger folded (a quiet one costs two stats), and one pass at a time (a
    second does nothing). Paused: nothing. {"remeasured", "repeats"}."""
    import fcntl
    out = {"remeasured": [], "repeats": []}
    if c.panicked():
        return out
    os.makedirs(p.dir, exist_ok=True)
    fd = os.open(os.path.join(p.dir, "concerns.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return out
        out["remeasured"] = remeasure(p)
        path = os.path.join(p.dir, "concerns.tick")
        try:
            with open(path, encoding="utf-8") as f:
                last = json.load(f)
        except (OSError, ValueError):
            last = None
        if _sig(p) == last:
            return out
        out["repeats"] = act(p)
        c.write_atomic(path, json.dumps(_sig(p)))
        return out
    finally:
        os.close(fd)


def safe_tick(p):
    """tick for the steer meter's pass: a failure (a torn line, a full disk, a refused capture) is logged where the hooks'
    errors go and never stops the meter."""
    try:
        return tick(p)
    except Exception:
        _log_error("tick")
        return {"remeasured": [], "repeats": []}


def _log_error(where):
    import traceback
    try:
        path = os.path.join(c.state_dir(), "logs", "hooks.log")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        c.append_line(path, f"{c.now()} concerns.{where} {c.redact(traceback.format_exc()).rstrip()}\n")
    except OSError:
        pass


def _surface_cmd(p, cmd):
    """Whether a command's words hold one of SURFACE_CMDS or the project's own (meta surface_commands), by the programs'
    names only (python3 plugin/bin/sampo shot URL is `sampo shot`)."""
    def names(s):
        try:
            return [os.path.basename(w) for w in shlex.split(str(s))]
        except ValueError:
            return [os.path.basename(w) for w in str(s).split()]
    extra = c.read_meta(p).get("surface_commands")
    heads = [tuple(h) for h in SURFACE_CMDS] + [tuple(names(x)) for x in extra if isinstance(x, str) and x.strip()] \
        if isinstance(extra, list) else [tuple(h) for h in SURFACE_CMDS]
    words = names(cmd)
    return any(h and words[i:i + len(h)] == list(h) for h in heads for i in range(len(words) - len(h) + 1))


_RUN_LINE = re.compile(r"^- \(step (\d+)\) `(.*?)` → (.*)$")


def surface_gaps(p, b, then=()):
    """T-4189: the refusal lines for a task that carries a surface step (a concern reported again): the step needs a
    passing run, made by Sampo, of a command that looks at the real surface (SURFACE_CMDS or the project's own), or its
    concern shown (sampo concerns show). then: [(step n, command)] the close is about to run, counted as made. [] for any
    other task, without reading a ledger."""
    steps = [s for s in b.steps() if s.text.startswith(SURFACE_STEP)]
    if not steps:
        return []
    shown = None
    out = []
    for s in steps:
        m = _ID.search(s.text.upper())
        cid = m.group(0) if m else None
        cmds = []
        for line in b.evidence():
            ev = _RUN_LINE.match(line)
            if ev and int(ev.group(1)) == s.n and c._RAN_MARK in line and not ev.group(3).lstrip().startswith("✗"):
                cmds.append(ev.group(2))
        cmds += [cmd for n, cmd in then if n == s.n and cmd]
        if any(_surface_cmd(p, x) for x in cmds):
            continue
        if cid:
            if shown is None:
                shown = {x["id"] for x in fold(p, briefs=[]) if x["state"] == "shown"}
            if cid in shown:
                continue
        how = (f"look at the real surface and record it: sampo task evidence {b.id} --step {s.n} --run \"sampo smoke\" "
               f"(or sampo shot URL, curl URL), or sampo concerns show {cid or 'K-ID'} --proof \"<what the user can now see>\"")
        have = (f" has only {c.joinmore(['`' + x + '`' for x in cmds], 3)}: not a surface run; " if cmds
                else " has no run on the real surface; ")
        out.append(f"step {s.n} ({SURFACE_STEP}{f', {cid}' if cid else ''}){have}the concern was reported again, so "
                   f"unit tests alone cannot close it: {how}")
    return out


def safe_gaps(p, b, then=()):
    """surface_gaps for the close gates: a reader that breaks must not stop a close, so it fails open and logs."""
    try:
        return surface_gaps(p, b, then)
    except Exception:
        _log_error("surface_gaps")
        return []


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
