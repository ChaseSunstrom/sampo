"""fm scout (T-0950): Sampo finds its own work, so nobody has to ask. Zero-token probes look for a newer Claude Code to
adopt, dependencies in the user's projects that no stack card covers, doctor failures, skill sources gone stale, and an
empty queue that wants a brainstorm round. Each finding is captured once, under a key of its own, into the inbox; the
drive then plans and works it under full autonomy. Bounded: MAX_CAPTURES a run, MAX_OPEN open at once, one quick run
per GAP_S. The idle Stop runs the quick probes (cached data only) and starts the slow ones (network, doctor, project
scans) detached; fm night runs them all first."""
import collections
import hashlib
import json
import os
import re
import subprocess
import sys
import time

from sampo import core as c

CC_TESTED = "2.1.304"  # the Claude Code version Foreman was last adapted to (README says the same)
MAX_CAPTURES = 3
MAX_OPEN = 5
GAP_S = 1800
CACHE_S = 12 * 3600
SOURCES_STALE_S = 7 * 86400
_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
SLOW_GAP_S = 6 * 3600  # the drain asks for the slow probes at most this often

FIND_DAY = 3  # T-2124: fm find leads filed per project per day, across runs
PROVEN = {"dep", "docs", "secret"}  # kinds whose finding is its own proof; the rest wait for a failing test (T-2122)

# confirm: why it waits on the user's own yes; home: file it in Foreman's own inbox; meta: fields set on the task
Finding = collections.namedtuple("Finding", "key title type tier why confirm home meta", defaults=("", False, None))


def _ver(v):
    return tuple(int(x) for x in v.split(".")) if isinstance(v, str) and _VERSION.match(v) else None


def _cache_path():
    return os.path.join(c.state_dir(), "scout.json")


