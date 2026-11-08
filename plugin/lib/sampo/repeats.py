"""fm repeats: what a project keeps doing by hand, from its briefs, and what to make of it.

Commands come from recorded evidence (what was really run), procedures from step texts. A command shape run 3+ times
in 2+ tasks, or a step that recurs in 3+ tasks, is a candidate for a project tool: a gate (`fm check add`), a script,
or a project skill (.claude/skills/<name>/SKILL.md) that Claude and its subagents load when the procedure applies.
Report-only; turning a candidate into a tool is an ordinary task.
"""
import hashlib
import json
import os
import re
import shlex

from sampo import core as c

MIN_RUNS, MIN_TASKS, MIN_STEP_TASKS = 3, 2, 3
_EVIDENCE_CMD = re.compile(r"^-\s+(?:\((?:step|ac)\s+\d+\)\s+)?`([^`]*)` →")
_RECORD = re.compile(r"git\s+(?:-C\s+\S+\s+)?(?:log|status|diff|show|rev-parse|branch)\b")
# a gate word as a whole word (letters around it end it, `_` and `.` don't), in a command's words or file names, not
# its directories: `bench_hooks.py` and `unittest` are gates, `dist/build/app.js` and `git checkout` aren't
_GATE = re.compile(r"(?<![a-z])(test|tests|unittest|pytest|jest|vitest|lint|eslint|ruff|mypy|tsc|check|build|fmt|"
                   r"vet|doctor|bench|clippy|roundtrip)(?![a-z])", re.I)
_SEQUENCE = re.compile(r"&&|;|\|")
# T-0857: a run of part of the suite — one test file or module, a name filter — is a step, never a gate for every task
_NARROW = re.compile(r"(^|\s)(-k|-p|-t|--filter|--grep|-run)(\s|=)|::|(^|[\s/.])test_\w+(\.py)?(\s|$)")


def shape(cmd):
    """A command's shape: a sequence (&&, ;, |) as a whole, otherwise its first three words; ids, numbers and quoted
    values normalized, so `unittest test_a -q` and `unittest discover -s tests` are the same habit."""
    cmd = re.sub(r"^(?:cd\s+\S+\s*&&\s*)?(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*", "", cmd.strip())
    cmd = re.sub(r"^(python[0-9.]*)((?:\s+-[WX]\s*\S+|\s+-[uBOqsSIE]+)+)", r"\1", cmd)  # interpreter flags
    cmd = re.sub(r"'[^']*'|\"[^\"]*\"", "'…'", re.sub(r"\bT-\d{4,}\b", "T-ID", cmd))
    if _SEQUENCE.search(cmd):
        return re.sub(r"\b\d+\b", "N", re.sub(r"\s+", " ", cmd))
    words = cmd.split()
    return " ".join(re.sub(r"^\d+$", "N", w) for w in words[:3]) + (" …" if len(words) > 3 else "")


def _step_key(text):
    return re.sub(r"\s+", " ", re.sub(r"\bT-\d{4,}\b|[`'\"]", "", text)).strip().rstrip(".:;!").lower()


