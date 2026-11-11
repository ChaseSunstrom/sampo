"""fm tidy: hygiene for Sampo state, auto memory, CLAUDE.md and rules (BUILD_PROMPT §9).

Dry-run by default. --apply performs only safe, reversible actions (archive, rotate, roll up, dedupe after
archiving a copy) and logs them; everything needing judgment (stale inbox items, dead paths, cycles,
oversize memory, contradictions) is reported for a decision. Nothing is deleted without an archive copy,
except derived caches (old statusline snapshots).
"""
import datetime
import fcntl
import glob
import json
import os
import re
import shutil
import stat
import time

from sampo import core as c

ARCHIVE_AFTER_DAYS = 14
INBOX_STALE_DAYS = 30
LEDGER_ROLL_BYTES = 1_000_000
EVENTS_ROTATE_BYTES = 5_000_000
HOOKS_LOG_ROTATE_BYTES = 1_000_000
ROTATED_KEEP = 3
SNAPSHOT_MAX_DAYS = 7
STACKS_MAX_DAYS = 14  # T-1405: state/logs/stacks, a hung long verb's stack dumps
REVERTED_DAYS = 30  # T-1442: fm code's kept-aside copies of reverted files
WORK_SNAPSHOT_DAYS = 14 # T-0884: saved working files before a discarding command
MEMORY_MAX_LINES, MEMORY_MAX_BYTES = 200, 25_000
GLOBAL_MD_MAX, PROJECT_MD_MAX = 50, 150
KNOWN_PLUGINS = {"ponytail", "claude-hud", "security-guidance", "code-review", "context7", "skill-creator",
                 "claude-md-management", "plugin-dev", "frontend-design", "clangd-lsp", "rust-analyzer-lsp",
                 "pyright-lsp", "typescript-lsp", "gopls-lsp", "playwright", "chrome-devtools-mcp", "sampo", "foreman",
                 "ecc", "superpowers", "design", "figma", "document-skills", "differential-review",
                 "insecure-defaults", "static-analysis", "sharp-edges", "supply-chain-risk-auditor", "c-review"}
LSP_BINARIES = {"clangd-lsp": "clangd", "pyright-lsp": "pyright-langserver", "typescript-lsp": "typescript-language-server",
                "gopls-lsp": "gopls", "rust-analyzer-lsp": "rust-analyzer"}


def finding(project, kind, severity, detail, fix="", auto=False):
    return {"project": project, "kind": kind, "severity": severity, "detail": detail, "fix": fix, "auto": auto}


# ---------------------------------------------------------------- helpers

_PATH_TOKEN = re.compile(r"`([^`\s]+)`")


def _looks_like_path(tok):
    if any(ch in tok for ch in "<>$*{}|()=,;") or "://" in tok or tok.startswith(("-", "@", "#")):
        return False
    if re.fullmatch(r"/[\w:-]+", tok):  # a slash command such as /sampo:intake, not a path
        return False
    return "/" in tok or bool(re.fullmatch(r"[\w.-]+\.[A-Za-z0-9]{1,6}", tok))


def dead_paths(text, base):
    out = []
    for tok in _PATH_TOKEN.findall(text):
        if not _looks_like_path(tok):
            continue
        path = os.path.expanduser(tok) if tok.startswith("~") else os.path.join(base, tok)
        if not os.path.exists(path.rstrip("/")) and tok not in out:
            out.append(tok)
    return out


def stale_commands(text, root):
    out = []
    pkg = os.path.join(root, "package.json")
    scripts = None
    if os.path.isfile(pkg) and not os.path.islink(pkg):  # a link may point at a FIFO or outside the tree (fm find)
        try:
            with open(pkg, encoding="utf-8", errors="replace") as f:
                scripts = set((json.load(f).get("scripts") or {}).keys())
        except (OSError, ValueError, TypeError, AttributeError, RecursionError):
            scripts = None
    if scripts is not None:  # without a package.json, npm lines are generic guidance, not this repo's commands
        for m in re.finditer(r"\b(?:npm run|pnpm run|yarn run|bun run)\s+([\w:.-]+)", text):
            if m.group(1) not in scripts:
                out.append(m.group(0))
    mk = os.path.join(root, "Makefile")
    if os.path.isfile(mk) and not os.path.islink(mk):
        with open(mk, errors="replace") as f:
            targets = set(re.findall(r"^([\w.-]+)\s*:(?!=)", f.read(1 << 20), re.M))
        for m in re.finditer(r"`make\s+([\w.-]+)", text):
            if m.group(1) not in targets:
                out.append(f"make {m.group(1)}")
    return list(dict.fromkeys(out))


def _norm(line):
    return re.sub(r"\s+", " ", line.strip().lstrip("-*").strip().lower())


def _read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def memory_dir(root):
    return os.path.join(os.path.expanduser("~"), ".claude", "projects", re.sub(r"[^A-Za-z0-9]", "-", root), "memory")


# ---------------------------------------------------------------- per-project checks

def unblocked(p, b, status):
    """T-0657: why a blocked task may be worth retrying — every task it depends on closed, or a project file its block
    reason names changed since it was blocked — or None."""
    deps = b.meta.get("depends_on") or []
    if deps and all(status.get(d) in c.CLOSED for d in deps):
        return "its dependencies are closed"
    rv = b.meta.get("revisit")  # T-1183: --until installed NAME / exists PATH / a date / a changed file
    if rv and c.deferral_due(p.root, rv, datetime.date.today().isoformat()):
        return f"its --until condition holds ({rv.split(' @')[0]})"
    last = re.findall(r"(?m)^- (\S+) blocked: (.*)$", b.section("Log"))
    at = c.parse_ts(last[-1][0]) if last else None
    if not at:
        return None
    for tok in re.findall(r"[\w./-]+", last[-1][1]):
        tok = tok.rstrip(".")
        path = os.path.realpath(os.path.join(p.root, tok))
        if (_looks_like_path(tok) and path.startswith(os.path.realpath(p.root) + os.sep) and os.path.isfile(path)
                and _changed_since(p.root, path, at.timestamp())):
            return f"{tok} changed since it was blocked"
    return None


