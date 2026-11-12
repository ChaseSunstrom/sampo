"""T-4221 (big tiers part B, T-4219): a big task (tier XL, HUGE or GIANT) cannot start until its brief holds a full plan
whose structure code checks, and one verb turns that plan into child tasks.

The plan is three sections of the brief: Design (the interfaces and decisions every child relies on), Breakdown (one
block per child) and Integration (the criteria that hold once the children are in). The Breakdown is plain lines, no
JSON, so a person, a small model and a diff all read it:

    [c1] Title of the child
    type: FEATURE            # the parent's type when left out
    tier: M                  # S or M; a GIANT's milestone is XL or HUGE and gets `outcome:` instead of the rest
    files: lib/a.py, tests/test_a.py
    after: c0, c2            # keys that must land first
    covers: 1, 2             # numbers of the parent's criteria this child delivers
    criterion: what is true when it is done :: a command that fails when it is not
    step: what to do first

sampo big plan ID writes the empty skeleton, sampo big check ID reports what is wrong and how to fix it, sampo big expand
ID makes the children (split's child path, idempotent). core.plan_gaps asks gate_gaps, so focus, a lane and the next
action all refuse a big task whose plan has problems. Nothing here calls a model: the plan is checked by code (models
advise, proof decides)."""
import fnmatch
import functools
import hashlib
import json
import posixpath
import re

from sampo import core as c

WINDOW = {"XL": (3, 8), "HUGE": (9, 25)}  # the children a tier holds; GIANT: 2+ milestones, no top
LEAF_TIERS = ("S", "M")
MILESTONE_TIERS = ("XL", "HUGE")
MAX_BLOCKS = 200
SHOWN = 3  #the gate names this many problems, then counts the rest
SECTIONS = ("Design", "Breakdown", "Integration")
KEYS = {"type", "tier", "files", "after", "covers", "criterion", "step", "outcome"}
MILESTONE_KEYS = {"tier", "after", "covers", "outcome"}
INTEGRATION_KEYS = {"criterion", "covers"}
_HEAD = re.compile(r"^\[([A-Za-z][\w-]*)\]\s+(\S.*)$")
_LINE = re.compile(r"^([A-Za-z]+):\s*(.*)$")
_CHILDREN = re.compile(r"^(\S+) → (T-\d+)\b.*?\(block ([0-9a-f]{8})\)\s*$")
_INHERIT = re.compile(r"^(?:CONTEXT|MUST|NEVER|CONSTRAINT):", re.I)
_COMMENT = re.compile(r"(?:^|\s)#.*$")  # an inline `# note` on a type, tier, files, after or covers line
_BAD_FILE = re.compile(r"[()]|^/|\.\.")


def _norm(path):
    n = posixpath.normpath(re.sub(r"^(?:\./)+", "", path))
    return n[2:] if n.startswith("./") else n


def _clash(a, b):
    """True when two `files` entries can name the same file: equal after normalising, one a directory holding the
    other, or a glob matching the other. Case is never folded."""
    a, b = _norm(a), _norm(b)
    if a == b or a == "." or b == "." or b.startswith(a + "/") or a.startswith(b + "/"):
        return True
    return fnmatch.fnmatchcase(b, a) or fnmatch.fnmatchcase(a, b)


def big_tiers():
    return tuple(getattr(c, "BIG_TIERS", ("XL", "HUGE", "GIANT")))  # part A owns the literals


def is_big(b):
    return b.tier in big_tiers()


def _problem(block, problem, fix):
    return {"block": block, "problem": problem, "fix": fix}


def _words(text):
    return [w for w in re.split(r"[,\s]+", text or "") if w]


def _numbers(text, block, problems):
    out = []
    for w in _words(text):
        m = re.fullmatch(r"(\d{1,3})(?:-(\d{1,3}))?", w)
        if not m:
            problems.append(_problem(block, f"covers {w!r} is not a criterion number",
                                     "write the numbers of the parent's criteria: covers: 1, 2 or 1-3"))
            continue
        lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
        out += range(lo, min(max(hi, lo), lo + 200) + 1)
    return out


