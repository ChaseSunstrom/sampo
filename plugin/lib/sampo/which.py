"""fm which TASK (T-1699): the Sampo skills, playbooks and fm commands that fit a task in plain words, each with the
call that loads it. With a dozen skills, eighty playbooks and a hundred commands nobody knows what exists.

Lexical, stdlib only: a document per skill (its name and description, from the skills folder as it is when this runs,
so a skill added later is indexed with no list to update), per playbook (its path and header: title and the lines above
its first section, HEADER_MAX characters at most) and per command (name, help and options' help, as fm help reads them;
not Sampo's own-repo tools, FOREMAN_ONLY). The words are fmrecall.words (\\w+, so 3d and ui count; its stems and
stop words). A document scores the idf of each task word it holds, 1 + NAMED times when its name holds it; it can be
listed when it holds at least half the task's words or its name holds one, and is when it is within CUT of its kind's
best and FLOOR of the best overall. When the listed ones together hold under half the task's words, nothing matches:
one common word ("page" in a login task) isn't a fit.
ponytail: lexical, so a synonym (animation for motion) misses; embeddings if that starts to matter."""
import glob
import math
import os
import re
import shutil

from sampo import core as c

SKILLS = os.environ.get("FOREMAN_SKILLS") or os.path.join(c.PLUGIN_ROOT, "skills")  # tests: a fixture
READ_MAX = 8000  # characters of a file read: a header is near the top
HEADER_MAX = 600  # characters of a playbook's header indexed: a body's common words would outrank real headers
CUT = 0.4  # listed: a score at least this share of its kind's best (a skill's one sentence scores less than a playbook)
FLOOR = 0.25  # and this share of the best overall: a kind whose best is one stray word lists nothing
NAMED = 2  # extra weight of a task word in a document's name (ui/motion, fm notify, sampo:tidy)
STEM = 6  # letters a stem keeps: at fmrecall's 5, archives and architecture are one word
SHOWN = {"skills": 3, "playbooks": 5, "commands": 3}
BIG = 2000  # T-1835: past this many playbooks, candidates come from fmplaybooks.find's index, not every header
SELF = {"which", "help"}  # the commands that answer this same question
FOREMAN_ONLY = {"release", "release-check", "bench", "burden", "evals"}  # Foreman's own repo's tools, not a user's
_USER_ONLY = re.compile(r"^disable-model-invocation:[ \t]*true[ \t]*$", re.M | re.I)


def words(text):
    from sampo import recall
    return recall.words(text, cut=STEM)


def _head(path):
    if not os.path.isfile(path):  # a FIFO would block the read
        return ""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(READ_MAX)
    except OSError:
        return ""


def skills(root=None):
    """[{name, load, summary, text}] for each skill folder's SKILL.md; a user-only one (disable-model-invocation) is
    one the user types, so that is its load line."""
    from sampo import plugins as splugins
    out = []
    for path in sorted(glob.glob(os.path.join(root or SKILLS, "*", "SKILL.md"))):
        fm = splugins._frontmatter(path)
        name = c.plain(fm.get("name") or os.path.basename(os.path.dirname(path)))
        desc = c.plain(fm.get("description") or "")
        if len(desc) > 1 and desc[0] == desc[-1] and desc[0] in "\"'":  # YAML's quoted scalar
            desc = desc[1:-1]
        front = re.match(r"---\n(.*?)\n---", _head(path), re.S)
        typed = bool(front and _USER_ONLY.search(front.group(1)))
        out.append({"name": name, "load": f"(you type /sampo:{name})" if typed else f"Skill sampo:{name}",
                    "summary": desc, "text": f"{name.replace('-', ' ')} {desc}"})
    return out


def playbooks(task=None):
    """[{name, load, summary, text}] for each playbook: its stage and name, then its title and the lines above its
    first section (the Use-when line), HEADER_MAX characters at most; named the way fm playbooks show takes it. Past
    BIG books only the ones fmplaybooks.find returns for the task are read."""
    from sampo import playbooks as splaybooks
    out, rels = [], splaybooks._playbooks(splaybooks.ROOT)
    if task and len(rels) > BIG:
        rels = [x["path"] for x in splaybooks.find(task, 4 * SHOWN["playbooks"], splaybooks.ROOT)[1]]
    for rel in rels:
        _, prose = splaybooks._blocks(_head(os.path.join(splaybooks.ROOT, rel)))
        head = prose[:next((i for i, x in enumerate(prose) if x.startswith("## ")), len(prose))]
        head = [x.strip() for x in head if x.strip() and not x.lstrip().startswith("<!--")]
        name = rel.removeprefix("references/").removesuffix(".md")
        title = next((x[2:].strip() for x in head if x.startswith("# ")), name)
        out.append({"name": name, "load": f"fm playbooks show {name}", "summary": c.plain(title),
                    "text": f"{name.replace('/', ' ').replace('-', ' ')} {' '.join(head)[:HEADER_MAX]}"})
    return out


