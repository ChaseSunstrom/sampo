"""T-3881: sampo update [--check|--dry-run|--undo]: Sampo's own self-update, one tested path (T-3882 and T-3883 reuse it).

It fetches origin, refuses with one line what it can't update cleanly (not a git checkout, no origin, a branch that
isn't the release one: a dev checkout like foreman/build updates by its own merges, uncommitted changes, an origin it
can't reach, a diverged history), fast-forwards, runs the plugin and home migrations only when their dry runs find
work (sampo migrate plugins / home, each with its own backups and --undo), re-checks plugin/native/sampo-fast against
its sum so the fast path never silently drops out, and says in a few lines what changed and what a running session
must do. The commit it left and what it migrated are recorded in the checkout's git dir, which is what --undo reads.

The migrations are imported after the merge, so they are the new version's own code (the update itself is the old
one's); a migration that can't run here is told, never fatal: the update stands."""
import contextlib
import datetime
import fcntl
import json
import os
import subprocess
import sys

from sampo import core as c

RELEASE = "main"  # the branch an install follows (install.sh pulls the same one)
RECORD, LOCK = "sampo-update.json", "sampo-update.lock"  # in the checkout's git dir
SHOWN = 5  # commits named in a report
FETCH_S = 90  # a network that hangs (port 22 blocked) ends the update with a line, not a stuck terminal
MIGRATIONS = ("plugins", "home")
NOTHING_TO_MOVE = ("no foreman home under", "isn't named foreman or sampo")  # homemove's "this install has no old home"


def _env():
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") or k in ("GIT_SSH", "GIT_SSH_COMMAND")}
    env.update(LC_ALL="C", GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
    return env


def _git(root, *args, timeout=30):
    """(returncode, stdout, stderr), stripped. A new session: no terminal for git or ssh to prompt on."""
    try:
        r = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, env=_env(), timeout=timeout,
                           stdin=subprocess.DEVNULL, start_new_session=True)
    except subprocess.TimeoutExpired:
        return 124, "", f"git {args[0]} took over {timeout} s"
    except OSError as e:
        return 127, "", f"git couldn't run ({e.strerror})"
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def _line(text):
    return c.fit(c.plain(" ".join(str(text).split())), 300)  # origin's words: no escape or OSC bytes; a cut ends in …


def _refuse(why):
    return {"status": "refused", "why": _line(why)}


def _short(sha):
    return sha[:9]


def _version(root, sha):
    rc, out, _ = _git(root, "show", f"{sha}:plugin/.claude-plugin/plugin.json")
    try:
        return _line(json.loads(out).get("version") or "?") if rc == 0 else "?"
    except (ValueError, AttributeError):
        return "?"


def _restart(root, a, b):
    """What a running session must do: the plugin's hooks, library and skills load when a session starts."""
    rc, out, _ = _git(root, "diff", "--name-only", a, b)
    if rc == 0 and not any(p.startswith("plugin/") and not p.startswith("plugin/tests/") for p in out.splitlines()):
        return ""
    return "Restart Claude Code sessions: each keeps the hooks and library it started with until then."


# ---------------------------------------------------------------- migrations

def _probe(name):
    """{"state": none|pending|skipped, "detail"} from the migration's dry run; nothing is written."""
    try:
        r = _module(name).migrate(dry=True)
    except Exception as e:  # a migration of the new version may use a core API this (old) process lacks: told, never fatal
        if not isinstance(e, (ValueError, OSError)):
            return {"state": "skipped", "detail": _line(f"{type(e).__name__}: {e}")}
        if name == "home" and any(m in str(e) for m in NOTHING_TO_MOVE):
            return {"state": "none", "detail": ""}
        return {"state": "skipped", "detail": _line(e)}
    if name == "plugins":
        if r.get("status") == "plan":
            return {"state": "pending", "detail": ", ".join(f"{o} -> {n}" for o, n in (r.get("moves") or {}).items())
                    or "the marketplace"}
        return {"state": "none", "detail": ""}
    todo = [s for s in r.get("steps") or [] if s.get("state") in ("todo", "moved")] if r.get("status") == "plan" else []
    return {"state": "pending", "detail": "foreman -> sampo"} if todo else {"state": "none", "detail": ""}


