"""fm lane pilot ID | --all [--dry] (T-5706): one returned lane, reviewed headless and accepted with no lead turn.

The pilot gates the lane first (its builder returned, it awaits its merge, no land hold, no red check, the contract
still runs), then runs fm audit run's reviewers on the lane's own tree: one per lens, self for an S lane and the five
default lenses for M or L, a sensitive S lane adds adversary. auditrun records only the clean lenses (ok, no MEDIUM or
worse). A clean lane goes to `sampo lane accept` as a subprocess, so the accept's own rules still decide. Anything else
is one line naming why: changes needed, or an exception. Haiku reviews an S lane; sonnet reviews M, L and a sensitive
diff."""
import contextlib
import io
import json
import os
import subprocess
import sys
import time

from sampo import contract
from sampo import core as c
from sampo import lanes
from sampo import train

ACCEPT_TIMEOUT = 600  # s: the accept starts the lane's check and returns; the check runs detached
SAMPO = os.path.join(c.PLUGIN_ROOT, "bin", "sampo")


def lenses_for(b, sensitive):
    """(lenses, model) a lane's review runs with: an S lane reads its self lens on haiku (a sensitive one adds adversary
    on sonnet); M, L and the big tiers read the default lenses on sonnet."""
    if c.canonical_tier(b.tier) == "S":
        return (["self", "adversary"], "sonnet") if sensitive else (["self"], "haiku")
    return list(c.DEFAULT_LENSES), "sonnet"


SENSITIVE_TIMEOUT = 60  # s: one lane's diff read (train's 2 s default is too short for a big lane)


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
    return lanes.land_holds(p, b, main, tip, timing=False) or train._check_hold(b, tip, main) or ""


def _state(main, b):
    """(lane commit, lane content id) now (T-5706). core.worktree_id does not move on a commit that adds no content, so
    the commit is read too; an edit moves the content id and leaves the commit alone."""
    lane = b.meta.get("lane") or ""
    return train._lane_tip(main, b), (c.worktree_id(lane) if lane and os.path.isdir(lane) else "")


def review(p, tid, lenses, model):
    """fm audit run's reviewers on these lenses; auditrun records each clean one against the lane's tree. Returns
    (verdicts {lens: ok | changes needed}, findings ["lens: SEVERITY file:line text"], why) where why names a refusal
    or a reviewer that gave no verdict, '' when every lens answered."""
    from sampo import auditrun, cli
    args = cli.build_parser().parse_args(["audit", "run", tid])
    args.lens, args.model, args.pilot, args.json = list(lenses), model, True, True
    args.project = p.slug  # the pilot's own project, not whatever the process cwd resolves to
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
    findings = [f"{lens}: {x}" for lens, xs in (data.get("findings") or {}).items() for x in xs]
    return data.get("verdicts") or {}, findings, why


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
    lenses, model = lenses_for(b, _sensitive(main, train._lane_tip(main, b)))
    res.update(lenses=lenses, model=model)
    before = _state(main, b)
    verdicts, findings, why = review(p, b.id, lenses, model)
    res["findings"] = findings
    now = _state(main, b)
    if now != before:  # T-5706: a commit or an edit that landed while the reviewers read is not what they reviewed
        return done("exception", f"tip moved during review (lane was at {before[0][:12] or 'no commit'}, now "
                                 f"{now[0][:12] or 'no commit'}); nothing accepted, rerun the pilot")
    if why:
        return done("exception", why)
    stuck = [x for x in lenses if verdicts.get(x) != "ok"]
    if stuck:
        # round 3: stamp the reviewed tip, so the next sweep waits for a new commit (under the project lock, one event)
        cli.mutate(p, b.id, lambda x: x.meta.update(pilot_tip=before[0]), "pilot_changes",
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
    lenses, model = lenses_for(b, _sensitive(main, train._lane_tip(main, b)))
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
    if any(r["outcome"] == "exception" for r in rows):  # one line, nonzero: the top level prints the message once
        raise cli.UsageError(text)
    cli.out(args, {"lanes": rows}, text)