def scan(p):
    """{"commands": [{shape, count, tasks, example, covered, suggest}], "steps": [{step, tasks, suggest}]}"""
    runs, steps = {}, {}
    for b in c.load_briefs(p, include_archive=True):
        for line in b.evidence():
            m = _EVIDENCE_CMD.match(line)
            if not m or m.group(1).split()[:1] in ([], ["fm"], ["sampo"]) or m.group(1).startswith(("fm-", "fm check:", "sampo check:")) \
                    or _RECORD.match(m.group(1)):
                continue  # Foreman's own commands, reviewer notes and git reads record state; they aren't habits
            sh = shape(m.group(1))
            if not sh:
                continue
            r = runs.setdefault(sh, {"count": 0, "tasks": set(), "example": m.group(1)})
            if _NARROW.search(r["example"]) and not _NARROW.search(m.group(1)):
                r["example"] = m.group(1)  # T-0857: the whole-suite run speaks for the habit
            r["count"] += 1
            r["tasks"].add(b.id)
        for s in b.steps():
            steps.setdefault(_step_key(s.text), set()).add(b.id)
    meta = c.read_meta(p)
    gates = {shape(x) for x in meta.get("checks") or [] if isinstance(x, str)}
    keys = [k for k in meta.get("repeats_dismissed") or [] if isinstance(k, str)]
    dismissed = set(keys) | {_step_key(k) for k in keys}
    commands = []
    for sh, r in runs.items():
        if r["count"] < MIN_RUNS or len(r["tasks"]) < MIN_TASKS or sh in dismissed:
            continue
        covered = sh in gates
        commands.append({"shape": sh, "count": r["count"], "tasks": len(r["tasks"]), "example": r["example"],
                         "covered": covered, "suggest": "covered by fm check" if covered else suggest(sh, r["example"])})
    procedures = [{"step": k, "tasks": len(ids), "suggest": "a project skill (.claude/skills/<name>/SKILL.md) holding "
                   "the procedure, so every session and subagent follows it the same way"}
                  for k, ids in steps.items() if k and len(ids) >= MIN_STEP_TASKS and k not in dismissed]
    return {"commands": sorted(commands, key=lambda x: (-x["count"], x["shape"])),
            "steps": sorted(procedures, key=lambda x: (-x["tasks"], x["step"]))}


def suggest(sh, example):
    if _SEQUENCE.search(sh):
        return "a project script (one command for the sequence); fm check add it if it's a gate"
    if any(_GATE.search(os.path.basename(w)) for w in sh.split()):
        if _NARROW.search(example):
            return "a project script or alias (it runs part of the suite; the whole-suite command is the gate)"
        return f"fm check add \"{example}\" (a gate for every task)"
    return "a project script or alias"


def dismiss(p, key):
    """Stop reporting a shape or step: it became a project tool, or isn't worth one."""
    key = key.strip()
    with c.lock(p.dir):
        meta = c.read_meta(p)
        meta["repeats_dismissed"] = sorted(set(meta.get("repeats_dismissed") or []) | {key})
        c.write_meta(p, meta)
        c.log_event(p, "repeats_dismissed", data={"key": key[:200]})


SEQ_MIN, SEQ_MAX = 3, 6  # calls in a routine
SEQ_RUNS, SEQ_SESSIONS = 2, 2  # runs of one routine in a session, in this many sessions, before it is a routine
SEQ_DAYS_MAX = 3650  # --days: a window longer than this is a typo, and a huge one would overflow the date math
SEQ_DAYS, SEQ_TOP = 7, 20  # the window (sampo repeats --days), and the routines listed
SEQ_CALL_CAP, SEQ_LEDGER_CAP = 100000, 20000  # ponytail: calls read per run (a GB-a-week transcript folder would not fit)
_PATH = re.compile(r"(?<![\w.-])(?:~|\.{1,2})?/[\w./@+-]*|\b\w[\w.-]*/[\w./@+-]+")
_HEXID = re.compile(r"\b[0-9a-f]{7,40}\b")
_GIT_C = re.compile(r"\bgit\s+-C\s+\S+\s*")  # where git runs, not what it does: `git -C /r status` is `git status`
_BASIC = re.compile(r"(\s(?:-u|--user)(?:\s+|=))\S+")  # curl -u user:password: c.redact doesn't know this form


def _path_tok(m):
    """A path as a placeholder, a script's basename kept (`scripts/a.py` and `tools/b.py` are different calls)."""
    base = m.group(0).rsplit("/", 1)[-1]
    return base if re.fullmatch(r"[\w-]+\.\w+", base) else "PATH"


_CHAIN = re.compile(r"\s*(?:&&|\|\||;|\||\n)\s*")  # a chain's links
_SETUP = re.compile(r"^(?:cd\s|export\s|[A-Za-z_]\w*=\S*(?:\s|$))")  # where the shell is, not what it runs
_SAMPO = re.compile(r"^(?:python3?\s+\S*/)?(?:sampo|fm)\b")  # `python3 plugin/bin/sampo x` and `fm x` are the sampo verb `x`


