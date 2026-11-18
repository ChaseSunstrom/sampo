"""T-2374 (Mission 6, #5): the install-grant queue. `fm ask ID CATEGORY --later` records, on the task, a request only the
user can answer, and grants nothing: no dialog, no pending approval, no hook record. The request is a row in the task
brief's "Grant requests" section, one fixed format (`- [queued|granted|refused] {json}`), read by `rows` alone, so a
forged line is only text: no reader treats a row as an authorization. `fmhooks._grant` stays the only writer of
`approval_granted`; it calls `settle` so a real yes marks the matching row granted and resumes a blocked task.
A queued request never expires (APPROVAL_TTL doesn't apply: nothing is waiting on a reply). A no is remembered twice:
the row turns refused and the ledger has `grant_refused`, which `queue` checks before it queues the same thing again.
The ledger also has `grant_queued` (data name: the open miss it answers, else the pin or what; fmmiss reads it)."""
import json
import os
import re

from sampo import core as c

SECTION = "Grant requests"
_ROW = re.compile(r"^- \[(queued|granted|refused)\] (\{.*\})$")
ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]{1,63}")  # T-2383: what --needs-env takes: a variable's name, never a value
_TAIL = re.compile(rb"""\\?["']?\s*[=:]\s*\\?["']?([^\s"'\\,;]{8,200})""")  # T-2383: NAME's =value, quoted or not


def _clean(x, n):
    return c.fit(c.redact(c.plain(str(x or ""))).strip(), n)


def row(state, d):
    return f"- [{state}] {json.dumps(d, sort_keys=True, separators=(',', ':'))}"


def rows(b):
    """[(state, dict)] for each well-formed row of the brief's section; anything else in it is ignored."""
    out = []
    for line in b.section(SECTION).splitlines():
        m = _ROW.match(line)
        try:
            d = json.loads(m.group(2)) if m else None
        except ValueError:
            d = None
        if isinstance(d, dict) and isinstance(d.get("cats"), list) and all(isinstance(x, str) for x in d["cats"]) \
                and d["cats"] and isinstance(d.get("pin") or "", str) and isinstance(d.get("what") or "", str):
            out.append((m.group(1), d))
    return out


def _key(cats, pin, what):
    return (tuple(sorted(set(cats))), pin or what or "")


def _write(b, items):
    b.set_section(SECTION, "".join(row(s, d) + "\n" for s, d in items))


def refused_before(p, cats, pin, what=""):
    """The user said no to this request on some task of the project (the ledger's `grant_refused`)."""
    k = _key(cats, pin, what)
    return any(_key(d.get("cats") or [], d.get("pin"), d.get("what")) == k
               for e in c.ledger_events(p, ("grant_refused",)) for d in [e.get("data") or {}] if isinstance(d, dict))


def _evidence(p, b):
    """(the newest open capability miss, what fm which already searched) on this task, for the user's context."""
    from sampo import miss as smiss
    miss = (smiss.open_misses(p, b) or [None])[-1]
    seen = [e for e in c.ledger_tail(p, 1500, kinds=("tool_search",)) if e.get("event") == "tool_search" and e.get("task") == b.id]
    hits = ((seen[-1].get("data") or {}).get("hits") or {}) if seen else {}
    alts = [f"{k} x{n}" for k, n in hits.items() if isinstance(n, int) and n > 0]
    return ({"name": _clean(miss["name"], 60), "cmd": _clean(miss["cmd"], 120)} if miss else None), alts


