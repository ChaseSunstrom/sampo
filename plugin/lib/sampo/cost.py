"""fm cost / fm usage (T-0064, T-0056): where the tokens went — per session, per task and per tool — read from Claude
Code's own transcripts, and which Sampo skills, playbooks and commands actually get used. Read-only; stdlib only."""
import bisect
import collections
import datetime
import hashlib
import itertools
import json
import os
import re
import statistics
import time

from sampo import core as c

WEIGHTS = {"input_tokens": 1, "cache_creation_input_tokens": 1.25, "cache_read_input_tokens": 0.1, "output_tokens": 5}
WRITE_1H = 2.0  # T-1820: a 1 h cache write costs 2× base input; WEIGHTS' 1.25 is the 5 min cache's price
SHORT = {"input_tokens": "input", "cache_creation_input_tokens": "cache writes", "cache_read_input_tokens":
         "cache reads", "output_tokens": "output"}


IMAGE_CHARS = 6400  # ponytail: an image is at most ~1,600 tokens (≈4 chars each); read its size if that matters


def _size(content):
    """A tool result's context cost in characters: an image its token cost, not its base64 length (T-0194)."""
    if isinstance(content, list):
        return sum(IMAGE_CHARS if isinstance(x, dict) and x.get("type") == "image" else len(json.dumps(x))
                   for x in content)
    return len(json.dumps(content))


def transcripts_dir(root):
    """Claude Code keeps a project's transcripts under ~/.claude/projects/<its path, non-alphanumerics as '-'>."""
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    return os.path.join(base, "projects", re.sub(r"[^A-Za-z0-9]", "-", root))


def project_folders(p):
    """T-3921 review (T-3938), moved here by T-5950 so repeats and the per-lane report share it: a project's sessions run
    in its worktrees and lanes, each with its own transcript folder (`<root>--claude-worktrees-…`, `<root>-lanes-…`), so a
    walk reads the main checkout's family of folders."""
    base = transcripts_dir(c.main_worktree(p.root) or p.root)
    parent, name = os.path.split(base)
    try:
        names = sorted(os.listdir(parent))
    except OSError:
        return [base]
    return [os.path.join(parent, n) for n in names
            if n == name or n.startswith((name + "--claude-worktrees-", name + "-lanes-"))]


def _since(days):
    return (c.clock() - datetime.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")


def one_hour(u):
    """A usage row's 1 h cache writes (Claude Code logs them under cache_creation)."""
    cc = u.get("cache_creation")
    return (cc.get("ephemeral_1h_input_tokens") or 0) if isinstance(cc, dict) else 0


def weighted(u):
    """A usage row in input-equivalent tokens."""
    return (sum((u.get(k) or 0) * w for k, w in WEIGHTS.items())
            + one_hour(u) * (WRITE_1H - WEIGHTS["cache_creation_input_tokens"]))


def _human(n):
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}k" if n >= 1e3 else f"{n:.0f}"


_TS = re.compile(r'"timestamp":\s*"([^"]{19})')
BLOB = '{"type":"api-request-blob"'  # T-3521: a request's body, with no top-level timestamp: no reader here uses one
_BLOB = BLOB  # the old name (T-5973 made it public for targets.py)


def mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


_mtime = mtime  # the old name (T-5973)


def _entries(folder, since, need=None, newest=False, context=False):
    """(file path, entry) for each transcript line on or after the cutoff. T-1116: a file not written since the
    cutoff is skipped unopened, and a line is parsed only when one of its timestamps reaches the cutoff (the newest
    counts: a nested older one can't hide the entry's own). T-3193: `need`, a substring a wanted line always holds,
    skips the rest before any regex or parse. T-3521: request blobs are skipped unparsed, and the line's last stamp (the
    entry's own) is read first: only when it is older are all of them searched. T-3921: newest, the files newest first
    (a reader that stops early keeps the latest). T-4825: context, every line of a file written since the cutoff (a
    reader that needs what the older lines left in context)."""
    cutoff = since[:19]
    try:
        cut = datetime.datetime.strptime(since[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=datetime.timezone.utc).timestamp()
    except ValueError:
        cut = 0
    roots = [folder] if isinstance(folder, str) else folder  # T-3921: a project's worktrees and lanes, one list
    paths = [os.path.join(d, n) for root in roots for d, _, ns in os.walk(root) for n in ns if n.endswith(".jsonl")]
    if newest:
        paths.sort(key=mtime, reverse=True)
    for path in paths:
        try:
            if os.path.getmtime(path) < cut:
                continue
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if need and need not in line or line.startswith(BLOB):
                        continue
                    last = _TS.match(line, max(line.rfind('"timestamp":'), 0))
                    if not context and not (last and last.group(1) >= cutoff):
                        stamps = _TS.findall(line)
                        if stamps and max(stamps) < cutoff:
                            continue
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(e, dict):
                        yield path, e
        except OSError:
            continue


_TWO_WORD = ("task", "bench", "lib", "lane")  # verbs whose subcommand names the cost


def _fm_verb(cmd):
    """T-1276: the fm verb of a Bash command made of fm calls alone ("task show"; a cd, a redirect and a trailing tail,
    head or grep allowed), else None: 'fm task log … && python3 -m unittest' is a test run's output, not Sampo's.
    ponytail: split on && || ; outside quotes too, so a quoted ';' undercounts a command, never overcounts."""
    verb = None
    for seg in re.split(r"\s*(?:&&|\|\||;)\s*", cmd.strip()):
        if re.fullmatch(r"cd \S+", seg):
            continue
        seg = re.sub(r"\s*(?:2>&1\s*)?\|\s*(?:tail|head|grep)\b[^|]*$", "", seg)
        m = re.match(r"(?:fm|sampo)\s+(?:quiet\s+(?:--\S+\s+\S+\s+)*--\s+(?:(?:fm|sampo)\s+)?)?([\w-]+)(?:\s+([\w-]+))?", seg)
        if not m or "|" in seg:
            return None
        verb = verb or m[1] + (f" {m[2]}" if m[1] in _TWO_WORD and m[2] else "")
    return verb
_FM_NOTE = re.compile(r"\b(?:Foreman|Sampo|Ward|Smith|Rune|Louhi)\b|^Ambient tests:")  # how every note of the hooks opens (Foreman: older transcripts)
_STYLE_NOTE = " output style is active. Remember to follow the specific guidelines for this style."


def _share(a, share):
    """T-1257: add a transcript attachment's characters to share["all"], and Sampo's part of them to its source."""
    def add(source, n, ours):
        share["all"] += n
        if ours:
            share[source] += n
    t = a.get("type")
    if t in ("hook_additional_context", "hook_system_message", "hook_blocking_error"):
        body = a.get("content") or a.get("blockingError") or ""
        body = "\n".join(map(str, body)) if isinstance(body, list) else str(body)
        add("hook notes", len(body), bool(_FM_NOTE.search(body[:80])))
    elif t in ("skill_listing", "agent_listing_delta"):
        rows = str(a.get("content") or "").splitlines() if t == "skill_listing" else a.get("addedLines") or []
        for row in map(str, rows):
            add("skill listing" if t == "skill_listing" else "agent listing", len(row) + 1, row.startswith(tuple("- " + n for n in c.ns_names(""))))
    elif t == "output_style_instructions":
        style = a.get("style") if isinstance(a.get("style"), dict) else {}
        add("output style", len(str(style.get("prompt") or "")), str(style.get("name")).startswith(c.ns_names("")))
    elif t == "output_style":  # the reminder Claude Code renders each turn around the style's name
        add("output style", len(str(a.get("style"))) + len(_STYLE_NOTE), str(a.get("style")).startswith(c.ns_names("")))
    elif t == "instructions":
        for f in a.get("files") or []:
            add("rules", len(str(f.get("content") or "")), str(f.get("path") or "").endswith(tuple("rules/" + n for n in c.RULES_NAMES)))  # T-5048: the link's path or the file's
    else:  # ponytail: any other attachment counts by its JSON size, a rough measure of what it adds
        share["all"] += len(json.dumps(a))


BIG_RESULT = 6000  # T-4704: a tool result over this many characters is counted apart
_LABEL = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,31}")
_HOOK_ATTACHMENTS = ("hook_additional_context", "hook_system_message", "hook_blocking_error")


def _iso_week(ts, memo={}):
    """T-4704: "2026-W46" for a transcript stamp, None for one that is no date (memoised per day)."""
    day = ts[:10]
    if day not in memo:
        try:
            y, w, _ = datetime.date.fromisoformat(day).isocalendar()
            memo[day] = f"{y}-W{w:02d}"
        except ValueError:
            memo[day] = None
    return memo[day]


def _label(name):
    """A tool or attachment name as a source row: MCP tools fold to one row, and a name that is not a plain word (a
    transcript could carry anything) shows as "other", so the section prints no transcript text."""
    name = str(name or "")
    if name.startswith("mcp__"):
        return "MCP"
    return name if _LABEL.fullmatch(name) else "other"


def _entering(a):
    """T-4704: (source, characters) of one transcript attachment."""
    if a.get("type") in _HOOK_ATTACHMENTS:
        body = a.get("content") or a.get("blockingError") or ""
        return "hook notes", len("\n".join(map(str, body)) if isinstance(body, list) else str(body))
    return "attachment: " + _label(a.get("type")), len(json.dumps(a))


def _entering_blocks(x, week, seen, role):
    """T-4704: add one content block's characters to a week's Counter by source (reply text, tool-call input, a
    reminder against the user's own text; a tool result is added by scan, which sizes it once)."""
    t = x.get("type")
    if t == "tool_use":
        if x.get("id") not in seen:  # a reply split over lines may repeat a block
            seen.add(x.get("id"))
            week["tool-call inputs"] += len(json.dumps(x.get("input") or {}))
    elif t == "text":
        text = str(x.get("text") or "")
        week["reply text" if role == "assistant" else "reminders" if text.lstrip().startswith(
            "<system-reminder>") else "user text"] += len(text)


TRIGGERS = ("user", "tool result", "agent finished", "agent waiting", "shell finished", "monitor event",
            "other notification", "stop hook", "compaction", "other")  # T-4815: what started a turn, most specific first
# a gap holding several (a person typing while a tool ran) takes the first of these
_RANK = ("user", "compaction", "stop hook", "agent waiting", "monitor event", "shell finished", "agent finished",
         "other notification", "tool result", "other")
AVOIDABLE = ("agent waiting", "monitor event", "stop hook", "other notification")  # turns nobody asked for
_WAITING = re.compile(r"\bis waiting\b.{0,80}\bbackground\b", re.S)  # the harness's own words for an agent that ended its turn on its own background work: the one place no field says it
_SUMMARY = re.compile(r"<summary>(.{0,400}?)</summary>", re.S)


def _notification(text):
    """T-4815: a background-task notification's kind from the wrapper's own tags (event: a monitor's; usage or
    subagent_tokens: an agent's; output-file: a shell command's). An agent that only says it is waiting on its own
    background work is told apart by the harness's summary words (_WAITING), the one text match here."""
    if not isinstance(text, str):
        return "other notification"
    ev = text.find("<event>")
    agent = [i for i in map(text.find, ("<result>", "<subagent_tokens>", "<usage>")) if i >= 0]
    if ev >= 0 and not (agent and min(agent) < ev):  # the wrapper's own tag comes first: an agent's result may quote one
        return "monitor event"
    if agent:
        m = _SUMMARY.search(text)
        return "agent waiting" if m and _WAITING.search(m.group(1)) else "agent finished"
    return "shell finished" if "<output-file>" in text else "other notification"


def trigger_of(e):
    """T-4815: what one transcript record contributes toward starting the next reply (None: nothing), from its type and
    fields: a user line's origin (human, task-notification) and tool_result blocks, a Stop-event hook_blocking_error
    attachment (its meta user line is the continuation), a compact boundary. No message text is read but a
    notification's wrapper (see _notification)."""
    t = e.get("type")
    if t == "user":
        m, o = e.get("message"), e.get("origin")
        content = m.get("content") if isinstance(m, dict) else None
        kind = o.get("kind") if isinstance(o, dict) else None
        if e.get("isCompactSummary"):
            return "compaction"
        if kind == "task-notification":
            return _notification(content)
        if kind == "human":
            return "user"
        if e.get("isMeta"):
            return "other"
        if isinstance(content, list):
            types = {x.get("type") for x in content if isinstance(x, dict)}
            return "tool result" if "tool_result" in types else "user" if types & {"text", "image"} else "other"
        return "user" if isinstance(content, str) else "other"
    if t == "attachment":
        a = e.get("attachment")
        if isinstance(a, dict) and a.get("type") == "hook_blocking_error" and a.get("hookEvent") == "Stop":
            return "stop hook"
    elif t == "system" and e.get("subtype") == "compact_boundary":
        return "compaction"
    return None


def trigger_rows(msgs):
    """T-4815: {kind: {"turns", "cache_read"}} over scan(kinds=True)'s messages, most re-read first."""
    rows = {}
    for _, _, u, kind in msgs:
        r = rows.setdefault(kind, {"turns": 0, "cache_read": 0})
        r["turns"] += 1
        r["cache_read"] += u.get("cache_read_input_tokens") or 0
    return dict(sorted(rows.items(), key=lambda kv: -kv[1]["cache_read"]))


def scan(folder, since, share=None, sidechains=True, entries=None, ctx=None, kinds=False):
    """(assistant messages [(ts, session, usage)], tool result chars by tool name) since the cutoff. A message's usage
    repeats on each of its content blocks: counted once per message id. A `share` Counter gets what entered the
    context: "all", and Sampo's part by source (T-1257). A subagent's replies carry its parent's session id:
    `sidechains=False` leaves them out (T-1797). `entries`: _entries already read (T-3521: fm cost reads once).
    T-4704: a `ctx` dict gets, per ISO week, a Counter of the characters that entered by source (same pass).
    T-4815: `kinds` adds a fourth item to each message, what started its turn (TRIGGERS), read in the same pass from
    the records between it and the previous reply of the same file."""
    msgs, tools, names, fm_ids, seen = {}, collections.Counter(), {}, {}, set()
    started, gap, gap_path = {}, set(), None
    for path, e in _entries(folder, since) if entries is None else entries:
        if not sidechains and f"{os.sep}subagents{os.sep}" in path:
            continue
        n = os.path.basename(path)
        ts = str(e.get("timestamp") or "")
        if kinds and ts[:19] >= since:
            if path != gap_path:
                gap, gap_path = set(), path  # a new file: the records of the last one started nothing here
            if e.get("type") == "assistant":
                key = (e.get("message") or {}).get("id") or (n, ts) if isinstance(e.get("message"), dict) else (n, ts)
                if key not in started:  # a reply split over lines: the first line took the gap
                    started[key] = next((k for k in _RANK if k in gap), "other")
                    gap = set()
            elif (k := trigger_of(e)):
                gap.add(k)
        week = None
        if ctx is not None and ts[:19] >= since and (week := _iso_week(ts)):
            week = ctx.setdefault(week, collections.Counter())
            if isinstance(e.get("attachment"), dict):
                source, size = _entering(e["attachment"])
                week[source] += size
        if share is not None and isinstance(e.get("attachment"), dict) and ts[:19] >= since:
            _share(e["attachment"], share)
        m = e.get("message")
        if not isinstance(m, dict) or ts[:19] < since:
            continue
        if week is not None and isinstance(m.get("content"), str):  # a typed prompt arrives as a plain string
            week["reply text" if e.get("type") == "assistant" else "reminders" if m["content"].lstrip().startswith(
                "<system-reminder>") else "user text"] += len(m["content"])
        content = m.get("content") if isinstance(m.get("content"), list) else []
        for x in content:
            if not isinstance(x, dict):
                continue
            if week is not None:
                _entering_blocks(x, week, seen, e.get("type"))
            if x.get("type") == "tool_use":
                names[x.get("id")] = x.get("name") or "?"
                verb = _fm_verb(str((x.get("input") or {}).get("command") or "")) if x.get("name") == "Bash" else None
                if verb:
                    fm_ids[x.get("id")] = verb
            elif x.get("type") == "tool_result":
                size = _size(x.get("content"))
                tools[names.get(x.get("tool_use_id"), "?")] += size
                if week is not None:
                    name = names.get(x.get("tool_use_id"))
                    week[_label(name) if name else "unknown tool"] += size
                    week["#big"] += size > BIG_RESULT
                if share is not None:
                    share["all"] += size
                    if x.get("tool_use_id") in fm_ids:
                        share["fm output"] += size
                        share["fm:" + fm_ids[x["tool_use_id"]]] += size  # by verb (T-1276)
        if e.get("type") == "assistant" and isinstance(m.get("usage"), dict):
            msgs[m.get("id") or (n, ts)] = (ts, e.get("sessionId") or n[:-6], m["usage"])
    if kinds:
        return [row + (started.get(key, "other"),) for key, row in msgs.items()], tools
    return list(msgs.values()), tools


_WRAPPERS = {"timeout", "nice", "env", "time", "stdbuf", "ionice", "command", "exec"}
_LEAD_SKIP = {"cd", "set", "export"}
_SHELL_ARG = re.compile(r"-\S*|\d+[smhd]?|[A-Za-z_]\w*=\S*")  # a wrapper's options, its numbers, an env assignment
_WORD = re.compile(r"[\w][\w.+-]{0,39}")


def _words(seg):
    import shlex
    try:
        return shlex.split(seg)
    except ValueError:  # an unbalanced quote: the plain split names the program well enough
        return seg.split()


def _after_options(words, takes=()):
    """The first word that is not an option (or an option's value, for those in `takes`)."""
    skip = False
    for w in words:
        if skip:
            skip = False
        elif w in takes:
            skip = True
        elif not w.startswith("-"):
            return w
    return ""


_VERBS = []


def _sampo_kind(rest):
    """"sampo VERB" when the first word after the options is one of sampo's verbs, else "sampo" (an option's value,
    as in `sampo -p NAME task show`, is not a verb)."""
    if not _VERBS:
        import argparse
        from sampo import cli
        sub = next(a for a in cli.build_parser()._actions if isinstance(a, argparse._SubParsersAction))
        _VERBS.append(frozenset(sub.choices))
    verb = _after_options(rest)
    return f"sampo {verb}" if verb in _VERBS[0] and _WORD.fullmatch(verb) else "sampo"


def _bash_kind(cmd):
    """T-4825: what a Bash command ran, as a short kind: leading cd/set/export segments, env assignments and wrappers
    (timeout, nice, …, with their numbers) dropped; the tests runner, a sampo verb, a git subcommand, python's inline or
    script use, else the program's basename. Never more than the program and a verb: no path, argument or text."""
    for seg in re.split(r"&&|;|\n", cmd or ""):
        words = _words(seg)
        while words and (os.path.basename(words[0]) in _WRAPPERS or re.fullmatch(r"[A-Za-z_]\w*=.*", words[0])):
            wrapper = os.path.basename(words[0]) in _WRAPPERS
            words = words[1:]
            while wrapper and words and _SHELL_ARG.fullmatch(words[0]):
                words = words[1:]
        if not words or words[0].startswith("#") or os.path.basename(words[0]) in _LEAD_SKIP:
            continue
        prog = os.path.basename(words[0])
        rest = words[1:]
        if re.fullmatch(r"python[0-9.]*", prog):
            i = 0
            while i < len(rest) and rest[i].startswith("-") and rest[i] != "-":  # interpreter options before the script
                if rest[i] == "-c":
                    return "python3 inline"
                if rest[i] == "-m":
                    return "tests" if rest[i + 1:i + 2] in (["unittest"], ["pytest"]) else "python3 module"
                i += 2 if rest[i] in ("-W", "-X") else 1
            first = rest[i] if i < len(rest) else ""
            if first == "-":
                return "python3 inline"
            if first.endswith("tests/run.py"):
                return "tests"
            if os.path.basename(first) in ("sampo", "fm"):
                return _sampo_kind(rest[i + 1:])
            return "python3 script" if first.endswith(".py") else "python3"
        if prog in ("sampo", "fm"):
            return _sampo_kind(rest)
        if prog == "git":
            verb = _after_options(rest, ("-C", "-c"))
            return f"git {verb}" if _WORD.fullmatch(verb) else "git"
        if prog in ("bash", "sh"):
            return "bash inline" if "-c" in rest else "bash script" if _after_options(rest) else prog
        return prog if _WORD.fullmatch(prog) else "other"
    return "shell"


def _tool_kind(name, inp):
    if not isinstance(name, str):
        return "unknown"
    if name.startswith("mcp__"):
        return "mcp"
    if name == "Bash":
        return _bash_kind(str((inp or {}).get("command") or "") if isinstance(inp, dict) else "")
    return name if _WORD.fullmatch(name) else "other"


_NO_CONTEXT = ("prompt_snapshot", "deferred_tools_record")  # T-4830: sent to no model call, so in no context
_UI_ONLY = ("hook_non_blocking_error", "command_permissions")  # T-4830: shown to the user, never sent to the model


def _field(v):
    """T-4830: the characters of one transcript field that the model reads: a string's length, a list's items, a dict's
    content (or its text, or its blocking error), and a dict with none of those counted as its JSON. A number, a flag or
    nothing is no text."""
    if isinstance(v, str):
        return len(v)
    if isinstance(v, list):
        return sum(_field(x) for x in v)
    if isinstance(v, dict):
        for key in ("content", "text", "blockingError"):
            if key in v:
                return _field(v[key])
        return len(json.dumps(v))
    return 0