def commands():
    from sampo import cli
    from sampo import recall
    out = [{"name": n, "load": f"fm {n}", "summary": cli._no_ids(" ".join(h.split())), "text": t}
           for n, (h, t) in sorted(recall.verb_docs().items()) if n.split()[0] not in SELF | FOREMAN_ONLY]
    out.append({"name": "task set ID scope=[...]", "load": "fm task set ID scope=[...]",
                "summary": "widen task scope: set the scope assignments on a task (scope+=PATH appends, scope-=PATH removes)", "text": "task set ID scope= widen task scope"})
    out.append({"name": "task set ID approved=true", "load": "fm task set ID approved=true",
                "summary": "approve a task: mark it approved", "text": "task set ID approved=true approve a task"})
    out.append({"name": "task set ID --allow CATEGORY", "load": "fm task set ID --allow CATEGORY",
                "summary": "allow a guard category on a task", "text": "task set ID --allow guard category allow a task"})
    return out


# ---- T-2371: the other shelves. Local files only: no network, no process; a shelf that fails is skipped and said.
HERE = ("fixes", "tools", "mcp", "installed", "binaries")  # solved before, then what this machine holds
SHELVES = HERE + ("skills", "playbooks", "commands", "library", "plugins")
TITLES = {"fixes": "Solved before", "tools": "On this machine: project tools", "mcp": "On this machine: MCP servers",
          "installed": "On this machine: installed skills", "binaries": "On this machine: binaries",
          "skills": "Skills", "playbooks": "Playbooks", "commands": "fm commands",
          "library": "Stack cards and skill sources", "plugins": "Marketplace plugins"}
_NAME = re.compile(r"[A-Za-z0-9_.+-]{1,64}")  # the capability-miss hook's rule for a tool name
GRANT = "fm ask ID plugin"
UNREVIEWED = "this repo's, unreviewed: read it first"  # a file the repo carries: its word, not this machine's
HIT_MAX = 140  # characters of the hook note's local hit
JSON_MAX = 8_000_000  # bytes of a config read (a real ~/.claude.json is a few MB at most)


def _row(name, load, summary, source, grant="", text=None):
    return {"name": c.fit(c.plain(str(name)), 80), "load": c.fit(c.plain(str(load)), 160),
            "summary": c.fit(c.plain(str(summary)), 160), "source": source, "grant": grant,
            "text": c.plain(text if text is not None else f"{name} {summary}")}


def binary(name, root):
    """The path of an executable called name: PATH, then the bin folders a session's PATH may lack, or ''."""
    if not _NAME.fullmatch(name):
        return ""
    home = os.path.expanduser("~")
    for d in (os.path.join(home, ".local", "bin"), os.path.join(home, ".cargo", "bin"), os.path.join(root, "node_modules", ".bin")):
        path = os.path.join(d, name)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return shutil.which(name) or path
    return shutil.which(name) or ""


def binaries(task, root):
    """Only a need that is a name (one word): a sentence's stray word (login in "login page oauth") is no tool."""
    names = re.findall(r"[a-z0-9_.+-]{2,64}", task.lower())
    names = names if len(names) == 1 and len(task.split()) == 1 else []
    paths = {n: binary(n, root) for n in names}
    return [_row(n, path, "an executable here", "binary", text=n) for n, path in paths.items() if path]


def _tool_file(root, name):
    """tools/NAME.py when it is a script `fm new tool` made (its header), else ''. ponytail: only the folder fm new
    tool writes to by default; a tool made with a DIR elsewhere isn't found."""
    from sampo import new
    path = os.path.join(root, "tools", f"{name}.py")
    return path if _NAME.fullmatch(name) and new._is_tool(path) else ""


def tools(root):
    out = []
    for path in sorted(glob.glob(os.path.join(root, "tools", "*.py")))[:300]:
        name = os.path.basename(path)[:-3]
        if _tool_file(root, name):
            doc = re.search(r'"""([^\n]*)', _head(path)[:1500])
            out.append(_row(name, f"python3 tools/{name}.py", doc.group(1) if doc else "",
                        f"project tool ({UNREVIEWED})"))
    return out