def queue(p, tid, cats, why, pin=None, what="", fallback="", session=None, env=()):
    """Record the request on task tid. Returns (state, row dict): queued, dedupe (an identical one is still queued) or
    refused (the user said no before). Grants nothing and writes no file but the brief and the ledger."""
    cats = sorted(set(cats))
    with c.lock(p.dir):
        b = c.find_brief(p, tid)
        if not b:
            return "missing", {}
        items = rows(b)
        k = _key(cats, pin, what)
        if any(s == "queued" and _key(d["cats"], d.get("pin"), d.get("what")) == k for s, d in items):
            return "dedupe", next(d for s, d in items if s == "queued" and _key(d["cats"], d.get("pin"), d.get("what")) == k)
        if refused_before(p, cats, pin, what):
            return "refused", {"cats": cats, "pin": pin or "", "what": what}
        miss, alts = _evidence(p, b)
        d = {"cats": cats, "pin": pin or "", "what": _clean(what, 80), "why": _clean(why, 200) or "no reason given",
             "miss": miss, "alts": alts, "fallback": _clean(fallback, 200), "at": c.now(),
             **({"env": sorted(set(env))} if env else {})}
        _write(b, items + [("queued", d)])
        c.save_brief(p, b)
        c.log_event(p, "grant_queued", task=b.id, session=session,
                    data={"name": (miss or {}).get("name") or pin or what or cats[0], "allow": cats, "pin": pin or "",
                          "what": d["what"]})
        c.regen_views(p)
        return "queued", d


def settle(b, cats, pin, state="granted"):
    """In the caller's lock, before it saves b: the queued rows this answer covers become granted or refused. A yes also
    resumes a blocked or deferred task once none is left queued. Returns the rows it settled."""
    items, hit = rows(b), []
    for i, (s, d) in enumerate(items):
        if s == "queued" and set(d["cats"]) <= set(cats) and (d.get("pin") or "") == (pin or ""):
            items[i] = (state, d)
            hit.append(d)
    if hit:
        _write(b, items)
        if state == "granted" and not any(s == "queued" for s, _ in items) and b.status in ("blocked", "deferred"):
            b.meta["status"] = "planned" if b.acceptance() else "captured"
            for x in ("revisit", "recoveries", "lane_stop"):
                b.meta.pop(x, None)
            b.append_log("unblocked: the user's yes to a queued grant request")
    return hit


def refuse(p, b, cats, pin, session=None):
    """The user answered no to a request that was queued on b: remembered (row and ledger). The caller holds the lock."""
    hit = settle(b, cats, pin, "refused")
    if hit:
        c.save_brief(p, b)
        for d in hit:
            c.log_event(p, "grant_refused", task=b.id, session=session,
                        data={"allow": d["cats"], "cats": d["cats"], "pin": d.get("pin") or "", "what": d.get("what") or ""})
    return hit


def queued(p, briefs=None):
    """[(brief, row)] still queued on open tasks, oldest first: a closed task's grant would grant nothing."""
    out = []
    for b in briefs if briefs is not None else c.load_briefs(p):
        if b.status not in c.CLOSED and b.section(SECTION):  # T-3573: an indexed stub answers it without its file
            out += [(b, d) for s, d in rows(b) if s == "queued"]
    return sorted(out, key=lambda x: x[1].get("at") or "")


def command(b, d):
    pin = f" --pin {d['pin']}" if d.get("pin") else ""
    return f"fm ask {b.id} {' '.join(d['cats'])}{pin} --why \"{d['why'][:80]}\"; the user answers yes"


def text(d):
    """The row the way the user reads it: what is asked, the miss behind it, the alternatives and the fallback."""
    what = d.get("pin") or d.get("what") or ""
    parts = [f"{', '.join(d['cats'])}{' ' + what if what else ''}: {d.get('why') or 'no reason given'}"]
    if d.get("miss"):
        parts.append(f"miss: {d['miss'].get('name')}" + (f" ({d['miss']['cmd']})" if d["miss"].get("cmd") else ""))
    parts.append("already searched: " + ", ".join(d["alts"]) if d.get("alts") else "no alternatives searched")
    parts.append(f"meanwhile: {d['fallback']}" if d.get("fallback") else "no fallback named")
    if d.get("env"):
        parts.append(f"needs {', '.join(d['env'])} set outside the session (never pasted here)")
    return "; ".join(parts)