def _changed_since(root, path, t):
    """T-1106: a commit touched path after t, or it has uncommitted edits made after t. Not its mtime alone: a fresh
    clone or a builder's worktree gives every file a new one (T-1077's lesson)."""
    if not c.git_root(root):
        return os.path.getmtime(path) > t + 1
    rel = os.path.relpath(path, root)
    if c._git(root, "log", "-1", "--format=%ct", f"--since=@{int(t) + 1}", "--", rel, timeout=30).strip():
        return True
    dirty = c._git(root, "status", "--porcelain", "--", rel, timeout=30).strip()
    return bool(dirty) and os.path.getmtime(path) > t + 1


def _git_facts(root, since):
    """T-1077: the paths in HEAD, the ones changed since HEAD, and when each path last changed on the first-parent
    line since `since` (a merged lane's change at its merge), in three git calls; all empty outside git."""
    head = set(c._git(root, "ls-tree", "-rz", "--name-only", "HEAD", timeout=30).split("\0")) - {""}
    dirty = set(c._git(root, "diff", "-z", "--name-only", "HEAD", timeout=30).split("\0")) - {""}
    log, ct = {}, 0
    for line in c._git(root, "log", "--first-parent", "--diff-merges=first-parent", "--no-renames", "--name-only",
                       f"--since=@{int(since) - 2}", "--format=%x01%ct", timeout=30).splitlines():
        if line.startswith("\x01"):
            ct = int(line[1:])
        elif line:
            log.setdefault(line, ct)  # newest first
    return head, dirty, log


def stale_plans(p, briefs):
    """T-1077: {id: why} for each queued (planned) brief that needs a re-plan: a dependency was dropped, a scope path
    changed after the brief was last edited, or a scope path that existed is gone (one that never existed is a file the
    plan creates). Seen from the main checkout, as git has it: a tracked file by when its change landed, so a fresh
    clone's or a lane's checkout mtimes mean nothing; an uncommitted or untracked one by its mtime.
    ponytail: a fast-forwarded commit counts at the time it was made, not when it landed."""
    status = {b.id: b.status for b in briefs}
    planned = [b for b in briefs if b.status == "planned"]
    ats = {b.id: t.timestamp() + 1 for b in planned if (t := c.parse_ts(b.meta.get("updated")))}
    root = os.path.realpath(c.main_view(p).root)
    if any(b.meta.get("scope") for b in planned if b.id in ats):
        head, dirty, log = _git_facts(root, min(ats.values()))
    out = {}
    for b in planned:
        dropped = next((d for d in b.meta.get("depends_on") or [] if status.get(d) == "dropped"), None)
        if dropped:
            out[b.id] = f"its dependency {dropped} was dropped"
            continue
        at = ats.get(b.id)
        for s in b.meta.get("scope") or [] if at else []:
            path = os.path.realpath(os.path.join(root, s))
            if re.search(r"[*?\[]", s) or not path.startswith(root + os.sep):
                continue
            if os.path.basename(path) == "CHANGELOG.md":  # T-1890: every finished task appends to it: no re-plan
                continue
            rel = os.path.relpath(path, root)
            landed = max((t for f, t in log.items() if f == rel or f.startswith(rel + "/")), default=0)
            if not os.path.exists(path):
                if rel in head or landed > at:
                    out[b.id] = f"{s} is gone"
                    break
            elif landed > at or (os.path.isfile(path) and (rel in dirty or rel not in head)
                                 and os.path.getmtime(path) > at):
                out[b.id] = f"{s} changed since it was planned"
                break
    return out


DEFER_DROP_DAYS = 90


def triage_deferred(p, briefs):
    """T-1048: [(id, verdict, why)] for each deferred brief; proposes only, nothing is applied. do: its --until date or
    file change came (nothing else says it is ready: a young brief with untouched files is not). drop: 90+ days old
    with no scope file left (or none named) and no revisit trigger. ask: the rest."""
    root, today, out = c.main_view(p).root, c.datetime.date.today().isoformat(), []
    for b in briefs:
        if b.status != "deferred":
            continue
        age, rv = c.age_days(b.meta.get("updated")) or 0, b.meta.get("revisit")
        scope = b.meta.get("scope") or []
        live = gone = False
        for s in scope:  # like stale_plans: a path that never existed is a file the plan creates, not a dead one
            if re.search(r"[*?\[]", s):
                live = live or bool(glob.glob(os.path.join(root, s), recursive=True))
            elif os.path.exists(os.path.join(root, s)):
                live = True
            elif c._git(root, "log", "-1", "--format=%ct", "--", s, timeout=30).strip():
                gone = True
        if rv and c.deferral_due(root, rv, today):
            out.append((b.id, "do", f"its revisit trigger came ({rv})"))
        elif age >= DEFER_DROP_DAYS and not live and not rv and (gone or not scope):
            out.append((b.id, "drop", f"{int(age)}d old, " + ("its scope is gone" if gone else "no scope and no trigger")))
        else:
            out.append((b.id, "ask", f"{int(age)}d old" + (f", revisit {rv}" if rv else "")))
    return out


EXAM_WORK_DAYS = 14