def _cache():
    try:
        with open(_cache_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_cache(**kw):
    data = dict(_cache(), **kw)
    c.write_atomic(_cache_path(), json.dumps(data, indent=1))


# ---------------------------------------------------------------- probes: (p, ctx) -> [Finding]

def probe_claude_code(p, ctx):
    latest, installed = _ver(ctx.get("latest")), _ver(ctx.get("installed"))
    if not latest:
        return []
    have = max(x for x in (installed, _ver(CC_TESTED)) if x)
    if latest <= have:
        return []
    v = ctx["latest"]
    return [Finding(f"cc:{v}", f"Adopt Claude Code {v}: install it and take its new features and fixes into Sampo",
                    "FEATURE", "M", f"published {v}; installed {ctx.get('installed') or 'unknown'}; Sampo tested on "
                    f"{CC_TESTED}. Read its changelog since {CC_TESTED}, adopt what helps, re-verify the hooks and the "
                    f"pane, then raise CC_TESTED and the README")]


def probe_lib_gaps(p, ctx):
    """Dependencies used in the user's projects that no card's dep: signal covers and no earlier finding named, most
    used first. Cards live in Sampo's own repo, so without named roots it runs there only."""
    from sampo import lib
    if not ctx.get("roots") and os.path.realpath(p.root or "") != os.path.realpath(os.path.dirname(c.PLUGIN_ROOT)):
        return []
    roots = ctx.get("roots") or [x.root for x, _ in c.all_projects() if x.root and os.path.isdir(x.root)]
    asked = {d for k in c.read_meta(p).get("scout_keys") or {} if k.startswith("libgap:") for d in k[7:].split(",")}
    seen = lib.uncovered_deps(roots, asked)
    top = [d for d, _ in sorted(seen.items(), key=lambda kv: (-kv[1], kv[0]))[:8]]
    if not top:
        return []
    return [Finding("libgap:" + ",".join(sorted(top)), f"Write stack cards for {c.joinmore(top, 5)}", "FEATURE", "S",
                    f"dependencies in your projects with no stack card: {', '.join(top)}. Write one card each to the "
                    f"spec in plugin/skills/library/SKILL.md (a writer subagent per batch), then fm lib check")]


def probe_brainstorm(p, ctx):
    """An empty queue and inbox with no brainstorm in 7 days: a round of fm ideas, grounded and queued."""
    if any(b.status not in c.CLOSED for b in c.load_briefs(p)):
        return []
    folder = os.path.join(p.dir, "research")
    stamps = [m.group(1) for n in (os.listdir(folder) if os.path.isdir(folder) else [])
              for m in [re.match(r"brainstorm-(\d{8})", n)] if m]
    recent = time.strftime("%Y%m%d", time.gmtime(time.time() - 7 * 86400))
    if any(s >= recent for s in stamps):
        return []
    week = time.strftime("%G-W%V", time.gmtime())
    return [Finding(f"brainstorm:{week}", "Brainstorm round: what Sampo should build or fix next", "RESEARCH", "M",
                    "the queue and inbox are empty and no brainstorm ran this week: /sampo:brainstorm, a cheap "
                    "probe first (two lenses), ground every idea, queue the ones worth building")]


def _auto_start(p, m):
    """T-2178: gen-0 from the user's standing goal text, no model (the template's fallback capabilities: a probe runs
    from hooks), sealed locally, one decision row, the key spent. None when the offer stands (words not the user's,
    lines from other briefs, init refuses a secret-shaped line); [] when a second session's gen-0 got there first;
    else the note that names the tower."""
    from sampo import cli
    from sampo import rune
    if not _user_backed(p, m["text"]):
        return None
    d = rune.draft(p, m["text"], model=False)
    if d["unconfirmed"]:  # MUST/NEVER lines quoted from briefs the user didn't write: the user reads them first
        return None
    try:
        path = rune.init(p, d["text"].encode("utf-8"))
    except ValueError:
        return [] if os.path.isfile(os.path.join(rune.home(p), "gen-0.md")) else None  # raced: theirs stands
    try:
        c.log_event(p, "genesis_init", data={"path": path, "sha256": rune.seal(p)}, session=cli.session())
    finally:  # the rows land even if the ledger write fails
        try:
            cli._write_decision(p, "Genesis started itself from the standing mandate", "gen-0 #13: it starts by itself "
                                  "when the queue runs dry; full autonomy, 10+ done tasks and the user's own goal text "
                                  "(fm mandate, backed by their words) hold what gen-0 needs", "keep offering (T-2105): "
                                  "waits on a yes that never comes; inventing a goal under full autonomy", kind="costly")
        finally:
            c.update_meta(p, scout_keys=dict(c.read_meta(p).get("scout_keys") or {}, **{"genesis:start": "auto"}))
    rune.kick(p)
    return [Finding("genesis:started", "Genesis started from your goal: drive it with fm genesis step", "FEATURE", "M",
                    "the queue ran dry under full autonomy, so gen-0 was drafted from the standing goal, sealed in the "
                    "tower folder and logged as a decision. Run fm genesis step and follow /sampo:genesis")]


def _user_backed(p, text):
    """T-2178: fm mandate set is unguarded, so a goal only counts as the user's words when 80% of its significant words
    (4+ letters) sit in one user-sourced brief's raw request or one user-steer decision row."""
    from sampo import rune
    want = set(rune.WORD.findall(text.lower()))
    srcs = [b.section("Raw request") or "" for b in c.load_briefs(p, include_archive=True)
            if (b.meta.get("source") or "user") == "user"] + [t for _, t in c.steer_rules(p)]
    return bool(want) and any(len(want & set(rune.WORD.findall(s.lower()))) >= 0.8 * len(want) for s in srcs)


def probe_genesis(p, ctx):
    """T-2105: a drained queue in a project with a track record (10+ done tasks) and no Genesis tower: offer one, once
    ever (the key never repeats). It waits on the user's own yes (meta confirm). Blocked and deferred work doesn't hold
    it back: that waits on the user too, and so does the offer. T-2178: under full autonomy with an unexpired standing
    goal (the user's own text) in a non-sensitive project it starts itself instead (_auto_start), once."""
    from sampo import rune
    from sampo import mandate
    if "genesis:start" in (c.read_meta(p).get("scout_keys") or {}):  # spent: skip loading the archive
        return []
    briefs = c.load_briefs(p, include_archive=True)
    if any(b.status in c.RUNNABLE or b.status == "captured" for b in briefs) or \
            os.path.isfile(os.path.join(rune.home(p), "gen-0.md")) or sum(b.status == "done" for b in briefs) < 10:
        return []
    m = mandate.get(p)
    if c.effective_autonomy(p) == "full" and m and not m["expired"] and not c.read_meta(p).get("sensitive"):
        if ctx.get("dry"):  # a dry run changes nothing
            return [Finding("genesis:start", "Genesis would start itself from the standing goal", "FEATURE", "L",
                            "dry run: full autonomy, a drained queue and a standing goal hold the user's own words")]
        started = _auto_start(p, m)
        if started is not None:
            return started
    return [Finding("genesis:start", "Start Genesis here? Generations toward one goal you write, to a final report",
                    "FEATURE", "L", "the queue is drained and this project has a track record: Genesis runs generations "
                    "toward one goal the user writes (gen-0). Ask for their goal and MUST/NEVER lines; with only a line, "
                    "fm genesis draft \"<line>\" prints a whole gen-0 to show them; fm genesis init saves it, then "
                    "/sampo:genesis drives it. A no is a drop: it is never offered again",
                    confirm="Genesis is a long run toward a goal only you can write")]


def probe_friction(p, ctx):
    """No self-improvement pass scheduled (fm friction --every), none in 7 days, and at least 3 tasks closed since:
    one pass this week, so Sampo fixes its own friction without being asked."""
    meta = c.read_meta(p)
    if int(meta.get("rsi_every") or 0) > 0 or (meta.get("rsi_at") and (c.age_days(meta["rsi_at"]) or 0) < 7):
        return []
    start = meta.get("rsi_at") or ""
    done = sum(1 for e in c.tail_jsonl(os.path.join(p.dir, "ledger.jsonl"), 6000)
               if e.get("event") == "task_done" and (e.get("ts") or "") >= start)
    if done < 3:
        return []
    from sampo import friction
    return [Finding("friction:" + time.strftime("%G-W%V", time.gmtime()), "Self-improvement pass: Sampo's own "
                    "friction since the last one", "RESEARCH", "S", f"{done} tasks closed and no pass in a week: "
                    + friction.ACTION)]


def _is_home(p):
    return os.path.realpath(p.root or "") == os.path.realpath(c.foreman_home())


def probe_find(p, ctx):
    """T-2124: fm find on the user's own project (Sampo's home runs it by hand): proven kinds only, FIND_DAY a day,
    never a key in fmfind.settled (dropped, a reverted fix, still open). Refused, with the reason, in a tree whose last
    50 commits have none by git's user.email: a clone isn't the user's to file work in. An email is only a claim, so
    where others commit too each lead waits for your yes. At most FIND_DAY open and FIND_DAY in any 24 hours."""
    from sampo import find
    root = os.path.realpath(p.root or "")
    if not p.root or _is_home(p):
        return []
    git = lambda *a: c._git(root, "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", *a, timeout=30)
    me, authors = git("config", "user.email").strip().lower(), set(git("log", "-50", "--format=%ae").lower().split())
    if not authors:  # no commits, or no git: nothing here is provably yours, and nothing to say about it
        return []
    if not me or me not in authors:
        raise PermissionError(f"not scanned: no recent commit here is by {c.plain(me) or 'you (no git user.email)'}; "
                              f"authors: {c.fit(c.plain(', '.join(sorted(authors))), 120) or 'none'}")
    others = sorted(authors - {me})
    confirm = (f"others commit here too ({c.fit(c.plain(', '.join(others)), 120)}): file it only if this tree is yours"
               if others else "")
    keys = c.read_meta(p).get("scout_keys") or {}
    mine = {i for k, i in keys.items() if k.startswith("find:")}
    ours = [b for b in c.load_briefs(p, include_archive=True) if b.id in mine]
    ages = [c.age_days(b.meta.get("created")) for b in ours]
    left = FIND_DAY - max(sum(a is not None and a < 1 for a in ages), sum(b.status not in c.CLOSED for b in ours))
    if left <= 0:
        return []
    done, out = find.settled(p), []
    for key, f in find.keyed(find.scan(root, find.precision(p))[1], root, root):
        if len(out) >= left:
            break
        if f.get("held") or f["kind"] not in PROVEN or key in done or "find:" + key in keys:
            continue
        what = c.plain(f"{f['file']}:{f['line']} {f['symbol'] or '-'}: {f['says']}")
        out.append(Finding("find:" + key, c.fit(f"{f['kind']}: {what}", 120), "SECURITY" if f["kind"] == "secret"
                           else "FIX", "S", f"fm find ({f['finder']}), quoted as data:\n> {what}", confirm=confirm,
                           meta={"finder": f["finder"], "find_key": key}))
    return out


def probe_doctor(p, ctx):
    try:
        r = subprocess.run([sys.executable, os.path.join(c.PLUGIN_ROOT, "bin", "fm"), "doctor", "--json"], cwd=p.root,
                           capture_output=True, text=True, timeout=120, env=dict(os.environ, FOREMAN_NO_BACKGROUND="1"))
    except subprocess.TimeoutExpired:
        raise OSError("fm doctor --json took over 120 s")
    rows = json.loads(r.stdout)["results"]  # a broken answer is a probe error (logged), never "nothing found"
    out = []
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and row.get("status") == "FAIL":
            detail = c.plain(str(row.get("detail", "")))
            key = "doctor:" + str(row.get("name")) + ":" + hashlib.sha1(detail.encode()).hexdigest()[:8]
            out.append(Finding(key, f"Fix the doctor's {row.get('name')} check", "FIX", "S", c.fit(detail, 300),
                               home=not _is_home(p)))  # T-2124: Foreman's fault, so Foreman's inbox, not the user's
    return out


def _remote_head(url):
    """T-1391: the sha a source's remote HEAD points at now (git ls-remote, no fetch), or None when it can't tell."""
    from sampo import lib
    try:
        out = lib._git_src(None, "ls-remote", "--", url, "HEAD", timeout=20)
    except Exception:  # offline, or a remote that won't say: refresh as before
        return None
    m = re.match(r"([0-9a-f]{40})\s", out or "")
    return m.group(1) if m else None


def index_delta(old, new):
    """T-1391: (added, removed, renamed) item names between two indexes of one source; a rename keeps its path."""
    o = {x.get("path"): str(x["name"]) for x in old or [] if isinstance(x, dict) and x.get("name")}
    n = {x.get("path"): str(x["name"]) for x in new or [] if isinstance(x, dict) and x.get("name")}
    return (sorted(n[k] for k in n.keys() - o.keys()), sorted(o[k] for k in o.keys() - n.keys()),
            sorted(f"{o[k]} → {n[k]}" for k in o.keys() & n.keys() if o[k] != n[k]))


def probe_sources(p, ctx):
    """Skill sources older than a week are refreshed (a read-only data index: no code run). T-1391: a source whose
    remote HEAD is the pinned commit isn't fetched; one that moved and changed its items is one finding listing what
    was added, removed and renamed, with its licence class (nothing installs)."""
    import calendar
    from sampo import lib
    if ctx.get("dry"):
        return []
    out = []
    for meta, index in lib.sources():
        added = meta.get("added") or ""
        try:
            age = time.time() - calendar.timegm(time.strptime(added, "%Y-%m-%dT%H:%M:%SZ"))
        except ValueError:
            age = SOURCES_STALE_S
        if age >= SOURCES_STALE_S:
            if meta.get("commit") and _remote_head(meta["url"]) == meta["commit"]:
                continue  # T-1391: upstream hasn't moved: no fetch
            try:
                new = lib.source_add(meta["url"], meta["name"], replace=True)
            except Exception:  # offline, or the repo moved: next time
                continue
            if isinstance(new, tuple) and meta.get("commit") and new[0].get("commit") != meta.get("commit"):
                plus, minus, renamed = index_delta(index, new[1])
                if plus or minus or renamed:
                    name, sha = c.plain(str(meta["name"])), str(new[0].get("commit", ""))[:8]
                    lines = [f"{label}: {', '.join(c.plain(x) for x in xs[:10])}{' …' if len(xs) > 10 else ''}"
                             for label, xs in (("added", plus), ("removed", minus), ("renamed", renamed)) if xs]
                    out.append(Finding(f"upstream:{name}:{sha}", f"Source {name} moved to {sha}: +{len(plus)} "
                                       f"−{len(minus)} ~{len(renamed)}", "RESEARCH", "S",
                                       f"fm lib source {name} ({new[0].get('class') or 'ideas-only'}) moved from "
                                       f"{str(meta['commit'])[:8]}. {'; '.join(lines)}. Read what changed (fm lib "
                                       "show NAME) and decide what Sampo takes; nothing was installed."))
            moved = lib.class_change(meta, new[0]) if isinstance(new, tuple) else ""
            if moved:  # T-1389: a licence class that moved is one finding, never silent
                out.append(Finding(f"licence:{new[0].get('name')}:{new[0].get('class')}:"
                                   f"{str(new[0].get('commit', ''))[:8]}", f"Source {moved}",
                                   "RESEARCH", "S", f"fm lib source refreshed {c.plain(str(meta['name']))}; its licence "
                                   "class moved. Read the new licence and decide whether the source stays a default."))
    return out


_OWN = None


def _foreman_text():
    """Sampo's own words, once a run: README, MASTER, CHANGELOG, the rules, the skills and plugin/lib."""
    global _OWN
    if _OWN is None:
        import glob
        top = os.path.dirname(c.PLUGIN_ROOT)
        parts = []
        for pat in ("README.md", "MASTER.md", "docs/map/*.md", "CHANGELOG.md", "plugin/rules/*.md", "plugin/skills/*/SKILL.md",
                    "plugin/lib/*.py", "plugin/lib/sampo/*.py", "plugin/hooks/*"):
            for f in sorted(glob.glob(os.path.join(top, pat))):
                try:
                    with open(f, encoding="utf-8", errors="replace") as fh:
                        parts.append(fh.read(4 << 20))
                except OSError:
                    continue
        _OWN = "\n".join(parts)
    return _OWN


def probe_cc_changelog(p, ctx):
    """T-1391: each bullet of Claude Code's changelog past CC_TESTED that names something in backticks Sampo's own
    text never mentions: a finding to decide whether Sampo should use or adapt to it. The changelog is the indexed
    anthropic-claude-code source's checkout (data, never run); FOREMAN_SCOUT_CHANGELOG names another file (tests)."""
    from sampo import lib
    path = os.environ.get("FOREMAN_SCOUT_CHANGELOG") or os.path.join(lib._sources_dir(), "anthropic-claude-code",
                                                                    "repo", "CHANGELOG.md")
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read(2 << 20)
    except OSError:
        return []
    tested, ver, out, own = _ver(CC_TESTED), None, [], None
    for line in text.splitlines():
        h = re.match(r"^#{1,3}\s*\[?v?(\d+\.\d+\.\d+)", line)
        if h:
            ver = h.group(1)
            continue
        b = re.match(r"^\s*[-*]\s+(.+)", line)
        if not (b and ver and _ver(ver) and _ver(ver) > tested):
            continue
        names = [n for n in re.findall(r"`([^`\n]{2,60})`", b.group(1)) if n.strip()]
        own = own if own is not None else _foreman_text()
        if names and not any(n in own for n in names):
            text_ = c.plain(b.group(1))
            out.append(Finding(f"ccfeat:{ver}:{hashlib.sha1(text_.encode()).hexdigest()[:10]}",
                               f"Claude Code {ver}: {c.fit(text_, 100)}", "RESEARCH", "S",
                               f"Claude Code's changelog for {ver} names {c.joinmore([c.plain(n) for n in names], 3)}, "
                               "which nothing in Sampo mentions. Decide whether Sampo should use or adapt to it."))
    return out


QUICK = [("claude-code", probe_claude_code), ("friction", probe_friction), ("brainstorm", probe_brainstorm),
         ("genesis", probe_genesis)]
SLOW = [("claude-code", probe_claude_code), ("cc changelog", probe_cc_changelog), ("library gaps", probe_lib_gaps),
        ("doctor", probe_doctor), ("sources", probe_sources), ("friction", probe_friction),
        ("brainstorm", probe_brainstorm), ("find", probe_find)]


# ---------------------------------------------------------------- running and capturing

def _installed():
    try:
        r = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=20)
        m = re.search(r"\d+\.\d+\.\d+", r.stdout or "")
        return m.group(0) if m else None
    except (OSError, subprocess.SubprocessError):
        return None