def _norm_cmd(cmd):
    """T-3921: a call is the first thing it runs after its setup (`cd x; S=y; uptime | head` is one uptime), shaped as
    `sampo repeats` shapes a habit, then its paths and hex ids as placeholders (after the shape, so the words it keeps
    still tell the subcommands apart). shape() keeps a chain whole: right for a habit, too narrow for a routine."""
    links = [s for s in _CHAIN.split(_GIT_C.sub("git ", cmd).strip()) if s and not _SETUP.match(s)]
    head = _SAMPO.sub("sampo", links[0]) if links else ""
    return _PATH.sub(_path_tok, shape(_HEXID.sub("HEX", head))) or "Bash"


def _call(name, inp):
    """(token, example, specific) of one tool call: a Bash command by its shape, any other tool by its name (the file
    or the pattern it touched is the example's, not the routine's). Secrets are redacted before anything is counted
    or shown; specific: a Bash command or an MCP tool, the calls that say what was done (not Read, Edit or Grep)."""
    inp = inp if isinstance(inp, dict) else {}
    if name == "Bash":
        raw = _BASIC.sub(r"\1[REDACTED]", c.redact(str(inp.get("command") or "")))
        return _norm_cmd(raw), c.redact_local(raw), True
    if name == "Agent":  # a subagent launch: the type says what was run (`sampo lane brief` → Agent fm-builder is a routine)
        kind = str(inp.get("subagent_type") or "general")
        return f"Agent {kind}", c.redact_local(c.redact(f"Agent {kind}")), True
    arg = next((str(inp[k]) for k in ("file_path", "path", "pattern", "url", "description") if inp.get(k)), "")
    return name, c.redact_local(c.redact(f"{name} {arg}".strip())), name.startswith("mcp__")


def _folders(p):
    """T-3921 review (T-3938): a project's sessions run in its worktrees and lanes, each with its own transcript folder
    (`<root>--claude-worktrees-…`, `<root>-lanes-…`), so the walk reads the main checkout's family of folders."""
    from sampo import cost
    base = cost.transcripts_dir(c.main_worktree(p.root) or p.root)
    parent, name = os.path.split(base)
    try:
        names = sorted(os.listdir(parent))
    except OSError:
        return [base]
    return [os.path.join(parent, n) for n in names
            if n == name or n.startswith((name + "--claude-worktrees-", name + "-lanes-"))]


def _units(p, since):
    """({unit: (session id, [(token, example, specific)])}, truncated): each transcript file in the project's folder
    (a main session, or one subagent's run in it), newest first, plus the event log's evidence for the sessions no
    transcript holds (T-3921). truncated: the reads stopped at a cap, so older runs are not counted."""
    from sampo import cost  # the transcript walk fm cost uses: subagents included, files older than the window skipped
    units, n, truncated = {}, 0, False
    for path, e in cost._entries(_folders(p), since, need='"tool_use"', newest=True):
        content = (e.get("message") or {}).get("content") if isinstance(e.get("message"), dict) else None
        sid = str(e.get("sessionId") or "")
        for x in content if isinstance(content, list) and sid else []:
            if isinstance(x, dict) and x.get("type") == "tool_use":
                units.setdefault(("transcript", path), (sid, []))[1].append(_call(str(x.get("name") or "?"), x.get("input")))
                n += 1
        if n > SEQ_CALL_CAP:
            truncated = True
            break
    have = {sid for sid, _ in units.values()}
    ledger = c.ledger_tail(p, SEQ_LEDGER_CAP, kinds=["evidence"])
    truncated = truncated or len(ledger) >= SEQ_LEDGER_CAP
    for e in ledger:
        data, sid = e.get("data"), str(e.get("session_id") or "")
        cmd = data.get("cmd") if isinstance(data, dict) else None
        if sid and sid not in have and isinstance(cmd, str) and str(e.get("ts") or "") >= since:
            units.setdefault(("ledger", sid), (sid, []))[1].append(_call("Bash", {"command": cmd}))
    return units, truncated