def parse_blocks(text, integration=False):
    """([block dicts], [problems]) from a Breakdown (or, integration=True, an Integration) section. Blank lines and
    lines starting with # are skipped; any other line that isn't `key: value` under a [key] Title head is a problem."""
    blocks, problems, cur, seen = [], [], None, set()
    if integration:
        cur = {"key": "Integration", "title": "", "criteria": [], "covers": [], "line": 0}
        blocks.append(cur)
    for n, raw in enumerate((text or "").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if m := (None if integration else _HEAD.match(line)):
            key = m.group(1)
            if len(blocks) >= MAX_BLOCKS:  # the wave and cycle walks are quadratic: a plan this long is split, not checked
                problems.append(_problem("Breakdown", f"more than {MAX_BLOCKS} blocks (line {n})", "a plan this size is "
                                         "several GIANT milestones, each with its own plan"))
                break
            if key in seen:
                problems.append(_problem(key, f"the key {key} is used twice (line {n})", "give each child its own key"))
            seen.add(key)
            cur = {"key": key, "title": c.plain(m.group(2)).strip(), "type": "", "tier": "", "files": [], "after": [],
                   "covers": [], "criteria": [], "steps": [], "outcome": "", "line": n}
            blocks.append(cur)
            continue
        m = _LINE.match(line)
        where = cur["key"] if cur else "Breakdown"
        allowed = INTEGRATION_KEYS if integration else KEYS
        if not cur:
            problems.append(_problem(where, f"line {n} sits outside any block: {c.fit(line, 60)!r}",
                                     "start each child with [key] Title, e.g. [c1] Parser"))
        elif not m or m.group(1).lower() not in allowed:
            problems.append(_problem(where, f"line {n} isn't understood: {c.fit(line, 60)!r}",
                                     "use only " + ", ".join(sorted(allowed)) + " lines, one 'key: value' each"))
        else:
            k, v = m.group(1).lower(), m.group(2).strip()
            if k in ("type", "tier", "files", "after", "covers"):  # a criterion's command or a step may hold ' #'
                v = _COMMENT.sub("", v).strip()
            if k == "criterion":
                cur["criteria"].append(c.split_verify(v))
            elif k == "step":
                cur["steps"].append(v)
            elif k == "covers":
                cur["covers"] += _numbers(v, where, problems)
            elif k in ("files", "after"):
                cur[k] += _words(v)
                for w in (_words(v) if k == "files" else []):
                    if _BAD_FILE.search(w):
                        problems.append(_problem(where, f"files token {w!r} is not a path inside the repo",
                                                 "write a relative path or glob without parentheses, a leading / or .."))
            elif k in ("tier", "type"):
                cur[k] = v.upper()
            else:
                cur[k] = v
    return blocks, problems


def _empty(text):
    return not any(l.strip() and not l.strip().startswith("#") for l in (text or "").splitlines())


def waves(blocks):
    """([[keys of one wave]], [keys left in a cycle]): a block's wave is one past the highest wave of its `after`."""
    keys = {x["key"] for x in blocks}
    deps = {b["key"]: [a for a in b["after"] if a in keys and a != b["key"]] for b in blocks}
    level, left = {}, list(deps)
    while left:
        ready = [k for k in left if all(d in level for d in deps[k])]
        if not ready:
            break
        for k in ready:
            level[k] = 1 + max((level[d] for d in deps[k]), default=-1)
        left = [k for k in left if k not in level]
    out = []
    for k in deps:
        if k in level:
            while len(out) <= level[k]:
                out.append([])
            out[level[k]].append(k)
    return out, left


def _cycle(blocks, left):
    """One cycle among the keys left over, as 'c1 → c3 → c1'."""
    deps = {b["key"]: [a for a in b["after"] if a in left] for b in blocks if b["key"] in left}
    start = left[0]
    path, at = [start], start
    while True:
        at = next((d for d in deps.get(at, []) if d in left), None)
        if at is None:
            return " → ".join(left)
        if at in path:
            return " → ".join(path[path.index(at):] + [at])
        path.append(at)


def _shared(blocks):
    """A problem per pair of children that own overlapping files with no `after` path between them (either way, through
    any number of others): lanes order by depends_on alone, so such a pair can run at once whatever its wave."""
    deps = {x["key"]: {a for a in x["after"] if a != x["key"]} for x in blocks}
    below = {}

    def reach(k):  # every key k waits on, however far round (the graph has no cycle here)
        if k not in below:
            below[k] = set()
            for d in deps.get(k, ()):
                below[k] |= {d} | reach(d)
        return below[k]
    out = []
    for i, x in enumerate(blocks):
        for y in blocks[i + 1:]:
            if x["key"] in reach(y["key"]) or y["key"] in reach(x["key"]):  # ordered: no file clash to look for
                continue
            hit = next(((f, g) for f in x["files"] for g in y["files"] if _clash(f, g)), None)
            if hit:
                f, g = hit
                what = _norm(f) if _norm(f) == _norm(g) else f"{f} ({g} in {y['key']})"
                out.append(_problem(x["key"], f"shares {what} with {y['key']}: neither waits on the other, so they could "
                                    f"run at once", f"order them (after: {x['key']} on {y['key']}) or give the file to one of them"
                                    f", or split the file"))
    return out


def _fit_tier(n):
    return "L" if n < WINDOW["XL"][0] else "XL" if n <= WINDOW["XL"][1] else "HUGE" if n <= WINDOW["HUGE"][1] else "GIANT"


def analyse(b, root="."):
    """The plan's structure as {"ok", "problems", "warnings", "waves", "blocks", "giant"}; each problem names the
    block and the fix. Read-only: b is the parent's brief."""
    giant, tier = b.tier == "GIANT", b.tier
    problems, warns = [], []
    design, bd, integ = (b.section(s) for s in SECTIONS)
    if _empty(design):
        problems.append(_problem("Design", "the Design section is empty", f"write the interfaces and decisions every child "
                                 f"relies on: sampo big plan {b.id} shows the shape"))
    blocks, perr = parse_blocks(bd)
    problems += perr
    if not blocks and not perr:
        problems.append(_problem("Breakdown", "the Breakdown has no blocks", f"one [key] Title block per child: sampo big "
                                 f"plan {b.id} shows the shape"))
    ib, ierr = parse_blocks(integ, integration=True)
    problems += ierr
    icrit = ib[0]["criteria"] if ib else []
    if _empty(integ):
        problems.append(_problem("Integration", "the Integration section is empty", "add 'criterion: what holds once every "
                                 "child is in :: a command that can fail'"))
    elif not icrit:
        problems.append(_problem("Integration", "no Integration criterion", "add 'criterion: what holds once every child is "
                                 "in :: a command that can fail'"))
    for text, verify in icrit:
        problems += _weak("Integration", text, verify, root)
    keys = {x["key"] for x in blocks}
    for x in blocks:
        k = x["key"]
        for a in x["after"]:
            if a == k:
                problems.append(_problem(k, "it waits on itself", f"drop {a} from its after line"))
            elif a not in keys:
                problems.append(_problem(k, f"after names {a}, which is no block", f"use a key from the Breakdown "
                                         f"({', '.join(sorted(keys))}) or drop it"))
        if giant:
            if x["tier"] not in MILESTONE_TIERS:
                problems.append(_problem(k, f"tier {x['tier'] or 'unset'} is not XL or HUGE", "a GIANT's milestone is an "
                                         "XL or HUGE task with its own plan: write 'tier: XL' or 'tier: HUGE'"))
            if not x["outcome"]:
                problems.append(_problem(k, "the milestone has no outcome", "add 'outcome: what is true when it lands'"))
            if x["files"] or x["criteria"] or x["steps"] or x["type"]:
                problems.append(_problem(k, "a milestone takes a title, tier, outcome, after and covers only", "its own "
                                         "plan (sampo big plan) holds the files, criteria and steps"))
            continue
        if x["outcome"]:
            problems.append(_problem(k, "outcome belongs to a GIANT's milestone", "write criterion lines instead"))
        if x["type"] and x["type"] not in c.TYPES:
            problems.append(_problem(k, f"type {x['type']} is none of {', '.join(c.TYPES)}", "use one of those or leave it out"))
        if x["tier"] in LEAF_TIERS:
            pass
        elif c.at_least(x["tier"], "L"):
            problems.append(_problem(k, f"tier {x['tier']} is above M: a child must be S or M", f"split {k} into smaller "
                                     "blocks, or move it up to the plan's own tier"))
        else:
            problems.append(_problem(k, f"tier {x['tier'] or 'unset'} is not S or M", "write 'tier: S' or 'tier: M'"))
        if not x["files"]:
            problems.append(_problem(k, "it owns no files", "add 'files: path, path': what it may change"))
        if not x["criteria"]:
            problems.append(_problem(k, "no criterion", "add 'criterion: what is true when done :: a command that can fail'"))
        for text, verify in x["criteria"]:
            problems += _weak(k, text, verify, root)
    wv, left = waves(blocks)
    if left:
        problems.append(_problem(left[0], f"cycle: {_cycle(blocks, left)}", "no block may wait, however far round, on itself: "
                                 "drop one of those after links"))
    elif not giant:
        problems += _shared(blocks)
    n = len(b.acceptance())
    covered = {m for x in blocks for m in x["covers"]} | {m for x in ib for m in x["covers"]}
    for x in blocks + ib:
        for m in x["covers"]:
            if not 1 <= m <= n:
                problems.append(_problem(x["key"], f"covers {m}, but {b.id} has {n} criteria", "name a criterion number "
                                         "that exists"))
    for a in b.acceptance():
        if a.n not in covered:
            problems.append(_problem("Breakdown", f"criterion {a.n} ({c.fit(c.strip_verify(a.text), 50)}) is covered by no "
                                     f"child and not by Integration", f"add 'covers: {a.n}' to the child that delivers it, "
                                     "or to the Integration section"))
    if blocks and not perr:
        lo, hi = WINDOW.get(tier, (2, None))
        if len(blocks) < lo or (hi and len(blocks) > hi):
            fit = _fit_tier(len(blocks)) if not giant else "L"
            warns.append(_problem("Breakdown", f"{len(blocks)} {'milestones' if giant else 'children'}, outside {tier}'s "
                                  f"{lo}..{hi or 'many'}", f"that many is a {fit} task's size: re-tier the task, or "
                                  f"{'split' if len(blocks) > (hi or 0) else 'merge'} blocks"))
    return {"ok": not problems, "problems": problems, "warnings": warns, "waves": wv, "blocks": len(blocks),
            "giant": giant}


def _weak(block, text, verify, root):
    if not verify:
        return [_problem(block, f"criterion {c.fit(text, 40)!r} has no verify command", "end it with ' :: <a command that "
                         "fails when the criterion doesn't hold>'")]
    return [_problem(block, f"criterion {c.fit(text, 40)!r}: {w}", "use a command that can fail")
            for w in _weakness(verify, str(root)) if "can't fail" in w]


@functools.lru_cache(maxsize=512)
def _weakness(verify, root):
    """lint_verify's verdict, once per command and root in a process: plan_gaps asks on every stage() of a big task."""
    return tuple(c.lint_verify(verify, root))


def gate_gaps(b, root="."):
    """core.plan_gaps's entry: [] for a task that isn't big or was started; else one gap that names the first problems."""
    if not is_big(b) or c._started(b):
        return []
    got = analyse(b, root)["problems"]
    if not got:
        return []
    shown = "; ".join(f"{x['block']}: {x['problem']}" for x in got[:SHOWN])
    more = f" (+{len(got) - SHOWN} more)" if len(got) > SHOWN else ""
    return [f"a full plan ({shown}{more}; sampo big check {b.id} says how to fix each)"]


def next_line(b):
    """The next action for a big task whose plan has problems ('' when it has none)."""
    if not gate_gaps(b):
        return ""
    if all(_empty(b.section(s)) for s in SECTIONS):
        return f"{b.id}: write the full plan: sampo big plan {b.id}, fill Design, Breakdown and Integration, then sampo big check {b.id}"
    return f"{b.id}: finish the full plan: sampo big check {b.id} lists each problem and its fix, then sampo big expand {b.id}"


# ---------------------------------------------------------------- the skeleton

GUIDE_DESIGN = """# The interfaces and decisions every child relies on, one per line, so the children agree before any starts.
# interface: parse(text) -> list of blocks, in lib/parser.py
# decision: a block with no files is refused
# A child's Interpretation quotes the lines that name its files.
"""
GUIDE_BREAKDOWN = """# One block per child, in order of work. A block opens with [key] Title; then one 'key: value' per line.
# [c1] Parser
# type: FEATURE                    (the parent's type when left out)
# tier: M                          (S or M: a child above M is split again)
# files: lib/parser.py, tests/test_parser.py   (what it owns; two children in one wave never share a file)
# after: c0, c2                    (keys that land first; leave out when none)
# covers: 1, 2                     (numbers of this task's criteria it delivers)
# criterion: the parser reads one block :: python3 -m unittest tests.test_parser
# step: write parse() in lib/parser.py
"""
GUIDE_GIANT = """# One block per milestone; each is an XL or HUGE task that gets its own plan when it starts.
# [m1] Parsing lands
# tier: XL                         (XL or HUGE)
# outcome: the parser exists and reads every block
# after: m0                        (keys that land first; leave out when none)
# covers: 1, 2                     (numbers of this task's criteria it delivers)
"""
GUIDE_INTEGRATION = """# What holds once every child is in, checked by commands that can fail. 'covers: N' names a criterion of this task
# that only the whole delivers.
# criterion: parse and run work together :: python3 -m unittest tests.test_integration
# covers: 3
"""
CHECKS = ("every criterion of this task is covered by a child or by Integration (covers: N)",
          "no child above M (a GIANT's milestones are XL or HUGE)",
          "no cycle and no unknown key in any after line",
          "no two children own the same file (a directory, a glob and a ./ or // spelling count) unless one waits on the other",
          "every child has a criterion whose verify command can fail",
          "Design and Integration are filled, with at least one Integration criterion",
          "the child count fits the tier (XL 3..8, HUGE 9..25, GIANT 2+ milestones): a warning, not a refusal")


def task_plan(p, args):
    from sampo import cli
    b = cli.need_brief(p, args.id)
    _need_big(b)
    guides = {"Design": GUIDE_DESIGN, "Breakdown": GUIDE_GIANT if b.tier == "GIANT" else GUIDE_BREAKDOWN,
              "Integration": GUIDE_INTEGRATION}
    wrote = []

    def fill(x):
        for name in SECTIONS:
            if _empty(x.section(name)):  # a filled section is never touched; a guide alone is rewritten the same
                x.set_section(name, guides[name])
                wrote.append(name)
        x.append_log("big plan: skeleton written (" + ", ".join(wrote) + ")") if wrote else None
    cli.mutate(p, b.id, fill, "big_plan", {"wrote": wrote})
    text = [f"{b.id} ({b.tier}): " + (f"skeleton written for {', '.join(wrote)}" if wrote else "every section is filled; nothing written"),
            f"Fill the three sections in the brief (sampo task set {b.id} --section Breakdown --text '…'), then:",
            f"  sampo big check {b.id}    what it applies:"] + [f"    - {x}" for x in CHECKS] + [
            f"  sampo big expand {b.id}   one child task per block, linked, ordered, scoped (--dry-run first)"]
    return cli.out(args, {"id": b.id, "wrote": wrote, "checks": list(CHECKS)}, "\n".join(text))


def _need_big(b):
    from sampo import cli
    if not is_big(b):
        raise cli.UsageError(f"{b.id} is tier {b.tier}: a full plan is for {', '.join(big_tiers())} tasks "
                             f"(an L task plans with Interpretation, Approach and Risks)")


def task_check(p, args):
    from sampo import cli
    b = cli.need_brief(p, args.id)
    _need_big(b)
    r = analyse(b, p.root)
    lines = [f"{b.id} ({b.tier}): " + ("the full plan holds: " + f"{r['blocks']} block(s) in {len(r['waves'])} wave(s)"
                                       if r["ok"] else f"{len(r['problems'])} problem(s)")]
    lines += [f"  ✗ {x['block']}: {x['problem']}\n      fix: {x['fix']}" for x in r["problems"]]
    lines += [f"  ! {x['block']}: {x['problem']}\n      fix: {x['fix']}" for x in r["warnings"]]
    lines += [f"  wave {i}: {', '.join(w)}" for i, w in enumerate(r["waves"], 1)] if r["ok"] else []
    if r["ok"]:
        lines.append(f"Next: sampo big expand {b.id}")
    cli.out(args, r, "\n".join(lines))
    return 0 if r["ok"] else 1


# ---------------------------------------------------------------- expand

def _sig(x):
    keep = {k: x.get(k) for k in ("title", "type", "tier", "files", "after", "covers", "criteria", "steps", "outcome")}
    return hashlib.sha1(json.dumps(keep, sort_keys=True, default=list).encode()).hexdigest()[:8]


def _children(b):
    """{block key: (child id, block hash)} from the parent's Children section."""
    out = {}
    for line in b.section("Children").splitlines():
        if m := _CHILDREN.match(line.strip()):
            out[m.group(1)] = (m.group(2), m.group(3))
    return out


def _have(p, b):
    """({block key: (child id, sig)} of the children that exist, {block key: id} of recorded ones whose brief is gone).
    A child carries its own plan key from its first write, so the tasks whose parent is b are the truth and the
    parent's Children record only adds to them: a kill between the two leaves nothing to make twice."""
    rec = _children(b)
    briefs = sorted(c.load_briefs(p, include_archive=True), key=lambda x: c.id_num(x.id))
    have = {}
    for kid in briefs:
        k = kid.meta.get("big_key")
        if k and kid.meta.get("parent") == b.id and k not in have:
            have[k] = (kid.id, str(kid.meta.get("big_sig") or rec.get(k, ("", ""))[1]))
    alive = {kid.id for kid in briefs}
    gone = {}
    for k, (i, sig) in rec.items():
        if k not in have:
            if i in alive:
                have[k] = (i, sig)
            else:
                gone[k] = i  # the brief was deleted: absent, made again
    return have, gone


def _write_children(p, parent_id, entries, strip, drop):
    """The parent's side of expand: Children lines for entries [(key, id, title, sig)] (a key's older line goes), the
    ids of `drop` leave depends_on, the new ids join it."""
    b = c.find_brief(p, parent_id)
    out = [l for l in b.section("Children").splitlines()
           if not ((m := _CHILDREN.match(l.strip())) and (m.group(1) in strip or m.group(1) in {e[0] for e in entries}))]
    for k, i, title, sig in entries:
        out.append(f"{k} → {i}  {c.fit(title, 60)}  (block {sig})")
    b.set_section("Children", "\n".join(out) + "\n" if out else "")
    b.meta["depends_on"] = sorted((set(b.meta.get("depends_on") or []) - set(drop)) | {e[1] for e in entries})
    b.append_log("big expand: " + ", ".join([f"{k}={i}" for k, i, _, _ in entries]
                                            + [f"{i} was deleted" for i in sorted(drop)]))
    c.save_brief(p, b)
    from sampo import cli
    c.log_event(p, "big_expand", task=b.id, data={"created": {k: i for k, i, _, _ in entries}}, session=cli.session())
    c.regen_views(p)
    return b


def _inherited(b):
    return [l for l in (re.sub(r"^>\s?", "", x).strip() for x in b.section("Raw request").splitlines()) if _INHERIT.match(l)]


def _interface(b, files):
    """The Design lines that name one of the child's files (its path or its file name)."""
    names = {f for f in files} | {f.rsplit("/", 1)[-1] for f in files if len(f.rsplit("/", 1)[-1]) >= 3}
    lines = [l.strip() for l in b.section("Design").splitlines() if l.strip() and not l.strip().startswith("#")]
    return [l for l in lines if any(n in l for n in names)][:8]


def _make(p, b, x, ids, giant):
    from sampo import cli
    k = x["key"]
    raw = "\n".join([f"{'Milestone' if giant else 'Child'} {k} of {b.id} ({b.title}): {x['title']}"
                     + (f". Outcome: {x['outcome']}" if giant else "")] + _inherited(b))
    deps = sorted({ids[a] for a in x["after"] if a in ids})
    kid = cli._create(p, x["title"], x["type"] or b.type, x["tier"] if not giant else x["tier"] or "XL", "planned", raw=raw,
                      scope=x["files"], depends=deps, source=b.meta.get("source", "user"), priority=b.priority,
                      meta={"parent": b.id, "big_key": k, "big_sig": _sig(x)})  # on the first write: a rerun after a kill finds it
    if giant:
        kid.set_section("Interpretation", f"{x['outcome']}\nThis milestone is part of {b.id}'s Design: its own full plan "
                        f"(Design, Breakdown, Integration) is written when it starts: sampo big plan {kid.id}.\n")
        mine = [a for a in b.acceptance() if a.n in x["covers"]]
        for a in mine:
            kid.add_ac(c.strip_verify(a.text), c.verify_of(a.text))
        if not mine:
            kid.add_ac(x["outcome"] or x["title"])
        kid.add_step(f"write the full plan: sampo big plan {kid.id}, then sampo big check {kid.id}")
    else:
        kid.meta["harness_plan"] = "big"  # the plan was made and checked at the parent: no readings or options to gate
        mine = _interface(b, x["files"])
        kid.set_section("Interpretation", f"{b.id}'s Design, the interfaces this child touches:\n"
                        + ("".join(f"- {l}\n" for l in mine) if mine else f"- none names its files: read {b.id}'s Design\n"))
        for text, verify in x["criteria"]:
            kid.add_ac(text, verify)
        for s in x["steps"] or ["deliver the criteria and keep them passing"]:
            kid.add_step(s)
    c.save_brief(p, kid)
    return kid


def task_expand(p, args):
    from sampo import cli
    b = cli.need_brief(p, args.id)
    _need_big(b)
    if b.status in c.CLOSED:
        raise cli.UsageError(f"{b.id} is {b.status}")
    giant = b.tier == "GIANT"
    created, existing, changed, dropped = {}, {}, [], []
    plan, entries, gone = {}, [], {}  # entries: [(key, id, title, sig)] the parent's Children record still lacks

    def work(b, make):
        r = plan["r"] = analyse(b, p.root)  # again under the lock: the Breakdown may have changed since the first look
        if r["problems"]:
            shown = "; ".join(f"{x['block']}: {x['problem']}" for x in r["problems"][:SHOWN])
            more = f" (+{len(r['problems']) - SHOWN} more)" if len(r["problems"]) > SHOWN else ""
            raise c.PolicyError(f"{b.id}: the plan has problems, nothing made: {shown}{more}; sampo big check {b.id}")
        blocks, _ = parse_blocks(b.section("Breakdown"))
        by = {x["key"]: x for x in blocks}
        have, lost = _have(p, b)
        gone.update(lost)
        recorded = _children(b)
        started = []
        for k, (kid_id, _) in have.items():
            kid = c.find_brief(p, kid_id)
            if k not in by and kid and kid.status not in c.CLOSED:
                (started if c._started(kid) else dropped).append((k, kid_id))
        if started:
            raise c.PolicyError(f"{b.id}: " + "; ".join(f"{k} → {i} has started but is no longer in the Breakdown" for k, i in
                                started) + ": put its block back, or drop that task on purpose first (sampo task set ID "
                                "status=dropped)")
        ids = {k: i for k, (i, _) in have.items()}
        for wave in r["waves"]:
            for k in wave:
                x = by[k]
                if k in have:
                    existing[k] = have[k][0]
                    if have[k][1] != _sig(x):
                        changed.append((k, have[k][0]))
                    if recorded.get(k, ("",))[0] != have[k][0]:  # made, but never written to the parent
                        entries.append((k, have[k][0], x["title"], have[k][1]))
                else:
                    created[k] = make(x, ids)
                    ids[k] = created[k]
        return ids

    if args.dry_run:
        work(b, lambda x, ids: None)
        ids = {}
    else:
        with c.lock(p.dir):
            b = cli.need_brief(p, b.id)

            def make(x, ids):
                kid = _make(p, b, x, ids, giant)
                entries.append((x["key"], kid.id, x["title"], _sig(x)))
                return kid.id
            ids = {}
            try:
                ids = work(b, make)
            finally:  # whatever was made is recorded, even when a later one failed; a hard kill is covered by each
                if entries or gone:  # child's own plan key (_have), so a second run must not make it twice either
                    _write_children(p, b.id, entries, set(gone), set(gone.values()))
    r = plan["r"]
    lines = [f"{b.id} ({b.tier}) {'would make' if args.dry_run else 'made'} {len(created)} "
             f"{'milestone' if giant else 'child'}(ren){' (dry run)' if args.dry_run else ''}, "
             f"{len(existing)} already there:"]
    lines += [f"  {k} → {i or '(new)'}" for k, i in created.items()]
    lines += [f"  {k} → {i} changed in the Breakdown since it was made: edit that task by hand (sampo task set/ac/step), "
              "nothing was rewritten" for k, i in changed]
    lines += [f"  {k} → {i} is no longer in the Breakdown and hasn't started: drop it (sampo task set {i} status=dropped) "
              "or put its block back" for k, i in dropped]
    if created and not args.dry_run:
        lines.append("Run each in a lane (sampo lane brief ID), in wave order: " +
                     " | ".join(", ".join(ids.get(k, k) for k in w) for w in r["waves"]) + f"; {b.id} closes after its children.")
    return cli.out(args, {"created": created, "existing": existing, "changed": dict(changed), "dropped": dict(dropped),
                          "waves": r["waves"], "dry_run": bool(args.dry_run)}, "\n".join(lines))


# ---------------------------------------------------------------- the integration branch (T-4222, part C1)

def kids_of(b):
    """{key: (child id, signature)}: the Children record of the big task b. EVERY reader of a parent's children in the wave
    (wave_state, unmet, _need_expanded, wave_line, task_run) goes through this one function, so reading the record another
    way (T-4223's `_have`) is a change here and nowhere else."""
    return _children(b)


def _main(p):
    return c.main_worktree(p.root) or p.root


def _g(p, *args):
    from sampo import lanes
    return lanes._git(_main(p), *args)


def _zero(p):
    """update-ref's "the ref must not exist yet": the all-zero id, as long as the repository's object ids (sha256: 64)."""
    fmt = _g(p, "rev-parse", "--show-object-format").stdout.strip()
    return "0" * (64 if fmt == "sha256" else 40)


def _ref(p, name):
    """The commit refs/heads/<name> points at, or ''."""
    return _g(p, "rev-parse", "--verify", "-q", f"refs/heads/{name}^{{commit}}").stdout.strip()


def anchor(p, b, by_id=None):
    """The nearest big ancestor of the task b (its parent when that is big, else the parent's parent, and so on), or None:
    the task whose integration branch b is cut from. A GIANT's milestone gets the GIANT, a nested child the milestone."""
    cur, seen = b, {b.id}
    for _ in range(16):
        pid = cur.meta.get("parent")
        if not pid or pid in seen:
            return None
        seen.add(pid)
        up = by_id.get(pid) if by_id is not None else c.find_brief(p, pid)
        if up is None:
            return None
        if is_big(up):
            return up
        cur = up
    return None


def branch(p, b):
    """big/<ID> for the big task b: made once, lazily, from the build head (the main checkout's HEAD), or from the tip of
    the nearest big ancestor's branch; its name and cut commit go on b's meta (big_branch, big_cut) for the children to
    read. It is a local branch only: nothing here pushes. -> {"branch", "sha" (the cut), "tip", "created"}"""
    from sampo import cli
    name = f"big/{b.id}"
    tip, created = _ref(p, name), False
    if not tip:
        up = anchor(p, b)
        start = branch(p, up)["tip"] if up else _g(p, "rev-parse", "HEAD").stdout.strip()
        if not start:
            raise cli.UsageError(f"{b.id}: the build branch has no commit to cut {name} from")
        made = _g(p, "update-ref", f"refs/heads/{name}", start, _zero(p))
        tip, created = _ref(p, name), made.returncode == 0
        if not tip:
            raise c.PolicyError(f"{b.id}: could not make {name}: {made.stderr.strip() or 'git refused'}")
        cut = start if created else tip
    else:
        cut = str(b.meta.get("big_cut") or tip)
    if b.meta.get("big_branch") != name or b.meta.get("big_cut") != cut:
        def record(x):
            x.meta["big_branch"], x.meta["big_cut"] = name, cut

        cli.mutate(p, b.id, record, "big_branch", {"branch": name, "cut": cut})
        b.meta["big_branch"], b.meta["big_cut"] = name, cut
    return {"branch": name, "sha": cut, "tip": tip, "created": created}


def _landed(p, up, ids):
    """The ids among `ids` whose own work is on up's integration branch (a "Merge ID:" or "(ID)" subject, a Revert undoes)."""
    from sampo import lanes
    name = f"big/{up.id}"
    return lanes._merged_among(_main(p), list(ids), name) if ids and _ref(p, name) else set()


def _sibling_deps(kid, up):
    sib = {i for i, _ in kids_of(up).values()}
    return [d for d in c._deps(kid) if d in sib]


def unmet(p, kid, up, by_id=None):
    """The ids the child `kid` of the big task `up` still waits on. A sibling it comes after is met only when its commits are
    ON up's integration branch (the lane is cut from that tip: a closed sibling that never landed there would be missing
    from it), or when it was dropped (its dependents are freed of it); any other dependency is met once closed, as
    everywhere."""
    sib = set(_sibling_deps(kid, up))
    on = None
    out = []
    for d in c._deps(kid):
        x = by_id.get(d) if by_id is not None else c.find_brief(p, d)
        if d in sib:
            if x is not None and x.status == "dropped":
                continue
            on = _landed(p, up, sorted(sib)) if on is None else on
            if d not in on:
                out.append(d)
        elif x is None or x.status not in c.CLOSED:
            out.append(d)
    return out


def proved(p, kid, up):
    """The siblings `kid` comes after whose commits are on up's integration branch right now (what a brief may claim)."""
    deps = _sibling_deps(kid, up)
    on = _landed(p, up, deps)
    return [d for d in deps if d in on]


def why_waits(p, up, ids, by_id=None):
    """One phrase per id `unmet` returned: what the child is still waiting for."""
    out = []
    for d in ids:
        x = by_id.get(d) if by_id is not None else c.find_brief(p, d)
        if x is None:
            out.append(f"{d} has no brief")
        elif x.status == "done":
            out.append(f"{d} is closed but not on big/{up.id} yet")
        else:
            out.append(f"{d} is {x.status}")
    return out


def _checked_out(p, name):
    """The folder where the branch is checked out, or ''."""
    here = ""
    for line in _g(p, "worktree", "list", "--porcelain").stdout.splitlines():
        if line.startswith("worktree "):
            here = line[9:]
        elif line == f"branch refs/heads/{name}":
            return here
    return ""


def _ident(p):
    return [] if _g(p, "config", "user.email").stdout.strip() else ["-c", "user.name=Sampo", "-c", "user.email=sampo@localhost"]


def sync(p, b):
    """Merge the build branch (or the nearest big ancestor's branch) into big/<ID> without touching a worktree: a
    fast-forward when the branch has nothing of its own yet, else a merge commit made from git's own merge-tree. A
    conflict changes nothing and is named. -> {"branch", "from", "status", "sha", "conflicts"}"""
    got = branch(p, b)
    name, up = got["branch"], anchor(p, b)
    if up:
        source, src = f"big/{up.id}", branch(p, up)["tip"]
    else:
        head = _g(p, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        source, src = (head if head and head != "HEAD" else "the build head"), _g(p, "rev-parse", "HEAD").stdout.strip()
    tip = _ref(p, name)
    res = {"branch": name, "from": source, "status": "up to date", "sha": tip, "conflicts": []}
    if _g(p, "merge-base", "--is-ancestor", src, tip).returncode == 0:
        return res
    if where := _checked_out(p, name):
        raise c.PolicyError(f"{name} is checked out in {where}: sync moves the branch without a worktree, so leave it first")
    if _g(p, "merge-base", "--is-ancestor", tip, src).returncode == 0:
        new, res["status"] = src, "fast-forwarded"
    else:
        r = _g(p, "merge-tree", "--write-tree", "--name-only", tip, src)
        lines = r.stdout.splitlines()
        if r.returncode not in (0, 1):  # 1 is "conflicts"; anything else is git not doing the merge at all (T-4222 review)
            ver = _g(p, "version").stdout.strip() or "this git"
            if r.returncode == 129 or "write-tree" in r.stderr:
                raise c.PolicyError(f"{name}: sync needs git 2.38 or newer (merge-tree --write-tree); {ver} lacks it. "
                                    f"Merge by hand: git worktree add <dir> {name}, git merge {source} there, then "
                                    f"git worktree remove <dir>")
            raise c.PolicyError(f"{name}: git merge-tree failed ({r.returncode}): {c.fit(r.stderr.strip(), 160) or 'no message'}")
        if r.returncode != 0:
            files = []
            for l in lines[1:]:
                if not l.strip():
                    break
                files.append(l.strip())
            res.update(status="conflict", conflicts=files or [c.fit(r.stderr.strip() or "git could not merge", 120)])
            return res
        made = _g(p, *_ident(p), "commit-tree", lines[0].strip(), "-p", tip, "-p", src, "-m", f"Merge {source} into {name}")
        new = made.stdout.strip()
        if made.returncode or not new:
            raise c.PolicyError(f"{name}: the merge commit failed: {made.stderr.strip() or 'git refused'}")
        res["status"] = "merged"
    moved = _g(p, "update-ref", f"refs/heads/{name}", new, tip)
    if moved.returncode:
        raise c.PolicyError(f"{name} moved while it was synced: run it again ({moved.stderr.strip()})")
    res["sha"] = new
    return res


def _main_only(p, what):
    """`big run` and `big sync` count every lane and move refs of the repository: in a lane they would count its own as
    the whole (the lane sees a different set of slots), so they are for the main checkout."""
    from sampo import cli
    if p.lane:
        raise cli.UsageError(f"sampo big {what} runs from the main checkout, not a lane: run it from the main checkout")


def task_sync(p, args):
    from sampo import cli
    _main_only(p, "sync")
    b = cli.need_brief(p, args.id)
    _need_big(b)
    r = sync(p, b)
    if r["status"] == "conflict":
        lines = [f"{r['branch']}: merging {r['from']} conflicts in {len(r['conflicts'])} place(s), nothing changed:"]
        lines += [f"  ✗ {f}" for f in r["conflicts"][:20]]
        lines.append(f"Resolve it by hand: git worktree add <dir> {r['branch']}, git merge {r['from']} there, fix the files and "
                     f"commit, then git worktree remove <dir>; sampo big sync {args.id} afterwards finds it up to date.")
        cli.out(args, r, "\n".join(lines))
        return 1
    text = {"up to date": f"{r['branch']} is up to date with {r['from']}",
            "fast-forwarded": f"{r['branch']} fast-forwarded to {r['from']} ({r['sha'][:9]})",
            "merged": f"{r['branch']} merged {r['from']} ({r['sha'][:9]})"}[r["status"]]
    return cli.out(args, r, text)


# ---------------------------------------------------------------- the wave

CHOICES = "retry, re-plan or drop"


def _blocked_why(kid):
    for line in reversed(kid.section("Log").splitlines()):
        if m := re.match(r"^- \S+ blocked: (.*)$", line.strip()):
            return m.group(1).strip()
    return ""


def _problem_row(parent, kid, row, reason, retry):
    return {**row, "reason": reason, "choices": [
        f"retry: {retry}",
        f"re-plan: change {row['key']}'s block in {parent.id}'s Breakdown, then sampo big expand {parent.id}",
        f"drop: sampo task drop {kid.id} \"<why>\" (its dependents are then free of it)"]}


def wave_state(p, b, briefs=None):
    """Where each child of the expanded big task b stands, read-only: landed (closed, or its work is on b's integration
    branch), running (a builder out, or a lane open), problems (blocked, parked by a conflict or a refusal, its builder
    stopped or stalled), waiting (an `after` sibling has not landed), ready. The wave is `ready`, in Breakdown order."""
    from sampo import lanes
    briefs = c.load_briefs(p) if briefs is None else briefs
    by_id = {x.id: x for x in briefs}
    kids = kids_of(b)
    ids = [i for i, _ in kids.values()]
    name = f"big/{b.id}"
    landed = _landed(p, b, ids)
    working, merging, dormant = lanes._slots(p, briefs)
    causes = lanes.stop_causes(p, briefs)
    out = {k: [] for k in ("landed", "dropped", "running", "problems", "waiting", "ready")}
    for key, (kid_id, _) in kids.items():
        kid = by_id.get(kid_id)
        row = {"id": kid_id, "key": key, "title": kid.title if kid else ""}
        if kid is None:
            out["problems"].append({**row, "reason": "its brief is missing", "choices": [
                f"re-plan: remove {key}'s line from {b.id}'s Children, then sampo big expand {b.id}"]})
        elif kid.status == "dropped":  # not landed: it never built anything (its dependents are freed of it)
            out["dropped"].append(row)
        elif kid.status in c.CLOSED or kid_id in landed:
            out["landed"].append(row)
        elif kid.status == "blocked":
            why = _blocked_why(kid)
            out["problems"].append(_problem_row(b, kid, row, "blocked" + (f": {why}" if why else ""),
                                                f"sampo task unblock {kid_id}, once what it waited on is settled"))
        elif kid_id in causes:
            cause, detail = causes[kid_id]
            out["problems"].append(_problem_row(b, kid, row, lanes.STOP_CAUSES[cause][0],
                                                lanes.recovery_text(cause, kid_id, detail)))
        elif lanes.parked(kid):
            kind = lanes._stop(kid)[0]
            out["problems"].append(_problem_row(b, kid, row, f"its lane is parked ({kind})", f"sampo lane recover {kid_id}"))
        elif kid_id in dormant:
            out["problems"].append(_problem_row(b, kid, row, f"its lane is stalled ({lanes.STALL_QUIET_H} h without a change)",
                                                f"sampo lane recover {kid_id}"))
        elif (kid_id in working or kid_id in merging or kid.meta.get("lane") or kid.status in ("active", "verifying")
              or c.held_elsewhere(kid)):
            out["running"].append({**row, "state": "waits on a merge" if kid_id in merging else "working"})
        elif waits := unmet(p, kid, b, by_id):
            out["waiting"].append({**row, "waits": waits, "why": why_waits(p, b, waits, by_id)})
        else:
            out["ready"].append(row)
    cap = lanes.builders_cap(p)
    # a dropped child built nothing: it is reported on its own, and a parent whose children were all dropped has landed nothing
    out.update(branch=name, free=max(0, cap - len(working)), cap=cap, working=working, merging=merging,
               all_landed=bool(out["landed"]) and len(out["landed"]) + len(out["dropped"]) == len(kids))
    return out


def _need_expanded(b):
    from sampo import cli
    _need_big(b)
    if not kids_of(b):
        raise cli.UsageError(f"{b.id} has no children yet: sampo big check {b.id}, then sampo big expand {b.id}")
    if b.status in c.CLOSED:
        raise cli.UsageError(f"{b.id} is {b.status}")


def _strain(st):
    """The refusal a new lane would meet from the load gate or the unmerged cap, or '' (builder_brief's own two checks)."""
    from sampo import lanes
    if len(st["merging"]) >= lanes.UNMERGED_MAX:
        return f"{len(st['merging'])} finished lanes wait unmerged: merge some first (sampo lane merge ID)"
    return c.host_strain(lanes.LANE_HARD_BAR if not st["working"] else c.LOAD_BAR, nice_free=True) or ""


def task_run(p, args):
    """sampo big run ID: the next wave of the expanded big task. Every ready child is briefed (its lane cut from the
    integration branch) and printed as an Agent call, all to go out in one message; capped by the free lane slots and the
    load gate, which is never bypassed. What is left is the next wave, and nothing is handed out twice: a launch marks
    the child's brief, and a launch that never opened a lane is released after 30 minutes by the slot count itself."""
    import argparse
    import contextlib
    import io
    from sampo import cli, lanes
    _main_only(p, "run")
    b = cli.need_brief(p, args.id)
    _need_expanded(b)
    st = wave_state(p, b)
    giant = b.tier == "GIANT"
    dry = bool(getattr(args, "dry_run", False))
    wave, next_wave, cut_failed, held = [], [], [], ""
    if giant:  # a milestone is planned, not built: it is a big task of its own, expanded and run on its own
        wave_rows = []
    elif st["ready"] and dry:
        held = _strain(st)
    for row in st["ready"]:
        if giant:
            wave_rows.append(row)
            continue
        if len(wave) >= st["free"]:
            next_wave.append({**row, "reason": f"no builder slot left ({st['cap']} of {st['cap']} out)"})
        elif held:
            next_wave.append({**row, "reason": held})
        elif dry:
            wave.append({**row, "launch": None})
        else:
            kid = c.find_brief(p, row["id"])
            buf = io.StringIO()
            try:
                with contextlib.redirect_stdout(buf):  # builder_brief's own text is replaced by the block below
                    lanes.builder_brief(p, kid, argparse.Namespace(json=True, model=None))
                data = json.loads(buf.getvalue())
            except Exception as e:  # noqa: BLE001 — one child's lane failing to cut is its own report; the rest go on
                if getattr(e, "batch", False):
                    held = str(e)
                    next_wave.append({**row, "reason": held})
                else:
                    cut_failed.append({**row, "reason": str(e) or type(e).__name__})
                continue
            wave.append({**row, "launch": data["agent"], "path": data["path"], "model": data["model"],
                         "warnings": data["warnings"]})
    r = {"id": b.id, "branch": st["branch"], "wave": wave, "next_wave": next_wave, "waiting": st["waiting"],
         "running": st["running"], "problems": st["problems"], "cut_failed": cut_failed, "landed": st["landed"],
         "dropped": st["dropped"], "held": held, "all_landed": st["all_landed"], "dry_run": dry, "free": st["free"]}
    if giant:
        r["plan"] = wave_rows
    lines = []
    if st["all_landed"]:
        gone = (f"; {len(st['dropped'])} dropped, built nothing: {', '.join(x['id'] for x in st['dropped'])}"
                if st["dropped"] else "")
        lines.append(f"{b.id}: every child that was built ({len(st['landed'])}) is done{gone}: close the parent, "
                     f"sampo task finish {b.id}")
    elif wave:
        lines.append(f"{b.id}: {'would launch' if dry else 'launch'} these {len(wave)} builder(s), one Agent call each, all in "
                     f"one message (cut from {st['branch']}):")
        lines += [f"  {x['id']}: {x['launch'] or '(dry run: no brief written)'}  [{x['key']}: {x['title']}]" for x in wave]
        lines += [f"  note: {w}" for x in wave for w in x.get("warnings") or []]
    elif giant and wave_rows:
        lines.append(f"{b.id}: plan these milestones, each as a big task of its own (sampo focus ID, sampo big plan ID):")
        lines += [f"  {x['id']}: {x['title']}" for x in wave_rows]
    elif not (st["running"] or st["problems"] or st["waiting"] or next_wave or cut_failed):
        lines.append(f"{b.id}: nothing to launch")
    if next_wave:
        lines.append(f"next wave ({len(next_wave)}): " + "; ".join(f"{x['id']} ({x['reason']})" for x in next_wave[:6])
                     + f"; run sampo big run {b.id} again when a builder returns")
    if held:
        lines.append(f"held by the host gate, not bypassed: {held}")
    for x in cut_failed:
        lines.append(f"  ✗ {x['id']}: its lane did not cut: {x['reason']}; sampo big run {b.id} again hands it out with the rest")
    for x in st["problems"]:
        lines.append(f"  ✗ {x['id']} ({x['key']}): {x['reason']}")
        lines += [f"      {ch}" for ch in x["choices"]]
    if st["running"]:
        lines.append("running: " + ", ".join(f"{x['id']} ({x['state']})" for x in st["running"]))
    if st["waiting"]:
        lines.append("waiting: " + "; ".join(f"{x['id']} after {', '.join(x['waits'])}" for x in st["waiting"]))
        lines += [f"  waits: {x['id']}: {'; '.join(x['why'])}" for x in st["waiting"]]
    if st["dropped"] and not st["all_landed"]:
        lines.append(f"dropped: {', '.join(x['id'] for x in st['dropped'])} (built nothing; their dependents are freed of them)")
    lines.append(f"landed: {len(st['landed'])} of {len(kids_of(b))}")
    return cli.out(args, r, "\n".join(lines))


def wave_line(p, b, briefs=None):
    """The next action for a focused, expanded big task, from the wave ('' when there is nothing to say). `briefs` is the
    list the caller (`sampo next`, every turn) already holds: without it every brief is read again."""
    if not is_big(b) or b.status in c.CLOSED or not kids_of(b):
        return ""
    st = wave_state(p, b, briefs)
    n = min(len(st["ready"]), st["free"])
    bad = (f" · {len(st['problems'])} child(ren) failed or parked ({', '.join(x['id'] for x in st['problems'][:4])}): "
           f"sampo big run {b.id} lists each with its choices ({CHOICES})") if st["problems"] else ""
    if st["all_landed"]:
        return f"{b.id}: every child that was built is done: close the parent, sampo task finish {b.id}"
    if b.tier == "GIANT" and st["ready"]:
        return (f"{b.id}: plan the milestone(s) {c.joinmore([x['id'] for x in st['ready']], 4)}: sampo focus ID, then sampo big "
                f"plan ID" + bad)
    if n:
        who = f"these {n} builders" if n > 1 else "this builder"
        return (f"{b.id}: launch {who} in one message: sampo big run {b.id} writes each brief and prints the Agent calls"
                + (f" ({len(st['ready']) - n} more are the next wave)" if len(st["ready"]) > n else "") + bad)
    if st["running"]:
        return (f"{b.id}: {len(st['running'])} builder(s) running ({', '.join(x['id'] for x in st['running'][:4])}): review and "
                f"merge each as it returns, then sampo big run {b.id} for the next wave" + bad)
    if st["problems"]:
        return bad.lstrip(" ·").strip().replace("child(ren)", f"child(ren) of {b.id}", 1)
    return ""


def cmd_big(args):
    from sampo import cli, bigview
    p = cli.resolve(args)
    fn = {"plan": task_plan, "check": task_check, "expand": task_expand, "run": task_run, "sync": task_sync,
          "status": bigview.task_status, "estimate": bigview.task_estimate, "report": bigview.task_report}[args.action]
    if not args.id and args.action != "status":  # T-4223: only status takes no ID (--all)
        raise cli.UsageError(f"sampo big {args.action} needs the big task's ID")
    return fn(p, args)
