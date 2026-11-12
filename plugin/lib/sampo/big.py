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


def cmd_big(args):
    from sampo import cli
    p = cli.resolve(args)
    fn = {"plan": task_plan, "check": task_check, "expand": task_expand}[args.action]
    return fn(p, args)
