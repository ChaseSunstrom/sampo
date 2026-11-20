"""T-5011 (slice of T-4324): where a capture belongs. `place_capture(p, text, type_, scope)` gives one verdict for the
text: new (a task of its own), join (a step or follow-up on an open task that covers it), merge (it is the same request
as an open task of its type) or duplicate (a finished task already did it), with the task it names, the reason, the
backend that decided and its probability.

The rule is today's word-overlap test (recall.near_tasks: the open task of this type, the open task of another type and
the done task closest in title, plus the open awake task whose scope covers a file the text names) and it answers below
BAR on purpose: models.decide (T-2485: the user's own and loopback backends only, advice that is booked for scoring) is
asked among the verbs that have a task to land on, and a backend's answer that clears the bar replaces the rule's. No
backend, no answer or one under the bar leaves the rule's verdict, so the placement is never worse than the rule and
no model gates a capture (models advise, proof decides). Nothing here writes a task: applying a verdict is the
caller's (T-5021)."""
import time

from sampo import core as c

BAR = 0.8  # a model's answer clears it; the rule's own never does
RULE_P = {"merge": 0.7, "duplicate": 0.7, "join": 0.6, "new": 0.7}  # the rule's confidence per verdict: all under BAR
TIMEOUT = 8  # seconds: the budget for asking every backend together (a request inside models may still retry past it)
BACKENDS = 2  # at most this many backends are asked, cheapest first
TEXT = 600  # characters of the capture a model reads
TITLE = 100  # characters of a task's title a model reads

WHERE = {
    "new": "It is work no open or finished task covers: it needs a task of its own",
    "join": "It belongs on an open task that covers it, as a step or follow-up there",
    "merge": "It is the same request as an open task of its type: fold it into that task",
    "duplicate": "A finished task already did this: it is not new work",
}


def _line(s, width=TITLE):
    """One plain, redacted line of outside text (a title or a capture) for a reason or a prompt."""
    return c.fit(c.plain(c.redact(" ".join(str(s or "").split()))), width)


def candidates(p, text, type_=None, scope=()):
    """{verb: (task id, title, why)} for the verbs that have a task to land on, from the rule's own tests: merge the open
    task of this type whose title nearly repeats the text, duplicate the done one that does, join the open task of
    another type that does, else the open awake task whose scope covers a file the text names (the narrowest scope, then
    the same type, the one being worked, the oldest: triggers' own order)."""
    from sampo import cli
    from sampo import recall
    from sampo import triggers
    same, other, done = recall.near_tasks(p, cli._title(text), cli._scope_paths(scope), type_, skip=triggers._ASLEEP)
    out = {}
    for verb, hit, why in (("merge", same, "open {id} of the same type nearly repeats it: {title}"),
                           ("duplicate", done, "done {id} nearly repeats it: {title}"),
                           ("join", other, "open {id} of another type nearly repeats it: {title}")):
        if hit:
            out[verb] = (hit[0], hit[1], why.format(id=hit[0], title=_line(hit[1])))
    if "join" not in out:
        paths = triggers.named_paths(text)
        best = []
        for b in c.load_briefs(p) if paths else ():
            if b.status not in c.OPEN or b.status in triggers._ASLEEP:
                continue
            ranks = [r for r in (triggers._scope_rank(x, b.meta.get("scope")) for x in paths) if r is not None]
            if ranks:
                best.append((min(ranks), b.type != type_, b.status not in ("active", "verifying"),
                             b.status not in c.RUNNABLE, c.id_num(b.id), b))
        if best:
            b = min(best, key=lambda x: x[:5])[5]
            out["join"] = (b.id, b.title, f"open {b.id} covers a file it names ({_line(paths[0], 60)}): {_line(b.title)}")
    return out


def _rule(cands):
    """(verb, why) the word-overlap rule gives: the first verb with a task, in the order merge, duplicate, join."""
    for verb in ("merge", "duplicate", "join"):
        if verb in cands:
            return verb, cands[verb][2]
    return "new", "no open or done task shares its words or covers a file it names"


def _prompt(text, cands):
    rows = "\n".join(f"- {verb}: {tid} {_line(title)!r}" for verb, (tid, title, _) in cands.items())
    return (f"A request was captured: {_line(text, TEXT)!r}\nTasks it may belong to:\n{rows}\n"
            "Say where it goes: onto one of these tasks, or as new work.")


def place_capture(p, text, type_=None, scope=(), backends=None, timeout=TIMEOUT):
    """{where, target, reason, backend, probability}: where is new, join, merge or duplicate, target the task id it names
    (None for new), reason one plain line saying why, backend 'rule' or the backend that decided, probability its
    number in [0, 1]. The rule's answer is under BAR, so the models are asked (backends: the user's own, or the ones
    given) whenever there is a task to land on; a model's answer counts only when it clears BAR and names a verb that has
    a task. Any failure of a model leaves the rule's verdict."""
    cands = candidates(p, text, type_, scope)
    verb, why = _rule(cands)
    out = {"where": verb, "target": cands[verb][0] if verb in cands else None, "reason": why, "backend": "rule",
           "probability": RULE_P[verb]}
    if not cands or not str(text or "").strip():
        return out
    from sampo import models
    choices = {v: WHERE[v] for v in ("new", *cands)}
    deadline, got = time.monotonic() + timeout, None
    try:
        bl = models.own_backends(BACKENDS) if backends is None else list(backends)
        for b in bl or [None]:  # no backend: one call, so the rule's answer is still booked
            left = deadline - time.monotonic()
            if b is not None and left <= 0:
                break
            one = models.decide("capture-place", _prompt(text, cands), choices, rule=lambda _: (verb, RULE_P[verb]),
                                bar=BAR, p=p, backends=[] if b is None else [b], timeout=max(left, 1) if b else timeout)
            if one and (not got or one["probability"] > got["probability"] or one["cleared"]):
                got = one
            if got and got["cleared"]:
                break
    except Exception:  # a model is advice: a refusal, a timeout or a broken backend never costs the capture its placement
        return out
    if not got or got["backend"] == "rule":
        return out
    pr = round(float(got["probability"]), 3)
    if not got["cleared"]:
        out["reason"] = f"{why} (a model said {got['choice']} at {pr:.0%}, under the {BAR:.0%} bar)"
        return out
    pick = got["choice"]  # models.decide returns only a listed choice
    if not models.live(p, "capture-place"):  # the user's rule: a model replaces its path only after an exam shows it beats it
        out["reason"] = f"{why} (shadow: a model said {pick} at {pr:.0%}; it is not yet proven live)"
        return out
    note = f"{got['backend']} read it as {pick} at {pr:.0%}"
    return {"where": pick, "target": cands[pick][0] if pick in cands else None,
            "reason": f"{note}: {cands[pick][2]}" if pick in cands else
            f"{note}: the tasks the rule found are not the same work ({why})",
            "backend": got["backend"], "probability": pr}