def _prune_exam_work(p, apply, actions):
    """T-2568: a graded exam run's work copies (the repo copies its grade reads) once its newest grade is EXAM_WORK_DAYS
    old and a newer graded run of the same tier exists for that agent. Answers, grades and keys stay; a PRUNED marker
    makes a regrade ask for a rerun. Ungraded runs are never touched."""
    from sampo import louhi
    from sampo import rune
    last = {}  # (tier, agent, run) -> newest grade time
    for x in c.tail_jsonl(os.path.join(rune.home(p), "exam.jsonl"), 100000):
        if isinstance(x, dict) and x.get("run") and x.get("agent") and x.get("at"):
            k = ("long" if x.get("tier") == "long" else "short", str(x["agent"]), str(x["run"]))
            last[k] = max(last.get(k, ""), str(x["at"]))
    newest = {}
    for (tier, agent, _), at in last.items():
        newest[(tier, agent)] = max(newest.get((tier, agent), ""), at)
    out = []
    for (tier, agent, run), at in sorted(last.items()):
        if "/" in run or "/" in agent or run.startswith(".") or agent.startswith("."):
            continue  # a line's text never names a path outside the exam store
        base = os.path.join(louhi.root(p), *(("long",) if tier == "long" else ()), run, "work", agent)
        age = c.age_days(at) or 0
        if (at == newest[(tier, agent)] or age < EXAM_WORK_DAYS or os.path.islink(base) or not os.path.isdir(base)
                or louhi.pruned(base)):  # a symlinked work dir is never followed
            continue
        mb = sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(base) for f in fs
                 if not os.path.islink(os.path.join(d, f))) / 1e6
        out.append(finding(p.slug, "exam_work", "action", f"{tier} exam {run} ({agent}): work copies {mb:.0f} MB, "
                           f"graded {int(age)}d ago, a newer run graded since", "remove them; answers and grades stay",
                           auto=True))
        if apply:
            shutil.rmtree(base, ignore_errors=True)
            os.makedirs(base, exist_ok=True)
            c.write_atomic(os.path.join(base, louhi.PRUNED), at + "\n")
            actions.append(("exam_work", f"{run}/{agent}"))
    return out


def check_project(p, apply, actions):
    out, slug = [], p.slug
    briefs = c.load_briefs(p)
    from sampo import docs  # fmdocs reuses this module's detectors
    for f in docs.scan(p.root):
        out.append(finding(slug, "docs_drift", "warn", f"{f['file']}: {f['kind']} `{f['detail']}` doesn't exist",
                           "update the doc (fm docs lists every drift)"))
    for b in briefs:
        age = c.age_days(b.meta.get("updated")) or 0
        if b.status in c.CLOSED and age > ARCHIVE_AFTER_DAYS:
            out.append(finding(slug, "archive_task", "action", f"{b.id} {b.status} {int(age)}d ago: {b.title}",
                               "move to archive/YYYY-MM/", auto=True))
            if apply:
                dest = os.path.join(p.dir, "archive", (b.meta.get("updated") or c.now())[:7])  # not a list: a path or word fragment, not a list
                os.makedirs(dest, exist_ok=True)
                shutil.move(b.path, os.path.join(dest, os.path.basename(b.path)))
                actions.append(("archive_task", b.id))
        elif b.status == "captured" and age > INBOX_STALE_DAYS:
            out.append(finding(slug, "inbox_stale", "warn", f"{b.id} captured {int(age)}d ago: {b.title}",
                               f"keep (fm task new … --from {b.id}) or drop (fm task drop {b.id} \"<why>\")"))
    _, cycles, dangling = c.order_queue(briefs, c.archived_ids(p))
    by_id = {b.id: b for b in briefs}
    for cyc in cycles:  # T-0383: a cycle through an edge fm relate inferred is undone there
        guess = next(((a, d) for a in cyc for d in c._deps(by_id[a]) if d in cyc
                      and d not in (by_id[a].meta.get("depends_on") or [])), None)
        out.append(finding(slug, "cycle", "warn", "dependency cycle: " + " ↔ ".join(cyc),
                           f"fm relate --drop {guess[0]} {guess[1]} (inferred) or fm relate --clear" if guess
                           else "fm task set ID depends_on=…"))
    for a, d in dangling:
        out.append(finding(slug, "dangling_dep", "warn", f"{a} depends on unknown {d}", f"fm task set {a} depends_on=…"))
    status = {b.id: b.status for b in briefs}
    for tid, why in stale_plans(p, briefs).items():
        out.append(finding(slug, "stale_plan", "warn", f"{tid} re-plan: {why}", f're-read its Steps against the code, '
                           f'fix what no longer holds, then fm task log {tid} "plan re-read: <what changed, or still valid>"'))
    for date, decision, sunset in c.expired_decisions(p):  # T-1084
        out.append(finding(slug, "decision_expired", "warn", f"decision of {date} hit its sunset {sunset}: {c.fit(decision, 80)}",
                           'keep it (fm decide "<it again>" --revisited WORDS) or undo it (--reverses WORDS)'))
    asks = []
    for tid, verdict, why in triage_deferred(p, briefs):
        if verdict == "ask":
            asks.append(tid)
            continue
        out.append(finding(slug, "deferred_triage", "warn" if verdict == "drop" else "info", f"{tid} deferred: propose {verdict} ({why})",
                           f'fm task drop {tid} "{why}"' if verdict == "drop" else f"fm task set {tid} status=planned"))
    if asks:
        out.append(finding(slug, "deferred_triage", "info", f"{len(asks)} deferred briefs to decide: {', '.join(asks)}",
                           "do each (fm task set ID status=planned) or drop it (fm task drop ID \"<why>\")"))
    for b in briefs:
        why = unblocked(p, b, status) if b.status == "blocked" else None
        if why:
            out.append(finding(slug, "unblockable", "action", f"{b.id} is blocked but {why}",
                               "reopened as planned: fm next picks it up", auto=True))
            if apply:  # T-0657: what it waited on changed, so it's worth another try
                from sampo import cli
                cli.mutate(p, b.id, lambda x: (x.meta.update(status="planned"), x.append_log(f"reopened: {why}")),
                             "task_set", {"status": "planned", "why": why})
                actions.append(("reopen", b.id))
    from sampo import cli
    for tid in cli.unanswered_asks(p):  # T-0622: an ask nobody answered goes quiet after a week, never deleted
        b = c.find_brief(p, tid)
        if b and (c.age_days(b.meta.get("created")) or 0) > cli.ASK_DAYS:
            out.append(finding(slug, "ask_expired", "action", f"{tid} (an unanswered ask from fm second session) is "
                               f"{cli.ASK_DAYS}+ days old", "deferred, not dropped", auto=True))
            if apply:
                cli.mutate(p, tid, lambda x: (x.meta.update(status="deferred"), x.append_log(
                    f"deferred: an unanswered ask, {cli.ASK_DAYS}+ days old (T-0622)")), "task_defer",
                    {"reason": "unanswered ask expired"})
                actions.append(("ask_expired", tid))
    from sampo import repeats
    n = repeats.open_candidates(repeats.scan(p))
    if n:
        out.append(finding(slug, "repeats", "info", f"{n} repeated command(s) or procedure(s) could become project tools",
                           "fm repeats lists them, each with a suggestion (fm check add, a script, a project skill)"))
    from sampo import lanes as slanes
    out += slanes.stale(p, briefs, apply, actions, finding)
    out += _roll_ledger(p, apply, actions)
    out += _prune_snapshots(p, apply, actions)
    out += _prune_exam_work(p, apply, actions)  # T-2568
    out += _prune_reverted(p, apply, actions)
    out += _prune_door_snapshots(p, apply, actions)
    out += _backup_state(p, apply, actions)
    out += _check_memory(p, apply, actions)
    out += _hygiene(p, apply, actions)
    out += _check_instructions(slug, p.root, [os.path.join(p.root, "CLAUDE.md"), os.path.join(p.root, ".claude", "CLAUDE.md"),
                                              os.path.join(p.root, "CLAUDE.local.md")],
                               os.path.join(p.root, ".claude", "rules"), PROJECT_MD_MAX)
    if apply:
        c.update_meta(p, last_tidy=c.now())  # T-1879: before the views, or STATE.md says "Last tidy: never" after one
        with c.lock(p.dir):
            c.regen_views(p)
        c.log_event(p, "tidy", data={"actions": [f"{k}:{v}" for k, v in actions if k != "global"][:50],
                                     "findings": len(out)})
    return out


