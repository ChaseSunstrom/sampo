"""fm draft (T-1062): while nobody is waiting on the queue, a headless claude drafts the top queued S task in a throwaway
worktree on branch draft/T-x, ready to review. The draft is fingerprinted (task id + brief hash) in draft.json and swept
when the queue's top or its brief changes. Never merged; fm tidy's lanes pass and the branch name are the user's to act on."""
import hashlib
import json
import os
import re
import shutil
import subprocess

from sampo import core as c

TIMEOUT = 1800


def _top(p):
    """The first queued S task whose dependencies are closed, or None."""
    briefs = c.load_briefs(p)
    state = {b.id: b.status for b in briefs}
    return next((b for b in c.order_queue(briefs)[0]
                 if b.tier == "S" and all(state.get(d) in c.CLOSED for d in c._deps(b))), None)


def _fp(b):
    with open(b.path, "rb") as f:
        return {"task": b.id, "hash": hashlib.sha256(f.read()).hexdigest()[:16]}


def _wt(p, tid):
    return os.path.join(os.path.dirname(p.root), os.path.basename(p.root) + ".draft", tid)


def _drop(p, tid):
    c._git(p.root, "worktree", "remove", "--force", _wt(p, tid), timeout=60)
    shutil.rmtree(_wt(p, tid), ignore_errors=True)
    c._git(p.root, "worktree", "prune", timeout=30)
    c._git(p.root, "branch", "-D", f"draft/{tid}", timeout=30)


def sweep(p):
    """Remove the draft when the queue's top (or its brief) no longer matches; the swept task ids."""
    path = os.path.join(p.dir, "draft.json")
    try:
        with open(path, encoding="utf-8") as f:
            old = json.load(f)
    except (OSError, ValueError):
        return []
    top = _top(p)
    if top and _fp(top) == old:
        return []
    _drop(p, old["task"])
    os.remove(path)
    return [old["task"]]