def unblocks(p, b, briefs=None):
    """What a yes frees: this task, and the open tasks that wait on it."""
    waiting = [x.id for x in (briefs if briefs is not None else c.load_briefs(p))
               if x.status not in c.CLOSED and b.id in (x.meta.get("depends_on") or [])]
    return f"{b.id} {c.fit(b.title, 60)}" + (f" and {c.joinmore(waiting, 5)} waiting on it" if waiting else "")


def _freed(b, briefs):
    """T-2367: the open tasks a yes on b frees: b and every open task waiting on it, transitively."""
    deps = {}
    for x in briefs:
        if x.status not in c.CLOSED:
            for d in x.meta.get("depends_on") or []:
                deps.setdefault(d, []).append(x.id)
    seen, todo = {b.id}, [b.id]
    while todo:
        for t in deps.get(todo.pop(), []):
            if t not in seen:
                seen.add(t)
                todo.append(t)
    return seen


def ranked(p, briefs=None):
    """T-2367: queued(), the yes that frees the most open work first; oldest first on a tie (the sort is stable)."""
    briefs = c.load_briefs(p) if briefs is None else briefs
    return sorted(queued(p, briefs), key=lambda x: -len(_freed(x[0], briefs)))


def line(p, briefs=None):
    """One line for fm next, the Stop hook and fm run's end: how many wait on the user, or ''."""
    q = ranked(p, briefs)
    return (f"{len(q)} grant request{'s' if len(q) != 1 else ''} queued for the user (fm todo): "
            + ", ".join(dict.fromkeys(f"{b.id} {d.get('pin') or d.get('what') or d['cats'][0]}" for b, d in q[:5]))
            + (" …" if len(q) > 5 else "")) if q else ""


def env_names(b):
    """The secret variables the task's grant rows name (T-2383); a row's other fields never count."""
    return sorted({n for _, d in rows(b) for n in (d.get("env") if isinstance(d.get("env"), list) else [])
                   if isinstance(n, str) and ENV_NAME.fullmatch(n)})


def secret_gaps(p, b, path=None):
    """T-2383: a refusal per secret variable whose value is in this session's transcript. Set here, the scan looks for
    its live value (so a rotated secret clears it); unset, for NAME=<value> that c.redact would redact (a $NAME or
    <placeholder> is a reference). mmap.find per name: a transcript runs to hundreds of MB (0.14 s on 600 MB).
    ponytail: this session's transcript only; a value pasted in an earlier session of the task isn't seen."""
    names = env_names(b)
    if not names:
        return []
    if path is None:
        from sampo import claude
        try:
            path = claude._transcript(c.session_id())
        except ValueError:
            return []
    import mmap
    out = []
    with open(path, "rb") as f:
        if not os.fstat(f.fileno()).st_size:
            return []
        with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
            for n in names:
                live = os.environ.get(n) or ""
                if len(live) >= 8:
                    hit = m.find(live.encode()) >= 0
                else:  # find the name, then match its =value tail: a leading lookbehind made re scan 600 MB in 9 s
                    key, i, hit = n.encode(), m.find(n.encode()), False
                    while i >= 0 and not hit:
                        x = None if i and (m[i - 1] == 95 or bytes([m[i - 1]]).isalnum()) else _TAIL.match(m, i + len(key))
                        if x and x.group(1)[:1] not in b"$<{%":
                            s = (key + x.group(0)).decode("utf-8", "replace")
                            hit = c.redact(s) != s
                        i = m.find(key, i + 1)
                if hit:
                    out.append(f"this session's transcript holds {'the live value' if len(live) >= 8 else 'a value'} "
                               f"of {n}, which {b.id}'s grant asks the user to set outside the session: the user rotates "
                               f"it and sets the new value outside the session (with {n} set, only its live value counts)")
    return out


def safe_secret_gaps(p, b):
    """secret_gaps for the close gates: an unreadable transcript fails open, logged where hooks' errors go."""
    try:
        return secret_gaps(p, b)
    except Exception:
        import traceback
        try:
            from sampo import hooks  # T-3431: rotation and repeat collapse; lazy, the failure path only
            hooks.log_error("fmgrants.secret_gaps", traceback.format_exc())
        except Exception:
            pass
        return []