def _latest():
    import urllib.request
    try:
        with urllib.request.urlopen("https://downloads.claude.ai/claude-code-releases/latest", timeout=5) as r:
            v = r.read(64).decode("ascii", "replace").strip()
    except Exception:  # offline, a timeout, a block: unknown, never "no update"
        return None
    return v if _VERSION.match(v) else None


def context(slow=False, dry=False):
    """What the probes read: the published and installed Claude Code versions, from the environment (tests), else a
    fresh fetch (slow runs, successes cached unless dry) or the cache (quick runs: no network in a hook)."""
    cache = _cache()
    latest, installed = os.environ.get("FOREMAN_SCOUT_LATEST"), os.environ.get("FOREMAN_SCOUT_INSTALLED")
    if slow and not latest and time.time() - (cache.get("cc_latest_at") or 0) >= CACHE_S:
        got = _latest()
        if got and dry:
            cache = dict(cache, cc_latest=got)
        elif got:
            _save_cache(cc_latest=got, cc_latest_at=time.time())
            cache = _cache()
    if slow and not installed:
        got = _installed()
        if got and dry:
            cache = dict(cache, cc_installed=got)
        elif got:
            _save_cache(cc_installed=got)
            cache = _cache()
    return {"latest": latest or cache.get("cc_latest"), "installed": installed or cache.get("cc_installed"), "dry": dry}