# T-4830: the fields whose text a context holds, by attachment type; a type not here counts its non-metadata fields
_MODEL_FIELDS = {
    "skill_listing": ("content",),
    "instructions": ("content", "files"),
    "queued_command": ("prompt",),  # a user's steer, or a task notification the model reads
    "edited_text_file": ("filename", "snippet"),
    "agent_listing_delta": ("addedTypes", "removedTypes"),
}
_META_KEYS = frozenset(("type", "uuid", "parentUuid", "sessionId", "toolUseID", "isSidechain", "entrypoint", "cwd",
                        "version", "gitBranch", "slug", "userType", "requestId", "stdout", "stderr", "exitCode",
                        "durationMs", "command", "updatedToolOutput", "updatedMCPToolOutput"))


def _is_meta(key):
    """T-4830: a field that is the record's own bookkeeping, not text: a type, an id, a timestamp, or a hook's output."""
    k = str(key).lower()
    return key in _META_KEYS or k == "id" or k.endswith(("id", "uuid")) or "time" in k or k.startswith("hook")


def _model_attachment(a):
    """T-4830: (source, characters) of the text one attachment puts in a context, or None when it puts none there: a
    hook's UI-only record, a prompt snapshot, a deferred-tool record. A hook note counts its content or blocking error;
    a mapped type counts its mapped fields; any other type counts its non-metadata fields, so it is never silently 0."""
    kind = a.get("type")
    if kind in _NO_CONTEXT or kind in _UI_ONLY:
        return None
    if kind in _HOOK_ATTACHMENTS or kind == "hook_success":
        source, n = "hook notes", _field(a.get("content")) or _field(a.get("blockingError"))
    elif kind in _MODEL_FIELDS:
        source, n = "attachment: " + _label(kind), sum(_field(a.get(f)) for f in _MODEL_FIELDS[kind])
    else:
        source, n = "attachment: " + _label(kind), sum(_field(v) for k, v in a.items() if not _is_meta(k))
    return (source, n) if n else None


def _count(v):
    """A usage count from a transcript line: a whole number, else 0 (a corrupt line is not a crash)."""
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 0


def _settle(reply, parts):
    """T-4830: a reply's parts join the context when the next reply starts: its text and its tool-call inputs, and, when
    the reply holds a thinking block, its thinking, estimated as its output tokens at 4 characters less those two (the
    transcripts keep no thinking text, and a reply without a block has none to estimate)."""
    if reply is None:
        return
    if reply["text"]:
        parts["reply text"] += reply["text"]
    if reply["input"]:
        parts["tool input"] += reply["input"]
    thinking = max(0, reply["out"] * 4 - reply["text"] - reply["input"]) if reply["thinking"] else 0
    if thinking:
        parts["thinking"] += thinking


def _user_kind(e, text):
    """T-4830: what a user entry's text is in context: a compact summary, a task notice, a reminder, or the user's own."""
    if e.get("isCompactSummary"):
        return "compact summary"
    origin = e.get("origin")
    if isinstance(origin, dict) and origin.get("kind") == "task-notification":
        return "task notice"
    return "reminders" if text.lstrip().startswith("<system-reminder>") else "user text"


def _side_tokens(counter, who):
    """T-4830: one side's re-read tokens, each kind rounded as its row is, so the per-call figures add up to the rows."""
    return sum(round(v / 4) for k, v in counter.items() if k.split(" ", 1)[0] == who)


def outputs_reread(folder, since, entries=None):
    """T-4825: tool outputs weighed by how often they are re-read. Per file, in order: a running count of the live
    tool-result characters by kind; each assistant call (once per message id) adds every live kind's characters to that
    kind's re-read total and its cache-read tokens to its side's; a compact_boundary clears the live counts. A result's
    kind is its tool_use's tool (every MCP tool "mcp"; Bash by command word); main thread or sub by the file's place.
    The cutoff limits only the totals: older lines of a file still fill the live counts and kinds. A message id counts
    once across all files (a resumed session copies history into a new one). One line at a time; memory is one file's
    tool ids and kinds plus 8 bytes per call. Returns the figures; no text of the transcripts.
    T-4830: the other parts in context (a reply's text, its tool inputs and, for a reply with a thinking block, its
    thinking; attachments by the text they send, never a hook's UI-only stdout; the user's text and reminders; a compact
    summary; a task notice; an image) are re-read the same way, as parts. Each side's
    per-call figures split its context read into tool outputs, other parts, and what the transcripts do not hold."""
    side = {"main": collections.Counter(), "sub": collections.Counter()}
    reread, results, chars = collections.Counter(), collections.Counter(), collections.Counter()
    parts_reread = collections.Counter()
    cur, live, live_parts, kinds_of, reply = None, collections.Counter(), collections.Counter(), {}, None
    seen = set()  # across files, as scan() dedupes
    for path, e in _entries(folder, since, context=True) if entries is None else entries:
        if path != cur:  # a file's own context: a resumed session's copy of the history replays it from the top
            cur, live, live_parts, kinds_of, reply = path, collections.Counter(), collections.Counter(), {}, None
        ts = str(e.get("timestamp") or "")
        in_window = ts[:19] >= since  # the cutoff limits the totals, not what is already in context
        who = "sub" if f"{os.sep}subagents{os.sep}" in path else "main"
        if e.get("type") == "system" and e.get("subtype") == "compact_boundary":
            live.clear()
            live_parts.clear()
            reply = None  # the reply still open is in the context that the compaction drops
            continue
        if e.get("type") in _NO_CONTEXT:
            continue
        if e.get("type") == "attachment":
            got = _model_attachment(e["attachment"]) if isinstance(e.get("attachment"), dict) else None
            if got:
                live_parts[got[0]] += got[1]
            continue
        m = e.get("message")
        if not isinstance(m, dict):
            continue
        if e.get("type") == "assistant":
            mid = m.get("id") if isinstance(m.get("id"), str) and m["id"] else f"{path}\0{ts}"
            if reply is None or reply["id"] != mid:  # a reply's entries are consecutive: the last one's parts settle here
                _settle(reply, live_parts)
                reply = {"id": mid, "text": 0, "input": 0, "out": 0, "tools": set(), "thinking": False}
            content = m.get("content") if isinstance(m.get("content"), list) else []
            for x in content:
                if not isinstance(x, dict):
                    continue
                if x.get("type") == "tool_use" and isinstance(x.get("id"), str):
                    kinds_of[x["id"]] = _tool_kind(x.get("name"), x.get("input"))
                    if x["id"] not in reply["tools"]:  # a reply split over lines may repeat a block
                        reply["tools"].add(x["id"])
                        reply["input"] += len(json.dumps(x.get("input") or {}))
                elif x.get("type") == "text":
                    reply["text"] += len(str(x.get("text") or ""))
                elif x.get("type") in ("thinking", "redacted_thinking"):
                    reply["thinking"] = True  # the block is the one sign that the reply's output holds thinking
            usage = m.get("usage")
            if isinstance(usage, dict):
                reply["out"] = max(reply["out"], _count(usage.get("output_tokens")))
                if in_window:
                    key = hashlib.blake2b(mid.encode("utf-8", "replace"), digest_size=8).digest()  # bounded memory
                    if key not in seen:
                        seen.add(key)
                        side[who]["calls"] += 1
                        side[who]["cache_read"] += _count(usage.get("cache_read_input_tokens"))
                        for kind, n in live.items():
                            reread[f"{who} {kind}"] += n
                        for kind, n in live_parts.items():
                            parts_reread[f"{who} {kind}"] += n
        elif isinstance(m.get("content"), str):
            live_parts[_user_kind(e, m["content"])] += len(m["content"])
        elif isinstance(m.get("content"), list):
            for x in m["content"]:
                if not isinstance(x, dict):
                    continue
                if x.get("type") == "tool_result":
                    tid = x.get("tool_use_id")
                    kind = kinds_of.get(tid, "unknown") if isinstance(tid, str) else "unknown"
                    n = _size(x.get("content"))
                    live[kind] += n
                    if in_window:
                        results[f"{who} {kind}"] += 1
                        chars[f"{who} {kind}"] += n
                elif x.get("type") == "text":
                    text = str(x.get("text") or "")
                    live_parts[_user_kind(e, text)] += len(text)
                elif x.get("type") == "image":
                    live_parts["image"] += IMAGE_CHARS
    cache = side["main"]["cache_read"] + side["sub"]["cache_read"]
    rows = [{"kind": k, "results": results[k], "avg_tokens": round(chars[k] / results[k] / 4) if results[k] else 0,
             "reread_tokens": round(v / 4), "share_of_cache_read": round(100 * v / 4 / cache, 1) if cache else 0.0}
            for k, v in reread.most_common()]
    total = sum(r["reread_tokens"] for r in rows)
    parts = [{"kind": k, "reread_tokens": round(v / 4), "share_of_cache_read": round(100 * v / 4 / cache, 1) if cache else 0.0}
             for k, v in parts_reread.most_common()]
    parts_total = sum(r["reread_tokens"] for r in parts)
    figures = {}
    for who in ("main", "sub"):
        calls = side[who]["calls"]
        avg = side[who]["cache_read"] // (calls or 1)
        tools = round(_side_tokens(reread, who) / calls) if calls else 0
        others = round(_side_tokens(parts_reread, who) / calls) if calls else 0
        figures[who] = {"calls": calls, "cache_read_tokens": side[who]["cache_read"], "avg_context_read": avg,
                        "tool_outputs_per_call": tools, "parts_per_call": others,
                        "unaccounted_per_call": max(0, avg - tools - others)}
    return {"main": figures["main"], "sub": figures["sub"],
            "tool_output_reread_tokens": total,
            "share_of_cache_read": round(100 * total / cache, 1) if cache else 0.0, "kinds": rows,
            "parts_reread_tokens": parts_total,
            "parts_share_of_cache_read": round(100 * parts_total / cache, 1) if cache else 0.0, "parts": parts}


def _outputs(folder, args, cli):
    got = {"days": args.days, **outputs_reread(folder, _since(args.days))}
    if not (got["main"]["calls"] or got["sub"]["calls"]):
        return cli.out(args, got, f"No assistant calls in {folder} for the last {args.days:g} day(s).")
    def side(k):
        g = got[k]
        return (f"{g['calls']:,} calls, {_human(g['cache_read_tokens'])} cache-read tokens, "
                f"{_human(g['avg_context_read'])} context read per call\n"
                f"        per call: {_human(g['tool_outputs_per_call'])} tool outputs, {_human(g['parts_per_call'])} other "
                f"parts, {_human(g['unaccounted_per_call'])} not in the transcripts (system prompt, tool schemas)")
    text = (f"Context re-read, last {args.days:g} day(s) (tokens ≈ characters / 4; each output or part counted again on "
            f"every later call until a compaction):\n  main: {side('main')}\n  sub:  {side('sub')}\n"
            f"  tool outputs re-read: {_human(got['tool_output_reread_tokens'])} tokens, "
            f"{got['share_of_cache_read']:g}% of all cache reads\nLargest, by re-read (results, average size, share of cache "
            "reads):\n" + "\n".join(f"  {r['kind']}: {r['results']:,} results, {_human(r['avg_tokens'])} each, "
                                    f"{_human(r['reread_tokens'])} re-read, {r['share_of_cache_read']:g}%"
                                    for r in got["kinds"][:15])
            + f"\n  other parts re-read: {_human(got['parts_reread_tokens'])} tokens, "
            f"{got['parts_share_of_cache_read']:g}% of all cache reads\nLargest other parts, by re-read (share of cache "
            "reads):\n" + "\n".join(f"  {r['kind']}: {_human(r['reread_tokens'])} re-read, {r['share_of_cache_read']:g}%"
                                    for r in got["parts"][:15]))
    return cli.out(args, got, text)


def _result_text(content):
    """The text of a tool result, for a marker test: a string, or a list's text blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(x.get("text") or "") for x in content if isinstance(x, dict))
    return ""


def repeat_reads(folder, since, entries=None, top=10):
    """T-6064: the Read results that gave the model what it already held. Per transcript file, in order, a Read of the same
    file, offset and limit as an earlier one since the last compact_boundary, whose result is the same characters, is a
    repeat: its size is repeat bytes (the whole repeat is the waste; no change was made). A result carrying the Read hook's
    unchanged or changed note is a note served: bytes avoided are the size of the result it stood in for (the earlier
    read of that range) less the note's own. Main thread against subagents; per session the biggest few, by a short id
    (never a path). The cutoff limits only the totals: older lines still fill the live state. One line at a time; memory
    is a hash and a size per distinct (file, range) of one transcript."""
    tally = _RepeatReads(since, top)
    for path, e in _entries(folder, since, context=True) if entries is None else entries:
        tally.feed(path, e)
    return tally.result()


class _RepeatReads:
    """T-6064: repeat_reads one entry at a time, so `sampo cost` feeds it the rows its own single pass already reads (T-3521:
    one walk of the transcripts) instead of walking them again. Fed only the cutoff's lines, a repeat of a read from before
    it is missed (the --repeat-reads walk keeps those lines as context)."""

    def __init__(self, since, top=10):
        zero = lambda: {"reads": 0, "repeats": 0, "repeat_bytes": 0, "notes": 0, "avoided_bytes": 0}  # noqa: E731
        self.since, self.top, self.zero = since, top, zero
        self.side, self.sessions = {"main": zero(), "sub": zero()}, collections.defaultdict(zero)
        self.cur, self.kinds_of, self.held = None, {}, {}

    def feed(self, path, e):
        if path != self.cur:
            self.cur, self.kinds_of, self.held = path, {}, {}
        kinds_of, held, side, sessions = self.kinds_of, self.held, self.side, self.sessions
        if e.get("type") == "system" and e.get("subtype") == "compact_boundary":
            held.clear()  # what the model read before is no longer in front of it
            return
        m = e.get("message")
        if not isinstance(m, dict) or not isinstance(m.get("content"), list):
            return
        in_window = str(e.get("timestamp") or "")[:19] >= self.since
        who = "sub" if f"{os.sep}subagents{os.sep}" in path else "main"
        sid = os.path.basename(os.path.dirname(os.path.dirname(path))) if who == "sub" else os.path.basename(path)[:-6]
        sid = sid[:8]  # <session>.jsonl, or <session>/subagents/agent-N.jsonl
        for x in m["content"]:
            if not isinstance(x, dict):
                continue
            if e.get("type") == "assistant" and x.get("type") == "tool_use" and x.get("name") == "Read" \
                    and isinstance(x.get("id"), str) and isinstance(x.get("input"), dict):
                i = x["input"]
                kinds_of[x["id"]] = hashlib.blake2b(f"{i.get('file_path')}\0{i.get('offset')}\0{i.get('limit')}".encode(
                    "utf-8", "replace"), digest_size=8).digest()
            elif x.get("type") == "tool_result" and kinds_of.get(x.get("tool_use_id")) is not None:
                key, n, text = kinds_of[x["tool_use_id"]], _size(x.get("content")), _result_text(x.get("content"))
                head = text[:300]
                sums = (side[who], sessions[sid])
                if c.READ_UNCHANGED in head or c.READ_CHANGED in head:
                    before = held.get(key)
                    if in_window:
                        for s in sums:
                            s["notes"] += 1
                            s["avoided_bytes"] += max(0, before[1] - n) if before else 0
                    continue
                digest = hashlib.blake2b(text.encode("utf-8", "replace"), digest_size=8).digest()
                if in_window:
                    for s in sums:
                        s["reads"] += 1
                        if held.get(key, (None,))[0] == digest:
                            s["repeats"] += 1
                            s["repeat_bytes"] += n
                held[key] = (digest, n)

    def result(self):
        rows = sorted(({"session": k, **v} for k, v in self.sessions.items() if v["repeats"] or v["notes"]),
                      key=lambda r: -(r["repeat_bytes"] + r["avoided_bytes"]))[:self.top]
        return {"main": self.side["main"], "sub": self.side["sub"], "sessions": rows,
                "repeat_bytes": self.side["main"]["repeat_bytes"] + self.side["sub"]["repeat_bytes"],
                "avoided_bytes": self.side["main"]["avoided_bytes"] + self.side["sub"]["avoided_bytes"]}


def repeat_line(got):
    """The one line `sampo cost` carries for _RepeatReads' result: empty when no Read was seen."""
    reads = got["main"]["reads"] + got["sub"]["reads"]
    if not reads:
        return ""
    return (f"\nRepeat reads: {got['main']['repeats'] + got['sub']['repeats']:,} of {reads:,} reads gave the model what it held "
            f"({_human(got['repeat_bytes'])} characters; lead {got['main']['repeats']:,}, subagents {got['sub']['repeats']:,}); "
            f"{got['main']['notes'] + got['sub']['notes']:,} answered with a note, {_human(got['avoided_bytes'])} characters "
            f"avoided (sampo cost --repeat-reads)")


def _repeat_reads(folder, args, cli):
    got = {"days": args.days, **repeat_reads(folder, _since(args.days))}
    side = lambda k: (f"{got[k]['reads']:,} reads, {got[k]['repeats']:,} repeats of {_human(got[k]['repeat_bytes'])} "  # noqa: E731
                      f"characters; {got[k]['notes']:,} notes served, {_human(got[k]['avoided_bytes'])} characters avoided")
    text = (f"Repeated reads, last {args.days:g} day(s) (a Read of a file range the session already read, same characters, "
            f"no compaction between):\n  main: {side('main')}\n  sub:  {side('sub')}\n"
            f"  still repeated: {_human(got['repeat_bytes'])} characters; avoided by notes: {_human(got['avoided_bytes'])}")
    if got["sessions"]:
        text += "\nPer session, biggest first (repeats, characters repeated, notes, characters avoided):\n" + "\n".join(
            f"  {r['session']}: {r['repeats']:,} repeats, {_human(r['repeat_bytes'])}, {r['notes']:,} notes, "
            f"{_human(r['avoided_bytes'])}" for r in got["sessions"])
    return cli.out(args, got, text)


BURN_CONTEXT = 150_000  # T-1800: a session averaging more context than this per reply compacts too late


