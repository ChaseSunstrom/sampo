"""fm lane pilot ID | --all [--dry] (T-5706): one returned lane, reviewed headless and accepted with no lead turn.

The pilot gates the lane first (its builder returned, it awaits its merge, no land hold, no red check, the contract
still runs), then runs fm audit run's reviewers on the lane's own tree: one per lens, the lenses and model the depth map gives
(lanes.review_depth: intent + adversary on haiku for a light lane, all five on sonnet for a protected path or SECURITY
task; a sensitive diff always carries the adversary on sonnet). auditrun records only the clean lenses (ok, no MEDIUM or
worse). A parsed verdict is kept by diff bytes, lens, model, prompt version and brief, so an unchanged re-review launches
nothing (T-6044); after a fix round a lens that asked for changes re-reads the whole diff and a clean one only the fix.
A clean lane goes to `sampo lane accept` as a subprocess, so the accept's own rules still decide. Anything else
is one line naming why: changes needed, or an exception."""
import contextlib
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import time

from sampo import contract
from sampo import core as c
from sampo import lanes
from sampo import train

ACCEPT_TIMEOUT = 600  # s: the accept starts the lane's check and returns; the check runs detached
SAMPO = os.path.join(c.PLUGIN_ROOT, "bin", "sampo")


SENSITIVE_TIMEOUT = 60  # s: one lane's diff read (train's 2 s default is too short for a big lane)
DIFF_CAP = 64 << 20  # bytes: a lane diff past this is not read whole, so it is never cached (a miss, never a wrong hit)
CACHE_MAX = 200  # verdicts kept (T-6044): the oldest go first
CACHE_TTL = 14 * 86400  # s: an older verdict is a miss
CACHE_FINDINGS, CACHE_FINDING_CHARS = 20, 400  # what a cached changes-needed verdict replays
AUDIT_REF = os.path.join(c.PLUGIN_ROOT, "skills", "intake", "references", "audit.md")  # the lens templates: part of the prompt


def lenses_for(b, sensitive, p=None, names=None, raw=b""):
    """(lenses, model) a lane's review runs with (T-6044): lanes.review_depth's, from the lane's files and diff (the depth
    map and its floors: a protected path, a SECURITY task, SAMPO_REVIEW_DEPTH=full). A sensitive diff always carries the
    adversary, on sonnet. names None (the diff could not be read) is every lens on sonnet: fail toward more review."""
    from sampo import cli
    if names is None:
        return list(c.DEFAULT_LENSES), "sonnet"
    d = lanes.review_depth(b, names, raw.decode("utf-8", "replace"), p=p, tests_linked=cli._tests_linked(p, b, names))
    lenses, model = list(d["lenses"]), d["model"]
    if sensitive:
        lenses, model = [x for x in c.DEFAULT_LENSES if x in lenses or x == "adversary"], "sonnet"
    return lenses, model