def _json_file(path):
    """A config's parsed JSON, or {}: a regular file under JSON_MAX bytes only (a FIFO would block, a huge one cost)."""
    from sampo import plugins as splugins
    try:
        if not os.path.isfile(path) or os.path.getsize(path) > JSON_MAX:
            return {}
    except OSError:
        return {}
    return splugins._json(path, {}) or {}


def mcp(root):
    out = []

    def take(servers, source):
        for name in servers if isinstance(servers, dict) else ():
            out.append(_row(name, f"MCP server {name}", "a configured MCP server", source, text=str(name)))
    mine = _json_file(os.path.join(root, ".mcp.json"))
    take(mine.get("mcpServers") if isinstance(mine, dict) else None, f".mcp.json ({UNREVIEWED})")
    conf = _json_file(os.path.join(os.path.expanduser("~"), ".claude.json"))
    if isinstance(conf, dict):
        take(conf.get("mcpServers"), "~/.claude.json")
        proj = conf.get("projects")
        mine = proj.get(root) if isinstance(proj, dict) else None
        take(mine.get("mcpServers") if isinstance(mine, dict) else None, "~/.claude.json (this project)")
    return out


def installed(p):
    from sampo import plugins as splugins
    return [_row(x["name"], f"Skill {x['name'].lstrip('/')}", x["text"], "installed skill")
            for x in (splugins._skill_index(p).get("skills") or []) if isinstance(x, dict) and x.get("name")]


def fixes(p):
    from sampo import fixes as sfixes
    out = []
    for f in sfixes.known(p)[:300]:
        mine = f["trusted"]
        out.append(_row(f["sig"], f["do"], f["sig"], "learned here" if mine else
                        "this repo's fixes, not learned here: check it before running anything it names",
                        text=f"{f['sig']} {f['do']}"))
    return out


def library(task):
    """Stack cards and indexed skill sources; an enabled plugin's skill is the installed shelf's, a disabled one's
    enabling needs a grant."""
    from sampo import lib
    plug = {s["name"]: s for s in lib.plugin_skills()}
    out = []
    for r in lib.search(task, 30):
        s = plug.get(r["name"])
        if s and s.get("enabled"):
            continue
        source, grant = (("stack card", "") if r["kind"] == "card" else
                         ("installed plugin, not enabled", GRANT) if s else ("skill source", ""))
        out.append(_row(r["name"], f"fm lib show {r['name']}", r["summary"], source, grant))
    return out


def plugins(task):
    from sampo import plugins as splugins
    out = []
    for h in splugins.find(task, 10):
        on = h["installed"] and h["enabled"]
        out.append(_row(h["id"], h["id"], h["description"], "installed plugin" if on else
                        "installed plugin, not enabled" if h["installed"] else f"marketplace {h['marketplace']}",
                        "" if on else GRANT, text=f"{h['name']} {h['description']} {h['category']}"))
    return out


def search(task, p=None, failed=None):
    """{shelf: [row with its score]} over every shelf, each shelf's best first; a shelf that raises is empty and its
    name appended to failed."""
    root = p.root if p else os.getcwd()
    makers = {"fixes": lambda: fixes(p), "tools": lambda: tools(root), "mcp": lambda: mcp(root),
              "installed": lambda: installed(p) if p else [], "binaries": lambda: binaries(task, root),
              "skills": skills, "playbooks": lambda: playbooks(task), "commands": commands,
              "library": lambda: library(task), "plugins": lambda: plugins(task)}
    found = {}
    for shelf in SHELVES:
        try:
            found[shelf] = makers[shelf]()
        except Exception:
            found[shelf] = []
            if failed is not None:
                failed.append(shelf)
    return rank(task, found)


def skipped_note(failed):
    """The line that says a shelf could not be read, so a quiet answer is not mistaken for a complete search."""
    return f"skipped: {', '.join(failed)} (could not be read)" if failed else ""


def local_hit(p, name):
    """What the capability-miss hook adds to its note, from the cheap sources only (a past fix this machine learned, a
    binary in a bin folder): one short phrase, or ''. No plugin index, no skill index. Only what this machine holds or
    learned: a repo's own tools/NAME.py is its word, so a repo can't plant one and have the note send a session to run
    it. A phrase that would not fit whole is left out: a command cut mid-string is worse than none."""
    from sampo import fixes as sfixes
    if not _NAME.fullmatch(name):
        return ""
    low = name.lower()
    for f in sfixes.known(p)[:300]:  # a fix that came with the repo is data to weigh: not put in a note
        first = re.findall(r"[a-z0-9_.+-]+", f["sig"].lower())[:1]  # the command word the failure names
        if f["trusted"] and first == [low]:
            hit = c.plain(f"a known fix: {f['do']}")
            return hit if len(hit) <= HIT_MAX else ""
    path = binary(name, p.root)
    hit = c.plain(f"it is already at {path}") if path else ""
    return hit if len(hit) <= HIT_MAX else ""