def burn_findings(folder=None, days=2, forecast=None):
    """T-1800: why usage burns, as [(finding, fix)], from every project's transcripts (usage is account-wide) and the
    statusline's usage reading. Each cause comes with its fix, so nobody has to ask."""
    folder = folder or os.path.join(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude"), "projects")
    ctx, by, hours, seen = [], collections.Counter(), collections.defaultdict(set), set()
    for path, e in _entries(folder, _since(days), need='"usage"'):  # only an assistant reply's line holds it
        m = e.get("message")
        if e.get("type") != "assistant" or not isinstance(m, dict) or not isinstance(m.get("usage"), dict) \
                or m.get("id") in seen:
            continue
        seen.add(m.get("id"))
        u = m["usage"]
        size = sum(u.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        sub = f"{os.sep}subagents{os.sep}" in path
        if not sub:
            ctx.append(size)
            hours[str(e.get("timestamp"))[:13]].add(e.get("sessionId"))
        by[("subagent " if sub else "") + ("opus" if "opus" in str(m.get("model")) else "other")] += size
    out = []
    if len(ctx) >= 20 and sum(ctx) / len(ctx) > BURN_CONTEXT:
        from sampo import setup
        w = _window()  # T-2537: fm install-user leaves a window the user set alone, so naming it as the fix is a no-op
        fix = (f"sessions compact late: your settings set a {_human(w)} compact window (Sampo leaves your choice "
               f"alone); /compact long sessions at task boundaries" if w and w > parse_tokens(setup.COMPACT_WINDOW)
               else "sessions compact late: /compact long sessions at task boundaries" if w
               else "sessions compact late: fm install-user sets Sampo's 200k compact window; /compact long ones")
        out.append((f"context per reply averages {_human(sum(ctx) / len(ctx))} over {len(ctx)} replies", fix))
    peak = max(map(len, hours.values()), default=0)
    if peak >= 3:
        out.append((f"{peak} sessions replied in the same hour", "each one multiplies usage: fm drive off in the "
                    "ones nobody watches, or fm run one queue headless"))
    total = sum(by.values())
    if total and by["subagent opus"] / total > 0.2:
        out.append((f"Opus subagents used {by['subagent opus'] * 100 // total}% of the tokens",
                    "subagents on Sonnet or Haiku (model: in the agent's frontmatter, or Agent model=)"))
    week = (forecast if forecast is not None else __import__("sampo.budget").budget.forecast()).get("seven_day") or {}
    if week.get("full_at") and week.get("resets_at"):
        out.append((f"weekly usage is on pace to run out {(week['resets_at'] - week['full_at']) / 3600:.0f} h before "
                    "its reset", "optional work already waits; fewer sessions, or the causes above"))
    return out


def _burn_path():
    return os.path.join(c.state_dir(), "burn.json")


def write_burn(found):
    try:
        c.write_atomic(_burn_path(), json.dumps({"ts": time.time(), "found": found}))
    except OSError:
        pass


def burn_line(max_age=86400):
    """The session-start item from the last cached scan (the Stop hook's detached refresh writes it): never a scan;
    fm digest names every finding with its fix (T-2117)."""
    try:
        with open(_burn_path(), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return ""
    if not d.get("found") or time.time() - d.get("ts", 0) > max_age:
        return ""
    f = d["found"]
    return f"usage burn: {c.fit(f[0][0], 90)}" + (f" (+{len(f) - 1} more)" if len(f) > 1 else "") + " → fm digest"


IDLE_S = 600  # a gap longer than this between two entries is time away, not work


def _dur(s):
    s = int(round(s))
    return f"{s // 3600}h {s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m {s % 60:02d}s" if s >= 60 else f"{s}s"


def _program(cmd):
    """What a shell command ran, for the time split: fm's subcommand, an interpreter's script, else the program; leading
    cd, set and NAME=value parts skipped."""
    for part in re.split(r"&&|\|\|?|;|\n", cmd or ""):
        words = [w for w in part.split() if not re.match(r"[A-Za-z_]\w*=", w)]
        if not words or words[0] in ("cd", "set", "export", "true", ":"):
            continue
        name = os.path.basename(words[0])
        if len(words) > 1 and not words[1].startswith("-") and (name in ("fm", "sampo") or re.fullmatch(r"python[0-9.]*|node|bash|sh", name)):
            return f"{name} {os.path.basename(words[1]) if name not in ('fm', 'sampo') else words[1]}"
        return name
    return "shell"


def timing(folder, since, entries=None):
    """(seconds to the model, {tool: seconds}, {shell program: seconds}) since the cutoff (T-0188). In each session the
    stretch between two entries goes to what ended it: a tool result to its tool, a reply to the model; a prompt ends
    time spent waiting on the person, and a gap over IDLE_S is time away: neither counts. `entries` as scan's."""
    model, tools, shell = 0.0, collections.Counter(), collections.Counter()
    by_file = collections.defaultdict(list)
    for path, e in _entries(folder, since) if entries is None else entries:
        ts = str(e.get("timestamp") or "")
        try:
            t = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        if ts[:19] >= since:
            by_file[path].append((t, e))
    for rows in by_file.values():
        names, cmds, prev = {}, {}, None
        for t, e in sorted(rows, key=lambda r: r[0]):
            m = e.get("message") if isinstance(e.get("message"), dict) else {}
            content = [x for x in (m.get("content") if isinstance(m.get("content"), list) else []) if isinstance(x, dict)]
            for x in content:
                if x.get("type") == "tool_use":
                    names[x.get("id")] = x.get("name") or "?"
                    cmds[x.get("id")] = (x.get("input") or {}).get("command") if isinstance(x.get("input"), dict) else None
            results = [x for x in content if x.get("type") == "tool_result"]
            if prev is not None and 0 < t - prev <= IDLE_S:
                if e.get("type") == "assistant":
                    model += t - prev
                for x in results:
                    name, share = names.get(x.get("tool_use_id"), "?"), (t - prev) / len(results)
                    tools[name] += share
                    if name == "Bash":
                        shell[_program(cmds.get(x.get("tool_use_id")))] += share
            prev = t
    return model, tools, shell


def _task_at(timeline, ts):
    """The task of the newest ledger event at or before ts (events carry the task they were recorded for); timeline is
    sorted by time. T-1116: a bisect, not a scan per turn."""
    i = bisect.bisect_right(timeline, ts, key=lambda r: r[0])
    return timeline[i - 1][1] if i else None


def _forecast(args, cli):
    """T-0973: when each usage window fills at the pace so far (fmbudget.forecast)."""
    from sampo import budget
    w = budget.forecast()
    names = {"five_hour": "5-hour", "seven_day": "weekly"}
    clock = lambda t: time.strftime("%a %H:%M", time.localtime(t))  # noqa: E731
    rows = [f"{names[k]}: {v['used']:g}% used, on pace for {v['projected']:g}% at its reset ({clock(v['resets_at'])})"
            + (f"; fills about {clock(v['full_at'])}, before the reset" if v["full_at"] else "")
            for k, v in w.items()]
    why = budget.unknown_why()  # T-4671: a reading too old to stand is unknown, said with its age
    return cli.out(args, {"windows": w, "unknown": why}, "\n".join(rows) + "\n(a straight line from the window's start: "
                   "early on it swings)" if rows else f"fm cost --forecast: usage unknown ({why})" if why else
                   "fm cost --forecast: no usage snapshot yet (the status line writes one each turn once Claude Code "
                   "reports rate limits)")


UNEXPLAINED_GAP_S = 1800  # a turn this far from every ledger event of its session has no Foreman work around it (T-1079)
UNEXPLAINED_MIN_PCT = 20  # flag only when at least this share of the spend…
UNEXPLAINED_MIN_TOKENS = 100000  # …and this many input-equivalent tokens nothing in Foreman accounts for


def unexplained(msgs, events, gap=UNEXPLAINED_GAP_S):
    """Spend no ledger event explains: a session with none, or a turn over `gap` seconds from each of its session's.
    Returns {tokens, share (%), sessions}."""
    import bisect
    times = collections.defaultdict(list)
    for e in events:
        if e.get("session_id") and (t := _epoch(str(e.get("ts", "")))) is not None:
            times[e["session_id"]].append(t)
    for v in times.values():
        v.sort()
    total = lost = 0.0
    sessions = collections.Counter()
    for ts, sid, usage in msgs:
        w = weighted(usage)
        t, near = _epoch(ts), times.get(sid)
        if t is None:  # no usable time: neither explained nor not, and left out of the total
            continue
        total += w
        if near:
            i = bisect.bisect_left(near, t)
            if any(abs(near[j] - t) <= gap for j in (i - 1, i) if 0 <= j < len(near)):
                continue
        lost += w
        sessions[sid] += w
    return {"tokens": round(lost), "share": round(100 * lost / total, 1) if total else 0.0,
            "sessions": [s for s, _ in sessions.most_common()]}


def unexplained_spend(p, msgs, days):
    """unexplained() over every ledger event (archived months too) from the window's start less the gap."""
    since = _since(days + UNEXPLAINED_GAP_S / 86400)
    return unexplained(msgs, c.ledger_events(p, since=since))


def unexplained_line(u):
    if u["share"] >= UNEXPLAINED_MIN_PCT and u["tokens"] >= UNEXPLAINED_MIN_TOKENS:
        return (f"Unexplained spend: {u['share']:.0f}% (≈ {_human(u['tokens'])} tokens) in {len(u['sessions'])} session(s) "
                f"with no Sampo activity within {UNEXPLAINED_GAP_S // 60} min; top: {u['sessions'][0][:8]}")
    return ""


def _timelines(p, days):
    """T-1083 review: (per session id, all): a turn goes to the task its own session was on; the shared line only
    for a session with no event of its own (sessions running side by side otherwise bill each other's tasks)."""
    by, every = collections.defaultdict(list), []
    for e in c.ledger_events(p, since=_since(days + 2)):
        if e.get("task"):
            row = (str(e.get("ts", ""))[:19], e["task"])
            every.append(row)
            if e.get("session_id"):
                by[e["session_id"]].append(row)
    key = lambda r: r[0]  # noqa: E731
    return {k: sorted(v, key=key) for k, v in by.items()}, sorted(every, key=key)


def _task_for(lines, sid, ts):
    by, every = lines
    return _task_at(by.get(sid) or every, ts)


def retry_windows(p, days):
    """T-1824: {task: [(from, to)]}, each span from a step's failing evidence to its next passing one; a step still
    failing runs on ("~" sorts after every timestamp). Evidence on a later step drops an earlier step's open failure:
    the red run of red→green (a failing test, then the work) or a failure the task moved past isn't a retry."""
    failing, spans = {}, collections.defaultdict(list)
    for e in c.ledger_events(p, since=_since(days + 2)):
        if e.get("event") == "evidence" and e.get("task"):
            d, ts = e.get("data") or {}, str(e.get("ts", ""))[:19]
            key, step = (e["task"], d.get("step")), d.get("step")
            if isinstance(step, int):
                for k in [k for k in failing if k[0] == e["task"] and isinstance(k[1], int) and k[1] < step]:
                    del failing[k]
            if str(d.get("result", "")).startswith("✗"):
                failing.setdefault(key, ts)
            elif key in failing:
                spans[e["task"]].append((failing.pop(key), ts))
    for (task, _), ts in failing.items():
        spans[task].append((ts, "~"))
    return spans


def _retrying(spans, task, ts):
    return any(a <= ts < b for a, b in spans.get(task, ()))


def task_costs(p, days, retry=None):
    """T-1083: input-equivalent tokens per task over the window (what fm cost's by-task table sums). A `retry` Counter
    gets each task's share spent inside its retry windows (T-1824)."""
    folder = transcripts_dir(p.root)
    if not os.path.isdir(folder):
        return collections.Counter()
    lines, out = _timelines(p, days), collections.Counter()
    spans = retry_windows(p, days) if retry is not None else {}
    for ts, sid, usage in scan(folder, _since(days))[0]:
        task, w = _task_for(lines, sid, ts[:19]) or "(no task)", weighted(usage)
        out[task] += w
        if _retrying(spans, task, ts[:19]):
            retry[task] += w
    return out


def _window():
    """T-2537: the compact window Claude Code runs with (its env, else the user's settings.json env), or None."""
    raw = os.environ.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW")
    if raw is None:
        try:
            home = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
            with open(os.path.join(home, "settings.json"), encoding="utf-8") as f:
                raw = ((json.load(f) or {}).get("env") or {}).get("CLAUDE_CODE_AUTO_COMPACT_WINDOW")
        except (OSError, ValueError, AttributeError):
            raw = None
    return parse_tokens(raw) if raw else None


def parse_tokens(v):
    """'200k', '2M' or '150000' as a count; None when it isn't one."""
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([kKmM]?)", str(v).strip())
    return round(float(m[1]) * {"": 1, "k": 1e3, "m": 1e6}[m[2].lower()]) if m else None


def queue_quote(p, order, days=90):
    """T-1878: what the queue will likely cost before it starts: each task's median of finished tasks of its type and
    tier (two or more), summed, beside the last day's spend and the week's usage. A quote only: nothing is refused."""
    from sampo import budget
    costs, done = task_costs(p, days), collections.defaultdict(list)
    for x in c.load_briefs(p):
        if x.status == "done" and costs.get(x.id):
            done[(x.type, x.tier)].append(costs[x.id])
    med = {k: round(statistics.median(v)) for k, v in done.items() if len(v) >= 2}
    return {"tasks": len(order), "tokens": sum(med.get((b.type, b.tier), 0) for b in order),
            "no_history": [b.id for b in order if (b.type, b.tier) not in med],
            "last_day": sum(task_costs(p, 1).values()),
            "week_used": (budget.forecast().get("seven_day") or {}).get("used")}


def quote_line(q):
    left = q["no_history"]
    return (f"Quote: {q['tasks']} task(s) ≈ {_human(q['tokens'])} tokens (each the median of its type and tier"
            + (f"; no history: {', '.join(left[:5])}{' …' if left[5:] else ''}" if left else "")  # not a list: ends in … when cut
            + f") · last 24 h {_human(q['last_day'])}"
            + (f" · week {q['week_used']:.0f}% used" if q["week_used"] is not None else "")
            + ". A quote: nothing is refused.")


def forecast_task(p, b, costs=None, days=90, briefs=None):
    """T-1083: (median cost, how many) of finished tasks of b's type and tier (two or more), else None. briefs: the
    briefs the caller already loaded (T-4223: a tree's forecasts read them once, not once per child)."""
    costs = task_costs(p, days) if costs is None else costs
    pool = c.load_briefs(p) if briefs is None else briefs
    xs = [costs[x.id] for x in pool if x.status == "done" and x.id != b.id and x.type == b.type
          and x.tier == b.tier and costs.get(x.id)]
    return (round(statistics.median(xs)), len(xs)) if len(xs) >= 2 else None


CACHE_GROWTH = 1_000_000  # bytes of new transcript before the cached figures are stale…
CACHE_AGE_S = 300  # …or this long


def _cache_path(p):
    # T-1083 review: not in sessions/, where fm watch and the rate-limit readers take any *.json for a session snapshot
    return os.path.join(c.state_dir(), "cache", f"cost-{p.slug}.json")


def _read_cache(p):
    try:
        with open(_cache_path(p)) as f:
            old = json.load(f)
    except (OSError, ValueError):
        return None
    ok = isinstance(old, dict) and isinstance(old.get("size"), int) and isinstance(old.get("ts"), (int, float)) \
        and isinstance(old.get("costs"), dict)
    return old if ok else None  # the wrong shape is no cache: rewritten on the next compute


def _transcripts_size(p):
    return sum(os.path.getsize(os.path.join(d, n)) for d, _, ns in os.walk(transcripts_dir(p.root)) for n in ns
               if n.endswith(".jsonl"))


def _fresh(old, size):
    return bool(old) and abs(size - old["size"]) <= CACHE_GROWTH and time.time() - old["ts"] < CACHE_AGE_S


def cache_as_is(p):
    """(costs, made_at): the cost cache as it is, never computed: an empty Counter and None with no cache (T-4223)."""
    old = _read_cache(p)
    return (collections.Counter(old["costs"]), float(old["ts"])) if old else (collections.Counter(), None)


def cached_costs(p, compute=True, refresh=False):
    """task_costs(p, 90) kept in state/cache, keyed by the transcripts' total size: redone only when they changed by
    over ~1 MB or the figures are 5 min old. compute=False: the cache as it is (None when there is none); with
    refresh, a stale cache is redone in a detached `fm cost --refresh-cache` (the Stop hook has 5 s: never inline)."""
    old = _read_cache(p)
    if not compute:
        if refresh and not _fresh(old, _transcripts_size(p)):
            _refresh_detached(p)
            old = _read_cache(p) if os.environ.get("FOREMAN_NO_BACKGROUND") else old  # inline then: read it back
        return collections.Counter(old["costs"]) if old else None
    size = _transcripts_size(p)
    if _fresh(old, size):
        return collections.Counter(old["costs"])
    costs = task_costs(p, 90)
    try:
        c.write_atomic(_cache_path(p), json.dumps({"size": size, "ts": time.time(), "costs": dict(costs)}))
    except OSError:
        pass  # a cache: never worth failing for
    return costs


def sweep(p):
    """T-1810: the Stop hook's detached sweep, for every project (no cap or contract needed): CI on the default branch
    and a changed setup (fmforesight), then usage burn (T-1800), whose account-wide scan runs at most every 10 min
    unless the setup just changed."""
    try:
        from sampo import foresight
        changed = foresight.refresh(p)
    except Exception:
        changed = False
    try:
        fresh = not changed and time.time() - os.path.getmtime(_burn_path()) < 600
    except OSError:
        fresh = False
    if not fresh:
        write_burn(burn_findings())


def sweep_detached(p):
    """Spawn fm cost --sweep at most every SWEEP_EVERY per project: one stat per Stop hook otherwise."""
    from sampo import foresight
    stamp = os.path.join(p.dir, "sweep.spawned")
    try:
        if time.time() - os.path.getmtime(stamp) < foresight.SWEEP_EVERY:
            return
    except OSError:
        pass
    try:
        open(stamp, "w").close()
    except OSError:
        return  # no stamp, no spawn: never one per turn
    if os.environ.get("FOREMAN_NO_BACKGROUND"):
        return  # tests: no detached child racing a test folder's cleanup; sweep() is tested directly
    import subprocess
    try:
        subprocess.Popen([os.path.join(c.PLUGIN_ROOT, "bin", "fm"), "cost", "--sweep"], cwd=p.root,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError:
        pass


def _refresh_detached(p):
    if os.environ.get("FOREMAN_NO_BACKGROUND"):  # tests: inline, so the figure is there for the next call
        cached_costs(p)
        return
    import subprocess
    stamp = _cache_path(p) + ".spawned"  # T-1158: one detached refresh a minute, not one per Stop hook
    try:
        if time.time() - os.path.getmtime(stamp) < 60:
            return
    except OSError:
        pass
    try:
        os.makedirs(os.path.dirname(stamp), exist_ok=True)
        open(stamp, "w").close()
    except OSError:
        pass
    try:
        subprocess.Popen([os.path.join(c.PLUGIN_ROOT, "bin", "fm"), "cost", "--refresh-cache"], cwd=p.root,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError:
        pass


def check_cap(p, b, costs=None, held=False):
    """T-1127: whether b's cost cap is hit. The first time, cap_hit=<the cap> goes on the brief, so every dispatcher
    (c.waits_on_user: the drive, fm run, fm next, lanes) holds the task until its cost_cap changes. held: the caller
    already holds the project lock (the Stop hook does)."""
    cap = parse_tokens(b.meta.get("cost_cap"))
    if not cap:
        return False
    if str(b.meta.get("cap_hit")) == str(b.meta["cost_cap"]):
        return True
    costs = cached_costs(p, compute=False, refresh=True) if costs is None else costs
    spent = (costs or {}).get(b.id, 0)
    if spent < cap:
        return False
    import contextlib
    with (contextlib.nullcontext() if held else c.lock(p.dir)):
        x = c.find_brief(p, b.id)
        if x is not None:
            x.meta["cap_hit"] = x.meta["cost_cap"]
            x.append_log(f"cost cap hit: {_human(spent)} of {_human(cap)}")
            c.save_brief(p, x)
    c.log_event(p, "cost_cap_hit", task=b.id, data={"spent": spent, "cap": cap})
    b.meta["cap_hit"] = b.meta["cost_cap"]
    return True


def cost_line(p, b):
    """T-1083: 'Cost: forecast ≈ N (median of K FEATURE M), cap C, spent S' for a brief's readers; '' with neither a
    forecast nor a cap. Only a task with a cap pays for fresh figures; the rest read the cache, or say nothing."""
    cap = parse_tokens(b.meta.get("cost_cap"))
    try:
        costs = cached_costs(p, compute=bool(cap))
        fc = forecast_task(p, b, costs) if costs else None
    except Exception:  # a report: never stops the command it rides on
        return ""
    if not cap and not fc:
        return ""
    return "Cost: " + ", ".join(filter(None, [
        f"forecast ≈ {_human(fc[0])} (median of {fc[1]} {b.type} {b.tier}{', rough' if fc[1] < 5 else ''})" if fc else "",
        f"cap {_human(cap)}" if cap else "", f"spent {_human(costs.get(b.id, 0))}" if costs else ""]))


def _task(args, p, cli):
    b = cli.need_brief(p, args.task)
    retry = collections.Counter()
    costs = task_costs(p, 90, retry)
    spent, fc, cap = costs[b.id], forecast_task(p, b, costs), parse_tokens(b.meta.get("cost_cap"))
    text = (f"{b.id} [{b.type} {b.tier}]: spent {_human(spent)}"
            + (f", retry {_human(retry[b.id])} ({100 * retry[b.id] / spent:.0f}%: fixing failed steps)"
               if retry[b.id] else "") + (f", cap {_human(cap)}" if cap else "")
            + (f", forecast {_human(fc[0])} (median of {fc[1]} finished {b.type} {b.tier} tasks)" if fc
               else ", no forecast (needs two finished tasks of the same type and tier)"))
    return cli.out(args, {"task": b.id, "spent": round(spent), "retry": round(retry[b.id]), "cap": cap,
                            "forecast": fc and fc[0]}, text)


def _share_line(share):
    ours = {k: v for k, v in share.items() if k != "all" and not k.startswith("fm:") and v}
    if not share["all"] or not ours:
        return ""
    pct = lambda v: f"{100 * v / share['all']:.1f}%"
    verbs = sorted(((k[3:], v) for k, v in share.items() if k.startswith("fm:")), key=lambda x: -x[1])[:5]
    return (f"\nForeman's own share of what entered the context (tool results and attachments, "
            f"{_human(share['all'])} characters): {pct(sum(ours.values()))} · "
            + " · ".join(f"{k} {pct(v)}" for k, v in sorted(ours.items(), key=lambda x: -x[1]))
            + ("\nfm output by verb: " + " · ".join(f"{k} {pct(v)}" for k, v in verbs) if verbs else ""))


def closed_by_week(p):
    """T-4704: {ISO week: tasks closed done in it}, from the briefs' logs (a task folded into another has no close of
    its own)."""
    from sampo import outcomes
    out = collections.Counter()
    for b in c.load_briefs(p, include_archive=True):
        if b.status == "done" and not b.meta.get("done_in") and (wk := _iso_week(outcomes.done_at(b))):
            out[wk] += 1
    return out


def context_weeks(msgs, ctx, closed):
    """T-4704: per ISO week {replies, cache_read_per_reply, new_per_reply, closed, replies_per_closed (None without a
    closed task), sources (characters by source), share (percent by source), big (results over BIG_RESULT)}.
    Cache reads are the standing context re-read on every reply: the per-reply figure is its size, the replies per
    closed task how often it is re-read for one piece of work."""
    reads = collections.defaultdict(lambda: [0, 0, 0])
    for ts, _, usage in msgs:
        if wk := _iso_week(ts):
            r = reads[wk]
            r[0] += 1
            r[1] += usage.get("cache_read_input_tokens") or 0
            r[2] += (usage.get("input_tokens") or 0) + (usage.get("cache_creation_input_tokens") or 0)
    out = {}
    for wk in sorted(set(reads) | set(ctx)):
        n, cache, new = reads.get(wk, (0, 0, 0))
        src = {k: v for k, v in (ctx.get(wk) or {}).items() if not k.startswith("#") and v}
        total, done = sum(src.values()) or 1, closed.get(wk, 0)
        out[wk] = {"replies": n, "cache_read_per_reply": round(cache / n) if n else None,
                   "new_per_reply": round(new / n) if n else None, "closed": done,
                   "replies_per_closed": round(n / done, 2) if done and n else None,
                   "sources": dict(sorted(src.items(), key=lambda kv: -kv[1])),
                   "share": {k: round(100 * v / total, 1) for k, v in sorted(src.items(), key=lambda kv: -kv[1])},
                   "big": int((ctx.get(wk) or {}).get("#big", 0))}
    return out


def context_text(weeks, top=6):
    """T-4704: the section's lines, one per week: numbers and tool names, never a transcript's words."""
    if not weeks:
        return ""
    dash = lambda v: "-" if v is None else _human(v)
    rows = []
    for wk, w in weeks.items():
        per = w["replies_per_closed"]
        rows.append(f"  {wk}: {w['replies']} replies · cache reads {dash(w['cache_read_per_reply'])}/reply · "
                    f"new {dash(w['new_per_reply'])}/reply · replies per closed task "
                    + ("-" if per is None else f"{per:g} ({w['closed']} closed)") + " · "
                    + " · ".join(f"{k} {v:.0f}%" for k, v in list(w["share"].items())[:top])
                    + f" · results {w['big']} over {BIG_RESULT} chars")
    return ("\nWhere the context comes from, by ISO week (cache reads: the standing context re-read on every reply):\n"
            + "\n".join(rows))


def lead_cache_read(folder, first=None, now=None):
    """T-5769: the lead's cache-read tokens per turn (the median), for the 24 h before and the 24 h after the keeper's
    first compact (`first`, epoch seconds); with none yet, the last 24 h. The lead is the main thread: a subagent's
    turns are left out. Medians, so one long turn doesn't move the figure."""
    now, day = now or time.time(), 86400
    since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime((first - day) if first else now - day))
    reads = []
    for ts, _, u, *_k in scan(folder, since, sidechains=False)[0] if os.path.isdir(folder) else []:
        t = _epoch(ts)
        if t is not None:
            reads.append((t, u.get("cache_read_input_tokens") or 0, u.get("cache_creation_input_tokens") or 0))

    def side(lo, hi):
        rows = [(r, w) for t, r, w in reads if lo <= t < hi]
        med = lambda vals: int(statistics.median(vals)) if vals else None  # noqa: E731
        return {"turns": len(rows), "median_cache_read": med([r for r, _ in rows]),
                "median_cache_creation": med([w for _, w in rows])}
    if not first:
        return {"first_compact": None, "last_24h": side(now - day, now + 1)}
    before, after = side(first - day, first), side(first, first + day)
    change = None
    if before["median_cache_read"] and after["median_cache_read"] is not None:
        change = round((after["median_cache_read"] - before["median_cache_read"]) * 100 / before["median_cache_read"], 1)
    return {"first_compact": first, "before": before, "after": after, "change_pct": change}


def _lead(folder, cli, args):
    from sampo import keeper
    r = lead_cache_read(folder, keeper.first_compact())
    if not r["first_compact"]:
        d = r["last_24h"]
        return cli.out(args, r, f"Lead cache read per turn, last 24 h: {d['median_cache_read']} (median of {d['turns']} "
                                f"turns), cache creation {d['median_cache_creation']}; no compact by the keeper yet, so "
                                "nothing to compare.")
    b, a = r["before"], r["after"]
    return cli.out(args, r, f"Lead cache read per turn (median), 24 h each side of the keeper's first compact: before "
                            f"{b['median_cache_read']} ({b['turns']} turns), after {a['median_cache_read']} ({a['turns']} turns)"
                            + (f", {r['change_pct']:+}%." if r["change_pct"] is not None else ".")
                            + f" With cache creation per turn (median) before {b['median_cache_creation']}, after "
                              f"{a['median_cache_creation']}: a compact rewrites the cache once, so it shows here.")


PER_LANE_ROLES = ("lead", "builder", "reviewer", "fixer", "other")
PER_LANE_KINDS = {"input": "input_tokens", "cache_read": "cache_read_input_tokens",
                  "cache_write": "cache_creation_input_tokens", "output": "output_tokens"}
PER_LANE_WEIGHT = {"input": 1, "cache_read": 0.1, "cache_write": 1.25, "output": 5}  # WEIGHTS, by the short names
PER_LANE_BASELINE = "cost-per-lane-baseline.json"
PER_LANE_MAX_BYTES = 50 << 20  # a subagent transcript larger than this is a run with no tokens read (shown, not read)
PER_LANE_LOOKBACK_DAYS = 7  # a landed lane's work began before the window: its events are read this much earlier
PER_LANE_PROMPT_BYTES = 256 << 10  # how much of a run's transcript is read for the prompt that started it
TRUNK_MERGE = re.compile(r"Merge (T-\d+(?:[\s,&]+T-\d+)*)\b")  # the subject of a landing's merge commit


def _tids(task):
    return [t.strip() for t in str(task or "").split(",") if t.strip()]


def _share_new():
    return {**{k: 0.0 for k in PER_LANE_KINDS}, "turns": 0.0, "models": {}}


def _share_add(rec, usage, model, f=1.0):
    """Add f of one reply's usage (and of its turn, under its model) to a role's share of a lane."""
    for k, name in PER_LANE_KINDS.items():
        rec[k] += f * (usage.get(name) or 0)
    rec["turns"] += f
    if model:
        rec["models"][model] = rec["models"].get(model, 0) + f


def equiv(rec):
    """A share in input-equivalent tokens (WEIGHTS: reads ×0.1, writes ×1.25, output ×5)."""
    return sum(rec.get(k, 0) * w for k, w in PER_LANE_WEIGHT.items())


_equiv = equiv  # the old name (T-5973)


def _role_of(agent_type):
    from sampo import spawn
    return {"fm-builder": "builder", "fm-reviewer": "reviewer", "fm-fixer": "fixer"}.get(spawn.base_type(agent_type) or "", "other")


def _safe(path, base):
    """The real path of a ledger's transcript path when it is a readable file under `base` (the projects folder) and not
    over PER_LANE_MAX_BYTES, else None: a ledger path is data, nothing outside the projects folder is opened."""
    try:
        real = os.path.realpath(str(path))
        if os.path.commonpath([real, base]) != base or os.path.getsize(real) > PER_LANE_MAX_BYTES:
            return None
        return real
    except (OSError, ValueError):
        return None


def _prompt_tasks(path, base, bound):
    """[task] a run's first user message names, from the head of its transcript: the briefs it is told to read
    (audits/T-X.review.md, T-X.builder.md), else the lane worktree it names (worktree-agent-ID, via `bound`), else the
    first T-X in it; [] when it names none or can't be read."""
    real = _safe(path, base)
    text = ""
    if real:
        try:
            with open(real, "rb") as f:
                head = f.read(PER_LANE_PROMPT_BYTES).decode("utf-8", errors="replace")
        except OSError:
            head = ""
        for line in head.splitlines():
            try:
                e = json.loads(line)
            except ValueError:  # a line the read cut
                continue
            if not isinstance(e, dict) or e.get("type") != "user":
                continue
            m = e.get("message")
            content = m.get("content") if isinstance(m, dict) else None
            if isinstance(content, list):
                content = "\n".join(x.get("text") or "" for x in content if isinstance(x, dict) and x.get("type") == "text")
            if isinstance(content, str) and content.strip():
                text = content
                break
    briefs = list(dict.fromkeys(re.findall(r"audits/(T-\d+)\.[a-z]+\.md", text)))
    if briefs:
        return briefs
    for w in re.findall(r"worktree-agent-([A-Za-z0-9]+)", text):
        if bound.get(w):
            return bound[w]
    m = re.search(r"\bT-\d+\b", text)
    return [m[0]] if m else []


def _trunk_merges(root, floor):
    """([([task], second)] one per `Merge T-X` commit on HEAD's first-parent line since `floor` (UTC ISO), the first-parent
    commits there), or None when git can't read the log. A landing is its merge commit on the trunk: a lane landed twice
    or reverted is one task there."""
    r = c._git(root, "log", "--first-parent", "-n", "20000", "--format=%ct%x09%s", "HEAD", timeout=60, proc=True)  # no
    # --since: git stops at the first commit it finds older, and a commit dated out of order would hide those below it
    if r.returncode != 0:
        return None
    merges, n = [], 0
    for line in r.stdout.splitlines():
        ts, _, subject = line.partition("\t")
        if not ts.isdigit() or c.iso(int(ts))[:19] < floor[:19]:
            continue
        n += 1
        if m := TRUNK_MERGE.match(subject):
            merges.append((re.findall(r"T-\d+", m[1]), c.iso(int(ts))[:19]))
    return merges, n


def _replies(path, base=None):
    """[(usage, model)] of one transcript, a reply once per message id; None when `base` is given and the file is not
    under it, is over PER_LANE_MAX_BYTES or can't be read (_safe)."""
    try:
        if base is not None:
            path = _safe(path, base)
            if path is None:
                return None
        seen = {}
        with open(path, encoding="utf-8", errors="replace") as f:
            for n, line in enumerate(f):
                if '"usage"' not in line:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                m = e.get("message") if isinstance(e, dict) and e.get("type") == "assistant" else None
                if isinstance(m, dict) and isinstance(m.get("usage"), dict):
                    seen[m.get("id") or n] = (m["usage"], str(m.get("model") or ""))
        return list(seen.values())
    except (OSError, ValueError):
        return None


def _reads(path, base):
    """[file_path] of the Read tool calls in one transcript (a call once per tool_use id), from the same safe file _replies
    reads; None when it is not under `base`, is over PER_LANE_MAX_BYTES or can't be read."""
    real = _safe(path, base)
    if real is None:
        return None
    seen = {}
    try:
        with open(real, encoding="utf-8", errors="replace") as f:
            for n, line in enumerate(f):
                if '"Read"' not in line:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                m = e.get("message") if isinstance(e, dict) and e.get("type") == "assistant" else None
                for blk in (m.get("content") if isinstance(m, dict) and isinstance(m.get("content"), list) else ()):
                    if isinstance(blk, dict) and blk.get("type") == "tool_use" and blk.get("name") == "Read" \
                            and isinstance(blk.get("input"), dict) and isinstance(blk["input"].get("file_path"), str):
                        seen[blk.get("id") or (n, len(seen))] = blk["input"]["file_path"]
    except (OSError, ValueError):
        return None
    return list(seen.values())


def lead_reply(path, e, since):
    """(key, (ts, usage, model)) when the transcript entry `e` of file `path` is one of the lead's own replies on or after
    `since` (a subagent's file, a non-reply, and an older one are not), else None. The key is the message id: one reply
    is written once per content block, and across a resumed session's files (T-5973: shared with targets.py's one walk)."""
    m = e.get("message")
    if f"{os.sep}subagents{os.sep}" in path or e.get("type") != "assistant" or not isinstance(m, dict) \
            or not isinstance(m.get("usage"), dict) or str(e.get("timestamp") or "")[:19] < since[:19]:
        return None
    return m.get("id") or (path, e.get("timestamp")), (str(e["timestamp"]), m["usage"], str(m.get("model") or ""))


def lead_replies(folder, since):
    """[(ts, usage, model)] of the lead's own replies (a subagent's are left out), once per message id."""
    seen = {}
    folders = [folder] if isinstance(folder, str) else list(folder)  # the project's folder and its lane worktrees'
    for path, e in _entries([f for f in folders if os.path.isdir(f)], since, need='"usage"'):
        got = lead_reply(path, e, since)
        if got:
            seen[got[0]] = got[1]
    return list(seen.values())


_lead_replies = lead_replies  # the old name (T-5973)


def per_lane(p, days, now=None, lead_replies=None):
    """T-5950: the tokens of each lane that landed in the window, by role. A lane landed once per task: its `Merge T-X`
    commit on the main checkout's HEAD first-parent line in the window (a re-landed or reverted lane is one; the ledger's
    lane_landed count is returned beside it, and stands in only when git can't read the log). The lead's share is each
    of its replies (the main project's transcripts, whatever worktree this runs from) between the lane's start (its
    first lane_new, focus or run event) and its landing, split among the lanes open then; the builder's, reviewer's and
    fixer's are their own runs (the ledger's `subagent` events name each run's transcript). A run belongs to the lane of
    the first of: the task its agent id was bound to (reviewer_bound, a builder's mark, a lane_builder worktree path, the
    worktree in its transcript's path), the task its first message names (a brief file, a lane worktree, a bare T-X), the
    task the stop recorded (the lead's focus then: a guess). Runs and lead replies that fit no landed lane are returned
    under `unattributed`, never dropped. {days, landed, git: {source, merges, tasks}, ledger: {tasks, events}, lanes:
    {task: {role: share}}, median: {role: {n, input, cache_read, cache_write, output, equiv}}, unattributed: {subagents:
    [...], lead: share}}; a share is the four token kinds, `turns`, `models` (turns by model). `lead_replies` is a
    caller's own read of the lead's replies (T-5973: targets.py reads each transcript once for three readers): the
    `[(ts, usage, model)]` that `lead_replies()` returns, trimmed here to the window; None reads them itself."""
    since = _since(days + PER_LANE_LOOKBACK_DAYS)
    floor = _since(days)
    kinds = ("lane_landed", "lane_new", "lane_builder", "focus", "builder_mark", "subagent", "reviewer_bound",
             "lane_workset")
    events = [e for e in c.ledger_events(p, kinds, since=since) if isinstance(e, dict)]
    root = c.main_worktree(p.root) or p.root  # the lead's transcripts and the trunk are the main checkout's, not a lane's
    ledger, ledger_events, start, bound, began = {}, 0, {}, {}, set()
    explorer, reads = {}, {}  # T-6062: each lane's last `lane_workset` event, and its builders' Read paths
    for e in events:
        ts, d, kind = str(e.get("ts") or "")[:19], e.get("data") if isinstance(e.get("data"), dict) else {}, e.get("event")
        tids = _tids(e.get("task"))
        if kind == "lane_workset":  # the explorer's own record: it neither starts a lane nor binds a run
            for t in tids:
                explorer[t] = d
            continue
        if kind == "lane_landed":
            for t in tids:
                if ts >= floor:
                    ledger[t] = max(ledger.get(t, ""), ts)
            ledger_events += ts >= floor
            continue
        for t in tids:
            start[t] = min(start.get(t, ts), ts)
        if kind in ("lane_new", "focus"):
            began.update(tids)
        if kind == "reviewer_bound" and d.get("agent_id") and tids:
            bound.setdefault(str(d["agent_id"]), tids)
        elif kind == "builder_mark" and d.get("agent") and tids:
            bound.setdefault(str(d["agent"]), tids)
        elif kind == "lane_builder" and tids and (m := re.search(r"agent-([\w-]+)/?$", str(d.get("path") or ""))):
            bound.setdefault(m[1], tids)
    trunk = _trunk_merges(root, floor)
    if trunk is None:  # no readable trunk: the ledger's own landings, said so
        landed, git = dict(ledger), {"source": "ledger", "merges": 0, "tasks": 0}
    else:
        landed = {}
        for tasks, ts in trunk[0]:
            for t in tasks:
                landed[t] = max(landed.get(t, ""), ts)
        git = {"source": "git", "merges": len(trunk[0]), "tasks": len(landed)}
    lanes = {t: {} for t in landed}
    if late := [t for t in landed if t not in began]:  # no start in the window: it began before the lookback, so its last
        prior = {}  # lane_new at or before its landing is read without the cutoff (its start, not an older landing's)
        for e in c.ledger_events(p, ("lane_new",)):
            ts = str(e.get("ts") or "")[:19]
            for t in _tids(e.get("task")):
                if t in late and ts <= landed[t]:
                    prior[t] = max(prior.get(t, ""), ts)
        for t, ts in prior.items():
            start[t] = min(start.get(t, ts), ts)
    unattr = {"subagents": [], "lead": _share_new()}
    base = os.path.dirname(os.path.realpath(transcripts_dir(root)))
    seen = set()
    for e in events:
        d = e.get("data") if isinstance(e.get("data"), dict) else {}
        if e.get("event") != "subagent" or not d.get("agent_type") or (d.get("agent_id"), d.get("transcript")) in seen:
            continue
        seen.add((d.get("agent_id"), d.get("transcript")))
        aid, path = str(d.get("agent_id") or ""), str(d.get("transcript") or "")
        wt = re.search(r"worktrees-agent-([A-Za-z0-9]+)", path)
        tids = bound.get(aid) or (wt and bound.get(wt[1]))
        if not tids and path:  # the prompt that started the run names its brief: better than the focus at its stop
            tids = _prompt_tasks(path, base, bound)
        tids = tids or _tids(e.get("task"))
        role = _role_of(d["agent_type"])
        replies = _replies(path, base) if path else None
        mine = [t for t in tids if t in lanes]
        if mine and replies is not None:
            for t in mine:
                rec = lanes[t].setdefault(role, _share_new())
                for usage, model in replies:
                    _share_add(rec, usage, model, 1 / len(mine))
                if role == "builder" and t in explorer:  # T-6062: the files it read, against the note's
                    reads.setdefault(t, []).extend(_reads(path, base) or [])
        elif str(e.get("ts") or "")[:19] >= floor or not tids:  # before the window, a run with a task is that lane's
            rec = _share_new()
            for usage, model in replies or ():
                _share_add(rec, usage, model)
            why = ("transcript not read" if replies is None else "no task found" if not tids
                   else f"{', '.join(tids)} not landed in the window")
            unattr["subagents"].append({"agent_type": str(d["agent_type"]), "agent_id": aid, "task": ",".join(tids) or None,
                                        "why": why, **rec})
    # (the parameter shadows the function here: the alias reads the transcripts)
    replies_in = _lead_replies(project_folders(p), since) if lead_replies is None else \
        [r for r in lead_replies if str(r[0])[:19] >= since[:19]]
    for ts, usage, model in replies_in:
        open_ = [t for t, z in landed.items() if start.get(t, z) <= ts[:19] <= z]
        for t in open_:
            _share_add(lanes[t].setdefault("lead", _share_new()), usage, model, 1 / len(open_))
        if not open_ and ts[:19] >= floor:
            _share_add(unattr["lead"], usage, model)
    lanes = {t: r for t, r in sorted(lanes.items()) if r}
    from sampo import workset  # T-6062: the explorer's tokens and the explore share, apart from the roles' above
    ws = {t: w for t in lanes if (w := workset.row(explorer.get(t), reads.get(t)))}
    return {"days": days, "landed": len(landed), "git": git,
            "ledger": {"tasks": len(ledger), "events": ledger_events},
            "lanes": lanes, "median": _medians(lanes), "unattributed": unattr, "workset": ws,
            "landed_at": {t: landed[t] for t in lanes}}  # T-6060: the day a lane's dollars belong to


def _medians(lanes):
    """{role: {n lanes with the role, the median of each token kind, equiv: of its input-equivalent tokens}}."""
    out = {}
    for role in PER_LANE_ROLES:
        rows = [r[role] for r in lanes.values() if role in r]
        if rows:
            out[role] = {"n": len(rows), **{k: round(statistics.median(r[k] for r in rows)) for k in PER_LANE_KINDS},
                         "equiv": round(statistics.median(equiv(r) for r in rows))}
    return out


def _baseline_path(p):
    return os.path.join(p.dir, PER_LANE_BASELINE)


def _pct(now, was):
    return round((now - was) * 100 / was, 1) if was else None


def _per_lane_text(r, base=None):
    fmt = lambda s: " · ".join(f"{k.replace('_', ' ')} {_human(s[k])}" for k in PER_LANE_KINDS)  # noqa: E731
    g, led = r["git"], r["ledger"]
    out = [f"Tokens per landed lane, last {r['days']:g} day(s): {r['landed']} lane(s) landed "
           + (f"(git: {g['merges']} Merge T-… commit(s) on the trunk's first-parent line, {g['tasks']} task(s); "
              if g["source"] == "git" else "(no trunk log to read, counted from the ledger; ")
           + f"ledger: {led['tasks']} task(s) in {led['events']} lane_landed event(s)); "
           "input · cache read · cache write · output tokens by role; ≈ input-equivalent",
           "Roles: lead = the main session's replies split among the lanes open then; builder, reviewer, fixer = those "
           "agents' own runs; other = any agent that is not a builder, reviewer or fixer (a scout, an auditor)"]
    for t, roles in r["lanes"].items():
        out.append(f"  {t} (≈ {_human(sum(equiv(s) for s in roles.values()))}):")
        out += [f"    {role}: {fmt(roles[role])} · ≈ {_human(equiv(roles[role]))}"
                for role in PER_LANE_ROLES if role in roles]
        if t in r.get("workset", {}):  # T-6062: the explorer's own line, never added into the builder's
            from sampo import workset
            out.append(workset.line(r["workset"][t]))
    if r["median"]:
        out.append("Median per role over the lanes that have it:")
        out += [f"  {role} (n {m['n']}): {fmt(m)} · ≈ {_human(m['equiv'])}" for role, m in r["median"].items()]
    un = r["unattributed"]
    lead = un["lead"]
    if un["subagents"] or lead["turns"]:
        out.append("Unattributed (fit no landed lane, shown and not dropped):")
        out += [f"  {u['agent_type']} {u['agent_id'][:8]}: {fmt(u)} · {u['why']}" for u in un["subagents"]]
        if lead["turns"]:
            out.append(f"  lead, outside any landed lane's window ({lead['turns']:.0f} replies): {fmt(lead)}")
    if base is not None:
        out.append(base["line"] if "line" in base else "")
        for role, ch in base.get("change", {}).items():
            if ch["was"] is None:
                out.append(f"  {role}: no earlier figure (now ≈ {_human(ch['now'])} median per lane)")
                continue
            out.append(f"  {role}: ≈ {_human(ch['was'])} → {_human(ch['now'])} median per lane "
                       + ("(no earlier figure)" if ch["equiv"] is None else f"({ch['equiv']:+g}%)")
                       + " · " + " · ".join(f"{k.replace('_', ' ')} {'-' if ch[k] is None else format(ch[k], '+g') + '%'}"
                                            for k in PER_LANE_KINDS))
    return "\n".join(x for x in out if x)


def _per_lane(p, args, cli):
    r = per_lane(p, args.days)
    base, wrote = None, False
    if getattr(args, "compare_baseline", False):
        saved, why = None, None
        try:
            with open(_baseline_path(p), encoding="utf-8") as f:
                saved = json.load(f)
        except FileNotFoundError:
            base = {"line": "No baseline saved yet: sampo cost --per-lane --save-baseline writes one.", "change": {}}
        except (OSError, ValueError, RecursionError) as e:
            why = "not valid JSON" if isinstance(e, ValueError) else f"unreadable ({e.__class__.__name__})"
        else:
            if not isinstance(saved, dict):
                why = "not a JSON object"
            elif not isinstance(saved.get("median"), dict):
                why = "no median object"
        if why:
            base = {"line": f"Baseline file {_baseline_path(p)} ignored: {why}; sampo cost --per-lane --save-baseline "
                            "writes a new one.", "change": {}}
        if saved is not None and not why:
            was = saved["median"]
            change = {}
            for role, m in r["median"].items():
                w = was.get(role) if isinstance(was.get(role), dict) else {}
                nums = lambda x, k: x.get(k) if isinstance(x.get(k), (int, float)) else None  # noqa: E731
                change[role] = {"was": nums(w, "equiv"), "now": m["equiv"],
                                "equiv": _pct(m["equiv"], nums(w, "equiv")),
                                **{k: _pct(m[k], nums(w, k)) for k in PER_LANE_KINDS}}
            gone = [role for role in was if role not in r["median"]]
            base = {"saved": saved.get("saved"), "days": saved.get("days"), "change": change, "gone": gone,
                    "line": f"Against the baseline saved {saved.get('saved')} ({saved.get('landed')} lane(s), "
                            f"{saved.get('days')} day(s)); median per lane by role"
                            + (f"; no longer present: {', '.join(gone)}" if gone else "") + ":"}
    if getattr(args, "save_baseline", False):
        c.write_atomic(_baseline_path(p), json.dumps({"saved": c.now(), "days": r["days"], "landed": r["landed"],
                                                       "git": r["git"], "ledger": r["ledger"], "median": r["median"]}, indent=1))
        wrote = True
    text = _per_lane_text(r, base) + (f"\nBaseline saved to {_baseline_path(p)}." if wrote else "")
    data = dict(r, baseline=base, baseline_saved=wrote)
    return cli.out(args, data, text)


MODEL_PRICES = os.path.join(c.PLUGIN_ROOT, "data", "model-prices.json")  # T-6060: dollars per million tokens by family
NATIVE_REFERENCE = os.path.join(c.PLUGIN_ROOT, "data", "native-reference.json")  # the plain Claude Code arm's dollars per task
NATIVE_FILE = "native-reference.json"  # a measured one in the project's state replaces the stored one
NATIVE_TARGET = 0.1  # the user's goal (2026-11-22): a tenth of what plain Claude Code costs per task
NATIVE_DAYS = 14


def _json_file(path):
    try:
        with open(path, encoding="utf-8") as f:
            got = json.load(f)
    except (OSError, ValueError, RecursionError):
        return None
    return got if isinstance(got, dict) else None


def _price_row(row):
    """A family's row when all four token kinds are positive finite numbers, else None (a half-filled row is no price)."""
    ok = isinstance(row, dict) and all(
        isinstance(row.get(k), (int, float)) and not isinstance(row.get(k), bool) and 0 < row[k] < float("inf")
        for k in PER_LANE_KINDS)
    return {k: row[k] for k in PER_LANE_KINDS} if ok else None


def prices():
    """The price table: {date, source, families: {family: {input, cache_read, cache_write, output}}, highest}: only the
    families whose four prices are all positive numbers; `highest` is the dearest of them (what a Claude model of no known
    family is priced at), None with none. A missing or corrupt file gives no families: nothing then has a dollar figure."""
    got = _json_file(MODEL_PRICES) or {}
    fam = got.get("families") if isinstance(got.get("families"), dict) else {}
    rows = {k: r for k, v in fam.items() if (r := _price_row(v))}
    return {"date": got.get("date"), "source": got.get("source"), "families": rows,
            "highest": max(rows, key=lambda f: sum(rows[f].values())) if rows else None}


def model_family(model, table=None):
    """(family, assumed): 'opus', 'sonnet' or 'haiku' for a name that says claude or anthropic (any provider prefix: "us.anthropic.
    claude-…", "anthropic/claude-…") and a family; 'local' for any other name (a model of this machine's: zero dollars, its
    tokens still counted); a Claude name of no known family (or none: a reply that named no model) is the table's highest-priced
    family, assumed and flagged (an unknown model is never claimed free or cheap), 'unknown' with no table."""
    m = str(model or "").lower()
    if m and "claude" not in m and "anthropic" not in m and m != "<synthetic>":
        return "local", False
    for f in ("opus", "sonnet", "haiku"):
        if f in m:
            return f, False
    return (table or prices()).get("highest") or "unknown", True


def usd(rec, table=None):
    """{usd, local, assumed, unpriced, tokens} of one role's share of a lane: each token kind at its model family's price,
    the share's tokens split among its models by turns (a share that ran under two models is priced by how many turns each
    took); a local model's turns are zero dollars, counted. `local` is True when every turn was local. A Claude family with no
    row in the table has no price: usd is None and the family is in `unpriced` (never $0)."""
    table = table or prices()
    models = dict(rec.get("models") or {})
    if (rest := (rec.get("turns") or 0) - sum(models.values())) > 1e-9:  # replies that named no model: assumed, never free
        models[""] = rest
    turns = sum(models.values())
    total, local_turns, assumed, unpriced = 0.0, 0.0, [], set()
    for model, n in models.items():
        fam, guess = model_family(model, table)
        if fam == "local":
            local_turns += n
            continue
        if guess:
            assumed.append(model or "(no model)")
        if not (row := table["families"].get(fam)):
            unpriced.add(fam)
            continue
        total += sum(rec.get(k, 0) * (n / turns) * row[k] for k in PER_LANE_KINDS) / 1e6
    return {"usd": None if unpriced else round(total, 8), "local": bool(models) and local_turns == turns,
            "assumed": sorted(set(assumed)), "unpriced": sorted(unpriced),
            "tokens": {k: round(rec.get(k, 0)) for k in PER_LANE_KINDS}}


def native_reference(p):
    """{usd_per_task, n, source, date, measured} the plain Claude Code arm's dollars per solved task: the project's own
    native-reference.json (a measured one) over the stored exam figure (plugin/data/native-reference.json: exam s8-049,
    one task, marked unmeasured); None when neither is a positive number."""
    for path in (os.path.join(p.dir, NATIVE_FILE), NATIVE_REFERENCE):
        got = _json_file(path)
        v = got.get("usd_per_task") if got else None
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 and v == v and v != float("inf"):
            return {"usd_per_task": v, "n": got.get("n"), "source": str(got.get("source") or path)[:300],
                    "date": got.get("date"), "measured": bool(got.get("measured"))}
    return None


def _med(xs):
    return statistics.median(xs) if xs else None


def priced_lanes(r, table=None):
    """per_lane's result with each lane's roles priced: {task: {role: usd(rec)}}."""
    table = table or prices()
    return {t: {role: usd(rec, table) for role, rec in roles.items()} for t, roles in r["lanes"].items()}


def _lane_usd(roles):
    """A lane's dollars, its roles summed; None when any of them has no price."""
    got = [x["usd"] for x in roles.values()]
    return None if any(g is None for g in got) else sum(got)


def vs_native(p, days, r=None):
    """T-6060: dollars per landed lane by role against plain Claude Code. {days, landed, prices: {date, source}, lanes:
    {task: {role: {usd, local, assumed, tokens}}}, median: {role: {n, usd}}, lane_usd_median (of each lane's roles summed),
    builder_usd_median (the builder alone: a native session's proxy), reference, ratio (lane_usd_median / the
    reference's dollars; None with none), builder_ratio, target_ratio (0.1), target_usd, landed_at, unpriced}.
    `r` is a per_lane() result a caller already read."""
    r = r if r is not None else per_lane(p, days)
    table = prices()
    lanes = priced_lanes(r, table)
    no_price = sorted({f for roles in lanes.values() for x in roles.values() for f in x["unpriced"]})
    # one share with no price makes every aggregate unknown: a median of the lanes that do have one is a half truth
    med = {role: {"n": len(rows), "usd": None if no_price else _med([x["usd"] for x in rows])} for role in PER_LANE_ROLES
           if (rows := [roles[role] for roles in lanes.values() if role in roles])}
    ref = native_reference(p)
    per = None if no_price else _med([_lane_usd(roles) for roles in lanes.values()])
    builder = med.get("builder", {}).get("usd")
    ratio = lambda v: round(v / ref["usd_per_task"], 4) if ref and v is not None else None  # noqa: E731
    return {"days": days, "landed": r["landed"], "prices": {"date": table["date"], "source": table["source"]},
            "lanes": lanes, "median": med, "lane_usd_median": per, "builder_usd_median": builder, "reference": ref,
            "ratio": ratio(per), "builder_ratio": ratio(builder), "target_ratio": NATIVE_TARGET,
            "target_usd": ref["usd_per_task"] * NATIVE_TARGET if ref else None,
            "landed_at": r.get("landed_at", {}), "no_price": no_price, "highest": table["highest"],
            "unpriced": sorted({m for roles in lanes.values() for x in roles.values() for m in x["assumed"]})}


def native_series(p, days=NATIVE_DAYS, priced=None):
    """The Board's trend: {reference, target_ratio, target_usd, days: [{day, lanes, usd, ratio}]}, one entry per day, the
    last `days` days oldest first; a day's usd is the median dollars of the lanes that landed that day, its ratio that over
    the reference's dollars (None on a day nothing landed, or with no reference). `priced` is a vs_native() result."""
    v = priced if priced is not None else vs_native(p, days)
    ref = v.get("reference") or native_reference(p)
    by_day = collections.defaultdict(list)
    for t, roles in v["lanes"].items():
        day = str(v.get("landed_at", {}).get(t) or "")[:10]
        if day:
            by_day[day].append(_lane_usd(roles))
    today = datetime.datetime.now(datetime.timezone.utc).date()
    rows = []
    for back in range(days - 1, -1, -1):
        day = (today - datetime.timedelta(days=back)).isoformat()
        got = by_day.get(day, [])
        d = None if None in got else _med(got)  # a lane of that day with no price: the day has no figure
        rows.append({"day": day, "lanes": len(by_day.get(day, [])), "usd": None if d is None else round(d, 6),
                     "ratio": round(d / ref["usd_per_task"], 4) if d is not None and ref else None})
    return {"reference": ref, "target_ratio": NATIVE_TARGET,
            "target_usd": ref["usd_per_task"] * NATIVE_TARGET if ref else None, "days": rows}


def vs_native_text(v):
    money = lambda x: "n/a" if x is None else f"${x:.4f}" if x < 1 else f"${x:.2f}"  # noqa: E731
    out = [f"Dollars per landed lane, last {v['days']:g} day(s): {v['landed']} lane(s) landed; each token kind (input, "
           f"cache read, cache write, output) at its model family's price ({v['prices']['date']}), a local model at $0 with "
           "its tokens counted"]
    for t, roles in v["lanes"].items():
        out.append(f"  {t}: {money(_lane_usd(roles))} = " + " · ".join(
            f"{role} {money(x['usd'])}" + (" (local)" if x["local"] else "") for role, x in roles.items()))
    out.append("Median per role (the lanes that have it):")
    for role in PER_LANE_ROLES:
        if role in ("other",) and role not in v["median"]:
            continue
        m = v["median"].get(role)
        out.append(f"  {role}: " + (f"{money(m['usd'])} (n {m['n']})" if m else "none recorded"))
    out.append("Headless children (a rival read, a card fill) are not counted yet (T-6071): the dollars above are the "
               "lead, builder, reviewer and fixer shares")
    out.append(f"Per lane, all roles (median): {money(v['lane_usd_median'])}; the builder alone: {money(v['builder_usd_median'])}")
    if v["no_price"]:
        why = ("the price table is missing, unreadable or has no usable price for any family" if not v["highest"]
               else f"the price table has no price for {', '.join(v['no_price'])}")
        out.append(f"No dollar figure and no ratio: {why} (plugin/data/model-prices.json); a model with no price is never counted as $0")
        return "\n".join(out)
    ref = v["reference"]
    if not ref:
        out.append("no native reference: the plain Claude Code arm's dollars per solved task is not recorded, so no ratio")
        return "\n".join(out)
    out.append(f"Native (plain Claude Code) reference: ${ref['usd_per_task']:.2f} per solved task, n {ref['n']}, "
               f"{ref['date']}: {ref['source']}"
               + ("" if ref["measured"] else " [not measured: one exam task, n 1; Claude exam runs are paused by the user, "
                  "so no measured figure is possible now]"))
    if v["ratio"] is not None:
        alone = "" if v["builder_ratio"] is None else f" (builder alone {v['builder_ratio']:.2f}x)"
        out.append(f"Ratio, Sampo per lane over native: {v['ratio']:.2f}x{alone}; "
                   f"the 10x line is {NATIVE_TARGET:g}x = {money(v['target_usd'])} per lane: "
                   + ("met" if v["ratio"] <= NATIVE_TARGET else f"{v['ratio'] / NATIVE_TARGET:.1f} times over it"))
    if v["unpriced"]:
        out.append(f"Model(s) of no known family priced as {v['highest']}, the dearest: {', '.join(v['unpriced'])}")
    return "\n".join(out)


def _vs_native(p, args, cli):
    return cli.out(args, (v := vs_native(p, args.days)), vs_native_text(v))


CPU_ORIGINS = ("builder", "land", "verify", "ship", "probe", "other")  # tests/run.py's ORIGINS, and what none of them is


def _cpu_log_path(common):
    """The repo's CPU log: tests/run.py's own cpu_log_path (one place hashes the repo and names the folder), loaded from
    the plugin's file; None when that file can't be loaded."""
    import importlib.util
    try:
        spec = importlib.util.spec_from_file_location("sampo_tests_run", os.path.join(c.PLUGIN_ROOT, "tests", "run.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.cpu_log_path(common)
    except Exception:  # a plugin without its tests folder has no log to read
        return None


def cpu_by_origin(p, days, now=None):
    """T-6006: the CPU seconds the test runs of the last `days` days cost, by who started them: {days, landed,
    origins: {origin: {runs, cpu_s, saved_cpu_s, reused, per_lane_cpu_s}}, cpu_s, per_lane_cpu_s, saved_cpu_s, source}.
    Each tests/run.py appends a row to the repo's CPU log (cpu_s: its classes' and its own CPU time, from the kernel's
    rusage; saved_cpu_s: what the classes it reused cost when they ran). `landed` is the lanes that merged in the window
    (trunk `Merge T-X` commits, as per_lane counts them; the ledger's when git can't read the log); per_lane_cpu_s is
    the total over it, None while none landed. A torn or foreign line is skipped; an unknown origin counts as other."""
    now = time.time() if now is None else now
    root = c.main_worktree(p.root) or p.root
    r = c._git(root, "rev-parse", "--path-format=absolute", "--git-common-dir", timeout=20, proc=True)
    common = os.path.realpath(os.path.join(root, r.stdout.strip())) if r.returncode == 0 and r.stdout.strip() else None
    rows = []
    log = _cpu_log_path(common) if common else None
    try:
        with open(log or os.devnull, "rb") as f:
            for raw in f:
                try:
                    row = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(row, dict) and isinstance(row.get("t"), (int, float)) and now - days * 86400 <= row["t"] <= now + 3600 \
                        and isinstance(row.get("cpu_s"), (int, float)):
                    rows.append(row)
    except OSError:
        pass
    floor = _since(days)
    trunk = _trunk_merges(root, floor)
    if trunk is None:
        events = [e for e in c.ledger_events(p, ("lane_landed",), since=floor) if isinstance(e, dict)]
        landed, source = len({t for e in events for t in _tids(e.get("task"))}), "ledger"
    else:
        landed, source = len({t for tasks, _ in trunk[0] for t in tasks}), "git"
    origins = {}
    for row in rows:
        o = row.get("origin") if row.get("origin") in CPU_ORIGINS else "other"
        got = origins.setdefault(o, {"runs": 0, "cpu_s": 0.0, "saved_cpu_s": 0.0, "reused": 0})
        got["runs"] += 1
        got["cpu_s"] += row["cpu_s"]
        got["saved_cpu_s"] += row.get("saved_cpu_s") if isinstance(row.get("saved_cpu_s"), (int, float)) else 0.0
        got["reused"] += row.get("reused") if isinstance(row.get("reused"), int) else 0
    per = (lambda s: round(s / landed, 1)) if landed else (lambda s: None)  # noqa: E731
    for got in origins.values():
        got.update(cpu_s=round(got["cpu_s"], 1), saved_cpu_s=round(got["saved_cpu_s"], 1), per_lane_cpu_s=per(got["cpu_s"]))
    total = sum(g["cpu_s"] for g in origins.values())
    return {"days": days, "landed": landed, "source": source, "runs": len(rows),
            "origins": {o: origins[o] for o in CPU_ORIGINS if o in origins},
            "cpu_s": round(total, 1), "per_lane_cpu_s": per(total),
            "saved_cpu_s": round(sum(g["saved_cpu_s"] for g in origins.values()), 1)}


def _cpu_text(r):
    if not r["runs"]:
        return (f"No CPU to show: no test run was metered in the last {r['days']:g} day(s) (tests/run.py logs each run's "
                "CPU once it runs from this version on).")
    lines = [f"Test CPU in the last {r['days']} day(s): {r['cpu_s']:.1f} CPU s over {r['runs']} run(s), "
             + (f"{r['per_lane_cpu_s']:.1f} CPU s per landed lane ({r['landed']} landed, {r['source']})" if r["landed"]
                else "no lane landed in the window, so no per landed lane figure")]
    for o, g in r["origins"].items():
        lines.append(f"  {o:<8}{g['cpu_s']:>9.1f} CPU s  {g['runs']} run(s)"
                     + (f"  {g['per_lane_cpu_s']:.1f} per landed lane" if g["per_lane_cpu_s"] is not None else "")
                     + (f"  reused {g['reused']} classes (saved {g['saved_cpu_s']:.0f} CPU s)" if g["reused"] else ""))
    return "\n".join(lines)


def _cpu(args, p, cli):
    r = cpu_by_origin(p, args.days)
    return cli.out(args, r, _cpu_text(r))


def _local_parity(p, args, cli):
    """T-6089: the local reviewer against Claude's on the sampled lanes, per lens and lane class: how often the verdicts
    agree over every sample, the finding recall over the last PARITY_WINDOW, and whether the class routes back to Claude."""
    from sampo import lanepilot
    rows = lanepilot.parity_rows(p)
    groups = {}
    for r in rows:
        groups.setdefault((str(r.get("lens") or ""), str(r.get("class") or "")), []).append(r)
    out = []
    for (lens, cls), mine in sorted(groups.items()):
        agree = sum(1 for r in mine if r.get("match"))
        matched, total, window = lanepilot.parity_window(rows, lens, cls)
        out.append({"lens": lens, "class": cls, "samples": len(mine), "agreement": round(100 * agree / len(mine)),
                    "window": window, "recall": round(100 * matched / total) if total else None,
                    "routed_back": lanepilot.routed_back(rows, lens, cls)})
    lines = [f"Local reviewer against Claude on the sampled lanes (1 in {lanepilot.LOCAL_SAMPLE}, T-6089): per lens and lane "
             f"class, the verdict agreement over all samples, the finding recall over the last {lanepilot.PARITY_WINDOW}, and "
             f"whether the class is routed back to Claude (recall under {round(100 * lanepilot.PARITY_FLOOR)}%)."]
    if not out:
        lines.append("No sampled lane has a local review yet.")
    for x in out:
        recall = f"recall {x['recall']}% over {x['window']}" if x["recall"] is not None else "recall n/a (no Claude finding)"
        lines.append(f"  {x['lens']:<10} {x['class']:<20} {x['samples']:>3} sample(s)  agreement {x['agreement']}%  {recall}"
                     f"  {'routed back to Claude' if x['routed_back'] else 'local'}")
    return cli.out(args, {"parity": out, "window": lanepilot.PARITY_WINDOW, "floor": lanepilot.PARITY_FLOOR},
                   "\n".join(lines))


def cmd_cost(args):
    from sampo import cli
    p = cli.resolve(args)
    if getattr(args, "local_parity", False):  # T-6089
        return _local_parity(p, args, cli)
    if getattr(args, "cpu", False):  # T-6006
        return _cpu(args, p, cli)
    if getattr(args, "vs_native", False):  # T-6060
        return _vs_native(p, args, cli)
    if any(getattr(args, k, False) for k in ("per_lane", "save_baseline", "compare_baseline")):  # T-5950
        args.per_lane = True  # the baseline flags imply it (the help says so)
        return _per_lane(p, args, cli)
    if getattr(args, "refresh_cache", False):  # T-1083 review: the Stop hook's detached refresh
        old = _read_cache(p)
        if old and time.time() - old["ts"] < 60:  # another refresh just ran
            return 0
        try:
            c.write_atomic(_cache_path(p), json.dumps({**(old or {"costs": {}}), "size": (old or {}).get("size", 0),
                                                       "ts": time.time()}))  # claim it: no second scan meanwhile
        except OSError:
            pass
        os.makedirs(os.path.dirname(_cache_path(p)), exist_ok=True)
        size, costs = _transcripts_size(p), task_costs(p, 90)
        c.write_atomic(_cache_path(p), json.dumps({"size": size, "ts": time.time(), "costs": dict(costs)}))
        return 0
    if getattr(args, "sweep", False):
        sweep(p)
        return 0
    if getattr(args, "task", None):
        return _task(args, p, cli)
    if getattr(args, "by_model", False):
        return _by_model(p, args, cli)
    if getattr(args, "forecast", False):
        return _forecast(args, cli)
    folder = transcripts_dir(p.root)
    if getattr(args, "lead", False):  # T-5769: a harness with no Claude Code transcripts (sampo code) reads zero turns
        return _lead(folder, cli, args)
    if not os.path.isdir(folder):
        raise cli.UsageError(f"no Claude Code transcripts for this project at {folder}")
    if getattr(args, "outputs", False):
        return _outputs(folder, args, cli)
    if getattr(args, "repeat_reads", False):  # T-6064
        return _repeat_reads(folder, args, cli)
    share = collections.Counter()
    if getattr(args, "sessions", False):  # a subagent's own context isn't its parent's per-turn figure
        return _sessions(scan(folder, _since(args.days), sidechains=False, kinds=True)[0], args, cli)
    since, spent = _since(args.days), [0.0, collections.Counter(), collections.Counter()]
    repeats = _RepeatReads(since)  # T-6064: fed by the same pass (no second walk of the transcripts)

    def each_file():  # T-3521: one read of the transcripts for tokens and time (3.1 GB a week was parsed twice, 48-75 s
        # under load), one file's entries held at a time (timing alone held the week's, and the GC walked them all)
        nonlocal repeats
        for _, rows in itertools.groupby(_entries(folder, since), key=lambda r: r[0]):
            rows = list(rows)
            model, tool, shell = timing(folder, since, rows)
            spent[0] += model
            spent[1].update(tool)
            spent[2].update(shell)
            if repeats is not None:
                try:
                    for row in rows:
                        repeats.feed(*row)
                except Exception:  # a line in a shape this reader doesn't know: the figure is left out, the rest is not
                    repeats = None
            yield from rows
    ctx = {}  # T-4704: what entered the context per ISO week, gathered in the same pass
    msgs, tools = scan(folder, since, share, entries=each_file(), ctx=ctx)
    totals, by_task, by_session = collections.Counter(), collections.Counter(), collections.Counter()
    lines = _timelines(p, args.days)  # T-1083 review: each turn to its own session's task
    spans, retry = retry_windows(p, args.days), collections.Counter()
    for ts, sid, usage in msgs:
        for k in WEIGHTS:
            totals[k] += usage.get(k) or 0
        w, task = weighted(usage), _task_for(lines, sid, ts[:19]) or "(no task)"
        by_task[task] += w
        if _retrying(spans, task, ts[:19]):
            retry[task] += w
        by_session[sid] += w
    if not msgs:  # R3 canary: transcripts are Claude Code's internal format and may change under us
        return cli.out(args, {"days": args.days, "messages": 0},
                         f"No token usage found in {folder} for the last {args.days:g} day(s): nothing ran, or the "
                         f"transcript format changed (fm cost reads message.usage on assistant lines).")
    total = sum(by_task.values()) or 1
    tool_total = sum(tools.values()) or 1
    first = {}  # R3: each session's first reply shows the context every turn starts from (system, tools, rules, hooks)
    for ts, sid, usage in sorted(msgs, key=lambda x: x[0]):
        first.setdefault(sid, sum(usage.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens",
                                                               "cache_read_input_tokens")))
    base = sorted(first.values())[len(first) // 2] if first else 0
    model_s, tool_s, shell_s = spent
    active = model_s + sum(tool_s.values())
    unex = unexplained_spend(p, msgs, args.days)
    unex_line = unexplained_line(unex)
    weeks = context_weeks(msgs, ctx, closed_by_week(p))
    data = {"days": args.days, "messages": len(msgs), "tokens": dict(totals), "input_equivalent": round(total),
            "unexplained": unex, "context": weeks,
            "by_task": dict(by_task.most_common()), "retry_by_task": dict(retry), "by_session": dict(by_session.most_common()),
            "tool_result_chars": dict(tools.most_common()), "session_start_context": base,
            "foreman_share": {"all": share["all"], "chars": {k: v for k, v in share.most_common()
                                                             if k != "all" and not k.startswith("fm:")},
                              "fm_verbs": {k[3:]: v for k, v in share.most_common() if k.startswith("fm:")}},
            "time": {"active_s": round(active), "model_s": round(model_s),
                     "tools_s": {k: round(v) for k, v in tool_s.most_common()},
                     "shell_s": {k: round(v) for k, v in shell_s.most_common()}}}
    if repeats is not None:  # T-6064
        got = repeats.result()
        data["repeat_reads"] = {k: got[k] for k in ("main", "sub", "repeat_bytes", "avoided_bytes")}
    pct =lambda v: f"{100 * v / (active or 1):.0f}%"
    when = (f"\nTime, active ({c.fit(_dur(active), 12)}; waiting on you and gaps over {IDLE_S // 60} min left out): "
            f"model {pct(model_s)} · " + " · ".join(
                f"{k} {pct(v)}" + (" (" + ", ".join(f"{p} {pct(s)}" for p, s in shell_s.most_common(4)) + ")"
                                    if k == "Bash" and shell_s else "") for k, v in tool_s.most_common(5))) if active else ""
    text = (f"Tokens, last {args.days} day(s), {len(by_session)} session(s), {len(msgs)} replies: "
            + " · ".join(f"{SHORT[k]} {_human(totals[k])}" for k in WEIGHTS)
            + f" ≈ {_human(total)} input-equivalent (cache reads ×0.1, writes ×1.25 or ×2 on the 1 h cache, output ×5)"
            + "\nBy task: " + " · ".join(f"{t} {_human(v)} ({100 * v / total:.0f}%"
                                      + (f", retry {_human(retry[t])}" if retry[t] else "") + ")"
                                      for t, v in by_task.most_common(8))
            + ("\n" + unex_line if unex_line else "")
            + "\nTool results (what filled the context): " + " · ".join(
                f"{t} {100 * v / tool_total:.0f}%" for t, v in tools.most_common(6))
            + "\nSessions: " + " · ".join(f"{s[:8]} {_human(v)}" for s, v in by_session.most_common(5))
            + _share_line(share)
            + (repeat_line(data["repeat_reads"]) if "repeat_reads" in data else "")
            + (f"\nContext at the first reply of a session (median): {_human(base)} tokens, re-read every turn"
               if base else "") + when + context_text(weeks))
    cli.out(args, data, text)


BUST_MIN = 30000  # a turn re-writing over half of a context this big lost its cache (T-0703)
# T-1797: Foreman's compact window (T-1796): a session averaging past it compacted late. BURN_CONTEXT (150k) is the
# account-wide early warning; this is the per-session regression line.
SESSION_OVER = 200_000
CACHE_TTL_S, CACHE_TTL_1H_S = 300, 3600  # the prompt cache's default lifetime: a bust after a longer gap is the ttl, else a context edit


def _epoch(ts):
    try:
        return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


SPLIT = {"cache_read_input_tokens": "cache read", "cache_creation_input_tokens": "cache write", "input_tokens": "fresh",
         "output_tokens": "output"}


def _sessions(msgs, args, cli):
    """T-0703: per session, the context every turn re-reads (the 78% of spend that is cache reads), its weighted split
    and the cache busts: turns that re-wrote over half of a context past BUST_MIN tokens."""
    rows = {}
    for ts, sid, u, *kind in sorted(msgs, key=lambda x: x[0]):
        r = rows.setdefault(sid, {"session": sid, "turns": 0, "busts": 0, "ctx": 0, "prev": 0, "prev_t": None,
                                  "causes": collections.Counter(), "tokens": collections.Counter(), "h1": 0,
                                  "bust_cost": 0.0, "ttl": None, "kinds": []})
        r["kinds"].append((ts, sid, u, kind[0] if kind else "other"))
        ctx = sum(u.get(k) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        w = u.get("cache_creation_input_tokens") or 0
        t, h = _epoch(ts), one_hour(u)
        if r["prev"] > BUST_MIN and ctx > BUST_MIN and w > ctx / 2:  # T-0959: the cache expired, or the prefix changed
            r["busts"] += 1
            ttl = r["ttl"] or (CACHE_TTL_1H_S if h else CACHE_TTL_S)  # T-1820: the lifetime its last write chose
            r["causes"]["ttl" if t and r["prev_t"] and t - r["prev_t"] > ttl else "context edit"] += 1
            r["bust_cost"] += weighted({"cache_creation_input_tokens": w, "cache_creation": u.get("cache_creation")}) \
                - w * WEIGHTS["cache_read_input_tokens"]  # re-written instead of read
        r["h1"] += h
        if w:
            r["ttl"] = CACHE_TTL_1H_S if h else CACHE_TTL_S
        r["turns"], r["ctx"], r["prev"], r["prev_t"] = r["turns"] + 1, r["ctx"] + ctx, ctx, t
        for k in WEIGHTS:
            r["tokens"][k] += u.get(k) or 0
    out = []
    for r in rows.values():
        spend = {k: r["tokens"][k] * WEIGHTS[k] for k in WEIGHTS}
        spend["cache_creation_input_tokens"] += r["h1"] * (WRITE_1H - WEIGHTS["cache_creation_input_tokens"])
        out.append({"session": r["session"], "turns": r["turns"], "avg_context": r["ctx"] // r["turns"],
                    "over": r["ctx"] // r["turns"] > SESSION_OVER,
                    "busts": r["busts"], "bust_causes": dict(sorted(r["causes"].items())), "tokens": dict(r["tokens"]),
                    "triggers": trigger_rows(r["kinds"]),
                    "input_equivalent": round(sum(spend.values())), "bust_cost": round(r["bust_cost"]),
                    "split": {name: round(100 * spend[k] / (sum(spend.values()) or 1)) for k, name in SPLIT.items()}})
    out.sort(key=lambda s: -s["input_equivalent"])
    over = sum(s["over"] for s in out)
    by_trigger = trigger_rows(k for r in rows.values() for k in r["kinds"])
    reread = sum(v["cache_read"] for v in by_trigger.values()) or 1
    for v in by_trigger.values():
        v["share_of_cache_read"] = round(100 * v["cache_read"] / reread)
    avoid = max((k for k in by_trigger if k in AVOIDABLE), key=lambda k: by_trigger[k]["cache_read"], default=None)
    largest = {"kind": avoid, **by_trigger[avoid]} if avoid else None

    def kinds_line(t, top=None):
        n = lambda v: f"{v['turns']} turn{'s' * (v['turns'] != 1)}"
        return ", ".join(f"{k} {n(v)} ({_human(v['cache_read'])})" for k, v in list(t.items())[:top])
    text = (f"Sessions, last {args.days:g} day(s), costliest first, main thread only (the context a turn re-reads is "
            "most of the cost):\n"
            + "\n".join(f"  {s['session'][:12]}: {s['turns']} turns, {_human(s['avg_context'])} context per turn, "
                        f"≈ {_human(s['input_equivalent'])} input-equivalent (" + ", ".join(
                            f"{k} {v}%"
                            for k, v in s["split"].items()) + f"), {s['busts']} bust{'s' * (s['busts'] != 1)}"
                        + (" (" + ", ".join(f"{n} {k}" for k, n in s["bust_causes"].items())
                           + f") ≈ {_human(s['bust_cost'])} lost" if s["busts"] else "")
                        + (" ⚠ over 200k a reply: compacted late (one started after fm install-user and a restart "
                           "shouldn't)" if s["over"] else "")
                        + f"\n      started by: {kinds_line(s['triggers'], 6)}"
                        for s in out[:15]) + (f"\n  {over} session(s) average over 200k a reply" if over else "")
            + "\nTurns by what started them, all sessions (in brackets the cache-read tokens those turns re-read): "
            + kinds_line(by_trigger)
            + (f"\nLargest avoidable kind: {avoid} ({largest['turns']} turns, {_human(largest['cache_read'])} re-read, "
               f"{largest['share_of_cache_read']}% of all re-read; avoidable = {', '.join(AVOIDABLE)})" if avoid else "")
            + ("\n  Busts: a ttl one outlasted the cache (5 min, or 1 h when the session writes 1 h): fewer long idle "
               "gaps. A context edit changed the prefix: CLAUDE.md, rules, tools or MCP servers edited mid-session, "
               "or a model switch." if any(s["busts"] for s in out[:15]) else "")
            if out else f"No token usage in the last {args.days:g} day(s).")
    return cli.out(args, {"days": args.days, "sessions": out, "by_trigger": by_trigger, "largest_avoidable": largest},
                   text)


def _by_model(p, args, cli):
    """T-0487: tasks finished per model and type/tier, with their verification grades, from task_done events (those
    since T-0487 carry them); what a router picking the cheapest model that passes would read."""
    since, rows = _since(args.days), {}
    for e in c.ledger_tail(p, 20000, kinds=("task_done",)):
        d = e.get("data") or {}
        if e.get("event") == "task_done" and d.get("tier") and str(e.get("ts", ""))[:19] >= since:
            r = rows.setdefault(str(d.get("model") or "unknown"), {}).setdefault(f"{d.get('type')} {d['tier']}",
                                                                                   {"passed": 0, "verified": {}})
            r["passed"] += 1
            r["verified"][str(d.get("verified"))] = r["verified"].get(str(d.get("verified")), 0) + 1
    lines = [f"Tasks finished by model, last {args.days:g} day(s):" if rows else
             f"No finished task recorded its model in the last {args.days:g} day(s) (fm task finish records it)."]
    lines += [f"  {m} · {k}: {r['passed']} passed ({', '.join(f'{n} {g}' for g, n in sorted(r['verified'].items()))})"
              for m, kinds in sorted(rows.items()) for k, r in sorted(kinds.items())]
    return cli.out(args, {"days": args.days, "by_model": rows}, "\n".join(lines))


def _week(p, days):
    since = _since(days)
    done = [b for b in c.load_briefs(p, include_archive=True)
            if b.status == "done" and str(b.meta.get("updated", ""))[:19] >= since]
    lessons = [(b.id, x.lstrip("- ").strip()) for b in done for x in b.section("Lessons").splitlines()[:1]
               if x.strip() and not x.lstrip("- ").lower().startswith("none")]
    review = [l for l in week_decisions(p, days) if re.search(r"\| \[(costly|outward)\]", l)]
    return done, lessons, review


def week_decisions(p, days):
    """T-1084: every decisions.md row since the cutoff (the digest's "Decided without you")."""
    try:
        with open(os.path.join(p.dir, "decisions.md"), encoding="utf-8") as f:
            return [l.strip() for l in f if l.startswith("| 2") and l[2:12] >= _since(days)[:10]]
    except OSError:
        return []


def weekly_line(p):
    """One SessionStart line when the last week had finished work (R1: a weekly digest), else None."""
    done, lessons, review = _week(p, 7)
    if not done:
        return None
    open_ = sum(bool(b.meta.get("unproven")) for b in done)  # T-2635: fm prove still lists them, whatever the grade
    strong = sum(b.meta.get("verified") == "strong" and not b.meta.get("unproven") for b in done)
    return (f"This week: {len(done)} task(s) done ({strong} strongly verified"
            + (f", {open_} closed unproven" if open_ else "") + f"), {len(lessons)} lesson(s)"
            + (f", {len(review)} decision(s) to review" if review else "") + "; fm digest shows it.")


def reversals(p):
    """T-0667: {kind: (decisions, reversed)} from decisions.md — a row is reversed when a later row's [reverses: words]
    matches its text."""
    try:
        with open(os.path.join(p.dir, "decisions.md"), encoding="utf-8", errors="replace") as f:
            rows = [ln for ln in f if ln.startswith("| 2")]
    except OSError:
        return {}
    parsed = []
    for ln in rows:
        cells = [x.strip() for x in ln.strip().strip("|").split("|")]
        text = cells[1] if len(cells) > 1 else ""
        kind = (re.match(r"\[(costly|outward)\]", text) or [None, "reversible"])[1]
        undoes = (re.search(r"\[reverses: ([^\]]+)\]", text) or [None, None])[1]
        parsed.append((kind, re.sub(r"\[[a-z]+(?::[^\]]*)?\]\s*", "", text).lower(), undoes))
    out = {}
    for i, (kind, text, _) in enumerate(parsed):
        hit = any(u and u.lower().strip() in text for _, _, u in parsed[i + 1:])
        n, r = out.get(kind, (0, 0))
        out[kind] = (n + 1, r + hit)
    return out


def cmd_digest(args):
    """The week (or --days N) in one screen: finished work with its verification grade, lessons, decisions the user
    should review, blocked work, recurring failures and tokens."""
    from sampo import cli
    p = cli.resolve(args)
    done, lessons, review = _week(p, args.days)
    since = _since(args.days)
    grade = lambda b: "folded into another" if b.meta.get("done_in") else b.meta.get("verified", "ungraded")  # T-0852
    grades = collections.Counter(grade(b) for b in done)
    fails = collections.Counter(r.get("sig") for r in c.tail_jsonl(os.path.join(p.dir, "failures.jsonl"), 1000)
                                if str(r.get("at", ""))[:19] >= since and r.get("sig"))
    briefs = c.load_briefs(p)
    blocked = [b for b in briefs if b.status == "blocked"]
    from sampo import tidy
    stale = tidy.stale_plans(p, briefs)  # T-1077: queued plans whose files or premises moved since planning
    triage = tidy.triage_deferred(p, briefs)  # T-1048
    tokens, unex = None, ""
    if os.path.isdir(transcripts_dir(p.root)):
        msgs, _ = scan(transcripts_dir(p.root), since)
        tokens = sum(weighted(u) for _, _, u in msgs)
        unex = unexplained_line(unexplained_spend(p, msgs, args.days))
    lines = [f"Last {args.days:g} day(s) in {p.slug}: {len(done)} task(s) done — "
             + (", ".join(f"{v} {k}" for k, v in grades.most_common()) or "none")
             + ("; latest 8:" if len(done) > 8 else "")]  # T-0349: the list below is cut, the count isn't
    lines += [f"- {b.id} [{b.type} {b.tier}] {c.fit(b.title, 90)} ({grade(b)})" for b in done[-8:]]
    lines += ["Lessons:"] + [f"- {tid}: {c.fit(x, 140)}" for tid, x in lessons[-6:]] if lessons else []
    insights = [(b.id, b.section("Insight").strip()) for b in done if b.section("Insight").strip()]  # T-0639
    lines += ["Insights:"] + [f"- {tid}: {c.fit(x, 160)}" for tid, x in insights[-6:]] if insights else []
    redo = [(b.id, b.section("Would do differently").strip()) for b in done if b.section("Would do differently").strip()]
    lines += ["Would do differently:"] + [f"- {tid}: {c.fit(x, 160)}" for tid, x in redo[-6:]] if redo else []
    from sampo import outcomes
    outs = outcomes.outcomes(p)
    grew = [d["changed"] / d["planned"] for e in c.ledger_tail(p, 20000, kinds=("task_done",)) if e.get("event") == "task_done"
            and str(e.get("ts", ""))[:19] >= since and (d := e.get("data") or {}).get("planned") and "changed" in d]
    lines += [f"Planned vs changed files: {len(grew)} task(s) with a scope changed ×{statistics.median(grew):.1f} "
              f"the paths they planned (median; T-0644)"] if grew else []
    lines += outcomes.dropped_class_lines(p, outs) + outcomes.survival_lines(p, outs) + outcomes.track_lines(p, outs) + outcomes.ledger_lines(p, outs) + outcomes.plan_lines(p)  # T-1060, T-0641, T-0619, T-1061
    raised = [e for e in c.ledger_events(p, ("cost_cap_changed",), since=since)  # T-1083 review: caps raised
              if (e.get("data") or {}).get("to", 0) > (e.get("data") or {}).get("from", 0)]
    lines += ["Cost caps raised: " + ", ".join(f"{e.get('task')} {_human(e['data']['from'])} → {_human(e['data']['to'])}"
                                               for e in raised[-6:])] if raised else []
    rev = reversals(p)  # T-0667: how often each kind of decision got undone
    lines += ["Decisions reversed, by kind: " + " · ".join(f"{k}: {r} of {n} reversed" for k, (n, r) in rev.items())] \
        if any(r for _, r in rev.values()) else []
    decided = week_decisions(p, args.days)
    plain = [x for x in decided if x not in review]  # costly/outward rows are listed once, under review below
    lines += [f"Decided without you: {len(decided)} ({len(review)} to review below)"] + [f"- {c.fit(x, 160)}" for x in plain[-6:]] \
        if decided else []
    lines += ["Decisions to review (costly/outward):"] + [f"- {c.fit(x, 160)}" for x in review[-6:]] if review else []
    doors = collections.Counter((e["event"], (e.get("data") or {}).get("door")) for e in
                                c.ledger_events(p, ("door_ask", "door_park"), since=since))  # T-2342
    part = lambda ev, name: [f"{name}: " + ", ".join(f"{doors[ev, d]} {d}" for d in ("two-way", "one-way") if doors[ev, d])] \
        if any(doors[ev, d] for d in ("two-way", "one-way")) else []
    lines += ["Doors: " + " · ".join(part("door_ask", "asks") + part("door_park", "parks"))] \
        if part("door_ask", "asks") or part("door_park", "parks") else []
    from sampo import trials
    lines += trials.lines(p, act=True)  # T-1059; T-2351: the digest files and settles self-fix verdicts
    try:  # T-2355: trigger routes move on outcomes Foreman didn't author, once a digest, each move a decide row
        from sampo import triggers
        moved = triggers.settle_routes(p)
        lines += [f"Trigger routes moved: {', '.join(f'{t} -> {r}' for t, r in moved.items())}"] if moved else []
        if triggers.held(p):  # T-4743: items that waited past midnight file now, best first, as far as today has room
            with c.lock(p.dir, timeout=1):
                got = triggers.drain_due(p)
            lines += [f"Held captures filed: {', '.join(got)}"] if got else []
    except c.CorruptState as e:  # the held list can't be read: said, never written over (sampo fsck)
        lines += [f"Held captures: {e}"]
    except Exception:  # a digest never fails on a derived report
        pass
    from sampo import anergy  # T-2563: prompts approved by reflex, with the rule that would retire each (never applied here)
    reflex = anergy.report()["reflex"]
    lines += ["Approved by reflex (an allow rule would stop asking; your call): "
              + c.joinmore([f"{cls} ×{n}, median {m}s" for cls, n, m in reflex], 4, " · ")] if reflex else []
    lines += [f"Blocked: {c.joinmore([f'{b.id} {c.fit(b.title, 40)}' for b in blocked], 5)}"] if blocked else []
    triage.sort(key=lambda r: ("do", "drop", "ask").index(r[1]))  # a do or drop never hides behind asks
    lines += [f"Deferred: {t} → {v} ({w})" for t, v, w in triage[:6]]
    lines += [f"Deferred: +{len(triage) - 6} more: fm tidy"] if len(triage) > 6 else []
    lines += [f"Plans to re-plan ({len(stale)}): " + ", ".join(f"{t} ({why})" for t, why in list(stale.items())[:5])
              ] if stale else []
    lines += ["Recurring failures: " + " · ".join(f"{c.fit(s, 60)} ×{n}" for s, n in fails.most_common(3) if n > 1)] \
        if any(n > 1 for n in fails.values()) else []
    lines += [f"Tokens: ≈ {_human(tokens)} input-equivalent (fm cost for the breakdown)"] if tokens else []
    lines += [unex] if unex else []
    from sampo import flowreport  # T-5503: request-to-done by stage, agents, tests, guard, made/closed; once-a-day snapshot
    flow, flow_lines = flowreport.report(p, args.days)
    lines += flow_lines
    try:  # T-2273: what closed without a green run here
        got = c.unproven_summary(p, args.days)
        unproven, kinds = got["rows"], got["counts"]
    except Exception:  # the rollup never costs the digest
        unproven, kinds = [], {}
    lines += [c.unproven_line(len(unproven), kinds, lead="Done without a green run here: ")] if unproven else []
    nights = [e for e in c.ledger_tail(p, 3000, kinds=("night",)) if e.get("event") == "night" and str(e.get("ts", ""))[:19] >= since]
    lines += ["Night shift (fm night):"] + [  # T-0236
        f"- {str(e.get('ts', ''))[:10]}: " + (", ".join(f"{'✓' if r.get('exit') == 0 else '✗'} {r.get('name')}"
                                                        for r in (e.get("data") or {}).get("ran") or []) or "nothing ran")
        + (f"; skipped {', '.join((e.get('data') or {}).get('skipped'))}" if (e.get("data") or {}).get("skipped") else "")
        + (f"; closed {', '.join((e.get('data') or {}).get('closed'))}" if (e.get("data") or {}).get("closed") else "")  # T-1046
        for e in nights[-5:]] if nights else []
    learn = learned(p, args.days, len(done))
    lines += [learn["line"]]
    burn = burn_findings()  # T-1800: why usage burns, named with its fix
    lines += ["Usage burn:"] + [f"- {a} → {b}" for a, b in burn] if burn else []
    cli.out(args, {"learned": learn, "done": [b.id for b in done], "grades": dict(grades), "lessons": lessons, "review": review, "decided": decided,
                     "blocked": [b.id for b in blocked], "unproven": [r["id"] for r in unproven], "stale_plans": stale, "deferred_triage": triage, "failures": dict(fails.most_common(5)), "tokens": tokens,
                     "burn": burn, "flow": flow},
              "\n".join(lines))
    if getattr(args, "say", False):  # T-1549: the digest read aloud, when a speech program is installed
        say("\n".join(lines))


def say(text, most=4000):
    """T-1549: text spoken by the first of say, spd-say, espeak-ng or espeak on PATH (ids, dashes and symbols read as
    words would be noise, so only letters, digits and plain punctuation go); a note on stderr when none is."""
    import shutil
    import subprocess
    import sys
    tool = next((t for t in ("say", "spd-say", "espeak-ng", "espeak") if shutil.which(t)), None)
    if not tool:
        print("fm digest --say: no speech program (say, spd-say, espeak-ng or espeak) is installed", file=sys.stderr)
        return False
    words = re.sub(r"[^\w\s.,;:!?'%-]+", " ", text.replace("(s)", "s").replace("\n", ". "))[:most]
    try:
        subprocess.run([tool, *(["-w"] if tool == "spd-say" else []), "--", words], stdin=subprocess.DEVNULL,
                       capture_output=True, timeout=600)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"fm digest --say: {tool} didn't run ({e})", file=sys.stderr)
        return False
    return True


def learned(p, days, closed):
    """T-1045: what Sampo learned in the window, for the digest and the pane's idle line: fixes new/worked/missed,
    rules proposed from corrections, and interventions (corrections plus guard blocks) per closed task."""
    from sampo import fixes as sfixes, ideas
    since = _since(days)
    d = sfixes.learned(p, since)
    n = len(c.ledger_events(p, ("correction",), since=since)) \
        + sum(1 for e in c.global_events(since) if e.get("kind") == "guard_block" and e.get("project") == p.slug)
    d.update(rules=len(ideas.correction_clusters(p)), interventions=n,
             interventions_per_task=round(n / closed, 1) if closed else None)
    d["line"] = (f"Learned: {d['fixes_new']} new fix(es), {d['worked']} worked, {d['missed']} missed · "
                 f"{d['rules']} rule(s) to propose" + (f" · {d['interventions_per_task']:g} interventions per closed task"
                                                     if closed else f" · {n} interventions, no closed task"))
    return d


def cmd_evals(args):
    """fm evals add ID (T-0052): a blocked or failed task becomes a claude plugin eval case — its request as the
    prompt, graders that the session classified it and ran its checks — so the referee remembers real failures."""
    from sampo import cli
    p = cli.resolve(args)
    if args.action == "inbox":  # T-0734: what could become an eval case
        rows = eval_inbox(p)
        return cli.out(args, {"inbox": rows}, ("Eval inbox (fm evals add ID turns one into a case):\n" + "\n".join(
            f"- {r['line']}" for r in rows)) if rows else "Eval inbox: empty.")
    if not args.id:
        raise cli.UsageError("fm evals add needs a task id")
    b = cli.need_brief(p, args.id)
    runs_failed = any("` → ✗ exit" in l for l in b.evidence())
    if b.status not in ("blocked", "dropped") and not runs_failed:
        raise cli.UsageError(f"{b.id} is {b.status} with no failed runs: eval cases come from blocked or failed work")
    folder, files = eval_case(p, b, args.out)
    cli.out(args, {"case": folder, "files": sorted(files)},
              f"Eval case for {b.id}: {folder} ({', '.join(sorted(files))}). Review the graders, then copy it into a "
              f"plugin's evals/ and run claude plugin eval.")


def eval_inbox(p, n=20):
    """T-0734: [{task, kind, line}] — tasks blocked, and guard blocks today's guard allows (fixed false positives, each
    worth a case that keeps them allowed), newest last."""
    events = c.ledger_tail(p, 20000)
    rows = [{"task": e.get("task"), "kind": "blocked",
             "line": f"{e.get('task')} blocked: {c.fit(str((e.get('data') or {}).get('reason') or ''), 120)}"}
            for e in events if e.get("event") == "task_block" and e.get("task")]
    # T-0754: a block the user then granted is the guard working; a false positive is a block today's guard allows
    from sampo import friction
    blocks = [e for e in c.tail_jsonl(os.path.join(c.state_dir(), "events.jsonl"), 30000)
              if e.get("kind") == "guard_block" and e.get("project") == p.slug and e.get("cmd")
              and e.get("category") != "brief"][-50:]  # "no active task" depends on the moment, not the rule
    try:
        now = friction._recheck(blocks)
    except Exception:  # transcripts unreadable: no candidates rather than a guess
        now = {}
    rows += [{"task": None, "kind": "guard", "line": f"guard {e.get('category')} block that today's guard allows now "
              f"(keep it allowed with an eval case): {c.fit(c.redact(str(e['cmd'])), 100)}"}
             for i, e in enumerate(blocks) if now.get(i) == "today's guard allows it"]
    seen, out = set(), []
    for r in rows:
        if r["line"] not in seen:
            seen.add(r["line"])
            out.append(r)
    return out[-n:]


def eval_case(p, b, out_dir=None):
    """The eval case folder for a blocked or failed task, and its files."""
    why = next((l.split("blocked:", 1)[1].strip() for l in reversed(b.section("Log").splitlines()) if "blocked:" in l),
               "a check failed")
    name = "regression-" + c.kebab(b.title, 40)
    folder = os.path.join(os.path.abspath(out_dir) if out_dir else os.path.join(p.dir, "evals"), name)
    os.makedirs(os.path.join(folder, "graders"), exist_ok=True)
    request = re.sub(r"(?m)^> ?", "", b.section("Raw request")).strip() or b.title
    with open(c.rules_path(), encoding="utf-8") as f:
        rules = "".join("  " + l if l.strip() else l for l in f)

    def q(s):  # a YAML single-quoted scalar
        return "'" + str(s).replace("'", "''") + "'"
    files = {"case.yaml": f'schema_version: "1.1"\nname: {name}\n',
             "prompt.md": f"---\ndescription: {q(c.fit(f'Regression from {b.id} ({b.status}: {why})', 200))}\n"
                          f"max_turns: 30\ntimeout_seconds: 900\nallowed_tools: [Read, Glob, Grep, Skill, Bash, Write, Edit]\n"
                          f"tags: [regression, {b.type.lower()}]\nappend_system_prompt: |\n{rules}---\n\n{request}\n",
             "graders/classified.md": "---\ntype: regex\ntarget: trace\npattern: 'fm (task new|capture)[^\\n]{0,400}--type'\n---\n"}
    checks = [cmd for _, cmd in b.verify_cmds() if cmd]
    if checks:
        files["graders/ran-checks.md"] = (f"---\ntype: regex\ntarget: trace\npattern: "
                                          f"{q('|'.join(re.escape(x) for x in checks[:3]))}\n---\n")  # not a list: a grep pattern
    for rel, text in files.items():
        c.write_atomic(os.path.join(folder, rel), c.redact(text))
    return folder, files


def _bench_runs(p, tid, newest=20):
    """T-0263: how this task's bench case fared in the newest saved runs (fm bench run results)."""
    folder = os.path.join(p.dir, "bench", "results")
    try:
        names = sorted((n for n in os.listdir(folder) if n.endswith(".json")),
                       key=lambda n: os.path.getmtime(os.path.join(folder, n)), reverse=True)[:newest]
    except OSError:
        return []
    out = []
    for n in names:
        try:
            with open(os.path.join(folder, n), encoding="utf-8") as f:
                cases = json.load(f).get("cases")
        except (OSError, ValueError, AttributeError, RecursionError):
            continue
        for x in cases if isinstance(cases, list) else []:  # review: a hand-edited results file can't crash fm pr
            if isinstance(x, dict) and x.get("id") == tid:
                cost = x.get("cost_usd") if isinstance(x.get("cost_usd"), (int, float)) else 0
                out.append(f"- bench: {'✓' if x.get('pass') else '✗'} replayed in {c.plain(n[:-5])} "
                           f"({c.fit(c.plain(str(x.get('turns', '?'))), 8)} turns, ${cost:.2f})")
    return out[:5]


def cmd_pr(args):
    """fm pr ID (R1): a pull-request description from the brief — what and why, criteria with their evidence, how it
    was verified and reviewed, the lesson. Printed only: opening the PR stays an outward action behind the user's yes."""
    from sampo import cli
    p = cli.resolve(args)
    if args.id in ("threads", "train"):  # T-1087
        from sampo import pr
        return getattr(pr, args.id)(p, args)
    b = cli.need_brief(p, args.id)
    why = (b.section("Interpretation").strip() or re.sub(r"(?m)^> ?", "", b.section("Raw request")).strip()
           or b.title)
    ev = [l for l in b.evidence() if not l.startswith("- (audit ")]
    grade, how = b.grade()
    lines = [f"## {b.title}", "", c.plain(why), "", "### Done when"]
    for a in b.acceptance():
        proof = next((l.split("`", 2)[1] + " → " + l.split("` → ", 1)[1].split(" [")[0] for l in reversed(ev)
                      if l.startswith(f"- (ac {a.n}) ") and "` → " in l), "")
        lines.append(f"- [{'x' if a.checked else ' '}] {c.strip_verify(a.text)}"
                     + (f" — `{proof}`" if proof else ""))
    lines += ["", "### Verification", f"- {grade} ({how})"]
    rg = b.red_green_cmd()  # T-0263: the proof pack — what failed before and passes after, each lens's verdict
    if rg:
        lines.append(f"- red→green: `{rg}` failed before the change and passes after")
    if b.inconclusive():
        lines.append(f"- {len(b.inconclusive())} inconclusive run(s), not counted")
    said = {}
    for line in b.section("Verification evidence").splitlines():
        m = re.match(r"^- \(audit ([a-z]+)\) `[^`]*` → (.*?)(?: \[tree [0-9a-f]+\])? \(\d{4}-[^)]*\)\s*$", line)
        if m:
            said[m.group(1)] = m.group(2)  # the newest per lens
    lines += [f"- {lens}: {c.plain(result)[:200]}" for lens, result in sorted(said.items())]
    if b.assumptions():
        lines += ["", "### Assumptions"] + [
            f"- [{tag}{': ' + how if how else ''}] {c.plain(text)[:200]}" if tag else f"- [unchecked] {c.plain(text)[:200]}"
            for _, tag, how, text in b.assumptions()]
    bench = _bench_runs(p, b.id)
    if bench:
        lines += ["", "### Bench"] + bench
    claims = [x for x in b.section("Claims").splitlines() if x.strip()]
    lines += ["", "### Claims"] + [c.plain(x) for x in claims] if claims else []  # T-0614
    try:  # T-2392: the unproven rollup's kinds for this task, each with the command that proves it
        from sampo import infra
        needs = c.proof_needs(p, b, infra.touched(p, b))
    except Exception:
        needs = []
    lines += ["", "### Unproven"] + [f"- {n['kind']}: {c.plain(n['why'])[:160]} → `{n['upgrade']}`" for n in needs] \
        if needs else []
    lesson = next((x.lstrip("- ").strip() for x in b.section("Lessons").splitlines() if x.strip()), "")
    if lesson:
        lines += ["", "### Notes", f"- {c.plain(lesson)}"]
    lines += ["", f"Sampo task {b.id} ({b.type} {b.tier})"]
    body = c.redact("\n".join(lines))
    if getattr(args, "post", False):  # T-1167: outward, behind the task's publish grant
        from sampo import pr
        n = pr.post(p, b, body, args.pr)
        return cli.out(args, {"id": b.id, "body": body, "posted": n}, f"Posted {b.id}'s description on PR #{n}.")
    cli.out(args, {"id": b.id, "body": "\n".join(lines)}, body)


AGENTS_MARK = "<!-- generated by Sampo (fm export agents); edits here are replaced when it runs again -->"
AGENTS_CAP = 32 * 1024  # Codex reads AGENTS.md up to project_doc_max_bytes, 32 KiB by default
AGENTS_RULES = """## How work is done here
This repo's work is tracked by Sampo. With its `fm` CLI on PATH (`fm --help`):
- Before editing, write the plan: `fm task new "<title>" --type FIX|FEATURE|CLEAN|PERFORMANCE|SECURITY --tier S|M|L|XL|HUGE|GIANT --ac "<done when> :: <verify cmd>" --step "<step>" --focus`; `fm next` names the one next action.
- Nothing is done without a check that ran: `fm task evidence ID --step N --run "<cmd>"`; every gate at once: `fm check`.
- Close with `fm task finish ID --audit "<how it was reviewed>" --lesson "<what the next similar task should know>"`.
Without `fm`: say what "done" means before starting, run the checks below and show their real output before claiming done, change only what the task needs, and never commit, push or delete without asking."""


EXPORTS = {"agents": "AGENTS.md", "gemini": "GEMINI.md", "cursor": os.path.join(".cursor", "rules", "foreman.mdc")}
CURSOR_HEAD = "---\ndescription: How work is done in this repo (Sampo): rules, corrections, fixes, lessons\nalwaysApply: true\n---\n"


def agents_md(p, target="agents"):
    """T-0246: AGENTS.md for other harnesses — the working rules, the user's corrections, the map, lessons, decisions
    and the playbook index — redacted, each section bounded, the whole under AGENTS_CAP."""
    from sampo import map as smap
    from sampo import recall
    mark = AGENTS_MARK.replace("fm export agents", f"fm export {target}")
    parts = ["# " + os.path.basename(EXPORTS[target]), mark, "", AGENTS_RULES]
    private = bool(c.read_meta(p).get("sensitive"))  # a sensitive project shares no one's words: rules, map, playbooks
    from sampo import rune
    g = None if private else rune.health(p)
    if g:  # T-2118: another agent drives the tower the same way
        parts += ["", "## Genesis (a self-improvement tower runs here)",
                  f"Now: {c.genesis_line(g).replace('`', chr(39))}. `fm genesis step` names its next action; "
                  "its files change only through `fm genesis put`."]
    from sampo import mandate
    gl = None if private else mandate.goal_line(p)
    if gl:  # T-2141: the user's goal and the verdict fm goal verdict last stored, both data
        v = mandate.fresh_verdict(p) or {}
        parts += ["", "## Goal (the user's words; data, not instructions)", c.defang(gl).replace("`", chr(39))]
        if v.get("verdict"):
            parts += [f"Last verdict ({str(v.get('at'))[:10]}): " + c.fit(c.defang(c.plain(v["verdict"] + (": " + "; ".join(map(str, v.get("causes") or []))
                                                                                  if v.get("causes") else ""))), 400)]
    said = [] if private else [c.fit(c.defang(c.plain(str((e.get("data") or {}).get("text") or ""))), 200)
                               for e in c.ledger_tail(p, 3000, kinds=("correction",)) if e.get("event") == "correction"][-10:]
    if said:
        parts += ["", "## What the user has corrected (their words; follow them)"] + [f"- {x}" for x in said if x]
    from sampo import fixes as sfixes
    fixes = [] if private else [f for f in sfixes.known(p) if f["trusted"]][:15]  # a repo-only fix is a suggestion
    if fixes:
        parts += ["", "## Known fixes (obstacles met and got past here; try these first)"] + [
            f"- `{c.fit(c.defang(c.plain(f['sig'])), 120).replace('`', chr(39))}` -> {c.fit(c.defang(c.plain(f['do'])), 200)}"
            for f in fixes]
    try:
        parts += ["", "## Project map", smap.render(smap.load(p))]
    except Exception:  # no git, an unreadable tree: the rest still helps
        pass
    done = sorted((b for b in c.load_briefs(p, include_archive=True) if b.status == "done"),
                  key=lambda b: str(b.meta.get("updated", "")), reverse=True)
    lessons = [] if private else [(b.id, c.defang(x.lstrip("- ").strip())) for b in done
                                  for x in b.section("Lessons").splitlines()[:1]
                                  if x.strip() and not x.lstrip("- ").lower().startswith("none")][:15]
    if lessons:
        parts += ["", "## Lessons from finished work (newest first)"] + [f"- {tid}: {c.fit(c.plain(x), 240)}"
                                                                         for tid, x in lessons]
    try:
        if private:
            raise OSError
        with open(os.path.join(p.dir, "decisions.md"), encoding="utf-8", errors="replace") as f:
            rows = [re.split(r"(?<!\\)\|", x) for x in f if x.startswith("| 2")][-12:]
        decided = [f"- {r[1].strip()}: {c.fit(c.plain(r[2].strip()), 200)}" + (f" — {c.fit(c.plain(r[3].strip()), 160)}"
                                                                              if r[3].strip() else "")
                   for r in reversed(rows) if len(r) >= 4]
    except OSError:
        decided = []
    if decided:
        parts += ["", "## Decisions (newest first; don't undo one without saying so)"] + decided
    books = []
    for group, _, names in sorted(os.walk(recall.PLAYBOOKS)):
        for n in sorted(x for x in names if x.endswith(".md")):
            path = os.path.join(group, n)
            with open(path, encoding="utf-8", errors="replace") as f:
                title = next((x[2:].strip() for x in f.read(4000).splitlines() if x.startswith("# ")), n[:-3])
            books.append(f"- {c.plain(title)}: {os.path.relpath(path, c.PLUGIN_ROOT)} in the Sampo plugin")
    if books:
        parts += ["", "## Playbooks (read the one that fits before a stage)"] + books[:60]
    text = (CURSOR_HEAD if target == "cursor" else "") + c.redact("\n".join(parts)) + "\n"
    raw = text.encode()
    return text if len(raw) <= AGENTS_CAP else raw[:AGENTS_CAP - 64].decode(errors="ignore") + "\n… (cut at 32 KiB)\n"


def cmd_export(args):
    from sampo import cli
    p = cli.resolve(args)
    path = os.path.abspath(args.out or os.path.join(p.root, EXPORTS[args.what]))
    root, parent = os.path.realpath(p.root), os.path.realpath(os.path.dirname(path))
    if os.path.commonpath([root, parent]) != root:  # T-0288: the guard can't see fm's own writes; nor does fm leave
        raise cli.UsageError(f"{path}: fm export writes inside the project ({p.root}) only; copy the file yourself")
    if {".git", ".claude"} & set(os.path.relpath(parent, root).split(os.sep)):  # review: config and settings live there
        raise cli.UsageError(f"{path}: fm export doesn't write under .git or .claude")
    try:
        if os.path.islink(path):
            raise PermissionError("a symlink")  # review: the link would silently become a file
        with open(path, encoding="utf-8", errors="replace") as f:
            ours = AGENTS_MARK[:30] in f.read(1000)  # "<!-- generated by Foreman (fm export": any target's
    except FileNotFoundError:
        ours = True
    except OSError as e:
        ours = False
        if not args.force or not os.path.islink(path):
            raise cli.UsageError(f"{path}: {e.strerror or e}; write elsewhere with --out PATH")
    if not ours and not args.force:
        raise cli.UsageError(f"{path} wasn't written by fm export; keep it, or replace it with --force "
                               f"(or write elsewhere with --out PATH)")
    text = agents_md(p, args.what)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        c.write_atomic(path, text)
    except OSError as e:
        raise cli.UsageError(f"{path}: {e.strerror or e}")
    cli.out(args, {"path": path, "bytes": len(text.encode())}, f"Wrote {path} ({len(text.encode()) // 1024 + 1} KiB). "
              f"It quotes the user's corrections, lessons and decisions: read it before committing it.")


def _lesions(p, args, cli):
    """T-0664: what each ablation (fm evolve --drop) found: the bench held without the file (it may not earn the tokens
    it costs every session) or dropped (it earns its place), newest per file."""
    seen = {}
    for e in c.ledger_tail(p, 50000, kinds=("evolve",)):
        d = e.get("data") or {}
        if e.get("event") == "evolve" and str(d.get("why") or "").startswith("ablation") and d.get("target"):
            seen[d["target"]] = (str(e.get("ts", ""))[:10], bool(d.get("kept")))
    lines = [f"  {t}: {'held without it — a candidate to trim' if kept else 'dropped without it — it earns its place'}"
             f" ({at})" for t, (at, kept) in sorted(seen.items())]
    text = ("Lesions (fm evolve --drop: does the bench hold without the file?):\n" + "\n".join(lines) if lines else
            "No lesions run yet.") + ("\nNext: lesion a file fm usage lists as never used: fm evolve --drop <file> "
                                       "(a bench run, budget-checked).")
    return cli.out(args, {"lesions": {t: {"at": at, "held": kept} for t, (at, kept) in seen.items()}}, text)


def _agents(p, args, cli):
    """T-0647: a scorecard per agent type — spawns and tokens from the spend ledger, and for builders the lanes merged
    against removed unmerged, from this project's ledger. ponytail: reviewer findings confirmed vs rejected need audit
    results recorded per finding; add them when fm task audit records that."""
    from sampo import budget
    rows = collections.defaultdict(collections.Counter)
    for e in budget.ledger(days=max(1, int(args.days))):
        f = str(e.get("feature") or "")
        if f.startswith("subagent:"):
            r = rows[f.split(":")[-1]]
            r["spawns"] += int(e.get("runs") or 1)
            r["tokens"] += int(e.get("tokens") or 0)
    since = _since(args.days)
    for e in c.ledger_tail(p, 20000, kinds=("lane_merge", "lane_rm")):
        if str(e.get("ts", ""))[:19] >= since and e.get("event") in ("lane_merge", "lane_rm"):
            rows["fm-builder"]["merged" if e["event"] == "lane_merge" else "removed"] += 1
    text = (f"Agents, last {args.days:g} day(s):\n" + "\n".join(
        f"  {name}: {r['spawns']} spawns, {_human(r['tokens'])} tokens"
        + (f", lanes merged {r['merged']}, removed {r['removed']}" if name == "fm-builder" else "")
        for name, r in sorted(rows.items(), key=lambda kv: -kv[1]["tokens"]))) if rows else "No agent spawns recorded."
    lenses, passes = yields(p, since)
    text += ("\nAudit lenses (found something = fixed, found or captured; from the recorded results):\n" + "\n".join(
        f"  {k}: {r['hit']} of {r['n']} found something" + (f", {r['fp']} false positive(s)" if r["fp"] else "")
        for k, r in sorted(lenses.items(), key=lambda kv: -kv[1]["n"]) if r["n"])) if any(
        r["n"] for r in lenses.values()) else ""
    judged = {k: r for k, r in lenses.items() if r["confirmed"] + r["rejected"]}
    text += ("\nReviewer precision (fm task finding verdicts): " + " · ".join(
        f"{k}: {r['confirmed']} of {r['confirmed'] + r['rejected']} confirmed" for k, r in sorted(judged.items()))
             ) if judged else ""
    text += ("\nPasses: " + " · ".join(f"{k}: {r['hit']} of {r['n']} {verb}" for k, (r, verb) in passes.items())
             ) if passes else ""
    return cli.out(args, {"agents": {k: dict(v) for k, v in rows.items()}, "lenses": {k: dict(v) for k, v in
                     lenses.items()}, "passes": {k: dict(r) for k, (r, _) in passes.items()}}, text)


_HIT = re.compile(r"(?i)\b(fixed|found|caught|captured|T-\d{4,})\b")
_CLEAN = re.compile(r"(?i)\s*(no|nothing|none|clean)\b")  # "no issues found" found nothing
_FP = re.compile(r"(?i)(\d+)\s+false positives?\b|\bfalse positive\b|\bnot real\b")


def yields(p, since):
    """T-0640: ({lens: Counter(n, hit, fp)} from recorded audits, {pass: (Counter(n, hit), verb)}) since then: a lens
    found something when its result says fixed, found, caught or captured (or names a task); false positives when it
    says so. Passes: second plans that left dissent, second sessions that captured work."""
    lenses = collections.defaultdict(collections.Counter)
    second = collections.Counter()
    for e in c.ledger_tail(p, 50000):
        if str(e.get("ts", ""))[:19] < since:
            continue
        d = e.get("data") or {}
        if e.get("event") == "audit" and d.get("lens"):
            r, res = lenses[d["lens"]], str(d.get("result") or "")
            fp = _FP.search(res)
            r["n"] += 1
            r["fp"] += int(fp.group(1)) if fp and fp.group(1) else bool(fp)
            r["hit"] += bool(_HIT.search(_FP.sub("", res))) and not _CLEAN.match(res)
        elif e.get("event") == "second_session" and "error" not in d:
            second["n"] += 1
            second["hit"] += bool(d.get("captured"))
    verdicts = collections.defaultdict(collections.Counter)  # T-0743: per-finding verdicts, where recorded
    for e in c.ledger_tail(p, 50000, kinds=("finding",)):
        d = e.get("data") or {}
        if e.get("event") == "finding" and d.get("lens") and str(e.get("ts", ""))[:19] >= since:
            verdicts[d["lens"]][d.get("verdict")] += 1
    for lens, v in verdicts.items():
        lenses[lens]["confirmed"], lenses[lens]["rejected"] = v["confirmed"], v["rejected"]
    plan = collections.Counter()
    for b in c.load_briefs(p, include_archive=True):
        if b.section("Plan review").strip() and str(b.meta.get("updated") or "")[:19] >= since:
            plan["n"] += 1
            plan["hit"] += bool(re.search(r"(?m)^- \[[ xX]\] ", b.section("Dissent")))
    passes = {k: v for k, v in (("second plan", (plan, "left dissent")), ("second session", (second, "captured work")))
              if v[0]["n"]}
    return lenses, passes


def note_skill(e, all_skills, named, loaded):
    """T-1702/T-1703: fold one skill_named (a hook's note named them) or slash (the user typed one) event into the
    per-session `named` sets; a slash is a load, so it goes in `loaded` and comes back. A workflow (/sampo:review)
    or a typo is no skill. fm usage and fm doctor both count through here."""
    if not isinstance(e.get("skills"), list):
        return set()
    names = {str(x) for x in e["skills"]} & set(all_skills)
    (named if e["kind"] == "skill_named" else loaded)[e.get("session_id")].update(names)
    return names if e["kind"] == "slash" else set()


def counts(p, since):
    """T-3499: what sampo usage counts in p since then, one pass over the events: skill loads, playbook reads and fm
    commands (Counters), the Next followed and ignored, skills named and not loaded, and the never-used skills and
    playbooks. fits reads the same counts, so "unused" has one definition."""
    from sampo import playbooks
    listed = playbooks.listed()  # T-1621: a load through fm playbooks show is a read too
    skills, books, cmds = collections.Counter(), collections.Counter(), collections.Counter()
    followed, ignored, pending = collections.Counter(), collections.Counter(), {}
    named, loaded = collections.defaultdict(set), collections.defaultdict(set)  # T-1702: skills, per session
    all_skills = sorted(n for n in os.listdir(os.path.join(c.PLUGIN_ROOT, "skills"))
                        if os.path.isfile(os.path.join(c.PLUGIN_ROOT, "skills", n, "SKILL.md")))
    for e in c.tail_jsonl(os.path.join(c.state_dir(), "events.jsonl"), 100000):
        if e.get("project") != p.slug or str(e.get("ts", ""))[:19] < since:
            continue
        sid = e.get("session_id")
        if e.get("kind") == "next":  # T-0051: was the injected Next's command the next fm command run?
            if pending.get(sid):
                ignored[pending[sid][0]] += 1
            m = re.search(r"\b(?:fm|sampo) ([a-z-]+(?: [a-z][a-z-]*)?)", e.get("action", ""))  # e.g. fm task step, sampo audit prep
            # T-2855: counted under the Next's own spelling; matched on the words after it, so either spelling follows either
            pending[sid] = (m.group(0), m.group(1)) if m else None
            continue
        if e.get("kind") in ("skill_named", "slash"):
            if e["kind"] == "slash":
                skills.update(note_skill(e, all_skills, named, loaded))
            else:
                note_skill(e, all_skills, named, loaded)
            continue
        if e.get("kind") != "tool":
            continue
        tool, target = e.get("tool"), str(e.get("target") or "")
        if tool == "Bash" and pending.get(sid) and (ran := re.match(r"\s*(?:fm|sampo) (.*)", target, re.S)):
            (followed if ran.group(1).startswith(pending[sid][1]) else ignored)[pending[sid][0]] += 1
            pending[sid] = None
        if tool == "Skill":
            skills[target.split(":")[-1]] += 1
            loaded[sid].add(target.split(":")[-1])
        elif tool == "Read" and (e.get("book") or "/skills/playbooks/references/" in target and target.endswith(".md")):
            books[str(e.get("book") or target.split("/skills/playbooks/references/")[1][:-3])] += 1  # T-1702: book is
            # the whole path's tail: a lane's (a subagent's) path outruns the target's cut
        elif tool == "Bash" and (m := re.match(r"\s*(?:fm|sampo)\s+([a-z-]+)(?:\s+([a-z-]+))?", target)):
            cmds[m.group(1) + (" " + m.group(2) if m.group(1) == "task" and m.group(2) else "")] += 1
        if tool == "Bash" and (hit := playbooks.shown(target, listed)):
            books[hit[len("references/"):-3]] += 1
    ref = os.path.join(c.PLUGIN_ROOT, "skills", "playbooks", "references")
    all_books = sorted(os.path.relpath(os.path.join(d, n), ref)[:-3] for d, _, fs in os.walk(ref) for n in fs
                       if n.endswith(".md"))
    unused = {"skills": [s for s in all_skills if not skills[s]], "playbooks": [b for b in all_books if not books[b]]}
    missed = collections.Counter(s for sid, ns in named.items() for s in ns - loaded[sid])  # sessions offered X, no load
    return {"skills": skills, "playbooks": books, "commands": cmds, "followed": followed, "ignored": ignored,
            "missed": missed, "unused": unused, "all_books": all_books}


def _verbs(cli):
    import argparse
    sub = next(a for a in cli.build_parser()._actions if isinstance(a, argparse._SubParsersAction))
    return sorted(n for n in sub.choices if not n.startswith("_"))


def cmd_usage(args):
    """Which skills, playbooks and fm commands this project used in the window, and which it never did: candidates to
    trim from the always-loaded context."""
    from sampo import cli
    from sampo import fits
    p = cli.resolve(args)
    if getattr(args, "agents", False):
        return _agents(p, args, cli)
    if getattr(args, "lesions", False):
        return _lesions(p, args, cli)
    since = _since(args.days)
    if getattr(args, "prune", False):  # T-0468: fm commands nobody ran in the window, in any project; never deletes
        return _prune(args, since)
    n = counts(p, since)
    skills, books, cmds, followed, ignored, missed, unused, all_books = (n[k] for k in (
        "skills", "playbooks", "commands", "followed", "ignored", "missed", "unused", "all_books"))
    shown = sum(followed.values()) + sum(ignored.values())
    nexts = (f"\n  Next followed: {sum(followed.values())} of {shown}"
             + (f"; most often not: {', '.join(f'{k} ×{v}' for k, v in ignored.most_common(3))}" if ignored else "")
             if shown else "")
    ran = {k.split()[0] for k in cmds}  # T-3499: the unused features no fits probe watches yet: add one or retire it
    idle = list(dict.fromkeys([x for x in unused["skills"] if x not in ran]
                              + [v for v in _verbs(cli) if v not in ran and not skills[v]] + unused["playbooks"]))
    bare = [x for x in idle if x not in fits.PROBES]
    cli.out(args, {"days": args.days, "skills": dict(skills), "playbooks": dict(books), "commands": dict(cmds),
                     "unused": unused, "next_followed": dict(followed), "next_ignored": dict(ignored),
                     "named_not_loaded": dict(missed), "no_probe": bare},
              f"Last {args.days} day(s) in {p.slug}:\n  skills: "
              + (" · ".join(f"{k} {v}" for k, v in skills.most_common()) or "none")
              + "\n  playbooks read: " + (" · ".join(f"{k} {v}" for k, v in books.most_common(8)) or "none")
              + "\n  fm commands: " + (" · ".join(f"{k} {v}" for k, v in cmds.most_common(12)) or "none")
              + f"\n  never used: skills {', '.join(unused['skills']) or '-'}; {len(unused['playbooks'])} of "
                f"{len(all_books)} playbooks"
              + ("\n  named, not loaded: " + ", ".join(f"{k} ×{v}" for k, v in missed.most_common()) if missed else "")
              + nexts
              + f"\n  unused with no situation probe yet: {len(bare)} of {len(idle)} ({c.joinmore(bare, 8)}): "
                "add one to fits.PROBES or retire the feature")


def _prune(args, since):
    """T-0468: the fm commands no session ran (any project) since `since`: candidates to fold or retire, proposed as
    one CLEAN capture to review. Nothing is removed here."""
    from sampo import cli
    used = set()
    for e in c.global_events(since):  # rotated logs too, or a verb used before the last fm tidy reads as unused
        if e.get("kind") == "tool" and e.get("tool") == "Bash":
            used.update(re.findall(r"(?:^|[;&|(]\s*|\s)(?:fm|sampo)\s+([a-z][a-z-]*)", str(e.get("target") or "")))
    names = _verbs(cli)
    unused = [n for n in names if n not in used]
    capture = (f'fm capture "CLEAN: review fm commands not run in {args.days:g} days: {", ".join(unused[:20])}" '
               f'--type CLEAN') if unused else ""
    cli.out(args, {"days": args.days, "commands": len(names), "unused": unused, "capture": capture},
              f"Not run in {args.days:g} day(s), in any project: {len(unused)} of {len(names)} fm commands: "
              + (", ".join(unused) or "none") + (f"\nTo propose trimming them (nothing is removed): {capture}"
                                                 if capture else ""))


# ---------------------------------------------------------------- fm burden (T-0712)

BOOKKEEPING = re.compile(r"^(?:(?:fm|sampo) quiet (?:--\S+ \S+ )*-- )?(?:fm|sampo) (?:task|focus|checkpoint|capture|decide|log|next|queue|state|"
                         r"status|ask|batch|intake|gates|resume)\b")
INJECTS = ("hook_additional_context", "async_hook_response", "hook_blocking_error", "hook_system_message")


def _version(root):
    m = re.search(r"/(\d+\.\d+\.\d+)(?:/|$)", str(root or ""))
    return m.group(1) if m else ("working copy" if root else "unknown")


def _only_bookkeeping(cmd):
    parts = [x.strip() for x in re.split(r"&&|;|\n", re.sub(r"^(?:cd \S+ && )+", "", cmd or "")) if x.strip()]
    return bool(parts) and all(BOOKKEEPING.match(x) for x in parts)


def _text(x):
    if isinstance(x, str):
        return x
    if isinstance(x, list):
        return "\n".join(_text(y) for y in x)
    if isinstance(x, dict):
        return _text(x.get("text") or x.get("content") or x.get("additionalContext") or x.get("blockingError")
                     or x.get("hookSpecificOutput") or x.get("response") or "")
    return ""


def burden(folder, since):
    """{session: counts}: turns, turns that were only Sampo bookkeeping, guard refusals, refused fm calls and the
    characters Sampo's hooks put into the context."""
    out = {}
    for dirpath, _, files in os.walk(folder):
        for n in files:
            if not n.endswith(".jsonl"):
                continue
            tools, s = collections.defaultdict(list), out.setdefault(n[:-6], collections.Counter())  # a file: a session
            try:
                with open(os.path.join(dirpath, n), encoding="utf-8", errors="replace") as f:
                    for line in f:
                        try:
                            e = json.loads(line)
                        except ValueError:
                            continue
                        if not isinstance(e, dict) or str(e.get("timestamp") or "")[:19] < since:
                            continue
                        m = e.get("message") if isinstance(e.get("message"), dict) else {}
                        content = m.get("content") if isinstance(m.get("content"), list) else []
                        if e.get("type") == "assistant" and m.get("id"):
                            tools[m["id"]] += [x for x in content if isinstance(x, dict) and x.get("type") == "tool_use"]
                        for x in content:
                            if isinstance(x, dict) and x.get("type") == "tool_result":
                                t = _text(x.get("content"))
                                s["guard_refusals"] += "Foreman guard: blocked" in t or "Ward: blocked" in t
                                s["fm_refusals"] += bool(re.search(r"\bfm: (?:refused|give --run)", t))
                        a = e.get("attachment") if isinstance(e.get("attachment"), dict) else {}
                        t, src = "", str(a.get("hookName") or a.get("type") or "?").split(":")[0]
                        if a.get("type") in INJECTS:
                            t = _text(a.get("content") or a.get("response") or a.get("blockingError") or "")
                        elif a.get("type") == "hook_success" and a.get("stdout"):  # T-2667: SessionStart's text is here
                            try:
                                j = json.loads(a["stdout"])
                            except (ValueError, TypeError):
                                j = None
                            j = j if isinstance(j, dict) else {}
                            h = j.get("hookSpecificOutput") if isinstance(j.get("hookSpecificOutput"), dict) else {}
                            # not updatedToolOutput: that is the tool's own output
                            t = "\n".join(x for x in (h.get("additionalContext"), j.get("systemMessage")) if isinstance(x, str))
                        if "Sampo" in t or "Foreman" in t or "fm " in t or "sampo " in t:
                            s["injected_chars"] += len(t)
                            s["src:" + src] += len(t)
            except OSError:
                continue
            for uses in tools.values():
                s["turns"] += 1
                s["bookkeeping_turns"] += bool(uses) and all(
                    u.get("name") == "Bash" and _only_bookkeeping((u.get("input") or {}).get("command")) for u in uses)
    return {k: v for k, v in out.items() if v.get("turns")}


def by_source(sessions, turns=None):
    """[(source, injected chars per turn)] biggest first (T-2667)."""
    tot = collections.Counter()
    for s in sessions.values():
        tot.update({k[4:]: v for k, v in s.items() if k.startswith("src:")})
    n = max(turns if turns is not None else sum(s["turns"] for s in sessions.values()), 1)
    return sorted(((k, v / n) for k, v in tot.items()), key=lambda kv: -kv[1])


def cmd_burden(args):
    """fm burden: Sampo's own cost on real sessions, per Sampo version — the objective a self-change must lower."""
    from sampo import cli
    p = cli.resolve(args)
    if getattr(args, "by_verb", False):  # T-0959: the verbs no session called carry their weight for nothing
        return _prune(args, _since(args.days))
    sessions = burden(transcripts_dir(p.root), _since(args.days))
    version = {}
    for e in c.ledger_tail(p, 50000, kinds=("session_start",)):
        if e.get("event") == "session_start" and e.get("session_id"):
            version[e["session_id"]] = _version((e.get("data") or {}).get("root"))
    if getattr(args, "by_source", False):
        rows = by_source(sessions)
        return cli.out(args, {"days": args.days, "sources": dict(rows)},
                         f"Injected characters per turn by source, last {args.days:g} day(s), biggest first:\n" + (
                             "\n".join(f"  {k}: {v:.1f}" for k, v in rows) or "  none"))
    by = {}
    for sid, s in sessions.items():
        v = by.setdefault(version.get(sid, "unknown"), collections.Counter())
        v.update(s)
        v["sessions"] += 1
    rate = lambda v, k: 100 * v[k] / max(v["turns"], 1)
    text = (f"Sampo's burden, last {args.days:g} day(s), per version (lower is better):\n" + "\n".join(
        f"  {k}: {v['sessions']} session(s), {v['turns']} turns · bookkeeping-only turns {rate(v, 'bookkeeping_turns'):.0f}% · "
        f"guard refusals {rate(v, 'guard_refusals'):.1f}/100 turns · refused fm calls {rate(v, 'fm_refusals'):.1f}/100 · "
        f"injected {v['injected_chars'] / max(v['turns'], 1):.0f} chars/turn"
        for k, v in sorted(by.items(), key=lambda kv: kv[0])) if by else
            f"No sessions with turns in the last {args.days:g} day(s).")
    return cli.out(args, {"days": args.days, "versions": {k: dict(v) for k, v in by.items()},
                            "sessions": {k: dict(v, version=version.get(k, "unknown")) for k, v in sessions.items()}},
                     text)
