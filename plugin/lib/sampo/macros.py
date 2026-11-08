"""T-3922: sampo do NAME runs a routine from plugin/macros/*.json: its steps are sampo verbs, run in order in this process,
for the routines no verb owns. A routine is {"about": "...", "steps": [["lane", "recover", "--all"], ["lane", "land"]]}:
1 to 3 steps, none of them `do`. It prints at most five lines (a header, then one line per step: the step's last output
line, or the refusal that stopped the routine). A file that doesn't load is skipped and named by --list, never run."""
import json
import os
import re

from sampo import core as c

DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "macros"))
MAX_STEPS = 3
NAME = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
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


def load(d=DIR):
    """({name: routine}, [(file, why it was skipped)]) for the routines in folder d."""
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


def cmd_do(args):
    from sampo import cli
    from sampo import train
    routines, problems = load()
    if args.list or not args.name:
        rows = [f"{n}: {r['about']}" for n, r in sorted(routines.items())]
        rows += [f"skipped {fn}: {why}" for fn, why in problems]
        return cli.out(args, {"routines": sorted(routines), "skipped": [fn for fn, _ in problems]},
                       "\n".join(rows) or f"No routines in {DIR}.")
    r = routines.get(args.name)
    if r is None:
        raise cli.UsageError(f"no routine {args.name!r}; sampo do --list shows them: "
                             f"{', '.join(sorted(routines)) or 'none'}")
    lines, refused = [f"do {args.name}: {len(r['steps'])} step(s)"], None
    for step in r["steps"]:
        rc, out, err = train._sampo(step)
        if rc:  # the first refusal stops the routine; its reason is the line
            refused = " ".join(step)
            lines.append(f"  refused at {refused}: {train._why(err or out)}")
            break
        last = next((ln for ln in reversed(out.splitlines()) if ln.strip()), "ok")
        lines.append(f"  {' '.join(step)}: {c.fit(c.plain(last.strip()), 160)}")
    cli.out(args, {"routine": args.name, "lines": len(lines), "refused_at": refused}, "\n".join(lines))
    return 1 if refused else 0
