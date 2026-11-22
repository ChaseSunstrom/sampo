"""T-3922: sampo do NAME runs a routine from plugin/macros/*.json: its steps are sampo verbs, run in order in this process,
for the routines no verb owns. A routine is {"about": "...", "steps": [["lane", "recover", "--all"], ["lane", "land"]]}:
1 to 3 steps, none of them `do`. It prints at most five lines (a header, then one line per step: the step's last output
line, or the refusal that stopped the routine). A file that doesn't load is skipped and named by --list, never run.
T-3923: the routines reach a session's and a subagent's start (start_line), the by-hand note (by_hand, over calls), sampo
mcp and Sampo Code (run_text: the same five lines sampo do prints)."""
import json
import os
import re

from sampo import core as c

DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "macros"))
MAX_STEPS = 3
NAME = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
START_MAX = 300  # T-3923: the start line's cap (one line, 300 characters or fewer)
ABOUT_MAX = 60  # each routine's about, cut in the start line and the tool's menu
TAIL = 32  # room the start line keeps for its "+N more" tail
VERB = re.compile(r"[a-z][a-z0-9-]{0,30}")  # a sampo verb word: no flag, no path, no id
ENV = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
PY = re.compile(r"python3?(?:\.\d+)?")
SEP = re.compile(r"\|\||&&|[|;&\n]")
SEGMENTS = 64  # a command line's segments read per Bash call: a bound on what one call costs the hook
# ponytail: the folder is read on each call; a cache once the registry holds hundreds of routines


def _check(name, body):
    """The routine a file holds, or ValueError saying what is wrong with it."""
    if not NAME.match(name):
        raise ValueError("the file name is not a routine name (lowercase letters, digits and hyphens)")
    if not isinstance(body, dict):
        raise ValueError("not a JSON object")
    about = body.get("about")
    if not isinstance(about, str) or not about.strip():
        raise ValueError('needs an "about" line')
    steps = body.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        raise ValueError(f'"steps" is a list of 1 to {MAX_STEPS} steps')
    for s in steps:
        if not isinstance(s, list) or not s or not all(isinstance(x, str) for x in s):
            raise ValueError("each step is a list of strings: a sampo verb and its arguments")
        if s[0] == "do":
            raise ValueError("a step may not run do (a routine runs verbs, not routines)")
    return {"about": about.strip(), "steps": steps}


def load(d=None):
    """({name: routine}, [(file, why it was skipped)]) for the routines in folder d (default: DIR, read at the call)."""
    d = DIR if d is None else d
    routines, problems = {}, []
    try:
        files = sorted(os.listdir(d))
    except OSError:
        return routines, problems
    for fn in files:
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, fn), encoding="utf-8") as f:
                body = json.load(f)
            routines[fn[:-5]] = _check(fn[:-5], body)
        except (OSError, ValueError) as e:
            problems.append((fn, c.fit(str(e), 160)))
    return routines, problems


def _listed(routines, limit):
    """(text, shown): the routines' names with their about lines, whole ones only, joined by "; " within limit."""
    parts = []
    for n in sorted(routines):
        about = c.fit(" ".join(c.plain(routines[n]["about"]).split()), ABOUT_MAX)
        part = f"{n} ({about})"
        if len("; ".join(parts + [part])) > limit:
            break
        parts.append(part)
    return "; ".join(parts), len(parts)


def start_line(routines=None):
    """T-3923: the line a session's and a subagent's start carry, 300 characters or fewer: the routines that fit, by name
    and about, then how many did not; "" with no routine."""
    if routines is None:
        routines, _ = load()
    if not routines:
        return ""
    head = "Sampo one-call routines (sampo do NAME runs one): "
    text, shown = _listed(routines, START_MAX - len(head) - TAIL)
    more = len(routines) - shown
    tail = ((("; " if text else "") + f"+{more} more (sampo do --list)") if more else "")
    return c.fit(head + text + tail, START_MAX)