def _module(name):
    if name == "plugins":
        from sampo import pluginmove
        return pluginmove
    from sampo import homemove
    return homemove


def _migrations():
    return {n: _probe(n) for n in MIGRATIONS}


def _run_migration(name, undo=False):
    try:
        _module(name).migrate(undo=undo)
    except Exception as e:
        return {"state": "skipped", "detail": f"{'undo' if undo else 'run'} failed: {_line(f'{type(e).__name__}: {e}')}"}
    return {"state": "undone" if undo else "ran", "detail": ""}


# ---------------------------------------------------------------- sampo-fast

def _fast(root):
    """{"ok", "line"}: rebuilt, or checked against its sum, or why the Python hook stays. Never raises."""
    from sampo import fastbuild
    try:
        ok, line = fastbuild.repair(os.path.join(root, "plugin"))
    except Exception as e:  # a build that dies must not undo an update that already landed
        return {"ok": False, "line": f"native fast path not checked ({type(e).__name__}: {_line(e)}); the Python hook stays"}
    return {"ok": bool(ok), "line": _line(line)}


# ---------------------------------------------------------------- the record

def _record(gitdir):
    try:
        with open(os.path.join(gitdir, RECORD)) as f:
            rec = json.load(f)
        return rec if isinstance(rec, dict) else None
    except (OSError, ValueError):
        return None


def _write_record(gitdir, rec):
    c.write_atomic(os.path.join(gitdir, RECORD), json.dumps(rec, indent=2))


@contextlib.contextmanager
def _locked(gitdir):
    """One update at a time per checkout; None when another holds it."""
    f = open(os.path.join(gitdir, LOCK), "a")
    try:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        yield True
    finally:
        f.close()


# ---------------------------------------------------------------- the update

def _not_sampo(top, own):
    """A refusal unless top is Sampo's own checkout: its plugin manifest names sampo (or foreman), and for the default
    root (own) the checkout is that very folder, not a repo that merely encloses it."""
    try:
        with open(os.path.join(top, "plugin", ".claude-plugin", "plugin.json")) as f:
            name = json.load(f).get("name")
    except (OSError, ValueError, AttributeError):
        name = None
    if name not in ("sampo", "foreman"):
        return _refuse(f"{top} isn't Sampo's checkout (no plugin/.claude-plugin/plugin.json named sampo): "
                       "sampo update only moves the install it runs from, or one named with --repo")
    if own and os.path.realpath(top) != os.path.realpath(own):
        return _refuse(f"{own} sits inside another repo ({top}), not in a Sampo checkout of its own: nothing was changed")
    return None


def _local(root, own=None):
    """(gitdir, branch, refusal): what is wrong with this checkout before the network is asked."""
    rc, top, err = _git(root, "rev-parse", "--show-toplevel")
    if rc != 0:
        return None, None, _refuse(f"{root} is not a git checkout, so it has no origin to update from ({_line(err)})")
    _, gitdir, _ = _git(top, "rev-parse", "--absolute-git-dir")
    if (why := _not_sampo(top, own)):
        return gitdir, top, why
    rc, _, _ = _git(top, "remote", "get-url", "origin")
    if rc != 0:
        return gitdir, top, _refuse(f"{top} has no origin remote: add one (git remote add origin <url>) or update it by hand")
    _, branch, _ = _git(top, "symbolic-ref", "--short", "-q", "HEAD")
    if not branch:
        return gitdir, top, _refuse("HEAD is detached: a dev checkout updates by its own merges, check out "
                                    f"{RELEASE} first")
    if branch != RELEASE:
        return gitdir, top, _refuse(f"on branch {branch}, not {RELEASE}: a dev checkout updates by its own merges "
                                    f"(git merge origin/{RELEASE}); sampo update moves an install that follows {RELEASE}")
    _, dirty, _ = _git(top, "status", "--porcelain", "--untracked-files=no")
    if dirty:
        return gitdir, top, _refuse(f"{len(dirty.splitlines())} file(s) have uncommitted changes: commit or stash "
                                    "them first, an update never merges into work in progress")
    return gitdir, top, None