HINT_STALE_DAYS = 60


def _hygiene(p, apply, actions):
    """T-1072: a known fix that keeps missing is removed on --apply (its line copied to archive/fixes-removed.md first);
    one unused for 60 days is only listed (a quarterly fix is still a fix), and so is one synced from the repo (fmsync
    would put it back). Of the cards the map picked for this project, those no edit used in 60 days are listed once
    (a shipped card file is never retired). Lessons keep no fire record."""
    from sampo import fixes
    slug, out, st = p.slug, [], fixes._load(fixes._stats_path())
    for f in fixes.known(p, st):
        x = (st.get("fixes") or {}).get(f["id"]) or {}
        age = c.age_days(x.get("last"))  # no record at all: unknown age, kept
        losing = fixes._losing(st, f["id"])
        if not (losing or (age and age > HINT_STALE_DAYS)):
            continue
        synced = f["store"] == "project" and not f["trusted"]
        what = "keeps missing" if losing else f"unused {int(age)}d"
        out.append(finding(slug, "losing_fix" if losing else "stale_fix", "warn",
                           f"known fix `{f['sig'][:60]}` ({what}{'; shared: change it in the repo file' if synced else ''})",
                           "fm tidy --apply retires it" if losing and not synced else
                           "listed only: fm fix rm <n> if it no longer applies", auto=losing and not synced))
        if apply and losing and not synced:
            line = fixes._line({k: v for k, v in f.items() if k != "trusted"})
            if fixes.remove(p, f["id"]):
                d = os.path.join(p.dir, "archive")
                os.makedirs(d, exist_ok=True)
                with open(os.path.join(d, "fixes-removed.md"), "a", encoding="utf-8") as fh:
                    fh.write(f"<!-- removed {c.now()[:10]} -->\n{line}")
                actions.append(("losing_fix", f["id"]))
    try:
        with open(os.path.join(p.dir, "map.json"), encoding="utf-8") as fh:
            rows = json.load(fh).get("lib_cards")
    except (OSError, ValueError, AttributeError):
        rows = None
    mine = {x["name"] for x in rows if isinstance(x, dict) and isinstance(x.get("name"), str)} if isinstance(rows, list) else set()
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - HINT_STALE_DAYS * 86400))
    used = {(e.get("data") or {}).get("card") for e in c.ledger_events(p, ("enforce",), since=since)
            if (e.get("data") or {}).get("kind") == "card_injected"}
    hit = c.rule_last_hits(p)  # T-1115: rules no call matched in the window, older than it (the user removes them)
    quiet = [f"{n} `{c.fit(r['pattern'], 40)}`" for n, r in enumerate(c.rules(p), 1)
             if str(r.get("at") or "") < since and str(hit.get(r["pattern"]) or "") < since]
    if quiet:
        out.append(finding(slug, "quiet_rule", "info", f"rule(s) no call matched in {HINT_STALE_DAYS} days: "
                           + c.joinmore(quiet, 8), "listed only: the user removes a rule (fm rule rm N) if it's obsolete"))
    ignored = sorted(c.hints_quiet(p))
    if ignored:
        out.append(finding(slug, "ignored_hint", "info", f"hints shown {c.QUIET_AFTER}+ times running without use: "
                           + ", ".join(ignored), "listed only: Next already shortens them to the bare command"))
    names = sorted(mine - used)
    if names:  # info, not warn: a clean project shouldn't read as needing action
        out.append(finding(slug, "unused_card", "info", f"{len(names)} of {len(mine)} cards picked for this project reached no "
                           f"edit here in {HINT_STALE_DAYS} days: {', '.join(names[:8])}",
                           "listed only: cards ship with the plugin (fm lib stats shows use)"))
    return out