def rank(task, found=None):
    """{kind: [row with its score]} for the task's words, best first; every kind empty when no fit holds half of
    them."""
    want = list(dict.fromkeys(words(task)))
    found = found or {"skills": skills(), "playbooks": playbooks(task), "commands": commands()}
    # T-2248: a name's last segment only; a folder (release/) is shared by every book in it, so as a name word it ties
    # them all and the alphabet picks; it still counts once in the text
    docs = [(kind, x, set(words(x["text"])), set(words(x["name"].rsplit("/", 1)[-1].replace("-", " "))))
            for kind, rows in found.items() for x in rows]
    df = {w: sum(w in d for _, _, d, _ in docs) for w in want}
    idf = {w: math.log(1 + len(docs) / df[w]) for w in want if df[w]}
    scored = [(sum(v for w, v in idf.items() if w in d) + NAMED * sum(v for w, v in idf.items() if w in n), kind, x,
               d & set(want)) for kind, x, d, n in docs if 2 * len(d & set(want)) >= len(want) or n & set(want)]
    top = max((r[0] for r in scored), default=0)
    best = {kind: max((r[0] for r in scored if r[1] == kind), default=0) for kind in found}
    out, covered = {kind: [] for kind in found}, set()
    for s, kind, x, shared in sorted(scored, key=lambda r: (-r[0], r[1], r[2]["name"])):
        if s and s >= CUT * best[kind] and s >= FLOOR * top and len(out[kind]) < SHOWN.get(kind, 3):
            out[kind].append(dict(x, score=round(s, 2)))
            covered |= shared
    return out if 2 * len(covered) >= len(want) else {kind: [] for kind in found}


def cmd(args):
    from sampo import cli
    from sampo import recall
    if getattr(args, "instruments", False):  # T-2461: fm instruments folded in here
        from sampo import instr
        return instr.cmd_instruments(args)
    task = " ".join(args.task)
    if not words(task):
        raise cli.UsageError(f"nothing to look up in {task!r}, only words like the, with and need: name the work, "
                               "e.g. fm which add a 3D hero with motion")
    p, failed = c.find_project(os.getcwd(), create=False), []
    got = search(task, p, failed)
    data = {"task": task, **{k: [{x: r.get(x, "") for x in ("name", "load", "summary", "score", "source", "grant")}
                                 for r in v] for k, v in got.items()}, "skipped": failed}
    if p:  # the receipt: what was searched and how much each shelf held, a search that found nothing included
        b = c.active_brief_fast(p, p.lane)  # T-2372: a build is held to a receipt made on its own task
        c.log_event(p, "tool_search", task=b and b.id, data={"need": task[:200], "sources": list(got),
                                            "hits": {k: len(v) for k, v in got.items()}, "failed": failed})
    if not any(got.values()):
        cli.out(args, data, f"Nothing matches {c.fit(c.plain(task), 60)!r}: no past fix, project tool, MCP server, "
                  "binary, skill, playbook, fm command, stack card or plugin holds enough of its words.\n"
                  "sampo help --all lists every command; say what the work is in other words, or build it: sampo new tool NAME."
                  + ("\n" + skipped_note(failed) if failed else ""))
        return 1
    lines = []
    ours = ("skills", "playbooks", "commands")  # T-1908: Foreman's own, the strongest kind first
    order = list(HERE) + sorted(ours, key=lambda k: -max((r["score"] for r in got[k]), default=0)) + ["library", "plugins"]
    for kind in order:
        if got[kind]:
            w = max(len(r["load"]) for r in got[kind]) + 2
            lines += [f"{TITLES[kind]}:"] + [
                f"  {r['load']:<{w}}{recall.gist(r['summary'], task, max(30, 98 - w))}"
                + (f"  [{r['source']}{'; needs ' + r['grant'] if r['grant'] else ''}]" if r.get("source") else "")
                for r in got[kind]]
    cli.out(args, data, "\n".join(lines + ([skipped_note(failed)] if failed else [])))
    return 0