def _inside(g, h):
    n = len(g)
    return any(h[i:i + n] == g for i in range(len(h) - n + 1))


def sequences(p, days=SEQ_DAYS):
    """T-3921: the routines the sessions run by hand, a run of SEQ_MIN-SEQ_MAX calls that repeats: {"sequences":
    [{calls, count, sessions, saved, example}], "days"}, ranked by calls saved (count x (calls - 1)). A routine that
    is part of a longer one that runs as often is that routine's, so it is not listed twice."""
    since = c.iso(c.clock().timestamp() - days * 86400)
    units, truncated = _units(p, since)
    runs = {}
    for unit, (_, calls) in units.items():  # a unit is one run: a main session's file, a subagent's file, a ledger session
        toks = [t for t, _, _ in calls]
        for n in range(SEQ_MIN, min(SEQ_MAX, len(toks)) + 1):
            for i in range(len(toks) - n + 1):
                g = tuple(toks[i:i + n])
                if len(set(g)) < 2:  # one call again and again is a loop, not a routine
                    continue
                if len({t for t, _, s in calls[i:i + n] if s}) < 2:  # Read -> Edit -> Read is every session's noise
                    continue
                r = runs.get(g)
                if r is None:
                    r = runs[g] = {"per": {}, "example": [x for _, x, _ in calls[i:i + n]]}
                r["per"][unit] = r["per"].get(unit, 0) + 1
    cand = []
    for g, r in runs.items():  # SEQ_RUNS+ runs in SEQ_SESSIONS+ sessions: a routine each session runs once still repeats
        if sum(r["per"].values()) >= SEQ_RUNS and len(r["per"]) >= SEQ_SESSIONS:
            cand.append((g, sum(r["per"].values()), len(r["per"]), r["example"]))
    cand.sort(key=lambda x: (-x[1] * (len(x[0]) - 1), -x[1], x[0]))
    kept = []
    for g, count, nses, ex in cand:  # a longer routine that runs as often comes first (more calls saved), so it covers its parts
        if any(h[1] == count and _inside(g, h[0]) for h in kept):
            continue
        kept.append((g, count, nses, ex))
        if len(kept) == SEQ_TOP:
            break
    return {"sequences": [{"calls": [c.plain(c.redact_local(t)) for t in g], "count": count, "sessions": nses,
                           "saved": count * (len(g) - 1),
                           "example": [c.plain(c.redact_local(c.redact(x.replace("\n", " ")))) for x in ex]}
                          for g, count, nses, ex in kept], "days": days, "truncated": truncated}


def sequence_lines(r):
    if not r["sequences"]:
        lines = [f"No routine repeats in {SEQ_SESSIONS}+ sessions yet ({SEQ_MIN}-{SEQ_MAX} calls, {SEQ_RUNS}+ runs "
                 f"each, the last {r['days']:g} day(s))."]
    else:
        lines = [f"Routines ({SEQ_MIN}-{SEQ_MAX} calls, {SEQ_RUNS}+ runs in {SEQ_SESSIONS}+ sessions, the last "
                 f"{r['days']:g} day(s); by calls saved):"]
    for x in r["sequences"]:
        lines.append(f"  {x['count']}× in {x['sessions']} sessions  saves {x['saved']} calls  "
                     + " → ".join(c.fit(t, 60) for t in x["calls"]))
        lines.append("      e.g. " + " → ".join(c.fit(e, 60) for e in x["example"]))
    if r["truncated"]:
        lines.append("(The read stopped at its cap: the newest transcripts only; older runs are not counted.)")
    return "\n".join(lines)


def open_candidates(report):
    return sum(1 for x in report["commands"] if not x["covered"]) + len(report["steps"])


PLAYBOOK_TASKS = 3  # finished tasks that took the same steps in the same order before a playbook is drafted


