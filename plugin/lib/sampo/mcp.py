"""fm mcp (T-0245): Sampo's memory and state as read-only MCP tools over stdio — JSON-RPC 2.0, one message a line,
stdlib only — so other agents and clients can read the project's state, next action, recall, research notes and
briefs. Each tool runs the matching read-only fm command (the CLI stays the one implementation); everything sent is
redacted; a sensitive project serves nothing. Registering it (`claude mcp add foreman -- fm mcp`) is the user's
step: the guard asks for the plugin category.
T-0946: plus the stack library (lib_suggest, lib_search, lib_show) and the read-only instruments (outline, sym,
impact, why, fail on given text, logs, data, trace), whose path inputs must resolve inside the project; every value
is length-capped and never starts with '-'; each call has a timeout. `fm mcp new` scaffolds a server for any CLI;
`fm mcp register` prints the line that adds one to a harness."""
import json
import os
import re
import shlex
import shutil
import subprocess
import sys

from sampo import core as c
from sampo import instr

VERSION = "2025-06-18"
_TEXT = {"type": "object", "properties": {}, "additionalProperties": False}
PATH_CAP, QUERY_CAP, TEXT_CAP = 1000, 500, 200_000
SECTIONS = ["verify", "test", "do", "avoid", "review"]


def _obj(required=(), **props):
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


def _str(desc, cap=PATH_CAP, **more):
    return {"type": "string", "description": desc, "maxLength": cap, **more}


def _instr(name):
    """An instrument's input schema, from fminstr's registry, with each value capped."""
    s = instr.schema(name)
    for v in s["properties"].values():
        v["maxLength"] = PATH_CAP
    return s


def _say(name):
    cap, summary, _ = instr.TOOLS[name]
    return f"{summary[0].upper()}{summary[1:]} (at most {cap} lines; paths inside the project only)."