BACKUP_KEEP = 5
BACKUP_PARTS = ("tasks", "archive", "meta.json", "ledger.jsonl", "decisions.md")  # the record; the rest is derived


def _backup_state(p, apply, actions):
    """T-0922: a dated copy of the project's record (briefs, ledger, meta, decisions) at each tidy --apply, the newest
    BACKUP_KEEP kept; views, graphs, research and audits are rebuilt or reference, so they stay out."""
    if not apply:
        return []
    import tarfile
    d = os.path.join(p.dir, "backups")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + ".tar.gz")
    with tarfile.open(path + ".part", "w:gz") as t:  # no lock: seconds of gzip would starve the hooks' quick locks
        # (state files are replaced whole; a ledger line caught mid-append only costs the copy its last line)
        for part in BACKUP_PARTS:
            if os.path.exists(os.path.join(p.dir, part)):
                t.add(os.path.join(p.dir, part), arcname=part)
    os.replace(path + ".part", path)
    old = sorted(f for f in os.listdir(d) if f.endswith(".tar.gz"))[:-BACKUP_KEEP]
    for f in old:
        os.remove(os.path.join(d, f))
    actions.append(("backup", os.path.basename(path)))
    return [finding(p.slug, "state_backup", "info", f"record backed up to backups/{os.path.basename(path)}",
                    f"the newest {BACKUP_KEEP} are kept" + (f"; {len(old)} older removed" if old else ""))]


def _prune_snapshots(p, apply, actions):
    """T-0884: work snapshots (refs/foreman/snapshots/<UTC stamp>-<sha>) older than WORK_SNAPSHOT_DAYS go."""
    top = c.git_root(p.root)
    refs = c._git(top, "for-each-ref", "--format=%(refname)", c.SNAPSHOTS, timeout=30).split() if top else []
    cut = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(time.time() - WORK_SNAPSHOT_DAYS * 86400))
    old = [r for r in refs if r[len(c.SNAPSHOTS):].split("-", 1)[0] < cut]
    if not old:
        return []
    if apply:
        for r in old:
            c._git(top, "update-ref", "-d", r, timeout=10)
        actions.append(("snapshots", len(old)))
    return [finding(p.slug, "work_snapshots", "action", f"{len(old)} work snapshot(s) older than {WORK_SNAPSHOT_DAYS}d",
                    "deleted (git gc reclaims them)", auto=True)]


def _prune_reverted(p, apply, actions):
    """T-1442: fm code's private copies of reverted files (<state>/code/reverted/) older than REVERTED_DAYS go. Plain files
    only: a link, a folder, or a reverted/ that is itself a link (the worker can write there) is left alone."""
    d, cut, old = os.path.join(p.dir, "code", "reverted"), time.time() - REVERTED_DAYS * 86400, []
    try:
        if not stat.S_ISDIR(os.lstat(os.path.join(p.dir, "code")).st_mode) or not stat.S_ISDIR(os.lstat(d).st_mode):
            return []
        for n in os.listdir(d):
            st = os.lstat(os.path.join(d, n))
            if stat.S_ISREG(st.st_mode) and st.st_mtime < cut:
                old.append(os.path.join(d, n))
    except OSError:
        return []
    if not old:
        return []
    if apply:
        for f in old:
            try:
                os.unlink(f)
            except OSError:  # gone meanwhile
                pass
        actions.append(("reverted", len(old)))
    return [finding(p.slug, "old_reverted", "action", f"{len(old)} fm code reverted cop(ies) older than {REVERTED_DAYS}d",
                    "deleted (the worker's discarded file versions)", auto=True)]


def _prune_door_snapshots(p, apply, actions):
    """T-2342: the copies fm focus takes before a one-way step (<state>/door-snapshots/<task>-step<N>[.json]) go once their
    task has been closed for WORK_SNAPSHOT_DAYS; a task still open (or one this can't find) keeps them. A link is never followed."""
    d, old = os.path.join(p.dir, "door-snapshots"), {}
    try:
        if not stat.S_ISDIR(os.lstat(d).st_mode):
            return []
        briefs = {b.id: b for b in c.load_briefs(p, include_archive=True)}
        for n in os.listdir(d):
            m = re.match(r"(T-\d+)-(?:step|verify)\d+(?:\.json|\.out)?$", n)  # T-4377: .out holds the outside paths' copies
            b = briefs.get(m.group(1)) if m else None
            if b and b.status in c.CLOSED and (c.age_days(b.meta.get("updated")) or 0) > WORK_SNAPSHOT_DAYS:
                old[n] = os.path.join(d, n)
    except OSError:
        return []
    if not old:
        return []
    if apply:
        for f in old.values():
            try:
                if os.path.isdir(f) and not os.path.islink(f):
                    shutil.rmtree(f, ignore_errors=True)
                else:
                    os.unlink(f)
            except OSError:
                pass
        actions.append(("door_snapshots", len(old)))
    return [finding(p.slug, "old_door_snapshots", "action",
                    f"{len(old)} one-way-step snapshot(s) of tasks closed over {WORK_SNAPSHOT_DAYS}d ago",
                    "deleted (their Undo lines no longer restore anything)", auto=True)]