def menu(limit, routines=None):
    """T-3923: the routines' names and abouts for a tool's description, cut at limit characters; "" with none."""
    if routines is None:
        routines, _ = load()
    return _listed(routines, limit)[0]


def _leading(words):
    """The leading verb words of a command's words: at most three, ended by the first word that is not verb-shaped."""
    out = []
    for w in words:
        if len(out) == 3 or not VERB.fullmatch(w):
            break
        out.append(w)
    return tuple(out)


def _verbs(seg):
    """The verb words of one command segment: after any env assignments and a python word, the sampo (or fm) word's
    leading verb words; () for a segment that runs no sampo."""
    words = seg.split()
    i = 0
    while i < len(words) and ENV.match(words[i]):
        i += 1
    if i < len(words) and PY.fullmatch(os.path.basename(words[i])):
        i += 1
    if i >= len(words) or os.path.basename(words[i]) not in ("sampo", "fm"):
        return ()
    return _leading(words[i + 1:])


def calls(command):
    """The verb tuples of the sampo calls in a command line, in order. Only verb words are kept: no flag, path, id or
    other text of the command, so a note or a state file never holds what a command was given."""
    segs = SEP.split(command if isinstance(command, str) else "")[:SEGMENTS]
    return [v for v in (_verbs(s) for s in segs) if v]


def by_hand(prev, cur, routines=None):
    """T-3923: the routine a by-hand run of its steps is under way for, or "". Its first two calls are the last two sampo
    calls in order: prev (the calls before this Bash command), then cur (this command's), the second of them in cur; a
    one-step routine's one call in cur is enough. The name is the registry's, never the command's text."""
    if routines is None:
        routines, _ = load()
    seq = list(prev) + list(cur)
    for name in sorted(routines):
        steps = [_leading(s) for s in routines[name]["steps"]]
        if not steps[0]:
            continue
        if len(steps) == 1:
            if any(v[:len(steps[0])] == steps[0] for v in cur):
                return name
            continue
        for i in range(max(0, len(prev) - 1), len(seq) - 1):
            if seq[i][:len(steps[0])] == steps[0] and seq[i + 1][:len(steps[1])] == steps[1] and steps[1]:
                return name
    return ""


def run_text(name, routines=None):
    """T-3923: (text, refused_at): a routine's run as the five lines at most that sampo do prints: the header, then a line
    per step (its last output line, or the refusal that stopped the routine). sampo mcp's do and Sampo Code's do serve
    the same text. A name that is no routine raises cli.UsageError naming the registry."""
    from sampo import cli
    from sampo import train
    if routines is None:
        routines, _ = load()
    r = routines.get(name)
    if r is None:
        raise cli.UsageError(f"no routine {name!r}; sampo do --list shows them: "
                             f"{', '.join(sorted(routines)) or 'none'}")
    lines, refused = [f"do {name}: {len(r['steps'])} step(s)"], None
    for step in r["steps"]:
        rc, out, err = train._sampo(step)
        if rc:  # the first refusal stops the routine; its reason is the line
            refused = " ".join(step)
            lines.append(f"  refused at {refused}: {train._why(err or out)}")
            break
        last = next((ln for ln in reversed(out.splitlines()) if ln.strip()), "ok")
        lines.append(f"  {' '.join(step)}: {c.fit(c.plain(last.strip()), 160)}")
    return "\n".join(lines), refused


def cmd_do(args):
    from sampo import cli
    routines, problems = load()
    if args.list or not args.name:
        rows = [f"{n}: {r['about']}" for n, r in sorted(routines.items())]
        rows += [f"skipped {fn}: {why}" for fn, why in problems]
        return cli.out(args, {"routines": sorted(routines), "skipped": [fn for fn, _ in problems]},
                       "\n".join(rows) or f"No routines in {DIR}.")
    text, refused = run_text(args.name, routines)
    cli.out(args, {"routine": args.name, "lines": len(text.splitlines()), "refused_at": refused}, text)
    return 1 if refused else 0