def _git_bytes(main, *args):
    """One git read in main as bytes (a diff is hashed byte for byte), no repo hook, driver or prompt; raises ValueError
    on a non-zero exit or output past DIFF_CAP."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    r = subprocess.run(["git", "-C", main, "-c", "core.quotePath=false", "-c", "core.fsmonitor=false",
                        "-c", "core.hooksPath=/dev/null", *args], capture_output=True, timeout=SENSITIVE_TIMEOUT,
                       stdin=subprocess.DEVNULL, env=env)
    if r.returncode != 0 or len(r.stdout) > DIFF_CAP:
        raise ValueError(f"git {args[0]} gave no usable output")
    return r.stdout


def lane_diff(main, tip, since=""):
    """(file names, the diff's bytes) of the lane at tip: from where it left main (HEAD...tip), or since a commit (a fix
    round's delta). (None, b"") when any read fails, times out or is past DIFF_CAP: nothing is then cached or taken as light."""
    if not re.fullmatch(r"[0-9a-f]{40,64}", tip or "") or (since and not re.fullmatch(r"[0-9a-f]{40,64}", since)):
        return None, b""
    rng = f"{since}..{tip}" if since else f"HEAD...{tip}"
    try:
        names = _git_bytes(main, "diff", "-z", "--name-only", "--no-renames", rng)
        raw = _git_bytes(main, "diff", "--no-ext-diff", "--no-textconv", "--binary", "--full-index", rng)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None, b""
    return [x.decode("utf-8", "replace") for x in names.split(b"\0") if x], raw


def cache_path(p):
    return os.path.join(p.dir, "lanepilot_verdicts.json")


def _valid(e, now):
    return (isinstance(e, dict) and e.get("verdict") in ("ok", "changes needed") and isinstance(e.get("lens"), str)
            and isinstance(e.get("ts"), (int, float)) and now - e["ts"] < CACHE_TTL
            and isinstance(e.get("findings"), list) and all(isinstance(f, str) for f in e["findings"]))


def _load(p):
    """The verdict store's entries {key: entry}, each one checked: a missing, torn or tampered file or entry is a miss."""
    try:
        with open(cache_path(p), encoding="utf-8") as f:
            ents = json.load(f).get("entries")
    except (OSError, ValueError, AttributeError):
        return {}
    now = time.time()
    return {k: v for k, v in ents.items() if isinstance(k, str) and _valid(v, now)} if isinstance(ents, dict) else {}


def cache_size(p):
    return len(_load(p))


def _store(p, new):
    """Add entries {key: entry}, the newest CACHE_MAX kept (the project lock, one atomic write)."""
    with c.lock(p.dir):
        ents = _load(p)
        for k, v in new.items():
            ents.pop(k, None)
            ents[k] = v
        keep = sorted(ents.items(), key=lambda kv: kv[1]["ts"])[-CACHE_MAX:]  # stable: a tie keeps file order
        c.write_atomic(cache_path(p), json.dumps({"entries": dict(keep)}), durable=False, mode=0o600)


def _prompt_version():
    """sha256 of what a reviewer is asked (auditrun's system and closing text, the reply schema and the format text made
    from it, and the lens templates): a change to any of it is a new version. None when it can't be read (no caching)."""
    from sampo import auditrun
    try:
        with open(AUDIT_REF, "rb") as f:
            ref = f.read()
    except OSError:
        return None
    h = hashlib.sha256()
    for part in (auditrun.SYSTEM.encode(), auditrun.CLOSING.encode(), auditrun.typed_format().encode(),
                 json.dumps(auditrun.REPLY_SCHEMA, sort_keys=True, default=str).encode(), ref):
        h.update(hashlib.sha256(part).digest())
    return h.hexdigest()


def _brief_hash(b):
    """sha256 of what the task asked: id, type, tier, request, criteria, steps and the builder's lens pass. Not its
    evidence, its ticks or its log, which move after every review."""
    from sampo import cli
    doc = [b.id, str(b.type), str(b.tier), b.title, b.section("Raw request"), [a.text for a in b.acceptance()],
           [s.text for s in b.steps()], cli._builder_lenses(b)]
    return hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()


def cache_key(b, lens, model, raw):
    """The verdict cache's key (T-6044): the diff's bytes the lens reads, the lens, the model, the prompt version and the
    brief. '' (no key, so no caching) for a diff that was not read or a prompt that can't be versioned."""
    version = _prompt_version()
    if not raw or not version:
        return ""
    h = hashlib.sha256()
    for part in (hashlib.sha256(raw).hexdigest(), lens, model, version, _brief_hash(b)):
        h.update(hashlib.sha256(part.encode()).digest())
    return h.hexdigest()


def _sensitive(main, tip):
    """True when the lane's diff touches auth, crypto, secrets, exec or deserialization (c.sensitive). Fails closed
    (T-5706): a git error, a timeout or an exception counts as sensitive, so a diff the pilot could not read is never
    waved through. Names are read NUL-separated, so a name with a space or a newline is whole; each file's diff is read
    and scanned whole (no prefix), with a literal pathspec so a name's glob characters match only that file."""
    try:
        r = train._git(main, "diff", "-z", "--name-only", "--no-renames", f"HEAD...{tip}",
                       timeout=SENSITIVE_TIMEOUT, proc=True)
        if r.returncode != 0:
            return True
        names = [x for x in r.stdout.split("\0") if x]
        for name in names:
            d = train._git(main, "diff", f"HEAD...{tip}", "--", f":(literal){name}", timeout=SENSITIVE_TIMEOUT,
                           proc=True)
            if d.returncode != 0 or c.sensitive([name], d.stdout):
                return True
        return False
    except Exception:
        return True


def gate(p, b, main):
    """Why the pilot must not review or accept this lane now, or '' (T-5706). Each reason is one exception line."""
    lane = b.meta.get("lane") or ""
    if not lane or not os.path.isdir(lane):
        return "its lane worktree is gone"
    if not lanes._builder_returned(b):
        return "its builder has not returned"
    tip = train._lane_tip(main, b)
    if not tip:
        return "its lane branch has no commit"
    if b.meta.get("reviewed_tip") == tip:
        return f"reviewed at {tip[:12]} and not accepted: sampo lane accept {b.id} says why"
    if b.meta.get("pilot_tip") == tip:  # round 3: a changes round already spent its reviewers on this tip
        return f"reviewed at {tip[:12]}, changes needed: waiting for the builder's next commit"
    if c.is_big(b):
        return c.big_refusal(b)
    if b.id not in lanes.awaiting_merge(p, c.load_briefs(p)):
        return "not awaiting a merge (still running, parked, or its last merge failed)"
    why = contract.over(p)
    if why:
        return f"the contract is over ({why})"
    # round 3: timing=False, so a dry run or a sweep never starts the hook bench (it takes the machine lock); the accept
    # runs land_holds with its timing
    # T-4805 exempt: a hold check before the review, no landing of its own; the accept's land records the landing (lanes.log_landed)
    return lanes.land_holds(p, b, main, tip, timing=False, review=False) or train._check_hold(b, tip, main) or ""


def _state(main, b):
    """(lane commit, lane content id) now (T-5706). core.worktree_id does not move on a commit that adds no content, so
    the commit is read too; an edit moves the content id and leaves the commit alone."""
    lane = b.meta.get("lane") or ""
    return train._lane_tip(main, b), (c.worktree_id(lane) if lane and os.path.isdir(lane) else "")


def review(p, tid, lenses, model, since=""):
    """fm audit run's reviewers on these lenses (since: a commit, so they read only the fix after it, T-6044); auditrun
    records each clean one against the lane's tree. Returns
    (verdicts {lens: ok | changes needed}, findings ["lens: SEVERITY file:line text"], why, unparsed) where why names a
    refusal or a reviewer that gave no verdict, '' when every lens answered, and unparsed (T-6042) lists the lenses whose
    reviewer answered with text that parses to no verdict (a failed or timed-out reviewer is in why, not here)."""
    from sampo import auditrun, cli
    args = cli.build_parser().parse_args(["audit", "run", tid])
    args.lens, args.model, args.pilot, args.json = list(lenses), model, True, True
    args.project = p.slug  # the pilot's own project, not whatever the process cwd resolves to
    if since:  # prep diffs since that commit; the focus line says why the diff is short
        args.delta_base = since
        args.note = list(getattr(args, "note", None) or []) + [
            f"this diff is only the builder's fix since {since[:12]}, which you (or a reviewer of your lens) already found "
            f"clean; review the fix and what it can break, not the code it did not touch"]
    buf, why = io.StringIO(), ""
    try:
        with contextlib.redirect_stdout(buf):
            auditrun.run(p, args)
    except (cli.UsageError, c.PolicyError) as e:
        why = str(e)
    try:
        data = json.loads(buf.getvalue() or "{}")
    except ValueError:
        data = {}
        why = why or "audit run output unreadable"  # T-6042: its own reason: not an unparsed reply, not changes needed
    # T-6043: a typed reply's findings are read from their fields; the flattened strings serve a lens the regex parser read
    typed = data.get("typed") if isinstance(data.get("typed"), dict) else {}
    findings = [f"{lens}: " + " ".join(f"{f.get('severity', '')} {at} {f.get('text', '')}".split())
                for lens, fs in typed.items() if isinstance(fs, list) for f in fs if isinstance(f, dict)
                for at in [f"{f.get('file') or ''}" + (f":{f['line']}" if f.get("line") else "")]]
    findings += [f"{lens}: {x}" for lens, xs in (data.get("findings") or {}).items() if lens not in typed for x in xs]
    return data.get("verdicts") or {}, findings, why, list(data.get("unparsed") or [])  # auditrun's own classification


def scopes_for(main, b, lenses, tip, whole, sensitive):
    """{lens: (since, diff bytes it reads)} (T-6044). After a changes round every lens reads the whole new diff, except a
    lens the round found clean: that one reads only old tip..new tip. Only when the old tip is an ancestor of the new
    one (no rewritten history), the new diff is not sensitive and the fix is not empty; any doubt reads the whole diff."""
    scopes = {x: ("", whole) for x in lenses}
    prior = b.meta.get("pilot_tip") or ""
    oks = b.meta.get("pilot_ok")
    clean = {x.rsplit(":", 1)[0] for x in oks if isinstance(x, str) and x.endswith(":" + prior)} \
        if prior and isinstance(oks, list) else set()
    if not clean or prior == tip or sensitive or not whole:
        return scopes
    try:
        if train._git(main, "merge-base", "--is-ancestor", prior, tip, timeout=SENSITIVE_TIMEOUT, proc=True).returncode:
            return scopes
    except Exception:
        return scopes
    names, delta = lane_diff(main, tip, since=prior)
    if names and delta:
        scopes.update({x: (prior, delta) for x in lenses if x in clean})
    return scopes


def review_cached(p, b, main, before, lenses, model, scopes):
    """review() through the verdict cache (T-6044): same return. A lens whose key (diff bytes it reads, lens, model, prompt
    version, brief) is stored is answered from the store with no child: a clean one is recorded against the lane's tree
    as a review would, a changes-needed one replays its findings. The rest run, grouped by the diff they read, and a
    parsed verdict is stored, only while the lane is still at `before`: an unparsed, failed or timed-out lens never is.
    One pilot_lens event per lens says what it read."""
    tree, found, hits, todo = before[1], {}, {}, []
    keys = {x: cache_key(b, x, model, scopes.get(x, ("", b""))[1]) if tree else "" for x in lenses}
    stored = _load(p) if any(keys.values()) else {}
    verdicts, findings, whys, unparsed = {}, [], [], []
    for x in lenses:
        e = stored.get(keys[x])
        if e and e["lens"] == x:
            hits[x] = e
            verdicts[x] = e["verdict"]
            findings += [f"{x}: {f}" for f in e["findings"]]
            if e["verdict"] == "ok":  # the audit the reviewer would have recorded, on the tree the key was read at
                _record_cached(p, b.id, x, tree)
        else:
            todo.append(x)
    for since in sorted({scopes.get(x, ("", b""))[0] for x in todo}, key=lambda s: (s != "", s)):
        group = [x for x in todo if scopes.get(x, ("", b""))[0] == since]
        v, f, w, u = review(p, b.id, group, model, since=since)
        verdicts.update(v)
        findings += f
        unparsed += u
        if w:
            whys.append(w)
        for x in group:
            found[x] = [y[len(x) + 2:] for y in f if y.startswith(x + ": ")]
    if todo and _state(main, b) == before:
        now, new = time.time(), {}
        for x in todo:
            if keys[x] and x not in unparsed and verdicts.get(x) in ("ok", "changes needed"):
                new[keys[x]] = {"ts": now, "lens": x, "verdict": verdicts[x], "findings": [
                    c.redact(" ".join(y.split()))[:CACHE_FINDING_CHARS] for y in found.get(x, [])[:CACHE_FINDINGS]]}
        if new:
            try:
                _store(p, new)
            except (OSError, c.PolicyError):  # a cache that can't be written costs the next review, not this one
                pass
    with c.lock(p.dir):
        for x in lenses:
            since = scopes.get(x, ("", b""))[0]
            c.log_event(p, "pilot_lens", task=b.id, data={
                "lens": x, "scope": "delta" if since else "whole", "since": since[:12], "tip": before[0][:12],
                "cached": x in hits, "verdict": verdicts.get(x, "")})
    return verdicts, findings, "; ".join(whys), unparsed


def _record_cached(p, tid, lens, tree):
    from sampo import cli
    cli.mutate(p, tid, lambda x: x.add_audit(lens, "fm audit run (cached verdict)", "ok", tree=tree),
               "audit", {"lens": lens, "how": "fm audit run (cached verdict)", "result": "ok"})


def _accept(tid, main):
    """(exit code, the last line the accept printed) of `sampo lane accept` run in the main checkout."""
    try:
        r = subprocess.run([sys.executable, SAMPO, "lane", "accept", tid], cwd=main, capture_output=True, text=True,
                           timeout=ACCEPT_TIMEOUT)
    except subprocess.TimeoutExpired:
        return 124, f"sampo lane accept ran past {ACCEPT_TIMEOUT} s"
    lines = [x.strip() for x in (r.stdout + r.stderr).splitlines() if x.strip()]
    return r.returncode, (lines[-1][:300] if lines else "")


def pilot(p, tid, main):
    """One lane through the pilot (T-5706). Returns {tid, outcome, lenses, findings, model, seconds, reason}: outcome
    accepted (sampo lane accept took it), changes (a lens reported changes needed, its clean lenses recorded, no
    accept), or exception (a gate, a refused review or the accept said no). Never accepts a lane with a blocking
    finding: the accept runs only when every reviewed lens came back clean."""
    from sampo import cli
    t0 = time.monotonic()
    b = cli.need_brief(p, tid)
    res = {"tid": b.id, "outcome": "exception", "lenses": [], "findings": [], "model": "", "seconds": 0.0,
           "reason": ""}

    def done(outcome, reason=""):
        res.update(outcome=outcome, reason=reason, seconds=round(time.monotonic() - t0, 1))
        return res

    why = gate(p, b, main)
    if why:
        return done("exception", why)
    before = _state(main, b)  # T-6044: the tip every read below (diff, scan, cache key, reviewers) is taken at
    sensitive = _sensitive(main, before[0])
    names, whole = lane_diff(main, before[0])
    lenses, model = lenses_for(b, sensitive, p, names, whole)
    res.update(lenses=lenses, model=model)
    scopes = scopes_for(main, b, lenses, before[0], whole, sensitive)
    verdicts, findings, why, unparsed = review_cached(p, b, main, before, lenses, model, scopes)
    res["findings"] = findings
    if unparsed:  # T-6042: counted before any other exit (selfcheck.verdict_rate reads it), even when the tip moved
        with c.lock(p.dir):
            c.log_event(p, "pilot_unparsed", task=b.id, data={"lenses": unparsed, "parsed": len(verdicts)})
    now = _state(main, b)
    if now != before:  # T-5706: a commit or an edit that landed while the reviewers read is not what they reviewed
        return done("exception", f"tip moved during review (lane was at {before[0][:12] or 'no commit'}, now "
                                 f"{now[0][:12] or 'no commit'}); nothing accepted, rerun the pilot")
    if unparsed:  # T-6042: an exception, never a changes round
        return done("exception", f"reply unparsed: {', '.join(unparsed)}" + (f"; {why}" if why else ""))
    if why:
        return done("exception", why)
    stuck = [x for x in lenses if verdicts.get(x) != "ok"]
    if stuck:
        # round 3: stamp the reviewed tip, so the next sweep waits for a new commit (under the project lock, one event)
        # T-6044: and the lenses that were clean at it ("lens:tip"), which the next round reads only the fix for
        clean = [f"{x}:{before[0]}" for x in lenses if verdicts.get(x) == "ok"]
        cli.mutate(p, b.id, lambda x: x.meta.update(pilot_tip=before[0], pilot_ok=clean), "pilot_changes",
                   {"tip": before[0][:12], "lenses": stuck})
        return done("changes", f"changes needed: {', '.join(stuck)} (not accepted; its clean lenses are recorded)")
    rc, last = _accept(b.id, main)
    fresh = c.find_brief(p, b.id)
    if rc == 0 and fresh and fresh.meta.get("accepted_at"):
        return done("accepted")
    if rc == 0 and fresh and fresh.meta.get("check_state") == "running" \
            and fresh.meta.get("check_tip") == before[0]:  # item 3: the accept started its check, on this tip (round 3)
        return done("accepted", "check running")
    return done("exception", last or f"sampo lane accept exited {rc}")


def plan(p, b, main):
    """What the pilot would do with this lane, read only (T-5706: --dry)."""
    res = {"tid": b.id, "outcome": "dry", "lenses": [], "findings": [], "model": "", "seconds": 0.0, "reason": ""}
    why = gate(p, b, main)
    if why:
        res["reason"] = why
        return res
    tip = train._lane_tip(main, b)
    names, whole = lane_diff(main, tip)
    lenses, model = lenses_for(b, _sensitive(main, tip), p, names, whole)
    res.update(lenses=lenses, model=model)
    return res


def line(r):
    """The one line a lane prints (T-5706): accepted, changes needed, one exception, or the dry plan."""
    if r["outcome"] == "accepted":
        running = ", check running" if r["reason"] == "check running" else ""
        return f"{r['tid']}: accepted{running} ({', '.join(r['lenses'])} on {r['model']}, {r['seconds']:g} s)"
    if r["outcome"] == "changes":
        return f"{r['tid']}: {r['reason']}"
    if r["outcome"] == "dry":
        if r["reason"]:
            return f"{r['tid']}: would stop: {r['reason']}"
        return f"{r['tid']}: would review {', '.join(r['lenses'])} on {r['model']}, then accept"
    return f"{r['tid']}: exception: {r['reason']}"


ANSWER_FINDINGS, ANSWER_TEXT = 20, 300  # T-6011: the one-line answer's bounds (the trigger reads at most pilotsig.ANSWER_MAX)


def answer(r):
    """T-6011: one lane's `--json` answer as ONE compact line, which pilotsig reads as the verb's last line: tid, outcome,
    rounds (reviews run: 1 when the reviewers read the lane), why, lenses, findings. ASCII-escaped, so no newline or U+2028
    ends it early; findings and text are cut, so it stays under the reader's cap."""
    def cut(x):
        return " ".join(str(x).split())[:ANSWER_TEXT]
    row = {"tid": r["tid"], "outcome": r["outcome"], "rounds": 1 if r["outcome"] in ("accepted", "changes") else 0,
           "why": cut(r["reason"]), "lenses": [cut(x) for x in r["lenses"]],
           "findings": [cut(x) for x in r["findings"][:ANSWER_FINDINGS]], "model": cut(r["model"]),
           "seconds": r["seconds"]}
    return json.dumps(row, separators=(",", ":"))


def _crashed(tid, e):
    """T-5706 item 6: the row for a lane whose pilot raised: one line, and the sweep goes on to the next lane."""
    return {"tid": tid, "outcome": "exception", "lenses": [], "findings": [], "model": "", "seconds": 0.0,
            "reason": " ".join(f"{type(e).__name__}: {e}".split())[:300]}


def cmd(p, args, main):
    """fm lane pilot ID | --all [--max N] [--dry] (T-5706): the pilot for one returned lane, or for every returned lane
    whose reviewed tip is not its current tip (the sweep; --max N takes the first N). --dry prints each plan and runs no
    reviewer and no accept. A lane that raises is its own exception row; the rest still run."""
    from sampo import cli
    if p.lane:
        raise c.PolicyError(f"fm lane pilot runs from the main checkout ({main}), not from inside a lane")
    top = getattr(args, "max", None)
    if getattr(args, "all", False):
        if args.id:
            raise cli.UsageError("fm lane pilot --all takes no task id")
        if top is not None and top < 1:
            raise cli.UsageError("fm lane pilot --max takes a count of at least 1")
        briefs = c.load_briefs(p)
        awaiting = set(lanes.awaiting_merge(p, briefs))
        ids = [b.id for b in briefs  # T-5706 item 3: reviewed at its current tip is done; at an older tip, review again
               if b.id in awaiting and lanes._builder_returned(b)
               and train._lane_tip(main, b) not in (b.meta.get("reviewed_tip"), b.meta.get("pilot_tip"))]
        ids = ids[:top] if top is not None else ids
    elif args.id:
        if top is not None:
            raise cli.UsageError("fm lane pilot --max goes with --all")
        ids = [args.id]
    else:
        raise cli.UsageError("fm lane pilot needs a task id, or --all")
    run = plan if getattr(args, "dry", False) else pilot
    rows = []
    for i in ids:
        try:
            rows.append(run(p, cli.need_brief(p, i), main) if run is plan else run(p, i, main))
        except Exception as e:  # noqa: BLE001 - one lane's crash is its row, not the sweep's end
            rows.append(_crashed(i, e))
    text = "\n".join(line(r) for r in rows) or "no returned lane awaits a pilot"
    one = getattr(args, "json", False) and not getattr(args, "all", False) and len(rows) == 1
    if one:
        print(answer(rows[0]))  # T-6011: one lane's answer is one compact line (the trigger reads the last line); --all keeps {lanes}
    if any(r["outcome"] == "exception" for r in rows):  # one line, nonzero: the top level prints the message once
        raise cli.UsageError(text)
    if not one:
        cli.out(args, {"lanes": rows}, text)