def run_probes(p, probes, ctx):
    found, errors = [], []
    for name, fn in probes:
        try:
            found += fn(p, ctx) or []
        except Exception as e:  # one failing probe never stops the others
            errors.append(f"{name}: {type(e).__name__}: {c.fit(str(e), 120)}")
    return found, errors


def capture(p, findings, dry=False):
    """New inbox items for the findings not captured before under their key; at most MAX_CAPTURES now and MAX_OPEN
    open scout items in all. Returns the new ids (none when dry)."""
    from sampo import cli
    made = []
    with c.lock(p.dir):
        meta = c.read_meta(p)
        keys = dict(meta.get("scout_keys") or {})
        open_n = sum(b.meta.get("source") == "scout" and b.status not in c.CLOSED for b in c.load_briefs(p))
        for f in findings:
            if f.key in keys or len(made) >= MAX_CAPTURES or open_n + len(made) >= MAX_OPEN:
                continue
            if dry:
                made.append(f.key)
                continue
            b = cli._create(p, c.fit(c.defang(f.title), 120), f.type, f.tier, "captured",  # T-2235: repo text
                              raw=f"Found by fm scout: {c.defang(f.why)}", source="scout")
            if f.confirm:
                b.meta.update(confirm=True, confirm_why=f.confirm)
            if f.confirm or f.meta:
                b.meta.update(f.meta or {})
                c.save_brief(p, b, touch=False)
            c.log_event(p, "capture", task=b.id, data={"source": "scout", "key": f.key})
            keys[f.key] = b.id
            made.append(b.id)
        if made and not dry:
            meta = c.read_meta(p)
            meta["scout_keys"] = keys
            c.write_meta(p, meta)
            c.regen_views(p)
    return [] if dry else made