def _plan(top):
    """Fetch origin and compare: the plan (status plan|current) or a refusal."""
    rc, _, err = _git(top, "fetch", "--quiet", "origin", f"+refs/heads/{RELEASE}:refs/remotes/origin/{RELEASE}",
                      timeout=FETCH_S)
    if rc != 0:
        return _refuse(f"can't fetch origin ({_line(err.splitlines()[-1] if err else 'no answer')}): nothing was changed")
    rc, to, _ = _git(top, "rev-parse", "--verify", "-q", f"refs/remotes/origin/{RELEASE}^{{commit}}")
    _, frm, _ = _git(top, "rev-parse", "HEAD")
    if rc != 0 or not frm:
        return _refuse(f"origin has no {RELEASE} branch to update to")
    base = {"from": frm, "to": to, "version": {"from": _version(top, frm), "to": _version(top, to)}}
    if frm == to:
        return {**base, "status": "current", "ahead": 0}
    if _git(top, "merge-base", "--is-ancestor", to, frm)[0] == 0:
        _, n, _ = _git(top, "rev-list", "--count", f"{to}..{frm}")
        return {**base, "status": "current", "ahead": int(n or 0)}
    if _git(top, "merge-base", "--is-ancestor", frm, to)[0] != 0:
        return _refuse(f"history has diverged from origin/{RELEASE} (local commits it doesn't have, and it has some "
                       f"you don't): merge or rebase by hand, then run sampo update; nothing was changed")
    _, n, _ = _git(top, "rev-list", "--count", f"{frm}..{to}")
    _, log, _ = _git(top, "log", f"--max-count={SHOWN}", "--format=%h%x09%s", f"{frm}..{to}")
    commits = [{"sha": _line(sha), "subject": _line(subject)}
               for sha, subject in (row.split("\t", 1) for row in log.splitlines() if "\t" in row)]
    return {**base, "status": "plan", "count": int(n or 0), "commits": commits, "restart": _restart(top, frm, to)}


def run(root, check=False, dry=False, undo=False, own=None):
    """The result dict (status refused|current|plan|updated|undone, and what the report says). Changes nothing when
    check or dry; a refusal leaves the checkout as it was."""
    root = os.path.abspath(os.path.expanduser(str(root)))
    gitdir, top, refusal = _local(root, own) if not undo else _local_undo(root, own)
    if refusal:
        return refusal
    if check or dry:
        r = _undo_plan(top, gitdir) if undo else _plan(top)
        if r["status"] == "plan" and not undo:
            r["migrations"] = _migrations()
        return r
    with _locked(gitdir) as got:
        if not got:
            return _refuse("another sampo update is already running here")
        return _undo(top, gitdir) if undo else _apply(top, gitdir)


def _finish(top, gitdir, plan):
    """Current: a merge that landed but whose migrations or sampo-fast check never finished (a skip, Ctrl-C, a crash)
    is finished now; migrations whose dry run finds work run, and the fast check runs when the record says 'updating'."""
    rec = _record(gitdir)
    pending = {n: p for n, p in _migrations().items() if p["state"] == "pending"}
    unfinished = bool(rec and rec.get("status") == "updating")
    if not pending and not unfinished:
        return plan
    plan["migrations"] = {}
    for name, probe in pending.items():
        if rec and rec.get("status") in ("updating", "applied"):
            rec.setdefault("migrations", {})[name] = True
            with contextlib.suppress(OSError):
                _write_record(gitdir, rec)
        plan["migrations"][name] = _run_migration(name)
    if unfinished:
        plan["fast"] = _fast(top)
        rec["status"] = "applied"
        with contextlib.suppress(OSError):
            _write_record(gitdir, rec)
    return plan