_FAIL = dict(_instr("fail"), properties={"text": _str("the test runner's failure output", TEXT_CAP)}, required=["text"])
TOOLS = [
    ("state", "The project's Sampo state: active task, queue, inbox, blocked work.", _TEXT, lambda a, p: ["state"]),
    ("next", "The one next required action and its procedure.", _TEXT, lambda a, p: ["next"]),
    ("profile", "The user's standing preferences (their machine-local profile, also the sampo://profile resource): how "
     "they want work done, read before acting.", _TEXT, None),  # T-3672
    ("recall", "Past work, decisions, lessons and research related to a question.",
     {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
     lambda a, p: ["recall", _need(a, "query", r"[^\x00-][^\x00]{0,499}")]),  # review: no leading -, so never a flag
    ("research_list", "The names of the project's saved research notes, newest first.", _TEXT, None),
    ("research_read", "One saved research note by name (from research_list).",
     {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}, None),
    ("task_show", "A task's brief: request, interpretation, criteria, steps, evidence and log.",
     {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
     lambda a, p: ["task", "show", _need(a, "id", r"T-[0-9]{4,}")]),
    ("checks", "The project's last gate run (fm check): each command, its exit and time.", _TEXT, None),
    ("find", "Likely defects in the project or a folder in it, worst first: untested public code, imports no manifest "
     "declares, SQL built from a parameter, docs naming code that doesn't exist, secrets. Each is unproven until a "
     "failing test shows it; nothing in the tree runs.", _obj(path=_str("a folder inside the project (default: its root)")),
     lambda a, p: ["find", os.path.join(".", _inside(p, a, "path")) if a.get("path") else ".", "--rank"]),  # ./: never a flag
    # T-0946: the stack library
    ("lib_suggest", "Stack-library cards that fit this project's languages, frameworks and tools.", _TEXT,
     lambda a, p: ["lib", "suggest"]),
    ("lib_search", "Stack-library cards and installed plugins' skills matching topic words.",
     _obj(["query"], query=_str("topic words", QUERY_CAP)), lambda a, p: ["lib", "search", _val(a, "query", QUERY_CAP)]),
    ("lib_show", "One stack-library card (from lib_search or lib_suggest), or only some of its sections.",
     _obj(["name"], name=_str("a card name, or plugin:skill", 200), section=_str("only this section", 20, enum=SECTIONS)),
     lambda a, p: ["lib", "show", _val(a, "name", 200)] + (["--section", _val(a, "section", 20, SECTIONS)]
                                                           if "section" in a else [])),
    # T-0946: the read-only instruments, paths confined to the project
    ("outline", "A file's definitions with line ranges.", _obj(["path"], path=_str("a file in the project")),
     lambda a, p: ["outline", _inside(p, a, "path")]),
    ("impact", "Likely tests and dependents of a path.", _obj(["path"], path=_str("a path in the project")),
     lambda a, p: ["impact", _inside(p, a, "path")]),
    ("why", "The commits and Sampo tasks behind FILE[:LINE], with their lessons.",
     _obj(["target"], target=_str("FILE or FILE:LINE in the project")),
     lambda a, p: ["why", _inside(p, a, "target", r"(.*?)(?::\d+)?")]),
    ("sym", _say("sym"), _instr("sym"), lambda a, p: ["sym", _inside(p, a, "target", r"(.*):[^:/]+")]),
    ("fail", _say("fail").replace("paths inside the project only", "from the given text"), _FAIL, None),
    ("logs", _say("logs"), _instr("logs"),
     lambda a, p: ["logs", _inside(p, a, "file")] + (["--since-good", _inside(p, a, "since_good")]
                                                     if "since_good" in a else [])),
    ("data", _say("data"), _instr("data"), lambda a, p: ["data", _inside(p, a, "file")]),
    ("trace", _say("trace"), dict(_instr("trace"), required=["file"]),  # no stdin here: the file is required
     lambda a, p: ["trace", _inside(p, a, "file")]),
    ("exam_scorecard", "Genesis' done gate for the user (fm exam scorecard): each gen-0 §2 capability's state on its "
     "held-out exam, the top gaps, and how many Haiku smoke lines it left out.", _TEXT,
     lambda a, p: ["exam", "scorecard"]),
    ("genesis_status", "The Genesis tower's status (fm genesis status): which generation it's on, its files, coverage, "
     "the one next action and its command, the last exam result.", _TEXT, lambda a, p: ["genesis", "status"]),  # T-2201
    ("triggers", "Each trigger that files tasks (T-2276): fired, done, dropped, open, precision, route; the builder "
     "lanes recovering and the self-fix trials (T-2364), as JSON. Read-only.", _TEXT,
     lambda a, p: ["triggers", "--json"]),
    ("goal", "The standing goal (T-2141): its text, items with their state, the one progress line, and the verdict "
     "fm goal verdict last stored. Reads meta and briefs only; it runs no check.", _TEXT, None),
    ("calibration", "Judgment's track record (T-2344): stated-confidence buckets per kind, the Brier beside the base rate's, "
     "the research and race bars, races run and how often the first choice lost, tasks parked at a one-way door. "
     "Read-only: it settles nothing and moves no bar (fm outcomes --judgment does).", _TEXT, None),
    ("which", "What already does a piece of work (fm which, T-2378): past fixes, project tools, MCP servers, binaries, "
     "skills, playbooks, fm commands, stack cards and plugins that hold its words, each shelf's best first. Read-only: "
     "it logs no search receipt.", _obj(["task"], task=_str("the work, in words", cap=300)), None),
    ("proof", "Done tasks no run here proved, by kind (fm prove --rollup, T-2395): typed, red, ui, docs, perf, claimed, meaning "
     "and the rest, each row's needs with the command that closes it. Read before calling work verified.", _TEXT,
     lambda a, p: ["prove", "--rollup", "--json"]),
    ("grants", "Install grants queued for the user (T-2374): per open task, what is asked and why, the miss behind it, "
     "what was searched, the fallback, and what a yes unblocks. Read-only: a grant is the user's to answer.", _TEXT, None),
    ("decide", "One choice among 2 to 12 by Sampo's model layer (T-2489): local backends before cloud, the first "
     "answer whose probability clears 0.8, as {choice, probability, backend, cleared, tried}; booked as mcp-KIND for "
     "calibration. It advises; it gates nothing.",
     _obj(["kind", "prompt", "choices"], kind=_str("what is decided, e.g. intake-type", cap=40),
          prompt=_str("the question", cap=4000),
          choices={"type": "array", "items": {"type": "string", "maxLength": 80}, "minItems": 2, "maxItems": 12}), None),
]
MAX_LINE = 1_000_000


class _Bad(Exception):
    pass


def _val(args, key, cap, choices=None):
    """A string argument: present, at most cap characters, never an option (no leading '-'), no NUL."""
    v = args.get(key)
    if not isinstance(v, str) or not v:
        raise _Bad(f"{key}: expected a string")
    if len(v) > cap:
        raise _Bad(f"{key}: over {cap} characters")
    if v.startswith("-") or "\x00" in v:
        raise _Bad(f"{key}: may not start with '-' or hold a NUL")
    if choices and v not in choices:
        raise _Bad(f"{key}: one of {', '.join(choices)}")
    return v


def _inside(p, args, key, shape=r"(.*)"):
    """A path argument (group 1 of shape) that resolves, links followed, inside the project's root."""
    v = _val(args, key, PATH_CAP)
    m = re.fullmatch(shape, v, re.S)
    if not m:
        raise _Bad(f"{key}: expected PATH:NAME, got {c.fit(c.plain(v), 80)}")
    root = os.path.realpath(p.root)
    real = os.path.realpath(os.path.join(root, m.group(1))) if m and m.group(1) else ""
    if not real or os.path.commonpath([root, real]) != root:
        raise _Bad(f"{key}: {c.fit(c.plain(v), 80)} is outside the project; only paths under its root are served")
    if _SECRET.search(os.path.relpath(real, root)):
        raise _Bad(f"{key}: {c.fit(c.plain(v), 80)} looks like a secret (.env, keys, .git): never served")
    return v


_SECRET = re.compile(r"(^|/)(\.git(/|$)|\.env(\.[^/]*)?$|id_(rsa|dsa|ecdsa|ed25519)[^/]*$|[^/]*\.(pem|key|p12|pfx|jks)$"
                     r"|\.npmrc$|\.pypirc$|\.netrc$|credentials[^/]*$)")


def _need(args, key, pattern):
    v = args.get(key) if isinstance(args, dict) else None
    if not isinstance(v, str) or not re.fullmatch(pattern, v):
        raise _Bad(f"{key}: expected {pattern}")
    return v


def _timeout():
    try:
        return float(os.environ.get("FOREMAN_MCP_TIMEOUT") or 120)
    except ValueError:
        return 120.0


def _fm(p, argv, stdin="", ok=(0,)):
    # T-0946: stdin is given (empty by default), so a tool never reads the server's own message stream
    r = subprocess.run([sys.executable, os.path.join(c.PLUGIN_ROOT, "bin", "fm"), *argv], cwd=p.root, input=stdin,
                       capture_output=True, text=True, errors="replace", timeout=_timeout(),
                       env=dict(os.environ, FOREMAN_NO_BACKGROUND="1"))
    # an allowed non-zero exit (fm find's 1, findings) with nothing on stdout is a crash's traceback, not an answer
    bad = r.returncode not in ok or (r.returncode != 0 and not r.stdout.strip())
    return ((r.stderr or r.stdout) if bad else r.stdout), bad


def _call(p, name, args):
    """(text, is_error) for one tool call."""
    if c.read_meta(p).get("sensitive"):
        return "sensitive project: fm mcp serves nothing from it", True
    tool = next((t for t in TOOLS if t[0] == name), None)
    if not tool:
        return f"no such tool: {c.fit(c.plain(str(name)), 60)}", True
    if not isinstance(args, dict) or set(args) - set(tool[2]["properties"]):
        return f"arguments: an object with only {', '.join(tool[2]['properties']) or 'no keys'}", True
    try:
        folder = os.path.join(p.dir, "research")
        if name == "research_list":
            names = sorted((n for n in os.listdir(folder) if n.endswith(".md")) if os.path.isdir(folder) else [],
                           key=lambda n: os.path.getmtime(os.path.join(folder, n)), reverse=True)
            return "\n".join(n[:-3] for n in names[:200]) or "(no research notes)", False  # not a list: text for a model, not a screen
        if name == "research_read":
            note = _need(args, "name", r"[A-Za-z0-9][A-Za-z0-9._-]{0,120}")
            path = os.path.join(folder, note + ".md")
            if not os.path.isfile(path) or os.path.dirname(os.path.realpath(path)) != os.path.realpath(folder):
                return f"no research note named {note}", True
            with open(path, encoding="utf-8", errors="replace") as f:
                return f.read(200_000), False
        if name == "profile":  # T-3672
            from sampo import profile
            return profile.full() or "(no profile: sampo profile rebuild builds one)", False
        if name == "checks":  # review: the request's "check results", read from the ledger in-process
            run = next((e for e in reversed(c.ledger_tail(p, 5000, kinds=("check_run",))) if e.get("event") == "check_run"), None)
            if not run:
                return "(fm check hasn't run in this project)", False
            rows = (run.get("data") or {}).get("results") or []
            return "\n".join([f"fm check at {run.get('ts')}:"] + [
                f"{'✓' if r.get('exit') == 0 else '✗'} {r.get('cmd')} (exit {r.get('exit')}, {r.get('s')} s)"
                for r in rows if isinstance(r, dict)]), False
        if name == "goal":  # T-2141: the stored verdict as it stands; fm goal verdict is what refreshes it
            from sampo import mandate
            m = mandate.get(p)
            if m:
                by = {b.id: b for b in c.load_briefs(p, include_archive=True)}
                m["items"] = [dict(it, state=mandate.item_state(it, by)) for it in m.get("items") or []]
            return json.dumps({"goal": m, "line": mandate.goal_line(p), "verdict": mandate.fresh_verdict(p, m)},
                              ensure_ascii=False), False
        if name == "decide":  # T-2489: a sensitive project never gets here (refused above), so cloud is allowed
            from sampo import models
            kind, prompt, choices = _need(args, "kind", r"[a-z][a-z0-9-]{0,39}"), args.get("prompt"), args.get("choices")
            if not isinstance(prompt, str) or not 0 < len(prompt) <= 4000:
                return "prompt: the question, 1 to 4000 characters", True
            if (not isinstance(choices, list) or not 2 <= len(choices) <= 12
                    or not all(isinstance(x, str) and 0 < len(x) <= 80 for x in choices)):
                return "choices: 2 to 12 strings, each 1 to 80 characters", True
            got = models.decide("mcp-" + kind, prompt, choices, p=p)
            if not got:
                return "no backend answered (fm doctor's model backends line lists them)", True
            return json.dumps({k: got.get(k) for k in ("choice", "probability", "backend", "cleared", "tried")},
                              ensure_ascii=False), False
        if name == "which":  # T-2378: the shelves searched in-process, so no tool_search receipt lands on a task
            from sampo import which
            task = args.get("task")
            if not isinstance(task, str) or not which.words(task):
                return "task: the work in words, e.g. render a chart from a CSV", True
            failed = []
            got = which.search(task, p, failed)
            return json.dumps({"task": task, **{k: [{x: r.get(x, "") for x in ("name", "load", "summary", "source", "grant")}
                                                    for r in v[:5]] for k, v in got.items()}, "skipped": failed},
                              ensure_ascii=False, default=str), False
        if name == "grants":  # T-2378: the queue as fm todo shows it, without the command (the user's to run)
            from sampo import grants
            briefs = c.load_briefs(p)
            return json.dumps([{"task": b.id, "text": grants.text(d), "unblocks": grants.unblocks(p, b, briefs)}
                               for b, d in grants.ranked(p, briefs)], ensure_ascii=False), False
        if name == "calibration":  # T-2344: fm outcomes --judgment's JSON without its writes
            from sampo import outcomes
            return json.dumps(outcomes.snapshot(p), ensure_ascii=False), False
        if name == "fail":
            text = args.get("text")
            if not isinstance(text, str) or not text.strip() or len(text) > TEXT_CAP:
                return f"text: the failure output, 1 to {TEXT_CAP} characters", True
            return _fm(p, ["fail"], text)
        if name == "exam_scorecard":  # T-2172: exit 1 means not every capability passes yet, an answer
            return _fm(p, tool[3](args, p), ok=(0, 1))
        if name == "find":  # T-2127: exit 1 means findings, an answer; the 200 worst are plenty for one turn
            argv = tool[3](args, p)
            text, err = _fm(p, argv, ok=(0, 1))
            rows = text.splitlines()
            more = f"… {len(rows) - 201} more: fm find {argv[1]} --rank --json lists all"
            return "\n".join(rows[:201] + [more] * (len(rows) > 201)), err
        return _fm(p, tool[3](args, p))
    except _Bad as e:
        return str(e), True
    except subprocess.TimeoutExpired as e:
        return f"timed out after {e.timeout:g} s", True
    except (OSError, subprocess.SubprocessError) as e:
        return f"{type(e).__name__}: {c.fit(str(e), 200)}", True


PROFILE_URI = "sampo://profile"


def _answer(p, msg):
    """The response to one message, or None for a notification."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
        return {"jsonrpc": "2.0", "id": msg.get("id") if isinstance(msg, dict) else None,
                "error": {"code": -32600, "message": "Invalid Request"}}
    method, mid, params = msg["method"], msg.get("id"), msg.get("params") or {}
    if "id" not in msg:  # notifications (initialized, cancelled): nothing to say
        return None
    if method == "initialize":
        result = {"protocolVersion": params.get("protocolVersion") or VERSION,
                  "capabilities": {"tools": {"listChanged": False}, "resources": {}},
                  "serverInfo": {"name": "foreman", "version": "1"}}
    elif method == "ping":
        result = {}
    elif method in ("resources/list", "resources/read"):  # T-3672: the profile, as a resource too
        from sampo import profile
        text = "" if c.read_meta(p).get("sensitive") else profile.full()
        if method == "resources/list":
            result = {"resources": [{"uri": PROFILE_URI, "name": "The user's profile", "mimeType": "text/markdown",
                                     "description": "The user's standing preferences (machine-local)"}] if text else []}
        elif params.get("uri") != PROFILE_URI or not text:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32002, "message": "Resource not found"}}
        else:
            result = {"contents": [{"uri": PROFILE_URI, "mimeType": "text/markdown", "text": c.redact(text)}]}
    elif method == "tools/list":
        sensitive = c.read_meta(p).get("sensitive")
        result = {"tools": [] if sensitive else [{"name": n, "description": d, "inputSchema": s} for n, d, s, _ in TOOLS]}
    elif method == "tools/call":
        text, err = _call(p, params.get("name"), params.get("arguments") or {})
        result = {"content": [{"type": "text", "text": c.redact(text)}], "isError": err}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def cmd_mcp(args):
    from sampo import cli
    if args.action == "new":
        return cmd_new(args)
    if args.action == "register":
        return cmd_register(args)
    if args.words:
        raise cli.UsageError("fm mcp takes no words: fm mcp (serve) · fm mcp new NAME --wrap 'CMD' · "
                               "fm mcp register claude [DIR]")
    p = cli.resolve(args)
    while True:
        line = sys.stdin.readline(MAX_LINE)
        if not line:
            break
        if len(line) >= MAX_LINE and not line.endswith("\n"):  # review: a line too long is refused, never held
            while (rest := sys.stdin.readline(MAX_LINE)) and not rest.endswith("\n"):
                pass
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": None, "error": {
                "code": -32600, "message": f"Invalid Request: a message over {MAX_LINE} bytes"}}) + "\n")
            sys.stdout.flush()
            continue
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            out = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        else:
            try:
                out = _answer(p, msg)
            except Exception as e:  # one bad request never ends the server
                out = {"jsonrpc": "2.0", "id": msg.get("id") if isinstance(msg, dict) else None,
                       "error": {"code": -32603, "message": f"Internal error: {type(e).__name__}"}}
        if out is not None:
            sys.stdout.write(json.dumps(out) + "\n")
            sys.stdout.flush()


# ---------------------------------------------------------------- fm mcp new / register (T-0946)
_SHELL = re.compile(r"[|;&<>$`\\\n\x00]")
# Commands a generated server never wraps, whatever its arguments: they delete, escalate or run arbitrary code
_NEVER = {"rm", "rmdir", "unlink", "shred", "dd", "truncate", "sudo", "su", "doas", "sh", "bash", "zsh", "dash", "ksh",
          "fish", "csh", "tcsh", "eval", "exec", "xargs", "env", "nohup", "kill", "killall", "pkill", "chmod", "chown",
          "reboot", "shutdown", "halt"}
# Interpreters and runners whose first free argument is code (a program, an expression, a host to run on): wrapped
# only with that argument fixed in --wrap (python3 tools/report.py, awk -f prog.awk), never bare or ending in an option
_CODE = {"python", "python2", "python3", "node", "nodejs", "deno", "bun", "perl", "ruby", "php", "lua", "luajit", "tclsh",
         "wish", "Rscript", "julia", "osascript", "pwsh", "powershell", "awk", "gawk", "mawk", "nawk", "sed", "gsed",
         "make", "gmake", "ssh", "busybox", "toybox", "nc", "ncat", "socat", "expect", "jq", "yq", "gdb", "vim", "vi",
         "nvim", "emacs", "ed", "ex", "watch", "timeout", "nice", "ionice", "time", "strace", "ltrace", "chroot",
         "unshare", "nsenter", "flock", "setsid", "stdbuf"}
# Dispatchers whose free arguments could pick a destructive subcommand: they need --sub
_DISPATCH = {"git", "npm", "pnpm", "yarn", "pip", "pip3", "uv", "cargo", "docker", "podman", "kubectl", "gh", "brew",
             "apt", "apt-get", "dnf", "pacman", "systemctl"}
_GIT_READ = {"status", "log", "diff", "show", "blame", "grep", "ls-files", "ls-tree", "rev-parse", "shortlog",
             "describe", "cat-file"}
TEMPLATE = os.path.join(c.PLUGIN_ROOT, "templates", "mcp-server")


def _refusal(argv, ctx):
    """Why a generated server can't run argv (plus caller arguments), or None."""
    import fmguard
    exe = os.path.basename(argv[0])
    if exe in _NEVER or exe.startswith("mkfs"):
        return f"{exe} deletes, escalates or runs arbitrary code"
    if exe in _CODE and (len(argv) < 2 or argv[-1].startswith("-")):
        return (f"{exe}'s next argument is code it runs: fix the script in --wrap (e.g. --wrap '{exe} tools/x') so a "
                "caller only adds plain values")
    if exe == "git" and len(argv) > 1 and argv[1] not in _GIT_READ:
        return f"git {argv[1]} isn't one of git's read-only subcommands ({', '.join(sorted(_GIT_READ))})"
    found = fmguard.check_bash(shlex.join(argv), ctx)
    return "the guard flags it: " + "; ".join(f"{cat}: {d}" for cat, d in found) if found else None


def cmd_new(args):
    from sampo import cli
    import fmguard
    name = (args.words or [""])[0]
    if len(args.words) != 1 or not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", name):
        raise cli.UsageError("fm mcp new NAME --wrap 'CMD' [--sub a,b] [--dir DIR] [--force]: NAME is lowercase "
                               "letters, digits, - and _")
    if not args.wrap or _SHELL.search(args.wrap):
        raise cli.UsageError("--wrap 'CMD': a command as plain words; a generated server runs argv, never a shell, "
                               "so no pipes, ;, &, redirections, $ or backquotes")
    try:
        wrap = shlex.split(args.wrap)
    except ValueError as e:
        raise cli.UsageError(f"--wrap: {e}")
    if wrap and "/" in wrap[0]:  # a relative ./tool would break once a harness starts the server from elsewhere
        wrap[0] = os.path.abspath(wrap[0])
    subs = list(dict.fromkeys(s.strip() for s in (args.sub or "").split(",") if s.strip()))
    bad = [s for s in subs if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,40}", s)]
    if not wrap or bad:
        raise cli.UsageError(f"--sub: plain subcommand names, comma-separated (not {', '.join(bad)})" if bad
                               else "--wrap needs a command")
    if not subs and os.path.basename(wrap[0]) in _DISPATCH and len(wrap) == 1:
        raise cli.UsageError(f"{wrap[0]} alone would let a caller pick any subcommand: name them with --sub "
                               f"(e.g. --sub status,log)")
    home = os.path.expanduser("~")
    ctx = fmguard.Ctx(cwd=os.getcwd(), project_root=fmguard.project_root_for(os.getcwd(), home), home=home,
                      foreman_home=c.foreman_home(), state_dir=c.state_dir(), state_fallbacks=c.state_fallbacks())
    for argv in [wrap + [s] for s in subs] or [wrap]:
        why = _refusal(argv, ctx)
        if why:
            raise cli.UsageError(f"refused: {shlex.join(argv)}: {why}. A generated MCP server never wraps a "
                                   "destructive command")
    d = os.path.abspath(args.dir or name)
    files = ["server.py", "test_server.py"]
    if os.path.exists(d) and (not os.path.isdir(d) or os.listdir(d) and not args.force):
        raise cli.UsageError(f"{d} isn't an empty folder; fm mcp new writes {' and '.join(files)} there "
                               "(--force writes them anyway, replacing those two)")
    os.makedirs(d, exist_ok=True)
    desc = (f"Runs `{shlex.join(wrap)}{' SUB' if subs else ''} ARGS…` (argv only, never a shell; each argument a "
            "plain value, never an option).")
    fill = {'"__NAME__"': json.dumps(name), '["__WRAP__"]': json.dumps(wrap), '["__SUBS__"]': json.dumps(subs),
            '"__DESCRIPTION__"': json.dumps(desc)}
    for f in files:
        with open(os.path.join(TEMPLATE, f), encoding="utf-8") as src:
            text = src.read()
        for k, v in fill.items():
            text = text.replace(k, v, 1)
        c.write_atomic(os.path.join(d, f), text)
    os.chmod(os.path.join(d, "server.py"), 0o755)
    from sampo import toolproof  # T-2372: the active task must prove this server before it closes
    toolproof.built(name, os.path.join(d, "server.py"), os.path.join(d, "test_server.py"), kind="mcp")
    print(f"Wrote a stdlib MCP server for `{shlex.join(wrap)}`" + (f" (subcommands: {', '.join(subs)})" if subs else "")
          + ":\n" + "\n".join(f"  {os.path.join(d, f)}" for f in files)
          + f"\nTest it: python3 {shlex.quote(os.path.join(d, 'test_server.py'))}"
          + f"\nRegister it: fm mcp register claude {shlex.quote(d)}")


def cmd_register(args):
    from sampo import cli
    harness, rest = (args.words or [""])[0], (args.words or [])[1:]
    if harness == "desktop" and not rest:  # T-2232: Claude Desktop starts servers with no shell PATH and no project
        p = cli.resolve(args)            # folder, so the stanza names fm by path and the project with -p
        fm = shutil.which("fm") or os.path.join(c.PLUGIN_ROOT, "bin", "fm")
        stanza = {"mcpServers": {"foreman": {"command": os.path.abspath(fm), "args": ["mcp", "-p", p.slug]}}}
        return print("Add this to Claude Desktop's claude_desktop_config.json (merge into an existing mcpServers), "
                     "then restart Desktop:\n" + json.dumps(stanza, indent=2))
    if harness not in ("claude", "codex", "gemini") or len(rest) > 1:
        raise cli.UsageError("fm mcp register claude|codex|gemini|desktop [DIR]: DIR is a folder fm mcp new wrote "
                               "(default: Sampo's own server)")
    name, cmd = "foreman", ["fm", "mcp"]
    if rest:
        server = os.path.join(os.path.abspath(rest[0]), "server.py")
        if not os.path.isfile(server):
            raise cli.UsageError(f"no server.py in {rest[0]} (fm mcp new writes one)")
        with open(server, encoding="utf-8", errors="replace") as f:
            m = re.search(r'^NAME = "([a-z][a-z0-9_-]{0,39})"$', f.read(), re.M)
        name, cmd = (m.group(1) if m else os.path.basename(os.path.dirname(server))), ["python3", server]
    if harness == "claude":  # the form in Claude Code's MCP docs: claude mcp add NAME -- COMMAND [ARGS…]
        print(f"claude mcp add {shlex.quote(name)} -- {shlex.join(cmd)}")
        try:  # T-2374: adding the server is the user's grant to give: queue it on the active task, grant nothing
            from sampo import grants
            p = cli.resolve(args, create=False)
            act = c.active_brief(c.load_briefs(p), p.lane)
            if act and grants.queue(p, act.id, ["plugin"], f"register the {name} MCP server with Claude Code",
                                      what=f"mcp:{name}", session=os.environ.get("CLAUDE_SESSION_ID"))[0] == "queued":
                print(f"Plugin request for mcp:{name} queued for the user on {act.id} (fm todo).")
        except cli.UsageError:
            pass
        return
    # T-0946: only formats confirmed in the harness's current docs are printed; none is confirmed here yet
    print(f"No {harness} registration format is confirmed against {harness}'s current docs, so none is printed: check "
          f"its MCP docs and register this stdio server as {name!r}, command: {shlex.join(cmd)}"
          + ("" if rest else f"\n(fm agents install {harness} already wires Sampo's own server into its config.)"))