def _roll_ledger(p, apply, actions):
    path = os.path.join(p.dir, "ledger.jsonl")
    try:
        size = os.path.getsize(path)
    except OSError:
        return []
    if size <= LEDGER_ROLL_BYTES:
        return []
    out = [finding(p.slug, "roll_ledger", "action", f"ledger.jsonl is {size // 1024} KB",
                   "move past months to archive/ledger-YYYY-MM.jsonl", auto=True)]
    if apply:
        month, rolled = c.now()[:7], {}
        # T-1099: bytes, under the ledger's own flock (log_event's), rewritten in place: write_atomic would swap the
        # inode, and a writer waiting on the old one would append to the unlinked file. ponytail: an older Foreman's
        # log_event never flocks, so its line can land mid-roll (lost) or between a writer's tail read and its write
        # (a false break in fm verify-ledger); closing that needs every writer upgraded.
        side = path + ".rolling"
        with open(path, "r+b") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            _resume_rewrite(f, side)
            f.seek(0)
            lines = f.readlines()
            run = ([os.path.basename(a)[7:14] for a in c.ledger_files(p)[:-1]] or [None])[-1]
            months = []
            for line in lines:  # months never fall, so the archives read in chain order and the rolled lines are a prefix
                m = _ledger_month(line, month)
                run = max(run, m) if run and m else run or m  # no ts, or a future one: the line before's month
                months.append(run)
            first = next((m for m in months if m), month)  # leading lines with no ts go with the first that has one
            months = [m or first for m in months]
            cut = sum(m < month for m in months)
            for line, m in zip(lines[:cut], months):
                rolled.setdefault(m, []).append(line if line.endswith(b"\n") else line + b"\n")
            os.makedirs(os.path.join(p.dir, "archive"), exist_ok=True)
            for m, chunk in rolled.items():
                chunk = b"".join(chunk)
                with open(os.path.join(p.dir, "archive", f"ledger-{m}.jsonl"), "a+b") as a:
                    a.seek(max(0, a.seek(0, os.SEEK_END) - len(chunk)))
                    if a.read() != chunk:  # equal: a roll that died before its rewrite archived these already
                        a.write(chunk)
                        a.flush()
                        os.fsync(a.fileno())  # archived for good before the live file lets them go
            if cut:
                _rewrite(f, side, b"".join(lines), b"".join(lines[cut:]))
        if rolled:
            c.log_event(p, "ledger_rolled", data={m: len(v) for m, v in rolled.items()})
            actions.append(("roll_ledger", ",".join(sorted(rolled))))
    return out


def _rewrite(f, side, whole, keep):
    """The live ledger `whole` cut down to its suffix `keep`, in place. The sidecar first holds keep, the old size and the
    hash of the bytes past keep that the rewrite leaves until its truncate, so a roll that dies midway can be resumed."""
    c.write_atomic(side, b"%d %s\n" % (len(whole), c.ledger_hash(whole[len(keep):]).encode()) + keep)
    f.seek(0)
    f.write(keep)
    f.flush()
    os.ftruncate(f.fileno(), len(keep))
    os.fsync(f.fileno())
    os.remove(side)


def _resume_rewrite(f, side):
    """T-1099 review: finish a rewrite a dead roll left. Bytes past the sidecar's keep still as they were: it never
    truncated, so the live ledger is keep plus whatever landed past the old size since. Else it did, and the sidecar
    is stale."""
    try:
        with open(side, "rb") as s:
            head, keep = s.readline(), s.read()
    except FileNotFoundError:
        return
    size, h = head.split()
    live = f.read()
    if c.ledger_hash(live[len(keep):int(size)]) == h.decode():
        _rewrite(f, side, live, keep + live[int(size):])
    else:
        os.remove(side)


def _ledger_month(line, month):
    """A ledger line's YYYY-MM, None when its ts is missing, unparseable or past `month` (a clock that ran ahead)."""
    try:
        ts = json.loads(line.decode("utf-8", "replace")).get("ts")
    except (ValueError, AttributeError, RecursionError):
        return None
    m = ts[:7] if isinstance(ts, str) and re.match(r"\d{4}-\d\d", ts) else None
    return m if m and m <= month else None