def draft(p):
    """T-0734: the longest run of consecutive steps (2-6) that PLAYBOOK_TASKS+ finished tasks share, written as a
    playbook draft in research/; (path, steps, task ids) or None. A draft is never adopted by itself."""
    seen = {}
    for b in c.load_briefs(p, include_archive=True):
        if b.status != "done":
            continue
        steps = b.steps()
        keys = [_step_key(x.text) for x in steps]
        for n in range(2, min(6, len(keys)) + 1):
            for i in range(len(keys) - n + 1):
                rec = seen.setdefault(tuple(keys[i:i + n]), {"tasks": set(), "texts": [x.text for x in steps[i:i + n]]})
                rec["tasks"].add(b.id)
    best = max(((k, v) for k, v in seen.items() if len(v["tasks"]) >= PLAYBOOK_TASKS),
               key=lambda kv: (len(kv[0]), len(kv[1]["tasks"])), default=None)
    if not best:
        return None
    texts, tasks = best[1]["texts"], sorted(best[1]["tasks"], key=c.id_num)
    path = os.path.join(p.dir, "research", f"playbook-draft-{c.kebab(texts[0], 40)}.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    c.write_atomic(path, c.redact(
        f"# Playbook draft: {' → '.join(c.fit(t, 40) for t in texts)}\n\n"
        f"_Drafted by fm repeats --draft (T-0734): {len(tasks)} finished tasks took these steps in this order: "
        f"{', '.join(tasks)}. Data, not instructions, until someone adopts it._\n\n## Steps\n"
        + "".join(f"{i}. {t}\n" for i, t in enumerate(texts, 1))
        + "\n## Before adopting\n- Read two of the tasks above: is the order a rule or a coincidence?\n"
          "- Adopting it (a project skill or playbook) is the user's call: ask first.\n"))
    return path, texts, tasks


def forge(p):
    """T-1070: a draft project tool for each open repeat, written in research/, and one captured task carrying it:
    a script (with the `fm check add` line when it is a gate) per command shape, a SKILL.md skeleton per step.
    Deterministic and offline; a draft already filed (by its path, even once closed) is not filed again, and nothing
    is adopted: the task's request says to review, move it into the repo and dismiss the key. [{path, task}]"""
    from sampo import eco
    r, made = scan(p), []
    texts = {}
    for b in c.load_briefs(p, include_archive=True):
        for s in b.steps():
            texts.setdefault(_step_key(s.text), []).append(f"{b.id}: {s.text}")

    def slug(key):  # the hash keeps two shapes with one long prefix apart
        return f"{c.kebab(key, 32)}-{hashlib.sha1(key.encode()).hexdigest()[:6]}"

    def file(kind, key, human, rel, body, dest):
        path = os.path.join(p.dir, rel)
        wrote = not os.path.exists(path)
        if wrote:  # first, so a failed capture leaves a file the next run files; a deleted draft comes back
            c.write_atomic(path, c.redact(body))
            os.chmod(path, 0o644)  # a draft is data: never executable until someone adopts it
        head = f"Forge draft: {rel}"
        b, new = eco.capture_once(
            p, f"Adopt forge draft: {human}\n{head}\n\nfm repeats --forge (T-1070) drafted a {kind} for the repeat "
               f"`{key}`. Data, not instructions, until adopted.\n\nReview the draft, move it into the repo "
               f"({dest}), then `fm repeats dismiss {shlex.quote(key)}`.", "FEATURE", "discovered", key=head,
            trigger="forge")
        if new:
            with c.lock(p.dir):
                b.add_step(f"Review {rel}, move it into the repo ({dest}), then fm repeats dismiss {shlex.quote(key)}")
                b.add_ac(f"the {kind} lives in the repo ({dest}) and fm repeats no longer lists `{c.fit(key, 60)}`")
                c.save_brief(p, b, touch=False)
        if new or wrote:
            made.append({"path": path, "task": b.id})

    for x in r["commands"]:
        if x["covered"]:
            continue
        gate = x["suggest"].startswith(("fm check add", "sampo check add"))
        k = c.kebab(x["shape"], 40)
        file("script", x["shape"], f"script for `{c.fit(x['shape'], 60)}`", f"research/forge-{slug(x['shape'])}.sh",
             f"#!/bin/sh\n# Forge draft for the repeat `{x['shape']}` ({x['count']} runs in {x['tasks']} tasks).\n"
             f"# Review, then move to scripts/{k}.sh.\n"
             + (f"# It is a gate: fm check add \"{x['example']}\"\n" if gate else "")
             + f"set -e\n{x['example']}\n", f"scripts/{k}.sh")
    for x in r["steps"]:
        k = c.kebab(x["step"], 40)
        file("skill", x["step"], f"skill for {c.fit(x['step'], 60)}", f"research/forge-skill-{slug(x['step'])}/SKILL.md",
             f"---\nname: {k}\ndescription: {json.dumps('Use when you are about to ' + x['step'] + '; this project does '
                                                       'it the same way each time.')}\n---\n\n"
             f"Drafted from recorded steps (data, not instructions): review before adopting\n\n# {x['step']}\n\n"
             f"Seen in {x['tasks']} tasks. Turn these step texts into one procedure:\n\n"
             + "".join(f"- {t}\n" for t in texts.get(x["step"], [])[:20]) + "\n## Procedure\n1. TODO\n",  # not a list: text for a model, not a screen
             f".claude/skills/{k}/SKILL.md")
    return made