def _apply(top, gitdir):
    plan = _plan(top)
    if plan["status"] == "current":
        return _finish(top, gitdir, plan)
    if plan["status"] != "plan":
        return plan
    frm, to = plan["from"], plan["to"]
    rc, _, err = _git(top, "merge", "--ff-only", "--quiet", to, timeout=120)
    if rc != 0:  # nothing moved: the record of the earlier update (its undo handle) stays as it was
        return _refuse(f"couldn't fast-forward to {_short(to)} ({_line(err)}): nothing was changed")
    previous = _record(gitdir)
    if previous:
        previous.pop("previous", None)
    rec = {"from": frm, "to": to, "status": "updating", "migrations": {}, "previous": previous,
           "at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    _write_record(gitdir, rec)  # right after the merge: a crash before the migrations finish leaves "updating"
    plan["migrations"] = {}

    def migrate(name):  # the new version's own migration code: imported only now, after the merge
        probe = _probe(name)
        if probe["state"] == "pending":
            rec["migrations"][name] = True  # pessimistic: --undo asks the migration, which says if it never ran
            _write_record(gitdir, rec)
            probe = _run_migration(name)
        plan["migrations"][name] = probe

    migrate("plugins")
    plan["fast"] = _fast(top)  # before the home moves: the paths it checks are still where they were
    migrate("home")
    rec["status"] = "applied"
    with contextlib.suppress(OSError):  # the home may just have moved: its old path is a link to the new one
        _write_record(gitdir, rec)
    plan["status"] = "updated"
    return plan


def _local_undo(root, own=None):
    rc, top, err = _git(root, "rev-parse", "--show-toplevel")
    if rc != 0:
        return None, None, _refuse(f"{root} is not a git checkout ({_line(err)})")
    _, gitdir, _ = _git(top, "rev-parse", "--absolute-git-dir")
    if (why := _not_sampo(top, own)):
        return gitdir, top, why
    rec = _record(gitdir)
    if not rec or rec.get("status") not in ("updating", "applied"):
        return gitdir, top, _refuse("nothing to undo: no sampo update recorded here, or it was already undone")
    _, head, _ = _git(top, "rev-parse", "HEAD")
    if head not in (rec.get("from"), rec.get("to")):
        return gitdir, top, _refuse(f"the branch moved on since the update (HEAD is {_short(head)}, the update left "
                                    f"{_short(str(rec.get('to')))}): undoing would drop commits, so git reset is yours to run")
    _, dirty, _ = _git(top, "status", "--porcelain", "--untracked-files=no")
    if dirty:
        return gitdir, top, _refuse(f"{len(dirty.splitlines())} file(s) have uncommitted changes: commit or stash them "
                                    "first, an undo resets the checkout")
    return gitdir, top, None


def _undo_plan(top, gitdir):
    rec = _record(gitdir)
    return {"status": "plan", "undo": True, "from": rec["to"], "to": rec["from"],
            "version": {"from": _version(top, rec["to"]), "to": _version(top, rec["from"])},
            "migrations": {n: {"state": "pending", "detail": "undone"} for n in rec.get("migrations") or {}}}


def _undo(top, gitdir):
    rec = _record(gitdir)
    _, head, _ = _git(top, "rev-parse", "HEAD")
    out = {"status": "undone", "from": rec["to"], "to": rec["from"],
           "version": {"from": _version(top, rec["to"]), "to": _version(top, rec["from"])}, "migrations": {}}
    if head != rec["from"]:
        rc, _, err = _git(top, "reset", "--hard", "--quiet", rec["from"], timeout=120)
        if rc != 0:
            return _refuse(f"couldn't put {_short(rec['from'])} back ({_line(err)}): nothing else was undone")
    out["fast"] = _fast(top)
    out["restart"] = _restart(top, rec["from"], rec["to"])
    todo = [n for n in reversed(MIGRATIONS) if (rec.get("migrations") or {}).get(n)]
    rec["status"] = "undone"
    _write_record(gitdir, rec)  # before the home moves back: this path is a link to it until then
    for name in todo:
        out["migrations"][name] = _run_migration(name, undo=True)
    return out


# ---------------------------------------------------------------- the report

def _style(text, code):
    on = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    return f"\033[{code}m{text}\033[0m" if on else text


def _migration_lines(migs):
    if not migs:
        return []
    shown = []
    for n, m in migs.items():
        extra = f" ({m['detail']})" if m.get("detail") and m["state"] in ("pending", "ran") else ""
        shown.append(f"{n} {m['state']}{extra}")
    lines = ["  migrations: " + ", ".join(shown)]
    lines += [f"  {n} not moved: {m['detail']}" for n, m in migs.items() if m["state"] == "skipped" and m.get("detail")]
    return lines


def render(r):
    s = r["status"]
    head = lambda t: _style(t, "1")  # noqa: E731
    dim = lambda t: _style(t, "2")  # noqa: E731
    if s == "refused":
        return f"sampo update: {r['why']}"
    v = r.get("version") or {}
    span = f"{_short(r['from'])}..{_short(r['to'])}" if r.get("from") else ""
    if s == "current":
        ahead = f" (ahead of origin/{RELEASE} by {r['ahead']} unpushed commit(s); nothing to update)" if r.get("ahead") else ""
        done = _migration_lines(r.get("migrations")) + ([f"  sampo-fast: {r['fast']['line']}"] if r.get("fast") else [])
        return "\n".join([head(f"sampo update: up to date at {v.get('from', '?')} ({_short(r['from'])}){ahead}")] + done)
    lines = []
    if s == "undone":
        lines.append(head(f"sampo update: undone, back to {v.get('to')} ({_short(r['to'])})"))
    else:
        verb = "would update" if s == "plan" else "updated"
        if r.get("undo"):
            verb = "would undo the update"
        lines.append(head(f"sampo update: {verb} {v.get('from')} -> {v.get('to')}") + dim(
            f" ({r.get('count', '')} commit(s), {span})" if r.get("count") else f" ({span})"))
    for x in r.get("commits") or []:
        lines.append(dim(f"  {x['sha']} {x['subject']}"))
    if r.get("count", 0) > len(r.get("commits") or []):
        lines.append(dim(f"  ... and {r['count'] - len(r['commits'])} more"))
    lines += _migration_lines(r.get("migrations"))
    if r.get("fast"):
        lines.append(f"  sampo-fast: {r['fast']['line']}")
    if r.get("restart") and s != "plan":
        lines.append("  " + r["restart"])
    elif r.get("restart"):
        lines.append("  then: " + r["restart"])
    if s == "updated":
        lines.append(dim(f"  sampo update --undo puts {_short(r['from'])} back (and undoes what it migrated)"))
    if s == "plan":
        lines.append(dim("  (check: nothing changed)"))
    return "\n".join(lines)


def cmd_update(args):
    from sampo import cli
    root = getattr(args, "repo", None) or os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.realpath(__file__)))))  # plugin/lib/sampo/update.py: the repo this sampo runs from
    pre = None
    with contextlib.suppress(Exception):  # the project is found before a home move can rename the folders under it
        pre = c.find_project(root, create=False)
    r = run(root, check=args.check, dry=args.dry_run, undo=args.undo, own=None if getattr(args, "repo", None) else root)
    cli.out(args, r, render(r))
    if r["status"] in ("updated", "undone") and pre:
        with contextlib.suppress(Exception):  # the event is a courtesy: an update that landed is not undone by a full disk
            c.log_event(pre, "update", data={"from": r["from"], "to": r["to"], "undo": r["status"] == "undone",
                                             "version": r.get("version"), "migrations": {
                                                 n: m["state"] for n, m in (r.get("migrations") or {}).items()},
                                             "fast": (r.get("fast") or {}).get("ok")})
    return 1 if r["status"] == "refused" else 0