def _check_memory(p, apply, actions):
    d = memory_dir(p.root)
    index = os.path.join(d, "MEMORY.md")
    text = _read(index)
    out = []
    if text is None:
        return out
    lines = text.splitlines()
    if len(lines) > MEMORY_MAX_LINES or len(text.encode()) > MEMORY_MAX_BYTES:
        out.append(finding(p.slug, "memory_oversize", "warn", f"MEMORY.md is {len(lines)} lines / {len(text.encode())} B "
                           f"(startup window {MEMORY_MAX_LINES} lines / {MEMORY_MAX_BYTES} B)", "move detail into topic files"))
    seen, dupes, deduped = set(), [], []
    for line in lines:
        key = _norm(line)
        if key and not line.lstrip().startswith("#") and key in seen:
            dupes.append(line.strip())
            continue
        seen.add(key)
        deduped.append(line)
    if dupes:
        out.append(finding(p.slug, "memory_duplicate", "action", f"{len(dupes)} duplicate MEMORY.md line(s): {dupes[0][:80]}",
                           "keep the first occurrence (a copy is archived first)", auto=True))
        if apply:
            os.makedirs(os.path.join(p.dir, "archive"), exist_ok=True)
            shutil.copy2(index, os.path.join(p.dir, "archive", f"memory-{time.strftime('%Y%m%d-%H%M%S')}.md"))
            c.write_atomic(index, "\n".join(deduped) + ("\n" if text.endswith("\n") else ""))
            actions.append(("memory_duplicate", len(dupes)))
    for tok in dead_paths(text, p.root):
        out.append(finding(p.slug, "memory_dead_path", "warn", f"MEMORY.md mentions missing path `{tok}`",
                           "update or remove the entry"))
    for f in sorted(os.listdir(d)):
        full = os.path.join(d, f)
        if f != "MEMORY.md" and re.search(r"progress|status|todo|tasks", f, re.I) and os.path.getsize(full) > 20_000:
            out.append(finding(p.slug, "memory_progress_log", "info", f"memory/{f} is {os.path.getsize(full) // 1024} KB of "
                               "progress notes", "task status belongs in Sampo briefs/STATE; keep memory for durable learnings"))
    return out


def _check_instructions(slug, base, md_files, rules_dir, max_lines):
    out, texts = [], {}
    for path in md_files:
        text = _read(path)
        if text is None:
            continue
        texts[path] = text
        n = len(text.splitlines())
        if n > max_lines:
            out.append(finding(slug, "claude_md_size", "warn", f"{path} is {n} lines (budget {max_lines})",
                               "move detail to rules with paths: or to docs read on demand"))
        for tok in dead_paths(text, base):
            out.append(finding(slug, "dead_path", "warn", f"{os.path.basename(path)} mentions missing path `{tok}`",
                               "fix or remove the line"))
        for cmd in stale_commands(text, base):
            out.append(finding(slug, "stale_command", "warn", f"{os.path.basename(path)} mentions `{cmd}`, not defined in "
                               "package.json/Makefile", "update the command"))
    rules = {}
    if os.path.isdir(rules_dir):
        for f in sorted(os.listdir(rules_dir)):
            if f.endswith(".md"):
                rules[f] = _read(os.path.join(rules_dir, f)) or ""
                for tok in dead_paths(rules[f], base):
                    out.append(finding(slug, "dead_path", "warn", f"rules/{f} mentions missing path `{tok}`", "fix the rule"))
    md_lines = {_norm(l) for t in texts.values() for l in t.splitlines() if len(_norm(l)) > 25}
    for f, t in rules.items():
        for line in t.splitlines():
            if len(_norm(line)) > 25 and _norm(line) in md_lines:
                out.append(finding(slug, "rule_duplicate", "warn", f"rules/{f} repeats a CLAUDE.md line: {line.strip()[:80]}",
                                   "keep it in one place"))
    return out


# ---------------------------------------------------------------- global checks

def check_global(apply, actions, plugins=False):
    """T-3423: hooks.log is rotated by hooks.log_error itself now (to hooks.log.1); the rotate_hooks_log finding below is
    mostly a fallback for direct appenders that bypass log_error."""
    home_claude = os.path.join(os.path.expanduser("~"), ".claude")
    out = _check_instructions(None, os.path.expanduser("~"), [os.path.join(home_claude, "CLAUDE.md")],
                              os.path.join(home_claude, "rules"), GLOBAL_MD_MAX)
    logs = os.path.join(c.state_dir(), "logs")
    for name, limit, kind in (("events.jsonl", EVENTS_ROTATE_BYTES, "rotate_events"),
                              (os.path.join("logs", "hooks.log"), HOOKS_LOG_ROTATE_BYTES, "rotate_hooks_log")):
        path = os.path.join(c.state_dir(), name)
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        if size <= limit:
            continue
        out.append(finding(None, kind, "action", f"{name} is {size // 1024} KB", "rotate into state/logs/", auto=True))
        if apply:
            os.makedirs(logs, exist_ok=True)
            stem, ext = os.path.basename(name).rsplit(".", 1)
            os.replace(path, os.path.join(logs, f"{stem}-{time.strftime('%Y%m%d-%H%M%S')}.{ext}"))
            open(path, "a").close()
            rotated = sorted(f for f in os.listdir(logs) if f.startswith(stem + "-"))
            for old in rotated[:-ROTATED_KEEP]:
                os.remove(os.path.join(logs, old))
            actions.append(("global", kind))
    def stale(d, days=SNAPSHOT_MAX_DAYS):
        out = []
        for f in os.listdir(d) if os.path.isdir(d) else []:
            if f == "shadow.jsonl":  # T-0963 review: fm compact-output shadow's measurement, not a cache
                continue
            try:  # T-0839: a heartbeat its watcher removes between listdir and here
                if time.time() - os.path.getmtime(os.path.join(d, f)) > days * 86400:
                    out.append(os.path.join(d, f))
            except OSError:
                pass
        return out
    for kind, what, old, days in (
            ("old_snapshots", "statusline snapshot(s)",  # by-pid/ is pruned by SessionStart (T-0777)
             [f for f in stale(os.path.join(c.state_dir(), "sessions")) if not os.path.isdir(f)], SNAPSHOT_MAX_DAYS),
            ("old_outputs", "sessions' compact-output full texts (T-0811)",
             stale(os.path.join(c.state_dir(), "outputs")), SNAPSHOT_MAX_DAYS),
            ("old_heartbeats", "mail watcher heartbeat(s) of ended sessions (T-0819)",  # a live one beats every 2 s;
             [f for f in stale(os.path.join(c.state_dir(), "bus"))  # seen-<sid> stays: a resumed session's read mark
              if os.path.basename(f).startswith("watch-")], SNAPSHOT_MAX_DAYS),
            ("old_stacks", "long verbs' stack dumps (T-1405)",
             stale(os.path.join(logs, "stacks"), STACKS_MAX_DAYS), STACKS_MAX_DAYS)):
        if old:
            out.append(finding(None, kind, "action", f"{len(old)} {what} older than {days}d",
                               "delete (derived cache)", auto=True))
            if apply:
                for f in old:
                    try:
                        shutil.rmtree(f) if os.path.isdir(f) else os.remove(f)
                    except OSError:  # gone meanwhile
                        pass
                actions.append(("global", kind))
    out += _dead_projects(apply, actions)
    projects = os.path.join(home_claude, "projects")  # T-0856: shells Claude Code made for Foreman's child folders
    shells = [os.path.join(projects, d) for d in (os.listdir(projects) if os.path.isdir(projects) else [])
              if re.match(r"-(run-user-\d+|tmp)-fm-", d)
              and not any(files for _, _, files in os.walk(os.path.join(projects, d)))]
    if shells:
        out.append(finding(None, "child_shells", "action", f"{len(shells)} empty ~/.claude/projects folder(s) left by "
                           "Sampo's own claude -p runs", "delete (no files in them)", auto=True))
        if apply:
            for d in shells:
                shutil.rmtree(d, ignore_errors=True)
            actions.append(("global", "child_shells"))
    if plugins:
        out += _check_plugins()
    return out