def due(p):
    return time.time() - float(c.read_meta(p).get("scout_at") or 0) >= GAP_S


def run(p, slow=False, dry=False):
    """One scout run; a real one is ledgered with what it captured and each probe that failed.
    ponytail: probes run in-process with no deadline of their own; fm night's job timeout bounds a hung slow run."""
    from sampo import mandate
    m = mandate.get(p)
    if m and m["expired"]:  # T-1400: an ended mandate leaves one decision and nothing more is captured under it
        found, errors = [mandate.lapse_finding(m)], []
    else:
        found, errors = run_probes(p, SLOW if slow else QUICK, context(slow, dry))
        if m:  # T-1400: what's found while it stands says so
            found = [f._replace(why=f"{f.why} (found under the standing mandate: {c.fit(m['text'], 80)})")
                     for f in found]
    away = [f for f in found if f.home and not dry]
    made = capture(p, [f for f in found if f not in away], dry=dry)
    if away:  # T-2124: one inbox for Foreman's own faults, deduped across every project that saw them
        home = c.foreman_home()
        try:
            made += capture(c.find_project(home, create=True) or c.init_project(home), away)
        except OSError as e:
            errors.append(f"home inbox: {type(e).__name__}: {c.fit(str(e), 120)}")
    if not dry:
        c.update_meta(p, scout_at=time.time(), **({"scout_slow_at": time.time()} if slow else {}))
        c.log_event(p, "scout", data={"slow": slow, "captured": made, "errors": errors})
    return made, found, errors


def idle(p):
    """The drained queue's Stop: the quick probes now (cached data, no network). (new ids, whether to ask Claude to
    run the slow probes): asked when the quick ones found nothing and none ran in SLOW_GAP_S, so what they capture
    reaches this session's drive instead of waiting in the inbox for the next one."""
    made, _, _ = run(p)
    slow_due = time.time() - float(c.read_meta(p).get("scout_slow_at") or 0) >= SLOW_GAP_S
    return made, not made and slow_due and not os.environ.get("FOREMAN_SCOUT_QUICK_ONLY")


def cmd_scout(args):
    from sampo import cli
    p = cli.resolve(args)
    made, found, errors = run(p, slow=args.slow or args.dry, dry=args.dry)
    lines = [f"  {f.key:<34} {c.fit(f.title, 90)}" for f in found]
    head = ("fm scout (dry run): what it would capture" if args.dry else
            f"fm scout: captured {', '.join(made)}" if made else "fm scout: nothing new to capture")
    cli.out(args, {"captured": made, "found": [f._asdict() for f in found], "errors": errors},
              "\n".join([head] + lines + [f"  probe failed: {e}" for e in errors]))
