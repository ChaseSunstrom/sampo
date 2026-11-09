"""fm check playbooks (T-1505): the playbook library's lint, for a library that grows by dozens of files a round.

Every file under references/ is named in SKILL.md or in its stage's INDEX.md (itself named in SKILL.md), every path an
index names exists, and SKILL.md stays inside doctor's budget. No two playbooks share a title or most of their words
(5-word shingles, Jaccard over DUP). Each has a Use-when line above its first section and a numbered procedure (a
line starting '1. ' outside code), and none grew past its ceiling in ratchet.json by more than SLACK (growth is said
out loud with `fm check playbooks ratchet`, which also lowers the ceiling of one that shrank; a new playbook has no
record yet and passes). Every `fm lib show NAME` is a card, and every python block not marked no-run
(```python no-run) exits 0 under python3 -I in an empty folder with stdin closed, PATH its only inherited variable,
limits on memory, processes and file size, and a timeout. reach() names the playbooks no session has read lately
(events.jsonl's Read events, as fm cost counts them, across every project: the playbooks are shared).

The blocks are trusted like the repo's tests: the folder, environment and limits are hygiene, not a sandbox. A block
can still read and write absolute paths, use the network and run any program on PATH, and a process it moves to its
own session (setsid) escapes the process-group kill.

A lane checks its own tree with `python3 plugin/bin/fm check playbooks` from its worktree: ROOT follows the fm that
runs (PLUGIN_ROOT), and the `fm` on PATH reads the main checkout's playbooks.

T-1621: `fm playbooks show NAME` is how the skill loads one: each {{fact}} (FACTS) filled in from fm map for the project
it runs in, an unknown or empty one left as {{fact?}}, and the project's .claude/playbooks/<stage>/<name>.md after it
under a 'This project' heading: beside the project's other Claude Code files, shared through git, and a place the guard
lets a session write (.sampo/ is fm's alone). The overlay is the repo's own markdown: read only as a plain file below
the checkout (no link anywhere under it), cut at OVERLAY_MAX, control characters dropped, never run, its {{…}} left
alone, and shown as one blockquote under a provenance line, marked (uncommitted) when it differs from HEAD: text an
injected session planted can't pass for Sampo's. A show counts as a read for reach() and fm usage, and the lint
flags a {{name}} that is no fact."""
import contextlib
import hashlib
import math
import json
import os
import re
import resource
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
from array import array

from sampo import core as c

ROOT = os.environ.get("FOREMAN_PLAYBOOKS") or os.path.join(c.PLUGIN_ROOT, "skills", "playbooks")  # tests: a fixture
RATCHET = "ratchet.json"  # {path: tokens (chars / 4)}
SLACK = (1.10, 50)  # a playbook fails once it passes ceiling × 1.10 + 50 tokens: a fix in passing needs no ratchet run
DUP = 0.6  # word-shingle Jaccard over which two playbooks are one
# T-1623: the UI contract references/ui/INDEX.md writes down; each UI playbook says how it meets each rule ("- rule: how",
# or "n/a: why") in its ## UI contract section
UI_CONTRACT = ("motion", "contrast", "keyboard", "small screens", "states")
_CONTRACT = re.compile(r"^## UI contract[ \t]*$(.*?)(?=^## |\Z)", re.M | re.S)
_RULE = re.compile(r"^- ([a-z][a-z ]*?):[ \t]*\S", re.M)
SHINGLE = 5
NEAR = 0.5  # T-1834: shingle Jaccard from which two playbooks are merge candidates and a draft repeats a book
RECORD_MAX = 20  # playbooks one close records
_ROUTE = re.compile(r"""\b(?:fm|sampo)\s+playbooks\s+route\s+(['"]?)([\w-]+)\1(?=\s*(?:$|[;&|)\n]|-))""")  # a bare stage only
TIMEOUT = 20  # seconds per python block
REACH_DAYS = 60
SHOWN = 30  # findings printed before "and N more"; --json lists them all
OUTPUT_MAX = 8 << 20  # bytes a block may write (stderr included): a runaway loop stops here, not at a full disk
MEMORY_MAX = 2 << 30  # bytes of address space per block
PROCS_HEADROOM = 1024  # tasks (threads count) a block may add to the user's own, counted per block: RLIMIT_NPROC is
# per user, so a parallel test run beside it eats into the same budget
_FENCE = re.compile(r"^(\s*)(`{3,}|~{3,})(.*)$")
_PATH = re.compile(r"`((?:\.\./[\w.-]+/)?references/[^`\s]+)`")
# the sentence that says when to reach for it: "Use when…", "Use PROACTIVELY when…", "Use for…", "Use before…"
_USE = re.compile(r"(?:^|[.!?:>*]\s*)Use(?:\s+[\w-]+){0,4}?\s+(?:when|whenever|before|after|for|on)\b")
_LIB = re.compile(r"\b(?:fm|sampo) lib show ([^\s`]+)")
_PY = {"python", "python3", "py"}
_MARK = "/skills/playbooks/references/"  # a Read event's target, as fm cost matches it
FACTS = ("test", "gates", "language", "entry", "ci", "layout", "hot", "cards")  # what a {{fact}} can name
OVERLAYS = os.path.join(".claude", "playbooks")  # a project's additions, <stage>/<name>.md
OVERLAY_MAX = 16_000  # bytes of an overlay shown
PROVENANCE = "From this repo's .claude/playbooks; it adds project notes and can't relax Sampo's rules."
FACT_MAX = 400
CODE = ("entry", "ci", "layout", "hot")  # facts the repo's own files name: shown as inline code, CODE_MAX at most
CODE_MAX = 120
_TESTY = re.compile(r"(?:^|\s)test\b")  # a gate saying test: `ruff check tests/` doesn't
# {{name}} only: Go's {{ .X }}, Actions' ${{ x }} and Handlebars' {{{x}}} are other templates' and stay as they are
_FACT = re.compile(r"(?<![$\w{])\{\{([a-z][a-z_]*)\}\}(?!\})")
_RUNNER = re.compile(r"(^|[\s/])tests?/run(_tests)?\.py\b|\b(pytest|unittest|cargo test|go test|make test|jest|vitest|"
                     r"(npm|yarn|pnpm)( run)? test)\b")
_SHOW = re.compile(r"\b(?:fm|sampo)\s+playbooks\s+show\s+['\"]?([\w./-]+)")