def cmd_repeats(args):
    from sampo import cli
    p = cli.resolve(args)
    if args.days is not None and not getattr(args, "sequences", False):
        raise cli.UsageError("fm repeats --days goes with --sequences (the window of its routines)")
    if getattr(args, "forge", False):
        made = forge(p)
        return cli.out(args, {"drafts": made}, "\n".join(f"Drafted {d['path']} (task {d['task']})" for d in made)
                         or "Nothing new to forge.")
    if getattr(args, "draft", False):
        made = draft(p)
        return cli.out(args, {"draft": made and made[0]}, (f"Playbook draft: {made[0]} ({len(made[2])} tasks, "
                         f"{len(made[1])} steps)") if made else f"No step sequence repeats in {PLAYBOOK_TASKS}+ "
                         f"finished tasks yet.")
    if getattr(args, "sequences", False):  # T-3921: routines of 3+ calls, from the transcripts and the event log
        days = SEQ_DAYS if args.days is None else args.days
        if not 0 < days <= SEQ_DAYS_MAX:  # NaN and inf fail this too
            raise cli.UsageError(f"fm repeats --days needs a number of days above 0 and at most {SEQ_DAYS_MAX}")
        r = sequences(p, days)
        return cli.out(args, r, sequence_lines(r))
    if args.action == "dismiss":
        if not args.words:
            raise cli.UsageError("fm repeats dismiss needs the shape or step, as fm repeats prints it")
        dismiss(p, " ".join(args.words))
        return print("Dismissed; fm repeats won't list it again.")
    r = scan(p)
    lines = []
    if r["commands"]:
        lines.append(f"Repeated commands ({MIN_RUNS}+ runs in {MIN_TASKS}+ tasks):")
        lines += [f"  {x['count']}× in {x['tasks']} tasks  {x['shape']}  → {x['suggest']}" for x in r["commands"]]
    if r["steps"]:
        lines.append(f"Repeated steps (in {MIN_STEP_TASKS}+ tasks):")
        lines += [f"  {x['tasks']} tasks  {x['step']}  → {x['suggest']}" for x in r["steps"]]
    n = len(c.read_meta(p).get("repeats_dismissed") or [])
    cli.out(args, r, ("\n".join(lines) or f"Nothing repeated yet (commands {MIN_RUNS}+ times in {MIN_TASKS}+ tasks, "
                                              f"steps in {MIN_STEP_TASKS}+ tasks).") + (f" ({n} dismissed)" if n else ""))