def cmd_draft(args):
    from sampo import cli
    p = cli.resolve(args)
    swept = sweep(p)
    if args.status:
        path = os.path.join(p.dir, "draft.json")
        cur = json.load(open(path, encoding="utf-8"))["task"] if os.path.exists(path) else None
        return cli.out(args, {"swept": swept, "draft": cur},
                         "\n".join([f"swept the stale draft of {t}" for t in swept] + [f"draft: {cur or 'none'}"]))
    top = _top(p)
    if not top:
        return cli.out(args, {"drafted": None, "swept": swept}, "nothing to draft: no unblocked queued S task")
    if os.path.exists(os.path.join(p.dir, "draft.json")):
        return cli.out(args, {"drafted": None, "swept": swept}, "a draft is already waiting for review")
    if c.read_meta(p).get("sensitive"):
        raise cli.UsageError("sensitive project: nothing is drafted")
    wt = _wt(p, top.id)
    os.makedirs(os.path.dirname(wt), exist_ok=True)
    r = c._git(p.root, "worktree", "add", "-q", "-b", f"draft/{top.id}", wt, "HEAD", timeout=120, proc=True)
    if r.returncode:
        raise cli.UsageError(f"git worktree add: {r.stderr.strip()[:200]}")
    title = re.sub(r"[\x00-\x1f\"]", " ", top.title)[:200]
    prompt = (f"Draft task {top.id} (titled \"{title}\") in this throwaway worktree: edit files so the change is ready "
              f"to review, with its test. Do not commit, push or merge; do not run fm. Brief:\n{top.section('Acceptance criteria')[:1500]}")
    try:
        import argparse
        from sampo import serve
        # same flags fm run gives its sessions (sandbox, models, permission mode); only the prompt differs
        # acceptEdits: a headless session can't answer an edit prompt, and the draft only ever edits its own throwaway
        # worktree (never merged); shell commands still ask (denied headless) and the guard still runs
        cmd = serve._claude_cmd(p, top, argparse.Namespace(max_usd=0, permission_mode="acceptEdits"), {})
        cmd[2] = prompt
        subprocess.run(cmd, cwd=wt, capture_output=True, text=True,
                       timeout=TIMEOUT)
        c._git(wt, "add", "-A", timeout=60)
        c._git(wt, "-c", "user.name=Sampo draft", "-c", "user.email=foreman@localhost", "-c", "commit.gpgsign=false",
               "commit", "-q", "--no-verify", "-m", f"draft: {top.id} (not reviewed)", timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        _drop(p, top.id)
        raise cli.UsageError(f"the draft of {top.id} failed: {e}")
    with c.lock(p.dir):
        with open(os.path.join(p.dir, "draft.json"), "w", encoding="utf-8") as f:
            json.dump(_fp(top), f)
        c.log_event(p, "draft", task=top.id, data={"branch": f"draft/{top.id}"}, session=cli.session())
    cli.out(args, {"drafted": top.id, "branch": f"draft/{top.id}", "worktree": wt, "swept": swept},
              f"drafted {top.id} on draft/{top.id} ({wt}): review it, then merge or `fm draft --status` to sweep")


# ---------------------------------------------------------------- T-2407 (#3): draft ahead
AHEAD = "ahead.json"  # the one speculative draft waiting: {id, tid, wt, at, keys, plan}
AHEAD_DAYS, ADOPT = 3, 0.6  # removed (scored expired) after 3 days; adopted by a request covering 60%+ of its keys
_WORD = re.compile(r"[a-z][a-z0-9_]{2,}")


def _keys(title, paths):
    """A request's words (3+ letters) and paths: what a draft and a new request are compared on."""
    return sorted(set(_WORD.findall(str(title).lower())) | {str(x) for x in paths or ()})


def _ahead_doc(p):
    try:
        with open(os.path.join(p.dir, AHEAD), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) and isinstance(d.get("id"), str) else None


def _drop_ahead(p, score=None):
    """Remove the waiting draft (worktree, branch, file); a score ({adopted, for} or {expired}) goes in the book."""
    from sampo import foresight
    d = _ahead_doc(p)
    if d and re.fullmatch(r"ahead-[\w.-]+", str(d.get("tid") or "")):
        _drop(p, d["tid"])
    try:
        os.remove(os.path.join(p.dir, AHEAD))
    except OSError:
        pass
    if d and score:
        c.append_line(os.path.join(p.dir, foresight.PREDICTIONS),
                      json.dumps(dict({"kind": "draft", "id": d["id"], "at": c.now()}, **score)) + "\n")


def ahead(p):
    """T-2407 (#3), on the tick: while next-request beats its naive baseline (and neither the kind nor the draft-ahead
    trigger route is muted), the newest unscored prediction is planned ahead: the plan of this user's closest past
    request of the predicted type, committed alone as DRAFTED-AHEAD.md (no code) in a throwaway worktree on
    draft/ahead-ID, labelled speculative; never pushed or merged. One draft per prediction; one older than AHEAD_DAYS
    is removed and scored expired. The new draft, or None."""
    from sampo import foresight as fs
    from sampo import triggers
    d = _ahead_doc(p)
    if d:
        if (c.age_days(d.get("at")) or 0) > AHEAD_DAYS:
            _drop_ahead(p, {"expired": True})
        return None
    if c.read_meta(p).get("sensitive") or triggers.row(p, "draft-ahead")["route"] == "muted":
        return None
    rows = fs._book(p)
    pred = next((r for r in reversed(rows) if r.get("kind") == "next-request"), None)
    if not pred or any(r.get("id") == pred["id"] and r.get("kind") in ("score", "draft") for r in rows):
        return None
    lead = fs.leading(p, rows, pred)  # T-2912: the local arm's guess when it leads the count, else the count's
    if not lead:
        return None
    want = lead[1]
    wp = set(want.get("paths") or ())
    past = [(b, ps) for b, t, ps in fs._requests(p) if t == want.get("type")]
    if not past:
        return None
    src = max(enumerate(past), key=lambda x: (len(wp & set(x[1][1])), x[0]))[1][0]  # most shared paths, then newest
    body = lambda name: re.sub(r"(?m)^#+\s*", "", src.section(name)).strip() or "(none)"  # noqa: E731
    # T-4817: the plan is committed to a branch that may be pushed: private terms and this machine's layout go too;
    # strict, so an unreadable term list raises before any worktree is made (the tick's note says the fixed line)
    plan = c.redact_local(c.redact(f"Speculative: Sampo predicted the next request is a {want.get('type')} touching "
                    f"{', '.join(sorted(wp)) or 'no known paths'} ({pred['id']}); this is the plan of {src.id} "
                    f"({c.plain(src.title)}), the closest past one. A plan only, no code; never pushed or merged.\n\n"
                    f"Approach:\n{body('Approach (options → choice → why)')}\n\nSteps:\n{body('Steps')}\n"))
    tid = "ahead-" + re.sub(r"[^\w.-]", "-", pred["id"])
    wt = _wt(p, tid)
    os.makedirs(os.path.dirname(wt), exist_ok=True)
    if c._git(p.root, "worktree", "add", "-q", "-b", f"draft/{tid}", wt, "HEAD", timeout=120, proc=True).returncode:
        return None
    with open(os.path.join(wt, "DRAFTED-AHEAD.md"), "w", encoding="utf-8") as f:
        f.write(f"# Drafted ahead\n\n{plan}")
    c._git(wt, "add", "DRAFTED-AHEAD.md", timeout=60)
    c._git(wt, "-c", "user.name=Sampo draft", "-c", "user.email=foreman@localhost", "-c", "commit.gpgsign=false",
           "commit", "-q", "--no-verify", "-m", f"draft ahead: {pred['id']} (speculative, not reviewed)", timeout=60)
    doc = {"id": pred["id"], "tid": tid, "wt": wt, "at": c.now(), "keys": _keys(src.title, wp), "plan": plan}
    with c.lock(p.dir):
        with open(os.path.join(p.dir, AHEAD), "w", encoding="utf-8") as f:
            json.dump(doc, f)
        c.append_line(os.path.join(p.dir, fs.PREDICTIONS),
                      json.dumps({"kind": "draft", "id": pred["id"], "at": doc["at"], "made": src.id}) + "\n")
    return doc


def adopt(p, b, source="user"):
    """T-2407: at task creation (fm task new, fm capture); one stat when no draft waits. A request covering ADOPT+ of
    the draft's words and paths takes its plan (returned, for the caller to attach as 'Drafted ahead') and the draft is
    removed and scored adopted; any other request, and every capture Sampo files itself (source: the request's,
    not a reused capture's), leaves it."""
    if source != "user" or not os.path.exists(os.path.join(p.dir, AHEAD)):
        return None
    d = _ahead_doc(p)
    keys = set(d.get("keys") or ()) if d else set()
    scope = b.meta.get("scope") if isinstance(b.meta.get("scope"), list) else []
    if not keys or len(keys & set(_keys(b.title, scope))) / len(keys) < ADOPT:
        return None
    _drop_ahead(p, {"adopted": True, "for": b.id})
    return str(d.get("plan") or "")