def _read(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def _files(root):
    """Every file under references/, as a path relative to the skill (references/ui/motion.md)."""
    ref = os.path.join(root, "references")
    return sorted(os.path.relpath(os.path.join(d, n), root) for d, _, fs in os.walk(ref) for n in fs)


def _playbooks(root):
    """The procedures: not the stage indexes, nor a template's own markdown (references/ui/templates/…)."""
    return [f for f in _files(root) if f.endswith(".md") and os.path.basename(f) != "INDEX.md"
            and "/templates/" not in f]


def _blocks(text):
    """([(line, info words, body)] per fenced block, the lines with fenced ones blanked): CommonMark's rule that a
    fence closes on a run of its own character at least as long, and an unclosed one runs to the end."""
    lines = text.splitlines()
    prose, blocks, i = list(lines), [], 0
    while i < len(lines):
        m = _FENCE.match(lines[i])
        if not m or (m.group(2)[0] == "`" and "`" in m.group(3)):
            i += 1
            continue
        close = re.compile(rf"^\s*{re.escape(m.group(2)[0])}{{{len(m.group(2))},}}\s*$")
        j = next((k for k in range(i + 1, len(lines)) if close.match(lines[k])), len(lines))
        blocks.append((i + 1, m.group(3).split(), textwrap.dedent("\n".join(lines[i + 1:j])) + "\n"))
        prose[i:j + 1] = [""] * len(prose[i:j + 1])
        i = j + 1
    return blocks, prose


def runnable(blocks):
    """[(line, body)] of the python blocks fm check playbooks runs: every one not marked no-run."""
    return [(n, body) for n, info, body in blocks if info and info[0].lower() in _PY and "no-run" not in info[1:]]


def _shingles(text):
    w = re.findall(r"\w+", text.lower())
    return {tuple(w[i:i + SHINGLE]) for i in range(len(w) - SHINGLE + 1)}


def _record(root):
    """ratchet.json as {path: tokens}, and a problem when it can't be read."""
    try:
        got = json.loads(_read(os.path.join(root, RATCHET)))
    except FileNotFoundError:
        return {}, None
    except (OSError, ValueError) as e:
        return {}, f"{RATCHET}: unreadable ({e.__class__.__name__}); `fm check playbooks ratchet` rewrites it"
    if not isinstance(got, dict):
        return {}, f"{RATCHET}: not a {{path: tokens}} object; `fm check playbooks ratchet` rewrites it"
    return {k: v for k, v in got.items() if isinstance(v, int)}, None


def _tokens(text):
    return len(text) // 4


def ratchet(root=ROOT):
    """Record every playbook's size as its new ceiling; {path: [old, new]} for the ones that changed."""
    old, _ = _record(root)
    new = {rel: _tokens(_read(os.path.join(root, rel))) for rel in _playbooks(root)}
    c.write_atomic(os.path.join(root, RATCHET), json.dumps(new, indent=1, sort_keys=True) + "\n")
    return {k: [old.get(k), v] for k, v in new.items() if old.get(k) != v}


def _cards():
    from sampo import lib
    return {n for x in lib.cards() for n in (x["name"], f"{x['dir']}/{x['name']}")}  # fm lib show takes both


def _user_tasks():
    """The processes and threads this user runs now (RLIMIT_NPROC counts them all), from /proc; None without it."""
    uid, n = os.getuid(), 0
    try:
        pids = [x for x in os.listdir("/proc") if x.isdigit()]
    except OSError:
        return None
    for pid in pids:
        try:
            if os.stat(f"/proc/{pid}").st_uid == uid:
                n += len(os.listdir(f"/proc/{pid}/task"))
        except OSError:
            pass  # gone meanwhile
    return n or None


def _limits():
    """[(resource, value)] for a block: file size, address space and (with /proc) the user's tasks plus headroom; never
    looser than the limits fm itself runs under."""
    want = [(resource.RLIMIT_FSIZE, OUTPUT_MAX), (resource.RLIMIT_AS, MEMORY_MAX)]
    tasks = _user_tasks()
    if tasks is not None:  # ponytail: no count without /proc (macOS), so no process cap there
        want.append((resource.RLIMIT_NPROC, tasks + PROCS_HEADROOM))
    return [(r, min(x for x in (v, *resource.getrlimit(r)) if x != resource.RLIM_INFINITY)) for r, v in want]


def _run(code, timeout, limits=(), argv=(sys.executable, "-I", "-c"), env=None, what="python block",
         stdout=subprocess.DEVNULL):
    """Why the block failed, or None: argv + [code] (python3 -I) in a new temporary folder, stdin closed, stdout to
    `stdout`, PATH the only variable passed on (HOME is the folder, so ~ doesn't reach the real one) plus env(folder)
    when given, under `limits`; then its process group is killed, so a hung block, or a server it started and forgot,
    stops with it (one that setsid()s escapes: see the module). T-1519: fm release-check runs the docs' runnable fences
    through it, fm new check a panel."""
    def limit():
        for r, v in limits:
            try:
                resource.setrlimit(r, (v, v))
            except (OSError, ValueError):
                pass  # a platform without that limit
    with tempfile.TemporaryDirectory(prefix="fm-playbook-", ignore_cleanup_errors=True) as d, \
            tempfile.TemporaryFile() as err:
        d = os.path.realpath(d)  # T-1828: macOS /var is /private/var; a fence's $PWD is the real path
        env = dict({"PATH": os.environ.get("PATH") or os.defpath, "HOME": d}, **(env(d) if env else {}))
        try:
            proc = subprocess.Popen([*argv, code], cwd=d, env=env, stdin=subprocess.DEVNULL,
                                    stdout=stdout, stderr=err, start_new_session=True, preexec_fn=limit)
        except (OSError, ValueError) as e:  # a NUL byte, an argument list too long
            return f"{what} can't run: {e.__class__.__name__}"
        try:
            rc = proc.wait(timeout)
        except subprocess.TimeoutExpired:
            rc = None
        try:  # a timed-out leader isn't reaped yet, and live members hold the group id: it can't have been reused
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        if rc is None:
            proc.wait()
            return f"timed out after {timeout:g} s"
        if not rc:
            return None
        err.seek(max(0, err.seek(0, 2) - 4096))
        last = next((x for x in reversed(err.read().decode("utf-8", "replace").splitlines()) if x.strip()), "")
        return (f"{what} exited {rc}" if rc > 0 else f"{what} killed by signal {-rc}") \
            + (f": {c.fit(last.strip(), 160)}" if last.strip() else "")


def problems(root=ROOT, card_names=None, timeout=TIMEOUT, run=True):
    """Every lint finding, as 'path[:line]: what'; [] when the library is clean."""
    from sampo import doctor
    out, files = [], _files(root)
    try:
        skill = _read(os.path.join(root, "SKILL.md"))
    except OSError:
        skill = ""
        out.append("SKILL.md: missing, so nothing is indexed")
    if len(skill) > doctor.SKILL_MAX:
        out.append(f"SKILL.md: {len(skill):,} characters, over its {doctor.SKILL_MAX:,}-character budget: move a "
                   f"stage's lines into references/<stage>/INDEX.md")
    indexes = {"SKILL.md": skill}
    for f in files:
        if os.path.basename(f) == "INDEX.md" and f.count("/") == 2 and f in set(_PATH.findall(skill)):
            indexes[f] = _read(os.path.join(root, f))
    named = {}
    for name, text in indexes.items():
        for n, line in enumerate(text.splitlines(), 1):
            for path in _PATH.findall(line):
                named.setdefault(path, set()).add(name)
                if not os.path.exists(os.path.join(root, path)):
                    out.append(f"{name}:{n}: {path} doesn't exist")
    folders = [(d, s) for d, s in named.items() if d.endswith("/")]  # T-1785: a line naming a folder covers its files
    for f in files:
        stage = "/".join(f.split("/")[:2]) + "/INDEX.md"  # not a list: a path or word fragment, not a list
        by = named.get(f, set()).union(*(s for d, s in folders if f.startswith(d)))
        if not by & {"SKILL.md", stage}:
            out.append(f"{f}: in no index (SKILL.md, or {stage} named in SKILL.md)")
    record, bad = _record(root)
    if bad:
        out.append(bad)
    cards = card_names if card_names is not None else _cards()
    titles, snippets = {}, []
    for rel in _playbooks(root):
        text = _read(os.path.join(root, rel))
        blocks, prose = _blocks(text)
        title = next((x[2:].strip() for x in prose if x.startswith("# ")), rel).lower()  # untitled: its path, unique
        if title in titles:
            out.append(f"{rel}: same title as {titles[title]} ({title!r})")
        titles.setdefault(title, rel)
        head = prose[:next((i for i, x in enumerate(prose) if x.startswith("## ")), len(prose))]
        if not any(_USE.search(x) for x in head):
            out.append(f"{rel}: no Use-when line above its first section ('Use when …')")
        if not any(x.startswith("1. ") for x in prose):
            out.append(f"{rel}: no numbered procedure (a line starting '1. ' outside code)")
        if rel.startswith("references/ui/") and not rel.endswith("/INDEX.md"):  # T-1623
            sec = _CONTRACT.search(text)
            miss = [r for r in UI_CONTRACT if r not in set(_RULE.findall(sec.group(1)))] if sec else None
            if sec is None:
                out.append(f"{rel}: no ## UI contract section ({', '.join(UI_CONTRACT)}: references/ui/INDEX.md)")
            elif miss:
                out.append(f"{rel}: its ## UI contract doesn't say {', '.join(miss)} (a '- rule: how' line each, or "
                           "'n/a: why'; the rules: references/ui/INDEX.md)")
        for name in sorted(set(_FACT.findall(text)) - set(FACTS)):
            out.append(f"{rel}: {{{{{name}}}}} is no fact, so every load shows {{{{{name}?}}}} (facts: {', '.join(FACTS)})")
        if rel in record and _tokens(text) > record[rel] * SLACK[0] + SLACK[1]:
            out.append(f"{rel}: grew from {record[rel]:,} to {_tokens(text):,} tokens, past its ceiling's slack "
                       f"(10% + 50); say so with `fm check playbooks ratchet`, or trim it")
        for n, line in enumerate(text.splitlines(), 1):
            for name in _LIB.findall(line):
                name = name.rstrip(".,;:)!?'\"")
                if name and not name.startswith("-") and not re.search(r"[A-Z<>{}\[\]]", name) \
                        and name not in cards:  # NAME, <name>: placeholders; --section: an option
                    out.append(f"{rel}:{n}: fm lib show {name}: no such card")
        snippets += [(rel, n, body) for n, body in runnable(blocks)]
        for n, info, body in blocks:  # T-1587: a no-run block never runs, so a syntax slip would never show
            if info and info[0].lower() in _PY and "no-run" in info[1:]:
                try:
                    compile(body, f"{rel}:{n}", "exec")
                except (SyntaxError, ValueError) as e:  # ValueError: a NUL byte in the block
                    out.append(f"{rel}:{n}: python no-run block doesn't parse: {getattr(e, 'msg', e)} "
                               f"(its line {getattr(e, 'lineno', '?')})")
    for a, b, share in similar(root, DUP):  # T-1834: same-stage books sharing a title word, not every pair
        if share > DUP:
            out.append(f"{b}: shares {share:.0%} of its wording with {a}: merge them or cut one")
    for rel, n, body in snippets if run else ():
        if sys.version_info < (3, 14) and "__future__" not in body:  # T-1799: 3.14's lazy annotations, on 3.12 too
            body = "from __future__ import annotations\n" + body
        why = _run(body, timeout, _limits())
        if why:
            out.append(f"{rel}:{n}: {why} (mark it ```python no-run if it isn't a self-check)")
    return out


def coverage(root=ROOT, cards=None):
    """T-1623: {stage: {area: n}}: per stage (references/<stage>/), how many playbooks name a card of each stack area
    (fm lib's card folders). Names of 3+ characters, matched as words. ponytail: a name is a word match, so a playbook
    that mentions a stack in passing counts as covering it."""
    if cards is None:
        from sampo import lib
        cards = [(x["dir"], x["name"]) for x in lib.cards()]
    names = {}
    for area, name in cards:
        names.setdefault(area, set())
        if len(name) >= 3:
            names[area].add(name.lower())
    rx = {a: re.compile(r"(?<![\w-])(?:" + "|".join(map(re.escape, sorted(ns, key=len, reverse=True))) + r")(?![\w-])",
                        re.I) for a, ns in names.items() if ns}
    out = {}
    for rel in _playbooks(root):
        parts = rel.split("/")
        if len(parts) < 3 or parts[0] != "references":
            continue
        row = out.setdefault(parts[1], {a: 0 for a in sorted(names)})
        text = _read(os.path.join(root, rel))
        for a, r in rx.items():
            row[a] += bool(r.search(text))
    return dict(sorted(out.items()))


HEAT = "·░▒▓█"  # none, 1, 2, 3, 4+


def coverage_text(cov):
    """The heatmap: a row per stage, a column per area (its name down the column), and a legend."""
    areas = sorted({a for row in cov.values() for a in row})
    if not areas:
        return "no playbooks under references/<stage>/"
    tall = max(map(len, areas))
    head = ["          " + " ".join(a.rjust(tall)[i] for a in areas) for i in range(tall)]
    rows = [f"{stage:<9} " + " ".join(HEAT[min(row.get(a, 0), 4)] for a in areas) for stage, row in cov.items()]
    return "\n".join(head + rows + ["", "playbooks in a stage naming a card of that stack: · none  ░ 1  ▒ 2  ▓ 3  █ 4+"])


def _scan(root, days, routes=False):
    """One pass over the event logs (the rotated ones too): ({book: reads}, {book: routes}, days covered) in the
    window, from every project. T-1834: reach() and unused() share it, so a report reads the logs once."""
    books, read, routed, stages, oldest = _playbooks(root), {}, {}, {}, None
    known = set(books)
    since = c.iso(time.time() - days * 86400)
    for e in c.global_events(""):
        ts = str(e.get("ts") or "")
        oldest = ts if ts and (oldest is None or ts < oldest) else oldest
        if ts < since:
            continue
        if e.get("kind") == "playbook" and e.get("action") == "route":  # T-1833: route TEXT named these, no read
            for hit in {x for x in e.get("paths") or () if isinstance(x, str) and x in known}:
                routed[hit] = routed.get(hit, 0) + 1
            continue
        if hit := _book_of(e, books):
            read[hit] = read.get(hit, 0) + 1
        m = _ROUTE.search(str(e.get("target") or "")) if routes and e.get("tool") == "Bash" else None
        if m:  # route() walks the library: once per stage, not per event
            if m.group(2) not in stages:
                stages[m.group(2)] = (route(m.group(2), root) or (0, 0, []))[2]
            for x in stages[m.group(2)]:
                routed[x] = routed.get(x, 0) + 1
    ts = c.parse_ts(oldest) if oldest and oldest > since else None
    covers = 0 if oldest is None else round((time.time() - ts.timestamp()) / 86400, 1) if ts else days
    return read, routed, covers


def reach(root=ROOT, days=REACH_DAYS):
    """{days, covers, read: {path: reads}, routed: {path: routes}, unread: [path]}: Read events of each playbook in the
    window, from every project; routed counts the books `fm playbooks route TEXT` named (T-1833, not reads; stage
    routes count only in unused()); covers is the
    days the event log actually spans, when that is fewer."""
    read, routed, covers = _scan(root, days)
    return {"days": days, "covers": covers,
            "read": read, "routed": routed, "unread": [b for b in _playbooks(root) if b not in read]}


def _book_of(e, books):
    """The playbook a tool event loaded (fm playbooks show in a shell, or a Read of the skill's file), or None."""
    target = str(e.get("target") or "")
    if e.get("kind") != "tool":
        return None
    if e.get("tool") == "Bash":
        return shown(target, books)
    if e.get("tool") == "Read" and e.get("book"):  # T-1702: the hook keeps the book's name when the target is cut
        key = f"references/{e['book']}.md"
        return key if key in books else None
    if e.get("tool") != "Read" or _MARK not in target:
        return None
    key = "references/" + target.split(_MARK, 1)[1]
    # fmhooks keeps 120 characters of a target: a cut path still names one playbook by its prefix
    hit = [b for b in books if b == key] or ([] if key.endswith(".md") else [b for b in books if b.startswith(key)])
    return hit[0] if len(hit) == 1 else None


def followed(p, b, root=ROOT):
    """T-1834: [{playbook, followed, steps}] for each playbook this task loaded since its first focus (this project's
    tool events), with score()'s share of its steps the run followed. Empty when it loaded none: no scoring then."""
    start = min((e["ts"] for e in c.ledger_tail(p, 5000, kinds=("focus",)) if e.get("task") == b.id and e.get("event") == "focus"
                 and e.get("ts")), default=str(b.meta.get("created") or ""))
    books, loaded = listed(root), []  # an index loads too, and score() counts words over all of them
    # ponytail: the live log's last 50,000 events; a task that outlives a rotation or that many events loses its early loads
    for e in c.tail_jsonl(os.path.join(c.state_dir(), "events.jsonl"), 50000):
        if str(e.get("ts") or "") >= start and e.get("project") == p.slug and (hit := _book_of(e, books)):
            if hit not in loaded:
                loaded.append(hit)
    if not loaded:
        return []
    seen, out = _doc_freq(books, root), []
    for rel in loaded[:RECORD_MAX]:
        rows = score(_read(os.path.join(root, rel)), b, seen=seen, total=len(books))
        out.append({"playbook": rel, "followed": sum(x["followed"] for x in rows), "steps": len(rows)})
    return out


def record_use(p, b, root=ROOT):
    """T-1834: one playbook_use ledger event for a closed task: the books it loaded and the share it followed. Never
    fails the close: a problem is a warning."""
    try:
        got = followed(p, b, root)
        if got:
            c.log_event(p, "playbook_use", task=b.id, data={"books": got})
    except Exception as e:
        print(f"sampo: warning: playbook use not recorded: {e}", file=sys.stderr)


def unused(root=ROOT, days=REACH_DAYS, p=None):
    """T-1834: (drop candidates, days the logs cover): the playbooks with no read, route hit or followed record (this
    project's ledger) in the window. ponytail: the event log is every project's but the followed records are this
    one's; only ever names."""
    since = c.iso(time.time() - days * 86400)
    used, routed, covers = _scan(root, days, routes=True)
    used = set(used) | set(routed)
    for e in c.ledger_events(p, ("playbook_use",), since=since) if p else ():
        used.update(x.get("playbook") for x in (e.get("data") or {}).get("books") or [])
    return [b for b in _playbooks(root) if b not in used], covers


def overlap(a, b):
    """Shingle Jaccard of two shingle sets (what problems() calls sharing wording)."""
    union = len(a | b)
    return len(a & b) / union if union else 0.0


def similar(root=ROOT, least=NEAR):
    """T-1834: merge candidates, [(a, b, jaccard)] with a < b, overlap at least `least`. Exact, not sampled: a pair at
    Jaccard >= least must share one of the first n - ceil(least*n) + 1 shingle hashes (in hash order) of the smaller
    book (prefix filtering), so only books meeting on such a hash are compared, each pair once, and sets whose sizes
    differ by more than 1/least can't reach it (skipped). A book is its sorted shingle hashes (8 bytes each) and only
    pairs at `least` are kept. ponytail: boilerplate shared by thousands of books fills a posting list; skip those
    hashes by document frequency if a library ever does that."""
    sh, post = {}, {}
    for rel in _playbooks(root):
        sh[rel] = array("q", sorted({hash(t) for t in _shingles(_read(os.path.join(root, rel)))}))
        n = len(sh[rel])
        for t in sh[rel][:n - math.ceil(least * n) + 1]:
            post.setdefault(t, []).append(rel)
    seen, out = set(), []
    for rels in post.values():
        for i, a in enumerate(rels):
            for b in rels[i + 1:]:
                x, y = sorted((a, b))
                if (x, y) in seen:
                    continue
                seen.add((x, y))
                if min(len(sh[x]), len(sh[y])) < least * max(len(sh[x]), len(sh[y])):
                    continue
                if (j := overlap(set(sh[x]), set(sh[y]))) >= least:
                    out.append((x, y, j))
    return sorted(out)


def nearest(text, root=ROOT):
    """(book, jaccard) of the playbook sharing the most wording with text, or (None, 0)."""
    mine, best = _shingles(text), (None, 0.0)
    for rel in _playbooks(root):
        j = overlap(mine, _shingles(_read(os.path.join(root, rel))))
        best = (rel, j) if j > best[1] else best
    return best


def listed(root=ROOT):
    """Every markdown file `fm playbooks show` can load (indexes and templates too), as references/…md."""
    return [f for f in _files(root) if f.endswith(".md")]


def resolve(name, books):
    """The books a name means: its path (references/fix/sql.md), <stage>/<name>[.md], or a bare name (sql) when no
    path matches: every book with that file name. Only ever compared, never joined into a path."""
    key = name.strip().removeprefix("references/").removesuffix(".md")
    if f"references/{key}.md" in books:
        return [f"references/{key}.md"]
    return [b for b in books if "/" not in key and key and b.rsplit("/", 1)[-1] == f"{key}.md"]


def shown(target, books):
    """The book a shell command loads with fm playbooks show, or None."""
    m = _SHOW.search(target)
    hits = resolve(m.group(1), books) if m else []
    return hits[0] if len(hits) == 1 else None


def facts(m):
    """{fact: text} from fm map's map m. test is the gate that runs the tests (the repo's runner, pytest, cargo test…),
    else the first gate saying test; language the map's language cards. The ones the repo's own files name (CODE) are
    inline code cut at CODE_MAX, so a file or CI line can't pass for prose. Empty means unknown."""
    gates = [str(g) for g in m.get("gates") or []]
    test = next((g for g in gates if _RUNNER.search(g)), None) or next((g for g in gates if _TESTY.search(g)), "")
    got = {"test": test, "gates": "; ".join(gates),
           "language": ", ".join(str(x.get("name")) for x in m.get("lib_cards") or [] if x.get("area") == "lang"),
           "entry": ", ".join(map(str, m.get("entry") or [])), "ci": "; ".join(map(str, m.get("ci") or [])),
           "layout": ", ".join(str(x[0]) for x in m.get("layout") or [] if x), "hot": ", ".join(map(str, m.get("hot") or [])),
           "cards": ", ".join(map(str, m.get("lib") or []))}
    got = {k: c.fit(c.plain(v), CODE_MAX if k in CODE else FACT_MAX) for k, v in got.items()}
    return {k: _code(v) if k in CODE and v else v for k, v in got.items()}


def _code(v):
    """v as inline code: a fence one backtick longer than any run inside it (CommonMark), spaced when v starts or
    ends with one."""
    fence = "`" * (max(map(len, re.findall(r"`+", v)), default=0) + 1)
    pad = " " if v[:1] == "`" or v[-1:] == "`" else ""
    return f"{fence}{pad}{v}{pad}{fence}"


def render(text, known):
    return _FACT.sub(lambda m: known.get(m.group(1)) or f"{{{{{m.group(1)}?}}}}", text)


def _project_facts(p):
    try:
        from sampo import map as smap
        return facts(smap.load(p)) if c.git_root(p.root) else {}
    except Exception as e:  # the facts are a hint: a broken map costs them, never the playbook
        print(f"fm playbooks: no project facts ({e.__class__.__name__}: {c.fit(c.plain(str(e)), 120)}); "
              f"`fm map --rebuild` remakes the map", file=sys.stderr)
        return {}


def _uncommitted(root, where):
    """Whether the overlay differs from HEAD: new, changed, staged or ignored (or git can't say)."""
    try:
        r = subprocess.run(["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", root, "status",
                            "--porcelain", "--ignored", "--", where], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return True
    return r.returncode != 0 or bool(r.stdout.strip())


def _overlay(root, rel):
    """(where, text, note) of the project's overlay of book rel: its text, or a note why it wasn't read; None without
    one."""
    where = os.path.join(OVERLAYS, rel.removeprefix("references/"))
    path = os.path.join(os.path.realpath(root), where)
    if not os.path.lexists(path):
        return None
    if os.path.realpath(path) != path:  # a link, or a linked folder below the checkout: it could be any file
        return where, "", "(not read: it or a folder above it is a link; an overlay is a plain file in the repo)"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as e:
        return where, "", f"(not read: {e.strerror or e.__class__.__name__})"
    with os.fdopen(fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):  # a FIFO would hang the read
            return where, "", "(not read: not a regular file)"
        data = f.read(OVERLAY_MAX + 1)
    text = c.plain_lines(data[:OVERLAY_MAX].decode("utf-8", "replace")).strip()
    return where, text, f"(cut at {OVERLAY_MAX:,} bytes)" if len(data) > OVERLAY_MAX else ""


def load(rel, root=ROOT, p=None):
    """Book rel as `fm playbooks show` gives it: the shipped text with each {{fact}} filled in from p's fm map (built
    only when the text names one), then project p's overlay under a 'This project' heading: a provenance line and
    the overlay as one blockquote, so it can't open a heading or end the section and pass for shipped text, and its
    own {{…}} (a Mustache or Vue project's) is left as written."""
    text = _read(os.path.join(root, rel)).rstrip("\n") + "\n"
    text = render(text, _project_facts(p) if p and _FACT.search(text) else {})
    got = _overlay(p.root, rel) if p else None
    if got and (got[1] or got[2]):  # an empty overlay adds nothing
        where, body, note = got
        dirty = " (uncommitted)" if body and _uncommitted(os.path.realpath(p.root), where) else ""
        quoted = [PROVENANCE, "\n".join(f"> {x}".rstrip() for x in body.splitlines())] if body else []
        text += "\n\n".join([f"\n## This project: {where}{dirty}", *quoted, *([note] if note else [])]) + "\n"
    return text


_STAGE = re.compile(rf"\b(?:{'|'.join(c.NAMESPACES)}):playbooks \((\w+)")  # SKILL_FOR's "Skill sampo:playbooks (clean)"
_TASK = re.compile(r"\bT-\d{4,}\b")
_TRAILER = re.compile(r"(?mi)^Task:\s*(T-\d{4,})\b")
_QUOTED = re.compile(r"`([^`\n]+)`|\"([^\"\n]+)\"|“([^”\n]+)”")
_VERIFY = re.compile(r"\s::\s+(.+)")  # a criterion's "done when … :: CMD", as fm task new --ac takes it


def route(stage, root=ROOT):
    """(SKILL_FOR key, its hint, [book]) for a stage or task type (any case): the books its hint shows by name, and
    for 'sampo:playbooks (X)' stage X's INDEX.md, else every playbook under references/X/. None: no such stage."""
    key = next((k for k in c.SKILL_FOR if k.lower() == stage.strip().lower()), None)
    if key is None:
        return None
    hint, books, got = c.SKILL_FOR[key], listed(root), []
    for m in _SHOW.finditer(hint):
        got += resolve(m.group(1), books)[:1]
    for m in _STAGE.finditer(hint):
        d = f"references/{m.group(1)}/"
        got += [d + "INDEX.md"] if d + "INDEX.md" in books else [b for b in _playbooks(root) if b.startswith(d)]
    return key, hint, list(dict.fromkeys(got))


# T-1833: routing by meaning. An FTS5 index of the library in Foreman's state (the plugin tree may be read-only),
# refreshed from one stat pass: only a book whose mtime or size changed is read again. Costs nothing until a query.
BODY_MAX = 4096  # characters of a book indexed: 20k books stay a few tens of MB
READ_MAX = 8192  # bytes of a book read to index it
TERMS_MAX = 8  # query words kept: the first ones (a task's title) route real tasks best, more dilute them (T-1838)
WEIGHTS = (0, 10, 10, 2, 0.25)  # bm25 per column: path (unindexed), title, use line, headings, body (T-1838)
FOLDER = {"FIX": "fix", "PERFORMANCE": "perf", "SECURITY": "security", "CLEAN": "clean", "RESEARCH": "ground"}
PREFER = 1.5  # T-1838: a task type's folder (FOLDER) scores this many times higher
INDEX_VERSION = 1


def _entries(root, dirs):
    """Every playbook's DirEntry (what _playbooks lists) under root; dirs gets each folder's (path, mtime_ns). The same
    rule as that walk: a link to a regular file counts, a linked folder isn't entered; a FIFO or device is skipped,
    since reading one could block."""
    todo = [os.path.join(os.path.abspath(root), "references")]
    while todo:
        d = todo.pop()
        try:
            dirs.append((d, os.stat(d).st_mtime_ns))
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                n = e.name
                try:
                    if n.endswith(".md"):
                        if n != "INDEX.md" and e.is_file():  # as _playbooks' walk: a link to a regular file counts
                            yield e
                    elif n != "templates" and e.is_dir(follow_symlinks=False):
                        todo.append(e.path)
                except OSError:
                    continue  # gone since the listing


def _stat_tree(root):
    """{(rel, mtime_ns, size)} for every playbook, one stat each."""
    cut, out = len(os.path.abspath(root)) + 1, set()
    for e in _entries(root, []):
        try:
            st = e.stat()
        except OSError:
            continue
        out.add((e.path[cut:], st.st_mtime_ns, st.st_size))
    return out


def _signature(root):
    """A string that changes when a book is added, removed, renamed or edited, for one stat per book and no more: the
    count and the sums of mtimes and sizes, and every folder's mtime (an add, remove or rename touches one). The stat
    per file is what a call costs at 20k books, so a call that finds this unchanged reads nothing else.
    ponytail: two edits whose mtimes move opposite ways by the same ns cancel in the sum; the next edit shows them."""
    n = m = z = 0
    dirs = []
    for e in _entries(root, dirs):
        try:
            st = e.stat()
        except OSError:
            continue
        n += 1
        m += st.st_mtime_ns
        z += st.st_size
    return f"{n} {m} {z} {sorted(dirs)}"


def _doc(root, rel):
    """(title, use line, headings, body) of a book; empty ones when it can't be read (it is read again once its mtime
    or size changes)."""
    try:
        with open(os.path.join(root, rel), encoding="utf-8", errors="replace") as f:
            text = f.read(READ_MAX)
    except OSError:
        return ("", "", "", "")
    lines = text.splitlines()
    return (next((x[2:].strip() for x in lines if x.startswith("# ")), ""),
            next((x.strip() for x in lines[:40] if _USE.search(x)), ""),
            " ".join(x[3:].strip() for x in lines if x.startswith("## ")), text[:BODY_MAX])


def _terms(text):
    """The words a query keeps: 5+ letters, not common, each once, TERMS_MAX at most."""
    return list(dict.fromkeys(w for w in _WORD.findall(text.lower()) if w not in _COMMON))[:TERMS_MAX]


def _index_file(root):
    """Where root's index lives: in the state folder, named by a hash of the library's real path."""
    folder = os.path.join(c.state_dir(), "playbooks")
    os.makedirs(folder, exist_ok=True)
    key = hashlib.sha1(os.path.realpath(root).encode("utf-8", "replace")).hexdigest()[:12]
    return os.path.join(folder, f"index-v{INDEX_VERSION}-{key}.db")


def _drop_index(path):
    for suffix in ("", "-journal", "-wal", "-shm"):
        with contextlib.suppress(OSError):
            os.remove(path + suffix)


def _prune(mine):
    """Drops the other indexes whose library (recorded in their meta row) is gone: a plugin update runs from a new
    cache folder, and its index starts again beside the old one's."""
    import pathlib
    import sqlite3
    for f in os.listdir(os.path.dirname(mine)):
        path = os.path.join(os.path.dirname(mine), f)
        if not (f.startswith("index-") and f.endswith(".db")) or path == mine:
            continue
        try:
            con = sqlite3.connect(pathlib.Path(path).as_uri() + "?mode=ro", uri=True, timeout=1)
            try:
                row = con.execute("select root from meta").fetchone()
            finally:
                con.close()
        except sqlite3.Error:
            continue
        if row and not os.path.isdir(row[0]):
            _drop_index(path)


def _index(root):
    """The open, refreshed index of root: raises sqlite3.Error (no FTS5, a broken file) for the caller to handle."""
    import sqlite3
    # 60 s: a first build of 20k books by one process is not a reason for a second to scan every file instead
    con = sqlite3.connect(_index_file(root), timeout=60, isolation_level=None)
    try:
        con.execute("pragma synchronous=off")  # rebuildable: a torn file is dropped and built again
        con.execute("create table if not exists meta(root text)")
        if con.execute("select 1 from meta").fetchone() is None:  # a new index: say whose, and tidy the dead ones
            con.execute("insert into meta values(?)", (os.path.realpath(root),))
            _prune(_index_file(root))
        con.execute("create table if not exists files(path text primary key, mtime_ns int, size int)")
        con.execute("create virtual table if not exists books using fts5(path unindexed, title, use, heads, body, "
                    "tokenize='porter unicode61')")
        con.execute("create table if not exists seen(sig text)")
        sig = _signature(root)  # before the books are read: an edit after it shows as a change next call
        if (con.execute("select sig from seen").fetchone() or (None,))[0] != sig:
            now = _stat_tree(root)
            con.execute("begin immediate")  # another fm may have refreshed since: read the table again under the lock
            have = {p: (m, s, i) for p, m, s, i in con.execute("select path, mtime_ns, size, rowid from files")}
            paths = {p for p, _, _ in now}
            for p, (_, _, i) in have.items():
                if p not in paths:
                    con.execute("delete from books where rowid=?", (i,))
                    con.execute("delete from files where rowid=?", (i,))
            for p, *ms in now:
                if p in have and list(have[p][:2]) == ms:
                    continue
                doc = _doc(root, p)
                if p in have:
                    con.execute("delete from books where rowid=?", (have[p][2],))
                    con.execute("update files set mtime_ns=?, size=? where rowid=?", (*ms, have[p][2]))
                    rid = have[p][2]
                else:
                    rid = con.execute("insert into files values(?,?,?)", (p, *ms)).lastrowid
                con.execute("insert into books(rowid, path, title, use, heads, body) values(?,?,?,?,?,?)", (rid, p, *doc))
            con.execute("delete from seen")
            con.execute("insert into seen values(?)", (sig,))
            con.execute("commit")
        return con
    except BaseException:
        con.close()
        raise


def _fts_find(root, terms, k, kind=None):
    import sqlite3
    q = " OR ".join('"' + t + '"' for t in terms)
    near = (f"references/{FOLDER[kind]}/%", PREFER) if kind in FOLDER else ("", 1)
    for attempt in (0, 1):
        try:
            con = _index(root)
            try:  # the query too: a corrupt FTS table passes the refresh when the signature matches
                return [{"path": p, "score": round(-s, 2), "use": u} for p, u, s in con.execute(
                    "select path, use, bm25(books, ?, ?, ?, ?, ?) * (case when path like ? then ? else 1 end) as s "
                    "from books where books match ? order by s limit ?", (*WEIGHTS, *near, q, k))]
            finally:
                con.close()
        except sqlite3.DatabaseError as e:
            if attempt or "no such module" in str(e).lower() or "locked" in str(e).lower():
                raise
            _drop_index(_index_file(root))  # a torn or corrupt file: build it again, once


def find(text, k=5, root=ROOT, kind=None):
    """(mode, [{path, score, use}]): the k books that best fit text, best first; mode fts5, or scan when this
    Python's sqlite has no FTS5 (or the index can't be had): books whose title and use line share the most words.
    kind: a task type, whose folder's books score PREFER times higher (FTS5 only)."""
    import sqlite3
    terms = _terms(text)
    if not terms:
        return "fts5", []
    try:
        return "fts5", _fts_find(root, terms, k, kind)
    except sqlite3.Error:
        pass
    got = []
    for rel, *_ in _stat_tree(root):
        doc = _doc(root, rel)
        n = len(set(terms) & set(_WORD.findall(f"{doc[0]} {doc[1]}".lower())))
        if n:
            got.append({"path": rel, "score": float(n), "use": doc[1]})
    return "scan", sorted(got, key=lambda x: (-x["score"], x["path"]))[:k]


def task_text(b):
    """What a task asks for, as a query: its title, interpretation and criteria (without their verify commands)."""
    crit = "\n".join(x.split("::")[0] for x in b.section("Acceptance criteria").splitlines())
    return f"{b.title}\n{b.section('Interpretation')}\n{crit}"


def cmd_route(args):
    """fm playbooks route TEXT|--task ID [-k N]: the top books by relevance, with score and Use-when line."""
    from sampo import cli
    if args.task:
        b = c.find_brief(cli.resolve(args), args.task)
        if not b:
            raise cli.UsageError(f"no task {args.task}")
        text, kind = task_text(b), b.type
    else:
        text, kind = args.name, None
    k = max(1, min(args.k, 50))
    mode, got = find(text, k, kind=kind)
    if not got and not args.task and len(text.split()) == 1:  # one word: most likely a stage key mistyped
        raise cli.UsageError(f"no stage or book fits {text!r}; stages: {', '.join(c.SKILL_FOR)}")
    if got:
        from sampo import hooks
        hooks._event({"kind": "playbook", "action": "route", "paths": [x["path"] for x in got]})  # the text stays out
    return cli.out(args, {"mode": mode, "playbooks": got}, "\n".join(
        f"  {x['score']:>6}  {x['path']} — {c.fit(x['use'], 100)}" for x in got) if got else
        "no playbook fits: no word of 5 letters or more matches a title, Use-when line, heading or body "
        f"(a stage routes by its key: {', '.join(c.SKILL_FOR)})") or 0


def blame(path):
    """[{line, task, commit, summary, text}] per line of path from git blame: task is the first T-NNNN in the commit's
    whole message (None without one), commit None for a line not committed yet. ValueError when git can't blame it."""
    d, f = os.path.split(path)
    # the repo's own config could name a command: no textconv, no signature check (gpg.program). ponytail: a clean
    # filter in its .gitattributes still runs on an uncommitted file, so workers may not blame (fmcli._worker_may)
    git = ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-c", "log.showSignature=false", "-C", d]
    try:
        r = subprocess.run([*git, "blame", "--porcelain", "--no-textconv", "--", f], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        raise ValueError(f"git blame failed: {e.__class__.__name__}")
    if r.returncode:
        why = c.fit(c.plain(r.stderr.decode("utf-8", "replace").strip()), 160)
        raise ValueError(f"git can't blame it: {why or 'no reason given'}")
    rows, sha = [], None
    for x in r.stdout.decode("utf-8", "replace").split("\n"):  # bytes: text mode would split a line at a \r
        if x.startswith("\t"):
            rows.append((sha, x[1:]))
        elif m := re.match(r"([0-9a-f]{40}) \d+ \d+", x):
            sha = m.group(1)
    shas = sorted({s for s, _ in rows if s.strip("0")})
    msgs = {}
    if shas:  # one git call for every commit's whole message: an id can sit below the summary line
        try:
            log = subprocess.run([*git, "log", "--no-walk=unsorted", "--stdin", "--format=%H%x00%B%x1e"],
                                 input=("\n".join(shas) + "\n").encode(), capture_output=True,
                                 timeout=30).stdout.decode("utf-8", "replace")
        except (OSError, subprocess.SubprocessError):
            log = ""
        msgs = dict(x.strip("\n").split("\0", 1) for x in log.split("\x1e") if "\0" in x)
    out = []
    for n, (s, text) in enumerate(rows, 1):
        msg = msgs.get(s, "") if s and s.strip("0") else None
        head, _, body = (msg or "").strip().partition("\n")
        # the summary's id, then a Task: trailer, then the body's first: "Reverts T-1500" in a body isn't the author
        task = _TASK.search(head) or _TRAILER.search(body) or _TASK.search(body)
        out.append({"line": n, "task": task.group(task.lastindex or 0) if task else None,
                    "commit": s if msg is not None else None, "summary": c.fit(c.plain(head), 80), "text": text})
    return out


def preview(text):
    """{runnable: [{line, code}], verify: [cmd]}: the python blocks fm check playbooks runs, and the criterion
    verify commands its prose quotes ("… :: CMD" or `… :: CMD`): a bare :: in a sentence is no command."""
    blocks, prose = _blocks(text)
    spans = [q.group(q.lastindex) for x in prose for q in _QUOTED.finditer(x)]
    verify = [m.group(1).strip() for s in spans for m in [_VERIFY.search(s)] if m]
    return {"runnable": [{"line": n, "code": b} for n, b in runnable(blocks)],
            "verify": list(dict.fromkeys(v for v in verify if v))}


# T-1523: playbooks drafted from what this repo has done: a proved task's steps, its history's co-changes, its failures
_EVIDENCE = re.compile(r"^- \(step (\d+)\) `(.+?)` → exit 0\b")
_WORD = re.compile(r"[a-z][a-z0-9_]{4,}")
_COMMON = {"python3", "plugin", "tests", "failed", "error", "classes", "slowest", "after", "before", "which", "there",
           "their", "about", "should", "would", "every", "first", "check", "where", "these", "those", "things"}


def draft(b):
    """T-1523: a playbook draft in the library's shape (a Use-when line, a numbered procedure) from a done task's
    brief, the distilled transcript: each step with the last run that proved it, the criteria as checks, its lessons
    and what failed first as pitfalls, and the files it touched."""
    proof = {int(m.group(1)): m.group(2) for m in map(_EVIDENCE.match, b.evidence()) if m}
    out = [f"# {b.title}", "", f"Use when: a {str(b.meta.get('type') or 'task').lower()} task like {b.id}'s. "
           f"Drafted by fm playbooks draft {b.id}; edit it before you keep it.", "", "## Procedure", ""]
    out += [f"{s.n}. {s.text}" + (f" — proved by `{proof[s.n]}`." if s.n in proof else "") for s in b.steps()]
    for head, rows in (("Checks", [f"- {a.text}" for a in b.acceptance()]),
                       ("Pitfalls", [x for x in (b.section("Lessons") + "\n" + b.section("Plan gaps")).splitlines()
                                     if x.startswith("- ")]),
                       ("Files it touched", [x for x in b.section("Files touched").splitlines() if x.startswith("- ")])):
        out += ["", f"## {head}", ""] + rows if rows else []
    return "\n".join(out) + "\n"


def mine(root, last=400, least=5, share=0.6):
    """T-1523: recipes from the repo's history: each file changed in at least `least` of the last commits, with the
    files changed beside it in at least `share` of those (the unwritten "change X, change Y too"). A commit of more
    than 30 files is a sweep and says nothing about pairs."""
    from collections import Counter
    seen, beside = Counter(), {}
    for chunk in c._git(root, "log", "--no-merges", f"-{last}", "--name-only", "--format=%x00", timeout=30).split("\0"):
        files = set(filter(None, chunk.splitlines()))
        for f in files if 1 < len(files) <= 30 else ():
            seen[f] += 1
            beside.setdefault(f, Counter()).update(files - {f})
    out = []
    for f, n in sorted(seen.items(), key=lambda x: (-x[1], x[0])):
        pals = sorted(((g, k) for g, k in beside[f].items() if k / n >= share), key=lambda x: (-x[1], x[0]))
        if n >= least and pals:
            out.append({"file": f, "commits": n, "with": [[g, round(k / n, 2)] for g, k in pals]})
    return out


def counter(p, root, last=2000):
    """T-1523: the counter-playbook, what failed here: commits git reverted (its "This reverts commit" line) and the
    tasks blocked with a reason, newest first (a dropped task is mostly a duplicate or already built, not a failure)."""
    rows = []
    for line in c._git(root, "log", f"-{last}", "--grep=This reverts commit", "--format=%h%x09%s", timeout=30).splitlines():
        sha, _, subject = line.partition("\t")
        m = re.fullmatch(r'Revert "(.*)"', subject)
        rows.append({"kind": "reverted", "what": m.group(1) if m else subject, "why": f"reverted by {sha}"})
    for b in sorted(c.load_briefs(p, include_archive=True) if p else [], key=lambda b: str(b.meta.get("updated")),
                    reverse=True):
        why = [m.group(1) for m in re.finditer(rf"(?m)^- \S+ {b.status}: (.+)$", b.section("Log"))]
        if b.status == "blocked" and why:
            rows.append({"kind": b.status, "what": f"{b.id} {b.title}", "why": why[-1]})
    return rows


def _sharing(text, words):
    """[{n, text, words}]: each numbered line of a playbook with the words (5+ letters, not common) it shares with
    `words`."""
    words = set(_WORD.findall(" ".join(words).lower())) - _COMMON
    return [{"n": int(m.group(1)), "text": m.group(2), "words": sorted(words & set(_WORD.findall(m.group(2).lower())))}
            for m in (re.match(r"(\d+)\. (.+)", line.strip()) for line in text.splitlines()) if m]


def look_first(text, b):
    """T-1523: the playbook re-ranked by this task's trouble: up to three of its numbered lines sharing the most words
    (two at least) with the active task's failed runs and what failed first."""
    bad = [x for x in b.evidence() if "→ ✗" in x] + b.section("Plan gaps").splitlines() if b else []
    hits = [x for x in _sharing(text, bad) if len(x["words"]) >= 2] if bad else []
    return sorted(hits, key=lambda x: (-len(x["words"]), x["n"]))[:3]


def _doc_freq(books, root=ROOT):
    """{word: playbooks using it} over these books. T-1834: kept under the state dir while no book's size or mtime
    changes, so a close opens only the books it loaded, not the library (20,000 opens). ponytail: one edited book
    counts everything again; per-book word sets would recount only it."""
    stats = []
    for rel in books:
        try:
            st = os.stat(os.path.join(root, rel))
            stats.append((rel, st.st_size, st.st_mtime_ns))
        except OSError:
            stats.append((rel, 0, 0))
    sig = hashlib.sha1(json.dumps([root, stats]).encode()).hexdigest()
    path = os.path.join(c.state_dir(), "playbook_df.json")
    try:
        with open(path, encoding="utf-8") as f:
            kept = json.load(f)
        if isinstance(kept, dict) and kept.get("sig") == sig and isinstance(kept.get("df"), dict):
            return kept["df"]
    except (OSError, ValueError):
        pass
    seen = {}
    for rel in books:
        for w in set(_WORD.findall(_read(os.path.join(root, rel)).lower())):
            seen[w] = seen.get(w, 0) + 1
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        c.write_atomic(path, json.dumps({"sig": sig, "df": seen}))
    except OSError:
        pass  # a read-only state dir only costs the next close the recount
    return seen


def score(text, b, seen=None, total=None):
    """T-1525, the dojo: each of a playbook's numbered steps, followed when the task's run (its steps, evidence, approach
    and log) shares two or more of its telling words: ones at most an eighth of the library's playbooks use, so 'files'
    or 'evidence' never make a match. T-1834: a caller scoring several books passes the word counts once."""
    if seen is None:
        books = listed()
        seen, total = _doc_freq(books), len(books)
    limit = max(1, total // 8)
    run = [s.text for s in b.steps()] + b.evidence() + [b.section("Approach (options → choice → why)"), b.section("Log")]
    rows = [dict(x, words=[w for w in x["words"] if seen.get(w, 0) <= limit]) for x in _sharing(text, run)]
    return [dict(x, followed=len(x["words"]) >= 2) for x in rows]


def lesson(p, root, rng):
    """T-1525: a five-minute lesson from a diff: its commits, the folders it changed, what each task its commits name
    learned, and the review checklists the change calls for. ponytail: one level for every reader; a user level would
    choose how much each part explains."""
    rng = rng if ".." in rng else f"{rng}~1..{rng}"  # one commit: the change it made
    log = c._git(root, "log", "--no-merges", "-50", "--format=%h%x09%s", rng, "--", timeout=30, fail=None)
    if log is None:
        raise ValueError(f"git can't read {rng}")
    commits = [x.split("\t", 1) for x in log.splitlines() if "\t" in x]
    files = [f for f in c._git(root, "diff", "--name-only", rng, "--", timeout=30).splitlines() if f]
    folders = {}
    for f in files:
        folders.setdefault(os.path.dirname(f) or ".", []).append(os.path.basename(f))
    learned = []
    for tid in dict.fromkeys(re.findall(r"\bT-\d{4,}\b", " ".join(s for _, s in commits))):
        b = c.find_brief(p, tid) if p else None
        rows = [x[2:] for x in (b.section("Lessons").splitlines() if b else []) if x.startswith("- ")]
        learned += [f"{tid} {b.title}: {x}" for x in rows[:2]] if b else []
    names = ["review/code-reviewer"] + sorted({REVIEW_BY_EXT[e] for f in files
                                               if (e := os.path.splitext(f)[1].lower()) in REVIEW_BY_EXT})
    return {"range": rng, "commits": commits[:12], "more": max(0, len(commits) - 12), "files": len(files),
            "folders": sorted(folders.items(), key=lambda x: -len(x[1]))[:8], "learned": learned[:8],
            "checklists": names}


def lesson_text(x):
    parts = [f"Lesson: {x['range']}, {len(x['commits']) + x['more']} commit(s), {x['files']} file(s) (about five "
             "minutes)", "", "What changed"] + [f"  {sha} {subject}" for sha, subject in x["commits"]]
    parts += [f"  … and {x['more']} more"] if x["more"] else []
    parts += ["", "Where"] + [f"  {d}/ ({len(fs)}): {', '.join(fs[:6])}{' …' if len(fs) > 6 else ''}"
                              for d, fs in x["folders"]]
    parts += ["", "What was learned"] + [f"  {y}" for y in x["learned"]] if x["learned"] else []
    parts += ["", "Read it with", "  " + ", ".join(f"fm playbooks show {n}" for n in x["checklists"])]
    return "\n".join(parts)


def _blame_text(rel, rows):
    """Runs of lines from one commit: '1-12  T-1505  <sha>  <summary>'."""
    out, i = [rel], 0
    while i < len(rows):
        j = i
        while j + 1 < len(rows) and rows[j + 1]["commit"] == rows[i]["commit"]:
            j += 1
        r = rows[i]
        span = f"{r['line']}" + (f"-{rows[j]['line']}" if j > i else "")
        who = ("uncommitted" if r["commit"] is None else
               f"{r['task'] or 'no task id'}  {r['commit'][:10]}  {r['summary']}")
        out.append(f"  {span:>9}  {who}")
        i = j + 1
    return "\n".join(out)


def _near(name, books, n=3):
    """T-2009: up to n playbook names close to a misspelled one ("alpah" -> fix/alpha), best first."""
    import difflib
    names = [b.removeprefix("references/").removesuffix(".md") for b in books]
    q = name.lower().removeprefix("references/").removesuffix(".md")
    def score(x):
        return 1.0 if q in x else max(difflib.SequenceMatcher(None, q, x).ratio(),
                                      difflib.SequenceMatcher(None, q.rsplit("/", 1)[-1], x.rsplit("/", 1)[-1]).ratio())
    return [x for sc, x in sorted(((score(x), x) for x in names), key=lambda t: -t[0]) if sc >= 0.6][:n]


def _one(name, books):
    from sampo import cli
    hits = resolve(name, books)
    if len(hits) != 1:
        near = [] if hits else _near(name, books)
        raise cli.UsageError(f"{name!r} names {len(hits)} playbooks: {', '.join(hits)}; give its stage too"
                               if hits else f"no playbook {name!r}: did you mean {' or '.join(near)}?" if near
                               else f"no playbook {name!r}: name it as SKILL.md does (fix/sql or references/fix/sql.md)")
    return hits[0]


def cmd_show(args):
    """fm playbooks show NAME (T-1621); eval NAME (T-1618); route STAGE|TEXT|--task ID, blame NAME, preview NAME (T-1622)."""
    from sampo import cli
    if args.action == "small":  # T-2909: cards written for small models
        from sampo import smallcards
        return smallcards.cmd(args)
    if args.action == "coverage" and args.unused:  # T-1834: who earns a place; only ever names, never deletes
        p = cli.resolve(args, create=False) if getattr(args, "project", None) else c.find_project(os.getcwd())
        days = REACH_DAYS if args.days is None else args.days
        if days < 1:
            raise cli.UsageError("fm playbooks coverage --unused --days takes a whole number of days, 1 or more")
        (drop, covers), merge = unused(ROOT, days, p), [(a, b, round(j, 2)) for a, b, j in similar(ROOT)]
        return cli.out(args, {"days": days, "covers": covers, "unused": drop, "similar": merge}, (
            (f"Note: the event log covers only {covers:g} of the {days} days, so a book read earlier shows as unused\n"
             if covers < days else "")
            + f"Drop candidates: no read, route hit or followed record in {days} days ({len(drop)})"
            + "".join(f"\n  {x}" for x in drop[:SHOWN]) + (f"\n  … and {len(drop) - SHOWN} more (--json lists them all)"
                                                           if len(drop) > SHOWN else "")
            + f"\nMerge candidates: {int(NEAR * 100)}%+ of their wording shared ({len(merge)})"
            + "".join(f"\n  {a} ~ {b} ({j:.0%})" for a, b, j in merge[:SHOWN])
            + (f"\n  … and {len(merge) - SHOWN} more" if len(merge) > SHOWN else "")
            + "\nThis names books; it never deletes one.")) or 0
    if args.action == "coverage":  # T-1623
        cov = coverage()
        return cli.out(args, cov, coverage_text(cov)) or 0
    if args.action == "lesson":  # T-1525: a five-minute lesson from a diff
        p = cli.resolve(args) if getattr(args, "project", None) else c.find_project(os.getcwd())
        root = c.git_root(p.root if p else os.getcwd())
        rng = args.name or "HEAD"
        if not root or rng.startswith("-"):
            raise cli.UsageError("fm playbooks lesson [RANGE] reads a git range (a..b or one commit) in a repository")
        try:
            got = lesson(p, root, rng)
        except ValueError as e:
            raise cli.UsageError(f"fm playbooks lesson: {e}")
        return cli.out(args, got, lesson_text(got)) or 0
    if args.action in ("mine", "counter"):  # T-1523: from this repo's history
        p = cli.resolve(args) if getattr(args, "project", None) else c.find_project(os.getcwd())
        root = c.git_root(p.root if p else os.getcwd())
        if not root:
            raise cli.UsageError(f"fm playbooks {args.action} reads git history: run it in a git repository")
        if args.action == "mine":
            got = mine(root)
            return cli.out(args, {"recipes": got}, (
                f"Recipes from {root}'s last 400 commits: files changed together\n" + "\n".join(
                    f"  {x['file']} ({x['commits']} commits): also "
                    + c.joinmore([f"{g} {round(s * 100)}%" for g, s in x["with"]], 5) for x in got[:30])
                + (f"\n  {c.more(got, 30)[1]}" if len(got) > 30 else "")
                if got else "no recipes: no file changed in 5 or more of the last 400 commits with the same others")) or 0
        got = counter(p, root)
        return cli.out(args, {"failures": got}, ("What failed here (don't repeat it)\n" + "\n".join(
            f"  ✗ {x['what']} ({x['kind']}: {x['why']})" for x in got[:40])
            if got else "nothing failed here: no reverted commit and no task blocked with a reason")) or 0
    if args.action == "route" and (args.task or (args.name and route(args.name) is None)):  # T-1833: by meaning
        return cmd_route(args)
    if args.name is None:  # left out (coverage takes none); an empty one still says no playbook
        raise cli.UsageError(f"fm playbooks {args.action} NAME" + (" (or --task ID)" if args.action == "route" else ""))
    if args.action == "eval":
        return cmd_eval(args)
    if args.action == "draft":  # T-1523
        p = cli.resolve(args)
        b = c.find_brief(p, args.name)
        if not b:
            raise cli.UsageError(f"no task {args.name}")
        if b.status != "done":
            raise cli.UsageError(f"{b.id} is {b.status}: a draft comes from a done task, whose steps were proved")
        text = draft(b)
        near, share = nearest(text)  # T-1834: a draft that says what a book says is a merge, not a new book
        dup = near is not None and share >= NEAR
        if near and share:
            print(f"nearest: {near} ({share:.0%} of its wording shared)", file=sys.stderr)
        if dup and not args.force:
            if getattr(args, "json", False):
                cli.out(args, {"task": b.id, "refused": True, "nearest": near, "overlap": round(share, 2)}, None)
            else:
                print(f"fm playbooks draft: {near} already says this ({share:.0%} shared wording): edit that book, or "
                      f"--force to draft anyway", file=sys.stderr)
            return 1
        return cli.out(args, {"task": b.id, "text": text, "nearest": near, "overlap": round(share, 2)},
                         text.rstrip("\n")) or 0
    if args.action == "route":
        got = route(args.name)
        if got is None:
            raise cli.UsageError(f"no stage {args.name!r}: one of {', '.join(c.SKILL_FOR)}")
        key, hint, books = got
        return cli.out(args, {"stage": key, "hint": hint, "playbooks": books},
                         f"{key}: {hint}\n" + ("\n".join(f"  {b}" for b in books) or "  (no playbook: the hint is "
                                                                                       "a skill or a verb)")) or 0
    books = listed()
    hits = [_one(args.name, books)]
    if args.action == "score":  # T-1525, the dojo: a done task's run against this playbook
        p = cli.resolve(args)
        b = c.find_brief(p, args.task) if args.task else None
        if not b:
            raise cli.UsageError(f"fm playbooks score {args.name} --task ID: " + (f"no task {args.task}" if args.task
                                                                                   else "which task's run to score"))
        rows = score(load(hits[0], ROOT), b)
        got = sum(x["followed"] for x in rows)
        return cli.out(args, {"playbook": hits[0], "task": b.id, "followed": got, "steps": rows}, (
            f"{b.id} followed {got} of {len(rows)} steps of {hits[0]}" + "".join(
                f"\n  {'✓' if x['followed'] else '·'} {x['n']}. {c.fit(x['text'], 100)}" for x in rows))) or 0
    if args.action == "kata":  # T-1525: the playbook's held-out briefs as practice; --answer FILE scores one answer
        path = os.path.join(EVALS, hits[0][len("references/"):-len(".md")] + ".json")
        try:
            briefs = load_briefs(path)
        except ValueError:
            raise cli.UsageError(f"no katas for {hits[0]}: its held-out briefs ({path}) are missing or broken")
        if not args.answer:
            return cli.out(args, {"playbook": hits[0], "katas": [x["task"] for x in briefs]}, (
                f"Katas for {hits[0]}: answer one with your plan and commands, then fm playbooks kata "
                f"{args.name} --answer FILE" + "".join(f"\n  {k}. {x['task']}" for k, x in enumerate(briefs, 1)))) or 0
        try:
            answer = _read(args.answer)
        except OSError as e:
            raise cli.UsageError(f"can't read {args.answer}: {e.strerror}")
        rows = [{"task": x["task"], "pass": passes(answer, x["checks"])} for x in briefs]
        return cli.out(args, {"playbook": hits[0], "katas": rows}, (
            f"{sum(x['pass'] for x in rows)} of {len(rows)} katas pass with this answer" + "".join(
                f"\n  {'✓' if x['pass'] else '✗'} {c.fit(x['task'], 100)}" for x in rows))) or 0
    if args.action == "blame":
        try:
            rows = blame(os.path.join(ROOT, hits[0]))
        except ValueError as e:
            raise cli.UsageError(f"fm playbooks blame {hits[0]}: {e}")
        return cli.out(args, {"playbook": hits[0], "lines": rows}, _blame_text(hits[0], rows)) or 0
    if args.action == "preview":
        got = preview(_read(os.path.join(ROOT, hits[0])))
        lines = [f"  line {x['line']}: python block (fm check playbooks runs it)\n"
                 + textwrap.indent(x["code"].rstrip("\n"), "    ") for x in got["runnable"]]
        lines += [f"  verify: {v}" for v in got["verify"]]
        return cli.out(args, {"playbook": hits[0], **got}, f"{hits[0]}: " + (
            f"{len(got['runnable'])} runnable block(s), {len(got['verify'])} verify command(s)\n" + "\n".join(lines)
            if lines else "no checks: no runnable python block and no named verify command")) or 0
    p = cli.resolve(args) if getattr(args, "project", None) else c.find_project(os.getcwd())
    if p and any(os.path.isdir(os.path.join(p.root, m, "playbooks")) for m in c.MIRRORS):
        print(f"fm playbooks: {c.MIRROR}/playbooks/ isn't read; overlays live in {OVERLAYS}/<stage>/<name>.md",
              file=sys.stderr)
    text = load(hits[0], ROOT, p)
    if getattr(args, "model", None):  # T-2917: the kept small-model variant for that profile, else the original
        from sampo import smallcards
        kept = smallcards.variant_for(hits[0], args.model)
        if kept:
            return cli.out(args, {"playbook": hits[0], "text": kept, "variant": args.model}, kept) or 0
    first = look_first(text, c.active_brief_fast(p) if p else None)  # T-1523
    if first:
        text = ("Look here first: this task's failed runs share words with these steps\n"
                + "".join(f"  {x['n']}. {x['text']} ({', '.join(x['words'])})\n" for x in first) + "\n" + text)
    cli.out(args, {"playbook": hits[0], "text": text, **({"look_first": first} if first else {})}, text.rstrip("\n"))
    return 0


def cmd(args, out):
    """fm check playbooks [ratchet]: the lint and the reach report; exit 1 on any finding."""
    if args.words not in ([], ["ratchet"]):  # `ratchet typo` rewrites nothing
        from sampo import cli
        raise cli.UsageError("fm check playbooks [ratchet]")
    if args.words:
        changed = ratchet()
        out(args, {"ratchet": changed}, f"ratchet.json: {len(changed)} playbook size(s) recorded"
            + "".join(f"\n  {k}: {o if o is not None else 'new'} → {n} tokens" for k, (o, n) in sorted(changed.items())))
        return 0
    found, r = problems(), reach()
    n = len(r["read"]) + len(r["unread"])
    window = f"{r['days']} days" + (f" (the event log covers {r['covers']:g})" if r["covers"] < r["days"] else "")
    shown = [f"✗ {x}" for x in found[:SHOWN]] + ([f"✗ … and {len(found) - SHOWN} more (--json lists them all)"]
                                                 if len(found) > SHOWN else [])
    out(args, {"problems": found, "reach": r},
        "\n".join(shown) + ("\n" if shown else "")
        + f"{'✗' if found else '✓'} playbooks: {n} checked, {len(found)} problem(s)\n"
        + f"  unread in {window}: {len(r['unread'])} of {n}"
        + (": " + ", ".join(x[len('references/'):-3] for x in r["unread"][:12])
           + (f" and {len(r['unread']) - 12} more" if len(r["unread"]) > 12 else "") if r["unread"] else "")
        + variant_line())
    return 1 if found else 0


def variant_line():
    """T-2917: the small-model variants kept, by profile: the corpus's coverage ("" when there are none)."""
    from sampo import smallcards
    v = smallcards.coverage()["variants"]
    return ("\n  small-model variants kept: " + ", ".join(f"{k} {n}" for k, n in sorted(v.items()))) if v else ""


# T-1618: held-out briefs per playbook, scored with and without it loaded; the lift is the pass rate with minus without
EVALS = os.path.join(c.PLUGIN_ROOT, "evals", "playbooks")  # <stage>/<name>.json beside the playbook's own name
CHECKS = ("has", "re", "not")
NESTED = re.compile(r"[+*}]\)*[+*{]")  # (a+)+, ((a)*)*, a{2}{3}: a repeat of a repeat
SYSTEM = ("You are a senior engineer working a task in a code repository. Answer with your plan and the commands "
          "you would run; be concrete and brief.")


REVIEW_BY_EXT = {".py": "review/python", ".ts": "review/typescript", ".tsx": "review/typescript",
                 ".js": "review/typescript", ".jsx": "review/typescript", ".mjs": "review/typescript",
                 ".rs": "review/rust", ".c": "review/cpp", ".cc": "review/cpp", ".cpp": "review/cpp", ".h": "review/cpp",
                 ".hpp": "review/cpp"}  # T-1681: the language checklist a changed file calls for


def review_checklists(p, files):
    """T-1681: the review checklists a diff of `files` calls for (code-reviewer always, one per language changed), each
    as fm playbooks show gives it for project p (its {{facts}} filled in, its overlay after), under a name line."""
    names = ["review/code-reviewer"] + sorted({REVIEW_BY_EXT[e] for f in files
                                               if (e := os.path.splitext(f)[1].lower()) in REVIEW_BY_EXT})
    books, out = listed(), []
    for name in names:
        hits = resolve(name, books)
        if len(hits) == 1:
            out.append(f"<!-- {name} (fm playbooks show {name}) -->\n" + load(hits[0], ROOT, p).rstrip("\n"))
    return "\n\n".join(out) + "\n" if out else ""


def load_briefs(path):
    """[{task, checks}] from a brief-set file ({"briefs": [{"task": …, "checks": [{"has"|"re"|"not": text}]}]}):
    ValueError naming what's wrong."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError, RecursionError) as e:  # RecursionError: JSON nested past the stack
        raise ValueError(f"{path}: {e}")
    briefs = data.get("briefs") if isinstance(data, dict) else None
    if not isinstance(briefs, list) or not briefs:
        raise ValueError(f'{path}: needs {{"briefs": [{{"task": …, "checks": [{{"has": …}}]}}]}}')
    for i, b in enumerate(briefs, 1):
        if not (isinstance(b, dict) and isinstance(b.get("task"), str) and b["task"].strip()
                and isinstance(b.get("checks"), list) and b["checks"]):
            raise ValueError(f"{path}: brief {i} needs a task and a non-empty list of checks")
        for k in b["checks"]:
            if not (isinstance(k, dict) and len(k) == 1 and next(iter(k)) in CHECKS
                    and isinstance(next(iter(k.values())), str) and next(iter(k.values())).strip()):
                raise ValueError(f'{path}: brief {i} has a check that isn\'t {{"has"|"re"|"not": text}}: {k!r}')
            if "re" in k:
                # a brief file may come from a cloned repo: a long or nested-quantifier pattern could backtrack
                # without end, and Python's regex engine doesn't stop for ^C. ponytail: a heuristic, not a proof
                if len(k["re"]) > 200 or NESTED.search(k["re"]):
                    raise ValueError(f"{path}: brief {i}: check regex {k['re'][:60]!r} is too long or nests a "
                                     "quantifier ((a+)+): write it plainer")
                try:
                    re.compile(k["re"])
                except re.error as e:
                    raise ValueError(f"{path}: brief {i}: check regex {k['re']!r}: {e}")
    return briefs


def passes(answer, checks):
    """Every check holds: has/not are case-blind substrings, re a case-blind search."""
    low = answer.lower()
    for k in checks:
        (kind, v), = k.items()
        if not (v.lower() in low if kind == "has" else v.lower() not in low if kind == "not"
                else re.search(v, answer, re.I)):
            return False
    return True


def literal(task, book):
    """The scripted model: it answers with the task and, when loaded, the playbook's every line taken literally. A
    text-presence check: does the playbook say what each brief checks for? --real measures what a model does."""
    return task + ("\n" + book if book else "")


literal.name = "literal"


def local_profile(name):
    """T-2917: the own or loopback generative profile called `name` (an own one only for a non-sensitive project), else
    None: a cloud model name keeps the claude -p path."""
    from sampo import models, moonscale
    try:
        want = models.resolve(name).get("name")
        p = c.find_project(os.getcwd())
        return next((x for x in moonscale._generative(bool(p and c.read_meta(p).get("sensitive")))
                     if x.get("name") == want), None)
    except (models.ModelError, OSError, ValueError):
        return None


def real_model(model):
    """claude -p, tool-less, each call checked against and recorded in fm budget (fmideas.run_child); an own or
    loopback profile (T-2917) answers through models.complete instead, reasoning off so it can't eat the answer."""
    from sampo import ideas, models
    prof = local_profile(model)

    def ask(task, book):
        system = SYSTEM + (f"\n\nFollow this playbook:\n\n{book}" if book else "")
        if prof is None:
            return ideas.run_child("playbook-eval", system, task, model, 300, detail="fm playbooks eval")
        try:
            r = models.complete(prof, [{"role": "system", "content": system}, {"role": "user", "content": task}],
                                max_tokens=min(prof.get("max_out", 2048), 2048), timeout=300, think=False)
        except (models.ModelError, *models._TRANSPORT) as e:
            raise ValueError(f"{prof['name']}: {e}")
        return r.get("text") or ""
    ask.name = prof["name"] if prof else model
    return ask


def preflight_real(briefs, passes_each=2):
    """The whole run's estimate against today's budget before the first child: ValueError when it can't cover it."""
    from sampo import budget
    try:
        budget.check("playbook-eval", budget.estimate("playbook-eval", passes_each * len(briefs), 0.03),
                       "fewer briefs, or no --ablate")
    except budget.BudgetError as e:
        raise ValueError(str(e))


def evaluate(book, briefs, model, without=None):
    """{n, with, without, lift} for briefs answered by model(task, book or None); without: a count already known."""
    def ok(i, b, loaded):
        try:
            return bool(passes(model(b["task"], loaded), b["checks"]))
        except ValueError as e:  # a --real child failed: say which brief (its cost is already recorded)
            raise ValueError(f"brief {i} ({'with' if loaded else 'without'} the playbook): {e}")
    w = sum(ok(i, b, book) for i, b in enumerate(briefs, 1))
    wo = without if without is not None else sum(ok(i, b, None) for i, b in enumerate(briefs, 1))
    return {"n": len(briefs), "with": w, "without": wo, "lift": round((w - wo) / len(briefs), 4)}


def sections(book):
    """[(title, start, end)] of the playbook's top-level ## sections, outside code fences; the text above the first
    stays in every ablation (T-1619)."""
    fenced, marks = False, []
    for m in re.finditer(r"(?m)^(```|~~~)|^## +(.+?)\s*$", book):
        if m.group(1):
            fenced = not fenced
        elif not fenced:
            marks.append(m)
    return [(m.group(2), m.start(), marks[i + 1].start() if i + 1 < len(marks) else len(book))
            for i, m in enumerate(marks)]


def ablate(book, briefs, model, base=None):
    """Each section's share of the lift: the full lift minus the lift with that section dropped. A share of zero or
    less flags the section as a cut candidate (the ratchet keeps playbooks small). base: evaluate()'s result, reused."""
    base = base or evaluate(book, briefs, model)
    n, rows = len(briefs), []
    for title, a, b in sections(book):
        w = evaluate(book[:a] + book[b:], briefs, model, without=base["without"])["with"]
        share = round(base["lift"] - (w - base["without"]) / n, 4)
        rows.append({"section": title, "share": share, "cut": share <= 0})
    return rows


def cmd_eval(args):
    """fm playbooks eval NAME [--briefs FILE] [--real [--model M]] (T-1618)."""
    from sampo import cli
    hits = resolve(args.name, listed())
    if not hits and args.name.endswith(".md") and os.path.isfile(args.name):  # a draft outside the library
        name, book = os.path.abspath(args.name), _read(args.name)
        default = os.path.splitext(name)[0] + ".json"
        print(f"fm playbooks eval: reading {name} (not a library playbook)", file=sys.stderr)
    else:
        name = _one(args.name, listed())
        book = load(name)
        default = os.path.join(EVALS, name[len("references/"):-len(".md")] + ".json")
    path = args.briefs or default
    if not os.path.isfile(path):
        raise cli.UsageError(f"no held-out briefs for {name}: write {path} "
                               '({"briefs": [{"task": …, "checks": [{"has": …}]}]}) or pass --briefs FILE')
    try:
        briefs = load_briefs(path)
        mname = args.model or "haiku"
        if args.real and not local_profile(mname):  # a local profile costs no budget
            preflight_real(briefs, 2 + (len(sections(book)) if args.ablate else 0))
        model = real_model(mname) if args.real else literal
        r = evaluate(book, briefs, model)
        cuts = ablate(book, briefs, model, r) if args.ablate else None
    except ValueError as e:
        raise cli.UsageError(str(e))
    row = {"ts": c.now(), "playbook": name, "briefs": os.path.abspath(path), "model": model.name, **r,
           **({"ablate": cuts} if cuts is not None else {})}
    os.makedirs(c.state_dir(), exist_ok=True)
    c.append_line(os.path.join(c.state_dir(), "playbook-evals.jsonl"), json.dumps(row) + "\n")
    n = r["n"]
    cli.out(args, row, f"{name}: lift {r['lift']:+.2f} (with {r['with']}/{n}, without {r['without']}/{n}) on {n} "
              f"held-out brief(s), {model.name} model"
              + ("\n  literal: a text-presence check (does the shipped playbook say what each brief checks for?); "
                 "--real measures what a model does (budgeted)" if not args.real else "")
              + ("".join(f"\n  {x['section'][:40]:<40} {x['share']:+.2f}" + ("  ← cut candidate: no share alone (or another section repeats it)"
                                                                            if x["cut"] else "") for x in cuts)
                 if cuts else "\n  no ## sections to ablate" if cuts == [] else ""))
    return 0