def _dead_projects(apply, actions):
    """T-2091: a project whose root folder is gone. Under a temp dir it was a throwaway trial: --apply moves its state
    to state/archive/projects/ (nothing deleted). Anywhere else the folder may only be unplugged or moved: reported."""
    import tempfile
    temps = {os.path.realpath(d) for d in ("/tmp", "/var/tmp", tempfile.gettempdir())}
    out = []
    for p, _ in c.all_projects():
        if os.path.isdir(p.root):
            continue
        root = os.path.realpath(p.root)
        temp = any(os.path.commonpath([root, t]) == t for t in temps)
        out.append(finding(None, "dead_project", "action" if temp else "warn", f"{p.slug} {c.plain(p.root)} is gone",
                           "move its state to state/archive/projects/" if temp else
                           "folder moved or unplugged? fm tidy leaves it", auto=temp))
        if apply and temp:
            dest = os.path.join(c.state_dir(), "archive", "projects", p.slug)
            if os.path.exists(dest):
                dest += time.strftime("-%Y%m%d-%H%M%S")
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            try:
                shutil.move(os.path.join(c.projects_dir(), p.slug), dest)
            except OSError:  # another tidy moved it first
                continue
            actions.append(("global", f"dead_project {p.slug}"))
    return out


def _check_plugins():
    import subprocess
    out = []
    try:
        listing = json.loads(subprocess.run(["claude", "plugin", "list", "--json"], capture_output=True, text=True,
                                            timeout=60).stdout or "[]")
    except (OSError, ValueError, subprocess.SubprocessError):
        return [finding(None, "plugins", "info", "could not run `claude plugin list --json`")]
    for pl in listing:
        pid = pl.get("id", "")
        name = pid.split("@")[0]
        if not pl.get("enabled", True):
            continue
        if name not in KNOWN_PLUGINS:
            out.append(finding(None, "plugin_unclassified", "warn", f"{pid} isn't classified in BUILD_PROMPT §4.6",
                               "map it to a lifecycle stage, scope it to projects, or disable it"))
        if name in LSP_BINARIES and not shutil.which(LSP_BINARIES[name]):
            out.append(finding(None, "plugin_lsp_missing", "warn", f"{pid} is enabled but `{LSP_BINARIES[name]}` isn't on PATH",
                               f"install the server or: claude plugin disable {pid}"))
        try:
            details = subprocess.run(["claude", "plugin", "details", pid], capture_output=True, text=True, timeout=60).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        m = re.search(r"Always-on:\s+~?([\d,.]+k?) tok", details)
        if m:
            out.append(finding(None, "plugin_footprint", "info", f"{pid}: ~{m.group(1)} always-on tokens"))
    return out


# ---------------------------------------------------------------- command

def cmd_tidy(args):
    from sampo import cli
    projects = [p for p, _ in c.all_projects()] if args.all else [cli.resolve(args)]
    actions, findings = [], []
    lanes = getattr(args, "lanes_only", False)  # T-1071: what fm night runs: nothing but closed tasks' lanes
    for p in projects:
        if lanes:
            from sampo import lanes as slanes
            findings += slanes.stale(p, c.load_briefs(p), args.apply, actions, finding)
        else:
            findings += check_project(p, args.apply, actions)
    if not lanes:
        findings += check_global(args.apply, actions, plugins=getattr(args, "plugins", False))
    report = {"applied": bool(args.apply), "projects": [p.slug for p in projects], "findings": findings,
              "actions": [f"{k}:{v}" for k, v in actions]}
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return
    if not findings:
        print("Tidy: nothing to do.")
        return
    order = {"action": 0, "warn": 1, "info": 2}
    for f in sorted(findings, key=lambda f: (order[f["severity"]], f["project"] or "", f["kind"])):
        mark = {"action": "DONE" if args.apply else "APPLY", "warn": "DECIDE", "info": "info"}[f["severity"]]
        print(f"{mark:6} [{f['project'] or 'global'}] {f['kind']}: {f['detail']}" + (f"  → {f['fix']}" if f["fix"] else ""))
    if not args.apply and any(f["auto"] for f in findings):
        print("Dry run. `fm tidy --apply` performs the APPLY items (archives first, never deletes user requests).")
